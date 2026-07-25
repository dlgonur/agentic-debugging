from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_debugger.events.golden import GoldenArtifact, GoldenArtifactError, load_golden_artifact
from agentic_debugger.events.replay import compare_trajectories

from .support import candidate_patch, patch_sha256, pdb_steps, rejected_steps, run_trajectory, scripted_sequence_mapping, static_steps


DATA = Path(__file__).parent / "data"


@pytest.mark.parametrize(
    ("artifact_name", "policy"),
    [
        ("static-successful-repair.json", "static"),
        ("pdb-gated-successful-repair.json", "pdb"),
        ("deterministic-rejection.json", "rejected"),
    ],
)
def test_committed_artifact_matches_actual_trajectory(tmp_path: Path, artifact_name: str, policy: str) -> None:
    artifact = load_golden_artifact(DATA / artifact_name)
    run = run_trajectory(artifact.data["task_id"], policy, None, tmp_path / policy)
    comparison = compare_trajectories(artifact.data["expected_semantic_events"], run.semantic_events)
    assert comparison.equal, comparison.mismatch
    factories = {"static": static_steps, "pdb": pdb_steps, "rejected": lambda task, patch: rejected_steps()}
    assert artifact.data["expected_controller_terminal_state"] == run.controller.final_state.value
    assert artifact.data["fixed_scripted_model_sequence"] == scripted_sequence_mapping(factories[policy](run.task, run.patch))
    assert artifact.data["expected_model_call_count"] == run.controller.model_calls
    assert artifact.data["expected_pdb_usage"] == {
        "actions": run.pdb_action_count,
        "observations": run.pdb_observation_count,
    }
    patch_assertion = artifact.data["expected_patch_assertion"]
    if policy == "rejected":
        assert patch_assertion == {"executed": False, "reason": "action_disallowed_in_current_state"}
        assert artifact.data["expected_evaluator_status"] == "not_run"
    else:
        assert run.evaluator is not None
        assert artifact.data["expected_evaluator_status"] == run.evaluator.status.value
        assert artifact.data["expected_evaluator_outcome"] == run.evaluator.outcome.value
        assert patch_assertion["target_file"] == run.task.constraints.allowed_write_paths[0]
        assert patch_assertion["patch_sha256"] == patch_sha256(run.patch)
        assert patch_assertion["valid_unified_diff"] is True
        assert patch_assertion["semantic_change"] in run.patch
        assert artifact.data["expected_f2p_totals"] == {"passed": run.evaluator.f2p_passed, "total": run.evaluator.f2p_total}
        assert artifact.data["expected_p2p_totals"] == {"passed": run.evaluator.p2p_passed, "total": run.evaluator.p2p_total}


def test_corrupted_golden_artifact_is_rejected() -> None:
    valid = json.loads((DATA / "static-successful-repair.json").read_text(encoding="utf-8"))
    valid["schema_version"] = "9.0"
    with pytest.raises(GoldenArtifactError, match="unsupported golden schema"):
        GoldenArtifact.from_mapping(valid)
    valid = json.loads((DATA / "static-successful-repair.json").read_text(encoding="utf-8"))
    valid["expected_semantic_events"] = [float("nan")]
    with pytest.raises(GoldenArtifactError, match="JSON-compatible"):
        GoldenArtifact.from_mapping(valid)
