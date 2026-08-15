"""Adversarial UI tests for the Local Application V1.

Actively tries to break the application: resize during live runs, returning
home while a live session runs, history vanishing between selection and
open, unresolved-verifier presentation, multiple patch attempts, late
duplicate event delivery after a terminal, and the UI/journal sequence
boundary (no Textual code may write the journal or construct events).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest

textual = pytest.importorskip("textual")

from agentic_debugger.application.events import (
    SessionEventKind,
    SourceKind,
    validate_session_event_stream,
)
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    SessionViewState,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.application.session import SessionStatus
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import HomeScreen, WorkspaceMode, WorkspaceScreen
from agentic_debugger.ui.widgets import LiveBar, PatchPanel, StatusHeader, VerifierPanel

from textual.widgets import Static as _Static

from ui_support import (
    VALID_TASK_ID,
    make_rich_stream,
    populate_history,
    renumber,
    run_headless,
)


def pane_text(workspace, selector: str) -> str:
    """Plain text of a pane's inner Static (the pane is a scroller)."""
    pane = workspace.query_one(selector)
    static = pane.query_one(_Static)
    rendered = static.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)

TASK_ID = "curated-off-by-one-002"
POLICY = "pdb-on-uncertainty"


def make_app(tmp_path: Path) -> LocalApplicationV1:
    return LocalApplicationV1(history_store=HistoryStore(tmp_path))


