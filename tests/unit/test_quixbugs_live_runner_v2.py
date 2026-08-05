from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import quixbugs_live_runner_v2 as runner
import quixbugs_paired_pilot as pilot


# ---- fixtures and helpers ------------------------------------------------------


@pytest.fixture
def manifest():
    return pilot.load_manifest(pilot.MANIFEST_PATH_V2)


@pytest.fixture
def v1_manifest():
    return pilot.load_manifest(pilot.MANIFEST_PATH)


@pytest.fixture
def auth(manifest, tmp_path):
    return _valid_authorization(manifest, tmp_path / "attempt-out")


@pytest.fixture
def git_state_provider():
    return lambda commit: _clean_git_state(commit)


def _clean_git_state(commit):
    return runner.GitRepositoryState(
        head=commit,
        execution_commit_exists=True,
        execution_commit_descends_from_baseline=True,
        tracked_working_tree_clean=True,
        git_index_clean=True,
    )


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _valid_authorization(manifest, output_root, **overrides):
    value = {
        "schema_version": runner.AUTHORIZATION_SCHEMA_VERSION,
        "template": False,
        "authorize_live": True,
        "campaign_id": "quixbugs-paired-pilot-v2",
        "campaign_version": 2,
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": runner.ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "qualification_contract_hash": manifest["qualification_contract_hash"],
        "accepted_campaign_commit": runner.ACCEPTED_BASELINE,
        "permitted_case_ids": [case["case_id"] for case in manifest["case_order"]],
        "provider": "OpenCode Go",
        "model": "deepseek-v4-flash",
        "variant": "max",
        "protocol": "1.3",
        "expected_opencode_version": "1.0.0",
        "expected_catalog_fingerprint": "c" * 64,
        "expected_runtime_model_id": "opencode-go/deepseek-v4-flash",
        "subscription_route_required": True,
        "expected_billing_route": "SUBSCRIPTION",
        "subscription_entitlement_confirmed": True,
        "subscription_account_observation": {"entitlement_confirmed": True, "evidence_reference": "test-account-observation-001"},
        "expected_account_status": "ACTIVE",
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
        "operator_authorization_id": "test-operator-001",
        "authorization_created_at": "2026-08-02T00:00:00Z",
        "authorization_valid_until": None,
        "output_root": str(Path(output_root).resolve()),
        "campaign_attempt_identity": "quixbugs-paired-pilot-v2-attempt-" + "d" * 64,
        "single_frozen_six_case_campaign_confirmation": True,
    }
    value.update(overrides)
    return value


def _route_evidence(manifest, **overrides):
    value = {
        "provider": "OpenCode Go",
        "model": "deepseek-v4-flash",
        "variant": "max",
        "protocol": "1.3",
        "opencode_version": "1.0.0",
        "catalog_fingerprint": "c" * 64,
        "runtime_model_id": "opencode-go/deepseek-v4-flash",
        "billing_route": "SUBSCRIPTION",
        "subscription_entitlement_confirmed": True,
        "account_status": "ACTIVE",
        "active_model_status": "ACTIVE",
        "variant_available": True,
        "input_price": 0.1,
        "output_price": 0.2,
        "provider_reported_cost": 0.0042,
        "paid_fallback_used": False,
        "alternate_provider_used": False,
        "ollama_used": False,
        "zen_used": False,
        "free_tier_used": False,
        "metered_fallback_used": False,
        "paid_overage_used": False,
        "per_call_billing_used": False,
        "model_substitution_observed": False,
        "observed_at": _now_iso(),
    }
    value.update(overrides)
    return value


def _task_source_hash(manifest, case):
    return next(item["source_sha256"] for item in manifest["inventory"] if item["task_id"] == case["task_id"])


def _base_outcome(manifest, case, route_observation):
    return {
        "terminal_status": "UNRESOLVED",
        "terminal_reason_code": "UNRESOLVED_COMPLETED",
        "termination_reason": "synthetic completed case",
        "logical_model_calls": 1,
        "provider_process_attempts": 1,
        "retries": 0,
        "valid_directives": 1,
        "malformed_directive_rejections": 0,
        "bounded_directive_feedback_events": 0,
        "baseline_reproduction": True,
        "controller_states_visited": ["REPRODUCE", "UNDERSTAND"],
        "hypotheses_created": 0,
        "pdb_gate_decisions": [],
        "pdb_counts": dict(runner.ZERO_PDB_COUNTS),
        "pdb_sessions_started": 0,
        "successful_pdb_observations": 0,
        "failed_pdb_observations": 0,
        "verifier_runs": 0,
        "patch_submissions": 0,
        "independent_verifier_result": {"status": "COMPLETED", "outcome": "NO_OP", "lifecycle_succeeded": True},
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "COMPLETED_RESPONSE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "synthetic",
        },
        "blocked_evidence": {"block_kind": "none", "reason_code": "NONE", "confirmed": False, "evidence_reference": "synthetic"},
        "infrastructure_evidence": {
            "stage": "none", "reason_code": "NONE", "confirmed_failure": False, "classification": "NONE",
            "terminal_classification": "NOT_APPLICABLE", "provider_attempt_index": None,
            "prior_lifecycle_completed": False, "source_mutation_observed": False,
            "expected_source_hash": None, "evidence_reference": "synthetic",
        },
        "preflight_failure_evidence": {field: None for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS},
        "campaign_stop_evidence": {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS},
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "reasoning_tokens": 0,
        "provider_reported_cost": route_observation["provider_reported_cost"],
        "wall_clock_duration_seconds": 1.0,
        "public_evidence_bytes": 100,
        "canonical_source_restoration": True,
        "owned_workspace_cleanup": True,
        "evidence_consistency": True,
        "public_request_hash": "b" * 64,
        "source_hash": _task_source_hash(manifest, case),
        "candidate_hash": None,
        "repair_outcome": "NO_CANDIDATE",
        "resource_ids": {},
        "interrupted": False,
    }


def _completed_outcome(manifest, case, route_observation, **overrides):
    outcome = _base_outcome(manifest, case, route_observation)
    outcome.update(overrides)
    return outcome


def _no_contact_outcome(manifest, case, route_observation, **overrides):
    """A raw outcome that is already the frozen no-contact representation on
    every accounting and terminal/evidence field, except for the overflowing
    ``public_evidence_bytes`` counter and the termination detail/evidence
    references that are attached during terminalization.  Single-field
    contradictions are applied via ``overrides`` to prove they abort."""
    outcome = _base_outcome(manifest, case, route_observation)
    outcome.update({
        "terminal_status": "INFRASTRUCTURE_ERROR",
        "terminal_reason_code": "WORKSPACE_FAILURE",
        "termination_reason": "synthetic no-contact harness failure",
        "logical_model_calls": 0,
        "provider_process_attempts": 0,
        "valid_directives": 0,
        "baseline_reproduction": False,
        "controller_states_visited": [],
        "independent_verifier_result": {"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": None, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": False, "evidence_reference": "synthetic-no-contact",
        },
        "blocked_evidence": {"block_kind": "none", "reason_code": "NONE", "confirmed": False, "evidence_reference": "synthetic-no-contact"},
        "infrastructure_evidence": {
            "stage": "pre_provider", "reason_code": "WORKSPACE_FAILURE", "confirmed_failure": True,
            "classification": "PRE_PROVIDER", "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "provider_attempt_index": None, "prior_lifecycle_completed": False,
            "source_mutation_observed": False, "expected_source_hash": None,
            "evidence_reference": "synthetic-no-contact",
        },
        "preflight_failure_evidence": {field: None for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS},
        "campaign_stop_evidence": dict(
            {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS}, confirmed=False
        ),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "provider_reported_cost": 0,
        "wall_clock_duration_seconds": 0.0,
        "public_request_hash": None,
        "source_hash": None,
        "candidate_hash": None,
        "public_evidence_bytes": 20001,
    })
    outcome.update(overrides)
    return outcome


def _resolved_outcome(manifest, case, route_observation, **overrides):
    outcome = _completed_outcome(manifest, case, route_observation)
    outcome.update({
        "terminal_status": "RESOLVED",
        "terminal_reason_code": "RESOLVED_COMPLETED",
        "termination_reason": "synthetic resolved case",
        "patch_submissions": 1,
        "verifier_runs": 1,
        "independent_verifier_result": {"status": "COMPLETED", "outcome": "RESOLVED", "lifecycle_succeeded": True},
        "candidate_hash": "e" * 64,
        "repair_outcome": "RESOLVED",
    })
    outcome.update(overrides)
    return outcome


def _invalid_model_outcome(manifest, case, route_observation, **overrides):
    outcome = _base_outcome(manifest, case, route_observation)
    outcome.update({
        "terminal_status": "INVALID_MODEL_RESPONSE",
        "terminal_reason_code": "MALFORMED_RESPONSE",
        "termination_reason": "synthetic malformed responses exhausted",
        "logical_model_calls": 1,
        "provider_process_attempts": 3,
        "retries": 2,
        "valid_directives": 0,
        "malformed_directive_rejections": 1,
        "bounded_directive_feedback_events": 1,
        "transport_evidence": {"completed_response": True, "malformed_response": True, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "MALFORMED_RESPONSE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "synthetic-malformed",
        },
    })
    outcome.update(overrides)
    return outcome


def _provider_error_outcome(manifest, case, route_observation, **overrides):
    outcome = _base_outcome(manifest, case, route_observation)
    outcome.update({
        "terminal_status": "PROVIDER_ERROR",
        "terminal_reason_code": "PROVIDER_ERROR",
        "termination_reason": "synthetic provider error",
        "logical_model_calls": 1,
        "provider_process_attempts": 2,
        "retries": 1,
        "valid_directives": 0,
        "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": True, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "PROVIDER_ERROR", "process_exit_code": 1, "timed_out": False,
            "provider_error_category": "provider_internal", "provider_completed_response": False, "evidence_reference": "synthetic-provider-error",
        },
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "provider_reported_cost": 0,
        "baseline_reproduction": True,
    })
    if route_observation.get("provider_reported_cost") != 0:
        outcome["provider_reported_cost"] = route_observation["provider_reported_cost"]
    outcome.update(overrides)
    return outcome


def _pdb_not_reached_outcome(manifest, case, route_observation, **overrides):
    outcome = _base_outcome(manifest, case, route_observation)
    outcome.update({
        "terminal_status": "PDB_NOT_REACHED",
        "terminal_reason_code": "PDB_NOT_REACHED_NO_GATE",
        "termination_reason": "synthetic pdb not reached",
        "logical_model_calls": 2,
        "provider_process_attempts": 2,
        "valid_directives": 2,
    })
    outcome.update(overrides)
    return outcome


def _pdb_activity_outcome(manifest, case, route_observation, **overrides):
    outcome = _base_outcome(manifest, case, route_observation)
    outcome.update({
        "hypotheses_created": 1,
        "pdb_gate_decisions": [
            {"allowed": True, "reason": "uncertainty"},
            {"allowed": True, "reason": "uncertainty"},
            {"allowed": False, "reason": "budget"},
        ],
        "pdb_counts": {
            "total_gate_decisions": 3, "allowed_gate_openings": 2, "rejected_gate_decisions": 1,
            "sessions_started": 2, "successful_observations": 2, "failed_observations": 0,
        },
        "pdb_sessions_started": 2,
        "successful_pdb_observations": 2,
        "failed_pdb_observations": 0,
    })
    outcome.update(overrides)
    return outcome


def _cleanup_failure_outcome(manifest, case, route_observation, **overrides):
    outcome = _base_outcome(manifest, case, route_observation)
    outcome.update({
        "terminal_status": "INFRASTRUCTURE_ERROR",
        "terminal_reason_code": "CLEANUP_FAILURE",
        "termination_reason": "synthetic cleanup failure",
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "synthetic-cleanup",
        },
        "infrastructure_evidence": {
            "stage": "cleanup", "reason_code": "CLEANUP_FAILURE", "confirmed_failure": True,
            "classification": "CLEANUP", "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "provider_attempt_index": None, "prior_lifecycle_completed": True,
            "source_mutation_observed": False, "expected_source_hash": None,
            "evidence_reference": "synthetic-cleanup",
        },
        "owned_workspace_cleanup": False,
    })
    outcome.update(overrides)
    return outcome


def _source_mutation_outcome(manifest, case, route_observation, **overrides):
    outcome = _base_outcome(manifest, case, route_observation)
    expected = _task_source_hash(manifest, case)
    outcome.update({
        "terminal_status": "INFRASTRUCTURE_ERROR",
        "terminal_reason_code": "CLEANUP_FAILURE",
        "termination_reason": "synthetic source mutation during cleanup",
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "synthetic-source-mutation",
        },
        "infrastructure_evidence": {
            "stage": "cleanup", "reason_code": "CLEANUP_FAILURE", "confirmed_failure": True,
            "classification": "CLEANUP", "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "provider_attempt_index": None, "prior_lifecycle_completed": True,
            "source_mutation_observed": True, "expected_source_hash": expected,
            "evidence_reference": "synthetic-source-mutation",
        },
        "source_hash": "f" * 64,
        "canonical_source_restoration": False,
    })
    outcome.update(overrides)
    return outcome


def _verifier_failure_outcome(manifest, case, route_observation, **overrides):
    outcome = _base_outcome(manifest, case, route_observation)
    outcome.update({
        "terminal_status": "INFRASTRUCTURE_ERROR",
        "terminal_reason_code": "VERIFIER_INTEGRITY_FAILURE",
        "termination_reason": "synthetic verifier integrity failure",
        "verifier_runs": 1,
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "synthetic-verifier",
        },
        "infrastructure_evidence": {
            "stage": "verifier", "reason_code": "VERIFIER_INTEGRITY_FAILURE", "confirmed_failure": True,
            "classification": "VERIFIER", "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "provider_attempt_index": None, "prior_lifecycle_completed": True,
            "source_mutation_observed": False, "expected_source_hash": None,
            "evidence_reference": "synthetic-verifier",
        },
    })
    outcome.update(overrides)
    return outcome


class CountingSyntheticTransport:
    """A fresh provider-process transport that counts every request."""

    def __init__(self, case_id, *, drift=None):
        self.case_id = case_id
        self.drift = drift
        self.request_count = 0
        self.payloads = []

    def request(self, payload, timeout_seconds):
        self.request_count += 1
        self.payloads.append(payload)
        if self.drift is not None:
            raise runner.RouteDriftError(self.drift, f"synthetic route drift for {self.case_id}")
        return {"directive": {"kind": "stop", "reason": "synthetic"}, "usage": {"prompt_tokens": 12, "completion_tokens": 8}}


class ScriptedCaseRunner:
    """Deterministic synthetic case runner: plays the script per frozen order."""

    def __init__(self, entries):
        self.entries = list(entries)
        self.order_log = []
        self.session_boundaries = []
        self.transports = []

    def __call__(self, case, *, attempt_identity, run_id, session_id, transport, route_observation, budgets, clock):
        entry = self.entries[int(case["order_index"]) - 1]
        self.order_log.append(case["case_id"])
        self.session_boundaries.append((run_id, session_id))
        self.transports.append(transport)
        for _ in range(entry.get("provider_process_attempts", 0)):
            transport.request({"synthetic": True, "case_id": case["case_id"]}, 1.0)
        if entry.get("drift"):
            raise runner.RouteDriftError(entry["drift"], f"synthetic drift after preflight for {case['case_id']}")
        if entry.get("runner_raises"):
            raise entry["runner_raises"]
        return entry["outcome"]


class RecordingTransportFactory:
    def __init__(self, transports=None):
        self.transports = transports if transports is not None else {}
        self.created = []

    def __call__(self, case):
        case_id = case["case_id"]
        transport = self.transports.get(case_id, CountingSyntheticTransport(case_id))
        self.created.append(case_id)
        return transport


def _run_campaign(manifest, auth, tmp_path, *, route_evidence=None, runner_entries=None, git_state_provider=None, **kwargs):
    output = tmp_path / "attempt-out"
    factory = RecordingTransportFactory()
    entries = runner_entries if runner_entries is not None else [
        {"provider_process_attempts": 1, "outcome": _completed_outcome(manifest, case, _route_evidence(manifest))}
        for case in manifest["case_order"]
    ]
    case_runner = ScriptedCaseRunner(entries)
    provider = git_state_provider if git_state_provider is not None else (lambda commit: _clean_git_state(commit))
    record = runner.run_campaign(
        manifest,
        authorization=auth,
        output_root=output,
        route_evidence_provider=(lambda: route_evidence) if route_evidence is not None else (lambda: _route_evidence(manifest)),
        transport_factory=factory,
        case_runner=case_runner,
        git_state_provider=provider,
        **kwargs,
    )
    return record, factory, case_runner, output


