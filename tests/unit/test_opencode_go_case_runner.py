"""Case-runner binding and campaign reconciliation tests for the OpenCode Go execution adapter."""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import quixbugs_opencode_go_adapter as adapter
import quixbugs_live_runner_v2 as runner
import quixbugs_paired_pilot as pilot

from opencode_go_test_support import prepare_wrapper_environment
from test_opencode_go_transport_factory import (
    _authorization,
    _configuration,
    _observed,
)


@pytest.fixture
def manifest():
    return pilot.load_manifest(pilot.MANIFEST_PATH_V2)


@pytest.fixture
def synthetic_executable() -> Path:
    return REPO_ROOT / "scripts" / "opencode_go_synthetic_executable.py"


class FakeLiveResult:
    """A deterministic stand-in for the accepted LiveCaseResult mapping."""

    def __init__(self, mapping: dict) -> None:
        self._mapping = dict(mapping)

    def to_mapping(self) -> dict:
        return copy.deepcopy(self._mapping)


def _live_mapping(manifest, case, **overrides) -> dict:
    value = {
        "schema_version": "1.0",
        "case_id": case["case_id"],
        "run_id": f"live-{case['case_id']}",
        "trajectory_id": f"live-{case['case_id']}",
        "task_id": case["task_id"],
        "policy": case["policy"],
        "repetition": 1,
        "status": "UNRESOLVED",
        "controller": {"completed": True, "final_state": "Done", "stop_reason": None, "model_calls": 1, "exception": False},
        "verifier": {
            "executed": True, "failure": False, "status": "COMPLETED", "outcome": "NO_OP",
            "baseline_valid": True, "patch_application": None, "fail_to_pass": {"passed": 0, "total": 2},
            "pass_to_pass": {"passed": 2, "total": 2}, "workspace_cleaned": True,
            "canonical_fixture_unchanged": True, "localization": {"outcome": "NO_LOCALIZATION"},
        },
        "measurements": {
            "model_request_count": 1, "model_response_count": 1, "retry_count": 0,
            "provider_error_count": 0, "provider_error_kinds": [],
            "token_usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16,
                            "provider_reported": True, "missing_fields": []},
            "termination_reason": None,
            "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
            "tool_call_count": 2, "case_elapsed_duration_ms": 1200,
            "model_phase_elapsed_duration_ms": 1000, "model_transport_duration_ms": 1000,
            "elapsed_scope": "case_observed; model_phase=transport_only",
        },
        "reporting": {
            "mode": "live", "completed": True, "partial": False, "interrupted": False,
            "event_recorded": True, "cleanup": "cleaned", "case_directory_owned": True,
        },
        "events_jsonl": _events_jsonl(),
        "diagnostics": [],
        "evidence": {"pdb_gate_decisions": [], "directive_rejections": []},
    }
    value.update(overrides)
    return value


def _events_jsonl(*, with_pdb_action: bool = False) -> str:
    lines = [
        {"event_type": "decision", "name": "decision", "state": "Reproduce", "payload": {"directive_kind": "action", "model_call_index": 0}},
        {"event_type": "action", "name": "run_reproduction", "state": "Reproduce", "payload": {"action": {"name": "run_reproduction"}}},
        {"event_type": "observation", "name": "run_reproduction", "state": "Reproduce", "payload": {"observation": {"name": "run_reproduction", "status": "ok", "payload": {"phase": "baseline", "failure_reproduced": True}}}},
        {"event_type": "decision", "name": "decision", "state": "Understand", "payload": {"directive_kind": "add_hypothesis", "model_call_index": 1}},
        {"event_type": "transition", "name": "transition", "state": "Understand", "payload": {"target_state": "Understand"}},
        {"event_type": "final", "name": "final", "state": "Done", "payload": {"final_state": "Done", "stop_reason": "completed", "model_calls": 1}},
    ]
    if with_pdb_action:
        lines.insert(5, {"event_type": "action", "name": "start_pdb_session", "state": "RuntimeEvidence", "payload": {"action": {"name": "start_pdb_session"}}})
    return "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n"


