"""Live context, workstream/live-trace rendering, and their panels.

This module owns the live-run context panel, the workstream and live
trace text rendering (semantic unit badges, targets, details, diff
previews), and the live/workstream/replay bar widgets.  Rendering is
callback/data driven over presentation state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from agentic_debugger.application.events import (
    SessionStatus,
    SourceKind,
)
from agentic_debugger.application.live_execution import LiveExecutionState, OperationKind
from agentic_debugger.application.model_providers import format_model_display_name
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    SessionTokenUsage,
    SessionViewState,
    TimelineEntry,
)
from agentic_debugger.application.workstream import (
    ChangePreview,
    WorkstreamEntry,
    WorkstreamKind,
    WorkstreamStatus,
)
from agentic_debugger.ui.exports import live_export_text
from agentic_debugger.ui.panels import (
    format_token_count_compact,
    session_tokens_breakdown,
    session_tokens_summary,
)
from agentic_debugger.ui.render_helpers import (
    _append_diff_lines,
    _change_stats_text,
    _kind_badge,
    _kind_style,
    _markup_escape,
    _operator_stage_label,
)
from agentic_debugger.ui.timing import (
    _local_project_identity,
    _official_tests_label,
)
from agentic_debugger.ui.theme import (
    DEBUGGER,
    ERROR,
    EVIDENCE,
    FAINT,
    FOREGROUND,
    LINE,
    MUTED,
    PRIMARY,
    SECONDARY,
    SUCCESS,
    TOOL,
    WARNING,
)

class LiveRunContextPanel(VerticalScroll):
    """Truthful runtime context for wide capability-ladder workspaces."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text = Static("")

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(self, view: SessionViewState, *, elapsed: str = "—") -> None:
        terminal = view.status.terminal
        if view.pdb_observed:
            pdb = "Observed"
        elif view.debugger.session_started:
            pdb = "Active"
        elif terminal:
            pdb = "Not reached"
        else:
            pdb = "Pending"
        if view.verifier_summary is not None:
            outcome = view.verifier_summary.outcome.value if view.verifier_summary.outcome else (view.verifier_summary.status or "Completed")
            verifier = f"Completed ({outcome})"
        elif view.verifier_stages:
            verifier = "Not completed" if terminal else "Active"
        elif terminal:
            verifier = "Not started"
        else:
            verifier = "Pending"
        if view.patch_attempts:
            patch_status = f"Attempt {len(view.patch_attempts)} ({view.patch_attempts[-1].stage.value})"
        else:
            patch_status = "No candidate yet"

        target = view.current_tool_target or (
            f"{view.debugger.script}:{view.debugger.line}"
            if view.debugger.script and view.debugger.line is not None
            else view.debugger.script
        ) or "—"
        step = (
            str(view.latest_controller_step_index + 1)
            if view.latest_controller_step_index is not None
            else "—"
        )

        model = view.model_provenance
        model_name = model.display_name if model and model.display_name else (model.profile_id if model and model.profile_id else "—")

        tokens_summary = session_tokens_summary(view.token_usage)

        def row(label: str, value: str) -> str:
            return f"[{MUTED}]{label:<10}[/] [{FOREGROUND}]{_markup_escape(value)}[/]"

        if view.source_kind is SourceKind.LOCAL_PROJECT:
            repo_basename, head_short = _local_project_identity(view)
            lines = [
                f"[bold {PRIMARY}]RUN CONTEXT[/]",
                row("MODEL", model_name),
                row("TARGET", target),
                row("STEP", step),
                row("PDB", pdb),
                row("PATCH", patch_status),
                row("VERIFIER", verifier),
                row("PROJECT", repo_basename),
                row("HEAD", head_short),
            ]
            if tokens_summary is not None:
                lines.insert(2, row("TOKENS", tokens_summary.replace("Tokens ", "")))
                lines.insert(3, row("USAGE", session_tokens_breakdown(view.token_usage)))
            self._text.update("\n".join(lines))
            return

        lines = [
            f"[bold {PRIMARY}]RUN CONTEXT[/]",
            row("MODEL", model_name),
            row("TARGET", target),
            row("STEP", step),
            row("PDB", pdb),
            row("PATCH", patch_status),
            row("VERIFIER", verifier),
        ]
        if tokens_summary is not None:
            lines.insert(2, row("TOKENS", tokens_summary.replace("Tokens ", "")))
            lines.insert(3, row("USAGE", session_tokens_breakdown(view.token_usage)))
        self._text.update("\n".join(lines))

    def update_execution(self, state: LiveExecutionState) -> None:
        """Render operational facts before static provenance on wide screens."""
        view = state.view

        def counter(value, maximum):
            if value is None:
                return "—"
            return f"{value} / {maximum}" if maximum is not None else str(value)

        if view.pdb_observed:
            pdb = "Observed"
        elif view.debugger.session_started:
            pdb = "Active"
        elif view.status.terminal:
            pdb = "Not reached"
        else:
            pdb = "Waiting"

        if view.verifier_summary is not None:
            outcome = view.verifier_summary.outcome.value if view.verifier_summary.outcome else (view.verifier_summary.status or "Completed")
            verifier = f"Completed ({outcome})"
        elif view.verifier_stages:
            verifier = "Active"
        elif view.status.terminal:
            verifier = "Not started"
        else:
            verifier = "Pending"

        if view.patch_attempts:
            patch_status = f"Attempt {len(view.patch_attempts)} ({view.patch_attempts[-1].stage.value})"
        elif state.candidate_attempt_ordinal:
            patch_status = f"Attempt {state.candidate_attempt_ordinal}"
        else:
            patch_status = "No candidate yet"

        model = view.model_provenance
        model_name = model.display_name if model and model.display_name else (model.profile_id if model and model.profile_id else "—")

        def row(label: str, value: str) -> str:
            return f"[{MUTED}]{label:<10}[/] [{FOREGROUND}]{_markup_escape(value)}[/]"

        target = state.current_target or view.current_tool_target or "—"
        step = counter(state.controller_step_ordinal, state.ceilings.controller_steps)

        if view.source_kind is SourceKind.LOCAL_PROJECT:
            repo_basename, head_short = _local_project_identity(view)
            lines = [
                f"[bold {PRIMARY}]RUN CONTEXT[/]",
                row("MODEL", model_name),
                row("TARGET", target),
                row("STEP", step),
                row("PDB", pdb),
                row("PATCH", patch_status),
                row("VERIFIER", verifier),
                row("PROJECT", repo_basename),
                row("HEAD", head_short),
            ]
            if session_tokens_summary(view.token_usage) is not None:
                lines.insert(2, row("TOKENS", session_tokens_summary(view.token_usage).replace("Tokens ", "")))
                lines.insert(3, row("USAGE", session_tokens_breakdown(view.token_usage)))
            self._text.update("\n".join(lines))
            return

        lines = [
            f"[bold {PRIMARY}]RUN CONTEXT[/]",
            row("MODEL", model_name),
            row("TARGET", target),
            row("STEP", step),
            row("PDB", pdb),
            row("PATCH", patch_status),
            row("VERIFIER", verifier),
        ]
        if session_tokens_summary(view.token_usage) is not None:
            lines.insert(2, row("TOKENS", session_tokens_summary(view.token_usage).replace("Tokens ", "")))
            lines.insert(3, row("USAGE", session_tokens_breakdown(view.token_usage)))
        self._text.update("\n".join(lines))


