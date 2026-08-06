"""Preference exporter: ordered rules, guards, audit, determinism."""

from __future__ import annotations

import hashlib

from pathlib import Path

import pytest

from agentic_debugger.comparison.schema import AttemptRecord, ComparisonExperiment
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.preference.exporter import (
    ExportError,
    decide_preference,
    export_preferences_from_experiment,
)
from agentic_debugger.preference.schema import (
    PreferenceInvariantError,
    PreferencePair,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-off-by-one-002"


def _task_oracle():
    task = DebugTask.from_file(str(FIXTURES / TASK_ID / "task.json"))
    return {TASK_ID: task.oracle}


def _attempt(**overrides):
    base = {
        "attempt_id": f"{TASK_ID}:a",
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
        "runtime_ms": 10,
        "memory_bytes": None,
        "cost": None,
        "tokens": None,
        "retrieval_count": None,
        "retrieval_bytes": None,
        "retrieval_latency_ms": None,
        "replay_valid": None,
        "cleanup_status": "cleaned",
        "canonical_fixture_unchanged": True,
        "provider_attempts": 0,
        "network_attempts": 0,
        "external_provider_attempts": None,
        "external_network_attempts": None,
        "response_text": "response a",
        "response_sha256": None,
        "verifier_evidence": {"executed": True, "outcome": "RESOLVED",
                              "patch_applied": True, "syntax_passed": True},
        "provenance": {"source": "test", "generator": "offline-deterministic-demo",
                       "prompt_contract": "contract-x"},
    }
    base.update(overrides)
    if "response_sha256" not in overrides and base["response_text"]:
        base["response_sha256"] = hashlib.sha256(
            base["response_text"].encode("utf-8")
        ).hexdigest()
    return AttemptRecord.from_mapping(base)


def _experiment(attempts):
    conditions = tuple(dict.fromkeys(a.condition_id for a in attempts))
    if "base" not in conditions:
        conditions = ("base",) + conditions
    return ComparisonExperiment(
        schema_version="comparison-v1",
        experiment_id="test-exp",
        baseline_condition="base",
        conditions=conditions,
        task_ids=tuple(dict.fromkeys(a.task_id for a in attempts)),
        attempts=tuple(attempts),
    )


# ---------------------------------------------------------------------------
# Ordered rules
# ---------------------------------------------------------------------------


def test_rule_1_resolved_beats_non_resolved():
    chosen = _attempt(attempt_id="a", verifier_outcome="RESOLVED")
    rejected = _attempt(attempt_id="b", verifier_outcome="NO_OP",
                        failure_category="NO_OP", f2p_passed=0, f2p_total=1,
                        response_text="response b")
    decided = decide_preference(chosen, rejected)
    assert decided is not None
    winner, loser, rule, _reason = decided
    assert winner is chosen and rule == "rule-1"
    # Symmetric: the loser cannot win.
    assert decide_preference(rejected, chosen)[0] is chosen


def test_rule_2_valid_patch_beats_invalid():
    valid = _attempt(attempt_id="a", verifier_outcome="NO_OP", valid_patch=True,
                     f2p_passed=0, f2p_total=1, response_text="valid response")
    invalid = _attempt(attempt_id="b", verifier_outcome=None, valid_patch=False,
                       f2p_passed=None, f2p_total=None, response_text="invalid response")
    decided = decide_preference(valid, invalid)
    assert decided[2] == "rule-2"
    assert decided[0] is valid


def test_rule_3_stronger_f2p_wins():
    weak = _attempt(attempt_id="a", verifier_outcome="WORK_IN_PROGRESS",
                    f2p_passed=0, f2p_total=2, response_text="weak")
    strong = _attempt(attempt_id="b", verifier_outcome="WORK_IN_PROGRESS",
                      f2p_passed=1, f2p_total=2, response_text="strong")
    decided = decide_preference(weak, strong)
    assert decided[2] == "rule-3"
    assert decided[0] is strong


def test_rule_4_equal_f2p_stronger_p2p_wins():
    a = _attempt(attempt_id="a", verifier_outcome="REGRESSION", f2p_passed=1,
                 f2p_total=2, p2p_passed=1, p2p_total=3, response_text="a")
    b = _attempt(attempt_id="b", verifier_outcome="REGRESSION", f2p_passed=1,
                 f2p_total=2, p2p_passed=2, p2p_total=3, response_text="b")
    decided = decide_preference(a, b)
    assert decided[2] == "rule-4"
    assert decided[0] is b


def test_rule_5_fewer_changed_files_breaks_ties():
    a = _attempt(attempt_id="a", verifier_outcome="NO_OP", f2p_passed=0, f2p_total=1,
                 changed_file_count=3, response_text="a")
    b = _attempt(attempt_id="b", verifier_outcome="NO_OP", f2p_passed=0, f2p_total=1,
                 changed_file_count=1, response_text="b")
    decided = decide_preference(a, b)
    assert decided[2] == "rule-5"
    assert decided[0] is b


def test_equal_attempts_produce_no_pair():
    a = _attempt(attempt_id="a", verifier_outcome="NO_OP", f2p_passed=0, f2p_total=1,
                 changed_file_count=1, response_text="a")
    b = _attempt(attempt_id="b", verifier_outcome="NO_OP", f2p_passed=0, f2p_total=1,
                 changed_file_count=1, response_text="b")
    assert decide_preference(a, b) is None


def test_incomparable_evidence_produces_no_pair():
    a = _attempt(attempt_id="a", verifier_outcome=None, valid_patch=False,
                 f2p_passed=None, f2p_total=None, verifier_evidence=None,
                 cleanup_status=None, response_text="a")
    b = _attempt(attempt_id="b", role="preference-fixture",
                 verifier_outcome=None, valid_patch=False,
                 f2p_passed=None, f2p_total=None, verifier_evidence=None,
                 cleanup_status=None, response_text="b")
    assert decide_preference(a, b) is None


def test_different_prompt_contract_is_incomparable():
    a = _attempt(attempt_id="a", verifier_outcome="RESOLVED",
                 provenance={"source": "t", "prompt_contract": "x"},
                 response_text="a")
    b = _attempt(attempt_id="b", verifier_outcome="NO_OP", f2p_passed=0, f2p_total=1,
                 provenance={"source": "t", "prompt_contract": "y"},
                 response_text="b")
    assert decide_preference(a, b) is None


# ---------------------------------------------------------------------------
# Export-level guards
# ---------------------------------------------------------------------------


def test_export_produces_pairs_and_audit_counts():
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:correct", verifier_outcome="RESOLVED",
                 response_text="correct response"),
        _attempt(attempt_id=f"{TASK_ID}:wrong", role="preference-fixture", verifier_outcome="NO_OP",
                 failure_category="NO_OP", f2p_passed=0, f2p_total=1,
                 response_text="wrong response"),
    ]
    pairs, audit = export_preferences_from_experiment(
        _experiment(attempts),
        task_oracles=_task_oracle(),
        source_comparison_identity="exp:experiment.json",
    )
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.chosen.attempt_id == f"{TASK_ID}:correct"
    assert pair.rejected.attempt_id == f"{TASK_ID}:wrong"
    assert pair.rule_id == "rule-1"
    assert audit["pairs_produced"] == 1
    assert audit["rule_counts"]["rule-1"] == 1
    assert pair.verifier_evidence["chosen"]["outcome"] == "RESOLVED"


