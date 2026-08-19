"""Pure, deterministic model-adapter contracts for the controller MVP."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, TypeAlias

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
    allowed_actions_for_state,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.events.schema import Observation


MAX_MODEL_ARGUMENT_BYTES = 32768
MAX_MODEL_JSON_DEPTH = 32
MAX_MODEL_JSON_NODES = 4096
MAX_MODEL_REASON_BYTES = 2048
MAX_MODEL_NAME_BYTES = 128


class ModelAdapterError(ValueError):
    """Raised when a model-adapter contract is invalid."""


class ModelScriptExhaustedError(ModelAdapterError):
    """Raised when a scripted adapter has no step for a snapshot index."""


class ModelScriptMismatchError(ModelAdapterError):
    """Raised when a scripted step does not match the snapshot state."""


class ModelDirectiveKind(str, Enum):
    ACTION = "action"
    TRANSITION = "transition"
    ADD_HYPOTHESIS = "add_hypothesis"
    REVISE_HYPOTHESIS = "revise_hypothesis"
    SET_HYPOTHESIS_STATUS = "set_hypothesis_status"


def _invalid(field: str) -> ModelAdapterError:
    return ModelAdapterError(f"invalid {field}")


def _require_exact(value: object, expected: type, field: str) -> None:
    if type(value) is not expected:
        raise _invalid(field)


def _require_enum(value: object, enum_type: type[Enum], field: str) -> None:
    if type(value) is not enum_type:
        raise _invalid(field)


def _validate_text(value: object, field: str, maximum_bytes: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _invalid(field)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid(field) from None
    if len(encoded) > maximum_bytes:
        raise _invalid(field)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise _invalid(field)
    return value


def _validate_hypothesis_id(value: object) -> str:
    value = _validate_text(value, "hypothesis_id", 128)
    if any(
        not (
            "a" <= char <= "z"
            or "A" <= char <= "Z"
            or "0" <= char <= "9"
            or char in "-_."
        )
        for char in value
    ):
        raise _invalid("hypothesis_id")
    return value


def _validate_hypothesis_fields(
    hypothesis_id: object,
    statement: object,
    confidence: object,
    evidence_refs: object,
    requires_runtime_evidence: object,
) -> RootCauseHypothesis:
    """Use Task 5A's accepted hypothesis validator as the source of truth."""

    if type(evidence_refs) is not tuple:
        raise _invalid("evidence_refs")
    try:
        return RootCauseHypothesis(
            hypothesis_id=hypothesis_id,
            statement=statement,
            confidence=confidence,
            status=HypothesisStatus.ACTIVE,
            evidence_refs=evidence_refs,
            requires_runtime_evidence=requires_runtime_evidence,
            revision=1,
        )
    except (ControllerPolicyError, TypeError, ValueError):
        raise _invalid("hypothesis") from None


class _JsonCopyBudget:
    __slots__ = ("nodes", "active")

    def __init__(self) -> None:
        self.nodes = 0
        self.active: set[int] = set()


def _copy_json_value(value: object, depth: int, budget: _JsonCopyBudget) -> object:
    if depth > MAX_MODEL_JSON_DEPTH:
        raise _invalid("arguments")
    budget.nodes += 1
    if budget.nodes > MAX_MODEL_JSON_NODES:
        raise _invalid("arguments")

    value_type = type(value)
    if value is None or value_type is bool or value_type is int:
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise _invalid("arguments")
        return value
    if value_type is str:
        return value
    if value_type is list:
        identity = id(value)
        if identity in budget.active:
            raise _invalid("arguments")
        budget.active.add(identity)
        result: list[object] = []
        for item in value:
            result.append(_copy_json_value(item, depth + 1, budget))
        budget.active.remove(identity)
        return result
    if value_type is dict:
        identity = id(value)
        if identity in budget.active:
            raise _invalid("arguments")
        budget.active.add(identity)
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _invalid("arguments")
            result[key] = _copy_json_value(item, depth + 1, budget)
        budget.active.remove(identity)
        return result
    raise _invalid("arguments")