# ---- authorization artifact contract ------------------------------------------


def test_valid_authorization_validates(manifest, auth):
    assert runner.authorization_failure(auth, manifest) is None
    assert runner.authorization_hash(auth) == runner.sha256_text(runner.canonical_json(auth))


def test_template_is_rejected_as_authorization(manifest):
    template = runner.authorization_template()
    assert runner.authorization_failure(template, manifest) == "TEMPLATE_IS_NOT_AUTHORIZATION"


def test_write_authorization_template_produces_rejected_artifact(manifest, tmp_path):
    target = tmp_path / "authorization.template.json"
    runner.write_authorization_template(target)
    assert target.is_file()
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["template"] is True
    assert artifact["authorize_live"] is False
    assert runner.authorization_failure(artifact, manifest) == "TEMPLATE_IS_NOT_AUTHORIZATION"


@pytest.mark.parametrize("field,value,expected", [
    ("schema_version", "wrong", "SCHEMA_VERSION_MISMATCH"),
    ("authorize_live", False, "AUTHORIZATION_FLAG_INVALID"),
    ("campaign_id", "quixbugs-paired-pilot-v1", "CAMPAIGN_IDENTITY_MISMATCH"),
    ("campaign_version", 1, "CAMPAIGN_IDENTITY_MISMATCH"),
    ("campaign_manifest_hash", "0" * 64, "MANIFEST_MISMATCH"),
    ("accepted_baseline", "0" * 40, "BASELINE_MISMATCH"),
    ("planning_baseline_commit", "0" * 40, "PLANNING_BASELINE_MISMATCH"),
    ("qualification_contract_hash", "0" * 64, "QUALIFICATION_CONTRACT_MISMATCH"),
    ("accepted_campaign_commit", "not-a-commit", "COMMIT_INVALID"),
    ("accepted_campaign_commit", "18e067f24c337e7215139373edc699a347cf2127", "COMMIT_INVALID"),
    ("provider", "OpenCode Zen", "ROUTE_MISMATCH"),
    ("model", "deepseek-v4-flash-free", "ROUTE_MISMATCH"),
    ("variant", "default", "ROUTE_MISMATCH"),
    ("protocol", "1.2", "ROUTE_MISMATCH"),
    ("expected_opencode_version", "", "VERSION_BINDING_MISSING"),
    ("expected_catalog_fingerprint", "short", "CATALOG_BINDING_MISSING"),
    ("expected_runtime_model_id", "", "RUNTIME_MODEL_ID_BINDING_MISSING"),
    ("subscription_route_required", False, "SUBSCRIPTION_ROUTE_REQUIRED"),
    ("expected_billing_route", "ZEN", "BILLING_ROUTE_MISMATCH"),
    ("subscription_entitlement_confirmed", False, "ENTITLEMENT_EVIDENCE_MISSING"),
    ("billing_route_classification", "PER_CALL", "BILLING_ROUTE_MISMATCH"),
    ("expected_account_status", "", "ACCOUNT_STATUS_BINDING_MISSING"),
    ("no_fallback_required", False, "FALLBACK_POLICY_MISMATCH"),
    ("operator_authorization_id", "", "OPERATOR_IDENTITY_MISSING"),
    ("authorization_created_at", "not-a-timestamp", "CREATED_AT_INVALID"),
    ("campaign_attempt_identity", "attempt-1", "ATTEMPT_IDENTITY_INVALID"),
    ("single_frozen_six_case_campaign_confirmation", False, "CAMPAIGN_CONFIRMATION_MISSING"),
])
def test_authorization_rejects_wrong_field_values(manifest, auth, field, value, expected):
    changed = copy.deepcopy(auth)
    changed[field] = value
    assert runner.authorization_failure(changed, manifest) == expected


def test_authorization_rejects_unknown_fields(manifest, auth):
    changed = copy.deepcopy(auth)
    changed["zero_price_required"] = True
    assert runner.authorization_failure(changed, manifest) == "ZERO_PRICING_RULE_CONTRADICTION"
    changed = copy.deepcopy(auth)
    changed["catalog_binding_procedure"] = "observe"
    assert runner.authorization_failure(changed, manifest) == "CATALOG_BINDING_MISSING"
    changed = copy.deepcopy(auth)
    changed["unexpected_field"] = 1
    assert runner.authorization_failure(changed, manifest) == "UNKNOWN_FIELDS"


def test_authorization_rejects_missing_fields(manifest, auth):
    changed = copy.deepcopy(auth)
    del changed["expected_runtime_model_id"]
    assert runner.authorization_failure(changed, manifest) == "MISSING_FIELDS"


def test_authorization_rejects_wrong_types(manifest, auth):
    changed = copy.deepcopy(auth)
    changed["expected_opencode_version"] = 3
    assert runner.authorization_failure(changed, manifest) == "WRONG_TYPE"
    changed = copy.deepcopy(auth)
    changed["deny_zen_route"] = "yes"
    assert runner.authorization_failure(changed, manifest) == "WRONG_TYPE"
    changed = copy.deepcopy(auth)
    changed["permitted_case_ids"] = "not-a-list"
    assert runner.authorization_failure(changed, manifest) == "WRONG_TYPE"
    changed = copy.deepcopy(auth)
    changed["subscription_account_observation"] = "not-an-object"
    assert runner.authorization_failure(changed, manifest) == "WRONG_TYPE"


def test_authorization_rejects_duplicate_case_ids(manifest, auth):
    changed = copy.deepcopy(auth)
    changed["permitted_case_ids"] = [auth["permitted_case_ids"][0]] * 6
    assert runner.authorization_failure(changed, manifest) == "DUPLICATE_CASE_ID"


def test_authorization_rejects_changed_case_order(manifest, auth):
    changed = copy.deepcopy(auth)
    changed["permitted_case_ids"] = list(reversed(changed["permitted_case_ids"]))
    assert runner.authorization_failure(changed, manifest) == "CASE_SET_MISMATCH"


def test_authorization_rejects_wrong_output_root(manifest, auth, tmp_path):
    assert runner.authorization_failure(auth, manifest, expected_output_root=tmp_path / "other") == "OUTPUT_ROOT_MISMATCH"


def test_authorization_rejects_expired_validity(manifest, auth):
    changed = copy.deepcopy(auth)
    changed["authorization_valid_until"] = "2026-08-02T01:00:00Z"
    assert runner.authorization_failure(changed, manifest, now=runner._utc_now()) == "AUTHORIZATION_EXPIRED"


def test_authorization_rejects_validity_not_after_creation(manifest, auth):
    changed = copy.deepcopy(auth)
    changed["authorization_valid_until"] = "2026-08-01T00:00:00Z"
    assert runner.authorization_failure(changed, manifest, now=runner._utc_now()) == "VALIDITY_NOT_AFTER_CREATION"


def test_authorization_rejects_created_at_materially_in_future(manifest, auth):
    changed = copy.deepcopy(auth)
    changed["authorization_created_at"] = "2099-01-01T00:00:00Z"
    assert runner.authorization_failure(changed, manifest, now=runner._utc_now()) == "CREATED_AT_FUTURE"


def test_authorization_rejects_unknown_account_observation_fields(manifest, auth):
    changed = copy.deepcopy(auth)
    changed["subscription_account_observation"] = {"entitlement_confirmed": True, "evidence_reference": "x", "extra": 1}
    assert runner.authorization_failure(changed, manifest) == "ACCOUNT_OBSERVATION_INVALID"
    changed = copy.deepcopy(auth)
    changed["subscription_account_observation"] = {"entitlement_confirmed": True}
    assert runner.authorization_failure(changed, manifest) == "ACCOUNT_OBSERVATION_INVALID"


def test_authorization_rejects_whitespace_account_evidence_reference(manifest, auth):
    changed = copy.deepcopy(auth)
    changed["subscription_account_observation"] = {"entitlement_confirmed": True, "evidence_reference": "   "}
    assert runner.authorization_failure(changed, manifest) == "ACCOUNT_OBSERVATION_INVALID"


def test_authorization_rejects_wrong_account_observation_types(manifest, auth):
    changed = copy.deepcopy(auth)
    changed["subscription_account_observation"] = {"entitlement_confirmed": "yes", "evidence_reference": "x"}
    assert runner.authorization_failure(changed, manifest) == "ACCOUNT_OBSERVATION_INVALID"
    changed = copy.deepcopy(auth)
    changed["subscription_account_observation"] = {"entitlement_confirmed": True, "evidence_reference": 3}
    assert runner.authorization_failure(changed, manifest) == "ACCOUNT_OBSERVATION_INVALID"


def test_authorization_rejects_unconfirmed_account_observation(manifest, auth):
    changed = copy.deepcopy(auth)
    changed["subscription_account_observation"] = {"entitlement_confirmed": False, "evidence_reference": "x"}
    assert runner.authorization_failure(changed, manifest) == "ACCOUNT_OBSERVATION_INVALID"


@pytest.mark.parametrize("field", [
    "deny_zen_route", "deny_free_tier_substitution", "deny_ollama_route",
    "deny_alternate_provider", "deny_model_substitution", "deny_metered_fallback",
    "deny_paid_overage", "deny_per_call_billing_fallback",
])
def test_authorization_requires_every_denial_flag(manifest, auth, field):
    changed = copy.deepcopy(auth)
    changed[field] = False
    assert runner.authorization_failure(changed, manifest) == "DENIAL_FLAG_NOT_TRUE"


def test_v1_style_authorization_is_rejected_for_v2(manifest, auth):
    v1_style = copy.deepcopy(auth)
    del v1_style["subscription_route_required"]
    del v1_style["expected_billing_route"]
    del v1_style["expected_runtime_model_id"]
    del v1_style["subscription_entitlement_confirmed"]
    del v1_style["subscription_account_observation"]
    del v1_style["expected_account_status"]
    del v1_style["billing_route_classification"]
    for field in runner.DENIAL_FIELDS:
        del v1_style[field]
    v1_style["zero_price_required"] = True
    assert runner.authorization_failure(v1_style, manifest) == "ZERO_PRICING_RULE_CONTRADICTION"


def test_authorization_hash_is_deterministic(manifest, auth):
    assert runner.authorization_hash(auth) == runner.authorization_hash(copy.deepcopy(auth))


# ---- execution-commit / repository-state binding -------------------------------


def test_execution_commit_valid_when_head_matches(manifest, auth, tmp_path, git_state_provider):
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert record["status"] == "COMPLETED"
    execution = record["execution_commit"]
    assert execution["authorization_bound_execution_commit"] == auth["accepted_campaign_commit"]
    assert execution["independently_observed_head"] == auth["accepted_campaign_commit"]
    assert execution["verified"] is True
    assert execution["commit_exists_in_repository"] is True
    assert execution["descends_from_accepted_baseline"] is True
    assert execution["tracked_working_tree_clean"] is True
    assert execution["git_index_clean"] is True
    assert all(entry["execution_commit"] == auth["accepted_campaign_commit"] for entry in record["cases"])
    assert all(entry["campaign_commit"] == auth["accepted_campaign_commit"] for entry in record["cases"])
    assert record["ledger"]["execution_commit"] == auth["accepted_campaign_commit"]
    assert record["preflight"]["route_observation"]["execution_commit"] == auth["accepted_campaign_commit"]


def test_execution_commit_mismatch_is_rejected_before_provider(manifest, auth, tmp_path):
    def dirty_state(commit):
        return runner.GitRepositoryState(head="1" * 40, execution_commit_exists=True,
                                         execution_commit_descends_from_baseline=True,
                                         tracked_working_tree_clean=True, git_index_clean=True)

    record, factory, case_runner, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=dirty_state)
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "EXECUTION_COMMIT_MISMATCH"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert factory.created == []
    assert case_runner.order_log == []
    assert not (output / "campaign.json").exists()
    assert not (output / "ledger.json").exists()
    assert not (output / ".attempt-owner").exists()
    assert (output.parent / f"rejections-{output.name}").is_dir()


def test_execution_commit_not_found_is_rejected(manifest, auth, tmp_path):
    def missing_state(commit):
        return runner.GitRepositoryState(head=commit, execution_commit_exists=False,
                                         execution_commit_descends_from_baseline=False,
                                         tracked_working_tree_clean=True, git_index_clean=True)

    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, git_state_provider=missing_state)
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "EXECUTION_COMMIT_NOT_FOUND"
    assert record["provider_call_proof"]["logical_requests"] == 0


def test_execution_commit_not_descending_is_rejected(manifest, auth, tmp_path):
    def non_descending(commit):
        return runner.GitRepositoryState(head=commit, execution_commit_exists=True,
                                         execution_commit_descends_from_baseline=False,
                                         tracked_working_tree_clean=True, git_index_clean=True)

    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, git_state_provider=non_descending)
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "EXECUTION_COMMIT_ANCESTRY_FAILED"
    assert record["provider_call_proof"]["logical_requests"] == 0


@pytest.mark.parametrize("state_overrides", [
    {"tracked_working_tree_clean": False},
    {"git_index_clean": False},
    {"untracked_non_ignored": ("stray.py",)},
])
def test_dirty_repository_state_is_rejected(manifest, auth, tmp_path, state_overrides):
    def dirty_state(commit):
        return runner.GitRepositoryState(head=commit, execution_commit_exists=True,
                                         execution_commit_descends_from_baseline=True,
                                         tracked_working_tree_clean=state_overrides.get("tracked_working_tree_clean", True),
                                         git_index_clean=state_overrides.get("git_index_clean", True),
                                         untracked_non_ignored=state_overrides.get("untracked_non_ignored", ()))

    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, git_state_provider=dirty_state)
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "TRACKED_STATE_DIRTY"
    assert record["provider_call_proof"]["logical_requests"] == 0


def test_commit_drift_between_cases_stops_campaign(manifest, auth, tmp_path):
    calls = {"count": 0}

    def drifting_state(commit):
        calls["count"] += 1
        if calls["count"] >= 5:
            return runner.GitRepositoryState(head="9" * 40, execution_commit_exists=True,
                                             execution_commit_descends_from_baseline=True,
                                             tracked_working_tree_clean=True, git_index_clean=True)
        return _clean_git_state(commit)

    # Authority verification call sequence: 1 = initial, then per case
    # 2k = pre-case, 2k+1 = post-case (k = 1..6), 14 = pre-terminal.
    # Call 5 is the post-case check of case 2, so case 1 completes, case 2 is
    # authority-invalidated, and the drift stops the campaign.
    record, factory, case_runner, _ = _run_campaign(manifest, auth, tmp_path, git_state_provider=drifting_state)
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "TRACKED_SOURCE_CHANGED"
    assert [entry["terminal_status"] for entry in record["cases"][:2]] == ["UNRESOLVED", "UNRESOLVED"]
    for entry in record["cases"][2:]:
        assert entry["terminal_status"] == "BLOCKED"
        assert entry["terminal_reason_code"] == "TRACKED_SOURCE_CHANGED"
        assert entry["campaign_stop_evidence"]["pre_case_authority_check_identity"] == "AUTHORITY_CHECK:TRACKED_SOURCE"
    assert len(case_runner.order_log) == 2
    assert record["provider_call_proof"]["logical_requests"] == 2
    assert record["counts"]["completed_case_count"] == 1
    assert record["counts"]["invalidated_case_count"] == 1
    assert record["counts"]["blocked_case_count"] == 4
    assert record["case_lifecycle_states"][manifest["case_order"][1]["case_id"]] == "authority-invalidated"
    assert runner.validate_campaign_record(record, manifest) is True


