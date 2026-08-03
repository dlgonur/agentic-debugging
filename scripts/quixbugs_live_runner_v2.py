"""Fail-closed live runner for the frozen QuixBugs paired-pilot v2 campaign.

This module implements the campaign-level live-runner infrastructure for
``research/quixbugs/PAIRED_PILOT_V2.json`` (canonical manifest hash
``bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171``,
accepted lineage baseline ``28ec7754336fc53f21ebbae8a851b33e26714932``, live
protocol ``1.3``).  It reuses the accepted paired-pilot validator path
(:mod:`quixbugs_paired_pilot`) for manifest validation, authorization
semantics, preflight-failure derivation, and strict in-order case-result
validation; it does not create a parallel evaluation framework.

The runner is fail-closed by construction:

* no provider process, transport request, or model call can be created before
  (a) a strict versioned authorization artifact validates, (b) the actual
  repository state is verified against the authorization-bound execution
  commit (HEAD equality, commit existence, descent from the accepted
  baseline, tracked working tree and Git index cleanliness), (c) an explicit
  provider transport and case runner are configured, (d) the pre-provider
  route gate establishes the exact subscription-route evidence from a strict
  raw route-evidence contract, (e) the output/attempt root is claimed
  exclusively for this campaign-attempt identity, and (f) the durable
  attempt ledger accepts the attempt;
* route evidence is strict: missing, unobservable, stale, future-dated,
  contradictory, substituted, or unsupported evidence blocks before any
  provider process can be created; missing fields are never defaulted from
  the manifest or the authorization;
* the six frozen cases run strictly in order, never in parallel, with one
  fresh session/workspace boundary per case, and every produced case record
  must pass the frozen ``quixbugs-paired-pilot-result-v2`` validator before
  it is written;
* the campaign stops (or aborts) according to the frozen v2 stop contract and
  never silently skips a case; a campaign stop blocks the remaining unstarted
  cases as ``campaign-stop`` records with typed trigger or authority evidence;
  the execution commit and tracked-clean repository state are re-verified
  before every case, and a post-preflight drift stops the campaign with typed
  authority evidence;
* a durable attempt ledger plus an atomic output-root ownership claim forbid
  silent restart, rerun, evidence replacement, resume, and same-authorization
  re-execution against a changed manifest, baseline, route, or case order;
  one output/attempt root belongs to exactly one campaign-attempt identity
  and authoritative artifacts use create-once semantics;
* the terminal ledger state is finalized before the terminal ``campaign.json``
  is written; ``campaign.json`` is written last, and a ledger-finalization
  failure never leaves a valid-looking completed campaign artifact.

Nothing in this module contacts a live provider, model catalog, entitlement
service, or paid endpoint.  Route evidence is supplied by an injected
provider, case execution by an injected runner, transports by an injected
factory, and repository state by an injectable Git-state provider; the CLI
never defaults into live execution and has no hidden provider selection or
fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import quixbugs_paired_pilot as pilot  # noqa: E402  (accepted validator path)

RUNNER_SCHEMA_VERSION = "quixbugs-live-runner-v2"
AUTHORIZATION_SCHEMA_VERSION = "quixbugs-paired-pilot-authorization-v1"
ACCEPTED_BASELINE = "28ec7754336fc53f21ebbae8a851b33e26714932"
LIVE_PROTOCOL_VERSION = "1.3"
CAMPAIGN_ID_V2 = pilot.CAMPAIGN_ID_V2
MANIFEST_PATH_V2 = pilot.MANIFEST_PATH_V2
AUTHORIZED_BILLING_ROUTE = pilot.AUTHORIZED_BILLING_ROUTE
PLANNING_BASELINE_COMMIT_V2 = pilot.PLANNING_BASELINE_COMMIT_V2
SOURCE_INTEGRITY_RELATIVE_PATH = pilot.SOURCE_INTEGRITY_RELATIVE_PATH
SOURCE_INTEGRITY_SHA256 = pilot.SOURCE_INTEGRITY_SHA256
MAX_ROUTE_EVIDENCE_AGE_SECONDS = 600
CLOCK_SKEW_ALLOWANCE_SECONDS = 120
MAX_PRIVATE_EVIDENCE_BYTES = 1_000_000
ATTEMPT_IDENTITY_PATTERN = re.compile(r"^quixbugs-paired-pilot-v2-attempt-[0-9a-f]{12,64}$")
ISO_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")

AUTHORIZATION_FIELDS = frozenset({
    "schema_version",
    "template",
    "authorize_live",
    "campaign_id",
    "campaign_version",
    "campaign_manifest_hash",
    "accepted_baseline",
    "planning_baseline_commit",
    "qualification_contract_hash",
    "accepted_campaign_commit",
    "permitted_case_ids",
    "provider",
    "model",
    "variant",
    "protocol",
    "expected_opencode_version",
    "expected_catalog_fingerprint",
    "expected_runtime_model_id",
    "subscription_route_required",
    "expected_billing_route",
    "subscription_entitlement_confirmed",
    "subscription_account_observation",
    "expected_account_status",
    "billing_route_classification",
    "deny_zen_route",
    "deny_free_tier_substitution",
    "deny_ollama_route",
    "deny_alternate_provider",
    "deny_model_substitution",
    "deny_metered_fallback",
    "deny_paid_overage",
    "deny_per_call_billing_fallback",
    "no_fallback_required",
    "operator_authorization_id",
    "authorization_created_at",
    "authorization_valid_until",
    "output_root",
    "campaign_attempt_identity",
    "single_frozen_six_case_campaign_confirmation",
})

DENIAL_FIELDS = (
    "deny_zen_route",
    "deny_free_tier_substitution",
    "deny_ollama_route",
    "deny_alternate_provider",
    "deny_model_substitution",
    "deny_metered_fallback",
    "deny_paid_overage",
    "deny_per_call_billing_fallback",
)

ACCOUNT_OBSERVATION_FIELDS = frozenset({"entitlement_confirmed", "evidence_reference"})

STRING_FIELDS = {
    "schema_version", "campaign_id", "campaign_manifest_hash", "accepted_baseline",
    "planning_baseline_commit", "qualification_contract_hash", "accepted_campaign_commit",
    "provider", "model", "variant", "protocol", "expected_opencode_version",
    "expected_catalog_fingerprint", "expected_runtime_model_id", "expected_billing_route",
    "billing_route_classification", "operator_authorization_id", "authorization_created_at",
    "authorization_valid_until", "output_root", "campaign_attempt_identity",
    "expected_account_status",
}
BOOL_FIELDS = {
    "template", "authorize_live", "subscription_route_required",
    "subscription_entitlement_confirmed", "no_fallback_required",
    "single_frozen_six_case_campaign_confirmation", *DENIAL_FIELDS,
}
INT_FIELDS = {"campaign_version"}

#: Strict raw route-evidence contract.  Every acceptance-critical field must be
#: explicitly observed with the stated type before normalization; missing
#: fields are never defaulted from the manifest or the authorization.
RAW_ROUTE_EVIDENCE_SCHEMA: dict[str, str] = {
    "provider": "nonempty-string",
    "model": "nonempty-string",
    "variant": "nonempty-string",
    "protocol": "nonempty-string",
    "opencode_version": "nonempty-string",
    "catalog_fingerprint": "hex64",
    "runtime_model_id": "nonempty-string",
    "billing_route": "billing-route-enum",
    "subscription_entitlement_confirmed": "bool",
    "account_status": "nonempty-string",
    "active_model_status": "active-status-enum",
    "variant_available": "bool",
    "zen_used": "bool",
    "free_tier_used": "bool",
    "ollama_used": "bool",
    "paid_fallback_used": "bool",
    "alternate_provider_used": "bool",
    "metered_fallback_used": "bool",
    "paid_overage_used": "bool",
    "per_call_billing_used": "bool",
    "model_substitution_observed": "bool",
    "input_price": "number",
    "output_price": "number",
    "provider_reported_cost": "number",
    "observed_at": "timestamp",
}
#: Optional versioned metadata explicitly allowed on raw route evidence.
RAW_ROUTE_EVIDENCE_OPTIONAL = {
    "schema_version": "quixbugs-route-evidence-v1",
}
ACTIVE_STATUS_VALUES = ("ACTIVE", "INACTIVE", "NOT_RUN")

CASE_LIFECYCLE_STATES = (
    "authorization-rejected", "pre-provider-rejected", "provider-attempted",
    "logical-model-call", "transport-retry", "directive-accepted",
    "completed", "blocked", "aborted", "authority-invalidated", "unstarted",
)

CAMPAIGN_TERMINAL_STATUSES = ("COMPLETED", "PARTIAL", "ABORTED", "BLOCKED", "REJECTED")

TERMINAL_COMMIT_VERSION = "quixbugs-terminal-commit-v1"
TERMINALIZATION_STEPS = (
    "after_prepare", "after_campaign_payload", "before_ledger_terminalization",
    "after_ledger_terminalization", "before_terminal_commit", "during_terminal_commit",
)

CAMPAIGN_REJECTION_CODES = {
    "MISSING_AUTHORIZATION", "AUTHORIZATION_FLAG_INVALID", "SCHEMA_VERSION_MISMATCH",
    "TEMPLATE_IS_NOT_AUTHORIZATION", "UNKNOWN_FIELDS", "MISSING_FIELDS", "WRONG_TYPE",
    "CAMPAIGN_IDENTITY_MISMATCH", "MANIFEST_MISMATCH", "BASELINE_MISMATCH",
    "PLANNING_BASELINE_MISMATCH", "QUALIFICATION_CONTRACT_MISMATCH", "COMMIT_INVALID",
    "CASE_SET_MISMATCH", "DUPLICATE_CASE_ID", "ROUTE_MISMATCH",
    "VERSION_BINDING_MISSING", "CATALOG_BINDING_MISSING",
    "RUNTIME_MODEL_ID_BINDING_MISSING", "SUBSCRIPTION_ROUTE_REQUIRED",
    "BILLING_ROUTE_MISMATCH", "ENTITLEMENT_EVIDENCE_MISSING",
    "ACCOUNT_OBSERVATION_MISSING", "ACCOUNT_OBSERVATION_INVALID",
    "ACCOUNT_STATUS_BINDING_MISSING",
    "DENIAL_FLAG_NOT_TRUE", "FALLBACK_POLICY_MISMATCH",
    "OPERATOR_IDENTITY_MISSING", "CREATED_AT_INVALID", "CREATED_AT_FUTURE",
    "AUTHORIZATION_EXPIRED", "VALIDITY_NOT_AFTER_CREATION",
    "OUTPUT_ROOT_MISMATCH", "ATTEMPT_IDENTITY_INVALID",
    "CAMPAIGN_CONFIRMATION_MISSING", "ZERO_PRICING_RULE_CONTRADICTION",
    "DUPLICATE_ATTEMPT", "ATTEMPT_ALREADY_STARTED", "LEDGER_CONTRADICTION",
    "EXECUTION_COMMIT_MISMATCH", "EXECUTION_COMMIT_NOT_FOUND",
    "EXECUTION_COMMIT_ANCESTRY_FAILED", "TRACKED_STATE_DIRTY",
    "REPOSITORY_STATE_UNVERIFIABLE",
    "OUTPUT_ROOT_OWNED", "OUTPUT_ROOT_OCCUPIED", "SAME_ATTEMPT_DUPLICATE",
    "ROUTE_EVIDENCE_NOT_CONFIGURED",
    "ROUTE_EVIDENCE_INVALID", "TRANSPORT_NOT_CONFIGURED", "CASE_RUNNER_NOT_CONFIGURED",
    "UNEXPECTED_CASE_FAILURE", "PUBLIC_PRIVATE_BOUNDARY_VIOLATION",
    "SCHEMA_INCONSISTENCY", "BUDGET_EXCEEDED", "OUTPUT_INTEGRITY_FAILURE",
    "STATIC_POLICY_PDB_VIOLATION", "LEDGER_FINALIZATION_FAILED",
    "NON_FINITE_VALUE",
}

OUTCOME_FIELDS = frozenset({
    "terminal_status", "terminal_reason_code", "termination_reason",
    "logical_model_calls", "provider_process_attempts", "retries",
    "valid_directives", "malformed_directive_rejections", "bounded_directive_feedback_events",
    "baseline_reproduction", "controller_states_visited", "hypotheses_created",
    "pdb_gate_decisions", "pdb_counts", "pdb_sessions_started",
    "successful_pdb_observations", "failed_pdb_observations",
    "verifier_runs", "patch_submissions", "independent_verifier_result",
    "transport_evidence", "terminal_transport_evidence",
    "blocked_evidence", "infrastructure_evidence",
    "preflight_failure_evidence", "campaign_stop_evidence",
    "prompt_tokens", "completion_tokens", "reasoning_tokens", "provider_reported_cost",
    "wall_clock_duration_seconds", "public_evidence_bytes",
    "canonical_source_restoration", "owned_workspace_cleanup", "evidence_consistency",
    "public_request_hash", "source_hash", "candidate_hash", "repair_outcome",
    "resource_ids",
})

ZERO_PDB_COUNTS = {
    "total_gate_decisions": 0, "allowed_gate_openings": 0, "rejected_gate_decisions": 0,
    "sessions_started": 0, "successful_observations": 0, "failed_observations": 0,
}

COMPLETED_TERMINAL_STATUSES = {
    "RESOLVED", "UNRESOLVED", "PDB_NOT_REACHED", "INVALID_MODEL_RESPONSE",
    "PROVIDER_ERROR", "INFRASTRUCTURE_ERROR",
}


class LiveRunnerError(ValueError):
    """A fail-closed live-runner error; the campaign never contacts a provider."""


class RepositoryStateError(LiveRunnerError):
    """The actual repository state does not satisfy the execution-commit contract."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


class RouteEvidenceUnavailable(LiveRunnerError):
    """The route-evidence provider could not establish required evidence."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


class RouteEvidenceInvalid(LiveRunnerError):
    """Raw route evidence violates the strict acceptance-critical contract."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class RouteDriftError(LiveRunnerError):
    """Route/model identity drifted from the preflight binding after preflight."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


class BudgetViolationError(LiveRunnerError):
    """A case outcome violates a frozen v2 manifest budget."""

    def __init__(self, field: str, limit: int, observed: int) -> None:
        super().__init__(f"budget exceeded: {field} = {observed} > {limit}")
        self.field = field
        self.limit = limit
        self.observed = observed


class StaticPolicyPdbViolation(LiveRunnerError):
    """A static-baseline case opened or observed PDB, which the frozen policy forbids."""


class SanitizationViolationError(LiveRunnerError):
    """Public output would expose private material."""


class OutputIntegrityError(LiveRunnerError):
    """A bounded, create-once artifact write failed or collided."""


class NonFiniteValueError(LiveRunnerError):
    """A numeric evidence value is NaN or infinite and must fail closed."""


class AttemptClaimError(LiveRunnerError):
    """A typed attempt-claim rejection; the claim never mutates the ledger."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class SameAttemptClaimError(AttemptClaimError):
    """A second claim attempt with the same attempt identity and authorization
    hash; the single-winner owner gate never lets a second process pass."""

    def __init__(self, detail: str) -> None:
        super().__init__("SAME_ATTEMPT_DUPLICATE", detail)


class OutputRootOwnedError(AttemptClaimError):
    """The output/attempt root is owned by a different campaign-attempt identity
    (or its owner record is corrupt/contradictory)."""

    def __init__(self, detail: str) -> None:
        super().__init__("OUTPUT_ROOT_OWNED", detail)


class OutputRootOccupiedError(AttemptClaimError):
    """The output/attempt root is not structurally empty; it must be absent or
    empty before a claim."""

    def __init__(self, detail: str) -> None:
        super().__init__("OUTPUT_ROOT_OCCUPIED", detail)


def _assert_finite_json(value: Any, path: str = "root") -> None:
    """Recursively reject NaN, +Infinity, and -Infinity numeric values.

    Booleans are not numbers.  Valid finite zero values are preserved when
    explicitly observed; absence is never replaced by zero.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NonFiniteValueError(f"non-finite numeric value at {path}")
        return
    if isinstance(value, int):
        return
    if isinstance(value, str):
        return
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_finite_json(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_finite_json(item, f"{path}.{key}")
        return
    raise NonFiniteValueError(f"unsupported JSON value type at {path}: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def authorization_hash(authorization: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(authorization))


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not ISO_TIMESTAMP_PATTERN.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now(clock: Callable[[], float] | None = None) -> datetime:
    if clock is None:
        return datetime.now(timezone.utc)
    value = clock()
    if not math.isfinite(value):
        raise LiveRunnerError("non-finite campaign clock value")
    return datetime.fromtimestamp(value, tz=timezone.utc)


# ---- repository state / execution-commit binding -----------------------------


@dataclass(frozen=True)
class GitRepositoryState:
    """Independently observed repository state for the execution-commit contract."""

    head: str
    execution_commit_exists: bool
    execution_commit_descends_from_baseline: bool
    tracked_working_tree_clean: bool
    git_index_clean: bool
    untracked_non_ignored: tuple[str, ...] = ()


def _git(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
    )


def real_git_state(expected_commit: str) -> GitRepositoryState:
    """Observe the real repository state without touching the index or worktree.

    ``expected_commit`` is the authorization-bound execution commit whose
    existence and ancestry are checked.  Untracked files are allowed only when
    they are ignored (operator/output artifacts); any non-ignored untracked
    file or any tracked change makes the state dirty.
    """
    head_result = _git(["git", "rev-parse", "HEAD"])
    head = (head_result.stdout or "").strip()
    if head_result.returncode != 0 or not (len(head) == 40 and all(char in "0123456789abcdef" for char in head)):
        raise RepositoryStateError("REPOSITORY_STATE_UNVERIFIABLE", "Git HEAD could not be resolved")
    exists = _git(["git", "cat-file", "-e", f"{expected_commit}^{{commit}}"]).returncode == 0
    descends = False
    if exists:
        descends = _git(["git", "merge-base", "--is-ancestor", ACCEPTED_BASELINE, expected_commit]).returncode == 0
    status_result = _git(["git", "status", "--porcelain"])
    lines = status_result.stdout.splitlines() if status_result.returncode == 0 else []
    index_clean = True
    tracked_clean = True
    untracked: list[str] = []
    for line in lines:
        if not line or len(line) < 3:
            continue
        x, y = line[0], line[1]
        path = line[3:]
        if x == "?" and y == "?":
            untracked.append(path)
            continue
        tracked_clean = False
        if x != " ":
            index_clean = False
    non_ignored = [path for path in untracked if _git(["git", "check-ignore", "-q", "--", path]).returncode != 0]
    return GitRepositoryState(
        head=head,
        execution_commit_exists=exists,
        execution_commit_descends_from_baseline=descends,
        tracked_working_tree_clean=tracked_clean,
        git_index_clean=index_clean,
        untracked_non_ignored=tuple(non_ignored),
    )


def verify_execution_repository_state(
    authorization: Mapping[str, Any],
    git_state_provider: Callable[[str], GitRepositoryState] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify the actual repository state against the authorization-bound commit.

    The authorization's ``accepted_campaign_commit`` is treated as the exact
    commit whose code will execute the future campaign.  Before ledger claim,
    preflight, transport creation, or provider contact the runner requires:
    actual HEAD equals the bound commit; the commit exists in the repository;
    the commit descends from the accepted baseline; and the tracked working
    tree plus the real Git index are clean (only ignored operator/output
    artifacts allowed).  Returns the independently observed and verified
    execution-commit evidence; raises :class:`RepositoryStateError` otherwise.
    """
    expected = authorization["accepted_campaign_commit"]
    provider = git_state_provider if git_state_provider is not None else real_git_state
    try:
        state = provider(expected)
    except LiveRunnerError:
        raise
    except Exception as exc:
        raise RepositoryStateError("REPOSITORY_STATE_UNVERIFIABLE", f"repository state could not be observed: {type(exc).__name__}: {exc}") from exc
    if not isinstance(state, GitRepositoryState):
        raise RepositoryStateError("REPOSITORY_STATE_UNVERIFIABLE", "git state provider returned a non-GitRepositoryState value")
    if state.head != expected:
        raise RepositoryStateError("EXECUTION_COMMIT_MISMATCH", f"actual Git HEAD {state.head} != authorization-bound execution commit {expected}")
    if not state.execution_commit_exists:
        raise RepositoryStateError("EXECUTION_COMMIT_NOT_FOUND", f"authorization-bound execution commit {expected} does not exist in this repository")
    if not state.execution_commit_descends_from_baseline:
        raise RepositoryStateError("EXECUTION_COMMIT_ANCESTRY_FAILED", f"authorization-bound execution commit {expected} does not descend from accepted baseline {ACCEPTED_BASELINE}")
    if not state.tracked_working_tree_clean or not state.git_index_clean or state.untracked_non_ignored:
        raise RepositoryStateError("TRACKED_STATE_DIRTY", f"tracked working tree or Git index is not clean; untracked non-ignored: {list(state.untracked_non_ignored)}")
    return {
        "authorization_bound_execution_commit": expected,
        "independently_observed_head": state.head,
        "verified": True,
        "commit_exists_in_repository": True,
        "descends_from_accepted_baseline": True,
        "accepted_baseline": ACCEPTED_BASELINE,
        "tracked_working_tree_clean": True,
        "git_index_clean": True,
        "untracked_non_ignored": [],
        "checked_at": (now if now is not None else _utc_now()).isoformat().replace("+00:00", "Z"),
    }


