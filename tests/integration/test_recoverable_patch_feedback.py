from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerRunResult,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisConfidence,
    HypothesisLedger,
    HypothesisStatus,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    AddHypothesisDirective,
    ControllerSnapshot,
    ModelAdapter,
    ModelDirective,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import (
    MAX_TOOL_RESULT_PAYLOAD_BYTES,
    ToolDispatchReason,
)
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.demo.tools import (
    DemoToolContext,
    build_registry,
)
from agentic_debugger.evaluation import load_task
from agentic_debugger.evaluation.live import (
    LiveModelAdapter,
    LiveModelConfig,
    LiveRunLimits,
)
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
from agentic_debugger.evaluation.runner import EvaluationStatus
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.events.schema import Action, Observation, ObservationStatus
from agentic_debugger.runtime.exceptions import (
    PatchApplyError,
    PatchAuthorizationError,
    PatchRevertError,
    PatchStateError,
    PatchValidationError,
)
from agentic_debugger.runtime.workspace import TaskWorkspace
from scripts.ollama_cloud_command_adapter import build_apply_patch_guidance

REPO_ROOT = Path(__file__).resolve().parents[2]
CURATED = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"


class ReactiveModelAdapter(ModelAdapter):
    """Adaptive model double for testing intra-session feedback loops."""

    def __init__(self, step_fn: Callable[[ControllerSnapshot], ModelDirective]) -> None:
        self._step_fn = step_fn
        self.recorded_snapshots: list[ControllerSnapshot] = []

    def next_directive(self, snapshot: ControllerSnapshot) -> ModelDirective:
        self.recorded_snapshots.append(snapshot)
        return self._step_fn(snapshot)


class _DummyTransport:
    def request(self, payload: Any, timeout_seconds: float) -> dict[str, Any]:
        return {"directive": {"kind": "transition", "target_state": "Failed", "reason": "dummy"}}


