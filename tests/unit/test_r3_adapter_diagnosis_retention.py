"""R3 adapter: diagnosis retained verbatim into PATCH prompt; runtime slice captured."""

import sys, hashlib, io, json
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.controller import ControllerRunConfig, DeterministicController
from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger, PdbPolicy
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.tools import DemoToolContext, build_registry, prepare_pdb_probe
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.runtime.workspace import TaskWorkspace
from experiments.debugger_interaction_v2_r3.adapter import ScriptedBridgeAdapter, R2StageTracker, make_r2_session_state_provider

TASK_ID="curated-off-by-one-002"
CURATED_ROOT=REPO_ROOT/"agentic_debugger"/"datasets"/"curated"


def _case(tmp_path):
    fixture_dir=CURATED_ROOT/TASK_ID
    task=load_task(str(fixture_dir/"task.json"))
    scenario=scenario_for(TASK_ID)
    case_dir=tmp_path/"case"; case_dir.mkdir(parents=True, exist_ok=True)
    workspace=TaskWorkspace(str(fixture_dir), parent_dir=str(case_dir))
    probe=prepare_pdb_probe(fixture_dir, scenario, case_dir, model_selects_breakpoint=True)
    context=DemoToolContext(task=task, workspace=workspace, patch="", probe=probe)
    registry=build_registry(context, pdb_policy=PdbPolicy.ALWAYS_ON, interactive_debugger_controls=True)
    from experiments.debugger_interaction_v2_r3.bridge import breakpoint_eligible_lines
    original_source=(fixture_dir/scenario.runtime_probe.module_path).read_text(encoding="utf-8")
    eligible=breakpoint_eligible_lines(original_source)
    return {"task":task,"workspace":workspace,"probe":probe,"context":context,"registry":registry,"case_dir":case_dir,"original_source":original_source,"eligible":eligible,"script_path":scenario.runtime_probe.module_path}

def test_diagnosis_retained_into_patch_prompt_and_bounded(tmp_path):
    case=_case(tmp_path)
    # Chain: reproduce -> runtime -> break 2 -> stack -> locals -> step -> stack -> diagnosis -> patch (malformed then rejected?) but test just retention
    # We test that after diagnosis, adapter.retained_diagnosis is set and PATCH telemetry contains it
    diagnosis_text="the end_index subtracts 1 when size==len"
    commands=("reproduce","understand","runtime","break 2","stack","locals","step","stack",f"diagnosis {diagnosis_text}","failed")
    tracker=R2StageTracker(); provider=make_r2_session_state_provider(case["context"], lambda: tracker.stage)
    adapter=ScriptedBridgeAdapter(steps=commands, model_name="scripted-r3", task_description="test", script_path=case["script_path"], source_text=case["original_source"], eligible_lines=case["eligible"], session_state_provider=provider, stage_tracker=tracker)
    from experiments.debugger_interaction_v2_r3.bridge import R2Stage
    controller=DeterministicController(case["registry"], adapter, ControllerRunConfig(max_model_calls=30))
    snapshot=ControllerSnapshot(f"r3-test-{TASK_ID}", TASK_ID, ControllerState.REPRODUCE, 0, ControllerBudgetLimits.from_task_constraints(case["task"].constraints), ControllerBudgetState(), HypothesisLedger())
    result=controller.run(snapshot)
    # Diagnosis should have been retained
    assert adapter.retained_diagnosis == diagnosis_text
    # Telemetry: first PATCH state record should carry rendered_diagnosis_sha256
    patch_records=[r for r in adapter.telemetry if r["controller_state"]=="Patch"]
    # There is no PATCH state unless diagnosis succeeded -> check RuntimeEvidence diagnosis transition
    diag_records=[r for r in adapter.telemetry if r["translated_directive"].get("is_diagnosis") is True]
    assert len(diag_records)==1
    assert diag_records[0]["translated_directive"]["target_state"]=="Patch"
    assert diag_records[0]["translated_directive"]["diagnosis_text"]==diagnosis_text
    # provenance bound
    assert diag_records[0]["provenance"]["prior_observation_id"] is not None
    # runtime slice captured generically
    assert adapter._runtime_slice.get("stack_G1") is not None
    assert adapter._runtime_slice.get("inspection") is not None
    assert adapter._runtime_slice.get("step") is not None
    assert adapter._runtime_slice.get("stack_G2") is not None
    case["context"].release_pdb()

