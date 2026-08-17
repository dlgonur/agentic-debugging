"""State-aware Validate action gating.

The static Validate allowlist still contains classify_outcome.  The live
request surface must not offer it until both required evidence values exist.
"""

from __future__ import annotations

import inspect
import json
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
    allowed_actions_for_state,
)
from agentic_debugger.agent.model_adapter import ActionDirective, ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry
from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.demo.tools import (
    DemoToolContext,
    build_registry,
    validation_classification_ready,
)
from agentic_debugger.evaluation.live import (
    LiveCaseStatus,
    LiveModelAdapter,
    LiveModelAdapterError,
    LiveModelConfig,
    LiveRunLimits,
    _action_contracts_for_state,
    run_live_case,
)
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.events.schema import Action, Observation
from agentic_debugger.runtime.workspace import TaskWorkspace


ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "curated-off-by-one-002"


@pytest.fixture
def workspace_parent():
    base = Path(tempfile.gettempdir()) / "agentic-debugger-state-aware-validate"
    base.mkdir(parents=True, exist_ok=True)
    path = base / ("case-" + uuid.uuid4().hex)
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _config(model_name: str = "test-model") -> LiveModelConfig:
    return LiveModelConfig(model_name, ("test-model-command",))


def _registry_case(tmp_path: Path):
    fixture = ROOT / "agentic_debugger/datasets/curated" / TASK_ID
    task = DebugTask.from_mapping(json.loads((fixture / "task.json").read_text()))
    workspace = TaskWorkspace(str(fixture), parent_dir=str(tmp_path))
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    return task, workspace, context, build_registry(context)


def _contracts(registry, *, f2p: bool = False, regression: bool = False):
    return _action_contracts_for_state(
        ControllerState.VALIDATE,
        registry=registry,
        policy=DemoPolicy.STATIC_BASELINE,
        session_active=False,
        pdb_available=True,
        pdb_observations_remaining=1,
        post_patch_f2p_collected=f2p,
        regression_collected=regression,
    )


def _observation(task: DebugTask, name: str, payload: dict, *, run_id: str = "validate-run"):
    return Observation.from_mapping(
        {
            "observation_id": f"obs-{name}",
            "action_id": f"act-{name}",
            "run_id": run_id,
            "task_id": task.task_id,
            "name": name,
            "status": "ok",
            "payload": payload,
            "summary": "",
            "truncated": False,
        }
    )


def _validate_snapshot(task: DebugTask, index: int, last=None, *, run_id: str = "validate-run"):
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    return ControllerSnapshot(
        run_id,
        task.task_id,
        ControllerState.VALIDATE,
        index,
        limits,
        ControllerBudgetState(),
        HypothesisLedger(),
        last_observation=last,
    )


def _patch() -> str:
    fixture = ROOT / "agentic_debugger/datasets/curated" / TASK_ID
    scenario = scenario_for(TASK_ID)
    return build_reference_patch(
        (fixture / scenario.reference_repair.target_path).read_text(encoding="utf-8"),
        scenario.reference_repair,
    )


