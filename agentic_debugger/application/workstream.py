"""Operational workstream projection and bounded change previews.

This module is a pure, Textual-free presentation projection over facts the
durable session-event path already owns.  It creates no evidence: every
work unit and every diff line is derived from a :class:`SessionEvent` (or a
patch body such an event already carries), so observer-only presentation
cannot alter controller, model, patch, PDB, verifier, or cleanup behavior.

Two layers live here:

- a bounded **change preview** (:func:`build_change_preview`) that turns the
  authoritative candidate patch text (``patch.proposed`` →
  ``PatchAttemptView.patch_text``) into a small terminal-native diff
  projection; anything unparseable fails closed to ``None`` rather than a
  fabricated or misleading rendering;

- the **workstream** (:class:`WorkstreamEntry`) maintained incrementally by
  the shared reducer via :func:`apply_workstream_event`.  Entries have
  semantic identity (tool identity, request ordinal, attempt ordinal,
  verifier unit), so a started operation settles its own entry instead of
  producing endless duplicate rows, and distinct operations are never
  merged.

Dependency rule: this module imports nothing from the application package
(the reducer imports *these* types).  Event facts arrive as plain arguments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional, Tuple

__all__ = [
    "ChangeFileSummary",
    "ChangePreview",
    "ChangePreviewLimits",
    "DEFAULT_PREVIEW_LIMITS",
    "DiffLine",
    "DiffLineKind",
    "DiffPathKind",
    "MAX_WORKSTREAM_ENTRIES",
    "WorkstreamEntry",
    "WorkstreamKind",
    "WorkstreamStatus",
    "apply_workstream_event",
    "build_change_preview",
]

#: Durable retained workstream entries (tail-bounded; rendering shows far
#: fewer).  Small because entries are semantic units, not raw events.
MAX_WORKSTREAM_ENTRIES = 500

#: Tool names that read source (rendered as READ SOURCE units).
_SOURCE_READ_TOOLS = frozenset(
    {"get_source_window", "search_code", "find_function", "find_class"}
)

#: Tool names that drive the debugger session (rendered as DEBUGGER units).
_DEBUGGER_TOOLS = frozenset(
    {
        "start_pdb_session",
        "continue_pdb_session",
        "step_pdb_session",
        "next_pdb_session",
        "stop_pdb_session",
    }
)

#: Tool names that observe PDB state (rendered as PDB units).
_PDB_OBSERVE_TOOLS = frozenset(
    {
        "get_stack_summary",
        "get_frame",
        "get_frame_locals",
        "safe_eval_expression",
        "inspect_caller_frame",
    }
)

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class WorkstreamKind(str, Enum):
    """Semantic type of one operational work unit."""

    WORKSPACE = "workspace"
    MODEL_REQUEST = "model_request"
    SOURCE_READ = "source_read"
    TOOL = "tool"
    DEBUGGER = "debugger"
    PDB = "pdb"
    DIAGNOSIS = "diagnosis"
    CHANGE = "change"
    VERIFICATION = "verification"
    OFFICIAL_VERIFICATION = "official_verification"
    CLEANUP = "cleanup"
    ERROR = "error"
    SESSION = "session"


class WorkstreamStatus(str, Enum):
    """Lifecycle status of one operational work unit."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING = "waiting"


class DiffLineKind(str, Enum):
    """Rendering class of one bounded diff line."""

    HUNK = "hunk"
    CONTEXT = "context"
    ADDED = "added"
    REMOVED = "removed"


class FileOperation(str, Enum):
    """Repository file operation of one changed file.

    ``MODIFIED``/``ADDED``/``DELETED`` are ordinary repository code changes.
    Binary patch bodies are not supported in this campaign: a binary or
    unparseable patch still fails closed to no inline preview.
    """

    MODIFIED = "M"
    ADDED = "A"
    DELETED = "D"


@dataclass(frozen=True)
class DiffLine:
    """One bounded diff line with its authoritative line numbers."""

    kind: DiffLineKind
    old_lineno: Optional[int]
    new_lineno: Optional[int]
    text: str


@dataclass(frozen=True)
class ChangeFileSummary:
    """Per-file change counts derived from the candidate patch only."""

    path: str
    additions: int
    deletions: int
    operation: FileOperation = FileOperation.MODIFIED


@dataclass(frozen=True)
class ChangePreviewLimits:
    """Deterministic bounds for the live change preview."""

    max_files: int = 4
    max_hunks: int = 2
    max_lines: int = 8
    max_line_chars: int = 96


DEFAULT_PREVIEW_LIMITS = ChangePreviewLimits()


@dataclass(frozen=True)
class ChangePreview:
    """A bounded, terminal-native projection of one candidate patch.

    ``files`` carries the per-file summary (bounded); ``lines`` carries the
    bounded diff body of the *primary* (most recent) file only.
    ``truncated``/``omitted_*`` state honestly what was left out.  Built
    exclusively from the authoritative patch text -- never from model prose.
    """

    files: Tuple[ChangeFileSummary, ...]
    additions: int
    deletions: int
    primary_path: Optional[str]
    lines: Tuple[DiffLine, ...]
    truncated: bool
    omitted_files: int
    omitted_lines: int

    @property
    def multi_file(self) -> bool:
        return len(self.files) > 1


