"""Fail-closed Pilot-10 per-task result schema and post-pilot formulas."""

from __future__ import annotations

from typing import Any, Mapping

PILOT_RESULT_SCHEMA_VERSION = "gpt-oss-swerebench-v2-pilot-result-v1"
REQUIRED_IDENTITY = (
    "task_id",
    "instance_id",
    "repository",
    "base_commit",
    "manifest_order_index",
    "harness_commit",
    "model_profile_id",
    "model_alias",
    "upstream_model",
    "policy",
    "protocol",
)
REQUIRED_RUNTIME = (
    "session_id",
    "wall_clock_seconds",
    "logical_model_calls",
    "transport_attempts",
    "adapter_retry_count",
    "fallback_count",
    "token_usage",
    "provider_failures",
)
REQUIRED_TRAJECTORY = (
    "baseline_reproduced",
    "understand_reached",
    "hypotheses",
    "source_operations",
    "test_operations",
    "patch_attempts",
    "patch_rejections",
    "candidate_applied",
    "validate_sequence",
    "terminal_reason",
)
REQUIRED_PDB = (
    "pdb_eligible",
    "pdb_gate_opened",
    "pdb_entered",
    "debugger_actions",
    "debugger_observations",
    "runtime_evidence_preceded_patch",
    "pdb_not_exercised",
    "classification",
)
REQUIRED_VERIFIER = (
    "verifier_ran",
    "verifier_infrastructure_valid",
    "baseline_valid",
    "fail_to_pass",
    "pass_to_pass",
    "full_suite",
    "verifier_outcome",
    "cleanup",
)
REQUIRED_SCIENCE = (
    "admissible_model_result",
    "infrastructure_invalid",
    "contaminated",
    "provider_invalid",
    "resolved",
    "unresolved",
    "debugger_assisted_resolved",
    "execution_classification",
)
EXECUTION_CLASSIFICATIONS = frozenset(
    {
        "admissible_model_failure",
        "model_controller_failure_before_candidate",
        "candidate_produced_verifier_not_reached",
        "independent_verifier_unresolved",
        "independent_verifier_resolved",
        "infrastructure_invalid",
        "provider_invalid",
        "contaminated",
    }
)
PDB_CLASSIFICATIONS = frozenset(
    {
        "pdb_unavailable_by_treatment_contract",
        "pdb_eligible_gate_not_opened",
        "pdb_gate_opened_session_not_entered",
        "pdb_session_entered_breakpoint_not_reached",
        "real_pdb_exercised",
        "bug_relevant_runtime_evidence_preceded_patch",
        "debugger_assisted_resolved",
    }
)
SCIENTIFIC_CLASSIFICATIONS = frozenset(
    {
        "admissible_unresolved",
        "admissible_resolved",
        "debugger_assisted_resolved",
        "infrastructure_invalid",
        "contaminated",
        "provider_invalid",
    }
)

POST_PILOT_FORMULAS = {
    "mean_task_wall_seconds": "mean(wall_clock_seconds over admissible rows)",
    "median_task_wall_seconds": "median(wall_clock_seconds over admissible rows)",
    "p95_task_wall_seconds": "percentile_95(wall_clock_seconds over admissible rows)",
    "total_provider_wall_seconds": "sum(wall_clock_seconds over all executed provider rows)",
    "logical_calls_per_task": "mean(logical_model_calls over admissible rows)",
    "tokens_per_task": "mean(token_usage.total) when durably available else null",
    "resolved_rate": "count(resolved)/count(admissible_model_result)",
    "pdb_exercise_rate": "count(pdb_entered)/count(admissible_model_result)",
    "debugger_assisted_resolved_count": "count(debugger_assisted_resolved)",
    "infrastructure_invalid_rate": "count(infrastructure_invalid)/count(all rows)",
    "projected_30_runtime_seconds": "10 * mean_task_wall_seconds * 3",
    "projected_full_clean_validation_runtime_seconds": (
        "mean_task_wall_seconds * eligible_population_n"
    ),
}


class PilotResultSchemaError(ValueError):
    """A Pilot-10 result object is missing, extra, or contradictory."""


