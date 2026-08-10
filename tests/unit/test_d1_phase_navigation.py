"""Focused offline tests for the D1 phase-navigation diagnostic.

These tests prove the D1 mechanism with a deterministic/scripted inner
adapter and the REAL controller + REAL PDB backend (same pattern as the S1
integration test ``test_debugger_interaction_v2_pdb.py``):

1. Successful (real) reproduction observation
   -> deterministic administrative REPRODUCE->UNDERSTAND->RUNTIME_EVIDENCE
   -> the next model-facing request is the existing RuntimeEvidence prompt
   -> no debugger command is fabricated
   -> the next debugger/action response still comes from the model adapter.

2. failure_reproduced != true -> no forced runtime entry.

3. The administrative navigation happens exactly once; subsequent
   UNDERSTAND visits are model-authored (delegated).
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
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    ControllerSnapshot,
    ModelDirective,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.tools import (
    DemoToolContext,
    build_registry,
    prepare_pdb_probe,
)
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.events.schema import Observation, ObservationStatus
from agentic_debugger.runtime.workspace import TaskWorkspace

from experiments.debugger_interaction_v2.adapter import ScriptedBridgeAdapter
from experiments.debugger_interaction_v2.runner import _compute_gate_b
from experiments.debugger_interaction_v2_d1.d1_adapter import (
    D1PhaseNavigationAdapter,
)

TASK_ID = "curated-off-by-one-002"
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"


# ---------------------------------------------------------------------------
# Test double: recording inner adapter (for no-entry / one-time tests)
# ---------------------------------------------------------------------------


class RecordingInnerAdapter:
    """A minimal ModelAdapter double that records every delegated call."""

    def __init__(self) -> None:
        self.delegated_calls: list[ControllerState] = []
        self.telemetry_records: list[dict] = []

    @property
    def model_name(self) -> str:
        return "recording-inner"

    def next_directive(self, snapshot: ControllerSnapshot) -> ModelDirective:
        self.delegated_calls.append(snapshot.state)
        return TransitionDirective(ControllerState.FAILED, "recording inner")

    @property
    def telemetry(self) -> list[dict]:
        return list(self.telemetry_records)

    @property
    def post_debug_diagnoses(self) -> list[dict]:
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    state: ControllerState = ControllerState.REPRODUCE,
    last_observation: Observation | None = None,
    model_call_index: int = 0,
) -> ControllerSnapshot:
    return ControllerSnapshot(
        run_id="d1-test",
        task_id=TASK_ID,
        state=state,
        model_call_index=model_call_index,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=2,
            max_test_runs=5,
            max_pdb_observations=8,
        ),
        budget_state=ControllerBudgetState(),
        hypotheses=HypothesisLedger(),
        last_observation=last_observation,
    )


def _make_reproduction_observation(failure_reproduced: bool) -> Observation:
    return Observation(
        observation_id="obs-reproduction",
        action_id="act-reproduction",
        run_id="d1-test",
        task_id=TASK_ID,
        name="run_reproduction",
        status=ObservationStatus.OK,
        payload={
            "dispatch_reason": "ok",
            "exit_code": 1,
            "expected_exit_code": 1,
            "failure_reproduced": failure_reproduced,
            "node_id": "tests/test_recent_window.py::test_full_length_window_includes_every_value",
            "passed": not failure_reproduced,
            "phase": "baseline",
        },
        summary="baseline reproduction executed",
        truncated=False,
    )


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


# ---------------------------------------------------------------------------
# Test 1 — positive: verified reproduction -> admin nav -> RuntimeEvidence
# prompt -> model-authored next command -> real PDB loop
# ---------------------------------------------------------------------------


class TestD1PositiveLoop:
    def test_admin_navigation_after_successful_reproduction(self, case_setup):
        """Full loop with the REAL controller + REAL PDB:

        scripted model authors ``reproduce`` (REPRODUCE) -> D1 harness
        deterministically navigates REPRODUCE->UNDERSTAND->RUNTIME_EVIDENCE
        -> the model is next called at RUNTIME_EVIDENCE with the existing
        prompt -> the model authors ``break 9`` and ``stack`` which reach the
        real PDB backend -> Gate B passes.
        """

        # Scripted model-authored commands.  NOTE: the script does NOT
        # contain "understand"/"runtime" — those transitions are the D1
        # harness's administrative navigation, not model choices.
        commands = (
            "reproduce",   # REPRODUCE: model-authored reproduce
            "break 9",     # RUNTIME_EVIDENCE: model-authored (model chooses breakpoint)
            "stack",       # RUNTIME_EVIDENCE: model-authored
            "stop",        # RUNTIME_EVIDENCE: model-authored
            "failed",      # end the case
        )

        inner = ScriptedBridgeAdapter(
            steps=commands,
            model_name="d1-scripted-inner",
            task_description="test task",
        )
        adapter = D1PhaseNavigationAdapter(inner)

        controller = DeterministicController(
            case_setup["registry"],
            adapter,
            ControllerRunConfig(max_model_calls=24),
        )

        snapshot = _make_snapshot()
        result = controller.run(snapshot)

        # --- The controller ran and reached RUNTIME_EVIDENCE --------------
        assert result is not None
        # Final state is FAILED ("failed" ends the case); the point is that
        # the debugger loop happened before that.

        telemetry = adapter.telemetry
        admin = adapter.admin_transitions

        # --- Exactly two ADMINISTRATIVE transitions, clearly tagged -------
        assert len(admin) == 2, f"expected 2 admin transitions, got {len(admin)}"
        assert admin[0]["d1_authorship"] == "administrative"
        assert admin[0]["raw_response_status"] == "administrative_navigation"
        assert admin[0]["parse_result"]["status"] == "administrative"
        assert admin[0]["translated_directive"]["kind"] == "transition"
        assert admin[0]["translated_directive"]["target_state"] == "Understand"
        assert admin[0]["translated_directive"]["action_name"] is None
        assert admin[1]["translated_directive"]["target_state"] == "RuntimeEvidence"
        assert admin[1]["translated_directive"]["action_name"] is None

        # --- Administrative transitions consumed ZERO model calls ---------
        # The inner scripted adapter was called exactly 5 times (the 5
        # scripted commands) — the admin navigation did NOT call the model.
        model_calls = [t for t in telemetry
                       if t.get("d1_authorship") != "administrative"]
        assert len(model_calls) == 5, \
            f"expected 5 model-authored calls, got {len(model_calls)}"

        # --- Call 0: model-authored reproduce in REPRODUCE ----------------
        assert model_calls[0]["controller_state"] == "Reproduce"
        assert model_calls[0]["parse_result"]["status"] == "accepted"
        assert model_calls[0]["translated_directive"]["action_name"] == "run_reproduction"
        assert model_calls[0]["raw_response_text"] == "reproduce"

        # --- Call 1: model authored break 9 at RUNTIME_EVIDENCE -----------
        break_record = model_calls[1]
        assert break_record["controller_state"] == "RuntimeEvidence", (
            "the model's next call must face the RUNTIME_EVIDENCE boundary "
            f"after admin navigation, got {break_record['controller_state']}"
        )
        assert break_record["parse_result"]["status"] == "accepted"
        assert break_record["translated_directive"]["action_name"] == "start_pdb_session"
        assert break_record["translated_directive"]["arguments"]["breakpoint_line"] == 9
        # The model-facing request is the EXISTING RuntimeEvidence prompt.
        summary = break_record["request"]["user_prompt_summary"]
        assert "Current phase: RuntimeEvidence" in summary
        for command in ("break", "stack", "locals", "print", "step", "next",
                        "continue", "stop", "diagnosis"):
            assert command in summary, f"RuntimeEvidence prompt missing {command!r}"

        # --- Call 2: model authored stack reaches real PDB with provenance -
        stack_record = model_calls[2]
        assert stack_record["controller_state"] == "RuntimeEvidence"
        assert stack_record["parse_result"]["status"] == "accepted"
        assert stack_record["translated_directive"]["action_name"] == "get_stack_summary"
        assert stack_record["provenance"]["prior_observation_id"] is not None, (
            "the stack request must bind the real break observation provenance"
        )
        assert stack_record["provenance"]["rendered_observation_sha256"] is not None

        # --- No debugger command was fabricated ---------------------------
        # Every accepted action in telemetry is model-authored (raw text
        # present); the only non-model records are the two tagged admin
        # transitions with no action_name.
        for t in telemetry:
            if t.get("d1_authorship") == "administrative":
                assert t["translated_directive"]["action_name"] is None
            else:
                assert t["raw_response_status"] == "decoded"
                assert t["parse_result"]["status"] == "accepted"

        # --- Gate B (existing S1 computation) PASSES ----------------------
        # Exactly the 2 model-authored PDB commands count: start_pdb_session
        # and get_stack_summary.  The admin transitions cannot be counted.
        gate_b = _compute_gate_b(telemetry)
        assert gate_b["passed"] is True, f"Gate B should pass, got {gate_b}"
        assert gate_b["accepted_pdb_count"] == 2
        assert gate_b["first_command"] == "start_pdb_session"
        assert gate_b["second_command"] == "get_stack_summary"


# ---------------------------------------------------------------------------
# Test 2 — negative: failure_reproduced != true -> no forced runtime entry
# ---------------------------------------------------------------------------


class TestD1NoForcedEntry:
    def test_no_forced_entry_without_reproduction(self):
        """In REPRODUCE with no reproduction observation, the wrapper must
        delegate to the model, NOT emit an administrative transition."""

        inner = RecordingInnerAdapter()
        adapter = D1PhaseNavigationAdapter(inner)

        snapshot = _make_snapshot(state=ControllerState.REPRODUCE, last_observation=None)
        directive = adapter.next_directive(snapshot)

        assert inner.delegated_calls == [ControllerState.REPRODUCE], (
            "wrapper must delegate when reproduction has not occurred"
        )
        assert adapter.admin_transitions == [], (
            "no administrative transition may be emitted before reproduction"
        )
        # The directive came from the inner (model) adapter, not the harness.
        assert directive.target_state is ControllerState.FAILED

    def test_no_forced_entry_when_reproduction_failed(self):
        """A reproduction observation with failure_reproduced=False must NOT
        trigger runtime entry."""

        inner = RecordingInnerAdapter()
        adapter = D1PhaseNavigationAdapter(inner)

        failed_obs = _make_reproduction_observation(failure_reproduced=False)
        snapshot = _make_snapshot(
            state=ControllerState.REPRODUCE, last_observation=failed_obs
        )
        directive = adapter.next_directive(snapshot)

        assert inner.delegated_calls == [ControllerState.REPRODUCE], (
            "wrapper must delegate when failure_reproduced is not true"
        )
        assert adapter.admin_transitions == []
        assert directive.target_state is ControllerState.FAILED


# ---------------------------------------------------------------------------
# Test 3 — the administrative navigation happens exactly once; afterwards
# everything is delegated (model-authored), including UNDERSTAND visits
# ---------------------------------------------------------------------------


class TestD1OneTimeNavigation:
    def test_admin_navigation_is_one_time_then_model_authored(self):
        inner = RecordingInnerAdapter()
        adapter = D1PhaseNavigationAdapter(inner)

        success_obs = _make_reproduction_observation(failure_reproduced=True)

        # Step 1: REPRODUCE + verified reproduction -> admin to UNDERSTAND.
        d1 = adapter.next_directive(
            _make_snapshot(state=ControllerState.REPRODUCE, last_observation=success_obs)
        )
        assert isinstance(d1, TransitionDirective)
        assert d1.target_state is ControllerState.UNDERSTAND
        assert inner.delegated_calls == []

        # Step 2: UNDERSTAND -> admin to RUNTIME_EVIDENCE (navigation done).
        d2 = adapter.next_directive(
            _make_snapshot(state=ControllerState.UNDERSTAND, last_observation=success_obs)
        )
        assert isinstance(d2, TransitionDirective)
        assert d2.target_state is ControllerState.RUNTIME_EVIDENCE
        assert inner.delegated_calls == []

        # Step 3: a later UNDERSTAND visit (e.g. the model transitioned back)
        # must be DELEGATED — no second administrative navigation.
        d3 = adapter.next_directive(
            _make_snapshot(state=ControllerState.UNDERSTAND, last_observation=success_obs)
        )
        assert inner.delegated_calls == [ControllerState.UNDERSTAND]
        assert d3.target_state is ControllerState.FAILED  # from the inner adapter
        assert len(adapter.admin_transitions) == 2, (
            "administrative navigation must happen exactly once"
        )
