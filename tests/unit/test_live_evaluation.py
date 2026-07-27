from __future__ import annotations

import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from agentic_debugger.agent.controller_policy import ActionName, PdbPolicy
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.demo.tools import DemoToolContext, build_registry
from agentic_debugger.evaluation.live import (
    JsonlCommandTransport,
    LiveCaseStatus,
    LiveConfigurationError,
    LiveExecutionAuthorization,
    LiveModelAdapter,
    LiveModelConfig,
    LiveOptInError,
    LiveRunLimits,
    LiveTransportError,
    redact_for_recording,
    render_live_report,
    run_live_case,
    run_live_evaluation,
    validate_live_report,
)
from agentic_debugger.evaluation.live_cli import main as live_main
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.events.schema import Action
from agentic_debugger.runtime.workspace import TaskWorkspace


ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "curated-none-handling-001"


@pytest.fixture
def workspace_parent():
    base = Path(tempfile.gettempdir()) / "agentic-debugger-task10a-acceptance"
    base.mkdir(parents=True, exist_ok=True)
    path = base / ("case-" + uuid.uuid4().hex)
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def config() -> LiveModelConfig:
    return LiveModelConfig("test-model", ("test-model-command",))


class FakeTransport:
    def __init__(self, responses=None, failures=0, interrupt=False):
        self.responses = list(responses or [])
        self.failures = failures
        self.interrupt = interrupt
        self.calls = 0

    def request(self, payload, timeout_seconds):
        self.calls += 1
        assert "oracle" not in payload["task"]
        if self.interrupt:
            raise KeyboardInterrupt
        if self.calls <= self.failures:
            raise LiveTransportError("provider detail must not escape")
        return self.responses.pop(0)


def _patch() -> str:
    fixture = ROOT / "agentic_debugger/datasets/curated" / TASK_ID
    scenario = scenario_for(TASK_ID)
    return build_reference_patch(
        (fixture / scenario.reference_repair.target_path).read_text(encoding="utf-8"),
        scenario.reference_repair,
    )


class ScriptedTransport:
    def __init__(self, patch: str, *, invalid_patch: bool = False):
        scenario = scenario_for(TASK_ID)
        self.patch = patch
        self.invalid_patch = invalid_patch
        self.index = 0
        self.directives = [
            {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
            {"kind": "transition", "target_state": "Understand", "reason": "reproduced"},
            {"kind": "action", "name": "find_function", "arguments": {"name": scenario.localization.symbol, "path": scenario.localization.file_path}},
            {"kind": "action", "name": "get_source_window", "arguments": {"path": scenario.localization.file_path, "line": 1}},
            {"kind": "add_hypothesis", "hypothesis_id": scenario.hypothesis_id, "statement": scenario.root_cause_statement, "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": False},
            {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": scenario.hypothesis_id, "statement": scenario.root_cause_statement, "target_file": scenario.localization.file_path, "target_symbol": scenario.localization.symbol, "confidence": "low"}},
            {"kind": "transition", "target_state": "Patch", "reason": "static evidence is sufficient"},
            {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch if not invalid_patch else "not-a-patch"}},
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

class PdbScriptedTransport(ScriptedTransport):
    def __init__(self, patch: str):
        super().__init__(patch)
        scenario = scenario_for(TASK_ID)
        self.directives = [
            {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
            {"kind": "transition", "target_state": "Understand", "reason": "reproduced"},
            {"kind": "transition", "target_state": "RuntimeEvidence", "reason": "runtime evidence requested"},
            {"kind": "action", "name": "start_pdb_session", "arguments": {}},
            {"kind": "action", "name": "get_stack_summary", "arguments": {}},
            {"kind": "action", "name": "get_frame_locals", "arguments": {"frame_id": 0, "pause_generation": 1}},
            {"kind": "action", "name": "stop_pdb_session", "arguments": {}},
            {"kind": "transition", "target_state": "Understand", "reason": "runtime evidence collected"},
            {"kind": "action", "name": "find_function", "arguments": {"name": scenario.localization.symbol, "path": scenario.localization.file_path}},
            {"kind": "action", "name": "get_source_window", "arguments": {"path": scenario.localization.file_path, "line": 1}},
            {"kind": "add_hypothesis", "hypothesis_id": scenario.hypothesis_id, "statement": scenario.root_cause_statement, "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": True},
            {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": scenario.hypothesis_id, "statement": scenario.root_cause_statement, "target_file": scenario.localization.file_path, "target_symbol": scenario.localization.symbol, "confidence": "high"}},
            {"kind": "transition", "target_state": "Patch", "reason": "runtime evidence is sufficient"},
            {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch}},
            {"kind": "action", "name": "syntax_check", "arguments": {}},
            {"kind": "transition", "target_state": "Validate", "reason": "syntax checked"},
            {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "post_patch"}},
            {"kind": "action", "name": "run_regression_tests", "arguments": {}},
            {"kind": "action", "name": "classify_outcome", "arguments": {}},
            {"kind": "transition", "target_state": "Done", "reason": "finished"},
        ]


class FailAfterFirstTransport:
    def request(self, payload, timeout_seconds):
        return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "controlled failure"}}


def _case(tmp_path: Path, transport, **kwargs):
    return run_live_case(
        repository_root=ROOT,
        task_id=TASK_ID,
        policy=kwargs.pop("policy", DemoPolicy.STATIC_BASELINE),
        repetition=kwargs.pop("repetition", 1),
        workspace_parent=tmp_path,
        config=config(),
        limits=kwargs.pop("limits", LiveRunLimits(max_model_requests=32, max_controller_steps=32)),
        transport=transport,
        **kwargs,
    )


