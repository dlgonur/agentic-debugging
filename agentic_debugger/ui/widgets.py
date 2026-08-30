"""Domain presentation panes for the Agentic Debugger workspace.

Every pane renders exclusively from the immutable
:class:`~agentic_debugger.application.presentation.SessionViewState` produced
by the shared pure reducer.  Widgets never import PDB, PatchManager,
controller, or verifier implementation internals, and never execute domain
logic: missing recorded data is rendered as an explicit ``NOT RECORDED`` /
empty state, never fabricated.

Panes keep their own scroll/focus state (UI-owned); all presentation facts
come from the view.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pygments import lex
from pygments.lexers import ClassNotFound, get_lexer_for_filename
from pygments.token import Comment, Keyword, Name, Number, Operator, String, Token
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from agentic_debugger.application.case_brief import (
    EvidenceStageState,
    project_case_brief,
)
from agentic_debugger.application.events import (
    OperatorStage,
    SessionEventKind,
    SessionStatus,
    SessionTerminationReason,
    SourceSnapshotStage,
    SourceKind,
)
from agentic_debugger.application.presentation import (
    DebuggerViewState,
    PatchStage,
    SessionViewState,
    TimelineEntry,
    VerifierSummaryView,
    current_source,
)
from agentic_debugger.application.live_execution import LiveExecutionState, OperationKind
from agentic_debugger.application.workstream import (
    ChangePreview,
    ChangePreviewLimits,
    DiffLineKind,
    WorkstreamEntry,
    WorkstreamStatus,
    build_change_preview,
)
from agentic_debugger.ui.theme import (
    CODE_FUNCTION,
    CODE_STRING,
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

_NOT_RECORDED = "NOT RECORDED"

#: Bounded change preview limits for the detailed Patch pane (larger than
#: the compact live workstream preview, still never a full dump).
PATCH_PANE_PREVIEW_LIMITS = ChangePreviewLimits(
    max_files=8, max_hunks=4, max_lines=40, max_line_chars=110
)


def _markup_escape(value: Any) -> str:
    return str(value).replace("[", "\\[").replace("]", "\\]")


def _operator_stage_label(stage: Any) -> str:
    return str(stage.value if hasattr(stage, "value") else stage).replace("_", " ").capitalize()


class EvidenceState(str, Enum):
    """Presentation availability of domain evidence in the active view."""

    AVAILABLE = "available"
    LIVE_PENDING = "live_pending"
    REPLAY_PENDING = "replay_pending"
    SESSION_ABSENT = "session_absent"


_ACTIVITY_FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "all events"),
    ("lifecycle", "lifecycle (session/cleanup)"),
    ("controller", "controller"),
    ("model", "model requests"),
    ("tools", "tools"),
    ("debugger", "debugger"),
    ("patch", "patch/source"),
    ("verifier", "verifier"),
)

_ACTIVITY_FILTER_KINDS: dict[str, frozenset[str]] = {
    "all": frozenset(),
    "lifecycle": frozenset(
        {
            SessionEventKind.SESSION_CREATED.value,
            SessionEventKind.SESSION_STARTED.value,
            SessionEventKind.SESSION_STATUS_CHANGED.value,
            SessionEventKind.SESSION_CANCEL_REQUESTED.value,
            SessionEventKind.SESSION_COMPLETED.value,
            SessionEventKind.SESSION_FAILED.value,
            SessionEventKind.SESSION_CANCELLED.value,
            SessionEventKind.CLEANUP_STARTED.value,
            SessionEventKind.CLEANUP_COMPLETED.value,
            SessionEventKind.ARTIFACT_WRITTEN.value,
        }
    ),
    "controller": frozenset(
        {
            SessionEventKind.CONTROLLER_STEP.value,
            SessionEventKind.CONTROLLER_TRANSITION.value,
        }
    ),
    "model": frozenset(
        {
            SessionEventKind.MODEL_REQUEST_STARTED.value,
            SessionEventKind.MODEL_REQUEST_COMPLETED.value,
            SessionEventKind.MODEL_DIRECTIVE_ACCEPTED.value,
            SessionEventKind.MODEL_DIRECTIVE_REJECTED.value,
            SessionEventKind.MODEL_CONFIGURED.value,
            SessionEventKind.OPERATOR_PROGRESS.value,
        }
    ),
    "tools": frozenset(
        {
            SessionEventKind.TOOL_STARTED.value,
            SessionEventKind.TOOL_COMPLETED.value,
        }
    ),
    "debugger": frozenset(
        {
            SessionEventKind.DEBUGGER_STARTED.value,
            SessionEventKind.DEBUGGER_LOCATION_CHANGED.value,
            SessionEventKind.DEBUGGER_STACK_OBSERVED.value,
            SessionEventKind.DEBUGGER_LOCALS_OBSERVED.value,
        }
    ),
    "patch": frozenset(
        {
            SessionEventKind.PATCH_PROPOSED.value,
            SessionEventKind.PATCH_REJECTED.value,
            SessionEventKind.PATCH_APPLY_FAILED.value,
            SessionEventKind.PATCH_APPLIED.value,
            SessionEventKind.PATCH_REVERTED.value,
            SessionEventKind.SOURCE_SNAPSHOT.value,
            SessionEventKind.DIAGNOSIS_RECORDED.value,
        }
    ),
    "verifier": frozenset(
        {
            SessionEventKind.VERIFIER_STARTED.value,
            SessionEventKind.VERIFIER_STAGE_STARTED.value,
            SessionEventKind.VERIFIER_STAGE_COMPLETED.value,
            SessionEventKind.VERIFIER_COMPLETED.value,
        }
    ),
}

_KIND_STYLE: dict[str, str] = {
    SessionEventKind.SESSION_CREATED.value: FAINT,
    SessionEventKind.SESSION_STARTED.value: f"bold {SUCCESS}",
    SessionEventKind.SESSION_STATUS_CHANGED.value: "bold",
    SessionEventKind.SESSION_CANCEL_REQUESTED.value: f"bold {WARNING}",
    SessionEventKind.SESSION_COMPLETED.value: f"bold {SUCCESS}",
    SessionEventKind.SESSION_FAILED.value: f"bold {ERROR}",
    SessionEventKind.SESSION_CANCELLED.value: f"bold {WARNING}",
    SessionEventKind.CLEANUP_STARTED.value: FAINT,
    SessionEventKind.CLEANUP_COMPLETED.value: SUCCESS,
    SessionEventKind.ARTIFACT_WRITTEN.value: TOOL,
    SessionEventKind.CONTROLLER_STEP.value: PRIMARY,
    SessionEventKind.CONTROLLER_TRANSITION.value: f"bold {PRIMARY}",
    SessionEventKind.MODEL_REQUEST_STARTED.value: SECONDARY,
    SessionEventKind.MODEL_REQUEST_COMPLETED.value: SECONDARY,
    SessionEventKind.MODEL_DIRECTIVE_ACCEPTED.value: f"bold {SECONDARY}",
    SessionEventKind.MODEL_DIRECTIVE_REJECTED.value: WARNING,
    SessionEventKind.MODEL_CONFIGURED.value: f"bold {SECONDARY}",
    SessionEventKind.OPERATOR_PROGRESS.value: f"bold {PRIMARY}",
    SessionEventKind.TOOL_STARTED.value: TOOL,
    SessionEventKind.TOOL_COMPLETED.value: TOOL,
    SessionEventKind.DEBUGGER_STARTED.value: DEBUGGER,
    SessionEventKind.DEBUGGER_LOCATION_CHANGED.value: DEBUGGER,
    SessionEventKind.DEBUGGER_STACK_OBSERVED.value: DEBUGGER,
    SessionEventKind.DEBUGGER_LOCALS_OBSERVED.value: DEBUGGER,
    SessionEventKind.PATCH_PROPOSED.value: EVIDENCE,
    SessionEventKind.PATCH_REJECTED.value: WARNING,
    SessionEventKind.PATCH_APPLY_FAILED.value: f"bold {ERROR}",
    SessionEventKind.PATCH_APPLIED.value: EVIDENCE,
    SessionEventKind.PATCH_REVERTED.value: EVIDENCE,
    SessionEventKind.SOURCE_SNAPSHOT.value: TOOL,
    SessionEventKind.DIAGNOSIS_RECORDED.value: PRIMARY,
    SessionEventKind.VERIFIER_STARTED.value: SUCCESS,
    SessionEventKind.VERIFIER_STAGE_STARTED.value: f"dim {SUCCESS}",
    SessionEventKind.VERIFIER_STAGE_COMPLETED.value: SUCCESS,
    SessionEventKind.VERIFIER_COMPLETED.value: f"bold {SUCCESS}",
}


def _entry_style(entry: TimelineEntry) -> str:
    """Return styling for a timeline entry using kind and status information."""
    if entry.event_kind == SessionEventKind.TOOL_COMPLETED:
        if "(error" in entry.summary or "(failed" in entry.summary:
            return f"bold {ERROR}"
        if "(rejected" in entry.summary:
            return WARNING
        return TOOL
    return _KIND_STYLE.get(entry.event_kind.value, "default")


def _append_section(text: Text, title: str) -> None:
    """Append one section heading as plain text with its own style.

    Recorded/derived text is always appended as plain ``rich.text.Text``;
    styling is supplied separately and markup is never embedded in a
    preconstructed ``Text`` object.
    """
    text.append("\n")
    text.append(title, style=f"bold {PRIMARY}")
    text.append("\n")


def _append_kv(text: Text, key: str, value: str) -> None:
    """Append one ``key: value`` line; both parts stay plain text."""
    text.append(f"{key}: ", style="dim")
    text.append(f"{value}\n")


def _stage_style(stage: PatchStage) -> str:
    return {
        PatchStage.PROPOSED: EVIDENCE,
        PatchStage.REJECTED: WARNING,
        PatchStage.APPLY_FAILED: ERROR,
        PatchStage.APPLIED: EVIDENCE,
        PatchStage.REVERTED: EVIDENCE,
        PatchStage.VERIFIED: SUCCESS,
    }[stage]


def _source_token_style(token_type: Token) -> Optional[str]:
    """Map common Pygments categories to the restrained application palette."""
    if token_type in Comment:
        return f"dim italic {MUTED}"
    if token_type in Keyword:
        return f"bold {ERROR}"
    if token_type in Name.Function:
        return CODE_FUNCTION
    if token_type in Name.Class:
        return f"bold {EVIDENCE}"
    if token_type in String:
        return CODE_STRING
    if token_type in Number:
        return PRIMARY
    if token_type in Operator:
        return ERROR
    if token_type in Token.Punctuation:
        return FOREGROUND
    return None


def _highlight_source_lines(source_text: str, source_path: str) -> list[Text]:
    """Return one styled Rich ``Text`` per source line.

    Lexer selection is filename-based and deliberately fails closed to plain
    text for extensions Pygments does not recognize.  The input text is only
    split into styled spans; it is never parsed as Rich markup.
    """
    lines = [Text(line, no_wrap=True) for line in (source_text.splitlines() or [""])]
    try:
        filename = Path(source_path).name or source_path
        lexer = get_lexer_for_filename(filename, code=source_text)
    except (ClassNotFound, OSError, TypeError, ValueError):
        return lines

    lines = [Text("", no_wrap=True) for _ in lines]
    line_index = 0
    for token_type, value in lex(source_text, lexer):
        chunks = value.split("\n")
        for chunk_index, chunk in enumerate(chunks):
            if line_index < len(lines) and chunk:
                style = _source_token_style(token_type)
                if style is None:
                    lines[line_index].append(chunk)
                else:
                    lines[line_index].append(chunk, style=style)
            if chunk_index < len(chunks) - 1:
                line_index += 1
    return lines


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


def _counts(passed: Optional[int], total: Optional[int]) -> str:
    if passed is None and total is None:
        return "not recorded"
    return f"{passed}/{total}"


class ActivityPanel(VerticalScroll):
    """Filterable activity timeline (controller/model/tool/debugger/…)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None
        self.filter: str = "all"

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(self, view: SessionViewState) -> None:
        self._view = view
        self._text.update(self._render_view(view))

    def cycle_filter(self, step: int = 1) -> str:
        names = [name for name, _ in _ACTIVITY_FILTERS]
        current = names.index(self.filter) if self.filter in names else 0
        self.filter = names[(current + step) % len(names)]
        if self._view is not None:
            self._text.update(self._render_view(self._view))
        return self.filter

    def _render_view(self, view: SessionViewState) -> Text:
        text = Text()
        text.append("Filter: ", style="dim")
        text.append(self.filter, style="bold")
        text.append(
            f" ({dict(_ACTIVITY_FILTERS)[self.filter]})  —  keys: 1..7 "
            "filter (1 = all)\n",
            style="dim",
        )
        text.append("─" * 40, style="dim")
        text.append("\n")
        allowed = _ACTIVITY_FILTER_KINDS[self.filter]
        entries = [
            entry
            for entry in view.timeline
            if not allowed or entry.event_kind.value in allowed
        ]
        if not entries:
            text.append("No activity recorded for this filter.", style="dim")
            return text
        for entry in reversed(entries):
            style = _entry_style(entry)
            text.append(f"#{entry.sequence:<5} ", style="dim")
            text.append(entry.summary, style=style)
            text.append("\n")
        return text

    def export_text(self, view: Optional[SessionViewState] = None) -> str:
        """Full logical Activity export for clipboard (filter-aware)."""
        target = view if view is not None else self._view
        if target is None:
            return "No activity recorded."
        return activity_export_text(target, filter_name=self.filter)


