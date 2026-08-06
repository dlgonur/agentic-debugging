"""Deterministic, repository-native lexical RAG subsystem.

This package is stdlib-only and offline by construction.  It provides a
strict, versioned, self-contained repository index (``repository-index-v1``)
and a deterministic lexical retrieval result (``retrieval-result-v1``) that
can be replayed byte-for-byte from the index artifact alone.

The package deliberately contains no provider, network, vector-database or
model code.  The bounded, agent-facing context structure
(:class:`agentic_debugger.rag.context.RagContext`) is the only surface that
other packages consume.
"""

from agentic_debugger.rag.schema import (
    MAX_CHUNK_BYTES,
    MAX_CHUNK_LINES,
    MAX_CHUNKS,
    MAX_FILE_BYTES,
    MAX_INDEX_BYTES,
    MAX_LINE_BYTES,
    MAX_QUERY_BYTES,
    MAX_RAG_CONTEXT_BYTES,
    MAX_RETRIEVAL_CONTEXT_BYTES,
    MAX_RETRIEVAL_RESULTS,
    RAG_SCHEMA_VERSION,
    RETRIEVAL_SCHEMA_VERSION,
    Chunk,
    CorpusDocument,
    RepositoryIndex,
    RetrievalResult,
    RagError,
    RagInputError,
    RagValidationError,
    canonical_json,
)

__all__ = [
    "MAX_CHUNK_BYTES",
    "MAX_CHUNK_LINES",
    "MAX_CHUNKS",
    "MAX_FILE_BYTES",
    "MAX_INDEX_BYTES",
    "MAX_LINE_BYTES",
    "MAX_QUERY_BYTES",
    "MAX_RAG_CONTEXT_BYTES",
    "MAX_RETRIEVAL_CONTEXT_BYTES",
    "MAX_RETRIEVAL_RESULTS",
    "RAG_SCHEMA_VERSION",
    "RETRIEVAL_SCHEMA_VERSION",
    "Chunk",
    "CorpusDocument",
    "RepositoryIndex",
    "RetrievalResult",
    "RagError",
    "RagInputError",
    "RagValidationError",
    "canonical_json",
]
