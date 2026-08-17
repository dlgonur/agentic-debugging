"""Tool-boundary tests for the Task 9 demonstration handlers.

These tests exercise the demonstration's rejection and error paths through the
real tool registry, so a failing component is proven to surface as an honest
``rejected``/``error`` observation rather than as a fabricated success.
"""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
from typing import Iterator

import pytest

from agentic_debugger.agent.controller_policy import ActionName
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolDispatchReason
from agentic_debugger.demo.catalog import (
    PROBE_DRIVER_FUNCTION,
    build_reference_patch,
    scenario_for,
)
from agentic_debugger.demo.runner import CURATED_RELATIVE_ROOT
from agentic_debugger.demo.tools import (
    DemoToolContext,
    DemoToolError,
    build_registry,
    prepare_pdb_probe,
    pytest_argv,
)
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.events.schema import Action, Observation, ObservationStatus
from agentic_debugger.runtime.workspace import TaskWorkspace

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "curated-none-handling-001"
FIXTURE = REPO_ROOT / CURATED_RELATIVE_ROOT / TASK_ID


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture
def context(tmp_path: Path) -> Iterator[DemoToolContext]:
    workspace = TaskWorkspace(str(FIXTURE), parent_dir=str(tmp_path))
    scenario = scenario_for(TASK_ID)
    task = load_task(str(FIXTURE / "task.json"))
    source = (Path(workspace.root) / scenario.reference_repair.target_path).read_text(
        encoding="utf-8"
    )
    patch = build_reference_patch(source, scenario.reference_repair)
    item = DemoToolContext(task=task, workspace=workspace, patch=patch, probe=None)
    try:
        yield item
    finally:
        item.release_pdb()
        workspace.cleanup()


def _dispatch(
    context: DemoToolContext,
    name: str,
    state: ControllerState,
    arguments: dict[str, object],
) -> Observation:
    registry = build_registry(context)
    action = Action(
        action_id="action-000000000",
        run_id="demo-run",
        task_id=context.task.task_id,
        state=state,
        name=name,
        arguments=arguments,
    )
    return registry.dispatch(action, observation_id="observation-000000000")


def _reason(observation: Observation) -> str:
    return observation.payload["dispatch_reason"]


class TestArgumentValidation:
    def test_unknown_argument_is_rejected(self, context: DemoToolContext) -> None:
        observation = _dispatch(
            context, "syntax_check", ControllerState.PATCH, {"unexpected": 1}
        )
        assert observation.status is ObservationStatus.REJECTED
        assert _reason(observation) == ToolDispatchReason.INVALID_ARGUMENTS.value

    def test_missing_argument_is_rejected(self, context: DemoToolContext) -> None:
        observation = _dispatch(context, "find_function", ControllerState.UNDERSTAND, {})
        assert observation.status is ObservationStatus.REJECTED
        assert _reason(observation) == ToolDispatchReason.INVALID_ARGUMENTS.value

    def test_wrong_argument_type_is_rejected(self, context: DemoToolContext) -> None:
        observation = _dispatch(
            context,
            "get_source_window",
            ControllerState.UNDERSTAND,
            {"path": "display_name.py", "line": "2"},
        )
        assert observation.status is ObservationStatus.REJECTED
        assert _reason(observation) == ToolDispatchReason.INVALID_ARGUMENTS.value

    def test_negative_integer_argument_is_rejected(self, context: DemoToolContext) -> None:
        observation = _dispatch(
            context,
            "get_frame_locals",
            ControllerState.RUNTIME_EVIDENCE,
            {"frame_id": -1, "pause_generation": 1},
        )
        assert observation.status is ObservationStatus.REJECTED
        assert _reason(observation) == ToolDispatchReason.INVALID_ARGUMENTS.value

    def test_reproduction_phase_rejection_exposes_safe_legal_value_diagnostic(
        self, context: DemoToolContext
    ) -> None:
        observation = _dispatch(
            context, "run_reproduction", ControllerState.REPRODUCE, {"phase": "initial"}
        )
        assert observation.status is ObservationStatus.REJECTED
        assert _reason(observation) == ToolDispatchReason.TOOL_REJECTED.value
        assert observation.payload["diagnostic"] == "phase must be baseline or post_patch"


