"""Normalized comparison experiment schema (``comparison-v1``).

One comparison experiment is a strict, versioned document containing:

* the declared condition identities (unique) and the single declared
  baseline condition (must be one of them);
* the declared task identities;
* one normalized attempt record per executed attempt (imported or native),
  each with a strict role: ``evaluation`` (primary comparison evidence) or
  ``preference-fixture`` (auxiliary, verifier-backed preference evidence);
* nondeterministic ``environment`` and ``timing`` sections kept apart from
  the deterministic payload.

Contract hardening (repair 1):

* attempt roles — at most one primary ``evaluation`` attempt per
  ``(task_id, condition_id)``; primary aggregates and baseline deltas use
  evaluation-role attempts only; auxiliary attempts are identified in every
  report and can never pollute performance metrics;
* strict attempt invariants — identifier patterns, ``passed <= total``,
  patch/response hash consistency, closed failure-category vocabulary,
  cleanup/verifier consistency;
* free-form JSON payloads (provenance, verifier evidence, environment,
  timing, notes) are recursively validated with bounded depth/entries/
  strings/bytes and finite numbers only.

The document is validated fail-closed: no unknown fields, no missing fields,
no NaN/Infinity, canonical JSON serialization.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

COMPARISON_SCHEMA_VERSION = "comparison-v1"

#: Recommended condition identities (not mandatory; uniqueness is mandatory).
CONDITION_IDS = ("base", "fine-tuned", "rag-assisted", "agentic")

#: Attempt roles (closed vocabulary).
ATTEMPT_ROLES = ("evaluation", "preference-fixture")

#: Closed vocabulary of normalized failure categories (``None`` = RESOLVED).
FAILURE_CATEGORIES = (
    "NO_GENERATION",
    "NO_PATCH",
    "PATCH_INVALID",
    "PATCH_NOT_APPLIED",
    "SYNTAX_FAILED",
    "VERIFIER_FAILED",
    "NO_OP",
    "F2P_NOT_PASSED",
    "P2P_REGRESSION",
    "NOT_REPRODUCED",
    "UNCLASSIFIED",
)

_EXPERIMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ATTEMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CONDITION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_HEX64_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")

#: Bound on the response text retained per attempt (preference evidence).
#: The deterministic truncation marker is included inside this budget.
MAX_RESPONSE_TEXT_BYTES = 64 * 1024
_RESPONSE_TRUNCATION_MARKER = "\n[response-truncated]\n"


class ComparisonError(ValueError):
    """Base error for the comparison subsystem."""


class ComparisonInputError(ComparisonError):
    """Raised for invalid comparison inputs."""


class ComparisonInvariantError(ComparisonError):
    """Raised when comparison contracts are violated."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bound_response_text(
    text: str,
    *,
    cap: int = MAX_RESPONSE_TEXT_BYTES,
    marker: str = _RESPONSE_TRUNCATION_MARKER,
) -> str:
    """Deterministically bound a response for storage.

    The marker is included **inside** the exact ``cap`` byte budget; the cut
    never splits a UTF-8 code point; no replacement characters are
    introduced; the exact output byte length never exceeds ``cap``.
    """

    if type(text) is not str:
        raise ComparisonInvariantError("bound_response_text requires a string")
    if type(cap) is not int or isinstance(cap, bool) or cap < 1:
        raise ComparisonInvariantError("cap must be a positive integer")
    if type(marker) is not str or not marker:
        raise ComparisonInvariantError("marker must be a non-empty string")
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= cap:
        raise ComparisonInvariantError("truncation marker exceeds the cap")
    budget = cap - len(marker_bytes)
    cut = encoded[:budget]
    # Step back to a valid UTF-8 boundary (at most 3 bytes for a 4-byte
    # sequence) so no code point is split and no replacement character is
    # introduced by decoding.
    for _ in range(4):
        try:
            cut.decode("utf-8")
            break
        except UnicodeDecodeError:
            cut = cut[:-1]
    else:
        raise ComparisonInvariantError("could not find a valid UTF-8 cut boundary")
    bounded = cut.decode("utf-8") + marker
    if len(bounded.encode("utf-8")) > cap:
        raise ComparisonInvariantError("bounded response exceeds the cap")
    return bounded


def _ensure_str(v: Any, label: str) -> str:
    if type(v) is not str or not v:
        raise ComparisonInvariantError(f"{label} must be a non-empty string")
    return v


def _ensure_optional_str(v: Any, label: str) -> Optional[str]:
    if v is None:
        return None
    return _ensure_str(v, label)


