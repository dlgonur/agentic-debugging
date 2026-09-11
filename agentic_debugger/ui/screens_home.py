"""Home and history entry screens for the Agentic Debugger TUI.

The welcome surface (hero banner, action cards, history count) and the
session-archive surface (recorded-session table with verification cells).

Screens are presentation-only: history comes from the app-owned
HistoryStore; opening a session or starting a new one delegates to the
application object, which owns workspace construction and live execution.
"""

from __future__ import annotations

from typing import Any, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Static

from agentic_debugger.application.history import (
    HistoryClassification,
    SessionHistoryEntry,
)
from agentic_debugger.ui.session_config import TARGET_LOCAL_PROJECT
from agentic_debugger.ui.screens_providers import ModelProvidersScreen
from agentic_debugger.ui.screens_setup import StartSessionScreen
from agentic_debugger.ui.screens_shared import (
    _CLASSIFICATION_STYLE,
    _RESULT_STYLE,
    _compact_session_id,
    _compact_source_label,
    _format_duration,
    _format_timestamp,
    HelpModalScreen,
)
from agentic_debugger.ui.theme import (
    CANVAS,
    FAINT,
    FOREGROUND,
    LINE_STRONG,
    MUTED,
    PRIMARY,
    SUCCESS,
    WARNING,
)


BANNER_3LINE = """ ▄▀▀█ ▄▀▀▀ █▀▀ █▄  █ ▀█▀ █ ▄▀▀   █▀▀▄ █▀▀ █▀▀▄ █  █ ▄▀▀▀ ▄▀▀▀ █▀▀ █▀▀▄
 █▄▄█ █ ▀█ █▀  █ ▀▄█  █  █ █     █  █ █▀  █▀▀▄ █  █ █ ▀█ █ ▀█ █▀  █▄▄▀
 █  █ ▀▄▄▀ ▀▀▀ ▀   ▀  ▀  ▀ ▀▄▄   ▀▀▀  ▀▀▀ ▀▀▀   ▀▀▀ ▀▄▄▀ ▀▄▄▀ ▀▀▀ ▀  ▀"""

BANNER_WIDE_SLANT = """ ▄▀▀▀▄ ▄▀▀▀▄ █▀▀▀▀ █▄  █ ▀█▀ █ ▄▀▀▀▄   █▀▀▀▄ █▀▀▀▀ █▀▀▀▄ █   █ ▄▀▀▀▄ ▄▀▀▀▄ █▀▀▀▀ █▀▀▀▄
 █▄▄▄█ █  ▄▄ █▀▀▀  █ ▀▄█  █  █ █       █   █ █▀▀▀  █▀▀▀▄ █   █ █  ▄▄ █  ▄▄ █▀▀▀  █▄▄▄▀
 █   █ ▀▄▄▄▀ ▀▀▀▀▀ ▀   ▀  ▀  ▀ ▀▄▄▄▀   ▀▀▀▀  ▀▀▀▀▀ ▀▀▀▀  ▀▀▀▀▀ ▀▄▄▄▀ ▀▄▄▄▀ ▀▀▀▀▀ ▀   ▀"""