def _single_case_report(case):
    payload = case.to_mapping()
    limits = LiveRunLimits(max_model_requests=32, max_controller_steps=32)
    evaluation_id = "single-case-validation"
    return {
        "schema_version": "1.0",
        "report_id": evaluation_id,
        "evaluation_id": evaluation_id,
        "run_label": "single-case-validation",
        "mode": "live",
        "disposition": "configured_live_execution",
        "completion": "interrupted" if payload["reporting"]["interrupted"] else ("partial" if not payload["reporting"]["completed"] else "complete"),
        "model": "test-model",
        "configuration": config().to_metadata(limits),
        "selected_tasks": [payload["task_id"]],
        "selected_policies": [payload["policy"]],
        "repetitions": 1,
        "expected_case_count": 1,
        "started_case_count": 1,
        "completed_case_count": 1 if payload["reporting"]["completed"] else 0,
        "incomplete_case_count": 0 if payload["reporting"]["completed"] else 1,
        "unstarted_case_count": 0,
        "interrupted": payload["reporting"]["interrupted"],
        "evaluation_cleanup": "not_owned",
        "evaluation_cleanup_error": None,
        "cases": [payload],
    }


@pytest.mark.parametrize("live_selected,confirmed", [(False, False), (False, True), (True, False)])
def test_both_opt_in_flags_are_required(live_selected, confirmed):
    with pytest.raises(LiveOptInError):
        LiveExecutionAuthorization.authorize(confirmed, live_selected)
    assert LiveExecutionAuthorization.authorize(True, True)


def test_cli_rejects_without_reading_configuration_in_all_flag_combinations(workspace_parent):
    missing = workspace_parent / "does-not-exist.json"
    for live_selected, confirmed in ((False, False), (False, True), (True, False)):
        output = workspace_parent / f"rejected-{live_selected}-{confirmed}.json"
        human = workspace_parent / f"rejected-{live_selected}-{confirmed}.txt"
        assert live_main(["--config", str(missing), "--output", str(output), "--human-output", str(human), *(["--live"] if live_selected else []), *(["--confirm-live-model-access"] if confirmed else [])]) == 2
        assert json.loads(output.read_text())["disposition"] == "attempted_but_rejected"
        assert "rejected" in human.read_text()


def test_configuration_rejects_null_malformed_unsupported_and_credentials(workspace_parent):
    with pytest.raises(LiveConfigurationError):
        LiveModelConfig.from_mapping(None)
    with pytest.raises(LiveConfigurationError):
        LiveModelConfig.from_mapping({"model_name": "m", "command": None})
    with pytest.raises(LiveConfigurationError):
        LiveModelConfig.from_mapping({"model_name": "m", "command": ["x"], "unsupported": 1})
    with pytest.raises(LiveConfigurationError):
        LiveModelConfig.from_mapping({"model_name": "m", "command": ["x"], "secret": "value"})
    with pytest.raises(LiveConfigurationError):
        LiveModelConfig.from_mapping({"model_name": "m", "command": ["x", "--api-key"]})
    with pytest.raises(LiveConfigurationError):
        LiveModelConfig.from_mapping({"model_name": "token=usable", "command": ["x"]})
    with pytest.raises(LiveConfigurationError):
        LiveModelConfig.from_mapping({"model_name": "m", "command": ["x", "--api-key=credential-value"]})
    with pytest.raises(LiveConfigurationError):
        LiveModelConfig.from_mapping({"model_name": "m", "command": ["x", "Bearer credential-value"]})
    with pytest.raises(LiveConfigurationError):
        LiveModelConfig.from_file(workspace_parent / "bad.json")


def test_token_usage_survives_redaction_and_secret_values_do_not():
    value = {"token_usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "provider_reported": True, "missing_fields": []}, "api_key": "secret-value", "diagnostic": "token=secret-value"}
    result = redact_for_recording(value)
    assert result["token_usage"]["total_tokens"] == 3
    assert result["token_usage"]["provider_reported"] is True
    assert result["api_key"] == "<redacted>"
    assert "secret-value" not in json.dumps(result)


def test_adapter_request_and_retry_limits_and_unknown_usage():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    transport = FakeTransport([{"directive": {"kind": "transition", "target_state": "Failed", "reason": "fake stop"}}], failures=2)
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=transport, limits=LiveRunLimits(max_model_requests=3, max_controller_steps=3, max_retries=2))
    from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
    from agentic_debugger.agent.model_adapter import ControllerSnapshot
    directive = adapter.next_directive(ControllerSnapshot("run", task.task_id, ControllerState.REPRODUCE, 0, ControllerBudgetLimits.from_task_constraints(task.constraints), ControllerBudgetState(), HypothesisLedger()))
    assert directive.target_state is ControllerState.FAILED
    assert adapter.metrics.model_requests == 3
    assert adapter.metrics.model_responses == 1
    assert adapter.metrics.retries == 2
    assert adapter.metrics.to_mapping()["token_usage"]["total_tokens"] is None


def test_timeout_classification_and_measurements_survive_adapter_failure():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    transport = FakeTransport(failures=0)
    transport.request = lambda payload, timeout_seconds: (_ for _ in ()).throw(LiveTransportError("timeout", kind="request_timeout", timed_out=True))
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=transport, limits=LiveRunLimits(max_model_requests=1, max_controller_steps=1, max_retries=0))
    from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
    from agentic_debugger.agent.model_adapter import ControllerSnapshot
    with pytest.raises(Exception):
        adapter.next_directive(ControllerSnapshot("run", task.task_id, ControllerState.REPRODUCE, 0, ControllerBudgetLimits.from_task_constraints(task.constraints), ControllerBudgetState(), HypothesisLedger()))
    assert adapter.metrics.model_requests == 1
    assert adapter.metrics.to_mapping()["provider_error_count"] == 1
    assert adapter.metrics.termination_reason == "request_timeout"