_START_TO_COMPLETION_KINDS: dict[SessionEventKind, SessionEventKind] = {
    SessionEventKind.MODEL_REQUEST_STARTED: SessionEventKind.MODEL_REQUEST_COMPLETED,
    SessionEventKind.TOOL_STARTED: SessionEventKind.TOOL_COMPLETED,
    SessionEventKind.VERIFIER_STAGE_STARTED: SessionEventKind.VERIFIER_STAGE_COMPLETED,
    SessionEventKind.VERIFIER_STARTED: SessionEventKind.VERIFIER_COMPLETED,
    SessionEventKind.CLEANUP_STARTED: SessionEventKind.CLEANUP_COMPLETED,
}


def _find_in_flight_sequences(view: SessionViewState) -> set[int]:
    """Find sequence numbers of operations that have started but not yet completed."""
    if view.status is not SessionStatus.RUNNING:
        return set()
    active_starts: dict[tuple[SessionEventKind, Optional[str]], list[int]] = {}
    for entry in view.timeline:
        if entry.event_kind in _START_TO_COMPLETION_KINDS:
            comp_kind = _START_TO_COMPLETION_KINDS[entry.event_kind]
            key = (comp_kind, entry.operation_key)
            active_starts.setdefault(key, []).append(entry.sequence)
        elif entry.event_kind in (
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            SessionEventKind.TOOL_COMPLETED,
            SessionEventKind.VERIFIER_STAGE_COMPLETED,
            SessionEventKind.VERIFIER_COMPLETED,
            SessionEventKind.CLEANUP_COMPLETED,
        ):
            key = (entry.event_kind, entry.operation_key)
            if key in active_starts and active_starts[key]:
                active_starts[key].pop(0)
    in_flight: set[int] = set()
    for seqs in active_starts.values():
        in_flight.update(seqs)
    return in_flight


