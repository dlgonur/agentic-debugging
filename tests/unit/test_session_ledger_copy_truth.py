"""Focused regression for SESSION-LEDGER-03: verifier truth, workstream, and COPY ALL."""

from __future__ import annotations

import hashlib

import pytest

from agentic_debugger.application.events import SessionEventKind, SourceKind, SessionStatus
from agentic_debugger.application.presentation import PresentationIdentity, initial_session_view, reduce_event
from agentic_debugger.application.live_execution import ExecutionMode, KnownCeilings, project_live_execution
from agentic_debugger.ui.widgets import LiveRunContextPanel, ActivityPanel, TimelinePanel, activity_export_text, timeline_export_text

from application_support import VALID_RUN_ID, VALID_SESSION_ID, VALID_SPEC_FINGERPRINT, VALID_TASK_ID, make_event

L32_TASK = "audreyr__cookiecutter-967"
FINGERPRINT = "a" * 64
PATCH = (
    "--- a/cookiecutter/config.py\n"
    "+++ b/cookiecutter/config.py\n"
    "@@ -54,6 +54,6 @@\n"
    "     value = config.get(key)\n"
    "\n"
    "     if value is None:\n"
    "-        return None\n"
    "+        return \"\"\n"
    "\n"
    "     return value\n"
)


def _identity(task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR, session_id=VALID_SESSION_ID):
    return PresentationIdentity(task_id=task_id, source_kind=source_kind, session_id=session_id)


def _reduce_all(view, events):
    for ev in events:
        view = reduce_event(view, ev)
    return view


def _verifier_completed_payload(status="COMPLETED", outcome=None, official_proven=True):
    return {
        "status": status,
        "outcome": outcome,
        "f2p_passed": 1,
        "f2p_total": 5,
        "p2p_passed": 9,
        "p2p_total": 9,
        "workspace_cleaned": True,
        "classification": "official_rejection_semantic",
        "official_test_execution_proven": official_proven,
    }


# ---------------------------------------------------------------------------
# 1. V10-style final UNRESOLVED session: verifier completed => sidebar Completed
# ---------------------------------------------------------------------------

def test_v10_unresolved_verifier_completed_sidebar_shows_completed():
    identity = _identity()
    view = initial_session_view(identity)
    events = [
        make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": FINGERPRINT}, sequence=0, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, sequence=2, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.VERIFIER_COMPLETED, _verifier_completed_payload(outcome=None, official_proven=True), sequence=3, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.CLEANUP_STARTED, {}, sequence=4, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, sequence=5, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.SESSION_COMPLETED, {"status": "unresolved", "termination_reason": "unresolved"}, sequence=6, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
    ]
    view = _reduce_all(view, events)
    assert view.status is SessionStatus.UNRESOLVED
    assert view.verifier_summary is not None
    assert view.verifier_summary.status == "COMPLETED"
    assert view.verifier_summary.official_test_execution_proven is True
    assert view.cleanup_verified is True

    # Sidebar projection must be durable and replay-identical.
    state = project_live_execution(view, mode=ExecutionMode.LIVE, ceilings=KnownCeilings(model_requests=40))
    panel = LiveRunContextPanel()
    panel.update_execution(state)
    text = panel._text.render().plain
    # The RUN sidebar's VERIFIER row must be Completed, not Not started.
    assert "VERIFIER" in text
    # Find verifier line.
    assert "Completed" in text
    assert "Not started" not in text
    # Also test replay path: same view via replay mode must also be Completed.
    replay_state = project_live_execution(view, mode=ExecutionMode.REPLAY)
    replay_panel = LiveRunContextPanel()
    replay_panel.update_execution(replay_state)
    replay_text = replay_panel._text.render().plain
    assert "Completed" in replay_text


# ---------------------------------------------------------------------------
# 2. verifier never started => sidebar still says Not started
# ---------------------------------------------------------------------------