def test_process_output_is_bounded_before_serialization():
    command = (sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000); sys.stderr.write('y' * 100000)")
    transport = JsonlCommandTransport(LiveModelConfig("local", command), max_output_bytes=1024)
    with pytest.raises(LiveTransportError, match="output bound"):
        transport.request({}, 5)

def test_process_stdin_and_wait_share_the_declared_timeout():
    command = (sys.executable, "-c", "import time; time.sleep(5)")
    transport = JsonlCommandTransport(LiveModelConfig("local", command), max_output_bytes=1024)
    with pytest.raises(LiveTransportError) as error:
        transport.request({"large_context": "x" * 500000}, 0.05)
    assert error.value.timed_out is True
    assert error.value.kind == "request_timeout"

def test_case_provider_failure_and_controller_exception_retain_measurements(workspace_parent, monkeypatch):
    provider = _case(workspace_parent, FakeTransport(failures=99), limits=LiveRunLimits(max_model_requests=2, max_controller_steps=2, max_retries=1))
    assert provider.status is LiveCaseStatus.PROVIDER_ERROR
    assert provider.measurements["model_request_count"] == 2
    assert provider.measurements["provider_error_count"] == 2
    assert provider.measurements["retry_count"] == 1
    assert provider.measurements["termination_reason"] == "provider_or_transport_error"
    from agentic_debugger.evaluation import live as live_module
    def explode(controller, snapshot):
        controller.model_adapter.next_directive(snapshot)
        raise RuntimeError("controller exploded")
    monkeypatch.setattr(live_module.DeterministicController, "run", explode)
    failed = _case(workspace_parent, ScriptedTransport(_patch()))
    assert failed.status is LiveCaseStatus.CONTROLLER_FAILED
    assert failed.measurements["model_request_count"] == 1
    assert failed.measurements["case_elapsed_duration_ms"] >= 0
    assert failed.controller["exception"] is True

def test_model_request_context_is_complete_bounded_and_identity_scoped():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = {}
    class ContextTransport:
        def request(self, payload, timeout_seconds):
            captured.update(payload)
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "context check"}}
    from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
    from agentic_debugger.agent.model_adapter import ControllerSnapshot
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=ContextTransport(), limits=LiveRunLimits(max_model_requests=1), evaluation_id="eval-x", case_id="case-x", run_id="run-x", trajectory_id="trajectory-x")
    adapter.next_directive(ControllerSnapshot("run-x", task.task_id, ControllerState.REPRODUCE, 0, ControllerBudgetLimits.from_task_constraints(task.constraints), ControllerBudgetState(), HypothesisLedger()))
    assert captured["protocol"]["name"] == "agentic-debugger-live-jsonl"
    assert captured["identity"] == {"evaluation_id": "eval-x", "case_id": "case-x", "run_id": "run-x", "trajectory_id": "trajectory-x"}
    assert "budget_limits" in captured["controller"]
    assert "budget_state" in captured["controller"]
    assert "hypotheses" in captured["controller"]
    assert "directive_schema" in captured
    assert "apply_patch" in captured["action_contracts"]
    assert isinstance(captured["history"], list)


def test_successful_fake_model_uses_controller_patch_lifecycle_verifier_and_events(workspace_parent):
    result = _case(workspace_parent, ScriptedTransport(_patch()))
    assert result.status is LiveCaseStatus.RESOLVED
    assert result.controller["completed"] is True
    assert result.verifier["executed"] is True
    assert result.verifier["outcome"] == "RESOLVED"
    assert result.verifier["patch_application"]["attempted"] is True
    assert result.reporting["event_recorded"] is True
    assert result.case_id.endswith(":r1")
    assert result.run_id in result.events_jsonl
    assert not list(workspace_parent.iterdir())

def test_pdb_enabled_live_case_uses_real_probe_observation_and_cleans_up(workspace_parent):
    result = _case(workspace_parent, PdbScriptedTransport(_patch()), policy=DemoPolicy.PDB_ON_UNCERTAINTY, limits=LiveRunLimits(max_model_requests=32, max_controller_steps=32))
    assert result.status is LiveCaseStatus.RESOLVED
    assert result.measurements["successful_pdb_observation_count"] >= 1
    assert result.measurements["failed_pdb_observation_count"] == 0
    assert result.controller["completed"] is True
    assert result.verifier["outcome"] == "RESOLVED"
    assert not list(workspace_parent.iterdir())


def test_rejected_patch_attempt_cannot_be_verified_as_resolved(workspace_parent):
    result = _case(workspace_parent, ScriptedTransport(_patch(), invalid_patch=True), limits=LiveRunLimits(max_model_requests=20, max_controller_steps=20))
    assert result.status is LiveCaseStatus.UNRESOLVED
    assert result.verifier["executed"] is False
    assert result.reporting["completed"] is True
    assert not list(workspace_parent.iterdir())


