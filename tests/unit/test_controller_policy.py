import pytest

from agentic_debugger.agent.controller_policy import (
    ActionName,
    BudgetExceededError,
    BudgetKind,
    ControllerBudgetLimits,
    ControllerBudgetState,
    ControllerPolicyError,
    HypothesisConfidence,
    HypothesisStatus,
    PdbGateContext,
    PdbGateReason,
    PdbPolicy,
    RootCauseHypothesis,
    allowed_actions_for_state,
    budget_kind_for_action,
    decide_pdb_access,
    is_action_allowed,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.evaluation.task_schema import Constraints


EXPECTED_ACTIONS = {
    "run_tests", "run_reproduction", "get_failure_trace", "search_code",
    "find_function", "find_class", "get_source_window", "extract_failing_test",
    "express_root_cause_hypothesis", "request_more_evidence", "start_pdb_session",
    "get_stack_summary", "get_frame", "get_frame_locals", "safe_eval_expression",
    "inspect_caller_frame", "discard_hypothesis", "stop_pdb_session", "apply_patch",
    "syntax_check", "revert_patch", "run_regression_tests", "classify_outcome",
}

EXPECTED_ALLOWLISTS = {
    ControllerState.REPRODUCE: {"run_tests", "run_reproduction", "get_failure_trace"},
    ControllerState.UNDERSTAND: {
        "search_code", "find_function", "find_class", "get_source_window",
        "extract_failing_test", "express_root_cause_hypothesis", "request_more_evidence",
    },
    ControllerState.RUNTIME_EVIDENCE: {
        "start_pdb_session", "get_stack_summary", "get_frame", "get_frame_locals",
        "get_source_window", "safe_eval_expression", "inspect_caller_frame",
        "discard_hypothesis", "request_more_evidence", "stop_pdb_session",
    },
    ControllerState.PATCH: {"apply_patch", "syntax_check", "revert_patch"},
    ControllerState.VALIDATE: {
        "run_reproduction", "run_regression_tests", "classify_outcome", "revert_patch",
    },
    ControllerState.DONE: set(),
    ControllerState.FAILED: set(),
}


def test_action_values_and_allowlists_are_exact_and_immutable():
    assert {action.value for action in ActionName} == EXPECTED_ACTIONS
    assert len(ActionName.__members__) == len(ActionName)
    for state, expected in EXPECTED_ALLOWLISTS.items():
        actual = allowed_actions_for_state(state)
        assert {action.value for action in actual} == expected
        with pytest.raises(AttributeError):
            actual.add(ActionName.RUN_TESTS)
    assert set().union(*(set(allowed_actions_for_state(s)) for s in ControllerState)) == set(ActionName)
    assert allowed_actions_for_state(ControllerState.UNDERSTAND) == allowed_actions_for_state(ControllerState.UNDERSTAND)


@pytest.mark.parametrize("state", list(ControllerState))
def test_unknown_actions_and_wrong_states_are_rejected(state):
    with pytest.raises(ControllerPolicyError):
        is_action_allowed(state, "run_tests")
    with pytest.raises(ControllerPolicyError):
        allowed_actions_for_state(state.value)


EXPECTED_BUDGETS = {
    "apply_patch": BudgetKind.PATCH_ATTEMPTS,
    "run_tests": BudgetKind.TEST_RUNS,
    "run_reproduction": BudgetKind.TEST_RUNS,
    "run_regression_tests": BudgetKind.TEST_RUNS,
    "get_failure_trace": BudgetKind.PDB_OBSERVATIONS,
    "get_stack_summary": BudgetKind.PDB_OBSERVATIONS,
    "get_frame": BudgetKind.PDB_OBSERVATIONS,
    "get_frame_locals": BudgetKind.PDB_OBSERVATIONS,
    "safe_eval_expression": BudgetKind.PDB_OBSERVATIONS,
    "inspect_caller_frame": BudgetKind.PDB_OBSERVATIONS,
    "search_code": BudgetKind.SOURCE_OBSERVATIONS,
    "find_function": BudgetKind.SOURCE_OBSERVATIONS,
    "find_class": BudgetKind.SOURCE_OBSERVATIONS,
    "get_source_window": BudgetKind.SOURCE_OBSERVATIONS,
    "extract_failing_test": BudgetKind.SOURCE_OBSERVATIONS,
}


@pytest.mark.parametrize("action", list(ActionName))
def test_every_action_has_exact_budget_classification(action):
    assert budget_kind_for_action(action) == EXPECTED_BUDGETS.get(action.value)


def test_budget_limits_and_task_constraint_import():
    limits = ControllerBudgetLimits(2, 4, 0)
    assert limits.max_active_hypotheses == 3
    assert limits.max_source_observations == 12
    constraints = Constraints(
        allowed_write_paths=["fixture"], denied_write_paths=["tests", "task.json"],
        network_allowed=False, external_services_allowed=False,
        max_patch_attempts=2, max_test_runs=4, max_pdb_observations=5,
    )
    assert ControllerBudgetLimits.from_task_constraints(constraints) == ControllerBudgetLimits(2, 4, 5)
    with pytest.raises(ControllerPolicyError):
        ControllerBudgetLimits(True, 1, 0)
    with pytest.raises(ControllerPolicyError):
        ControllerBudgetLimits(0, 1, 0)
    with pytest.raises(ControllerPolicyError):
        ControllerBudgetLimits(1, 1, -1)
    with pytest.raises(ControllerPolicyError):
        ControllerBudgetLimits(1, 1, 0, max_active_hypotheses=0)
    with pytest.raises(ControllerPolicyError):
        ControllerBudgetLimits(1, 1, 0, max_source_observations=0)


@pytest.mark.parametrize("amount", [0, -1, True])
def test_budget_amount_must_be_exact_positive_integer(amount):
    limits = ControllerBudgetLimits(1, 1, 1)
    with pytest.raises(ControllerPolicyError):
        ControllerBudgetState().consume(limits, BudgetKind.TEST_RUNS, amount)


def test_budget_consumption_is_copy_on_write_and_has_no_partial_failure():
    limits = ControllerBudgetLimits(2, 2, 1, max_source_observations=2)
    original = ControllerBudgetState()
    consumed = original.consume(limits, BudgetKind.TEST_RUNS, 2)
    assert original == ControllerBudgetState()
    assert consumed == ControllerBudgetState(test_runs=2)
    assert consumed.remaining(limits, BudgetKind.TEST_RUNS) == 0
    with pytest.raises(BudgetExceededError):
        consumed.consume(limits, BudgetKind.TEST_RUNS)
    assert consumed == ControllerBudgetState(test_runs=2)
    assert consumed.consume(limits, BudgetKind.SOURCE_OBSERVATIONS, 2) == ControllerBudgetState(test_runs=2, source_observations=2)
    with pytest.raises(ControllerPolicyError):
        original.remaining(limits, "test_runs")


def _hypothesis(confidence=HypothesisConfidence.LOW, *, runtime=False, status=HypothesisStatus.ACTIVE):
    return RootCauseHypothesis("h-1", "the value is missing", confidence, status, ("trace-1",), runtime, 1)


@pytest.mark.parametrize("value", ["", " h-1", "h-1 ", "h/1", "h\x00", "é"])
def test_hypothesis_id_validation(value):
    with pytest.raises(ControllerPolicyError):
        RootCauseHypothesis(value, "statement", HypothesisConfidence.LOW, HypothesisStatus.ACTIVE, (), False, 1)


def test_hypothesis_utf8_and_evidence_validation():
    with pytest.raises(ControllerPolicyError):
        RootCauseHypothesis("h-1", "é" * 2049, HypothesisConfidence.LOW, HypothesisStatus.ACTIVE, (), False, 1)
    with pytest.raises(ControllerPolicyError):
        RootCauseHypothesis("h-1", "bad\x7f", HypothesisConfidence.LOW, HypothesisStatus.ACTIVE, (), False, 1)
    with pytest.raises(ControllerPolicyError):
        RootCauseHypothesis("h-1", "statement", HypothesisConfidence.LOW, HypothesisStatus.ACTIVE, ["e"], False, 1)
    with pytest.raises(ControllerPolicyError):
        RootCauseHypothesis("h-1", "statement", HypothesisConfidence.LOW, HypothesisStatus.ACTIVE, ("e", "e"), False, 1)
    hypothesis = RootCauseHypothesis("h-1", "statement", HypothesisConfidence.LOW, HypothesisStatus.ACTIVE, ("é",), False, 1)
    assert hypothesis.evidence_refs == ("é",)
    with pytest.raises(ControllerPolicyError):
        RootCauseHypothesis("h-1", "statement", HypothesisConfidence.LOW, HypothesisStatus.ACTIVE, ("é" * 129,), False, 1)
    with pytest.raises(ControllerPolicyError):
        RootCauseHypothesis("h-1", "statement", HypothesisConfidence.LOW, HypothesisStatus.ACTIVE, (), False, True)


def test_pdb_gate_precedence_and_outcomes():
    hypothesis = _hypothesis()
    base = dict(failure_reproduced=True, remaining_pdb_observations=1, failed_patch_attempts=0, active_hypothesis=hypothesis)
    for state in (ControllerState.REPRODUCE, ControllerState.RUNTIME_EVIDENCE, ControllerState.PATCH, ControllerState.DONE, ControllerState.FAILED):
        decision = decide_pdb_access(PdbPolicy.ALWAYS_ON, PdbGateContext(state, **base))
        assert decision == (type(decision)(False, PdbGateReason.INVALID_SOURCE_STATE))
    context = PdbGateContext(ControllerState.UNDERSTAND, **base)
    assert decide_pdb_access(PdbPolicy.DISABLED, context).reason is PdbGateReason.POLICY_DISABLED
    assert decide_pdb_access(PdbPolicy.ALWAYS_ON, context).allowed
    assert decide_pdb_access(PdbPolicy.AFTER_FAILED_PATCH, context).reason is PdbGateReason.FAILED_PATCH_REQUIRED
    assert decide_pdb_access(PdbPolicy.ON_UNCERTAINTY, context).allowed
    assert decide_pdb_access(PdbPolicy.ON_UNCERTAINTY, PdbGateContext(ControllerState.UNDERSTAND, True, 1, 0, _hypothesis(HypothesisConfidence.MEDIUM))).reason is PdbGateReason.UNCERTAINTY_NOT_ESTABLISHED
    assert decide_pdb_access(PdbPolicy.ON_UNCERTAINTY, PdbGateContext(ControllerState.UNDERSTAND, True, 1, 0, _hypothesis(HypothesisConfidence.HIGH, runtime=True))).allowed
    assert decide_pdb_access(PdbPolicy.ALWAYS_ON, PdbGateContext(ControllerState.UNDERSTAND, False, 1, 0, hypothesis)).reason is PdbGateReason.FAILURE_NOT_REPRODUCED
    assert decide_pdb_access(PdbPolicy.ALWAYS_ON, PdbGateContext(ControllerState.UNDERSTAND, True, 0, 0, hypothesis)).reason is PdbGateReason.BUDGET_EXHAUSTED


def test_terminal_hypothesis_is_not_valid_pdb_context():
    with pytest.raises(ControllerPolicyError):
        PdbGateContext(ControllerState.UNDERSTAND, True, 1, 0, _hypothesis(status=HypothesisStatus.SUPPORTED))


@pytest.mark.parametrize(
    ("field", "limit_field", "kind"),
    [
        ("patch_attempts", "max_patch_attempts", BudgetKind.PATCH_ATTEMPTS),
        ("test_runs", "max_test_runs", BudgetKind.TEST_RUNS),
        ("pdb_observations", "max_pdb_observations", BudgetKind.PDB_OBSERVATIONS),
        ("source_observations", "max_source_observations", BudgetKind.SOURCE_OBSERVATIONS),
    ],
)
def test_all_budget_apis_reject_each_over_limit_counter(field, limit_field, kind):
    limits = ControllerBudgetLimits(2, 2, 1, max_source_observations=2)
    values = {"patch_attempts": 0, "test_runs": 0, "pdb_observations": 0, "source_observations": 0}
    values[field] = getattr(limits, limit_field) + 1
    state = ControllerBudgetState(**values)
    for operation in (
        lambda: state.remaining(limits, kind),
        lambda: state.can_consume(limits, kind),
        lambda: state.consume(limits, kind),
    ):
        with pytest.raises(ControllerPolicyError) as caught:
            operation()
        assert "budget state exceeds limits" == str(caught.value)
    assert state == ControllerBudgetState(**values)


def test_unrelated_over_limit_counter_invalidates_requested_budget_api():
    limits = ControllerBudgetLimits(1, 2, 1, max_source_observations=1)
    malformed = ControllerBudgetState(patch_attempts=2)
    for operation in (
        lambda: malformed.remaining(limits, BudgetKind.TEST_RUNS),
        lambda: malformed.can_consume(limits, BudgetKind.TEST_RUNS),
        lambda: malformed.consume(limits, BudgetKind.TEST_RUNS),
    ):
        with pytest.raises(ControllerPolicyError):
            operation()
    assert malformed == ControllerBudgetState(patch_attempts=2)


def test_exact_limits_and_requested_overrun_are_distinct():
    limits = ControllerBudgetLimits(1, 5, 0, max_source_observations=1)
    exact = ControllerBudgetState(test_runs=5)
    assert exact.remaining(limits, BudgetKind.TEST_RUNS) == 0
    assert not exact.can_consume(limits, BudgetKind.TEST_RUNS)
    with pytest.raises(BudgetExceededError):
        exact.consume(limits, BudgetKind.TEST_RUNS)
    almost = ControllerBudgetState(test_runs=4)
    result = almost.consume(limits, BudgetKind.TEST_RUNS)
    assert result.test_runs == 5
    assert result.remaining(limits, BudgetKind.TEST_RUNS) == 0
    with pytest.raises(BudgetExceededError):
        almost.consume(limits, BudgetKind.TEST_RUNS, 2)
    assert almost.test_runs == 4


def test_zero_pdb_limit_is_valid_only_with_zero_consumption():
    limits = ControllerBudgetLimits(1, 1, 0)
    valid = ControllerBudgetState()
    assert valid.remaining(limits, BudgetKind.PDB_OBSERVATIONS) == 0
    assert not valid.can_consume(limits, BudgetKind.PDB_OBSERVATIONS)
    malformed = ControllerBudgetState(pdb_observations=1)
    for operation in (
        lambda: malformed.remaining(limits, BudgetKind.PDB_OBSERVATIONS),
        lambda: malformed.can_consume(limits, BudgetKind.PDB_OBSERVATIONS),
        lambda: malformed.consume(limits, BudgetKind.PDB_OBSERVATIONS),
    ):
        with pytest.raises(ControllerPolicyError):
            operation()
