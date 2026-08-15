"""Screens of the Local Application V1 TUI.

Screens are presentation-only.  The home screen exposes app-owned history
through the accepted :class:`HistoryStore`; the workspace renders one
:class:`SessionViewState` in either read-only REPLAY mode or LIVE mode; the
start-session screen is the only place a bounded new deterministic session
may be requested.  No screen executes controller, PDB, patch, verifier, or
model work.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional, Tuple

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    Select,
    Static,
    TabPane,
    TabbedContent,
)

from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    SessionPhase,
    SessionStatus,
)
from agentic_debugger.application.history import (
    HistoryClassification,
    ReopenedSession,
    SessionHistoryEntry,
)
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    SessionViewState,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.application.replay import phase_boundaries
from agentic_debugger.application.session import SessionResult
from agentic_debugger.application.worker_process import SessionWorkerProcess
from agentic_debugger.ui.models import LiveSessionRunner, ReplayController
from agentic_debugger.ui.widgets import (
    ActivityPanel,
    DebuggerPanel,
    LiveBar,
    PatchPanel,
    ReplayBar,
    SourcePanel,
    StatusHeader,
    TimelinePanel,
    VerifierPanel,
)

_TERMINAL_KINDS = frozenset(
    {
        SessionEventKind.SESSION_COMPLETED,
        SessionEventKind.SESSION_FAILED,
        SessionEventKind.SESSION_CANCELLED,
    }
)

_CLASSIFICATION_STYLE = {
    HistoryClassification.COMPLETE: "green",
    HistoryClassification.INTERRUPTED: "yellow",
    HistoryClassification.MALFORMED: "red",
    HistoryClassification.INVALID_MANIFEST: "red",
    HistoryClassification.UNREGISTERED: "dim",
}


def _markup_escape(value: Any) -> str:
    return str(value).replace("[", "\\[").replace("]", "\\]")


def render_view_header(
    view: SessionViewState,
    *,
    mode: str,
    mode_style: str,
    replay_position: Optional[str] = None,
    extra: Optional[str] = None,
) -> Text:
    """One compact two-line header derived from the presentation view."""
    head = Text()
    head.append(f"[{mode_style}]{mode}[/] ", style="bold")
    head.append(_markup_escape(view.session_id or "session-unbound"), style="bold")
    head.append(f"  ·  task {_markup_escape(view.task_id)}")
    head.append(f"  ·  {_markup_escape(view.source_kind.value)}")
    head.append("\n")
    status_style = {
        SessionStatus.RUNNING: "bold blue",
        SessionStatus.STARTING: "blue",
        SessionStatus.SUCCEEDED: "bold green",
        SessionStatus.UNRESOLVED: "yellow",
        SessionStatus.FAILED: "bold red",
        SessionStatus.CANCELLED: "bold yellow",
        SessionStatus.TIMED_OUT: "bold red",
        SessionStatus.INTERRUPTED: "bold red",
        SessionStatus.CLEANUP_FAILED: "bold red",
        SessionStatus.CREATED: "dim",
    }.get(view.status, "default")
    status_text = view.status.value
    if view.status is SessionStatus.RUNNING and view.phase is not None:
        status_text += f"/{view.phase.value}"
    if view.status.terminal and view.termination_reason is not None:
        status_text += f" ({view.termination_reason.value})"
    head.append(status_text, style=status_style)
    if view.controller_phase is not None:
        head.append(f"  ·  controller: {_markup_escape(view.controller_phase.value)}")
    verifier = ""
    if view.verifier_summary is not None:
        summary = view.verifier_summary
        verifier = f"{summary.status or '?'}/{summary.outcome.value if summary.outcome else '?'}"
        if summary.f2p_total is not None:
            verifier += f" f2p {summary.f2p_passed}/{summary.f2p_total}"
    elif view.verifier_stages:
        verifier = "running"
    head.append(f"  ·  verifier: {verifier or '—'}")
    if view.run_id is not None:
        head.append(f"  ·  run {_markup_escape(view.run_id)}")
    if replay_position is not None:
        head.append(f"  ·  {replay_position}")
    if extra is not None:
        head.append(f"  ·  {extra}")
    return head


class HomeScreen(Screen):
    """App-owned run history: the primary navigation surface."""

    BINDINGS = [
        Binding("n", "start_session", "New session"),
        Binding("o", "open_selected", "Open"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit_app", "Quit"),
        Binding("?", "show_help", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #58a6ff]Agentic Debugging — Local Application V1[/]\n"
            "[dim]App-owned session history[/]",
            id="home-title",
        )
        yield Static("", id="home-empty", classes="empty-state")
        yield DataTable(id="history-table")
        yield Static(
            "[dim]new: [bold]n[/]   open: [bold]o[/]/[bold]enter[/]   "
            "refresh: [bold]r[/]   quit: [bold]q[/]   help: [bold]?[/][/]",
            id="home-hint",
        )

    def on_mount(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "State", "Session", "Task", "Source", "Started", "Ended",
            "Status", "Verifier", "Note",
        )
        self.refresh_history()

    def refresh_history(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        entries = self.app.history_store.list_sessions()
        empty = self.query_one("#home-empty", Static)
        if not entries:
            empty.update(
                "[dim]No app-owned sessions yet.  Press [bold]n[/] to start a "
                "deterministic offline session, or [bold]r[/] to refresh.[/]"
            )
            empty.display = True
        else:
            empty.display = False
        for entry in entries:
            table.add_row(
                Text(entry.classification.value, style=_CLASSIFICATION_STYLE.get(
                    entry.classification, "default")),
                Text(entry.session_id or "—"),
                Text(entry.task_id or "—"),
                Text(entry.source_kind.value if entry.source_kind else "—"),
                Text(entry.started_at_utc or "—"),
                Text(entry.ended_at_utc or "—"),
                Text(entry.status.value if entry.status else "—"),
                Text(verifier_cell(entry)),
                Text(entry.note or ""),
                key=entry.session_id or entry.directory or "",
            )

    def _selected_entry(self) -> Optional[SessionHistoryEntry]:
        table = self.query_one("#history-table", DataTable)
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        if row_key is None or row_key.value is None:
            return None
        for entry in self.app.history_store.list_sessions():
            if (entry.session_id or entry.directory or "") == row_key.value:
                return entry
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter selects a row (the DataTable owns the enter binding)."""
        self.action_open_selected()

    def action_start_session(self) -> None:
        self.app.push_screen(StartSessionScreen())

    def action_open_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify("Select a session row first.", severity="warning")
            return
        if entry.classification is HistoryClassification.MALFORMED:
            self.notify(
                f"Session {entry.session_id} has a malformed journal and "
                "cannot be replayed.", severity="error"
            )
            return
        if entry.classification is HistoryClassification.INVALID_MANIFEST:
            self.notify(
                f"Session {entry.session_id} has an invalid manifest; "
                "it cannot be opened as a valid session.",
                severity="error",
            )
            return
        self.app.open_session(entry.session_id or "")

    def action_refresh(self) -> None:
        self.refresh_history()
        self.notify("History refreshed.")

    def action_quit_app(self) -> None:
        self.app.exit()

    def action_show_help(self) -> None:
        self.notify(
            "n new session · o/enter open replay · r refresh · q quit. "
            "Replay is always read-only.",
            title="Home",
        )