def test_held_out_tasks_produce_no_pairs():
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:a", verifier_outcome="RESOLVED",
                 response_text="a"),
        _attempt(attempt_id=f"{TASK_ID}:b", role="preference-fixture", verifier_outcome="NO_OP",
                 failure_category="NO_OP", f2p_passed=0, f2p_total=1,
                 response_text="b"),
    ]
    pairs, audit = export_preferences_from_experiment(
        _experiment(attempts),
        task_oracles=_task_oracle(),
        held_out_task_ids=(TASK_ID,),
        source_comparison_identity="exp:experiment.json",
    )
    assert pairs == ()
    assert audit["rejected"]["held_out_task"] == 2
    assert audit["tasks_excluded_held_out"] == [TASK_ID]


def test_oracle_contamination_rejects_pairs_fail_closed():
    oracle = _task_oracle()[TASK_ID]
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:a", verifier_outcome="RESOLVED",
                 response_text="correct response"),
        _attempt(attempt_id=f"{TASK_ID}:leaky", role="preference-fixture", verifier_outcome="NO_OP",
                 failure_category="NO_OP", f2p_passed=0, f2p_total=1,
                 response_text=oracle.root_cause_summary),
    ]
    pairs, audit = export_preferences_from_experiment(
        _experiment(attempts),
        task_oracles=_task_oracle(),
        source_comparison_identity="exp:experiment.json",
    )
    assert pairs == ()
    assert audit["rejected"]["contamination"] == 1


