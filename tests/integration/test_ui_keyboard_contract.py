"""Focused provider-free tests for the application keyboard contract."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("textual")

from agentic_debugger.application.history import HistoryStore
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import (
    ChoicePickerScreen,
    HomeScreen,
    StartSessionScreen,
    TimeLimitEditorScreen,
    WorkspaceScreen,
)
from agentic_debugger.ui.screens import REPLAY_FOOTER, WORKSPACE_FOOTER_ACTIVE

from ui_support import make_rich_stream, populate_history, run_headless


def _app(tmp_path: Path) -> LocalApplicationV1:
    return LocalApplicationV1(history_store=HistoryStore(tmp_path))


async def _open_workspace(pilot) -> WorkspaceScreen:
    await pilot.press("escape")
    await pilot.press("enter")
    assert isinstance(pilot.app.screen, WorkspaceScreen)
    return pilot.app.screen


def test_ctrl_c_exits_from_new_session(tmp_path: Path) -> None:
    async def scenario(pilot) -> None:
        assert isinstance(pilot.app.screen, StartSessionScreen)
        await pilot.press("ctrl+c")
        assert pilot.app.is_running is False

    run_headless(_app(tmp_path), scenario)


def test_ctrl_c_active_run_requests_cleanup_before_exit(tmp_path: Path) -> None:
    app = _app(tmp_path)

    class ActiveRunner:
        def __init__(self) -> None:
            self.cancel_requested = False
            self.cleanup_complete = False
            self.worker_alive = True

        def close(self) -> None:
            self.cancel_requested = True
            self.worker_alive = False
            self.cleanup_complete = True

    active = ActiveRunner()
    app._live_runner = active  # type: ignore[assignment]

    async def scenario(pilot) -> None:
        await pilot.press("ctrl+c")
        assert active.cancel_requested is True
        assert active.cleanup_complete is True
        assert active.worker_alive is False
        assert pilot.app.is_running is False

    run_headless(app, scenario)


def test_global_history_and_new_session_reach_timeline_focus(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    populate_history(store, "sess.keyboard.timeline", events=make_rich_stream("sess.keyboard.timeline"))
    app = LocalApplicationV1(history_store=store)

    async def scenario(pilot) -> None:
        workspace = await _open_workspace(pilot)
        tabs = workspace.query_one("#pane-tabs")
        timeline = workspace.query_one("#timeline-pane")
        tabs.active = "tab-timeline"
        timeline.focus()

        await pilot.press("n")
        assert isinstance(pilot.app.screen, StartSessionScreen)
        await pilot.press("escape")
        assert isinstance(pilot.app.screen, WorkspaceScreen)

        workspace = pilot.app.screen
        workspace.query_one("#timeline-pane").focus()
        await pilot.press("h")
        assert isinstance(pilot.app.screen, HomeScreen)

    run_headless(app, scenario)


def test_global_workspace_routing_and_text_entry_contract(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    populate_history(store, "sess.keyboard.routing", events=make_rich_stream("sess.keyboard.routing"))
    app = LocalApplicationV1(history_store=store)

    async def scenario(pilot) -> None:
        workspace = await _open_workspace(pilot)
        tabs = workspace.query_one("#pane-tabs")
        panes = (
            ("tab-source", "#source-pane"),
            ("tab-debugger", "#debugger-pane"),
            ("tab-patch", "#patch-pane"),
            ("tab-verifier", "#verifier-pane"),
            ("tab-activity", "#activity-pane"),
            ("tab-timeline", "#timeline-pane"),
        )
        for tab_id, pane_id in panes:
            tabs.active = tab_id
            workspace.query_one(pane_id).focus()
            await pilot.pause()
            await pilot.press("right")
            assert tabs.active != tab_id
            await pilot.press("left")
            tabs.active = tab_id
            workspace.query_one(pane_id).focus()
            await pilot.press("5")

        await pilot.press("n")
        assert isinstance(pilot.app.screen, StartSessionScreen)
        await pilot.press("escape")

        # The modal owns ordinary text input; global navigation does not steal
        # letters typed into its editor.
        start = pilot.app.screen
        assert isinstance(start, WorkspaceScreen)
        await pilot.press("n")
        assert isinstance(pilot.app.screen, StartSessionScreen)
        await pilot.press("enter")
        assert isinstance(pilot.app.screen, ChoicePickerScreen)
        await pilot.press("escape")

    run_headless(app, scenario)


@pytest.mark.parametrize(
    ("tab_id", "pane_id"),
    [
        ("tab-source", "#source-pane"),
        ("tab-debugger", "#debugger-pane"),
        ("tab-patch", "#patch-pane"),
        ("tab-verifier", "#verifier-pane"),
        ("tab-activity", "#activity-pane"),
        ("tab-timeline", "#timeline-pane"),
    ],
)
def test_ctrl_c_exits_from_every_workspace_view(
    tmp_path: Path, tab_id: str, pane_id: str
) -> None:
    store = HistoryStore(tmp_path)
    populate_history(store, f"sess.keyboard.quit.{tab_id}")
    app = LocalApplicationV1(history_store=store)

    async def scenario(pilot) -> None:
        workspace = await _open_workspace(pilot)
        tabs = workspace.query_one("#pane-tabs")
        tabs.active = tab_id
        workspace.query_one(pane_id).focus()
        await pilot.pause()
        await pilot.press("ctrl+c")
        assert pilot.app.is_running is False

    run_headless(app, scenario)


def test_workspace_footers_do_not_advertise_q_or_replay_cancel(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    populate_history(store, "sess.keyboard.footer")
    app = LocalApplicationV1(history_store=store)

    async def scenario(pilot) -> None:
        workspace = await _open_workspace(pilot)
        footer = workspace.query_one("#replay-bar").render().plain
        assert "ctrl+c quit" in footer
        assert "h history" in footer and "n new session" in footer
        assert "q history" not in footer
        assert "c cancel" not in footer
        assert "events" in REPLAY_FOOTER
        assert "c cancel" in WORKSPACE_FOOTER_ACTIVE

    run_headless(app, scenario)


def test_text_entry_keeps_letter_keys_for_the_focused_editor(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def scenario(pilot) -> None:
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)
        start._task_id = "curated-off-by-one-002"
        start._refresh_mode()
        start._focus_row("time_limit")
        await pilot.press("enter")
        assert isinstance(pilot.app.screen, TimeLimitEditorScreen)
        editor = pilot.app.screen.query_one("#time-limit-editor")
        editor.focus()
        await pilot.press("1", "2", "3")
        assert editor.value == "123"
        await pilot.press("escape")

    run_headless(app, scenario)
