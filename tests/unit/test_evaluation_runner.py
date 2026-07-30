from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentic_debugger.evaluation import load_task as package_load_task
from agentic_debugger.evaluation.evaluator import EvaluationRunner, EvaluationVerifier, load_task as evaluator_load_task
from agentic_debugger.evaluation.outcome_taxonomy import OutcomeInputError, SemanticOutcome, classify_outcome
from agentic_debugger.evaluation.runner import (
    EvaluationInputError,
    EvaluationStatus,
    ExecutionBoundary,
    PytestCounts,
    LifecycleStatus,
    SyntaxFileRecord,
    TestRecord,
    TestRecordStatus,
    normalize_output,
)
from agentic_debugger.runtime.command_runner import CommandResult
from agentic_debugger.runtime.test_runner import TestRunKind, TestRunResult
from agentic_debugger.runtime.exceptions import WorkspaceError
import agentic_debugger.evaluation.verifier as verifier_module
from agentic_debugger.evaluation.verifier import _parse_collected_nodes, _parse_counts, _record_status, _tree_hash


FIXTURE = "agentic_debugger/datasets/curated/curated-none-handling-001/task.json"


class FakeWorkspace:
    root = r"C:\tmp\task_workspace_fake"

    def cleanup(self) -> None:
        return None


class ScriptedRunner:
    def __init__(self, workspace, *, nodes, post_f2p, post_p2p, full_error=False, f2p_count=1):
        self.nodes = tuple(nodes)
        self.post_f2p = list(post_f2p)
        self.post_p2p = list(post_p2p)
        self.full_error = full_error
        self.f2p_count = f2p_count
        self.reproduction_calls = 0
        self.post_f2p_index = 0
        self.post_p2p_index = 0

    def result(self, argv, code, stdout, *, reproduction_match=None, kind=TestRunKind.SELECTED):
        command = CommandResult(list(argv), ".", code, False, 0, stdout, "", False, False)
        return TestRunResult(command, kind, code == 0, reproduction_match, False, False)

    def run_reproduction(self, task):
        self.reproduction_calls += 1
        if self.reproduction_calls == 1:
            return self.result(task.reproduction.argv, 1, "1 failed in 0.01s\n", reproduction_match=True)
        return self.result(task.reproduction.argv, 0, "1 passed in 0.01s\n", reproduction_match=False)

    def run_tests(self, argv, cwd, timeout_seconds, kind=TestRunKind.SELECTED):
        if "--collect-only" in argv:
            return self.result(argv, 0, "\n".join(self.nodes) + f"\n{len(self.nodes)} tests collected in 0.01s\n")
        node = next((item for item in argv if "::" in item), "")
        if self.reproduction_calls == 1:
            code = 1 if node in self.nodes[:self.f2p_count] else 0
            return self.result(argv, code, f"{node} {'FAILED' if code else 'PASSED'}\n" + ("1 failed in 0.01s\n" if code else "1 passed in 0.01s\n"))
        if self.post_f2p_index < len(self.post_f2p):
            passed = self.post_f2p[self.post_f2p_index]
            self.post_f2p_index += 1
        else:
            passed = self.post_p2p[self.post_p2p_index]
            self.post_p2p_index += 1
        return self.result(argv, 0 if passed else 1, f"{node} {'PASSED' if passed else 'FAILED'}\n" + ("1 passed in 0.01s\n" if passed else "1 failed in 0.01s\n"))

    def run_full_suite(self, task):
        if self.full_error:
            return self.result(task.tests.full_suite_argv, 1, "2 passed, 1 failed in 0.01s\n", kind=TestRunKind.FULL_SUITE)
        passed = sum(self.post_f2p) + sum(self.post_p2p)
        failed = len(self.post_f2p) + len(self.post_p2p) - passed
        return self.result(task.tests.full_suite_argv, 0 if failed == 0 else 1, f"{passed} passed" + (f", {failed} failed" if failed else "") + " in 0.01s\n", kind=TestRunKind.FULL_SUITE)


