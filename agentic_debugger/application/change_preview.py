"""Bounded change previews projected from authoritative candidate patches.

This module owns the pure, Textual-free diff-projection layer of the
workstream architecture: :func:`build_change_preview` turns the
authoritative candidate patch text (``patch.proposed`` ->
``PatchAttemptView.patch_text``) into a small terminal-native diff
projection.  Anything unparseable fails closed to ``None`` rather than a
fabricated or misleading rendering.

Dependency rule: this module imports nothing from the application package.
It is imported by :mod:`agentic_debugger.application.workstream_entries`
(the entry contract carries a :class:`ChangePreview`) and re-exported by
:mod:`agentic_debugger.application.workstream`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    "build_change_preview",
]

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


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