def _truncate_line(text: str, max_chars: int) -> str:
    text = text.replace("\t", "    ").rstrip()
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 1)] + "…"


def _parse_hunk_header(line: str) -> Optional[Tuple[int, int, int, int]]:
    match = _HUNK_HEADER_RE.match(line)
    if match is None:
        return None
    old_start = int(match.group(1))
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_start = int(match.group(3))
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    return old_start, old_count, new_start, new_count


class DiffPathKind(str, Enum):
    """Parse result of one ``---``/``+++`` diff header path.

    ``DEV_NULL`` is the explicit file-addition/deletion sentinel and is
    deliberately distinct from ``INVALID`` (absolute, parent-traversing, or
    otherwise unsafe paths).  A generic failure never implies ``/dev/null``.
    """

    VALID = "valid"
    DEV_NULL = "dev_null"
    INVALID = "invalid"


def _diff_path(header: str) -> Tuple[DiffPathKind, Optional[str]]:
    """Classify one diff header path (``a/``/``b/`` prefixes stripped).

    Returns ``(DiffPathKind.VALID, path)`` for safe repository-relative
    logical paths, ``(DiffPathKind.DEV_NULL, None)`` only for the exact
    ``/dev/null`` sentinel, and ``(DiffPathKind.INVALID, None)`` for
    absolute paths, drive letters, parent traversal, or empty values.
    """
    path = header.strip()
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    path = path.strip("\t ")
    if path == "/dev/null":
        return DiffPathKind.DEV_NULL, None
    if not path:
        return DiffPathKind.INVALID, None
    if path.startswith(("/", "\\")):
        return DiffPathKind.INVALID, None
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return DiffPathKind.INVALID, None
    if ".." in path.replace("\\", "/").split("/"):
        return DiffPathKind.INVALID, None
    return DiffPathKind.VALID, path


#: Safe Git metadata lines accepted between ``diff --git`` and the next
#: ``---``/``+++`` header pair (normal text modifications/additions/
#: deletions only).  Renames/copies and any unknown metadata fail closed.
_GIT_METADATA_PREFIXES = (
    "index ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
)


def _is_git_metadata(line: str) -> bool:
    return line.startswith(_GIT_METADATA_PREFIXES) or line in ("---", "+++")


