"""End-to-end post-mortem PDB observation and replay integration."""

from __future__ import annotations

import shutil
from pathlib import Path

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
    PdbPolicy,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    ControllerSnapshot,
    ScriptedModelAdapter,
    ScriptedModelStep,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
from agentic_debugger.demo.runner import CURATED_RELATIVE_ROOT
from agentic_debugger.demo.tools import DemoToolContext, build_registry, prepare_pdb_probe
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.events.replay import replay_events, semantic_projection
from agentic_debugger.events.schema import EventType
from agentic_debugger.runtime.workspace import TaskWorkspace


ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "curated-none-handling-001"
FIXTURE = ROOT / CURATED_RELATIVE_ROOT / TASK_ID


def test_post_mortem_trace_is_a_budgeted_replayable_controller_observation(
    tmp_path: Path,
) -> None:
    """Exercise real test, PDB worker, tool, controller, event, and replay paths."""

    task = load_task(str(FIXTURE / "task.json"))
    scenario = scenario_for(TASK_ID)
    workspace = TaskWorkspace(str(FIXTURE), parent_dir=str(tmp_path))
    probe = prepare_pdb_probe(FIXTURE, scenario, tmp_path)
    source = (
        Path(workspace.root) / scenario.reference_repair.target_path
    ).read_text(encoding="utf-8")
    patch = build_reference_patch(source, scenario.reference_repair)
    context = DemoToolContext(
        task=task,
        workspace=workspace,
        patch=patch,
        probe=probe,
    )
    try:
        steps = (
            ScriptedModelStep(
                ControllerState.REPRODUCE,
                ActionDirective(ActionName.RUN_REPRODUCTION, {"phase": "baseline"}),
            ),
            ScriptedModelStep(
                ControllerState.REPRODUCE,
                ActionDirective(ActionName.GET_FAILURE_TRACE, {}),
            ),
            ScriptedModelStep(
                ControllerState.REPRODUCE,
                TransitionDirective(
                    ControllerState.FAILED,
                    "post-mortem trajectory integration smoke complete",
                ),
            ),
        )
        controller = DeterministicController(
            build_registry(context, pdb_policy=PdbPolicy.ON_UNCERTAINTY),
            ScriptedModelAdapter(steps, model_name="post-mortem-smoke"),
            ControllerRunConfig(max_model_calls=len(steps)),
        )
        result = controller.run(
            ControllerSnapshot(
                "post-mortem-trajectory-run",
                TASK_ID,
                ControllerState.REPRODUCE,
                0,
                ControllerBudgetLimits.from_task_constraints(task.constraints),
                ControllerBudgetState(),
                HypothesisLedger(),
            )
        )

        assert result.budget_state.test_runs == 1
        assert result.budget_state.pdb_observations == 1
        assert context.pdb_observation_names == ["get_failure_trace"]
        assert context.pdb_session is None
        assert context.pdb_workspace is None

        events = project_controller_run(
            result,
            tool_version="post-mortem-trajectory-v1",
            model="post-mortem-smoke",
        )
        replayed = replay_events(events)
        projected = semantic_projection(replayed, workspace_roots=(str(tmp_path),))
        assert len(projected) == len(events)
        observations = [
            event
            for event in replayed.events
            if event.event_type is EventType.OBSERVATION
            and event.name == ActionName.GET_FAILURE_TRACE.value
        ]
        assert len(observations) == 1
        observation = observations[0].payload["observation"]
        assert observation["status"] == "ok"
        payload = observation["payload"]
        assert payload["evidence_kind"] == "pdb-post-mortem-v1"
        assert payload["post_mortem"] is True
        assert payload["session_stopped"] is True
        assert payload["workspace_removed"] is True
        response = payload["pdb_response"]
        assert response["success"] is True
        assert response["result"]["status"] == "post_mortem"
        assert response["result"]["post_mortem"] is True
        assert response["result"]["script"] == "display_name.py"
        assert response["result"]["exception"]["type"] == "AttributeError"
        assert response["result"]["innermost_frame"]["function"] == (
            "format_display_name"
        )
    finally:
        context.release_pdb()
        workspace.cleanup()
        if probe.source_dir.exists():
            shutil.rmtree(probe.source_dir)


def test_post_mortem_tool_rejects_before_baseline_without_starting_pdb(
    tmp_path: Path,
) -> None:
    task = load_task(str(FIXTURE / "task.json"))
    scenario = scenario_for(TASK_ID)
    workspace = TaskWorkspace(str(FIXTURE), parent_dir=str(tmp_path))
    probe = prepare_pdb_probe(FIXTURE, scenario, tmp_path)
    source = (
        Path(workspace.root) / scenario.reference_repair.target_path
    ).read_text(encoding="utf-8")
    context = DemoToolContext(
        task=task,
        workspace=workspace,
        patch=build_reference_patch(source, scenario.reference_repair),
        probe=probe,
    )
    try:
        steps = (
            ScriptedModelStep(
                ControllerState.REPRODUCE,
                ActionDirective(ActionName.GET_FAILURE_TRACE, {}),
            ),
        )
        result = DeterministicController(
            build_registry(context, pdb_policy=PdbPolicy.ON_UNCERTAINTY),
            ScriptedModelAdapter(steps, model_name="post-mortem-negative"),
            ControllerRunConfig(max_model_calls=1),
        ).run(
            ControllerSnapshot(
                "post-mortem-negative-run",
                TASK_ID,
                ControllerState.REPRODUCE,
                0,
                ControllerBudgetLimits.from_task_constraints(task.constraints),
                ControllerBudgetState(),
                HypothesisLedger(),
            )
        )
        observation = result.steps[0].observation
        assert observation is not None
        assert observation.status.value == "rejected"
        assert observation.payload["dispatch_reason"] == "tool_rejected"
        assert "reproduced baseline" in observation.payload["diagnostic"]
        assert context.pdb_session_started is False
        # Budget is charged for the accepted action request even if the tool
        # rejects its runtime precondition, matching controller semantics.
        assert result.budget_state.pdb_observations == 1
    finally:
        context.release_pdb()
        workspace.cleanup()
        if probe.source_dir.exists():
            shutil.rmtree(probe.source_dir)