class ScriptedTransport:
    def __init__(self, patch: str):
        scenario = scenario_for(TASK_ID)
        self.index = 0
        self.directives = [
            {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
            {"kind": "transition", "target_state": "Understand", "reason": "reproduced"},
            {
                "kind": "action",
                "name": "find_function",
                "arguments": {
                    "name": scenario.localization.symbol,
                    "path": scenario.localization.file_path,
                },
            },
            {
                "kind": "action",
                "name": "get_source_window",
                "arguments": {"path": scenario.localization.file_path, "line": 1},
            },
            {
                "kind": "add_hypothesis",
                "hypothesis_id": scenario.hypothesis_id,
                "statement": scenario.root_cause_statement,
                "confidence": "low",
                "evidence_refs": [],
                "requires_runtime_evidence": False,
            },
            {
                "kind": "action",
                "name": "express_root_cause_hypothesis",
                "arguments": {
                    "hypothesis_id": scenario.hypothesis_id,
                    "statement": scenario.root_cause_statement,
                    "target_file": scenario.localization.file_path,
                    "target_symbol": scenario.localization.symbol,
                    "confidence": "low",
                },
            },
            {"kind": "transition", "target_state": "Patch", "reason": "static evidence is sufficient"},
            {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch}},
            {"kind": "action", "name": "syntax_check", "arguments": {}},
            {"kind": "transition", "target_state": "Validate", "reason": "syntax checked"},
            {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "post_patch"}},
            {"kind": "action", "name": "run_regression_tests", "arguments": {}},
            {"kind": "action", "name": "classify_outcome", "arguments": {}},
            {"kind": "transition", "target_state": "Done", "reason": "finished"},
        ]
        self.requests: list[dict] = []

    def request(self, payload, timeout_seconds):
        self.requests.append(payload)
        directive = self.directives[min(self.index, len(self.directives) - 1)]
        self.index += 1
        return {
            "directive": directive,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class CaptureFailedTransport:
    def __init__(self):
        self.payloads: list[dict] = []

    def request(self, payload, timeout_seconds):
        self.payloads.append(payload)
        return {
            "directive": {
                "kind": "transition",
                "target_state": "Failed",
                "reason": "inspect validate surface",
            }
        }


def test_neither_evidence_hides_classify_outcome(tmp_path):
    _task, workspace, _context, registry = _registry_case(tmp_path)
    try:
        contracts = _contracts(registry)
        assert ActionName.CLASSIFY_OUTCOME.value not in contracts
        assert ActionName.RUN_REPRODUCTION.value in contracts
        assert ActionName.RUN_REGRESSION_TESTS.value in contracts
        assert ActionName.REVERT_PATCH.value in contracts
        assert contracts[ActionName.RUN_REPRODUCTION.value]["properties"]["phase"]["enum"] == [
            "post_patch"
        ]
        assert ActionName.CLASSIFY_OUTCOME.value in {
            action.value for action in allowed_actions_for_state(ControllerState.VALIDATE)
        }
    finally:
        workspace.cleanup()


def test_regression_only_still_requires_post_patch_reproduction(tmp_path):
    _task, workspace, _context, registry = _registry_case(tmp_path)
    try:
        contracts = _contracts(registry, regression=True)
        assert ActionName.CLASSIFY_OUTCOME.value not in contracts
        assert ActionName.RUN_REPRODUCTION.value in contracts
        assert ActionName.RUN_REGRESSION_TESTS.value in contracts
    finally:
        workspace.cleanup()


def test_post_patch_f2p_only_still_requires_regression(tmp_path):
    _task, workspace, _context, registry = _registry_case(tmp_path)
    try:
        contracts = _contracts(registry, f2p=True)
        assert ActionName.CLASSIFY_OUTCOME.value not in contracts
        assert ActionName.RUN_REPRODUCTION.value in contracts
        assert ActionName.RUN_REGRESSION_TESTS.value in contracts
    finally:
        workspace.cleanup()


def test_both_evidence_values_expose_classify_outcome(tmp_path):
    _task, workspace, context, registry = _registry_case(tmp_path)
    try:
        contracts = _contracts(registry, f2p=True, regression=True)
        assert ActionName.CLASSIFY_OUTCOME.value in contracts
        context.post_patch_f2p_passed = True
        context.regression_passed = True
        observation = ToolRegistry.dispatch(
            registry,
            Action(
                action_id="action-000000000",
                run_id="validate-run",
                task_id=context.task.task_id,
                state=ControllerState.VALIDATE,
                name=ActionName.CLASSIFY_OUTCOME.value,
                arguments={},
            ),
            observation_id="observation-000000000",
        )
        assert observation.status.value == "ok"
        assert observation.payload["outcome"]
        assert context.controller_outcome == observation.payload["outcome"]
    finally:
        workspace.cleanup()


def test_live_adapter_hides_then_exposes_classify_from_observations(tmp_path):
    task, workspace, _context, registry = _registry_case(tmp_path)
    transport = CaptureFailedTransport()
    try:
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.STATIC_BASELINE,
            config=_config(),
            transport=transport,
            limits=LiveRunLimits(max_model_requests=4),
            registry=registry,
        )
        adapter.next_directive(_validate_snapshot(task, 0))
        assert ActionName.CLASSIFY_OUTCOME.value not in transport.payloads[0]["action_contracts"]
        assert ActionName.CLASSIFY_OUTCOME.value not in transport.payloads[0]["controller"]["allowed_actions"]
        assert "Failed" in transport.payloads[0]["controller"]["legal_transition_targets"]
        assert "Done" in transport.payloads[0]["controller"]["legal_transition_targets"]

        adapter.next_directive(
            _validate_snapshot(
                task,
                1,
                _observation(
                    task,
                    ActionName.RUN_REGRESSION_TESTS.value,
                    {"all_passed": True},
                ),
            )
        )
        assert ActionName.CLASSIFY_OUTCOME.value not in transport.payloads[1]["action_contracts"]
        assert ActionName.RUN_REPRODUCTION.value in transport.payloads[1]["action_contracts"]

        adapter.next_directive(
            _validate_snapshot(
                task,
                2,
                _observation(
                    task,
                    ActionName.RUN_REPRODUCTION.value,
                    {"phase": "post_patch", "passed": True},
                ),
            )
        )
        assert ActionName.CLASSIFY_OUTCOME.value in transport.payloads[2]["action_contracts"]
        assert ActionName.CLASSIFY_OUTCOME.value in transport.payloads[2]["controller"]["allowed_actions"]
    finally:
        workspace.cleanup()


