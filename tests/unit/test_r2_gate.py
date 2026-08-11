"""Focused R2 gate tests — staged chain A-F with trajectory correlation."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.events.schema import Observation
from experiments.debugger_interaction_v2_r2 import bridge
from experiments.debugger_interaction_v2_r2.r2_runner import _compute_gate_r2


def _render_hash(obs: dict) -> str:
    return hashlib.sha256(
        bridge._render_observation(Observation.from_mapping(obs)).encode("utf-8")
    ).hexdigest()


def _obs(name: str, oid: str, aid: str, status: str, payload: dict):
    return {
        "observation_id": oid, "action_id": aid, "run_id": "r", "task_id": "t",
        "name": name, "status": status, "payload": payload,
        "summary": "x", "truncated": False,
    }


def _action(name: str, aid: str, args: dict):
    return {
        "action_id": aid, "run_id": "r", "task_id": "t",
        "name": name, "state": "RuntimeEvidence", "arguments": args,
    }


def _event(action, observation):
    return [
        {"event_type": "action", "name": observation["name"] if observation else action["name"], "payload": {"action": action}},
        {"event_type": "observation", "name": observation["name"], "payload": {"observation": observation}},
    ]


def _full_trajectory_and_telemetry(
    *,
    g1: int = 1,
    g2: int = 2,
    break_state: str = "paused",
    step_state: str = "paused",
    include_inspection: str = "locals",  # locals|print|none
    break_status: str = "ok",
    step_status: str = "ok",
    binding: bool = True,
):
    """Build a minimal full trajectory + telemetry for gate testing."""
    events = []
    # A: break
    a_action = _action("start_pdb_session", "act-1", {"breakpoint_line": 2})
    a_obs_payload = {"dispatch_reason": "ok", "state": break_state, "script": "recent_window.py", "line": 2, "function": "recent_window", "breakpoint_line": 2} if break_status == "ok" else {"dispatch_reason": "tool_error", "diagnostic": "oops"}
    a_obs = _obs("start_pdb_session", "obs-1", "act-1", break_status, a_obs_payload)
    if break_state != "paused":
        a_obs["payload"] = {"dispatch_reason": "ok", "state": break_state, "script": "recent_window.py", "exit_code": 0}
    events.extend(_event(a_action, a_obs))
    # B: stack G1
    b_action = _action("get_stack_summary", "act-2", {})
    b_obs = _obs("get_stack_summary", "obs-2", "act-2", "ok", {
        "state": "paused", "script": "recent_window.py", "pause_generation": g1,
        "frames": [{"frame_id": 0, "script": "recent_window.py", "line": 2, "function": "recent_window", "is_current": True}],
        "total_frames": 1, "truncated": False,
    })
    events.extend(_event(b_action, b_obs))
    # C: inspection
    if include_inspection != "none":
        name = "get_frame_locals" if include_inspection == "locals" else "safe_eval_expression"
        c_action = _action(name, "act-3", {"frame_id": 0, "pause_generation": g1, "expression": "x"} if name == "safe_eval_expression" else {"frame_id": 0, "pause_generation": g1})
        c_payload = {"state": "paused", "pause_generation": g1, "frame_id": 0, "locals": [], "total_count": 0, "truncated": False} if name == "get_frame_locals" else {"state": "paused", "pause_generation": g1, "frame": {"frame_id": 0}, "expression": "values", "value": {"kind": "list", "type": "builtins.list", "size": 5}}
        c_obs = _obs(name, "obs-3", "act-3", "ok", c_payload)
        events.extend(_event(c_action, c_obs))
    # D: step/next
    d_action = _action("step_pdb_session", "act-4", {})
    if step_status == "ok" and step_state == "paused":
        d_payload = {"state": "paused", "script": "recent_window.py", "line": 3, "function": "recent_window"}
    elif step_status == "ok" and step_state == "exited":
        d_payload = {"state": "exited", "script": "recent_window.py", "exit_code": 0}
    else:
        d_payload = {"dispatch_reason": "tool_error", "diagnostic": "nope"}
    d_obs = _obs("step_pdb_session", "obs-4", "act-4", step_status, d_payload)
    events.extend(_event(d_action, d_obs))
    # E: post-step stack G2
    e_action = _action("get_stack_summary", "act-5", {})
    e_obs = _obs("get_stack_summary", "obs-5", "act-5", "ok", {
        "state": "paused", "script": "recent_window.py", "pause_generation": g2,
        "frames": [{"frame_id": 0, "script": "recent_window.py", "line": 3, "function": "recent_window", "is_current": True}],
        "total_frames": 1, "truncated": False,
    })
    events.extend(_event(e_action, e_obs))
    # Telemetry — one record per action + one diagnosis
    trajectory = "\n".join(json.dumps(ev) for ev in events)

    # Build telemetry that binds each observation
    telemetry = []
    # We need to simulate telemetry entries that correspond to the model calls.
    # For gate binding, the NEXT record's prior_observation_id must equal obs id.
    # Simplest: produce telemetry where each step's next record binds previous obs.
    obs_ids = ["obs-1", "obs-2", "obs-3", "obs-5", "obs-4"]  # placeholder
    # Actually gate searches telemetry for ANY record whose prior matches obs.
    # So we add a telemetry record for each observation's binding.
    # For simplicity, add binding records for A,B,C,D,E.

    def _telemetry_with_binding(obs_dict, next_prior_id, next_hash):
        return {
            "parse_result": {"status": "accepted"},
            "translated_directive": {"action_name": obs_dict["payload"].get("state", "x")},
            "provenance": {"prior_observation_id": next_prior_id, "rendered_observation_sha256": next_hash},
        }

    # Build real telemetry: accepted actions + diagnosis
    tele = [
        {"parse_result": {"status": "accepted"}, "translated_directive": {"action_name": "start_pdb_session"}, "provenance": {}},
        {"parse_result": {"status": "accepted"}, "translated_directive": {"action_name": "get_stack_summary"}, "provenance": {"prior_observation_id": "obs-1", "rendered_observation_sha256": _render_hash(a_obs)}},
        {"parse_result": {"status": "accepted"}, "translated_directive": {"action_name": "get_frame_locals" if include_inspection == "locals" else "safe_eval_expression" if include_inspection == "print" else "step_pdb_session"}, "provenance": {"prior_observation_id": "obs-2", "rendered_observation_sha256": _render_hash(b_obs)}},
    ]
    if include_inspection != "none":
        d_prior = "obs-3"
        c_hash = _render_hash([e["payload"]["observation"] for e in events if e["payload"].get("observation", {}).get("observation_id") == "obs-3"][0]) if include_inspection != "none" else _render_hash(b_obs)
        tele.append({"parse_result": {"status": "accepted"}, "translated_directive": {"action_name": "step_pdb_session"}, "provenance": {"prior_observation_id": d_prior, "rendered_observation_sha256": c_hash}})
        e_prior_tele = {"parse_result": {"status": "accepted"}, "translated_directive": {"action_name": "get_stack_summary"}, "provenance": {"prior_observation_id": "obs-4", "rendered_observation_sha256": _render_hash(d_obs)}}
        tele.append(e_prior_tele)
        e_hash = _render_hash(e_obs)
        tele.append({"parse_result": {"status": "accepted"}, "translated_directive": {"action_name": "get_stack_summary"}, "provenance": {"prior_observation_id": "obs-4", "rendered_observation_sha256": _render_hash(d_obs)}})
        diag_tele = {"parse_result": {"status": "accepted"}, "translated_directive": {"is_diagnosis": True, "diagnosis_text": "the off-by-one truncates the window"}, "provenance": {"prior_observation_id": "obs-5", "rendered_observation_sha256": _render_hash(e_obs)}}
        tele.append(diag_tele)
        # Extra dummy binding for obs-5 so E's provenance check passes even without diagnosis
        # (the gate requires obs-5 be bound; diagnosis provides it but we also need it for F-not-exists case)
        # Add a second binding for obs-5 as a non-diagnosis record so removing diagnosis doesn't break E.
        tele.append({"parse_result": {"status": "accepted"}, "translated_directive": {"action_name": "failed", "is_diagnosis": False}, "provenance": {"prior_observation_id": "obs-5", "rendered_observation_sha256": _render_hash(e_obs)}})
    else:
        # no inspection — chain is broken, still add a diagnosis but it won't be after E properly
        e_hash = _render_hash(e_obs)
        diag_tele = {"parse_result": {"status": "accepted"}, "translated_directive": {"is_diagnosis": True, "diagnosis_text": "oops"}, "provenance": {"prior_observation_id": "obs-5", "rendered_observation_sha256": e_hash}}
        tele.append(diag_tele)

    if not binding:
        # break provenance by corrupting the diagnosis prior (find the diagnosis record)
        for t in tele:
            if t.get("translated_directive", {}).get("is_diagnosis"):
                t["provenance"]["prior_observation_id"] = "wrong-id"
                break

    return trajectory, tele


def test_full_chain_passes():
    traj, tele = _full_trajectory_and_telemetry()
    result = _compute_gate_r2(tele, traj, expected_script="recent_window.py")
    assert result["passed"] is True, result


def test_continue_exited_does_not_satisfy_step():
    traj, tele = _full_trajectory_and_telemetry(step_state="exited", step_status="ok")
    result = _compute_gate_r2(tele, traj, expected_script="recent_window.py")
    assert result["passed"] is False
    assert "exited" in result["reason"] or "step" in result["reason"].lower()


def test_inspection_error_does_not_advance():
    # Build trajectory where C is an error observation
    events = []
    a_action = _action("start_pdb_session", "act-1", {"breakpoint_line": 2})
    a_obs = _obs("start_pdb_session", "obs-1", "act-1", "ok", {"dispatch_reason": "ok", "state": "paused", "script": "recent_window.py", "line": 2, "function": "recent_window", "breakpoint_line": 2})
    events.extend(_event(a_action, a_obs))
    b_action = _action("get_stack_summary", "act-2", {})
    b_obs = _obs("get_stack_summary", "obs-2", "act-2", "ok", {"state": "paused", "script": "recent_window.py", "pause_generation": 1, "frames": [{"frame_id": 0, "script": "recent_window.py", "line": 2, "function": "recent_window", "is_current": True}], "total_frames": 1, "truncated": False})
    events.extend(_event(b_action, b_obs))
    # C as error
    c_action = _action("get_frame_locals", "act-3", {"frame_id": 0, "pause_generation": 1})
    c_obs = _obs("get_frame_locals", "obs-3", "act-3", "error", {"dispatch_reason": "tool_error", "diagnostic": "oops"})
    events.extend(_event(c_action, c_obs))
    trajectory = "\n".join(json.dumps(ev) for ev in events)
    telemetry = [
        {"parse_result": {"status": "accepted"}, "translated_directive": {"action_name": "start_pdb_session"}, "provenance": {}},
        {"parse_result": {"status": "accepted"}, "translated_directive": {"action_name": "get_stack_summary"}, "provenance": {"prior_observation_id": "obs-1", "rendered_observation_sha256": _render_hash(a_obs)}},
        {"parse_result": {"status": "accepted"}, "translated_directive": {"action_name": "get_frame_locals"}, "provenance": {"prior_observation_id": "obs-2", "rendered_observation_sha256": _render_hash(b_obs)}},
    ]
    result = _compute_gate_r2(telemetry, trajectory, expected_script="recent_window.py")
    assert result["passed"] is False


def test_g2_must_be_greater_than_g1():
    traj, tele = _full_trajectory_and_telemetry(g1=2, g2=1)
    result = _compute_gate_r2(tele, traj, expected_script="recent_window.py")
    assert result["passed"] is False
    assert "G1" in result["reason"] or "G2" in result["reason"]


def test_accepted_tokens_without_ok_observations_fail():
    # All telemetry says accepted but trajectory has errors
    events = []
    for i, name in enumerate(["start_pdb_session", "get_stack_summary"], 1):
        act = _action(name, f"act-{i}", {})
        obs = _obs(name, f"obs-{i}", f"act-{i}", "error", {"dispatch_reason": "tool_error", "diagnostic": "fail"})
        events.extend(_event(act, obs))
    trajectory = "\n".join(json.dumps(ev) for ev in events)
    telemetry = [
        {"parse_result": {"status": "accepted"}, "translated_directive": {"action_name": "start_pdb_session"}, "provenance": {}},
        {"parse_result": {"status": "accepted"}, "translated_directive": {"action_name": "get_stack_summary"}, "provenance": {"prior_observation_id": "obs-1", "rendered_observation_sha256": "deadbeef"}},
    ]
    result = _compute_gate_r2(telemetry, trajectory, expected_script="recent_window.py")
    assert result["passed"] is False


def test_wrong_provenance_fails():
    traj, tele = _full_trajectory_and_telemetry(binding=False)
    result = _compute_gate_r2(tele, traj, expected_script="recent_window.py")
    assert result["passed"] is False
    assert "provenance" in result["reason"].lower() or "bind" in result["reason"].lower()


def test_no_diagnosis_fails():
    traj, tele = _full_trajectory_and_telemetry()
    # Remove diagnosis
    tele = [t for t in tele if not t.get("translated_directive", {}).get("is_diagnosis")]
    result = _compute_gate_r2(tele, traj, expected_script="recent_window.py")
    assert result["passed"] is False
    assert "diagnosis" in result["reason"].lower()


def test_print_expression_counts_same_as_locals():
    traj, tele = _full_trajectory_and_telemetry(include_inspection="print")
    result = _compute_gate_r2(tele, traj, expected_script="recent_window.py")
    assert result["passed"] is True, result


def test_single_char_safe_eval_is_sufficient():
    # Gate should not enforce length heuristic — single-char variable is meaningful
    # We build with safe_eval having expression "x" — the trajectory encodes it.
    traj, tele = _full_trajectory_and_telemetry(include_inspection="print")
    result = _compute_gate_r2(tele, traj, expected_script="recent_window.py")
    # If gate had an expression-length check, this would fail — verify it passes
    assert result["passed"] is True, result
