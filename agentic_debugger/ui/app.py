"""Local Application V1 — the replay-first Textual application.

Launch surface (one documented command)::

    python -m agentic_debugger.ui [--root DIR]

The application is presentation-only over the accepted application/session
layer:

- the Home screen exposes app-owned history through
  :class:`~agentic_debugger.application.history.HistoryStore`;
- recorded sessions open as read-only replays through
  :class:`~agentic_debugger.ui.models.ReplayController` (same pure reducer
  as live events; no executable resource is ever touched);
- a bounded deterministic offline session may be started through the
  accepted cancellable worker boundary; live events flow from the durable
  journal into the same presentation model;
- app teardown never strands a live worker (bounded cooperative
  cancellation, Task-3 escalation, then handle release).

The app requires no GPU, model provider, network, WSL, or campaign
infrastructure.  The scientific core never imports this package.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from textual.app import App
from textual.binding import Binding

from agentic_debugger.application.events import SessionEvent, SourceKind
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    SessionViewState,
    initial_session_view,
    presentation_identity,
    reduce_event,
)
from agentic_debugger.application.replay import SessionReplaySource
from agentic_debugger.application.session import SessionBudgets, SessionResult, SessionSpec
from agentic_debugger.application.sources import ExecutionSourceSpec
from agentic_debugger.application.worker_process import SessionWorkerProcess
from agentic_debugger.ui.models import LiveSessionRunner, ReplayController
from agentic_debugger.ui.screens import (
    HomeScreen,
    StartSessionScreen,
    WorkspaceMode,
    WorkspaceScreen,
)

DEFAULT_HISTORY_DIR_NAME = "AgenticDebugger"

_COOPERATIVE_GRACE_SECONDS = 10.0
_READY_TIMEOUT_SECONDS = 30.0


def deterministic_source_name() -> str:
    """The one production deterministic worker source (Task 7)."""
    from agentic_debugger.application.deterministic_source import (
        DETERMINISTIC_SOURCE_NAME,
    )

    return DETERMINISTIC_SOURCE_NAME


def default_history_root() -> Path:
    """The application-owned run root (``%LOCALAPPDATA%/AgenticDebugger``)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / DEFAULT_HISTORY_DIR_NAME


def repository_root() -> Path:
    """The repository root owning the installed package."""
    import agentic_debugger

    return Path(agentic_debugger.__file__).resolve().parent.parent