def _ensure_bool(v: Any, label: str) -> bool:
    if type(v) is not bool:
        raise ComparisonInvariantError(f"{label} must be a boolean")
    return v


def _ensure_int_or_none(v: Any, label: str) -> Optional[int]:
    if v is None:
        return None
    if type(v) is not int or isinstance(v, bool) or v < 0:
        raise ComparisonInvariantError(f"{label} must be a non-negative integer or null")
    return v


def _ensure_finite_or_none(v: Any, label: str) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool) or type(v) not in (int, float):
        raise ComparisonInvariantError(f"{label} must be a finite number or null")
    value = float(v)
    if not math.isfinite(value):
        raise ComparisonInvariantError(f"{label} must be finite")
    return value


def _check_required_fields(m: Mapping[str, Any], required: set, label: str) -> None:
    missing = required - set(m.keys())
    if missing:
        raise ComparisonInvariantError(f"Missing required fields in {label}: {sorted(missing)}")


def _check_no_unknown_fields(m: Mapping[str, Any], known: set, label: str) -> None:
    extra = set(m.keys()) - known
    if extra:
        raise ComparisonInvariantError(f"Unknown fields in {label}: {sorted(extra)}")


def _validate_json_bounds(value: Any, label: str) -> None:
    from agentic_debugger.comparison.json_bounds import validate_json_bounds

    try:
        validate_json_bounds(value)
    except ComparisonInvariantError as exc:
        raise ComparisonInvariantError(f"{label} {exc}") from exc


