import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from agentic_debugger.runtime.exceptions import WorkspaceError
from agentic_debugger.runtime.workspace import TaskWorkspace


@pytest.fixture
def source_dir():
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "hello.py").write_text("print('hello')\n")
        (tmp / "subdir").mkdir()
        (tmp / "subdir" / "util.py").write_text("def util(): return 42\n")
        (tmp / "__pycache__").mkdir()
        (tmp / "__pycache__" / "cached.pyc").write_text("cache")
        (tmp / "ignored.pyc").write_text("bytecode")
        yield str(tmp)
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


@pytest.fixture
def single_file():
    tmp = Path(tempfile.mkdtemp())
    try:
        p = str(tmp / "file.txt")
        open(p, "w").close()
        yield p
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


class TestTaskWorkspaceCreate:
    def test_source_is_copied(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            assert os.path.isdir(ws.root)
            assert os.path.isfile(os.path.join(ws.root, "hello.py"))
            assert os.path.isfile(os.path.join(ws.root, "subdir", "util.py"))

    def test_source_files_remain_unchanged_when_workspace_edited(
        self, source_dir
    ):
        with TaskWorkspace(source_dir) as ws:
            ws_file = os.path.join(ws.root, "hello.py")
            with open(ws_file, "w") as f:
                f.write("changed\n")
            src_file = os.path.join(source_dir, "hello.py")
            with open(src_file) as f:
                assert f.read() == "print('hello')\n"

    def test_ignored_cache_not_copied(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            assert not os.path.exists(
                os.path.join(ws.root, "__pycache__")
            )
            assert not os.path.exists(
                os.path.join(ws.root, "ignored.pyc")
            )

    def test_explicit_cleanup_removes_workspace(self, source_dir):
        ws = TaskWorkspace(source_dir)
        root = ws.root
        assert os.path.isdir(root)
        ws.cleanup()
        assert not os.path.exists(root)

    def test_context_manager_cleans_up(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            root = ws.root
            assert os.path.isdir(root)
        assert not os.path.exists(root)

    def test_repeated_cleanup_is_safe(self, source_dir):
        ws = TaskWorkspace(source_dir)
        ws.cleanup()
        ws.cleanup()

    def test_missing_source_rejected(self):
        with pytest.raises(WorkspaceError, match="does not exist"):
            TaskWorkspace("/nonexistent/path/that/does/not/exist")

    def test_nondirectory_source_rejected(self, single_file):
        with pytest.raises(
            WorkspaceError, match="does not exist or is not a directory"
        ):
            TaskWorkspace(single_file)

    def test_invalid_parent_rejected(self, source_dir):
        with pytest.raises(WorkspaceError, match="parent_dir.*not a directory"):
            TaskWorkspace(source_dir, parent_dir="/nonexistent/parent")

    def test_non_string_source_rejected(self):
        with pytest.raises(WorkspaceError, match="non-empty string"):
            TaskWorkspace(123)

    def test_empty_source_rejected(self):
        with pytest.raises(WorkspaceError, match="non-empty string"):
            TaskWorkspace("")

    def test_callable_parent_provided(self, source_dir):
        parent = tempfile.mkdtemp()
        try:
            with TaskWorkspace(source_dir, parent_dir=parent) as ws:
                assert os.path.isdir(ws.root)
                assert ws.root.startswith(parent)
                assert os.path.isfile(os.path.join(ws.root, "hello.py"))
        finally:
            shutil.rmtree(parent, ignore_errors=True)


class TestTaskWorkspaceResolvePath:
    def test_valid_relative_path(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            resolved = ws.resolve_path("hello.py")
            assert os.path.isfile(resolved)
            assert resolved == os.path.join(ws.root, "hello.py")

    def test_valid_nested_path(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            resolved = ws.resolve_path("subdir/util.py")
            assert os.path.isfile(resolved)

    def test_dot_path(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            resolved = ws.resolve_path(".")
            assert os.path.isdir(resolved)
            assert resolved == os.path.normpath(ws.root)

    def test_must_exist_ok(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            resolved = ws.resolve_path("hello.py", must_exist=True)
            assert os.path.isfile(resolved)

    def test_must_exist_fails(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            with pytest.raises(WorkspaceError, match="does not exist"):
                ws.resolve_path("nonexistent.py", must_exist=True)

    def test_must_exist_not_required_ok(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            resolved = ws.resolve_path("nonexistent.py")
            assert resolved == os.path.join(ws.root, "nonexistent.py")
            assert not os.path.exists(resolved)

    def test_absolute_path_unix_rejected(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            with pytest.raises(WorkspaceError, match="Absolute paths"):
                ws.resolve_path("/etc/passwd")

    def test_absolute_path_windows_drive_rejected(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            with pytest.raises(WorkspaceError, match="Absolute paths"):
                ws.resolve_path("C:\\Windows\\system32")

    def test_absolute_path_leading_backslash_rejected(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            with pytest.raises(WorkspaceError, match="Absolute paths"):
                ws.resolve_path("\\etc\\passwd")

    def test_traversal_rejected(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            with pytest.raises(WorkspaceError, match="Path traversal"):
                ws.resolve_path("../outside")

    def test_deep_traversal_rejected(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            with pytest.raises(WorkspaceError, match="Path traversal"):
                ws.resolve_path("subdir/../../outside")

    def test_forward_slash_normalized(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            resolved = ws.resolve_path("subdir/util.py")
            assert os.path.isfile(resolved)

    def test_backslash_normalized_on_windows(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            resolved = ws.resolve_path("subdir\\util.py")
            assert os.path.isfile(resolved)

    def test_empty_path_rejected(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            with pytest.raises(WorkspaceError, match="non-empty"):
                ws.resolve_path("")

    def test_non_string_path_rejected(self, source_dir):
        with TaskWorkspace(source_dir) as ws:
            with pytest.raises(WorkspaceError, match="non-empty"):
                ws.resolve_path(123)


class TestStripTrailingSeparators:
    def test_posix_root_preserved(self):
        from agentic_debugger.runtime.workspace import _strip_trailing_separators
        assert _strip_trailing_separators("/") == "/"

    def test_normal_trailing_separator_removed(self):
        from agentic_debugger.runtime.workspace import _strip_trailing_separators
        result = _strip_trailing_separators("/tmp/source/")
        assert result == "/tmp/source"
        assert not result.endswith("/")

    def test_windows_drive_root_preserved(self):
        from agentic_debugger.runtime.workspace import _strip_trailing_separators
        if sys.platform == "win32":
            assert _strip_trailing_separators("C:\\") == "C:\\"
            assert _strip_trailing_separators("C:\\source\\") == "C:\\source"
            assert (
                _strip_trailing_separators("\\\\server\\share\\")
                == "\\\\server\\share\\"
            )
        else:
            pytest.skip("Windows-specific drive root test")

    def test_existing_trailing_symlink_still_rejected(self, source_dir):
        parent = os.path.dirname(source_dir)
        link_dir = os.path.join(parent, "trail_sym_2")
        try:
            os.symlink(source_dir, link_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("Platform does not support symlinks")
        try:
            with pytest.raises(WorkspaceError, match="symlink"):
                TaskWorkspace(link_dir + os.sep)
        finally:
            if os.path.islink(link_dir):
                os.unlink(link_dir)


class TestTaskWorkspaceSymlinks:
    def test_source_symlink_rejected(self, source_dir):
        symlink_path = os.path.join(source_dir, "link_to_root")
        src = os.path.dirname(source_dir)
        try:
            os.symlink(src, symlink_path, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("Platform does not support symlinks")
        with pytest.raises(WorkspaceError, match="symlink"):
            TaskWorkspace(source_dir)

    def test_source_symlink_file_rejected(self, source_dir):
        link_path = os.path.join(source_dir, "secret_link")
        try:
            os.symlink("/etc/passwd", link_path)
        except (OSError, NotImplementedError):
            pytest.skip("Platform does not support symlinks")
        with pytest.raises(WorkspaceError, match="symlink"):
            TaskWorkspace(source_dir)

    def test_source_root_is_symlink_rejected(self, source_dir):
        parent = os.path.dirname(source_dir)
        link_dir = os.path.join(parent, "source_link")
        try:
            os.symlink(source_dir, link_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("Platform does not support symlinks")
        try:
            with pytest.raises(WorkspaceError, match="symlink"):
                TaskWorkspace(link_dir)
        finally:
            if os.path.islink(link_dir):
                os.unlink(link_dir)

    def test_source_symlink_with_trailing_separator_rejected(self, source_dir):
        parent = os.path.dirname(source_dir)
        link_dir = os.path.join(parent, "trailing_link")
        try:
            os.symlink(source_dir, link_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("Platform does not support symlinks")
        try:
            with pytest.raises(WorkspaceError, match="symlink"):
                TaskWorkspace(link_dir + os.sep)
        finally:
            if os.path.islink(link_dir):
                os.unlink(link_dir)

    def test_intermediate_symlink_escape_rejected(self, source_dir):
        outside_dir = tempfile.mkdtemp()
        try:
            with TaskWorkspace(source_dir) as ws:
                link_path = os.path.join(ws.root, "escape_link")
                try:
                    os.symlink(outside_dir, link_path, target_is_directory=True)
                except (OSError, NotImplementedError):
                    pytest.skip("Platform does not support symlinks")
                with pytest.raises(WorkspaceError, match="escapes workspace root"):
                    ws.resolve_path("escape_link/secret.txt")
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)
