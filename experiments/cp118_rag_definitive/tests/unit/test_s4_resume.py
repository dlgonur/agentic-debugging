"""S4 — resume/retry/provenance semantics tests (Repair Pass 1 + 2).

Deterministic, no model, no network.  Uses the real frozen manifest for
task identities and the real pair/resume/retry/identity helpers.

Covers (Repair Pass 2):
* Blocker 1 — the immutable run identity is created once and REUSED
  byte-identically across two invocations/resume;
* Blocker 2 — retries apply ONLY pre-completion to the model call;
  post-completion persistence/validation failures abort with exactly ONE
  model generation call;
* Blocker 3 — pair validity binds the immutable run identity and the
  retrieval evidence (parseable JSON, task_id, RAG provenance consistent
  with meta); stale/foreign pairs fail closed;
* Blocker 4 — eval source-state guard (dirty tree / mismatched
  identities fail closed).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.cp118_rag_definitive.s4_payload import MAX_NEW_TOKENS, sha256_text
from experiments.cp118_rag_definitive.s4_runner import (
    MODEL_ID,
    PROTOCOL_VERSION,
    RunnerError,
    TransportError,
    all_pairs_valid,
    build_run_identity,
    generate_and_persist_task,
    git_state,
    load_contract,
    pair_files,
    pair_is_valid,
    plan_resume,
    verify_eval_source_state,
    with_infra_retries,
    write_or_verify_run_identity,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
MANIFEST = (
    REPO_ROOT / "experiments/raw-pilot-v1.1/state/quix40-v1"
    / "pilot_manifest_frozen_v1.jsonl"
)


@pytest.fixture(scope="module")
def tasks():
    from experiments.cp118_rag_definitive.s4_corpus import validate_manifest

    return validate_manifest(MANIFEST)


def fake_rag_evidence(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "index_id": "i",
        "query_identity": "q-id",
        "retrieval_id": "r-id",
        "context_identity": "c-id",
        "selected_chunk_count": 1,
        "selected_bytes": 10,
        "retrieval_truncated": False,
        "retrieval_context_bytes_truncated": False,
        "context_truncated": False,
        "retrieval_latency_ms": None,
        "budget": {"retrieved_context_bytes": 100},
    }


def write_valid_pair(out_dir: Path, task, run_identity_sha256: str) -> None:
    """Write a byte-valid raw/meta/retrieval pair bound to the run."""

    raw, meta_p, retrieval_p = pair_files(out_dir, task["task_id"])
    raw.parent.mkdir(parents=True, exist_ok=True)
    meta_p.parent.mkdir(parents=True, exist_ok=True)
    retrieval_p.parent.mkdir(parents=True, exist_ok=True)
    raw_text = (
        "PATCH\n--- a/%s\n+++ b/%s\n@@ -1,1 +1,1 @@\n"
        "FILES\n%s\nSYMBOLS\n%s::f\nROOT_CAUSE\ncanned.\n"
        % (task["target_file"], task["target_file"], task["target_file"],
           task["target_file"])
    )
    raw.write_text(raw_text, encoding="utf-8")
    rag = fake_rag_evidence(task["task_id"])
    meta = {
        "protocol_version": PROTOCOL_VERSION,
        "model": MODEL_ID,
        "task_id": task["task_id"],
        "rank": int(task["slot"]),
        "source_v1_1_payload_sha256": task["payload_sha256"],
        "max_new_tokens": MAX_NEW_TOKENS,
        "raw_sha256": sha256_text(raw_text),
        "run_identity_sha256": run_identity_sha256,
        "rag": {
            "enabled": True,
            "query_identity": rag["query_identity"],
            "retrieval_id": rag["retrieval_id"],
            "context_identity": rag["context_identity"],
        },
    }
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    retrieval_p.write_text(json.dumps(rag), encoding="utf-8")


# ---------------------------------------------------------------------------
# Blocker 1 — immutable run identity across invocations
# ---------------------------------------------------------------------------


def test_run_identity_immutable_across_two_invocations(tmp_path):
    """The identity created on the first invocation (including created_at
    and run_identity_sha256) is REUSED byte-identically on a later resume
    invocation — no second identity hash is ever produced."""

    contract = load_contract()
    g = git_state()
    run_dir = tmp_path / "run"
    first = write_or_verify_run_identity(
        run_dir, build_run_identity(contract, g))
    stored = json.loads(
        (run_dir / "run-identity.json").read_text(encoding="utf-8"))
    assert stored == first

    # A second invocation constructs a fresh identity (different
    # created_at) but MUST return the stored one.
    second = write_or_verify_run_identity(
        run_dir, build_run_identity(contract, g))
    assert second == first
    assert second["run_identity_sha256"] == first["run_identity_sha256"]
    assert (run_dir / "run-identity.json").read_text(encoding="utf-8") == \
        json.dumps(first, indent=2, ensure_ascii=False)


def test_run_identity_conflict_fails_closed(tmp_path):
    """A stored identity from a different source/contract state fails
    closed instead of being reused."""

    contract = load_contract()
    g = git_state()
    run_dir = tmp_path / "run"
    write_or_verify_run_identity(run_dir, build_run_identity(contract, g))
    other = build_run_identity(contract, g)
    other["contract_sha256"] = "0" * 64
    with pytest.raises(RunnerError, match="identity conflict"):
        write_or_verify_run_identity(run_dir, other)


def test_run_identity_tampered_hash_fails_closed(tmp_path):
    contract = load_contract()
    g = git_state()
    run_dir = tmp_path / "run"
    write_or_verify_run_identity(run_dir, build_run_identity(contract, g))
    path = run_dir / "run-identity.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["run_identity_sha256"] = "0" * 64
    path.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(RunnerError, match="not self-consistent"):
        write_or_verify_run_identity(run_dir, build_run_identity(contract, g))


# ---------------------------------------------------------------------------
# Blocker 2 — pre-completion retry vs post-completion no-retry
# ---------------------------------------------------------------------------


class FakeTransport:
    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0
        self.last_text = None

    def generate_one(self, text: str) -> dict:
        self.calls += 1
        self.last_text = text
        if self.calls <= self.fail_times:
            raise TransportError("pre-completion transport failure")
        return {
            "text": "PATCH\nok\n",
            "prompt_tokens": 10,
            "output_tokens": 5,
            "generation_latency_s": 1.0,
            "peak_allocated_gib": 4.0,
        }


def _stub_assembly(monkeypatch, task_id: str):
    import experiments.cp118_rag_definitive.s4_runner as runner

    rag = fake_rag_evidence(task_id)
    monkeypatch.setattr(
        runner, "_assemble_task_prompt",
        lambda *a, **k: ("ASSEMBLED", rag, {"retrieved_context_bytes": 100}),
    )
    return runner


def _real_payload(task) -> str:
    from experiments.cp118_rag_definitive.s4_corpus import validate_payloads

    payloads = validate_payloads(
        [task], REPO_ROOT / "experiments/raw-pilot-v1.1/state/quix40-v1/payloads"
    )
    return payloads[task["task_id"]]


def test_two_pre_completion_failures_then_success_uses_budget(
        tmp_path, tasks, monkeypatch):
    """Two pre-completion transport failures then success consume the
    bounded retry budget exactly (3 model calls); the real persistence
    path then writes a valid pair bound to the run identity."""

    runner = _stub_assembly(monkeypatch, tasks[0]["task_id"])
    transport = FakeTransport(fail_times=2)
    out_dir = tmp_path / "run"
    gen, attempts = generate_and_persist_task(
        transport=transport, index=None, task=tasks[0],
        payload_text=_real_payload(tasks[0]), revision="r",
        out_dir=out_dir, run_identity_sha256="run-sha", load_s=1.0,
    )
    assert transport.calls == 3
    assert attempts == 3
    assert gen["text"] == "PATCH\nok\n"
    valid, reason = pair_is_valid(
        out_dir, tasks[0], expected_run_identity_sha256="run-sha")
    assert valid and reason == ""


def test_post_completion_oserror_aborts_with_one_model_call(
        tmp_path, tasks, monkeypatch):
    """An OSError during post-completion persistence causes abort with
    EXACTLY ONE model generation call."""

    runner = _stub_assembly(monkeypatch, tasks[0]["task_id"])
    transport = FakeTransport(fail_times=0)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(runner, "_persist_pair", boom)
    with pytest.raises(RunnerError, match="post-completion persistence "
                                          "failed"):
        generate_and_persist_task(
            transport=transport, index=None, task=tasks[0],
            payload_text=_real_payload(tasks[0]), revision="r",
            out_dir=tmp_path / "run", run_identity_sha256="run-sha",
            load_s=1.0,
        )
    assert transport.calls == 1


def test_pair_validation_failure_aborts_with_one_model_call(
        tmp_path, tasks, monkeypatch):
    """A pair-validation failure after completion causes abort with EXACTLY
    ONE model generation call (never retried for quality)."""

    runner = _stub_assembly(monkeypatch, tasks[0]["task_id"])
    transport = FakeTransport(fail_times=0)

    def bad_pair(*a, **k):
        raise RunnerError("generated pair failed internal validation (x)")

    monkeypatch.setattr(runner, "_persist_pair", bad_pair)
    with pytest.raises(RunnerError, match="internal validation"):
        generate_and_persist_task(
            transport=transport, index=None, task=tasks[0],
            payload_text=_real_payload(tasks[0]), revision="r",
            out_dir=tmp_path / "run", run_identity_sha256="run-sha",
            load_s=1.0,
        )
    assert transport.calls == 1


# ---------------------------------------------------------------------------
# Blocker 3 — pair/run-identity provenance
# ---------------------------------------------------------------------------


def test_valid_pair_binds_run_identity(tmp_path, tasks):
    out_dir = tmp_path / "run"
    write_valid_pair(out_dir, tasks[0], "run-sha-1")
    valid, reason = pair_is_valid(
        out_dir, tasks[0], expected_run_identity_sha256="run-sha-1")
    assert valid and reason == ""


def test_stale_pair_foreign_run_identity_fails_closed(tmp_path, tasks):
    """A pair from another run (different run_identity_sha256) must fail
    closed and never be skipped."""

    out_dir = tmp_path / "run"
    write_valid_pair(out_dir, tasks[0], "run-sha-A")
    valid, reason = pair_is_valid(
        out_dir, tasks[0], expected_run_identity_sha256="run-sha-B")
    assert not valid and reason == "meta_mismatch"
    skipped, pending, corrupt = plan_resume(
        out_dir, tasks, expected_run_identity_sha256="run-sha-B")
    assert tasks[0]["task_id"] not in skipped
    assert any(c.startswith(tasks[0]["task_id"]) for c in corrupt)
    assert tasks[0]["task_id"] not in [t["task_id"] for t in pending]


def test_corrupt_retrieval_evidence_fails_closed(tmp_path, tasks):
    out_dir = tmp_path / "run"
    write_valid_pair(out_dir, tasks[0], "run-sha-1")
    retrieval_p = pair_files(out_dir, tasks[0]["task_id"])[2]
    retrieval_p.write_text("{not json", encoding="utf-8")
    valid, reason = pair_is_valid(
        out_dir, tasks[0], expected_run_identity_sha256="run-sha-1")
    assert not valid and reason == "retrieval_unparseable"


def test_retrieval_task_id_mismatch_fails_closed(tmp_path, tasks):
    out_dir = tmp_path / "run"
    write_valid_pair(out_dir, tasks[0], "run-sha-1")
    retrieval_p = pair_files(out_dir, tasks[0]["task_id"])[2]
    rag = fake_rag_evidence(tasks[0]["task_id"])
    rag["task_id"] = "quixbugs-some-other-task"
    retrieval_p.write_text(json.dumps(rag), encoding="utf-8")
    valid, reason = pair_is_valid(
        out_dir, tasks[0], expected_run_identity_sha256="run-sha-1")
    assert not valid and reason == "retrieval_mismatch"


def test_retrieval_meta_inconsistency_fails_closed(tmp_path, tasks):
    """Retrieval evidence whose retrieval_id disagrees with the meta's RAG
    block must fail closed (RAG provenance must be consistent)."""

    out_dir = tmp_path / "run"
    write_valid_pair(out_dir, tasks[0], "run-sha-1")
    meta_p = pair_files(out_dir, tasks[0]["task_id"])[1]
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["rag"]["retrieval_id"] = "different-id"
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    valid, reason = pair_is_valid(
        out_dir, tasks[0], expected_run_identity_sha256="run-sha-1")
    assert not valid and reason == "retrieval_mismatch"


def test_all_pairs_valid_aggregate(tmp_path, tasks):
    out_dir = tmp_path / "run"
    for task in tasks[:2]:
        write_valid_pair(out_dir, task, "run-sha-1")
    ok, invalid = all_pairs_valid(
        out_dir, tasks, expected_run_identity_sha256="run-sha-1")
    assert not ok and len(invalid) == len(tasks) - 2
    for task in tasks[2:]:
        write_valid_pair(out_dir, task, "run-sha-1")
    ok, invalid = all_pairs_valid(
        out_dir, tasks, expected_run_identity_sha256="run-sha-1")
    assert ok and invalid == []


# ---------------------------------------------------------------------------
# Blocker 4 — eval clean-source-state guard
# ---------------------------------------------------------------------------


def _ok_identity() -> dict:
    contract = load_contract()
    g = git_state()
    return build_run_identity(contract, g)


def test_eval_guard_requires_clean_tracked_tree():
    contract = load_contract()
    identity = _ok_identity()
    dirty = {"branch": "b", "head_sha": identity["source_commit_sha"],
             "tracked_dirty": True}
    with pytest.raises(RunnerError, match="tracked working tree is dirty"):
        verify_eval_source_state(dirty, identity, contract)


def test_eval_guard_head_mismatch_fails_closed():
    contract = load_contract()
    identity = _ok_identity()
    other_head = {"branch": "b", "head_sha": "0" * 40, "tracked_dirty": False}
    with pytest.raises(RunnerError, match="source_commit_sha"):
        verify_eval_source_state(other_head, identity, contract)


def test_eval_guard_contract_mismatch_fails_closed():
    contract = load_contract()
    identity = _ok_identity()
    identity = dict(identity)
    identity["contract_sha256"] = "0" * 64
    g = {"branch": "b", "head_sha": identity["source_commit_sha"],
         "tracked_dirty": False}
    with pytest.raises(RunnerError, match="contract_sha256"):
        verify_eval_source_state(g, identity, contract)


def test_eval_guard_passes_on_clean_matching_state():
    contract = load_contract()
    identity = _ok_identity()
    g = {"branch": "b", "head_sha": identity["source_commit_sha"],
         "tracked_dirty": False}
    verify_eval_source_state(g, identity, contract)  # no raise
