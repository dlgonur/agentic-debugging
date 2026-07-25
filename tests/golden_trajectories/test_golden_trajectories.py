from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agentic_debugger.agent.controller import ControllerStopReason
from agentic_debugger.agent.controller_policy import (
    ControllerState,
    HypothesisConfidence,
    HypothesisStatus,
    PdbGateContext,
    PdbPolicy,
    RootCauseHypothesis,
    decide_pdb_access,
)
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
from agentic_debugger.evaluation.runner import EvaluationStatus, LifecycleStatus
from agentic_debugger.events.replay import compare_trajectories
from agentic_debugger.events.schema import EventType

from .support import (
    CURATED,
    candidate_patch,
    patch_sha256,
    run_trajectory,
    static_steps,
)


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_static_successful_repair_uses_actual_controller_and_task7(tmp_path: Path) -> None:
    fixture = CURATED / "curated-none-handling-001"
    before = {path.relative_to(fixture).as_posix(): _file_hash(path) for path in fixture.rglob("*") if path.is_file()}
    run = run_trajectory("curated-none-handling-001", "static", None, tmp_path)
    after = {path.relative_to(fixture).as_posix(): _file_hash(path) for path in fixture.rglob("*") if path.is_file()}

    assert run.controller.final_state.value == "Done"
    assert run.controller.stop_reason is ControllerStopReason.DONE
    assert run.controller.model_calls == len(static_steps(run.task, run.patch))
    assert run.tool_calls.count("apply_patch") == 1
    assert not any(name.startswith("get_") or name.startswith("start_pdb") for name in run.tool_calls)
    assert run.evaluator is not None
    assert run.evaluator.status is EvaluationStatus.COMPLETED
    assert run.evaluator.outcome is SemanticOutcome.RESOLVED
    assert run.evaluator.baseline.valid
    assert run.evaluator.candidate_patch_attempt_count == 1
    assert run.evaluator.patch_application.success
    assert run.evaluator.patch_application.changed_files == ("display_name.py",)
    assert run.evaluator.syntax.passed
    assert run.evaluator.f2p_passed == run.evaluator.f2p_total
    assert run.evaluator.p2p_passed == run.evaluator.p2p_total
    assert run.evaluator.full_suite is not None
    assert run.evaluator.full_suite.status.value == "PASS"
    assert run.evaluator.workspace.lifecycle is LifecycleStatus.CLEANED
    assert run.evaluator.workspace.canonical_fixture_unchanged
    assert before == after
    assert run.patch.startswith("--- a/display_name.py\n")
    assert "\n+++ b/display_name.py\n" in run.patch
    assert patch_sha256(run.patch) == patch_sha256(candidate_patch("curated-none-handling-001"))
    assert "normalized_name = name.strip() if name is not None else \"\"" in run.patch
    assert run.trajectory.events[-1].event_type is EventType.FINAL


def test_pdb_gated_successful_repair_has_real_bounded_pdb_evidence(tmp_path: Path) -> None:
    hypothesis = RootCauseHypothesis(
        "runtime-hypothesis",
        "The optional value requires runtime inspection.",
        HypothesisConfidence.LOW,
        HypothesisStatus.ACTIVE,
        (),
        True,
        1,
    )
    decision = decide_pdb_access(
        PdbPolicy.ON_UNCERTAINTY,
        PdbGateContext(
            ControllerState.UNDERSTAND,
            True,
            8,
            0,
            hypothesis,
        ),
    )
    assert decision.allowed

    run = run_trajectory("curated-none-handling-001", "pdb", None, tmp_path)
    assert run.controller.final_state.value == "Done"
    assert run.controller.model_calls == 13
    assert run.pdb_action_count == 4
    assert run.pdb_observation_count == 2
    assert "start_pdb_session" in run.tool_calls
    assert "get_stack_summary" in run.tool_calls
    assert "get_frame_locals" in run.tool_calls
    assert "stop_pdb_session" in run.tool_calls
    assert run.evaluator is not None
    assert run.evaluator.status is EvaluationStatus.COMPLETED
    assert run.evaluator.outcome is SemanticOutcome.RESOLVED
    assert run.evaluator.workspace.lifecycle is LifecycleStatus.CLEANED
    assert run.evaluator.workspace.canonical_fixture_unchanged
    assert run.source_root is not None
    assert not run.source_root.exists()
    pdb_observations = [
        event for event in run.trajectory.events
        if event.event_type is EventType.OBSERVATION
        and event.payload.get("observation", {}).get("name") in {"get_stack_summary", "get_frame_locals"}
    ]
    assert len(pdb_observations) == 2
    assert all(event.payload["observation"]["payload"]["dispatch_reason"] == "ok" for event in pdb_observations)
    assert run.evaluator.f2p_passed == run.evaluator.f2p_total
    assert run.evaluator.p2p_passed == run.evaluator.p2p_total


def test_deterministic_rejection_has_bounded_stop_and_no_tool_side_effect(tmp_path: Path) -> None:
    run = run_trajectory("curated-none-handling-001", "rejected", None, tmp_path)
    assert run.controller.final_state.value == "Failed"
    assert run.controller.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
    assert run.controller.model_calls == 1
    assert run.tool_calls == ()
    assert run.evaluator is None
    final = run.trajectory.events[-1]
    assert final.event_type is EventType.FINAL
    assert final.payload["final_state"] == "Failed"
    assert final.payload["stop_reason"] == "directive_rejected"
    assert all(event.event_type is not EventType.OBSERVATION for event in run.trajectory.events)


def test_repeat_runs_are_semantically_equal_and_isolated(tmp_path: Path) -> None:
    first = run_trajectory("curated-none-handling-001", "static", None, tmp_path / "first")
    second = run_trajectory("curated-none-handling-001", "static", None, tmp_path / "second")
    comparison = compare_trajectories(first.semantic_events, second.semantic_events)
    assert comparison.equal
    assert first.evaluator is not None and second.evaluator is not None
    assert first.evaluator.semantic_mapping() == second.evaluator.semantic_mapping()
    failed = run_trajectory("curated-none-handling-001", "rejected", None, tmp_path / "failed")
    later = run_trajectory("curated-none-handling-001", "static", None, tmp_path / "later")
    assert failed.controller.final_state.value == "Failed"
    assert later.controller.final_state.value == "Done"
    assert later.evaluator is not None
    assert later.evaluator.status is EvaluationStatus.COMPLETED


def test_static_artifact_projection_is_json_compatible(tmp_path: Path) -> None:
    run = run_trajectory("curated-none-handling-001", "static", None, tmp_path)
    json.dumps(run.semantic_events, ensure_ascii=False, allow_nan=False)
    assert all(isinstance(event, dict) for event in run.semantic_events)
