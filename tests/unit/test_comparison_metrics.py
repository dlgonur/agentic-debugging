"""Normalized metrics: failure categories, aggregates, delta, CSV.

Covers the repair-1 separation of primary (evaluation) attempts from
auxiliary (preference-fixture) attempts: aggregates and deltas use
evaluation-role attempts only, and the old synthetic
``base 0.50 vs tuned 1.00`` result is impossible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_debugger.comparison.metrics import (
    CSV_COLUMNS,
    MetricsError,
    aggregate_all,
    aggregate_condition,
    csv_rows,
    delta_against_baseline,
    normalize_failure_category,
    to_csv_text,
)
from agentic_debugger.comparison.schema import (
    AttemptRecord,
    ComparisonExperiment,
    ComparisonInvariantError,
)
from agentic_debugger.evaluation.root_cause_metric import build_root_cause_assessment

ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "curated-off-by-one-002"


def _attempt(**overrides):
    base = {
        "attempt_id": f"{TASK_ID}:base",
        "condition_id": "base",
        "task_id": TASK_ID,
        "mode": "imported",
        "role": "evaluation",
        "source_identity": "generation-artifact-v1:x",
        "generation_produced": True,
        "valid_patch": True,
        "patch_sha256": "a" * 64,
        "changed_file_count": 1,
        "correct_target_file": True,
        "localization_outcome": None,
        "f2p_passed": 1,
        "f2p_total": 1,
        "p2p_passed": 2,
        "p2p_total": 2,
        "verifier_outcome": "RESOLVED",
        "verifier_status": "COMPLETED",
        "failure_category": None,
        "runtime_ms": 100,
        "memory_bytes": 1024,
        "cost": 0.001,
        "tokens": 50,
        "retrieval_count": None,
        "retrieval_bytes": None,
        "retrieval_latency_ms": None,
        "replay_valid": None,
        "cleanup_status": "cleaned",
        "canonical_fixture_unchanged": True,
        "provider_attempts": 0,
        "network_attempts": 0,
        "external_provider_attempts": 0,
        "external_network_attempts": 0,
        "response_text": "response",
        "response_sha256": None,
        "verifier_evidence": {"executed": True, "outcome": "RESOLVED",
                              "patch_applied": True, "syntax_passed": True},
        "provenance": {
            "source": "test",
            "generator": "offline-deterministic-demo",
            "prompt_contract": "test",
        },
    }
    auto_hash = "response_sha256" not in overrides
    base.update(overrides)
    if auto_hash and base["response_text"]:
        import hashlib

        base["response_sha256"] = hashlib.sha256(
            base["response_text"].encode("utf-8")
        ).hexdigest()
    return AttemptRecord.from_mapping(base)


def _with_root_cause(attempt, **overrides):
    values = {
        "task_id": attempt.task_id,
        "attempt_id": attempt.attempt_id,
        "assessor_kind": "independent-human",
        "assessor_id": "comparison-reviewer",
        "claim_text": "The upper loop bound omits the final required index.",
        "mechanism": "SATISFIED",
        "failure_connection": "SATISFIED",
        "repair_alignment": "SATISFIED",
        "contradicts_evidence": False,
        "evidence_refs": ("trajectory:event-12",),
    }
    values.update(overrides)
    assessment = build_root_cause_assessment(**values)
    mapping = attempt.to_mapping()
    mapping["provenance"] = {
        **mapping["provenance"],
        "root_cause_assessment": assessment.to_mapping(),
    }
    return AttemptRecord.from_mapping(mapping)


def test_failure_category_mapping():
    cases = [
        ({"verifier_outcome": "RESOLVED"}, None),
        ({"verifier_outcome": "NO_OP"}, "NO_OP"),
        ({"verifier_outcome": "REGRESSION"}, "P2P_REGRESSION"),
        ({"verifier_outcome": "BREAKING_RESOLVED"}, "P2P_REGRESSION"),
        ({"verifier_outcome": "WORK_IN_PROGRESS"}, "F2P_NOT_PASSED"),
        ({"verifier_outcome": "PARTIALLY_RESOLVED"}, "F2P_NOT_PASSED"),
        ({"verifier_outcome": "WEIRD"}, "UNCLASSIFIED"),
        ({"verifier_outcome": None, "generation_produced": False}, "NO_GENERATION"),
        ({"verifier_outcome": None, "generation_produced": True,
          "valid_patch": False, "patch_present": False}, "NO_PATCH"),
        ({"verifier_outcome": None, "generation_produced": True,
          "valid_patch": False, "patch_present": True,
          "patch_applied": False}, "PATCH_NOT_APPLIED"),
        ({"verifier_outcome": None, "generation_produced": True,
          "valid_patch": False, "patch_present": True,
          "patch_applied": None}, "PATCH_INVALID"),
        ({"verifier_outcome": None, "generation_produced": True,
          "valid_patch": True, "patch_applied": True,
          "syntax_passed": False}, "SYNTAX_FAILED"),
        ({"verifier_outcome": None, "generation_produced": True,
          "valid_patch": True, "patch_applied": True,
          "syntax_passed": True, "verifier_status": None}, "VERIFIER_FAILED"),
        ({"verifier_outcome": None, "generation_produced": True,
          "valid_patch": True, "patch_applied": True,
          "syntax_passed": True, "verifier_status": "INTERNAL_ERROR"},
         "VERIFIER_FAILED"),
    ]
    for facts, expected in cases:
        assert normalize_failure_category(facts) == expected


def _experiment(attempts):
    conditions = tuple(dict.fromkeys(a.condition_id for a in attempts))
    if "base" not in conditions:
        conditions = ("base",) + conditions
    return ComparisonExperiment(
        schema_version="comparison-v1",
        experiment_id="test-exp",
        baseline_condition="base",
        conditions=conditions,
        task_ids=(TASK_ID,),
        attempts=tuple(attempts),
    )


def test_aggregates_are_derived_from_evaluation_records_only():
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:base"),
        _attempt(attempt_id=f"{TASK_ID}:base:wrong", role="preference-fixture",
                 verifier_outcome="NO_OP", failure_category="NO_OP",
                 f2p_passed=0, f2p_total=1),
        _attempt(
            attempt_id=f"{TASK_ID}:agentic",
            condition_id="agentic",
            mode="native",
            verifier_outcome="RESOLVED",
            retrieval_count=3,
            retrieval_bytes=1200,
            retrieval_latency_ms=5,
        ),
    ]
    experiment = _experiment(attempts)
    aggregates = aggregate_all(experiment)
    by_condition = {b["condition_id"]: b for b in aggregates["conditions"]}
    assert by_condition["base"]["attempts"] == 1  # auxiliary excluded
    assert by_condition["base"]["auxiliary_attempts"] == 1
    assert by_condition["base"]["resolved"] == 1
    assert by_condition["base"]["resolved_rate"] == 1.0
    assert by_condition["base"]["valid_patch"] == 1
    assert by_condition["base"]["f2p_passed"] == 1
    assert by_condition["agentic"]["resolved"] == 1
    assert by_condition["agentic"]["retrieval_count"] == 3
    assert by_condition["base"]["memory_bytes"] == 1024
    assert by_condition["base"]["external_provider_attempts"] == 0
    assert by_condition["base"]["root_cause_assessment_records"] == 0
    assert by_condition["base"]["root_cause_missing_assessments"] == 1
    assert by_condition["base"]["root_cause_correct_rate"] == 0.0


def test_root_cause_assessments_feed_aggregates_delta_and_csv():
    base = _with_root_cause(_attempt(attempt_id=f"{TASK_ID}:base"))
    agentic = _with_root_cause(
        _attempt(
            attempt_id=f"{TASK_ID}:agentic",
            condition_id="agentic",
            mode="native",
        ),
        repair_alignment="PARTIAL",
    )
    experiment = _experiment([base, agentic])
    aggregates = aggregate_all(experiment)
    by_condition = {b["condition_id"]: b for b in aggregates["conditions"]}
    assert by_condition["base"]["root_cause_assessment_records"] == 1
    assert by_condition["base"]["root_cause_assessed_claims"] == 1
    assert by_condition["base"]["root_cause_correct"] == 1
    assert by_condition["base"]["root_cause_correct_rate"] == 1.0
    assert by_condition["agentic"]["root_cause_partially_correct"] == 1
    assert by_condition["agentic"]["root_cause_correct_rate"] == 0.0
    delta = delta_against_baseline(experiment, aggregates)
    aggregate_delta = {
        (entry["condition_id"], entry["metric"]): entry["delta"]
        for entry in delta["delta_entries"]
        if entry["task_id"] is None
    }
    assert aggregate_delta[("agentic", "root_cause_correct")] == -1.0
    assert aggregate_delta[("agentic", "root_cause_correct_rate")] == -1.0
    csv = to_csv_text(csv_rows(experiment))
    assert "root_cause_outcome" in csv.splitlines()[0]
    assert "CORRECT" in csv
    assert "PARTIALLY_CORRECT" in csv


def test_mismatched_or_tampered_root_cause_assessment_fails_closed():
    attempt = _with_root_cause(_attempt(attempt_id=f"{TASK_ID}:base"))
    mapping = attempt.to_mapping()
    mapping["provenance"]["root_cause_assessment"]["attempt_id"] = "wrong"
    with pytest.raises(ComparisonInvariantError, match="root_cause_assessment"):
        AttemptRecord.from_mapping(mapping)


def test_valid_but_wrong_task_root_cause_assessment_fails_at_schema_boundary():
    attempt = _attempt(attempt_id=f"{TASK_ID}:base")
    assessment = build_root_cause_assessment(
        task_id="curated-wrong-branch-003",
        attempt_id=attempt.attempt_id,
        assessor_kind="independent-human",
        assessor_id="comparison-reviewer",
        claim_text=None,
        evidence_refs=("trajectory:no-root-cause-action",),
    )
    mapping = attempt.to_mapping()
    mapping["provenance"] = {
        **mapping["provenance"],
        "root_cause_assessment": assessment.to_mapping(),
    }
    with pytest.raises(ComparisonInvariantError, match="task_id mismatch"):
        AttemptRecord.from_mapping(mapping)


def test_old_synthetic_base_0_50_vs_tuned_1_00_is_impossible():
    """Auxiliary NO_OP attempts can never lower the base aggregate."""
    base = [
        _attempt(attempt_id=f"{TASK_ID}:base"),
        _attempt(attempt_id=f"{TASK_ID}:base:wrong", role="preference-fixture",
                 verifier_outcome="NO_OP", failure_category="NO_OP",
                 f2p_passed=0, f2p_total=1),
    ]
    tuned = [
        _attempt(attempt_id=f"{TASK_ID}:tuned", condition_id="fine-tuned",
                 verifier_outcome="RESOLVED"),
    ]
    experiment = _experiment(base + tuned)
    aggregates = aggregate_all(experiment)
    by_condition = {b["condition_id"]: b for b in aggregates["conditions"]}
    assert by_condition["base"]["resolved_rate"] == 1.0
    assert by_condition["fine-tuned"]["resolved_rate"] == 1.0
    # No manufactured tuned-over-base gain.
    delta = delta_against_baseline(experiment, aggregates)
    aggregate_deltas = {
        (e["condition_id"], e["metric"]): e["delta"]
        for e in delta["delta_entries"]
        if e["task_id"] is None
    }
    assert aggregate_deltas[("fine-tuned", "resolved")] == 0
    assert aggregate_deltas[("fine-tuned", "resolved_rate")] == 0.0
    assert aggregate_deltas[("fine-tuned", "valid_patch")] == 0


def test_auxiliary_attempts_never_reach_delta():
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:base"),
        _attempt(attempt_id=f"{TASK_ID}:base:wrong", role="preference-fixture",
                 verifier_outcome="REGRESSION", failure_category="P2P_REGRESSION",
                 f2p_passed=0, f2p_total=1, p2p_passed=1, p2p_total=2),
    ]
    experiment = _experiment(attempts)
    aggregates = aggregate_all(experiment)
    delta = delta_against_baseline(experiment, aggregates)
    assert not any(e["condition_id"] == "base" for e in delta["delta_entries"])


def test_aggregate_over_mixed_conditions_is_rejected():
    with pytest.raises(MetricsError):
        aggregate_condition([_attempt(attempt_id="a"), _attempt(
            attempt_id="b", condition_id="agentic")])


def test_aggregate_requires_evaluation_attempts():
    with pytest.raises(MetricsError):
        aggregate_condition([
            _attempt(attempt_id="a", role="preference-fixture",
                     verifier_outcome="NO_OP", failure_category="NO_OP",
                     f2p_passed=0, f2p_total=1),
        ])


def test_delta_against_baseline():
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:base"),
        _attempt(
            attempt_id=f"{TASK_ID}:tuned",
            condition_id="fine-tuned",
            verifier_outcome="REGRESSION",
            failure_category="P2P_REGRESSION",
            f2p_passed=0,
            f2p_total=1,
            p2p_passed=1,
            p2p_total=2,
        ),
    ]
    experiment = _experiment(attempts)
    aggregates = aggregate_all(experiment)
    delta = delta_against_baseline(experiment, aggregates)
    entries = {(e["condition_id"], e["metric"]): e for e in delta["delta_entries"]
               if e["task_id"] is None}
    assert entries[("fine-tuned", "resolved")]["delta"] == -1
    assert entries[("fine-tuned", "valid_patch")]["delta"] == 0
    assert entries[("fine-tuned", "resolved_rate")]["delta"] == -1.0
    task_entries = [e for e in delta["delta_entries"] if e["task_id"] == TASK_ID]
    assert task_entries
    assert not any(e["condition_id"] == "base" for e in delta["delta_entries"])


def test_delta_requires_baseline_aggregate():
    experiment = _experiment([_attempt(attempt_id=f"{TASK_ID}:base")])
    with pytest.raises(MetricsError):
        delta_against_baseline(experiment, {"conditions": []})


def test_csv_is_deterministic_and_one_row_per_attempt():
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:base"),
        _attempt(attempt_id=f"{TASK_ID}:tuned", condition_id="fine-tuned"),
        _attempt(attempt_id=f"{TASK_ID}:base:wrong", role="preference-fixture",
                 verifier_outcome="NO_OP", failure_category="NO_OP",
                 f2p_passed=0, f2p_total=1),
    ]
    experiment = _experiment(attempts)
    first = to_csv_text(csv_rows(experiment))
    second = to_csv_text(csv_rows(experiment))
    assert first == second
    lines = first.splitlines()
    assert lines[0] == ",".join(CSV_COLUMNS)
    assert len(lines) == 4  # header + 3 attempts
    assert "role" in lines[0]
    assert "memory_bytes" in lines[0]
    assert "external_provider_attempts" in lines[0]


def test_experiment_rejects_duplicate_attempts_and_bad_baseline():
    with pytest.raises(Exception):
        _experiment([_attempt(attempt_id="dup"), _attempt(attempt_id="dup")])
    with pytest.raises(Exception):
        ComparisonExperiment(
            schema_version="comparison-v1",
            experiment_id="x",
            baseline_condition="nope",
            conditions=("base",),
            task_ids=(TASK_ID,),
            attempts=(_attempt(attempt_id="a"),),
        )
    with pytest.raises(Exception):
        ComparisonExperiment(
            schema_version="comparison-v1",
            experiment_id="x",
            baseline_condition="base",
            conditions=("base", "base"),
            task_ids=(TASK_ID,),
            attempts=(_attempt(attempt_id="a"),),
        )


def test_experiment_rejects_duplicate_primary_task_condition():
    with pytest.raises(Exception):
        _experiment([
            _attempt(attempt_id="a"),
            _attempt(attempt_id="b"),
        ])
    # One evaluation + one auxiliary for the same task/condition is legal.
    experiment = _experiment([
        _attempt(attempt_id="a"),
        _attempt(attempt_id="a:wrong", role="preference-fixture",
                 verifier_outcome="NO_OP", failure_category="NO_OP",
                 f2p_passed=0, f2p_total=1),
    ])
    assert len(experiment.attempts) == 2


def test_attempt_record_invariants():
    with pytest.raises(Exception):
        _attempt(f2p_passed=2, f2p_total=1)
    with pytest.raises(Exception):
        _attempt(p2p_passed=3, p2p_total=2)
    with pytest.raises(Exception):
        _attempt(valid_patch=True, patch_sha256=None)
    with pytest.raises(Exception):
        _attempt(response_text="x", response_sha256=None)
    with pytest.raises(Exception):
        _attempt(response_text="x", response_sha256="0" * 64)
    with pytest.raises(Exception):
        _attempt(failure_category="NOT_A_CATEGORY")
    with pytest.raises(Exception):
        _attempt(cleanup_status="cleaned", verifier_status=None,
                 verifier_evidence=None)
    with pytest.raises(Exception):
        _attempt(condition_id="Bad-Condition")
    with pytest.raises(Exception):
        _attempt(task_id="BAD TASK ID")