class FakeFacts:
    def __init__(self) -> None:
        self.execution_context = object()


def _harness(tmp_path, manifest, synthetic_executable, *, claim: bool = True):
    authorization = _authorization(manifest, tmp_path)
    configuration = _configuration(manifest, tmp_path, synthetic_executable)
    configuration["authorization_hash"] = runner.authorization_hash(authorization)
    observed = _observed(manifest)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    adapter.bind_adapter_configuration(validated, manifest, authorization, observed)
    binding = adapter.build_runtime_identity_binding(authorization, observed, validated)
    output_root = tmp_path / "attempt-out"
    if claim:
        runner.claim_output_root(
            output_root,
            attempt_identity=authorization["campaign_attempt_identity"],
            authorization_hash=runner.authorization_hash(authorization),
            campaign_manifest_hash=pilot.manifest_hash(manifest),
        )
        ledger = runner.AttemptLedger(output_root / "ledger.json")
        ledger.claim({
            "attempt_identity": authorization["campaign_attempt_identity"],
            "authorization_hash": runner.authorization_hash(authorization),
            "campaign_manifest_hash": pilot.manifest_hash(manifest),
            "accepted_baseline": runner.ACCEPTED_BASELINE,
            "planning_baseline_commit": manifest["planning_baseline_commit"],
            "execution_commit": runner.ACCEPTED_BASELINE,
            "case_ids": [case["case_id"] for case in manifest["case_order"]],
            "route_binding": {"execution_commit": runner.ACCEPTED_BASELINE},
            "status": "STARTED",
            "created_at": observed["observed_at"],
            "updated_at": observed["observed_at"],
            "output_root": str(output_root.resolve()),
        })
    factory = adapter.OpenCodeGoTransportFactory(
        authorization=authorization,
        execution_commit=runner.ACCEPTED_BASELINE,
        route_observation=observed,
        configuration=validated,
        binding=binding,
        attempt_identity=authorization["campaign_attempt_identity"],
        output_root=output_root,
        ledger_path=output_root / "ledger.json",
        evidence_dir=output_root / "private",
        environment_override=prepare_wrapper_environment(tmp_path, synthetic_executable),
    )
    environment = adapter.QuixBugsCaseEnvironment(
        repository_root=str(tmp_path / "repo"),
        sources_parent=str(tmp_path / "sources"),
        facts_provider=lambda: FakeFacts(),
    )
    return {
        "authorization": authorization,
        "configuration": validated,
        "observed": observed,
        "binding": binding,
        "factory": factory,
        "case_runner": None,
        "output_root": output_root,
        "manifest": manifest,
        "environment": environment,
    }


def _clean_git_state(commit):
    return runner.GitRepositoryState(
        head=commit,
        execution_commit_exists=True,
        execution_commit_descends_from_baseline=True,
        tracked_working_tree_clean=True,
        git_index_clean=True,
    )


class FakeExecutor:
    """Records accepted-executor invocations and returns per-case results."""

    def __init__(self, manifest, attempt_identity, result_builder, *, call_transport: bool = False, harness=None) -> None:
        self.calls: list[dict] = []
        self.result_builder = result_builder
        self.call_transport = call_transport
        self._manifest = manifest
        self.harness = harness
        self._attempt_identity = attempt_identity

    def case_for(self, kwargs: dict) -> dict:
        policy_value = kwargs["policy"].value if hasattr(kwargs["policy"], "value") else str(kwargs["policy"])
        manifest_path = str(kwargs["manifest_path"]).replace("\\", "/")
        stem = Path(manifest_path).stem
        task_slug = stem.split("_SMOKE")[0].lower().replace("_", "-")
        task_id = f"quixbugs-{task_slug}-smoke-v1"
        return next(
            case for case in self._manifest["case_order"]
            if case["task_id"] == task_id and case["policy"] == policy_value
        )

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.call_transport:
            kwargs["transport"].request({"directive_feedback": None}, 30.0)
        case = self.case_for(kwargs)
        result = self.result_builder(kwargs, case, self.harness)
        return result if isinstance(result, FakeLiveResult) else FakeLiveResult(result)


