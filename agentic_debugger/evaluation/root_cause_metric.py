"""Strict, reviewable root-cause explanation assessment contract.

Patch correctness cannot establish that a model explained the cause of a
failure.  This module therefore records an independent rubric assessment and
derives its outcome from explicit dimension judgments.  It deliberately does
not use lexical similarity to hidden oracle text and does not retain the claim
text; only its SHA-256 identity is stored.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


ROOT_CAUSE_ASSESSMENT_SCHEMA_VERSION = "root-cause-assessment-v1"
ROOT_CAUSE_RUBRIC_VERSION = "root-cause-rubric-v1"

ROOT_CAUSE_OUTCOMES = (
    "CORRECT",
    "PARTIALLY_CORRECT",
    "INCORRECT",
    "NOT_PROVIDED",
    "NOT_ASSESSED",
)

ROOT_CAUSE_DIMENSION_RATINGS = (
    "SATISFIED",
    "PARTIAL",
    "NOT_SATISFIED",
    "NOT_ASSESSED",
)

ROOT_CAUSE_ASSESSOR_KINDS = (
    "independent-human",
    "independent-ai",
    "deterministic-fixture",
)

MAX_ROOT_CAUSE_CLAIM_BYTES = 16 * 1024
MAX_ROOT_CAUSE_EVIDENCE_REFS = 32
MAX_ROOT_CAUSE_EVIDENCE_REF_BYTES = 512

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_TASK_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_HEX64_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class RootCauseMetricError(ValueError):
    """Raised when root-cause metric evidence is malformed or contradictory."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_exact_string(value: Any, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RootCauseMetricError(f"{label} must be a non-empty trimmed string")
    return value


