"""Session workspace surface for the Agentic Debugger TUI.

WorkspaceScreen renders one SessionViewState in read-only REPLAY mode or
LIVE mode across seven tabs (Live/Evidence/Source/Debugger/Patch/Verifier/
Timeline); the jump-to-sequence and effort modals are only ever opened from
here.  The workspace never mutates domain state: replay navigation,
live refresh, retry, cancel, and apply-to-project all delegate to the
application-owned controllers.

CopyAllButton lives here because the workspace copy controls are its only
consumer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Tuple

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Static, TabPane, TabbedContent

from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    SessionStatus,
    SourceKind,
)
from agentic_debugger.application.history import SessionHistoryEntry
from agentic_debugger.application.live_execution import (
    ExecutionMode,
    LiveExecutionState,
    project_live_execution,
)
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    SessionViewState,
    current_source,
    reduce_event,
)
from agentic_debugger.application.replay import phase_boundaries
from agentic_debugger.application.session import SessionResult
from agentic_debugger.ui.models import LiveSessionRunner, ReplayController
from agentic_debugger.ui.screens_setup import StartSessionScreen
from agentic_debugger.ui.screens_shared import (
    REPLAY_FOOTER,
    REPLAY_FOOTER_COMPACT,
    WORKSPACE_FOOTER_ACTIVE,
    WORKSPACE_FOOTER_ACTIVE_COMPACT,
    WORKSPACE_FOOTER_IDLE,
    WORKSPACE_FOOTER_IDLE_COMPACT,
    HelpModalScreen,
    render_view_header,
)
from agentic_debugger.ui.theme import CANVAS, EVIDENCE, PRIMARY, SECONDARY, SUCCESS
from agentic_debugger.ui.widgets import (
    DebuggerPanel,
    EvidenceReviewPanel,
    EvidenceState,
    LiveBar,
    LivePanel,
    LiveRunContextPanel,
    PatchPanel,
    ReplayBar,
    SourcePanel,
    StatusHeader,
    TimelinePanel,
    VerifierPanel,
    WorkstreamPanel,
    live_export_text,
    timeline_export_text,
)


class CopyAllButton(Button):
    """Mouse-clickable copy control without adding a keyboard focus stop."""

    can_focus = False


class WorkspaceMode(str, Enum):
    REPLAY = "replay"
    LIVE = "live"


class WorkspaceScreen(Screen):
    """The core developer surface: one session, replay or live.

    Every pane renders the same ``SessionViewState`` produced by the shared
    pure reducer; the screen never mutates domain state and never executes
    domain logic.
    """

    BINDINGS = [
        Binding("left", "workspace_previous_view", "Previous view", priority=True),
        Binding("right", "workspace_next_view", "Next view", priority=True),
        Binding("]", "replay_next", "Next event"),
        Binding("[", "replay_previous", "Previous event"),
        Binding("}", "replay_next_phase", "Next phase"),
        Binding("{", "replay_previous_phase", "Previous phase"),
        Binding("g", "replay_begin", "Beginning"),
        Binding("G", "replay_end", "End"),
        Binding("j", "replay_jump", "Jump to sequence"),
        Binding("1", "select_tab_1", "Live", show=False),
        Binding("2", "select_tab_2", "Evidence", show=False),
        Binding("3", "select_tab_3", "Source", show=False),
        Binding("4", "select_tab_4", "Debugger", show=False),
        Binding("5", "select_tab_5", "Patch", show=False),
        Binding("6", "select_tab_6", "Verifier", show=False),
        Binding("7", "select_tab_7", "Timeline", show=False),
        Binding("c", "cancel_live", "Cancel session"),
        Binding("h", "history", "History", priority=True),
        Binding("n", "new_session", "New session", priority=True),
        Binding("a", "apply_to_project", "Apply To Project"),
        Binding("w", "show_effort", "What the agent tried"),
        Binding("r", "retry_session", "Retry session"),
        Binding("escape", "back_home", "Back", show=False),
        Binding("?", "show_help", "Help"),
    ]

    def __init__(
        self,
        *,
        mode: WorkspaceMode,
        controller: Optional[ReplayController] = None,
        entry: Optional[SessionHistoryEntry] = None,
        identity: Optional[PresentationIdentity] = None,
        view: Optional[SessionViewState] = None,
        runner: Optional[LiveSessionRunner] = None,
    ) -> None:
        super().__init__()
        self.mode = mode
        self.controller = controller
        self.entry = entry
        self._identity = identity
        self._runner = runner
        if mode is WorkspaceMode.REPLAY:
            if controller is None:
                raise ValueError("a replay workspace requires a ReplayController")
            self._view = controller.view
        else:
            if identity is None or view is None:
                raise ValueError("a live workspace requires identity and view")
            self._view = view
        self._live_events: Tuple[SessionEvent, ...] = ()
        self._live_last_sequence = -1
        self._live_terminal: Optional[SessionResult] = None
        self._live_failure: Optional[str] = None
        self._cancel_requested_ui = False
        self._cancel_active = False

    # -- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield StatusHeader(id="status-header")
        with Horizontal(id="workspace-body"):
            with Vertical(id="workspace-main"):
                with TabbedContent(id="pane-tabs"):
                    with TabPane("Live", id="tab-live"):
                        with Vertical(id="live-container"):
                            with Horizontal(id="live-copy-bar"):
                                yield CopyAllButton("Copy all", id="copy-live", classes="copy-button")
                            yield LivePanel(id="live-pane")
                    with TabPane("Evidence", id="tab-evidence"):
                        yield EvidenceReviewPanel(id="evidence-pane")
                    with TabPane("Source", id="tab-source"):
                        yield SourcePanel(id="source-pane")
                    with TabPane("Debugger", id="tab-debugger"):
                        yield DebuggerPanel(id="debugger-pane")
                    with TabPane("Patch", id="tab-patch"):
                        yield PatchPanel(id="patch-pane")
                    with TabPane("Verifier", id="tab-verifier"):
                        yield VerifierPanel(id="verifier-pane")
                    with TabPane("Timeline", id="tab-timeline"):
                        with Vertical(id="timeline-container"):
                            with Horizontal(id="timeline-copy-bar"):
                                yield CopyAllButton("Copy all", id="copy-timeline", classes="copy-button")
                            yield TimelinePanel(id="timeline-pane")
            if self.mode is WorkspaceMode.LIVE:
                yield LiveRunContextPanel(id="live-run-context")
        if self.mode is WorkspaceMode.REPLAY:
            yield ReplayBar(id="replay-bar")
        else:
            yield LiveBar(id="live-bar")

    def on_mount(self) -> None:
        if self.mode is WorkspaceMode.LIVE:
            # Live default view is tab-live
            try:
                tabs = self.query_one("#pane-tabs", TabbedContent)
                tabs.active = "tab-live"
            except Exception:
                pass
            # A live worker can surface its first events (or even its
            # terminal/failure) before this screen finishes mounting.  The
            # app-owned live state is authoritative, so catch up now; the
            # panes render whatever terminal/failure fields already exist.
            self.refresh_live()
            self.set_interval(0.5, self.refresh_live)
        else:
            self._render_all()
        self._update_live_context_visibility(self.size.width)

    def on_resize(self, event: Any) -> None:
        self._update_live_context_visibility(event.size.width)

    def _update_live_context_visibility(self, width: int) -> None:
        if self.mode is WorkspaceMode.LIVE and self.query("#live-run-context"):
            self.query_one("#live-run-context", LiveRunContextPanel).display = width >= 100

    def on_unmount(self) -> None:
        self.app.detach_live_workspace(self)
        # A live session may have just registered into app-owned history;
        # make the freshly visible home list show it.
        if self.mode is WorkspaceMode.LIVE:
            self.app.refresh_home_history()

    # -- live wiring --------------------------------------------------------

    def refresh_live(self) -> None:
        """Apply the app's current live presentation state and re-render."""
        if self.mode is not WorkspaceMode.LIVE:
            return
        events = self.app.live_events()
        for event in events:
            if event.sequence <= self._live_last_sequence:
                continue
            self._live_last_sequence = event.sequence
            self._view = reduce_event(self._view, event)
            # Compatibility-only cursor support; the durable reducer already
            # owns the full bounded presentation timeline.
            self._live_events = (self._live_events + (event,))[-2000:]
            # The visible cancelling state comes from the recorded
            # ``session.cancel_requested`` evidence, never from the key
            # press alone; the terminal still waits for worker evidence.
            if (
                event.event_kind is SessionEventKind.SESSION_CANCEL_REQUESTED
                and not self._view.status.terminal
            ):
                self._cancel_requested_ui = True
                self._cancel_active = False
        self._render_all()

    def show_live_terminal(self, result: SessionResult, registration_error: Optional[str]) -> None:
        if self.mode is not WorkspaceMode.LIVE:
            return
        self._live_terminal = result
        self._live_failure = registration_error
        if not self.is_mounted:
            # The terminal arrived before this screen finished mounting
            # (fast worker); ``on_mount`` renders the recorded fields.
            return
        self._render_all()
        if registration_error:
            self.notify(registration_error, severity="warning", title="History registration")

    def show_live_failure(self, diagnostic: str) -> None:
        if self.mode is not WorkspaceMode.LIVE:
            return
        self._live_failure = diagnostic
        if not self.is_mounted:
            # Same mount race as the terminal: ``on_mount`` renders it.
            return
        self._render_all()
        self.notify(diagnostic, severity="error", title="Live session")

    # -- rendering ----------------------------------------------------------

    def _mode_parts(self) -> tuple[str, str]:
        if self.mode is WorkspaceMode.LIVE:
            return "LIVE", f"bold {CANVAS} on {PRIMARY}"
        if self.entry is not None and self.entry.source_kind is not None and self.entry.source_kind.recorded:
            return "RECORDED", f"bold {CANVAS} on {SECONDARY}"
        return "REPLAY", f"bold {CANVAS} on {SUCCESS}"

    def _render_all(self) -> None:
        if not self.query("#status-header"):
            # Quit raced this screen's first mount (the app shut down before
            # the panes were composed); there is nothing left to render.
            return
        # Replay presentation always comes from the controller's reduced
        # view; the live path owns its own incremental view.
        view = (
            self.controller.view
            if self.mode is WorkspaceMode.REPLAY and self.controller is not None
            else self._view
        )
        mode, mode_style = self._mode_parts()
        position: Optional[str] = None
        extra: Optional[str] = None
        if self.mode is WorkspaceMode.REPLAY and self.controller is not None:
            if self.controller.at_beginning:
                position = (
                    f"position 0/{self.controller.total_events}"
                    "  ·  before first event  ·  read-only replay"
                )
            elif self.controller.at_end:
                position = (
                    f"event {self.controller.index}/{self.controller.total_events}"
                    "  ·  at end  ·  read-only replay"
                )
            else:
                position = (
                    f"event {self.controller.index}/{self.controller.total_events}"
                    "  ·  read-only replay"
                )
        rail_visible = (self.size.width >= 100) if (self.is_mounted and self.size.width > 0) else False
        if self.mode is WorkspaceMode.LIVE:
            if self._live_failure is not None and self._live_terminal is None:
                extra = "startup failed"
            elif self._cancel_requested_ui and self._live_terminal is None:
                extra = "cancel requested — waiting for worker cleanup"
            elif self._cancel_active and self._live_terminal is None:
                extra = "cancelling…"
            if (
                not rail_visible
                and view.model_provenance is not None
                and view.model_provenance.display_name
            ):
                model_extra = f"model: {view.model_provenance.display_name}"
                extra = f"{model_extra}  ·  {extra}" if extra else model_extra
        elapsed = self._live_elapsed()
        include_verifier = (not rail_visible) if mode == "LIVE" else True
        header = render_view_header(
            view, mode=mode, mode_style=mode_style,
            elapsed=elapsed,
            replay_position=position, extra=extra,
            include_verifier=include_verifier,
        )
        self.query_one("#status-header", StatusHeader).update(header)

        self.query_one("#evidence-pane", EvidenceReviewPanel).update_view(view)

        # Determine the domain evidence state for each pane
        is_live_running = (
            self.mode is WorkspaceMode.LIVE
            and self._live_terminal is None
            and self._live_failure is None
        )

        if self.mode is WorkspaceMode.REPLAY and self.controller is not None:
            events = self.controller.replay.events
            has_source = any(e.event_kind is SessionEventKind.SOURCE_SNAPSHOT for e in events)
            has_debugger = any(e.event_kind.value.startswith("debugger.") for e in events)
            has_patch = any(e.event_kind.value.startswith("patch.") for e in events)
            has_verifier = any(e.event_kind.value.startswith("verifier.") for e in events)

            source_state = (
                EvidenceState.AVAILABLE
                if current_source(view) is not None
                else (EvidenceState.REPLAY_PENDING if has_source else EvidenceState.SESSION_ABSENT)
            )
            debugger_state = (
                EvidenceState.AVAILABLE
                if view.debugger.session_started
                else (EvidenceState.REPLAY_PENDING if has_debugger else EvidenceState.SESSION_ABSENT)
            )
            patch_state = (
                EvidenceState.AVAILABLE
                if view.patch_attempts
                else (EvidenceState.REPLAY_PENDING if has_patch else EvidenceState.SESSION_ABSENT)
            )
            verifier_state = (
                EvidenceState.AVAILABLE
                if (view.verifier_summary is not None or view.verifier_stages)
                else (EvidenceState.REPLAY_PENDING if has_verifier else EvidenceState.SESSION_ABSENT)
            )
        elif self.mode is WorkspaceMode.LIVE:
            source_state = (
                EvidenceState.AVAILABLE
                if current_source(view) is not None
                else (EvidenceState.LIVE_PENDING if is_live_running else EvidenceState.SESSION_ABSENT)
            )
            debugger_state = (
                EvidenceState.AVAILABLE
                if view.debugger.session_started
                else (EvidenceState.LIVE_PENDING if is_live_running else EvidenceState.SESSION_ABSENT)
            )
            patch_state = (
                EvidenceState.AVAILABLE
                if view.patch_attempts
                else (EvidenceState.LIVE_PENDING if is_live_running else EvidenceState.SESSION_ABSENT)
            )
            verifier_state = (
                EvidenceState.AVAILABLE
                if (view.verifier_summary is not None or view.verifier_stages)
                else (EvidenceState.LIVE_PENDING if is_live_running else EvidenceState.SESSION_ABSENT)
            )
        else:
            source_state = EvidenceState.SESSION_ABSENT
            debugger_state = EvidenceState.SESSION_ABSENT
            patch_state = EvidenceState.SESSION_ABSENT
            verifier_state = EvidenceState.SESSION_ABSENT

        execution: Optional[LiveExecutionState]
        if self.mode is WorkspaceMode.LIVE:
            execution = self.app.live_execution_state()
        else:
            execution = project_live_execution(view, mode=ExecutionMode.REPLAY)

        if self.query("#live-pane"):
            self.query_one("#live-pane", LivePanel).update_view(view, execution_state=execution)
        if self.query("#evidence-pane"):
            self.query_one("#evidence-pane", EvidenceReviewPanel).update_view(view)
        if self.query("#source-pane"):
            self.query_one("#source-pane", SourcePanel).update_view(
                view, evidence_state=source_state
            )
        if self.query("#debugger-pane"):
            self.query_one("#debugger-pane", DebuggerPanel).update_view(
                view, evidence_state=debugger_state
            )
        if self.query("#patch-pane"):
            self.query_one("#patch-pane", PatchPanel).update_view(
                view, evidence_state=patch_state
            )
        if self.query("#verifier-pane"):
            self.query_one("#verifier-pane", VerifierPanel).update_view(
                view, evidence_state=verifier_state
            )
        if self.query("#timeline-pane"):
            boundaries = self._current_boundaries()
            self.query_one("#timeline-pane", TimelinePanel).update_view(view, boundaries)
        self._update_tab_labels(view)

        if execution is not None:
            if self.mode is WorkspaceMode.LIVE and self.query("#live-run-context"):
                self.query_one("#live-run-context", LiveRunContextPanel).update_execution(execution)
        self._render_bar()

    def _update_tab_labels(self, view: SessionViewState) -> None:
        try:
            tabs = self.query_one("#pane-tabs", TabbedContent)
            live_tab = tabs.get_tab("tab-live")
            if live_tab:
                live_tab.label = "Live •" if view.status is SessionStatus.RUNNING else "Live"
            ev_tab = tabs.get_tab("tab-evidence")
            if ev_tab:
                ev_tab.label = "Evidence"
            src_tab = tabs.get_tab("tab-source")
            if src_tab:
                src_tab.label = "Source"
            dbg_tab = tabs.get_tab("tab-debugger")
            if dbg_tab:
                dbg_tab.label = "Debugger •" if (view.pdb_observed or view.debugger.session_started) else "Debugger"
            patch_tab = tabs.get_tab("tab-patch")
            if patch_tab:
                patch_tab.label = f"Patch ({len(view.patch_attempts)})" if view.patch_attempts else "Patch"
            ver_tab = tabs.get_tab("tab-verifier")
            if ver_tab:
                if view.verifier_summary is not None:
                    ver_tab.label = "Verifier ✓" if (view.verifier_summary.outcome is not None and getattr(view.verifier_summary.outcome, "value", str(view.verifier_summary.outcome)) == "RESOLVED") else "Verifier"
                elif view.verifier_stages:
                    ver_tab.label = "Verifier •"
                else:
                    ver_tab.label = "Verifier"
            time_tab = tabs.get_tab("tab-timeline")
            if time_tab:
                time_tab.label = "Timeline"
        except Exception:
            pass

    def _retry_footer_hint(self) -> str:
        return "   r retry" if self._retry_available() else ""

    def _terminal_effort_phrase(self) -> Optional[str]:
        """One-line counted effort shown once the session is terminal.

        Derived from the same journal projection as the ``w`` modal; empty
        while the session still runs.
        """
        if self._live_terminal is None and self._live_failure is None:
            return None
        from agentic_debugger.application.effort_summary import summarize_events

        summary = summarize_events(self._session_events_for_effort())
        parts = [
            f"tried: {summary.model_requests} req",
            f"{summary.directives_accepted} directives",
        ]
        if summary.tool_calls:
            parts.append(f"{summary.tool_calls} tools")
        if summary.patches_proposed:
            parts.append(f"{summary.patches_proposed} patch")
        if summary.debugger_observations:
            parts.append(f"{summary.debugger_observations} pdb obs")
        return f"[bold {EVIDENCE}]" + ", ".join(parts) + "[/]"

    def _live_elapsed(self) -> str:
        events = self._live_events or (self.app.live_events() if hasattr(self.app, "live_events") else ())
        if not events:
            return "—"
        try:
            started = None
            for e in events:
                if e.timestamp_utc:
                    started = datetime.fromisoformat(e.timestamp_utc.replace("Z", "+00:00"))
                    break
            if started is None:
                return "—"
            if self._live_terminal is not None:
                ended = None
                for e in reversed(events):
                    if e.timestamp_utc:
                        ended = datetime.fromisoformat(e.timestamp_utc.replace("Z", "+00:00"))
                        break
                if ended is None:
                    ended = datetime.now(timezone.utc)
            else:
                ended = datetime.now(timezone.utc)
            seconds = max(0, int((ended - started).total_seconds()))
            return f"{seconds // 60:02d}:{seconds % 60:02d}"
        except Exception:
            return "—"

    def _current_boundaries(self) -> frozenset[int]:
        if self.mode is WorkspaceMode.REPLAY and self.controller is not None:
            return frozenset(
                self.controller.replay.events[i].sequence
                for i in self.controller.phase_boundaries
            )
        return frozenset(
            self._live_events[i].sequence for i in phase_boundaries(self._live_events)
        )

    def _render_bar(self) -> None:
        compact = (self.size.width > 0 and self.size.width < 80) if self.is_mounted else False
        if self.mode is WorkspaceMode.REPLAY:
            bar = self.query_one("#replay-bar", ReplayBar)
            if self.controller is None:
                bar.update("")
                return
            footer = REPLAY_FOOTER_COMPACT if compact else REPLAY_FOOTER
            if self._view.source_kind is SourceKind.LOCAL_PROJECT and self.size.width >= 100:
                try:
                    _, apply_patch = self._local_project_apply_candidate()
                except Exception:
                    apply_patch = None
                if apply_patch:
                    footer = (
                        "left/right views   1-7 tabs   events   phases   "
                        "a apply to project   h history   n new session   ctrl+c quit"
                    )
            bar.update(
                f"[dim]{footer}   ? help[/]"
            )
        else:
            bar = self.query_one("#live-bar", LiveBar)
            if (
                not self._view.status.terminal
                and self._live_terminal is None
                and self._live_failure is None
            ):
                footer = WORKSPACE_FOOTER_ACTIVE_COMPACT if compact else WORKSPACE_FOOTER_ACTIVE
                bar.update(f"[dim]{footer}   ? help[/]")
            else:
                footer = WORKSPACE_FOOTER_IDLE_COMPACT if compact else WORKSPACE_FOOTER_IDLE
                if self._view.source_kind is SourceKind.LOCAL_PROJECT:
                    try:
                        _, apply_patch = self._local_project_apply_candidate()
                    except Exception:
                        apply_patch = None
                    if apply_patch:
                        footer = (
                            "left/right views   1-7 tabs   "
                            "a apply to project   h history   n new session   w effort   r retry   ctrl+c quit"
                        )
                effort_phrase = self._terminal_effort_phrase()
                if effort_phrase and not compact:
                    footer = f"{effort_phrase}   {footer}"
                if not compact:
                    footer = footer.replace(
                        "   r retry",
                        self._retry_footer_hint(),
                        1,
                    ) if self._retry_footer_hint() else footer.replace("   r retry", "", 1)
                bar.update(f"[dim]{footer}   ? help[/]")

    # -- workspace view navigation ------------------------------------------

    _VIEW_IDS = (
        "tab-live",
        "tab-evidence",
        "tab-source",
        "tab-debugger",
        "tab-patch",
        "tab-verifier",
        "tab-timeline",
    )

    def _switch_workspace_view(self, offset: int) -> None:
        """Switch views from the screen so child focus cannot swallow arrows."""
        tabs = self.query_one("#pane-tabs", TabbedContent)
        try:
            current = self._VIEW_IDS.index(tabs.active)
        except ValueError:
            current = 0 if offset > 0 else len(self._VIEW_IDS) - 1
        target = (current + offset) % len(self._VIEW_IDS)
        tabs.active = self._VIEW_IDS[target]
        # Keep focus in the newly visible scrollable pane for coherent
        # Up/Down behavior and immediate repeated Left/Right navigation.
        tabs.get_pane(self._VIEW_IDS[target]).focus()

    def action_workspace_previous_view(self) -> None:
        self._switch_workspace_view(-1)

    def action_workspace_next_view(self) -> None:
        self._switch_workspace_view(1)

    # -- effort + retry actions ---------------------------------------------

    def _session_events_for_effort(self) -> tuple:
        if self.mode is WorkspaceMode.REPLAY and self.controller is not None:
            return tuple(self.controller.replay.events)
        return tuple(self._live_events)

    def action_show_effort(self) -> None:
        """Show the counted 'what the agent tried' projection as a modal."""
        from agentic_debugger.application.effort_summary import (
            render_effort_summary,
            summarize_events,
        )

        summary = summarize_events(self._session_events_for_effort())
        title = "What the agent tried"
        if self.mode is WorkspaceMode.LIVE and self._live_terminal is not None:
            status = getattr(self._live_terminal, "status", None)
            reason = getattr(self._live_terminal, "termination_reason", None)
            if status is not None:
                title += f"  ·  terminal: {status.value}"
                if reason is not None:
                    title += f" ({reason.value})"
        body = render_effort_summary(summary, title=title)
        self.app.push_screen(EffortModalScreen(body))

    def _retry_available(self) -> bool:
        """Whether the retry action targets the session this screen shows.

        The app-global retry request belongs to the most recent captured
        LIVE session.  Replay workspaces display a different (recorded)
        session and must never invoke it: retry is only offered while the
        terminal LIVE workspace for the very session the request captured
        is the one visible here.  The capture stores the session id, so
        the comparison is exact and identity-based.
        """
        if self.mode is not WorkspaceMode.LIVE:
            return False
        if self._live_terminal is None:
            return False
        request = getattr(self.app, "_live_retry_request", None)
        if not request:
            return False
        captured = request.get("session_id")
        if not captured:
            return False
        identity = self._identity
        return identity is not None and identity.session_id == captured

    def action_retry_session(self) -> None:
        """Restart the session this screen shows, linked to the original."""
        if not self._retry_available():
            self.notify(
                "Retry is only available for the terminal live session "
                "currently displayed.",
                severity="warning",
                title="Retry",
            )
            return
        retried = self.app.retry_live_session()
        if not retried:
            self.notify(
                "Retry unavailable: a session may be active, or this session "
                "type does not support retry.",
                severity="warning",
                title="Retry",
            )

    # -- replay actions -----------------------------------------------------

    def action_replay_next(self) -> None:
        if self.controller is None:
            return
        self.controller.next()
        self._render_all()

    def action_replay_previous(self) -> None:
        if self.controller is None:
            return
        self.controller.previous()
        self._render_all()

    def action_replay_next_phase(self) -> None:
        if self.controller is None:
            return
        if not self.controller.next_phase():
            self.notify("Already at the last phase boundary.", severity="information")
        self._render_all()

    def action_replay_previous_phase(self) -> None:
        if self.controller is None:
            return
        if not self.controller.previous_phase():
            self.notify("Already at the first phase boundary.", severity="information")
        self._render_all()

    def action_replay_begin(self) -> None:
        if self.controller is None:
            return
        self.controller.begin()
        self._render_all()

    def action_replay_end(self) -> None:
        if self.controller is None:
            return
        self.controller.end()
        self._render_all()

    def action_replay_jump(self) -> None:
        if self.controller is None:
            return
        events = self.controller.replay.events
        min_seq = events[0].sequence if events else 0
        max_seq = events[-1].sequence if events else None
        self.app.push_screen(
            JumpToSequenceScreen(
                self._jump_to_sequence,
                min_sequence=min_seq,
                max_sequence=max_seq,
            )
        )

    def _jump_to_sequence(self, sequence: Optional[int]) -> None:
        if sequence is None or self.controller is None:
            return
        if not self.controller.seek_sequence(sequence):
            self.notify(f"No event with sequence {sequence}.", severity="warning")
        self._render_all()

    # -- live actions -------------------------------------------------------

    def action_cancel_live(self) -> None:
        if self.mode is not WorkspaceMode.LIVE or self._runner is None:
            return
        if self._live_terminal is not None:
            self.notify("The session already finished.", severity="information")
            return
        # The user-facing request exists as soon as this action is accepted.
        # Mark it before dispatch so a fast worker cannot deliver the durable
        # cancel event and terminal in one UI batch without ever rendering the
        # truthful intermediate state.
        self._cancel_requested_ui = True
        self._cancel_active = True
        self._render_all()
        self._runner.cancel()

    def _full_session_view(self) -> Optional[SessionViewState]:
        """The session-final view for owner-facing decisions.

        LIVE mode tracks the terminal-updated view already; REPLAY mode must
        reduce the complete recorded stream, because the replay cursor's
        prefix view is navigation state, never session-final truth.
        """
        if self.mode is WorkspaceMode.REPLAY and self.controller is not None:
            from agentic_debugger.application.presentation import (
                initial_session_view,
                reduce_event,
            )

            view = initial_session_view(self.controller.identity)
            for event in self.controller.replay.events:
                view = reduce_event(view, event)
            return view
        return self._view

    def _local_project_apply_candidate(
        self,
    ) -> tuple[Optional[SessionViewState], Optional[str]]:
        """(session-final view, active candidate patch text) or reasons."""
        view = self._full_session_view()
        if view is None or view.source_kind is not SourceKind.LOCAL_PROJECT:
            return view, None
        if not view.status.terminal:
            return view, None
        from agentic_debugger.application.presentation import active_candidate_attempt

        attempt = active_candidate_attempt(view)
        if attempt is None or not attempt.patch_text:
            return view, None
        return view, attempt.patch_text

    def _session_directory(self) -> Optional[Path]:
        if self._runner is not None:
            try:
                return Path(self._runner.worker.session_dir)
            except Exception:
                return None
        if self.entry is not None and self.entry.directory:
            return Path(self.entry.directory)
        return None

    def _local_project_apply_proof(
        self,
        view: SessionViewState,
        patch_text: str,
    ) -> tuple[Optional[Path], Optional[str], str]:
        """Resolve and validate the independent certificate for one Apply.

        Old sessions without the versioned certificate remain inspectable but
        are deliberately not applyable.  A terminal/session-success claim is
        insufficient: the independent verifier must have returned RESOLVED,
        and its certificate must match both the recorded source HEAD and the
        exact candidate bytes.
        """
        summary = view.verifier_summary
        if (
            summary is None
            or summary.status != "COMPLETED"
            or summary.outcome is None
            or summary.outcome.value != "RESOLVED"
        ):
            return None, None, "candidate is not independently verified as RESOLVED"
        session_dir = self._session_directory()
        if session_dir is None:
            return None, None, "session artifact directory is unavailable"
        try:
            from agentic_debugger.application.local_project import (
                check_verification_certificate,
                load_apply_verification_materials,
                local_project_task_spec_sha256,
            )

            task, certificate = load_apply_verification_materials(session_dir)
        except FileNotFoundError:
            return None, None, "independent verification certificate is missing"
        except Exception as exc:
            return None, None, f"independent verification certificate is invalid: {exc}"
        ok, reason = check_verification_certificate(
            certificate,
            expected_task_id=view.task_id,
            expected_session_id=view.session_id or "",
            expected_task_spec_sha256=local_project_task_spec_sha256(task),
            expected_head=task.source_head_commit,
            patch_text=patch_text,
        )
        if not ok:
            return None, None, reason
        return Path(task.source_repo_path), task.source_head_commit, "verified"

    def check_action(self, action: str, parameters: tuple[Any, ...]) -> bool | None:
        if action == "cancel_live":
            return bool(
                self.mode is WorkspaceMode.LIVE
                and self._runner is not None
                and self._live_terminal is None
                and self._live_failure is None
                and not self._view.status.terminal
            )
        if action == "apply_to_project":
            # Only a finished Local Project Debug session with an active
            # candidate and an exact, RESOLVED independent-verifier proof.
            if self.mode is not WorkspaceMode.LIVE and self.controller is None:
                return False
            view, patch_text = self._local_project_apply_candidate()
            if view is None or view.source_kind is not SourceKind.LOCAL_PROJECT:
                return False
            if patch_text is None:
                return False
            repo_path, expected_head, _ = self._local_project_apply_proof(
                view, patch_text
            )
            return repo_path is not None and expected_head is not None
        return True

    def action_apply_to_project(self) -> None:
        """Explicit Apply To Project with closed safety gates (no commit).

        The candidate is the session-ledger's ACTIVE attempt (an applied
        candidate that was not later reverted); proposed/rejected/failed
        bodies are never applied to the owner project.  The gates and the
        apply itself run off the UI event loop.
        """
        view, patch_text = self._local_project_apply_candidate()
        if view is None or view.source_kind is not SourceKind.LOCAL_PROJECT:
            self.notify("Apply To Project is only for Local Project Debug sessions.", severity="warning")
            return
        if not view.status.terminal:
            self.notify(
                "The session is still running — wait for it to finish before applying.",
                severity="warning",
            )
            return
        if not patch_text:
            self.notify("No active candidate patch available to apply.", severity="warning")
            return
        repo_path, expected_head, proof_reason = self._local_project_apply_proof(
            view, patch_text
        )
        if repo_path is None or expected_head is None:
            self.notify(
                f"Apply To Project blocked: {proof_reason}.",
                severity="error",
            )
            return
        self.notify("Checking Apply gates (HEAD, clean tree, patch fit)…", timeout=3.0)
        # Pass the callable itself: evaluating
        # ``self._apply_to_project_worker(...)`` here would run the whole
        # gate/apply chain synchronously on the UI event-loop thread before
        # ``run_worker`` ever saw it.
        self.run_worker(
            lambda: self._apply_to_project_worker(repo_path, expected_head, patch_text),
            thread=True,
            exclusive=True,
            group="apply-to-project",
        )

    def _apply_to_project_worker(
        self, repo_path: Path, expected_head: str, patch_text: str
    ) -> None:
        """Gate + apply off the UI loop; report through the event loop."""
        from agentic_debugger.application.local_project import (
            apply_patch_to_project,
            check_apply_gates,
        )

        try:
            ok, reason = check_apply_gates(repo_path, expected_head, patch_text)
            if not ok:
                self._report_apply_outcome(False, f"Apply To Project blocked: {reason}")
                return
            success, msg = apply_patch_to_project(
                repo_path,
                patch_text,
                expected_head=expected_head,
            )
            self._report_apply_outcome(success, msg if success else f"Apply failed: {msg}")
        except Exception as exc:
            self._report_apply_outcome(False, f"Apply To Project failed: {exc}")

    def _report_apply_outcome(self, success: bool, message: str) -> None:
        try:
            # Only App provides call_from_thread; a Screen/DOMNode has no
            # such method, so the marshal must go through the running app.
            self.app.call_from_thread(
                lambda: self.notify(message, severity="information" if success else "error")
            )
        except Exception:
            pass

    def live_cancel_event_seen(self) -> None:
        self._cancel_requested_ui = True
        self._render_all()

    # -- common actions -----------------------------------------------------

    def action_back_home(self) -> None:
        self.app.go_home()

    def action_history(self) -> None:
        self.app.go_home()

    def action_new_session(self) -> None:
        self.app.push_screen(
            StartSessionScreen(task_options=list(self.app.curated_task_options()))
        )

    def action_show_help(self) -> None:
        self.app.push_screen(HelpModalScreen())

    # -- direct tab navigation ----------------------------------------------

    def _select_tab_index(self, index: int) -> None:
        if 0 <= index < len(self._VIEW_IDS):
            tabs = self.query_one("#pane-tabs", TabbedContent)
            tab_id = self._VIEW_IDS[index]
            tabs.active = tab_id
            try:
                tabs.get_pane(tab_id).focus()
            except Exception:
                pass

    def action_select_tab_1(self) -> None:
        self._select_tab_index(0)

    def action_select_tab_2(self) -> None:
        self._select_tab_index(1)

    def action_select_tab_3(self) -> None:
        self._select_tab_index(2)

    def action_select_tab_4(self) -> None:
        self._select_tab_index(3)

    def action_select_tab_5(self) -> None:
        self._select_tab_index(4)

    def action_select_tab_6(self) -> None:
        self._select_tab_index(5)

    def action_select_tab_7(self) -> None:
        self._select_tab_index(6)

    # -- copy all -----------------------------------------------------------

    def _current_view_for_copy(self) -> Optional[SessionViewState]:
        """Durable view for copy: replay controller or live incremental view."""
        if self.mode is WorkspaceMode.REPLAY and self.controller is not None:
            return self.controller.view
        return self._view

    def _copy_to_clipboard(self, text: str, success_message: str) -> None:
        """Clipboard write without new dependency; failure is non-fatal."""
        try:
            # Prefer Textual's OSC-52 clipboard; fall back to no-op if unavailable.
            self.app.copy_to_clipboard(text)
            self.notify(success_message, timeout=2.0)
        except Exception:
            # Clipboard failure must be non-fatal and must not mutate history.
            self.notify("Copy failed — clipboard unavailable", severity="warning", timeout=3.0)

    def _live_copy_text(self) -> str:
        view = self._current_view_for_copy()
        if view is None:
            return "No operational activity recorded."
        from agentic_debugger.ui.widgets import live_export_text

        return live_export_text(view)

    def _timeline_copy_text(self) -> str:
        view = self._current_view_for_copy()
        if view is None:
            return "No events recorded."
        # Preserve phase boundaries as displayed.
        boundaries = self._current_boundaries()
        from agentic_debugger.ui.widgets import timeline_export_text

        return timeline_export_text(view, phase_boundary_sequences=boundaries)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Mouse-clickable COPY ALL without keyboard focus trap; no global shortcut.
        # Do NOT journal the copy action; do NOT mutate session history.
        if event.button.id == "copy-live":
            text = self._live_copy_text()
            view = self._current_view_for_copy()
            count = len(view.workstream) if view is not None else 0
            success = f"Copied {count} live events" if count != 1 else "Copied 1 live event"
            self._copy_to_clipboard(text, success)
            event.stop()
        elif event.button.id == "copy-timeline":
            text = self._timeline_copy_text()
            view = self._current_view_for_copy()
            count = len(view.timeline) if view is not None else 0
            success = f"Copied {count} timeline events" if count else "Copied timeline"
            self._copy_to_clipboard(text, success)
            event.stop()