# ---- binding to the accepted executor ---------------------------------------


def test_case_runner_uses_fresh_transport_and_calls_accepted_executor(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)
    calls: list[dict] = []
    attempt_identity = harness["authorization"]["campaign_attempt_identity"]
    call_index = {"value": 0}

    def fake_executor(**kwargs):
        case = manifest["case_order"][call_index["value"]]
        call_index["value"] += 1
        calls.append({"transport": kwargs["transport"], "policy": kwargs["policy"], "pdb_binding": kwargs.get("pdb_identity_binding")})
        return FakeLiveResult(_live_mapping(manifest, case))

    runner_obj = adapter.OpenCodeGoCaseRunner(
        binding=harness["binding"],
        configuration=harness["configuration"],
        factory=harness["factory"],
        environment=harness["environment"],
        manifest=manifest,
        live_executor=fake_executor,
    )
    first_case = manifest["case_order"][0]
    second_case = manifest["case_order"][1]
    transport_one = harness["factory"].prepare(first_case)
    outcome_one = runner_obj(first_case, attempt_identity=attempt_identity,
                             run_id=runner.deterministic_run_id(attempt_identity, first_case),
                             session_id="s1", transport=object(),
                             route_observation=harness["observed"], budgets=manifest["budgets"])
    transport_two = harness["factory"].prepare(second_case)
    outcome_two = runner_obj(second_case, attempt_identity=attempt_identity,
                             run_id=runner.deterministic_run_id(attempt_identity, second_case),
                             session_id="s2", transport=object(),
                             route_observation=harness["observed"], budgets=manifest["budgets"])
    assert transport_one is not transport_two
    assert len(calls) == 2
    assert calls[0]["policy"].value == "pdb-on-uncertainty"
    assert calls[0]["pdb_binding"] == ("OpenCode Go", "opencode-go/test-deepseek-v4-flash", "max")
    assert calls[1]["policy"].value == "static-baseline"
    assert calls[1]["pdb_binding"] is None
    assert outcome_one["terminal_status"] == "UNRESOLVED"
    assert outcome_two["terminal_status"] == "UNRESOLVED"


def test_case_runner_passes_attempt_identity_and_limits(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)
    captured: dict = {}
    attempt_identity = harness["authorization"]["campaign_attempt_identity"]

    def fake_executor(**kwargs):
        captured.update(kwargs)
        policy_value = kwargs["policy"].value if hasattr(kwargs["policy"], "value") else str(kwargs["policy"])
        case = next(case for case in manifest["case_order"] if case["policy"] == policy_value)
        return FakeLiveResult(_live_mapping(manifest, case))

    runner_obj = adapter.OpenCodeGoCaseRunner(
        binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
        environment=harness["environment"], manifest=manifest, live_executor=fake_executor,
    )
    case = manifest["case_order"][0]
    harness["factory"].prepare(case)
    runner_obj(case, attempt_identity=attempt_identity,
               run_id=runner.deterministic_run_id(attempt_identity, case),
               session_id="session-1", transport=object(),
               route_observation=harness["observed"], budgets=manifest["budgets"])
    assert captured["evaluation_id"] == attempt_identity
    assert captured["repetition"] == 1
    assert captured["repository_root"] == str(tmp_path / "repo")
    assert captured["manifest_path"].endswith("FIND_IN_SORTED_SMOKE_MANIFEST_V1.json")
    assert captured["sources_parent"] == str(tmp_path / "sources")
    assert captured["limits"].max_retries == 1
    assert captured["limits"].max_controller_steps == manifest["budgets"]["max_logical_model_calls"]
    assert captured["limits"].max_model_phase_seconds == 40
    assert captured["config"].model_name == "opencode-go/test-deepseek-v4-flash"
    assert captured["config"].command[0] == sys.executable