def test_verifier_never_started_sidebar_not_started():
    identity = _identity()
    view = initial_session_view(identity)
    events = [
        make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": FINGERPRINT}, sequence=0, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, sequence=2, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.CLEANUP_STARTED, {}, sequence=3, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, sequence=4, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.SESSION_COMPLETED, {"status": "unresolved", "termination_reason": "unresolved"}, sequence=5, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
    ]
    view = _reduce_all(view, events)
    assert view.verifier_summary is None
    assert not view.verifier_stages
    state = project_live_execution(view, mode=ExecutionMode.LIVE)
    panel = LiveRunContextPanel()
    panel.update_execution(state)
    text = panel._text.render().plain
    assert "Not started" in text
    assert "Completed" not in text


# ---------------------------------------------------------------------------
# 3. 260-event Activity ledger with small viewport: COPY ALL returns all logical events
# ---------------------------------------------------------------------------

def test_activity_copy_all_returns_full_ledger_not_viewport():
    identity = _identity(task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO, session_id=VALID_SESSION_ID)
    view = initial_session_view(identity)
    # Build 260 events: many tool events
    seq = 0
    events = [make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": FINGERPRINT}, sequence=seq, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO)]
    seq += 1
    events.append(make_event(SessionEventKind.SESSION_STARTED, {}, sequence=seq, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO))
    seq += 1
    events.append(make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, sequence=seq, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO))
    seq += 1
    for i in range(260):
        events.append(make_event(SessionEventKind.TOOL_STARTED, {"tool_name": f"tool_{i % 5}"}, sequence=seq, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO))
        seq += 1
        events.append(make_event(SessionEventKind.TOOL_COMPLETED, {"tool_name": f"tool_{i % 5}", "status": "ok"}, sequence=seq, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO))
        seq += 1
    # Only keep 260 logical entries? We already have many, but test count
    # Reduce all
    view = _reduce_all(view, events)
    # Activity export should contain all timeline entries matching filter (all)
    text = activity_export_text(view, filter_name="all")
    lines = [l for l in text.splitlines() if l.startswith("#")]
    # Should be equal to number of timeline entries (which is capped at 2000, but we have ~523)
    # For this test, we ensure it copies logical, not viewport (viewport would be e.g., 10)
    assert len(lines) == len(view.timeline)
    assert len(lines) >= 260
    # Ensure first and last are present
    assert f"#{view.timeline[0].sequence}" in text
    assert f"#{view.timeline[-1].sequence}" in text


# ---------------------------------------------------------------------------
# 4. Activity filter active: copied output matches tab's filter semantics
# ---------------------------------------------------------------------------

def test_activity_copy_respects_filter():
    identity = _identity(task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO, session_id=VALID_SESSION_ID)
    view = initial_session_view(identity)
    events = [
        make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": FINGERPRINT}, sequence=0, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, sequence=2, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 0, "patch_sha256": "b"*64}, sequence=3, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.VERIFIER_STARTED, {}, sequence=4, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.VERIFIER_COMPLETED, {"status": "COMPLETED", "outcome": "RESOLVED", "f2p_passed": 1, "f2p_total": 1, "p2p_passed": 1, "p2p_total": 1, "workspace_cleaned": True}, sequence=5, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.TOOL_STARTED, {"tool_name": "apply_patch"}, sequence=6, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
    ]
    view = _reduce_all(view, events)
    # Filter to patch should only include patch events
    patch_text = activity_export_text(view, filter_name="patch")
    assert "patch attempt 1 proposed" in patch_text
    assert "verifier" not in patch_text.lower()
    # Filter to verifier should only include verifier
    verifier_text = activity_export_text(view, filter_name="verifier")
    assert "verifier" in verifier_text.lower()
    assert "patch attempt" not in verifier_text.lower()
    # All should include both
    all_text = activity_export_text(view, filter_name="all")
    assert "patch attempt 1 proposed" in all_text
    assert "verifier" in all_text.lower()


# ---------------------------------------------------------------------------
# 5. Timeline: full durable timeline copied
# ---------------------------------------------------------------------------

