from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Any

from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.runtime.command_runner import CommandResult, CommandRunner
from agentic_debugger.runtime.exceptions import CommandExecutionError
from agentic_debugger.runtime.execution import VerifiedExecutionContext
from agentic_debugger.runtime.workspace import TaskWorkspace


class TestRunKind(Enum):
    __test__ = False  # pytest: production contract, not a test container

    REPRODUCTION = "reproduction"
    SELECTED = "selected"
    REGRESSION = "regression"
    FULL_SUITE = "full_suite"


@dataclass(frozen=True)
class TestRunResult:
    __test__ = False  # pytest: production result type, not a test container

    command_result: CommandResult
    kind: TestRunKind
    passed: bool
    reproduction_match: Optional[bool]
    timed_out: bool
    launch_error: bool

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "command_result": self.command_result.to_mapping(),
            "kind": self.kind.value,
            "passed": self.passed,
            "reproduction_match": self.reproduction_match,
            "timed_out": self.timed_out,
            "launch_error": self.launch_error,
        }


class TestRunner:
    """Executes test commands using CommandRunner.

    Delegates actual subprocess execution to ``CommandRunner``; does not
    create its own subprocess paths.
    """

    __test__ = False  # pytest: production runner, not a test container

    def __init__(
        self,
        workspace: TaskWorkspace,
        command_runner: Optional[CommandRunner] = None,
        execution_context: Optional[VerifiedExecutionContext] = None,
    ) -> None:
        self._workspace = workspace
        self._runner = command_runner or CommandRunner(workspace, execution_context)

    def run_reproduction(self, task: DebugTask) -> TestRunResult:
        argv = task.reproduction.argv
        cwd = task.reproduction.cwd
        timeout = task.reproduction.timeout_seconds
        expected = task.reproduction.expected_exit_code

        result, launch_error = self._run_command(argv, cwd, timeout)
        passed = result.exit_code == 0 if not launch_error else False
        reproduction_match = (
            result.exit_code == expected
            if (not result.timed_out and not launch_error)
            else None
        )
        return TestRunResult(
            command_result=result,
            kind=TestRunKind.REPRODUCTION,
            passed=passed,
            reproduction_match=reproduction_match,
            timed_out=result.timed_out,
            launch_error=launch_error,
        )

    def run_tests(
        self,
        argv: List[str],
        cwd: str,
        timeout_seconds: float,
        kind: TestRunKind = TestRunKind.SELECTED,
    ) -> TestRunResult:
        result, launch_error = self._run_command(argv, cwd, timeout_seconds)
        passed = result.exit_code == 0 if not launch_error else False
        return TestRunResult(
            command_result=result,
            kind=kind,
            passed=passed,
            reproduction_match=None,
            timed_out=result.timed_out,
            launch_error=launch_error,
        )

    def run_regression_tests(
        self,
        argv: List[str],
        cwd: str,
        timeout_seconds: float,
    ) -> TestRunResult:
        return self.run_tests(
            argv, cwd, timeout_seconds, kind=TestRunKind.REGRESSION
        )

    def run_full_suite(self, task: DebugTask) -> TestRunResult:
        argv = task.tests.full_suite_argv
        cwd = task.reproduction.cwd
        timeout = task.tests.timeout_seconds
        result, launch_error = self._run_command(argv, cwd, timeout)
        passed = result.exit_code == 0 if not launch_error else False
        return TestRunResult(
            command_result=result,
            kind=TestRunKind.FULL_SUITE,
            passed=passed,
            reproduction_match=None,
            timed_out=result.timed_out,
            launch_error=launch_error,
        )

    def _run_command(
        self,
        argv: List[str],
        cwd: str,
        timeout_seconds: float,
    ) -> tuple:
        try:
            return self._runner.run(argv, cwd, timeout_seconds), False
        except CommandExecutionError:
            return (
                CommandResult(
                    argv=list(argv),
                    cwd=cwd,
                    exit_code=None,
                    timed_out=False,
                    duration_ms=0,
                    stdout="",
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                ),
                True,
            )
