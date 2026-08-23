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

_PRIVATE_CALLER_CALLEE_SOURCE = '''
from price_pipeline import format_price


def test_private_nonzero_dollars_are_converted() -> None:
    assert format_price(3, "dollars") == "$3.00"


def test_private_small_cents_are_not_converted_again() -> None:
    assert format_price(7, "cents") == "$0.07"
'''

_PRIVATE_MULTISTAGE_UNITS_SOURCE = '''
from deadline_pipeline import request_deadline


def test_private_seconds_without_retries_still_convert() -> None:
    assert request_deadline(1, "seconds", 0, 10) == 1010


def test_private_mixed_case_seconds_expand_after_conversion() -> None:
    assert request_deadline(3, "Seconds", 1, 5) == 6005


def test_private_milliseconds_expand_without_double_conversion() -> None:
    assert request_deadline(15, "milliseconds", 2, 5) == 50
'''

_PRIVATE_CHECKS = {
    "pdb-required-boundary-006": (
        "test_boundary_contract.py",
        _PRIVATE_SOURCE,
    ),
    "pdb-required-caller-callee-007": (
        "test_caller_callee_contract.py",
        _PRIVATE_CALLER_CALLEE_SOURCE,
    ),
    "pdb-required-multistage-units-008": (
        "test_multistage_units_contract.py",
        _PRIVATE_MULTISTAGE_UNITS_SOURCE,
    ),
}


def run_private_checks(task: Any, runner: TestRunner, workspace: Any) -> PrivateCheckResult:
    private_check = _PRIVATE_CHECKS.get(getattr(task, "task_id", None))
    if private_check is None:
        return PrivateCheckResult(False, None)
    filename, source = private_check
    root = Path(workspace.root)
    relative = Path("__private_verifier__") / filename
    hidden_path = root / relative
    hidden_path.parent.mkdir(parents=True, exist_ok=False)
    hidden_path.write_text(source, encoding="utf-8", newline="\n")
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