# ---- outcome mapping ---------------------------------------------------------


def test_outcome_mapping_reconciles_with_transport(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)
    case = manifest["case_order"][1]
    result = FakeLiveResult(_live_mapping(manifest, case))
    inner = harness["factory"].prepare(case)
    outcome = adapter._outcome_from_live_case(
        case, result, harness["observed"], inner,
        transport_attempts=inner.process_attempts, policy_value=case["policy"], run_id="run-1",
        source_hash=next(item["source_sha256"] for item in manifest["inventory"] if item["task_id"] == case["task_id"]),
    )
    runner.validate_case_outcome(outcome)
    runner.enforce_case_budgets(outcome, manifest, case_policy=case["policy"])
    assert outcome["provider_process_attempts"] == 0
    assert outcome["logical_model_calls"] == 1
    assert outcome["retries"] == 0
    assert outcome["prompt_tokens"] == 11
    assert outcome["completion_tokens"] == 5
    # No provider response reported a monetary cost, so the case execution
    # cost is the frozen schema's absence representation (zero) and is never
    # taken from the preflight route observation.
    assert outcome["provider_reported_cost"] == 0.0
    assert outcome["provider_reported_cost_observed"] is False
    assert outcome["baseline_reproduction"] is True
    assert outcome["public_request_hash"] and len(outcome["public_request_hash"]) == 64
    assert outcome["source_hash"] == next(item["source_sha256"] for item in manifest["inventory"] if item["task_id"] == case["task_id"])
    assert outcome["transport_evidence"] == {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False}
    assert outcome["terminal_transport_evidence"]["final_attempt_classification"] == "COMPLETED_RESPONSE"


def test_case_cost_propagation_distinguishes_absent_zero_positive(tmp_path, manifest, synthetic_executable):
    """Absent, explicitly-zero, and positive provider-reported costs are
    distinguished and propagated into the case outcome; the preflight route
    observation cost is never used as the case execution cost."""
    harness = _harness(tmp_path, manifest, synthetic_executable)
    case = manifest["case_order"][1]
    mapping = _live_mapping(manifest, case)
    inner = harness["factory"].prepare(case)

    outcome_absent = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=inner.process_attempts, policy_value=case["policy"], run_id="run-absent",
        source_hash="0" * 64,
    )
    assert outcome_absent["provider_reported_cost"] == 0.0
    assert outcome_absent["provider_reported_cost_observed"] is False
    assert outcome_absent["provider_cost_report_count"] == 0

    inner.reported_costs.append(0.0)
    outcome_zero = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=inner.process_attempts, policy_value=case["policy"], run_id="run-zero",
        source_hash="0" * 64,
    )
    assert outcome_zero["provider_reported_cost"] == 0.0
    assert outcome_zero["provider_reported_cost_observed"] is True
    assert outcome_zero["provider_cost_report_count"] == 1

    inner.reported_costs.append(0.0042)
    inner.reported_costs.append(0.0010)
    outcome_positive = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=inner.process_attempts, policy_value=case["policy"], run_id="run-positive",
        source_hash="0" * 64,
    )
    assert outcome_positive["provider_reported_cost"] == pytest.approx(0.0052)
    assert outcome_positive["provider_reported_cost_observed"] is True
    assert outcome_positive["provider_cost_report_count"] == 3
    assert outcome_positive["provider_reported_cost"] != harness["observed"]["provider_reported_cost"]


