"""R4 — run the frozen model-generated regression test against buggy/fixed code.

Both runs use a FRESH disposable ``TaskWorkspace`` copied from the canonical
fixture (strictly separate BUGGY and FIXED workspaces, R4 amendment 5). The
frozen generated test is written into ``tests/`` inside each disposable copy
(it is never a patch to the canonical fixture). The canonical fixture is never
mutated.

The FIXED run applies the accepted R3.2 repair ``R_fix_C`` through the real
``PatchManager`` with the same path/syntax/authorization gates the production
verifier uses (``allowed_paths=["recent_window.py"]``,
``denied_paths=["tests", "task.json"]``). No gate is weakened.

Buggy-FAIL validity is STRUCTURED (R4 amendment 6), not just a nonzero pytest
exit code: compile + collection + execution + counts + infrastructure-marker
checks + assertion attribution to the generated test exercising
``recent_window``.

Cleanup is part of correctness: every workspace is cleaned on success and
failure, or the remaining state is reported.
"""

from __future__ import annotations

import hashlib
import py_compile
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agentic_debugger.runtime.exceptions import (
    PatchApplyError,
    PatchAuthorizationError,
    PatchStateError,
    PatchValidationError,
)
from agentic_debugger.runtime.patcher import PatchManager
from agentic_debugger.runtime.test_runner import TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace

from experiments.model_generated_test_probe_r4.test_generation import (
    GENERATED_TEST_NODE,
    FrozenTest,
    _parse_pytest_summary,
    _sha256,
)

ALLOWED_PATCH_PATHS = ["recent_window.py"]
DENIED_PATCH_PATHS = ["tests", "task.json"]

GENERATED_TEST_FILENAME = "test_generated_regression.py"

# Infrastructure markers whose presence disqualifies a "genuine" test run.
_INFRASTRUCTURE_MARKERS = (
    "syntaxerror",
    "importerror",
    "modulenotfounderror",
    "error collecting",
    "internalerror",
    "fixture",
    "oserror",
    "filenotfounderror",
)


@dataclass(frozen=True)
class FrozenTestRunResult:
    """Structured result of running the frozen test against one code version."""

    label: str  # "buggy" | "fixed" | "executability"
    frozen_test_sha256: str  # SHA of the parsed candidate T_parsed
    written_test_sha256: str  # SHA of the exact bytes written (T_written)
    executed: bool
    status: str  # "PASS" | "FAIL" | "ERROR" | "NOT_RUN"
    exit_code: Optional[int]
    counts: Optional[dict[str, int]]
    reason: str
    timed_out: bool
    launch_error: bool
    patch_applied: bool
    patch_error: Optional[str]
    compiled: bool
    compile_error: Optional[str]
    collected: Optional[int]
    collect_error: bool
    infrastructure_markers: list[str]
    assertion_attributed: bool
    valid_buggy_failure: bool  # structured buggy-FAIL gate (buggy label only)
    workspace_cleaned: bool
    canonical_tree_hash_before: Optional[str]
    canonical_tree_hash_after: Optional[str]
    stdout_bounded: str
    stderr_bounded: str


def _bound(text: str, limit: int = 8192) -> str:
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return text[: limit - 3] + "..."


def _tree_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        p for p in directory.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    for p in files:
        rel = p.relative_to(directory).as_posix()
        digest.update(rel.encode("utf-8")); digest.update(b"\0")
        digest.update(p.read_bytes()); digest.update(b"\0")
    return digest.hexdigest()


def _compile_check(test_file: Path) -> tuple[bool, Optional[str]]:
    """Structurally compile the written test file (syntax gate)."""
    try:
        py_compile.compile(str(test_file), doraise=True)
        return True, None
    except Exception as exc:  # noqa: BLE001 — fail-closed retention
        return False, f"{type(exc).__name__}: {exc}"


def _collect_check(runner: TestRunner, timeout_seconds: int) -> tuple[int, bool]:
    """pytest --collect-only gate: return (collected_count, collect_error)."""
    raw = runner.run_tests(
        argv=[
            "python", "-m", "pytest", GENERATED_TEST_NODE,
            "--collect-only", "-q", "-p", "no:cacheprovider", "--no-header",
        ],
        cwd=".",
        timeout_seconds=timeout_seconds,
    )
    output = (raw.command_result.stdout or "") + "\n" + (raw.command_result.stderr or "")
    collected = 0
    # pytest >= 8: "1 test collected in 0.03s"; older: "collected 1 item".
    match = re.search(
        r"(?:collected\s+(\d+)\s+(?:test|tests|item|items)|"
        r"(\d+)\s+(?:test|tests|item|items)\s+collected)",
        output,
    )
    if match:
        collected = int(match.group(1) or match.group(2))
    collect_error = bool(
        raw.timed_out
        or raw.launch_error
        or "error" in output.lower()
        or collected == 0
    )
    return collected, collect_error


def _infrastructure_markers(output: str) -> list[str]:
    lowered = output.lower()
    return [m for m in _INFRASTRUCTURE_MARKERS if m in lowered]


def _assertion_attribution(output: str) -> bool:
    """AssertionError present, the generated node failed, target exercised.

    The failure must show an ``AssertionError``, the failed node must be the
    generated test file (path-separator robust: the plain filename appears in
    the FAILED summary), and the failure must reference the target symbol
    ``recent_window`` (i.e., the test actually exercises the target behavior).
    Infrastructure markers are handled separately.
    """

    lowered = output.lower()
    if "assertionerror" not in lowered:
        return False
    if GENERATED_TEST_FILENAME not in output:
        return False
    if "recent_window" not in lowered:
        return False
    return True