def _tracked_state_fingerprint(state: GitRepositoryState, now: datetime) -> str:
    """Runner-level tracked-source state fingerprint for drift authority evidence."""
    return sha256_text(canonical_json({
        "git_head": state.head,
        "tracked_working_tree_clean": state.tracked_working_tree_clean,
        "git_index_clean": state.git_index_clean,
        "untracked_non_ignored": sorted(state.untracked_non_ignored),
        "source_integrity_authority_sha256": SOURCE_INTEGRITY_SHA256,
        "observed_at": now.isoformat().replace("+00:00", "Z"),
    }))


# ---- authorization artifact ---------------------------------------------------


def authorization_failure(
    authorization: Mapping[str, Any] | None,
    manifest: Mapping[str, Any],
    *,
    expected_output_root: str | Path | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return the strict authorization failure category, or None when valid.

    The artifact must bind the exact v2 campaign identity and manifest hash,
    the accepted repository baseline, the exact six frozen case IDs in order,
    protocol 1.3, the OpenCode Go subscription route, DeepSeek V4 Flash, the
    expected runtime model/catalog identity fields, exact OpenCode version and
    catalog fingerprint, a strict subscription account/entitlement observation,
    billing-route classification, explicit denial of every prohibited
    fallback/substitution route, operator identity, bounded validity, output
    root, attempt identity, and explicit confirmation that this is one frozen
    six-case campaign only.  Unknown fields, missing fields, wrong types,
    duplicate or reordered cases, wrong hashes, wrong baseline, stale or
    contradictory route data, materially future creation timestamps, and
    invalid validity ordering are rejected.  v1-style zero-price fields are a
    contradiction and fail closed.  ``accepted_campaign_commit`` is the exact
    commit whose code will execute the campaign; its existence, equality with
    the actual HEAD, baseline descent, and repository cleanliness are verified
    separately by :func:`verify_execution_repository_state` before any
    provider contact.
    """
    if authorization is None:
        return "MISSING_AUTHORIZATION"
    if not isinstance(authorization, Mapping):
        return "AUTHORIZATION_FLAG_INVALID"
    try:
        _assert_finite_json(authorization)
    except NonFiniteValueError:
        return "NON_FINITE_VALUE"
    if authorization.get("template") is True:
        return "TEMPLATE_IS_NOT_AUTHORIZATION"
    if authorization.get("schema_version") != AUTHORIZATION_SCHEMA_VERSION:
        return "SCHEMA_VERSION_MISMATCH"
    if "zero_price_required" in authorization:
        return "ZERO_PRICING_RULE_CONTRADICTION"
    if "catalog_binding_procedure" in authorization:
        return "CATALOG_BINDING_MISSING"
    unknown = set(authorization) - AUTHORIZATION_FIELDS
    if unknown:
        return "UNKNOWN_FIELDS"
    missing = AUTHORIZATION_FIELDS - set(authorization)
    if missing:
        return "MISSING_FIELDS"
    for field in STRING_FIELDS:
        if field == "authorization_valid_until":
            continue
        if type(authorization[field]) is not str:
            return "WRONG_TYPE"
    for field in BOOL_FIELDS:
        if type(authorization[field]) is not bool:
            return "WRONG_TYPE"
    for field in INT_FIELDS:
        if type(authorization[field]) is not int:
            return "WRONG_TYPE"
    permitted = authorization["permitted_case_ids"]
    if not isinstance(permitted, list) or any(type(item) is not str for item in permitted):
        return "WRONG_TYPE"
    account = authorization["subscription_account_observation"]
    if not isinstance(account, Mapping):
        return "WRONG_TYPE"
    if set(account) != ACCOUNT_OBSERVATION_FIELDS:
        return "ACCOUNT_OBSERVATION_INVALID"
    if type(account["entitlement_confirmed"]) is not bool or not account["entitlement_confirmed"]:
        return "ACCOUNT_OBSERVATION_INVALID"
    evidence_reference = account["evidence_reference"]
    if type(evidence_reference) is not str or not evidence_reference.strip():
        return "ACCOUNT_OBSERVATION_INVALID"
    if authorization["authorization_valid_until"] is not None and type(authorization["authorization_valid_until"]) is not str:
        return "WRONG_TYPE"
    if authorization["authorize_live"] is not True:
        return "AUTHORIZATION_FLAG_INVALID"
    if authorization["campaign_id"] != CAMPAIGN_ID_V2 or authorization["campaign_version"] != 2:
        return "CAMPAIGN_IDENTITY_MISMATCH"
    expected_manifest_hash = pilot.manifest_hash(manifest)
    if authorization["campaign_manifest_hash"] != expected_manifest_hash:
        return "MANIFEST_MISMATCH"
    if authorization["accepted_baseline"] != ACCEPTED_BASELINE:
        return "BASELINE_MISMATCH"
    if authorization["planning_baseline_commit"] != manifest.get("planning_baseline_commit"):
        return "PLANNING_BASELINE_MISMATCH"
    if authorization["qualification_contract_hash"] != manifest.get("qualification_contract_hash"):
        return "QUALIFICATION_CONTRACT_MISMATCH"
    accepted = authorization["accepted_campaign_commit"]
    if not (len(accepted) == 40 and all(char in "0123456789abcdef" for char in accepted) and accepted != PLANNING_BASELINE_COMMIT_V2):
        return "COMMIT_INVALID"
    frozen_ids = [case["case_id"] for case in manifest["case_order"]]
    if permitted != frozen_ids:
        if len(permitted) == len(set(permitted)) and set(permitted) == set(frozen_ids):
            return "CASE_SET_MISMATCH"
        if len(permitted) != len(set(permitted)):
            return "DUPLICATE_CASE_ID"
        return "CASE_SET_MISMATCH"
    for key in ("provider", "model", "variant", "protocol"):
        if authorization[key] != manifest["route"].get(key):
            return "ROUTE_MISMATCH"
    if not authorization["expected_opencode_version"]:
        return "VERSION_BINDING_MISSING"
    catalog = authorization["expected_catalog_fingerprint"]
    if not (len(catalog) == 64 and all(char in "0123456789abcdef" for char in catalog)):
        return "CATALOG_BINDING_MISSING"
    if not authorization["expected_runtime_model_id"]:
        return "RUNTIME_MODEL_ID_BINDING_MISSING"
    if authorization["subscription_route_required"] is not True:
        return "SUBSCRIPTION_ROUTE_REQUIRED"
    if authorization["expected_billing_route"] != AUTHORIZED_BILLING_ROUTE:
        return "BILLING_ROUTE_MISMATCH"
    if authorization["subscription_entitlement_confirmed"] is not True:
        return "ENTITLEMENT_EVIDENCE_MISSING"
    if not authorization["expected_account_status"]:
        return "ACCOUNT_STATUS_BINDING_MISSING"
    if authorization["billing_route_classification"] != AUTHORIZED_BILLING_ROUTE:
        return "BILLING_ROUTE_MISMATCH"
    for field in DENIAL_FIELDS:
        if authorization[field] is not True:
            return "DENIAL_FLAG_NOT_TRUE"
    if authorization["no_fallback_required"] is not True:
        return "FALLBACK_POLICY_MISMATCH"
    if not authorization["operator_authorization_id"]:
        return "OPERATOR_IDENTITY_MISSING"
    reference_time = now if now is not None else _utc_now()
    created = _parse_iso(authorization["authorization_created_at"])
    if created is None:
        return "CREATED_AT_INVALID"
    if created > reference_time + _seconds_timedelta(CLOCK_SKEW_ALLOWANCE_SECONDS):
        return "CREATED_AT_FUTURE"
    valid_until_value = authorization["authorization_valid_until"]
    if valid_until_value is not None:
        valid_until = _parse_iso(valid_until_value)
        if valid_until is None:
            return "CREATED_AT_INVALID"
        if valid_until <= created:
            return "VALIDITY_NOT_AFTER_CREATION"
        if reference_time >= valid_until:
            return "AUTHORIZATION_EXPIRED"
    output_root = authorization["output_root"]
    if expected_output_root is not None:
        expected = str(Path(expected_output_root).resolve())
        if Path(output_root).resolve() != Path(expected).resolve():
            return "OUTPUT_ROOT_MISMATCH"
    if not ATTEMPT_IDENTITY_PATTERN.fullmatch(authorization["campaign_attempt_identity"]):
        return "ATTEMPT_IDENTITY_INVALID"
    if authorization["single_frozen_six_case_campaign_confirmation"] is not True:
        return "CAMPAIGN_CONFIRMATION_MISSING"
    legacy = pilot._authorization_failure_category(manifest, authorization)
    if legacy is not None:
        return legacy
    return None


def _seconds_timedelta(seconds: float) -> Any:
    from datetime import timedelta

    return timedelta(seconds=seconds)


def validate_authorization_artifact(
    authorization: Mapping[str, Any] | None,
    manifest: Mapping[str, Any],
    *,
    expected_output_root: str | Path | None = None,
    now: datetime | None = None,
) -> Mapping[str, Any]:
    failure = authorization_failure(authorization, manifest, expected_output_root=expected_output_root, now=now)
    if failure is not None:
        raise LiveRunnerError(f"live authorization is invalid: {failure}")
    assert isinstance(authorization, Mapping)
    return authorization


def load_authorization_artifact(path: str | Path) -> Mapping[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveRunnerError(f"invalid live authorization artifact: {exc}") from exc
    if not isinstance(value, Mapping):
        raise LiveRunnerError("live authorization artifact must be a JSON object")
    return value


def authorization_template() -> dict[str, Any]:
    """The non-authorizing v2 authorization template.

    The template is structurally complete but carries ``template: true``,
    ``authorize_live: false``, placeholder identities, and an explicit
    non-authorizing note.  The strict validator rejects it; it can never be
    mistaken for an active authorization.
    """
    return {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "template": True,
        "authorize_live": False,
        "campaign_id": CAMPAIGN_ID_V2,
        "campaign_version": 2,
        "campaign_manifest_hash": "bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171",
        "accepted_baseline": ACCEPTED_BASELINE,
        "planning_baseline_commit": PLANNING_BASELINE_COMMIT_V2,
        "qualification_contract_hash": "7246d289fcc689e93d93385751cbae5fa75a3c52e3c04e001f2c977a1990c52d",
        "accepted_campaign_commit": "<40-hex commit whose code will execute the campaign; must equal the actual Git HEAD at execution time>",
        "permitted_case_ids": [f"{CAMPAIGN_ID_V2}:{task_id}:{policy}" for task_id, policy in pilot.V1_FROZEN_CASE_ORDER],
        "provider": "OpenCode Go",
        "model": "deepseek-v4-flash",
        "variant": "max",
        "protocol": LIVE_PROTOCOL_VERSION,
        "expected_opencode_version": "<exact OpenCode runtime version, resolved by the operator before contact>",
        "expected_catalog_fingerprint": "<64-hex catalog fingerprint observed by the operator>",
        "expected_runtime_model_id": "<exact catalog-qualified runtime model identity>",
        "subscription_route_required": True,
        "expected_billing_route": "SUBSCRIPTION",
        "subscription_entitlement_confirmed": True,
        "subscription_account_observation": {
            "entitlement_confirmed": True,
            "evidence_reference": "<operator account/entitlement observation reference>",
        },
        "expected_account_status": "<required account/route status>",
        "billing_route_classification": "SUBSCRIPTION",
        "deny_zen_route": True,
        "deny_free_tier_substitution": True,
        "deny_ollama_route": True,
        "deny_alternate_provider": True,
        "deny_model_substitution": True,
        "deny_metered_fallback": True,
        "deny_paid_overage": True,
        "deny_per_call_billing_fallback": True,
        "no_fallback_required": True,
        "operator_authorization_id": "<operator authorization record ID>",
        "authorization_created_at": "<ISO-8601 UTC timestamp>",
        "authorization_valid_until": "<ISO-8601 UTC expiry later than creation and later than execution time, or null>",
        "output_root": "<explicit output/attempt directory for this campaign; one root belongs to exactly one attempt identity>",
        "campaign_attempt_identity": "quixbugs-paired-pilot-v2-attempt-<64-hex>",
        "single_frozen_six_case_campaign_confirmation": True,
        "_template_note": (
            "NON-AUTHORIZING TEMPLATE. This document is a schema reference only. "
            "It grants nothing. A real operator authorization must be created outside "
            "tracked source (ignored operator-artifact location), must replace every "
            "placeholder with a genuinely observed value, must set authorize_live=true "
            "and template=false, and remains rejected by this runner unless it passes "
            "the strict authorization validation. accepted_campaign_commit must be the "
            "exact commit whose code will execute the campaign; the runner verifies it "
            "against the actual Git HEAD, requires it to exist and descend from the "
            "accepted baseline, and requires a clean tracked working tree and index "
            "before any provider contact. No value in this template was observed from "
            "any live catalog, provider, account, or entitlement service."
        ),
    }


def write_authorization_template(path: str | Path) -> Path:
    target = Path(path)
    if target.exists():
        raise LiveRunnerError(f"authorization template target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(authorization_template(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


# ---- strict raw route evidence and pre-provider route gate ---------------------


def _validate_raw_route_evidence(
    raw: Mapping[str, Any],
    authorization: Mapping[str, Any],
    *,
    now: datetime,
    staleness_window_seconds: int,
    clock_skew_seconds: int,
) -> dict[str, Any]:
    """Validate the strict raw route-evidence contract.

    Every acceptance-critical field must be explicitly present with the exact
    type.  Missing fields are rejected, never defaulted from the manifest or
    the authorization; missing denial/fallback observations are never
    converted to ``False``; missing price/cost evidence is never converted to
    zero.  ``account_status`` must equal the authorization's expected account
    status, and ``observed_at`` must parse, must not be materially in the
    future, and must be within the accepted freshness window.  Unknown fields
    are rejected unless they are the versioned optional metadata
    ``schema_version``.
    """
    if not isinstance(raw, Mapping):
        raise RouteEvidenceInvalid("NOT_AN_OBJECT", "route evidence provider returned a non-object")
    unknown = set(raw) - set(RAW_ROUTE_EVIDENCE_SCHEMA) - set(RAW_ROUTE_EVIDENCE_OPTIONAL)
    if unknown:
        raise RouteEvidenceInvalid("UNKNOWN_FIELD", f"route evidence carries unsupported fields: {sorted(unknown)}")
    for field, expected_version in RAW_ROUTE_EVIDENCE_OPTIONAL.items():
        if field in raw and raw[field] != expected_version:
            raise RouteEvidenceInvalid("UNKNOWN_FIELD", f"route evidence optional metadata {field} has an unsupported value")
    missing = [field for field in RAW_ROUTE_EVIDENCE_SCHEMA if field not in raw]
    if missing:
        raise RouteEvidenceInvalid("MISSING_FIELD", f"route evidence is missing acceptance-critical fields: {sorted(missing)}")
    validated: dict[str, Any] = {}
    for field, kind in RAW_ROUTE_EVIDENCE_SCHEMA.items():
        value = raw[field]
        if kind == "nonempty-string":
            if type(value) is not str or not value.strip():
                raise RouteEvidenceInvalid("WRONG_TYPE", f"route evidence field {field} must be a non-empty string")
        elif kind == "hex64":
            if not (type(value) is str and len(value) == 64 and all(char in "0123456789abcdef" for char in value)):
                raise RouteEvidenceInvalid("WRONG_TYPE", f"route evidence field {field} must be a 64-hex string")
        elif kind == "bool":
            if type(value) is not bool:
                raise RouteEvidenceInvalid("WRONG_TYPE", f"route evidence field {field} must be boolean")
        elif kind == "number":
            if type(value) not in (int, float) or isinstance(value, bool):
                raise RouteEvidenceInvalid("WRONG_TYPE", f"route evidence field {field} must be a non-negative number")
            if isinstance(value, float) and not math.isfinite(value):
                raise RouteEvidenceInvalid("NON_FINITE_VALUE", f"route evidence field {field} must be finite")
            if value < 0:
                raise RouteEvidenceInvalid("WRONG_TYPE", f"route evidence field {field} must be a non-negative number")
        elif kind == "billing-route-enum":
            if value not in pilot.BILLING_ROUTE_VALUES:
                raise RouteEvidenceInvalid("WRONG_TYPE", f"route evidence field {field} is not a frozen billing route")
        elif kind == "active-status-enum":
            if value not in ACTIVE_STATUS_VALUES:
                raise RouteEvidenceInvalid("WRONG_TYPE", f"route evidence field {field} is not an active-model status")
        elif kind == "timestamp":
            parsed = _parse_iso(value)
            if parsed is None:
                raise RouteEvidenceInvalid("INVALID_TIMESTAMP", "route evidence observed_at must be a parseable ISO-8601 UTC timestamp")
            age_seconds = (now - parsed).total_seconds()
            if age_seconds < -clock_skew_seconds:
                raise RouteEvidenceInvalid("FUTURE_TIMESTAMP", "route evidence observed_at is materially in the future")
            if age_seconds > staleness_window_seconds:
                raise RouteEvidenceInvalid("STALE_TIMESTAMP", "route evidence observed_at is outside the accepted freshness window")
        validated[field] = value
    if validated["account_status"] != authorization["expected_account_status"]:
        raise RouteEvidenceInvalid(
            "ACCOUNT_STATUS_MISMATCH",
            f"observed account status {validated['account_status']!r} != authorization expected account status {authorization['expected_account_status']!r}",
        )
    return validated


def _build_route_observation(validated: Mapping[str, Any], execution_commit: str | None) -> dict[str, Any]:
    """Shape strict raw evidence into the frozen v2 route observation.

    No defaulting occurs: every field comes from the validated raw evidence.
    ``account_status`` and ``observed_at`` are preserved in the observation,
    and the route observation is bound to the independently verified execution
    commit.
    """
    observation: dict[str, Any] = {
        "provider": validated["provider"],
        "model": validated["model"],
        "variant": validated["variant"],
        "protocol": validated["protocol"],
        "opencode_version": validated["opencode_version"],
        "catalog_fingerprint": validated["catalog_fingerprint"],
        "runtime_model_id": validated["runtime_model_id"],
        "billing_route": validated["billing_route"],
        "subscription_entitlement_confirmed": validated["subscription_entitlement_confirmed"],
        "account_status": validated["account_status"],
        "active_model_status": validated["active_model_status"],
        "variant_available": validated["variant_available"],
        "input_price": validated["input_price"],
        "output_price": validated["output_price"],
        "provider_reported_cost": validated["provider_reported_cost"],
        "paid_fallback_used": validated["paid_fallback_used"],
        "alternate_provider_used": validated["alternate_provider_used"],
        "ollama_used": validated["ollama_used"],
        "zen_used": validated["zen_used"],
        "free_tier_used": validated["free_tier_used"],
        "metered_fallback_used": validated["metered_fallback_used"],
        "paid_overage_used": validated["paid_overage_used"],
        "per_call_billing_used": validated["per_call_billing_used"],
        "model_substitution_observed": validated["model_substitution_observed"],
        "observed_at": validated["observed_at"],
        "preflight_success": False,
    }
    if execution_commit is not None:
        observation["execution_commit"] = execution_commit
    return observation


def _derived_failure_category(
    observation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    authorization: Mapping[str, Any],
) -> str | None:
    evidence: dict[str, Any] = {field: None for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS}
    evidence["model_substitution_observed"] = observation.get("model_substitution_observed") is True
    evidence["catalog_failure_category"] = observation.get("catalog_failure_category")
    evidence["catalog_failure_error"] = observation.get("catalog_failure_error")
    result = {"route_observation": observation}
    return pilot._derive_preflight_failure_category(evidence, result, manifest, authorization)


def _gate_failure_observation(
    manifest: Mapping[str, Any],
    authorization: Mapping[str, Any],
    *,
    now: datetime,
    execution_commit: str | None,
) -> dict[str, Any]:
    """Base gate-constructed failure observation for provider-unavailable evidence.

    The route-evidence provider declared the evidence unobtainable; the gate
    constructs the failed observation explicitly (never by defaulting raw
    evidence).  Zero prices and ``False`` flags here are contract-required
    values of the frozen failure predicates, not fabricated observations.
    """
    route = manifest["route"]
    observation: dict[str, Any] = {
        "provider": route["provider"],
        "model": route["model"],
        "variant": route["variant"],
        "protocol": route["protocol"],
        "opencode_version": authorization["expected_opencode_version"],
        "catalog_fingerprint": None,
        "runtime_model_id": None,
        "billing_route": "UNKNOWN",
        "subscription_entitlement_confirmed": False,
        "account_status": None,
        "active_model_status": "NOT_RUN",
        "variant_available": False,
        "input_price": 0,
        "output_price": 0,
        "provider_reported_cost": 0,
        "paid_fallback_used": False,
        "alternate_provider_used": False,
        "ollama_used": False,
        "zen_used": False,
        "free_tier_used": False,
        "metered_fallback_used": False,
        "paid_overage_used": False,
        "per_call_billing_used": False,
        "model_substitution_observed": False,
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "preflight_success": False,
    }
    if execution_commit is not None:
        observation["execution_commit"] = execution_commit
    return observation


def _catalog_unavailable_observation(
    manifest: Mapping[str, Any],
    authorization: Mapping[str, Any],
    *,
    now: datetime,
    execution_commit: str | None,
    detail: str,
) -> dict[str, Any]:
    observation = _gate_failure_observation(manifest, authorization, now=now, execution_commit=execution_commit)
    observation.update({
        "billing_route": AUTHORIZED_BILLING_ROUTE,
        "subscription_entitlement_confirmed": True,
        "runtime_model_id": authorization["expected_runtime_model_id"],
        "opencode_version": authorization["expected_opencode_version"],
        "catalog_failure_category": "route_evidence_unavailable",
        "catalog_failure_error": detail,
    })
    return observation


def _entitlement_unavailable_observation(
    manifest: Mapping[str, Any],
    authorization: Mapping[str, Any],
    *,
    now: datetime,
    execution_commit: str | None,
) -> dict[str, Any]:
    return _gate_failure_observation(manifest, authorization, now=now, execution_commit=execution_commit)


def _build_preflight_failure_evidence(
    manifest: Mapping[str, Any],
    authorization: Mapping[str, Any],
    observation: Mapping[str, Any],
    category: str,
    *,
    evidence_reference: str,
) -> dict[str, Any]:
    expected = manifest["route"]
    observed = observation
    fills: dict[str, Any] = {
        "failure_category": category,
        "evidence_reference": evidence_reference,
        "expected_provider": expected["provider"], "observed_provider": observed.get("provider"),
        "expected_model": expected["model"], "observed_model": observed.get("model"),
        "expected_variant": expected["variant"], "observed_variant": observed.get("variant"),
        "expected_protocol": expected["protocol"], "observed_protocol": observed.get("protocol"),
        "observed_billing_route": observed.get("billing_route"),
        "subscription_entitlement_confirmed": observed.get("subscription_entitlement_confirmed"),
        "expected_runtime_model_id": authorization["expected_runtime_model_id"],
        "observed_runtime_model_id": observed.get("runtime_model_id"),
        "observed_active_model_status": observed.get("active_model_status"),
        "observed_variant_available": observed.get("variant_available"),
        "observed_input_price": observed.get("input_price"),
        "observed_output_price": observed.get("output_price"),
        "expected_opencode_version": authorization["expected_opencode_version"],
        "observed_opencode_version": observed.get("opencode_version"),
        "expected_manifest_hash": pilot.manifest_hash(manifest),
        "observed_manifest_hash": observed.get("observed_manifest_hash"),
        "expected_qualification_contract_hash": manifest["qualification_contract_hash"],
        "observed_qualification_contract_hash": observed.get("observed_qualification_contract_hash"),
        "expected_source_authority_hash": pilot.SOURCE_INTEGRITY_SHA256,
        "observed_source_authority_hash": observed.get("observed_source_authority_hash"),
        "expected_campaign_commit": authorization["accepted_campaign_commit"],
        "observed_campaign_commit": observed.get("observed_campaign_commit"),
        "authorization_artifact_hash": authorization_hash(authorization),
        "authorization_validation_error": None,
        "catalog_failure_category": observed.get("catalog_failure_category"),
        "catalog_failure_error": observed.get("catalog_failure_error"),
        "paid_fallback_required": observed.get("paid_fallback_required") if observed.get("paid_fallback_required") is not None else (observed.get("paid_fallback_used") is True),
        "alternate_provider_required": observed.get("alternate_provider_required") if observed.get("alternate_provider_required") is not None else (observed.get("alternate_provider_used") is True),
        "model_substitution_observed": observed.get("model_substitution_observed"),
        "zen_route_observed": observed.get("zen_route_observed") if observed.get("zen_route_observed") is not None else (observed.get("zen_used") is True),
        "free_tier_route_observed": observed.get("free_tier_route_observed") if observed.get("free_tier_route_observed") is not None else (observed.get("free_tier_used") is True),
        "ollama_route_observed": observed.get("ollama_route_observed") if observed.get("ollama_route_observed") is not None else (observed.get("ollama_used") is True),
        "metered_fallback_required": observed.get("metered_fallback_required") if observed.get("metered_fallback_required") is not None else (observed.get("metered_fallback_used") is True),
        "paid_overage_required": observed.get("paid_overage_required") if observed.get("paid_overage_required") is not None else (observed.get("paid_overage_used") is True),
        "per_call_billing_fallback_required": observed.get("per_call_billing_fallback_required") if observed.get("per_call_billing_fallback_required") is not None else (observed.get("per_call_billing_used") is True),
    }
    relevant = pilot.PRE_PROVIDER_REASON_FIELDS_V2.get(category) if category in pilot.PRE_PROVIDER_REASON_FIELDS_V2 else pilot.PRE_PROVIDER_REASON_FIELDS.get(category)
    if relevant is None:
        raise LiveRunnerError(f"preflight category {category} is not a frozen pre-provider reason")
    return {
        field: (fills.get(field) if field in relevant or field == "failure_category" else None)
        for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS
    }


class PreflightVerdict:
    """Result of the mandatory pre-provider route gate."""

    __slots__ = ("passed", "route_observation", "failure_category", "preflight_failure_evidence", "evidence_reference", "stale", "detail")

    def __init__(
        self,
        *,
        passed: bool,
        route_observation: Mapping[str, Any],
        failure_category: str | None,
        preflight_failure_evidence: Mapping[str, Any] | None,
        evidence_reference: str,
        stale: bool = False,
        detail: str | None = None,
    ) -> None:
        self.passed = passed
        self.route_observation = dict(route_observation)
        self.failure_category = failure_category
        self.preflight_failure_evidence = dict(preflight_failure_evidence) if preflight_failure_evidence is not None else None
        self.evidence_reference = evidence_reference
        self.stale = stale
        self.detail = detail

    def to_mapping(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "route_observation": self.route_observation,
            "failure_category": self.failure_category,
            "preflight_failure_evidence": self.preflight_failure_evidence,
            "evidence_reference": self.evidence_reference,
            "stale": self.stale,
            "detail": self.detail,
        }


def run_route_preflight(
    manifest: Mapping[str, Any],
    authorization: Mapping[str, Any],
    route_evidence_provider: Callable[[], Mapping[str, Any]] | None,
    *,
    now: datetime | None = None,
    staleness_window_seconds: int = MAX_ROUTE_EVIDENCE_AGE_SECONDS,
    clock_skew_seconds: int = CLOCK_SKEW_ALLOWANCE_SECONDS,
    attempt_identity: str = "preflight",
    execution_commit: str | None = None,
) -> PreflightVerdict:
    """Complete the mandatory pre-provider route gate.

    The gate validates the authorization, obtains fresh route evidence from
    the injected provider (which may be the real OpenCode transport preflight
    in a future execution task, or a deterministic test double), enforces the
    strict raw route-evidence contract (explicit typed observations for every
    acceptance-critical field, timestamp freshness, exact account-status
    match; no defaulting), normalizes it into the frozen v2 route-observation
    shape, binds it to the independently verified execution commit, and
    derives the controlling preflight failure.  Missing, unobservable, stale,
    future-dated, contradictory, substituted, or unsupported evidence blocks
    before any provider process can be created.  No provider request is ever
    issued here.
    """
    validate_authorization_artifact(authorization, manifest, now=now)
    reference_time = now if now is not None else _utc_now()
    if route_evidence_provider is None:
        raise LiveRunnerError("live execution requires an explicitly configured route-evidence provider; none is configured")
    try:
        raw = route_evidence_provider()
    except RouteEvidenceUnavailable as exc:
        if exc.category not in (pilot.ALL_PRE_PROVIDER_REASON_CODES):
            raise LiveRunnerError(f"route-evidence provider raised an unsupported category: {exc.category}") from exc
        if exc.category == "CATALOG_PREFLIGHT_FAILED":
            observation = _catalog_unavailable_observation(
                manifest, authorization, now=reference_time, execution_commit=execution_commit, detail=exc.detail,
            )
        elif exc.category == "SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED":
            observation = _entitlement_unavailable_observation(
                manifest, authorization, now=reference_time, execution_commit=execution_commit,
            )
        else:
            raise LiveRunnerError(f"route-evidence provider failure category is not gate-constructible: {exc.category}") from exc
        category = _derived_failure_category(observation, manifest, authorization)
        if category is None:
            category = exc.category
        reference = f"{attempt_identity}:preflight:{category}"
        evidence = _build_preflight_failure_evidence(manifest, authorization, observation, category, evidence_reference=reference)
        return PreflightVerdict(
            passed=False, route_observation=observation, failure_category=category,
            preflight_failure_evidence=evidence, evidence_reference=reference, detail=exc.detail,
        )
    except (OSError, TypeError, ValueError) as exc:
        observation = _entitlement_unavailable_observation(
            manifest, authorization, now=reference_time, execution_commit=execution_commit,
        )
        category = "SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED"
        reference = f"{attempt_identity}:preflight:{category}"
        evidence = _build_preflight_failure_evidence(manifest, authorization, observation, category, evidence_reference=reference)
        return PreflightVerdict(
            passed=False, route_observation=observation, failure_category=category,
            preflight_failure_evidence=evidence, evidence_reference=reference,
            detail=f"route evidence could not be obtained: {type(exc).__name__}: {exc}",
        )

    validated = _validate_raw_route_evidence(
        raw, authorization,
        now=reference_time,
        staleness_window_seconds=staleness_window_seconds,
        clock_skew_seconds=clock_skew_seconds,
    )
    observation = _build_route_observation(validated, execution_commit)

    try:
        pilot.validate_route_observation(observation, manifest["route"], allow_observed_mismatch=True)
    except pilot.PilotError as exc:
        observation = _entitlement_unavailable_observation(
            manifest, authorization, now=reference_time, execution_commit=execution_commit,
        )
        category = "SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED"
        reference = f"{attempt_identity}:preflight:{category}"
        evidence = _build_preflight_failure_evidence(manifest, authorization, observation, category, evidence_reference=reference)
        return PreflightVerdict(
            passed=False, route_observation=observation, failure_category=category,
            preflight_failure_evidence=evidence, evidence_reference=reference,
            detail=f"route evidence is contradictory or unsupported: {exc}",
        )

    expected_catalog = authorization["expected_catalog_fingerprint"]
    observed_catalog = observation.get("catalog_fingerprint")
    if isinstance(observed_catalog, str) and observed_catalog != expected_catalog:
        observation = _gate_failure_observation(manifest, authorization, now=reference_time, execution_commit=execution_commit)
        observation.update({
            "catalog_failure_category": "catalog_fingerprint_mismatch",
            "catalog_failure_error": "observed catalog fingerprint differs from the authorization-bound fingerprint",
        })
        category = "CATALOG_PREFLIGHT_FAILED"
        reference = f"{attempt_identity}:preflight:{category}"
        evidence = _build_preflight_failure_evidence(manifest, authorization, observation, category, evidence_reference=reference)
        return PreflightVerdict(
            passed=False, route_observation=observation, failure_category=category,
            preflight_failure_evidence=evidence, evidence_reference=reference,
            detail="observed catalog fingerprint differs from the authorization binding",
        )

    category = _derived_failure_category(observation, manifest, authorization)
    if category is not None:
        if category in {"MODEL_INACTIVE", "VARIANT_UNAVAILABLE"}:
            observation = dict(observation)
            observation["input_price"] = 0
            observation["output_price"] = 0
            category = _derived_failure_category(observation, manifest, authorization)
        reference = f"{attempt_identity}:preflight:{category}"
        evidence = _build_preflight_failure_evidence(manifest, authorization, observation, category, evidence_reference=reference)
        return PreflightVerdict(
            passed=False, route_observation=observation, failure_category=category,
            preflight_failure_evidence=evidence, evidence_reference=reference,
        )

    observation = dict(observation)
    observation["preflight_success"] = True
    pilot.validate_route_observation(observation, manifest["route"])
    return PreflightVerdict(
        passed=True, route_observation=observation, failure_category=None,
        preflight_failure_evidence=None, evidence_reference=f"{attempt_identity}:preflight:passed",
    )


# ---- attempt ledger and output-root ownership ----------------------------------


class AttemptLedger:
    """Durable fail-closed campaign-attempt ledger.

    The ledger lives inside the output/attempt root and records one entry per
    attempt key.  A claim is rejected when the same attempt identity, the same
    authorization, or the same authorization against a changed contract
    already exists, or when a previous attempt is still STARTED (a crashed
    attempt may not be resumed or silently restarted).  Writes are atomic via
    a temporary file and rename; cross-process exclusivity for the same
    authorization/output root is provided by the atomic output-root ownership
    claim (:func:`claim_output_root`).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise LiveRunnerError(f"attempt ledger is unreadable or corrupted: {self.path}") from None
        if not isinstance(value, dict):
            raise LiveRunnerError("attempt ledger root must be an object")
        return value

    def _atomic_write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
        except (TypeError, ValueError) as exc:
            raise LiveRunnerError(f"attempt ledger failed strict JSON serialization: {exc}") from exc
        temporary = self.path.with_suffix(self.path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise LiveRunnerError(f"attempt ledger write failed: {exc}") from exc

    def entry(self) -> dict[str, Any]:
        return self._load()

    def claim(self, entry: Mapping[str, Any]) -> None:
        """Atomically claim the attempt; raises LiveRunnerError when forbidden."""
        identity = entry["attempt_identity"]
        authorization_hash = entry["authorization_hash"]
        manifest_hash = entry["campaign_manifest_hash"]
        ledger = self._load()
        for existing in ledger.values():
            if existing.get("authorization_hash") == authorization_hash:
                if existing.get("status") == "STARTED":
                    raise LiveRunnerError("a previous attempt with this authorization is still STARTED; resume or silent rerun is forbidden")
                raise LiveRunnerError("this authorization has already been used for a campaign attempt; rerun is forbidden")
        for existing in ledger.values():
            if existing.get("attempt_identity") == identity:
                raise LiveRunnerError(f"duplicate attempt is rejected: {identity}")
            if existing.get("campaign_manifest_hash") == manifest_hash and existing.get("accepted_baseline") != entry.get("accepted_baseline"):
                raise LiveRunnerError("ledger contradiction: same manifest with a different baseline")
        attempt_key = sha256_text(canonical_json({
            "campaign_manifest_hash": manifest_hash,
            "authorization_hash": authorization_hash,
            "attempt_identity": identity,
        }))
        ledger[attempt_key] = dict(entry)
        self._atomic_write(ledger)

    def finish(self, entry: Mapping[str, Any]) -> None:
        ledger = self._load()
        attempt_key = sha256_text(canonical_json({
            "campaign_manifest_hash": entry["campaign_manifest_hash"],
            "authorization_hash": entry["authorization_hash"],
            "attempt_identity": entry["attempt_identity"],
        }))
        if attempt_key not in ledger:
            raise LiveRunnerError("attempt ledger entry is missing; refusing to fabricate a terminal state")
        ledger[attempt_key] = dict(entry)
        self._atomic_write(ledger)


def _entry_kind(path: Path) -> str:
    try:
        if path.is_symlink():
            return "symlink"
    except OSError:
        return "unknown"
    if path.is_dir():
        return "directory"
    return "file"


def _read_owner_record(owner_path: Path) -> Mapping[str, Any] | None:
    """Read the owner record with a bounded retry.

    The owner record is written by the single winner immediately after the
    exclusive create; a concurrent loser may transiently observe a partially
    written file, so the read is retried briefly before failing closed.
    """
    import time as _time

    last_error: Exception | None = None
    for _ in range(20):
        try:
            value = json.loads(owner_path.read_text(encoding="utf-8"))
            if isinstance(value, Mapping):
                return value
            last_error = ValueError("owner record root is not an object")
        except (OSError, json.JSONDecodeError) as exc:
            last_error = exc
        _time.sleep(0.01)
    raise OutputRootOwnedError(f"OUTPUT_ROOT_OWNED: the output/attempt root owner record is corrupt or unreadable: {last_error}")


def claim_output_root(
    output_root: str | Path,
    *,
    attempt_identity: str,
    authorization_hash: str,
    campaign_manifest_hash: str,
    now: datetime | None = None,
) -> None:
    """Atomically claim one output/attempt root for exactly one attempt identity.

    Single-winner semantics: the exclusive ``.attempt-owner`` creation
    (``O_CREAT|O_EXCL``) is the only gate through which an attempt can pass.
    An existing owner record never lets a second process return successfully —
    even when the identity and authorization hash match — and the initial
    durable ``STARTED`` ledger entry is only reached by the single winner.
    Typed errors distinguish a same-identity duplicate
    (:class:`SameAttemptClaimError`) from a different-owner conflict
    (:class:`OutputRootOwnedError`); both stop before any ledger mutation.

    The authoritative attempt root must be absent or structurally empty
    before the owner record is created; pre-existing ``campaign.json``,
    ``ledger.json``, case files, private evidence, temporary files, unknown
    files, directories, symlinks, or contradictory owner data are rejected
    (:class:`OutputRootOccupiedError` / :class:`OutputRootOwnedError`) before
    any case can execute.  A crashed or abandoned claim is never silently
    reclaimed: the owner record persists and blocks every later claim.
    """
    root = Path(output_root)
    owner_path = root / ".attempt-owner"
    claimed_at = (now if now is not None else _utc_now()).isoformat().replace("+00:00", "Z")

    def _owner_rejection(existing: Mapping[str, Any] | None, detail: str) -> None:
        if existing is not None and isinstance(existing, Mapping):
            if existing.get("attempt_identity") == attempt_identity and existing.get("authorization_hash") == authorization_hash:
                raise SameAttemptClaimError(detail)
        raise OutputRootOwnedError(detail)

    if root.exists():
        if owner_path.exists():
            existing = _read_owner_record(owner_path)
            _owner_rejection(existing, "OUTPUT_ROOT_OWNED: the output/attempt root is owned by this or another campaign-attempt identity; one output root belongs to exactly one attempt identity")
        try:
            entries = sorted(root.iterdir())
        except OSError as exc:
            raise OutputRootOccupiedError(f"OUTPUT_ROOT_OCCUPIED: the output/attempt root could not be inspected: {exc}") from exc
        if entries:
            kinds = ", ".join(f"{entry.name}={_entry_kind(entry)}" for entry in entries)
            raise OutputRootOccupiedError(
                f"OUTPUT_ROOT_OCCUPIED: the output/attempt root is not structurally empty: {kinds}"
            )
    else:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OutputRootOccupiedError(f"OUTPUT_ROOT_OCCUPIED: the output/attempt root could not be created: {exc}") from exc

    payload = json.dumps({
        "attempt_identity": attempt_identity,
        "authorization_hash": authorization_hash,
        "campaign_manifest_hash": campaign_manifest_hash,
        "claimed_at": claimed_at,
        "pid": os.getpid(),
    }, indent=2, sort_keys=True, allow_nan=False) + "\n"
    try:
        fd = os.open(owner_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = _read_owner_record(owner_path)
        _owner_rejection(existing, "OUTPUT_ROOT_OWNED: a concurrent process claimed this output/attempt root; exactly one attempt claim may win")
    except OSError as exc:
        raise OutputRootOccupiedError(f"OUTPUT_ROOT_OCCUPIED: the owner record could not be created: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    except OSError as exc:
        try:
            owner_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise OutputRootOccupiedError(f"OUTPUT_ROOT_OCCUPIED: the owner record could not be written: {exc}") from exc
    # Close the race window: anything other than the owner record appearing in
    # the root between the inspection and the owner creation invalidates the
    # claim; the freshly created owner record is removed again so the root is
    # not left looking usable.
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        raise OutputRootOccupiedError(f"OUTPUT_ROOT_OCCUPIED: the output/attempt root could not be re-inspected: {exc}") from exc
    unexpected = [entry for entry in entries if entry.name != ".attempt-owner"]
    if unexpected:
        try:
            owner_path.unlink(missing_ok=True)
        except OSError:
            pass
        kinds = ", ".join(f"{entry.name}={_entry_kind(entry)}" for entry in unexpected)
        raise OutputRootOccupiedError(f"OUTPUT_ROOT_OCCUPIED: the output/attempt root became occupied during the claim: {kinds}")


def atomic_create_json(
    path: str | Path,
    value: Mapping[str, Any],
    *,
    fault_after_temp: Callable[[], None] | None = None,
) -> None:
    """Create an authoritative artifact exactly once.

    Uses a temporary file plus an atomic no-overwrite link (or an exclusive
    create as fallback).  An existing authoritative file is never replaced.
    Serialization is strict (``allow_nan=False``): a non-finite or otherwise
    unserializable value fails closed and leaves no partial authoritative
    file.  ``fault_after_temp`` is a deterministic fault-injection hook called
    after the temporary file is durable but before the atomic link, so a
    simulated process death leaves only a non-authoritative temporary file.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise OutputIntegrityError(f"authoritative artifact failed strict JSON serialization: {target}: {exc}") from exc
    temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        temporary.write_text(payload, encoding="utf-8")
    except OSError as exc:
        raise OutputIntegrityError(f"authoritative artifact temporary write failed: {target}: {exc}") from exc
    if fault_after_temp is not None:
        fault_after_temp()
    try:
        os.link(temporary, target)
        temporary.unlink()
        return
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        raise OutputIntegrityError(f"authoritative artifact already exists and must not be replaced: {target}") from None
    except OSError:
        temporary.unlink(missing_ok=True)
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise OutputIntegrityError(f"authoritative artifact already exists and must not be replaced: {target}") from None
        except OSError as exc:
            raise OutputIntegrityError(f"authoritative artifact could not be created: {target}: {exc}") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
        except OSError as exc:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise OutputIntegrityError(f"authoritative artifact could not be written: {target}: {exc}") from exc


def _rejection_dir(output_root: Path) -> Path:
    """Parent-level non-authoritative rejection location.

    Rejection evidence is stored OUTSIDE the authoritative attempt root so it
    can never make the root appear usable or claimable.
    """
    return output_root.parent / f"rejections-{output_root.name}"


def _rejection_path(output_root: Path, identity: str | None) -> Path:
    safe = identity if identity is not None else "unknown-attempt"
    return _rejection_dir(output_root) / f"rejection-{safe}.json"


def _write_rejection_record(output_root: Path, record: Mapping[str, Any]) -> None:
    """Write a non-authoritative rejection record at the parent level that can
    never replace accepted attempt evidence or occupy the attempt root."""
    try:
        atomic_create_json(_rejection_path(output_root, record.get("campaign_attempt_identity")), record)
    except OutputIntegrityError:
        return


# ---- deterministic identity and resource helpers -----------------------------


def attempt_identity_for(authorization: Mapping[str, Any]) -> str:
    return str(authorization["campaign_attempt_identity"])


def deterministic_run_id(attempt_identity: str, case: Mapping[str, Any]) -> str:
    return f"{attempt_identity}:run:{case['case_id']}"


def deterministic_session_id(attempt_identity: str, case: Mapping[str, Any]) -> str:
    return f"{attempt_identity}:session:{case['case_id']}"


def deterministic_workspace_id(attempt_identity: str, case: Mapping[str, Any]) -> str:
    return f"{attempt_identity}:workspace:{case['case_id']}"


def deterministic_resource_ids(attempt_identity: str, case: Mapping[str, Any]) -> dict[str, str]:
    run_id = deterministic_run_id(attempt_identity, case)
    return {
        "case_execution_context": run_id,
        "model_adapter": f"{run_id}:adapter",
        "provider_process": f"{run_id}:provider-process",
        "owned_workspace": deterministic_workspace_id(attempt_identity, case),
        "controller_state": f"{run_id}:controller",
        "session_state": deterministic_session_id(attempt_identity, case),
        "directive_feedback_buffer": f"{run_id}:feedback",
        "task_memory": f"{run_id}:task-memory",
    }


# ---- record builders ----------------------------------------------------------


def _default_preflight_failure_evidence() -> dict[str, Any]:
    return {field: None for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS}


def _default_campaign_stop_evidence() -> dict[str, Any]:
    evidence = {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS}
    evidence["confirmed"] = False
    return evidence


def _default_infrastructure_evidence(reference: str) -> dict[str, Any]:
    return {
        "stage": "none", "reason_code": "NONE", "confirmed_failure": False,
        "classification": "NONE", "terminal_classification": "NOT_APPLICABLE",
        "provider_attempt_index": None, "prior_lifecycle_completed": False,
        "source_mutation_observed": False, "expected_source_hash": None,
        "evidence_reference": reference,
    }


def build_preprovider_block_record(
    manifest: Mapping[str, Any],
    case: Mapping[str, Any],
    authorization: Mapping[str, Any],
    verdict: PreflightVerdict,
    *,
    attempt_identity: str,
    execution_commit: str,
) -> dict[str, Any]:
    record = pilot.public_case_record(manifest, case)
    observation = verdict.route_observation
    reference = verdict.evidence_reference
    record.update({
        "execution_kind": "LIVE_CASE",
        "campaign_commit": execution_commit,
        "accepted_code_commit": execution_commit,
        "execution_commit": execution_commit,
        "provider": observation.get("provider"),
        "model": observation.get("model"),
        "variant": observation.get("variant"),
        "route_observation": dict(observation),
        "terminal_status": "BLOCKED",
        "terminal_reason_code": verdict.failure_category,
        "termination_reason": f"pre-provider rejection: {verdict.failure_category}",
        "blocked_evidence": {
            "block_kind": "live-pre-provider", "reason_code": verdict.failure_category,
            "confirmed": True, "evidence_reference": reference,
        },
        "terminal_transport_evidence": {
            "final_attempt_classification": "PRE_PROVIDER_BLOCK", "process_exit_code": None,
            "timed_out": False, "provider_error_category": None,
            "provider_completed_response": False, "evidence_reference": reference,
        },
        "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False},
        "preflight_failure_evidence": dict(verdict.preflight_failure_evidence) if verdict.preflight_failure_evidence is not None else _default_preflight_failure_evidence(),
        "campaign_stop_evidence": _default_campaign_stop_evidence(),
        "infrastructure_evidence": _default_infrastructure_evidence(reference),
        "resource_ids": deterministic_resource_ids(attempt_identity, case),
    })
    pilot.validate_case_result(record, manifest, authorization)
    return record


def build_campaign_stop_evidence(
    manifest: Mapping[str, Any],
    reason: str,
    *,
    trigger_case_id: str | None,
    trigger_result_sha256: str | None,
    authority_identity: str | None,
    authority_record_sha256: str | None,
    evidence_reference: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS}
    evidence.update({
        "reason_code": reason,
        "trigger_case_id": trigger_case_id,
        "pre_case_authority_check_identity": authority_identity,
        "evidence_reference": evidence_reference,
        "confirmed": True,
        "trigger_result_sha256": trigger_result_sha256,
        "authority_check_record_sha256": authority_record_sha256,
    })
    evidence.update(extra or {})
    return evidence


def build_campaign_stop_record(
    manifest: Mapping[str, Any],
    case: Mapping[str, Any],
    authorization: Mapping[str, Any],
    stop_evidence: Mapping[str, Any],
    *,
    attempt_identity: str,
    execution_commit: str,
) -> dict[str, Any]:
    record = pilot.public_case_record(manifest, case)
    reference = str(stop_evidence["evidence_reference"])
    record.update({
        "execution_kind": "LIVE_CASE",
        "campaign_commit": execution_commit,
        "accepted_code_commit": execution_commit,
        "execution_commit": execution_commit,
        "terminal_status": "BLOCKED",
        "terminal_reason_code": stop_evidence["reason_code"],
        "termination_reason": f"campaign stopped: {stop_evidence['reason_code']}",
        "blocked_evidence": {
            "block_kind": "campaign-stop", "reason_code": stop_evidence["reason_code"],
            "confirmed": True, "evidence_reference": reference,
        },
        "terminal_transport_evidence": {
            "final_attempt_classification": "CAMPAIGN_STOP", "process_exit_code": None,
            "timed_out": False, "provider_error_category": None,
            "provider_completed_response": False, "evidence_reference": reference,
        },
        "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False},
        "campaign_stop_evidence": dict(stop_evidence),
        "preflight_failure_evidence": _default_preflight_failure_evidence(),
        "infrastructure_evidence": _default_infrastructure_evidence(reference),
        "resource_ids": deterministic_resource_ids(attempt_identity, case),
    })
    # Campaign-stop records are validated by the campaign validator, which owns
    # the stored trigger/authority ledgers; they cannot self-validate here.
    return record


def materialize_case_record(
    manifest: Mapping[str, Any],
    case: Mapping[str, Any],
    authorization: Mapping[str, Any],
    route_observation: Mapping[str, Any],
    outcome: Mapping[str, Any],
    *,
    attempt_identity: str,
    execution_commit: str,
) -> dict[str, Any]:
    record = pilot.public_case_record(manifest, case)
    record.update({
        "execution_kind": "LIVE_CASE",
        "campaign_commit": execution_commit,
        "accepted_code_commit": execution_commit,
        "execution_commit": execution_commit,
        "provider": route_observation.get("provider"),
        "model": route_observation.get("model"),
        "variant": route_observation.get("variant"),
        "route_observation": dict(route_observation),
        "resource_ids": deterministic_resource_ids(attempt_identity, case),
    })
    for field in OUTCOME_FIELDS:
        if field in outcome:
            record[field] = outcome[field]
    return record


def validate_case_outcome(outcome: Mapping[str, Any]) -> None:
    missing = OUTCOME_FIELDS - set(outcome)
    if missing:
        raise LiveRunnerError(f"case outcome is missing fields: {sorted(missing)}")
    for field in OUTCOME_FIELDS:
        if field not in outcome:
            raise LiveRunnerError(f"case outcome is missing field: {field}")
    try:
        _assert_finite_json(outcome)
    except NonFiniteValueError as exc:
        raise LiveRunnerError(f"case outcome carries non-finite numeric evidence: {exc}") from exc


def enforce_case_budgets(outcome: Mapping[str, Any], manifest: Mapping[str, Any], *, case_policy: str) -> None:
    """Enforce the frozen v2 manifest budgets on a produced case outcome."""
    budgets = manifest["budgets"]
    pairs = (
        ("logical_model_calls", "max_logical_model_calls"),
        ("provider_process_attempts", "max_total_provider_process_attempts"),
        ("retries", "max_total_transport_retries"),
        ("valid_directives", "max_accepted_directives"),
        ("malformed_directive_rejections", "max_malformed_directive_feedback_cycles"),
        ("bounded_directive_feedback_events", "max_malformed_directive_feedback_cycles"),
        ("hypotheses_created", "max_hypotheses"),
        ("patch_submissions", "max_patch_submissions"),
        ("verifier_runs", "max_verifier_runs"),
        ("public_evidence_bytes", "max_public_evidence_bytes"),
    )
    for actual_field, limit_field in pairs:
        value = outcome[actual_field]
        if type(value) is not int or value < 0:
            raise BudgetViolationError(actual_field, budgets[limit_field], -1)
        if value > budgets[limit_field]:
            raise BudgetViolationError(actual_field, budgets[limit_field], value)
    if outcome["provider_process_attempts"] > outcome["logical_model_calls"] * budgets["max_transport_attempts_per_logical_call"]:
        raise BudgetViolationError("provider_process_attempts", outcome["logical_model_calls"] * budgets["max_transport_attempts_per_logical_call"], outcome["provider_process_attempts"])
    if outcome["retries"] > outcome["logical_model_calls"] * budgets["max_transport_retries_per_logical_call"]:
        raise BudgetViolationError("retries", outcome["logical_model_calls"] * budgets["max_transport_retries_per_logical_call"], outcome["retries"])
    if outcome["retries"] > outcome["provider_process_attempts"]:
        raise BudgetViolationError("retries", outcome["provider_process_attempts"], outcome["retries"])
    if outcome["valid_directives"] > outcome["logical_model_calls"]:
        raise BudgetViolationError("valid_directives", outcome["logical_model_calls"], outcome["valid_directives"])
    if outcome["bounded_directive_feedback_events"] < outcome["malformed_directive_rejections"]:
        raise BudgetViolationError("bounded_directive_feedback_events", outcome["malformed_directive_rejections"], outcome["bounded_directive_feedback_events"])
    duration = outcome["wall_clock_duration_seconds"]
    if type(duration) not in (int, float) or isinstance(duration, bool) or duration < 0 or duration > budgets["total_case_timeout_seconds"]:
        raise BudgetViolationError("wall_clock_duration_seconds", budgets["total_case_timeout_seconds"], int(duration))
    pdb = outcome["pdb_counts"]
    if not isinstance(pdb, Mapping):
        raise LiveRunnerError("case outcome PDB accounting is malformed")
    pdb_keys = ("total_gate_decisions", "allowed_gate_openings", "rejected_gate_decisions", "sessions_started", "successful_observations", "failed_observations")
    if not all(type(pdb.get(key)) is int for key in pdb_keys):
        raise LiveRunnerError("case outcome PDB accounting is malformed")
    pdb_gates = outcome["pdb_gate_decisions"]
    if not isinstance(pdb_gates, list) or len(pdb_gates) != pdb["total_gate_decisions"]:
        raise LiveRunnerError("case outcome PDB gate decision list is malformed")
    if pdb["allowed_gate_openings"] > budgets["max_pdb_gate_openings"]:
        raise BudgetViolationError("pdb_gate_openings", budgets["max_pdb_gate_openings"], pdb["allowed_gate_openings"])
    if pdb["sessions_started"] > pdb["allowed_gate_openings"]:
        raise BudgetViolationError("pdb_sessions_started", pdb["allowed_gate_openings"], pdb["sessions_started"])
    if pdb["successful_observations"] + pdb["failed_observations"] > budgets["max_pdb_observations"]:
        raise BudgetViolationError("pdb_observations", budgets["max_pdb_observations"], pdb["successful_observations"] + pdb["failed_observations"])
    pdb_activity = pdb["allowed_gate_openings"] or pdb["sessions_started"] or pdb["successful_observations"] + pdb["failed_observations"]
    if case_policy == "static-baseline" and pdb_activity:
        raise StaticPolicyPdbViolation("static-baseline case opened or observed PDB; the frozen policy forbids it")


def _stop_reason_for_infrastructure(outcome: Mapping[str, Any]) -> str | None:
    infrastructure = outcome["infrastructure_evidence"]
    if outcome["terminal_status"] != "INFRASTRUCTURE_ERROR" or not isinstance(infrastructure, Mapping):
        return None
    if infrastructure.get("source_mutation_observed") is True:
        return "SOURCE_MUTATION"
    stage = infrastructure.get("stage")
    reason = infrastructure.get("reason_code")
    if stage == "provider_transport" and reason == "TRANSPORT_EVIDENCE_LOSS":
        return "TRANSPORT_EVIDENCE_LOSS"
    if stage == "cleanup" and reason == "CLEANUP_FAILURE":
        return "CLEANUP_FAILURE"
    if stage == "verifier" and reason in {"VERIFIER_INTEGRITY_FAILURE", "VERIFIER_FAILURE"}:
        return "VERIFIER_INTEGRITY_FAILURE"
    if stage in {"pre_provider", "containment_pre_provider"} and reason == "CONTAINMENT_FAILURE":
        return "CONTAINMENT_UNCERTAINTY"
    if reason == "RESULT_SCHEMA_INCONSISTENCY":
        return "RESULT_SCHEMA_INCONSISTENCY"
    return None


def _reason_specific_stop_fields(manifest: Mapping[str, Any], reason: str, trigger: Mapping[str, Any]) -> dict[str, Any]:
    infrastructure = trigger.get("infrastructure_evidence") or {}
    if reason == "TRANSPORT_EVIDENCE_LOSS":
        return {"expected_evidence_complete": True, "observed_evidence_complete": False}
    if reason == "CONTAINMENT_UNCERTAINTY":
        return {"expected_containment_confirmed": True, "observed_containment_confirmed": "UNKNOWN"}
    if reason == "CLEANUP_FAILURE":
        return {"expected_cleanup_succeeded": True, "observed_cleanup_succeeded": False}
    if reason == "VERIFIER_INTEGRITY_FAILURE":
        return {"expected_verifier_integrity": True, "observed_verifier_integrity": False}
    if reason == "RESULT_SCHEMA_INCONSISTENCY":
        return {"schema_error_code": infrastructure.get("schema_error_code") or "TERMINAL_CONTRADICTION"}
    if reason == "SOURCE_MUTATION":
        task_id = trigger.get("task_id")
        entry = next(item for item in manifest["inventory"] if item["task_id"] == task_id)
        return {
            "expected_source_hash": entry["source_sha256"],
            "observed_source_hash": trigger.get("source_hash"),
        }
    return {}


def _verify_authority_state(
    manifest: Mapping[str, Any],
    *,
    git_state_provider: Callable[[str], GitRepositoryState] | None = None,
    execution_commit: str | None = None,
    manifest_loader: Callable[[], Mapping[str, Any]] | None = None,
    source_authority_loader: Callable[[], bytes] | None = None,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Re-verify every campaign authority; return (reason, typed record) on drift.

    Covers the tracked manifest, the qualification contract, the tracked
    source-integrity authority, and the execution-commit contract (actual HEAD
    equals the authorization-bound execution commit, commit exists and
    descends from the accepted baseline, tracked working tree and Git index
    clean, no non-ignored untracked files).  Commit/tracked-state drift is
    reported through the accepted ``TRACKED_SOURCE_CHANGED`` authority stop;
    the observed value is the runner's tracked-source state fingerprint.
    """
    current_manifest = manifest_loader() if manifest_loader is not None else pilot.load_manifest(pilot.MANIFEST_PATH_V2)
    if pilot.manifest_hash(current_manifest) != pilot.manifest_hash(manifest):
        record = {
            "identity": "AUTHORITY_CHECK:MANIFEST", "reason_code": "MANIFEST_HASH_CHANGED",
            "evidence_reference": "pre-case-authority:manifest",
            "expected_manifest_hash": pilot.manifest_hash(manifest),
            "observed_manifest_hash": pilot.manifest_hash(current_manifest),
            "execution_commit": execution_commit,
        }
        return "MANIFEST_HASH_CHANGED", record
    if pilot.qualification_contract_hash(current_manifest) != manifest["qualification_contract_hash"]:
        record = {
            "identity": "AUTHORITY_CHECK:QUALIFICATION_CONTRACT", "reason_code": "QUALIFICATION_CONTRACT_CHANGED",
            "evidence_reference": "pre-case-authority:qualification-contract",
            "expected_qualification_contract_hash": manifest["qualification_contract_hash"],
            "observed_qualification_contract_hash": pilot.qualification_contract_hash(current_manifest),
            "execution_commit": execution_commit,
        }
        return "QUALIFICATION_CONTRACT_CHANGED", record
    raw = source_authority_loader() if source_authority_loader is not None else (pilot.REPO_ROOT / pilot.SOURCE_INTEGRITY_RELATIVE_PATH).read_bytes()
    observed_authority = hashlib.sha256(raw).hexdigest()
    if observed_authority != pilot.SOURCE_INTEGRITY_SHA256:
        record = {
            "identity": "AUTHORITY_CHECK:TRACKED_SOURCE", "reason_code": "TRACKED_SOURCE_CHANGED",
            "evidence_reference": "pre-case-authority:tracked-source",
            "expected_source_authority_hash": pilot.SOURCE_INTEGRITY_SHA256,
            "observed_source_authority_hash": observed_authority,
            "execution_commit": execution_commit,
        }
        return "TRACKED_SOURCE_CHANGED", record
    if git_state_provider is not None and execution_commit is not None:
        reference_time = now if now is not None else _utc_now()
        try:
            state = git_state_provider(execution_commit)
        except LiveRunnerError:
            raise
        except Exception as exc:
            raise LiveRunnerError(f"repository state could not be re-verified: {type(exc).__name__}: {exc}") from exc
        drifted = (
            state.head != execution_commit
            or not state.execution_commit_exists
            or not state.execution_commit_descends_from_baseline
            or not state.tracked_working_tree_clean
            or not state.git_index_clean
            or bool(state.untracked_non_ignored)
        )
        if drifted:
            record = {
                "identity": "AUTHORITY_CHECK:TRACKED_SOURCE", "reason_code": "TRACKED_SOURCE_CHANGED",
                "evidence_reference": "pre-case-authority:execution-commit",
                "expected_source_authority_hash": pilot.SOURCE_INTEGRITY_SHA256,
                "observed_source_authority_hash": _tracked_state_fingerprint(state, reference_time),
                "execution_commit": execution_commit,
                "git_head": state.head,
            }
            return "TRACKED_SOURCE_CHANGED", record
    return None


def _pre_case_authority_check(
    manifest: Mapping[str, Any],
    *,
    git_state_provider: Callable[[str], GitRepositoryState] | None = None,
    execution_commit: str | None = None,
    manifest_loader: Callable[[], Mapping[str, Any]] | None = None,
    source_authority_loader: Callable[[], bytes] | None = None,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Re-verify the campaign authorities before a case starts (accepted
    pre-case hook; see :func:`_verify_authority_state`)."""
    return _verify_authority_state(
        manifest,
        git_state_provider=git_state_provider,
        execution_commit=execution_commit,
        manifest_loader=manifest_loader,
        source_authority_loader=source_authority_loader,
        now=now,
    )


# ---- sanitization and private evidence ---------------------------------------


def sanitize_public_payload(payload: Mapping[str, Any], private_markers: tuple[str, ...]) -> tuple[dict[str, Any], list[str]]:
    """Return (clean copy, violations).  Any marker found in public output is a
    public/private evidence-boundary violation that must stop the campaign."""
    serialized = canonical_json(payload)
    violations = [marker for marker in private_markers if marker and marker in serialized]
    return dict(payload), violations


def _bounded_private_line(value: Any, maximum_bytes: int = MAX_PRIVATE_EVIDENCE_BYTES) -> str:
    from agentic_debugger.evaluation.live import redact_for_recording

    redacted = redact_for_recording(value)
    try:
        payload = json.dumps(redacted, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise OutputIntegrityError(f"private evidence failed strict JSON serialization: {exc}") from exc
    if len(payload.encode("utf-8")) > maximum_bytes:
        return json.dumps({
            "truncated": True,
            "original_character_count": len(payload),
            "record": redact_for_recording({"summary": "private evidence truncated"}),
        }, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return payload


def append_private_evidence(path: str | Path | None, value: Mapping[str, Any]) -> None:
    if path is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(_bounded_private_line(value) + "\n")
    except OSError as exc:
        raise OutputIntegrityError(f"private evidence write failed: {exc}") from exc


# ---- campaign orchestration ----------------------------------------------------


def build_plan(manifest: Mapping[str, Any], authorization: Mapping[str, Any], output_root: str | Path) -> dict[str, Any]:
    """The clear pre-provider plan displayed before execution."""
    validate_authorization_artifact(authorization, manifest, expected_output_root=output_root)
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "plan_kind": "pre-provider-plan",
        "campaign_id": manifest["campaign_id"],
        "campaign_version": manifest["campaign_version"],
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "authorization": {
            "schema_version": authorization["schema_version"],
            "operator_authorization_id": authorization["operator_authorization_id"],
            "campaign_attempt_identity": authorization["campaign_attempt_identity"],
            "authorization_hash": authorization_hash(authorization),
            "created_at": authorization["authorization_created_at"],
            "valid_until": authorization["authorization_valid_until"],
        },
        "execution_commit_binding": {
            "authorization_bound_execution_commit": authorization["accepted_campaign_commit"],
            "requirement": (
                "actual Git HEAD must equal the authorization-bound execution commit; the commit must exist and "
                "descend from the accepted baseline; the tracked working tree and Git index must be clean. Verified "
                "independently before ledger claim, preflight, transport creation, or provider contact, and re-verified "
                "before every case."
            ),
        },
        "route": {
            "provider": authorization["provider"],
            "model": authorization["model"],
            "variant": authorization["variant"],
            "protocol": authorization["protocol"],
            "expected_opencode_version": authorization["expected_opencode_version"],
            "expected_catalog_fingerprint": authorization["expected_catalog_fingerprint"],
            "expected_runtime_model_id": authorization["expected_runtime_model_id"],
            "expected_billing_route": authorization["expected_billing_route"],
            "expected_account_status": authorization["expected_account_status"],
        },
        "frozen_case_order": [case["case_id"] for case in manifest["case_order"]],
        "budgets": dict(manifest["budgets"]),
        "output_root": str(Path(output_root).resolve()),
        "provider_contact_guarantee": "zero provider processes, transport requests, and model calls until authorization, execution-commit, configuration, route preflight, output-root ownership, and ledger gates all succeed",
        "execution_requires": [
            "explicit live authorization artifact",
            "actual repository HEAD equal to the authorization-bound execution commit",
            "clean tracked working tree and Git index",
            "exclusive output/attempt root for this attempt identity",
            "durable attempt-ledger claim",
            "successful pre-provider route gate",
            "explicitly configured provider transport",
            "explicitly configured case runner",
        ],
    }


def _campaign_rejection_record(
    manifest: Mapping[str, Any],
    authorization: Mapping[str, Any] | None,
    output_root: str | Path,
    *,
    stop_reason: str,
    detail: str | None,
    execution_evidence: Mapping[str, Any] | None = None,
    lifecycle_state: str = "authorization-rejected",
) -> dict[str, Any]:
    """A pre-execution rejection record (non-authoritative; never written as
    campaign.json and never allowed to replace attempt evidence)."""
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "record_kind": "campaign",
        "campaign_id": manifest["campaign_id"],
        "campaign_version": manifest["campaign_version"],
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "authorization_identity": {
            "operator_authorization_id": authorization.get("operator_authorization_id") if authorization else None,
            "campaign_attempt_identity": authorization.get("campaign_attempt_identity") if authorization else None,
            "authorization_hash": authorization_hash(authorization) if authorization else None,
        },
        "campaign_attempt_identity": authorization.get("campaign_attempt_identity") if authorization else None,
        "output_root": str(Path(output_root).resolve()),
        "status": "REJECTED",
        "stop_reason": stop_reason,
        "stop_detail": detail,
        "execution_commit": execution_evidence,
        "provider_processes_started": 0,
        "provider_call_proof": {"transports_created": 0, "process_launches": 0, "logical_requests": 0},
        "preflight": None,
        "frozen_case_order": [case["case_id"] for case in manifest["case_order"]],
        "case_lifecycle_states": {case["case_id"]: lifecycle_state for case in manifest["case_order"]},
        "counts": {
            "logical_model_calls": 0, "provider_process_attempts": 0, "transport_retries": 0,
            "accepted_directives": 0, "malformed_directive_rejections": 0,
            "completed_case_count": 0, "blocked_case_count": 0, "aborted_case_count": 0,
            "invalidated_case_count": 0, "unstarted_case_count": 6,
        },
        "authority_stop": None,
        "authority_invalidated_cases": [],
        "pdb": {"gate_openings": 0, "observations": 0, "sessions_started": 0},
        "hypotheses": 0, "patch_submissions": 0,
        "verifier": {"runs": 0},
        "timing": {"campaign_wall_clock_seconds": 0},
        "cost_summary": {"classification": "ABSENT", "total_provider_reported_cost": None, "provider_reported": False},
        "cleanup": {"verdict": "NOT_STARTED"},
        "source_integrity": {"verdict": "NOT_CHECKED"},
        "sanitization": {"markers_checked": 0, "violations": 0},
        "ledger": None,
        "cases": [],
    }


def _blocked_pre_provider_record(
    manifest: Mapping[str, Any],
    authorization: Mapping[str, Any],
    output_root: str | Path,
    verdict: PreflightVerdict,
    *,
    identity: str,
    execution_evidence: Mapping[str, Any],
    block_record: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "record_kind": "campaign",
        "campaign_id": manifest["campaign_id"],
        "campaign_version": manifest["campaign_version"],
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "authorization_identity": {
            "operator_authorization_id": authorization["operator_authorization_id"],
            "campaign_attempt_identity": identity,
            "authorization_hash": authorization_hash(authorization),
        },
        "campaign_attempt_identity": identity,
        "output_root": str(Path(output_root).resolve()),
        "status": "BLOCKED",
        "stop_reason": f"BLOCKED_PRE_PROVIDER:{verdict.failure_category}",
        "stop_detail": verdict.detail,
        "execution_commit": execution_evidence,
        "provider_processes_started": 0,
        "provider_call_proof": {"transports_created": 0, "process_launches": 0, "logical_requests": 0},
        "preflight": verdict.to_mapping(),
        "frozen_case_order": [case["case_id"] for case in manifest["case_order"]],
        "case_lifecycle_states": {
            block_record["case_id"]: "pre-provider-rejected",
            **{case["case_id"]: "unstarted" for case in manifest["case_order"] if case["case_id"] != block_record["case_id"]},
        },
        "counts": {
            "logical_model_calls": 0, "provider_process_attempts": 0, "transport_retries": 0,
            "accepted_directives": 0, "malformed_directive_rejections": 0,
            "completed_case_count": 0, "blocked_case_count": 1, "aborted_case_count": 0,
            "invalidated_case_count": 0, "unstarted_case_count": 5,
        },
        "authority_stop": None,
        "authority_invalidated_cases": [],
        "pdb": {"gate_openings": 0, "observations": 0, "sessions_started": 0},
        "hypotheses": 0, "patch_submissions": 0,
        "verifier": {"runs": 0},
        "timing": {"campaign_wall_clock_seconds": 0},
        "cost_summary": _cost_summary([block_record]),
        "cleanup": {"verdict": "NOT_STARTED"},
        "source_integrity": {"verdict": "NOT_CHECKED"},
        "sanitization": {"markers_checked": 0, "violations": 0},
        "ledger": None,
        "cases": [{**block_record, "record_sha256": pilot.result_sha256(block_record)}],
    }


class _CountingTransportProxy:
    """Wraps one fresh provider-process transport and counts its requests."""

    def __init__(self, transport: Any, counter: "ProviderCallCounter") -> None:
        self._transport = transport
        self._counter = counter

    def request(self, payload: Mapping[str, Any], timeout_seconds: float) -> Mapping[str, Any]:
        self._counter.logical_requests += 1
        self._counter.process_launches += 1
        return self._transport.request(payload, timeout_seconds)


class ProviderCallCounter:
    """Deterministic proof of provider activity: every transport creation and
    request invocation is counted; failed preflight paths must leave all
    counts at zero."""

    def __init__(self) -> None:
        self.transports_created = 0
        self.process_launches = 0
        self.logical_requests = 0

    def proof(self) -> dict[str, int]:
        return {
            "transports_created": self.transports_created,
            "process_launches": self.process_launches,
            "logical_requests": self.logical_requests,
        }


def _default_case_runner(*args: Any, **kwargs: Any) -> Any:
    raise LiveRunnerError(
        "live case execution requires an explicitly configured case runner bound to the accepted "
        "QuixBugs live execution path (facts, adapter, verifier, containment) and provider transport; "
        "none is configured in this runner task"
    )


def run_campaign(
    manifest: Mapping[str, Any],
    *,
    authorization: Mapping[str, Any],
    output_root: str | Path,
    route_evidence_provider: Callable[[], Mapping[str, Any]] | None = None,
    transport_factory: Callable[[Mapping[str, Any]], Any] | None = None,
    case_runner: Callable[..., Any] | None = None,
    ledger_path: str | Path | None = None,
    private_markers: tuple[str, ...] = (),
    clock: Callable[[], float] | None = None,
    git_state_provider: Callable[[str], GitRepositoryState] | None = None,
    now: datetime | None = None,
    staleness_window_seconds: int = MAX_ROUTE_EVIDENCE_AGE_SECONDS,
    clock_skew_seconds: int = CLOCK_SKEW_ALLOWANCE_SECONDS,
    terminalization_fault: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute the frozen six-case v2 campaign, fail-closed.

    Returns the deterministic, versioned campaign record.  Raises
    :class:`LiveRunnerError` only for hard configuration errors (never for a
    gate rejection, which is recorded as a REJECTED/BLOCKED campaign).
    """
    pilot.validate_manifest(manifest)
    if manifest["campaign_id"] != CAMPAIGN_ID_V2:
        raise LiveRunnerError("the live runner is bound to the frozen v2 campaign only")
    output = Path(output_root)
    counter = ProviderCallCounter()

    def _reject(stop_reason: str, detail: str | None, *, execution_evidence: Mapping[str, Any] | None = None, lifecycle_state: str = "authorization-rejected") -> dict[str, Any]:
        record = _campaign_rejection_record(
            manifest, authorization, output, stop_reason=stop_reason, detail=detail,
            execution_evidence=execution_evidence, lifecycle_state=lifecycle_state,
        )
        _write_rejection_record(output, record)
        return record

    try:
        reference_time = now if now is not None else _utc_now(clock)
        start = clock() if clock is not None else 0.0
        if not math.isfinite(start):
            raise LiveRunnerError("non-finite campaign clock value")
    except LiveRunnerError as exc:
        return _reject("NON_FINITE_VALUE", str(exc), lifecycle_state="unstarted")

    # 1. authorization boundary
    auth_failure = authorization_failure(authorization, manifest, expected_output_root=output, now=reference_time)
    if auth_failure is not None:
        return _reject(auth_failure, "authorization artifact rejected before any provider contact")
    auth_hash = authorization_hash(authorization)
    identity = attempt_identity_for(authorization)
    execution_commit = authorization["accepted_campaign_commit"]

    # 2. execution-commit / repository-state contract (before ledger claim,
    #    preflight, transport creation, or provider contact)
    try:
        execution_evidence = verify_execution_repository_state(authorization, git_state_provider, now=reference_time)
    except RepositoryStateError as exc:
        return _reject(exc.category, exc.detail, execution_evidence={
            "authorization_bound_execution_commit": execution_commit,
            "detail": exc.detail,
            "checked_at": reference_time.isoformat().replace("+00:00", "Z"),
        })
    except LiveRunnerError as exc:
        return _reject("REPOSITORY_STATE_UNVERIFIABLE", str(exc))

    # 3. hard configuration failures reject before consuming the authorization
    if transport_factory is None:
        return _reject("TRANSPORT_NOT_CONFIGURED", "no provider transport has been explicitly configured; live execution is rejected", execution_evidence=execution_evidence, lifecycle_state="unstarted")
    if case_runner is None:
        return _reject("CASE_RUNNER_NOT_CONFIGURED", "no case runner has been explicitly configured; live execution is rejected", execution_evidence=execution_evidence, lifecycle_state="unstarted")

    # 4. pre-provider route gate
    try:
        verdict = run_route_preflight(
            manifest, authorization, route_evidence_provider,
            now=reference_time, staleness_window_seconds=staleness_window_seconds,
            clock_skew_seconds=clock_skew_seconds,
            attempt_identity=identity,
            execution_commit=execution_commit,
        )
    except RouteEvidenceInvalid as exc:
        return _reject(f"ROUTE_EVIDENCE_INVALID:{exc.reason}", exc.detail, execution_evidence=execution_evidence, lifecycle_state="unstarted")
    except LiveRunnerError as exc:
        return _reject("ROUTE_EVIDENCE_NOT_CONFIGURED", str(exc), execution_evidence=execution_evidence, lifecycle_state="unstarted")

    if not verdict.passed:
        case_one = manifest["case_order"][0]
        try:
            block_record = build_preprovider_block_record(
                manifest, case_one, authorization, verdict,
                attempt_identity=identity, execution_commit=execution_commit,
            )
        except (pilot.PilotError, LiveRunnerError) as exc:
            return _reject("SCHEMA_INCONSISTENCY", f"pre-provider block record could not be materialized: {exc}", execution_evidence=execution_evidence, lifecycle_state="unstarted")
        record = _blocked_pre_provider_record(
            manifest, authorization, output, verdict,
            identity=identity, execution_evidence=execution_evidence, block_record=block_record,
        )
        _write_rejection_record(output, record)
        return record

    route_observation = verdict.route_observation

    # 5. output-root ownership (one root, exactly one attempt identity; the
    #    exclusive owner record is the single-winner claim gate and the root
    #    must be absent or structurally empty)
    try:
        claim_output_root(
            output,
            attempt_identity=identity,
            authorization_hash=auth_hash,
            campaign_manifest_hash=pilot.manifest_hash(manifest),
            now=reference_time,
        )
    except SameAttemptClaimError as exc:
        return _reject("DUPLICATE_ATTEMPT", str(exc), execution_evidence=execution_evidence, lifecycle_state="unstarted")
    except OutputRootOwnedError as exc:
        return _reject("OUTPUT_ROOT_OWNED", str(exc), execution_evidence=execution_evidence, lifecycle_state="unstarted")
    except OutputRootOccupiedError as exc:
        return _reject("OUTPUT_ROOT_OCCUPIED", str(exc), execution_evidence=execution_evidence, lifecycle_state="unstarted")
    except AttemptClaimError as exc:
        return _reject(exc.code, str(exc), execution_evidence=execution_evidence, lifecycle_state="unstarted")

    # 6. durable attempt ledger
    ledger = AttemptLedger(ledger_path if ledger_path is not None else output / "ledger.json")
    ledger_entry: dict[str, Any] = {
        "attempt_identity": identity,
        "authorization_hash": auth_hash,
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "execution_commit": execution_commit,
        "case_ids": [case["case_id"] for case in manifest["case_order"]],
        "route_binding": {
            "provider": authorization["provider"], "model": authorization["model"],
            "variant": authorization["variant"], "protocol": authorization["protocol"],
            "opencode_version": authorization["expected_opencode_version"],
            "catalog_fingerprint": authorization["expected_catalog_fingerprint"],
            "runtime_model_id": authorization["expected_runtime_model_id"],
            "billing_route": authorization["expected_billing_route"],
            "execution_commit": execution_commit,
        },
        "status": "STARTED",
        "created_at": reference_time.isoformat().replace("+00:00", "Z"),
        "updated_at": reference_time.isoformat().replace("+00:00", "Z"),
        "output_root": str(output.resolve()),
    }
    try:
        ledger.claim(ledger_entry)
    except LiveRunnerError as exc:
        if "still STARTED" in str(exc):
            stop_reason = "ATTEMPT_ALREADY_STARTED"
        elif "duplicate" in str(exc).lower() or "already been used" in str(exc).lower():
            stop_reason = "DUPLICATE_ATTEMPT"
        else:
            stop_reason = "LEDGER_CONTRADICTION"
        return _reject(stop_reason, str(exc), execution_evidence=execution_evidence, lifecycle_state="authorization-rejected")

    case_dir = output / "cases"
    private_log = output / "private" / "evidence.jsonl"
    private_evidence_path = private_log

    def _write_case_record(record: Mapping[str, Any], case: Mapping[str, Any]) -> None:
        case_path = case_dir / f"case-{int(case['order_index']):02d}-{case['case_id'].replace(':', '__')}.json"
        atomic_create_json(case_path, record)
        append_private_evidence(private_evidence_path, {
            "record_kind": "private-case-evidence",
            "case_id": case["case_id"],
            "attempt_identity": identity,
            "execution_commit": execution_commit,
            "record_sha256": pilot.result_sha256(record),
        })

    def _fail_campaign(
        stop_reason: str,
        detail: str,
        *,
        ledger_status: str,
        lifecycle: dict[str, str],
        records: list[dict[str, Any]],
        preflight: PreflightVerdict | None,
        completed: int,
        blocked: int,
        unstarted: int,
        counts: dict[str, int],
        timing: float,
        authority_stop_info: Mapping[str, Any] | None = None,
        authority_invalidated_cases: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        aborted = sum(1 for value in lifecycle.values() if value == "aborted")
        invalidated = sum(1 for value in lifecycle.values() if value == "authority-invalidated")
        counts = dict(counts)
        counts["aborted_case_count"] = aborted
        counts["invalidated_case_count"] = invalidated
        campaign = {
            "schema_version": RUNNER_SCHEMA_VERSION,
            "record_kind": "campaign",
            "campaign_id": manifest["campaign_id"],
            "campaign_version": manifest["campaign_version"],
            "campaign_manifest_hash": pilot.manifest_hash(manifest),
            "accepted_baseline": ACCEPTED_BASELINE,
            "planning_baseline_commit": manifest["planning_baseline_commit"],
            "authorization_identity": {
                "operator_authorization_id": authorization["operator_authorization_id"],
                "campaign_attempt_identity": identity,
                "authorization_hash": auth_hash,
                "created_at": authorization["authorization_created_at"],
                "valid_until": authorization["authorization_valid_until"],
            },
            "campaign_attempt_identity": identity,
            "output_root": str(output.resolve()),
            "status": ledger_status,
            "stop_reason": stop_reason,
            "stop_detail": detail,
            "execution_commit": execution_evidence,
            "provider_processes_started": counts["provider_process_attempts"],
            "provider_call_proof": counter.proof(),
            "preflight": preflight.to_mapping() if preflight is not None else None,
            "frozen_case_order": [case["case_id"] for case in manifest["case_order"]],
            "case_execution_order": list(attempted_order),
            "case_lifecycle_states": lifecycle,
            "counts": counts,
            "authority_stop": authority_stop_info,
            "authority_invalidated_cases": list(authority_invalidated_cases) if authority_invalidated_cases else [],
            "pdb": {
                "gate_openings": sum(int(r["pdb_counts"]["allowed_gate_openings"]) for r in records),
                "observations": sum(int(r["pdb_counts"]["successful_observations"]) + int(r["pdb_counts"]["failed_observations"]) for r in records),
                "sessions_started": sum(int(r["pdb_counts"]["sessions_started"]) for r in records),
            },
            "hypotheses": sum(int(r["hypotheses_created"]) for r in records),
            "patch_submissions": sum(int(r["patch_submissions"]) for r in records),
            "verifier": {"runs": sum(int(r["verifier_runs"]) for r in records)},
            "timing": {"campaign_wall_clock_seconds": round(timing, 3)},
            "cost_summary": _cost_summary(records),
            "cleanup": {"verdict": "OK" if all(r["owned_workspace_cleanup"] and r["canonical_source_restoration"] for r in records) else "PARTIAL"},
            "source_integrity": {"verdict": "OK" if all(r["source_hash"] is None or r["canonical_source_restoration"] for r in records) else "PARTIAL"},
            "sanitization": {"markers_checked": len(private_markers), "violations": 0},
            "ledger": None,
            "cases": [{**r, "record_sha256": pilot.result_sha256(r)} for r in records],
        }
        # ---- crash-safe terminal package commitment ---------------------------
        #
        # Three durable steps, in order; the terminal commitment is created
        # only after both campaign.json and the terminal ledger entry are
        # durable and mutually hash-bound.  A process death at ANY transition
        # leaves either a fully committed and verifiable package, or an
        # explicitly uncommitted package that verify_attempt_package() and
        # every operator-facing loader reject.  No best-effort deletion is
        # used for correctness: the intermediate campaign payload is clearly
        # non-authoritative (commit_state "PREPARED", terminal_commit null)
        # until the create-once terminal-commit.json exists.
        #
        # T1: create the terminal campaign payload (create-once).
        intended_status = ledger_status
        terminal_entry = dict(ledger_entry)
        terminal_entry.update({
            "status": intended_status,
            "updated_at": reference_time.isoformat().replace("+00:00", "Z"),
            "stop_reason": stop_reason,
        })
        campaign["ledger"] = terminal_entry
        campaign["commit_state"] = "PREPARED"
        campaign["terminal_commit"] = None
        final_status = intended_status
        campaign["artifact_written"] = True
        if terminalization_fault is not None:
            terminalization_fault("after_prepare")
        try:
            _assert_finite_json(campaign)
            atomic_create_json(output / "campaign.json", campaign)
        except NonFiniteValueError as exc:
            final_status = "ABORTED"
            campaign["status"] = "ABORTED"
            campaign["stop_reason"] = "NON_FINITE_VALUE"
            campaign["stop_detail"] = f"campaign record carries non-finite numeric evidence: {exc}"
            campaign["artifact_written"] = False
        except OutputIntegrityError as exc:
            final_status = "ABORTED"
            campaign["status"] = "ABORTED"
            if intended_status not in ("ABORTED",):
                campaign["stop_reason"] = "OUTPUT_INTEGRITY_FAILURE"
            campaign["stop_detail"] = f"{detail} [campaign artifact write failed: {exc}]"
            campaign["artifact_written"] = False
        if terminalization_fault is not None:
            terminalization_fault("after_campaign_payload")
        campaign_json_sha256: str | None = None
        if campaign["artifact_written"]:
            campaign_json_sha256 = hashlib.sha256((output / "campaign.json").read_bytes()).hexdigest()
        # T2: finalize the ledger to the same terminal status.
        terminal_entry = dict(ledger_entry)
        terminal_entry.update({
            "status": final_status,
            "updated_at": reference_time.isoformat().replace("+00:00", "Z"),
            "stop_reason": campaign["stop_reason"],
        })
        campaign["ledger"] = terminal_entry
        try:
            if terminalization_fault is not None:
                terminalization_fault("before_ledger_terminalization")
            ledger.finish(terminal_entry)
            if terminalization_fault is not None:
                terminalization_fault("after_ledger_terminalization")
        except LiveRunnerError as exc:
            # The package is left explicitly uncommitted: the ledger stays at
            # its previous (STARTED) state and no terminal commitment exists.
            campaign["status"] = "ABORTED"
            campaign["stop_reason"] = "LEDGER_FINALIZATION_FAILED"
            campaign["stop_detail"] = f"terminal ledger finalization failed: {exc}"
            campaign["artifact_written"] = False
            campaign["commit_state"] = "PREPARED"
            _write_rejection_record(output, campaign)
            return campaign
        # T3: create the terminal commitment (create-once, LAST) binding the
        # campaign payload, the exact terminal ledger entry, the frozen
        # manifest hash, and the case-record inventory.
        if campaign_json_sha256 is not None:
            ledger_entry_sha256 = sha256_text(canonical_json(terminal_entry))
            commitment = {
                "commit_version": TERMINAL_COMMIT_VERSION,
                "attempt_identity": identity,
                "authorization_hash": auth_hash,
                "execution_commit": execution_commit,
                "intended_terminal_status": final_status,
                "campaign_json_sha256": campaign_json_sha256,
                "ledger_entry_sha256": ledger_entry_sha256,
                "manifest_hash": pilot.manifest_hash(manifest),
                "case_inventory": [
                    {"case_id": entry["case_id"], "order_index": entry["order_index"], "record_sha256": entry["record_sha256"]}
                    for entry in campaign["cases"]
                ],
                "created_at": reference_time.isoformat().replace("+00:00", "Z"),
            }
            try:
                _assert_finite_json(commitment)
                if terminalization_fault is not None:
                    terminalization_fault("before_terminal_commit")
                atomic_create_json(
                    output / "terminal-commit.json", commitment,
                    fault_after_temp=(
                        (lambda: terminalization_fault("during_terminal_commit"))
                        if terminalization_fault is not None else None
                    ),
                )
            except (NonFiniteValueError, OutputIntegrityError) as exc:
                # The commitment could not be created: downgrade the ledger to
                # an honest output-integrity terminal state (best effort) and
                # return without a committed package.  verify_attempt_package
                # rejects the uncommitted package either way.
                campaign["status"] = "ABORTED"
                campaign["stop_reason"] = "OUTPUT_INTEGRITY_FAILURE"
                campaign["stop_detail"] = f"{detail} [terminal commitment creation failed: {exc}]"
                campaign["artifact_written"] = False
                campaign["commit_state"] = "PREPARED"
                downgraded = dict(ledger_entry)
                downgraded.update({
                    "status": "ABORTED",
                    "updated_at": reference_time.isoformat().replace("+00:00", "Z"),
                    "stop_reason": "OUTPUT_INTEGRITY_FAILURE",
                })
                try:
                    ledger.finish(downgraded)
                except LiveRunnerError:
                    pass
                return campaign
            commit_bytes = (output / "terminal-commit.json").read_bytes()
            campaign["terminal_commit_record_sha256"] = hashlib.sha256(commit_bytes).hexdigest()
            campaign["commit_state"] = "COMMITTED"
        return campaign

    records: list[dict[str, Any]] = []
    lifecycle: dict[str, str] = {}
    attempted_order: list[str] = []
    authority_invalidated_cases: list[dict[str, Any]] = []
    validator = pilot.CampaignResultValidator(manifest, authorization)
    stop_active: tuple[str, dict[str, Any]] | None = None
    abort_info: tuple[str, str] | None = None
    counts = {
        "logical_model_calls": 0, "provider_process_attempts": 0, "transport_retries": 0,
        "accepted_directives": 0, "malformed_directive_rejections": 0,
        "completed_case_count": 0, "blocked_case_count": 0, "aborted_case_count": 0,
        "invalidated_case_count": 0, "unstarted_case_count": 6,
    }

    for case in manifest["case_order"]:
        case_id = case["case_id"]
        if abort_info is not None:
            lifecycle[case_id] = "unstarted"
            continue
        if stop_active is not None:
            stop_reason, trigger = stop_active
            if trigger["kind"] == "prior_case":
                stop_evidence = build_campaign_stop_evidence(
                    manifest, stop_reason,
                    trigger_case_id=trigger["case_id"],
                    trigger_result_sha256=trigger["result_sha256"],
                    authority_identity=None, authority_record_sha256=None,
                    evidence_reference=f"{identity}:stop:{stop_reason}",
                    extra=_reason_specific_stop_fields(manifest, stop_reason, trigger["record"]),
                )
            else:
                stop_evidence = build_campaign_stop_evidence(
                    manifest, stop_reason,
                    trigger_case_id=None, trigger_result_sha256=None,
                    authority_identity=trigger["identity"],
                    authority_record_sha256=trigger["record_sha256"],
                    evidence_reference=f"{identity}:stop:{stop_reason}",
                    extra=trigger["extra"],
                )
            record = build_campaign_stop_record(manifest, case, authorization, stop_evidence,
                                                attempt_identity=identity, execution_commit=execution_commit)
            validator.validate_result(record)
            _write_case_record(record, case)
            records.append(record)
            lifecycle[case_id] = "blocked"
            counts["blocked_case_count"] += 1
            counts["unstarted_case_count"] -= 1
            continue

        try:
            authority = _pre_case_authority_check(
                manifest,
                git_state_provider=git_state_provider,
                execution_commit=execution_commit,
                now=reference_time,
            )
        except LiveRunnerError as exc:
            abort_info = ("SCHEMA_INCONSISTENCY", f"pre-case authority recheck failed closed: {exc}")
            lifecycle[case_id] = "unstarted"
            break
        if authority is not None:
            stop_reason, authority_record = authority
            try:
                validator.register_authority_checks([authority_record])
            except (pilot.PilotError, KeyError, TypeError, ValueError) as exc:
                abort_info = ("SCHEMA_INCONSISTENCY", f"authority-check record failed the frozen validation: {exc}")
                lifecycle[case_id] = "unstarted"
                break
            stored = validator.validated_authority_records[authority_record["identity"]]
            extra = {
                "expected_manifest_hash": authority_record.get("expected_manifest_hash"),
                "observed_manifest_hash": authority_record.get("observed_manifest_hash"),
                "expected_qualification_contract_hash": authority_record.get("expected_qualification_contract_hash"),
                "observed_qualification_contract_hash": authority_record.get("observed_qualification_contract_hash"),
                "expected_source_authority_hash": authority_record.get("expected_source_authority_hash"),
                "observed_source_authority_hash": authority_record.get("observed_source_authority_hash"),
            }
            stop_active = (stop_reason, {"kind": "authority", "identity": authority_record["identity"], "record_sha256": stored.sha256, "extra": extra})
            stop_evidence = build_campaign_stop_evidence(
                manifest, stop_reason,
                trigger_case_id=None, trigger_result_sha256=None,
                authority_identity=authority_record["identity"],
                authority_record_sha256=stored.sha256,
                evidence_reference=f"{identity}:stop:{stop_reason}",
                extra=extra,
            )
            record = build_campaign_stop_record(manifest, case, authorization, stop_evidence,
                                                attempt_identity=identity, execution_commit=execution_commit)
            validator.validate_result(record)
            _write_case_record(record, case)
            records.append(record)
            lifecycle[case_id] = "blocked"
            counts["blocked_case_count"] += 1
            counts["unstarted_case_count"] -= 1
            continue

        lifecycle[case_id] = "provider-attempted"
        attempted_order.append(case_id)
        requests_before = counter.logical_requests
        launches_before = counter.process_launches
        try:
            transport = _CountingTransportProxy(transport_factory(case), counter)
        except Exception as exc:
            abort_info = ("TRANSPORT_NOT_CONFIGURED", f"provider transport could not be created: {type(exc).__name__}: {exc}")
            lifecycle[case_id] = "aborted"
            counts["unstarted_case_count"] -= 1
            break
        counter.transports_created += 1
        run_id = deterministic_run_id(identity, case)
        session_id = deterministic_session_id(identity, case)
        try:
            outcome = case_runner(
                case,
                attempt_identity=identity,
                run_id=run_id,
                session_id=session_id,
                transport=transport,
                route_observation=route_observation,
                budgets=manifest["budgets"],
                clock=clock,
            )
        except RouteDriftError as exc:
            outcome = _drift_outcome(case, run_id, route_observation, counter.logical_requests - requests_before, exc, manifest)
        except LiveRunnerError as exc:
            abort_info = ("UNEXPECTED_CASE_FAILURE", f"case runner failed closed: {type(exc).__name__}: {exc}")
            lifecycle[case_id] = "aborted"
            counts["unstarted_case_count"] -= 1
            counts["provider_process_attempts"] += counter.process_launches - launches_before
            break
        except Exception as exc:
            abort_info = ("UNEXPECTED_CASE_FAILURE", f"case runner raised {type(exc).__name__}: {exc}")
            lifecycle[case_id] = "aborted"
            counts["unstarted_case_count"] -= 1
            counts["provider_process_attempts"] += counter.process_launches - launches_before
            break
        if not isinstance(outcome, Mapping):
            abort_info = ("UNEXPECTED_CASE_FAILURE", "case runner returned a non-object outcome")
            lifecycle[case_id] = "aborted"
            counts["unstarted_case_count"] -= 1
            counts["provider_process_attempts"] += counter.process_launches - launches_before
            break
        try:
            validate_case_outcome(outcome)
        except LiveRunnerError as exc:
            abort_info = ("SCHEMA_INCONSISTENCY", f"case outcome is invalid: {exc}")
            lifecycle[case_id] = "aborted"
            counts["unstarted_case_count"] -= 1
            counts["provider_process_attempts"] += counter.process_launches - launches_before
            break
        try:
            enforce_case_budgets(outcome, manifest, case_policy=case["policy"])
        except StaticPolicyPdbViolation as exc:
            abort_info = ("STATIC_POLICY_PDB_VIOLATION", f"case budget or accounting violation: {exc}")
            lifecycle[case_id] = "aborted"
            counts["unstarted_case_count"] -= 1
            counts["provider_process_attempts"] += counter.process_launches - launches_before
            break
        except (LiveRunnerError, KeyError, TypeError, ValueError) as exc:
            abort_info = ("BUDGET_EXCEEDED", f"case budget or accounting violation: {exc}")
            lifecycle[case_id] = "aborted"
            counts["unstarted_case_count"] -= 1
            counts["provider_process_attempts"] += counter.process_launches - launches_before
            break
        if outcome["provider_process_attempts"] != counter.process_launches - launches_before:
            abort_info = ("SCHEMA_INCONSISTENCY", "case outcome provider-process count disagrees with the transport counter")
            lifecycle[case_id] = "aborted"
            counts["unstarted_case_count"] -= 1
            counts["provider_process_attempts"] += counter.process_launches - launches_before
            break
        if counter.logical_requests - requests_before != counter.process_launches - launches_before:
            abort_info = ("SCHEMA_INCONSISTENCY", "case outcome logical-request accounting disagrees with the transport counter")
            lifecycle[case_id] = "aborted"
            counts["unstarted_case_count"] -= 1
            counts["provider_process_attempts"] += counter.process_launches - launches_before
            break

        record = materialize_case_record(manifest, case, authorization, route_observation, outcome,
                                         attempt_identity=identity, execution_commit=execution_commit)
        try:
            validator.validate_result(record)
        except (pilot.PilotError, KeyError, TypeError, ValueError) as exc:
            abort_info = ("SCHEMA_INCONSISTENCY", f"case record failed the frozen v2 result validation: {exc}")
            lifecycle[case_id] = "aborted"
            counts["unstarted_case_count"] -= 1
            counts["provider_process_attempts"] += counter.process_launches - launches_before
            break
        clean, violations = sanitize_public_payload(record, private_markers)
        if violations:
            abort_info = ("PUBLIC_PRIVATE_BOUNDARY_VIOLATION", f"public record exposes private material markers: {violations}")
            lifecycle[case_id] = "aborted"
            counts["unstarted_case_count"] -= 1
            counts["provider_process_attempts"] += counter.process_launches - launches_before
            break
        _write_case_record(clean, case)
        append_private_evidence(private_evidence_path, {
            "record_kind": "private-case-evidence",
            "case_id": case_id,
            "attempt_identity": identity,
            "execution_commit": execution_commit,
            "record_sha256": pilot.result_sha256(record),
            "lifecycle": {
                "case_id": case_id,
                "provider_process_attempts": outcome["provider_process_attempts"],
                "logical_model_calls": outcome["logical_model_calls"],
                "transport_retries": outcome["retries"],
                "accepted_directives": outcome["valid_directives"],
                "terminal_status": outcome["terminal_status"],
            },
        })
        records.append(record)
        counts["logical_model_calls"] += int(outcome["logical_model_calls"])
        counts["provider_process_attempts"] += int(outcome["provider_process_attempts"])
        counts["transport_retries"] += int(outcome["retries"])
        counts["accepted_directives"] += int(outcome["valid_directives"])
        counts["malformed_directive_rejections"] += int(outcome["malformed_directive_rejections"])
        counts["unstarted_case_count"] -= 1

        # Post-case authority re-verification: after the case runner returned
        # and after its cleanup/restoration phase, independently re-verify the
        # execution commit, baseline ancestry, index/tracked cleanliness, and
        # the tracked manifest and source-integrity authorities.  Drift
        # invalidates the affected case: its raw execution outcome is preserved
        # only as quarantined authority-invalidated evidence, it never counts
        # as completed, and the campaign stops with the accepted typed
        # authority/campaign-stop evidence.
        try:
            post_case_drift = _verify_authority_state(
                manifest,
                git_state_provider=git_state_provider,
                execution_commit=execution_commit,
                now=reference_time,
            )
        except LiveRunnerError as exc:
            abort_info = ("SCHEMA_INCONSISTENCY", f"post-case authority recheck failed closed: {exc}")
            break
        if post_case_drift is not None:
            stop_reason, authority_record = post_case_drift
            try:
                validator.register_authority_checks([authority_record])
            except (pilot.PilotError, KeyError, TypeError, ValueError) as exc:
                abort_info = ("SCHEMA_INCONSISTENCY", f"authority-check record failed the frozen validation: {exc}")
                break
            stored = validator.validated_authority_records[authority_record["identity"]]
            extra = {
                "expected_manifest_hash": authority_record.get("expected_manifest_hash"),
                "observed_manifest_hash": authority_record.get("observed_manifest_hash"),
                "expected_qualification_contract_hash": authority_record.get("expected_qualification_contract_hash"),
                "observed_qualification_contract_hash": authority_record.get("observed_qualification_contract_hash"),
                "expected_source_authority_hash": authority_record.get("expected_source_authority_hash"),
                "observed_source_authority_hash": authority_record.get("observed_source_authority_hash"),
            }
            lifecycle[case_id] = "authority-invalidated"
            counts["invalidated_case_count"] += 1
            authority_invalidated_cases.append({
                "case_id": case_id,
                "original_raw_terminal_outcome": outcome["terminal_status"],
                "original_terminal_reason_code": outcome["terminal_reason_code"],
                "authority_failure_reason": stop_reason,
                "authority_check_record_sha256": stored.sha256,
                "provider_contact_occurred": int(outcome["provider_process_attempts"]) > 0,
                "excluded_from_evaluation": True,
                "observed_at": reference_time.isoformat().replace("+00:00", "Z"),
            })
            stop_active = (stop_reason, {
                "kind": "authority",
                "identity": authority_record["identity"],
                "record_sha256": stored.sha256,
                "extra": extra,
                "post_case_case_id": case_id,
            })
            continue

        lifecycle[case_id] = "completed"
        if outcome["terminal_status"] in COMPLETED_TERMINAL_STATUSES:
            counts["completed_case_count"] += 1
        elif outcome["terminal_status"] == "BLOCKED":
            counts["blocked_case_count"] += 1

        stop_reason = _stop_reason_for_infrastructure(outcome)
        if stop_reason is not None:
            stop_active = (stop_reason, {"kind": "prior_case", "case_id": case_id, "result_sha256": pilot.result_sha256(record), "record": record})

    for case in manifest["case_order"]:
        lifecycle.setdefault(case["case_id"], "unstarted")

    # Final authority check immediately before terminal ledger finalization.
    # Drift here prevents a terminal COMPLETED ledger or campaign.
    authority_stop_info: dict[str, Any] | None = None
    if abort_info is None and stop_active is None:
        try:
            pre_terminal_drift = _verify_authority_state(
                manifest,
                git_state_provider=git_state_provider,
                execution_commit=execution_commit,
                now=reference_time,
            )
        except LiveRunnerError as exc:
            abort_info = ("SCHEMA_INCONSISTENCY", f"pre-terminal authority recheck failed closed: {exc}")
            pre_terminal_drift = None
        if pre_terminal_drift is not None:
            stop_reason, authority_record = pre_terminal_drift
            try:
                validator.register_authority_checks([authority_record])
            except (pilot.PilotError, KeyError, TypeError, ValueError) as exc:
                abort_info = ("SCHEMA_INCONSISTENCY", f"authority-check record failed the frozen validation: {exc}")
            else:
                stored = validator.validated_authority_records[authority_record["identity"]]
                extra = {
                    "expected_manifest_hash": authority_record.get("expected_manifest_hash"),
                    "observed_manifest_hash": authority_record.get("observed_manifest_hash"),
                    "expected_qualification_contract_hash": authority_record.get("expected_qualification_contract_hash"),
                    "observed_qualification_contract_hash": authority_record.get("observed_qualification_contract_hash"),
                    "expected_source_authority_hash": authority_record.get("expected_source_authority_hash"),
                    "observed_source_authority_hash": authority_record.get("observed_source_authority_hash"),
                }
                stop_active = (stop_reason, {
                    "kind": "authority",
                    "identity": authority_record["identity"],
                    "record_sha256": stored.sha256,
                    "extra": extra,
                })
    if stop_active is not None and stop_active[1]["kind"] == "authority":
        trigger = stop_active[1]
        authority_stop_info = {
            "reason_code": stop_active[0],
            "identity": trigger["identity"],
            "authority_check_record_sha256": trigger["record_sha256"],
            "affected_case_id": trigger.get("post_case_case_id"),
            "observed_at": reference_time.isoformat().replace("+00:00", "Z"),
        }

    if abort_info is not None:
        stop_reason, detail = abort_info
        return _fail_campaign(
            stop_reason=stop_reason, detail=detail,
            ledger_status="ABORTED",
            lifecycle=lifecycle,
            records=records,
            preflight=verdict,
            completed=counts["completed_case_count"], blocked=counts["blocked_case_count"], unstarted=counts["unstarted_case_count"],
            counts=counts,
            timing=(clock() - start) if clock is not None else 0.0,
            authority_stop_info=authority_stop_info,
            authority_invalidated_cases=authority_invalidated_cases,
        )

    ledger_status = "PARTIAL" if stop_active is not None else "COMPLETED"
    stop_reason = stop_active[0] if stop_active is not None else None
    if stop_active is not None and stop_active[1].get("kind") == "authority":
        detail = f"authority drift detected: {stop_reason}; the campaign cannot report a completed outcome"
    else:
        detail = f"campaign stopped before subsequent cases: {stop_reason}" if stop_active else "all six frozen cases completed"
    return _fail_campaign(
        stop_reason=stop_reason, detail=detail,
        ledger_status=ledger_status,
        lifecycle=lifecycle,
        records=records,
        preflight=verdict,
        completed=counts["completed_case_count"], blocked=counts["blocked_case_count"], unstarted=counts["unstarted_case_count"],
        counts=counts,
        timing=(clock() - start) if clock is not None else 0.0,
        authority_stop_info=authority_stop_info,
        authority_invalidated_cases=authority_invalidated_cases,
    )


def _drift_outcome(
    case: Mapping[str, Any],
    run_id: str,
    route_observation: Mapping[str, Any],
    attempts: int,
    drift: RouteDriftError,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    reference = f"{run_id}:route-drift:{drift.category}"
    entry = next(item for item in manifest["inventory"] if item["task_id"] == case["task_id"])
    return {
        "terminal_status": "INFRASTRUCTURE_ERROR",
        "terminal_reason_code": "TRANSPORT_EVIDENCE_LOSS",
        "termination_reason": f"route drift after preflight: {drift.category}",
        "logical_model_calls": 1,
        "provider_process_attempts": max(1, attempts),
        "retries": 0,
        "valid_directives": 0,
        "malformed_directive_rejections": 0,
        "bounded_directive_feedback_events": 0,
        "baseline_reproduction": False,
        "controller_states_visited": [],
        "hypotheses_created": 0,
        "pdb_gate_decisions": [],
        "pdb_counts": dict(ZERO_PDB_COUNTS),
        "pdb_sessions_started": 0,
        "successful_pdb_observations": 0,
        "failed_pdb_observations": 0,
        "verifier_runs": 0,
        "patch_submissions": 0,
        "independent_verifier_result": {"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": None,
            "timed_out": False, "provider_error_category": None,
            "provider_completed_response": False, "evidence_reference": reference,
        },
        "blocked_evidence": {"block_kind": "none", "reason_code": "NONE", "confirmed": False, "evidence_reference": reference},
        "infrastructure_evidence": {
            "stage": "provider_transport", "reason_code": "TRANSPORT_EVIDENCE_LOSS",
            "confirmed_failure": True, "classification": "PROVIDER_TRANSPORT",
            "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "provider_attempt_index": max(1, attempts), "prior_lifecycle_completed": False,
            "source_mutation_observed": False, "expected_source_hash": None,
            "evidence_reference": f"{reference}:{drift.detail}",
        },
        "preflight_failure_evidence": _default_preflight_failure_evidence(),
        "campaign_stop_evidence": _default_campaign_stop_evidence(),
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
        "provider_reported_cost": route_observation.get("provider_reported_cost", 0),
        "wall_clock_duration_seconds": 0,
        "public_evidence_bytes": 0,
        "canonical_source_restoration": True,
        "owned_workspace_cleanup": True,
        "evidence_consistency": True,
        "public_request_hash": "b" * 64,
        "source_hash": entry["source_sha256"],
        "candidate_hash": None,
        "repair_outcome": "NO_CANDIDATE",
        "resource_ids": {},
    }


def _cost_summary(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    costs = [float(r["provider_reported_cost"]) for r in records]
    tokens = [int(r["prompt_tokens"]) + int(r["completion_tokens"]) for r in records]
    attempts = [int(r["provider_process_attempts"]) for r in records]
    if not records or all(cost == 0 and token == 0 and attempt == 0 for cost, token, attempt in zip(costs, tokens, attempts)):
        classification = "ABSENT"
    elif any(cost > 0 for cost in costs):
        classification = "REPORTED"
    elif any(token > 0 for token in tokens):
        classification = "SUBSCRIPTION_COVERED"
    else:
        classification = "UNKNOWN"
    return {
        "classification": classification,
        "total_provider_reported_cost": None if classification == "ABSENT" else round(sum(costs), 6),
        "provider_reported": bool(records and any(cost > 0 for cost in costs)),
        "note": (
            "provider-reported monetary cost is preserved truthfully and is never forced to zero by "
            "subscription access; REPORTED = explicit monetary cost, SUBSCRIPTION_COVERED = zero reported "
            "cost with reported token usage, UNKNOWN = zero reported cost without token usage, ABSENT = no cases"
        ),
    }


def validate_campaign_record(record: Mapping[str, Any], manifest: Mapping[str, Any]) -> bool:
    """Runner-level campaign consistency validator.

    Verifies status, lifecycle/count reconciliation against the frozen
    six-case contract, case-record hash binding, provider-call proof
    consistency, and the embedded terminal ledger snapshot.  Raises
    :class:`LiveRunnerError` on any inconsistency.
    """
    if not isinstance(record, Mapping):
        raise LiveRunnerError("campaign record must be an object")
    try:
        _assert_finite_json(record)
    except NonFiniteValueError as exc:
        raise LiveRunnerError(f"campaign record carries non-finite numeric evidence: {exc}") from exc
    if record.get("schema_version") != RUNNER_SCHEMA_VERSION or record.get("record_kind") != "campaign":
        raise LiveRunnerError("campaign record schema identity is invalid")
    if record.get("campaign_id") != manifest["campaign_id"] or record.get("campaign_manifest_hash") != pilot.manifest_hash(manifest):
        raise LiveRunnerError("campaign record identity does not match the frozen manifest")
    status = record.get("status")
    if status not in CAMPAIGN_TERMINAL_STATUSES:
        raise LiveRunnerError(f"campaign status {status!r} is not frozen")
    frozen_ids = [case["case_id"] for case in manifest["case_order"]]
    if record.get("frozen_case_order") != frozen_ids:
        raise LiveRunnerError("campaign frozen case order mismatch")
    lifecycle = record.get("case_lifecycle_states")
    if not isinstance(lifecycle, Mapping) or set(lifecycle) != set(frozen_ids):
        raise LiveRunnerError("campaign lifecycle states do not cover the frozen cases exactly")
    for value in lifecycle.values():
        if value not in CASE_LIFECYCLE_STATES:
            raise LiveRunnerError(f"campaign lifecycle state {value!r} is unknown")
    counts = record.get("counts")
    if not isinstance(counts, Mapping):
        raise LiveRunnerError("campaign counts are missing")
    completed = int(counts.get("completed_case_count", 0))
    blocked = int(counts.get("blocked_case_count", 0))
    aborted = int(counts.get("aborted_case_count", 0))
    invalidated = int(counts.get("invalidated_case_count", 0))
    unstarted = int(counts.get("unstarted_case_count", 0))
    if completed + blocked + aborted + invalidated + unstarted != 6:
        raise LiveRunnerError(f"campaign lifecycle counts do not reconcile with the frozen six cases: completed={completed} blocked={blocked} aborted={aborted} invalidated={invalidated} unstarted={unstarted}")
    actual_completed = sum(1 for value in lifecycle.values() if value == "completed")
    actual_blocked = sum(1 for value in lifecycle.values() if value in {"blocked", "pre-provider-rejected"})
    actual_aborted = sum(1 for value in lifecycle.values() if value == "aborted")
    actual_invalidated = sum(1 for value in lifecycle.values() if value == "authority-invalidated")
    actual_unstarted = sum(1 for value in lifecycle.values() if value == "unstarted")
    if (completed, blocked, aborted, invalidated, unstarted) != (actual_completed, actual_blocked, actual_aborted, actual_invalidated, actual_unstarted):
        raise LiveRunnerError("campaign counts disagree with lifecycle states")
    invalidated_cases = record.get("authority_invalidated_cases")
    if not isinstance(invalidated_cases, list):
        raise LiveRunnerError("campaign authority-invalidated cases must be a list")
    invalidated_ids = {entry.get("case_id") for entry in invalidated_cases}
    lifecycle_invalidated_ids = {case_id for case_id, state in lifecycle.items() if state == "authority-invalidated"}
    if invalidated_ids != lifecycle_invalidated_ids:
        raise LiveRunnerError("campaign authority-invalidated case inventory disagrees with lifecycle states")
    for entry in invalidated_cases:
        if not isinstance(entry, Mapping):
            raise LiveRunnerError("campaign authority-invalidated case entry must be an object")
        for field in ("case_id", "original_raw_terminal_outcome", "original_terminal_reason_code",
                      "authority_failure_reason", "authority_check_record_sha256",
                      "provider_contact_occurred", "excluded_from_evaluation", "observed_at"):
            if field not in entry:
                raise LiveRunnerError(f"campaign authority-invalidated case entry is missing {field}")
        if entry.get("excluded_from_evaluation") is not True:
            raise LiveRunnerError("an authority-invalidated case must be excluded from evaluation")
    cases = record.get("cases")
    if not isinstance(cases, list) or len(cases) != completed + blocked + invalidated:
        raise LiveRunnerError("campaign case records do not match completed+blocked+invalidated counts")
    seen: set[str] = set()
    for entry in cases:
        case_id = entry.get("case_id")
        if case_id not in frozen_ids or case_id in seen:
            raise LiveRunnerError("campaign case records duplicate or unfrozen case IDs")
        seen.add(case_id)
        body = {key: value for key, value in entry.items() if key != "record_sha256"}
        if pilot.result_sha256(body) != entry.get("record_sha256"):
            raise LiveRunnerError(f"campaign case record hash binding mismatch: {case_id}")
    proof = record.get("provider_call_proof")
    if not isinstance(proof, Mapping):
        raise LiveRunnerError("campaign provider-call proof is missing")
    if int(proof.get("logical_requests", 0)) != int(proof.get("process_launches", 0)):
        raise LiveRunnerError("campaign provider-call proof is inconsistent")
    if int(proof.get("process_launches", 0)) != int(counts.get("provider_process_attempts", 0)):
        raise LiveRunnerError("campaign provider-call proof disagrees with reported provider attempts")
    if status == "REJECTED":
        if record.get("ledger") is not None or completed or blocked or aborted:
            raise LiveRunnerError("rejected campaign has attempt evidence")
    elif status == "BLOCKED":
        if record.get("ledger") is not None or completed or aborted or blocked > 1 or unstarted != 6 - blocked:
            raise LiveRunnerError("blocked campaign state is inconsistent")
    else:
        ledger = record.get("ledger")
        if not isinstance(ledger, Mapping) or ledger.get("status") != status:
            raise LiveRunnerError("campaign embedded terminal ledger status does not match the campaign status")
        if ledger.get("attempt_identity") != record.get("campaign_attempt_identity"):
            raise LiveRunnerError("campaign embedded ledger identity does not match the attempt identity")
        if ledger.get("execution_commit") != record.get("execution_commit", {}).get("authorization_bound_execution_commit"):
            raise LiveRunnerError("campaign embedded ledger execution commit does not match the verified execution commit")
    return True


def verify_attempt_package(output_root: str | Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Automated on-disk package consistency check.

    Requires the crash-safe terminal commitment: a standalone ``campaign.json``
    is never an accepted terminal campaign merely because its internal status
    says COMPLETED/PARTIAL/ABORTED.  The create-once ``terminal-commit.json``
    must exist and bind the campaign-attempt identity, authorization hash,
    execution commit, intended terminal status, the SHA-256 of the actual
    ``campaign.json``, the SHA-256 of the exact terminal ledger entry, the
    frozen manifest hash, and the case-record inventory.  Interrupted
    FINALIZING/PREPARED states (commitment absent or mismatched) are rejected.
    Also verifies that the embedded terminal ledger snapshot matches the
    ledger file, that every case file on disk matches its recorded SHA-256,
    and that the provider-call proof is consistent.  Returns the verification
    result; raises :class:`LiveRunnerError` on inconsistency.
    """
    root = Path(output_root)
    campaign_path = root / "campaign.json"
    ledger_path = root / "ledger.json"
    commit_path = root / "terminal-commit.json"
    errors: list[str] = []
    result: dict[str, Any] = {"output_root": str(root.resolve()), "campaign_status": None}
    try:
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveRunnerError(f"attempt package has no readable campaign.json: {exc}") from exc
    result["campaign_status"] = campaign.get("status")
    try:
        _assert_finite_json(campaign)
    except NonFiniteValueError as exc:
        raise LiveRunnerError(f"attempt package campaign.json carries non-finite numeric evidence: {exc}") from exc
    if campaign.get("commit_state") != "PREPARED" or campaign.get("terminal_commit") is not None:
        raise LiveRunnerError("attempt package campaign.json has an unexpected commit-state marker")
    try:
        validate_campaign_record(campaign, manifest)
    except LiveRunnerError as exc:
        errors.append(f"campaign record consistency: {exc}")

    # Crash-safe terminal commitment (mandatory for any terminal campaign).
    if not commit_path.is_file():
        raise LiveRunnerError(
            "TERMINAL_COMMIT_MISSING: the attempt package has no terminal commitment; "
            "the campaign payload is uncommitted/interrupted and is not an accepted terminal campaign"
        )
    try:
        commitment = json.loads(commit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveRunnerError(f"attempt package has no readable terminal-commit.json: {exc}") from exc
    try:
        _assert_finite_json(commitment)
    except NonFiniteValueError as exc:
        raise LiveRunnerError(f"attempt package terminal commitment carries non-finite numeric evidence: {exc}") from exc
    if commitment.get("commit_version") != TERMINAL_COMMIT_VERSION:
        raise LiveRunnerError("attempt package terminal commitment has an unsupported commit version")
    if commitment.get("attempt_identity") != campaign.get("campaign_attempt_identity"):
        raise LiveRunnerError("attempt package terminal commitment identity mismatch")
    authorization_hash = campaign.get("authorization_identity", {}).get("authorization_hash")
    if commitment.get("authorization_hash") != authorization_hash:
        raise LiveRunnerError("attempt package terminal commitment authorization hash mismatch")
    execution_commit = campaign.get("execution_commit", {}).get("authorization_bound_execution_commit")
    if commitment.get("execution_commit") != execution_commit:
        raise LiveRunnerError("attempt package terminal commitment execution commit mismatch")
    if commitment.get("intended_terminal_status") != campaign.get("status"):
        raise LiveRunnerError("attempt package terminal commitment status mismatch")
    if commitment.get("manifest_hash") != pilot.manifest_hash(manifest):
        raise LiveRunnerError("attempt package terminal commitment manifest hash mismatch")
    actual_campaign_sha = hashlib.sha256(campaign_path.read_bytes()).hexdigest()
    if commitment.get("campaign_json_sha256") != actual_campaign_sha:
        raise LiveRunnerError("attempt package terminal commitment campaign hash mismatch")

    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveRunnerError(f"attempt package has no readable ledger.json: {exc}") from exc
    embedded = campaign.get("ledger")
    if not isinstance(embedded, Mapping):
        raise LiveRunnerError("attempt package campaign record embeds no ledger snapshot")
    identity = embedded.get("attempt_identity")
    entry = next((value for value in ledger.values() if isinstance(value, Mapping) and value.get("attempt_identity") == identity), None)
    if entry is None:
        errors.append("embedded ledger identity is absent from the ledger file")
    else:
        if entry.get("status") != embedded.get("status"):
            errors.append(f"ledger-file status {entry.get('status')!r} != embedded ledger status {embedded.get('status')!r}")
        elif entry.get("status") != campaign.get("status"):
            errors.append(f"ledger-file status {entry.get('status')!r} != campaign status {campaign.get('status')!r}")
        if entry.get("execution_commit") != embedded.get("execution_commit"):
            errors.append("ledger-file execution commit differs from the embedded snapshot")
        actual_entry_sha = sha256_text(canonical_json(entry))
        if commitment.get("ledger_entry_sha256") != actual_entry_sha:
            raise LiveRunnerError("attempt package terminal commitment ledger-entry hash mismatch")
    expected_inventory = [
        {"case_id": entry["case_id"], "order_index": entry["order_index"], "record_sha256": entry["record_sha256"]}
        for entry in campaign.get("cases", [])
    ]
    if commitment.get("case_inventory") != expected_inventory:
        raise LiveRunnerError("attempt package terminal commitment case inventory mismatch")
    case_dir = root / "cases"
    for entry in campaign.get("cases", []):
        case_path = case_dir / f"case-{int(entry['order_index']):02d}-{entry['case_id'].replace(':', '__')}.json"
        if not case_path.is_file():
            errors.append(f"case file missing: {case_path.name}")
            continue
        try:
            on_disk = json.loads(case_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append(f"case file unreadable: {case_path.name}")
            continue
        if pilot.result_sha256(on_disk) != entry.get("record_sha256"):
            errors.append(f"case file hash mismatch: {case_path.name}")
    result["case_files_on_disk"] = sum(1 for _ in (case_dir.glob("case-*.json") if case_dir.is_dir() else ()))
    result["case_records_referenced"] = len(campaign.get("cases", []))
    result["terminal_commit"] = "PRESENT"
    result["consistent"] = not errors
    result["errors"] = errors
    if errors:
        raise LiveRunnerError("attempt package is inconsistent: " + "; ".join(errors))
    return result


def run_preflight_only(
    manifest: Mapping[str, Any],
    *,
    authorization: Mapping[str, Any],
    output_root: str | Path,
    route_evidence_provider: Callable[[], Mapping[str, Any]] | None = None,
    ledger_path: str | Path | None = None,
    private_markers: tuple[str, ...] = (),
    clock: Callable[[], float] | None = None,
    git_state_provider: Callable[[str], GitRepositoryState] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Preflight-only mode: completes every pre-provider gate and stops.

    Never claims an attempt, never creates a transport, never launches a
    provider process, and never calls a model.  The result is written to
    ``preflight.json`` (or to the non-authoritative rejections directory for a
    blocked/rejected preflight) and never touches ``campaign.json``, the
    attempt owner record, or the attempt ledger.
    """
    pilot.validate_manifest(manifest)
    output = Path(output_root)
    reference_time = now if now is not None else _utc_now(clock)

    def _reject(stop_reason: str, detail: str | None, *, execution_evidence: Mapping[str, Any] | None = None, lifecycle_state: str = "authorization-rejected") -> dict[str, Any]:
        record = _campaign_rejection_record(
            manifest, authorization, output, stop_reason=stop_reason, detail=detail,
            execution_evidence=execution_evidence, lifecycle_state=lifecycle_state,
        )
        _write_rejection_record(output, record)
        return record

    auth_failure = authorization_failure(authorization, manifest, expected_output_root=output, now=reference_time)
    if auth_failure is not None:
        return _reject(auth_failure, "authorization artifact rejected before any provider contact")
    identity = attempt_identity_for(authorization)
    execution_commit = authorization["accepted_campaign_commit"]
    try:
        execution_evidence = verify_execution_repository_state(authorization, git_state_provider, now=reference_time)
    except RepositoryStateError as exc:
        return _reject(exc.category, exc.detail, execution_evidence={
            "authorization_bound_execution_commit": execution_commit,
            "detail": exc.detail,
            "checked_at": reference_time.isoformat().replace("+00:00", "Z"),
        })
    except LiveRunnerError as exc:
        return _reject("REPOSITORY_STATE_UNVERIFIABLE", str(exc))
    try:
        verdict = run_route_preflight(
            manifest, authorization, route_evidence_provider,
            now=reference_time, attempt_identity=identity, execution_commit=execution_commit,
        )
    except RouteEvidenceInvalid as exc:
        return _reject(f"ROUTE_EVIDENCE_INVALID:{exc.reason}", exc.detail, execution_evidence=execution_evidence, lifecycle_state="unstarted")
    except LiveRunnerError as exc:
        return _reject("ROUTE_EVIDENCE_NOT_CONFIGURED", str(exc), execution_evidence=execution_evidence, lifecycle_state="unstarted")

    lifecycle = {
        case["case_id"]: ("pre-provider-rejected" if (not verdict.passed and case["order_index"] == 1) else "unstarted")
        for case in manifest["case_order"]
    }
    plan = build_plan(manifest, authorization, output)
    if not verdict.passed:
        record = _campaign_rejection_record(manifest, authorization, output, stop_reason=f"BLOCKED_PRE_PROVIDER:{verdict.failure_category}", detail=verdict.detail, execution_evidence=execution_evidence, lifecycle_state="unstarted")
        record.update({
            "status": "BLOCKED",
            "preflight": verdict.to_mapping(),
            "case_lifecycle_states": lifecycle,
            "provider_call_proof": {"transports_created": 0, "process_launches": 0, "logical_requests": 0},
            "counts": {
                "logical_model_calls": 0, "provider_process_attempts": 0, "transport_retries": 0,
                "accepted_directives": 0, "malformed_directive_rejections": 0,
                "completed_case_count": 0, "blocked_case_count": 1, "aborted_case_count": 0,
                "invalidated_case_count": 0, "unstarted_case_count": 5,
            },
        })
        _write_rejection_record(output, record)
        return record
    record = _campaign_rejection_record(manifest, authorization, output, stop_reason=None, detail=None, execution_evidence=execution_evidence, lifecycle_state="unstarted")
    record.update({
        "status": "COMPLETED",
        "stop_reason": None,
        "stop_detail": "preflight-only mode completed every pre-provider gate with zero provider activity",
        "preflight": verdict.to_mapping(),
        "case_lifecycle_states": lifecycle,
        "provider_call_proof": {"transports_created": 0, "process_launches": 0, "logical_requests": 0},
        "plan": plan,
    })
    # Preflight-only output is non-authoritative and stored at the parent level
    # so the authoritative attempt root stays absent/empty for a future live
    # attempt.
    try:
        atomic_create_json(_rejection_dir(output) / f"preflight-{identity}.json", record)
    except OutputIntegrityError:
        pass
    return record


# ---- CLI ----------------------------------------------------------------------


def _cli_plan_text(plan: Mapping[str, Any]) -> str:
    lines = [
        "QuixBugs paired-pilot v2 live runner - pre-provider plan",
        f"campaign: {plan['campaign_id']} (version {plan['campaign_version']})",
        f"manifest hash: {plan['campaign_manifest_hash']}",
        f"accepted baseline: {plan['accepted_baseline']}",
        f"authorization: {plan['authorization']['operator_authorization_id']} ({plan['authorization']['campaign_attempt_identity']})",
        f"execution commit (must equal actual Git HEAD): {plan['execution_commit_binding']['authorization_bound_execution_commit']}",
        f"route: {plan['route']['provider']} / {plan['route']['model']} / {plan['route']['variant']} / protocol {plan['route']['protocol']}",
        f"runtime model identity: {plan['route']['expected_runtime_model_id']}",
        f"opencode version: {plan['route']['expected_opencode_version']}",
        f"catalog fingerprint: {plan['route']['expected_catalog_fingerprint']}",
        f"billing route: {plan['route']['expected_billing_route']} (account status {plan['route']['expected_account_status']})",
        "frozen case order:",
    ]
    for index, case_id in enumerate(plan["frozen_case_order"], 1):
        lines.append(f"  {index}. {case_id}")
    lines.append("provider contact guarantee: " + plan["provider_contact_guarantee"])
    return "\n".join(lines)


def run_live_entry(
    manifest: Mapping[str, Any],
    authorization_path: str | Path | None,
    output_root: str | Path | None,
    *,
    preflight_only: bool = False,
    route_evidence_json: str | Path | None = None,
    git_state_provider: Callable[[str], GitRepositoryState] | None = None,
) -> dict[str, Any]:
    """CLI entry: loads the artifacts, prints the pre-provider plan, and runs
    the preflight-only or full fail-closed campaign path."""
    if authorization_path is None or not Path(authorization_path).is_file():
        raise LiveRunnerError("live mode requires a separate explicit authorization artifact")
    if output_root is None:
        raise LiveRunnerError("live mode requires an explicit output/attempt location")
    authorization = load_authorization_artifact(authorization_path)
    output = Path(output_root)
    validate_authorization_artifact(authorization, manifest, expected_output_root=output)
    plan = build_plan(manifest, authorization, output)
    print(_cli_plan_text(plan))

    provider: Callable[[], Mapping[str, Any]] | None = None
    if route_evidence_json is not None:
        path = Path(route_evidence_json)
        if not path.is_file():
            raise LiveRunnerError(f"route-evidence file is missing: {path}")
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveRunnerError(f"route-evidence file is invalid: {exc}") from exc
        if not isinstance(evidence, Mapping):
            raise LiveRunnerError("route-evidence file must contain a JSON object")
        provider = lambda: evidence

    if preflight_only:
        return run_preflight_only(
            manifest, authorization=authorization, output_root=output,
            route_evidence_provider=provider, git_state_provider=git_state_provider,
        )
    return run_campaign(
        manifest, authorization=authorization, output_root=output,
        route_evidence_provider=provider,
        transport_factory=None, case_runner=None,
        git_state_provider=git_state_provider,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed QuixBugs paired-pilot v2 live runner (runner infrastructure only; no provider contact in this task)")
    parser.add_argument("mode", nargs="?", choices=("preflight", "live", "template"), default="preflight")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH_V2)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--route-evidence-json", type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.mode == "template":
            if args.output is None:
                print("BLOCKED: template mode requires --output", file=sys.stderr)
                return 2
            target = write_authorization_template(args.output)
            print(json.dumps({"template_written": str(target), "authorizing": False}, indent=2, sort_keys=True))
            return 0
        manifest = pilot.load_manifest(args.manifest)
        pilot.validate_manifest(manifest)
        if manifest["campaign_id"] != CAMPAIGN_ID_V2:
            print("BLOCKED: the live runner is bound to the frozen v2 campaign only", file=sys.stderr)
            return 2
        if args.mode == "preflight" or args.preflight_only:
            record = run_live_entry(
                manifest, args.authorization, args.output,
                preflight_only=True, route_evidence_json=args.route_evidence_json,
            )
        else:
            record = run_live_entry(
                manifest, args.authorization, args.output,
                preflight_only=False, route_evidence_json=args.route_evidence_json,
            )
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0
    except LiveRunnerError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    except pilot.PilotError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "ACCEPTED_BASELINE",
    "ATTEMPT_IDENTITY_PATTERN",
    "AUTHORIZATION_FIELDS",
    "AUTHORIZATION_SCHEMA_VERSION",
    "CAMPAIGN_TERMINAL_STATUSES",
    "CLOCK_SKEW_ALLOWANCE_SECONDS",
    "LIVE_PROTOCOL_VERSION",
    "RAW_ROUTE_EVIDENCE_SCHEMA",
    "RUNNER_SCHEMA_VERSION",
    "AttemptLedger",
    "AttemptClaimError",
    "BudgetViolationError",
    "GitRepositoryState",
    "LiveRunnerError",
    "NonFiniteValueError",
    "OutputIntegrityError",
    "OutputRootOccupiedError",
    "OutputRootOwnedError",
    "PreflightVerdict",
    "ProviderCallCounter",
    "RepositoryStateError",
    "RouteDriftError",
    "RouteEvidenceInvalid",
    "RouteEvidenceUnavailable",
    "SameAttemptClaimError",
    "SanitizationViolationError",
    "StaticPolicyPdbViolation",
    "append_private_evidence",
    "atomic_create_json",
    "authorization_failure",
    "authorization_hash",
    "authorization_template",
    "build_campaign_stop_evidence",
    "build_campaign_stop_record",
    "build_plan",
    "build_preprovider_block_record",
    "claim_output_root",
    "deterministic_resource_ids",
    "deterministic_run_id",
    "deterministic_session_id",
    "deterministic_workspace_id",
    "enforce_case_budgets",
    "load_authorization_artifact",
    "main",
    "materialize_case_record",
    "real_git_state",
    "run_campaign",
    "run_live_entry",
    "run_preflight_only",
    "run_route_preflight",
    "sanitize_public_payload",
    "validate_authorization_artifact",
    "validate_campaign_record",
    "validate_case_outcome",
    "verify_attempt_package",
    "verify_execution_repository_state",
    "write_authorization_template",
]


if __name__ == "__main__":
    raise SystemExit(main())