def test_outcome_mapping_pdb_counts_from_controller_gate(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)
    case = manifest["case_order"][0]
    decisions = [
        {"source_state": "Understand", "failure_reproduced": True, "allowed": True, "reason": "uncertainty"},
        {"source_state": "Understand", "failure_reproduced": True, "allowed": False, "reason": "budget"},
    ]
    mapping = _live_mapping(manifest, case, **{
        "status": "PDB_NOT_REACHED",
        "evidence": {"pdb_gate_decisions": decisions, "directive_rejections": []},
        "events_jsonl": _events_jsonl(with_pdb_action=True),
        "verifier": {"executed": True, "failure": False, "status": "COMPLETED", "outcome": "NO_OP", "patch_application": None, "localization": {"outcome": "NO_LOCALIZATION"}},
    })
    inner = harness["factory"].prepare(case)
    outcome = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=inner.process_attempts, policy_value=case["policy"], run_id="run-1",
        source_hash="0" * 64,
    )
    assert outcome["pdb_counts"] == {
        "total_gate_decisions": 2, "allowed_gate_openings": 1, "rejected_gate_decisions": 1,
        "sessions_started": 1, "successful_observations": 0, "failed_observations": 0,
    }
    assert outcome["pdb_sessions_started"] == 1
    runner.enforce_case_budgets(outcome, manifest, case_policy="pdb-on-uncertainty")


def test_static_policy_pdb_prohibition_via_budget_enforcement(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)
    case = manifest["case_order"][1]
    mapping = _live_mapping(manifest, case, **{
        "status": "UNRESOLVED",
        "evidence": {"pdb_gate_decisions": [{"allowed": True, "reason": "uncertainty"}], "directive_rejections": []},
    })
    inner = harness["factory"].prepare(case)
    outcome = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=inner.process_attempts, policy_value="static-baseline", run_id="run-1",
        source_hash="0" * 64,
    )
    with pytest.raises(runner.StaticPolicyPdbViolation):
        runner.enforce_case_budgets(outcome, manifest, case_policy="static-baseline")


def test_malformed_response_exhaustion_maps_to_typed_status(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)
    case = manifest["case_order"][1]
    mapping = _live_mapping(manifest, case, **{
        "status": "PROVIDER_ERROR",
        "measurements": {
            "model_request_count": 2, "model_response_count": 2, "retry_count": 1,
            "provider_error_count": 2, "provider_error_kinds": ["invalid_model_response"],
            "token_usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                            "provider_reported": False, "missing_fields": ["prompt_tokens"]},
            "termination_reason": "invalid_model_response",
            "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
            "tool_call_count": 0, "case_elapsed_duration_ms": 500,
            "model_phase_elapsed_duration_ms": 400, "model_transport_duration_ms": 400,
            "elapsed_scope": "case_observed; model_phase=transport_only",
        },
        "evidence": {"pdb_gate_decisions": [], "directive_rejections": [
            {"category": "malformed_directive", "message": "missing directive", "rejected_transport_attempt": 1},
        ]},
    })
    inner = harness["factory"].prepare(case)
    outcome = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=2, policy_value="static-baseline", run_id="run-1",
        source_hash="0" * 64,
    )
    assert outcome["terminal_status"] == "INVALID_MODEL_RESPONSE"
    assert outcome["terminal_reason_code"] == "MALFORMED_RESPONSE"
    assert outcome["malformed_directive_rejections"] == 1
    assert outcome["bounded_directive_feedback_events"] == 1
    assert outcome["valid_directives"] == 1
    assert outcome["provider_process_attempts"] == 2
    runner.enforce_case_budgets(outcome, manifest, case_policy="static-baseline")


