from __future__ import annotations

import hashlib
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
    PatchValidationError,
    PatchRevertError,
)
from agentic_debugger.runtime.patcher import (
    PatchManager,
    _check_python_syntax,
    _parse_unified_diff,
    _verify_file_hash,
    _MANDATORY_DENIED_RULES,
    _PolicyRule,
    _PolicyKind,
)
from agentic_debugger.runtime.workspace import TaskWorkspace


@pytest.fixture
def patcher_workspace():
    tmp = Path(tempfile.mkdtemp())
    try:
        src = tmp / "source"
        src.mkdir()
        (src / "profile.py").write_text(
            "def format_name(first, last):\n"
            "    return f\"{first} {last}\"\n"
        )
        (src / "utils.py").write_text(
            "def add(a, b):\n"
            "    return a + b\n"
        )
        (src / "protected.py").write_text(
            "SECRET = 'protected'\n"
        )
        (src / "tests").mkdir()
        (src / "tests" / "test_profile.py").write_text(
            "def test_format():\n    pass\n"
        )
        (src / "task.json").write_text('{"version": 1}\n')
        ws = TaskWorkspace(str(src))
        yield ws
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


VALID_DIFF = """\
--- a/profile.py
+++ b/profile.py
@@ -1,2 +1,3 @@
 def format_name(first, last):
-    return f"{first} {last}"
+    result = f"{first} {last}"
+    return result
"""

MULTI_FILE_DIFF = """\
--- a/utils.py
+++ b/utils.py
@@ -1,2 +1,3 @@
 def add(a, b):
-    return a + b
+    result = a + b
+    return result
--- a/profile.py
+++ b/profile.py
@@ -1,2 +1,3 @@
 def format_name(first, last):
-    return f"{first} {last}"
+    result = f"{first} {last}"
+    return result
"""

OMITTED_COUNT_DIFF = """\
--- a/profile.py
+++ b/profile.py
@@ -1 +1 @@
-def format_name(first, last):
+def format_name(first, last, title=None):
"""


def make_pm(ws, allowed=None, denied=None):
    if allowed is None:
        allowed = ["profile.py", "utils.py"]
    if denied is None:
        denied = ["tests", "task.json"]
    return PatchManager(ws, allowed_paths=allowed, denied_paths=denied)


class TestPatchParser:
    def test_valid_one_file(self):
        patches = _parse_unified_diff(VALID_DIFF)
        assert len(patches) == 1
        assert patches[0].path == "profile.py"
        assert len(patches[0].hunks) == 1


