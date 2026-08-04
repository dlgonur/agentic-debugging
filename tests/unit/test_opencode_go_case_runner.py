"""Case-runner binding and campaign reconciliation tests for the OpenCode Go execution adapter."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import quixbugs_opencode_go_adapter as adapter
import quixbugs_live_runner_v2 as runner
import quixbugs_paired_pilot as pilot

from agentic_debugger.demo.catalog import RuntimeProbe
from agentic_debugger.quixbugs.adapter import QuixBugsAdapter, QuixBugsPreflightFacts
from agentic_debugger.runtime.execution import (
    ContainmentGuarantee,
    DependencyPreparation,
    PreparedEnvironment,
    VerifiedExecutionContext,
)

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


class _FakeRunner:
    runner_id = "fake-go-case-runner"
    resource_isolation_ready = True

    def __init__(self, tmp_path: Path) -> None:
        self._root = str(tmp_path.resolve())
        self.boundary_guarantee: dict = {}

    def run(self, argv, cwd, timeout_seconds, env):
        raise AssertionError("the case-runner fake facts context must never run commands")


def _task_facts(tmp_path: Path, manifest_path: str) -> QuixBugsPreflightFacts:
    """Exact QuixBugsPreflightFacts whose dependency preparation is bound to
    the selected task manifest -- the same shape the real
    scripts/quixbugs_live_wire_environment.provide() builds."""
    adapter_obj = QuixBugsAdapter.from_manifest(manifest_path)
    recipe = f"pytest=={adapter_obj.manifest.environment['pinned_packages']['pytest']}"
    dependency = DependencyPreparation(
        pilot_task_id=adapter_obj.manifest.task_id,
        manifest_fingerprint=adapter_obj.manifest.fingerprint,
        authority_revision=adapter_obj.manifest.authority_revision,
        project="quixbugs",
        bug_id=adapter_obj.manifest.algorithm,
        buggy_revision=adapter_obj.manifest.authority_revision,
        recipe_path=recipe,
        recipe_sha256=hashlib.sha256(recipe.encode("utf-8")).hexdigest(),
        installed_fingerprint=adapter_obj.manifest.environment["expected_fingerprint"],
    )
    environment = PreparedEnvironment(
        str(tmp_path / "venv" / "bin" / "python"), "3.10.12", ".", (), {}, dependency,
    )
    containment = ContainmentGuarantee(
        str(tmp_path.resolve()), _FakeRunner.runner_id, resource_limits={"cpu_seconds": "prlimit-enforced:5"},
    )
    fake_runner = _FakeRunner(tmp_path)
    fake_runner.boundary_guarantee = containment.to_mapping()
    context = VerifiedExecutionContext(environment, containment, fake_runner)
    return QuixBugsPreflightFacts(
        platform="linux",
        pinned_source_verified=True,
        license_reviewed=True,
        dependency_install_boundary_ready=True,
        workspace_cleanup_ready=True,
        target_annotation_reviewed=True,
        external_parent=str(tmp_path / "external"),
        execution_context=context,
    )


def _facts_provider(tmp_path: Path, calls: list | None = None):
    def _provide(manifest_path: str) -> QuixBugsPreflightFacts:
        if calls is not None:
            calls.append(str(manifest_path))
        return _task_facts(tmp_path, str(manifest_path))

    return _provide


def _harness(tmp_path, manifest, synthetic_executable, *, claim: bool = True, facts_calls: list | None = None):
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
        facts_provider=_facts_provider(tmp_path, calls=facts_calls),
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
        calls.append({
            "transport": kwargs["transport"],
            "policy": kwargs["policy"],
            "pdb_binding": kwargs.get("pdb_identity_binding"),
            "probe": kwargs.get("runtime_probe"),
        })
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
    # The PDB case receives the task-local probe built from the frozen
    # inventory entry for quixbugs-find-in-sorted-smoke-v1 -- never from
    # corrected source, tests, model output, or runtime guesses.
    probe = calls[0]["probe"]
    assert type(probe) is RuntimeProbe
    assert probe.module_path == "python_programs/find_in_sorted.py"
    assert probe.focus_function == "binsearch"
    assert probe.anchor == "mid ="
    assert probe.call_source == "find_in_sorted([1, 2], 3)"
    assert probe.inspect_expressions == ("arr", "x")
    assert calls[1]["policy"].value == "static-baseline"
    assert calls[1]["pdb_binding"] is None
    assert "probe" not in calls[1] or calls[1]["probe"] is None
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
    assert outcome["infrastructure_evidence"]["prior_lifecycle_completed"] is False
    assert outcome["transport_evidence"] == {
        "completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False,
    }
    assert outcome["terminal_transport_evidence"]["provider_completed_response"] is False
    assert outcome["terminal_transport_evidence"]["process_exit_code"] is None
    runner.enforce_case_budgets(outcome, manifest, case_policy="static-baseline")


def test_controller_stage_infrastructure_failure_records_completed_prior_response(tmp_path, manifest, synthetic_executable):
    """Post-transport (controller-stage) infrastructure failure after
    completed provider responses emits aggregate and terminal evidence bound
    to the completed prior response and validates against the frozen result
    schema -- the production-shaped outcome of live attempt
    quixbugs-paired-pilot-v2-attempt-320550a55d0b48d1a08b7ec7f60dc90f that
    previously aborted the campaign with 'post-transport infrastructure
    requires one completed prior response'."""
    harness = _harness(tmp_path, manifest, synthetic_executable)
    case = manifest["case_order"][1]
    source_hash = next(item["source_sha256"] for item in manifest["inventory"] if item["task_id"] == case["task_id"])
    mapping = _live_mapping(manifest, case, **{
        "status": "CONTROLLER_FAILED",
        "controller": {"completed": False, "final_state": None, "stop_reason": "controller exception", "model_calls": 7, "exception": True},
        "verifier": {"executed": False, "failure": False, "status": None, "outcome": None, "patch_application": None, "localization": {"outcome": "NO_LOCALIZATION"}},
        "measurements": {
            "model_request_count": 8, "model_response_count": 7, "retry_count": 1,
            "provider_error_count": 0, "provider_error_kinds": [],
            "token_usage": {"prompt_tokens": 210, "completion_tokens": 180, "total_tokens": 390,
                            "provider_reported": True, "missing_fields": []},
            "termination_reason": "controller_failure",
            "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
            "tool_call_count": 7, "case_elapsed_duration_ms": 120000,
            "model_phase_elapsed_duration_ms": 100000, "model_transport_duration_ms": 100000,
            "elapsed_scope": "case_observed; model_phase=transport_only",
        },
        "evidence": {"pdb_gate_decisions": [], "directive_rejections": []},
    })
    inner = harness["factory"].prepare(case)
    inner.reported_costs.append(0.002119)
    inner.reported_costs.append(0.003097932)
    outcome = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=8, policy_value=case["policy"], run_id="run-320550a55d0b48d1a08b7ec7f60dc90f",
        source_hash=source_hash,
    )
    assert outcome["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert outcome["terminal_reason_code"] == "INFRASTRUCTURE_FAILURE"
    assert outcome["infrastructure_evidence"]["stage"] == "controller"
    assert outcome["infrastructure_evidence"]["reason_code"] == "CONTROLLER_FAILURE"
    assert outcome["infrastructure_evidence"]["prior_lifecycle_completed"] is True
    assert outcome["logical_model_calls"] == 7
    assert outcome["valid_directives"] == 7
    assert outcome["provider_reported_cost"] == pytest.approx(0.005217)
    assert outcome["provider_reported_cost_observed"] is True
    # The aggregate transport carries exactly one completed-response state and
    # the controller failure is never reclassified as a provider failure.
    assert outcome["transport_evidence"] == {
        "completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False,
    }
    assert sum(bool(outcome["transport_evidence"][key]) for key in ("completed_response", "malformed_response", "provider_error")) == 1
    assert outcome["transport_evidence"]["provider_error"] is False
    # Terminal infrastructure evidence binds the completed prior response.
    terminal = outcome["terminal_transport_evidence"]
    assert terminal["final_attempt_classification"] == "INFRASTRUCTURE_FAILURE"
    assert terminal["provider_completed_response"] is True
    assert terminal["process_exit_code"] == 0
    assert terminal["timed_out"] is False
    assert terminal["provider_error_category"] is None
    runner.validate_case_outcome(outcome)
    runner.enforce_case_budgets(outcome, manifest, case_policy=case["policy"])
    # The frozen result schema accepts the repaired mapping: the exact gate
    # that aborted the live campaign must no longer raise
    # 'post-transport infrastructure requires one completed prior response'.
    record = runner.materialize_case_record(
        manifest, case, harness["authorization"], harness["observed"], outcome,
        attempt_identity=harness["authorization"]["campaign_attempt_identity"],
        execution_commit=runner.ACCEPTED_BASELINE,
    )
    pilot.validate_case_result(record, manifest, harness["authorization"])


def test_provider_error_mapping_remains_unchanged(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)
    case = manifest["case_order"][1]
    mapping = _live_mapping(manifest, case, **{
        "status": "PROVIDER_ERROR",
        "measurements": {
            "model_request_count": 1, "model_response_count": 0, "retry_count": 0,
            "provider_error_count": 1, "provider_error_kinds": ["provider_or_transport_error"],
            "token_usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                            "provider_reported": False, "missing_fields": []},
            "termination_reason": "provider_or_transport_error",
            "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
            "tool_call_count": 0, "case_elapsed_duration_ms": 1000,
            "model_phase_elapsed_duration_ms": 800, "model_transport_duration_ms": 800,
            "elapsed_scope": "case_observed; model_phase=transport_only",
        },
        "evidence": {"pdb_gate_decisions": [], "directive_rejections": []},
    })
    inner = harness["factory"].prepare(case)
    inner.last_provider_error_category = "PROVIDER_ERROR"
    outcome = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=1, policy_value="static-baseline", run_id="run-1",
        source_hash="0" * 64,
    )
    assert outcome["terminal_status"] == "PROVIDER_ERROR"
    assert outcome["transport_evidence"] == {
        "completed_response": False, "malformed_response": False, "provider_error": True, "synthetic": False,
    }
    terminal = outcome["terminal_transport_evidence"]
    assert terminal["final_attempt_classification"] == "PROVIDER_ERROR"
    assert terminal["provider_completed_response"] is False
    assert terminal["timed_out"] is False
    assert terminal["provider_error_category"] == "PROVIDER_ERROR"
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


