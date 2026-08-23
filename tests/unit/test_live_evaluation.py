from __future__ import annotations

import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from agentic_debugger.agent.controller_policy import (
    ActionName,
    BudgetKind,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisConfidence,
    HypothesisLedger,
    PdbPolicy,
    allowed_actions_for_state,
    budget_kind_for_action,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRejectedError, ToolRegistry, ToolResult, ToolSpec
from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.demo.tools import DemoToolContext, build_registry, prepare_pdb_probe
from agentic_debugger.evaluation.live import (
    MAX_REJECTION_DETAIL_CHARS,
    MODEL_HISTORY_WINDOW,
    PROOF_HISTORY_WINDOW,
    DirectiveRejectionCategory,
    JsonlCommandTransport,
    LiveCaseStatus,
    LiveConfigurationError,
    LiveExecutionAuthorization,
    LiveModelAdapter,
    LiveModelAdapterError,
    LiveModelConfig,
    LiveOptInError,
    LiveRunLimits,
    LiveTransportError,
    redact_for_recording,
    render_live_report,
    run_live_case,
    run_live_evaluation,
    validate_synthetic_qualification_content,
    validate_live_report,
)
from agentic_debugger.evaluation.live_cli import main as live_main
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.events.schema import Action, Observation, ObservationStatus
from agentic_debugger.events.replay import replay_events
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


def _test_live_registry() -> ToolRegistry:
    return ToolRegistry((
        ToolSpec(
            ActionName.RUN_REPRODUCTION,
            lambda arguments: dict(arguments),
            lambda _action, _arguments: ToolResult(ObservationStatus.OK, {}, "ok"),
            argument_contract={
                "required": ["phase"],
                "properties": {
                    "phase": {"type": "string", "min_length": 1}
                },
                "additional_properties": False,
            },
        ),
    ))


def test_live_adapter_requires_registry_before_transport():
    task = DebugTask.from_mapping(
        json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text())
    )
    calls = []

    class NeverTransport:
        def request(self, payload, timeout_seconds):
            calls.append(payload)
            raise AssertionError("transport must not be called")

    with pytest.raises(LiveConfigurationError, match="registry is required"):
        LiveModelAdapter(
            task=task,
            policy=DemoPolicy.STATIC_BASELINE,
            config=config(),
            transport=NeverTransport(),
            limits=LiveRunLimits(max_model_requests=1),
        )
    assert calls == []


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
            {"kind": "action", "name": "find_function", "arguments": {"name": scenario.localization.symbol, "path": scenario.localization.file_path}},
            {"kind": "action", "name": "get_source_window", "arguments": {"path": scenario.localization.file_path, "line": 1}},
            {"kind": "add_hypothesis", "hypothesis_id": scenario.hypothesis_id, "statement": scenario.root_cause_statement, "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": True},
            {"kind": "transition", "target_state": "RuntimeEvidence", "reason": "qualifying hypothesis requests runtime evidence"},
            {"kind": "action", "name": "start_pdb_session", "arguments": {}},
            {"kind": "action", "name": "get_stack_summary", "arguments": {}},
            {"kind": "action", "name": "get_frame_locals", "arguments": {"frame_id": 0, "pause_generation": 1}},
            {"kind": "action", "name": "stop_pdb_session", "arguments": {}},
            {"kind": "transition", "target_state": "Understand", "reason": "runtime evidence collected"},
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
        "schema_version": "1.1",
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
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=transport, limits=LiveRunLimits(max_model_requests=3, max_controller_steps=3, max_retries=2), registry=_test_live_registry())
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
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=transport, limits=LiveRunLimits(max_model_requests=1, max_controller_steps=1, max_retries=0), registry=_test_live_registry())
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


def test_typed_command_error_kind_survives_and_arbitrary_stderr_is_discarded():
    envelope = json.dumps(
        {
            "schema_version": "command-error-v1",
            "kind": "invalid_directive",
            "message": "provider completion did not satisfy the directive contract",
        },
        separators=(",", ":"),
    )


    typed_command = (
        sys.executable,
        "-c",
        f"import sys; sys.stdin.read(); sys.stderr.write({envelope!r}); raise SystemExit(1)",
    )
    with pytest.raises(LiveTransportError) as typed_error:
        JsonlCommandTransport(
            LiveModelConfig("local", typed_command), max_output_bytes=1024
        ).request({}, 5)
    assert typed_error.value.kind == "invalid_directive"
    assert "provider completion" not in str(typed_error.value)

    task = DebugTask.from_mapping(
        json.loads(
            (
                ROOT
                / "agentic_debugger/datasets/curated"
                / TASK_ID
                / "task.json"
            ).read_text()
        )
    )
    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig("local", typed_command),
        transport=JsonlCommandTransport(
            LiveModelConfig("local", typed_command), max_output_bytes=1024
        ),
        limits=LiveRunLimits(max_model_requests=1, max_retries=0),
        registry=_test_live_registry(),
    )
    with pytest.raises(LiveModelAdapterError):
        adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))
    assert adapter.metrics.to_mapping()["provider_error_kinds"] == [
        "invalid_directive"
    ]

    secret = "token=super-secret-value"
    arbitrary_command = (
        sys.executable,
        "-c",
        "import sys; sys.stderr.write(sys.stdin.read()); raise SystemExit(1)",
    )
    with pytest.raises(LiveTransportError) as arbitrary_error:
        JsonlCommandTransport(
            LiveModelConfig("local", arbitrary_command), max_output_bytes=1024
        ).request({"diagnostic": secret}, 5)
    assert arbitrary_error.value.kind == "process_error"
    assert secret not in str(arbitrary_error.value)


def _synthetic_qualification_contract() -> dict[str, dict[str, object]]:
    return {
        "run_reproduction": {
            "properties": {
                "phase": {"type": "string", "enum": ["baseline"]},
            },
            "required": ["phase"],
            "additional_properties": False,
        },
    }


def test_synthetic_qualification_reuses_live_parser_and_normalization_policy() -> None:
    content = '{"kind":"action","name":"run_reproduction","arguments":{"phase":"baseline"}}'

    accepted = validate_synthetic_qualification_content(
        content + "}",
        action_contracts=_synthetic_qualification_contract(),
    )
    rejected = validate_synthetic_qualification_content(
        '\":\"action\",\"name\":\"run_reproduction\",\"arguments\":{\"phase\":\"baseline\"}}',
        action_contracts=_synthetic_qualification_contract(),
    )

    assert accepted["directive_protocol_ok"] is True
    assert accepted["category"] == "DIRECTIVE_PROTOCOL_VERIFIED"
    assert accepted["normalization_applied"] is True
    assert rejected["directive_protocol_ok"] is False
    assert rejected["category"] == "DIRECTIVE_INVALID_JSON"
    assert rejected["reason_code"] == "invalid_json"

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
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=ContextTransport(), limits=LiveRunLimits(max_model_requests=1), registry=_test_live_registry(), evaluation_id="eval-x", case_id="case-x", run_id="run-x", trajectory_id="trajectory-x")
    adapter.next_directive(ControllerSnapshot("run-x", task.task_id, ControllerState.REPRODUCE, 0, ControllerBudgetLimits.from_task_constraints(task.constraints), ControllerBudgetState(), HypothesisLedger()))
    assert captured["protocol"]["name"] == "agentic-debugger-live-jsonl"
    assert captured["identity"] == {"evaluation_id": "eval-x", "case_id": "case-x", "run_id": "run-x", "trajectory_id": "trajectory-x"}
    assert "budget_limits" in captured["controller"]
    assert "budget_state" in captured["controller"]
    assert "hypotheses" in captured["controller"]
    assert "directive_schema" in captured
    assert set(captured["action_contracts"]) == set(captured["controller"]["allowed_actions"])
    assert set(captured["action_contracts"]) == {"run_reproduction"}
    assert captured["action_contracts"]["run_reproduction"] == {
        "required": ["phase"],
        "properties": {
            "phase": {"type": "string", "min_length": 1, "enum": ["baseline"]}
        },
        "additional_properties": False,
    }
    assert captured["controller"]["legal_transition_targets"] == ["Understand", "Failed"]
    assert set(captured["directive_schema"]) == {"action", "transition"}
    assert isinstance(captured["history"], list)


