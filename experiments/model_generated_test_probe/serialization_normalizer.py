"""Deterministic patch-serialization normalizer for the S1-P diagnostic.

Purpose (post-hoc auxiliary diagnostic, NOT a new fix attempt):

    Given the ORIGINAL one-shot model-produced candidate patch from S1-P
    Live Run 1 (which PatchManager rejected with "Git metadata lines are
    not supported"), deterministically remove the syntactic serialization
    that PatchManager does not support — and nothing else.

Operations (the minimum required, each recorded exactly):

    N1  remove unsupported git-metadata lines: every line starting with
        ``diff --git``. PatchManager rejects these in every parser state.
        Any OTHER unsupported diff metadata that PatchManager rejects
        (``new file``, ``deleted file``, ``old mode``, ``new mode``,
        ``copy``, ``rename``) is NOT silently removed: dropping those would
        change file-creation/deletion/mode semantics, so the normalizer
        fails closed instead.

    N2  re-derive hunk-header line counts from the hunk's own body when
        the authored counts do not match the body. The hunk header is
        serialization metadata; its counts are exactly determined by the
        body (context/removed lines vs context/added lines). Rewriting a
        mismatched header never touches a body line.

Hard guarantees:

    * No context/added/removed body line is invented, edited, reordered,
      or repaired. Semantic hunk identity is proven by hashing the hunk
      body lines before vs after normalization (must be identical).
    * File paths in ``--- a/...`` / ``+++ b/...`` headers are preserved.
    * No gold code, no fixture-test-based repair, no model call.
    * The normalized patch must parse under the project's own
      ``_parse_unified_diff`` (the same parser PatchManager and the
      independent EvaluationVerifier use). If it does not, the normalizer
      raises ``NormalizationError``: deterministic serialization
      normalization is insufficient.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from agentic_debugger.runtime.patcher import (
    PatchValidationError,
    _parse_unified_diff,
)

# Exactly the parser's hunk-header grammar (patcher.py `_HUNK_HEADER_RE`).
_HUNK_HEADER_RE = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@"
)

# Metadata lines PatchManager rejects and that carry create/delete/mode
# semantics. Removing them would change patch semantics, so the normalizer
# fails closed on them (it only removes `diff --git` lines).
_UNSUPPORTED_SEMANTIC_METADATA = (
    "new file", "deleted file", "old mode", "new mode", "copy", "rename",
)

# Bare `---` / `+++` separators the parser skips (allowed, no content).
_BARE_HEADER_RE = re.compile(r"^(?:---|\+\+\+)$")


class NormalizationError(RuntimeError):
    """Deterministic serialization normalization is insufficient."""


@dataclass(frozen=True)
class NormalizationOperation:
    """One recorded normalization of a serialization-metadata line."""

    kind: str  # "remove_git_metadata" | "correct_hunk_header_counts"
    line_index: int  # 0-based index in the ORIGINAL patch
    original_line: str
    replacement: Optional[str] = None


@dataclass(frozen=True)
class NormalizationResult:
    original_patch: str
    normalized_patch: str
    operations: Tuple[NormalizationOperation, ...]
    semantic_hunk_before_sha256: str
    semantic_hunk_after_sha256: str
    semantic_hunks_identical: bool
    removed_lines: Tuple[str, ...] = field(default_factory=tuple)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def semantic_hunk_lines(diff_text: str) -> Tuple[str, ...]:
    """Extract every hunk body line (`` ``/``+``/``-``/``\\`` prefixes).

    Hunk headers, file headers, git metadata, ``index`` lines, and bare
    ``---``/``+++`` separators are structural serialization and are NOT
    part of the semantic hunk identity.
    """

    body: List[str] = []
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith(("--- ", "+++ ", "diff --git")) or _BARE_HEADER_RE.match(line):
            in_hunk = False
            continue
        if line.startswith("index ") and ".." in line:
            continue
        if line.startswith((" ", "+", "-", "\\")):
            body.append(line)
        # Any other line is structural (e.g. an empty separator); skip it.
    return tuple(body)


def strip_git_metadata_only(diff_text: str) -> Tuple[str, Tuple[str, ...]]:
    """Remove ONLY unsupported ``diff --git`` git-metadata lines.

    Used by the diagnostic to record the intermediate state: git metadata
    removed but hunk headers untouched (N1 without N2). Raises
    ``NormalizationError`` for any other unsupported metadata line.
    """

    kept: List[str] = []
    removed: List[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            removed.append(line)
            continue
        if line.startswith(_UNSUPPORTED_SEMANTIC_METADATA):
            raise NormalizationError(
                f"unsupported diff metadata with create/delete/mode semantics "
                f"present: {line!r}"
            )
        kept.append(line)
    return "\n".join(kept), tuple(removed)


def normalize_patch(diff_text: str) -> NormalizationResult:
    """Deterministically normalize patch serialization; semantics untouched.

    Raises ``NormalizationError`` if the patch cannot be made acceptable
    to PatchManager without changing semantic hunk content.
    """

    if not diff_text or not diff_text.strip():
        raise NormalizationError("empty patch: nothing to normalize")

    had_trailing_newline = diff_text.endswith("\n")
    raw_lines = diff_text.splitlines()
    semantic_before = semantic_hunk_lines(diff_text)

    # -- N1: remove unsupported git-metadata lines (fail closed on others) --
    kept: List[Tuple[int, str]] = []  # (original_index, line)
    operations: List[NormalizationOperation] = []
    removed_lines: List[str] = []
    for i, line in enumerate(raw_lines):
        if line.startswith("diff --git"):
            operations.append(NormalizationOperation(
                kind="remove_git_metadata", line_index=i, original_line=line,
            ))
            removed_lines.append(line)
            continue
        if line.startswith(_UNSUPPORTED_SEMANTIC_METADATA):
            raise NormalizationError(
                "unsupported diff metadata with create/delete/mode semantics "
                f"present: {line!r}; removing it would change patch semantics"
            )
        kept.append((i, line))

    # -- N2: re-derive hunk-header counts from each hunk's own body --------
    normalized_lines: List[str] = []
    for idx, (orig_index, line) in enumerate(kept):
        header_match = _HUNK_HEADER_RE.match(line)
        if header_match is None:
            normalized_lines.append(line)
            continue

        old_start = int(header_match.group(1))
        old_claimed = (
            int(header_match.group(2)) if header_match.group(2) is not None else 1
        )
        new_start = int(header_match.group(3))
        new_claimed = (
            int(header_match.group(4)) if header_match.group(4) is not None else 1
        )

        # Scan this hunk's body (mirroring the parser's counting rules:
        # ` ` -> old+new, `-` -> old, `+` -> new; `\` markers, empty lines,
        # `index` lines and bare headers count nothing).
        old_actual = 0
        new_actual = 0
        for body_line in (kept[j][1] for j in range(idx + 1, len(kept))):
            if body_line.startswith(("@@", "--- ", "+++ ", "diff --git")):
                break
            if body_line.startswith((" ", "-")):
                old_actual += 1
            if body_line.startswith((" ", "+")):
                new_actual += 1

        if old_actual == old_claimed and new_actual == new_claimed:
            normalized_lines.append(line)
            continue

        replacement = f"@@ -{old_start},{old_actual} +{new_start},{new_actual} @@"
        operations.append(NormalizationOperation(
            kind="correct_hunk_header_counts",
            line_index=orig_index,
            original_line=line,
            replacement=replacement,
        ))
        normalized_lines.append(replacement)

    normalized = "\n".join(normalized_lines)
    if had_trailing_newline:
        normalized += "\n"

    semantic_after = semantic_hunk_lines(normalized)
    if semantic_after != semantic_before:
        raise NormalizationError(
            "normalization changed semantic hunk content "
            "(invariant violation); aborting"
        )

    # -- Postcondition: PatchManager's own parser must accept the result ----
    try:
        _parse_unified_diff(normalized)
    except PatchValidationError as exc:
        raise NormalizationError(
            f"normalized patch still rejected by PatchManager: {exc}"
        ) from exc

    return NormalizationResult(
        original_patch=diff_text,
        normalized_patch=normalized,
        operations=tuple(operations),
        semantic_hunk_before_sha256=_sha256("\n".join(semantic_before)),
        semantic_hunk_after_sha256=_sha256("\n".join(semantic_after)),
        semantic_hunks_identical=semantic_before == semantic_after,
        removed_lines=tuple(removed_lines),
    )


__all__ = [
    "NormalizationError",
    "NormalizationOperation",
    "NormalizationResult",
    "normalize_patch",
    "semantic_hunk_lines",
    "strip_git_metadata_only",
]
