"""Deterministic lexical retrieval over a repository index.

Retrieval is a pure function of ``(index artifact, query)``:

* identifier-aware tokenization (``[a-z0-9_]+`` after lowercasing);
* normalized query token frequencies;
* an integer score: the dot product of query and chunk token counts
  (``sum(qf * cf)`` over the token intersection) — fully deterministic and
  JSON-safe;
* deduplication by chunk identity (defensive; the index is unique by
  construction);
* deterministic tie order ``(score desc, path asc, start_line asc,
  chunk_id asc)``;
* maximum result count and maximum total context bytes, each reported
  explicitly as a truncation flag;
* a single selected chunk that would exceed the total context budget is a
  fail-closed error — never silently returned and never truncated;
* retrieval identity is computed over the deterministic payload only;
  wall-clock latency is reported separately and cannot affect the identity.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from agentic_debugger.rag.schema import (
    MAX_QUERY_BYTES,
    MAX_RETRIEVAL_CONTEXT_BYTES,
    MAX_RETRIEVAL_RESULTS,
    RETRIEVAL_SCHEMA_VERSION,
    Chunk,
    RagInputError,
    RagValidationError,
    RepositoryIndex,
    RetrievalResult,
    RetrievalSelection,
    canonical_json,
    query_identity_hash,
    sha256_text,
)

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


class RetrievalError(RagValidationError):
    """Raised when a retrieval cannot be performed within the declared bounds."""


def tokenize(text: str) -> Tuple[str, ...]:
    """Identifier-aware tokenization: lowercase ``[a-z0-9_]+`` runs."""

    if type(text) is not str:
        raise RetrievalError("tokenize requires a string")
    return tuple(_TOKEN_RE.findall(text.lower()))


def _counts(tokens: Sequence[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return counts


def _score(query_counts: Mapping[str, int], chunk_counts: Mapping[str, int]) -> int:
    total = 0
    for token, qf in query_counts.items():
        cf = chunk_counts.get(token)
        if cf is not None:
            total += qf * cf
    return total


def query_identity(query: str) -> str:
    """Deterministic identity of a retrieval query (schema canonical form)."""

    return query_identity_hash(query)


def verify_retrieval_against_index(
    result: RetrievalResult, index: RepositoryIndex
) -> None:
    """Fail-closed verification that every selection exists in the bound index
    with matching path, line range and text."""

    if not isinstance(result, RetrievalResult) or not isinstance(index, RepositoryIndex):
        raise RetrievalError("verify_retrieval_against_index requires result and index")
    if result.index_id != index.index_id:
        raise RetrievalError("retrieval result is not bound to this index")
    by_id = {chunk.chunk_id: chunk for chunk in index.chunks}
    for selection in result.selected:
        chunk = by_id.get(selection.chunk_id)
        if chunk is None:
            raise RetrievalError(
                f"selection {selection.chunk_id!r} does not exist in the index"
            )
        if (
            chunk.path != selection.path
            or chunk.start_line != selection.start_line
            or chunk.end_line != selection.end_line
            or chunk.text != selection.text
        ):
            raise RetrievalError(
                f"selection {selection.chunk_id!r} does not match the indexed chunk"
            )


def retrieve(
    index: RepositoryIndex,
    query: str,
    *,
    expected_revision: Optional[str] = None,
    max_results: int = MAX_RETRIEVAL_RESULTS,
    max_context_bytes: int = MAX_RETRIEVAL_CONTEXT_BYTES,
    latency_ms: Optional[int] = None,
) -> RetrievalResult:
    """Run deterministic lexical retrieval over an index.

    Raises :class:`RetrievalError` for an empty/oversized query, a stale
    revision, invalid bounds, or a selected chunk that individually exceeds
    ``max_context_bytes``.
    """

    if not isinstance(index, RepositoryIndex):
        raise RetrievalError("retrieve requires a RepositoryIndex")
    if type(query) is not str or not query.strip():
        raise RetrievalError("query must be a non-empty string")
    if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        raise RetrievalError(f"query exceeds the {MAX_QUERY_BYTES}-byte cap")
    if expected_revision is not None and index.revision != expected_revision:
        from agentic_debugger.rag.indexer import StaleRevisionError

        raise StaleRevisionError(
            f"index revision {index.revision!r} does not match expected "
            f"revision {expected_revision!r}"
        )
    if type(max_results) is not int or isinstance(max_results, bool) or max_results < 1:
        raise RetrievalError("max_results must be a positive integer")
    if (
        type(max_context_bytes) is not int
        or isinstance(max_context_bytes, bool)
        or max_context_bytes < 1
    ):
        raise RetrievalError("max_context_bytes must be a positive integer")
    if latency_ms is not None and (type(latency_ms) is not int or latency_ms < 0):
        raise RetrievalError("latency_ms must be a non-negative integer or null")

    query_counts = _counts(tokenize(query))
    scored: List[Tuple[int, Chunk]] = []
    for chunk in index.chunks:
        chunk_counts = _counts(tokenize(chunk.text))
        score = _score(query_counts, chunk_counts)
        if score > 0:
            scored.append((score, chunk))
    scored.sort(
        key=lambda item: (
            -item[0],
            item[1].path,
            item[1].start_line,
            item[1].chunk_id,
        )
    )

    selected: List[RetrievalSelection] = []
    total_bytes = 0
    result_count_truncated = False
    context_bytes_truncated = False
    for score, chunk in scored:
        chunk_bytes = chunk.text_bytes
        if chunk_bytes > max_context_bytes:
            raise RetrievalError(
                f"best chunk {chunk.chunk_id!r} ({chunk_bytes} bytes) exceeds the "
                f"max_context_bytes budget of {max_context_bytes}"
            )
        if len(selected) >= max_results:
            result_count_truncated = True
            break
        if total_bytes + chunk_bytes > max_context_bytes:
            context_bytes_truncated = True
            break
        selected.append(
            RetrievalSelection(
                chunk_id=chunk.chunk_id,
                path=chunk.path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                score=score,
                bytes=chunk_bytes,
                text=chunk.text,
            )
        )
        total_bytes += chunk_bytes

    qid = query_identity(query)
    retrieval_id = RetrievalResult.identity(
        index.index_id,
        qid,
        query,
        selected,
        result_count_truncated,
        context_bytes_truncated,
        total_bytes,
        max_results,
        max_context_bytes,
    )
    result = RetrievalResult(
        schema_version=RETRIEVAL_SCHEMA_VERSION,
        retrieval_id=retrieval_id,
        index_id=index.index_id,
        query_identity=qid,
        query=query,
        selected=tuple(selected),
        result_count_truncated=result_count_truncated,
        context_bytes_truncated=context_bytes_truncated,
        selected_bytes=total_bytes,
        max_results=max_results,
        max_context_bytes=max_context_bytes,
        latency_ms=latency_ms,
    )
    # Integrity: every selection must exist in the bound index with matching
    # provenance and text (fail-closed; never silently accepted).
    verify_retrieval_against_index(result, index)
    return result


__all__ = [
    "RetrievalError",
    "tokenize",
    "query_identity",
    "verify_retrieval_against_index",
    "retrieve",
]
