import json
import shutil
from pathlib import Path

import pytest

from agentic_debugger.swerebench.execution import (
    SWERebenchExecutionError,
    create_external_execution_root,
    inspect_external_root_target,
)
from agentic_debugger.swerebench.provenance import frozen_harness_identity
from agentic_debugger.swerebench.preflight import run_zero_provider_authorization_preflight
from agentic_debugger.swerebench.authority import frozen_dir
from scripts.gpt_oss_swerebench_v2_pilot10 import main


def test_external_root_authorization_requires_a_nonexistent_target(tmp_path: Path):
    project = tmp_path / "repository"
    project.mkdir()
    target = tmp_path / "nested" / "campaign"

    initial = inspect_external_root_target(target, project_root=project)
    assert initial["authorized"] is True
    assert initial["state"] == "nonexistent_target"
    assert initial["parent_exists"] is False
    assert initial["parent_writable"] is True

    created = create_external_execution_root(target, project_root=project)
    assert created == target.resolve()
    assert created.is_dir()

    existing_empty = inspect_external_root_target(target, project_root=project)
    assert existing_empty["authorized"] is False
    assert existing_empty["state"] == "existing_empty"
    (target / "marker").write_text("owned", encoding="utf-8")
    existing_nonempty = inspect_external_root_target(target, project_root=project)
    assert existing_nonempty["authorized"] is False
    assert existing_nonempty["state"] == "existing_nonempty"
    with pytest.raises(SWERebenchExecutionError, match="already exists"):
        create_external_execution_root(target, project_root=project)


def test_external_root_rejects_repository_contained_and_special_targets(tmp_path: Path):
    project = tmp_path / "repository"
    project.mkdir()
    inside = inspect_external_root_target(project / "external", project_root=project)
    assert inside["authorized"] is False
    assert "inside" in inside["reason"]

    filesystem_root = inspect_external_root_target(
        Path(Path.cwd().anchor), project_root=project
    )
    assert filesystem_root["authorized"] is False
    assert "filesystem root" in filesystem_root["reason"]

    reserved = inspect_external_root_target(tmp_path / "NUL", project_root=project)
    assert reserved["authorized"] is False
    assert "reserved" in reserved["reason"]


def test_frozen_harness_identity_has_execution_time_head_policy():
    identity = frozen_harness_identity()
    assert "runtime_head" not in identity
    assert identity["runtime_head_policy"] == "record_at_execution"
    assert identity["working_tree_policy"] == "must_be_clean_at_execution"


def test_zero_provider_gate_rejects_manifest_and_harness_mismatch(tmp_path: Path):
    frozen_copy = tmp_path / "frozen"
    shutil.copytree(frozen_dir(), frozen_copy)
    manifest = json.loads((frozen_copy / "pilot10_manifest.json").read_text(encoding="utf-8"))
    manifest["selected_instance_ids"][0] = "tampered__instance-1"
    (frozen_copy / "pilot10_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    manifest_result = run_zero_provider_authorization_preflight(
        frozen=frozen_copy,
        config_root=tmp_path / "config",
        profile_id="ollama-cloud-gpt-oss-20b",
        external_root=tmp_path / "external-root",
        preflight_summary=tmp_path / "summary.json",
    )
    assert manifest_result["checks"]["frozen_selection_hashes"] is False
    assert manifest_result["provider_generation_calls"] == 0

    shutil.copytree(frozen_dir(), frozen_copy, dirs_exist_ok=True)
    contract = json.loads((frozen_copy / "execution_contract.json").read_text(encoding="utf-8"))
    contract["harness"]["harness_content_sha256"] = "0" * 64
    (frozen_copy / "execution_contract.json").write_text(
        json.dumps(contract), encoding="utf-8"
    )
    harness_result = run_zero_provider_authorization_preflight(
        frozen=frozen_copy,
        config_root=tmp_path / "config",
        profile_id="ollama-cloud-gpt-oss-20b",
        external_root=tmp_path / "external-root-2",
        preflight_summary=tmp_path / "summary.json",
    )
    assert harness_result["checks"]["harness_content"] is False
    assert harness_result["provider_generation_calls"] == 0


def test_real_top_level_zero_provider_smoke_stops_before_generation(tmp_path: Path):
    evidence_path = tmp_path / "zero-provider-smoke.json"
    assert main(["smoke", "--output-path", str(evidence_path)]) == 0
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["authorization_ready"] is True
    assert evidence["clean_disposable_repository"] is True
    assert evidence["runtime_head_differs_from_parent_baseline"] is True
    assert evidence["external_root_target_state_before_executor"] == "nonexistent_target"
    assert evidence["external_root_created_by_executor"] is True
    assert evidence["first_task_mapping_instantiated"] is True
    assert evidence["docker_runtime_selected"] is True
    assert evidence["official_verifier_selected"] is True
    assert evidence["hidden_gold_data_model_visible"] is False
    assert evidence["reached_provider_inference_boundary"] is True
    assert evidence["provider_inference_started"] is False
    assert evidence["generation_calls"] == 0
    assert evidence["api_chat_calls"] == 0
