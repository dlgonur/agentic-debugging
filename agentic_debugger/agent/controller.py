"""The single deterministic repair controller.

This module owns :class:`DeterministicController` — the single
orchestration authority of the repair architecture — plus the runtime
step helpers it dispatches through: budget accounting
(``_remaining``/``_consume_direct``), failure-state classification,
directive canonicalization, dispatch identity checks, and canonical
observation construction.

Architecture (one-way dependency graph):

* :mod:`agentic_debugger.agent.controller_contracts` — error vocabulary,
  stop reasons, primitive validators, deep canonical-copy authority,
  ``ControllerRunConfig``, and record-shape validators;
* :mod:`agentic_debugger.agent.controller_results` — the immutable
  step/run result records and action/observation identity authority;
* this module — the deterministic run/transition implementation and the
  public import surface.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Callable, Optional

from agentic_debugger.agent.controller_contracts import (
    DEFAULT_MAX_MODEL_CALLS,
    ControllerError,
    ControllerInputError,
    ControllerInvariantError,
    ControllerRunConfig,
    ControllerStopReason,
    MAX_CONTROLLER_MODEL_CALLS,
    MAX_CONTROLLER_MODEL_CALL_INDEX,
    _canonicalize_initial_snapshot,
    _canonicalize_registry,
    _copy_budget_state,
    _copy_json,
    _copy_ledger,
    _copy_limits,
    _copy_observation,
    _invariant,
    _input,
    _model_error_text,
    _resolve_model_method,
    _resolve_observer_notify,
    _validate_ledger,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    BudgetKind,
    ControllerBudgetLimits,
    ControllerBudgetState,
    ControllerPolicyError,
    HypothesisLedger,
    budget_kind_for_action,
    is_action_allowed,
)
from agentic_debugger.agent.controller_results import (
    ControllerRunResult,
    ControllerStepResult,
    _action_id,
    _observation_id,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    AddHypothesisDirective,
    ControllerSnapshot,
    ModelAdapterError,
    ModelDirectiveKind,
    ModelScriptExhaustedError,
    ModelScriptMismatchError,
    ReviseHypothesisDirective,
    SetHypothesisStatusDirective,
    TransitionDirective,
    UsageReportingModelAdapter,
    directive_kind,
)
from agentic_debugger.agent.token_usage import TokenUsage, TokenUsageCoverage
from agentic_debugger.agent.observer import (
    ControllerObservation,
    ControllerObservationKind,
    ControllerObserver,
    NoopControllerObserver,
)
from agentic_debugger.agent.state_machine import ControllerState, is_transition_allowed
from agentic_debugger.agent.tool_registry import (
    ToolDispatchReason,
    ToolRegistry,
    ToolRegistryError,
    ToolRejectedError,
    ToolSpec,
)
from agentic_debugger.cancellation import CancellationError
from agentic_debugger.events.schema import Action, Observation, ObservationStatus

_BUDGET_FIELDS = {
    BudgetKind.PATCH_ATTEMPTS: ("max_patch_attempts", "patch_attempts"),
    BudgetKind.TEST_RUNS: ("max_test_runs", "test_runs"),
    BudgetKind.PDB_OBSERVATIONS: ("max_pdb_observations", "pdb_observations"),
    BudgetKind.SOURCE_OBSERVATIONS: ("max_source_observations", "source_observations"),
}
_NO_HANDLER_REASONS = frozenset({
    ToolDispatchReason.UNKNOWN_ACTION,
    ToolDispatchReason.STATE_ACTION_NOT_ALLOWED,
    ToolDispatchReason.TOOL_NOT_REGISTERED,
    ToolDispatchReason.INVALID_ARGUMENTS,
})


def _remaining(limits: ControllerBudgetLimits, state: ControllerBudgetState,
               kind: BudgetKind) -> int:
    limit_field, state_field = _BUDGET_FIELDS[kind]
    return getattr(limits, limit_field) - getattr(state, state_field)


def _consume_direct(limits: ControllerBudgetLimits, state: ControllerBudgetState,
                    kind: BudgetKind) -> ControllerBudgetState:
    values = {
        "patch_attempts": state.patch_attempts,
        "test_runs": state.test_runs,
        "pdb_observations": state.pdb_observations,
        "source_observations": state.source_observations,
    }
    _, field_name = _BUDGET_FIELDS[kind]
    values[field_name] += 1
    try:
        return ControllerBudgetState(**values)
    except Exception:
        _invariant("budget_state")


def _failure_state(state: ControllerState) -> ControllerState:
    if type(state) is not ControllerState or state in (ControllerState.DONE, ControllerState.FAILED):
        _invariant("failure_state")
    try:
        allowed = is_transition_allowed(state, ControllerState.FAILED)
    except Exception:
        _invariant("failure_transition")
    if type(allowed) is not bool or not allowed:
        _invariant("failure_transition")
    return ControllerState.FAILED


def _canonical_directive(directive: object) -> tuple[ModelDirectiveKind, object]:
    try:
        kind = directive_kind(directive)
        if type(kind) is not ModelDirectiveKind:
            _input("directive")
        if type(directive) is ActionDirective:
            return kind, ActionDirective(directive.name, directive.arguments)
        if type(directive) is TransitionDirective:
            return kind, TransitionDirective(directive.target_state, directive.reason)
        if type(directive) is AddHypothesisDirective:
            return kind, AddHypothesisDirective(
                directive.hypothesis_id, directive.statement, directive.confidence,
                directive.evidence_refs, directive.requires_runtime_evidence,
            )
        if type(directive) is ReviseHypothesisDirective:
            return kind, ReviseHypothesisDirective(
                directive.hypothesis_id, directive.statement, directive.confidence,
                directive.evidence_refs, directive.requires_runtime_evidence,
            )
        if type(directive) is SetHypothesisStatusDirective:
            return kind, SetHypothesisStatusDirective(directive.hypothesis_id, directive.status)
    except Exception:
        _input("directive")
    _input("directive")


def _assert_dispatch_identity(record_action: Action, dispatch_action: Action) -> None:
    if type(dispatch_action) is not Action:
        _invariant("action")
    for field_name in ("action_id", "run_id", "task_id", "state", "name"):
        expected = getattr(record_action, field_name)
        actual = getattr(dispatch_action, field_name)
        if type(actual) is not type(expected):
            _invariant("action")
        if actual != expected:
            _invariant("action")


def _canonical_observation(value: object, action: Action,
                           expected_observation_id: str) -> tuple[Observation, ToolDispatchReason]:
    if type(value) is not Observation:
        _invariant("observation")
    try:
        observation_id = value.observation_id
        action_id = value.action_id
        run_id = value.run_id
        task_id = value.task_id
        name = value.name
        status = value.status
        payload = value.payload
        summary = value.summary
        truncated = value.truncated
    except Exception:
        _invariant("observation")
    if (
        type(observation_id) is not str
        or type(action_id) is not str
        or type(run_id) is not str
        or type(task_id) is not str
        or type(name) is not str
        or type(status) is not ObservationStatus
        or type(payload) is not dict
        or type(summary) is not str
        or type(truncated) is not bool
    ):
        _invariant("observation")
    copied_payload = _copy_json(payload)
    if type(copied_payload) is not dict:
        _invariant("observation")
    if "dispatch_reason" not in copied_payload or type(copied_payload["dispatch_reason"]) is not str:
        _invariant("dispatch_reason")
    reason_value = copied_payload["dispatch_reason"]
    reason = next((candidate for candidate in ToolDispatchReason
                   if candidate.value == reason_value), None)
    if reason is None:
        _invariant("dispatch_reason")
    if (
        observation_id != expected_observation_id
        or action_id != action.action_id
        or run_id != action.run_id
        or task_id != action.task_id
        or name != action.name
    ):
        _invariant("observation")
    try:
        observation = Observation(
            observation_id=observation_id,
            action_id=action_id,
            run_id=run_id,
            task_id=task_id,
            name=name,
            status=status,
            payload=copied_payload,
            summary=summary,
            truncated=truncated,
        )
    except Exception:
        _invariant("observation")
    return observation, reason


@dataclass(frozen=True)
class DeterministicController:
    registry: ToolRegistry
    model_adapter: object
    config: ControllerRunConfig = field(default_factory=ControllerRunConfig)
    observer: ControllerObserver = field(default_factory=NoopControllerObserver, compare=False)
    _canonical_registry: ToolRegistry = field(init=False, repr=False, compare=False)
    _model_method: Callable[..., object] = field(init=False, repr=False, compare=False)
    _canonical_max_model_calls: int | None = field(init=False, repr=False, compare=False)
    _canonical_require_pdb_evidence_before_patch: bool = field(init=False, repr=False, compare=False)
    _canonical_observer: ControllerObserver = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        canonical_registry = _canonicalize_registry(self.registry)
        if type(self.config) is not ControllerRunConfig:
            _input("config")
        max_model_calls = self.config.max_model_calls
        # Task 44: None means unbounded (no total-session progression
        # ceiling); an explicit finite value is honored only for explicit
        # callers (tests/frozen treatments).
        if max_model_calls is None:
            pass
        elif type(max_model_calls) is not int or isinstance(max_model_calls, bool) or not 1 <= max_model_calls <= MAX_CONTROLLER_MODEL_CALLS:
            _input("config")
        canonical_config = ControllerRunConfig(
            max_model_calls,
            self.config.require_pdb_evidence_before_patch,
        )
        method = _resolve_model_method(self.model_adapter)
        _resolve_observer_notify(self.observer)
        object.__setattr__(self, "config", canonical_config)
        object.__setattr__(self, "_canonical_registry", canonical_registry)
        object.__setattr__(self, "_model_method", method)
        object.__setattr__(self, "_canonical_max_model_calls", max_model_calls)
        object.__setattr__(
            self,
            "_canonical_require_pdb_evidence_before_patch",
            self.config.require_pdb_evidence_before_patch,
        )
        object.__setattr__(self, "_canonical_observer", self.observer)

    def _validate_controller(self) -> None:
        if type(self.registry) is not ToolRegistry:
            _input("registry")
        if type(self._canonical_registry) is not ToolRegistry:
            _invariant("registry")
        _max_calls = self._canonical_max_model_calls
        if _max_calls is not None and (
            type(_max_calls) is not int
            or isinstance(_max_calls, bool)
            or not 1 <= _max_calls <= MAX_CONTROLLER_MODEL_CALLS
        ):
            _invariant("max_model_calls")
        if not inspect.isfunction(self._model_method) or not callable(self._model_method):
            _invariant("model_method")
        if not callable(getattr(self._canonical_observer, "notify", None)):
            _invariant("observer")

    def run(
        self,
        initial_snapshot: ControllerSnapshot,
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> ControllerRunResult:
        """Execute one bounded controller run.

        ``cancel_check`` is an optional cooperative cancellation checkpoint:
        when supplied it is invoked only at coherent safe boundaries (loop
        top, around the model request, before tool dispatch) and must raise
        :class:`CancellationError` to request cancellation.  Cancellation
        propagates explicitly and is never converted into a scientific stop
        reason or a ``ControllerRunResult``; a cancelled run produces no
        result, no terminal observation, and no canonical trajectory.  When
        ``cancel_check`` is ``None`` behavior is unchanged.
        """
        self._validate_controller()
        if cancel_check is not None and not callable(cancel_check):
            _input("cancel_check")
        snapshot = _canonicalize_initial_snapshot(initial_snapshot)
        max_calls = self._canonical_max_model_calls
        # Task 44: None means unbounded — no total-session progression
        # ceiling.  The overflow guard below applies only when a finite
        # bound was explicitly requested.
        if max_calls is not None and snapshot.model_call_index + max_calls > MAX_CONTROLLER_MODEL_CALL_INDEX:
            _input("model_call_index")

        run_id = snapshot.run_id
        task_id = snapshot.task_id
        initial_state = snapshot.state
        state = snapshot.state
        model_call_index = snapshot.model_call_index
        budget_state = snapshot.budget_state
        hypotheses = snapshot.hypotheses
        last_observation = snapshot.last_observation
        tool_observations: list[Observation] = []
        if last_observation is not None:
            tool_observations.append(last_observation)
        steps: list[ControllerStepResult] = []
        model_calls = 0
        last_request_index = model_call_index

        def _emit(kind: ControllerObservationKind, **details: object) -> None:
            """Fire one detached observation; observer failure never alters
            a controller decision, step, budget, stop reason, or result.

            Ordinary ``Exception`` failures (including observation
            construction) are bounded and swallowed; ``BaseException``
            (``KeyboardInterrupt``/``SystemExit``) propagates."""
            try:
                observation = ControllerObservation(
                    kind=kind, run_id=run_id, task_id=task_id, **details
                )
                self._canonical_observer.notify(observation)
            except Exception:
                pass

        def _check_cancelled() -> None:
            """Honor cooperative cancellation at a safe boundary.

            The check is a bare call: ``CancellationError`` (or any other
            exception the caller's check raises) propagates and aborts the
            run without fabricating a scientific outcome.
            """
            if cancel_check is not None:
                cancel_check()

        def _completed_request_telemetry() -> tuple[Optional[TokenUsage], Optional[TokenUsageCoverage]]:
            """Read provider-reported usage and coverage of the just-finished request.

            Only adapters implementing the optional usage seam can report
            it (live provider adapters aggregating transport retries and
            directive repairs); scripted adapters legitimately report
            none.  A seam failure or invalid value never fails the
            controller request itself.
            """
            adapter = self.model_adapter
            if not isinstance(adapter, UsageReportingModelAdapter):
                return None, None
            try:
                usage = adapter.last_request_token_usage()
            except Exception:
                return None, None
            if not isinstance(usage, TokenUsage):
                return None, None
            coverage = None
            cov_getter = getattr(adapter, "last_request_token_coverage", None)
            if callable(cov_getter):
                try:
                    cov = cov_getter()
                    if isinstance(cov, TokenUsageCoverage):
                        coverage = cov
                except Exception:
                    coverage = None
            return usage, coverage

        _emit(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=snapshot.model_call_index,
            state_before=initial_state,
        )

        if state is ControllerState.DONE:
            _emit(
                ControllerObservationKind.TERMINAL,
                model_call_index=snapshot.model_call_index,
                state_after=ControllerState.DONE,
                stop_reason=ControllerStopReason.DONE.value,
            )
            return ControllerRunResult(
                run_id, task_id, initial_state, ControllerState.DONE,
                ControllerStopReason.DONE, 0, (), budget_state, hypotheses,
                last_observation,
            )
        if state is ControllerState.FAILED:
            _emit(
                ControllerObservationKind.TERMINAL,
                model_call_index=snapshot.model_call_index,
                state_after=ControllerState.FAILED,
                stop_reason=ControllerStopReason.FAILED.value,
            )
            return ControllerRunResult(
                run_id, task_id, initial_state, ControllerState.FAILED,
                ControllerStopReason.FAILED, 0, (), budget_state, hypotheses,
                last_observation,
            )

        def result(stop_reason: ControllerStopReason, final_state: ControllerState) -> ControllerRunResult:
            _emit(
                ControllerObservationKind.TERMINAL,
                model_call_index=last_request_index,
                state_after=final_state,
                stop_reason=stop_reason.value,
            )
            return ControllerRunResult(
                run_id, task_id, initial_state, final_state, stop_reason,
                model_calls, tuple(steps), budget_state, hypotheses,
                last_observation,
            )

        def failure_step(reason: ControllerStopReason, *, kind: ModelDirectiveKind | None = None,
                         action: Action | None = None, observation: Observation | None = None,
                         transition_reason: str | None = None,
                         state_before: ControllerState | None = None,
                         budget_before: ControllerBudgetState | None = None,
                         hypotheses_before: HypothesisLedger | None = None,
                         step_model_call_index: int | None = None) -> None:
            nonlocal state
            before_state = state if state_before is None else state_before
            before_budget = budget_state if budget_before is None else budget_before
            before_hypotheses = hypotheses if hypotheses_before is None else hypotheses_before
            step_model_call_index = (model_call_index if step_model_call_index is None and kind is None
                                     else (model_call_index - 1 if step_model_call_index is None
                                           else step_model_call_index))
            state = _failure_state(before_state)
            _emit(
                ControllerObservationKind.STATE_TRANSITION,
                model_call_index=step_model_call_index,
                state_before=before_state,
                state_after=state,
            )
            steps.append(ControllerStepResult(
                model_call_index=step_model_call_index,
                state_before=before_state,
                state_after=state,
                directive_kind=kind,
                action=action,
                observation=observation,
                transition_reason=transition_reason,
                budget_before=before_budget,
                budget_after=budget_state,
                hypotheses_before=before_hypotheses,
                hypotheses_after=hypotheses,
                stop_reason=reason,
            ))
            _emit(
                ControllerObservationKind.STEP_COMPLETED,
                model_call_index=step_model_call_index,
                step_index=len(steps) - 1,
                state_before=before_state,
                state_after=state,
                directive_kind=kind.value if kind is not None else None,
                stop_reason=reason.value,
            )

        while True:
            _check_cancelled()
            # Task 44: a finite ``max_calls`` is honored only when an
            # explicit caller requested it (tests/frozen treatments).
            # ``None`` means unbounded interactive execution: the loop
            # continues for as long as the model/controller protocol
            # permits, with ``model_calls``/``steps`` as telemetry only.
            if max_calls is not None and model_calls >= max_calls:
                before_state = state
                state = _failure_state(state)
                _emit(
                    ControllerObservationKind.STATE_TRANSITION,
                    model_call_index=last_request_index,
                    state_before=before_state,
                    state_after=state,
                )
                return result(ControllerStopReason.MODEL_CALL_LIMIT, state)

            state_before = state
            budget_before = budget_state
            hypotheses_before = hypotheses
            try:
                model_limits = _copy_limits(snapshot.budget_limits)
                model_budget_state = _copy_budget_state(budget_state)
                model_hypotheses = _copy_ledger(hypotheses, model_limits)
                model_observation = None if last_observation is None else _copy_observation(
                    last_observation, run_id=run_id, task_id=task_id
                )
                call_snapshot = ControllerSnapshot(
                    run_id=run_id,
                    task_id=task_id,
                    state=state,
                    model_call_index=model_call_index,
                    budget_limits=model_limits,
                    budget_state=model_budget_state,
                    hypotheses=model_hypotheses,
                    last_observation=model_observation,
                )
            except Exception:
                _invariant("snapshot")
            model_calls += 1
            last_request_index = model_call_index
            _check_cancelled()
            _emit(
                ControllerObservationKind.MODEL_REQUEST_STARTED,
                model_call_index=model_call_index,
                state_before=state,
            )
            try:
                directive = self._model_method(self.model_adapter, call_snapshot)
            except CancellationError:
                raise
            except ModelScriptExhaustedError:
                req_usage, req_coverage = _completed_request_telemetry()
                _emit(ControllerObservationKind.MODEL_REQUEST_COMPLETED,
                      model_call_index=model_call_index, state_before=state,
                      request_status="error", error_kind="model_script_exhausted",
                      error_message="scripted model response is unavailable",
                      token_usage=req_usage,
                      token_usage_coverage=req_coverage)
                failure_step(ControllerStopReason.MODEL_SCRIPT_EXHAUSTED)
                return result(ControllerStopReason.MODEL_SCRIPT_EXHAUSTED, state)
            except ModelScriptMismatchError:
                req_usage, req_coverage = _completed_request_telemetry()
                _emit(ControllerObservationKind.MODEL_REQUEST_COMPLETED,
                      model_call_index=model_call_index, state_before=state,
                      request_status="error", error_kind="model_script_mismatch",
                      error_message="scripted model response does not match controller state",
                      token_usage=req_usage,
                      token_usage_coverage=req_coverage)
                failure_step(ControllerStopReason.MODEL_SCRIPT_MISMATCH)
                return result(ControllerStopReason.MODEL_SCRIPT_MISMATCH, state)
            except ModelAdapterError as exc:
                req_usage, req_coverage = _completed_request_telemetry()
                _emit(ControllerObservationKind.MODEL_REQUEST_COMPLETED,
                      model_call_index=model_call_index, state_before=state,
                      request_status="error",
                      error_kind=_model_error_text(
                          getattr(exc, "error_kind", None), "model_adapter_error", 64
                      ),
                      error_message=_model_error_text(
                          getattr(exc, "safe_message", None),
                          "model adapter rejected the request",
                          512,
                      ),
                      token_usage=req_usage,
                      token_usage_coverage=req_coverage)
                failure_step(ControllerStopReason.MODEL_ERROR)
                return result(ControllerStopReason.MODEL_ERROR, state)
            except Exception:
                req_usage, req_coverage = _completed_request_telemetry()
                _emit(ControllerObservationKind.MODEL_REQUEST_COMPLETED,
                      model_call_index=model_call_index, state_before=state,
                      request_status="error", error_kind="unexpected_model_error",
                      error_message="unexpected model request failure",
                      token_usage=req_usage,
                      token_usage_coverage=req_coverage)
                failure_step(ControllerStopReason.MODEL_ERROR)
                return result(ControllerStopReason.MODEL_ERROR, state)

            try:
                kind, directive = _canonical_directive(directive)
            except Exception:
                req_usage, req_coverage = _completed_request_telemetry()
                _emit(ControllerObservationKind.MODEL_REQUEST_COMPLETED,
                      model_call_index=model_call_index, state_before=state,
                      request_status="error", error_kind="invalid_directive",
                      error_message="model response did not produce a canonical directive",
                      token_usage=req_usage,
                      token_usage_coverage=req_coverage)
                failure_step(ControllerStopReason.MODEL_ERROR)
                return result(ControllerStopReason.MODEL_ERROR, state)

            _check_cancelled()
            model_call_index += 1
            req_usage, req_coverage = _completed_request_telemetry()
            _emit(
                ControllerObservationKind.MODEL_REQUEST_COMPLETED,
                model_call_index=model_call_index - 1,
                state_before=state_before,
                request_status="ok",
                token_usage=req_usage,
                token_usage_coverage=req_coverage,
            )
            if kind is ModelDirectiveKind.ACTION:
                _check_cancelled()
                action_directive = directive
                record_action: Action | None = None
                dispatch_action: Action | None = None
                tool_started = False
                try:
                    if not is_action_allowed(state, action_directive.name):
                        _emit(ControllerObservationKind.DIRECTIVE_REJECTED,
                              model_call_index=model_call_index - 1, state_before=state_before,
                              directive_kind=kind.value,
                              rejection_category="state_action_not_allowed")
                        failure_step(ControllerStopReason.DIRECTIVE_REJECTED, kind=kind,
                                     state_before=state_before, budget_before=budget_before,
                                     hypotheses_before=hypotheses_before)
                        return result(ControllerStopReason.DIRECTIVE_REJECTED, state)
                    budget_kind = budget_kind_for_action(action_directive.name)
                    if budget_kind is not None and _remaining(snapshot.budget_limits, budget_state, budget_kind) <= 0:
                        _emit(ControllerObservationKind.DIRECTIVE_REJECTED,
                              model_call_index=model_call_index - 1, state_before=state_before,
                              directive_kind=kind.value,
                              rejection_category="budget_exhausted")
                        failure_step(ControllerStopReason.BUDGET_EXHAUSTED, kind=kind,
                                     state_before=state_before, budget_before=budget_before,
                                     hypotheses_before=hypotheses_before)
                        return result(ControllerStopReason.BUDGET_EXHAUSTED, state)
                    action_id = _action_id(model_call_index - 1)
                    observation_id = _observation_id(model_call_index - 1)
                    record_action = Action(
                        action_id=action_id,
                        run_id=run_id,
                        task_id=task_id,
                        state=state_before,
                        name=action_directive.name.value,
                        arguments=_copy_json(action_directive.arguments),
                    )
                    dispatch_action = Action(
                        action_id=action_id,
                        run_id=run_id,
                        task_id=task_id,
                        state=state_before,
                        name=action_directive.name.value,
                        arguments=_copy_json(action_directive.arguments),
                    )
                    if type(record_action.arguments) is not dict or type(dispatch_action.arguments) is not dict:
                        _invariant("action")
                    _emit(ControllerObservationKind.DIRECTIVE_ACCEPTED,
                          model_call_index=model_call_index - 1, state_before=state_before,
                          directive_kind=kind.value, tool_name=action_directive.name.value)
                    _emit(ControllerObservationKind.TOOL_STARTED,
                          model_call_index=model_call_index - 1, state_before=state_before,
                          tool_name=action_directive.name.value)
                    tool_started = True
                    observation_raw = ToolRegistry.dispatch(
                        self._canonical_registry,
                        dispatch_action,
                        observation_id=observation_id,
                    )
                    _assert_dispatch_identity(record_action, dispatch_action)
                    observation, reason = _canonical_observation(
                        observation_raw, record_action, observation_id
                    )
                    tool_observations.append(observation)
                    _emit(ControllerObservationKind.TOOL_COMPLETED,
                          model_call_index=model_call_index - 1, state_before=state_before,
                          tool_name=action_directive.name.value,
                          observation_status=observation.status)
                except CancellationError:
                    raise
                except Exception:
                    if tool_started:
                        _emit(ControllerObservationKind.TOOL_COMPLETED,
                              model_call_index=model_call_index - 1, state_before=state_before,
                              tool_name=action_directive.name.value,
                              observation_status=ObservationStatus.ERROR)
                    failure_step(ControllerStopReason.CONTROLLER_ERROR, kind=kind,
                                 action=record_action,
                                 state_before=state_before, budget_before=budget_before,
                                 hypotheses_before=hypotheses_before)
                    return result(ControllerStopReason.CONTROLLER_ERROR, state)
                if budget_kind is not None and reason not in _NO_HANDLER_REASONS:
                    budget_state = _consume_direct(snapshot.budget_limits, budget_state, budget_kind)
                last_observation = observation

                is_fatal_tool_failure = False
                if observation.status in (
                    ObservationStatus.ERROR,
                    ObservationStatus.REJECTED,
                ) and observation.payload and (
                    observation.payload.get("recoverable") is False
                    or (
                        isinstance(observation.payload.get("patch_failure"), dict)
                        and observation.payload["patch_failure"].get("recoverable") is False
                    )
                ):
                    is_fatal_tool_failure = True

                stop = ControllerStopReason.FAILED if is_fatal_tool_failure else None
                state_after_step = ControllerState.FAILED if is_fatal_tool_failure else state

                steps.append(ControllerStepResult(
                    model_call_index=model_call_index - 1,
                    state_before=state_before,
                    state_after=state_after_step,
                    directive_kind=kind,
                    action=record_action,
                    observation=observation,
                    transition_reason=None,
                    budget_before=budget_before,
                    budget_after=budget_state,
                    hypotheses_before=hypotheses_before,
                    hypotheses_after=hypotheses,
                    stop_reason=stop,
                ))
                _emit(ControllerObservationKind.STEP_COMPLETED,
                      model_call_index=model_call_index - 1,
                      step_index=len(steps) - 1,
                      state_before=state_before, state_after=state_after_step,
                      directive_kind=kind.value)

                if is_fatal_tool_failure:
                    state = ControllerState.FAILED
                    _emit(
                        ControllerObservationKind.STATE_TRANSITION,
                        model_call_index=model_call_index - 1,
                        state_before=state_before,
                        state_after=state,
                        transition_reason="fatal non-recoverable tool execution failure",
                    )
                    return result(ControllerStopReason.FAILED, state)
            elif kind is ModelDirectiveKind.TRANSITION:
                transition = directive
                if not is_transition_allowed(state, transition.target_state):
                    _emit(ControllerObservationKind.DIRECTIVE_REJECTED,
                          model_call_index=model_call_index - 1, state_before=state_before,
                          directive_kind=kind.value,
                          rejection_category="transition_not_allowed",
                          transition_reason=transition.reason)
                    failure_step(ControllerStopReason.DIRECTIVE_REJECTED, kind=kind,
                                 transition_reason=transition.reason,
                                 state_before=state_before, budget_before=budget_before,
                                 hypotheses_before=hypotheses_before)
                    return result(ControllerStopReason.DIRECTIVE_REJECTED, state)
                if (
                    transition.target_state is ControllerState.PATCH
                    and self._canonical_require_pdb_evidence_before_patch
                ):
                    from agentic_debugger.agent.proof_gate import validate_pdb_patch_evidence

                    allowed, reason = validate_pdb_patch_evidence(tool_observations)
                    if not allowed:
                        _emit(
                            ControllerObservationKind.DIRECTIVE_REJECTED,
                            model_call_index=model_call_index - 1,
                            state_before=state_before,
                            directive_kind=kind.value,
                            rejection_category="pdb_evidence_required",
                            transition_reason=reason,
                        )
                        failure_step(
                            ControllerStopReason.DIRECTIVE_REJECTED,
                            kind=kind,
                            transition_reason=reason,
                            state_before=state_before,
                            budget_before=budget_before,
                            hypotheses_before=hypotheses_before,
                        )
                        return result(ControllerStopReason.DIRECTIVE_REJECTED, state)
                _emit(ControllerObservationKind.DIRECTIVE_ACCEPTED,
                      model_call_index=model_call_index - 1, state_before=state_before,
                      directive_kind=kind.value, target_state=transition.target_state,
                      transition_reason=transition.reason)
                state = transition.target_state
                stop = None
                if state is ControllerState.DONE:
                    stop = ControllerStopReason.DONE
                elif state is ControllerState.FAILED:
                    stop = ControllerStopReason.FAILED
                _emit(ControllerObservationKind.STATE_TRANSITION,
                      model_call_index=model_call_index - 1,
                      state_before=state_before, state_after=state,
                      transition_reason=transition.reason)
                steps.append(ControllerStepResult(
                    model_call_index=model_call_index - 1,
                    state_before=state_before,
                    state_after=state,
                    directive_kind=kind,
                    action=None,
                    observation=None,
                    transition_reason=transition.reason,
                    budget_before=budget_before,
                    budget_after=budget_state,
                    hypotheses_before=hypotheses_before,
                    hypotheses_after=hypotheses,
                    stop_reason=stop,
                ))
                _emit(ControllerObservationKind.STEP_COMPLETED,
                      model_call_index=model_call_index - 1,
                      step_index=len(steps) - 1,
                      state_before=state_before, state_after=state,
                      directive_kind=kind.value,
                      transition_reason=transition.reason,
                      stop_reason=stop.value if stop is not None else None)
                if stop is not None:
                    return result(stop, state)
            else:
                allowed_states = {
                    ModelDirectiveKind.ADD_HYPOTHESIS: (ControllerState.UNDERSTAND,),
                    ModelDirectiveKind.REVISE_HYPOTHESIS: (ControllerState.UNDERSTAND, ControllerState.RUNTIME_EVIDENCE),
                    ModelDirectiveKind.SET_HYPOTHESIS_STATUS: (ControllerState.UNDERSTAND, ControllerState.RUNTIME_EVIDENCE),
                }
                if state not in allowed_states[kind]:
                    _emit(ControllerObservationKind.DIRECTIVE_REJECTED,
                          model_call_index=model_call_index - 1, state_before=state_before,
                          directive_kind=kind.value,
                          rejection_category="state_not_allowed")
                    failure_step(ControllerStopReason.DIRECTIVE_REJECTED, kind=kind,
                                 state_before=state_before, budget_before=budget_before,
                                 hypotheses_before=hypotheses_before)
                    return result(ControllerStopReason.DIRECTIVE_REJECTED, state)
                try:
                    if kind is ModelDirectiveKind.ADD_HYPOTHESIS:
                        add_directive = directive
                        hypotheses = HypothesisLedger.add(
                            hypotheses, snapshot.budget_limits,
                            hypothesis_id=add_directive.hypothesis_id,
                            statement=add_directive.statement,
                            confidence=add_directive.confidence,
                            evidence_refs=add_directive.evidence_refs,
                            requires_runtime_evidence=add_directive.requires_runtime_evidence,
                        )
                    elif kind is ModelDirectiveKind.REVISE_HYPOTHESIS:
                        revise_directive = directive
                        hypotheses = HypothesisLedger.revise(
                            hypotheses, revise_directive.hypothesis_id,
                            statement=revise_directive.statement,
                            confidence=revise_directive.confidence,
                            evidence_refs=revise_directive.evidence_refs,
                            requires_runtime_evidence=revise_directive.requires_runtime_evidence,
                        )
                    else:
                        status_directive = directive
                        hypotheses = HypothesisLedger.transition(
                            hypotheses, status_directive.hypothesis_id,
                            status_directive.status,
                        )
                except ControllerPolicyError:
                    _emit(ControllerObservationKind.DIRECTIVE_REJECTED,
                          model_call_index=model_call_index - 1, state_before=state_before,
                          directive_kind=kind.value,
                          rejection_category="policy_rejected")
                    failure_step(ControllerStopReason.DIRECTIVE_REJECTED, kind=kind,
                                 state_before=state_before, budget_before=budget_before,
                                 hypotheses_before=hypotheses_before)
                    return result(ControllerStopReason.DIRECTIVE_REJECTED, state)
                if type(hypotheses) is not HypothesisLedger:
                    _invariant("hypotheses")
                _emit(ControllerObservationKind.DIRECTIVE_ACCEPTED,
                      model_call_index=model_call_index - 1, state_before=state_before,
                      directive_kind=kind.value)
                steps.append(ControllerStepResult(
                    model_call_index=model_call_index - 1,
                    state_before=state_before,
                    state_after=state,
                    directive_kind=kind,
                    action=None,
                    observation=None,
                    transition_reason=None,
                    budget_before=budget_before,
                    budget_after=budget_state,
                    hypotheses_before=hypotheses_before,
                    hypotheses_after=hypotheses,
                ))
                _emit(ControllerObservationKind.STEP_COMPLETED,
                      model_call_index=model_call_index - 1,
                      step_index=len(steps) - 1,
                      state_before=state_before, state_after=state,
                      directive_kind=kind.value)


__all__ = [
    "DEFAULT_MAX_MODEL_CALLS",
    "MAX_CONTROLLER_MODEL_CALLS",
    "MAX_CONTROLLER_MODEL_CALL_INDEX",
    "ControllerError",
    "ControllerInputError",
    "ControllerInvariantError",
    "ControllerStopReason",
    "ControllerRunConfig",
    "ControllerStepResult",
    "ControllerRunResult",
    "DeterministicController",
]