def _serialize_action_arguments(arguments: object) -> str:
    if type(arguments) is not dict:
        raise _invalid("arguments")
    copied = _copy_json_value(arguments, 0, _JsonCopyBudget())
    try:
        serialized = json.dumps(
            copied,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        serialized_bytes = serialized.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, OverflowError, RecursionError):
        raise _invalid("arguments") from None
    if len(serialized_bytes) > MAX_MODEL_ARGUMENT_BYTES:
        raise _invalid("arguments")
    return serialized


def _deserialize_action_arguments(serialized: object) -> dict[str, object]:
    if type(serialized) is not str:
        raise _invalid("arguments")
    try:
        if len(serialized.encode("utf-8")) > MAX_MODEL_ARGUMENT_BYTES:
            raise _invalid("arguments")
        parsed = json.loads(serialized)
    except ModelAdapterError:
        raise
    except (TypeError, ValueError, UnicodeEncodeError, OverflowError, RecursionError):
        raise _invalid("arguments") from None
    if type(parsed) is not dict:
        raise _invalid("arguments")
    canonical = _serialize_action_arguments(parsed)
    if canonical != serialized:
        raise _invalid("arguments")
    return parsed


def _detach_action_arguments(arguments: object) -> dict[str, object]:
    return _deserialize_action_arguments(_serialize_action_arguments(arguments))


@dataclass(frozen=True)
class ActionDirective:
    name: ActionName
    arguments: dict[str, object]

    def __post_init__(self) -> None:
        _require_enum(self.name, ActionName, "name")
        detached = _detach_action_arguments(self.arguments)
        object.__setattr__(self, "arguments", detached)

    @property
    def kind(self) -> ModelDirectiveKind:
        return ModelDirectiveKind.ACTION


@dataclass(frozen=True)
class TransitionDirective:
    target_state: ControllerState
    reason: str

    def __post_init__(self) -> None:
        _require_enum(self.target_state, ControllerState, "target_state")
        _validate_text(self.reason, "reason", MAX_MODEL_REASON_BYTES)

    @property
    def kind(self) -> ModelDirectiveKind:
        return ModelDirectiveKind.TRANSITION


@dataclass(frozen=True)
class AddHypothesisDirective:
    hypothesis_id: str
    statement: str
    confidence: HypothesisConfidence
    evidence_refs: tuple[str, ...] = ()
    requires_runtime_evidence: bool = False

    def __post_init__(self) -> None:
        candidate = _validate_hypothesis_fields(
            self.hypothesis_id,
            self.statement,
            self.confidence,
            self.evidence_refs,
            self.requires_runtime_evidence,
        )
        object.__setattr__(self, "hypothesis_id", candidate.hypothesis_id)
        object.__setattr__(self, "statement", candidate.statement)
        object.__setattr__(self, "confidence", candidate.confidence)
        object.__setattr__(self, "evidence_refs", candidate.evidence_refs)
        object.__setattr__(
            self,
            "requires_runtime_evidence",
            candidate.requires_runtime_evidence,
        )

    @property
    def kind(self) -> ModelDirectiveKind:
        return ModelDirectiveKind.ADD_HYPOTHESIS


@dataclass(frozen=True)
class ReviseHypothesisDirective:
    hypothesis_id: str
    statement: str
    confidence: HypothesisConfidence
    evidence_refs: tuple[str, ...]
    requires_runtime_evidence: bool

    def __post_init__(self) -> None:
        candidate = _validate_hypothesis_fields(
            self.hypothesis_id,
            self.statement,
            self.confidence,
            self.evidence_refs,
            self.requires_runtime_evidence,
        )
        object.__setattr__(self, "hypothesis_id", candidate.hypothesis_id)
        object.__setattr__(self, "statement", candidate.statement)
        object.__setattr__(self, "confidence", candidate.confidence)
        object.__setattr__(self, "evidence_refs", candidate.evidence_refs)
        object.__setattr__(
            self,
            "requires_runtime_evidence",
            candidate.requires_runtime_evidence,
        )

    @property
    def kind(self) -> ModelDirectiveKind:
        return ModelDirectiveKind.REVISE_HYPOTHESIS


