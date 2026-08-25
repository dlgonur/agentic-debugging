"""Provider-free tests for the observer-only live execution projection."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application.events import SessionEventKind, SessionStatus
from agentic_debugger.application.live_execution import (
    EphemeralSnapshot,
    ExecutionMode,
    KnownCeilings,
    OperationKind,
    project_live_execution,
)
from agentic_debugger.application.presentation import SessionViewState, TimelineEntry
from agentic_debugger.application.presentation import DebuggerViewState
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import LiveModelAdapter, LiveModelConfig, LiveRunLimits
from agentic_debugger.evaluation.task_schema import DebugTask


def _view(*entries: TimelineEntry, status: SessionStatus = SessionStatus.RUNNING, **kwargs: object) -> SessionViewState:
    return SessionViewState(task_id="projection-task", status=status, timeline=tuple(entries), **kwargs)


def test_outstanding_quiet_request_renders_waiting_from_injected_clock() -> None:
    view = _view(TimelineEntry(1, SessionEventKind.MODEL_REQUEST_STARTED, "model request 17 started"), latest_model_request_index=17, outstanding_model_request_index=17)
    state = project_live_execution(
        view,
        mode=ExecutionMode.LIVE,
        ceilings=KnownCeilings(model_requests=24, controller_steps=24),
        snapshot=EphemeralSnapshot(1, 17, 38.0, 6.0, True, 300.0, 100.0),
        now_monotonic=101.0,
    )
    assert state.operation is OperationKind.WAITING_FOR_MODEL
    assert state.request_index == 17
    assert state.request_elapsed_seconds == 39.0
    assert state.last_activity_age_seconds == 7.0
    assert state.ceilings.model_requests == 24


def test_replay_and_terminal_drop_ephemeral_liveness() -> None:
    view = _view(TimelineEntry(1, SessionEventKind.MODEL_REQUEST_STARTED, "model request 1 started"), latest_model_request_index=1, outstanding_model_request_index=1)
    snapshot = EphemeralSnapshot(2, 1, 12.0, 7.0, True, 300.0, 1.0)
    replay = project_live_execution(view, mode=ExecutionMode.REPLAY, snapshot=snapshot)
    assert replay.snapshot is None
    terminal = project_live_execution(
        _view(status=SessionStatus.SUCCEEDED), mode=ExecutionMode.LIVE, snapshot=snapshot
    )
    assert terminal.snapshot is None
    assert terminal.operation is OperationKind.TERMINAL


def test_completed_request_is_not_waiting() -> None:
    view = _view(
        TimelineEntry(1, SessionEventKind.MODEL_REQUEST_STARTED, "model request 3 started"),
        TimelineEntry(2, SessionEventKind.MODEL_REQUEST_COMPLETED, "model request 3 completed (ok)"),
        latest_model_request_index=3,
    )
    state = project_live_execution(view, mode=ExecutionMode.LIVE)
    assert state.operation is not OperationKind.WAITING_FOR_MODEL
    assert state.request_index == 3


def test_request_ordinal_is_consistently_one_based() -> None:
    for internal in (0, 12, 39):
        state = project_live_execution(
            _view(latest_model_request_index=internal, outstanding_model_request_index=internal),
            mode=ExecutionMode.LIVE,
        )
        assert state.request_ordinal == internal + 1


def test_pdb_start_is_not_proof_but_observation_is() -> None:
    active = project_live_execution(
        _view(debugger=DebuggerViewState(session_started=True)), mode=ExecutionMode.LIVE
    )
    observed = project_live_execution(
        _view(debugger=DebuggerViewState(session_started=True), pdb_observed=True),
        mode=ExecutionMode.LIVE,
    )
    assert active.view.pdb_observed is False
    assert observed.view.pdb_observed is True


def test_level32_progress_writer_emits_real_liveness_without_content(tmp_path) -> None:
    from scripts.run_cookiecutter_967_pdb_proof import _ProgressWriter
    path = tmp_path / "operator.progress.jsonl"
    writer = _ProgressWriter(str(path))
    writer.observe_stage("model_running")
    writer.transport_activity("stream_activity")
    writer.transport_activity("request_completed")
    rows = [__import__("json").loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    liveness = [row for row in rows if row.get("kind") == "liveness"]
    assert len(liveness) >= 2
    assert liveness[0]["request_index"] == 0
    assert liveness[-1]["transport_alive"] is False
    assert all("prompt" not in row and "content" not in row for row in liveness)


def test_writer_operation_channel_assigns_candidate_ordinals_only_for_real_attempts(tmp_path) -> None:
    from scripts.run_cookiecutter_967_pdb_proof import _ProgressWriter
    path = tmp_path / "operator.progress.jsonl"
    writer = _ProgressWriter(str(path))
    writer.observe_operation({"operation": "candidate", "phase": "rejected", "reason": "bad context"})
    writer.observe_operation({"operation": "candidate", "phase": "applied", "reason": "applied", "changed_files": ["a.py"]})
    writer.observe_operation({"operation": "candidate", "phase": "reverted", "reason": "candidate reverted"})
    rows = [__import__("json").loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    candidates = [row for row in rows if row.get("kind") == "operation" and row.get("operation") == "candidate"]
    assert [row["phase"] for row in candidates] == ["rejected", "applied", "reverted"]
    assert [row["attempt"] for row in candidates] == [1, 2, 2]
    # A revert without any real attempt is not a candidate fact (and the
    # file is only materialized by a real record).
    fresh = _ProgressWriter(str(tmp_path / "second.progress.jsonl"))
    fresh.observe_operation({"operation": "candidate", "phase": "reverted", "reason": "x"})
    second_path = tmp_path / "second.progress.jsonl"
    rows2 = (
        [__import__("json").loads(line) for line in second_path.read_text(encoding="utf-8").splitlines()]
        if second_path.exists()
        else []
    )
    assert not [row for row in rows2 if row.get("kind") == "operation"]


def test_writer_candidate_stage_never_claims_availability(tmp_path) -> None:
    from scripts.run_cookiecutter_967_pdb_proof import _ProgressWriter
    path = tmp_path / "operator.progress.jsonl"
    writer = _ProgressWriter(str(path))
    writer.observe_stage("candidate")
    text = path.read_text(encoding="utf-8")
    assert "available" not in text


def test_in_flight_apply_projects_applying_candidate_with_next_ordinal() -> None:
    from agentic_debugger.application.presentation import PatchAttemptView, PatchStage
    view = _view(
        current_tool_name="apply_patch",
        patch_attempts=(PatchAttemptView(attempt_index=0, stage=PatchStage.REJECTED),),
    )
    state = project_live_execution(view, mode=ExecutionMode.LIVE)
    assert state.operation is OperationKind.CANDIDATE
    assert state.operation_label == "Applying candidate (attempt 2)"
    first = project_live_execution(
        _view(current_tool_name="apply_patch"), mode=ExecutionMode.LIVE
    )
    assert first.operation_label == "Applying candidate (attempt 1)"


def test_tool_operation_carries_typed_source_target() -> None:
    view = _view(current_tool_name="get_source_window", current_tool_target="cookiecutter/config.py:40-80")
    state = project_live_execution(view, mode=ExecutionMode.LIVE)
    assert state.operation is OperationKind.TOOL
    assert state.operation_label == "Running get_source_window (cookiecutter/config.py:40-80)"
    assert state.current_target == "cookiecutter/config.py:40-80"


def test_outstanding_model_request_outranks_idle_debugger_label() -> None:
    from agentic_debugger.application.presentation import DebuggerViewState
    waiting = _view(
        debugger=DebuggerViewState(session_started=True),
        outstanding_model_request_index=4,
        latest_model_request_index=4,
    )
    state = project_live_execution(
        waiting,
        mode=ExecutionMode.LIVE,
        snapshot=EphemeralSnapshot(1, 4, 40.0, 30.0, True, 300.0, 10.0),
        now_monotonic=11.0,
    )
    assert state.operation is OperationKind.WAITING_FOR_MODEL
    assert state.request_ordinal == 5
    idle = project_live_execution(
        _view(debugger=DebuggerViewState(session_started=True)), mode=ExecutionMode.LIVE
    )
    assert idle.operation is OperationKind.DEBUGGER
    assert idle.operation_label == "Debugger active"


def test_official_milestone_stages_project_official_verifier_operation() -> None:
    from agentic_debugger.application.events import OperatorStage
    for stage in (
        OperatorStage.OFFICIAL_VERIFICATION_PREPARING,
        OperatorStage.OFFICIAL_EVALUATOR_STARTED,
        OperatorStage.OFFICIAL_EVALUATOR_COMPLETED,
    ):
        state = project_live_execution(
            _view(operator_stage=stage), mode=ExecutionMode.LIVE
        )
        assert state.operation is OperationKind.OFFICIAL_VERIFIER


def test_candidate_attempt_and_step_ordinals_are_one_based() -> None:
    from agentic_debugger.application.presentation import PatchAttemptView, PatchStage
    view = _view(
        latest_controller_step_index=6,
        patch_attempts=(PatchAttemptView(attempt_index=1, stage=PatchStage.APPLIED),),
    )
    state = project_live_execution(view, mode=ExecutionMode.LIVE)
    assert state.controller_step_ordinal == 7
    assert state.candidate_attempt_ordinal == 2
    assert state.view.latest_controller_step_index == 6


def test_in_flight_apply_attempt_counter_agrees_with_now_label() -> None:
    from agentic_debugger.application.presentation import PatchAttemptView, PatchStage
    view = _view(
        current_tool_name="apply_patch",
        patch_attempts=(PatchAttemptView(attempt_index=0, stage=PatchStage.REJECTED),),
    )
    state = project_live_execution(view, mode=ExecutionMode.LIVE)
    assert state.operation is OperationKind.CANDIDATE
    assert state.operation_label == "Applying candidate (attempt 2)"
    # ATTEMPT and NOW must agree on the SAME in-flight ordinal.
    assert state.candidate_attempt_ordinal == 2
    assert state.candidate_attempt == 1  # durable index stays zero-based
    first = project_live_execution(
        _view(current_tool_name="apply_patch"), mode=ExecutionMode.LIVE
    )
    assert first.operation_label == "Applying candidate (attempt 1)"
    assert first.candidate_attempt_ordinal == 1


def test_adapter_request_counter_includes_transport_retries() -> None:
    """The runtime request authority counts every transport attempt.

    ``LiveModelAdapter.metrics.model_requests`` increments for each
    ``transport.request()`` call, so the UI ordinal ``Request N / 40`` is
    the N-th actual provider request and a retry consumes the next ordinal.
    """
    import json

    from agentic_debugger.demo.catalog import scenario_for
    from agentic_debugger.evaluation.live import (
        LiveModelAdapter,
        LiveModelConfig,
        LiveRunLimits,
        LiveTransportError,
    )
    from agentic_debugger.evaluation.task_schema import DebugTask
    from agentic_debugger.agent.controller_policy import ActionName
    from agentic_debugger.agent.tool_registry import ToolRegistry, ToolResult, ToolSpec
    from agentic_debugger.events.schema import ObservationStatus

    task = DebugTask.from_mapping(
        json.loads(
            (ROOT / "agentic_debugger" / "datasets" / "curated" / "curated-none-handling-001" / "task.json").read_text(encoding="utf-8")
        )
    )

    class RetryTransport:
        def __init__(self):
            self.calls = 0

        def request(self, payload, timeout_seconds):
            self.calls += 1
            if self.calls == 1:
                raise LiveTransportError("provider detail must not escape")
            return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "controlled"}}

    registry = ToolRegistry(
        (
            ToolSpec(
                ActionName.RUN_REPRODUCTION,
                lambda arguments: dict(arguments),
                lambda _action, _arguments: ToolResult(ObservationStatus.OK, {}, "ok"),
            ),
        )
    )
    transport = RetryTransport()
    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig("test-model", ("test-model-command",)),
        transport=transport,
        limits=LiveRunLimits(max_model_requests=40, max_controller_steps=2, max_retries=1),
        registry=registry,
    )
    snapshot = ControllerSnapshot(
        "run-x",
        task.task_id,
        ControllerState.REPRODUCE,
        0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(),
        HypothesisLedger(),
    )
    directive = adapter.next_directive(snapshot)
    # The retried transport attempt succeeded: the directive resolves to the
    # controlled failure transition and the runtime counter has counted
    # every actual transport.request() attempt.
    assert directive is not None
    assert transport.calls == 2
    assert adapter.metrics.model_requests == 2
    assert adapter.metrics.retries == 1
