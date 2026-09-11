"""Live model adapter: controller-facing provider interaction.

``LiveModelAdapter`` remains the single authority for mutable live
adapter state (metrics, budgets, history, transport ownership,
operation observers, PDB session flags, gate caches, per-call usage).
Proof/gate/observation methods are inherited from ``LiveProofMixin``
(``live_proof``); request construction, the transport retry/directive
repair loop, token-usage seams, and phase accounting live here."""

from __future__ import annotations

import json, time, uuid

from typing import Any, Callable, Mapping
from agentic_debugger.agent.controller_policy import ControllerBudgetLimits
from agentic_debugger.agent.model_adapter import TransitionDirective
from agentic_debugger.agent.token_usage import TokenUsage, TokenUsageCoverage
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.agent.tool_registry import ToolRegistry
from agentic_debugger.rag.context import RagContext
from agentic_debugger.evaluation.directive_observability import (
    serialize_rejection_evidence, validate_rejection_evidence,
)

from agentic_debugger.evaluation.live_contracts import DirectiveRejectionCategory, LIVE_PROTOCOL_VERSION, LiveConfigurationError, LiveModelAdapterError, LiveTransportError, MODEL_HISTORY_WINDOW, ModelRequestBudgetExceeded, PROOF_HISTORY_WINDOW, _proof_observation_for_provider, redact_for_recording
from agentic_debugger.evaluation.live_directive_parse import _parse, _resolve_provider_directive
from agentic_debugger.evaluation.live_directive_schema import _legal_transition_targets
from agentic_debugger.evaluation.live_proof import LiveProofMixin
from agentic_debugger.evaluation.live_usage import LiveModelMetrics, _LogicalRequestUsage


