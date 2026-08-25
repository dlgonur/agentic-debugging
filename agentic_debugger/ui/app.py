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

The app requires no GPU, model provider, WSL, or campaign infrastructure.
Deterministic sessions are application-controlled offline execution.
Configured command-model sessions launch a user-configured local command
(trusted user configuration); the app itself adds no provider integration,
but V1 does not enforce child-process network isolation.  The scientific
core never imports this package.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from textual.app import App
from textual.binding import Binding

from agentic_debugger.application.command_config import (
    CommandConfigError,
    CommandModelConfigStore,
)
from agentic_debugger.application.events import SessionEvent, SourceKind
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.level32 import (
    LEVEL32_TASK_ID,
    Level32OperatorWorker,
    build_level32_spec,
    level32_model_profiles,
    next_level32_treatment,
)
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
LEVEL32_TASK_TITLE = "Level 32/100 — Cookiecutter #967"

_CURATED_TASK_TITLES: dict[str, str] = {
    "curated-none-handling-001": "Format an optional display name",
    "curated-off-by-one-002": "Return the complete recent window",
    "curated-wrong-branch-003": "Select the correct access branch",
    "curated-mutation-alias-004": "Append a label without mutating the caller",
    "curated-caller-callee-005": "Convert the caller representation at the boundary",
}