def test_provider_timeout_maps_to_typed_terminal(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)
    case = manifest["case_order"][1]
    mapping = _live_mapping(manifest, case, **{
        "status": "TIMED_OUT",
        "measurements": {
            "model_request_count": 1, "model_response_count": 0, "retry_count": 0,
            "provider_error_count": 1, "provider_error_kinds": ["request_timeout"],
            "token_usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                            "provider_reported": False, "missing_fields": []},
            "termination_reason": "request_timeout",
            "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
            "tool_call_count": 0, "case_elapsed_duration_ms": 1000,
            "model_phase_elapsed_duration_ms": 900, "model_transport_duration_ms": 900,
            "elapsed_scope": "case_observed; model_phase=transport_only",
        },
        "evidence": {"pdb_gate_decisions": [], "directive_rejections": []},
    })
    inner = harness["factory"].prepare(case)
    inner.last_timed_out = True
    outcome = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=inner.process_attempts, policy_value="static-baseline", run_id="run-1",
        source_hash="0" * 64,
    )
    assert outcome["terminal_status"] == "PROVIDER_ERROR"
    assert outcome["terminal_transport_evidence"]["final_attempt_classification"] == "TIMEOUT"
    assert outcome["terminal_transport_evidence"]["timed_out"] is True
    assert outcome["transport_evidence"]["provider_error"] is True
    runner.enforce_case_budgets(outcome, manifest, case_policy="static-baseline")