class TestStatePolicyEnforcement:
    def test_patch_action_is_refused_outside_the_patch_state(
        self, context: DemoToolContext
    ) -> None:
        observation = _dispatch(
            context, "apply_patch", ControllerState.REPRODUCE, {"patch": context.patch}
        )
        assert observation.status is ObservationStatus.REJECTED
        assert _reason(observation) == ToolDispatchReason.STATE_ACTION_NOT_ALLOWED.value
        assert context.tool_calls == []
        assert not context.patch_applied

    def test_debugger_action_is_refused_outside_runtime_evidence(
        self, context: DemoToolContext
    ) -> None:
        observation = _dispatch(context, "get_stack_summary", ControllerState.PATCH, {})
        assert observation.status is ObservationStatus.REJECTED
        assert _reason(observation) == ToolDispatchReason.STATE_ACTION_NOT_ALLOWED.value


class TestPatchLifecycleRejections:
    def test_denied_write_path_is_rejected(self, context: DemoToolContext) -> None:
        relative = "tests/test_display_name.py"
        original = (Path(context.workspace.root) / relative).read_text(encoding="utf-8")
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                (original + "\n# tampered\n").splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
                lineterm="\n",
            )
        )
        observation = _dispatch(context, "apply_patch", ControllerState.PATCH, {"patch": diff})
        assert observation.status is ObservationStatus.REJECTED
        assert _reason(observation) == ToolDispatchReason.TOOL_REJECTED.value
        assert context.tool_errors and context.tool_errors[0]["action"] == "apply_patch"
        assert (Path(context.workspace.root) / relative).read_text(encoding="utf-8") == original

    def test_malformed_diff_is_rejected(self, context: DemoToolContext) -> None:
        observation = _dispatch(
            context, "apply_patch", ControllerState.PATCH, {"patch": "not a diff at all"}
        )
        assert observation.status is ObservationStatus.REJECTED
        assert _reason(observation) == ToolDispatchReason.TOOL_REJECTED.value

    def test_syntax_check_without_an_active_patch_errors(
        self, context: DemoToolContext
    ) -> None:
        observation = _dispatch(context, "syntax_check", ControllerState.PATCH, {})
        assert observation.status is ObservationStatus.ERROR
        assert _reason(observation) == ToolDispatchReason.TOOL_ERROR.value

    def test_successful_patch_and_syntax_check_record_real_state(
        self, context: DemoToolContext
    ) -> None:
        applied = _dispatch(
            context, "apply_patch", ControllerState.PATCH, {"patch": context.patch}
        )
        assert applied.status is ObservationStatus.OK
        assert applied.payload["applied"] is True
        assert applied.payload["changed_files"] == ["display_name.py"]
        assert context.patch_applied is True
        checked = _dispatch(context, "syntax_check", ControllerState.PATCH, {})
        assert checked.status is ObservationStatus.OK
        assert checked.payload["all_passed"] is True
        assert context.syntax_passed is True
        patched = (Path(context.workspace.root) / "display_name.py").read_text(encoding="utf-8")
        assert 'if name is not None else ""' in patched


class TestSourceRetrievalFailures:
    def test_unknown_symbol_is_an_error(self, context: DemoToolContext) -> None:
        observation = _dispatch(
            context,
            "find_function",
            ControllerState.UNDERSTAND,
            {"name": "absent_symbol", "path": "display_name.py"},
        )
        assert observation.status is ObservationStatus.ERROR
        assert _reason(observation) == ToolDispatchReason.TOOL_ERROR.value

    def test_line_beyond_the_file_is_an_error(self, context: DemoToolContext) -> None:
        observation = _dispatch(
            context,
            "get_source_window",
            ControllerState.UNDERSTAND,
            {"path": "display_name.py", "line": 10_000},
        )
        assert observation.status is ObservationStatus.ERROR

    def test_path_traversal_is_refused(self, context: DemoToolContext) -> None:
        observation = _dispatch(
            context,
            "get_source_window",
            ControllerState.UNDERSTAND,
            {"path": "../escape.py", "line": 1},
        )
        assert observation.status is ObservationStatus.ERROR

    def test_located_symbol_reports_a_usable_line(self, context: DemoToolContext) -> None:
        observation = _dispatch(
            context,
            "find_function",
            ControllerState.UNDERSTAND,
            {"name": "format_display_name", "path": "display_name.py"},
        )
        assert observation.status is ObservationStatus.OK
        assert observation.payload["path"] == "display_name.py"
        assert observation.payload["start_line"] == 1