def _validate_identifier(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    value = _require_exact_string(value, label)
    if pattern.fullmatch(value) is None:
        raise RootCauseMetricError(f"invalid {label}: {value!r}")
    return value


def _validate_optional_hash(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    value = _require_exact_string(value, label)
    if _HEX64_PATTERN.fullmatch(value) is None:
        raise RootCauseMetricError(f"invalid {label}: {value!r}")
    return value


def _validate_rating(value: Any, label: str) -> str:
    if value not in ROOT_CAUSE_DIMENSION_RATINGS:
        raise RootCauseMetricError(f"unknown {label}: {value!r}")
    return value


def _validate_evidence_refs(value: Any) -> Tuple[str, ...]:
    if type(value) not in (list, tuple):
        raise RootCauseMetricError("evidence_refs must be a list or tuple")
    if len(value) > MAX_ROOT_CAUSE_EVIDENCE_REFS:
        raise RootCauseMetricError(
            f"evidence_refs exceeds the {MAX_ROOT_CAUSE_EVIDENCE_REFS}-entry cap"
        )
    result = []
    for index, item in enumerate(value):
        item = _require_exact_string(item, f"evidence_refs[{index}]")
        if len(item.encode("utf-8")) > MAX_ROOT_CAUSE_EVIDENCE_REF_BYTES:
            raise RootCauseMetricError(
                f"evidence_refs[{index}] exceeds the "
                f"{MAX_ROOT_CAUSE_EVIDENCE_REF_BYTES}-byte cap"
            )
        result.append(item)
    if len(set(result)) != len(result):
        raise RootCauseMetricError("evidence_refs must be unique")
    return tuple(result)


def derive_root_cause_outcome(
    *,
    claim_sha256: Optional[str],
    mechanism: str,
    failure_connection: str,
    repair_alignment: str,
    contradicts_evidence: Optional[bool],
    evidence_refs: Sequence[str],
) -> str:
    """Derive the closed outcome from rubric judgments, failing closed.

    ``NOT_PROVIDED`` and ``NOT_ASSESSED`` are explicit missingness outcomes.
    Any assessed claim needs all three dimensions, a contradiction judgment,
    and at least one evidence reference.  A contradiction is always
    ``INCORRECT``; a fully satisfied, non-contradictory claim is ``CORRECT``;
    a claim that misses both the mechanism and its connection to the observed
    failure is ``INCORRECT``; every other assessed shape is partial.
    """

    _validate_optional_hash(claim_sha256, "claim_sha256")
    ratings = (
        _validate_rating(mechanism, "mechanism"),
        _validate_rating(failure_connection, "failure_connection"),
        _validate_rating(repair_alignment, "repair_alignment"),
    )
    refs = _validate_evidence_refs(evidence_refs)
    if contradicts_evidence is not None and type(contradicts_evidence) is not bool:
        raise RootCauseMetricError("contradicts_evidence must be bool or null")

    if claim_sha256 is None:
        if any(rating != "NOT_ASSESSED" for rating in ratings):
            raise RootCauseMetricError("missing claim cannot carry rubric ratings")
        if contradicts_evidence is not None:
            raise RootCauseMetricError("missing claim cannot carry contradiction judgment")
        if not refs:
            raise RootCauseMetricError("NOT_PROVIDED requires an evidence reference")
        return "NOT_PROVIDED"

    all_not_assessed = all(rating == "NOT_ASSESSED" for rating in ratings)
    any_not_assessed = any(rating == "NOT_ASSESSED" for rating in ratings)
    if all_not_assessed:
        if contradicts_evidence is not None:
            raise RootCauseMetricError(
                "NOT_ASSESSED claim cannot carry contradiction judgment"
            )
        if refs:
            raise RootCauseMetricError(
                "NOT_ASSESSED claim cannot carry assessment evidence"
            )
        return "NOT_ASSESSED"
    if any_not_assessed:
        raise RootCauseMetricError("assessed claim requires all rubric dimensions")
    if type(contradicts_evidence) is not bool:
        raise RootCauseMetricError(
            "assessed claim requires a contradiction judgment"
        )
    if not refs:
        raise RootCauseMetricError("assessed claim requires evidence_refs")
    if contradicts_evidence:
        return "INCORRECT"
    if all(rating == "SATISFIED" for rating in ratings):
        return "CORRECT"
    if (
        mechanism == "NOT_SATISFIED"
        and failure_connection == "NOT_SATISFIED"
    ):
        return "INCORRECT"
    return "PARTIALLY_CORRECT"


@dataclass(frozen=True)
class RootCauseAssessment:
    """One immutable assessment bound to an exact task, attempt, and claim."""

    schema_version: str
    assessment_id: str
    rubric_version: str
    task_id: str
    attempt_id: str
    assessor_kind: str
    assessor_id: str
    claim_sha256: Optional[str]
    mechanism: str
    failure_connection: str
    repair_alignment: str
    contradicts_evidence: Optional[bool]
    evidence_refs: Tuple[str, ...]
    outcome: str

    _FIELDS = {
        "schema_version",
        "assessment_id",
        "rubric_version",
        "task_id",
        "attempt_id",
        "assessor_kind",
        "assessor_id",
        "claim_sha256",
        "mechanism",
        "failure_connection",
        "repair_alignment",
        "contradicts_evidence",
        "evidence_refs",
        "outcome",
    }

    def __post_init__(self) -> None:
        if self.schema_version != ROOT_CAUSE_ASSESSMENT_SCHEMA_VERSION:
            raise RootCauseMetricError(
                f"unsupported schema_version: {self.schema_version!r}"
            )
        if self.rubric_version != ROOT_CAUSE_RUBRIC_VERSION:
            raise RootCauseMetricError(
                f"unsupported rubric_version: {self.rubric_version!r}"
            )
        _validate_identifier(self.task_id, "task_id", _TASK_PATTERN)
        _validate_identifier(self.attempt_id, "attempt_id", _ID_PATTERN)
        if self.assessor_kind not in ROOT_CAUSE_ASSESSOR_KINDS:
            raise RootCauseMetricError(
                f"unknown assessor_kind: {self.assessor_kind!r}"
            )
        _validate_identifier(self.assessor_id, "assessor_id", _ID_PATTERN)
        _validate_optional_hash(self.claim_sha256, "claim_sha256")
        refs = _validate_evidence_refs(self.evidence_refs)
        if refs != self.evidence_refs:
            raise RootCauseMetricError("evidence_refs must be an immutable tuple")
        derived = derive_root_cause_outcome(
            claim_sha256=self.claim_sha256,
            mechanism=self.mechanism,
            failure_connection=self.failure_connection,
            repair_alignment=self.repair_alignment,
            contradicts_evidence=self.contradicts_evidence,
            evidence_refs=self.evidence_refs,
        )
        if self.outcome != derived:
            raise RootCauseMetricError(
                f"outcome {self.outcome!r} does not match derived outcome {derived!r}"
            )
        if self.assessment_id != self.identity():
            raise RootCauseMetricError("assessment_id does not match assessment content")

    def _identity_payload(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "rubric_version": self.rubric_version,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "assessor_kind": self.assessor_kind,
            "assessor_id": self.assessor_id,
            "claim_sha256": self.claim_sha256,
            "mechanism": self.mechanism,
            "failure_connection": self.failure_connection,
            "repair_alignment": self.repair_alignment,
            "contradicts_evidence": self.contradicts_evidence,
            "evidence_refs": list(self.evidence_refs),
            "outcome": self.outcome,
        }

    def identity(self) -> str:
        return _sha256_text(_canonical_json(self._identity_payload()))

    def to_mapping(self) -> Dict[str, Any]:
        return {"assessment_id": self.assessment_id, **self._identity_payload()}

    @staticmethod
    def from_mapping(value: Any) -> "RootCauseAssessment":
        if not isinstance(value, Mapping):
            raise RootCauseMetricError("root-cause assessment must be a mapping")
        missing = RootCauseAssessment._FIELDS - set(value)
        extra = set(value) - RootCauseAssessment._FIELDS
        if missing:
            raise RootCauseMetricError(
                f"missing root-cause assessment fields: {sorted(missing)}"
            )
        if extra:
            raise RootCauseMetricError(
                f"unknown root-cause assessment fields: {sorted(extra)}"
            )
        refs = _validate_evidence_refs(value["evidence_refs"])
        contradiction = value["contradicts_evidence"]
        if contradiction is not None and type(contradiction) is not bool:
            raise RootCauseMetricError("contradicts_evidence must be bool or null")
        return RootCauseAssessment(
            schema_version=_require_exact_string(
                value["schema_version"], "schema_version"
            ),
            assessment_id=_validate_identifier(
                value["assessment_id"], "assessment_id", _HEX64_PATTERN
            ),
            rubric_version=_require_exact_string(
                value["rubric_version"], "rubric_version"
            ),
            task_id=_validate_identifier(value["task_id"], "task_id", _TASK_PATTERN),
            attempt_id=_validate_identifier(
                value["attempt_id"], "attempt_id", _ID_PATTERN
            ),
            assessor_kind=_require_exact_string(
                value["assessor_kind"], "assessor_kind"
            ),
            assessor_id=_validate_identifier(
                value["assessor_id"], "assessor_id", _ID_PATTERN
            ),
            claim_sha256=_validate_optional_hash(
                value["claim_sha256"], "claim_sha256"
            ),
            mechanism=_validate_rating(value["mechanism"], "mechanism"),
            failure_connection=_validate_rating(
                value["failure_connection"], "failure_connection"
            ),
            repair_alignment=_validate_rating(
                value["repair_alignment"], "repair_alignment"
            ),
            contradicts_evidence=contradiction,
            evidence_refs=refs,
            outcome=_require_exact_string(value["outcome"], "outcome"),
        )


def build_root_cause_assessment(
    *,
    task_id: str,
    attempt_id: str,
    assessor_kind: str,
    assessor_id: str,
    claim_text: Optional[str],
    mechanism: str = "NOT_ASSESSED",
    failure_connection: str = "NOT_ASSESSED",
    repair_alignment: str = "NOT_ASSESSED",
    contradicts_evidence: Optional[bool] = None,
    evidence_refs: Sequence[str] = (),
) -> RootCauseAssessment:
    """Build a canonical assessment without storing oracle or claim text."""

    if claim_text is None:
        claim_sha256 = None
    else:
        if type(claim_text) is not str or not claim_text.strip():
            raise RootCauseMetricError("claim_text must be non-empty or null")
        if len(claim_text.encode("utf-8")) > MAX_ROOT_CAUSE_CLAIM_BYTES:
            raise RootCauseMetricError(
                f"claim_text exceeds the {MAX_ROOT_CAUSE_CLAIM_BYTES}-byte cap"
            )
        claim_sha256 = _sha256_text(claim_text)
    refs = _validate_evidence_refs(evidence_refs)
    outcome = derive_root_cause_outcome(
        claim_sha256=claim_sha256,
        mechanism=mechanism,
        failure_connection=failure_connection,
        repair_alignment=repair_alignment,
        contradicts_evidence=contradicts_evidence,
        evidence_refs=refs,
    )
    payload = {
        "schema_version": ROOT_CAUSE_ASSESSMENT_SCHEMA_VERSION,
        "rubric_version": ROOT_CAUSE_RUBRIC_VERSION,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "assessor_kind": assessor_kind,
        "assessor_id": assessor_id,
        "claim_sha256": claim_sha256,
        "mechanism": mechanism,
        "failure_connection": failure_connection,
        "repair_alignment": repair_alignment,
        "contradicts_evidence": contradicts_evidence,
        "evidence_refs": list(refs),
        "outcome": outcome,
    }
    assessment_id = _sha256_text(_canonical_json(payload))
    return RootCauseAssessment(
        assessment_id=assessment_id,
        evidence_refs=refs,
        **{key: value for key, value in payload.items() if key != "evidence_refs"},
    )


def aggregate_root_cause_assessments(
    assessments: Iterable[RootCauseAssessment],
    *,
    expected_attempt_ids: Sequence[str],
) -> Dict[str, Any]:
    """Aggregate explicit outcomes while keeping missingness visible."""

    expected = tuple(expected_attempt_ids)
    if not expected:
        raise RootCauseMetricError("expected_attempt_ids must not be empty")
    if any(type(item) is not str or _ID_PATTERN.fullmatch(item) is None for item in expected):
        raise RootCauseMetricError("expected_attempt_ids contains an invalid identity")
    if len(set(expected)) != len(expected):
        raise RootCauseMetricError("expected_attempt_ids must be unique")

    by_attempt: Dict[str, RootCauseAssessment] = {}
    for assessment in assessments:
        if type(assessment) is not RootCauseAssessment:
            raise RootCauseMetricError(
                "assessments must contain exact RootCauseAssessment values"
            )
        if assessment.attempt_id not in expected:
            raise RootCauseMetricError(
                f"assessment references undeclared attempt {assessment.attempt_id!r}"
            )
        if assessment.attempt_id in by_attempt:
            raise RootCauseMetricError(
                f"duplicate assessment for attempt {assessment.attempt_id!r}"
            )
        by_attempt[assessment.attempt_id] = assessment

    counts = {outcome: 0 for outcome in ROOT_CAUSE_OUTCOMES}
    for assessment in by_attempt.values():
        counts[assessment.outcome] += 1
    total = len(expected)
    present = len(by_attempt)
    assessed = counts["CORRECT"] + counts["PARTIALLY_CORRECT"] + counts["INCORRECT"]
    return {
        "expected_attempts": total,
        "assessment_records": present,
        "missing_assessments": total - present,
        "assessed_claims": assessed,
        "outcomes": counts,
        "assessment_coverage_rate": round(present / total, 6),
        "correct_rate_all_attempts": round(counts["CORRECT"] / total, 6),
        "correct_rate_assessed_claims": (
            round(counts["CORRECT"] / assessed, 6) if assessed else None
        ),
    }


__all__ = [
    "ROOT_CAUSE_ASSESSMENT_SCHEMA_VERSION",
    "ROOT_CAUSE_RUBRIC_VERSION",
    "ROOT_CAUSE_OUTCOMES",
    "ROOT_CAUSE_DIMENSION_RATINGS",
    "ROOT_CAUSE_ASSESSOR_KINDS",
    "MAX_ROOT_CAUSE_CLAIM_BYTES",
    "MAX_ROOT_CAUSE_EVIDENCE_REFS",
    "MAX_ROOT_CAUSE_EVIDENCE_REF_BYTES",
    "RootCauseMetricError",
    "RootCauseAssessment",
    "derive_root_cause_outcome",
    "build_root_cause_assessment",
    "aggregate_root_cause_assessments",
]
