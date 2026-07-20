from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from agentic_debugger.runtime.exceptions import WorkspaceError


class TaskWorkspace:
    """A disposable workspace copied from an immutable fixture/source directory.

    The workspace is created by copying a source directory into a newly
    created temporary directory.  Commands should be executed inside the
    workspace, never directly in the source.
    """

    def __init__(
        self,
        source_path: str,
        parent_dir: Optional[str] = None,
    ) -> None:
        if not isinstance(source_path, str) or not source_path:
            raise WorkspaceError("source_path must be a non-empty string")

        stripped_source = _strip_trailing_separators(source_path)

        if os.path.islink(stripped_source):
            raise WorkspaceError(
                f"source_path is a symlink, which is not allowed: {source_path!r}"
            )

        real_source = os.path.realpath(stripped_source)
        if not os.path.isdir(real_source):
            raise WorkspaceError(
                f"source_path does not exist or is not a directory: {source_path!r}"
            )

        _check_source_has_no_symlinks(real_source)

        destination_parent: str
        if parent_dir is not None:
            if not isinstance(parent_dir, str) or not parent_dir:
                raise WorkspaceError("parent_dir must be a non-empty string")
            if not os.path.isdir(parent_dir):
                raise WorkspaceError(
                    f"parent_dir does not exist or is not a directory: {parent_dir!r}"
                )
            destination_parent = parent_dir
        else:
            destination_parent = tempfile.gettempdir()

        try:
            self._root: str = tempfile.mkdtemp(
                prefix="task_workspace_", dir=destination_parent
            )
        except OSError as e:
            raise WorkspaceError(
                f"Failed to create temporary workspace directory: {e}"
            ) from e

        self._real_root: str = os.path.realpath(self._root)

        try:
            _copy_contents(real_source, self._root)
        except Exception as e:
            self.cleanup()
            raise WorkspaceError(
                f"Failed to copy source into workspace: {e}"
            ) from e

        self._cleaned = False

    @property
    def root(self) -> str:
        return self._root

    def resolve_path(
        self,
        relative_path: str,
        *,
        must_exist: bool = False,
    ) -> str:
        if not isinstance(relative_path, str) or not relative_path:
            raise WorkspaceError("relative_path must be a non-empty string")

        if _is_absolute_path(relative_path):
            raise WorkspaceError(
                f"Absolute paths are rejected: {relative_path!r}"
            )

        native = relative_path.replace("/", os.sep).replace("\\", os.sep)
        parts = native.split(os.sep)
        if ".." in parts:
            raise WorkspaceError(
                f"Path traversal is rejected: {relative_path!r}"
            )

        resolved = os.path.normpath(os.path.join(self._root, native))
        real_resolved = os.path.realpath(resolved)

        root_norm = os.path.normpath(self._root)
        real_root_norm = os.path.normpath(self._real_root)

        if not real_resolved.startswith(real_root_norm + os.sep):
            if real_resolved != real_root_norm:
                raise WorkspaceError(
                    f"Resolved path escapes workspace root: {relative_path!r}"
                )

        common = os.path.commonpath([real_resolved, real_root_norm])
        if common != real_root_norm:
            raise WorkspaceError(
                f"Resolved path escapes workspace root: {relative_path!r}"
            )

        if must_exist and not os.path.exists(resolved):
            raise WorkspaceError(
                f"Path does not exist: {relative_path!r}"
            )

        return resolved

    def cleanup(self) -> None:
        if getattr(self, "_cleaned", False):
            return
        self._cleaned = True
        root = getattr(self, "_root", None)
        if root is not None and os.path.isdir(root):
            try:
                shutil.rmtree(root, ignore_errors=False)
            except Exception:
                pass

    def __enter__(self) -> TaskWorkspace:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_val: object,
        exc_tb: object,
    ) -> None:
        self.cleanup()


def _copy_contents(src: str, dst: str) -> None:
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.islink(s):
            raise WorkspaceError(
                f"Source contains symlink which is not allowed: {s!r}"
            )
        if item == "__pycache__" or item.endswith(".pyc"):
            continue
        if os.path.isdir(s):
            shutil.copytree(
                s, d, symlinks=False, ignore=_workspace_ignore
            )
        else:
            shutil.copy2(s, d)


def _workspace_ignore(path: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name == "__pycache__":
            ignored.add(name)
        elif name.endswith(".pyc"):
            ignored.add(name)
    return ignored


def _check_source_has_no_symlinks(source_path: str) -> None:
    for dirpath, dirnames, filenames in os.walk(
        source_path, followlinks=False, topdown=True
    ):
        clean_dirnames: list[str] = []
        for dn in dirnames:
            full = os.path.join(dirpath, dn)
            if os.path.islink(full):
                raise WorkspaceError(
                    f"Source contains symlink which is not allowed: {full!r}"
                )
            clean_dirnames.append(dn)
        dirnames[:] = clean_dirnames

        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                raise WorkspaceError(
                    f"Source contains symlink which is not allowed: {full!r}"
                )


def _strip_trailing_separators(path: str) -> str:
    if not path:
        return path
    drive, rest = os.path.splitdrive(path)
    if drive:
        if rest and all(c in (os.sep, "/") for c in rest):
            return drive + rest[:1]
        return drive + rest.rstrip(os.sep).rstrip("/")
    if all(c in (os.sep, "/") for c in path):
        return path[:1]
    return path.rstrip(os.sep).rstrip("/")


def _is_absolute_path(path: str) -> bool:
    if path.startswith("/") or path.startswith("\\"):
        return True
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return True
    if os.path.isabs(path):
        return True
    return False
