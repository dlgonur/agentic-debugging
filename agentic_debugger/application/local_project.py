"""Local Project Debug — project validation, isolated worktree, and task contract.

This module owns the bounded v1 Local Project Debug product surface that the
agentic-debugger core itself never needed:

- launch-cwd preservation (captured before any ``--root`` filesystem handling);
- three user project-input modes that resolve to one canonical repository root;
- safe Git working-tree validation (directory exists, readable, inside a Git
  working tree, resolvable repository root, HEAD exists, no dangerous path);
- dirty-worktree truth (fail-closed on any uncommitted change for this bounded
  v1);
- isolated session workspace from the selected Git repository (detached Git
  worktree at the recorded HEAD) with explicit lifecycle and cleanup;
- sandboxed source access (no traversal/symlink escape);
- immutable task/session specification record (the Local Project contract).

The Git interaction here is intentionally narrow: only the read-only validation
commands and the explicit worktree create/remove commands are used.  No
``reset --hard``, ``clean``, ``stash``, or branch checkout ever touches the
owner working tree.
"""

from __future__ import annotations

import os
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.session import SessionBudgets


LOCAL_PROJECT_VERIFICATION_FILE_NAME = "local_project_verification.json"
LOCAL_PROJECT_VERIFICATION_SCHEMA_VERSION = 2
LOCAL_PROJECT_VERIFICATION_AUTHORITY = (
    "agentic-debugger.independent-local-project-verifier"
)


# ---------------------------------------------------------------------------
# Launch CWD preservation
# ---------------------------------------------------------------------------

_LAUNCH_CWD: Optional[Path] = None


def capture_launch_cwd() -> Path:
    """Capture and return the shell working directory at process launch.

    The first call records ``Path.cwd()`` resolved; later calls return the
    same value.  The UI launcher calls this at module import / app
    construction time before any ``--root`` filesystem handling could change
    the internal context.  Falling back to ``cwd()`` on first access also
    preserves the invariant for programmatic callers.
    """
    global _LAUNCH_CWD
    if _LAUNCH_CWD is None:
        try:
            _LAUNCH_CWD = Path.cwd().resolve()
        except Exception:
            _LAUNCH_CWD = Path.cwd()
    return _LAUNCH_CWD


def get_launch_cwd() -> Path:
    """Return the captured launch cwd, capturing it now if necessary."""
    return capture_launch_cwd()


def set_launch_cwd_for_tests(path: str | os.PathLike[str]) -> None:
    """Test seam: override the captured launch cwd (tests only)."""
    global _LAUNCH_CWD
    _LAUNCH_CWD = Path(path).resolve()


def reset_launch_cwd() -> None:
    """Reset the captured value (tests only)."""
    global _LAUNCH_CWD
    _LAUNCH_CWD = None


# ---------------------------------------------------------------------------
# Path resolution (A/B/C modes converge here)
# ---------------------------------------------------------------------------

_MAX_PATH_BYTES = 4096
_UNSAFE_COMPONENTS = re.compile(r"[\x00]")

def resolve_project_path(raw: str, launch_cwd: Optional[Path] = None) -> Path:
    """Resolve one user-supplied project path against the launch cwd.

    - absolute paths are resolved absolutely;
    - relative paths are resolved against ``launch_cwd`` (or captured launch cwd);
    - ``~`` expansion is not performed (explicit paths only);
    - result is a resolved absolute ``Path`` (symlinks/reparse points resolved).

    Raises :class:`ApplicationInputError` on invalid input.
    """
    if type(raw) is not str or not raw.strip():
        raise ApplicationInputError("project path must be a non-empty string")
    if len(raw.encode("utf-8")) > _MAX_PATH_BYTES:
        raise ApplicationInputError("project path exceeds the byte bound")
    if _UNSAFE_COMPONENTS.search(raw):
        raise ApplicationInputError("project path contains a prohibited character")
    base = launch_cwd if launch_cwd is not None else get_launch_cwd()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve()
    except Exception as exc:
        raise ApplicationInputError(f"project path cannot be resolved: {exc}") from exc
    return resolved


