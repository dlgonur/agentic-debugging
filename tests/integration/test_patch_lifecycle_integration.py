from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from agentic_debugger.runtime.exceptions import (
    PatchApplyError,
    PatchAuthorizationError,
    PatchStateError,
)
from agentic_debugger.runtime.patcher import PatchManager, _parse_unified_diff
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.skills.file_skills import get_source_window, open_file
from agentic_debugger.skills.search_skills import (
    find_class,
    find_function,
    get_function_source,
    search_code,
)


@pytest.fixture
def lifecycle_workspace():
    tmp = Path(tempfile.mkdtemp())
    try:
        src = tmp / "source"
        src.mkdir()
        (src / "calculator.py").write_text(
            "def add(a, b):\n"
            "    return a + b\n"
            "\n"
            "def subtract(a, b):\n"
            "    return a - b\n"
        )
        (src / "tests").mkdir()
        (src / "tests" / "test_calculator.py").write_text(
            "from calculator import add, subtract\n"
            "\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n"
            "\n"
            "def test_subtract():\n"
            "    assert subtract(5, 3) == 2\n"
        )
        (src / "task.json").write_text('{"task_id": "test-001"}\n')
        ws = TaskWorkspace(str(src))
        yield ws
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


class TestPatchLifecycleIntegration:
    def test_full_patch_lifecycle(self, lifecycle_workspace):
        ws = lifecycle_workspace
        pm = PatchManager(
            ws,
            allowed_paths=["calculator.py"],
            denied_paths=["tests", "task.json"],
        )

        resolved = ws.resolve_path("calculator.py")
        original_bytes = Path(resolved).read_bytes()

        import hashlib
        original_hash = hashlib.sha256(original_bytes).hexdigest()

        diff = """\
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,3 @@
 def add(a, b):
-    return a + b
+    result = a + b
+    return result
"""
        result = pm.apply_patch(diff)
        assert result.success is True
        assert len(result.changed_files) == 1
        assert pm.has_active_patch is True

        patched_bytes = Path(resolved).read_bytes()
        assert patched_bytes != original_bytes
        assert result.before_sha256["calculator.py"] == original_hash

        revert_result = pm.revert_patch()
        assert revert_result.success is True
        assert pm.has_active_patch is False

        restored_bytes = Path(resolved).read_bytes()
        assert restored_bytes == original_bytes
        restored_hash = hashlib.sha256(restored_bytes).hexdigest()
        assert restored_hash == original_hash

    def test_authorization_rejects_tests(self, lifecycle_workspace):
        pm = PatchManager(
            lifecycle_workspace,
            allowed_paths=["calculator.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = """\
--- a/tests/test_calculator.py
+++ b/tests/test_calculator.py
@@ -1,2 +1,2 @@
 def test_add():
-    assert add(2, 3) == 5
+    assert add(2, 3) == 6
"""
        with pytest.raises(PatchAuthorizationError, match="denied"):
            pm.apply_patch(diff)

    def test_syntax_validation_then_revert(self, lifecycle_workspace):
        pm = PatchManager(
            lifecycle_workspace,
            allowed_paths=["calculator.py"],
            denied_paths=["tests", "task.json"],
        )

        bad_diff = """\
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return broken syntax(
"""
        result = pm.apply_patch(bad_diff)
        assert result.success is True
        syntax_result = pm.syntax_check()
        assert syntax_result.all_passed is False

    def test_search_and_patch(self, lifecycle_workspace):
        ws = lifecycle_workspace
        matches, truncated = search_code(ws, "return a + b")
        assert len(matches) >= 1
        assert matches[0].path == "calculator.py"

        func = find_function(ws, "add")
        assert func is not None
        assert func.qualified_name == "add"

        src = get_function_source(ws, "add")
        assert src is not None
        assert "return a + b" in src.source_lines[1].text

        pm = PatchManager(
            ws,
            allowed_paths=["calculator.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = """\
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,3 @@
 def add(a, b):
-    return a + b
+    result = a + b
+    return result
"""
        result = pm.apply_patch(diff)
        assert result.success is True

        syntax = pm.syntax_check()
        assert syntax.all_passed is True

        pm.revert_patch()

    def test_no_git_command_used(self, lifecycle_workspace):
        pm = PatchManager(
            lifecycle_workspace,
            allowed_paths=["calculator.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = """\
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a + b + 0
"""
        pm.apply_patch(diff)
        assert pm.has_active_patch is True
        pm.revert_patch()
        assert pm.has_active_patch is False

    def test_atomic_apply_with_rollback(self, lifecycle_workspace):
        pm = PatchManager(
            lifecycle_workspace,
            allowed_paths=["calculator.py"],
            denied_paths=["tests", "task.json"],
        )

        diff = """\
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,3 @@
 def add(a, b):
-    return a + b
+    result = a + b
+    return result
"""
        result = pm.apply_patch(diff)
        assert result.success is True
        resolved = lifecycle_workspace.resolve_path("calculator.py")
        content = Path(resolved).read_text()
        assert "result = a + b" in content
        assert "return result" in content

    def test_context_mismatch_rejected(self, lifecycle_workspace):
        pm = PatchManager(
            lifecycle_workspace,
            allowed_paths=["calculator.py"],
            denied_paths=["tests", "task.json"],
        )

        bad_context_diff = """\
--- a/calculator.py
+++ b/calculator.py
@@ -10,2 +10,2 @@
 def nonexistent():
-    return 1
+    return 2
"""
        with pytest.raises(PatchApplyError, match="Context mismatch|beyond file"):
            pm.apply_patch(bad_context_diff)

    def test_line_endings_preserved_after_patch(self, lifecycle_workspace):
        ws = lifecycle_workspace
        resolved = ws.resolve_path("calculator.py")
        original_bytes = Path(resolved).read_bytes()
        original_eol = b"\r\n" if b"\r\n" in original_bytes else b"\n"

        pm = PatchManager(
            ws,
            allowed_paths=["calculator.py"],
            denied_paths=["tests", "task.json"],
        )

        diff = """\
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,3 @@
 def add(a, b):
-    return a + b
+    result = a + b
+    return result
"""
        pm.apply_patch(diff)
        content = Path(resolved).read_bytes()
        assert content.endswith(b"\n")
        if original_eol == b"\n":
            assert b"\r\n" not in content
        else:
            assert b"\r\n" in content

    def test_coverage_omitted_count(self, lifecycle_workspace):
        pm = PatchManager(
            lifecycle_workspace,
            allowed_paths=["calculator.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = "--- a/calculator.py\n+++ b/calculator.py\n@@ -1 +1 @@\n-def add(a, b):\n+def add(a, b, c=0):\n"
        result = pm.apply_patch(diff)
        assert result.success is True
        resolved = lifecycle_workspace.resolve_path("calculator.py")
        content = Path(resolved).read_text()
        assert "def add(a, b, c=0):" in content