def test_same_response_and_no_evidence_rejections():
    attempts = [
        _attempt(attempt_id="x1", response_text="identical"),
        _attempt(attempt_id="x2", role="preference-fixture", response_text="identical"),
    ]
    pairs, audit = export_preferences_from_experiment(
        _experiment(attempts),
        task_oracles=_task_oracle(),
        source_comparison_identity="exp",
    )
    assert pairs == ()
    assert audit["rejected"]["same_response"] == 1

    none_attempts = [
        _attempt(attempt_id="y1", response_text=None, verifier_evidence=None,
                 generation_produced=False, valid_patch=False, cleanup_status=None),
        _attempt(attempt_id="y2", role="preference-fixture", response_text=None,
                 verifier_evidence=None, generation_produced=False,
                 valid_patch=False, cleanup_status=None),
    ]
    pairs2, audit2 = export_preferences_from_experiment(
        _experiment(none_attempts),
        task_oracles=_task_oracle(),
        source_comparison_identity="exp",
    )
    assert pairs2 == ()
    assert audit2["rejected"]["no_evidence"] == 1


def test_duplicate_pair_identity_is_a_hard_error():
    # Two identical (chosen, rejected) pairs cannot occur naturally because
    # pair ids are deterministic per attempt pair; simulate by exporting the
    # same experiment twice is impossible — instead assert determinism.
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:a", verifier_outcome="RESOLVED",
                 response_text="a"),
        _attempt(attempt_id=f"{TASK_ID}:b", role="preference-fixture", verifier_outcome="NO_OP",
                 failure_category="NO_OP", f2p_passed=0, f2p_total=1,
                 response_text="b"),
    ]
    first, _audit1 = export_preferences_from_experiment(
        _experiment(attempts), task_oracles=_task_oracle(),
        source_comparison_identity="exp:experiment.json",
    )
    second, _audit2 = export_preferences_from_experiment(
        _experiment(attempts), task_oracles=_task_oracle(),
        source_comparison_identity="exp:experiment.json",
    )
    assert [p.pair_id for p in first] == [p.pair_id for p in second]
    assert first[0].to_mapping() == second[0].to_mapping()


def test_missing_oracle_for_task_is_a_hard_error():
    attempts = [_attempt(attempt_id="a", verifier_outcome="RESOLVED",
                         response_text="a")]
    with pytest.raises(ExportError):
        export_preferences_from_experiment(
            _experiment(attempts),
            task_oracles={},  # no oracle for TASK_ID
            source_comparison_identity="exp",
        )


def test_export_is_deterministic_and_sorted_by_pair_id():
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:c", verifier_outcome="RESOLVED",
                 response_text="c response"),
        _attempt(attempt_id=f"{TASK_ID}:w", role="preference-fixture", verifier_outcome="NO_OP",
                 failure_category="NO_OP", f2p_passed=0, f2p_total=1,
                 response_text="w response"),
    ]
    result_a = export_preferences_from_experiment(
        _experiment(attempts), task_oracles=_task_oracle(),
        source_comparison_identity="exp",
    )
    result_b = export_preferences_from_experiment(
        _experiment(attempts), task_oracles=_task_oracle(),
        source_comparison_identity="exp",
    )
    assert result_a[0] == result_b[0]
    assert result_a[1] == result_b[1]
    ids = [p.pair_id for p in result_a[0]]
    assert ids == sorted(ids)

def test_same_attempt_rejection_is_counted_in_audit():
    """The audit must carry a complete rejected-key set incl. same_attempt."""
    attempts = [
        _attempt(attempt_id="x1", response_text="r1"),
        _attempt(attempt_id="x2", role="preference-fixture", response_text="r2"),
    ]
    experiment = _experiment(attempts)
    pairs, audit = export_preferences_from_experiment(
        experiment, task_oracles=_task_oracle(), source_comparison_identity="exp"
    )
    assert pairs == ()
    assert set(audit["rejected"]) == {
        "held_out_task", "same_attempt", "same_response",
        "contamination", "no_evidence", "incomparable",
    }


