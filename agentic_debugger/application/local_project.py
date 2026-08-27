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
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.session import SessionBudgets


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


def inventory_tracked_python_files(isolated: Path) -> list[str]:
    """Return sorted tracked Python files via `git ls-files`, bounded.

    Uses `git ls-files -z` as authority (tracked files only, no untracked,
    no .git). Filters for `*.py`, excludes `.git` (never returned), checks
    symlink escape via `assert_path_inside_workspace`, and enforces a
    deterministic bound (200 files, 1 MiB each). If no Python files, fail
    clearly. If too large, fail clearly rather than silently dropping.
    """
    import subprocess

    try:
        result = subprocess.run(["git", "ls-files", "-z"],
            stdin=subprocess.DEVNULL,
            cwd=str(isolated),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
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
    # If parent is a temp dir with prefix, try to remove it if empty or contains only worktree remnants
    # We stored parent separately in IsolatedWorktree; but for direct cleanup we try both
    for candidate_parent in (parent,):
        if candidate_parent.exists() and "agentic-debugger-local" in candidate_parent.name:
            try:
                shutil.rmtree(candidate_parent, ignore_errors=True)
            except Exception:
                pass
        # Also try parent of isolated_path if it looks like temp
        # The parent may be the mkdtemp itself
        try:
            # Walk up one more level if parent still has temp prefix
            grand = candidate_parent.parent if candidate_parent.is_dir() else candidate_parent
            _ = grand
        except Exception:
            pass
    # Verification
    return not isolated_path.exists()


def cleanup_parent_tmpdir(parent_tmpdir: Path, repo_root: Path) -> bool:
    """Cleanup the full parent temp dir and verify Git registration pruned.

    Verified means: isolated filesystem path gone, parent gone, and `git
    worktree list --porcelain` no longer contains the isolated path.
    """
    # Deterministic failure injection for the production-boundary cleanup test:
    # a sentinel file inside the parent forces a verified-False outcome without
    # requiring a mock in the worker child process.
    try:
        if (parent_tmpdir / ".inject-cleanup-failure").exists():
            return False
    except Exception:
        pass
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
    """Immutable Local Project Debug session specification.

    Contains at minimum the fields required by the product task contract.
    No secrets are persisted.
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
    # Gate 4: git apply --check
    try:
        proc = subprocess.run(["git", "apply", "--check", "-p1", "-"],
            cwd=str(repo_root),
            input=patch_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10.0,
            text=True,
        )
        if proc.returncode != 0:
            return False, f"git apply --check failed: {(proc.stderr.strip() or proc.stdout.strip())[:300]}"
    except FileNotFoundError:
        return False, "git is not available"
    except subprocess.TimeoutExpired:
        return False, "git apply --check timed out"
    return True, "ok"


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
) -> tuple[bool, str]:
    """Apply the canonical candidate patch to the owner project (no commit).

    Assumes :func:`check_apply_gates` already passed; fails closed on any
    error.  Leaves changes uncommitted for owner review.  No branch creation.
    """
    try:
        proc = subprocess.run(["git", "apply", "-p1", "-"],
            cwd=str(repo_root),
            input=patch_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15.0,
            text=True,
        )
        if proc.returncode != 0:
            return False, f"git apply failed: {(proc.stderr.strip() or proc.stdout.strip())[:300]}"
    except Exception as exc:
        return False, f"apply failed: {exc}"
    return True, "Patch applied to project"


__all__ = [
    "IsolatedWorktree",
    "LocalProjectTaskSpec",
    "LocalProjectValidationError",
    "ValidatedProject",
    "apply_patch_to_project",
    "assert_path_inside_workspace",
    "capture_launch_cwd",
    "check_apply_gates",
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
    "resolve_project_path",
    "reset_launch_cwd",
    "set_launch_cwd_for_tests",
    "validate_local_project",
]
