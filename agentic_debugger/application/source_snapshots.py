"""Safe app-owned source snapshot capture for new execution evidence.

The application must be able to display the source corresponding to recorded
execution state even after the disposable execution workspace is removed.
This module captures bounded, hashed source content at materially distinct
source states (``initial``, ``applied``, ``reverted``) using logical
repository/workspace-relative identities.

Safety rules:

- only the caller-selected logical path is read (never a recursive scan), so
  hidden tests, evaluator oracles, credentials, and unrelated environment
  content cannot enter a snapshot;
- captured content that matches the shared credential-shape policy
  (:func:`agentic_debugger.application.events.contains_credential_shape`) is
  withheld: the capture fails closed instead of persisting a credential-
  shaped body or rewriting it with fake source semantics;
- the logical path must be a relative path without ``..`` traversal, and the
  resolved file must stay inside ``root_dir`` (the same containment rule the
  PDB layer enforces);
- content is bounded (:data:`MAX_SOURCE_TEXT_CHARS`); larger files are
  captured as a truncated prefix with ``truncated=True`` while the SHA-256
  still covers the exact full file bytes (integrity is never weakened);
- the content hash is computed over the exact file bytes, so a snapshot's
  hash identifies the historical file without trusting today's checkout;
- decoding is fail-safe: a truncated tail that splits a multi-byte character
  decodes with ``errors="replace"`` (the snapshot is marked truncated), and
  the retained text is trimmed to a character prefix that always re-encodes
  within the event source-text bound;

Historical evidence that never recorded source stays ``NOT RECORDED`` in the
presentation layer; this module only captures genuinely new app-owned
evidence and never reconstructs old source from the current repository.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.events import (
    MAX_SOURCE_TEXT_CHARS,
    SourceSnapshotStage,
    contains_credential_shape,
)

#: Hard read bound for one captured file (the PDB target source bound is
#: 16 MiB; refusing anything larger keeps the capture helper deterministic).
MAX_SOURCE_READ_BYTES = 16 * 1024 * 1024

__all__ = [
    "MAX_SOURCE_READ_BYTES",
    "MAX_SOURCE_TEXT_CHARS",
    "SourceSnapshot",
    "SourceSnapshotError",
    "SourceSnapshotStage",
    "capture_source_snapshot",
    "validate_logical_path",
]


class SourceSnapshotError(ApplicationInputError):
    """Raised when a source snapshot cannot be captured safely."""


def _has_raw_dotdot(path: str) -> bool:
    return ".." in [part for part in path.replace("\\", "/").split("/") if part]


def validate_logical_path(path: str) -> str:
    """Validate one logical source identity (workspace-relative, safe)."""
    if type(path) is not str or not path:
        raise SourceSnapshotError("source path must be a non-empty string")
    if "\x00" in path:
        raise SourceSnapshotError("source path contains a NUL byte")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        raise SourceSnapshotError("source path must be UTF-8 text")
    if len(path.encode("utf-8")) > 512:
        raise SourceSnapshotError("source path exceeds the 512-byte bound")
    if path.startswith("/") or path.startswith("\\"):
        raise SourceSnapshotError("source path must be a relative path")
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        raise SourceSnapshotError("source path must not carry a drive letter")
    if _has_raw_dotdot(path):
        raise SourceSnapshotError("source path must not contain .. traversal")
    return path.replace("\\", "/")


@dataclass(frozen=True)
class SourceSnapshot:
    """One captured, bounded, hashed source snapshot."""

    logical_path: str
    sha256: str
    text: str
    line_count: int
    truncated: bool
    stage: SourceSnapshotStage

    def to_event_payload(self) -> dict[str, object]:
        return {
            "path": self.logical_path,
            "sha256": self.sha256,
            "text": self.text,
            "line_count": self.line_count,
            "truncated": self.truncated,
            "stage": self.stage.value,
        }


def capture_source_snapshot(
    root_dir: str | os.PathLike[str],
    logical_path: str,
    stage: SourceSnapshotStage,
    *,
    max_chars: int = MAX_SOURCE_TEXT_CHARS,
) -> SourceSnapshot:
    """Capture one safe source snapshot from ``root_dir``.

    The logical path is validated (relative, no traversal), resolved inside
    the root, read with a hard size bound, hashed over the exact bytes, and
    bounded into the returned snapshot.  Raises :class:`SourceSnapshotError`
    on any safety violation; never silently widens the root boundary.
    """
    if type(stage) is not SourceSnapshotStage:
        raise SourceSnapshotError("stage must be a SourceSnapshotStage")
    if type(max_chars) is not int or isinstance(max_chars, bool) or max_chars < 1:
        raise SourceSnapshotError("max_chars must be a positive integer")
    logical = validate_logical_path(logical_path)

    root = os.path.realpath(str(root_dir))
    if not os.path.isdir(root):
        raise SourceSnapshotError(f"source root is not a directory: {root}")
    absolute = os.path.realpath(os.path.join(root, logical.replace("/", os.sep)))
    try:
        common = os.path.commonpath([root, absolute])
    except ValueError as exc:
        raise SourceSnapshotError(
            f"source path escapes the root: {logical}"
        ) from exc
    if os.path.normcase(common) != os.path.normcase(root):
        raise SourceSnapshotError(f"source path escapes the root: {logical}")
    if not os.path.isfile(absolute):
        raise SourceSnapshotError(f"source file does not exist: {logical}")

    try:
        with open(absolute, "rb") as stream:
            data = stream.read(MAX_SOURCE_READ_BYTES + 1)
    except OSError as exc:
        raise SourceSnapshotError(f"cannot read source file {logical}: {exc}") from exc
    if len(data) > MAX_SOURCE_READ_BYTES:
        raise SourceSnapshotError(
            f"source file exceeds the {MAX_SOURCE_READ_BYTES}-byte read bound: {logical}"
        )

    digest = hashlib.sha256(data).hexdigest()
    truncated = len(data) > max_chars
    kept = data[:max_chars]
    # A truncated tail may split a multi-byte UTF-8 character; ``replace``
    # turns the partial character into U+FFFD inside an explicitly truncated
    # snapshot instead of failing the whole capture.
    text = kept.decode("utf-8", errors="replace")
    # Repair Pass 2: replacement expansion (each invalid/partial byte becomes
    # a 3-byte U+FFFD) can push the encoded size of the decoded text past the
    # byte bound even though the raw prefix fit.  Every successfully returned
    # snapshot must satisfy the event source-text contract, so trim complete
    # characters from the end (a bounded character prefix of the replaced
    # decode, never a byte-split tail) until the retained text re-encodes
    # within the bound.  The full-file hash still covers the exact original
    # bytes and ``truncated`` stays True.
    if len(text.encode("utf-8")) > max_chars:
        encoded = text.encode("utf-8")
        excess = len(encoded) - max_chars
        removed = 0
        end = len(text)
        while end > 0 and removed < excess:
            end -= 1
            removed += len(text[end].encode("utf-8"))
        text = text[:end]
    # Producer-side content policy (Repair Pass 3): a source body whose
    # content matches the shared credential-shape policy is withheld -- the
    # snapshot is rejected, never silently rewritten with fake semantics.
    # The policy is keyword-anchored, so harmless source identifiers such as
    # ``token_count`` or ``secretary`` never match.
    if contains_credential_shape(text):
        raise SourceSnapshotError(
            "source content contains a credential-shaped value; snapshot withheld"
        )
    # Line numbering follows the PDB convention (1-based over splitlines),
    # so a snapshot's line_count matches the debugger's line validation.
    line_count = max(1, len(text.splitlines()))
    return SourceSnapshot(
        logical_path=logical,
        sha256=digest,
        text=text,
        line_count=line_count,
        truncated=truncated,
        stage=stage,
    )