_STATUS_MARKER: dict[WorkstreamStatus, tuple[str, str]] = {
    WorkstreamStatus.ACTIVE: ("→", f"bold {PRIMARY}"),
    WorkstreamStatus.COMPLETED: ("✓", SUCCESS),
    WorkstreamStatus.FAILED: ("×", ERROR),
    WorkstreamStatus.WAITING: ("~", WARNING),
}

_KIND_LABEL_STYLE = {
    "session": f"bold {PRIMARY}",
    "workspace": f"bold {PRIMARY}",
    "model_request": f"bold {SECONDARY}",
    "source_read": f"bold {TOOL}",
    "tool": f"bold {TOOL}",
    "debugger": f"bold {DEBUGGER}",
    "pdb": f"bold {DEBUGGER}",
    "diagnosis": f"bold {PRIMARY}",
    "change": f"bold {EVIDENCE}",
    "verification": f"bold {SECONDARY}",
    "official_verification": f"bold {SECONDARY}",
    "cleanup": f"bold {SUCCESS}",
    "error": f"bold {ERROR}",
}


def _append_entry(
    text: "Text",
    entry: WorkstreamEntry,
    *,
    with_change_body: bool,
    narrow: bool,
) -> None:
    marker, marker_style = _STATUS_MARKER[entry.status]
    text.append(f"{marker} ", style=marker_style)
    text.append(entry.label.upper() if not narrow else entry.label, style=_kind_style(entry))
    if entry.ordinal is not None:
        text.append(f" {entry.ordinal}", style=FOREGROUND)
    target = entry.target
    if not target and entry.change is not None and entry.change.primary_path:
        # A rejected candidate has no authoritative changed-file list; the
        # preview's primary path is the honest fallback.
        target = entry.change.primary_path
    if target:
        text.append(f"  {target}", style=FOREGROUND)
    if entry.change is not None:
        text.append(f"  {_change_stats_text(entry.change)}", style=f"bold {SUCCESS}")
    detail = entry.detail
    if entry.change is not None and detail and detail.startswith("+") and detail.endswith("more"):
        # The preview's file summary already states what was changed.
        detail = None
    if detail:
        text.append(f"  · {detail}", style=MUTED)
    text.append("\n")
    if with_change_body and entry.change is not None and not narrow:
        change = entry.change
        if change.multi_file:
            for file_summary in change.files:
                text.append(
                    f"    {file_summary.operation.value} {file_summary.path}"
                    f"  +{file_summary.additions} -{file_summary.deletions}\n",
                    style=MUTED,
                )
            if change.omitted_files:
                text.append(f"    … +{change.omitted_files} more\n", style="dim")
        if change.primary_path and change.multi_file:
            text.append(f"    {change.primary_path}\n", style=FOREGROUND)
        _append_diff_lines(text, change, indent="  ")