def test_forged_authorization_commit_is_rejected_and_prior_evidence_unchanged(manifest, tmp_path, git_state_provider):
    output = tmp_path / "attempt-out"
    first_auth = _valid_authorization(manifest, output)
    first, _, _, _ = _run_campaign(manifest, first_auth, tmp_path, git_state_provider=git_state_provider)
    assert first["status"] == "COMPLETED"
    campaign_before = (tmp_path / "attempt-out" / "campaign.json").read_bytes()
    case_files_before = {
        path.name: path.read_bytes() for path in (tmp_path / "attempt-out" / "cases").iterdir()
    }
    ledger_before = (tmp_path / "attempt-out" / "ledger.json").read_bytes()

    forged = _valid_authorization(manifest, tmp_path / "forged-out")
    forged["accepted_campaign_commit"] = first_auth["accepted_campaign_commit"]

    def forged_state(commit):
        return runner.GitRepositoryState(head="2" * 40, execution_commit_exists=True,
                                         execution_commit_descends_from_baseline=True,
                                         tracked_working_tree_clean=True, git_index_clean=True)

    record = runner.run_campaign(
        manifest, authorization=forged, output_root=tmp_path / "forged-out",
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=forged_state,
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "EXECUTION_COMMIT_MISMATCH"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert record["cases"] == []
    assert not (tmp_path / "forged-out" / "campaign.json").exists()
    assert (tmp_path / "attempt-out" / "campaign.json").read_bytes() == campaign_before
    assert (tmp_path / "attempt-out" / "ledger.json").read_bytes() == ledger_before
    assert {path.name: path.read_bytes() for path in (tmp_path / "attempt-out" / "cases").iterdir()} == case_files_before


def test_execution_commit_evidence_is_not_copied_from_authorization_only(manifest, auth, tmp_path, git_state_provider):
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert record["execution_commit"]["independently_observed_head"] == auth["accepted_campaign_commit"]
    assert record["execution_commit"]["verified"] is True


# ---- strict raw route evidence -------------------------------------------------


def test_preflight_passes_with_full_subscription_evidence(manifest, auth):
    verdict = runner.run_route_preflight(manifest, auth, lambda: _route_evidence(manifest), now=runner._utc_now())
    assert verdict.passed is True
    assert verdict.failure_category is None
    assert verdict.route_observation["preflight_success"] is True
    assert verdict.route_observation["billing_route"] == "SUBSCRIPTION"
    assert verdict.route_observation["subscription_entitlement_confirmed"] is True
    assert verdict.route_observation["runtime_model_id"] == auth["expected_runtime_model_id"]
    assert verdict.route_observation["account_status"] == auth["expected_account_status"]
    assert "observed_at" in verdict.route_observation


IDENTITY_FIELDS = ["provider", "model", "variant", "protocol", "opencode_version", "catalog_fingerprint",
                   "runtime_model_id", "billing_route", "subscription_entitlement_confirmed", "account_status",
                   "active_model_status", "variant_available", "observed_at", "input_price", "output_price",
                   "provider_reported_cost"]


@pytest.mark.parametrize("field", IDENTITY_FIELDS)
def test_preflight_rejects_missing_identity_field(manifest, auth, field):
    evidence = _route_evidence(manifest)
    del evidence[field]
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert exc.value.reason == "MISSING_FIELD"


DENIAL_FLAG_FIELDS = ["zen_used", "free_tier_used", "ollama_used", "paid_fallback_used", "alternate_provider_used",
                      "metered_fallback_used", "paid_overage_used", "per_call_billing_used", "model_substitution_observed"]


@pytest.mark.parametrize("field", DENIAL_FLAG_FIELDS)
def test_preflight_rejects_missing_denial_observation(manifest, auth, field):
    evidence = _route_evidence(manifest)
    del evidence[field]
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert exc.value.reason == "MISSING_FIELD"


def test_preflight_rejects_missing_everything_route_identity(manifest, auth):
    evidence = {"observed_at": _now_iso()}
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert exc.value.reason == "MISSING_FIELD"
    assert "provider" in exc.value.detail


def test_preflight_rejects_account_status_mismatch(manifest, auth):
    evidence = _route_evidence(manifest, account_status="SUSPENDED")
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert exc.value.reason == "ACCOUNT_STATUS_MISMATCH"


def test_preflight_rejects_invalid_timestamp(manifest, auth):
    evidence = _route_evidence(manifest, observed_at="not-a-timestamp")
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert exc.value.reason == "INVALID_TIMESTAMP"


def test_preflight_rejects_stale_timestamp(manifest, auth):
    evidence = _route_evidence(manifest, observed_at="2000-01-01T00:00:00Z")
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert exc.value.reason == "STALE_TIMESTAMP"


def test_preflight_rejects_future_timestamp(manifest, auth):
    evidence = _route_evidence(manifest, observed_at="2099-01-01T00:00:00Z")
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert exc.value.reason == "FUTURE_TIMESTAMP"


def test_preflight_rejects_unknown_fields(manifest, auth):
    evidence = _route_evidence(manifest, suspicious_extra="x")
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert exc.value.reason == "UNKNOWN_FIELD"


def test_preflight_rejects_wrong_optional_metadata_version(manifest, auth):
    evidence = _route_evidence(manifest, schema_version="wrong-version")
    with pytest.raises(runner.RouteEvidenceInvalid):
        runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())


def test_preflight_allows_versioned_optional_metadata(manifest, auth):
    evidence = _route_evidence(manifest, schema_version="quixbugs-route-evidence-v1")
    verdict = runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert verdict.passed is True


@pytest.mark.parametrize("field,value", [
    ("provider", 3), ("variant_available", "yes"), ("zen_used", 1),
    ("catalog_fingerprint", "short"), ("input_price", "free"), ("billing_route", "BILLED"),
    ("active_model_status", "STANDBY"), ("input_price", -1),
])
def test_preflight_rejects_wrong_types(manifest, auth, field, value):
    evidence = _route_evidence(manifest, **{field: value})
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert exc.value.reason == "WRONG_TYPE"


@pytest.mark.parametrize("overrides,expected", [
    ({"billing_route": "ZEN", "zen_used": True, "subscription_entitlement_confirmed": False}, "ZEN_ROUTE_OBSERVED"),
    ({"billing_route": "FREE_TIER", "free_tier_used": True, "subscription_entitlement_confirmed": False}, "FREE_TIER_SUBSTITUTION"),
    ({"billing_route": "OLLAMA", "ollama_used": True, "subscription_entitlement_confirmed": False}, "OLLAMA_ROUTE_OBSERVED"),
    ({"billing_route": "METERED", "metered_fallback_used": True, "subscription_entitlement_confirmed": False}, "METERED_FALLBACK_REQUIRED"),
    ({"paid_overage_used": True}, "PAID_OVERAGE_REQUIRED"),
    ({"billing_route": "PER_CALL", "per_call_billing_used": True, "subscription_entitlement_confirmed": False}, "PER_CALL_BILLING_FALLBACK"),
    ({"billing_route": "UNKNOWN", "subscription_entitlement_confirmed": False}, "SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED"),
    ({"subscription_entitlement_confirmed": False}, "SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED"),
    ({"runtime_model_id": "different/model-id"}, "RUNTIME_MODEL_ID_MISMATCH"),
    ({"opencode_version": "9.9.9"}, "OPENCODE_VERSION_MISMATCH"),
    ({"active_model_status": "INACTIVE"}, "MODEL_INACTIVE"),
    ({"active_model_status": "ACTIVE", "variant_available": False}, "VARIANT_UNAVAILABLE"),
    ({"provider": "OpenCode Zen"}, "PROVIDER_MISMATCH"),
    ({"model": "deepseek-v4-flash-free"}, "MODEL_MISMATCH"),
    ({"variant": "default"}, "VARIANT_MISMATCH"),
    ({"protocol": "1.2"}, "PROTOCOL_MISMATCH"),
])
def test_preflight_blocks_every_prohibited_route(manifest, auth, overrides, expected):
    evidence = _route_evidence(manifest, **overrides)
    verdict = runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert verdict.passed is False
    assert verdict.failure_category == expected
    assert verdict.route_observation["preflight_success"] is False
    assert verdict.preflight_failure_evidence["failure_category"] == expected


def test_preflight_blocks_model_substitution(manifest, auth):
    evidence = _route_evidence(manifest, model="other-model", model_substitution_observed=True,
                               runtime_model_id="opencode-go/other-model")
    verdict = runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert verdict.passed is False
    assert verdict.failure_category == "MODEL_SUBSTITUTION_OBSERVED"
    assert verdict.preflight_failure_evidence["expected_runtime_model_id"] == auth["expected_runtime_model_id"]
    assert verdict.preflight_failure_evidence["observed_runtime_model_id"] == "opencode-go/other-model"


def test_preflight_blocks_catalog_fingerprint_mismatch(manifest, auth):
    evidence = _route_evidence(manifest, catalog_fingerprint="d" * 64)
    verdict = runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert verdict.passed is False
    assert verdict.failure_category == "CATALOG_PREFLIGHT_FAILED"
    assert verdict.route_observation["active_model_status"] == "NOT_RUN"
    assert verdict.preflight_failure_evidence["catalog_failure_category"] == "catalog_fingerprint_mismatch"


def test_preflight_blocks_unobservable_evidence(manifest, auth):
    def unavailable():
        raise runner.RouteEvidenceUnavailable("CATALOG_PREFLIGHT_FAILED", "catalog query not run")

    verdict = runner.run_route_preflight(manifest, auth, unavailable, now=runner._utc_now())
    assert verdict.passed is False
    assert verdict.failure_category == "CATALOG_PREFLIGHT_FAILED"
    assert verdict.route_observation["active_model_status"] == "NOT_RUN"

    def unavailable_entitlement():
        raise runner.RouteEvidenceUnavailable("SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED", "entitlement not observable")

    verdict = runner.run_route_preflight(manifest, auth, unavailable_entitlement, now=runner._utc_now())
    assert verdict.passed is False
    assert verdict.failure_category == "SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED"
    assert verdict.route_observation["billing_route"] == "UNKNOWN"


def test_preflight_requires_route_evidence_provider(manifest, auth):
    with pytest.raises(runner.LiveRunnerError):
        runner.run_route_preflight(manifest, auth, None, now=runner._utc_now())


def test_preflight_failure_evidence_validates_as_frozen_block(manifest, auth):
    evidence = _route_evidence(manifest, billing_route="ZEN", zen_used=True, subscription_entitlement_confirmed=False)
    verdict = runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    record = runner.build_preprovider_block_record(manifest, manifest["case_order"][0], auth, verdict,
                                                   attempt_identity=auth["campaign_attempt_identity"],
                                                   execution_commit=auth["accepted_campaign_commit"])
    pilot.validate_case_result(record, manifest, auth)
    assert record["terminal_status"] == "BLOCKED"
    assert record["blocked_evidence"]["block_kind"] == "live-pre-provider"
    assert record["logical_model_calls"] == 0
    assert record["provider_process_attempts"] == 0
    assert record["execution_commit"] == auth["accepted_campaign_commit"]


# ---- campaign orchestration ----------------------------------------------------

def _completed_entries(manifest, **outcome_overrides):
    route = _route_evidence(manifest)
    entries = []
    for case in manifest["case_order"]:
        entries.append({
            "provider_process_attempts": 1,
            "outcome": _completed_outcome(manifest, case, route, **outcome_overrides),
        })
    return entries


def test_campaign_runs_all_six_cases_in_frozen_order(manifest, auth, tmp_path, git_state_provider):
    record, factory, case_runner, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert record["status"] == "COMPLETED"
    assert record["stop_reason"] is None
    assert [case["case_id"] for case in manifest["case_order"]] == case_runner.order_log == factory.created
    assert record["case_execution_order"] == [case["case_id"] for case in manifest["case_order"]]
    assert record["provider_call_proof"]["transports_created"] == 6
    assert record["provider_call_proof"]["logical_requests"] == 6
    assert record["counts"]["completed_case_count"] == 6
    assert record["counts"]["unstarted_case_count"] == 0
    assert len(record["cases"]) == 6
    assert (output / "campaign.json").is_file()
    assert (output / "ledger.json").is_file()
    assert (output / ".attempt-owner").is_file()
    ledger = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
    assert any(entry["status"] == "COMPLETED" and entry["attempt_identity"] == auth["campaign_attempt_identity"] for entry in ledger.values())


def test_campaign_no_parallel_execution_and_fresh_boundary_per_case(manifest, auth, tmp_path, git_state_provider):
    record, factory, case_runner, _ = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert case_runner.order_log == [case["case_id"] for case in manifest["case_order"]]
    run_ids = [run_id for run_id, _ in case_runner.session_boundaries]
    session_ids = [session_id for _, session_id in case_runner.session_boundaries]
    assert len(set(run_ids)) == 6
    assert len(set(session_ids)) == 6
    assert len(set(factory.created)) == 6
    assert len({id(transport) for transport in case_runner.transports}) == 6
    assert run_ids[0] == runner.deterministic_run_id(auth["campaign_attempt_identity"], manifest["case_order"][0])
    assert session_ids[0] == runner.deterministic_session_id(auth["campaign_attempt_identity"], manifest["case_order"][0])


def test_campaign_preflight_failure_blocks_with_zero_provider_calls(manifest, auth, tmp_path, git_state_provider):
    record, factory, case_runner, output = _run_campaign(
        manifest, auth, tmp_path,
        route_evidence=_route_evidence(manifest, billing_route="ZEN", zen_used=True, subscription_entitlement_confirmed=False),
        git_state_provider=git_state_provider,
    )
    assert record["status"] == "BLOCKED"
    assert record["stop_reason"] == "BLOCKED_PRE_PROVIDER:ZEN_ROUTE_OBSERVED"
    assert record["provider_call_proof"] == {"transports_created": 0, "process_launches": 0, "logical_requests": 0}
    assert factory.created == []
    assert case_runner.order_log == []
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "pre-provider-rejected"
    assert record["counts"]["unstarted_case_count"] == 5
    assert record["cases"][0]["terminal_status"] == "BLOCKED"
    assert not (output / "campaign.json").exists()
    assert not (output / ".attempt-owner").exists()
    assert (output.parent / f"rejections-{output.name}").is_dir()


@pytest.mark.parametrize("overrides,expected", [
    ({"billing_route": "ZEN", "zen_used": True, "subscription_entitlement_confirmed": False}, "ZEN_ROUTE_OBSERVED"),
    ({"billing_route": "FREE_TIER", "free_tier_used": True, "subscription_entitlement_confirmed": False}, "FREE_TIER_SUBSTITUTION"),
    ({"billing_route": "OLLAMA", "ollama_used": True, "subscription_entitlement_confirmed": False}, "OLLAMA_ROUTE_OBSERVED"),
    ({"billing_route": "METERED", "metered_fallback_used": True, "subscription_entitlement_confirmed": False}, "METERED_FALLBACK_REQUIRED"),
    ({"paid_overage_used": True}, "PAID_OVERAGE_REQUIRED"),
    ({"billing_route": "PER_CALL", "per_call_billing_used": True, "subscription_entitlement_confirmed": False}, "PER_CALL_BILLING_FALLBACK"),
    ({"billing_route": "UNKNOWN", "subscription_entitlement_confirmed": False}, "SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED"),
    ({"subscription_entitlement_confirmed": False}, "SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED"),
    ({"runtime_model_id": "different/model-id"}, "RUNTIME_MODEL_ID_MISMATCH"),
    ({"opencode_version": "9.9.9"}, "OPENCODE_VERSION_MISMATCH"),
    ({"active_model_status": "INACTIVE"}, "MODEL_INACTIVE"),
    ({"active_model_status": "ACTIVE", "variant_available": False}, "VARIANT_UNAVAILABLE"),
    ({"model": "deepseek-v4-flash-free", "model_substitution_observed": True, "runtime_model_id": "opencode-go/other"}, "MODEL_SUBSTITUTION_OBSERVED"),
])
def test_campaign_preflight_failure_keeps_provider_calls_zero_for_every_route(manifest, auth, tmp_path, git_state_provider, overrides, expected):
    record, factory, case_runner, _ = _run_campaign(
        manifest, auth, tmp_path,
        route_evidence=_route_evidence(manifest, **overrides),
        git_state_provider=git_state_provider,
    )
    assert record["status"] == "BLOCKED"
    assert record["stop_reason"] == f"BLOCKED_PRE_PROVIDER:{expected}"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert factory.created == []
    assert case_runner.order_log == []


