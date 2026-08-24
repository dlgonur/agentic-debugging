"""Headless Textual tests for the Task 6 replay-first application.

These tests drive the real ``LocalApplicationV1`` app through Textual's
supported ``App.run_test()`` / Pilot headless facilities at representative
terminal sizes.  Replay tests never spawn workers and never execute
controller/PDB/patch/verifier code (one test proves that by replacing the
domain entry points with failing stubs).  Live-session tests live in
``test_ui_live.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

textual = pytest.importorskip("textual")

from agentic_debugger.application.history import HistoryClassification, HistoryStore
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    SessionViewState,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import (
    HomeScreen,
    ChoicePickerScreen,
    StartSessionScreen,
    TimeLimitEditorScreen,
    WorkspaceMode,
    WorkspaceScreen,
)
from agentic_debugger.ui.widgets import (
    ActivityPanel,
    DebuggerPanel,
    PatchPanel,
    ReplayBar,
    SourcePanel,
    StatusHeader,
    TimelinePanel,
    VerifierPanel,
)

from textual.widgets import DataTable, OptionList, Static

from ui_support import (
    VALID_TASK_ID,
    make_rich_stream,
    populate_history,
    renumber,
    run_headless,
)


def table_text(table: DataTable) -> str:
    """Plain text of every visible table cell (row-major)."""
    lines = []
    for row_index in range(table.row_count):
        cells = []
        for cell in table.get_row_at(row_index):
            cells.append(cell.plain if hasattr(cell, "plain") else str(cell))
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def pane_text(workspace, selector: str) -> str:
    """Plain text of a pane's inner Static (the pane is a scroller)."""
    pane = workspace.query_one(selector)
    static = pane.query_one(Static)
    return static.render().plain if hasattr(static.render(), "plain") else str(static.render())


def make_app(tmp_path: Path) -> LocalApplicationV1:
    store = HistoryStore(tmp_path)
    return LocalApplicationV1(history_store=store)


async def open_first_row(pilot) -> None:
    await pilot.press("escape")
    await pilot.press("enter")


