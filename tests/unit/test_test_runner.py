import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from agentic_debugger.evaluation.task_schema import DebugTask, Reproduction, Tests
from agentic_debugger.runtime.command_runner import CommandResult, CommandRunner
from agentic_debugger.runtime.exceptions import (
    CommandRequestError,
    WorkspaceError,
)
from agentic_debugger.runtime.test_runner import TestRunKind, TestRunResult, TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace


def _make_minimal_task(
    reproduction_argv=None,
    reproduction_cwd=".",
    reproduction_timeout=10,
    expected_exit_code=1,
    full_suite_argv=None,
    test_timeout=20,
):
    if reproduction_argv is None:
        reproduction_argv = [sys.executable, "-c", "exit(1)"]
    if full_suite_argv is None:
        full_suite_argv = [sys.executable, "-c", "exit(0)"]
    return DebugTask(
        schema_version="1.0",
        task_id="test-task-001",
        title="Test Task",
        description="A test task",
        language="python",
        fixture_path="agentic_debugger/datasets/curated/test-task-001",
        reproduction=Reproduction(
            argv=reproduction_argv,
            cwd=reproduction_cwd,
            timeout_seconds=reproduction_timeout,
            expected_exit_code=expected_exit_code,
        ),
        tests=Tests(
            fail_to_pass=["tests/test_main.py::test_a"],
            pass_to_pass=["tests/test_main.py::test_b", "tests/test_main.py::test_c"],
            full_suite_argv=full_suite_argv,
            timeout_seconds=test_timeout,
        ),
        constraints=None,
        oracle=None,
        tags=["test"],
    )


@pytest.fixture
def workspace():
    src = Path(tempfile.mkdtemp())
    try:
        (src / "hello.py").write_text("print('hello')\n")
        with TaskWorkspace(str(src)) as ws:
            yield ws
    finally:
        shutil.rmtree(str(src), ignore_errors=True)


class TestTestRunKind:
    def test_members(self):
        assert TestRunKind.REPRODUCTION.value == "reproduction"
        assert TestRunKind.SELECTED.value == "selected"
        assert TestRunKind.REGRESSION.value == "regression"
        assert TestRunKind.FULL_SUITE.value == "full_suite"


class TestTestRunnerReproduction:
    def test_reproduction_uses_task_argv(self, workspace):
        task = _make_minimal_task(
            reproduction_argv=[sys.executable, "-c", "exit(1)"]
        )
        runner = TestRunner(workspace)
        result = runner.run_reproduction(task)
        assert result.command_result.exit_code == 1
        assert result.kind == TestRunKind.REPRODUCTION
        assert result.passed is False
        assert result.reproduction_match is True
        assert result.timed_out is False
        assert result.launch_error is False

    def test_reproduction_expected_failing_exit_code_recognized(
        self, workspace
    ):
        task = _make_minimal_task(
            reproduction_argv=[sys.executable, "-c", "exit(1)"],
            expected_exit_code=1,
        )
        runner = TestRunner(workspace)
        result = runner.run_reproduction(task)
        assert result.reproduction_match is True

    def test_reproduction_unexpected_success_distinguishable(
        self, workspace
    ):
        task = _make_minimal_task(
            reproduction_argv=[sys.executable, "-c", "exit(0)"],
            expected_exit_code=1,
        )
        runner = TestRunner(workspace)
        result = runner.run_reproduction(task)
        assert result.passed is True
        assert result.reproduction_match is False


class TestTestRunnerFullSuite:
    def test_full_suite_uses_task_test_timeout(self, workspace):
        task = _make_minimal_task(
            full_suite_argv=[sys.executable, "-c", "exit(0)"]
        )
        runner = TestRunner(workspace)
        result = runner.run_full_suite(task)
        assert result.kind == TestRunKind.FULL_SUITE
        assert result.passed is True
        assert result.command_result.exit_code == 0

    def test_full_suite_failing(self, workspace):
        task = _make_minimal_task(
            full_suite_argv=[sys.executable, "-c", "exit(1)"]
        )
        runner = TestRunner(workspace)
        result = runner.run_full_suite(task)
        assert result.passed is False


