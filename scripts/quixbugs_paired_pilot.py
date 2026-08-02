"""Frozen, model-free harness for the QuixBugs paired pilot.

The default path is validation only.  Qualification uses the already accepted
local QuixBugs adapter, verifier, WSL/Bubblewrap boundary, and contained PDB
runtime.  The live path is deliberately fail-closed until a separately
reviewed authorization artifact exists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
MANIFEST_PATH = REPO_ROOT / "research" / "quixbugs" / "PAIRED_PILOT_V1.json"
SOURCE_INTEGRITY_PATH = REPO_ROOT / "research" / "quixbugs" / "QUIXBUGS_SOURCE_INTEGRITY_V1.json"
SOURCE_INTEGRITY_RELATIVE_PATH = "research/quixbugs/QUIXBUGS_SOURCE_INTEGRITY_V1.json"
SOURCE_INTEGRITY_SHA256 = "a3ccf9d083f3405f0811b66c69a5e93d8a347d77b5f8ccb9d168d93102bd1977"
CAMPAIGN_ID = "quixbugs-paired-pilot-v1"
PLANNING_BASELINE_COMMIT = "fe91deb273f485c75ad50f58d0623b947f22631a"
POLICIES = ("static-baseline", "pdb-on-uncertainty")
EXECUTION_KINDS = ("DRY_RUN", "LIVE_CASE")
TERMINAL_STATUSES = (
    "RESOLVED", "UNRESOLVED", "PDB_NOT_REACHED", "INVALID_MODEL_RESPONSE",
    "PROVIDER_ERROR", "INFRASTRUCTURE_ERROR", "BLOCKED",
)
DRY_RUN_BLOCK_REASON_CODES = {"DRY_RUN_ONLY", "SYNTHETIC_MALFORMED_RESPONSE", "INJECTED_INFRASTRUCTURE_FAILURE"}
LIVE_PRE_PROVIDER_REASON_CODES = {
    "PROVIDER_MISMATCH", "MODEL_MISMATCH", "VARIANT_MISMATCH", "PROTOCOL_MISMATCH",
    "MODEL_INACTIVE", "VARIANT_UNAVAILABLE", "NONZERO_PRICING", "PAID_FALLBACK_REQUIRED",
    "ALTERNATE_PROVIDER_REQUIRED", "CATALOG_PREFLIGHT_FAILED", "OPENCODE_VERSION_MISMATCH",
    "MANIFEST_HASH_CHANGED", "QUALIFICATION_CONTRACT_CHANGED", "TRACKED_SOURCE_CHANGED",
    "CAMPAIGN_COMMIT_MISMATCH", "LIVE_AUTHORIZATION_INVALID",
}
CAMPAIGN_STOP_REASON_CODES = {
    "TRANSPORT_EVIDENCE_LOSS", "CONTAINMENT_UNCERTAINTY", "SOURCE_MUTATION", "CLEANUP_FAILURE",
    "VERIFIER_INTEGRITY_FAILURE", "RESULT_SCHEMA_INCONSISTENCY", "TRACKED_SOURCE_CHANGED",
    "MANIFEST_HASH_CHANGED", "QUALIFICATION_CONTRACT_CHANGED",
}
AUTHORIZATION_FAILURE_CODES = {
    "MISSING_AUTHORIZATION", "AUTHORIZATION_FLAG_INVALID", "MANIFEST_MISMATCH",
    "QUALIFICATION_CONTRACT_MISMATCH", "COMMIT_INVALID", "CASE_SET_MISMATCH",
    "ROUTE_MISMATCH", "VERSION_BINDING_MISSING", "CATALOG_BINDING_MISSING",
    "FALLBACK_POLICY_MISMATCH",
}
AUTHORITY_CHECK_IDENTITIES = {
    "AUTHORITY_CHECK:AUTHORIZATION", "AUTHORITY_CHECK:MANIFEST",
    "AUTHORITY_CHECK:QUALIFICATION_CONTRACT", "AUTHORITY_CHECK:TRACKED_SOURCE",
    "AUTHORITY_CHECK:ROUTE", "AUTHORITY_CHECK:CONTAINMENT", "AUTHORITY_CHECK:VERIFIER",
}
INFRASTRUCTURE_STAGE_MATRIX = {
    "pre_provider": {"CONTAINMENT_FAILURE", "WORKSPACE_FAILURE", "ROUTE_PREFLIGHT_FAILURE"},
    "workspace_pre_provider": {"WORKSPACE_FAILURE"},
    "containment_pre_provider": {"CONTAINMENT_FAILURE"},
    "provider_transport": {"TRANSPORT_EVIDENCE_LOSS", "PROVIDER_PROCESS_FAILURE"},
    "controller": {"CONTROLLER_FAILURE", "RESULT_SCHEMA_INCONSISTENCY"},
    "pdb_runtime": {"PDB_RUNTIME_FAILURE", "CONTAINMENT_FAILURE"},
    "verifier": {"VERIFIER_FAILURE", "VERIFIER_INTEGRITY_FAILURE"},
    "cleanup": {"CLEANUP_FAILURE"},
    "evidence_packaging": {"EVIDENCE_PACKAGING_FAILURE", "RESULT_SCHEMA_INCONSISTENCY"},
}
INFRASTRUCTURE_CLASSIFICATIONS = {
    "pre_provider": "PRE_PROVIDER",
    "workspace_pre_provider": "PRE_PROVIDER",
    "containment_pre_provider": "PRE_PROVIDER",
    "provider_transport": "PROVIDER_TRANSPORT",
    "controller": "CONTROLLER",
    "pdb_runtime": "PDB_RUNTIME",
    "verifier": "VERIFIER",
    "cleanup": "CLEANUP",
    "evidence_packaging": "EVIDENCE_PACKAGING",
}
PREFLIGHT_FAILURE_PRECEDENCE = (
    "LIVE_AUTHORIZATION_INVALID", "MANIFEST_HASH_CHANGED", "QUALIFICATION_CONTRACT_CHANGED",
    "TRACKED_SOURCE_CHANGED", "CAMPAIGN_COMMIT_MISMATCH", "PROVIDER_MISMATCH", "MODEL_MISMATCH",
    "VARIANT_MISMATCH", "PROTOCOL_MISMATCH", "OPENCODE_VERSION_MISMATCH", "CATALOG_PREFLIGHT_FAILED",
    "MODEL_INACTIVE", "VARIANT_UNAVAILABLE", "NONZERO_PRICING", "PAID_FALLBACK_REQUIRED",
    "ALTERNATE_PROVIDER_REQUIRED",
)
CAMPAIGN_STOP_EVIDENCE_FIELDS = (
    "reason_code", "trigger_case_id", "pre_case_authority_check_identity", "evidence_reference", "confirmed",
    "expected_evidence_complete", "observed_evidence_complete", "expected_containment_confirmed",
    "observed_containment_confirmed", "expected_source_hash", "observed_source_hash",
    "expected_cleanup_succeeded", "observed_cleanup_succeeded", "expected_verifier_integrity",
    "observed_verifier_integrity", "schema_error_code", "expected_source_authority_hash",
    "observed_source_authority_hash", "expected_manifest_hash", "observed_manifest_hash",
    "expected_qualification_contract_hash", "observed_qualification_contract_hash",
    "trigger_result_sha256", "authority_check_record_sha256",
)
CAMPAIGN_STOP_REASON_FIELDS = {
    "TRANSPORT_EVIDENCE_LOSS": {"expected_evidence_complete", "observed_evidence_complete", "evidence_reference", "trigger_result_sha256"},
    "CONTAINMENT_UNCERTAINTY": {"expected_containment_confirmed", "observed_containment_confirmed", "evidence_reference", "trigger_result_sha256", "authority_check_record_sha256"},
    "SOURCE_MUTATION": {"expected_source_hash", "observed_source_hash", "evidence_reference", "trigger_result_sha256"},
    "CLEANUP_FAILURE": {"expected_cleanup_succeeded", "observed_cleanup_succeeded", "evidence_reference", "trigger_result_sha256"},
    "VERIFIER_INTEGRITY_FAILURE": {"expected_verifier_integrity", "observed_verifier_integrity", "evidence_reference", "trigger_result_sha256", "authority_check_record_sha256"},
    "RESULT_SCHEMA_INCONSISTENCY": {"schema_error_code", "evidence_reference", "trigger_result_sha256"},
    "TRACKED_SOURCE_CHANGED": {"expected_source_authority_hash", "observed_source_authority_hash", "evidence_reference", "authority_check_record_sha256"},
    "MANIFEST_HASH_CHANGED": {"expected_manifest_hash", "observed_manifest_hash", "evidence_reference", "authority_check_record_sha256"},
    "QUALIFICATION_CONTRACT_CHANGED": {"expected_qualification_contract_hash", "observed_qualification_contract_hash", "evidence_reference", "authority_check_record_sha256"},
}
CAMPAIGN_STOP_TRIGGER_COMPATIBILITY = {
    "TRANSPORT_EVIDENCE_LOSS": {"prior_case"},
    "SOURCE_MUTATION": {"prior_case"},
    "CLEANUP_FAILURE": {"prior_case"},
    "RESULT_SCHEMA_INCONSISTENCY": {"prior_case"},
    "CONTAINMENT_UNCERTAINTY": {"prior_case", "authority"},
    "VERIFIER_INTEGRITY_FAILURE": {"prior_case", "authority"},
    "TRACKED_SOURCE_CHANGED": {"authority"},
    "MANIFEST_HASH_CHANGED": {"authority"},
    "QUALIFICATION_CONTRACT_CHANGED": {"authority"},
}
CAMPAIGN_STOP_AUTHORITY_IDENTITIES = {
    "CONTAINMENT_UNCERTAINTY": {"AUTHORITY_CHECK:CONTAINMENT"},
    "VERIFIER_INTEGRITY_FAILURE": {"AUTHORITY_CHECK:VERIFIER"},
    "TRACKED_SOURCE_CHANGED": {"AUTHORITY_CHECK:TRACKED_SOURCE"},
    "MANIFEST_HASH_CHANGED": {"AUTHORITY_CHECK:MANIFEST"},
    "QUALIFICATION_CONTRACT_CHANGED": {"AUTHORITY_CHECK:QUALIFICATION_CONTRACT"},
}
PREFLIGHT_FAILURE_FIELDS = (
    "failure_category", "expected_provider", "observed_provider", "expected_model", "observed_model",
    "expected_variant", "observed_variant", "expected_protocol", "observed_protocol",
    "expected_opencode_version", "observed_opencode_version", "expected_catalog_fingerprint",
    "observed_catalog_fingerprint", "observed_input_price", "observed_output_price",
    "paid_fallback_required", "alternate_provider_required", "expected_manifest_hash",
    "observed_manifest_hash", "expected_qualification_contract_hash", "observed_qualification_contract_hash",
    "expected_source_authority_hash", "observed_source_authority_hash", "expected_campaign_commit",
    "observed_campaign_commit", "authorization_artifact_hash", "authorization_validation_error",
    "observed_active_model_status", "observed_variant_available", "catalog_failure_category",
    "catalog_failure_error", "evidence_reference",
)
PRE_PROVIDER_REASON_FIELDS = {
    "PROVIDER_MISMATCH": {"expected_provider", "observed_provider", "evidence_reference"},
    "MODEL_MISMATCH": {"expected_provider", "observed_provider", "expected_model", "observed_model", "evidence_reference"},
    "VARIANT_MISMATCH": {"expected_provider", "observed_provider", "expected_model", "observed_model", "expected_variant", "observed_variant", "evidence_reference"},
    "PROTOCOL_MISMATCH": {"expected_provider", "observed_provider", "expected_model", "observed_model", "expected_variant", "observed_variant", "expected_protocol", "observed_protocol", "evidence_reference"},
    "MODEL_INACTIVE": {"expected_provider", "observed_provider", "expected_model", "observed_model", "expected_variant", "observed_variant", "expected_protocol", "observed_protocol", "observed_active_model_status", "evidence_reference"},
    "VARIANT_UNAVAILABLE": {"expected_provider", "observed_provider", "expected_model", "observed_model", "expected_variant", "observed_variant", "expected_protocol", "observed_protocol", "observed_active_model_status", "observed_variant_available", "evidence_reference"},
    "NONZERO_PRICING": {"expected_provider", "observed_provider", "expected_model", "observed_model", "expected_variant", "observed_variant", "expected_protocol", "observed_protocol", "observed_active_model_status", "observed_variant_available", "observed_input_price", "observed_output_price", "evidence_reference"},
    "PAID_FALLBACK_REQUIRED": {"expected_provider", "observed_provider", "expected_model", "observed_model", "expected_variant", "observed_variant", "expected_protocol", "observed_protocol", "paid_fallback_required", "evidence_reference"},
    "ALTERNATE_PROVIDER_REQUIRED": {"expected_provider", "observed_provider", "expected_model", "observed_model", "expected_variant", "observed_variant", "expected_protocol", "observed_protocol", "alternate_provider_required", "evidence_reference"},
    "CATALOG_PREFLIGHT_FAILED": {"expected_provider", "observed_provider", "expected_model", "observed_model", "expected_variant", "observed_variant", "expected_protocol", "observed_protocol", "catalog_failure_category", "catalog_failure_error", "evidence_reference"},
    "OPENCODE_VERSION_MISMATCH": {"expected_opencode_version", "observed_opencode_version", "evidence_reference"},
    "MANIFEST_HASH_CHANGED": {"expected_manifest_hash", "observed_manifest_hash", "evidence_reference"},
    "QUALIFICATION_CONTRACT_CHANGED": {"expected_qualification_contract_hash", "observed_qualification_contract_hash", "evidence_reference"},
    "TRACKED_SOURCE_CHANGED": {"expected_source_authority_hash", "observed_source_authority_hash", "evidence_reference"},
    "CAMPAIGN_COMMIT_MISMATCH": {"expected_campaign_commit", "observed_campaign_commit", "evidence_reference"},
    "LIVE_AUTHORIZATION_INVALID": {"authorization_artifact_hash", "authorization_validation_error", "evidence_reference"},
}
EXPECTED_SELECTED = (
    "quixbugs-find-in-sorted-smoke-v1",
    "quixbugs-is-valid-parenthesization-smoke-v1",
    "quixbugs-hanoi-smoke-v1",
)
SCREENING_TASK_IDS = (
    "quixbugs-bucketsort-smoke-v1",
    "quixbugs-find-in-sorted-smoke-v1",
    "quixbugs-flatten-smoke-v1",
    "quixbugs-hanoi-smoke-v1",
    "quixbugs-is-valid-parenthesization-smoke-v1",
    "quixbugs-kheapsort-smoke-v1",
    "quixbugs-kth-smoke-v1",
)
FROZEN_BUDGETS = {
    "max_logical_model_calls": 64,
    "max_transport_attempts_per_logical_call": 3,
    "max_transport_retries_per_logical_call": 2,
    "max_total_provider_process_attempts": 192,
    "max_total_transport_retries": 128,
    "per_call_timeout_seconds": 60,
    "total_case_timeout_seconds": 900,
    "max_accepted_directives": 64,
    "max_malformed_directive_feedback_cycles": 2,
    "max_hypotheses": 3,
    "max_pdb_gate_openings": 3,
    "max_pdb_observations": 3,
    "max_patch_submissions": 1,
    "max_verifier_runs": 20,
    "max_public_evidence_bytes": 20000,
}


class PilotError(ValueError):
    """A fail-closed manifest, evidence, or orchestration error."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(manifest))


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_source_integrity_authority() -> dict[str, Mapping[str, Any]]:
    _require(SOURCE_INTEGRITY_PATH.is_file(), "independent source-integrity authority is missing")
    _require(file_hash(SOURCE_INTEGRITY_PATH) == SOURCE_INTEGRITY_SHA256, "source-integrity authority SHA-256 mismatch")
    try:
        authority = json.loads(SOURCE_INTEGRITY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotError(f"source-integrity authority cannot be loaded: {exc}") from exc
    _require(isinstance(authority, Mapping) and authority.get("schema_version") == "quixbugs-source-integrity-v1", "invalid source-integrity authority schema")
    tasks = authority.get("tasks")
    _require(isinstance(tasks, list) and len(tasks) == 8, "source-integrity authority must contain eight tasks")
    ids = [entry.get("task_id") for entry in tasks]
    _require(all(isinstance(task_id, str) for task_id in ids) and len(set(ids)) == 8, "source-integrity authority task IDs must be unique")
    expected_ids = {"quixbugs-bucketsort-smoke-v1", "quixbugs-find-in-sorted-smoke-v1", "quixbugs-flatten-smoke-v1", "quixbugs-gcd-smoke-v1", "quixbugs-hanoi-smoke-v1", "quixbugs-is-valid-parenthesization-smoke-v1", "quixbugs-kheapsort-smoke-v1", "quixbugs-kth-smoke-v1"}
    _require(set(ids) == expected_ids, "source-integrity authority task set mismatch")
    for entry in tasks:
        for key in ("task_id", "repository", "revision", "implementation_path", "test_path", "buggy_source_sha256", "test_sha256", "authority_record_provenance"):
            _require(key in entry, f"source-integrity authority entry missing {key}")
        for key in ("buggy_source_sha256", "test_sha256"):
            _require(isinstance(entry[key], str) and len(entry[key]) == 64 and all(char in "0123456789abcdef" for char in entry[key]), f"invalid authority {key}")
        _require(entry["repository"] == "https://github.com/jkoppel/QuixBugs" and entry["revision"] == "4257f44b0ff1181dedaedee6a447e133219fcebf", "source-integrity authority route mismatch")
        _require(entry["implementation_path"].startswith("python_programs/") and entry["test_path"].startswith("python_testcases/"), "source-integrity authority path outside QuixBugs")
    return {entry["task_id"]: entry for entry in tasks}


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotError(f"manifest cannot be loaded: {exc}") from exc
    if not isinstance(data, dict):
        raise PilotError("manifest root must be an object")
    return data


def _screening_passes(item: Mapping[str, Any]) -> bool:
    evidence = _screening_evidence_for(item)
    if not isinstance(evidence, Mapping) or evidence.get("screened") is not True:
        return False
    verifier = evidence.get("independent_verifier")
    pdb = evidence.get("pdb")
    decisions = pdb.get("gate_decisions") if isinstance(pdb, Mapping) else None
    if not isinstance(verifier, Mapping) or not isinstance(pdb, Mapping) or not isinstance(decisions, list):
        return False
    allowed = sum(1 for decision in decisions if isinstance(decision, Mapping) and decision.get("allowed") is True)
    rejected = sum(1 for decision in decisions if isinstance(decision, Mapping) and decision.get("allowed") is False)
    return (
        evidence.get("source_hash_before") == item.get("source_sha256") == evidence.get("source_hash_after")
        and evidence.get("test_sha256") == item.get("test_sha256")
        and evidence.get("baseline_reproduced") is True
        and evidence.get("baseline_deterministic_across_two_checks") is True
        and verifier.get("status") == "COMPLETED" and verifier.get("outcome") == "NO_OP"
        and verifier.get("lifecycle_succeeded") is True and verifier.get("cleanup_succeeded") is True
        and verifier.get("canonical_fixture_unchanged") is True and verifier.get("expected_f2p_p2p_behavior") is True
        and pdb.get("verdict") == "REACHABILITY_CASE_PASSED"
        and pdb.get("quixbugs_preflight_authorized") is True and pdb.get("contained_preflight_authorized") is True
        and allowed >= 1 and allowed + rejected == len(decisions)
        and pdb.get("total_gate_decisions") == len(decisions)
        and pdb.get("allowed_gate_openings") == allowed and pdb.get("rejected_gate_decisions") == rejected
        and isinstance(pdb.get("sessions_started"), int) and pdb["sessions_started"] >= 1
        and isinstance(pdb.get("successful_observations"), int) and pdb["successful_observations"] >= 1
        and isinstance(pdb.get("failed_observations"), int) and pdb["failed_observations"] >= 0
        and all(pdb.get(key) is True for key in ("events_valid", "sequence_ok", "launch_plan_present", "runtime_bundle_provenance_present", "diagnostics_empty", "cleanup_succeeded", "canonical_source_unchanged"))
        and evidence.get("owned_workspaces_removed") is True
        and evidence.get("oracle_or_gold_material_in_record") is False
    )


def _screening_evidence_for(item: Mapping[str, Any]) -> Mapping[str, Any]:
    evidence = item.get("screening_evidence")
    if isinstance(evidence, Mapping):
        return evidence
    if item.get("qualification_evidence_ref") != item.get("task_id"):
        return {}
    path = REPO_ROOT / "research" / "quixbugs" / "PAIRED_PILOT_QUALIFICATION_EVIDENCE_V1.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8")).get("screening", [])
    except (OSError, json.JSONDecodeError):
        return {}
    for record in records:
        if record.get("task_id") == item.get("task_id"):
            return record.get("screening_evidence", {})
    return {}


