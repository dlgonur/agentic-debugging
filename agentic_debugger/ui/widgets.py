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
from textual.containers import VerticalScroll
from textual.widgets import Static

from agentic_debugger.application.events import (
    OperatorStage,
    SessionEventKind,
    SessionStatus,
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

_NOT_RECORDED = "NOT RECORDED"


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
            text.append(
                f"Attempt {attempt.attempt_index} — {stage.value.upper()}",
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
            if attempt.patch_text:
                text.append("\n", style="dim")
                for line in attempt.patch_text.splitlines():
                    if line.startswith("+") and not line.startswith("+++"):
                        style = "green"
                    elif line.startswith("-") and not line.startswith("---"):
                        style = "red"
                    elif line.startswith("@@"):
                        style = "cyan"
                    else:
                        style = "dim"
                    text.append(line, style=style)
                    text.append("\n")
                text.append("\n")
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
        stage = _operator_stage_label(view.operator_stage) if view.operator_stage else "Not recorded"
        if view.debugger.session_started:
            pdb = "Observed"
        elif view.operator_stage is OperatorStage.DEBUGGER:
            pdb = "Active"
        else:
            pdb = "Pending"
        if view.verifier_summary is not None:
            verifier = view.verifier_summary.outcome.value if view.verifier_summary.outcome else "Completed"
        elif view.verifier_stages:
            verifier = "Active"
        else:
            verifier = "Pending"
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
        official = None
        if view.source_kind is SourceKind.LEVEL32_OPERATOR:
            official = (
                "Executed"
                if view.verifier_summary is not None
                and view.verifier_summary.official_test_execution_proven is True
                else "Not executed"
            )
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


class ReplayBar(Static):
    """Replay-control footer (position + key hints)."""


class LiveBar(Static):
    """Live-session footer (operational status + cancel hint)."""


__all__ = [
    "ActivityPanel",
    "DebuggerPanel",
    "EvidenceState",
    "LiveBar",
    "LiveRunContextPanel",
    "PatchPanel",
    "ReplayBar",
    "SourcePanel",
    "StatusHeader",
    "TimelinePanel",
    "VerifierPanel",
]
