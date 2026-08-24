"""Screens of the Local Application V1 TUI.

Screens are presentation-only.  The home screen exposes app-owned history
through the accepted :class:`HistoryStore`; the workspace renders one
:class:`SessionViewState` in either read-only REPLAY mode or LIVE mode; the
start-session screen is the only place a bounded new deterministic session
may be requested.  No screen executes controller, PDB, patch, verifier, or
model work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional, Tuple

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Input,
    OptionList,
    Static,
    TabPane,
    TabbedContent,
)

from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    SessionStatus,
    SourceKind,
)
from agentic_debugger.application.history import (
    HistoryClassification,
    SessionHistoryEntry,
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
from agentic_debugger.ui.widgets import (
    ActivityPanel,
    DebuggerPanel,
    EvidenceState,
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


def _format_duration(started: Optional[str], ended: Optional[str]) -> str:
    if not started or not ended:
        return "—"
    try:
        from datetime import datetime

        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        seconds = max(0.0, (end_dt - start_dt).total_seconds())
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes = int(seconds // 60)
        remaining = int(seconds % 60)
        return f"{minutes}m {remaining}s"
    except Exception:
        return "—"


def _format_timestamp(utc_str: Optional[str]) -> str:
    if not utc_str:
        return "—"
    clean = utc_str.replace("T", " ")
    if len(clean) >= 16:
        return clean[:16]
    return clean


def _compact_session_id(session_id: Optional[str], max_len: int = 16) -> str:
    if not session_id:
        return "—"
    if len(session_id) <= max_len:
        return session_id
    head = max_len // 2 - 1
    tail = max_len - head - 1
    return f"{session_id[:head]}…{session_id[-tail:]}"


def _compact_source_label(source_kind: Optional[SourceKind]) -> str:
    if source_kind is None:
        return "—"
    return {
        SourceKind.OFFLINE_DEMO: "offline",
        SourceKind.CONFIGURED_MODEL: "configured",
        SourceKind.SESSION_BUNDLE: "bundle",
        SourceKind.CANONICAL_TRAJECTORY: "trajectory",
        SourceKind.EXPERIMENT_EVIDENCE: "experiment",
    }.get(source_kind, source_kind.value)


_RESULT_STYLE: dict[SessionStatus, str] = {
    SessionStatus.SUCCEEDED: "bold green",
    SessionStatus.CANCELLED: "bold yellow",
    SessionStatus.FAILED: "bold red",
    SessionStatus.TIMED_OUT: "bold red",
    SessionStatus.INTERRUPTED: "bold red",
    SessionStatus.CLEANUP_FAILED: "bold red",
    SessionStatus.UNRESOLVED: "yellow",
    SessionStatus.RUNNING: "bold blue",
    SessionStatus.STARTING: "blue",
    SessionStatus.CREATED: "dim",
}


def render_view_header(
    view: SessionViewState,
    *,
    mode: str,
    mode_style: str,
    replay_position: Optional[str] = None,
    extra: Optional[str] = None,
) -> Text:
    """One compact two-line header derived from the presentation view.

    Recorded values are appended as plain ``rich.text.Text`` (never parsed
    as Rich markup), so session ids, task ids, run ids, paths, and status
    text render literally; styling is supplied separately.
    """
    from agentic_debugger.ui.app import task_display_title

    head = Text()
    head.append(f" {mode} ", style=mode_style)
    title = task_display_title(view.task_id)
    if title and title != view.task_id:
        head.append(f"  ·  {title} · {view.task_id}")
    else:
        head.append(f"  ·  task {view.task_id}")
    source_label = {
        SourceKind.OFFLINE_DEMO: "deterministic offline",
        SourceKind.CONFIGURED_MODEL: "configured command model",
    }.get(view.source_kind, view.source_kind.value)
    head.append(f"  ·  {source_label}")
    if view.model_provenance is not None and view.model_provenance.display_name:
        # Recorded safe provenance only; never a claimed provider identity.
        head.append(
            f"  ·  model {view.model_provenance.display_name}"
            f" ({view.model_provenance.profile_id})"
        )
    if replay_position is not None:
        head.append(f"  ·  {replay_position}")
    if view.session_id:
        head.append(f"  ·  {view.session_id}", style="dim")
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
    status_text = view.status.value.upper()
    if view.status is SessionStatus.RUNNING and view.phase is not None:
        status_text += f" ({view.phase.value})"
    if view.status.terminal and view.termination_reason is not None:
        status_text += f" ({view.termination_reason.value})"
    head.append(status_text, style=status_style)
    if view.controller_phase is not None:
        head.append(f"  ·  phase: {view.controller_phase.value.capitalize()}")
    verifier = ""
    if view.verifier_summary is not None:
        summary = view.verifier_summary
        outcome_str = summary.outcome.value if summary.outcome else (summary.status or "?")
        verifier = f"verifier: {outcome_str}"
        if summary.f2p_total is not None:
            verifier += f" · fail-to-pass {summary.f2p_passed}/{summary.f2p_total}"
        if summary.p2p_total is not None and summary.p2p_total > 0:
            verifier += f" · pass-to-pass {summary.p2p_passed}/{summary.p2p_total}"
        if summary.workspace_cleaned:
            verifier += " · cleanup verified"
    elif view.verifier_stages:
        verifier = "verifier: running"
    else:
        verifier = "verifier: pending" if view.status is SessionStatus.RUNNING else "verifier: —"
    head.append(f"  ·  {verifier}")
    if extra is not None:
        head.append(f"  ·  {extra}")
    return head


class HomeScreen(Screen):
    """App-owned run history: the secondary/replay navigation surface."""

    BINDINGS = [
        Binding("n", "start_session", "New session"),
        Binding("o", "open_selected", "Open"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit_app", "Quit"),
        Binding("?", "show_help", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #58a6ff]Agentic Debugging[/]\n"
            "[dim]Session history[/]",
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
            "Record", "Session", "Task", "Source", "Started", "Duration",
            "Result", "Verifier",
        )
        self.refresh_history()

    def refresh_history(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        entries = self.app.history_store.list_sessions()
        empty = self.query_one("#home-empty", Static)
        if not entries:
            empty.update(
                "[dim]No sessions yet.  Press [bold]n[/] to start a "
                "new session, or [bold]r[/] to refresh.[/]"
            )
            empty.display = True
        else:
            empty.display = False
        for entry in entries:
            result_style = (
                _RESULT_STYLE.get(entry.status, "default")
                if entry.status
                else "default"
            )
            table.add_row(
                Text(entry.classification.value, style=_CLASSIFICATION_STYLE.get(
                    entry.classification, "default")),
                Text(_compact_session_id(entry.session_id)),
                Text(entry.task_id or "—"),
                Text(_compact_source_label(entry.source_kind)),
                Text(_format_timestamp(entry.started_at_utc)),
                Text(_format_duration(entry.started_at_utc, entry.ended_at_utc)),
                Text(entry.status.value if entry.status else "—", style=result_style),
                Text(verifier_cell(entry)),
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
        self.app.push_screen(
            StartSessionScreen(
                task_options=list(self.app.curated_task_options())
            )
        )

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
        self.app.push_screen(HelpModalScreen())


def verifier_cell(entry: SessionHistoryEntry) -> str:
    if entry.verifier_outcome:
        return entry.verifier_outcome.upper()
    if entry.verifier_status:
        return entry.verifier_status.upper()
    return "—"


@dataclass(frozen=True)
class ChoiceOption:
    """One option shown by ChoicePickerScreen."""

    value: str
    title: str
    description: str = ""
    secondary: str = ""


class SessionSettingRow(Static):
    """A compact, keyboard-focusable terminal setting row."""

    can_focus = True

    def __init__(self, label: str, *, row_key: str, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.label = label
        self.row_key = row_key
        self._value = ""
        self._secondary = ""
        self._focused = False

    def set_value(self, value: str, *, secondary: str = "") -> None:
        self._value, self._secondary = value, secondary
        self._render_row()

    def _render_row(self) -> None:
        text = Text()
        focused = self._focused
        text.append("> " if focused else "  ", style="bold #58a6ff" if focused else "dim")
        text.append(f"{self.label:<12}", style="#8b949e")
        text.append(self._value, style="bold #ffffff" if focused else "#c9d1d9")
        if self._secondary:
            text.append(f"  {self._secondary}", style="#8b949e")
        self.update(text)

    def on_focus(self) -> None:
        self._focused = True
        self._render_row()

    def on_blur(self) -> None:
        self._focused = False
        self._render_row()

    def on_click(self, event: Any) -> None:
        self.focus()
        self.screen._activate_row(self.row_key)  # type: ignore[attr-defined]
        event.stop()

    def on_key(self, event: Any) -> None:
        if getattr(event, "key", None) in ("enter", "space"):
            self.screen._activate_row(self.row_key)  # type: ignore[attr-defined]
            event.prevent_default()
            event.stop()

class TimeLimitRow(Static):
    """A compact, keyboard-focusable time-limit setting row."""

    can_focus = True

    def __init__(self, *, row_key: str = "time_limit", **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.row_key = row_key
        self._value = "No limit"
        self._focused = False

    def on_mount(self) -> None:
        self._render_row()

    def _render_row(self) -> None:
        text = Text()
        focused = self._focused
        text.append("> " if focused else "  ", style="bold #58a6ff" if focused else "dim")
        text.append("Time limit  ", style="#8b949e")
        text.append(self._value, style="bold #ffffff" if focused else "#c9d1d9")
        self.update(text)

    def set_value(self, value: Optional[int]) -> None:
        self._value = "No limit" if value is None else str(value)
        self._render_row()

    def on_focus(self) -> None:
        self._focused = True
        self._render_row()

    def on_blur(self) -> None:
        self._focused = False
        self._render_row()

    def on_click(self, event: Any) -> None:
        self.focus()
        self.screen._activate_row(self.row_key)  # type: ignore[attr-defined]
        event.stop()

    def on_key(self, event: Any) -> None:
        if getattr(event, "key", None) in ("enter", "space"):
            self.screen._activate_row(self.row_key)  # type: ignore[attr-defined]
            event.prevent_default()
            event.stop()

class TimeLimitEditorInput(Input):
    """Modal time editor input with reliable Enter submission."""

    def on_key(self, event: Any) -> None:
        if getattr(event, "key", None) == "enter":
            self.screen.action_save()  # type: ignore[attr-defined]
            event.prevent_default()
            event.stop()
            return


class TimeLimitEditorScreen(Screen):
    """Small flat modal for editing the optional elapsed-time limit."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "save", "Save", show=False),
    ]

    def __init__(
        self,
        *,
        current: Optional[int],
        on_save: Callable[[Optional[int]], None],
        on_cancel: Callable[[], None],
    ) -> None:
        super().__init__()
        self.current = current
        self._on_save = on_save
        self._on_cancel = on_cancel

    def compose(self) -> ComposeResult:
        with Vertical(id="time-limit-dialog"):
            yield Static("Set time limit", id="time-limit-title")
            yield Static("Seconds", id="time-limit-label")
            yield TimeLimitEditorInput(
                value="" if self.current is None else str(self.current),
                type="integer",
                id="time-limit-editor",
            )
            yield Static("Empty value means no limit.", id="time-limit-help")
            yield Static("enter save   esc cancel", id="time-limit-hint")
            yield Static("", id="time-limit-error")

    def on_mount(self) -> None:
        self.query_one("#time-limit-editor", Input).focus()

    def action_save(self) -> None:
        raw = self.query_one("#time-limit-editor", Input).value.strip()
        error = self.query_one("#time-limit-error", Static)
        if not raw:
            self._close(None)
            return
        try:
            value = int(raw)
        except ValueError:
            error.update("time limit must be a whole number of seconds")
            return
        if value < 1:
            error.update("time limit must be at least 1 second")
            return
        self._close(value)

    def _close(self, value: Optional[int]) -> None:
        self.app.pop_screen()
        self._on_save(value)

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_cancel()


