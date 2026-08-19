"""Deterministic repository-scale harness regressions.

These tests exercise only local synthetic repositories and the in-process
controller/tool contracts.  They deliberately do not launch a provider.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisConfidence,
    HypothesisLedger,
    PdbPolicy,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    ControllerSnapshot,
    ScriptedModelAdapter,
    ScriptedModelStep,
    TransitionDirective,
)
from agentic_debugger.demo.tools import DemoToolContext, build_registry
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import (
    LiveModelAdapter,
    LiveModelAdapterError,
    LiveModelConfig,
    LiveRunLimits,
    _action_contracts_for_state,
    _legal_transition_targets,
)
from agentic_debugger.events.schema import Action, ObservationStatus
from agentic_debugger.events.schema import Observation
from agentic_debugger.runtime.exceptions import PatchAuthorizationError, WorkspaceError
from agentic_debugger.runtime.patcher import PatchManager, _parse_unified_diff
from agentic_debugger.agent.tool_registry import ToolRegistry, ToolResult, ToolSpec
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.swerebench.mapping import build_model_task
from agentic_debugger.swerebench.schema import classify_execution_result

from test_swerebench_repair1_contracts import _bundle, _ordered


def _external_task():
    return build_model_task(
        _ordered(),
        _bundle(),
        fixture_path="sources/example",
        allowed_write_paths=["src", "setup.py"],
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text(
        "class Widget:\n    pass\n\ndef target(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_public.py").write_text(
        "def test_public():\n    assert True\n", encoding="utf-8"
    )
    (root / "setup.py").write_text("# synthetic public setup\n", encoding="utf-8")
    return root


def _context(tmp_path: Path, *, pdb_policy=None):
    source = _repo(tmp_path)
    workspace = TaskWorkspace(str(source), parent_dir=str(tmp_path))
    context = DemoToolContext(
        task=_external_task(), workspace=workspace, patch="", probe=None
    )
    return workspace, context, build_registry(context, pdb_policy=pdb_policy)


def _action(task_id: str, state: ControllerState, name: ActionName, arguments: dict):
    return Action(
        action_id="action-000000000",
        run_id="repair7",
        task_id=task_id,
        state=state,
        name=name.value,
        arguments=arguments,
    )


def test_external_registry_requires_model_selected_public_target(tmp_path):
    workspace, context, registry = _context(tmp_path)
    try:
        observation = registry.dispatch(
            _action(context.task.task_id, ControllerState.REPRODUCE, ActionName.RUN_REPRODUCTION, {"phase": "baseline"}),
            observation_id="observation-000000000",
        )
        assert observation.status is ObservationStatus.REJECTED
    finally:
        workspace.cleanup()


def test_external_reproduction_contract_marks_public_target_required(tmp_path):
    workspace, _context_value, registry = _context(tmp_path)
    try:
        contract = registry.argument_contracts()[ActionName.RUN_REPRODUCTION.value]
        assert contract["required"] == ["phase", "public_target"]
        assert contract["additional_properties"] is False
    finally:
        workspace.cleanup()


def test_external_default_registry_has_no_pdb_surface(tmp_path):
    workspace, _context_value, registry = _context(tmp_path)
    try:
        names = set(registry.names())
        assert not names & {
            ActionName.GET_FAILURE_TRACE,
            ActionName.START_PDB_SESSION,
            ActionName.GET_STACK_SUMMARY,
            ActionName.GET_FRAME_LOCALS,
            ActionName.SAFE_EVAL_EXPRESSION,
            ActionName.STOP_PDB_SESSION,
        }
    finally:
        workspace.cleanup()


def test_external_explicit_pdb_policy_adds_only_opt_in_pdb_surface(tmp_path):
    workspace, _context_value, registry = _context(
        tmp_path, pdb_policy=PdbPolicy.ON_UNCERTAINTY
    )
    try:
        names = set(registry.names())
        assert ActionName.START_PDB_SESSION in names
        assert ActionName.GET_STACK_SUMMARY in names
        assert ActionName.STOP_PDB_SESSION in names
    finally:
        workspace.cleanup()


def test_external_reproduce_contract_contains_bounded_discovery_tools(tmp_path):
    workspace, _context_value, registry = _context(tmp_path)
    try:
        contracts = _action_contracts_for_state(
            ControllerState.REPRODUCE,
            registry=registry,
            policy=DemoPolicy.STATIC_BASELINE,
            pdb_available=False,
            external_discovery=True,
        )
        assert {
            ActionName.SEARCH_CODE.value,
            ActionName.FIND_FUNCTION.value,
            ActionName.FIND_CLASS.value,
            ActionName.GET_SOURCE_WINDOW.value,
        } <= set(contracts)
    finally:
        workspace.cleanup()


def test_curated_reproduce_contract_does_not_gain_external_discovery(tmp_path):
    source = Path(__file__).resolve().parents[2] / "agentic_debugger" / "datasets" / "curated" / "curated-off-by-one-002"
    workspace = TaskWorkspace(str(source), parent_dir=str(tmp_path))
    from agentic_debugger.evaluation.task_schema import DebugTask
    import json

    task = DebugTask.from_mapping(json.loads((source / "task.json").read_text()))
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    try:
        registry = build_registry(context)
        contracts = _action_contracts_for_state(
            ControllerState.REPRODUCE,
            registry=registry,
            policy=DemoPolicy.STATIC_BASELINE,
            pdb_available=True,
            external_discovery=False,
        )
        assert not set(contracts) & {
            ActionName.SEARCH_CODE.value,
            ActionName.FIND_FUNCTION.value,
            ActionName.FIND_CLASS.value,
            ActionName.GET_SOURCE_WINDOW.value,
        }
    finally:
        workspace.cleanup()


def test_external_request_explains_public_discovery_and_hidden_verifier_boundary(tmp_path):
    workspace, context, registry = _context(tmp_path)
    try:
        adapter = LiveModelAdapter(
            task=context.task,
            policy=DemoPolicy.STATIC_BASELINE,
            config=LiveModelConfig("synthetic", ("synthetic",)),
            transport=object(),
            limits=LiveRunLimits(max_model_requests=1),
            registry=registry,
        )
        from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
        from agentic_debugger.agent.model_adapter import ControllerSnapshot

        limits = ControllerBudgetLimits.from_task_constraints(context.task.constraints)
        snapshot = ControllerSnapshot(
            "repair7", context.task.task_id, ControllerState.REPRODUCE, 0,
            limits, ControllerBudgetState(), HypothesisLedger()
        )
        request = adapter._request_context(snapshot, logical_request_index=0, transport_attempt_index=1)
        assert "No hidden reproduction target is supplied" in request["instructions"]
        assert "hidden verifier tests remain private" in request["instructions"]
    finally:
        workspace.cleanup()


def test_external_find_function_can_discover_without_path(tmp_path):
    workspace, context, registry = _context(tmp_path)
    try:
        observation = registry.dispatch(
            _action(context.task.task_id, ControllerState.REPRODUCE, ActionName.FIND_FUNCTION, {"name": "target"}),
            observation_id="observation-000000000",
        )
        assert observation.status is ObservationStatus.OK
        assert observation.payload["path"] == "src/pkg/mod.py"
    finally:
        workspace.cleanup()


def test_external_find_class_can_discover_without_path(tmp_path):
    workspace, context, registry = _context(tmp_path)
    try:
        observation = registry.dispatch(
            _action(context.task.task_id, ControllerState.REPRODUCE, ActionName.FIND_CLASS, {"name": "Widget"}),
            observation_id="observation-000000000",
        )
        assert observation.status is ObservationStatus.OK
        assert observation.payload["path"] == "src/pkg/mod.py"
    finally:
        workspace.cleanup()


def test_external_search_code_is_bounded_and_case_insensitive(tmp_path):
    workspace, context, registry = _context(tmp_path)
    try:
        observation = registry.dispatch(
            _action(context.task.task_id, ControllerState.REPRODUCE, ActionName.SEARCH_CODE, {"query": "def", "max_matches": 1, "case_sensitive": False}),
            observation_id="observation-000000000",
        )
        assert observation.status is ObservationStatus.OK
        assert len(observation.payload["matches"]) == 1
        assert observation.payload["truncated"] is True
    finally:
        workspace.cleanup()


def test_workspace_preserves_git_metadata_but_bounds_model_search(tmp_path):
    source = _repo(tmp_path)
    (source / ".git" / "objects" / "pack").mkdir(parents=True)
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    metadata = source / ".git" / "objects" / "pack" / "pack-synthetic"
    metadata.write_bytes(b"synthetic-pack")
    metadata.chmod(stat.S_IREAD)
    workspace = TaskWorkspace(str(source), parent_dir=str(tmp_path))
    try:
        copied_root = Path(workspace.root)
        assert (copied_root / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"
        assert (copied_root / ".git" / "objects" / "pack" / "pack-synthetic").read_bytes() == b"synthetic-pack"
        assert (Path(workspace.root) / "tests" / "test_public.py").exists()
        public_search = registry = build_registry(
            DemoToolContext(task=_external_task(), workspace=workspace, patch="", probe=None)
        ).dispatch(
            _action(_external_task().task_id, ControllerState.REPRODUCE, ActionName.SEARCH_CODE, {"query": "synthetic-pack"}),
            observation_id="observation-git-search",
        )
        assert public_search.status is ObservationStatus.OK
        assert public_search.payload["matches"] == []
        with pytest.raises(PatchAuthorizationError):
            PatchManager(workspace, allowed_paths=["src"], denied_paths=[]).apply_patch(
                "--- a/.git/HEAD\n+++ b/.git/HEAD\n@@ -1,1 +1,1 @@\n-ref: refs/heads/main\n+ref: refs/heads/repair\n"
            )
    finally:
        workspace.cleanup()
    assert not Path(workspace.root).exists()


def test_workspace_cleanup_clears_readonly_owned_files(tmp_path):
    source = _repo(tmp_path)
    workspace = TaskWorkspace(str(source), parent_dir=str(tmp_path))
    root = Path(workspace.root)
    target = root / "readonly.txt"
    target.write_text("owned", encoding="utf-8")
    target.chmod(stat.S_IREAD)
    workspace.cleanup()
    assert not root.exists()


def test_workspace_cleanup_failure_is_retryable(monkeypatch, tmp_path):
    source = _repo(tmp_path)
    workspace = TaskWorkspace(str(source), parent_dir=str(tmp_path))
    root = Path(workspace.root)
    import agentic_debugger.runtime.workspace as workspace_module

    original = workspace_module.shutil.rmtree
    calls = [0]

    def fail_once(path, *args, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("synthetic cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(workspace_module.shutil, "rmtree", fail_once)
    with pytest.raises(WorkspaceError):
        workspace.cleanup()
    assert root.exists()
    workspace.cleanup()
    assert not root.exists()


def test_patch_count_mismatch_normalizes_old_and_new_counts():
    parsed = _parse_unified_diff(
        "--- a/x.py\n+++ b/x.py\n@@ -9,8 +9,1 @@\n-old\n+new\n"
    )[0]
    assert parsed.hunks[0].old_count == 1
    assert parsed.hunks[0].new_count == 1
    assert parsed.hunk_count_adjustments == [(1, 8, 1, 1, 1)]


def test_patch_extra_body_lines_are_semantic_not_silently_dropped():
    parsed = _parse_unified_diff(
        "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n+c\n"
    )[0]
    assert [line.text for line in parsed.hunks[0].lines] == ["a", "b", "c"]
    assert parsed.hunk_count_adjustments == [(1, 1, 1, 1, 2)]


def test_patch_missing_hunk_position_is_still_rejected():
    with pytest.raises(Exception, match="hunk header"):
        _parse_unified_diff("--- a/x.py\n+++ b/x.py\n@@\n+x\n")


def test_patch_dash_dash_removal_line_preserves_semantic_text():
    parsed = _parse_unified_diff(
        "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n--- value\n+++ new\n"
    )[0]
    assert parsed.hunks[0].lines[0].text == "-- value"


def test_patch_manager_reports_count_adjustment_and_preserves_bytes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "x.py").write_bytes(b"old\n")
    workspace = TaskWorkspace(str(source), parent_dir=str(tmp_path))
    try:
        result = PatchManager(workspace, allowed_paths=["x.py"], denied_paths=[]).apply_patch(
            "--- a/x.py\n+++ b/x.py\n@@ -1,9 +1,1 @@\n-old\n+new\n"
        )
        assert result.hunk_count_adjustments == (("x.py", 1, 9, 1, 1, 1),)
        assert Path(workspace.root, "x.py").read_bytes() == b"new\n"
    finally:
        workspace.cleanup()


def test_patch_manager_allows_src_layout_and_denies_guessed_root_path(tmp_path):
    source = tmp_path / "source"
    (source / "src" / "pkg").mkdir(parents=True)
    (source / "src" / "pkg" / "mod.py").write_text("return_value = 1\n", encoding="utf-8")
    workspace = TaskWorkspace(str(source), parent_dir=str(tmp_path))
    try:
        manager = PatchManager(workspace, allowed_paths=["src"], denied_paths=[])
        patch = "--- a/src/pkg/mod.py\n+++ b/src/pkg/mod.py\n@@ -1,1 +1,1 @@\n-return_value = 1\n+return_value = 2\n"
        assert manager.apply_patch(patch).success
        manager.revert_patch()
        with pytest.raises(PatchAuthorizationError):
            manager.apply_patch(
                "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,1 +1,1 @@\n-return_value = 2\n+return_value = 3\n"
            )
    finally:
        workspace.cleanup()


def test_patch_contract_hides_syntax_and_revert_before_candidate(tmp_path):
    workspace, _context_value, registry = _context(tmp_path)
    try:
        contracts = _action_contracts_for_state(
            ControllerState.PATCH, registry=registry, candidate_applied=False
        )
        assert ActionName.APPLY_PATCH.value in contracts
        assert ActionName.SYNTAX_CHECK.value not in contracts
        assert ActionName.REVERT_PATCH.value not in contracts
    finally:
        workspace.cleanup()


def test_validate_contract_hides_candidate_dependent_actions_after_revert(tmp_path):
    workspace, _context_value, registry = _context(tmp_path)
    try:
        contracts = _action_contracts_for_state(
            ControllerState.VALIDATE,
            registry=registry,
            candidate_applied=False,
        )
        assert not set(contracts) & {
            ActionName.RUN_REPRODUCTION.value,
            ActionName.RUN_REGRESSION_TESTS.value,
            ActionName.CLASSIFY_OUTCOME.value,
            ActionName.REVERT_PATCH.value,
        }
    finally:
        workspace.cleanup()


def test_patch_transition_to_validate_requires_candidate():
    assert "Validate" not in _legal_transition_targets(
        ControllerState.PATCH, candidate_applied=False
    )
    assert "Validate" in _legal_transition_targets(
        ControllerState.PATCH, candidate_applied=True
    )


def _hypothesis_snapshot(task, state, index=0, *, candidate=False, last=None):
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    ledger = HypothesisLedger().add(
        limits,
        hypothesis_id="low-confidence",
        statement="runtime evidence may be required",
        confidence=HypothesisConfidence.LOW,
        requires_runtime_evidence=True,
    )
    return ControllerSnapshot(
        "repair8", task.task_id, state, index, limits, ControllerBudgetState(),
        ledger, last_observation=last, candidate_applied=candidate,
    )


def test_external_disabled_registry_cannot_advertise_runtime_evidence(tmp_path):
    workspace, context, registry = _context(tmp_path)
    try:
        adapter = LiveModelAdapter(
            task=context.task,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            config=LiveModelConfig("synthetic", ("synthetic",)),
            transport=object(),
            limits=LiveRunLimits(max_model_requests=1),
            registry=registry,
        )
        adapter._failure_reproduced = True
        request = adapter._request_context(
            _hypothesis_snapshot(context.task, ControllerState.UNDERSTAND),
            logical_request_index=0,
            transport_attempt_index=1,
        )
        assert "RuntimeEvidence" not in request["controller"]["legal_transition_targets"]
        assert ActionName.START_PDB_SESSION.value not in request["action_contracts"]
        assert adapter._runtime_transition_allowed(
            _hypothesis_snapshot(context.task, ControllerState.UNDERSTAND)
        ) is False
        assert adapter.pdb_gate_decisions == []

        class AttemptsRuntimeEvidence:
            def request(self, payload, timeout_seconds):
                return {
                    "directive": {
                        "kind": "transition",
                        "target_state": "RuntimeEvidence",
                        "reason": "synthetic denied attempt",
                    }
                }

        adapter.transport = AttemptsRuntimeEvidence()
        with pytest.raises(LiveModelAdapterError):
            adapter.next_directive(
                _hypothesis_snapshot(context.task, ControllerState.UNDERSTAND)
            )
        assert adapter.pdb_gate_decisions == []
    finally:
        workspace.cleanup()


def test_enabled_registry_can_expose_runtime_evidence_after_gate(tmp_path):
    workspace, context, registry = _context(tmp_path, pdb_policy=PdbPolicy.ON_UNCERTAINTY)
    try:
        adapter = LiveModelAdapter(
            task=context.task,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            config=LiveModelConfig("synthetic", ("synthetic",)),
            transport=object(),
            limits=LiveRunLimits(max_model_requests=1),
            registry=registry,
        )
        adapter._failure_reproduced = True
        snapshot = _hypothesis_snapshot(context.task, ControllerState.UNDERSTAND)
        request = adapter._request_context(
            snapshot, logical_request_index=0, transport_attempt_index=1
        )
        assert "RuntimeEvidence" in request["controller"]["legal_transition_targets"]
        assert adapter._runtime_transition_allowed(snapshot) is True
        adapter._runtime_transition_authorized = True
        runtime_request = adapter._request_context(
            _hypothesis_snapshot(context.task, ControllerState.RUNTIME_EVIDENCE),
            logical_request_index=1,
            transport_attempt_index=1,
        )
        assert ActionName.START_PDB_SESSION.value in runtime_request["action_contracts"]

        class OpensRuntimeEvidence:
            def request(self, payload, timeout_seconds):
                return {
                    "directive": {
                        "kind": "transition",
                        "target_state": "RuntimeEvidence",
                        "reason": "synthetic accepted gate",
                    }
                }

        adapter.transport = OpensRuntimeEvidence()
        accepted = adapter.next_directive(snapshot)
        assert accepted.target_state is ControllerState.RUNTIME_EVIDENCE
        assert adapter.pdb_gate_decisions[-1]["allowed"] is True
    finally:
        workspace.cleanup()


def _candidate_lifecycle_registry(counters):
    def validator(arguments):
        return arguments

    def apply(action, arguments):
        counters["apply"] += 1
        return ToolResult(ObservationStatus.OK, {"applied": True}, "candidate applied")

    def revert(action, arguments):
        counters["revert"] += 1
        return ToolResult(ObservationStatus.OK, {"reverted": True}, "candidate reverted")

    def dependent(action, arguments):
        counters["dependent"] += 1
        return ToolResult(ObservationStatus.OK, {}, "dependent action")

    return ToolRegistry(
        (
            ToolSpec(ActionName.APPLY_PATCH, validator, apply),
            ToolSpec(ActionName.REVERT_PATCH, validator, revert),
            ToolSpec(ActionName.RUN_REGRESSION_TESTS, validator, dependent),
        )
    )


def test_validate_revert_removes_done_and_candidate_dependent_actions():
    counters = {"apply": 0, "revert": 0, "dependent": 0}
    registry = _candidate_lifecycle_registry(counters)
    steps = [
        ScriptedModelStep(
            ControllerState.PATCH,
            ActionDirective(ActionName.APPLY_PATCH, {"patch": "candidate"}),
        ),
        ScriptedModelStep(
            ControllerState.PATCH,
            TransitionDirective(ControllerState.VALIDATE, "candidate accepted"),
        ),
        ScriptedModelStep(
            ControllerState.VALIDATE,
            ActionDirective(ActionName.REVERT_PATCH, {}),
        ),
        ScriptedModelStep(
            ControllerState.VALIDATE,
            TransitionDirective(ControllerState.DONE, "incorrectly complete after revert"),
        ),
    ]
    result = DeterministicController(
        registry,
        ScriptedModelAdapter(tuple(steps)),
        ControllerRunConfig(max_model_calls=8),
    ).run(
        ControllerSnapshot(
            "repair8", "task-1", ControllerState.PATCH, 0,
            ControllerBudgetLimits(2, 2, 0), ControllerBudgetState(), HypothesisLedger(),
        )
    )
    assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
    assert result.final_state is ControllerState.FAILED
    assert counters == {"apply": 1, "revert": 1, "dependent": 0}
    assert "Done" not in _legal_transition_targets(
        ControllerState.VALIDATE, candidate_applied=False
    )


def test_candidate_dependent_validate_action_is_rejected_without_candidate():
    counters = {"apply": 0, "revert": 0, "dependent": 0}
    result = DeterministicController(
        _candidate_lifecycle_registry(counters),
        ScriptedModelAdapter(
            (
                ScriptedModelStep(
                    ControllerState.VALIDATE,
                    ActionDirective(ActionName.RUN_REGRESSION_TESTS, {}),
                ),
            )
        ),
        ControllerRunConfig(max_model_calls=1),
    ).run(
        ControllerSnapshot(
            "repair8", "task-1", ControllerState.VALIDATE, 0,
            ControllerBudgetLimits(1, 2, 0), ControllerBudgetState(), HypothesisLedger(),
        )
    )
    assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
    assert counters["dependent"] == 0


def test_external_model_task_has_no_hidden_verifier_identity():
    visible = _external_task().agent_visible_mapping()
    text = repr(visible)
    assert "test_hidden.py" not in text
    assert "GOLD-ONLY" not in text


def test_external_model_task_exposes_public_repo_metadata_only():
    visible = _external_task().agent_visible_mapping()
    assert visible["source"]["kind"] == "external"
    assert "no-public-reproduction" in visible["reproduction"]["argv"][0]
    assert visible["tests"]["pass_to_pass"] == ["[hidden-from-model]"]


def test_external_pdb_contract_is_absent_when_registry_is_disabled(tmp_path):
    workspace, _context_value, registry = _context(tmp_path, pdb_policy=PdbPolicy.DISABLED)
    try:
        assert ActionName.START_PDB_SESSION not in set(registry.names())
        assert ActionName.GET_FAILURE_TRACE not in set(registry.names())
    finally:
        workspace.cleanup()


def test_external_model_adapter_does_not_infer_provider_identity(tmp_path):
    workspace, context, registry = _context(tmp_path)
    try:
        first = LiveModelAdapter(
            task=context.task, policy=None,
            config=LiveModelConfig("model-a", ("synthetic",)),
            transport=object(), limits=LiveRunLimits(max_model_requests=1), registry=registry,
        )
        second = LiveModelAdapter(
            task=context.task, policy=None,
            config=LiveModelConfig("model-b", ("synthetic",)),
            transport=object(), limits=LiveRunLimits(max_model_requests=1), registry=registry,
        )
        assert first._effective_contract.__name__ == second._effective_contract.__name__
        assert "model_name" not in first._effective_contract.__code__.co_varnames
    finally:
        workspace.cleanup()


def test_cleanup_failure_takes_precedence_over_provider_failure():
    assert classify_execution_result(
        controller_completed=False,
        candidate_produced=False,
        verifier_ran=False,
        verifier_resolved=False,
        verifier_infrastructure_valid=True,
        provider_invalid=True,
        runtime_infrastructure_invalid=True,
    ) == "infrastructure_invalid"
