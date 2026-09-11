"""Session evidence panels: source, debugger, patch, verifier, status.

This module owns the Textual session panels — evidence review, source
view, debugger view, patch attempts, verifier summary, and the status
header with token summaries — rendering bounded presentation state
callback/data-driven (never authoritative application state).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from agentic_debugger.application.case_brief import (
    CaseBrief,
    EvidenceStageState,
    project_case_brief,
)
from agentic_debugger.application.events import SessionStatus, SourceSnapshotStage
from agentic_debugger.application.presentation import (
    DebuggerViewState,
    DiagnosisView,
    PatchAttemptView,
    PatchStage,
    SessionTokenUsage,
    SessionViewState,
    SourceView,
    VerifierSummaryView,
    active_candidate_attempt,
    current_source,
)
from agentic_debugger.application.workstream import ChangePreview, build_change_preview
from agentic_debugger.ui.render_helpers import (
    PATCH_PANE_PREVIEW_LIMITS,
    EvidenceState,
    _NOT_RECORDED,
    _append_diff_lines,
    _append_kv,
    _append_section,
    _counts,
    _highlight_source_lines,
    _markup_escape,
    _stage_style,
)
from agentic_debugger.ui.theme import (
    DEBUGGER,
    ERROR,
    EVIDENCE,
    EVIDENCE_SURFACE,
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

class EvidenceReviewPanel(VerticalScroll):
    """One-screen causal review of the session's recorded evidence prefix."""

    _STATE_LABELS = {
        EvidenceStageState.PROVEN: "PROVEN",
        EvidenceStageState.RECORDED: "RECORDED",
        EvidenceStageState.FAILED: "FAILED",
        EvidenceStageState.PENDING: "PENDING",
        EvidenceStageState.NOT_RECORDED: "NOT RECORDED",
        EvidenceStageState.NOT_REQUIRED: "NOT REQUIRED",
    }
    _STATE_STYLES = {
        EvidenceStageState.PROVEN: f"bold {SUCCESS}",
        EvidenceStageState.RECORDED: f"bold {PRIMARY}",
        EvidenceStageState.FAILED: f"bold {ERROR}",
        EvidenceStageState.PENDING: f"bold {WARNING}",
        EvidenceStageState.NOT_RECORDED: FAINT,
        EvidenceStageState.NOT_REQUIRED: MUTED,
    }

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text = Static("")

    def compose(self) -> ComposeResult:
        yield self._text

    @classmethod
    def _render_view(cls, view: SessionViewState) -> Text:
        brief = project_case_brief(view)
        text = Text()
        text.append("Evidence Review", style=f"bold {PRIMARY}")
        text.append("  /  causal case brief\n", style=MUTED)
        text.append("VERDICT  ", style=f"bold {MUTED}")
        verdict_style = (
            f"bold {SUCCESS}"
            if brief.verdict == "RESOLVED"
            else f"bold {ERROR}"
            if brief.verdict_authoritative
            else f"bold {WARNING}"
        )
        text.append(brief.verdict, style=verdict_style)
        if brief.verdict_authoritative:
            text.append("  AUTHORITATIVE\n", style=f"bold {EVIDENCE}")
        else:
            text.append("  (Awaiting independent verification)\n", style=MUTED)
        text.append("─" * 72 + "\n", style=LINE)
        for index, stage in enumerate(brief.stages):
            label = cls._STATE_LABELS[stage.state]
            text.append(f"{label:<13}", style=cls._STATE_STYLES[stage.state])
            text.append(f"{stage.kind.value.upper():<11}", style=f"bold {FOREGROUND}")
            text.append(stage.title, style=FOREGROUND)
            text.append("\n")
            if stage.detail and stage.detail not in ("Pending", "Not recorded"):
                text.append(" " * 13)
                text.append(stage.detail, style=MUTED)
                text.append("\n")
            if stage.references:
                references = ", ".join(stage.references[:4])
                if len(stage.references) > 4:
                    references += f", +{len(stage.references) - 4} more"
                text.append(" " * 13)
                text.append(f"evidence: {references}\n", style=f"dim {PRIMARY}")
            if index < len(brief.stages) - 1:
                text.append("\n")
        return text

    def update_view(self, view: SessionViewState) -> None:
        self._text.update(self._render_view(view))


