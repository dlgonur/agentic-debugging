"""Screens for the Agentic Debugger terminal application.

Screens are presentation-only.  The home screen exposes app-owned history
through the accepted :class:`HistoryStore`; the workspace renders one
:class:`SessionViewState` in either read-only REPLAY mode or LIVE mode; the
start-session screen is the only place a bounded new deterministic session
may be requested.  No screen executes controller, PDB, patch, verifier, or
model work.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from agentic_debugger.application.model_providers import (
    PROVIDER_KIND_CONFIGURED,
    PROVIDER_KIND_OLLAMA,
    format_model_display_name,
    list_provider_models,
)

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
from agentic_debugger.ui.session_config import (
    AUTO_RETRY_MAX,
    OFFLINE_CHOICE,
    POLICY_LABELS,
    POLICY_ON_UNCERTAINTY,
    POLICY_STATIC_BASELINE,
    PROVIDER_COMMANDCODE,
    PROVIDER_CONFIGURED,
    PROVIDER_LABELS,
    PROVIDER_OFFLINE,
    PROVIDER_OLLAMA,
    PROVIDER_OPENCODE,
    ROW_AUTO_RETRY,
    ROW_BUG,
    ROW_DEBUGGER,
    ROW_MODEL,
    ROW_ORDER,
    ROW_PROJECT,
    ROW_REPRO,
    ROW_TARGET,
    ROW_TASK,
    ROW_TIME_LIMIT,
    ROW_VERIFY,
    SEVERITY_ERROR,
    ModelChoice,
    ModelOption,
    ProjectStatus,
    SessionCatalog,
    SessionConfig,
    SessionReadiness,
    TARGET_CURATED,
    TARGET_LABELS,
    TARGET_LADDER,
    TARGET_LOCAL_PROJECT,
    TaskOption,
    derive_readiness,
    model_compatibility,
)
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
from agentic_debugger.ui.theme import (
    CANVAS,
    ERROR,
    EVIDENCE,
    FAINT,
    FOREGROUND,
    LINE,
    LINE_STRONG,
    MUTED,
    PRIMARY,
    SECONDARY,
    SUCCESS,
    WARNING,
)

_TERMINAL_KINDS = frozenset(
    {
        SessionEventKind.SESSION_COMPLETED,
        SessionEventKind.SESSION_FAILED,
        SessionEventKind.SESSION_CANCELLED,
    }
)

_CLASSIFICATION_STYLE = {
    HistoryClassification.COMPLETE: f"bold {SUCCESS}",
    HistoryClassification.INTERRUPTED: f"bold {WARNING}",
    HistoryClassification.MALFORMED: f"bold {ERROR}",
    HistoryClassification.INVALID_MANIFEST: f"bold {ERROR}",
    HistoryClassification.UNREGISTERED: FAINT,
}

# Canonical user-facing keyboard vocabulary shared by footers and help.
START_FOOTER = "↑/↓ move   Enter edit   S run   P local project   C providers   H history   Esc back   Ctrl+C quit"
START_FOOTER_COMPACT = "↑/↓ move   Enter edit   S run   P local   C providers   H history   Esc back"
WORKSPACE_FOOTER_ACTIVE = "left/right views   1-7 tabs   c cancel   h history   n new session   ctrl+c quit"
WORKSPACE_FOOTER_ACTIVE_COMPACT = "left/right views   1-7 tabs   c cancel   h history   ctrl+c quit"
WORKSPACE_FOOTER_IDLE = "left/right views   1-7 tabs   h history   n new session   w effort   r retry   ctrl+c quit"
WORKSPACE_FOOTER_IDLE_COMPACT = "left/right views   1-7 tabs   h history   n new   r retry   ctrl+c quit"
REPLAY_FOOTER = "left/right views   1-7 tabs   events   phases   h history   n new session   ctrl+c quit"
REPLAY_FOOTER_COMPACT = "left/right views   1-7 tabs   events   h history   ctrl+c quit"


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
    SessionStatus.SUCCEEDED: f"bold {SUCCESS}",
    SessionStatus.CANCELLED: f"bold {WARNING}",
    SessionStatus.FAILED: f"bold {ERROR}",
    SessionStatus.TIMED_OUT: f"bold {ERROR}",
    SessionStatus.INTERRUPTED: f"bold {ERROR}",
    SessionStatus.CLEANUP_FAILED: f"bold {ERROR}",
    SessionStatus.UNRESOLVED: WARNING,
    SessionStatus.RUNNING: f"bold {PRIMARY}",
    SessionStatus.STARTING: PRIMARY,
    SessionStatus.CREATED: FAINT,
}


def render_view_header(
    view: SessionViewState,
    *,
    mode: str,
    mode_style: str,
    elapsed: Optional[str] = None,
    replay_position: Optional[str] = None,
    extra: Optional[str] = None,
    include_verifier: bool = True,
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
        SessionStatus.RUNNING: f"bold {PRIMARY}",
        SessionStatus.STARTING: PRIMARY,
        SessionStatus.SUCCEEDED: f"bold {SUCCESS}",
        SessionStatus.UNRESOLVED: WARNING,
        SessionStatus.FAILED: f"bold {ERROR}",
        SessionStatus.CANCELLED: f"bold {WARNING}",
        SessionStatus.TIMED_OUT: f"bold {ERROR}",
        SessionStatus.INTERRUPTED: f"bold {ERROR}",
        SessionStatus.CLEANUP_FAILED: f"bold {ERROR}",
        SessionStatus.CREATED: FAINT,
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
            phase = view.controller_phase.value.replace("_", " ").title()
        elif view.phase is not None:
            phase = view.phase.value.replace("_", " ").title()
        if phase is not None:
            status_text += f"  ·  {phase}"
    head.append(status_text, style=status_style)
    if elapsed and elapsed != "—":
        head.append(f"  ·  {elapsed}", style=f"bold {EVIDENCE}")
    if mode == "LIVE" and view.status is SessionStatus.RUNNING:
        if view.latest_model_request_index is not None:
            head.append(f"  ·  Request {view.latest_model_request_index + 1}")
    if include_verifier is None:
        include_verifier = (mode != "LIVE")
    if include_verifier:
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


@dataclass(frozen=True)
class ChoiceOption:
    """One option shown by ChoicePickerScreen.

    ``disabled`` options stay visible with their ``disabled_reason`` —
    incompatibilities are explained, never hidden.  A ``group`` label is
    rendered once as a section header when the group changes.  ``group_note``
    communicates provider-level status once at the group header.
    """

    value: str
    title: str
    description: str = ""
    secondary: str = ""
    group: str = ""
    group_note: str = ""
    disabled: bool = False
    disabled_reason: str = ""


class SessionSettingRow(Static):
    """A compact, keyboard-focusable terminal setting row.

    A row always occupies its place in the stack.  When the current
    target makes it inapplicable it renders dimmed with its reason
    (``set_disabled``) instead of disappearing, and activation explains
    why instead of silently changing anything.
    """

    can_focus = True

    def __init__(self, label: str, *, row_key: str, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.label = label
        self.row_key = row_key
        self._value = ""
        self._secondary = ""
        self._focused = False
        self._disabled = False
        self._disabled_reason = ""

    def set_value(self, value: str, *, secondary: str = "") -> None:
        self._value, self._secondary = value, secondary
        self._render_row()

    def set_disabled(self, reason: str) -> None:
        self._disabled = True
        self._disabled_reason = reason
        self._render_row()

    def set_enabled(self) -> None:
        self._disabled = False
        self._disabled_reason = ""
        self._render_row()

    @property
    def is_disabled(self) -> bool:
        return self._disabled

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    def _render_row(self) -> None:
        text = Text()
        if self._disabled:
            text.append("  ", style=FAINT)
            text.append(f"{self.label:<14}", style=FAINT)
            if self._value:
                text.append(self._value, style=FAINT)
            if self._disabled_reason:
                text.append(f"  ({self._disabled_reason})", style=f"dim {FAINT}")
            self.update(text)
            return
        focused = self._focused
        text.append("› " if focused else "  ", style=f"bold {PRIMARY}" if focused else FAINT)
        text.append(f"{self.label:<14}", style=MUTED)
        text.append(self._value, style=f"bold {FOREGROUND}" if focused else FOREGROUND)
        if self._secondary:
            text.append(f"  {self._secondary}", style=MUTED)
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

    Visual: centered semantic dialog, dark input with a rounded focus border,
    primary Save action, and footer "Enter save    Esc cancel".

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
                yield Button("Save", id="single-line-save-button", classes="primary-action")
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
        subtitle: Optional[str] = None,
        empty_text: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.title, self.choices, self.current = title, list(choices), current
        self.subtitle = subtitle
        self.empty_text = empty_text
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-picker-dialog"):
            yield Static(self.title, id="choice-picker-title")
            if self.subtitle:
                yield Static(self.subtitle, id="choice-picker-subtitle")
            yield OptionList(id="choice-picker-list")
            yield Static("up/down navigate   enter select   esc cancel", id="choice-picker-hint")

    def on_mount(self) -> None:
        option_list = self.query_one("#choice-picker-list", OptionList)
        from textual.widgets.option_list import Option

        last_group = None
        for choice in self.choices:
            if choice.group and choice.group != last_group:
                last_group = choice.group
                header = Text()
                header.append(f"{choice.group.upper()}", style=f"bold {PRIMARY}")
                if choice.group_note:
                    header.append(f"   {choice.group_note}", style=f"dim italic {MUTED}")
                option_list.add_option(
                    Option(header, disabled=True)
                )
            option_list.add_option(
                Option(
                    self._option_prompt(choice, None),
                    disabled=choice.disabled,
                    id=self._option_id(choice),
                )
            )
        selectable = [index for index, choice in enumerate(self.choices) if not choice.disabled]
        if self.choices:
            current = next(
                (i for i, choice in enumerate(self.choices) if choice.value == self.current and not choice.disabled),
                None,
            )
            if current is None and selectable:
                current = min(selectable)
            if current is not None:
                # Map the choice index onto its OptionList slot (group
                # headers occupy option slots of their own).
                option_list.highlighted = self._option_slots[current]
            option_list.focus()
        else:
            option_list.display = False
            msg = self.empty_text or "No eligible choices available."
            self.mount(Static(msg, id="choice-picker-empty"),
                       before=self.query_one("#choice-picker-hint"))

    def _option_id(self, choice: ChoiceOption) -> str:
        return f"choice::{choice.value}"

    @property
    def _option_slots(self) -> dict[int, int]:
        """choice index -> OptionList option index (headers add slots)."""
        slots: dict[int, int] = {}
        slot = 0
        last_group = None
        for index, choice in enumerate(self.choices):
            if choice.group and choice.group != last_group:
                last_group = choice.group
                slot += 1
            slots[index] = slot
            slot += 1
        return slots

    def _option_prompt(self, choice: ChoiceOption, highlighted_index: Optional[int]) -> Text:
        selected = choice.value == self.current
        focused = (
            highlighted_index is not None
            and highlighted_index == self.query_one("#choice-picker-list", OptionList).highlighted
        )
        active = selected or focused
        if choice.disabled:
            text = Text()
            text.append("  ", style=FAINT)
            text.append(choice.title, style=FAINT)
            if choice.secondary:
                text.append(f"  {choice.secondary}", style=FAINT)
            return text
        text = Text()
        text.append("› " if active else "  ", style=f"bold {PRIMARY}" if active else MUTED)
        text.append(choice.title, style=f"bold {FOREGROUND}" if active else FOREGROUND)
        if choice.secondary:
            text.append(f"  {choice.secondary}", style=MUTED)
        if choice.description:
            text.append(f"  {choice.description}", style=MUTED)
        return text

    @property
    def _slot_choices(self) -> dict[int, int]:
        """OptionList option index -> choice index (header slots excluded)."""
        return {slot: index for index, slot in self._option_slots.items()}

    def _refresh_option_markers(self) -> None:
        option_list = self.query_one("#choice-picker-list", OptionList)
        highlighted = option_list.highlighted
        for slot, index in self._slot_choices.items():
            option_list.replace_option_prompt_at_index(
                slot, self._option_prompt(self.choices[index], highlighted)
            )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._refresh_option_markers()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = getattr(event, "option_index", None)
        if index is None:
            index = self.query_one("#choice-picker-list", OptionList).highlighted
        if index is None:
            return
        choice_index = self._slot_choices.get(index)
        if choice_index is None or not 0 <= choice_index < len(self.choices):
            return
        choice = self.choices[choice_index]
        if choice.disabled:
            return
        self.app.pop_screen()
        self._on_select(choice.value)

    def action_cancel(self) -> None:
        self.app.pop_screen()


def _clip_cells(value: str, room: int) -> str:
    """Ellipsize to a cell budget so content never hard-clips at a border."""
    if room <= 1:
        return "…"
    if len(value) <= room:
        return value
    return value[: room - 1].rstrip() + "…"


def _fit_row_cells(
    value: str,
    secondary: str,
    reason: str,
    budget: int,
) -> tuple[str, str, str]:
    """Fit one setting row's value, secondary, and reason into ``budget``
    cells (everything after the 16-cell prefix+label chrome).

    Priority keeps the value intact longest: the reason clips first,
    then the secondary, then the value itself.
    """
    def used(v: str, s: str, r: str) -> int:
        total = len(v)
        if s:
            total += 2 + len(s)
        if r:
            total += 4 + len(r)  # gap + parentheses
        return total

    if used(value, secondary, reason) <= budget:
        return value, secondary, reason
    room = budget - len(value) - (4 if reason else 0)
    if reason and room >= 4:
        reason = _clip_cells(reason, budget - len(value) - 4)
        if used(value, secondary, reason) <= budget:
            return value, secondary, reason
    if secondary:
        secondary = _clip_cells(secondary, max(1, budget - len(value) - (4 + len(reason) if reason else 0)))
        if used(value, secondary, reason) <= budget:
            return value, secondary, reason
    keep = budget - (4 + len(reason) if reason else 0)
    return _clip_cells(value, max(1, keep)), "", reason


def _short_unavailable_reason(reason: Optional[str]) -> str:
    """One bounded picker line for a provider unavailability reason."""
    if not reason:
        return "unavailable"
    text = reason.split("(", 1)[0].strip().rstrip(".")
    if len(text) > 60:
        text = text[:60].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text or "unavailable"


class MaskedKeyInput(Input):
    """Masked single-line input with reliable Enter -> connect."""

    def on_key(self, event: Any) -> None:
        if getattr(event, "key", None) == "enter":
            try:
                self.screen.action_save()  # type: ignore[attr-defined]
            except Exception:
                pass
            event.prevent_default()
            event.stop()
            return


class MaskedKeyEditorScreen(Screen):
    """Masked API-key entry: memory-only, never persisted or re-shown.

    The repository has no durable secret store, so a pasted key is
    kept process-local for this app session only.  The screen states
    that plainly, masks the value while typing, and never renders the
    submitted value again.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "save", "Save", show=False),
    ]

    def __init__(
        self,
        *,
        title: str,
        on_save: Callable[[Optional[str]], None],
        note: str = "",
    ) -> None:
        super().__init__()
        self.title_text = title
        self.note_text = note
        self._on_save = on_save

    def compose(self) -> ComposeResult:
        with Vertical(id="single-line-dialog"):
            yield Static(self.title_text, id="single-line-title")
            yield MaskedKeyInput(
                password=True, placeholder="paste API key", id="masked-key-editor"
            )
            if self.note_text:
                yield Static(self.note_text, id="single-line-error")
            with Horizontal(id="single-line-actions"):
                yield Button("Connect", id="single-line-save-button", classes="primary-action")
            yield Static("Enter connect    Esc cancel", id="single-line-hint")

    def on_mount(self) -> None:
        self.query_one("#masked-key-editor", Input).focus()

    def action_save(self) -> None:
        raw = self.query_one("#masked-key-editor", Input).value
        self.app.pop_screen()
        self._on_save(raw.strip() or None)

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_save(None)

    def on_button_pressed(self, event: Any) -> None:
        if getattr(event.button, "id", None) == "single-line-save-button":
            self.action_save()
            event.stop()


