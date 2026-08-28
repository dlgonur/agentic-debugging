"""Screens for the Agentic Debugger terminal application.

Screens are presentation-only.  The home screen exposes app-owned history
through the accepted :class:`HistoryStore`; the workspace renders one
:class:`SessionViewState` in either read-only REPLAY mode or LIVE mode; the
start-session screen is the only place a bounded new deterministic session
may be requested.  No screen executes controller, PDB, patch, verifier, or
model work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    OptionList,
    Static,
    TabPane,
    TabbedContent,
    TextArea,
)


class CopyAllButton(Button):
    """Mouse-clickable copy control without adding a keyboard focus stop."""

    can_focus = False

from agentic_debugger.application.events import (
    OperatorStage,
    SessionEvent,
    SessionEventKind,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
)
from agentic_debugger.application.level32 import (
    LEVEL32_TASK_ID,
    is_ladder_task,
    ladder_task_metadata,
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
from agentic_debugger.application.live_execution import ExecutionMode, LiveExecutionState, project_live_execution
from agentic_debugger.application.replay import phase_boundaries
from agentic_debugger.application.session import SessionResult
from agentic_debugger.ui.models import LiveSessionRunner, ReplayController
from agentic_debugger.ui.widgets import (
    ActivityPanel,
    DebuggerPanel,
    EvidenceReviewPanel,
    EvidenceState,
    LiveBar,
    LiveRunContextPanel,
    PatchPanel,
    ReplayBar,
    SourcePanel,
    StatusHeader,
    TimelinePanel,
    VerifierPanel,
    WorkstreamPanel,
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

# Canonical user-facing keyboard vocabulary shared by footers and help.
START_FOOTER = "S start   H history   ↑/↓ move   Enter edit   Esc back   Ctrl+C quit"
START_FOOTER_COMPACT = "S start  H history  ↑/↓ move  Enter edit  Esc back"
WORKSPACE_FOOTER_ACTIVE = "left/right views   1-8 activity filters   c cancel   h history   n new session   ctrl+c quit"
WORKSPACE_FOOTER_IDLE = "left/right views   1-8 activity filters   h history   n new session   ctrl+c quit"
REPLAY_FOOTER = "left/right views   1-8 activity filters   events   phases   h history   n new session   ctrl+c quit"


def _markup_escape(value: Any) -> str:
    return str(value).replace("[", "\\[").replace("]", "\\]")


def _operator_stage_label(stage: Any) -> str:
    return str(stage.value if hasattr(stage, "value") else stage).replace("_", " ").capitalize()


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
        SourceKind.OLLAMA_CLOUD_LADDER: "Ollama Cloud",
        SourceKind.SESSION_BUNDLE: "bundle",
        SourceKind.CANONICAL_TRAJECTORY: "trajectory",
        SourceKind.EXPERIMENT_EVIDENCE: "experiment",
        SourceKind.LEVEL32_OPERATOR: "Level-32 operator",
        SourceKind.LOCAL_PROJECT: "Local Project",
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
    head.append(f"  {title}")
    source_label = {
        SourceKind.OFFLINE_DEMO: "deterministic offline",
        SourceKind.CONFIGURED_MODEL: "configured command model",
        SourceKind.OLLAMA_CLOUD_LADDER: "Ollama Cloud ladder",
        SourceKind.SESSION_BUNDLE: "recorded bundle",
        SourceKind.CANONICAL_TRAJECTORY: "recorded trajectory",
        SourceKind.EXPERIMENT_EVIDENCE: "recorded experiment",
        SourceKind.LEVEL32_OPERATOR: "Level-32 authoritative operator",
        SourceKind.LOCAL_PROJECT: "Local Project Debug",
    }.get(view.source_kind, view.source_kind.value)
    if view.source_kind not in (SourceKind.OLLAMA_CLOUD_LADDER, SourceKind.LEVEL32_OPERATOR):
        # Local Project Debug already names the mode in its task title;
        # repeating the source label duplicates it on the same line.
        if not (
            view.source_kind is SourceKind.LOCAL_PROJECT
            and title == source_label
        ):
            head.append(f"  ·  {source_label}")
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
    status_text = (
        "Completed"
        if view.status is SessionStatus.SUCCEEDED
        else view.status.value.replace("_", " ").capitalize()
    )
    if view.status is SessionStatus.RUNNING:
        phase = None
        if view.operator_stage is not None:
            phase = (
                "Finalizing"
                if view.operator_stage is OperatorStage.COMPLETED
                else _operator_stage_label(view.operator_stage)
            )
        elif view.controller_phase is not None:
            phase = view.controller_phase.value
        elif view.phase is not None:
            phase = view.phase.value.replace("_", " ").title()
        if phase is not None:
            status_text += f"  ·  {phase}"
    head.append(status_text, style=status_style)
    verifier = ""
    if view.verifier_summary is not None:
        summary = view.verifier_summary
        outcome_str = summary.outcome.value if summary.outcome else (summary.status or "?")
        verifier = f"verifier: {outcome_str}"
        if summary.workspace_cleaned:
            verifier += " · cleanup verified"
        elif summary.workspace_cleaned is False:
            verifier += " · cleanup failed"
    elif view.verifier_stages:
        verifier = "verifier incomplete" if view.status.terminal else "verifier running"
    elif view.termination_reason is SessionTerminationReason.MODEL_ERROR:
        verifier = "model error"
    elif view.termination_reason is SessionTerminationReason.DIRECTIVE_EXHAUSTED:
        verifier = "controller budget exhausted"
    elif view.termination_reason is SessionTerminationReason.CONTROLLER_FAILED:
        verifier = "controller failed"
    elif view.termination_reason is SessionTerminationReason.SUBPROCESS_ERROR:
        verifier = "operator error"
    elif view.status is SessionStatus.CANCELLED:
        verifier = "cancelled"
    else:
        verifier = "verifier pending" if view.status is SessionStatus.RUNNING else "verifier: —"
    if getattr(view, "cleanup_not_required", False) and "cleanup" not in verifier and "Not required" not in verifier and "No resources" not in verifier:
        verifier += " · No resources created"
    elif view.cleanup_verified is True and "cleanup verified" not in verifier:
        verifier += " · cleanup verified"
    elif view.cleanup_verified is False and "cleanup failed" not in verifier:
        verifier += " · cleanup failed"
    head.append(f"  ·  {verifier}")
    if replay_position is not None:
        head.append(f"  ·  {replay_position}", style="dim")
    if extra is not None:
        head.append(f"  ·  {extra}")
    return head


class HomeScreen(Screen):
    """App-owned run history: the secondary/replay navigation surface."""

    BINDINGS = [
        Binding("n", "start_session", "New session"),
        Binding("p", "start_local_project", "Local Project"),
        Binding("o", "open_selected", "Open"),
        Binding("r", "refresh", "Refresh"),
        Binding("?", "show_help", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #58a6ff]Agentic Debugger[/]\n"
            "[dim]Debugging sessions[/]",
            id="home-title",
        )
        yield Static("", id="home-empty", classes="empty-state")
        yield DataTable(id="history-table")
        yield Static(
            "[dim]N new session · P local project · O/Enter open · "
            "R refresh · Ctrl+C quit · ? help[/]",
            id="home-hint",
        )

    def on_mount(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Journal", "Session", "Task", "Source", "Started", "Duration",
            "Outcome", "Verification",
        )
        self.refresh_history()

    def refresh_history(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        entries = self.app.history_store.list_sessions()
        empty = self.query_one("#home-empty", Static)
        if not entries:
            empty.update(
                "[dim]No sessions yet. Press N to start a new session or "
                "R to refresh.[/]"
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

    def action_start_local_project(self) -> None:
        # Direct Local Project entry (bounded v1): separate form, not mixed into ladder
        initial = None
        try:
            from agentic_debugger.application.local_project import get_launch_cwd
            initial = str(get_launch_cwd())
        except Exception:
            pass
        self.app.push_screen(LocalProjectStartScreen(initial_project=initial))

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
        self.app.action_quit()

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


class ReadonlySettingRow(Static):
    """Flat task-specific metadata row with no editable affordance."""

    def __init__(self, label: str, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.label = label
        self._value = ""

    def set_value(self, value: str) -> None:
        self._value = value
        text = Text()
        text.append("  ", style="dim")
        text.append(f"{self.label:<12}", style="#8b949e")
        text.append(self._value, style="#c9d1d9")
        self.update(text)

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


class SingleLineEditorInput(Input):
    """Single-line input with reliable Enter -> save for focused editor."""

    def on_key(self, event: Any) -> None:
        if getattr(event, "key", None) == "enter":
            # Single-line editors save on Enter; multiline Bug editor does not.
            try:
                self.screen.action_save()  # type: ignore[attr-defined]
            except Exception:
                pass
            event.prevent_default()
            event.stop()
            return


class SingleLineFieldEditorScreen(Screen):
    """Reusable centered single-line editor in the same family as Bug picker.

    Visual: centered dialog width 70, dark #161b22, Input #0d1117 with rounded
    border, green Save button, footer "Enter save    Esc cancel".

    Keyboard contract (single-line):
        Enter => save
        Esc   => cancel (no mutation)
        Save click => save
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "save", "Save", show=False),
    ]

    def __init__(
        self,
        *,
        title: str,
        current: str,
        on_save: Callable[[Optional[str]], None],
        placeholder: str = "",
        max_length: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.current = current or ""
        self.placeholder = placeholder or title
        self.max_length = max_length
        self._on_save = on_save

    def compose(self) -> ComposeResult:
        with Vertical(id="single-line-dialog"):
            yield Static(self.title_text, id="single-line-title")
            yield SingleLineEditorInput(
                value=self.current,
                placeholder=self.placeholder,
                id="single-line-editor",
            )
            with Horizontal(id="single-line-actions"):
                yield Button("Save", id="single-line-save-button", variant="primary")
            yield Static("Enter save    Esc cancel", id="single-line-hint")
            yield Static("", id="single-line-error")

    def on_mount(self) -> None:
        inp = self.query_one("#single-line-editor", Input)
        inp.focus()
        try:
            inp.cursor_position = len(inp.value)
        except Exception:
            pass

    def action_save(self) -> None:
        raw = self.query_one("#single-line-editor", Input).value
        if self.max_length is not None and len(raw.encode("utf-8")) > self.max_length:
            self.query_one("#single-line-error", Static).update(
                f"value exceeds {self.max_length} bytes — shorten it before saving"
            )
            return
        self.app.pop_screen()
        self._on_save(raw)

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_save(None)

    def on_button_pressed(self, event: Any) -> None:
        if getattr(event.button, "id", None) == "single-line-save-button":
            self.action_save()
            event.stop()


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
            self.mount(Static("No eligible model profiles.", id="choice-picker-empty"),
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
        Binding("up", "move_up", "Previous setting", show=False, priority=True),
        Binding("down", "move_down", "Next setting", show=False, priority=True),
        Binding("s", "start", "Start"),
        Binding("h", "history", "History"),
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
                    yield Static("[bold #79c0ff]Agentic Debugger[/]\n[bold #f0f6fc]Evidence-led repair session[/]\n[dim]Run a bounded case from failure to independent verification.[/]", id="start-title")
                    yield SessionSettingRow("Mode", row_key="mode", id="mode-row")
                    yield SessionSettingRow("Model", row_key="model", id="model-row")
                    yield SessionSettingRow("Task", row_key="task", id="task-row")
                    yield SessionSettingRow("Debugger", row_key="debugger", id="debugger-row")
                    yield ReadonlySettingRow("Debugger", id="level32-debugger-row")
                    yield TimeLimitRow(id="time-limit-row")
                    yield ReadonlySettingRow("Treatment", id="level32-treatment-row")
                    yield Static("", id="start-status")
                    yield Static("", id="start-trust")
                    with Horizontal(id="start-actions"):
                        yield Button("Start session", id="start-session-button", variant="success")
                    yield Static(
                        "[bold #8b949e]EVIDENCE PROTOCOL[/]\n"
                        "[#c9d1d9]Reproduce  ->  Inspect  ->  Diagnose  ->  Change  ->  Verify  ->  Cleanup[/]\n"
                        "[dim]Claims stay separate from verifier authority.[/]",
                        id="start-method",
                    )
                yield Static(START_FOOTER, id="start-footer")
            with VerticalScroll(id="start-context"):
                yield Static("[bold #79c0ff]Session setup[/]", id="context-title")
                yield Static("", id="context-summary")

    def on_mount(self) -> None:
        if not self._task_options:
            self._task_options = list(self.app.curated_task_options())
            self._task_id = self._task_options[0][1] if self._task_options else None
        if not is_ladder_task(self._task_id) and self._mode == self.MODE_CONFIGURED:
            self._refresh_profiles()
        if is_ladder_task(self._task_id):
            profiles = self.app.ollama_cloud_model_profiles()
            self._profile_id = profiles[0].alias if profiles else None
        self._refresh_mode()
        self._focus_row("task" if is_ladder_task(self._task_id) else "mode")
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
            START_FOOTER_COMPACT
            if width < 70
            else START_FOOTER
        )

    def _focusable_row_ids(self) -> list[str]:
        if is_ladder_task(self._task_id):
            return ["task", "model"]
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
        if is_ladder_task(self._task_id) and row_key not in {"task", "model"}:
            return
        self._open_time_limit_editor() if row_key == "time_limit" else self._open_choice_picker(row_key)

    def _choice(self, value: str, title: str, description: str = "", secondary: str = "") -> ChoiceOption:
        return ChoiceOption(value, title, description, secondary)

    def _open_choice_picker(self, row_key: str) -> None:
        if row_key == "mode":
            choices = [
                self._choice(self.MODE_DETERMINISTIC, "Offline demo", "Deterministic and provider-free."),
                self._choice(self.MODE_CONFIGURED, "Configured model", "Uses a command-model profile you control."),
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
            if is_ladder_task(self._task_id):
                choices = [
                    self._choice(
                        p.alias,
                        p.display_name,
                        f"{p.readiness} · Ollama Cloud",
                        p.alias,
                    )
                    for p in self.app.level32_model_profiles()
                ]
                title, current = "Select model", self._profile_id
            else:
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
            if value == self.MODE_CONFIGURED and not is_ladder_task(self._task_id):
                self._refresh_profiles()
            self._refresh_mode()
        elif row_key == "task":
            self._task_id = value
            if is_ladder_task(self._task_id) and self._profile_id is None:
                profiles = self.app.ollama_cloud_model_profiles()
                self._profile_id = profiles[0].alias if profiles else None
            elif not is_ladder_task(self._task_id) and self._mode == self.MODE_CONFIGURED:
                self._refresh_profiles()
            self._refresh_mode()
        elif row_key == "debugger":
            self._policy = value
            self._render_rows()
        elif row_key == "model":
            self._profile_id = value
            if is_ladder_task(self._task_id):
                # Model selection changes the Level-32 readiness/status state,
                # not only the displayed row value.
                self._refresh_mode()
            else:
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
        if is_ladder_task(self._task_id):
            return any(p.alias == self._profile_id for p in self.app.level32_model_profiles())
        return self._mode != self.MODE_CONFIGURED or bool(self._profiles and self._profile_id)

    def _profile_display_name(self) -> str:
        if is_ladder_task(self._task_id):
            profiles = self.app.level32_model_profiles()
            if self._profile_id is None:
                return "Not selected" if profiles else "Not available"
            return next((p.display_name for p in profiles if p.alias == self._profile_id), "Not available")
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
        self.query_one("#mode-row", SessionSettingRow).set_value("Offline demo" if self._mode == self.MODE_DETERMINISTIC else "Configured model")
        self.query_one("#model-row", SessionSettingRow).set_value(self._profile_display_name())
        self.query_one("#task-row", SessionSettingRow).set_value(self._task_display_name())
        self.query_one("#debugger-row", SessionSettingRow).set_value("On uncertainty" if self._policy == "pdb-on-uncertainty" else "Disabled")
        self.query_one("#time-limit-row", TimeLimitRow).set_value(self._max_elapsed_seconds)
        self.query_one("#start-session-button", Button).label = (
            "Run evidence demo"
            if self._mode == self.MODE_DETERMINISTIC and not is_ladder_task(self._task_id)
            else "Start session"
        )
        if is_ladder_task(self._task_id):
            metadata = ladder_task_metadata(self._task_id)
            self.query_one("#level32-debugger-row", ReadonlySettingRow).set_value(metadata.debugger)
            self.query_one("#level32-treatment-row", ReadonlySettingRow).set_value(metadata.treatment)
        else:
            self.query_one("#level32-debugger-row", ReadonlySettingRow).set_value("Exact PDB required")
            self.query_one("#level32-treatment-row", ReadonlySettingRow).set_value("Frozen Level-32")

    def _refresh_mode(self) -> None:
        ladder = is_ladder_task(self._task_id)
        configured = self._mode == self.MODE_CONFIGURED
        self.query_one("#mode-row", SessionSettingRow).display = not ladder
        self.query_one("#model-row", SessionSettingRow).display = configured or ladder
        self.query_one("#debugger-row", SessionSettingRow).display = not ladder
        self.query_one("#level32-debugger-row", ReadonlySettingRow).display = ladder
        self.query_one("#time-limit-row", TimeLimitRow).display = not ladder
        self.query_one("#level32-treatment-row", ReadonlySettingRow).display = ladder
        status = self.query_one("#start-status", Static)
        if ladder and not self.app.ollama_cloud_model_profiles():
            status.update("[yellow]Start unavailable — the research operator is not installed.[/]")
        elif ladder and self._profile_id is None:
            status.update("[yellow]Choose an eligible Ollama model.[/]")
        elif self._config_error is not None and configured:
            status.update(f"[red]Configuration error: {_markup_escape(self._config_error)}[/]")
        elif configured and not self._profiles:
            status.update("[yellow]Start unavailable — no configured model profiles.[/]")
        else:
            status.update("")
        trust = self.query_one("#start-trust", Static)
        trust.update("[yellow]Research tasks use the canonical Ollama Cloud operator contract.[/]" if ladder else ("[yellow]Configured commands are trusted user configuration; network isolation is not enforced.[/]" if configured else ""))
        trust.display = (configured or ladder) and self.size.width < 100
        self._render_rows()
        self._update_context()

    def _update_context(self) -> None:
        if is_ladder_task(self._task_id):
            ready = "Yes" if self.start_available else "No"
            profiles = self.app.ollama_cloud_model_profiles()
            if self._profile_id is not None:
                alias = self._profile_id
            elif profiles:
                alias = "—"
            else:
                alias = "Not available"
            lines = [
                f"[#8b949e]Task[/]\n{_markup_escape(ladder_task_metadata(self._task_id).title)}",
                f"\n[#8b949e]Model[/]\n{_markup_escape(self._profile_display_name())}",
                f"\n[#8b949e]Alias[/]\n{_markup_escape(alias)}",
                f"\n[#8b949e]Debugger[/]\n{_markup_escape(ladder_task_metadata(self._task_id).debugger)}",
                f"\n[#8b949e]Treatment[/]\n{_markup_escape(ladder_task_metadata(self._task_id).treatment)}",
                f"\n[#8b949e]Evaluation[/]\n{_markup_escape(ladder_task_metadata(self._task_id).evaluation)}",
                f"\n[#8b949e]Ready[/]\n{ready}",
            ]
            self.query_one("#context-summary", Static).update("\n".join(lines))
            return
        ready = "Yes" if self.start_available and self._task_id else "No"
        lines = [
            f"[#8b949e]Mode[/]\n{_markup_escape('Offline demo' if self._mode == self.MODE_DETERMINISTIC else 'Configured model')}",
            f"\n[#8b949e]Debugger[/]\n{_markup_escape('On uncertainty' if self._policy == 'pdb-on-uncertainty' else 'Disabled')}",
            f"\n[#8b949e]Task[/]\n{_markup_escape(self._task_display_name())}",
            f"\n[#8b949e]Task ID[/]\n{_markup_escape(self._task_id or 'Not selected')}",
            f"\n[#8b949e]Execution[/]\n{_markup_escape('Local, provider-free' if self._mode == self.MODE_DETERMINISTIC else 'Configured command')}",
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
        # The displayed task/mode are the single authoritative source for the CTA.
        # No stale default, history, or scientific task may be substituted.
        selected_task = str(self._task_id) if self._task_id else None
        selected_mode = self._mode
        if not selected_task:
            status.update("[red]Choose a task.[/]")
            return
        # Validate that the selected task is actually offered in the picker.
        if not any(task_id == selected_task for _, task_id in self._task_options):
            status.update("[red]Selected task is not available.[/]")
            return
        if selected_mode == self.MODE_CONFIGURED and not self.start_available:
            status.update("[yellow]Start unavailable — choose a configured model profile.[/]")
            return
        try:
            if is_ladder_task(selected_task):
                if not self.start_available:
                    status.update("[yellow]Start unavailable — choose an eligible Ollama model.[/]")
                    return
                self.app.start_live_session(
                    task_id=selected_task,
                    policy=("exact-pdb-level32-frozen" if selected_task == LEVEL32_TASK_ID else "pdb-on-uncertainty"),
                    max_elapsed_seconds=None,
                    source_kind=(SourceKind.LEVEL32_OPERATOR if selected_task == LEVEL32_TASK_ID else SourceKind.OLLAMA_CLOUD_LADDER),
                    profile_id=self._profile_id,
                )
                return
            # Non-ladder (curated) tasks: the mode-selected source is authoritative.
            # An Offline demo task must never be routed to a Level-32/Ollama worker.
            self.app.start_live_session(
                task_id=selected_task,
                policy=self._policy,
                max_elapsed_seconds=self._max_elapsed_seconds,
                source_kind=SourceKind.CONFIGURED_MODEL if selected_mode == self.MODE_CONFIGURED else SourceKind.OFFLINE_DEMO,
                profile_id=self._profile_id if selected_mode == self.MODE_CONFIGURED else None,
            )
        except Exception as exc:
            status.update(f"[red]{_markup_escape(exc)}[/]")

    def action_start(self) -> None:
        self._start()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-session-button":
            self._start()
            event.stop()

class BrowseScreen(Screen):
    """Minimal terminal-native directory picker (no OS dialog).

    Shows parent directory, child directories, and Select-current action.
    Uses ``pathlib``/``os.scandir`` only (no native file-dialog dependency).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select"),
        Binding("backspace", "parent", "Parent"),
    ]

    def __init__(
        self,
        *,
        start_path: str | os.PathLike[str],
        on_select: Any,
    ) -> None:
        super().__init__()
        self._current = Path(start_path).resolve() if start_path else Path.cwd().resolve()
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        with Vertical(id="browse-dialog"):
            yield Static("Browse — select project directory", id="browse-title")
            yield Static("", id="browse-current")
            yield Static("[dim]parent: .. (backspace)   select current: enter on first row[/]", id="browse-hint")
            yield OptionList(id="browse-list")
            yield Static("up/down navigate   enter select   backspace parent   esc cancel", id="browse-footer")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        try:
            cur_text = str(self._current)
        except Exception:
            cur_text = "—"
        self.query_one("#browse-current", Static).update(f"[bold #79c0ff]Current:[/] {_markup_escape(cur_text)}")
        option_list = self.query_one("#browse-list", OptionList)
        option_list.clear_options()
        # First option is "Select current directory"
        option_list.add_option(Text(f"▶ Use current directory: {self._current.name or str(self._current)}", style="bold green"))
        # Parent
        parent = self._current.parent
        if parent != self._current:
            option_list.add_option(Text(f"↑ Parent: {parent}", style="#8b949e"))
        # Children
        try:
            from agentic_debugger.application.local_project import list_child_directories
            children = list_child_directories(self._current)
            for child in children[:64]:
                option_list.add_option(Text(f"  {child.name}/", style="#c9d1d9"))
            if len(children) > 64:
                option_list.add_option(Text(f"  … +{len(children)-64} more", style="dim"))
        except Exception as exc:
            option_list.add_option(Text(f"(cannot list: {exc})", style="red"))
        option_list.highlighted = 0
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = getattr(event, "option_index", None)
        if idx is None:
            idx = self.query_one("#browse-list", OptionList).highlighted
        if idx is None:
            return
        if idx == 0:
            self.app.pop_screen()
            self._on_select(str(self._current))
            return
        # Second row is parent when not root
        parent = self._current.parent
        is_parent_row = 1 if parent != self._current else -1
        if idx == 1 and parent != self._current:
            self._current = parent.resolve()
            self._refresh()
            return
        # Child rows
        offset = 2 if parent != self._current else 1
        child_index = idx - offset
        try:
            from agentic_debugger.application.local_project import list_child_directories
            children = list_child_directories(self._current)
            if 0 <= child_index < len(children):
                self._current = children[child_index]
                self._refresh()
        except Exception:
            pass

    def action_parent(self) -> None:
        parent = self._current.parent
        if parent != self._current:
            self._current = parent.resolve()
            self._refresh()

    def action_select(self) -> None:
        # Treat highlighted select as activation
        lst = self.query_one("#browse-list", OptionList)
        idx = lst.highlighted or 0
        if idx == 0:
            self.app.pop_screen()
            self._on_select(str(self._current))
        elif idx == 1 and self._current.parent != self._current:
            self.action_parent()
        else:
            self.on_option_list_option_selected(type("E", (), {"option_index": idx})())

    def action_cancel(self) -> None:
        self.app.pop_screen()


class LocalProjectStartScreen(Screen):
    """Professional terminal form for Local Project Debug new session.

    Hierarchy:

        Mode  Local Project Debug
        Project  [ resolved/path  ]  [Use current] [Browse]
        Bug      [multi-line bug description]
        Reproduction  [optional command]
        Verification  [optional command]
        Model    [existing selector]
        START DEBUGGING
    """

    BINDINGS = [
        Binding("up", "move_up", "Previous", show=False),
        Binding("down", "move_down", "Next", show=False),
        Binding("escape", "cancel", "Back"),
        Binding("s", "start", "Start debugging", priority=True),
        Binding("h", "history", "History", priority=True),
        Binding("enter", "confirm", "Confirm", show=False),
    ]

    def __init__(
        self,
        *,
        initial_project: Optional[str] = None,
    ) -> None:
        super().__init__()
        from agentic_debugger.application.local_project import get_launch_cwd, resolve_project_path

        launch = get_launch_cwd()
        self._launch_cwd = launch
        if initial_project:
            try:
                resolved = resolve_project_path(initial_project, launch)
                self._project_path = str(resolved)
            except Exception:
                self._project_path = initial_project
        else:
            self._project_path = str(launch)
        self._bug_description = ""
        self._repro_command: Optional[str] = None
        self._verify_command: Optional[str] = None
        self._profile_id: Optional[str] = None
        self._profiles: Tuple[Any, ...] = ()
        self._config_error: Optional[str] = None
        self._max_elapsed_seconds: Optional[int] = None
        # Tracks whether the user has manually edited a field; automatic
        # project-derived defaults must not overwrite manual values.
        self._repro_user_edited: bool = False
        self._verify_user_edited: bool = False
        self._model_user_edited: bool = False
        self._time_limit_user_edited: bool = False
        # Whether the current value was produced by the automatic ``repro.py``
        # convention (not a user edit). Used to decide when a project change
        # should clear the auto value without clobbering a manual assignment
        # that happens to equal ``python repro.py``.
        self._repro_is_auto: bool = False
        self._verify_is_auto: bool = False
        # Apply safe project-derived defaults for the initial project. Safe
        # checks only; does not execute anything and does not fabricate bug.
        try:
            self._apply_tracked_repro_defaults()
        except Exception:
            pass

    def _apply_tracked_repro_defaults(self) -> None:
        """Populate Repro/Verify from tracked ``repro.py`` iff unset/manual.

        - Inspects only Git-tracked project metadata (``git ls-files``).
        - Prefills ``python repro.py`` when a tracked root-level ``repro.py``
          exists and the field is still in automatic state.
        - Clears the automatic value when the new project no longer tracks
          ``repro.py`` (still automatic).
        - Never overwrites a value the user has manually entered (including an
          explicit blank that became ``None``).
        - Never infers arbitrary pytest/full-suite commands.
        """
        if getattr(self, "_repro_user_edited", False) and getattr(self, "_verify_user_edited", False):
            return
        has_repro = False
        try:
            from agentic_debugger.application.local_project import has_tracked_root_repro, validate_local_project

            validated = validate_local_project(self._project_path, launch_cwd=self._launch_cwd)
            has_repro = has_tracked_root_repro(validated.repo_root)
        except Exception:
            has_repro = False
        if not getattr(self, "_repro_user_edited", False):
            if has_repro:
                # Only auto-fill when still unset or already auto; never
                # overwrite a manual direct assignment that happens to have the
                # same text but was not produced by this helper.
                if self._repro_command is None or getattr(self, "_repro_is_auto", False):
                    self._repro_command = "python repro.py"
                    self._repro_is_auto = True
                elif self._repro_command == "python repro.py" and not getattr(self, "_repro_is_auto", False):
                    # Manual assignment that equals the auto text — treat as
                    # manual and do not mark as auto so future clears don't
                    # clobber it. Leave as-is.
                    pass
            else:
                if getattr(self, "_repro_is_auto", False) and self._repro_command == "python repro.py":
                    self._repro_command = None
                    self._repro_is_auto = False
        if not getattr(self, "_verify_user_edited", False):
            if has_repro:
                if self._verify_command is None or getattr(self, "_verify_is_auto", False):
                    self._verify_command = "python repro.py"
                    self._verify_is_auto = True
                elif self._verify_command == "python repro.py" and not getattr(self, "_verify_is_auto", False):
                    pass
            else:
                if getattr(self, "_verify_is_auto", False) and self._verify_command == "python repro.py":
                    self._verify_command = None
                    self._verify_is_auto = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="start-workspace"):
            with Vertical(id="start-main"):
                with VerticalScroll(id="start-config"):
                    yield Static("[bold #79c0ff]Agentic Debugger[/]\n[bold #79c0ff]Debug a local project[/]\n[dim]Select a clean Git repository and describe the problem.[/]", id="start-title")
                    yield Static("[bold #8b949e]Mode: Local repository[/]", id="local-mode-row")
                    yield SessionSettingRow("Project", row_key="project", id="project-row")
                    with Horizontal(id="local-project-actions"):
                        yield CopyAllButton("Use current directory", id="use-cwd-button", classes="copy-button")
                        yield CopyAllButton("Browse…", id="browse-button", classes="copy-button")
                    yield Static("", id="local-project-resolved")
                    yield SessionSettingRow("Bug", row_key="bug", id="bug-row")
                    yield SessionSettingRow("Repro", row_key="repro", id="repro-row")
                    yield SessionSettingRow("Verify", row_key="verify", id="verify-row")
                    yield SessionSettingRow("Model", row_key="model", id="local-model-row")
                    yield TimeLimitRow(id="local-time-limit-row")
                    yield Static("", id="local-start-status")
                    yield Static("", id="local-start-trust")
                    with Horizontal(id="local-start-actions"):
                        yield Button("Start debugging", id="local-start-button", variant="success")
                yield Static(START_FOOTER, id="start-footer")
            with VerticalScroll(id="start-context"):
                yield Static("[bold #79c0ff]Project status[/]", id="context-title")
                yield Static("", id="local-context-summary")

    def on_mount(self) -> None:
        self._refresh_profiles()
        try:
            self._apply_tracked_repro_defaults()
        except Exception:
            pass
        self._render_rows()
        self._focus_row("project")
        self._update_context_visibility(self.size.width)
        self._update_footer(self.size.width)

    def on_resize(self, event: Any) -> None:
        self._update_context_visibility(event.size.width)
        self._update_footer(event.size.width)
        if self.is_mounted:
            self._render_rows()

    def _update_context_visibility(self, width: int) -> None:
        self.query_one("#start-context", VerticalScroll).display = width >= 100

    def _update_footer(self, width: int) -> None:
        footer = self.query_one("#start-footer", Static)
        footer.update(START_FOOTER_COMPACT if width < 70 else START_FOOTER)

    def _refresh_profiles(self) -> None:
        # Prefer Ollama Cloud roster (actual product) with fallback to configured profiles.
        # Local Project default: qwen3.5:cloud when canonical eligible, else first eligible
        # deterministically. Never fail merely because qwen3.5 is unavailable.
        try:
            ollama = list(self.app.ollama_cloud_model_profiles())
        except Exception:
            ollama = []
        if ollama:
            # Wrap Ollama profiles as ProfileSummary-like objects for UI
            self._profiles = tuple(type("P", (), {"profile_id": m.alias, "display_name": m.display_name, "executable": "ollama", "is_ollama": True, "alias": m.alias})() for m in ollama)
            self._config_error = None
            if not getattr(self, "_model_user_edited", False):
                preferred = next((m for m in ollama if m.alias == "qwen3.5:cloud"), None)
                chosen = preferred if preferred is not None else ollama[0]
                self._profile_id = chosen.alias
            return
        try:
            self._profiles, self._config_error = self.app.configured_profiles()
        except Exception as exc:
            self._profiles, self._config_error = (), str(exc)
        if self._profiles and not getattr(self, "_model_user_edited", False):
            self._profile_id = self._profiles[0].profile_id

    def _focusable_row_ids(self) -> list[str]:
        return ["project", "bug", "repro", "verify", "model", "time_limit"]

    def _focus_row(self, row_key: str) -> None:
        row_type = TimeLimitRow if row_key == "time_limit" else SessionSettingRow
        row_id = "local-time-limit-row" if row_key == "time_limit" else ("local-model-row" if row_key == "model" else f"{row_key}-row")
        try:
            self.query_one(f"#{row_id}", row_type).focus()
        except Exception:
            pass

    def _focused_row_key(self) -> str:
        return getattr(self.app.focused, "row_key", "project")

    def action_move_down(self) -> None:
        rows, current = self._focusable_row_ids(), self._focused_row_key()
        nxt = rows[(rows.index(current) + 1) % len(rows)] if current in rows else rows[0]
        self._focus_row(nxt)

    def action_move_up(self) -> None:
        rows, current = self._focusable_row_ids(), self._focused_row_key()
        nxt = rows[(rows.index(current) - 1) % len(rows)] if current in rows else rows[0]
        self._focus_row(nxt)

    def _activate_row(self, row_key: str) -> None:
        if row_key == "project":
            self._open_project_picker()
        elif row_key == "bug":
            self._open_text_editor("Bug description", self._bug_description, self._on_bug_saved, multiline=True)
        elif row_key == "repro":
            self._open_text_editor("Reproduction command (optional)", self._repro_command or "", self._on_repro_saved)
        elif row_key == "verify":
            self._open_text_editor("Verification command (optional)", self._verify_command or "", self._on_verify_saved)
        elif row_key == "model":
            self._open_model_picker()
        elif row_key == "time_limit":
            self._open_time_limit_editor()
        else:
            self._open_project_picker()

    def _render_rows(self) -> None:
        # Project row shows basename or truncated path
        proj = self._project_path or "Not selected"
        if len(proj) > 60 and self.size.width and self.size.width < 100:
            proj = proj[:57] + "…"
        self.query_one("#project-row", SessionSettingRow).set_value(proj)
        try:
            from agentic_debugger.application.local_project import validate_local_project
            validated = None
            status_text = ""
            try:
                validated = validate_local_project(self._project_path, launch_cwd=self._launch_cwd)
                if validated.dirty:
                    status_text = "[yellow]Project has uncommitted changes. Commit/stash them first or choose a clean repository.[/]"
                else:
                    status_text = f"[green]Git: {validated.repo_root.name} @ {validated.head_commit[:7]}[/]"
            except Exception as exc:
                msg = str(exc)
                if "not a Git repository" in msg:
                    status_text = "[red]Not a Git repository.[/]"
                elif "project path not found" in msg or "not found" in msg:
                    status_text = "[red]Project path not found.[/]"
                elif "not a directory" in msg:
                    status_text = "[red]Project path is not a directory.[/]"
                elif "Git worktree" in msg:
                    status_text = f"[red]{_markup_escape(msg)[:80]}[/]"
                else:
                    status_text = f"[red]{_markup_escape(msg)[:80]}[/]"
            self.query_one("#local-project-resolved", Static).update(status_text)
        except Exception:
            self.query_one("#local-project-resolved", Static).update("")
        if self._bug_description.strip():
            first_line = self._bug_description.strip().splitlines()[0]
            bounded = first_line[:48] + ("…" if len(first_line) > 48 else "")
            if "\n" in self._bug_description.strip():
                bounded = bounded + " [+]" if bounded else "Described [+]"
            bug_preview = bounded if bounded else "Described"
        else:
            bug_preview = "Not described"
        self.query_one("#bug-row", SessionSettingRow).set_value(bug_preview)
        self.query_one("#repro-row", SessionSettingRow).set_value(self._repro_command or "Not set (optional)")
        self.query_one("#verify-row", SessionSettingRow).set_value(self._verify_command or "Not set (optional)")
        # Model row
        model_name = "No eligible models available"
        if self._profiles and self._profile_id:
            match = next((p for p in self._profiles if getattr(p, "profile_id", None) == self._profile_id), None)
            if match:
                model_name = match.display_name
            else:
                model_name = self._profile_id
        elif self._config_error:
            model_name = "Config error"
        elif not self._profiles:
            model_name = "No eligible models available"
        self.query_one("#local-model-row", SessionSettingRow).set_value(model_name)
        self.query_one("#local-time-limit-row", TimeLimitRow).set_value(self._max_elapsed_seconds)
        self._update_context()

    def _update_context(self) -> None:
        try:
            from agentic_debugger.application.local_project import validate_local_project
            repo = "—"
            head = "—"
            dirty = "unknown"
            try:
                v = validate_local_project(self._project_path, launch_cwd=self._launch_cwd)
                repo = str(v.repo_root)
                head = v.head_commit[:12]
                dirty = "dirty" if v.dirty else "clean"
            except Exception as exc:
                repo = str(exc)[:60]
            # Bug shown as bounded first-line preview in the compact side panel; the
            # complete multiline text remains the source of truth for Start.
            if self._bug_description.strip():
                _bl = self._bug_description.strip().splitlines()[0][:60]
                if len(self._bug_description.strip().splitlines()[0]) > 60:
                    _bl += "…"
                if "\n" in self._bug_description.strip():
                    _bl += " [+]"
                bug_ctx = _bl
            else:
                bug_ctx = "Not described"
            lines = [
                f"[#8b949e]Project[/]\n{_markup_escape(self._project_path or '—')}",
                f"\n[#8b949e]Repo[/]\n{_markup_escape(repo)}",
                f"\n[#8b949e]HEAD[/]\n{head}",
                f"\n[#8b949e]State[/]\n{dirty}",
                f"\n[#8b949e]Bug[/]\n{_markup_escape(bug_ctx)}",
                f"\n[#8b949e]Repro[/]\n{_markup_escape(self._repro_command or 'Not set')}",
                f"\n[#8b949e]Verify[/]\n{_markup_escape(self._verify_command or 'Not set')}",
                f"\n[#8b949e]Model[/]\n{_markup_escape(self._profile_id or 'offline')}",
            ]
            self.query_one("#local-context-summary", Static).update("\n".join(lines))
        except Exception:
            pass

    def _open_project_picker(self) -> None:
        self.app.push_screen(ChoicePickerScreen(
            title="Project input",
            choices=[
                ChoiceOption("use_cwd", "Use current directory", f"{self._launch_cwd}"),
                ChoiceOption("browse", "Browse…", "Pick via directory list"),
                ChoiceOption("type", "Type/paste path…", "Enter absolute or relative path"),
            ],
            current=None,
            on_select=self._project_choice_selected,
        ))

    def _project_choice_selected(self, value: str) -> None:
        if value == "use_cwd":
            self._project_path = str(self._launch_cwd)
            try:
                self._apply_tracked_repro_defaults()
            except Exception:
                pass
            self._render_rows()
            self._focus_row("project")
        elif value == "browse":
            self.app.push_screen(BrowseScreen(start_path=self._project_path, on_select=self._on_browse_selected))
        elif value == "type":
            self._open_text_editor("Project path", self._project_path, self._on_project_saved)

    def _on_browse_selected(self, path: str) -> None:
        self._project_path = path
        try:
            self._apply_tracked_repro_defaults()
        except Exception:
            pass
        self._render_rows()
        self._focus_row("project")

    def _on_project_saved(self, value: Optional[str]) -> None:
        if value is not None:
            # Resolve relative against launch cwd for display
            try:
                from agentic_debugger.application.local_project import resolve_project_path
                resolved = resolve_project_path(value, self._launch_cwd)
                self._project_path = str(resolved)
            except Exception:
                self._project_path = value
            try:
                self._apply_tracked_repro_defaults()
            except Exception:
                pass
        self._render_rows()
        self._focus_row("project")

    def _on_bug_saved(self, value: Optional[str]) -> None:
        if value is not None:
            self._bug_description = value
        self._render_rows()
        self._focus_row("bug")

    def _on_repro_saved(self, value: Optional[str]) -> None:
        if value is None:
            # Esc cancel — preserve previously saved value, do not clear to Not set
            self._render_rows()
            self._focus_row("repro")
            return
        self._repro_user_edited = True
        self._repro_is_auto = False
        self._repro_command = value.strip() if value.strip() else None
        self._render_rows()
        self._focus_row("repro")

    def _on_verify_saved(self, value: Optional[str]) -> None:
        if value is None:
            self._render_rows()
            self._focus_row("verify")
            return
        self._verify_user_edited = True
        self._verify_is_auto = False
        self._verify_command = value.strip() if value.strip() else None
        self._render_rows()
        self._focus_row("verify")

    def _open_text_editor(self, title: str, current: str, on_save: Any, multiline: bool = False) -> None:
        if multiline:
            # Bug description gets the dedicated multiline surface (Enter => newline, Ctrl+Enter => save).
            self.app.push_screen(BugDescriptionEditorScreen(current=current or "", on_save=on_save))
            return
        self.app.push_screen(
            SingleLineFieldEditorScreen(
                title=title,
                current=current or "",
                on_save=on_save,
                placeholder=title,
            )
        )

    def _open_model_picker(self) -> None:
        choices: list[ChoiceOption] = []
        if self._profiles:
            for p in self._profiles:
                # Ollama vs configured
                if getattr(p, "is_ollama", False):
                    choices.append(ChoiceOption(p.profile_id, p.display_name, f"Ollama Cloud · {p.profile_id}"))
                else:
                    choices.append(ChoiceOption(p.profile_id, p.display_name, f"command: {p.executable}"))
        if not choices:
            # No eligible models
            choices.append(ChoiceOption("", "No eligible models available", "No Ollama Cloud models qualified"))
            self.app.push_screen(ChoicePickerScreen(
                title="Select model", choices=choices, current="",
                on_select=lambda v: None,
            ))
            return
        self.app.push_screen(ChoicePickerScreen(
            title="Select model", choices=choices, current=self._profile_id or choices[0].value,
            on_select=self._model_selected,
        ))

    def _model_selected(self, value: str) -> None:
        self._model_user_edited = True
        self._profile_id = None if value == "offline" else value
        self._render_rows()
        self._focus_row("model")

    def _open_time_limit_editor(self) -> None:
        self.app.push_screen(TimeLimitEditorScreen(
            current=self._max_elapsed_seconds,
            on_save=self._time_limit_saved,
            on_cancel=lambda: self._focus_row("time_limit"),
        ))

    def _time_limit_saved(self, value: Optional[int]) -> None:
        self._time_limit_user_edited = True
        self._max_elapsed_seconds = value
        self._render_rows()
        self._focus_row("time_limit")

    def action_confirm(self) -> None:
        self._activate_row(self._focused_row_key())

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def action_history(self) -> None:
        self.app.pop_screen()

    def _start(self) -> None:
        status = self.query_one("#local-start-status", Static)
        # Validation gates
        if not self._profiles or not self._profile_id:
            status.update("[red]No eligible model is available. Configure a model profile first.[/]")
            return
        if not self._bug_description.strip():
            status.update("[red]Bug description is required.[/]")
            return
        try:
            from agentic_debugger.application.local_project import validate_local_project
            validated = validate_local_project(self._project_path, launch_cwd=self._launch_cwd)
            if validated.dirty:
                status.update("[yellow]Project has uncommitted changes. Commit/stash them first or choose a clean repository.[/]")
                return
        except Exception as exc:
            status.update(f"[red]{_markup_escape(exc)[:120]}[/]")
            return
        try:
            self.app.start_local_project_session(
                project_path=self._project_path,
                bug_description=self._bug_description.strip(),
                reproduction_command=self._repro_command,
                verification_command=self._verify_command,
                profile_id=self._profile_id,
                max_elapsed_seconds=self._max_elapsed_seconds,
            )
        except Exception as exc:
            status.update(f"[red]{_markup_escape(exc)[:200]}[/]")

    def action_start(self) -> None:
        self._start()

    def on_button_pressed(self, event: Any) -> None:
        if getattr(event.button, "id", None) == "use-cwd-button":
            self._project_path = str(self._launch_cwd)
            try:
                self._apply_tracked_repro_defaults()
            except Exception:
                pass
            self._render_rows()
            event.stop()
        elif getattr(event.button, "id", None) == "browse-button":
            self.app.push_screen(BrowseScreen(start_path=self._project_path, on_select=self._on_browse_selected))
            event.stop()
        elif getattr(event.button, "id", None) == "local-start-button":
            self._start()
            event.stop()


class BugDescriptionEditorScreen(Screen):
    """Dedicated terminal-native multiline editor for Bug Description.

    Visual sibling of the ``Select model`` picker: a focused modal with a
    meaningfully larger editing surface, dark restrained palette, and an
    explicit Save affordance.

    Keyboard contract (multiline):
        Enter      => newline (handled by TextArea, not a save)
        Ctrl+Enter => save and return to the Local Project form
        Esc        => cancel and return without modifying the previous value
        Save click => save and return
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "save", "Save", show=False),
    ]

    def __init__(self, *, current: str, on_save: Any) -> None:
        super().__init__()
        self._current = current or ""
        self._on_save = on_save

    def compose(self) -> ComposeResult:
        with Vertical(id="bug-editor-dialog"):
            yield Static("Bug description", id="bug-editor-title")
            yield TextArea(
                text=self._current,
                id="bug-editor",
                soft_wrap=True,
                show_line_numbers=False,
            )
            with Horizontal(id="bug-editor-actions"):
                yield Button("Save", id="bug-save-button", variant="primary")
            yield Static("Ctrl+Enter save    Esc cancel", id="bug-editor-hint")
            yield Static("", id="bug-editor-error")

    def on_mount(self) -> None:
        editor = self.query_one("#bug-editor", TextArea)
        editor.focus()
        # Move cursor to end so appending edits is natural; preserve existing value.
        try:
            editor.cursor_location = (len(editor.text.splitlines()), len(editor.text.splitlines()[-1]) if editor.text else 0)
        except Exception:
            pass

    def action_save(self) -> None:
        editor = self.query_one("#bug-editor", TextArea)
        raw: str = editor.text
        # Keep the task spec size bound (4 KiB) but allow multiline content fully.
        if len(raw.encode("utf-8")) > 4096:
            self.query_one("#bug-editor-error", Static).update(
                "bug description exceeds 4 KiB — shorten it before saving"
            )
            return
        self.app.pop_screen()
        self._on_save(raw)

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_save(None)

    def on_button_pressed(self, event: Any) -> None:
        if getattr(event.button, "id", None) == "bug-save-button":
            self.action_save()
            event.stop()


# Backwards-compat alias: the legacy tiny upper-left editor is removed.
# The reusable SingleLineFieldEditorScreen is the single centered implementation.
_SingleLineEditorScreen = SingleLineFieldEditorScreen


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
        Binding("1", "filter_all", "Filter: all", show=False),
        Binding("2", "filter_lifecycle", "Filter: lifecycle", show=False),
        Binding("3", "filter_controller", "Filter: controller", show=False),
        Binding("4", "filter_model", "Filter: model", show=False),
        Binding("5", "filter_debugger", "Filter: debugger", show=False),
        Binding("6", "filter_patch", "Filter: patch", show=False),
        Binding("7", "filter_verifier", "Filter: verifier", show=False),
        Binding("8", "filter_tools", "Filter: tools", show=False),
        Binding("c", "cancel_live", "Cancel session"),
        Binding("h", "history", "History", priority=True),
        Binding("n", "new_session", "New session", priority=True),
        Binding("a", "apply_to_project", "Apply To Project"),
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
                    with TabPane("Evidence", id="tab-evidence"):
                        yield EvidenceReviewPanel(id="evidence-pane")
                    with TabPane("Source", id="tab-source"):
                        yield SourcePanel(id="source-pane")
                        yield WorkstreamPanel(id="source-workstream")
                    with TabPane("Debugger", id="tab-debugger"):
                        yield DebuggerPanel(id="debugger-pane")
                        yield WorkstreamPanel(id="debugger-workstream")
                    with TabPane("Patch", id="tab-patch"):
                        yield PatchPanel(id="patch-pane")
                        yield WorkstreamPanel(id="patch-workstream")
                    with TabPane("Verifier", id="tab-verifier"):
                        yield VerifierPanel(id="verifier-pane")
                        yield WorkstreamPanel(id="verifier-workstream")
                    with TabPane("Activity", id="tab-activity"):
                        with Vertical(id="activity-container"):
                            with Horizontal(id="activity-copy-bar"):
                                yield CopyAllButton("Copy all", id="copy-activity", classes="copy-button")
                            yield ActivityPanel(id="activity-pane")
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
            if self._live_failure is not None and self._live_terminal is None:
                extra = "startup failed"
            elif self._cancel_requested_ui and self._live_terminal is None:
                extra = "cancel requested — waiting for worker cleanup"
            elif self._cancel_active and self._live_terminal is None:
                extra = "cancelling…"
            else:
                extra = None
            if (
                view.source_kind not in (SourceKind.OLLAMA_CLOUD_LADDER, SourceKind.LEVEL32_OPERATOR)
                and view.model_provenance is not None
                and view.model_provenance.display_name
            ):
                model_extra = f"model: {view.model_provenance.display_name}"
                extra = f"{model_extra}  ·  {extra}" if extra else model_extra
        header = render_view_header(
            view, mode=mode, mode_style=mode_style,
            replay_position=position, extra=extra,
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
        execution: Optional[LiveExecutionState]
        if self.mode is WorkspaceMode.LIVE:
            execution = self.app.live_execution_state()
        else:
            execution = project_live_execution(view, mode=ExecutionMode.REPLAY)
        if execution is not None:
            # Empty evidence panes lend their space to the workstream; a
            # pane with real content keeps it and gets a compact stream.
            # No tab stealing happens here: only the *selected* pane's
            # layout adapts, and every pane re-render is evidence-driven.
            narrow = self.size.width < 100
            for pane_id, workstream_id, available in (
                ("#source-pane", "#source-workstream", source_state is EvidenceState.AVAILABLE),
                ("#debugger-pane", "#debugger-workstream", debugger_state is EvidenceState.AVAILABLE),
                ("#patch-pane", "#patch-workstream", patch_state is EvidenceState.AVAILABLE),
                ("#verifier-pane", "#verifier-workstream", verifier_state is EvidenceState.AVAILABLE),
            ):
                pane = self.query_one(pane_id)
                workstream = self.query_one(workstream_id, WorkstreamPanel)
                expanded = not available and bool(view.workstream)
                pane.styles.height = "1fr" if available or not view.workstream else "auto"
                workstream.styles.height = "1fr" if expanded else "auto"
                workstream.update_workstream(
                    execution,
                    expanded=expanded,
                    narrow=narrow,
                    height=self.size.height,
                    # The Patch pane owns the detailed diff; its stream stays
                    # compact without a duplicate diff block.
                    suppress_change_body=pane_id == "#patch-pane",
                )
            if self.mode is WorkspaceMode.LIVE and self.query("#live-run-context"):
                self.query_one("#live-run-context", LiveRunContextPanel).update_execution(execution)
        self._render_bar()

    def _live_elapsed(self) -> str:
        if len(self._live_events) < 2:
            return "—"
        try:
            started = datetime.fromisoformat(
                self._live_events[1].timestamp_utc.replace("Z", "+00:00")
            )
            ended = (
                datetime.fromisoformat(
                    self._live_events[-1].timestamp_utc.replace("Z", "+00:00")
                )
                if self._live_terminal is not None
                else datetime.now(timezone.utc)
            )
            seconds = max(0, int((ended - started).total_seconds()))
            return f"{seconds // 60:02d}:{seconds % 60:02d}"
        except (TypeError, ValueError):
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
        if self.mode is WorkspaceMode.REPLAY:
            bar = self.query_one("#replay-bar", ReplayBar)
            if self.controller is None:
                bar.update("")
                return
            footer = REPLAY_FOOTER
            if self._view.source_kind is SourceKind.LOCAL_PROJECT and self.size.width >= 100:
                try:
                    _, apply_patch = self._local_project_apply_candidate()
                except Exception:
                    apply_patch = None
                if apply_patch:
                    footer = (
                        "left/right views   1-8 activity filters   events   phases   "
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
                state = self.app.live_execution_state()
                if state is not None and self.size.width < 100:
                    ordinal = state.request_ordinal
                    req = (
                        f"req {ordinal}/{state.ceilings.model_requests}"
                        if ordinal is not None and state.ceilings.model_requests is not None
                        else f"req {ordinal}" if ordinal is not None else "req —"
                    )
                    detail = f"LIVE · {state.operation_label} · {req}"
                    if state.request_elapsed_seconds is not None:
                        detail += f" · {state.request_elapsed_seconds:.0f}s"
                    if state.last_activity_age_seconds is not None:
                        detail += f" · last {state.last_activity_age_seconds:.0f}s"
                    bar.update(f"[dim]{_markup_escape(detail)}   {WORKSPACE_FOOTER_ACTIVE}   ? help[/]")
                else:
                    bar.update(f"[dim]{WORKSPACE_FOOTER_ACTIVE}   ? help[/]")
            else:
                footer = WORKSPACE_FOOTER_IDLE
                if self._view.source_kind is SourceKind.LOCAL_PROJECT:
                    try:
                        _, apply_patch = self._local_project_apply_candidate()
                    except Exception:
                        apply_patch = None
                    if apply_patch:
                        footer = (
                            "left/right views   1-8 activity filters   "
                            "a apply to project   h history   n new session   ctrl+c quit"
                        )
                bar.update(f"[dim]{footer}   ? help[/]")

    # -- workspace view navigation ------------------------------------------

    _VIEW_IDS = (
        "tab-evidence",
        "tab-source",
        "tab-debugger",
        "tab-patch",
        "tab-verifier",
        "tab-activity",
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
            import json as _json
            from agentic_debugger.application.local_project import (
                LOCAL_PROJECT_VERIFICATION_FILE_NAME,
                LocalProjectTaskSpec,
                LocalProjectVerificationCertificate,
                check_verification_certificate,
                local_project_task_spec_sha256,
            )

            task = LocalProjectTaskSpec.from_mapping(
                _json.loads(
                    (session_dir / "local_project_task.json").read_text(
                        encoding="utf-8"
                    )
                )
            )
            certificate = LocalProjectVerificationCertificate.from_mapping(
                _json.loads(
                    (session_dir / LOCAL_PROJECT_VERIFICATION_FILE_NAME).read_text(
                        encoding="utf-8"
                    )
                )
            )
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

    def action_filter_tools(self) -> None:
        self._set_filter("tools")

    def _set_filter(self, name: str) -> None:
        panel = self._activity_panel()
        panel.filter = name
        if self._view is not None:
            panel.update_view(self._view)

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

    def _activity_copy_text(self) -> str:
        view = self._current_view_for_copy()
        if view is None:
            return "No activity recorded."
        panel = self._activity_panel()
        # Use the panel's current filter and the view's durable timeline.
        # Do NOT use rendered Textual lines or scroll window.
        from agentic_debugger.ui.widgets import activity_export_text

        return activity_export_text(view, filter_name=panel.filter)

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
        if event.button.id == "copy-activity":
            text = self._activity_copy_text()
            # Count logical events matching current filter for acknowledgement.
            view = self._current_view_for_copy()
            if view is not None:
                from agentic_debugger.ui.widgets import _ACTIVITY_FILTER_KINDS

                allowed = _ACTIVITY_FILTER_KINDS.get(self._activity_panel().filter, frozenset())
                count = sum(
                    1 for e in view.timeline if not allowed or e.event_kind.value in allowed
                )
                # For "all", count is total timeline length.
                success = f"Copied {count} activity events" if count != 1 else "Copied 1 activity event"
            else:
                success = "Copied activity"
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
        Binding("enter", "close_help", "Close"),
        Binding("?", "close_help", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            yield Static(
                "[bold #58a6ff]Agentic Debugger[/]\n"
                "[dim]Keyboard reference and evidence guide[/]",
                id="help-title",
            )
            yield Static(
                "[bold #79c0ff]Session modes[/]\n"
                "  • LIVE — Executing session (deterministic offline or configured command)\n"
                "  • REPLAY — Read-only recorded session from authoritative journal\n"
                "\n"
                "[bold #79c0ff]Workspace views[/]\n"
                "  • Evidence — Causal case brief and authoritative verdict\n"
                "  • Source — Recorded workspace source with execution line markers\n"
                "  • Debugger — PDB location, stack frames, locals, and breakpoints\n"
                "  • Patch — Candidate lifecycle and unified diff\n"
                "  • Verifier — Independent correctness authority (RESOLVED / UNRESOLVED)\n"
                "  • Activity — Filtered operational events (keys 1–8)\n"
                "  • Timeline — Full ordered SessionEvent stream with phase boundaries\n"
                "\n"
                "[bold yellow]Evidence rule:[/] [bold]An applied patch is not automatically a fix.[/]\n"
                "[dim]Only the independent verifier can mark a candidate RESOLVED.[/]\n"
                "\n"
                "[bold #79c0ff]Navigation[/]\n"
                "  • Home — N new session · P local project · O/Enter open replay · R refresh\n"
                "           Ctrl+C quit · ? help\n"
                "  • Workspace — \\[ / ] previous/next event · { / } previous/next phase\n"
                "                G/Shift+G begin/end · J jump · 1–8 activity filter\n"
                "                C cancel live · H history · N new session\n"
                "                A apply candidate · Ctrl+C quit · ? help",
                id="help-content",
            )
            yield Static(
                "[dim]Press Esc or Enter to close help[/]", id="help-hint"
            )

    def on_mount(self) -> None:
        self.query_one("#help-dialog", VerticalScroll).focus()

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