def test_premature_classify_is_rejected_as_illegal_action(tmp_path):
    task, workspace, _context, registry = _registry_case(tmp_path)

    class AlwaysClassify:
        def request(self, payload, timeout_seconds):
            return {"directive": {"kind": "action", "name": "classify_outcome", "arguments": {}}}

    try:
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.STATIC_BASELINE,
            config=_config(),
            transport=AlwaysClassify(),
            limits=LiveRunLimits(max_model_requests=2, max_retries=1),
            registry=registry,
        )
        with pytest.raises(LiveModelAdapterError):
            adapter.next_directive(_validate_snapshot(task, 0))
        assert adapter.directive_rejections
        assert adapter.directive_rejections[0]["category"] == "illegal_action"
    finally:
        workspace.cleanup()


def test_live_adapter_accepts_classify_only_after_both_evidence(tmp_path):
    task, workspace, _context, registry = _registry_case(tmp_path)
    captured = []

    class ClassifyWhenOffered:
        def request(self, payload, timeout_seconds):
            captured.append(payload)
            if ActionName.CLASSIFY_OUTCOME.value in payload["action_contracts"]:
                return {"directive": {"kind": "action", "name": "classify_outcome", "arguments": {}}}
            return {
                "directive": {
                    "kind": "transition",
                    "target_state": "Failed",
                    "reason": "classify not yet legal",
                }
            }

    try:
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.STATIC_BASELINE,
            config=_config(),
            transport=ClassifyWhenOffered(),
            limits=LiveRunLimits(max_model_requests=3, max_retries=0),
            registry=registry,
        )
        first = adapter.next_directive(
            _validate_snapshot(
                task,
                0,
                _observation(
                    task,
                    ActionName.RUN_REPRODUCTION.value,
                    {"phase": "post_patch", "passed": True},
                ),
            )
        )
        assert first.target_state is ControllerState.FAILED
        assert ActionName.CLASSIFY_OUTCOME.value not in captured[0]["action_contracts"]

        second = adapter.next_directive(
            _validate_snapshot(
                task,
                1,
                _observation(
                    task,
                    ActionName.RUN_REGRESSION_TESTS.value,
                    {"all_passed": True},
                ),
            )
        )
        assert isinstance(second, ActionDirective)
        assert second.name is ActionName.CLASSIFY_OUTCOME
        assert captured[1]["action_contracts"]["classify_outcome"]["additional_properties"] is False
    finally:
        workspace.cleanup()


