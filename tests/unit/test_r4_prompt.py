"""R4 unit tests — prompt anti-leakage on the FINAL rendered prompt.

Amendment 1: spec section rendered ONLY from agent-visible title+description.
Amendment 9: anti-leakage checked on the final rendered live prompt.
Amendment 10: no imports through untracked S1/S1-P directories.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.evaluation.runner import load_task
from experiments.debugger_interaction_v2_r3.transport import (
    BASE_REPOSITORY,
    BASE_REVISION,
    GENERATION_CONFIG,
)
from experiments.model_generated_test_probe_r4 import probe as r4_probe
from experiments.model_generated_test_probe_r4 import test_generation as tg


@pytest.fixture(scope="module")
def task():
    fixture = (
        REPO_ROOT
        / "agentic_debugger" / "datasets" / "curated" / "curated-off-by-one-002"
    )
    return load_task(str(fixture / "task.json"))


@pytest.fixture(scope="module")
def buggy_source():
    fixture = (
        REPO_ROOT
        / "agentic_debugger" / "datasets" / "curated" / "curated-off-by-one-002"
    )
    return (fixture / "recent_window.py").read_text(encoding="utf-8")


class TestSpecSection:
    def test_spec_section_is_exactly_title_and_description(self, task):
        spec = tg.render_task_spec_section(task)
        assert spec == (
            "Title: Return the complete recent window\n"
            "Description: A deterministic sequence-window helper should return "
            "the requested recent values at the exact boundary as well as for "
            "ordinary windows."
        )

    def test_spec_section_has_no_extra_behavior(self, task):
        # Amendment 1: no harness-authored boundary facts (e.g. size<=0 or
        # empty-list behavior is NOT stated by the agent-visible task and must
        # not be added).
        spec = tg.render_task_spec_section(task).lower()
        for fragment in ("size <= 0", "empty", "returns []", "non-positive"):
            assert fragment not in spec

    def test_spec_section_sha_is_frozen(self, task):
        assert tg._sha256(tg.render_task_spec_section(task)) == (
            "18aea9f1f430465dac938b24385b079f6b0016e95fd617ae5be1aefdf7056604"
        )


class TestFinalRenderedPromptAntiLeakage:
    def test_final_prompt_contains_only_legitimate_content(self, task, buggy_source):
        user_prompt = tg.build_generation_user_prompt(task, buggy_source)
        assert "Title: Return the complete recent window" in user_prompt
        assert "CURRENT SOURCE of recent_window.py (this version is BUGGY)" in user_prompt
        assert "def recent_window(values" in user_prompt
        assert "import recent_window from recent_window" in user_prompt

    @pytest.mark.parametrize(
        "fragment",
        [
            "test_full_length_window_includes_every_value",
            "test_smaller_window_returns_recent_values",
            "test_zero_window_is_empty",
            "test_recent_window.py",
            "root_cause_summary",
            "runtime_evidence_hint",
            "target_symbols",
            "inspect_expressions",
            "The calculated loop indexes omit",
            "831b1c2b",
            "8c051faa",
            "002fc5ca",
            "R_fix",
            "normalize_hunk_counts",
            "model_patch_serialization_normalized",
            "--- a/recent_window.py",
            "+++ b/recent_window.py",
            "diff --git",
            "reference_repair",
            "fixed_revision",
        ],
    )
    def test_forbidden_fragments_absent(self, task, buggy_source, fragment):
        user_prompt = tg.build_generation_user_prompt(task, buggy_source)
        combined = tg.SYSTEM_PROMPT_GENERATION + "\n" + user_prompt
        assert fragment.lower() not in combined.lower()

    def test_anti_leakage_gate_passes_on_final_prompt(self, task, buggy_source):
        user_prompt = tg.build_generation_user_prompt(task, buggy_source)
        result = r4_probe._check_anti_leakage(
            tg.SYSTEM_PROMPT_GENERATION, user_prompt
        )
        assert result["passed"] is True
        assert result["forbidden_found"] == []

    def test_anti_leakage_gate_detects_leak(self):
        result = r4_probe._check_anti_leakage(
            "ok", "root_cause_summary leaked here"
        )
        assert result["passed"] is False
        assert any("root_cause_summary" in f["fragment"] for f in result["forbidden_found"])


class TestImportBoundaries:
    def test_no_imports_through_untracked_s1_s1p(self):
        result = r4_probe._check_import_boundaries()
        assert result["passed"] is True, result
        for module, path in result["runtime_modules"].items():
            assert "debugger_interaction_v2_r3" in pathlib.Path(path).parts

    def test_forbidden_import_regex(self):
        # bare S1/S1-P prefixes are forbidden; tracked _r3/_r4 are not.
        assert r4_probe._FORBIDDEN_IMPORT_RE.search(
            "from experiments.debugger_interaction_v2 import adapter"
        )
        assert r4_probe._FORBIDDEN_IMPORT_RE.search(
            "import experiments.model_generated_test_probe.test_generation"
        )
        assert not r4_probe._FORBIDDEN_IMPORT_RE.search(
            "from experiments.debugger_interaction_v2_r3.transport import FakeTransport"
        )
        assert not r4_probe._FORBIDDEN_IMPORT_RE.search(
            "from experiments.model_generated_test_probe_r4 import probe"
        )


class TestModelIdentity:
    def test_frozen_model_identity(self):
        assert BASE_REPOSITORY == "Qwen/Qwen2.5-Coder-7B-Instruct"
        assert BASE_REVISION == "c03e6d358207e414f1eca0bb1891e29f1db0e242"
        assert GENERATION_CONFIG == {
            "do_sample": False,
            "max_new_tokens": 1024,
            "max_input_tokens": 32768,
        }
