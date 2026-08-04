"""Focused red/green regressions for canonical-public-request budget exhaustion.

Locks the confirmed production failure of attempt
``quixbugs-paired-pilot-v2-attempt-9f5958631df47a8a3a6b6f19c4a42f00a3a2880c84b8dc0421d9b7ac87e4ae74``:
12 completed provider responses, then a 20,475-byte next canonical public
request, 36,374 cumulative public evidence bytes, three retried
provider/transport-shaped failures, and a campaign abort at case 1 with zero
materialized cases.

The repair detects the oversized canonical public request in-process, before
any wrapper/provider process launch and before any process-launch counter is
incremented, raises a typed non-retryable signal
(:class:`agentic_debugger.evaluation.live.ModelRequestBudgetExceeded`),
terminalizes the case as ``PDB_NOT_REACHED / PDB_NOT_REACHED_NO_GATE`` with
all completed accounting preserved and ``public_evidence_bytes`` capped at
the frozen 20,000-byte limit, and continues the campaign.
"""
from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import quixbugs_live_runner_v2 as runner
import quixbugs_opencode_go_adapter as adapter
import quixbugs_paired_pilot as pilot

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    DeterministicController,
    ControllerStopReason,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry, ToolResult, ToolSpec
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import (
    LiveCaseStatus,
    LiveModelAdapter,
    LiveModelConfig,
    LiveRunLimits,
    LiveTransportError,
    ModelRequestBudgetExceeded,
    _finalize_live_case,
)
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.events.schema import ObservationStatus

from test_quixbugs_live_runner_v2 import (
    RecordingTransportFactory,
    ScriptedCaseRunner,
    _clean_git_state,
    _completed_entries,
    _completed_outcome,
    _route_evidence,
    _valid_authorization,
)
from test_opencode_go_case_runner import FakeLiveResult, _harness, _live_mapping
from test_opencode_go_transport_factory import _scenario_payload, _scenario_transport


# ---- fixtures and helpers ------------------------------------------------------


@pytest.fixture
def manifest():
    return pilot.load_manifest(pilot.MANIFEST_PATH_V2)


@pytest.fixture
def auth(manifest, tmp_path):
    return _valid_authorization(manifest, tmp_path / "attempt-out")


@pytest.fixture
def git_state_provider():
    return lambda commit: _clean_git_state(commit)


@pytest.fixture
def synthetic_executable() -> Path:
    return REPO_ROOT / "scripts" / "opencode_go_synthetic_executable.py"


def _canonical_payload(byte_count: int) -> dict:
    """A payload whose canonical public request serialization is exactly
    ``byte_count`` bytes (``{"filler":"<x...x>"}``)."""
    filler = byte_count - len('{"filler":"')
    return {"filler": "x" * (filler - 2)}


def _scenario_transport(harness, scenario: str, **overrides):
    """Build one direct OpenCodeGoTransport from the case-runner harness."""
    configuration = dict(harness["configuration"])
    return adapter.OpenCodeGoTransport(
        factory=harness["factory"],
        case_id=f"test-{scenario}",
        command=list(configuration["command"]),
        working_directory=Path(configuration["working_directory"]),
        environment_allowlist=list(configuration["environment_allowlist"]),
        max_stdout_bytes=int(overrides.get("max_stdout_bytes", configuration["max_stdout_bytes"])),
        max_stderr_bytes=int(overrides.get("max_stderr_bytes", configuration["max_stderr_bytes"])),
        max_diagnostic_bytes=int(overrides.get("max_diagnostic_bytes", configuration["max_diagnostic_bytes"])),
        per_call_timeout_seconds=float(overrides.get("per_call_timeout_seconds", configuration["per_call_timeout_seconds"])),
        environment_override=harness["factory"].environment_override,
    )


def _assert_canonical_bytes(payload: dict, expected: int) -> None:
    observed = len(runner.transport.canonical_public_request(payload).encode("utf-8"))
    assert observed == expected, f"canonical payload is {observed} bytes, expected {expected}"


def _live_registry() -> ToolRegistry:
    return ToolRegistry((
        ToolSpec(
            ActionName.RUN_REPRODUCTION,
            lambda arguments: dict(arguments),
            lambda _action, _arguments: ToolResult(ObservationStatus.OK, {}, "ok"),
            argument_contract={
                "required": ["phase"],
                "properties": {"phase": {"type": "string", "min_length": 1}},
                "additional_properties": False,
            },
        ),
    ))


def _curated_task() -> DebugTask:
    return DebugTask.from_mapping(
        json.loads((REPO_ROOT / "agentic_debugger/datasets/curated/curated-none-handling-001/task.json").read_text())
    )


def _events_jsonl_of_bytes(total: int) -> str:
    """The accepted production-shaped events log padded to exactly ``total``
    bytes (baseline reproduction observed, hypothesis created, zero PDB)."""
    from test_opencode_go_case_runner import _events_jsonl

    base = _events_jsonl()
    base_bytes = len(base.encode("utf-8"))
    assert total > base_bytes
    padding = json.dumps(
        {"event_type": "padding", "name": "padding", "state": "Reproduce",
         "payload": {"filler": "x" * (total - base_bytes - 1 - len(json.dumps(
             {"event_type": "padding", "name": "padding", "state": "Reproduce",
              "payload": {"filler": "x"}}, ensure_ascii=False)) + 1)}},
        ensure_ascii=False,
    )
    value = base + padding + "\n"
    assert len(value.encode("utf-8")) == total
    return value


