"""Local Project Debug — project validation, isolated worktree, and task contract.

This module is the public surface of the bounded v1 Local Project Debug
product, coordinating the final Apply-to-Project decision on top of the
Git/validation, worktree-lifecycle, and contract layers:

* :mod:`agentic_debugger.application.local_project_git` — launch-cwd
  preservation, project path resolution, read-only Git validation, the
  disposable isolated-worktree lifecycle with verified cleanup, and the
  workspace sandbox check;
* :mod:`agentic_debugger.application.local_project_contracts` — the
  immutable task/session specification record, its canonical hash, and
  the independent verifier's Apply-gate certificate with its binding
  check;
* this module — the owner-facing Apply-to-Project gates and the final
  no-commit patch application, plus the public import surface.

The Git interaction remains intentionally narrow: only the read-only
validation commands and the explicit worktree create/remove commands are
used.  No ``reset --hard``, ``clean``, ``stash``, or branch checkout ever
touches the owner working tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.local_project_contracts import (
    LOCAL_PROJECT_VERIFICATION_AUTHORITY,
    LOCAL_PROJECT_VERIFICATION_FILE_NAME,
    LOCAL_PROJECT_VERIFICATION_SCHEMA_VERSION,
    LocalProjectTaskSpec,
    LocalProjectVerificationCertificate,
    check_verification_certificate,
    local_project_task_spec_sha256,
)
from agentic_debugger.application.local_project_git import (
    IsolatedWorktree,
    LocalProjectValidationError,
    ValidatedProject,
    assert_path_inside_workspace,
    capture_launch_cwd,
    cleanup_isolated_worktree,
    cleanup_parent_tmpdir,
    create_isolated_worktree,
    get_dirty_summary,
    get_git_root,
    get_head_commit,
    get_launch_cwd,
    has_uncommitted_changes,
    inventory_tracked_python_files,
    is_git_worktree,
    list_child_directories,
    reset_launch_cwd,
    resolve_project_path,
    set_launch_cwd_for_tests,
    validate_local_project,
)


# ---------------------------------------------------------------------------
# Apply-to-project safety gates (#16)
# ---------------------------------------------------------------------------

def check_apply_gates(
    repo_root: Path,
    expected_head: str,
    patch_text: str,
) -> tuple[bool, str]:
    """Check the three safety gates before applying to owner project.

    Gates:

    1. selected owner repo still resolves to the recorded repository;
    2. HEAD has not changed since session start;
    3. working tree is still clean;
    4. ``git apply --check`` against owner tree succeeds.

    Returns (ok, reason).
    Never mutates the owner tree.
    """
    # Gate 1: repo still resolves
    if not repo_root.is_dir():
        return False, "project directory no longer exists"
    if not is_git_worktree(repo_root):
        return False, "project is no longer a Git repository"
    try:
        current_root = get_git_root(repo_root)
    except ApplicationInputError as exc:
        return False, f"cannot resolve repository root: {exc}"
    if current_root.resolve() != repo_root.resolve():
        return False, "project repository root changed"
    # Gate 2: HEAD unchanged
    try:
        current_head = get_head_commit(repo_root)
    except ApplicationInputError as exc:
        return False, f"cannot read HEAD: {exc}"
    if current_head != expected_head:
        return False, "project HEAD changed since session start"
    # Gate 3: working tree still clean
    try:
        if has_uncommitted_changes(repo_root):
            return False, "project working tree is dirty"
    except ApplicationInputError as exc:
        return False, f"cannot check working tree: {exc}"
    # Gate 4: git apply --check (UTF-8 bytes so non-ASCII patch content is
    # never re-encoded through the Windows locale code page)
    try:
        proc = subprocess.run(["git", "apply", "--check", "-p1", "-"],
            cwd=str(repo_root),
            input=patch_text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10.0,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).decode("utf-8", errors="replace")
            return False, f"git apply --check failed: {detail.strip()[:300]}"
    except FileNotFoundError:
        return False, "git is not available"
    except subprocess.TimeoutExpired:
        return False, "git apply --check timed out"
    return True, "ok"


def load_apply_verification_materials(
    session_dir: Path,
) -> tuple[LocalProjectTaskSpec, "LocalProjectVerificationCertificate"]:
    """Read one session's task contract and verification certificate.

    Read-only parsing for the UI's Apply To Project gate: keeps JSON
    loading and schema mapping on the application side of the boundary.
    Raises ``FileNotFoundError``/``ValueError`` for missing or invalid
    artifacts; callers translate those into their fail-closed messages.
    """
    import json as _json

    task = LocalProjectTaskSpec.from_mapping(
        _json.loads(
            (session_dir / "local_project_task.json").read_text(encoding="utf-8")
        )
    )
    certificate = LocalProjectVerificationCertificate.from_mapping(
        _json.loads(
            (session_dir / LOCAL_PROJECT_VERIFICATION_FILE_NAME).read_text(
                encoding="utf-8"
            )
        )
    )
    return task, certificate


def has_tracked_root_repro(repo_root: Path) -> bool:
    """Whether the repository tracks a root-level ``repro.py`` exactly.

    Uses ``git ls-files`` as the authority (tracked files only).  Does not
    inspect arbitrary filesystem content, does not search subdirectories, and
    does not infer commands.  Conservative: any Git failure returns ``False``.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", "repro.py"],
            stdin=subprocess.DEVNULL,
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5.0,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return False
        files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return "repro.py" in files
    except Exception:
        return False


def apply_patch_to_project(
    repo_root: Path,
    patch_text: str,
    *,
    expected_head: Optional[str] = None,
) -> tuple[bool, str]:
    """Apply the canonical candidate patch to the owner project (no commit).

    When ``expected_head`` is supplied, repeats every read-only gate inside
    this mutation helper immediately before ``git apply``.  This narrows the
    caller/helper TOCTOU window; it cannot lock out unrelated external Git
    writers.  Leaves changes uncommitted for owner review.  No branch creation.
    """
    if expected_head is not None:
        ok, reason = check_apply_gates(repo_root, expected_head, patch_text)
        if not ok:
            return False, f"apply-time gate failed: {reason}"
    try:
        proc = subprocess.run(["git", "apply", "-p1", "-"],
            cwd=str(repo_root),
            input=patch_text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15.0,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).decode("utf-8", errors="replace")
            return False, f"git apply failed: {detail.strip()[:300]}"
    except Exception as exc:
        return False, f"apply failed: {exc}"
    return True, "Patch applied to project"


__all__ = [
    "IsolatedWorktree",
    "LocalProjectTaskSpec",
    "LocalProjectVerificationCertificate",
    "LocalProjectValidationError",
    "ValidatedProject",
    "apply_patch_to_project",
    "assert_path_inside_workspace",
    "capture_launch_cwd",
    "check_apply_gates",
    "check_verification_certificate",
    "cleanup_isolated_worktree",
    "cleanup_parent_tmpdir",
    "create_isolated_worktree",
    "get_dirty_summary",
    "get_git_root",
    "get_head_commit",
    "get_launch_cwd",
    "has_tracked_root_repro",
    "has_uncommitted_changes",
    "is_git_worktree",
    "list_child_directories",
    "local_project_task_spec_sha256",
    "resolve_project_path",
    "reset_launch_cwd",
    "set_launch_cwd_for_tests",
    "validate_local_project",
]