def verifier(post_f2p, post_p2p, *, full_error=False):
    task = package_load_task(FIXTURE)
    nodes = tuple(task.tests.fail_to_pass) + tuple(task.tests.pass_to_pass)
    runner = EvaluationVerifier(
        ".",
        workspace_factory=lambda path, **kwargs: FakeWorkspace(),
        test_runner_factory=lambda workspace: ScriptedRunner(workspace, nodes=nodes, post_f2p=post_f2p, post_p2p=post_p2p, full_error=full_error, f2p_count=len(task.tests.fail_to_pass)),
    )
    return runner, task


def test_exact_outcome_matrix_and_invalid_vectors():
    assert classify_outcome([True], [True]) is SemanticOutcome.RESOLVED
    assert classify_outcome([True], [False]) is SemanticOutcome.BREAKING_RESOLVED
    assert classify_outcome([True, False], [True]) is SemanticOutcome.PARTIALLY_RESOLVED
    assert classify_outcome([True, False], [False]) is SemanticOutcome.WORK_IN_PROGRESS
    assert classify_outcome([False], [True]) is SemanticOutcome.NO_OP
    assert classify_outcome([False], [False]) is SemanticOutcome.REGRESSION
    with pytest.raises(OutcomeInputError):
        classify_outcome([], [True])
    with pytest.raises(OutcomeInputError):
        classify_outcome([1], [True])


def test_records_and_mappings_are_detached_and_immutable():
    argv = ["python", "-m", "pytest"]
    record = TestRecord("tests/test.py::test_x", "F2P", argv, ".", 3, 0, TestRecordStatus.PASS, False, "", "", False, False, PytestCounts(passed=1))
    argv.append("mutated")
    assert record.argv == ("python", "-m", "pytest")
    mapping = record.to_mapping()
    mapping["argv"].append("changed")
    assert record.argv == ("python", "-m", "pytest")



def test_direct_debug_task_uses_complete_schema_validation_and_detachment():
    base = package_load_task(FIXTURE)
    malformed = [
        replace(base, tests=replace(base.tests, fail_to_pass=[base.tests.fail_to_pass[0], base.tests.fail_to_pass[0]])),
        replace(base, tests=replace(base.tests, pass_to_pass=[base.tests.pass_to_pass[0], base.tests.pass_to_pass[0]])),
        replace(base, reproduction=replace(base.reproduction, argv=[])),
        replace(base, reproduction=replace(base.reproduction, timeout_seconds=0)),
        replace(base, constraints=replace(base.constraints, max_test_runs=0)),
        replace(base, constraints=replace(base.constraints, allowed_write_paths=["../outside.py"])),
        replace(base, tests=replace(base.tests, pass_to_pass=[base.tests.fail_to_pass[0], base.tests.pass_to_pass[1]])),
    ]
    for task in malformed:
        with pytest.raises(EvaluationInputError):
            evaluator_load_task(task)
    loaded = evaluator_load_task(base)
    base.tests.fail_to_pass.append("tests/changed_after_load.py::test_changed")
    base.reproduction.argv.append("mutated")
    assert loaded.tests.fail_to_pass == ["tests/test_display_name.py::test_missing_display_name_returns_fallback"]
    assert "mutated" not in loaded.reproduction.argv


def test_paths_outputs_and_semantic_mapping_are_portable():
    text = normalize_output(r"C:\tmp\task_workspace_abcd\trace", r"C:\tmp\task_workspace_abcd")
    assert text == "<WORKSPACE>\\trace"
    runner, task = verifier([False], [True, True])
    first = runner.evaluate(task, "")
    second = runner.evaluate(task, "")
    assert first.semantic_mapping() == second.semantic_mapping()
    assert first.boundary is ExecutionBoundary.TRUSTED_LOCAL_WORKSPACE
    assert "task_workspace_" not in json.dumps(first.semantic_mapping())


