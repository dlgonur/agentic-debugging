"""RagContext bounds, request mapping and public-request budget tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_debugger.rag.context import (
    PUBLIC_REQUEST_BYTE_BUDGET,
    RagChunkRef,
    RagContext,
    RagContextError,
)
from agentic_debugger.rag.corpus import build_corpus
from agentic_debugger.rag.indexer import build_index
from agentic_debugger.rag.retrieval import retrieve
from agentic_debugger.rag.schema import MAX_RAG_CONTEXT_BYTES, canonical_json

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-off-by-one-002"


@pytest.fixture
def retrieval_result():
    corpus = build_corpus(str(FIXTURES / TASK_ID), mode="fixture", task_id=TASK_ID)
    index = build_index(corpus, revision="rev1", repository_identity="r", task_id=TASK_ID)
    return retrieve(
        index,
        "recent_window sequence length",
        expected_revision="rev1",
        latency_ms=7,
    )


def test_context_from_retrieval_is_bounded_and_identity_stable(retrieval_result):
    ctx = RagContext.from_retrieval(retrieval_result, retrieval_latency_ms=7)
    assert ctx.selected_bytes == sum(c.text_bytes for c in ctx.chunks)
    assert ctx.selected_bytes <= MAX_RAG_CONTEXT_BYTES
    assert ctx.retrieval_id == retrieval_result.retrieval_id
    assert ctx.context_identity() == RagContext.from_retrieval(
        retrieval_result, retrieval_latency_ms=999
    ).context_identity()  # latency never affects identity


def test_context_duplicate_chunks_are_rejected():
    chunk = RagChunkRef("c" * 64, "a.py", 1, 2, 3, "text\n")
    with pytest.raises(RagContextError):
        RagContext(
            index_id="i",
            query_identity="q" * 64,
            retrieval_id="r" * 64,
            query="query",
            chunks=(chunk, chunk),
            selected_bytes=chunk.text_bytes * 2,
            truncated=False,
            retrieval_latency_ms=None,
        )


def test_chunk_ref_validation_is_strict():
    with pytest.raises(RagContextError):
        RagChunkRef("not-a-hash", "a.py", 1, 2, 3, "text\n")
    with pytest.raises(RagContextError):
        RagChunkRef("c" * 64, "../a.py", 1, 2, 3, "text\n")
    with pytest.raises(RagContextError):
        RagChunkRef("c" * 64, "a.py", 3, 2, 3, "text\n")
    with pytest.raises(RagContextError):
        RagChunkRef("c" * 64, "a.py", 0, 2, 3, "text\n")
    with pytest.raises(RagContextError):
        RagChunkRef("c" * 64, "a.py", 1, 2, -1, "text\n")
    with pytest.raises(RagContextError):
        RagChunkRef("c" * 64, "a.py", 1, 2, 3, "")
    from agentic_debugger.rag.schema import MAX_CHUNK_BYTES

    with pytest.raises(RagContextError):
        RagChunkRef("c" * 64, "a.py", 1, 2, 3, "x" * (MAX_CHUNK_BYTES + 1))


def test_context_byte_budget_is_enforced():
    chunk_a = RagChunkRef("c" * 64, "a.py", 1, 1, 1, "x" * 3000)
    chunk_b = RagChunkRef("d" * 64, "b.py", 1, 1, 1, "y" * 3000)
    total = chunk_a.text_bytes + chunk_b.text_bytes
    assert total > MAX_RAG_CONTEXT_BYTES
    with pytest.raises(RagContextError):
        RagContext(
            index_id="i",
            query_identity="q" * 64,
            retrieval_id="r" * 64,
            query="query",
            chunks=(chunk_a, chunk_b),
            selected_bytes=total,
            truncated=False,
            retrieval_latency_ms=None,
        )


def test_context_rejects_non_ragchunkref_entries():
    chunk = RagChunkRef("c" * 64, "a.py", 1, 1, 1, "text\n")
    with pytest.raises(RagContextError):
        RagContext(
            index_id="i",
            query_identity="q" * 64,
            retrieval_id="r" * 64,
            query="query",
            chunks=(chunk, {"chunk_id": "x"}),  # lookalike entry
            selected_bytes=chunk.text_bytes,
            truncated=False,
            retrieval_latency_ms=None,
        )


def test_context_requires_valid_retrieval_identity():
    chunk = RagChunkRef("c" * 64, "a.py", 1, 1, 1, "text\n")
    with pytest.raises(RagContextError):
        RagContext(
            index_id="i",
            query_identity="q" * 64,
            retrieval_id="not-a-hash",
            query="query",
            chunks=(chunk,),
            selected_bytes=chunk.text_bytes,
            truncated=False,
            retrieval_latency_ms=None,
        )


def test_context_selected_bytes_must_match():
    chunk = RagChunkRef("c" * 64, "a.py", 1, 1, 1, "text\n")
    with pytest.raises(RagContextError):
        RagContext(
            index_id="i",
            query_identity="q" * 64,
            retrieval_id="r" * 64,
            query="query",
            chunks=(chunk,),
            selected_bytes=999,
            truncated=False,
            retrieval_latency_ms=None,
        )


def test_request_mapping_shape_and_budget(retrieval_result):
    ctx = RagContext.from_retrieval(retrieval_result, retrieval_latency_ms=7)
    mapping = ctx.to_request_mapping()
    assert set(mapping) == {
        "index_id", "query_identity", "retrieval_id", "query", "chunks",
        "selected_bytes", "truncated",
    }
    serialized = canonical_json(mapping)
    assert len(serialized.encode("utf-8")) <= PUBLIC_REQUEST_BYTE_BUDGET
    for chunk in mapping["chunks"]:
        assert set(chunk) == {"chunk_id", "path", "start_line", "end_line", "score", "text"}


def test_record_mapping_carries_identities_without_text(retrieval_result):
    ctx = RagContext.from_retrieval(retrieval_result, retrieval_latency_ms=7)
    record = ctx.to_record_mapping()
    assert record["chunk_count"] == len(ctx.chunks)
    assert len(record["chunk_ids"]) == record["chunk_count"]
    assert "text" not in canonical_json(record)
    assert record["retrieval_latency_ms"] == 7
    assert record["index_id"] == retrieval_result.index_id


def test_context_from_mapping_accepts_validated_dict(retrieval_result):
    ctx = RagContext.from_retrieval(retrieval_result, retrieval_latency_ms=7)
    rebuilt = RagContext.from_retrieval(
        retrieval_result.to_mapping(), retrieval_latency_ms=7
    )
    assert rebuilt.context_identity() == ctx.context_identity()


def test_empty_context_is_rejected():
    with pytest.raises(RagContextError):
        RagContext(
            index_id="i",
            query_identity="q" * 64,
            retrieval_id="r" * 64,
            query="query",
            chunks=(),
            selected_bytes=0,
            truncated=False,
            retrieval_latency_ms=None,
        )