def test_campaign_accepts_production_shaped_controller_infrastructure_failure(tmp_path, manifest, synthetic_executable):
    """The exact production-shaped outcome from live attempt
    quixbugs-paired-pilot-v2-attempt-320550a55d0b48d1a08b7ec7f60dc90f
    (controller-stage infrastructure failure after multiple completed
    provider responses) completes the frozen campaign instead of aborting
    with 'post-transport infrastructure requires one completed prior
    response'."""
    def builder(kwargs, case, harness):
        if case["order_index"] == 1:
            for _ in range(8):
                kwargs["transport"].request({"directive_feedback": None}, 30.0)
            inner = harness["factory"].active_transport
            assert inner is not None
            # The synthetic transport reports a deterministic default cost on
            # every response.  This regression models the exact historical
            # attempt evidence instead, so replace those fixture costs with
            # the two provider-reported values from that attempt.
            inner.reported_costs.clear()
            inner.reported_costs.append(0.002119)
            inner.reported_costs.append(0.003097932)
            return _live_mapping(manifest, case, **{
                "status": "CONTROLLER_FAILED",
                "controller": {"completed": False, "final_state": None, "stop_reason": "controller exception", "model_calls": 7, "exception": True},
                "verifier": {"executed": False, "failure": False, "status": None, "outcome": None, "patch_application": None, "localization": {"outcome": "NO_LOCALIZATION"}},
                "measurements": {
                    "model_request_count": 8, "model_response_count": 7, "retry_count": 1,
                    "provider_error_count": 0, "provider_error_kinds": [],
                    "token_usage": {"prompt_tokens": 210, "completion_tokens": 180, "total_tokens": 390,
                                    "provider_reported": True, "missing_fields": []},
                    "termination_reason": "controller_failure",
                    "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
                    "tool_call_count": 7, "case_elapsed_duration_ms": 120000,
                    "model_phase_elapsed_duration_ms": 100000, "model_transport_duration_ms": 100000,
                    "elapsed_scope": "case_observed; model_phase=transport_only",
                },
                "evidence": {"pdb_gate_decisions": [], "directive_rejections": []},
            })
        return _live_mapping(manifest, case)

    record, _, harness = _run_campaign_with(tmp_path, manifest, synthetic_executable, builder)
    assert record["status"] == "COMPLETED"
    assert record["counts"]["completed_case_count"] == 6
    assert record["counts"]["aborted_case_count"] == 0
    first = record["cases"][0]
    assert first["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert first["terminal_reason_code"] == "INFRASTRUCTURE_FAILURE"
    assert first["infrastructure_evidence"]["stage"] == "controller"
    assert first["infrastructure_evidence"]["reason_code"] == "CONTROLLER_FAILURE"
    assert first["infrastructure_evidence"]["prior_lifecycle_completed"] is True
    assert first["transport_evidence"] == {
        "completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False,
    }
    assert first["terminal_transport_evidence"]["final_attempt_classification"] == "INFRASTRUCTURE_FAILURE"
    assert first["terminal_transport_evidence"]["provider_completed_response"] is True
    assert first["terminal_transport_evidence"]["process_exit_code"] == 0
    assert first["provider_reported_cost"] == pytest.approx(0.005217)
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


# ---- task-local PDB probe binding ------------------------------------------


def _inventory_probe_for(manifest, task_id) -> dict:
    return copy.deepcopy(
        next(item["runtime_probe"] for item in manifest["inventory"] if item["task_id"] == task_id)
    )


def test_three_selected_pdb_cases_receive_their_own_reviewed_probes(tmp_path, manifest, synthetic_executable):
    """Each of the three selected PDB cases receives the exact task-local
    probe built from its own frozen inventory entry -- never the gcd probe
    and never anything derived from corrected source, tests, model output,
    or runtime guesses."""
    harness = _harness(tmp_path, manifest, synthetic_executable)
    captured: dict[str, dict] = {}
    attempt_identity = harness["authorization"]["campaign_attempt_identity"]

    def fake_executor(**kwargs):
        task_id = QuixBugsAdapter.from_manifest(kwargs["manifest_path"]).manifest.task_id
        captured[task_id] = {"probe": kwargs.get("runtime_probe"), "policy": kwargs["policy"].value}
        case = next(c for c in manifest["case_order"] if c["task_id"] == task_id and c["policy"] == kwargs["policy"].value)
        return FakeLiveResult(_live_mapping(manifest, case))

    runner_obj = adapter.OpenCodeGoCaseRunner(
        binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
        environment=harness["environment"], manifest=manifest, live_executor=fake_executor,
    )
    pdb_cases = [case for case in manifest["case_order"] if case["policy"] == "pdb-on-uncertainty"]
    assert [case["task_id"] for case in pdb_cases] == [
        "quixbugs-find-in-sorted-smoke-v1",
        "quixbugs-is-valid-parenthesization-smoke-v1",
        "quixbugs-hanoi-smoke-v1",
    ]
    for case in pdb_cases:
        harness["factory"].prepare(case)
        runner_obj(case, attempt_identity=attempt_identity,
                   run_id=runner.deterministic_run_id(attempt_identity, case),
                   session_id=f"s-{case['task_id']}", transport=object(),
                   route_observation=harness["observed"], budgets=manifest["budgets"])

    for task_id in ("quixbugs-find-in-sorted-smoke-v1", "quixbugs-is-valid-parenthesization-smoke-v1", "quixbugs-hanoi-smoke-v1"):
        probe = captured[task_id]["probe"]
        assert type(probe) is RuntimeProbe, task_id
        frozen = _inventory_probe_for(manifest, task_id)
        assert probe.module_path == frozen["module_path"]
        assert probe.focus_function == frozen["focus_function"]
        assert probe.call_source == frozen["call_expression"]
        assert probe.anchor == frozen["breakpoint_anchor"]
        assert probe.inspect_expressions == tuple(frozen["inspect_names"])
        assert probe.module_path == next(
            item["implementation_path"] for item in manifest["inventory"] if item["task_id"] == task_id
        )
    assert captured["quixbugs-find-in-sorted-smoke-v1"]["probe"].module_path == "python_programs/find_in_sorted.py"
    assert captured["quixbugs-is-valid-parenthesization-smoke-v1"]["probe"].module_path == "python_programs/is_valid_parenthesization.py"
    assert captured["quixbugs-hanoi-smoke-v1"]["probe"].module_path == "python_programs/hanoi.py"


def test_static_cases_receive_no_probe(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)
    captured: list[dict] = []
    attempt_identity = harness["authorization"]["campaign_attempt_identity"]

    def fake_executor(**kwargs):
        captured.append(kwargs)
        case = next(c for c in manifest["case_order"] if c["task_id"] == QuixBugsAdapter.from_manifest(kwargs["manifest_path"]).manifest.task_id and c["policy"] == kwargs["policy"].value)
        return FakeLiveResult(_live_mapping(manifest, case))

    runner_obj = adapter.OpenCodeGoCaseRunner(
        binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
        environment=harness["environment"], manifest=manifest, live_executor=fake_executor,
    )
    for case in manifest["case_order"]:
        harness["factory"].prepare(case)
        runner_obj(case, attempt_identity=attempt_identity,
                   run_id=runner.deterministic_run_id(attempt_identity, case),
                   session_id=f"s-{case['task_id']}", transport=object(),
                   route_observation=harness["observed"], budgets=manifest["budgets"])
    static_calls = [kwargs for kwargs in captured if kwargs["policy"].value == "static-baseline"]
    assert len(static_calls) == 3
    for kwargs in static_calls:
        assert kwargs.get("runtime_probe") is None
        assert kwargs.get("pdb_identity_binding") is None


def test_missing_probe_metadata_fails_before_provider_execution(tmp_path, manifest, synthetic_executable):
    mutated = copy.deepcopy(manifest)
    entry = next(item for item in mutated["inventory"] if item["task_id"] == "quixbugs-find-in-sorted-smoke-v1")
    del entry["runtime_probe"]
    harness = _harness(tmp_path, manifest, synthetic_executable)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="no frozen runtime_probe metadata"):
        adapter.OpenCodeGoCaseRunner(
            binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
            environment=harness["environment"], manifest=mutated,
        )