class TestTestRunnerGeneric:
    def test_selected_tests_delegates_to_runner(self, workspace):
        runner = TestRunner(workspace)
        result = runner.run_tests(
            [sys.executable, "-c", "exit(0)"],
            cwd=".",
            timeout_seconds=10,
            kind=TestRunKind.SELECTED,
        )
        assert result.kind == TestRunKind.SELECTED
        assert result.passed is True

    def test_regression_tests(self, workspace):
        runner = TestRunner(workspace)
        result = runner.run_regression_tests(
            [sys.executable, "-c", "exit(0)"],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.kind == TestRunKind.REGRESSION
        assert result.passed is True

    def test_passing_state(self, workspace):
        runner = TestRunner(workspace)
        result = runner.run_tests(
            [sys.executable, "-c", "exit(0)"],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.passed is True
        assert result.timed_out is False

    def test_failing_state(self, workspace):
        runner = TestRunner(workspace)
        result = runner.run_tests(
            [sys.executable, "-c", "exit(1)"],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.passed is False

    def test_timed_out_state(self, workspace):
        runner = TestRunner(workspace)
        code = "import time; time.sleep(30)"
        result = runner.run_tests(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=1,
        )
        assert result.timed_out is True
        assert result.passed is False

    def test_launch_error_state(self, workspace):
        runner = TestRunner(workspace)
        result = runner.run_tests(
            ["/nonexistent/binary"],
            cwd=".",
            timeout_seconds=5,
        )
        assert result.launch_error is True

    def test_command_output_preserved(self, workspace):
        runner = TestRunner(workspace)
        result = runner.run_tests(
            [sys.executable, "-c", "print('hello')"],
            cwd=".",
            timeout_seconds=10,
        )
        assert "hello" in result.command_result.stdout

    def test_truncation_flags_preserved(self, workspace):
        runner = TestRunner(workspace)
        code = "print('x' * 30000)"
        result = runner.run_tests(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.command_result.stdout_truncated is True

    def test_no_alternate_subprocess(self, workspace):
        runner = TestRunner(workspace)
        internal_runner = runner._runner
        assert isinstance(internal_runner, CommandRunner)


class TestTestRunResult:
    def test_to_mapping(self):
        cr = CommandResult(
            argv=["python", "-c", "pass"],
            cwd=".",
            exit_code=0,
            timed_out=False,
            duration_ms=10,
            stdout="ok",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        )
        trr = TestRunResult(
            command_result=cr,
            kind=TestRunKind.REPRODUCTION,
            passed=True,
            reproduction_match=True,
            timed_out=False,
            launch_error=False,
        )
        m = trr.to_mapping()
        assert m["kind"] == "reproduction"
        assert m["passed"] is True
        assert m["reproduction_match"] is True
        assert m["command_result"]["exit_code"] == 0


class TestTestRunnerErrorPropagation:
    def test_invalid_argv_propagates(self, workspace):
        runner = TestRunner(workspace)
        with pytest.raises(CommandRequestError):
            runner.run_tests([], cwd=".", timeout_seconds=10)

    def test_invalid_cwd_propagates(self, workspace):
        runner = TestRunner(workspace)
        with pytest.raises(WorkspaceError):
            runner.run_tests(
                [sys.executable, "-c", "pass"],
                cwd="/etc",
                timeout_seconds=10,
            )

    def test_traversal_cwd_propagates(self, workspace):
        runner = TestRunner(workspace)
        with pytest.raises(WorkspaceError):
            runner.run_tests(
                [sys.executable, "-c", "pass"],
                cwd="../outside",
                timeout_seconds=10,
            )
