"""Unit tests for the S1-P frozen patch serialization normalization diagnostic.

Covers the deterministic normalizer and the post-hoc diagnostic over the
EXACT frozen artifacts of S1-P Live Run 1 (self-verifying SHA-256 constants):

* the normalizer removes exactly the unsupported ``diff --git`` git
  metadata line and re-derives the mismatched hunk-header count, with
  zero semantic hunk body changes (proven by hash);
* clean patches are untouched (no-op);
* unsupported metadata with create/delete/mode semantics fails closed;
* the raw live patch still reproduces the live rejection through the same
  PatchManager path; the normalized patch applies, passes the SAME frozen
  generated test, and the independent EvaluationVerifier returns RESOLVED
  with F2P 1/1 and P2P 2/2;
* canonical fixture immutability and workspace cleanup;
* the diagnostic runner verifies the live evidence identity end-to-end.

No model call; no gold code; no fixture-test-based repair.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.runtime.patcher import (
    PatchManager,
    PatchValidationError,
    _parse_unified_diff,
)
from agentic_debugger.runtime.workspace import TaskWorkspace

from experiments.model_generated_test_probe import probe as probe_mod
from experiments.model_generated_test_probe.generated_test_runner import run_fixed
from experiments.model_generated_test_probe.serialization_diagnostic import (
    EXPECTED_CANDIDATE_PATCH_SHA256,
    EXPECTED_FROZEN_TEST_SHA256,
    EXPECTED_RAW_FIX_RESPONSE_SHA256,
    EXPECTED_SOURCE_COMMIT_SHA,
    run_diagnostic,
)
from experiments.model_generated_test_probe.serialization_normalizer import (
    NormalizationError,
    normalize_patch,
    semantic_hunk_lines,
    strip_git_metadata_only,
)

FIXTURE_DIR = (
    REPO_ROOT / "agentic_debugger" / "datasets" / "curated" / "curated-none-handling-001"
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Exact frozen artifacts of S1-P Live Run 1 (from the live evidence.json)
# ---------------------------------------------------------------------------

# The model's raw one-shot fix response (``raw_model_response``).
RAW_FIX_RESPONSE = (
    "```diff\n"
    "diff --git a/display_name.py b/display_name.py\n"
    "--- a/display_name.py\n"
    "+++ b/display_name.py\n"
    "@@ -1,5 +1,6 @@\n"
    " def format_display_name(name: str | None) -> str:\n"
    "+    if name is None:\n"
    "+        return \"Anonymous\"\n"
    "     normalized_name = name.strip()\n"
    "     if not normalized_name:\n"
    "         return \"Anonymous\"\n"
    "```"
)

# The extracted original candidate patch (``candidate_patch``).
RAW_CANDIDATE_PATCH = (
    "diff --git a/display_name.py b/display_name.py\n"
    "--- a/display_name.py\n"
    "+++ b/display_name.py\n"
    "@@ -1,5 +1,6 @@\n"
    " def format_display_name(name: str | None) -> str:\n"
    "+    if name is None:\n"
    "+        return \"Anonymous\"\n"
    "     normalized_name = name.strip()\n"
    "     if not normalized_name:\n"
    "         return \"Anonymous\""
)

# The frozen generated regression test (``frozen_test.source``).
FROZEN_TEST_SOURCE = (
    "from display_name import format_display_name\n"
    "\n"
    "def test_format_display_name():\n"
    "    assert format_display_name(None) == \"Anonymous\"\n"
    "    assert format_display_name(\"  \") == \"Anonymous\"\n"
    "    assert format_display_name(\"john doe\") == \"John Doe\"\n"
    "    assert format_display_name(\"jane doe  \") == \"Jane Doe\"\n"
    "    assert format_display_name(\"  jane doe  \") == \"Jane Doe\"\n"
    "    assert format_display_name(\"\") == \"Anonymous\""
)

# Expected normalized patch (N1 + N2, semantic body untouched).
EXPECTED_NORMALIZED_PATCH = (
    "--- a/display_name.py\n"
    "+++ b/display_name.py\n"
    "@@ -1,4 +1,6 @@\n"
    " def format_display_name(name: str | None) -> str:\n"
    "+    if name is None:\n"
    "+        return \"Anonymous\"\n"
    "     normalized_name = name.strip()\n"
    "     if not normalized_name:\n"
    "         return \"Anonymous\""
)

# Expected fixed source after applying the normalized patch to the buggy file.
EXPECTED_FIXED_SOURCE = (
    "def format_display_name(name: str | None) -> str:\n"
    "    if name is None:\n"
    "        return \"Anonymous\"\n"
    "    normalized_name = name.strip()\n"
    "    if not normalized_name:\n"
    "        return \"Anonymous\"\n"
    "    return normalized_name.title()\n"
)


def test_frozen_artifact_constants_self_verify() -> None:
    """The embedded constants must match the S1-P Live Run 1 hashes."""
    assert _sha256(RAW_CANDIDATE_PATCH) == EXPECTED_CANDIDATE_PATCH_SHA256
    assert _sha256(RAW_FIX_RESPONSE) == EXPECTED_RAW_FIX_RESPONSE_SHA256
    assert _sha256(FROZEN_TEST_SOURCE) == EXPECTED_FROZEN_TEST_SHA256


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


class TestNormalizer:
    def test_live_patch_operations_and_semantic_identity(self) -> None:
        result = normalize_patch(RAW_CANDIDATE_PATCH)

        # Exactly two deterministic serialization operations.
        kinds = [op.kind for op in result.operations]
        assert kinds == ["remove_git_metadata", "correct_hunk_header_counts"]
        assert result.removed_lines == (
            "diff --git a/display_name.py b/display_name.py",
        )
        header_op = result.operations[1]
        assert header_op.original_line == "@@ -1,5 +1,6 @@"
        assert header_op.replacement == "@@ -1,4 +1,6 @@"

        # Semantic hunk identity: byte-identical before vs after.
        assert result.semantic_hunks_identical is True
        assert (result.semantic_hunk_before_sha256
                == result.semantic_hunk_after_sha256)

        # Exact normalized serialization.
        assert result.normalized_patch == EXPECTED_NORMALIZED_PATCH
        # The project's own parser accepts the normalized patch.
        parsed = _parse_unified_diff(result.normalized_patch)
        assert len(parsed) == 1
        assert parsed[0].path == "display_name.py"

    def test_clean_patch_is_noop(self) -> None:
        clean = (
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
        result = normalize_patch(clean)
        assert result.operations == ()
        assert result.removed_lines == ()
        assert result.normalized_patch == clean
        assert result.semantic_hunks_identical is True

    def test_unsupported_semantic_metadata_fails_closed(self) -> None:
        bad = (
            "diff --git a/display_name.py b/display_name.py\n"
            "new file mode 100644\n"
            "--- a/display_name.py\n"
            "+++ b/display_name.py\n"
            "@@ -1,5 +1,6 @@\n"
            " def format_display_name(name: str | None) -> str:\n"
            "+    if name is None:\n"
            "+        return \"Anonymous\"\n"
            "     normalized_name = name.strip()\n"
            "     if not normalized_name:\n"
            "         return \"Anonymous\"\n"
        )
        with pytest.raises(NormalizationError, match="new file"):
            normalize_patch(bad)

    def test_correct_counts_are_left_untouched(self) -> None:
        # Header counts match the body (5 old / 7 new) -> no operation.
        patch = (
            "--- a/display_name.py\n"
            "+++ b/display_name.py\n"
            "@@ -1,5 +1,7 @@\n"
            " def format_display_name(name: str | None) -> str:\n"
            "+    if name is None:\n"
            "+        return \"Anonymous\"\n"
            "     normalized_name = name.strip()\n"
            "     if not normalized_name:\n"
            "         return \"Anonymous\"\n"
            "     return normalized_name.title()\n"
        )
        result = normalize_patch(patch)
        assert result.operations == ()
        assert result.normalized_patch == patch

    def test_semantic_hunk_hash_is_content_sensitive(self) -> None:
        # A semantically DIFFERENT hunk must produce a different hash.
        other = EXPECTED_NORMALIZED_PATCH.replace(
            'return "Anonymous"', 'return "UNKNOWN"'
        )
        assert (semantic_hunk_lines(EXPECTED_NORMALIZED_PATCH)
                != semantic_hunk_lines(other))

    def test_strip_git_metadata_only_keeps_header_counts(self) -> None:
        stripped, removed = strip_git_metadata_only(RAW_CANDIDATE_PATCH)
        assert removed == ("diff --git a/display_name.py b/display_name.py",)
        assert "@@ -1,5 +1,6 @@" in stripped  # N2 NOT applied here
        # N1-only still fails PatchManager: the header over-counts the body.
        with pytest.raises(PatchValidationError, match="old_count=5"):
            _parse_unified_diff(stripped)


# ---------------------------------------------------------------------------
# End-to-end diagnostic over the frozen live artifacts
# ---------------------------------------------------------------------------


class TestDiagnostic:
    def test_raw_patch_reproduces_live_rejection(self, tmp_path: Path) -> None:
        from experiments.model_generated_test_probe.test_generation import (
            FrozenTest,
        )
        frozen = FrozenTest(
            source=FROZEN_TEST_SOURCE,
            sha256=EXPECTED_FROZEN_TEST_SHA256,
            attempt_index=0,
            raw_response_text=RAW_FIX_RESPONSE,
            raw_response_sha256=EXPECTED_RAW_FIX_RESPONSE_SHA256,
            system_prompt_sha256="unused",
            user_prompt_sha256="unused",
            transport_error_category=None,
            usage={},
            executability={},
        )
        result = run_fixed(frozen, RAW_CANDIDATE_PATCH, FIXTURE_DIR, tmp_path)
        assert result.patch_applied is False
        assert result.patch_error == (
            "PatchValidationError: Git metadata lines are not supported"
        )
        assert result.status == "NOT_RUN"
        assert result.workspace_cleaned is True

    def test_normalized_patch_applies_to_correct_fixed_source(
        self, tmp_path: Path
    ) -> None:
        result = normalize_patch(RAW_CANDIDATE_PATCH)
        workspace = TaskWorkspace(str(FIXTURE_DIR), parent_dir=str(tmp_path))
        try:
            manager = PatchManager(
                workspace,
                allowed_paths=["display_name.py"],
                denied_paths=["tests", "task.json"],
            )
            applied = manager.apply_patch(result.normalized_patch)
            assert applied.success is True
            fixed = (Path(workspace.root) / "display_name.py").read_text(
                encoding="utf-8"
            )
            assert fixed == EXPECTED_FIXED_SOURCE
        finally:
            workspace.cleanup()

    def test_frozen_test_passes_on_normalized_patch(self, tmp_path: Path) -> None:
        from experiments.model_generated_test_probe.test_generation import (
            FrozenTest,
        )
        result = normalize_patch(RAW_CANDIDATE_PATCH)
        frozen = FrozenTest(
            source=FROZEN_TEST_SOURCE,
            sha256=EXPECTED_FROZEN_TEST_SHA256,
            attempt_index=0,
            raw_response_text=RAW_FIX_RESPONSE,
            raw_response_sha256=EXPECTED_RAW_FIX_RESPONSE_SHA256,
            system_prompt_sha256="unused",
            user_prompt_sha256="unused",
            transport_error_category=None,
            usage={},
            executability={},
        )
        run = run_fixed(
            frozen, result.normalized_patch, FIXTURE_DIR, tmp_path,
        )
        assert run.patch_applied is True
        assert run.status == "PASS"
        assert run.workspace_cleaned is True

    def test_verifier_resolves_normalized_patch(self, tmp_path: Path) -> None:
        result = normalize_patch(RAW_CANDIDATE_PATCH)
        task = load_task(str(FIXTURE_DIR / "task.json"))
        evaluation = EvaluationVerifier(
            str(REPO_ROOT), workspace_parent=str(tmp_path)
        ).evaluate(task, result.normalized_patch)
        assert evaluation.outcome.value == "RESOLVED"
        assert evaluation.f2p_passed == 1 and evaluation.f2p_total == 1
        assert evaluation.p2p_passed == 2 and evaluation.p2p_total == 2
        assert evaluation.workspace is not None
        assert evaluation.workspace.canonical_fixture_unchanged is True
        assert evaluation.workspace.cleaned is True

    def test_canonical_fixture_immutable(self, tmp_path: Path) -> None:
        before = probe_mod._fixture_tree_sha256(FIXTURE_DIR)
        result = normalize_patch(RAW_CANDIDATE_PATCH)
        run_fixed_result = _frozen_run_result(tmp_path, result.normalized_patch)
        assert run_fixed_result is not None
        after = probe_mod._fixture_tree_sha256(FIXTURE_DIR)
        assert before == after


def _frozen_run_result(tmp_path: Path, normalized_patch: str):
    from experiments.model_generated_test_probe.test_generation import FrozenTest
    frozen = FrozenTest(
        source=FROZEN_TEST_SOURCE,
        sha256=EXPECTED_FROZEN_TEST_SHA256,
        attempt_index=0,
        raw_response_text=RAW_FIX_RESPONSE,
        raw_response_sha256=EXPECTED_RAW_FIX_RESPONSE_SHA256,
        system_prompt_sha256="unused",
        user_prompt_sha256="unused",
        transport_error_category=None,
        usage={},
        executability={},
    )
    return run_fixed(frozen, normalized_patch, FIXTURE_DIR, tmp_path)


def _synthetic_live_evidence() -> dict:
    """A minimal evidence dict carrying the EXACT frozen live artifacts."""
    gen_usage = {"provider_reported": True, "prompt_tokens": 329,
                 "completion_tokens": 98, "total_tokens": 427}
    fix_usage = {"provider_reported": True, "prompt_tokens": 447,
                 "completion_tokens": 83, "total_tokens": 530}
    contract = probe_mod._load_contract()
    return {
        "run_identity": {
            "task_id": "curated-none-handling-001",
            "source_commit_sha": EXPECTED_SOURCE_COMMIT_SHA,
            "model_condition": "RAW_BASE",
            "adapter_applied": False,
            "base_repository": "Qwen/Qwen2.5-Coder-7B-Instruct",
            "base_revision": "c03e6d358207e414f1eca0bb1891e29f1db0e242",
            "rag_enabled": False,
            "experiment_contract_sha256":
                "3d7c7e8d5ab17f523e4a03d20282363c6801dec8ef0a2a44bdd4b75934685bd0",
            "behavior_spec_sha256":
                contract["behavior_spec"]["sha256"],
            "system_prompt_generation_sha256":
                contract["prompts"]["system_prompt_generation_sha256"],
            "system_prompt_fix_sha256":
                contract["prompts"]["system_prompt_fix_sha256"],
        },
        "test_generation": {
            "system_prompt_sha256": contract["prompts"]["system_prompt_generation_sha256"],
            "behavior_spec": "public behavior spec (frozen)",
            "attempts": [{
                "attempt_index": 0,
                "raw_response_text": RAW_FIX_RESPONSE,
                "transport_error_category": None,
            }],
            "frozen_test": {
                "source": FROZEN_TEST_SOURCE,
                "sha256": EXPECTED_FROZEN_TEST_SHA256,
                "attempt_index": 0,
                "raw_response_sha256": "8cbebc0e8a5695dab400ea3fce8870bd829d20a00547de3363433aef0860a256",
                "user_prompt_sha256": "c87ff5ea481601f046503a64e44cf6d5c57a38cac238c6bfcbfc0bfc731dc8ca",
                "usage": gen_usage,
                "executability": {"executable": True, "status": "FAIL"},
            },
        },
        "model_fixed_code": {
            "candidate_patch": RAW_CANDIDATE_PATCH,
            "candidate_patch_sha256": EXPECTED_CANDIDATE_PATCH_SHA256,
            "raw_model_response": RAW_FIX_RESPONSE,
            "raw_response_sha256": EXPECTED_RAW_FIX_RESPONSE_SHA256,
            "usage": fix_usage,
        },
        "buggy_run": {"frozen_test_sha256": EXPECTED_FROZEN_TEST_SHA256},
        "generated_test_eval": {"frozen_test_sha256": EXPECTED_FROZEN_TEST_SHA256},
    }


class TestDiagnosticRunner:
    def test_run_diagnostic_full_pipeline(self, tmp_path: Path) -> None:
        summary = run_diagnostic(_synthetic_live_evidence(), tmp_path)

        # Evidence identity gate passed.
        assert summary["evidence_verification"]["all_ok"] is True

        # ORIGINAL LIVE RESULT reproduced deterministically.
        assert summary["original_live_result"]["patch_manager_rejection"] == (
            "PatchValidationError: Git metadata lines are not supported"
        )
        assert summary["original_live_result"]["resolved"] is False

        # POST-HOC diagnostic.
        diag = summary["post_hoc_serialization_diagnostic"]
        assert diag["normalized_patch"] == EXPECTED_NORMALIZED_PATCH
        assert diag["normalization"]["semantic_hunks_identical"] is True
        assert diag["normalization"]["n1_only_remaining_parser_error"] is not None
        assert "old_count=5" in diag["normalization"]["n1_only_remaining_parser_error"]
        assert diag["apply_result"]["patch_applied"] is True
        assert diag["frozen_generated_test"]["sha_matches_live_run_1"] is True
        assert diag["frozen_generated_test"]["result"]["status"] == "PASS"
        assert diag["verifier_f2p"]["outcome"] == "RESOLVED"
        assert diag["verifier_f2p"]["resolved"] is True
        assert diag["verifier_f2p"]["f2p_passed"] == 1
        assert diag["verifier_f2p"]["p2p_passed"] == 2
        assert diag["canonical_fixture_unchanged"] is True
        assert diag["workspace_cleaned"] is True
        assert diag["verifier_workspace_cleaned"] is True

        # Corrected token arithmetic.
        usage = summary["corrected_combined_usage"]
        assert usage["prompt_tokens"] == 776
        assert usage["completion_tokens"] == 181
        assert usage["total_tokens"] == 957

        # Evidence files written.
        for name in (
            "diagnostic-summary.json", "diagnostic-report.md",
            "raw_candidate_patch.patch", "normalized_candidate_patch.patch",
            "normalization-operations.json", "semantic-hunk-before.txt",
            "semantic-hunk-after.txt", "frozen_generated_test.py",
            "frozen-test-run.json", "verifier-result.json",
            "original-evidence-summary.json",
        ):
            assert (tmp_path / name).is_file(), name
        assert (tmp_path / "normalized_candidate_patch.patch").read_text(
            encoding="utf-8"
        ) == EXPECTED_NORMALIZED_PATCH
        assert (tmp_path / "frozen_generated_test.py").read_text(
            encoding="utf-8"
        ) == FROZEN_TEST_SOURCE

    def test_run_diagnostic_stops_on_identity_mismatch(self, tmp_path: Path) -> None:
        evidence = _synthetic_live_evidence()
        evidence["run_identity"]["source_commit_sha"] = "0" * 40
        with pytest.raises(RuntimeError, match="identity verification FAILED"):
            run_diagnostic(evidence, tmp_path)
