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

import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Optional

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
    create_isolated_worktree,
    get_dirty_summary,
    get_git_root,
    get_head_commit,
    get_launch_cwd,
    has_uncommitted_changes,
    is_git_worktree,
    list_child_directories,
    reset_launch_cwd,
    resolve_project_path,
    set_launch_cwd_for_tests,
    validate_local_project,
)


def inventory_tracked_python_files(
    isolated: Path,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """Return sorted tracked Python files via `git ls-files`, bounded.

    Uses `git ls-files -z` as authority (tracked files only, no untracked,
    no .git). Filters for `*.py`, excludes `.git` (never returned), checks
    symlink escape via `assert_path_inside_workspace`, and enforces a
    deterministic bound (200 files, 1 MiB each). If no Python files, fail
    clearly. If too large, fail clearly rather than silently dropping.

    ``environment`` is an explicit child-process mapping supplied by the
    session's V2 execution-environment authority (the project-command role
    for Local Project worker use).  When ``None`` the historical
    parent-inheritance behavior is preserved for direct non-product/UI
    callers; the real Local Project worker always supplies it so the Git
    child never implicitly inherits worker control/model/provider state.
    """
    import subprocess

    if environment is not None:
        if not isinstance(environment, Mapping):
            raise ApplicationInputError(
                "environment must be a mapping of strings or None"
            )
        for name, value in environment.items():
            if type(name) is not str or not name or type(value) is not str:
                raise ApplicationInputError(
                    "environment must map non-empty strings to strings"
                )
        child_env: Optional[dict[str, str]] = dict(environment)
    else:
        child_env = None

    try:
        result = subprocess.run(["git", "ls-files", "-z"],
            stdin=subprocess.DEVNULL,
            cwd=str(isolated),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            env=child_env,
        )
    except Exception as exc:
        raise ApplicationInputError(f"git ls-files failed: {exc}") from exc
    if result.returncode != 0:
        raise ApplicationInputError(f"git ls-files failed: {result.stderr.decode(errors='replace')[:200]}")
    raw = result.stdout.split(b"\x00")
    files: list[str] = []
    for b in raw:
        if not b:
            continue
        try:
            p = b.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if p.startswith(".git/") or p == ".git":
            continue
        if not p.endswith(".py"):
            continue
        # Symlink and size checks
        try:
            assert_path_inside_workspace(isolated, p)
        except ApplicationInputError:
            continue
        try:
            size = (isolated / p).stat().st_size
            if size > 1024 * 1024:
                continue
        except OSError:
            continue
        files.append(p.replace("\\", "/"))
    files = sorted(set(files))
    if not files:
        raise ApplicationInputError("No supported Python source files found.")
    if len(files) > 200:
        raise ApplicationInputError(f"Repository too large for bounded v1 inventory: {len(files)} Python files exceed 200 limit.")
    return files


def cleanup_parent_tmpdir(
    parent_tmpdir: Path,
    repo_root: Path,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    """Cleanup the full parent temp dir and verify Git registration pruned.

    Verified means: isolated filesystem path gone, parent gone, and `git
    worktree list --porcelain` no longer contains the isolated path.

    ``environment`` is the explicit project-safe child mapping from the
    session's V2 execution-environment authority.  The normal Local
    Project worker always supplies it so the ``git worktree prune`` /
    ``git worktree list`` children never implicitly inherit worker
    control/model/provider state.  ``None`` preserves the historical
    inheritance behavior for direct non-product callers (supervisor
    post-mortem, UI teardown, tests).
    """
    if environment is not None:
        if not isinstance(environment, Mapping):
            raise ApplicationInputError(
                "environment must be a mapping of strings or None"
            )
        for name, value in environment.items():
            if type(name) is not str or not name or type(value) is not str:
                raise ApplicationInputError(
                    "environment must map non-empty strings to strings"
                )
        child_env: Optional[dict[str, str]] = dict(environment)
    else:
        child_env = None
    isolated_path = None
    try:
        cand = parent_tmpdir / "worktree"
        if cand.exists():
            isolated_path = cand
    except Exception:
        pass
    if parent_tmpdir.exists():
        try:
            shutil.rmtree(parent_tmpdir, ignore_errors=True)
        except Exception:
            pass
        try:
            subprocess.run(["git", "worktree", "prune"],
                stdin=subprocess.DEVNULL,
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10.0,
                env=child_env,
            )
        except Exception:
            pass
    fs_gone = not parent_tmpdir.exists()
    git_pruned = True
    try:
        result = subprocess.run(["git", "worktree", "list", "--porcelain"],
            stdin=subprocess.DEVNULL,
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10.0,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
        )
        if result.returncode == 0:
            if isolated_path is not None:
                git_pruned = str(isolated_path.resolve()) not in result.stdout
            else:
                git_pruned = str(parent_tmpdir.resolve()) not in result.stdout
        else:
            git_pruned = False
    except Exception:
        git_pruned = False
    return fs_gone and git_pruned


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