def test_proof_request_compacts_only_provider_history_and_normal_history_is_unchanged():
    task = DebugTask.from_mapping(
        json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text())
    )
    contracts = _test_live_registry().argument_contracts()
    seeded_history = [{"request_index": index} for index in range(40)]
    for proof_required, expected_window in (
        (False, MODEL_HISTORY_WINDOW),
        (True, PROOF_HISTORY_WINDOW),
    ):
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.STATIC_BASELINE,
            config=config(),
            transport=FakeTransport(),
            limits=LiveRunLimits(max_model_requests=1),
            registry=_test_live_registry(),
            proof_required=proof_required,
        )
        adapter.history = list(seeded_history)
        request = adapter._request_context(
            _snapshot(task, ControllerState.REPRODUCE, model_call_index=40),
            logical_request_index=40,
            transport_attempt_index=1,
            contracts=contracts,
            legal_targets=["Understand", "Failed"],
        )
        assert [entry["request_index"] for entry in request["history"]] == list(
            range(40 - expected_window, 40)
        )


def test_proof_request_compacts_large_locals_without_mutating_audit_evidence():
    from agentic_debugger.agent.model_adapter import ControllerSnapshot

    task = DebugTask.from_mapping(
        json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text())
    )

    def scalar(value):
        return {
            "kind": "str",
            "type": "builtins.str",
            "value": value,
            "special": None,
            "size": len(value),
            "items": [],
            "entries": [],
            "truncated": False,
        }

    mapping_value = {
        "kind": "dict",
        "type": "builtins.dict",
        "value": None,
        "special": None,
        "size": 12,
        "items": [],
        "entries": [
            {"key": scalar(f"key-{index}"), "value": scalar("x" * 120)}
            for index in range(12)
        ],
        "truncated": False,
    }
    observation = Observation(
        "observation-locals",
        "action-locals",
        "proof-run",
        task.task_id,
        ActionName.GET_FRAME_LOCALS.value,
        ObservationStatus.OK,
        {
            "state": "paused",
            "frame_id": 0,
            "pause_generation": 1,
            "locals": [
                {"name": f"local_{index}", "value": mapping_value}
                for index in range(4)
            ],
            "proof": {
                "exact_reproduction": True,
                "task_id": task.task_id,
                "reproduction_argv": ["python", "-m", "pytest", "public::node"],
                "pytest_node": "public::node",
                "workspace_id": "workspace-id",
                "production_file": "target.py",
                "production_file_sha256": "a" * 64,
                "breakpoint_line": 12,
                "production_frame": "target",
            },
        },
        "bounded frame locals collected",
        False,
    )
    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=config(),
        transport=FakeTransport(),
        limits=LiveRunLimits(max_model_requests=1),
        registry=_test_live_registry(),
        proof_required=True,
    )
    adapter.history = [
        {
            "request_index": 8,
            "state": ControllerState.RUNTIME_EVIDENCE.value,
            "allowed_actions": [ActionName.NEXT_PDB_SESSION.value],
            "last_observation": observation.to_mapping(),
        }
    ]
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    snapshot = ControllerSnapshot(
        "proof-run",
        task.task_id,
        ControllerState.RUNTIME_EVIDENCE,
        8,
        limits,
        ControllerBudgetState(),
        HypothesisLedger(),
        observation,
    )
    request = adapter._request_context(
        snapshot,
        logical_request_index=8,
        transport_attempt_index=1,
        contracts={},
        legal_targets=[],
        directive_schema={},
    )

    assert request["history"] == []
    projected = request["controller"]["last_observation"]
    assert set(projected["payload"]["proof"]) == {
        "exact_reproduction",
        "production_file",
        "production_frame",
        "breakpoint_line",
    }
    assert projected["payload"]["locals"][0]["value"] == mapping_value
    assert len(json.dumps(request, separators=(",", ":")).encode("utf-8")) <= 25_000
    assert observation.payload["proof"]["production_file_sha256"] == "a" * 64
    assert observation.payload["locals"][0]["value"]["type"] == "builtins.dict"


def test_proof_binding_prefers_scenario_declared_observed_local():
    task = DebugTask.from_mapping(
        json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text())
    )
    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=config(),
        transport=FakeTransport(),
        limits=LiveRunLimits(max_model_requests=1),
        registry=_test_live_registry(),
        proof_required=True,
        proof_observed_local_names=("yaml_dict", "config_dict"),
    )

    def proof_observation(index, name, payload):
        return Observation(
            f"observation-{index}",
            f"action-{index}",
            "proof-run",
            task.task_id,
            name,
            ObservationStatus.OK,
            payload,
            name,
            False,
        )

    adapter._proof_observations = [
        proof_observation(
            1,
            ActionName.START_PDB_SESSION.value,
            {
                "proof": {
                    "production_file": "target.py",
                    "production_frame": "target",
                }
            },
        ),
        proof_observation(
            2,
            ActionName.GET_STACK_SUMMARY.value,
            {
                "pause_generation": 1,
                "frames": [{"frame_id": 0, "is_current": True}],
            },
        ),
        proof_observation(
            3,
            ActionName.GET_FRAME_LOCALS.value,
            {
                "locals": [
                    {"name": "config_dict", "value": {"host_path": "C:/host"}},
                    {"name": "yaml_dict", "value": {"abbreviations": {"local": "url"}}},
                ]
            },
        ),
        proof_observation(4, ActionName.NEXT_PDB_SESSION.value, {"state": "paused"}),
    ]

    bindings = adapter._proof_evidence_bindings()
    assert bindings["observed_values"] == {
        "yaml_dict": {"abbreviations": {"local": "url"}}
    }


def test_current_request_reports_protocol_13():
    task = DebugTask.from_mapping(
        json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text())
    )
    captured = {}

    class ProtocolTransport:
        def request(self, payload, timeout_seconds):
            captured.update(payload)
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "version check"}}

    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=config(),
        transport=ProtocolTransport(),
        limits=LiveRunLimits(max_model_requests=1),
        registry=_test_live_registry(),
    )
    adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))

    assert captured["protocol"]["version"] == "1.3"
    assert config().to_metadata(LiveRunLimits(max_model_requests=1))["protocol_version"] == "1.3"


def test_current_report_validation_rejects_protocol_12_configuration(workspace_parent):
    report = _single_case_report(_case(workspace_parent, FailAfterFirstTransport()))
    assert report["configuration"]["protocol_version"] == "1.3"
    report["configuration"]["protocol_version"] = "1.2"

    with pytest.raises(LiveConfigurationError):
        validate_live_report(report)


def test_transport_request_contract_mutation_does_not_alias_registry_or_global_schema(tmp_path):
    from agentic_debugger.evaluation import live as live_module

    fixture = ROOT / "agentic_debugger/datasets/curated" / TASK_ID
    task = DebugTask.from_mapping(json.loads((fixture / "task.json").read_text()))
    workspace = TaskWorkspace(str(fixture), parent_dir=str(tmp_path))
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    registry = build_registry(context, pdb_policy=PdbPolicy.DISABLED)
    captured = []

    class MutatingTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                payload["action_contracts"]["run_reproduction"]["properties"]["phase"]["enum"].append("transport-mutated")
                payload["directive_schema"]["action"]["required"].append("transport-mutated")
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "mutation check"}}

    try:
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.STATIC_BASELINE,
            config=config(),
            transport=MutatingTransport(),
            limits=LiveRunLimits(max_model_requests=2),
            registry=registry,
        )
        adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE, 0))
        adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE, 1))

        assert captured[1]["action_contracts"]["run_reproduction"]["properties"]["phase"]["enum"] == ["baseline"]
        assert captured[1]["directive_schema"]["action"]["required"] == ["name", "arguments"]
        assert registry.argument_contracts()["run_reproduction"]["properties"]["phase"] == {
            "type": "string",
            "min_length": 1,
        }
        assert live_module.LIVE_DIRECTIVE_SCHEMA["action"]["required"] == ["name", "arguments"]
    finally:
        workspace.cleanup()