def build_change_preview(
    patch_text: str,
    limits: ChangePreviewLimits = DEFAULT_PREVIEW_LIMITS,
) -> Optional[ChangePreview]:
    """Project one bounded change preview from real unified-diff text.

    Fail-closed: structurally unexpected content yields ``None`` (no
    preview) instead of a partial or invented rendering.  The input is the
    model-authored candidate patch already recorded by the durable path;
    nothing else (prompts, completions, tool stdout) ever enters here.

    Supported file forms: ``--- a/x``/``+++ b/x`` (modified), ``---
    /dev/null``/``+++ b/x`` (added), ``--- a/x``/``+++ /dev/null``
    (deleted), including realistic Git multi-file output with ``diff --git``
    separators and safe mode/index metadata.  ``/dev/null`` is an explicit
    sentinel: an invalid path is never treated as a file addition.  Every
    hunk must consume exactly the old/new counts declared in its
    ``@@ -o,c +n,c @@`` header before the next hunk, the next file section,
    or end of patch; an underfilled or overrunning hunk returns ``None``.
    The standard ``\\ No newline at end of file`` marker consumes zero hunk
    lines and must directly follow the line it annotates.
    """
    if type(patch_text) is not str or not patch_text.strip():
        return None
    files: list[Tuple[Optional[str], str, list[Tuple[str, str]]]] = []
    current_path: Optional[str] = None
    current_operation: Optional[FileOperation] = None
    current_body: list[Tuple[str, str]] = []
    hunk_pending = False
    remaining_old = remaining_new = 0
    expect_new_header = False
    #: A ``diff --git`` separator was seen and its section has not yet
    #: produced its ``---``/``+++`` header pair.  An incomplete trailing
    #: Git section must fail closed, never disappear silently.
    git_pending = False

    def _flush_file() -> bool:
        """Close the current file section; False means the section is
        malformed (underfilled hunk, header-only, or an old header awaiting
        its ``+++``) and the parse must fail closed.  Callers honor the
        result."""
        nonlocal current_path, current_operation, current_body, hunk_pending, expect_new_header
        if hunk_pending:
            # A section ended while its hunk was still underfilled.
            return False
        if expect_new_header:
            # An old header waiting for its matching +++ header is ALWAYS
            # an incomplete section, regardless of whether the old side is a
            # VALID path or DEV_NULL (which intentionally has no path yet).
            # A diff --git separator, another --- header, EOF, or any other
            # structural transition may never silently close/reset it.
            return False
        if current_path is not None:
            if not current_body:
                # Header pair without a valid hunk/body: never dropped.
                return False
            files.append(
                (current_path, current_operation or FileOperation.MODIFIED, current_body)
            )
        current_path = None
        current_operation = None
        current_body = []
        hunk_pending = False
        expect_new_header = False
        return True

    for raw in patch_text.splitlines():
        line = raw.rstrip("\r")
        if hunk_pending:
            # Hunk body records take precedence over file-header tokens: a
            # removed source line may legitimately begin with "-- " and an
            # added one with "++ " (after the diff prefix).  File headers,
            # ``diff --git``, Git metadata, and new hunk/file boundaries
            # are recognized only after the current hunk counts have been
            # fully satisfied.
            if line.startswith("\\ "):
                # No-newline marker: annotates the preceding line, consumes
                # zero hunk lines.
                if not current_body or current_body[-1][0] in ("@", "\\"):
                    return None
                current_body.append(("\\", line[2:].strip()))
                continue
            if line.startswith(" ") or (
                line == "" and remaining_old > 0 and remaining_new > 0
            ):
                # A truly empty line inside a hunk counts as blank context
                # (editors strip the trailing space of " " context lines).
                if remaining_old <= 0 or remaining_new <= 0:
                    return None
                current_body.append((" ", line[1:]))
                remaining_old -= 1
                remaining_new -= 1
            elif line.startswith("-"):
                if remaining_old <= 0:
                    return None
                current_body.append(("-", line[1:]))
                remaining_old -= 1
            elif line.startswith("+"):
                if remaining_new <= 0:
                    return None
                current_body.append(("+", line[1:]))
                remaining_new -= 1
            else:
                return None
            if remaining_old == 0 and remaining_new == 0:
                hunk_pending = False
            continue

        # -- not inside an active hunk: structural tokens only ------------
        if line.startswith("\\ "):
            # No-newline marker directly after a completed hunk.
            if not current_body or current_body[-1][0] in ("@", "\\"):
                return None
            current_body.append(("\\", line[2:].strip()))
            continue
        if line.startswith("@@ "):
            parsed = _parse_hunk_header(line)
            if parsed is None or current_path is None:
                return None
            remaining_old, remaining_new = parsed[1], parsed[3]
            if remaining_old == 0 and remaining_new == 0:
                return None
            hunk_pending = True
            expect_new_header = False
            current_body.append(("@", line))
            continue
        if line.startswith("diff --git "):
            if not _flush_file():
                return None
            git_pending = True
            continue
        if line.startswith("--- "):
            if not _flush_file():
                return None
            kind, old_path = _diff_path(line[4:])
            if kind is DiffPathKind.INVALID:
                return None
            current_path = old_path  # None for DEV_NULL (added file)
            current_operation = None
            expect_new_header = True
            git_pending = False
            continue
        if line.startswith("+++ "):
            if not expect_new_header:
                return None
            new_kind, new_path = _diff_path(line[4:])
            if new_kind is DiffPathKind.INVALID:
                return None
            if current_path is None and new_kind is DiffPathKind.VALID:
                # --- /dev/null + +++ b/x  =>  added file
                current_operation = FileOperation.ADDED
                current_path = new_path
            elif new_kind is DiffPathKind.DEV_NULL and current_path is not None:
                # --- a/x + +++ /dev/null  =>  deleted file
                current_operation = FileOperation.DELETED
            elif (
                new_kind is DiffPathKind.VALID
                and current_path is not None
                and new_path == current_path
            ):
                current_operation = FileOperation.MODIFIED
            else:
                return None
            expect_new_header = False
            continue
        if expect_new_header:
            # Only safe Git metadata may appear between diff --git and the
            # ---/+++ pair; anything else fails closed.
            if line.strip() == "" or _is_git_metadata(line):
                continue
            return None
        if current_path is None:
            # Between file sections only blank separators / git metadata
            # are ignored; anything else fails closed.
            if line.strip() == "" or _is_git_metadata(line):
                continue
            return None
        if line.strip() == "":
            continue
        return None
    if hunk_pending:
        # End of patch while a hunk was still underfilled.
        return None
    if git_pending:
        # Trailing diff --git section that never produced file headers.
        return None
    if expect_new_header:
        # An old header without its matching +++ header (incomplete pair).
        return None
    if not _flush_file():
        return None
    if not files:
        return None

    summaries: list[ChangeFileSummary] = []
    total_add = total_del = 0
    for path, operation, body in files:
        additions = sum(1 for prefix, _ in body if prefix == "+")
        deletions = sum(1 for prefix, _ in body if prefix == "-")
        summaries.append(
            ChangeFileSummary(
                path=path, additions=additions, deletions=deletions,
                operation=operation,
            )
        )
        total_add += additions
        total_del += deletions

    # Primary file = the most recently described file in the patch.
    primary_path, _, primary_body = files[-1]
    omitted_files = max(0, len(summaries) - limits.max_files)
    shown_files = tuple(summaries[: limits.max_files])

    preview_lines: list[DiffLine] = []
    omitted_lines = 0
    truncated = False
    hunks_used = 0
    old_lineno = new_lineno = 0
    body_index = 0
    while body_index < len(primary_body):
        prefix, text = primary_body[body_index]
        body_index += 1
        if prefix == "@":
            if hunks_used >= limits.max_hunks or len(preview_lines) >= limits.max_lines:
                truncated = True
                omitted_lines += sum(
                    1 for item_prefix, _ in primary_body[body_index - 1 :] if item_prefix != "@"
                )
                break
            parsed = _parse_hunk_header(text)
            if parsed is None:  # pragma: no cover - guarded above
                return None
            old_lineno, new_lineno = parsed[0], parsed[2]
            preview_lines.append(
                DiffLine(
                    DiffLineKind.HUNK,
                    None,
                    None,
                    _truncate_line(text, limits.max_line_chars),
                )
            )
            hunks_used += 1
            continue
        if prefix == "\\":
            # No-newline annotation: never a displayed source-code line.
            continue
        if len(preview_lines) >= limits.max_lines:
            truncated = True
            omitted_lines += 1
            continue
        truncated_text = _truncate_line(text, limits.max_line_chars)
        if prefix == " ":
            preview_lines.append(
                DiffLine(DiffLineKind.CONTEXT, old_lineno, new_lineno, truncated_text)
            )
            old_lineno += 1
            new_lineno += 1
        elif prefix == "-":
            preview_lines.append(
                DiffLine(DiffLineKind.REMOVED, old_lineno, None, truncated_text)
            )
            old_lineno += 1
        elif prefix == "+":
            preview_lines.append(
                DiffLine(DiffLineKind.ADDED, None, new_lineno, truncated_text)
            )
            new_lineno += 1
        else:
            preview_lines.append(
                DiffLine(DiffLineKind.CONTEXT, None, None, truncated_text)
            )
    # Never end on a hunk header whose body did not fit at all.
    while preview_lines and preview_lines[-1].kind is DiffLineKind.HUNK:
        preview_lines.pop()
        hunks_used -= 1

    return ChangePreview(
        files=shown_files,
        additions=total_add,
        deletions=total_del,
        primary_path=primary_path,
        lines=tuple(preview_lines),
        truncated=truncated,
        omitted_files=omitted_files,
        omitted_lines=omitted_lines,
    )


