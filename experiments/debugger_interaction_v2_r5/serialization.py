"""R5 — fail-closed unified-diff hunk-count normalization (metadata-only).

Verbatim copy of the accepted R3.2 serialization policy (immutable R3 source,
no behavioral change).

The R3.1 live probe proved the model-authored semantic repair B is correct:
its only defect is unified-diff hunk metadata — the `@@ ... @@` header
declares `old_count=7/new_count=7` while the hunk body carries 6 old-side
(5 context + 1 removed) and 6 new-side (5 context + 1 added) lines.

This module is a strictly metadata-only serialization normalizer:

- preserves file paths, old_start, new_start, hunk section/function suffix,
  every body line (context/removed/added), hunk order, file order, and
  `\\ No newline at end of file` markers exactly;
- recomputes ONLY `old_count` and `new_count` from the hunk body and rewrites
  only those two header fields when they are incorrect;
- never changes start positions, body lines, +/- semantics, paths, syntax,
  or code content;
- never consults the oracle/reference patch;
- fails closed (SerializationNormalizationError) on any structure it cannot
  normalize by metadata-only edits, including malformed headers, unsupported
  body lines, empty hunks, or a semantic/body fingerprint mismatch.

A semantic/body fingerprint (excluding only the normalized hunk counts) is
computed before and after; dispatch is forbidden unless it is unchanged.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

# Unified-diff hunk header: @@ -old_start[,old_count] +new_start[,new_count] @@ suffix
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
# `\ No newline at end of file` — metadata, never counted as a body line
_NO_NEWLINE_MARKER = "\\ No newline at end of file"
_BODY_PREFIXES = (" ", "-", "+")


class SerializationNormalizationError(ValueError):
    """Raised when a diff cannot be normalized by strictly metadata-only edits."""


@dataclass(frozen=True)
class HunkHeader:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    suffix: str


@dataclass(frozen=True)
class HunkChange:
    """One hunk whose count fields were rewritten."""

    before: str
    after: str
    old_count_before: int
    old_count_after: int
    new_count_before: int
    new_count_after: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "before": self.before,
            "after": self.after,
            "old_count_before": self.old_count_before,
            "old_count_after": self.old_count_after,
            "new_count_before": self.new_count_before,
            "new_count_after": self.new_count_after,
        }


@dataclass(frozen=True)
class NormalizationRecord:
    """Explicit, machine-checkable record of a B -> C normalization."""

    model_patch_raw_sha256: str
    model_patch_serialization_normalized_sha256: str
    changed_headers: tuple[HunkChange, ...]
    header_fields_changed: int
    semantic_body_fingerprint_raw: str
    semantic_body_fingerprint_normalized: str
    fingerprint_equal: bool
    paths_and_starts_unchanged: bool

    def to_mapping(self) -> dict[str, object]:
        return {
            "model_patch_raw_sha256": self.model_patch_raw_sha256,
            "model_patch_serialization_normalized_sha256": self.model_patch_serialization_normalized_sha256,
            "changed_headers": [c.to_mapping() for c in self.changed_headers],
            "header_fields_changed": self.header_fields_changed,
            "semantic_body_fingerprint_raw": self.semantic_body_fingerprint_raw,
            "semantic_body_fingerprint_normalized": self.semantic_body_fingerprint_normalized,
            "fingerprint_equal": self.fingerprint_equal,
            "paths_and_starts_unchanged": self.paths_and_starts_unchanged,
        }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_hunk_header(line: str) -> Optional[HunkHeader]:
    match = _HUNK_RE.match(line)
    if match is None:
        return None
    old_start = int(match.group(1))
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_start = int(match.group(3))
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    suffix = match.group(5) or ""
    return HunkHeader(old_start, old_count, new_start, new_count, suffix)


def _is_file_header(line: str) -> bool:
    return line.startswith("--- ") or line.startswith("+++ ")


def _split_logical_lines(diff: str) -> list[str]:
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines


def normalize_hunk_counts(diff: str) -> tuple[str, NormalizationRecord]:
    """Normalize ONLY hunk count metadata; return (C, NormalizationRecord).

    Raises SerializationNormalizationError (fail-closed) if the diff cannot
    be normalized by strictly metadata-only edits or if the semantic/body
    fingerprint changes.
    """

    if type(diff) is not str or not diff:
        raise SerializationNormalizationError("diff must be a non-empty string")
    lines = _split_logical_lines(diff)
    out: list[str] = []
    changes: list[HunkChange] = []
    changed_fields = 0
    hunk_count = 0
    file_header_count = 0
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if _is_file_header(line):
            file_header_count += 1
            out.append(line)
            index += 1
            continue
        header = _parse_hunk_header(line)
        if header is None:
            raise SerializationNormalizationError(
                f"unsupported diff structure at line {index + 1}: {line!r}"
            )
        hunk_count += 1
        body: list[str] = []
        cursor = index + 1
        while cursor < total:
            candidate = lines[cursor]
            if _parse_hunk_header(candidate) is not None or _is_file_header(candidate):
                break
            if candidate == _NO_NEWLINE_MARKER:
                body.append(candidate)
                cursor += 1
                continue
            if candidate.startswith(_BODY_PREFIXES):
                body.append(candidate)
                cursor += 1
                continue
            raise SerializationNormalizationError(
                f"unsupported hunk body line {cursor + 1}: {candidate!r}"
            )
        if not body:
            raise SerializationNormalizationError(
                f"hunk at line {index + 1} has an empty body"
            )
        old_actual = sum(1 for item in body if item.startswith((" ", "-")))
        new_actual = sum(1 for item in body if item.startswith((" ", "+")))
        header_line = line
        if old_actual != header.old_count or new_actual != header.new_count:
            header_line = (
                f"@@ -{header.old_start},{old_actual} "
                f"+{header.new_start},{new_actual} @@{header.suffix}"
            )
            changes.append(
                HunkChange(
                    before=line,
                    after=header_line,
                    old_count_before=header.old_count,
                    old_count_after=old_actual,
                    new_count_before=header.new_count,
                    new_count_after=new_actual,
                )
            )
            changed_fields += (0 if old_actual == header.old_count else 1) + (
                0 if new_actual == header.new_count else 1
            )
        out.append(header_line)
        out.extend(body)
        index = cursor
    if hunk_count == 0:
        raise SerializationNormalizationError("diff contains no hunks")
    if file_header_count == 0:
        raise SerializationNormalizationError("diff contains no file header (---/+++)")

    result = "\n".join(out)
    if diff.endswith("\n") and not result.endswith("\n"):
        result += "\n"

    fingerprint_b = semantic_body_fingerprint(diff)
    fingerprint_c = semantic_body_fingerprint(result)
    record = NormalizationRecord(
        model_patch_raw_sha256=_sha256(diff),
        model_patch_serialization_normalized_sha256=_sha256(result),
        changed_headers=tuple(changes),
        header_fields_changed=changed_fields,
        semantic_body_fingerprint_raw=fingerprint_b,
        semantic_body_fingerprint_normalized=fingerprint_c,
        fingerprint_equal=fingerprint_b == fingerprint_c,
        paths_and_starts_unchanged=fingerprint_b == fingerprint_c,
    )
    if not record.fingerprint_equal:
        raise SerializationNormalizationError(
            "semantic/body fingerprint changed during normalization — dispatch aborted"
        )
    return result, record


def semantic_body_fingerprint(diff: str) -> str:
    """SHA-256 over paths, hunk starts (NOT counts), suffixes, and every body line.

    Any path/start/body-line change (or a body line dropped/inserted) alters
    the fingerprint; only hunk count metadata is excluded.
    """

    lines = _split_logical_lines(diff)
    out: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if _is_file_header(line):
            out.append(line)
            index += 1
            continue
        header = _parse_hunk_header(line)
        if header is None:
            raise SerializationNormalizationError(
                f"unsupported diff structure at line {index + 1}: {line!r}"
            )
        out.append(f"@@ {header.old_start} {header.new_start} @@{header.suffix}")
        index += 1
        while index < total and not _is_file_header(lines[index]) and _parse_hunk_header(lines[index]) is None:
            out.append(lines[index])
            index += 1
    return _sha256("\n".join(out))


__all__ = [
    "SerializationNormalizationError",
    "NormalizationRecord",
    "HunkChange",
    "HunkHeader",
    "normalize_hunk_counts",
    "semantic_body_fingerprint",
]