def test_state_specific_contract_uses_post_patch_phase_and_authoritative_targets():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class ContextTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "contract check"}}
    from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
    from agentic_debugger.agent.model_adapter import ControllerSnapshot
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=ContextTransport(), limits=LiveRunLimits(max_model_requests=2), registry=_test_live_registry())
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    adapter.next_directive(ControllerSnapshot("run", task.task_id, ControllerState.REPRODUCE, 0, limits, ControllerBudgetState(), HypothesisLedger()))
    adapter.next_directive(ControllerSnapshot("run", task.task_id, ControllerState.VALIDATE, 1, limits, ControllerBudgetState(), HypothesisLedger()))
    assert captured[1]["action_contracts"]["run_reproduction"] == {
        "required": ["phase"],
        "properties": {
            "phase": {"type": "string", "min_length": 1, "enum": ["post_patch"]}
        },
        "additional_properties": False,
    }
    assert captured[1]["controller"]["legal_transition_targets"] == ["Understand", "Patch", "Done", "Failed"]


def _gate_snapshot(task, *, reproduced=True, hypothesis=True, confidence=HypothesisConfidence.LOW,
                   requires_runtime_evidence=False, budget_state=None):
    from agentic_debugger.agent.model_adapter import ControllerSnapshot

    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    ledger = HypothesisLedger()
    if hypothesis:
        ledger = ledger.add(
            limits,
            hypothesis_id="gate-hypothesis",
            statement="runtime evidence may distinguish the cause",
            confidence=confidence,
            requires_runtime_evidence=requires_runtime_evidence,
        )
    observation = Observation(
        "observation-baseline",
        "action-baseline",
        "gate-run",
        task.task_id,
        ActionName.RUN_REPRODUCTION.value,
        ObservationStatus.OK,
        {
            "dispatch_reason": "ok",
            "phase": "baseline",
            "failure_reproduced": reproduced,
        },
        "baseline reproduction executed",
        False,
    )
    return ControllerSnapshot(
        "gate-run",
        task.task_id,
        ControllerState.UNDERSTAND,
        0,
        limits,
        budget_state or ControllerBudgetState(),
        ledger,
        observation,
    )


class _RuntimeTransitionTransport:
    def __init__(self):
        self.payloads = []

    def request(self, payload, timeout_seconds):
        self.payloads.append(payload)
        return {
            "directive": {
                "kind": "transition",
                "target_state": "RuntimeEvidence",
                "reason": "runtime evidence requested",
            }
        }


@pytest.mark.parametrize(
    ("kwargs", "allowed"),
    [
        ({"reproduced": False}, False),
        ({"budget_state": ControllerBudgetState(pdb_observations=8)}, False),
        ({"hypothesis": False}, False),
        ({"confidence": HypothesisConfidence.MEDIUM}, False),
        ({"confidence": HypothesisConfidence.LOW}, True),
        ({"confidence": HypothesisConfidence.HIGH, "requires_runtime_evidence": True}, True),
    ],
)
def test_live_uncertainty_gate_is_machine_enforced(kwargs, allowed):
    task = DebugTask.from_mapping(
        json.loads(
            (ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()
        )
    )
    transport = _RuntimeTransitionTransport()
    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=config(),
        transport=transport,
        limits=LiveRunLimits(max_model_requests=1, max_retries=0),
        registry=_test_live_registry(),
    )
    snapshot = _gate_snapshot(task, **kwargs)
    if allowed:
        directive = adapter.next_directive(snapshot)
        assert directive.target_state is ControllerState.RUNTIME_EVIDENCE
        assert "RuntimeEvidence" in transport.payloads[0]["controller"]["legal_transition_targets"]
    else:
        with pytest.raises(LiveModelAdapterError):
            adapter.next_directive(snapshot)
        assert "RuntimeEvidence" not in transport.payloads[0]["controller"]["legal_transition_targets"]


def test_static_live_policy_does_not_advertise_or_accept_runtime_evidence():
    task = DebugTask.from_mapping(
        json.loads(
            (ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()
        )
    )
    transport = _RuntimeTransitionTransport()
    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=config(),
        transport=transport,
        limits=LiveRunLimits(max_model_requests=1, max_retries=0),
        registry=_test_live_registry(),
    )
    with pytest.raises(LiveModelAdapterError):
        adapter.next_directive(_gate_snapshot(task))
    payload = transport.payloads[0]
    assert "RuntimeEvidence" not in payload["controller"]["legal_transition_targets"]
    assert not any(name.endswith("pdb_session") for name in payload["action_contracts"])


def test_actual_live_registry_is_the_source_of_effective_contract_coherence(tmp_path):
    from agentic_debugger.evaluation.live import _action_contracts_for_state

    fixture = ROOT / "agentic_debugger/datasets/curated" / TASK_ID
    task = DebugTask.from_mapping(json.loads((fixture / "task.json").read_text()))
    workspace = TaskWorkspace(str(fixture), parent_dir=str(tmp_path))
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    registry = build_registry(context, pdb_policy=PdbPolicy.ON_UNCERTAINTY)
    registered = {name.value for name in registry.names()}
    pdb_names = {
        ActionName.GET_FAILURE_TRACE.value,
        ActionName.START_PDB_SESSION.value,
        ActionName.GET_STACK_SUMMARY.value,
        ActionName.GET_FRAME_LOCALS.value,
        ActionName.SAFE_EVAL_EXPRESSION.value,
        ActionName.STOP_PDB_SESSION.value,
    }
    try:
        for state in ControllerState:
            contracts = _action_contracts_for_state(
                state,
                registry=registry,
                policy=DemoPolicy.STATIC_BASELINE,
                session_active=False,
                pdb_available=True,
                pdb_observations_remaining=1,
            )
            expected = {
                action.value
                for action in allowed_actions_for_state(state)
            } & registered
            expected -= pdb_names
            if state is ControllerState.VALIDATE:
                expected.discard(ActionName.CLASSIFY_OUTCOME.value)
            assert set(contracts) == expected
            for contract in contracts.values():
                assert set(contract["required"]) == set(contract["properties"])
                assert contract["additional_properties"] is False

        before = _action_contracts_for_state(
            ControllerState.RUNTIME_EVIDENCE,
            registry=registry,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            session_active=False,
            pdb_available=True,
            pdb_observations_remaining=1,
        )
        active = _action_contracts_for_state(
            ControllerState.RUNTIME_EVIDENCE,
            registry=registry,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            session_active=True,
            pdb_available=True,
            pdb_observations_remaining=1,
        )
        assert ActionName.START_PDB_SESSION.value in before
        assert ActionName.GET_STACK_SUMMARY.value not in before
        assert ActionName.STOP_PDB_SESSION.value not in before
        assert ActionName.START_PDB_SESSION.value not in active
        assert ActionName.GET_STACK_SUMMARY.value in active
        assert ActionName.STOP_PDB_SESSION.value in active
    finally:
        workspace.cleanup()


def test_failure_trace_is_advertised_only_after_successful_baseline(tmp_path):
    from agentic_debugger.agent.model_adapter import ControllerSnapshot

    fixture = ROOT / "agentic_debugger/datasets/curated/pdb-required-boundary-006"
    task = DebugTask.from_mapping(json.loads((fixture / "task.json").read_text()))
    probe = prepare_pdb_probe(fixture, scenario_for(task.task_id), tmp_path, task=task)
    case_dir = tmp_path / "failure-trace-case"
    case_dir.mkdir()
    workspace = TaskWorkspace(str(fixture), parent_dir=str(case_dir))

    class CaptureTransport:
        def __init__(self):
            self.payload = None

        def request(self, payload, timeout_seconds):
            del timeout_seconds
            self.payload = payload
            target = payload["controller"]["legal_transition_targets"][0]
            return {
                "directive": {
                    "kind": "transition",
                    "target_state": target,
                    "reason": "contract captured",
                }
            }

    def advertised(last_observation, *, proof_required=True):
        transport = CaptureTransport()
        context = DemoToolContext(task=task, workspace=workspace, patch="", probe=probe)
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            config=config(),
            transport=transport,
            limits=LiveRunLimits(max_model_requests=1, max_retries=0),
            registry=build_registry(context, pdb_policy=PdbPolicy.ON_UNCERTAINTY),
            proof_required=proof_required,
        )
        adapter.next_directive(
            ControllerSnapshot(
                "failure-trace-run",
                task.task_id,
                ControllerState.REPRODUCE,
                0,
                ControllerBudgetLimits.from_task_constraints(task.constraints),
                ControllerBudgetState(),
                HypothesisLedger(),
                last_observation,
            )
        )
        return set(transport.payload["controller"]["allowed_actions"])

    def baseline(status, failure_reproduced):
        return Observation(
            "baseline-observation",
            "baseline-action",
            "failure-trace-run",
            task.task_id,
            ActionName.RUN_REPRODUCTION.value,
            status,
            {"phase": "baseline", "failure_reproduced": failure_reproduced},
            "baseline",
            False,
        )

    try:
        assert ActionName.GET_FAILURE_TRACE.value not in advertised(None)
        assert ActionName.GET_FAILURE_TRACE.value not in advertised(
            baseline(ObservationStatus.REJECTED, True)
        )
        assert ActionName.GET_FAILURE_TRACE.value not in advertised(
            baseline(ObservationStatus.OK, False)
        )
        assert ActionName.GET_FAILURE_TRACE.value not in advertised(
            baseline(ObservationStatus.OK, True)
        )
        assert ActionName.GET_FAILURE_TRACE.value in advertised(
            baseline(ObservationStatus.OK, True), proof_required=False
        )
    finally:
        workspace.cleanup()
        if probe.source_dir.exists():
            shutil.rmtree(probe.source_dir)


