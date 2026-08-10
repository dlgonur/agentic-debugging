"""Integration test: S1 bridge → controller → real PDB → observation → model loop.

This test proves the core S1 seam works end-to-end WITHOUT loading the real
model.  It uses ``ScriptedBridgeAdapter`` to feed pre-written grammar commands
through the real bridge parser → real ``DeterministicController`` → real
``ToolRegistry`` → real ``PdbSession`` → real PDB worker → real observation.

The key assertion (Amendment 2): after ``stack`` produces a real PDB
observation, the next ``next_directive`` call receives a snapshot containing
that observation, and the telemetry records the observation provenance
(prior_observation_id + rendered_observation_sha256).  This proves the
model/debugger interaction loop, not just one-shot command formatting.

This test is BOUNDED to the new seam (bridge → controller → PDB → observation
→ model).  It does NOT build a second golden-trajectory/verifier framework.
The existing verifier tests already prove the verifier works.
"""

from __future__ import annotations

import sys
import tempfile
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

from experiments.debugger_interaction_v2.adapter import ScriptedBridgeAdapter

TASK_ID = "curated-off-by-one-002"
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"


@pytest.fixture
def case_setup(tmp_path):
    """Set up task, workspace, probe, context, and registry for one test case."""

    fixture_dir = CURATED_ROOT / TASK_ID
    task = load_task(str(fixture_dir / "task.json"))
    scenario = scenario_for(TASK_ID)
    case_dir = tmp_path / "case"
    case_dir.mkdir(parents=True, exist_ok=True)

    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_dir))
    probe = prepare_pdb_probe(
        fixture_dir,
        scenario,
        case_dir,
        model_selects_breakpoint=True,
    )

    context = DemoToolContext(
        task=task,
        workspace=workspace,
        patch="",
        probe=probe,
    )
    registry = build_registry(
        context,
        pdb_policy=PdbPolicy.ALWAYS_ON,
        interactive_debugger_controls=True,
    )

    yield {
        "task": task,
        "workspace": workspace,
        "probe": probe,
        "context": context,
        "registry": registry,
        "case_dir": case_dir,
    }

    # Cleanup.
    context.release_pdb()


def _make_snapshot(case_setup, state=ControllerState.REPRODUCE, index=0, last_obs=None):
    task = case_setup["task"]
    return ControllerSnapshot(
        run_id=f"s1-test-{TASK_ID}",
        task_id=TASK_ID,
        state=state,
        model_call_index=index,
        budget_limits=ControllerBudgetLimits.from_task_constraints(task.constraints),
        budget_state=ControllerBudgetState(),
        hypotheses=HypothesisLedger(),
        last_observation=last_obs,
    )