def test_level18_recoverable_patch_context_mismatch_feedback_loop(tmp_path: Path) -> None:
    """Test Level-18 scenario: context mismatch -> feedback with source -> corrected patch -> RESOLVED."""
    task_id = "pdb-required-multistage-units-008"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    # Read original source
    target_rel = "deadline_pipeline.py"
    target_file = Path(workspace.root) / target_rel
    original_source = target_file.read_text(encoding="utf-8")

    # Failing patch: Level-18 attempted semantic change with assumed/hallucinated context
    failing_patch = (
        "--- a/deadline_pipeline.py\n"
        "+++ b/deadline_pipeline.py\n"
        "@@ -10,3 +10,3 @@\n"
        "     if not retries:\n"
        "-    expanded_delay = _expand_retry_window(value, retries, retry_count)\n"
        "+    expanded_delay = _expand_retry_window(base_delay_ms, retries, retry_count)\n"
        "     return expanded_delay\n"
    )

    # Corrected patch matching the actual source file
    corrected_source = original_source.replace(
        "    retry_window_ms = _expand_retry_window(value, retry_count)",
        "    retry_window_ms = _expand_retry_window(base_delay_ms, retry_count)",
    )
    corrected_patch = "".join(
        difflib.unified_diff(
            original_source.splitlines(keepends=True),
            corrected_source.splitlines(keepends=True),
            fromfile=f"a/{target_rel}",
            tofile=f"b/{target_rel}",
            lineterm="\n",
        )
    )

    context = DemoToolContext(
        task=task,
        workspace=workspace,
        patch="",
        probe=None,
    )
    registry = build_registry(context)

    patch_attempts_made = 0
    feedback_received: list[str] = []

    def model_policy(snapshot: ControllerSnapshot) -> ModelDirective:
        nonlocal patch_attempts_made

        if snapshot.state is ControllerState.REPRODUCE:
            if snapshot.last_observation is None:
                return ActionDirective(
                    ActionName.RUN_REPRODUCTION,
                    {"phase": "baseline"},
                )
            return TransitionDirective(
                ControllerState.UNDERSTAND,
                reason="baseline reproduced",
            )

        if snapshot.state is ControllerState.UNDERSTAND:
            hypotheses = snapshot.hypotheses.active_hypotheses()
            if not hypotheses:
                return AddHypothesisDirective(
                    hypothesis_id="hyp-1",
                    statement="_expand_retry_window is receiving raw value instead of converted base_delay_ms",
                    confidence=HypothesisConfidence.HIGH,
                    evidence_refs=(),
                    requires_runtime_evidence=False,
                )
            return TransitionDirective(
                ControllerState.PATCH,
                reason="diagnosis expressed",
            )

        if snapshot.state is ControllerState.PATCH:
            if patch_attempts_made == 0:
                # Attempt 1: submit the failing patch
                patch_attempts_made += 1
                return ActionDirective(
                    ActionName.APPLY_PATCH,
                    {"patch": failing_patch},
                )
            elif patch_attempts_made == 1:
                # Inspect feedback received from Attempt 1
                obs = snapshot.last_observation
                assert obs is not None
                assert obs.name == ActionName.APPLY_PATCH.value
                assert obs.status is ObservationStatus.ERROR
                feedback_received.append(obs.summary)

                # Diagnostic & structured payload verification
                assert "Context mismatch" in obs.summary
                assert "deadline_pipeline.py" in obs.summary

                pf = obs.payload.get("patch_failure", {})
                assert pf.get("kind") == "context_mismatch"
                assert pf.get("recoverable") is True
                assert pf.get("path") == "deadline_pipeline.py"
                assert pf.get("line_number") == 10
                assert pf.get("current_source_window") is not None
                assert "retry_window_ms" in pf.get("current_source_window")

                # Attempt 2: submit the corrected patch based on source context
                patch_attempts_made += 1
                return ActionDirective(
                    ActionName.APPLY_PATCH,
                    {"patch": corrected_patch},
                )
            else:
                # Patch applied successfully in Attempt 2; advance to VALIDATE
                obs = snapshot.last_observation
                assert obs is not None
                assert obs.name == ActionName.APPLY_PATCH.value
                assert obs.status is ObservationStatus.OK
                return TransitionDirective(
                    ControllerState.VALIDATE,
                    reason="patch applied successfully",
                )

        if snapshot.state is ControllerState.VALIDATE:
            if snapshot.last_observation is None or snapshot.last_observation.name == ActionName.APPLY_PATCH.value:
                return ActionDirective(
                    ActionName.RUN_REPRODUCTION,
                    {"phase": "post_patch"},
                )
            return TransitionDirective(
                ControllerState.DONE,
                reason="validation passed",
            )

        raise AssertionError(f"unexpected state: {snapshot.state}")

    adapter = ReactiveModelAdapter(model_policy)
    controller = DeterministicController(
        registry,
        adapter,
        ControllerRunConfig(max_model_calls=20),
    )

    initial_snapshot = ControllerSnapshot(
        run_id="test-run-level18",
        task_id=task_id,
        state=ControllerState.REPRODUCE,
        model_call_index=0,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=3,
            max_test_runs=5,
            max_pdb_observations=10,
            max_active_hypotheses=3,
            max_source_observations=10,
        ),
        budget_state=ControllerBudgetState(0, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=None,
    )

    result = controller.run(initial_snapshot)

    # 1. Controller finished DONE without terminating prematurely
    assert result.final_state is ControllerState.DONE, f"failed with stop_reason={result.stop_reason}, steps={[(s.state_before, s.state_after, s.stop_reason) for s in result.steps]}"
    assert result.stop_reason is ControllerStopReason.DONE
    assert result.model_calls > 5

    # 2. Bounded budget was tracked honestly (2 patch attempts consumed)
    assert result.budget_state.patch_attempts == 2

    # 3. Observability permanently recorded Attempt 1 failure and Attempt 2 success
    apply_failed_steps = [
        s for s in result.steps
        if s.action and s.action.name == ActionName.APPLY_PATCH.value
        and s.observation and s.observation.status is ObservationStatus.ERROR
    ]
    assert len(apply_failed_steps) == 1

    # 4. Context patch_applied is True and candidate_patch is the corrected patch
    assert context.patch_applied is True
    assert context.candidate_patch == corrected_patch

    # 5. Independent verifier verifies candidate patch on clean baseline -> RESOLVED
    verifier_parent = tmp_path / "verifier"
    verifier_parent.mkdir()
    verifier = EvaluationVerifier(str(REPO_ROOT), workspace_parent=str(verifier_parent))
    eval_result = verifier.evaluate(task, context.candidate_patch)
    assert eval_result.status is EvaluationStatus.COMPLETED
    assert eval_result.outcome is SemanticOutcome.RESOLVED
    assert eval_result.f2p_passed == eval_result.f2p_total
    assert eval_result.p2p_passed == eval_result.p2p_total
    assert eval_result.workspace.canonical_fixture_unchanged is True


