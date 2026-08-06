"""Strict versioned schemas for the repository-native RAG subsystem.

Schemas follow the repository's established contract style:

* exact schema version strings;
* no unknown fields, no missing fields;
* canonical JSON serialization (sorted keys, compact separators,
  ``allow_nan=False``);
* fail-closed validation with typed errors;
* deterministic identity computation over canonical payloads.

Two artifact schemas live here:

* ``repository-index-v1`` — the self-contained deterministic index
  (:class:`RepositoryIndex`);
* ``retrieval-result-v1`` — the replayable retrieval result
  (:class:`RetrievalResult`).

Integrity hardening (repair 1): every identity field is recomputed and
verified on load and construction — index identity, corpus digest, chunk
identities, query identity, retrieval identity, selection byte counts — and
all declared caps are enforced on build and load.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Bounds and limits (documented; enforced; unit-tested)
# ---------------------------------------------------------------------------

#: Version of the repository index artifact schema.
RAG_SCHEMA_VERSION = "repository-index-v1"
#: Version of the retrieval result artifact schema.
RETRIEVAL_SCHEMA_VERSION = "retrieval-result-v1"

#: Hard cap on the complete serialized index artifact (approximately 5 MB),
#: including the derived ``index_id``.
MAX_INDEX_BYTES = 5 * 1024 * 1024
#: Hard cap on the number of chunks inside one index.
MAX_CHUNKS = 10000
#: Hard cap on the number of documents inside one index.
MAX_DOCUMENTS = 4096
#: Hard cap on the serialized bytes of one chunk.
MAX_CHUNK_BYTES = 8192
#: Hard cap on the byte length of one source line (fail-closed above).
MAX_LINE_BYTES = MAX_CHUNK_BYTES
#: A symbol chunk wider than this many lines is subdivided deterministically.
MAX_CHUNK_LINES = 200
#: Default deterministic line-window size for non-symbol text.
CHUNK_WINDOW_LINES = 40
#: Hard cap on one ingested file's bytes (fail-closed above).
MAX_FILE_BYTES = 512 * 1024
#: Hard cap on a retrieval query's UTF-8 bytes (fail-closed above).
MAX_QUERY_BYTES = 2000
#: Default maximum number of chunks returned by retrieval.
MAX_RETRIEVAL_RESULTS = 8
#: Default maximum total context bytes returned by retrieval.
MAX_RETRIEVAL_CONTEXT_BYTES = 4096
#: Default maximum total context bytes allowed inside one RagContext.
MAX_RAG_CONTEXT_BYTES = 4000
#: Mirror of the frozen transport public-evidence budget
#: (``scripts/opencode_protocol_transport.MAX_PUBLIC_EVIDENCE_BYTES``).
#: Used by the live adapter to fail closed when the canonical public
#: request plus the retrieved context would exceed the transport bound.
PUBLIC_REQUEST_BYTE_BUDGET = 20_000
#: Hard cap on the derived failure-output document bytes.
MAX_FAILURE_DOC_BYTES = 32 * 1024

_TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_DOC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_PATH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/ -]{0,511}$")
_CHUNK_ID_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")
_HEX64_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")


class RagError(ValueError):
    """Base error for the RAG subsystem."""


class RagInputError(RagError):
    """Raised for an invalid input to a RAG operation."""


class RagValidationError(RagError):
    """Raised when an artifact violates its strict schema or bounds."""


def canonical_json(value: Any) -> str:
    """The project's canonical JSON serialization (stable bytes).

    Sorted keys, compact separators, ASCII-escaped output, and strict finite
    numbers — the same rules used by the deterministic catalog-entry
    fingerprint and public-request contracts.
    """

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def query_identity_hash(query: str) -> str:
    """Deterministic identity of a retrieval query."""

    if type(query) is not str:
        raise RagValidationError("query must be a string")
    return sha256_text(canonical_json({"query": query}))


def corpus_digest_of(documents: Sequence["CorpusDocument"]) -> str:
    """The canonical corpus digest: hash of sorted document payloads."""

    payload = [
        (doc.document_id, doc.kind, doc.path, doc.text) for doc in documents
    ]
    return sha256_text(canonical_json(payload))


def _check_required_fields(m: Mapping[str, Any], required: set, label: str) -> None:
    missing = required - set(m.keys())
    if missing:
        raise RagValidationError(f"Missing required fields in {label}: {sorted(missing)}")


def _check_no_unknown_fields(m: Mapping[str, Any], known: set, label: str) -> None:
    extra = set(m.keys()) - known
    if extra:
        raise RagValidationError(f"Unknown fields in {label}: {sorted(extra)}")


def _ensure_str(v: Any, label: str) -> str:
    if type(v) is not str or not v:
        raise RagValidationError(f"{label} must be a non-empty string")
    return v


def _ensure_optional_str(v: Any, label: str) -> Optional[str]:
    if v is None:
        return None
    return _ensure_str(v, label)


def _ensure_int(v: Any, label: str, lo: int, hi: int) -> int:
    if type(v) is not int or isinstance(v, bool):
        raise RagValidationError(f"{label} must be an integer")
    if v < lo or v > hi:
        raise RagValidationError(f"{label} must be in [{lo}, {hi}]")
    return v


def _ensure_finite_number(v: Any, label: str) -> float:
    import math

    if isinstance(v, bool) or type(v) not in (int, float):
        raise RagValidationError(f"{label} must be a finite number")
    value = float(v)
    if not math.isfinite(value):
        raise RagValidationError(f"{label} must be finite")
    return value


def _ensure_bool(v: Any, label: str) -> bool:
    if type(v) is not bool:
        raise RagValidationError(f"{label} must be a boolean")
    return v


def _validate_relative_path(path: str, label: str) -> str:
    path = _ensure_str(path, label)
    if path.startswith("/") or path.startswith("\\"):
        raise RagValidationError(f"{label} must be relative: {path!r}")
    normalized = path.replace("\\", "/")
    if ".." in normalized.split("/"):
        raise RagValidationError(f"{label} must not contain path traversal: {path!r}")
    if _PATH_PATTERN.match(path) is None:
        raise RagValidationError(f"{label} contains unsupported characters: {path!r}")
    return normalized


# ---------------------------------------------------------------------------
# Corpus document and chunk records
# ---------------------------------------------------------------------------


class CorpusSourceKind(str):
    """Closed vocabulary of corpus document kinds."""

    SOURCE = "source"
    TEST = "test"
    ISSUE = "issue"
    FAILURE = "failure"
    DOC = "doc"


_ALL_KINDS = frozenset(
    {CorpusSourceKind.SOURCE, CorpusSourceKind.TEST, CorpusSourceKind.ISSUE,
     CorpusSourceKind.FAILURE, CorpusSourceKind.DOC}
)


@dataclass(frozen=True)
class CorpusDocument:
    """One indexed source unit (file, issue projection or failure excerpt)."""

    document_id: str
    kind: str
    path: str
    text: str

    def __post_init__(self) -> None:
        _ensure_str(self.document_id, "document_id")
        if _DOC_ID_PATTERN.match(self.document_id) is None:
            raise RagValidationError(f"invalid document_id: {self.document_id!r}")
        if self.kind not in _ALL_KINDS:
            raise RagValidationError(f"unknown corpus source kind: {self.kind!r}")
        _validate_relative_path(self.path, "path")
        if type(self.text) is not str:
            raise RagValidationError("text must be a string")
        if len(self.text.encode("utf-8")) > MAX_FILE_BYTES:
            raise RagValidationError(f"document text exceeds {MAX_FILE_BYTES} bytes")

    @property
    def text_bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "kind": self.kind,
            "path": self.path,
            "text": self.text,
        }

    @staticmethod
    def from_mapping(m: Any) -> "CorpusDocument":
        if not isinstance(m, Mapping):
            raise RagValidationError("corpus document must be a mapping")
        _check_required_fields(m, {"document_id", "kind", "path", "text"}, "document")
        _check_no_unknown_fields(m, {"document_id", "kind", "path", "text"}, "document")
        return CorpusDocument(
            document_id=_ensure_str(m["document_id"], "document_id"),
            kind=_ensure_str(m["kind"], "kind"),
            path=_ensure_str(m["path"], "path"),
            text=m["text"] if type(m["text"]) is str else _ensure_str(m["text"], "text"),
        )


@dataclass(frozen=True)
class Chunk:
    """One deterministic source chunk with explicit identity and provenance."""

    chunk_id: str
    document_id: str
    kind: str
    path: str
    start_line: int
    end_line: int
    text: str

    def __post_init__(self) -> None:
        _ensure_str(self.chunk_id, "chunk_id")
        if _CHUNK_ID_PATTERN.match(self.chunk_id) is None:
            raise RagValidationError(f"invalid chunk_id: {self.chunk_id!r}")
        _ensure_str(self.document_id, "document_id")
        if self.kind not in _ALL_KINDS:
            raise RagValidationError(f"unknown chunk kind: {self.kind!r}")
        _validate_relative_path(self.path, "path")
        _ensure_int(self.start_line, "start_line", 1, 10**9)
        _ensure_int(self.end_line, "end_line", 1, 10**9)
        if self.start_line > self.end_line:
            raise RagValidationError("start_line must not exceed end_line")
        if type(self.text) is not str:
            raise RagValidationError("text must be a string")
        if not self.text:
            raise RagValidationError("chunk text must be non-empty")
        if len(self.text.encode("utf-8")) > MAX_CHUNK_BYTES:
            raise RagValidationError(f"chunk text exceeds {MAX_CHUNK_BYTES} bytes")

    @property
    def text_bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "kind": self.kind,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "text": self.text,
        }

    @staticmethod
    def from_mapping(m: Any) -> "Chunk":
        if not isinstance(m, Mapping):
            raise RagValidationError("chunk must be a mapping")
        _check_required_fields(
            m,
            {"chunk_id", "document_id", "kind", "path", "start_line", "end_line", "text"},
            "chunk",
        )
        _check_no_unknown_fields(
            m,
            {"chunk_id", "document_id", "kind", "path", "start_line", "end_line", "text"},
            "chunk",
        )
        return Chunk(
            chunk_id=_ensure_str(m["chunk_id"], "chunk_id"),
            document_id=_ensure_str(m["document_id"], "document_id"),
            kind=_ensure_str(m["kind"], "kind"),
            path=_ensure_str(m["path"], "path"),
            start_line=_ensure_int(m["start_line"], "start_line", 1, 10**9),
            end_line=_ensure_int(m["end_line"], "end_line", 1, 10**9),
            text=m["text"] if type(m["text"]) is str else _ensure_str(m["text"], "text"),
        )

    @staticmethod
    def identity(document_id: str, path: str, start_line: int, end_line: int, text: str) -> str:
        """Deterministic chunk identity over content and provenance."""

        payload = canonical_json(
            {
                "document_id": document_id,
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "text": text,
            }
        )
        return sha256_text(payload)


# ---------------------------------------------------------------------------
# Repository index artifact (repository-index-v1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepositoryIndex:
    """The self-contained deterministic index artifact.

    On construction and load every integrity field is recomputed and
    verified: unique document identities, chunk→document binding (kind and
    path agreement), recomputed chunk identities, recomputed corpus digest,
    document/chunk caps, and the final serialized size including
    ``index_id``.
    """

    schema_version: str
    index_id: str
    mode: str
    corpus_digest: str
    revision: str
    repository_identity: str
    task_id: Optional[str]
    documents: Tuple[CorpusDocument, ...]
    chunks: Tuple[Chunk, ...]

    _KNOWN_FIELDS = {
        "schema_version", "index_id", "mode", "corpus_digest", "revision",
        "repository_identity", "task_id", "documents", "chunks",
    }

    def __post_init__(self) -> None:
        if self.schema_version != RAG_SCHEMA_VERSION:
            raise RagValidationError(
                f"unsupported index schema version: {self.schema_version!r}"
            )
        if self.mode not in {"fixture", "repo"}:
            raise RagValidationError(f"unknown index mode: {self.mode!r}")
        _ensure_str(self.index_id, "index_id")
        if _HEX64_PATTERN.match(self.index_id) is None:
            raise RagValidationError(f"invalid index_id: {self.index_id!r}")
        _ensure_str(self.corpus_digest, "corpus_digest")
        if _HEX64_PATTERN.match(self.corpus_digest) is None:
            raise RagValidationError(f"invalid corpus_digest: {self.corpus_digest!r}")
        _ensure_str(self.revision, "revision")
        _ensure_str(self.repository_identity, "repository_identity")
        if self.task_id is not None:
            if _TASK_ID_PATTERN.match(self.task_id) is None:
                raise RagValidationError(f"invalid task_id: {self.task_id!r}")
        if type(self.documents) is not tuple:
            raise RagValidationError("documents must be a tuple")
        if type(self.chunks) is not tuple:
            raise RagValidationError("chunks must be a tuple")
        if len(self.documents) > MAX_DOCUMENTS:
            raise RagValidationError(
                f"index exceeds the document cap of {MAX_DOCUMENTS}"
            )
        if len(self.chunks) > MAX_CHUNKS:
            raise RagValidationError(f"index exceeds the chunk cap of {MAX_CHUNKS}")

        document_ids: set[str] = set()
        for document in self.documents:
            if document.document_id in document_ids:
                raise RagValidationError(
                    f"duplicate document identity: {document.document_id}"
                )
            document_ids.add(document.document_id)
        documents_by_id = {d.document_id: d for d in self.documents}

        chunk_ids: set[str] = set()
        for chunk in self.chunks:
            if chunk.chunk_id in chunk_ids:
                raise RagValidationError(f"duplicate chunk identity: {chunk.chunk_id}")
            chunk_ids.add(chunk.chunk_id)
            document = documents_by_id.get(chunk.document_id)
            if document is None:
                raise RagValidationError(
                    f"chunk {chunk.chunk_id} references unknown document "
                    f"{chunk.document_id!r}"
                )
            if chunk.kind != document.kind or chunk.path != document.path:
                raise RagValidationError(
                    f"chunk {chunk.chunk_id} kind/path disagree with document "
                    f"{document.document_id!r}"
                )
            expected_id = Chunk.identity(
                chunk.document_id, chunk.path, chunk.start_line, chunk.end_line,
                chunk.text,
            )
            if expected_id != chunk.chunk_id:
                raise RagValidationError(
                    f"chunk identity {chunk.chunk_id!r} does not match its content"
                )

        expected_digest = corpus_digest_of(self.documents)
        if expected_digest != self.corpus_digest:
            raise RagValidationError("corpus digest does not match the documents")

        expected_identity = self._compute_identity()
        if self.index_id != expected_identity:
            raise RagValidationError("index identity does not match its content")

        serialized = canonical_json(self.to_mapping())
        if len(serialized.encode("utf-8")) > MAX_INDEX_BYTES:
            raise RagValidationError(
                f"serialized index exceeds the {MAX_INDEX_BYTES}-byte cap"
            )

    def _identity_payload(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "corpus_digest": self.corpus_digest,
            "revision": self.revision,
            "repository_identity": self.repository_identity,
            "task_id": self.task_id,
            "documents": [doc.to_mapping() for doc in self.documents],
            "chunks": [chunk.to_mapping() for chunk in self.chunks],
        }

    def _compute_identity(self) -> str:
        return sha256_text(canonical_json(self._identity_payload()))

    def to_mapping(self) -> Dict[str, Any]:
        mapping = {
            "schema_version": self.schema_version,
            "index_id": self.index_id,
            "mode": self.mode,
            "corpus_digest": self.corpus_digest,
            "revision": self.revision,
            "repository_identity": self.repository_identity,
            "task_id": self.task_id,
            "documents": [doc.to_mapping() for doc in self.documents],
            "chunks": [chunk.to_mapping() for chunk in self.chunks],
        }
        return mapping

    @staticmethod
    def from_mapping(m: Any) -> "RepositoryIndex":
        if not isinstance(m, Mapping):
            raise RagValidationError("index must be a mapping")
        _check_required_fields(
            m, RepositoryIndex._KNOWN_FIELDS, "repository-index-v1"
        )
        _check_no_unknown_fields(
            m, RepositoryIndex._KNOWN_FIELDS, "repository-index-v1"
        )
        documents_raw = m["documents"]
        chunks_raw = m["chunks"]
        if not isinstance(documents_raw, list) or not isinstance(chunks_raw, list):
            raise RagValidationError("index documents/chunks must be lists")
        documents = tuple(CorpusDocument.from_mapping(item) for item in documents_raw)
        chunks = tuple(Chunk.from_mapping(item) for item in chunks_raw)
        return RepositoryIndex(
            schema_version=_ensure_str(m["schema_version"], "schema_version"),
            index_id=_ensure_str(m["index_id"], "index_id"),
            mode=_ensure_str(m["mode"], "mode"),
            corpus_digest=_ensure_str(m["corpus_digest"], "corpus_digest"),
            revision=_ensure_str(m["revision"], "revision"),
            repository_identity=_ensure_str(
                m["repository_identity"], "repository_identity"
            ),
            task_id=_ensure_optional_str(m["task_id"], "task_id"),
            documents=documents,
            chunks=chunks,
        )

    @staticmethod
    def from_text(text: str) -> "RepositoryIndex":
        if type(text) is not str or not text:
            raise RagValidationError("index text must be a non-empty string")
        if len(text.encode("utf-8")) > MAX_INDEX_BYTES:
            raise RagValidationError(
                f"index text exceeds the {MAX_INDEX_BYTES}-byte cap"
            )
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise RagValidationError(f"index is not valid JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise RagValidationError("index must be a JSON object")
        return RepositoryIndex.from_mapping(parsed)

    @staticmethod
    def from_file(path: str) -> "RepositoryIndex":
        import os

        if not isinstance(path, str) or not path:
            raise RagValidationError("path must be a non-empty string")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise RagValidationError(f"index could not be read: {exc}") from None
        return RepositoryIndex.from_text(text)

    def to_text(self) -> str:
        return canonical_json(self.to_mapping()) + "\n"


# ---------------------------------------------------------------------------
# Retrieval result artifact (retrieval-result-v1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalSelection:
    """One selected chunk inside a retrieval result."""

    chunk_id: str
    path: str
    start_line: int
    end_line: int
    score: int
    bytes: int
    text: str

    def __post_init__(self) -> None:
        _ensure_str(self.chunk_id, "chunk_id")
        if _CHUNK_ID_PATTERN.match(self.chunk_id) is None:
            raise RagValidationError(f"invalid chunk_id: {self.chunk_id!r}")
        _validate_relative_path(self.path, "path")
        _ensure_int(self.start_line, "start_line", 1, 10**9)
        _ensure_int(self.end_line, "end_line", 1, 10**9)
        if self.start_line > self.end_line:
            raise RagValidationError("start_line must not exceed end_line")
        _ensure_int(self.score, "score", 0, 10**12)
        if type(self.text) is not str or not self.text:
            raise RagValidationError("text must be a non-empty string")
        if len(self.text.encode("utf-8")) > MAX_CHUNK_BYTES:
            raise RagValidationError(f"selection text exceeds {MAX_CHUNK_BYTES} bytes")
        if self.bytes != len(self.text.encode("utf-8")):
            raise RagValidationError("selection bytes do not match its text")

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "score": self.score,
            "bytes": self.bytes,
            "text": self.text,
        }

    @staticmethod
    def from_mapping(m: Any) -> "RetrievalSelection":
        if not isinstance(m, Mapping):
            raise RagValidationError("retrieval selection must be a mapping")
        _check_required_fields(
            m, {"chunk_id", "path", "start_line", "end_line", "score", "bytes", "text"},
            "selection",
        )
        _check_no_unknown_fields(
            m, {"chunk_id", "path", "start_line", "end_line", "score", "bytes", "text"},
            "selection",
        )
        return RetrievalSelection(
            chunk_id=_ensure_str(m["chunk_id"], "chunk_id"),
            path=_ensure_str(m["path"], "path"),
            start_line=_ensure_int(m["start_line"], "start_line", 1, 10**9),
            end_line=_ensure_int(m["end_line"], "end_line", 1, 10**9),
            score=_ensure_int(m["score"], "score", 0, 10**12),
            bytes=_ensure_int(m["bytes"], "bytes", 0, 10**9),
            text=m["text"] if type(m["text"]) is str else _ensure_str(m["text"], "text"),
        )


@dataclass(frozen=True)
class RetrievalResult:
    """The replayable, deterministic retrieval result artifact.

    On load and construction the query identity, the retrieval identity,
    every selection byte count, selection uniqueness, the declared result
    cap and the declared context cap are all verified.
    """

    schema_version: str
    retrieval_id: str
    index_id: str
    query_identity: str
    query: str
    selected: Tuple[RetrievalSelection, ...]
    result_count_truncated: bool
    context_bytes_truncated: bool
    selected_bytes: int
    max_results: int
    max_context_bytes: int
    latency_ms: Optional[int]

    _KNOWN_FIELDS = {
        "schema_version", "retrieval_id", "index_id", "query_identity", "query",
        "selected", "result_count_truncated", "context_bytes_truncated",
        "selected_bytes", "max_results", "max_context_bytes", "latency_ms",
    }

    def __post_init__(self) -> None:
        if self.schema_version != RETRIEVAL_SCHEMA_VERSION:
            raise RagValidationError(
                f"unsupported retrieval schema version: {self.schema_version!r}"
            )
        _ensure_str(self.retrieval_id, "retrieval_id")
        if _HEX64_PATTERN.match(self.retrieval_id) is None:
            raise RagValidationError(f"invalid retrieval_id: {self.retrieval_id!r}")
        _ensure_str(self.index_id, "index_id")
        _ensure_str(self.query_identity, "query_identity")
        _ensure_str(self.query, "query")
        if len(self.query.encode("utf-8")) > MAX_QUERY_BYTES:
            raise RagValidationError(f"query exceeds {MAX_QUERY_BYTES} bytes")
        expected_query_identity = query_identity_hash(self.query)
        if self.query_identity != expected_query_identity:
            raise RagValidationError("query_identity does not match the query")
        if type(self.selected) is not tuple:
            raise RagValidationError("selected must be a tuple")
        _ensure_int(self.max_results, "max_results", 1, MAX_RETRIEVAL_RESULTS)
        _ensure_int(
            self.max_context_bytes, "max_context_bytes", 1, 10**9
        )
        if len(self.selected) > self.max_results:
            raise RagValidationError(
                f"selection count {len(self.selected)} exceeds max_results "
                f"{self.max_results}"
            )
        seen: set[str] = set()
        total = 0
        for item in self.selected:
            if item.chunk_id in seen:
                raise RagValidationError(f"duplicate selection: {item.chunk_id}")
            seen.add(item.chunk_id)
            total += item.bytes
        if total != self.selected_bytes:
            raise RagValidationError("selected_bytes does not match the selections")
        if self.selected_bytes > self.max_context_bytes:
            raise RagValidationError(
                f"selected bytes {self.selected_bytes} exceed max_context_bytes "
                f"{self.max_context_bytes}"
            )
        _ensure_bool(self.result_count_truncated, "result_count_truncated")
        _ensure_bool(self.context_bytes_truncated, "context_bytes_truncated")
        expected_identity = RetrievalResult.identity(
            self.index_id,
            self.query_identity,
            self.query,
            self.selected,
            self.result_count_truncated,
            self.context_bytes_truncated,
            self.selected_bytes,
            self.max_results,
            self.max_context_bytes,
        )
        if self.retrieval_id != expected_identity:
            raise RagValidationError("retrieval identity does not match its content")
        if self.latency_ms is not None:
            _ensure_int(self.latency_ms, "latency_ms", 0, 10**9)

    def _identity_payload(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "index_id": self.index_id,
            "query_identity": self.query_identity,
            "query": self.query,
            "selected": [item.to_mapping() for item in self.selected],
            "result_count_truncated": self.result_count_truncated,
            "context_bytes_truncated": self.context_bytes_truncated,
            "selected_bytes": self.selected_bytes,
            "max_results": self.max_results,
            "max_context_bytes": self.max_context_bytes,
        }

    def to_mapping(self) -> Dict[str, Any]:
        mapping = {
            "schema_version": self.schema_version,
            "retrieval_id": self.retrieval_id,
            "index_id": self.index_id,
            "query_identity": self.query_identity,
            "query": self.query,
            "selected": [item.to_mapping() for item in self.selected],
            "result_count_truncated": self.result_count_truncated,
            "context_bytes_truncated": self.context_bytes_truncated,
            "selected_bytes": self.selected_bytes,
            "max_results": self.max_results,
            "max_context_bytes": self.max_context_bytes,
            "latency_ms": self.latency_ms,
        }
        return mapping

    @staticmethod
    def identity(index_id: str, query_identity: str, query: str,
                 selected: Sequence[RetrievalSelection], result_count_truncated: bool,
                 context_bytes_truncated: bool, selected_bytes: int,
                 max_results: int, max_context_bytes: int) -> str:
        """Deterministic retrieval identity; latency is deliberately excluded."""

        payload = {
            "schema_version": RETRIEVAL_SCHEMA_VERSION,
            "index_id": index_id,
            "query_identity": query_identity,
            "query": query,
            "selected": [item.to_mapping() for item in selected],
            "result_count_truncated": result_count_truncated,
            "context_bytes_truncated": context_bytes_truncated,
            "selected_bytes": selected_bytes,
            "max_results": max_results,
            "max_context_bytes": max_context_bytes,
        }
        return sha256_text(canonical_json(payload))

    @staticmethod
    def from_mapping(m: Any) -> "RetrievalResult":
        if not isinstance(m, Mapping):
            raise RagValidationError("retrieval result must be a mapping")
        _check_required_fields(m, RetrievalResult._KNOWN_FIELDS, "retrieval-result-v1")
        _check_no_unknown_fields(m, RetrievalResult._KNOWN_FIELDS, "retrieval-result-v1")
        selected_raw = m["selected"]
        if not isinstance(selected_raw, list):
            raise RagValidationError("selected must be a list")
        selected = tuple(RetrievalSelection.from_mapping(item) for item in selected_raw)
        return RetrievalResult(
            schema_version=_ensure_str(m["schema_version"], "schema_version"),
            retrieval_id=_ensure_str(m["retrieval_id"], "retrieval_id"),
            index_id=_ensure_str(m["index_id"], "index_id"),
            query_identity=_ensure_str(m["query_identity"], "query_identity"),
            query=_ensure_str(m["query"], "query"),
            selected=selected,
            result_count_truncated=_ensure_bool(
                m["result_count_truncated"], "result_count_truncated"
            ),
            context_bytes_truncated=_ensure_bool(
                m["context_bytes_truncated"], "context_bytes_truncated"
            ),
            selected_bytes=_ensure_int(m["selected_bytes"], "selected_bytes", 0, 10**9),
            max_results=_ensure_int(m["max_results"], "max_results", 1, MAX_RETRIEVAL_RESULTS),
            max_context_bytes=_ensure_int(
                m["max_context_bytes"], "max_context_bytes", 1, 10**9
            ),
            latency_ms=(
                None
                if m["latency_ms"] is None
                else _ensure_int(m["latency_ms"], "latency_ms", 0, 10**9)
            ),
        )

    @staticmethod
    def from_text(text: str) -> "RetrievalResult":
        if type(text) is not str or not text:
            raise RagValidationError("retrieval text must be a non-empty string")
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise RagValidationError(f"retrieval result is not valid JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise RagValidationError("retrieval result must be a JSON object")
        return RetrievalResult.from_mapping(parsed)

    def to_text(self) -> str:
        return canonical_json(self.to_mapping()) + "\n"


__all__ = [
    "RAG_SCHEMA_VERSION",
    "RETRIEVAL_SCHEMA_VERSION",
    "MAX_INDEX_BYTES",
    "MAX_CHUNKS",
    "MAX_DOCUMENTS",
    "MAX_CHUNK_BYTES",
    "MAX_LINE_BYTES",
    "MAX_CHUNK_LINES",
    "CHUNK_WINDOW_LINES",
    "MAX_FILE_BYTES",
    "MAX_QUERY_BYTES",
    "MAX_RETRIEVAL_RESULTS",
    "MAX_RETRIEVAL_CONTEXT_BYTES",
    "MAX_RAG_CONTEXT_BYTES",
    "PUBLIC_REQUEST_BYTE_BUDGET",
    "MAX_FAILURE_DOC_BYTES",
    "RagError",
    "RagInputError",
    "RagValidationError",
    "canonical_json",
    "sha256_text",
    "query_identity_hash",
    "corpus_digest_of",
    "CorpusSourceKind",
    "CorpusDocument",
    "Chunk",
    "RepositoryIndex",
    "RetrievalSelection",
    "RetrievalResult",
]