def test_patch_context_only_authorizes_successful_and_current_patch(workspace_parent):
    fixture = ROOT / "agentic_debugger/datasets/curated" / TASK_ID
    workspace = TaskWorkspace(str(fixture), parent_dir=str(workspace_parent))
    context = DemoToolContext(task=DebugTask.from_mapping(json.loads((fixture / "task.json").read_text())), workspace=workspace, patch="", probe=None)
    registry = build_registry(context, pdb_policy=PdbPolicy.DISABLED)
    valid = _patch()
    invalid = Action("invalid", "run", TASK_ID, ControllerState.PATCH, ActionName.APPLY_PATCH.value, {"patch": "bad"})
    registry.dispatch(invalid, observation_id="obs-invalid")
    assert context.candidate_patch == ""
    accepted = Action("accepted", "run", TASK_ID, ControllerState.PATCH, ActionName.APPLY_PATCH.value, {"patch": valid})
    registry.dispatch(accepted, observation_id="obs-accepted")
    assert context.candidate_patch == valid
    superseded = Action("superseded", "run", TASK_ID, ControllerState.PATCH, ActionName.APPLY_PATCH.value, {"patch": valid})
    rejected = registry.dispatch(superseded, observation_id="obs-superseded")
    assert rejected.status.value == "rejected"
    assert context.candidate_patch == valid
    reverted = Action("revert", "run", TASK_ID, ControllerState.PATCH, ActionName.REVERT_PATCH.value, {})
    registry.dispatch(reverted, observation_id="obs-revert")
    assert context.candidate_patch == ""
    accepted_again = registry.dispatch(accepted, observation_id="obs-accepted-again")
    assert accepted_again.status.value == "ok"
    assert context.candidate_patch == valid
    registry.dispatch(reverted, observation_id="obs-revert-again")
    workspace.cleanup()


def test_static_policy_cannot_use_pdb(workspace_parent):
    fixture = ROOT / "agentic_debugger/datasets/curated" / TASK_ID
    workspace = TaskWorkspace(str(fixture), parent_dir=str(workspace_parent))
    context = DemoToolContext(task=DebugTask.from_mapping(json.loads((fixture / "task.json").read_text())), workspace=workspace, patch="", probe=None)
    action = Action("pdb", "run", TASK_ID, ControllerState.RUNTIME_EVIDENCE, ActionName.START_PDB_SESSION.value, {})
    observation = build_registry(context, pdb_policy=PdbPolicy.DISABLED).dispatch(action, observation_id="obs")
    assert observation.status.value == "rejected"
    assert context.tool_errors and "disabled" in context.tool_errors[0]["diagnostic"].lower()
    workspace.cleanup()


def test_pdb_enabled_policy_keeps_accepted_runtime_boundary(workspace_parent):
    fixture = ROOT / "agentic_debugger/datasets/curated" / TASK_ID
    workspace = TaskWorkspace(str(fixture), parent_dir=str(workspace_parent))
    context = DemoToolContext(task=DebugTask.from_mapping(json.loads((fixture / "task.json").read_text())), workspace=workspace, patch="", probe=None)
    action = Action("pdb", "run", TASK_ID, ControllerState.RUNTIME_EVIDENCE, ActionName.START_PDB_SESSION.value, {})
    observation = build_registry(context, pdb_policy=PdbPolicy.ON_UNCERTAINTY).dispatch(action, observation_id="obs")
    assert observation.status.value == "rejected"
    assert context.tool_errors and "probe" in context.tool_errors[0]["diagnostic"].lower()
    workspace.cleanup()


def test_case_and_trajectory_identity_are_unique_across_policy_and_repetition(workspace_parent):
    report = run_live_evaluation(repository_root=ROOT, authorization=LiveExecutionAuthorization.authorize(True, True), config=config(), limits=LiveRunLimits(max_model_requests=2, max_controller_steps=2), task_ids=(TASK_ID,), policies=(DemoPolicy.STATIC_BASELINE, DemoPolicy.PDB_ON_UNCERTAINTY), repetitions=2, workspace_parent=workspace_parent, transport_factory=lambda task_id, policy, repetition: FailAfterFirstTransport(), evaluation_id="identity-test")
    cases = report["cases"]
    assert report["schema_version"] == "1.0"
    assert len({case["case_id"] for case in cases}) == 4
    assert len({case["trajectory_id"] for case in cases}) == 4
    assert len({event["run_id"] for case in cases for event in (json.loads(line) for line in case["events_jsonl"].splitlines())}) == 4


def test_separate_evaluations_have_unique_report_case_run_trajectory_and_request_namespaces(workspace_parent):
    captured = []
    class IdentityTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload["identity"] | {"request_id": payload["protocol"]["request_id"]})
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "identity check"}}
    def factory(*args):
        return IdentityTransport()
    first = run_live_evaluation(repository_root=ROOT, authorization=LiveExecutionAuthorization.authorize(True, True), config=config(), limits=LiveRunLimits(max_model_requests=2, max_controller_steps=2), task_ids=(TASK_ID,), policies=(DemoPolicy.STATIC_BASELINE,), repetitions=1, workspace_parent=workspace_parent, transport_factory=factory)
    second = run_live_evaluation(repository_root=ROOT, authorization=LiveExecutionAuthorization.authorize(True, True), config=config(), limits=LiveRunLimits(max_model_requests=2, max_controller_steps=2), task_ids=(TASK_ID,), policies=(DemoPolicy.STATIC_BASELINE,), repetitions=1, workspace_parent=workspace_parent, transport_factory=factory)
    validate_live_report(first)
    validate_live_report(second)
    assert first["evaluation_id"] != second["evaluation_id"]
    assert first["report_id"] != second["report_id"]
    assert first["cases"][0]["case_id"] != second["cases"][0]["case_id"]
    assert first["cases"][0]["run_id"] != second["cases"][0]["run_id"]
    assert first["cases"][0]["trajectory_id"] != second["cases"][0]["trajectory_id"]
    assert captured[0]["request_id"] != captured[1]["request_id"]
    assert first["configuration"]["configuration_fingerprint"] == second["configuration"]["configuration_fingerprint"]
    assert "command" not in first["configuration"]