@dataclass(frozen=True)
class WorkstreamEntry:
    """One curated operational work unit.

    ``sequence`` is the durable event sequence that last touched the unit
    (ordering authority); ``ordinal`` is the one-based user-facing
    request/attempt number when the unit has one.  ``change`` carries the
    bounded diff preview for code-changing units only.
    """

    kind: WorkstreamKind
    status: WorkstreamStatus
    label: str
    sequence: int
    target: Optional[str] = None
    detail: Optional[str] = None
    ordinal: Optional[int] = None
    change: Optional[ChangePreview] = None
    timestamp_utc: Optional[str] = None
    duration_seconds: Optional[float] = None


def _append(
    entries: Tuple[WorkstreamEntry, ...], entry: WorkstreamEntry
) -> Tuple[WorkstreamEntry, ...]:
    updated = entries + (entry,)
    if len(updated) > MAX_WORKSTREAM_ENTRIES:
        updated = updated[len(updated) - MAX_WORKSTREAM_ENTRIES :]
    return updated


def _settle(
    entries: Tuple[WorkstreamEntry, ...],
    *,
    kind: WorkstreamKind,
    status: WorkstreamStatus,
    sequence: int,
    label: Optional[str] = None,
    detail: Optional[str] = None,
    target: Optional[str] = None,
    ordinal: Optional[int] = None,
    match_detail: Optional[str] = None,
    timestamp_utc: Optional[str] = None,
    duration_seconds: Optional[float] = None,
) -> Tuple[WorkstreamEntry, ...]:
    """Settle the most recent ACTIVE entry of one kind (identity match).

    Returns the input unchanged when no matching active unit exists.
    """
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if entry.kind is not kind or entry.status is not WorkstreamStatus.ACTIVE:
            continue
        if ordinal is not None and entry.ordinal is not None and entry.ordinal != ordinal:
            continue
        if match_detail is not None and entry.detail != match_detail:
            continue
        settled = replace(
            entry,
            status=status,
            sequence=sequence,
            label=label or entry.label,
            detail=detail if detail is not None else entry.detail,
            target=target or entry.target,
            timestamp_utc=entry.timestamp_utc or timestamp_utc,
            duration_seconds=duration_seconds if duration_seconds is not None else entry.duration_seconds,
        )
        return entries[:index] + (settled,) + entries[index + 1 :]
    return entries


def _coalesce_completed(
    entries: Tuple[WorkstreamEntry, ...], entry: WorkstreamEntry
) -> Tuple[WorkstreamEntry, ...]:
    """Coalesce a completed unit with an identical immediately-preceding one.

    Only *identical operational identity* (same kind, label, and target)
    coalesces: repeated PDB observations of the same pause location collapse
    to a single observed row while distinct locations stay distinct rows.
    """
    if entries:
        last = entries[-1]
        if (
            last.kind is entry.kind
            and last.label == entry.label
            and last.target == entry.target
            and last.status is WorkstreamStatus.COMPLETED
        ):
            return entries[:-1] + (entry,)
    return _append(entries, entry)