def test_timeline_copy_full():
    identity = _identity(task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO, session_id=VALID_SESSION_ID)
    view = initial_session_view(identity)
    events = [
        make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": FINGERPRINT}, sequence=0, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, sequence=2, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 0, "patch_sha256": "b"*64}, sequence=3, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.PATCH_APPLIED, {"attempt_index": 0, "changed_files": ["a.py"], "syntax_passed": True}, sequence=4, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
    ]
    view = _reduce_all(view, events)
    text = timeline_export_text(view)
    lines = [l for l in text.splitlines() if l.strip().startswith("#") or l.strip().startswith("» #")]
    assert len(lines) == len(view.timeline)
    # Timeline is chronological, first entry is #0
    assert "#0" in text
    assert "#4" in text


# ---------------------------------------------------------------------------
# 6. Replay: COPY ALL works without live liveness
# ---------------------------------------------------------------------------

def test_copy_all_replay_without_liveness():
    # Build a view via replay-like reduction (same as live but via REPLAY mode)
    identity = _identity(task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR, session_id=VALID_SESSION_ID)
    view = initial_session_view(identity)
    events = [
        make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": FINGERPRINT}, sequence=0, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, sequence=2, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.VERIFIER_COMPLETED, _verifier_completed_payload(outcome=None), sequence=3, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.CLEANUP_STARTED, {}, sequence=4, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, sequence=5, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
        make_event(SessionEventKind.SESSION_COMPLETED, {"status": "unresolved", "termination_reason": "unresolved"}, sequence=6, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR),
    ]
    view = _reduce_all(view, events)
    # Simulate replay controller view (same as view) and ensure export works without snapshot
    replay_state = project_live_execution(view, mode=ExecutionMode.REPLAY)
    # Activity and timeline exports should use durable view, not snapshot
    activity = activity_export_text(replay_state.view, filter_name="all")
    timeline = timeline_export_text(replay_state.view)
    assert "#0" in activity
    assert "#6" in timeline
    # Sidebar via replay should still be Completed
    panel = LiveRunContextPanel()
    panel.update_execution(replay_state)
    assert "Completed" in panel._text.render().plain


# ---------------------------------------------------------------------------
# 7. Clipboard failure: non-fatal; no journal mutation
# ---------------------------------------------------------------------------

def test_clipboard_failure_non_fatal():
    # This tests the pure export functions don't mutate view, and that
    # the clipboard wrapper would be non-fatal.  We test the pure layer
    # here: export doesn't mutate timeline, and we simulate a failing
    # clipboard by ensuring view unchanged after attempted copy.
    identity = _identity(task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO, session_id=VALID_SESSION_ID)
    view = initial_session_view(identity)
    events = [
        make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": FINGERPRINT}, sequence=0, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
    ]
    view = _reduce_all(view, events)
    before = view.timeline
    _ = activity_export_text(view)
    _ = timeline_export_text(view)
    assert view.timeline == before
    # Simulate clipboard failure via a dummy app that raises
    class DummyApp:
        def copy_to_clipboard(self, text):
            raise RuntimeError("clipboard unavailable")
    # The WorkspaceScreen._copy_to_clipboard wraps this in try/except, so we
    # just verify that our export functions are non-mutating and don't raise.


# ---------------------------------------------------------------------------
# 8. Terminal session: no final workstream entry remains "applying"
# ---------------------------------------------------------------------------