def test_duplicate_task_and_policy_selection_is_rejected_before_any_case(workspace_parent):
    def never_start(*args):
        raise AssertionError("case must not start")
    with pytest.raises(LiveConfigurationError, match="duplicate task"):
        run_live_evaluation(repository_root=ROOT, authorization=LiveExecutionAuthorization.authorize(True, True), config=config(), limits=LiveRunLimits(), task_ids=(TASK_ID, TASK_ID), policies=(DemoPolicy.STATIC_BASELINE,), workspace_parent=workspace_parent, transport_factory=never_start)
    with pytest.raises(LiveConfigurationError, match="duplicate policy"):
        run_live_evaluation(repository_root=ROOT, authorization=LiveExecutionAuthorization.authorize(True, True), config=config(), limits=LiveRunLimits(), task_ids=(TASK_ID,), policies=(DemoPolicy.STATIC_BASELINE, DemoPolicy.STATIC_BASELINE), workspace_parent=workspace_parent, transport_factory=never_start)


def test_configuration_fingerprint_is_stable_safe_and_sensitive_to_material_change():
    same = LiveModelConfig("test-model", ("test-model-command",))
    changed = LiveModelConfig("test-model", ("test-model-command",), request_timeout_seconds=61.0)
    assert same.configuration_fingerprint == config().configuration_fingerprint
    assert same.configuration_fingerprint != changed.configuration_fingerprint
    metadata = same.to_metadata(LiveRunLimits(max_model_requests=7, max_controller_steps=8, max_retries=1, continue_on_task_failure=False))
    assert metadata["configuration_fingerprint"] == same.configuration_fingerprint
    assert metadata["limits"]["max_model_requests"] == 7
    assert metadata["continue_on_task_failure"] is False
    assert "command" not in metadata


def test_model_timing_accumulates_transport_only_across_requests_and_retries():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
    from agentic_debugger.agent.model_adapter import ControllerSnapshot
    now = [0.0]
    calls = {"count": 0}
    class TimedTransport:
        def request(self, payload, timeout_seconds):
            calls["count"] += 1
            now[0] += 0.5
            if calls["count"] == 1:
                raise LiveTransportError("retry", kind="provider_failure")
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "timed"}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=TimedTransport(), limits=LiveRunLimits(max_model_requests=3, max_controller_steps=3, max_retries=1), clock=lambda: now[0])
    snapshot = ControllerSnapshot("timed-run", task.task_id, ControllerState.REPRODUCE, 0, ControllerBudgetLimits.from_task_constraints(task.constraints), ControllerBudgetState(), HypothesisLedger())
    adapter.next_directive(snapshot)
    now[0] += 4.0
    adapter.next_directive(snapshot)
    assert adapter.metrics.retries == 1
    assert adapter.model_phase_elapsed_seconds == 1.5
    assert adapter._remaining() == 898.5


def test_case_model_timing_excludes_post_request_verifier_event_cleanup(workspace_parent):
    result = _case(workspace_parent, ScriptedTransport(_patch()))
    assert result.measurements["model_phase_elapsed_duration_ms"] == result.measurements["model_transport_duration_ms"]
    assert result.measurements["elapsed_scope"] == "case_observed; model_phase=transport_only"
    assert result.measurements["model_phase_elapsed_duration_ms"] <= result.measurements["case_elapsed_duration_ms"]


def test_preexisting_collision_content_is_not_deleted(workspace_parent, monkeypatch):
    collision = workspace_parent / ("agentic-live-case-" + uuid.UUID(int=0).hex)
    collision.mkdir()
    marker = collision / "keep.txt"
    marker.write_text("keep")
    monkeypatch.setattr("agentic_debugger.evaluation.live.uuid.uuid4", lambda: uuid.UUID(int=0))
    result = _case(workspace_parent, FailAfterFirstTransport())
    assert result.status is LiveCaseStatus.HARNESS_ERROR
    assert marker.read_text() == "keep"
    assert collision.exists()


def test_interrupted_run_is_partial_and_cleaned(workspace_parent):
    result = _case(workspace_parent, FakeTransport(interrupt=True))
    assert result.status is LiveCaseStatus.INCOMPLETE
    assert result.reporting["partial"] is True
    assert result.reporting["interrupted"] is True
    validate_live_report(_single_case_report(result))
    assert not list(workspace_parent.iterdir())


def test_transport_setup_interruption_has_correct_case_schema_counts_and_human_report(workspace_parent):
    def interrupted_factory(*args):
        raise KeyboardInterrupt

    report = run_live_evaluation(
        repository_root=ROOT,
        authorization=LiveExecutionAuthorization.authorize(True, True),
        config=config(),
        limits=LiveRunLimits(max_model_requests=2, max_controller_steps=2),
        task_ids=(TASK_ID,),
        policies=(DemoPolicy.STATIC_BASELINE,),
        repetitions=1,
        workspace_parent=workspace_parent,
        transport_factory=interrupted_factory,
        evaluation_id="transport-setup-interrupted",
    )
    validate_live_report(report)
    case = report["cases"][0]
    assert report["completion"] == "interrupted"
    assert report["interrupted"] is True
    assert (report["expected_case_count"], report["started_case_count"], report["completed_case_count"], report["incomplete_case_count"], report["unstarted_case_count"]) == (1, 1, 0, 1, 0)
    assert case["status"] == "INCOMPLETE"
    assert case["controller"]["completed"] is False
    assert case["verifier"]["executed"] is False
    assert case["measurements"]["termination_reason"] == "interrupted"
    assert case["reporting"]["completed"] is False
    assert case["reporting"]["cleanup"] == "not_started"
    assert case["events_jsonl"] == ""
    assert "INCOMPLETE" in render_live_report(report)