def _tool_unit(tool_name: str) -> Tuple[WorkstreamKind, str]:
    """Map one tool identity to its human-facing work-unit class."""
    if tool_name in _SOURCE_READ_TOOLS:
        return WorkstreamKind.SOURCE_READ, "Read source"
    if tool_name in _DEBUGGER_TOOLS:
        return WorkstreamKind.DEBUGGER, "Debugger"
    if tool_name in _PDB_OBSERVE_TOOLS:
        return WorkstreamKind.PDB, "PDB observe"
    if tool_name == "express_root_cause_hypothesis":
        return WorkstreamKind.DIAGNOSIS, "Diagnosis"
    if tool_name == "apply_patch":
        return WorkstreamKind.CHANGE, "Change"
    if tool_name == "run_reproduction":
        return WorkstreamKind.TOOL, "Run reproduction"
    if tool_name in ("run_tests", "run_regression_tests"):
        return WorkstreamKind.TOOL, "Run tests"
    return WorkstreamKind.TOOL, tool_name


def _tool_detail(tool_name: str, unit_label: str) -> Optional[str]:
    """Secondary tool text: kept only when it is not the label itself."""
    if tool_name == unit_label or tool_name.replace("_", " ") == unit_label.lower():
        return None
    return tool_name


def _change_index(
    entries: Tuple[WorkstreamEntry, ...], ordinal: int
) -> Optional[int]:
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if entry.kind is WorkstreamKind.CHANGE and entry.ordinal == ordinal:
            return index
    return None


def _update_change(
    entries: Tuple[WorkstreamEntry, ...],
    ordinal: int,
    *,
    active_only: bool = False,
    **fields: object,
) -> Tuple[WorkstreamEntry, ...]:
    """Update one change unit by attempt ordinal (no-op when absent).

    ``active_only`` restricts the update to units still in flight so a late
    lifecycle fact can never regress a settled apply/reject outcome.
    """
    index = _change_index(entries, ordinal)
    if index is None:
        return entries
    entry = entries[index]
    if active_only and entry.status is not WorkstreamStatus.ACTIVE:
        return entries
    updated = replace(entry, **fields)  # type: ignore[arg-type]
    return entries[:index] + (updated,) + entries[index + 1 :]


def apply_workstream_event(
    entries: Tuple[WorkstreamEntry, ...],
    *,
    event_kind: str,
    payload: dict,
    sequence: int,
    in_flight_attempt_ordinal: int,
    debugger_target: Optional[str] = None,
    timestamp_utc: Optional[str] = None,
    duration_seconds: Optional[float] = None,
) -> Tuple[WorkstreamEntry, ...]:
    """Fold one durable session event into the curated workstream.

    Pure and total: unknown facts leave the workstream unchanged.
    ``in_flight_attempt_ordinal`` is the one-based ordinal of the candidate
    currently being applied (derived by the reducer from recorded attempts);
    ``debugger_target`` is the last recorded debugger location
    (``script:line``) used only to label PDB observation units.
    """
    folded = _fold_workstream_event(
        entries,
        event_kind=event_kind,
        payload=payload,
        sequence=sequence,
        in_flight_attempt_ordinal=in_flight_attempt_ordinal,
        debugger_target=debugger_target,
        timestamp_utc=timestamp_utc,
        duration_seconds=duration_seconds,
    )
    if folded is entries:
        return entries
    return folded