@dataclass(frozen=True)
class SetHypothesisStatusDirective:
    hypothesis_id: str
    status: HypothesisStatus

    def __post_init__(self) -> None:
        _validate_hypothesis_id(self.hypothesis_id)
        _require_enum(self.status, HypothesisStatus, "status")
        if self.status is HypothesisStatus.ACTIVE:
            raise _invalid("status")

    @property
    def kind(self) -> ModelDirectiveKind:
        return ModelDirectiveKind.SET_HYPOTHESIS_STATUS


ModelDirective: TypeAlias = (
    ActionDirective
    | TransitionDirective
    | AddHypothesisDirective
    | ReviseHypothesisDirective
    | SetHypothesisStatusDirective
)

_DIRECTIVE_TYPES = (
    ActionDirective,
    TransitionDirective,
    AddHypothesisDirective,
    ReviseHypothesisDirective,
    SetHypothesisStatusDirective,
)


def directive_kind(directive: ModelDirective) -> ModelDirectiveKind:
    canonical = _canonicalize_directive(directive)
    if type(canonical) is _CanonicalAction:
        return ModelDirectiveKind.ACTION
    if type(canonical) is _CanonicalTransition:
        return ModelDirectiveKind.TRANSITION
    if type(canonical) is _CanonicalHypothesis:
        if canonical.kind in {
            ModelDirectiveKind.ADD_HYPOTHESIS,
            ModelDirectiveKind.REVISE_HYPOTHESIS,
        }:
            return canonical.kind
        raise _invalid("directive")
    if type(canonical) is _CanonicalStatus:
        return ModelDirectiveKind.SET_HYPOTHESIS_STATUS
    raise _invalid("directive")


def _validate_budget_records(
    budget_limits: object,
    budget_state: object,
) -> None:
    _require_exact(budget_limits, ControllerBudgetLimits, "budget_limits")
    _require_exact(budget_state, ControllerBudgetState, "budget_state")
    try:
        limits = (
            budget_limits.max_patch_attempts,
            budget_limits.max_test_runs,
            budget_limits.max_pdb_observations,
            budget_limits.max_active_hypotheses,
            budget_limits.max_source_observations,
        )
        state = (
            budget_state.patch_attempts,
            budget_state.test_runs,
            budget_state.pdb_observations,
            budget_state.source_observations,
        )
    except AttributeError:
        raise _invalid("budget_state") from None
    if any(type(value) is not int for value in limits):
        raise _invalid("budget_limits")
    if any(type(value) is not int for value in state):
        raise _invalid("budget_state")
    if (
        limits[0] <= 0
        or limits[1] <= 0
        or limits[2] < 0
        or limits[3] <= 0
        or limits[4] <= 0
        or any(value < 0 for value in state)
    ):
        raise _invalid("budget_state")
    if (
        state[0] > limits[0]
        or state[1] > limits[1]
        or state[2] > limits[2]
        or state[3] > limits[4]
    ):
        raise _invalid("budget_state")


def _validate_hypothesis_ledger(
    ledger: object,
) -> tuple[RootCauseHypothesis, ...]:
    _require_exact(ledger, HypothesisLedger, "hypotheses")
    try:
        records = ledger.hypotheses
    except AttributeError:
        raise _invalid("hypotheses") from None
    if type(records) is not tuple:
        raise _invalid("hypotheses")
    seen: set[str] = set()
    active_records: list[RootCauseHypothesis] = []
    for record in records:
        if type(record) is not RootCauseHypothesis:
            raise _invalid("hypotheses")
        try:
            validated = RootCauseHypothesis(
                record.hypothesis_id,
                record.statement,
                record.confidence,
                record.status,
                record.evidence_refs,
                record.requires_runtime_evidence,
                record.revision,
            )
        except (AttributeError, ControllerPolicyError, TypeError, ValueError):
            raise _invalid("hypotheses") from None
        if validated.hypothesis_id in seen:
            raise _invalid("hypotheses")
        seen.add(validated.hypothesis_id)
        if validated.status is HypothesisStatus.ACTIVE:
            active_records.append(validated)
    return tuple(active_records)


