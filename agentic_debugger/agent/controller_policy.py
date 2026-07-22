"""Pure controller-policy contracts for the MVP controller.

This module deliberately contains policy and immutable value objects only.  It
does not execute actions, inspect a workspace, or create events.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final

from agentic_debugger.evaluation.task_schema import Constraints
from agentic_debugger.agent.state_machine import ControllerState


class ControllerPolicyError(ValueError):
    """Raised when a controller policy contract is invalid."""


class BudgetExceededError(ControllerPolicyError):
    """Raised when a budget consumption would exceed its configured limit."""


class ActionName(str, Enum):
    RUN_TESTS = "run_tests"
    RUN_REPRODUCTION = "run_reproduction"
    GET_FAILURE_TRACE = "get_failure_trace"

    SEARCH_CODE = "search_code"
    FIND_FUNCTION = "find_function"
    FIND_CLASS = "find_class"
    GET_SOURCE_WINDOW = "get_source_window"
    EXTRACT_FAILING_TEST = "extract_failing_test"
    EXPRESS_ROOT_CAUSE_HYPOTHESIS = "express_root_cause_hypothesis"
    REQUEST_MORE_EVIDENCE = "request_more_evidence"

    START_PDB_SESSION = "start_pdb_session"
    GET_STACK_SUMMARY = "get_stack_summary"
    GET_FRAME = "get_frame"
    GET_FRAME_LOCALS = "get_frame_locals"
    SAFE_EVAL_EXPRESSION = "safe_eval_expression"
    INSPECT_CALLER_FRAME = "inspect_caller_frame"
    DISCARD_HYPOTHESIS = "discard_hypothesis"
    STOP_PDB_SESSION = "stop_pdb_session"

    APPLY_PATCH = "apply_patch"
    SYNTAX_CHECK = "syntax_check"
    REVERT_PATCH = "revert_patch"

    RUN_REGRESSION_TESTS = "run_regression_tests"
    CLASSIFY_OUTCOME = "classify_outcome"


class BudgetKind(str, Enum):
    PATCH_ATTEMPTS = "patch_attempts"
    TEST_RUNS = "test_runs"
    PDB_OBSERVATIONS = "pdb_observations"
    SOURCE_OBSERVATIONS = "source_observations"


class HypothesisConfidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class HypothesisStatus(str, Enum):
    ACTIVE = "active"
    SUPPORTED = "supported"
    REJECTED = "rejected"
    DISCARDED = "discarded"


class PdbPolicy(str, Enum):
    DISABLED = "disabled"
    ALWAYS_ON = "always_on"
    AFTER_FAILED_PATCH = "after_failed_patch"
    ON_UNCERTAINTY = "on_uncertainty"


class PdbGateReason(str, Enum):
    ALLOWED = "allowed"
    INVALID_SOURCE_STATE = "invalid_source_state"
    FAILURE_NOT_REPRODUCED = "failure_not_reproduced"
    BUDGET_EXHAUSTED = "budget_exhausted"
    POLICY_DISABLED = "policy_disabled"
    FAILED_PATCH_REQUIRED = "failed_patch_required"
    ACTIVE_HYPOTHESIS_REQUIRED = "active_hypothesis_required"
    UNCERTAINTY_NOT_ESTABLISHED = "uncertainty_not_established"


def _invalid(field: str) -> ControllerPolicyError:
    return ControllerPolicyError(f"invalid {field}")


def _require_exact(value: object, expected: type, field: str) -> None:
    if type(value) is not expected:
        raise _invalid(field)


def _require_enum(value: object, enum_type: type[Enum], field: str) -> None:
    if type(value) is not enum_type:
        raise _invalid(field)


def _validate_non_negative_int(value: object, field: str) -> None:
    if type(value) is not int or value < 0:
        raise _invalid(field)


def _validate_positive_int(value: object, field: str) -> None:
    if type(value) is not int or value <= 0:
        raise _invalid(field)


def _validate_utf8_text(value: object, field: str, maximum_bytes: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _invalid(field)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _invalid(field) from exc
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise _invalid(field)
    if len(encoded) > maximum_bytes:
        raise _invalid(field)
    return value


def _validate_hypothesis_id(value: object) -> str:
    value = _validate_utf8_text(value, "hypothesis_id", 128)
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


def _validate_evidence_refs(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > 64:
        raise _invalid("evidence_refs")
    seen: set[str] = set()
    for reference in value:
        reference = _validate_utf8_text(reference, "evidence_ref", 256)
        if reference in seen:
            raise _invalid("evidence_refs")
        seen.add(reference)
    return value


_ALLOWED_ACTIONS_BY_STATE: Final = MappingProxyType(
    {
        ControllerState.REPRODUCE: frozenset(
            {
                ActionName.RUN_TESTS,
                ActionName.RUN_REPRODUCTION,
                ActionName.GET_FAILURE_TRACE,
            }
        ),
        ControllerState.UNDERSTAND: frozenset(
            {
                ActionName.SEARCH_CODE,
                ActionName.FIND_FUNCTION,
                ActionName.FIND_CLASS,
                ActionName.GET_SOURCE_WINDOW,
                ActionName.EXTRACT_FAILING_TEST,
                ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS,
                ActionName.REQUEST_MORE_EVIDENCE,
            }
        ),
        ControllerState.RUNTIME_EVIDENCE: frozenset(
            {
                ActionName.START_PDB_SESSION,
                ActionName.GET_STACK_SUMMARY,
                ActionName.GET_FRAME,
                ActionName.GET_FRAME_LOCALS,
                ActionName.GET_SOURCE_WINDOW,
                ActionName.SAFE_EVAL_EXPRESSION,
                ActionName.INSPECT_CALLER_FRAME,
                ActionName.DISCARD_HYPOTHESIS,
                ActionName.REQUEST_MORE_EVIDENCE,
                ActionName.STOP_PDB_SESSION,
            }
        ),
        ControllerState.PATCH: frozenset(
            {
                ActionName.APPLY_PATCH,
                ActionName.SYNTAX_CHECK,
                ActionName.REVERT_PATCH,
            }
        ),
        ControllerState.VALIDATE: frozenset(
            {
                ActionName.RUN_REPRODUCTION,
                ActionName.RUN_REGRESSION_TESTS,
                ActionName.CLASSIFY_OUTCOME,
                ActionName.REVERT_PATCH,
            }
        ),
        ControllerState.DONE: frozenset(),
        ControllerState.FAILED: frozenset(),
    }
)


def allowed_actions_for_state(
    state: ControllerState,
) -> frozenset[ActionName]:
    """Return the immutable action allowlist for an exact controller state."""

    _require_enum(state, ControllerState, "state")
    return _ALLOWED_ACTIONS_BY_STATE[state]


def is_action_allowed(
    state: ControllerState,
    action_name: ActionName,
) -> bool:
    """Return whether an exact action is allowed in an exact state."""

    _require_enum(state, ControllerState, "state")
    _require_enum(action_name, ActionName, "action_name")
    return action_name in _ALLOWED_ACTIONS_BY_STATE[state]


_BUDGET_KIND_BY_ACTION: Final = MappingProxyType(
    {
        ActionName.APPLY_PATCH: BudgetKind.PATCH_ATTEMPTS,
        ActionName.RUN_TESTS: BudgetKind.TEST_RUNS,
        ActionName.RUN_REPRODUCTION: BudgetKind.TEST_RUNS,
        ActionName.RUN_REGRESSION_TESTS: BudgetKind.TEST_RUNS,
        ActionName.GET_STACK_SUMMARY: BudgetKind.PDB_OBSERVATIONS,
        ActionName.GET_FRAME: BudgetKind.PDB_OBSERVATIONS,
        ActionName.GET_FRAME_LOCALS: BudgetKind.PDB_OBSERVATIONS,
        ActionName.SAFE_EVAL_EXPRESSION: BudgetKind.PDB_OBSERVATIONS,
        ActionName.INSPECT_CALLER_FRAME: BudgetKind.PDB_OBSERVATIONS,
        ActionName.SEARCH_CODE: BudgetKind.SOURCE_OBSERVATIONS,
        ActionName.FIND_FUNCTION: BudgetKind.SOURCE_OBSERVATIONS,
        ActionName.FIND_CLASS: BudgetKind.SOURCE_OBSERVATIONS,
        ActionName.GET_SOURCE_WINDOW: BudgetKind.SOURCE_OBSERVATIONS,
        ActionName.EXTRACT_FAILING_TEST: BudgetKind.SOURCE_OBSERVATIONS,
    }
)


def budget_kind_for_action(
    action_name: ActionName,
) -> BudgetKind | None:
    """Classify an action without consuming any budget."""

    _require_enum(action_name, ActionName, "action_name")
    return _BUDGET_KIND_BY_ACTION.get(action_name)


@dataclass(frozen=True)
class ControllerBudgetLimits:
    max_patch_attempts: int
    max_test_runs: int
    max_pdb_observations: int
    max_active_hypotheses: int = 3
    max_source_observations: int = 12

    def __post_init__(self) -> None:
        _validate_positive_int(self.max_patch_attempts, "max_patch_attempts")
        _validate_positive_int(self.max_test_runs, "max_test_runs")
        _validate_non_negative_int(
            self.max_pdb_observations, "max_pdb_observations"
        )
        _validate_positive_int(
            self.max_active_hypotheses, "max_active_hypotheses"
        )
        _validate_positive_int(
            self.max_source_observations, "max_source_observations"
        )

    @classmethod
    def from_task_constraints(
        cls,
        constraints: Constraints,
        *,
        max_active_hypotheses: int = 3,
        max_source_observations: int = 12,
    ) -> "ControllerBudgetLimits":
        _require_exact(constraints, Constraints, "constraints")
        try:
            return cls(
                max_patch_attempts=constraints.max_patch_attempts,
                max_test_runs=constraints.max_test_runs,
                max_pdb_observations=constraints.max_pdb_observations,
                max_active_hypotheses=max_active_hypotheses,
                max_source_observations=max_source_observations,
            )
        except ControllerPolicyError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise _invalid("constraints") from exc


@dataclass(frozen=True)
class ControllerBudgetState:
    patch_attempts: int = 0
    test_runs: int = 0
    pdb_observations: int = 0
    source_observations: int = 0

    def __post_init__(self) -> None:
        _validate_non_negative_int(self.patch_attempts, "patch_attempts")
        _validate_non_negative_int(self.test_runs, "test_runs")
        _validate_non_negative_int(self.pdb_observations, "pdb_observations")
        _validate_non_negative_int(self.source_observations, "source_observations")

    def consumed(self, kind: BudgetKind) -> int:
        _require_enum(kind, BudgetKind, "kind")
        if kind is BudgetKind.PATCH_ATTEMPTS:
            return self.patch_attempts
        if kind is BudgetKind.TEST_RUNS:
            return self.test_runs
        if kind is BudgetKind.PDB_OBSERVATIONS:
            return self.pdb_observations
        return self.source_observations

    def remaining(
        self,
        limits: ControllerBudgetLimits,
        kind: BudgetKind,
    ) -> int:
        _require_exact(limits, ControllerBudgetLimits, "limits")
        _require_enum(kind, BudgetKind, "kind")
        _validate_budget_state_against_limits(self, limits)
        limit = _limit_for_kind(limits, kind)
        return limit - self.consumed(kind)

    def can_consume(
        self,
        limits: ControllerBudgetLimits,
        kind: BudgetKind,
        amount: int = 1,
    ) -> bool:
        _require_exact(limits, ControllerBudgetLimits, "limits")
        _require_enum(kind, BudgetKind, "kind")
        _validate_budget_state_against_limits(self, limits)
        _validate_positive_int(amount, "amount")
        return amount <= self.remaining(limits, kind)

    def consume(
        self,
        limits: ControllerBudgetLimits,
        kind: BudgetKind,
        amount: int = 1,
    ) -> "ControllerBudgetState":
        _require_exact(limits, ControllerBudgetLimits, "limits")
        _require_enum(kind, BudgetKind, "kind")
        _validate_budget_state_against_limits(self, limits)
        _validate_positive_int(amount, "amount")
        if amount > self.remaining(limits, kind):
            raise BudgetExceededError("budget exceeded")

        if kind is BudgetKind.PATCH_ATTEMPTS:
            return ControllerBudgetState(
                patch_attempts=self.patch_attempts + amount,
                test_runs=self.test_runs,
                pdb_observations=self.pdb_observations,
                source_observations=self.source_observations,
            )
        if kind is BudgetKind.TEST_RUNS:
            return ControllerBudgetState(
                patch_attempts=self.patch_attempts,
                test_runs=self.test_runs + amount,
                pdb_observations=self.pdb_observations,
                source_observations=self.source_observations,
            )
        if kind is BudgetKind.PDB_OBSERVATIONS:
            return ControllerBudgetState(
                patch_attempts=self.patch_attempts,
                test_runs=self.test_runs,
                pdb_observations=self.pdb_observations + amount,
                source_observations=self.source_observations,
            )
        return ControllerBudgetState(
            patch_attempts=self.patch_attempts,
            test_runs=self.test_runs,
            pdb_observations=self.pdb_observations,
            source_observations=self.source_observations + amount,
        )


def _limit_for_kind(limits: ControllerBudgetLimits, kind: BudgetKind) -> int:
    if kind is BudgetKind.PATCH_ATTEMPTS:
        return limits.max_patch_attempts
    if kind is BudgetKind.TEST_RUNS:
        return limits.max_test_runs
    if kind is BudgetKind.PDB_OBSERVATIONS:
        return limits.max_pdb_observations
    return limits.max_source_observations


def _validate_budget_state_against_limits(
    state: ControllerBudgetState,
    limits: ControllerBudgetLimits,
) -> None:
    """Reject any state whose counters exceed any configured hard limit."""

    _require_exact(state, ControllerBudgetState, "state")
    _require_exact(limits, ControllerBudgetLimits, "limits")
    if (
        state.patch_attempts > limits.max_patch_attempts
        or state.test_runs > limits.max_test_runs
        or state.pdb_observations > limits.max_pdb_observations
        or state.source_observations > limits.max_source_observations
    ):
        raise ControllerPolicyError("budget state exceeds limits")


@dataclass(frozen=True)
class RootCauseHypothesis:
    hypothesis_id: str
    statement: str
    confidence: HypothesisConfidence
    status: HypothesisStatus
    evidence_refs: tuple[str, ...]
    requires_runtime_evidence: bool
    revision: int

    def __post_init__(self) -> None:
        _validate_hypothesis_id(self.hypothesis_id)
        _validate_utf8_text(self.statement, "statement", 4096)
        _require_enum(self.confidence, HypothesisConfidence, "confidence")
        _require_enum(self.status, HypothesisStatus, "status")
        _validate_evidence_refs(self.evidence_refs)
        if type(self.requires_runtime_evidence) is not bool:
            raise _invalid("requires_runtime_evidence")
        _validate_positive_int(self.revision, "revision")


@dataclass(frozen=True)
class HypothesisLedger:
    hypotheses: tuple[RootCauseHypothesis, ...] = ()

    def __post_init__(self) -> None:
        if type(self.hypotheses) is not tuple:
            raise _invalid("hypotheses")
        seen: set[str] = set()
        for hypothesis in self.hypotheses:
            if type(hypothesis) is not RootCauseHypothesis:
                raise _invalid("hypotheses")
            if hypothesis.hypothesis_id in seen:
                raise _invalid("hypotheses")
            seen.add(hypothesis.hypothesis_id)

    def active_hypotheses(self) -> tuple[RootCauseHypothesis, ...]:
        return tuple(
            hypothesis
            for hypothesis in self.hypotheses
            if hypothesis.status is HypothesisStatus.ACTIVE
        )

    def get(self, hypothesis_id: str) -> RootCauseHypothesis:
        if type(hypothesis_id) is not str:
            raise _invalid("hypothesis_id")
        for hypothesis in self.hypotheses:
            if hypothesis.hypothesis_id == hypothesis_id:
                return hypothesis
        raise ControllerPolicyError("hypothesis not found")

    def add(
        self,
        limits: ControllerBudgetLimits,
        *,
        hypothesis_id: str,
        statement: str,
        confidence: HypothesisConfidence,
        evidence_refs: tuple[str, ...] = (),
        requires_runtime_evidence: bool = False,
    ) -> "HypothesisLedger":
        _require_exact(limits, ControllerBudgetLimits, "limits")
        candidate = RootCauseHypothesis(
            hypothesis_id=hypothesis_id,
            statement=statement,
            confidence=confidence,
            status=HypothesisStatus.ACTIVE,
            evidence_refs=evidence_refs,
            requires_runtime_evidence=requires_runtime_evidence,
            revision=1,
        )
        if any(
            hypothesis.hypothesis_id == candidate.hypothesis_id
            for hypothesis in self.hypotheses
        ):
            raise ControllerPolicyError("hypothesis ID already used")
        if len(self.active_hypotheses()) >= limits.max_active_hypotheses:
            raise ControllerPolicyError("active hypothesis limit reached")
        return HypothesisLedger(self.hypotheses + (candidate,))

    def revise(
        self,
        hypothesis_id: str,
        *,
        statement: str,
        confidence: HypothesisConfidence,
        evidence_refs: tuple[str, ...],
        requires_runtime_evidence: bool,
    ) -> "HypothesisLedger":
        current = self.get(hypothesis_id)
        if current.status is not HypothesisStatus.ACTIVE:
            raise ControllerPolicyError("only active hypotheses may be revised")
        revised = RootCauseHypothesis(
            hypothesis_id=current.hypothesis_id,
            statement=statement,
            confidence=confidence,
            status=HypothesisStatus.ACTIVE,
            evidence_refs=evidence_refs,
            requires_runtime_evidence=requires_runtime_evidence,
            revision=current.revision + 1,
        )
        return HypothesisLedger(
            tuple(
                revised if hypothesis is current else hypothesis
                for hypothesis in self.hypotheses
            )
        )

    def transition(
        self,
        hypothesis_id: str,
        status: HypothesisStatus,
    ) -> "HypothesisLedger":
        _require_enum(status, HypothesisStatus, "status")
        current = self.get(hypothesis_id)
        if current.status is not HypothesisStatus.ACTIVE:
            raise ControllerPolicyError("terminal hypothesis cannot transition")
        if status is HypothesisStatus.ACTIVE:
            raise ControllerPolicyError("transition to active is not allowed")
        transitioned = RootCauseHypothesis(
            hypothesis_id=current.hypothesis_id,
            statement=current.statement,
            confidence=current.confidence,
            status=status,
            evidence_refs=current.evidence_refs,
            requires_runtime_evidence=current.requires_runtime_evidence,
            revision=current.revision,
        )
        return HypothesisLedger(
            tuple(
                transitioned if hypothesis is current else hypothesis
                for hypothesis in self.hypotheses
            )
        )


@dataclass(frozen=True)
class PdbGateContext:
    source_state: ControllerState
    failure_reproduced: bool
    remaining_pdb_observations: int
    failed_patch_attempts: int
    active_hypothesis: RootCauseHypothesis | None

    def __post_init__(self) -> None:
        _require_enum(self.source_state, ControllerState, "source_state")
        if type(self.failure_reproduced) is not bool:
            raise _invalid("failure_reproduced")
        _validate_non_negative_int(
            self.remaining_pdb_observations, "remaining_pdb_observations"
        )
        _validate_non_negative_int(
            self.failed_patch_attempts, "failed_patch_attempts"
        )
        if self.active_hypothesis is not None:
            if type(self.active_hypothesis) is not RootCauseHypothesis:
                raise _invalid("active_hypothesis")
            if self.active_hypothesis.status is not HypothesisStatus.ACTIVE:
                raise _invalid("active_hypothesis")


@dataclass(frozen=True)
class PdbGateDecision:
    allowed: bool
    reason: PdbGateReason

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise _invalid("allowed")
        _require_enum(self.reason, PdbGateReason, "reason")


def _deny(reason: PdbGateReason) -> PdbGateDecision:
    return PdbGateDecision(allowed=False, reason=reason)


def decide_pdb_access(
    policy: PdbPolicy,
    context: PdbGateContext,
) -> PdbGateDecision:
    """Apply the bounded, precedence-ordered PDB access policy."""

    _require_enum(policy, PdbPolicy, "policy")
    _require_exact(context, PdbGateContext, "context")
    if context.source_state not in {
        ControllerState.UNDERSTAND,
        ControllerState.VALIDATE,
    }:
        return _deny(PdbGateReason.INVALID_SOURCE_STATE)
    if not context.failure_reproduced:
        return _deny(PdbGateReason.FAILURE_NOT_REPRODUCED)
    if context.remaining_pdb_observations == 0:
        return _deny(PdbGateReason.BUDGET_EXHAUSTED)
    if policy is PdbPolicy.DISABLED:
        return _deny(PdbGateReason.POLICY_DISABLED)
    if policy is PdbPolicy.ALWAYS_ON:
        return PdbGateDecision(allowed=True, reason=PdbGateReason.ALLOWED)
    if policy is PdbPolicy.AFTER_FAILED_PATCH:
        if context.failed_patch_attempts < 1:
            return _deny(PdbGateReason.FAILED_PATCH_REQUIRED)
        return PdbGateDecision(allowed=True, reason=PdbGateReason.ALLOWED)

    hypothesis = context.active_hypothesis
    if hypothesis is None:
        return _deny(PdbGateReason.ACTIVE_HYPOTHESIS_REQUIRED)
    if (
        hypothesis.confidence is HypothesisConfidence.LOW
        or hypothesis.requires_runtime_evidence
    ):
        return PdbGateDecision(allowed=True, reason=PdbGateReason.ALLOWED)
    return _deny(PdbGateReason.UNCERTAINTY_NOT_ESTABLISHED)


__all__ = [
    "ActionName",
    "BudgetKind",
    "BudgetExceededError",
    "ControllerBudgetLimits",
    "ControllerBudgetState",
    "ControllerPolicyError",
    "HypothesisConfidence",
    "HypothesisLedger",
    "HypothesisStatus",
    "PdbGateContext",
    "PdbGateDecision",
    "PdbGateReason",
    "PdbPolicy",
    "RootCauseHypothesis",
    "allowed_actions_for_state",
    "budget_kind_for_action",
    "decide_pdb_access",
    "is_action_allowed",
]
