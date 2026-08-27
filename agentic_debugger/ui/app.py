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

The deterministic and replay paths require no model provider, WSL, or
campaign infrastructure.  Capability-ladder sessions use the canonical
Ollama Cloud operator path and keep provider readiness separate from the
app-owned configured-command registry.  Configured command-model sessions
remain trusted user configuration; the app itself does not enforce
child-process network isolation.  The scientific core never imports this
package.
"""

from __future__ import annotations

import os
import secrets
import time
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
from agentic_debugger.application.live_execution import (
    EphemeralSnapshot, ExecutionMode, KnownCeilings, LiveExecutionState,
    project_live_execution,
)
from agentic_debugger.application.level32 import (
    LADDER_TASK_IDS,
    ladder_task_metadata,
    ladder_task_options,
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
from agentic_debugger.application.worker_protocol import WorkerLiveness
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
    if task_id == "local-project-debug":
        return "Local Project Debug"
    if task_id in _CURATED_TASK_TITLES:
        return _CURATED_TASK_TITLES[task_id]
    if task_id in LADDER_TASK_IDS:
        return ladder_task_metadata(task_id).title
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
        # The universal application exit key. Priority keeps it reachable
        # from ordinary child widgets and active workspaces.
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(
        self,
        history_root: Optional[str | os.PathLike[str]] = None,
        repo_root: Optional[str | os.PathLike[str]] = None,
        history_store: Optional[HistoryStore] = None,
        initial_project: Optional[str | os.PathLike[str]] = None,
    ) -> None:
        super().__init__()
        # Capture launch cwd BEFORE any --root/session handling could imply a cwd change
        try:
            from agentic_debugger.application.local_project import capture_launch_cwd
            capture_launch_cwd()
        except Exception:
            pass
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
        self._initial_project: Optional[Path] = None
        if initial_project is not None:
            try:
                from agentic_debugger.application.local_project import resolve_project_path, get_launch_cwd
                self._initial_project = resolve_project_path(str(initial_project), get_launch_cwd())
            except Exception:
                self._initial_project = Path(str(initial_project))
        # Live session state is app-owned: a presentation disconnect (back to
        # history) does not cancel the worker; it keeps journaling and the
        # finished session registers into app-owned history.
        self._live_runner: Optional[LiveSessionRunner] = None
        self._live_identity: Optional[PresentationIdentity] = None
        self._live_view: Optional[SessionViewState] = None
        self._live_events: Tuple[SessionEvent, ...] = ()
        self._live_last_sequence = -1
        self._live_snapshot: Optional[EphemeralSnapshot] = None
        self._live_generation = 0
        self._live_workspace: Optional[WorkspaceScreen] = None
        self._local_project_worktree_parent: Optional[Path] = None
        self._local_project_repo_root: Optional[Path] = None
        self._local_project_head: Optional[str] = None

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

    def live_execution_state(self) -> Optional[LiveExecutionState]:
        if self._live_view is None:
            return None
        ceilings = KnownCeilings()
        if self._live_view.task_id in LADDER_TASK_IDS and self._live_view.task_id != LEVEL32_TASK_ID:
            from agentic_debugger.application.ollama_cloud_source import ladder_runtime_contract
            contract = ladder_runtime_contract(self._live_view.task_id)
            ceilings = KnownCeilings(contract.max_model_requests, contract.max_controller_steps)
        elif self._live_view.task_id == LEVEL32_TASK_ID:
            from agentic_debugger.evaluation.live import LiveTreatmentBudget
            budget = LiveTreatmentBudget(max_retries=1)
            ceilings = KnownCeilings(
                budget.max_model_requests, budget.max_controller_steps,
                budget.max_patch_attempts,
            )
        return project_live_execution(
            self._live_view, mode=ExecutionMode.LIVE, ceilings=ceilings,
            snapshot=self._live_snapshot, now_monotonic=time.monotonic(),
        )

    @property
    def initial_project(self) -> Optional[Path]:
        return self._initial_project

    # -- boot ---------------------------------------------------------------

    def on_mount(self) -> None:
        # History remains the base navigation surface, but a new debugging
        # session is the product's primary first-run action.
        self.push_screen(HomeScreen())
        # If --project was supplied, prefill Local Project Debug directly
        if self._initial_project is not None:
            try:
                from agentic_debugger.ui.screens import LocalProjectStartScreen
                self.push_screen(LocalProjectStartScreen(initial_project=str(self._initial_project)))
                return
            except Exception:
                pass
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
        """The product picker exposes exactly the accepted capability ladder."""

        return ladder_task_options()

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
        """Return the canonical, local-only Ollama Cloud roster."""
        return level32_model_profiles()

    def ollama_cloud_model_profiles(self):
        return self.level32_model_profiles()

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
        generation = self._live_generation + 1
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
        elif task_id in LADDER_TASK_IDS:
            if profile_id is None:
                raise ValueError("capability-ladder sessions require a canonical Ollama Cloud alias")
            model = next((item for item in self.ollama_cloud_model_profiles() if item.alias == profile_id), None)
            if model is None:
                raise ValueError("selected model is not currently Ollama Cloud eligible")
            from agentic_debugger.application.ollama_cloud_source import OLLAMA_CLOUD_SOURCE_NAME
            scenario = OLLAMA_CLOUD_SOURCE_NAME
            scenario_params = {"model_alias": model.alias, "policy": policy}
            model_config_ref = model.alias
            source_kind = SourceKind.OLLAMA_CLOUD_LADDER
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
            on_liveness=lambda liveness: self._on_live_liveness(
                generation, session_id, liveness
            ),
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
        self._live_generation = generation
        self._live_snapshot = None
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

    def start_local_project_session(
        self,
        *,
        project_path: str,
        bug_description: str,
        reproduction_command: Optional[str] = None,
        verification_command: Optional[str] = None,
        profile_id: Optional[str] = None,
        max_elapsed_seconds: Optional[int] = None,
    ) -> None:
        """Start one Local Project Debug session.

        Validates the project, creates an isolated detached worktree, builds
        the immutable task contract, and supervises the worker.  The UI
        workspace is pushed first so the presentation model is ready for
        first events.
        """
        from agentic_debugger.application.local_project import (
            LocalProjectTaskSpec,
            capture_launch_cwd,
            cleanup_parent_tmpdir,
            create_isolated_worktree,
            get_launch_cwd,
            validate_local_project,
        )
        from agentic_debugger.application.local_project_source import (
            LOCAL_PROJECT_SOURCE_NAME,
        )

        if self._live_runner is not None:
            raise RuntimeError("a live session is already active")
        # Resolve against captured launch cwd (preserve shell cwd before --root)
        try:
            launch_cwd = get_launch_cwd()
        except Exception:
            launch_cwd = Path.cwd().resolve()
        # Model selection is REQUIRED and real — fail before creating any execution resources
        # Support both Ollama Cloud roster (primary) and configured command-model profiles (additional)
        if profile_id is None or not str(profile_id).strip():
            raise RuntimeError("Local Project Debug requires a selected model profile")
        # Try Ollama Cloud roster first (actual product)
        ollama_profile = None
        expected_fp = None
        model_config_ref = None
        try:
            from agentic_debugger.application.level32 import level32_model_profiles
            for m in level32_model_profiles():
                if m.alias == profile_id:
                    ollama_profile = m
                    expected_fp = m.transport_config_fingerprint
                    model_config_ref = m.alias
                    break
        except Exception:
            pass
        if ollama_profile is None:
            try:
                prof = self._config_store.get(profile_id)
                expected_fp = prof.configuration_fingerprint
                model_config_ref = prof.profile_id
            except Exception as exc:
                raise RuntimeError(f"Selected model profile unavailable: {exc}") from exc
        validated = validate_local_project(project_path, launch_cwd=launch_cwd)
        if validated.dirty:
            raise RuntimeError(
                "Project has uncommitted changes. "
                "Commit/stash them first or choose a clean repository."
            )
        # Create isolated worktree (Git-native, no owner mutation) — parent owns until worker owns
        worktree = None
        try:
            worktree = create_isolated_worktree(validated.repo_root, validated.head_commit)
        except Exception:
            raise
        isolated_path = worktree.isolated_path
        parent_tmpdir = worktree.parent_tmpdir
        # Build SessionSpec for Local Project (task_id is fixed, policy is None)
        from datetime import datetime, timezone

        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        task_id = "local-project-debug"
        session_id = make_session_id()
        generation = self._live_generation + 1
        run_id = f"run-{session_id}"
        spec = SessionSpec(
            task_id=task_id,
            source=ExecutionSourceSpec(
                kind=SourceKind.LOCAL_PROJECT,
                task_id=task_id,
                policy=None,
                model_config_ref=model_config_ref,
            ),
            budgets=SessionBudgets(max_elapsed_seconds=max_elapsed_seconds),
        )
        # Also persist the LocalProjectTaskSpec contract as an artifact (immutable)
        # but also keep it in memory for Apply-to-Project gates
        local_spec = LocalProjectTaskSpec(
            session_id=session_id,
            source_repo_path=str(validated.repo_root),
            source_head_commit=validated.head_commit,
            isolated_workspace_path=str(isolated_path),
            bug_description=bug_description,
            reproduction_command=reproduction_command,
            verification_command=verification_command,
            model_runtime=profile_id,
            budgets=SessionBudgets(max_elapsed_seconds=max_elapsed_seconds),
            created_at_utc=created_at,
        )
        # Remember worktree for emergency fallback (parent owns until worker owns)
        self._local_project_worktree_parent = parent_tmpdir
        self._local_project_repo_root = validated.repo_root
        self._local_project_head = validated.head_commit

        # Pre-validate profile fingerprint for worker
        worker_owned = False
        try:
            # Distinguish Ollama vs configured for worker
            scenario_params={
                "project_repo_path": str(validated.repo_root),
                "project_head": validated.head_commit,
                "isolated_workspace": str(isolated_path),
                "bug_description": bug_description,
                "reproduction_command": reproduction_command,
                "verification_command": verification_command,
                "config_root": str(self._config_store.root),
                "profile_id": profile_id,
                "expected_fingerprint": expected_fp,
                "parent_tmpdir": str(parent_tmpdir),
                "policy": "pdb-on-uncertainty",
            }
            # Mark Ollama for worker
            try:
                from agentic_debugger.application.level32 import level32_model_profiles
                if any(m.alias == profile_id for m in level32_model_profiles()):
                    scenario_params["is_ollama"] = True
                    scenario_params["ollama_alias"] = profile_id
            except Exception:
                pass
            worker = SessionWorkerProcess(
                session_dir=self.history_store.session_dir(session_id),
                session_id=session_id,
                spec=spec,
                run_id=run_id,
                scenario=LOCAL_PROJECT_SOURCE_NAME,
                scenario_params=scenario_params,
                cooperative_grace_seconds=_COOPERATIVE_GRACE_SECONDS,
                ready_timeout_seconds=_READY_TIMEOUT_SECONDS,
                max_elapsed_seconds=max_elapsed_seconds,
            )
            # Pre-write the contract artifact so history can replay without workspace
            try:
                worker.session_dir.mkdir(parents=True, exist_ok=True)
                import json as _json
                (worker.session_dir / "local_project_task.json").write_text(
                    _json.dumps(local_spec.to_mapping(), indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            except Exception:
                pass
            identity = presentation_identity(spec)
            view = initial_session_view(identity)
            runner = LiveSessionRunner(
                worker,
                history_store=self.history_store,
                on_started=self._on_live_started,
                on_events=self._on_live_events,
                on_terminal=self._on_live_terminal,
                on_failure=self._on_live_failure,
                on_liveness=lambda liveness: self._on_live_liveness(
                    generation, session_id, liveness
                ),
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
            self._live_generation = generation
            self._live_snapshot = None
            self._live_workspace = workspace
            # Local project start always replaces the form
            try:
                self.switch_screen(workspace)
            except Exception:
                self.push_screen(workspace)
            runner.start()
            worker_owned = True
        except Exception as exc:
            # Before worker ownership: parent must clean worktree (verified)
            if not worker_owned:
                try:
                    from agentic_debugger.application.local_project import cleanup_parent_tmpdir
                    cleanup_parent_tmpdir(parent_tmpdir, validated.repo_root)
                except Exception:
                    pass
            raise

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
            # A journal prefix is already retained by the worker and reduced
            # into the bounded timeline. Keep only a bounded compatibility
            # tail; never create a second unbounded event log in the app.
            self._live_events = (self._live_events + (event,))[-2000:]
        workspace = self._live_workspace
        if workspace is not None and workspace.is_mounted:
            workspace.refresh_live()

    def _on_live_liveness(
        self, generation: int, session_id: str, liveness: WorkerLiveness
    ) -> None:
        try:
            self.call_from_thread(self._live_liveness_ui, generation, session_id, liveness)
        except Exception:
            pass

    def _live_liveness_ui(
        self, generation: int, session_id: str, liveness: WorkerLiveness
    ) -> None:
        if (
            self._live_view is None
            or self._live_view.status.terminal
            or generation != self._live_generation
            or self._live_identity is None
            or self._live_identity.session_id != session_id
        ):
            return
        self._live_snapshot = EphemeralSnapshot(
            generation=self._live_generation,
            request_index=liveness.request_index,
            request_elapsed_seconds=liveness.request_elapsed_seconds,
            last_activity_age_seconds=liveness.last_activity_age_seconds,
            transport_alive=liveness.transport_alive,
            watchdog_idle_seconds=liveness.watchdog_idle_seconds,
            received_monotonic=time.monotonic(),
        )
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