def _run_campaign_custom(manifest, auth, tmp_path, *, case_runner, runner_entries=None, git_state_provider=None):
    output = tmp_path / "attempt-out"
    factory = RecordingTransportFactory()
    entries = runner_entries if runner_entries is not None else [
        {"provider_process_attempts": 1, "outcome": _completed_outcome(manifest, case, _route_evidence(manifest))}
        for case in manifest["case_order"]
    ]
    provider = git_state_provider if git_state_provider is not None else (lambda commit: _clean_git_state(commit))
    record = runner.run_campaign(
        manifest,
        authorization=auth,
        output_root=output,
        route_evidence_provider=(lambda: _route_evidence(manifest)),
        transport_factory=factory,
        case_runner=case_runner,
        git_state_provider=provider,
    )
    return record, factory, case_runner, output


def _production_exhausted_outcome(manifest, case, route, **overrides):
    """The exact production-shaped raw outcome for attempt 9f595...: twelve
    completed responses, twelve accepted directives, one hypothesis, the
    controller stopped in Validate, zero PDB activity, zero patch submission,
    and 36,374 cumulative public evidence bytes."""
    outcome = _completed_outcome(manifest, case, route, **{
        "terminal_status": "INFRASTRUCTURE_ERROR",
        "terminal_reason_code": "INFRASTRUCTURE_FAILURE",
        "termination_reason": "controller stopped before DONE",
        "logical_model_calls": 12,
        "provider_process_attempts": 12,
        "valid_directives": 12,
        "baseline_reproduction": True,
        "controller_states_visited": ["REPRODUCE", "UNDERSTAND", "PATCH", "VALIDATE"],
        "hypotheses_created": 1,
        "pdb_gate_decisions": [],
        "pdb_counts": dict(runner.ZERO_PDB_COUNTS),
        "verifier_runs": 0,
        "patch_submissions": 0,
        "independent_verifier_result": {"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "COMPLETED_RESPONSE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True,
            "evidence_reference": f"opencode-go:{case['case_id']}:12",
        },
        "infrastructure_evidence": {
            "stage": "controller", "reason_code": "CONTROLLER_FAILURE", "confirmed_failure": True,
            "classification": "CONTROLLER", "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "provider_attempt_index": None, "prior_lifecycle_completed": True,
            "source_mutation_observed": False, "expected_source_hash": None,
            "evidence_reference": "opencode-go-controller-stop",
        },
        "prompt_tokens": 87216,
        "completion_tokens": 216,
        "reasoning_tokens": 4200,
        "provider_reported_cost": 0.013511,
        "wall_clock_duration_seconds": 190.0,
        "public_evidence_bytes": 36374,
    })
    outcome.update(overrides)
    return outcome


# ---- request boundary ----------------------------------------------------------


def test_exact_20000_byte_canonical_request_remains_accepted():
    """An exact canonical public request of 20,000 bytes is accepted by the
    shared serializer and by the outer transport gate (the inner transport is
    reached, and the request counters move exactly once)."""
    payload = _canonical_payload(20_000)
    _assert_canonical_bytes(payload, 20_000)
    message = runner.transport.build_user_message(payload)
    assert runner.transport.PUBLIC_REQUEST_START in message

    calls = []

    class StubTransport:
        def request(self, request_payload, timeout_seconds):
            calls.append(request_payload)
            return {"directive": {"kind": "stop", "reason": "synthetic"}}

    counter = runner.ProviderCallCounter()
    proxy = runner._CountingTransportProxy(StubTransport(), counter)
    response = proxy.request(payload, 1.0)
    assert response["directive"]["kind"] == "stop"
    assert counter.proof() == {"transports_created": 0, "process_launches": 1, "logical_requests": 1}
    assert len(calls) == 1


def test_20001_byte_request_is_typed_signal_no_launch_no_counters_no_retry(tmp_path, manifest, synthetic_executable, monkeypatch):
    """A canonical public request of 20,001 bytes produces the typed
    public-evidence exhaustion signal, launches no wrapper/provider process,
    increments no process-attempt counter, and is never retried."""
    payload = _canonical_payload(20_001)
    _assert_canonical_bytes(payload, 20_001)

    calls = []

    class StubTransport:
        def request(self, request_payload, timeout_seconds):
            calls.append(request_payload)
            return {}

    counter = runner.ProviderCallCounter()
    proxy = runner._CountingTransportProxy(StubTransport(), counter)
    with pytest.raises(ModelRequestBudgetExceeded) as info:
        proxy.request(payload, 1.0)
    assert info.value.request_byte_count == 20_001
    assert info.value.limit == 20_000
    assert counter.proof() == {"transports_created": 0, "process_launches": 0, "logical_requests": 0}
    assert calls == []

    harness = _harness(tmp_path, manifest, synthetic_executable)
    inner = _scenario_transport(harness, "budget-gate")
    launched = []

    def _no_launch(*args, **kwargs):
        launched.append(1)
        raise AssertionError("provider process must not be launched for an oversized request")

    monkeypatch.setattr(adapter.subprocess, "Popen", _no_launch)
    with pytest.raises(ModelRequestBudgetExceeded) as info:
        inner.request(payload, 25.0)
    assert info.value.request_byte_count == 20_001
    assert info.value.limit == 20_000
    assert inner.process_attempts == 0
    assert harness["factory"].spawned_processes == 0
    assert launched == []


# ---- live adapter non-retry signal ---------------------------------------------


