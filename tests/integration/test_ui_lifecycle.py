"""Application ownership lifecycle tests for live sessions (Repair Pass 1).

The application owns exactly one ``LiveSessionRunner`` at a time.  This
suite proves the ownership lifecycle without spawning real workers: a
scripted ``SessionWorkerProcess`` double writes a real journal to the
app-owned session directory (so history registration and replay work end to
end), and the app-level semantics are asserted through the headless app:

- terminal completion/cancellation releases application ownership;
- startup/supervision failure releases application ownership;
- while a session is genuinely active a second start is rejected;
- releasing ownership never orphans the worker (the runner's own
  supervision path closes it);
- the previous session's persisted history remains reopenable after a new
  session starts.

The real-worker gates live in ``test_ui_live.py``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import pytest

textual = pytest.importorskip("textual")

from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    SourceKind,
)
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.session import (
    SessionId,
    SessionResult,
    SessionStatus,
    SessionTerminationReason,
)
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import (
    HomeScreen,
    StartSessionScreen,
    WorkspaceMode,
    WorkspaceScreen,
)


from ui_support import (
    VALID_TASK_ID,
    make_event,
    run_headless,
)

TASK_ID = "curated-off-by-one-002"
POLICY = "pdb-on-uncertainty"


def make_app(tmp_path: Path) -> LocalApplicationV1:
    return LocalApplicationV1(history_store=HistoryStore(tmp_path))


async def wait_until(
    pilot,
    predicate: Callable[[], bool],
    timeout_seconds: float = 60.0,
    interval: float = 0.05,
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


def _stream(
    session_id: str,
    run_id: str,
    outcome: str,
) -> tuple[SessionEvent, ...]:
    """One scripted journal stream with the given terminal outcome."""
    events = [
        make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": "a" * 64},
            sequence=0,
            session_id=session_id,
        ),
        make_event(
            SessionEventKind.SESSION_STARTED,
            {},
            sequence=1,
            session_id=session_id,
            run_id=run_id,
        ),
        make_event(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": "executing_tool"},
            sequence=2,
            session_id=session_id,
            run_id=run_id,
        ),
    ]
    if outcome == "active":
        # A genuine prefix with no terminal: the session is still running.
        return tuple(events)
    events.extend(
        [
            make_event(
                SessionEventKind.CLEANUP_STARTED,
                {},
                sequence=3,
                session_id=session_id,
                run_id=run_id,
            ),
            make_event(
                SessionEventKind.CLEANUP_COMPLETED,
                {"verified": True},
                sequence=4,
                session_id=session_id,
                run_id=run_id,
            ),
        ]
    )
    if outcome == "cancelled":
        events.append(
            make_event(
                SessionEventKind.SESSION_CANCELLED,
                {"status": "cancelled", "termination_reason": "cancelled"},
                sequence=5,
                session_id=session_id,
                run_id=run_id,
            )
        )
    else:
        events.append(
            make_event(
                SessionEventKind.SESSION_COMPLETED,
                {"status": "succeeded", "termination_reason": "done"},
                sequence=5,
                session_id=session_id,
                run_id=run_id,
            )
        )
    return tuple(events)


class _ScriptedWorker:
    """A scripted ``SessionWorkerProcess`` seam for app lifecycle tests.

    Accepts the real worker's constructor keywords, writes the scripted
    stream to the app-owned session directory when started (so the
    application's own history registration and replay work end to end),
    and serves the scripted terminal outcome.
    """

    def __init__(
        self,
        *,
        session_dir,
        session_id,
        spec,
        run_id,
        scenario,
        scenario_params,
        cooperative_grace_seconds,
        ready_timeout_seconds,
        max_elapsed_seconds,
        retry_of_session_id=None,
        outcome: str = "completed",
        startup_error: Optional[str] = None,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.session_id = session_id
        self.spec = spec
        self.run_id = run_id
        self.outcome = outcome
        self.startup_error = startup_error
        self.pid: Optional[int] = None
        self.events: tuple[SessionEvent, ...] = ()
        self.started = False
        self.cancelled = False
        self.closed = False
        self._released = threading.Event()

    def start(self) -> Optional[SessionResult]:
        if self.startup_error is not None:
            raise RuntimeError(self.startup_error)
        self.started = True
        self.events = _stream(self.session_id, self.run_id, self.outcome)
        if self.events:
            from agentic_debugger.application.journal import SessionEventJournal

            session_dir = Path(self.session_dir)
            session_dir.mkdir(parents=True, exist_ok=True)
            journal = SessionEventJournal(
                session_dir / "session.events.jsonl",
                session_id=self.session_id,
                task_id=VALID_TASK_ID,
                source_kind=SourceKind.OFFLINE_DEMO,
            )
            for event in self.events:
                journal.append(event)
            journal.close()
        return None

    def cancel(self) -> None:
        self.cancelled = True
        self._released.set()

    def wait(self) -> SessionResult:
        if self.outcome == "active":
            # A genuinely active session only terminates through the
            # accepted cancellation path (used by runner teardown).
            self._released.wait(timeout=60.0)
        terminal = self.events[-1]
        payload = dict(terminal.payload)
        return SessionResult(
            session_id=SessionId(self.session_id),
            spec=self.spec,
            status=SessionStatus(payload["status"]),
            termination_reason=SessionTerminationReason(payload["termination_reason"]),
            run_id=self.run_id,
            cleanup_verified=True,
            sequence=terminal.sequence,
        )

    def close(self) -> None:
        self.closed = True


class _ScriptedWorkerFactory:
    """Builds scripted workers and keeps every created instance for
    assertions (e.g. no-orphan checks)."""

    def __init__(
        self,
        outcome: str = "completed",
        startup_error: Optional[str] = None,
    ) -> None:
        self.outcome = outcome
        self.startup_error = startup_error
        self.created: list[_ScriptedWorker] = []

    def __call__(self, **kwargs) -> _ScriptedWorker:
        worker = _ScriptedWorker(
            outcome=self.outcome,
            startup_error=self.startup_error,
            **kwargs,
        )
        self.created.append(worker)
        return worker


def start_via_start_screen(pilot) -> WorkspaceScreen:
    """Drive the real StartSessionScreen UX: n -> select -> Start."""
    from agentic_debugger.ui.screens import StartSessionScreen as _Start

    start_screen = pilot.app.screen
    assert isinstance(start_screen, _Start)
    start_screen._choice_selected("task", TASK_ID)
    start_screen._choice_selected("debugger", "pdb-on-uncertainty")
    return start_screen


class TestOwnershipRelease:
    def test_completed_session_releases_runner_and_closes_worker(
        self, tmp_path, monkeypatch
    ):
        factory = _ScriptedWorkerFactory(outcome="completed")
        monkeypatch.setattr("agentic_debugger.ui.app.SessionWorkerProcess", factory)
        app = make_app(tmp_path)

        async def scenario(pilot):
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            runner = app.live_runner
            assert runner is not None
            workspace = pilot.app.screen
            assert isinstance(workspace, WorkspaceScreen)
            assert workspace.mode is WorkspaceMode.LIVE
            # After the terminal has been delivered the application no
            # longer considers the runner active...
            await wait_until(
                pilot,
                lambda: app.live_runner is None,
                label="runner-released",
            )
            assert app.live_view is not None
            assert app.live_view.status is SessionStatus.SUCCEEDED
            # ...and the runner's own supervision path still closes the
            # worker (releasing ownership never orphans it).
            await wait_until(
                pilot,
                lambda: runner.worker.closed,
                label="worker-closed",
            )
            # The finished session registered into app-owned history.
            await pilot.press("h")
            assert isinstance(pilot.app.screen, HomeScreen)
            table = pilot.app.screen.query_one("#history-table")
            assert table.row_count == 1

        run_headless(app, scenario)

    def test_cancelled_session_releases_runner_and_allows_retry(
        self, tmp_path, monkeypatch
    ):
        factory = _ScriptedWorkerFactory(outcome="cancelled")
        monkeypatch.setattr("agentic_debugger.ui.app.SessionWorkerProcess", factory)
        app = make_app(tmp_path)

        async def scenario(pilot):
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            runner = app.live_runner
            workspace = pilot.app.screen
            await wait_until(
                pilot,
                lambda: app.live_runner is None,
                label="runner-released",
            )
            assert app.live_view.status is SessionStatus.CANCELLED
            await wait_until(pilot, lambda: runner.worker.closed, label="worker-closed")
            assert workspace._live_terminal is not None
            # Back to Home, then a cancelled session must not block a fresh
            # session either.
            await pilot.press("h")
            assert isinstance(pilot.app.screen, HomeScreen)
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            workspace = pilot.app.screen
            assert workspace.mode is WorkspaceMode.LIVE
            await wait_until(
                pilot,
                lambda: app.live_runner is None,
                label="runner-released-2",
            )
            await pilot.press("h")
            assert isinstance(pilot.app.screen, HomeScreen)

        run_headless(app, scenario)

    def test_startup_failure_releases_runner_and_allows_retry(
        self, tmp_path, monkeypatch
    ):
        factory = _ScriptedWorkerFactory(startup_error="injected startup failure")
        monkeypatch.setattr("agentic_debugger.ui.app.SessionWorkerProcess", factory)
        app = make_app(tmp_path)

        async def scenario(pilot):
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            runner = app.live_runner
            workspace = pilot.app.screen
            # The failure surfaces on the workspace and releases ownership.
            await wait_until(
                pilot,
                lambda: workspace._live_failure is not None,
                label="failure-shown",
            )
            assert "startup failed" in str(
                workspace.query_one("#status-header").render()
            )
            await wait_until(
                pilot,
                lambda: app.live_runner is None,
                label="runner-released",
            )
            await wait_until(pilot, lambda: runner.worker.closed, label="worker-closed")
            # Back to Home; a retry can start another session without
            # restarting the TUI.
            await pilot.press("h")
            assert isinstance(pilot.app.screen, HomeScreen)
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            workspace = pilot.app.screen
            assert workspace.mode is WorkspaceMode.LIVE
            assert workspace._live_failure is None
            await wait_until(
                pilot,
                lambda: app.live_runner is None,
                label="runner-released-2",
            )
            await pilot.press("h")
            assert isinstance(pilot.app.screen, HomeScreen)

        run_headless(app, scenario)

    def test_second_session_rejected_while_first_is_active(
        self, tmp_path, monkeypatch
    ):
        factory = _ScriptedWorkerFactory(outcome="active")
        monkeypatch.setattr("agentic_debugger.ui.app.SessionWorkerProcess", factory)
        app = make_app(tmp_path)

        async def scenario(pilot):
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            runner = app.live_runner
            assert runner is not None
            # The worker is genuinely active: a second start is rejected.
            with pytest.raises(RuntimeError, match="already active"):
                app.start_live_session(
                    task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
                )
            # Quitting tears the active session down without orphaning it.
            app.exit()

        run_headless(app, scenario)
        assert factory.created[0].closed

    def test_start_form_shows_error_while_session_active(
        self, tmp_path, monkeypatch
    ):
        """Through the real start UX: while a live session is genuinely
        active, launching a second one keeps the form on screen with its
        error (the form is only replaced after a successful launch)."""
        factory = _ScriptedWorkerFactory(outcome="active")
        monkeypatch.setattr("agentic_debugger.ui.app.SessionWorkerProcess", factory)
        app = make_app(tmp_path)

        async def scenario(pilot):
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, StartSessionScreen),
                label="start-screen-1",
            )
            start_via_start_screen(pilot)
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                label="workspace-1",
            )
            # Back to Home while the first session stays genuinely active.
            await pilot.press("h")
            assert isinstance(pilot.app.screen, HomeScreen)
            # The second launch is rejected on the form itself.
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, StartSessionScreen),
                label="start-screen-2",
            )
            start_via_start_screen(pilot)
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(pilot.app.screen, StartSessionScreen)
            error = pilot.app.screen.query_one("#start-status")
            assert "already active" in str(error.render())
            # Escape still returns to Home normally.
            await pilot.press("escape")
            assert isinstance(pilot.app.screen, HomeScreen)
            app.exit()

        run_headless(app, scenario)
        assert factory.created[0].closed

    def test_background_completion_on_home_releases_runner(
        self, tmp_path, monkeypatch
    ):
        """The terminal may arrive while the user is already on Home; the
        application still releases ownership and shows the new history row."""
        factory = _ScriptedWorkerFactory(outcome="completed")
        monkeypatch.setattr("agentic_debugger.ui.app.SessionWorkerProcess", factory)
        app = make_app(tmp_path)

        async def scenario(pilot):
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            # Back to Home while the session is still running.
            await pilot.press("h")
            assert isinstance(pilot.app.screen, HomeScreen)
            # The background completion releases ownership and registers.
            await wait_until(
                pilot,
                lambda: app.live_runner is None,
                label="runner-released",
            )
            await wait_until(
                pilot,
                lambda: pilot.app.screen.query_one("#history-table").row_count == 1,
                label="history-registered",
            )
            # And a new session can start from that Home.
            await pilot.press("n")
            start_via_start_screen(pilot)
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                label="second-workspace",
            )
            assert pilot.app.screen.mode is WorkspaceMode.LIVE
            await wait_until(
                pilot,
                lambda: app.live_runner is None,
                label="runner-released-2",
            )

        run_headless(app, scenario)


class TestSequentialSessions:
    def test_start_screen_replaced_and_second_session_starts(
        self, tmp_path, monkeypatch
    ):
        """Blocker 1 + 2 together through the real StartSessionScreen UX.

        A successful launch replaces the start form on the stack (q returns
        to Home, never to the stale form), and after the first session
        completes a second session starts from the same Home.
        """
        factory = _ScriptedWorkerFactory(outcome="completed")
        monkeypatch.setattr("agentic_debugger.ui.app.SessionWorkerProcess", factory)
        app = make_app(tmp_path)

        async def scenario(pilot):
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, StartSessionScreen),
                label="start-screen",
            )
            start_via_start_screen(pilot)
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                label="workspace-1",
            )
            workspace1 = pilot.app.screen
            assert workspace1.mode is WorkspaceMode.LIVE
            # The stack is Home -> Workspace; the start form is gone.
            assert isinstance(pilot.app._screen_stack[-2], HomeScreen)
            await wait_until(
                pilot,
                lambda: app.live_runner is None,
                label="runner-released-1",
            )
            session1_id = app.live_view.session_id
            assert session1_id is not None
            # q returns to Home, not to the StartSessionScreen.
            await pilot.press("h")
            assert isinstance(pilot.app.screen, HomeScreen)
            assert pilot.app.screen.query_one("#history-table").row_count == 1
            # A second session starts from the same Home screen.
            await pilot.press("n")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, StartSessionScreen),
                label="start-screen-2",
            )
            start_via_start_screen(pilot)
            await pilot.press("s")
            await wait_until(
                pilot,
                lambda: isinstance(pilot.app.screen, WorkspaceScreen),
                label="workspace-2",
            )
            workspace2 = pilot.app.screen
            assert workspace2.mode is WorkspaceMode.LIVE
            assert isinstance(pilot.app._screen_stack[-2], HomeScreen)
            session2_id = app.live_view.session_id
            assert session2_id is not None
            assert session2_id != session1_id
            await wait_until(
                pilot,
                lambda: app.live_runner is None,
                label="runner-released-2",
            )
            await pilot.press("h")
            assert isinstance(pilot.app.screen, HomeScreen)
            assert pilot.app.screen.query_one("#history-table").row_count == 2

        run_headless(app, scenario)

    def test_history_and_replay_of_first_session_after_second_starts(
        self, tmp_path, monkeypatch
    ):
        factory = _ScriptedWorkerFactory(outcome="completed")
        monkeypatch.setattr("agentic_debugger.ui.app.SessionWorkerProcess", factory)
        app = make_app(tmp_path)

        async def scenario(pilot):
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            await wait_until(
                pilot,
                lambda: app.live_runner is None,
                label="runner-released-1",
            )
            session1_id = app.live_view.session_id
            await pilot.press("h")
            assert isinstance(pilot.app.screen, HomeScreen)
            # Start the second session; its completion registers afterwards.
            app.start_live_session(
                task_id=TASK_ID, policy=POLICY, max_elapsed_seconds=None
            )
            # The view carries the session id only after the worker's first
            # journaled event reaches the presentation model.
            await wait_until(
                pilot,
                lambda: app.live_view is not None
                and app.live_view.session_id is not None,
                label="session2-id",
            )
            session2_id = app.live_view.session_id
            assert session2_id != session1_id
            await wait_until(
                pilot,
                lambda: app.live_runner is None,
                label="runner-released-2",
            )
            # Both sequential sessions are in history with distinct ids.
            await pilot.press("h")
            assert isinstance(pilot.app.screen, HomeScreen)
            await wait_until(
                pilot,
                lambda: pilot.app.screen.query_one("#history-table").row_count == 2,
                label="history-both",
            )
            ids = [
                entry.session_id
                for entry in app.history_store.list_sessions()
            ]
            assert session1_id in ids and session2_id in ids
            assert len(set(ids)) == 2
            # Session 1's persisted history still replays after session 2.
            app.open_session(session1_id)
            workspace = pilot.app.screen
            assert workspace.mode is WorkspaceMode.REPLAY
            assert workspace.entry.session_id == session1_id
            workspace.action_replay_end()
            assert workspace.controller.view.status is SessionStatus.SUCCEEDED
            await pilot.press("h")
            assert isinstance(pilot.app.screen, HomeScreen)

        run_headless(app, scenario)