def test_loader_export_identity_and_noop_result():
    assert package_load_task is evaluator_load_task
    assert EvaluationRunner is EvaluationVerifier
    runner, task = verifier([False], [True, True])
    result = runner.evaluate(task, "")
    assert result.status is EvaluationStatus.COMPLETED
    assert result.outcome is SemanticOutcome.NO_OP
    assert result.workspace.lifecycle.value == "CLEANED"
    assert result.workspace.canonical_fixture_unchanged


def test_verification_commands_do_not_consume_controller_budget():
    runner, task = verifier([False], [True, True])
    result = runner.evaluate(task, "")
    assert result.status is EvaluationStatus.COMPLETED
    assert result.task_max_test_runs == 5
    assert result.verification_command_count == 10
    assert result.verification_selected_test_count == 11


def test_full_suite_contradiction_has_no_outcome():
    runner, task = verifier([True], [True, True], full_error=True)
    result = runner.evaluate(task, "")
    assert result.status is EvaluationStatus.FULL_SUITE_CONTRADICTION
    assert result.outcome is None







def test_collection_parser_preserves_duplicates_and_order():
    output = "tests/a.py::test_one\n tests/a.py::test_one[param]\n tests/a.py::test_two\n"
    assert _parse_collected_nodes(output) == ["tests/a.py::test_one", "tests/a.py::test_one[param]", "tests/a.py::test_two"]


def test_collection_counts_and_cross_line_summary_are_exact():
    counts = _parse_counts("FAILED tests/a.py::test_x - assert 0 == 3\n1 failed, 2 passed in 0.01s\n")
    assert counts is not None
    assert counts.failed == 1
    assert counts.passed == 2


def test_nested_record_validation_rejects_hostile_values():
    with pytest.raises(EvaluationInputError):
        TestRecord("tests/test.py::test_x", "F2P", ["pytest"], ".", 3, 0, TestRecordStatus.PASS, False, "", "", False, False, {"passed": 1})


def test_partial_evidence_survives_malformed_later_node():
    task = package_load_task(FIXTURE)

    class MalformedRunner(ScriptedRunner):
        def run_tests(self, argv, cwd, timeout_seconds, kind=TestRunKind.SELECTED):
            if self.reproduction_calls > 1 and "tests/test_display_name.py::test_whitespace_is_normalized" in argv:
                return object()
            return super().run_tests(argv, cwd, timeout_seconds, kind=kind)

    nodes = tuple(task.tests.fail_to_pass + task.tests.pass_to_pass)
    evaluator = EvaluationVerifier(
        ".",
        workspace_factory=lambda path, **kwargs: FakeWorkspace(),
        test_runner_factory=lambda workspace: MalformedRunner(workspace, nodes=nodes, post_f2p=[True], post_p2p=[True, True]),
    )
    result = evaluator.evaluate(task, "")
    assert result.status is EvaluationStatus.EVALUATOR_INVARIANT_FAILED
    assert result.outcome is None
    assert len(result.post_patch_f2p) == 1
    assert len(result.post_patch_p2p) == 1


def test_preparation_failure_has_single_not_attempted_lifecycle():
    calls = []
    def failing_factory(path, **kwargs):
        calls.append(path)
        raise WorkspaceError("cannot prepare")
    result = EvaluationVerifier(".", workspace_factory=failing_factory).evaluate(package_load_task(FIXTURE), "")
    assert result.status is EvaluationStatus.WORKSPACE_PREPARATION_FAILED
    assert result.workspace.lifecycle.value == "NOT_ATTEMPTED"
    assert not result.workspace.cleanup_attempted
    assert len(calls) == 1


