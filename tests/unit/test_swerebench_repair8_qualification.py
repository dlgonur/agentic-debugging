"""Zero-provider Repair-8 qualification proofs."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerStopReason,
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
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.demo.tools import DemoToolContext, build_registry
from agentic_debugger.evaluation.task_schema import Reproduction, Tests
from agentic_debugger.evaluation.live import LiveModelAdapter, LiveModelConfig, LiveRunLimits
from agentic_debugger.runtime.patcher import PatchManager
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.swerebench.devqual import validate_devqual_identity

from test_swerebench_repair7_harness import _bundle, _ordered
from agentic_debugger.swerebench.mapping import build_model_task


def _public_task():
    task = build_model_task(
        _ordered(), _bundle(), fixture_path=".", allowed_write_paths=["src"]
    )
    return replace(
        task,
        reproduction=Reproduction(
            [sys.executable, "-m", "pytest", "tests/test_public.py", "-q", "-p", "no:cacheprovider"],
            ".",
            30,
            1,
        ),
        tests=Tests(
            ["tests/test_public.py::test_public"],
            [],
            [sys.executable, "-m", "pytest", "tests/test_public.py", "-q", "-p", "no:cacheprovider"],
            30,
        ),
    )


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "external-repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text(
        "def target(value):\n    return value + 1\n", encoding="utf-8"
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_public.py").write_text(
        "from src.pkg.mod import target\n\n\ndef test_public():\n    assert target(1) == 3\n",
        encoding="utf-8",
    )
    (root / ".git" / "objects" / "pack").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".git" / "objects" / "pack" / "pack-synthetic").write_bytes(b"pack")
    return root


def _no_pdb_external_registry(context: DemoToolContext) -> ToolRegistry:
    enabled = build_registry(context, pdb_policy=PdbPolicy.ON_UNCERTAINTY)
    debugger_only = {
        ActionName.START_PDB_SESSION,
        ActionName.GET_STACK_SUMMARY,
        ActionName.GET_FRAME,
        ActionName.GET_FRAME_LOCALS,
        ActionName.SAFE_EVAL_EXPRESSION,
        ActionName.INSPECT_CALLER_FRAME,
        ActionName.CONTINUE_PDB_SESSION,
        ActionName.STEP_PDB_SESSION,
        ActionName.NEXT_PDB_SESSION,
        ActionName.STOP_PDB_SESSION,
    }
    return ToolRegistry(tuple(spec for spec in enabled.specs if spec.name not in debugger_only))


class _QualificationTransport:
    def __init__(self, patch: str, *, revert_branch: bool = False):
        self.requests: list[dict] = []
        self.synthetic_transport_calls = 0
        self.provider_generation_calls = 0
        self.index = 0
        self.directives = [
            {"kind": "action", "name": "search_code", "arguments": {"query": "def target", "max_matches": 10}},
            {"kind": "action", "name": "find_function", "arguments": {"name": "target"}},
            {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline", "public_target": "tests/test_public.py::test_public"}},
            {"kind": "transition", "target_state": "Understand", "reason": "public failure reproduced"},
            {"kind": "action", "name": "get_source_window", "arguments": {"path": "src/pkg/mod.py", "line": 1}},
            {"kind": "add_hypothesis", "hypothesis_id": "h-1", "statement": "target increments one too few", "confidence": "high", "evidence_refs": [], "requires_runtime_evidence": False},
            {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "h-1", "statement": "target increments one too few", "target_file": "src/pkg/mod.py", "target_symbol": "target", "confidence": "high"}},
            {"kind": "transition", "target_state": "Patch", "reason": "static source explains the failure"},
            {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch}},
            {"kind": "transition", "target_state": "Validate", "reason": "candidate accepted"},
            {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "post_patch", "public_target": "tests/test_public.py::test_public"}},
            {"kind": "action", "name": "run_regression_tests", "arguments": {}},
            {"kind": "action", "name": "classify_outcome", "arguments": {}},
            {"kind": "transition", "target_state": "Done", "reason": "validation complete"},
        ]
        if revert_branch:
            self.directives = [
                {"kind": "transition", "target_state": "Understand", "reason": "synthetic setup"},
                {"kind": "transition", "target_state": "Patch", "reason": "synthetic setup"},
                {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch}},
                {"kind": "transition", "target_state": "Validate", "reason": "candidate accepted"},
                {"kind": "action", "name": "revert_patch", "arguments": {}},
                {"kind": "transition", "target_state": "Patch", "reason": "candidate was reverted"},
                {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch}},
                {"kind": "transition", "target_state": "Validate", "reason": "replacement candidate accepted"},
                {"kind": "transition", "target_state": "Done", "reason": "replacement candidate exists"},
            ]

    def request(self, payload, timeout_seconds):
        self.synthetic_transport_calls += 1
        self.requests.append(payload)
        directive = self.directives[self.index]
        self.index += 1
        return {"directive": directive}


def _run_external_controller(tmp_path: Path, transport: _QualificationTransport):
    source = _source(tmp_path)
    task = _public_task()
    workspace = TaskWorkspace(str(source), parent_dir=str(tmp_path))
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    registry = _no_pdb_external_registry(context)
    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=LiveModelConfig("synthetic-zero-provider", ("never-launched",)),
        transport=transport,
        limits=LiveRunLimits(max_model_requests=32, max_controller_steps=32, max_retries=0),
        registry=registry,
    )
    result = DeterministicController(
        registry, adapter, ControllerRunConfig(max_model_calls=32)
    ).run(
        ControllerSnapshot(
            "qualification", task.task_id, ControllerState.REPRODUCE, 0,
            ControllerBudgetLimits.from_task_constraints(task.constraints),
            ControllerBudgetState(), HypothesisLedger(),
        )
    )
    return source, task, workspace, context, result, adapter


def test_external_zero_provider_qualification_reaches_verifier_and_cleanup(tmp_path):
    patch = "--- a/src/pkg/mod.py\n+++ b/src/pkg/mod.py\n@@ -1,2 +1,2 @@\n def target(value):\n-    return value + 1\n+    return value + 2\n"
    transport = _QualificationTransport(patch)
    source, task, workspace, context, result, adapter = _run_external_controller(tmp_path, transport)
    try:
        assert result.stop_reason is ControllerStopReason.DONE
        assert result.final_state is ControllerState.DONE
        assert transport.synthetic_transport_calls > 0
        assert transport.provider_generation_calls == 0
        assert all(
            "RuntimeEvidence" not in request["controller"]["legal_transition_targets"]
            for request in transport.requests
        )
        assert any(request["controller"]["state"] == "Reproduce" for request in transport.requests)
        assert any("search_code" in request["action_contracts"] for request in transport.requests)

        verifier_workspace = TaskWorkspace(str(source), parent_dir=str(tmp_path))
        verifier = PatchManager(verifier_workspace, allowed_paths=["src"], denied_paths=[])
        verifier.apply_patch(patch)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_public.py", "-q", "-p", "no:cacheprovider"],
            cwd=verifier_workspace.root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0
        verifier_workspace.cleanup()
        assert not Path(verifier_workspace.root).exists()
    finally:
        context.release_pdb()
        workspace.cleanup()
    assert not Path(workspace.root).exists()


def test_external_qualification_revert_requires_replacement_candidate(tmp_path):
    patch = "--- a/src/pkg/mod.py\n+++ b/src/pkg/mod.py\n@@ -1,2 +1,2 @@\n def target(value):\n-    return value + 1\n+    return value + 2\n"
    transport = _QualificationTransport(patch, revert_branch=True)
    _source_path, _task, workspace, context, result, _adapter = _run_external_controller(tmp_path, transport)
    try:
        assert result.stop_reason is ControllerStopReason.DONE
        assert result.final_state is ControllerState.DONE
        assert sum(
            step.action is not None and step.action.name == ActionName.APPLY_PATCH.value
            for step in result.steps
        ) == 2
    finally:
        context.release_pdb()
        workspace.cleanup()


def test_devqual_identity_is_bound_to_immutable_first_ten():
    identity = validate_devqual_identity()
    assert identity["source_full_ordering_sha256"] == "599a07b6a527b4f8dffda4120be8e3c524ad608929bb048ea98286f80e0f5061"
    assert len(identity["first_ten_instance_ids"]) == 10