def _validate_snapshot(
    snapshot: object,
) -> tuple[RootCauseHypothesis, ...]:
    _require_exact(snapshot, ControllerSnapshot, "snapshot")
    try:
        _validate_text(snapshot.run_id, "run_id", 256)
        _validate_text(snapshot.task_id, "task_id", 256)
        _require_enum(snapshot.state, ControllerState, "state")
        if type(snapshot.model_call_index) is not int or snapshot.model_call_index < 0:
            raise _invalid("model_call_index")
        if type(snapshot.candidate_applied) is not bool:
            raise _invalid("candidate_applied")
        _validate_budget_records(snapshot.budget_limits, snapshot.budget_state)
        active_records = _validate_hypothesis_ledger(snapshot.hypotheses)
        if len(active_records) > snapshot.budget_limits.max_active_hypotheses:
            raise _invalid("hypotheses")
        observation = snapshot.last_observation
        if observation is not None:
            _require_exact(observation, Observation, "last_observation")
            if type(observation.run_id) is not str or type(observation.task_id) is not str:
                raise _invalid("last_observation")
            if observation.run_id != snapshot.run_id or observation.task_id != snapshot.task_id:
                raise _invalid("last_observation")
        return active_records
    except ModelAdapterError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise _invalid("snapshot") from None


@dataclass(frozen=True)
class ControllerSnapshot:
    run_id: str
    task_id: str
    state: ControllerState
    model_call_index: int
    budget_limits: ControllerBudgetLimits
    budget_state: ControllerBudgetState
    hypotheses: HypothesisLedger
    last_observation: Observation | None = None
    # Authoritative lifecycle fact projected by the controller after the
    # PatchManager accepts/reverts a candidate.
    candidate_applied: bool = False

    def __post_init__(self) -> None:
        _validate_snapshot(self)
        if type(self.candidate_applied) is not bool:
            raise ModelAdapterError("invalid candidate_applied")

    @property
    def allowed_actions(self) -> tuple[ActionName, ...]:
        _validate_snapshot(self)
        allowed = allowed_actions_for_state(self.state)
        return tuple(action for action in ActionName if action in allowed)

    @property
    def active_hypotheses(self) -> tuple[RootCauseHypothesis, ...]:
        return _validate_snapshot(self)


class ModelAdapter(Protocol):
    @property
    def model_name(self) -> str:
        ...

    def next_directive(
        self,
        snapshot: ControllerSnapshot,
    ) -> ModelDirective:
        ...


@dataclass(frozen=True)
class _CanonicalAction:
    name: ActionName
    arguments_json: str


@dataclass(frozen=True)
class _CanonicalTransition:
    target_state: ControllerState
    reason: str


@dataclass(frozen=True)
class _CanonicalHypothesis:
    kind: ModelDirectiveKind
    hypothesis_id: str
    statement: str
    confidence: HypothesisConfidence
    evidence_refs: tuple[str, ...]
    requires_runtime_evidence: bool


@dataclass(frozen=True)
class _CanonicalStatus:
    hypothesis_id: str
    status: HypothesisStatus


_CanonicalDirective = (
    _CanonicalAction
    | _CanonicalTransition
    | _CanonicalHypothesis
    | _CanonicalStatus
)


@dataclass(frozen=True)
class _CanonicalStep:
    expected_state: ControllerState
    directive: _CanonicalDirective


