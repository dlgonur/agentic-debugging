"""R3 scripted e2e: debugger chain -> diagnosis -> patch -> verifier RESOLVED.

Uses ScriptedBridgeAdapter with real PDB and real PatchManager/Verifier.
Reference repair is used ONLY as test double diff, never injected into prompt.
"""

import sys, hashlib, io, json
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.controller import ControllerRunConfig, DeterministicController
from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger, PdbPolicy
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.demo.catalog import scenario_for, build_reference_patch
from agentic_debugger.demo.tools import DemoToolContext, build_registry, prepare_pdb_probe
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.runtime.workspace import TaskWorkspace

from experiments.debugger_interaction_v2_r3.adapter import ScriptedBridgeAdapter, R2StageTracker, make_r2_session_state_provider
from experiments.debugger_interaction_v2_r3.bridge import breakpoint_eligible_lines
from experiments.debugger_interaction_v2_r3.r3_runner import _compute_gate_r2, _compute_gate_r3

TASK_ID="curated-off-by-one-002"
CURATED_ROOT=REPO_ROOT/"agentic_debugger"/"datasets"/"curated"

import pytest

@pytest.fixture
def case_setup(tmp_path):
    fixture_dir=CURATED_ROOT/TASK_ID
    task=load_task(str(fixture_dir/"task.json"))
    scenario=scenario_for(TASK_ID)
    case_dir=tmp_path/"case"; case_dir.mkdir(parents=True, exist_ok=True)
    workspace=TaskWorkspace(str(fixture_dir), parent_dir=str(case_dir))
    probe=prepare_pdb_probe(fixture_dir, scenario, case_dir, model_selects_breakpoint=True)
    context=DemoToolContext(task=task, workspace=workspace, patch="", probe=probe)
    registry=build_registry(context, pdb_policy=PdbPolicy.ALWAYS_ON, interactive_debugger_controls=True)
    original_source=(fixture_dir/scenario.runtime_probe.module_path).read_text(encoding="utf-8")
    eligible=breakpoint_eligible_lines(original_source)
    # Build the exact reference diff deterministically (verified against fixture bytes)
    diff=build_reference_patch(original_source, scenario.reference_repair)
    yield {"task":task,"workspace":workspace,"probe":probe,"context":context,"registry":registry,"case_dir":case_dir,"original_source":original_source,"eligible":eligible,"script_path":scenario.runtime_probe.module_path,"diff":diff}
    context.release_pdb()


def _run(case_setup, commands, expected_diff=None):
    tracker=R2StageTracker(); provider=make_r2_session_state_provider(case_setup["context"], lambda: tracker.stage)
    adapter=ScriptedBridgeAdapter(steps=commands, model_name="scripted-r3", task_description="test", script_path=case_setup["script_path"], source_text=case_setup["original_source"], eligible_lines=case_setup["eligible"], session_state_provider=provider, stage_tracker=tracker)
    controller=DeterministicController(case_setup["registry"], adapter, ControllerRunConfig(max_model_calls=32))
    snapshot=ControllerSnapshot(f"r3-test-{TASK_ID}", TASK_ID, ControllerState.REPRODUCE, 0, ControllerBudgetLimits.from_task_constraints(case_setup["task"].constraints), ControllerBudgetState(), HypothesisLedger())
    result=controller.run(snapshot)
    telemetry=adapter.telemetry
    stream=io.StringIO(); logger=JsonlEventLogger(result.run_id, result.task_id, stream=stream)
    for ev in project_controller_run(result, tool_version="debugger-interaction-v2-r3", model=adapter.model_name, timestamp="2026-01-01T00:00:00Z", duration_ms=0):
        logger.append(ev)
    logger.flush(); logger.close(); traj=stream.getvalue()
    candidate=case_setup["context"].candidate_patch
    verifier_result=None
    if candidate:
        ev=EvaluationVerifier(str(REPO_ROOT), workspace_parent=str(case_setup["case_dir"])).evaluate(case_setup["task"], candidate)
        verifier_result={"executed":True,"status":ev.status.value if hasattr(ev.status,"value") else str(ev.status),"outcome":ev.outcome.value if hasattr(ev.outcome,"value") else str(ev.outcome),"f2p_passed":[r.status.value for r in ev.post_patch_f2p],"p2p_passed":[r.status.value for r in ev.post_patch_p2p],"f2p_records":[{"node_id":r.node_id,"status":r.status.value} for r in ev.post_patch_f2p],"p2p_records":[{"node_id":r.node_id,"status":r.status.value} for r in ev.post_patch_p2p]}
    else:
        verifier_result={"executed":False}
    return {"result":result,"telemetry":telemetry,"traj":traj,"candidate":candidate,"verifier":verifier_result,"adapter":adapter}


class TestR3E2E:
    def test_locals_path_resolved(self, case_setup):
        diff=case_setup["diff"]
        commands=("reproduce","understand","runtime","break 2","stack","locals","step","stack","diagnosis boundary off-by-one in window calculation",f"patch\n{diff}")
        out=_run(case_setup, commands)
        assert out["adapter"].retained_diagnosis is not None
        assert out["candidate"] is not None
        assert out["candidate"].encode("utf-8")==diff.encode("utf-8") or diff in out["candidate"]
        assert out["verifier"]["executed"] is True
        assert out["verifier"]["status"]=="COMPLETED"
        assert out["verifier"]["outcome"]=="RESOLVED"
        # gate R3 strict
        gate=_compute_gate_r3(out["telemetry"], out["traj"], out["verifier"], candidate_patch=out["candidate"], inner_adapter=out["adapter"], expected_script="recent_window.py")
        assert gate["passed"] is True, gate
        # Three patch representations: raw contains patch prefix, B is diff
        raw=[r for r in out["telemetry"] if r["translated_directive"].get("action_name")=="apply_patch"][0]["raw_response_text"]
        assert raw.startswith("patch")
        assert hashlib.sha256(out["candidate"].encode("utf-8")).hexdigest()==hashlib.sha256(diff.encode("utf-8")).hexdigest()

    def test_print_path_resolved(self, case_setup):
        diff=case_setup["diff"]
        commands=("reproduce","understand","runtime","break 2","stack","print values","next","stack","diagnosis safe_eval path also off-by-one",f"patch\n{diff}")
        out=_run(case_setup, commands)
        assert out["verifier"]["outcome"]=="RESOLVED"
        gate=_compute_gate_r3(out["telemetry"], out["traj"], out["verifier"], candidate_patch=out["candidate"], inner_adapter=out["adapter"], expected_script="recent_window.py")
        assert gate["passed"] is True, gate

    def test_verifier_non_resolved_causes_gate_fail(self, case_setup):
        # Submit a trivial non-fixing diff — verifier should not be RESOLVED and gate must fail
        bad_diff="--- a/recent_window.py\n+++ b/recent_window.py\n@@ -1,3 +1,3 @@\n def recent_window(values, size):\n-    sequence_length = len(values)\n+    sequence_length = len(values)  # no fix\n"
        commands=("reproduce","understand","runtime","break 2","stack","locals","step","stack","diagnosis nonsense",f"patch\n{bad_diff}")
        out=_run(case_setup, commands)
        # candidate exists but not resolved
        if out["verifier"]["executed"]:
            assert out["verifier"]["outcome"]!="RESOLVED"
            gate=_compute_gate_r3(out["telemetry"], out["traj"], out["verifier"], candidate_patch=out["candidate"], inner_adapter=out["adapter"], expected_script="recent_window.py")
            assert gate["passed"] is False
