from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.model_adapter import (
    ControllerSnapshot,
    ScriptedModelAdapter,
    ScriptedModelStep,
    TransitionDirective,
)
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.proof_gate import validate_pdb_patch_evidence
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry
from agentic_debugger.demo.catalog import exact_pytest_driver_source, scenario_for
from agentic_debugger.demo.tools import prepare_pdb_probe
from agentic_debugger.evaluation.private_checks import _PRIVATE_SOURCE
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.events.schema import Action, Observation, ObservationStatus
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.workspace import TaskWorkspace


ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "pdb-required-boundary-006"
FIXTURE = ROOT / "agentic_debugger" / "datasets" / "curated" / TASK_ID


def _observation(name: str, payload: dict[str, object], index: int) -> Observation:
    return Observation(
        observation_id=f"observation-{index:09d}",
        action_id=f"action-{index:09d}",
        run_id="run-proof",
        task_id=TASK_ID,
        name=name,
        status=ObservationStatus.OK,
        payload=payload,
        summary=name,
        truncated=False,
    )


def _contract() -> dict[str, object]:
    return {
        "exact_reproduction": True,
        "task_id": TASK_ID,
        "reproduction_argv": [
            "python", "-m", "pytest",
            "tests/test_window_tail.py::test_full_window_keeps_the_boundary_value",
            "-q", "-p", "no:cacheprovider",
        ],
        "pytest_node": "tests/test_window_tail.py::test_full_window_keeps_the_boundary_value",
        "workspace_id": "workspace-proof",
        "production_file": "window_tail.py",
        "production_file_sha256": "a" * 64,
        "breakpoint_line": 9,
        "production_frame": "tail_window",
    }


def _complete_observations() -> list[Observation]:
    contract = _contract()
    proof = {"proof": contract}
    return [
        _observation(
            "run_reproduction",
            {
                "phase": "baseline",
                "node_id": contract["pytest_node"],
                "reproduction_argv": contract["reproduction_argv"],
                "failure_reproduced": True,
            },
            0,
        ),
        _observation(
            "start_pdb_session",
            {**proof, "state": "paused", "script": "window_tail.py", "function": "tail_window"},
            1,
        ),
        _observation(
            "get_stack_summary",
            {
                **proof,
                "frames": [{"frame_id": 0, "script": "window_tail.py", "function": "tail_window", "is_current": True}],
            },
            2,
        ),
        _observation(
            "get_frame_locals",
            {
                **proof,
                "state": "paused",
                "frame_id": 0,
                "locals": [{"name": "requested_size", "value": {"type": "int", "preview": "4"}}],
            },
            3,
        ),
        _observation(
            "step_pdb_session",
            {**proof, "state": "paused", "script": "window_tail.py", "function": "tail_window"},
            4,
        ),
        _observation(
            "express_root_cause_hypothesis",
            {
                "evidence_refs": [
                    "observation-000000001",
                    "observation-000000002",
                    "observation-000000003",
                    "observation-000000004",
                ],
                "observed_values": {
                    "requested_size": {"type": "int", "preview": "4"}
                },
                "proof_contract": contract,
            },
            5,
        ),
    ]


def test_patch_is_rejected_before_the_opt_in_proof_gate() -> None:
    adapter = ScriptedModelAdapter(
        (
            ScriptedModelStep(
                ControllerState.UNDERSTAND,
                TransitionDirective(ControllerState.PATCH, "premature patch"),
            ),
        )
    )
    result = DeterministicController(
        ToolRegistry(),
        adapter,
        ControllerRunConfig(1, require_pdb_evidence_before_patch=True),
    ).run(
        ControllerSnapshot(
            "run-proof", TASK_ID, ControllerState.UNDERSTAND, 0,
            ControllerBudgetLimits(2, 4, 6, max_active_hypotheses=2, max_source_observations=3),
            ControllerBudgetState(), HypothesisLedger(),
        )
    )
    assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED


