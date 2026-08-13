"""R5.9 fail-closed ACTUAL-PROMPT anti-leakage audit tests.

The final matrix PASS gate requires ``leakage_findings == []`` for every
exact live ``telemetry[*].request.user_prompt_full``.

The decisive regression test: the OLD r5.7 evidence (raw pytest failure
output + unfiltered stacks + raw verifier-record tails in the prompts)
MUST FAIL the new audit — with the exact leak forms FirstMate found
(hidden test source, assertion expressions, expected literals, node ids,
test function names).  Clean scripted prompts built by the r5.9 treatment
must PASS.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.debugger_interaction_v2_r5.anti_leakage import (
    ForbiddenContent,
    audit_evidence_dict,
    derive_forbidden_content,
    scan_evidence,
    scan_prompt,
)

CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"

R5_TASKS = (
    "curated-none-handling-001",
    "curated-off-by-one-002",
    "curated-wrong-branch-003",
    "curated-mutation-alias-004",
    "curated-caller-callee-005",
)

# Tracked, self-contained regression fixture: decisive OLD r5.7 leaked
# prompt excerpts embedded from the preserved (untracked) r5.7 live matrix.
# A fresh Git checkout must run the regression from tracked content only.
OLD_R57_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "old_r57_leakage"


def _old_r57_fixture(task_id: str) -> dict:
    path = OLD_R57_FIXTURE_DIR / f"{task_id}.json"
    assert path.is_file(), f"missing tracked old-r5.7 fixture {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _forbidden(task_id: str) -> ForbiddenContent:
    return derive_forbidden_content(task_id, CURATED_ROOT / task_id)


class TestDeriveForbiddenContent:
    @pytest.mark.parametrize("task_id", R5_TASKS)
    def test_derives_hidden_test_assets(self, task_id):
        forbidden = _forbidden(task_id)
        assert forbidden.f2p_node_ids
        assert forbidden.p2p_node_ids
        assert forbidden.hidden_test_filenames
        assert forbidden.hidden_test_function_names
        assert forbidden.hidden_test_source_lines
        assert forbidden.assertion_source_lines
        assert forbidden.oracle_root_cause_summary
        assert forbidden.oracle_runtime_evidence_hint
        assert forbidden.production_source_lines
        assert forbidden.production_module_path

    def test_001_contains_firstmate_examples(self):
        forbidden = _forbidden("curated-none-handling-001")
        assert "test_missing_display_name_returns_fallback" in (
            forbidden.hidden_test_function_names
        )
        assert any(
            "assert format_display_name(None) == \"Anonymous\""
            in line
            for line in forbidden.assertion_source_lines
        )
        assert "tests/test_display_name.py::test_missing_display_name_returns_fallback" in (
            forbidden.f2p_node_ids
        )

    def test_literals_derived_from_assertions(self):
        forbidden = _forbidden("curated-caller-callee-005")
        assert any("$12.00" in literal for literal in forbidden.expected_literals)


class TestScanPrompt:
    def test_detects_hidden_test_source_block(self):
        forbidden = _forbidden("curated-none-handling-001")
        prompt = (
            "Current phase: RuntimeEvidence\n"
            "Real failure output:\n"
            "    def test_missing_display_name_returns_fallback() -> None:\n"
            ">       assert format_display_name(None) == \"Anonymous\"\n"
        )
        findings = scan_prompt(prompt, forbidden)
        kinds = {f.kind for f in findings}
        assert "hidden_test_function_name" in kinds
        assert "assertion_source_expression" in kinds
        assert "hidden_test_source_line" in kinds

    def test_detects_verifier_node_id_and_hidden_value(self):
        forbidden = _forbidden("curated-none-handling-001")
        prompt = (
            "Failing checks:\n"
            "  [p2p] tests/test_display_name.py::test_regular_display_name_is_formatted (FAIL)\n"
            "      name = 'Ada Lovelace'\n"
        )
        findings = scan_prompt(prompt, forbidden)
        kinds = {f.kind for f in findings}
        assert "hidden_p2p_node_id" in kinds
        assert "hidden_test_function_name" in kinds
        assert "expected_literal" in kinds

    def test_legitimate_source_and_debugger_evidence_not_findings(self):
        """The model legitimately sees the original source, the production
        module path, and real stack/pause frames; none of these may be
        reported as oracle/target/literal findings."""
        forbidden = _forbidden("curated-none-handling-001")
        prompt = (
            "Target script for debugging: display_name.py\n"
            "Source (lines marked with '>' are breakpoint-eligible):\n"
            "    1: def format_display_name(name: str | None) -> str:\n"
            ">   2:     normalized_name = name.strip()\n"
            ">   3:     if not normalized_name:\n"
            ">   4:         return \"Anonymous\"\n"
            ">   5:     return normalized_name.title()\n"
            "Debugger: PDB session paused at line 2 in function "
            "'format_display_name'\n"
            "  * frame_id=0 format_display_name line=2 script=display_name.py\n"
            "  Paused at line 2 in function 'format_display_name' "
            "(breakpoint at line 2)\n"
            "file display_name.py\n"
            "--- a/display_name.py\n"
            "+++ b/display_name.py\n"
        )
        assert scan_prompt(prompt, forbidden) == []

    def test_legitimate_locals_not_findings(self):
        forbidden = _forbidden("curated-none-handling-001")
        prompt = "  name = None\n  normalized_name = 'unnamed'\n"
        assert scan_prompt(prompt, forbidden) == []

    def test_repair_identifier_already_in_original_source_is_not_forbidden(self):
        """A repair token already visible in source/locals is not an oracle."""
        forbidden = _forbidden("curated-off-by-one-002")
        assert "end_index" not in forbidden.reference_repair_snippets
        prompt = "[get_frame_locals] status=ok\n  end_index = 4\n"
        assert scan_prompt(prompt, forbidden) == []

    def test_legitimate_short_word_in_description_not_finding(self):
        """003's public description contains the word 'employee'; the
        expected-literal needle for it is excluded at derivation time."""
        forbidden = _forbidden("curated-wrong-branch-003")
        prompt = (
            "Task:\n"
            "Title: Select the correct access branch\n"
            "Description: An access selector should distinguish the combined "
            "flag case from the individual employee and pass-holder cases.\n"
        )
        assert scan_prompt(prompt, forbidden) == []


class TestOldR57EvidenceFailsAudit:
    """THE regression test: the old leaking r5.7 prompt forms must FAIL the
    fail-closed actual-prompt audit, with the exact leak forms FirstMate
    found in the live prompts.

    Self-contained: the decisive leaked prompt excerpts are embedded in the
    TRACKED fixture ``tests/fixtures/old_r57_leakage/`` (extracted from the
    preserved, untracked r5.7 live matrix), so a fresh Git checkout tests
    the regression without any gitignored run directories.
    """

    @pytest.mark.parametrize("task_id", R5_TASKS)
    def test_old_r57_evidence_fails(self, task_id):
        fixture = _old_r57_fixture(task_id)
        audit = audit_evidence_dict(fixture, task_id, CURATED_ROOT / task_id)
        assert audit["scanned_prompt_count"] > 0
        assert audit["leakage_findings"], (
            f"old r5.7 {task_id} prompt excerpts must fail the audit"
        )
        assert audit["passed"] is False

    def test_old_001_contains_firstmate_exact_leak_forms(self):
        fixture = _old_r57_fixture("curated-none-handling-001")
        all_prompts = "\n".join(
            (rec.get("request") or {}).get("user_prompt_full") or ""
            for rec in fixture.get("telemetry") or []
            if type(rec) is dict
        )
        # The exact leaked forms FirstMate quoted from the live prompts.
        assert "def test_missing_display_name_returns_fallback() -> None:" in all_prompts
        assert "assert format_display_name(None) == \"Anonymous\"" in all_prompts
        # Verifier retry feedback exposes P2P hidden-test source/values.
        assert "test_regular_display_name_is_formatted" in all_prompts
        assert "Ada Lovelace" in all_prompts

    def test_all_five_old_rows_combined_fail(self):
        total = 0
        for task_id in R5_TASKS:
            audit = audit_evidence_dict(
                _old_r57_fixture(task_id), task_id, CURATED_ROOT / task_id
            )
            total += len(audit["leakage_findings"])
        assert total > 0

    def test_fixture_does_not_depend_on_untracked_run_directories(self):
        """The tracked fixture is the only source of the old leaked prompts:
        the regression must run from a fresh Git checkout."""
        for task_id in R5_TASKS:
            fixture = _old_r57_fixture(task_id)
            assert fixture.get("schema_version") == "old-r5.7-leaked-prompt-excerpts-v1"
            assert len(fixture.get("telemetry") or []) >= 3


class TestCleanPromptsPassAudit:
    """Clean scripted r5.9 prompts (sanitized diagnostics + region-filtered
    stacks + sanitized verifier feedback) must PASS with zero findings."""

    def test_sanitized_reproduction_prompt_passes(self):
        forbidden = _forbidden("curated-none-handling-001")
        prompt = (
            "Current phase: RuntimeEvidence\n"
            "Last observation:\n"
            "[run_reproduction] status=ok\n"
            "  phase=baseline exit_code=1 passed=False failure_reproduced=True\n"
            "  Real failure diagnostic (sanitized, from the reproduction run):\n"
            "    baseline failure reproduced\n"
            "    production exception:\n"
            "      display_name.py:2: AttributeError\n"
            "      AttributeError: 'NoneType' object has no attribute 'strip'\n"
        )
        assert scan_prompt(prompt, forbidden) == []

    def test_sanitized_verifier_feedback_prompt_passes(self):
        forbidden = _forbidden("curated-none-handling-001")
        prompt = (
            "Last observation:\n"
            "[apply_patch] status=ok\n"
            "  Real verifier (independent EvaluationVerifier): "
            "status=COMPLETED outcome=BREAKING_RESOLVED f2p=1/1 p2p=0/2 "
            "full_suite=FAIL syntax=True\n"
            "  Failing checks:\n"
            "    [p2p] FAIL\n"
            "      Candidate runtime exception:\n"
            "        display_name.py:4: NameError: name 'normalized_name' "
            "is not defined\n"
        )
        assert scan_prompt(prompt, forbidden) == []

    @pytest.mark.parametrize("task_id", R5_TASKS)
    def test_generic_assertion_diagnostic_passes(self, task_id):
        forbidden = _forbidden(task_id)
        prompt = (
            "Real failure diagnostic (sanitized, from the reproduction run):\n"
            "    baseline behavioral check failed after executing the "
            "target behavior\n"
        )
        assert scan_prompt(prompt, forbidden) == []

    def test_model_authored_diagnosis_rendered_back_is_not_finding(self):
        """The model's OWN diagnosis (which legitimately names the
        production function it observed) is rendered back into the PATCH
        prompt; it is model-authored evidence, never hidden-test content."""
        forbidden = _forbidden("curated-none-handling-001")
        evidence = {
            "telemetry": [
                {
                    "controller_state": "Patch",
                    "request": {
                        "user_prompt_full": (
                            "Your diagnosis (from debugger evidence):\n"
                            "The function `format_display_name` encountered "
                            "an `AttributeError` because the variable `name` "
                            "was `None` when the `strip()` method was called "
                            "on it.\n"
                        )
                    },
                    "translated_directive": {
                        "is_diagnosis": True,
                        "diagnosis_text": (
                            "The function `format_display_name` encountered "
                            "an `AttributeError` because the variable `name` "
                            "was `None` when the `strip()` method was called "
                            "on it."
                        ),
                    },
                }
            ]
        }
        assert scan_evidence(evidence, forbidden) == []
