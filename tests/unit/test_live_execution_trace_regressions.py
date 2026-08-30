"""Direct behavioral regression suite for the Live execution trace and Timeline.

Covers:
- Scenario A: Long semantic Live trace (multiple model requests retained chronologically)
- Scenario B: Recoverable patch loop (attempt 1 fails -> model retries -> attempt 2 succeeds -> RESOLVED)
- Scenario C: Fatal failure context displayed truthfully
- Scenario D: Replay parity from durable events
- Scenario E: Timeline timing truth (measured vs unmeasured, wall-clock invariance)
- Scenario F: Follow-tail reactive behavior in LivePanel
"""

from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest

from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
)
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    SessionStatus,
    SessionViewState,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.application.workstream import (
    WorkstreamKind,
    WorkstreamStatus,
)
from agentic_debugger.application.journal import SessionEventJournal, read_session_journal
from agentic_debugger.application.replay import SessionReplaySource
from agentic_debugger.ui.widgets import (
    LivePanel,
    compute_session_timing,
    live_export_text,
    render_live_trace,
    render_timeline_report,
    timeline_export_text,
)


def _make_identity(
    task_id: str = "curated-off-by-one-002",
    source_kind: SourceKind = SourceKind.OFFLINE_DEMO,
    session_id: str = "sess.regression.test",
) -> PresentationIdentity:
    return PresentationIdentity(
        task_id=task_id,
        source_kind=source_kind,
        session_id=session_id,
    )


def _make_event(
    kind: SessionEventKind,
    payload: dict[str, Any],
    sequence: int,
    *,
    timestamp_utc: Optional[str] = None,
    session_id: str = "sess.regression.test",
    task_id: str = "curated-off-by-one-002",
    source_kind: SourceKind = SourceKind.OFFLINE_DEMO,
) -> SessionEvent:
    data = {
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "event_kind": kind.value,
        "payload": payload,
        "sequence": sequence,
        "session_id": session_id,
        "task_id": task_id,
        "run_id": None if kind is SessionEventKind.SESSION_CREATED else "run-test",
        "timestamp_utc": timestamp_utc or "2026-08-30T10:00:00Z",
        "source_kind": source_kind.value,
        "controller_phase": None,
    }
    return SessionEvent.from_mapping(data)


def _fold_events(
    events: list[SessionEvent],
    identity: Optional[PresentationIdentity] = None,
) -> SessionViewState:
    ident = identity or _make_identity()
    view = initial_session_view(ident)
    for event in events:
        view = reduce_event(view, event)
    return view


# ---------------------------------------------------------------------------
# Scenario A: Long semantic Live trace
# ---------------------------------------------------------------------------

