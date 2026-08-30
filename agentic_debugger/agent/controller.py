"""Deterministic, in-memory controller execution for Task 5C2."""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from agentic_debugger.agent.controller_policy import (
    ActionName,
    BudgetKind,
    ControllerBudgetLimits,
    ControllerBudgetState,
    ControllerPolicyError,
    HypothesisConfidence,
    HypothesisLedger,
    HypothesisStatus,
    RootCauseHypothesis,
    budget_kind_for_action,
    is_action_allowed,
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
    directive_kind,
)
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
    ToolSpec,
)
from agentic_debugger.cancellation import CancellationError
from agentic_debugger.events.schema import Action, Observation, ObservationStatus


DEFAULT_MAX_MODEL_CALLS = 64
MAX_CONTROLLER_MODEL_CALLS = 10_000
MAX_CONTROLLER_MODEL_CALL_INDEX = 999_999_999


class ControllerError(ValueError):
    """Base error for controller construction and result contracts."""


class ControllerInputError(ControllerError):
    """Raised when a controller or snapshot input is malformed."""


class ControllerInvariantError(ControllerError):
    """Raised when an internal controller invariant cannot be maintained."""


class ControllerStopReason(str, Enum):
    DONE = "done"
    FAILED = "failed"
    MODEL_SCRIPT_EXHAUSTED = "model_script_exhausted"
    MODEL_SCRIPT_MISMATCH = "model_script_mismatch"
    MODEL_ERROR = "model_error"
    DIRECTIVE_REJECTED = "directive_rejected"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MODEL_CALL_LIMIT = "model_call_limit"
    CONTROLLER_ERROR = "controller_error"


def _input(field_name: str) -> None:
    raise ControllerInputError(f"invalid {field_name}")


def _invariant(field_name: str) -> None:
    raise ControllerInvariantError(f"invalid {field_name}")


def _exact(value: object, expected: type, field_name: str) -> None:
    if type(value) is not expected:
        _input(field_name)


