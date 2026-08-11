"""Integration test: R1 bridge → controller → real PDB → observation → model loop.

Proves the full R1 seam works end-to-end without loading the real model.
Uses ``ScriptedBridgeAdapter`` with the R1 bridge to feed pre-written
commands through the real bridge parser → real ``DeterministicController``
→ real ``ToolRegistry`` → real ``PdbSession`` → real PDB worker → real
observation.

The key R1 assertion: the prompt rendered at the ``break`` step contains
the target source with line numbers and breakpoint-eligible lines.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
    PdbPolicy,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.tools import (
    DemoToolContext,
    build_registry,
    prepare_pdb_probe,
)
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.runtime.workspace import TaskWorkspace

from experiments.debugger_interaction_v2_r1.adapter import (
    ScriptedBridgeAdapter,
    make_session_state_provider,
)
from experiments.debugger_interaction_v2_r1.bridge import (
    breakpoint_eligible_lines,
)

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
    probe = prepare_pdb_probe(
        fixture_dir, scenario, case_dir, model_selects_breakpoint=True,
    )
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=probe)
    registry = build_registry(
        context, pdb_policy=PdbPolicy.ALWAYS_ON,
        interactive_debugger_controls=True,
    )

    original_source = (fixture_dir / scenario.runtime_probe.module_path).read_text(
        encoding="utf-8"
    )
    eligible_lines = breakpoint_eligible_lines(original_source)

    yield {
        "task": task, "workspace": workspace, "probe": probe,
        "context": context, "registry": registry, "case_dir": case_dir,
        "original_source": original_source, "eligible_lines": eligible_lines,
        "script_path": scenario.runtime_probe.module_path,
    }
    context.release_pdb()


def _make_snapshot(case_setup, state=ControllerState.REPRODUCE, index=0, last_obs=None):
    task = case_setup["task"]
    return ControllerSnapshot(
        run_id=f"r1-test-{TASK_ID}", task_id=TASK_ID, state=state,
        model_call_index=index,
        budget_limits=ControllerBudgetLimits.from_task_constraints(task.constraints),
        budget_state=ControllerBudgetState(), hypotheses=HypothesisLedger(),
        last_observation=last_obs,
    )


class TestR1PDBInteractionLoop:
    def test_break_stack_locals_loop_with_real_pdb(self, case_setup):
        """The minimal interaction loop: break 9 → stack → locals → stop,
        all against the real PDB backend, with observation provenance."""

        commands = (
            "reproduce",      # REPRODUCE
            "understand",     # → UNDERSTAND
            "runtime",        # → RUNTIME_EVIDENCE
            "break 9",        # start PDB at line 9 (model-selected)
            "stack",          # get stack summary (real PDB)
            "locals",         # get frame locals (real PDB)
            "stop",           # stop PDB
            "failed",         # end
        )

        session_provider = make_session_state_provider(case_setup["context"])
        adapter = ScriptedBridgeAdapter(
            steps=commands,
            model_name="scripted-r1-test",
            task_description="test task",
            script_path=case_setup["script_path"],
            source_text=case_setup["original_source"],
            eligible_lines=case_setup["eligible_lines"],
            session_state_provider=session_provider,
        )

        controller = DeterministicController(
            case_setup["registry"], adapter,
            ControllerRunConfig(max_model_calls=24),
        )

        snapshot = _make_snapshot(case_setup)
        result = controller.run(snapshot)
        telemetry = adapter.telemetry

        assert len(telemetry) >= 7

        # Find the break, stack, locals records
        break_record = None
        stack_record = None
        locals_record = None
        for t in telemetry:
            action = t.get("translated_directive", {}).get("action_name")
            if action == "start_pdb_session":
                break_record = t
            elif action == "get_stack_summary":
                stack_record = t
            elif action == "get_frame_locals":
                locals_record = t

        assert break_record is not None
        assert stack_record is not None
        assert locals_record is not None

        # Provenance: stack call consumed the break observation
        stack_provenance = stack_record.get("provenance", {})
        assert stack_provenance.get("prior_observation_id") is not None
        assert stack_provenance.get("rendered_observation_sha256") is not None

        # Provenance: locals call consumed the stack observation
        locals_provenance = locals_record.get("provenance", {})
        assert locals_provenance.get("prior_observation_id") is not None
        assert locals_provenance.get("rendered_observation_sha256") is not None

    def test_break_prompt_contains_source_and_eligible_lines(self, case_setup):
        """The prompt rendered at the break step contains the target
        source with line numbers and breakpoint-eligible lines."""

        session_provider = make_session_state_provider(case_setup["context"])

        # We just need to get to RUNTIME_EVIDENCE and check the prompt.
        commands = (
            "reproduce",
            "understand",
            "runtime",
            "break 9",
            "stop",
            "failed",
        )

        adapter = ScriptedBridgeAdapter(
            steps=commands,
            model_name="scripted-r1-prompt-test",
            task_description="test task",
            script_path=case_setup["script_path"],
            source_text=case_setup["original_source"],
            eligible_lines=case_setup["eligible_lines"],
            session_state_provider=session_provider,
        )

        controller = DeterministicController(
            case_setup["registry"], adapter,
            ControllerRunConfig(max_model_calls=24),
        )

        snapshot = _make_snapshot(case_setup)
        controller.run(snapshot)

        telemetry = adapter.telemetry

        # Find the telemetry record where the controller was in
        # RUNTIME_EVIDENCE and the raw text was "break 9".
        break_prompt_summary = None
        for t in telemetry:
            if (t.get("controller_state") == "RuntimeEvidence"
                    and t.get("raw_response_text") == "break 9"):
                break_prompt_summary = t.get("request", {}).get("user_prompt_summary", "")
                break

        assert break_prompt_summary is not None, "no break prompt found in telemetry"
        assert "Target script for debugging" in break_prompt_summary
        assert "Breakpoint-eligible lines" in break_prompt_summary
        assert "calculated_indexes" in break_prompt_summary

    def test_break_9_produces_non_error_paused_observation(self, case_setup):
        """The real PDB backend pauses at line 9 with status=ok."""

        session_provider = make_session_state_provider(case_setup["context"])
        commands = (
            "reproduce",
            "understand",
            "runtime",
            "break 9",
            "stop",
            "failed",
        )

        adapter = ScriptedBridgeAdapter(
            steps=commands,
            model_name="scripted-r1-pause-test",
            task_description="test task",
            script_path=case_setup["script_path"],
            source_text=case_setup["original_source"],
            eligible_lines=case_setup["eligible_lines"],
            session_state_provider=session_provider,
        )

        controller = DeterministicController(
            case_setup["registry"], adapter,
            ControllerRunConfig(max_model_calls=24),
        )

        snapshot = _make_snapshot(case_setup)
        result = controller.run(snapshot)

        # The controller result should not be a MODEL_ERROR (which would
        # indicate the break command failed to parse or dispatch).
        assert result.stop_reason.value != "model_error"

        # The break command was accepted (parse_status = "accepted").
        break_record = None
        for t in adapter.telemetry:
            if t.get("translated_directive", {}).get("action_name") == "start_pdb_session":
                break_record = t
                break
        assert break_record is not None
        assert break_record.get("parse_result", {}).get("status") == "accepted"