def test_scenario_a_long_semantic_live_trace() -> None:
    """Multiple model requests are retained in chronological order with intent."""
    events = [
        _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0),
        _make_event(SessionEventKind.SESSION_STARTED, {}, 1),
        _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, 2),
    ]
    seq = 3
    actions = [
        ("get_source_window", "Inspect source"),
        ("get_stack_summary", "Inspect stack"),
        ("get_frame", "Inspect frame"),
        ("get_frame_locals", "Inspect locals"),
        ("apply_patch", "Apply change"),
    ]
    for idx, (action_name, expected_detail) in enumerate(actions):
        events.append(_make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": idx}, seq))
        seq += 1
        events.append(_make_event(
            SessionEventKind.MODEL_DIRECTIVE_ACCEPTED,
            {"action_name": action_name, "directive_kind": "action", "target_state": None},
            seq,
        ))
        seq += 1
        events.append(_make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": idx, "status": "ok"}, seq))
        seq += 1

    events.append(_make_event(SessionEventKind.SESSION_COMPLETED, {"status": "succeeded", "termination_reason": "done"}, seq))

    view = _fold_events(events)

    model_entries = [e for e in view.workstream if e.kind is WorkstreamKind.MODEL_REQUEST]
    assert len(model_entries) == 5
    assert [e.ordinal for e in model_entries] == [1, 2, 3, 4, 5]
    for entry, (_, expected_detail) in zip(model_entries, actions):
        assert entry.detail == expected_detail

    # Export must preserve all 5 model requests in chronological order
    exported = live_export_text(view)
    for ord_num in [1, 2, 3, 4, 5]:
        assert f"MODEL #{ord_num}" in exported


# ---------------------------------------------------------------------------
# Scenario B: Recoverable patch loop
# ---------------------------------------------------------------------------

def test_scenario_b_recoverable_patch_loop() -> None:
    """Attempt 1 fails -> model retries -> attempt 2 succeeds -> verified RESOLVED."""
    events = [
        _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0),
        _make_event(SessionEventKind.SESSION_STARTED, {}, 1),
        _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, 2),

        # Request 1: proposes failing patch
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0}, 3),
        _make_event(SessionEventKind.MODEL_DIRECTIVE_ACCEPTED, {"action_name": "apply_patch", "directive_kind": "action", "target_state": None}, 4),
        _make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 0, "status": "ok"}, 5),
        _make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 0, "patch_sha256": "1" * 64, "patch_text": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-bad\n+wrong\n"}, 6),
        _make_event(SessionEventKind.PATCH_APPLY_FAILED, {"attempt_index": 0, "apply_failure_reason": "syntax_error"}, 7),

        # Request 2: corrects syntax and proposes patch 2
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 1}, 8),
        _make_event(SessionEventKind.MODEL_DIRECTIVE_ACCEPTED, {"action_name": "apply_patch", "directive_kind": "action", "target_state": None}, 9),
        _make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 1, "status": "ok"}, 10),
        _make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 1, "patch_sha256": "2" * 64, "patch_text": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-bad\n+fixed\n"}, 11),
        _make_event(SessionEventKind.PATCH_APPLIED, {"attempt_index": 1, "changed_files": ["x.py"], "syntax_passed": True}, 12),

        # Independent verification succeeds
        _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "verifying"}, 13),
        _make_event(SessionEventKind.VERIFIER_STARTED, {}, 14),
        _make_event(SessionEventKind.VERIFIER_COMPLETED, {
            "status": "COMPLETED",
            "outcome": "RESOLVED",
            "f2p_passed": 1,
            "f2p_total": 1,
            "p2p_passed": 2,
            "p2p_total": 2,
            "workspace_cleaned": True,
        }, 15),
        _make_event(SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, 16),
        _make_event(SessionEventKind.SESSION_COMPLETED, {"status": "succeeded", "termination_reason": "done"}, 17),
    ]

    view = _fold_events(events)

    # Check that both patch attempts exist in workstream
    patches = [e for e in view.workstream if e.kind is WorkstreamKind.CHANGE]
    assert len(patches) == 2
    assert patches[0].ordinal == 1
    assert patches[0].status is WorkstreamStatus.FAILED
    assert patches[1].ordinal == 2
    assert patches[1].status is WorkstreamStatus.COMPLETED

    # Check that verification is recorded as resolved
    ver = [e for e in view.workstream if e.kind is WorkstreamKind.VERIFICATION]
    assert len(ver) == 1
    assert ver[0].status is WorkstreamStatus.COMPLETED
    assert "resolved" in (ver[0].detail or "").lower()

    # Export includes the full story truthfully
    exported = live_export_text(view)
    assert "MODEL #1" in exported
    assert "PATCH #1" in exported
    assert "MODEL #2" in exported
    assert "PATCH #2" in exported
    assert "VERIFY" in exported
    assert "resolved" in exported.lower()


# ---------------------------------------------------------------------------
# Scenario C: Fatal failure
# ---------------------------------------------------------------------------

def test_scenario_c_fatal_failure_presentation() -> None:
    """Fatal termination shows failure truthfully on Live trace with error context."""
    events = [
        _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0),
        _make_event(SessionEventKind.SESSION_STARTED, {}, 1),
        _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, 2),
        _make_event(SessionEventKind.TOOL_STARTED, {"tool_name": "run_pdb"}, 3),
        _make_event(SessionEventKind.TOOL_COMPLETED, {"tool_name": "run_pdb", "status": "error"}, 4),
        _make_event(SessionEventKind.SESSION_FAILED, {"status": "failed", "termination_reason": "pdb_error"}, 5),
    ]

    view = _fold_events(events)
    assert view.status is SessionStatus.FAILED

    rendered = render_live_trace(view).plain
    assert "ERROR" in rendered or "FAILED" in rendered or "pdb" in rendered.lower()