class SourcePanel(VerticalScroll):
    """Recorded source with the current execution line highlighted."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None
        self._evidence_state: EvidenceState = EvidenceState.AVAILABLE

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(
        self,
        view: SessionViewState,
        evidence_state: EvidenceState = EvidenceState.AVAILABLE,
    ) -> None:
        self._view = view
        self._evidence_state = evidence_state
        self._text.update(
            self._render_view(view, evidence_state=evidence_state)
        )

    @staticmethod
    def _render_view(
        view: SessionViewState,
        evidence_state: EvidenceState = EvidenceState.AVAILABLE,
    ) -> Text:
        source = current_source(view)
        debugger = view.debugger
        if source is None:
            if view.sources:
                text = Text(
                    "The debugger's current file has no recorded source snapshot "
                    "(source for the current execution location is not recorded).\n\n",
                    style=WARNING,
                )
                text.append("NOT RECORDED", style="bold dim")
                return text
            if evidence_state == EvidenceState.LIVE_PENDING:
                text = Text("Source evidence not available yet.\n")
                text.append("Waiting for source evidence...\n", style="dim")
                return text
            if evidence_state == EvidenceState.REPLAY_PENDING:
                text = Text(
                    "Source snapshot not yet available at this replay position.\n"
                )
                text.append(
                    "Advance the replay to the first source event.\n",
                    style="dim",
                )
                return text
            text = Text("No source snapshot was recorded for this session.\n\n")
            text.append("NOT RECORDED", style="bold dim")
            return text
        text = Text()
        stage_label = {
            SourceSnapshotStage.INITIAL: "initial workspace",
            SourceSnapshotStage.APPLIED: "patched workspace",
            SourceSnapshotStage.REVERTED: "reverted workspace",
        }.get(source.stage, source.stage.value)
        text.append(
            f"{source.path}  ·  {stage_label}  ·  sha256 "
            f"{source.sha256[:12]}…"
        )
        if source.truncated:
            text.append("  ·  [truncated]", style=WARNING)
        text.append(f"  ·  {source.line_count} lines", style=FAINT)
        text.append("\n")
        text.append("─" * 40, style=LINE)
        text.append("\n")
        highlight_line = (
            debugger.line
            if debugger.script == source.path and debugger.line is not None
            else None
        )
        lines = _highlight_source_lines(source.text, source.path)
        highlighted = highlight_line is not None and 1 <= highlight_line <= len(lines)
        gutter_width = max(3, len(str(len(lines))))
        for index, line in enumerate(lines, start=1):
            gutter = Text(f"{index:>{gutter_width}} ", style=FAINT)
            if index == highlight_line:
                gutter.stylize(f"bold {EVIDENCE}")
            text.append_text(gutter)
            if index == highlight_line:
                # Keep the syntax foreground colors and add only the current
                # line emphasis/background on top of them.
                line.stylize(f"bold on {EVIDENCE_SURFACE}")
                text.append("▶ ", style=f"bold {EVIDENCE}")
            else:
                text.append("  ")
            text.append_text(line)
            text.append("\n")
        if not highlighted and debugger.script is not None:
            text.append(
                f"\n(no execution-line marker: debugger is at "
                f"{debugger.script}:{debugger.line or '?'})",
                style="dim",
            )
        return text


class DebuggerPanel(VerticalScroll):
    """Recorded debugger state: location, breakpoints, stack, locals."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None
        self._evidence_state: EvidenceState = EvidenceState.AVAILABLE

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(
        self,
        view: SessionViewState,
        evidence_state: EvidenceState = EvidenceState.AVAILABLE,
    ) -> None:
        self._view = view
        self._evidence_state = evidence_state
        self._text.update(
            self._render_view(view, evidence_state=evidence_state)
        )

    @staticmethod
    def _render_view(
        view: SessionViewState,
        evidence_state: EvidenceState = EvidenceState.AVAILABLE,
    ) -> Text:
        debugger: DebuggerViewState = view.debugger
        text = Text()
        if not debugger.session_started:
            if evidence_state == EvidenceState.LIVE_PENDING:
                text.append("Debugger evidence not available yet.\n")
                text.append("Waiting for debugger activity...\n", style="dim")
                return text
            if evidence_state == EvidenceState.REPLAY_PENDING:
                text.append(
                    "Debugger evidence not yet available at this replay position.\n"
                )
                text.append(
                    "Advance the replay to the first debugger event.\n",
                    style="dim",
                )
                return text
            text.append("No debugger evidence was recorded for this session.\n\n")
            text.append(_NOT_RECORDED, style="dim italic")
            return text
        _append_section(text, "Current location")
        _append_kv(text, "script", debugger.script if debugger.script else "—")
        _append_kv(text, "line", str(debugger.line) if debugger.line is not None else "—")
        _append_kv(
            text,
            "function",
            debugger.function if debugger.function else "—",
        )
        _append_kv(
            text,
            "pause generation",
            str(debugger.pause_generation)
            if debugger.pause_generation is not None
            else "not recorded",
        )
        _append_section(text, "Breakpoints")
        if debugger.breakpoints:
            for item in debugger.breakpoints:
                text.append(f"  • {item}\n")
        else:
            text.append("  none recorded\n")
        _append_section(text, "Stack")
        if debugger.frames:
            for frame in debugger.frames:
                marker = "▶ " if frame.is_current else "  "
                line = (
                    f"{marker}#{frame.index} {frame.function} — "
                    f"{frame.file}:{frame.line}"
                )
                text.append(line, style="bold" if frame.is_current else "default")
                text.append("\n")
        else:
            text.append("  no stack recorded\n")
        _append_section(text, "Locals (current recorded frame)")
        if debugger.locals:
            for local in debugger.locals:
                if "redacted" in local.summary:
                    text.append(f"  {local.name} = ", style=WARNING)
                    text.append(local.summary, style=f"bold {ERROR}")
                    text.append("\n")
                else:
                    text.append(f"  {local.name} = {local.summary}\n")
        else:
            text.append("  no locals recorded\n")
        if debugger.pause_generation is None:
            text.append(
                "\npause generation: not recorded (stale guard "
                "applies in stream order)\n",
                style="dim",
            )
        return text