@pytest.mark.parametrize(
    "mutator",
    [
        lambda observations: observations[0].payload.update({"reproduction_argv": ["python", "-m", "pytest", "wrong.py::test_wrong"]}),
        lambda observations: observations[2].payload.update({"proof": {**_contract(), "pytest_node": "wrong::node"}}),
        lambda observations: observations[2].payload.update({"proof": {**_contract(), "production_file_sha256": "b" * 64}}),
        lambda observations: observations[2].payload.update({"frames": [{"script": "window_tail.py", "function": "other_function"}]}),
        lambda observations: observations.pop(4),
        lambda observations: observations[5].payload.update({"evidence_refs": ["observation-999999999"]}),
        lambda observations: observations[5].payload.update({"observed_values": {"requested_size": {"type": "int", "preview": "9"}}}),
    ],
)
def test_mismatched_or_stale_proof_evidence_fails_closed(mutator) -> None:
    observations = _complete_observations()
    mutator(observations)
    allowed, reason = validate_pdb_patch_evidence(observations)
    assert allowed is False
    assert reason


@pytest.mark.parametrize(
    "mutator",
    [
        lambda observations: observations[0].payload.update({"reproduction_argv": None}),
        lambda observations: observations[5].payload.update({"evidence_refs": ["observation-000000001", "observation-000000002", "observation-000000003"]}),
        lambda observations: observations.insert(4, observations[3]),
    ],
)
def test_required_proof_chain_rejects_missing_argv_missing_step_reference_and_duplicates(mutator) -> None:
    observations = _complete_observations()
    mutator(observations)
    allowed, reason = validate_pdb_patch_evidence(observations)
    assert allowed is False
    assert reason


def test_exact_probe_driver_runs_declared_pytest_node_without_direct_call() -> None:
    task = load_task(str(FIXTURE / "task.json"))
    scenario = scenario_for(TASK_ID)
    source = exact_pytest_driver_source(
        scenario.runtime_probe,
        tuple(task.reproduction.argv[3:]),
    )
    assert "pytest.main" in source
    assert scenario.runtime_probe.call_source not in source
    assert "PYTEST_ADDOPTS" in source
    compile(source, "window_tail.py", "exec")


def test_private_verifier_source_is_not_agent_visible() -> None:
    task = load_task(str(FIXTURE / "task.json"))
    visible = json.dumps(task.agent_visible_mapping(), sort_keys=True)
    assert "oracle" not in visible
    assert _PRIVATE_SOURCE not in visible
    assert "private_verifier" not in visible


def test_exact_probe_preserves_canonical_fixture_and_uses_a_disposable_identity(tmp_path: Path) -> None:
    before = (FIXTURE / "window_tail.py").read_bytes()
    task = load_task(str(FIXTURE / "task.json"))
    probe = prepare_pdb_probe(FIXTURE, scenario_for(TASK_ID), tmp_path, task=task)
    assert (FIXTURE / "window_tail.py").read_bytes() == before
    assert probe.exact_public_reproduction is True
    assert probe.reproduction_argv == tuple(task.reproduction.argv)
    assert probe.reproduction_node == task.tests.fail_to_pass[0]
    assert probe.workspace_id
    assert len(probe.production_file_sha256) == 64
    public_manifest = json.loads((probe.source_dir / "task.json").read_text(encoding="utf-8"))
    assert "oracle" not in public_manifest
    assert "private" not in json.dumps(public_manifest).lower()


def test_default_pdb_worker_keeps_site_packages_out_of_isolation(tmp_path: Path) -> None:
    workspace = TaskWorkspace(str(FIXTURE), parent_dir=str(tmp_path))
    try:
        normal = " ".join(PdbSession(workspace)._get_worker_argv())
        proof = " ".join(PdbSession(workspace, proof_pytest_dependencies=True)._get_worker_argv())
        assert "getsitepackages" not in normal
        assert "getusersitepackages" not in normal
        assert "getusersitepackages" in proof
        assert "getsitepackages" not in proof
    finally:
        workspace.cleanup()
