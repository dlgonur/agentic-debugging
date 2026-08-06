from __future__ import annotations

import copy

import pytest

from agentic_debugger.evaluation.root_cause_metric import (
    MAX_ROOT_CAUSE_CLAIM_BYTES,
    MAX_ROOT_CAUSE_EVIDENCE_REF_BYTES,
    MAX_ROOT_CAUSE_EVIDENCE_REFS,
    ROOT_CAUSE_OUTCOMES,
    RootCauseAssessment,
    RootCauseMetricError,
    aggregate_root_cause_assessments,
    build_root_cause_assessment,
    derive_root_cause_outcome,
)


TASK_ID = "curated-off-by-one-002"
ATTEMPT_ID = f"base:{TASK_ID}"


def _build(**overrides):
    values = {
        "task_id": TASK_ID,
        "attempt_id": ATTEMPT_ID,
        "assessor_kind": "independent-human",
        "assessor_id": "reviewer-1",
        "claim_text": "The loop bound excludes the final required index.",
        "mechanism": "SATISFIED",
        "failure_connection": "SATISFIED",
        "repair_alignment": "SATISFIED",
        "contradicts_evidence": False,
        "evidence_refs": ("trajectory:event-12", "verifier:f2p-1"),
    }
    values.update(overrides)
    return build_root_cause_assessment(**values)


def test_correct_assessment_round_trips_and_stores_only_claim_hash():
    assessment = _build()
    assert assessment.outcome == "CORRECT"
    mapping = assessment.to_mapping()
    assert mapping["claim_sha256"] is not None
    assert "loop bound" not in str(mapping)
    assert RootCauseAssessment.from_mapping(mapping) == assessment


@pytest.mark.parametrize(
    ("overrides", "outcome"),
    [
        ({"repair_alignment": "PARTIAL"}, "PARTIALLY_CORRECT"),
        ({"mechanism": "NOT_SATISFIED", "failure_connection": "PARTIAL"},
         "PARTIALLY_CORRECT"),
        ({"mechanism": "NOT_SATISFIED",
          "failure_connection": "NOT_SATISFIED"}, "INCORRECT"),
        ({"contradicts_evidence": True}, "INCORRECT"),
    ],
)
def test_assessed_outcomes_are_derived(overrides, outcome):
    assert _build(**overrides).outcome == outcome


def test_not_provided_is_explicit_and_evidence_bound():
    assessment = _build(
        claim_text=None,
        mechanism="NOT_ASSESSED",
        failure_connection="NOT_ASSESSED",
        repair_alignment="NOT_ASSESSED",
        contradicts_evidence=None,
        evidence_refs=("trajectory:no-root-cause-action",),
    )
    assert assessment.outcome == "NOT_PROVIDED"
    assert assessment.claim_sha256 is None


def test_not_assessed_is_distinct_from_missing_claim():
    assessment = _build(
        mechanism="NOT_ASSESSED",
        failure_connection="NOT_ASSESSED",
        repair_alignment="NOT_ASSESSED",
        contradicts_evidence=None,
        evidence_refs=(),
    )
    assert assessment.outcome == "NOT_ASSESSED"
    assert assessment.claim_sha256 is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"mechanism": "NOT_ASSESSED"},
        {"contradicts_evidence": None},
        {"evidence_refs": ()},
        {"claim_text": None},
    ],
)
def test_contradictory_or_incomplete_assessment_shapes_fail_closed(overrides):
    with pytest.raises(RootCauseMetricError):
        _build(**overrides)


def test_missing_claim_with_rating_fails_closed():
    with pytest.raises(RootCauseMetricError, match="missing claim"):
        _build(claim_text=None, evidence_refs=("trajectory:none",))


def test_not_assessed_with_evidence_fails_closed():
    with pytest.raises(RootCauseMetricError, match="cannot carry assessment evidence"):
        _build(
            mechanism="NOT_ASSESSED",
            failure_connection="NOT_ASSESSED",
            repair_alignment="NOT_ASSESSED",
            contradicts_evidence=None,
        )