def list_child_directories(directory: Path) -> list[Path]:
    """Return sorted child directories of ``directory`` (for minimal browse)."""
    if not directory.is_dir():
        raise ApplicationInputError("browse directory is not a directory")
    children: list[Path] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    children.append(Path(entry.path).resolve())
    except OSError as exc:
        raise ApplicationInputError(f"cannot list directory: {exc}") from exc
    return sorted(children)


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


# ---------------------------------------------------------------------------
# Git validation helpers (read-only, no mutation)
# ---------------------------------------------------------------------------

def _run_git(cwd: Path, args: list[str], timeout: float = 10.0) -> str:
    """Run one Git command in ``cwd`` and return stdout stripped.

    Raises :class:`ApplicationInputError` on any failure; never touches the
    working tree.
    """
    try:
        result = subprocess.run(["git"] + args,
            stdin=subprocess.DEVNULL,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise ApplicationInputError("git is not available on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ApplicationInputError(f"git command timed out: {' '.join(args)}") from exc
    if result.returncode != 0:
        raise ApplicationInputError(
            f"git {' '.join(args)} failed: {result.stderr.strip()[:400] or result.stdout.strip()[:400]}"
        )
    return result.stdout.strip()


def is_git_worktree(path: Path) -> bool:
    """Whether ``path`` is inside a Git working tree (read-only)."""
    try:
        out = _run_git(path, ["rev-parse", "--is-inside-work-tree"])
        return out == "true"
    except ApplicationInputError:
        return False


def get_git_root(path: Path) -> Path:
    """Resolve the repository root (``git rev-parse --show-toplevel``)."""
    out = _run_git(path, ["rev-parse", "--show-toplevel"])
    root = Path(out).resolve()
    if not root.is_dir():
        raise ApplicationInputError("git repository root is not a directory")
    return root


def get_head_commit(repo_root: Path) -> str:
    """Verify HEAD exists and return its full SHA (``git rev-parse HEAD``)."""
    sha = _run_git(repo_root, ["rev-parse", "HEAD"])
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ApplicationInputError("HEAD is not a valid commit SHA")
    # Also verify it resolves: if repo has no commits, rev-parse HEAD fails above
    return sha


def has_uncommitted_changes(repo_root: Path) -> bool:
    """Whether the repository has any tracked/untracked changes."""
    # ``--porcelain`` is stable; any output => dirty (including ?? untracked)
    try:
        result = subprocess.run(["git", "status", "--porcelain"],
            stdin=subprocess.DEVNULL,
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10.0,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        raise ApplicationInputError(f"git status failed: {exc}") from exc
    if result.returncode != 0:
        raise ApplicationInputError(
            f"git status failed: {result.stderr.strip()[:400]}"
        )
    return bool(result.stdout.strip())


def get_dirty_summary(repo_root: Path) -> str:
    """One-line dirty summary for diagnostics (bounded)."""
    try:
        out = _run_git(repo_root, ["status", "--porcelain"])
        lines = out.splitlines()[:5]
        summary = "; ".join(lines)
        if len(summary) > 300:
            summary = summary[:297] + "..."
        return summary or "dirty"
    except Exception:
        return "dirty"


class LocalProjectValidationError(ApplicationInputError):
    """Typed validation failure for Local Project input."""


@dataclass(frozen=True)
class ValidatedProject:
    """One validated LOCAL GIT PROJECT (no mutation)."""

    requested_path: Path
    repo_root: Path
    head_commit: str
    dirty: bool


def validate_local_project(
    requested_path: str | os.PathLike[str],
    *,
    launch_cwd: Optional[Path] = None,
) -> ValidatedProject:
    """Validate one user-selected project path as a clean Git working tree.

    Checks (fail-closed, no mutation, no network):

    - directory exists;
    - readable;
    - is inside a Git working tree;
    - resolves repository root;
    - HEAD exists;
    - no unsupported dangerous path conditions;
    - launch-cwd resolution already handled by the caller.

    Returns :class:`ValidatedProject`.  A dirty working tree is reported
    via ``dirty=True`` but not yet rejected here; the caller enforces the
    v1 clean-repository gate with a clear message.
    """
    # Use raw string form for resolve to keep launch-cwd semantics
    if isinstance(requested_path, Path):
        raw = str(requested_path)
        resolved = requested_path.resolve() if requested_path.is_absolute() else resolve_project_path(raw, launch_cwd)
    else:
        raw = str(requested_path)
        resolved = resolve_project_path(raw, launch_cwd)

    if not resolved.exists():
        raise LocalProjectValidationError("project path not found")
    if not resolved.is_dir():
        raise LocalProjectValidationError("project path is not a directory")
    # Readability: try to list
    try:
        with os.scandir(resolved):
            pass
    except OSError as exc:
        raise LocalProjectValidationError(f"project directory is not readable: {exc}") from exc
    # Dangerous path conditions: reject filesystem root or empty, and check drive letter handling
    # Already ensured exists/is_dir; now ensure git checks
    if not is_git_worktree(resolved):
        raise LocalProjectValidationError("not a Git repository")
    try:
        repo_root = get_git_root(resolved)
    except ApplicationInputError as exc:
        raise LocalProjectValidationError(str(exc)) from exc
    try:
        head = get_head_commit(repo_root)
    except ApplicationInputError as exc:
        raise LocalProjectValidationError(f"HEAD does not exist: {exc}") from exc
    # Dangerous: repo_root must not be filesystem root
    if repo_root.parent == repo_root:
        raise LocalProjectValidationError("unsupported project location")
    dirty = has_uncommitted_changes(repo_root)
    return ValidatedProject(
        requested_path=resolved,
        repo_root=repo_root,
        head_commit=head,
        dirty=dirty,
    )


# ---------------------------------------------------------------------------
# Isolated worktree lifecycle (Git-native, no owner mutation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IsolatedWorktree:
    """One isolated session workspace derived from a Git HEAD."""

    repo_root: Path
    head_commit: str
    isolated_path: Path
    parent_tmpdir: Path


def create_isolated_worktree(
    repo_root: Path,
    head_commit: str,
) -> IsolatedWorktree:
    """Create a temporary detached Git worktree at ``head_commit``.

    Requirements:

    - original working tree is not mutated;
    - source commit is recorded;
    - isolated path is recorded;
    - cleanup is explicit and verified;
    - does NOT run ``reset --hard`` / ``clean`` / ``stash`` / ``checkout`` over
      owner files and does not touch the project's branches.

    Fail-closed on any Git error.
    """
    if not repo_root.is_dir():
        raise LocalProjectValidationError("repository root is not a directory")
    # Verify repo_root is a git worktree and head matches
    try:
        current_head = get_head_commit(repo_root)
    except ApplicationInputError as exc:
        raise LocalProjectValidationError(f"cannot read HEAD: {exc}") from exc
    if current_head != head_commit:
        raise LocalProjectValidationError(
            "source HEAD changed between validation and worktree creation"
        )
    # Create a temporary parent directory that will own the worktree
    parent = Path(tempfile.mkdtemp(prefix="agentic-debugger-local-"))
    worktree_path = parent / "worktree"
    # Ensure worktree_path does not already exist (mkdtemp parent is empty)
    if worktree_path.exists():
        raise LocalProjectValidationError("temporary worktree path already exists")
    # Attempt detached worktree at HEAD
    try:
        result = subprocess.run(["git", "worktree", "add", "--detach", str(worktree_path), head_commit],
            stdin=subprocess.DEVNULL,
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30.0,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        # Cleanup parent before failing
        try:
            shutil.rmtree(parent, ignore_errors=True)
        except Exception:
            pass
        raise LocalProjectValidationError("git is not available") from exc
    except subprocess.TimeoutExpired as exc:
        try:
            shutil.rmtree(parent, ignore_errors=True)
        except Exception:
            pass
        raise LocalProjectValidationError(f"git worktree creation timed out: {exc}") from exc
    if result.returncode != 0:
        # Cleanup parent
        try:
            shutil.rmtree(parent, ignore_errors=True)
        except Exception:
            pass
        raise LocalProjectValidationError(
            f"Git worktree creation failed: {(result.stderr.strip() or result.stdout.strip())[:500]}"
        )
    # Verify worktree exists and is inside parent
    if not worktree_path.is_dir():
        try:
            shutil.rmtree(parent, ignore_errors=True)
        except Exception:
            pass
        raise LocalProjectValidationError("isolated worktree was not created")
    # Verify worktree HEAD matches expected
    try:
        wt_head = _run_git(worktree_path, ["rev-parse", "HEAD"])
    except ApplicationInputError as exc:
        # Best-effort cleanup
        try:
            cleanup_isolated_worktree(worktree_path, repo_root)
        except Exception:
            pass
        try:
            shutil.rmtree(parent, ignore_errors=True)
        except Exception:
            pass
        raise LocalProjectValidationError(f"isolated worktree HEAD invalid: {exc}") from exc
    if wt_head != head_commit:
        try:
            cleanup_isolated_worktree(worktree_path, repo_root)
        except Exception:
            pass
        try:
            shutil.rmtree(parent, ignore_errors=True)
        except Exception:
            pass
        raise LocalProjectValidationError(
            "isolated worktree HEAD does not match the recorded source commit"
        )
    return IsolatedWorktree(
        repo_root=repo_root,
        head_commit=head_commit,
        isolated_path=worktree_path,
        parent_tmpdir=parent,
    )


def cleanup_isolated_worktree(
    isolated_path: Path,
    repo_root: Path,
) -> bool:
    """Remove the isolated worktree and verify removal.

    Returns True when verified removed, False otherwise.  Never deletes the
    owner's repository content; only operates on the isolated temporary path.

    The three-way cleanup truth is reported by the caller (NOT REQUIRED /
    VERIFIED / FAILED) — this helper returns the verified boolean.

    Tries ``git worktree remove --force`` first, then falls back to removing
    the parent temp directory and pruning.
    """
    # Try git worktree remove if the worktree still looks registered
    if isolated_path.exists():
        try:
            result = subprocess.run(["git", "worktree", "remove", "--force", str(isolated_path)],
                stdin=subprocess.DEVNULL,
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30.0,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            # If worktree remove succeeded or path already gone, continue
            if result.returncode != 0:
                # Fallback: direct removal of the worktree directory
                try:
                    if isolated_path.is_dir():
                        shutil.rmtree(isolated_path, ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            try:
                if isolated_path.is_dir():
                    shutil.rmtree(isolated_path, ignore_errors=True)
            except Exception:
                pass
        # Also prune stale worktree metadata
        try:
            subprocess.run(["git", "worktree", "prune"],
                stdin=subprocess.DEVNULL,
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10.0,
            )
        except Exception:
            pass
    # The parent tmpdir should be removed; if isolated_path was inside parent,
    # its parent may still exist
    parent = isolated_path.parent
    if parent.exists() and "agentic-debugger-local" in parent.name:
        try:
            shutil.rmtree(parent, ignore_errors=True)
        except Exception:
            pass
    # Verification
    return not isolated_path.exists()


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
# Source access sandboxing
# ---------------------------------------------------------------------------

def assert_path_inside_workspace(
    workspace_root: Path,
    logical_path: str,
) -> Path:
    """Validate a model/tool-requested source path stays inside workspace.

    Rejects:

    - absolute paths outside workspace;
    - ``..`` traversal escaping workspace;
    - symlink escape (via ``realpath`` commonpath check).

    Returns the resolved absolute path inside workspace.
    Raises :class:`ApplicationInputError` on violation.
    """
    if type(logical_path) is not str or not logical_path:
        raise ApplicationInputError("source path must be a non-empty string")
    if len(logical_path) >= 2 and logical_path[1] == ":" and logical_path[0].isalpha():
        raise ApplicationInputError("source path must not be absolute")
    if logical_path.startswith("/") or logical_path.startswith("\\"):
        raise ApplicationInputError("source path must be relative")
    parts = [p for p in logical_path.replace("\\", "/").split("/") if p]
    if ".." in parts:
        raise ApplicationInputError("source path must not contain .. traversal")
    root_real = os.path.realpath(str(workspace_root))
    # Disallow paths that try to escape via symlink: resolve candidate
    candidate = os.path.realpath(os.path.join(root_real, logical_path.replace("/", os.sep)))
    try:
        common = os.path.commonpath([root_real, candidate])
    except ValueError as exc:
        raise ApplicationInputError(f"source path escapes workspace: {logical_path}") from exc
    if os.path.normcase(common) != os.path.normcase(root_real):
        raise ApplicationInputError(f"source path escapes workspace: {logical_path}")
    return Path(candidate)


# ---------------------------------------------------------------------------
# Immutable task/session specification contract (#9)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocalProjectTaskSpec:
    """Canonical Local Project Debug session specification (one schema).

    ``to_mapping`` / ``from_mapping`` are the single persisted contract for
    ``local_project_task.json``: the app pre-writes it before the worker
    starts, the source preserves it through terminal completion, and Apply
    To Project / history-reopen read it back.  No secrets are persisted.
    """

    session_id: str
    source_repo_path: str
    source_head_commit: str
    isolated_workspace_path: str
    bug_description: str
    reproduction_command: Optional[str]
    verification_command: Optional[str]
    model_runtime: Optional[str]
    budgets: SessionBudgets
    created_at_utc: str

    def __post_init__(self) -> None:
        from agentic_debugger.application.events import validate_session_id, validate_utc_timestamp

        try:
            validate_session_id(self.session_id)
        except Exception as exc:
            raise ApplicationInputError(f"invalid session id: {exc}") from exc
        if type(self.source_repo_path) is not str or not self.source_repo_path:
            raise ApplicationInputError("source_repo_path must be a non-empty string")
        if type(self.source_head_commit) is not str or not re.fullmatch(r"[0-9a-f]{40}", self.source_head_commit):
            raise ApplicationInputError("source_head_commit must be a 40-hex SHA")
        if type(self.isolated_workspace_path) is not str or not self.isolated_workspace_path:
            raise ApplicationInputError("isolated_workspace_path must be a non-empty string")
        if type(self.bug_description) is not str or not self.bug_description.strip():
            raise ApplicationInputError("bug_description must be a non-empty string")
        if len(self.bug_description.encode("utf-8")) > 4096:
            raise ApplicationInputError("bug_description exceeds the 4 KiB bound")
        if self.reproduction_command is not None:
            if type(self.reproduction_command) is not str:
                raise ApplicationInputError("reproduction_command must be a string or null")
            if self.reproduction_command and len(self.reproduction_command.encode("utf-8")) > 2048:
                raise ApplicationInputError("reproduction_command exceeds the 2 KiB bound")
        if self.verification_command is not None:
            if type(self.verification_command) is not str:
                raise ApplicationInputError("verification_command must be a string or null")
            if self.verification_command and len(self.verification_command.encode("utf-8")) > 2048:
                raise ApplicationInputError("verification_command exceeds the 2 KiB bound")
        if self.model_runtime is not None and type(self.model_runtime) is not str:
            raise ApplicationInputError("model_runtime must be a string or null")
        if type(self.budgets) is not SessionBudgets:
            raise ApplicationInputError("budgets must be a SessionBudgets")
        try:
            validate_utc_timestamp(self.created_at_utc)
        except Exception as exc:
            raise ApplicationInputError(f"created_at_utc is invalid: {exc}") from exc

    def to_mapping(self) -> dict:
        return {
            "session_id": self.session_id,
            "source_repo_path": self.source_repo_path,
            "source_head_commit": self.source_head_commit,
            "isolated_workspace_path": self.isolated_workspace_path,
            "bug_description": self.bug_description,
            "reproduction_command": self.reproduction_command,
            "verification_command": self.verification_command,
            "model_runtime": self.model_runtime,
            "budgets": self.budgets.to_mapping(),
            "created_at_utc": self.created_at_utc,
        }

    @staticmethod
    def from_mapping(m: dict) -> "LocalProjectTaskSpec":
        if not isinstance(m, dict):
            raise ApplicationInputError("spec mapping must be a dict")
        required = {
            "session_id",
            "source_repo_path",
            "source_head_commit",
            "isolated_workspace_path",
            "bug_description",
            "reproduction_command",
            "verification_command",
            "model_runtime",
            "budgets",
            "created_at_utc",
        }
        if set(m) != required:
            raise ApplicationInputError("spec mapping fields are invalid")
        return LocalProjectTaskSpec(
            session_id=m["session_id"],
            source_repo_path=m["source_repo_path"],
            source_head_commit=m["source_head_commit"],
            isolated_workspace_path=m["isolated_workspace_path"],
            bug_description=m["bug_description"],
            reproduction_command=m.get("reproduction_command"),
            verification_command=m.get("verification_command"),
            model_runtime=m.get("model_runtime"),
            budgets=SessionBudgets(**m.get("budgets", {})),
            created_at_utc=m["created_at_utc"],
        )


def local_project_task_spec_sha256(spec: LocalProjectTaskSpec) -> str:
    """Hash the canonical semantic task contract for certificate binding."""
    if type(spec) is not LocalProjectTaskSpec:
        raise ApplicationInputError("spec must be a LocalProjectTaskSpec")
    encoded = json.dumps(
        spec.to_mapping(),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class LocalProjectVerificationCertificate:
    """Portable Apply-gate proof produced by the independent verifier.

    The certificate intentionally contains no command text, filesystem path,
    captured output, model claim, or controller classification.  It binds the
    verifier's fail-closed result to exactly one source commit and candidate
    patch while retaining only the facts required for an owner-facing Apply
    decision.
    """

    task_id: str
    session_id: str
    task_spec_sha256: str
    source_head_commit: str
    candidate_sha256: str
    status: str
    outcome: Optional[str]
    baseline_failure_reproduced: bool
    baseline_regression_passed: bool
    post_patch_reproduction_passed: bool
    regression_passed: bool
    f2p_passed: int
    f2p_total: int
    p2p_passed: int
    p2p_total: int
    verifier_workspace_cleaned: bool
    source_repo_unchanged: bool

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not self.task_id:
            raise ApplicationInputError("verification task_id must be non-empty")
        from agentic_debugger.application.events import validate_session_id

        try:
            validate_session_id(self.session_id)
        except Exception as exc:
            raise ApplicationInputError(
                f"verification session_id is invalid: {exc}"
            ) from exc
        if not re.fullmatch(r"[0-9a-f]{64}", self.task_spec_sha256):
            raise ApplicationInputError(
                "verification task_spec_sha256 must be a 64-hex SHA"
            )
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_head_commit):
            raise ApplicationInputError(
                "verification source_head_commit must be a 40-hex SHA"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.candidate_sha256):
            raise ApplicationInputError(
                "verification candidate_sha256 must be a 64-hex SHA"
            )
        if type(self.status) is not str or not self.status:
            raise ApplicationInputError("verification status must be non-empty")
        if self.outcome is not None and type(self.outcome) is not str:
            raise ApplicationInputError("verification outcome must be a string or null")
        for name in (
            "baseline_failure_reproduced",
            "baseline_regression_passed",
            "post_patch_reproduction_passed",
            "regression_passed",
            "verifier_workspace_cleaned",
            "source_repo_unchanged",
        ):
            if type(getattr(self, name)) is not bool:
                raise ApplicationInputError(f"verification {name} must be boolean")
        for name in ("f2p_passed", "f2p_total", "p2p_passed", "p2p_total"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ApplicationInputError(
                    f"verification {name} must be a non-negative integer"
                )
        if self.f2p_passed > self.f2p_total or self.p2p_passed > self.p2p_total:
            raise ApplicationInputError("verification passed counts exceed totals")

    @property
    def permits_apply(self) -> bool:
        """Whether this exact certificate proves a resolved, clean result."""
        return bool(
            self.status == "COMPLETED"
            and self.outcome == "RESOLVED"
            and self.baseline_failure_reproduced
            and self.baseline_regression_passed
            and self.post_patch_reproduction_passed
            and self.regression_passed
            and self.f2p_total == 1
            and self.f2p_passed == 1
            and self.p2p_total == 1
            and self.p2p_passed == 1
            and self.verifier_workspace_cleaned
            and self.source_repo_unchanged
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": LOCAL_PROJECT_VERIFICATION_SCHEMA_VERSION,
            "authority": LOCAL_PROJECT_VERIFICATION_AUTHORITY,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "task_spec_sha256": self.task_spec_sha256,
            "source_head_commit": self.source_head_commit,
            "candidate_sha256": self.candidate_sha256,
            "status": self.status,
            "outcome": self.outcome,
            "baseline_failure_reproduced": self.baseline_failure_reproduced,
            "baseline_regression_passed": self.baseline_regression_passed,
            "post_patch_reproduction_passed": self.post_patch_reproduction_passed,
            "regression_passed": self.regression_passed,
            "f2p_passed": self.f2p_passed,
            "f2p_total": self.f2p_total,
            "p2p_passed": self.p2p_passed,
            "p2p_total": self.p2p_total,
            "verifier_workspace_cleaned": self.verifier_workspace_cleaned,
            "source_repo_unchanged": self.source_repo_unchanged,
        }

    @staticmethod
    def from_mapping(value: Mapping[str, Any]) -> "LocalProjectVerificationCertificate":
        if not isinstance(value, Mapping):
            raise ApplicationInputError("verification certificate must be a mapping")
        required = {
            "schema_version",
            "authority",
            "task_id",
            "session_id",
            "task_spec_sha256",
            "source_head_commit",
            "candidate_sha256",
            "status",
            "outcome",
            "baseline_failure_reproduced",
            "baseline_regression_passed",
            "post_patch_reproduction_passed",
            "regression_passed",
            "f2p_passed",
            "f2p_total",
            "p2p_passed",
            "p2p_total",
            "verifier_workspace_cleaned",
            "source_repo_unchanged",
        }
        if set(value) != required:
            raise ApplicationInputError("verification certificate fields are invalid")
        if value["schema_version"] != LOCAL_PROJECT_VERIFICATION_SCHEMA_VERSION:
            raise ApplicationInputError("unsupported verification certificate version")
        if value["authority"] != LOCAL_PROJECT_VERIFICATION_AUTHORITY:
            raise ApplicationInputError("verification certificate authority is invalid")
        return LocalProjectVerificationCertificate(
            task_id=value["task_id"],
            session_id=value["session_id"],
            task_spec_sha256=value["task_spec_sha256"],
            source_head_commit=value["source_head_commit"],
            candidate_sha256=value["candidate_sha256"],
            status=value["status"],
            outcome=value["outcome"],
            baseline_failure_reproduced=value["baseline_failure_reproduced"],
            baseline_regression_passed=value["baseline_regression_passed"],
            post_patch_reproduction_passed=value["post_patch_reproduction_passed"],
            regression_passed=value["regression_passed"],
            f2p_passed=value["f2p_passed"],
            f2p_total=value["f2p_total"],
            p2p_passed=value["p2p_passed"],
            p2p_total=value["p2p_total"],
            verifier_workspace_cleaned=value["verifier_workspace_cleaned"],
            source_repo_unchanged=value["source_repo_unchanged"],
        )


def check_verification_certificate(
    certificate: LocalProjectVerificationCertificate,
    *,
    expected_task_id: str,
    expected_session_id: str,
    expected_task_spec_sha256: str,
    expected_head: str,
    patch_text: str,
) -> tuple[bool, str]:
    """Bind Apply to one journal identity, task contract, HEAD, and patch."""
    if type(certificate) is not LocalProjectVerificationCertificate:
        return False, "independent verification certificate is malformed"
    if certificate.task_id != expected_task_id:
        return False, "verification certificate belongs to a different task"
    if certificate.session_id != expected_session_id:
        return False, "verification certificate belongs to a different session"
    if certificate.task_spec_sha256 != expected_task_spec_sha256:
        return False, "verification certificate belongs to a different task contract"
    if certificate.source_head_commit != expected_head:
        return False, "verification certificate belongs to a different source commit"
    candidate_sha256 = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    if certificate.candidate_sha256 != candidate_sha256:
        return False, "verification certificate belongs to a different candidate patch"
    if not certificate.permits_apply:
        return False, "candidate is not independently verified as RESOLVED"
    return True, "verified"


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
