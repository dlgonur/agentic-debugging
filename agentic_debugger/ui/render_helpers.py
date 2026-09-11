"""Shared UI rendering helpers: styles, escaping, source highlighting.

This module owns the style vocabulary and bounded text helpers shared by
the session panels, timing/timeline rendering, live/workstream
rendering, and text exports: markup escaping, operator-stage and patch-
stage styles, timeline entry styles, the activity filter vocabulary,
section/key-value appends, Pygments source-token styles and line
highlighting, and count formatting.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pygments import lex
from pygments.lexers import ClassNotFound, get_lexer_for_filename
from pygments.token import Comment, Keyword, Name, Number, Operator, String, Token
from rich.text import Text

from agentic_debugger.application.events import (
    OperatorStage,
    SessionEventKind,
)
from agentic_debugger.application.presentation import PatchStage, TimelineEntry
from agentic_debugger.application.workstream import (
    ChangePreview,
    ChangePreviewLimits,
    DiffLine,
    DiffLineKind,
    WorkstreamEntry,
    WorkstreamKind,
    WorkstreamStatus,
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


def _kind_style(entry: WorkstreamEntry) -> str:
    return _KIND_LABEL_STYLE.get(entry.kind.value, FOREGROUND)


def _kind_badge(entry: WorkstreamEntry) -> str:
    kind = entry.kind
    if kind is WorkstreamKind.SESSION:
        return "SESSION"
    if kind is WorkstreamKind.WORKSPACE:
        return "WORKSPACE"
    if kind is WorkstreamKind.MODEL_REQUEST:
        return f"MODEL #{entry.ordinal}" if entry.ordinal is not None else "MODEL"
    if kind is WorkstreamKind.SOURCE_READ:
        return "SOURCE"
    if kind is WorkstreamKind.TOOL:
        return "TOOL"
    if kind is WorkstreamKind.DEBUGGER:
        return "DEBUGGER"
    if kind is WorkstreamKind.PDB:
        return "RUNTIME"
    if kind is WorkstreamKind.DIAGNOSIS:
        return "DIAGNOSIS"
    if kind is WorkstreamKind.CHANGE:
        return f"PATCH #{entry.ordinal}" if entry.ordinal is not None else "PATCH"
    if kind is WorkstreamKind.VERIFICATION:
        return "VERIFY"
    if kind is WorkstreamKind.OFFICIAL_VERIFICATION:
        return "OFFICIAL VER"
    if kind is WorkstreamKind.CLEANUP:
        return "CLEANUP"
    if kind is WorkstreamKind.ERROR:
        return "ERROR"
    return kind.value.upper()


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


def _counts(passed: Optional[int], total: Optional[int]) -> str:
    if passed is None and total is None:
        return "not recorded"
    return f"{passed}/{total}"


_START_TO_COMPLETION_KINDS: dict[SessionEventKind, SessionEventKind] = {
    SessionEventKind.MODEL_REQUEST_STARTED: SessionEventKind.MODEL_REQUEST_COMPLETED,
    SessionEventKind.TOOL_STARTED: SessionEventKind.TOOL_COMPLETED,
    SessionEventKind.VERIFIER_STAGE_STARTED: SessionEventKind.VERIFIER_STAGE_COMPLETED,
    SessionEventKind.VERIFIER_STARTED: SessionEventKind.VERIFIER_COMPLETED,
    SessionEventKind.CLEANUP_STARTED: SessionEventKind.CLEANUP_COMPLETED,
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