def test_cleanup_failure_is_reported_once_after_preparation():
    class CleanupFailureWorkspace(FakeWorkspace):
        cleanup_calls = 0
        def cleanup(self):
            type(self).cleanup_calls += 1
            raise RuntimeError("cleanup failed")
    runner, task = verifier([False], [True, True])
    evaluator = EvaluationVerifier(".", workspace_factory=lambda path, **kwargs: CleanupFailureWorkspace(), test_runner_factory=lambda workspace: ScriptedRunner(workspace, nodes=tuple(task.tests.fail_to_pass + task.tests.pass_to_pass), post_f2p=[False], post_p2p=[True, True]))
    result = evaluator.evaluate(task, "")
    assert result.status is EvaluationStatus.CLEANUP_FAILED
    assert result.workspace.lifecycle.value == "CLEANUP_FAILED"
    assert CleanupFailureWorkspace.cleanup_calls == 1


def test_control_flow_exceptions_cross_public_boundary():
    class InterruptRunner(ScriptedRunner):
        def run_tests(self, argv, cwd, timeout_seconds, kind=TestRunKind.SELECTED):
            raise KeyboardInterrupt()
    task = package_load_task(FIXTURE)
    nodes = tuple(task.tests.fail_to_pass) + tuple(task.tests.pass_to_pass)
    evaluator = EvaluationVerifier(".", workspace_factory=lambda path, **kwargs: FakeWorkspace(), test_runner_factory=lambda workspace: InterruptRunner(workspace, nodes=nodes, post_f2p=[False], post_p2p=[True, True]))
    with pytest.raises(KeyboardInterrupt):
        evaluator.evaluate(task, "")


@pytest.mark.parametrize(
    "node_id",
    [
        "tests/test_x.py::test_case[param]",
        "tests/test_x.py::test_case[param-with-dashes]",
        "tests/test_x.py::test_case[value::nested]",
        "tests/test_x.py::test_case[value with spaces]",
    ],
)
def test_collection_parser_preserves_parameterized_node_ids(node_id):
    assert _parse_collected_nodes(node_id + "\n") == [node_id]


def test_tree_hash_ignores_only_generated_pycache_bytecode(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "generated" / "subdir" / "__pycache__").mkdir(parents=True)
    (tmp_path / "other-cache").mkdir()
    (tmp_path / "__pycache__" / "module.cpython-311.pyc").write_bytes(b"cache")
    (tmp_path / "generated" / "subdir" / "__pycache__" / "module.pyc").write_bytes(b"cache")
    baseline = _tree_hash(str(tmp_path))
    (tmp_path / "__pycache__" / "module.cpython-311.pyc").write_bytes(b"changed")
    (tmp_path / "generated" / "subdir" / "__pycache__" / "module.pyc").write_bytes(b"changed")
    assert _tree_hash(str(tmp_path)) == baseline
    (tmp_path / "extra.pyc").write_bytes(b"visible")
    assert _tree_hash(str(tmp_path)) != baseline
    after_extra = _tree_hash(str(tmp_path))
    (tmp_path / "other-cache" / "module.pyc").write_bytes(b"visible")
    assert _tree_hash(str(tmp_path)) != after_extra


def test_full_suite_exit_code_and_summary_mismatches_are_errors():
    task_runner = verifier([False], [True, True])[0]
    task = package_load_task(FIXTURE)
    runner = task_runner
    base = ScriptedRunner(None, nodes=tuple(task.tests.fail_to_pass + task.tests.pass_to_pass), post_f2p=[False], post_p2p=[True, True])
    raw = base.result(task.tests.full_suite_argv, 0, "1 failed in 0.01s\n", kind=TestRunKind.FULL_SUITE)
    status, error = _record_status(raw, _parse_counts(raw.command_result.stdout), full=True)
    assert status is TestRecordStatus.ERROR and error
    raw = base.result(task.tests.full_suite_argv, 1, "1 passed in 0.01s\n", kind=TestRunKind.FULL_SUITE)
    status, error = _record_status(raw, _parse_counts(raw.command_result.stdout), full=True)
    assert status is TestRecordStatus.ERROR and error