class TimelinePanel(VerticalScroll):
    """Concise event timeline with effective phase-boundary markers.

    ``phase_boundary_sequences`` is derived by the screen from the recorded
    event stream (the same ``phase_boundaries`` derivation the replay cursor
    uses); the pane itself only renders it.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None
        self._boundaries: frozenset[int] = frozenset()

    def compose(self) -> ComposeResult:
        yield self._text

    def update_view(
        self,
        view: SessionViewState,
        phase_boundary_sequences: Optional[frozenset[int]] = None,
    ) -> None:
        self._view = view
        if phase_boundary_sequences is not None:
            self._boundaries = phase_boundary_sequences
        self._text.update(self._render_view(view))

    def _render_view(self, view: SessionViewState) -> Text:
        text = Text()
        if not view.timeline:
            text.append("No events recorded.", style="dim")
            return text
        start_dt = None
        for entry in view.timeline:
            if entry.timestamp_utc:
                try:
                    start_dt = datetime.fromisoformat(entry.timestamp_utc.replace("Z", "+00:00"))
                    break
                except Exception:
                    pass
        in_flight_seqs = _find_in_flight_sequences(view)
        for i, entry in enumerate(view.timeline):
            marker = "» " if entry.sequence in self._boundaries else "  "
            style = _entry_style(entry)
            time_str = ""
            if entry.timestamp_utc and start_dt:
                try:
                    entry_dt = datetime.fromisoformat(entry.timestamp_utc.replace("Z", "+00:00"))
                    secs = max(0, int((entry_dt - start_dt).total_seconds()))
                    time_str = f"{secs // 60:02d}:{secs % 60:02d} "
                except Exception:
                    pass
            text.append(marker, style=f"bold {PRIMARY}" if marker == "» " else "dim")
            if time_str:
                text.append(time_str, style=f"bold {EVIDENCE}")
            text.append(f"#{entry.sequence:<4} ", style="dim")
            text.append(entry.summary, style=style)
            if entry.duration_seconds is not None:
                if entry.duration_seconds < 60:
                    text.append(f"  ({entry.duration_seconds:.1f}s)", style="dim")
                else:
                    mins = int(entry.duration_seconds // 60)
                    secs = int(entry.duration_seconds % 60)
                    text.append(f"  ({mins}m {secs}s)", style="dim")
            elif entry.sequence in in_flight_seqs:
                text.append("  (running…)", style="dim")
            text.append("\n")
        return text

    def export_text(self, view: Optional[SessionViewState] = None) -> str:
        """Full logical Timeline export for clipboard."""
        target = view if view is not None else self._view
        if target is None:
            return "No events recorded."
        return timeline_export_text(target, phase_boundary_sequences=self._boundaries)


def _local_project_identity(view: SessionViewState) -> tuple[str, str]:
    """(repo basename, short HEAD) from the durable diagnosis record.

    The Local Project session records both facts in the ``diagnosis.recorded``
    observed_values at session start; the reducer copies them verbatim.  When
    the record is absent the honest placeholder is shown — never an inferred
    value from task ids or aliases.
    """
    observed = view.diagnosis.observed_values if view.diagnosis is not None else None
    repo = "—"
    head = "—"
    if isinstance(observed, dict):
        candidate_repo = observed.get("repo_basename")
        candidate_head = observed.get("source_head")
        if isinstance(candidate_repo, str) and candidate_repo:
            repo = candidate_repo
        if isinstance(candidate_head, str) and candidate_head:
            head = candidate_head
    return repo, head


def _official_tests_label(view: SessionViewState) -> Optional[str]:
    """Official-verifier milestone label; only proven execution is 'Executed'."""
    if view.source_kind is not SourceKind.LEVEL32_OPERATOR:
        return None
    if view.official_execution_proven is True:
        return "Executed"
    if view.operator_stage is OperatorStage.OFFICIAL_EVALUATOR_COMPLETED:
        return "Completed (unproven)"
    if view.operator_stage is OperatorStage.OFFICIAL_EVALUATOR_STARTED:
        return "Evaluator launched"
    if view.operator_stage is OperatorStage.OFFICIAL_VERIFICATION_PREPARING:
        return "Preparing"
    if view.status.terminal:
        return "Not executed"
    return "Not started"


class StatusHeader(Static):
    """One compact status header line derived from the presentation view.

    The header is a widget boundary: it renders the same ``SessionViewState``
    as every pane and never touches domain state directly.
    """


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
        self._text.update("\n".join(lines))


_STATUS_MARKER: dict[WorkstreamStatus, tuple[str, str]] = {
    WorkstreamStatus.ACTIVE: ("→", f"bold {PRIMARY}"),
    WorkstreamStatus.COMPLETED: ("✓", SUCCESS),
    WorkstreamStatus.FAILED: ("×", ERROR),
    WorkstreamStatus.WAITING: ("~", WARNING),
}

_KIND_LABEL_STYLE = {
    "change": f"bold {EVIDENCE}",
    "official_verification": f"bold {SECONDARY}",
    "verification": SECONDARY,
    "error": f"bold {ERROR}",
}


def _kind_style(entry: WorkstreamEntry) -> str:
    return _KIND_LABEL_STYLE.get(entry.kind.value, FOREGROUND)


def _change_stats_text(change: ChangePreview) -> str:
    if change.multi_file:
        return f"{len(change.files) + change.omitted_files} files · +{change.additions} -{change.deletions}"
    return f"+{change.additions} -{change.deletions}"


def _append_diff_lines(text: "Text", change: ChangePreview, *, indent: str) -> None:
    """Append the bounded, terminal-native diff body of the primary file."""
    for line in change.lines:
        pad = " " * len(indent)
        if line.kind is DiffLineKind.HUNK:
            text.append(f"{pad}{line.text}\n", style=PRIMARY)
            continue
        number = line.old_lineno if line.old_lineno is not None else line.new_lineno
        prefix = {
            DiffLineKind.CONTEXT: " ",
            DiffLineKind.ADDED: "+",
            DiffLineKind.REMOVED: "-",
        }[line.kind]
        style = {
            DiffLineKind.CONTEXT: MUTED,
            DiffLineKind.ADDED: SUCCESS,
            DiffLineKind.REMOVED: ERROR,
        }[line.kind]
        text.append(f"{pad}{number:>4} │{prefix}", style=style)
        text.append(f"{line.text}\n", style=style)
    if change.truncated or change.omitted_files:
        parts = []
        if change.omitted_files:
            parts.append(f"+{change.omitted_files} more file{'s' if change.omitted_files > 1 else ''}")
        if change.omitted_lines:
            parts.append(f"+{change.omitted_lines} more changed line{'s' if change.omitted_lines > 1 else ''}")
        if parts:
            text.append(f"{indent}… {' · '.join(parts)}\n", style="dim")


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


def activity_export_text(
    view: SessionViewState,
    *,
    filter_name: str = "all",
    task_title: Optional[str] = None,
) -> str:
    """Deterministic text export for the Activity ledger.

    Uses the same filter/order semantics as the current tab and the same
    safe presentation summaries.  The result is the complete logical
    Activity contents, not the visible viewport.  Pure; never touches
    clipboard, journal, or ephemeral liveness.
    """
    from agentic_debugger.ui.app import task_display_title

    title = task_title if task_title is not None else task_display_title(view.task_id)
    status_label = (
        "Completed"
        if view.status is SessionStatus.SUCCEEDED
        else view.status.value.replace("_", " ").capitalize()
    )
    # Treatment label: reuse the same derivation as the context panel.
    try:
        from agentic_debugger.application.level32 import is_ladder_task, ladder_task_metadata
        if view.source_kind is SourceKind.LEVEL32_OPERATOR and view.model_provenance and view.model_provenance.treatment_revision is not None:
            treatment = f"V{view.model_provenance.treatment_revision}"
        elif is_ladder_task(view.task_id):
            treatment = ladder_task_metadata(view.task_id).treatment
        else:
            treatment = "—"
    except Exception:
        treatment = "—"
    allowed = _ACTIVITY_FILTER_KINDS.get(filter_name, frozenset())
    entries = [
        entry
        for entry in view.timeline
        if not allowed or entry.event_kind.value in allowed
    ]
    lines = [
        title,
        f"Status: {status_label}",
        f"Treatment: {treatment}",
        f"View: Activity",
        f"Filter: {filter_name}",
        "",
    ]
    # Activity renders newest first (reversed), so export preserves that.
    for entry in reversed(entries):
        lines.append(f"#{entry.sequence} {entry.summary}")
    if not entries:
        lines.append("No activity recorded for this filter.")
    return "\n".join(lines)


def timeline_export_text(
    view: SessionViewState,
    *,
    task_title: Optional[str] = None,
    phase_boundary_sequences: Optional[frozenset[int]] = None,
) -> str:
    """Deterministic text export for the full Timeline.

    Full logical timeline in its displayed (chronological) ordering with
    effective phase-boundary markers preserved as "»".  Pure.
    """
    from agentic_debugger.ui.app import task_display_title

    title = task_title if task_title is not None else task_display_title(view.task_id)
    status_label = (
        "Completed"
        if view.status is SessionStatus.SUCCEEDED
        else view.status.value.replace("_", " ").capitalize()
    )
    try:
        from agentic_debugger.application.level32 import is_ladder_task, ladder_task_metadata
        if view.source_kind is SourceKind.LEVEL32_OPERATOR and view.model_provenance and view.model_provenance.treatment_revision is not None:
            treatment = f"V{view.model_provenance.treatment_revision}"
        elif is_ladder_task(view.task_id):
            treatment = ladder_task_metadata(view.task_id).treatment
        else:
            treatment = "—"
    except Exception:
        treatment = "—"
    lines = [
        title,
        f"Status: {status_label}",
        f"Treatment: {treatment}",
        f"View: Timeline",
        "",
    ]
    if not view.timeline:
        lines.append("No events recorded.")
        return "\n".join(lines)
    boundaries = phase_boundary_sequences or frozenset()
    in_flight_seqs = _find_in_flight_sequences(view)
    for entry in view.timeline:
        marker = "» " if entry.sequence in boundaries else ""
        dur = ""
        if entry.duration_seconds is not None:
            if entry.duration_seconds < 60:
                dur = f"  ({entry.duration_seconds:.1f}s)"
            else:
                mins = int(entry.duration_seconds // 60)
                secs = int(entry.duration_seconds % 60)
                dur = f"  ({mins}m {secs}s)"
        elif entry.sequence in in_flight_seqs:
            dur = "  (running…)"
        lines.append(f"{marker}#{entry.sequence} {entry.summary}{dur}")
    return "\n".join(lines)


__all__ = [
    "ActivityPanel",
    "DebuggerPanel",
    "EvidenceState",
    "LiveBar",
    "LiveRunContextPanel",
    "PATCH_PANE_PREVIEW_LIMITS",
    "PatchPanel",
    "ReplayBar",
    "SourcePanel",
    "StatusHeader",
    "TimelinePanel",
    "VerifierPanel",
    "WorkstreamPanel",
    "activity_export_text",
    "render_workstream",
    "timeline_export_text",
]
