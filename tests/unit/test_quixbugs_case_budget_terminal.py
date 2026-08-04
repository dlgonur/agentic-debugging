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
import hashlib
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
    HypothesisConfidence,
    HypothesisStatus,
    HypothesisLedger,
    PdbPolicy,
    RootCauseHypothesis,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry, ToolResult, ToolSpec
from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.tools import DemoToolContext, build_registry, prepare_pdb_probe
from agentic_debugger.evaluation.live import (
    LiveCaseStatus,
    LiveModelAdapter,
    LiveModelAdapterError,
    LiveModelConfig,
    LiveRunLimits,
    LiveTransportError,
    ModelRequestBudgetExceeded,
    _finalize_live_case,
)
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.events.schema import ObservationStatus
from agentic_debugger.runtime.workspace import TaskWorkspace

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


# ---- PDB gate recording: one decision per real gate lifecycle ------------------
#
# Regressions for attempt ``quixbugs-paired-pilot-v2-attempt-
# 5b4080ddb6ec44ba8af49762af0a54eeb7440c12a9fc4c7ab71738886d322fe4``: one PDB
# session lifecycle was reported as nine ``allowed_gate_openings`` because the
# live adapter recorded a gate decision on every reread of the pure
# ``decide_pdb_access`` gate.  The repair records exactly one decision per
# real ``UNDERSTAND -> RUNTIME_EVIDENCE`` gate consumption, bounded per
# logical call, while allowing a later distinct controller lifecycle to
# record a new decision.


def _pdb_registry(context: DemoToolContext) -> ToolRegistry:
    return build_registry(context, pdb_policy=PdbPolicy.ON_UNCERTAINTY)


def _understand_snapshot_with_qualifying_hypothesis(task, model_call_index: int = 4):
    """A controller snapshot in UNDERSTAND with a low-confidence, runtime-evidence
    hypothesis that passes the ``pdb-on-uncertainty`` gate."""
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    hypothesis = RootCauseHypothesis(
        "h-1", "root cause", HypothesisConfidence.LOW, HypothesisStatus.ACTIVE, (), True, 1,
    )
    return ControllerSnapshot(
        "run", task.task_id, ControllerState.UNDERSTAND, model_call_index, limits,
        ControllerBudgetState(), HypothesisLedger((hypothesis,)),
    )