def test_canonical_hash_failures_are_bounded_and_cleanup_is_preserved(monkeypatch):
    task = package_load_task(FIXTURE)
    calls = {"count": 0}
    def fail_hash(path):
        calls["count"] += 1
        raise OSError("hash unavailable")
    monkeypatch.setattr(verifier_module, "_tree_hash", fail_hash)
    evaluator = EvaluationVerifier(".", workspace_factory=lambda path, **kwargs: FakeWorkspace())
    result = evaluator.evaluate(task, "")
    assert result.status is EvaluationStatus.EVALUATOR_INVARIANT_FAILED
    assert result.outcome is None
    assert result.workspace.lifecycle.value == "NOT_ATTEMPTED"
    assert not result.workspace.cleanup_attempted
    assert result.workspace.canonical_fixture_unchanged is False
    assert calls["count"] == 1


def test_final_canonical_hash_failure_is_bounded_after_cleanup(monkeypatch):
    task = package_load_task(FIXTURE)
    values = iter(["before", OSError("final hash unavailable")])
    monkeypatch.setattr(verifier_module, "_tree_hash", lambda path: (lambda value: (_ for _ in ()).throw(value) if isinstance(value, Exception) else value)(next(values)))
    calls = []
    class CleanupWorkspace(FakeWorkspace):
        def cleanup(self):
            calls.append("cleanup")
    runner, _ = verifier([False], [True, True])
    evaluator = EvaluationVerifier(".", workspace_factory=lambda path, **kwargs: CleanupWorkspace(), test_runner_factory=lambda workspace: ScriptedRunner(workspace, nodes=tuple(task.tests.fail_to_pass + task.tests.pass_to_pass), post_f2p=[False], post_p2p=[True, True]))
    result = evaluator.evaluate(task, "")
    assert result.status is EvaluationStatus.EVALUATOR_INVARIANT_FAILED
    assert result.outcome is None
    assert calls == ["cleanup"]
    assert result.workspace.cleanup_attempted
    assert result.workspace.canonical_fixture_unchanged is False



def test_counts_ignore_non_summary_trace_text_and_require_pytest_summary():
    counts = _parse_counts("trace: 1 failed\n1 passed in 0.01s\n")
    assert counts is not None
    assert counts.passed == 1
    assert counts.failed == 0
    assert _parse_counts("trace: 1 failed\n") is None


def test_selected_infrastructure_exit_code_is_not_assertion_failure():
    task = package_load_task(FIXTURE)
    base = ScriptedRunner(None, nodes=tuple(task.tests.fail_to_pass + task.tests.pass_to_pass), post_f2p=[False], post_p2p=[True, True])
    raw = base.result(["python", "-m", "pytest"], 2, "tests/x.py::test_x FAILED\n1 failed in 0.01s\n")
    status, error = _record_status(raw, _parse_counts(raw.command_result.stdout), full=False)
    assert status is TestRecordStatus.ERROR
    assert error



def _timeout_result(argv, kind):
    command = CommandResult(list(argv), ".", None, True, 0, "", "", False, False)
    return TestRunResult(command, kind, False, None, True, False)


@pytest.mark.parametrize("mode", ["reproduction", "f2p", "p2p", "post"])
def test_timeout_boundaries_have_no_semantic_outcome(mode):
    task = package_load_task(FIXTURE)
    nodes = tuple(task.tests.fail_to_pass + task.tests.pass_to_pass)
    class TimeoutRunner(ScriptedRunner):
        def run_reproduction(self, current_task):
            if mode == "reproduction":
                self.reproduction_calls += 1
                return _timeout_result(current_task.reproduction.argv, TestRunKind.REPRODUCTION)
            return super().run_reproduction(current_task)
        def run_tests(self, argv, cwd, timeout_seconds, kind=TestRunKind.SELECTED):
            node = next((item for item in argv if "::" in item), "")
            baseline = self.reproduction_calls == 1
            if mode == "f2p" and baseline and node == nodes[0]:
                return _timeout_result(argv, kind)
            if mode == "p2p" and baseline and node == nodes[1]:
                return _timeout_result(argv, kind)
            if mode == "post" and "--collect-only" not in argv and not baseline and kind is TestRunKind.SELECTED:
                return _timeout_result(argv, kind)
            return super().run_tests(argv, cwd, timeout_seconds, kind=kind)
    evaluator = EvaluationVerifier(
        ".",
        workspace_factory=lambda path, **kwargs: FakeWorkspace(),
        test_runner_factory=lambda workspace: TimeoutRunner(workspace, nodes=nodes, post_f2p=[False], post_p2p=[True, True]),
    )
    result = evaluator.evaluate(task, "")
    assert result.status is EvaluationStatus.TEST_TIMEOUT
    assert result.outcome is None
    assert result.timeout
    if mode == "reproduction":
        assert result.baseline.reproduction is not None
        assert result.baseline.reproduction.status is TestRecordStatus.TIMEOUT
    elif mode in {"f2p", "p2p"}:
        records = result.baseline.f2p + result.baseline.p2p
        assert any(record.status is TestRecordStatus.TIMEOUT for record in records)
    else:
        assert any(record.status is TestRecordStatus.TIMEOUT for record in result.post_patch_f2p + result.post_patch_p2p)


