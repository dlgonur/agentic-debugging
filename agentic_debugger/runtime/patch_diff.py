"""Unified-diff parsing contracts and the strict parser.

This module owns the unified-diff parse layer of the patch runtime:
hunk/file-patch records, diff-header path authorization (path shape
only — allow/deny policy lives in
:mod:`agentic_debugger.runtime.patch_policy`), hunk header parsing and
count validation, the LF-only line splitter, and the
``_parse_unified_diff`` state machine with its size bounds.

The parser is strict: malformed headers, unbalanced hunk counts,
duplicate file sections, unsupported Git metadata, and oversize patches
fail closed with :class:`PatchValidationError` /
:class:`PatchAuthorizationError` exactly as the unified patcher contract
requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Optional, Set

from agentic_debugger.runtime.exceptions import (
    PatchAuthorizationError,
    PatchValidationError,
)

_MAX_PATCH_CHARS = 100_000
_MAX_PATCH_FILES = 10
_MAX_PATCH_HUNKS = 100


@dataclass(frozen=True)
class _HunkLine:
    prefix: str
    text: str


@dataclass(frozen=True)
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: List[_HunkLine]


@dataclass(frozen=True)
class _ParsedFilePatch:
    path: str
    hunks: List[_Hunk]


def _parse_diff_path(header: str) -> str:
    path = header.strip()
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    if path == "/dev/null":
        raise PatchValidationError(
            "File creation/deletion not supported (/dev/null)"
        )
    if not path:
        raise PatchValidationError("Empty path in diff header")
    if "\x00" in path:
        raise PatchAuthorizationError(
            f"NUL character in diff path: {path!r}",
            path=path,
        )
    if path.startswith("/") or path.startswith("\\"):
        raise PatchAuthorizationError(
            f"Absolute path in diff: {path!r}",
            path=path,
        )
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        raise PatchAuthorizationError(
            f"Windows absolute path in diff: {path!r}",
            path=path,
        )
    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        raise PatchAuthorizationError(
            f"Path traversal in diff: {path!r}",
            path=path,
        )
    return path


_HUNK_HEADER_RE = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@"
)


def _parse_hunk_header(line: str) -> _Hunk:
    m = _HUNK_HEADER_RE.match(line)
    if not m:
        raise PatchValidationError(f"Malformed hunk header: {line!r}")
    old_start = int(m.group(1))
    old_count = int(m.group(2)) if m.group(2) is not None else 1
    new_start = int(m.group(3))
    new_count = int(m.group(4)) if m.group(4) is not None else 1
    if old_count == 0 and new_count == 0:
        raise PatchValidationError(
            f"Empty hunk (old_count=0, new_count=0): {line!r}"
        )
    if old_count < 0 or new_count < 0:
        raise PatchValidationError(
            f"Invalid hunk counts: {line!r}"
        )
    if old_start < 0 or new_start < 0:
        raise PatchValidationError(
            f"Invalid hunk line numbers: {line!r}"
        )
    if old_count > 0 and old_start < 1:
        raise PatchValidationError(
            f"Invalid old_start for non-zero old_count: {line!r}"
        )
    if new_count > 0 and new_start < 1:
        raise PatchValidationError(
            f"Invalid new_start for non-zero new_count: {line!r}"
        )
    return _Hunk(
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        lines=[],
    )


def _validate_file_hunks(hunks: List[_Hunk], path: str) -> None:
    if not hunks:
        raise PatchValidationError(
            f"File header with no hunks for {path!r}"
        )
    for i, h in enumerate(hunks):
        if not h.lines:
            raise PatchValidationError(
                f"Empty hunk in {path!r} at position {i}"
            )
        context_removed = sum(
            1 for hl in h.lines if hl.prefix in (" ", "-")
        )
        context_added = sum(
            1 for hl in h.lines if hl.prefix in (" ", "+")
        )
        if context_removed != h.old_count:
            raise PatchValidationError(
                f"Hunk {i + 1} in {path!r}: old_count={h.old_count} "
                f"but body has {context_removed} context/removed lines"
            )
        if context_added != h.new_count:
            raise PatchValidationError(
                f"Hunk {i + 1} in {path!r}: new_count={h.new_count} "
                f"but body has {context_added} context/added lines"
            )
    for i in range(len(hunks) - 1):
        cur = hunks[i]
        nxt = hunks[i + 1]
        cur_end = cur.old_start + cur.old_count - 1
        if nxt.old_start <= cur_end:
            raise PatchValidationError(
                f"Hunks {i + 1} and {i + 2} in {path!r} overlap "
                f"or are out of order"
            )


class _ParserState(Enum):
    EXPECT_OLD_HEADER = auto()
    EXPECT_NEW_HEADER = auto()
    EXPECT_HUNK = auto()
    IN_HUNK = auto()


def _split_diff_lines(diff_text: str) -> List[str]:
    """Split a diff into lines on ``\\n`` only, keeping line terminators.

    ``str.splitlines`` would additionally split on ``\\v``, ``\\f``,
    ``\\x1c``-``\\x1e``, ``\\x85``, ``\\u2028`` and ``\\u2029`` — characters
    that are legal inside source lines (a form feed in Python source is not
    rare).  Splitting on those boundaries silently corrupts hunk line
    accounting and wrongly rejects otherwise-valid patches.  A ``\\r``
    preceding ``\\n`` stays part of the line and is stripped later as EOL.
    """
    if not diff_text:
        return []
    parts = diff_text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _parse_unified_diff(diff_text: str) -> List[_ParsedFilePatch]:
    if not diff_text or not diff_text.strip():
        raise PatchValidationError("Empty patch")

    if len(diff_text) > _MAX_PATCH_CHARS:
        raise PatchValidationError(
            f"Patch exceeds maximum length of {_MAX_PATCH_CHARS}"
        )

    raw_lines = _split_diff_lines(diff_text)
    file_patches: List[_ParsedFilePatch] = []
    current_path: Optional[str] = None
    current_hunk: Optional[_Hunk] = None
    current_hunks: List[_Hunk] = []
    seen_paths: Set[str] = set()
    state = _ParserState.EXPECT_OLD_HEADER
    hunk_remaining_old = 0
    hunk_remaining_new = 0

    def _flush_hunk() -> None:
        nonlocal current_hunk
        if current_hunk is not None:
            current_hunks.append(current_hunk)
        current_hunk = None

    def _flush_file() -> None:
        nonlocal current_path, current_hunks
        _flush_hunk()
        if current_path is not None:
            if not current_hunks:
                raise PatchValidationError(
                    f"File header with no hunks for {current_path!r}"
                )
            _validate_file_hunks(current_hunks, current_path)
            if current_path in seen_paths:
                raise PatchValidationError(
                    f"Duplicate file section: {current_path!r}"
                )
            file_patches.append(
                _ParsedFilePatch(path=current_path, hunks=list(current_hunks))
            )
            seen_paths.add(current_path)
        current_path = None
        current_hunks = []

    def _hunk_complete() -> bool:
        return hunk_remaining_old <= 0 and hunk_remaining_new <= 0

    def _add_body_line(prefix: str, text: str) -> None:
        nonlocal hunk_remaining_old, hunk_remaining_new
        if prefix == " ":
            hunk_remaining_old -= 1
            hunk_remaining_new -= 1
        elif prefix == "-":
            hunk_remaining_old -= 1
        elif prefix == "+":
            hunk_remaining_new -= 1
        elif prefix == "\\":
            pass
        current_hunk.lines.append(_HunkLine(prefix=prefix, text=text))

    for i, raw_line in enumerate(raw_lines):
        line = raw_line.rstrip("\n\r")

        if state is _ParserState.IN_HUNK:
                if _hunk_complete():
                    if line.startswith("\\ "):
                        text = line[2:].strip()
                        if text != "No newline at end of file":
                            raise PatchValidationError(
                                f"Malformed no-newline marker: {line!r}"
                            )
                        _add_body_line("\\", text)
                    elif line.startswith("@@"):
                        _flush_hunk()
                        current_hunk = _parse_hunk_header(line)
                        hunk_remaining_old = current_hunk.old_count
                        hunk_remaining_new = current_hunk.new_count
                    elif line.startswith("--- "):
                        _flush_hunk()
                        _flush_file()
                        last_old_path = _parse_diff_path(line[4:])
                        state = _ParserState.EXPECT_NEW_HEADER
                    elif line == "":
                        pass
                    elif line.startswith("diff --git"):
                        raise PatchValidationError(
                            "Git metadata lines are not supported"
                        )
                    elif line.startswith(("new file", "deleted file", "old mode", "new mode", "copy", "rename")):
                        raise PatchValidationError(
                            f"Unsupported diff metadata: {line!r}"
                        )
                    elif line.startswith("index ") and ".." in line:
                        pass
                    elif re.match(r"^---$", line):
                        pass
                    elif re.match(r"^\+\+\+$", line):
                        pass
                    else:
                        raise PatchValidationError(
                            f"Extra line after hunk counts satisfied at {i + 1}: {line!r}"
                        )
                else:
                    if line.startswith("\\ "):
                        text = line[2:].strip()
                        if text != "No newline at end of file":
                            raise PatchValidationError(
                                f"Malformed no-newline marker: {line!r}"
                            )
                        _add_body_line("\\", text)
                    elif line.startswith(" "):
                        _add_body_line(" ", line[1:])
                    elif line.startswith("+"):
                        _add_body_line("+", line[1:])
                    elif line.startswith("-"):
                        _add_body_line("-", line[1:])
                    elif line.startswith("--- "):
                        raise PatchValidationError(
                            f"New file header before hunk counts satisfied at {i + 1}"
                        )
                    elif line.startswith("+++ "):
                        raise PatchValidationError(
                            f"New file header before hunk counts satisfied at {i + 1}"
                        )
                    elif line.startswith("@@"):
                        raise PatchValidationError(
                            f"New hunk before previous hunk counts satisfied at {i + 1}"
                        )
                    elif line == "":
                        pass
                    elif line.startswith("diff --git"):
                        raise PatchValidationError(
                            "Git metadata lines are not supported"
                        )
                    elif line.startswith(("new file", "deleted file", "old mode", "new mode", "copy", "rename")):
                        raise PatchValidationError(
                            f"Unsupported diff metadata: {line!r}"
                        )
                    elif line.startswith("index ") and ".." in line:
                        pass
                    elif re.match(r"^---$", line):
                        pass
                    elif re.match(r"^\+\+\+$", line):
                        pass
                    else:
                        raise PatchValidationError(
                            f"Unexpected line in diff at {i + 1}: {line!r}"
                        )
                continue

        if state is _ParserState.EXPECT_OLD_HEADER:
            if line.startswith("--- "):
                _flush_file()
                last_old_path = _parse_diff_path(line[4:])
                state = _ParserState.EXPECT_NEW_HEADER
            elif line == "":
                pass
            elif line.startswith("diff --git"):
                raise PatchValidationError(
                    "Git metadata lines are not supported"
                )
            elif line.startswith(("new file", "deleted file", "old mode", "new mode", "copy", "rename")):
                raise PatchValidationError(
                    f"Unsupported diff metadata: {line!r}"
                )
            elif line.startswith("index ") and ".." in line:
                pass
            elif re.match(r"^---$", line):
                pass
            elif re.match(r"^\+\+\+$", line):
                pass
            else:
                raise PatchValidationError(
                    f"Unexpected line in diff at {i + 1}: {line!r}"
                )
            continue

        if state is _ParserState.EXPECT_NEW_HEADER:
            if line.startswith("--- "):
                raise PatchValidationError(
                    "Duplicate --- before +++"
                )
            elif line.startswith("+++ "):
                new_path = _parse_diff_path(line[4:])
                if new_path != last_old_path:
                    raise PatchValidationError(
                        f"Old and new paths differ: "
                        f"{last_old_path!r} vs {new_path!r}"
                    )
                current_path = new_path
                state = _ParserState.EXPECT_HUNK
            elif line == "":
                pass
            elif line.startswith("diff --git"):
                raise PatchValidationError(
                    "Git metadata lines are not supported"
                )
            elif line.startswith(("new file", "deleted file", "old mode", "new mode", "copy", "rename")):
                raise PatchValidationError(
                    f"Unsupported diff metadata: {line!r}"
                )
            elif line.startswith("index ") and ".." in line:
                pass
            elif re.match(r"^---$", line):
                pass
            elif re.match(r"^\+\+\+$", line):
                pass
            else:
                raise PatchValidationError(
                    f"Unexpected line in diff at {i + 1}: {line!r}"
                )
            continue

        if state is _ParserState.EXPECT_HUNK:
            if line.startswith("--- "):
                _flush_file()
                last_old_path = _parse_diff_path(line[4:])
                state = _ParserState.EXPECT_NEW_HEADER
            elif line.startswith("@@"):
                _flush_hunk()
                current_hunk = _parse_hunk_header(line)
                hunk_remaining_old = current_hunk.old_count
                hunk_remaining_new = current_hunk.new_count
                state = _ParserState.IN_HUNK
            elif line == "":
                pass
            elif line.startswith("diff --git"):
                raise PatchValidationError(
                    "Git metadata lines are not supported"
                )
            elif line.startswith(("new file", "deleted file", "old mode", "new mode", "copy", "rename")):
                raise PatchValidationError(
                    f"Unsupported diff metadata: {line!r}"
                )
            elif line.startswith("index ") and ".." in line:
                pass
            elif re.match(r"^---$", line):
                pass
            elif re.match(r"^\+\+\+$", line):
                pass
            else:
                raise PatchValidationError(
                    f"Unexpected line in diff at {i + 1}: {line!r}"
                )
            continue

    _flush_file()

    if state is _ParserState.EXPECT_NEW_HEADER:
        raise PatchValidationError(
            "Missing +++ after final --- header"
        )

    if not file_patches:
        raise PatchValidationError("No valid patch content found")

    if len(file_patches) > _MAX_PATCH_FILES:
        raise PatchValidationError(
            f"Patch modifies too many files "
            f"({len(file_patches)} > {_MAX_PATCH_FILES})"
        )

    total_hunks = sum(len(p.hunks) for p in file_patches)
    if total_hunks > _MAX_PATCH_HUNKS:
        raise PatchValidationError(
            f"Patch has too many hunks "
            f"({total_hunks} > {_MAX_PATCH_HUNKS})"
        )

    return file_patches