_PROVIDER_CREDENTIAL_SOURCE_LABELS = {
    "session_key": "Session API key (memory-only)",
    "environment": "Environment variable",
    "cli_auth_store": "CLI auth (read in place)",
}


class ModelProvidersScreen(Screen):
    """First-class Model Provider Manager: endpoints, credentials, and models.

    Operational surface:
    - Left column: provider sidebar with statuses + '+ Add provider'
    - Right column: active provider details, credential status, model discovery,
      manual model fallback, edit/delete, and add provider form.
    - Fully centered, substantial desktop geometry (width ~98 cols) and responsive
      adaptation on compact screens.
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("up", "select_previous", "Previous provider", show=False),
        Binding("down", "select_next", "Next provider", show=False),
        Binding("r", "refresh", "Refresh models"),
        Binding("k", "connect_key", "Connect API key"),
        Binding("a", "add_provider", "Add provider"),
        Binding("e", "edit_provider", "Edit provider"),
        Binding("d", "delete_provider", "Delete provider"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._selected_index = 0
        self._mode = "details"  # "details", "add", "edit"
        self._refreshing: set[str] = set()
        self._message = ""
        self._form_name = ""
        self._form_url = ""
        self._form_key = ""
        self._form_format = "chat_completions"
        self._editing_provider_id: Optional[str] = None
        self._discovery_results: Optional[str] = None

    def compose(self) -> ComposeResult:
        from agentic_debugger.application.provider_connections import connection_statuses

        statuses = connection_statuses()
        with Vertical(id="providers-wrap"):
            with Vertical(id="providers-manager-card"):
                with Horizontal(id="providers-manager-header"):
                    yield Static("MODEL PROVIDER MANAGER", id="providers-title")
                    yield Static("Endpoints · OS Secure Store · Direct API", id="providers-subtitle")
                with Horizontal(id="providers-manager-body"):
                    with Vertical(id="providers-sidebar"):
                        yield Static("PROVIDERS", id="providers-sidebar-title")
                        with VerticalScroll(id="providers-sidebar-list"):
                            for index, st in enumerate(statuses):
                                dot = "● " if st.connected else "○ "
                                label = f"{dot}{st.label}"
                                yield Button(
                                    label,
                                    id=f"provider-select-{st.kind}",
                                    classes=f"provider-item-button{' -selected' if index == self._selected_index else ''}",
                                )
                        yield Button("+ Add provider", id="provider-add-button", classes="primary-action")
                    with VerticalScroll(id="provider-main-view"):
                        for st in statuses:
                            with Vertical(classes="provider-panel", id=f"provider-panel-{st.kind}"):
                                yield Static("", id=f"provider-summary-{st.kind}", classes="provider-summary")
                                yield Static("", id=f"provider-refresh-{st.kind}", classes="provider-refresh")
                                with Horizontal(classes="provider-actions", id=f"provider-actions-{st.kind}"):
                                    yield Button(
                                        "Refresh models",
                                        id=f"provider-refresh-button-{st.kind}",
                                        classes="provider-action-button",
                                    )
                                    yield Button(
                                        "Connect API key",
                                        id=f"provider-key-button-{st.kind}",
                                        classes="provider-action-button",
                                    )
                                    yield Button(
                                        "Edit provider",
                                        id=f"provider-edit-button-{st.kind}",
                                        classes="provider-action-button",
                                    )
                                    if not st.is_builtin:
                                        yield Button(
                                            "Delete",
                                            id=f"provider-delete-button-{st.kind}",
                                            classes="provider-action-button danger-action",
                                        )
                                yield Static("MODELS", classes="models-header", id=f"provider-models-title-{st.kind}")
                                with Vertical(classes="models-box", id=f"provider-models-box-{st.kind}"):
                                    yield Static("", id=f"provider-models-list-{st.kind}", classes="models-list-text")
                                yield Button(
                                    "+ Add model (manual)",
                                    id=f"provider-add-model-button-{st.kind}",
                                    classes="provider-action-button",
                                )
                yield Static("", id="providers-status")
                yield Static(
                    "up/down select   r refresh models   k connect key   a add provider   e edit   esc back",
                    id="providers-hint",
                )

    def on_mount(self) -> None:
        self.render_state()

    # -- state ---------------------------------------------------------------

    def _current_statuses(self):
        from agentic_debugger.application.provider_connections import connection_statuses
        return connection_statuses()

    def _selected_kind(self) -> str:
        statuses = self._current_statuses()
        if not statuses:
            return "opencode_go"
        idx = max(0, min(self._selected_index, len(statuses) - 1))
        return statuses[idx].kind

    def render_state(self) -> None:
        statuses = self._current_statuses()
        selected_kind = self._selected_kind()

        # Update sidebar buttons
        for index, status in enumerate(statuses):
            btn_id = f"#provider-select-{status.kind}"
            try:
                btn = self.query_one(btn_id, Button)
                dot = "● " if status.connected else "○ "
                btn.label = f"{dot}{status.label}"
                if index == self._selected_index:
                    btn.add_class("-selected")
                else:
                    btn.remove_class("-selected")
            except Exception:
                pass

        # Update main view panels
        for index, status in enumerate(statuses):
            panel_id = f"#provider-panel-{status.kind}"
            try:
                panel = self.query_one(panel_id, Vertical)
                # Show only active provider panel
                panel.display = (index == self._selected_index)
            except Exception:
                continue

            summary = self.query_one(f"#provider-summary-{status.kind}", Static)
            refresh_line = self.query_one(f"#provider-refresh-{status.kind}", Static)
            models_list = self.query_one(f"#provider-models-list-{status.kind}", Static)

            if status.connected:
                source = _PROVIDER_CREDENTIAL_SOURCE_LABELS.get(
                    status.credential_source, status.credential_source or ""
                )
                summary.update(
                    Text()
                    .append(f"{status.label}", style=f"bold {FOREGROUND}")
                    .append(f"   Connected · {source}", style=f"{PRIMARY}")
                    .append(f"\nBase URL: {status.base_url} · Format: {status.api_format}", style=f"{MUTED}")
                )
            else:
                summary.update(
                    Text()
                    .append(f"{status.label}", style=f"bold {MUTED}")
                    .append("   Not connected", style=FAINT)
                    .append(f"\nBase URL: {status.base_url} · Format: {status.api_format}", style=f"{MUTED}")
                )

            lines = []
            if status.model_count:
                when = (status.last_refresh_utc or "").replace("T", " ").split(".", 1)[0]
                suffix = " · stale/unverified" if status.stale else ""
                lines.append(
                    f"{status.model_count} models · last refresh: {when} UTC (live catalog){suffix}"
                )
            elif status.connected:
                lines.append("No catalog yet — refresh models to discover the live catalog")
            if status.status_message:
                lines.append(status.status_message)
            refresh_line.update("\n".join(lines) if lines else "")

            # Render models list
            model_items = []
            for m in status.cached_models:
                proto_str = f" [{m.protocol}]" if m.protocol else ""
                model_items.append(f"• {m.display_name} ({m.model_id}){proto_str}")
            if model_items:
                models_list.update("\n".join(model_items[:24]))
            elif status.connected:
                models_list.update("[dim]No models discovered yet. Click 'Refresh models' or '+ Add model'.[/]")
            else:
                models_list.update("[dim]Connect API key and refresh models to discover available models.[/]")

        status_widget = self.query_one("#providers-status", Static)
        status_widget.update(self._message)

    # -- actions ---------------------------------------------------------------

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_select_previous(self) -> None:
        if self._selected_index > 0:
            self._selected_index -= 1
            self.render_state()

    def action_select_next(self) -> None:
        statuses = self._current_statuses()
        if self._selected_index < len(statuses) - 1:
            self._selected_index += 1
            self.render_state()

    def _set_message(self, text: str) -> None:
        self._message = text
        try:
            self.query_one("#providers-status", Static).update(text)
        except Exception:
            pass

    def action_refresh(self) -> None:
        kind = self._selected_kind()
        if kind in self._refreshing:
            return
        self._refreshing.add(kind)
        self._set_message("")
        self.render_state()
        self.run_worker(
            partial(self._refresh_catalog, kind),
            name=f"refresh-{kind}",
            exclusive=False,
            thread=True,
        )

    def _refresh_catalog(self, kind: str):
        from agentic_debugger.application.provider_connections import (
            ProviderConnectionError,
            refresh_provider_catalog,
        )

        try:
            snapshot = refresh_provider_catalog(kind)
        except ProviderConnectionError as exc:
            self.app.call_from_thread(self._refresh_finished, kind, False, str(exc))
            return
        except Exception:
            self.app.call_from_thread(
                self._refresh_finished,
                kind,
                False,
                "catalog refresh failed unexpectedly; retry or check the connection",
            )
            return
        self.app.call_from_thread(
            self._refresh_finished, kind, True, f"{len(snapshot.models)} models"
        )

    def _refresh_finished(self, kind: str, ok: bool, detail: str) -> None:
        self._refreshing.discard(kind)
        detail = detail if len(detail) <= 160 else detail[:160] + "…"
        if ok:
            self._set_message(f"Catalog refreshed — {detail}")
        else:
            self._set_message(f"Refresh failed — {detail}")
        self.render_state()

    def action_connect_key(self) -> None:
        kind = self._selected_kind()

        def handle(value: Optional[str]) -> None:
            if value:
                from agentic_debugger.application.provider_connections import (
                    save_secure_credential,
                    set_session_key,
                )

                try:
                    set_session_key(kind, value)
                    saved = save_secure_credential(kind, value)
                    if saved:
                        self._set_message("API key: saved securely in OS credential manager")
                    else:
                        self._set_message("API key: connected for this app session (memory-only)")
                except Exception as exc:
                    self._set_message(f"API key rejected: {exc}")
            self.render_state()

        self.app.push_screen(
            MaskedKeyEditorScreen(
                title=f"Connect API key for {kind}",
                note=(
                    "Stored securely in OS Credential Manager (Windows) or memory-only "
                    "for this session. Never written to project files."
                ),
                on_save=handle,
            )
        )

    def action_add_provider(self) -> None:
        def on_saved(new_cfg):
            if new_cfg:
                self._set_message(f"Added provider '{new_cfg.name}'")
                # Reload screen to refresh widget tree
                self.app.pop_screen()
                self.app.push_screen(ModelProvidersScreen())

        self.app.push_screen(AddProviderDialogScreen(on_save=on_saved))

    def action_edit_provider(self) -> None:
        kind = self._selected_kind()
        from agentic_debugger.application.provider_connections import get_provider_config
        cfg = get_provider_config(kind)
        if not cfg:
            self._set_message(f"Provider {kind} cannot be edited")
            return

        def on_saved(updated_cfg):
            if updated_cfg:
                self._set_message(f"Updated provider '{updated_cfg.name}'")
                self.render_state()

        self.app.push_screen(EditProviderDialogScreen(config=cfg, on_save=on_saved))

    def action_delete_provider(self) -> None:
        kind = self._selected_kind()
        from agentic_debugger.application.provider_connections import delete_provider_config, get_provider_config
        cfg = get_provider_config(kind)
        if cfg and cfg.is_builtin:
            self._set_message("Built-in providers cannot be deleted")
            return
        if delete_provider_config(kind):
            self._set_message(f"Deleted provider {kind}")
            self.app.pop_screen()
            self.app.push_screen(ModelProvidersScreen())

    def _action_add_manual_model(self, kind: str) -> None:
        def on_added(model_id: Optional[str], display_name: Optional[str], protocol: Optional[str]):
            if model_id:
                from agentic_debugger.application.provider_connections import add_manual_model
                try:
                    add_manual_model(kind, model_id, display_name, protocol)
                    self._set_message(f"Added model {model_id}")
                    self.render_state()
                except Exception as exc:
                    self._set_message(f"Failed to add model: {exc}")

        self.app.push_screen(AddManualModelDialogScreen(provider_id=kind, on_save=on_added))

    def on_button_pressed(self, event: Any) -> None:
        button_id = getattr(event.button, "id", "") or ""
        if button_id.startswith("provider-select-"):
            kind = button_id.removeprefix("provider-select-")
            self._selected_index = self._index_of(kind)
            self.render_state()
            event.stop()
        elif button_id == "provider-add-button":
            self.action_add_provider()
            event.stop()
        elif button_id.startswith("provider-refresh-button-"):
            kind = button_id.removeprefix("provider-refresh-button-")
            self._selected_index = self._index_of(kind)
            self.action_refresh()
            event.stop()
        elif button_id.startswith("provider-key-button-"):
            kind = button_id.removeprefix("provider-key-button-")
            self._selected_index = self._index_of(kind)
            self.action_connect_key()
            event.stop()
        elif button_id.startswith("provider-edit-button-"):
            kind = button_id.removeprefix("provider-edit-button-")
            self._selected_index = self._index_of(kind)
            self.action_edit_provider()
            event.stop()
        elif button_id.startswith("provider-delete-button-"):
            kind = button_id.removeprefix("provider-delete-button-")
            self._selected_index = self._index_of(kind)
            self.action_delete_provider()
            event.stop()
        elif button_id.startswith("provider-add-model-button-"):
            kind = button_id.removeprefix("provider-add-model-button-")
            self._selected_index = self._index_of(kind)
            self._action_add_manual_model(kind)
            event.stop()

    def _index_of(self, kind: str) -> int:
        statuses = self._current_statuses()
        for idx, st in enumerate(statuses):
            if st.kind == kind:
                return idx
        return 0


class AddProviderDialogScreen(Screen):
    """Dialog for adding a new generic model provider."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, on_save: Callable[[Any], None]) -> None:
        super().__init__()
        self._on_save = on_save
        self._format = "chat_completions"

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-dialog-card"):
            yield Static("ADD MODEL PROVIDER", id="dialog-title")
            yield Static("Provider Name", classes="dialog-label")
            yield Input(placeholder="e.g. Groq Direct or DeepSeek V3", id="input-name")
            yield Static("Base URL", classes="dialog-label")
            yield Input(placeholder="https://api.groq.com/openai/v1", id="input-url")
            yield Static("API Key (optional, stored securely)", classes="dialog-label")
            yield Input(password=True, placeholder="API key", id="input-key")
            yield Static("API Protocol Format", classes="dialog-label")
            with Horizontal(id="format-buttons-row"):
                yield Button("Chat Completions", id="fmt-chat", classes="fmt-btn -selected")
                yield Button("Responses", id="fmt-resp", classes="fmt-btn")
                yield Button("Messages", id="fmt-msg", classes="fmt-btn")
            yield Static("", id="dialog-feedback")
            with Horizontal(id="dialog-actions-row"):
                yield Button("Save provider", id="btn-save-dialog", classes="primary-action")
                yield Button("Cancel", id="btn-cancel-dialog")

    def on_mount(self) -> None:
        self.query_one("#input-name", Input).focus()

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_save(None)

    def on_button_pressed(self, event: Any) -> None:
        btn_id = getattr(event.button, "id", "")
        if btn_id == "fmt-chat":
            self._format = "chat_completions"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "fmt-resp":
            self._format = "responses"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "fmt-msg":
            self._format = "messages"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "btn-cancel-dialog":
            self.action_cancel()
            event.stop()
        elif btn_id == "btn-save-dialog":
            self._do_save()
            event.stop()

    def _update_format_buttons(self) -> None:
        for fid, val in [("fmt-chat", "chat_completions"), ("fmt-resp", "responses"), ("fmt-msg", "messages")]:
            try:
                b = self.query_one(f"#{fid}", Button)
                if self._format == val:
                    b.add_class("-selected")
                else:
                    b.remove_class("-selected")
            except Exception:
                pass

    def _do_save(self) -> None:
        name = self.query_one("#input-name", Input).value.strip()
        url = self.query_one("#input-url", Input).value.strip()
        key = self.query_one("#input-key", Input).value.strip()
        feedback = self.query_one("#dialog-feedback", Static)

        if not name:
            feedback.update("[red]Provider name is required[/]")
            return
        if not url:
            feedback.update("[red]Base URL is required[/]")
            return
        from agentic_debugger.application.provider_connections import add_provider_config
        try:
            cfg = add_provider_config(
                name=name,
                base_url=url,
                api_format=self._format,
                api_key=key or None,
            )
            self.app.pop_screen()
            self._on_save(cfg)
        except Exception as exc:
            feedback.update(f"[red]Error: {exc}[/]")