class TestOfficialPatchCompatibility:
    def test_contextful_patch_passes_official_direct_apply_check(self, patcher_workspace):
        if shutil.which("git") is None:
            pytest.skip("git is required for official patch compatibility coverage")
        pm = PatchManager(
            patcher_workspace,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
            official_patch_compatibility=True,
        )
        result = pm.apply_patch(VALID_DIFF)
        assert result.success is True

    def test_local_only_fuzzy_patch_is_rejected_before_mutation(self, patcher_workspace):
        if shutil.which("git") is None:
            pytest.skip("git is required for official patch compatibility coverage")
        local_only_diff = (
            "--- a/profile.py\n"
            "+++ b/profile.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-def format_name(first, last):\n"
            "+def format_name(first, last, title=None):\n"
        )
        permissive = make_pm(patcher_workspace)
        assert permissive.apply_patch(local_only_diff).success is True
        permissive.revert_patch()

        strict = PatchManager(
            patcher_workspace,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
            official_patch_compatibility=True,
        )
        with pytest.raises(PatchValidationError, match="official git apply"):
            strict.apply_patch(local_only_diff)
        assert strict.has_active_patch is False

    def test_valid_multi_file(self):
        patches = _parse_unified_diff(MULTI_FILE_DIFF)
        assert len(patches) == 2
        assert patches[0].path == "utils.py"
        assert patches[1].path == "profile.py"

    def test_omitted_hunk_counts(self):
        patches = _parse_unified_diff(OMITTED_COUNT_DIFF)
        assert len(patches) == 1
        assert len(patches[0].hunks) == 1
        hunk = patches[0].hunks[0]
        assert hunk.old_count == 1
        assert hunk.new_count == 1

    def test_empty_patch_rejected(self):
        with pytest.raises(PatchValidationError, match="Empty"):
            _parse_unified_diff("")

    def test_malformed_hunk_header(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -abc +def @@\n x\n"
        with pytest.raises(PatchValidationError):
            _parse_unified_diff(diff)

    def test_hunk_body_without_header(self):
        diff = "--- a/x.py\n+++ b/x.py\n x\n"
        with pytest.raises(PatchValidationError):
            _parse_unified_diff(diff)

    def test_absolute_path_rejected(self):
        diff = "--- a//etc/passwd\n+++ b//etc/passwd\n@@ -1 +1 @@\n x\n"
        with pytest.raises(PatchValidationError, match="Absolute path"):
            _parse_unified_diff(diff)

    def test_windows_absolute_path_rejected(self):
        diff = "--- a/C:\\\\foo.py\n+++ b/C:\\\\foo.py\n@@ -1 +1 @@\n x\n"
        with pytest.raises(PatchValidationError, match="Windows absolute"):
            _parse_unified_diff(diff)

    def test_traversal_path_rejected(self):
        diff = "--- a/../outside.py\n+++ b/../outside.py\n@@ -1 +1 @@\n x\n"
        with pytest.raises(PatchValidationError, match="traversal"):
            _parse_unified_diff(diff)

    def test_dev_null_rejected(self):
        diff = "--- a//dev/null\n+++ b/dev/null.py\n@@ -1 +1 @@\n x\n"
        with pytest.raises(PatchValidationError, match="/dev/null"):
            _parse_unified_diff(diff)

    def test_diff_paths_differ(self):
        diff = "--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n x\n"
        with pytest.raises(PatchValidationError, match="Old and new paths differ"):
            _parse_unified_diff(diff)

    def test_patch_too_large(self):
        large = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n"
        large += " " + "x" * 100_000 + "\n"
        with pytest.raises(PatchValidationError, match="maximum length"):
            _parse_unified_diff(large)

    def test_nul_in_path_rejected(self):
        diff = "--- a/x\x00y.py\n+++ b/x\x00y.py\n@@ -1 +1 @@\n x\n"
        with pytest.raises(PatchValidationError, match="NUL"):
            _parse_unified_diff(diff)

    def test_malformed_no_newline_marker(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n x\n\\ Bad marker\n"
        with pytest.raises(PatchValidationError, match="no-newline marker"):
            _parse_unified_diff(diff)

    def test_empty_hunk_rejected(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,0 +1,0 @@\n"
        with pytest.raises(PatchValidationError):
            _parse_unified_diff(diff)

    def test_duplicate_file_section(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n x\n--- a/x.py\n+++ b/x.py\n@@ -2 +2 @@\n y\n"
        with pytest.raises(PatchValidationError, match="Duplicate"):
            _parse_unified_diff(diff)

    def test_hunk_count_mismatch_old(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,5 +1,1 @@\n x\n"
        with pytest.raises(PatchValidationError, match="old_count"):
            _parse_unified_diff(diff)

    def test_hunk_count_mismatch_new(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,5 @@\n x\n"
        with pytest.raises(PatchValidationError, match="new_count"):
            _parse_unified_diff(diff)

    def test_overlapping_hunks(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n b\n c\n@@ -2,3 +2,3 @@\n b\n c\n d\n"
        with pytest.raises(PatchValidationError, match="overlap"):
            _parse_unified_diff(diff)

    def test_out_of_order_hunks(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -5,3 +5,3 @@\n e\n f\n g\n@@ -1,3 +1,3 @@\n a\n b\n c\n"
        with pytest.raises(PatchValidationError, match="overlap|out of order"):
            _parse_unified_diff(diff)

    def test_file_header_no_hunks(self):
        diff = "--- a/x.py\n+++ b/x.py\n"
        with pytest.raises(PatchValidationError, match="no hunks"):
            _parse_unified_diff(diff)

    def test_git_metadata_rejected(self):
        diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n x\n"
        with pytest.raises(PatchValidationError, match="metadata"):
            _parse_unified_diff(diff)

    def test_mode_only_rejected(self):
        diff = "old mode 100644\nnew mode 100755\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n x\n"
        with pytest.raises(PatchValidationError, match="metadata"):
            _parse_unified_diff(diff)

    def test_hunk_inside_hunk_prevents_false_match(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,5 +1,5 @@\n--- a\n+++ b\n@@ -10 +10 @@\n x\n"
        with pytest.raises(PatchValidationError, match="counts satisfied"):
            _parse_unified_diff(diff)

    def test_duplicate_plus_plus_header(self):
        diff = "--- a/x.py\n+++ b/x.py\n+++ b/x.py\n@@ -1 +1 @@\n x\n"
        with pytest.raises(PatchValidationError):
            _parse_unified_diff(diff)

    def test_removal_line_dash_dash_value(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n--- value\n+++ new_value\n"
        patches = _parse_unified_diff(diff)
        assert len(patches) == 1
        hunk = patches[0].hunks[0]
        assert hunk.lines[0].prefix == "-"
        assert hunk.lines[0].text == "-- value"
        assert hunk.lines[1].prefix == "+"
        assert hunk.lines[1].text == "++ new_value"

    def test_completed_hunk_followed_by_atat(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n- a\n+ b\n@@ -2,1 +2,2 @@\n c\n+d\n"
        patches = _parse_unified_diff(diff)
        assert len(patches) == 1
        assert len(patches[0].hunks) == 2

    def test_completed_hunk_followed_by_new_file(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n- a\n+ b\n--- a/y.py\n+++ b/y.py\n@@ -1,1 +1,2 @@\n x\n+y\n"
        patches = _parse_unified_diff(diff)
        assert len(patches) == 2

    def test_incomplete_hunk_file_header_rejected(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n--- a/y.py\n+++ b/y.py\n@@ -1,1 +1,1 @@\n x\n+y\n"
        with pytest.raises(PatchValidationError, match="counts satisfied"):
            _parse_unified_diff(diff)

    def test_extra_body_line_after_counts_rejected(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n a\n b\n"
        with pytest.raises(PatchValidationError, match="Extra line"):
            _parse_unified_diff(diff)

    def test_duplicate_old_header_before_new(self):
        diff = "--- a/x.py\n--- a/x.py\n+++ a/x.py\n@@ -1 +1 @@\n x\n"
        with pytest.raises(PatchValidationError, match="Duplicate ---"):
            _parse_unified_diff(diff)

    def test_empty_hunk_followed_by_valid(self):
        diff = "--- a/x.py\n+++ a/x.py\n@@ -0,0 +1,1 @@\n+new_line\n@@ -1,1 +2,2 @@\n x\n+y\n"
        patches = _parse_unified_diff(diff)
        assert len(patches) == 1
        assert len(patches[0].hunks) == 2

    def test_pure_insertion(self):
        diff = "--- a/x.py\n+++ a/x.py\n@@ -0,0 +1,1 @@\n+inserted\n"
        patches = _parse_unified_diff(diff)
        assert len(patches) == 1
        assert patches[0].hunks[0].old_count == 0
        assert patches[0].hunks[0].new_count == 1

    def test_pure_deletion(self):
        diff = "--- a/x.py\n+++ a/x.py\n@@ -1,1 +0,0 @@\n-removed\n"
        patches = _parse_unified_diff(diff)
        assert len(patches) == 1
        assert patches[0].hunks[0].old_count == 1
        assert patches[0].hunks[0].new_count == 0

    def test_both_zero_rejected(self):
        diff = "--- a/x.py\n+++ a/x.py\n@@ -0,0 +0,0 @@\n"
        with pytest.raises(PatchValidationError):
            _parse_unified_diff(diff)

    def test_trailing_lone_old_header_rejected(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n--- a/y.py\n"
        with pytest.raises(PatchValidationError, match="Missing \\+\\+\\+"):
            _parse_unified_diff(diff)

    def test_patch_only_dash_dash_rejected(self):
        diff = "--- a/x.py\n"
        with pytest.raises(PatchValidationError, match="Missing \\+\\+\\+"):
            _parse_unified_diff(diff)

    def test_file_header_no_hunk_rejected(self):
        diff = "--- a/x.py\n+++ b/x.py\n"
        with pytest.raises(PatchValidationError):
            _parse_unified_diff(diff)

    def test_incomplete_final_hunk_rejected(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n"
        with pytest.raises(PatchValidationError):
            _parse_unified_diff(diff)

    def test_completed_final_hunk_no_trailing_newline(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-a\n+b"
        patches = _parse_unified_diff(diff)
        assert len(patches) == 1
        assert len(patches[0].hunks) == 1


class TestPatchAuthorization:
    def test_allowed_exact_file(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        result = pm.apply_patch(VALID_DIFF)
        assert result.success is True

    def test_allowed_directory_descendant(self, patcher_workspace):
        pm = make_pm(patcher_workspace, allowed=["profile.py", "utils.py"])
        result = pm.apply_patch(VALID_DIFF)
        assert result.success is True

    def test_denied_exact_file(self, patcher_workspace):
        pm = make_pm(
            patcher_workspace,
            allowed=["profile.py"],
            denied=["profile.py", "tests", "task.json"],
        )
        with pytest.raises(PatchAuthorizationError, match="denied"):
            pm.apply_patch(VALID_DIFF)

    def test_denied_directory_descendant(self, patcher_workspace):
        diff = "--- a/tests/test_profile.py\n+++ b/tests/test_profile.py\n@@ -1,1 +1,1 @@\n-def test_format():\n+def test_format_new():\n"
        pm = make_pm(patcher_workspace)
        with pytest.raises(PatchAuthorizationError, match="denied"):
            pm.apply_patch(diff)

    def test_deny_precedence(self, patcher_workspace):
        pm = PatchManager(
            patcher_workspace,
            allowed_paths=["profile.py", "protected.py", "tests"],
            denied_paths=["protected.py", "tests"],
        )
        diff_protected = "--- a/protected.py\n+++ b/protected.py\n@@ -1 +1 @@\n-SECRET = 'protected'\n+SECRET = 'unlocked'\n"
        with pytest.raises(PatchAuthorizationError, match="denied"):
            pm.apply_patch(diff_protected)

    def test_substring_lookalike_not_allowed(self, patcher_workspace):
        pm = PatchManager(
            patcher_workspace,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = "--- a/not_profile.py\n+++ b/not_profile.py\n@@ -1 +1 @@\n x\n"
        with pytest.raises(PatchAuthorizationError, match="not allowed"):
            pm.apply_patch(diff)

    def test_task_manifest_protected(self, patcher_workspace):
        diff = "--- a/task.json\n+++ b/task.json\n@@ -1 +1 @@\n-{'v':1}\n+{'v':2}\n"
        pm = make_pm(patcher_workspace)
        with pytest.raises(PatchAuthorizationError, match="denied"):
            pm.apply_patch(diff)

    def test_mandatory_deny_even_with_empty_denylist(self, patcher_workspace):
        pm = PatchManager(
            patcher_workspace,
            allowed_paths=["tests", "task.json", "profile.py"],
            denied_paths=[],
        )
        diff_tests = "--- a/tests/test_profile.py\n+++ b/tests/test_profile.py\n@@ -1 +1 @@\n-def test_format():\n+def test_format_new():\n"
        with pytest.raises(PatchAuthorizationError, match="denied"):
            pm.apply_patch(diff_tests)
        diff_task = "--- a/task.json\n+++ b/task.json\n@@ -1 +1 @@\n-{'v':1}\n+{'v':2}\n"
        with pytest.raises(PatchAuthorizationError, match="denied"):
            pm.apply_patch(diff_task)

    def test_mandatory_deny_overrides_allow(self, patcher_workspace):
        pm = PatchManager(
            patcher_workspace,
            allowed_paths=["tests/test_profile.py", "task.json", "profile.py"],
            denied_paths=[],
        )
        diff = "--- a/tests/test_profile.py\n+++ b/tests/test_profile.py\n@@ -1 +1 @@\n-def test_format():\n+def test_format_new():\n"
        with pytest.raises(PatchAuthorizationError, match="denied"):
            pm.apply_patch(diff)

    def test_allowed_exact_file_not_descendants(self, patcher_workspace):
        pm = PatchManager(
            patcher_workspace,
            allowed_paths=["tests"],
            denied_paths=[],
        )
        diff = "--- a/tests/test_profile.py\n+++ b/tests/test_profile.py\n@@ -1 +1 @@\n-def test_format():\n+def test_format_new():\n"
        with pytest.raises(PatchAuthorizationError):
            pm.apply_patch(diff)

    def test_exact_file_allow_no_descendant(self, patcher_workspace):
        pm = PatchManager(
            patcher_workspace,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = "--- a/profile.py/child\n+++ b/profile.py/child\n@@ -1 +1 @@\n x\n"
        with pytest.raises(PatchAuthorizationError, match="not allowed"):
            pm.apply_patch(diff)

    def test_exact_file_deny_no_sibling_prefix(self, patcher_workspace):
        ws = patcher_workspace
        (Path(ws.root) / "profile_extra.py").write_text("x = 1\n")
        pm = PatchManager(
            ws,
            allowed_paths=["profile_extra.py", "profile.py"],
            denied_paths=["profile.py"],
        )
        diff = "--- a/profile_extra.py\n+++ b/profile_extra.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        result = pm.apply_patch(diff)
        assert result.success is True

    def test_unsafe_policy_entry_rejected(self, patcher_workspace):
        with pytest.raises(PatchAuthorizationError):
            PatchManager(
                patcher_workspace,
                allowed_paths=["../outside"],
                denied_paths=[],
            )

    def test_missing_allowed_entry_rejected(self, patcher_workspace):
        with pytest.raises(PatchAuthorizationError):
            PatchManager(
                patcher_workspace,
                allowed_paths=["nonexistent.py"],
                denied_paths=[],
            )

    def test_missing_denied_entry_rejected(self, patcher_workspace):
        with pytest.raises(PatchAuthorizationError):
            PatchManager(
                patcher_workspace,
                allowed_paths=["profile.py"],
                denied_paths=["nonexistent.py"],
            )

    def test_mandatory_rules_explicit_kinds(self):
        assert _MANDATORY_DENIED_RULES is not None
        rules = {r.path: r.kind for r in _MANDATORY_DENIED_RULES}
        assert rules["tests"] == _PolicyKind.DIRECTORY
        assert rules["task.json"] == _PolicyKind.EXACT_FILE


class TestPatchApply:
    def test_successful_application(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        result = pm.apply_patch(VALID_DIFF)
        assert result.success is True
        assert len(result.changed_files) == 1
        assert result.hunk_count == 1
        resolved = patcher_workspace.resolve_path("profile.py")
        content = Path(resolved).read_text()
        assert "result" in content

    def test_multiple_files(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        result = pm.apply_patch(MULTI_FILE_DIFF)
        assert result.success is True
        assert len(result.changed_files) == 2
        assert result.hunk_count == 2

    def test_result_hashes_match(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        result = pm.apply_patch(VALID_DIFF)
        resolved = patcher_workspace.resolve_path("profile.py")
        actual_hash = hashlib.sha256(
            Path(resolved).read_bytes()
        ).hexdigest()
        assert result.after_sha256["profile.py"] == actual_hash

    def test_context_mismatch(self, patcher_workspace):
        diff = """\
--- a/profile.py
+++ b/profile.py
@@ -1,2 +1,2 @@
 def format_name(first, last):
-    return f"{first} {last}"
+    return None
"""
        pm = make_pm(patcher_workspace)
        pm.apply_patch(diff)
        resolved = patcher_workspace.resolve_path("profile.py")
        content = Path(resolved).read_text()
        assert "return None" in content

    def test_context_mismatch_wrong(self, patcher_workspace):
        diff = """\
--- a/profile.py
+++ b/profile.py
@@ -1,2 +1,2 @@
 def nonexistent_function():
-    return 1
+    return 2
"""
        pm = make_pm(patcher_workspace)
        with pytest.raises(PatchApplyError, match="Context mismatch"):
            pm.apply_patch(diff)

    def test_bounded_context_fuzz_applies_misaligned_hunk(self, patcher_workspace):
        """A hunk whose declared start position is imprecise (the context
        body belongs to an earlier line) is applied via bounded deterministic
        fuzz; content matching stays exact and the displacement is recorded."""
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        source = (
            "def format_name(first, last):\n"
            "    first = first.strip()\n"
            "    last = last.strip()\n"
            '    return f"{first} {last}"\n'
        )
        Path(resolved).write_bytes(source.encode("utf-8"))
        # Declared start -4 (the 'return' line) while the body begins at the
        # 'first = ...' line (position 2): 2-line displacement.
        diff = """\
--- a/profile.py
+++ b/profile.py
@@ -4,3 +4,3 @@
     first = first.strip()
     last = last.strip()
-    return f"{first} {last}"
+    return f"{first} {last}".title()
"""
        pm = make_pm(patcher_workspace)
        result = pm.apply_patch(diff)
        assert result.success is True
        assert result.hunk_adjustments == (("profile.py", 1, -2),)
        content = Path(resolved).read_text()
        assert 'return f"{first} {last}".title()' in content
        assert "first = first.strip()" in content

    def test_context_fuzz_never_matches_wrong_content(self, patcher_workspace):
        """Bounded fuzz is location-only: content that does not exist in the
        file anywhere still fails closed (no fabricated application)."""
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        Path(resolved).write_bytes(b"a\nb\nc\nd\n")
        diff = """\
--- a/profile.py
+++ b/profile.py
@@ -2,2 +2,2 @@
 def nonexistent_function():
-    return 1
+    return 2
"""
        pm = make_pm(patcher_workspace)
        with pytest.raises(PatchApplyError, match="Context mismatch"):
            pm.apply_patch(diff)

    def test_active_snapshot_exists(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        pm.apply_patch(VALID_DIFF)
        assert pm.has_active_patch is True

    def test_second_patch_rejected(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        pm.apply_patch(VALID_DIFF)
        with pytest.raises(PatchStateError, match="Active patch exists"):
            pm.apply_patch(VALID_DIFF)

    def test_immutable_source_fixture(self, patcher_workspace):
        ws = patcher_workspace
        pm = make_pm(ws)
        pm.apply_patch(VALID_DIFF)
        resolved = ws.resolve_path("profile.py")
        patched = Path(resolved).read_text()
        assert "result" in patched

    def test_first_hunk_adds_second_modifies(self, patcher_workspace):
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        Path(resolved).write_bytes(b"a\nb\nc\nd\n")
        pm = PatchManager(
            ws,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = """\
--- a/profile.py
+++ b/profile.py
@@ -1,2 +1,3 @@
 a
 b
+x
@@ -4,1 +4,2 @@
 d
+y
"""
        pm.apply_patch(diff)
        content = Path(resolved).read_text()
        assert "a\nb\nx\nc\nd\ny\n" in content

    def test_first_hunk_deletes_second_modifies(self, patcher_workspace):
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        Path(resolved).write_bytes(b"a\nb\nc\nd\n")
        pm = PatchManager(
            ws,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = """\
--- a/profile.py
+++ b/profile.py
@@ -1,2 +1,1 @@
-a
 b
@@ -4,1 +3,2 @@
 d
+e
"""
        pm.apply_patch(diff)
        content = Path(resolved).read_bytes()
        assert content == b"b\nc\nd\ne\n"

    def test_zero_net_change_hunks(self, patcher_workspace):
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        Path(resolved).write_bytes(b"x\ny\nz\n")
        pm = PatchManager(
            ws,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = """\
--- a/profile.py
+++ b/profile.py
@@ -1,1 +1,1 @@
-x
+x1
@@ -3,1 +3,1 @@
-z
+z1
"""
        pm.apply_patch(diff)
        content = Path(resolved).read_bytes()
        assert content == b"x1\ny\nz1\n"

    def test_pure_insertion_at_beginning(self, patcher_workspace):
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        Path(resolved).write_bytes(b"original\n")
        pm = make_pm(patcher_workspace)
        diff = "--- a/profile.py\n+++ b/profile.py\n@@ -0,0 +1,1 @@\n+first\n"
        pm.apply_patch(diff)
        content = Path(resolved).read_bytes()
        assert content == b"first\noriginal\n"

    def test_pure_insertion_after_middle(self, patcher_workspace):
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        Path(resolved).write_bytes(b"a\nb\nc\n")
        pm = make_pm(patcher_workspace)
        diff = "--- a/profile.py\n+++ b/profile.py\n@@ -2,0 +3,1 @@\n+mid\n"
        pm.apply_patch(diff)
        content = Path(resolved).read_bytes()
        assert content == b"a\nb\nmid\nc\n"

    def test_pure_insertion_at_eof(self, patcher_workspace):
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        Path(resolved).write_bytes(b"a\nb\n")
        pm = make_pm(patcher_workspace)
        diff = "--- a/profile.py\n+++ b/profile.py\n@@ -2,0 +3,1 @@\n+eof\n"
        pm.apply_patch(diff)
        content = Path(resolved).read_bytes()
        assert content == b"a\nb\neof\n"

    def test_overlap_rejected_at_parser(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,5 +1,5 @@\n a\n b\n c\n d\n e\n@@ -3,3 +3,2 @@\n c\n d\n e\n"
        with pytest.raises(PatchValidationError):
            _parse_unified_diff(diff)


class TestPatchRevert:
    def test_revert_restores_original(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        resolved = patcher_workspace.resolve_path("profile.py")
        original = Path(resolved).read_bytes()
        pm.apply_patch(VALID_DIFF)
        pm.revert_patch()
        restored = Path(resolved).read_bytes()
        assert restored == original

    def test_revert_restores_hashes(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        resolved = patcher_workspace.resolve_path("profile.py")
        original_hash = hashlib.sha256(
            Path(resolved).read_bytes()
        ).hexdigest()
        pm.apply_patch(VALID_DIFF)
        pm.revert_patch()
        restored_hash = hashlib.sha256(
            Path(resolved).read_bytes()
        ).hexdigest()
        assert original_hash == restored_hash

    def test_revert_no_active_patch(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        with pytest.raises(PatchStateError, match="No active patch"):
            pm.revert_patch()

    def test_repeat_revert(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        pm.apply_patch(VALID_DIFF)
        pm.revert_patch()
        assert pm.has_active_patch is False
        with pytest.raises(PatchStateError, match="No active patch"):
            pm.revert_patch()

    def test_multi_file_revert(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        originals = {}
        for path in ["profile.py", "utils.py"]:
            resolved = patcher_workspace.resolve_path(path)
            originals[path] = (
                Path(resolved).read_bytes(),
                hashlib.sha256(Path(resolved).read_bytes()).hexdigest(),
            )
        pm.apply_patch(MULTI_FILE_DIFF)
        pm.revert_patch()
        for path, (orig_bytes, orig_hash) in originals.items():
            resolved = patcher_workspace.resolve_path(path)
            restored = Path(resolved).read_bytes()
            assert restored == orig_bytes
            assert hashlib.sha256(restored).hexdigest() == orig_hash


class TestPatchLineEndings:
    def test_lf_preserved(self, patcher_workspace):
        resolved = patcher_workspace.resolve_path("profile.py")
        Path(resolved).write_bytes(b"line1\nline2\nline3\n")
        pm = make_pm(patcher_workspace)
        diff = """\
--- a/profile.py
+++ b/profile.py
@@ -1,3 +1,4 @@
 line1
 line2
-line3
+line3 modified
+line4
"""
        pm.apply_patch(diff)
        content = Path(resolved).read_bytes()
        assert content == b"line1\nline2\nline3 modified\nline4\n"

    def test_crlf_preserved(self, patcher_workspace):
        resolved = patcher_workspace.resolve_path("profile.py")
        Path(resolved).write_bytes(b"line1\r\nline2\r\nline3\r\n")
        pm = make_pm(patcher_workspace)
        diff = """\
--- a/profile.py
+++ b/profile.py
@@ -1,3 +1,4 @@
 line1
 line2
-line3
+line3 modified
+line4
"""
        pm.apply_patch(diff)
        content = Path(resolved).read_bytes()
        assert content == b"line1\r\nline2\r\nline3 modified\r\nline4\r\n"

    def test_no_final_newline_preserved(self, patcher_workspace):
        resolved = patcher_workspace.resolve_path("profile.py")
        Path(resolved).write_bytes(b"line1\nline2")
        pm = make_pm(patcher_workspace)
        diff = "--- a/profile.py\n+++ b/profile.py\n@@ -1,2 +1,2 @@\n line1\n-line2\n\\ No newline at end of file\n+line2 modified\n\\ No newline at end of file\n"
        pm.apply_patch(diff)
        content = Path(resolved).read_bytes()
        assert content == b"line1\nline2 modified"


class TestPatchEncoding:
    def test_latin1_cookie_preserved(self, patcher_workspace):
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        with open(resolved, "wb") as f:
            f.write("# -*- coding: latin-1 -*-\n".encode("ascii"))
            f.write("x = 'caf\xe9'\n".encode("latin-1"))
        pm = PatchManager(
            ws,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = "--- a/profile.py\n+++ b/profile.py\n@@ -1,2 +1,2 @@\n # -*- coding: latin-1 -*-\n-x = 'caf\xe9'\n+x = 'caf\xe9 modifi\xe9'\n"
        result = pm.apply_patch(diff)
        assert result.success is True
        content = Path(resolved).read_bytes()
        assert b"# -*- coding: latin-1 -*-" in content
        assert b"caf\xe9 modifi\xe9" in content

    def test_utf8_bom_preserved(self, patcher_workspace):
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        with open(resolved, "wb") as f:
            f.write(b"\xef\xbb\xbfx = 1\n")
        pm = PatchManager(
            ws,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = "--- a/profile.py\n+++ b/profile.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        result = pm.apply_patch(diff)
        assert result.success is True
        content = Path(resolved).read_bytes()
        assert content.startswith(b"\xef\xbb\xbf")
        assert b"x = 2" in content

    def test_utf8_plain(self, patcher_workspace):
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        Path(resolved).write_text("x = 'hello'\n", encoding="utf-8")
        pm = PatchManager(
            ws,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = "--- a/profile.py\n+++ b/profile.py\n@@ -1 +1 @@\n-x = 'hello'\n+x = 'world'\n"
        result = pm.apply_patch(diff)
        assert result.success is True
        content = Path(resolved).read_bytes()
        assert b"x = 'world'" in content

    def test_revert_after_encoding(self, patcher_workspace):
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        with open(resolved, "wb") as f:
            f.write("# -*- coding: latin-1 -*-\n".encode("ascii"))
            f.write("x = 'caf\xe9'\n".encode("latin-1"))
        original = Path(resolved).read_bytes()
        pm = PatchManager(
            ws,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
        )
        diff = "--- a/profile.py\n+++ b/profile.py\n@@ -1,2 +1,2 @@\n # -*- coding: latin-1 -*-\n-x = 'caf\xe9'\n+x = 'caf\xe9 mod\xe9'\n"
        pm.apply_patch(diff)
        pm.revert_patch()
        restored = Path(resolved).read_bytes()
        assert restored == original


class TestAtomicWriteAndRollback:
    def test_post_write_hash_verified(self, patcher_workspace):
        ws = patcher_workspace
        pm = make_pm(ws)
        result = pm.apply_patch(VALID_DIFF)
        resolved = ws.resolve_path("profile.py")
        actual = hashlib.sha256(Path(resolved).read_bytes()).hexdigest()
        assert result.after_sha256["profile.py"] == actual

    def test_post_replace_verification_failure_restores(self, patcher_workspace):
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        original_bytes = Path(resolved).read_bytes()
        original_hash = hashlib.sha256(original_bytes).hexdigest()

        original_verify = _verify_file_hash

        def _failing_verify(target, expected_content):
            if "profile" in target:
                raise PatchApplyError("Simulated verify failure")
            original_verify(target, expected_content)

        import agentic_debugger.runtime.patcher as patcher_mod
        patcher_mod._verify_file_hash = _failing_verify
        try:
            pm = PatchManager(
                ws,
                allowed_paths=["profile.py"],
                denied_paths=["tests", "task.json"],
            )
            with pytest.raises(PatchApplyError, match="write failed"):
                pm.apply_patch(VALID_DIFF)
            assert pm.has_active_patch is False
            restored = Path(resolved).read_bytes()
            assert restored == original_bytes
            assert hashlib.sha256(restored).hexdigest() == original_hash
        finally:
            patcher_mod._verify_file_hash = original_verify

    def test_revert_verify_failure_preserves_snapshot(self, patcher_workspace):
        ws = patcher_workspace
        pm = PatchManager(
            ws,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
        )
        pm.apply_patch(VALID_DIFF)
        assert pm.has_active_patch is True

        original_verify = _verify_file_hash

        def _failing_verify(target, expected_content):
            raise PatchApplyError("Simulated verify failure")

        import agentic_debugger.runtime.patcher as patcher_mod
        patcher_mod._verify_file_hash = _failing_verify
        try:
            with pytest.raises(PatchRevertError, match="snapshot preserved"):
                pm.revert_patch()
            assert pm.has_active_patch is True
        finally:
            patcher_mod._verify_file_hash = original_verify

    def _count_tmp_files(self, ws):
        import glob
        return len([p for p in Path(ws.root).rglob(".agentic_debugger_tmp_*")])

    def test_apply_replace_failure_cleans_temp(self, patcher_workspace):
        ws = patcher_workspace
        original_replace = __import__("agentic_debugger.runtime.patcher").runtime.patcher.os.replace

        def _failing_replace(src, dst):
            if "profile" in str(dst):
                raise OSError("Simulated replace failure")
            original_replace(src, dst)

        import agentic_debugger.runtime.patcher as patcher_mod
        patcher_mod.os.replace = _failing_replace
        try:
            resolved = ws.resolve_path("profile.py")
            original = Path(resolved).read_bytes()
            pm = PatchManager(
                ws,
                allowed_paths=["profile.py"],
                denied_paths=["tests", "task.json"],
            )
            with pytest.raises(PatchApplyError):
                pm.apply_patch(VALID_DIFF)
            assert self._count_tmp_files(ws) == 0
            restored = Path(resolved).read_bytes()
            assert restored == original
            assert pm.has_active_patch is False
        finally:
            patcher_mod.os.replace = original_replace

    def test_multi_file_replace_failure_cleans_temp(self, patcher_workspace):
        ws = patcher_workspace
        original_replace = __import__("agentic_debugger.runtime.patcher").runtime.patcher.os.replace

        call_count = [0]

        def _failing_replace(src, dst):
            call_count[0] += 1
            if call_count[0] == 2:
                raise OSError("Simulated replace failure on second file")
            original_replace(src, dst)

        import agentic_debugger.runtime.patcher as patcher_mod
        patcher_mod.os.replace = _failing_replace
        try:
            orig_utils = Path(ws.resolve_path("utils.py")).read_bytes()
            orig_profile = Path(ws.resolve_path("profile.py")).read_bytes()
            pm = PatchManager(
                ws,
                allowed_paths=["utils.py", "profile.py"],
                denied_paths=["tests", "task.json"],
            )
            diff = "--- a/utils.py\n+++ b/utils.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n-    return a + b\n+    result = a + b\n+    return result\n--- a/profile.py\n+++ b/profile.py\n@@ -1,2 +1,3 @@\n def format_name(first, last):\n-    return f\"{first} {last}\"\n+    result = f\"{first} {last}\"\n+    return result\n"
            with pytest.raises(PatchApplyError):
                pm.apply_patch(diff)
            assert self._count_tmp_files(ws) == 0
            assert Path(ws.resolve_path("utils.py")).read_bytes() == orig_utils
            assert Path(ws.resolve_path("profile.py")).read_bytes() == orig_profile
            assert pm.has_active_patch is False
        finally:
            patcher_mod.os.replace = original_replace

    def test_revert_replace_failure_cleans_temp(self, patcher_workspace):
        ws = patcher_workspace
        pm = PatchManager(
            ws,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
        )
        pm.apply_patch(VALID_DIFF)
        assert pm.has_active_patch is True

        original_replace = __import__("agentic_debugger.runtime.patcher").runtime.patcher.os.replace

        def _failing_replace(src, dst):
            if "profile" in str(dst):
                raise OSError("Simulated revert replace failure")
            original_replace(src, dst)

        import agentic_debugger.runtime.patcher as patcher_mod
        patcher_mod.os.replace = _failing_replace
        try:
            with pytest.raises(PatchRevertError, match="snapshot preserved"):
                pm.revert_patch()
            assert self._count_tmp_files(ws) == 0
            assert pm.has_active_patch is True
        finally:
            patcher_mod.os.replace = original_replace

    def test_revert_verify_failure_then_retry(self, patcher_workspace):
        ws = patcher_workspace
        resolved = ws.resolve_path("profile.py")
        pm = PatchManager(
            ws,
            allowed_paths=["profile.py"],
            denied_paths=["tests", "task.json"],
        )
        pm.apply_patch(VALID_DIFF)
        snapshot_before = pm._snapshot

        original_verify = _verify_file_hash
        fail_count = [0]

        def _failing_then_ok(target, expected_content):
            fail_count[0] += 1
            if fail_count[0] == 1:
                raise PatchApplyError("Simulated verify failure")
            original_verify(target, expected_content)

        import agentic_debugger.runtime.patcher as patcher_mod
        patcher_mod._verify_file_hash = _failing_then_ok
        try:
            with pytest.raises(PatchRevertError, match="snapshot preserved"):
                pm.revert_patch()
            assert pm.has_active_patch is True
        finally:
            patcher_mod._verify_file_hash = original_verify

        pm.revert_patch()
        assert pm.has_active_patch is False
        restored = Path(resolved).read_bytes()
        assert hashlib.sha256(restored).hexdigest() == snapshot_before.before_hashes["profile.py"]


class TestSyntaxCheck:
    def test_valid_python(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        pm.apply_patch(VALID_DIFF)
        result = pm.syntax_check()
        assert result.all_passed is True
        assert len(result.results) >= 1
        assert result.results[0].success is True

    def test_invalid_python(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        diff = """\
--- a/profile.py
+++ b/profile.py
@@ -1,2 +1,2 @@
 def format_name(first, last):
-    return f"{first} {last}"
+    return broken syntax(
"""
        pm.apply_patch(diff)
        result = pm.syntax_check()
        assert result.all_passed is False
        assert result.results[0].success is False
        assert result.results[0].error_type == "SyntaxError"
        assert result.results[0].line is not None

    def test_explicit_paths(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        result = pm.syntax_check(paths=["profile.py"])
        assert result.all_passed is True

    def test_no_active_patch_requires_paths(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        with pytest.raises(PatchStateError, match="No active patch"):
            pm.syntax_check()

    def test_no_pyc_artifact(self, patcher_workspace):
        pm = make_pm(patcher_workspace)
        pm.apply_patch(VALID_DIFF)
        pyc_files_before = set(
            Path(patcher_workspace.root).rglob("*.pyc")
        )
        pm.syntax_check()
        pyc_files_after = set(
            Path(patcher_workspace.root).rglob("*.pyc")
        )
        assert pyc_files_before == pyc_files_after

    def test_non_python_extension_skipped(self, patcher_workspace):
        ws = patcher_workspace
        txt_path = os.path.join(ws.root, "readme.txt")
        Path(txt_path).write_bytes(b"not python content")
        pm = make_pm(patcher_workspace)
        result = pm.syntax_check(paths=["readme.txt"])
        assert result.all_passed is True


class TestCheckPythonSyntax:
    def test_valid_syntax(self):
        result = _check_python_syntax("test.py", b"x = 1\n")
        assert result.success is True

    def test_invalid_syntax(self):
        result = _check_python_syntax(
            "test.py", b"def broken(\n"
        )
        assert result.success is False
        assert result.error_type == "SyntaxError"
        assert result.line == 1
        assert result.column is not None

    def test_explicit_error_line_and_column(self):
        result = _check_python_syntax(
            "test.py", b"if True\n    pass\n"
        )
        assert result.success is False
        assert result.line is not None