def test_mismatched_probe_metadata_fails_before_provider_execution(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)

    mutated = copy.deepcopy(manifest)
    entry = next(item for item in mutated["inventory"] if item["task_id"] == "quixbugs-find-in-sorted-smoke-v1")
    entry["runtime_probe"]["module_path"] = "python_programs/hanoi.py"
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="does not match its implementation_path"):
        adapter.OpenCodeGoCaseRunner(
            binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
            environment=harness["environment"], manifest=mutated,
        )

    mutated = copy.deepcopy(manifest)
    entry = next(item for item in mutated["inventory"] if item["task_id"] == "quixbugs-find-in-sorted-smoke-v1")
    entry["runtime_probe"]["focus_function"] = "not-a-reviewed-symbol"
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="not a reviewed target symbol"):
        adapter.OpenCodeGoCaseRunner(
            binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
            environment=harness["environment"], manifest=mutated,
        )

    mutated = copy.deepcopy(manifest)
    entry = next(item for item in mutated["inventory"] if item["task_id"] == "quixbugs-find-in-sorted-smoke-v1")
    entry["runtime_probe"]["inspect_names"] = ["arr", "arr"]
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="inspect_names.*unique"):
        adapter.OpenCodeGoCaseRunner(
            binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
            environment=harness["environment"], manifest=mutated,
        )