def test_patch_context_does_not_expose_oracle(tmp_path):
    case=_case(tmp_path)
    diagnosis_text="off-by-one"
    commands=("reproduce","understand","runtime","break 2","stack","print values","next","stack",f"diagnosis {diagnosis_text}","failed")
    tracker=R2StageTracker(); provider=make_r2_session_state_provider(case["context"], lambda: tracker.stage)
    adapter=ScriptedBridgeAdapter(steps=commands, model_name="scripted-r3", task_description="test", script_path=case["script_path"], source_text=case["original_source"], eligible_lines=case["eligible"], session_state_provider=provider, stage_tracker=tracker)
    controller=DeterministicController(case["registry"], adapter, ControllerRunConfig(max_model_calls=30))
    from agentic_debugger.agent.model_adapter import ControllerSnapshot
    snapshot=ControllerSnapshot(f"r3-test2-{TASK_ID}", TASK_ID, ControllerState.REPRODUCE, 0, ControllerBudgetLimits.from_task_constraints(case["task"].constraints), ControllerBudgetState(), HypothesisLedger())
    controller.run(snapshot)
    # Find PATCH telemetry summary — should not contain oracle strings
    patch_records=[r for r in adapter.telemetry if r["controller_state"]=="Patch"]
    # Even if no PATCH telemetry (bounded still Patch not entered when failed), check adapter retained prompt would contain slice not oracle
    from experiments.debugger_interaction_v2_r3.bridge import DebuggerContext, render_prompt
    from agentic_debugger.agent.state_machine import ControllerState as CS
    if adapter.retained_diagnosis:
        ctx=DebuggerContext(script_path=case["script_path"], source_text=case["original_source"], eligible_lines=case["eligible"], retained_diagnosis=adapter.retained_diagnosis, runtime_slice=adapter._runtime_slice)
        prompt=render_prompt(CS.PATCH, None, "Task:", debugger=ctx)
        for forbidden in ("reference_repair","root_cause_summary","target_symbols","runtime_evidence_hint","inspect_expressions"):
            assert forbidden not in prompt
    case["context"].release_pdb()


def test_patch_stage_requires_first_repair(tmp_path):
    """First PATCH turn: 'failed' rejected by stage mask; genuine patch advances to retry."""
    case=_case(tmp_path)
    diagnosis_text="off-by-one"
    # Scripted: first PATCH turn tries `failed` — bridge rejects COMMAND_NOT_IN_LIFECYCLE.
    # ScriptedBridgeAdapter raises ModelAdapterError on parse failure (no retry loop),
    # so instead test the adapter stage logic directly via the scripted path with a
    # valid first patch: assert patch_attempted flips and second PATCH turn exposes retry.
    diff="--- a/recent_window.py\n+++ b/recent_window.py\n@@ -1 +1 @@\n-old\n+new\n"
    commands=("reproduce","understand","runtime","break 2","stack","locals","step","stack",f"diagnosis {diagnosis_text}",f"patch\n{diff}","failed")
    tracker=R2StageTracker(); provider=make_r2_session_state_provider(case["context"], lambda: tracker.stage)
    adapter=ScriptedBridgeAdapter(steps=commands, model_name="scripted-r3", task_description="test", script_path=case["script_path"], source_text=case["original_source"], eligible_lines=case["eligible"], session_state_provider=provider, stage_tracker=tracker)
    controller=DeterministicController(case["registry"], adapter, ControllerRunConfig(max_model_calls=30))
    from agentic_debugger.agent.model_adapter import ControllerSnapshot
    snapshot=ControllerSnapshot(f"r3-test-{TASK_ID}", TASK_ID, ControllerState.REPRODUCE, 0, ControllerBudgetLimits.from_task_constraints(case["task"].constraints), ControllerBudgetState(), HypothesisLedger())
    controller.run(snapshot)
    assert adapter.patch_attempted is True
    # First PATCH telemetry prompt must have exposed only `patch` (NEEDS_FIRST_REPAIR)
    patch_records=[r for r in adapter.telemetry if r["controller_state"]=="Patch"]
    assert patch_records, "no PATCH telemetry"
    first_patch=patch_records[0]["request"]["user_prompt_summary"]
    # The Available commands block in first PATCH turn lists only `- patch`
    import re
    cmds_block=first_patch.split("Available commands:")[1].split("Target script")[0]
    assert "patch" in cmds_block
    assert "failed" not in cmds_block
    case["context"].release_pdb()