def test_exact_proof_diagnosis_contract_requires_unique_successful_pdb_observations(tmp_path):
    from agentic_debugger.agent.model_adapter import ControllerSnapshot

    fixture = ROOT / "agentic_debugger/datasets/curated/pdb-required-boundary-006"
    task = DebugTask.from_mapping(json.loads((fixture / "task.json").read_text()))
    probe = prepare_pdb_probe(fixture, scenario_for(task.task_id), tmp_path, task=task)
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    workspace = TaskWorkspace(str(fixture), parent_dir=str(case_dir))

    class StopTransport:
        def __init__(self):
            self.payloads = []

        def request(self, payload, timeout_seconds):
            del timeout_seconds
            self.payloads.append(payload)
            allowed = payload["controller"]["allowed_actions"]
            if ActionName.GET_SOURCE_WINDOW.value in allowed:
                return {
                    "directive": {
                        "kind": "action",
                        "name": ActionName.GET_SOURCE_WINDOW.value,
                        "arguments": {"path": "window_tail.py", "line": 1},
                    }
                }
            if ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS.value in allowed:
                return {
                    "directive": {
                        "kind": "action",
                        "name": ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS.value,
                        "arguments": {
                            "hypothesis_id": "contract-check",
                            "statement": "contract check",
                            "target_file": "window_tail.py",
                            "target_symbol": "tail_window",
                            "confidence": "low",
                            "evidence_refs": [],
                            "observed_values": {},
                        },
                    }
                }
            raise AssertionError("test request exposed no inspectable action")

    proof_contract = {
        "exact_reproduction": True,
        "task_id": task.task_id,
        "reproduction_argv": list(probe.reproduction_argv),
        "pytest_node": probe.reproduction_node,
        "workspace_id": probe.workspace_id,
        "production_file": probe.script,
        "production_file_sha256": probe.production_file_sha256,
        "breakpoint_line": probe.breakpoint_line,
        "production_frame": probe.focus_function,
    }

    def observation(name, index, status=ObservationStatus.OK, payload=None):
        return Observation(
            f"proof-observation-{index}",
            f"proof-action-{index}",
            "proof-contract-run",
            task.task_id,
            name,
            status,
            dict(payload or {}),
            name,
            False,
        )

    required = [
        observation(ActionName.START_PDB_SESSION.value, 1, payload={
            "state": "paused",
            "script": probe.script,
            "function": probe.focus_function,
            "line": probe.breakpoint_line,
            "proof": proof_contract,
        }),
        observation(ActionName.GET_STACK_SUMMARY.value, 2, payload={
            "frames": [{
                "frame_id": 0,
                "is_current": True,
                "script": probe.script,
                "function": probe.focus_function,
            }],
            "proof": proof_contract,
        }),
        observation(ActionName.GET_FRAME_LOCALS.value, 3, payload={
            "state": "paused",
            "frame_id": 0,
            "locals": [{"name": "values", "value": "[1, 2, 3]"}],
            "proof": proof_contract,
        }),
        observation(ActionName.STEP_PDB_SESSION.value, 4, payload={
            "state": "paused",
            "script": probe.script,
            "function": probe.focus_function,
            "proof": proof_contract,
        }),
    ]
    baseline = observation("run_reproduction", 0, payload={
        "phase": "baseline",
        "failure_reproduced": True,
        "node_id": probe.reproduction_node,
        "reproduction_argv": list(probe.reproduction_argv),
    })

    def request_for(proof_observations):
        transport = StopTransport()
        context = DemoToolContext(
            task=task,
            workspace=workspace,
            patch="",
            probe=probe,
        )
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            config=config(),
            transport=transport,
            limits=LiveRunLimits(max_model_requests=1, max_retries=0),
            registry=build_registry(context, pdb_policy=PdbPolicy.ON_UNCERTAINTY),
            proof_required=True,
        )
        adapter._proof_observations = [baseline, *proof_observations]
        adapter.next_directive(
            ControllerSnapshot(
                "proof-contract-run",
                task.task_id,
                ControllerState.UNDERSTAND,
                0,
                ControllerBudgetLimits.from_task_constraints(task.constraints),
                ControllerBudgetState(),
                HypothesisLedger(),
                    baseline,
            )
        )
        return transport.payloads[0]

    try:
        assert "express_root_cause_hypothesis" not in request_for([])["controller"]["allowed_actions"]
        assert "express_root_cause_hypothesis" not in request_for([
            observation("get_failure_trace", 10),
        ])["controller"]["allowed_actions"]
        assert "express_root_cause_hypothesis" not in request_for([
            *required[:3],
            observation(ActionName.STEP_PDB_SESSION.value, 4, ObservationStatus.REJECTED),
        ])["controller"]["allowed_actions"]
        assert "express_root_cause_hypothesis" not in request_for([
            *required[:3],
            observation(ActionName.SAFE_EVAL_EXPRESSION.value, 7),
        ])["controller"]["allowed_actions"]
        assert "express_root_cause_hypothesis" not in request_for([
            *required,
            observation(ActionName.GET_STACK_SUMMARY.value, 5),
        ])["controller"]["allowed_actions"]
        ready = request_for(required)
        assert "express_root_cause_hypothesis" in ready["controller"]["allowed_actions"]
        next_ready = request_for([
            *required[:3],
            observation(ActionName.NEXT_PDB_SESSION.value, 6, payload={
                "state": "paused",
                "script": probe.script,
                "function": probe.focus_function,
                "proof": proof_contract,
            }),
        ])
        assert "express_root_cause_hypothesis" in next_ready["controller"]["allowed_actions"]
        assert "afterward revise from observation ids, then diagnose" in ready["instructions"]
        assert "oracle" not in json.dumps(ready).lower()
        assert "gold patch" not in json.dumps(ready).lower()
    finally:
        workspace.cleanup()
        if probe.source_dir.exists():
            shutil.rmtree(probe.source_dir)


def _runtime_budget_snapshot(task, pdb_observations: int):
    from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
    from agentic_debugger.agent.model_adapter import ControllerSnapshot

    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    return ControllerSnapshot(
        "runtime-budget-run",
        task.task_id,
        ControllerState.RUNTIME_EVIDENCE,
        0,
        limits,
        ControllerBudgetState(pdb_observations=pdb_observations),
        HypothesisLedger(),
    )


def _pdb_registry_case(tmp_path):
    fixture = ROOT / "agentic_debugger/datasets/curated" / TASK_ID
    task = DebugTask.from_mapping(json.loads((fixture / "task.json").read_text()))
    workspace = TaskWorkspace(str(fixture), parent_dir=str(tmp_path))
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    return task, workspace, build_registry(context, pdb_policy=PdbPolicy.ON_UNCERTAINTY)


