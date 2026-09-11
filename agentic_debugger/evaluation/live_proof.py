"""PDB proof/gate/observation layer of the live model adapter.

``LiveProofMixin`` owns the proof-state methods of ``LiveModelAdapter``
(PDB gate evaluation with per-logical-call caching, proof-observation
selection, proof-cycle bookkeeping, operation-fact projection, and the
effective directive/contract derivation under proof).  It defines no
``__init__`` and owns no counters, budgets, transport, or cleanup:
``LiveModelAdapter`` remains the single authority for all mutable
adapter state; the mixin only operates on it."""

from __future__ import annotations

from dataclasses import field
from typing import Any, Mapping
from agentic_debugger.agent.controller_policy import ActionName, PdbGateContext, PdbPolicy, decide_pdb_access
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.proof_gate import validate_pdb_patch_evidence, validate_pdb_runtime_evidence
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.events.schema import Observation

from agentic_debugger.evaluation.live_directive_schema import _action_contracts_for_state, _directive_schema_for_state


class LiveProofMixin:
    """PDB proof/gate/observation methods (see module docstring)."""

    @staticmethod
    def _hypotheses(snapshot):
        return [{"hypothesis_id":item.hypothesis_id,"statement":item.statement,"confidence":item.confidence.value,"status":item.status.value,"evidence_refs":list(item.evidence_refs),"requires_runtime_evidence":item.requires_runtime_evidence,"revision":item.revision} for item in snapshot.hypotheses.hypotheses]
    def _control_matches_selected_proof_contract(self, observation: Observation) -> bool:
        starts = [
            item
            for item in self._proof_observations
            if item.name == ActionName.START_PDB_SESSION.value
            and item.status.value == "ok"
        ]
        if len(starts) != 1:
            return False
        proof = starts[0].payload.get("proof")
        return (
            type(proof) is dict
            and observation.payload.get("state") == "paused"
            and observation.payload.get("script") == proof.get("production_file")
            and observation.payload.get("function") == proof.get("production_frame")
        )

    def _reset_selected_runtime_proof_cycle(self, observation: Observation) -> None:
        runtime_names = {
            ActionName.START_PDB_SESSION.value,
            ActionName.GET_STACK_SUMMARY.value,
            ActionName.GET_FRAME_LOCALS.value,
            ActionName.STEP_PDB_SESSION.value,
            ActionName.NEXT_PDB_SESSION.value,
            ActionName.STOP_PDB_SESSION.value,
        }
        removed = [
            item.observation_id
            for item in self._proof_observations
            if item.name in runtime_names
        ]
        self._proof_observations = [
            item for item in self._proof_observations if item.name not in runtime_names
        ]
        self.proof_cycle_events.append({
            "schema_version": "pdb-proof-cycle-event-v1",
            "event": "selected_runtime_cycle_reset",
            "trigger_observation_id": observation.observation_id,
            "trigger_action": observation.name,
            "trigger_state": observation.payload.get("state"),
            "reason": "control did not pause in the declared production frame and the session is inactive",
            "removed_selected_observation_ids": removed,
            "trigger_retained_in_trajectory": True,
        })

    @staticmethod
    def _pdb_location_from_payload(payload: Mapping[str, Any]) -> tuple[str | None, int | None]:
        proof = payload.get("proof") if isinstance(payload.get("proof"), Mapping) else None
        if proof is not None:
            file_name = proof.get("production_file")
            line = proof.get("breakpoint_line")
            if isinstance(file_name, str) and type(line) is int and line > 0:
                return file_name, line
        frames = payload.get("frames")
        if isinstance(frames, list) and frames:
            ordered = sorted(
                (frame for frame in frames if isinstance(frame, Mapping)),
                key=lambda frame: not frame.get("is_current"),
            )
            for frame in ordered:
                script = frame.get("script")
                line = frame.get("line")
                if isinstance(script, str) and type(line) is int and line > 0:
                    return script, line
        return None, None

    #: Handler-level dispatch reasons: the apply_patch handler actually ran
    #: (and consumed a patch attempt) so its outcome is a real candidate
    #: attempt result.  Dispatch-level rejections never proposed a patch.
    _HANDLER_OUTCOME_REASONS = frozenset({"tool_rejected", "tool_error", "tool_timeout"})

    def _project_operation_facts(self, observation: Any) -> None:
        """Emit bounded structured facts the adapter genuinely owns.

        Only tool names, file ranges, breakpoint identities, statuses and
        bounded diagnostics cross this seam -- never prompts, model text,
        completions, or private test identities.
        """
        if self._operation_observer is None or type(observation) is not Observation:
            return
        name = observation.name
        status = observation.status.value
        payload = observation.payload if isinstance(observation.payload, Mapping) else {}
        if name == ActionName.GET_SOURCE_WINDOW.value:
            if status == "ok":
                path = payload.get("path")
                start = payload.get("start_line")
                end = payload.get("end_line")
                if (
                    isinstance(path, str) and path
                    and type(start) is int and type(end) is int
                    and 0 < start <= end
                ):
                    self._notify_operation({
                        "operation": "source_inspection",
                        "tool": name,
                        "file": path,
                        "start_line": start,
                        "end_line": end,
                    })
            return
        if name == ActionName.APPLY_PATCH.value:
            reason = payload.get("diagnostic")
            bounded_reason = (
                reason if isinstance(reason, str) and reason
                else str(payload.get("dispatch_reason") or status)
            )[:200]
            if status == "ok":
                files = payload.get("changed_files")
                changed = [
                    item for item in (files if isinstance(files, list) else ())
                    if isinstance(item, str)
                ]
                self._notify_operation({
                    "operation": "candidate",
                    "phase": "applied",
                    "reason": bounded_reason,
                    "changed_files": changed[:16],
                })
            elif payload.get("dispatch_reason") in self._HANDLER_OUTCOME_REASONS:
                phase = {
                    "rejected": "rejected",
                    "timeout": "failed",
                    "error": "failed",
                }.get(status)
                if phase is not None:
                    self._notify_operation({
                        "operation": "candidate",
                        "phase": phase,
                        "reason": bounded_reason,
                    })
            return
        if name == ActionName.REVERT_PATCH.value:
            if status == "ok":
                self._notify_operation({
                    "operation": "candidate",
                    "phase": "reverted",
                    "reason": "candidate reverted",
                })
            return
        if name == ActionName.START_PDB_SESSION.value:
            if status == "ok" and payload.get("state") == "paused":
                script = payload.get("script")
                line = payload.get("breakpoint_line")
                if isinstance(script, str) and script and type(line) is int and line > 0:
                    self._notify_operation({
                        "operation": "debugger_active",
                        "script": script,
                        "breakpoint_line": line,
                    })
            return
        if name in {
            ActionName.GET_STACK_SUMMARY.value,
            ActionName.GET_FRAME_LOCALS.value,
            ActionName.SAFE_EVAL_EXPRESSION.value,
        } and status == "ok":
            record: dict[str, Any] = {"operation": "pdb_observation"}
            script, line = self._pdb_location_from_payload(payload)
            if script is not None and line is not None:
                record["script"] = script
                record["line"] = line
            self._notify_operation(record)

    def _observe_snapshot(self, snapshot: ControllerSnapshot) -> None:
        observation = snapshot.last_observation
        if observation is None:
            return
        # Structured operation facts are projected before any gate/proof
        # bookkeeping: they are pure telemetry over the same observation.
        self._project_operation_facts(observation)
        # Failed/rejected attempts remain in the authoritative event stream, but
        # they are not proof.  Keeping only successful observations here lets a
        # model recover from a rejected debugger action without either erasing
        # the failed event or poisoning the later exact proof chain.
        if (
            self.proof_required
            and type(observation) is Observation
            and observation.status.value == "ok"
        ):
            already_selected = any(
                item.observation_id == observation.observation_id
                for item in self._proof_observations
            )
            already_recorded_as_control_event = any(
                item.get("trigger_observation_id") == observation.observation_id
                for item in self.proof_cycle_events
            )
            is_control = observation.name in {
                ActionName.STEP_PDB_SESSION.value,
                ActionName.NEXT_PDB_SESSION.value,
            }
            if (
                is_control
                and not already_selected
                and not already_recorded_as_control_event
                and not self._control_matches_selected_proof_contract(observation)
            ):
                if observation.payload.get("state") != "paused":
                    self._reset_selected_runtime_proof_cycle(observation)
                else:
                    self.proof_cycle_events.append({
                        "schema_version": "pdb-proof-cycle-event-v1",
                        "event": "control_observation_not_selected",
                        "trigger_observation_id": observation.observation_id,
                        "trigger_action": observation.name,
                        "trigger_state": observation.payload.get("state"),
                        "reason": "control paused outside the declared production frame",
                        "removed_selected_observation_ids": [],
                        "trigger_retained_in_trajectory": True,
                    })
            elif not already_selected and not already_recorded_as_control_event:
                self._proof_observations.append(observation)
        if observation.name in {
            ActionName.APPLY_PATCH.value,
            ActionName.REVERT_PATCH.value,
        }:
            # The candidate outcome itself is projected through the
            # structured operation channel (``_project_operation_facts``);
            # stage-level detail would only invite unstructured claims.
            # A new or reverted candidate invalidates prior Validate evidence.
            self._post_patch_f2p_collected = False
            self._regression_collected = False
            return
        if observation.status.value != "ok":
            return
        payload = observation.payload
        if observation.name == ActionName.RUN_REPRODUCTION.value:
            phase = payload.get("phase")
            if phase == "baseline":
                if type(payload.get("failure_reproduced")) is bool:
                    self._failure_reproduced = payload["failure_reproduced"]
            elif phase == "post_patch":
                self._post_patch_f2p_collected = True
        elif observation.name == ActionName.RUN_REGRESSION_TESTS.value:
            self._regression_collected = True
        elif observation.name == ActionName.START_PDB_SESSION.value:
            self._pdb_session_active = payload.get("state") == "paused"
            if self._pdb_session_active and self._progress_observer is not None:
                self._progress_observer("debugger")
        elif observation.name in {
            ActionName.CONTINUE_PDB_SESSION.value,
            ActionName.STEP_PDB_SESSION.value,
            ActionName.NEXT_PDB_SESSION.value,
        }:
            # Execution control may either pause again or exit the target.  The
            # next request must not advertise session-only tools after exit.
            self._pdb_session_active = payload.get("state") == "paused"
        elif observation.name == ActionName.STOP_PDB_SESSION.value:
            self._pdb_session_active = payload.get("stopped") is True
            if self._pdb_session_active:
                self._pdb_session_active = False

    def _evaluate_pdb_gate(self, snapshot: ControllerSnapshot) -> object:
        """Pure PDB gate evaluation; never appends to ``pdb_gate_decisions``.

        Mirrors :func:`decide_pdb_access` consumed by the contained
        reachability driver (``contained_pdb.py``): the decision is a function
        of policy, source state, failure reproduction, remaining observations,
        failed patch attempts and the active hypothesis only.
        """
        active = snapshot.hypotheses.active_hypotheses()
        source_state = snapshot.state
        if source_state is ControllerState.RUNTIME_EVIDENCE:
            # RuntimeEvidence is reached only after an authorized transition
            # from Understand. This keeps the accepted gate's source-state
            # semantics while preserving the lifecycle state in the request.
            source_state = ControllerState.UNDERSTAND
        decision = decide_pdb_access(
            PdbPolicy.DISABLED
            if self.policy is DemoPolicy.STATIC_BASELINE
            else PdbPolicy.ON_UNCERTAINTY,
            PdbGateContext(
                source_state=source_state,
                failure_reproduced=self._failure_reproduced,
                remaining_pdb_observations=max(
                    0,
                    snapshot.budget_limits.max_pdb_observations
                    - snapshot.budget_state.pdb_observations,
                ),
                failed_patch_attempts=snapshot.budget_state.patch_attempts,
                active_hypothesis=active[0] if active else None,
            ),
        )
        return decision

    def _record_pdb_gate_decision(self, snapshot: ControllerSnapshot, decision: object) -> None:
        """Append one PDB gate decision to the public record.

        Called exactly once per real ``UNDERSTAND -> RUNTIME_EVIDENCE`` gate
        consumption, bounded by ``_pdb_gate_recorded_for_index`` so repeated
        malformed or denied transport attempts within the same logical call
        never re-append an identical decision.
        """
        active = snapshot.hypotheses.active_hypotheses()
        source_state = snapshot.state
        if source_state is ControllerState.RUNTIME_EVIDENCE:
            source_state = ControllerState.UNDERSTAND
        self.pdb_gate_decisions.append({
            "source_state": source_state.value,
            "failure_reproduced": self._failure_reproduced,
            "remaining_pdb_observations": max(0, snapshot.budget_limits.max_pdb_observations - snapshot.budget_state.pdb_observations),
            "failed_patch_attempts": snapshot.budget_state.patch_attempts,
            "active_hypothesis_id": active[0].hypothesis_id if active else None,
            "active_hypothesis_confidence": active[0].confidence.value if active else None,
            "active_hypothesis_requires_runtime_evidence": active[0].requires_runtime_evidence if active else None,
            "allowed": decision.allowed,
            "reason": decision.reason.value,
        })

    def _cached_pdb_gate_decision(self, snapshot: ControllerSnapshot) -> object:
        """Return the gate decision for this logical call, computing it once.

        ``model_call_index`` is constant across the transport retry loop and
        increments only on the next controller step, so caching by it scopes
        the decision to a single :func:`next_directive` call and guarantees
        every reread within that call sees the same decision.
        """
        cached = self._pdb_gate_decision_cache
        if cached is not None and cached[0] == snapshot.model_call_index:
            return cached[1]
        decision = self._evaluate_pdb_gate(snapshot)
        self._pdb_gate_decision_cache = (snapshot.model_call_index, decision)
        return decision

    def _runtime_transition_allowed(self, snapshot: ControllerSnapshot) -> bool:
        if self.policy is DemoPolicy.STATIC_BASELINE:
            return False
        return bool(self._cached_pdb_gate_decision(snapshot).allowed)

    def _proof_patch_allowed(self) -> bool:
        if not self.proof_required:
            return True
        return validate_pdb_patch_evidence(self._proof_observations)[0]

    def _proof_diagnosis_ready(self) -> bool:
        """Return whether the pre-diagnosis exact-PDB observations are ready."""

        if not self.proof_required:
            return True
        return validate_pdb_runtime_evidence(self._proof_observations)[0]

    def _proof_runtime_progress(self) -> dict[str, Any]:
        """Expose the exact proof's next bounded action without oracle data."""

        successful_names = [
            observation.name
            for observation in self._proof_observations
            if observation.status.value == "ok"
        ]
        counts = {name: successful_names.count(name) for name in set(successful_names)}
        start_ready = counts.get(ActionName.START_PDB_SESSION.value, 0) == 1
        stack_ready = counts.get(ActionName.GET_STACK_SUMMARY.value, 0) == 1
        locals_ready = counts.get(ActionName.GET_FRAME_LOCALS.value, 0) == 1
        control_count = sum(
            counts.get(name.value, 0)
            for name in (ActionName.STEP_PDB_SESSION, ActionName.NEXT_PDB_SESSION)
        )
        diagnosis_ready = self._proof_diagnosis_ready()

        if not start_ready:
            next_actions = [ActionName.START_PDB_SESSION.value]
        elif not stack_ready:
            next_actions = [ActionName.GET_STACK_SUMMARY.value]
        elif not locals_ready:
            next_actions = [ActionName.GET_FRAME_LOCALS.value]
        elif control_count != 1:
            # Exact proof must remain in the declared production frame.  A
            # ``step`` on a call expression can descend into a helper and make
            # otherwise valid evidence unusable; ``next`` executes that line
            # while preserving the caller frame.
            next_actions = [ActionName.NEXT_PDB_SESSION.value]
        elif self._pdb_session_active:
            next_actions = [ActionName.STOP_PDB_SESSION.value]
        else:
            next_actions = []

        return {
            "next_required_actions": next_actions,
            "pre_diagnosis_ready": diagnosis_ready,
            "session_active": self._pdb_session_active,
        }

    def _unique_proof_observation(self, name: ActionName) -> Observation | None:
        matches = [
            observation
            for observation in self._proof_observations
            if observation.name == name.value and observation.status.value == "ok"
        ]
        return matches[0] if len(matches) == 1 else None

    def _proof_evidence_bindings(self) -> dict[str, Any] | None:
        """Derive exact argument values solely from public runtime evidence."""

        start = self._unique_proof_observation(ActionName.START_PDB_SESSION)
        stack = self._unique_proof_observation(ActionName.GET_STACK_SUMMARY)
        locals_observation = self._unique_proof_observation(ActionName.GET_FRAME_LOCALS)
        controls = [
            observation
            for observation in self._proof_observations
            if observation.name
            in {ActionName.STEP_PDB_SESSION.value, ActionName.NEXT_PDB_SESSION.value}
            and observation.status.value == "ok"
        ]
        if start is None or stack is None:
            return None
        frames = stack.payload.get("frames")
        current_frames = (
            [
                frame
                for frame in frames
                if type(frame) is dict and frame.get("is_current") is True
            ]
            if type(frames) is list
            else []
        )
        pause_generation = stack.payload.get("pause_generation")
        frame_id = current_frames[0].get("frame_id") if len(current_frames) == 1 else None
        result: dict[str, Any] = {
            "frame_id": frame_id,
            "pause_generation": pause_generation,
        }
        if locals_observation is None or len(controls) != 1:
            return result
        local_entries = locals_observation.payload.get("locals")
        usable_locals = (
            [
                entry
                for entry in local_entries
                if type(entry) is dict and type(entry.get("name")) is str
            ]
            if type(local_entries) is list
            else []
        )
        proof = start.payload.get("proof")
        if not usable_locals or type(proof) is not dict:
            return result
        selected_local = usable_locals[0]
        for name in self.proof_observed_local_names:
            match = next(
                (entry for entry in usable_locals if entry["name"] == name),
                None,
            )
            if match is not None:
                selected_local = match
                break
        result.update(
            {
                "evidence_refs": [
                    start.observation_id,
                    stack.observation_id,
                    locals_observation.observation_id,
                    controls[0].observation_id,
                ],
                "observed_values": {
                    selected_local["name"]: selected_local.get("value")
                },
                "target_file": proof.get("production_file"),
                "target_symbol": proof.get("production_frame"),
            }
        )
        return result

    def _effective_directive_schema(
        self, snapshot: ControllerSnapshot
    ) -> dict[str, dict[str, Any]]:
        result = _directive_schema_for_state(snapshot.state)
        if not self.proof_required:
            return result
        if snapshot.state is ControllerState.RUNTIME_EVIDENCE:
            kind = (
                "transition"
                if self._proof_diagnosis_ready() and not self._pdb_session_active
                else "action"
            )
            return {kind: result[kind]}
        if snapshot.state is not ControllerState.UNDERSTAND:
            return result

        active = snapshot.hypotheses.active_hypotheses()
        hypothesis = active[0] if active else None
        diagnosis_ready = self._proof_diagnosis_ready()
        patch_ready = self._proof_patch_allowed()
        if not diagnosis_ready:
            if hypothesis is not None and hypothesis.requires_runtime_evidence:
                kinds = {"transition"}
            elif self._unique_proof_observation(ActionName.GET_SOURCE_WINDOW) is not None:
                kinds = {"add_hypothesis"}
            else:
                kinds = {"action"}
        elif patch_ready:
            kinds = {"transition"}
        elif hypothesis is not None and hypothesis.requires_runtime_evidence:
            kinds = {"revise_hypothesis"}
        else:
            kinds = {"action"}
        effective = {kind: schema for kind, schema in result.items() if kind in kinds}
        required_runtime_flag = {
            "add_hypothesis": True,
            "revise_hypothesis": False,
        }
        for kind, required_value in required_runtime_flag.items():
            if kind not in effective:
                continue
            schema = dict(effective[kind])
            constraints = dict(schema.get("constraints", {}))
            constraints["requires_runtime_evidence"] = {
                "type": "boolean",
                "enum": [required_value],
            }
            schema["constraints"] = constraints
            effective[kind] = schema
        if "revise_hypothesis" in effective and hypothesis is not None:
            bindings = self._proof_evidence_bindings()
            if bindings is not None and "evidence_refs" in bindings:
                schema = dict(effective["revise_hypothesis"])
                constraints = dict(schema.get("constraints", {}))
                constraints["hypothesis_id"] = {
                    "type": "string",
                    "enum": [hypothesis.hypothesis_id],
                }
                constraints["evidence_refs"] = {
                    "type": "array",
                    "example": bindings["evidence_refs"],
                }
                schema["constraints"] = constraints
                effective["revise_hypothesis"] = schema
        return effective

    def _effective_contract(self, snapshot: ControllerSnapshot) -> dict[str, dict[str, Any]]:
        pdb_observations_remaining = max(
            0,
            snapshot.budget_limits.max_pdb_observations
            - snapshot.budget_state.pdb_observations,
        )
        pdb_available = (
            self.policy is not DemoPolicy.STATIC_BASELINE
            and (
                snapshot.state is ControllerState.RUNTIME_EVIDENCE
                and self._runtime_transition_authorized
                or snapshot.state is not ControllerState.RUNTIME_EVIDENCE
            )
        )
        result = _action_contracts_for_state(
            snapshot.state,
            registry=self.registry,
            policy=self.policy,
            session_active=self._pdb_session_active,
            pdb_available=pdb_available,
            pdb_observations_remaining=pdb_observations_remaining,
            post_patch_f2p_collected=self._post_patch_f2p_collected,
            regression_collected=self._regression_collected,
            patch_allowed=self._proof_patch_allowed(),
            diagnosis_allowed=self._proof_diagnosis_ready(),
            failure_trace_allowed=self._failure_reproduced,
        )
        if self.proof_required and snapshot.state is ControllerState.REPRODUCE:
            if self._failure_reproduced:
                # The exact proof consumes one unique baseline observation.
                # Re-advertising baseline and optional post-mortem actions lets
                # a small model exhaust the task's test/PDB budgets without
                # adding admissible proof.  Once reproduced, the only useful
                # next decision is the state transition handled below.
                result = {}
            else:
                result = {
                    name: contract
                    for name, contract in result.items()
                    if name == ActionName.RUN_REPRODUCTION.value
                }
        if self.proof_required and snapshot.state is ControllerState.UNDERSTAND:
            active = snapshot.hypotheses.active_hypotheses()
            hypothesis = active[0] if active else None
            if not self._proof_diagnosis_ready():
                if hypothesis is not None and hypothesis.requires_runtime_evidence:
                    result = {}
                elif self._unique_proof_observation(ActionName.GET_SOURCE_WINDOW) is not None:
                    result = {}
                else:
                    result = {
                        name: contract
                        for name, contract in result.items()
                        if name == ActionName.GET_SOURCE_WINDOW.value
                    }
                    source_name = ActionName.GET_SOURCE_WINDOW.value
                    public_paths = [
                        path
                        for path in self.task.constraints.allowed_write_paths
                        if path.endswith(".py")
                    ]
                    if source_name in result and len(public_paths) == 1:
                        contract = dict(result[source_name])
                        properties = dict(contract.get("properties", {}))
                        for field, value in (("path", public_paths[0]), ("line", self.proof_source_line)):
                            spec = dict(properties.get(field, {}))
                            spec["enum"] = [value]
                            properties[field] = spec
                        contract["properties"] = properties
                        result[source_name] = contract
            elif self._proof_patch_allowed():
                result = {}
            elif hypothesis is not None and hypothesis.requires_runtime_evidence:
                result = {}
            else:
                result = {
                    name: contract
                    for name, contract in result.items()
                    if name == ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS.value
                }
        if self.proof_required and snapshot.state is ControllerState.RUNTIME_EVIDENCE:
            # The lowest-rung proof is intentionally a narrow debugger
            # lifecycle.  Advertising unrelated execution controls caused the
            # live model to spend its bounded run on legal but proof-irrelevant
            # actions.  The event/controller path stays model-driven, while the
            # request surface exposes only the next evidence-producing choice.
            next_actions = set(self._proof_runtime_progress()["next_required_actions"])
            result = {
                name: contract
                for name, contract in result.items()
                if name in next_actions
            }
            locals_name = ActionName.GET_FRAME_LOCALS.value
            bindings = self._proof_evidence_bindings()
            if locals_name in result and bindings is not None:
                frame_id = bindings.get("frame_id")
                pause_generation = bindings.get("pause_generation")
                if type(frame_id) is int and type(pause_generation) is int:
                    contract = dict(result[locals_name])
                    properties = dict(contract.get("properties", {}))
                    for field, value in (
                        ("frame_id", frame_id),
                        ("pause_generation", pause_generation),
                    ):
                        spec = dict(properties.get(field, {}))
                        spec["enum"] = [value]
                        properties[field] = spec
                    contract["properties"] = properties
                    result[locals_name] = contract
        if (
            self.proof_required
            and snapshot.state is ControllerState.UNDERSTAND
            and ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS.value in result
        ):
            active = snapshot.hypotheses.active_hypotheses()
            bindings = self._proof_evidence_bindings()
            if active and bindings is not None and "evidence_refs" in bindings:
                name = ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS.value
                contract = dict(result[name])
                properties = dict(contract.get("properties", {}))
                exact_values = {
                    "hypothesis_id": active[0].hypothesis_id,
                    "target_file": bindings["target_file"],
                    "target_symbol": bindings["target_symbol"],
                }
                for field, value in exact_values.items():
                    spec = dict(properties.get(field, {}))
                    spec["enum"] = [value]
                    properties[field] = spec
                evidence_spec = dict(properties.get("evidence_refs", {}))
                evidence_spec["example"] = bindings["evidence_refs"]
                properties["evidence_refs"] = evidence_spec
                observed_spec = dict(properties.get("observed_values", {}))
                observed_spec["example"] = bindings["observed_values"]
                properties["observed_values"] = observed_spec
                contract["properties"] = properties
                result[name] = contract
        return result
