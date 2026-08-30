"""Task 8 UI gates: configured command-model mode in the Textual TUI.

Headless Textual/Pilot scenarios over the real configured worker source:

- the Start screen offers both supported modes; invalid configuration never
  crashes the TUI and Start is disabled with a clear reason when no valid
  configured profile exists;
- a configured session starts through the real start UX, completes honestly,
  registers into app-owned history, replays read-only without re-running
  the command, and achieves live/replay presentation parity;
- cancellation interrupts the active command promptly and the terminal
  waits for worker evidence;
- sequential mixed-mode sessions (configured -> deterministic) work in one
  application lifetime with distinct identities;
- quitting the app during an active configured request with a descendant
  leaves no process behind;
- a configured failure is a professional application state (return to
  history, start another session).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Callable, Optional

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
from agentic_debugger.ui.screens import (
    ChoicePickerScreen,
    HistoryScreen,
    HomeScreen,
    StartSessionScreen,
    WorkspaceMode,
    WorkspaceScreen,
)
from agentic_debugger.ui.widgets import LiveBar, StatusHeader

from textual.widgets import Static as _Static

from ui_support import run_headless

from test_configured_source import (
    FIXTURE,
    TASK_ID,
    PDB_POLICY,
    write_profile,
    write_task_data,
)


def pane_text(workspace, selector: str) -> str:
    pane = workspace.query_one(selector)
    static = pane.query_one(_Static)
    rendered = static.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


def live_bar_text(workspace: WorkspaceScreen) -> str:
    return str(workspace.query_one("#live-bar", LiveBar).render())


def header_text(workspace: WorkspaceScreen) -> str:
    return str(workspace.query_one("#status-header", StatusHeader).render())


async def wait_until(
    pilot,
    predicate: Callable[[], bool],
    timeout_seconds: float = 240.0,
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


def _select_configured(start, task_id: str = TASK_ID, profile: str = "dummy") -> None:
    """Configure a configured-command-model session through the real seams."""
    start._choice_selected("model", f"configured:{profile}")
    start._choice_selected("task", task_id)
    start._choice_selected("debugger", PDB_POLICY)


def _select_offline(start, task_id: str = TASK_ID) -> None:
    """Configure the provider-free offline session through the real seams."""
    start._choice_selected("model", "offline:")
    start._choice_selected("task", task_id)
    start._choice_selected("debugger", PDB_POLICY)



@pytest.fixture(autouse=True)
def _non_ladder_task_options(monkeypatch):
    """Restore the non-ladder task option these configured-mode tests need.

    At the current accepted HEAD the Start picker exposes only the
    capability-ladder tasks, so the configured/deterministic UI modes cannot
    be reached through the product picker and the default screen opens on a
    ladder task (mode row hidden, configured start unavailable).  These tests
    protect the configured-mode screen machinery itself, so they restore the
    non-ladder task option through the app's task-options seam.
    """
    from agentic_debugger.ui.app import LocalApplicationV1

    def _options(self):
        return ((f"Configured fixture - {TASK_ID}", TASK_ID),)

    monkeypatch.setattr(LocalApplicationV1, "curated_task_options", _options)


class TestConfiguredStartScreen:
    def test_no_profiles_leaves_offline_ready_and_picker_honest(self, tmp_path):
        async def scenario(pilot):
            await pilot.press("s")
            start = pilot.app.screen
            # With no configured profiles the unified picker simply offers
            # no custom-profile entries; the offline default stays ready.
            assert start.start_available is True
            start._open_model_picker()
            await pilot.pause()
            picker = pilot.app.screen
            assert isinstance(picker, ChoicePickerScreen)
            assert not [
                choice for choice in picker.choices
                if choice.value.startswith("configured:") and not choice.disabled
            ]
            await pilot.press("escape")

        run_headless(make_app(tmp_path), scenario, size=(80, 24))

    def test_malformed_config_does_not_crash_the_tui(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command-models.json").write_text("{not-json", encoding="utf-8")

        async def scenario(pilot):
            await pilot.press("s")
            start = pilot.app.screen
            start._open_model_picker()
            await pilot.pause()
            picker = pilot.app.screen
            assert isinstance(picker, ChoicePickerScreen)
            error_entry = next(
                (
                    choice
                    for choice in picker.choices
                    if choice.title == "Configuration error"
                ),
                None,
            )
            assert error_entry is not None
            assert error_entry.disabled is True
            assert "configuration" in str(error_entry.disabled_reason).casefold()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(pilot.app.screen, StartSessionScreen)
            await pilot.press("escape")
            assert isinstance(pilot.app.screen, HomeScreen)

        run_headless(make_app(tmp_path), scenario, size=(80, 24))

    def test_config_load_error_is_safe_and_bounded(self, tmp_path):
        secret = "API_KEY=supersecret-value"
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command-models.json").write_text(
            json.dumps({"schema_version": secret, "profiles": []}), encoding="utf-8"
        )

        async def scenario(pilot):
            await pilot.press("s")
            start = pilot.app.screen
            start._open_model_picker()
            await pilot.pause()
            picker = pilot.app.screen
            error_entry = next(
                (
                    choice
                    for choice in picker.choices
                    if choice.title == "Configuration error"
                ),
                None,
            )
            assert error_entry is not None
            reason = error_entry.disabled_reason
            assert "unsupported command-model configuration version" in reason
            assert secret not in reason
            assert len(reason.encode("utf-8")) < 200

        run_headless(make_app(tmp_path), scenario, size=(80, 24))

    def test_profiles_are_available_through_the_shared_picker(self, tmp_path):
        write_profile(tmp_path, "dummy", "valid")

        async def scenario(pilot):
            await pilot.press("s")
            start = pilot.app.screen
            start._open_model_picker()
            await pilot.pause()
            assert isinstance(pilot.app.screen, ChoicePickerScreen)
            picker = pilot.app.screen
            assert picker.title == "Select model"
            assert "configured:dummy" in [choice.value for choice in picker.choices]
            picker._on_select("configured:dummy")
            pilot.app.pop_screen()
            await pilot.pause()
            assert start.profile_id == "dummy"
            assert start.start_available is True
            model_row = str(start.query_one("#model-row").render())
            assert "Dummy command model" in model_row

        run_headless(make_app(tmp_path), scenario, size=(100, 30))

    def test_trust_boundary_wording_is_truthful_per_selection(self, tmp_path):
        write_profile(tmp_path, "dummy", "valid")

        async def scenario(pilot):
            await pilot.press("s")
            start = pilot.app.screen
            assert "network isolation" not in str(start.query_one("#start-notes").render())
            start._choice_selected("model", "configured:dummy")
            await pilot.pause()
            notes = str(start.query_one("#start-notes").render())
            assert "trusted user configuration" in notes
            assert "network isolation" in notes
            assert "network-isolated" not in notes

        run_headless(make_app(tmp_path), scenario, size=(80, 24))

    def test_local_project_presents_custom_command_profile_provider_label(self, tmp_path):
        write_profile(tmp_path, "dummy", "valid")

        async def scenario(pilot):
            app = pilot.app
            screen = StartSessionScreen(
                task_options=list(app.curated_task_options()),
                initial_target="local_project",
                initial_project=str(tmp_path),
            )
            app.push_screen(screen)
            await pilot.pause()
            # Select the custom command profile on the same unified surface
            screen._choice_selected("model", "configured:dummy")
            await pilot.pause()
            context = screen.query_one("#context-summary").render().plain
            assert "Provider\nCustom command profile" in context
            assert "Model\nDummy command model" in context

        run_headless(make_app(tmp_path), scenario, size=(100, 30))


class TestConfiguredLiveSession:
    def test_configured_session_completes_replays_and_is_parity(self, tmp_path):
        data_file = write_task_data(tmp_path)
        state_dir = tmp_path / "state-dummy"
        write_profile(
            tmp_path, "dummy", "valid", extra_argv=("--data", str(data_file))
        )
        app = make_app(tmp_path)

        async def scenario(pilot):
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start = pilot.app.screen
            _select_configured(start)
            await pilot.pause()
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            workspace = pilot.app.screen
            assert workspace.mode is WorkspaceMode.LIVE
            # provenance arrives through the shared journal
            await wait_until(
                pilot,
                lambda: workspace._view.model_provenance is not None
                and workspace._view.model_provenance.display_name == "Dummy command model",
                timeout_seconds=120.0,
                label="stage1-provenance-evidence",
            )
            await wait_until(
                pilot,
                lambda: workspace._live_terminal is not None,
                timeout_seconds=300.0,
                label="stage2-terminal",
            )
            header = header_text(workspace)
            assert "Completed" in header
            assert "Succeeded" not in header
            assert "cleanup verified" in header
            live_view = app.live_view
            assert live_view is not None
            assert live_view.status is SessionStatus.SUCCEEDED
            assert live_view.model_provenance is not None
            assert live_view.model_provenance.profile_id == "dummy"
            session_id = live_view.session_id
            assert session_id is not None
            # recorded source facts are visible in the panes
            await wait_until(
                pilot,
                lambda: "recent_window.py" in pane_text(workspace, "#source-pane"),
                timeout_seconds=60.0,
                label="stage3-source-pane",
            )
            # return to history and replay read-only
            phase_before = (
                (state_dir / "phase.json").read_text(encoding="utf-8")
                if (state_dir / "phase.json").is_file()
                else None
            )
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            await pilot.press("h")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HistoryScreen),
                timeout_seconds=30.0,
            )
            table = pilot.app.screen.query_one("#history-table")
            assert table.row_count == 1
            await pilot.press("enter")
            workspace = pilot.app.screen
            assert workspace.mode is WorkspaceMode.REPLAY
            assert "REPLAY" in header_text(workspace)
            await pilot.press("G")
            verifier = pane_text(workspace, "#verifier-pane")
            assert "RESOLVED" in verifier
            assert "correctness authority" in verifier
            phase_after = (
                (state_dir / "phase.json").read_text(encoding="utf-8")
                if (state_dir / "phase.json").is_file()
                else None
            )
            assert phase_after == phase_before, "replay re-ran the configured command"
            # live/replay parity through the app's own replay path
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

        run_headless(app, scenario, size=(120, 40))

    def test_configured_failure_is_a_professional_state_then_retry(self, tmp_path):
        """Pass 2 Blocker 2: an invalid-response failure is a professional
        terminal state and does not break the TUI."""
        write_profile(tmp_path, "dummy", "malformed", timeout=10.0)
        app = make_app(tmp_path)

        async def scenario(pilot):
            def open_configured_start():
                start_screen = pilot.app.screen
                _select_configured(start_screen)

            async def settle_and_click():
                await pilot.pause()
                await pilot.press("s")

            # -- session 1: start the failing configured model -------------
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            open_configured_start()
            await settle_and_click()
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            workspace = pilot.app.screen
            await wait_until(
                pilot,
                lambda: workspace._live_terminal is not None,
                timeout_seconds=180.0,
                label="stage1-failed-terminal",
            )
            assert workspace._live_terminal.status is SessionStatus.FAILED
            assert workspace._live_terminal.termination_reason.value == "model_error"
            assert "Failed" in header_text(workspace)
            assert "c cancel" not in live_bar_text(workspace)
            assert "1-7 tabs" in live_bar_text(workspace)
            assert "activity filters" not in live_bar_text(workspace)
            # return to history; the failure registered honestly
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            assert len(app.history_store.list_sessions()) == 1
            # a second session can start after the failure
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            open_configured_start()
            await settle_and_click()
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            workspace2 = pilot.app.screen
            assert workspace2 is not workspace
            await wait_until(
                pilot,
                lambda: workspace2._live_terminal is not None,
                timeout_seconds=180.0,
                label="stage2-second-failed-terminal",
            )
            assert workspace2._live_terminal.status is SessionStatus.FAILED
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            # both sessions registered with distinct identities
            assert len(app.history_store.list_sessions()) == 2
            ids = [entry.session_id for entry in app.history_store.list_sessions()]
            assert len(set(ids)) == 2

        run_headless(app, scenario, size=(100, 30))


class TestConfiguredCancellation:
    def test_cancel_interrupts_configured_request(self, tmp_path):
        """Pass 2 Blocker 3: cancellation while a configured request is
        in flight sends SIGINT/SIGTERM to the child process and records
        the session as cancelled."""
        write_profile(
            tmp_path, "dummy", "slow", timeout=120.0, extra_argv=("--delay", "300")
        )
        app = make_app(tmp_path)

        async def scenario(pilot):
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start = pilot.app.screen
            _select_configured(start)
            await pilot.pause()
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            workspace = pilot.app.screen
            # Worker pid is set asynchronously after WorkspaceScreen appears;
            # wait until the subprocess has started before capturing the pid.
            await wait_until(
                pilot,
                lambda: app.live_runner is not None
                and app.live_runner.worker is not None
                and app.live_runner.worker.pid is not None,
                timeout_seconds=30.0,
                label="stage0-worker-started",
            )
            worker_pid = app.live_runner.worker.pid
            assert worker_pid is not None
            assert pid_is_alive(worker_pid)
            # wait until the model request starts
            await wait_until(
                pilot,
                lambda: len(workspace._live_events) >= 3,
                timeout_seconds=120.0,
                label="stage1-request-started",
            )
            await pilot.press("c")
            await wait_until(
                pilot,
                lambda: "cancel requested" in header_text(workspace),
                timeout_seconds=30.0,
                label="stage2-cancel-requested",
            )
            await wait_until(
                pilot,
                lambda: workspace._live_terminal is not None,
                timeout_seconds=120.0,
                label="stage3-terminal",
            )
            assert workspace._live_terminal.status is SessionStatus.CANCELLED
            assert "Cancelled" in header_text(workspace)
            await wait_until(
                pilot,
                lambda: not pid_is_alive(worker_pid),
                timeout_seconds=60.0,
                label="stage4-worker-gone",
            )
            kinds = [e.event_kind.value for e in app.live_events()]
            assert "session.cancel_requested" in kinds
            assert kinds[-1] == "session.cancelled"

        run_headless(app, scenario, size=(100, 30))


class TestMixedSequentialSessions:
    def test_configured_then_deterministic_in_one_lifetime(self, tmp_path):
        data_file = write_task_data(tmp_path)
        write_profile(tmp_path, "dummy", "valid", extra_argv=("--data", str(data_file)))
        app = make_app(tmp_path)

        async def scenario(pilot):
            # -- session 1: configured, successful --------------------------
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start = pilot.app.screen
            _select_configured(start)
            await pilot.pause()
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            workspace1 = pilot.app.screen
            await wait_until(
                pilot,
                lambda: "configured command model" in header_text(workspace1),
                label="stage1-configured-header",
            )
            await wait_until(
                pilot,
                lambda: workspace1._live_terminal is not None,
                timeout_seconds=300.0,
                label="stage1-configured-terminal",
            )
            assert "Completed" in header_text(workspace1)
            assert "Succeeded" not in header_text(workspace1)
            assert "c cancel" not in live_bar_text(workspace1)
            session1_id = app.live_view.session_id
            assert session1_id is not None
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            assert len(app.history_store.list_sessions()) == 1

            # -- session 2: deterministic, cancelled ------------------------
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start2 = pilot.app.screen
            _select_offline(start2)
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            workspace2 = pilot.app.screen
            assert workspace2 is not workspace1
            await wait_until(
                pilot,
                lambda: len(workspace2._live_events) >= 4,
                timeout_seconds=120.0,
                label="stage2-det-events",
            )
            await pilot.press("c")
            await wait_until(
                pilot,
                lambda: workspace2._live_terminal is not None,
                timeout_seconds=120.0,
                label="stage3-det-terminal",
            )
            assert "Cancelled" in header_text(workspace2)
            session2_id = app.live_view.session_id
            assert session2_id != session1_id
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            # both sessions registered with distinct identities
            assert len(app.history_store.list_sessions()) == 2
            ids = [entry.session_id for entry in app.history_store.list_sessions()]
            assert session1_id in ids and session2_id in ids
            assert len(set(ids)) == 2

        run_headless(app, scenario, size=(100, 30))


class TestMixedSequentialSessionsExtra:
    """Blocker-F adversarial matrix: the remaining mixed-mode orderings.

    Complements ``TestMixedSequentialSessions`` (configured -> deterministic)
    and the failure/cancel retry tests with the two remaining orderings:
    deterministic -> configured, and configured cancel -> retry.
    """

    def test_deterministic_then_configured_in_one_lifetime(self, tmp_path):
        write_profile(tmp_path, "dummy", "malformed", timeout=10.0)
        app = make_app(tmp_path)

        async def scenario(pilot):
            # -- session 1: deterministic, cancelled ------------------------
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start = pilot.app.screen
            _select_offline(start)
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            workspace1 = pilot.app.screen
            assert "deterministic offline" in header_text(workspace1)
            await wait_until(
                pilot,
                lambda: len(workspace1._live_events) >= 4,
                timeout_seconds=120.0,
                label="stage1-det-events",
            )
            await pilot.press("c")
            await wait_until(
                pilot,
                lambda: workspace1._live_terminal is not None,
                timeout_seconds=120.0,
                label="stage2-det-terminal",
            )
            assert "Cancelled" in header_text(workspace1)
            session1_id = app.live_view.session_id
            assert session1_id is not None
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )

            # -- session 2: configured, honest failure ----------------------
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start2 = pilot.app.screen
            _select_configured(start2)
            await pilot.pause()
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            workspace2 = pilot.app.screen
            assert workspace2 is not workspace1
            assert "configured command model" in header_text(workspace2)
            await wait_until(
                pilot,
                lambda: workspace2._live_terminal is not None,
                timeout_seconds=180.0,
                label="stage3-configured-terminal",
            )
            assert workspace2._live_terminal.status is SessionStatus.FAILED
            session2_id = app.live_view.session_id
            assert session2_id is not None
            assert session2_id != session1_id
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            # both sessions registered with distinct identities
            assert len(app.history_store.list_sessions()) == 2
            ids = [entry.session_id for entry in app.history_store.list_sessions()]
            assert session1_id in ids and session2_id in ids
            assert len(set(ids)) == 2

        run_headless(app, scenario, size=(100, 30))

    def test_configured_cancel_then_retry_in_one_lifetime(self, tmp_path):
        write_profile(
            tmp_path, "dummy", "slow", timeout=120.0, extra_argv=("--delay", "300")
        )
        app = make_app(tmp_path)

        async def scenario(pilot):
            # -- session 1: configured, cancelled ---------------------------
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start = pilot.app.screen
            _select_configured(start)
            await pilot.pause()
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            workspace1 = pilot.app.screen
            await wait_until(
                pilot,
                lambda: len(workspace1._live_events) >= 3,
                timeout_seconds=120.0,
                label="stage1-request-active",
            )
            await pilot.press("c")
            await wait_until(
                pilot,
                lambda: workspace1._live_terminal is not None,
                timeout_seconds=120.0,
                label="stage2-cancel-terminal",
            )
            assert workspace1._live_terminal.status is SessionStatus.CANCELLED
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )

            # -- session 2: configured retry after the cancel ---------------
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start2 = pilot.app.screen
            _select_configured(start2)
            await pilot.pause()
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            workspace2 = pilot.app.screen
            assert workspace2 is not workspace1
            await wait_until(
                pilot,
                lambda: len(workspace2._live_events) >= 3,
                timeout_seconds=120.0,
                label="stage3-retry-active",
            )
            await pilot.press("c")
            await wait_until(
                pilot,
                lambda: workspace2._live_terminal is not None,
                timeout_seconds=120.0,
                label="stage4-retry-terminal",
            )
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            # both the cancelled session and the retry registered
            assert len(app.history_store.list_sessions()) == 2

        run_headless(app, scenario, size=(100, 30))


class TestConfiguredAdversarial:
    def test_profile_deleted_after_start_screen_opens_fails_cleanly(self, tmp_path):
        write_profile(tmp_path, "dummy", "valid")
        app = make_app(tmp_path)

        async def scenario(pilot):
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start = pilot.app.screen
            _select_configured(start)
            # the configuration changes between discovery and start
            (tmp_path / "config" / "command-models.json").unlink()
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            # still on the start screen with a clear error, never a crash
            assert pilot.app.screen.__class__.__name__ == "StartSessionScreen"
            error = str(start.query_one("#start-status").render())
            assert "!" in error
            assert "no longer offered" in error
            # the TUI remains usable: escape returns home
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )

        run_headless(app, scenario, size=(100, 30))

    def test_rapid_repeated_start_is_rejected_without_crash(self, tmp_path):
        # The start form is replaced by the workspace on launch, so a second
        # click cannot even reach the form; the app-owned guard additionally
        # rejects a second start while a live session is active.
        write_profile(tmp_path, "dummy", "slow", timeout=120.0, extra_argv=("--delay", "300"))
        app = make_app(tmp_path)

        async def scenario(pilot):
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start = pilot.app.screen
            _select_configured(start)
            await pilot.pause()
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            # a direct second start request is a clear error, never a crash
            import pytest as _pytest

            with _pytest.raises(RuntimeError):
                app.start_live_session(
                    task_id=TASK_ID,
                    policy=PDB_POLICY,
                    max_elapsed_seconds=None,
                    source_kind=SourceKind.CONFIGURED_MODEL,
                    profile_id="dummy",
                )
            workspace = pilot.app.screen
            await pilot.press("c")
            await wait_until(
                pilot,
                lambda: workspace._live_terminal is not None,
                timeout_seconds=120.0,
            )

        run_headless(app, scenario, size=(100, 30))

    def test_small_terminal_configured_failure_flow(self, tmp_path):
        """E8: the configured Start screen/errors must not break 80x24."""
        write_profile(tmp_path, "dummy", "malformed", timeout=10.0)
        app = make_app(tmp_path)

        async def scenario(pilot):
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start = pilot.app.screen
            _select_configured(start)
            await pilot.pause()
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            workspace = pilot.app.screen
            await wait_until(
                pilot,
                lambda: workspace._live_terminal is not None,
                timeout_seconds=180.0,
                label="stage1-small-terminal-failure",
            )
            assert "Failed" in header_text(workspace)
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            # replay the recorded failure at the compact size
            await pilot.press("h")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HistoryScreen),
                timeout_seconds=30.0,
            )
            await pilot.press("enter")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            assert "REPLAY" in header_text(pilot.app.screen)

        run_headless(app, scenario, size=(80, 24))


class TestAppExitDuringConfiguredRequest:
    def test_quit_leaves_no_worker_or_descendant(self, tmp_path):
        child_pid_file = tmp_path / "child.pid"
        write_profile(
            tmp_path,
            "dummy",
            "spawn_child",
            timeout=120.0,
            extra_argv=(
                "--child-pid-file",
                str(child_pid_file),
                "--delay",
                "300",
            ),
        )
        app = make_app(tmp_path)

        async def scenario() -> None:
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("n")
                await wait_until(
                    pilot,
                    lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                    timeout_seconds=30.0,
                )
                start = pilot.app.screen
                _select_configured(start)
                await pilot.pause()
                await pilot.press("s")
                await wait_until(
                    pilot,
                    lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                    timeout_seconds=30.0,
                )
                workspace = pilot.app.screen
                # Worker pid is set asynchronously after WorkspaceScreen appears;
                # wait until the subprocess has started before capturing the pid.
                await wait_until(
                    pilot,
                    lambda: app.live_runner is not None
                    and app.live_runner.worker is not None
                    and app.live_runner.worker.pid is not None,
                    timeout_seconds=30.0,
                    label="stage0-worker-started",
                )
                worker_pid = app.live_runner.worker.pid
                assert worker_pid is not None
                await wait_until(
                    pilot,
                    lambda: len(workspace._live_events) >= 3,
                    timeout_seconds=120.0,
                    label="stage1-request-active",
                )
                app.exit()
            # after full teardown: no worker, no command descendant
            assert app.live_runner is None
            assert pid_is_alive(worker_pid) is False
            deadline = time.monotonic() + 10.0
            child_pid = int(child_pid_file.read_text()) if child_pid_file.is_file() else None
            if child_pid is not None:
                while time.monotonic() < deadline and pid_is_alive(child_pid):
                    time.sleep(0.1)
                assert not pid_is_alive(child_pid), "command descendant survived app exit"

        asyncio.run(scenario())
