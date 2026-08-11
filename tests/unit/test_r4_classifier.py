"""R4 unit tests — structured buggy-FAIL classifier (amendment 6).

Valid buggy FAIL is proven structurally: compile + collection + execution +
counts + infrastructure-marker rejection + assertion attribution. Rejections:
SyntaxError, ImportError, collection errors, timeout, malformed tests, tests
that never exercise the target behavior.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.debugger_interaction_v2_r3.serialization import normalize_hunk_counts
from experiments.model_generated_test_probe_r4.generated_test_runner import (
    run_structured_generated_test,
)

FIXTURE_DIR = (
    REPO_ROOT / "agentic_debugger" / "datasets" / "curated" / "curated-off-by-one-002"
)

FULL_WINDOW_TEST = (
    "from recent_window import recent_window\n"
    "\n"
    "\n"
    "def test_full_window_returns_all_values() -> None:\n"
    "    values = [10, 20, 30, 40]\n"
    "    assert recent_window(values, len(values)) == values\n"
)

R_FIX_B_PATH = REPO_ROOT / "tests" / "fixtures" / "r31_model_patch_raw.patch"


def _r_fix_c() -> str:
    b = R_FIX_B_PATH.read_text(encoding="utf-8")
    c, _record = normalize_hunk_counts(b)
    return c


def _canonical_tree_hash() -> str:
    digest = hashlib.sha256()
    files = sorted(
        p for p in FIXTURE_DIR.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    for p in files:
        rel = p.relative_to(FIXTURE_DIR).as_posix()
        digest.update(rel.encode("utf-8")); digest.update(b"\0")
        digest.update(p.read_bytes()); digest.update(b"\0")
    return digest.hexdigest()


class TestValidBuggyFailure:
    def test_buggy_assertion_failure_is_valid(self, tmp_path):
        result = run_structured_generated_test(
            FULL_WINDOW_TEST, FIXTURE_DIR, tmp_path,
            label="buggy", candidate_patch=None, timeout_seconds=20,
        )
        assert result.status == "FAIL"
        assert result.reason == "failed"
        assert result.executed is True
        assert result.compiled is True
        assert result.collected == 1
        assert result.collect_error is False
        assert result.counts == {"passed": 0, "failed": 1, "errors": 0,
                                 "skipped": 0, "xfailed": 0, "xpassed": 0}
        assert result.infrastructure_markers == []
        assert result.assertion_attributed is True
        assert result.valid_buggy_failure is True
        assert result.patch_applied is False
        assert result.workspace_cleaned is True

    def test_fixed_pass_same_test(self, tmp_path):
        result = run_structured_generated_test(
            FULL_WINDOW_TEST, FIXTURE_DIR, tmp_path,
            label="fixed", candidate_patch=_r_fix_c(), timeout_seconds=20,
        )
        assert result.status == "PASS"
        assert result.executed is True
        assert result.patch_applied is True
        assert result.patch_error is None
        assert result.collected == 1
        assert result.workspace_cleaned is True

    def test_workspace_and_canonical_immutability(self, tmp_path):
        before = _canonical_tree_hash()
        for _ in range(2):
            result = run_structured_generated_test(
                FULL_WINDOW_TEST, FIXTURE_DIR, tmp_path,
                label="buggy", candidate_patch=None, timeout_seconds=20,
            )
            assert result.workspace_cleaned is True
            # The workspace copy may only GAIN the generated test file; the
            # copied buggy sources stay byte-identical (no patch applied).
            # The canonical fixture is what must never change:
            assert _canonical_tree_hash() == before


class TestRejections:
    def test_syntax_error_rejected(self, tmp_path):
        result = run_structured_generated_test(
            "from recent_window import recent_window\n\n\ndef test_x(:\n    pass\n",
            FIXTURE_DIR, tmp_path,
            label="buggy", candidate_patch=None, timeout_seconds=20,
        )
        assert result.compiled is False
        assert result.status == "ERROR"
        assert result.reason == "compile_error"
        assert result.valid_buggy_failure is False

    def test_import_error_rejected(self, tmp_path):
        result = run_structured_generated_test(
            "from no_such_module_xyz import nothing\n\n\ndef test_x():\n    pass\n",
            FIXTURE_DIR, tmp_path,
            label="buggy", candidate_patch=None, timeout_seconds=20,
        )
        assert result.valid_buggy_failure is False
        assert result.collect_error is True or result.infrastructure_markers

    def test_two_test_functions_rejected(self, tmp_path):
        source = (
            "from recent_window import recent_window\n\n\n"
            "def test_a():\n    assert recent_window([1], 1) == [1]\n\n\n"
            "def test_b():\n    assert recent_window([1, 2], 2) == [1, 2]\n"
        )
        result = run_structured_generated_test(
            source, FIXTURE_DIR, tmp_path,
            label="buggy", candidate_patch=None, timeout_seconds=20,
        )
        assert result.collected == 2
        assert result.status == "ERROR"
        assert result.valid_buggy_failure is False

    def test_zero_test_functions_rejected(self, tmp_path):
        result = run_structured_generated_test(
            "x = 1\n", FIXTURE_DIR, tmp_path,
            label="buggy", candidate_patch=None, timeout_seconds=20,
        )
        assert result.collected == 0
        assert result.collect_error is True
        assert result.valid_buggy_failure is False

    def test_test_never_exercising_target_rejected(self, tmp_path):
        # Fails, but not because it exercises recent_window behavior.
        result = run_structured_generated_test(
            "def test_irrelevant():\n    assert 1 == 2\n",
            FIXTURE_DIR, tmp_path,
            label="buggy", candidate_patch=None, timeout_seconds=20,
        )
        assert result.status == "FAIL"
        assert result.assertion_attributed is False
        assert result.valid_buggy_failure is False

    def test_timeout_rejected(self, tmp_path):
        result = run_structured_generated_test(
            "import time\n\n\ndef test_slow():\n    time.sleep(60)\n",
            FIXTURE_DIR, tmp_path,
            label="buggy", candidate_patch=None, timeout_seconds=2,
        )
        assert result.timed_out is True
        assert result.status == "ERROR"
        assert result.reason == "timed_out"
        assert result.valid_buggy_failure is False

    def test_buggy_label_only_gets_valid_buggy_failure(self, tmp_path):
        # The structured gate is label-scoped: a PASS on "fixed" never gets
        # valid_buggy_failure, and a FAIL on "buggy" does.
        pass_result = run_structured_generated_test(
            FULL_WINDOW_TEST, FIXTURE_DIR, tmp_path,
            label="fixed", candidate_patch=_r_fix_c(), timeout_seconds=20,
        )
        assert pass_result.valid_buggy_failure is False