def classify_execution_result(
    *,
    controller_completed: bool,
    candidate_produced: bool,
    verifier_ran: bool,
    verifier_resolved: bool,
    verifier_infrastructure_valid: bool,
    provider_invalid: bool = False,
    runtime_infrastructure_invalid: bool = False,
    contaminated: bool = False,
) -> str:
    """Map durable evidence to the frozen execution vocabulary."""

    if contaminated:
        return "contaminated"
    if provider_invalid:
        return "provider_invalid"
    if runtime_infrastructure_invalid:
        return "infrastructure_invalid"
    if not verifier_infrastructure_valid:
        return "infrastructure_invalid"
    if verifier_ran and verifier_resolved:
        return "independent_verifier_resolved"
    if verifier_ran:
        return "independent_verifier_unresolved"
    if candidate_produced:
        return "candidate_produced_verifier_not_reached"
    if not controller_completed:
        return "model_controller_failure_before_candidate"
    return "admissible_model_failure"


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PilotResultSchemaError(f"{label} must be a mapping")
    return value


def _require_keys(mapping: Mapping[str, Any], keys: tuple[str, ...], label: str) -> None:
    missing = [key for key in keys if key not in mapping]
    if missing:
        raise PilotResultSchemaError(f"{label} missing {missing}")


def validate_pilot_result(payload: Any) -> dict[str, Any]:
    data = _require_mapping(payload, "result")
    if data.get("schema_version") != PILOT_RESULT_SCHEMA_VERSION:
        raise PilotResultSchemaError(
            f"unsupported result schema {data.get('schema_version')!r}"
        )
    _require_keys(
        data,
        (
            "schema_version",
            "identity",
            "runtime",
            "trajectory",
            "pdb",
            "verification",
            "science",
        ),
        "result",
    )
    extra = set(data) - {
        "schema_version",
        "identity",
        "runtime",
        "trajectory",
        "pdb",
        "verification",
        "science",
        "notes",
    }
    if extra:
        raise PilotResultSchemaError(f"unknown result fields: {sorted(extra)}")

    identity = _require_mapping(data["identity"], "identity")
    _require_keys(identity, REQUIRED_IDENTITY, "identity")
    runtime = _require_mapping(data["runtime"], "runtime")
    _require_keys(runtime, REQUIRED_RUNTIME, "runtime")
    trajectory = _require_mapping(data["trajectory"], "trajectory")
    _require_keys(trajectory, REQUIRED_TRAJECTORY, "trajectory")
    pdb = _require_mapping(data["pdb"], "pdb")
    _require_keys(pdb, REQUIRED_PDB, "pdb")
    verification = _require_mapping(data["verification"], "verification")
    _require_keys(verification, REQUIRED_VERIFIER, "verification")
    science = _require_mapping(data["science"], "science")
    _require_keys(science, REQUIRED_SCIENCE, "science")

    if science.get("classification") not in SCIENTIFIC_CLASSIFICATIONS:
        raise PilotResultSchemaError(
            f"unknown scientific classification: {science.get('classification')!r}"
        )
    if science.get("execution_classification") not in EXECUTION_CLASSIFICATIONS:
        raise PilotResultSchemaError(
            "unknown execution classification: "
            f"{science.get('execution_classification')!r}"
        )
    if pdb.get("classification") not in PDB_CLASSIFICATIONS:
        raise PilotResultSchemaError(
            f"unknown PDB classification: {pdb.get('classification')!r}"
        )
    boolean_fields = (
        "admissible_model_result",
        "infrastructure_invalid",
        "contaminated",
        "provider_invalid",
        "resolved",
        "unresolved",
        "debugger_assisted_resolved",
    )
    for field in boolean_fields:
        if type(science.get(field)) is not bool:
            raise PilotResultSchemaError(f"science.{field} must be a boolean")
    if science.get("resolved") and science.get("unresolved"):
        raise PilotResultSchemaError("a row cannot be both resolved and unresolved")
    execution = science.get("execution_classification")
    invalid_execution = execution in {"infrastructure_invalid", "provider_invalid", "contaminated"}
    expected_science = (
        "infrastructure_invalid" if execution == "infrastructure_invalid" else
        "provider_invalid" if execution == "provider_invalid" else
        "contaminated" if execution == "contaminated" else
        "debugger_assisted_resolved" if science.get("debugger_assisted_resolved") else
        "admissible_resolved" if execution == "independent_verifier_resolved" else
        "admissible_unresolved"
    )
    if science.get("classification") != expected_science:
        raise PilotResultSchemaError(
            "science.classification contradicts execution_classification"
        )
    if science.get("infrastructure_invalid") != (execution == "infrastructure_invalid"):
        raise PilotResultSchemaError("infrastructure-invalid flag contradicts execution")
    if science.get("provider_invalid") != (execution == "provider_invalid"):
        raise PilotResultSchemaError("provider-invalid flag contradicts execution")
    if science.get("contaminated") != (execution == "contaminated"):
        raise PilotResultSchemaError("contaminated flag contradicts execution")
    if invalid_execution and science.get("admissible_model_result"):
        raise PilotResultSchemaError(
            "invalid/provider/contaminated rows must not be admissible model results"
        )
    if invalid_execution and (science.get("resolved") or science.get("unresolved")):
        raise PilotResultSchemaError("invalid/provider/contaminated rows have no repair outcome")
    if execution == "independent_verifier_resolved":
        if not science.get("resolved") or science.get("unresolved"):
            raise PilotResultSchemaError("verifier-resolved execution must be RESOLVED only")
    elif not invalid_execution:
        if science.get("resolved"):
            raise PilotResultSchemaError("only independent verifier RESOLVED may set resolved")
        if not science.get("unresolved"):
            raise PilotResultSchemaError("admissible non-resolved rows must be UNRESOLVED")
    if science.get("infrastructure_invalid") and science.get("admissible_model_result"):
        raise PilotResultSchemaError(
            "infrastructure-invalid rows cannot also be admissible model results"
        )
    if science.get("debugger_assisted_resolved"):
        if not science.get("resolved"):
            raise PilotResultSchemaError(
                "debugger-assisted RESOLVED requires verifier RESOLVED"
            )
        if pdb.get("pdb_not_exercised") or not pdb.get("pdb_entered"):
            raise PilotResultSchemaError(
                "debugger-assisted RESOLVED requires actual PDB exercise"
            )
        if not pdb.get("runtime_evidence_preceded_patch"):
            raise PilotResultSchemaError(
                "debugger-assisted RESOLVED requires runtime evidence before the patch"
            )
    if science.get("resolved") and not verification.get("verifier_ran"):
        raise PilotResultSchemaError("RESOLVED requires the independent verifier")
    if science.get("resolved"):
        if not verification.get("verifier_infrastructure_valid"):
            raise PilotResultSchemaError("RESOLVED requires a valid evaluator result")
        if verification.get("verifier_outcome") != "RESOLVED":
            raise PilotResultSchemaError("RESOLVED science row has a contradictory verifier outcome")
    if pdb.get("pdb_not_exercised") and pdb.get("pdb_entered"):
        raise PilotResultSchemaError("PDB entered and PDB NOT EXERCISED cannot both be true")
    if (
        pdb.get("classification") == "pdb_unavailable_by_treatment_contract"
        and not pdb.get("pdb_not_exercised")
    ):
        raise PilotResultSchemaError("treatment-unavailable PDB must be NOT EXERCISED")
    return data


def empty_result_template() -> dict[str, Any]:
    return {
        "schema_version": PILOT_RESULT_SCHEMA_VERSION,
        "identity": {key: None for key in REQUIRED_IDENTITY},
        "runtime": {key: None for key in REQUIRED_RUNTIME},
        "trajectory": {key: None for key in REQUIRED_TRAJECTORY},
        "pdb": {key: None for key in REQUIRED_PDB},
        "verification": {key: None for key in REQUIRED_VERIFIER},
        "science": {key: None for key in REQUIRED_SCIENCE + ("classification",)},
    }