def render_workstream(
    state: LiveExecutionState,
    *,
    expanded: bool,
    narrow: bool,
    height: int = 40,
    suppress_change_body: bool = False,
) -> "Text":
    """Render the curated operational workstream (pure; no widget state).

    ``expanded`` is used when the selected evidence pane has no substantive
    content yet: the workstream becomes the primary body.  ``narrow``
    degrades to single-line entries without diff bodies.  Row budgets are
    bounded by the terminal height.  ``suppress_change_body`` keeps the
    diff out of the stream when the selected pane already owns diff detail
    (the Patch pane), avoiding a duplicated block.
    """
    entries = state.view.workstream
    text = Text()
    prefix = "LIVE" if state.mode.value == "live" else "RECENT"
    header = state.operation_label
    if (
        state.operation in (OperationKind.MODEL_REQUEST, OperationKind.WAITING_FOR_MODEL)
        and state.request_ordinal is not None
    ):
        ceiling = state.ceilings.model_requests
        header = (
            f"{header} / {ceiling}" if ceiling is not None else header
        )
    text.append(f"{prefix} · ", style=f"bold {MUTED}")
    text.append(f"{header}\n", style=f"bold {FOREGROUND}")
    text.append("─" * 40 + "\n", style=LINE)
    if not entries:
        text.append("Waiting for operational activity…\n", style="dim")
        return text
    if expanded:
        rows = 16 if height >= 30 else 8
    else:
        rows = 5 if height >= 30 else 3
    visible = entries[-rows:]
    # The diff body appears for the most recent change unit only: rhythm
    # over repetition, and a hard bound on rendered diff lines.
    last_change_index = -1
    if not suppress_change_body:
        for index in range(len(visible) - 1, -1, -1):
            if visible[index].change is not None:
                last_change_index = index
                break
    for index, entry in enumerate(visible):
        _append_entry(
            text,
            entry,
            with_change_body=index == last_change_index,
            narrow=narrow,
        )
    hidden = len(entries) - len(visible)
    if hidden > 0:
        text.append(f"… {hidden} earlier operation{'s' if hidden > 1 else ''}\n", style="dim")
    return text