def test_tampering_with_content_or_identity_is_rejected():
    mapping = _build().to_mapping()
    changed = copy.deepcopy(mapping)
    changed["mechanism"] = "PARTIAL"
    with pytest.raises(RootCauseMetricError, match="outcome|assessment_id"):
        RootCauseAssessment.from_mapping(changed)
    changed = copy.deepcopy(mapping)
    changed["assessment_id"] = "0" * 64
    with pytest.raises(RootCauseMetricError, match="assessment_id"):
        RootCauseAssessment.from_mapping(changed)


def test_claim_and_evidence_bounds_are_enforced():
    with pytest.raises(RootCauseMetricError, match="claim_text exceeds"):
        _build(claim_text="x" * (MAX_ROOT_CAUSE_CLAIM_BYTES + 1))
    with pytest.raises(RootCauseMetricError, match="entry cap"):
        _build(evidence_refs=tuple(f"ref-{i}" for i in range(
            MAX_ROOT_CAUSE_EVIDENCE_REFS + 1
        )))
    with pytest.raises(RootCauseMetricError, match="byte cap"):
        _build(evidence_refs=("x" * (MAX_ROOT_CAUSE_EVIDENCE_REF_BYTES + 1),))


def test_duplicate_evidence_references_are_rejected():
    with pytest.raises(RootCauseMetricError, match="unique"):
        _build(evidence_refs=("event:1", "event:1"))


def test_mapping_is_strict_about_missing_and_unknown_fields():
    mapping = _build().to_mapping()
    missing = dict(mapping)
    missing.pop("outcome")
    with pytest.raises(RootCauseMetricError, match="missing"):
        RootCauseAssessment.from_mapping(missing)
    extra = dict(mapping, surprise=True)
    with pytest.raises(RootCauseMetricError, match="unknown"):
        RootCauseAssessment.from_mapping(extra)


def test_derived_outcome_rejects_non_boolean_contradiction():
    with pytest.raises(RootCauseMetricError, match="bool or null"):
        derive_root_cause_outcome(
            claim_sha256="a" * 64,
            mechanism="SATISFIED",
            failure_connection="SATISFIED",
            repair_alignment="SATISFIED",
            contradicts_evidence=1,
            evidence_refs=("event:1",),
        )


def test_aggregate_keeps_coverage_missingness_and_denominators_visible():
    correct = _build()
    partial = _build(
        attempt_id="agentic:curated-off-by-one-002",
        repair_alignment="PARTIAL",
    )
    aggregate = aggregate_root_cause_assessments(
        (correct, partial),
        expected_attempt_ids=(
            ATTEMPT_ID,
            "agentic:curated-off-by-one-002",
            "rag-assisted:curated-off-by-one-002",
        ),
    )
    assert aggregate == {
        "expected_attempts": 3,
        "assessment_records": 2,
        "missing_assessments": 1,
        "assessed_claims": 2,
        "outcomes": {
            outcome: int(outcome in ("CORRECT", "PARTIALLY_CORRECT"))
            for outcome in ROOT_CAUSE_OUTCOMES
        },
        "assessment_coverage_rate": 0.666667,
        "correct_rate_all_attempts": 0.333333,
        "correct_rate_assessed_claims": 0.5,
    }


def test_aggregate_rejects_duplicate_and_undeclared_attempts():
    assessment = _build()
    with pytest.raises(RootCauseMetricError, match="duplicate"):
        aggregate_root_cause_assessments(
            (assessment, assessment), expected_attempt_ids=(ATTEMPT_ID,)
        )
    with pytest.raises(RootCauseMetricError, match="undeclared"):
        aggregate_root_cause_assessments(
            (assessment,), expected_attempt_ids=("base:curated-wrong-branch-003",)
        )


def test_aggregate_rejects_empty_or_duplicate_expected_ids():
    with pytest.raises(RootCauseMetricError, match="must not be empty"):
        aggregate_root_cause_assessments((), expected_attempt_ids=())
    with pytest.raises(RootCauseMetricError, match="must be unique"):
        aggregate_root_cause_assessments(
            (), expected_attempt_ids=(ATTEMPT_ID, ATTEMPT_ID)
        )