def test_zero_pdb_budget_filters_active_observations_but_keeps_stop(tmp_path):
    task, workspace, registry = _pdb_registry_case(tmp_path)
    captured = []

    class StopTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "budget check"}}

    try:
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            config=config(),
            transport=StopTransport(),
            limits=LiveRunLimits(max_model_requests=1),
            registry=registry,
        )
        adapter._runtime_transition_authorized = True
        adapter._pdb_session_active = True
        adapter.next_directive(_runtime_budget_snapshot(task, task.constraints.max_pdb_observations))
        actions = set(captured[0]["action_contracts"])
        observation_actions = {
            name.value
            for name in registry.names()
            if budget_kind_for_action(name) is BudgetKind.PDB_OBSERVATIONS
        }
        assert not actions & observation_actions
        assert ActionName.STOP_PDB_SESSION.value in actions
    finally:
        workspace.cleanup()


def test_zero_pdb_budget_filters_inactive_start_and_session_actions(tmp_path):
    task, workspace, registry = _pdb_registry_case(tmp_path)
    captured = []

    class InactiveTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "budget check"}}

    try:
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            config=config(),
            transport=InactiveTransport(),
            limits=LiveRunLimits(max_model_requests=1),
            registry=registry,
        )
        adapter._runtime_transition_authorized = True
        adapter.next_directive(_runtime_budget_snapshot(task, task.constraints.max_pdb_observations))
        actions = set(captured[0]["action_contracts"])
        assert ActionName.START_PDB_SESSION.value not in actions
        assert ActionName.STOP_PDB_SESSION.value not in actions
        assert not any(
            budget_kind_for_action(ActionName(name)) is BudgetKind.PDB_OBSERVATIONS
            for name in actions
        )
    finally:
        workspace.cleanup()


def test_positive_pdb_budget_preserves_start_observe_stop_lifecycle(tmp_path):
    task, workspace, registry = _pdb_registry_case(tmp_path)
    captured = []

    class LifecycleTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "budget check"}}

    try:
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            config=config(),
            transport=LifecycleTransport(),
            limits=LiveRunLimits(max_model_requests=2),
            registry=registry,
        )
        adapter._runtime_transition_authorized = True
        adapter.next_directive(_runtime_budget_snapshot(task, 0))
        before = set(captured[0]["action_contracts"])
        assert ActionName.START_PDB_SESSION.value in before
        assert ActionName.GET_STACK_SUMMARY.value not in before
        assert ActionName.STOP_PDB_SESSION.value not in before

        adapter._pdb_session_active = True
        adapter.next_directive(_runtime_budget_snapshot(task, 0))
        active = set(captured[1]["action_contracts"])
        assert ActionName.START_PDB_SESSION.value not in active
        assert ActionName.GET_STACK_SUMMARY.value in active
        assert ActionName.STOP_PDB_SESSION.value in active
    finally:
        workspace.cleanup()


def test_execution_control_exit_clears_stale_active_session_contract(tmp_path):
    from agentic_debugger.agent.model_adapter import ControllerSnapshot

    task, workspace, registry = _pdb_registry_case(tmp_path)
    captured = []

    class ExitTransport:
        def request(self, payload, timeout_seconds):
            del timeout_seconds
            captured.append(payload)
            return {
                "directive": {
                    "kind": "transition",
                    "target_state": "Failed",
                    "reason": "session-state check",
                }
            }

    exited = Observation(
        "observation-session-exited",
        "action-session-exited",
        "runtime-session-run",
        task.task_id,
        ActionName.CONTINUE_PDB_SESSION.value,
        ObservationStatus.OK,
        {"state": "exited", "exit_code": 0},
        "target exited",
        False,
    )
    try:
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            config=config(),
            transport=ExitTransport(),
            limits=LiveRunLimits(max_model_requests=1),
            registry=registry,
        )
        adapter._runtime_transition_authorized = True
        adapter._pdb_session_active = True
        adapter.next_directive(
            ControllerSnapshot(
                "runtime-session-run",
                task.task_id,
                ControllerState.RUNTIME_EVIDENCE,
                0,
                ControllerBudgetLimits.from_task_constraints(task.constraints),
                ControllerBudgetState(pdb_observations=1),
                HypothesisLedger(),
                exited,
            )
        )
        actions = set(captured[0]["action_contracts"])
        assert ActionName.START_PDB_SESSION.value in actions
        assert ActionName.GET_STACK_SUMMARY.value not in actions
        assert ActionName.STOP_PDB_SESSION.value not in actions
    finally:
        workspace.cleanup()


def test_exhausted_pdb_observation_is_illegal_action_and_recovers_to_stop(tmp_path):
    task, workspace, registry = _pdb_registry_case(tmp_path)
    captured = []

    class ExhaustedObservationTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                return {"directive": {"kind": "action", "name": ActionName.GET_STACK_SUMMARY.value, "arguments": {}}}
            return {"directive": {"kind": "action", "name": ActionName.STOP_PDB_SESSION.value, "arguments": {}}}

    try:
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            config=config(),
            transport=ExhaustedObservationTransport(),
            limits=LiveRunLimits(max_model_requests=2, max_retries=1),
            registry=registry,
        )
        adapter._runtime_transition_authorized = True
        adapter._pdb_session_active = True
        directive = adapter.next_directive(_runtime_budget_snapshot(task, task.constraints.max_pdb_observations))
        assert directive.name is ActionName.STOP_PDB_SESSION
        assert ActionName.GET_STACK_SUMMARY.value not in captured[0]["action_contracts"]
        assert ActionName.STOP_PDB_SESSION.value in captured[0]["action_contracts"]
        assert captured[1]["directive_feedback"]["category"] == "illegal_action"
        assert captured[0]["protocol"]["request_id"] != captured[1]["protocol"]["request_id"]
        assert captured[0]["protocol"]["transport_attempt_index"] == 1
        assert captured[1]["protocol"]["transport_attempt_index"] == 2
        assert adapter.metrics.model_requests == 2
        assert adapter.metrics.model_responses == 2
        assert adapter.metrics.retries == 1
    finally:
        workspace.cleanup()


def test_registry_argument_contract_matches_validator_constraints(tmp_path):
    fixture = ROOT / "agentic_debugger/datasets/curated" / TASK_ID
    task = DebugTask.from_mapping(json.loads((fixture / "task.json").read_text()))
    workspace = TaskWorkspace(str(fixture), parent_dir=str(tmp_path))
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    registry = build_registry(context, pdb_policy=PdbPolicy.ON_UNCERTAINTY)
    try:
        contracts = registry.argument_contracts()
        find_contract = contracts[ActionName.FIND_FUNCTION.value]
        window_contract = contracts[ActionName.GET_SOURCE_WINDOW.value]
        hypothesis_contract = contracts[ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS.value]

        assert find_contract["required"] == ["name", "path"]
        assert find_contract["properties"]["name"] == {
            "type": "string",
            "min_length": 1,
        }
        assert find_contract["additional_properties"] is False
        assert window_contract["properties"]["line"] == {
            "type": "integer",
            "minimum": 1,
        }
        assert hypothesis_contract["properties"]["confidence"]["enum"] == [
            item.value for item in HypothesisConfidence
        ]

        invalid_cases = (
            (ActionName.FIND_FUNCTION, {"name": "", "path": "demo.py"}),
            (ActionName.GET_SOURCE_WINDOW, {"path": "demo.py", "line": 0}),
            (
                ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS,
                {
                    "hypothesis_id": "h1",
                    "statement": "root cause",
                    "target_file": "demo.py",
                    "target_symbol": "run",
                    "confidence": "certain",
                },
            ),
            (ActionName.FIND_FUNCTION, {"name": "run"}),
            (ActionName.FIND_FUNCTION, {"name": "run", "path": "demo.py", "extra": 1}),
        )
        for name, arguments in invalid_cases:
            with pytest.raises(ToolRejectedError):
                registry.get(name).argument_validator(arguments)

        assert registry.get(ActionName.FIND_FUNCTION).argument_validator(
            {"name": "run", "path": "demo.py"}
        ) == {"name": "run", "path": "demo.py"}
        assert registry.get(ActionName.GET_SOURCE_WINDOW).argument_validator(
            {"path": "demo.py", "line": 1}
        ) == {"path": "demo.py", "line": 1}
        assert registry.get(ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS).argument_validator(
            {
                "hypothesis_id": "h1",
                "statement": "root cause",
                "target_file": "demo.py",
                "target_symbol": "run",
                "confidence": "low",
            }
        )["confidence"] == "low"
    finally:
        workspace.cleanup()