def test_campaign_strict_route_evidence_failure_is_rejected(manifest, auth, tmp_path, git_state_provider):
    record, factory, case_runner, output = _run_campaign(
        manifest, auth, tmp_path,
        route_evidence=_route_evidence(manifest, account_status="SUSPENDED"),
        git_state_provider=git_state_provider,
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "ROUTE_EVIDENCE_INVALID:ACCOUNT_STATUS_MISMATCH"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert factory.created == []
    assert case_runner.order_log == []
    assert not (output / "campaign.json").exists()
    assert not (output / "ledger.json").exists()


def test_campaign_invalid_authorization_is_rejected_before_provider(manifest, tmp_path, git_state_provider):
    output = tmp_path / "attempt-out"
    bad_auth = dict(_valid_authorization(manifest, output))
    bad_auth["campaign_manifest_hash"] = "0" * 64
    record = runner.run_campaign(
        manifest, authorization=bad_auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=git_state_provider,
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "MANIFEST_MISMATCH"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert record["case_lifecycle_states"] == {case["case_id"]: "authorization-rejected" for case in manifest["case_order"]}
    assert not (output / "campaign.json").exists()


def test_campaign_missing_transport_and_runner_reject_before_consuming_authorization(manifest, auth, tmp_path, git_state_provider):
    output = tmp_path / "attempt-out"
    record = runner.run_campaign(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=None, case_runner=None,
        git_state_provider=git_state_provider,
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "TRANSPORT_NOT_CONFIGURED"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert not (output / "ledger.json").exists()
    assert not (output / ".attempt-owner").exists()
    assert not (output / "campaign.json").exists()
    assert record["case_lifecycle_states"] == {case["case_id"]: "unstarted" for case in manifest["case_order"]}


def test_campaign_first_case_invalid_model_response_then_continues(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 3, "outcome": _invalid_model_outcome(manifest, manifest["case_order"][0], route)}
    record, factory, case_runner, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "COMPLETED"
    assert record["cases"][0]["terminal_status"] == "INVALID_MODEL_RESPONSE"
    assert [entry["terminal_status"] for entry in record["cases"]] == ["INVALID_MODEL_RESPONSE"] + ["UNRESOLVED"] * 5


def test_campaign_middle_case_failure_blocks_remaining_cases(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[2] = {"provider_process_attempts": 1, "outcome": _cleanup_failure_outcome(manifest, manifest["case_order"][2], route)}
    record, factory, case_runner, output = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "CLEANUP_FAILURE"
    statuses = [entry["terminal_status"] for entry in record["cases"]]
    assert statuses[:3] == ["UNRESOLVED", "UNRESOLVED", "INFRASTRUCTURE_ERROR"]
    assert statuses[3:] == ["BLOCKED", "BLOCKED", "BLOCKED"]
    for entry in record["cases"][3:]:
        assert entry["terminal_reason_code"] == "CLEANUP_FAILURE"
    assert record["counts"]["completed_case_count"] == 3
    assert record["counts"]["blocked_case_count"] == 3
    assert record["counts"]["unstarted_case_count"] == 0
    assert len(list((output / "cases").iterdir())) == 6


def test_campaign_final_case_failure_records_partial_campaign(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[5] = {"provider_process_attempts": 1, "outcome": _cleanup_failure_outcome(manifest, manifest["case_order"][5], route)}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "CLEANUP_FAILURE"
    assert [entry["terminal_status"] for entry in record["cases"][:-1]] == ["UNRESOLVED"] * 5
    assert record["cases"][-1]["terminal_status"] == "INFRASTRUCTURE_ERROR"


def test_campaign_route_drift_after_preflight_stops_campaign(manifest, auth, tmp_path, git_state_provider):
    entries = _completed_entries(manifest)
    entries[1]["drift"] = "RUNTIME_MODEL_ID_MISMATCH"
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "TRANSPORT_EVIDENCE_LOSS"
    assert record["cases"][0]["terminal_status"] == "UNRESOLVED"
    assert record["cases"][1]["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert record["cases"][1]["terminal_reason_code"] == "TRANSPORT_EVIDENCE_LOSS"
    assert record["cases"][1]["infrastructure_evidence"]["stage"] == "provider_transport"
    for entry in record["cases"][2:]:
        assert entry["terminal_status"] == "BLOCKED"
        assert entry["terminal_reason_code"] == "TRANSPORT_EVIDENCE_LOSS"


def test_campaign_model_substitution_after_preflight_stops_campaign(manifest, auth, tmp_path, git_state_provider):
    entries = _completed_entries(manifest)
    entries[0]["drift"] = "MODEL_SUBSTITUTION_OBSERVED"
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "TRANSPORT_EVIDENCE_LOSS"
    assert record["cases"][0]["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert "MODEL_SUBSTITUTION_OBSERVED" in record["cases"][0]["infrastructure_evidence"]["evidence_reference"]


def test_campaign_source_mutation_stops_with_source_mutation_evidence(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[1] = {"provider_process_attempts": 1, "outcome": _source_mutation_outcome(manifest, manifest["case_order"][1], route)}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "SOURCE_MUTATION"
    assert record["cases"][1]["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert record["cases"][1]["infrastructure_evidence"]["source_mutation_observed"] is True
    for entry in record["cases"][2:]:
        assert entry["terminal_reason_code"] == "SOURCE_MUTATION"


def test_campaign_verifier_integrity_failure_stops_campaign(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[3] = {"provider_process_attempts": 1, "outcome": _verifier_failure_outcome(manifest, manifest["case_order"][3], route)}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "VERIFIER_INTEGRITY_FAILURE"
    assert record["cases"][3]["terminal_status"] == "INFRASTRUCTURE_ERROR"
    for entry in record["cases"][4:]:
        assert entry["terminal_reason_code"] == "VERIFIER_INTEGRITY_FAILURE"


def test_campaign_static_policy_cannot_open_pdb(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    static_index = next(i for i, case in enumerate(manifest["case_order"]) if case["policy"] == "static-baseline")
    entries[static_index] = {"provider_process_attempts": 1, "outcome": _pdb_activity_outcome(manifest, manifest["case_order"][static_index], route)}
    record, factory, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "STATIC_POLICY_PDB_VIOLATION"
    assert record["provider_call_proof"]["logical_requests"] == static_index + 1


def test_campaign_pdb_policy_uses_gate_and_budgets(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    for i, case in enumerate(manifest["case_order"]):
        if case["policy"] == "pdb-on-uncertainty":
            entries[i] = {"provider_process_attempts": 1, "outcome": _pdb_activity_outcome(manifest, case, route)}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "COMPLETED"
    pdb_cases = [entry for i, entry in enumerate(record["cases"]) if manifest["case_order"][i]["policy"] == "pdb-on-uncertainty"]
    assert all(entry["pdb_counts"]["allowed_gate_openings"] == 2 for entry in pdb_cases)
    assert record["pdb"]["gate_openings"] == 6
    assert record["pdb"]["observations"] == 6


def test_campaign_pdb_not_reached_is_a_valid_outcome(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    pdb_index = next(i for i, case in enumerate(manifest["case_order"]) if case["policy"] == "pdb-on-uncertainty")
    entries[pdb_index] = {"provider_process_attempts": 2, "outcome": _pdb_not_reached_outcome(manifest, manifest["case_order"][pdb_index], route)}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "COMPLETED"
    assert record["cases"][pdb_index]["terminal_status"] == "PDB_NOT_REACHED"
    assert record["cases"][pdb_index]["terminal_reason_code"] == "PDB_NOT_REACHED_NO_GATE"


def test_campaign_pdb_gate_rejected_route(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    pdb_index = next(i for i, case in enumerate(manifest["case_order"]) if case["policy"] == "pdb-on-uncertainty")
    outcome = _pdb_not_reached_outcome(manifest, manifest["case_order"][pdb_index], route, **{
        "terminal_reason_code": "PDB_NOT_REACHED_GATE_REJECTED",
        "pdb_gate_decisions": [{"allowed": False, "reason": "gate"}, {"allowed": False, "reason": "gate"}],
        "pdb_counts": {
            "total_gate_decisions": 2, "allowed_gate_openings": 0, "rejected_gate_decisions": 2,
            "sessions_started": 0, "successful_observations": 0, "failed_observations": 0,
        },
    })
    entries[pdb_index] = {"provider_process_attempts": 2, "outcome": outcome}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "COMPLETED"
    assert record["cases"][pdb_index]["terminal_reason_code"] == "PDB_NOT_REACHED_GATE_REJECTED"


def test_campaign_budget_exceeded_aborts(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 65, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route,
                                                                                 logical_model_calls=65, provider_process_attempts=65)}
    record, factory, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["unstarted_case_count"] == 5
    assert record["counts"]["aborted_case_count"] == 1
    assert record["provider_call_proof"]["logical_requests"] == 65


@pytest.mark.parametrize("field,value", [
    ("logical_model_calls", 65),
    ("provider_process_attempts", 193),
    ("retries", 129),
    ("valid_directives", 65),
    ("malformed_directive_rejections", 3),
    ("bounded_directive_feedback_events", 3),
    ("hypotheses_created", 4),
    ("patch_submissions", 2),
    ("verifier_runs", 21),
])
def test_campaign_each_frozen_budget_is_enforced(manifest, auth, tmp_path, git_state_provider, field, value):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 1, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route, **{field: value})}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"


def test_enforce_case_budgets_distinguishes_expected_public_evidence_exhaustion(manifest):
    route = _route_evidence(manifest)
    case = manifest["case_order"][0]
    exhausted = _completed_outcome(manifest, case, route, public_evidence_bytes=20001)
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(exhausted, manifest, case_policy=case["policy"])
    assert info.value.limit == 20000
    assert info.value.observed == 20001
    corrupt = _completed_outcome(manifest, case, route, public_evidence_bytes=-1)
    with pytest.raises(runner.BudgetViolationError):
        runner.enforce_case_budgets(corrupt, manifest, case_policy=case["policy"])


def test_campaign_public_evidence_budget_exhaustion_is_case_level_terminal(manifest, auth, tmp_path, git_state_provider):
    """Production-shaped regression: nine completed provider responses, nine
    structurally accepted directives, one hypothesis, controller reached
    Patch, next public_evidence_bytes 21949 > frozen 20000 limit, provider
    cost 0.0066370976.  The case is terminalized at case level with all
    completed accounting and cost preserved; the campaign proceeds to the
    second case and is never ABORTED."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    raw = _completed_outcome(manifest, manifest["case_order"][0], route, **{
        "terminal_status": "INFRASTRUCTURE_ERROR",
        "terminal_reason_code": "INFRASTRUCTURE_FAILURE",
        "termination_reason": "controller stopped before DONE",
        "logical_model_calls": 9,
        "provider_process_attempts": 9,
        "valid_directives": 9,
        "baseline_reproduction": True,
        "controller_states_visited": ["REPRODUCE", "UNDERSTAND", "PATCH"],
        "hypotheses_created": 1,
        "pdb_gate_decisions": [],
        "pdb_counts": dict(runner.ZERO_PDB_COUNTS),
        "verifier_runs": 0,
        "patch_submissions": 0,
        "independent_verifier_result": {"status": None, "outcome": None, "lifecycle_succeeded": False},
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "synthetic-controller-stop",
        },
        "infrastructure_evidence": {
            "stage": "controller", "reason_code": "CONTROLLER_FAILURE", "confirmed_failure": True,
            "classification": "CONTROLLER", "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "provider_attempt_index": None, "prior_lifecycle_completed": True,
            "source_mutation_observed": False, "expected_source_hash": None,
            "evidence_reference": "synthetic-controller-stop",
        },
        "prompt_tokens": 42000,
        "completion_tokens": 1200,
        "reasoning_tokens": 800,
        "provider_reported_cost": 0.0066370976,
        "wall_clock_duration_seconds": 190.0,
        "public_evidence_bytes": 21949,
    })
    entries[0] = {"provider_process_attempts": 9, "outcome": raw}
    record, factory, _, output = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)

    assert record["status"] == "COMPLETED"
    assert record["stop_reason"] is None
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "completed"
    assert record["counts"]["completed_case_count"] == 6
    assert record["counts"]["unstarted_case_count"] == 0
    assert record["counts"]["aborted_case_count"] == 0
    assert record["counts"]["logical_model_calls"] == 14
    assert record["counts"]["provider_process_attempts"] == 14
    assert record["counts"]["accepted_directives"] == 14
    assert record["provider_call_proof"]["logical_requests"] == 14
    first = record["cases"][0]
    assert first["terminal_status"] == "PDB_NOT_REACHED"
    assert first["terminal_reason_code"] == "PDB_NOT_REACHED_NO_GATE"
    assert "21949" in first["termination_reason"] and "20000" in first["termination_reason"]
    assert "public-evidence budget exhausted" in first["termination_reason"]
    assert first["public_evidence_bytes"] == 20000
    assert first["logical_model_calls"] == 9
    assert first["provider_process_attempts"] == 9
    assert first["valid_directives"] == 9
    assert first["hypotheses_created"] == 1
    assert first["controller_states_visited"] == ["REPRODUCE", "UNDERSTAND", "PATCH"]
    assert first["prompt_tokens"] == 42000
    assert first["completion_tokens"] == 1200
    assert first["reasoning_tokens"] == 800
    assert first["provider_reported_cost"] == pytest.approx(0.0066370976)
    assert first["transport_evidence"]["provider_error"] is False
    assert first["terminal_transport_evidence"]["final_attempt_classification"] == "COMPLETED_RESPONSE"
    assert first["terminal_transport_evidence"]["timed_out"] is False
    assert first["terminal_transport_evidence"]["process_exit_code"] == 0
    assert first["repair_outcome"] == "NO_CANDIDATE"
    # The campaign proceeds to the second frozen case.
    assert record["cases"][1]["case_id"] == manifest["case_order"][1]["case_id"]
    assert record["cases"][1]["terminal_status"] == "UNRESOLVED"
    assert record["cost_summary"]["classification"] == "REPORTED"
    assert record["cost_summary"]["total_provider_reported_cost"] == pytest.approx(0.027637)
    assert (output / "cases" / "case-01-quixbugs-paired-pilot-v2__quixbugs-find-in-sorted-smoke-v1__pdb-on-uncertainty.json").is_file()
    assert runner.verify_attempt_package(output, manifest)["consistent"] is True


def test_campaign_public_evidence_exhaustion_before_any_provider_call_is_no_contact_terminal(manifest, auth, tmp_path, git_state_provider):
    """Public-evidence exhaustion before any provider call materializes a
    schema-valid no-contact case terminal result and the campaign continues.

    The raw outcome is an internally consistent zero-contact shape: every
    activity counter is zero, lifecycle and transport evidence are empty,
    tokens/cost/hashes carry no activity, and the terminal fields already
    match the frozen pre-provider harness-error representation.  It does not
    rely on normalization of the completed-outcome defaults."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    raw = _completed_outcome(manifest, manifest["case_order"][0], route, **{
        "terminal_status": "INFRASTRUCTURE_ERROR",
        "terminal_reason_code": "WORKSPACE_FAILURE",
        "termination_reason": "synthetic no-contact harness failure",
        "logical_model_calls": 0,
        "provider_process_attempts": 0,
        "valid_directives": 0,
        "baseline_reproduction": False,
        "controller_states_visited": [],
        "independent_verifier_result": {"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": None, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": False, "evidence_reference": "synthetic-no-contact",
        },
        "infrastructure_evidence": {
            "stage": "pre_provider", "reason_code": "WORKSPACE_FAILURE", "confirmed_failure": True,
            "classification": "PRE_PROVIDER", "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "provider_attempt_index": None, "prior_lifecycle_completed": False,
            "source_mutation_observed": False, "expected_source_hash": None,
            "evidence_reference": "synthetic-no-contact",
        },
        "preflight_failure_evidence": {field: None for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS},
        "campaign_stop_evidence": dict(
            {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS}, confirmed=False
        ),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "provider_reported_cost": 0,
        "wall_clock_duration_seconds": 0.0,
        "public_request_hash": None,
        "source_hash": None,
        "candidate_hash": None,
        "public_evidence_bytes": 20001,
    })
    entries[0] = {"provider_process_attempts": 0, "outcome": raw}
    record, factory, _, output = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)

    assert record["status"] == "COMPLETED"
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "completed"
    assert record["counts"]["completed_case_count"] == 6
    first = record["cases"][0]
    assert first["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert first["terminal_reason_code"] == "WORKSPACE_FAILURE"
    assert first["infrastructure_evidence"]["stage"] == "pre_provider"
    assert first["infrastructure_evidence"]["prior_lifecycle_completed"] is False
    assert "20001" in first["termination_reason"] and "20000" in first["termination_reason"]
    assert first["public_evidence_bytes"] == 20000
    assert first["logical_model_calls"] == 0
    assert first["provider_process_attempts"] == 0
    assert first["retries"] == 0
    assert first["valid_directives"] == 0
    assert first["prompt_tokens"] == 0
    assert first["provider_reported_cost"] == 0
    assert first["public_request_hash"] is None
    assert first["source_hash"] is None
    assert first["candidate_hash"] is None
    assert runner.verify_attempt_package(output, manifest)["consistent"] is True


def test_campaign_public_evidence_exhaustion_no_contact_with_resolved_terminal_aborts(manifest, auth, tmp_path, git_state_provider):
    """Contradictory terminal semantics: a zero-contact raw outcome that
    claims a RESOLVED terminal status is not normalized into the frozen
    INFRASTRUCTURE_ERROR/WORKSPACE_FAILURE no-contact representation.  It has
    no valid frozen terminal representation and the campaign aborts."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 0, "outcome": _no_contact_outcome(manifest, manifest["case_order"][0], route,
                                                                                 terminal_status="RESOLVED")}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert "no valid terminal representation" in record["stop_detail"]
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["unstarted_case_count"] == 5


def test_campaign_public_evidence_exhaustion_no_contact_with_completed_terminal_transport_aborts(manifest, auth, tmp_path, git_state_provider):
    """Contradictory terminal transport evidence: a zero-contact raw outcome
    whose terminal transport claims a completed provider response is not
    normalized into the frozen pre-provider infrastructure representation.  It
    has no valid frozen terminal representation and the campaign aborts."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    terminal = {
        "final_attempt_classification": "COMPLETED_RESPONSE", "process_exit_code": 0, "timed_out": False,
        "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "synthetic-contradiction",
    }
    entries[0] = {"provider_process_attempts": 0, "outcome": _no_contact_outcome(manifest, manifest["case_order"][0], route,
                                                                                 terminal_transport_evidence=terminal)}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert "no valid terminal representation" in record["stop_detail"]
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["unstarted_case_count"] == 5


def test_campaign_public_evidence_exhaustion_no_contact_with_confirmed_block_aborts(manifest, auth, tmp_path, git_state_provider):
    """Contradictory blocked evidence: a zero-contact raw outcome carrying a
    confirmed block is not normalized into the frozen no-block no-contact
    representation.  The block evidence must never be erased, so the campaign
    aborts."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 0, "outcome": _no_contact_outcome(manifest, manifest["case_order"][0], route,
                                                                                 blocked_evidence={
                                                                                     "block_kind": "live-pre-provider",
                                                                                     "reason_code": "ROUTE_PREFLIGHT_FAILURE",
                                                                                     "confirmed": True,
                                                                                     "evidence_reference": "synthetic-contradiction",
                                                                                 })}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert "no valid terminal representation" in record["stop_detail"]
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["unstarted_case_count"] == 5


def test_campaign_public_evidence_exhaustion_no_contact_with_unconfirmed_infrastructure_aborts(manifest, auth, tmp_path, git_state_provider):
    """Contradictory infrastructure evidence: a zero-contact raw outcome whose
    infrastructure evidence does not confirm a pre-provider WORKSPACE_FAILURE
    is not normalized into the frozen no-contact representation.  It has no
    valid frozen terminal representation and the campaign aborts."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 0, "outcome": _no_contact_outcome(manifest, manifest["case_order"][0], route,
                                                                                 infrastructure_evidence={
                                                                                     "stage": "controller", "reason_code": "CONTROLLER_FAILURE",
                                                                                     "confirmed_failure": True, "classification": "CONTROLLER",
                                                                                     "terminal_classification": "INFRASTRUCTURE_FAILURE",
                                                                                     "provider_attempt_index": None, "prior_lifecycle_completed": True,
                                                                                     "source_mutation_observed": False, "expected_source_hash": None,
                                                                                     "evidence_reference": "synthetic-contradiction",
                                                                                 })}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert "no valid terminal representation" in record["stop_detail"]
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["unstarted_case_count"] == 5


def test_campaign_public_evidence_exhaustion_no_contact_with_boolean_token_accounting_aborts(manifest, auth, tmp_path, git_state_provider):
    """Type-correctness: a zero-contact raw outcome carrying a Boolean token
    counter (``prompt_tokens = False``) is not accepted as the frozen integer
    zero.  It has no valid frozen terminal representation and the campaign
    aborts instead of rewriting the Boolean into a schema-valid terminal."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 0, "outcome": _no_contact_outcome(manifest, manifest["case_order"][0], route,
                                                                                 prompt_tokens=False)}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert "no valid terminal representation" in record["stop_detail"]
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["unstarted_case_count"] == 5


def test_campaign_public_evidence_exhaustion_zero_calls_with_accepted_directive_aborts(manifest, auth, tmp_path, git_state_provider):
    """Corrupt accounting: zero logical calls but an accepted directive.  The
    relational invariant must be evaluated before public-evidence exhaustion
    can be terminalized, so the campaign aborts instead of silently
    normalizing the directive away."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 0, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route, **{
        "logical_model_calls": 0,
        "provider_process_attempts": 0,
        "valid_directives": 1,
        "public_evidence_bytes": 20001,
    })}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["unstarted_case_count"] == 5


def test_campaign_public_evidence_exhaustion_zero_attempts_with_retry_aborts(manifest, auth, tmp_path, git_state_provider):
    """Corrupt accounting: zero provider attempts but a recorded transport
    retry.  The retry relational invariant aborts the campaign instead of the
    exhaustion being terminalized."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 0, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route, **{
        "logical_model_calls": 0,
        "provider_process_attempts": 0,
        "retries": 1,
        "public_evidence_bytes": 20001,
    })}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["unstarted_case_count"] == 5


def test_campaign_public_evidence_exhaustion_no_contact_with_pdb_activity_aborts(manifest, auth, tmp_path, git_state_provider):
    """A zero-contact raw outcome carrying unsupported PDB gate activity is
    not silently normalized into the frozen no-contact terminal: it has no
    valid frozen representation and the campaign aborts."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 0, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route, **{
        "logical_model_calls": 0,
        "provider_process_attempts": 0,
        "valid_directives": 0,
        "pdb_counts": dict(runner.ZERO_PDB_COUNTS, total_gate_decisions=1, rejected_gate_decisions=1),
        "pdb_gate_decisions": [{"allowed": False, "reason": "rejected"}],
        "public_evidence_bytes": 20001,
    })}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert "no valid terminal representation" in record["stop_detail"]
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["unstarted_case_count"] == 5


