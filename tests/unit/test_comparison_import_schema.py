"""Strict imported-generation schema and verifier-backed execution tests.

Covers the repair-1 raw-output-to-patch binding contract: unrelated passing
patches are rejected, one-byte modifications are rejected, offsets outside
the raw output are rejected, malformed diffs stay invalid, patch-only output
verifies, prose-plus-diff extraction is exact, raw output is stored
byte-for-byte, and a RESOLVED result proves the verified patch originated in
the raw output.  Also covers telemetry preservation, the EvaluationInputError
branch, and response-bound behavior.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agentic_debugger.comparison.import_schema import (
    MAX_RAW_OUTPUT_BYTES,
    GenerationArtifact,
    ImportError,
    derive_patch,
    extraction_for_substring,
    run_imported_attempt,
)
from agentic_debugger.comparison.schema import AttemptRecord, bound_response_text
from agentic_debugger.evaluation.task_schema import DebugTask

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-off-by-one-002"

_DIFF_PATCH = (
    "--- a/recent_window.py\n+++ b/recent_window.py\n"
    "@@ -1,1 +1,1 @@\n-old\n+new\n"
)


def _artifact_mapping(**overrides):
    patch = overrides.pop("patch", _DIFF_PATCH)
    raw_output = overrides.pop("raw_output", f"Synthetic generation output.\n{patch}")
    extraction = overrides.pop(
        "patch_extraction",
        None if patch is None else extraction_for_substring(raw_output, patch),
    )
    mapping = {
        "schema_version": "generation-artifact-v1",
        "experiment_id": "test-experiment",
        "attempt_id": f"{TASK_ID}:base",
        "condition_id": "base",
        "task_id": TASK_ID,
        "model_repository": "offline-deterministic-demo",
        "model_revision": "rev1",
        "adapter_identity": None,
        "prompt_contract": "generation-artifact-v1:test",
        "generation_config": {"temperature": 0.0, "synthetic": True},
        "raw_output": raw_output,
        "patch_extraction": extraction,
        "patch": patch,
        "runtime_ms": None,
        "memory_bytes": None,
        "cost": None,
        "tokens": None,
        "external_provider_attempts": None,
        "external_network_attempts": None,
        "provenance": {"generator": "offline-deterministic-demo", "note": "test"},
    }
    mapping.update(overrides)
    return mapping


def _artifact(**overrides) -> GenerationArtifact:
    return GenerationArtifact.from_mapping(_artifact_mapping(**overrides))


# ---------------------------------------------------------------------------
# Raw-output-to-patch binding
# ---------------------------------------------------------------------------


def test_unrelated_passing_patch_plus_failing_raw_output_is_rejected():
    """A passing patch not produced in the raw output must never verify."""
    with pytest.raises(ImportError):
        _artifact(raw_output="completely unrelated prose, no patch inside",
                  patch=_DIFF_PATCH,
                  patch_extraction={"mode": "exact"})


def test_patch_modified_by_one_byte_after_extraction_is_rejected():
    patch = _DIFF_PATCH
    raw = f"prose\n{patch}"
    extraction = extraction_for_substring(raw, patch)
    modified = patch[:-1] + ("x" if not patch.endswith("x") else "y")
    with pytest.raises(ImportError):
        _artifact(raw_output=raw, patch=modified, patch_extraction=extraction)


def test_extraction_offsets_outside_raw_output_are_rejected():
    patch = _DIFF_PATCH
    raw = f"prose\n{patch}"
    with pytest.raises(ImportError):
        derive_patch(raw, patch, {"mode": "substring", "start": 0, "end": 10**9})
    with pytest.raises(ImportError):
        derive_patch(raw, patch, {"mode": "substring", "start": 5, "end": 2})


def test_extraction_offsets_that_split_utf8_are_rejected():
    patch = _DIFF_PATCH
    raw = f"prose \u00e9\u00e9\n{patch}"
    extraction = extraction_for_substring(raw, patch)
    with pytest.raises(ImportError):
        derive_patch(raw, patch, {"mode": "substring",
                                  "start": extraction["start"] - 1,
                                  "end": extraction["start"] + 1})


def test_malformed_raw_diff_remains_invalid(tmp_path: Path):
    """A malformed diff inside the raw output stays invalid; the strict
    parser and verifier decide (apply-phase rejection, distinct category)."""
    artifact = _artifact(
        raw_output="model output with a broken patch:\nthis is not a diff",
        patch="this is not a diff",
    )
    task = DebugTask.from_file(str(FIXTURES / TASK_ID / "task.json"))
    attempt = run_imported_attempt(
        artifact,
        task=task,
        repository_root=str(ROOT),
        workspace_parent=str(tmp_path),
    )
    assert attempt["generation_produced"] is True
    assert attempt["valid_patch"] is False
    assert attempt["verifier_outcome"] is None
    assert attempt["failure_category"] in ("PATCH_INVALID", "PATCH_NOT_APPLIED")


def test_exact_patch_only_output_verifies():
    artifact = _artifact(
        raw_output=_DIFF_PATCH, patch=_DIFF_PATCH, patch_extraction={"mode": "exact"}
    )
    assert artifact.patch == artifact.raw_output


def test_prose_plus_diff_extracts_exact_substring_only():
    patch = _DIFF_PATCH
    raw = f"Analysis prose.\n\n{patch}\n\nTrailing prose."
    artifact = _artifact(raw_output=raw, patch=patch)
    assert artifact.patch == patch
    assert artifact.patch_extraction["mode"] == "substring"
    assert artifact.patch not in ("Analysis prose.", "Trailing prose.")
    derived = derive_patch(raw, patch, artifact.patch_extraction)
    assert derived == patch


def test_raw_output_stored_byte_for_byte():
    raw = f"Prose \u00e9\u00e9 with unicode.\n{_DIFF_PATCH}"
    artifact = _artifact(raw_output=raw, patch=_DIFF_PATCH)
    text = artifact.to_text()
    reloaded = GenerationArtifact.from_text(text)
    assert reloaded.raw_output == raw
    assert reloaded.identity() == artifact.identity()


def test_imported_resolved_proves_patch_originated_in_raw_output(tmp_path: Path):
    from agentic_debugger.demo.catalog import build_reference_patch, scenario_for

    scenario = scenario_for(TASK_ID)
    fixture = FIXTURES / TASK_ID
    source = (fixture / scenario.reference_repair.target_path).read_text(encoding="utf-8")
    patch = build_reference_patch(source, scenario.reference_repair)
    raw = f"Synthetic offline deterministic generation.\nCandidate patch:\n{patch}"
    artifact = _artifact(raw_output=raw, patch=patch)
    task = DebugTask.from_file(str(fixture / "task.json"))
    attempt = run_imported_attempt(
        artifact, task=task, repository_root=str(ROOT), workspace_parent=str(tmp_path)
    )
    assert attempt["valid_patch"] is True
    assert attempt["verifier_outcome"] == "RESOLVED"
    # The verified patch is bound to the raw output: the source identity is
    # the artifact identity, which covers the raw output byte-for-byte.
    assert attempt["source_identity"].startswith("generation-artifact-v1:")
    assert artifact.patch == patch


def test_patch_requires_extraction_contract_and_vice_versa():
    with pytest.raises(ImportError):
        _artifact(patch=_DIFF_PATCH, patch_extraction=None)
    with pytest.raises(ImportError):
        _artifact(patch=None, patch_extraction={"mode": "exact"})
    with pytest.raises(ImportError):
        _artifact(patch_extraction={"mode": "fence"})


# ---------------------------------------------------------------------------
# Schema strictness and telemetry
# ---------------------------------------------------------------------------


def test_artifact_round_trip_and_identity_stability():
    artifact = _artifact()
    text = artifact.to_text()
    reloaded = GenerationArtifact.from_text(text)
    assert reloaded.identity() == artifact.identity()
    assert reloaded.raw_output.startswith("Synthetic generation output.")


def test_artifact_rejects_unknown_and_missing_fields():
    mapping = _artifact_mapping()
    mapping["extra"] = 1
    with pytest.raises(ImportError):
        GenerationArtifact.from_mapping(mapping)
    del mapping["extra"]
    del mapping["raw_output"]
    with pytest.raises(ImportError):
        GenerationArtifact.from_mapping(mapping)


def test_artifact_rejects_wrong_schema_version():
    with pytest.raises(ImportError):
        GenerationArtifact.from_mapping(_artifact_mapping(schema_version="generation-artifact-v0"))


def test_artifact_rejects_non_finite_numbers():
    with pytest.raises(ImportError):
        _artifact(cost=float("nan"))
    with pytest.raises(ImportError):
        _artifact(cost=float("inf"))


def test_artifact_rejects_nested_nan_in_generation_config():
    with pytest.raises(ImportError):
        _artifact(generation_config={"nested": {"temperature": float("nan")}})
    with pytest.raises(ImportError):
        _artifact(provenance={"nested": {"value": float("inf")}})


def test_artifact_rejects_oversized_raw_output():
    with pytest.raises(ImportError):
        _artifact(raw_output="x" * (MAX_RAW_OUTPUT_BYTES + 1))


def test_artifact_rejects_invalid_identifiers():
    with pytest.raises(ImportError):
        _artifact(attempt_id="bad id!")
    with pytest.raises(ImportError):
        _artifact(condition_id="Upper Case")


def test_external_telemetry_is_preserved(tmp_path: Path):
    artifact = _artifact(
        runtime_ms=1500, memory_bytes=8192, cost=0.0123, tokens=987,
        external_provider_attempts=4, external_network_attempts=2,
    )
    task = DebugTask.from_file(str(FIXTURES / TASK_ID / "task.json"))
    attempt = run_imported_attempt(
        artifact, task=task, repository_root=str(ROOT), workspace_parent=str(tmp_path)
    )
    assert attempt["memory_bytes"] == 8192
    assert attempt["cost"] == 0.0123
    assert attempt["tokens"] == 987
    assert attempt["external_provider_attempts"] == 4
    assert attempt["external_network_attempts"] == 2
    # Local verification offline counters stay separate and zero.
    assert attempt["provider_attempts"] == 0
    assert attempt["network_attempts"] == 0


def test_evaluation_input_error_branch_never_leaves_evaluation_unbound(tmp_path: Path):
    """A strict-parse rejection (NUL byte in the patch) must not raise
    UnboundLocalError."""
    malformed = "bad\x00patch"
    artifact = _artifact(
        raw_output=f"model output with a broken patch\n{malformed}",
        patch=malformed,
    )
    task = DebugTask.from_file(str(FIXTURES / TASK_ID / "task.json"))
    attempt = run_imported_attempt(
        artifact, task=task, repository_root=str(ROOT), workspace_parent=str(tmp_path)
    )
    assert attempt["valid_patch"] is False
    assert attempt["verifier_evidence"] is None
    assert attempt["failure_category"] == "PATCH_INVALID"


def test_no_patch_attempt_is_recorded_honestly(tmp_path: Path):
    artifact = _artifact(patch=None, raw_output="generation without a patch")
    task = DebugTask.from_file(str(FIXTURES / TASK_ID / "task.json"))
    attempt = run_imported_attempt(
        artifact, task=task, repository_root=str(ROOT), workspace_parent=str(tmp_path)
    )
    assert attempt["generation_produced"] is True
    assert attempt["valid_patch"] is False
    assert attempt["failure_category"] == "NO_PATCH"
    assert attempt["verifier_evidence"] is None
    assert attempt["provider_attempts"] == 0
    assert attempt["network_attempts"] == 0


def test_empty_generation_is_not_produced(tmp_path: Path):
    artifact = _artifact(raw_output="   ", patch=None)
    task = DebugTask.from_file(str(FIXTURES / TASK_ID / "task.json"))
    attempt = run_imported_attempt(
        artifact, task=task, repository_root=str(ROOT), workspace_parent=str(tmp_path)
    )
    assert attempt["generation_produced"] is False
    assert attempt["failure_category"] == "NO_GENERATION"


def test_response_size_64_to_256_kib_is_supported(tmp_path: Path):
    """A valid artifact with a large raw output must produce a valid attempt
    record; response storage is bounded with a marker inside the cap."""
    from agentic_debugger.comparison.schema import MAX_RESPONSE_TEXT_BYTES

    patch = _DIFF_PATCH
    filler = "y" * (120 * 1024)
    raw = f"{filler}\n{patch}"
    artifact = _artifact(raw_output=raw, patch=patch)
    assert len(raw.encode("utf-8")) > MAX_RESPONSE_TEXT_BYTES
    task = DebugTask.from_file(str(FIXTURES / TASK_ID / "task.json"))
    attempt = run_imported_attempt(
        artifact, task=task, repository_root=str(ROOT), workspace_parent=str(tmp_path)
    )
    record = AttemptRecord.from_mapping(attempt)  # must not fail on size limits
    assert record.response_text.endswith("[response-truncated]\n")
    assert len(record.response_text.encode("utf-8")) <= MAX_RESPONSE_TEXT_BYTES
    assert record.response_sha256 == hashlib.sha256(
        record.response_text.encode("utf-8")
    ).hexdigest()


def test_task_mismatch_is_rejected(tmp_path: Path):
    artifact = _artifact(task_id="curated-none-handling-001")
    task = DebugTask.from_file(str(FIXTURES / TASK_ID / "task.json"))
    with pytest.raises(ImportError):
        run_imported_attempt(
            artifact, task=task, repository_root=str(ROOT), workspace_parent=str(tmp_path)
        )


def test_attempt_record_validates_with_role(tmp_path: Path):
    from agentic_debugger.demo.catalog import build_reference_patch, scenario_for

    scenario = scenario_for(TASK_ID)
    fixture = FIXTURES / TASK_ID
    source = (fixture / scenario.reference_repair.target_path).read_text(encoding="utf-8")
    patch = build_reference_patch(source, scenario.reference_repair)
    artifact = _artifact(
        raw_output=f"Synthetic generation.\n{patch}", patch=patch
    )
    task = DebugTask.from_file(str(fixture / "task.json"))
    attempt = run_imported_attempt(
        artifact, task=task, repository_root=str(ROOT), workspace_parent=str(tmp_path),
        role="preference-fixture",
    )
    assert attempt["valid_patch"] is True
    assert attempt["verifier_outcome"] == "RESOLVED"
    assert attempt["failure_category"] is None
    assert attempt["cleanup_status"] == "cleaned"
    assert attempt["canonical_fixture_unchanged"] is True
    record = AttemptRecord.from_mapping(attempt)
    assert record.attempt_id == f"{TASK_ID}:base"
    assert record.mode == "imported"
    assert record.role == "preference-fixture"
    assert record.provider_attempts == 0


def test_bound_response_text_utf8_boundaries():
    """Marker inside the exact cap; no code-point split; no replacement
    expansion; exact output length never exceeds the cap."""
    from agentic_debugger.comparison.schema import MAX_RESPONSE_TEXT_BYTES

    cap = 64 * 1024
    for char in ("\u00e9", "\u20ac", "\U0001f600"):  # 2, 3, 4-byte chars
        text = char * (cap + 500)
        bounded = bound_response_text(text, cap=cap)
        encoded = bounded.encode("utf-8")
        assert len(encoded) <= cap
        assert bounded.endswith("[response-truncated]\n")
        assert "\ufffd" not in bounded  # no replacement characters
        bounded.encode("utf-8").decode("utf-8")  # always decodable (no split)
    # Exact-boundary case: text exactly at cap is untouched.
    text = "a" * cap
    assert bound_response_text(text, cap=cap) == text
