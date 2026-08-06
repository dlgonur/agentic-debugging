"""Building and loading of the deterministic repository index artifact.

The index is self-contained (chunk text embedded), revision-bound, and
strictly validated on load:

* schema ``repository-index-v1`` with no unknown fields;
* canonical JSON serialization with ``allow_nan=False``;
* stable index identity computed from the content payload;
* bounded chunk count, per-chunk bytes and total serialized bytes;
* fail-closed for stale revision, malformed schema, duplicate identities,
  unsupported paths, an oversized corpus and undecodable input (the latter
  two are enforced at corpus/chunking time).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from agentic_debugger.rag.chunking import ChunkingError, chunk_document
from agentic_debugger.rag.corpus import Corpus, CorpusError, build_corpus
from agentic_debugger.rag.schema import (
    MAX_CHUNKS,
    MAX_INDEX_BYTES,
    RAG_SCHEMA_VERSION,
    Chunk,
    CorpusDocument,
    RagValidationError,
    RepositoryIndex,
    canonical_json,
    sha256_text,
)


class IndexError(RagValidationError):
    """Raised when an index cannot be built or loaded."""


class StaleRevisionError(IndexError):
    """Raised when an index is bound to a different revision than required."""


def build_index(
    corpus: Corpus,
    *,
    revision: str,
    repository_identity: str,
    task_id: Optional[str] = None,
    max_bytes: int = MAX_INDEX_BYTES,
) -> RepositoryIndex:
    """Build a validated index artifact from a corpus.

    ``revision`` is the repository revision the corpus was read from (e.g. the
    enclosing repository's Git HEAD); ``repository_identity`` names the
    source tree.  For a fixture-scoped corpus the ``task_id`` binding must be
    consistent with the corpus.
    """

    if type(revision) is not str or not revision.strip():
        raise IndexError("revision must be a non-empty string")
    if type(repository_identity) is not str or not repository_identity.strip():
        raise IndexError("repository_identity must be a non-empty string")
    if not isinstance(corpus, Corpus):
        raise IndexError("corpus must be a Corpus")

    bound_task = task_id if task_id is not None else corpus.task_id
    if corpus.task_id is not None and task_id is not None and corpus.task_id != task_id:
        raise IndexError(
            f"task_id {task_id!r} does not match corpus binding {corpus.task_id!r}"
        )
    if corpus.mode == "repo" and bound_task is not None:
        raise IndexError("repo-mode indexes cannot carry a task binding")

    chunks: List[Chunk] = []
    for document in corpus.documents:
        try:
            chunks.extend(chunk_document(document))
        except ChunkingError as exc:
            raise IndexError(f"cannot chunk {document.path!r}: {exc}") from exc
    if len(chunks) > MAX_CHUNKS:
        raise IndexError(f"index exceeds the chunk cap of {MAX_CHUNKS}")
    if len(chunks) == 0:
        raise IndexError("corpus produced no chunks; refusing an empty index")

    seen: set[str] = set()
    for chunk in chunks:
        if chunk.chunk_id in seen:
            raise IndexError(f"duplicate chunk identity: {chunk.chunk_id}")
        seen.add(chunk.chunk_id)

    documents = tuple(corpus.documents)
    payload = {
        "schema_version": RAG_SCHEMA_VERSION,
        "mode": corpus.mode,
        "corpus_digest": corpus.digest,
        "revision": revision,
        "repository_identity": repository_identity,
        "task_id": bound_task,
        "documents": [doc.to_mapping() for doc in documents],
        "chunks": [chunk.to_mapping() for chunk in chunks],
    }
    serialized = canonical_json(payload)
    index_id = sha256_text(serialized)
    index = RepositoryIndex(
        schema_version=RAG_SCHEMA_VERSION,
        index_id=index_id,
        mode=corpus.mode,
        corpus_digest=corpus.digest,
        revision=revision,
        repository_identity=repository_identity,
        task_id=bound_task,
        documents=documents,
        chunks=tuple(chunks),
    )
    # Prove byte stability: the dataclass identity payload (which excludes
    # the derived index_id) must serialize to the identical canonical bytes.
    if canonical_json(index._identity_payload()) != serialized:
        raise IndexError("index serialization is not stable")
    # Enforce the final artifact cap including the derived index_id.
    final_size = len(index_to_text(index).encode("utf-8"))
    if final_size > max_bytes:
        raise IndexError(
            f"serialized index (including index_id) exceeds the {max_bytes}-byte cap"
        )
    return index


def index_to_text(index: RepositoryIndex) -> str:
    """Canonical serialized artifact (byte-stable)."""

    if not isinstance(index, RepositoryIndex):
        raise IndexError("index must be a RepositoryIndex")
    return canonical_json(index.to_mapping()) + "\n"


def load_index(text: str, *, expected_revision: Optional[str] = None) -> RepositoryIndex:
    """Strictly validate and load an index artifact from text.

    Enforces the final artifact byte cap (including ``index_id``) before
    parsing.  When ``expected_revision`` is supplied and differs from the
    artifact's revision, :class:`StaleRevisionError` is raised (fail-closed).
    """

    if type(text) is not str or not text:
        raise IndexError("index text must be a non-empty string")
    if len(text.encode("utf-8")) > MAX_INDEX_BYTES:
        raise IndexError(
            f"index text exceeds the {MAX_INDEX_BYTES}-byte cap"
        )
    try:
        index = RepositoryIndex.from_text(text)
    except RagValidationError as exc:
        raise IndexError(str(exc)) from exc
    if expected_revision is not None:
        if type(expected_revision) is not str or not expected_revision:
            raise IndexError("expected_revision must be a non-empty string")
        if index.revision != expected_revision:
            raise StaleRevisionError(
                f"index revision {index.revision!r} does not match expected "
                f"revision {expected_revision!r}"
            )
    return index


def load_index_file(path: str, *, expected_revision: Optional[str] = None) -> RepositoryIndex:
    """Strictly validate and load an index artifact from a file."""

    if type(path) is not str or not path:
        raise IndexError("path must be a non-empty string")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise IndexError(f"index file could not be read: {exc}") from None
    return load_index(text, expected_revision=expected_revision)


__all__ = [
    "IndexError",
    "StaleRevisionError",
    "build_index",
    "index_to_text",
    "load_index",
    "load_index_file",
]
