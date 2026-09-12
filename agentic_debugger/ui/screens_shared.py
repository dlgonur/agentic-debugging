"""Shared screen vocabulary for the Agentic Debugger TUI.

Footer keyboard vocabulary, history/classification style maps, pure
recorded-text formatting helpers, the shared session view header, and the
product help modal pushed from home, history, setup, and workspace alike.

Screens are presentation-only: no controller, PDB, patch, verifier, or
model work happens here.
"""

from __future__ import annotations

from typing import Any, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from agentic_debugger.application.events import (
    OperatorStage,
    SessionEventKind,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
)
from agentic_debugger.application.history import HistoryClassification
from agentic_debugger.application.presentation import SessionViewState
from agentic_debugger.ui.theme import (
    ERROR,
    EVIDENCE,
    FAINT,
    PRIMARY,
    SUCCESS,
    WARNING,
)
from agentic_debugger.ui.widgets import session_tokens_summary


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
    # Cumulative provider-reported token usage (live and replay alike):
    # absent when no completed request reported usable usage, visibly
    # "(partial)" when coverage is incomplete, never a fabricated total.
    tokens_summary = session_tokens_summary(view.token_usage)
    if tokens_summary is not None:
        head.append(f"  ·  {tokens_summary}")
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
            # Surface the typed cause the architecture already owns (e.g.
            # malformed_directive, illegal_action, invalid_argument) instead
            # of only a generic "model error"; the bounded timeline retains
            # the same typed information.  When an HTTP error occurs, surface
            # the sanitized bounded error detail (HTTP status and provider snippet).
            error_kind = getattr(view, "latest_model_error_kind", None)
            error_msg = getattr(view, "latest_model_error_message", None)
            if type(error_kind) is str and error_kind:
                if (
                    type(error_msg) is str
                    and error_msg
                    and error_msg != error_kind
                    and error_kind == "http_error"
                ):
                    verifier = f"model error ({error_kind} · {error_msg})"
                else:
                    verifier = f"model error ({error_kind})"
            else:
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


def _reveal_option_list_highlight(option_list: Any) -> None:
    """Reveal the current highlight now and after the pending layout.

    ``OptionList.scroll_to_highlight()`` runs synchronously against the
    current ``virtual_size``/``container_size``.  During a
    clear/add rebuild (or a details-pane show/hide that follows a
    highlight change) that geometry is stale, so the immediate scroll is
    clamped to a stale ``max_scroll_y`` and never retried.  Scheduling a
    second reveal via the canonical ``call_after_refresh`` lifecycle
    retries the same reveal once layout has refreshed, without sleeps,
    hardcoded offsets, or index special-casing.  Never raises.
    """
    try:
        option_list.refresh(layout=True)
    except Exception:
        pass
    try:
        option_list.scroll_to_highlight()
    except Exception:
        pass
    try:
        schedule = getattr(option_list, "call_after_refresh", None)
        reveal = getattr(option_list, "scroll_to_highlight", None)
        if callable(schedule) and callable(reveal):
            try:
                schedule(reveal)
            except Exception:
                pass
    except Exception:
        pass


def ensure_option_list_highlight_visible(option_list: Any) -> None:
    """Keep the current highlight scrolled into view (native-key path).

    Native ``OptionList`` bindings (End/Home/Up/Down/PageUp/PageDown)
    assign ``highlighted`` through the reactive watcher, bypassing
    :func:`set_option_list_highlight`.  Screens must call this from
    ``on_option_list_option_highlighted`` (after any details/layout
    update) and from ``on_resize`` so keyboard movement ends with the
    same highlight-visible invariant as programmatic rebuilds.  Never
    raises.
    """
    try:
        if option_list is None:
            return
        try:
            count = int(option_list.option_count)  # type: ignore[union-attr]
        except Exception:
            return
        if count <= 0:
            return
        try:
            highlighted = option_list.highlighted
        except Exception:
            return
        if highlighted is None:
            return
        _reveal_option_list_highlight(option_list)
    except Exception:
        pass


def set_option_list_highlight(option_list: Any, index: Any) -> None:
    """Highlight one ``OptionList`` row and keep it scrolled into view.

    The list's own ``highlighted``/scroll state is the single source of
    truth: the scrollbar thumb is derived from it, so every highlight
    change (keyboard, mouse, page keys, filter rebuilds) must flow
    through one place that both assigns the index and explicitly reveals
    it.  Relying only on the reactive watcher is not enough: a
    programmatic assignment during mount/resize/filter rebuilds can run
    against a stale viewport, leaving the highlight off-screen while the
    thumb shows the old position.

    The reveal is two-phase: an immediate ``scroll_to_highlight()``
    plus a canonical ``call_after_refresh`` retry once layout has
    refreshed the scroll extent.  No sleeps, no hardcoded offsets.

    ``index`` is clamped to the live option range; ``None`` clears the
    highlight (empty/disabled-only lists).  Never raises: pickers must
    stay usable in any terminal state.
    """
    try:
        count = int(option_list.option_count)
    except Exception:
        return
    if count <= 0:
        return
    if index is None:
        try:
            option_list.highlighted = None
        except Exception:
            pass
        try:
            option_list.refresh(layout=True)
        except Exception:
            pass
        try:
            option_list.scroll_to(y=0, animate=False)
        except Exception:
            pass
        try:
            schedule = getattr(option_list, "call_after_refresh", None)
            if callable(schedule):
                try:
                    schedule(option_list.scroll_to, y=0, animate=False)
                except Exception:
                    pass
        except Exception:
            pass
        return
    try:
        target = int(index)
    except (TypeError, ValueError):
        target = 0
    target = max(0, min(target, count - 1))
    try:
        option_list.highlighted = target
    except Exception:
        return
    _reveal_option_list_highlight(option_list)