def test_campaign_public_evidence_exhaustion_no_contact_with_candidate_activity_aborts(manifest, auth, tmp_path, git_state_provider):
    """A zero-contact raw outcome carrying a submitted candidate is not
    silently normalized into the frozen no-contact terminal: it has no valid
    frozen representation and the campaign aborts."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 0, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route, **{
        "logical_model_calls": 0,
        "provider_process_attempts": 0,
        "valid_directives": 0,
        "patch_submissions": 1,
        "public_evidence_bytes": 20001,
    })}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert "no valid terminal representation" in record["stop_detail"]
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["unstarted_case_count"] == 5


def test_campaign_public_evidence_non_integer_counter_is_accounting_violation_and_aborts(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 1, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route,
                                                                                 public_evidence_bytes=20001.5)}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["unstarted_case_count"] == 5


def test_campaign_public_evidence_negative_counter_is_accounting_violation_and_aborts(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 1, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route,
                                                                                 public_evidence_bytes=-1)}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["unstarted_case_count"] == 5


def test_campaign_public_evidence_exhaustion_on_unsupported_shape_still_aborts(manifest, auth, tmp_path, git_state_provider):
    """A static-baseline case after provider contact has no valid frozen
    terminal representation for budget exhaustion; the campaign aborts
    honestly instead of fabricating one."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[1] = {"provider_process_attempts": 1, "outcome": _completed_outcome(manifest, manifest["case_order"][1], route,
                                                                                 public_evidence_bytes=20001)}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"
    assert "no valid terminal representation" in record["stop_detail"]


def test_campaign_pdb_budgets_are_enforced(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    pdb_index = next(i for i, case in enumerate(manifest["case_order"]) if case["policy"] == "pdb-on-uncertainty")
    outcome = _pdb_activity_outcome(manifest, manifest["case_order"][pdb_index], route)
    outcome["pdb_counts"] = dict(outcome["pdb_counts"], allowed_gate_openings=4, total_gate_decisions=4, sessions_started=4)
    outcome["pdb_gate_decisions"] = [{"allowed": True, "reason": "x"}] * 4
    entries[pdb_index] = {"provider_process_attempts": 1, "outcome": outcome}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"


def test_campaign_case_timeout_budget_is_enforced(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 1, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route,
                                                                                 wall_clock_duration_seconds=901)}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "BUDGET_EXCEEDED"


def test_campaign_transport_timeout_is_provider_error(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    outcome = _provider_error_outcome(manifest, manifest["case_order"][0], route, **{
        "terminal_reason_code": "TIMEOUT",
        "terminal_transport_evidence": {
            "final_attempt_classification": "TIMEOUT", "process_exit_code": None, "timed_out": True,
            "provider_error_category": "TIMEOUT", "provider_completed_response": False, "evidence_reference": "synthetic-timeout",
        },
        "retries": 0,
    })
    entries[0] = {"provider_process_attempts": 2, "outcome": outcome}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "COMPLETED"
    assert record["cases"][0]["terminal_status"] == "PROVIDER_ERROR"
    assert record["cases"][0]["terminal_transport_evidence"]["timed_out"] is True


def test_campaign_public_private_boundary_violation_aborts(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 1, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route,
                                                                                 termination_reason="PRIVATE-GOLD-PATCH-LEAKED")}
    record, factory, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries,
                                          git_state_provider=git_state_provider,
                                          private_markers=("PRIVATE-GOLD-PATCH-LEAKED",))
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "PUBLIC_PRIVATE_BOUNDARY_VIOLATION"
    assert not (tmp_path / "attempt-out" / "cases" / "case-01-quixbugs-paired-pilot-v2__quixbugs-find-in-sorted-smoke-v1__pdb-on-uncertainty.json").exists()


