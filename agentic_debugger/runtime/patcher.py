from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import tokenize
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

from agentic_debugger.runtime.exceptions import (
    PatchApplyError,
    PatchAuthorizationError,
    PatchRevertError,
    PatchStateError,
    PatchValidationError,
)
from agentic_debugger.runtime.workspace import TaskWorkspace

_MAX_PATCH_CHARS = 100_000
_MAX_PATCH_FILES = 10
_MAX_PATCH_HUNKS = 100
_TEMP_PREFIX = ".agentic_debugger_tmp_"

# Bounded context-location fuzz for hunk application (lines).  When the
# declared hunk start position does not match the file exactly, the applier
# searches this many lines before/after the declared position for the first
# position where EVERY context/removed line matches exactly.  This is a
# deterministic mechanical accommodation of imprecise hunk headers (analogous
# to GNU patch fuzz / ``git apply -C``); content matching remains exact and
# the displacement is recorded per hunk in the result.
_CONTEXT_FUZZ = 10

class _PolicyKind(Enum):
    EXACT_FILE = auto()
    DIRECTORY = auto()


@dataclass(frozen=True)
class _PolicyRule:
    path: str
    kind: _PolicyKind


_MANDATORY_DENIED_RULES: List[_PolicyRule] = [
    _PolicyRule(path="tests", kind=_PolicyKind.DIRECTORY),
    _PolicyRule(path="task.json", kind=_PolicyKind.EXACT_FILE),
]


@dataclass(frozen=True)
class PatchFileChange:
    path: str
    hunks_applied: int

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "hunks_applied": self.hunks_applied,
        }


@dataclass(frozen=True)
class PatchApplyResult:
    success: bool
    changed_files: List[PatchFileChange]
    hunk_count: int
    before_sha256: Dict[str, str]
    after_sha256: Dict[str, str]
    bytes_before: Dict[str, int]
    bytes_after: Dict[str, int]
    error: Optional[str]
    # Per-hunk context-location adjustments applied by bounded fuzz:
    # (1-based hunk index, line displacement from the declared position).
    # Empty when every hunk applied at its declared position.
    hunk_adjustments: Tuple[Tuple[int, int], ...] = ()

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "changed_files": [c.to_mapping() for c in self.changed_files],
            "hunk_count": self.hunk_count,
            "before_sha256": dict(self.before_sha256),
            "after_sha256": dict(self.after_sha256),
            "bytes_before": dict(self.bytes_before),
            "bytes_after": dict(self.bytes_after),
            "error": self.error,
            "hunk_adjustments": [list(item) for item in self.hunk_adjustments],
        }


@dataclass(frozen=True)
class CanonicalPatchArtifact:
    """A deterministic Git serialization of an accepted workspace state."""

    patch: str
    changed_paths: Tuple[str, ...]
    before_sha256: Dict[str, str]
    after_sha256: Dict[str, str]
    raw_patch_sha256: str
    canonical_patch_sha256: str
    raw_apply: PatchApplyResult
    semantic_equivalent: bool

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "artifact_name": "candidate-official.patch",
            "patch": self.patch,
            "changed_paths": list(self.changed_paths),
            "before_sha256": dict(self.before_sha256),
            "after_sha256": dict(self.after_sha256),
            "raw_patch_sha256": self.raw_patch_sha256,
            "canonical_patch_sha256": self.canonical_patch_sha256,
            "raw_apply": self.raw_apply.to_mapping(),
            "semantic_equivalent": self.semantic_equivalent,
        }


@dataclass(frozen=True)
class PatchSnapshot:
    files: Dict[str, bytes]
    before_hashes: Dict[str, str]
    after_hashes: Dict[str, str]


@dataclass(frozen=True)
class SyntaxFileResult:
    path: str
    success: bool
    error_type: Optional[str]
    message: Optional[str]
    line: Optional[int]
    column: Optional[int]

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "success": self.success,
            "error_type": self.error_type,
            "message": self.message,
            "line": self.line,
            "column": self.column,
        }


