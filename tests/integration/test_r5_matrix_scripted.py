"""R5 scripted e2e: generalized debugger chain -> diagnosis -> patch ->
verifier across ALL five curated tasks, using real PDB (pytest launcher
target), real PatchManager, real EvaluationVerifier.

Reference repairs are used ONLY as scripted test doubles inside this
engineering test; they never enter any live prompt or live semantic path.
For curated-none-handling-001 the test documents the structural boundary of
the crash-on-first-line bug class (no post-step production pause exists),
proving deterministically that the strict chain cannot pass for that task by
construction — not by model incapacity and not by a harness defect.
"""

from __future__ import annotations

import hashlib
import io
import json
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
from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
from agentic_debugger.demo.tools import DemoToolContext, build_registry
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.workspace import TaskWorkspace

from experiments.debugger_interaction_v2_r5.adapter import R5StageTracker, ScriptedBridgeAdapter, make_r5_session_state_provider
from experiments.debugger_interaction_v2_r5.bridge import breakpoint_eligible_lines
from experiments.debugger_interaction_v2_r5.launcher import (
    fixture_tree_sha256,
    prepare_r5_probe,
    task_target_module_path,
)
from experiments.debugger_interaction_v2_r5.r5_runner import (
    R5_TASKS,
    _compute_gate_r5_chain,
    _compute_gate_r5_patch,
    _first_causal_failure,
)

CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"

# Real debugger sessions must use a generous fixed timeout in tests (the
# contract timeout is frozen from measured latency before live runs).
TEST_PDB_TIMEOUT = 60.0


def _session_factory(workspace):
    return PdbSession(
        workspace,
        startup_timeout=TEST_PDB_TIMEOUT,
        request_timeout=TEST_PDB_TIMEOUT,
        shutdown_timeout=5.0,
    )


@pytest.fixture
def case_setup(tmp_path):
    return {"tmp": tmp_path}


