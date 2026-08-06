"""Deterministic chunking of corpus documents.

Chunking strategy (documented and unit-tested):

* for parseable ``source``/``test`` documents, top-level Python symbols
  (classes and functions, including their decorator start line) become
  symbol chunks — the natural unit for retrieval of code;
* deterministic **gap chunks** cover every line outside the symbol chunks:
  module docstrings, imports, module-level assignments and constants, code
  between symbols and trailing module text — no non-empty line is silently
  lost and coverage is complete and non-overlapping;
* any document that does not parse (or has no symbols) falls back to
  deterministic line windows of :data:`CHUNK_WINDOW_LINES` lines;
* a symbol wider than :data:`MAX_CHUNK_LINES` lines is subdivided into
  deterministic line windows;
* a window whose assembled text would exceed :data:`MAX_CHUNK_BYTES` is
  subdivided (halving the window) until it fits; a single line wider than
  :data:`MAX_LINE_BYTES` is a fail-closed error — it is never truncated
  silently;
* chunk identity is a SHA-256 over the canonical content-plus-provenance
  payload, so identical text at the same provenance always yields the same
  chunk ID.

Source text is only ever parsed with the standard-library ``ast`` module.
Arbitrary source is never loaded through execution or imports.
"""

from __future__ import annotations

import ast
from typing import List, Sequence, Tuple

from agentic_debugger.rag.corpus import CorpusError
from agentic_debugger.rag.schema import (
    CHUNK_WINDOW_LINES,
    MAX_CHUNK_BYTES,
    MAX_CHUNK_LINES,
    MAX_LINE_BYTES,
    Chunk,
    CorpusDocument,
    CorpusSourceKind,
    RagValidationError,
)


class ChunkingError(CorpusError):
    """Raised when a document cannot be chunked within the declared bounds."""


def _line_bytes(line: str) -> int:
    return len(line.encode("utf-8"))


def _verify_lines(lines: Sequence[str], path: str) -> None:
    for index, line in enumerate(lines):
        if _line_bytes(line) > MAX_LINE_BYTES:
            raise ChunkingError(
                f"line {index + 1} of {path!r} exceeds the {MAX_LINE_BYTES}-byte "
                f"line cap"
            )


