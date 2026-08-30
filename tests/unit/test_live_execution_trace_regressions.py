"""Direct behavioral regression suite for the Live execution trace and Timeline.

Covers:
- Scenario A: Long semantic Live trace (multiple model requests retained chronologically)
- Scenario B: Recoverable patch loop (attempt 1 fails -> model retries -> attempt 2 succeeds -> RESOLVED)
- Scenario C: Fatal failure context displayed truthfully (no fabricated recovery or verifier success)
- Scenario D: Replay parity from durable events
- Scenario E: Timeline timing truth (partial attribution, full attribution, no timing, overlapping measurements)
- Scenario F: Follow-tail reactive behavior in LivePanel via Textual widget integration
- Scenario G: Seven-tab workspace UI contract, 1-7 direct selection, compact footers, Help & Architecture
- Scenario H: No fabricated feedback delivery claims on patch failure retry
"""

from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest
from textual.app import App, ComposeResult

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
from agentic_debugger.ui.screens import (
    HelpModalScreen,
    WorkspaceMode,
    WorkspaceScreen,
    WORKSPACE_FOOTER_ACTIVE,
    WORKSPACE_FOOTER_ACTIVE_COMPACT,
    WORKSPACE_FOOTER_IDLE,
    WORKSPACE_FOOTER_IDLE_COMPACT,
    REPLAY_FOOTER,
    REPLAY_FOOTER_COMPACT,
)
from agentic_debugger.ui.widgets import (
    LivePanel,
    SessionTiming,
    TimingCategorySummary,
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
    timestamp_utc: Any = "DEFAULT",
    session_id: str = "sess.regression.test",
    task_id: str = "curated-off-by-one-002",
    source_kind: SourceKind = SourceKind.OFFLINE_DEMO,
) -> SessionEvent:
    ts = "2026-08-30T10:00:00Z" if timestamp_utc == "DEFAULT" else timestamp_utc
    data = {
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "event_kind": kind.value,
        "payload": payload,
        "sequence": sequence,
        "session_id": session_id,
        "task_id": task_id,
        "run_id": None if kind is SessionEventKind.SESSION_CREATED else "run-test",
        "timestamp_utc": ts,
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
# Scenario C: Fatal failure (Fail-Closed contract)
# ---------------------------------------------------------------------------

def test_scenario_c_fatal_failure_presentation() -> None:
    """Fatal termination shows failure truthfully on Live trace with error context
    and NEVER fabricates recovery, retry success, verifier success, or RESOLVED.
    """
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
    exported = live_export_text(view)

    # Truthful error context
    assert "pdb_error" in rendered.lower() or "error" in rendered.lower() or "failed" in rendered.lower()

    # Fail-closed contract: MUST NOT synthesize recovery or verifier success
    for text in (rendered.lower(), exported.lower()):
        assert "resolved" not in text
        assert "recovery" not in text
        assert "retry success" not in text
        assert "succeeded" not in text


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
# Scenario E: Timeline timing truth (measured vs unmeasured durations)
# ---------------------------------------------------------------------------

def test_scenario_e_timeline_truth_partial_attribution() -> None:
    """Timeline derives Total Elapsed strictly from timestamps and shows Unattributed honestly."""
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
    assert "Accounted:     01:00.0 (60.0s) / 02:00.0 (120.0s) (50.0%)" in rendered
    assert "Unattributed" in rendered
    assert "60.0s" in rendered
    assert "50.0%" in rendered
    assert "Not measured" in rendered
    assert "Not recorded" in rendered
    assert "—" in rendered
    # Allocation bar contains unmeasured filler
    assert "░" in rendered


def test_scenario_e_timeline_truth_fully_attributed() -> None:
    """Fully attributed timing shows 100% accounted and no unattributed remainder."""
    t0 = "2026-08-30T10:00:00Z"
    t1 = "2026-08-30T10:00:20Z"
    events = [
        _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0, timestamp_utc=t0),
        _make_event(SessionEventKind.SESSION_STARTED, {}, 1, timestamp_utc=t0),
        _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, 2, timestamp_utc=t0),
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0}, 3, timestamp_utc=t0),
        _make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 0, "status": "ok"}, 4, timestamp_utc=t1),
        _make_event(SessionEventKind.SESSION_COMPLETED, {"status": "succeeded", "termination_reason": "done"}, 5, timestamp_utc=t1),
    ]
    view = _fold_events(events)
    timing = compute_session_timing(view)
    assert timing.total_elapsed_seconds == 20.0
    assert timing.accounted_seconds == 20.0

    rendered = render_timeline_report(view, timing).plain
    assert "Total Elapsed: 20.0s" in rendered
    assert "Accounted:     20.0s / 20.0s (100.0%)" in rendered
    assert "Unattributed" not in rendered
    assert "░" not in rendered