def test_baseline_p2p_genuine_failure_is_baseline_invalid():
    task = package_load_task(FIXTURE)
    nodes = tuple(task.tests.fail_to_pass + task.tests.pass_to_pass)
    evaluator = EvaluationVerifier(
        ".",
        workspace_factory=lambda path, **kwargs: FakeWorkspace(),
        test_runner_factory=lambda workspace: ScriptedRunner(workspace, nodes=nodes, post_f2p=[False], post_p2p=[True, True], f2p_count=2),
    )
    result = evaluator.evaluate(task, "")
    assert result.status is EvaluationStatus.BASELINE_INVALID
    assert result.baseline.reason == "baseline_p2p_not_genuine_pass"
    assert result.baseline.p2p[0].status is TestRecordStatus.FAIL
    assert result.outcome is None



def _completed_result(post_f2p, post_p2p):
    evaluator, task = verifier(post_f2p, post_p2p)
    result = evaluator.evaluate(task, "")
    assert result.status is EvaluationStatus.COMPLETED
    return result


@pytest.mark.parametrize(
    "mutate",
    [
        lambda result: replace(result, stop_reason="not_completed"),
        lambda result: replace(result, timeout=True),
        lambda result: replace(result, candidate_patch_attempt_count=0),
        lambda result: replace(result, workspace=replace(result.workspace, lifecycle=LifecycleStatus.PREPARED)),
        lambda result: replace(result, workspace=replace(result.workspace, prepared=False)),
        lambda result: replace(result, workspace=replace(result.workspace, cleanup_attempted=False)),
        lambda result: replace(result, workspace=replace(result.workspace, cleaned=False)),
        lambda result: replace(result, workspace=replace(result.workspace, canonical_fixture_unchanged=False)),
        lambda result: replace(result, workspace=replace(result.workspace, error="cleanup")),
        lambda result: replace(result, baseline=replace(result.baseline, valid=False)),
        lambda result: replace(result, baseline=replace(result.baseline, collection=None)),
        lambda result: replace(result, baseline=replace(result.baseline, collection=replace(result.baseline.collection, passed=False))),
        lambda result: replace(result, baseline=replace(result.baseline, collection=replace(result.baseline.collection, error="collection"))),
        lambda result: replace(result, baseline=replace(result.baseline, reproduction=None)),
        lambda result: replace(result, baseline=replace(result.baseline, reproduction=replace(result.baseline.reproduction, status=TestRecordStatus.ERROR))),
        lambda result: replace(result, baseline=replace(result.baseline, reproduction=replace(result.baseline.reproduction, status=TestRecordStatus.TIMEOUT))),
        lambda result: replace(result, baseline=replace(result.baseline, reproduction=replace(result.baseline.reproduction, reproduction_match=False))),
        lambda result: replace(result, baseline=replace(result.baseline, f2p=(replace(result.baseline.f2p[0], status=TestRecordStatus.PASS),))),
        lambda result: replace(result, baseline=replace(result.baseline, p2p=(replace(result.baseline.p2p[0], status=TestRecordStatus.FAIL),) + result.baseline.p2p[1:])),
        lambda result: replace(result, patch_application=replace(result.patch_application, attempted=False)),
        lambda result: replace(result, patch_application=replace(result.patch_application, success=False)),
        lambda result: replace(result, patch_application=replace(result.patch_application, error="patch")),
        lambda result: replace(result, syntax=replace(result.syntax, passed=False)),
        lambda result: replace(result, syntax=replace(result.syntax, results=(SyntaxFileRecord("module.py", False, "SyntaxError", "bad", 1, 1),))),
        lambda result: replace(result, syntax=replace(result.syntax, error="syntax")),
        lambda result: replace(result, post_patch_reproduction=replace(result.post_patch_reproduction, status=TestRecordStatus.ERROR)),
        lambda result: replace(result, post_patch_reproduction=replace(result.post_patch_reproduction, status=TestRecordStatus.TIMEOUT)),
        lambda result: replace(result, post_patch_reproduction=replace(result.post_patch_reproduction, parse_error="parse")),
        lambda result: replace(result, post_patch_f2p=(replace(result.post_patch_f2p[0], status=TestRecordStatus.ERROR),)),
        lambda result: replace(result, post_patch_f2p=(replace(result.post_patch_f2p[0], status=TestRecordStatus.TIMEOUT),)),
        lambda result: replace(result, post_patch_f2p=(replace(result.post_patch_f2p[0], parse_error="parse"),)),
        lambda result: replace(result, full_suite=replace(result.full_suite, status=TestRecordStatus.ERROR)),
        lambda result: replace(result, full_suite=replace(result.full_suite, status=TestRecordStatus.TIMEOUT)),
        lambda result: replace(result, full_suite=replace(result.full_suite, parse_error="parse")),
        lambda result: replace(result, full_suite=replace(result.full_suite, counts=PytestCounts(passed=2))),
        lambda result: replace(result, full_suite=replace(result.full_suite, counts=PytestCounts(passed=2, failed=1))),
        lambda result: replace(result, full_suite=replace(result.full_suite, counts=PytestCounts(passed=3, failed=1))),
        lambda result: replace(result, outcome=SemanticOutcome.NO_OP),
    ],
)
def test_completed_result_rejects_every_contradictory_state(mutate):
    result = _completed_result([True], [True, True])
    with pytest.raises(EvaluationInputError):
        mutate(result)


