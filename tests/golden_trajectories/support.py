from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agentic_debugger.agent.controller import (
    ControllerRunResult,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    ControllerSnapshot,
    ModelScriptExhaustedError,
    ScriptedModelAdapter,
    ScriptedModelStep,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import (
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from agentic_debugger.agent.trajectory import (
    FIXED_TIMESTAMP,
    project_controller_run,
)
from agentic_debugger.demo.catalog import reference_repair_snippets, scenario_ids
from agentic_debugger.evaluation import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.events.replay import ReplayTrajectory, replay_events, semantic_projection
from agentic_debugger.events.schema import ObservationStatus, RunEvent
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.workspace import TaskWorkspace


ROOT = Path(__file__).resolve().parents[2]
CURATED = ROOT / "agentic_debugger" / "datasets" / "curated"

# The reference repairs live in the product demonstration catalog so Task 8 and
# Task 9 cannot drift apart.  The golden artifacts pin the resulting patch
# hashes, so a catalog edit fails these tests loudly instead of silently.
PATCH_REPLACEMENTS = {
    task_id: reference_repair_snippets(task_id) for task_id in scenario_ids()
}


@dataclass(frozen=True)
class TrajectoryRun:
    controller: ControllerRunResult
    trajectory: ReplayTrajectory
    semantic_events: tuple[dict[str, Any], ...]
    task: Any
    patch: str
    evaluator: Any
    tool_calls: tuple[str, ...]
    pdb_action_count: int
    pdb_observation_count: int
    source_root: Path | None
    model_backend: str = "scripted"
    network_attempts: int = 0
    provider_attempts: int = 0


def _task8_ignore_generated(path: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    if Path(path).name == "__pycache__":
        ignored.update(name for name in names if name.endswith(".pyc"))
    ignored.update(name for name in names if name == "__pycache__")
    return ignored


class FixedTaskWorkspace(TaskWorkspace):
    def __init__(self, source_path: str, parent_dir: str | None = None) -> None:
        if not isinstance(parent_dir, str) or not parent_dir:
            raise ValueError("Task 8 workspace parent_dir is required")
        parent = Path(parent_dir).expanduser()
        if not parent.is_dir():
            raise ValueError(f"Task 8 workspace parent does not exist: {parent}")
        root = Path(tempfile.mkdtemp(prefix="task8-fixed-workspace-", dir=parent))
        try:
            shutil.copytree(source_path, root, dirs_exist_ok=True, ignore=_task8_ignore_generated)
        except BaseException as execution:
            try:
                if root.exists():
                    shutil.rmtree(root)
                if root.exists():
                    raise RuntimeError(root)
            except BaseException as cleanup:
                if isinstance(execution, Exception) and isinstance(cleanup, Exception):
                    raise ExceptionGroup("Task 8 workspace creation and cleanup failed", [execution, cleanup]) from execution
                raise BaseExceptionGroup("Task 8 workspace creation and cleanup failed", [execution, cleanup])
            raise
        self._root = str(root)
        self._real_root = os.path.realpath(self._root)
        self._workspace_parent = str(parent.resolve())
        self._cleaned = False

    @property
    def workspace_parent(self) -> str:
        return self._workspace_parent

    def cleanup(self) -> None:
        if self._cleaned:
            return
        root = Path(self._root)
        if root.exists():
            shutil.rmtree(root)
        if root.exists():
            raise RuntimeError(f"Task 8 workspace cleanup failed: {root}")
        self._cleaned = True


class ScriptedOutputAccountingError(RuntimeError):
    pass


class ModelBackendError(RuntimeError):
    pass


class GoldenExecutionLedger:
    def __init__(self) -> None:
        self.network_attempts = 0
        self.provider_attempts = 0

    def reject_network(self) -> None:
        self.network_attempts += 1
        raise AssertionError("golden harness attempted network access")

    def reject_provider(self) -> None:
        self.provider_attempts += 1
        raise ModelBackendError("golden harness attempted provider construction")


def require_scripted_model(model: Any) -> None:
    if not isinstance(model, CountingScriptedModel):
        raise ModelBackendError("Task 8 golden harness accepts only the scripted model backend")


class CountingScriptedModel:
    model_name = "scripted"

    def __init__(self, steps: tuple[ScriptedModelStep, ...], ledger: GoldenExecutionLedger | None = None) -> None:
        self._adapter = ScriptedModelAdapter(steps)
        self.calls = 0
        self.sequence_length = len(steps)
        self.ledger = ledger or GoldenExecutionLedger()

    def next_directive(self, snapshot: ControllerSnapshot) -> Any:
        self.calls += 1
        return self._adapter.next_directive(snapshot)


def scripted_sequence_mapping(steps: tuple[ScriptedModelStep, ...]) -> list[dict[str, Any]]:
    result = []
    for step in steps:
        directive = step.directive
        item = {"expected_state": step.expected_state.value, "kind": directive.kind.value}
        if hasattr(directive, "name"):
            item["action"] = directive.name.value
            item["arguments"] = directive.arguments
        elif hasattr(directive, "target_state"):
            item["target_state"] = directive.target_state.value
            item["reason"] = directive.reason
        result.append(item)
    return result


def task_for(task_id: str) -> Any:
    return load_task(str(CURATED / task_id / "task.json"))


def candidate_patch(task_id: str) -> str:
    rel, old, new = PATCH_REPLACEMENTS[task_id]
    source = (CURATED / task_id / rel).read_text(encoding="utf-8")
    assert old in source
    return "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            source.replace(old, new).splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="\n",
        )
    )


def patch_sha256(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


def _step(state: ControllerState, action: ActionName, arguments: dict[str, Any]) -> ScriptedModelStep:
    return ScriptedModelStep(state, ActionDirective(action, arguments))


def _transition(state: ControllerState, target: ControllerState, reason: str) -> ScriptedModelStep:
    return ScriptedModelStep(state, TransitionDirective(target, reason))


def static_steps(task: Any, patch: str) -> tuple[ScriptedModelStep, ...]:
    node = task.tests.fail_to_pass[0]
    target = task.oracle.target_symbols[0]
    return (
        _step(ControllerState.REPRODUCE, ActionName.RUN_REPRODUCTION, {"node_id": node}),
        _transition(ControllerState.REPRODUCE, ControllerState.UNDERSTAND, "failure reproduced"),
        _step(ControllerState.UNDERSTAND, ActionName.SEARCH_CODE, {"path": task.constraints.allowed_write_paths[0], "query": target}),
        _transition(ControllerState.UNDERSTAND, ControllerState.PATCH, "static evidence sufficient"),
        _step(ControllerState.PATCH, ActionName.APPLY_PATCH, {"patch": patch}),
        _transition(ControllerState.PATCH, ControllerState.VALIDATE, "patch applied"),
        _step(ControllerState.VALIDATE, ActionName.RUN_REPRODUCTION, {"node_id": node}),
        _transition(ControllerState.VALIDATE, ControllerState.DONE, "candidate verified"),
    )


def pdb_steps(task: Any, patch: str) -> tuple[ScriptedModelStep, ...]:
    node = task.tests.fail_to_pass[0]
    return (
        _step(ControllerState.REPRODUCE, ActionName.RUN_REPRODUCTION, {"node_id": node}),
        _transition(ControllerState.REPRODUCE, ControllerState.UNDERSTAND, "failure reproduced"),
        _transition(ControllerState.UNDERSTAND, ControllerState.RUNTIME_EVIDENCE, "runtime state requested"),
        _step(ControllerState.RUNTIME_EVIDENCE, ActionName.START_PDB_SESSION, {}),
        _step(ControllerState.RUNTIME_EVIDENCE, ActionName.GET_STACK_SUMMARY, {}),
        _step(ControllerState.RUNTIME_EVIDENCE, ActionName.GET_FRAME_LOCALS, {"frame_id": 0, "pause_generation": 1}),
        _step(ControllerState.RUNTIME_EVIDENCE, ActionName.STOP_PDB_SESSION, {}),
        _transition(ControllerState.RUNTIME_EVIDENCE, ControllerState.UNDERSTAND, "runtime evidence collected"),
        _transition(ControllerState.UNDERSTAND, ControllerState.PATCH, "runtime evidence sufficient"),
        _step(ControllerState.PATCH, ActionName.APPLY_PATCH, {"patch": patch}),
        _transition(ControllerState.PATCH, ControllerState.VALIDATE, "patch applied"),
        _step(ControllerState.VALIDATE, ActionName.RUN_REPRODUCTION, {"node_id": node}),
        _transition(ControllerState.VALIDATE, ControllerState.DONE, "candidate verified"),
    )


def rejected_steps(task: Any = None, patch: str = "") -> tuple[ScriptedModelStep, ...]:
    return (
        _step(ControllerState.REPRODUCE, ActionName.APPLY_PATCH, {"patch": "forbidden-in-reproduce"}),
    )


def _json_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(arguments, ensure_ascii=False, allow_nan=False))


def _tool_result(payload: dict[str, Any], summary: str, status: ObservationStatus = ObservationStatus.OK) -> ToolResult:
    return ToolResult(status, payload, summary)


def _registry(
    task: Any,
    patch: str,
    tool_calls: list[str],
    pdb_context: dict[str, Any],
    pdb_source_root: Path | None,
    candidate_root: Path,
) -> ToolRegistry:
    def spec(name: ActionName, handler: Callable[[Any, dict[str, Any]], ToolResult]) -> ToolSpec:
        return ToolSpec(name, _json_arguments, handler)

    def run_reproduction(action: Any, arguments: dict[str, Any]) -> ToolResult:
        tool_calls.append(action.name)
        node_id = arguments.get("node_id")
        command = ["python", "-m", "pytest", str(node_id), "-q"]
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", str(node_id), "-q"],
            cwd=str(candidate_root),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        status = "pass" if completed.returncode == 0 else "fail"
        return _tool_result(
            {
                "phase": action.state.value,
                "node_id": node_id,
                "status": status,
                "returncode": completed.returncode,
                "command": command,
                "output_present": bool(completed.stdout or completed.stderr),
            },
            "candidate test execution recorded",
        )

    def search_code(action: Any, arguments: dict[str, Any]) -> ToolResult:
        tool_calls.append(action.name)
        rel = arguments["path"]
        source = (candidate_root / rel).read_text(encoding="utf-8")
        return _tool_result({"path": rel, "query": arguments["query"], "match": arguments["query"] in source, "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()}, "source symbol located")

    def apply_patch(action: Any, arguments: dict[str, Any]) -> ToolResult:
        tool_calls.append(action.name)
        value = arguments["patch"]
        valid = value.startswith("--- a/") and "\n+++ b/" in value and value == patch
        rel, old_source, new_source = PATCH_REPLACEMENTS[task.task_id]
        target = candidate_root / rel
        source = target.read_text(encoding="utf-8")
        applied = valid and old_source in source
        if applied:
            target.write_text(source.replace(old_source, new_source, 1), encoding="utf-8", newline="")
        return _tool_result({"valid_unified_diff": valid, "applied": applied, "target": task.constraints.allowed_write_paths[0], "patch_sha256": patch_sha256(value), "semantic_assertion": "expected-symbol-change"}, "candidate patch applied")

    def start_pdb(action: Any, arguments: dict[str, Any]) -> ToolResult:
        tool_calls.append(action.name)
        assert pdb_source_root is not None
        workspace = TaskWorkspace(str(pdb_source_root))
        session = PdbSession(workspace)
        session.start()
        script = "display_name.py"
        started = session.start_paused_target(script, [2])
        pdb_context["workspace"] = workspace
        pdb_context["session"] = session
        return _tool_result(
            {
                "state": started["state"],
                "line": started["line"],
                "function": started["function"],
                "script": script,
                "fixture_source": "display_name.py",
                "driver": "display_name.py::task8_driver",
                "target_symbol": "format_display_name",
                "curated_input": {"name": None},
            },
            "curated display-name PDB target paused",
        )

    def stack_pdb(action: Any, arguments: dict[str, Any]) -> ToolResult:
        tool_calls.append(action.name)
        stack = pdb_context["session"].get_stack_summary()
        pdb_context["generation"] = stack["pause_generation"]
        return _tool_result(
            {
                "state": stack["state"],
                "pause_generation": stack["pause_generation"],
                "frames": stack["frames"],
                "fixture_source": "display_name.py",
                "driver": "display_name.py::task8_driver",
                "target_symbol": "format_display_name",
            },
            "curated display-name PDB stack",
        )

    def locals_pdb(action: Any, arguments: dict[str, Any]) -> ToolResult:
        tool_calls.append(action.name)
        generation = pdb_context["generation"]
        locals_result = pdb_context["session"].get_frame_locals(0, generation)
        return _tool_result(
            {
                "state": locals_result["state"],
                "pause_generation": locals_result["pause_generation"],
                "frame_id": locals_result["frame_id"],
                "locals": locals_result["locals"],
                "fixture_source": "display_name.py",
                "driver": "display_name.py::task8_driver",
                "target_symbol": "format_display_name",
                "curated_input": {"name": None},
            },
            "curated display-name PDB locals",
        )

    def stop_pdb(action: Any, arguments: dict[str, Any]) -> ToolResult:
        tool_calls.append(action.name)
        session = pdb_context.pop("session", None)
        if session is not None:
            session.stop()
        workspace = pdb_context.pop("workspace", None)
        if workspace is not None:
            workspace.cleanup()
        return _tool_result({"cleaned": True}, "PDB session cleaned")

    return ToolRegistry((
        spec(ActionName.RUN_REPRODUCTION, run_reproduction),
        spec(ActionName.SEARCH_CODE, search_code),
        spec(ActionName.APPLY_PATCH, apply_patch),
        spec(ActionName.START_PDB_SESSION, start_pdb),
        spec(ActionName.GET_STACK_SUMMARY, stack_pdb),
        spec(ActionName.GET_FRAME_LOCALS, locals_pdb),
        spec(ActionName.STOP_PDB_SESSION, stop_pdb),
    ))


def project_controller(result: ControllerRunResult) -> list[RunEvent]:
    """Project a Task 8 controller run through the shared product projection."""

    return project_controller_run(
        result,
        tool_version="task8",
        model="scripted",
        timestamp=FIXED_TIMESTAMP,
        duration_ms=0,
    )


def _cleanup_resources(
    candidate_root: Path,
    pdb_root: Path | None,
    pdb_context: dict[str, Any],
    evaluator_parent: Path | None,
) -> list[BaseException]:
    errors: list[BaseException] = []

    def attempt(label: str, action: Callable[[], None]) -> None:
        try:
            action()
        except BaseException as exc:
            errors.append(RuntimeError(f"{label} cleanup failed: {exc}"))

    def stop_session() -> None:
        session = pdb_context.pop("session", None)
        if session is not None:
            session.stop()

    def clean_pdb_workspace() -> None:
        workspace = pdb_context.pop("workspace", None)
        if workspace is not None:
            workspace.cleanup()

    def remove(path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)
        if path.exists():
            raise RuntimeError(path)

    attempt("PDB session", stop_session)
    attempt("PDB task workspace", clean_pdb_workspace)
    if pdb_root is not None:
        attempt("PDB source root", lambda: remove(pdb_root))
    attempt("controller workspace", lambda: remove(candidate_root))
    if evaluator_parent is not None:
        attempt("evaluator parent", lambda: remove(evaluator_parent))
    return errors


def _raise_execution_and_cleanup(execution: BaseException, cleanup: list[BaseException]) -> None:
    if not cleanup:
        raise execution
    if isinstance(execution, Exception) and all(isinstance(item, Exception) for item in cleanup):
        raise ExceptionGroup("Task 8 execution and cleanup failed", [execution, *cleanup]) from execution
    raise BaseExceptionGroup("Task 8 execution and cleanup failed", [execution, *cleanup])


def run_trajectory(
    task_id: str,
    policy: str,
    steps_factory: Callable[[Any, str], tuple[ScriptedModelStep, ...]] | None,
    tmp_path: Path,
) -> TrajectoryRun:
    task = task_for(task_id)
    patch = candidate_patch(task_id)
    tool_calls: list[str] = []
    pdb_context: dict[str, Any] = {}
    pdb_root: Path | None = None
    evaluator_parent: Path | None = None
    candidate_root = tmp_path / "controller-workspace"
    execution: BaseException | None = None
    run: TrajectoryRun | None = None
    try:
        shutil.copytree(CURATED / task_id, candidate_root, ignore=_task8_ignore_generated)
        if policy == "pdb":
            pdb_root = tmp_path / "pdb-source"
            shutil.copytree(CURATED / task_id, pdb_root, ignore=_task8_ignore_generated)
            display_name = pdb_root / "display_name.py"
            display_name.write_text(
                display_name.read_text(encoding="utf-8")
                + "\n\ndef task8_driver():\n"
                + "    name = None\n"
                + "    return format_display_name(name)\n\n"
                + "task8_driver()\n",
                encoding="utf-8",
                newline="\n",
            )
        default_steps = rejected_steps if policy == "rejected" else (static_steps if policy == "static" else pdb_steps)
        steps = steps_factory(task, patch) if steps_factory is not None else default_steps(task, patch)
        ledger = GoldenExecutionLedger()
        adapter = CountingScriptedModel(tuple(steps), ledger)
        require_scripted_model(adapter)
        limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
        snapshot = ControllerSnapshot(task.task_id + "-run", task.task_id, ControllerState.REPRODUCE, 0, limits, ControllerBudgetState(), HypothesisLedger())
        controller = DeterministicController(_registry(task, patch, tool_calls, pdb_context, pdb_root, candidate_root), adapter)
        result = controller.run(snapshot)
        if adapter.calls > len(steps):
            raise ModelScriptExhaustedError(f"model script exhausted: attempted call {adapter.calls} of {len(steps)}")
        if adapter.calls < len(steps):
            raise ScriptedOutputAccountingError(f"unused scripted outputs: consumed {adapter.calls} of {len(steps)}")
        stream = io.StringIO()
        logger = JsonlEventLogger(result.run_id, result.task_id, stream=stream)
        for event in project_controller(result):
            logger.append(event)
        logger.flush()
        logger.close()
        trajectory = replay_events(stream.getvalue())
        evaluator = None
        if policy in {"static", "pdb"}:
            evaluator_parent = tmp_path / "evaluator-parent"
            evaluator_parent.mkdir(parents=True, exist_ok=True)
            evaluator = EvaluationVerifier(str(ROOT), workspace_parent=str(evaluator_parent), workspace_factory=FixedTaskWorkspace).evaluate(task, patch)
        run = TrajectoryRun(
            result,
            trajectory,
            semantic_projection(trajectory, workspace_roots=[str(tmp_path)]),
            task,
            patch,
            evaluator,
            tuple(tool_calls),
            sum(item in {"start_pdb_session", "get_stack_summary", "get_frame_locals", "stop_pdb_session"} for item in tool_calls),
            sum(item in {"get_stack_summary", "get_frame_locals"} for item in tool_calls),
            pdb_root,
            "scripted",
            ledger.network_attempts,
            ledger.provider_attempts,
        )
    except BaseException as exc:
        execution = exc
    cleanup = _cleanup_resources(candidate_root, pdb_root, pdb_context, evaluator_parent)
    if execution is not None:
        _raise_execution_and_cleanup(execution, cleanup)
    if cleanup:
        if len(cleanup) == 1:
            raise cleanup[0]
        raise ExceptionGroup("Task 8 cleanup failed", [*cleanup])
    if run is None:
        raise RuntimeError("Task 8 trajectory did not produce a result")
    return run
