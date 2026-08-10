"""Unit tests for the S1-P model-generated regression test probe.

These tests prove the offline pipeline and its negative/STOP paths using a
deterministic ``FakeTransport`` (no model, no GPU). They cover:

* the full happy path: generate -> freeze -> buggy FAILs frozen test ->
  one-shot fix -> fixed code PASSes frozen test -> verifier RESOLVED;
* anti-leakage: the generation prompt excludes the oracle, fixture tests,
  and test node names, and includes the public behavior spec;
* deterministic code/diff extraction (fenced and bare);
* the executability gate (exactly one genuine PASS-or-FAIL test);
* retry/STOP: non-executable test -> retry -> no_executable_test; executable
  test that the buggy code passes -> generated_test_did_not_encode_defect
  (no regeneration); transport failure -> transport_failure;
* ``--validate-only`` source/contract provenance;
* canonical fixture immutability.

Production core and S1 debugger code are never modified.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.evaluation.runner import load_task

from experiments.debugger_interaction_v2.transport import FakeTransport, FailingTransport
from experiments.model_generated_test_probe import probe as probe_mod
from experiments.model_generated_test_probe import test_generation as tg


PROBE_SCRIPT = REPO_ROOT / "experiments" / "model_generated_test_probe" / "probe.py"
FIXTURE_DIR = (
    REPO_ROOT / "agentic_debugger" / "datasets" / "curated" / "curated-none-handling-001"
)

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_task():
    return load_task(str(FIXTURE_DIR / "task.json"))


def _buggy_source() -> str:
    return (FIXTURE_DIR / "display_name.py").read_text(encoding="utf-8")


# A single-function generated test that encodes the None case (exposes the
# bug: the buggy code calls .strip() on None).
_GOOD_TEST = (
    "from display_name import format_display_name\n"
    "\n"
    "\n"
    "def test_none_returns_anonymous() -> None:\n"
    "    assert format_display_name(None) == \"Anonymous\"\n"
)

# A generated test that the BUGGY code PASSES (does not encode the defect).
# It only checks a normal name, which the buggy code handles correctly.
_NON_DEFECT_TEST = (
    "from display_name import format_display_name\n"
    "\n"
    "\n"
    "def test_normal_name() -> None:\n"
    "    assert format_display_name(\"ada lovelace\") == \"Ada Lovelace\"\n"
)

# A generated test with a syntax error (not executable).
_SYNTAX_BAD_TEST = (
    "from display_name import format_display_name\n"
    "\n"
    "\n"
    "def test_bad() -> None:\n"
    "    assert format_display_name(None) ==  # missing value\n"
)

# A generated test with two functions (not exactly one executed test).
_TWO_FUNC_TEST = (
    "from display_name import format_display_name\n"
    "\n"
    "\n"
    "def test_a() -> None:\n"
    "    assert format_display_name(None) == \"Anonymous\"\n"
    "\n"
    "\n"
    "def test_b() -> None:\n"
    "    assert format_display_name(\"x\") == \"X\"\n"
)

_GOOD_DIFF = (
    "--- a/display_name.py\n"
    "+++ b/display_name.py\n"
    "@@ -1,5 +1,7 @@\n"
    " def format_display_name(name: str | None) -> str:\n"
    "-    normalized_name = name.strip()\n"
    "+    if name is None:\n"
    "+        return \"Anonymous\"\n"
    "+    normalized_name = name.strip()\n"
    "     if not normalized_name:\n"
    "         return \"Anonymous\"\n"
    "     return normalized_name.title()\n"
)


def _fence(body: str, lang: str = "python") -> str:
    return f"```{lang}\n{body}```\n"


def _run_probe_with_transport(transport, tmp_path: Path) -> dict:
    """Run the full probe with a given transport and return the evidence dict."""
    contract = probe_mod._load_contract()
    probe_mod._validate_contract(contract)
    evidence = probe_mod.run_probe(
        contract, transport,
        model_name="test-transport",
        output_dir=tmp_path,
    )
    return evidence


# ---------------------------------------------------------------------------
# Anti-leakage / prompt assembly
# ---------------------------------------------------------------------------


class TestPromptAntiLeakage:
    def test_generation_prompt_contains_behavior_spec_and_buggy_source(self) -> None:
        task = _load_task()
        prompt = tg.build_generation_user_prompt(task, _buggy_source())
        # The public behavior spec is intentionally supplied.
        assert "PUBLIC BEHAVIOR SPEC" in prompt
        assert "Anonymous" in prompt
        assert "display_name" in prompt
        # The buggy source is included.
        assert "normalized_name = name.strip()" in prompt

    def test_generation_prompt_excludes_oracle(self) -> None:
        task = _load_task()
        prompt = tg.build_generation_user_prompt(task, _buggy_source())
        # Oracle fields must NOT appear.
        assert "root_cause_summary" not in prompt
        assert "runtime_evidence_hint" not in prompt
        assert "None handling" not in prompt  # oracle.bug_category
        assert "applies a string operation before normalizing" not in prompt

    def test_generation_prompt_excludes_fixture_tests_and_node_names(self) -> None:
        task = _load_task()
        prompt = tg.build_generation_user_prompt(task, _buggy_source())
        # Existing fixture test source must NOT appear.
        assert "test_missing_display_name_returns_fallback" not in prompt
        assert "test_regular_display_name_is_formatted" not in prompt
        assert "test_whitespace_is_normalized" not in prompt
        # The fixture test file content must NOT appear.
        assert 'format_display_name("Ada Lovelace")' not in prompt

    def test_fix_prompt_excludes_gold_and_includes_frozen_test(self) -> None:
        task = _load_task()
        prompt = tg.build_fix_user_prompt(task, _buggy_source(), _GOOD_TEST)
        assert "PUBLIC BEHAVIOR SPEC" in prompt
        assert "FROZEN REGRESSION TEST" in prompt
        assert "test_none_returns_anonymous" in prompt
        # No gold/fixed code (the correct None-guard) is present.
        assert "if name is None" not in prompt

    def test_behavior_spec_hash_stable(self) -> None:
        # The contract pins this hash; it must not drift.
        assert (
            tg.BEHAVIOR_SPEC_SHA256
            == "c3ce767a7517cef0d7b4a6cc0e3a1df0f8ea0a8968ad07f16103bb9849f936f2"
        )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


class TestExtraction:
    def test_extract_fenced_python(self) -> None:
        raw = "Here is the test:\n```python\n" + _GOOD_TEST + "```\nDone."
        assert tg.extract_code_block(raw) == _GOOD_TEST.strip()

    def test_extract_fenced_no_lang(self) -> None:
        raw = "```\n" + _GOOD_TEST + "```"
        assert tg.extract_code_block(raw) == _GOOD_TEST.strip()

    def test_extract_bare_module_starting_with_import(self) -> None:
        raw = _GOOD_TEST
        assert tg.extract_code_block(raw) == _GOOD_TEST.strip()

    def test_extract_rejects_empty(self) -> None:
        with pytest.raises(tg.ExtractionError, match="empty_response"):
            tg.extract_code_block("")

    def test_extract_rejects_prose_without_code(self) -> None:
        with pytest.raises(tg.ExtractionError, match="no_code_block"):
            tg.extract_code_block("I cannot write that test.")

    def test_extract_diff_fenced(self) -> None:
        raw = "```diff\n" + _GOOD_DIFF + "```"
        assert tg.extract_diff_block(raw) == _GOOD_DIFF.strip()

    def test_extract_diff_bare_header(self) -> None:
        raw = _GOOD_DIFF
        assert tg.extract_diff_block(raw) == _GOOD_DIFF.strip()

    def test_extract_diff_rejects_empty(self) -> None:
        with pytest.raises(tg.ExtractionError, match="empty_response"):
            tg.extract_diff_block("")


# ---------------------------------------------------------------------------
# Executability gate
# ---------------------------------------------------------------------------


class TestExecutabilityGate:
    def test_good_test_is_executable_and_fails_buggy(self, tmp_path: Path) -> None:
        result = tg.check_executable(_GOOD_TEST, FIXTURE_DIR, tmp_path)
        assert result["executable"] is True
        # The good test asserts None -> "Anonymous"; buggy code raises, so FAIL.
        assert result["status"] == "FAIL"

    def test_syntax_bad_test_not_executable(self, tmp_path: Path) -> None:
        result = tg.check_executable(_SYNTAX_BAD_TEST, FIXTURE_DIR, tmp_path)
        assert result["executable"] is False

    def test_two_func_test_not_executable(self, tmp_path: Path) -> None:
        result = tg.check_executable(_TWO_FUNC_TEST, FIXTURE_DIR, tmp_path)
        assert result["executable"] is False
        assert "exactly 1" in result["reason"]

    def test_non_defect_test_executable_and_passes_buggy(self, tmp_path: Path) -> None:
        # The non-defect test is executable but PASSES on buggy code.
        result = tg.check_executable(_NON_DEFECT_TEST, FIXTURE_DIR, tmp_path)
        assert result["executable"] is True
        assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# Full offline pipeline — happy path
# ---------------------------------------------------------------------------


class TestOfflineHappyPath:
    def test_full_pipeline_freeze_buggy_fail_fixed_pass_verifier_resolved(
        self, tmp_path: Path
    ) -> None:
        transport = FakeTransport((
            _fence(_GOOD_TEST, "python"),
            _fence(_GOOD_DIFF, "diff"),
        ))
        evidence = _run_probe_with_transport(transport, tmp_path)

        summary = evidence["summary"]
        assert summary["generated_test_froze"] is True
        assert summary["generated_test_did_not_encode_defect"] is False
        assert summary["buggy_failed_frozen_test"] is True
        assert summary["fixed_code_passed_frozen_test"] is True
        assert summary["verifier_executed"] is True
        assert summary["verifier_resolved"] is True

        # The frozen test source/hash are recorded.
        frozen = evidence["test_generation"]["frozen_test"]
        assert frozen["sha256"] == tg._sha256(_GOOD_TEST.strip())
        assert frozen["attempt_index"] == 0

        # Same frozen test used for both runs.
        assert evidence["buggy_run"]["frozen_test_sha256"] == frozen["sha256"]
        assert evidence["generated_test_eval"]["frozen_test_sha256"] == frozen["sha256"]

        # Verifier is the authority and reports RESOLVED + canonical immutability.
        verifier = evidence["verifier"]
        assert verifier["status"] == "COMPLETED"
        assert verifier["outcome"] == "RESOLVED"
        assert verifier["canonical_fixture_unchanged"] is True
        assert verifier["workspace_cleaned"] is True

    def test_anti_leakage_flags_recorded(self, tmp_path: Path) -> None:
        transport = FakeTransport((
            _fence(_GOOD_TEST, "python"),
            _fence(_GOOD_DIFF, "diff"),
        ))
        evidence = _run_probe_with_transport(transport, tmp_path)
        anti = evidence["test_generation"]["anti_leakage"]
        assert anti["oracle_shown_to_model"] is False
        assert anti["fixed_or_gold_source_shown"] is False
        assert anti["existing_fixture_test_source_shown"] is False
        assert anti["existing_test_node_names_shown"] is False
        assert anti["behavior_spec_is_intentionally_shown"] is True
        fix_anti = evidence["model_fixed_code"]["anti_leakage"]
        assert fix_anti["gold_code_shown"] is False


# ---------------------------------------------------------------------------
# Retry / STOP paths
# ---------------------------------------------------------------------------


class TestRetryAndStop:
    def test_non_executable_then_good_then_diff_freezes(self, tmp_path: Path) -> None:
        # attempt 0: syntax-bad test -> retry; attempt 1: good test -> freeze.
        # Then the fix diff.
        transport = FakeTransport((
            _fence(_SYNTAX_BAD_TEST, "python"),
            _fence(_GOOD_TEST, "python"),
            _fence(_GOOD_DIFF, "diff"),
        ))
        evidence = _run_probe_with_transport(transport, tmp_path)
        summary = evidence["summary"]
        assert summary["generated_test_froze"] is True
        frozen = evidence["test_generation"]["frozen_test"]
        assert frozen["attempt_index"] == 1  # froze on the second attempt
        # Two generation attempts recorded before the fix.
        gen_attempts = evidence["test_generation"]["attempts"]
        assert len(gen_attempts) == 2

    def test_all_non_executable_stops_no_executable_test(self, tmp_path: Path) -> None:
        transport = FakeTransport((
            _fence(_SYNTAX_BAD_TEST, "python"),
            _fence(_SYNTAX_BAD_TEST, "python"),
            _fence(_SYNTAX_BAD_TEST, "python"),
        ))
        evidence = _run_probe_with_transport(transport, tmp_path)
        summary = evidence["summary"]
        assert summary["generated_test_froze"] is False
        assert summary["stop_reason"] == "no_executable_test"
        assert evidence["test_generation"]["frozen_test"] is None
        # No buggy/fixed/verifier runs occurred.
        assert evidence["buggy_run"] is None
        assert evidence["verifier"]["executed"] is False

    def test_executable_test_that_buggy_passes_stops(self, tmp_path: Path) -> None:
        # The generated test is executable but does NOT encode the defect
        # (buggy code passes it). This is a STOP — no regeneration.
        transport = FakeTransport((
            _fence(_NON_DEFECT_TEST, "python"),
            _fence(_GOOD_DIFF, "diff"),
        ))
        evidence = _run_probe_with_transport(transport, tmp_path)
        summary = evidence["summary"]
        assert summary["generated_test_froze"] is True
        assert summary["generated_test_did_not_encode_defect"] is True
        assert summary["stop_reason"] == "generated_test_did_not_encode_defect"
        assert summary["buggy_failed_frozen_test"] is False
        # Only one generation attempt — no retry for an executable test.
        assert len(evidence["test_generation"]["attempts"]) == 1

    def test_transport_failure_stops(self, tmp_path: Path) -> None:
        evidence = _run_probe_with_transport(FailingTransport(), tmp_path)
        summary = evidence["summary"]
        assert summary["generated_test_froze"] is False
        assert summary["stop_reason"] == "transport_failure"
        assert evidence["test_generation"]["frozen_test"] is None

    def test_malformed_fix_diff_is_valid_evidence_not_retried(self, tmp_path: Path) -> None:
        # Good generated test, but the fix response is unparseable prose.
        # One-shot fix: no retry. This is valid evidence (fixed_code not run).
        transport = FakeTransport((
            _fence(_GOOD_TEST, "python"),
            "I cannot produce a diff.",
        ))
        evidence = _run_probe_with_transport(transport, tmp_path)
        summary = evidence["summary"]
        assert summary["generated_test_froze"] is True
        assert summary["buggy_failed_frozen_test"] is True
        # No candidate patch -> fixed code not run, verifier not executed.
        assert evidence["model_fixed_code"]["candidate_patch"] is None
        assert evidence["generated_test_eval"]["status"] == "NOT_RUN"
        assert evidence["verifier"]["executed"] is False
        assert summary["fixed_code_passed_frozen_test"] is False


# ---------------------------------------------------------------------------
# --validate-only provenance
# ---------------------------------------------------------------------------


class TestValidateOnly:
    def test_validate_only_passes_and_reports_provenance(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(PROBE_SCRIPT), "--validate-only"],
            cwd=str(REPO_ROOT), check=True, capture_output=True,
            text=True, timeout=30,
        )
        payload = json.loads(completed.stdout)
        assert payload["status"] == "PASS"
        validation = payload["validation"]
        assert validation["validated"] is True
        assert _COMMIT_SHA_RE.match(validation["source_commit_sha"])
        assert validation["experiment_contract_sha256"]
        assert validation["fixture_tree_sha256"] == (
            "11fcd99767052b52e786eeb9bc3947c8af0d2708322e251fb10da2166e341bec"
        )
        assert validation["behavior_spec_sha256"] == tg.BEHAVIOR_SPEC_SHA256
        identity = payload["run_identity"]
        assert identity["task_id"] == "curated-none-handling-001"
        assert identity["model_condition"] == "RAW_BASE"
        assert identity["adapter_applied"] is False
        assert identity["rag_enabled"] is False
        assert identity["base_revision"] == (
            "c03e6d358207e414f1eca0bb1891e29f1db0e242"
        )


# ---------------------------------------------------------------------------
# Canonical fixture immutability
# ---------------------------------------------------------------------------


class TestCanonicalImmutability:
    def test_canonical_fixture_unchanged_after_offline_run(self, tmp_path: Path) -> None:
        before = probe_mod._fixture_tree_sha256(FIXTURE_DIR)
        transport = FakeTransport((
            _fence(_GOOD_TEST, "python"),
            _fence(_GOOD_DIFF, "diff"),
        ))
        _run_probe_with_transport(transport, tmp_path)
        after = probe_mod._fixture_tree_sha256(FIXTURE_DIR)
        assert before == after