def test_duplicate_inventory_entries_rejected_before_provider_execution(tmp_path, manifest, synthetic_executable):
    mutated = copy.deepcopy(manifest)
    entry = next(item for item in mutated["inventory"] if item["task_id"] == "quixbugs-find-in-sorted-smoke-v1")
    mutated["inventory"].append(copy.deepcopy(entry))
    harness = _harness(tmp_path, manifest, synthetic_executable)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="duplicate inventory entries"):
        adapter.OpenCodeGoCaseRunner(
            binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
            environment=harness["environment"], manifest=mutated,
        )


def test_probe_metadata_never_derived_from_corrected_source_or_tests(tmp_path, manifest, synthetic_executable):
    """Probe metadata is taken exclusively from the frozen inventory entry;
    the runtime probe never targets corrected, test, or support material."""
    mutated = copy.deepcopy(manifest)
    entry = next(item for item in mutated["inventory"] if item["task_id"] == "quixbugs-find-in-sorted-smoke-v1")
    entry["runtime_probe"]["module_path"] = "correct_python_programs/find_in_sorted.py"
    harness = _harness(tmp_path, manifest, synthetic_executable)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="does not match its implementation_path"):
        adapter.OpenCodeGoCaseRunner(
            binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
            environment=harness["environment"], manifest=mutated,
        )

    mutated = copy.deepcopy(manifest)
    entry = next(item for item in mutated["inventory"] if item["task_id"] == "quixbugs-hanoi-smoke-v1")
    entry["runtime_probe"]["module_path"] = "python_testcases/test_hanoi.py"
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="does not match its implementation_path"):
        adapter.OpenCodeGoCaseRunner(
            binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
            environment=harness["environment"], manifest=mutated,
        )