def test_controller_tool_pdb_verifier_event_and_cleanup_interruptions_are_schema_valid(workspace_parent, monkeypatch):
    from agentic_debugger.evaluation import live as live_module
    from agentic_debugger.agent.tool_registry import ToolRegistry
    from agentic_debugger.runtime.pdb_session import PdbSession

    monkeypatch.setattr(live_module.DeterministicController, "run", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt))
    controller_case = _case(workspace_parent, ScriptedTransport(_patch()))
    assert controller_case.status is LiveCaseStatus.INCOMPLETE
    validate_live_report(_single_case_report(controller_case))

    monkeypatch.undo()
    monkeypatch.setattr(ToolRegistry, "dispatch", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt))
    tool_case = _case(workspace_parent, ScriptedTransport(_patch()))
    assert tool_case.status is LiveCaseStatus.INCOMPLETE
    validate_live_report(_single_case_report(tool_case))

    monkeypatch.undo()
    monkeypatch.setattr(PdbSession, "start", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt))
    pdb_case = _case(workspace_parent, PdbScriptedTransport(_patch()), policy=DemoPolicy.PDB_ON_UNCERTAINTY)
    assert pdb_case.status is LiveCaseStatus.INCOMPLETE
    validate_live_report(_single_case_report(pdb_case))

    monkeypatch.undo()
    monkeypatch.setattr(live_module.EvaluationVerifier, "evaluate", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt))
    verifier_case = _case(workspace_parent, ScriptedTransport(_patch()))
    assert verifier_case.status is LiveCaseStatus.INCOMPLETE
    validate_live_report(_single_case_report(verifier_case))

    monkeypatch.undo()
    monkeypatch.setattr(live_module, "project_controller_run", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt))
    event_case = _case(workspace_parent, ScriptedTransport(_patch()))
    assert event_case.status is LiveCaseStatus.INCOMPLETE
    validate_live_report(_single_case_report(event_case))

    monkeypatch.undo()
    monkeypatch.setattr(live_module, "_remove_owned_case_dir", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt))
    cleanup_case = _case(workspace_parent, FailAfterFirstTransport())
    assert cleanup_case.status is LiveCaseStatus.INCOMPLETE
    assert cleanup_case.reporting["cleanup"] == "failed"
    validate_live_report(_single_case_report(cleanup_case))


def test_cli_exit_three_uses_actual_transport_setup_interrupted_report(workspace_parent, monkeypatch):
    from agentic_debugger.evaluation import live_cli as live_cli_module

    config_path = workspace_parent / "config.json"
    config_path.write_text(json.dumps({"model_name": "local-fake", "command": ["not-launched"]}))
    output = workspace_parent / "interrupted.json"
    human = workspace_parent / "interrupted.txt"

    def actual_interrupted_evaluation(**kwargs):
        kwargs["task_ids"] = (TASK_ID,)
        kwargs["policies"] = (DemoPolicy.STATIC_BASELINE,)
        kwargs["repetitions"] = 1
        kwargs["workspace_parent"] = workspace_parent
        kwargs["transport_factory"] = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt)
        return run_live_evaluation(**kwargs)

    monkeypatch.setattr(live_cli_module, "run_live_evaluation", actual_interrupted_evaluation)
    assert live_main(["--live", "--confirm-live-model-access", "--config", str(config_path), "--output", str(output), "--human-output", str(human)]) == 3
    report = json.loads(output.read_text())
    validate_live_report(report)
    assert report["completion"] == "interrupted"
    assert "INCOMPLETE" in human.read_text()


def test_validator_rejects_swapped_missing_and_impossible_case_mappings(workspace_parent):
    valid = _case(workspace_parent, ScriptedTransport(_patch()))
    report = _single_case_report(valid)
    validate_live_report(report)

    swapped = json.loads(json.dumps(report))
    swapped["cases"][0]["measurements"], swapped["cases"][0]["reporting"] = swapped["cases"][0]["reporting"], swapped["cases"][0]["measurements"]
    with pytest.raises(LiveConfigurationError):
        validate_live_report(swapped)

    malformed_usage = json.loads(json.dumps(report))
    del malformed_usage["cases"][0]["measurements"]["token_usage"]["total_tokens"]
    with pytest.raises(LiveConfigurationError):
        validate_live_report(malformed_usage)

    impossible = json.loads(json.dumps(report))
    impossible["completion"] = "interrupted"
    impossible["interrupted"] = True
    with pytest.raises(LiveConfigurationError):
        validate_live_report(impossible)

    missing_controller = json.loads(json.dumps(report))
    del missing_controller["cases"][0]["controller"]
    with pytest.raises(LiveConfigurationError):
        validate_live_report(missing_controller)

    malformed_top_level = json.loads(json.dumps(report))
    malformed_top_level["started_case_count"] = "1"
    with pytest.raises(LiveConfigurationError):
        validate_live_report(malformed_top_level)

    not_started = json.loads(json.dumps(report))
    not_started["completion"] = "not_started"
    with pytest.raises(LiveConfigurationError):
        validate_live_report(not_started)

