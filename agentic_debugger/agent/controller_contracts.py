"""Deterministic controller contracts: validation, canonicalization, config.

This module owns the immutable contract layer beneath the deterministic
controller: the error vocabulary, :class:`ControllerStopReason`, the
fail-closed primitive validators, the deep canonical-copy authority
(budgets, hypothesis ledger, observations, initial snapshots, tool
registries), the trusted model/observer method-resolution seams,
:class:`ControllerRunConfig`, and the record-shape validators (action,
detached JSON, budget records, hypothesis ledger, transition reason).
"""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from agentic_debugger.agent.controller_policy import (
    ActionName,
    BudgetKind,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
    HypothesisStatus,
    RootCauseHypothesis,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    AddHypothesisDirective,
    ControllerSnapshot,
    ModelDirectiveKind,
    ReviseHypothesisDirective,
    SetHypothesisStatusDirective,
    TransitionDirective,
    directive_kind,
)
from agentic_debugger.agent.state_machine import ControllerState, is_transition_allowed
from agentic_debugger.agent.tool_registry import ToolRegistry, ToolSpec
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
    # Task 44 (Unbounded Session Progress v1): ``max_model_calls`` is
    # ``None`` for unbounded interactive/configured execution — the
    # controller may serve model requests indefinitely and telemetry
    # (``model_calls``/``steps``) grows without an execution ceiling.
    # An explicit finite value remains honored ONLY for explicit callers
    # (deterministic unit harnesses, frozen scientific treatments) that
    # intentionally request a bound; generic application sources always
    # pass ``None``.  Counters are telemetry, not execution authority
    # for unrestricted sessions.
    max_model_calls: int | None = None
    require_pdb_evidence_before_patch: bool = False
    # Successful Session Token Efficiency v1: when True, a successfully
    # accepted/applied candidate patch deterministically continues the
    # mandatory validation pipeline (syntax check, Validate transition,
    # post-patch reproduction, regression, classification, Done
    # transition) without additional model requests.  Any semantic or
    # recoverable failure returns control to the model; non-recoverable
    # infrastructure failures terminate honestly.  Default False preserves
    # the historical per-step model-request behavior for explicit
    # scripted harnesses; product sources opt in explicitly.
    deterministic_post_patch_validation: bool = False

    def __post_init__(self) -> None:
        if self.max_model_calls is None:
            pass
        elif type(self.max_model_calls) is not int or isinstance(self.max_model_calls, bool):
            raise ControllerInputError("invalid max_model_calls")
        elif not 1 <= self.max_model_calls <= MAX_CONTROLLER_MODEL_CALLS:
            raise ControllerInputError("invalid max_model_calls")
        if type(self.require_pdb_evidence_before_patch) is not bool:
            raise ControllerInputError("invalid require_pdb_evidence_before_patch")
        if type(self.deterministic_post_patch_validation) is not bool:
            raise ControllerInputError("invalid deterministic_post_patch_validation")


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