def test_live_adapter_budget_signal_is_non_retryable_and_unaccounted():
    """The typed budget signal raised by the transport is not retried, is not
    counted as a provider error, is not fed back as a malformed directive,
    and the rejected logical call stays unaccounted."""
    task = _curated_task()
    calls = []

    class BudgetTransport:
        def request(self, payload, timeout_seconds):
            calls.append(payload)
            raise ModelRequestBudgetExceeded(20_475, 20_000)

    adapter_obj = LiveModelAdapter(
        task=task, policy=DemoPolicy.STATIC_BASELINE, config=LiveModelConfig("test-model", ("test-command",)),
        transport=BudgetTransport(), limits=LiveRunLimits(max_model_requests=3, max_retries=2),
        registry=_live_registry(),
    )
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    with pytest.raises(ModelRequestBudgetExceeded) as info:
        adapter_obj.next_directive(ControllerSnapshot(
            "budget-run", task.task_id, ControllerState.REPRODUCE, 0, limits,
            ControllerBudgetState(), HypothesisLedger(),
        ))
    assert info.value.request_byte_count == 20_475
    assert adapter_obj.metrics.model_requests == 0
    assert adapter_obj.metrics.model_responses == 0
    assert adapter_obj.metrics.retries == 0
    assert adapter_obj.metrics.termination_reason == "public_evidence_budget_exceeded"
    assert adapter_obj.directive_rejections == []
    assert len(calls) == 1


def test_live_controller_budget_stop_materializes_pdb_not_reached():
    """The production-shaped live path (one completed response, then the next
    request over budget) stops the controller without a provider failure and
    finalizes as ``PDB_NOT_REACHED`` with the completed accounting and the
    typed termination reason preserved."""
    task = _curated_task()

    class BudgetAfterOneTransport:
        def __init__(self):
            self.calls = 0

        def request(self, payload, timeout_seconds):
            self.calls += 1
            if self.calls == 1:
                return {
                    "directive": {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            raise ModelRequestBudgetExceeded(20_475, 20_000)

    config = LiveModelConfig("test-model", ("test-command",))
    live_adapter = LiveModelAdapter(
        task=task, policy=DemoPolicy.PDB_ON_UNCERTAINTY, config=config,
        transport=BudgetAfterOneTransport(), limits=LiveRunLimits(max_model_requests=3, max_retries=2),
        registry=_live_registry(),
    )
    controller = DeterministicController(_live_registry(), live_adapter, ControllerRunConfig(max_model_calls=3))
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    result = controller.run(ControllerSnapshot(
        "budget-chain", task.task_id, ControllerState.REPRODUCE, 0, limits,
        ControllerBudgetState(), HypothesisLedger(),
    ))
    assert result.stop_reason is ControllerStopReason.MODEL_ERROR
    assert live_adapter.metrics.model_requests == 1
    assert live_adapter.metrics.model_responses == 1
    assert live_adapter.metrics.retries == 0
    assert live_adapter.metrics.termination_reason == "public_evidence_budget_exceeded"

    finalized = _finalize_live_case(
        task_id=task.task_id, policy=DemoPolicy.PDB_ON_UNCERTAINTY, repetition=1,
        case_id="budget-case", run_id="budget-run", config=config, task=task,
        context=None, workspace=None, result=result, metrics=live_adapter.metrics,
        live_adapter=live_adapter, started=time.monotonic() - 1.0,
        interrupted=False, controller_failed=False, diagnostics=[],
        verify=lambda: pytest.fail("verifier must not run for a budget-exhausted case"),
        extra_cleanup=lambda: (True, None), extra_cleanup_owned=False,
        evidence={"pdb_gate_decisions": [], "directive_rejections": []},
    )
    assert finalized.status is LiveCaseStatus.PDB_NOT_REACHED
    assert finalized.reporting["completed"] is True
    mapping = finalized.to_mapping()
    assert mapping["measurements"]["termination_reason"] == "public_evidence_budget_exceeded"
    assert mapping["measurements"]["model_request_count"] == 1
    assert mapping["measurements"]["model_response_count"] == 1
    assert mapping["measurements"]["retry_count"] == 0


# ---- attempt-shaped outcome mapping and terminalization ------------------------


def test_production_shape_budget_exhaustion_maps_and_terminalizes(tmp_path, manifest, synthetic_executable):
    """The adapter outcome mapping for the production shape (twelve completed
    responses, 36,374 cumulative public evidence bytes) stays provider-error
    free and the frozen case-level terminalization rewrites it to
    ``PDB_NOT_REACHED / PDB_NOT_REACHED_NO_GATE`` with the observed count in
    the termination detail."""
    harness = _harness(tmp_path, manifest, synthetic_executable)
    case = manifest["case_order"][0]
    events_jsonl = _events_jsonl_of_bytes(36_374)
    mapping = _live_mapping(manifest, case, **{
        "status": "PDB_NOT_REACHED",
        "measurements": {
            "model_request_count": 12, "model_response_count": 12, "retry_count": 0,
            "provider_error_count": 0, "provider_error_kinds": [],
            "token_usage": {"prompt_tokens": 87216, "completion_tokens": 216, "total_tokens": 87432,
                            "provider_reported": True, "missing_fields": []},
            "termination_reason": "public_evidence_budget_exceeded",
            "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
            "tool_call_count": 12, "case_elapsed_duration_ms": 190000,
            "model_phase_elapsed_duration_ms": 185000, "model_transport_duration_ms": 185000,
            "elapsed_scope": "case_observed; model_phase=transport_only",
        },
        "events_jsonl": events_jsonl,
        "evidence": {"pdb_gate_decisions": [], "directive_rejections": []},
    })
    inner = harness["factory"].prepare(case)
    inner.process_attempts = 12
    inner.last_process_exit_code = 0
    inner.reported_costs.extend([0.001125936] * 12)
    outcome = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=inner.process_attempts, policy_value=case["policy"], run_id="run-9f5958",
        source_hash="0" * 64,
    )

    assert outcome["terminal_status"] == "PDB_NOT_REACHED"
    assert outcome["terminal_reason_code"] == "PDB_NOT_REACHED_NO_GATE"
    assert outcome["provider_process_attempts"] == 12
    assert outcome["logical_model_calls"] == 12
    assert outcome["retries"] == 0
    assert outcome["valid_directives"] == 12
    assert outcome["malformed_directive_rejections"] == 0
    assert outcome["transport_evidence"] == {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False}
    assert outcome["terminal_transport_evidence"]["final_attempt_classification"] == "COMPLETED_RESPONSE"
    assert outcome["terminal_transport_evidence"]["provider_completed_response"] is True
    assert outcome["terminal_transport_evidence"]["process_exit_code"] == 0
    assert outcome["terminal_transport_evidence"]["timed_out"] is False
    assert outcome["terminal_transport_evidence"]["evidence_reference"] == f"opencode-go:{inner.case_id}:12"
    assert outcome["provider_reported_cost"] == pytest.approx(round(12 * 0.001125936, 6))
    assert outcome["provider_cost_report_count"] == 12
    assert outcome["provider_reported_cost_observed"] is True
    assert outcome["public_evidence_bytes"] == 36_374
    assert outcome["repair_outcome"] == "NO_CANDIDATE"

    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest, case_policy=case["policy"])
    assert info.value.observed == 36_374
    assert info.value.limit == 20_000

    rewritten = runner._budget_exhausted_outcome(case, outcome, info.value, run_id="run-9f5958")
    assert rewritten is not None
    assert rewritten["terminal_status"] == "PDB_NOT_REACHED"
    assert rewritten["terminal_reason_code"] == "PDB_NOT_REACHED_NO_GATE"
    assert rewritten["public_evidence_bytes"] == 20_000
    assert "36374" in rewritten["termination_reason"] and "20000" in rewritten["termination_reason"]
    assert "public-evidence budget exhausted" in rewritten["termination_reason"]
    assert rewritten["logical_model_calls"] == 12
    assert rewritten["provider_process_attempts"] == 12
    assert rewritten["valid_directives"] == 12
    assert rewritten["provider_reported_cost"] == pytest.approx(round(12 * 0.001125936, 6))
    assert rewritten["prompt_tokens"] == 87216
    assert rewritten["terminal_transport_evidence"]["evidence_reference"] == f"opencode-go:{inner.case_id}:12"