# ---------------------------------------------------------------------------
# Scenario D: Replay parity
# ---------------------------------------------------------------------------

def test_scenario_d_replay_parity(tmp_path: Path) -> None:
    """Reopening a session from durable events reproduces the semantic Live trace and timeline."""
    session_id = "sess.replay.parity"
    journal_path = tmp_path / "session.events.jsonl"
    journal = SessionEventJournal(
        journal_path,
        session_id=session_id,
        task_id="curated-off-by-one-002",
        source_kind=SourceKind.OFFLINE_DEMO,
    )
    t0 = "2026-08-30T10:00:00Z"
    t1 = "2026-08-30T10:00:02.500000Z"
    events = [
        _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0, session_id=session_id, timestamp_utc=t0),
        _make_event(SessionEventKind.SESSION_STARTED, {}, 1, session_id=session_id, timestamp_utc=t0),
        _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, 2, session_id=session_id, timestamp_utc=t0),
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0}, 3, session_id=session_id, timestamp_utc=t0),
        _make_event(SessionEventKind.MODEL_DIRECTIVE_ACCEPTED, {"action_name": "get_source_window", "directive_kind": "action", "target_state": None}, 4, session_id=session_id, timestamp_utc=t0),
        _make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 0, "status": "ok"}, 5, session_id=session_id, timestamp_utc=t1),
        _make_event(SessionEventKind.CLEANUP_STARTED, {}, 6, session_id=session_id, timestamp_utc=t1),
        _make_event(SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, 7, session_id=session_id, timestamp_utc=t1),
        _make_event(SessionEventKind.SESSION_COMPLETED, {"status": "succeeded", "termination_reason": "done"}, 8, session_id=session_id, timestamp_utc=t1),
    ]
    for ev in events:
        journal.append(ev)
    journal.close()

    # Read back through SessionReplaySource
    read_result = read_session_journal(journal_path)
    assert read_result.is_success
    replay = SessionReplaySource(read_result.events, source_kind=SourceKind.OFFLINE_DEMO, task_id="curated-off-by-one-002")
    replayed_events = tuple(replay.events)
    assert len(replayed_events) == len(events)

    ident = _make_identity(session_id=session_id)
    view = initial_session_view(ident)
    for ev in replayed_events:
        view = reduce_event(view, ev)

    live_text = live_export_text(view)
    assert "MODEL #1" in live_text
    assert "Inspect source" in live_text

    timeline_text = timeline_export_text(view)
    assert "SESSION TIME BREAKDOWN" in timeline_text
    assert "Model requests" in timeline_text
    assert "2.5s" in timeline_text


# ---------------------------------------------------------------------------
# Scenario E: Timeline truth (measured vs unmeasured durations)
# ---------------------------------------------------------------------------