class TestBootAndHome:
    def test_app_boots_to_new_session_screen(self, tmp_path):
        async def scenario(pilot):
            app = pilot.app
            assert isinstance(app.screen, StartSessionScreen)
            assert "New debugging session" in str(app.screen.query_one("#start-title").render())
            assert app.screen.query_one("#start-footer")
            await pilot.press("h")
            assert isinstance(app.screen, HomeScreen)
            await pilot.press("n")
            assert isinstance(app.screen, StartSessionScreen)

        run_headless(make_app(tmp_path), scenario, size=(80, 24))

    def test_new_session_is_flat_and_uses_one_shared_picker(self, tmp_path):
        async def scenario(pilot):
            start = pilot.app.screen
            assert isinstance(start, StartSessionScreen)
            assert start._selected_mode() == "deterministic"
            assert start._selected_policy() == "pdb-on-uncertainty"
            assert start.task_id is not None
            assert len(start.query("RadioSet")) == 0
            assert len(start.query("Select")) == 0
            assert start.query_one("#start-context").display is False
            assert "s start" in str(start.query_one("#start-footer").render())

            # Up/down move through rows, and Enter opens the shared picker.
            assert pilot.app.focused.row_key == "mode"
            await pilot.press("enter")
            assert isinstance(pilot.app.screen, ChoicePickerScreen)
            picker = pilot.app.screen
            assert picker.title == "Select execution mode"
            assert picker.query_one("#choice-picker-list").highlighted == 0
            hint = picker.query_one("#choice-picker-hint")
            assert "up/down navigate   enter select   esc cancel" in str(hint.render())
            assert hint.region.height >= 2
            assert hint.content_region.height >= 1
            assert "↑" not in str(hint.render()) and "↓" not in str(hint.render())
            await pilot.press("down", "enter")
            assert start._selected_mode() == "configured"
            assert start.query_one("#model-row").display is True

            # The configured model row uses the same picker API.
            await pilot.press("down", "enter")
            assert isinstance(pilot.app.screen, ChoicePickerScreen)
            assert pilot.app.screen.title == "Select model profile"
            await pilot.press("escape")
            assert isinstance(pilot.app.screen, StartSessionScreen)

            # Task and debugger are also shared pickers and preserve domain values.
            await pilot.press("down", "enter")
            assert pilot.app.screen.title == "Select task"
            old_task = start.task_id
            await pilot.press("down", "enter")
            assert start.task_id != old_task
            await pilot.press("down", "enter")
            assert pilot.app.screen.title == "Select debugger policy"
            await pilot.press("down", "enter")
            assert start._selected_policy() == "static-baseline"

            # Time limit uses the same modal language as the shared pickers.
            await pilot.press("down", "enter")
            assert isinstance(pilot.app.screen, TimeLimitEditorScreen)
            assert pilot.app.screen.query_one("#time-limit-dialog").region.height <= pilot.app.size.height
            pilot.app.screen.query_one("#time-limit-editor").value = "12"
            await pilot.press("enter")
            assert start._max_elapsed_seconds == 12
            assert isinstance(pilot.app.screen, StartSessionScreen)
            assert pilot.app.focused.row_key == "time_limit"

        run_headless(make_app(tmp_path), scenario, size=(80, 24))

    def test_new_session_focus_marker_transfers_without_alignment_shift(self, tmp_path):
        async def scenario(pilot):
            start = pilot.app.screen

            def row_text(selector: str) -> str:
                row = start.query_one(selector)
                return str(row.render())

            selectors = ("#mode-row", "#task-row", "#debugger-row", "#time-limit-row")
            labels = ("Mode", "Task", "Debugger", "Time limit")

            await pilot.pause()
            for selector, label in zip(selectors, labels):
                rendered = row_text(selector)
                assert rendered.index(label) == 2
                assert rendered.startswith("> " if selector == "#mode-row" else "  ")
            assert sum(row_text(selector).startswith("> ") for selector in selectors) == 1

            await pilot.press("down")
            await pilot.pause()
            for selector, label in zip(selectors, labels):
                rendered = row_text(selector)
                assert rendered.index(label) == 2
                assert rendered.startswith("> " if selector == "#task-row" else "  ")
            assert sum(row_text(selector).startswith("> ") for selector in selectors) == 1

            await pilot.press("down")
            await pilot.pause()
            assert row_text("#debugger-row").startswith("> ")
            assert row_text("#task-row").startswith("  ")

        run_headless(make_app(tmp_path), scenario, size=(80, 24))

    def test_time_limit_modal_validates_cancel_and_empty_values(self, tmp_path):
        async def scenario(pilot):
            start = pilot.app.screen
            await pilot.press("down", "down", "down", "enter")
            assert isinstance(pilot.app.screen, TimeLimitEditorScreen)
            editor = pilot.app.screen
            assert editor.query_one("#time-limit-dialog").region.height <= pilot.app.size.height

            editor.query_one("#time-limit-editor").value = "0"
            await pilot.press("enter")
            assert isinstance(pilot.app.screen, TimeLimitEditorScreen)
            assert "at least 1 second" in str(editor.query_one("#time-limit-error").render())
            await pilot.press("escape")
            assert start._max_elapsed_seconds is None
            assert pilot.app.focused.row_key == "time_limit"

            await pilot.press("enter")
            editor = pilot.app.screen
            editor.query_one("#time-limit-editor").value = "15"
            await pilot.press("enter")
            assert start._max_elapsed_seconds == 15
            assert pilot.app.focused.row_key == "time_limit"

            await pilot.press("enter")
            editor = pilot.app.screen
            editor.query_one("#time-limit-editor").value = ""
            await pilot.press("escape")
            assert start._max_elapsed_seconds == 15
            assert pilot.app.focused.row_key == "time_limit"

            await pilot.press("enter")
            editor = pilot.app.screen
            editor.query_one("#time-limit-editor").value = ""
            await pilot.press("enter")
            assert start._max_elapsed_seconds is None

        run_headless(make_app(tmp_path), scenario, size=(60, 20))

    def test_new_session_wide_context_and_picker_geometry(self, tmp_path):
        async def scenario(pilot):
            start = pilot.app.screen
            assert start.query_one("#start-context").display is True
            await pilot.press("enter")
            picker = pilot.app.screen
            assert isinstance(picker, ChoicePickerScreen)
            dialog = picker.query_one("#choice-picker-dialog")
            hint = picker.query_one("#choice-picker-hint")
            assert dialog.region.width <= pilot.app.size.width
            assert dialog.region.height <= pilot.app.size.height
            assert "up/down navigate   enter select   esc cancel" in str(hint.render())
            assert hint.region.height >= 2
            assert hint.content_region.height >= 1
            assert hint.region.y + hint.region.height <= pilot.app.size.height
            await pilot.press("escape")

        run_headless(make_app(tmp_path), scenario, size=(120, 40))

    def test_very_small_terminal_keeps_settings_and_actions_reachable(self, tmp_path):
        async def scenario(pilot):
            start = pilot.app.screen
            assert start.query_one("#start-context").display is False
            for selector in ("#mode-row", "#task-row", "#debugger-row", "#time-limit-row", "#start-footer"):
                widget = start.query_one(selector)
                assert widget.region.y >= 0
                assert widget.region.y + widget.region.height <= pilot.app.size.height
            footer = str(start.query_one("#start-footer").render())
            assert "s start" in footer and "up/down move" in footer and "q quit" in footer
            assert "↑" not in footer and "↓" not in footer
            await pilot.press("down", "down", "enter")
            assert isinstance(pilot.app.screen, ChoicePickerScreen)
            await pilot.press("escape")

        run_headless(make_app(tmp_path), scenario, size=(60, 20))

    def test_empty_history_shows_empty_state(self, tmp_path):
        async def scenario(pilot):
            await pilot.press("escape")
            app = pilot.app
            assert isinstance(app.screen, HomeScreen)
            empty = app.screen.query_one("#home-empty")
            assert empty.display is True
            assert "No sessions yet" in str(empty.render())

        run_headless(make_app(tmp_path), scenario)

    def test_populated_history_lists_sessions(self, tmp_path):
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.hist.rich")
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await pilot.press("escape")
            table = pilot.app.screen.query_one("#history-table")
            row_count = table.row_count
            assert row_count == 1
            empty = pilot.app.screen.query_one("#home-empty")
            assert empty.display is False
            # session id and honest classification are visible
            rendered = table_text(table)
            assert "sess.hist.rich" in rendered
            assert "complete" in rendered

        run_headless(app, scenario)

    def test_malformed_and_interrupted_history_presented_honestly(self, tmp_path):
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.bad", corrupt=True)
        populate_history(store, "sess.cut", interrupted=True)
        populate_history(store, "sess.good")

        async def scenario(pilot):
            await pilot.press("escape")
            table = pilot.app.screen.query_one("#history-table")
            rendered = table_text(table)
            assert "malformed" in rendered
            assert "interrupted" in rendered
            assert "complete" in rendered
            assert "malformed | sess.bad" in rendered
            assert "interrupted | sess.cut" in rendered
            assert "complete | sess.good" in rendered

        run_headless(make_app(tmp_path), scenario)

    def test_history_table_120_columns_shows_result_and_verifier(self, tmp_path):
        store = HistoryStore(tmp_path)
        populate_history(store, "sess-demo-professor-001")
        populate_history(store, "sess-demo-cancelled-002", interrupted=True)
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await pilot.press("escape")
            table = pilot.app.screen.query_one("#history-table")
            rendered = table_text(table)
            assert "complete" in rendered
            assert "interrupted" in rendered
            assert "succeeded" in rendered
            assert "RESOLVED" in rendered
            columns = [col.label.plain for col in table.columns.values()]
            assert columns == [
                "Record", "Session", "Task", "Source", "Started", "Duration",
                "Result", "Verifier",
            ]

        run_headless(app, scenario, size=(120, 35))


