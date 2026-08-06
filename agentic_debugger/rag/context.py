"""The bounded, agent-facing RAG context structure.

``RagContext`` is the only surface of this package that other packages
consume:

* the live model adapter embeds :meth:`RagContext.to_request_mapping` as the
  additive ``retrieved_context`` key of the public request (only when RAG is
  explicitly enabled);
* the deterministic demo path records :meth:`RagContext.to_record_mapping`
  as case/attempt evidence (identities and metrics, without the full text).

The structure is bounded, strictly validated, and fail-closed:

* every chunk reference is a validated :class:`RagChunkRef` (types, relative
  path, positive line range, score, text byte count, unique identities);
* total context text bytes are capped at :data:`MAX_RAG_CONTEXT_BYTES`;
* the context binds the retrieval identity of the source retrieval result,
  and ``context_identity`` is recomputed over the full deterministic payload
  (latency excluded);
* :data:`PUBLIC_REQUEST_BYTE_BUDGET` mirrors the frozen transport
  public-evidence budget (20,000 bytes); the live adapter enforces it for
  request-plus-context before any transport call.

Both the demo boundary and the live boundary accept **only** a validated
:class:`RagContext`; arbitrary lookalike objects exposing
``to_request_mapping()``/``to_record_mapping()`` are rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from agentic_debugger.rag.schema import (
    MAX_CHUNK_BYTES,
    MAX_RAG_CONTEXT_BYTES,
    PUBLIC_REQUEST_BYTE_BUDGET,
    RagInputError,
    RagValidationError,
    canonical_json,
    sha256_text,
)

_PATH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/ -]{0,511}$")
_CHUNK_ID_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")


class RagContextError(RagInputError):
    """Raised when a RAG context violates its bounds or schema."""


@dataclass(frozen=True)
class RagChunkRef:
    """One retrieved chunk as passed to the agent boundary (strict)."""

    chunk_id: str
    path: str
    start_line: int
    end_line: int
    score: int
    text: str

    def __post_init__(self) -> None:
        if type(self.chunk_id) is not str or not self.chunk_id:
            raise RagContextError("chunk_id must be a non-empty string")
        if _CHUNK_ID_PATTERN.match(self.chunk_id) is None:
            raise RagContextError(f"invalid chunk_id: {self.chunk_id!r}")
        if type(self.path) is not str or not self.path:
            raise RagContextError("path must be a non-empty string")
        if self.path.startswith("/") or self.path.startswith("\\"):
            raise RagContextError(f"path must be relative: {self.path!r}")
        if ".." in self.path.replace("\\", "/").split("/"):
            raise RagContextError(f"path must not contain traversal: {self.path!r}")
        if _PATH_PATTERN.match(self.path) is None:
            raise RagContextError(f"path contains unsupported characters: {self.path!r}")
        if type(self.start_line) is not int or self.start_line < 1:
            raise RagContextError("start_line must be a positive integer")
        if type(self.end_line) is not int or self.end_line < self.start_line:
            raise RagContextError("end_line must be >= start_line")
        if type(self.score) is not int or isinstance(self.score, bool) or self.score < 0:
            raise RagContextError("score must be a non-negative integer")
        if type(self.text) is not str or not self.text:
            raise RagContextError("text must be a non-empty string")
        if len(self.text.encode("utf-8")) > MAX_CHUNK_BYTES:
            raise RagContextError(
                f"chunk text exceeds the {MAX_CHUNK_BYTES}-byte cap"
            )

    @property
    def text_bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    def to_request_mapping(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "score": self.score,
            "text": self.text,
        }


@dataclass(frozen=True)
class RagContext:
    """Bounded retrieved context bound to index, query and retrieval
    identities."""

    index_id: str
    query_identity: str
    retrieval_id: str
    query: str
    chunks: Tuple[RagChunkRef, ...]
    selected_bytes: int
    truncated: bool
    retrieval_latency_ms: Optional[int]

    def __post_init__(self) -> None:
        if type(self.index_id) is not str or not self.index_id:
            raise RagContextError("index_id must be a non-empty string")
        if type(self.query_identity) is not str or not self.query_identity:
            raise RagContextError("query_identity must be a non-empty string")
        if type(self.retrieval_id) is not str or not self.retrieval_id:
            raise RagContextError("retrieval_id must be a non-empty string")
        if _CHUNK_ID_PATTERN.match(self.retrieval_id) is None:
            raise RagContextError(f"invalid retrieval_id: {self.retrieval_id!r}")
        if type(self.query) is not str or not self.query:
            raise RagContextError("query must be a non-empty string")
        if type(self.chunks) is not tuple or not self.chunks:
            raise RagContextError("chunks must be a non-empty tuple")
        seen: set[str] = set()
        total = 0
        for chunk in self.chunks:
            if type(chunk) is not RagChunkRef:
                raise RagContextError("chunks must contain only RagChunkRef values")
            if chunk.chunk_id in seen:
                raise RagContextError(
                    f"duplicate chunk identity: {chunk.chunk_id}"
                )
            seen.add(chunk.chunk_id)
            total += chunk.text_bytes
        if total > MAX_RAG_CONTEXT_BYTES:
            raise RagContextError(
                f"context text exceeds the {MAX_RAG_CONTEXT_BYTES}-byte cap"
            )
        if total != self.selected_bytes:
            raise RagContextError("selected_bytes does not match the chunks")
        if type(self.truncated) is not bool:
            raise RagContextError("truncated must be a boolean")
        if self.retrieval_latency_ms is not None and (
            type(self.retrieval_latency_ms) is not int or self.retrieval_latency_ms < 0
        ):
            raise RagContextError("retrieval_latency_ms must be a non-negative int or null")

    @staticmethod
    def from_retrieval(
        result: Any,
        *,
        max_context_bytes: int = MAX_RAG_CONTEXT_BYTES,
        retrieval_latency_ms: Optional[int] = None,
    ) -> "RagContext":
        """Build a bounded agent context from a ``retrieval-result-v1`` artifact.

        Accepts the :class:`RetrievalResult` dataclass or its validated
        mapping.  Raises :class:`RagContextError` when the retrieval result
        does not fit the (smaller) agent-context budget.
        """

        from agentic_debugger.rag.schema import RetrievalResult

        if isinstance(result, Mapping):
            result = RetrievalResult.from_mapping(result)
        if not isinstance(result, RetrievalResult):
            raise RagContextError("from_retrieval requires a RetrievalResult")
        chunks = tuple(
            RagChunkRef(
                chunk_id=item.chunk_id,
                path=item.path,
                start_line=item.start_line,
                end_line=item.end_line,
                score=item.score,
                text=item.text,
            )
            for item in result.selected
        )
        total = sum(chunk.text_bytes for chunk in chunks)
        if total > max_context_bytes:
            raise RagContextError(
                f"retrieval result exceeds the {max_context_bytes}-byte "
                f"agent-context cap"
            )
        return RagContext(
            index_id=result.index_id,
            query_identity=result.query_identity,
            retrieval_id=result.retrieval_id,
            query=result.query,
            chunks=chunks,
            selected_bytes=total,
            truncated=result.result_count_truncated or result.context_bytes_truncated,
            retrieval_latency_ms=retrieval_latency_ms,
        )

    def to_request_mapping(self) -> Dict[str, Any]:
        """The additive ``retrieved_context`` request block (bounded)."""

        return {
            "index_id": self.index_id,
            "query_identity": self.query_identity,
            "retrieval_id": self.retrieval_id,
            "query": self.query,
            "chunks": [chunk.to_request_mapping() for chunk in self.chunks],
            "selected_bytes": self.selected_bytes,
            "truncated": self.truncated,
        }

    def to_record_mapping(self) -> Dict[str, Any]:
        """Compact case/attempt evidence: identities and metrics, no text."""

        return {
            "index_id": self.index_id,
            "query_identity": self.query_identity,
            "retrieval_id": self.retrieval_id,
            "query": self.query,
            "chunk_count": len(self.chunks),
            "chunk_ids": [chunk.chunk_id for chunk in self.chunks],
            "chunk_locations": [
                {
                    "path": chunk.path,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "score": chunk.score,
                }
                for chunk in self.chunks
            ],
            "selected_bytes": self.selected_bytes,
            "truncated": self.truncated,
            "retrieval_latency_ms": self.retrieval_latency_ms,
        }

    def context_identity(self) -> str:
        """Deterministic identity of the bounded context (latency excluded).

        Recomputed over the full deterministic payload; any tampering with
        the bound identities changes it.
        """

        payload = {
            "index_id": self.index_id,
            "query_identity": self.query_identity,
            "retrieval_id": self.retrieval_id,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "path": chunk.path,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "score": chunk.score,
                    "text": chunk.text,
                }
                for chunk in self.chunks
            ],
            "selected_bytes": self.selected_bytes,
            "truncated": self.truncated,
        }
        return sha256_text(canonical_json(payload))


__all__ = [
    "PUBLIC_REQUEST_BYTE_BUDGET",
    "MAX_RAG_CONTEXT_BYTES",
    "RagContextError",
    "RagChunkRef",
    "RagContext",
]
