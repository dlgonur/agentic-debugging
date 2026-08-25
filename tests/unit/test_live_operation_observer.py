"""Provider-free tests for the structured live operation observer channel.

The structured operation observer is the Level-32 telemetry boundary: the
adapter and the controller report facts they genuinely own (tool dispatch,
source ranges, debugger lifecycle, PDB proof observations, candidate
outcomes) and nothing else.  These tests prove the records are emitted from
real observations, that observer failures never propagate, and that the
scientific result (trajectory, verifier, classification) is invariant when
visibility is enabled versus disabled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.observer import (
    ControllerObservation,
    ControllerObservationKind,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry, ToolResult, ToolSpec
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import (
    LiveCaseStatus,
    LiveModelAdapter,
    LiveModelConfig,
    LiveRunLimits,
    _ControllerOperationObserver,
    run_live_case,
)
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.events.schema import Observation, ObservationStatus

ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "curated-none-handling-001"


def _task() -> DebugTask:
    return DebugTask.from_mapping(
        json.loads(
            (ROOT / "agentic_debugger" / "datasets" / "curated" / TASK_ID / "task.json").read_text(
                encoding="utf-8"
            )
        )
    )


def _registry() -> ToolRegistry:
    return ToolRegistry(
        (
            ToolSpec(
                ActionName.RUN_REPRODUCTION,
                lambda arguments: dict(arguments),
                lambda _action, _arguments: ToolResult(ObservationStatus.OK, {}, "ok"),
            ),
        )
    )


class _SilentTransport:
    def request(self, payload, timeout_seconds):  # pragma: no cover - unused
        raise AssertionError("transport must not be called by these tests")


def _adapter(records: list | None) -> LiveModelAdapter:
    return LiveModelAdapter(
        task=_task(),
        policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig("test-model", ("test-model-command",)),
        transport=_SilentTransport(),
        limits=LiveRunLimits(max_model_requests=4, max_controller_steps=4),
        registry=_registry(),
        operation_observer=(records.append if records is not None else None),
    )


def _observation(name: str, payload: dict, status: ObservationStatus = ObservationStatus.OK) -> Observation:
    return Observation(
        observation_id="obs-1",
        action_id="act-1",
        run_id="run-x",
        task_id=TASK_ID,
        name=name,
        status=status,
        payload=payload,
        summary="bounded summary",
        truncated=False,
    )


def _snapshot(observation: Observation) -> ControllerSnapshot:
    task = _task()
    return ControllerSnapshot(
        "run-x",
        TASK_ID,
        ControllerState.UNDERSTAND,
        3,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(),
        HypothesisLedger(),
        observation,
    )


def test_source_inspection_record_carries_the_real_window_range() -> None:
    records: list = []
    adapter = _adapter(records)
    adapter._observe_snapshot(
        _snapshot(
            _observation(
                "get_source_window",
                {"path": "cookiecutter/config.py", "start_line": 40, "end_line": 80},
            )
        )
    )
    assert records == [
        {
            "operation": "source_inspection",
            "tool": "get_source_window",
            "file": "cookiecutter/config.py",
            "start_line": 40,
            "end_line": 80,
        }
    ]


def test_debugger_active_and_pdb_observation_are_separate_facts() -> None:
    records: list = []
    adapter = _adapter(records)
    adapter._observe_snapshot(
        _snapshot(
            _observation(
                "start_pdb_session",
                {
                    "state": "paused",
                    "script": "cookiecutter/config.py",
                    "line": 58,
                    "function": "prompt_and_delete",
                    "breakpoint_line": 58,
                },
            )
        )
    )
    assert records == [
        {
            "operation": "debugger_active",
            "script": "cookiecutter/config.py",
            "breakpoint_line": 58,
        }
    ]
    records.clear()
    adapter._observe_snapshot(
        _snapshot(
            _observation(
                "get_stack_summary",
                {
                    "pause_generation": 1,
                    "frames": [
                        {
                            "frame_id": 0,
                            "script": "cookiecutter/config.py",
                            "line": 58,
                            "function": "prompt_and_delete",
                            "is_current": True,
                        }
                    ],
                },
            )
        )
    )
    assert records == [
        {
            "operation": "pdb_observation",
            "script": "cookiecutter/config.py",
            "line": 58,
        }
    ]


def test_failed_pdb_start_is_never_a_debugger_active_fact() -> None:
    records: list = []
    adapter = _adapter(records)
    adapter._observe_snapshot(
        _snapshot(
            _observation(
                "start_pdb_session",
                {"state": "exited"},
                status=ObservationStatus.ERROR,
            )
        )
    )
    assert records == []


def test_candidate_records_carry_only_real_apply_outcomes() -> None:
    records: list = []
    adapter = _adapter(records)
    adapter._observe_snapshot(
        _snapshot(
            _observation(
                "apply_patch",
                {
                    "applied": True,
                    "changed_files": ["cookiecutter/config.py"],
                    "patch_sha256": "0" * 64,
                },
            )
        )
    )
    adapter._observe_snapshot(
        _snapshot(
            _observation(
                "apply_patch",
                {"dispatch_reason": "tool_rejected", "diagnostic": "patch did not apply"},
                status=ObservationStatus.REJECTED,
            )
        )
    )
    adapter._observe_snapshot(
        _snapshot(
            _observation(
                "apply_patch",
                {"dispatch_reason": "invalid_arguments"},
                status=ObservationStatus.REJECTED,
            )
        )
    )
    adapter._observe_snapshot(
        _snapshot(
            _observation(
                "apply_patch",
                {"dispatch_reason": "tool_error", "diagnostic": "hunk failed"},
                status=ObservationStatus.ERROR,
            )
        )
    )
    adapter._observe_snapshot(
        _snapshot(
            _observation(
                "revert_patch",
                {"changed_files": ["cookiecutter/config.py"]},
            )
        )
    )
    assert [record["operation"] for record in records] == ["candidate"] * 4
    assert records[0]["phase"] == "applied"
    assert records[0]["changed_files"] == ["cookiecutter/config.py"]
    assert records[1]["phase"] == "rejected"
    assert records[1]["reason"] == "patch did not apply"
    # A dispatch-level rejection (arguments never reached the handler) is
    # not a candidate attempt and must not be reported as one.
    assert records[2]["phase"] == "failed"
    assert records[3]["phase"] == "reverted"


def test_observer_failure_never_propagates() -> None:
    class _BrokenObserver:
        def __call__(self, record):  # pragma: no cover - raising body
            raise RuntimeError("telemetry sink failed")

    adapter = LiveModelAdapter(
        task=_task(),
        policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig("test-model", ("test-model-command",)),
        transport=_SilentTransport(),
        limits=LiveRunLimits(max_model_requests=4, max_controller_steps=4),
        registry=_registry(),
        operation_observer=_BrokenObserver(),
    )
    adapter._observe_snapshot(
        _snapshot(
            _observation(
                "get_source_window",
                {"path": "a.py", "start_line": 1, "end_line": 5},
            )
        )
    )


def test_invalid_operation_observer_is_rejected() -> None:
    with pytest.raises(Exception, match="operation_observer must be callable or None"):
        LiveModelAdapter(
            task=_task(),
            policy=DemoPolicy.STATIC_BASELINE,
            config=LiveModelConfig("test-model", ("test-model-command",)),
            transport=_SilentTransport(),
            limits=LiveRunLimits(max_model_requests=4, max_controller_steps=4),
            registry=_registry(),
            operation_observer="not-callable",
        )


def test_controller_observer_projects_tool_and_step_boundaries_only() -> None:
    records: list = []
    observer = _ControllerOperationObserver(records.append)
    observer.notify(
        ControllerObservation(
            kind=ControllerObservationKind.TOOL_STARTED,
            run_id="run-x",
            task_id=TASK_ID,
            tool_name="get_source_window",
        )
    )
    observer.notify(
        ControllerObservation(
            kind=ControllerObservationKind.TOOL_COMPLETED,
            run_id="run-x",
            task_id=TASK_ID,
            tool_name="get_source_window",
            observation_status=ObservationStatus.OK,
        )
    )
    observer.notify(
        ControllerObservation(
            kind=ControllerObservationKind.STEP_COMPLETED,
            run_id="run-x",
            task_id=TASK_ID,
            step_index=2,
            directive_kind="action",
        )
    )
    observer.notify(
        ControllerObservation(
            kind=ControllerObservationKind.MODEL_REQUEST_STARTED,
            run_id="run-x",
            task_id=TASK_ID,
        )
    )
    assert records == [
        {"operation": "tool", "phase": "started", "tool": "get_source_window"},
        {
            "operation": "tool",
            "phase": "completed",
            "tool": "get_source_window",
            "status": "ok",
        },
        {"operation": "controller_step", "step_index": 2, "directive_kind": "action"},
    ]


class _PdbScriptedTransport:
    """Real-registry scripted flow: reproduce -> PDB evidence -> patch."""

    def __init__(self, patch: str) -> None:
        from agentic_debugger.demo.catalog import scenario_for

        scenario = scenario_for(TASK_ID)
        self.index = 0
        self.directives = [
            {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
            {"kind": "transition", "target_state": "Understand", "reason": "reproduced"},
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

    def request(self, payload, timeout_seconds):
        directive = self.directives[min(self.index, len(self.directives) - 1)]
        self.index += 1
        return {"directive": directive}


def _reference_patch() -> str:
    from agentic_debugger.demo.catalog import build_reference_patch, scenario_for

    fixture = ROOT / "agentic_debugger" / "datasets" / "curated" / TASK_ID
    scenario = scenario_for(TASK_ID)
    return build_reference_patch(
        (fixture / scenario.reference_repair.target_path).read_text(encoding="utf-8"),
        scenario.reference_repair,
    )


def _run_case(tmp_path: Path, records: list | None) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    case = run_live_case(
        repository_root=ROOT,
        task_id=TASK_ID,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        repetition=1,
        workspace_parent=tmp_path,
        config=LiveModelConfig("test-model", ("test-model-command",)),
        limits=LiveRunLimits(max_model_requests=32, max_controller_steps=32),
        transport=_PdbScriptedTransport(_reference_patch()),
        operation_observer=(records.append if records is not None else None),
    )
    return case.to_mapping()


def _strip_timestamps(value):
    if isinstance(value, dict):
        return {
            key: _strip_timestamps(item)
            for key, item in value.items()
            if key not in ("timestamp", "timestamp_utc", "recorded_at_utc")
        }
    if isinstance(value, list):
        return [_strip_timestamps(item) for item in value]
    return value


def _trajectory_fingerprint(mapping: dict):
    """Structural trajectory identity: content without wall-clock stamps."""
    events = [
        _strip_timestamps(json.loads(line))
        for line in mapping["events_jsonl"].splitlines()
        if line
    ]
    return events


def test_visibility_enabled_vs_disabled_leaves_scientific_result_unchanged(tmp_path):
    """The observer channel is telemetry-only: identical scientific output."""
    baseline = _run_case(tmp_path / "baseline", None)
    assert baseline["status"] == LiveCaseStatus.RESOLVED.value, (
        "invariance proof requires a real completed case"
    )
    records: list = []
    observed = _run_case(tmp_path / "observed", records)
    for key in ("task_id", "policy", "status", "controller", "verifier", "diagnostics"):
        assert observed[key] == baseline[key], f"scientific field diverged: {key}"
    assert _trajectory_fingerprint(observed) == _trajectory_fingerprint(baseline)
    operations = [record.get("operation") for record in records]
    assert "tool" in operations
    assert "controller_step" in operations
    assert "source_inspection" in operations
    assert "debugger_active" in operations
    assert "pdb_observation" in operations
    assert "candidate" in operations
    applied = [record for record in records if record.get("operation") == "candidate"]
    assert applied and applied[-1]["phase"] == "applied"
    # No model text, prompts, or completions ever cross the channel.
    for record in records:
        assert not any(
            key in record for key in ("prompt", "completion", "content", "directive")
        )


def test_operation_records_never_carry_model_or_private_payloads(tmp_path):
    records: list = []
    _run_case(tmp_path, records)
    serialized = json.dumps(records)
    for forbidden in ("root_cause_statement", "oracle", "prompt", "completion"):
        assert forbidden not in serialized