class PatchPanel(VerticalScroll):
    """Recorded patch-attempt lifecycle (never a correctness verdict)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None
        self._evidence_state: EvidenceState = EvidenceState.AVAILABLE

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(
        self,
        view: SessionViewState,
        evidence_state: EvidenceState = EvidenceState.AVAILABLE,
    ) -> None:
        self._view = view
        self._evidence_state = evidence_state
        self._text.update(
            self._render_view(view, evidence_state=evidence_state)
        )

    @staticmethod
    def _render_view(
        view: SessionViewState,
        evidence_state: EvidenceState = EvidenceState.AVAILABLE,
    ) -> Text:
        text = Text()
        if not view.patch_attempts:
            if evidence_state == EvidenceState.LIVE_PENDING:
                if view.current_tool_name == "apply_patch":
                    text.append("Candidate attempt 1\n", style=f"bold {EVIDENCE}")
                    text.append("Applying change…\n", style="dim")
                else:
                    text.append("No patch attempt yet.\n")
                    text.append("Waiting for patch generation...\n", style="dim")
                return text
            if evidence_state == EvidenceState.REPLAY_PENDING:
                text.append(
                    "Patch attempts not yet available at this replay position.\n"
                )
                text.append(
                    "Advance the replay to the first patch event.\n",
                    style="dim",
                )
                return text
            text.append("No patch attempts were recorded for this session.\n\n")
            text.append(_NOT_RECORDED, style="dim italic")
            return text
        for attempt in view.patch_attempts:
            stage = attempt.stage
            # Displayed ordinals are one-based; the durable attempt_index
            # inside the view stays zero-based.
            text.append(
                f"Attempt {attempt.attempt_index + 1} — {stage.value.upper()}",
                style=_stage_style(stage),
            )
            text.append("\n")
            if attempt.patch_sha256:
                text.append(f"  sha256: {attempt.patch_sha256[:16]}…\n", style="dim")
            if attempt.changed_files:
                text.append(
                    f"  changed files: {', '.join(attempt.changed_files)}\n"
                )
            if attempt.syntax_passed is not None:
                text.append(f"  syntax passed: {attempt.syntax_passed}\n")
            if attempt.rejection_reason:
                text.append(f"  rejection: {attempt.rejection_reason}\n", style=WARNING)
            if attempt.apply_failure_reason:
                text.append(
                    f"  apply failure: {attempt.apply_failure_reason}\n",
                    style=ERROR,
                )
            preview = (
                build_change_preview(attempt.patch_text, PATCH_PANE_PREVIEW_LIMITS)
                if attempt.patch_text
                else None
            )
            if preview is not None:
                text.append(
                    f"\n  CHANGED FILES · +{preview.additions} -{preview.deletions}"
                    f" across {len(preview.files) + preview.omitted_files}"
                    f" file{'s' if len(preview.files) + preview.omitted_files > 1 else ''}\n",
                    style=f"bold {PRIMARY}",
                )
                for file_summary in preview.files:
                    text.append(
                        f"  {file_summary.operation.value} {file_summary.path}"
                        f"  +{file_summary.additions}"
                        f" -{file_summary.deletions}\n",
                        style=FOREGROUND,
                    )
                if preview.omitted_files:
                    text.append(
                        f"  … +{preview.omitted_files} more file{'s' if preview.omitted_files > 1 else ''}\n",
                        style="dim",
                    )
                text.append("\n  DIFF", style=f"bold {PRIMARY}")
                if preview.primary_path:
                    text.append(f" · {preview.primary_path}", style=FOREGROUND)
                text.append("\n")
                _append_diff_lines(text, preview, indent="  ")
                text.append("\n")
            elif attempt.patch_text:
                text.append(
                    "\n  (diff body withheld: patch content did not parse as a bounded unified diff)\n",
                    style=WARNING,
                )
        text.append(
            "\nPatch application only mutates the recorded workspace. "
            "APPLIED does not mean FIXED — correctness is decided by the "
            "independent verifier only.\n",
            style="dim",
        )
        return text


class VerifierPanel(VerticalScroll):
    """Verifier progress and final result (the correctness authority)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None
        self._evidence_state: EvidenceState = EvidenceState.AVAILABLE

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(
        self,
        view: SessionViewState,
        evidence_state: EvidenceState = EvidenceState.AVAILABLE,
    ) -> None:
        self._view = view
        self._evidence_state = evidence_state
        self._text.update(
            self._render_view(view, evidence_state=evidence_state)
        )

    @staticmethod
    def _render_view(
        view: SessionViewState,
        evidence_state: EvidenceState = EvidenceState.AVAILABLE,
    ) -> Text:
        text = Text()
        if view.verifier_stages:
            _append_section(text, "Stages (progress only)")
            for stage in view.verifier_stages:
                style = {
                    "running": PRIMARY,
                    "completed": SUCCESS,
                    "failed": ERROR,
                    "skipped": FAINT,
                    "cancelled": WARNING,
                }.get(stage.status.value, "default")
                text.append(
                    f"  {stage.stage.value:<32} {stage.status.value}", style=style
                )
                text.append("\n")
        summary: Optional[VerifierSummaryView] = view.verifier_summary
        if summary is None:
            if view.verifier_stages:
                text.append(
                    "\nVerifier is in progress or was interrupted.\n",
                    style=WARNING,
                )
            elif evidence_state == EvidenceState.LIVE_PENDING:
                text.append(
                    "\nVerifier has not started yet.\n"
                )
                text.append(
                    "Waiting for independent verification...\n",
                    style="dim",
                )
            elif evidence_state == EvidenceState.REPLAY_PENDING:
                text.append(
                    "\nVerifier evidence not yet available at this replay position.\n"
                )
                text.append(
                    "Advance the replay to the verifier events.\n",
                    style="dim",
                )
            elif view.status == SessionStatus.CANCELLED:
                text.append(
                    "\nIndependent verification was not run: the session was cancelled before reaching the verification stage.\n\n"
                )
                text.append(_NOT_RECORDED, style="dim italic")
            else:
                text.append("\nNo verifier result was recorded for this session.\n\n")
                text.append(_NOT_RECORDED, style="dim italic")
            return text
        _append_section(text, "Final verifier result (authoritative)")
        _append_kv(text, "status", summary.status if summary.status else "—")
        _append_kv(
            text,
            "outcome",
            summary.outcome.value if summary.outcome else "—",
        )
        _append_kv(text, "fail-to-pass", _counts(summary.f2p_passed, summary.f2p_total))
        _append_kv(text, "pass-to-pass", _counts(summary.p2p_passed, summary.p2p_total))
        if summary.official_test_execution_proven is not None:
            _append_kv(
                text,
                "official tests executed",
                "Yes" if summary.official_test_execution_proven else "No",
            )
        if summary.workspace_cleaned is not None:
            _append_kv(text, "workspace cleaned", str(summary.workspace_cleaned))
        if summary.classification:
            _append_kv(text, "Level-32 classification", summary.classification)
        return text


