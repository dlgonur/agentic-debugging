"""Retrieval determinism, scoring, budgets and identity tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_debugger.rag.corpus import build_corpus
from agentic_debugger.rag.indexer import StaleRevisionError, build_index
from agentic_debugger.rag.retrieval import (
    RetrievalError,
    query_identity,
    retrieve,
    tokenize,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-off-by-one-002"


@pytest.fixture
def index():
    corpus = build_corpus(str(FIXTURES / TASK_ID), mode="fixture", task_id=TASK_ID)
    return build_index(corpus, revision="rev1", repository_identity="repo@rev1", task_id=TASK_ID)


def test_tokenize_is_identifier_aware_and_case_insensitive():
    assert tokenize("RecentWindow recent_window 123") == (
        "recentwindow",
        "recent_window",
        "123",
    )


def test_retrieval_is_deterministic(index):
    query = "recent_window sequence length off by one"
    a = retrieve(index, query, expected_revision="rev1")
    b = retrieve(index, query, expected_revision="rev1")
    assert a.retrieval_id == b.retrieval_id
    assert a.to_mapping() == b.to_mapping()
    assert a.selected_bytes == sum(item.bytes for item in a.selected)


def test_retrieval_identity_excludes_latency(index):
    query = "recent_window sequence length"
    a = retrieve(index, query, expected_revision="rev1", latency_ms=None)
    b = retrieve(index, query, expected_revision="rev1", latency_ms=123)
    assert a.retrieval_id == b.retrieval_id
    assert a.to_mapping()["latency_ms"] is None
    assert b.to_mapping()["latency_ms"] == 123


def test_retrieval_returns_relevant_source_first(index):
    query = "recent_window sequence_length"
    result = retrieve(index, query, expected_revision="rev1")
    assert len(result.selected) >= 1
    # The source symbol chunk should outrank unrelated content for its own
    # symbol name.
    assert result.selected[0].path == "recent_window.py"


def test_query_identity_is_stable():
    assert query_identity("a b") == query_identity("a b")
    assert query_identity("a b") != query_identity("b a")


def test_retrieval_stale_revision_fails_closed(index):
    with pytest.raises(StaleRevisionError):
        retrieve(index, "query", expected_revision="other")


def test_empty_or_oversized_query_fails_closed(index):
    with pytest.raises(RetrievalError):
        retrieve(index, "   ", expected_revision="rev1")
    from agentic_debugger.rag.schema import MAX_QUERY_BYTES

    with pytest.raises(RetrievalError):
        retrieve(index, "x" * (MAX_QUERY_BYTES + 1), expected_revision="rev1")


def test_max_results_budget_sets_truncation_flag(index):
    result = retrieve(
        index,
        "recent_window sequence length value",
        expected_revision="rev1",
        max_results=1,
    )
    assert len(result.selected) == 1
    assert result.result_count_truncated is True


def test_context_byte_budget_sets_truncation_flag(index):
    result = retrieve(
        index,
        "recent_window sequence length value test",
        expected_revision="rev1",
        max_context_bytes=900,
    )
    assert 1 <= len(result.selected)
    assert result.selected_bytes <= 900
    assert result.context_bytes_truncated is True or len(result.selected) == 1


def test_single_oversized_chunk_fails_closed(index):
    with pytest.raises(RetrievalError):
        retrieve(
            index,
            "recent_window sequence length",
            expected_revision="rev1",
            max_context_bytes=1,
        )


def test_dedup_by_chunk_identity(index):
    result = retrieve(
        index, "recent_window", expected_revision="rev1", max_results=8
    )
    ids = [item.chunk_id for item in result.selected]
    assert len(ids) == len(set(ids))


def test_tie_order_is_deterministic(index):
    query = "recent_window"
    a = retrieve(index, query, expected_revision="rev1")
    order_a = [(s.path, s.start_line, s.chunk_id) for s in a.selected]
    b = retrieve(index, query, expected_revision="rev1")
    assert order_a == [(s.path, s.start_line, s.chunk_id) for s in b.selected]


def test_selected_chunks_carry_full_provenance(index):
    result = retrieve(index, "recent_window", expected_revision="rev1")
    for item in result.selected:
        assert item.chunk_id
        assert item.path
        assert 1 <= item.start_line <= item.end_line
        assert item.score >= 1
        assert item.bytes == len(item.text.encode("utf-8"))
        assert item.text


def test_selections_are_verified_against_the_bound_index(index):
    from agentic_debugger.rag.retrieval import verify_retrieval_against_index

    result = retrieve(index, "recent_window", expected_revision="rev1")
    verify_retrieval_against_index(result, index)  # must not raise
    tampered = result.to_mapping()
    tampered["selected"][0]["text"] += "\n# tampered\n"
    from agentic_debugger.rag.schema import RetrievalResult

    # The tampered mapping fails identity recompute before index check.
    with pytest.raises(Exception):
        RetrievalResult.from_mapping(tampered)


def test_tampered_retrieval_id_is_rejected_on_load(index):
    result = retrieve(index, "recent_window", expected_revision="rev1")
    parsed = result.to_mapping()
    parsed["retrieval_id"] = "0" * 64
    from agentic_debugger.rag.schema import RetrievalResult

    with pytest.raises(Exception):
        RetrievalResult.from_mapping(parsed)
