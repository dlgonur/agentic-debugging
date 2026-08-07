"""Strict schema and canonical-identity tests for the RAG subsystem.

Covers the repair-1 integrity hardening: recomputed index identity, corpus
digest, chunk identities, document uniqueness, chunk→document binding,
query/retrieval identities, selection byte counts and the declared caps —
with tampering tests for every identity field.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_debugger.rag.schema import (
    MAX_CHUNK_BYTES,
    MAX_CHUNKS,
    MAX_DOCUMENTS,
    RAG_SCHEMA_VERSION,
    RETRIEVAL_SCHEMA_VERSION,
    Chunk,
    CorpusDocument,
    RagValidationError,
    RepositoryIndex,
    RetrievalResult,
    RetrievalSelection,
    canonical_json,
    corpus_digest_of,
    query_identity_hash,
    sha256_text,
)

ROOT = Path(__file__).resolve().parents[2]


def _source_doc(path: str = "a.py", text: str = "def a():\n    return a\n") -> CorpusDocument:
    return CorpusDocument(
        document_id=f"source:{path}", kind="source", path=path, text=text
    )


def _chunk(doc: CorpusDocument = None, seed: str = "x", start: int = 1, end: int = 1) -> Chunk:
    doc = doc if doc is not None else _source_doc()
    text = f"def {seed}():\n    return {seed}\n"
    return Chunk(
        chunk_id=Chunk.identity(doc.document_id, doc.path, start, end, text),
        document_id=doc.document_id,
        kind=doc.kind,
        path=doc.path,
        start_line=start,
        end_line=end,
        text=text,
    )


def _index_mapping(**overrides):
    doc = _source_doc()
    chunk = _chunk(doc)
    mapping = {
        "schema_version": RAG_SCHEMA_VERSION,
        "index_id": "0" * 64,
        "mode": "fixture",
        "corpus_digest": corpus_digest_of((doc,)),
        "revision": "rev1",
        "repository_identity": "repo@rev1",
        "task_id": "curated-none-handling-001",
        "documents": [doc.to_mapping()],
        "chunks": [chunk.to_mapping()],
    }
    mapping.update(overrides)
    return mapping


def _valid_index_mapping(**overrides):
    mapping = _index_mapping(**overrides)
    mapping["index_id"] = sha256_text(canonical_json(
        {k: v for k, v in mapping.items() if k != "index_id"}
    ))
    return mapping


def test_canonical_json_is_byte_stable_and_sorted():
    a = {"b": 1, "a": [2, {"z": 3}], "c": "x"}
    b = {"c": "x", "a": [2, {"z": 3}], "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert canonical_json(a) == '{"a":[2,{"z":3}],"b":1,"c":"x"}'


def test_canonical_json_rejects_non_finite():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
    with pytest.raises(ValueError):
        canonical_json({"x": float("inf")})


def test_chunk_identity_is_deterministic_and_content_bound():
    doc = _source_doc()
    a = _chunk(doc, seed="alpha")
    b = _chunk(doc, seed="alpha")
    c = _chunk(doc, seed="beta")
    assert a.chunk_id == b.chunk_id
    assert a.chunk_id != c.chunk_id
    assert a.chunk_id == Chunk.identity(doc.document_id, doc.path, 1, 1, a.text)


def test_chunk_validates_provenance_and_bounds():
    with pytest.raises(RagValidationError):
        Chunk(chunk_id="c", document_id="d", kind="source", path="a.py",
              start_line=3, end_line=2, text="x\n")
    with pytest.raises(RagValidationError):
        Chunk(chunk_id="c", document_id="d", kind="nope", path="a.py",
              start_line=1, end_line=1, text="x\n")
    with pytest.raises(RagValidationError):
        Chunk(chunk_id="c", document_id="d", kind="source", path="../a.py",
              start_line=1, end_line=1, text="x\n")
    with pytest.raises(RagValidationError):
        Chunk(chunk_id="c", document_id="d", kind="source", path="a.py",
              start_line=1, end_line=1, text="x" * (MAX_CHUNK_BYTES + 1))
    with pytest.raises(RagValidationError):
        Chunk(chunk_id="not-a-hash", document_id="d", kind="source", path="a.py",
              start_line=1, end_line=1, text="x\n")


def test_index_rejects_unknown_and_missing_fields():
    mapping = _valid_index_mapping()
    mapping["extra"] = True
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)
    del mapping["extra"]
    del mapping["revision"]
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)


def test_index_rejects_wrong_schema_version():
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(_index_mapping(schema_version="repository-index-v0"))


def test_index_rejects_duplicate_chunk_identities():
    chunk = _chunk()
    mapping = _valid_index_mapping(chunks=[chunk.to_mapping(), dict(chunk.to_mapping())])
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)


def test_index_rejects_duplicate_document_identities():
    doc = _source_doc()
    mapping = _valid_index_mapping(
        documents=[doc.to_mapping(), dict(doc.to_mapping())]
    )
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)


def test_index_rejects_chunk_referencing_unknown_document():
    mapping = _valid_index_mapping()
    mapping["chunks"][0]["document_id"] = "source:missing.py"
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)


def test_index_rejects_chunk_kind_path_disagreement():
    mapping = _valid_index_mapping()
    mapping["chunks"][0]["kind"] = "test"
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)
    mapping = _valid_index_mapping()
    mapping["chunks"][0]["path"] = "other.py"
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)


def test_index_rejects_tampered_chunk_identity():
    mapping = _valid_index_mapping()
    mapping["chunks"][0]["chunk_id"] = "f" * 64
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)
    mapping = _valid_index_mapping()
    mapping["chunks"][0]["text"] = "def tampered():\n    return 1\n"
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)


def test_index_rejects_tampered_corpus_digest():
    mapping = _valid_index_mapping()
    mapping["corpus_digest"] = "e" * 64
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)


def test_index_rejects_tampered_index_id():
    mapping = _valid_index_mapping()
    mapping["index_id"] = "a" * 64
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)


def test_index_rejects_tampered_revision_and_mode():
    mapping = _valid_index_mapping()
    mapping["revision"] = "other-revision"
    with pytest.raises(RagValidationError):  # identity recompute fails
        RepositoryIndex.from_mapping(mapping)
    mapping = _valid_index_mapping()
    mapping["mode"] = "repo"
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)


def test_index_document_cap_is_enforced():
    doc = _source_doc()
    many = [dict(doc.to_mapping()) for _ in range(MAX_DOCUMENTS + 1)]
    mapping = _valid_index_mapping(documents=many)
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(mapping)


def test_index_chunk_cap_is_enforced():
    chunk = _chunk()
    many = [dict(chunk.to_mapping()) for _ in range(MAX_CHUNKS + 1)]
    with pytest.raises(RagValidationError):
        RepositoryIndex.from_mapping(_valid_index_mapping(chunks=many))


def test_index_round_trip_preserves_content_and_identity():
    mapping = _valid_index_mapping()
    index = RepositoryIndex.from_mapping(mapping)
    text = canonical_json(index.to_mapping())
    reloaded = RepositoryIndex.from_text(text)
    assert reloaded.index_id == index.index_id
    assert reloaded.revision == "rev1"
    assert reloaded.task_id == "curated-none-handling-001"
    assert reloaded.chunks == index.chunks


def _retrieval_mapping_helpers():
    def selection(text: str = "def f():\n    pass\n") -> dict:
        chunk_id = Chunk.identity("source:a.py", "a.py", 1, 2, text)
        return {
            "chunk_id": chunk_id,
            "path": "a.py",
            "start_line": 1,
            "end_line": 2,
            "score": 3,
            "bytes": len(text.encode("utf-8")),
            "text": text,
        }

    sel = selection()
    query = "def f"
    base = {
        "schema_version": RETRIEVAL_SCHEMA_VERSION,
        "retrieval_id": "0" * 64,
        "index_id": "1" * 64,
        "query_identity": query_identity_hash(query),
        "query": query,
        "selected": [sel],
        "result_count_truncated": False,
        "context_bytes_truncated": False,
        "selected_bytes": sel["bytes"],
        "max_results": 8,
        "max_context_bytes": 4096,
        "latency_ms": None,
    }
    payload = {k: v for k, v in base.items() if k not in ("retrieval_id", "latency_ms")}
    base["retrieval_id"] = sha256_text(canonical_json(payload))
    return base, sel


def test_retrieval_result_round_trip_and_strictness():
    mapping, _ = _retrieval_mapping_helpers()
    result = RetrievalResult.from_mapping(mapping)
    assert result.retrieval_id != "0" * 64
    reloaded = RetrievalResult.from_text(result.to_text())
    assert reloaded.retrieval_id == result.retrieval_id
    mapping["extra"] = 1
    with pytest.raises(RagValidationError):
        RetrievalResult.from_mapping(mapping)


def test_arbitrary_retrieval_id_must_not_load():
    """A mapping with an arbitrary ``"0"*64`` retrieval id must not load."""
    mapping, _ = _retrieval_mapping_helpers()
    mapping["retrieval_id"] = "0" * 64
    with pytest.raises(RagValidationError):
        RetrievalResult.from_mapping(mapping)


def test_retrieval_rejects_tampered_query_identity():
    mapping, _ = _retrieval_mapping_helpers()
    mapping["query_identity"] = "9" * 64
    with pytest.raises(RagValidationError):
        RetrievalResult.from_mapping(mapping)
    mapping, _ = _retrieval_mapping_helpers()
    mapping["query"] = "tampered query"
    with pytest.raises(RagValidationError):
        RetrievalResult.from_mapping(mapping)


def test_retrieval_rejects_tampered_selection_bytes():
    mapping, _ = _retrieval_mapping_helpers()
    mapping["selected"][0]["bytes"] += 1
    with pytest.raises(RagValidationError):
        RetrievalResult.from_mapping(mapping)


def test_retrieval_rejects_selection_count_over_cap():
    mapping, sel = _retrieval_mapping_helpers()
    mapping["selected"] = [dict(sel), dict(sel)]
    mapping["selected"][1]["text"] = "def g():\n    pass\n"
    mapping["selected"][1]["chunk_id"] = Chunk.identity(
        "source:a.py", "a.py", 1, 2, mapping["selected"][1]["text"]
    )
    mapping["selected"][1]["bytes"] = len(mapping["selected"][1]["text"].encode("utf-8"))
    mapping["selected_bytes"] = sum(s["bytes"] for s in mapping["selected"])
    mapping["max_results"] = 1
    with pytest.raises(RagValidationError):
        RetrievalResult.from_mapping(mapping)


def test_retrieval_rejects_total_bytes_over_context_cap():
    mapping, _ = _retrieval_mapping_helpers()
    mapping["max_context_bytes"] = mapping["selected_bytes"] - 1
    with pytest.raises(RagValidationError):
        RetrievalResult.from_mapping(mapping)


def test_retrieval_rejects_duplicate_selections():
    mapping, _ = _retrieval_mapping_helpers()
    mapping["selected"] = [dict(mapping["selected"][0]), dict(mapping["selected"][0])]
    mapping["selected_bytes"] = mapping["selected"][0]["bytes"] * 2
    with pytest.raises(RagValidationError):
        RetrievalResult.from_mapping(mapping)


def test_retrieval_rejects_wrong_selected_bytes_sum():
    mapping, _ = _retrieval_mapping_helpers()
    mapping["selected_bytes"] += 1
    with pytest.raises(RagValidationError):
        RetrievalResult.from_mapping(mapping)


def test_document_identity_and_kind_validation():
    doc = CorpusDocument.from_mapping(
        {"document_id": "source:a.py", "kind": "source", "path": "a.py", "text": "x\n"}
    )
    assert doc.kind == "source"
    empty = CorpusDocument.from_mapping(
        {"document_id": "source:empty.py", "kind": "source", "path": "empty.py", "text": ""}
    )
    assert empty.text == ""
    with pytest.raises(RagValidationError):
        CorpusDocument.from_mapping(
            {"document_id": "source:a.py", "kind": "nope", "path": "a.py", "text": "x\n"}
        )
    with pytest.raises(RagValidationError):
        CorpusDocument.from_mapping(
            {"document_id": "bad id!", "kind": "source", "path": "a.py", "text": "x\n"}
        )


def test_query_identity_hash_is_stable():
    assert query_identity_hash("a b") == query_identity_hash("a b")
    assert query_identity_hash("a b") != query_identity_hash("b a")