class StatusHeader(Static):
    """One compact status header line derived from the presentation view.

    The header is a widget boundary: it renders the same ``SessionViewState``
    as every pane and never touches domain state directly.
    """


def format_token_count_compact(value: Optional[int]) -> str:
    """Compact token count for summaries (``31842`` -> ``31.8k``).

    Unknown (``None``) renders as the explicit unavailable marker ``—``,
    never as zero.
    """
    if value is None:
        return "—"
    if value >= 1_000_000:
        scaled, suffix = value / 1_000_000, "M"
    elif value >= 1_000:
        scaled, suffix = value / 1_000, "k"
    else:
        return f"{value:,}"
    text = f"{scaled:.1f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}"


def session_tokens_summary(usage: SessionTokenUsage) -> Optional[str]:
    """Header-level cumulative summary (``Tokens 31.8k``).

    Returns ``None`` when no completed request reported usable usage, so
    sessions without coverage show no fake total.  A partial-coverage
    session (some completed request reported no usage) is visibly
    marked instead of presenting a complete total.
    """
    if not usage.usage_available:
        return None
    total = usage.effective_total_tokens
    if total is None:
        return None
    summary = f"Tokens {format_token_count_compact(total)}"
    if not usage.total_complete:
        summary += " (partial)"
    return summary


def session_tokens_breakdown(usage: SessionTokenUsage) -> str:
    """Dimension breakdown (known dimensions only; unknown as ``—``)."""
    if not usage.usage_available:
        return "—"
    parts = []
    if usage.input_tokens is not None:
        suffix = "+" if usage.is_partial("input_tokens") else ""
        parts.append(f"In {format_token_count_compact(usage.input_tokens)}{suffix}")
    if usage.cached_input_tokens is not None:
        suffix = "+" if usage.is_partial("cached_input_tokens") else ""
        parts.append(f"Cache {format_token_count_compact(usage.cached_input_tokens)}{suffix}")
    if usage.output_tokens is not None:
        suffix = "+" if usage.is_partial("output_tokens") else ""
        parts.append(f"Out {format_token_count_compact(usage.output_tokens)}{suffix}")
    if not parts:
        return "—"
    return " · ".join(parts)