class JumpToSequenceScreen(Screen):
    """One compact input modal for jumping to a replay sequence."""

    BINDINGS = [Binding("escape", "cancel", "Back")]

    def __init__(
        self,
        on_submit: Any,
        min_sequence: int = 0,
        max_sequence: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._on_submit = on_submit
        self._min_sequence = min_sequence
        self._max_sequence = max_sequence

    def compose(self) -> ComposeResult:
        with Static(id="jump-dialog"):
            yield Static(f"[bold {PRIMARY}]Jump to sequence[/]", id="jump-title")
            placeholder = (
                f"sequence number ({self._min_sequence}–{self._max_sequence})"
                if self._max_sequence is not None
                else "sequence number"
            )
            yield Input(id="jump-input", placeholder=placeholder)
            hint = (
                f"[dim]enter: jump ({self._min_sequence}–{self._max_sequence}) · escape: cancel[/]"
                if self._max_sequence is not None
                else "[dim]enter: jump · escape: cancel[/]"
            )
            yield Static(hint, id="jump-hint")

    def on_mount(self) -> None:
        self.query_one("#jump-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "jump-input":
            return
        raw = event.input.value.strip()
        sequence: Optional[int] = None
        if raw:
            try:
                sequence = int(raw)
            except ValueError:
                self.notify("Sequence must be a whole number.", severity="warning")
                return
        self._on_submit(sequence)
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()


class EffortModalScreen(Screen):
    """Read-only 'what the agent tried' projection modal."""

    BINDINGS = [
        Binding("escape", "close_effort", "Close"),
        Binding("enter", "close_effort", "Close"),
        Binding("w", "close_effort", "Close"),
    ]

    def __init__(self, body: str) -> None:
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="effort-dialog"):
            yield Static(self._body, id="effort-body")

    def action_close_effort(self) -> None:
        self.app.pop_screen()