def test_harness_error_maps_to_pre_provider_infrastructure(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)
    case = manifest["case_order"][1]
    mapping = _live_mapping(manifest, case, **{
        "status": "HARNESS_ERROR",
        "controller": {"completed": False, "final_state": None, "stop_reason": None, "model_calls": 0, "exception": False},
        "verifier": {"executed": False, "failure": False, "status": None, "outcome": None, "patch_application": None, "localization": {"outcome": "NO_LOCALIZATION"}},
        "measurements": {
            "model_request_count": 0, "model_response_count": 0, "retry_count": 0,
            "provider_error_count": 0, "provider_error_kinds": [],
            "token_usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                            "provider_reported": False, "missing_fields": []},
            "termination_reason": None,
            "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
            "tool_call_count": 0, "case_elapsed_duration_ms": 0,
            "model_phase_elapsed_duration_ms": 0, "model_transport_duration_ms": 0,
            "elapsed_scope": "case_observed; model_phase=transport_only",
        },
        "events_jsonl": "",
        "evidence": {"pdb_gate_decisions": [], "directive_rejections": []},
    })
    inner = harness["factory"].prepare(case)
    outcome = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=inner.process_attempts, policy_value="static-baseline", run_id="run-1",
        source_hash=None,
    )
    assert outcome["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert outcome["infrastructure_evidence"]["stage"] == "pre_provider"
    assert outcome["infrastructure_evidence"]["reason_code"] == "WORKSPACE_FAILURE"
    runner.enforce_case_budgets(outcome, manifest, case_policy="static-baseline")


# ---- campaign-level reconciliation -------------------------------------------


def _run_campaign_with(tmp_path, manifest, synthetic_executable, result_builder, *, call_transport=False):
    harness = _harness(tmp_path, manifest, synthetic_executable, claim=False)
    fake_executor = FakeExecutor(
        manifest,
        harness["authorization"]["campaign_attempt_identity"],
        result_builder,
        call_transport=call_transport,
        harness=harness,
    )
    runner_obj = adapter.OpenCodeGoCaseRunner(
        binding=harness["binding"],
        configuration=harness["configuration"],
        factory=harness["factory"],
        environment=harness["environment"],
        manifest=manifest,
        live_executor=fake_executor,
    )
    evidence = {k: v for k, v in harness["observed"].items() if k not in ("preflight_success", "execution_commit")}
    record = runner.run_campaign(
        manifest,
        authorization=harness["authorization"],
        output_root=harness["output_root"],
        route_evidence_provider=lambda: evidence,
        transport_factory=lambda case: harness["factory"].prepare(case),
        case_runner=runner_obj,
        git_state_provider=_clean_git_state,
    )
    return record, fake_executor, harness


def test_campaign_completes_with_fresh_session_per_case(tmp_path, manifest, synthetic_executable):
    transports_seen: list[int] = []

    def builder(kwargs, case, harness):
        transports_seen.append(id(kwargs["transport"]))
        return _live_mapping(manifest, case)

    record, fake_executor, harness = _run_campaign_with(tmp_path, manifest, synthetic_executable, builder)
    assert record["status"] == "COMPLETED"
    assert record["counts"]["completed_case_count"] == 6
    assert record["provider_call_proof"] == {"transports_created": 6, "process_launches": 0, "logical_requests": 0}
    assert len(transports_seen) == 6
    assert len(set(transports_seen)) == 6
    assert fake_executor.calls[0]["policy"].value == "pdb-on-uncertainty"
    assert fake_executor.calls[1]["policy"].value == "static-baseline"
    assert fake_executor.calls[2]["policy"].value == "static-baseline"
    runner.validate_campaign_record(record, manifest)
    package = runner.verify_attempt_package(harness["output_root"], manifest)
    assert package["consistent"] is True


def test_campaign_route_drift_during_case_stops_with_typed_contract(tmp_path, manifest, synthetic_executable):
    def builder(kwargs, case, harness):
        kwargs["transport"].request({"directive_feedback": None}, 30.0)
        inner = harness["factory"].active_transport
        assert inner is not None
        inner.drift_category = "ZEN_ROUTE_OBSERVED"
        return _live_mapping(manifest, case)

    record, _, harness = _run_campaign_with(tmp_path, manifest, synthetic_executable, builder)
    assert record["status"] == "PARTIAL"
    assert record["stop_reason"] == "TRANSPORT_EVIDENCE_LOSS"
    assert record["counts"]["completed_case_count"] == 1
    assert record["counts"]["aborted_case_count"] == 0
    first = record["cases"][0]
    assert first["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert first["infrastructure_evidence"]["stage"] == "provider_transport"
    assert first["infrastructure_evidence"]["reason_code"] == "TRANSPORT_EVIDENCE_LOSS"
    assert first["provider_process_attempts"] == 1
    assert record["counts"]["blocked_case_count"] == 5
    assert record["counts"]["unstarted_case_count"] == 0
    for entry in record["cases"][1:]:
        assert entry["terminal_status"] == "BLOCKED"
        assert entry["terminal_reason_code"] == "TRANSPORT_EVIDENCE_LOSS"
    runner.validate_campaign_record(record, manifest)


def test_campaign_accounting_reconciles_with_transport_counter(tmp_path, manifest, synthetic_executable):
    def builder(kwargs, case, harness):
        kwargs["transport"].request({"directive_feedback": None}, 30.0)
        return _live_mapping(manifest, case)

    record, _, harness = _run_campaign_with(tmp_path, manifest, synthetic_executable, builder)
    assert record["status"] == "COMPLETED"
    assert record["counts"]["provider_process_attempts"] == 6
    assert record["provider_call_proof"] == {"transports_created": 6, "process_launches": 6, "logical_requests": 6}
    for entry in record["cases"]:
        assert entry["provider_process_attempts"] == 1
        assert entry["logical_model_calls"] == 1
    runner.validate_campaign_record(record, manifest)
    package = runner.verify_attempt_package(harness["output_root"], manifest)
    assert package["consistent"] is True


def test_pdb_not_reached_final_case_validates(tmp_path, manifest, synthetic_executable):
    def builder(kwargs, case, harness):
        if case["policy"] == "pdb-on-uncertainty":
            return _live_mapping(manifest, case, **{
                "status": "PDB_NOT_REACHED",
                "evidence": {"pdb_gate_decisions": [{"allowed": False, "reason": "no active hypothesis"}], "directive_rejections": []},
            })
        return _live_mapping(manifest, case)

    record, _, harness = _run_campaign_with(tmp_path, manifest, synthetic_executable, builder)
    assert record["status"] == "COMPLETED"
    pdb_not_reached = [entry for entry in record["cases"] if entry["terminal_status"] == "PDB_NOT_REACHED"]
    assert len(pdb_not_reached) == 3
    for entry in pdb_not_reached:
        assert entry["terminal_reason_code"] == "PDB_NOT_REACHED_GATE_REJECTED"
        assert entry["pdb_counts"]["total_gate_decisions"] == 1
    runner.validate_campaign_record(record, manifest)
