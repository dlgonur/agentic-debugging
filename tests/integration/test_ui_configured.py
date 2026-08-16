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
from agentic_debugger.ui.screens import HomeScreen, StartSessionScreen, WorkspaceMode, WorkspaceScreen
from agentic_debugger.ui.widgets import LiveBar, StatusHeader

from textual.widgets import Button, Select, Static as _Static

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


class TestConfiguredStartScreen:
    def test_no_profiles_disables_start_with_reason(self, tmp_path):
        app = make_app(tmp_path)

        async def scenario(pilot):
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start = pilot.app.screen
            start.query_one("#mode-select", Select).value = "configured"
            await pilot.pause()
            # no valid configured profile: Start disabled with a clear reason
            button = start.query_one("#start-button", Button)
            assert button.disabled is True
            info = str(start.query_one("#config-info").render())
            assert "no configured profiles" in info
            # switching back to deterministic re-enables Start
            start.query_one("#mode-select", Select).value = "deterministic"
            await pilot.pause()
            assert start.query_one("#start-button", Button).disabled is False

        run_headless(app, scenario, size=(100, 30))

    def test_malformed_config_does_not_crash_the_tui(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command-models.json").write_text("{not-json", encoding="utf-8")
        app = make_app(tmp_path)

        async def scenario(pilot):
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start = pilot.app.screen
            start.query_one("#mode-select", Select).value = "configured"
            await pilot.pause()
            info = str(start.query_one("#config-info").render())
            assert "configuration error" in info
            assert start.query_one("#start-button", Button).disabled is True
            # escape still returns home; the TUI is alive
            await pilot.press("escape")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )

        run_headless(app, scenario, size=(100, 30))

    def test_config_load_error_shows_safe_bounded_diagnostic(self, tmp_path):
        """Repair Pass 2 Blocker 1: a malformed config whose raw values are
        credential-shaped must surface only the safe bounded structural
        diagnostic on the Start screen — never the secret literal.
        """
        secret = "API_KEY=supersecret-value"
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command-models.json").write_text(
            json.dumps({"schema_version": secret, "profiles": []}),
            encoding="utf-8",
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
            start.query_one("#mode-select", Select).value = "configured"
            await pilot.pause()
            info = str(start.query_one("#config-info").render())
            assert "configuration error" in info
            # the safe structural diagnostic, never the raw secret
            assert "unsupported command-model configuration version" in info
            assert secret not in info
            assert "supersecret-value" not in info
            # the diagnostic stays within the explicit byte bound
            assert len(info.encode("utf-8")) < 1000
            assert start.query_one("#start-button", Button).disabled is True

        run_headless(app, scenario, size=(100, 30))

    def test_profiles_are_listed_with_safe_info(self, tmp_path):
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
            start.query_one("#mode-select", Select).value = "configured"
            await pilot.pause()
            select = start.query_one("#profile-select", Select)
            assert len(select._options) >= 1
            info = str(start.query_one("#config-info").render())
            assert "Dummy command model" in info
            assert "dummy" in info
            assert start.query_one("#start-button", Button).disabled is False

        run_headless(app, scenario, size=(100, 30))

    def test_trust_boundary_wording_is_truthful_per_mode(self, tmp_path):
        """Blocker F: configured mode must not promise network isolation.

        Deterministic mode keeps its truthful offline claim; configured mode
        must state that the child is trusted user configuration and that V1
        does not enforce child-process network isolation.  No umbrella
        "no network" promise may cover the configured command subprocess.
        """
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

            def description_text() -> str:
                # The mode-aware trust-boundary wording lives in the bottom
                # hint (below the Start button) so it never pushes the button
                # off-screen at the accepted compact 80x24 size.
                return str(start.query_one("#start-hint").render())

            # Deterministic mode: truthful offline claim.
            start.query_one("#mode-select", Select).value = "deterministic"
            await pilot.pause()
            det = description_text()
            assert "offline" in det
            assert "network isolation" not in det

            # Configured mode: must NOT promise network isolation; must state
            # the child is trusted user configuration and V1 does not enforce
            # child-process network isolation.
            start.query_one("#mode-select", Select).value = "configured"
            await pilot.pause()
            cfg = description_text()
            assert "trusted user configuration" in cfg
            assert "does not enforce" in cfg and "network isolation" in cfg
            # No false umbrella promise that the configured child is
            # network-isolated.
            assert "network-isolated" not in cfg

        run_headless(app, scenario, size=(100, 30))


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
            start.query_one("#mode-select", Select).value = "configured"
            start.query_one("#profile-select", Select).value = "dummy"
            start.query_one("#task-select", Select).value = TASK_ID
            start.query_one("#policy-select", Select).value = PDB_POLICY
            await pilot.pause()
            await pilot.click("#start-button")
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
                lambda: "model Dummy command model" in header_text(workspace),
                timeout_seconds=120.0,
                label="stage1-provenance-header",
            )
            await wait_until(
                pilot,
                lambda: workspace._live_terminal is not None,
                timeout_seconds=300.0,
                label="stage2-terminal",
            )
            bar = live_bar_text(workspace)
            assert "SUCCEEDED" in bar
            assert "cleanup verified: True" in bar
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
            await pilot.press("q")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
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
                start_screen.query_one("#mode-select", Select).value = "configured"
                start_screen.query_one("#profile-select", Select).value = "dummy"
                start_screen.query_one("#task-select", Select).value = TASK_ID
                start_screen.query_one("#policy-select", Select).value = PDB_POLICY

            async def settle_and_click():
                await pilot.pause()
                await pilot.click("#start-button")

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
            assert "FAILED" in live_bar_text(workspace)
            # return to history; the failure registered honestly
            await pilot.press("q")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            table = pilot.app.screen.query_one("#history-table")
            assert table.row_count == 1
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
            await pilot.press("q")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            # both sessions registered with distinct identities
            table = pilot.app.screen.query_one("#history-table")
            assert table.row_count == 2
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
            start.query_one("#mode-select", Select).value = "configured"
            start.query_one("#profile-select", Select).value = "dummy"
            start.query_one("#task-select", Select).value = TASK_ID
            start.query_one("#policy-select", Select).value = PDB_POLICY
            await pilot.pause()
            await pilot.click("#start-button")
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
                lambda: "cancel requested" in live_bar_text(workspace),
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
            assert "CANCELLED" in live_bar_text(workspace)
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
            start.query_one("#mode-select", Select).value = "configured"
            start.query_one("#profile-select", Select).value = "dummy"
            start.query_one("#task-select", Select).value = TASK_ID
            start.query_one("#policy-select", Select).value = PDB_POLICY
            await pilot.pause()
            await pilot.click("#start-button")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                timeout_seconds=30.0,
            )
            workspace1 = pilot.app.screen
            assert "configured command model" in header_text(workspace1)
            await wait_until(
                pilot,
                lambda: workspace1._live_terminal is not None,
                timeout_seconds=300.0,
                label="stage1-configured-terminal",
            )
            assert "SUCCEEDED" in live_bar_text(workspace1)
            session1_id = app.live_view.session_id
            assert session1_id is not None
            await pilot.press("q")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            assert pilot.app.screen.query_one("#history-table").row_count == 1

            # -- session 2: deterministic, cancelled ------------------------
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: pilot.app.screen.__class__.__name__ == "StartSessionScreen",
                timeout_seconds=30.0,
            )
            start2 = pilot.app.screen
            start2.query_one("#task-select", Select).value = TASK_ID
            start2.query_one("#policy-select", Select).value = PDB_POLICY
            await pilot.click("#start-button")
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
            assert "CANCELLED" in live_bar_text(workspace2)
            session2_id = app.live_view.session_id
            assert session2_id != session1_id
            await pilot.press("q")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            # both sessions registered with distinct identities
            table = pilot.app.screen.query_one("#history-table")
            assert table.row_count == 2
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
            start.query_one("#task-select", Select).value = TASK_ID
            start.query_one("#policy-select", Select).value = PDB_POLICY
            await pilot.click("#start-button")
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
            assert "CANCELLED" in live_bar_text(workspace1)
            session1_id = app.live_view.session_id
            assert session1_id is not None
            await pilot.press("q")
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
            start2.query_one("#mode-select", Select).value = "configured"
            start2.query_one("#profile-select", Select).value = "dummy"
            start2.query_one("#task-select", Select).value = TASK_ID
            start2.query_one("#policy-select", Select).value = PDB_POLICY
            await pilot.pause()
            await pilot.click("#start-button")
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
            await pilot.press("q")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            # both sessions registered with distinct identities
            table = pilot.app.screen.query_one("#history-table")
            assert table.row_count == 2
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
            start.query_one("#mode-select", Select).value = "configured"
            start.query_one("#profile-select", Select).value = "dummy"
            start.query_one("#task-select", Select).value = TASK_ID
            start.query_one("#policy-select", Select).value = PDB_POLICY
            await pilot.pause()
            await pilot.click("#start-button")
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
            await pilot.press("q")
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
            start2.query_one("#mode-select", Select).value = "configured"
            start2.query_one("#profile-select", Select).value = "dummy"
            start2.query_one("#task-select", Select).value = TASK_ID
            start2.query_one("#policy-select", Select).value = PDB_POLICY
            await pilot.pause()
            await pilot.click("#start-button")
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
            await pilot.press("q")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            # both the cancelled session and the retry registered
            table = pilot.app.screen.query_one("#history-table")
            assert table.row_count == 2

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
            start.query_one("#mode-select", Select).value = "configured"
            start.query_one("#profile-select", Select).value = "dummy"
            start.query_one("#task-select", Select).value = TASK_ID
            start.query_one("#policy-select", Select).value = PDB_POLICY
            # the configuration changes between discovery and start
            (tmp_path / "config" / "command-models.json").unlink()
            await pilot.pause()
            await pilot.click("#start-button")
            await pilot.pause()
            # still on the start screen with a clear error, never a crash
            assert pilot.app.screen.__class__.__name__ == "StartSessionScreen"
            error = str(start.query_one("#start-error").render())
            assert "configured command model unavailable" in error
            assert "profile not found" in error
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
            start.query_one("#mode-select", Select).value = "configured"
            start.query_one("#profile-select", Select).value = "dummy"
            start.query_one("#task-select", Select).value = TASK_ID
            start.query_one("#policy-select", Select).value = PDB_POLICY
            await pilot.pause()
            await pilot.click("#start-button")
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
            start.query_one("#mode-select", Select).value = "configured"
            start.query_one("#profile-select", Select).value = "dummy"
            start.query_one("#task-select", Select).value = TASK_ID
            start.query_one("#policy-select", Select).value = PDB_POLICY
            await pilot.pause()
            await pilot.click("#start-button")
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
            assert "FAILED" in live_bar_text(workspace)
            await pilot.press("q")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, HomeScreen),
                timeout_seconds=30.0,
            )
            # replay the recorded failure at the compact size
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
                start.query_one("#mode-select", Select).value = "configured"
                start.query_one("#profile-select", Select).value = "dummy"
                start.query_one("#task-select", Select).value = TASK_ID
                start.query_one("#policy-select", Select).value = PDB_POLICY
                await pilot.pause()
                await pilot.click("#start-button")
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