def verifier_cell(entry: SessionHistoryEntry) -> str:
    if entry.verifier_status or entry.verifier_outcome:
        return f"{entry.verifier_status or '?'}/{entry.verifier_outcome or '?'}"
    return "—"


class StartSessionScreen(Screen):
    """Bounded start of one deterministic offline session."""

    BINDINGS = [Binding("escape", "cancel", "Back")]

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #58a6ff]Start deterministic session[/]\n"
            "[dim]Offline demo source: real controller, PDB, patch manager "
            "and independent verifier. No provider, no network.[/]",
            id="start-title",
        )
        yield Label("Task")
        yield Select(id="task-select", allow_blank=False)
        yield Label("Policy")
        yield Select(
            id="policy-select",
            options=[
                ("static baseline (debugger disabled)", "static-baseline"),
                ("pdb on uncertainty (debugger gated)", "pdb-on-uncertainty"),
            ],
            allow_blank=False,
            value="pdb-on-uncertainty",
        )
        yield Label("Elapsed budget (seconds, optional)")
        yield Input(id="elapsed-input", placeholder="empty = no limit", type="integer")
        yield Button("Start session", id="start-button", variant="primary")
        yield Static("", id="start-error")
        yield Static(
            "[dim]escape: back to history[/]", id="start-hint"
        )

    def on_mount(self) -> None:
        options = []
        for task_id in self.app.curated_task_ids():
            options.append((task_id, task_id))
        select = self.query_one("#task-select", Select)
        select.set_options(options)
        if options:
            select.value = options[0][1]

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-button":
            self._start()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "elapsed-input":
            self._start()

    def _start(self) -> None:
        task_select = self.query_one("#task-select", Select)
        policy_select = self.query_one("#policy-select", Select)
        elapsed_input = self.query_one("#elapsed-input", Input)
        error = self.query_one("#start-error", Static)
        if task_select.value is Select.BLANK:
            error.update("[red]choose a task[/]")
            return
        max_elapsed: Optional[int] = None
        raw = elapsed_input.value.strip()
        if raw:
            try:
                max_elapsed = int(raw)
            except ValueError:
                error.update("[red]elapsed budget must be a whole number of seconds[/]")
                return
            if max_elapsed < 1:
                error.update("[red]elapsed budget must be at least 1 second[/]")
                return
        error.update("")
        try:
            self.app.start_live_session(
                task_id=str(task_select.value),
                policy=str(policy_select.value),
                max_elapsed_seconds=max_elapsed,
            )
        except Exception as exc:
            error.update(f"[red]{_markup_escape(exc)}[/]")


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
        Binding("]", "replay_next", "Next event"),
        Binding("[", "replay_previous", "Previous event"),
        Binding("}", "replay_next_phase", "Next phase"),
        Binding("{", "replay_previous_phase", "Previous phase"),
        Binding("g", "replay_begin", "Beginning"),
        Binding("G", "replay_end", "End"),
        Binding("j", "replay_jump", "Jump to sequence"),
        Binding("1", "filter_all", "Filter: all", show=False),
        Binding("2", "filter_lifecycle", "Filter: lifecycle", show=False),
        Binding("3", "filter_controller", "Filter: controller", show=False),
        Binding("4", "filter_model", "Filter: model", show=False),
        Binding("5", "filter_debugger", "Filter: debugger", show=False),
        Binding("6", "filter_patch", "Filter: patch", show=False),
        Binding("7", "filter_verifier", "Filter: verifier", show=False),
        Binding("c", "cancel_live", "Cancel session"),
        Binding("q", "back_home", "Back to history"),
        Binding("escape", "back_home", "Back", show=False),
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
        with TabbedContent(id="pane-tabs"):
            with TabPane("Source", id="tab-source"):
                yield SourcePanel(id="source-pane")
            with TabPane("Debugger", id="tab-debugger"):
                yield DebuggerPanel(id="debugger-pane")
            with TabPane("Patch", id="tab-patch"):
                yield PatchPanel(id="patch-pane")
            with TabPane("Verifier", id="tab-verifier"):
                yield VerifierPanel(id="verifier-pane")
            with TabPane("Activity", id="tab-activity"):
                yield ActivityPanel(id="activity-pane")
            with TabPane("Timeline", id="tab-timeline"):
                yield TimelinePanel(id="timeline-pane")
        if self.mode is WorkspaceMode.REPLAY:
            yield ReplayBar(id="replay-bar")
        else:
            yield LiveBar(id="live-bar")

    def on_mount(self) -> None:
        self._render_all()

    def on_unmount(self) -> None:
        self.app.detach_live_workspace(self)

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
            self._live_events = self._live_events + (event,)
        self._render_all()

    def show_live_terminal(self, result: SessionResult, registration_error: Optional[str]) -> None:
        if self.mode is not WorkspaceMode.LIVE:
            return
        self._live_terminal = result
        self._live_failure = registration_error
        self._render_all()
        if registration_error:
            self.notify(registration_error, severity="warning", title="History registration")

    def show_live_failure(self, diagnostic: str) -> None:
        if self.mode is not WorkspaceMode.LIVE:
            return
        self._live_failure = diagnostic
        self._render_all()
        self.notify(diagnostic, severity="error", title="Live session")

    def live_cancelling(self) -> None:
        self._cancel_requested_ui = True
        self._render_all()

    # -- rendering ----------------------------------------------------------

    def _mode_parts(self) -> tuple[str, str]:
        if self.mode is WorkspaceMode.LIVE:
            return "LIVE", "bold white on #1f6feb"
        if self.entry is not None and self.entry.source_kind is not None and self.entry.source_kind.recorded:
            return "RECORDED", "bold white on #8957e5"
        return "REPLAY", "bold white on #238636"

    def _render_all(self) -> None:
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
            position = (
                f"{self.controller.index}/{self.controller.total_events} events"
                "  ·  read-only replay"
            )
            if self.controller.at_end:
                position += "  ·  at end"
        if self.mode is WorkspaceMode.LIVE:
            if self._live_terminal is not None:
                extra = "session finished — q returns to history"
            elif self._live_failure is not None:
                extra = "startup failed"
            elif self._cancel_requested_ui:
                extra = "cancel requested — waiting for worker cleanup"
            elif self._cancel_active:
                extra = "cancelling…"
            else:
                extra = "live"
        header = render_view_header(
            view, mode=mode, mode_style=mode_style,
            replay_position=position, extra=extra,
        )
        self.query_one("#status-header", StatusHeader).update(header)
        self.query_one("#source-pane", SourcePanel).update_view(view)
        self.query_one("#debugger-pane", DebuggerPanel).update_view(view)
        self.query_one("#patch-pane", PatchPanel).update_view(view)
        self.query_one("#verifier-pane", VerifierPanel).update_view(view)
        self.query_one("#activity-pane", ActivityPanel).update_view(view)
        boundaries = self._current_boundaries()
        self.query_one("#timeline-pane", TimelinePanel).update_view(view, boundaries)
        self._render_bar()

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
        if self.mode is WorkspaceMode.REPLAY:
            bar = self.query_one("#replay-bar", ReplayBar)
            if self.controller is None:
                bar.update("")
                return
            controller = self.controller
            bar.update(
                f"[dim][bold]replay[/] {controller.index}/{controller.total_events} events"
                f"   ·   [bold][[/] prev   [bold]][/] next   [bold]{{[/] prev phase   "
                f"[bold]}}[/] next phase   [bold]g[/] begin   [bold]G[/] end   "
                f"[bold]j[/] jump to seq   [bold]q[/] history[/]"
            )
        else:
            bar = self.query_one("#live-bar", LiveBar)
            if self._live_terminal is not None:
                result = self._live_terminal
                bar.update(
                    f"[bold]{result.status.value}[/] ({result.termination_reason.value})"
                    f"  ·  cleanup verified: {result.cleanup_verified}"
                    f"  ·  [bold]q[/] returns to history"
                )
            elif self._live_failure is not None:
                bar.update(f"[bold red]startup failed[/]  ·  [bold]q[/] returns to history")
            elif self._cancel_requested_ui:
                bar.update("[bold yellow]cancel requested[/] — waiting for the worker's cooperative cleanup and terminal evidence")
            else:
                bar.update("[dim]live session running[/]   ·   [bold]c[/] cancel   [bold]q[/] history")

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
        self.app.push_screen(JumpToSequenceScreen(self._jump_to_sequence))

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
        self._cancel_active = True
        self._render_all()
        self._runner.cancel()

    def live_cancel_event_seen(self) -> None:
        self._cancel_requested_ui = True
        self._render_all()

    # -- common actions -----------------------------------------------------

    def action_back_home(self) -> None:
        self.app.go_home()

    # -- activity filters ---------------------------------------------------

    def _activity_panel(self) -> ActivityPanel:
        return self.query_one("#activity-pane", ActivityPanel)

    def action_filter_all(self) -> None:
        self._set_filter("all")

    def action_filter_lifecycle(self) -> None:
        self._set_filter("lifecycle")

    def action_filter_controller(self) -> None:
        self._set_filter("controller")

    def action_filter_model(self) -> None:
        self._set_filter("model")

    def action_filter_debugger(self) -> None:
        self._set_filter("debugger")

    def action_filter_patch(self) -> None:
        self._set_filter("patch")

    def action_filter_verifier(self) -> None:
        self._set_filter("verifier")

    def _set_filter(self, name: str) -> None:
        panel = self._activity_panel()
        panel.filter = name
        if self._view is not None:
            panel.update_view(self._view)


class JumpToSequenceScreen(Screen):
    """One compact input modal for jumping to a replay sequence."""

    BINDINGS = [Binding("escape", "cancel", "Back")]

    def __init__(self, on_submit: Any) -> None:
        super().__init__()
        self._on_submit = on_submit

    def compose(self) -> ComposeResult:
        yield Static("[bold #58a6ff]Jump to sequence[/]", id="jump-title")
        yield Input(id="jump-input", placeholder="sequence number")
        yield Static("[dim]enter: jump · escape: cancel[/]", id="jump-hint")

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


__all__ = [
    "HomeScreen",
    "JumpToSequenceScreen",
    "StartSessionScreen",
    "WorkspaceMode",
    "WorkspaceScreen",
    "render_view_header",
]