@pytest.mark.parametrize(
    ("post_f2p", "post_p2p", "outcome", "full_status"),
    [
        ([True], [True, True], SemanticOutcome.RESOLVED, TestRecordStatus.PASS),
        ([True], [True, False], SemanticOutcome.BREAKING_RESOLVED, TestRecordStatus.FAIL),
        ([False], [True, True], SemanticOutcome.NO_OP, TestRecordStatus.FAIL),
        ([False], [True, False], SemanticOutcome.REGRESSION, TestRecordStatus.FAIL),
    ],
)
def test_completed_result_accepts_consistent_schema_v1_shapes(post_f2p, post_p2p, outcome, full_status):
    result = _completed_result(post_f2p, post_p2p)
    assert result.outcome is outcome
    assert result.full_suite.status is full_status
    assert result.semantic_mapping()["outcome"] == outcome.value



def test_completed_result_reconciles_full_suite_status_with_counts_and_exit_code():
    resolved = _completed_result([True], [True, True])
    with pytest.raises(EvaluationInputError):
        replace(resolved, full_suite=replace(resolved.full_suite, status=TestRecordStatus.FAIL))

    no_op = _completed_result([False], [True, True])
    with pytest.raises(EvaluationInputError):
        replace(no_op, full_suite=replace(no_op.full_suite, status=TestRecordStatus.PASS))


def test_result_counters_reject_boolean_values():
    result = _completed_result([True], [True, True])
    with pytest.raises(EvaluationInputError):
        replace(result, f2p_total=True)