def test_campaign_private_evidence_is_classified_separately(manifest, auth, tmp_path, git_state_provider):
    record, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert (output / "private" / "evidence.jsonl").is_file()
    lines = (output / "private" / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 6
    assert all(entry["record_kind"] == "private-case-evidence" for entry in (json.loads(line) for line in lines))
    assert all(entry["execution_commit"] == auth["accepted_campaign_commit"] for entry in (json.loads(line) for line in lines))


def test_campaign_case_not_reported_attempted_without_provider_contact(manifest, auth, tmp_path, git_state_provider):
    record, factory, case_runner, _ = _run_campaign(
        manifest, auth, tmp_path,
        route_evidence=_route_evidence(manifest, billing_route="ZEN", zen_used=True, subscription_entitlement_confirmed=False),
        git_state_provider=git_state_provider,
    )
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert record["cases"][0]["provider_process_attempts"] == 0
    assert record["cases"][0]["logical_model_calls"] == 0
    assert record["counts"]["unstarted_case_count"] == 5


def test_campaign_lifecycle_states_distinguish_outcomes(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[1] = {"provider_process_attempts": 3, "outcome": _invalid_model_outcome(manifest, manifest["case_order"][1], route)}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "completed"
    assert record["case_lifecycle_states"][manifest["case_order"][1]["case_id"]] == "completed"
    assert record["counts"]["provider_process_attempts"] == 1 + 3 + 4
    assert record["counts"]["transport_retries"] == 2
    assert record["counts"]["logical_model_calls"] == 1 + 1 + 4


def test_campaign_unexpected_runner_failure_aborts_honestly(manifest, auth, tmp_path, git_state_provider):
    entries = _completed_entries(manifest)
    entries[1]["runner_raises"] = RuntimeError("synthetic runner crash")
    record, _, _, output = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "UNEXPECTED_CASE_FAILURE"
    assert record["case_lifecycle_states"][manifest["case_order"][1]["case_id"]] == "aborted"
    assert record["case_lifecycle_states"][manifest["case_order"][2]["case_id"]] == "unstarted"
    assert record["counts"]["unstarted_case_count"] == 4
    campaign = json.loads((output / "campaign.json").read_text(encoding="utf-8"))
    assert campaign["status"] == "ABORTED"


def test_campaign_abort_leaves_no_valid_completed_artifact(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[1]["runner_raises"] = RuntimeError("synthetic crash mid-campaign")
    record, _, _, output = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    campaign = json.loads((output / "campaign.json").read_text(encoding="utf-8"))
    assert campaign["status"] == "ABORTED"
    assert campaign["stop_reason"] == "UNEXPECTED_CASE_FAILURE"
    written_cases = sorted(path.name for path in (output / "cases").iterdir())
    assert len(written_cases) == 1
    ledger = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
    entry = next(iter(ledger.values()))
    assert entry["status"] == "ABORTED"
    assert entry["stop_reason"] == "UNEXPECTED_CASE_FAILURE"


def test_campaign_completed_artifact_only_written_at_end(manifest, auth, tmp_path, git_state_provider):
    output = tmp_path / "attempt-out"
    factory = RecordingTransportFactory()
    entries = _completed_entries(manifest)
    case_runner = ScriptedCaseRunner(entries)
    assert not (output / "campaign.json").exists()
    runner.run_campaign(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=factory, case_runner=case_runner,
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    campaign = json.loads((output / "campaign.json").read_text(encoding="utf-8"))
    assert campaign["status"] == "COMPLETED"
    assert campaign["artifact_written"] is True
    assert (output / "terminal-commit.json").is_file()
    assert runner.verify_attempt_package(output, manifest)["consistent"] is True


# ---- crash-safe terminal package commitment ------------------------------------


class _SimulatedProcessDeath(BaseException):
    """A BaseException that bypasses any cleanup, like a forced kill."""


@pytest.mark.parametrize("step", ["after_prepare", "after_campaign_payload", "before_terminal_commit", "during_terminal_commit"])
def test_terminalization_fault_leaves_uncommitted_package(manifest, auth, tmp_path, git_state_provider, step):
    output = tmp_path / "attempt-out"

    def fault(step_name):
        if step_name == step:
            raise runner.LiveRunnerError(f"synthetic terminalization fault at {step_name}")

    with pytest.raises(runner.LiveRunnerError):
        runner.run_campaign(
            manifest, authorization=auth, output_root=output,
            route_evidence_provider=lambda: _route_evidence(manifest),
            transport_factory=RecordingTransportFactory(),
            case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
            git_state_provider=lambda commit: _clean_git_state(commit),
            terminalization_fault=fault,
        )
    # Whatever survived, the package must be explicitly uncommitted: the
    # terminal commitment is absent and verify_attempt_package rejects it.
    assert not (output / "terminal-commit.json").exists()
    with pytest.raises(runner.LiveRunnerError):
        runner.verify_attempt_package(output, manifest)


@pytest.mark.parametrize("step", list(runner.TERMINALIZATION_STEPS))
def test_terminalization_process_death_leaves_uncommitted_package(manifest, auth, tmp_path, git_state_provider, step):
    output = tmp_path / "attempt-out"

    def fault(step_name):
        if step_name == step:
            raise _SimulatedProcessDeath(f"simulated process death at {step_name}")

    with pytest.raises(_SimulatedProcessDeath):
        runner.run_campaign(
            manifest, authorization=auth, output_root=output,
            route_evidence_provider=lambda: _route_evidence(manifest),
            transport_factory=RecordingTransportFactory(),
            case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
            git_state_provider=lambda commit: _clean_git_state(commit),
            terminalization_fault=fault,
        )
    assert not (output / "terminal-commit.json").exists()
    with pytest.raises(runner.LiveRunnerError):
        runner.verify_attempt_package(output, manifest)


def test_confirmed_adversarial_state_is_rejected(manifest, auth, tmp_path):
    """campaign.json = COMPLETED, ledger.json = STARTED, no commitment."""
    output = tmp_path / "attempt-out"
    output.mkdir()
    ledger = runner.AttemptLedger(output / "ledger.json")
    ledger.claim({
        "attempt_identity": auth["campaign_attempt_identity"],
        "authorization_hash": runner.authorization_hash(auth),
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": runner.ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "execution_commit": auth["accepted_campaign_commit"],
        "case_ids": [case["case_id"] for case in manifest["case_order"]],
        "route_binding": {},
        "status": "STARTED",
        "created_at": "2026-08-02T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "output_root": str(output.resolve()),
    })
    fake_campaign = runner._campaign_rejection_record(manifest, auth, output, stop_reason=None, detail=None)
    fake_campaign["status"] = "COMPLETED"
    fake_campaign["stop_reason"] = None
    fake_campaign["commit_state"] = "PREPARED"
    fake_campaign["terminal_commit"] = None
    fake_campaign["campaign_attempt_identity"] = auth["campaign_attempt_identity"]
    (output / "campaign.json").write_text(json.dumps(fake_campaign, sort_keys=True), encoding="utf-8")
    with pytest.raises(runner.LiveRunnerError) as exc:
        runner.verify_attempt_package(output, manifest)
    assert "TERMINAL_COMMIT_MISSING" in str(exc.value)


def test_committed_package_passes_verification(manifest, auth, tmp_path, git_state_provider):
    record, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert record["status"] == "COMPLETED"
    assert record["commit_state"] == "COMMITTED"
    assert record["terminal_commit_record_sha256"]
    result = runner.verify_attempt_package(output, manifest)
    assert result["consistent"] is True
    assert result["terminal_commit"] == "PRESENT"
    commitment = json.loads((output / "terminal-commit.json").read_text(encoding="utf-8"))
    assert commitment["commit_version"] == runner.TERMINAL_COMMIT_VERSION
    assert commitment["attempt_identity"] == auth["campaign_attempt_identity"]
    assert commitment["authorization_hash"] == runner.authorization_hash(auth)
    assert commitment["execution_commit"] == auth["accepted_campaign_commit"]
    assert commitment["intended_terminal_status"] == "COMPLETED"
    assert commitment["manifest_hash"] == pilot.manifest_hash(manifest)
    assert len(commitment["case_inventory"]) == 6


def test_commitment_tampering_is_rejected(manifest, auth, tmp_path, git_state_provider):
    _, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    commit_path = output / "terminal-commit.json"
    commitment = json.loads(commit_path.read_text(encoding="utf-8"))

    def tamper(update):
        changed = dict(commitment)
        changed.update(update)
        commit_path.write_text(json.dumps(changed, sort_keys=True), encoding="utf-8")

    tamper({"campaign_json_sha256": "0" * 64})
    with pytest.raises(runner.LiveRunnerError) as exc:
        runner.verify_attempt_package(output, manifest)
    assert "campaign hash mismatch" in str(exc.value)
    tamper({"ledger_entry_sha256": "0" * 64})
    with pytest.raises(runner.LiveRunnerError) as exc:
        runner.verify_attempt_package(output, manifest)
    assert "ledger-entry hash mismatch" in str(exc.value)
    tamper({"intended_terminal_status": "PARTIAL"})
    with pytest.raises(runner.LiveRunnerError) as exc:
        runner.verify_attempt_package(output, manifest)
    assert "status mismatch" in str(exc.value)
    tamper({"attempt_identity": "quixbugs-paired-pilot-v2-attempt-" + "f" * 64})
    with pytest.raises(runner.LiveRunnerError) as exc:
        runner.verify_attempt_package(output, manifest)
    assert "identity mismatch" in str(exc.value)


def test_interrupted_attempt_is_not_silently_resumed(manifest, auth, tmp_path, git_state_provider):
    output = tmp_path / "attempt-out"

    def fault(step_name):
        if step_name == "before_terminal_commit":
            raise _SimulatedProcessDeath("simulated process death")

    with pytest.raises(_SimulatedProcessDeath):
        runner.run_campaign(
            manifest, authorization=auth, output_root=output,
            route_evidence_provider=lambda: _route_evidence(manifest),
            transport_factory=RecordingTransportFactory(),
            case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
            git_state_provider=lambda commit: _clean_git_state(commit),
            terminalization_fault=fault,
        )
    # A later invocation cannot silently resume or finish the interrupted
    # attempt: the owner gate rejects it with a typed duplicate error.
    later = runner.run_campaign(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    assert later["status"] == "REJECTED"
    assert later["stop_reason"] == "DUPLICATE_ATTEMPT"
    assert not (output / "terminal-commit.json").exists()


def test_validate_campaign_record_rejects_invalidated_case_counted_completed(manifest, auth, tmp_path, git_state_provider):
    record = _scenario_records(manifest, auth, tmp_path, git_state_provider, "completed")
    record["case_lifecycle_states"] = dict(record["case_lifecycle_states"])
    first = manifest["case_order"][0]["case_id"]
    record["case_lifecycle_states"][first] = "authority-invalidated"
    record["counts"] = dict(record["counts"])
    record["counts"]["invalidated_case_count"] = 1
    record["counts"]["completed_case_count"] = 5
    record["authority_invalidated_cases"] = [{
        "case_id": first,
        "original_raw_terminal_outcome": "UNRESOLVED",
        "original_terminal_reason_code": "UNRESOLVED_COMPLETED",
        "authority_failure_reason": "TRACKED_SOURCE_CHANGED",
        "authority_check_record_sha256": "a" * 64,
        "provider_contact_occurred": True,
        "excluded_from_evaluation": True,
        "observed_at": "2026-08-02T00:00:00Z",
    }]
    assert runner.validate_campaign_record(record, manifest) is True
    # Re-classifying the invalidated case as completed must be rejected.
    forged = json.loads(json.dumps(record))
    forged["case_lifecycle_states"][first] = "completed"
    forged["counts"]["completed_case_count"] = 6
    forged["counts"]["invalidated_case_count"] = 0
    with pytest.raises(runner.LiveRunnerError):
        runner.validate_campaign_record(forged, manifest)


# ---- immutable output / no-overwrite -------------------------------------------


def test_duplicate_invocation_keeps_original_campaign_json_unchanged(manifest, auth, tmp_path, git_state_provider):
    first, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert first["status"] == "COMPLETED"
    campaign_before = (output / "campaign.json").read_bytes()
    ledger_before = (output / "ledger.json").read_bytes()
    cases_before = {path.name: path.read_bytes() for path in (output / "cases").iterdir()}
    second = runner.run_campaign(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    assert second["status"] == "REJECTED"
    assert second["stop_reason"] == "DUPLICATE_ATTEMPT"
    assert (output / "campaign.json").read_bytes() == campaign_before
    assert (output / "ledger.json").read_bytes() == ledger_before
    assert {path.name: path.read_bytes() for path in (output / "cases").iterdir()} == cases_before


def test_fresh_authorization_same_output_root_is_rejected(manifest, tmp_path, git_state_provider):
    output = tmp_path / "attempt-out"
    first_auth = _valid_authorization(manifest, output)
    first, _, _, _ = _run_campaign(manifest, first_auth, tmp_path, git_state_provider=git_state_provider)
    assert first["status"] == "COMPLETED"
    campaign_before = (output / "campaign.json").read_bytes()
    second_auth = _valid_authorization(manifest, output)
    second_auth["campaign_attempt_identity"] = "quixbugs-paired-pilot-v2-attempt-" + "e" * 64
    second_auth["operator_authorization_id"] = "test-operator-002"
    record = runner.run_campaign(
        manifest, authorization=second_auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "OUTPUT_ROOT_OWNED"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert (output / "campaign.json").read_bytes() == campaign_before


def test_partial_campaign_evidence_cannot_be_replaced(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[2] = {"provider_process_attempts": 1, "outcome": _cleanup_failure_outcome(manifest, manifest["case_order"][2], route)}
    partial, _, _, output = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert partial["status"] == "PARTIAL"
    campaign_before = (output / "campaign.json").read_bytes()
    cases_before = {path.name: path.read_bytes() for path in (output / "cases").iterdir()}
    later_auth = _valid_authorization(manifest, output)
    later_auth["campaign_attempt_identity"] = "quixbugs-paired-pilot-v2-attempt-" + "f" * 64
    later_auth["operator_authorization_id"] = "test-operator-003"
    record = runner.run_campaign(
        manifest, authorization=later_auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "OUTPUT_ROOT_OWNED"
    assert (output / "campaign.json").read_bytes() == campaign_before
    assert {path.name: path.read_bytes() for path in (output / "cases").iterdir()} == cases_before


def test_no_case_file_can_be_overwritten(manifest, auth, tmp_path, git_state_provider):
    _, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    case_file = next((output / "cases").iterdir())
    original = case_file.read_bytes()
    with pytest.raises(runner.OutputIntegrityError):
        runner.atomic_create_json(case_file, {"replacement": True})
    assert case_file.read_bytes() == original


def test_campaign_json_create_once(manifest, auth, tmp_path, git_state_provider):
    _, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    original = (output / "campaign.json").read_bytes()
    with pytest.raises(runner.OutputIntegrityError):
        runner.atomic_create_json(output / "campaign.json", {"replacement": True})
    assert (output / "campaign.json").read_bytes() == original


def test_fresh_authorization_with_fresh_root_runs(manifest, tmp_path, git_state_provider):
    output = tmp_path / "attempt-out"
    first_auth = _valid_authorization(manifest, output)
    first, _, _, _ = _run_campaign(manifest, first_auth, tmp_path, git_state_provider=git_state_provider)
    assert first["status"] == "COMPLETED"
    fresh_output = tmp_path / "attempt-out-2"
    second_auth = _valid_authorization(manifest, fresh_output)
    second_auth["campaign_attempt_identity"] = "quixbugs-paired-pilot-v2-attempt-" + "e" * 64
    second_auth["operator_authorization_id"] = "test-operator-002"
    record = runner.run_campaign(
        manifest, authorization=second_auth, output_root=fresh_output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    assert record["status"] == "COMPLETED"
    assert (fresh_output / "campaign.json").is_file()


# ---- attempt ledger and no-rerun enforcement -----------------------------------


def test_ledger_rejects_duplicate_attempt(manifest, auth, tmp_path, git_state_provider):
    first, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert first["status"] == "COMPLETED"
    second = runner.run_campaign(
        manifest, authorization=auth, output_root=tmp_path / "attempt-out",
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    assert second["status"] == "REJECTED"
    assert second["stop_reason"] == "DUPLICATE_ATTEMPT"
    assert second["provider_call_proof"]["logical_requests"] == 0


def test_ledger_rejects_crashed_started_attempt_resume(manifest, tmp_path, git_state_provider):
    output = tmp_path / "attempt-out"
    auth = _valid_authorization(manifest, output)
    # Simulate a crashed attempt: the owner record exists (single-winner gate)
    # and the ledger carries a durable STARTED entry.
    runner.claim_output_root(
        output,
        attempt_identity=auth["campaign_attempt_identity"],
        authorization_hash=runner.authorization_hash(auth),
        campaign_manifest_hash=pilot.manifest_hash(manifest),
    )
    ledger = runner.AttemptLedger(output / "ledger.json")
    ledger.claim({
        "attempt_identity": auth["campaign_attempt_identity"],
        "authorization_hash": runner.authorization_hash(auth),
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": runner.ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "execution_commit": auth["accepted_campaign_commit"],
        "case_ids": [case["case_id"] for case in manifest["case_order"]],
        "route_binding": {},
        "status": "STARTED",
        "created_at": "2026-08-02T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "output_root": str(output.resolve()),
    })
    record = runner.run_campaign(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "DUPLICATE_ATTEMPT"
    assert record["provider_call_proof"]["logical_requests"] == 0
    # The crashed STARTED ledger entry is untouched (no silent reclaim).
    ledger_after = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
    assert next(iter(ledger_after.values()))["status"] == "STARTED"


def test_ledger_only_started_entry_without_owner_is_occupied(manifest, tmp_path, git_state_provider):
    output = tmp_path / "attempt-out"
    auth = _valid_authorization(manifest, output)
    ledger = runner.AttemptLedger(output / "ledger.json")
    ledger.claim({
        "attempt_identity": auth["campaign_attempt_identity"],
        "authorization_hash": runner.authorization_hash(auth),
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": runner.ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "execution_commit": auth["accepted_campaign_commit"],
        "case_ids": [case["case_id"] for case in manifest["case_order"]],
        "route_binding": {},
        "status": "STARTED",
        "created_at": "2026-08-02T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "output_root": str(output.resolve()),
    })
    record = runner.run_campaign(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "OUTPUT_ROOT_OCCUPIED"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert not (output / ".attempt-owner").exists()


def test_ledger_prevents_same_authorization_against_changed_manifest(manifest, auth, tmp_path, git_state_provider):
    output = tmp_path / "attempt-out"
    runner.claim_output_root(
        output,
        attempt_identity=auth["campaign_attempt_identity"],
        authorization_hash=runner.authorization_hash(auth),
        campaign_manifest_hash="1" * 64,
    )
    ledger = runner.AttemptLedger(output / "ledger.json")
    ledger.claim({
        "attempt_identity": auth["campaign_attempt_identity"],
        "authorization_hash": runner.authorization_hash(auth),
        "campaign_manifest_hash": "1" * 64,
        "accepted_baseline": runner.ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "execution_commit": auth["accepted_campaign_commit"],
        "case_ids": [case["case_id"] for case in manifest["case_order"]],
        "route_binding": {},
        "status": "PARTIAL",
        "created_at": "2026-08-02T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "output_root": str(output.resolve()),
    })
    record = runner.run_campaign(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "DUPLICATE_ATTEMPT"
    assert record["provider_call_proof"]["logical_requests"] == 0


def test_ledger_rejects_same_authorization_rerun_with_new_manifest_hash(manifest, auth, tmp_path):
    output = tmp_path / "attempt-out"
    ledger = runner.AttemptLedger(output / "ledger.json")
    ledger.claim({
        "attempt_identity": auth["campaign_attempt_identity"],
        "authorization_hash": runner.authorization_hash(auth),
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": runner.ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "execution_commit": auth["accepted_campaign_commit"],
        "case_ids": [case["case_id"] for case in manifest["case_order"]],
        "route_binding": {},
        "status": "COMPLETED",
        "created_at": "2026-08-02T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "output_root": str(output.resolve()),
    })
    with pytest.raises(runner.LiveRunnerError):
        ledger.claim({
            "attempt_identity": auth["campaign_attempt_identity"],
            "authorization_hash": runner.authorization_hash(auth),
            "campaign_manifest_hash": "2" * 64,
            "accepted_baseline": runner.ACCEPTED_BASELINE,
            "planning_baseline_commit": manifest["planning_baseline_commit"],
            "execution_commit": auth["accepted_campaign_commit"],
            "case_ids": [case["case_id"] for case in manifest["case_order"]],
            "route_binding": {},
            "status": "STARTED",
            "created_at": "2026-08-02T00:00:00Z",
            "updated_at": "2026-08-02T00:00:00Z",
            "output_root": str(output.resolve()),
        })


def test_authority_check_stop_blocks_remaining_cases(manifest, auth, tmp_path, git_state_provider, monkeypatch):
    def drifted(manifest, **kwargs):
        return ("MANIFEST_HASH_CHANGED", {
            "identity": "AUTHORITY_CHECK:MANIFEST", "reason_code": "MANIFEST_HASH_CHANGED",
            "evidence_reference": "pre-case-authority:manifest",
            "expected_manifest_hash": pilot.manifest_hash(manifest),
            "observed_manifest_hash": "9" * 64,
            "execution_commit": auth["accepted_campaign_commit"],
        })

    monkeypatch.setattr(runner, "_pre_case_authority_check", drifted)
    record, factory, case_runner, _ = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "MANIFEST_HASH_CHANGED"
    assert record["cases"][0]["terminal_status"] == "BLOCKED"
    assert record["cases"][0]["terminal_reason_code"] == "MANIFEST_HASH_CHANGED"
    assert factory.created == []
    assert case_runner.order_log == []


def test_embedded_ledger_status_matches_ledger_file(manifest, auth, tmp_path, git_state_provider):
    record, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    ledger = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
    entry = next(iter(ledger.values()))
    assert record["ledger"]["status"] == entry["status"] == "COMPLETED"
    assert record["ledger"]["execution_commit"] == entry["execution_commit"] == auth["accepted_campaign_commit"]
    assert record["ledger"]["attempt_identity"] == entry["attempt_identity"]


def test_ledger_finish_failure_leaves_no_completed_artifact(manifest, auth, tmp_path, git_state_provider, monkeypatch):
    def failing_finish(self, entry):
        raise runner.LiveRunnerError("synthetic ledger finalization failure")

    monkeypatch.setattr(runner.AttemptLedger, "finish", failing_finish)
    record, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "LEDGER_FINALIZATION_FAILED"
    assert record["artifact_written"] is False
    ledger = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
    assert next(iter(ledger.values()))["status"] == "STARTED"
    # The package is left explicitly uncommitted: the campaign payload is a
    # non-authoritative PREPARED intermediate and the terminal commitment is
    # absent, so verify_attempt_package rejects it.
    campaign = json.loads((output / "campaign.json").read_text(encoding="utf-8"))
    assert campaign["commit_state"] == "PREPARED"
    assert campaign["terminal_commit"] is None
    assert not (output / "terminal-commit.json").exists()
    with pytest.raises(runner.LiveRunnerError) as exc:
        runner.verify_attempt_package(output, manifest)
    assert "TERMINAL_COMMIT_MISSING" in str(exc.value)


# ---- single-winner claim and concurrency ---------------------------------------


def test_two_process_concurrent_claim_exactly_one_succeeds(tmp_path):
    root = tmp_path / "shared-root"
    root.mkdir()
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    worker = tmp_path / "claim_worker.py"
    worker.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, %r)\n"
        "import quixbugs_live_runner_v2 as runner\n"
        "root = sys.argv[1]\n"
        "identity = sys.argv[2]\n"
        "barrier = Path(sys.argv[3])\n"
        "# Explicit barrier: both processes must observe the pre-claim state\n"
        "# (empty root, no owner) simultaneously before either attempts.\n"
        "Path(barrier, 'ready-' + str(os.getpid())).touch()\n"
        "while len(list(barrier.glob('ready-*'))) < 2:\n"
        "    time.sleep(0.005)\n"
        "entry = {\n"
        "    'attempt_identity': identity,\n"
        "    'authorization_hash': 'h' * 64,\n"
        "    'campaign_manifest_hash': 'm' * 64,\n"
        "    'accepted_baseline': runner.ACCEPTED_BASELINE,\n"
        "    'planning_baseline_commit': 'p' * 40,\n"
        "    'execution_commit': 'e' * 40,\n"
        "    'case_ids': [],\n"
        "    'route_binding': {},\n"
        "    'status': 'STARTED',\n"
        "    'created_at': '2026-08-02T00:00:00Z',\n"
        "    'updated_at': '2026-08-02T00:00:00Z',\n"
        "    'output_root': root,\n"
        "}\n"
        "try:\n"
        "    runner.claim_output_root(root, attempt_identity=identity, authorization_hash='h' * 64, campaign_manifest_hash='m' * 64)\n"
        "    runner.AttemptLedger(root + '/ledger.json').claim(entry)\n"
        "    print('CLAIMED')\n"
        "except runner.SameAttemptClaimError as exc:\n"
        "    print('REJECTED: SAME_ATTEMPT_DUPLICATE')\n"
        "except runner.OutputRootOwnedError as exc:\n"
        "    print('REJECTED: OUTPUT_ROOT_OWNED')\n"
        "except runner.OutputRootOccupiedError as exc:\n"
        "    print('REJECTED: OUTPUT_ROOT_OCCUPIED')\n"
        "except Exception as exc:\n"
        "    print('REJECTED: OTHER:', type(exc).__name__)\n"
        % (str(REPO_ROOT / "scripts"),)
    )
    identity = "quixbugs-paired-pilot-v2-attempt-" + "c" * 64
    processes = [
        subprocess.Popen([sys.executable, str(worker), str(root), identity, str(barrier_dir)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    outputs = [process.communicate(timeout=60)[0] for process in processes]
    claimed = [out for out in outputs if "CLAIMED" in out]
    rejected = [out for out in outputs if out.startswith("REJECTED:")]
    assert len(claimed) == 1, outputs
    assert len(rejected) == 1, outputs
    # The loser always receives a typed claim rejection; for the same attempt
    # identity this is SAME_ATTEMPT_DUPLICATE (or, in a rare scheduling race,
    # the typed OUTPUT_ROOT_OWNED / OUTPUT_ROOT_OCCUPIED conflict — all typed
    # claim rejections that stop before ledger mutation).
    assert (
        "SAME_ATTEMPT_DUPLICATE" in rejected[0]
        or "OUTPUT_ROOT_OWNED" in rejected[0]
        or "OUTPUT_ROOT_OCCUPIED" in rejected[0]
    ), outputs
    ledger = json.loads((root / "ledger.json").read_text(encoding="utf-8"))
    assert len(ledger) == 1
    owner = json.loads((root / ".attempt-owner").read_text(encoding="utf-8"))
    assert owner["attempt_identity"] == identity


def test_owner_record_never_lets_matching_identity_pass(manifest, tmp_path):
    output = tmp_path / "attempt-out"
    auth = _valid_authorization(manifest, output)
    runner.claim_output_root(
        output,
        attempt_identity=auth["campaign_attempt_identity"],
        authorization_hash=runner.authorization_hash(auth),
        campaign_manifest_hash=pilot.manifest_hash(manifest),
    )
    with pytest.raises(runner.SameAttemptClaimError):
        runner.claim_output_root(
            output,
            attempt_identity=auth["campaign_attempt_identity"],
            authorization_hash=runner.authorization_hash(auth),
            campaign_manifest_hash=pilot.manifest_hash(manifest),
        )


def test_owner_record_rejects_different_identity_with_typed_error(manifest, tmp_path):
    output = tmp_path / "attempt-out"
    first_auth = _valid_authorization(manifest, output)
    runner.claim_output_root(
        output,
        attempt_identity=first_auth["campaign_attempt_identity"],
        authorization_hash=runner.authorization_hash(first_auth),
        campaign_manifest_hash=pilot.manifest_hash(manifest),
    )
    second_auth = _valid_authorization(manifest, output)
    second_auth["campaign_attempt_identity"] = "quixbugs-paired-pilot-v2-attempt-" + "e" * 64
    with pytest.raises(runner.OutputRootOwnedError):
        runner.claim_output_root(
            output,
            attempt_identity=second_auth["campaign_attempt_identity"],
            authorization_hash=runner.authorization_hash(second_auth),
            campaign_manifest_hash=pilot.manifest_hash(manifest),
        )


def test_corrupt_owner_record_is_typed_rejection(manifest, tmp_path):
    output = tmp_path / "attempt-out"
    output.mkdir()
    (output / ".attempt-owner").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(runner.OutputRootOwnedError):
        runner.claim_output_root(
            output,
            attempt_identity="quixbugs-paired-pilot-v2-attempt-" + "c" * 64,
            authorization_hash="h" * 64,
            campaign_manifest_hash="m" * 64,
        )


def test_duplicate_invocation_rejected_by_owner_gate_before_ledger(manifest, auth, tmp_path, git_state_provider):
    first, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert first["status"] == "COMPLETED"
    campaign_before = (output / "campaign.json").read_bytes()
    ledger_before = (output / "ledger.json").read_bytes()
    second = runner.run_campaign(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    assert second["status"] == "REJECTED"
    assert second["stop_reason"] == "DUPLICATE_ATTEMPT"
    assert (output / "campaign.json").read_bytes() == campaign_before
    assert (output / "ledger.json").read_bytes() == ledger_before


# ---- occupied output roots -----------------------------------------------------


def _occupied_run(manifest, auth, tmp_path, seed):
    output = tmp_path / "attempt-out"
    output.mkdir()
    seed(output)
    record = runner.run_campaign(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    return record, output


def test_occupied_root_existing_campaign_json(manifest, auth, tmp_path):
    def seed(output):
        (output / "campaign.json").write_text('{"status": "COMPLETED"}', encoding="utf-8")

    record, output = _occupied_run(manifest, auth, tmp_path, seed)
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "OUTPUT_ROOT_OCCUPIED"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert not (output / ".attempt-owner").exists()
    assert (output / "campaign.json").read_text(encoding="utf-8") == '{"status": "COMPLETED"}'
    assert not (output / "cases").exists()


def test_occupied_root_existing_case_directory(manifest, auth, tmp_path):
    def seed(output):
        (output / "cases").mkdir()

    record, output = _occupied_run(manifest, auth, tmp_path, seed)
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "OUTPUT_ROOT_OCCUPIED"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert not (output / ".attempt-owner").exists()


def test_occupied_root_unknown_file(manifest, auth, tmp_path):
    def seed(output):
        (output / "stray.tmp").write_text("x", encoding="utf-8")

    record, output = _occupied_run(manifest, auth, tmp_path, seed)
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "OUTPUT_ROOT_OCCUPIED"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert not (output / ".attempt-owner").exists()


def test_occupied_root_symlink_entry(manifest, auth, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("x", encoding="utf-8")
    try:
        (tmp_path / "attempt-out").mkdir()
        (tmp_path / "attempt-out" / "link").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not supported on this platform")
    output = tmp_path / "attempt-out"
    record = runner.run_campaign(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "OUTPUT_ROOT_OCCUPIED"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert not (output / ".attempt-owner").exists()


def test_occupied_root_rejection_evidence_is_parent_level(manifest, auth, tmp_path):
    def seed(output):
        (output / "campaign.json").write_text("{}", encoding="utf-8")

    record, output = _occupied_run(manifest, auth, tmp_path, seed)
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "OUTPUT_ROOT_OCCUPIED"
    rejection_dir = output.parent / f"rejections-{output.name}"
    assert rejection_dir.is_dir()
    assert any(rejection_dir.iterdir())
    assert sorted(path.name for path in output.iterdir()) == ["campaign.json"]


def test_campaign_artifact_creation_failure_is_honest_output_integrity(manifest, auth, tmp_path, git_state_provider, monkeypatch):
    real_create = runner.atomic_create_json

    def failing_create(path, value):
        if Path(path).name == "campaign.json":
            raise runner.OutputIntegrityError("synthetic campaign.json write failure")
        return real_create(path, value)

    monkeypatch.setattr(runner, "atomic_create_json", failing_create)
    record, factory, case_runner, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "OUTPUT_INTEGRITY_FAILURE"
    assert record["artifact_written"] is False
    assert not (output / "campaign.json").exists()
    ledger = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
    assert next(iter(ledger.values()))["status"] == "ABORTED"
    assert next(iter(ledger.values()))["stop_reason"] == "OUTPUT_INTEGRITY_FAILURE"
    assert len(list((output / "cases").iterdir())) == 6
    assert len(factory.created) == 6


def test_ledger_never_completed_without_matching_campaign_json(manifest, auth, tmp_path, git_state_provider, monkeypatch):
    real_finish = runner.AttemptLedger.finish

    def failing_finish(self, entry):
        if entry.get("status") == "COMPLETED":
            raise runner.LiveRunnerError("synthetic ledger finalization failure on COMPLETED")
        return real_finish(self, entry)

    monkeypatch.setattr(runner.AttemptLedger, "finish", failing_finish)
    record, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "LEDGER_FINALIZATION_FAILED"
    assert record["artifact_written"] is False
    ledger = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
    assert next(iter(ledger.values()))["status"] == "STARTED"
    assert not (output / "terminal-commit.json").exists()
    with pytest.raises(runner.LiveRunnerError) as exc:
        runner.verify_attempt_package(output, manifest)
    assert "TERMINAL_COMMIT_MISSING" in str(exc.value)


# ---- post-case and pre-terminal authority verification -------------------------


def _drift_at_provider(manifest, drift_call):
    calls = {"count": 0}

    def provider(commit):
        calls["count"] += 1
        if calls["count"] == drift_call:
            return runner.GitRepositoryState(head="9" * 40, execution_commit_exists=True,
                                             execution_commit_descends_from_baseline=True,
                                             tracked_working_tree_clean=True, git_index_clean=True)
        return _clean_git_state(commit)

    return provider


def test_drift_during_first_case_stops_campaign(manifest, auth, tmp_path):
    record, factory, case_runner, _ = _run_campaign(manifest, auth, tmp_path, git_state_provider=_drift_at_provider(manifest, 3))
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "TRACKED_SOURCE_CHANGED"
    assert record["cases"][0]["terminal_status"] == "UNRESOLVED"
    for entry in record["cases"][1:]:
        assert entry["terminal_status"] == "BLOCKED"
        assert entry["terminal_reason_code"] == "TRACKED_SOURCE_CHANGED"
    assert record["authority_stop"]["affected_case_id"] == manifest["case_order"][0]["case_id"]
    assert len(case_runner.order_log) == 1
    assert record["provider_call_proof"]["logical_requests"] == 1
    assert record["counts"]["completed_case_count"] == 0
    assert record["counts"]["invalidated_case_count"] == 1
    assert record["counts"]["blocked_case_count"] == 5
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "authority-invalidated"
    assert record["authority_invalidated_cases"][0]["case_id"] == manifest["case_order"][0]["case_id"]
    assert record["authority_invalidated_cases"][0]["provider_contact_occurred"] is True
    assert record["counts"]["provider_process_attempts"] == 1
    assert runner.validate_campaign_record(record, manifest) is True


def test_drift_during_middle_case_stops_campaign(manifest, auth, tmp_path):
    record, _, case_runner, _ = _run_campaign(manifest, auth, tmp_path, git_state_provider=_drift_at_provider(manifest, 7))
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "TRACKED_SOURCE_CHANGED"
    assert [entry["terminal_status"] for entry in record["cases"][:3]] == ["UNRESOLVED", "UNRESOLVED", "UNRESOLVED"]
    for entry in record["cases"][3:]:
        assert entry["terminal_reason_code"] == "TRACKED_SOURCE_CHANGED"
    assert len(case_runner.order_log) == 3
    assert record["counts"]["completed_case_count"] == 2
    assert record["counts"]["invalidated_case_count"] == 1
    assert record["counts"]["blocked_case_count"] == 3
    assert record["case_lifecycle_states"][manifest["case_order"][2]["case_id"]] == "authority-invalidated"
    assert record["authority_invalidated_cases"][0]["case_id"] == manifest["case_order"][2]["case_id"]
    assert record["counts"]["provider_process_attempts"] == 3
    assert runner.validate_campaign_record(record, manifest) is True


def test_drift_during_final_case_prevents_completed(manifest, auth, tmp_path):
    record, _, case_runner, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=_drift_at_provider(manifest, 13))
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "TRACKED_SOURCE_CHANGED"
    assert record["authority_stop"]["affected_case_id"] == manifest["case_order"][5]["case_id"]
    assert len(case_runner.order_log) == 6
    # The authority-invalidated final case is excluded from the completed count.
    assert record["counts"]["completed_case_count"] == 5
    assert record["counts"]["invalidated_case_count"] == 1
    assert record["counts"]["unstarted_case_count"] == 0
    assert record["case_lifecycle_states"][manifest["case_order"][5]["case_id"]] == "authority-invalidated"
    invalidated = record["authority_invalidated_cases"]
    assert len(invalidated) == 1
    assert invalidated[0]["case_id"] == manifest["case_order"][5]["case_id"]
    assert invalidated[0]["original_raw_terminal_outcome"] == "UNRESOLVED"
    assert invalidated[0]["authority_failure_reason"] == "TRACKED_SOURCE_CHANGED"
    assert invalidated[0]["provider_contact_occurred"] is True
    assert invalidated[0]["excluded_from_evaluation"] is True
    # The invalidated case's raw record is preserved as quarantined evidence.
    assert record["cases"][5]["terminal_status"] == "UNRESOLVED"
    persisted = json.loads((output / "campaign.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "PARTIAL"
    assert persisted["counts"]["completed_case_count"] == 5
    assert persisted["counts"]["invalidated_case_count"] == 1
    ledger = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
    assert next(iter(ledger.values()))["status"] == "PARTIAL"
    assert runner.validate_campaign_record(record, manifest) is True
    assert runner.verify_attempt_package(output, manifest)["consistent"] is True


def test_drift_after_final_case_before_terminalization_prevents_completed(manifest, auth, tmp_path):
    record, _, case_runner, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=_drift_at_provider(manifest, 14))
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "TRACKED_SOURCE_CHANGED"
    assert record["authority_stop"]["affected_case_id"] is None
    assert len(case_runner.order_log) == 6
    # All six post-case checks passed; this is a campaign-level pre-terminal
    # authority failure, not drift during the final case.
    assert record["counts"]["completed_case_count"] == 6
    assert record["counts"]["invalidated_case_count"] == 0
    assert record["authority_invalidated_cases"] == []
    persisted = json.loads((output / "campaign.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "PARTIAL"
    assert persisted["authority_stop"]["reason_code"] == "TRACKED_SOURCE_CHANGED"
    ledger = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
    assert next(iter(ledger.values()))["status"] == "PARTIAL"
    assert next(iter(ledger.values()))["stop_reason"] == "TRACKED_SOURCE_CHANGED"
    assert runner.validate_campaign_record(record, manifest) is True
    assert runner.verify_attempt_package(output, manifest)["consistent"] is True


# ---- non-finite numeric evidence and strict JSON -------------------------------


@pytest.mark.parametrize("field,value", [
    ("input_price", float("nan")),
    ("output_price", float("inf")),
    ("provider_reported_cost", float("-inf")),
])
def test_route_evidence_rejects_non_finite_numbers(manifest, auth, field, value):
    evidence = _route_evidence(manifest, **{field: value})
    with pytest.raises(runner.RouteEvidenceInvalid) as exc:
        runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert exc.value.reason == "NON_FINITE_VALUE"


def test_authorization_rejects_non_finite_numbers(manifest, auth):
    changed = copy.deepcopy(auth)
    changed["output_root"] = float("nan")
    assert runner.authorization_failure(changed, manifest) == "NON_FINITE_VALUE"


@pytest.mark.parametrize("field,value", [
    ("provider_reported_cost", float("nan")),
    ("wall_clock_duration_seconds", float("inf")),
    ("prompt_tokens", float("-inf")),
])
def test_case_outcome_rejects_non_finite_numbers(manifest, auth, tmp_path, git_state_provider, field, value):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {"provider_process_attempts": 1, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route, **{field: value})}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "SCHEMA_INCONSISTENCY"
    assert "non-finite" in record["stop_detail"]


def test_nested_non_finite_evidence_rejected(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    outcome = _completed_outcome(manifest, manifest["case_order"][0], route)
    outcome["terminal_transport_evidence"] = dict(outcome["terminal_transport_evidence"], process_exit_code=float("nan"))
    entries[0] = {"provider_process_attempts": 1, "outcome": outcome}
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)
    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "SCHEMA_INCONSISTENCY"


def test_campaign_timing_non_finite_rejected(manifest, auth, tmp_path, git_state_provider):
    output = tmp_path / "attempt-out"
    record = runner.run_campaign(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        transport_factory=RecordingTransportFactory(),
        case_runner=ScriptedCaseRunner(_completed_entries(manifest)),
        git_state_provider=lambda commit: _clean_git_state(commit),
        clock=lambda: float("inf"),
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "NON_FINITE_VALUE"
    assert record["provider_call_proof"]["logical_requests"] == 0
    assert not (output / "campaign.json").exists()
    assert not (output / "ledger.json").exists()
    assert not (output / ".attempt-owner").exists()


def test_atomic_create_json_rejects_non_finite(tmp_path):
    with pytest.raises(runner.OutputIntegrityError):
        runner.atomic_create_json(tmp_path / "x.json", {"cost": float("nan")})
    assert not (tmp_path / "x.json").exists()


def test_ledger_write_rejects_non_finite(tmp_path):
    ledger = runner.AttemptLedger(tmp_path / "ledger.json")
    entry = {
        "attempt_identity": "a", "authorization_hash": "h" * 64, "campaign_manifest_hash": "m" * 64,
        "accepted_baseline": runner.ACCEPTED_BASELINE, "planning_baseline_commit": "p",
        "execution_commit": "e" * 40, "case_ids": [], "route_binding": {},
        "status": "STARTED", "created_at": "x", "updated_at": "x", "output_root": "x",
        "cost": float("inf"),
    }
    with pytest.raises(runner.LiveRunnerError):
        ledger.claim(entry)
    assert not (tmp_path / "ledger.json").exists()


def test_finite_zero_values_are_preserved(manifest, auth):
    evidence = _route_evidence(manifest, input_price=0.0, output_price=0, provider_reported_cost=0.0)
    verdict = runner.run_route_preflight(manifest, auth, lambda: evidence, now=runner._utc_now())
    assert verdict.passed is True
    assert verdict.route_observation["input_price"] == 0.0
    assert verdict.route_observation["provider_reported_cost"] == 0.0


def test_canonical_json_rejects_non_finite():
    with pytest.raises(ValueError):
        runner.canonical_json({"cost": float("nan")})


# ---- campaign consistency validation -------------------------------------------


def _scenario_records(manifest, auth, tmp_path, git_state_provider, scenario):
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    if scenario == "completed":
        return _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)[0]
    if scenario == "partial-cleanup":
        entries[2] = {"provider_process_attempts": 1, "outcome": _cleanup_failure_outcome(manifest, manifest["case_order"][2], route)}
        return _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)[0]
    if scenario == "partial-drift":
        entries[1]["drift"] = "RUNTIME_MODEL_ID_MISMATCH"
        return _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)[0]
    if scenario == "aborted-budget":
        entries[0] = {"provider_process_attempts": 65, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route, logical_model_calls=65, provider_process_attempts=65)}
        return _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)[0]
    if scenario == "aborted-unexpected":
        entries[1]["runner_raises"] = RuntimeError("crash")
        return _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider)[0]
    if scenario == "aborted-sanitization":
        entries[0] = {"provider_process_attempts": 1, "outcome": _completed_outcome(manifest, manifest["case_order"][0], route, termination_reason="MARKER-LEAK")}
        return _run_campaign(manifest, auth, tmp_path, runner_entries=entries, git_state_provider=git_state_provider, private_markers=("MARKER-LEAK",))[0]
    if scenario == "blocked-preprovider":
        return _run_campaign(manifest, auth, tmp_path, route_evidence=_route_evidence(manifest, billing_route="ZEN", zen_used=True, subscription_entitlement_confirmed=False), git_state_provider=git_state_provider)[0]
    raise ValueError(scenario)


@pytest.mark.parametrize("scenario", [
    "completed", "partial-cleanup", "partial-drift", "aborted-budget",
    "aborted-unexpected", "aborted-sanitization", "blocked-preprovider",
])
def test_every_terminal_campaign_record_passes_consistency_validator(manifest, auth, tmp_path, git_state_provider, scenario):
    record = _scenario_records(manifest, auth, tmp_path, git_state_provider, scenario)
    assert runner.validate_campaign_record(record, manifest) is True


def test_campaign_counts_reconcile_with_frozen_case_count(manifest, auth, tmp_path, git_state_provider):
    for scenario in ["completed", "partial-cleanup", "aborted-budget", "blocked-preprovider"]:
        record = _scenario_records(manifest, auth, tmp_path, git_state_provider, scenario)
        counts = record["counts"]
        assert counts["completed_case_count"] + counts["blocked_case_count"] + counts["aborted_case_count"] + counts["unstarted_case_count"] == 6


def test_verify_attempt_package_checks_on_disk_artifacts(manifest, auth, tmp_path, git_state_provider):
    _, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    result = runner.verify_attempt_package(output, manifest)
    assert result["consistent"] is True
    assert result["campaign_status"] == "COMPLETED"
    assert result["case_files_on_disk"] == result["case_records_referenced"] == 6


def test_verify_attempt_package_detects_missing_case_file(manifest, auth, tmp_path, git_state_provider):
    _, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    case_file = next((output / "cases").iterdir())
    case_file.unlink()
    with pytest.raises(runner.LiveRunnerError):
        runner.verify_attempt_package(output, manifest)


def test_verify_attempt_package_detects_corrupt_case_file(manifest, auth, tmp_path, git_state_provider):
    _, _, _, output = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    case_file = next((output / "cases").iterdir())
    case_file.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(runner.LiveRunnerError):
        runner.verify_attempt_package(output, manifest)


def test_validate_campaign_record_rejects_inconsistent_counts(manifest, auth, tmp_path, git_state_provider):
    record = _scenario_records(manifest, auth, tmp_path, git_state_provider, "completed")
    record["counts"]["unstarted_case_count"] = 3
    with pytest.raises(runner.LiveRunnerError):
        runner.validate_campaign_record(record, manifest)


# ---- truthful token and cost semantics -----------------------------------------


def test_campaign_preserves_provider_reported_cost(manifest, auth, tmp_path, git_state_provider):
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert record["cost_summary"]["classification"] == "REPORTED"
    assert record["cost_summary"]["total_provider_reported_cost"] == pytest.approx(6 * 0.0042)
    assert all(entry["provider_reported_cost"] == 0.0042 for entry in record["cases"])


def test_campaign_does_not_force_cost_to_zero_for_subscription(manifest, auth, tmp_path, git_state_provider):
    route = _route_evidence(manifest, provider_reported_cost=0.0)
    entries = [
        {"provider_process_attempts": 1, "outcome": _completed_outcome(manifest, case, route)}
        for case in manifest["case_order"]
    ]
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, runner_entries=entries,
                                    route_evidence=route, git_state_provider=git_state_provider)
    assert record["cost_summary"]["classification"] == "SUBSCRIPTION_COVERED"
    assert record["cost_summary"]["total_provider_reported_cost"] == 0.0
    assert record["cost_summary"]["provider_reported"] is False


def test_campaign_cost_classification_absent_when_no_cases(manifest, auth, tmp_path, git_state_provider):
    record, _, _, _ = _run_campaign(
        manifest, auth, tmp_path,
        route_evidence=_route_evidence(manifest, billing_route="ZEN", zen_used=True, subscription_entitlement_confirmed=False),
        git_state_provider=git_state_provider,
    )
    assert record["cost_summary"]["classification"] == "ABSENT"
    assert record["cost_summary"]["total_provider_reported_cost"] is None


def test_campaign_tokens_are_recorded_truthfully(manifest, auth, tmp_path, git_state_provider):
    record, _, _, _ = _run_campaign(manifest, auth, tmp_path, git_state_provider=git_state_provider)
    assert sum(entry["prompt_tokens"] for entry in record["cases"]) == 6 * 12
    assert sum(entry["completion_tokens"] for entry in record["cases"]) == 6 * 8


# ---- preflight-only mode and CLI -----------------------------------------------


def test_preflight_only_passes_with_zero_provider_calls(manifest, tmp_path, git_state_provider):
    output = tmp_path / "preflight-out"
    auth = _valid_authorization(manifest, output)
    record = runner.run_preflight_only(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        git_state_provider=git_state_provider,
    )
    assert record["status"] == "COMPLETED"
    assert record["preflight"]["passed"] is True
    assert record["provider_call_proof"] == {"transports_created": 0, "process_launches": 0, "logical_requests": 0}
    assert record["plan"]["frozen_case_order"] == [case["case_id"] for case in manifest["case_order"]]
    assert not (output / "cases").exists()
    assert not (output / "campaign.json").exists()
    assert not (output / ".attempt-owner").exists()
    assert (output.parent / f"rejections-{output.name}" / f"preflight-{auth['campaign_attempt_identity']}.json").is_file()


def test_preflight_only_blocks_on_failed_route(manifest, tmp_path, git_state_provider):
    output = tmp_path / "preflight-out"
    auth = _valid_authorization(manifest, output)
    record = runner.run_preflight_only(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest, billing_route="ZEN", zen_used=True, subscription_entitlement_confirmed=False),
        git_state_provider=git_state_provider,
    )
    assert record["status"] == "BLOCKED"
    assert record["preflight"]["failure_category"] == "ZEN_ROUTE_OBSERVED"
    assert record["provider_call_proof"]["logical_requests"] == 0


def test_preflight_only_rejects_invalid_authorization(manifest, tmp_path, git_state_provider):
    output = tmp_path / "preflight-out"
    bad_auth = _valid_authorization(manifest, output)
    bad_auth["expected_billing_route"] = "ZEN"
    record = runner.run_preflight_only(
        manifest, authorization=bad_auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        git_state_provider=git_state_provider,
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "BILLING_ROUTE_MISMATCH"
    assert record["provider_call_proof"]["logical_requests"] == 0


def test_preflight_only_rejects_execution_commit_mismatch(manifest, tmp_path):
    output = tmp_path / "preflight-out"
    auth = _valid_authorization(manifest, output)

    def wrong_head(commit):
        return runner.GitRepositoryState(head="3" * 40, execution_commit_exists=True,
                                         execution_commit_descends_from_baseline=True,
                                         tracked_working_tree_clean=True, git_index_clean=True)

    record = runner.run_preflight_only(
        manifest, authorization=auth, output_root=output,
        route_evidence_provider=lambda: _route_evidence(manifest),
        git_state_provider=wrong_head,
    )
    assert record["status"] == "REJECTED"
    assert record["stop_reason"] == "EXECUTION_COMMIT_MISMATCH"
    assert record["provider_call_proof"]["logical_requests"] == 0


def test_pilot_live_mode_still_fails_closed(manifest, tmp_path):
    with pytest.raises(pilot.PilotError):
        pilot.live(manifest, None)
    with pytest.raises(pilot.PilotError):
        pilot.live(manifest, pilot.MANIFEST_PATH_V2)
    with pytest.raises(pilot.PilotError):
        pilot.live(manifest, tmp_path / "auth.json", None)


def test_pilot_cli_preflight_mode_requires_authorization_and_output(tmp_path, capsys):
    rc = pilot.main(["preflight", "--manifest", str(pilot.MANIFEST_PATH_V2)])
    assert rc == 2
    assert "BLOCKED" in capsys.readouterr().err


def test_pilot_cli_preflight_mode_with_valid_authorization_and_route_evidence(manifest, tmp_path, capsys, monkeypatch):
    output = tmp_path / "cli-preflight"
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps(_valid_authorization(manifest, output)), encoding="utf-8")
    evidence_path = tmp_path / "route-evidence.json"
    evidence_path.write_text(json.dumps(_route_evidence(manifest)), encoding="utf-8")
    monkeypatch.setattr(runner, "real_git_state", lambda commit: _clean_git_state(commit))
    rc = pilot.main(["preflight", "--manifest", str(pilot.MANIFEST_PATH_V2),
                     "--authorization", str(auth_path), "--output", str(output),
                     "--route-evidence-json", str(evidence_path)])
    assert rc == 0
    identity = _valid_authorization(manifest, output)["campaign_attempt_identity"]
    record = json.loads((output.parent / f"rejections-{output.name}" / f"preflight-{identity}.json").read_text(encoding="utf-8"))
    assert record["status"] == "COMPLETED"
    assert record["provider_call_proof"]["logical_requests"] == 0


def test_pilot_cli_template_mode_writes_non_authorizing_template(tmp_path, capsys):
    target = tmp_path / "authorization-template.json"
    rc = pilot.main(["template", "--output", str(target)])
    assert rc == 0
    assert json.loads(target.read_text(encoding="utf-8"))["template"] is True
    assert "template_written" in json.loads(capsys.readouterr().out)


def test_pilot_cli_live_without_authorization_is_blocked(tmp_path, capsys):
    rc = pilot.main(["live", "--manifest", str(pilot.MANIFEST_PATH_V2), "--output", str(tmp_path / "out")])
    assert rc == 2
    assert "BLOCKED" in capsys.readouterr().err


def test_pilot_default_mode_is_still_validation_only(manifest, monkeypatch):
    called = []
    monkeypatch.setattr(pilot, "run_qualification", lambda value: called.append(value))
    assert pilot.main(["--manifest", str(pilot.MANIFEST_PATH_V2)]) == 0
    assert called == []


def test_pilot_plan_and_dry_run_unchanged(manifest):
    assert pilot.plan(manifest)["provider_contacted"] is False
    assert pilot.dry_run(manifest, fail_at=None)["provider_processes_started"] == 0


# ---- v1/v2 validator compatibility ---------------------------------------------


def test_v1_manifest_still_validates_and_v2_compat(v1_manifest, manifest):
    assert pilot.validate_manifest(v1_manifest) == "5d84ea22820ca38ce80dd90a5d36e6f80160220178496950f9b45be41fae19ce"
    assert pilot.validate_manifest(manifest) == "bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171"
    assert v1_manifest["campaign_id"] == "quixbugs-paired-pilot-v1"


def test_runner_rejects_v1_manifest(manifest, auth, tmp_path):
    v1 = pilot.load_manifest(pilot.MANIFEST_PATH)
    with pytest.raises(runner.LiveRunnerError):
        runner.run_campaign(v1, authorization=auth, output_root=tmp_path / "out",
                            route_evidence_provider=lambda: {}, git_state_provider=lambda commit: _clean_git_state(commit))


def test_validator_entrypoint_validates_both_versions():
    from scripts.validate_quixbugs_paired_pilot import TRACKED_MANIFESTS
    assert len(TRACKED_MANIFESTS) == 4
    for path in TRACKED_MANIFESTS:
        pilot.validate_manifest(pilot.load_manifest(path))


def test_authorization_template_binds_frozen_manifest_hash(manifest):
    template = runner.authorization_template()
    assert template["campaign_manifest_hash"] == "bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171"
    assert template["accepted_baseline"] == runner.ACCEPTED_BASELINE
    assert template["protocol"] == "1.3"
    assert template["model"] == "deepseek-v4-flash"
    assert template["provider"] == "OpenCode Go"
    assert template["permitted_case_ids"] == [case["case_id"] for case in manifest["case_order"]]