def _fold_workstream_event(
    entries: Tuple[WorkstreamEntry, ...],
    *,
    event_kind: str,
    payload: dict,
    sequence: int,
    in_flight_attempt_ordinal: int,
    debugger_target: Optional[str] = None,
    timestamp_utc: Optional[str] = None,
    duration_seconds: Optional[float] = None,
) -> Tuple[WorkstreamEntry, ...]:
    kind = event_kind

    if kind == "session.started":
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.SESSION,
                status=WorkstreamStatus.COMPLETED,
                label="Session started",
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "model.configured":
        display = payload.get("display_name") or payload.get("profile_id")
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.SESSION,
                status=WorkstreamStatus.COMPLETED,
                label="Model configured",
                detail=display,
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "model.request_started":
        entries = _settle(
            entries,
            kind=WorkstreamKind.WORKSPACE,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            timestamp_utc=timestamp_utc,
        )
        continuing_detail = None
        for prev in reversed(entries):
            if prev.kind is WorkstreamKind.CHANGE:
                if prev.status is WorkstreamStatus.FAILED:
                    continuing_detail = "Continuing after patch failure"
                break
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.MODEL_REQUEST,
                status=WorkstreamStatus.ACTIVE,
                label="Model request",
                sequence=sequence,
                ordinal=payload["request_index"] + 1,
                detail=continuing_detail,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "model.directive_accepted":
        action_name = payload.get("action_name")
        directive_kind = payload.get("directive_kind")
        target_state = payload.get("target_state")
        action_desc = None
        if action_name:
            _action_map = {
                "get_source_window": "Inspect source",
                "get_stack_summary": "Inspect stack",
                "get_frame": "Inspect frame",
                "get_frame_locals": "Inspect locals",
                "step_over": "Step over",
                "step_into": "Step into",
                "continue_execution": "Continue execution",
                "apply_patch": "Apply change",
                "run_reproduction": "Run reproduction",
                "run_tests": "Run tests",
                "run_regression_tests": "Run regression tests",
                "express_root_cause_hypothesis": "Formulate diagnosis",
            }
            action_desc = _action_map.get(action_name, action_name.replace("_", " ").capitalize())
        elif directive_kind == "transition" and target_state:
            action_desc = f"Transition to {target_state}"

        if action_desc:
            # Find the most recent active or recent MODEL_REQUEST entry and annotate detail
            for index in range(len(entries) - 1, -1, -1):
                entry = entries[index]
                if entry.kind is WorkstreamKind.MODEL_REQUEST:
                    updated_detail = f"{entry.detail} · {action_desc}" if entry.detail and "Continuing" in entry.detail else action_desc
                    updated_entry = replace(entry, detail=updated_detail)
                    return entries[:index] + (updated_entry,) + entries[index + 1 :]
        return entries

    if kind == "model.request_completed":
        ordinal = payload["request_index"] + 1
        failed = payload.get("status") in ("error", "timeout")
        return _settle(
            entries,
            kind=WorkstreamKind.MODEL_REQUEST,
            status=WorkstreamStatus.FAILED if failed else WorkstreamStatus.COMPLETED,
            sequence=sequence,
            ordinal=ordinal,
            detail=payload.get("error_kind") if failed else None,
            timestamp_utc=timestamp_utc,
            duration_seconds=duration_seconds,
        )

    if kind == "tool.started":
        tool_name = payload["tool_name"]
        target = payload.get("target")
        if tool_name == "apply_patch":
            # The same semantic change unit: an already-proposed attempt is
            # enriched to "applying", never duplicated.
            existing = _update_change(
                entries,
                in_flight_attempt_ordinal,
                sequence=sequence,
                detail="applying",
                active_only=True,
            )
            if existing is not entries:
                return existing
            return _append(
                entries,
                WorkstreamEntry(
                    kind=WorkstreamKind.CHANGE,
                    status=WorkstreamStatus.ACTIVE,
                    label="Change",
                    sequence=sequence,
                    ordinal=in_flight_attempt_ordinal,
                    detail="applying",
                    timestamp_utc=timestamp_utc,
                ),
            )
        unit_kind, unit_label = _tool_unit(tool_name)
        return _append(
            entries,
            WorkstreamEntry(
                kind=unit_kind,
                status=WorkstreamStatus.ACTIVE,
                label=unit_label,
                sequence=sequence,
                target=target,
                detail=_tool_detail(tool_name, unit_label),
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "tool.completed":
        tool_name = payload["tool_name"]
        target = payload.get("target")
        failed = payload.get("status", "ok") != "ok"
        unit_kind, unit_label = _tool_unit(tool_name)
        if unit_kind is WorkstreamKind.CHANGE:
            # apply_patch completion: the candidate lifecycle event owns the
            # change unit; nothing to settle here.
            return entries
        detail = _tool_detail(tool_name, unit_label)
        settled = _settle(
            entries,
            kind=unit_kind,
            status=WorkstreamStatus.FAILED if failed else WorkstreamStatus.COMPLETED,
            sequence=sequence,
            target=target,
            match_detail=detail,
            timestamp_utc=timestamp_utc,
            duration_seconds=duration_seconds,
        )
        if settled is not entries:
            return settled
        # A completed tool without a started twin (the structured operator
        # channel emits source inspections as completions with the range)
        # becomes its own completed unit.
        return _coalesce_completed(
            entries,
            WorkstreamEntry(
                kind=unit_kind,
                status=WorkstreamStatus.FAILED if failed else WorkstreamStatus.COMPLETED,
                label=unit_label,
                sequence=sequence,
                target=target,
                detail=detail,
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "debugger.started":
        script = payload.get("script")
        breakpoints = payload.get("breakpoints") or ()
        target = None
        if script and breakpoints:
            first = breakpoints[0]
            tail = first.split(":")[-1]
            if tail.isdigit():
                target = f"{script}:{tail}"
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.DEBUGGER,
                status=WorkstreamStatus.ACTIVE,
                label="Start debugger",
                sequence=sequence,
                target=target or script,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "debugger.location_changed":
        script = payload.get("script")
        line = payload.get("line")
        target = f"{script}:{line}" if script and line is not None else script
        return _settle(
            entries,
            kind=WorkstreamKind.DEBUGGER,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            label="Debugger",
            target=target,
            timestamp_utc=timestamp_utc,
            duration_seconds=duration_seconds,
        )

    if kind == "debugger.stack_observed":
        target = _current_frame_target(payload.get("frames")) or debugger_target
        return _coalesce_completed(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.PDB,
                status=WorkstreamStatus.COMPLETED,
                label="PDB observed",
                sequence=sequence,
                target=target,
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "debugger.locals_observed":
        return _coalesce_completed(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.PDB,
                status=WorkstreamStatus.COMPLETED,
                label="PDB locals",
                sequence=sequence,
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "operator.progress":
        return _apply_operator_progress(entries, payload, sequence, timestamp_utc=timestamp_utc)

    if kind == "verifier.started":
        existing = _settle(
            entries,
            kind=WorkstreamKind.VERIFICATION,
            status=WorkstreamStatus.ACTIVE,
            sequence=sequence,
            label="Verifier",
            detail="running",
            timestamp_utc=timestamp_utc,
        )
        if existing is not entries:
            return existing
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.VERIFICATION,
                status=WorkstreamStatus.ACTIVE,
                label="Verifier",
                sequence=sequence,
                detail="running",
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "verifier.stage_completed":
        stage = payload.get("stage")
        return _settle(
            entries,
            kind=WorkstreamKind.VERIFICATION,
            status=WorkstreamStatus.ACTIVE,
            sequence=sequence,
            label="Verifier",
            detail=stage.replace("_", " ") if isinstance(stage, str) else None,
            timestamp_utc=timestamp_utc,
            duration_seconds=duration_seconds,
        )

    if kind == "verifier.completed":
        outcome = payload.get("outcome")
        detail = outcome.lower() if isinstance(outcome, str) else "completed"
        settled = _settle(
            entries,
            kind=WorkstreamKind.VERIFICATION,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            label="Verifier",
            detail=detail,
            timestamp_utc=timestamp_utc,
            duration_seconds=duration_seconds,
        )
        if settled is not entries:
            return settled
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.VERIFICATION,
                status=WorkstreamStatus.COMPLETED,
                label="Verifier",
                sequence=sequence,
                detail=detail,
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "patch.proposed":
        ordinal = payload["attempt_index"] + 1
        patch_text = payload.get("patch_text")
        preview = build_change_preview(patch_text) if patch_text else None
        index = _change_index(entries, ordinal)
        if index is not None:
            # Enrich the same semantic unit when the patch body arrives --
            # including late, after the apply outcome was recorded.  The
            # settled status/label of the unit are never regressed, and an
            # unparseable body never erases an existing preview.
            entry = entries[index]
            fields: dict = {"sequence": sequence}
            if entry.status is WorkstreamStatus.ACTIVE:
                fields["detail"] = "proposed"
            if preview is not None:
                fields["change"] = preview
            if timestamp_utc is not None and entry.timestamp_utc is None:
                fields["timestamp_utc"] = timestamp_utc
            updated = replace(entry, **fields)
            return entries[:index] + (updated,) + entries[index + 1 :]
        # No cross-ordinal deduplication: distinct patch attempts remain
        # distinct even when patch bodies are identical.  Same-ordinal
        # late enrichment is handled above; different ordinals are never
        # collapsed based on SHA/body/preview equality.
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CHANGE,
                status=WorkstreamStatus.ACTIVE,
                label="Change",
                sequence=sequence,
                ordinal=ordinal,
                detail="proposed",
                change=preview,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "patch.applied":
        ordinal = payload["attempt_index"] + 1
        changed = payload.get("changed_files") or ()
        detail = f"+{len(changed) - 1} more" if len(changed) > 1 else None
        fields: dict = {
            "status": WorkstreamStatus.COMPLETED,
            "sequence": sequence,
            "label": "Applied change",
            "detail": detail,
        }
        if changed:
            fields["target"] = changed[0]
        if duration_seconds is not None:
            fields["duration_seconds"] = duration_seconds
        updated = _update_change(entries, ordinal, **fields)
        if updated is not entries:
            return updated
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CHANGE,
                status=WorkstreamStatus.COMPLETED,
                label="Applied change",
                sequence=sequence,
                ordinal=ordinal,
                target=changed[0] if changed else None,
                detail=detail,
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "patch.rejected":
        ordinal = payload["attempt_index"] + 1
        updated = _update_change(
            entries,
            ordinal,
            status=WorkstreamStatus.FAILED,
            sequence=sequence,
            label="Rejected change",
            detail=payload.get("rejection_reason"),
            duration_seconds=duration_seconds,
        )
        if updated is not entries:
            return updated
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CHANGE,
                status=WorkstreamStatus.FAILED,
                label="Rejected change",
                sequence=sequence,
                ordinal=ordinal,
                detail=payload.get("rejection_reason"),
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "patch.apply_failed":
        ordinal = payload["attempt_index"] + 1
        updated = _update_change(
            entries,
            ordinal,
            status=WorkstreamStatus.FAILED,
            sequence=sequence,
            label="Apply failed",
            detail=payload.get("apply_failure_reason"),
            duration_seconds=duration_seconds,
        )
        if updated is not entries:
            return updated
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CHANGE,
                status=WorkstreamStatus.FAILED,
                label="Apply failed",
                sequence=sequence,
                ordinal=ordinal,
                detail=payload.get("apply_failure_reason"),
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "patch.reverted":
        ordinal = payload["attempt_index"] + 1
        updated = _update_change(
            entries,
            ordinal,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            label="Change reverted",
            duration_seconds=duration_seconds,
        )
        if updated is not entries:
            return updated
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CHANGE,
                status=WorkstreamStatus.COMPLETED,
                label="Change reverted",
                sequence=sequence,
                ordinal=ordinal,
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "diagnosis.recorded":
        return _coalesce_completed(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.DIAGNOSIS,
                status=WorkstreamStatus.COMPLETED,
                label="Diagnosis recorded",
                sequence=sequence,
                target=payload.get("file_path"),
                detail=payload.get("text"),
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "cleanup.started":
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CLEANUP,
                status=WorkstreamStatus.ACTIVE,
                label="Cleanup",
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "cleanup.completed":
        verified = payload.get("verified") is True
        return _settle(
            entries,
            kind=WorkstreamKind.CLEANUP,
            status=WorkstreamStatus.COMPLETED if verified else WorkstreamStatus.FAILED,
            sequence=sequence,
            label="Cleanup",
            detail="verified" if verified else "unverified",
            timestamp_utc=timestamp_utc,
            duration_seconds=duration_seconds,
        )

    if kind == "cleanup.not_required":
        # Explicit positive proof: no disposable resources were created.
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CLEANUP,
                status=WorkstreamStatus.COMPLETED,
                label="Cleanup not required",
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "session.failed":
        reason = payload.get("termination_reason")
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.ERROR,
                status=WorkstreamStatus.FAILED,
                label="Session failed",
                sequence=sequence,
                detail=reason.replace("_", " ") if isinstance(reason, str) else None,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind in ("session.completed", "session.cancelled"):
        label = (
            "Session completed" if kind == "session.completed" else "Session cancelled"
        )
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.SESSION,
                status=WorkstreamStatus.COMPLETED,
                label=label,
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )

    return entries


def _apply_operator_progress(
    entries: Tuple[WorkstreamEntry, ...],
    payload: dict,
    sequence: int,
    timestamp_utc: Optional[str] = None,
) -> Tuple[WorkstreamEntry, ...]:
    stage = payload.get("stage")
    if stage == "preparing_workspace":
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.WORKSPACE,
                status=WorkstreamStatus.ACTIVE,
                label="Preparing workspace",
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )
    if stage in ("starting", "preflight"):
        return _coalesce_completed(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.WORKSPACE,
                status=WorkstreamStatus.COMPLETED,
                label="Preflight",
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )
    if stage == "model_running":
        return _settle(
            entries,
            kind=WorkstreamKind.WORKSPACE,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            timestamp_utc=timestamp_utc,
        )
    if stage == "verification":
        existing = _settle(
            entries,
            kind=WorkstreamKind.VERIFICATION,
            status=WorkstreamStatus.ACTIVE,
            sequence=sequence,
            label="Verifier",
            detail="running",
            timestamp_utc=timestamp_utc,
        )
        if existing is not entries:
            return existing
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.VERIFICATION,
                status=WorkstreamStatus.ACTIVE,
                label="Verifier",
                sequence=sequence,
                detail="running",
                timestamp_utc=timestamp_utc,
            ),
        )
    if stage == "official_verification_preparing":
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.OFFICIAL_VERIFICATION,
                status=WorkstreamStatus.ACTIVE,
                label="Official verification",
                sequence=sequence,
                detail="preparing",
                timestamp_utc=timestamp_utc,
            ),
        )
    if stage == "official_evaluator_started":
        return _settle(
            entries,
            kind=WorkstreamKind.OFFICIAL_VERIFICATION,
            status=WorkstreamStatus.ACTIVE,
            sequence=sequence,
            label="Official verification",
            detail="evaluator running",
            timestamp_utc=timestamp_utc,
        )
    if stage == "official_evaluator_completed":
        proven = payload.get("official_execution_proven") is True
        return _settle(
            entries,
            kind=WorkstreamKind.OFFICIAL_VERIFICATION,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            label="Official verification",
            detail="execution proven" if proven else "completed (unproven)",
            timestamp_utc=timestamp_utc,
        )
    if stage in ("cleanup", "finalizing"):
        return _settle(
            entries,
            kind=WorkstreamKind.VERIFICATION,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            timestamp_utc=timestamp_utc,
        )
    return entries


def _current_frame_target(frames: object) -> Optional[str]:
    """``script:line`` of the current frame of one recorded stack."""
    if not isinstance(frames, (list, tuple)):
        return None
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        if frame.get("is_current") is not True:
            continue
        script = frame.get("file")
        line = frame.get("line")
        if isinstance(script, str) and isinstance(line, int):
            return f"{script}:{line}"
    return None