class ChoicePickerScreen(Screen):
    """One shared flat picker for mode, task, debugger, and model choices."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        title: str,
        choices: list[ChoiceOption],
        current: Optional[str],
        on_select: Callable[[str], None],
    ) -> None:
        super().__init__()
        self.title, self.choices, self.current = title, list(choices), current
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-picker-dialog"):
            yield Static(self.title, id="choice-picker-title")
            yield OptionList(id="choice-picker-list")
            yield Static("up/down navigate   enter select   esc cancel", id="choice-picker-hint")

    def on_mount(self) -> None:
        option_list = self.query_one("#choice-picker-list", OptionList)
        for choice in self.choices:
            text = Text("> " if choice.value == self.current else "  ", style="#58a6ff" if choice.value == self.current else "#8b949e")
            text.append(choice.title, style="bold #ffffff" if choice.value == self.current else "#c9d1d9")
            if choice.secondary:
                text.append(f"\n    {choice.secondary}", style="#8b949e")
            if choice.description:
                text.append(f"\n    {choice.description}", style="#8b949e")
            option_list.add_option(text)
        if self.choices:
            option_list.highlighted = next(
                (i for i, choice in enumerate(self.choices) if choice.value == self.current), 0
            )
            option_list.focus()
            self._refresh_option_markers()
        else:
            option_list.display = False
            self.mount(Static("No configured model profiles.", id="choice-picker-empty"),
                       before=self.query_one("#choice-picker-hint"))

    def _option_renderable(self, index: int) -> Text:
        choice = self.choices[index]
        selected = index == self.query_one("#choice-picker-list", OptionList).highlighted
        text = Text("> " if selected else "  ", style="#58a6ff" if selected else "#8b949e")
        text.append(choice.title, style="bold #ffffff" if selected else "#c9d1d9")
        if choice.secondary:
            text.append(f"\n    {choice.secondary}", style="#8b949e")
        if choice.description:
            text.append(f"\n    {choice.description}", style="#8b949e")
        return text

    def _refresh_option_markers(self) -> None:
        option_list = self.query_one("#choice-picker-list", OptionList)
        for index in range(len(self.choices)):
            option_list.replace_option_prompt_at_index(index, self._option_renderable(index))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._refresh_option_markers()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = getattr(event, "option_index", None)
        if index is None:
            index = self.query_one("#choice-picker-list", OptionList).highlighted
        if index is None or not 0 <= index < len(self.choices):
            return
        self.app.pop_screen()
        self._on_select(self.choices[index].value)

    def action_cancel(self) -> None:
        self.app.pop_screen()


class StartSessionScreen(Screen):
    """Keyboard-first workspace shell for starting one live session."""

    BINDINGS = [
        Binding("up", "move_up", "Previous setting", show=False),
        Binding("down", "move_down", "Next setting", show=False),
        Binding("s", "start", "Start"),
        Binding("h", "history", "History"),
        Binding("q", "quit_app", "Quit"),
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("escape", "cancel", "Back"),
    ]
    MODE_DETERMINISTIC = "deterministic"
    MODE_CONFIGURED = "configured"

    def __init__(self, task_options: Optional[list[tuple[str, str]]] = None) -> None:
        super().__init__()
        from agentic_debugger.ui.app import task_display_option
        self._task_options: list[tuple[str, str]] = []
        for item in list(task_options or []):
            if isinstance(item, tuple) and len(item) == 2:
                label, value = item
                self._task_options.append(task_display_option(value) if label == value else (label, value))
            elif isinstance(item, str):
                self._task_options.append(task_display_option(item))
        self._profiles: Tuple[Any, ...] = ()
        self._config_error: Optional[str] = None
        self._mode = self.MODE_DETERMINISTIC
        self._policy = "pdb-on-uncertainty"
        self._task_id = self._task_options[0][1] if self._task_options else None
        self._profile_id: Optional[str] = None
        self._max_elapsed_seconds: Optional[int] = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="start-workspace"):
            with Vertical(id="start-main"):
                with VerticalScroll(id="start-config"):
                    yield Static("[bold #79c0ff]Agentic Debugging[/]\n[bold #79c0ff]New debugging session[/]\n[dim]Configure the debugging run.[/]", id="start-title")
                    yield SessionSettingRow("Mode", row_key="mode", id="mode-row")
                    yield SessionSettingRow("Model", row_key="model", id="model-row")
                    yield SessionSettingRow("Task", row_key="task", id="task-row")
                    yield SessionSettingRow("Debugger", row_key="debugger", id="debugger-row")
                    yield TimeLimitRow(id="time-limit-row")
                    yield Static("", id="start-status")
                    yield Static("", id="start-trust")
                yield Static("s start   h history   up/down navigate   enter edit   esc back   q quit", id="start-footer")
            with VerticalScroll(id="start-context"):
                yield Static("[bold #79c0ff]SESSION SETUP[/]", id="context-title")
                yield Static("", id="context-summary")

    def on_mount(self) -> None:
        if not self._task_options:
            self._task_options = list(self.app.curated_task_options())
            self._task_id = self._task_options[0][1] if self._task_options else None
        self._refresh_profiles()
        self._refresh_mode()
        self._focus_row("mode")
        self._update_context_visibility(self.size.width)
        self._update_footer(self.size.width)

    def on_resize(self, event: Any) -> None:
        self._update_context_visibility(event.size.width)
        self._update_footer(event.size.width)
        if self.is_mounted:
            self._refresh_mode()

    def _update_context_visibility(self, width: int) -> None:
        self.query_one("#start-context", VerticalScroll).display = width >= 100

    def _update_footer(self, width: int) -> None:
        footer = self.query_one("#start-footer", Static)
        footer.update(
            "s start h hist up/down move enter edit esc back q quit"
            if width < 70
            else "s start   h history   up/down navigate   enter edit   esc back   q quit"
        )

    def _focusable_row_ids(self) -> list[str]:
        rows = ["mode"]
        if self._mode == self.MODE_CONFIGURED:
            rows.append("model")
        rows.extend(("task", "debugger", "time_limit"))
        return rows

    def _focus_row(self, row_key: str) -> None:
        row_type = TimeLimitRow if row_key == "time_limit" else SessionSettingRow
        row_id = "time-limit-row" if row_key == "time_limit" else f"{row_key}-row"
        self.query_one(f"#{row_id}", row_type).focus()

    def _focused_row_key(self) -> str:
        return getattr(self.app.focused, "row_key", "mode")

    def action_move_down(self) -> None:
        rows, current = self._focusable_row_ids(), self._focused_row_key()
        self._focus_row(rows[(rows.index(current) + 1) % len(rows)] if current in rows else rows[0])

    def action_move_up(self) -> None:
        rows, current = self._focusable_row_ids(), self._focused_row_key()
        self._focus_row(rows[(rows.index(current) - 1) % len(rows)] if current in rows else rows[0])

    def _activate_row(self, row_key: str) -> None:
        self._open_time_limit_editor() if row_key == "time_limit" else self._open_choice_picker(row_key)

    def _choice(self, value: str, title: str, description: str = "", secondary: str = "") -> ChoiceOption:
        return ChoiceOption(value, title, description, secondary)

    def _open_choice_picker(self, row_key: str) -> None:
        if row_key == "mode":
            choices = [
                self._choice(self.MODE_DETERMINISTIC, "Deterministic offline", "Runs locally without a configured model."),
                self._choice(self.MODE_CONFIGURED, "Configured command model", "Uses a configured model profile."),
            ]
            title, current = "Select execution mode", self._mode
        elif row_key == "task":
            choices = []
            for label, task_id in self._task_options:
                title = label.split("·", 1)[0].strip()
                secondary = label.split("·", 1)[1].strip() if "·" in label else task_id
                choices.append(self._choice(task_id, title, secondary=secondary))
            title, current = "Select task", self._task_id
        elif row_key == "debugger":
            choices = [
                self._choice("pdb-on-uncertainty", "On uncertainty", "Use debugger when runtime evidence is useful."),
                self._choice("static-baseline", "Disabled", "Use static reasoning only."),
            ]
            title, current = "Select debugger policy", self._policy
        elif row_key == "model":
            choices = [self._choice(p.profile_id, p.display_name, f"command: {p.executable}", p.profile_id) for p in self._profiles]
            title, current = "Select model profile", self._profile_id
        else:
            return
        self.app.push_screen(ChoicePickerScreen(
            title=title, choices=choices, current=current,
            on_select=lambda value: self._choice_selected(row_key, value),
        ))

    def _choice_selected(self, row_key: str, value: str) -> None:
        if row_key == "mode":
            self._mode = value
            self._refresh_mode()
        elif row_key == "task":
            self._task_id = value
            self._render_rows()
        elif row_key == "debugger":
            self._policy = value
            self._render_rows()
        elif row_key == "model":
            self._profile_id = value
            self._render_rows()
        self._focus_row(row_key)
        self._update_context()

    def _refresh_profiles(self) -> None:
        self._profiles, self._config_error = self.app.configured_profiles()

    def _selected_mode(self) -> str:
        return self._mode

    def _selected_policy(self) -> str:
        return self._policy

    @property
    def task_id(self) -> Optional[str]:
        return self._task_id

    @property
    def profile_id(self) -> Optional[str]:
        return self._profile_id

    @property
    def start_available(self) -> bool:
        return self._mode != self.MODE_CONFIGURED or bool(self._profiles and self._profile_id)

    def _profile_display_name(self) -> str:
        return next((p.display_name for p in self._profiles if p.profile_id == self._profile_id), "Not configured")

    def _task_display_name(self) -> str:
        title = next(
            (label.split("·", 1)[0].strip() for label, task_id in self._task_options if task_id == self._task_id),
            self._task_id or "Not selected",
        )
        if self.size.width and self.size.width < 70:
            available = max(18, self.size.width - 20)
            if len(title) > available:
                return f"{title[:available - 1]}…"
        return title

    def _render_rows(self) -> None:
        self.query_one("#mode-row", SessionSettingRow).set_value("Deterministic offline" if self._mode == self.MODE_DETERMINISTIC else "Configured command model")
        self.query_one("#model-row", SessionSettingRow).set_value(self._profile_display_name())
        self.query_one("#task-row", SessionSettingRow).set_value(self._task_display_name())
        self.query_one("#debugger-row", SessionSettingRow).set_value("On uncertainty" if self._policy == "pdb-on-uncertainty" else "Disabled")
        self.query_one("#time-limit-row", TimeLimitRow).set_value(self._max_elapsed_seconds)

    def _refresh_mode(self) -> None:
        configured = self._mode == self.MODE_CONFIGURED
        self.query_one("#model-row", SessionSettingRow).display = configured
        status = self.query_one("#start-status", Static)
        if self._config_error is not None and configured:
            status.update(f"[red]configuration error: {_markup_escape(self._config_error)}[/]")
        elif configured and not self._profiles:
            status.update("[yellow]start unavailable — no configured model profiles[/]")
        else:
            status.update("")
        trust = self.query_one("#start-trust", Static)
        trust.update("[yellow]configured commands are trusted user configuration; network isolation is not enforced[/]" if configured else "")
        trust.display = configured and self.size.width < 100
        self._render_rows()
        self._update_context()

    def _update_context(self) -> None:
        ready = "Yes" if self.start_available and self._task_id else "No"
        lines = [
            f"[#8b949e]Mode[/]\n{_markup_escape('Deterministic offline' if self._mode == self.MODE_DETERMINISTIC else 'Configured command model')}",
            f"\n[#8b949e]Debugger[/]\n{_markup_escape('On uncertainty' if self._policy == 'pdb-on-uncertainty' else 'Disabled')}",
            f"\n[#8b949e]Task[/]\n{_markup_escape(self._task_id or 'Not selected')}",
            f"\n[#8b949e]Execution[/]\n{_markup_escape('Local' if self._mode == self.MODE_DETERMINISTIC else 'Configured command')}",
        ]
        if self._mode == self.MODE_CONFIGURED:
            lines += [f"\n[#8b949e]Model[/]\n{_markup_escape(self._profile_id or 'Not configured')}", "\n[#8b949e]Trust[/]\nconfigured user command"]
        lines.append(f"\n[#8b949e]Ready[/]\n{ready}")
        self.query_one("#context-summary", Static).update("\n".join(lines))

    def _open_time_limit_editor(self) -> None:
        self.app.push_screen(TimeLimitEditorScreen(
            current=self._max_elapsed_seconds,
            on_save=self._time_limit_saved,
            on_cancel=lambda: self._focus_row("time_limit"),
        ))

    def _time_limit_saved(self, value: Optional[int]) -> None:
        self._max_elapsed_seconds = value
        self._render_rows()
        self._update_context()
        self._focus_row("time_limit")

    def action_edit(self) -> None:
        self._activate_row(self._focused_row_key())

    def action_confirm(self) -> None:
        self.action_edit()

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def action_history(self) -> None:
        self.app.pop_screen()

    def action_quit_app(self) -> None:
        self.app.action_quit()

    def _start(self) -> None:
        from agentic_debugger.application.events import SourceKind
        status = self.query_one("#start-status", Static)
        if not self._task_id:
            status.update("[red]choose a task[/]")
            return
        if self._mode == self.MODE_CONFIGURED and not self.start_available:
            status.update("[yellow]start unavailable — choose a configured model profile[/]")
            return
        try:
            self.app.start_live_session(
                task_id=str(self._task_id),
                policy=self._policy,
                max_elapsed_seconds=self._max_elapsed_seconds,
                source_kind=SourceKind.CONFIGURED_MODEL if self._mode == self.MODE_CONFIGURED else SourceKind.OFFLINE_DEMO,
                profile_id=self._profile_id if self._mode == self.MODE_CONFIGURED else None,
            )
        except Exception as exc:
            status.update(f"[red]{_markup_escape(exc)}[/]")

    def action_start(self) -> None:
        self._start()

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
        if self.mode is WorkspaceMode.LIVE:
            # A live worker can surface its first events (or even its
            # terminal/failure) before this screen finishes mounting.  The
            # app-owned live state is authoritative, so catch up now; the
            # panes render whatever terminal/failure fields already exist.
            self.refresh_live()
        else:
            self._render_all()

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
            self._live_events = self._live_events + (event,)
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
            return "LIVE", "bold white on #1f6feb"
        if self.entry is not None and self.entry.source_kind is not None and self.entry.source_kind.recorded:
            return "RECORDED", "bold white on #8957e5"
        return "REPLAY", "bold white on #238636"

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
                extra = None
        header = render_view_header(
            view, mode=mode, mode_style=mode_style,
            replay_position=position, extra=extra,
        )
        self.query_one("#status-header", StatusHeader).update(header)

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

        self.query_one("#source-pane", SourcePanel).update_view(
            view, evidence_state=source_state
        )
        self.query_one("#debugger-pane", DebuggerPanel).update_view(
            view, evidence_state=debugger_state
        )
        self.query_one("#patch-pane", PatchPanel).update_view(
            view, evidence_state=patch_state
        )
        self.query_one("#verifier-pane", VerifierPanel).update_view(
            view, evidence_state=verifier_state
        )
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
            pos_label = (
                "before first event"
                if controller.at_beginning
                else ("at end" if controller.at_end else f"event {controller.index}/{controller.total_events}")
            )
            bar.update(
                f"[dim][bold]replay[/] {controller.index}/{controller.total_events} events ({pos_label})"
                f"   ·   [bold]\\[[/] prev   [bold]][/] next   [bold]{{[/] prev phase   "
                f"[bold]}}[/] next phase   [bold]g[/] begin   [bold]G[/] end   "
                f"[bold]j[/] jump   [bold]?[/] help   [bold]q[/] history[/]"
            )
        else:
            bar = self.query_one("#live-bar", LiveBar)
            if self._live_terminal is not None:
                result = self._live_terminal
                term_style = (
                    "bold green"
                    if result.status is SessionStatus.SUCCEEDED
                    else ("bold yellow" if result.status is SessionStatus.CANCELLED else "bold red")
                )
                bar.update(
                    f"[{term_style}]{result.status.value.upper()}[/] ({result.termination_reason.value})"
                    f"  ·  cleanup verified: {result.cleanup_verified}"
                    f"  ·  [bold]?[/] help  ·  [bold]q[/] returns to history"
                )
            elif self._live_failure is not None:
                bar.update(f"[bold red]startup failed[/]  ·  [bold]?[/] help  ·  [bold]q[/] returns to history")
            elif self._cancel_requested_ui or self._cancel_active:
                bar.update(
                    "[bold yellow]cancel requested[/] — waiting for the "
                    "worker's cooperative cleanup and terminal evidence"
                )
            else:
                bar.update(
                    "[dim]live session running[/]   ·   [bold]c[/] cancel   [bold]?[/] help   [bold]q[/] history"
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
        self._cancel_active = True
        self._render_all()
        self._runner.cancel()

    def live_cancel_event_seen(self) -> None:
        self._cancel_requested_ui = True
        self._render_all()

    # -- common actions -----------------------------------------------------

    def action_back_home(self) -> None:
        self.app.go_home()

    def action_show_help(self) -> None:
        self.app.push_screen(HelpModalScreen())

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
            yield Static("[bold #58a6ff]Jump to sequence[/]", id="jump-title")
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


class HelpModalScreen(Screen):
    """Product conceptual legend and key bindings modal."""

    BINDINGS = [
        Binding("escape", "close_help", "Close"),
        Binding("q", "close_help", "Close"),
        Binding("enter", "close_help", "Close"),
        Binding("?", "close_help", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Static(id="help-dialog"):
            yield Static(
                "[bold #58a6ff]Agentic Debugging — Local Application V1[/]\n"
                "[dim]Developer Guide & Conceptual Legend[/]",
                id="help-title",
            )
            yield Static(
                "[bold #79c0ff]Core Concepts[/]\n"
                "  • [bold]LIVE[/]       Executing session (deterministic offline or configured command)\n"
                "  • [bold]REPLAY[/]     Read-only recorded session from authoritative journal\n"
                "\n"
                "[bold #79c0ff]Workspace Panes[/]\n"
                "  • [bold]Source[/]     Recorded workspace source with execution line markers\n"
                "  • [bold]Debugger[/]   PDB location, stack frames, locals, breakpoints\n"
                "  • [bold]Patch[/]      Candidate lifecycle and unified diff\n"
                "  • [bold]Verifier[/]   Independent correctness authority (RESOLVED / UNRESOLVED)\n"
                "  • [bold]Activity[/]   Filtered operational events (keys 1..7)\n"
                "  • [bold]Timeline[/]   Full ordered SessionEvent stream with phase boundaries\n"
                "\n"
                "[bold yellow]Important Principle:[/] [bold]APPLIED does not mean FIXED.[/]\n"
                "[dim]Only the independent verifier decides whether a candidate is RESOLVED.[/]\n"
                "\n"
                "[bold #79c0ff]Navigation[/]\n"
                "  • Home:      [bold]n[/] new session · [bold]o[/]/[bold]enter[/] open replay · [bold]r[/] refresh · [bold]q[/] quit · [bold]?[/] help\n"
                "  • Workspace: [bold]\\[[/]/[bold]][/] prev/next event · [bold]{{[/]/[bold]}}[/] prev/next phase · [bold]g[/]/[bold]G[/] begin/end\n"
                "               [bold]j[/] jump to sequence · [bold]1[/]..[bold]7[/] activity filter · [bold]c[/] cancel live · [bold]q[/] history · [bold]?[/] help",
                id="help-content",
            )
            yield Static(
                "[dim]Press escape, q, or enter to close help[/]", id="help-hint"
            )

    def action_close_help(self) -> None:
        self.app.pop_screen()


__all__ = [
    "HelpModalScreen",
    "HomeScreen",
    "JumpToSequenceScreen",
    "StartSessionScreen",
    "WorkspaceMode",
    "WorkspaceScreen",
    "render_view_header",
]