def test_attempt_shape_20475_next_request_and_36374_cumulative_no_longer_abort(manifest, auth, tmp_path, git_state_provider):
    """The exact attempt-shaped values from attempt 9f595... (a 20,475-byte
    next canonical public request rejected before launch with zero counter
    movement, twelve completed responses, 36,374 cumulative public evidence
    bytes) no longer abort the campaign at case 1: the case materializes as
    ``PDB_NOT_REACHED / PDB_NOT_REACHED_NO_GATE`` with the completed
    accounting preserved, and the campaign proceeds to case 2."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {
        "provider_process_attempts": 12,
        "outcome": _production_exhausted_outcome(manifest, manifest["case_order"][0], route),
    }

    class BudgetExhaustedFirstCaseRunner(ScriptedCaseRunner):
        def __init__(self, scripted_entries):
            super().__init__(scripted_entries)
            self.oversized_rejections = []

        def __call__(self, case, **kwargs):
            if int(case["order_index"]) == 1:
                transport = kwargs["transport"]
                next_request = _canonical_payload(20_475)
                with pytest.raises(ModelRequestBudgetExceeded) as info:
                    transport.request(next_request, 1.0)
                self.oversized_rejections.append(info.value)
            return super().__call__(case, **kwargs)

    case_runner = BudgetExhaustedFirstCaseRunner(entries)
    record, factory, runner_obj, output = _run_campaign_custom(
        manifest, auth, tmp_path, case_runner=case_runner, runner_entries=entries,
        git_state_provider=git_state_provider,
    )

    assert len(case_runner.oversized_rejections) == 1
    assert case_runner.oversized_rejections[0].request_byte_count == 20_475
    assert case_runner.oversized_rejections[0].limit == 20_000

    assert record["status"] == "COMPLETED"
    assert record["stop_reason"] is None
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "completed"
    assert record["counts"]["completed_case_count"] == 6
    assert record["counts"]["unstarted_case_count"] == 0
    assert record["counts"]["aborted_case_count"] == 0
    assert record["counts"]["logical_model_calls"] == 17
    assert record["counts"]["provider_process_attempts"] == 17
    assert record["counts"]["accepted_directives"] == 17
    assert record["counts"]["transport_retries"] == 0
    assert record["provider_call_proof"]["logical_requests"] == 17
    assert record["provider_call_proof"]["process_launches"] == 17

    first = record["cases"][0]
    assert first["terminal_status"] == "PDB_NOT_REACHED"
    assert first["terminal_reason_code"] == "PDB_NOT_REACHED_NO_GATE"
    assert "36374" in first["termination_reason"] and "20000" in first["termination_reason"]
    assert "public-evidence budget exhausted" in first["termination_reason"]
    assert first["public_evidence_bytes"] == 20_000
    assert first["logical_model_calls"] == 12
    assert first["provider_process_attempts"] == 12
    assert first["valid_directives"] == 12
    assert first["retries"] == 0
    assert first["hypotheses_created"] == 1
    assert first["controller_states_visited"] == ["REPRODUCE", "UNDERSTAND", "PATCH", "VALIDATE"]
    assert first["prompt_tokens"] == 87216
    assert first["completion_tokens"] == 216
    assert first["reasoning_tokens"] == 4200
    assert first["provider_reported_cost"] == pytest.approx(round(12 * 0.001125936, 6))
    assert first["transport_evidence"]["provider_error"] is False
    assert first["terminal_transport_evidence"]["final_attempt_classification"] == "COMPLETED_RESPONSE"
    assert first["terminal_transport_evidence"]["timed_out"] is False
    assert first["terminal_transport_evidence"]["process_exit_code"] == 0
    assert first["terminal_transport_evidence"]["provider_completed_response"] is True
    assert first["terminal_transport_evidence"]["evidence_reference"].endswith(":12")
    assert first["repair_outcome"] == "NO_CANDIDATE"
    assert first["pdb_counts"] == dict(runner.ZERO_PDB_COUNTS)

    assert record["cases"][1]["case_id"] == manifest["case_order"][1]["case_id"]
    assert record["cases"][1]["terminal_status"] == "UNRESOLVED"
    assert record["cost_summary"]["classification"] == "REPORTED"
    assert record["cost_summary"]["total_provider_reported_cost"] == pytest.approx(round(12 * 0.001125936, 6) + 5 * 0.0042)
    assert (output / "cases" / "case-01-quixbugs-paired-pilot-v2__quixbugs-find-in-sorted-smoke-v1__pdb-on-uncertainty.json").is_file()
    assert runner.verify_attempt_package(output, manifest)["consistent"] is True


# ---- completed UNRESOLVED lifecycle budget exhaustion (attempt ddc26502...) -----


def _completed_unresolved_exhausted_outcome(manifest, case, route, **overrides):
    """The exact live-proven shape from attempt ddc26502...: a completed
    UNRESOLVED / NO_OP lifecycle with one submitted patch, completed verifier
    activity, completed provider responses, zero PDB activity, and cumulative
    public evidence bytes above the frozen 20,000-byte limit."""
    outcome = _completed_outcome(manifest, case, route, **{
        "terminal_status": "UNRESOLVED",
        "terminal_reason_code": "UNRESOLVED_COMPLETED",
        "termination_reason": "controller transitioned to Failed after post-patch validation NO_OP",
        "logical_model_calls": 15,
        "provider_process_attempts": 15,
        "valid_directives": 15,
        "retries": 0,
        "malformed_directive_rejections": 0,
        "bounded_directive_feedback_events": 0,
        "baseline_reproduction": True,
        "controller_states_visited": ["REPRODUCE", "UNDERSTAND", "PATCH", "VALIDATE"],
        "hypotheses_created": 1,
        "pdb_gate_decisions": [],
        "pdb_counts": dict(runner.ZERO_PDB_COUNTS),
        "pdb_sessions_started": 0,
        "successful_pdb_observations": 0,
        "failed_pdb_observations": 0,
        "verifier_runs": 1,
        "patch_submissions": 1,
        "independent_verifier_result": {"status": "COMPLETED", "outcome": "NO_OP", "lifecycle_succeeded": True},
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "COMPLETED_RESPONSE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True,
            "evidence_reference": f"opencode-go:{case['case_id']}:15",
        },
        "campaign_stop_evidence": {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS},
        "prompt_tokens": 6761,
        "completion_tokens": 66,
        "reasoning_tokens": 0,
        "provider_reported_cost": 0.013511232,
        "wall_clock_duration_seconds": 250.0,
        "public_evidence_bytes": 34787,
        "candidate_hash": "d" * 64,
        "repair_outcome": "NO_CANDIDATE",
    })
    outcome.update(overrides)
    return outcome


def test_completed_unresolved_budget_exhaustion_terminalizes(manifest):
    """The exact ddc26502... shape (15 completed responses, 1 patch
    submission, verifier COMPLETED with NO_OP, 34787 cumulative public
    evidence bytes) is terminalized as UNRESOLVED/UNRESOLVED_COMPLETED with
    all completed accounting preserved and public_evidence_bytes capped at
    the frozen 20,000-byte limit."""
    case = manifest["case_order"][0]
    route = _route_evidence(manifest)
    outcome = _completed_unresolved_exhausted_outcome(manifest, case, route)

    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest, case_policy=case["policy"])
    assert info.value.observed == 34787
    assert info.value.limit == 20000

    rewritten = runner._budget_exhausted_outcome(case, outcome, info.value, run_id="run-ddc26502")
    assert rewritten is not None
    assert rewritten["terminal_status"] == "UNRESOLVED"
    assert rewritten["terminal_reason_code"] == "UNRESOLVED_COMPLETED"
    assert rewritten["public_evidence_bytes"] == 20000
    assert "34787" in rewritten["termination_reason"] and "20000" in rewritten["termination_reason"]
    assert "public-evidence budget exhausted" in rewritten["termination_reason"]
    assert "UNRESOLVED" in rewritten["termination_reason"]

    assert rewritten["logical_model_calls"] == 15
    assert rewritten["provider_process_attempts"] == 15
    assert rewritten["valid_directives"] == 15
    assert rewritten["retries"] == 0
    assert rewritten["malformed_directive_rejections"] == 0
    assert rewritten["bounded_directive_feedback_events"] == 0
    assert rewritten["baseline_reproduction"] is True
    assert rewritten["controller_states_visited"] == ["REPRODUCE", "UNDERSTAND", "PATCH", "VALIDATE"]
    assert rewritten["hypotheses_created"] == 1
    assert rewritten["pdb_counts"] == dict(runner.ZERO_PDB_COUNTS)
    assert rewritten["pdb_sessions_started"] == 0
    assert rewritten["successful_pdb_observations"] == 0
    assert rewritten["failed_pdb_observations"] == 0
    assert rewritten["verifier_runs"] == 1
    assert rewritten["patch_submissions"] == 1
    assert rewritten["independent_verifier_result"] == {"status": "COMPLETED", "outcome": "NO_OP", "lifecycle_succeeded": True}
    assert rewritten["transport_evidence"] == {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False}
    assert rewritten["terminal_transport_evidence"]["final_attempt_classification"] == "COMPLETED_RESPONSE"
    assert rewritten["terminal_transport_evidence"]["provider_completed_response"] is True
    assert rewritten["terminal_transport_evidence"]["process_exit_code"] == 0
    assert rewritten["terminal_transport_evidence"]["evidence_reference"] == f"opencode-go:{case['case_id']}:15"
    assert rewritten["provider_reported_cost"] == pytest.approx(0.013511232)
    assert rewritten["prompt_tokens"] == 6761
    assert rewritten["completion_tokens"] == 66
    assert rewritten["candidate_hash"] == "d" * 64
    assert rewritten["repair_outcome"] == "NO_CANDIDATE"
    assert rewritten["campaign_stop_evidence"] == {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS}
    assert rewritten["preflight_failure_evidence"] == runner._default_preflight_failure_evidence()

    runner.enforce_case_budgets(rewritten, manifest, case_policy=case["policy"])


def test_completed_unresolved_budget_exhaustion_campaign_proceeds(manifest, auth, tmp_path, git_state_provider):
    """End-to-end: case 1 produces the ddc26502... UNRESOLVED shape with
    public_evidence_bytes == 34787; the case materializes as
    UNRESOLVED/UNRESOLVED_COMPLETED with completed accounting preserved in
    the campaign aggregates, and the campaign proceeds to case 2."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {
        "provider_process_attempts": 15,
        "outcome": _completed_unresolved_exhausted_outcome(manifest, manifest["case_order"][0], route),
    }

    record, factory, case_runner, output = _run_campaign_custom(
        manifest, auth, tmp_path, case_runner=ScriptedCaseRunner(entries), runner_entries=entries,
        git_state_provider=git_state_provider,
    )

    assert record["status"] == "COMPLETED"
    assert record["stop_reason"] is None
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "completed"
    assert record["counts"]["completed_case_count"] == 6
    assert record["counts"]["unstarted_case_count"] == 0
    assert record["counts"]["aborted_case_count"] == 0
    assert record["counts"]["logical_model_calls"] == 15 + 5
    assert record["counts"]["provider_process_attempts"] == 15 + 5
    assert record["counts"]["accepted_directives"] == 15 + 5

    first = record["cases"][0]
    assert first["terminal_status"] == "UNRESOLVED"
    assert first["terminal_reason_code"] == "UNRESOLVED_COMPLETED"
    assert first["public_evidence_bytes"] == 20000
    assert "34787" in first["termination_reason"] and "20000" in first["termination_reason"]
    assert "public-evidence budget exhausted" in first["termination_reason"]
    assert first["logical_model_calls"] == 15
    assert first["provider_process_attempts"] == 15
    assert first["valid_directives"] == 15
    assert first["patch_submissions"] == 1
    assert first["verifier_runs"] == 1
    assert first["independent_verifier_result"]["outcome"] == "NO_OP"
    assert first["provider_reported_cost"] == pytest.approx(0.013511232)
    assert first["repair_outcome"] == "NO_CANDIDATE"

    assert record["cases"][1]["case_id"] == manifest["case_order"][1]["case_id"]
    assert record["cases"][1]["terminal_status"] == "UNRESOLVED"
    assert (output / "cases" / "case-01-quixbugs-paired-pilot-v2__quixbugs-find-in-sorted-smoke-v1__pdb-on-uncertainty.json").is_file()
    assert (output / "cases" / "case-02-quixbugs-paired-pilot-v2__quixbugs-find-in-sorted-smoke-v1__static-baseline.json").is_file()