def test_model_request_receives_structured_feedback_both_error_and_rejected(tmp_path: Path) -> None:
    """Proves the real production request boundary serializes structured failure and source for ERROR and REJECTED."""
    task_id = "pdb-required-multistage-units-008"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    registry = build_registry(context)

    config = LiveModelConfig(
        model_name="mock-model",
        command=("mock", "cmd"),
        request_timeout_seconds=10.0,
    )
    live_adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=config,
        transport=_DummyTransport(),
        limits=LiveRunLimits(max_model_requests=1),
        registry=registry,
        evaluation_id="eval-1",
        case_id="case-1",
        run_id="run-1",
        trajectory_id="traj-1",
    )

    # 1. Test ERROR status (e.g. context mismatch apply error)
    error_obs = Observation(
        observation_id="obs-error",
        action_id="action-1",
        run_id="run-1",
        task_id=task_id,
        name=ActionName.APPLY_PATCH.value,
        status=ObservationStatus.ERROR,
        summary="Patch application failed. Context mismatch at deadline_pipeline.py around line 10.",
        payload={
            "dispatch_reason": ToolDispatchReason.TOOL_ERROR.value,
            "applied": False,
            "error": "context_mismatch",
            "recoverable": True,
            "diagnostic": "Patch application failed. Context mismatch at deadline_pipeline.py around line 10.",
            "patch_failure": {
                "kind": "context_mismatch",
                "recoverable": True,
                "path": "deadline_pipeline.py",
                "line_number": 10,
                "hunk_index": 1,
                "expected": "    expanded_delay = _expand_retry_window(value, retries, retry_count)",
                "current_source_window": " 10 |     retry_window_ms = _expand_retry_window(value, retry_count)",
            },
        },
        truncated=False,
    )

    error_snapshot = ControllerSnapshot(
        run_id="run-1",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=1,
        budget_limits=ControllerBudgetLimits(max_patch_attempts=3, max_test_runs=5, max_pdb_observations=10, max_active_hypotheses=3, max_source_observations=10),
        budget_state=ControllerBudgetState(1, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=error_obs,
    )

    request_error = live_adapter._request_context(
        error_snapshot,
        logical_request_index=1,
        transport_attempt_index=1,
    )

    # Assert structured failure is intact in shared production request mapping
    last_obs_error = request_error["controller"]["last_observation"]
    assert last_obs_error["status"] == "error"
    pf_error = last_obs_error["payload"]["patch_failure"]
    assert pf_error["kind"] == "context_mismatch"
    assert pf_error["recoverable"] is True
    assert pf_error["path"] == "deadline_pipeline.py"
    assert pf_error["line_number"] == 10
    assert "retry_window_ms" in pf_error["current_source_window"]

    # Assert guidance formats the structured failure into actionable model prompt text
    guidance_error = build_apply_patch_guidance(request_error)
    assert "PREVIOUS APPLY_PATCH ATTEMPT FAILED:" in guidance_error
    assert "Target file: deadline_pipeline.py" in guidance_error
    assert "Target line: 10" in guidance_error
    assert "retry_window_ms" in guidance_error

    # 2. Test REJECTED status (e.g. malformed diff validation error)
    rejected_obs = Observation(
        observation_id="obs-rejected",
        action_id="action-1",
        run_id="run-1",
        task_id=task_id,
        name=ActionName.APPLY_PATCH.value,
        status=ObservationStatus.REJECTED,
        summary="Malformed hunk header at 'deadline_pipeline.py'",
        payload={
            "dispatch_reason": ToolDispatchReason.TOOL_REJECTED.value,
            "applied": False,
            "error": "validation_error",
            "recoverable": True,
            "diagnostic": "Malformed hunk header at 'deadline_pipeline.py': old_count=3 but body has 2 lines",
            "patch_failure": {
                "kind": "validation_error",
                "recoverable": True,
                "path": "deadline_pipeline.py",
            },
        },
        truncated=False,
    )

    rejected_snapshot = ControllerSnapshot(
        run_id="run-1",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=1,
        budget_limits=ControllerBudgetLimits(max_patch_attempts=3, max_test_runs=5, max_pdb_observations=10, max_active_hypotheses=3, max_source_observations=10),
        budget_state=ControllerBudgetState(1, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=rejected_obs,
    )

    request_rejected = live_adapter._request_context(
        rejected_snapshot,
        logical_request_index=1,
        transport_attempt_index=1,
    )

    last_obs_rejected = request_rejected["controller"]["last_observation"]
    assert last_obs_rejected["status"] == "rejected"
    pf_rejected = last_obs_rejected["payload"]["patch_failure"]
    assert pf_rejected["kind"] == "validation_error"
    assert pf_rejected["recoverable"] is True
    assert pf_rejected["path"] == "deadline_pipeline.py"

    guidance_rejected = build_apply_patch_guidance(request_rejected)
    assert "PREVIOUS APPLY_PATCH ATTEMPT FAILED:" in guidance_rejected
    assert "Target file: deadline_pipeline.py" in guidance_rejected
    assert "Malformed hunk header" in guidance_rejected


def test_repeated_recoverable_failures_are_budget_bounded(tmp_path: Path) -> None:
    """Proves repeated recoverable failures exhaust budget honestly with no infinite loop."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    failing_patch = (
        "--- a/display_name.py\n"
        "+++ b/display_name.py\n"
        "@@ -100,2 +100,2 @@\n"
        "-nonexistent_line_1\n"
        "+fixed_line_1\n"
        " nonexistent_line_2\n"
    )

    context = DemoToolContext(
        task=task,
        workspace=workspace,
        patch="",
        probe=None,
    )
    registry = build_registry(context)

    def model_policy(snapshot: ControllerSnapshot) -> ModelDirective:
        if snapshot.state is ControllerState.PATCH:
            return ActionDirective(
                ActionName.APPLY_PATCH,
                {"patch": failing_patch},
            )
        return TransitionDirective(ControllerState.PATCH, reason="patch")

    adapter = ReactiveModelAdapter(model_policy)
    controller = DeterministicController(
        registry,
        adapter,
        ControllerRunConfig(max_model_calls=10),
    )

    max_patches = 3
    initial_snapshot = ControllerSnapshot(
        run_id="test-budget-exhaustion",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=0,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=max_patches,
            max_test_runs=5,
            max_pdb_observations=10,
            max_active_hypotheses=3,
            max_source_observations=10,
        ),
        budget_state=ControllerBudgetState(0, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=None,
    )

    result = controller.run(initial_snapshot)

    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.BUDGET_EXHAUSTED
    assert result.budget_state.patch_attempts == max_patches
    assert context.patch_applied is False


def test_malformed_diff_validation_recovery(tmp_path: Path) -> None:
    """Proves recoverable validation failure (malformed diff) can be corrected in same session."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    target_rel = "display_name.py"
    target_file = Path(workspace.root) / target_rel
    original_source = target_file.read_text(encoding="utf-8")

    malformed_patch = "this is not a unified diff"

    corrected_source = original_source.replace(
        "normalized_name = name.strip()",
        "normalized_name = name.strip() if name is not None else \"\"",
    )
    valid_patch = "".join(
        difflib.unified_diff(
            original_source.splitlines(keepends=True),
            corrected_source.splitlines(keepends=True),
            fromfile=f"a/{target_rel}",
            tofile=f"b/{target_rel}",
            lineterm="\n",
        )
    )

    context = DemoToolContext(
        task=task,
        workspace=workspace,
        patch="",
        probe=None,
    )
    registry = build_registry(context)

    attempts = 0

    def model_policy(snapshot: ControllerSnapshot) -> ModelDirective:
        nonlocal attempts
        if snapshot.state is ControllerState.PATCH:
            if attempts == 0:
                attempts += 1
                return ActionDirective(
                    ActionName.APPLY_PATCH,
                    {"patch": malformed_patch},
                )
            elif attempts == 1:
                obs = snapshot.last_observation
                assert obs is not None
                assert obs.status is ObservationStatus.REJECTED
                attempts += 1
                return ActionDirective(
                    ActionName.APPLY_PATCH,
                    {"patch": valid_patch},
                )
            else:
                obs = snapshot.last_observation
                assert obs is not None
                assert obs.status is ObservationStatus.OK
                return TransitionDirective(
                    ControllerState.VALIDATE,
                    reason="patch applied",
                )
        if snapshot.state is ControllerState.VALIDATE:
            return TransitionDirective(
                ControllerState.DONE,
                reason="done",
            )
        raise AssertionError(f"unexpected state: {snapshot.state}")

    adapter = ReactiveModelAdapter(model_policy)
    controller = DeterministicController(
        registry,
        adapter,
        ControllerRunConfig(max_model_calls=10),
    )

    initial_snapshot = ControllerSnapshot(
        run_id="test-malformed-recovery",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=0,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=3,
            max_test_runs=5,
            max_pdb_observations=10,
            max_active_hypotheses=3,
            max_source_observations=10,
        ),
        budget_state=ControllerBudgetState(0, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=None,
    )

    result = controller.run(initial_snapshot)
    assert result.final_state is ControllerState.DONE
    assert result.stop_reason is ControllerStopReason.DONE
    assert context.patch_applied is True
    assert context.candidate_patch == valid_patch


def test_large_patch_mismatch_payload_remains_strictly_bounded(tmp_path: Path) -> None:
    """Proves very large patch mismatch cannot create an unbounded Observation.payload."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    registry = build_registry(context)

    # Huge expected hunk lines (fits in argument bound, but exceeds result payload budget if unclipped)
    lines_count = 50
    huge_expected = "\n".join(f" context_line_{i}_{'x' * 60}" for i in range(lines_count))
    huge_failing_patch = (
        "--- a/display_name.py\n"
        "+++ b/display_name.py\n"
        f"@@ -1,{lines_count + 1} +1,{lines_count + 1} @@\n"
        f"{huge_expected}\n"
        "-nonexistent\n"
        "+replacement\n"
    )

    action = Action(
        action_id="action-000000000",
        run_id="test-large-payload",
        task_id=task_id,
        state=ControllerState.PATCH,
        name=ActionName.APPLY_PATCH.value,
        arguments={"patch": huge_failing_patch},
    )

    observation = registry.dispatch(action, observation_id="observation-000000000")

    assert observation.status in (ObservationStatus.ERROR, ObservationStatus.REJECTED)
    serialized_payload = json.dumps(observation.payload, ensure_ascii=False)
    assert len(serialized_payload.encode("utf-8")) <= MAX_TOOL_RESULT_PAYLOAD_BYTES
    pf = observation.payload.get("patch_failure", {})
    assert pf.get("recoverable") is True
    assert pf.get("kind") is not None


def test_abnormally_long_source_line_is_bounded_with_useful_context(tmp_path: Path) -> None:
    """Proves abnormally long source line in target is bounded while retaining useful correction context."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    # Inject an abnormally long line into the workspace file
    target_file = Path(workspace.root) / "display_name.py"
    original = target_file.read_text(encoding="utf-8")
    giant_line = "# " + "A" * 10_000
    target_file.write_text(giant_line + "\n" + original, encoding="utf-8")

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    registry = build_registry(context)

    # Trigger context mismatch near line 1
    mismatch_patch = (
        "--- a/display_name.py\n"
        "+++ b/display_name.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-# expected context mismatch\n"
        "+# replacement\n"
        " def format_name(name):\n"
    )

    action = Action(
        action_id="action-000000000",
        run_id="test-long-line",
        task_id=task_id,
        state=ControllerState.PATCH,
        name=ActionName.APPLY_PATCH.value,
        arguments={"patch": mismatch_patch},
    )

    obs = registry.dispatch(action, observation_id="observation-000000000")
    assert obs.status is ObservationStatus.ERROR
    pf = obs.payload.get("patch_failure", {})
    assert pf.get("kind") == "context_mismatch"
    assert pf.get("recoverable") is True
    assert pf.get("path") == "display_name.py"
    source_window = pf.get("current_source_window")
    assert source_window is not None
    # Verify individual line is bounded with truncation marker
    assert "..." in source_window
    assert len(source_window) <= 4096
    serialized = json.dumps(obs.payload, ensure_ascii=False)
    assert len(serialized.encode("utf-8")) <= MAX_TOOL_RESULT_PAYLOAD_BYTES


def test_non_recoverable_failure_fails_closed(tmp_path: Path) -> None:
    """Proves unrecoverable internal failures fail closed immediately with ZERO further model calls."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)

    # Mock patch_manager to raise an unrecoverable PatchApplyError (e.g. disk write failure)
    def failing_apply_patch(diff: str) -> None:
        raise PatchApplyError("Disk write failed: read-only filesystem", recoverable=False)

    context.patch_manager.apply_patch = failing_apply_patch  # type: ignore[assignment]
    registry = build_registry(context)

    patch_text = (
        "--- a/display_name.py\n"
        "+++ b/display_name.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-# a\n"
        "+# b\n"
    )

    model_calls = 0

    def model_policy(snapshot: ControllerSnapshot) -> ModelDirective:
        nonlocal model_calls
        model_calls += 1
        if snapshot.state is ControllerState.PATCH:
            return ActionDirective(ActionName.APPLY_PATCH, {"patch": patch_text})
        return TransitionDirective(ControllerState.PATCH, reason="patch")

    adapter = ReactiveModelAdapter(model_policy)
    controller = DeterministicController(registry, adapter, ControllerRunConfig(max_model_calls=10))

    initial_snapshot = ControllerSnapshot(
        run_id="test-unrecoverable",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=0,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=3,
            max_test_runs=5,
            max_pdb_observations=10,
            max_active_hypotheses=3,
            max_source_observations=10,
        ),
        budget_state=ControllerBudgetState(0, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=None,
    )

    result = controller.run(initial_snapshot)

    # 1. Controller terminates immediately with FAILED
    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.FAILED

    # 2. Exactly 1 model call made (ZERO further model calls)
    assert model_calls == 1
    assert result.model_calls == 1

    # 3. Step observation truthfully records unrecoverable failure
    assert len(result.steps) == 1
    first_step = result.steps[0]
    assert first_step.observation is not None
    assert first_step.observation.status is ObservationStatus.ERROR
    assert first_step.observation.payload.get("recoverable") is False
    assert first_step.stop_reason is ControllerStopReason.FAILED

    # 4. Budget was tracked honestly (1 attempt consumed, not reset or minted)
    assert result.budget_state.patch_attempts == 1

    # 5. Candidate is NOT marked applied
    assert context.patch_applied is False
    assert context.candidate_patch == ""


def test_path_traversal_fails_closed_immediately(tmp_path: Path) -> None:
    """Proves path traversal in diff is rejected as fatal authorization violation with zero further model calls."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    registry = build_registry(context)

    # Traversal patch attempting to escape workspace
    traversal_patch = (
        "--- a/../outside.py\n"
        "+++ b/../outside.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-# secret\n"
        "+# pwned\n"
    )

    model_calls = 0

    def model_policy(snapshot: ControllerSnapshot) -> ModelDirective:
        nonlocal model_calls
        model_calls += 1
        if snapshot.state is ControllerState.PATCH:
            return ActionDirective(ActionName.APPLY_PATCH, {"patch": traversal_patch})
        return TransitionDirective(ControllerState.PATCH, reason="patch")

    adapter = ReactiveModelAdapter(model_policy)
    controller = DeterministicController(registry, adapter, ControllerRunConfig(max_model_calls=10))

    initial_snapshot = ControllerSnapshot(
        run_id="test-traversal-fail-closed",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=0,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=3,
            max_test_runs=5,
            max_pdb_observations=10,
            max_active_hypotheses=3,
            max_source_observations=10,
        ),
        budget_state=ControllerBudgetState(0, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=None,
    )

    result = controller.run(initial_snapshot)

    # 1. Controller terminates immediately with FAILED
    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.FAILED

    # 2. Exactly 1 model call made (ZERO further retry calls)
    assert model_calls == 1
    assert result.model_calls == 1

    # 3. Step observation truthfully records rejection with recoverable=False
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.observation is not None
    assert step.observation.status is ObservationStatus.REJECTED
    assert step.observation.payload.get("recoverable") is False
    pf = step.observation.payload.get("patch_failure", {})
    assert pf.get("kind") == "authorization_error"
    assert pf.get("recoverable") is False
    assert step.stop_reason is ControllerStopReason.FAILED

    # 4. Budget was tracked honestly (1 attempt consumed)
    assert result.budget_state.patch_attempts == 1

    # 5. Candidate is NOT marked applied
    assert context.patch_applied is False


def test_absolute_path_fails_closed_immediately(tmp_path: Path) -> None:
    """Proves absolute path in diff is rejected as fatal authorization violation with zero further model calls."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    registry = build_registry(context)

    # Absolute path patch
    abs_patch = (
        "--- a//etc/passwd\n"
        "+++ b//etc/passwd\n"
        "@@ -1,1 +1,1 @@\n"
        "-root:x:0:0\n"
        "+root:x:0:1\n"
    )

    model_calls = 0

    def model_policy(snapshot: ControllerSnapshot) -> ModelDirective:
        nonlocal model_calls
        model_calls += 1
        if snapshot.state is ControllerState.PATCH:
            return ActionDirective(ActionName.APPLY_PATCH, {"patch": abs_patch})
        return TransitionDirective(ControllerState.PATCH, reason="patch")

    adapter = ReactiveModelAdapter(model_policy)
    controller = DeterministicController(registry, adapter, ControllerRunConfig(max_model_calls=10))

    initial_snapshot = ControllerSnapshot(
        run_id="test-abs-fail-closed",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=0,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=3,
            max_test_runs=5,
            max_pdb_observations=10,
            max_active_hypotheses=3,
            max_source_observations=10,
        ),
        budget_state=ControllerBudgetState(0, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=None,
    )

    result = controller.run(initial_snapshot)

    # 1. Controller terminates immediately with FAILED
    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.FAILED

    # 2. Exactly 1 model call made (ZERO further retry calls)
    assert model_calls == 1
    assert result.model_calls == 1

    # 3. Step observation truthfully records rejection with recoverable=False
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.observation is not None
    assert step.observation.status is ObservationStatus.REJECTED
    assert step.observation.payload.get("recoverable") is False
    pf = step.observation.payload.get("patch_failure", {})
    assert pf.get("kind") == "authorization_error"
    assert pf.get("recoverable") is False
    assert step.stop_reason is ControllerStopReason.FAILED

    # 4. Budget was tracked honestly (1 attempt consumed)
    assert result.budget_state.patch_attempts == 1

    # 5. Candidate is NOT marked applied
    assert context.patch_applied is False


def test_authorization_provenance_violation_fails_closed(tmp_path: Path) -> None:
    """Proves unauthorized patch targets fail closed immediately without retry loops."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    registry = build_registry(context)

    # Unauthorized patch attempting to modify forbidden test file
    unauthorized_patch = (
        "--- a/tests/test_display_name.py\n"
        "+++ b/tests/test_display_name.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-# test\n"
        "+# tampered\n"
    )

    model_calls = 0

    def model_policy(snapshot: ControllerSnapshot) -> ModelDirective:
        nonlocal model_calls
        model_calls += 1
        if snapshot.state is ControllerState.PATCH:
            return ActionDirective(ActionName.APPLY_PATCH, {"patch": unauthorized_patch})
        return TransitionDirective(ControllerState.PATCH, reason="patch")

    adapter = ReactiveModelAdapter(model_policy)
    controller = DeterministicController(registry, adapter, ControllerRunConfig(max_model_calls=10))

    initial_snapshot = ControllerSnapshot(
        run_id="test-unauthorized",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=0,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=3,
            max_test_runs=5,
            max_pdb_observations=10,
            max_active_hypotheses=3,
            max_source_observations=10,
        ),
        budget_state=ControllerBudgetState(0, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=None,
    )

    result = controller.run(initial_snapshot)

    # 1. Controller terminates immediately with FAILED
    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.FAILED

    # 2. Exactly 1 model call made (ZERO further retry calls)
    assert model_calls == 1
    assert result.model_calls == 1

    # 3. Step observation truthfully records rejection with recoverable=False
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.observation is not None
    assert step.observation.status is ObservationStatus.REJECTED
    assert step.observation.payload.get("recoverable") is False
    pf = step.observation.payload.get("patch_failure", {})
    assert pf.get("kind") == "authorization_error"
    assert pf.get("recoverable") is False
    assert step.stop_reason is ControllerStopReason.FAILED

    # 4. Budget was tracked honestly (1 attempt consumed)
    assert result.budget_state.patch_attempts == 1

    # 5. Candidate is NOT marked applied
    assert context.patch_applied is False


def test_fatal_write_permission_exception_redacts_workspace_path(tmp_path: Path) -> None:
    """Proves fatal write/permission errors with absolute workspace paths are redacted across summary, payload, and serialized model requests."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    ws_root = str(workspace.root)
    ws_forward = ws_root.replace("\\", "/")

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)

    # Mock patch_manager to raise a write failure containing the real absolute workspace path
    raw_error_message = f"Patch write failed: [Errno 13] Permission denied: '{ws_root}\\display_name.py'"

    def failing_apply(diff: str) -> None:
        raise PatchApplyError(
            raw_error_message,
            path="display_name.py",
            error_kind="write_failure",
            recoverable=False,
        )

    context.patch_manager.apply_patch = failing_apply  # type: ignore[assignment]
    registry = build_registry(context)

    action = Action(
        action_id="action-000000001",
        run_id="run-redact",
        task_id=task_id,
        state=ControllerState.PATCH,
        name=ActionName.APPLY_PATCH.value,
        arguments={"patch": "--- a/display_name.py\n+++ b/display_name.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"},
    )

    obs = registry.dispatch(action, observation_id="observation-000000001")

    # 1. Observation status is ERROR
    assert obs.status is ObservationStatus.ERROR

    # 2. Observation.summary must NOT contain raw absolute workspace path
    assert ws_root not in obs.summary
    assert ws_forward not in obs.summary

    # 3. Observation.payload must NOT contain raw absolute workspace path
    payload_json = json.dumps(obs.payload, ensure_ascii=False)
    assert ws_root not in payload_json
    assert ws_forward not in payload_json
    assert obs.payload.get("recoverable") is False
    assert obs.payload.get("dispatch_reason") == ToolDispatchReason.TOOL_ERROR.value
    pf = obs.payload.get("patch_failure", {})
    assert pf.get("kind") == "write_failure"
    assert pf.get("recoverable") is False

    # 4. Serialized model request must NOT contain raw absolute workspace path
    config = LiveModelConfig(
        model_name="mock-model",
        command=("mock", "cmd"),
        request_timeout_seconds=10.0,
    )
    live_adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=config,
        transport=_DummyTransport(),
        limits=LiveRunLimits(max_model_requests=1),
        registry=registry,
        evaluation_id="eval-redact",
        case_id="case-redact",
        run_id="run-redact",
        trajectory_id="traj-redact",
    )

    snapshot = ControllerSnapshot(
        run_id="run-redact",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=1,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=3,
            max_test_runs=5,
            max_pdb_observations=10,
            max_active_hypotheses=3,
            max_source_observations=10,
        ),
        budget_state=ControllerBudgetState(1, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=obs,
    )

    request_context = live_adapter._request_context(
        snapshot,
        logical_request_index=1,
        transport_attempt_index=1,
    )
    serialized_request = json.dumps(request_context, ensure_ascii=False)
    assert ws_root not in serialized_request
    assert ws_forward not in serialized_request


def test_revised_patch_automatic_revert_failure_fails_closed_immediately(tmp_path: Path) -> None:
    """Proves automatic revert failure (PatchRevertError) during revise-patch terminates FAILED immediately with 0 further calls."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)

    # Apply patch A first
    patch_a = (
        "--- a/display_name.py\n"
        "+++ b/display_name.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def format_display_name(name: str | None) -> str:\n"
        "-    normalized_name = name.strip()\n"
        "+    normalized_name = (name or '').strip()\n"
    )
    context.patch_manager.apply_patch(patch_a)
    context.candidate_patch = patch_a
    context.patch_applied = True
    assert context.patch_manager.has_active_patch is True

    # Now make revert_patch raise PatchRevertError
    def failing_revert() -> None:
        raise PatchRevertError(
            "Revert write failed: disk hash mismatch",
            path="display_name.py",
            error_kind="revert_failure",
            recoverable=False,
        )

    context.patch_manager.revert_patch = failing_revert  # type: ignore[assignment]
    registry = build_registry(context)

    patch_b = (
        "--- a/display_name.py\n"
        "+++ b/display_name.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def format_display_name(name: str | None) -> str:\n"
        "-    normalized_name = name.strip()\n"
        "+    normalized_name = 'revised'\n"
    )

    model_calls = 0

    def step(snapshot: ControllerSnapshot) -> ModelDirective:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ActionDirective(ActionName.APPLY_PATCH, {"patch": patch_b})
        raise AssertionError("Model should NOT be called after fatal revert failure")

    adapter = ReactiveModelAdapter(step)
    controller = DeterministicController(
        registry,
        adapter,
        ControllerRunConfig(max_model_calls=5),
    )

    initial_snapshot = ControllerSnapshot(
        run_id="run-revised-revert-fail",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=0,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=3,
            max_test_runs=5,
            max_pdb_observations=10,
            max_active_hypotheses=3,
            max_source_observations=10,
        ),
        budget_state=ControllerBudgetState(0, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=None,
    )

    result = controller.run(initial_snapshot)

    # 1. Controller terminates immediately with FAILED
    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.FAILED

    # 2. Exactly 1 model call made (zero subsequent retry requests)
    assert model_calls == 1
    assert result.model_calls == 1

    # 3. Patch B was never applied
    assert context.candidate_patch != patch_b

    # 4. Step observation records ERROR with recoverable=False
    assert len(result.steps) == 1
    step_res = result.steps[0]
    assert step_res.observation is not None
    assert step_res.observation.status is ObservationStatus.ERROR
    assert step_res.observation.payload.get("recoverable") is False
    pf = step_res.observation.payload.get("patch_failure", {})
    assert pf.get("kind") == "revert_failure"
    assert pf.get("recoverable") is False
    assert step_res.stop_reason is ControllerStopReason.FAILED


def test_automatic_revert_state_error_fails_closed_immediately(tmp_path: Path) -> None:
    """Proves automatic revert PatchStateError during revise-patch terminates FAILED immediately with 0 further calls."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)

    # Set active patch
    context.patch_manager._snapshot = "dummy_active_snapshot"  # type: ignore[assignment]
    context.candidate_patch = "some_patch"
    context.patch_applied = True

    def failing_revert() -> None:
        raise PatchStateError("Corrupted patch state", error_kind="state_error", recoverable=False)

    context.patch_manager.revert_patch = failing_revert  # type: ignore[assignment]
    registry = build_registry(context)

    model_calls = 0

    def step(snapshot: ControllerSnapshot) -> ModelDirective:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ActionDirective(ActionName.APPLY_PATCH, {"patch": "diff"})
        raise AssertionError("Model should NOT be called after fatal state failure")

    adapter = ReactiveModelAdapter(step)
    controller = DeterministicController(
        registry,
        adapter,
        ControllerRunConfig(max_model_calls=5),
    )

    initial_snapshot = ControllerSnapshot(
        run_id="run-state-fail",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=0,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=3,
            max_test_runs=5,
            max_pdb_observations=10,
            max_active_hypotheses=3,
            max_source_observations=10,
        ),
        budget_state=ControllerBudgetState(0, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=None,
    )

    result = controller.run(initial_snapshot)

    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.FAILED
    assert model_calls == 1
    assert len(result.steps) == 1
    obs = result.steps[0].observation
    assert obs is not None
    assert obs.status is ObservationStatus.ERROR
    assert obs.payload.get("recoverable") is False
    assert obs.payload.get("patch_failure", {}).get("kind") == "revert_failure"


def test_explicit_revert_patch_revert_error_fails_closed(tmp_path: Path) -> None:
    """Proves explicit revert_patch encountering PatchRevertError terminates FAILED immediately with 0 further calls."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)

    def failing_revert() -> None:
        raise PatchRevertError(
            "Explicit revert failed",
            path="display_name.py",
            error_kind="revert_failure",
            recoverable=False,
        )

    context.patch_manager.revert_patch = failing_revert  # type: ignore[assignment]
    registry = build_registry(context)

    model_calls = 0

    def step(snapshot: ControllerSnapshot) -> ModelDirective:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ActionDirective(ActionName.REVERT_PATCH, {})
        raise AssertionError("Model should NOT be called after fatal explicit revert failure")

    adapter = ReactiveModelAdapter(step)
    controller = DeterministicController(
        registry,
        adapter,
        ControllerRunConfig(max_model_calls=5),
    )

    initial_snapshot = ControllerSnapshot(
        run_id="run-explicit-revert-fail",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=0,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=3,
            max_test_runs=5,
            max_pdb_observations=10,
            max_active_hypotheses=3,
            max_source_observations=10,
        ),
        budget_state=ControllerBudgetState(0, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=None,
    )

    result = controller.run(initial_snapshot)

    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.FAILED
    assert model_calls == 1
    assert len(result.steps) == 1
    obs = result.steps[0].observation
    assert obs is not None
    assert obs.status is ObservationStatus.ERROR
    assert obs.payload.get("recoverable") is False
    assert obs.payload.get("patch_failure", {}).get("kind") == "revert_failure"


def test_fatal_syntax_check_exception_fails_closed(tmp_path: Path) -> None:
    """Proves syntax_check infrastructure error terminates FAILED immediately with 0 further calls."""
    task_id = "curated-none-handling-001"
    fixture_dir = CURATED / task_id
    task = load_task(str(fixture_dir / "task.json"))
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(tmp_path))

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)

    def failing_syntax_check() -> None:
        raise PatchApplyError(
            "Disk read failure during syntax check",
            path="display_name.py",
            error_kind="syntax_check_failure",
            recoverable=False,
        )

    context.patch_manager.syntax_check = failing_syntax_check  # type: ignore[assignment]
    registry = build_registry(context)

    model_calls = 0

    def step(snapshot: ControllerSnapshot) -> ModelDirective:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ActionDirective(ActionName.SYNTAX_CHECK, {})
        raise AssertionError("Model should NOT be called after fatal syntax check failure")

    adapter = ReactiveModelAdapter(step)
    controller = DeterministicController(
        registry,
        adapter,
        ControllerRunConfig(max_model_calls=5),
    )

    initial_snapshot = ControllerSnapshot(
        run_id="run-syntax-fail",
        task_id=task_id,
        state=ControllerState.PATCH,
        model_call_index=0,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=3,
            max_test_runs=5,
            max_pdb_observations=10,
            max_active_hypotheses=3,
            max_source_observations=10,
        ),
        budget_state=ControllerBudgetState(0, 0, 0, 0),
        hypotheses=HypothesisLedger(()),
        last_observation=None,
    )

    result = controller.run(initial_snapshot)

    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.FAILED
    assert model_calls == 1
    assert len(result.steps) == 1
    obs = result.steps[0].observation
    assert obs is not None
    assert obs.status is ObservationStatus.ERROR
    assert obs.payload.get("recoverable") is False
    assert obs.payload.get("patch_failure", {}).get("kind") == "syntax_check_failure"