async def wait_until(
    pilot,
    predicate,
    timeout_seconds: float = 180.0,
    interval: float = 0.2,
    label: str = "",
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:
            pass
        await pilot.pause()
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within the timeout: {label}")


class TestLiveAdversarial:
    def test_resize_while_live_keeps_events_flowing(self, tmp_path):
        app = make_app(tmp_path)

        async def scenario(pilot):
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            workspace = pilot.app.screen
            await wait_until(
                pilot,
                lambda: len(workspace._live_events) >= 4,
                label="live-events",
            )
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            await pilot.resize_terminal(160, 50)
            await pilot.pause()
            # events keep arriving after the resizes
            count = len(workspace._live_events)
            await wait_until(
                pilot,
                lambda: len(workspace._live_events) > count,
                label="live-events-after-resize",
            )
            await wait_until(
                pilot,
                lambda: workspace._live_terminal is not None,
                label="live-terminal",
            )

        run_headless(app, scenario, size=(120, 40))

    def test_back_home_while_live_does_not_cancel_session(self, tmp_path):
        """A presentation disconnect never cancels the worker; the finished
        session registers into history and reopens."""
        app = make_app(tmp_path)

        async def scenario(pilot):
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            workspace = pilot.app.screen
            await wait_until(
                pilot,
                lambda: len(workspace._live_events) >= 4,
                label="live-events",
            )
            await pilot.press("q")
            assert isinstance(pilot.app.screen, HomeScreen)
            # the session keeps running in the background and registers
            await wait_until(
                pilot,
                lambda: pilot.app.screen.query_one("#history-table").row_count == 1,
                label="history-registered",
            )
            # the presentation model reaches its terminal state on the
            # event loop (registration happens on the runner thread first)
            await wait_until(
                pilot,
                lambda: app.live_view is not None
                and app.live_view.status.terminal,
                label="live-terminal-state",
            )
            # reopen the completed session from history and replay it
            await pilot.press("enter")
            replayed = pilot.app.screen
            assert replayed.mode is WorkspaceMode.REPLAY
            live_view = app.live_view
            replayed.action_replay_end()
            assert replayed.controller.view == live_view

        run_headless(app, scenario, size=(100, 30))

    def test_duplicate_event_delivery_after_terminal_is_benign(self, tmp_path):
        """Re-delivering an already-reduced prefix must not corrupt the
        presentation model (the sequence guard is the UI's catch-up rule)."""
        app = make_app(tmp_path)
        events = make_rich_stream("sess.adv.dup")
        identity = PresentationIdentity(
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
            session_id="sess.adv.dup",
        )
        view = initial_session_view(identity)
        for event in events:
            view = reduce_event(view, event)
        assert view.status.terminal

        async def scenario(pilot):
            app._live_view = view
            app._live_events = events
            app._live_last_sequence = events[-1].sequence
            # a straggler callback re-delivers the same prefix and even a
            # stale extra event; the sequence guard rejects it
            app._live_events_ui(events)
            app._live_events_ui(events + (events[-1],))
            assert app.live_view == view
            assert len(app.live_events()) == len(events)

        run_headless(app, scenario)


class TestReplayAdversarial:
    def test_history_vanishing_between_selection_and_open(self, tmp_path):
        store = HistoryStore(tmp_path)
        session_dir = populate_history(store, "sess.adv.vanished")
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            # the directory disappears between the listing and the open
            shutil.rmtree(session_dir)
            await pilot.press("enter")
            # stays on home with an error notification; no crash
            await pilot.pause()
            assert isinstance(pilot.app.screen, HomeScreen)

        run_headless(app, scenario)

    def test_applied_patch_with_unresolved_verifier_stays_applied(self, tmp_path):
        """An applied patch whose verifier never completed must never be
        presented as verified."""
        store = HistoryStore(tmp_path)
        events = make_rich_stream("sess.adv.unresolved")
        events = renumber(
            tuple(
                e
                for e in events
                if e.event_kind
                not in (
                    SessionEventKind.VERIFIER_COMPLETED,
                    SessionEventKind.VERIFIER_STARTED,
                    SessionEventKind.VERIFIER_STAGE_STARTED,
                    SessionEventKind.VERIFIER_STAGE_COMPLETED,
                )
            )
        )
        populate_history(store, "sess.adv.unresolved", events=events)
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await pilot.press("enter")
            workspace = pilot.app.screen
            workspace.action_replay_end()
            patch = pane_text(workspace, "#patch-pane")
            assert "APPLIED" in patch
            assert "VERIFIED" not in patch
            verifier = pane_text(workspace, "#verifier-pane")
            assert "No verifier result recorded" in verifier

        run_headless(app, scenario)

    def test_multiple_patch_attempts_render_distinctly(self, tmp_path):
        """A rejected attempt and a later applied attempt stay distinct."""
        store = HistoryStore(tmp_path)
        events = make_rich_stream("sess.adv.patches")
        # insert a rejected attempt before the accepted one (renumbered)
        from ui_support import make_event

        rejected = make_event(
            SessionEventKind.PATCH_REJECTED,
            {"attempt_index": 0, "rejection_reason": "malformed diff"},
            sequence=99,
            session_id="sess.adv.patches",
            run_id="run-test-001",
        )
        patched = []
        for event in events:
            if event.event_kind in (
                SessionEventKind.PATCH_PROPOSED,
                SessionEventKind.PATCH_APPLIED,
                SessionEventKind.PATCH_REVERTED,
            ):
                # shift the whole accepted-attempt lifecycle to index 1 so
                # the injected rejected attempt 0 stays distinct.  The event
                # is rebuilt through its strict public mapping round-trip
                # (the canonicalized payload uses tuples internally).
                mapping = event.to_mapping()
                mapping["payload"]["attempt_index"] = 1
                event = make_event(
                    event.event_kind,
                    dict(mapping["payload"]),
                    sequence=event.sequence,
                    session_id=event.session_id,
                    run_id=event.run_id,
                )
            patched.append(event)
        stream = renumber(tuple([*patched[:15], rejected, *patched[15:]]))
        validate_session_event_stream(stream)
        populate_history(store, "sess.adv.patches", events=stream)
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await pilot.press("enter")
            workspace = pilot.app.screen
            workspace.action_replay_end()
            patch = pane_text(workspace, "#patch-pane")
            assert "Attempt 0" in patch and "REJECTED" in patch
            assert "malformed diff" in patch
            # the accepted attempt was independently verified (verifier
            # completed), so its final stage is VERIFIED, never APPLIED==FIXED
            assert "Attempt 1" in patch and "VERIFIED" in patch

        run_headless(app, scenario)

    def test_terminal_result_arriving_with_another_focus(self, tmp_path):
        """The terminal may arrive while any pane has focus; the workspace
        still renders the terminal state (no focus dependency)."""
        app = make_app(tmp_path)

        async def scenario(pilot):
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            workspace = pilot.app.screen
            await wait_until(
                pilot,
                lambda: len(workspace._live_events) >= 4,
                label="live-events",
            )
            # move focus around while the run continues
            await pilot.press("tab", "tab", "tab", "down", "down")
            await wait_until(
                pilot,
                lambda: workspace._live_terminal is not None,
                label="live-terminal",
            )
            header = str(workspace.query_one("#status-header", StatusHeader).render())
            assert "succeeded" in header or "cancelled" in header

        run_headless(app, scenario)


class TestUiJournalBoundary:
    def test_ui_code_never_writes_the_journal_or_constructs_events(self):
        """The Textual package must not import the journal or construct
        ``SessionEvent`` values: it consumes only reduced presentation
        state and replay cursors."""
        import agentic_debugger.ui

        ui_root = Path(agentic_debugger.ui.__file__).resolve().parent
        for source in sorted(ui_root.rglob("*.py")):
            text = source.read_text(encoding="utf-8")
            assert "SessionEventJournal" not in text, source
            assert "from_mapping" not in text, source
            assert "read_session_journal" not in text, source
            assert "SessionWorkerProcess" not in text or source.name in (
                "models.py",
                "app.py",
            ), source

    def test_ui_never_calls_controller_pdb_patch_or_verifier(self):
        """No UI module may import the executable domain entry points."""
        import agentic_debugger.ui

        ui_root = Path(agentic_debugger.ui.__file__).resolve().parent
        forbidden = (
            "agentic_debugger.agent.controller",
            "agentic_debugger.runtime.pdb_session",
            "agentic_debugger.runtime.patcher",
            "agentic_debugger.evaluation.verifier",
        )
        for source in sorted(ui_root.rglob("*.py")):
            text = source.read_text(encoding="utf-8")
            for module in forbidden:
                assert module not in text, f"{source} imports {module}"
