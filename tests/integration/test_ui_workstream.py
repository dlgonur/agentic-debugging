"""Provider-free UI integration tests for the operational workstream.

Drives the real Textual workspace headlessly with canned schema-valid
session state only (no provider, no worker, no PDB).  Covers the
LIVE-WORKSTREAM-02 presentation contract: empty-pane expansion, compact
mode when evidence exists, narrow degradation, Activity/Timeline purity,
non-focusability, and replay truth.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

textual = pytest.importorskip("textual")

from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
)
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import WorkspaceMode, WorkspaceScreen
from agentic_debugger.ui.widgets import WorkstreamPanel, SourcePanel

from textual.css.scalar import Unit
from textual.widgets import Static as _Static
from textual.widgets import TabbedContent

REPO_ROOT = Path(__file__).resolve().parents[2]
L32_TASK = "audreyr__cookiecutter-967"
FINGERPRINT = "a" * 64
CLOCK = "2026-08-25T10:00:00Z"

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


def pane_text(workspace, selector: str) -> str:
    pane = workspace.query_one(selector)
    static = pane.query_one(_Static)
    rendered = static.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


class Canned:
    """Builds a strictly schema-valid canned Level-32 stream."""

    def __init__(self) -> None:
        self.emitter = SessionEventEmitter(
            session_id="sess-workstream-ui",
            task_id=L32_TASK,
            source_kind=SourceKind.LEVEL32_OPERATOR,
            clock=lambda: CLOCK,
        )
        self.events: list[SessionEvent] = []
        self.identity = PresentationIdentity(
            task_id=L32_TASK,
            source_kind=SourceKind.LEVEL32_OPERATOR,
            session_id="sess-workstream-ui",
        )
        self.emit(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": FINGERPRINT})
        self.emitter.bind_run_id("run-w")
        self.emit(SessionEventKind.SESSION_STARTED, {})
        self.emit(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"})
        self.emit(
            SessionEventKind.MODEL_CONFIGURED,
            {
                "profile_id": "glm-5.2:cloud",
                "config_fingerprint": FINGERPRINT,
                "display_name": "GLM 5.2",
                "protocol_version": "1.3",
                "tool_version": "level32-frozen-operator",
                "treatment_revision": 7,
                "treatment_id": "level32-t7",
                "result_location": "experiments/t7",
            },
        )
        for stage in ("starting", "preflight", "preparing_workspace"):
            self.emit(SessionEventKind.OPERATOR_PROGRESS, {"stage": stage})

    def emit(self, kind: SessionEventKind, payload: dict) -> None:
        self.events.append(self.emitter.emit(kind, payload))

    def view(self):
        view = initial_session_view(self.identity)
        for event in self.events:
            view = reduce_event(view, event)
        return view


def base_stream() -> Canned:
    stream = Canned()
    for index in range(3):
        stream.emit(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": index})
        stream.emit(
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            {"request_index": index, "status": "ok"},
        )
    stream.emit(
        SessionEventKind.TOOL_COMPLETED,
        {
            "tool_name": "get_source_window",
            "status": "ok",
            "target": "cookiecutter/config.py:42-66",
        },
    )
    stream.emit(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 3})
    return stream


def debugger_facts(stream: Canned) -> None:
    stream.emit(
        SessionEventKind.DEBUGGER_STARTED,
        {"script": "cookiecutter/config.py", "breakpoints": ("cookiecutter/config.py:58",)},
    )
    stream.emit(
        SessionEventKind.DEBUGGER_LOCATION_CHANGED,
        {
            "script": "cookiecutter/config.py",
            "line": 58,
            "function": None,
            "pause_generation": 1,
        },
    )
    stream.emit(
        SessionEventKind.DEBUGGER_STACK_OBSERVED,
        {"pause_generation": 1, "frames": ()},
    )


def applied_candidate(stream: Canned, *, with_patch_text: bool) -> None:
    stream.emit(
        SessionEventKind.PATCH_APPLIED,
        {
            "attempt_index": 0,
            "changed_files": ("cookiecutter/config.py",),
            "syntax_passed": None,
        },
    )
    if with_patch_text:
        stream.emit(
            SessionEventKind.PATCH_PROPOSED,
            {
                "attempt_index": 0,
                "patch_sha256": hashlib.sha256(PATCH.encode()).hexdigest(),
                "patch_text": PATCH,
            },
        )


def push_live(app: LocalApplicationV1, stream: Canned):
    workspace = WorkspaceScreen(
        mode=WorkspaceMode.LIVE,
        identity=stream.identity,
        view=initial_session_view(stream.identity),
    )
    app._live_view = stream.view()
    app._live_events = tuple(stream.events)
    app._live_last_sequence = stream.events[-1].sequence
    app._live_identity = stream.identity
    app._live_generation = 1
    app._live_snapshot = None
    app._live_workspace = workspace
    app.push_screen(workspace)
    return workspace


def finish(app: LocalApplicationV1, workspace) -> None:
    app._live_workspace = None
    app._live_runner = None


class TestWorkstreamRendering:
    def test_empty_source_pane_expands_workstream(self, tmp_path):
        """Level-32 early execution: no source evidence yet, the main body
        shows the operational workstream instead of a mostly blank pane."""
        app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
        stream = base_stream()

        async def scenario(pilot):
            workspace = push_live(app, stream)
            await pilot.pause()
            await pilot.pause()
            body = pane_text(workspace, "#source-workstream")
            assert "READ SOURCE" in body or "Read source" in body
            assert "cookiecutter/config.py:42-66" in body
            assert "Model request" in body
            # the workstream owns the main area (expanded), the source pane
            # collapses to its short pending placeholder
            workstream = workspace.query_one("#source-workstream", WorkstreamPanel)
            pane = workspace.query_one("#source-pane", SourcePanel)
            assert workstream.styles.height.unit is Unit.FRACTION
            assert pane.styles.height.unit is Unit.AUTO
            assert "Waiting for source evidence" in pane_text(workspace, "#source-pane")
            finish(app, workspace)

        from ui_support import run_headless

        run_headless(app, scenario, size=(140, 40))

    def test_source_pane_with_evidence_keeps_compact_workstream(self, tmp_path):
        app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
        stream = base_stream()
        stream.emit(
            SessionEventKind.SOURCE_SNAPSHOT,
            {
                "path": "cookiecutter/config.py",
                "sha256": FINGERPRINT,
                "text": "def prompt_and_delete(config, key, no_input=False):\n    return config.get(key)\n",
                "line_count": 2,
                "truncated": False,
                "stage": "initial",
            },
        )

        async def scenario(pilot):
            workspace = push_live(app, stream)
            await pilot.pause()
            await pilot.pause()
            workstream = workspace.query_one("#source-workstream", WorkstreamPanel)
            assert workstream.styles.height.unit is Unit.AUTO
            body = pane_text(workspace, "#source-workstream")
            assert "cookiecutter/config.py:42-66" in body
            assert "prompt_and_delete" in pane_text(workspace, "#source-pane")
            finish(app, workspace)

        from ui_support import run_headless

        run_headless(app, scenario, size=(140, 40))

    def test_change_preview_renders_real_diff_with_truthful_label(self, tmp_path):
        app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
        stream = base_stream()
        debugger_facts(stream)
        applied_candidate(stream, with_patch_text=True)

        async def scenario(pilot):
            workspace = push_live(app, stream)
            await pilot.pause()
            await pilot.pause()
            body = pane_text(workspace, "#source-workstream")
            assert "APPLIED CHANGE" in body
            assert "cookiecutter/config.py" in body
            assert "+1 -1" in body
            # the actual diff body: removed and added lines from the patch
            assert "return None" in body
            assert 'return ""' in body
            assert "REJECTED" not in body
            finish(app, workspace)

        from ui_support import run_headless

        run_headless(app, scenario, size=(140, 40))

    def test_rejected_change_never_labelled_applied(self, tmp_path):
        app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
        stream = base_stream()
        stream.emit(
            SessionEventKind.PATCH_PROPOSED,
            {
                "attempt_index": 0,
                "patch_sha256": FINGERPRINT,
                "patch_text": PATCH,
            },
        )
        stream.emit(
            SessionEventKind.PATCH_REJECTED,
            {"attempt_index": 0, "rejection_reason": "patch context mismatch"},
        )

        async def scenario(pilot):
            workspace = push_live(app, stream)
            await pilot.pause()
            await pilot.pause()
            body = pane_text(workspace, "#source-workstream")
            assert "REJECTED CHANGE" in body
            assert "APPLIED" not in body
            # the rejected candidate may still show its proposed diff
            assert "return None" in body
            finish(app, workspace)

        from ui_support import run_headless

        run_headless(app, scenario, size=(140, 40))

    def test_patch_pane_shows_bounded_detailed_diff(self, tmp_path):
        app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
        stream = base_stream()
        applied_candidate(stream, with_patch_text=True)

        async def scenario(pilot):
            workspace = push_live(app, stream)
            await pilot.pause()
            workspace.query_one("#pane-tabs", TabbedContent).active = "tab-patch"
            await pilot.pause()
            await pilot.pause()
            body = pane_text(workspace, "#patch-pane")
            assert "Attempt 1 — APPLIED" in body
            assert "CHANGED FILES" in body
            assert "DIFF" in body
            assert "return None" in body
            finish(app, workspace)

        from ui_support import run_headless

        run_headless(app, scenario, size=(140, 40))

    def test_narrow_mode_drops_diff_body_keeps_one_line_change(self, tmp_path):
        app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
        stream = base_stream()
        applied_candidate(stream, with_patch_text=True)

        async def scenario(pilot):
            workspace = push_live(app, stream)
            await pilot.pause()
            await pilot.pause()
            body = pane_text(workspace, "#source-workstream")
            assert "Applied change" in body
            assert "+1 -1" in body
            # narrow: no diff body, no widened layout
            assert "│" not in body
            finish(app, workspace)

        from ui_support import run_headless

        run_headless(app, scenario, size=(92, 36))

    def test_activity_and_timeline_have_no_workstream_duplicate(self, tmp_path):
        app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
        stream = base_stream()

        async def scenario(pilot):
            workspace = push_live(app, stream)
            await pilot.pause()
            workspace.query_one("#pane-tabs", TabbedContent).active = "tab-activity"
            await pilot.pause()
            assert not workspace.query("#tab-activity WorkstreamPanel")
            workspace.query_one("#pane-tabs", TabbedContent).active = "tab-timeline"
            await pilot.pause()
            assert not workspace.query("#tab-timeline WorkstreamPanel")
            finish(app, workspace)

        from ui_support import run_headless

        run_headless(app, scenario, size=(140, 40))

    def test_workstream_never_takes_focus_and_keys_still_navigate(self, tmp_path):
        app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
        stream = base_stream()

        async def scenario(pilot):
            workspace = push_live(app, stream)
            await pilot.pause()
            panel = workspace.query_one("#source-workstream", WorkstreamPanel)
            assert panel.can_focus is False
            await pilot.press("right")
            await pilot.pause()
            tabs = workspace.query_one("#pane-tabs", TabbedContent)
            assert tabs.active == "tab-debugger"
            await pilot.press("left")
            await pilot.pause()
            assert tabs.active == "tab-source"
            finish(app, workspace)

        from ui_support import run_headless

        run_headless(app, scenario, size=(140, 40))

    def test_replay_shows_workstream_without_live_liveness(self, tmp_path):
        root = Path(tmp_path)
        session_dir = root / "runs" / "sess-workstream-replay"
        session_dir.mkdir(parents=True)
        journal = SessionEventJournal(
            session_dir / "session.events.jsonl",
            session_id="sess-workstream-replay",
            task_id=L32_TASK,
            source_kind=SourceKind.LEVEL32_OPERATOR,
        )
        emitter = SessionEventEmitter(
            session_id="sess-workstream-replay",
            task_id=L32_TASK,
            source_kind=SourceKind.LEVEL32_OPERATOR,
            clock=lambda: CLOCK,
            sink=journal,
        )
        stream = base_stream()
        applied_candidate(stream, with_patch_text=True)
        for index, event in enumerate(stream.events):
            if index == 1:
                emitter.bind_run_id("run-w")
            emitter.emit(event.event_kind, event.to_mapping()["payload"])
        journal.close()
        store = HistoryStore(root)
        store.register(session_dir)
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            app.open_session("sess-workstream-replay")
            await pilot.pause()
            workspace = app.screen
            workspace.controller.seek(max(1, workspace.controller.total_events - 3))
            workspace._render_all()
            await pilot.pause()
            body = pane_text(workspace, "#source-workstream")
            # replay keeps the change preview and work units…
            assert "RECENT" in body
            # …but never live liveness chrome
            assert "LIVE ·" not in body

        from ui_support import run_headless

        run_headless(app, scenario, size=(140, 40))
