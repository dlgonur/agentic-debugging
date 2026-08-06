"""Normalized metric derivation for comparison experiments.

This module derives, from the strict attempt records only:

* the normalized failure category (closed vocabulary, distinguishing
  strict-parse / apply / syntax / verifier phases);
* per-condition aggregates over **evaluation-role** attempts only
  (auxiliary ``preference-fixture`` attempts are counted separately and can
  never affect resolved rate, valid-patch rate, F2P/P2P or deltas);
* the delta of every non-baseline condition against the declared baseline.

Timing and cost are deterministic values *inside* an attempt record, but
aggregate averages of runtime/latency are kept in the separate nondeterministic
``timing`` section, matching the repository's byte-stability conventions.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from agentic_debugger.comparison.schema import (
    FAILURE_CATEGORIES,
    AttemptRecord,
)
from agentic_debugger.evaluation.root_cause_metric import (
    RootCauseAssessment,
    RootCauseMetricError,
)


class MetricsError(ValueError):
    """Raised when metrics cannot be derived from attempt records."""


#: Deterministic aggregate metrics (excluded: runtime, cost, latency).
AGGREGATE_METRICS = (
    "attempts",
    "auxiliary_attempts",
    "generation_produced",
    "valid_patch",
    "resolved",
    "resolved_rate",
    "correct_target_file",
    "root_cause_assessment_records",
    "root_cause_missing_assessments",
    "root_cause_assessed_claims",
    "root_cause_correct",
    "root_cause_partially_correct",
    "root_cause_incorrect",
    "root_cause_not_provided",
    "root_cause_not_assessed",
    "root_cause_assessment_coverage_rate",
    "root_cause_correct_rate",
    "f2p_passed",
    "f2p_total",
    "p2p_passed",
    "p2p_total",
    "replay_valid",
    "cleanup_ok",
    "canonical_fixture_unchanged",
    "provider_attempts",
    "network_attempts",
    "external_provider_attempts",
    "external_network_attempts",
    "retrieval_count",
    "memory_bytes",
)

#: Metric names that are summed; everything else is a rate/count pair.
_SUM_METRICS = {
    "attempts",
    "auxiliary_attempts",
    "generation_produced",
    "valid_patch",
    "resolved",
    "correct_target_file",
    "root_cause_assessment_records",
    "root_cause_missing_assessments",
    "root_cause_assessed_claims",
    "root_cause_correct",
    "root_cause_partially_correct",
    "root_cause_incorrect",
    "root_cause_not_provided",
    "root_cause_not_assessed",
    "f2p_passed",
    "f2p_total",
    "p2p_passed",
    "p2p_total",
    "replay_valid",
    "cleanup_ok",
    "canonical_fixture_unchanged",
    "provider_attempts",
    "network_attempts",
    "external_provider_attempts",
    "external_network_attempts",
    "retrieval_count",
    "memory_bytes",
}


def normalize_failure_category(facts: Mapping[str, Any]) -> Optional[str]:
    """Map verifier/attempt facts to the closed failure-category vocabulary.

    The mapping distinguishes: no generation, no patch, malformed patch
    (strict-parse phase), patch not applied, syntax failure, verifier
    infrastructure failure, and the semantic outcome categories.
    """

    outcome = facts.get("verifier_outcome")
    if outcome is not None:
        if outcome == "RESOLVED":
            return None
        if outcome == "NO_OP":
            return "NO_OP"
        if outcome in ("REGRESSION", "BREAKING_RESOLVED"):
            return "P2P_REGRESSION"
        if outcome in ("WORK_IN_PROGRESS", "PARTIALLY_RESOLVED"):
            return "F2P_NOT_PASSED"
        return "UNCLASSIFIED"
    if not facts.get("generation_produced"):
        return "NO_GENERATION"
    if not facts.get("valid_patch"):
        if not facts.get("patch_present"):
            return "NO_PATCH"
        if facts.get("patch_applied") is False:
            return "PATCH_NOT_APPLIED"
        return "PATCH_INVALID"
    if facts.get("syntax_passed") is False:
        return "SYNTAX_FAILED"
    verifier_status = facts.get("verifier_status")
    if verifier_status is None or verifier_status != "COMPLETED":
        return "VERIFIER_FAILED"
    return "UNCLASSIFIED"


def attempt_facts(attempt: AttemptRecord) -> Dict[str, Any]:
    """The deterministic facts used for categorization and aggregates."""

    evidence = attempt.verifier_evidence or {}
    return {
        "generation_produced": attempt.generation_produced,
        "valid_patch": attempt.valid_patch,
        "patch_present": attempt.patch_sha256 is not None,
        "patch_applied": evidence.get("patch_applied"),
        "syntax_passed": evidence.get("syntax_passed"),
        "verifier_outcome": attempt.verifier_outcome,
        "verifier_status": attempt.verifier_status,
        "f2p_passed": attempt.f2p_passed,
        "f2p_total": attempt.f2p_total,
        "p2p_passed": attempt.p2p_passed,
        "p2p_total": attempt.p2p_total,
        "patch_sha256": attempt.patch_sha256,
        "changed_file_count": attempt.changed_file_count,
    }


def _rate(passed: Optional[int], total: Optional[int]) -> Optional[float]:
    if total is None or passed is None or total == 0:
        return None
    return round(passed / total, 6)


def root_cause_assessment_for_attempt(
    attempt: AttemptRecord,
) -> Optional[RootCauseAssessment]:
    """Load and bind an optional assessment from attempt provenance.

    Keeping the additive record in ``provenance`` preserves the strict
    ``comparison-v1`` wire schema.  If present, it is still validated through
    the independent assessment schema and must match the attempt/task exactly.
    """

    raw = attempt.provenance.get("root_cause_assessment")
    if raw is None:
        return None
    try:
        assessment = RootCauseAssessment.from_mapping(raw)
    except RootCauseMetricError as exc:
        raise MetricsError(
            f"invalid root-cause assessment for {attempt.attempt_id!r}: {exc}"
        ) from exc
    if assessment.attempt_id != attempt.attempt_id:
        raise MetricsError(
            f"root-cause assessment attempt mismatch for {attempt.attempt_id!r}"
        )
    if assessment.task_id != attempt.task_id:
        raise MetricsError(
            f"root-cause assessment task mismatch for {attempt.attempt_id!r}"
        )
    return assessment


def aggregate_condition(
    attempts: Sequence[AttemptRecord],
    *,
    include_timing: bool = False,
) -> Dict[str, Any]:
    """Derive the deterministic per-condition aggregate bucket.

    Only ``evaluation``-role attempts contribute to the aggregate; auxiliary
    attempts are counted separately and excluded from every performance
    metric.
    """

    attempts = tuple(attempts)
    if not attempts:
        raise MetricsError("aggregate requires at least one attempt")
    condition = attempts[0].condition_id
    if any(a.condition_id != condition for a in attempts):
        raise MetricsError("aggregate over mixed conditions")
    primary = [a for a in attempts if a.role == "evaluation"]
    auxiliary = [a for a in attempts if a.role != "evaluation"]
    if not primary:
        raise MetricsError(
            f"condition {condition!r} has no evaluation-role attempts"
        )

    def _count(predicate) -> int:
        return sum(1 for a in primary if predicate(a))

    def _sum_int(field: str) -> int:
        return sum(int(getattr(a, field) or 0) for a in primary)

    resolved = _count(lambda a: a.verifier_outcome == "RESOLVED")
    root_cause_assessments = [
        assessment
        for attempt in primary
        if (assessment := root_cause_assessment_for_attempt(attempt)) is not None
    ]
    root_cause_counts = {
        outcome: sum(1 for assessment in root_cause_assessments if assessment.outcome == outcome)
        for outcome in (
            "CORRECT",
            "PARTIALLY_CORRECT",
            "INCORRECT",
            "NOT_PROVIDED",
            "NOT_ASSESSED",
        )
    }
    root_cause_assessed = (
        root_cause_counts["CORRECT"]
        + root_cause_counts["PARTIALLY_CORRECT"]
        + root_cause_counts["INCORRECT"]
    )
    bucket = {
        "condition_id": condition,
        "attempts": len(primary),
        "auxiliary_attempts": len(auxiliary),
        "generation_produced": _count(lambda a: a.generation_produced),
        "valid_patch": _count(lambda a: a.valid_patch),
        "resolved": resolved,
        "resolved_rate": _rate(resolved, len(primary)),
        "correct_target_file": _count(lambda a: a.correct_target_file is True),
        "root_cause_assessment_records": len(root_cause_assessments),
        "root_cause_missing_assessments": len(primary) - len(root_cause_assessments),
        "root_cause_assessed_claims": root_cause_assessed,
        "root_cause_correct": root_cause_counts["CORRECT"],
        "root_cause_partially_correct": root_cause_counts["PARTIALLY_CORRECT"],
        "root_cause_incorrect": root_cause_counts["INCORRECT"],
        "root_cause_not_provided": root_cause_counts["NOT_PROVIDED"],
        "root_cause_not_assessed": root_cause_counts["NOT_ASSESSED"],
        "root_cause_assessment_coverage_rate": _rate(
            len(root_cause_assessments), len(primary)
        ),
        "root_cause_correct_rate": _rate(
            root_cause_counts["CORRECT"], len(primary)
        ),
        "f2p_passed": _sum_int("f2p_passed"),
        "f2p_total": _sum_int("f2p_total"),
        "p2p_passed": _sum_int("p2p_passed"),
        "p2p_total": _sum_int("p2p_total"),
        "replay_valid": _count(lambda a: a.replay_valid is True),
        "cleanup_ok": _count(lambda a: a.cleanup_status == "cleaned"),
        "canonical_fixture_unchanged": _count(
            lambda a: a.canonical_fixture_unchanged is True
        ),
        "provider_attempts": _sum_int("provider_attempts"),
        "network_attempts": _sum_int("network_attempts"),
        "external_provider_attempts": _sum_int("external_provider_attempts"),
        "external_network_attempts": _sum_int("external_network_attempts"),
        "retrieval_count": _sum_int("retrieval_count"),
        "memory_bytes": _sum_int("memory_bytes"),
        "failure_categories": {},
    }
    for category in FAILURE_CATEGORIES:
        bucket["failure_categories"][category] = _count(
            lambda a, c=category: a.failure_category == c
        )
    if include_timing:
        runtimes = [a.runtime_ms for a in primary if a.runtime_ms is not None]
        costs = [a.cost for a in primary if a.cost is not None]
        latencies = [
            a.retrieval_latency_ms for a in primary if a.retrieval_latency_ms is not None
        ]
        bucket["timing"] = {
            "runtime_ms_avg": (
                round(sum(runtimes) / len(runtimes)) if runtimes else None
            ),
            "cost_sum": round(sum(costs), 6) if costs else None,
            "retrieval_latency_ms_avg": (
                round(sum(latencies) / len(latencies)) if latencies else None
            ),
        }
    return bucket


def aggregate_all(experiment: Any) -> Dict[str, Any]:
    """Per-condition aggregates in declared condition order."""

    by_condition: Dict[str, List[AttemptRecord]] = {c: [] for c in experiment.conditions}
    for attempt in experiment.attempts:
        by_condition[attempt.condition_id].append(attempt)
    return {
        "conditions": [
            aggregate_condition(by_condition[condition])
            for condition in experiment.conditions
        ]
    }


def delta_against_baseline(
    experiment: Any, aggregates: Mapping[str, Any]
) -> Dict[str, Any]:
    """Delta of each non-baseline condition against the declared baseline.

    Aggregates are evaluation-role only, so auxiliary attempts can never
    manufacture a delta.  Returns a list of ``{condition_id, task_id
    (nullable), metric, baseline_value, value, delta}`` entries.
    """

    baseline = experiment.baseline_condition
    aggregates_by_condition = {
        item["condition_id"]: item for item in aggregates["conditions"]
    }
    if baseline not in aggregates_by_condition:
        raise MetricsError("baseline condition has no aggregate")
    base = aggregates_by_condition[baseline]

    entries: List[Dict[str, Any]] = []

    def _compare(label: str, metric: str, base_value: Any, value: Any) -> None:
        if base_value is None or value is None:
            delta: Optional[float] = None
        elif isinstance(base_value, (int, float)) and isinstance(value, (int, float)):
            delta = round(float(value) - float(base_value), 6)
        else:
            delta = None
        entries.append(
            {
                "condition_id": label,
                "task_id": None,
                "metric": metric,
                "baseline_value": base_value,
                "value": value,
                "delta": delta,
            }
        )

    for condition in experiment.conditions:
        if condition == baseline:
            continue
        bucket = aggregates_by_condition[condition]
        for metric in AGGREGATE_METRICS:
            _compare(condition, metric, base.get(metric), bucket.get(metric))

    # Per-task deltas for the central outcome metrics (evaluation-role only).
    per_task_base: Dict[str, Dict[str, Any]] = {}
    for task_id in experiment.task_ids:
        task_base = [a for a in experiment.attempts
                     if a.task_id == task_id and a.condition_id == baseline
                     and a.role == "evaluation"]
        resolved = sum(1 for a in task_base if a.verifier_outcome == "RESOLVED")
        per_task_base[task_id] = {
            "resolved": resolved,
            "resolved_rate": _rate(resolved, len(task_base)),
            "valid_patch": sum(1 for a in task_base if a.valid_patch),
        }
    for condition in experiment.conditions:
        if condition == baseline:
            continue
        for task_id in experiment.task_ids:
            task_attempts = [
                a for a in experiment.attempts
                if a.task_id == task_id and a.condition_id == condition
                and a.role == "evaluation"
            ]
            resolved = sum(1 for a in task_attempts if a.verifier_outcome == "RESOLVED")
            values = {
                "resolved": resolved,
                "resolved_rate": _rate(resolved, len(task_attempts)),
                "valid_patch": sum(1 for a in task_attempts if a.valid_patch),
            }
            for metric, value in values.items():
                base_value = per_task_base[task_id].get(metric)
                delta = (
                    None
                    if base_value is None or value is None
                    else round(float(value) - float(base_value), 6)
                )
                entries.append(
                    {
                        "condition_id": condition,
                        "task_id": task_id,
                        "metric": metric,
                        "baseline_value": base_value,
                        "value": value,
                        "delta": delta,
                    }
                )
    entries.sort(
        key=lambda item: (item["condition_id"], item["task_id"] or "", item["metric"])
    )
    return {"delta_entries": entries}


# ---------------------------------------------------------------------------
# CSV projection
# ---------------------------------------------------------------------------

#: Fixed CSV column order (one row per attempt).
CSV_COLUMNS: Tuple[str, ...] = (
    "attempt_id",
    "condition_id",
    "task_id",
    "mode",
    "role",
    "generation_produced",
    "valid_patch",
    "correct_target_file",
    "localization_outcome",
    "root_cause_outcome",
    "root_cause_assessment_id",
    "f2p_passed",
    "f2p_total",
    "p2p_passed",
    "p2p_total",
    "verifier_outcome",
    "failure_category",
    "runtime_ms",
    "memory_bytes",
    "cost",
    "tokens",
    "retrieval_count",
    "retrieval_bytes",
    "retrieval_latency_ms",
    "replay_valid",
    "cleanup_status",
    "canonical_fixture_unchanged",
    "provider_attempts",
    "network_attempts",
    "external_provider_attempts",
    "external_network_attempts",
    "changed_file_count",
    "patch_sha256",
)


def csv_rows(experiment: Any) -> Tuple[Tuple[str, ...], ...]:
    """Deterministic CSV rows (header + one row per attempt, sorted)."""

    attempts = sorted(
        experiment.attempts,
        key=lambda a: (a.condition_id, a.task_id, a.attempt_id),
    )
    rows: List[Tuple[str, ...]] = [CSV_COLUMNS]
    for attempt in attempts:
        record = attempt.to_mapping()
        assessment = root_cause_assessment_for_attempt(attempt)
        record["root_cause_outcome"] = (
            assessment.outcome if assessment is not None else None
        )
        record["root_cause_assessment_id"] = (
            assessment.assessment_id if assessment is not None else None
        )
        rows.append(
            tuple(
                "" if record.get(column) is None else str(record.get(column))
                for column in CSV_COLUMNS
            )
        )
    return tuple(rows)


def to_csv_text(rows: Sequence[Sequence[str]]) -> str:
    """Deterministic CSV text (LF newlines; no quoting surprises in metrics)."""

    lines: List[str] = []
    for row in rows:
        cells = [str(cell) for cell in row]
        lines.append(",".join(cells))
    return "\n".join(lines) + "\n"


__all__ = [
    "FAILURE_CATEGORIES",
    "AGGREGATE_METRICS",
    "CSV_COLUMNS",
    "MetricsError",
    "normalize_failure_category",
    "attempt_facts",
    "root_cause_assessment_for_attempt",
    "aggregate_condition",
    "aggregate_all",
    "delta_against_baseline",
    "csv_rows",
    "to_csv_text",
]
