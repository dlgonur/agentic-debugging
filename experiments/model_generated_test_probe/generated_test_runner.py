"""Run the frozen model-generated regression test against buggy and fixed code.

Both runs use a FRESH disposable ``TaskWorkspace`` copied from the canonical
fixture. The frozen generated test is written into ``tests/`` inside the
workspace (it is NOT a patch to the canonical fixture — it lives only in the
disposable copy). The canonical fixture is never mutated.

The model-fixed-code run applies the model's candidate unified diff through
the existing ``PatchManager`` (same path/syntax/authorization gates as the
production verifier: ``allowed_write_paths=["display_name.py"]``,
``denied_write_paths=["tests", "task.json"]``). No gate is weakened.

Cleanup is part of correctness: every workspace is cleaned on success and
failure, or the remaining state is reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.runtime.exceptions import (
    PatchApplyError,
    PatchAuthorizationError,
    PatchStateError,
    PatchValidationError,
)
from agentic_debugger.runtime.patcher import PatchManager
from agentic_debugger.runtime.test_runner import TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace

from experiments.model_generated_test_probe.test_generation import (
    GENERATED_TEST_NODE,
    FrozenTest,
)


@dataclass(frozen=True)
class FrozenTestRunResult:
    """The result of running the frozen test against one code version."""

    label: str  # "buggy" | "fixed"
    frozen_test_sha256: str
    executed: bool
    status: str  # "PASS" | "FAIL" | "ERROR" | "NOT_RUN"
    exit_code: Optional[int]
    counts: Optional[dict[str, int]]
    reason: str
    patch_applied: bool
    patch_error: Optional[str]
    workspace_cleaned: bool
    stdout_bounded: str
    stderr_bounded: str


def _bound(text: str, limit: int = 8192) -> str:
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return text[: limit - 3] + "..."


def _run_frozen_test_in_workspace(
    workspace: TaskWorkspace,
    frozen_test: FrozenTest,
    *,
    label: str,
    candidate_patch: Optional[str],
    timeout_seconds: int,
) -> FrozenTestRunResult:
    """Apply an optional patch, write the frozen test, run it, record result."""

    patch_applied = False
    patch_error: Optional[str] = None
    if candidate_patch:
        try:
            manager = PatchManager(
                workspace,
                allowed_paths=["display_name.py"],
                denied_paths=["tests", "task.json"],
            )
            manager.apply_patch(candidate_patch)
            patch_applied = True
        except (PatchValidationError, PatchAuthorizationError,
                PatchApplyError, PatchStateError) as exc:
            patch_error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 — fail-closed retention
            patch_error = f"{type(exc).__name__}: {exc}"

    # Write the frozen generated test into the workspace's tests/ dir.
    test_dir = Path(workspace.root) / "tests"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "test_generated_regression.py").write_text(
        frozen_test.source, encoding="utf-8"
    )

    runner = TestRunner(workspace)
    raw = runner.run_tests(
        argv=[
            "python", "-m", "pytest", GENERATED_TEST_NODE,
            "-q", "-p", "no:cacheprovider", "--no-header", "-vv",
        ],
        cwd=".",
        timeout_seconds=timeout_seconds,
    )
    cmd = raw.command_result
    output = (cmd.stdout or "") + "\n" + (cmd.stderr or "")

    # Reuse the probe-local summary parser.
    from experiments.model_generated_test_probe.test_generation import (
        _parse_pytest_summary,
    )
    counts = _parse_pytest_summary(output)

    status = "ERROR"
    reason = ""
    if raw.timed_out:
        status = "ERROR"
        reason = "timed_out"
    elif raw.launch_error or cmd.exit_code is None:
        status = "ERROR"
        reason = "launch_error"
    elif patch_error is not None:
        status = "NOT_RUN"
        reason = "patch_apply_failed"
    elif counts is None:
        status = "ERROR"
        reason = "malformed_pytest_summary"
    else:
        executed = counts["passed"] + counts["failed"]
        if executed == 1 and counts["passed"] == 1:
            status = "PASS"
            reason = "passed"
        elif executed == 1 and counts["failed"] == 1:
            status = "FAIL"
            reason = "failed"
        elif counts["errors"] > 0:
            status = "ERROR"
            reason = f"errors={counts['errors']}"
        else:
            status = "ERROR"
            reason = f"unexpected counts {counts}"

    return FrozenTestRunResult(
        label=label,
        frozen_test_sha256=frozen_test.sha256,
        executed=(patch_error is None and not raw.timed_out
                  and not raw.launch_error and counts is not None),
        status=status,
        exit_code=cmd.exit_code,
        counts=counts,
        reason=reason,
        patch_applied=patch_applied,
        patch_error=patch_error,
        workspace_cleaned=False,  # set by caller after cleanup
        stdout_bounded=_bound(cmd.stdout or ""),
        stderr_bounded=_bound(cmd.stderr or ""),
    )


def _run_with_cleanup(
    frozen_test: FrozenTest,
    fixture_dir: Path,
    case_dir: Path,
    *,
    label: str,
    candidate_patch: Optional[str],
    timeout_seconds: int,
) -> FrozenTestRunResult:
    """Disposable-workspace run with guaranteed cleanup.

    Creates a fresh ``TaskWorkspace`` copy of the canonical fixture, optionally
    applies the candidate patch, writes the frozen generated test, runs it,
    records the result, and cleans up. The cleanup status is folded into the
    returned (frozen) result.
    """

    workspace: Optional[TaskWorkspace] = None
    result: Optional[FrozenTestRunResult] = None
    try:
        workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_dir))
        result = _run_frozen_test_in_workspace(
            workspace, frozen_test,
            label=label, candidate_patch=candidate_patch,
            timeout_seconds=timeout_seconds,
        )
        return result
    finally:
        cleaned = False
        if workspace is not None:
            try:
                workspace.cleanup()
                cleaned = not Path(workspace.root).exists()
            except Exception:  # noqa: BLE001 — report, do not raise
                cleaned = False
        if result is not None:
            object.__setattr__(result, "workspace_cleaned", cleaned)


def run_buggy(
    frozen_test: FrozenTest,
    fixture_dir: Path,
    case_dir: Path,
    *,
    timeout_seconds: int = 20,
) -> FrozenTestRunResult:
    """Run the frozen test against the unmodified buggy fixture copy."""

    return _run_with_cleanup(
        frozen_test, fixture_dir, case_dir,
        label="buggy", candidate_patch=None,
        timeout_seconds=timeout_seconds,
    )


def run_fixed(
    frozen_test: FrozenTest,
    candidate_patch: str,
    fixture_dir: Path,
    case_dir: Path,
    *,
    timeout_seconds: int = 20,
) -> FrozenTestRunResult:
    """Run the frozen test against the model-patched fixture copy."""

    return _run_with_cleanup(
        frozen_test, fixture_dir, case_dir,
        label="fixed", candidate_patch=candidate_patch,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "FrozenTestRunResult",
    "run_buggy",
    "run_fixed",
]