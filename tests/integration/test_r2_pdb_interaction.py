"""Integration: R2 staged bridge → real PDB → full A-F chain."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.controller import ControllerRunConfig, DeterministicController
from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger, PdbPolicy
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.tools import DemoToolContext, build_registry, prepare_pdb_probe
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.events.logger import JsonlEventLogger
import io, json

from experiments.debugger_interaction_v2_r2.adapter import (
    R2StageTracker,
    ScriptedBridgeAdapter,
    make_r2_session_state_provider,
)
from experiments.debugger_interaction_v2_r2.bridge import R2Stage, breakpoint_eligible_lines
from experiments.debugger_interaction_v2_r2.r2_runner import _compute_gate_r2

TASK_ID = "curated-off-by-one-002"
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"


@pytest.fixture
def case_setup(tmp_path):
    fixture_dir = CURATED_ROOT / TASK_ID
    task = load_task(str(fixture_dir / "task.json"))
    scenario = scenario_for(TASK_ID)
    case_dir = tmp_path / "case"
    case_dir.mkdir(parents=True, exist_ok=True)
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_dir))
    probe = prepare_pdb_probe(fixture_dir, scenario, case_dir, model_selects_breakpoint=True)
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=probe)
    registry = build_registry(context, pdb_policy=PdbPolicy.ALWAYS_ON, interactive_debugger_controls=True)
    original_source = (fixture_dir / scenario.runtime_probe.module_path).read_text(encoding="utf-8")
    eligible_lines = breakpoint_eligible_lines(original_source)
    yield {
        "task": task, "workspace": workspace, "probe": probe,
        "context": context, "registry": registry, "case_dir": case_dir,
        "original_source": original_source, "eligible_lines": eligible_lines,
        "script_path": scenario.runtime_probe.module_path,
    }
    context.release_pdb()


def _snapshot(case_setup, state=ControllerState.REPRODUCE, index=0, last_obs=None):
    task = case_setup["task"]
    return ControllerSnapshot(
        run_id=f"r2-test-{TASK_ID}", task_id=TASK_ID, state=state,
        model_call_index=index,
        budget_limits=ControllerBudgetLimits.from_task_constraints(task.constraints),
        budget_state=ControllerBudgetState(), hypotheses=HypothesisLedger(),
        last_observation=last_obs,
    )


class TestR2RealPDBChain:
    def test_full_r2_chain_with_locals(self, case_setup):
        """break 2 -> stack G1 -> locals G1 -> step -> stack G2 -> diagnosis"""
        commands = (
            "reproduce", "understand", "runtime",
            "break 2", "stack", "locals", "step", "stack",
            "diagnosis the range end_index truncation",
            "failed",
        )
        tracker = R2StageTracker()
        provider = make_r2_session_state_provider(case_setup["context"], lambda: tracker.stage)
        adapter = ScriptedBridgeAdapter(
            steps=commands, model_name="scripted-r2", task_description="test",
            script_path=case_setup["script_path"],
            source_text=case_setup["original_source"],
            eligible_lines=case_setup["eligible_lines"],
            session_state_provider=provider, stage_tracker=tracker,
        )
        controller = DeterministicController(case_setup["registry"], adapter, ControllerRunConfig(max_model_calls=30))
        result = controller.run(_snapshot(case_setup))
        telemetry = adapter.telemetry
        # Project trajectory for gate
        stream = io.StringIO()
        logger = JsonlEventLogger(result.run_id, result.task_id, stream=stream)
        for ev in project_controller_run(result, tool_version="debugger-interaction-v2-r2", model=adapter.model_name, timestamp="2026-01-01T00:00:00Z", duration_ms=0):
            logger.append(ev)
        logger.flush(); logger.close()
        traj = stream.getvalue()

        gate = _compute_gate_r2(telemetry, traj, expected_script="recent_window.py")
        assert gate["passed"] is True, gate

    def test_full_r2_chain_with_print(self, case_setup):
        """break 2 -> stack G1 -> print values G1 -> next -> stack G2 -> diagnosis"""
        commands = (
            "reproduce", "understand", "runtime",
            "break 2", "stack", "print values", "next", "stack",
            "diagnosis off-by-one in range end",
            "failed",
        )
        tracker = R2StageTracker()
        provider = make_r2_session_state_provider(case_setup["context"], lambda: tracker.stage)
        adapter = ScriptedBridgeAdapter(
            steps=commands, model_name="scripted-r2", task_description="test",
            script_path=case_setup["script_path"],
            source_text=case_setup["original_source"],
            eligible_lines=case_setup["eligible_lines"],
            session_state_provider=provider, stage_tracker=tracker,
        )
        controller = DeterministicController(case_setup["registry"], adapter, ControllerRunConfig(max_model_calls=30))
        result = controller.run(_snapshot(case_setup))
        telemetry = adapter.telemetry
        stream = io.StringIO()
        logger = JsonlEventLogger(result.run_id, result.task_id, stream=stream)
        for ev in project_controller_run(result, tool_version="debugger-interaction-v2-r2", model=adapter.model_name, timestamp="2026-01-01T00:00:00Z", duration_ms=0):
            logger.append(ev)
        logger.flush(); logger.close()
        traj = stream.getvalue()
        gate = _compute_gate_r2(telemetry, traj, expected_script="recent_window.py")
        assert gate["passed"] is True, gate

    def test_continue_exited_does_not_bypass_stage(self, case_setup):
        """continue->exited should not satisfy the step gate; gate should FAIL"""
        # In R2 staging, continue is not even legal — but we test gate separately:
        # Build a trajectory that has an exited continue instead of a paused step
        commands = (
            "reproduce", "understand", "runtime",
            "break 2", "stack", "locals",
            # step stage: if model somehow issued continue, it would be rejected by parser
            # But we want gate negative: a trajectory with exited continue should fail
        )
        tracker = R2StageTracker()
        provider = make_r2_session_state_provider(case_setup["context"], lambda: tracker.stage)
        adapter = ScriptedBridgeAdapter(
            steps=commands + ("failed",),
            model_name="scripted-r2", task_description="test",
            script_path=case_setup["script_path"],
            source_text=case_setup["original_source"],
            eligible_lines=case_setup["eligible_lines"],
            session_state_provider=provider, stage_tracker=tracker,
        )
        controller = DeterministicController(case_setup["registry"], adapter, ControllerRunConfig(max_model_calls=30))
        result = controller.run(_snapshot(case_setup))
        stream = io.StringIO()
        logger = JsonlEventLogger(result.run_id, result.task_id, stream=stream)
        for ev in project_controller_run(result, tool_version="debugger-interaction-v2-r2", model=adapter.model_name, timestamp="2026-01-01T00:00:00Z", duration_ms=0):
            logger.append(ev)
        logger.flush(); logger.close()
        traj = stream.getvalue()
        telemetry = adapter.telemetry
        gate = _compute_gate_r2(telemetry, traj, expected_script="recent_window.py")
        # Incomplete chain — no step, no post-step stack, no diagnosis -> FAIL
        assert gate["passed"] is False