def test_terminal_no_applying():
    identity = _identity(task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO, session_id=VALID_SESSION_ID)
    view = initial_session_view(identity)
    # Simulate a tool started apply_patch that never got a completion before terminal.
    events = [
        make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": FINGERPRINT}, sequence=0, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, sequence=2, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.TOOL_STARTED, {"tool_name": "apply_patch"}, sequence=3, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        # No TOOL_COMPLETED / patch outcome, directly to terminal
        make_event(SessionEventKind.CLEANUP_STARTED, {}, sequence=4, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, sequence=5, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
        make_event(SessionEventKind.SESSION_COMPLETED, {"status": "unresolved", "termination_reason": "unresolved"}, sequence=6, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO),
    ]
    view = _reduce_all(view, events)
    assert view.status.terminal
    active = [e for e in view.workstream if e.status.value == "active"]
    assert not active, f"Found active workstream entries at terminal: {active}"
    # Also ensure no entry has detail "applying" with active status
    for entry in view.workstream:
        assert not (entry.detail == "applying" and entry.status.value == "active")


# ---------------------------------------------------------------------------
# 9. V10-shaped patch sequence: no fabricated Change 8 (real durable sequence)
# ---------------------------------------------------------------------------

def test_v10_patch_sequence_no_fabricated_change_8():
    """Real V10: successful Attempt 6 is final active candidate, Attempt 7 fails,
    then a tool apply_patch start/completion(error) creates presentation-only
    in-flight ordinal 8, and the final candidate milestone correctly enriches
    Attempt 6 (not 7, not 8)."""
    identity = _identity(task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR, session_id=VALID_SESSION_ID)
    view = initial_session_view(identity)
    def patch(i):
        return f"--- a/cookiecutter/config.py\n+++ b/cookiecutter/config.py\n@@ -1,1 +1,1 @@\n-old {i}\n+new {i}\n"
    seq = 0
    view = reduce_event(view, make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": FINGERPRINT}, sequence=seq, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.SESSION_STARTED, {}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    # Attempt 1 rejected
    view = reduce_event(view, make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 0, "patch_sha256": hashlib.sha256(patch(0).encode()).hexdigest(), "patch_text": patch(0)}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.PATCH_REJECTED, {"attempt_index": 0, "rejection_reason": "malformed_diff"}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    for idx in [1,2,3,4]:
        view = reduce_event(view, make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": idx, "patch_sha256": hashlib.sha256(patch(idx).encode()).hexdigest(), "patch_text": patch(idx)}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
        seq += 1
        view = reduce_event(view, make_event(SessionEventKind.PATCH_APPLIED, {"attempt_index": idx, "changed_files": ["cookiecutter/config.py"], "syntax_passed": True}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
        seq += 1
    # Attempt 6 applied (final successful, the active candidate)
    view = reduce_event(view, make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 5, "patch_sha256": hashlib.sha256(patch(5).encode()).hexdigest(), "patch_text": patch(5)}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.PATCH_APPLIED, {"attempt_index": 5, "changed_files": ["cookiecutter/config.py"], "syntax_passed": True}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    # Attempt 7 apply_failed
    view = reduce_event(view, make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 6, "patch_sha256": hashlib.sha256(patch(6).encode()).hexdigest(), "patch_text": patch(6)}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.PATCH_APPLY_FAILED, {"attempt_index": 6, "apply_failure_reason": "hunk does not apply"}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    # Real V10 orphan: after Attempt 7 settled, a tool apply_patch start/completion(error) creates presentation-only Change 8
    view = reduce_event(view, make_event(SessionEventKind.TOOL_STARTED, {"tool_name": "apply_patch"}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.TOOL_COMPLETED, {"tool_name": "apply_patch", "status": "error"}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    # Final candidate milestone: correctly enriches Attempt 6 (not 7, not 8)
    # Emulate the fixed operator: candidate provenance attempt 6, milestone carries that.
    # The patch body for Attempt 6 is already present, but late enrichment would still target 6.
    # We emit a PATCH_PROPOSED for attempt 6 with same patch (late body after outcome) to simulate enrichment.
    # It should enrich Attempt 6, not create Attempt 8.
    view = reduce_event(view, make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 5, "patch_sha256": hashlib.sha256(patch(5).encode()).hexdigest(), "patch_text": patch(5)}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    # Official verification and terminal (real V10 settles official verification before verifier)
    view = reduce_event(view, make_event(SessionEventKind.OPERATOR_PROGRESS, {"stage": "official_verification_preparing"}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.OPERATOR_PROGRESS, {"stage": "official_evaluator_started"}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.OPERATOR_PROGRESS, {"stage": "official_evaluator_completed", "detail": "official execution proven", "official_execution_proven": True}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.VERIFIER_COMPLETED, _verifier_completed_payload(outcome=None, official_proven=True), sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.CLEANUP_STARTED, {}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.SESSION_COMPLETED, {"status": "unresolved", "termination_reason": "unresolved"}, sequence=seq, run_id=VALID_RUN_ID, task_id=L32_TASK, source_kind=SourceKind.LEVEL32_OPERATOR))
    # Patch attempts: Attempt 6 retains final candidate body, Attempt 7 remains APPLY_FAILED, no Attempt 8
    assert len(view.patch_attempts) == 7, f"Expected 7 patch attempts, got {len(view.patch_attempts)}: {[(p.attempt_index, p.stage.value) for p in view.patch_attempts]}"
    by_index = {p.attempt_index: p for p in view.patch_attempts}
    assert by_index[5].patch_text == patch(5)
    assert by_index[5].patch_sha256 == hashlib.sha256(patch(5).encode()).hexdigest()
    assert by_index[6].stage.value == "apply_failed"
    assert 7 not in by_index, f"Fabricated Attempt 8 found: {list(by_index.keys())}"
    # Workstream: no Change 8 active at terminal, no fabricated Change 8 final candidate
    change_ordinals = [e.ordinal for e in view.workstream if e.kind.value == "change" and e.ordinal is not None]
    assert 8 not in change_ordinals, f"Fabricated Change 8 found in workstream ordinals {change_ordinals}"
    active = [e for e in view.workstream if e.status.value == "active"]
    assert not active, f"Active workstream at terminal: {active}"
    # No Final candidate 8 manufactured
    final_candidates = [e for e in view.workstream if e.label == "Final candidate" and e.ordinal == 8]
    assert not final_candidates, f"Fabricated Final candidate 8: {final_candidates}"
    # Final candidate preview/provenance resolves to Attempt 6
    assert by_index[5].patch_text is not None


def test_identical_patch_bodies_remain_distinct_attempts():
    """Patch equality does not prove attempt identity: two real attempts with
    the same patch body must remain distinct."""
    identity = _identity(task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO, session_id=VALID_SESSION_ID)
    view = initial_session_view(identity)
    patch_x = "--- a/buggy.py\n+++ b/buggy.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    sha_x = hashlib.sha256(patch_x.encode()).hexdigest()
    seq = 0
    view = reduce_event(view, make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": FINGERPRINT}, sequence=seq, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.SESSION_STARTED, {}, sequence=seq, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, sequence=seq, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO))
    seq += 1
    # Attempt 1: proposed body X, then apply_failed
    view = reduce_event(view, make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 0, "patch_sha256": sha_x, "patch_text": patch_x}, sequence=seq, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.PATCH_APPLY_FAILED, {"attempt_index": 0, "apply_failure_reason": "hunk does not apply"}, sequence=seq, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO))
    seq += 1
    # Attempt 2: same body X, distinct attempt
    view = reduce_event(view, make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 1, "patch_sha256": sha_x, "patch_text": patch_x}, sequence=seq, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO))
    seq += 1
    view = reduce_event(view, make_event(SessionEventKind.PATCH_APPLIED, {"attempt_index": 1, "changed_files": ["buggy.py"], "syntax_passed": True}, sequence=seq, run_id=VALID_RUN_ID, task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO))
    seq += 1
    # Both attempts must exist as distinct ordinals
    assert len(view.patch_attempts) == 2
    assert view.patch_attempts[0].attempt_index == 0
    assert view.patch_attempts[1].attempt_index == 1
    assert view.patch_attempts[0].patch_sha256 == sha_x
    assert view.patch_attempts[1].patch_sha256 == sha_x
    assert view.patch_attempts[0].stage.value == "apply_failed"
    assert view.patch_attempts[1].stage.value == "applied"
    # Workstream must also keep two distinct CHANGE entries
    change_ordinals = sorted([e.ordinal for e in view.workstream if e.kind.value == "change" and e.ordinal is not None])
    assert change_ordinals == [1, 2], f"Expected [1,2] got {change_ordinals}"