def test_evaluation_interruption_retains_cases_and_counts(workspace_parent):
    calls = {"count": 0}
    def factory(task, policy, repetition):
        calls["count"] += 1
        return FailAfterFirstTransport() if calls["count"] == 1 else FakeTransport(interrupt=True)
    report = run_live_evaluation(repository_root=ROOT, authorization=LiveExecutionAuthorization.authorize(True, True), config=config(), limits=LiveRunLimits(max_model_requests=4, max_controller_steps=4), task_ids=(TASK_ID, "curated-off-by-one-002"), policies=(DemoPolicy.STATIC_BASELINE,), repetitions=1, workspace_parent=workspace_parent, transport_factory=factory, evaluation_id="interrupt-evaluation")
    validate_live_report(report)
    assert report["completion"] == "interrupted"
    assert report["interrupted"] is True
    assert report["expected_case_count"] == 2
    assert report["started_case_count"] == 2
    assert report["completed_case_count"] == 1
    assert report["incomplete_case_count"] == 1
    assert report["unstarted_case_count"] == 0
    assert report["cases"][1]["status"] == "INCOMPLETE"

def test_cleanup_and_evaluation_cleanup_failures_are_reported(workspace_parent, monkeypatch):
    from agentic_debugger.evaluation import live as live_module
    monkeypatch.setattr(live_module, "_remove_owned_case_dir", lambda *args, **kwargs: (False, "controlled case cleanup failure"))
    failed = _case(workspace_parent, FailAfterFirstTransport())
    assert failed.status is LiveCaseStatus.CLEANUP_FAILED
    monkeypatch.undo()
    original = live_module.shutil.rmtree
    def failing_rmtree(path, *args, **kwargs):
        if Path(path).name.startswith("agentic-live-evaluation-"):
            raise OSError("controlled evaluation cleanup failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(live_module.shutil, "rmtree", failing_rmtree)
    report = run_live_evaluation(repository_root=ROOT, authorization=LiveExecutionAuthorization.authorize(True, True), config=config(), limits=LiveRunLimits(max_model_requests=2, max_controller_steps=2), task_ids=(TASK_ID,), policies=(DemoPolicy.STATIC_BASELINE,), repetitions=1, workspace_parent=None, transport_factory=lambda *args: FailAfterFirstTransport(), evaluation_id="cleanup-evaluation")
    validate_live_report(report)
    assert report["evaluation_cleanup"] == "failed"
    assert report["evaluation_cleanup_error"]


def test_verifier_and_event_failures_are_distinct(workspace_parent, monkeypatch):
    from agentic_debugger.evaluation import live as live_module
    monkeypatch.setattr(live_module.EvaluationVerifier, "evaluate", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("verifier failure")))
    verifier_failed = _case(workspace_parent, ScriptedTransport(_patch()))
    assert verifier_failed.status is LiveCaseStatus.VERIFIER_FAILED
    monkeypatch.setattr(live_module, "project_controller_run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("event failure")))
    event_failed = _case(workspace_parent, ScriptedTransport(_patch()))
    assert event_failed.status is LiveCaseStatus.EVENT_REPORTING_FAILED


def test_cli_machine_human_and_exit_semantics(workspace_parent):
    config_path = workspace_parent / "config.json"
    config_path.write_text(json.dumps({"schema_version": "1.0", "model_name": "local-fake", "command": [sys.executable, "-c", "import json,sys; json.dump({'directive': {'kind':'transition','target_state':'Failed','reason':'cli failure'}},sys.stdout)"]}))
    output = workspace_parent / "failed.json"
    human = workspace_parent / "failed.txt"
    exit_code = live_main(["--live", "--confirm-live-model-access", "--config", str(config_path), "--task-id", TASK_ID, "--policy", "static-baseline", "--output", str(output), "--human-output", str(human)])
    assert exit_code == 1
    assert json.loads(output.read_text())["cases"][0]["status"] == "CONTROLLER_FAILED"
    assert "CONTROLLER_FAILED" in human.read_text()

def test_cli_exit_zero_and_three_follow_complete_or_interrupted_reports(workspace_parent, monkeypatch):
    from agentic_debugger.evaluation import live_cli as live_cli_module
    config_path = workspace_parent / "config.json"
    config_path.write_text(json.dumps({"model_name": "local-fake", "command": ["not-launched"]}))
    resolved_report = run_live_evaluation(repository_root=ROOT, authorization=LiveExecutionAuthorization.authorize(True, True), config=config(), limits=LiveRunLimits(max_model_requests=32, max_controller_steps=32), task_ids=(TASK_ID,), policies=(DemoPolicy.STATIC_BASELINE,), repetitions=1, workspace_parent=workspace_parent, transport_factory=lambda *args: ScriptedTransport(_patch()), evaluation_id="cli-resolved")
    monkeypatch.setattr(live_cli_module, "run_live_evaluation", lambda **kwargs: resolved_report)
    assert live_main(["--live", "--confirm-live-model-access", "--config", str(config_path), "--output", str(workspace_parent / "resolved.json")]) == 0
    interrupted_report = run_live_evaluation(repository_root=ROOT, authorization=LiveExecutionAuthorization.authorize(True, True), config=config(), limits=LiveRunLimits(max_model_requests=2, max_controller_steps=2), task_ids=(TASK_ID,), policies=(DemoPolicy.STATIC_BASELINE,), repetitions=1, workspace_parent=workspace_parent, transport_factory=lambda *args: FakeTransport(interrupt=True), evaluation_id="cli-interrupted")
    validate_live_report(interrupted_report)
    monkeypatch.setattr(live_cli_module, "run_live_evaluation", lambda **kwargs: interrupted_report)
    assert live_main(["--live", "--confirm-live-model-access", "--config", str(config_path), "--output", str(workspace_parent / "interrupted.json")]) == 3


def test_cli_incomplete_exit_when_stop_on_failure_leaves_cases_unstarted(workspace_parent):
    config_path = workspace_parent / "config.json"
    config_path.write_text(json.dumps({"model_name": "local-fake", "command": [sys.executable, "-c", "import json,sys; json.dump({'directive': {'kind':'transition','target_state':'Failed','reason':'cli failure'}},sys.stdout)"]}))
    output = workspace_parent / "partial.json"
    human = workspace_parent / "partial.txt"
    exit_code = live_main(["--live", "--confirm-live-model-access", "--config", str(config_path), "--task-id", TASK_ID, "--task-id", "curated-off-by-one-002", "--policy", "static-baseline", "--output", str(output), "--human-output", str(human), "--stop-on-task-failure"])
    assert exit_code == 3
    report = json.loads(output.read_text())
    assert report["completion"] == "partial"
    assert report["completed_case_count"] == 1


def test_cli_rejects_duplicate_task_and_policy_selections_before_cases(workspace_parent):
    config_path = workspace_parent / "config.json"
    config_path.write_text(json.dumps({"model_name": "local-fake", "command": ["not-launched"]}))
    for args in (("--task-id", TASK_ID, "--task-id", TASK_ID), ("--policy", "static-baseline", "--policy", "static-baseline")):
        output = workspace_parent / ("duplicate-" + str(len(args)) + ".json")
        assert live_main(["--live", "--confirm-live-model-access", "--config", str(config_path), "--output", str(output), *args]) == 2
        report = json.loads(output.read_text())
        validate_live_report(report)
        assert report["disposition"] == "attempted_but_rejected"


def test_cli_rejects_internal_malformed_configured_report_before_writing(workspace_parent, monkeypatch):
    from agentic_debugger.evaluation import live_cli as live_cli_module
    config_path = workspace_parent / "config.json"
    config_path.write_text(json.dumps({"model_name": "local-fake", "command": ["not-launched"]}))
    valid = run_live_evaluation(repository_root=ROOT, authorization=LiveExecutionAuthorization.authorize(True, True), config=config(), limits=LiveRunLimits(max_model_requests=32, max_controller_steps=32), task_ids=(TASK_ID,), policies=(DemoPolicy.STATIC_BASELINE,), repetitions=1, workspace_parent=workspace_parent, transport_factory=lambda *args: ScriptedTransport(_patch()))
    malformed = dict(valid)
    malformed.pop("configuration")
    monkeypatch.setattr(live_cli_module, "run_live_evaluation", lambda **kwargs: malformed)
    output = workspace_parent / "malformed.json"
    assert live_main(["--live", "--confirm-live-model-access", "--config", str(config_path), "--output", str(output)]) == 1
    assert not output.exists()


def test_cleanup_failure_has_exit_one_contract(workspace_parent, monkeypatch):
    from agentic_debugger.evaluation import live as live_module
    from agentic_debugger.evaluation import live_cli as live_cli_module
    config_path = workspace_parent / "config.json"
    config_path.write_text(json.dumps({"model_name": "local-fake", "command": ["not-launched"]}))
    monkeypatch.setattr(live_module, "_remove_owned_case_dir", lambda *args, **kwargs: (False, "controlled cleanup failure"))
    cleanup_report = run_live_evaluation(repository_root=ROOT, authorization=LiveExecutionAuthorization.authorize(True, True), config=config(), limits=LiveRunLimits(max_model_requests=2, max_controller_steps=2), task_ids=(TASK_ID,), policies=(DemoPolicy.STATIC_BASELINE,), repetitions=1, workspace_parent=workspace_parent, transport_factory=lambda *args: FailAfterFirstTransport())
    validate_live_report(cleanup_report)
    monkeypatch.setattr(live_cli_module, "run_live_evaluation", lambda **kwargs: cleanup_report)
    output = workspace_parent / "cleanup.json"
    assert live_main(["--live", "--confirm-live-model-access", "--config", str(config_path), "--output", str(output)]) == 1
    assert json.loads(output.read_text())["completion"] == "partial"


def test_human_report_redacts_supported_secret_values_and_keeps_usage():
    report = {"schema_version": "1.0", "mode": "live", "disposition": "configured_live_execution", "completion": "complete", "model": "m", "expected_case_count": 1, "started_case_count": 1, "completed_case_count": 1, "incomplete_case_count": 0, "unstarted_case_count": 0, "interrupted": False, "cases": [{"schema_version": "1.0", "case_id": "c", "run_id": "r", "trajectory_id": "t", "status": "RESOLVED", "reporting": {"completed": True}, "measurements": {"model_request_count": 1, "retry_count": 0, "token_usage": {"total_tokens": 9, "provider_reported": True}, "diagnostic": {"nested_tokens": 123, "api_key": "secret"}}}]}
    human = render_live_report(report)
    assert "secret" not in human
    assert "tokens=9" in human

def test_adversarial_nested_tokens_are_redacted_but_typed_usage_survives():
    value = {"token_usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "provider_reported": True, "missing_fields": []}, "tokens": 999, "nested": {"tokens": 888, "credential": {"token": "secret-value"}}, "event": {"metadata": {"tokens": 777}, "diagnostic": "Bearer credential-value"}}
    result = redact_for_recording(value)
    assert result["token_usage"]["total_tokens"] == 3
    assert result["tokens"] == "<redacted>"
    assert result["nested"]["tokens"] == "<redacted>"
    assert result["event"]["metadata"]["tokens"] == "<redacted>"
    assert "secret-value" not in json.dumps(result)
    assert "credential-value" not in json.dumps(result)