class TestOpenReplay:
    def test_open_completed_session_renders_all_panes(self, tmp_path):
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.replay.rich")
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await open_first_row(pilot)
            workspace = pilot.app.screen
            assert isinstance(workspace, WorkspaceScreen)
            assert workspace.mode is WorkspaceMode.REPLAY
            # header shows REPLAY identity and position
            header = str(workspace.query_one("#status-header", StatusHeader).render())
            assert "REPLAY" in header
            assert "0/28" in header
            assert "before first event" in header
            # replay to the end so every recorded fact is in the view
            await pilot.press("G")
            # source pane renders recorded source with the execution line
            source = pane_text(workspace, "#source-pane")
            assert "recent_window.py" in source
            assert "def recent_window" in source
            assert "sha256" in source
            # debugger pane: stack + locals + redaction marker
            debugger = pane_text(workspace, "#debugger-pane")
            assert "recent_window" in debugger
            assert "redacted: credential-shaped local name" in debugger
            # patch pane: applied attempt with patch text and the APPLIED!=FIXED note
            patch = pane_text(workspace, "#patch-pane")
            assert "APPLIED" in patch
            assert "recent_window.py" in patch
            assert "does not mean FIXED" in patch
            # verifier pane: stages and final authority
            verifier = pane_text(workspace, "#verifier-pane")
            assert "COMPLETED" in verifier
            assert "RESOLVED" in verifier
            assert "1/1" in verifier
            assert "correctness authority" in verifier
            # activity + timeline panes render recorded events
            activity = pane_text(workspace, "#activity-pane")
            assert "controller transition" in activity
            timeline = pane_text(workspace, "#timeline-pane")
            assert "session succeeded" in timeline
            assert "verifier completed" in timeline

        run_headless(app, scenario)

    def test_no_rich_markup_leaks_into_rendered_panes(self, tmp_path):
        """Every pane's ``Text.plain`` renders recorded facts, never style
        tags: markup is only ever supplied separately (or through the
        markup-aware Static API for trusted UI strings)."""
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.replay.plain")
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await open_first_row(pilot)
            workspace = pilot.app.screen
            await pilot.press("G")
            header = str(workspace.query_one("#status-header", StatusHeader).render())
            for selector in (
                "#source-pane",
                "#debugger-pane",
                "#patch-pane",
                "#verifier-pane",
                "#activity-pane",
                "#timeline-pane",
            ):
                rendered = pane_text(workspace, selector)
                for tag in ("[bold", "[dim", "[/", "[red", "[yellow", "[green", "\\["):
                    assert tag not in rendered, f"{selector} leaked {tag!r}"
            for tag in ("[bold", "[dim", "[/", "\\["):
                assert tag not in header, f"header leaked {tag!r}"
            assert "REPLAY" in header

        run_headless(app, scenario)

    def test_replay_position_tracks_navigation(self, tmp_path):
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.replay.nav")
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await open_first_row(pilot)
            workspace = pilot.app.screen
            bar = workspace.query_one("#replay-bar", ReplayBar)
            header = workspace.query_one("#status-header", StatusHeader)
            assert "0/28" in str(bar.render())
            # The prev/next key hints render their literal bracket glyphs
            # (no markup-escape artifacts like "[[/").
            bar_text = str(bar.render())
            assert "[ prev" in bar_text
            assert "[[/" not in bar_text
            # next events
            await pilot.press("]")
            assert "1/28" in str(bar.render())
            await pilot.press("]")
            await pilot.press("]")
            assert "3/28" in str(bar.render())
            # previous
            await pilot.press("[")
            assert "2/28" in str(bar.render())
            # end and begin
            await pilot.press("G")
            assert "28/28" in str(bar.render())
            header_text = str(header.render())
            assert "SUCCEEDED" in header_text
            assert "at end" in header_text
            await pilot.press("g")
            assert "0/28" in str(bar.render())
            # phase navigation: effective boundaries are 0,2,3,15,18
            await pilot.press("}")
            assert "2/28" in str(bar.render())
            await pilot.press("}")
            assert "3/28" in str(bar.render())
            await pilot.press("}")
            assert "15/28" in str(bar.render())
            await pilot.press("}")
            assert "18/28" in str(bar.render())
            await pilot.press("{")
            assert "15/28" in str(bar.render())

        run_headless(app, scenario)

    def test_source_rendering_matches_current_execution_line(self, tmp_path):
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.replay.line")
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await open_first_row(pilot)
            workspace = pilot.app.screen
            # No events reduced yet: no recorded source exists at the start.
            source = pane_text(workspace, "#source-pane")
            assert "Source snapshot not yet available" in source
            # Reduce through the source snapshot (sequence 13 -> index 14):
            # the recorded debugger location (line 25) is outside the
            # recorded snapshot, so the pane shows the location marker
            # rather than a fake highlight.
            for _ in range(15):
                await pilot.press("]")
            source = pane_text(workspace, "#source-pane")
            assert "recent_window.py" in source
            assert "recent_window.py:25" in source

        run_headless(app, scenario)

    def test_interrupted_session_replays_recorded_prefix(self, tmp_path):
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.replay.cut", interrupted=True)
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await open_first_row(pilot)
            workspace = pilot.app.screen
            assert isinstance(workspace, WorkspaceScreen)
            bar = workspace.query_one("#replay-bar", ReplayBar)
            assert "events" in str(bar.render())
            await pilot.press("G")
            header = str(workspace.query_one("#status-header", StatusHeader).render())
            # no terminal status is fabricated for the interrupted session
            assert "succeeded" not in header
            assert "interrupted" not in header

        run_headless(app, scenario)

    def test_malformed_session_cannot_be_opened(self, tmp_path):
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.replay.bad", corrupt=True)
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await open_first_row(pilot)
            assert isinstance(pilot.app.screen, HomeScreen)
            assert isinstance(pilot.app.screen, HomeScreen)

        run_headless(app, scenario)

    def test_source_not_recorded_state(self, tmp_path):
        store = HistoryStore(tmp_path)
        events = make_rich_stream("sess.replay.nosource")
        events = renumber(
            tuple(e for e in events if e.event_kind.value != "source.snapshot")
        )
        populate_history(store, "sess.replay.nosource", events=events)
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await open_first_row(pilot)
            workspace = pilot.app.screen
            await pilot.press("G")
            source = pane_text(workspace, "#source-pane")
            assert "NOT RECORDED" in source

        run_headless(app, scenario)

    def test_back_home_navigation(self, tmp_path):
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.replay.back")
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await open_first_row(pilot)
            assert isinstance(pilot.app.screen, WorkspaceScreen)
            await pilot.press("q")
            assert isinstance(pilot.app.screen, HomeScreen)
            # history still listed after returning home
            table = pilot.app.screen.query_one("#history-table")
            assert table.row_count == 1

        run_headless(app, scenario)

    def test_jump_to_sequence(self, tmp_path):
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.replay.jump")
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await open_first_row(pilot)
            await pilot.press("j")
            await pilot.press("1", "5")
            await pilot.press("enter")
            workspace = pilot.app.screen
            bar = workspace.query_one("#replay-bar", ReplayBar)
            assert "15/28" in str(bar.render())

        run_headless(app, scenario)