def test_state_illegal_hypothesis_directive_gets_bounded_feedback_and_recovers():
    task = DebugTask.from_mapping(
        json.loads(
            (ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()
        )
    )
    captured = []

    class IllegalHypothesisThenValid:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                return {
                    "directive": {
                        "kind": "add_hypothesis",
                        "hypothesis_id": "h1",
                        "statement": "root cause",
                        "confidence": "low",
                        "evidence_refs": [],
                        "requires_runtime_evidence": False,
                    }
                }
            return {
                "directive": {
                    "kind": "transition",
                    "target_state": "Failed",
                    "reason": "recovered",
                }
            }

    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=config(),
        transport=IllegalHypothesisThenValid(),
        limits=LiveRunLimits(max_model_requests=2, max_retries=1),
        registry=_test_live_registry(),
    )
    directive = adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))
    assert directive.target_state is ControllerState.FAILED
    assert captured[1]["directive_feedback"]["category"] == "illegal_action"
    assert "add_hypothesis" in captured[1]["directive_feedback"]["message"]


def test_provider_completed_invalid_directive_retries_and_retains_each_usage_and_identity():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class InvalidThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                return {"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}, "directive": {"kind": "transition", "target_state": "NotAState", "reason": "invalid"}}
            return {"usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11}, "directive": {"kind": "transition", "target_state": "Failed", "reason": "recovered"}}
    from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
    from agentic_debugger.agent.model_adapter import ControllerSnapshot
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=InvalidThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    directive = adapter.next_directive(ControllerSnapshot("retry-run", task.task_id, ControllerState.REPRODUCE, 0, limits, ControllerBudgetState(), HypothesisLedger()))
    assert directive.target_state is ControllerState.FAILED
    assert adapter.metrics.model_requests == 2
    assert adapter.metrics.model_responses == 2
    assert adapter.metrics.retries == 1
    assert adapter.metrics.to_mapping()["token_usage"] == {"prompt_tokens": 8, "completion_tokens": 10, "total_tokens": 18, "provider_reported": True, "missing_fields": []}
    assert captured[0]["protocol"]["logical_model_call_index"] == captured[1]["protocol"]["logical_model_call_index"] == 0
    assert captured[0]["protocol"]["transport_attempt_index"] == 1
    assert captured[1]["protocol"]["transport_attempt_index"] == 2
    assert captured[0]["protocol"]["request_id"] != captured[1]["protocol"]["request_id"]
    assert len(adapter.history) == 1
    assert captured[0]["directive_feedback"] is None
    assert captured[1]["directive_feedback"] == {"category": "malformed_directive", "message": "unrecognized target_state", "rejected_transport_attempt": 1}


def test_stream_activity_is_aggregated_without_reasoning_content():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))

    class ActivityTransport:
        def request(self, payload, timeout_seconds):
            return {
                "directive": {
                    "kind": "transition",
                    "target_state": "Failed",
                    "reason": "bounded test",
                },
                "transport_activity": {
                    "stream_frame_count": 7,
                    "thinking_bytes": 1234,
                    "content_bytes": 81,
                },
            }

    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=config(),
        transport=ActivityTransport(),
        limits=LiveRunLimits(max_model_requests=1, max_retries=0),
        registry=_test_live_registry(),
    )
    directive = adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))
    assert directive.target_state is ControllerState.FAILED
    metrics = adapter.metrics.to_mapping()
    assert metrics["stream_frame_count"] == 7
    assert metrics["thinking_bytes"] == 1234
    assert metrics["action_content_bytes"] == 81


@pytest.mark.parametrize(
    "bad_kind",
    [[], {}, None, True, 1, "unknown-kind"],
    ids=["array", "object", "null", "boolean", "number", "unknown-string"],
)
def test_non_string_or_unknown_directive_kind_is_bounded_and_recovers(bad_kind):
    task = DebugTask.from_mapping(
        json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text())
    )
    captured = []

    class BadKindThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
            if len(captured) == 1:
                return {"usage": usage, "directive": {"kind": bad_kind}}
            return {
                "usage": usage,
                "directive": {
                    "kind": "transition",
                    "target_state": "Failed",
                    "reason": "recovered",
                },
            }

    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=config(),
        transport=BadKindThenValidTransport(),
        limits=LiveRunLimits(max_model_requests=2, max_retries=1),
        registry=_test_live_registry(),
    )
    directive = adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))

    assert directive.target_state is ControllerState.FAILED
    assert adapter.metrics.model_requests == 2
    assert adapter.metrics.model_responses == 2
    assert adapter.metrics.retries == 1
    assert adapter.metrics.to_mapping()["token_usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 6,
        "total_tokens": 10,
        "provider_reported": True,
        "missing_fields": [],
    }
    assert captured[0]["protocol"]["transport_attempt_index"] == 1
    assert captured[1]["protocol"]["transport_attempt_index"] == 2
    assert captured[0]["protocol"]["request_id"] != captured[1]["protocol"]["request_id"]
    assert captured[0]["directive_feedback"] is None
    feedback = captured[1]["directive_feedback"]
    assert feedback["category"] == "malformed_directive"
    assert feedback["message"] == "unrecognized or missing directive 'kind'"
    assert feedback["rejected_transport_attempt"] == 1


def test_non_string_directive_kind_terminates_without_retry_and_retains_usage():
    task = DebugTask.from_mapping(
        json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text())
    )

    class InvalidKindTransport:
        def request(self, payload, timeout_seconds):
            return {
                "usage": {"prompt_tokens": 7, "completion_tokens": 8, "total_tokens": 15},
                "directive": {"kind": []},
            }

    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=config(),
        transport=InvalidKindTransport(),
        limits=LiveRunLimits(max_model_requests=1, max_retries=0),
        registry=_test_live_registry(),
    )
    with pytest.raises(LiveModelAdapterError):
        adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))

    assert adapter.metrics.termination_reason == "directive_rejected"
    assert adapter.metrics.provider_errors == 0
    assert adapter.metrics.directive_rejections == 1
    assert adapter.metrics.model_requests == 1
    assert adapter.metrics.model_responses == 1
    assert adapter.metrics.retries == 0
    assert adapter.metrics.to_mapping()["token_usage"]["total_tokens"] == 15


def _snapshot(task, state, model_call_index=0):
    from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
    from agentic_debugger.agent.model_adapter import ControllerSnapshot
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    return ControllerSnapshot("category-run", task.task_id, state, model_call_index, limits, ControllerBudgetState(), HypothesisLedger())


def test_model_selected_breakpoint_is_positive_integer_without_source_line_steering():
    task = DebugTask.from_mapping(
        json.loads(
            (ROOT / "agentic_debugger/datasets/curated/pdb-required-boundary-006/task.json")
            .read_text()
        )
    )
    registry = ToolRegistry(
        (
            ToolSpec(
                ActionName.START_PDB_SESSION,
                lambda arguments: dict(arguments),
                lambda _action, _arguments: ToolResult(ObservationStatus.OK, {}, "ok"),
                argument_contract={
                    "required": ["breakpoint_line"],
                    "properties": {
                        "breakpoint_line": {"type": "integer", "minimum": 1},
                    },
                    "additional_properties": False,
                },
            ),
        )
    )

    class Transport:
        def request(self, payload, timeout_seconds):
            assert payload["action_contracts"]["start_pdb_session"]["properties"]["breakpoint_line"] == {
                "type": "integer",
                "minimum": 1,
            }
            return {
                "directive": {
                    "kind": "action",
                    "name": "start_pdb_session",
                    "arguments": {"breakpoint_line": 58},
                }
            }

    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=config(),
        transport=Transport(),
        limits=LiveRunLimits(max_model_requests=1),
        registry=registry,
        proof_required=True,
    )
    snapshot = _snapshot(task, ControllerState.RUNTIME_EVIDENCE)
    adapter._runtime_transition_authorized = True
    contract = adapter._effective_contract(snapshot)
    assert contract["start_pdb_session"]["properties"]["breakpoint_line"] == {
        "type": "integer",
        "minimum": 1,
    }
    directive = adapter.next_directive(snapshot)
    assert directive.name is ActionName.START_PDB_SESSION
    assert directive.arguments == {"breakpoint_line": 58}


