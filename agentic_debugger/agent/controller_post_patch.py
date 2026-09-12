"""Deterministic post-patch validation pipeline (implementation helper).

This module owns the mechanical Successful Session Token Efficiency v1
continuation that runs after a successfully applied candidate patch::

    SYNTAX_CHECK -> PATCH -> VALIDATE -> RUN_REPRODUCTION (post_patch)
        -> RUN_REGRESSION_TESTS -> CLASSIFY_OUTCOME
        -> VALIDATE -> DONE (only for RESOLVED)

without new model requests on the happy path.

Single-controller authority is preserved.  :class:`DeterministicController`
in :mod:`agentic_debugger.agent.controller` remains the sole owner of
controller state, the model request loop, model request ordinals,
budgets, hypotheses, stop reasons, controller outcomes, and
policy/state-machine authority.  This module is an implementation helper
only:

* it operates on an explicit :class:`PostPatchRuntime` holder and a
  bounded :class:`PostPatchCallbacks` set supplied by the controller;
* it reports a :class:`PostPatchOutcome` that the controller translates
  into a terminal result (or a return to the model loop);
* it owns no model loop, state-machine policy, action-allowance policy,
  verifier authority, persistent controller state, or model-call
  accounting semantics.

Policy queries reuse :mod:`agentic_debugger.agent.controller_policy` and
:mod:`agentic_debugger.agent.state_machine`; budget accounting,
failure-state classification, dispatch identity, and observation
canonicalization are the shared single-definition helpers in
:mod:`agentic_debugger.agent.controller_contracts`.  Deterministic
identities are allocated by the controller-owned allocator callback, so
the run-scoped deterministic sequence counter never leaves the
controller run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from agentic_debugger.agent.controller_contracts import (
    ControllerStopReason,
    _NO_HANDLER_REASONS,
    _assert_dispatch_identity,
    _canonical_observation,
    _consume_direct,
    _copy_json,
    _failure_state,
    _remaining,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
    budget_kind_for_action,
    is_action_allowed,
)
from agentic_debugger.agent.controller_results import ControllerStepResult
from agentic_debugger.agent.observer import ControllerObservationKind
from agentic_debugger.agent.state_machine import ControllerState, is_transition_allowed
from agentic_debugger.agent.tool_registry import ToolRegistry
from agentic_debugger.cancellation import CancellationError
from agentic_debugger.events.schema import Action, Observation, ObservationStatus


@dataclass
class PostPatchRuntime:
    """Explicit working state for one deterministic post-patch pipeline.

    The controller builds this holder from its run locals at the
    APPLY_PATCH trigger and syncs the scalar fields back after the
    pipeline returns, before recording any terminal result.  ``steps``
    and ``tool_observations`` are the live run lists, so appends made by
    the pipeline are visible to the controller directly.
    """

    state: ControllerState
    budget_state: ControllerBudgetState
    hypotheses: HypothesisLedger
    last_observation: Observation | None
    steps: list[ControllerStepResult]
    tool_observations: list[Observation]


@dataclass(frozen=True)
class PostPatchContext:
    """Immutable pipeline identity: run/task correlation and limits/registry."""

    run_id: str
    task_id: str
    budget_limits: ControllerBudgetLimits
    registry: ToolRegistry


@dataclass(frozen=True)
class PostPatchCallbacks:
    """Bounded controller-owned seams the pipeline may use.

    ``emit`` records a detached observation (observer failure never
    alters a controller decision); ``check_cancelled`` honors
    cooperative cancellation at safe boundaries; ``allocate_det_ids``
    returns the next ``(det-action-*, det-observation-*)`` pair from the
    controller-owned run-scoped deterministic sequence (deterministic
    work never consumes a model request ordinal).
    """

    emit: Callable[..., None]
    check_cancelled: Callable[[], None]
    allocate_det_ids: Callable[[], tuple[str, str]]


@dataclass(frozen=True)
class PostPatchOutcome:
    """Pipeline result.

    ``terminal`` is None to return control to the model loop with
    preserved evidence; otherwise the controller records its terminal
    result for that stop reason from the synced runtime.
    """

    terminal: ControllerStopReason | None


def _det_is_fatal(observation: Observation) -> bool:
    try:
        payload = observation.payload
        status = observation.status
    except Exception:
        return False
    if status not in (
        ObservationStatus.ERROR,
        ObservationStatus.REJECTED,
    ):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("recoverable") is False:
        return True
    inner = payload.get("patch_failure")
    if isinstance(inner, dict) and inner.get("recoverable") is False:
        return True
    return False


def _det_record_controller_error(
    runtime: PostPatchRuntime,
    callbacks: PostPatchCallbacks,
    det_index: int,
    det_state_before: ControllerState,
    det_budget_before: ControllerBudgetState,
    det_hypotheses_before: HypothesisLedger,
    record_action: Action | None,
) -> None:
    try:
        runtime.state = _failure_state(det_state_before)
    except Exception:
        return
    callbacks.emit(
        ControllerObservationKind.STATE_TRANSITION,
        model_call_index=det_index,
        state_before=det_state_before,
        state_after=runtime.state,
    )
    try:
        runtime.steps.append(ControllerStepResult(
            model_call_index=det_index,
            state_before=det_state_before,
            state_after=runtime.state,
            directive_kind=None,
            action=record_action,
            observation=None,
            transition_reason=None,
            budget_before=det_budget_before,
            budget_after=runtime.budget_state,
            hypotheses_before=det_hypotheses_before,
            hypotheses_after=runtime.hypotheses,
            stop_reason=ControllerStopReason.CONTROLLER_ERROR,
            deterministic=True,
        ))
    except Exception:
        return
    callbacks.emit(
        ControllerObservationKind.STEP_COMPLETED,
        model_call_index=det_index,
        step_index=len(runtime.steps) - 1,
        state_before=det_state_before,
        state_after=runtime.state,
        directive_kind=None,
        stop_reason=ControllerStopReason.CONTROLLER_ERROR.value,
    )


def _det_dispatch_tool(
    context: PostPatchContext,
    runtime: PostPatchRuntime,
    callbacks: PostPatchCallbacks,
    action_name: object,
    arguments: dict[str, object],
    trig_index: int,
) -> tuple[Observation | None, str]:
    """Dispatch one deterministic tool; never consumes a model request.

    The owning model request's ordinal (``trig_index``) is recorded on
    the step for attribution, but the model ordinal sequence is NOT
    advanced: the next model snapshot keeps the contiguous next ordinal.
    Action/observation uniqueness comes from the controller-allocated
    deterministic identity pair in the disjoint
    det-action-/det-observation- namespace.

    Returns (observation, outcome) where outcome is one of:
    "ok" (tool succeeded, step recorded, continue pipeline),
    "abort" (not dispatchable or non-fatal tool outcome; return
    control to the model with preserved state), "failed"
    (non-recoverable failure; terminal FAILED already recorded),
    "controller_error" / "budget_exhausted" (terminal already
    recorded with that stop reason).
    """
    det_index = trig_index
    det_state_before = runtime.state
    det_budget_before = runtime.budget_state
    det_hypotheses_before = runtime.hypotheses
    # Deterministic dispatch is only valid for exact action names.
    if type(action_name) is not ActionName:
        return None, "abort"
    try:
        allowed = is_action_allowed(det_state_before, action_name)
    except Exception:
        # Fail closed as controller error (terminal, honest).
        _det_record_controller_error(
            runtime, callbacks,
            det_index, det_state_before, det_budget_before,
            det_hypotheses_before, None,
        )
        return None, "controller_error"
    if not allowed:
        return None, "abort"
    try:
        context.registry.get(action_name)
    except Exception:
        return None, "abort"
    try:
        bkind = budget_kind_for_action(action_name)
    except Exception:
        _det_record_controller_error(
            runtime, callbacks,
            det_index, det_state_before, det_budget_before,
            det_hypotheses_before, None,
        )
        return None, "controller_error"
    if bkind is not None:
        try:
            remaining = _remaining(context.budget_limits, runtime.budget_state, bkind)
        except Exception:
            _det_record_controller_error(
                runtime, callbacks,
                det_index, det_state_before, det_budget_before,
                det_hypotheses_before, None,
            )
            return None, "controller_error"
        if remaining <= 0:
            # Honest budget exhaustion during deterministic work.
            nonlocal_state_before = det_state_before
            try:
                runtime.state = _failure_state(nonlocal_state_before)
            except Exception:
                return None, "controller_error"
            callbacks.emit(
                ControllerObservationKind.STATE_TRANSITION,
                model_call_index=det_index,
                state_before=nonlocal_state_before,
                state_after=runtime.state,
            )
            runtime.steps.append(ControllerStepResult(
                model_call_index=det_index,
                state_before=nonlocal_state_before,
                state_after=runtime.state,
                directive_kind=None,
                action=None,
                observation=None,
                transition_reason=None,
                budget_before=det_budget_before,
                budget_after=runtime.budget_state,
                hypotheses_before=det_hypotheses_before,
                hypotheses_after=runtime.hypotheses,
                stop_reason=ControllerStopReason.BUDGET_EXHAUSTED,
                deterministic=True,
            ))
            callbacks.emit(
                ControllerObservationKind.STEP_COMPLETED,
                model_call_index=det_index,
                step_index=len(runtime.steps) - 1,
                state_before=nonlocal_state_before,
                state_after=runtime.state,
                directive_kind=None,
                stop_reason=ControllerStopReason.BUDGET_EXHAUSTED.value,
            )
            return None, "budget_exhausted"
    try:
        copied_args = _copy_json(arguments)
    except Exception:
        _det_record_controller_error(
            runtime, callbacks,
            det_index, det_state_before, det_budget_before,
            det_hypotheses_before, None,
        )
        return None, "controller_error"
    if type(copied_args) is not dict:
        _det_record_controller_error(
            runtime, callbacks,
            det_index, det_state_before, det_budget_before,
            det_hypotheses_before, None,
        )
        return None, "controller_error"
    try:
        det_action_id, det_observation_id = callbacks.allocate_det_ids()
    except Exception:
        _det_record_controller_error(
            runtime, callbacks,
            det_index, det_state_before, det_budget_before,
            det_hypotheses_before, None,
        )
        return None, "controller_error"
    try:
        record_action = Action(
            action_id=det_action_id,
            run_id=context.run_id,
            task_id=context.task_id,
            state=det_state_before,
            name=action_name.value,
            arguments=copied_args,
        )
        dispatch_action = Action(
            action_id=det_action_id,
            run_id=context.run_id,
            task_id=context.task_id,
            state=det_state_before,
            name=action_name.value,
            arguments=_copy_json(copied_args),
        )
    except Exception:
        _det_record_controller_error(
            runtime, callbacks,
            det_index, det_state_before, det_budget_before,
            det_hypotheses_before, None,
        )
        return None, "controller_error"
    callbacks.check_cancelled()
    callbacks.emit(
        ControllerObservationKind.TOOL_STARTED,
        model_call_index=det_index,
        state_before=det_state_before,
        tool_name=action_name.value,
    )
    tool_started = True
    try:
        observation_raw = ToolRegistry.dispatch(
            context.registry,
            dispatch_action,
            observation_id=det_observation_id,
        )
        _assert_dispatch_identity(record_action, dispatch_action)
        observation, reason = _canonical_observation(
            observation_raw, record_action, det_observation_id
        )
    except CancellationError:
        raise
    except Exception:
        callbacks.emit(
            ControllerObservationKind.TOOL_COMPLETED,
            model_call_index=det_index,
            state_before=det_state_before,
            tool_name=action_name.value,
            observation_status=ObservationStatus.ERROR,
        )
        _det_record_controller_error(
            runtime, callbacks,
            det_index, det_state_before, det_budget_before,
            det_hypotheses_before, record_action,
        )
        return None, "controller_error"
    runtime.tool_observations.append(observation)
    callbacks.emit(
        ControllerObservationKind.TOOL_COMPLETED,
        model_call_index=det_index,
        state_before=det_state_before,
        tool_name=action_name.value,
        observation_status=observation.status,
    )
    if bkind is not None and reason not in _NO_HANDLER_REASONS:
        try:
            runtime.budget_state = _consume_direct(
                context.budget_limits, runtime.budget_state, bkind
            )
        except Exception:
            _det_record_controller_error(
                runtime, callbacks,
                det_index, det_state_before, det_budget_before,
                det_hypotheses_before, record_action,
            )
            return None, "controller_error"
    runtime.last_observation = observation
    if _det_is_fatal(observation):
        failed_before = det_state_before
        try:
            runtime.state = _failure_state(failed_before)
        except Exception:
            _det_record_controller_error(
                runtime, callbacks,
                det_index, det_state_before, det_budget_before,
                det_hypotheses_before, record_action,
            )
            return None, "controller_error"
        runtime.steps.append(ControllerStepResult(
            model_call_index=det_index,
            state_before=det_state_before,
            state_after=runtime.state,
            directive_kind=None,
            action=record_action,
            observation=observation,
            transition_reason=None,
            budget_before=det_budget_before,
            budget_after=runtime.budget_state,
            hypotheses_before=det_hypotheses_before,
            hypotheses_after=runtime.hypotheses,
            stop_reason=ControllerStopReason.FAILED,
            deterministic=True,
        ))
        callbacks.emit(
            ControllerObservationKind.STEP_COMPLETED,
            model_call_index=det_index,
            step_index=len(runtime.steps) - 1,
            state_before=det_state_before,
            state_after=runtime.state,
            directive_kind=None,
            stop_reason=ControllerStopReason.FAILED.value,
        )
        callbacks.emit(
            ControllerObservationKind.STATE_TRANSITION,
            model_call_index=det_index,
            state_before=failed_before,
            state_after=runtime.state,
            transition_reason="fatal non-recoverable tool execution failure",
        )
        return observation, "failed"
    runtime.steps.append(ControllerStepResult(
        model_call_index=det_index,
        state_before=det_state_before,
        state_after=det_state_before,
        directive_kind=None,
        action=record_action,
        observation=observation,
        transition_reason=None,
        budget_before=det_budget_before,
        budget_after=runtime.budget_state,
        hypotheses_before=det_hypotheses_before,
        hypotheses_after=runtime.hypotheses,
        stop_reason=None,
        deterministic=True,
    ))
    callbacks.emit(
        ControllerObservationKind.STEP_COMPLETED,
        model_call_index=det_index,
        step_index=len(runtime.steps) - 1,
        state_before=det_state_before,
        state_after=det_state_before,
        directive_kind=None,
    )
    # Non-OK tool status (TIMEOUT/REJECTED/ERROR without fatal
    # marking) preserves honest evidence but requires a model
    # decision; abort the pipeline with state preserved.  The model
    # ordinal sequence is untouched: the next model snapshot keeps
    # the contiguous next ordinal.
    if observation.status is not ObservationStatus.OK:
        return observation, "abort"
    return observation, "ok"


def _det_transition(
    runtime: PostPatchRuntime,
    callbacks: PostPatchCallbacks,
    target: ControllerState,
    reason_text: str,
    trig_index: int,
) -> str:
    """Deterministically advance state; returns outcome string.

    "ok" (transition recorded, continue), "abort" (not allowed;
    return to model), "controller_error" (terminal already
    recorded).  The model ordinal sequence is NOT advanced: the
    step is attributed to the owning model request.
    """
    det_index = trig_index
    det_state_before = runtime.state
    det_budget_before = runtime.budget_state
    det_hypotheses_before = runtime.hypotheses
    try:
        allowed = is_transition_allowed(runtime.state, target)
    except Exception:
        _det_record_controller_error(
            runtime, callbacks,
            det_index, det_state_before, det_budget_before,
            det_hypotheses_before, None,
        )
        return "controller_error"
    if not allowed:
        return "abort"
    callbacks.check_cancelled()
    runtime.state = target
    stop = None
    if runtime.state is ControllerState.DONE:
        stop = ControllerStopReason.DONE
    elif runtime.state is ControllerState.FAILED:
        stop = ControllerStopReason.FAILED
    callbacks.emit(
        ControllerObservationKind.STATE_TRANSITION,
        model_call_index=det_index,
        state_before=det_state_before,
        state_after=runtime.state,
        transition_reason=reason_text,
    )
    try:
        runtime.steps.append(ControllerStepResult(
            model_call_index=det_index,
            state_before=det_state_before,
            state_after=runtime.state,
            directive_kind=None,
            action=None,
            observation=None,
            transition_reason=reason_text,
            budget_before=det_budget_before,
            budget_after=runtime.budget_state,
            hypotheses_before=det_hypotheses_before,
            hypotheses_after=runtime.hypotheses,
            stop_reason=stop,
            deterministic=True,
        ))
    except Exception:
        _det_record_controller_error(
            runtime, callbacks,
            det_index, det_state_before, det_budget_before,
            det_hypotheses_before, None,
        )
        return "controller_error"
    callbacks.emit(
        ControllerObservationKind.STEP_COMPLETED,
        model_call_index=det_index,
        step_index=len(runtime.steps) - 1,
        state_before=det_state_before,
        state_after=runtime.state,
        directive_kind=None,
        transition_reason=reason_text,
        stop_reason=stop.value if stop is not None else None,
    )
    return "ok"


def run_post_patch_validation(
    context: PostPatchContext,
    runtime: PostPatchRuntime,
    callbacks: PostPatchCallbacks,
    *,
    trig_index: int,
) -> PostPatchOutcome:
    """Run the mandatory validation pipeline without model requests.

    ``trig_index`` is the owning APPLY_PATCH request's ordinal; all
    deterministic steps are attributed to it and the model ordinal
    sequence is left contiguous for the next model call.

    Returns a :class:`PostPatchOutcome` carrying an honest terminal stop
    reason when the pipeline reaches one (DONE, FAILED,
    BUDGET_EXHAUSTED, CONTROLLER_ERROR), otherwise a None terminal to
    return control to the model with preserved evidence.  The controller
    translates a terminal outcome into its own terminal result; this
    helper never constructs a run result.
    """
    # 1. Mandatory syntax validation in PATCH.
    callbacks.check_cancelled()
    syntax_obs, syntax_outcome = _det_dispatch_tool(
        context, runtime, callbacks,
        ActionName.SYNTAX_CHECK, {}, trig_index,
    )
    if syntax_outcome in ("failed", "controller_error", "budget_exhausted"):
        if syntax_outcome == "failed":
            return PostPatchOutcome(terminal=ControllerStopReason.FAILED)
        if syntax_outcome == "budget_exhausted":
            return PostPatchOutcome(terminal=ControllerStopReason.BUDGET_EXHAUSTED)
        return PostPatchOutcome(terminal=ControllerStopReason.CONTROLLER_ERROR)
    if syntax_outcome != "ok":
        return PostPatchOutcome(terminal=None)
    # Syntax tool succeeded; the candidate must pass to continue.
    try:
        syntax_passed = bool(syntax_obs.payload.get("all_passed"))
    except Exception:
        return PostPatchOutcome(terminal=None)
    if syntax_obs.status is not ObservationStatus.OK or not syntax_passed:
        # Model-correctable: revised repair required.
        return PostPatchOutcome(terminal=None)
    # 2. Deterministic PATCH -> VALIDATE transition.
    callbacks.check_cancelled()
    transition_outcome = _det_transition(
        runtime, callbacks,
        ControllerState.VALIDATE,
        "deterministic continuation: syntax validated; advancing to Validate",
        trig_index,
    )
    if transition_outcome == "controller_error":
        return PostPatchOutcome(terminal=ControllerStopReason.CONTROLLER_ERROR)
    if transition_outcome != "ok":
        return PostPatchOutcome(terminal=None)
    # 3. Mandatory post-patch reproduction in VALIDATE.
    callbacks.check_cancelled()
    repro_obs, repro_outcome = _det_dispatch_tool(
        context, runtime, callbacks,
        ActionName.RUN_REPRODUCTION, {"phase": "post_patch"}, trig_index,
    )
    if repro_outcome in ("failed", "controller_error", "budget_exhausted"):
        if repro_outcome == "failed":
            return PostPatchOutcome(terminal=ControllerStopReason.FAILED)
        if repro_outcome == "budget_exhausted":
            return PostPatchOutcome(terminal=ControllerStopReason.BUDGET_EXHAUSTED)
        return PostPatchOutcome(terminal=ControllerStopReason.CONTROLLER_ERROR)
    if repro_outcome != "ok":
        return PostPatchOutcome(terminal=None)
    if repro_obs.status is not ObservationStatus.OK:
        return PostPatchOutcome(terminal=None)
    # 4. Mandatory regression checks in VALIDATE.
    callbacks.check_cancelled()
    regression_obs, regression_outcome = _det_dispatch_tool(
        context, runtime, callbacks,
        ActionName.RUN_REGRESSION_TESTS, {}, trig_index,
    )
    if regression_outcome in ("failed", "controller_error", "budget_exhausted"):
        if regression_outcome == "failed":
            return PostPatchOutcome(terminal=ControllerStopReason.FAILED)
        if regression_outcome == "budget_exhausted":
            return PostPatchOutcome(terminal=ControllerStopReason.BUDGET_EXHAUSTED)
        return PostPatchOutcome(terminal=ControllerStopReason.CONTROLLER_ERROR)
    if regression_outcome != "ok":
        return PostPatchOutcome(terminal=None)
    if regression_obs.status is not ObservationStatus.OK:
        return PostPatchOutcome(terminal=None)
    # 5. Mandatory outcome classification in VALIDATE.
    callbacks.check_cancelled()
    classify_obs, classify_outcome = _det_dispatch_tool(
        context, runtime, callbacks,
        ActionName.CLASSIFY_OUTCOME, {}, trig_index,
    )
    if classify_outcome in ("failed", "controller_error", "budget_exhausted"):
        if classify_outcome == "failed":
            return PostPatchOutcome(terminal=ControllerStopReason.FAILED)
        if classify_outcome == "budget_exhausted":
            return PostPatchOutcome(terminal=ControllerStopReason.BUDGET_EXHAUSTED)
        return PostPatchOutcome(terminal=ControllerStopReason.CONTROLLER_ERROR)
    if classify_outcome != "ok":
        return PostPatchOutcome(terminal=None)
    if classify_obs.status is not ObservationStatus.OK:
        return PostPatchOutcome(terminal=None)
    try:
        outcome_value = classify_obs.payload.get("outcome")
    except Exception:
        return PostPatchOutcome(terminal=None)
    # Only an authoritative RESOLVED classification completes
    # deterministically.  Any other outcome (BREAKING_RESOLVED,
    # PARTIALLY_RESOLVED, NO_OP, REGRESSION, unclassified) requires
    # a model repair decision; preserve evidence and return.
    if outcome_value != "RESOLVED":
        return PostPatchOutcome(terminal=None)
    # 6. Deterministic VALIDATE -> DONE transition.
    callbacks.check_cancelled()
    done_outcome = _det_transition(
        runtime, callbacks,
        ControllerState.DONE,
        "deterministic continuation: controller validation classified RESOLVED",
        trig_index,
    )
    if done_outcome == "controller_error":
        return PostPatchOutcome(terminal=ControllerStopReason.CONTROLLER_ERROR)
    if done_outcome != "ok":
        return PostPatchOutcome(terminal=None)
    return PostPatchOutcome(terminal=ControllerStopReason.DONE)


__all__ = [
    "PostPatchRuntime",
    "PostPatchContext",
    "PostPatchCallbacks",
    "PostPatchOutcome",
    "run_post_patch_validation",
]
