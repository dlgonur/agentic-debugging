"""Patch path policy and official-patch compatibility gates.

This module owns the authorization layer of the patch runtime: the
allowed/denied policy rule vocabulary (exact file vs directory), the
mandatory denied rules (tests directory and task.json), policy-entry
classification against the workspace boundary, path normalization, and
the side-effect-free official ``git apply --check`` compatibility probe
used before live treatments are judged by the official evaluator.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Optional

from agentic_debugger.runtime.exceptions import (
    PatchAuthorizationError,
    PatchValidationError,
)
from agentic_debugger.runtime.patch_apply import _TEMP_PREFIX
from agentic_debugger.runtime.workspace import TaskWorkspace


class _PolicyKind(Enum):
    EXACT_FILE = auto()
    DIRECTORY = auto()


@dataclass(frozen=True)
class _PolicyRule:
    path: str
    kind: _PolicyKind


_MANDATORY_DENIED_RULES: List[_PolicyRule] = [
    _PolicyRule(path="tests", kind=_PolicyKind.DIRECTORY),
    _PolicyRule(path="task.json", kind=_PolicyKind.EXACT_FILE),
]


def _normalize_path(p: str) -> str:
    return p.replace("\\", "/").rstrip("/")


def _classify_policy_entry(
    workspace: TaskWorkspace, entry: str
) -> _PolicyRule:
    npath = _normalize_path(entry)
    if not npath:
        raise PatchAuthorizationError(f"Empty policy entry: {entry!r}")
    if npath.startswith("/") or ".." in npath.split("/"):
        raise PatchAuthorizationError(
            f"Unsafe policy entry: {entry!r}"
        )
    try:
        resolved = workspace.resolve_path(npath, must_exist=True)
    except Exception as e:
        raise PatchAuthorizationError(
            f"Invalid policy entry {entry!r}: {e}"
        ) from e
    real = os.path.normpath(resolved)
    if not os.path.exists(real):
        raise PatchAuthorizationError(
            f"Policy entry does not exist: {entry!r}"
        )
    if os.path.islink(real):
        raise PatchAuthorizationError(
            f"Policy entry is a symlink: {entry!r}"
        )
    if os.path.isfile(real):
        return _PolicyRule(path=npath, kind=_PolicyKind.EXACT_FILE)
    if os.path.isdir(real):
        return _PolicyRule(path=npath, kind=_PolicyKind.DIRECTORY)
    raise PatchAuthorizationError(
        f"Policy entry is not a regular file or directory: {entry!r}"
    )


def _check_official_patch_compatibility(workspace_root: str, diff_text: str) -> None:
    """Require the same direct Git patch semantics used by the official evaluator.

    The repository PatchManager intentionally supports bounded context fuzz for
    ordinary debugger workflows.  The official SWE-rebench evaluator does not:
    after its optional three-way attempt, it falls back to direct ``git apply``.
    Live treatments that will be judged by that evaluator must therefore reject
    a candidate before the permissive local applier mutates the workspace.
    ``--check`` keeps this probe side-effect free.
    """

    git = shutil.which("git")
    if git is None:
        raise PatchValidationError(
            "official patch compatibility check unavailable: git executable not found"
        )

    probe_root: Optional[str] = None
    patch_path: Optional[str] = None
    try:
        # TaskWorkspace intentionally does not require a Git checkout.  Build
        # a disposable index so the check has the same direct-application
        # semantics as the official evaluator without mutating the live
        # workspace or creating repository state in it.
        probe_root = tempfile.mkdtemp(
            prefix=f"{_TEMP_PREFIX}git-check-",
            dir=os.path.dirname(workspace_root),
        )
        shutil.copytree(
            workspace_root,
            probe_root,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", f"{_TEMP_PREFIX}*"
            ),
        )
        initialized = subprocess.run(
            [git, "init", "--quiet"],
            cwd=probe_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        if initialized.returncode != 0:
            diagnostic = (initialized.stderr or initialized.stdout or "git init failed").strip()
            raise PatchValidationError(
                f"official patch compatibility check could not initialize probe: {diagnostic}"
            )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=_TEMP_PREFIX,
            suffix=".patch",
            dir=probe_root,
            delete=False,
        ) as handle:
            # The official operator adds this harmless serialization newline
            # before invoking its evaluator; mirror that normalization here.
            handle.write(diff_text if diff_text.endswith("\n") else diff_text + "\n")
            patch_path = handle.name
        checked = subprocess.run(
            [
                git,
                "apply",
                "--check",
                "--recount",
                "--ignore-space-change",
                "--whitespace=nowarn",
                patch_path,
            ],
            cwd=probe_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PatchValidationError(
            f"official patch compatibility check failed to run: {exc}"
        ) from exc
    finally:
        if patch_path is not None:
            try:
                os.unlink(patch_path)
            except OSError:
                pass
        if probe_root is not None:
            def _remove_readonly(_function, path, _exc_info):
                try:
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                    _function(path)
                except OSError:
                    pass

            shutil.rmtree(probe_root, onerror=_remove_readonly)

    if checked.returncode != 0:
        diagnostic = (checked.stderr or checked.stdout or "git apply rejected the patch").strip()
        raise PatchValidationError(
            f"patch is not compatible with official git apply: {diagnostic}"
        )
