"""Verifier-owned checks for the opt-in PDB proof fixture.

These checks are materialized only inside the verifier's disposable workspace.
They are never part of the task manifest, model workspace, tool observations,
provider prompt, or application event stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_debugger.runtime.test_runner import TestRunKind, TestRunner


@dataclass(frozen=True)
class PrivateCheckResult:
    applicable: bool
    passed: bool | None
    diagnostic: str | None = None


_PRIVATE_SOURCE = '''
from window_tail import tail_window


def test_private_window_larger_than_sequence_keeps_all_values() -> None:
    assert tail_window([2, 4, 6], 8) == [2, 4, 6]


def test_private_zero_window_is_empty() -> None:
    assert tail_window([2, 4, 6], 0) == []
'''


def run_private_checks(task: Any, runner: TestRunner, workspace: Any) -> PrivateCheckResult:
    if getattr(task, "task_id", None) != "pdb-required-boundary-006":
        return PrivateCheckResult(False, None)
    root = Path(workspace.root)
    relative = Path("__private_verifier__") / "test_boundary_contract.py"
    hidden_path = root / relative
    hidden_path.parent.mkdir(parents=True, exist_ok=False)
    hidden_path.write_text(_PRIVATE_SOURCE, encoding="utf-8", newline="\n")
    raw = runner.run_tests(
        ["python", "-m", "pytest", relative.as_posix(), "-q", "-p", "no:cacheprovider"],
        task.reproduction.cwd,
        task.tests.timeout_seconds,
        kind=TestRunKind.SELECTED,
    )
    if raw.timed_out or raw.launch_error or raw.command_result.exit_code is None:
        return PrivateCheckResult(True, False, "private verifier checks did not execute")
    if raw.command_result.exit_code != 0:
        return PrivateCheckResult(True, False, "private verifier checks failed")
    return PrivateCheckResult(True, True)


__all__ = ["PrivateCheckResult", "run_private_checks"]