class TestValidationEvidence:
    def test_classification_without_evidence_is_an_error(
        self, context: DemoToolContext
    ) -> None:
        observation = _dispatch(context, "classify_outcome", ControllerState.VALIDATE, {})
        assert observation.status is ObservationStatus.ERROR
        assert context.controller_outcome is None

    def test_classification_uses_the_recorded_controller_evidence(
        self, context: DemoToolContext
    ) -> None:
        context.post_patch_f2p_passed = True
        context.regression_passed = False
        observation = _dispatch(context, "classify_outcome", ControllerState.VALIDATE, {})
        assert observation.status is ObservationStatus.OK
        assert observation.payload["outcome"] == "BREAKING_RESOLVED"
        assert context.controller_outcome == "BREAKING_RESOLVED"

    def test_apply_and_revert_forget_stale_validation_evidence(
        self, context: DemoToolContext
    ) -> None:
        context.post_patch_f2p_passed = True
        context.regression_passed = True
        context.controller_outcome = "RESOLVED"
        observation = _dispatch(
            context, "apply_patch", ControllerState.PATCH, {"patch": context.patch}
        )
        assert observation.status is ObservationStatus.OK
        assert context.post_patch_f2p_passed is None
        assert context.regression_passed is None
        assert context.controller_outcome is None
        context.post_patch_f2p_passed = False
        context.regression_passed = True
        context.controller_outcome = "NO_OP"
        reverted = _dispatch(context, "revert_patch", ControllerState.VALIDATE, {})
        assert reverted.status is ObservationStatus.OK
        assert context.post_patch_f2p_passed is None
        assert context.regression_passed is None
        assert context.controller_outcome is None

    def test_hypothesis_declaration_records_the_localization_claim(
        self, context: DemoToolContext
    ) -> None:
        observation = _dispatch(
            context,
            "express_root_cause_hypothesis",
            ControllerState.UNDERSTAND,
            {
                "hypothesis_id": "h-1",
                "statement": "statement",
                "target_file": "display_name.py",
                "target_symbol": "format_display_name",
                "confidence": "low",
            },
        )
        assert observation.status is ObservationStatus.OK
        assert context.declared_localization == {
            "file_path": "display_name.py",
            "symbol": "format_display_name",
        }


class TestDebuggerLifecycleGuards:
    def test_start_without_a_probe_is_rejected(self, context: DemoToolContext) -> None:
        observation = _dispatch(
            context, "start_pdb_session", ControllerState.RUNTIME_EVIDENCE, {}
        )
        assert observation.status is ObservationStatus.REJECTED
        assert _reason(observation) == ToolDispatchReason.TOOL_REJECTED.value
        assert context.pdb_session is None

    def test_inspection_without_a_session_is_rejected(
        self, context: DemoToolContext
    ) -> None:
        for name, arguments in (
            ("get_stack_summary", {}),
            ("get_frame_locals", {"frame_id": 0, "pause_generation": 1}),
            ("safe_eval_expression", {"frame_id": 0, "pause_generation": 1, "expression": "x"}),
        ):
            observation = _dispatch(
                context, name, ControllerState.RUNTIME_EVIDENCE, arguments
            )
            assert observation.status is ObservationStatus.REJECTED
            assert _reason(observation) == ToolDispatchReason.TOOL_REJECTED.value

    def test_stop_is_safe_when_nothing_started(self, context: DemoToolContext) -> None:
        observation = _dispatch(
            context, "stop_pdb_session", ControllerState.RUNTIME_EVIDENCE, {}
        )
        assert observation.status is ObservationStatus.OK
        # workspace_removed reports the truth: nothing existed to remove.
        assert observation.payload == {
            "dispatch_reason": "ok",
            "session_started": False,
            "stopped": True,
            "workspace_removed": False,
        }
        assert context.release_pdb() == []

    def test_a_failed_session_stop_keeps_the_handle_for_a_retry(
        self, context: DemoToolContext
    ) -> None:
        class StubbornSession:
            def __init__(self) -> None:
                self.stop_calls = 0

            def stop(self) -> None:
                self.stop_calls += 1
                if self.stop_calls == 1:
                    raise RuntimeError("reader thread did not stop")

        session = StubbornSession()
        context.pdb_session = session  # type: ignore[assignment]
        first = context.release_pdb()
        assert len(first) == 1
        # The handle survives so an outer cleanup pass is a genuine retry.
        assert context.pdb_session is session
        assert context.release_pdb() == []
        assert context.pdb_session is None
        assert session.stop_calls == 2

    def test_diagnostics_never_leak_a_disposable_workspace_path(
        self, context: DemoToolContext
    ) -> None:
        context.record_error("apply_patch", OSError(f"failed on {context.workspace.root}"))
        diagnostic = context.tool_errors[0]["diagnostic"]
        assert context.workspace.root not in diagnostic
        assert "task_workspace_" not in diagnostic
        assert "<WORKSPACE>" in diagnostic