def test_complete_validate_trajectory_still_reaches_done(workspace_parent):
    transport = ScriptedTransport(_patch())
    result = run_live_case(
        repository_root=ROOT,
        task_id=TASK_ID,
        policy=DemoPolicy.STATIC_BASELINE,
        repetition=1,
        workspace_parent=workspace_parent,
        config=_config(),
        limits=LiveRunLimits(max_model_requests=32, max_controller_steps=32),
        transport=transport,
    )
    assert result.status is LiveCaseStatus.RESOLVED
    assert result.controller["completed"] is True
    assert result.controller["final_state"] == ControllerState.DONE.value
    assert result.verifier["executed"] is True
    assert result.verifier["outcome"] == "RESOLVED"
    validate_requests = [
        payload
        for payload in transport.requests
        if payload["controller"]["state"] == ControllerState.VALIDATE.value
    ]
    assert validate_requests
    assert ActionName.CLASSIFY_OUTCOME.value not in validate_requests[0]["action_contracts"]
    assert ActionName.CLASSIFY_OUTCOME.value in validate_requests[-2]["action_contracts"]
    assert validate_requests[-1]["controller"]["legal_transition_targets"]


def test_gating_does_not_inspect_model_identity(tmp_path):
    task, workspace, _context, registry = _registry_case(tmp_path)
    try:
        signature = inspect.signature(_action_contracts_for_state)
        assert "model" not in signature.parameters
        assert "model_name" not in signature.parameters
        assert "task_id" not in signature.parameters
        source = inspect.getsource(_action_contracts_for_state)
        lowered = source.lower()
        assert "nemotron" not in lowered
        assert "gpt-oss" not in lowered
        assert "curated-none-handling" not in lowered

        first = CaptureFailedTransport()
        second = CaptureFailedTransport()
        LiveModelAdapter(
            task=task,
            policy=DemoPolicy.STATIC_BASELINE,
            config=_config("ollama-cloud/gpt-oss:20b-cloud"),
            transport=first,
            limits=LiveRunLimits(max_model_requests=1),
            registry=registry,
        ).next_directive(_validate_snapshot(task, 0, run_id="model-a"))
        LiveModelAdapter(
            task=task,
            policy=DemoPolicy.STATIC_BASELINE,
            config=_config("ollama-cloud/nemotron-3-nano:30b-cloud"),
            transport=second,
            limits=LiveRunLimits(max_model_requests=1),
            registry=registry,
        ).next_directive(_validate_snapshot(task, 0, run_id="model-b"))
        assert first.payloads[0]["action_contracts"] == second.payloads[0]["action_contracts"]
        assert first.payloads[0]["controller"]["allowed_actions"] == second.payloads[0]["controller"]["allowed_actions"]
        adapter_source = inspect.getsource(LiveModelAdapter._effective_contract)
        assert "model_name" not in adapter_source
        assert "nemotron" not in adapter_source.lower()
    finally:
        workspace.cleanup()


def test_failed_evidence_values_still_count_as_collected():
    assert validation_classification_ready(False, False) is True
    assert validation_classification_ready(True, False) is True
    assert validation_classification_ready(None, False) is False
    assert validation_classification_ready(True, None) is False
    assert validation_classification_ready(None, None) is False


def test_apply_and_revert_forget_stale_validation_evidence(tmp_path):
    task, workspace, context, _registry = _registry_case(tmp_path)
    try:
        context.post_patch_f2p_passed = True
        context.regression_passed = False
        context.controller_outcome = "BREAKING_RESOLVED"
        context.clear_validation_evidence()
        assert context.post_patch_f2p_passed is None
        assert context.regression_passed is None
        assert context.controller_outcome is None
        assert context.validation_evidence_ready() is False

        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.STATIC_BASELINE,
            config=_config(),
            transport=CaptureFailedTransport(),
            limits=LiveRunLimits(max_model_requests=3),
            registry=build_registry(context),
        )
        adapter._post_patch_f2p_collected = True
        adapter._regression_collected = True
        adapter.next_directive(
            _validate_snapshot(
                task,
                0,
                _observation(task, ActionName.REVERT_PATCH.value, {"reverted": True}),
            )
        )
        assert adapter._post_patch_f2p_collected is False
        assert adapter._regression_collected is False
        assert ActionName.CLASSIFY_OUTCOME.value not in adapter.transport.payloads[0]["action_contracts"]
    finally:
        workspace.cleanup()
