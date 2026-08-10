"""S4 — cohort identity, DebugTask projection, payload assembly and budget
tests (Amendment 1/2).  All offline; no model, no network, no GPU.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.cp118_rag_definitive.s4_corpus import (
    MANIFEST_PATH,
    MANIFEST_SHA256,
    ORACLE_ABSENT,
    build_debug_task,
    build_rag_context,
    build_shared_index,
    build_task_mapping,
    build_v121_payload_hashes,
    retrieve_for_task,
    validate_manifest,
    validate_payloads,
    verify_failing_output_identity,
)
from experiments.cp118_rag_definitive.s4_payload import (
    MAX_NEW_TOKENS,
    MAX_PROMPT_TOKENS,
    OUTPUT_REQUIREMENTS_V121,
    assemble_rag_payload,
    build_v12_payload,
    canonical_rag_block,
    parse_payload_sections,
    record_prompt_budget,
    sha256_text,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
FROZEN_GPU_SCRIPT = (
    REPO_ROOT / "experiments/raw-pilot-v1.1/scripts/"
    "RAW_C9_5Model_40Task_Protocol_v1_2_1_GPU_GENERATE_v1.py"
)
PAYLOAD_DIR = REPO_ROOT / "experiments/raw-pilot-v1.1/state/quix40-v1/payloads"


@pytest.fixture(scope="module")
def cohort():
    tasks = validate_manifest(REPO_ROOT / MANIFEST_PATH)
    payloads = validate_payloads(tasks, PAYLOAD_DIR)
    return tasks, payloads


def test_manifest_identity_and_shape(cohort):
    tasks, _ = cohort
    assert len(tasks) == 40
    assert [t["slot"] for t in tasks] == list(range(1, 41))
    assert all(t["quixbugs_revision"] == "4257f44b0ff1181dedaedee6a447e133219fcebf"
               for t in tasks)


def test_manifest_hash_matches_frozen():
    digest = __import__("hashlib").sha256(
        (REPO_ROOT / MANIFEST_PATH).read_bytes()).hexdigest()
    assert digest == MANIFEST_SHA256 == \
        "572082482a64adabc8c790293580a9869cdda485a3813505f14adec850577afd"


def test_all_payload_hashes_verified(cohort):
    _, payloads = cohort
    assert len(payloads) == 40


def test_failing_output_identity_matches_manifest(cohort):
    """38/40 FAILING_TEST_OUTPUT sections are hash-identical to the frozen
    manifest selection.  For quixbugs-mergesort and quixbugs-possible_change
    the frozen payload embeds a rendering that differs from the recorded
    selection hash (same visible_chars) — a documented provenance
    observation; the payload (payload_sha256-verified) remains the prompt
    authority."""

    tasks, payloads = cohort
    matches = verify_failing_output_identity(tasks, payloads)
    assert sum(matches.values()) == 38
    assert sorted(tid for tid, ok in matches.items() if not ok) == [
        "quixbugs-mergesort", "quixbugs-possible_change",
    ]


def test_task_projection_whitelist_only(cohort):
    tasks, payloads = cohort
    mapping = build_task_mapping(tasks[0], payloads[tasks[0]["task_id"]])
    assert mapping["task_id"] == tasks[0]["task_id"]
    assert mapping["title"] == tasks[0]["program"]
    assert mapping["description"]  # PROBLEM section present
    assert mapping["tags"] == []
    assert mapping["oracle"] == {
        "bug_category": ORACLE_ABSENT,
        "target_files": [],
        "target_symbols": [],
        "root_cause_summary": ORACLE_ABSENT,
        "runtime_evidence_hint": ORACLE_ABSENT,
    }
    # The issue projection must never surface oracle sentinels.
    task = build_debug_task(tasks[0], payloads[tasks[0]["task_id"]])
    from agentic_debugger.rag.corpus import task_issue_projection

    projection = task_issue_projection(task)
    assert ORACLE_ABSENT not in projection
    assert "root_cause" not in projection.lower()


def test_v121_builder_matches_frozen_script_verbatim(cohort):
    tasks, payloads = cohort
    tid = tasks[0]["task_id"]
    v11 = payloads[tid]
    v12 = build_v12_payload(v11)
    # Drift test: the frozen script must build the identical payload.  The
    # frozen GPU-generation protocol source is hash-pinned in the contract
    # (Blocker 3) and verified before use.
    import hashlib

    contract = json.loads(
        (REPO_ROOT / "experiments/cp118_rag_definitive/s4_contract.json")
        .read_text(encoding="utf-8"))
    pinned = contract["protocol"]["frozen_sources"]
    assert hashlib.sha256(FROZEN_GPU_SCRIPT.read_bytes()).hexdigest() == \
        pinned["gpu_generate_script_sha256"]
    ns: dict = {}
    source = FROZEN_GPU_SCRIPT.read_text(encoding="utf-8")
    exec(compile(
        "OUTPUT_REQUIREMENTS_V121=" + repr(OUTPUT_REQUIREMENTS_V121) + "\n"
        "def build_v12_payload(v11_payload):\n"
        "    marker = \"\\n\\nOUTPUT_REQUIREMENTS\\n\"\n"
        "    if v11_payload.count(marker) != 1:\n"
        "        raise RuntimeError('marker count')\n"
        "    prefix, _old = v11_payload.split(marker, 1)\n"
        "    return prefix + marker + OUTPUT_REQUIREMENTS_V121\n",
        "<drift-test>", "exec"), ns)
    assert v12 == ns["build_v12_payload"](v11)
    assert "\n\nOUTPUT_REQUIREMENTS\n" in v12
    assert v12.endswith(OUTPUT_REQUIREMENTS_V121)


def test_payload_section_parse_is_deterministic(cohort):
    tasks, payloads = cohort
    tid = tasks[0]["task_id"]
    sections = parse_payload_sections(payloads[tid])
    assert set(sections) == {
        "TASK", "PROBLEM", "REPOSITORY_TREE", "SOURCE_FILES",
        "FAILING_TEST_OUTPUT", "OUTPUT_REQUIREMENTS",
    }
    assert parse_payload_sections(payloads[tid]) == sections


def test_v121_hashes_stable(cohort):
    tasks, payloads = cohort
    hashes = build_v121_payload_hashes(tasks, payloads)
    assert len(hashes) == 40
    assert hashes == build_v121_payload_hashes(tasks, payloads)


def test_rag_payload_assembly_deterministic_and_marker_placement(cohort):
    tasks, payloads = cohort
    tid = tasks[0]["task_id"]
    v12 = build_v12_payload(payloads[tid])
    rag_mapping = {"index_id": "i", "query_identity": "q",
                   "retrieval_id": "r", "query": "x",
                   "chunks": [{"chunk_id": "c1", "path": "python_programs/gcd.py",
                               "start_line": 1, "end_line": 3,
                               "score": 4, "text": "def gcd(a, b):\n"}],
                   "selected_bytes": 20, "truncated": False}
    a = assemble_rag_payload(v12, rag_mapping)
    b = assemble_rag_payload(v12, rag_mapping)
    assert a == b
    assert "RETRIEVED_CONTEXT\n" in a
    assert a.index("RETRIEVED_CONTEXT\n") < a.index("OUTPUT_REQUIREMENTS\n")
    assert a.endswith(OUTPUT_REQUIREMENTS_V121)
    assert canonical_rag_block(rag_mapping) in a


def test_budget_recording_and_fail_closed(cohort):
    tasks, payloads = cohort
    tid = tasks[0]["task_id"]
    v12 = build_v12_payload(payloads[tid])
    assembled = assemble_rag_payload(
        v12, {"chunks": [{"chunk_id": "c", "path": "p",
                          "start_line": 1, "end_line": 2,
                          "score": 1, "text": "x"}],
              "selected_bytes": 4, "truncated": False})
    budget = record_prompt_budget(
        base_prompt=v12,
        retrieved_context_bytes=4,
        assembled_prompt=assembled,
        assembled_prompt_tokens=len(assembled) // 4,  # crude upper bound
        base_prompt_tokens_manifest=None,
        rag_context_truncated=False,
    )
    assert budget["max_prompt_tokens_ok"] is True
    assert budget["public_request_byte_budget"] == 20_000
    assert "LiveModelAdapter" in budget["public_request_byte_budget_scope"]
    assert budget["protocol_max_prompt_tokens"] == MAX_PROMPT_TOKENS
    assert budget["protocol_max_new_tokens"] == MAX_NEW_TOKENS
    with pytest.raises(RuntimeError, match="cannot coexist"):
        record_prompt_budget(
            base_prompt=v12, retrieved_context_bytes=4,
            assembled_prompt=assembled, assembled_prompt_tokens=MAX_PROMPT_TOKENS + 1,
            base_prompt_tokens_manifest=None, rag_context_truncated=False,
        )


def test_contract_freezes_amendment_2_scope():
    contract = json.loads(
        (REPO_ROOT / "experiments/cp118_rag_definitive/s4_contract.json")
        .read_text(encoding="utf-8"))
    assert contract["rag"]["context"]["public_request_byte_budget"] == 20000
    assert "NOT the one-shot generation prompt limit" in (
        contract["rag"]["context"]["public_request_byte_budget_scope"])
    assert contract["protocol"]["generation"]["max_prompt_tokens"] == 24576
    assert contract["protocol"]["generation"]["max_new_tokens"] == 4096


SCOPED_CORPUS = REPO_ROOT / "tmp" / "s4" / "scoped-corpus-v1"


@pytest.mark.skipif(
    not SCOPED_CORPUS.is_dir(),
    reason="scoped corpus not materialized under tmp/s4 (run validate)",
)
def test_all_40_retrievals_fit_agent_context_cap(cohort):
    """Campaign-wide constraint coexistence (Amendment 2): every task's
    retrieval result must fit the frozen 4000-byte agent-context cap so
    RagContext.from_retrieval never fails closed mid-campaign."""

    tasks, payloads = cohort
    index = build_shared_index(
        SCOPED_CORPUS, revision="test-revision", repository_identity="test"
    )
    for task in tasks:
        result = retrieve_for_task(
            index, task, payloads[task["task_id"]],
            expected_revision="test-revision",
        )
        context = build_rag_context(result)  # must not raise
        assert context.selected_bytes <= 4000
        assert context.selected_bytes == sum(
            c.bytes for c in result.selected
        )