class HomeActionRow(Static):
    """An interactive, keyboard-focusable action card on the welcome screen."""

    can_focus = True

    def __init__(
        self,
        key_label: str,
        title: str,
        description: str,
        action_id: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.key_label = key_label
        self.action_title = title
        self.action_desc = description
        self.action_id = action_id

    def set_description(self, desc: str) -> None:
        self.action_desc = desc
        self.refresh()

    def render(self) -> Text:
        t = Text(no_wrap=True)
        if self.has_focus:
            t.append(" › ", style=f"bold {PRIMARY}")
            t.append(f" {self.key_label} ", style=f"bold {CANVAS} on {PRIMARY}")
            t.append(f"  {self.action_title:<22}", style=f"bold {FOREGROUND}")
            t.append(f" {self.action_desc}", style=f"{MUTED}")
        else:
            t.append("   ", style=f"{FAINT}")
            t.append(f" {self.key_label} ", style=f"bold {PRIMARY} on #102430")
            t.append(f"  {self.action_title:<22}", style=f"bold {FOREGROUND}")
            t.append(f" {self.action_desc}", style=f"{FAINT}")
        return t

    def on_click(self) -> None:
        self.focus()
        screen = self.screen
        if isinstance(screen, HomeScreen):
            screen.trigger_action(self.action_id)


class HomeScreen(Screen):
    """The forensic welcome screen: calm, spacious, centered, and action-driven."""

    BINDINGS = [
        Binding("s", "start_session", "Start debugging", priority=True),
        Binding("n", "start_session", "New session", show=False),
        Binding("p", "start_local_project", "Local Project", priority=True),
        Binding("m", "open_providers", "Model Providers", priority=True),
        Binding("c", "open_providers", "Model Providers", show=False, priority=True),
        Binding("h", "open_history", "Session History", priority=True),
        Binding("?", "show_help", "Help", priority=True),
        Binding("q", "quit_app", "Quit", priority=True),
        Binding("enter", "select_action", "Select", priority=True),
        Binding("down", "focus_next", "Next action", show=False),
        Binding("up", "focus_previous", "Previous action", show=False),
        Binding("j", "focus_next", "Next action", show=False),
        Binding("k", "focus_previous", "Previous action", show=False),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="home-screen-wrap"):
            with Vertical(id="home-container"):
                with Vertical(id="home-hero-panel"):
                    yield Static(id="home-brand-banner")

                with Vertical(id="home-actions-panel"):
                    yield HomeActionRow(
                        "S",
                        "Start Debugging",
                        "Curated task or Capability Ladder",
                        "action-start",
                        id="action-start",
                    )
                    yield HomeActionRow(
                        "P",
                        "Debug Local Project",
                        "Debug a local Git repository",
                        "action-local",
                        id="action-local",
                    )
                    yield HomeActionRow(
                        "M",
                        "Model Providers",
                        "Manage external endpoints & API keys",
                        "action-providers",
                        id="action-providers",
                    )
                    yield HomeActionRow(
                        "H",
                        "Session History",
                        "No recorded sessions",
                        "action-history",
                        id="action-history",
                    )
                    yield HomeActionRow(
                        "?",
                        "Help & Architecture",
                        "System reference",
                        "action-help",
                        id="action-help",
                    )

        yield Static(
            f"[bold {PRIMARY}]↑/↓[/] Select   "
            f"[bold {PRIMARY}]Enter[/] Open   "
            f"[bold {PRIMARY}]Ctrl+C[/] Quit",
            id="home-footer-bar",
        )

    def on_mount(self) -> None:
        self.update_content()
        self.refresh_history()
        self.query_one("#action-start", HomeActionRow).focus()

    def on_screen_resume(self) -> None:
        self.refresh_history()
        if self.focused is None or not isinstance(self.focused, HomeActionRow):
            self.query_one("#action-start", HomeActionRow).focus()

    def on_resize(self, event: Any) -> None:
        self.update_content()
        self.refresh_history()

    def update_content(self) -> None:
        is_wide = self.size.width >= 102
        container = self.query_one("#home-container", Vertical)
        if is_wide:
            container.styles.width = 96
            banner_text = BANNER_WIDE_SLANT
        else:
            container.styles.width = 76
            banner_text = BANNER_3LINE

        banner_elem = self.query_one("#home-brand-banner", Static)
        banner_elem.update(Text(banner_text, style=f"bold {PRIMARY}", no_wrap=True))

        footer_elem = self.query_one("#home-footer-bar", Static)
        footer_elem.update(
            f"[bold {PRIMARY}]↑/↓[/] Select   "
            f"[bold {PRIMARY}]Enter[/] Open   "
            f"[bold {PRIMARY}]Ctrl+C[/] Quit"
        )

    def refresh_history(self) -> None:
        try:
            action_history = self.query_one("#action-history", HomeActionRow)
        except Exception:
            return
        entries = (
            self.app.history_store.list_sessions()
            if hasattr(self.app, "history_store") and self.app.history_store is not None
            else []
        )
        count = len(entries)
        if count == 0:
            action_history.set_description("No recorded sessions")
        elif count == 1:
            action_history.set_description("1 recorded session")
        else:
            action_history.set_description(f"{count} recorded sessions")

    def action_focus_next(self) -> None:
        self.focus_next()

    def action_focus_previous(self) -> None:
        self.focus_previous()

    def trigger_action(self, action_id: str) -> None:
        if action_id == "action-start":
            self.action_start_session()
        elif action_id == "action-local":
            self.action_start_local_project()
        elif action_id == "action-providers":
            self.action_open_providers()
        elif action_id == "action-history":
            self.action_open_history()
        elif action_id == "action-help":
            self.action_show_help()

    def action_select_action(self) -> None:
        focused = self.focused
        if isinstance(focused, HomeActionRow):
            self.trigger_action(focused.action_id)
        else:
            self.action_start_session()

    def action_start_session(self) -> None:
        self.app.push_screen(
            StartSessionScreen(
                task_options=list(self.app.curated_task_options())
            )
        )

    def action_start_local_project(self) -> None:
        self.app.push_screen(
            StartSessionScreen(
                task_options=list(self.app.curated_task_options()),
                initial_target=TARGET_LOCAL_PROJECT,
            )
        )

    def action_open_providers(self) -> None:
        self.app.push_screen(ModelProvidersScreen())

    def action_open_history(self) -> None:
        self.app.push_screen(HistoryScreen())

    def action_show_help(self) -> None:
        self.app.push_screen(HelpModalScreen())

    def action_refresh(self) -> None:
        self.refresh_history()
        self.notify("Workspace status refreshed.")

    def action_quit_app(self) -> None:
        self.app.action_quit()


class HistoryScreen(Screen):
    """App-owned run history: the archive and replay navigation surface."""

    BINDINGS = [
        Binding("s", "start_session", "New session", priority=True),
        Binding("n", "start_session", "New session", show=False),
        Binding("p", "start_local_project", "Local Project", priority=True),
        Binding("o", "open_selected", "Open", priority=True),
        Binding("enter", "open_selected", "Open", show=False),
        Binding("r", "refresh", "Refresh", priority=True),
        Binding("escape", "go_back", "Back", priority=True),
        Binding("h", "go_back", "Back", show=False),
        Binding("?", "show_help", "Help", priority=True),
        Binding("q", "go_back", "Back", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="history-header"):
            yield Static("", id="history-title")
            yield Static("", id="history-summary")
            with Horizontal(id="history-actions"):
                yield Button(
                    "New session",
                    id="history-new-button",
                    classes="primary-action",
                )
                yield Button(
                    "Debug local project",
                    id="history-local-button",
                    classes="secondary-action",
                )
                yield Button(
                    "Back to Home",
                    id="history-back-button",
                    classes="secondary-action",
                )
        yield Static("", id="history-empty", classes="empty-state")
        yield DataTable(id="history-table")
        yield Static(
            "[dim]↑/↓ Move · Enter/O Open replay · S New session · "
            "P Local project · R Refresh · Esc Home · ? Help[/]",
            id="history-footer",
        )

    def on_mount(self) -> None:
        title = Text()
        title.append("A G E N T I C   D E B U G G E R", style=f"bold {PRIMARY}")
        title.append("   // SESSION ARCHIVE", style=f"bold {FAINT}")
        title.append("\n")
        title.append("─" * 31, style=f"{LINE_STRONG}")
        title.append("\n")
        title.append("Every repair leaves a trail. ", style=f"bold {FOREGROUND}")
        title.append(
            "Reopen the evidence, inspect the verdict, or open a new case.",
            style=MUTED,
        )
        self.query_one("#history-title", Static).update(title)
        table = self.query_one("#history-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "State", "Session", "Verification", "Outcome", "Task", "Source",
            "Started", "Duration",
        )
        self.refresh_history()

    def refresh_history(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        entries = self.app.history_store.list_sessions()
        empty = self.query_one("#history-empty", Static)
        summary = self.query_one("#history-summary", Static)
        if not entries:
            empty.update(
                f"[bold {FOREGROUND}]No sessions yet.[/]\n\n"
                f"[{MUTED}]Press S to configure a new session — a curated task, "
                "your local project, or a capability-ladder run. "
                "Verifier evidence will appear here.[/]"
            )
            empty.display = True
            table.display = False
            summary.update(f"[{FAINT}]0 recorded runs[/]")
        else:
            empty.display = False
            table.display = True
            resolved = sum(
                1 for entry in entries
                if (entry.verifier_outcome or "").upper() == "RESOLVED"
            )
            attention = sum(
                1 for entry in entries
                if entry.classification is not HistoryClassification.COMPLETE
            )
            parts = [
                f"[bold {SUCCESS}]● {resolved} independently resolved[/]",
                f"[{FAINT}]{len(entries)} recorded runs[/]",
            ]
            if attention:
                parts.append(f"[bold {WARNING}]◆ {attention} need review[/]")
            summary.update(f"[{FAINT}]  ·  [/]".join(parts))
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
                Text(verifier_cell(entry)),
                Text(entry.status.value if entry.status else "—", style=result_style),
                Text(entry.task_id or "—"),
                Text(_compact_source_label(entry.source_kind)),
                Text(_format_timestamp(entry.started_at_utc)),
                Text(_format_duration(entry.started_at_utc, entry.ended_at_utc)),
                key=entry.session_id or entry.directory or "",
            )
        if entries:
            table.focus()

    def _selected_entry(self) -> Optional[SessionHistoryEntry]:
        table = self.query_one("#history-table", DataTable)
        if not table.is_valid_coordinate(table.cursor_coordinate):
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        if row_key is None or row_key.value is None:
            return None
        for entry in self.app.history_store.list_sessions():
            if (entry.session_id or entry.directory or "") == row_key.value:
                return entry
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open_selected()

    def action_start_session(self) -> None:
        self.app.push_screen(
            StartSessionScreen(
                task_options=list(self.app.curated_task_options())
            )
        )

    def action_start_local_project(self) -> None:
        self.app.push_screen(
            StartSessionScreen(
                task_options=list(self.app.curated_task_options()),
                initial_target=TARGET_LOCAL_PROJECT,
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

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_show_help(self) -> None:
        self.app.push_screen(HelpModalScreen())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "history-local-button":
            self.action_start_local_project()
            event.stop()
        elif event.button.id == "history-new-button":
            self.action_start_session()
            event.stop()
        elif event.button.id == "history-back-button":
            self.action_go_back()
            event.stop()


def verifier_cell(entry: SessionHistoryEntry) -> str:
    if entry.verifier_outcome:
        return entry.verifier_outcome.upper()
    if entry.verifier_status:
        return entry.verifier_status.upper()
    return "—"
