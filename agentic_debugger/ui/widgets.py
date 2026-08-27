"""Domain presentation panes of the Local Application V1 workspace.

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
    SessionEventKind.SESSION_CREATED.value: "dim",
    SessionEventKind.SESSION_STARTED.value: "bold green",
    SessionEventKind.SESSION_STATUS_CHANGED.value: "bold",
    SessionEventKind.SESSION_CANCEL_REQUESTED.value: "bold yellow",
    SessionEventKind.SESSION_COMPLETED.value: "bold green",
    SessionEventKind.SESSION_FAILED.value: "bold red",
    SessionEventKind.SESSION_CANCELLED.value: "bold yellow",
    SessionEventKind.CLEANUP_STARTED.value: "dim",
    SessionEventKind.CLEANUP_COMPLETED.value: "dim green",
    SessionEventKind.ARTIFACT_WRITTEN.value: "dim cyan",
    SessionEventKind.CONTROLLER_STEP.value: "#8fb7d9",
    SessionEventKind.CONTROLLER_TRANSITION.value: "bold #8fb7d9",
    SessionEventKind.MODEL_REQUEST_STARTED.value: "#a371f7",
    SessionEventKind.MODEL_REQUEST_COMPLETED.value: "#a371f7",
    SessionEventKind.MODEL_DIRECTIVE_ACCEPTED.value: "bold #a371f7",
    SessionEventKind.MODEL_DIRECTIVE_REJECTED.value: "yellow",
    SessionEventKind.MODEL_CONFIGURED.value: "bold #a371f7",
    SessionEventKind.OPERATOR_PROGRESS.value: "bold #79c0ff",
    SessionEventKind.TOOL_STARTED.value: "dark_cyan",
    SessionEventKind.TOOL_COMPLETED.value: "dark_cyan",
    SessionEventKind.DEBUGGER_STARTED.value: "magenta",
    SessionEventKind.DEBUGGER_LOCATION_CHANGED.value: "magenta",
    SessionEventKind.DEBUGGER_STACK_OBSERVED.value: "magenta",
    SessionEventKind.DEBUGGER_LOCALS_OBSERVED.value: "magenta",
    SessionEventKind.PATCH_PROPOSED.value: "yellow",
    SessionEventKind.PATCH_REJECTED.value: "yellow",
    SessionEventKind.PATCH_APPLY_FAILED.value: "bold red",
    SessionEventKind.PATCH_APPLIED.value: "orange1",
    SessionEventKind.PATCH_REVERTED.value: "orange1",
    SessionEventKind.SOURCE_SNAPSHOT.value: "dim cyan",
    SessionEventKind.DIAGNOSIS_RECORDED.value: "cyan",
    SessionEventKind.VERIFIER_STARTED.value: "blue",
    SessionEventKind.VERIFIER_STAGE_STARTED.value: "dim blue",
    SessionEventKind.VERIFIER_STAGE_COMPLETED.value: "green",
    SessionEventKind.VERIFIER_COMPLETED.value: "bold green",
}


def _entry_style(entry: TimelineEntry) -> str:
    """Return styling for a timeline entry using kind and status information."""
    if entry.event_kind == SessionEventKind.TOOL_COMPLETED:
        if "(error" in entry.summary or "(failed" in entry.summary:
            return "bold red"
        if "(rejected" in entry.summary:
            return "yellow"
        return "dark_cyan"
    return _KIND_STYLE.get(entry.event_kind.value, "default")


def _append_section(text: Text, title: str) -> None:
    """Append one section heading as plain text with its own style.

    Recorded/derived text is always appended as plain ``rich.text.Text``;
    styling is supplied separately and markup is never embedded in a
    preconstructed ``Text`` object.
    """
    text.append("\n")
    text.append(title, style="bold #8fb7d9")
    text.append("\n")


def _append_kv(text: Text, key: str, value: str) -> None:
    """Append one ``key: value`` line; both parts stay plain text."""
    text.append(f"{key}: ", style="dim")
    text.append(f"{value}\n")


def _stage_style(stage: PatchStage) -> str:
    return {
        PatchStage.PROPOSED: "yellow",
        PatchStage.REJECTED: "red",
        PatchStage.APPLY_FAILED: "red",
        PatchStage.APPLIED: "orange1",
        PatchStage.REVERTED: "orange1",
        PatchStage.VERIFIED: "green",
    }[stage]


def _source_token_style(token_type: Token) -> Optional[str]:
    """Map common Pygments categories to the restrained application palette."""
    if token_type in Comment:
        return "dim italic #8b949e"
    if token_type in Keyword:
        return "bold #ff7b72"
    if token_type in Name.Function:
        return "#d2a8ff"
    if token_type in Name.Class:
        return "bold #ffa657"
    if token_type in String:
        return "#a5d6ff"
    if token_type in Number:
        return "#79c0ff"
    if token_type in Operator:
        return "#ff7b72"
    if token_type in Token.Punctuation:
        return "#c9d1d9"
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
                    style="yellow",
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
            text.append("  ·  [truncated]", style="yellow")
        text.append(f"  ·  {source.line_count} lines", style="dim")
        text.append("\n")
        text.append("─" * 40, style="dim")
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
            gutter = Text(f"{index:>{gutter_width}} ", style="dim")
            if index == highlight_line:
                gutter.stylize("bold yellow")
            text.append_text(gutter)
            if index == highlight_line:
                # Keep the syntax foreground colors and add only the current
                # line emphasis/background on top of them.
                line.stylize("bold on #3a2f00")
                text.append("▶ ", style="bold yellow")
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
                    text.append(f"  {local.name} = ", style="yellow")
                    text.append(local.summary, style="bold red")
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
                    text.append("Candidate attempt 1\n", style="bold #ffa657")
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
                text.append(f"  rejection: {attempt.rejection_reason}\n", style="yellow")
            if attempt.apply_failure_reason:
                text.append(
                    f"  apply failure: {attempt.apply_failure_reason}\n",
                    style="red",
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
                    style="bold #8fb7d9",
                )
                for file_summary in preview.files:
                    text.append(
                        f"  {file_summary.operation.value} {file_summary.path}"
                        f"  +{file_summary.additions}"
                        f" -{file_summary.deletions}\n",
                        style="#c9d1d9",
                    )
                if preview.omitted_files:
                    text.append(
                        f"  … +{preview.omitted_files} more file{'s' if preview.omitted_files > 1 else ''}\n",
                        style="dim",
                    )
                text.append("\n  DIFF", style="bold #8fb7d9")
                if preview.primary_path:
                    text.append(f" · {preview.primary_path}", style="#c9d1d9")
                text.append("\n")
                _append_diff_lines(text, preview, indent="  ")
                text.append("\n")
            elif attempt.patch_text:
                text.append(
                    "\n  (diff body withheld: patch content did not parse as a bounded unified diff)\n",
                    style="yellow",
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
                    "running": "blue",
                    "completed": "green",
                    "failed": "red",
                    "skipped": "dim",
                    "cancelled": "yellow",
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
                    style="yellow",
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
        text.append(
            "\nThe verifier result is the correctness authority. "
            "Application completion is operational only.\n",
            style="dim",
        )
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
        for entry in view.timeline:
            marker = "» " if entry.sequence in self._boundaries else "  "
            style = _entry_style(entry)
            text.append(f"{marker}#{entry.sequence:<5} ", style="dim")
            text.append(entry.summary, style=style)
            text.append("\n")
        return text

    def export_text(self, view: Optional[SessionViewState] = None) -> str:
        """Full logical Timeline export for clipboard."""
        target = view if view is not None else self._view
        if target is None:
            return "No events recorded."
        return timeline_export_text(target, phase_boundary_sequences=self._boundaries)


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
        from agentic_debugger.ui.app import task_display_title
        from agentic_debugger.application.level32 import is_ladder_task, ladder_task_metadata

        model = view.model_provenance
        terminal = view.status.terminal
        if view.status is SessionStatus.SUCCEEDED:
            stage = "Completed"
        elif view.status is SessionStatus.CANCELLED:
            stage = "Cancelled"
        elif terminal and view.termination_reason is SessionTerminationReason.MODEL_ERROR:
            stage = "Model error"
        elif terminal and view.termination_reason is SessionTerminationReason.DIRECTIVE_EXHAUSTED:
            stage = "Controller budget exhausted"
        elif terminal and view.termination_reason is SessionTerminationReason.CONTROLLER_FAILED:
            stage = "Controller failed"
        elif terminal:
            stage = view.status.value.replace("_", " ").title()
        else:
            stage = (
                "Finalizing"
                if view.operator_stage is OperatorStage.COMPLETED
                else _operator_stage_label(view.operator_stage)
                if view.operator_stage
                else "Not recorded"
            )
        if view.debugger.session_started:
            pdb = "Observed"
        elif terminal:
            pdb = "Not reached"
        elif view.operator_stage is OperatorStage.DEBUGGER:
            pdb = "Active"
        else:
            pdb = "Pending"
        # Durable verifier truth for the compact context panel.
        # Presence of verifier_summary (VERIFIER_COMPLETED) is the authority;
        # outcome (RESOLVED vs UNRESOLVED) never changes whether it completed.
        if view.verifier_summary is not None:
            verifier = "Completed"
        elif view.verifier_stages:
            verifier = "Not completed" if terminal else "Active"
        elif view.operator_stage in (
            OperatorStage.VERIFICATION,
            OperatorStage.OFFICIAL_VERIFICATION,
            OperatorStage.OFFICIAL_VERIFICATION_PREPARING,
            OperatorStage.OFFICIAL_EVALUATOR_STARTED,
            OperatorStage.OFFICIAL_EVALUATOR_COMPLETED,
        ):
            verifier = "Not completed" if terminal else "Active"
        elif terminal:
            verifier = "Not run"
        else:
            verifier = "Pending"
        # Local Project Debug has distinct sidebar facts; hide ladder treatment
        if view.source_kind is SourceKind.LOCAL_PROJECT:
            runtime = "Local Project Debug"
            # Extract repo basename / HEAD from diagnosis observed_values if present
            repo_basename = "—"
            head_short = "—"
            try:
                diag = view.diagnosis
                if diag is not None and getattr(diag, "text", None):
                    # fallback to task_id basename
                    pass
                # presentation diagnosis doesn't expose observed_values, but we stored via diagnosis payload?
                # Try to read from verifier summary classification or from timeline? For now use task_id
                # Use view.task_id's repo info embedded in diagnosis text observed_values via raw: we stored in diagnosis.
                # Since Presentation DiagnosisView doesn't have observed_values, we fallback to showing task_id
                pass
            except Exception:
                pass
            # Try to derive from view.timeline diagnosis recorded observed values? Not available in view; use simple
            # Display the repo basename as task_id unless diagnosis provides better
            # For local project, task_id is local-project-debug; we show it as Project
            # and attempt to show head from model provenance alias if it looks like SHA
            if view.model_provenance is not None and view.model_provenance.profile_id and len(view.model_provenance.profile_id) == 12:
                head_short = view.model_provenance.profile_id
            lines = [
                "[bold #79c0ff]RUN[/]",
                "[bright_white]Local Project Debug[/]",
                "[#8b949e]Stage[/]", "[bright_white]" + _markup_escape(stage) + "[/]",
                "[#8b949e]Elapsed[/]", "[bright_white]" + _markup_escape(elapsed) + "[/]",
                "[#8b949e]PDB[/]", "[bright_white]" + pdb + "[/]",
                "[#8b949e]Verifier[/]", "[bright_white]" + _markup_escape(verifier) + "[/]",
                "[#8b949e]Project[/]", "[bright_white]" + _markup_escape(repo_basename) + "[/]",
                "[#8b949e]Source HEAD[/]", "[bright_white]" + _markup_escape(head_short) + "[/]",
            ]
            self._text.update("\n".join(lines))
            return
        ladder = is_ladder_task(view.task_id)
        metadata = ladder_task_metadata(view.task_id) if ladder else None
        if view.source_kind is SourceKind.LEVEL32_OPERATOR:
            treatment = (
                f"V{model.treatment_revision}"
                if model is not None and model.treatment_revision is not None
                else metadata.treatment if metadata is not None else "Not recorded"
            )
        elif metadata is not None:
            treatment = metadata.treatment
        else:
            treatment = "Not recorded"
        evaluation = metadata.evaluation if metadata is not None else None
        runtime = {
            SourceKind.OFFLINE_DEMO: "Local deterministic",
            SourceKind.CONFIGURED_MODEL: "Configured command",
            SourceKind.OLLAMA_CLOUD_LADDER: "Ollama Cloud",
            SourceKind.LEVEL32_OPERATOR: "Ollama Cloud",
        }.get(view.source_kind, "Recorded source")
        official = _official_tests_label(view)
        lines = [
            "[bold #79c0ff]RUN[/]",
            "[#8b949e]Task[/]", "[bright_white]" + _markup_escape(task_display_title(view.task_id)) + "[/]",
            "[#8b949e]ID[/]", "[bright_white]" + _markup_escape(view.task_id) + "[/]",
            "[#8b949e]Model[/]", "[bright_white]" + _markup_escape(model.display_name if model and model.display_name else "Not recorded") + "[/]",
            "[#8b949e]Alias[/]", "[bright_white]" + _markup_escape(model.profile_id if model and model.profile_id else "Not recorded") + "[/]",
            "[#8b949e]Runtime[/]", "[bright_white]" + runtime + "[/]",
            *(["[#8b949e]Evaluation[/]", "[bright_white]" + evaluation + "[/]"] if evaluation else []),
            "[#8b949e]Treatment[/]", "[bright_white]" + treatment + "[/]",
            "[#8b949e]Stage[/]", "[bright_white]" + _markup_escape(stage) + "[/]",
            "[#8b949e]Elapsed[/]", "[bright_white]" + _markup_escape(elapsed) + "[/]",
            "[#8b949e]PDB[/]", "[bright_white]" + pdb + "[/]",
            "[#8b949e]Verifier[/]", "[bright_white]" + _markup_escape(verifier) + "[/]",
        ]
        if official is not None:
            lines.extend(("[#8b949e]Official tests[/]", "[bright_white]" + official + "[/]"))
        self._text.update("\n".join(lines))

    def update_execution(self, state: LiveExecutionState) -> None:
        """Render operational facts before static provenance on wide screens.

        Every field is one physical row (label + value) so the complete
        runtime context -- including provenance and the official-verifier
        milestone -- stays above the fold at practical workspace sizes.
        """
        from agentic_debugger.ui.app import task_display_title
        from agentic_debugger.application.level32 import is_ladder_task, ladder_task_metadata
        view = state.view
        # Local Project Debug: hide ladder treatment/evaluation
        if view.source_kind is SourceKind.LOCAL_PROJECT:
            # Early return for Local Project execution context (distinct facts)
            def counter(value, maximum):
                if value is None:
                    return "Not recorded"
                return f"{value} / {maximum}" if maximum is not None else str(value)
            def duration(value):
                return "—" if value is None else f"{value:.1f}s"
            if view.pdb_observed:
                pdb = "Observed"
            elif view.debugger.session_started:
                pdb = "Active / awaiting evidence"
            elif view.status.terminal:
                pdb = "Not reached"
            else:
                pdb = "Not recorded"
            if view.verifier_summary is not None:
                verifier = "Completed"
            elif view.verifier_stages:
                verifier = "Active"
            else:
                verifier = "Not started"
            def row(label: str, value: str) -> str:
                return f"[#8b949e]{label:<10}[/] [bright_white]{_markup_escape(value)}[/]"
            repo_basename = "—"
            head_short = "—"
            # Model field may hold repo basename for local project when stored as profile_id
            lines = [
                "[bold #79c0ff]RUN[/]",
                "[bright_white]Local Project Debug[/]",
                row("NOW", state.operation_label),
                row("TARGET", state.current_target or "Not observed"),
                row("MODEL", f"Request {counter(state.request_ordinal, state.ceilings.model_requests)}"),
                row("STEP", counter(state.controller_step_ordinal, state.ceilings.controller_steps)),
                row("ATTEMPT", counter(state.candidate_attempt_ordinal, state.ceilings.candidate_attempts)),
                row("PDB", pdb),
                row("VERIFIER", verifier),
                row("Project", repo_basename),
                row("Source HEAD", head_short),
            ]
            self._text.update("\n".join(lines))
            return
        metadata = ladder_task_metadata(view.task_id) if is_ladder_task(view.task_id) else None
        runtime = {
            SourceKind.OFFLINE_DEMO: "Local deterministic",
            SourceKind.CONFIGURED_MODEL: "Configured command",
            SourceKind.OLLAMA_CLOUD_LADDER: "Ollama Cloud",
            SourceKind.LEVEL32_OPERATOR: "Ollama Cloud",
        }.get(view.source_kind, "Recorded source")
        treatment = (
            f"V{view.model_provenance.treatment_revision}"
            if view.source_kind is SourceKind.LEVEL32_OPERATOR and view.model_provenance and view.model_provenance.treatment_revision is not None
            else metadata.treatment if metadata is not None else "Not recorded"
        )
        def counter(value, maximum):
            if value is None:
                return "Not recorded"
            return f"{value} / {maximum}" if maximum is not None else str(value)
        def duration(value):
            return "—" if value is None else f"{value:.1f}s"
        if view.pdb_observed:
            pdb = "Observed"
        elif view.debugger.session_started:
            pdb = "Active / awaiting evidence"
        elif view.status.terminal:
            pdb = "Not reached"
        else:
            pdb = "Not recorded"
        # Durable verifier truth: VERIFIER_COMPLETED is the authority.
        # Do NOT infer from outcome (resolved vs unresolved) and do NOT
        # rely on ephemeral liveness after terminal completion.
        if view.verifier_summary is not None:
            verifier = "Completed"
        elif view.verifier_stages:
            verifier = "Active"
        elif view.operator_stage in (
            OperatorStage.VERIFICATION,
            OperatorStage.OFFICIAL_VERIFICATION,
            OperatorStage.OFFICIAL_VERIFICATION_PREPARING,
            OperatorStage.OFFICIAL_EVALUATOR_STARTED,
            OperatorStage.OFFICIAL_EVALUATOR_COMPLETED,
        ):
            # Level-32 official verification in progress: treat as active
            # unless already settled via verifier_summary.
            verifier = "Active" if not view.status.terminal else "Not completed"
        else:
            verifier = "Not started"
        official = _official_tests_label(view)

        def row(label: str, value: str) -> str:
            return (
                f"[#8b949e]{label:<10}[/] "
                f"[bright_white]{_markup_escape(value)}[/]"
            )

        lines = [
            "[bold #79c0ff]RUN[/]",
            f"[bright_white]{_markup_escape(task_display_title(view.task_id))}[/]",
            row("NOW", state.operation_label),
            row("TARGET", state.current_target or "Not observed"),
            row("MODEL", f"Request {counter(state.request_ordinal, state.ceilings.model_requests)}"),
            row("STEP", counter(state.controller_step_ordinal, state.ceilings.controller_steps)),
            row("ATTEMPT", counter(state.candidate_attempt_ordinal, state.ceilings.candidate_attempts)),
            row("PDB", pdb),
            row("VERIFIER", verifier),
        ]
        if state.live and state.snapshot is not None:
            lines.extend((
                row("ELAPSED", duration(state.request_elapsed_seconds)),
                row("ACTIVITY", f"{duration(state.last_activity_age_seconds)} ago"),
                row("TRANSPORT", "Alive" if state.snapshot.transport_alive else "Idle"),
                row("WATCHDOG", duration(state.snapshot.watchdog_idle_seconds)),
            ))
        lines.extend((
            row(
                "Model",
                view.model_provenance.display_name
                if view.model_provenance and view.model_provenance.display_name
                else "Not recorded",
            ),
            row(
                "Alias",
                view.model_provenance.profile_id
                if view.model_provenance and view.model_provenance.profile_id
                else "Not recorded",
            ),
        ))
        if metadata is not None:
            lines.extend((
                row("Runtime", runtime),
                row("Evaluation", metadata.evaluation),
                row("Treatment", treatment),
            ))
        if official is not None:
            lines.append(row("Official", official))
        self._text.update("\n".join(lines))


_STATUS_MARKER: dict[WorkstreamStatus, tuple[str, str]] = {
    WorkstreamStatus.ACTIVE: ("→", "bold #79c0ff"),
    WorkstreamStatus.COMPLETED: ("✓", "green"),
    WorkstreamStatus.FAILED: ("×", "red"),
    WorkstreamStatus.WAITING: ("~", "yellow"),
}

_KIND_LABEL_STYLE = {
    "change": "bold #ffa657",
    "official_verification": "bold #d2a8ff",
    "verification": "#a371f7",
    "error": "bold red",
}


def _kind_style(entry: WorkstreamEntry) -> str:
    return _KIND_LABEL_STYLE.get(entry.kind.value, "bright_white")


def _change_stats_text(change: ChangePreview) -> str:
    if change.multi_file:
        return f"{len(change.files) + change.omitted_files} files · +{change.additions} -{change.deletions}"
    return f"+{change.additions} -{change.deletions}"


def _append_diff_lines(text: "Text", change: ChangePreview, *, indent: str) -> None:
    """Append the bounded, terminal-native diff body of the primary file."""
    for line in change.lines:
        pad = " " * len(indent)
        if line.kind is DiffLineKind.HUNK:
            text.append(f"{pad}{line.text}\n", style="cyan")
            continue
        number = line.old_lineno if line.old_lineno is not None else line.new_lineno
        prefix = {
            DiffLineKind.CONTEXT: " ",
            DiffLineKind.ADDED: "+",
            DiffLineKind.REMOVED: "-",
        }[line.kind]
        style = {
            DiffLineKind.CONTEXT: "#8b949e",
            DiffLineKind.ADDED: "green",
            DiffLineKind.REMOVED: "red",
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
        text.append(f" {entry.ordinal}", style="#c9d1d9")
    target = entry.target
    if not target and entry.change is not None and entry.change.primary_path:
        # A rejected candidate has no authoritative changed-file list; the
        # preview's primary path is the honest fallback.
        target = entry.change.primary_path
    if target:
        text.append(f"  {target}", style="#c9d1d9")
    if entry.change is not None:
        text.append(f"  {_change_stats_text(entry.change)}", style="bold green")
    detail = entry.detail
    if entry.change is not None and detail and detail.startswith("+") and detail.endswith("more"):
        # The preview's file summary already states what was changed.
        detail = None
    if detail:
        text.append(f"  · {detail}", style="#8b949e")
    text.append("\n")
    if with_change_body and entry.change is not None and not narrow:
        change = entry.change
        if change.multi_file:
            for file_summary in change.files:
                text.append(
                    f"    {file_summary.operation.value} {file_summary.path}"
                    f"  +{file_summary.additions} -{file_summary.deletions}\n",
                    style="#8b949e",
                )
            if change.omitted_files:
                text.append(f"    … +{change.omitted_files} more\n", style="dim")
        if change.primary_path and change.multi_file:
            text.append(f"    {change.primary_path}\n", style="#c9d1d9")
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
    text.append(f"{prefix} · ", style="bold #8b949e")
    text.append(f"{header}\n", style="bold #ffffff")
    text.append("─" * 40 + "\n", style="dim")
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
    for entry in view.timeline:
        marker = "» " if entry.sequence in boundaries else ""
        lines.append(f"{marker}#{entry.sequence} {entry.summary}")
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