def test_illegal_action_rejection_carries_category_and_recovers_on_retry():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class IllegalActionThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                # extract_failing_test is a real ActionName but is only legal in Understand, not Reproduce.
                return {"directive": {"kind": "action", "name": "extract_failing_test", "arguments": {}}}
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "recovered"}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=IllegalActionThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    directive = adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))
    assert directive.target_state is ControllerState.FAILED
    assert captured[0]["directive_feedback"] is None
    feedback = captured[1]["directive_feedback"]
    assert feedback["category"] == "illegal_action"
    assert "extract_failing_test" in feedback["message"]
    assert feedback["rejected_transport_attempt"] == 1


def test_illegal_transition_rejection_carries_category_and_recovers_on_retry():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class IllegalTransitionThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                # Patch is not reachable directly from Reproduce.
                return {"directive": {"kind": "transition", "target_state": "Patch", "reason": "skip ahead"}}
            return {"directive": {"kind": "transition", "target_state": "Understand", "reason": "recovered"}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=IllegalTransitionThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    directive = adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))
    assert directive.target_state is ControllerState.UNDERSTAND
    feedback = captured[1]["directive_feedback"]
    assert feedback["category"] == "illegal_transition"
    assert "Patch" in feedback["message"]


def test_invalid_argument_value_rejection_for_reproduction_phase_and_hypothesis_confidence():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class BadPhaseThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                return {"directive": {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "post_patch"}}}
            return {"directive": {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=BadPhaseThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    directive = adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))
    assert directive.arguments["phase"] == "baseline"
    feedback = captured[1]["directive_feedback"]
    assert feedback["category"] == "invalid_argument_value"
    assert "phase" in feedback["message"]

    captured2 = []
    class BadConfidenceThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured2.append(payload)
            if len(captured2) == 1:
                return {"directive": {"kind": "add_hypothesis", "hypothesis_id": "h1", "statement": "root cause", "confidence": "extreme", "evidence_refs": [], "requires_runtime_evidence": False}}
            return {"directive": {"kind": "add_hypothesis", "hypothesis_id": "h1", "statement": "root cause", "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": False}}
    adapter2 = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=BadConfidenceThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    directive2 = adapter2.next_directive(_snapshot(task, ControllerState.UNDERSTAND))
    assert directive2.hypothesis_id == "h1"
    feedback2 = captured2[1]["directive_feedback"]
    assert feedback2["category"] == "invalid_argument_value"
    assert "confidence" in feedback2["message"]


def test_malformed_directive_rejection_for_unknown_kind_and_missing_field():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class UnknownKindThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                return {"directive": {"kind": "bogus-kind"}}
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "recovered"}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=UnknownKindThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    directive = adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))
    assert directive.target_state is ControllerState.FAILED
    feedback = captured[1]["directive_feedback"]
    assert feedback["category"] == "malformed_directive"

    captured2 = []
    class MissingReasonThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured2.append(payload)
            if len(captured2) == 1:
                return {"directive": {"kind": "transition", "target_state": "Failed"}}
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "recovered"}}
    adapter2 = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=MissingReasonThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    adapter2.next_directive(_snapshot(task, ControllerState.REPRODUCE))
    feedback2 = captured2[1]["directive_feedback"]
    assert feedback2["category"] == "malformed_directive"
    assert "reason" in feedback2["message"]


def test_add_hypothesis_missing_evidence_refs_is_rejected_and_corrected_on_retry():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class MissingEvidenceRefsThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                return {"directive": {"kind": "add_hypothesis", "hypothesis_id": "h1", "statement": "root cause", "confidence": "low", "requires_runtime_evidence": False}}
            return {"directive": {"kind": "add_hypothesis", "hypothesis_id": "h1", "statement": "root cause", "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": False}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=MissingEvidenceRefsThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    directive = adapter.next_directive(_snapshot(task, ControllerState.UNDERSTAND))
    assert directive.hypothesis_id == "h1"
    assert directive.evidence_refs == ()
    assert captured[0]["directive_feedback"] is None
    feedback = captured[1]["directive_feedback"]
    assert feedback["category"] == "malformed_directive"
    assert "evidence_refs" in feedback["message"]


def test_add_hypothesis_missing_requires_runtime_evidence_is_rejected():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class MissingRequiresRuntimeEvidenceThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                return {"directive": {"kind": "add_hypothesis", "hypothesis_id": "h1", "statement": "root cause", "confidence": "low", "evidence_refs": []}}
            return {"directive": {"kind": "add_hypothesis", "hypothesis_id": "h1", "statement": "root cause", "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": False}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=MissingRequiresRuntimeEvidenceThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    adapter.next_directive(_snapshot(task, ControllerState.UNDERSTAND))
    feedback = captured[1]["directive_feedback"]
    assert feedback["category"] == "malformed_directive"
    assert "requires_runtime_evidence" in feedback["message"]


def test_hypothesis_evidence_refs_non_array_shapes_are_rejected_not_coerced():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))

    def add_hypothesis_directive(evidence_refs):
        return {"kind": "add_hypothesis", "hypothesis_id": "h1", "statement": "root cause", "confidence": "low", "evidence_refs": evidence_refs, "requires_runtime_evidence": False}

    for bad_evidence_refs in ("abc", {"x": "y"}, 1, 1.5, True, None):
        captured = []
        class BadShapeThenValidTransport:
            def request(self, payload, timeout_seconds):
                captured.append(payload)
                if len(captured) == 1:
                    return {"directive": add_hypothesis_directive(bad_evidence_refs)}
                return {"directive": add_hypothesis_directive([])}
        adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=BadShapeThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
        directive = adapter.next_directive(_snapshot(task, ControllerState.UNDERSTAND))
        assert directive.evidence_refs == (), bad_evidence_refs
        feedback = captured[1]["directive_feedback"]
        assert feedback["category"] == "malformed_directive", bad_evidence_refs
        assert "evidence_refs" in feedback["message"]
        # The rejected value itself must never be echoed back into the bounded feedback message.
        assert "abc" not in feedback["message"] and "x" not in feedback["message"]


def test_revise_hypothesis_evidence_refs_non_array_shape_is_rejected_not_coerced():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class BadShapeThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                return {"directive": {"kind": "revise_hypothesis", "hypothesis_id": "h1", "statement": "root cause", "confidence": "low", "evidence_refs": "abc", "requires_runtime_evidence": False}}
            return {"directive": {"kind": "revise_hypothesis", "hypothesis_id": "h1", "statement": "root cause", "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": False}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=BadShapeThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    directive = adapter.next_directive(_snapshot(task, ControllerState.UNDERSTAND))
    assert directive.evidence_refs == ()
    feedback = captured[1]["directive_feedback"]
    assert feedback["category"] == "malformed_directive"
    assert "evidence_refs" in feedback["message"]


def test_hypothesis_valid_json_array_evidence_refs_is_accepted_unchanged():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class ValidArrayTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            return {"directive": {"kind": "add_hypothesis", "hypothesis_id": "h1", "statement": "root cause", "confidence": "low", "evidence_refs": ["obs-1", "obs-2"], "requires_runtime_evidence": False}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=ValidArrayTransport(), limits=LiveRunLimits(max_model_requests=1, max_retries=0), registry=_test_live_registry())
    directive = adapter.next_directive(_snapshot(task, ControllerState.UNDERSTAND))
    assert directive.evidence_refs == ("obs-1", "obs-2")
    assert captured[0]["directive_feedback"] is None


def test_request_instructions_distinguish_null_from_non_null_directive_feedback():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class InvalidThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                return {"directive": {"kind": "bogus-kind"}}
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "recovered"}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=InvalidThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))
    assert captured[0]["directive_feedback"] is None
    assert captured[1]["directive_feedback"] is not None
    for payload in captured:
        assert "directive_feedback" in payload
        assert "non-null" in payload["instructions"]


def test_ambiguous_response_envelope_is_rejected_rather_than_silently_resolved():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class AmbiguousThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                return {
                    "kind": "transition", "target_state": "Understand", "reason": "top-level directive",
                    "directive": {"kind": "transition", "target_state": "Failed", "reason": "nested directive"},
                }
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "recovered"}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=AmbiguousThenValidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    directive = adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))
    assert directive.target_state is ControllerState.FAILED
    feedback = captured[1]["directive_feedback"]
    assert feedback["category"] == "ambiguous_response_envelope"