# ---------------------------------------------------------------------------
# Attempt record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptRecord:
    """One normalized, verifier-backed attempt in a comparison experiment."""

    attempt_id: str
    condition_id: str
    task_id: str
    mode: str
    role: str
    source_identity: str
    generation_produced: bool
    valid_patch: bool
    patch_sha256: Optional[str]
    changed_file_count: Optional[int]
    correct_target_file: Optional[bool]
    localization_outcome: Optional[str]
    f2p_passed: Optional[int]
    f2p_total: Optional[int]
    p2p_passed: Optional[int]
    p2p_total: Optional[int]
    verifier_outcome: Optional[str]
    verifier_status: Optional[str]
    failure_category: Optional[str]
    runtime_ms: Optional[int]
    memory_bytes: Optional[int]
    cost: Optional[float]
    tokens: Optional[int]
    retrieval_count: Optional[int]
    retrieval_bytes: Optional[int]
    retrieval_latency_ms: Optional[int]
    replay_valid: Optional[bool]
    cleanup_status: Optional[str]
    canonical_fixture_unchanged: Optional[bool]
    provider_attempts: int
    network_attempts: int
    external_provider_attempts: Optional[int]
    external_network_attempts: Optional[int]
    response_text: Optional[str]
    response_sha256: Optional[str]
    verifier_evidence: Optional[Dict[str, Any]]
    provenance: Dict[str, Any]

    _KNOWN_FIELDS = {
        "attempt_id", "condition_id", "task_id", "mode", "role", "source_identity",
        "generation_produced", "valid_patch", "patch_sha256", "changed_file_count",
        "correct_target_file", "localization_outcome", "f2p_passed", "f2p_total",
        "p2p_passed", "p2p_total", "verifier_outcome", "verifier_status",
        "failure_category", "runtime_ms", "memory_bytes", "cost", "tokens",
        "retrieval_count", "retrieval_bytes", "retrieval_latency_ms", "replay_valid",
        "cleanup_status", "canonical_fixture_unchanged", "provider_attempts",
        "network_attempts", "external_provider_attempts", "external_network_attempts",
        "response_text", "response_sha256", "verifier_evidence", "provenance",
    }

    def __post_init__(self) -> None:
        _ensure_str(self.attempt_id, "attempt_id")
        if _ATTEMPT_ID_PATTERN.match(self.attempt_id) is None:
            raise ComparisonInvariantError(f"invalid attempt_id: {self.attempt_id!r}")
        _ensure_str(self.condition_id, "condition_id")
        if _CONDITION_ID_PATTERN.match(self.condition_id) is None:
            raise ComparisonInvariantError(f"invalid condition_id: {self.condition_id!r}")
        _ensure_str(self.task_id, "task_id")
        if _TASK_ID_PATTERN.match(self.task_id) is None:
            raise ComparisonInvariantError(f"invalid task_id: {self.task_id!r}")
        if self.mode not in {"imported", "native"}:
            raise ComparisonInvariantError(f"unknown mode: {self.mode!r}")
        if self.role not in ATTEMPT_ROLES:
            raise ComparisonInvariantError(f"unknown attempt role: {self.role!r}")
        _ensure_str(self.source_identity, "source_identity")
        _ensure_bool(self.generation_produced, "generation_produced")
        _ensure_bool(self.valid_patch, "valid_patch")
        _ensure_optional_str(self.patch_sha256, "patch_sha256")
        if self.patch_sha256 is not None and _HEX64_PATTERN.match(self.patch_sha256) is None:
            raise ComparisonInvariantError(f"invalid patch_sha256: {self.patch_sha256!r}")
        if self.valid_patch and self.patch_sha256 is None:
            raise ComparisonInvariantError("valid patch without patch identity")
        _ensure_int_or_none(self.changed_file_count, "changed_file_count")
        if self.correct_target_file is not None:
            _ensure_bool(self.correct_target_file, "correct_target_file")
        _ensure_optional_str(self.localization_outcome, "localization_outcome")
        _ensure_int_or_none(self.f2p_passed, "f2p_passed")
        _ensure_int_or_none(self.f2p_total, "f2p_total")
        _ensure_int_or_none(self.p2p_passed, "p2p_passed")
        _ensure_int_or_none(self.p2p_total, "p2p_total")
        if (
            self.f2p_passed is not None
            and self.f2p_total is not None
            and self.f2p_passed > self.f2p_total
        ):
            raise ComparisonInvariantError("f2p_passed exceeds f2p_total")
        if (
            self.p2p_passed is not None
            and self.p2p_total is not None
            and self.p2p_passed > self.p2p_total
        ):
            raise ComparisonInvariantError("p2p_passed exceeds p2p_total")
        _ensure_optional_str(self.verifier_outcome, "verifier_outcome")
        _ensure_optional_str(self.verifier_status, "verifier_status")
        if self.failure_category is not None:
            if self.failure_category not in FAILURE_CATEGORIES:
                raise ComparisonInvariantError(
                    f"failure_category {self.failure_category!r} is not in the "
                    f"closed vocabulary"
                )
        _ensure_int_or_none(self.runtime_ms, "runtime_ms")
        _ensure_int_or_none(self.memory_bytes, "memory_bytes")
        _ensure_finite_or_none(self.cost, "cost")
        _ensure_int_or_none(self.tokens, "tokens")
        _ensure_int_or_none(self.retrieval_count, "retrieval_count")
        _ensure_int_or_none(self.retrieval_bytes, "retrieval_bytes")
        _ensure_int_or_none(self.retrieval_latency_ms, "retrieval_latency_ms")
        if self.replay_valid is not None:
            _ensure_bool(self.replay_valid, "replay_valid")
        _ensure_optional_str(self.cleanup_status, "cleanup_status")
        if self.canonical_fixture_unchanged is not None:
            _ensure_bool(self.canonical_fixture_unchanged, "canonical_fixture_unchanged")
        _ensure_int_or_none(self.provider_attempts, "provider_attempts")
        _ensure_int_or_none(self.network_attempts, "network_attempts")
        if self.provider_attempts is None or self.network_attempts is None:
            raise ComparisonInvariantError("provider/network attempts must be present")
        _ensure_int_or_none(self.external_provider_attempts, "external_provider_attempts")
        _ensure_int_or_none(self.external_network_attempts, "external_network_attempts")
        _ensure_optional_str(self.response_text, "response_text")
        if self.response_text is not None and len(self.response_text.encode("utf-8")) > MAX_RESPONSE_TEXT_BYTES:
            raise ComparisonInvariantError(
                f"response_text exceeds the {MAX_RESPONSE_TEXT_BYTES}-byte cap"
            )
        _ensure_optional_str(self.response_sha256, "response_sha256")
        if self.response_sha256 is not None and _HEX64_PATTERN.match(self.response_sha256) is None:
            raise ComparisonInvariantError(f"invalid response_sha256: {self.response_sha256!r}")
        if self.response_text is not None:
            if self.response_sha256 is None:
                raise ComparisonInvariantError("response without response identity")
            if self.response_sha256 != _sha256_text(self.response_text):
                raise ComparisonInvariantError("response_sha256 does not match response")
        else:
            if self.response_sha256 is not None:
                raise ComparisonInvariantError("response identity without response")
        if self.verifier_evidence is not None:
            if type(self.verifier_evidence) is not dict:
                raise ComparisonInvariantError("verifier_evidence must be a mapping or null")
            _validate_json_bounds(self.verifier_evidence, "verifier_evidence")
        if type(self.provenance) is not dict:
            raise ComparisonInvariantError("provenance must be a mapping")
        _validate_json_bounds(self.provenance, "provenance")
        root_cause_raw = self.provenance.get("root_cause_assessment")
        if root_cause_raw is not None:
            from agentic_debugger.evaluation.root_cause_metric import (
                RootCauseAssessment,
                RootCauseMetricError,
            )

            try:
                root_cause = RootCauseAssessment.from_mapping(root_cause_raw)
            except RootCauseMetricError as exc:
                raise ComparisonInvariantError(
                    f"invalid provenance.root_cause_assessment: {exc}"
                ) from exc
            if root_cause.attempt_id != self.attempt_id:
                raise ComparisonInvariantError(
                    "provenance.root_cause_assessment attempt_id mismatch"
                )
            if root_cause.task_id != self.task_id:
                raise ComparisonInvariantError(
                    "provenance.root_cause_assessment task_id mismatch"
                )
        if self.cleanup_status == "cleaned":
            if self.verifier_evidence is None or self.verifier_status != "COMPLETED":
                raise ComparisonInvariantError(
                    "cleanup_status 'cleaned' requires completed verifier evidence"
                )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "condition_id": self.condition_id,
            "task_id": self.task_id,
            "mode": self.mode,
            "role": self.role,
            "source_identity": self.source_identity,
            "generation_produced": self.generation_produced,
            "valid_patch": self.valid_patch,
            "patch_sha256": self.patch_sha256,
            "changed_file_count": self.changed_file_count,
            "correct_target_file": self.correct_target_file,
            "localization_outcome": self.localization_outcome,
            "f2p_passed": self.f2p_passed,
            "f2p_total": self.f2p_total,
            "p2p_passed": self.p2p_passed,
            "p2p_total": self.p2p_total,
            "verifier_outcome": self.verifier_outcome,
            "verifier_status": self.verifier_status,
            "failure_category": self.failure_category,
            "runtime_ms": self.runtime_ms,
            "memory_bytes": self.memory_bytes,
            "cost": self.cost,
            "tokens": self.tokens,
            "retrieval_count": self.retrieval_count,
            "retrieval_bytes": self.retrieval_bytes,
            "retrieval_latency_ms": self.retrieval_latency_ms,
            "replay_valid": self.replay_valid,
            "cleanup_status": self.cleanup_status,
            "canonical_fixture_unchanged": self.canonical_fixture_unchanged,
            "provider_attempts": self.provider_attempts,
            "network_attempts": self.network_attempts,
            "external_provider_attempts": self.external_provider_attempts,
            "external_network_attempts": self.external_network_attempts,
            "response_text": self.response_text,
            "response_sha256": self.response_sha256,
            "verifier_evidence": self.verifier_evidence,
            "provenance": dict(self.provenance),
        }

    @staticmethod
    def from_mapping(m: Any) -> "AttemptRecord":
        if not isinstance(m, Mapping):
            raise ComparisonInvariantError("attempt must be a mapping")
        _check_required_fields(m, AttemptRecord._KNOWN_FIELDS, "comparison attempt")
        _check_no_unknown_fields(m, AttemptRecord._KNOWN_FIELDS, "comparison attempt")
        return AttemptRecord(
            attempt_id=_ensure_str(m["attempt_id"], "attempt_id"),
            condition_id=_ensure_str(m["condition_id"], "condition_id"),
            task_id=_ensure_str(m["task_id"], "task_id"),
            mode=_ensure_str(m["mode"], "mode"),
            role=_ensure_str(m["role"], "role"),
            source_identity=_ensure_str(m["source_identity"], "source_identity"),
            generation_produced=_ensure_bool(m["generation_produced"], "generation_produced"),
            valid_patch=_ensure_bool(m["valid_patch"], "valid_patch"),
            patch_sha256=_ensure_optional_str(m["patch_sha256"], "patch_sha256"),
            changed_file_count=_ensure_int_or_none(m["changed_file_count"], "changed_file_count"),
            correct_target_file=(
                None
                if m["correct_target_file"] is None
                else _ensure_bool(m["correct_target_file"], "correct_target_file")
            ),
            localization_outcome=_ensure_optional_str(m["localization_outcome"], "localization_outcome"),
            f2p_passed=_ensure_int_or_none(m["f2p_passed"], "f2p_passed"),
            f2p_total=_ensure_int_or_none(m["f2p_total"], "f2p_total"),
            p2p_passed=_ensure_int_or_none(m["p2p_passed"], "p2p_passed"),
            p2p_total=_ensure_int_or_none(m["p2p_total"], "p2p_total"),
            verifier_outcome=_ensure_optional_str(m["verifier_outcome"], "verifier_outcome"),
            verifier_status=_ensure_optional_str(m["verifier_status"], "verifier_status"),
            failure_category=_ensure_optional_str(m["failure_category"], "failure_category"),
            runtime_ms=_ensure_int_or_none(m["runtime_ms"], "runtime_ms"),
            memory_bytes=_ensure_int_or_none(m["memory_bytes"], "memory_bytes"),
            cost=_ensure_finite_or_none(m["cost"], "cost"),
            tokens=_ensure_int_or_none(m["tokens"], "tokens"),
            retrieval_count=_ensure_int_or_none(m["retrieval_count"], "retrieval_count"),
            retrieval_bytes=_ensure_int_or_none(m["retrieval_bytes"], "retrieval_bytes"),
            retrieval_latency_ms=_ensure_int_or_none(m["retrieval_latency_ms"], "retrieval_latency_ms"),
            replay_valid=(
                None
                if m["replay_valid"] is None
                else _ensure_bool(m["replay_valid"], "replay_valid")
            ),
            cleanup_status=_ensure_optional_str(m["cleanup_status"], "cleanup_status"),
            canonical_fixture_unchanged=(
                None
                if m["canonical_fixture_unchanged"] is None
                else _ensure_bool(m["canonical_fixture_unchanged"], "canonical_fixture_unchanged")
            ),
            provider_attempts=_ensure_int_or_none(m["provider_attempts"], "provider_attempts"),
            network_attempts=_ensure_int_or_none(m["network_attempts"], "network_attempts"),
            external_provider_attempts=_ensure_int_or_none(
                m["external_provider_attempts"], "external_provider_attempts"
            ),
            external_network_attempts=_ensure_int_or_none(
                m["external_network_attempts"], "external_network_attempts"
            ),
            response_text=_ensure_optional_str(m["response_text"], "response_text"),
            response_sha256=_ensure_optional_str(m["response_sha256"], "response_sha256"),
            verifier_evidence=(
                None
                if m["verifier_evidence"] is None
                else dict(m["verifier_evidence"])
            ),
            provenance=dict(m["provenance"]),
        )