def _canonicalize_directive_unchecked(directive: object) -> _CanonicalDirective:
    if type(directive) is ActionDirective:
        _require_enum(directive.name, ActionName, "name")
        return _CanonicalAction(
            directive.name,
            _serialize_action_arguments(directive.arguments),
        )
    if type(directive) is TransitionDirective:
        _require_enum(directive.target_state, ControllerState, "target_state")
        _validate_text(directive.reason, "reason", MAX_MODEL_REASON_BYTES)
        return _CanonicalTransition(directive.target_state, directive.reason)
    if type(directive) is AddHypothesisDirective:
        candidate = _validate_hypothesis_fields(
            directive.hypothesis_id,
            directive.statement,
            directive.confidence,
            directive.evidence_refs,
            directive.requires_runtime_evidence,
        )
        return _CanonicalHypothesis(
            ModelDirectiveKind.ADD_HYPOTHESIS,
            candidate.hypothesis_id,
            candidate.statement,
            candidate.confidence,
            candidate.evidence_refs,
            candidate.requires_runtime_evidence,
        )
    if type(directive) is ReviseHypothesisDirective:
        candidate = _validate_hypothesis_fields(
            directive.hypothesis_id,
            directive.statement,
            directive.confidence,
            directive.evidence_refs,
            directive.requires_runtime_evidence,
        )
        return _CanonicalHypothesis(
            ModelDirectiveKind.REVISE_HYPOTHESIS,
            candidate.hypothesis_id,
            candidate.statement,
            candidate.confidence,
            candidate.evidence_refs,
            candidate.requires_runtime_evidence,
        )
    if type(directive) is SetHypothesisStatusDirective:
        hypothesis_id = _validate_hypothesis_id(directive.hypothesis_id)
        _require_enum(directive.status, HypothesisStatus, "status")
        if directive.status is HypothesisStatus.ACTIVE:
            raise _invalid("status")
        return _CanonicalStatus(hypothesis_id, directive.status)
    raise _invalid("directive")


def _canonicalize_directive(directive: object) -> _CanonicalDirective:
    try:
        return _canonicalize_directive_unchecked(directive)
    except ModelAdapterError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise _invalid("directive") from None


def _canonicalize_step(step: object) -> _CanonicalStep:
    if type(step) is not ScriptedModelStep:
        raise _invalid("steps")
    try:
        _require_enum(step.expected_state, ControllerState, "expected_state")
        return _CanonicalStep(step.expected_state, _canonicalize_directive(step.directive))
    except ModelAdapterError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise _invalid("steps") from None


def _validated_canonical_step(step: object) -> _CanonicalStep:
    if type(step) is not _CanonicalStep:
        raise _invalid("steps")
    try:
        if type(step.expected_state) is not ControllerState:
            raise _invalid("expected_state")
        directive = step.directive
        if type(directive) is _CanonicalAction:
            if type(directive.name) is not ActionName:
                raise _invalid("directive")
            _deserialize_action_arguments(directive.arguments_json)
        elif type(directive) is _CanonicalTransition:
            _require_enum(directive.target_state, ControllerState, "target_state")
            _validate_text(directive.reason, "reason", MAX_MODEL_REASON_BYTES)
        elif type(directive) is _CanonicalHypothesis:
            _require_enum(directive.kind, ModelDirectiveKind, "directive")
            if directive.kind not in {
                ModelDirectiveKind.ADD_HYPOTHESIS,
                ModelDirectiveKind.REVISE_HYPOTHESIS,
            }:
                raise _invalid("directive")
            _validate_hypothesis_fields(
                directive.hypothesis_id,
                directive.statement,
                directive.confidence,
                directive.evidence_refs,
                directive.requires_runtime_evidence,
            )
        elif type(directive) is _CanonicalStatus:
            _validate_hypothesis_id(directive.hypothesis_id)
            _require_enum(directive.status, HypothesisStatus, "status")
            if directive.status is HypothesisStatus.ACTIVE:
                raise _invalid("status")
        else:
            raise _invalid("directive")
    except ModelAdapterError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise _invalid("steps") from None
    return step