class _RuntimeTransitionTransport:
    """Returns a single ``transition -> RuntimeEvidence`` directive."""

    def __init__(self):
        self.calls = 0

    def request(self, payload, timeout_seconds):
        self.calls += 1
        return {
            "directive": {"kind": "transition", "target_state": "RuntimeEvidence", "reason": "qualifying hypothesis"},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


class _MalformedThenRuntimeTransitionTransport:
    """Returns a malformed directive on the first attempt, then a valid
    ``transition -> RuntimeEvidence`` directive on the second attempt of the
    same logical call (exercising the transport retry loop)."""

    def __init__(self):
        self.calls = 0

    def request(self, payload, timeout_seconds):
        self.calls += 1
        if self.calls == 1:
            return {"directive": {"kind": "action", "name": "bogus", "arguments": {}}, "usage": {}}
        return {
            "directive": {"kind": "transition", "target_state": "RuntimeEvidence", "reason": "qualifying hypothesis"},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


class _AlwaysRuntimeTransitionTransport:
    """Always returns ``transition -> RuntimeEvidence`` (used to test denied
    retries: the gate denies, so ``_parse`` rejects every attempt)."""

    def __init__(self):
        self.calls = 0

    def request(self, payload, timeout_seconds):
        self.calls += 1
        return {
            "directive": {"kind": "transition", "target_state": "RuntimeEvidence", "reason": "request runtime"},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


def test_pdb_gate_records_exactly_one_decision_per_real_transition():
    """Primary inflation regression: one ``UNDERSTAND -> RUNTIME_EVIDENCE``
    transition records exactly one allowed gate decision, not one per reread
    (was 9+ in attempt 5b4080dd...)."""
    task = _curated_task()
    transport = _RuntimeTransitionTransport()
    live_adapter = LiveModelAdapter(
        task=task, policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=LiveModelConfig("test-model", ("test-command",)),
        transport=transport, limits=LiveRunLimits(max_model_requests=3, max_retries=0),
        registry=_live_registry(),
    )
    live_adapter._failure_reproduced = True
    snapshot = _understand_snapshot_with_qualifying_hypothesis(task)
    live_adapter.next_directive(snapshot)
    assert len(live_adapter.pdb_gate_decisions) == 1
    assert live_adapter.pdb_gate_decisions[0]["allowed"] is True


def test_pdb_gate_records_one_decision_across_transport_retries():
    """The per-logical-call dedup bound: a malformed-then-valid sequence in
    the retry loop records at most one gate decision for the logical call."""
    task = _curated_task()
    transport = _MalformedThenRuntimeTransitionTransport()
    live_adapter = LiveModelAdapter(
        task=task, policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=LiveModelConfig("test-model", ("test-command",)),
        transport=transport, limits=LiveRunLimits(max_model_requests=3, max_retries=2),
        registry=_live_registry(),
    )
    live_adapter._failure_reproduced = True
    snapshot = _understand_snapshot_with_qualifying_hypothesis(task)
    live_adapter.next_directive(snapshot)
    assert len(live_adapter.pdb_gate_decisions) == 1
    assert live_adapter.pdb_gate_decisions[0]["allowed"] is True


def test_pdb_gate_records_one_denied_decision_across_denied_retries():
    """Denied-retry dedup: when the gate denies (no active hypothesis), the
    model's repeated ``RuntimeEvidence`` requests are rejected by ``_parse``
    each retry, but exactly one denied decision is recorded for the call."""
    task = _curated_task()
    transport = _AlwaysRuntimeTransitionTransport()
    live_adapter = LiveModelAdapter(
        task=task, policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=LiveModelConfig("test-model", ("test-command",)),
        transport=transport, limits=LiveRunLimits(max_model_requests=3, max_retries=2),
        registry=_live_registry(),
    )
    live_adapter._failure_reproduced = True
    # UNDERSTAND with no active hypothesis -> gate denies.
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    snapshot = ControllerSnapshot(
        "run", task.task_id, ControllerState.UNDERSTAND, 0, limits,
        ControllerBudgetState(), HypothesisLedger(),
    )
    with pytest.raises(LiveModelAdapterError):
        live_adapter.next_directive(snapshot)
    assert len(live_adapter.pdb_gate_decisions) == 1
    assert live_adapter.pdb_gate_decisions[0]["allowed"] is False
    assert live_adapter.directive_rejections  # the transition was rejected each retry


def test_pdb_gate_records_one_decision_per_distinct_lifecycle():
    """Multi-lifecycle regression: two distinct ``UNDERSTAND ->
    RUNTIME_EVIDENCE`` controller steps (different ``model_call_index``
    values) each record their own decision; the per-lifecycle flag reset
    enables the second recording."""
    task = _curated_task()
    transport = _RuntimeTransitionTransport()
    live_adapter = LiveModelAdapter(
        task=task, policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=LiveModelConfig("test-model", ("test-command",)),
        transport=transport, limits=LiveRunLimits(max_model_requests=6, max_retries=0),
        registry=_live_registry(),
    )
    live_adapter._failure_reproduced = True
    # First lifecycle: model_call_index 4 (UNDERSTAND, qualifying hypothesis).
    snapshot_1 = _understand_snapshot_with_qualifying_hypothesis(task, model_call_index=4)
    live_adapter.next_directive(snapshot_1)
    # Second lifecycle: model_call_index 5 (also UNDERSTAND, same hypothesis).
    snapshot_2 = _understand_snapshot_with_qualifying_hypothesis(task, model_call_index=5)
    live_adapter.next_directive(snapshot_2)
    assert len(live_adapter.pdb_gate_decisions) == 2
    assert all(d["allowed"] is True for d in live_adapter.pdb_gate_decisions)


def test_static_baseline_never_records_pdb_gate_decision():
    """Static-baseline regression: the gate policy is DISABLED; even if the
    model spuriously requests ``RuntimeEvidence``, no gate decision is
    recorded and ``pdb_gate_decisions`` stays empty."""
    task = _curated_task()
    transport = _AlwaysRuntimeTransitionTransport()
    live_adapter = LiveModelAdapter(
        task=task, policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig("test-model", ("test-command",)),
        transport=transport, limits=LiveRunLimits(max_model_requests=3, max_retries=2),
        registry=_live_registry(),
    )
    live_adapter._failure_reproduced = True
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    snapshot = ControllerSnapshot(
        "run", task.task_id, ControllerState.UNDERSTAND, 0, limits,
        ControllerBudgetState(), HypothesisLedger(),
    )
    with pytest.raises(LiveModelAdapterError):
        live_adapter.next_directive(snapshot)
    assert live_adapter.pdb_gate_decisions == []


def test_full_pdb_on_uncertainty_case_records_one_gate_opening(tmp_path):
    """Full-case regression: a complete ``pdb-on-uncertainty`` controller run
    with one PDB session records exactly one allowed gate opening."""
    task = _curated_task()
    fixture = REPO_ROOT / "agentic_debugger" / "datasets" / "curated" / task.task_id
    workspace = TaskWorkspace(str(fixture), parent_dir=str(tmp_path))
    probe = prepare_pdb_probe(fixture, scenario_for(task.task_id), tmp_path)
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=probe)
    registry = _pdb_registry(context)
    patch = build_reference_patch(
        (fixture / scenario_for(task.task_id).reference_repair.target_path).read_text(encoding="utf-8"),
        scenario_for(task.task_id).reference_repair,
    )

    class _PdbCaseTransport:
        def __init__(self):
            self.index = 0
            self.directives = [
                {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
                {"kind": "transition", "target_state": "Understand", "reason": "reproduced"},
                {"kind": "action", "name": "find_function", "arguments": {"name": scenario_for(task.task_id).localization.symbol, "path": scenario_for(task.task_id).localization.file_path}},
                {"kind": "action", "name": "get_source_window", "arguments": {"path": scenario_for(task.task_id).localization.file_path, "line": 1}},
                {"kind": "add_hypothesis", "hypothesis_id": scenario_for(task.task_id).hypothesis_id, "statement": scenario_for(task.task_id).root_cause_statement, "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": True},
                {"kind": "transition", "target_state": "RuntimeEvidence", "reason": "qualifying hypothesis requests runtime evidence"},
                {"kind": "action", "name": "start_pdb_session", "arguments": {}},
                {"kind": "action", "name": "get_stack_summary", "arguments": {}},
                {"kind": "action", "name": "get_frame_locals", "arguments": {"frame_id": 0, "pause_generation": 1}},
                {"kind": "action", "name": "stop_pdb_session", "arguments": {}},
                {"kind": "transition", "target_state": "Understand", "reason": "runtime evidence collected"},
                {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": scenario_for(task.task_id).hypothesis_id, "statement": scenario_for(task.task_id).root_cause_statement, "target_file": scenario_for(task.task_id).localization.file_path, "target_symbol": scenario_for(task.task_id).localization.symbol, "confidence": "high"}},
                {"kind": "transition", "target_state": "Patch", "reason": "runtime evidence is sufficient"},
                {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch}},
                {"kind": "action", "name": "syntax_check", "arguments": {}},
                {"kind": "transition", "target_state": "Validate", "reason": "syntax checked"},
                {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "post_patch"}},
                {"kind": "action", "name": "run_regression_tests", "arguments": {}},
                {"kind": "action", "name": "classify_outcome", "arguments": {}},
                {"kind": "transition", "target_state": "Done", "reason": "finished"},
            ]

        def request(self, payload, timeout_seconds):
            directive = self.directives[min(self.index, len(self.directives) - 1)]
            self.index += 1
            return {"directive": directive, "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

    transport = _PdbCaseTransport()
    live_adapter = LiveModelAdapter(
        task=task, policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=LiveModelConfig("test-model", ("test-command",)),
        transport=transport, limits=LiveRunLimits(max_model_requests=32, max_controller_steps=32),
        registry=registry, evaluation_id="e", case_id="c", run_id="r", trajectory_id="r",
    )
    controller = DeterministicController(registry, live_adapter, ControllerRunConfig(max_model_calls=32))
    result = controller.run(ControllerSnapshot(
        "r", task.task_id, ControllerState.REPRODUCE, 0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(), HypothesisLedger(),
    ))
    assert result.final_state is ControllerState.DONE
    allowed = sum(1 for d in live_adapter.pdb_gate_decisions if d["allowed"] is True)
    rejected = sum(1 for d in live_adapter.pdb_gate_decisions if d["allowed"] is False)
    assert len(live_adapter.pdb_gate_decisions) == 1
    assert allowed == 1
    assert rejected == 0

# ---- v3 static-baseline pre-validate budget exhaustion (attempt e974af4...) ----
#
# Regressions for the v3 campaign: the live-proven shape of attempt
# ``quixbugs-paired-pilot-v2-attempt-e974af41545d4a1f8fbee527d880d9636cc2af2ddcc2439182b95e152bb14b39``
# case 5 (``quixbugs-is-valid-parenthesization-smoke-v1 / static-baseline``):
# ten completed provider responses, the controller reached Patch and applied a
# candidate (``return True`` -> ``return depth == 0``), the next public request
# would have exceeded the frozen 20000-byte public-evidence budget before the
# transition to Validate, the verifier never ran, and 34704 cumulative public
# evidence bytes were observed.  The exact v2 attempt measurements are used as
# provenance; all synthetic fixture identities and evidence references are
# restamped for the v3 campaign.

_IS_VALID_PARENTHESIZATION_PATCH = (
    "--- a/python_programs/is_valid_parenthesization.py\n"
    "+++ b/python_programs/is_valid_parenthesization.py\n"
    "@@ -9,5 +9,5 @@\n"
    "             if depth < 0:\n"
    "                 return False\n"
    " \n"
    "-    return True\n"
    "+    return depth == 0\n"
)


@pytest.fixture
def manifest_v3():
    return pilot.load_manifest(pilot.MANIFEST_PATH_V3)


@pytest.fixture
def auth_v3(manifest_v3, tmp_path):
    return _valid_authorization(
        manifest_v3, tmp_path / "attempt-out",
        campaign_id=manifest_v3["campaign_id"],
        campaign_version=manifest_v3["campaign_version"],
        campaign_attempt_identity="quixbugs-paired-pilot-v3-attempt-" + "d" * 64,
    )


def _v3_candidate_hash():
    return hashlib.sha256(
        pilot.canonical_json({"patch": _IS_VALID_PARENTHESIZATION_PATCH}).encode("utf-8")
    ).hexdigest()


def _static_baseline_pre_validate_events_jsonl(*, total_bytes: int) -> str:
    """The case-5 events log padded to exactly ``total_bytes`` bytes.

    Contains the baseline reproduction, two expressed hypotheses (h-1, h-2),
    the Patch transition, the ``apply_patch`` action carrying the exact
    candidate diff, the ``apply_patch_result`` observation with
    ``applied: true``, and the final event stopping in Patch with a model
    error (the budget-exhaustion stop).
    """
    patch_sha = hashlib.sha256(_IS_VALID_PARENTHESIZATION_PATCH.encode("utf-8")).hexdigest()
    lines = [
        {"event_type": "decision", "name": "decision", "state": "Reproduce", "payload": {"directive_kind": "action", "model_call_index": 0}},
        {"event_type": "action", "name": "run_reproduction", "state": "Reproduce", "payload": {"action": {"name": "run_reproduction"}}},
        {"event_type": "observation", "name": "run_reproduction", "state": "Reproduce", "payload": {"observation": {"name": "run_reproduction", "status": "ok", "payload": {"phase": "baseline", "failure_reproduced": True}}}},
        {"event_type": "decision", "name": "decision", "state": "Understand", "payload": {"directive_kind": "add_hypothesis", "model_call_index": 1}},
        {"event_type": "action", "name": "express_root_cause_hypothesis", "state": "Understand", "payload": {"action": {"name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "h-1"}}}},
        {"event_type": "observation", "name": "express_root_cause_hypothesis", "state": "Understand", "payload": {"observation": {"name": "express_root_cause_hypothesis", "status": "ok", "payload": {"hypothesis_id": "h-1"}}}},
        {"event_type": "action", "name": "express_root_cause_hypothesis", "state": "Understand", "payload": {"action": {"name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "h-2"}}}},
        {"event_type": "observation", "name": "express_root_cause_hypothesis", "state": "Understand", "payload": {"observation": {"name": "express_root_cause_hypothesis", "status": "ok", "payload": {"hypothesis_id": "h-2"}}}},
        {"event_type": "transition", "name": "transition", "state": "Patch", "payload": {"source_state": "Understand", "target_state": "Patch", "reason": "hypotheses recorded"}},
        {"event_type": "action", "name": "apply_patch", "state": "Patch", "payload": {"action": {"action_id": "action-000000009", "name": "apply_patch", "arguments": {"patch": _IS_VALID_PARENTHESIZATION_PATCH}}}},
        {"event_type": "observation", "name": "apply_patch", "state": "Patch", "payload": {"observation": {"action_id": "action-000000009", "name": "apply_patch", "status": "ok", "payload": {"applied": True, "changed_files": ["python_programs/is_valid_parenthesization.py"], "hunk_count": 1, "patch_sha256": patch_sha}}}},
        {"event_type": "final", "name": "final", "state": "Patch", "payload": {"final_state": "Patch", "stop_reason": "model_error", "model_calls": 10}},
    ]
    base = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n"
    base_bytes = len(base.encode("utf-8"))
    assert total_bytes > base_bytes
    padding = json.dumps(
        {"event_type": "padding", "name": "padding", "state": "Patch",
         "payload": {"filler": "x" * (total_bytes - base_bytes - 1 - len(json.dumps(
             {"event_type": "padding", "name": "padding", "state": "Patch",
              "payload": {"filler": "x"}}, ensure_ascii=False)) + 1)}},
        ensure_ascii=False,
    )
    value = base + padding + "\n"
    assert len(value.encode("utf-8")) == total_bytes
    return value


def _static_baseline_pre_validate_exhausted_outcome(manifest, case, route, **overrides):
    """The exact e974af4... case-5 raw outcome restamped for the v3 campaign.

    Ten completed provider responses, 34704 cumulative public evidence bytes,
    the controller stopped in Patch with an applied candidate, the verifier
    never ran.  The adapter maps the live VALIDATION_NOT_REACHED status to
    VALIDATION_NOT_REACHED/VALIDATION_NOT_REACHED_PRE_VALIDATE with
    candidate_provenance = applied_patch_event.
    """
    source_hash = next(item["source_sha256"] for item in manifest["inventory"] if item["task_id"] == case["task_id"])
    outcome = {
        "terminal_status": "VALIDATION_NOT_REACHED",
        "terminal_reason_code": "VALIDATION_NOT_REACHED_PRE_VALIDATE",
        "termination_reason": "opencode-go adapter: VALIDATION_NOT_REACHED: public_evidence_budget_exceeded",
        "logical_model_calls": 10,
        "provider_process_attempts": 10,
        "retries": 0,
        "valid_directives": 10,
        "malformed_directive_rejections": 0,
        "bounded_directive_feedback_events": 0,
        "baseline_reproduction": True,
        "controller_states_visited": ["Reproduce", "Understand", "Patch"],
        "hypotheses_created": 2,
        "pdb_gate_decisions": [],
        "pdb_counts": dict(runner.ZERO_PDB_COUNTS),
        "pdb_sessions_started": 0,
        "successful_pdb_observations": 0,
        "failed_pdb_observations": 0,
        "verifier_runs": 0,
        "patch_submissions": 1,
        "candidate_provenance": "applied_patch_event",
        "independent_verifier_result": {"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "COMPLETED_RESPONSE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True,
            "evidence_reference": f"opencode-go:{case['case_id']}:10",
        },
        "blocked_evidence": {"block_kind": "none", "reason_code": "NONE", "confirmed": False, "evidence_reference": "v3-synthetic:none"},
        "infrastructure_evidence": {
            "stage": "none", "reason_code": "NONE", "confirmed_failure": False, "classification": "NONE",
            "terminal_classification": "NOT_APPLICABLE", "provider_attempt_index": None,
            "prior_lifecycle_completed": False, "source_mutation_observed": False,
            "expected_source_hash": None, "evidence_reference": "v3-synthetic:none",
        },
        "preflight_failure_evidence": {field: None for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS},
        "campaign_stop_evidence": dict({field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS}, confirmed=False),
        "prompt_tokens": 45784,
        "completion_tokens": 705,
        "reasoning_tokens": 0,
        "provider_reported_cost": 0.0090328,
        "wall_clock_duration_seconds": 169.701511,
        "public_evidence_bytes": 34704,
        "canonical_source_restoration": True,
        "owned_workspace_cleanup": True,
        "evidence_consistency": True,
        "public_request_hash": hashlib.sha256(b"v3-synthetic-events").hexdigest(),
        "source_hash": source_hash,
        "candidate_hash": _v3_candidate_hash(),
        "repair_outcome": "NO_CANDIDATE",
        "resource_ids": {},
    }
    outcome.update(overrides)
    return outcome


def _run_campaign_v3(manifest, auth, tmp_path, *, case_runner, runner_entries, git_state_provider=None):
    return _run_campaign_custom(
        manifest, auth, tmp_path,
        case_runner=case_runner,
        runner_entries=runner_entries,
        git_state_provider=git_state_provider,
    )


def test_v3_manifest_and_authorization_validate(manifest_v3, auth_v3):
    """The v3 manifest validates with the result-v3 schema and its hash is
    frozen; the v3 authorization binds to the v3 manifest."""
    assert manifest_v3["campaign_id"] == "quixbugs-paired-pilot-v3"
    assert manifest_v3["campaign_version"] == 3
    assert manifest_v3["outcome_schema"]["schema_version"] == "quixbugs-paired-pilot-result-v3"
    assert "VALIDATION_NOT_REACHED" in manifest_v3["outcome_schema"]["terminal_statuses"]
    assert "candidate_provenance" in manifest_v3["outcome_schema"]["required_fields"]
    assert pilot.validate_manifest(manifest_v3) == "f5f513a16008ce807b4ed248e0310958940aefd348199e77dc0bbabc9a9e45cf"
    assert manifest_v3["qualification_contract_hash"] == "7246d289fcc689e93d93385751cbae5fa75a3c52e3c04e001f2c977a1990c52d"
    assert auth_v3["campaign_id"] == manifest_v3["campaign_id"]
    assert auth_v3["campaign_version"] == manifest_v3["campaign_version"]


def test_validation_not_reached_budget_exhaustion_terminalizes_v3(manifest_v3):
    """The exact e974af4... case-5 shape raises PublicEvidenceBudgetExhausted
    and rewrites to VALIDATION_NOT_REACHED/VALIDATION_NOT_REACHED_PRE_VALIDATE
    with public_evidence_bytes clamped to the frozen limit and every completed
    accounting field preserved."""
    case = manifest_v3["case_order"][4]
    route = _route_evidence(manifest_v3)
    outcome = _static_baseline_pre_validate_exhausted_outcome(manifest_v3, case, route)

    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest_v3, case_policy=case["policy"])
    assert info.value.observed == 34704
    assert info.value.limit == 20000

    rewritten = runner._budget_exhausted_outcome(
        case, outcome, info.value, run_id="run-e974af4-v3", manifest=manifest_v3,
    )
    assert rewritten is not None
    assert rewritten["terminal_status"] == "VALIDATION_NOT_REACHED"
    assert rewritten["terminal_reason_code"] == "VALIDATION_NOT_REACHED_PRE_VALIDATE"
    assert rewritten["public_evidence_bytes"] == 20000
    assert "34704" in rewritten["termination_reason"] and "20000" in rewritten["termination_reason"]
    assert "VALIDATION_NOT_REACHED" in rewritten["termination_reason"]
    assert rewritten["logical_model_calls"] == 10
    assert rewritten["provider_process_attempts"] == 10
    assert rewritten["valid_directives"] == 10
    assert rewritten["retries"] == 0
    assert rewritten["patch_submissions"] == 1
    assert rewritten["candidate_provenance"] == "applied_patch_event"
    assert rewritten["candidate_hash"] == _v3_candidate_hash()
    assert rewritten["verifier_runs"] == 0
    assert rewritten["repair_outcome"] == "NO_CANDIDATE"
    assert rewritten["controller_states_visited"] == ["Reproduce", "Understand", "Patch"]
    assert rewritten["prompt_tokens"] == 45784
    assert rewritten["completion_tokens"] == 705
    assert rewritten["reasoning_tokens"] == 0
    assert rewritten["provider_reported_cost"] == pytest.approx(0.0090328)
    assert rewritten["wall_clock_duration_seconds"] == pytest.approx(169.701511)
    assert rewritten["hypotheses_created"] == 2
    assert rewritten["pdb_counts"] == dict(runner.ZERO_PDB_COUNTS)
    assert rewritten["transport_evidence"]["completed_response"] is True
    assert rewritten["terminal_transport_evidence"]["evidence_reference"] == f"opencode-go:{case['case_id']}:10"

    runner.enforce_case_budgets(rewritten, manifest_v3, case_policy=case["policy"])


def _completed_entries_v3(manifest):
    """v2-shaped completed entries plus the v3 candidate_provenance required
    field (None for no-candidate outcomes, verifier_record for the completed
    RESOLVED outcomes with a submitted patch)."""
    route = _route_evidence(manifest)
    entries = []
    for case in manifest["case_order"]:
        outcome = _completed_outcome(manifest, case, route)
        outcome["candidate_provenance"] = "verifier_record" if outcome.get("patch_submissions", 0) > 0 else None
        entries.append({"provider_process_attempts": 1, "outcome": outcome})
    return entries


def test_validation_not_reached_budget_exhaustion_campaign_proceeds_v3(manifest_v3, auth_v3, tmp_path, git_state_provider):
    """End-to-end v3: case 5 produces the static-baseline pre-validate
    exhausted shape; the case materializes as VALIDATION_NOT_REACHED and the
    campaign proceeds to case 6."""
    route = _route_evidence(manifest_v3)
    entries = _completed_entries_v3(manifest_v3)
    entries[4] = {
        "provider_process_attempts": 10,
        "outcome": _static_baseline_pre_validate_exhausted_outcome(manifest_v3, manifest_v3["case_order"][4], route),
    }

    record, factory, case_runner, output = _run_campaign_v3(
        manifest_v3, auth_v3, tmp_path,
        case_runner=ScriptedCaseRunner(entries),
        runner_entries=entries,
        git_state_provider=git_state_provider,
    )

    assert record["status"] == "COMPLETED"
    assert record["stop_reason"] is None
    assert record["case_lifecycle_states"][manifest_v3["case_order"][4]["case_id"]] == "completed"
    assert record["counts"]["completed_case_count"] == 6
    assert record["counts"]["unstarted_case_count"] == 0
    assert record["counts"]["aborted_case_count"] == 0
    assert record["counts"]["logical_model_calls"] == 10 + 5
    assert record["counts"]["provider_process_attempts"] == 10 + 5

    fifth = record["cases"][4]
    assert fifth["terminal_status"] == "VALIDATION_NOT_REACHED"
    assert fifth["terminal_reason_code"] == "VALIDATION_NOT_REACHED_PRE_VALIDATE"
    assert fifth["public_evidence_bytes"] == 20000
    assert fifth["patch_submissions"] == 1
    assert fifth["candidate_provenance"] == "applied_patch_event"
    assert fifth["candidate_hash"] == _v3_candidate_hash()
    assert fifth["verifier_runs"] == 0
    assert fifth["repair_outcome"] == "NO_CANDIDATE"
    assert fifth["logical_model_calls"] == 10
    assert fifth["provider_reported_cost"] == pytest.approx(0.0090328)

    assert record["cases"][5]["case_id"] == manifest_v3["case_order"][5]["case_id"]
    assert record["cases"][5]["terminal_status"] == "UNRESOLVED"
    assert (output / "cases" / f"case-05-quixbugs-paired-pilot-v3__quixbugs-is-valid-parenthesization-smoke-v1__static-baseline.json").is_file()


def test_validation_not_reached_budget_exhaustion_attempt_package_verifies_v3(manifest_v3, auth_v3, tmp_path, git_state_provider):
    """After the v3 campaign from the progression test, verify_attempt_package
    returns consistent with all six case files on disk."""
    route = _route_evidence(manifest_v3)
    entries = _completed_entries_v3(manifest_v3)
    entries[4] = {
        "provider_process_attempts": 10,
        "outcome": _static_baseline_pre_validate_exhausted_outcome(manifest_v3, manifest_v3["case_order"][4], route),
    }

    record, factory, case_runner, output = _run_campaign_v3(
        manifest_v3, auth_v3, tmp_path,
        case_runner=ScriptedCaseRunner(entries),
        runner_entries=entries,
        git_state_provider=git_state_provider,
    )

    verification = runner.verify_attempt_package(output, manifest_v3)
    assert verification["consistent"] is True
    assert verification["errors"] == []
    assert verification["case_files_on_disk"] == 6
    assert verification["case_records_referenced"] == 6
    assert verification["terminal_commit"] == "PRESENT"
    assert verification["campaign_status"] == "COMPLETED"


def test_validation_not_reached_with_validate_state_still_aborts(manifest_v3):
    """A shape that reached Validate has no VALIDATION_NOT_REACHED
    representation and returns None (honest abort)."""
    case = manifest_v3["case_order"][4]
    route = _route_evidence(manifest_v3)
    outcome = _static_baseline_pre_validate_exhausted_outcome(
        manifest_v3, case, route,
        controller_states_visited=["Reproduce", "Understand", "Patch", "Validate"],
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest_v3, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, outcome, info.value, run_id="run-validate", manifest=manifest_v3) is None


def test_validation_not_reached_with_verifier_completed_still_aborts(manifest_v3):
    """A shape with a completed verifier has no VALIDATION_NOT_REACHED
    representation; the completed UNRESOLVED/RESOLVED paths own it."""
    case = manifest_v3["case_order"][4]
    route = _route_evidence(manifest_v3)
    outcome = _static_baseline_pre_validate_exhausted_outcome(
        manifest_v3, case, route,
        verifier_runs=1,
        independent_verifier_result={"status": "COMPLETED", "outcome": "NO_OP", "lifecycle_succeeded": True},
        candidate_provenance="verifier_record",
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest_v3, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, outcome, info.value, run_id="run-verifier", manifest=manifest_v3) is None


def test_validation_not_reached_requires_v3_campaign():
    """The VALIDATION_NOT_REACHED rewrite is v3-only: the same shape under a
    v2 manifest returns None (the frozen v2 contract does not admit it)."""
    manifest = pilot.load_manifest(pilot.MANIFEST_PATH_V2)
    case = manifest["case_order"][4]
    route = _route_evidence(manifest)
    outcome = _static_baseline_pre_validate_exhausted_outcome(manifest, case, route)
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, outcome, info.value, run_id="run-v2", manifest=manifest) is None


def test_v2_static_baseline_pdb_not_reached_still_rejected():
    """The frozen v2 validator still rejects a static-baseline PDB_NOT_REACHED
    record (the v2 terminal contract is unchanged)."""
    manifest = pilot.load_manifest(pilot.MANIFEST_PATH_V2)
    case = manifest["case_order"][1]
    record = _static_baseline_pre_validate_exhausted_outcome(manifest, case, _route_evidence(manifest))
    record.update({
        "terminal_status": "PDB_NOT_REACHED",
        "terminal_reason_code": "PDB_NOT_REACHED_NO_GATE",
        "candidate_provenance": None,
        "patch_submissions": 0,
        "candidate_hash": None,
        "verifier_runs": 0,
        "repair_outcome": "NO_CANDIDATE",
        "case_id": case["case_id"],
        "order_index": case["order_index"],
        "task_id": case["task_id"],
        "policy": case["policy"],
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "campaign_commit": "a" * 40,
        "accepted_code_commit": "a" * 40,
        "execution_kind": "LIVE_CASE",
        "qualification_contract_hash": manifest["qualification_contract_hash"],
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "source_revision": manifest["authority"]["revision"],
        "provider": manifest["route"]["provider"],
        "model": manifest["route"]["model"],
        "variant": manifest["route"]["variant"],
        "route_observation": _route_evidence(manifest),
    })
    record["route_observation"].update({"opencode_version": "1.0.0", "active_model_status": "ACTIVE", "variant_available": True, "catalog_fingerprint": "c" * 64, "preflight_success": True})
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(record, manifest)

# ---- v4 verifier-authoritative terminal matrix (attempt fddf1e39...) ---------
#
# Regressions for the v4 campaign: the live-proven shape of attempt
# ``quixbugs-paired-pilot-v3-attempt-fddf1e39b73cda5f430d8e69c6e442b558143a63d013229e54efd9cbb585fbac``
# case 1 (``quixbugs-find-in-sorted-smoke-v1 / pdb-on-uncertainty``): twelve
# logical model calls, thirteen provider process attempts, one bounded retry
# (attempt 10 produced a no_text_event stream and was retried), twelve valid
# directives, baseline reproduction, one applied candidate, Validate visited,
# the independent verifier executed, zero PDB observations, and 33,685
# cumulative public evidence bytes observed after the completed lifecycle.
# Under the frozen v3 contract this shape has no terminal representation and
# the campaign aborted honestly; v4 preregisters the verifier-authoritative
# classification and the budget-terminal matrix that materializes it.


@pytest.fixture
def manifest_v4():
    return pilot.load_manifest(pilot.MANIFEST_PATH_V4)


@pytest.fixture
def auth_v4(manifest_v4, tmp_path):
    return _valid_authorization(
        manifest_v4, tmp_path / "attempt-out",
        campaign_id=manifest_v4["campaign_id"],
        campaign_version=manifest_v4["campaign_version"],
        campaign_attempt_identity="quixbugs-paired-pilot-v4-attempt-" + "d" * 64,
    )


def _v4_candidate_hash():
    return hashlib.sha256(
        pilot.canonical_json({"patch": _IS_VALID_PARENTHESIZATION_PATCH}).encode("utf-8")
    ).hexdigest()


def _completed_post_apply_exhausted_outcome(manifest, case, route, *, verifier_outcome="RESOLVED", **overrides):
    """The exact fddf1e39... case-1 shape restamped for the campaign version.

    Twelve logical calls, thirteen provider process attempts, one bounded
    retry, twelve valid directives, an applied candidate (verifier_record
    provenance because the verifier executed), Validate visited, zero PDB
    activity, and 33,685 cumulative public evidence bytes.  The terminal is
    the verifier-authoritative RESOLVED (or UNRESOLVED) outcome the adapter
    produces under the v4 classification.
    """
    source_hash = next(item["source_sha256"] for item in manifest["inventory"] if item["task_id"] == case["task_id"])
    resolved = verifier_outcome == "RESOLVED"
    outcome = {
        "terminal_status": "RESOLVED" if resolved else "UNRESOLVED",
        "terminal_reason_code": "RESOLVED_COMPLETED" if resolved else "UNRESOLVED_COMPLETED",
        "termination_reason": "opencode-go adapter: RESOLVED: completed" if resolved else "opencode-go adapter: UNRESOLVED: completed",
        "logical_model_calls": 12,
        "provider_process_attempts": 13,
        "retries": 1,
        "valid_directives": 12,
        "malformed_directive_rejections": 0,
        "bounded_directive_feedback_events": 0,
        "baseline_reproduction": True,
        "controller_states_visited": ["Reproduce", "Understand", "Patch", "Validate", "Done"],
        "hypotheses_created": 1,
        "pdb_gate_decisions": [],
        "pdb_counts": dict(runner.ZERO_PDB_COUNTS),
        "pdb_sessions_started": 0,
        "successful_pdb_observations": 0,
        "failed_pdb_observations": 0,
        "verifier_runs": 1,
        "patch_submissions": 1,
        "candidate_provenance": "verifier_record",
        "independent_verifier_result": {
            "status": "COMPLETED", "outcome": verifier_outcome, "lifecycle_succeeded": True,
        },
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "COMPLETED_RESPONSE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True,
            "evidence_reference": f"opencode-go:{case['case_id']}:13",
        },
        "blocked_evidence": {"block_kind": "none", "reason_code": "NONE", "confirmed": False, "evidence_reference": "v4-synthetic:none"},
        "infrastructure_evidence": {
            "stage": "none", "reason_code": "NONE", "confirmed_failure": False, "classification": "NONE",
            "terminal_classification": "NOT_APPLICABLE", "provider_attempt_index": None,
            "prior_lifecycle_completed": False, "source_mutation_observed": False,
            "expected_source_hash": None, "evidence_reference": "v4-synthetic:none",
        },
        "preflight_failure_evidence": {field: None for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS},
        "campaign_stop_evidence": {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS},
        "prompt_tokens": 79990,
        "completion_tokens": 613,
        "reasoning_tokens": 0,
        "provider_reported_cost": 0.010565556,
        "wall_clock_duration_seconds": 226.613,
        "public_evidence_bytes": 33685,
        "canonical_source_restoration": True,
        "owned_workspace_cleanup": True,
        "evidence_consistency": True,
        "public_request_hash": hashlib.sha256(b"v4-synthetic-events").hexdigest(),
        "source_hash": source_hash,
        "candidate_hash": _v4_candidate_hash(),
        "repair_outcome": "RESOLVED" if resolved else "NO_CANDIDATE",
        "resource_ids": {},
    }
    outcome.update(overrides)
    return outcome


def test_v4_manifest_and_authorization_validate(manifest_v4, auth_v4):
    """The v4 manifest validates with the result-v4 schema and its hash is
    frozen; the v4 authorization binds to the v4 manifest."""
    assert manifest_v4["campaign_id"] == "quixbugs-paired-pilot-v4"
    assert manifest_v4["campaign_version"] == 4
    assert manifest_v4["outcome_schema"]["schema_version"] == "quixbugs-paired-pilot-result-v4"
    assert "VALIDATION_NOT_REACHED" in manifest_v4["outcome_schema"]["terminal_statuses"]
    assert "verifier_authoritative_classification" in manifest_v4["outcome_schema"]["terminal_status_rules"]
    assert pilot.validate_manifest(manifest_v4) == "020dfc1f7b8f23aa96a4d7c7942429e306cc290906abfed5ce96cde22b90354d"
    assert manifest_v4["qualification_contract_hash"] == "7246d289fcc689e93d93385751cbae5fa75a3c52e3c04e001f2c977a1990c52d"
    assert manifest_v4["budgets"]["max_public_evidence_bytes"] == 20000
    assert auth_v4["campaign_id"] == manifest_v4["campaign_id"]
    assert auth_v4["campaign_version"] == manifest_v4["campaign_version"]


def test_v4_completed_post_apply_budget_exhaustion_terminalizes_resolved(manifest_v4):
    """The exact observed shape raises PublicEvidenceBudgetExhausted and
    rewrites to the verifier-authoritative RESOLVED terminal with every
    completed accounting field preserved and the exact 33,685 byte count in
    the termination detail."""
    case = manifest_v4["case_order"][0]
    route = _route_evidence(manifest_v4)
    outcome = _completed_post_apply_exhausted_outcome(manifest_v4, case, route)

    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest_v4, case_policy=case["policy"])
    assert info.value.observed == 33685
    assert info.value.limit == 20000

    rewritten = runner._budget_exhausted_outcome(
        case, outcome, info.value, run_id="run-fddf1e39", manifest=manifest_v4,
    )
    assert rewritten is not None
    assert rewritten["terminal_status"] == "RESOLVED"
    assert rewritten["terminal_reason_code"] == "RESOLVED_COMPLETED"
    assert rewritten["public_evidence_bytes"] == 20000
    assert "33685" in rewritten["termination_reason"] and "20000" in rewritten["termination_reason"]
    assert rewritten["logical_model_calls"] == 12
    assert rewritten["provider_process_attempts"] == 13
    assert rewritten["retries"] == 1
    assert rewritten["valid_directives"] == 12
    assert rewritten["patch_submissions"] == 1
    assert rewritten["candidate_provenance"] == "verifier_record"
    assert rewritten["verifier_runs"] == 1
    assert rewritten["independent_verifier_result"]["outcome"] == "RESOLVED"
    assert rewritten["controller_states_visited"] == ["Reproduce", "Understand", "Patch", "Validate", "Done"]
    assert rewritten["prompt_tokens"] == 79990
    assert rewritten["completion_tokens"] == 613
    assert rewritten["provider_reported_cost"] == pytest.approx(0.010565556)
    assert rewritten["wall_clock_duration_seconds"] == pytest.approx(226.613)
    assert rewritten["pdb_counts"] == dict(runner.ZERO_PDB_COUNTS)
    assert rewritten["repair_outcome"] == "RESOLVED"

    runner.enforce_case_budgets(rewritten, manifest_v4, case_policy=case["policy"])


def test_v4_completed_post_apply_budget_exhaustion_terminalizes_unresolved(manifest_v4):
    """The same completed post-apply shape with a non-resolved verifier
    rewrites to the verifier-authoritative UNRESOLVED terminal with the
    accounting and provenance preserved."""
    case = manifest_v4["case_order"][0]
    route = _route_evidence(manifest_v4)
    outcome = _completed_post_apply_exhausted_outcome(
        manifest_v4, case, route, verifier_outcome="NO_OP",
    )

    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest_v4, case_policy=case["policy"])

    rewritten = runner._budget_exhausted_outcome(
        case, outcome, info.value, run_id="run-fddf1e39-unresolved", manifest=manifest_v4,
    )
    assert rewritten is not None
    assert rewritten["terminal_status"] == "UNRESOLVED"
    assert rewritten["terminal_reason_code"] == "UNRESOLVED_COMPLETED"
    assert rewritten["public_evidence_bytes"] == 20000
    assert "33685" in rewritten["termination_reason"]
    assert rewritten["logical_model_calls"] == 12
    assert rewritten["provider_process_attempts"] == 13
    assert rewritten["retries"] == 1
    assert rewritten["valid_directives"] == 12
    assert rewritten["patch_submissions"] == 1
    assert rewritten["candidate_provenance"] == "verifier_record"
    assert rewritten["independent_verifier_result"]["outcome"] == "NO_OP"
    assert rewritten["repair_outcome"] == "NO_CANDIDATE"


def test_v4_validation_not_reached_pdb_policy_with_validate_visited_terminalizes(manifest_v4):
    """v4 admits a pdb-on-uncertainty VALIDATION_NOT_REACHED applied-candidate
    shape that stopped in Validate before the verifier (an in-Validate
    per-request budget stop)."""
    case = manifest_v4["case_order"][0]
    route = _route_evidence(manifest_v4)
    outcome = _completed_post_apply_exhausted_outcome(
        manifest_v4, case, route,
        terminal_status="VALIDATION_NOT_REACHED",
        terminal_reason_code="VALIDATION_NOT_REACHED_PRE_VALIDATE",
        termination_reason="opencode-go adapter: VALIDATION_NOT_REACHED: public_evidence_budget_exceeded",
        verifier_runs=0,
        candidate_provenance="applied_patch_event",
        independent_verifier_result={"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        controller_states_visited=["Reproduce", "Understand", "Patch", "Validate"],
        repair_outcome="NO_CANDIDATE",
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest_v4, case_policy=case["policy"])

    rewritten = runner._budget_exhausted_outcome(
        case, outcome, info.value, run_id="run-v4-in-validate", manifest=manifest_v4,
    )
    assert rewritten is not None
    assert rewritten["terminal_status"] == "VALIDATION_NOT_REACHED"
    assert rewritten["terminal_reason_code"] == "VALIDATION_NOT_REACHED_PRE_VALIDATE"
    assert rewritten["public_evidence_bytes"] == 20000
    assert rewritten["patch_submissions"] == 1
    assert rewritten["candidate_provenance"] == "applied_patch_event"
    assert rewritten["verifier_runs"] == 0
    assert rewritten["controller_states_visited"] == ["Reproduce", "Understand", "Patch", "Validate"]

    runner.enforce_case_budgets(rewritten, manifest_v4, case_policy=case["policy"])


def test_v3_in_validate_pdb_policy_shape_still_aborts(manifest_v3):
    """The v3 frozen contract keeps rejecting the pdb-on-uncertainty
    Validate-visited applied-candidate shape: the same raw outcome under the
    v3 manifest returns None (honest abort)."""
    case = manifest_v3["case_order"][0]
    route = _route_evidence(manifest_v3)
    outcome = _completed_post_apply_exhausted_outcome(
        manifest_v3, case, route,
        terminal_status="VALIDATION_NOT_REACHED",
        terminal_reason_code="VALIDATION_NOT_REACHED_PRE_VALIDATE",
        termination_reason="opencode-go adapter: VALIDATION_NOT_REACHED: public_evidence_budget_exceeded",
        verifier_runs=0,
        candidate_provenance="applied_patch_event",
        independent_verifier_result={"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        controller_states_visited=["Reproduce", "Understand", "Patch", "Validate"],
        repair_outcome="NO_CANDIDATE",
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest_v3, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, outcome, info.value, run_id="run-v3-in-validate", manifest=manifest_v3) is None


def test_v3_pdb_not_reached_with_patch_and_verifier_still_aborts(manifest_v3):
    """The exact v3-classified raw shape (PDB_NOT_REACHED terminal masking an
    executed verifier on a patched pdb-on-uncertainty case) has no frozen v3
    terminal representation and returns None."""
    case = manifest_v3["case_order"][0]
    route = _route_evidence(manifest_v3)
    outcome = _completed_post_apply_exhausted_outcome(
        manifest_v3, case, route,
        terminal_status="PDB_NOT_REACHED",
        terminal_reason_code="PDB_NOT_REACHED_NO_GATE",
        termination_reason="opencode-go adapter: PDB_NOT_REACHED: completed",
        repair_outcome="NO_CANDIDATE",
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest_v3, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, outcome, info.value, run_id="run-v3-fddf", manifest=manifest_v3) is None


def test_v4_post_contact_infrastructure_budget_exhaustion_terminalizes(manifest_v4):
    """v4 terminalizes a post-contact controller-stage infrastructure outcome
    with the raw terminal preserved and the public counter clamped."""
    case = manifest_v4["case_order"][0]
    route = _route_evidence(manifest_v4)
    outcome = _completed_post_apply_exhausted_outcome(
        manifest_v4, case, route,
        terminal_status="INFRASTRUCTURE_ERROR",
        terminal_reason_code="INFRASTRUCTURE_FAILURE",
        termination_reason="opencode-go adapter: CONTROLLER_FAILED: controller stopped",
        verifier_runs=0,
        candidate_provenance="applied_patch_event",
        independent_verifier_result={"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        controller_states_visited=["Reproduce", "Understand", "Patch", "Validate"],
        repair_outcome="NO_CANDIDATE",
        infrastructure_evidence={
            "stage": "controller", "reason_code": "CONTROLLER_FAILURE", "confirmed_failure": True,
            "classification": "CONTROLLER", "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "provider_attempt_index": None, "prior_lifecycle_completed": True,
            "source_mutation_observed": False, "expected_source_hash": None,
            "evidence_reference": "opencode-go-controller-stop",
        },
        terminal_transport_evidence={
            "final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": 0, "timed_out": False,
            "provider_error_category": None, "provider_completed_response": True,
            "evidence_reference": f"opencode-go:{case['case_id']}:13",
        },
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest_v4, case_policy=case["policy"])

    rewritten = runner._budget_exhausted_outcome(
        case, outcome, info.value, run_id="run-v4-infra", manifest=manifest_v4,
    )
    assert rewritten is not None
    assert rewritten["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert rewritten["terminal_reason_code"] == "INFRASTRUCTURE_FAILURE"
    assert rewritten["public_evidence_bytes"] == 20000
    assert "33685" in rewritten["termination_reason"]
    assert rewritten["infrastructure_evidence"]["stage"] == "controller"
    assert rewritten["infrastructure_evidence"]["prior_lifecycle_completed"] is True
    assert rewritten["logical_model_calls"] == 12
    assert rewritten["provider_process_attempts"] == 13
    assert rewritten["retries"] == 1

    runner.enforce_case_budgets(rewritten, manifest_v4, case_policy=case["policy"])


def test_v4_unsupported_shapes_still_abort(manifest_v4):
    """Contradictory or unsupported v4 shapes stay fail-closed: a completed
    RESOLVED shape with PDB activity, a verifier-stage infrastructure shape,
    and a VALIDATION_NOT_REACHED shape with a verifier record all return
    None."""
    case = manifest_v4["case_order"][0]
    route = _route_evidence(manifest_v4)

    with_pdb = _completed_post_apply_exhausted_outcome(
        manifest_v4, case, route,
        pdb_counts={"total_gate_decisions": 1, "allowed_gate_openings": 1, "rejected_gate_decisions": 0,
                    "sessions_started": 1, "successful_observations": 1, "failed_observations": 0},
        pdb_gate_decisions=[{"source_state": "Understand", "failure_reproduced": True, "remaining_pdb_observations": 2,
                             "failed_patch_attempts": 0, "active_hypothesis_id": "h-1", "allowed": True, "reason": "ok"}],
        pdb_sessions_started=1,
        successful_pdb_observations=1,
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(with_pdb, manifest_v4, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, with_pdb, info.value, run_id="run-v4-pdb", manifest=manifest_v4) is None

    verifier_infra = _completed_post_apply_exhausted_outcome(
        manifest_v4, case, route,
        terminal_status="INFRASTRUCTURE_ERROR",
        terminal_reason_code="INFRASTRUCTURE_FAILURE",
        termination_reason="opencode-go adapter: VERIFIER_FAILED: verifier raised",
        independent_verifier_result={"status": None, "outcome": None, "lifecycle_succeeded": False},
        infrastructure_evidence={
            "stage": "verifier", "reason_code": "VERIFIER_FAILURE", "confirmed_failure": True,
            "classification": "VERIFIER", "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "provider_attempt_index": None, "prior_lifecycle_completed": True,
            "source_mutation_observed": False, "expected_source_hash": None,
            "evidence_reference": "opencode-go-verifier-stop",
        },
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(verifier_infra, manifest_v4, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, verifier_infra, info.value, run_id="run-v4-vinfra", manifest=manifest_v4) is None

    with_verifier = _completed_post_apply_exhausted_outcome(
        manifest_v4, case, route,
        terminal_status="VALIDATION_NOT_REACHED",
        terminal_reason_code="VALIDATION_NOT_REACHED_PRE_VALIDATE",
        termination_reason="opencode-go adapter: VALIDATION_NOT_REACHED: public_evidence_budget_exceeded",
        candidate_provenance="verifier_record",
        repair_outcome="NO_CANDIDATE",
    )
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(with_verifier, manifest_v4, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, with_verifier, info.value, run_id="run-v4-vnr", manifest=manifest_v4) is None


def _completed_entries_v4(manifest):
    route = _route_evidence(manifest)
    entries = []
    for case in manifest["case_order"]:
        outcome = _completed_outcome(manifest, case, route)
        outcome["candidate_provenance"] = "verifier_record" if outcome.get("patch_submissions", 0) > 0 else None
        entries.append({"provider_process_attempts": 1, "outcome": outcome})
    return entries


def test_v4_budget_exhaustion_campaign_proceeds(manifest_v4, auth_v4, tmp_path, git_state_provider):
    """End-to-end v4: case 1 produces the observed completed post-apply
    exhausted shape; the case materializes as RESOLVED with all accounting
    preserved and the campaign proceeds to the remaining five cases."""
    route = _route_evidence(manifest_v4)
    entries = _completed_entries_v4(manifest_v4)
    entries[0] = {
        "provider_process_attempts": 13,
        "outcome": _completed_post_apply_exhausted_outcome(manifest_v4, manifest_v4["case_order"][0], route),
    }

    record, factory, case_runner, output = _run_campaign_custom(
        manifest_v4, auth_v4, tmp_path,
        case_runner=ScriptedCaseRunner(entries),
        runner_entries=entries,
        git_state_provider=git_state_provider,
    )

    assert record["status"] == "COMPLETED"
    assert record["stop_reason"] is None
    assert record["case_lifecycle_states"][manifest_v4["case_order"][0]["case_id"]] == "completed"
    assert record["counts"]["completed_case_count"] == 6
    assert record["counts"]["unstarted_case_count"] == 0
    assert record["counts"]["aborted_case_count"] == 0
    assert record["counts"]["logical_model_calls"] == 12 + 5
    assert record["counts"]["provider_process_attempts"] == 13 + 5
    assert record["counts"]["transport_retries"] == 1
    assert record["counts"]["accepted_directives"] == 12 + 5

    first = record["cases"][0]
    assert first["terminal_status"] == "RESOLVED"
    assert first["terminal_reason_code"] == "RESOLVED_COMPLETED"
    assert first["public_evidence_bytes"] == 20000
    assert "33685" in first["termination_reason"]
    assert first["logical_model_calls"] == 12
    assert first["provider_process_attempts"] == 13
    assert first["retries"] == 1
    assert first["valid_directives"] == 12
    assert first["patch_submissions"] == 1
    assert first["candidate_provenance"] == "verifier_record"
    assert first["verifier_runs"] == 1
    assert first["independent_verifier_result"]["outcome"] == "RESOLVED"
    assert first["repair_outcome"] == "RESOLVED"

    verification = runner.verify_attempt_package(output, manifest_v4)
    assert verification["consistent"] is True
    assert verification["errors"] == []


def test_v4_verifier_authoritative_classification(manifest_v4, tmp_path):
    """The v4 classifier maps a completed pdb-on-uncertainty case with an
    executed verifier to the verifier semantic outcome, while the same case
    under campaign version 3 keeps the frozen PDB_NOT_REACHED classification."""
    from types import SimpleNamespace

    task = _curated_task()
    registry = ToolRegistry((
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
        ToolSpec(
            ActionName.APPLY_PATCH,
            lambda arguments: dict(arguments),
            lambda _action, _arguments: ToolResult(ObservationStatus.OK, {"applied": True, "patch_sha256": "c" * 64}, "ok"),
            argument_contract={
                "required": ["patch"],
                "properties": {"patch": {"type": "string", "min_length": 1}},
                "additional_properties": False,
            },
        ),
    ))
    patch = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-bug\n+fix\n"

    class _CompletedTransport:
        def __init__(self):
            self.calls = 0
            self.directives = [
                {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
                {"kind": "transition", "target_state": "Understand", "reason": "reproduced"},
                {"kind": "transition", "target_state": "Patch", "reason": "hypothesis ready"},
                {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch}},
                {"kind": "transition", "target_state": "Validate", "reason": "patched"},
                {"kind": "transition", "target_state": "Done", "reason": "verified"},
            ]

        def request(self, payload, timeout_seconds):
            directive = self.directives[min(self.calls, len(self.directives) - 1)]
            self.calls += 1
            return {"directive": directive, "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

    transport = _CompletedTransport()
    live_adapter = LiveModelAdapter(
        task=task, policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=LiveModelConfig("test-model", ("test-command",)),
        transport=transport, limits=LiveRunLimits(max_model_requests=12, max_controller_steps=12),
        registry=registry, evaluation_id="e", case_id="c", run_id="r", trajectory_id="r",
    )
    controller = DeterministicController(registry, live_adapter, ControllerRunConfig(max_model_calls=12))
    result = controller.run(ControllerSnapshot(
        "r", task.task_id, ControllerState.REPRODUCE, 0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(), HypothesisLedger(),
    ))
    assert result.final_state is ControllerState.DONE
    context = SimpleNamespace(
        patch_applied=True, candidate_patch=patch,
        declared_localization=None, patch_changed_files=[], tool_calls=[], release_pdb=lambda: [],
    )
    verifier = SimpleNamespace(
        status=SimpleNamespace(value="COMPLETED"),
        outcome=SimpleNamespace(value="RESOLVED"),
        baseline=SimpleNamespace(valid=True),
        patch_application=SimpleNamespace(to_mapping=lambda: {"patch_sha256": "c" * 64}),
        f2p_passed=1, f2p_total=1, p2p_passed=1, p2p_total=1,
        workspace=SimpleNamespace(cleaned=True, canonical_fixture_unchanged=True),
    )
    common = dict(
        task_id=task.task_id, policy=DemoPolicy.PDB_ON_UNCERTAINTY, repetition=1,
        case_id="c", run_id="r", config=LiveModelConfig("test-model", ("test-command",)),
        task=task, context=context, workspace=None, result=result, metrics=live_adapter.metrics,
        live_adapter=live_adapter, started=time.monotonic() - 1.0,
        interrupted=False, controller_failed=False, diagnostics=[],
        extra_cleanup=lambda: (True, None), extra_cleanup_owned=False,
        evidence={"pdb_gate_decisions": [], "directive_rejections": []},
    )
    v3_finalized = _finalize_live_case(**common, verify=lambda: verifier)
    assert v3_finalized.status is LiveCaseStatus.PDB_NOT_REACHED
    v4_finalized = _finalize_live_case(**common, campaign_version=4, verify=lambda: verifier)
    assert v4_finalized.status is LiveCaseStatus.RESOLVED
    v4_unresolved = _finalize_live_case(**common, campaign_version=4, verify=lambda: SimpleNamespace(
        status=SimpleNamespace(value="COMPLETED"),
        outcome=SimpleNamespace(value="NO_OP"),
        baseline=SimpleNamespace(valid=True),
        patch_application=SimpleNamespace(to_mapping=lambda: {"patch_sha256": "c" * 64}),
        f2p_passed=0, f2p_total=1, p2p_passed=1, p2p_total=1,
        workspace=SimpleNamespace(cleaned=True, canonical_fixture_unchanged=True),
    ))
    assert v4_unresolved.status is LiveCaseStatus.UNRESOLVED


def test_public_evidence_bytes_are_events_jsonl_only(tmp_path, manifest_v4, synthetic_executable):
    """public_evidence_bytes counts only the projected events log; provider
    usage, cost, and request material never enter the public counter."""
    harness = _harness(tmp_path, manifest_v4, synthetic_executable)
    case = manifest_v4["case_order"][0]
    events_jsonl = _events_jsonl_of_bytes(33685)
    mapping = _live_mapping(manifest_v4, case, **{
        "status": "RESOLVED",
        "measurements": {
            "model_request_count": 13, "model_response_count": 12, "retry_count": 1,
            "provider_error_count": 1, "provider_error_kinds": ["process_error"],
            "token_usage": {"prompt_tokens": 79990, "completion_tokens": 613, "total_tokens": 80603,
                            "provider_reported": True, "missing_fields": []},
            "termination_reason": "completed",
            "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
            "tool_call_count": 12, "case_elapsed_duration_ms": 226613,
            "model_phase_elapsed_duration_ms": 220000, "model_transport_duration_ms": 220000,
            "elapsed_scope": "case_observed; model_phase=transport_only",
        },
        "events_jsonl": events_jsonl,
        "evidence": {"pdb_gate_decisions": [], "directive_rejections": []},
    })
    inner = harness["factory"].prepare(case)
    inner.process_attempts = 13
    inner.last_process_exit_code = 0
    inner.reported_costs.extend([0.0008] * 12)
    outcome = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=inner.process_attempts, policy_value=case["policy"], run_id="run-replay",
        source_hash="0" * 64,
    )
    assert outcome["public_evidence_bytes"] == 33685
    assert outcome["logical_model_calls"] == 12
    assert outcome["provider_process_attempts"] == 13
    assert outcome["retries"] == 1
    assert outcome["valid_directives"] == 12
    assert outcome["provider_cost_report_count"] == 12
    assert outcome["prompt_tokens"] == 79990


# ---- adapter: events-log candidate derivation (action_id correlation) ---------


def _events_line(event_type, name, state, payload):
    return json.dumps({"event_type": event_type, "name": name, "state": state, "payload": payload}, ensure_ascii=False)


def _apply_patch_events_jsonl(*, applied=True, with_observation=True, revert=None):
    """Build an events log with an apply_patch action/observation pair and an
    optional revert action/observation.  ``revert`` is None (no revert),
    ``"ok"`` (matching observation with reverted=true), ``"no-observation"``
    (revert action without a matching observation), or ``"reverted-false"``
    (matching observation with reverted=false)."""
    patch_sha = hashlib.sha256(_IS_VALID_PARENTHESIZATION_PATCH.encode("utf-8")).hexdigest()
    lines = [
        _events_line("action", "apply_patch", "Patch", {"action": {"action_id": "action-000000009", "name": "apply_patch", "arguments": {"patch": _IS_VALID_PARENTHESIZATION_PATCH}}}),
    ]
    if with_observation:
        lines.append(_events_line("observation", "apply_patch", "Patch", {"observation": {"action_id": "action-000000009", "name": "apply_patch", "status": "ok", "payload": {"applied": applied, "changed_files": ["python_programs/is_valid_parenthesization.py"], "hunk_count": 1, "patch_sha256": patch_sha}}}))
    if revert == "ok":
        lines.append(_events_line("action", "revert_patch", "Patch", {"action": {"action_id": "action-000000010", "name": "revert_patch", "arguments": {}}}))
        lines.append(_events_line("observation", "revert_patch", "Patch", {"observation": {"action_id": "action-000000010", "name": "revert_patch", "status": "ok", "payload": {"reverted": True}}}))
    elif revert == "no-observation":
        lines.append(_events_line("action", "revert_patch", "Patch", {"action": {"action_id": "action-000000010", "name": "revert_patch", "arguments": {}}}))
    elif revert == "reverted-false":
        lines.append(_events_line("action", "revert_patch", "Patch", {"action": {"action_id": "action-000000010", "name": "revert_patch", "arguments": {}}}))
        lines.append(_events_line("observation", "revert_patch", "Patch", {"observation": {"action_id": "action-000000010", "name": "revert_patch", "status": "ok", "payload": {"reverted": False}}}))
    return "\n".join(lines) + "\n"


def test_applied_patch_from_events_derives_candidate():
    """The action_id-correlated apply_patch pair yields the candidate diff and
    sha256."""
    result = adapter._applied_patch_from_events(_apply_patch_events_jsonl())
    assert result is not None
    diff, patch_sha = result
    assert diff == _IS_VALID_PARENTHESIZATION_PATCH
    assert patch_sha == hashlib.sha256(_IS_VALID_PARENTHESIZATION_PATCH.encode("utf-8")).hexdigest()


def test_applied_patch_from_events_apply_without_observation_yields_none():
    assert adapter._applied_patch_from_events(_apply_patch_events_jsonl(with_observation=False)) is None


def test_applied_patch_from_events_apply_applied_false_yields_none():
    assert adapter._applied_patch_from_events(_apply_patch_events_jsonl(applied=False)) is None


def test_applied_patch_from_events_no_apply_action_yields_none():
    assert adapter._applied_patch_from_events(_events_line("final", "final", "Patch", {"final_state": "Patch"}) + "\n") is None


def test_applied_patch_from_events_revert_with_matching_observation_invalidates():
    """A revert_patch whose matching observation confirms reverted=true
    invalidates the candidate."""
    assert adapter._applied_patch_from_events(_apply_patch_events_jsonl(revert="ok")) is None


def test_applied_patch_from_events_revert_without_matching_observation_keeps_candidate():
    """A revert action without a matching observation does not invalidate the
    candidate (the revert did not complete)."""
    result = adapter._applied_patch_from_events(_apply_patch_events_jsonl(revert="no-observation"))
    assert result is not None
    assert result[0] == _IS_VALID_PARENTHESIZATION_PATCH


def test_applied_patch_from_events_revert_reverted_false_keeps_candidate():
    """A revert observation with reverted=false does not invalidate the
    candidate."""
    result = adapter._applied_patch_from_events(_apply_patch_events_jsonl(revert="reverted-false"))
    assert result is not None
    assert result[0] == _IS_VALID_PARENTHESIZATION_PATCH


def test_outcome_derives_candidate_from_events_when_verifier_never_ran(tmp_path, manifest_v3, synthetic_executable):
    """The adapter outcome derives patch_submissions/candidate_hash/
    candidate_provenance from the events log when the verifier never ran."""
    harness = _harness(tmp_path, manifest_v3, synthetic_executable)
    case = manifest_v3["case_order"][4]
    source_hash = next(item["source_sha256"] for item in manifest_v3["inventory"] if item["task_id"] == case["task_id"])
    mapping = _live_mapping(manifest_v3, case, **{
        "status": "VALIDATION_NOT_REACHED",
        "controller": {"completed": False, "final_state": "Patch", "stop_reason": "model_error", "model_calls": 10, "exception": False},
        "verifier": {"executed": False, "failure": False, "status": None, "outcome": None, "patch_application": None, "localization": {"outcome": "NO_LOCALIZATION"}},
        "measurements": {
            "model_request_count": 10, "model_response_count": 10, "retry_count": 0,
            "provider_error_count": 0, "provider_error_kinds": [],
            "token_usage": {"prompt_tokens": 45784, "completion_tokens": 705, "total_tokens": 46489,
                            "provider_reported": True, "missing_fields": []},
            "termination_reason": "public_evidence_budget_exceeded",
            "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
            "tool_call_count": 10, "case_elapsed_duration_ms": 169702,
            "model_phase_elapsed_duration_ms": 169000, "model_transport_duration_ms": 169000,
            "elapsed_scope": "case_observed; model_phase=transport_only",
        },
        "events_jsonl": _static_baseline_pre_validate_events_jsonl(total_bytes=34704),
        "evidence": {"pdb_gate_decisions": [], "directive_rejections": []},
    })
    inner = harness["factory"].prepare(case)
    inner.process_attempts = 10
    outcome = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=inner.process_attempts, policy_value=case["policy"], run_id="run-e974af4-v3",
        source_hash=source_hash,
    )
    assert outcome["terminal_status"] == "VALIDATION_NOT_REACHED"
    assert outcome["terminal_reason_code"] == "VALIDATION_NOT_REACHED_PRE_VALIDATE"
    assert outcome["patch_submissions"] == 1
    assert outcome["candidate_provenance"] == "applied_patch_event"
    assert outcome["candidate_hash"] == _v3_candidate_hash()
    assert outcome["verifier_runs"] == 0
    assert outcome["repair_outcome"] == "NO_CANDIDATE"
    assert outcome["logical_model_calls"] == 10
    assert outcome["public_evidence_bytes"] == 34704


def test_outcome_events_revert_invalidates_candidate_when_verifier_never_ran(tmp_path, manifest_v3, synthetic_executable):
    """When the events log shows a completed revert, the adapter reports no
    candidate (patch_submissions == 0, candidate_hash is None)."""
    harness = _harness(tmp_path, manifest_v3, synthetic_executable)
    case = manifest_v3["case_order"][4]
    source_hash = next(item["source_sha256"] for item in manifest_v3["inventory"] if item["task_id"] == case["task_id"])
    events = _apply_patch_events_jsonl(revert="ok")
    mapping = _live_mapping(manifest_v3, case, **{
        "status": "VALIDATION_NOT_REACHED",
        "controller": {"completed": False, "final_state": "Patch", "stop_reason": "model_error", "model_calls": 10, "exception": False},
        "verifier": {"executed": False, "failure": False, "status": None, "outcome": None, "patch_application": None, "localization": {"outcome": "NO_LOCALIZATION"}},
        "measurements": {
            "model_request_count": 10, "model_response_count": 10, "retry_count": 0,
            "provider_error_count": 0, "provider_error_kinds": [],
            "token_usage": {"prompt_tokens": 45784, "completion_tokens": 705, "total_tokens": 46489,
                            "provider_reported": True, "missing_fields": []},
            "termination_reason": "public_evidence_budget_exceeded",
            "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
            "tool_call_count": 10, "case_elapsed_duration_ms": 169702,
            "model_phase_elapsed_duration_ms": 169000, "model_transport_duration_ms": 169000,
            "elapsed_scope": "case_observed; model_phase=transport_only",
        },
        "events_jsonl": events,
        "evidence": {"pdb_gate_decisions": [], "directive_rejections": []},
    })
    inner = harness["factory"].prepare(case)
    inner.process_attempts = 10
    outcome = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), harness["observed"], inner,
        transport_attempts=inner.process_attempts, policy_value=case["policy"], run_id="run-e974af4-v3",
        source_hash=source_hash,
    )
    assert outcome["patch_submissions"] == 0
    assert outcome["candidate_hash"] is None
    assert outcome["candidate_provenance"] is None

# ---- v3 candidate-provenance lifecycle enforcement (negative regressions) ------


def _v3_result_record(manifest, case, outcome_overrides):
    """Build a minimal v3 LIVE_CASE record from an outcome for direct
    validate_case_result testing."""
    route = _route_evidence(manifest)
    outcome = _completed_outcome(manifest, case, route)
    outcome.update(outcome_overrides)
    record = pilot.public_case_record(manifest, case)
    record.update({
        "execution_kind": "LIVE_CASE",
        "campaign_commit": "a" * 40,
        "accepted_code_commit": "a" * 40,
        "execution_commit": "a" * 40,
        "provider": route["provider"],
        "model": route["model"],
        "variant": route["variant"],
        "route_observation": route,
        "resource_ids": {},
    })
    for field in runner.OUTCOME_FIELDS:
        if field in outcome:
            record[field] = outcome[field]
    if "candidate_provenance" in outcome:
        record["candidate_provenance"] = outcome["candidate_provenance"]
    return record


def test_v3_verifier_backed_candidate_with_applied_patch_event_rejected(manifest_v3):
    """A v3 verifier-backed submitted candidate (verifier_runs >= 1) with
    candidate_provenance = applied_patch_event is rejected."""
    case = manifest_v3["case_order"][1]
    record = _v3_result_record(manifest_v3, case, {
        "terminal_status": "RESOLVED",
        "terminal_reason_code": "RESOLVED_COMPLETED",
        "patch_submissions": 1,
        "verifier_runs": 1,
        "candidate_hash": "e" * 64,
        "candidate_provenance": "applied_patch_event",
        "repair_outcome": "RESOLVED",
        "independent_verifier_result": {"status": "COMPLETED", "outcome": "RESOLVED", "lifecycle_succeeded": True},
        "logical_model_calls": 1,
        "provider_process_attempts": 1,
        "valid_directives": 1,
        "baseline_reproduction": True,
    })
    record["route_observation"].update({"opencode_version": "1.0.0", "active_model_status": "ACTIVE", "variant_available": True, "catalog_fingerprint": "c" * 64, "preflight_success": True})
    with pytest.raises(pilot.PilotError, match="verifier_record"):
        pilot.validate_case_result(record, manifest_v3)


def test_v3_validation_not_reached_with_verifier_record_rejected(manifest_v3):
    """A v3 VALIDATION_NOT_REACHED result with candidate_provenance =
    verifier_record is rejected."""
    case = manifest_v3["case_order"][4]
    outcome = _static_baseline_pre_validate_exhausted_outcome(manifest_v3, case, _route_evidence(manifest_v3))
    outcome["candidate_provenance"] = "verifier_record"
    record = _v3_result_record(manifest_v3, case, outcome)
    record.update({
        "terminal_status": "VALIDATION_NOT_REACHED",
        "terminal_reason_code": "VALIDATION_NOT_REACHED_PRE_VALIDATE",
    })
    record["route_observation"].update({"opencode_version": "1.0.0", "active_model_status": "ACTIVE", "variant_available": True, "catalog_fingerprint": "c" * 64, "preflight_success": True})
    with pytest.raises(pilot.PilotError, match="applied_patch_event"):
        pilot.validate_case_result(record, manifest_v3)


def test_v3_zero_patch_with_non_null_provenance_rejected(manifest_v3):
    """A v3 zero-patch result with candidate_provenance = verifier_record is
    rejected (zero submissions require null provenance)."""
    case = manifest_v3["case_order"][0]
    record = _v3_result_record(manifest_v3, case, {
        "patch_submissions": 0,
        "verifier_runs": 0,
        "candidate_hash": None,
        "candidate_provenance": "verifier_record",
        "repair_outcome": "NO_CANDIDATE",
    })
    record["route_observation"].update({"opencode_version": "1.0.0", "active_model_status": "ACTIVE", "variant_available": True, "catalog_fingerprint": "c" * 64, "preflight_success": True})
    with pytest.raises(pilot.PilotError, match="null candidate provenance"):
        pilot.validate_case_result(record, manifest_v3)

def test_applied_patch_from_events_mismatched_patch_sha256_yields_none():
    """When the observation's patch_sha256 does not equal the SHA-256 of the
    action's exact patch string, the derivation fails closed (returns None)."""
    wrong_sha = "0" * 64
    events = (
        _events_line("action", "apply_patch", "Patch", {"action": {"action_id": "action-000000009", "name": "apply_patch", "arguments": {"patch": _IS_VALID_PARENTHESIZATION_PATCH}}})
        + "\n"
        + _events_line("observation", "apply_patch", "Patch", {"observation": {"action_id": "action-000000009", "name": "apply_patch", "status": "ok", "payload": {"applied": True, "changed_files": ["python_programs/is_valid_parenthesization.py"], "hunk_count": 1, "patch_sha256": wrong_sha}}})
        + "\n"
    )
    assert adapter._applied_patch_from_events(events) is None

# ---- version-aware public_case_record and validate_campaign_record regressions --


def test_v2_public_case_record_omits_candidate_provenance():
    """v1/v2 public_case_record must not contain candidate_provenance."""
    manifest = pilot.load_manifest(pilot.MANIFEST_PATH_V2)
    case = manifest["case_order"][0]
    record = pilot.public_case_record(manifest, case)
    assert "candidate_provenance" not in record


def test_v3_public_case_record_includes_candidate_provenance():
    """v3 public_case_record must contain candidate_provenance (default None)."""
    manifest = pilot.load_manifest(pilot.MANIFEST_PATH_V3)
    case = manifest["case_order"][0]
    record = pilot.public_case_record(manifest, case)
    assert "candidate_provenance" in record
    assert record["candidate_provenance"] is None


def test_v2_manifest_rejects_v3_runner_schema():
    """validate_campaign_record with a v2 manifest rejects a record carrying
    the v3 runner schema version."""
    manifest = pilot.load_manifest(pilot.MANIFEST_PATH_V2)
    record = {
        "schema_version": runner.RUNNER_SCHEMA_VERSION_V3,
        "record_kind": "campaign",
        "campaign_id": manifest["campaign_id"],
        "campaign_version": manifest["campaign_version"],
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "status": "COMPLETED",
        "frozen_case_order": [case["case_id"] for case in manifest["case_order"]],
    }
    with pytest.raises(runner.LiveRunnerError, match="schema identity is invalid"):
        runner.validate_campaign_record(record, manifest)


def test_v3_manifest_rejects_v2_runner_schema():
    """validate_campaign_record with a v3 manifest rejects a record carrying
    the v2 runner schema version."""
    manifest = pilot.load_manifest(pilot.MANIFEST_PATH_V3)
    record = {
        "schema_version": runner.RUNNER_SCHEMA_VERSION_V2,
        "record_kind": "campaign",
        "campaign_id": manifest["campaign_id"],
        "campaign_version": manifest["campaign_version"],
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "status": "COMPLETED",
        "frozen_case_order": [case["case_id"] for case in manifest["case_order"]],
    }
    with pytest.raises(runner.LiveRunnerError, match="schema identity is invalid"):
        runner.validate_campaign_record(record, manifest)