# ---------------------------------------------------------------------------
# Comparison experiment document
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComparisonExperiment:
    """The strict comparison experiment document."""

    schema_version: str
    experiment_id: str
    baseline_condition: str
    conditions: Tuple[str, ...]
    task_ids: Tuple[str, ...]
    attempts: Tuple[AttemptRecord, ...]

    _KNOWN_FIELDS = {
        "schema_version", "experiment_id", "baseline_condition", "conditions",
        "task_ids", "attempts", "environment", "timing", "aggregates", "delta",
        "notes",
    }

    def __post_init__(self) -> None:
        if self.schema_version != COMPARISON_SCHEMA_VERSION:
            raise ComparisonInvariantError(
                f"unsupported comparison schema version: {self.schema_version!r}"
            )
        _ensure_str(self.experiment_id, "experiment_id")
        if _EXPERIMENT_ID_PATTERN.match(self.experiment_id) is None:
            raise ComparisonInvariantError(f"invalid experiment_id: {self.experiment_id!r}")
        if type(self.conditions) is not tuple or not self.conditions:
            raise ComparisonInvariantError("conditions must be a non-empty tuple")
        if len(set(self.conditions)) != len(self.conditions):
            raise ComparisonInvariantError("condition identities must be unique")
        for condition in self.conditions:
            if _CONDITION_ID_PATTERN.match(condition) is None:
                raise ComparisonInvariantError(f"invalid condition id: {condition!r}")
        if self.baseline_condition not in self.conditions:
            raise ComparisonInvariantError(
                f"baseline condition {self.baseline_condition!r} is not declared"
            )
        if type(self.task_ids) is not tuple or not self.task_ids:
            raise ComparisonInvariantError("task_ids must be a non-empty tuple")
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ComparisonInvariantError("task_ids must be unique")
        for task_id in self.task_ids:
            if _TASK_ID_PATTERN.match(task_id) is None:
                raise ComparisonInvariantError(f"invalid task_id: {task_id!r}")
        if type(self.attempts) is not tuple:
            raise ComparisonInvariantError("attempts must be a tuple")
        seen: set[str] = set()
        primary_seen: set[Tuple[str, str]] = set()
        for attempt in self.attempts:
            if attempt.attempt_id in seen:
                raise ComparisonInvariantError(
                    f"duplicate attempt identity: {attempt.attempt_id}"
                )
            seen.add(attempt.attempt_id)
            if attempt.condition_id not in self.conditions:
                raise ComparisonInvariantError(
                    f"attempt {attempt.attempt_id!r} uses undeclared condition "
                    f"{attempt.condition_id!r}"
                )
            if attempt.task_id not in self.task_ids:
                raise ComparisonInvariantError(
                    f"attempt {attempt.task_id!r} uses undeclared task "
                    f"{attempt.task_id!r}"
                )
            if attempt.role == "evaluation":
                key = (attempt.task_id, attempt.condition_id)
                if key in primary_seen:
                    raise ComparisonInvariantError(
                        f"duplicate primary evaluation attempt for task/condition "
                        f"{key}: {attempt.attempt_id!r}"
                    )
                primary_seen.add(key)

    def to_mapping(
        self,
        *,
        environment: Optional[Dict[str, Any]] = None,
        timing: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        mapping = {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "baseline_condition": self.baseline_condition,
            "conditions": list(self.conditions),
            "task_ids": list(self.task_ids),
            "attempts": [attempt.to_mapping() for attempt in self.attempts],
        }
        if environment is not None:
            mapping["environment"] = environment
        if timing is not None:
            mapping["timing"] = timing
        return mapping

    @staticmethod
    def from_mapping(m: Any) -> "ComparisonExperiment":
        if not isinstance(m, Mapping):
            raise ComparisonInvariantError("comparison experiment must be a mapping")
        _check_required_fields(
            m,
            {"schema_version", "experiment_id", "baseline_condition", "conditions",
             "task_ids", "attempts"},
            "comparison-v1",
        )
        _check_no_unknown_fields(
            m,
            {"schema_version", "experiment_id", "baseline_condition", "conditions",
             "task_ids", "attempts", "environment", "timing", "aggregates", "delta",
             "notes"},
            "comparison-v1",
        )
        for optional_key in ("environment", "timing", "notes"):
            if m.get(optional_key) is not None:
                _validate_json_bounds(m[optional_key], optional_key)
        conditions_raw = m["conditions"]
        task_ids_raw = m["task_ids"]
        attempts_raw = m["attempts"]
        if not isinstance(conditions_raw, list) or not isinstance(task_ids_raw, list):
            raise ComparisonInvariantError("conditions/task_ids must be lists")
        if not isinstance(attempts_raw, list):
            raise ComparisonInvariantError("attempts must be a list")
        conditions = tuple(_ensure_str(item, "conditions[]") for item in conditions_raw)
        task_ids = tuple(_ensure_str(item, "task_ids[]") for item in task_ids_raw)
        attempts = tuple(AttemptRecord.from_mapping(item) for item in attempts_raw)
        return ComparisonExperiment(
            schema_version=_ensure_str(m["schema_version"], "schema_version"),
            experiment_id=_ensure_str(m["experiment_id"], "experiment_id"),
            baseline_condition=_ensure_str(m["baseline_condition"], "baseline_condition"),
            conditions=conditions,
            task_ids=task_ids,
            attempts=attempts,
        )

    @staticmethod
    def from_text(text: str) -> "ComparisonExperiment":
        if type(text) is not str or not text:
            raise ComparisonInvariantError("experiment text must be non-empty")
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ComparisonInvariantError(f"experiment is not valid JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise ComparisonInvariantError("experiment must be a JSON object")
        return ComparisonExperiment.from_mapping(parsed)

    @staticmethod
    def from_file(path: str) -> "ComparisonExperiment":
        import os

        if not isinstance(path, str) or not path:
            raise ComparisonInvariantError("path must be non-empty")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise ComparisonInvariantError(f"experiment file could not be read: {exc}") from None
        return ComparisonExperiment.from_text(text)


__all__ = [
    "COMPARISON_SCHEMA_VERSION",
    "CONDITION_IDS",
    "ATTEMPT_ROLES",
    "FAILURE_CATEGORIES",
    "MAX_RESPONSE_TEXT_BYTES",
    "ComparisonError",
    "ComparisonInputError",
    "ComparisonInvariantError",
    "canonical_json",
    "bound_response_text",
    "AttemptRecord",
    "ComparisonExperiment",
]