def _run(task_id, commands, tmp_path, verifier_feedback_fn=None):
    fixture_dir = CURATED_ROOT / task_id
    task = load_task(str(fixture_dir / "task.json"))
    module_path = task_target_module_path(task)
    original_source = (fixture_dir / module_path).read_text(encoding="utf-8")
    original_sha = hashlib.sha256(original_source.encode("utf-8")).hexdigest()
    line_count = len(original_source.splitlines())
    eligible = breakpoint_eligible_lines(original_source)

    case_dir = tmp_path / f"case-{task_id}"
    case_dir.mkdir(parents=True, exist_ok=True)

    before_hash = fixture_tree_sha256(fixture_dir)
    r5_probe = prepare_r5_probe(
        fixture_dir, module_path, task.reproduction.argv, case_dir,
        original_source_sha256=original_sha,
        original_source_line_count=line_count,
        eligible_lines=eligible,
        task_id=task_id,
    )
    assert r5_probe.driver_start_line == line_count + 1
    after_hash = fixture_tree_sha256(fixture_dir)
    assert before_hash == after_hash, "canonical fixture changed by probe prep"

    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_dir))
    context = DemoToolContext(
        task=task, workspace=workspace, patch="", probe=r5_probe.probe,
        pdb_session_factory=_session_factory,
        verifier_feedback_fn=verifier_feedback_fn,
    )
    registry = build_registry(context, pdb_policy=PdbPolicy.ALWAYS_ON, interactive_debugger_controls=True)

    tracker = R5StageTracker()
    provider = make_r5_session_state_provider(context, lambda: tracker.stage)
    adapter = ScriptedBridgeAdapter(
        steps=commands, model_name="scripted-r5",
        task_description=f"Title: {task.title}\nDescription: {task.description}",
        script_path=module_path, source_text=original_source,
        eligible_lines=eligible, original_line_count=line_count,
        session_state_provider=provider, stage_tracker=tracker,
    )
    controller = DeterministicController(
        registry, adapter, ControllerRunConfig(max_model_calls=32),
    )
    snapshot = ControllerSnapshot(
        f"r5-test-{task_id}", task_id, ControllerState.REPRODUCE, 0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(), HypothesisLedger(),
    )
    result = controller.run(snapshot)
    stream = io.StringIO()
    logger = JsonlEventLogger(result.run_id, result.task_id, stream=stream)
    for ev in project_controller_run(result, tool_version="debugger-interaction-v2-r5", model=adapter.model_name, timestamp="2026-01-01T00:00:00Z", duration_ms=0):
        logger.append(ev)
    logger.flush(); logger.close()
    traj = stream.getvalue()

    candidate = context.candidate_patch
    verifier_result = None
    if candidate:
        ev = EvaluationVerifier(str(REPO_ROOT), workspace_parent=str(case_dir)).evaluate(task, candidate)
        verifier_result = {
            "executed": True,
            "candidate_sha256": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
            "status": ev.status.value if hasattr(ev.status, "value") else str(ev.status),
            "outcome": ev.outcome.value if hasattr(ev.outcome, "value") else str(ev.outcome),
            "f2p_records": [{"node_id": r.node_id, "status": r.status.value if hasattr(r.status, "value") else str(r.status)} for r in ev.post_patch_f2p],
            "p2p_records": [{"node_id": r.node_id, "status": r.status.value if hasattr(r.status, "value") else str(r.status)} for r in ev.post_patch_p2p],
            "f2p_total": ev.f2p_total, "f2p_passed": ev.f2p_passed,
            "p2p_total": ev.p2p_total, "p2p_passed": ev.p2p_passed,
            "full_suite_consistent": (
                ev.full_suite.status.value if ev.full_suite is not None and hasattr(ev.full_suite.status, "value") else None
            ) == "PASS" if ev.full_suite is not None else None,
            "syntax_passed": ev.syntax.passed if ev.syntax else None,
            "canonical_fixture_unchanged": ev.workspace.canonical_fixture_unchanged if ev.workspace else None,
            "workspace_lifecycle": ev.workspace.lifecycle.value if ev.workspace and hasattr(ev.workspace.lifecycle, "value") else None,
        }
    else:
        verifier_result = {"executed": False}

    chain_gate = _compute_gate_r5_chain(
        adapter.telemetry, traj,
        expected_script=module_path, original_line_count=line_count,
    )
    patch_gate = _compute_gate_r5_patch(
        adapter.telemetry, traj, verifier_result,
        candidate_patch=candidate, inner_adapter=adapter,
        expected_script=module_path, original_line_count=line_count,
        f2p_count=len(task.tests.fail_to_pass),
        p2p_count=len(task.tests.pass_to_pass),
    )
    evidence = {
        "gate_results": {"gate_chain": chain_gate, "gate_patch": patch_gate},
        "verifier": verifier_result,
        "candidate_patch": candidate,
        "telemetry": adapter.telemetry,
        "controller_result": {"final_state": result.final_state.value if hasattr(result.final_state, "value") else str(result.final_state), "stop_reason": result.stop_reason.value if hasattr(result.stop_reason, "value") else str(result.stop_reason)},
        "patch_identity": {"attempts": adapter.patch_attempts},
        "diagnosis_provenance": adapter.diagnosis_provenance,
    }
    try:
        context.release_pdb()
    finally:
        try:
            workspace.cleanup()
        except Exception:
            pass
    return {
        "task_id": task_id, "module_path": module_path,
        "line_count": line_count, "eligible": eligible,
        "candidate": candidate, "verifier": verifier_result,
        "chain": chain_gate, "patch": patch_gate,
        "adapter": adapter, "evidence": evidence,
        "verifier_feedback_history": list(getattr(context, "verifier_feedback_history", []) or []),
    }


def _reference_diff(task_id, module_path):
    scenario = scenario_for(task_id)
    original = (CURATED_ROOT / task_id / module_path).read_text(encoding="utf-8")
    return build_reference_patch(original, scenario.reference_repair)