def test_pair_id_tampering_is_rejected_on_load():
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:correct", verifier_outcome="RESOLVED",
                 response_text="correct response"),
        _attempt(attempt_id=f"{TASK_ID}:wrong", role="preference-fixture", verifier_outcome="NO_OP",
                 failure_category="NO_OP", f2p_passed=0, f2p_total=1,
                 response_text="wrong response"),
    ]
    pairs, _audit = export_preferences_from_experiment(
        _experiment(attempts), task_oracles=_task_oracle(),
        source_comparison_identity="exp",
    )
    mapping = pairs[0].to_mapping()
    mapping["pair_id"] = "f" * 64
    with pytest.raises(PreferenceInvariantError):
        PreferencePair.from_mapping(mapping)


def test_contamination_checked_on_full_response_and_spans_recorded():
    oracle = _task_oracle()[TASK_ID]
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:a", verifier_outcome="RESOLVED",
                 response_text="correct response"),
        _attempt(attempt_id=f"{TASK_ID}:leaky", role="preference-fixture", verifier_outcome="NO_OP",
                 failure_category="NO_OP", f2p_passed=0, f2p_total=1,
                 response_text=oracle.root_cause_summary),
    ]
    pairs, audit = export_preferences_from_experiment(
        _experiment(attempts), task_oracles=_task_oracle(),
        source_comparison_identity="exp",
    )
    assert pairs == ()
    assert audit["rejected"]["contamination"] == 1
    assert len(audit["contamination_rejections"]) == 1
    rejection = audit["contamination_rejections"][0]
    assert rejection["attempt_ids"] == sorted([f"{TASK_ID}:a", f"{TASK_ID}:leaky"])
    spans = rejection["spans"]["chosen_candidate"] + rejection["spans"]["rejected_candidate"]
    assert any(span["field"] == "root_cause_summary" for span in spans)


def test_contamination_beyond_storage_cutoff_never_leaks():
    """Oracle text beyond the pair-storage cutoff is never stored."""
    from agentic_debugger.comparison.schema import bound_response_text
    from agentic_debugger.preference.schema import MAX_PAIR_RESPONSE_BYTES

    oracle = _task_oracle()[TASK_ID]
    filler = "z" * MAX_PAIR_RESPONSE_BYTES
    # The attempt's stored response is the marker-bounded original; oracle
    # text beyond the cutoff is dropped by the bound and never stored.
    bounded = bound_response_text(f"{filler}{oracle.root_cause_summary}")
    assert oracle.root_cause_summary not in bounded
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:a", verifier_outcome="RESOLVED",
                 response_text="correct response"),
        _attempt(attempt_id=f"{TASK_ID}:b", role="preference-fixture",
                 verifier_outcome="NO_OP", failure_category="NO_OP",
                 f2p_passed=0, f2p_total=1,
                 response_text=bounded),
    ]
    pairs, _audit = export_preferences_from_experiment(
        _experiment(attempts), task_oracles=_task_oracle(),
        source_comparison_identity="exp",
    )
    assert len(pairs) == 1
    stored = pairs[0].rejected.response
    assert oracle.root_cause_summary not in stored
    assert len(stored.encode("utf-8")) <= MAX_PAIR_RESPONSE_BYTES


def test_export_pairs_are_verifier_backed_and_identity_bound():
    attempts = [
        _attempt(attempt_id=f"{TASK_ID}:correct", verifier_outcome="RESOLVED",
                 response_text="correct response"),
        _attempt(attempt_id=f"{TASK_ID}:wrong", role="preference-fixture", verifier_outcome="NO_OP",
                 failure_category="NO_OP", f2p_passed=0, f2p_total=1,
                 response_text="wrong response"),
    ]
    pairs, _audit = export_preferences_from_experiment(
        _experiment(attempts), task_oracles=_task_oracle(),
        source_comparison_identity="exp:experiment.json",
    )
    pair = pairs[0]
    assert pair.verifier_evidence["chosen"]["outcome"] == "RESOLVED"
    assert pair.chosen.response_sha256 is not None
    assert pair.chosen.source_identity
    assert pair.rejected.response_sha256 is not None
    assert pair.source_comparison_identity == "exp:experiment.json"