def task_display_title(task_id: str, repo_root: Optional[Path] = None) -> str:
    """Return a human-readable title for a task id.

    Tries loading the task title from task.json under the repository root,
    then checks curated mapping, and falls back to task_id if unavailable.
    """
    if repo_root is not None:
        task_json = (
            Path(repo_root)
            / "agentic_debugger"
            / "datasets"
            / "curated"
            / task_id
            / "task.json"
        )
        if task_json.is_file():
            try:
                import json

                with open(task_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and data.get("title"):
                        return str(data["title"])
            except Exception:
                pass
    if task_id in _CURATED_TASK_TITLES:
        return _CURATED_TASK_TITLES[task_id]
    if task_id == LEVEL32_TASK_ID:
        return LEVEL32_TASK_TITLE
    return task_id


def task_display_option(
    task_id: str, repo_root: Optional[Path] = None
) -> tuple[str, str]:
    """Return (label, task_id) for dropdown selectors."""
    title = task_display_title(task_id, repo_root)
    if title != task_id:
        return f"{title} · {task_id}", task_id
    return task_id, task_id


def deterministic_source_name() -> str:
    """The one production deterministic worker source (Task 7)."""
    from agentic_debugger.application.deterministic_source import (
        DETERMINISTIC_SOURCE_NAME,
    )

    return DETERMINISTIC_SOURCE_NAME


def configured_source_name() -> str:
    """The one production configured command-model worker source (Task 8)."""
    from agentic_debugger.application.configured_source import (
        CONFIGURED_SOURCE_NAME,
    )

    return CONFIGURED_SOURCE_NAME


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

    TITLE = "Agentic Debugging"
    SUB_TITLE = "Start a debugging session"
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
        # The app-owned command-model configuration lives under the same
        # application root as history (``<root>/config/command-models.json``).
        self._config_store = CommandModelConfigStore(self._history_store.root)
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
    def config_store(self) -> CommandModelConfigStore:
        return self._config_store

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
        # History remains the base navigation surface, but a new debugging
        # session is the product's primary first-run action.
        self.push_screen(HomeScreen())
        self.push_screen(
            StartSessionScreen(task_options=list(self.curated_task_options()))
        )

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

    def curated_task_options(self) -> Tuple[Tuple[str, str], ...]:
        """Human-readable options (label, task_id) for task selectors."""
        curated = tuple(
            task_display_option(task_id, self._repository_root)
            for task_id in self.curated_task_ids()
        )
        return curated + ((f"{LEVEL32_TASK_TITLE} · {LEVEL32_TASK_ID}", LEVEL32_TASK_ID),)

    def configured_profiles(self) -> Tuple[Tuple["ProfileSummary", ...], Optional[str]]:
        """Safe profile summaries for the Start screen, plus a load error.

        Returns ``(summaries, error)``: an empty summary tuple with a
        bounded diagnostic when the app-owned configuration cannot be
        loaded (a malformed config must never crash the TUI; the Start
        screen shows the reason and disables configured mode).
        """
        from agentic_debugger.application.command_config import ProfileSummary

        try:
            return self._config_store.summaries(), None
        except CommandConfigError as exc:
            return (), str(exc)

    def level32_model_profiles(self):
        """Return the canonical, local-only Level-32 Ollama roster."""
        return level32_model_profiles()

    def start_live_session(
        self,
        *,
        task_id: str,
        policy: str,
        max_elapsed_seconds: Optional[int],
        source_kind: SourceKind = SourceKind.OFFLINE_DEMO,
        profile_id: Optional[str] = None,
    ) -> None:
        """Start one real live session in the worker.

        Two supported modes share the same accepted application pipeline:

        - deterministic offline (default): the production deterministic
          source (real controller, tool registry, PDB, PatchManager, and
          independent verifier);
        - configured command model: the same pipeline driven by a validated
          app-owned command-model profile through the accepted JSON-lines
          command transport and ``LiveModelAdapter``.

        The workspace is pushed first so the presentation model is ready
        for the first events.
        """
        if self._live_runner is not None:
            raise RuntimeError("a live session is already active")
        session_id = make_session_id()
        run_id = f"run-{session_id}"
        if source_kind is SourceKind.LEVEL32_OPERATOR:
            if task_id != LEVEL32_TASK_ID:
                raise ValueError("Level-32 operator sessions require the canonical task id")
            if profile_id is None:
                raise ValueError("Level-32 sessions require a canonical Ollama model alias")
            if policy != "exact-pdb-level32-frozen":
                raise ValueError("Level-32 debugger policy is frozen to exact PDB")
            if max_elapsed_seconds is not None:
                raise ValueError("Level-32 uses its frozen operator budget")
            model = next((item for item in self.level32_model_profiles() if item.alias == profile_id), None)
            if model is None:
                raise ValueError("selected model is not currently Level-32 eligible")
            revision, treatment_id, output_dir = next_level32_treatment(self._repository_root, model.alias)
            spec = build_level32_spec(model.alias)
            worker = Level32OperatorWorker(
                session_dir=self.history_store.session_dir(session_id),
                session_id=session_id,
                run_id=run_id,
                repository_root=self._repository_root,
                model=model,
                revision=revision,
                treatment_id=treatment_id,
                output_dir=output_dir,
                spec=spec,
            )
        elif source_kind is SourceKind.CONFIGURED_MODEL:
            if profile_id is None:
                raise ValueError("configured command-model sessions require a profile id")
            # Re-validate at start time: the configuration may have changed
            # between discovery and start; a missing/invalid profile is a
            # clear start error, never a silent fallback.  The selected
            # profile's safe fingerprint is pinned into the worker launch
            # params so the worker can detect a configuration that changed
            # between this selection and its own load (TOCTOU) and fail
            # closed before launching any executable.
            try:
                profile = self._config_store.get(profile_id)
            except CommandConfigError as exc:
                raise RuntimeError(
                    f"configured command model unavailable: {exc}"
                ) from exc
            scenario = configured_source_name()
            scenario_params = {
                "config_root": str(self._config_store.root),
                "profile_id": profile_id,
                "policy": policy,
                "expected_fingerprint": profile.configuration_fingerprint,
            }
            model_config_ref = profile_id
        else:
            scenario = deterministic_source_name()
            scenario_params = {"task_id": task_id, "policy": policy}
            model_config_ref = None
        if source_kind is not SourceKind.LEVEL32_OPERATOR:
            spec = SessionSpec(
                task_id=task_id,
                source=ExecutionSourceSpec(
                    kind=source_kind,
                    task_id=task_id,
                    policy=policy,
                    model_config_ref=model_config_ref,
                ),
                budgets=SessionBudgets(max_elapsed_seconds=max_elapsed_seconds),
            )
            worker = SessionWorkerProcess(
                session_dir=self.history_store.session_dir(session_id),
                session_id=session_id,
                spec=spec,
                run_id=run_id,
                scenario=scenario,
                scenario_params=scenario_params,
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
    "task_display_option",
    "task_display_title",
]
