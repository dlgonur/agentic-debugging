"""Native comparison mode: parity, retrieval recording, query and fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_debugger.comparison.native import (
    NativeError,
    build_comment_append_patch,
    build_task_query,
    check_native_parity,
    native_case_to_attempt,
    reference_patch_for,
    run_native_attempt,
    synthetic_demo_artifact,
)
from agentic_debugger.comparison.schema import AttemptRecord
from agentic_debugger.demo.model import DemoPolicyModel
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.demo.runner import run_demo_case
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.rag.corpus import build_corpus
from agentic_debugger.rag.indexer import build_index
from agentic_debugger.rag.retrieval import retrieve
from agentic_debugger.rag.schema import MAX_QUERY_BYTES

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-off-by-one-002"


def test_build_task_query_bounds_and_truncation_marker():
    task = DebugTask.from_file(str(FIXTURES / TASK_ID / "task.json"))
    query = build_task_query(task, "E   assert [10, 20, 30] == [10, 20, 30, 40]\n")
    assert "recent_window" in query or "description" in query
    assert len(query.encode("utf-8")) <= MAX_QUERY_BYTES
    huge_failure = "E   assert x\n" * 100000
    bounded = build_task_query(task, huge_failure)
    assert len(bounded.encode("utf-8")) <= MAX_QUERY_BYTES
    assert "[query-truncated]" in bounded


def test_comment_append_patch_is_a_real_unified_diff():
    source = "def f():\n    return 1\n"
    patch = build_comment_append_patch(source, "f.py")
    assert patch.startswith("--- a/f.py")
    assert "+++ b/f.py" in patch
    assert "offline-deterministic-demo" in patch
    with pytest.raises(NativeError):
        build_comment_append_patch(source, "bad\\path.py")


def test_synthetic_artifact_is_clearly_labeled():
    artifact = synthetic_demo_artifact(
        experiment_id="exp",
        attempt_id="a1",
        condition_id="base",
        task_id=TASK_ID,
        model_revision="rev1",
        adapter_identity="demo-lora-adapter",
        patch="--- a/x\n+++ b/x\n",
        generation_note="test note",
    )
    assert artifact.provenance["generator"] == "offline-deterministic-demo"
    assert artifact.adapter_identity == "demo-lora-adapter"
    assert artifact.model_repository == "offline-deterministic-demo"
    assert artifact.generation_config["synthetic"] is True


def test_reference_patch_renders_from_canonical_bytes():
    patch = reference_patch_for(str(ROOT), TASK_ID)
    assert "diff" in patch.lower() or patch.startswith("---")


def _rag_context():
    corpus = build_corpus(str(FIXTURES / TASK_ID), mode="fixture", task_id=TASK_ID)
    index = build_index(corpus, revision="rev1", repository_identity="r", task_id=TASK_ID)
    result = retrieve(
        index, "recent_window sequence length", expected_revision="rev1"
    )
    from agentic_debugger.rag.context import RagContext

    return RagContext.from_retrieval(result, retrieval_latency_ms=2)


def test_demo_model_accepts_rag_context_and_records_it_without_changing_directives():
    from agentic_debugger.demo.catalog import scenario_for
    from agentic_debugger.demo.policies import pdb_policy_for

    scenario = scenario_for(TASK_ID)
    patch = reference_patch_for(str(ROOT), TASK_ID)
    pdb_mode = pdb_policy_for(DemoPolicy.STATIC_BASELINE)
    plain = DemoPolicyModel(scenario=scenario, patch=patch, pdb_policy=pdb_mode)
    rag = DemoPolicyModel(
        scenario=scenario,
        patch=patch,
        pdb_policy=pdb_mode,
        rag_context=_rag_context(),
    )
    assert plain.retrieval_record is None
    assert rag.retrieval_record is not None
    assert rag.retrieval_record["chunk_count"] >= 1
    assert "chunk_ids" in rag.retrieval_record
    with pytest.raises(Exception):
        DemoPolicyModel(
            scenario=scenario, patch=patch, pdb_policy=pdb_mode,
            rag_context=object(),
        )


def test_native_parity_check_enforces_rag_only_difference():
    base = {
        "patch_sha256": "p",
        "verifier_outcome": "RESOLVED",
        "valid_patch": True,
        "retrieval_count": None,
    }
    rag = {
        "patch_sha256": "p",
        "verifier_outcome": "RESOLVED",
        "valid_patch": True,
        "retrieval_count": 3,
    }
    check_native_parity(base, rag)  # OK
    broken = dict(rag)
    broken["patch_sha256"] = "q"
    with pytest.raises(NativeError):
        check_native_parity(base, broken)
    with pytest.raises(NativeError):
        check_native_parity(base, dict(base))  # no RAG-enabled side


def test_native_attempt_runs_through_the_real_path_and_records_retrieval(tmp_path: Path):
    """One native task, both conditions: parity and retrieval recording."""
    patch = reference_patch_for(str(ROOT), TASK_ID)
    agentic = run_native_attempt(
        repository_root=str(ROOT),
        task_id=TASK_ID,
        condition_id="agentic",
        workspace_parent=str(tmp_path),
        response_text=patch,
    )
    rag = run_native_attempt(
        repository_root=str(ROOT),
        task_id=TASK_ID,
        condition_id="rag-assisted",
        workspace_parent=str(tmp_path),
        response_text=patch,
        rag_context=_rag_context(),
    )
    check_native_parity(agentic, rag)
    assert agentic["verifier_outcome"] == "RESOLVED"
    assert agentic["valid_patch"] is True
    assert agentic["replay_valid"] is True
    assert agentic["cleanup_status"] == "cleaned"
    assert agentic["canonical_fixture_unchanged"] is True
    assert agentic["provider_attempts"] == 0
    assert agentic["network_attempts"] == 0
    assert agentic["retrieval_count"] is None
    assert rag["retrieval_count"] >= 1
    assert rag["retrieval_bytes"] >= 1
    assert rag["retrieval_latency_ms"] == 2
    AttemptRecord.from_mapping(agentic)
    AttemptRecord.from_mapping(rag)


def test_default_demo_case_record_has_no_retrieval_key(tmp_path: Path):
    case = run_demo_case(
        repository_root=str(ROOT),
        task_id=TASK_ID,
        policy=DemoPolicy.STATIC_BASELINE,
        workspace_parent=str(tmp_path),
    )
    mapping = case.to_mapping()
    assert "retrieval" not in mapping
