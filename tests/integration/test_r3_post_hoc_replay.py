"""R3.2 — POST-HOC REPLAY of frozen R3.1 live B through the real project path.

This is explicitly NOT a new live-model result.  The frozen R3.1 B
(`tests/fixtures/r31_model_patch_raw.patch`, SHA 831b1c2b...) is normalized
to C by the metadata-only hunk-count normalizer, then C is dispatched to the
real PatchManager and the independent EvaluationVerifier.

Requirement proof:
    normalization output SHA == PatchManager input SHA
    PatchManager accepted candidate SHA == verifier input SHA
    verifier COMPLETED / RESOLVED / F2P 1/1 / P2P 2/2 / full-suite / syntax
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.controller_policy import PdbPolicy
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.tools import DemoToolContext, build_registry, prepare_pdb_probe
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.runtime.workspace import TaskWorkspace
from experiments.debugger_interaction_v2_r3.serialization import normalize_hunk_counts

TASK_ID = "curated-off-by-one-002"
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "r31_model_patch_raw.patch"
R31_B_SHA256 = "831b1c2bc347c9812296de5ddb7ebac5f6f414bbd6512561b4cb29066e6e2c76"


def _normalize_frozen_b() -> tuple[str, str, object]:
    b = FIXTURE.read_text(encoding="utf-8")
    assert hashlib.sha256(b.encode("utf-8")).hexdigest() == R31_B_SHA256
    c, record = normalize_hunk_counts(b)
    return b, c, record


def _apply_and_verify(c: str, tmp_path) -> dict[str, object]:
    fixture_dir = CURATED_ROOT / TASK_ID
    task = load_task(str(fixture_dir / "task.json"))
    scenario = scenario_for(TASK_ID)
    case_dir = tmp_path / "case"
    case_dir.mkdir(parents=True, exist_ok=True)
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_dir))
    probe = prepare_pdb_probe(fixture_dir, scenario, case_dir, model_selects_breakpoint=True)
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=probe)
    registry = build_registry(context, pdb_policy=PdbPolicy.ALWAYS_ON, interactive_debugger_controls=True)
    try:
        # Real PatchManager path through the registered apply_patch tool
        from agentic_debugger.agent.model_adapter import ActionDirective
        from agentic_debugger.events.schema import Action, ObservationStatus
        from agentic_debugger.agent.state_machine import ControllerState
        from agentic_debugger.agent.controller_policy import ActionName
        action = Action(
            action_id="action-replay",
            run_id="replay",
            task_id=TASK_ID,
            state=ControllerState.PATCH,
            name=ActionName.APPLY_PATCH.value,
            arguments={"patch": c},
        )
        observation = registry.dispatch(action, observation_id="observation-replay")
        assert observation.status is ObservationStatus.OK, observation.payload
        candidate = context.candidate_patch
        assert candidate == c, "PatchManager accepted candidate must equal C"
        # Independent verifier on exact C bytes
        evaluation = EvaluationVerifier(str(REPO_ROOT), workspace_parent=str(case_dir)).evaluate(task, candidate)
        return {
            "candidate": candidate,
            "status": evaluation.status.value if hasattr(evaluation.status, "value") else str(evaluation.status),
            "outcome": evaluation.outcome.value if hasattr(evaluation.outcome, "value") else str(evaluation.outcome),
            "stop_reason": evaluation.stop_reason,
            "f2p": [r.status.value for r in evaluation.post_patch_f2p],
            "p2p": [r.status.value for r in evaluation.post_patch_p2p],
            "syntax_passed": evaluation.syntax.passed if evaluation.syntax else None,
            "full_suite": evaluation.full_suite.status.value if evaluation.full_suite else None,
            "patch_applied": evaluation.patch_application.success,
            "workspace_lifecycle": evaluation.workspace.lifecycle.value if evaluation.workspace else None,
            "canonical_unchanged": evaluation.workspace.canonical_fixture_unchanged if evaluation.workspace else None,
        }
    finally:
        context.release_pdb()


class TestPostHocReplay:
    def test_frozen_r31_b_normalized_reaches_resolved(self, tmp_path):
        b, c, record = _normalize_frozen_b()
        # strict B apply fact: raw B with -7,7 +7,7 was rejected in R3.1 (historical)
        assert "@@ -7,7 +7,7 @@" in b
        assert "@@ -7,6 +7,6 @@" in c
        assert record.header_fields_changed == 2
        assert record.fingerprint_equal is True

        result = _apply_and_verify(c, tmp_path)
        # PatchManager input == C (normalization output)
        assert hashlib.sha256(c.encode("utf-8")).hexdigest() == record.model_patch_serialization_normalized_sha256
        # verifier input == accepted candidate == C
        assert hashlib.sha256(result["candidate"].encode("utf-8")).hexdigest() == record.model_patch_serialization_normalized_sha256

        assert result["status"] == "COMPLETED"
        assert result["outcome"] == "RESOLVED"
        assert result["f2p"] == ["PASS"]
        assert result["p2p"] == ["PASS", "PASS"]
        assert result["syntax_passed"] is True
        assert result["full_suite"] == "PASS"
        assert result["patch_applied"] is True
        assert result["workspace_lifecycle"] == "CLEANED"
        assert result["canonical_unchanged"] is True