def _exact_nonnegative_int(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        _input(field_name)


def _bounded_text(value: object, field_name: str, maximum_bytes: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        _input(field_name)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _input(field_name)
    if len(encoded) > maximum_bytes:
        _input(field_name)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        _input(field_name)
    return value


def _identifier(value: object, field_name: str) -> str:
    return _bounded_text(value, field_name, 256)


def _model_error_text(value: object, fallback: str, maximum_bytes: int) -> str:
    """Accept only already-bounded model error telemetry, else use fallback."""

    if type(value) is not str or not value or value != value.strip():
        return fallback
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return fallback
    if len(encoded) > maximum_bytes or any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in value
    ):
        return fallback
    return value


def _read(value: object, field_name: str) -> object:
    try:
        return getattr(value, field_name)
    except Exception:
        _input(field_name)


def _copy_json(value: object, *, depth: int = 0, nodes: list[int] | None = None,
               active: set[int] | None = None) -> object:
    """Copy JSON-compatible values without invoking caller-defined methods."""

    if nodes is None:
        nodes = [0]
    if active is None:
        active = set()
    if depth > 32:
        _input("json")
    nodes[0] += 1
    if nodes[0] > 4096:
        _input("json")
    value_type = type(value)
    if value is None or value_type is bool or value_type is int or value_type is str:
        return value
    if value_type is float:
        if not math.isfinite(value):
            _input("json")
        return value
    if value_type in (list, dict):
        identity = id(value)
        if identity in active:
            _input("json")
        active.add(identity)
        try:
            if value_type is dict:
                copied: dict[str, object] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        _input("json")
                    copied[key] = _copy_json(item, depth=depth + 1,
                                             nodes=nodes, active=active)
                return copied
            copied_sequence = [
                _copy_json(item, depth=depth + 1, nodes=nodes, active=active)
                for item in value
            ]
            return copied_sequence
        finally:
            active.remove(identity)
    _input("json")


def _copy_limits(value: object) -> ControllerBudgetLimits:
    _exact(value, ControllerBudgetLimits, "budget_limits")
    try:
        return ControllerBudgetLimits(
            max_patch_attempts=value.max_patch_attempts,
            max_test_runs=value.max_test_runs,
            max_pdb_observations=value.max_pdb_observations,
            max_active_hypotheses=value.max_active_hypotheses,
            max_source_observations=value.max_source_observations,
        )
    except Exception:
        _input("budget_limits")


def _copy_budget_state(value: object) -> ControllerBudgetState:
    _exact(value, ControllerBudgetState, "budget_state")
    try:
        return ControllerBudgetState(
            patch_attempts=value.patch_attempts,
            test_runs=value.test_runs,
            pdb_observations=value.pdb_observations,
            source_observations=value.source_observations,
        )
    except Exception:
        _input("budget_state")


def _copy_hypothesis(value: object) -> RootCauseHypothesis:
    _exact(value, RootCauseHypothesis, "hypothesis")
    try:
        return RootCauseHypothesis(
            hypothesis_id=value.hypothesis_id,
            statement=value.statement,
            confidence=value.confidence,
            status=value.status,
            evidence_refs=value.evidence_refs,
            requires_runtime_evidence=value.requires_runtime_evidence,
            revision=value.revision,
        )
    except Exception:
        _input("hypothesis")


def _copy_ledger(value: object, limits: ControllerBudgetLimits) -> HypothesisLedger:
    _exact(value, HypothesisLedger, "hypotheses")
    records = _read(value, "hypotheses")
    if type(records) is not tuple:
        _input("hypotheses")
    copied = tuple(_copy_hypothesis(record) for record in records)
    active_count = sum(
        1 for record in copied if record.status is HypothesisStatus.ACTIVE
    )
    if active_count > limits.max_active_hypotheses:
        _input("hypotheses")
    try:
        return HypothesisLedger(copied)
    except Exception:
        _input("hypotheses")


def _copy_observation(value: object, *, run_id: str | None = None,
                      task_id: str | None = None) -> Observation:
    _exact(value, Observation, "last_observation")
    try:
        observation_id = value.observation_id
        action_id = value.action_id
        observation_run_id = value.run_id
        observation_task_id = value.task_id
        name = value.name
        status = value.status
        payload = value.payload
        summary = value.summary
        truncated = value.truncated
    except Exception:
        _input("last_observation")
    for field_name, field_value in (
        ("observation_id", observation_id),
        ("action_id", action_id),
        ("run_id", observation_run_id),
        ("task_id", observation_task_id),
        ("name", name),
    ):
        if type(field_value) is not str or not field_value:
            _input("last_observation")
    if type(status) is not ObservationStatus or type(payload) is not dict:
        _input("last_observation")
    if type(summary) is not str or type(truncated) is not bool:
        _input("last_observation")
    if run_id is not None and observation_run_id != run_id:
        _input("last_observation")
    if task_id is not None and observation_task_id != task_id:
        _input("last_observation")
    copied_payload = _copy_json(payload)
    if type(copied_payload) is not dict:
        _input("last_observation")
    try:
        return Observation(
            observation_id=observation_id,
            action_id=action_id,
            run_id=observation_run_id,
            task_id=observation_task_id,
            name=name,
            status=status,
            payload=copied_payload,
            summary=summary,
            truncated=truncated,
        )
    except Exception:
        _input("last_observation")


def _canonicalize_initial_snapshot(snapshot: ControllerSnapshot) -> ControllerSnapshot:
    _exact(snapshot, ControllerSnapshot, "snapshot")
    run_id = _identifier(_read(snapshot, "run_id"), "run_id")
    task_id = _identifier(_read(snapshot, "task_id"), "task_id")
    state = _read(snapshot, "state")
    _exact(state, ControllerState, "state")
    model_call_index = _read(snapshot, "model_call_index")
    _exact_nonnegative_int(model_call_index, "model_call_index")
    limits = _copy_limits(_read(snapshot, "budget_limits"))
    budget_state = _copy_budget_state(_read(snapshot, "budget_state"))
    ledger = _copy_ledger(_read(snapshot, "hypotheses"), limits)
    last = _read(snapshot, "last_observation")
    copied_observation = None if last is None else _copy_observation(
        last, run_id=run_id, task_id=task_id
    )
    try:
        canonical = ControllerSnapshot(
            run_id=run_id,
            task_id=task_id,
            state=state,
            model_call_index=model_call_index,
            budget_limits=limits,
            budget_state=budget_state,
            hypotheses=ledger,
            last_observation=copied_observation,
        )
    except Exception:
        _input("snapshot")
    return canonical


def _canonicalize_registry(registry: object) -> ToolRegistry:
    _exact(registry, ToolRegistry, "registry")
    specs = _read(registry, "specs")
    if type(specs) is not tuple:
        _input("registry")
    canonical_specs: list[ToolSpec] = []
    for spec in specs:
        _exact(spec, ToolSpec, "tool_spec")
        try:
            canonical_specs.append(ToolSpec(
                name=spec.name,
                argument_validator=spec.argument_validator,
                handler=spec.handler,
                version=spec.version,
                argument_contract=spec.argument_contract,
            ))
        except Exception:
            _input("registry")
    try:
        return ToolRegistry(tuple(canonical_specs))
    except Exception:
        _input("registry")


def _resolve_model_method(adapter: object) -> Callable[..., object]:
    try:
        method = inspect.getattr_static(type(adapter), "next_directive")
    except (AttributeError, TypeError):
        _input("model_adapter")
    if not inspect.isfunction(method) or not callable(method):
        _input("model_adapter")
    return method


def _resolve_observer_notify(observer: object) -> Callable[..., None]:
    try:
        method = inspect.getattr_static(type(observer), "notify")
    except (AttributeError, TypeError):
        _input("observer")
    if not inspect.isfunction(method) or not callable(method):
        _input("observer")
    return method


@dataclass(frozen=True)
class ControllerRunConfig:
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS
    require_pdb_evidence_before_patch: bool = False

    def __post_init__(self) -> None:
        if type(self.max_model_calls) is not int:
            raise ControllerInputError("invalid max_model_calls")
        if not 1 <= self.max_model_calls <= MAX_CONTROLLER_MODEL_CALLS:
            raise ControllerInputError("invalid max_model_calls")
        if type(self.require_pdb_evidence_before_patch) is not bool:
            raise ControllerInputError("invalid require_pdb_evidence_before_patch")


def _validate_action(value: object) -> None:
    if type(value) is not Action:
        _invariant("action")
    if (
        type(value.action_id) is not str
        or type(value.run_id) is not str
        or type(value.task_id) is not str
        or type(value.state) is not ControllerState
        or type(value.name) is not str
        or type(value.arguments) is not dict
    ):
        _invariant("action")
    _validate_detached_json(value.arguments, "action")


def _validate_detached_json(value: object, field_name: str) -> None:
    try:
        _copy_json(value)
    except ControllerInputError:
        _invariant(field_name)


def _validate_budget_records(limits: object, state: object) -> None:
    if type(limits) is not ControllerBudgetLimits:
        _invariant("budget_limits")
    if type(state) is not ControllerBudgetState:
        _invariant("budget_state")
    try:
        values = (
            limits.max_patch_attempts, limits.max_test_runs,
            limits.max_pdb_observations, limits.max_active_hypotheses,
            limits.max_source_observations, state.patch_attempts,
            state.test_runs, state.pdb_observations, state.source_observations,
        )
    except Exception:
        _invariant("budget_state")
    if any(type(value) is not int for value in values):
        _invariant("budget_state")
    if any(value < 0 for value in values[5:]):
        _invariant("budget_state")
    if (
        state.patch_attempts > limits.max_patch_attempts
        or state.test_runs > limits.max_test_runs
        or state.pdb_observations > limits.max_pdb_observations
        or state.source_observations > limits.max_source_observations
    ):
        _invariant("budget_state")


def _validate_ledger_shape(value: object) -> None:
    if type(value) is not HypothesisLedger or type(value.hypotheses) is not tuple:
        _invariant("hypotheses")
    seen: set[str] = set()
    for record in value.hypotheses:
        if type(record) is not RootCauseHypothesis:
            _invariant("hypotheses")
        try:
            canonical = RootCauseHypothesis(
                hypothesis_id=record.hypothesis_id,
                statement=record.statement,
                confidence=record.confidence,
                status=record.status,
                evidence_refs=record.evidence_refs,
                requires_runtime_evidence=record.requires_runtime_evidence,
                revision=record.revision,
            )
        except Exception:
            _invariant("hypotheses")
        if canonical.hypothesis_id in seen:
            _invariant("hypotheses")
        seen.add(canonical.hypothesis_id)


def _validate_ledger(value: object, limits: ControllerBudgetLimits) -> None:
    _validate_ledger_shape(value)
    if sum(record.status is HypothesisStatus.ACTIVE for record in value.hypotheses) > limits.max_active_hypotheses:
        _invariant("hypotheses")


def _validate_transition_reason(value: object) -> None:
    if type(value) is not str or not value or value != value.strip():
        _invariant("transition_reason")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _invariant("transition_reason")
    if len(encoded) > 2048 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        _invariant("transition_reason")


@dataclass(frozen=True)
class ControllerStepResult:
    model_call_index: int
    state_before: ControllerState
    state_after: ControllerState
    directive_kind: ModelDirectiveKind | None
    action: Action | None
    observation: Observation | None
    transition_reason: str | None
    budget_before: ControllerBudgetState
    budget_after: ControllerBudgetState
    hypotheses_before: HypothesisLedger
    hypotheses_after: HypothesisLedger
    stop_reason: ControllerStopReason | None = None

    def __post_init__(self) -> None:
        if type(self.model_call_index) is not int or self.model_call_index < 0:
            raise ControllerInvariantError("invalid model_call_index")
        if type(self.state_before) is not ControllerState or type(self.state_after) is not ControllerState:
            raise ControllerInvariantError("invalid state")
        if self.directive_kind is not None and type(self.directive_kind) is not ModelDirectiveKind:
            raise ControllerInvariantError("invalid directive_kind")
        if self.stop_reason is not None and type(self.stop_reason) is not ControllerStopReason:
            raise ControllerInvariantError("invalid stop_reason")
        _validate_budget_records(self._limits_for_validation(), self.budget_before)
        _validate_budget_records(self._limits_for_validation(), self.budget_after)
        _validate_ledger_shape(self.hypotheses_before)
        _validate_ledger_shape(self.hypotheses_after)
        if self.transition_reason is not None:
            _validate_transition_reason(self.transition_reason)
        if self.action is not None:
            _validate_action(self.action)
            if self.action.run_id == "" or self.action.task_id == "":
                raise ControllerInvariantError("invalid action")
            if self.action.state is not self.state_before:
                raise ControllerInvariantError("invalid action state")
            if self.action.action_id != _action_id(self.model_call_index):
                raise ControllerInvariantError("invalid action id")
            if self.action.name not in {candidate.value for candidate in ActionName}:
                raise ControllerInvariantError("invalid action name")
        if self.observation is not None:
            if self.action is None or type(self.observation) is not Observation:
                raise ControllerInvariantError("invalid observation")
            try:
                observation_id = self.observation.observation_id
                action_id = self.observation.action_id
                observation_run_id = self.observation.run_id
                observation_task_id = self.observation.task_id
                observation_name = self.observation.name
                observation_status = self.observation.status
                observation_payload = self.observation.payload
                observation_summary = self.observation.summary
                observation_truncated = self.observation.truncated
            except Exception:
                raise ControllerInvariantError("invalid observation")
            if (
                type(observation_id) is not str
                or type(action_id) is not str
                or type(observation_run_id) is not str
                or type(observation_task_id) is not str
                or type(observation_name) is not str
                or type(observation_status) is not ObservationStatus
                or type(observation_payload) is not dict
                or type(observation_summary) is not str
                or type(observation_truncated) is not bool
            ):
                raise ControllerInvariantError("invalid observation")
            try:
                trusted_payload = _copy_json(observation_payload)
            except ControllerInputError:
                raise ControllerInvariantError("invalid observation")
            if type(trusted_payload) is not dict:
                raise ControllerInvariantError("invalid observation")
            if "dispatch_reason" not in trusted_payload or type(trusted_payload["dispatch_reason"]) is not str:
                raise ControllerInvariantError("invalid dispatch_reason")
            if not any(candidate.value == trusted_payload["dispatch_reason"] for candidate in ToolDispatchReason):
                raise ControllerInvariantError("invalid dispatch_reason")
            if not observation_id or not action_id or not observation_run_id or not observation_task_id or not observation_name:
                raise ControllerInvariantError("invalid observation")
            if observation_id != _observation_id(self.model_call_index):
                raise ControllerInvariantError("invalid observation id")
            if (
                action_id != self.action.action_id
                or observation_run_id != self.action.run_id
                or observation_task_id != self.action.task_id
                or observation_name != self.action.name
            ):
                raise ControllerInvariantError("invalid observation correlation")
            try:
                canonical_observation = Observation(
                    observation_id=observation_id,
                    action_id=action_id,
                    run_id=observation_run_id,
                    task_id=observation_task_id,
                    name=observation_name,
                    status=observation_status,
                    payload=trusted_payload,
                    summary=observation_summary,
                    truncated=observation_truncated,
                )
            except Exception:
                raise ControllerInvariantError("invalid observation")
            object.__setattr__(self, "observation", canonical_observation)
        if self.directive_kind is ModelDirectiveKind.ACTION:
            if self.transition_reason is not None:
                raise ControllerInvariantError("invalid action step")
            if self.action is None:
                if self.stop_reason not in {
                    ControllerStopReason.DIRECTIVE_REJECTED,
                    ControllerStopReason.BUDGET_EXHAUSTED,
                }:
                    raise ControllerInvariantError("invalid action step")
            elif self.observation is None and self.stop_reason is not ControllerStopReason.CONTROLLER_ERROR:
                raise ControllerInvariantError("invalid action observation")
        elif self.directive_kind is ModelDirectiveKind.TRANSITION:
            if self.action is not None or self.observation is not None or self.transition_reason is None:
                raise ControllerInvariantError("invalid transition step")
        elif self.directive_kind in {
            ModelDirectiveKind.ADD_HYPOTHESIS,
            ModelDirectiveKind.REVISE_HYPOTHESIS,
            ModelDirectiveKind.SET_HYPOTHESIS_STATUS,
        }:
            if self.action is not None or self.observation is not None or self.transition_reason is not None:
                raise ControllerInvariantError("invalid hypothesis step")
        elif self.directive_kind is None:
            if self.action is not None or self.observation is not None or self.transition_reason is not None or self.stop_reason is None:
                raise ControllerInvariantError("invalid failure step")
        if self.stop_reason is not None:
            if self.stop_reason is ControllerStopReason.DONE:
                if self.state_after is not ControllerState.DONE:
                    raise ControllerInvariantError("invalid stop_state")
            elif self.state_after is not ControllerState.FAILED:
                raise ControllerInvariantError("invalid stop_state")

    def _limits_for_validation(self) -> ControllerBudgetLimits:
        """Use the exact limits carried by the snapshot-independent state."""

        # Step records intentionally carry budget states only.  The policy
        # limits are checked by the controller before constructing a step.
        # These positive, unbounded checks are enough for the immutable record.
        return ControllerBudgetLimits(
            max_patch_attempts=max(self.budget_before.patch_attempts, self.budget_after.patch_attempts, 1),
            max_test_runs=max(self.budget_before.test_runs, self.budget_after.test_runs, 1),
            max_pdb_observations=max(self.budget_before.pdb_observations, self.budget_after.pdb_observations),
            max_source_observations=max(self.budget_before.source_observations, self.budget_after.source_observations, 1),
        )


@dataclass(frozen=True)
class ControllerRunResult:
    run_id: str
    task_id: str
    initial_state: ControllerState
    final_state: ControllerState
    stop_reason: ControllerStopReason
    model_calls: int
    steps: tuple[ControllerStepResult, ...]
    budget_state: ControllerBudgetState
    hypotheses: HypothesisLedger
    last_observation: Observation | None

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not self.run_id or type(self.task_id) is not str or not self.task_id:
            raise ControllerError("invalid identifiers")
        if type(self.initial_state) is not ControllerState or type(self.final_state) is not ControllerState:
            raise ControllerError("invalid state")
        if type(self.stop_reason) is not ControllerStopReason:
            raise ControllerError("invalid stop_reason")
        if type(self.model_calls) is not int or self.model_calls < 0:
            raise ControllerError("invalid model_calls")
        if type(self.steps) is not tuple or any(type(step) is not ControllerStepResult for step in self.steps):
            raise ControllerError("invalid steps")
        if self.model_calls < len(self.steps):
            raise ControllerError("invalid model_calls")
        if type(self.budget_state) is not ControllerBudgetState:
            raise ControllerError("invalid budget_state")
        if any(type(getattr(self.budget_state, name)) is not int or getattr(self.budget_state, name) < 0
               for name in ("patch_attempts", "test_runs", "pdb_observations", "source_observations")):
            raise ControllerError("invalid budget_state")
        try:
            _validate_ledger_shape(self.hypotheses)
        except ControllerInvariantError:
            raise ControllerError("invalid hypotheses")
        if self.last_observation is not None:
            try:
                _copy_observation(self.last_observation,
                                  run_id=self.run_id,
                                  task_id=self.task_id)
            except ControllerInputError:
                raise ControllerError("invalid last_observation")
        if self.final_state not in (ControllerState.DONE, ControllerState.FAILED):
            raise ControllerError("invalid final_state")
        if self.stop_reason is ControllerStopReason.DONE:
            if self.final_state is not ControllerState.DONE:
                raise ControllerError("invalid stop_state")
        elif self.final_state is not ControllerState.FAILED:
            raise ControllerError("invalid stop_state")
        for step in self.steps:
            if step.action is not None:
                if step.action.run_id != self.run_id or step.action.task_id != self.task_id:
                    raise ControllerError("invalid action correlation")
            if step.observation is not None:
                if step.observation.run_id != self.run_id or step.observation.task_id != self.task_id:
                    raise ControllerError("invalid observation correlation")
        if self.last_observation is not None and (
            self.last_observation.run_id != self.run_id
            or self.last_observation.task_id != self.task_id
        ):
            raise ControllerError("invalid last_observation correlation")


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


def _action_id(index: int) -> str:
    return f"action-{index:09d}"


def _observation_id(index: int) -> str:
    return f"observation-{index:09d}"


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
    _canonical_max_model_calls: int = field(init=False, repr=False, compare=False)
    _canonical_require_pdb_evidence_before_patch: bool = field(init=False, repr=False, compare=False)
    _canonical_observer: ControllerObserver = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        canonical_registry = _canonicalize_registry(self.registry)
        if type(self.config) is not ControllerRunConfig:
            _input("config")
        max_model_calls = self.config.max_model_calls
        if type(max_model_calls) is not int or not 1 <= max_model_calls <= MAX_CONTROLLER_MODEL_CALLS:
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
        if type(self._canonical_max_model_calls) is not int or not 1 <= self._canonical_max_model_calls <= MAX_CONTROLLER_MODEL_CALLS:
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
        if snapshot.model_call_index + max_calls > MAX_CONTROLLER_MODEL_CALL_INDEX:
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
            if model_calls >= max_calls:
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
                _emit(ControllerObservationKind.MODEL_REQUEST_COMPLETED,
                      model_call_index=model_call_index, state_before=state,
                      request_status="error", error_kind="model_script_exhausted",
                      error_message="scripted model response is unavailable")
                failure_step(ControllerStopReason.MODEL_SCRIPT_EXHAUSTED)
                return result(ControllerStopReason.MODEL_SCRIPT_EXHAUSTED, state)
            except ModelScriptMismatchError:
                _emit(ControllerObservationKind.MODEL_REQUEST_COMPLETED,
                      model_call_index=model_call_index, state_before=state,
                      request_status="error", error_kind="model_script_mismatch",
                      error_message="scripted model response does not match controller state")
                failure_step(ControllerStopReason.MODEL_SCRIPT_MISMATCH)
                return result(ControllerStopReason.MODEL_SCRIPT_MISMATCH, state)
            except ModelAdapterError as exc:
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
                      ))
                failure_step(ControllerStopReason.MODEL_ERROR)
                return result(ControllerStopReason.MODEL_ERROR, state)
            except Exception:
                _emit(ControllerObservationKind.MODEL_REQUEST_COMPLETED,
                      model_call_index=model_call_index, state_before=state,
                      request_status="error", error_kind="unexpected_model_error",
                      error_message="unexpected model request failure")
                failure_step(ControllerStopReason.MODEL_ERROR)
                return result(ControllerStopReason.MODEL_ERROR, state)

            try:
                kind, directive = _canonical_directive(directive)
            except Exception:
                _emit(ControllerObservationKind.MODEL_REQUEST_COMPLETED,
                      model_call_index=model_call_index, state_before=state,
                      request_status="error", error_kind="invalid_directive",
                      error_message="model response did not produce a canonical directive")
                failure_step(ControllerStopReason.MODEL_ERROR)
                return result(ControllerStopReason.MODEL_ERROR, state)

            _check_cancelled()
            model_call_index += 1
            _emit(
                ControllerObservationKind.MODEL_REQUEST_COMPLETED,
                model_call_index=model_call_index - 1,
                state_before=state_before,
                request_status="ok",
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