def render_live_trace(
    view: SessionViewState,
    execution_state: Optional[LiveExecutionState] = None,
    *,
    narrow: bool = False,
) -> Text:
    """Render the chronological operational trace for the authoritative Live pane."""
    text = Text()
    entries = view.workstream
    if not entries:
        text.append("Waiting for operational activity…\n", style="dim")
        return text

    start_dt = None
    for entry in view.timeline:
        if entry.timestamp_utc:
            try:
                start_dt = datetime.fromisoformat(entry.timestamp_utc.replace("Z", "+00:00"))
                break
            except Exception:
                pass

    if execution_state is not None:
        is_live = (execution_state.mode.value == "live")
    else:
        is_live = (view.status == SessionStatus.RUNNING)
    status_label = "LIVE" if is_live else "RECENT"
    header_title = execution_state.operation_label if execution_state else (view.status.value.replace("_", " ").capitalize())
    text.append(f"{status_label} · ", style=f"bold {MUTED}")
    text.append(f"{header_title}\n", style=f"bold {FOREGROUND}")
    text.append("─" * 72 + "\n\n", style=LINE)

    last_change_index = -1
    for index in range(len(entries) - 1, -1, -1):
        if entries[index].change is not None:
            last_change_index = index
            break

    for index, entry in enumerate(entries):
        time_str = "       "
        if entry.timestamp_utc and start_dt:
            try:
                entry_dt = datetime.fromisoformat(entry.timestamp_utc.replace("Z", "+00:00"))
                secs = max(0, int((entry_dt - start_dt).total_seconds()))
                time_str = f"{secs // 60:02d}:{secs % 60:02d}  "
            except Exception:
                pass
        text.append(time_str, style=FAINT)

        marker, marker_style = _STATUS_MARKER[entry.status]
        text.append(f"{marker} ", style=marker_style)

        badge = _kind_badge(entry)
        text.append(f"{badge:<14} ", style=_kind_style(entry))

        label_text = entry.label
        text.append(label_text, style=FOREGROUND)
        if entry.target:
            text.append(f"  {entry.target}", style=f"bold {FOREGROUND}")

        if entry.change is not None:
            text.append(f"  {_change_stats_text(entry.change)}", style=f"bold {SUCCESS}")

        detail = entry.detail
        if entry.change is not None and detail and detail.startswith("+") and detail.endswith("more"):
            detail = None
        if detail:
            detail_style = WARNING if "rejection" in detail or "mismatch" in detail or "failed" in detail else MUTED
            text.append(f"  · {detail}", style=detail_style)

        if entry.duration_seconds is not None:
            if entry.duration_seconds < 60:
                text.append(f"  ({entry.duration_seconds:.1f}s)", style="dim")
            else:
                mins = int(entry.duration_seconds // 60)
                secs = int(entry.duration_seconds % 60)
                text.append(f"  ({mins}m {secs}s)", style="dim")
        elif entry.status is WorkstreamStatus.ACTIVE:
            text.append("  (running…)", style="dim italic")

        text.append("\n")

        if entry.change is not None and not narrow and index == last_change_index:
            change = entry.change
            if change.multi_file:
                for file_summary in change.files:
                    text.append(
                        f"                {file_summary.operation.value} {file_summary.path}"
                        f"  +{file_summary.additions} -{file_summary.deletions}\n",
                        style=MUTED,
                    )
                if change.omitted_files:
                    text.append(f"                … +{change.omitted_files} more\n", style="dim")
            if change.primary_path and change.multi_file:
                text.append(f"                {change.primary_path}\n", style=FOREGROUND)
            _append_diff_lines(text, change, indent="                ")
            text.append("\n")

    return text


class LivePanel(VerticalScroll):
    """Authoritative Live execution trace console."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None
        self._follow_tail: bool = True

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(
        self,
        view: SessionViewState,
        execution_state: Optional[LiveExecutionState] = None,
        *,
        narrow: bool = False,
    ) -> None:
        self._view = view
        self._text.update(
            render_live_trace(view, execution_state=execution_state, narrow=narrow)
        )
        if self._follow_tail:
            self.scroll_end(animate=False)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        if new_value < max(0, self.max_scroll_y - 2):
            self._follow_tail = False
        else:
            self._follow_tail = True

    def on_scroll(self) -> None:
        if self.scroll_y < max(0, self.max_scroll_y - 2):
            self._follow_tail = False
        else:
            self._follow_tail = True

    def export_text(self, view: Optional[SessionViewState] = None) -> str:
        """Full logical Live execution trace export for clipboard."""
        target = view if view is not None else self._view
        if target is None:
            return "No operational activity recorded."
        return live_export_text(target)


class WorkstreamPanel(Vertical):
    """Non-focusable curated operational workstream below an evidence pane.

    Observational only: it renders the same immutable ``SessionViewState``
    workstream every other pane renders and never takes keyboard focus.
    """

    can_focus = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")

    def compose(self) -> ComposeResult:
        yield self._text

    def update_workstream(
        self,
        state: LiveExecutionState,
        *,
        expanded: bool,
        narrow: bool,
        height: int = 40,
        suppress_change_body: bool = False,
    ) -> None:
        self._text.update(
            render_workstream(
                state,
                expanded=expanded,
                narrow=narrow,
                height=height,
                suppress_change_body=suppress_change_body,
            )
        )


class ReplayBar(Static):
    """Replay-control footer (position + key hints)."""


class LiveBar(Static):
    """Live-session footer (operational status + cancel hint)."""
