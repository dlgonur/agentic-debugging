"""Index build/load tests: bounds, revision binding, fail-closed behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_debugger.rag.corpus import build_corpus
from agentic_debugger.rag.indexer import (
    IndexError,
    StaleRevisionError,
    build_index,
    index_to_text,
    load_index,
    load_index_file,
)
from agentic_debugger.rag.schema import (
    MAX_CHUNKS,
    MAX_INDEX_BYTES,
    RepositoryIndex,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-off-by-one-002"


@pytest.fixture
def corpus():
    return build_corpus(str(FIXTURES / TASK_ID), mode="fixture", task_id=TASK_ID)


def test_build_is_deterministic(corpus):
    a = build_index(corpus, revision="rev1", repository_identity="repo@rev1", task_id=TASK_ID)
    b = build_index(corpus, revision="rev1", repository_identity="repo@rev1", task_id=TASK_ID)
    assert a.index_id == b.index_id
    assert index_to_text(a) == index_to_text(b)
    assert a.index_id == a.index_id


def test_revision_is_bound_and_stale_load_fails_closed(corpus):
    index = build_index(corpus, revision="rev1", repository_identity="repo@rev1", task_id=TASK_ID)
    text = index_to_text(index)
    assert load_index(text, expected_revision="rev1").index_id == index.index_id
    with pytest.raises(StaleRevisionError):
        load_index(text, expected_revision="rev2")


def test_revision_change_changes_index_identity(corpus):
    a = build_index(corpus, revision="rev1", repository_identity="repo@rev1", task_id=TASK_ID)
    b = build_index(corpus, revision="rev2", repository_identity="repo@rev1", task_id=TASK_ID)
    assert a.index_id != b.index_id


def test_task_binding_consistency(corpus):
    with pytest.raises(IndexError):
        build_index(corpus, revision="r", repository_identity="i", task_id="curated-none-handling-001")
    index = build_index(corpus, revision="r", repository_identity="i", task_id=TASK_ID)
    assert index.task_id == TASK_ID
    assert index.mode == "fixture"


def test_repo_mode_rejects_task_binding(tmp_path: Path):
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    corpus = build_corpus(str(tmp_path), mode="repo")
    with pytest.raises(IndexError):
        build_index(corpus, revision="r", repository_identity="i", task_id=TASK_ID)


def test_empty_corpus_is_refused(tmp_path: Path):
    corpus = build_corpus(str(tmp_path), mode="repo")  # no documents
    with pytest.raises(IndexError):
        build_index(corpus, revision="r", repository_identity="i")


def test_missing_revision_is_rejected(corpus):
    with pytest.raises(IndexError):
        build_index(corpus, revision="", repository_identity="i", task_id=TASK_ID)


def test_malformed_schema_is_rejected(corpus):
    index = build_index(corpus, revision="r", repository_identity="i", task_id=TASK_ID)
    text = index_to_text(index)
    parsed = json.loads(text)
    parsed["schema_version"] = "repository-index-v0"
    with pytest.raises(IndexError):
        load_index(json.dumps(parsed))
    parsed2 = json.loads(text)
    del parsed2["chunks"]
    with pytest.raises(IndexError):
        load_index(json.dumps(parsed2))
    parsed3 = json.loads(text)
    parsed3["surprise"] = True
    with pytest.raises(IndexError):
        load_index(json.dumps(parsed3))


def test_duplicate_identity_in_artifact_is_rejected(corpus):
    index = build_index(corpus, revision="r", repository_identity="i", task_id=TASK_ID)
    parsed = json.loads(index_to_text(index))
    parsed["chunks"].append(dict(parsed["chunks"][0]))
    with pytest.raises(IndexError):
        load_index(json.dumps(parsed))


def test_index_size_cap_is_enforced(corpus):
    with pytest.raises(IndexError):
        build_index(
            corpus,
            revision="r",
            repository_identity="i",
            task_id=TASK_ID,
            max_bytes=100,
        )


def test_documented_max_index_bytes_is_large_enough_for_fixture(corpus):
    index = build_index(corpus, revision="r", repository_identity="i", task_id=TASK_ID)
    assert len(index_to_text(index).encode("utf-8")) <= MAX_INDEX_BYTES


def test_oversized_index_text_is_rejected_before_parse(corpus, tmp_path: Path):
    index = build_index(corpus, revision="r", repository_identity="i", task_id=TASK_ID)
    text = index_to_text(index)
    with pytest.raises(IndexError):
        load_index(text + "x" * (MAX_INDEX_BYTES + 1))


def test_final_artifact_size_includes_index_id(corpus):
    index = build_index(corpus, revision="r", repository_identity="i", task_id=TASK_ID)
    assert len(index_to_text(index).encode("utf-8")) <= MAX_INDEX_BYTES
    assert index.index_id in index_to_text(index)


def test_tampered_index_identity_field_is_rejected_on_load(corpus):
    index = build_index(corpus, revision="r", repository_identity="i", task_id=TASK_ID)
    parsed = json.loads(index_to_text(index))
    parsed["revision"] = "tampered"
    with pytest.raises(IndexError):
        load_index(json.dumps(parsed))
    parsed = json.loads(index_to_text(index))
    parsed["corpus_digest"] = "d" * 64
    with pytest.raises(IndexError):
        load_index(json.dumps(parsed))
    parsed = json.loads(index_to_text(index))
    parsed["chunks"][0]["text"] += "\n# tampered\n"
    with pytest.raises(IndexError):
        load_index(json.dumps(parsed))


def test_load_index_file_round_trip(corpus, tmp_path: Path):
    index = build_index(corpus, revision="r", repository_identity="i", task_id=TASK_ID)
    path = tmp_path / "repository-index-v1.json"
    path.write_text(index_to_text(index), encoding="utf-8", newline="\n")
    loaded = load_index_file(str(path), expected_revision="r")
    assert loaded.index_id == index.index_id
    with pytest.raises(IndexError):
        load_index_file(str(tmp_path / "missing.json"))