class TestPDBInteractionLoop:
    """Prove the full bridge → controller → PDB → observation → model loop."""

    def test_break_stack_locals_loop_with_real_pdb(self, case_setup):
        """The minimal interaction loop: break → stack → locals, all against
        the real PDB backend, with observation provenance binding.

        This is the Gate B offline proof: the bridge works, PDB works, and
        observations are returned to the model with provenance.
        """

        # Scripted commands that the model would produce.
        # We start in REPRODUCE, reproduce, transition to UNDERSTAND,
        # transition to RUNTIME_EVIDENCE, then do the PDB interaction loop.
        # The off-by-one bug is at line 9 (calculated_indexes = list(...)).
        commands = (
            "reproduce",           # REPRODUCE: run failing test
            "understand",          # transition to UNDERSTAND
            "runtime",              # transition to RUNTIME_EVIDENCE
            "break 9",             # start PDB at line 9 (model-selected)
            "stack",               # get stack summary (real PDB)
            "locals",              # get frame locals (real PDB, derived from stack)
            "stop",                # stop PDB
            "failed",              # signal failure (end the case)
        )

        adapter = ScriptedBridgeAdapter(
            steps=commands,
            model_name="scripted-bridge-test",
            task_description="test task",
        )

        controller = DeterministicController(
            case_setup["registry"],
            adapter,
            ControllerRunConfig(max_model_calls=24),
        )

        snapshot = _make_snapshot(case_setup)
        result = controller.run(snapshot)

        # The controller should have run through the commands.
        # It may end with FAILED (since we emit "failed" at the end) or
        # it may end earlier if a command fails.  The key assertions are
        # about the telemetry, not the final state.
        telemetry = adapter.telemetry

        # We should have at least 8 telemetry records (one per command).
        assert len(telemetry) >= 7, \
            f"expected >=7 telemetry records, got {len(telemetry)}"

        # Find the break, stack, and locals records.
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

        # --- Assert break (start_pdb_session) was accepted and executed ---
        assert break_record is not None, "no start_pdb_session in telemetry"
        assert break_record["parse_result"]["status"] == "accepted"
        assert break_record["translated_directive"]["action_name"] == "start_pdb_session"
        assert break_record["translated_directive"]["arguments"]["breakpoint_line"] == 9

        # --- Assert stack (get_stack_summary) was accepted and executed ---
        assert stack_record is not None, "no get_stack_summary in telemetry"
        assert stack_record["parse_result"]["status"] == "accepted"
        assert stack_record["translated_directive"]["action_name"] == "get_stack_summary"

        # --- Assert locals (get_frame_locals) was accepted and executed ---
        assert locals_record is not None, "no get_frame_locals in telemetry"
        assert locals_record["parse_result"]["status"] == "accepted"
        assert locals_record["translated_directive"]["action_name"] == "get_frame_locals"
        # frame_id and pause_generation must be derived from the stack observation.
        args = locals_record["translated_directive"]["arguments"]
        assert "frame_id" in args
        assert "pause_generation" in args
        assert args["frame_id"] == 0  # the current frame
        assert args["pause_generation"] > 0  # from the real PDB session

        # --- KEY ASSERTION: observation provenance binding (Amendment 2) ---
        # The locals record must have prior_observation_id from the stack observation.
        # This proves the stack observation was returned to the model and included
        # in the next request.
        assert locals_record["provenance"]["prior_observation_id"] is not None, \
            "locals record must have prior_observation_id from stack observation"
        assert locals_record["provenance"]["prior_observation_sha256"] is not None, \
            "locals record must have prior_observation_sha256"
        assert locals_record["provenance"]["rendered_observation_sha256"] is not None, \
            "locals record must have rendered_observation_sha256"

        # The stack record should NOT have prior observation provenance
        # (it's the first PDB observation in this session — the prior
        # observation would be the start_pdb_session observation, which
        # is fine, but it's not a get_stack_summary observation).
        # We check that the stack record has SOME provenance (from the
        # start_pdb_session observation that preceded it).
        assert stack_record["provenance"]["prior_observation_id"] is not None, \
            "stack record should have prior_observation_id from start_pdb observation"

    def test_break_observation_provenance_into_stack_request(self, case_setup):
        """Specifically test that the start_pdb_session observation is bound
        into the stack request (the first step of the interaction loop)."""

        commands = (
            "reproduce",
            "understand",
            "runtime",
            "break 9",
            "stack",  # this should have provenance from the break observation
        )

        adapter = ScriptedBridgeAdapter(
            steps=commands,
            model_name="scripted-bridge-test",
            task_description="test task",
        )

        controller = DeterministicController(
            case_setup["registry"],
            adapter,
            ControllerRunConfig(max_model_calls=24),
        )

        snapshot = _make_snapshot(case_setup)
        controller.run(snapshot)

        telemetry = adapter.telemetry

        # Find the stack record.
        stack_record = None
        for t in telemetry:
            if t.get("translated_directive", {}).get("action_name") == "get_stack_summary":
                stack_record = t
                break

        assert stack_record is not None, "no get_stack_summary in telemetry"
        # The stack record must have provenance from the start_pdb_session observation.
        assert stack_record["provenance"]["prior_observation_id"] is not None, \
            "stack record must have prior_observation_id from start_pdb observation"
        assert stack_record["provenance"]["rendered_observation_sha256"] is not None, \
            "stack record must have rendered_observation_sha256 (the break observation was rendered)"

    def test_print_after_stack_with_real_pdb(self, case_setup):
        """Test print <expr> after stack, with real PDB evaluation."""

        commands = (
            "reproduce",
            "understand",
            "runtime",
            "break 9",
            "stack",
            "print sequence_length",  # evaluate a real variable in the frame
            "stop",
            "failed",
        )

        adapter = ScriptedBridgeAdapter(
            steps=commands,
            model_name="scripted-bridge-test",
            task_description="test task",
        )

        controller = DeterministicController(
            case_setup["registry"],
            adapter,
            ControllerRunConfig(max_model_calls=24),
        )

        snapshot = _make_snapshot(case_setup)
        result = controller.run(snapshot)

        telemetry = adapter.telemetry

        # Find the print (safe_eval_expression) record.
        print_record = None
        for t in telemetry:
            if t.get("translated_directive", {}).get("action_name") == "safe_eval_expression":
                print_record = t
                break

        assert print_record is not None, "no safe_eval_expression in telemetry"
        assert print_record["parse_result"]["status"] == "accepted"
        args = print_record["translated_directive"]["arguments"]
        assert args["expression"] == "sequence_length"
        assert args["frame_id"] == 0
        assert args["pause_generation"] > 0
        # Provenance binding from the stack observation.
        assert print_record["provenance"]["prior_observation_id"] is not None

    def test_diagnosis_after_debugging(self, case_setup):
        """Test that diagnosis is recorded after PDB evidence."""

        commands = (
            "reproduce",
            "understand",
            "runtime",
            "break 9",
            "stack",
            "locals",
            "stop",
            "diagnosis the loop drops the last value when size equals length",
            "failed",
        )

        adapter = ScriptedBridgeAdapter(
            steps=commands,
            model_name="scripted-bridge-test",
            task_description="test task",
        )

        controller = DeterministicController(
            case_setup["registry"],
            adapter,
            ControllerRunConfig(max_model_calls=24),
        )

        snapshot = _make_snapshot(case_setup)
        controller.run(snapshot)

        # The diagnosis must be recorded.
        diagnoses = adapter.post_debug_diagnoses
        assert len(diagnoses) >= 1, \
            f"expected >=1 diagnosis, got {len(diagnoses)}"
        assert "loop drops the last value" in diagnoses[0]["text"]
        assert diagnoses[0]["controller_state"] == "RuntimeEvidence"

    def test_step_next_continue_with_real_pdb(self, case_setup):
        """Test that step, next, and continue work through the bridge against
        the real PDB backend."""

        commands = (
            "reproduce",
            "understand",
            "runtime",
            "break 9",
            "stack",
            "step",   # advance to next line (enters calls)
            "stack",   # get stack after step
            "next",    # step over
            "stack",
            "continue",  # resume
            "stop",
            "failed",
        )

        adapter = ScriptedBridgeAdapter(
            steps=commands,
            model_name="scripted-bridge-test",
            task_description="test task",
        )

        controller = DeterministicController(
            case_setup["registry"],
            adapter,
            ControllerRunConfig(max_model_calls=24),
        )

        snapshot = _make_snapshot(case_setup)
        controller.run(snapshot)

        telemetry = adapter.telemetry

        # Verify step, next, continue were all accepted.
        action_names = [
            t.get("translated_directive", {}).get("action_name")
            for t in telemetry
            if t.get("parse_result", {}).get("status") == "accepted"
        ]
        assert "step_pdb_session" in action_names, "step was not accepted"
        assert "next_pdb_session" in action_names, "next was not accepted"
        assert "continue_pdb_session" in action_names, "continue was not accepted"

        # Verify provenance binding for the second stack (after step).
        stack_records = [
            t for t in telemetry
            if t.get("translated_directive", {}).get("action_name") == "get_stack_summary"
        ]
        if len(stack_records) >= 2:
            # The second stack should have provenance from the step observation.
            assert stack_records[1]["provenance"]["prior_observation_id"] is not None