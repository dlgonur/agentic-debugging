"""Unit tests for the R2 stage tracker — idempotent, fail-closed, only ok advances."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.events.schema import Observation, ObservationStatus
from experiments.debugger_interaction_v2_r2.adapter import R2StageTracker
from experiments.debugger_interaction_v2_r2.bridge import R2Stage


def _obs(obs_id: str, name: str, status: str = "ok", payload_extra: dict | None = None):
    payload = {"state": "paused", "script": "recent_window.py", "line": 2, "function": "recent_window"}
    if payload_extra:
        payload.update(payload_extra)
    obs_status = ObservationStatus.OK if status == "ok" else ObservationStatus.ERROR
    if status != "ok":
        payload = {"dispatch_reason": "tool_error", "diagnostic": "oops"}
    return Observation(
        observation_id=obs_id, action_id=f"act-{obs_id}", run_id="r", task_id="t",
        name=name, status=obs_status, payload=payload, summary="x", truncated=False,
    )


class TestR2StageTracker:
    def test_initial_is_not_started(self):
        tracker = R2StageTracker()
        assert tracker.stage is R2Stage.NOT_STARTED

    def test_break_ok_advances_to_needs_stack(self):
        tracker = R2StageTracker()
        tracker.update_from_observation(_obs("o1", "start_pdb_session", "ok", {"state": "paused"}))
        assert tracker.stage is R2Stage.PAUSED_NEEDS_STACK

    def test_idempotent_same_observation_does_not_advance_twice(self):
        tracker = R2StageTracker()
        obs = _obs("o1", "start_pdb_session", "ok", {"state": "paused"})
        tracker.update_from_observation(obs)
        assert tracker.stage is R2Stage.PAUSED_NEEDS_STACK
        tracker.update_from_observation(obs)
        assert tracker.stage is R2Stage.PAUSED_NEEDS_STACK
        # Should not have advanced to NEEDS_INSPECTION on second call with same id
        # even if stack would have advanced

    def test_error_break_does_not_advance(self):
        tracker = R2StageTracker()
        tracker.update_from_observation(_obs("o1", "start_pdb_session", "error"))
        assert tracker.stage is R2Stage.NOT_STARTED

    def test_full_chain(self):
        tracker = R2StageTracker()
        tracker.update_from_observation(_obs("o1", "start_pdb_session", "ok", {"state": "paused"}))
        assert tracker.stage is R2Stage.PAUSED_NEEDS_STACK
        # stack
        tracker.update_from_observation(_obs("o2", "get_stack_summary", "ok", {
            "state": "paused", "pause_generation": 1,
            "frames": [{"frame_id": 0, "is_current": True}], "total_frames": 1, "truncated": False
        }))
        assert tracker.stage is R2Stage.PAUSED_NEEDS_INSPECTION
        # locals
        tracker.update_from_observation(_obs("o3", "get_frame_locals", "ok", {"state": "paused", "pause_generation": 1}))
        assert tracker.stage is R2Stage.PAUSED_NEEDS_STEP
        # step
        tracker.update_from_observation(_obs("o4", "step_pdb_session", "ok", {"state": "paused", "line": 3, "function": "recent_window", "script": "recent_window.py"}))
        assert tracker.stage is R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK
        # post-step stack
        tracker.update_from_observation(_obs("o5", "get_stack_summary", "ok", {
            "state": "paused", "pause_generation": 2,
            "frames": [{"frame_id": 0, "is_current": True}], "total_frames": 1, "truncated": False
        }))
        assert tracker.stage is R2Stage.READY_FOR_DIAGNOSIS

    def test_safe_eval_also_advances_from_inspection(self):
        tracker = R2StageTracker()
        tracker.update_from_observation(_obs("o1", "start_pdb_session", "ok", {"state": "paused"}))
        tracker.update_from_observation(_obs("o2", "get_stack_summary", "ok", {
            "state": "paused", "pause_generation": 1,
            "frames": [], "total_frames": 0, "truncated": False
        }))
        tracker.update_from_observation(_obs("o3", "safe_eval_expression", "ok", {"state": "paused", "pause_generation": 1}))
        assert tracker.stage is R2Stage.PAUSED_NEEDS_STEP

    def test_next_also_advances_from_needs_step(self):
        tracker = R2StageTracker()
        tracker.update_from_observation(_obs("o1", "start_pdb_session", "ok", {"state": "paused"}))
        tracker.update_from_observation(_obs("o2", "get_stack_summary", "ok", {
            "state": "paused", "pause_generation": 1,
            "frames": [], "total_frames": 0, "truncated": False
        }))
        tracker.update_from_observation(_obs("o3", "get_frame_locals", "ok", {"state": "paused", "pause_generation": 1}))
        tracker.update_from_observation(_obs("o4", "next_pdb_session", "ok", {"state": "paused", "line": 3, "function": "recent_window", "script": "recent_window.py"}))
        assert tracker.stage is R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK

    def test_step_exited_does_not_advance(self):
        tracker = R2StageTracker()
        tracker.update_from_observation(_obs("o1", "start_pdb_session", "ok", {"state": "paused"}))
        tracker.update_from_observation(_obs("o2", "get_stack_summary", "ok", {
            "state": "paused", "pause_generation": 1,
            "frames": [], "total_frames": 0, "truncated": False
        }))
        tracker.update_from_observation(_obs("o3", "get_frame_locals", "ok", {"state": "paused", "pause_generation": 1}))
        tracker.update_from_observation(_obs("o4", "step_pdb_session", "ok", {"state": "exited", "exit_code": 0, "script": "recent_window.py"}))
        # state is exited, not paused — no advance
        assert tracker.stage is R2Stage.PAUSED_NEEDS_STEP

    def test_wrong_order_does_not_advance(self):
        tracker = R2StageTracker()
        # Try to jump straight to step without locals
        tracker.update_from_observation(_obs("o1", "start_pdb_session", "ok", {"state": "paused"}))
        tracker.update_from_observation(_obs("o2", "get_stack_summary", "ok", {
            "state": "paused", "pause_generation": 1,
            "frames": [], "total_frames": 0, "truncated": False
        }))
        tracker.update_from_observation(_obs("o3", "step_pdb_session", "ok", {"state": "paused", "line": 3, "function": "recent_window", "script": "recent_window.py"}))
        assert tracker.stage is R2Stage.PAUSED_NEEDS_INSPECTION

    def test_none_is_noop(self):
        tracker = R2StageTracker()
        tracker.update_from_observation(None)
        assert tracker.stage is R2Stage.NOT_STARTED
