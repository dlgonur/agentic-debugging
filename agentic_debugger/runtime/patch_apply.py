"""Hunk matching/application primitives and atomic patch file I/O.

This module owns the apply layer of the patch runtime: line-ending
detection, exact consumer-line anchoring with bounded deterministic fuzz,
source-window formatting for bounded recoverable feedback, hunk
application with per-hunk adjustment accounting, encoding detection, and
the atomic temp-file write/replace/verify primitives with rollback-safe
cleanup semantics.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import tokenize
from typing import List, Optional, Tuple

from agentic_debugger.runtime.exceptions import PatchApplyError
from agentic_debugger.runtime.patch_diff import _Hunk, _split_diff_lines

_TEMP_PREFIX = ".agentic_debugger_tmp_"

_CONTEXT_FUZZ = 10


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


def _format_source_window(
    lines: List[str],
    target_line: int,
    count: int = 1,
    context_lines: int = 3,
) -> str:
    """Format a bounded window of source lines with 1-based line numbers."""
    if not lines:
        return "<empty file>"
    lo = max(0, target_line - 1 - context_lines)
    hi = min(len(lines), target_line - 1 + max(count, 1) + context_lines)
    formatted = []
    width = max(len(str(hi)), 2)
    for idx in range(lo, hi):
        line_num = idx + 1
        line_text = lines[idx].rstrip("\r\n")
        formatted.append(f"{line_num:>{width}} | {line_text}")
    return "\n".join(formatted)


def _apply_hunks(
    original_text: str, hunks: List[_Hunk], path: str = ""
) -> Tuple[str, Tuple[Tuple[int, int], ...]]:
    # Same LF-only splitting as the parser: a form feed inside a source line
    # is line content, not a boundary.
    lines = _split_diff_lines(original_text)
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
            consumer_lines = [f"{prefix}{text}" for prefix, text in consumer]
            expected_sample = (
                "\n".join(consumer_lines)
                if consumer_lines
                else (f"-{hunk.lines[0].text}" if hunk.lines else "<insertion>")
            )
            target_line = max(1, min(hunk.old_start, len(lines))) if lines else 1
            source_window = _format_source_window(lines, target_line, hunk.old_count)
            target_desc = f"at {path} " if path else ""
            msg = (
                f"Patch application failed. Context mismatch {target_desc}around line {hunk.old_start} "
                f"(hunk {hunk_idx + 1}): expected context matching\n"
                f"{expected_sample}\n"
                f"Current source around line {hunk.old_start}:\n"
                f"{source_window}\n"
                f"Revise the patch against the current source and try again."
            )
            raise PatchApplyError(
                msg,
                path=path,
                hunk_index=hunk_idx + 1,
                line_number=hunk.old_start,
                expected=expected_sample,
                actual=None,
                current_source_window=source_window,
                error_kind="context_mismatch",
                recoverable=True,
            )
        if anchor != adjusted_idx:
            adjustments.append((hunk_idx + 1, anchor - adjusted_idx))

        check_idx = 0
        for hl in hunk.lines:
            if hl.prefix in (" ", "-"):
                actual = lines[anchor + check_idx].rstrip("\n\r")
                if actual != hl.text:
                    mismatch_line = anchor + check_idx + 1
                    source_window = _format_source_window(lines, mismatch_line, 1)
                    target_desc = f"at {path} " if path else ""
                    msg = (
                        f"Patch application failed. Context mismatch {target_desc}at line {mismatch_line} "
                        f"(hunk {hunk_idx + 1}): expected {hl.text!r}, got {actual!r}.\n"
                        f"Current source around line {mismatch_line}:\n"
                        f"{source_window}\n"
                        f"Revise the patch against the current source and try again."
                    )
                    raise PatchApplyError(
                        msg,
                        path=path,
                        hunk_index=hunk_idx + 1,
                        line_number=mismatch_line,
                        expected=hl.text,
                        actual=actual,
                        current_source_window=source_window,
                        error_kind="context_mismatch",
                        recoverable=True,
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