# ---- task-bound facts provider ---------------------------------------------


def test_facts_requested_separately_with_exact_manifest_path(tmp_path, manifest, synthetic_executable):
    facts_calls: list[str] = []
    harness = _harness(tmp_path, manifest, synthetic_executable, facts_calls=facts_calls)
    attempt_identity = harness["authorization"]["campaign_attempt_identity"]

    def fake_executor(**kwargs):
        case = next(c for c in manifest["case_order"] if c["task_id"] == QuixBugsAdapter.from_manifest(kwargs["manifest_path"]).manifest.task_id and c["policy"] == kwargs["policy"].value)
        return FakeLiveResult(_live_mapping(manifest, case))

    runner_obj = adapter.OpenCodeGoCaseRunner(
        binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
        environment=harness["environment"], manifest=manifest, live_executor=fake_executor,
    )
    for case in manifest["case_order"]:
        harness["factory"].prepare(case)
        runner_obj(case, attempt_identity=attempt_identity,
                   run_id=runner.deterministic_run_id(attempt_identity, case),
                   session_id=f"s-{case['task_id']}", transport=object(),
                   route_observation=harness["observed"], budgets=manifest["budgets"])
    expected_paths = {
        str((REPO_ROOT / "research" / "quixbugs" / "FIND_IN_SORTED_SMOKE_MANIFEST_V1.json").resolve()),
        str((REPO_ROOT / "research" / "quixbugs" / "HANOI_SMOKE_MANIFEST_V1.json").resolve()),
        str((REPO_ROOT / "research" / "quixbugs" / "IS_VALID_PARENTHESIZATION_SMOKE_MANIFEST_V1.json").resolve()),
    }
    assert len(facts_calls) == 6
    assert set(facts_calls) == expected_paths
    assert facts_calls.count(str((REPO_ROOT / "research" / "quixbugs" / "FIND_IN_SORTED_SMOKE_MANIFEST_V1.json").resolve())) == 2
    assert facts_calls.count(str((REPO_ROOT / "research" / "quixbugs" / "HANOI_SMOKE_MANIFEST_V1.json").resolve())) == 2
    assert facts_calls.count(str((REPO_ROOT / "research" / "quixbugs" / "IS_VALID_PARENTHESIZATION_SMOKE_MANIFEST_V1.json").resolve())) == 2