class EditProviderDialogScreen(Screen):
    """Dialog for editing an existing model provider."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, config: Any, on_save: Callable[[Any], None]) -> None:
        super().__init__()
        self._config = config
        self._on_save = on_save
        self._format = config.api_format

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-dialog-card"):
            yield Static(f"EDIT PROVIDER: {self._config.name}", id="dialog-title")
            yield Static("Provider Name", classes="dialog-label")
            yield Input(value=self._config.name, id="input-name")
            yield Static("Base URL", classes="dialog-label")
            yield Input(value=self._config.base_url, id="input-url")
            yield Static("API Key (leave blank to keep current)", classes="dialog-label")
            yield Input(password=True, placeholder="new API key", id="input-key")
            yield Static("API Protocol Format", classes="dialog-label")
            with Horizontal(id="format-buttons-row"):
                yield Button("Chat Completions", id="fmt-chat", classes=f"fmt-btn {'-selected' if self._format == 'chat_completions' else ''}")
                yield Button("Responses", id="fmt-resp", classes=f"fmt-btn {'-selected' if self._format == 'responses' else ''}")
                yield Button("Messages", id="fmt-msg", classes=f"fmt-btn {'-selected' if self._format == 'messages' else ''}")
            yield Static("", id="dialog-feedback")
            with Horizontal(id="dialog-actions-row"):
                yield Button("Save changes", id="btn-save-dialog", classes="primary-action")
                yield Button("Cancel", id="btn-cancel-dialog")

    def on_mount(self) -> None:
        self.query_one("#input-name", Input).focus()

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_save(None)

    def on_button_pressed(self, event: Any) -> None:
        btn_id = getattr(event.button, "id", "")
        if btn_id == "fmt-chat":
            self._format = "chat_completions"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "fmt-resp":
            self._format = "responses"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "fmt-msg":
            self._format = "messages"
            self._update_format_buttons()
            event.stop()
        elif btn_id == "btn-cancel-dialog":
            self.action_cancel()
            event.stop()
        elif btn_id == "btn-save-dialog":
            self._do_save()
            event.stop()

    def _update_format_buttons(self) -> None:
        for fid, val in [("fmt-chat", "chat_completions"), ("fmt-resp", "responses"), ("fmt-msg", "messages")]:
            try:
                b = self.query_one(f"#{fid}", Button)
                if self._format == val:
                    b.add_class("-selected")
                else:
                    b.remove_class("-selected")
            except Exception:
                pass

    def _do_save(self) -> None:
        name = self.query_one("#input-name", Input).value.strip()
        url = self.query_one("#input-url", Input).value.strip()
        key = self.query_one("#input-key", Input).value.strip()
        feedback = self.query_one("#dialog-feedback", Static)

        if not name:
            feedback.update("[red]Provider name is required[/]")
            return
        if not url:
            feedback.update("[red]Base URL is required[/]")
            return
        from agentic_debugger.application.provider_connections import update_provider_config
        try:
            cfg = update_provider_config(
                provider_id=self._config.provider_id,
                name=name,
                base_url=url,
                api_format=self._format,
                api_key=key or None,
            )
            self.app.pop_screen()
            self._on_save(cfg)
        except Exception as exc:
            feedback.update(f"[red]Error: {exc}[/]")


class AddManualModelDialogScreen(Screen):
    """Dialog for manually adding a model identifier to a provider."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, provider_id: str, on_save: Callable[[Optional[str], Optional[str], Optional[str]], None]) -> None:
        super().__init__()
        self._provider_id = provider_id
        self._on_save = on_save

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-dialog-card"):
            yield Static(f"ADD MANUAL MODEL ({self._provider_id})", id="dialog-title")
            yield Static("Model ID (sent to API)", classes="dialog-label")
            yield Input(placeholder="e.g. llama-3.3-70b-versatile or claude-3-7-sonnet", id="input-model-id")
            yield Static("Display Name (optional)", classes="dialog-label")
            yield Input(placeholder="e.g. Llama 3.3 70B", id="input-model-disp")
            yield Static("", id="dialog-feedback")
            with Horizontal(id="dialog-actions-row"):
                yield Button("Add model", id="btn-save-dialog", classes="primary-action")
                yield Button("Cancel", id="btn-cancel-dialog")

    def on_mount(self) -> None:
        self.query_one("#input-model-id", Input).focus()

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self._on_save(None, None, None)

    def on_button_pressed(self, event: Any) -> None:
        btn_id = getattr(event.button, "id", "")
        if btn_id == "btn-cancel-dialog":
            self.action_cancel()
            event.stop()
        elif btn_id == "btn-save-dialog":
            mid = self.query_one("#input-model-id", Input).value.strip()
            disp = self.query_one("#input-model-disp", Input).value.strip()
            feedback = self.query_one("#dialog-feedback", Static)
            if not mid:
                feedback.update("[red]Model ID is required[/]")
                return
            self.app.pop_screen()
            self._on_save(mid, disp or None, None)
            event.stop()