def test_scenario_e_timeline_truth_no_timing() -> None:
    """No timing timestamps degrade honestly."""
    events = [
        _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0),
    ]
    view = _fold_events(events)
    timing = SessionTiming(
        has_timestamps=False,
        total_elapsed_seconds=None,
        accounted_seconds=0.0,
        categories=(),
        timed_operations=(),
    )
    rendered = render_timeline_report(view, timing).plain
    assert "Total Elapsed: Not recorded" in rendered
    assert "Session timing data was not recorded" in rendered


def test_scenario_e_timeline_truth_overlapping_measurements() -> None:
    """Overlapping/nested durations exceeding wall-clock are indicated honestly."""
    events = [
        _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0),
        _make_event(SessionEventKind.SESSION_STARTED, {}, 1),
    ]
    view = _fold_events(events)
    cats = (
        TimingCategorySummary(name="Model requests", total_seconds=25.0, count=1, percentage=100.0, detail="1 request"),
        TimingCategorySummary(name="Tools", total_seconds=15.0, count=1, percentage=60.0, detail="1 execution"),
    )
    timing = SessionTiming(
        has_timestamps=True,
        total_elapsed_seconds=25.0,
        accounted_seconds=40.0,
        categories=cats,
        timed_operations=(),
    )
    rendered = render_timeline_report(view, timing).plain
    assert "overlapping measurements" in rendered
    assert "Overlapping measurements — categories exceed wall-clock time" in rendered
    assert "Unattributed" not in rendered


# ---------------------------------------------------------------------------
# Scenario F: Follow-tail in LivePanel via Textual widget integration
# ---------------------------------------------------------------------------

def test_scenario_f_live_panel_follow_tail_textual_integration() -> None:
    """LivePanel follow-tail pauses on scroll up, preserves inspected viewport,
    and resumes following tail after user returns to bottom.
    """
    class LiveTestApp(App):
        def compose(self) -> ComposeResult:
            yield LivePanel(id="live-pane")

    async def scenario() -> None:
        app = LiveTestApp()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = app.query_one("#live-pane", LivePanel)
            ident = _make_identity()
            view = initial_session_view(ident)

            # 1. Populate enough workstream entries to exceed the 24-row viewport
            events = [
                _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0),
                _make_event(SessionEventKind.SESSION_STARTED, {}, 1),
                _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, 2),
            ]
            seq = 3
            for i in range(60):
                events.append(_make_event(SessionEventKind.TOOL_STARTED, {"tool_name": f"tool_{i}", "target": f"file_{i}.py"}, seq))
                seq += 1
                events.append(_make_event(SessionEventKind.TOOL_COMPLETED, {"tool_name": f"tool_{i}", "status": "ok"}, seq))
                seq += 1
            for ev in events:
                view = reduce_event(view, ev)

            panel.update_view(view)
            await pilot.pause()

            # Panel has scrolled and follow_tail is active at bottom
            assert panel.max_scroll_y > 0
            assert panel.scroll_y == panel.max_scroll_y
            assert panel._follow_tail is True

            # 2. User scrolls upward (e.g. to scroll_y = 5)
            panel.scroll_to(y=5, animate=False)
            await pilot.pause()
            assert panel.scroll_y == 5
            assert panel._follow_tail is False

            # 3. Append a new semantic operation while user is inspecting scrolled position
            events.append(_make_event(SessionEventKind.TOOL_STARTED, {"tool_name": "new_tool", "target": "inspect.py"}, seq))
            seq += 1
            view = reduce_event(view, events[-1])
            panel.update_view(view)
            await pilot.pause()

            # Viewport must remain at user's inspected position (scroll_y == 5), NOT jump to end
            assert panel.scroll_y == 5
            assert panel._follow_tail is False

            # 4. User scrolls back to bottom
            panel.scroll_to(y=panel.max_scroll_y, animate=False)
            await pilot.pause()
            assert panel._follow_tail is True

            # 5. Subsequent update follows tail again
            events.append(_make_event(SessionEventKind.TOOL_COMPLETED, {"tool_name": "new_tool", "status": "ok"}, seq))
            seq += 1
            view = reduce_event(view, events[-1])
            panel.update_view(view)
            await pilot.pause()
            assert panel.scroll_y == panel.max_scroll_y
            assert panel._follow_tail is True

    import asyncio
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Scenario G: Seven-tab workspace UI contract and Help & Architecture
# ---------------------------------------------------------------------------