def test_completed_unresolved_budget_exhaustion_attempt_package_verifies(manifest, auth, tmp_path, git_state_provider):
    """After the campaign from the progression test, verify_attempt_package
    returns consistent with all six case files on disk and referenced."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {
        "provider_process_attempts": 15,
        "outcome": _completed_unresolved_exhausted_outcome(manifest, manifest["case_order"][0], route),
    }

    record, factory, case_runner, output = _run_campaign_custom(
        manifest, auth, tmp_path, case_runner=ScriptedCaseRunner(entries), runner_entries=entries,
        git_state_provider=git_state_provider,
    )

    verification = runner.verify_attempt_package(output, manifest)
    assert verification["consistent"] is True
    assert verification["errors"] == []
    assert verification["case_files_on_disk"] == 6
    assert verification["case_records_referenced"] == 6
    assert verification["terminal_commit"] == "PRESENT"
    assert verification["campaign_status"] == "COMPLETED"


# ---- completed RESOLVED lifecycle budget exhaustion (attempt 238f25ed...) -------


def _completed_resolved_exhausted_outcome(manifest, case, route, **overrides):
    """The exact live-proven shape from attempt 238f25ed...: a completed
    RESOLVED / RESOLVED_COMPLETED lifecycle with the patch applied, the
    post-patch reproduction passed, the regression tests passed, the
    verifier confirming RESOLVED, and cumulative public evidence bytes above
    the frozen 20,000-byte limit."""
    outcome = _completed_outcome(manifest, case, route, **{
        "terminal_status": "RESOLVED",
        "terminal_reason_code": "RESOLVED_COMPLETED",
        "termination_reason": "controller transitioned to Done after verifier-confirmed repair",
        "logical_model_calls": 16,
        "provider_process_attempts": 16,
        "valid_directives": 16,
        "retries": 0,
        "malformed_directive_rejections": 0,
        "bounded_directive_feedback_events": 0,
        "baseline_reproduction": True,
        "controller_states_visited": ["REPRODUCE", "UNDERSTAND", "PATCH", "VALIDATE"],
        "hypotheses_created": 1,
        "pdb_gate_decisions": [],
        "pdb_counts": dict(runner.ZERO_PDB_COUNTS),
        "pdb_sessions_started": 0,
        "successful_pdb_observations": 0,
        "failed_pdb_observations": 0,
        "verifier_runs": 1,
        "patch_submissions": 1,
        "independent_verifier_result": {"status": "COMPLETED", "outcome": "RESOLVED", "lifecycle_succeeded": True},
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "COMPLETED_RESPONSE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True,
            "evidence_reference": f"opencode-go:{case['case_id']}:16",
        },
        "campaign_stop_evidence": {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS},
        "prompt_tokens": 89712,
        "completion_tokens": 247,
        "reasoning_tokens": 4315,
        "provider_reported_cost": 0.014511232,
        "wall_clock_duration_seconds": 205.0,
        "public_evidence_bytes": 36189,
        "candidate_hash": "c" * 64,
        "repair_outcome": "RESOLVED",
    })
    outcome.update(overrides)
    return outcome


def test_completed_resolved_budget_exhaustion_terminalizes(manifest):
    """The exact 238f25ed... shape (16 completed responses, 1 patch
    submission, verifier COMPLETED with RESOLVED, 36189 cumulative public
    evidence bytes) is terminalized as RESOLVED/RESOLVED_COMPLETED with all
    completed accounting preserved and public_evidence_bytes capped at the
    frozen 20,000-byte limit."""
    case = manifest["case_order"][0]
    route = _route_evidence(manifest)
    outcome = _completed_resolved_exhausted_outcome(manifest, case, route)

    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest, case_policy=case["policy"])
    assert info.value.observed == 36189
    assert info.value.limit == 20000

    rewritten = runner._budget_exhausted_outcome(case, outcome, info.value, run_id="run-238f25ed")
    assert rewritten is not None
    assert rewritten["terminal_status"] == "RESOLVED"
    assert rewritten["terminal_reason_code"] == "RESOLVED_COMPLETED"
    assert rewritten["public_evidence_bytes"] == 20000
    assert "36189" in rewritten["termination_reason"] and "20000" in rewritten["termination_reason"]
    assert "public-evidence budget exhausted" in rewritten["termination_reason"]
    assert "RESOLVED" in rewritten["termination_reason"]

    assert rewritten["logical_model_calls"] == 16
    assert rewritten["provider_process_attempts"] == 16
    assert rewritten["valid_directives"] == 16
    assert rewritten["retries"] == 0
    assert rewritten["malformed_directive_rejections"] == 0
    assert rewritten["bounded_directive_feedback_events"] == 0
    assert rewritten["baseline_reproduction"] is True
    assert rewritten["controller_states_visited"] == ["REPRODUCE", "UNDERSTAND", "PATCH", "VALIDATE"]
    assert rewritten["hypotheses_created"] == 1
    assert rewritten["pdb_counts"] == dict(runner.ZERO_PDB_COUNTS)
    assert rewritten["pdb_sessions_started"] == 0
    assert rewritten["successful_pdb_observations"] == 0
    assert rewritten["failed_pdb_observations"] == 0
    assert rewritten["verifier_runs"] == 1
    assert rewritten["patch_submissions"] == 1
    assert rewritten["independent_verifier_result"] == {"status": "COMPLETED", "outcome": "RESOLVED", "lifecycle_succeeded": True}
    assert rewritten["transport_evidence"] == {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False}
    assert rewritten["terminal_transport_evidence"]["final_attempt_classification"] == "COMPLETED_RESPONSE"
    assert rewritten["terminal_transport_evidence"]["provider_completed_response"] is True
    assert rewritten["terminal_transport_evidence"]["process_exit_code"] == 0
    assert rewritten["terminal_transport_evidence"]["evidence_reference"] == f"opencode-go:{case['case_id']}:16"
    assert rewritten["provider_reported_cost"] == pytest.approx(0.014511232)
    assert rewritten["prompt_tokens"] == 89712
    assert rewritten["completion_tokens"] == 247
    assert rewritten["reasoning_tokens"] == 4315
    assert rewritten["candidate_hash"] == "c" * 64
    assert rewritten["repair_outcome"] == "RESOLVED"
    assert rewritten["campaign_stop_evidence"] == {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS}
    assert rewritten["preflight_failure_evidence"] == runner._default_preflight_failure_evidence()

    runner.enforce_case_budgets(rewritten, manifest, case_policy=case["policy"])


def test_completed_resolved_budget_exhaustion_campaign_proceeds(manifest, auth, tmp_path, git_state_provider):
    """End-to-end: case 1 produces the 238f25ed... RESOLVED shape with
    public_evidence_bytes == 36189; the case materializes as
    RESOLVED/RESOLVED_COMPLETED with completed accounting preserved in the
    campaign aggregates, and the campaign proceeds to case 2."""
    route = _route_evidence(manifest)
    entries = _completed_entries(manifest)
    entries[0] = {
        "provider_process_attempts": 16,
        "outcome": _completed_resolved_exhausted_outcome(manifest, manifest["case_order"][0], route),
    }

    record, factory, case_runner, output = _run_campaign_custom(
        manifest, auth, tmp_path, case_runner=ScriptedCaseRunner(entries), runner_entries=entries,
        git_state_provider=git_state_provider,
    )

    assert record["status"] == "COMPLETED"
    assert record["stop_reason"] is None
    assert record["case_lifecycle_states"][manifest["case_order"][0]["case_id"]] == "completed"
    assert record["counts"]["completed_case_count"] == 6
    assert record["counts"]["unstarted_case_count"] == 0
    assert record["counts"]["aborted_case_count"] == 0
    assert record["counts"]["logical_model_calls"] == 16 + 5
    assert record["counts"]["provider_process_attempts"] == 16 + 5
    assert record["counts"]["accepted_directives"] == 16 + 5

    first = record["cases"][0]
    assert first["terminal_status"] == "RESOLVED"
    assert first["terminal_reason_code"] == "RESOLVED_COMPLETED"
    assert first["public_evidence_bytes"] == 20000
    assert "36189" in first["termination_reason"] and "20000" in first["termination_reason"]
    assert "public-evidence budget exhausted" in first["termination_reason"]
    assert first["logical_model_calls"] == 16
    assert first["provider_process_attempts"] == 16
    assert first["valid_directives"] == 16
    assert first["patch_submissions"] == 1
    assert first["verifier_runs"] == 1
    assert first["independent_verifier_result"]["outcome"] == "RESOLVED"
    assert first["provider_reported_cost"] == pytest.approx(0.014511232)
    assert first["repair_outcome"] == "RESOLVED"

    assert record["cases"][1]["case_id"] == manifest["case_order"][1]["case_id"]
    assert record["cases"][1]["terminal_status"] == "UNRESOLVED"
    assert record["cost_summary"]["classification"] == "REPORTED"
    assert record["cost_summary"]["total_provider_reported_cost"] == pytest.approx(round(0.014511232, 6) + 5 * 0.0042)
    assert (output / "cases" / "case-01-quixbugs-paired-pilot-v2__quixbugs-find-in-sorted-smoke-v1__pdb-on-uncertainty.json").is_file()
    assert (output / "cases" / "case-02-quixbugs-paired-pilot-v2__quixbugs-find-in-sorted-smoke-v1__static-baseline.json").is_file()
    assert runner.verify_attempt_package(output, manifest)["consistent"] is True


# ---- honest abort for unproven / contradictory shapes ---------------------------


def test_completed_unresolved_with_pdb_activity_still_aborts(manifest):
    """A shape claiming UNRESOLVED but with non-zero PDB activity has no valid
    frozen terminal representation and returns None (honest abort)."""
    case = manifest["case_order"][0]
    route = _route_evidence(manifest)
    outcome = _completed_unresolved_exhausted_outcome(manifest, case, route,
        pdb_counts={"total_gate_decisions": 1, "allowed_gate_openings": 1, "rejected_gate_decisions": 0,
                    "sessions_started": 1, "successful_observations": 0, "failed_observations": 0},
        pdb_gate_decisions=[{"allowed": True, "reason": "uncertainty"}],
        pdb_sessions_started=1,
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, outcome, info.value, run_id="run-pdb") is None


def test_completed_unresolved_with_contradictory_verifier_still_aborts(manifest):
    """A shape claiming UNRESOLVED but with a non-completed verifier has no
    valid frozen terminal representation and returns None."""
    case = manifest["case_order"][0]
    route = _route_evidence(manifest)
    outcome = _completed_unresolved_exhausted_outcome(manifest, case, route,
        independent_verifier_result={"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        verifier_runs=0,
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, outcome, info.value, run_id="run-bad-verifier") is None


def test_completed_resolved_with_contradictory_verifier_still_aborts(manifest):
    """A shape claiming RESOLVED but with a non-resolved verifier outcome has
    no valid frozen terminal representation and returns None (honest abort)."""
    case = manifest["case_order"][0]
    route = _route_evidence(manifest)
    outcome = _completed_resolved_exhausted_outcome(manifest, case, route,
        independent_verifier_result={"status": "COMPLETED", "outcome": "NO_OP", "lifecycle_succeeded": True},
        repair_outcome="NO_CANDIDATE",
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, outcome, info.value, run_id="run-resolved-contradiction") is None


def test_invalid_model_response_budget_exhaustion_still_aborts(manifest):
    """An INVALID_MODEL_RESPONSE shape with public_evidence_bytes > 20000 has
    no valid frozen terminal representation in this task and returns None."""
    case = manifest["case_order"][0]
    route = _route_evidence(manifest)
    outcome = _completed_unresolved_exhausted_outcome(manifest, case, route,
        terminal_status="INVALID_MODEL_RESPONSE",
        terminal_reason_code="MALFORMED_RESPONSE",
        patch_submissions=0,
        verifier_runs=0,
        independent_verifier_result={"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        malformed_directive_rejections=1,
        bounded_directive_feedback_events=1,
        candidate_hash=None,
        transport_evidence={"completed_response": True, "malformed_response": True, "provider_error": False, "synthetic": False},
        terminal_transport_evidence={
            "final_attempt_classification": "MALFORMED_RESPONSE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True,
            "evidence_reference": f"opencode-go:{case['case_id']}:15",
        },
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, outcome, info.value, run_id="run-invalid") is None


# ---- unchanged behaviors -------------------------------------------------------


def test_real_provider_error_remains_retryable_transport_error(tmp_path, manifest, synthetic_executable):
    """A genuine provider error stays a transport error with the existing
    retry contract: it is never converted into the typed budget signal, and
    the process-launch accounting still moves."""
    harness = _harness(tmp_path, manifest, synthetic_executable)
    inner = _scenario_transport(harness, "startup-failure")
    with pytest.raises(LiveTransportError) as info:
        inner.request(_scenario_payload("startup-failure"), 25.0)
    assert info.value.kind == "process_error"
    assert not isinstance(info.value, ModelRequestBudgetExceeded)
    assert inner.process_attempts == 1
    assert harness["factory"].spawned_processes == 1
    assert inner.last_provider_error_category == "process_error"