def run_structured_generated_test(
    frozen_test_source: str,
    fixture_dir: Path,
    case_dir: Path,
    *,
    label: str,
    candidate_patch: Optional[str],
    timeout_seconds: int,
) -> FrozenTestRunResult:
    """Structured buggy/fixed run in a fresh disposable workspace.

    Sequence: fresh workspace copy -> optional R_fix_C via real PatchManager
    -> write frozen test (record T_written SHA) -> compile gate -> collect
    gate -> execute -> classify.
    """

    workspace: Optional[TaskWorkspace] = None
    result: Optional[FrozenTestRunResult] = None
    try:
        workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_dir))
        root = Path(workspace.root)
        tree_before = _tree_hash(root)

        patch_applied = False
        patch_error: Optional[str] = None
        if candidate_patch is not None:
            try:
                manager = PatchManager(
                    workspace,
                    allowed_paths=list(ALLOWED_PATCH_PATHS),
                    denied_paths=list(DENIED_PATCH_PATHS),
                )
                manager.apply_patch(candidate_patch)
                patch_applied = True
            except (PatchValidationError, PatchAuthorizationError,
                    PatchApplyError, PatchStateError) as exc:
                patch_error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 — fail-closed retention
                patch_error = f"{type(exc).__name__}: {exc}"

        # Write the frozen generated test into the workspace's tests/ dir.
        # Exact bytes: binary write with NO newline translation, so T_written
        # equals T_parsed byte-for-byte (recorded explicitly in evidence).
        test_dir = root / "tests"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = test_dir / GENERATED_TEST_FILENAME
        test_file.write_bytes(frozen_test_source.encode("utf-8"))
        written_sha = _sha256(frozen_test_source)

        runner = TestRunner(workspace)
        compiled, compile_error = _compile_check(test_file)
        collected, collect_error = _collect_check(runner, timeout_seconds)

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
        counts = _parse_pytest_summary(output)
        markers = _infrastructure_markers(output)

        status = "ERROR"
        reason = ""
        executed = False
        if raw.timed_out:
            reason = "timed_out"
        elif raw.launch_error or cmd.exit_code is None:
            reason = "launch_error"
        elif patch_error is not None:
            status = "NOT_RUN"
            reason = "patch_apply_failed"
        elif compile_error is not None:
            reason = "compile_error"
        elif collect_error:
            reason = "collection_error"
        elif counts is None:
            reason = "malformed_pytest_summary"
        elif markers:
            status = "ERROR"
            reason = f"infrastructure_markers={markers}"
        else:
            executed = True
            ran = counts["passed"] + counts["failed"]
            if ran == 1 and counts["passed"] == 1:
                status = "PASS"
                reason = "passed"
            elif ran == 1 and counts["failed"] == 1:
                status = "FAIL"
                reason = "failed"
            elif counts["errors"] > 0:
                reason = f"errors={counts['errors']}"
            else:
                reason = f"unexpected counts {counts}"

        attributed = False
        if status == "FAIL":
            attributed = _assertion_attribution(output)

        # Structured buggy-FAIL gate (only meaningful for the buggy label).
        valid_buggy_failure = bool(
            label == "buggy"
            and executed
            and status == "FAIL"
            and compiled
            and collected == 1
            and not collect_error
            and not raw.timed_out
            and not raw.launch_error
            and counts is not None
            and counts["failed"] == 1
            and counts["errors"] == 0
            and counts["skipped"] == 0
            and counts["xfailed"] == 0
            and counts["xpassed"] == 0
            and not markers
            and attributed
        )

        tree_after = _tree_hash(root)

        result = FrozenTestRunResult(
            label=label,
            frozen_test_sha256=_sha256(frozen_test_source),
            written_test_sha256=written_sha,
            executed=executed,
            status=status,
            exit_code=cmd.exit_code,
            counts=counts,
            reason=reason,
            timed_out=raw.timed_out,
            launch_error=raw.launch_error,
            patch_applied=patch_applied,
            patch_error=patch_error,
            compiled=compiled,
            compile_error=compile_error,
            collected=collected,
            collect_error=collect_error,
            infrastructure_markers=markers,
            assertion_attributed=attributed,
            valid_buggy_failure=valid_buggy_failure,
            workspace_cleaned=False,  # set by caller after cleanup
            canonical_tree_hash_before=tree_before,
            canonical_tree_hash_after=tree_after,
            stdout_bounded=_bound(cmd.stdout or ""),
            stderr_bounded=_bound(cmd.stderr or ""),
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

    return run_structured_generated_test(
        frozen_test.source,
        fixture_dir,
        case_dir,
        label="buggy",
        candidate_patch=None,
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
    """Run the frozen test against the R_fix_C-patched fixture copy."""

    return run_structured_generated_test(
        frozen_test.source,
        fixture_dir,
        case_dir,
        label="fixed",
        candidate_patch=candidate_patch,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "FrozenTestRunResult",
    "run_structured_generated_test",
    "run_buggy",
    "run_fixed",
    "ALLOWED_PATCH_PATHS",
    "DENIED_PATCH_PATHS",
    "GENERATED_TEST_FILENAME",
]