class TestR5MatrixScripted:
    @pytest.mark.parametrize("task_id", [
        "curated-off-by-one-002",
        "curated-wrong-branch-003",
        "curated-mutation-alias-004",
        "curated-caller-callee-005",
    ])
    def test_chain_diagnosis_patch_verifier_resolved(self, task_id, tmp_path):
        """Structural proof: the generalized harness can reach RESOLVED on
        each logic-bug task with real PDB/PatchManager/Verifier when a
        scripted model follows the staged chain (reference repair as test
        double only)."""
        diff = _reference_diff(task_id, task_target_module_path(load_task(str(CURATED_ROOT / task_id / "task.json"))))
        commands = (
            "reproduce", "understand", "runtime", "break 2", "stack", "locals",
            "step", "stack",
            "diagnosis scripted test double diagnosis",
            f"patch\n{diff}",
        )
        out = _run(task_id, commands, tmp_path)
        assert out["chain"]["passed"] is True, out["chain"]
        assert out["candidate"] is not None
        assert out["verifier"]["executed"] is True
        assert out["verifier"]["status"] == "COMPLETED"
        assert out["verifier"]["outcome"] == "RESOLVED"
        assert out["patch"]["passed"] is True, out["patch"]

    def test_print_and_next_branches(self, tmp_path):
        """safe_eval + next branch on caller-callee (print a callee local)."""
        task_id = "curated-caller-callee-005"
        diff = _reference_diff(task_id, task_target_module_path(load_task(str(CURATED_ROOT / task_id / "task.json"))))
        commands = (
            "reproduce", "understand", "runtime", "break 2", "stack",
            "print cents", "next", "stack",
            "diagnosis scripted print/next test double",
            f"patch\n{diff}",
        )
        out = _run(task_id, commands, tmp_path)
        assert out["chain"]["passed"] is True, out["chain"]
        assert out["verifier"]["outcome"] == "RESOLVED"
        assert out["patch"]["passed"] is True, out["patch"]

    def test_none_handling_terminal_path_resolved(self, tmp_path):
        """curated-none-handling-001: the failing execution crashes on the
        first executable production line (None.strip()).  A real pause exists
        (break/stack/locals at G1); step/next crashes the target, so no
        post-step production pause (G2) can exist.  R5.2 terminal runtime
        progression preserves the real terminal observation (exited) plus the
        real reproduction failure output, allows diagnosis from that evidence,
        and reaches a real verifier RESOLVED — the structural boundary is
        removed by a common treatment, not by a per-task special case."""
        task_id = "curated-none-handling-001"
        module_path = task_target_module_path(load_task(str(CURATED_ROOT / task_id / "task.json")))
        diff = _reference_diff(task_id, module_path)
        commands = (
            "reproduce", "understand", "runtime", "break 2", "stack", "locals",
            "next", "diagnosis scripted terminal-path test double diagnosis",
            f"patch\n{diff}",
        )
        out = _run(task_id, commands, tmp_path)
        chain = out["chain"]
        assert chain["passed"] is True, chain
        assert chain.get("terminal_path") is True, chain
        assert chain.get("G1") is not None
        assert chain.get("G2") is None
        assert out["candidate"] is not None
        assert out["verifier"]["executed"] is True
        assert out["verifier"]["status"] == "COMPLETED"
        assert out["verifier"]["outcome"] == "RESOLVED"
        assert out["patch"]["passed"] is True, out["patch"]

    def test_fenced_patch_diff_unwrapped_end_to_end(self, tmp_path):
        """Deterministic single-fence unwrap: a scripted model response that
        wraps the patch diff in one markdown fence with trailing prose must
        be unwrapped (bare-fence shape, command synthesized), normalized,
        applied and verifier-RESOLVED."""
        task_id = "curated-off-by-one-002"
        module_path = task_target_module_path(load_task(str(CURATED_ROOT / task_id / "task.json")))
        diff = _reference_diff(task_id, module_path)
        fenced = "```diff\n" + diff + "```\n\nThis patch ensures the boundary is included."
        commands = (
            "reproduce", "understand", "runtime", "break 2", "stack", "locals",
            "step", "stack", "diagnosis scripted fenced-diff test double",
            fenced,
        )
        out = _run(task_id, commands, tmp_path)
        assert out["chain"]["passed"] is True, out["chain"]
        attempts = out["adapter"].patch_attempts
        assert attempts, "no patch attempt recorded"
        unwrap = attempts[-1].get("fence_unwrap", {})
        assert unwrap.get("unwrapped") is True, unwrap
        assert unwrap.get("shape") == "bare_fence", unwrap
        assert unwrap.get("synthesized_patch_command") is True, unwrap
        assert unwrap.get("trailing_prose_bytes") > 0, unwrap
        assert out["verifier"]["outcome"] == "RESOLVED"
        assert out["patch"]["passed"] is True, out["patch"]

    def test_patch_command_plus_fenced_diff_unwrapped(self, tmp_path):
        """patch_plus_fence shape: 'patch' command line followed by one
        fenced diff block is unwrapped deterministically."""
        task_id = "curated-wrong-branch-003"
        module_path = task_target_module_path(load_task(str(CURATED_ROOT / task_id / "task.json")))
        diff = _reference_diff(task_id, module_path)
        commands = (
            "reproduce", "understand", "runtime", "break 5", "stack", "locals",
            "step", "stack", "diagnosis scripted patch+fence test double",
            f"patch\n```python\n{diff}\n```",
        )
        out = _run(task_id, commands, tmp_path)
        assert out["chain"]["passed"] is True, out["chain"]
        attempts = out["adapter"].patch_attempts
        assert attempts, "no patch attempt recorded"
        unwrap = attempts[-1].get("fence_unwrap", {})
        assert unwrap.get("unwrapped") is True, unwrap
        assert unwrap.get("shape") == "patch_plus_fence", unwrap
        assert unwrap.get("synthesized_patch_command") is False, unwrap
        assert out["verifier"]["outcome"] == "RESOLVED"
        assert out["patch"]["passed"] is True, out["patch"]

    def test_verifier_feedback_loop_revises_patch_to_resolved(self, tmp_path):
        """Real verifier-feedback repair loop: a first WRONG model patch is
        applied, the independent EvaluationVerifier returns real failure
        diagnostics (bound into the apply_patch observation), the retry
        replaces the candidate (auto-revert) with the corrected repair, and
        the final verifier is RESOLVED."""
        task_id = "curated-caller-callee-005"
        module_path = task_target_module_path(load_task(str(CURATED_ROOT / task_id / "task.json")))
        wrong_diff = (
            "--- a/price.py\n"
            "+++ b/price.py\n"
            "@@ -10,4 +10,4 @@ def format_price(amount: int, representation: str) -> str:\n"
            "     caller_amount = amount\n"
            "     caller_representation = representation\n"
            "     callee_input = caller_amount\n"
            "-    return _format_price(callee_input)\n"
            "+    return _format_price(caller_amount, caller_representation)\n"
        )
        fixed_diff = _reference_diff(task_id, module_path)

        def feedback_fn(task, diff):
            evaluation = EvaluationVerifier(str(REPO_ROOT), workspace_parent=None).evaluate(task, diff)
            failures = []
            for kind, records in (("f2p", evaluation.post_patch_f2p), ("p2p", evaluation.post_patch_p2p)):
                for record in records:
                    status = record.status.value if hasattr(record.status, "value") else str(record.status)
                    if status != "PASS":
                        failures.append({"kind": kind, "node_id": record.node_id, "status": status, "detail": (record.stdout or "")[-600:]})
            return {
                "status": evaluation.status.value if hasattr(evaluation.status, "value") else str(evaluation.status),
                "outcome": evaluation.outcome.value if hasattr(evaluation.outcome, "value") else str(evaluation.outcome),
                "f2p_total": evaluation.f2p_total, "f2p_passed": evaluation.f2p_passed,
                "p2p_total": evaluation.p2p_total, "p2p_passed": evaluation.p2p_passed,
                "full_suite": evaluation.full_suite.status.value if evaluation.full_suite else None,
                "syntax": evaluation.syntax.passed if evaluation.syntax else None,
                "failures": failures[:3],
            }

        commands = (
            "reproduce", "understand", "runtime", "break 2", "stack",
            "print cents", "next", "stack", "diagnosis scripted feedback-loop test double",
            f"patch\n{wrong_diff}",
            f"patch\n{fixed_diff}",
        )
        out = _run(task_id, commands, tmp_path, verifier_feedback_fn=feedback_fn)
        # Real verifier feedback was recorded for BOTH accepted patches.
        history = out["verifier_feedback_history"]
        assert len(history) == 2, history
        assert history[0]["outcome"] == "REGRESSION", history[0]
        assert history[0]["f2p_passed"] == 0, history[0]
        assert any("TypeError" in (f.get("detail") or "") for f in history[0].get("failures", [])), history[0]
        assert history[1]["outcome"] == "RESOLVED", history[1]
        assert out["verifier"]["outcome"] == "RESOLVED", out["verifier"]
        assert out["patch"]["passed"] is True, out["patch"]
        attempts = out["adapter"].patch_attempts
        assert len(attempts) == 2, attempts
        # First (wrong) patch applied; second replaced it via auto-revert.
        assert out["candidate"] == fixed_diff

    def test_live_semantic_path_has_no_reference_repair_lookup(self):
        """Static proof: the R5 live path never looks up reference repairs."""
        import experiments.debugger_interaction_v2_r5.launcher as launcher_mod
        import experiments.debugger_interaction_v2_r5.r5_runner as runner_mod
        import experiments.debugger_interaction_v2_r5.bridge as bridge_mod
        import experiments.debugger_interaction_v2_r5.adapter as adapter_mod
        for mod in (launcher_mod, runner_mod, bridge_mod, adapter_mod):
            source = Path(mod.__file__).read_text(encoding="utf-8")
            for needle in ("scenario_for", "build_reference_patch", "probe_driver_source", "reference_repair"):
                assert needle not in source, f"{mod.__name__} references {needle}"