def test_scenario_e_timeline_truth_measured_vs_unmeasured() -> None:
    """Timeline derives Total Elapsed strictly from timestamps and shows Not recorded for unmeasured."""
    t0 = "2026-08-30T10:00:00Z"
    t_mod_start = "2026-08-30T10:00:00Z"
    t_mod_end = "2026-08-30T10:00:30Z"    # 30.0s model
    t_tool_start = "2026-08-30T10:00:30Z"
    t_tool_end = "2026-08-30T10:00:40Z"   # 10.0s tool
    t_ver_start = "2026-08-30T10:01:40Z"
    t_ver_end = "2026-08-30T10:02:00Z"     # 20.0s verifier; total elapsed = 120s

    events = [
        _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0, timestamp_utc=t0),
        _make_event(SessionEventKind.SESSION_STARTED, {}, 1, timestamp_utc=t0),
        _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, 2, timestamp_utc=t0),

        # Measured model request: 30.0s
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0}, 3, timestamp_utc=t_mod_start),
        _make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 0, "status": "ok"}, 4, timestamp_utc=t_mod_end),

        # Measured tool: 10.0s
        _make_event(SessionEventKind.TOOL_STARTED, {"tool_name": "custom_search"}, 5, timestamp_utc=t_tool_start),
        _make_event(SessionEventKind.TOOL_COMPLETED, {"tool_name": "custom_search", "status": "ok"}, 6, timestamp_utc=t_tool_end),

        # Unmeasured debugger events (no completion duration)
        _make_event(SessionEventKind.DEBUGGER_STARTED, {"script": "main.py", "breakpoints": ["main.py:10"]}, 7, timestamp_utc=t_tool_end),
        _make_event(SessionEventKind.DEBUGGER_STACK_OBSERVED, {"pause_generation": 1, "frames": []}, 8, timestamp_utc=t_tool_end),

        # Unmeasured patch (no duration on apply)
        _make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 0, "patch_sha256": "0" * 64}, 9, timestamp_utc=t_tool_end),
        _make_event(SessionEventKind.PATCH_APPLIED, {"attempt_index": 0, "changed_files": ["main.py"], "syntax_passed": True}, 10, timestamp_utc=t_tool_end),

        # Measured verification: 20.0s
        _make_event(SessionEventKind.VERIFIER_STARTED, {}, 11, timestamp_utc=t_ver_start),
        _make_event(SessionEventKind.VERIFIER_COMPLETED, {
            "status": "COMPLETED",
            "outcome": "RESOLVED",
            "f2p_passed": 1,
            "f2p_total": 1,
            "p2p_passed": 1,
            "p2p_total": 1,
            "workspace_cleaned": True,
        }, 12, timestamp_utc=t_ver_end),

        _make_event(SessionEventKind.SESSION_COMPLETED, {"status": "succeeded", "termination_reason": "done"}, 13, timestamp_utc=t_ver_end),
    ]

    view = _fold_events(events)
    timing = compute_session_timing(view)

    # 1. Total elapsed must be exactly 120.0s (wall clock from t0 to t_ver_end)
    assert timing.total_elapsed_seconds == 120.0
    # 2. Accounted seconds is 30 + 10 + 20 = 60.0s (NOT replacing total_elapsed)
    assert timing.accounted_seconds == 60.0

    cat_map = {c.name: c for c in timing.categories}
    # Measured
    assert cat_map["Model requests"].total_seconds == 30.0
    assert cat_map["Model requests"].percentage == pytest.approx(25.0)

    assert cat_map["Tools"].total_seconds == 10.0
    assert cat_map["Tools"].percentage == pytest.approx(8.333, abs=0.1)

    assert cat_map["Verification"].total_seconds == 20.0
    assert cat_map["Verification"].percentage == pytest.approx(16.666, abs=0.1)

    # Unmeasured categories must be None (never 0.0s)
    assert cat_map["Debugger / PDB"].total_seconds is None
    assert cat_map["Debugger / PDB"].percentage is None
    assert "(unmeasured)" in cat_map["Debugger / PDB"].detail

    assert cat_map["Patch lifecycle"].total_seconds is None
    assert cat_map["Patch lifecycle"].percentage is None
    assert "(unmeasured)" in cat_map["Patch lifecycle"].detail

    # Rendered report must reflect truthfulness
    rendered = render_timeline_report(view, timing).plain
    assert "Total Elapsed: 02:00.0 (120.0s)" in rendered
    assert "Not recorded" in rendered
    assert "—" in rendered


# ---------------------------------------------------------------------------
# Scenario F: Follow-tail in LivePanel
# ---------------------------------------------------------------------------

def test_scenario_f_live_panel_follow_tail() -> None:
    """LivePanel follow-tail pauses on scroll up and resumes at bottom."""
    panel = LivePanel()
    assert panel._follow_tail is True

    with patch.object(LivePanel, "max_scroll_y", new_callable=lambda: property(lambda self: 100)):
        # User scrolls away from the bottom (e.g. scroll_y = 10, max_scroll_y = 100)
        panel.watch_scroll_y(0.0, 10.0)
        assert panel._follow_tail is False

        # User scrolls back to the bottom (scroll_y = 99 or 100)
        panel.watch_scroll_y(10.0, 100.0)
        assert panel._follow_tail is True