def _qualification_contract_tasks(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the contract task fingerprints derived from tracked manifests.

    The campaign file may freeze the contract, but it is not allowed to
    redefine a task manifest fingerprint or its reviewed probe.  This is the
    non-circular side of the qualification binding.
    """
    authority = _load_source_integrity_authority()
    tasks = []
    for item in sorted(
        (entry for entry in manifest.get("inventory", []) if entry.get("task_id") != "quixbugs-gcd-smoke-v1"),
        key=lambda entry: str(entry.get("task_id")),
    ):
        path = REPO_ROOT / str(item.get("manifest_path", ""))
        _require(path.is_file(), f"qualification task manifest is missing: {path}")
        task_manifest = load_manifest(path)
        target = task_manifest.get("target", {})
        probe = item.get("runtime_probe")
        source_record = authority.get(item.get("task_id"))
        _require(source_record is not None, f"task is absent from source-integrity authority: {item.get('task_id')}")
        tasks.append({
            "task_id": item.get("task_id"),
            "manifest_path": item.get("manifest_path"),
            "task_manifest_fingerprint": sha256_text(canonical_json(task_manifest)),
            "implementation_path": target.get("buggy_path"),
            "test_path": target.get("pytest_path"),
            "source_sha256": source_record.get("buggy_source_sha256"),
            "test_sha256": source_record.get("test_sha256"),
            "runtime_probe": probe,
        })
    return tasks


def qualification_contract_hash(manifest: Mapping[str, Any]) -> str:
    contract = manifest.get("qualification_contract")
    _require(isinstance(contract, Mapping), "qualification contract is missing")
    return sha256_text(canonical_json(contract))


def _validate_verifier_evidence(value: Any, expected_outcome: str, label: str) -> None:
    _require(isinstance(value, Mapping), f"{label} verifier evidence must be an object")
    _require(value.get("status") == "COMPLETED", f"{label} verifier did not complete")
    _require(value.get("outcome") == expected_outcome, f"{label} verifier semantic outcome mismatch")
    for key in ("lifecycle_succeeded", "cleanup_succeeded", "canonical_fixture_unchanged", "expected_f2p_p2p_behavior"):
        _require(value.get(key) is True, f"{label} verifier lifecycle field failed: {key}")
    _require(value.get("timeout") is False and value.get("diagnostic_present") is False, f"{label} verifier has timeout/internal error")


def _validate_deep_screening_evidence(manifest: Mapping[str, Any], record: Mapping[str, Any], contract_task: Mapping[str, Any]) -> None:
    evidence = record.get("screening_evidence")
    _require(isinstance(evidence, Mapping) and evidence.get("screened") is True, f"screening evidence is not complete: {record.get('task_id')}")
    _require(record.get("task_id") == contract_task.get("task_id"), "screening task identity mismatch")
    _require(evidence.get("manifest_fingerprint") == contract_task.get("task_manifest_fingerprint"), f"stale task manifest fingerprint: {record.get('task_id')}")
    _require(evidence.get("source_hash_before") == contract_task.get("source_sha256"), f"screening source_hash_before is not bound: {record.get('task_id')}")
    _require(evidence.get("source_hash_after") == contract_task.get("source_sha256"), f"screening source_hash_after is not bound: {record.get('task_id')}")
    _require(evidence.get("test_sha256") == contract_task.get("test_sha256"), f"screening test_sha256 is not bound: {record.get('task_id')}")
    _require(evidence.get("source_hash_before") == evidence.get("source_hash_after"), f"source was not restored: {record.get('task_id')}")
    probe = evidence.get("runtime_probe")
    frozen_probe = contract_task.get("runtime_probe")
    _require(isinstance(probe, Mapping) and isinstance(frozen_probe, Mapping), "runtime probe evidence is missing")
    _require(probe.get("module_path") == frozen_probe.get("module_path"), f"runtime probe module mismatch: {record.get('task_id')}")
    _require(probe.get("focus_function") == frozen_probe.get("focus_function"), f"runtime probe focus mismatch: {record.get('task_id')}")
    _require(type(probe.get("breakpoint_line")) is int and probe["breakpoint_line"] > 0, "runtime probe breakpoint is unresolved")
    _validate_verifier_evidence(evidence.get("independent_verifier"), "NO_OP", f"{record.get('task_id')} buggy")
    pdb = evidence.get("pdb")
    _require(isinstance(pdb, Mapping), f"PDB evidence is missing: {record.get('task_id')}")
    _require(pdb.get("verdict") == "REACHABILITY_CASE_PASSED", "stored PDB reachability verdict failed")
    _require(pdb.get("quixbugs_preflight_authorized") is True and pdb.get("contained_preflight_authorized") is True, "PDB preflight was not authorized")
    decisions = pdb.get("gate_decisions")
    _require(isinstance(decisions, list), "PDB gate decisions are missing")
    allowed = sum(1 for decision in decisions if isinstance(decision, Mapping) and decision.get("allowed") is True)
    rejected = sum(1 for decision in decisions if isinstance(decision, Mapping) and decision.get("allowed") is False)
    _require(allowed + rejected == len(decisions), "PDB gate decisions contain invalid booleans")
    _require(pdb.get("total_gate_decisions") == len(decisions), "PDB total gate count is not derived")
    _require(pdb.get("allowed_gate_openings") == allowed and pdb.get("rejected_gate_decisions") == rejected, "PDB gate counts do not match events")
    _require(allowed >= 1, "PDB qualification has no allowed gate opening")
    _require(type(pdb.get("sessions_started")) is int and pdb["sessions_started"] >= 1 and pdb["sessions_started"] <= allowed, "PDB lifecycle session count is invalid")
    _require(type(pdb.get("successful_observations")) is int and pdb["successful_observations"] >= 1, "PDB has no successful observation")
    _require(type(pdb.get("failed_observations")) is int and pdb["failed_observations"] >= 0, "PDB failed observation count is invalid")
    for key in ("events_valid", "sequence_ok", "launch_plan_present", "runtime_bundle_provenance_present", "diagnostics_empty", "cleanup_succeeded", "canonical_source_unchanged"):
        _require(pdb.get(key) is True, f"PDB qualification field failed: {key}")
    _require(pdb.get("qualification_passes") is True, "stored qualification_passes is not supported by deep evidence")
    _require(evidence.get("owned_workspaces_removed") is True and evidence.get("oracle_or_gold_material_in_record") is False, "screening privacy or cleanup boundary failed")


def _validate_qualification_evidence(manifest: Mapping[str, Any]) -> None:
    evidence_path = REPO_ROOT / str(manifest.get("qualification_evidence_path", ""))
    _require(evidence_path.is_file(), "qualification evidence file is missing")
    expected_file_hash = manifest.get("qualification_evidence_sha256")
    _require(isinstance(expected_file_hash, str) and file_hash(evidence_path) == expected_file_hash, "qualification evidence SHA-256 mismatch")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotError(f"qualification evidence cannot be loaded: {exc}") from exc
    _require(isinstance(evidence, Mapping), "qualification evidence root must be an object")
    _require(evidence.get("qualification_contract_hash") == manifest.get("qualification_contract_hash") == qualification_contract_hash(manifest), "qualification contract hash mismatch")
    _require(evidence.get("provider_contacted") is False and evidence.get("network_activity") is False, "qualification evidence claims provider or network activity")
    screening = evidence.get("screening")
    _require(isinstance(screening, list) and len(screening) == len(SCREENING_TASK_IDS), "qualification evidence must contain exactly seven screening tasks")
    ids = [entry.get("task_id") for entry in screening]
    _require(ids.count(None) == 0 and set(ids) == set(SCREENING_TASK_IDS) and len(set(ids)) == len(SCREENING_TASK_IDS), "qualification screening task set is not exact")
    contract_tasks = {task["task_id"]: task for task in manifest["qualification_contract"]["tasks"]}
    _require(set(contract_tasks) == set(SCREENING_TASK_IDS), "qualification contract task set is not exact")
    for record in screening:
        _validate_deep_screening_evidence(manifest, record, contract_tasks[record["task_id"]])
    selected = evidence.get("selected_full_qualification")
    _require(isinstance(selected, list) and {entry.get("task_id") for entry in selected} == set(EXPECTED_SELECTED) and len(selected) == 3, "selected private qualification set is not exact")
    for record in selected:
        _validate_verifier_evidence(record.get("private_correct_evaluator"), "RESOLVED", f"{record.get('task_id')} private correct")
        _require(record.get("private_correct_qualification_passes") is True and record.get("gold_oracle_material_in_record") is False, "private correct qualification is not valid or private")
    _require(evidence.get("screened_task_count") == 7, "screened task count is not seven")


def selection_ranking(inventory: list[Mapping[str, Any]], *, allow_unqualified: bool = False) -> list[dict[str, str]]:
    eligible = []
    for item in inventory:
        if item.get("exclusion_status") not in {"ELIGIBLE", "SELECTED"}:
            continue
        checks = (item.get("prior_live_use") == "NO", _screening_passes(item) if not allow_unqualified else all(item.get(key) == "PASS" for key in ("dependency_status", "deterministic_baseline_status", "verifier_status", "source_restoration_status", "contained_pdb_reachability_status")))
        if all(checks):
            task_id = str(item["task_id"])
            eligible.append((sha256_text(f"{CAMPAIGN_ID}:{task_id}"), task_id))
    return [
        {"task_id": task_id, "selection_hash": digest}
        for digest, task_id in sorted(eligible)
    ]


def case_order(selected: list[str]) -> list[dict[str, Any]]:
    cases = []
    for task_id in selected:
        for policy in POLICIES:
            digest = sha256_text(f"{CAMPAIGN_ID}:{task_id}:{policy}")
            cases.append({
                "case_id": f"{CAMPAIGN_ID}:{task_id}:{policy}",
                "task_id": task_id,
                "policy": policy,
                "case_hash": digest,
            })
    for index, case in enumerate(sorted(cases, key=lambda item: item["case_hash"]), 1):
        case["order_index"] = index
    return sorted(cases, key=lambda item: item["order_index"])


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PilotError(message)


class ValidatedCaseResultBinding:
    """Legacy capability type.  Unconditionally non-constructible.

    Trusted cross-case state is validator-owned ledger state; the public
    result validator no longer accepts caller-constructed capabilities.  Any
    construction attempt fails closed.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise PilotError("validated result bindings are validator-owned ledger state and cannot be constructed by callers")


class ValidatedAuthorityCheckBinding:
    """Legacy capability type.  Unconditionally non-constructible.

    Trusted authority-check state is validator-owned ledger state; the public
    result validator no longer accepts caller-constructed capabilities.  Any
    construction attempt fails closed.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise PilotError("authority-check bindings are validator-owned ledger state and cannot be constructed by callers")


class _StoredCaseRecord:
    """Immutable canonical record of one successfully validated case result.

    Only canonical JSON text, its SHA-256, and the frozen identity fields
    captured at validation time are stored.  ``result`` re-creates a fresh
    mapping from the canonical text on every access, so a returned copy can
    never mutate the stored state.  Every consumer must re-verify the digest
    before trusting the record, and the trigger payload must independently
    pass result validation again at every use.
    """

    __slots__ = ("_canonical", "_sha256", "_case_id", "_task_id", "_order_index", "_manifest_hash", "_campaign_commit")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise PilotError("stored validated case records are validator-owned and cannot be constructed by callers")

    def __setattr__(self, name: str, value: Any) -> None:
        raise PilotError("stored validated case records are immutable")

    @property
    def result(self) -> dict[str, Any]:
        return json.loads(self._canonical)

    @property
    def canonical(self) -> str:
        return self._canonical

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def case_id(self) -> str:
        return self._case_id

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def order_index(self) -> int:
        return self._order_index

    @property
    def campaign_manifest_hash(self) -> str:
        return self._manifest_hash

    @property
    def campaign_commit(self) -> str | None:
        return self._campaign_commit

    def _verify_digest(self) -> bool:
        return sha256_text(self._canonical) == self._sha256


class _StoredAuthorityRecord:
    """Immutable canonical record of one typed, validated authority check.

    The record stores only canonical JSON text plus its digest and the frozen
    binding fields captured at registration time.  ``record`` re-creates a
    fresh mapping from the canonical text on every access.  Every consumer
    must re-verify the digest and re-run the typed authority validator at
    every use.
    """

    __slots__ = ("_canonical", "_sha256", "_identity", "_reason_code", "_manifest_hash", "_qualification_contract_hash", "_source_authority_hash")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise PilotError("stored authority-check records are validator-owned and cannot be constructed by callers")

    def __setattr__(self, name: str, value: Any) -> None:
        raise PilotError("stored authority-check records are immutable")

    @property
    def record(self) -> dict[str, Any]:
        return json.loads(self._canonical)

    @property
    def canonical(self) -> str:
        return self._canonical

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def reason_code(self) -> str:
        return self._reason_code

    @property
    def campaign_manifest_hash(self) -> str:
        return self._manifest_hash

    @property
    def qualification_contract_hash(self) -> str:
        return self._qualification_contract_hash

    @property
    def source_authority_hash(self) -> str:
        return self._source_authority_hash

    def _verify_digest(self) -> bool:
        return sha256_text(self._canonical) == self._sha256


class _CaseResultLedger(dict):
    """Validator-owned case-result ledger snapshot.  Not publicly constructible."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise PilotError("case-result ledgers are validator-owned internal state and cannot be constructed by callers")


class _AuthorityCheckLedger(dict):
    """Validator-owned authority-check ledger snapshot.  Not publicly constructible."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise PilotError("authority-check ledgers are validator-owned internal state and cannot be constructed by callers")


def _new_case_result_ledger() -> _CaseResultLedger:
    return _CaseResultLedger.__new__(_CaseResultLedger)


def _new_authority_check_ledger() -> _AuthorityCheckLedger:
    return _AuthorityCheckLedger.__new__(_AuthorityCheckLedger)


def _new_stored_case_record(canonical: str, sha256: str, case_id: str, task_id: str, order_index: int, manifest_hash: str, campaign_commit: str | None) -> _StoredCaseRecord:
    record = _StoredCaseRecord.__new__(_StoredCaseRecord)
    object.__setattr__(record, "_canonical", canonical)
    object.__setattr__(record, "_sha256", sha256)
    object.__setattr__(record, "_case_id", case_id)
    object.__setattr__(record, "_task_id", task_id)
    object.__setattr__(record, "_order_index", order_index)
    object.__setattr__(record, "_manifest_hash", manifest_hash)
    object.__setattr__(record, "_campaign_commit", campaign_commit)
    return record


def _new_stored_authority_record(canonical: str, sha256: str, identity: str, reason_code: str, manifest_hash: str, qualification_contract_hash: str, source_authority_hash: str) -> _StoredAuthorityRecord:
    record = _StoredAuthorityRecord.__new__(_StoredAuthorityRecord)
    object.__setattr__(record, "_canonical", canonical)
    object.__setattr__(record, "_sha256", sha256)
    object.__setattr__(record, "_identity", identity)
    object.__setattr__(record, "_reason_code", reason_code)
    object.__setattr__(record, "_manifest_hash", manifest_hash)
    object.__setattr__(record, "_qualification_contract_hash", qualification_contract_hash)
    object.__setattr__(record, "_source_authority_hash", source_authority_hash)
    return record


class CampaignResultValidator:
    """Validator-owned stateful campaign ledger.

    The validator is the only authority that can record a validated case
    result or a typed authority-check record.  Campaign-stop validation
    resolves trigger results and authority records exclusively from the
    validator's own canonical ledgers; callers can never supply, forge, or
    mutate trusted cross-case state.  Case results must be validated strictly
    in the frozen order, and only results that fully pass validation enter
    the ledger.
    """

    def __init__(self, manifest: Mapping[str, Any], authorization: Mapping[str, Any] | None = None) -> None:
        validate_manifest(manifest)
        self._manifest = manifest
        self._authorization = authorization
        self.__case_ledger: dict[str, _StoredCaseRecord] = {}
        self.__authority_ledger: dict[str, _StoredAuthorityRecord] = {}
        self.__next_order_index = 1

    @property
    def validated_case_records(self) -> Mapping[str, _StoredCaseRecord]:
        """Read-only view of the internal validated case-result ledger."""
        return MappingProxyType(self.__case_ledger)

    @property
    def validated_authority_records(self) -> Mapping[str, _StoredAuthorityRecord]:
        """Read-only view of the internal validated authority-check ledger."""
        return MappingProxyType(self.__authority_ledger)

    def register_authority_checks(self, records: list[Mapping[str, Any]]) -> None:
        """Register typed authority-check records only after strict validation."""
        for record in records:
            reason = record.get("reason_code") if isinstance(record, Mapping) else None
            _validate_authority_check_record_strict(record, self._manifest, reason)
            identity = record["identity"]
            _require(identity not in self.__authority_ledger, "duplicate authority-check identity")
            canonical = canonical_json(dict(record))
            self.__authority_ledger[identity] = _new_stored_authority_record(
                canonical, sha256_text(canonical), identity, reason,
                manifest_hash(self._manifest), self._manifest["qualification_contract_hash"], SOURCE_INTEGRITY_SHA256,
            )

    def validate_result(self, result: Mapping[str, Any], *, authority_ledger: Any = None) -> None:
        """Validate one case result strictly in frozen order and store the validated canonical record."""
        reference_authority: Mapping[str, Any] = self.__authority_ledger if authority_ledger is None else _checked_authority_ledger(authority_ledger)
        self._validate_core(result, self.__case_ledger, reference_authority, strict=True)

    def case_ledger_snapshot(self) -> _CaseResultLedger:
        """Independent snapshot of the validated case-result ledger."""
        snapshot = _new_case_result_ledger()
        snapshot.update(self.__case_ledger)
        return snapshot

    def authority_ledger_snapshot(self) -> _AuthorityCheckLedger:
        """Independent snapshot of the validated authority-check ledger."""
        snapshot = _new_authority_check_ledger()
        snapshot.update(self.__authority_ledger)
        return snapshot

    def _validate_reference(self, result: Mapping[str, Any], prior_ledger: Any = None, authority_ledger: Any = None) -> None:
        """Single-result validation against validator-owned reference ledgers only."""
        reference_case: Mapping[str, Any] = self.__case_ledger
        reference_authority: Mapping[str, Any] = self.__authority_ledger
        if prior_ledger is not None:
            reference_case = _checked_case_ledger(prior_ledger)
        if authority_ledger is not None:
            reference_authority = _checked_authority_ledger(authority_ledger)
        self._validate_core(result, reference_case, reference_authority, strict=False)

    def _validate_core(self, result: Mapping[str, Any], reference_case: Mapping[str, Any], reference_authority: Mapping[str, Any], *, strict: bool) -> None:
        manifest = self._manifest
        authorization = self._authorization
        schema = manifest["outcome_schema"]
        for field in schema["required_fields"]:
            _require(field in result, f"result missing required field: {field}")
        if strict:
            _require(result["order_index"] == self.__next_order_index, "case results must validate strictly in frozen order")
            _require(result["case_id"] not in self.__case_ledger, "duplicate case replacement is rejected")
        elif reference_case:
            _require(result["case_id"] not in reference_case, "duplicate case replacement is rejected")
            _require(all(isinstance(record, _StoredCaseRecord) for record in reference_case.values()), "case-result ledger contains untrusted state")
            _require(result["order_index"] > max(record.order_index for record in reference_case.values()), "out-of-order or replaced case result is rejected")
        _require(result["qualification_contract_hash"] == manifest["qualification_contract_hash"], "case is bound to another qualification contract")
        _require(result["campaign_manifest_hash"] == manifest_hash(manifest), "case is bound to another manifest")
        execution_kind = result["execution_kind"]
        _require(execution_kind in EXECUTION_KINDS, "unknown execution kind")
        _require(result["planning_baseline_commit"] == PLANNING_BASELINE_COMMIT, "planning baseline binding mismatch")
        campaign_commit = result["campaign_commit"]
        _require(campaign_commit is None or (isinstance(campaign_commit, str) and len(campaign_commit) == 40 and all(c in "0123456789abcdef" for c in campaign_commit)), "invalid campaign commit")
        _require(campaign_commit != PLANNING_BASELINE_COMMIT, "planning baseline cannot be a campaign execution commit")
        _require(result["accepted_code_commit"] is None or result["accepted_code_commit"] == campaign_commit, "accepted code commit must equal campaign commit")
        _require(isinstance(result["case_id"], str) and isinstance(result["task_id"], str) and result["policy"] in POLICIES and type(result["order_index"]) is int, "invalid case identity types")
        allow_observed_route_mismatch = result["execution_kind"] == "LIVE_CASE" and result["terminal_status"] == "BLOCKED" and isinstance(result.get("blocked_evidence"), Mapping) and result["blocked_evidence"].get("block_kind") == "live-pre-provider"
        for key in ("provider", "model", "variant"):
            _require(isinstance(result[key], str) and result[key], f"result route identity missing: {key}")
            if not allow_observed_route_mismatch:
                _require(result[key] == manifest["route"][key], f"result route identity mismatch: {key}")
        frozen = {case["case_id"]: case for case in manifest["case_order"]}
        _require(result["case_id"] in frozen, "case ID is not frozen")
        case = frozen[result["case_id"]]
        for key in ("task_id", "policy", "order_index"):
            _require(result[key] == case[key], f"case identity mismatch: {key}")
        _require(result["terminal_status"] in TERMINAL_STATUSES, "unknown terminal status")
        _require(isinstance(result["preflight_failure_evidence"], Mapping) and isinstance(result["campaign_stop_evidence"], Mapping), "structured preflight/campaign-stop evidence is required")
        for key in ("provider", "model", "variant"):
            _require(result[key] == result["route_observation"].get(key), f"top-level route differs from route observation: {key}")
        validate_route_observation(result["route_observation"], manifest["route"], allow_observed_mismatch=allow_observed_route_mismatch)
        for key in ("logical_model_calls", "provider_process_attempts", "retries", "valid_directives", "malformed_directive_rejections", "bounded_directive_feedback_events", "hypotheses_created", "pdb_sessions_started", "successful_pdb_observations", "failed_pdb_observations", "patch_submissions", "verifier_runs", "public_evidence_bytes"):
            _require(type(result[key]) is int and result[key] >= 0, f"invalid non-negative counter: {key}")
        for key in ("baseline_reproduction", "canonical_source_restoration", "owned_workspace_cleanup", "evidence_consistency"):
            _require(type(result[key]) is bool, f"invalid evidence boolean: {key}")
        for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
            _require(type(result[key]) is int and result[key] >= 0, f"invalid token counter: {key}")
        _require(type(result["provider_reported_cost"]) in (int, float) and not isinstance(result["provider_reported_cost"], bool) and result["provider_reported_cost"] >= 0, "invalid provider cost")
        _require(type(result["wall_clock_duration_seconds"]) in (int, float) and not isinstance(result["wall_clock_duration_seconds"], bool) and result["wall_clock_duration_seconds"] >= 0, "invalid duration")
        for key in ("controller_states_visited",):
            _require(isinstance(result[key], list) and all(isinstance(item, str) for item in result[key]), f"invalid list field: {key}")
        _require(isinstance(result["campaign_manifest_hash"], str) and len(result["campaign_manifest_hash"]) == 64 and all(c in "0123456789abcdef" for c in result["campaign_manifest_hash"]), "invalid campaign manifest hash")
        _require(isinstance(result["source_revision"], str) and result["source_revision"], "invalid source revision")
        _require(len(result["source_revision"]) == 40 and all(c in "0123456789abcdef" for c in result["source_revision"]), "invalid source revision")
        _require(isinstance(result["termination_reason"], str) and result["termination_reason"], "invalid termination reason")
        for key in ("source_hash", "public_request_hash", "candidate_hash"):
            value = result[key]
            _require(value is None or (isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)), f"invalid SHA-256 field: {key}")
        _require(result["patch_submissions"] == 0 and result["candidate_hash"] is None or result["patch_submissions"] > 0 and result["candidate_hash"] is not None, "candidate hash must be null without a candidate and present for a submitted patch")
        counts = result["pdb_counts"]
        for key in ("total_gate_decisions", "allowed_gate_openings", "rejected_gate_decisions", "sessions_started", "successful_observations", "failed_observations"):
            _require(type(counts.get(key)) is int and counts[key] >= 0, f"invalid PDB accounting field: {key}")
        _require(counts["total_gate_decisions"] == counts["allowed_gate_openings"] + counts["rejected_gate_decisions"], "PDB gate accounting does not balance")
        _require(result["pdb_sessions_started"] == counts["sessions_started"], "session count mismatch")
        _require(result["successful_pdb_observations"] == counts["successful_observations"], "successful observation count mismatch")
        _require(result["failed_pdb_observations"] == counts["failed_observations"], "failed observation count mismatch")
        _require(isinstance(result["pdb_gate_decisions"], list) and len(result["pdb_gate_decisions"]) == counts["total_gate_decisions"], "gate decision count mismatch")
        _require(all(isinstance(decision, Mapping) and type(decision.get("allowed")) is bool and isinstance(decision.get("reason"), str) for decision in result["pdb_gate_decisions"]), "invalid gate decision type")
        actual_allowed = sum(1 for decision in result["pdb_gate_decisions"] if decision["allowed"] is True)
        actual_rejected = sum(1 for decision in result["pdb_gate_decisions"] if decision["allowed"] is False)
        _require(actual_allowed == counts["allowed_gate_openings"] and actual_rejected == counts["rejected_gate_decisions"], "reported PDB gate counts do not match actual decisions")
        _require(counts["sessions_started"] <= counts["allowed_gate_openings"], "PDB sessions exceed allowed gate openings")
        _require(counts["successful_observations"] + counts["failed_observations"] == 0 or counts["sessions_started"] >= 1, "PDB observation has no started session")
        _require(counts["sessions_started"] == 0 or counts["allowed_gate_openings"] >= 1, "PDB session has no allowed gate")
        _require(isinstance(result["resource_ids"], Mapping) and all(isinstance(key, str) and isinstance(value, str) for key, value in result["resource_ids"].items()), "invalid resource ID map")
        budgets = manifest["budgets"]
        budget_pairs = (("logical_model_calls", "max_logical_model_calls"), ("provider_process_attempts", "max_total_provider_process_attempts"), ("retries", "max_total_transport_retries"), ("valid_directives", "max_accepted_directives"), ("malformed_directive_rejections", "max_malformed_directive_feedback_cycles"), ("bounded_directive_feedback_events", "max_malformed_directive_feedback_cycles"), ("hypotheses_created", "max_hypotheses"), ("patch_submissions", "max_patch_submissions"), ("verifier_runs", "max_verifier_runs"), ("public_evidence_bytes", "max_public_evidence_bytes"))
        for actual, limit in budget_pairs:
            _require(result[actual] <= budgets[limit], f"budget exceeded: {actual}")
        _require(result["provider_process_attempts"] <= result["logical_model_calls"] * budgets["max_transport_attempts_per_logical_call"], "per-call transport-attempt budget exceeded")
        _require(result["retries"] <= result["logical_model_calls"] * budgets["max_transport_retries_per_logical_call"], "per-call transport-retry budget exceeded")
        _require(result["retries"] <= result["provider_process_attempts"], "retries exceed provider process attempts")
        _require(result["valid_directives"] <= result["logical_model_calls"], "valid directives exceed logical calls")
        _require(result["bounded_directive_feedback_events"] >= result["malformed_directive_rejections"], "malformed responses lack bounded feedback")
        _require(result["wall_clock_duration_seconds"] <= budgets["total_case_timeout_seconds"], "case wall-clock timeout exceeded")
        _require(counts["allowed_gate_openings"] <= budgets["max_pdb_gate_openings"], "PDB gate-opening budget exceeded")
        _require(counts["successful_observations"] + counts["failed_observations"] <= budgets["max_pdb_observations"], "PDB observation budget exceeded")
        transport = result["transport_evidence"]
        _require(isinstance(transport, Mapping), "transport evidence must be an object")
        for key in ("completed_response", "malformed_response", "provider_error", "synthetic"):
            _require(type(transport.get(key)) is bool, f"invalid transport evidence field: {key}")
        auth = None
        if execution_kind == "DRY_RUN":
            _require(transport["synthetic"] is True, "DRY_RUN must use synthetic transport evidence")
            _require(result["campaign_commit"] is None and result["accepted_code_commit"] is None, "DRY_RUN cannot claim an execution commit")
            _require(result["public_request_hash"] is None and result["source_hash"] is None, "DRY_RUN cannot claim live request/source identity")
            _require(result["terminal_status"] in {"BLOCKED", "INVALID_MODEL_RESPONSE"}, "DRY_RUN terminal status is not allowed")
        else:
            authorization_invalid_case = allow_observed_route_mismatch and result["terminal_reason_code"] == "LIVE_AUTHORIZATION_INVALID"
            if authorization_invalid_case:
                auth = authorization if isinstance(authorization, Mapping) else None
            else:
                auth = _validate_live_authorization(manifest, authorization)
            _require(transport["synthetic"] is False, "LIVE_CASE cannot use synthetic transport")
            if auth is not None and not authorization_invalid_case:
                _require(result["campaign_commit"] == auth["accepted_campaign_commit"] and result["accepted_code_commit"] == auth["accepted_campaign_commit"], "LIVE_CASE commit binding mismatch")
            else:
                _require(authorization_invalid_case, "LIVE_CASE authorization context is missing")
            campaign_stop = result["terminal_status"] == "BLOCKED" and result["blocked_evidence"].get("block_kind") == "campaign-stop"
            infrastructure_pre_contact = result["terminal_status"] == "INFRASTRUCTURE_ERROR" and result["infrastructure_evidence"].get("stage") in {"pre_provider", "workspace_pre_provider", "containment_pre_provider"}
            source_mutation_infrastructure = result["terminal_status"] == "INFRASTRUCTURE_ERROR" and result["infrastructure_evidence"].get("source_mutation_observed") is True
            if not allow_observed_route_mismatch and not campaign_stop and not infrastructure_pre_contact and not source_mutation_infrastructure:
                _require(result["public_request_hash"] is not None and result["source_hash"] is not None, "LIVE_CASE requires request and source hashes")
                _require(result["source_revision"] == manifest["authority"]["revision"], "LIVE_CASE source revision mismatch")
                entry = next(item for item in manifest["inventory"] if item["task_id"] == result["task_id"])
                _require(result["source_hash"] == entry["source_sha256"], "LIVE_CASE source hash mismatch")
                _require(result["route_observation"]["preflight_success"] is True and result["route_observation"]["active_model_status"] == "ACTIVE" and result["route_observation"]["variant_available"] is True, "LIVE_CASE route preflight did not succeed")
                _require(result["route_observation"]["input_price"] == 0 and result["route_observation"]["output_price"] == 0 and result["provider_reported_cost"] == 0, "LIVE_CASE pricing is not zero")
                _require(result["route_observation"]["opencode_version"] == auth["expected_opencode_version"], "LIVE_CASE OpenCode version is not authorization-bound")
                if auth.get("expected_catalog_fingerprint") is not None:
                    _require(result["route_observation"]["catalog_fingerprint"] == auth["expected_catalog_fingerprint"], "LIVE_CASE catalog fingerprint is not authorization-bound")
            else:
                if allow_observed_route_mismatch or infrastructure_pre_contact:
                    _require(result["public_request_hash"] is None, "pre-provider block cannot claim a public request")
                elif source_mutation_infrastructure:
                    _require(result["public_request_hash"] is not None and result["source_hash"] is not None, "source-mutation infrastructure result lacks identity")
                    _require(result["source_revision"] == manifest["authority"]["revision"], "source-mutation source revision mismatch")
                    _require(result["route_observation"]["preflight_success"] is True and result["route_observation"]["active_model_status"] == "ACTIVE" and result["route_observation"]["variant_available"] is True, "source-mutation route preflight did not succeed")
                    _require(result["route_observation"]["input_price"] == 0 and result["route_observation"]["output_price"] == 0 and result["provider_reported_cost"] == 0, "source-mutation pricing is not zero")
                    _require(result["route_observation"]["opencode_version"] == auth["expected_opencode_version"], "source-mutation OpenCode version is not authorization-bound")
                    _require(result["route_observation"]["catalog_fingerprint"] == auth["expected_catalog_fingerprint"], "source-mutation catalog is not authorization-bound")
                if campaign_stop:
                    _require(result["source_hash"] is None and result["provider"] == manifest["route"]["provider"] and result["model"] == manifest["route"]["model"] and result["variant"] == manifest["route"]["variant"], "campaign-stop identity is not frozen")
        if result["terminal_status"] != "BLOCKED" and transport["synthetic"] is False and not (result["terminal_status"] == "INFRASTRUCTURE_ERROR" and result["infrastructure_evidence"].get("stage") in {"pre_provider", "workspace_pre_provider", "containment_pre_provider"}):
            _require(result["route_observation"]["preflight_success"] is True, "live result lacks successful route preflight")
        _require(result.get("repair_outcome") in {"NO_CANDIDATE", "REJECTED", "RESOLVED"}, "invalid repair outcome")
        verifier = result["independent_verifier_result"]
        _require(isinstance(verifier, Mapping), "independent verifier result must be an object")
        if verifier.get("outcome") == "RESOLVED":
            _require(result["repair_outcome"] == "RESOLVED", "verifier-resolved result must expose repair_outcome RESOLVED")
        elif result["terminal_status"] == "RESOLVED":
            _require(False, "RESOLVED terminal status requires verifier-resolved repair outcome")
        elif result["repair_outcome"] == "RESOLVED":
            _require(result["terminal_status"] == "INFRASTRUCTURE_ERROR", "non-infrastructure result cannot hide a resolved repair")
        _validate_terminal_matrix(result, manifest, execution_kind, transport, verifier, counts, budgets, auth, reference_case, reference_authority)
        if strict:
            canonical = canonical_json(dict(result))
            self.__case_ledger[result["case_id"]] = _new_stored_case_record(
                canonical, sha256_text(canonical), result["case_id"], result["task_id"],
                result["order_index"], result["campaign_manifest_hash"], result["campaign_commit"],
            )
            self.__next_order_index += 1


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    require_screening: bool = True,
    require_qualification_binding: bool = True,
) -> str:
    required = (
        "campaign_id", "campaign_version", "freeze_status", "accepted_baseline",
        "planning_baseline_commit", "authority", "inventory", "selection", "route",
        "budgets", "case_order", "outcome_schema", "stop_rules",
        "public_private_boundary", "campaign_commit_binding", "qualification_contract",
        "qualification_contract_hash", "qualification_evidence_path", "qualification_evidence_sha256",
        "source_integrity_authority",
    )
    for key in required:
        _require(key in manifest, f"manifest missing {key}")
    _require(manifest["campaign_id"] == CAMPAIGN_ID, "campaign ID mismatch")
    _require(manifest["campaign_version"] == 1, "campaign version mismatch")
    _require(manifest["freeze_status"] == "FROZEN_BEFORE_LIVE", "manifest is not frozen")
    _require(manifest["accepted_baseline"] == PLANNING_BASELINE_COMMIT, "planning baseline mismatch")
    _require(manifest["planning_baseline_commit"] == PLANNING_BASELINE_COMMIT, "planning baseline commit mismatch")
    authority = manifest["authority"]
    _require(authority.get("repository") == "https://github.com/jkoppel/QuixBugs", "QuixBugs repository mismatch")
    _require(authority.get("revision") == "4257f44b0ff1181dedaedee6a447e133219fcebf", "QuixBugs revision mismatch")
    source_binding = manifest["source_integrity_authority"]
    _require(source_binding == {"path": SOURCE_INTEGRITY_RELATIVE_PATH, "schema_version": "quixbugs-source-integrity-v1", "sha256": SOURCE_INTEGRITY_SHA256}, "source-integrity authority binding mismatch")
    source_records = _load_source_integrity_authority()
    _require(manifest.get("qualification_contract_hash") == qualification_contract_hash(manifest), "qualification contract digest mismatch")
    contract = manifest["qualification_contract"]
    _require(contract.get("authority") == {"repository": authority["repository"], "revision": authority["revision"]}, "qualification contract authority mismatch")
    inventory = manifest["inventory"]
    _require(isinstance(inventory, list) and len(inventory) == 8, "complete eight-task inventory is required")
    task_ids = [item.get("task_id") for item in inventory]
    _require(len(set(task_ids)) == 8 and all(isinstance(item, str) for item in task_ids), "inventory task IDs must be unique")
    for item in inventory:
        for key in (
            "task_id", "implementation_path", "test_path", "exclusion_status",
            "exclusion_reason", "prior_live_use", "dependency_status",
            "deterministic_baseline_status", "verifier_status",
            "source_restoration_status", "contained_pdb_reachability_status",
        ):
            _require(key in item, f"inventory item missing {key}")
        _require(item["implementation_path"].startswith("python_programs/"), "implementation path is outside QuixBugs")
        _require(item["test_path"].startswith("python_testcases/"), "test path is outside QuixBugs")
        task_manifest_path = REPO_ROOT / item["manifest_path"]
        _require(task_manifest_path.is_file(), f"task manifest is missing: {item['manifest_path']}")
        task_manifest = load_manifest(task_manifest_path)
        _require(task_manifest.get("task_id") == item["task_id"], "task ID disagrees with task manifest")
        _require(task_manifest.get("authority", {}).get("official_repository") == authority.get("repository"), "task repository disagrees with campaign authority")
        _require(task_manifest.get("authority", {}).get("official_repository_revision") == authority.get("revision"), "task revision disagrees with campaign authority")
        target = task_manifest.get("target", {})
        _require(target.get("algorithm") == item.get("algorithm"), "inventory algorithm disagrees with task manifest")
        _require(target.get("buggy_path") == item.get("implementation_path"), "inventory implementation path is stale")
        _require(target.get("pytest_path") == item.get("test_path"), "inventory test path is stale")
        source_record = source_records.get(item["task_id"])
        _require(source_record is not None, f"inventory task is missing from source-integrity authority: {item['task_id']}")
        _require(source_record["repository"] == authority["repository"] and source_record["revision"] == authority["revision"], "source-integrity authority repository/revision mismatch")
        _require(source_record["implementation_path"] == item["implementation_path"] and source_record["test_path"] == item["test_path"], "source-integrity authority path mismatch")
        _require(source_record["buggy_source_sha256"] == item["source_sha256"] and source_record["test_sha256"] == item["test_sha256"], "inventory hash is not bound to source-integrity authority")
        for hash_key in ("source_sha256", "test_sha256"):
            _require(isinstance(item.get(hash_key), str) and len(item[hash_key]) == 64 and all(c in "0123456789abcdef" for c in item[hash_key]), f"invalid {hash_key}")
        if item["prior_live_use"] == "NO" and item["exclusion_status"] != "EXCLUDED":
            if require_screening:
                _require(_screening_passes(item), f"task lacks complete derived screening evidence: {item['task_id']}")
            evidence = _screening_evidence_for(item)
            if require_screening:
                for hash_key in ("source_hash_before", "source_hash_after", "test_sha256"):
                    _require(isinstance(evidence.get(hash_key), str) and len(evidence[hash_key]) == 64 and all(c in "0123456789abcdef" for c in evidence[hash_key]), f"screening evidence missing derived {hash_key}: {item['task_id']}")
                _require(evidence["source_hash_before"] == item["source_sha256"], f"source hash disagrees with screening evidence: {item['task_id']}")
                _require(evidence["source_hash_after"] == item["source_sha256"], f"restored source hash disagrees with inventory: {item['task_id']}")
                _require(evidence["test_sha256"] == item["test_sha256"], f"test hash disagrees with screening evidence: {item['task_id']}")
            for status_key in ("dependency_status", "deterministic_baseline_status", "verifier_status", "source_restoration_status", "contained_pdb_reachability_status"):
                if isinstance(evidence, Mapping) and status_key in evidence:
                    _require(item[status_key] == evidence[status_key], f"manual {status_key} disagrees with screening evidence")
        probe = item.get("runtime_probe")
        if item["prior_live_use"] == "NO":
            _require(isinstance(probe, Mapping), f"missing reviewed runtime probe: {item['task_id']}")
            _require(item.get("qualification_evidence_ref") == item["task_id"], f"missing task-local qualification evidence binding: {item['task_id']}")
    _require(contract.get("tasks") == _qualification_contract_tasks(manifest), "qualification contract task fingerprints or probes are stale")
    _require(isinstance(manifest.get("qualification_evidence_sha256"), str) and len(manifest["qualification_evidence_sha256"]) == 64 and all(c in "0123456789abcdef" for c in manifest["qualification_evidence_sha256"]), "invalid qualification evidence SHA-256")
    if require_qualification_binding:
        _validate_qualification_evidence(manifest)
        for item in inventory:
            if item["task_id"] in SCREENING_TASK_IDS:
                derived = _screening_evidence_for(item)
                _require(all(derived.get(key) == "PASS" for key in ("dependency_status", "deterministic_baseline_status", "verifier_status", "source_restoration_status", "contained_pdb_reachability_status")), f"stored inventory status is not supported by deep qualification evidence: {item['task_id']}")
    selection = manifest["selection"]
    ranking = selection_ranking(inventory, allow_unqualified=not require_screening)
    _require(selection.get("ranking") == ranking, "deterministic eligibility ranking mismatch")
    selected = selection.get("selected_task_ids")
    _require(selected == list(EXPECTED_SELECTED), "selected task set is not frozen to the ranked first three")
    _require(selection.get("selected_count") == 3, "exactly three tasks must be selected")
    _require(len(ranking) == 7, "gcd/prior-live exclusion must leave seven eligible tasks")
    _require(all(item["task_id"] in {entry["task_id"] for entry in ranking[:3]} for item in inventory if item["exclusion_status"] == "SELECTED"), "selected inventory flags disagree with ranking")
    route = manifest["route"]
    _require(route.get("provider") == "OpenCode Zen", "provider route mismatch")
    _require(route.get("model") == "opencode/deepseek-v4-flash-free", "model route mismatch")
    _require(route.get("variant") == "max" and route.get("protocol") == "1.3", "variant/protocol mismatch")
    _require(route.get("require_zero_input_price") and route.get("require_zero_output_price"), "zero pricing requirement missing")
    _require(not route.get("paid_fallback") and not route.get("alternate_provider") and not route.get("ollama_fallback") and not route.get("model_substitution"), "fallback or substitution is enabled")
    _require(manifest["budgets"] == FROZEN_BUDGETS, "frozen budgets mismatch")
    _require(manifest["budgets"]["max_transport_attempts_per_logical_call"] == manifest["budgets"]["max_transport_retries_per_logical_call"] + 1, "per-call transport attempts/retries mismatch")
    order = manifest["case_order"]
    expected_order = case_order(list(EXPECTED_SELECTED))
    _require(order == expected_order, "frozen case order mismatch")
    _require(len(order) == 6 and len({item["case_id"] for item in order}) == 6, "case IDs must be six unique immutable records")
    _require({(item["task_id"], item["policy"]) for item in order} == {(task, policy) for task in EXPECTED_SELECTED for policy in POLICIES}, "case pairing mismatch")
    schema = manifest["outcome_schema"]
    _require(set(schema.get("terminal_statuses", ())) == set(TERMINAL_STATUSES), "terminal status set mismatch")
    _require(schema.get("schema_version") == "quixbugs-paired-pilot-result-v1", "result schema version mismatch")
    _require("repair_outcome" in schema.get("required_fields", ()), "repair outcome is not frozen in the result schema")
    _require(set(schema.get("route_failure_reason_codes", ())) == LIVE_PRE_PROVIDER_REASON_CODES, "preflight failure vocabulary mismatch")
    _require(set(schema.get("campaign_stop_reason_codes", ())) == CAMPAIGN_STOP_REASON_CODES, "campaign-stop vocabulary mismatch")
    _require(set(schema.get("preflight_failure_evidence_fields", ())) == set(PREFLIGHT_FAILURE_FIELDS), "preflight evidence schema mismatch")
    _require(set(schema.get("campaign_stop_evidence_fields", ())) == set(CAMPAIGN_STOP_EVIDENCE_FIELDS), "campaign-stop evidence schema mismatch")
    _require(schema.get("infrastructure_stage_matrix") == {stage: {"allowed_reason_codes": sorted(reasons), "classification": INFRASTRUCTURE_CLASSIFICATIONS[stage]} for stage, reasons in INFRASTRUCTURE_STAGE_MATRIX.items()}, "infrastructure stage matrix mismatch")
    _require(tuple(schema.get("preflight_failure_contract", {}).get("controlling_failure_precedence", ())) == PREFLIGHT_FAILURE_PRECEDENCE, "preflight precedence mismatch")
    boundary = manifest["public_private_boundary"]
    _require(boundary.get("oracle_material_in_public_records") is False, "oracle material must remain private")
    _require(boundary.get("qualification_details_in_provider_requests") is False, "private qualification details must not enter provider requests")
    binding = manifest["campaign_commit_binding"]
    _require(binding.get("planning_baseline_commit") == PLANNING_BASELINE_COMMIT, "campaign binding planning baseline mismatch")
    _require(binding.get("live_authorization_required") is True, "live authorization requirement missing")
    _require(binding.get("accepted_campaign_commit") is None or (isinstance(binding.get("accepted_campaign_commit"), str) and len(binding["accepted_campaign_commit"]) == 40), "invalid accepted campaign commit binding")
    _require(set(binding.get("authorization_fields", ())) >= {"accepted_campaign_commit", "campaign_manifest_hash", "qualification_contract_hash", "permitted_case_ids", "provider", "model", "variant", "protocol", "expected_opencode_version", "expected_catalog_fingerprint", "zero_price_required", "no_fallback_required"}, "live authorization binding is incomplete")
    return manifest_hash(manifest)


def validate_route_observation(observation: Mapping[str, Any], route: Mapping[str, Any], *, allow_observed_mismatch: bool = False) -> None:
    for key in ("provider", "model", "variant", "protocol"):
        _require(isinstance(observation.get(key), str) and observation[key], f"route observation missing {key}")
        if not allow_observed_mismatch:
            _require(observation.get(key) == route.get(key), f"route observation mismatch: {key}")
    required = ("opencode_version", "active_model_status", "variant_available", "catalog_fingerprint", "input_price", "output_price", "paid_fallback_used", "alternate_provider_used", "ollama_used", "preflight_success")
    for key in required:
        _require(key in observation, f"route observation missing {key}")
    _require(observation.get("opencode_version") is None or (isinstance(observation.get("opencode_version"), str) and observation["opencode_version"]), "invalid OpenCode version")
    _require(observation.get("active_model_status") in {"ACTIVE", "NOT_RUN", "INACTIVE"}, "invalid active model status")
    _require(type(observation.get("variant_available")) is bool, "variant availability must be boolean")
    catalog = observation.get("catalog_fingerprint")
    _require(catalog is None or (isinstance(catalog, str) and len(catalog) == 64 and all(c in "0123456789abcdef" for c in catalog)), "invalid catalog fingerprint")
    for key in ("input_price", "output_price"):
        _require(type(observation.get(key)) in (int, float) and not isinstance(observation[key], bool) and observation[key] >= 0, f"invalid {key}")
        if not allow_observed_mismatch:
            _require(observation[key] == 0, "provider pricing is not zero")
    for key in ("paid_fallback_used", "alternate_provider_used", "ollama_used", "preflight_success"):
        _require(observation.get(key) is False or observation.get(key) is True, f"invalid route boolean: {key}")
    if not allow_observed_mismatch:
        _require(observation.get("paid_fallback_used") is False and observation.get("alternate_provider_used") is False and observation.get("ollama_used") is False, "fallback route used")
    if observation.get("preflight_success") is True:
        if not allow_observed_mismatch:
            _require(observation.get("active_model_status") == "ACTIVE" and observation.get("variant_available") is True, "successful preflight lacks active model/variant")
        _require(isinstance(observation.get("opencode_version"), str) and isinstance(catalog, str), "successful preflight lacks version/catalog fingerprint")


def _authorization_failure_category(manifest: Mapping[str, Any], authorization: Mapping[str, Any] | None) -> str | None:
    if authorization is None:
        return "MISSING_AUTHORIZATION"
    if not isinstance(authorization, Mapping):
        return "AUTHORIZATION_FLAG_INVALID"
    if authorization.get("authorize_live") is not True:
        return "AUTHORIZATION_FLAG_INVALID"
    if authorization.get("campaign_manifest_hash") != manifest_hash(manifest):
        return "MANIFEST_MISMATCH"
    if authorization.get("qualification_contract_hash") != manifest.get("qualification_contract_hash"):
        return "QUALIFICATION_CONTRACT_MISMATCH"
    accepted = authorization.get("accepted_campaign_commit")
    if not (isinstance(accepted, str) and len(accepted) == 40 and all(c in "0123456789abcdef" for c in accepted) and accepted != PLANNING_BASELINE_COMMIT):
        return "COMMIT_INVALID"
    if authorization.get("permitted_case_ids") != [case["case_id"] for case in manifest["case_order"]]:
        return "CASE_SET_MISMATCH"
    if any(authorization.get(key) != manifest["route"].get(key) for key in ("provider", "model", "variant", "protocol")):
        return "ROUTE_MISMATCH"
    if not (isinstance(authorization.get("expected_opencode_version"), str) and authorization["expected_opencode_version"]):
        return "VERSION_BINDING_MISSING"
    catalog = authorization.get("expected_catalog_fingerprint")
    if not (isinstance(catalog, str) and len(catalog) == 64 and all(c in "0123456789abcdef" for c in catalog)):
        return "CATALOG_BINDING_MISSING"
    if "catalog_binding_procedure" in authorization:
        return "CATALOG_BINDING_MISSING"
    if authorization.get("zero_price_required") is not True or authorization.get("no_fallback_required") is not True:
        return "FALLBACK_POLICY_MISMATCH"
    return None


def _derive_preflight_failure_category(evidence: Mapping[str, Any], result: Mapping[str, Any], manifest: Mapping[str, Any], authorization: Mapping[str, Any] | None) -> str | None:
    auth_failure = _authorization_failure_category(manifest, authorization)
    if auth_failure is not None:
        return "LIVE_AUTHORIZATION_INVALID"
    observed = result["route_observation"]
    expected = manifest["route"]
    if evidence.get("expected_manifest_hash") != evidence.get("observed_manifest_hash") and evidence.get("expected_manifest_hash") is not None and evidence.get("observed_manifest_hash") is not None:
        return "MANIFEST_HASH_CHANGED"
    if evidence.get("expected_qualification_contract_hash") != evidence.get("observed_qualification_contract_hash") and evidence.get("expected_qualification_contract_hash") is not None and evidence.get("observed_qualification_contract_hash") is not None:
        return "QUALIFICATION_CONTRACT_CHANGED"
    if evidence.get("expected_source_authority_hash") != evidence.get("observed_source_authority_hash") and evidence.get("expected_source_authority_hash") is not None and evidence.get("observed_source_authority_hash") is not None:
        return "TRACKED_SOURCE_CHANGED"
    if evidence.get("expected_campaign_commit") != evidence.get("observed_campaign_commit") and evidence.get("expected_campaign_commit") is not None and evidence.get("observed_campaign_commit") is not None:
        return "CAMPAIGN_COMMIT_MISMATCH"
    if observed["provider"] != expected["provider"]:
        return "PROVIDER_MISMATCH"
    if observed["model"] != expected["model"]:
        return "MODEL_MISMATCH"
    if observed["variant"] != expected["variant"]:
        return "VARIANT_MISMATCH"
    if observed["protocol"] != expected["protocol"]:
        return "PROTOCOL_MISMATCH"
    expected_version = authorization.get("expected_opencode_version") if authorization else evidence.get("expected_opencode_version")
    if isinstance(expected_version, str) and expected_version and isinstance(observed.get("opencode_version"), str) and observed["opencode_version"] and observed["opencode_version"] != expected_version:
        return "OPENCODE_VERSION_MISMATCH"
    if evidence.get("catalog_failure_category") is not None or evidence.get("catalog_failure_error") is not None:
        return "CATALOG_PREFLIGHT_FAILED"
    if observed["active_model_status"] != "ACTIVE":
        return "MODEL_INACTIVE"
    if observed["variant_available"] is False:
        return "VARIANT_UNAVAILABLE"
    if observed["input_price"] != 0 or observed["output_price"] != 0:
        return "NONZERO_PRICING"
    if observed["paid_fallback_used"] is True:
        return "PAID_FALLBACK_REQUIRED"
    if observed["alternate_provider_used"] is True or observed["ollama_used"] is True:
        return "ALTERNATE_PROVIDER_REQUIRED"
    return None


def _validate_preflight_failure_evidence(evidence: Mapping[str, Any], result: Mapping[str, Any], manifest: Mapping[str, Any], authorization: Mapping[str, Any] | None) -> None:
    _require(all(field in evidence for field in PREFLIGHT_FAILURE_FIELDS), "preflight failure evidence is incomplete")
    category = evidence.get("failure_category")
    _require(category in LIVE_PRE_PROVIDER_REASON_CODES, "unknown preflight failure category")
    _require(category == result["terminal_reason_code"], "preflight failure category mismatch")
    _require(category == _derive_preflight_failure_category(evidence, result, manifest, authorization), "preflight failure reason is not the derived controlling failure")
    _require(isinstance(evidence.get("evidence_reference"), str) and evidence["evidence_reference"], "preflight failure evidence reference is missing")
    relevant = PRE_PROVIDER_REASON_FIELDS[category]
    for field in PREFLIGHT_FAILURE_FIELDS:
        if field not in relevant and field != "failure_category":
            _require(evidence.get(field) is None, f"preflight evidence has unrelated populated field: {field}")
    observed = result["route_observation"]
    expected_route = manifest["route"]
    route_keys = ("provider", "model", "variant", "protocol")
    for key in route_keys:
        expected_key = f"expected_{key}"
        observed_key = f"observed_{key}"
        if expected_key in relevant:
            _require(evidence[expected_key] == expected_route[key], f"preflight expected {key} is not frozen")
            _require(evidence[observed_key] == observed[key], f"preflight observed {key} is not route-bound")
    if category == "PROVIDER_MISMATCH":
        _require(observed["provider"] != expected_route["provider"] and all(observed[key] == expected_route[key] for key in ("model", "variant", "protocol")), "provider mismatch predicate is not unique")
    elif category == "MODEL_MISMATCH":
        _require(observed["provider"] == expected_route["provider"] and observed["model"] != expected_route["model"] and observed["variant"] == expected_route["variant"] and observed["protocol"] == expected_route["protocol"], "model mismatch predicate is not unique")
    elif category == "VARIANT_MISMATCH":
        _require(observed["provider"] == expected_route["provider"] and observed["model"] == expected_route["model"] and observed["variant"] != expected_route["variant"] and observed["protocol"] == expected_route["protocol"], "variant mismatch predicate is not unique")
    elif category == "PROTOCOL_MISMATCH":
        _require(all(observed[key] == expected_route[key] for key in ("provider", "model", "variant")) and observed["protocol"] != expected_route["protocol"], "protocol mismatch predicate is not unique")
    elif category == "MODEL_INACTIVE":
        _require(all(observed[key] == expected_route[key] for key in route_keys) and evidence["observed_active_model_status"] == observed["active_model_status"] != "ACTIVE" and observed["input_price"] == 0 and observed["output_price"] == 0 and observed["paid_fallback_used"] is False and observed["alternate_provider_used"] is False, "model inactive predicate is not unique")
    elif category == "VARIANT_UNAVAILABLE":
        _require(all(observed[key] == expected_route[key] for key in route_keys) and observed["active_model_status"] == "ACTIVE" and observed["variant_available"] is False and observed["input_price"] == 0 and observed["output_price"] == 0 and observed["paid_fallback_used"] is False and observed["alternate_provider_used"] is False and evidence["observed_active_model_status"] == "ACTIVE" and evidence["observed_variant_available"] is False, "variant unavailable predicate is not unique")
    elif category == "NONZERO_PRICING":
        _require(all(observed[key] == expected_route[key] for key in route_keys) and observed["active_model_status"] == "ACTIVE" and observed["variant_available"] is True and (observed["input_price"] != 0 or observed["output_price"] != 0), "nonzero pricing predicate is not unique")
        _require(evidence["observed_input_price"] == observed["input_price"] and evidence["observed_output_price"] == observed["output_price"], "pricing evidence is not route-bound")
    elif category == "PAID_FALLBACK_REQUIRED":
        _require(all(observed[key] == expected_route[key] for key in route_keys) and observed["paid_fallback_used"] is True and evidence["paid_fallback_required"] is True, "paid fallback predicate is not unique")
    elif category == "ALTERNATE_PROVIDER_REQUIRED":
        _require(all(observed[key] == expected_route[key] for key in route_keys) and observed["alternate_provider_used"] is True and evidence["alternate_provider_required"] is True, "alternate provider predicate is not unique")
    elif category == "CATALOG_PREFLIGHT_FAILED":
        _require(all(observed[key] == expected_route[key] for key in route_keys) and observed["active_model_status"] == "NOT_RUN" and observed["variant_available"] is False and observed["input_price"] == 0 and observed["output_price"] == 0 and observed["paid_fallback_used"] is False and observed["alternate_provider_used"] is False and isinstance(evidence["catalog_failure_category"], str) and evidence["catalog_failure_category"] and isinstance(evidence["catalog_failure_error"], str) and evidence["catalog_failure_error"], "catalog failure requires NOT_RUN route and explicit category/error evidence")
    elif category == "OPENCODE_VERSION_MISMATCH":
        _require(all(observed[key] == expected_route[key] for key in route_keys), "OpenCode version mismatch has a route mismatch")
        _require(isinstance(evidence["expected_opencode_version"], str) and evidence["expected_opencode_version"] and isinstance(evidence["observed_opencode_version"], str) and evidence["observed_opencode_version"] and evidence["expected_opencode_version"] != evidence["observed_opencode_version"], "OpenCode version mismatch lacks distinct expected/observed versions")
        if authorization is not None and authorization.get("expected_opencode_version") is not None:
            _require(evidence["expected_opencode_version"] == authorization["expected_opencode_version"], "OpenCode version does not match authorization")
    elif category == "MANIFEST_HASH_CHANGED":
        _require(all(observed[key] == expected_route[key] for key in route_keys), "manifest-change evidence has a route mismatch")
        _require(evidence["expected_manifest_hash"] == manifest_hash(manifest) and isinstance(evidence["observed_manifest_hash"], str) and len(evidence["observed_manifest_hash"]) == 64 and evidence["observed_manifest_hash"] != evidence["expected_manifest_hash"], "manifest-change evidence is not exact")
    elif category == "QUALIFICATION_CONTRACT_CHANGED":
        _require(all(observed[key] == expected_route[key] for key in route_keys), "qualification-contract-change evidence has a route mismatch")
        _require(evidence["expected_qualification_contract_hash"] == manifest["qualification_contract_hash"] and isinstance(evidence["observed_qualification_contract_hash"], str) and len(evidence["observed_qualification_contract_hash"]) == 64 and evidence["observed_qualification_contract_hash"] != evidence["expected_qualification_contract_hash"], "qualification-contract-change evidence is not exact")
    elif category == "TRACKED_SOURCE_CHANGED":
        _require(all(observed[key] == expected_route[key] for key in route_keys), "tracked-source-change evidence has a route mismatch")
        _require(evidence["expected_source_authority_hash"] == SOURCE_INTEGRITY_SHA256 and isinstance(evidence["observed_source_authority_hash"], str) and len(evidence["observed_source_authority_hash"]) == 64 and evidence["observed_source_authority_hash"] != evidence["expected_source_authority_hash"], "tracked-source-change evidence is not exact")
    elif category == "CAMPAIGN_COMMIT_MISMATCH":
        _require(all(observed[key] == expected_route[key] for key in route_keys), "campaign-commit evidence has a route mismatch")
        expected = authorization.get("accepted_campaign_commit") if authorization else None
        _require(isinstance(expected, str) and evidence["expected_campaign_commit"] == expected and isinstance(evidence["observed_campaign_commit"], str) and evidence["observed_campaign_commit"] != expected, "campaign-commit evidence is not exact")
    elif category == "LIVE_AUTHORIZATION_INVALID":
        _require(all(observed[key] == expected_route[key] for key in route_keys), "authorization-invalid evidence has a route mismatch")
        derived = _authorization_failure_category(manifest, authorization)
        _require(derived is not None and evidence["authorization_validation_error"] == derived, "authorization-invalid evidence does not match the derived failure")
        if derived == "MISSING_AUTHORIZATION":
            _require(authorization is None and evidence["authorization_artifact_hash"] is None, "missing authorization must not claim an artifact hash")
        else:
            _require(isinstance(authorization, Mapping) and isinstance(evidence["authorization_artifact_hash"], str) and len(evidence["authorization_artifact_hash"]) == 64 and evidence["authorization_artifact_hash"] == sha256_text(canonical_json(authorization)), "authorization artifact digest is not bound")


def result_sha256(result: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(result))


def _validate_trigger_result_supports_reason(trigger: Mapping[str, Any], reason: str, evidence: Mapping[str, Any]) -> None:
    if reason == "SOURCE_MUTATION":
        infra = trigger.get("infrastructure_evidence", {})
        _require(trigger.get("terminal_status") == "INFRASTRUCTURE_ERROR" and infra.get("source_mutation_observed") is True, "source mutation trigger lacks typed source-integrity evidence")
        _require(trigger.get("source_hash") == evidence["observed_source_hash"], "source mutation observed hash is not the trigger result hash")
        _require(trigger.get("source_hash") != evidence["expected_source_hash"] and infra.get("expected_source_hash") == evidence["expected_source_hash"], "source mutation trigger did not observe the expected mutation")
    elif reason == "TRANSPORT_EVIDENCE_LOSS":
        infra = trigger.get("infrastructure_evidence", {})
        _require(trigger.get("terminal_status") == "INFRASTRUCTURE_ERROR" and infra.get("stage") == "provider_transport" and infra.get("reason_code") == reason, "transport-loss trigger result does not support the stop")
    elif reason == "CLEANUP_FAILURE":
        infra = trigger.get("infrastructure_evidence", {})
        _require(trigger.get("terminal_status") == "INFRASTRUCTURE_ERROR" and infra.get("stage") == "cleanup" and infra.get("reason_code") == reason, "cleanup trigger result does not support the stop")
    elif reason == "RESULT_SCHEMA_INCONSISTENCY":
        infra = trigger.get("infrastructure_evidence", {})
        _require(trigger.get("terminal_status") == "INFRASTRUCTURE_ERROR" and infra.get("reason_code") == reason, "schema trigger result does not support the stop")
    elif reason == "CONTAINMENT_UNCERTAINTY":
        infra = trigger.get("infrastructure_evidence", {})
        _require(trigger.get("terminal_status") == "INFRASTRUCTURE_ERROR" and infra.get("reason_code") in {"CONTAINMENT_FAILURE", reason}, "containment trigger result does not support the stop")
    elif reason == "VERIFIER_INTEGRITY_FAILURE":
        infra = trigger.get("infrastructure_evidence", {})
        _require(trigger.get("terminal_status") == "INFRASTRUCTURE_ERROR" and infra.get("reason_code") in {"VERIFIER_INTEGRITY_FAILURE", "VERIFIER_FAILURE"}, "verifier trigger result does not support the stop")


def _checked_case_ledger(prior_ledger: Any) -> Mapping[str, Any]:
    if prior_ledger is None:
        return {}
    if isinstance(prior_ledger, _CaseResultLedger):
        return prior_ledger
    if len(prior_ledger) == 0:
        return {}
    raise PilotError("prior results must be validator-owned ledger state; caller-supplied mappings are rejected")


def _checked_authority_ledger(authority_ledger: Any) -> Mapping[str, Any]:
    if authority_ledger is None:
        return {}
    if isinstance(authority_ledger, _AuthorityCheckLedger):
        return authority_ledger
    if len(authority_ledger) == 0:
        return {}
    raise PilotError("authority checks must be validator-owned ledger state; caller-supplied mappings are rejected")


def _trigger_result_is_independently_valid(trigger: Mapping[str, Any], manifest: Mapping[str, Any], authorization: Mapping[str, Any] | None) -> bool:
    try:
        CampaignResultValidator(manifest, authorization)._validate_reference(trigger)
        return True
    except (PilotError, KeyError, TypeError, AttributeError):
        return False


def _validate_campaign_stop_evidence(
    evidence: Mapping[str, Any],
    result: Mapping[str, Any],
    manifest: Mapping[str, Any],
    authorization: Mapping[str, Any] | None,
    case_ledger: Mapping[str, Any],
    authority_ledger: Mapping[str, Any],
) -> None:
    _require(all(field in evidence for field in CAMPAIGN_STOP_EVIDENCE_FIELDS), "campaign-stop evidence is incomplete")
    reason = evidence["reason_code"]
    _require(reason in CAMPAIGN_STOP_REASON_CODES and reason == result["terminal_reason_code"], "campaign-stop reason is not frozen")
    _require(evidence["confirmed"] is True and isinstance(evidence["evidence_reference"], str) and evidence["evidence_reference"], "campaign-stop evidence is not confirmed")
    trigger_case = bool(isinstance(evidence["trigger_case_id"], str) and evidence["trigger_case_id"])
    authority_check = bool(isinstance(evidence["pre_case_authority_check_identity"], str) and evidence["pre_case_authority_check_identity"])
    _require(trigger_case ^ authority_check, "campaign-stop trigger identity is missing or ambiguous")
    mode = "prior_case" if trigger_case else "authority"
    _require(mode in CAMPAIGN_STOP_TRIGGER_COMPATIBILITY[reason], "campaign-stop reason is incompatible with trigger form")
    if trigger_case:
        frozen = {case["case_id"]: case for case in manifest["case_order"]}
        trigger_id = evidence["trigger_case_id"]
        _require(trigger_id in frozen, "campaign-stop trigger case is not frozen")
        _require(frozen[trigger_id]["order_index"] < result["order_index"], "campaign-stop trigger must precede blocked case")
        _require(isinstance(evidence["trigger_result_sha256"], str) and len(evidence["trigger_result_sha256"]) == 64, "campaign-stop trigger result hash is required")
        _require(evidence["authority_check_record_sha256"] is None, "prior-case stop cannot carry an authority record")
        _require(trigger_id in case_ledger, "validated prior trigger result is required")
        stored = case_ledger[trigger_id]
        _require(isinstance(stored, _StoredCaseRecord), "trigger result must be a stored validated case record")
        _require(stored._verify_digest(), "stored trigger result digest mismatch")
        trigger = stored.result
        _require(stored.case_id == trigger_id and trigger.get("case_id") == trigger_id, "trigger result case identity mismatch")
        _require(stored.sha256 == result_sha256(trigger) == evidence["trigger_result_sha256"], "trigger result hash binding mismatch")
        _require(stored.campaign_manifest_hash == result["campaign_manifest_hash"] and trigger.get("campaign_manifest_hash") == result["campaign_manifest_hash"], "trigger result manifest binding mismatch")
        _require(stored.campaign_commit == result["campaign_commit"] and trigger.get("campaign_commit") == result["campaign_commit"] and trigger.get("accepted_code_commit") == result["accepted_code_commit"], "trigger result campaign commit mismatch")
        _require(stored.order_index < result["order_index"], "trigger result ordering is invalid")
        _require(_trigger_result_is_independently_valid(trigger, manifest, authorization), "trigger result is not independently valid")
        _validate_trigger_result_supports_reason(trigger, reason, evidence)
    else:
        identity = evidence["pre_case_authority_check_identity"]
        _require(identity in AUTHORITY_CHECK_IDENTITIES, "campaign-stop authority-check identity is not frozen")
        _require(identity in CAMPAIGN_STOP_AUTHORITY_IDENTITIES.get(reason, set()), "authority-check identity is incompatible with stop reason")
        _require(isinstance(evidence["authority_check_record_sha256"], str) and len(evidence["authority_check_record_sha256"]) == 64, "authority-check record hash is required")
        _require(evidence["trigger_result_sha256"] is None, "authority stop cannot carry a trigger result")
        _require(identity in authority_ledger, "validated authority-check record is required")
        stored = authority_ledger[identity]
        _require(isinstance(stored, _StoredAuthorityRecord), "authority check must be a stored validated record")
        _require(stored._verify_digest(), "stored authority-check record digest mismatch")
        _require(stored.sha256 == sha256_text(canonical_json(stored.record)) == evidence["authority_check_record_sha256"], "authority-check record hash binding mismatch")
        _require(stored.identity == identity and stored.reason_code == reason, "authority-check record identity/reason mismatch")
        _require(stored.campaign_manifest_hash == result["campaign_manifest_hash"], "authority-check manifest binding mismatch")
        _require(stored.qualification_contract_hash == manifest["qualification_contract_hash"] and stored.source_authority_hash == SOURCE_INTEGRITY_SHA256, "authority-check frozen binding mismatch")
        _require(validate_authority_check_record(stored.record, manifest, reason), "authority-check record is not independently valid")
    relevant = CAMPAIGN_STOP_REASON_FIELDS[reason]
    for field in CAMPAIGN_STOP_EVIDENCE_FIELDS:
        if field in {"reason_code", "trigger_case_id", "pre_case_authority_check_identity", "evidence_reference", "confirmed"} or field in relevant:
            continue
        _require(evidence[field] is None, f"campaign-stop evidence has unrelated populated field: {field}")
    if reason == "TRANSPORT_EVIDENCE_LOSS":
        _require(evidence["expected_evidence_complete"] is True and evidence["observed_evidence_complete"] is False, "transport-loss evidence is not complete-to-incomplete")
    elif reason == "CONTAINMENT_UNCERTAINTY":
        _require(evidence["expected_containment_confirmed"] is True and evidence["observed_containment_confirmed"] in {False, "UNKNOWN"}, "containment evidence is not confirmed-to-failed")
    elif reason == "SOURCE_MUTATION":
        trigger_id = evidence["trigger_case_id"]
        trigger_task = next(case["task_id"] for case in manifest["case_order"] if case["case_id"] == trigger_id)
        entry = next(item for item in manifest["inventory"] if item["task_id"] == trigger_task)
        _require(evidence["expected_source_hash"] == entry["source_sha256"] and isinstance(evidence["observed_source_hash"], str) and len(evidence["observed_source_hash"]) == 64 and evidence["observed_source_hash"] != entry["source_sha256"], "source mutation evidence is not bound to the trigger task")
    elif reason == "CLEANUP_FAILURE":
        _require(evidence["expected_cleanup_succeeded"] is True and evidence["observed_cleanup_succeeded"] is False, "cleanup evidence is not true-to-false")
    elif reason == "VERIFIER_INTEGRITY_FAILURE":
        _require(evidence["expected_verifier_integrity"] is True and evidence["observed_verifier_integrity"] is False, "verifier evidence is not true-to-false")
    elif reason == "RESULT_SCHEMA_INCONSISTENCY":
        _require(evidence["schema_error_code"] in {"MISSING_REQUIRED_FIELD", "CASE_ID_MISMATCH", "BUDGET_EXCEEDED", "TERMINAL_CONTRADICTION", "HASH_INVALID"}, "schema error code is not frozen")
    elif reason in {"TRACKED_SOURCE_CHANGED", "MANIFEST_HASH_CHANGED", "QUALIFICATION_CONTRACT_CHANGED"}:
        expected_field = {"TRACKED_SOURCE_CHANGED": "expected_source_authority_hash", "MANIFEST_HASH_CHANGED": "expected_manifest_hash", "QUALIFICATION_CONTRACT_CHANGED": "expected_qualification_contract_hash"}[reason]
        observed_field = expected_field.replace("expected", "observed")
        expected_value = {"TRACKED_SOURCE_CHANGED": SOURCE_INTEGRITY_SHA256, "MANIFEST_HASH_CHANGED": manifest_hash(manifest), "QUALIFICATION_CONTRACT_CHANGED": manifest["qualification_contract_hash"]}[reason]
        _require(evidence[expected_field] == expected_value and isinstance(evidence[observed_field], str) and len(evidence[observed_field]) == 64 and evidence[observed_field] != expected_value, "campaign-stop hash evidence is not exact")


def _validate_live_authorization(manifest: Mapping[str, Any], authorization: Mapping[str, Any] | None) -> Mapping[str, Any]:
    _require(isinstance(authorization, Mapping), "LIVE_CASE requires explicit live authorization")
    failure = _authorization_failure_category(manifest, authorization)
    _require(failure is None, f"live authorization is invalid: {failure}")
    return authorization


def _validate_infrastructure_transport(stage: str, reason: str, transport: Mapping[str, Any], terminal_transport: Mapping[str, Any]) -> None:
    state = tuple(bool(transport[key]) for key in ("completed_response", "malformed_response", "provider_error"))
    _require(sum(state) <= 1, "infrastructure aggregate transport state is contradictory")
    if stage in {"pre_provider", "workspace_pre_provider", "containment_pre_provider"}:
        _require(state == (False, False, False), "pre-provider infrastructure transport is not empty")
        _require(terminal_transport.get("final_attempt_classification") == "INFRASTRUCTURE_FAILURE" and terminal_transport.get("provider_completed_response") is False and terminal_transport.get("process_exit_code") is None and terminal_transport.get("timed_out") is False and terminal_transport.get("provider_error_category") is None, "pre-provider infrastructure terminal transport is inconsistent")
    elif stage == "provider_transport":
        _require(state == (False, False, False), "provider-transport infrastructure requires an evidence-loss aggregate state")
        _require(terminal_transport.get("final_attempt_classification") == "INFRASTRUCTURE_FAILURE" and terminal_transport.get("provider_completed_response") is False and terminal_transport.get("process_exit_code") is None and terminal_transport.get("timed_out") is False and terminal_transport.get("provider_error_category") is None, "provider-transport infrastructure terminal transport is inconsistent")
    else:
        _require(state == (True, False, False), "post-transport infrastructure requires one completed prior response")
        _require(terminal_transport.get("final_attempt_classification") == "INFRASTRUCTURE_FAILURE" and terminal_transport.get("provider_completed_response") is True and terminal_transport.get("process_exit_code") == 0 and terminal_transport.get("timed_out") is False and terminal_transport.get("provider_error_category") is None, "post-transport infrastructure terminal transport is inconsistent")


def _validate_terminal_matrix(result: Mapping[str, Any], manifest: Mapping[str, Any], execution_kind: str, transport: Mapping[str, Any], verifier: Mapping[str, Any], counts: Mapping[str, int], budgets: Mapping[str, int], authorization: Mapping[str, Any] | None, case_ledger: Mapping[str, Any] | None = None, authority_ledger: Mapping[str, Any] | None = None) -> None:
    status = result["terminal_status"]
    terminal_transport = result["terminal_transport_evidence"]
    infrastructure = result["infrastructure_evidence"]
    blocked = result["blocked_evidence"]
    _require(isinstance(result["terminal_reason_code"], str) and result["terminal_reason_code"], "terminal reason code is required")
    _require(isinstance(terminal_transport, Mapping), "structured terminal transport evidence is required")
    _require(isinstance(infrastructure, Mapping), "structured infrastructure evidence is required")
    _require(isinstance(blocked, Mapping), "structured blocked evidence is required")
    _require(type(infrastructure.get("confirmed_failure")) is bool, "invalid infrastructure confirmation")
    _require(type(blocked.get("confirmed")) is bool and isinstance(blocked.get("block_kind"), str) and isinstance(blocked.get("reason_code"), str), "invalid blocked evidence")
    infrastructure_pre_contact = status == "INFRASTRUCTURE_ERROR" and infrastructure.get("stage") in {"pre_provider", "workspace_pre_provider", "containment_pre_provider"}
    if execution_kind == "LIVE_CASE" and status != "BLOCKED" and not infrastructure_pre_contact:
        _require(result["logical_model_calls"] >= 1, "completed LIVE_CASE requires a logical model call")
        _require(result["public_request_hash"] is not None and result["source_hash"] is not None, "completed LIVE_CASE requires public/source identity")
        _require(result["route_observation"]["preflight_success"] is True and transport["synthetic"] is False, "completed LIVE_CASE lacks real route preflight")
        if status != "INFRASTRUCTURE_ERROR":
            _require(result["canonical_source_restoration"] and result["owned_workspace_cleanup"] and result["evidence_consistency"], "completed LIVE_CASE lacks restoration/cleanup/evidence consistency")
    if status == "RESOLVED":
        _require(result["baseline_reproduction"] is True and result["logical_model_calls"] >= 1 and result["valid_directives"] >= 1, "RESOLVED lacks baseline, model call, or valid directive")
        _require(result["patch_submissions"] >= 1 and result["candidate_hash"] is not None and result["verifier_runs"] >= 1, "RESOLVED lacks candidate/verifier evidence")
        _require(verifier.get("status") == "COMPLETED" and verifier.get("outcome") == "RESOLVED" and verifier.get("lifecycle_succeeded") is True, "RESOLVED lacks accepted verifier lifecycle")
        _require(transport["completed_response"] is True and transport["malformed_response"] is False and transport["provider_error"] is False, "RESOLVED has contradictory transport evidence")
        _require(terminal_transport.get("final_attempt_classification") == "COMPLETED_RESPONSE" and terminal_transport.get("provider_completed_response") is True and terminal_transport.get("process_exit_code") == 0 and terminal_transport.get("timed_out") is False and terminal_transport.get("provider_error_category") is None and isinstance(terminal_transport.get("evidence_reference"), str) and terminal_transport.get("evidence_reference"), "RESOLVED terminal-attempt evidence is invalid")
    elif status == "UNRESOLVED":
        _require(result["baseline_reproduction"] is True and result["logical_model_calls"] >= 1 and result["valid_directives"] >= 1, "UNRESOLVED lacks reproduced baseline/model call/directive")
        _require(verifier.get("status") == "COMPLETED" and verifier.get("outcome") in {"NO_OP", "UNRESOLVED"} and verifier.get("lifecycle_succeeded") is True, "UNRESOLVED lacks completed non-resolved verifier lifecycle")
        _require(transport["completed_response"] is True and transport["malformed_response"] is False and transport["provider_error"] is False, "UNRESOLVED has contradictory transport evidence")
        _require(terminal_transport.get("final_attempt_classification") == "COMPLETED_RESPONSE" and terminal_transport.get("provider_completed_response") is True and terminal_transport.get("process_exit_code") == 0 and terminal_transport.get("timed_out") is False and terminal_transport.get("provider_error_category") is None and isinstance(terminal_transport.get("evidence_reference"), str) and terminal_transport.get("evidence_reference"), "UNRESOLVED terminal-attempt evidence is invalid")
    elif status == "INVALID_MODEL_RESPONSE":
        _require(transport["completed_response"] is True and transport["malformed_response"] is True and transport["provider_error"] is False, "invalid-model result lacks completed malformed-response evidence")
        _require(result["malformed_directive_rejections"] >= 1 and result["bounded_directive_feedback_events"] >= result["malformed_directive_rejections"], "invalid-model result lacks bounded malformed-response accounting")
        _require(verifier.get("outcome") != "RESOLVED", "invalid-model result suppresses an accepted repair")
        terminal_class = terminal_transport.get("final_attempt_classification")
        _require(terminal_class == "MALFORMED_RESPONSE" and terminal_transport.get("provider_completed_response") is True and terminal_transport.get("process_exit_code") == 0 and terminal_transport.get("timed_out") is False and terminal_transport.get("provider_error_category") is None and isinstance(terminal_transport.get("evidence_reference"), str) and terminal_transport.get("evidence_reference"), "invalid-model terminal transport evidence is not structured")
    elif status == "PROVIDER_ERROR":
        _require(result["logical_model_calls"] >= 1 and result["provider_process_attempts"] >= 1, "provider-error lacks a logical call and provider attempt")
        _require(transport["completed_response"] is False and transport["malformed_response"] is False and transport["provider_error"] is True and transport["synthetic"] is False, "provider-error aggregate transport evidence is inconsistent")
        terminal_class = terminal_transport.get("final_attempt_classification")
        _require(terminal_class in {"PROVIDER_ERROR", "TRANSPORT_ERROR", "TIMEOUT"}, "provider-error terminal classification is missing")
        _require(type(terminal_transport.get("provider_completed_response")) is bool and terminal_transport.get("provider_completed_response") is False, "provider-error terminal response evidence is ambiguous")
        _require(isinstance(terminal_transport.get("evidence_reference"), str) and terminal_transport.get("evidence_reference"), "provider-error evidence reference is missing")
        _require(verifier.get("outcome") != "RESOLVED", "provider-error result suppresses an accepted repair")
        if terminal_class == "TIMEOUT":
            _require(terminal_transport.get("timed_out") is True and terminal_transport.get("provider_error_category") == "TIMEOUT" and terminal_transport.get("process_exit_code") is None, "TIMEOUT terminal evidence is inconsistent")
        elif terminal_class == "PROVIDER_ERROR":
            _require(terminal_transport.get("timed_out") is False and isinstance(terminal_transport.get("provider_error_category"), str) and terminal_transport.get("provider_error_category") and (terminal_transport.get("process_exit_code") is None or type(terminal_transport.get("process_exit_code")) is int), "provider-error terminal evidence is inconsistent")
        else:
            _require(terminal_transport.get("timed_out") is False and isinstance(terminal_transport.get("provider_error_category"), str) and terminal_transport.get("provider_error_category") and (terminal_transport.get("process_exit_code") is None or type(terminal_transport.get("process_exit_code")) is int), "transport-error terminal evidence is inconsistent")
    elif status == "INFRASTRUCTURE_ERROR":
        _require(execution_kind == "LIVE_CASE", "infrastructure error must be a live case")
        _require(infrastructure.get("confirmed_failure") is True and isinstance(infrastructure.get("stage"), str) and infrastructure.get("stage"), "infrastructure failure evidence is missing")
        stage = infrastructure["stage"]
        _require(stage in INFRASTRUCTURE_STAGE_MATRIX, "unknown infrastructure stage")
        _require(infrastructure.get("reason_code") in INFRASTRUCTURE_STAGE_MATRIX[stage], "infrastructure reason/stage combination is not frozen")
        _require(infrastructure.get("classification") == INFRASTRUCTURE_CLASSIFICATIONS[stage], "infrastructure classification does not match stage")
        _validate_infrastructure_transport(stage, infrastructure["reason_code"], transport, terminal_transport)
        _require(type(infrastructure.get("prior_lifecycle_completed")) is bool, "infrastructure prior lifecycle flag is invalid")
        _require(infrastructure.get("terminal_classification") == "INFRASTRUCTURE_FAILURE", "infrastructure terminal classification is invalid")
        _require(isinstance(infrastructure.get("evidence_reference"), str) and infrastructure.get("evidence_reference"), "infrastructure evidence reference is missing")
        _require(type(infrastructure.get("source_mutation_observed")) is bool and (infrastructure.get("expected_source_hash") is None or (isinstance(infrastructure.get("expected_source_hash"), str) and len(infrastructure["expected_source_hash"]) == 64)), "infrastructure source mutation evidence is malformed")
        if infrastructure.get("source_mutation_observed"):
            _require(stage == "cleanup" and infrastructure.get("reason_code") == "CLEANUP_FAILURE" and result.get("source_hash") != infrastructure.get("expected_source_hash"), "source mutation infrastructure evidence is not cleanup-bound")
        if infrastructure_pre_contact:
            _require(result["logical_model_calls"] == 0 and result["provider_process_attempts"] == 0 and result["retries"] == 0 and result["valid_directives"] == 0 and result["malformed_directive_rejections"] == 0 and result["bounded_directive_feedback_events"] == 0 and result["hypotheses_created"] == 0 and result["patch_submissions"] == 0 and result["verifier_runs"] == 0, "pre-provider infrastructure failure has case activity")
            _require(infrastructure["prior_lifecycle_completed"] is False, "pre-provider infrastructure failure claims prior lifecycle")
            _require(result["prompt_tokens"] == 0 and result["completion_tokens"] == 0 and result["reasoning_tokens"] == 0 and result["provider_reported_cost"] == 0 and result["public_request_hash"] is None and result["source_hash"] is None and result["candidate_hash"] is None, "pre-provider infrastructure failure has identity/token/cost activity")
            _require(result["source_revision"] == manifest["authority"]["revision"], "pre-provider infrastructure source revision mismatch")
            _require(all(transport[key] is False for key in ("synthetic", "completed_response", "malformed_response", "provider_error")), "pre-provider infrastructure transport evidence is not all false")
            _require(terminal_transport.get("final_attempt_classification") == "INFRASTRUCTURE_FAILURE" and terminal_transport.get("process_exit_code") is None and terminal_transport.get("timed_out") is False and terminal_transport.get("provider_error_category") is None and terminal_transport.get("provider_completed_response") is False, "pre-provider infrastructure terminal evidence is invalid")
        elif stage == "provider_transport":
            _require(result["logical_model_calls"] >= 1 and result["provider_process_attempts"] >= 1, "provider-transport infrastructure failure lacks provider activity")
            if infrastructure["reason_code"] == "TRANSPORT_EVIDENCE_LOSS":
                _require(type(infrastructure.get("provider_attempt_index")) is int and infrastructure["provider_attempt_index"] >= 1 and infrastructure["provider_attempt_index"] <= result["provider_process_attempts"], "transport evidence loss lacks provider attempt identity")
        elif stage == "pdb_runtime":
            _require(result["policy"] == "pdb-on-uncertainty" and result["hypotheses_created"] >= 1 and counts["allowed_gate_openings"] >= 1 and counts["sessions_started"] >= 1, "PDB infrastructure failure lacks lifecycle evidence")
        elif stage == "verifier":
            _require(result["verifier_runs"] >= 1, "verifier infrastructure failure lacks verifier activity")
        elif stage in {"controller", "cleanup", "evidence_packaging"}:
            _require(result["logical_model_calls"] >= 1, "post-contact infrastructure failure lacks prior lifecycle")
        if stage == "cleanup":
            _require(infrastructure["prior_lifecycle_completed"] is True, "cleanup failure lacks prior lifecycle record")
    elif status == "PDB_NOT_REACHED":
        _require(result["policy"] == "pdb-on-uncertainty", "PDB_NOT_REACHED requires PDB policy")
        _require(counts["allowed_gate_openings"] == 0 and counts["sessions_started"] == 0 and counts["successful_observations"] == 0 and counts["failed_observations"] == 0, "PDB_NOT_REACHED has PDB activity")
        _require(result["terminal_reason_code"] in {"PDB_NOT_REACHED_NO_GATE", "PDB_NOT_REACHED_GATE_REJECTED"}, "PDB_NOT_REACHED reason does not match its evidence")
        _require(result["baseline_reproduction"] is True and result["logical_model_calls"] >= 1 and result["valid_directives"] >= 1 and result["route_observation"]["preflight_success"] is True and transport["synthetic"] is False and transport["completed_response"] is True and transport["malformed_response"] is False and transport["provider_error"] is False and verifier.get("outcome") != "RESOLVED", "PDB_NOT_REACHED lacks valid completed pre-PDB execution")
        _require(terminal_transport.get("final_attempt_classification") == "COMPLETED_RESPONSE" and terminal_transport.get("provider_completed_response") is True and terminal_transport.get("process_exit_code") == 0 and terminal_transport.get("timed_out") is False and terminal_transport.get("provider_error_category") is None and isinstance(terminal_transport.get("evidence_reference"), str) and terminal_transport.get("evidence_reference"), "PDB_NOT_REACHED terminal evidence is invalid")
        if counts["total_gate_decisions"] == 0:
            _require(result["terminal_reason_code"] == "PDB_NOT_REACHED_NO_GATE", "PDB_NOT_REACHED reason claims a rejected gate without a decision")
        else:
            _require(result["terminal_reason_code"] == "PDB_NOT_REACHED_GATE_REJECTED" and counts["rejected_gate_decisions"] == counts["total_gate_decisions"], "PDB_NOT_REACHED gate evidence is inconsistent")
    elif status == "BLOCKED":
        if execution_kind == "DRY_RUN":
            _require(blocked.get("confirmed") is True and blocked.get("block_kind") == "dry-run", "DRY_RUN blocked result has invalid block kind")
            _require(result["terminal_reason_code"] in DRY_RUN_BLOCK_REASON_CODES, "DRY_RUN blocked result has invalid reason code")
        else:
            _require(blocked.get("confirmed") is True and blocked.get("block_kind") in {"live-pre-provider", "campaign-stop"}, "LIVE_CASE blocked result has invalid block kind")
            if blocked.get("block_kind") == "live-pre-provider":
                _require(result["terminal_reason_code"] in LIVE_PRE_PROVIDER_REASON_CODES, "LIVE_CASE pre-provider block has invalid reason code")
                _require(result["logical_model_calls"] == 0 and result["provider_process_attempts"] == 0 and result["retries"] == 0, "pre-provider block has provider activity")
                _require(result["valid_directives"] == 0 and result["malformed_directive_rejections"] == 0 and result["bounded_directive_feedback_events"] == 0, "pre-provider block has response activity")
                _require(result["hypotheses_created"] == 0 and result["patch_submissions"] == 0 and result["candidate_hash"] is None and result["verifier_runs"] == 0, "pre-provider block has case activity")
                _require(all(counts[key] == 0 for key in ("total_gate_decisions", "allowed_gate_openings", "rejected_gate_decisions", "sessions_started", "successful_observations", "failed_observations")), "pre-provider block has PDB activity")
                _require(result["route_observation"]["preflight_success"] is False and transport["synthetic"] is False, "pre-provider block lacks observed failed preflight")
                _require(terminal_transport.get("final_attempt_classification") == "PRE_PROVIDER_BLOCK" and terminal_transport.get("process_exit_code") is None and terminal_transport.get("timed_out") is False and terminal_transport.get("provider_error_category") is None and terminal_transport.get("provider_completed_response") is False, "pre-provider terminal evidence is invalid")
                _require(result["prompt_tokens"] == 0 and result["completion_tokens"] == 0 and result["reasoning_tokens"] == 0 and result["provider_reported_cost"] == 0, "pre-provider block has token or cost activity")
                _require(result["public_request_hash"] is None and result["source_hash"] is None and result["source_revision"] == manifest["authority"]["revision"], "pre-provider block has request/source identity")
                _require(all(transport[key] is False for key in ("synthetic", "completed_response", "malformed_response", "provider_error")), "pre-provider aggregate transport evidence is not all false")
                _validate_preflight_failure_evidence(result["preflight_failure_evidence"], result, manifest, authorization)
            else:
                _require(result["terminal_reason_code"] in CAMPAIGN_STOP_REASON_CODES, "campaign-stop block has invalid stop reason code")
                _require(terminal_transport.get("final_attempt_classification") == "CAMPAIGN_STOP" and terminal_transport.get("process_exit_code") is None and terminal_transport.get("timed_out") is False and terminal_transport.get("provider_error_category") is None and terminal_transport.get("provider_completed_response") is False, "campaign-stop terminal evidence is invalid")
                _require(result["logical_model_calls"] == 0 and result["provider_process_attempts"] == 0 and result["retries"] == 0 and result["valid_directives"] == 0 and result["malformed_directive_rejections"] == 0 and result["bounded_directive_feedback_events"] == 0 and result["hypotheses_created"] == 0 and result["patch_submissions"] == 0 and result["verifier_runs"] == 0, "campaign-stop has case activity")
                _require(result["prompt_tokens"] == 0 and result["completion_tokens"] == 0 and result["reasoning_tokens"] == 0 and result["provider_reported_cost"] == 0 and result["public_request_hash"] is None and result["candidate_hash"] is None, "campaign-stop has token, cost, request, or candidate activity")
                _require(result["source_hash"] is None and result["source_revision"] == manifest["authority"]["revision"], "campaign-stop source identity is invalid")
                _require(all(transport[key] is False for key in ("synthetic", "completed_response", "malformed_response", "provider_error")), "campaign-stop aggregate transport evidence is not all false")
                _require(all(counts[key] == 0 for key in ("total_gate_decisions", "allowed_gate_openings", "rejected_gate_decisions", "sessions_started", "successful_observations", "failed_observations")), "campaign-stop has PDB activity")
                _validate_campaign_stop_evidence(result["campaign_stop_evidence"], result, manifest, authorization, case_ledger, authority_ledger)
        _require(blocked.get("reason_code") == result["terminal_reason_code"], "blocked reason code mismatch")
    if status not in {"BLOCKED", "INFRASTRUCTURE_ERROR"}:
        _require(result["canonical_source_restoration"] and result["owned_workspace_cleanup"] and result["evidence_consistency"], "completed result lacks restoration/cleanup/evidence consistency")
    activity = counts["allowed_gate_openings"] > 0 or counts["sessions_started"] > 0 or counts["successful_observations"] + counts["failed_observations"] > 0
    if activity:
        _require(result["policy"] == "pdb-on-uncertainty" and result["baseline_reproduction"] is True and result["hypotheses_created"] >= 1, "PDB activity lacks PDB policy, baseline, or active hypothesis")
    if result["policy"] == "static-baseline":
        _require(all(counts[key] == 0 for key in ("total_gate_decisions", "allowed_gate_openings", "rejected_gate_decisions", "sessions_started", "successful_observations", "failed_observations")), "static policy opened or observed PDB")


def _validate_authority_check_record_strict(record: Mapping[str, Any], manifest: Mapping[str, Any], reason: str) -> None:
    _require(isinstance(record, Mapping), "authority-check record must be an object")
    _require(reason in CAMPAIGN_STOP_REASON_CODES, "authority-check reason is not frozen")
    _require(record.get("reason_code") == reason, "authority-check reason mismatch")
    expected_identity = {
        "MANIFEST_HASH_CHANGED": "AUTHORITY_CHECK:MANIFEST",
        "QUALIFICATION_CONTRACT_CHANGED": "AUTHORITY_CHECK:QUALIFICATION_CONTRACT",
        "TRACKED_SOURCE_CHANGED": "AUTHORITY_CHECK:TRACKED_SOURCE",
        "CONTAINMENT_UNCERTAINTY": "AUTHORITY_CHECK:CONTAINMENT",
        "VERIFIER_INTEGRITY_FAILURE": "AUTHORITY_CHECK:VERIFIER",
    }[reason]
    _require(record.get("identity") == expected_identity, "authority-check identity mismatch")
    _require(isinstance(record.get("evidence_reference"), str) and record["evidence_reference"], "authority-check evidence reference is required")
    if reason == "MANIFEST_HASH_CHANGED":
        expected, observed = record.get("expected_manifest_hash"), record.get("observed_manifest_hash")
    elif reason == "QUALIFICATION_CONTRACT_CHANGED":
        expected, observed = record.get("expected_qualification_contract_hash"), record.get("observed_qualification_contract_hash")
    elif reason == "TRACKED_SOURCE_CHANGED":
        expected, observed = record.get("expected_source_authority_hash"), record.get("observed_source_authority_hash")
    elif reason == "CONTAINMENT_UNCERTAINTY":
        expected, observed = record.get("expected_containment_confirmed"), record.get("observed_containment_confirmed")
        _require(expected is True and observed in {False, "UNKNOWN"}, "containment authority evidence is invalid")
        return
    else:
        expected, observed = record.get("expected_verifier_integrity"), record.get("observed_verifier_integrity")
        _require(expected is True and observed is False, "verifier authority evidence is invalid")
        return
    frozen = {
        "MANIFEST_HASH_CHANGED": manifest_hash(manifest),
        "QUALIFICATION_CONTRACT_CHANGED": manifest["qualification_contract_hash"],
        "TRACKED_SOURCE_CHANGED": SOURCE_INTEGRITY_SHA256,
    }[reason]
    _require(expected == frozen, "authority-check expected hash is not frozen")
    _require(isinstance(observed, str) and len(observed) == 64 and all(c in "0123456789abcdef" for c in observed) and observed != frozen, "authority-check observed hash is invalid")


def validate_authority_check_record(record: Mapping[str, Any], manifest: Mapping[str, Any], reason: str) -> bool:
    try:
        _validate_authority_check_record_strict(record, manifest, reason)
        return True
    except (PilotError, KeyError, TypeError, AttributeError):
        return False


def validate_authority_checks(records: list[Mapping[str, Any]], manifest: Mapping[str, Any]) -> _AuthorityCheckLedger:
    """Validate typed authority-check records and return a validator-owned ledger snapshot."""
    validator = CampaignResultValidator(manifest)
    validator.register_authority_checks(records)
    return validator.authority_ledger_snapshot()


def validate_case_results_in_order(
    results: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    authorization: Mapping[str, Any] | None = None,
    authority_checks: Any = None,
) -> _CaseResultLedger:
    """Validate case results strictly in frozen order and return a validator-owned ledger snapshot."""
    validator = CampaignResultValidator(manifest, authorization)
    for result in results:
        validator.validate_result(result, authority_ledger=authority_checks)
    return validator.case_ledger_snapshot()


def validate_case_result(
    result: Mapping[str, Any],
    manifest: Mapping[str, Any],
    authorization: Mapping[str, Any] | None = None,
    prior_results: Any = None,
    authority_checks: Any = None,
) -> None:
    """Validate one case result against validator-owned ledger references only.

    ``prior_results`` and ``authority_checks`` are accepted only as the
    validator's own internal ledger snapshots (or empty mappings); any
    caller-supplied mapping or caller-constructed capability object is
    rejected, and campaign-stop triggers are resolved only from that ledger
    state.
    """
    validator = CampaignResultValidator(manifest, authorization)
    validator._validate_reference(result, prior_ledger=prior_results, authority_ledger=authority_checks)


def public_case_record(manifest: Mapping[str, Any], case: Mapping[str, Any], *, terminal_status: str = "BLOCKED", reason: str = "DRY_RUN_ONLY") -> dict[str, Any]:
    policy = case["policy"]
    pdb = {"total_gate_decisions": 0, "allowed_gate_openings": 0, "rejected_gate_decisions": 0, "sessions_started": 0, "successful_observations": 0, "failed_observations": 0}
    route_observation = {
        "provider": manifest["route"]["provider"], "model": manifest["route"]["model"], "variant": manifest["route"]["variant"], "protocol": manifest["route"]["protocol"],
        "opencode_version": None, "active_model_status": "NOT_RUN", "variant_available": False, "catalog_fingerprint": None,
        "input_price": 0, "output_price": 0, "paid_fallback_used": False, "alternate_provider_used": False, "ollama_used": False, "preflight_success": False,
    }
    preflight_failure_evidence = {field: None for field in PREFLIGHT_FAILURE_FIELDS}
    campaign_stop_evidence = {field: None for field in CAMPAIGN_STOP_EVIDENCE_FIELDS}
    campaign_stop_evidence.update({"confirmed": False})
    return {
        "qualification_contract_hash": manifest["qualification_contract_hash"],
        "execution_kind": "DRY_RUN",
        "campaign_manifest_hash": manifest_hash(manifest),
        "planning_baseline_commit": PLANNING_BASELINE_COMMIT, "campaign_commit": None, "accepted_code_commit": None,
        "case_id": case["case_id"], "order_index": case["order_index"],
        "task_id": case["task_id"], "policy": policy,
        "provider": manifest["route"]["provider"], "model": manifest["route"]["model"], "variant": manifest["route"]["variant"], "route_observation": route_observation,
        "public_request_hash": None, "source_revision": manifest["authority"]["revision"], "source_hash": None,
        "candidate_hash": None, "repair_outcome": "NO_CANDIDATE",
        "logical_model_calls": 0, "provider_process_attempts": 0, "retries": 0,
        "valid_directives": 0, "malformed_directive_rejections": 0, "bounded_directive_feedback_events": 0,
        "baseline_reproduction": False, "controller_states_visited": [], "hypotheses_created": 0,
        "pdb_gate_decisions": [], "pdb_sessions_started": 0,
        "successful_pdb_observations": pdb["successful_observations"], "failed_pdb_observations": pdb["failed_observations"],
        "pdb_counts": pdb, "verifier_runs": 0, "patch_submissions": 0, "independent_verifier_result": {"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        "terminal_status": terminal_status, "termination_reason": reason,
         "terminal_reason_code": "DRY_RUN_ONLY",
         "terminal_transport_evidence": {"final_attempt_classification": "NOT_APPLICABLE", "process_exit_code": None, "timed_out": False, "provider_error_category": None, "provider_completed_response": False, "evidence_reference": "dry-run-synthetic"},
         "infrastructure_evidence": {"stage": "none", "reason_code": "NONE", "confirmed_failure": False, "classification": "NONE", "terminal_classification": "NOT_APPLICABLE", "provider_attempt_index": None, "prior_lifecycle_completed": False, "source_mutation_observed": False, "expected_source_hash": None, "evidence_reference": "dry-run-synthetic"},
         "blocked_evidence": {"block_kind": "dry-run", "reason_code": "DRY_RUN_ONLY", "confirmed": True, "evidence_reference": "dry-run-synthetic"},
         "preflight_failure_evidence": preflight_failure_evidence,
         "campaign_stop_evidence": campaign_stop_evidence,
        "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": True},
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "provider_reported_cost": 0, "public_evidence_bytes": 0,
        "wall_clock_duration_seconds": 0, "canonical_source_restoration": True,
        "owned_workspace_cleanup": True, "evidence_consistency": True, "resource_ids": {},
    }


class _FakeResource:
    def __init__(self, kind: str, ordinal: int, case_id: str) -> None:
        self.kind = kind
        self.ordinal = ordinal
        self.case_id = case_id
        self.handle_id = f"fake-{kind}-{ordinal}"


class _InjectedFactory:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls: list[str] = []

    def create(self, case: Mapping[str, Any]) -> _FakeResource:
        resource = _FakeResource(self.kind, int(case["order_index"]), str(case["case_id"]))
        self.calls.append(resource.handle_id)
        return resource


def dry_run(manifest: Mapping[str, Any], *, fail_at: int | None = 3, model_failure_at: int | None = None) -> dict[str, Any]:
    validate_manifest(manifest)
    factories = {kind: _InjectedFactory(kind) for kind in (
        "case_execution_context", "model_adapter", "provider_process", "owned_workspace",
        "controller_state", "session_state", "directive_feedback_buffer", "task_memory",
    )}
    resource_ids: list[dict[str, str]] = []
    resource_owners: list[dict[str, str]] = []
    records = []
    stopped = None
    for case in manifest["case_order"]:
        if fail_at is not None and case["order_index"] == fail_at:
            stopped = {"order_index": fail_at, "reason": "INJECTED_INFRASTRUCTURE_FAILURE"}
            break
        resources = {kind: factory.create(case) for kind, factory in factories.items()}
        ordinal = int(case["order_index"])
        ids = {kind: resource.handle_id for kind, resource in resources.items()}
        resource_ids.append(ids)
        resource_owners.extend({"resource_id": resource.handle_id, "case_id": resource.case_id} for resource in resources.values())
        record = public_case_record(manifest, case)
        record["resource_ids"] = ids
        if model_failure_at is not None and ordinal == model_failure_at:
            record["terminal_status"] = "INVALID_MODEL_RESPONSE"
            record["termination_reason"] = "SYNTHETIC_MODEL_RESULT_FAILURE"
            record["terminal_reason_code"] = "SYNTHETIC_MALFORMED_RESPONSE"
            record["logical_model_calls"] = 1
            record["provider_process_attempts"] = 1
            record["malformed_directive_rejections"] = 1
            record["bounded_directive_feedback_events"] = 1
            record["terminal_transport_evidence"] = {"final_attempt_classification": "MALFORMED_RESPONSE", "process_exit_code": 0, "timed_out": False, "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "dry-run-synthetic"}
            record["transport_evidence"] = {"completed_response": True, "malformed_response": True, "provider_error": False, "synthetic": True}
        validate_case_result(record, manifest)
        records.append(record)
    return {
        "manifest_hash": manifest_hash(manifest), "provider_processes_started": 0,
        "network_activity": False, "transport_synthetic": True,
        "factory_calls": {kind: len(factory.calls) for kind, factory in factories.items()},
        "fresh_case_resources": len(resource_ids) == len({tuple(sorted(ids.items())) for ids in resource_ids}) and len({item["resource_id"] for item in resource_owners}) == len(resource_owners),
        "resource_owners": resource_owners,
        "unique_resource_ids": resource_ids, "case_records_valid": True, "records_before_stop": records, "stop": stopped,
        "ordinary_model_failure_advanced_once": model_failure_at is None or any(record["order_index"] == model_failure_at for record in records),
        "no_task_replacement_or_rerun": len({record["case_id"] for record in records}) == len(records),
        "all_six_walked": len(records) == 6,
        "infrastructure_stopped_before_next_resource_construction": stopped is None or all(len(factory.calls) == len(records) for factory in factories.values()),
    }


def _runtime_probe_for(task_id: str):
    from agentic_debugger.demo.catalog import RuntimeProbe
    probes = {
        "quixbugs-bucketsort-smoke-v1": RuntimeProbe("python_programs/bucketsort.py", "bucketsort", "bucketsort([1, 0, 1], 2)", "for i, count in enumerate(arr):", ("counts", "i", "count")),
        "quixbugs-find-in-sorted-smoke-v1": RuntimeProbe("python_programs/find_in_sorted.py", "binsearch", "find_in_sorted([1, 2], 3)", "mid =", ("arr", "x")),
        "quixbugs-flatten-smoke-v1": RuntimeProbe("python_programs/flatten.py", "flatten", "list(flatten([[1], 2]))", "yield flatten(x)", ("arr", "x")),
        "quixbugs-is-valid-parenthesization-smoke-v1": RuntimeProbe("python_programs/is_valid_parenthesization.py", "is_valid_parenthesization", "is_valid_parenthesization('(')", "depth = 0", ("parens", "depth")),
        "quixbugs-hanoi-smoke-v1": RuntimeProbe("python_programs/hanoi.py", "hanoi", "hanoi(2)", "helper =", ("height", "start", "end")),
        "quixbugs-kheapsort-smoke-v1": RuntimeProbe("python_programs/kheapsort.py", "kheapsort", "list(kheapsort([2, 1, 3], 1))", "yield heapq.heappushpop(heap, x)", ("heap", "x", "k")),
        "quixbugs-kth-smoke-v1": RuntimeProbe("python_programs/kth.py", "kth", "kth([2, 1, 3], 1)", "if k < num_less:", ("pivot", "num_less", "num_lessoreq")),
    }
    try:
        return probes[task_id]
    except KeyError as exc:
        raise PilotError(f"no reviewed PDB probe for {task_id}") from exc


def validate_runtime_probe(probe: Any, task_manifest: Mapping[str, Any], source_root: Path) -> int:
    from agentic_debugger.demo.catalog import resolve_probe_breakpoint
    target = task_manifest["target"]
    module_path = target["buggy_path"]
    _require(probe.module_path == module_path, "runtime probe module does not equal buggy path")
    _require(probe.module_path != target["corrected_path"], "runtime probe points to corrected source")
    _require(not probe.module_path.startswith("python_testcases/") and not probe.module_path.startswith("conftest"), "runtime probe points to tests/support")
    _require(probe.focus_function in task_manifest["oracle"]["target_symbols"], "runtime probe focus is not a reviewed target symbol")
    module = source_root / "quixbugs" / probe.module_path
    _require(module.is_file(), "runtime probe module is missing")
    source = module.read_text(encoding="utf-8")
    breakpoint_line = resolve_probe_breakpoint(source, probe)
    _require(isinstance(breakpoint_line, int) and breakpoint_line > 0, "runtime probe breakpoint did not resolve")
    _require(breakpoint_line <= len(source.splitlines()), "runtime probe breakpoint is outside the buggy module")
    return breakpoint_line


def _qualification_environment():
    from scripts.quixbugs_gcd_pdb_reachability_case import _verify_environment_ready, EXTERNAL_ROOT_POSIX, PYTHON_VERSION
    from agentic_debugger.bugsinpy.wsl import ResourceLimits, wsl_unc_path
    runner, root_host, venv_posix, env_fingerprint = _verify_environment_ready()
    runs_host = wsl_unc_path(f"{EXTERNAL_ROOT_POSIX}/runs", runner.process.distro)
    sources_host = wsl_unc_path(f"{EXTERNAL_ROOT_POSIX}/sources", runner.process.distro)
    return runner, root_host, venv_posix, env_fingerprint, runs_host, sources_host, PYTHON_VERSION, ResourceLimits(cpu_seconds=5, memory_bytes=268435456, max_processes=8)


def _make_task_facts(adapter: Any, runner: Any, root_host: str, venv_posix: str, env_fingerprint: str, runs_host: str, python_version: str):
    from agentic_debugger.bugsinpy.wsl import create_verified_context
    from agentic_debugger.quixbugs.adapter import QuixBugsPreflightFacts
    from agentic_debugger.runtime.execution import DependencyPreparation
    recipe = f"pytest=={adapter.manifest.environment['pinned_packages']['pytest']}"
    dependency = DependencyPreparation(
        pilot_task_id=adapter.manifest.task_id, manifest_fingerprint=adapter.manifest.fingerprint,
        authority_revision=adapter.manifest.authority_revision, project="quixbugs", bug_id=adapter.manifest.algorithm,
        buggy_revision=adapter.manifest.authority_revision, recipe_path=recipe,
        recipe_sha256=hashlib.sha256(recipe.encode()).hexdigest(), installed_fingerprint=env_fingerprint,
    )
    context = create_verified_context(
        root_host=root_host, python_root_posix=venv_posix, python_executable_posix=f"{venv_posix}/bin/python",
        python_version=python_version, project_cwd=".", pythonpath=(), reviewed_environment={}, dependencies=dependency, runner=runner,
    )
    facts = QuixBugsPreflightFacts(
        platform="linux", pinned_source_verified=True, license_reviewed=True,
        dependency_install_boundary_ready=True, test_command_available=True,
        workspace_cleanup_ready=True, target_annotation_reviewed=True,
        external_parent=runs_host, execution_context=context,
    )
    return context, facts


def _discover_once(smoke_runner: Any, facts: Any, sources_host: str, runs_host: str) -> tuple[Any, bool]:
    from agentic_debugger.bugsinpy.adapter import ExternalWorkspace
    from agentic_debugger.runtime.workspace import TaskWorkspace
    project_root = smoke_runner.ensure_source(Path(sources_host))
    external = ExternalWorkspace.create(runs_host, repository_root=str(REPO_ROOT), containment_root=facts.execution_context.containment.root)
    workspace = None
    cleanup_succeeded = False
    try:
        external.verifier_workspace_parent.mkdir(parents=True, exist_ok=True)
        external.assert_contained(external.verifier_workspace_parent)
        workspace = TaskWorkspace(str(project_root), parent_dir=str(external.verifier_workspace_parent))
        discovery = smoke_runner.discover(facts.execution_context, workspace)
    finally:
        if workspace is not None:
            workspace.cleanup()
        root = Path(external.root)
        external.cleanup()
        cleanup_succeeded = not root.exists()
    return discovery, cleanup_succeeded


def _evaluator_lifecycle(result: Any, expected_outcome: str) -> tuple[bool, dict[str, Any]]:
    from agentic_debugger.evaluation.runner import EvaluationStatus, LifecycleStatus, TestRecordStatus
    f2p_expected = all(record.passed for record in result.post_patch_f2p) if expected_outcome == "RESOLVED" else all(not record.passed for record in result.post_patch_f2p)
    p2p_expected = all(record.passed for record in result.post_patch_p2p)
    lifecycle = (
        result.status is EvaluationStatus.COMPLETED
        and result.outcome is not None and result.outcome.value == expected_outcome
        and result.workspace.lifecycle is LifecycleStatus.CLEANED
        and result.workspace.prepared is True and result.workspace.cleanup_attempted is True
        and result.workspace.cleaned is True and result.workspace.canonical_fixture_unchanged is True
        and result.workspace.error is None and result.timeout is False and result.diagnostic is None
        and result.baseline.valid is True
        and all(record.status not in (TestRecordStatus.ERROR, TestRecordStatus.TIMEOUT) for record in result.post_patch_f2p + result.post_patch_p2p)
        and f2p_expected and p2p_expected
    )
    return lifecycle, {
        "status": result.status.value, "outcome": result.outcome.value if result.outcome else None,
        "lifecycle_succeeded": lifecycle, "canonical_fixture_unchanged": result.workspace.canonical_fixture_unchanged,
        "cleanup_succeeded": result.workspace.cleaned, "expected_f2p_p2p_behavior": f2p_expected and p2p_expected,
        "timeout": result.timeout, "diagnostic_present": result.diagnostic is not None,
    }


def _pdb_accounting(pdb_result: Any) -> dict[str, Any]:
    from agentic_debugger.quixbugs.contained_pdb import validate_events_jsonl
    observations = pdb_result.pdb_observations
    successful = observations.get("successful_pdb_observation_count")
    failed = observations.get("failed_pdb_observation_count")
    if type(successful) is not int or type(failed) is not int or successful < 0 or failed < 0:
        raise PilotError("contained-PDB observation counts are malformed")
    decisions = list(pdb_result.gate_decisions)
    allowed = sum(1 for decision in decisions if decision.get("allowed") is True)
    rejected = sum(1 for decision in decisions if decision.get("allowed") is False)
    _require(allowed + rejected == len(decisions), "contained-PDB gate decisions have invalid allowed values")
    sessions_started = 0
    if pdb_result.events_valid is True:
        valid, _, events = validate_events_jsonl(pdb_result.events_jsonl, task_id=pdb_result.task_id)
        if valid:
            sessions_started = sum(1 for event in events if event.event_type.value == "observation" and event.name == "start_pdb_session")
    sequence = pdb_result.sequence_evidence or {}
    provenance = pdb_result.launch_plan is not None and isinstance(pdb_result.pdb_runtime_bundle_hashes, dict) and bool(pdb_result.pdb_runtime_bundle_hashes)
    qualification_passes = (
        pdb_result.verdict == "REACHABILITY_CASE_PASSED"
        and pdb_result.quixbugs_preflight.authorized
        and pdb_result.contained_preflight is not None and pdb_result.contained_preflight.authorized
        and allowed >= 1 and pdb_result.events_valid is True and sequence.get("ok") is True
        and successful >= 1 and pdb_result.cleanup_succeeded is True
        and pdb_result.canonical_source_unchanged is True and provenance and not pdb_result.diagnostics
    )
    return {
        "verdict": pdb_result.verdict, "quixbugs_preflight_authorized": pdb_result.quixbugs_preflight.authorized,
        "contained_preflight_authorized": pdb_result.contained_preflight is not None and pdb_result.contained_preflight.authorized,
        "total_gate_decisions": len(decisions), "allowed_gate_openings": allowed, "rejected_gate_decisions": rejected,
        "sessions_started": sessions_started, "successful_observations": successful, "failed_observations": failed,
        "events_valid": pdb_result.events_valid is True, "sequence_ok": sequence.get("ok") is True,
        "launch_plan_present": pdb_result.launch_plan is not None, "runtime_bundle_provenance_present": provenance,
        "diagnostics_empty": not pdb_result.diagnostics, "cleanup_succeeded": pdb_result.cleanup_succeeded,
        "canonical_source_unchanged": pdb_result.canonical_source_unchanged is True,
        "gate_decisions": decisions, "qualification_passes": qualification_passes,
    }


def _screen_task(entry: Mapping[str, Any], adapter: Any, facts: Any, smoke_runner: Any, sources_host: str, runs_host: str, resource_limits: Any) -> dict[str, Any]:
    from agentic_debugger.evaluation.task_schema import TaskSource
    from agentic_debugger.evaluation.verifier import EvaluationVerifier
    source_root = Path(sources_host)
    probe = _runtime_probe_for(entry["task_id"])
    breakpoint_line = validate_runtime_probe(probe, adapter.manifest.to_mapping(), source_root)
    preflight = adapter.preflight(facts, repository_root=str(REPO_ROOT))
    first_discovery, first_cleanup = _discover_once(smoke_runner, facts, sources_host, runs_host)
    second_discovery, second_cleanup = _discover_once(smoke_runner, facts, sources_host, runs_host)
    baseline_ok = (
        first_discovery.baseline_outcomes == second_discovery.baseline_outcomes
        and bool(first_discovery.f2p_candidates) and bool(first_discovery.p2p_candidates)
    )
    task_source = TaskSource("external", "quixbugs", adapter.source_provenance())
    commands = adapter.build_commands(fail_to_pass=first_discovery.f2p_candidates, pass_to_pass=first_discovery.p2p_candidates)
    debug_task = adapter.to_debug_task(task_source, commands)
    independent_result = EvaluationVerifier(sources_host, workspace_parent=runs_host, execution_context=facts.execution_context).evaluate(debug_task, "")
    verifier_ok, verifier_evidence = _evaluator_lifecycle(independent_result, "NO_OP")
    source = source_root / "quixbugs" / entry["implementation_path"]
    before = file_hash(source)
    pdb_result = __import__("agentic_debugger.quixbugs.contained_pdb", fromlist=["run_quixbugs_pdb_reachability_case"]).run_quixbugs_pdb_reachability_case(
        repository_root=str(REPO_ROOT), manifest_path=str(REPO_ROOT / entry["manifest_path"]), sources_parent=sources_host,
        facts=facts, resource_limits=resource_limits, runtime_probe=probe,
        hypothesis_id=f"qualification-{entry['task_id']}", hypothesis_statement="synthetic reachability hypothesis",
    )
    after = file_hash(source)
    pdb = _pdb_accounting(pdb_result)
    restoration_ok = before == after == entry["source_sha256"] and first_cleanup and second_cleanup and verifier_evidence["cleanup_succeeded"] and pdb["cleanup_succeeded"]
    statuses = {
        "dependency_status": "PASS" if preflight.authorized else "FAIL",
        "deterministic_baseline_status": "PASS" if baseline_ok else "FAIL",
        "verifier_status": "PASS" if verifier_ok else "FAIL",
        "source_restoration_status": "PASS" if restoration_ok else "FAIL",
        "contained_pdb_reachability_status": "PASS" if pdb["qualification_passes"] else "FAIL",
    }
    evidence = {
        "screened": True, **statuses, "screening_passed": all(value == "PASS" for value in statuses.values()),
        "manifest_fingerprint": adapter.manifest.fingerprint, "source_hash_before": before, "source_hash_after": after,
        "test_sha256": entry["test_sha256"], "runtime_probe": {"module_path": probe.module_path, "focus_function": probe.focus_function, "breakpoint_line": breakpoint_line},
        "baseline_reproduced": bool(first_discovery.f2p_candidates), "baseline_deterministic_across_two_checks": baseline_ok,
        "independent_verifier": verifier_evidence, "pdb": pdb, "owned_workspaces_removed": restoration_ok,
        "oracle_or_gold_material_in_record": False,
    }
    return {"task_id": entry["task_id"], **statuses, "screening_evidence": evidence}


def run_qualification(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate recorded seven-task screening, then run selected private checks."""
    validate_manifest(manifest)
    from agentic_debugger.quixbugs.adapter import QuixBugsAdapter, QuixBugsSmokeRunner, QuixBugsSourceAcquirer
    evidence = json.loads((REPO_ROOT / manifest["qualification_evidence_path"]).read_text(encoding="utf-8"))
    screening = evidence.get("screening", [])
    _require(len(screening) == len(SCREENING_TASK_IDS) and all(item.get("screening_evidence", {}).get("screening_passed") is True for item in screening), "recorded seven-task screening is incomplete")
    runner, root_host, venv_posix, env_fingerprint, runs_host, sources_host, python_version, resource_limits = _qualification_environment()
    contexts = {}
    for task_id in EXPECTED_SELECTED:
        entry = next(item for item in manifest["inventory"] if item["task_id"] == task_id)
        adapter = QuixBugsAdapter.from_manifest(REPO_ROOT / entry["manifest_path"])
        context, facts = _make_task_facts(adapter, runner, root_host, venv_posix, env_fingerprint, runs_host, python_version)
        contexts[task_id] = (adapter, facts)
    selected_full = []
    for task_id in EXPECTED_SELECTED:
        entry = next(item for item in manifest["inventory"] if item["task_id"] == task_id)
        adapter, facts = contexts[task_id]
        smoke = QuixBugsSmokeRunner(adapter, QuixBugsSourceAcquirer()).run(facts=facts, sources_parent=sources_host, external_parent=runs_host, repository_root=str(REPO_ROOT))
        evaluation = smoke.evaluation
        private_ok = False
        private_evidence = {"status": None, "outcome": None, "lifecycle_succeeded": False, "cleanup_succeeded": smoke.cleanup_succeeded, "expected_f2p_p2p_behavior": False}
        if evaluation is not None:
            private_ok, private_evidence = _evaluator_lifecycle(evaluation, "RESOLVED")
            private_evidence["smoke_verdict"] = smoke.verdict
        selected_full.append({"task_id": task_id, "screening_pdb": next(item["screening_evidence"]["pdb"] for item in screening if item["task_id"] == task_id), "private_correct_qualification_passes": private_ok, "private_correct_evaluator": private_evidence, "gold_oracle_material_in_record": False})
    return {
        "manifest_hash": manifest_hash(manifest), "provider_contacted": False, "network_activity": False,
        "screened_task_count": len(screening), "all_screening_passed": all(item["screening_evidence"]["screening_passed"] for item in screening),
        "screening": screening, "selected_full_qualification": selected_full,
        "all_passed": all(item["screening_evidence"]["screening_passed"] for item in screening) and all(item["private_correct_qualification_passes"] for item in selected_full),
    }


def run_screening(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Run the complete seven-task screening without private correct checks."""
    # The screening run is the producer of the evidence file.  It validates
    # the frozen contract structure, but cannot require the previous evidence
    # digest while replacing that evidence after a deliberate qualification
    # rerun.
    validate_manifest(manifest, require_screening=False, require_qualification_binding=False)
    from agentic_debugger.quixbugs.adapter import QuixBugsAdapter, QuixBugsSmokeRunner, QuixBugsSourceAcquirer
    runner, root_host, venv_posix, env_fingerprint, runs_host, sources_host, python_version, resource_limits = _qualification_environment()
    screening = []
    for task_id in SCREENING_TASK_IDS:
        entry = next(item for item in manifest["inventory"] if item["task_id"] == task_id)
        adapter = QuixBugsAdapter.from_manifest(REPO_ROOT / entry["manifest_path"])
        _, facts = _make_task_facts(adapter, runner, root_host, venv_posix, env_fingerprint, runs_host, python_version)
        screening.append(_screen_task(entry, adapter, facts, QuixBugsSmokeRunner(adapter, QuixBugsSourceAcquirer()), sources_host, runs_host, resource_limits))
    return {"manifest_hash": manifest_hash(manifest), "provider_contacted": False, "network_activity": False, "screened_task_count": len(screening), "all_screening_passed": all(item["screening_evidence"]["screening_passed"] for item in screening), "screening": screening}


def plan(manifest: Mapping[str, Any]) -> dict[str, Any]:
    validate_manifest(manifest)
    return {"campaign_id": CAMPAIGN_ID, "manifest_hash": manifest_hash(manifest), "cases": manifest["case_order"], "provider_contacted": False}


def live(manifest: Mapping[str, Any], authorization: Path | None = None) -> None:
    if authorization is None or not authorization.is_file():
        raise PilotError("live mode requires a separate explicit authorization artifact")
    validate_manifest(manifest)
    try:
        artifact = json.loads(authorization.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotError(f"invalid live authorization artifact: {exc}") from exc
    _validate_live_authorization(manifest, artifact)
    raise PilotError("live execution is intentionally unavailable in this preregistration task")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=("validate", "screen", "qualify", "plan", "dry-run", "live"), default="validate")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--authorization", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.mode == "validate":
            print(json.dumps({"manifest_hash": validate_manifest(manifest), "valid": True}, indent=2, sort_keys=True))
        elif args.mode == "screen":
            screening = run_screening(manifest)
            print(json.dumps(screening, indent=2, sort_keys=True))
            return 0 if screening["all_screening_passed"] else 1
        elif args.mode == "plan":
            print(json.dumps(plan(manifest), indent=2, sort_keys=True))
        elif args.mode == "dry-run":
            print(json.dumps(dry_run(manifest), indent=2, sort_keys=True))
        elif args.mode == "qualify":
            qualification = run_qualification(manifest)
            print(json.dumps(qualification, indent=2, sort_keys=True))
            return 0 if qualification["all_passed"] else 1
        else:
            live(manifest, args.authorization)
        return 0
    except PilotError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
