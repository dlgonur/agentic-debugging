"""Focused R1 gate tests: successful runtime evidence must be real and correlated."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.events.schema import Observation
from experiments.debugger_interaction_v2_r1 import bridge
from experiments.debugger_interaction_v2_r1.r1_runner import (
    _compute_gate_c,
    _compute_gate_r1,
)


def _trajectory(*, status="ok", state="paused", line=9, function="recent_window", script="recent_window.py"):
    action = {
        "action_id": "action-1", "run_id": "r", "task_id": "t",
        "name": "start_pdb_session", "state": "RuntimeEvidence",
        "arguments": {"breakpoint_line": 9},
    }
    observation = {
        "observation_id": "observation-1", "action_id": "action-1",
        "run_id": "r", "task_id": "t", "name": "start_pdb_session",
        "status": status,
        "payload": {
            "dispatch_reason": "ok" if status == "ok" else "tool_error",
            "state": state, "script": script, "line": line,
            "function": function,
        },
        "summary": "runtime probe paused" if state == "paused" else "Tool execution failed.",
        "truncated": False,
    }
    events = [
        {"event_type": "action", "name": "start_pdb_session", "payload": {"action": action}},
        {"event_type": "observation", "name": "start_pdb_session", "payload": {"observation": observation}},
    ]
    return "\n".join(json.dumps(event) for event in events), observation


def _telemetry(*, prior="observation-1", rendered=None):
    observation = {
        "observation_id": "observation-1", "action_id": "action-1",
        "run_id": "r", "task_id": "t", "name": "start_pdb_session",
        "status": "ok", "payload": {
            "dispatch_reason": "ok", "state": "paused",
            "script": "recent_window.py", "line": 9,
            "function": "recent_window",
        }, "summary": "runtime probe paused", "truncated": False,
    }
    if rendered is None:
        rendered = hashlib.sha256(
            bridge._render_observation(Observation.from_mapping(observation)).encode("utf-8")
        ).hexdigest()
    return [
        {
            "model_call_index": 1,
            "parse_result": {"status": "accepted"},
            "translated_directive": {
                "action_name": "start_pdb_session", "arguments": {"breakpoint_line": 9},
            },
            "provenance": {},
        },
        {
            "model_call_index": 2,
            "parse_result": {"status": "accepted"},
            "translated_directive": {"action_name": "stop_pdb_session"},
            "provenance": {
                "prior_observation_id": prior,
                "rendered_observation_sha256": rendered,
            },
        },
    ]


def test_accepted_break_ok_paused_production_observation_passes():
    trajectory, _ = _trajectory()
    result = _compute_gate_r1(_telemetry(), trajectory, expected_script="recent_window.py")
    assert result["passed"] is True
    assert result["observation_id"] == "observation-1"


def test_accepted_break_tool_error_fails():
    trajectory, _ = _trajectory(status="error", state=None, line=None, function=None)
    result = _compute_gate_r1(_telemetry(), trajectory, expected_script="recent_window.py")
    assert result["passed"] is False
    assert "not OK" in result["reason"]


def test_accepted_break_exited_not_paused_fails():
    trajectory, _ = _trajectory(state="exited", line=None, function=None)
    result = _compute_gate_r1(_telemetry(), trajectory, expected_script="recent_window.py")
    assert result["passed"] is False
    assert "not paused" in result["reason"]


def test_successful_pause_wrong_next_provenance_fails():
    trajectory, _ = _trajectory()
    result = _compute_gate_r1(_telemetry(prior="wrong-observation"), trajectory, expected_script="recent_window.py")
    assert result["passed"] is False
    assert "does not bind" in result["reason"]


def test_successful_pause_missing_rendered_hash_fails():
    trajectory, _ = _trajectory()
    telemetry = _telemetry()
    telemetry[1]["provenance"]["rendered_observation_sha256"] = None
    result = _compute_gate_r1(telemetry, trajectory, expected_script="recent_window.py")
    assert result["passed"] is False
    assert "rendered observation" in result["reason"]


def test_gate_c_does_not_count_accepted_error_as_pdb_evidence():
    telemetry = [{
        "parse_result": {"status": "accepted"},
        "translated_directive": {"action_name": "start_pdb_session"},
    }]
    result = _compute_gate_c(telemetry, {"executed": False}, gate_r1={"passed": False})
    assert result["has_pdb_evidence"] is False
    assert result["passed"] is False