def _directive_from_canonical(directive: object) -> ModelDirective:
    directive = _validated_canonical_step(
        _CanonicalStep(ControllerState.REPRODUCE, directive)
    ).directive
    if type(directive) is _CanonicalAction:
        return ActionDirective(
            directive.name,
            _deserialize_action_arguments(directive.arguments_json),
        )
    if type(directive) is _CanonicalTransition:
        return TransitionDirective(directive.target_state, directive.reason)
    if type(directive) is _CanonicalHypothesis:
        if directive.kind is ModelDirectiveKind.ADD_HYPOTHESIS:
            return AddHypothesisDirective(
                directive.hypothesis_id,
                directive.statement,
                directive.confidence,
                directive.evidence_refs,
                directive.requires_runtime_evidence,
            )
        if directive.kind is ModelDirectiveKind.REVISE_HYPOTHESIS:
            return ReviseHypothesisDirective(
                directive.hypothesis_id,
                directive.statement,
                directive.confidence,
                directive.evidence_refs,
                directive.requires_runtime_evidence,
            )
        raise _invalid("directive")
    if type(directive) is _CanonicalStatus:
        return SetHypothesisStatusDirective(directive.hypothesis_id, directive.status)
    raise _invalid("directive")


@dataclass(frozen=True)
class ScriptedModelStep:
    expected_state: ControllerState
    directive: ModelDirective

    def __post_init__(self) -> None:
        _require_enum(self.expected_state, ControllerState, "expected_state")
        if type(self.directive) not in _DIRECTIVE_TYPES:
            raise _invalid("directive")


def _validate_model_name(value: object) -> str:
    value = _validate_text(value, "model_name", MAX_MODEL_NAME_BYTES)
    if any(
        not (
            "a" <= char <= "z"
            or "A" <= char <= "Z"
            or "0" <= char <= "9"
            or char in ".-_/"
        )
        for char in value
    ):
        raise _invalid("model_name")
    return value


@dataclass(frozen=True)
class ScriptedModelAdapter:
    steps: tuple[ScriptedModelStep, ...]
    model_name: str = "scripted"
    _canonical_steps: tuple[_CanonicalStep, ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.steps) is not tuple or not self.steps:
            raise _invalid("steps")
        for step in self.steps:
            if type(step) is not ScriptedModelStep:
                raise _invalid("steps")
        _validate_model_name(self.model_name)
        canonical_steps = tuple(_canonicalize_step(step) for step in self.steps)
        object.__setattr__(self, "_canonical_steps", canonical_steps)

    def next_directive(
        self,
        snapshot: ControllerSnapshot,
    ) -> ModelDirective:
        _validate_snapshot(snapshot)
        try:
            canonical_steps = self._canonical_steps
        except AttributeError:
            raise _invalid("steps") from None
        if type(canonical_steps) is not tuple or not canonical_steps:
            raise _invalid("steps")
        index = snapshot.model_call_index
        if index >= len(canonical_steps):
            raise ModelScriptExhaustedError("model script exhausted")
        try:
            canonical_step = _validated_canonical_step(canonical_steps[index])
        except (IndexError, TypeError):
            raise _invalid("steps") from None
        if canonical_step.expected_state is not snapshot.state:
            raise ModelScriptMismatchError("model script state mismatch")
        try:
            return _directive_from_canonical(canonical_step.directive)
        except ModelAdapterError:
            raise
        except (AttributeError, TypeError, ValueError):
            raise _invalid("directive") from None


__all__ = [
    "MAX_MODEL_ARGUMENT_BYTES",
    "MAX_MODEL_JSON_DEPTH",
    "MAX_MODEL_JSON_NODES",
    "MAX_MODEL_REASON_BYTES",
    "MAX_MODEL_NAME_BYTES",
    "ModelAdapterError",
    "ModelScriptExhaustedError",
    "ModelScriptMismatchError",
    "ModelDirectiveKind",
    "ActionDirective",
    "TransitionDirective",
    "AddHypothesisDirective",
    "ReviseHypothesisDirective",
    "SetHypothesisStatusDirective",
    "ModelDirective",
    "directive_kind",
    "ControllerSnapshot",
    "ModelAdapter",
    "ScriptedModelStep",
    "ScriptedModelAdapter",
]