def test_scenario_g_seven_tabs_navigation_and_help_contract() -> None:
    """WorkspaceScreen defines 7 tabs in order and direct 1-7 selection without activity filter wording."""
    identity = _make_identity()
    view = initial_session_view(identity)

    class WorkspaceTestApp(App):
        def live_events(self):
            return ()

        def live_execution_state(self):
            return None

        def detach_live_workspace(self, ws):
            pass

        def refresh_home_history(self):
            pass

        def compose(self) -> ComposeResult:
            yield WorkspaceScreen(
                mode=WorkspaceMode.LIVE,
                identity=identity,
                view=view,
            )

    async def scenario() -> None:
        app = WorkspaceTestApp()
        async with app.run_test(size=(120, 32)) as pilot:
            workspace = app.query_one(WorkspaceScreen)

            # Tab IDs and order
            assert workspace._VIEW_IDS == (
                "tab-live",
                "tab-evidence",
                "tab-source",
                "tab-debugger",
                "tab-patch",
                "tab-verifier",
                "tab-timeline",
            )

            tabs = workspace.query_one("#pane-tabs")
            assert tabs.active == "tab-live"

            # Direct 1-7 tab key selection
            for key, expected_id in [
                ("1", "tab-live"),
                ("2", "tab-evidence"),
                ("3", "tab-source"),
                ("4", "tab-debugger"),
                ("5", "tab-patch"),
                ("6", "tab-verifier"),
                ("7", "tab-timeline"),
            ]:
                await pilot.press(key)
                assert tabs.active == expected_id

            # Footer contains 1-7 tabs and no activity filter terminology
            bar = workspace.query_one("#live-bar")
            bar_text = str(bar.render())
            assert "1-7 tabs" in bar_text
            assert "activity filter" not in bar_text.lower()
            assert "activity filters" not in bar_text.lower()

    import asyncio
    asyncio.run(scenario())


def test_scenario_g_help_modal_workspace_views_description() -> None:
    """Help & Architecture accurately describes the 7 workspace views and navigation."""
    class HelpApp(App):
        def compose(self) -> ComposeResult:
            yield HelpModalScreen()

    async def scenario() -> None:
        app = HelpApp()
        async with app.run_test(size=(100, 30)) as pilot:
            help_screen = app.query_one(HelpModalScreen)
            text = str(help_screen.query_one("#help-content").render().plain)
            assert "Live — Operational execution story" in text
            assert "Evidence — Causal proof state" in text
            assert "Source — Source evidence" in text
            assert "Debugger — Runtime/PDB evidence" in text
            assert "Patch — Candidate lifecycle/diff" in text
            assert "Verifier — Independent correctness authority" in text
            assert "Timeline — Session time consumption" in text
            assert "Activity — Filtered" not in text
            assert "1–8 activity filter" not in text
            assert "1–7 direct tabs" in text

    import asyncio
    asyncio.run(scenario())


def test_scenario_g_compact_footer_restraint() -> None:
    """Footers have clean, restrained compact variants."""
    assert "1-7 tabs" in WORKSPACE_FOOTER_ACTIVE
    assert "1-7 tabs" in WORKSPACE_FOOTER_ACTIVE_COMPACT
    assert "1-7 tabs" in WORKSPACE_FOOTER_IDLE
    assert "1-7 tabs" in WORKSPACE_FOOTER_IDLE_COMPACT
    assert "1-7 tabs" in REPLAY_FOOTER
    assert "1-7 tabs" in REPLAY_FOOTER_COMPACT

    for footer in (
        WORKSPACE_FOOTER_ACTIVE,
        WORKSPACE_FOOTER_ACTIVE_COMPACT,
        WORKSPACE_FOOTER_IDLE,
        WORKSPACE_FOOTER_IDLE_COMPACT,
        REPLAY_FOOTER,
        REPLAY_FOOTER_COMPACT,
    ):
        assert "activity" not in footer.lower()
        assert "1-8" not in footer


# ---------------------------------------------------------------------------
# Scenario H: No fabricated feedback delivery claims
# ---------------------------------------------------------------------------

def test_scenario_h_recoverable_patch_loop_does_not_claim_feedback_without_durable_evidence() -> None:
    """A failed patch followed by model request says 'Continuing after patch failure',
    never claiming structured feedback was delivered without durable evidence.
    """
    events = [
        _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0),
        _make_event(SessionEventKind.SESSION_STARTED, {}, 1),
        _make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 0, "patch_sha256": "1" * 64}, 2),
        _make_event(SessionEventKind.PATCH_APPLY_FAILED, {"attempt_index": 0, "apply_failure_reason": "syntax_error"}, 3),
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 1}, 4),
        _make_event(SessionEventKind.MODEL_DIRECTIVE_ACCEPTED, {"action_name": "apply_patch", "directive_kind": "action", "target_state": None}, 5),
        _make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 1, "status": "ok"}, 6),
    ]
    view = _fold_events(events)
    rendered = render_live_trace(view).plain
    exported = live_export_text(view)
    assert "Continuing after patch failure" in rendered
    assert "Continuing after patch failure" in exported
    assert "patch failure feedback" not in rendered.lower()
    assert "patch failure feedback" not in exported.lower()