class LiveModelAdapter(LiveProofMixin):
    def __init__(self,*,task,policy,config,transport,limits,registry=None,evaluation_id="evaluation",case_id="case",run_id="run",trajectory_id="trajectory",clock=time.monotonic,rag_context=None,proof_required=False,proof_source_line=1,proof_observed_local_names=(),model_visible_budget_limits=None,model_visible_task=None,progress_observer: Callable[[str], None] | None = None,operation_observer: Callable[[Mapping[str, Any]], None] | None = None):
        if type(registry) is not ToolRegistry:
            raise LiveConfigurationError("live tool registry is required")
        if type(proof_source_line) is not int or proof_source_line < 1:
            raise LiveConfigurationError("proof source line must be a positive integer")
        if (
            not isinstance(proof_observed_local_names, (list, tuple))
            or any(type(name) is not str or not name for name in proof_observed_local_names)
            or len(set(proof_observed_local_names)) != len(proof_observed_local_names)
        ):
            raise LiveConfigurationError("proof observed local names are invalid")
        self.model_name=config.model_name; self.task=task; self.policy=policy; self.config=config; self.transport=transport; self.limits=limits; self.registry=registry; self.evaluation_id=evaluation_id; self.case_id=case_id; self.run_id=run_id; self.trajectory_id=trajectory_id; self.metrics=LiveModelMetrics(); self.clock=clock; self.model_phase_elapsed_seconds=0.0; self.history=[]; self.pdb_gate_decisions=[]; self.directive_rejections=[]; self.directive_rejection_evidence=[]; self.directive_attempts=[]; self.proof_cycle_events=[]; self.proof_required=bool(proof_required); self.proof_source_line=proof_source_line; self.proof_observed_local_names=tuple(proof_observed_local_names); self._proof_observations=[]
        # Optional RAG context: when None (the default) the public request is
        # byte-for-byte unchanged and ``retrieved_context`` is never emitted.
        # When supplied it must be a validated RagContext; arbitrary
        # lookalike objects are rejected at this boundary (repair 1).
        if rag_context is not None and not isinstance(rag_context, RagContext):
            raise LiveConfigurationError("rag_context must be a validated RagContext")
        self._rag_context=rag_context
        self.model_visible_budget_limits = model_visible_budget_limits or ControllerBudgetLimits.from_task_constraints(task.constraints)
        if progress_observer is not None and not callable(progress_observer):
            raise LiveConfigurationError("progress_observer must be callable or None")
        self._progress_observer = progress_observer
        # Structured operation telemetry seam: the adapter reports facts it
        # genuinely owns from completed tool observations (source ranges,
        # debugger lifecycle, PDB proof observations, candidate outcomes).
        # The channel is observer-only and side-effect safe; it never changes
        # decisions, metrics, budgets, or recorded evidence.
        if operation_observer is not None and not callable(operation_observer):
            raise LiveConfigurationError("operation_observer must be callable or None")
        self._operation_observer = operation_observer
        if model_visible_task is None:
            self.model_visible_task = task.agent_visible_mapping()
        elif not isinstance(model_visible_task, Mapping):
            raise LiveConfigurationError("model_visible_task must be a mapping")
        else:
            self.model_visible_task = dict(model_visible_task)
        self._failure_reproduced = False
        self._pdb_session_active = False
        self._runtime_transition_authorized = False
        self._post_patch_f2p_collected = False
        self._regression_collected = False
        # Per-logical-call PDB gate cache: ``(model_call_index, decision)``.
        # ``model_call_index`` is constant across the transport retry loop and
        # increments only on the next controller step, so it is the natural
        # per-:func:`next_directive` scope.  The cache is reused by every reread
        # (``_runtime_transition_allowed``, ``_effective_contract``,
        # ``_request_context`` and ``legal_transition_targets``) so the gate is
        # evaluated at most once per logical call and every consumer sees the
        # identical decision.  ``_pdb_gate_recorded_for_index`` bounds recording
        # to at most one append per logical call regardless of retry count.
        self._pdb_gate_decision_cache: tuple[int, Any] | None = None
        self._pdb_gate_recorded_for_index: int | None = None
        # Provider-reported usage of the most recent logical model call
        # (reset at the top of every ``next_directive``).  ``None`` until
        # the first provider-completed response of the current call.
        self._call_usage: _LogicalRequestUsage | None = None

    def reconcile_tool_dispatch(self, controller_result: Any) -> None:
        """Bind dispatch truth from completed controller/tool steps.

        Parsing can only establish acceptance.  A controller step carrying an
        action and authoritative observation proves the tool was dispatched;
        transitions are explicitly non-tool and remain ``None``.
        """
        steps = getattr(controller_result, "steps", ())
        dispatched = {
            step.model_call_index
            for step in steps
            if getattr(step, "action", None) is not None
            and getattr(step, "observation", None) is not None
        }
        for attempt in self.directive_attempts:
            if attempt.get("directive_accepted") is not True:
                continue
            directive = attempt.get("directive")
            if not isinstance(directive, Mapping):
                attempt["tool_dispatched"] = None
            elif directive.get("kind") != "action":
                attempt["tool_dispatched"] = None
            else:
                attempt["tool_dispatched"] = attempt.get("model_call_index") in dispatched

    def _notify_operation(self, record: Mapping[str, Any]) -> None:
        if self._operation_observer is None:
            return
        try:
            self._operation_observer(record)
        except Exception:
            # Telemetry must never alter the scientific path.
            pass

    def _request_context(self, snapshot, *, logical_request_index: int, transport_attempt_index: int, contracts=None, legal_targets=None, directive_schema=None, rejection: Mapping[str, Any] | None = None):
        if contracts is None:
            contracts = self._effective_contract(snapshot)
        if legal_targets is None:
            legal_targets = _legal_transition_targets(
                snapshot.state,
                pdb_transition_allowed=self._cached_pdb_gate_decision(snapshot).allowed,
                patch_allowed=self._proof_patch_allowed(),
            )
        if directive_schema is None:
            directive_schema = self._effective_directive_schema(snapshot)
        request_id = f"{self.run_id}:model-call:{logical_request_index}:attempt:{transport_attempt_index}:{uuid.uuid4().hex}"
        runtime_allowed = self._runtime_transition_allowed(snapshot)
        effective_actions = list(contracts)
        history_window = PROOF_HISTORY_WINDOW if self.proof_required else MODEL_HISTORY_WINDOW
        history = list(self.history[-history_window:])
        if (
            self.proof_required
            and history
            and history[-1].get("request_index") == logical_request_index
        ):
            # The current snapshot is represented authoritatively in the
            # controller object below. Do not duplicate it before the current
            # directive even exists.
            history = history[:-1]
        last_observation = snapshot.last_observation.to_mapping() if snapshot.last_observation else None
        if self.proof_required:
            last_observation = _proof_observation_for_provider(last_observation)
        visible_limits = self.model_visible_budget_limits
        payload = {"protocol":{"name":"agentic-debugger-live-jsonl","version":LIVE_PROTOCOL_VERSION,"request_id":request_id,"logical_model_call_index":logical_request_index,"transport_attempt_index":transport_attempt_index},"identity":{"evaluation_id":self.evaluation_id,"case_id":self.case_id,"run_id":self.run_id,"trajectory_id":self.trajectory_id},"task":self.model_visible_task,"policy":self.policy.value,"directive_schema":directive_schema,"action_contracts":contracts,"controller":{"state":snapshot.state.value,"task_id":snapshot.task_id,"model_call_index":snapshot.model_call_index,"allowed_actions":effective_actions,"legal_transition_targets":legal_targets,"budget_limits":{"max_patch_attempts":visible_limits.max_patch_attempts,"max_test_runs":visible_limits.max_test_runs,"max_pdb_observations":visible_limits.max_pdb_observations,"max_active_hypotheses":visible_limits.max_active_hypotheses,"max_source_observations":visible_limits.max_source_observations},"budget_state":{"patch_attempts":snapshot.budget_state.patch_attempts,"test_runs":snapshot.budget_state.test_runs,"pdb_observations":snapshot.budget_state.pdb_observations,"source_observations":snapshot.budget_state.source_observations},"hypotheses":self._hypotheses(snapshot),"last_observation":last_observation},"history":history,"directive_feedback":dict(rejection) if rejection else None,"instructions":"Return one directive JSON object. The request is the complete bounded current context; do not rely on process-local memory. Never return credentials. The 'directive_feedback' field is always present; it is null on the first transport attempt. When 'directive_feedback' is non-null, the previous transport attempt's directive was rejected for the stated category; do not repeat it, and choose a directive that satisfies the allowed_actions, legal_transition_targets, and action_contracts already advertised in this request."}
        if self.proof_required:
            payload["instructions"] = "Return one legal directive JSON object from current contracts. Context is complete; use no memory or credentials. Do not repeat non-null directive_feedback."
            if snapshot.state is ControllerState.REPRODUCE:
                if self._failure_reproduced:
                    payload["instructions"] += " Exact proof: baseline recorded; use the sole legal transition to Understand."
                else:
                    payload["instructions"] += " Exact proof: run the advertised baseline once; PDB is unavailable now."
            elif snapshot.state is ControllerState.RUNTIME_EVIDENCE:
                payload["proof_gate"] = self._proof_runtime_progress()
                payload["instructions"] += " Exact proof: use only proof_gate.next_required_actions. Break inside the target function, never on def/import/module code. Collect one stack, locals, and step-or-next; stop the session. Never continue."
            elif snapshot.state is ControllerState.UNDERSTAND:
                payload["instructions"] += " Exact proof: before PDB inspect source and add a runtime hypothesis; afterward revise from observation ids, then diagnose."
            elif snapshot.state is ControllerState.PATCH:
                payload["instructions"] += " Exact proof complete: submit a legal patch and follow the advertised lifecycle."
            elif snapshot.state is ControllerState.VALIDATE:
                payload["instructions"] += " Exact proof complete: collect advertised validation evidence before finishing."
        if self._rag_context is not None:
            payload["retrieved_context"] = self._rag_context.to_request_mapping()
        return payload
    def next_directive(self,snapshot):
        self._observe_snapshot(snapshot)
        self.metrics.logical_model_calls += 1
        # One logical model call: per-call usage aggregation restarts here
        # so a stale value from the previous call can never be reported.
        self._call_usage=_LogicalRequestUsage()
        # The gate is consumed from ``UNDERSTAND``.  ``_runtime_transition_authorized``
        # marks that the controller is already inside an authorized RUNTIME_EVIDENCE
        # visit; it is reset to ``False`` whenever the controller has left that
        # state, so a fresh ``UNDERSTAND -> RUNTIME_EVIDENCE`` lifecycle (a later
        # controller step with a different ``model_call_index``) is a distinct
        # gate consumption rather than a reread of the prior visit.
        if snapshot.state is not ControllerState.RUNTIME_EVIDENCE and self._runtime_transition_authorized:
            self._runtime_transition_authorized = False
        if snapshot.state is ControllerState.RUNTIME_EVIDENCE and not self._runtime_transition_authorized:
            self._runtime_transition_authorized = self._runtime_transition_allowed(snapshot)
        effective_contract = self._effective_contract(snapshot)
        directive_schema = self._effective_directive_schema(snapshot)
        legal_targets = _legal_transition_targets(snapshot.state, pdb_transition_allowed=self._cached_pdb_gate_decision(snapshot).allowed, patch_allowed=self._proof_patch_allowed())
        if (
            self.proof_required
            and snapshot.state is ControllerState.REPRODUCE
            and self._failure_reproduced
        ):
            legal_targets = [ControllerState.UNDERSTAND.value]
        if self.proof_required and snapshot.state is ControllerState.UNDERSTAND:
            active = snapshot.hypotheses.active_hypotheses()
            hypothesis = active[0] if active else None
            if not self._proof_diagnosis_ready():
                legal_targets = (
                    [ControllerState.RUNTIME_EVIDENCE.value]
                    if hypothesis is not None and hypothesis.requires_runtime_evidence
                    else []
                )
            elif self._proof_patch_allowed():
                legal_targets = [ControllerState.PATCH.value]
            else:
                legal_targets = []
        if (
            self.proof_required
            and snapshot.state is ControllerState.RUNTIME_EVIDENCE
        ):
            legal_targets = (
                [ControllerState.UNDERSTAND.value]
                if self._proof_diagnosis_ready() and not self._pdb_session_active
                else []
            )
        logical_request_index = snapshot.model_call_index
        history_observation = redact_for_recording(snapshot.last_observation.to_mapping()) if snapshot.last_observation else None
        if self.proof_required:
            history_observation = _proof_observation_for_provider(history_observation)
        history_entry={"request_index":logical_request_index,"state":snapshot.state.value,"allowed_actions":list(effective_contract),"last_observation":history_observation}
        self.history.append(history_entry)
        del self.history[:-MODEL_HISTORY_WINDOW]
        rejection: dict[str, Any] | None = None
        # Provider/transport retries and directive repairs are distinct
        # bounded concepts.  A transport failure (timeout, HTTP, process)
        # may be retried only under ``max_retries``.  A malformed or
        # illegal model directive is never a network retry: it consumes a
        # directive repair (same controller snapshot, typed
        # directive_feedback on the next request, no controller advance,
        # no tool dispatch) under ``max_directive_repairs``.  Every attempt
        # is a real model request counted indefinitely as telemetry.
        # Task 44: ``max_model_requests=None`` means unbounded — no
        # total-session request ceiling.  A finite value is honored only
        # for explicit callers (frozen treatments/operator/harness).
        transport_retries_used = 0
        directive_repairs_used = 0
        attempt = 0
        while True:
            attempt += 1
            _request_ceiling = self.limits.max_model_requests
            if _request_ceiling is not None and self.metrics.model_requests>=_request_ceiling: self.metrics.termination_reason="model_request_limit"; raise LiveModelAdapterError("live model request limit reached")
            request=redact_for_recording(self._request_context(snapshot,logical_request_index=logical_request_index,transport_attempt_index=attempt,contracts=effective_contract,legal_targets=legal_targets,directive_schema=directive_schema,rejection=rejection))
            final_content: str | None = None
            try:
                request_bytes=json.dumps(request,ensure_ascii=False,allow_nan=False).encode("utf-8")
            except (TypeError,ValueError,UnicodeError):
                self.metrics.termination_reason="request_serialization"; raise LiveModelAdapterError("live model context could not be serialized") from None
            # Provider-owned request size (Task 43): the intended request is
            # handed to the configured transport regardless of its
            # serialized size.  No Agentic-Debugger-owned byte/token/
            # character ceiling is enforced here — neither the response
            # bound (which constrains only provider output capture) nor
            # any historical public-request budget.  A provider that
            # rejects an oversized request surfaces a provider/transport
            # failure truthfully through the transport boundary below.
            self.metrics.model_requests+=1
            self.metrics.transport_attempts+=1
            self.metrics.cumulative_request_bytes += len(request_bytes)
            self.metrics.max_request_bytes = max(self.metrics.max_request_bytes, len(request_bytes))
            # The cumulative model-phase bound is an emergency guard checked
            # between calls.  A currently progressing streamed response is
            # governed by the transport's inactivity watchdog and is not cut
            # off merely because a wall-clock slice elapsed mid-response.
            self._remaining()
            # The frozen model-phase guard is the authoritative outer bound
            # for long-running cloud decisions. Configured transports retain
            # their shorter request bound through ``min``; ladder configs set
            # it to the rung guard and pass the remaining phase time here.
            timeout_seconds=min(self.config.request_timeout_seconds, self._remaining())
            phase_started=self.clock()
            try:
                if self._progress_observer is not None:
                    self._progress_observer("model_running")
                response=self.transport.request(request,timeout_seconds)
                if not isinstance(response,Mapping): raise LiveModelAdapterError("invalid model response",category=DirectiveRejectionCategory.MALFORMED_DIRECTIVE,detail="model response was not a JSON object")
                self.metrics.model_responses+=1
                usage=response.get("usage")
                self.metrics.usage(usage)
                self._call_usage.add_provider_response(usage)
                self.metrics.activity(response.get("transport_activity"))
                attempt_record={
                    "model_call_index": logical_request_index,
                    "transport_attempt_index": attempt,
                    "state": snapshot.state.value,
                    "directive": None,
                    "provider_transport_completed": True,
                    "directive_accepted": False,
                    "tool_dispatched": False,
                    "accepted": False,
                    "rejection": None,
                    "directive_transport_normalized": False,
                    "normalization_schema_version": None,
                    "normalization_policy_id": None,
                    "normalization_kind": None,
                    "normalization_before": None,
                    "normalization_after": None,
                    "normalization_removed_prefix": None,
                    "normalization_removed_suffix": None,
                }
                self.directive_attempts.append(attempt_record)
                del self.directive_attempts[:-256]
                raw_directive, final_content, normalization = _resolve_provider_directive(response)
                if normalization is not None:
                    attempt_record.update(normalization)
                attempt_record["directive"] = redact_for_recording(raw_directive)
                # Canonical PDB gate recording point.  The model's real
                # ``UNDERSTAND -> RUNTIME_EVIDENCE`` transition *attempt* is
                # visible in ``raw_directive`` before :func:`_parse` can reject
                # a denied transition as ``ILLEGAL_TRANSITION`` (a denied gate
                # removes RUNTIME_EVIDENCE from ``legal_transition_targets``).
                # Recording here captures both allowed and denied real attempts
                # exactly once, bounded per logical call by
                # ``_pdb_gate_recorded_for_index`` so repeated malformed or
                # denied transport attempts in the same call never re-append.
                if (
                    isinstance(raw_directive, Mapping)
                    and raw_directive.get("kind") == "transition"
                    and raw_directive.get("target_state") == ControllerState.RUNTIME_EVIDENCE.value
                    and snapshot.state is ControllerState.UNDERSTAND
                    and not self._runtime_transition_authorized
                    and self.policy is not DemoPolicy.STATIC_BASELINE
                    and self._pdb_gate_recorded_for_index != snapshot.model_call_index
                ):
                    gate_decision = self._cached_pdb_gate_decision(snapshot)
                    self._record_pdb_gate_decision(snapshot, gate_decision)
                    self._pdb_gate_recorded_for_index = snapshot.model_call_index
                contracts = effective_contract
                directive=_parse(raw_directive,snapshot,action_contracts=contracts,directive_kinds=set(directive_schema),directive_schema=directive_schema,legal_transition_targets=set(legal_targets))
                attempt_record["accepted"] = True
                attempt_record["directive_accepted"] = True
                attempt_record["tool_dispatched"] = None
                if isinstance(directive, TransitionDirective) and directive.target_state is ControllerState.RUNTIME_EVIDENCE:
                    self._runtime_transition_authorized = True
                self.history[-1]["directive"]=redact_for_recording(raw_directive)
                return directive
            except ModelRequestBudgetExceeded as exc:
                # Historical compatibility only: no new execution raises
                # this (request size is provider-owned since Task 43), but
                # a legacy/frozen transport may still raise it.  The logical
                # call stays unaccounted and the case terminates with the
                # typed historical termination reason.
                self.metrics.model_requests-=1
                self.metrics.termination_reason="public_evidence_budget_exceeded"
                raise
            except LiveTransportError as exc:
                rejection=None
                self.metrics.error(exc.kind)
                if transport_retries_used<self.limits.max_retries:
                    transport_retries_used+=1; self.metrics.retries+=1; continue
                self.metrics.termination_reason="request_timeout" if exc.timed_out else "provider_or_transport_error"; raise LiveModelAdapterError(
                    "model transport failed",
                    directive_rejection=False,
                    error_kind=exc.kind,
                    safe_message=exc.safe_message or str(exc),
                ) from None
            except LiveModelAdapterError as exc:
                if not exc.directive_rejection:
                    raise
                if final_content is not None and exc.content is None:
                    exc.content = final_content
                rejection={"category":exc.category.value,"message":exc.detail or "the directive was rejected","rejected_transport_attempt":attempt}
                if (
                    self.directive_attempts
                    and self.directive_attempts[-1].get("model_call_index") == logical_request_index
                    and self.directive_attempts[-1].get("transport_attempt_index") == attempt
                    and self.directive_attempts[-1].get("accepted") is False
                ):
                    self.directive_attempts[-1]["rejection"] = dict(rejection)
                evidence = serialize_rejection_evidence(stage=exc.stage, category=exc.category.value, reason_code=exc.reason_code, reason=exc.detail, content=exc.content)
                if validate_rejection_evidence(evidence):
                    self.directive_rejection_evidence.append(evidence)
                    if self.directive_attempts and self.directive_attempts[-1].get("model_call_index") == logical_request_index and self.directive_attempts[-1].get("transport_attempt_index") == attempt:
                        self.directive_attempts[-1]["rejection_evidence"] = evidence
                self.directive_rejections.append(dict(rejection))
                self.metrics.directive_rejection(exc.category.value)
                if directive_repairs_used<self.limits.max_directive_repairs:
                    directive_repairs_used+=1
                    self.metrics.directive_repair()
                    continue
                self.metrics.termination_reason="directive_rejected"; raise
            finally:
                self.model_phase_elapsed_seconds += max(0.0,self.clock()-phase_started)

    def last_request_token_usage(self) -> TokenUsage | None:
        """Provider-reported usage of the most recent logical model call.

        Optional adapter seam consumed by the controller after a model
        request completes (success or failure).  The value aggregates
        every provider-completed transport attempt inside that call —
        transport retries and directive repairs included — under the
        canonical counts-only contract.  ``None`` means no usable usage
        was reported; it is never a zero claim.
        """
        return self._call_usage.build() if self._call_usage is not None else None

    def last_request_token_coverage(self) -> TokenUsageCoverage | None:
        """Provider-reported usage coverage of the most recent logical model call.

        Optional adapter seam consumed by the controller alongside token usage.
        Indicates whether reported dimensions have complete coverage across all
        provider-completed attempts or are truthful lower bounds.
        """
        return self._call_usage.build_coverage() if self._call_usage is not None else None

    def _remaining(self):
        left=self.limits.max_model_phase_seconds-self.model_phase_elapsed_seconds
        if left<=0: self.metrics.termination_reason="elapsed_time_limit"; raise LiveModelAdapterError("live elapsed time limit reached")
        return left
