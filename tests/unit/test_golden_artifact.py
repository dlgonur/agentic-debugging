from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import (
    ControllerSnapshot,
    ModelScriptExhaustedError,
    ScriptedModelAdapter,
    ScriptedModelStep,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry
from agentic_debugger.events.golden import GoldenArtifact, GoldenArtifactError
from agentic_debugger.events.replay import replay_events
from agentic_debugger.events.schema import EventType, Metadata, RunEvent


def snapshot(index: int = 0, state: ControllerState = ControllerState.REPRODUCE) -> ControllerSnapshot:
    limits = ControllerBudgetLimits(2, 5, 2)
    return ControllerSnapshot(
        "run", "task", state, index, limits, ControllerBudgetState(), HypothesisLedger()
    )


def test_scripted_model_sequence_exhaustion_is_typed() -> None:
    step = ScriptedModelStep(
        ControllerState.REPRODUCE,
        TransitionDirective(ControllerState.UNDERSTAND, "failure reproduced"),
    )
    adapter = ScriptedModelAdapter((step,))
    assert adapter.next_directive(snapshot()).target_state is ControllerState.UNDERSTAND
    with pytest.raises(ModelScriptExhaustedError):
        adapter.next_directive(snapshot(1))


def test_controller_detects_unused_scripted_outputs_without_global_cursor() -> None:
    steps = (
        ScriptedModelStep(
            ControllerState.REPRODUCE,
            TransitionDirective(ControllerState.UNDERSTAND, "failure reproduced"),
        ),
        ScriptedModelStep(
            ControllerState.UNDERSTAND,
            TransitionDirective(ControllerState.PATCH, "patch"),
        ),
        ScriptedModelStep(
            ControllerState.PATCH,
            TransitionDirective(ControllerState.VALIDATE, "validate"),
        ),
        ScriptedModelStep(
            ControllerState.VALIDATE,
            TransitionDirective(ControllerState.DONE, "complete"),
        ),
        ScriptedModelStep(
            ControllerState.DONE,
            TransitionDirective(ControllerState.DONE, "unused"),
        ),
    )
    result = DeterministicController(
        ToolRegistry(),
        ScriptedModelAdapter(steps),
        ControllerRunConfig(max_model_calls=10),
    ).run(snapshot())
    assert result.stop_reason.value == "done"
    assert result.model_calls == 4
    assert result.model_calls < len(steps)
    assert DeterministicController(
        ToolRegistry(),
        ScriptedModelAdapter(steps),
        ControllerRunConfig(max_model_calls=10),
    ).run(snapshot()).model_calls == result.model_calls


def test_empty_trajectory_and_non_json_golden_are_rejected() -> None:
    with pytest.raises(Exception, match="empty"):
        replay_events([])
    with pytest.raises(GoldenArtifactError, match="JSON-compatible"):
        GoldenArtifact.from_mapping({
            "schema_version": "1.0",
            "trajectory_name": "bad",
            "task_id": "task",
            "policy_name": "static",
            "fixed_scripted_model_sequence": [{}],
            "expected_semantic_events": [{"bad": object()}],
            "expected_controller_terminal_state": "Done",
            "expected_patch_assertion": {"executed": True, "target_file": "module.py", "patch_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "valid_unified_diff": True, "semantic_change": "changed"},
            "expected_evaluator_status": "COMPLETED",
            "expected_evaluator_outcome": "RESOLVED",
            "expected_f2p_totals": {"passed": 1, "total": 1},
            "expected_p2p_totals": {"passed": 1, "total": 1},
            "expected_pdb_usage": {"actions": 0, "observations": 0},
            "expected_model_call_count": 1,
        })


def test_golden_payload_round_trip_is_detached() -> None:
    artifact = GoldenArtifact.from_file(
        Path(__file__).parents[1] / "golden_trajectories" / "data" / "static-successful-repair.json"
    )
    mapping = artifact.to_mapping()
    mapping["expected_semantic_events"][0]["sequence"] = 7
    assert artifact.data["expected_semantic_events"][0]["sequence"] == 0


def test_direct_golden_constructor_cannot_bypass_validation() -> None:
    with pytest.raises(GoldenArtifactError):
        GoldenArtifact({"schema_version": "9.0"})


def test_golden_artifact_rejects_unknown_script_fields_and_model_count_mismatch() -> None:
    from pathlib import Path
    valid = json.loads((Path(__file__).parents[1] / "golden_trajectories" / "data" / "static-successful-repair.json").read_text(encoding="utf-8"))
    valid["fixed_scripted_model_sequence"][0]["unknown"] = True
    with pytest.raises(GoldenArtifactError, match="unknown or missing fields"):
        GoldenArtifact.from_mapping(valid)
    valid = json.loads((Path(__file__).parents[1] / "golden_trajectories" / "data" / "static-successful-repair.json").read_text(encoding="utf-8"))
    valid["expected_model_call_count"] += 1
    with pytest.raises(GoldenArtifactError, match="equal scripted sequence length"):
        GoldenArtifact.from_mapping(valid)


def test_golden_artifact_accessors_are_detached() -> None:
    from pathlib import Path
    artifact = GoldenArtifact.from_file(Path(__file__).parents[1] / "golden_trajectories" / "data" / "static-successful-repair.json")
    first = artifact.data
    first["metadata"]["model_backend"] = "real"
    first["expected_semantic_events"][0]["payload"]["new"] = True
    second = artifact.to_mapping()
    assert second["metadata"]["model_backend"] == "scripted"
    assert "new" not in second["expected_semantic_events"][0]["payload"]


def _static_artifact_mapping() -> dict:
    return json.loads(
        (Path(__file__).parents[1] / "golden_trajectories" / "data" / "static-successful-repair.json").read_text(
            encoding="utf-8"
        )
    )


@pytest.mark.parametrize("mutation", ["task", "terminal", "final_payload", "pdb_count", "patch_action", "patch_applied", "patch_hash"])
def test_golden_artifact_rejects_cross_field_contradictions(mutation: str) -> None:
    mapping = _static_artifact_mapping()
    if mutation == "task":
        mapping["expected_semantic_events"][0]["task_id"] = "other-task"
    elif mutation == "terminal":
        mapping["expected_controller_terminal_state"] = "Failed"
    elif mutation == "final_payload":
        mapping["expected_semantic_events"][-1]["payload"]["final_state"] = "Failed"
    elif mutation == "pdb_count":
        mapping["expected_pdb_usage"]["actions"] = 1
    elif mutation == "patch_action":
        mapping["expected_semantic_events"] = [
            event for event in mapping["expected_semantic_events"]
            if not (event["event_type"] == "action" and event["name"] == "apply_patch")
        ]
    elif mutation == "patch_applied":
        next(
            event for event in mapping["expected_semantic_events"]
            if event["event_type"] == "observation" and event["name"] == "apply_patch"
        )["payload"]["observation"]["payload"]["applied"] = False
    else:
        next(
            event for event in mapping["expected_semantic_events"]
            if event["event_type"] == "observation" and event["name"] == "apply_patch"
        )["payload"]["observation"]["payload"]["patch_sha256"] = "0" * 64
    with pytest.raises(GoldenArtifactError):
        GoldenArtifact.from_mapping(mapping)


def test_rejection_artifact_cannot_claim_patch_execution() -> None:
    mapping = json.loads(
        (Path(__file__).parents[1] / "golden_trajectories" / "data" / "deterministic-rejection.json").read_text(
            encoding="utf-8"
        )
    )
    mapping["expected_patch_assertion"] = {
        "executed": True,
        "target_file": "display_name.py",
        "patch_sha256": "0" * 64,
        "valid_unified_diff": True,
        "semantic_change": "changed",
    }
    with pytest.raises(GoldenArtifactError):
        GoldenArtifact.from_mapping(mapping)