# Backward compatibility alias
ProviderConnectionsScreen = ModelProvidersScreen


class StartSessionScreen(Screen):
    """The ONE session-setup surface for every target and provider.

    One fixed stack of controls — Target, Task, Project, Bug, Repro,
    Verify, Model, Debugger, Time limit, Auto-retry — serves curated
    tasks, Local Project debugging, and the scientific capability ladder
    alike.  Rows never disappear: an inapplicable row is disabled with
    its reason.  One selection change never silently rewrites another.
    Every readiness presentation (Run button, status line, hero chips,
    pre-flight rail) renders from the single ``SessionReadiness``
    object derived by :func:`derive_readiness`.
    """

    BINDINGS = [
        Binding("up", "move_up", "Previous setting", show=False, priority=True),
        Binding("down", "move_down", "Next setting", show=False, priority=True),
        Binding("s", "start", "Run"),
        Binding("p", "focus_local_project", "Local project"),
        Binding("c", "open_providers", "Providers"),
        Binding("h", "history", "History"),
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("escape", "cancel", "Back"),
    ]

    def __init__(
        self,
        task_options: Optional[list[tuple[str, str]]] = None,
        *,
        initial_target: Optional[str] = None,
        initial_project: Optional[str] = None,
    ) -> None:
        super().__init__()
        from agentic_debugger.ui.app import task_display_option

        self._task_options: list[tuple[str, str]] = []
        for item in list(task_options or []):
            if isinstance(item, tuple) and len(item) == 2:
                label, value = item
                self._task_options.append(
                    task_display_option(value) if label == value else (label, value)
                )
            elif isinstance(item, str):
                self._task_options.append(task_display_option(item))
        self._config = SessionConfig()
        if initial_target in TARGET_LABELS:
            self._config = self._config.with_target(initial_target)
        self._catalog = SessionCatalog()
        self._project_status = ProjectStatus.unchecked("")
        self._readiness: Optional[SessionReadiness] = None
        self._start_error: Optional[str] = None
        # Manual-edit guards: automatic, project-derived defaults never
        # overwrite a value the user chose.
        self._repro_user_edited = False
        self._verify_user_edited = False
        self._repro_is_auto = False
        self._verify_is_auto = False
        # Launch-cwd capture for project resolution (never a cwd change).
        try:
            from agentic_debugger.application.local_project import (
                get_launch_cwd,
                resolve_project_path,
            )

            self._launch_cwd = get_launch_cwd()
        except Exception:
            self._launch_cwd = Path.cwd().resolve()
        initial_path = initial_project or str(self._launch_cwd)
        try:
            from agentic_debugger.application.local_project import (
                resolve_project_path,
            )

            self._config = replace(
                self._config, project_path=str(resolve_project_path(initial_path, self._launch_cwd))
            )
        except Exception:
            self._config = replace(self._config, project_path=initial_path)
        try:
            self._apply_tracked_repro_defaults()
        except Exception:
            pass

    # -- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="start-workspace"):
            with Vertical(id="start-main"):
                with VerticalScroll(id="start-config"):
                    yield Static("SESSION SETUP", id="start-section-label")
                    yield SessionSettingRow("Target", row_key=ROW_TARGET, id="target-row")
                    yield SessionSettingRow("Task", row_key=ROW_TASK, id="task-row")
                    yield SessionSettingRow("Project", row_key=ROW_PROJECT, id="project-row")
                    yield SessionSettingRow("Bug", row_key=ROW_BUG, id="bug-row")
                    yield SessionSettingRow("Repro", row_key=ROW_REPRO, id="repro-row")
                    yield SessionSettingRow("Verify (P2P)", row_key=ROW_VERIFY, id="verify-row")
                    yield SessionSettingRow("Model", row_key=ROW_MODEL, id="model-row")
                    yield SessionSettingRow("Debugger", row_key=ROW_DEBUGGER, id="debugger-row")
                    yield SessionSettingRow("Time limit", row_key=ROW_TIME_LIMIT, id="time-limit-row")
                    yield SessionSettingRow("Auto-retry", row_key=ROW_AUTO_RETRY, id="auto-retry-row")
                    yield Static("", id="start-status")
                    yield Static("", id="start-notes")
                    with Horizontal(id="start-actions"):
                        yield Button(
                            "Run", id="start-session-button", classes="primary-action"
                        )
                yield Static(START_FOOTER, id="start-footer")
            with VerticalScroll(id="start-context"):
                yield Static("[bold $primary]PRE-FLIGHT[/]", id="context-title")
                yield Static("", id="context-summary")

    def on_mount(self) -> None:
        if not self._task_options:
            self._task_options = list(self.app.curated_task_options())
        self._gather_catalog()
        first_curated = next(
            (task_id for _, task_id in self._task_options if not is_ladder_task(task_id)),
            None,
        )
        self._config = replace(self._config, task_id=first_curated)
        if self._config.target == TARGET_LOCAL_PROJECT:
            self._validate_project()
        self.render_state()
        self._focus_row(ROW_PROJECT if self._config.target == TARGET_LOCAL_PROJECT else ROW_TARGET)
        self._update_context_visibility(self.size.width)
        self._update_footer(self.size.width)

    def on_resize(self, event: Any) -> None:
        self._update_context_visibility(event.size.width)
        self._update_footer(event.size.width)
        if self.is_mounted:
            self.render_state()

    def _update_context_visibility(self, width: int) -> None:
        self.query_one("#start-context", VerticalScroll).display = width >= 100

    def _update_footer(self, width: int) -> None:
        footer = self.query_one("#start-footer", Static)
        footer.update(START_FOOTER_COMPACT if width < 100 else START_FOOTER)

    # -- catalog -------------------------------------------------------------

    def _gather_catalog(self) -> None:
        """Rebuild the read-only environment catalog (offline, no provider
        contact, no mutation)."""
        tasks: list[TaskOption] = []
        for label, task_id in self._task_options:
            title = label.split("·", 1)[0].strip() or task_id
            ladder = is_ladder_task(task_id)
            detail = ""
            if ladder:
                meta = ladder_task_metadata(task_id)
                detail = f"{meta.treatment} · {meta.evaluation}"
            tasks.append(TaskOption(task_id, title, ladder=ladder, detail=detail))

        models: list[ModelOption] = [
            ModelOption(
                PROVIDER_OFFLINE,
                "",
                "Offline",
                detail="",
            )
        ]
        provider_reasons: dict[str, Optional[str]] = {}
        try:
            for item in list_provider_models(include_ollama=True):
                if not item.available and item.provider_label not in provider_reasons:
                    provider_reasons.setdefault(
                        item.kind, item.unavailable_reason or "provider unavailable"
                    )
                models.append(
                    ModelOption(
                        item.kind,
                        item.model_id,
                        item.display_name,
                        detail=item.note or "",
                        available=item.available,
                        unavailable_reason=item.unavailable_reason,
                    )
                )
        except Exception:
            pass

        configured_error: Optional[str] = None
        try:
            summaries, configured_error = self.app.configured_profiles()
        except Exception as exc:  # pragma: no cover - bounded diagnostics
            summaries, configured_error = (), str(exc)
        for profile in summaries:
            models.append(
                ModelOption(
                    PROVIDER_CONFIGURED,
                    profile.profile_id,
                    profile.display_name,
                    detail="",
                )
            )

        ladder_models: list[ModelOption] = []
        try:
            for item in self.app.ollama_cloud_model_profiles():
                ladder_models.append(
                    ModelOption(
                        PROVIDER_OLLAMA,
                        item.alias,
                        item.display_name,
                        detail="",
                    )
                )
        except Exception:
            pass

        self._catalog = SessionCatalog(
            tasks=tuple(tasks),
            models=tuple(models),
            ladder_models=tuple(ladder_models),
            configured_error=configured_error,
        )

    # -- project validation ---------------------------------------------------

    def _validate_project(self) -> None:
        try:
            from agentic_debugger.application.local_project import (
                validate_local_project,
            )

            validated = validate_local_project(
                self._config.project_path, launch_cwd=self._launch_cwd
            )
            if validated.dirty:
                self._project_status = ProjectStatus(
                    path=str(validated.repo_root),
                    ok=False,
                    state="dirty",
                    message=(
                        "Project has uncommitted changes — commit or stash "
                        "them first."
                    ),
                )
            else:
                self._project_status = ProjectStatus(
                    path=str(validated.repo_root),
                    ok=True,
                    state="clean",
                    message=f"Git: {validated.repo_root.name} @ {validated.head_commit[:7]}",
                )
        except Exception as exc:
            message = self._project_error_message(exc)
            self._project_status = ProjectStatus(
                path=self._config.project_path, ok=False, state="invalid", message=message
            )

    @staticmethod
    def _project_error_message(exc: Exception) -> str:
        msg = str(exc)
        if "not a Git repository" in msg:
            return "Not a Git repository."
        if "not found" in msg:
            return "Project path not found."
        if "not a directory" in msg:
            return "Project path is not a directory."
        bounded = msg[:96]
        return bounded

    def _apply_tracked_repro_defaults(self) -> None:
        """Prefill Repro/Verify from a tracked ``repro.py`` (safe checks only).

        Never overwrites a manual value; never invents commands beyond the
        documented convention.
        """
        if self._repro_user_edited and self._verify_user_edited:
            return
        has_repro = False
        try:
            from agentic_debugger.application.local_project import (
                has_tracked_root_repro,
                validate_local_project,
            )

            validated = validate_local_project(
                self._config.project_path, launch_cwd=self._launch_cwd
            )
            has_repro = has_tracked_root_repro(validated.repo_root)
        except Exception:
            has_repro = False
        if not self._repro_user_edited:
            if has_repro:
                if (
                    self._config.reproduction_command is None
                    or self._repro_is_auto
                ):
                    self._config = replace(
                        self._config, reproduction_command="python repro.py"
                    )
                    self._repro_is_auto = True
            elif self._repro_is_auto and self._config.reproduction_command == "python repro.py":
                self._config = replace(self._config, reproduction_command=None)
                self._repro_is_auto = False
        if not self._verify_user_edited:
            if has_repro:
                if self._config.verification_command is None or self._verify_is_auto:
                    self._config = replace(
                        self._config, verification_command="python repro.py"
                    )
                    self._verify_is_auto = True
            elif self._verify_is_auto and self._config.verification_command == "python repro.py":
                self._config = replace(self._config, verification_command=None)
                self._verify_is_auto = False

    # -- navigation ------------------------------------------------------------

    def _focusable_row_ids(self) -> list[str]:
        # The stack is fixed: every row stays reachable for every target.
        return list(ROW_ORDER)

    def _focus_row(self, row_key: str) -> None:
        try:
            self.query_one(f"#{row_key.replace('_', '-')}-row", SessionSettingRow).focus()
        except Exception:
            pass

    def _focused_row_key(self) -> str:
        return getattr(self.app.focused, "row_key", ROW_TARGET)

    def action_move_down(self) -> None:
        rows, current = self._focusable_row_ids(), self._focused_row_key()
        self._focus_row(
            rows[(rows.index(current) + 1) % len(rows)] if current in rows else rows[0]
        )

    def action_move_up(self) -> None:
        rows, current = self._focusable_row_ids(), self._focused_row_key()
        self._focus_row(
            rows[(rows.index(current) - 1) % len(rows)] if current in rows else rows[0]
        )

    def _activate_row(self, row_key: str) -> None:
        readiness = self._readiness
        if readiness is not None:
            state = readiness.rows.get(row_key)
            if state is not None and not state.enabled:
                self.notify(
                    f"Not used for {TARGET_LABELS[self._config.target]} sessions — "
                    f"{state.reason}",
                    severity="information",
                    timeout=4.0,
                )
                return
        if row_key == ROW_TARGET:
            self._open_target_picker()
        elif row_key == ROW_TASK:
            self._open_task_picker()
        elif row_key == ROW_PROJECT:
            self._open_project_picker()
        elif row_key == ROW_BUG:
            self._open_bug_editor()
        elif row_key == ROW_REPRO:
            self._open_text_editor(
                "Reproduction command (optional)",
                self._config.reproduction_command or "",
                self._on_repro_saved,
            )
        elif row_key == ROW_VERIFY:
            self._open_text_editor(
                "Regression check command (optional; must pass BEFORE and after the fix)",
                self._config.verification_command or "",
                self._on_verify_saved,
            )
        elif row_key == ROW_MODEL:
            self._open_model_picker()
        elif row_key == ROW_DEBUGGER:
            self._open_debugger_picker()
        elif row_key == ROW_TIME_LIMIT:
            self._open_time_limit_editor()
        elif row_key == ROW_AUTO_RETRY:
            self._open_auto_retry_picker()

    # -- pickers ----------------------------------------------------------------

    def _open_target_picker(self) -> None:
        descriptions = {
            TARGET_CURATED: "Reproducible in-repo fixture; offline or any provider.",
            TARGET_LOCAL_PROJECT: "Your clean Git repository; describe the bug.",
            TARGET_LADDER: "Scientific capability rungs; qualified Ollama models.",
        }
        choices = [
            ChoiceOption(
                target,
                TARGET_LABELS[target],
                descriptions[target],
            )
            for target in (TARGET_CURATED, TARGET_LOCAL_PROJECT, TARGET_LADDER)
        ]
        self.app.push_screen(
            ChoicePickerScreen(
                title="Debug what?",
                choices=choices,
                current=self._config.target,
                on_select=lambda value: self._choice_selected(ROW_TARGET, value),
            )
        )

    def _open_task_picker(self) -> None:
        target = self._config.target
        choices: list[ChoiceOption] = []
        for task in self._catalog.tasks:
            if task.ladder:
                disabled = target != TARGET_LADDER
                reason = (
                    "" if not disabled else "runs under the Capability ladder target"
                )
                group = "CAPABILITY LADDER"
            else:
                disabled = target == TARGET_LADDER
                reason = "" if not disabled else "ladder runs use Level rungs"
                group = "CURATED TASKS"
            choices.append(
                ChoiceOption(
                    task.task_id,
                    task.title,
                    task.detail,
                    secondary=task.task_id,
                    group=group,
                    disabled=disabled,
                    disabled_reason=reason,
                )
            )
        self.app.push_screen(
            ChoicePickerScreen(
                title="Select task",
                choices=choices,
                current=self._config.task_id,
                on_select=lambda value: self._choice_selected(ROW_TASK, value),
            )
        )

    def _model_choice_key(self, choice: ModelChoice) -> str:
        return f"{choice.provider}:{choice.model_id}"

    def _open_model_picker(self) -> None:
        self._gather_catalog()
        target = self._config.target
        choices: list[ChoiceOption] = []
        offline_ok, offline_reason = model_compatibility(
            target, ModelOption(PROVIDER_OFFLINE, "", "Offline")
        )
        offline_group_note = "unavailable for Capability Ladder" if target == TARGET_LADDER else ""
        choices.append(
            ChoiceOption(
                self._model_choice_key(OFFLINE_CHOICE),
                "Offline",
                "",
                group="OFFLINE",
                group_note=offline_group_note,
                disabled=not offline_ok,
                disabled_reason=offline_reason,
            )
        )
        groups = (
            (PROVIDER_OLLAMA, "OLLAMA CLOUD"),
            (PROVIDER_OPENCODE, "OPENCODE GO"),
            (PROVIDER_COMMANDCODE, "COMMANDCODE GOAT"),
            (PROVIDER_CONFIGURED, "CUSTOM COMMAND PROFILES"),
        )

        # One stable provider world for every target.  The qualified roster
        # annotates Ollama entries; it never replaces the general catalog or
        # hides the other provider groups.  Missing qualified aliases are
        # merged into the one Ollama group, not duplicated in a second island.
        options_by_provider: dict[str, list[ModelOption]] = {
            provider: [] for provider, _ in groups
        }
        seen_keys: set[tuple[str, str]] = set()
        for option in self._catalog.models:
            if option.provider == PROVIDER_OFFLINE:
                continue
            key = (option.provider, option.model_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            options_by_provider.setdefault(option.provider, []).append(option)
        for option in self._catalog.ladder_models:
            key = (option.provider, option.model_id)
            if key not in seen_keys:
                seen_keys.add(key)
                options_by_provider[PROVIDER_OLLAMA].append(option)

        for provider, group in groups:
            provider_options = options_by_provider.get(provider, [])
            group_note = ""
            if target == TARGET_LADDER and provider != PROVIDER_OLLAMA:
                group_note = "unavailable for Capability Ladder"
            elif provider == PROVIDER_CONFIGURED and self._catalog.configured_error:
                group_note = "configuration error"
            elif provider == PROVIDER_CONFIGURED and not provider_options:
                group_note = "none configured"
            elif provider_options and not any(opt.available for opt in provider_options):
                first_reason = provider_options[0].unavailable_reason or ""
                if "auth store not found" in first_reason.lower() or "cli not found" in first_reason.lower():
                    group_note = "not configured"
                else:
                    group_note = _short_unavailable_reason(first_reason)

            if not provider_options:
                reason = (
                    _short_unavailable_reason(self._catalog.configured_error)
                    if provider == PROVIDER_CONFIGURED and self._catalog.configured_error
                    else f"No {PROVIDER_LABELS[provider]} models configured"
                )
                title = (
                    "Configuration error"
                    if provider == PROVIDER_CONFIGURED and self._catalog.configured_error
                    else "None configured"
                )
                choices.append(
                    ChoiceOption(
                        f"unavailable:{provider}",
                        title,
                        "",
                        group=group,
                        group_note=group_note,
                        disabled=True,
                        disabled_reason=reason,
                    )
                )
                continue

            for index, option in enumerate(provider_options):
                qualified = self._catalog.ladder_model(option.choice)
                effective = qualified or option
                compatible, compat_reason = model_compatibility(
                    target,
                    effective,
                    ladder_qualified=qualified is not None,
                )
                disabled = not effective.available or not compatible
                if not compatible:
                    reason = compat_reason
                elif not effective.available:
                    reason = _short_unavailable_reason(effective.unavailable_reason)
                else:
                    reason = ""
                if provider == PROVIDER_CONFIGURED:
                    display_name = effective.display or effective.model_id
                else:
                    display_name = format_model_display_name(effective.display or effective.model_id)
                # Discovered-catalog detail (direct-API protocol family or
                # the bounded unresolved-protocol note) stays secondary.
                secondary = effective.detail if effective.available else ""
                choices.append(
                    ChoiceOption(
                        self._model_choice_key(effective.choice),
                        display_name,
                        "",
                        secondary=secondary,
                        group=group if index == 0 else "",
                        group_note=group_note if index == 0 or group_note else "",
                        disabled=disabled,
                        disabled_reason=reason,
                    )
                )
        choices.append(
            ChoiceOption(
                "providers:manage",
                "Manage provider connections…",
                "status, model refresh, API key (press c anytime)",
                group="",
            )
        )
        self.app.push_screen(
            ChoicePickerScreen(
                title="Select model",
                choices=choices,
                current=self._model_choice_key(self._config.model),
                on_select=lambda value: self._choice_selected(ROW_MODEL, value),
            )
        )

    def _open_debugger_picker(self) -> None:
        choices = [
            ChoiceOption(
                POLICY_ON_UNCERTAINTY,
                POLICY_LABELS[POLICY_ON_UNCERTAINTY],
                "Attach PDB when runtime evidence is useful.",
            ),
            ChoiceOption(
                POLICY_STATIC_BASELINE,
                POLICY_LABELS[POLICY_STATIC_BASELINE],
                "Static reasoning only; no debugger session.",
            ),
        ]
        self.app.push_screen(
            ChoicePickerScreen(
                title="Select debugger policy",
                choices=choices,
                current=self._config.debugger_policy,
                on_select=lambda value: self._choice_selected(ROW_DEBUGGER, value),
            )
        )

    def _open_auto_retry_picker(self) -> None:
        choices = [
            ChoiceOption("0", "No auto-retry", "fail fast; retry manually with r"),
            ChoiceOption("1", "1 auto-retry", "one fresh attempt on retryable failure"),
            ChoiceOption("2", "2 auto-retries", "two fresh attempts"),
            ChoiceOption("3", "3 auto-retries", "maximum"),
        ]
        self.app.push_screen(
            ChoicePickerScreen(
                title="Auto-retry on failure",
                choices=choices,
                current=str(self._config.auto_retries),
                on_select=lambda value: self._choice_selected(ROW_AUTO_RETRY, value),
            )
        )

    def _open_project_picker(self) -> None:
        self.app.push_screen(
            ChoicePickerScreen(
                title="Project input",
                choices=[
                    ChoiceOption("use_cwd", "Use current directory", f"{self._launch_cwd}"),
                    ChoiceOption("browse", "Browse…", "Pick via directory list"),
                    ChoiceOption("type", "Type/paste path…", "Enter absolute or relative path"),
                ],
                current=None,
                on_select=self._project_choice_selected,
            )
        )

    def _project_choice_selected(self, value: str) -> None:
        if value == "use_cwd":
            self._set_project(str(self._launch_cwd))
        elif value == "browse":
            self.app.push_screen(
                BrowseScreen(
                    start_path=self._config.project_path, on_select=self._on_browse_selected
                )
            )
        elif value == "type":
            self._open_text_editor(
                "Project path", self._config.project_path, self._on_project_saved
            )

    def _set_project(self, path: str) -> None:
        try:
            from agentic_debugger.application.local_project import (
                resolve_project_path,
            )

            resolved = str(resolve_project_path(path, self._launch_cwd))
        except Exception:
            resolved = path
        self._config = replace(self._config, project_path=resolved)
        try:
            self._apply_tracked_repro_defaults()
        except Exception:
            pass
        if self._config.target == TARGET_LOCAL_PROJECT:
            self._validate_project()
        self.render_state()
        self._focus_row(ROW_PROJECT)

    def _on_browse_selected(self, path: str) -> None:
        self._set_project(path)

    def _on_project_saved(self, value: Optional[str]) -> None:
        if value is not None:
            self._set_project(value)
        else:
            self.render_state()
            self._focus_row(ROW_PROJECT)

    def _open_bug_editor(self) -> None:
        self.app.push_screen(
            BugDescriptionEditorScreen(
                current=self._config.bug_description or "",
                on_save=self._on_bug_saved,
            )
        )

    def _on_bug_saved(self, value: Optional[str]) -> None:
        if value is not None:
            self._config = replace(self._config, bug_description=value)
        self.render_state()
        self._focus_row(ROW_BUG)

    def _on_repro_saved(self, value: Optional[str]) -> None:
        if value is None:
            self.render_state()
            self._focus_row(ROW_REPRO)
            return
        self._repro_user_edited = True
        self._repro_is_auto = False
        self._config = replace(
            self._config,
            reproduction_command=value.strip() if value.strip() else None,
        )
        self.render_state()
        self._focus_row(ROW_REPRO)

    def _on_verify_saved(self, value: Optional[str]) -> None:
        if value is None:
            self.render_state()
            self._focus_row(ROW_VERIFY)
            return
        self._verify_user_edited = True
        self._verify_is_auto = False
        self._config = replace(
            self._config,
            verification_command=value.strip() if value.strip() else None,
        )
        self.render_state()
        self._focus_row(ROW_VERIFY)

    def _open_text_editor(
        self, title: str, current: str, on_save: Any, multiline: bool = False
    ) -> None:
        self.app.push_screen(
            SingleLineFieldEditorScreen(
                title=title,
                current=current or "",
                on_save=on_save,
                placeholder=title,
            )
        )

    def _open_time_limit_editor(self) -> None:
        self.app.push_screen(
            TimeLimitEditorScreen(
                current=self._config.time_limit_seconds,
                on_save=self._time_limit_saved,
                on_cancel=lambda: self._focus_row(ROW_TIME_LIMIT),
            )
        )

    def _time_limit_saved(self, value: Optional[int]) -> None:
        self._config = replace(self._config, time_limit_seconds=value)
        self.render_state()
        self._focus_row(ROW_TIME_LIMIT)

    # -- selection change (the single mutation entry point) ---------------------

    def _choice_selected(self, row_key: str, value: str) -> None:
        self._start_error = None
        if row_key == ROW_TARGET:
            self._config = self._config.with_target(value)
            if value == TARGET_LOCAL_PROJECT:
                self._validate_project()
                try:
                    self._apply_tracked_repro_defaults()
                except Exception:
                    pass
        elif row_key == ROW_TASK:
            self._config = replace(self._config, task_id=value)
        elif row_key == ROW_MODEL:
            if value == "providers:manage":
                # The picker's management entry never mutates the model
                # selection; it opens the provider-connections surface.
                self.app.push_screen(ProviderConnectionsScreen())
                return
            provider, _, model_id = value.partition(":")
            if provider == PROVIDER_OFFLINE:
                self._config = replace(self._config, model=OFFLINE_CHOICE)
            else:
                option = next(
                    (
                        m
                        for m in self._catalog.models + self._catalog.ladder_models
                        if m.provider == provider and m.model_id == model_id
                    ),
                    None,
                )
                if provider == PROVIDER_CONFIGURED:
                    display = option.display if option else model_id
                else:
                    display = format_model_display_name(option.display if option else model_id)
                self._config = replace(
                    self._config,
                    model=ModelChoice(provider, model_id, display),
                )
        elif row_key == ROW_DEBUGGER:
            self._config = replace(self._config, debugger_policy=value)
        elif row_key == ROW_AUTO_RETRY:
            try:
                self._config = replace(
                    self._config, auto_retries=max(0, min(int(value), AUTO_RETRY_MAX))
                )
            except ValueError:
                return
        self.render_state()
        self._focus_row(row_key)

    # -- rendering (single derivation, many surfaces) -----------------------------

    def _task_display_name(self) -> str:
        task = self._catalog.find_task(self._config.task_id)
        if task is not None:
            title = task.title
        elif self._config.task_id:
            title = next(
                (
                    label.split("·", 1)[0].strip()
                    for label, task_id in self._task_options
                    if task_id == self._config.task_id
                ),
                self._config.task_id,
            )
        else:
            title = "Not selected"
        if self.size.width and self.size.width < 70:
            available = max(18, self.size.width - 20)
            if len(title) > available:
                return f"{title[: available - 1]}…"
        return title

    def _model_display(self) -> tuple[str, str]:
        choice = self._config.model
        if choice.is_offline:
            return "Offline", "Offline"
        label = PROVIDER_LABELS.get(choice.provider, choice.provider)
        if choice.provider == PROVIDER_CONFIGURED:
            display = choice.display or choice.model_id
        else:
            display = format_model_display_name(choice.display or choice.model_id)
        return display, label

    def _debugger_display(self) -> str:
        if self._config.target == TARGET_LADDER:
            task = self._catalog.find_task(self._config.task_id)
            if task is not None and task.ladder:
                return ladder_task_metadata(task.task_id).debugger
            return "Frozen contract"
        if self._config.target == TARGET_LOCAL_PROJECT:
            return POLICY_LABELS[POLICY_ON_UNCERTAINTY]
        return POLICY_LABELS.get(self._config.debugger_policy, self._config.debugger_policy)

    def _bug_preview(self) -> str:
        text = self._config.bug_description.strip()
        if not text:
            return "—"
        first = text.splitlines()[0][:48] + ("…" if len(text.splitlines()[0]) > 48 else "")
        if "\n" in text:
            first = f"{first} [+]" if first else "Described [+]"
        return first or "Described"

    def _config_content_width(self) -> int:
        """Usable width of the configuration column (rail steals 36 cells at 100+)."""
        rail = 36 if self.size.width >= 100 else 0
        return max(30, self.size.width - rail - 6)

    _hero_content_width = _config_content_width

    def render_state(self) -> None:
        """Derive readiness once and render every surface from it."""
        self._readiness = derive_readiness(
            self._config, self._catalog, self._project_status
        )
        readiness = self._readiness
        config = self._config
        local = config.target == TARGET_LOCAL_PROJECT

        # -- rows (width-aware: the whole line fits or ellipsizes) ---------
        # Row chrome is 2 prefix + 14 label; one cell of scrollbar slack.
        width = self._config_content_width()
        row_budget = max(12, width - 17)

        def fitted(row_key: str, value: str, secondary: str = "") -> None:
            state = readiness.rows.get(row_key)
            reason = state.reason if state is not None and not state.enabled else ""
            value, secondary, reason = _fit_row_cells(
                value, secondary, reason, row_budget
            )
            row = self._row(row_key)
            row.set_value(value, secondary=secondary)
            if state is not None and not state.enabled:
                row.set_disabled(reason)
            else:
                row.set_enabled()

        fitted(ROW_TARGET, TARGET_LABELS[config.target])
        fitted(
            ROW_TASK,
            "" if local else self._task_display_name(),
            "" if local else (config.task_id or ""),
        )
        fitted(ROW_PROJECT, (config.project_path or "") if local else "")
        fitted(ROW_BUG, self._bug_preview() if local else "")
        fitted(
            ROW_REPRO,
            (config.reproduction_command or "Not set (optional)") if local else "",
        )
        fitted(
            ROW_VERIFY,
            (config.verification_command or "Not set (optional)") if local else "",
        )
        model_value, model_secondary = self._model_display()
        fitted(ROW_MODEL, model_value, model_secondary)
        fitted(ROW_DEBUGGER, self._debugger_display())
        fitted(
            ROW_TIME_LIMIT,
            "No limit"
            if config.time_limit_seconds is None
            else str(config.time_limit_seconds),
        )
        fitted(ROW_AUTO_RETRY, f"{config.auto_retries} on retryable failure")

        # -- blockers / status (concise actionable blocker when necessary) --
        status = self.query_one("#start-status", Static)
        if self._start_error is not None:
            status.update(f"[{ERROR}]! Start failed — {_markup_escape(self._start_error)}[/]")
        elif not readiness.ready:
            errors = [item for item in readiness.issues if item.severity == SEVERITY_ERROR]
            status.update("\n".join(f"[{ERROR}]! {_markup_escape(item.message)}[/]" for item in errors))
        else:
            status.update("")

        # Trust notices stay visible at every width (not only in the rail)
        notes = self.query_one("#start-notes", Static)
        if readiness.notes and config.model.provider == PROVIDER_CONFIGURED:
            notes.update(
                f"[{FAINT}]{'   '.join(_markup_escape(note) for note in readiness.notes)}[/]"
            )
        else:
            notes.update("")

        # -- run button -----------------------------------------------------
        button = self.query_one("#start-session-button", Button)
        button.label = readiness.run_label
        button.disabled = not readiness.ready

        self._update_context(readiness)

    def _row(self, row_key: str) -> SessionSettingRow:
        return self.query_one(f"#{row_key.replace('_', '-')}-row", SessionSettingRow)

    def _update_context(self, readiness: SessionReadiness) -> None:
        config = self._config
        lines: list[str] = []

        def kv(label: str, value: str) -> None:
            lines.append(f"[{MUTED}]{label}[/]\n[{FOREGROUND}]{_markup_escape(value)}[/]")

        kv("Target", TARGET_LABELS[config.target])
        if config.target != TARGET_LOCAL_PROJECT:
            kv("Task", self._task_display_name())
            kv("Task ID", config.task_id or "Not selected")
        else:
            kv("Project", config.project_path or "—")
            kv("Repo", self._project_status.message if self._project_status.state != "unchecked" else "—")
            kv("Bug", self._bug_preview())
            kv("Repro", config.reproduction_command or "Not set")
            kv("Verify", config.verification_command or "Not set")
        model_value, model_secondary = self._model_display()
        kv("Model", model_value)
        kv("Provider", model_secondary)
        kv("Debugger", self._debugger_display())
        kv("Time limit", "No limit" if config.time_limit_seconds is None else str(config.time_limit_seconds))
        if config.target == TARGET_LADDER:
            task = self._catalog.find_task(config.task_id)
            if task is not None and task.ladder:
                meta = ladder_task_metadata(task.task_id)
                kv("Treatment", meta.treatment)
                kv("Evaluation", meta.evaluation)

        if readiness.issues:
            lines.append("")
            lines.append(f"[bold {ERROR}]CHECKS[/]")
            for issue in readiness.issues:
                marker = "!" if issue.severity == SEVERITY_ERROR else "?"
                style = ERROR if issue.severity == SEVERITY_ERROR else WARNING
                lines.append(f"[{style}]{marker} {_markup_escape(issue.message)}[/]")
        if readiness.notes:
            lines.append("")
            lines.append(f"[bold {EVIDENCE}]NOTICES[/]")
            for note in readiness.notes:
                lines.append(f"[{MUTED}]· {_markup_escape(note)}[/]")

        lines.append("")
        if readiness.ready:
            lines.append(f"[bold {SUCCESS}]READY  Yes[/]  [{MUTED}]→ {readiness.run_label}[/]")
        else:
            errors = sum(1 for i in readiness.issues if i.severity == SEVERITY_ERROR)
            lines.append(f"[bold {ERROR}]READY  No[/]  [{MUTED}]· {errors} blocking issue(s)[/]")
        self.query_one("#context-summary", Static).update("\n".join(lines))

    # -- run ----------------------------------------------------------------------

    @property
    def start_available(self) -> bool:
        return self._readiness.ready if self._readiness is not None else False

    @property
    def task_id(self) -> Optional[str]:
        return self._config.task_id

    @property
    def profile_id(self) -> Optional[str]:
        return None if self._config.model.is_offline else self._config.model.model_id

    def action_edit(self) -> None:
        self._activate_row(self._focused_row_key())

    def action_confirm(self) -> None:
        self.action_edit()

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def action_history(self) -> None:
        self.app.pop_screen()

    def action_focus_local_project(self) -> None:
        """P: jump straight to the Local Project controls (same screen)."""
        if self._config.target != TARGET_LOCAL_PROJECT:
            self._choice_selected(ROW_TARGET, TARGET_LOCAL_PROJECT)
        self._focus_row(ROW_PROJECT)

    def action_open_providers(self) -> None:
        """C: open the provider-connections management surface."""
        self.app.push_screen(ProviderConnectionsScreen())

    def action_quit_app(self) -> None:
        self.app.action_quit()

    def _refresh_for_start(self) -> None:
        """Re-gather truth immediately before starting (fail closed on a
        changed environment rather than launching a stale selection)."""
        self._gather_catalog()
        if self._config.target == TARGET_LOCAL_PROJECT:
            self._validate_project()
        self.render_state()

    def _start(self) -> None:
        from agentic_debugger.application.events import SourceKind

        self._start_error = None
        self._refresh_for_start()
        readiness = self._readiness
        if readiness is None or not readiness.ready:
            return  # the status line already states the first blocker
        config = self._config
        try:
            if config.target == TARGET_LADDER:
                task_id = str(config.task_id)
                source_kind = (
                    SourceKind.LEVEL32_OPERATOR
                    if task_id == LEVEL32_TASK_ID
                    else SourceKind.OLLAMA_CLOUD_LADDER
                )
                self.app.start_live_session(
                    task_id=task_id,
                    policy=(
                        "exact-pdb-level32-frozen"
                        if task_id == LEVEL32_TASK_ID
                        else "pdb-on-uncertainty"
                    ),
                    max_elapsed_seconds=None,
                    source_kind=source_kind,
                    profile_id=config.model.model_id,
                )
                return
            if config.target == TARGET_LOCAL_PROJECT:
                provider = (
                    None
                    if config.model.provider == PROVIDER_CONFIGURED
                    else config.model.provider
                )
                self.app.start_local_project_session(
                    project_path=config.project_path,
                    bug_description=config.bug_description.strip(),
                    reproduction_command=config.reproduction_command,
                    verification_command=config.verification_command,
                    profile_id=config.model.model_id,
                    model_provider=provider,
                    max_elapsed_seconds=config.time_limit_seconds,
                    auto_retries=config.auto_retries,
                )
                return
            # Curated target: the model selection routes the source.
            if config.model.is_offline:
                self.app.start_live_session(
                    task_id=str(config.task_id),
                    policy=config.debugger_policy,
                    max_elapsed_seconds=config.time_limit_seconds,
                    source_kind=SourceKind.OFFLINE_DEMO,
                    profile_id=None,
                )
            elif config.model.provider == PROVIDER_CONFIGURED:
                self.app.start_live_session(
                    task_id=str(config.task_id),
                    policy=config.debugger_policy,
                    max_elapsed_seconds=config.time_limit_seconds,
                    source_kind=SourceKind.CONFIGURED_MODEL,
                    profile_id=config.model.model_id,
                )
            else:
                self.app.start_live_session(
                    task_id=str(config.task_id),
                    policy=config.debugger_policy,
                    max_elapsed_seconds=config.time_limit_seconds,
                    source_kind=SourceKind.CONFIGURED_MODEL,
                    profile_id=config.model.model_id,
                    model_provider=config.model.provider,
                )
        except Exception as exc:
            self._start_error = str(exc)
            self.render_state()

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
        self.query_one("#browse-current", Static).update(
            f"[bold {PRIMARY}]Current:[/] {_markup_escape(cur_text)}"
        )
        option_list = self.query_one("#browse-list", OptionList)
        option_list.clear_options()
        # First option is "Select current directory"
        option_list.add_option(
            Text(
                f"▶ Use current directory: {self._current.name or str(self._current)}",
                style=f"bold {SUCCESS}",
            )
        )
        # Parent
        parent = self._current.parent
        if parent != self._current:
            option_list.add_option(Text(f"↑ Parent: {parent}", style=MUTED))
        # Children
        try:
            from agentic_debugger.application.local_project import list_child_directories
            children = list_child_directories(self._current)
            for child in children[:64]:
                option_list.add_option(Text(f"  {child.name}/", style=FOREGROUND))
            if len(children) > 64:
                option_list.add_option(Text(f"  … +{len(children)-64} more", style="dim"))
        except Exception as exc:
            option_list.add_option(Text(f"(cannot list: {exc})", style=ERROR))
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
                yield Button("Save", id="bug-save-button", classes="primary-action")
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
                f"[bold {PRIMARY}]Agentic Debugger[/]\n"
                "[dim]Keyboard reference and evidence guide[/]",
                id="help-title",
            )
            yield Static(
                f"[bold {PRIMARY}]Session setup[/]\n"
                "  • Target — Curated task (offline/any provider) · Local project\n"
                "             (your repo) · Capability ladder (scientific rungs)\n"
                "  • Model — one picker: Offline · Ollama Cloud · OpenCode Go ·\n"
                "             CommandCode GOAT · custom command profiles\n"
                "  • Incompatible rows stay visible, dimmed with their reason\n"
                "\n"
                f"[bold {PRIMARY}]Independent proof chain[/]\n"
                "  FAILURE  →  PDB EVIDENCE  →  PATCH  →  VERIFIER VERDICT\n"
                "  The run may finish; only the verifier can close the case.\n"
                "\n"
                f"[bold {PRIMARY}]Session modes[/]\n"
                "  • LIVE — Executing session (offline, provider model, or command model)\n"
                "  • REPLAY — Read-only recorded session from authoritative journal\n"
                "\n"
                f"[bold {PRIMARY}]Workspace views[/]\n"
                "  • Live — Operational execution story\n"
                "  • Evidence — Causal proof state\n"
                "  • Source — Source evidence\n"
                "  • Debugger — Runtime/PDB evidence\n"
                "  • Patch — Candidate lifecycle/diff\n"
                "  • Verifier — Independent correctness authority\n"
                "  • Timeline — Session time consumption\n"
                "\n"
                f"[bold {EVIDENCE}]Evidence rule:[/] [bold]An applied patch is not automatically a fix.[/]\n"
                "[dim]Only the independent verifier can mark a candidate RESOLVED.[/]\n"
                "\n"
                f"[bold {PRIMARY}]Navigation[/]\n"
                "  • Home — S start debugging · P local project · H session history · ? help\n"
                "  • Setup — ↑/↓ move · Enter edit · S run · P local project ·\n"
                "            H history · Esc back\n"
                "  • History — ↑/↓ move · Enter/O open replay · S new session ·\n"
                "              P local project · R refresh · Esc home\n"
                "  • Workspace — Left/Right switch views · 1–7 direct tabs\n"
                "                \\[ / ] previous/next event · { / } previous/next phase\n"
                "                G/Shift+G begin/end · J jump · C cancel live\n"
                "                H history · N new session · A apply candidate · ? help",
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
    "HistoryScreen",
    "HomeActionRow",
    "HomeScreen",
    "JumpToSequenceScreen",
    "StartSessionScreen",
    "WorkspaceMode",
    "WorkspaceScreen",
    "render_view_header",
]
