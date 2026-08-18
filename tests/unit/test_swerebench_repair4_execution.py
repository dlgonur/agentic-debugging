from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_debugger.swerebench.authority import frozen_dir, repository_root
from scripts.gpt_oss_swerebench_v2_pilot10 import authorization_evidence_path, main


def test_repair4_clean_authorize_second_gate_and_execution_preparation(tmp_path: Path):
    evidence_path = tmp_path / "repair4-zero-provider.json"
    assert main(["smoke", "--output-path", str(evidence_path)]) == 0
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert evidence["git_clean_before_authorize"] is True
    assert evidence["git_clean_after_authorize"] is True
    assert evidence["authorization_evidence_outside_repository"] is True
    assert evidence["external_root_target_state_after_authorize"] == "nonexistent_target"
    assert evidence["second_authorization_ready"] is True
    assert evidence["external_root_created_exactly_once"] is True
    assert evidence["first_task_mapping_instantiated"] is True
    assert evidence["docker_runtime_selected"] is True
    assert evidence["official_verifier_selected"] is True
    assert evidence["hidden_gold_data_model_visible"] is False
    assert evidence["frozen_inputs_unchanged"] is True
    assert evidence["provider_inference_started"] is False
    assert evidence["generation_calls"] == 0
    assert evidence["api_chat_calls"] == 0
    assert evidence["profile_metadata"]["adapter_provenance_valid"] is True


def test_authorization_evidence_cannot_be_written_inside_repository(tmp_path: Path):
    with pytest.raises(ValueError, match="outside the repository"):
        authorization_evidence_path(
            tmp_path,
            repository_root() / "experiments" / "authorization.json",
        )


def test_tracked_summary_is_option_b_and_selection_hashes_are_unchanged():
    summary = json.loads(
        (frozen_dir() / "preflight" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["n"] == 10
    assert summary["ready"] == 10
    assert summary["invalid"] == []
    assert len(summary["records"]) == 10
    assert all(
        item["authorization_status"] == "ready-for-authorized-execution"
        and item["model_facing_isolated"] is True
        and item["model_side_runtime_ready"] is True
        and item["verifier_environment_ready"] is True
        and item["verifier_baseline_valid"] is True
        and item["pdb_classification"] == "PDB_DEFERRED_TO_SEPARATE_TREATMENT"
        for item in summary["records"]
    )
    assert all(
        item["classification"] == "PDB_DEFERRED_TO_SEPARATE_TREATMENT"
        for item in summary["pdb_classifications"]
    )
    assert "GATE_OPENS_AFTER_PUBLIC_FAILURE_THEN_MODEL_SELECTED_ENTRY" not in (
        json.dumps(summary)
    )
    hashes = json.loads(
        (frozen_dir() / "artifact_hashes.json").read_text(encoding="utf-8")
    )
    assert hashes["population.json"] == (
        "36bd31d1470b86db982235153372793455a850ae1fe9c1669bdf8c0e7e68ab8f"
    )
    assert hashes["full_ordering.json"] == (
        "599a07b6a527b4f8dffda4120be8e3c524ad608929bb048ea98286f80e0f5061"
    )
    assert hashes["pilot10_manifest.json"] == (
        "4b9b17f8f897e56263f0394e35c06261bc613097f38a1b2e157d4d9a215a963f"
    )
