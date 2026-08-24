"""Task 7 UI integration gates: real deterministic live sessions in the TUI.

These headless tests start the real production deterministic worker source
from the application, watch the same presentation model update live, cancel
safely, quit without orphaning the worker, reopen the resulting session from
history, and prove final live/replay ``SessionViewState`` parity.

Each test runs the real worker (controller + PDB + PatchManager + verifier),
so the suite intentionally keeps the number of full runs small.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

import pytest

textual = pytest.importorskip("textual")

from agentic_debugger.application.events import SourceKind
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.application.process_tree import pid_is_alive
from agentic_debugger.application.session import SessionStatus
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import HomeScreen, WorkspaceMode, WorkspaceScreen
from agentic_debugger.ui.widgets import LiveBar, SourcePanel, StatusHeader

from textual.widgets import Static as _Static

from ui_support import run_headless


def pane_text(workspace, selector: str) -> str:
    """Plain text of a pane's inner Static (the pane is a scroller)."""
    pane = workspace.query_one(selector)
    static = pane.query_one(_Static)
    rendered = static.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)

TASK_ID = "curated-off-by-one-002"
POLICY = "pdb-on-uncertainty"


async def wait_until(
    pilot,
    predicate: Callable[[], bool],
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


def make_app(tmp_path: Path) -> LocalApplicationV1:
    return LocalApplicationV1(history_store=HistoryStore(tmp_path))


def live_bar_text(workspace: WorkspaceScreen) -> str:
    return str(workspace.query_one("#live-bar", LiveBar).render())


async def wait_live_terminal(pilot, workspace: WorkspaceScreen) -> None:
    await wait_until(
        pilot,
        lambda: workspace._live_terminal is not None,
        timeout_seconds=240.0,
    )


class TestLiveStartAndProgression:
    def test_start_live_session_progression_terminal_and_history(self, tmp_path):
        app = make_app(tmp_path)

        async def scenario(pilot):
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            workspace = pilot.app.screen
            assert isinstance(workspace, WorkspaceScreen)
            assert workspace.mode is WorkspaceMode.LIVE
            # header is LIVE-labelled with the identity
            await wait_until(
                pilot,
                lambda: "LIVE" in str(
                    workspace.query_one("#status-header", StatusHeader).render()
                ),
            )
            # real journal progression reaches the workspace
            await wait_until(
                pilot,
                lambda: len(workspace._live_events) >= 6,
                timeout_seconds=120.0,
                label="stage1-live-events",
            )
            # the event loop stays responsive during the live run
            await pilot.press("]", "[", "tab", "tab")
            await pilot.pause()
            assert isinstance(pilot.app.screen, WorkspaceScreen)
            # panes render real recorded facts once they exist
            await wait_until(
                pilot,
                lambda: "recent_window.py" in pane_text(workspace, "#source-pane"),
                timeout_seconds=240.0,
                label="stage2-source-pane",
            )
            # operational terminal arrives with real evidence
            await wait_live_terminal(pilot, workspace)
            bar = live_bar_text(workspace)
            assert "SUCCEEDED" in bar
            assert "cleanup verified: True" in bar
            live_view = app.live_view
            assert live_view is not None
            assert live_view.status is SessionStatus.SUCCEEDED
            assert live_view.cleanup_verified is True

            # completed run appears in app-owned history
            await pilot.press("q")
            assert isinstance(pilot.app.screen, HomeScreen)
            table = pilot.app.screen.query_one("#history-table")
            assert table.row_count == 1

            # reopen + replay the same session from history
            session_id = live_view.session_id
            assert session_id is not None
            await pilot.press("enter")
            workspace = pilot.app.screen
            assert workspace.mode is WorkspaceMode.REPLAY
            assert "REPLAY" in str(
                workspace.query_one("#status-header", StatusHeader).render()
            )
            await pilot.press("G")

            # final live/replay parity (domain presentation state)
            reopened = app.history_store.reopen(session_id)
            replay_view = initial_session_view(
                PresentationIdentity(
                    task_id=live_view.task_id,
                    source_kind=live_view.source_kind,
                    session_id=session_id,
                )
            )
            while True:
                event = reopened.replay.next_event()
                if event is None:
                    break
                replay_view = reduce_event(replay_view, event)
            assert replay_view == live_view

            # representative prefix parity through the app's own replay path
            prefix = max(3, len(app.live_events()) // 2)
            live_prefix_view = initial_session_view(
                PresentationIdentity(
                    task_id=live_view.task_id,
                    source_kind=live_view.source_kind,
                    session_id=session_id,
                )
            )
            for event in app.live_events()[:prefix]:
                live_prefix_view = reduce_event(live_prefix_view, event)
            reopened.replay.rewind()
            replay_prefix_view = initial_session_view(
                PresentationIdentity(
                    task_id=live_view.task_id,
                    source_kind=live_view.source_kind,
                    session_id=session_id,
                )
            )
            for _ in range(prefix):
                event = reopened.replay.next_event()
                assert event is not None
                replay_prefix_view = reduce_event(replay_prefix_view, event)
            assert replay_prefix_view == live_prefix_view

        run_headless(app, scenario, size=(120, 40))


class TestLiveCancellation:
    def test_cancel_live_session_waits_for_terminal_evidence(self, tmp_path):
        app = make_app(tmp_path)

        async def scenario(pilot):
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            workspace = pilot.app.screen
            assert workspace.mode is WorkspaceMode.LIVE
            # wait until real execution events are flowing
            await wait_until(
                pilot,
                lambda: len(workspace._live_events) >= 6,
                timeout_seconds=120.0,
                label="stage1-live-events",
            )
            worker_pid = app.live_runner.worker.pid
            assert worker_pid is not None
            await pilot.press("c")
            # the visible cancelling state appears immediately on the key
            # press; the terminal still waits for real worker evidence
            await wait_until(
                pilot,
                lambda: "cancel requested" in live_bar_text(workspace),
                timeout_seconds=30.0,
                label="stage2-cancel-requested",
            )
            # the terminal only arrives with real worker evidence
            await wait_live_terminal(pilot, workspace)
            bar = live_bar_text(workspace)
            assert "cancelled" in bar
            assert "cleanup verified: True" in bar
            live_view = app.live_view
            assert live_view is not None
            assert live_view.status is SessionStatus.CANCELLED
            assert live_view.cleanup_verified is True
            # the recorded evidence shows cancel_requested before the terminal
            kinds = [e.event_kind.value for e in app.live_events()]
            assert "session.cancel_requested" in kinds
            assert kinds[-1] == "session.cancelled"
            # the worker process is gone
            await wait_until(
                pilot,
                lambda: not pid_is_alive(worker_pid),
                timeout_seconds=60.0,
                label="stage4-worker-gone",
            )

        run_headless(app, scenario, size=(100, 30))


class TestAppExitDuringLive:
    def test_quit_during_live_run_leaves_no_worker(self, tmp_path):
        """App teardown must never strand the live worker process."""
        app = make_app(tmp_path)

        async def scenario() -> None:
            async with app.run_test(size=(80, 24)) as pilot:
                app.start_live_session(
                    task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
                )
                workspace = pilot.app.screen
                assert workspace.mode is WorkspaceMode.LIVE
                await wait_until(
                    pilot,
                    lambda: len(workspace._live_events) >= 4,
                    timeout_seconds=120.0,
                )
                worker_pid = app.live_runner.worker.pid
                assert worker_pid is not None
                # quit the app while the live worker exists; the app's
                # shutdown path cancels, waits bounded, and releases handles
                app.exit()
            # after run_test fully shuts the app down, no worker survives
            assert app.live_runner is None
            assert pid_is_alive(worker_pid) is False

        asyncio.run(scenario())


class TestStartSessionScreen:
    def test_start_session_screen_starts_real_session(self, tmp_path):
        app = make_app(tmp_path)

        async def scenario(pilot):
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
                label="stage0-start-screen",
            )
            start_screen = pilot.app.screen
            task_select = start_screen.query_one("#task-select")
            assert len(task_select._options) == len(pilot.app.curated_task_options())
            assert len(task_select._options) >= 5  # canonical curated catalog
            # choose a task + policy through the widget API (user-equivalent)
            from textual.widgets import Select

            start_screen.query_one("#task-select", Select).value = TASK_ID
            start_screen.query_one("#policy-select", Select).value = POLICY
            await pilot.click("#start-button")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
                label="stage3-workspace",
            )
            workspace = pilot.app.screen
            assert workspace.mode is WorkspaceMode.LIVE
            await wait_until(
                pilot,
                lambda: len(workspace._live_events) >= 4,
                timeout_seconds=120.0,
                label="stage1-live-events",
            )
            # clean up: cancel + wait for the honest terminal
            await pilot.press("c")
            await wait_live_terminal(pilot, workspace)
            assert "cancelled" in live_bar_text(workspace) or "succeeded" in live_bar_text(workspace)

        run_headless(app, scenario, size=(100, 30))

    def test_escape_before_launch_returns_to_home(self, tmp_path):
        """Back/escape from StartSessionScreen before launching returns to
        Home normally (the form is only replaced after a successful start)."""
        app = make_app(tmp_path)

        async def scenario(pilot):
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
                label="stage0-start-screen",
            )
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
                label="stage1-home",
            )

        run_headless(app, scenario, size=(100, 30))

    def test_launch_replaces_form_and_sequential_real_sessions(self, tmp_path):
        """Blocker 2 + Blocker 1 through the REAL start UX.

        Home -> n -> StartSessionScreen -> Start -> LIVE Workspace; after the
        run finishes, q returns to Home (never to the stale start form), the
        registered run is in history, and a second real session starts from
        that same Home.  Both sessions get distinct ids.
        """
        from textual.widgets import Select

        app = make_app(tmp_path)

        async def scenario(pilot):
            def open_start_form():
                start_screen = pilot.app.screen
                start_screen.query_one("#task-select", Select).value = TASK_ID
                start_screen.query_one("#policy-select", Select).value = POLICY

            # -- session 1: complete through the real start UX -------------
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
                label="stage0-start-screen",
            )
            open_start_form()
            await pilot.click("#start-button")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
                label="stage1-workspace",
            )
            workspace1 = pilot.app.screen
            assert workspace1.mode is WorkspaceMode.LIVE
            # The launch replaced the start form: the stack below the
            # workspace is Home, not StartSessionScreen.
            assert isinstance(pilot.app._screen_stack[-2], HomeScreen)
            await wait_live_terminal(pilot, workspace1)
            assert "SUCCEEDED" in live_bar_text(workspace1)
            session1_id = app.live_view.session_id
            assert session1_id is not None
            # q returns to Home (the app says "q returns to history").
            await pilot.press("q")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
                label="stage2-home",
            )
            assert pilot.app.screen.__class__.__name__ == "HomeScreen"
            # the completed run is registered in app-owned history
            table = pilot.app.screen.query_one("#history-table")
            assert table.row_count == 1

            # -- session 2: start again from the same Home, then cancel ----
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
                label="stage3-start-screen",
            )
            open_start_form()
            await pilot.click("#start-button")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
                label="stage4-workspace",
            )
            workspace2 = pilot.app.screen
            assert workspace2.mode is WorkspaceMode.LIVE
            assert workspace2 is not workspace1
            await wait_until(
                pilot,
                lambda: len(workspace2._live_events) >= 4,
                timeout_seconds=120.0,
                label="stage5-live-events",
            )
            await pilot.press("c")
            await wait_live_terminal(pilot, workspace2)
            assert "CANCELLED" in live_bar_text(workspace2)
            session2_id = app.live_view.session_id
            assert session2_id is not None
            assert session2_id != session1_id
            await pilot.press("q")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
                label="stage6-home",
            )
            # both sequential sessions are in history with distinct ids
            table = pilot.app.screen.query_one("#history-table")
            assert table.row_count == 2
            ids = [entry.session_id for entry in app.history_store.list_sessions()]
            assert session1_id in ids and session2_id in ids
            assert len(set(ids)) == 2

        run_headless(app, scenario, size=(100, 30))
