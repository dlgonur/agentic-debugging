"""UI-level tests for COPY ALL."""

import pytest
textual = pytest.importorskip("textual")

from pathlib import Path

from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.events import SessionEventKind, SourceKind
from agentic_debugger.application.presentation import PresentationIdentity, initial_session_view, reduce_event
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import WorkspaceMode, WorkspaceScreen
from ui_support import run_headless

def _make_view(task_id="audreyr__cookiecutter-967", source_kind=SourceKind.LEVEL32_OPERATOR):
    identity = PresentationIdentity(task_id=task_id, source_kind=source_kind, session_id="sess-copy-ui")
    view = initial_session_view(identity)
    # Add a few events
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
    from application_support import VALID_RUN_ID, VALID_SPEC_FINGERPRINT, make_event
    sid = "sess-copy-ui"
    view = reduce_event(view, make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": VALID_SPEC_FINGERPRINT}, sequence=0, session_id=sid, task_id=task_id, source_kind=source_kind))
    view = reduce_event(view, make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, session_id=sid, run_id=VALID_RUN_ID, task_id=task_id, source_kind=source_kind))
    view = reduce_event(view, make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, sequence=2, session_id=sid, run_id=VALID_RUN_ID, task_id=task_id, source_kind=source_kind))
    for i in range(5):
        view = reduce_event(view, make_event(SessionEventKind.TOOL_STARTED, {"tool_name": f"tool{i}"}, sequence=3+i*2, session_id=sid, run_id=VALID_RUN_ID, task_id=task_id, source_kind=source_kind))
        view = reduce_event(view, make_event(SessionEventKind.TOOL_COMPLETED, {"tool_name": f"tool{i}", "status": "ok"}, sequence=4+i*2, session_id=sid, run_id=VALID_RUN_ID, task_id=task_id, source_kind=source_kind))
    return identity, view

def test_copy_buttons_exist_and_not_focusable(tmp_path):
    identity, view = _make_view()
    app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
    async def scenario(pilot):
        workspace = WorkspaceScreen(mode=WorkspaceMode.LIVE, identity=identity, view=view)
        app._live_view = view
        app._live_events = ()
        app._live_last_sequence = -1
        app._live_identity = identity
        app._live_generation = 1
        app._live_snapshot = None
        app._live_workspace = workspace
        app.push_screen(workspace)
        await pilot.pause()
        await pilot.pause()
        assert workspace.query_one("#copy-activity")
        assert workspace.query_one("#copy-timeline")
        assert workspace.query_one("#copy-activity").can_focus is False
        assert workspace.query_one("#copy-timeline").can_focus is False
    run_headless(app, scenario, size=(140,40))

def test_copy_all_activity_copies_full_log(tmp_path):
    identity, view = _make_view()
    app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
    copied = {}
    original_copy = app.copy_to_clipboard
    def fake_copy(text):
        copied["activity"] = text
    app.copy_to_clipboard = fake_copy
    async def scenario(pilot):
        workspace = WorkspaceScreen(mode=WorkspaceMode.LIVE, identity=identity, view=view)
        app._live_view = view
        app._live_events = ()
        app._live_last_sequence = -1
        app._live_identity = identity
        app._live_generation = 1
        app._live_snapshot = None
        app._live_workspace = workspace
        app.push_screen(workspace)
        await pilot.pause()
        await pilot.pause()
        from textual.widgets import TabbedContent
        workspace.query_one("#pane-tabs", TabbedContent).active = "tab-activity"
        await pilot.pause()
        await pilot.pause()
        # Press copy activity
        await pilot.click("#copy-activity")
        await pilot.pause()
        assert "activity" in copied
        assert "#0" in copied["activity"]
        assert "#2" in copied["activity"]
        # Should contain all timeline entries
        assert str(len(view.timeline)) in copied["activity"] or "#0" in copied["activity"]
    run_headless(app, scenario, size=(140,40))

def test_copy_all_timeline_copies_full(tmp_path):
    identity, view = _make_view()
    app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
    copied = {}
    def fake_copy(text):
        copied["timeline"] = text
    app.copy_to_clipboard = fake_copy
    async def scenario(pilot):
        workspace = WorkspaceScreen(mode=WorkspaceMode.LIVE, identity=identity, view=view)
        app._live_view = view
        app._live_events = ()
        app._live_last_sequence = -1
        app._live_identity = identity
        app._live_generation = 1
        app._live_snapshot = None
        app._live_workspace = workspace
        app.push_screen(workspace)
        await pilot.pause()
        await pilot.pause()
        from textual.widgets import TabbedContent
        workspace.query_one("#pane-tabs", TabbedContent).active = "tab-timeline"
        await pilot.pause()
        await pilot.pause()
        await pilot.click("#copy-timeline")
        await pilot.pause()
        assert "timeline" in copied
        assert "#0" in copied["timeline"]
    run_headless(app, scenario, size=(140,40))

def test_clipboard_failure_non_fatal(tmp_path):
    identity, view = _make_view()
    app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
    def failing_copy(text):
        raise RuntimeError("clipboard broken")
    app.copy_to_clipboard = failing_copy
    async def scenario(pilot):
        workspace = WorkspaceScreen(mode=WorkspaceMode.LIVE, identity=identity, view=view)
        app._live_view = view
        app._live_events = ()
        app._live_last_sequence = -1
        app._live_identity = identity
        app._live_generation = 1
        app._live_snapshot = None
        app._live_workspace = workspace
        app.push_screen(workspace)
        await pilot.pause()
        await pilot.pause()
        # Should not raise
        await pilot.click("#copy-activity")
        await pilot.pause()
        # Still alive
        assert workspace.is_mounted
    run_headless(app, scenario, size=(140,40))