def make_session_id() -> str:
    """One validated application session id (``sess-<utc>-<rand>``)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"sess-{stamp}-{secrets.token_hex(3)}"


class LocalApplicationV1(App):
    """The full-screen Local Application V1 TUI."""

    TITLE = "Agentic Debugging — Local Application V1"
    SUB_TITLE = "replay-first debugging session application"
    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False),
    ]

    def __init__(
        self,
        history_root: Optional[str | os.PathLike[str]] = None,
        repo_root: Optional[str | os.PathLike[str]] = None,
        history_store: Optional[HistoryStore] = None,
    ) -> None:
        super().__init__()
        self._history_store = history_store
        if self._history_store is None:
            self._history_store = HistoryStore(
                Path(history_root) if history_root is not None else default_history_root()
            )
        self._repository_root = (
            Path(repo_root).resolve() if repo_root is not None else repository_root()
        )
        # Live session state is app-owned: a presentation disconnect (back to
        # history) does not cancel the worker; it keeps journaling and the
        # finished session registers into app-owned history.
        self._live_runner: Optional[LiveSessionRunner] = None
        self._live_identity: Optional[PresentationIdentity] = None
        self._live_view: Optional[SessionViewState] = None
        self._live_events: Tuple[SessionEvent, ...] = ()
        self._live_last_sequence = -1
        self._live_workspace: Optional[WorkspaceScreen] = None

    # -- properties ---------------------------------------------------------

    @property
    def history_store(self) -> HistoryStore:
        return self._history_store

    @property
    def repository_root(self) -> Path:
        return self._repository_root

    @property
    def live_runner(self) -> Optional[LiveSessionRunner]:
        return self._live_runner

    def live_events(self) -> Tuple[SessionEvent, ...]:
        return self._live_events

    @property
    def live_view(self) -> Optional[SessionViewState]:
        """The app-owned live presentation state (None before a live start)."""
        return self._live_view

    # -- boot ---------------------------------------------------------------

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())

    def on_unmount(self) -> None:
        # App teardown must never orphan a live worker: bounded cooperative
        # cancellation + Task-3 escalation, then handle release.
        self._shutdown_live_runner()

    def action_quit(self) -> None:
        self._shutdown_live_runner()
        self.exit()

    def _shutdown_live_runner(self) -> None:
        runner = self._live_runner
        if runner is None:
            return
        self._live_runner = None
        self._live_workspace = None
        runner.close()

    # -- navigation ---------------------------------------------------------

    def open_session(self, session_id: str) -> None:
        """Open one app-owned session read-only and push the workspace."""
        try:
            reopened = self.history_store.reopen(session_id)
        except Exception as exc:
            self.notify(f"Cannot open session {session_id}: {exc}", severity="error")
            return
        self._push_replay_workspace(reopened)

    def _push_replay_workspace(self, reopened: object) -> None:
        entry = reopened.entry
        replay: SessionReplaySource = reopened.replay
        identity = PresentationIdentity(
            task_id=replay.task_id,
            source_kind=replay.source_kind,
            session_id=replay.session_id,
        )
        controller = ReplayController(replay, identity)
        self.push_screen(
            WorkspaceScreen(
                mode=WorkspaceMode.REPLAY,
                controller=controller,
                entry=entry,
                identity=identity,
            )
        )

    def go_home(self) -> None:
        """Return to the history screen; a live session keeps running."""
        self.pop_screen()
        self.refresh_home_history()

    def refresh_home_history(self) -> None:
        """Refresh the history table after the current screen settles.

        Called when a workspace is popped so a just-registered live session
        is immediately visible in the app-owned history list.
        """
        self.call_later(self._refresh_home_history)

    def _refresh_home_history(self) -> None:
        screen = self.screen
        if isinstance(screen, HomeScreen):
            screen.refresh_history()

    # -- live sessions ------------------------------------------------------

    def curated_task_ids(self) -> Tuple[str, ...]:
        """The canonical deterministic-session task catalog.

        Discovery comes from the live curated fixture directory; only tasks
        the accepted deterministic demo source actually has a scenario for
        are offered (starting any other fixture would fail at scenario
        resolution).  This is the repository's own catalog, never a second
        list.
        """
        from agentic_debugger.demo.catalog import scenario_ids
        from agentic_debugger.demo.runner import curated_task_ids

        supported = set(scenario_ids())
        return tuple(
            task_id
            for task_id in curated_task_ids(self._repository_root)
            if task_id in supported
        )

    def start_live_session(
        self,
        *,
        task_id: str,
        policy: str,
        max_elapsed_seconds: Optional[int],
    ) -> None:
        """Start one real deterministic offline session in the worker.

        The worker runs the accepted production deterministic source (real
        controller, tool registry, PDB, PatchManager, and independent
        verifier) with one shared session emitter.  The workspace is pushed
        first so the presentation model is ready for the first events.
        """
        if self._live_runner is not None:
            raise RuntimeError("a live session is already active")
        session_id = make_session_id()
        run_id = f"run-{session_id}"
        spec = SessionSpec(
            task_id=task_id,
            source=ExecutionSourceSpec(
                kind=SourceKind.OFFLINE_DEMO,
                task_id=task_id,
                policy=policy,
                model_config_ref=None,
            ),
            budgets=SessionBudgets(max_elapsed_seconds=max_elapsed_seconds),
        )
        worker = SessionWorkerProcess(
            session_dir=self.history_store.session_dir(session_id),
            session_id=session_id,
            spec=spec,
            run_id=run_id,
            scenario=deterministic_source_name(),
            scenario_params={"task_id": task_id, "policy": policy},
            cooperative_grace_seconds=_COOPERATIVE_GRACE_SECONDS,
            ready_timeout_seconds=_READY_TIMEOUT_SECONDS,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        identity = presentation_identity(spec)
        view = initial_session_view(identity)
        runner = LiveSessionRunner(
            worker,
            history_store=self.history_store,
            on_started=self._on_live_started,
            on_events=self._on_live_events,
            on_terminal=self._on_live_terminal,
            on_failure=self._on_live_failure,
        )
        workspace = WorkspaceScreen(
            mode=WorkspaceMode.LIVE,
            identity=identity,
            view=view,
            runner=runner,
        )
        self._live_runner = runner
        self._live_identity = identity
        self._live_view = view
        self._live_events = ()
        self._live_last_sequence = -1
        self._live_workspace = workspace
        if isinstance(self.screen, StartSessionScreen):
            # A successful launch replaces the start form on the stack so
            # q/escape from the workspace returns to Home, never to the
            # stale form.  The form is only replaced after all validation
            # succeeded, so a rejected start still shows its error there.
            self.switch_screen(workspace)
        else:
            self.push_screen(workspace)
        runner.start()

    def detach_live_workspace(self, workspace: WorkspaceScreen) -> None:
        """A workspace was popped; the live session itself keeps running."""
        if self._live_workspace is workspace:
            self._live_workspace = None

    # -- live callbacks (runner thread -> event loop) -----------------------

    def _on_live_started(self) -> None:
        try:
            self.call_from_thread(self._live_started_ui)
        except Exception:
            pass

    def _live_started_ui(self) -> None:
        workspace = self._live_workspace
        if workspace is not None and workspace.is_mounted:
            workspace.refresh_live()

    def _on_live_events(self, events: Tuple[SessionEvent, ...]) -> None:
        try:
            self.call_from_thread(self._live_events_ui, events)
        except Exception:
            pass

    def _live_events_ui(self, events: Tuple[SessionEvent, ...]) -> None:
        if self._live_view is None:
            return
        for event in events:
            if event.sequence <= self._live_last_sequence:
                continue
            self._live_last_sequence = event.sequence
            self._live_view = reduce_event(self._live_view, event)
            self._live_events = self._live_events + (event,)
        workspace = self._live_workspace
        if workspace is not None and workspace.is_mounted:
            workspace.refresh_live()

    def _on_live_terminal(self, result: SessionResult, registration_error: Optional[str]) -> None:
        try:
            self.call_from_thread(self._live_terminal_ui, result, registration_error)
        except Exception:
            pass

    def _live_terminal_ui(self, result: object, registration_error: Optional[str]) -> None:
        workspace = self._live_workspace
        if workspace is not None:
            # The workspace records the terminal itself (its ``is_mounted``
            # guard handles a fast worker that finished before the mount).
            workspace.show_live_terminal(result, registration_error)
        # The terminal has been delivered: the runner is finished (its own
        # supervision thread closes the worker right after this callback), so
        # the app no longer considers it active and another session may start.
        self._release_live_runner()
        home = self.screen
        if isinstance(home, HomeScreen):
            home.refresh_history()

    def _on_live_failure(self, diagnostic: str) -> None:
        try:
            self.call_from_thread(self._live_failure_ui, diagnostic)
        except Exception:
            pass

    def _live_failure_ui(self, diagnostic: str) -> None:
        workspace = self._live_workspace
        if workspace is not None:
            workspace.show_live_failure(diagnostic)
        else:
            self.notify(diagnostic, severity="error", title="Live session")
        # A startup/supervision failure is terminal for the runner: release
        # ownership so a retry can start another session.  The runner's own
        # supervision path performs the final worker handle close.
        self._release_live_runner()

    def _release_live_runner(self) -> None:
        """Drop application ownership of a finished/failed live runner.

        Only the runner's own supervision thread closes the worker, so this
        never joins the runner thread from the event loop (that would
        deadlock the terminal callback, which the driver thread is waiting
        on).  The recorded presentation data (``live_view``/``live_events``)
        stays available for reopening and replay parity.
        """
        self._live_runner = None
        self._live_workspace = None


__all__ = [
    "LocalApplicationV1",
    "default_history_root",
    "make_session_id",
    "repository_root",
]