@dataclass(frozen=True)
class SyntaxCheckResult:
    results: List[SyntaxFileResult]
    all_passed: bool

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "results": [r.to_mapping() for r in self.results],
            "all_passed": self.all_passed,
        }


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
        raise PatchValidationError(
            f"NUL character in diff path: {path!r}"
        )
    if path.startswith("/") or path.startswith("\\"):
        raise PatchValidationError(
            f"Absolute path in diff: {path!r}"
        )
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        raise PatchValidationError(
            f"Windows absolute path in diff: {path!r}"
        )
    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        raise PatchValidationError(
            f"Path traversal in diff: {path!r}"
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


def _parse_unified_diff(diff_text: str) -> List[_ParsedFilePatch]:
    if not diff_text or not diff_text.strip():
        raise PatchValidationError("Empty patch")

    if len(diff_text) > _MAX_PATCH_CHARS:
        raise PatchValidationError(
            f"Patch exceeds maximum length of {_MAX_PATCH_CHARS}"
        )

    raw_lines = diff_text.splitlines(True)
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


def _detect_line_ending(lines: List[str]) -> str:
    crlf = 0
    lf = 0
    cr = 0
    for l in lines:
        if l.endswith("\r\n"):
            crlf += 1
        elif l.endswith("\n"):
            lf += 1
        elif l.endswith("\r"):
            cr += 1
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr >= lf and cr >= crlf and cr > 0:
        return "\r"
    return "\n"


def _hunk_consumer_lines(hunk: _Hunk) -> List[Tuple[str, str]]:
    """The context/removed lines that must exist verbatim in the file."""
    return [(hl.prefix, hl.text) for hl in hunk.lines if hl.prefix in (" ", "-")]


def _find_hunk_anchor(
    lines: List[str],
    consumer: List[Tuple[str, str]],
    requested_idx: int,
    fuzz: int,
) -> Optional[int]:
    """Locate the exact position of a hunk's consumer lines.

    Preference order is deterministic: the declared position first, then
    positions moving outward by one line at a time within ``fuzz`` lines of
    the declared position.  Every consumer line must match exactly
    (``rstrip``-normalized); any content mismatch means no anchor.
    """
    count = len(consumer)
    if count == 0:
        return max(0, min(requested_idx, len(lines)))
    lo = max(0, requested_idx - fuzz)
    hi = min(len(lines) - count, requested_idx + fuzz)
    if lo > hi:
        return None
    offsets = [0]
    for distance in range(1, fuzz + 1):
        if requested_idx - distance >= lo:
            offsets.append(-distance)
        if requested_idx + distance <= hi:
            offsets.append(distance)
    for offset in offsets:
        start = requested_idx + offset
        if start < lo or start > hi:
            continue
        matches = True
        for i, (prefix, text) in enumerate(consumer):
            actual = lines[start + i].rstrip("\n\r")
            if actual != text:
                matches = False
                break
        if matches:
            return start
    return None


def _apply_hunks(
    original_text: str, hunks: List[_Hunk]
) -> Tuple[str, Tuple[Tuple[int, int], ...]]:
    lines = original_text.splitlines(True)
    dominant_eol = _detect_line_ending(lines)

    delta = 0
    adjustments: List[Tuple[int, int]] = []

    for hunk_idx, hunk in enumerate(hunks):
        if hunk.old_count == 0:
            adjusted_idx = hunk.old_start + delta
        else:
            adjusted_idx = hunk.old_start - 1 + delta

        consumer = _hunk_consumer_lines(hunk)
        consumer_count = len(consumer)

        # Bounded deterministic fuzz: the declared position may be imprecise
        # (imprecise hunk headers are a mechanical formatting defect, not a
        # semantic one).  The anchor search clamps to the file bounds, so a
        # declared position outside the file is still accommodated within the
        # bounded window; content matching remains exact.
        anchor = _find_hunk_anchor(
            lines, consumer, adjusted_idx, _CONTEXT_FUZZ
        )
        if anchor is None:
            raise PatchApplyError(
                f"Context mismatch in hunk {hunk_idx + 1} "
                f"at original line {hunk.old_start}: "
                f"expected {consumer[0][1] if consumer else '<insertion>'!r} "
                f"(bounded fuzz window {_CONTEXT_FUZZ})"
            )
        if anchor != adjusted_idx:
            adjustments.append((hunk_idx + 1, anchor - adjusted_idx))

        check_idx = 0
        for hl in hunk.lines:
            if hl.prefix in (" ", "-"):
                actual = lines[anchor + check_idx].rstrip("\n\r")
                if actual != hl.text:
                    raise PatchApplyError(
                        f"Context mismatch in hunk {hunk_idx + 1} "
                        f"at original line {hunk.old_start + check_idx}: "
                        f"expected {hl.text!r}, got {actual!r}"
                    )
                check_idx += 1

        pre = lines[:anchor]
        post = lines[anchor + consumer_count :]

        new_middle: List[str] = []
        orig_offset = 0
        last_non_marker_prefix: Optional[str] = None
        for hl in hunk.lines:
            if hl.prefix == " ":
                new_middle.append(lines[anchor + orig_offset])
                orig_offset += 1
                last_non_marker_prefix = " "
            elif hl.prefix == "-":
                orig_offset += 1
                last_non_marker_prefix = "-"
            elif hl.prefix == "+":
                new_middle.append(hl.text + dominant_eol)
                last_non_marker_prefix = "+"
            elif hl.prefix == "\\":
                if last_non_marker_prefix == "+" and new_middle:
                    new_middle[-1] = new_middle[-1].rstrip("\n\r")

        lines = pre + new_middle + post
        # Content-length change plus any context-location displacement.
        delta += (hunk.new_count - hunk.old_count) + (anchor - adjusted_idx)

    return "".join(lines), tuple(adjustments)


def _detect_encoding(file_path: str) -> Tuple[str, bool]:
    with open(file_path, "rb") as f:
        raw = f.read(4096)
    has_bom = raw[:3] == b"\xef\xbb\xbf"
    raw_lines = raw.split(b"\n")
    line_iter = iter(raw_lines[:2])

    def _readline() -> bytes:
        try:
            nxt = next(line_iter)
            if nxt:
                return nxt + b"\n"
            return b"\n"
        except StopIteration:
            return b""

    encoding, _ = tokenize.detect_encoding(_readline)
    return encoding, has_bom


def _write_temp_file(path: str, content: bytes) -> Tuple[str, Optional[int]]:
    dirname = os.path.dirname(path)
    target_mode: Optional[int] = None
    try:
        orig_st = os.stat(path)
        if stat.S_ISREG(orig_st.st_mode):
            target_mode = stat.S_IMODE(orig_st.st_mode)
    except OSError:
        pass

    fd, tmp_path = tempfile.mkstemp(
        prefix=_TEMP_PREFIX,
        dir=dirname,
    )
    try:
        if target_mode is not None:
            try:
                os.chmod(tmp_path, target_mode)
            except OSError:
                pass

        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        fd = None
    except Exception:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    return tmp_path, target_mode


def _replace_temp_file(tmp_path: str, target: str) -> None:
    try:
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise


def _verify_file_hash(target: str, expected_content: bytes) -> None:
    expected_hash = hashlib.sha256(expected_content).hexdigest()
    with open(target, "rb") as f:
        actual_hash = hashlib.sha256(f.read()).hexdigest()
    if actual_hash != expected_hash:
        raise PatchApplyError(
            f"Hash mismatch for {target!r}: "
            f"expected {expected_hash}, got {actual_hash}"
        )


def _normalize_path(p: str) -> str:
    return p.replace("\\", "/").rstrip("/")


def _classify_policy_entry(
    workspace: TaskWorkspace, entry: str
) -> _PolicyRule:
    npath = _normalize_path(entry)
    if not npath:
        raise PatchAuthorizationError(f"Empty policy entry: {entry!r}")
    if npath.startswith("/") or ".." in npath.split("/"):
        raise PatchAuthorizationError(
            f"Unsafe policy entry: {entry!r}"
        )
    try:
        resolved = workspace.resolve_path(npath, must_exist=True)
    except Exception as e:
        raise PatchAuthorizationError(
            f"Invalid policy entry {entry!r}: {e}"
        ) from e
    real = os.path.normpath(resolved)
    if not os.path.exists(real):
        raise PatchAuthorizationError(
            f"Policy entry does not exist: {entry!r}"
        )
    if os.path.islink(real):
        raise PatchAuthorizationError(
            f"Policy entry is a symlink: {entry!r}"
        )
    if os.path.isfile(real):
        return _PolicyRule(path=npath, kind=_PolicyKind.EXACT_FILE)
    if os.path.isdir(real):
        return _PolicyRule(path=npath, kind=_PolicyKind.DIRECTORY)
    raise PatchAuthorizationError(
        f"Policy entry is not a regular file or directory: {entry!r}"
    )


def _check_official_patch_compatibility(workspace_root: str, diff_text: str) -> None:
    """Require the same direct Git patch semantics used by the official evaluator.

    The repository PatchManager intentionally supports bounded context fuzz for
    ordinary debugger workflows.  The official SWE-rebench evaluator does not:
    after its optional three-way attempt, it falls back to direct ``git apply``.
    Live treatments that will be judged by that evaluator must therefore reject
    a candidate before the permissive local applier mutates the workspace.
    ``--check`` keeps this probe side-effect free.
    """

    git = shutil.which("git")
    if git is None:
        raise PatchValidationError(
            "official patch compatibility check unavailable: git executable not found"
        )

    probe_root: Optional[str] = None
    patch_path: Optional[str] = None
    try:
        # TaskWorkspace intentionally does not require a Git checkout.  Build
        # a disposable index so the check has the same direct-application
        # semantics as the official evaluator without mutating the live
        # workspace or creating repository state in it.
        probe_root = tempfile.mkdtemp(
            prefix=f"{_TEMP_PREFIX}git-check-",
            dir=os.path.dirname(workspace_root),
        )
        shutil.copytree(
            workspace_root,
            probe_root,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", f"{_TEMP_PREFIX}*"
            ),
        )
        initialized = subprocess.run(
            [git, "init", "--quiet"],
            cwd=probe_root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if initialized.returncode != 0:
            diagnostic = (initialized.stderr or initialized.stdout or "git init failed").strip()
            raise PatchValidationError(
                f"official patch compatibility check could not initialize probe: {diagnostic}"
            )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=_TEMP_PREFIX,
            suffix=".patch",
            dir=probe_root,
            delete=False,
        ) as handle:
            # The official operator adds this harmless serialization newline
            # before invoking its evaluator; mirror that normalization here.
            handle.write(diff_text if diff_text.endswith("\n") else diff_text + "\n")
            patch_path = handle.name
        checked = subprocess.run(
            [
                git,
                "apply",
                "--check",
                "--recount",
                "--ignore-space-change",
                "--whitespace=nowarn",
                patch_path,
            ],
            cwd=probe_root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PatchValidationError(
            f"official patch compatibility check failed to run: {exc}"
        ) from exc
    finally:
        if patch_path is not None:
            try:
                os.unlink(patch_path)
            except OSError:
                pass
        if probe_root is not None:
            def _remove_readonly(_function, path, _exc_info):
                try:
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                    _function(path)
                except OSError:
                    pass

            shutil.rmtree(probe_root, onerror=_remove_readonly)

    if checked.returncode != 0:
        diagnostic = (checked.stderr or checked.stdout or "git apply rejected the patch").strip()
        raise PatchValidationError(
            f"patch is not compatible with official git apply: {diagnostic}"
        )


class PatchManager:
    def __init__(
        self,
        workspace: TaskWorkspace,
        allowed_paths: List[str],
        denied_paths: List[str],
        *,
        official_patch_compatibility: bool = False,
    ) -> None:
        self._workspace = workspace
        self._official_patch_compatibility = official_patch_compatibility

        denied_rules: List[_PolicyRule] = []
        for p in denied_paths:
            if not p.strip():
                raise PatchAuthorizationError("Empty policy entry")
            if p == "tests" or p == "task.json":
                continue
            denied_rules.append(_classify_policy_entry(workspace, p))

        denied_rules.extend(_MANDATORY_DENIED_RULES)

        allowed_rules: List[_PolicyRule] = []
        for p in allowed_paths:
            if not p.strip():
                raise PatchAuthorizationError("Empty policy entry")
            allowed_rules.append(_classify_policy_entry(workspace, p))

        self._denied = denied_rules
        self._allowed = allowed_rules
        self._snapshot: Optional[PatchSnapshot] = None

    @property
    def has_active_patch(self) -> bool:
        return self._snapshot is not None

    def _authorize_path(self, path: str) -> None:
        npath = _normalize_path(path)

        for rule in self._denied:
            if rule.kind is _PolicyKind.EXACT_FILE:
                if npath == rule.path:
                    raise PatchAuthorizationError(
                        f"Path is denied: {path!r}"
                    )
            else:
                if npath == rule.path or npath.startswith(rule.path + "/"):
                    raise PatchAuthorizationError(
                        f"Path is inside denied directory: {path!r}"
                    )

        allowed = False
        for rule in self._allowed:
            if rule.kind is _PolicyKind.EXACT_FILE:
                if npath == rule.path:
                    allowed = True
                    break
            else:
                if npath == rule.path or npath.startswith(rule.path + "/"):
                    allowed = True
                    break

        if not allowed:
            raise PatchAuthorizationError(
                f"Path is not allowed: {path!r}"
            )

    def apply_patch(self, diff_text: str) -> PatchApplyResult:
        if self._snapshot is not None:
            raise PatchStateError(
                "Active patch exists; must revert before applying a new one"
            )

        file_patches = _parse_unified_diff(diff_text)

        for fp in file_patches:
            self._authorize_path(fp.path)

        if self._official_patch_compatibility:
            _check_official_patch_compatibility(self._workspace.root, diff_text)

        originals: Dict[str, bytes] = {}
        encoding_map: Dict[str, str] = {}
        for fp in file_patches:
            resolved = self._workspace.resolve_path(
                fp.path, must_exist=True
            )
            if not os.path.isfile(resolved):
                raise PatchApplyError(
                    f"Path is not a regular file: {fp.path!r}"
                )
            if os.path.islink(resolved):
                raise PatchApplyError(
                    f"Path is a symlink: {fp.path!r}"
                )
            if fp.path.endswith(".py"):
                encoding_map[fp.path], _ = _detect_encoding(resolved)
            else:
                raw = open(resolved, "rb").read(4096)
                if raw[:3] == b"\xef\xbb\xbf":
                    encoding_map[fp.path] = "utf-8-sig"
                else:
                    encoding_map[fp.path] = "utf-8"
            with open(resolved, "rb") as f:
                originals[fp.path] = f.read()

        new_contents: Dict[str, bytes] = {}
        hunk_adjustments: List[Tuple[int, int]] = []
        for fp in file_patches:
            encoding = encoding_map[fp.path]
            original_bytes = originals[fp.path]
            try:
                original_text = original_bytes.decode(encoding)
            except (UnicodeDecodeError, LookupError) as e:
                raise PatchApplyError(
                    f"Cannot decode {fp.path!r} with encoding {encoding!r}: {e}"
                ) from e
            new_text, adjustments = _apply_hunks(original_text, fp.hunks)
            hunk_adjustments.extend(
                (fp.path, hunk_idx, displacement)
                for hunk_idx, displacement in adjustments
            )
            try:
                new_contents[fp.path] = new_text.encode(encoding)
            except (UnicodeEncodeError, LookupError) as e:
                raise PatchApplyError(
                    f"Cannot encode patched {fp.path!r} with {encoding!r}: {e}"
                ) from e

        before_hashes: Dict[str, str] = {}
        after_hashes: Dict[str, str] = {}
        for p in originals:
            before_hashes[p] = hashlib.sha256(
                originals[p]
            ).hexdigest()
            after_hashes[p] = hashlib.sha256(
                new_contents[p]
            ).hexdigest()

        written: List[str] = []
        replaced: List[str] = []
        rollback_ok = True
        try:
            for path in sorted(new_contents.keys()):
                resolved = self._workspace.resolve_path(path)
                tmp_path, _ = _write_temp_file(resolved, new_contents[path])
                written.append(path)
                _replace_temp_file(tmp_path, resolved)
                replaced.append(path)
                _verify_file_hash(resolved, new_contents[path])
        except Exception as e:
            for path in reversed(written):
                if path in replaced:
                    try:
                        resolved = self._workspace.resolve_path(path)
                        tmp_path, _ = _write_temp_file(
                            resolved, originals[path]
                        )
                        _replace_temp_file(tmp_path, resolved)
                        _verify_file_hash(resolved, originals[path])
                    except Exception:
                        rollback_ok = False
                else:
                    pass
            if rollback_ok:
                raise PatchApplyError(
                    f"Patch write failed, all changes rolled back: {e}"
                ) from e
            else:
                raise PatchApplyError(
                    f"Patch write failed, partial rollback completed. "
                    f"Rollback of some files also failed: {e}"
                ) from e

        self._snapshot = PatchSnapshot(
            files=originals,
            before_hashes=before_hashes,
            after_hashes=after_hashes,
        )

        changed = [
            PatchFileChange(path=fp.path, hunks_applied=len(fp.hunks))
            for fp in file_patches
        ]
        total_hunks = sum(len(fp.hunks) for fp in file_patches)

        return PatchApplyResult(
            success=True,
            changed_files=changed,
            hunk_count=total_hunks,
            before_sha256=before_hashes,
            after_sha256=after_hashes,
            bytes_before={k: len(v) for k, v in originals.items()},
            bytes_after={k: len(v) for k, v in new_contents.items()},
            error=None,
            hunk_adjustments=tuple(hunk_adjustments),
        )

    def revert_patch(self) -> PatchApplyResult:
        if self._snapshot is None:
            raise PatchStateError("No active patch to revert")

        snapshot = self._snapshot

        verify_hashes: Dict[str, str] = {}
        for path in snapshot.files:
            resolved = self._workspace.resolve_path(path, must_exist=True)
            with open(resolved, "rb") as f:
                current = f.read()
            verify_hashes[path] = hashlib.sha256(current).hexdigest()

        written: List[str] = []
        replaced: List[str] = []
        rollback_ok = True
        try:
            for path in sorted(snapshot.files.keys()):
                resolved = self._workspace.resolve_path(path)
                tmp_path, _ = _write_temp_file(
                    resolved, snapshot.files[path]
                )
                written.append(path)
                _replace_temp_file(tmp_path, resolved)
                replaced.append(path)
                _verify_file_hash(resolved, snapshot.files[path])
        except Exception as e:
            for path in reversed(written):
                if path in replaced:
                    try:
                        resolved = self._workspace.resolve_path(path)
                        tmp_path, _ = _write_temp_file(
                            resolved, snapshot.files[path]
                        )
                        _replace_temp_file(tmp_path, resolved)
                        _verify_file_hash(resolved, snapshot.files[path])
                    except Exception:
                        rollback_ok = False
            raise PatchRevertError(
                f"Revert write failed, snapshot preserved: {e}"
            ) from e

        restored_hashes: Dict[str, str] = {}
        for path in snapshot.files:
            resolved = self._workspace.resolve_path(path, must_exist=True)
            with open(resolved, "rb") as f:
                restored = f.read()
            restored_hashes[path] = hashlib.sha256(restored).hexdigest()

        for path, expected in snapshot.before_hashes.items():
            actual = restored_hashes.get(path)
            if actual != expected:
                raise PatchRevertError(
                    f"Hash mismatch after revert for {path!r}: "
                    f"expected {expected}, got {actual}"
                )

        self._snapshot = None

        return PatchApplyResult(
            success=True,
            changed_files=[
                PatchFileChange(path=p, hunks_applied=0)
                for p in snapshot.files
            ],
            hunk_count=0,
            before_sha256=dict(verify_hashes),
            after_sha256=dict(restored_hashes),
            bytes_before={
                k: len(v) for k, v in snapshot.files.items()
            },
            bytes_after={
                k: len(v) for k, v in snapshot.files.items()
            },
            error=None,
            hunk_adjustments=(),
        )

    def syntax_check(
        self, paths: Optional[List[str]] = None
    ) -> SyntaxCheckResult:
        if paths is None:
            if self._snapshot is None:
                raise PatchStateError(
                    "No active patch; explicit paths required"
                )
            paths = sorted(self._snapshot.files.keys())

        results: List[SyntaxFileResult] = []
        all_passed = True

        for path in paths:
            resolved = self._workspace.resolve_path(path, must_exist=True)
            if not os.path.isfile(resolved):
                raise PatchApplyError(
                    f"Path is not a regular file: {path!r}"
                )
            with open(resolved, "rb") as f:
                source = f.read()

            if not path.endswith(".py"):
                results.append(
                    SyntaxFileResult(
                        path=path,
                        success=True,
                        error_type=None,
                        message="Non-Python file skipped",
                        line=None,
                        column=None,
                    )
                )
                continue

            result = _check_python_syntax(path, source)
            if not result.success:
                all_passed = False
            results.append(result)

        return SyntaxCheckResult(
            results=results, all_passed=all_passed
        )


def _check_python_syntax(
    path: str, source: bytes
) -> SyntaxFileResult:
    try:
        compile(source, path, "exec", dont_inherit=True)
        return SyntaxFileResult(
            path=path,
            success=True,
            error_type=None,
            message=None,
            line=None,
            column=None,
        )
    except SyntaxError as e:
        return SyntaxFileResult(
            path=path,
            success=False,
            error_type="SyntaxError",
            message=str(e),
            line=e.lineno,
            column=e.offset,
        )
    except ValueError as e:
        return SyntaxFileResult(
            path=path,
            success=False,
            error_type="ValueError",
            message=str(e),
            line=None,
            column=None,
        )


def _snapshot_workspace_files(root: str) -> Dict[str, bytes]:
    """Return a complete, deterministic snapshot of regular workspace files."""

    snapshot: Dict[str, bytes] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            path = os.path.join(directory, name)
            if os.path.islink(path):
                raise PatchValidationError(
                    f"workspace snapshot encountered a symlink: {path!r}"
                )
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            with open(path, "rb") as handle:
                snapshot[relative] = handle.read()
    return snapshot


def _copy_workspace_contents(source: str, destination: str) -> None:
    """Copy a symlink-free workspace tree without copying repository metadata."""

    for name in os.listdir(destination):
        if name == ".git":
            continue
        target = os.path.join(destination, name)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.unlink(target)
    for name in os.listdir(source):
        source_path = os.path.join(source, name)
        destination_path = os.path.join(destination, name)
        if os.path.islink(source_path):
            raise PatchValidationError(
                f"workspace canonicalization encountered a symlink: {source_path!r}"
            )
        if os.path.isdir(source_path):
            shutil.copytree(source_path, destination_path, symlinks=False)
        else:
            shutil.copy2(source_path, destination_path)
            # copy2 preserves the source mtime.  Refreshing the worktree
            # timestamp ensures Git does not trust a stale index stat cache
            # when a same-sized file changed in the disposable tree.
            os.utime(destination_path, None)


def _run_git_checked(argv: List[str], *, cwd: str, timeout: float = 30.0) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PatchValidationError(
            f"canonical Git operation could not run: {exc}"
        ) from exc
    return result


def materialize_and_canonicalize_patch(
    pristine_source: str,
    raw_patch: str,
    allowed_paths: List[str],
    denied_paths: List[str],
    *,
    parent_dir: Optional[str] = None,
) -> CanonicalPatchArtifact:
    """Materialize a raw candidate, then prove its canonical Git equivalent.

    The raw text is never rewritten.  A tolerant ``PatchManager`` is the only
    component allowed to interpret it; Git is used solely to serialize and
    strictly re-apply the resulting workspace state.
    """

    if type(raw_patch) is not str or not raw_patch.strip():
        raise PatchValidationError("raw candidate patch must be a non-empty string")
    if "\x00" in raw_patch:
        raise PatchValidationError("raw candidate patch contains a NUL byte")
    if parent_dir is not None and not os.path.isdir(parent_dir):
        raise PatchValidationError("canonicalization parent directory is unavailable")

    with tempfile.TemporaryDirectory(
        prefix=f"{_TEMP_PREFIX}canonical-",
        dir=parent_dir,
    ) as temporary:
        raw_workspace = TaskWorkspace(pristine_source, parent_dir=temporary)
        strict_workspace = TaskWorkspace(pristine_source, parent_dir=temporary)
        manager = PatchManager(
            raw_workspace,
            allowed_paths,
            denied_paths,
            official_patch_compatibility=False,
        )
        applied = manager.apply_patch(raw_patch)
        before = _snapshot_workspace_files(strict_workspace.root)
        materialized = _snapshot_workspace_files(raw_workspace.root)
        changed_paths = tuple(
            sorted(
                path
                for path in set(before) | set(materialized)
                if before.get(path) != materialized.get(path)
            )
        )
        if not changed_paths:
            raise PatchValidationError("raw candidate produced no material workspace delta")
        if set(changed_paths) != {item.path for item in applied.changed_files}:
            raise PatchValidationError(
                "raw candidate changed-path evidence disagrees with the workspace delta"
            )
        for path in changed_paths:
            manager._authorize_path(path)

        repo_root = os.path.join(temporary, "canonical-git-repo")
        shutil.copytree(strict_workspace.root, repo_root)
        initialized = _run_git_checked(["git", "init", "--quiet"], cwd=repo_root)
        if initialized.returncode != 0:
            raise PatchValidationError(
                "canonical Git repository initialization failed: "
                + (initialized.stderr or initialized.stdout).decode("utf-8", "replace").strip()
            )
        for key, value in (
            ("user.name", "agentic-debugger"),
            ("user.email", "agentic-debugger@localhost"),
            ("core.autocrlf", "false"),
        ):
            configured = _run_git_checked(["git", "config", key, value], cwd=repo_root)
            if configured.returncode != 0:
                raise PatchValidationError("canonical Git identity configuration failed")
        added = _run_git_checked(["git", "add", "--all"], cwd=repo_root)
        if added.returncode != 0:
            detail = (added.stderr or added.stdout).decode("utf-8", "replace").strip()
            raise PatchValidationError(
                "canonical Git baseline indexing failed" + (f": {detail}" if detail else "")
            )
        committed = _run_git_checked(
            ["git", "commit", "--quiet", "-m", "pristine baseline"],
            cwd=repo_root,
        )
        if committed.returncode != 0:
            raise PatchValidationError("canonical Git baseline commit failed")
        _copy_workspace_contents(raw_workspace.root, repo_root)
        diff_result = _run_git_checked(
            [
                "git", "diff", "--binary", "--full-index", "--no-ext-diff",
                "--no-renames", "--unified=3", "--src-prefix=a/", "--dst-prefix=b/", "--",
            ],
            cwd=repo_root,
        )
        if diff_result.returncode not in (0, 1):
            raise PatchValidationError(
                "canonical Git diff failed: "
                + (diff_result.stderr or diff_result.stdout).decode("utf-8", "replace").strip()
            )
        try:
            canonical = diff_result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchValidationError("canonical Git diff is not UTF-8 text") from exc
        if not canonical.strip():
            raise PatchValidationError("canonical Git diff is empty")

        patch_path = os.path.join(temporary, "candidate-official.patch")
        with open(patch_path, "wb") as handle:
            handle.write(canonical.encode("utf-8"))
        checked = _run_git_checked(
            ["git", "-c", "core.autocrlf=false", "apply", "--check", patch_path],
            cwd=str(strict_workspace.root),
        )
        if checked.returncode != 0:
            raise PatchValidationError(
                "canonical patch failed strict Git apply --check: "
                + (checked.stderr or checked.stdout).decode("utf-8", "replace").strip()
            )
        applied_strictly = _run_git_checked(
            ["git", "-c", "core.autocrlf=false", "apply", patch_path],
            cwd=str(strict_workspace.root),
        )
        if applied_strictly.returncode != 0:
            raise PatchValidationError(
                "canonical patch failed strict Git apply: "
                + (applied_strictly.stderr or applied_strictly.stdout).decode("utf-8", "replace").strip()
            )
        strict_after = _snapshot_workspace_files(strict_workspace.root)
        if strict_after != materialized:
            raise PatchValidationError(
                "canonical patch is not byte-for-byte equivalent to raw materialization"
            )
        canonical_paths = _run_git_checked(
            ["git", "diff", "--name-only", "--no-renames", "--"],
            cwd=repo_root,
        )
        if canonical_paths.returncode != 0:
            raise PatchValidationError("canonical changed-path inspection failed")
        serialized_paths = tuple(
            sorted(
                line.strip()
                for line in canonical_paths.stdout.decode("utf-8").splitlines()
                if line.strip()
            )
        )
        if serialized_paths != changed_paths:
            raise PatchValidationError(
                "canonical patch changed-path set differs from raw materialization"
            )
        before_hashes = {
            path: hashlib.sha256(before[path]).hexdigest() for path in changed_paths
        }
        after_hashes = {
            path: hashlib.sha256(materialized[path]).hexdigest() for path in changed_paths
        }
        return CanonicalPatchArtifact(
            patch=canonical,
            changed_paths=changed_paths,
            before_sha256=before_hashes,
            after_sha256=after_hashes,
            raw_patch_sha256=hashlib.sha256(raw_patch.encode("utf-8")).hexdigest(),
            canonical_patch_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            raw_apply=applied,
            semantic_equivalent=True,
        )