class TestProbePreparation:
    def test_probe_copy_receives_a_driver_and_leaves_the_fixture_untouched(
        self, tmp_path: Path
    ) -> None:
        before = _tree_digest(FIXTURE)
        probe = prepare_pdb_probe(FIXTURE, scenario_for(TASK_ID), tmp_path)
        assert _tree_digest(FIXTURE) == before
        copied = (probe.source_dir / probe.script).read_text(encoding="utf-8")
        assert PROBE_DRIVER_FUNCTION in copied
        assert probe.breakpoint_line == 2
        assert probe.focus_function == "format_display_name"
        original = (FIXTURE / probe.script).read_text(encoding="utf-8")
        assert copied.startswith(original)

    def test_interactive_probe_does_not_resolve_a_hidden_breakpoint(
        self, tmp_path: Path
    ) -> None:
        probe = prepare_pdb_probe(
            FIXTURE,
            scenario_for(TASK_ID),
            tmp_path,
            model_selects_breakpoint=True,
        )
        assert probe.breakpoint_line == 0
        copied = (probe.source_dir / probe.script).read_text(encoding="utf-8")
        assert PROBE_DRIVER_FUNCTION in copied

    def test_probe_refuses_to_overwrite_an_existing_directory(
        self, tmp_path: Path
    ) -> None:
        prepare_pdb_probe(FIXTURE, scenario_for(TASK_ID), tmp_path)
        with pytest.raises(DemoToolError, match="already exists"):
            prepare_pdb_probe(FIXTURE, scenario_for(TASK_ID), tmp_path)

    def test_probe_reports_a_missing_module(self, tmp_path: Path) -> None:
        source = tmp_path / "fixture"
        (source / "tests").mkdir(parents=True)
        (source / "tests" / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
        with pytest.raises(DemoToolError, match="probe module is missing"):
            prepare_pdb_probe(source, scenario_for(TASK_ID), tmp_path / "out")


class TestPostMortemToolCleanup:
    def test_session_factory_failure_removes_owned_workspace(
        self, context: DemoToolContext, tmp_path: Path
    ) -> None:
        context.probe = prepare_pdb_probe(
            FIXTURE, scenario_for(TASK_ID), tmp_path / "probe-parent"
        )
        context.baseline_failure_reproduced = True

        def fail_factory(workspace):
            raise RuntimeError("synthetic session factory failure")

        context.pdb_session_factory = fail_factory
        observation = _dispatch(
            context, "get_failure_trace", ControllerState.REPRODUCE, {}
        )
        assert observation.status is ObservationStatus.ERROR
        assert _reason(observation) == ToolDispatchReason.TOOL_ERROR.value
        assert context.pdb_session is None
        assert context.pdb_workspace is None

    def test_unexpected_start_exception_still_cleans_up(
        self, context: DemoToolContext, tmp_path: Path
    ) -> None:
        """Regression: a non-PdbSessionError from start() must not leak the session/workspace."""

        class _BoomOnStart:
            def __init__(self, workspace):
                self.workspace = workspace
                self.stopped = False

            def start(self):
                raise OSError("unexpected worker pipe failure")

            def run_post_mortem(self, script):
                raise AssertionError("run_post_mortem should not be reached")

            def stop(self):
                self.stopped = True

        context.probe = prepare_pdb_probe(
            FIXTURE, scenario_for(TASK_ID), tmp_path / "probe-parent"
        )
        context.baseline_failure_reproduced = True
        context.pdb_session_factory = _BoomOnStart
        observation = _dispatch(
            context, "get_failure_trace", ControllerState.REPRODUCE, {}
        )
        assert observation.status is ObservationStatus.ERROR
        assert _reason(observation) == ToolDispatchReason.TOOL_ERROR.value
        # The session and workspace must be released, not leaked.
        assert context.pdb_session is None
        assert context.pdb_workspace is None
        assert context.pdb_session_started is False

    def test_unexpected_run_post_mortem_exception_still_cleans_up(
        self, context: DemoToolContext, tmp_path: Path
    ) -> None:
        """Regression: a non-PdbSessionError from run_post_mortem() must not leak."""

        class _BoomOnRun:
            def __init__(self, workspace):
                self.workspace = workspace
                self.stopped = False

            def start(self):
                pass

            def run_post_mortem(self, script):
                raise RuntimeError("unexpected serialization crash")

            def stop(self):
                self.stopped = True

        context.probe = prepare_pdb_probe(
            FIXTURE, scenario_for(TASK_ID), tmp_path / "probe-parent"
        )
        context.baseline_failure_reproduced = True
        context.pdb_session_factory = _BoomOnRun
        observation = _dispatch(
            context, "get_failure_trace", ControllerState.REPRODUCE, {}
        )
        assert observation.status is ObservationStatus.ERROR
        assert _reason(observation) == ToolDispatchReason.TOOL_ERROR.value
        assert context.pdb_session is None
        assert context.pdb_workspace is None
        # start() succeeded so the started flag was set before the crash.
        assert context.pdb_session_started is True


class TestArgvConstruction:
    def test_node_ids_replace_the_manifest_selection(self) -> None:
        base = ["python", "-m", "pytest", "tests/t.py::a", "-q", "-p", "no:cacheprovider"]
        assert pytest_argv(base, ["tests/t.py::b", "tests/t.py::c"]) == [
            "python",
            "-m",
            "pytest",
            "tests/t.py::b",
            "tests/t.py::c",
            "-q",
            "-p",
            "no:cacheprovider",
        ]

    def test_node_ids_are_appended_when_the_manifest_has_none(self) -> None:
        assert pytest_argv(["python", "-m", "pytest", "-q"], ["tests/t.py::a"]) == [
            "python",
            "-m",
            "pytest",
            "-q",
            "tests/t.py::a",
        ]


def test_interactive_debugger_registry_is_opt_in_and_model_selectable(
    context: DemoToolContext,
) -> None:
    default_registry = build_registry(context)
    default_names = {item.value for item in default_registry.names()}
    assert "continue_pdb_session" not in default_names
    assert "step_pdb_session" not in default_names
    assert "next_pdb_session" not in default_names
    assert default_registry.argument_contracts()[ActionName.START_PDB_SESSION]["required"] == []

    interactive = build_registry(context, interactive_debugger_controls=True)
    names = {item.value for item in interactive.names()}
    assert {"continue_pdb_session", "step_pdb_session", "next_pdb_session"} <= names
    start_contract = interactive.argument_contracts()[ActionName.START_PDB_SESSION]
    assert start_contract["required"] == ["breakpoint_line"]
    assert start_contract["properties"]["breakpoint_line"] == {"type": "integer", "minimum": 1}


def test_interactive_execution_control_dispatches_typed_session_methods(
    context: DemoToolContext,
) -> None:
    class FakeSession:
        def step_paused_target(self):
            return {"state": "paused", "script": "x.py", "line": 4, "function": "f"}

        def next_paused_target(self):
            return {"state": "paused", "script": "x.py", "line": 5, "function": "f"}

        def continue_paused_target(self):
            return {"state": "exited", "script": "x.py", "exit_code": 0}

        def stop(self):
            return None

    context.pdb_session = FakeSession()  # type: ignore[assignment]
    registry = build_registry(context, interactive_debugger_controls=True)
    action = Action(
        action_id="action-000000001",
        run_id="demo-run",
        task_id=context.task.task_id,
        state=ControllerState.RUNTIME_EVIDENCE,
        name="step_pdb_session",
        arguments={},
    )
    observation = registry.dispatch(action, observation_id="observation-000000001")
    assert observation.status is ObservationStatus.OK
    assert observation.payload["state"] == "paused"
    assert observation.payload["line"] == 4
    assert context.pdb_observation_names[-1] == "step_pdb_session"
