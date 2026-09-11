"""Model-authored patch runtime: application authority and result contracts.

This module is the public surface of the patch runtime.  It owns:

- the result/record contracts (:class:`PatchFileChange`,
  :class:`PatchApplyResult`, :class:`CanonicalPatchArtifact`,
  :class:`PatchSnapshot`, :class:`SyntaxFileResult`,
  :class:`SyntaxCheckResult`);
- :class:`PatchManager` — the single patch application/revert authority
  over a disposable :class:`~agentic_debugger.runtime.workspace.TaskWorkspace`,
  enforcing the allowed-path policy and producing verified apply/revert
  results with rollback semantics and syntax checks;
- :func:`materialize_and_canonicalize_patch` — the raw-vs-canonical patch
  proof: a tolerant apply is serialized by Git and strictly re-applied to
  prove byte-for-byte equivalence, never rewriting the raw text;
- :func:`build_bounded_patch_failure_payload` — the bounded, sanitized,
  JSON-safe model feedback construction for recoverable patch failures.

Architecture (one-way dependency graph):

* :mod:`agentic_debugger.runtime.patch_diff` — strict unified-diff
  parsing, path-shape authorization, hunk count validation;
* :mod:`agentic_debugger.runtime.patch_apply` — hunk anchoring/application
  with bounded fuzz, encoding detection, atomic temp-file I/O;
* :mod:`agentic_debugger.runtime.patch_policy` — allowed/denied policy
  rules and the official-patch compatibility probe;
* this module — orchestration, records, canonicalization, and bounded
  failure payloads.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from agentic_debugger.runtime.exceptions import (
    PatchApplyError,
    PatchAuthorizationError,
    PatchRevertError,
    PatchStateError,
    PatchValidationError,
)
from agentic_debugger.runtime.patch_apply import (
    _TEMP_PREFIX,
    _apply_hunks,
    _detect_encoding,
    _replace_temp_file,
    _verify_file_hash,
    _write_temp_file,
)
from agentic_debugger.runtime.patch_diff import _parse_unified_diff
from agentic_debugger.runtime.patch_policy import (
    _MANDATORY_DENIED_RULES,
    _PolicyKind,
    _PolicyRule,
    _check_official_patch_compatibility,
    _classify_policy_entry,
    _normalize_path,
)
from agentic_debugger.runtime.workspace import TaskWorkspace

__all__ = [
    "CanonicalPatchArtifact",
    "PatchApplyResult",
    "PatchFileChange",
    "PatchManager",
    "PatchSnapshot",
    "PatchValidationError",
    "SyntaxCheckResult",
    "SyntaxFileResult",
    "build_bounded_patch_failure_payload",
    "materialize_and_canonicalize_patch",
]


@dataclass(frozen=True)
class PatchFileChange:
    path: str
    hunks_applied: int

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "hunks_applied": self.hunks_applied,
        }


@dataclass(frozen=True)
class PatchApplyResult:
    success: bool
    changed_files: List[PatchFileChange]
    hunk_count: int
    before_sha256: Dict[str, str]
    after_sha256: Dict[str, str]
    bytes_before: Dict[str, int]
    bytes_after: Dict[str, int]
    error: Optional[str]
    # Per-hunk context-location adjustments applied by bounded fuzz:
    # (1-based hunk index, line displacement from the declared position).
    # Empty when every hunk applied at its declared position.
    hunk_adjustments: Tuple[Tuple[int, int], ...] = ()

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "changed_files": [c.to_mapping() for c in self.changed_files],
            "hunk_count": self.hunk_count,
            "before_sha256": dict(self.before_sha256),
            "after_sha256": dict(self.after_sha256),
            "bytes_before": dict(self.bytes_before),
            "bytes_after": dict(self.bytes_after),
            "error": self.error,
            "hunk_adjustments": [list(item) for item in self.hunk_adjustments],
        }


@dataclass(frozen=True)
class CanonicalPatchArtifact:
    """A deterministic Git serialization of an accepted workspace state."""

    patch: str
    changed_paths: Tuple[str, ...]
    before_sha256: Dict[str, str]
    after_sha256: Dict[str, str]
    raw_patch_sha256: str
    canonical_patch_sha256: str
    raw_apply: PatchApplyResult
    semantic_equivalent: bool

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "artifact_name": "candidate-official.patch",
            "patch": self.patch,
            "changed_paths": list(self.changed_paths),
            "before_sha256": dict(self.before_sha256),
            "after_sha256": dict(self.after_sha256),
            "raw_patch_sha256": self.raw_patch_sha256,
            "canonical_patch_sha256": self.canonical_patch_sha256,
            "raw_apply": self.raw_apply.to_mapping(),
            "semantic_equivalent": self.semantic_equivalent,
        }


@dataclass(frozen=True)
class PatchSnapshot:
    files: Dict[str, bytes]
    before_hashes: Dict[str, str]
    after_hashes: Dict[str, str]


@dataclass(frozen=True)
class SyntaxFileResult:
    path: str
    success: bool
    error_type: Optional[str]
    message: Optional[str]
    line: Optional[int]
    column: Optional[int]

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "success": self.success,
            "error_type": self.error_type,
            "message": self.message,
            "line": self.line,
            "column": self.column,
        }


@dataclass(frozen=True)
class SyntaxCheckResult:
    results: List[SyntaxFileResult]
    all_passed: bool

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "results": [r.to_mapping() for r in self.results],
            "all_passed": self.all_passed,
        }


class PatchManager:
    def __init__(
        self,
        workspace: TaskWorkspace,
        allowed_paths: List[str],
        denied_paths: List[str],
        *,
        official_patch_compatibility: bool = False,
    ) -> None:
        self._workspace = workspace
        self._official_patch_compatibility = official_patch_compatibility

        denied_rules: List[_PolicyRule] = []
        for p in denied_paths:
            if not p.strip():
                raise PatchAuthorizationError("Empty policy entry")
            if p == "tests" or p == "task.json":
                continue
            denied_rules.append(_classify_policy_entry(workspace, p))

        denied_rules.extend(_MANDATORY_DENIED_RULES)

        allowed_rules: List[_PolicyRule] = []
        for p in allowed_paths:
            if not p.strip():
                raise PatchAuthorizationError("Empty policy entry")
            allowed_rules.append(_classify_policy_entry(workspace, p))

        self._denied = denied_rules
        self._allowed = allowed_rules
        self._snapshot: Optional[PatchSnapshot] = None

    @property
    def has_active_patch(self) -> bool:
        return self._snapshot is not None

    def _authorize_path(self, path: str) -> None:
        npath = _normalize_path(path)

        for rule in self._denied:
            if rule.kind is _PolicyKind.EXACT_FILE:
                if npath == rule.path:
                    raise PatchAuthorizationError(
                        f"Path is denied: {path!r}",
                        path=path,
                        error_kind="authorization_error",
                        recoverable=False,
                    )
            else:
                if npath == rule.path or npath.startswith(rule.path + "/"):
                    raise PatchAuthorizationError(
                        f"Path is inside denied directory: {path!r}",
                        path=path,
                        error_kind="authorization_error",
                        recoverable=False,
                    )

        allowed = False
        for rule in self._allowed:
            if rule.kind is _PolicyKind.EXACT_FILE:
                if npath == rule.path:
                    allowed = True
                    break
            else:
                if npath == rule.path or npath.startswith(rule.path + "/"):
                    allowed = True
                    break

        if not allowed:
            raise PatchAuthorizationError(
                f"Path is not allowed: {path!r}",
                path=path,
                error_kind="authorization_error",
                recoverable=False,
            )

    def apply_patch(self, diff_text: str) -> PatchApplyResult:
        if self._snapshot is not None:
            raise PatchStateError(
                "Active patch exists; must revert before applying a new one",
                error_kind="active_patch_conflict",
                recoverable=False,
            )

        file_patches = _parse_unified_diff(diff_text)

        for fp in file_patches:
            self._authorize_path(fp.path)

        if self._official_patch_compatibility:
            _check_official_patch_compatibility(self._workspace.root, diff_text)

        originals: Dict[str, bytes] = {}
        encoding_map: Dict[str, str] = {}
        for fp in file_patches:
            resolved = self._workspace.resolve_path(
                fp.path, must_exist=True
            )
            if not os.path.isfile(resolved):
                raise PatchApplyError(
                    f"Path is not a regular file: {fp.path!r}",
                    path=fp.path,
                    error_kind="invalid_file_type",
                    recoverable=False,
                )
            if os.path.islink(resolved):
                raise PatchApplyError(
                    f"Path is a symlink: {fp.path!r}",
                    path=fp.path,
                    error_kind="invalid_file_type",
                    recoverable=False,
                )
            if fp.path.endswith(".py"):
                encoding_map[fp.path], _ = _detect_encoding(resolved)
            else:
                with open(resolved, "rb") as sniff:
                    raw = sniff.read(4096)
                if raw[:3] == b"\xef\xbb\xbf":
                    encoding_map[fp.path] = "utf-8-sig"
                else:
                    encoding_map[fp.path] = "utf-8"
            with open(resolved, "rb") as f:
                originals[fp.path] = f.read()

        new_contents: Dict[str, bytes] = {}
        hunk_adjustments: List[Tuple[int, int]] = []
        for fp in file_patches:
            encoding = encoding_map[fp.path]
            original_bytes = originals[fp.path]
            try:
                original_text = original_bytes.decode(encoding)
            except (UnicodeDecodeError, LookupError) as e:
                raise PatchApplyError(
                    f"Cannot decode {fp.path!r} with encoding {encoding!r}: {e}",
                    path=fp.path,
                    error_kind="decode_error",
                    recoverable=False,
                ) from e
            new_text, adjustments = _apply_hunks(
                original_text, fp.hunks, path=fp.path
            )
            hunk_adjustments.extend(
                (fp.path, hunk_idx, displacement)
                for hunk_idx, displacement in adjustments
            )
            try:
                new_contents[fp.path] = new_text.encode(encoding)
            except (UnicodeEncodeError, LookupError) as e:
                raise PatchApplyError(
                    f"Cannot encode patched {fp.path!r} with {encoding!r}: {e}",
                    path=fp.path,
                    error_kind="encode_error",
                    recoverable=False,
                ) from e

        before_hashes: Dict[str, str] = {}
        after_hashes: Dict[str, str] = {}
        for p in originals:
            before_hashes[p] = hashlib.sha256(
                originals[p]
            ).hexdigest()
            after_hashes[p] = hashlib.sha256(
                new_contents[p]
            ).hexdigest()

        written: List[str] = []
        replaced: List[str] = []
        rollback_ok = True
        try:
            for path in sorted(new_contents.keys()):
                resolved = self._workspace.resolve_path(path)
                tmp_path, _ = _write_temp_file(resolved, new_contents[path])
                written.append(path)
                _replace_temp_file(tmp_path, resolved)
                replaced.append(path)
                _verify_file_hash(resolved, new_contents[path])
        except Exception as e:
            for path in reversed(written):
                if path in replaced:
                    try:
                        resolved = self._workspace.resolve_path(path)
                        tmp_path, _ = _write_temp_file(
                            resolved, originals[path]
                        )
                        _replace_temp_file(tmp_path, resolved)
                        _verify_file_hash(resolved, originals[path])
                    except Exception:
                        rollback_ok = False
                else:
                    pass
            if rollback_ok:
                raise PatchApplyError(
                    f"Patch write failed, all changes rolled back: {e}",
                    error_kind="write_failure",
                    recoverable=False,
                ) from e
            else:
                raise PatchApplyError(
                    f"Patch write failed, partial rollback completed. "
                    f"Rollback of some files also failed: {e}",
                    error_kind="rollback_failure",
                    recoverable=False,
                ) from e

        self._snapshot = PatchSnapshot(
            files=originals,
            before_hashes=before_hashes,
            after_hashes=after_hashes,
        )

        changed = [
            PatchFileChange(path=fp.path, hunks_applied=len(fp.hunks))
            for fp in file_patches
        ]
        total_hunks = sum(len(fp.hunks) for fp in file_patches)

        return PatchApplyResult(
            success=True,
            changed_files=changed,
            hunk_count=total_hunks,
            before_sha256=before_hashes,
            after_sha256=after_hashes,
            bytes_before={k: len(v) for k, v in originals.items()},
            bytes_after={k: len(v) for k, v in new_contents.items()},
            error=None,
            hunk_adjustments=tuple(hunk_adjustments),
        )

    def revert_patch(self) -> PatchApplyResult:
        if self._snapshot is None:
            raise PatchStateError("No active patch to revert", error_kind="state_error", recoverable=False)

        snapshot = self._snapshot

        verify_hashes: Dict[str, str] = {}
        pre_revert_bytes: Dict[str, bytes] = {}
        for path in snapshot.files:
            resolved = self._workspace.resolve_path(path, must_exist=True)
            with open(resolved, "rb") as f:
                current = f.read()
            pre_revert_bytes[path] = current
            verify_hashes[path] = hashlib.sha256(current).hexdigest()

        written: List[str] = []
        replaced: List[str] = []
        rollback_ok = True
        try:
            for path in sorted(snapshot.files.keys()):
                resolved = self._workspace.resolve_path(path)
                tmp_path, _ = _write_temp_file(
                    resolved, snapshot.files[path]
                )
                written.append(path)
                _replace_temp_file(tmp_path, resolved)
                replaced.append(path)
                _verify_file_hash(resolved, snapshot.files[path])
        except Exception as e:
            # Roll back to the PRE-REVERT (patched) bytes: re-writing the
            # snapshot bytes would leave already-reverted files mixed with
            # still-patched files while claiming the snapshot was preserved.
            for path in reversed(written):
                if path in replaced:
                    try:
                        resolved = self._workspace.resolve_path(path)
                        tmp_path, _ = _write_temp_file(
                            resolved, pre_revert_bytes[path]
                        )
                        _replace_temp_file(tmp_path, resolved)
                        _verify_file_hash(resolved, pre_revert_bytes[path])
                    except Exception:
                        rollback_ok = False
            if rollback_ok:
                raise PatchRevertError(
                    f"Revert write failed, workspace restored to the pre-revert state: {e}",
                    path=path if written else None,
                    error_kind="revert_failure",
                    recoverable=False,
                ) from e
            raise PatchRevertError(
                f"Revert write failed, partial rollback completed "
                f"(some files may remain reverted): {e}",
                path=path if written else None,
                error_kind="revert_partial_rollback",
                recoverable=False,
            ) from e

        restored_hashes: Dict[str, str] = {}
        for path in snapshot.files:
            resolved = self._workspace.resolve_path(path, must_exist=True)
            with open(resolved, "rb") as f:
                restored = f.read()
            restored_hashes[path] = hashlib.sha256(restored).hexdigest()

        for path, expected in snapshot.before_hashes.items():
            actual = restored_hashes.get(path)
            if actual != expected:
                raise PatchRevertError(
                    f"Hash mismatch after revert for {path!r}: "
                    f"expected {expected}, got {actual}",
                    path=path,
                    error_kind="revert_hash_mismatch",
                    recoverable=False,
                )

        self._snapshot = None

        return PatchApplyResult(
            success=True,
            changed_files=[
                PatchFileChange(path=p, hunks_applied=0)
                for p in snapshot.files
            ],
            hunk_count=0,
            before_sha256=dict(verify_hashes),
            after_sha256=dict(restored_hashes),
            bytes_before={
                k: len(v) for k, v in snapshot.files.items()
            },
            bytes_after={
                k: len(v) for k, v in snapshot.files.items()
            },
            error=None,
            hunk_adjustments=(),
        )

    def syntax_check(
        self, paths: Optional[List[str]] = None
    ) -> SyntaxCheckResult:
        if paths is None:
            if self._snapshot is None:
                raise PatchStateError(
                    "No active patch; explicit paths required"
                )
            paths = sorted(self._snapshot.files.keys())

        results: List[SyntaxFileResult] = []
        all_passed = True

        for path in paths:
            resolved = self._workspace.resolve_path(path, must_exist=True)
            if not os.path.isfile(resolved):
                raise PatchApplyError(
                    f"Path is not a regular file: {path!r}"
                )
            with open(resolved, "rb") as f:
                source = f.read()

            if not path.endswith(".py"):
                results.append(
                    SyntaxFileResult(
                        path=path,
                        success=True,
                        error_type=None,
                        message="Non-Python file skipped",
                        line=None,
                        column=None,
                    )
                )
                continue

            result = _check_python_syntax(path, source)
            if not result.success:
                all_passed = False
            results.append(result)

        return SyntaxCheckResult(
            results=results, all_passed=all_passed
        )


def _check_python_syntax(
    path: str, source: bytes
) -> SyntaxFileResult:
    try:
        compile(source, path, "exec", dont_inherit=True)
        return SyntaxFileResult(
            path=path,
            success=True,
            error_type=None,
            message=None,
            line=None,
            column=None,
        )
    except SyntaxError as e:
        return SyntaxFileResult(
            path=path,
            success=False,
            error_type="SyntaxError",
            message=str(e),
            line=e.lineno,
            column=e.offset,
        )
    except ValueError as e:
        return SyntaxFileResult(
            path=path,
            success=False,
            error_type="ValueError",
            message=str(e),
            line=None,
            column=None,
        )


def _snapshot_workspace_files(root: str) -> Dict[str, bytes]:
    """Return a complete, deterministic snapshot of regular workspace files."""

    snapshot: Dict[str, bytes] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            path = os.path.join(directory, name)
            if os.path.islink(path):
                raise PatchValidationError(
                    f"workspace snapshot encountered a symlink: {path!r}"
                )
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            with open(path, "rb") as handle:
                snapshot[relative] = handle.read()
    return snapshot


def _copy_workspace_contents(source: str, destination: str) -> None:
    """Copy a symlink-free workspace tree without copying repository metadata."""

    for name in os.listdir(destination):
        if name == ".git":
            continue
        target = os.path.join(destination, name)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.unlink(target)
    for name in os.listdir(source):
        source_path = os.path.join(source, name)
        destination_path = os.path.join(destination, name)
        if os.path.islink(source_path):
            raise PatchValidationError(
                f"workspace canonicalization encountered a symlink: {source_path!r}"
            )
        if os.path.isdir(source_path):
            shutil.copytree(source_path, destination_path, symlinks=False)
        else:
            shutil.copy2(source_path, destination_path)
            # copy2 preserves the source mtime.  Refreshing the worktree
            # timestamp ensures Git does not trust a stale index stat cache
            # when a same-sized file changed in the disposable tree.
            os.utime(destination_path, None)


def _run_git_checked(argv: List[str], *, cwd: str, timeout: float = 30.0) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PatchValidationError(
            f"canonical Git operation could not run: {exc}"
        ) from exc
    return result


def materialize_and_canonicalize_patch(
    pristine_source: str,
    raw_patch: str,
    allowed_paths: List[str],
    denied_paths: List[str],
    *,
    parent_dir: Optional[str] = None,
) -> CanonicalPatchArtifact:
    """Materialize a raw candidate, then prove its canonical Git equivalent.

    The raw text is never rewritten.  A tolerant ``PatchManager`` is the only
    component allowed to interpret it; Git is used solely to serialize and
    strictly re-apply the resulting workspace state.
    """

    if type(raw_patch) is not str or not raw_patch.strip():
        raise PatchValidationError("raw candidate patch must be a non-empty string")
    if "\x00" in raw_patch:
        raise PatchValidationError("raw candidate patch contains a NUL byte")
    if parent_dir is not None and not os.path.isdir(parent_dir):
        raise PatchValidationError("canonicalization parent directory is unavailable")

    with tempfile.TemporaryDirectory(
        prefix=f"{_TEMP_PREFIX}canonical-",
        dir=parent_dir,
    ) as temporary:
        raw_workspace = TaskWorkspace(pristine_source, parent_dir=temporary)
        strict_workspace = TaskWorkspace(pristine_source, parent_dir=temporary)
        manager = PatchManager(
            raw_workspace,
            allowed_paths,
            denied_paths,
            official_patch_compatibility=False,
        )
        applied = manager.apply_patch(raw_patch)
        before = _snapshot_workspace_files(strict_workspace.root)
        materialized = _snapshot_workspace_files(raw_workspace.root)
        changed_paths = tuple(
            sorted(
                path
                for path in set(before) | set(materialized)
                if before.get(path) != materialized.get(path)
            )
        )
        if not changed_paths:
            raise PatchValidationError("raw candidate produced no material workspace delta")
        if set(changed_paths) != {item.path for item in applied.changed_files}:
            raise PatchValidationError(
                "raw candidate changed-path evidence disagrees with the workspace delta"
            )
        for path in changed_paths:
            manager._authorize_path(path)

        repo_root = os.path.join(temporary, "canonical-git-repo")
        shutil.copytree(strict_workspace.root, repo_root)
        initialized = _run_git_checked(["git", "init", "--quiet"], cwd=repo_root)
        if initialized.returncode != 0:
            raise PatchValidationError(
                "canonical Git repository initialization failed: "
                + (initialized.stderr or initialized.stdout).decode("utf-8", "replace").strip()
            )
        for key, value in (
            ("user.name", "agentic-debugger"),
            ("user.email", "agentic-debugger@localhost"),
            ("core.autocrlf", "false"),
        ):
            configured = _run_git_checked(["git", "config", key, value], cwd=repo_root)
            if configured.returncode != 0:
                raise PatchValidationError("canonical Git identity configuration failed")
        added = _run_git_checked(["git", "add", "--all"], cwd=repo_root)
        if added.returncode != 0:
            detail = (added.stderr or added.stdout).decode("utf-8", "replace").strip()
            raise PatchValidationError(
                "canonical Git baseline indexing failed" + (f": {detail}" if detail else "")
            )
        committed = _run_git_checked(
            ["git", "commit", "--quiet", "-m", "pristine baseline"],
            cwd=repo_root,
        )
        if committed.returncode != 0:
            raise PatchValidationError("canonical Git baseline commit failed")
        _copy_workspace_contents(raw_workspace.root, repo_root)
        diff_result = _run_git_checked(
            [
                "git", "diff", "--binary", "--full-index", "--no-ext-diff",
                "--no-renames", "--unified=3", "--src-prefix=a/", "--dst-prefix=b/", "--",
            ],
            cwd=repo_root,
        )
        if diff_result.returncode not in (0, 1):
            raise PatchValidationError(
                "canonical Git diff failed: "
                + (diff_result.stderr or diff_result.stdout).decode("utf-8", "replace").strip()
            )
        try:
            canonical = diff_result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchValidationError("canonical Git diff is not UTF-8 text") from exc
        if not canonical.strip():
            raise PatchValidationError("canonical Git diff is empty")

        patch_path = os.path.join(temporary, "candidate-official.patch")
        with open(patch_path, "wb") as handle:
            handle.write(canonical.encode("utf-8"))
        checked = _run_git_checked(
            ["git", "-c", "core.autocrlf=false", "apply", "--check", patch_path],
            cwd=str(strict_workspace.root),
        )
        if checked.returncode != 0:
            raise PatchValidationError(
                "canonical patch failed strict Git apply --check: "
                + (checked.stderr or checked.stdout).decode("utf-8", "replace").strip()
            )
        applied_strictly = _run_git_checked(
            ["git", "-c", "core.autocrlf=false", "apply", patch_path],
            cwd=str(strict_workspace.root),
        )
        if applied_strictly.returncode != 0:
            raise PatchValidationError(
                "canonical patch failed strict Git apply: "
                + (applied_strictly.stderr or applied_strictly.stdout).decode("utf-8", "replace").strip()
            )
        strict_after = _snapshot_workspace_files(strict_workspace.root)
        if strict_after != materialized:
            raise PatchValidationError(
                "canonical patch is not byte-for-byte equivalent to raw materialization"
            )
        canonical_paths = _run_git_checked(
            ["git", "diff", "--name-only", "--no-renames", "--"],
            cwd=repo_root,
        )
        if canonical_paths.returncode != 0:
            raise PatchValidationError("canonical changed-path inspection failed")
        serialized_paths = tuple(
            sorted(
                line.strip()
                for line in canonical_paths.stdout.decode("utf-8").splitlines()
                if line.strip()
            )
        )
        if serialized_paths != changed_paths:
            raise PatchValidationError(
                "canonical patch changed-path set differs from raw materialization"
            )
        before_hashes = {
            path: hashlib.sha256(before[path]).hexdigest() for path in changed_paths
        }
        after_hashes = {
            path: hashlib.sha256(materialized[path]).hexdigest() for path in changed_paths
        }
        return CanonicalPatchArtifact(
            patch=canonical,
            changed_paths=changed_paths,
            before_sha256=before_hashes,
            after_sha256=after_hashes,
            raw_patch_sha256=hashlib.sha256(raw_patch.encode("utf-8")).hexdigest(),
            canonical_patch_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            raw_apply=applied,
            semantic_equivalent=True,
        )


MAX_PATCH_FAILURE_PATH_BYTES = 500
MAX_PATCH_FAILURE_KIND_BYTES = 100
MAX_PATCH_FAILURE_STRING_BYTES = 500
MAX_PATCH_FAILURE_SOURCE_BYTES = 1800
MAX_PATCH_FAILURE_TOTAL_BYTES = 4096


def _bound_patch_failure_text(text: Any, max_bytes: int) -> Optional[str]:
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        return encoded[: max(0, max_bytes - 3)].decode("utf-8", errors="ignore") + "..."
    return text


def _bound_patch_failure_source_window(
    window: Any,
    max_bytes: int = MAX_PATCH_FAILURE_SOURCE_BYTES,
    max_line_chars: int = 400,
) -> Optional[str]:
    if window is None:
        return None
    if not isinstance(window, str):
        window = str(window)
    bounded_lines = []
    for line in window.splitlines():
        if len(line) > max_line_chars:
            line = line[: max_line_chars - 3] + "..."
        bounded_lines.append(line)
    text = "\n".join(bounded_lines)
    return _bound_patch_failure_text(text, max_bytes)


def build_bounded_patch_failure_payload(
    exc: BaseException,
    *,
    error_kind: Optional[str] = None,
    recoverable: Optional[bool] = None,
) -> Tuple[Dict[str, Any], bool, str]:
    """Build a JSON-safe, bounded patch_failure payload from an exception.

    Returns (payload_data, is_recoverable, kind).
    """
    if isinstance(exc, PatchAuthorizationError):
        kind = error_kind or getattr(exc, "error_kind", "authorization_error")
        rec = False if recoverable is None else recoverable
    elif isinstance(exc, PatchStateError):
        kind = error_kind or getattr(exc, "error_kind", "state_error")
        rec = False if recoverable is None else recoverable
    elif isinstance(exc, PatchRevertError):
        kind = error_kind or getattr(exc, "error_kind", "revert_failure")
        rec = False if recoverable is None else recoverable
    elif isinstance(exc, PatchValidationError):
        kind = error_kind or getattr(exc, "error_kind", "validation_error")
        rec = getattr(exc, "recoverable", True) if recoverable is None else recoverable
    elif isinstance(exc, PatchApplyError):
        kind = error_kind or getattr(exc, "error_kind", "apply_error")
        rec = getattr(exc, "recoverable", False) if recoverable is None else recoverable
    else:
        kind = error_kind or getattr(exc, "error_kind", "patch_infrastructure_error")
        rec = False if recoverable is None else recoverable

    bounded_kind = _bound_patch_failure_text(
        kind, MAX_PATCH_FAILURE_KIND_BYTES
    ) or "patch_error"
    raw_path = getattr(exc, "path", None)
    bounded_path = _bound_patch_failure_text(
        raw_path, MAX_PATCH_FAILURE_PATH_BYTES
    )
    line_number = getattr(exc, "line_number", None)
    hunk_index = getattr(exc, "hunk_index", None)
    expected = _bound_patch_failure_text(
        getattr(exc, "expected", None), MAX_PATCH_FAILURE_STRING_BYTES
    )
    actual = _bound_patch_failure_text(
        getattr(exc, "actual", None), MAX_PATCH_FAILURE_STRING_BYTES
    )
    current_source = _bound_patch_failure_source_window(
        getattr(exc, "current_source_window", None),
        MAX_PATCH_FAILURE_SOURCE_BYTES,
    )

    patch_failure: Dict[str, Any] = {
        "kind": bounded_kind,
        "recoverable": bool(rec),
    }
    if bounded_path is not None:
        patch_failure["path"] = bounded_path
    if line_number is not None and isinstance(line_number, int) and not isinstance(line_number, bool):
        patch_failure["line_number"] = line_number
    if hunk_index is not None and isinstance(hunk_index, int) and not isinstance(hunk_index, bool):
        patch_failure["hunk_index"] = hunk_index
    if expected is not None:
        patch_failure["expected"] = expected
    if actual is not None:
        patch_failure["actual"] = actual
    if current_source is not None:
        patch_failure["current_source_window"] = current_source

    payload_data: Dict[str, Any] = {
        "applied": False,
        "error": bounded_kind,
        "recoverable": bool(rec),
        "patch_failure": patch_failure,
    }

    try:
        encoded_len = len(
            json.dumps(payload_data, ensure_ascii=False).encode("utf-8")
        )
        if encoded_len > MAX_PATCH_FAILURE_TOTAL_BYTES:
            # Degrade by shedding large source window first
            if "current_source_window" in patch_failure:
                del patch_failure["current_source_window"]
            encoded_len = len(
                json.dumps(payload_data, ensure_ascii=False).encode("utf-8")
            )
            if encoded_len > MAX_PATCH_FAILURE_TOTAL_BYTES:
                if "expected" in patch_failure:
                    del patch_failure["expected"]
                if "actual" in patch_failure:
                    del patch_failure["actual"]
    except Exception:
        # Fallback minimal payload
        minimal_pf: Dict[str, Any] = {
            "kind": bounded_kind,
            "recoverable": bool(rec),
        }
        if bounded_path is not None:
            minimal_pf["path"] = _bound_patch_failure_text(bounded_path, 200)
        if line_number is not None and isinstance(line_number, int) and not isinstance(line_number, bool):
            minimal_pf["line_number"] = line_number
        if hunk_index is not None and isinstance(hunk_index, int) and not isinstance(hunk_index, bool):
            minimal_pf["hunk_index"] = hunk_index
        payload_data = {
            "applied": False,
            "error": bounded_kind,
            "recoverable": bool(rec),
            "patch_failure": minimal_pf,
        }

    return payload_data, bool(rec), bounded_kind