def _window_chunks(
    document: CorpusDocument,
    lines: Sequence[str],
    start: int,
    end: int,
    window: int,
) -> List[Chunk]:
    """Chunk lines ``[start, end)`` (1-based inclusive/exclusive) in windows.

    ``start``/``end`` are 1-based line numbers; ``lines`` is 0-based.
    Windows are subdivided until the assembled text fits ``MAX_CHUNK_BYTES``;
    a single oversized line already failed ``_verify_lines``.
    """

    total_lines = end - start + 1
    if window < 1:
        raise ChunkingError("chunk window must be positive")
    chunks: List[Chunk] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + window - 1, end)
        selected = lines[cursor - 1 : window_end]
        text = "\n".join(line.rstrip("\r\n") for line in selected) + "\n"
        if _line_bytes(text) > MAX_CHUNK_BYTES and window > 1:
            half = max(1, window // 2)
            chunks.extend(
                _window_chunks(document, lines, cursor, end, half)
            )
            return chunks
        chunk = _make_chunk(document, cursor, window_end, text)
        chunks.append(chunk)
        cursor = window_end + 1
    return chunks


def _make_chunk(document: CorpusDocument, start_line: int, end_line: int, text: str) -> Chunk:
    if _line_bytes(text) > MAX_CHUNK_BYTES:
        raise ChunkingError(
            f"chunk for {document.path!r} exceeds the {MAX_CHUNK_BYTES}-byte cap"
        )
    chunk_id = Chunk.identity(
        document.document_id, document.path, start_line, end_line, text
    )
    return Chunk(
        chunk_id=chunk_id,
        document_id=document.document_id,
        kind=document.kind,
        path=document.path,
        start_line=start_line,
        end_line=end_line,
        text=text,
    )


def _symbol_chunks(document: CorpusDocument, lines: Sequence[str]) -> List[Chunk]:
    """Top-level symbol chunks for a parseable Python document."""

    try:
        # Parse the verbatim document text: ``splitlines`` strips line
        # endings, and joining those fragments back together would silently
        # collapse the source onto one line.
        tree = ast.parse(document.text, filename=document.path)
    except SyntaxError:
        raise ChunkingError(
            f"document {document.path!r} is not parseable as Python"
        ) from None

    symbols: List[Tuple[int, int]] = []  # (decorator_start, end_line)

    def add(node: ast.AST) -> None:
        decs = getattr(node, "decorator_list", [])
        start = min((d.lineno for d in decs), default=node.lineno)  # type: ignore[attr-defined]
        end = getattr(node, "end_lineno", None) or node.lineno  # type: ignore[attr-defined]
        symbols.append((start, end))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add(node)
    symbols.sort()
    if not symbols:
        return []
    chunks: List[Chunk] = []
    for start, end in symbols:
        if end - start + 1 > MAX_CHUNK_LINES:
            chunks.extend(
                _window_chunks(document, lines, start, end, CHUNK_WINDOW_LINES)
            )
        else:
            text = "\n".join(
                line.rstrip("\r\n") for line in lines[start - 1 : end]
            ) + "\n"
            if _line_bytes(text) > MAX_CHUNK_BYTES:
                chunks.extend(
                    _window_chunks(document, lines, start, end, CHUNK_WINDOW_LINES)
                )
            else:
                chunks.append(_make_chunk(document, start, end, text))
    return chunks


def _merge_symbol_and_gap_chunks(
    document: CorpusDocument, lines: Sequence[str], symbol_chunks: Sequence[Chunk]
) -> List[Chunk]:
    """Merge symbol chunks with deterministic gap chunks so that every
    non-empty line of the document is represented by at least one chunk.

    Gaps cover: module docstrings, imports, module-level assignments and
    constants, code between symbols, and trailing module text.  Coverage is
    complete, deterministic, and non-overlapping with the symbol chunks.
    """

    chunks: List[Chunk] = list(symbol_chunks)
    covered = sorted((chunk.start_line, chunk.end_line) for chunk in chunks)
    gaps: List[Tuple[int, int]] = []
    cursor = 1
    for start, end in covered:
        if cursor < start:
            gaps.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= len(lines):
        gaps.append((cursor, len(lines)))
    for gap_start, gap_end in gaps:
        chunks.extend(
            _window_chunks(document, lines, gap_start, gap_end, CHUNK_WINDOW_LINES)
        )
    chunks.sort(key=lambda chunk: (chunk.start_line, chunk.chunk_id))
    return chunks


def chunk_document(document: CorpusDocument) -> List[Chunk]:
    """Chunk one corpus document deterministically with full line coverage.

    For parseable ``source``/``test`` documents the result is the union of
    symbol chunks and deterministic gap chunks; no non-empty line is lost.
    A document that cannot be parsed falls back to line windows.  Raises
    :class:`ChunkingError` for an oversized single line.
    """

    if not isinstance(document, CorpusDocument):
        raise ChunkingError("chunk_document requires a CorpusDocument")
    lines = document.text.splitlines()
    if not lines:
        return []
    _verify_lines(lines, document.path)

    if document.kind in (CorpusSourceKind.SOURCE, CorpusSourceKind.TEST):
        try:
            symbol_chunks = _symbol_chunks(document, lines)
        except ChunkingError:
            symbol_chunks = []
        if symbol_chunks:
            return _merge_symbol_and_gap_chunks(document, lines, symbol_chunks)

    return _window_chunks(
        document, lines, 1, len(lines), CHUNK_WINDOW_LINES
    )


__all__ = [
    "ChunkingError",
    "chunk_document",
]