def test_wrong_task_dependency_facts_rejected_before_executor(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)
    attempt_identity = harness["authorization"]["campaign_attempt_identity"]
    executor_called = {"value": False}
    hanoi_manifest = str((REPO_ROOT / "research" / "quixbugs" / "HANOI_SMOKE_MANIFEST_V1.json").resolve())

    def wrong_task_provider(manifest_path: str):
        # Always returns facts bound to the hanoi manifest, even when the
        # exact find-in-sorted manifest is requested.
        return _task_facts(tmp_path, hanoi_manifest)

    def fake_executor(**kwargs):
        executor_called["value"] = True
        case = next(c for c in manifest["case_order"] if c["policy"] == kwargs["policy"].value)
        return FakeLiveResult(_live_mapping(manifest, case))

    environment = adapter.QuixBugsCaseEnvironment(
        repository_root=str(tmp_path / "repo"),
        sources_parent=str(tmp_path / "sources"),
        facts_provider=wrong_task_provider,
    )
    runner_obj = adapter.OpenCodeGoCaseRunner(
        binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
        environment=environment, manifest=manifest, live_executor=fake_executor,
    )
    case = next(c for c in manifest["case_order"] if c["task_id"] == "quixbugs-find-in-sorted-smoke-v1" and c["policy"] == "pdb-on-uncertainty")
    harness["factory"].prepare(case)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="does not match the selected task manifest"):
        runner_obj(case, attempt_identity=attempt_identity,
                   run_id=runner.deterministic_run_id(attempt_identity, case),
                   session_id="s1", transport=object(),
                   route_observation=harness["observed"], budgets=manifest["budgets"])
    assert executor_called["value"] is False