def test_transport_failure_does_not_carry_forward_prior_directive_rejection_feedback():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class InvalidThenTransportErrorThenValidTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if len(captured) == 1:
                return {"directive": {"kind": "bogus-kind"}}
            if len(captured) == 2:
                raise LiveTransportError("provider unavailable", kind="provider_failure")
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "recovered"}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=InvalidThenTransportErrorThenValidTransport(), limits=LiveRunLimits(max_model_requests=3, max_retries=2), registry=_test_live_registry())
    directive = adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))
    assert directive.target_state is ControllerState.FAILED
    assert captured[0]["directive_feedback"] is None
    assert captured[1]["directive_feedback"]["category"] == "malformed_directive"
    assert captured[2]["directive_feedback"] is None


def test_repeating_an_illegal_action_after_feedback_is_measurable_and_terminates_at_retry_limit():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    captured = []
    class AlwaysIllegalActionTransport:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            return {"directive": {"kind": "action", "name": "extract_failing_test", "arguments": {}}}
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=AlwaysIllegalActionTransport(), limits=LiveRunLimits(max_model_requests=3, max_retries=2), registry=_test_live_registry())
    with pytest.raises(LiveModelAdapterError):
        adapter.next_directive(_snapshot(task, ControllerState.REPRODUCE))
    assert adapter.metrics.termination_reason == "directive_rejected"
    assert adapter.metrics.provider_errors == 0
    assert adapter.metrics.directive_rejections == 3
    assert adapter.metrics.model_requests == 3
    assert adapter.metrics.retries == 2
    assert captured[0]["directive_feedback"] is None
    assert all(entry["directive_feedback"]["category"] == "illegal_action" for entry in captured[1:])


def test_rejection_detail_is_bounded_to_max_chars():
    long_detail = "x" * (MAX_REJECTION_DETAIL_CHARS + 50)
    error = LiveModelAdapterError("invalid model directive", category=DirectiveRejectionCategory.MALFORMED_DIRECTIVE, detail=long_detail)
    assert len(error.detail) == MAX_REJECTION_DETAIL_CHARS
    assert error.detail.endswith("...")

    short_detail = "unrecognized action name"
    assert LiveModelAdapterError("invalid model directive", detail=short_detail).detail == short_detail


def test_all_provider_completed_invalid_directives_terminate_as_model_directive_rejected():
    task = DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))
    class InvalidTransport:
        def request(self, payload, timeout_seconds):
            return {"usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}, "directive": {"kind": "not-a-directive"}}
    from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
    from agentic_debugger.agent.model_adapter import ControllerSnapshot
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=InvalidTransport(), limits=LiveRunLimits(max_model_requests=2, max_retries=1), registry=_test_live_registry())
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    with pytest.raises(LiveModelAdapterError):
        adapter.next_directive(ControllerSnapshot("invalid-run", task.task_id, ControllerState.REPRODUCE, 0, limits, ControllerBudgetState(), HypothesisLedger()))
    assert adapter.metrics.termination_reason == "directive_rejected"
    assert adapter.metrics.provider_errors == 0
    assert adapter.metrics.directive_rejections == 2
    assert adapter.metrics.model_requests == 2
    assert adapter.metrics.model_responses == 2
    assert adapter.metrics.retries == 1
    assert adapter.metrics.to_mapping()["token_usage"]["total_tokens"] == 10


def _run_single_taxonomy_case(response=None, *, failure=None):
    class Transport:
        def request(self, payload, timeout_seconds):
            if failure is not None:
                raise failure
            return response

    parent = Path(tempfile.mkdtemp(prefix="live-directive-taxonomy-"))
    try:
        return run_live_case(
            repository_root=str(ROOT),
            task_id=TASK_ID,
            policy=DemoPolicy.STATIC_BASELINE,
            repetition=1,
            workspace_parent=str(parent),
            config=config(),
            limits=LiveRunLimits(max_model_requests=1, max_controller_steps=1, max_retries=0, continue_on_task_failure=False),
            transport=Transport(),
            retain_observable_model_directives=True,
        )
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_actual_transport_failure_is_provider_error_not_directive_rejection():
    result = _run_single_taxonomy_case(failure=LiveTransportError("provider unavailable", kind="provider_failure"))
    assert result.status is LiveCaseStatus.PROVIDER_ERROR
    assert result.measurements["provider_error_count"] == 1
    assert result.measurements["directive_rejection_count"] == 0
    assert result.measurements["termination_reason"] == "provider_or_transport_error"


def test_live_model_call_limit_is_budget_limited_with_replayable_terminal_transition():
    result = _run_single_taxonomy_case(
        {
            "directive": {
                "kind": "action",
                "name": "run_reproduction",
                "arguments": {"phase": "baseline"},
            }
        }
    )
    assert result.status is LiveCaseStatus.BUDGET_LIMITED
    assert result.controller["stop_reason"] == "model_call_limit"
    trajectory = replay_events(result.events_jsonl)
    assert trajectory.events[-2].payload == {
        "source_state": "Reproduce",
        "target_state": "Failed",
        "reason": "model_call_limit",
    }
    assert trajectory.events[-1].payload["stop_reason"] == "model_call_limit"


@pytest.mark.parametrize("response", [
    {"provider_completion_schema_version": "provider-completion-v1", "directive_content": "not-json", "transport_activity": {"thinking_bytes": 17}},
    {"directive": {"kind": "action", "name": "extract_failing_test", "arguments": {}}},
    {"directive": {"kind": "action", "name": "run_reproduction", "arguments": {"phase": 3}}},
])
def test_provider_completed_rejection_is_non_provider_terminal_taxonomy(response):
    result = _run_single_taxonomy_case(response)
    assert result.status is LiveCaseStatus.MODEL_DIRECTIVE_REJECTED
    assert result.measurements["provider_error_count"] == 0
    assert result.measurements["directive_rejection_count"] == 1
    assert result.measurements["directive_rejection_categories"]
    assert result.measurements["termination_reason"] == "directive_rejected"
    assert result.evidence is not None
    evidence = result.evidence["observable_model_rejection_evidence"]
    assert evidence
    if "directive_content" in response:
        assert evidence[0]["content_representation"]["text"] == "not-json"
    assert "thinking_bytes" not in json.dumps(evidence)


def test_provider_completed_accepted_directive_has_no_rejection_or_provider_error():
    result = _run_single_taxonomy_case({"directive": {"kind": "transition", "target_state": "Failed", "reason": "accepted terminal transition"}})
    assert result.status is LiveCaseStatus.CONTROLLER_FAILED
    assert result.measurements["provider_error_count"] == 0
    assert result.measurements["directive_rejection_count"] == 0
    assert result.evidence["observable_model_rejection_evidence"] == []


def test_jsonl_wrapper_convention_keeps_provider_completed_invalid_directive_on_success_exit():
    response = json.dumps({"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, "directive": {"kind": "not-a-directive"}})
    command = (sys.executable, "-c", f"import sys; sys.stdout.write({response!r})")
    result = JsonlCommandTransport(LiveModelConfig("local", command), max_output_bytes=1024).request({}, 5)
    assert result["usage"]["total_tokens"] == 3
    assert result["directive"]["kind"] == "not-a-directive"


def test_jsonl_nonzero_exit_remains_transport_failure_even_with_json_output():
    response = json.dumps({"usage": {"total_tokens": 3}, "directive": {"kind": "not-a-directive"}})
    command = (sys.executable, "-c", f"import sys; sys.stdout.write({response!r}); sys.exit(7)")
    with pytest.raises(LiveTransportError) as error:
        JsonlCommandTransport(LiveModelConfig("local", command), max_output_bytes=1024).request({}, 5)
    assert error.value.kind == "process_error"


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
    assert report["schema_version"] == "1.1"
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
    adapter = LiveModelAdapter(task=task, policy=DemoPolicy.STATIC_BASELINE, config=config(), transport=TimedTransport(), limits=LiveRunLimits(max_model_requests=3, max_controller_steps=3, max_retries=1), registry=_test_live_registry(), clock=lambda: now[0])
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