class TestTerminalSizes:
    def test_all_supported_sizes_keep_primary_navigation(self, tmp_path):
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.replay.size")
        for size in ((80, 24), (100, 30), (120, 40), (160, 50)):
            app = LocalApplicationV1(history_store=store)

            async def scenario(pilot, size=size):
                # home -> open -> navigate -> switch tabs -> back home
                await open_first_row(pilot)
                workspace = pilot.app.screen
                assert isinstance(workspace, WorkspaceScreen)
                await pilot.press("G")
                await pilot.press("]", "[", "g")
                # switch to the debugger tab and back
                await pilot.press("tab", "tab")
                await pilot.press("q")
                assert isinstance(pilot.app.screen, HomeScreen)

            run_headless(app, scenario, size=size)

    def test_resize_while_replaying(self, tmp_path):
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.replay.resize")
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await open_first_row(pilot)
            await pilot.press("]")
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            await pilot.press("]")
            await pilot.resize_terminal(160, 50)
            await pilot.pause()
            await pilot.press("G")
            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            await pilot.press("q")
            assert isinstance(pilot.app.screen, HomeScreen)

        run_headless(app, scenario)


class TestReplayExecutesNothing:
    def test_replay_never_invokes_executable_resources(self, tmp_path, monkeypatch):
        import agentic_debugger.agent.controller
        import agentic_debugger.evaluation.verifier
        import agentic_debugger.runtime.pdb_session
        import agentic_debugger.application.worker_process

        def _forbidden(*args, **kwargs):
            raise AssertionError("domain execution during replay")

        monkeypatch.setattr(
            agentic_debugger.agent.controller.DeterministicController, "run", _forbidden
        )
        monkeypatch.setattr(
            agentic_debugger.evaluation.verifier.EvaluationVerifier, "evaluate", _forbidden
        )
        monkeypatch.setattr(
            agentic_debugger.runtime.pdb_session.PdbSession, "start", _forbidden
        )
        monkeypatch.setattr(
            agentic_debugger.application.worker_process.SessionWorkerProcess,
            "start", _forbidden,
        )
        store = HistoryStore(tmp_path)
        populate_history(store, "sess.replay.safe")
        app = LocalApplicationV1(history_store=store)

        async def scenario(pilot):
            await open_first_row(pilot)
            await pilot.press("G")
            await pilot.press("g")
            for _ in range(30):
                await pilot.press("]")
            await pilot.press("[")
            await pilot.press("q")
            assert isinstance(pilot.app.screen, HomeScreen)

        run_headless(app, scenario)


class TestReplayPrefixParity:
    def test_ui_replay_state_matches_pure_reducer_fold(self, tmp_path):
        """The workspace's presentation at every cursor position equals the
        pure fold of the recorded prefix (UI navigation cannot diverge)."""
        store = HistoryStore(tmp_path)
        events = make_rich_stream("sess.replay.parity")
        populate_history(store, "sess.replay.parity", events=events)
        app = LocalApplicationV1(history_store=store)
        identity = PresentationIdentity(
            task_id=VALID_TASK_ID,
            source_kind=events[0].source_kind,
            session_id="sess.replay.parity",
        )

        async def scenario(pilot):
            await open_first_row(pilot)
            workspace = pilot.app.screen
            controller = workspace.controller
            for step in range(controller.total_events):
                await pilot.press("]")
                expected = initial_session_view(identity)
                for event in events[: controller.index]:
                    expected = reduce_event(expected, event)
                assert controller.view == expected
            # backward walk keeps parity too
            for _ in range(controller.total_events):
                await pilot.press("[")
                expected = initial_session_view(identity)
                for event in events[: controller.index]:
                    expected = reduce_event(expected, event)
                assert controller.view == expected

        run_headless(app, scenario)