def test_zero_argument_generic_facts_provider_rejected(tmp_path, manifest, synthetic_executable):
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="not task-bound"):
        adapter._reject_zero_argument_facts_provider(lambda: None)

    harness = _harness(tmp_path, manifest, synthetic_executable)
    attempt_identity = harness["authorization"]["campaign_attempt_identity"]

    def fake_executor(**kwargs):
        raise AssertionError("executor must not be called for a generic facts provider")

    environment = adapter.QuixBugsCaseEnvironment(
        repository_root=str(tmp_path / "repo"),
        sources_parent=str(tmp_path / "sources"),
        facts_provider=lambda: SimpleNamespace(execution_context=None),
    )
    runner_obj = adapter.OpenCodeGoCaseRunner(
        binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
        environment=environment, manifest=manifest, live_executor=fake_executor,
    )
    case = manifest["case_order"][1]
    harness["factory"].prepare(case)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="manifest-path argument"):
        runner_obj(case, attempt_identity=attempt_identity,
                   run_id=runner.deterministic_run_id(attempt_identity, case),
                   session_id="s1", transport=object(),
                   route_observation=harness["observed"], budgets=manifest["budgets"])


def test_malformed_facts_result_rejected_before_executor(tmp_path, manifest, synthetic_executable):
    harness = _harness(tmp_path, manifest, synthetic_executable)
    attempt_identity = harness["authorization"]["campaign_attempt_identity"]

    def malformed_provider(manifest_path: str):
        return {"execution_context": None}

    def fake_executor(**kwargs):
        raise AssertionError("executor must not be called for malformed facts")

    environment = adapter.QuixBugsCaseEnvironment(
        repository_root=str(tmp_path / "repo"),
        sources_parent=str(tmp_path / "sources"),
        facts_provider=malformed_provider,
    )
    runner_obj = adapter.OpenCodeGoCaseRunner(
        binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
        environment=environment, manifest=manifest, live_executor=fake_executor,
    )
    case = manifest["case_order"][1]
    harness["factory"].prepare(case)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="exactly QuixBugsPreflightFacts"):
        runner_obj(case, attempt_identity=attempt_identity,
                   run_id=runner.deterministic_run_id(attempt_identity, case),
                   session_id="s1", transport=object(),
                   route_observation=harness["observed"], budgets=manifest["budgets"])


def test_six_case_runner_enters_all_bindings_with_synthetic_transport(tmp_path, manifest, synthetic_executable):
    """The full six-case runner enters every frozen case binding with
    synthetic transport and no real provider: each PDB case receives its own
    reviewed probe, each static case receives none, and facts are requested
    per case with the exact manifest path."""
    facts_calls: list[str] = []
    harness = _harness(tmp_path, manifest, synthetic_executable, claim=False, facts_calls=facts_calls)
    executor_calls: list[dict] = []

    def fake_executor(**kwargs):
        executor_calls.append(kwargs)
        case = next(c for c in manifest["case_order"] if c["task_id"] == QuixBugsAdapter.from_manifest(kwargs["manifest_path"]).manifest.task_id and c["policy"] == kwargs["policy"].value)
        return FakeLiveResult(_live_mapping(manifest, case))

    runner_obj = adapter.OpenCodeGoCaseRunner(
        binding=harness["binding"], configuration=harness["configuration"], factory=harness["factory"],
        environment=harness["environment"], manifest=manifest, live_executor=fake_executor,
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
    assert record["status"] == "COMPLETED"
    assert len(executor_calls) == 6
    pdb_calls = [kwargs for kwargs in executor_calls if kwargs["policy"].value == "pdb-on-uncertainty"]
    static_calls = [kwargs for kwargs in executor_calls if kwargs["policy"].value == "static-baseline"]
    assert len(pdb_calls) == 3
    assert len(static_calls) == 3
    for kwargs in pdb_calls:
        probe = kwargs["runtime_probe"]
        task_id = QuixBugsAdapter.from_manifest(kwargs["manifest_path"]).manifest.task_id
        frozen = _inventory_probe_for(manifest, task_id)
        assert type(probe) is RuntimeProbe
        assert probe.module_path == frozen["module_path"]
        assert probe.focus_function == frozen["focus_function"]
    for kwargs in static_calls:
        assert kwargs.get("runtime_probe") is None
        assert kwargs.get("pdb_identity_binding") is None
    assert len(facts_calls) == 6
    runner.validate_campaign_record(record, manifest)
    package = runner.verify_attempt_package(harness["output_root"], manifest)
    assert package["consistent"] is True
