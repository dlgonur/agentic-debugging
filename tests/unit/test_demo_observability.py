"""Demo producer wiring tests: real tool handlers emit Task-4 events.

Attaches a ``SessionObservability`` to the real ``DemoToolContext`` and
proves the demonstration tool handlers project genuine debugger/patch/source/
diagnosis facts into validated application events at the authoritative
boundaries -- without changing any tool result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from agentic_debugger.agent.controller_policy import ActionName, PdbPolicy
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry
from agentic_debugger.application.events import SourceKind
from agentic_debugger.application.observability import (
    ObservabilityContext,
    SessionObservability,
)
from agentic_debugger.application.presentation import (
    PatchStage,
    PresentationIdentity,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
from agentic_debugger.demo.runner import CURATED_RELATIVE_ROOT
from agentic_debugger.demo.tools import (
    DemoToolContext,
    build_registry,
    prepare_pdb_probe,
)
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.events.schema import Action, Observation, ObservationStatus
from agentic_debugger.runtime.workspace import TaskWorkspace

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "curated-none-handling-001"
FIXTURE = REPO_ROOT / CURATED_RELATIVE_ROOT / TASK_ID
FIXED = "2026-08-14T08:00:00Z"
SESSION_ID = "session-demo-obs-001"
RUN_ID = "run-demo-obs-001"


@pytest.fixture
def context(tmp_path: Path) -> Iterator[DemoToolContext]:
    workspace = TaskWorkspace(str(FIXTURE), parent_dir=str(tmp_path))
    scenario = scenario_for(TASK_ID)
    task = load_task(str(FIXTURE / "task.json"))
    source = (Path(workspace.root) / scenario.reference_repair.target_path).read_text(
        encoding="utf-8"
    )
    patch = build_reference_patch(source, scenario.reference_repair)
    probe = prepare_pdb_probe(FIXTURE, scenario, tmp_path)
    observability = SessionObservability(
        ObservabilityContext(
            session_id=SESSION_ID,
            task_id=task.task_id,
            source_kind=SourceKind.OFFLINE_DEMO,
            run_id=RUN_ID,
        ),
        clock=lambda: FIXED,
    )
    item = DemoToolContext(
        task=task,
        workspace=workspace,
        patch=patch,
        probe=probe,
        observability=observability,
    )
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
    registry = build_registry(
        context, pdb_policy=PdbPolicy.ALWAYS_ON, interactive_debugger_controls=True
    )
    action = Action(
        action_id="action-000000000",
        run_id=RUN_ID,
        task_id=context.task.task_id,
        state=state,
        name=name,
        arguments=arguments,
    )
    return registry.dispatch(action, observation_id="observation-000000000")


def _view(context: DemoToolContext):
    view = initial_session_view(
        PresentationIdentity(
            task_id=context.task.task_id,
            source_kind=SourceKind.OFFLINE_DEMO,
            session_id=SESSION_ID,
        )
    )
    for event in context.observability.events():
        view = reduce_event(view, event)
    return view


def test_initial_source_snapshot_captured(context: DemoToolContext):
    kinds = [e.event_kind.value for e in context.observability.events()]
    assert "source.snapshot" in kinds
    view = _view(context)
    assert view.sources
    assert view.sources[0].path == "display_name.py"


def test_hypothesis_emits_diagnosis(context: DemoToolContext):
    observation = _dispatch(
        context,
        "express_root_cause_hypothesis",
        ControllerState.UNDERSTAND,
        {
            "hypothesis_id": "h-1",
            "statement": "name is None before strip",
            "target_file": "display_name.py",
            "target_symbol": "format_display_name",
            "confidence": "high",
        },
    )
    assert observation.status is ObservationStatus.OK
    view = _view(context)
    assert view.diagnosis is not None
    assert view.diagnosis.text == "name is None before strip"
    assert view.diagnosis.file_path == "display_name.py"


def test_apply_patch_emits_patch_lifecycle_and_applied_source(context: DemoToolContext):
    observation = _dispatch(
        context,
        "apply_patch",
        ControllerState.PATCH,
        {"patch": context.patch},
    )
    assert observation.status is ObservationStatus.OK
    view = _view(context)
    applied = [a for a in view.patch_attempts if a.stage is PatchStage.APPLIED]
    assert len(applied) == 1
    assert applied[0].patch_sha256
    assert applied[0].patch_text == context.patch
    assert "display_name.py" in applied[0].changed_files
    # The applied source snapshot reflects the patched workspace content.
    applied_sources = [s for s in view.sources if s.stage.value == "applied"]
    assert applied_sources
    assert applied_sources[0].path == "display_name.py"


def test_revert_patch_emits_reverted(context: DemoToolContext):
    assert _dispatch(context, "apply_patch", ControllerState.PATCH, {"patch": context.patch}).status is ObservationStatus.OK
    assert _dispatch(context, "revert_patch", ControllerState.PATCH, {}).status is ObservationStatus.OK
    view = _view(context)
    reverted = [a for a in view.patch_attempts if a.stage is PatchStage.REVERTED]
    assert reverted
    assert any(s.stage.value == "reverted" for s in view.sources)


def test_rejected_patch_emits_rejected_not_applied(context: DemoToolContext):
    observation = _dispatch(
        context,
        "apply_patch",
        ControllerState.PATCH,
        {"patch": "not a valid diff"},
    )
    assert observation.status is ObservationStatus.REJECTED
    view = _view(context)
    rejected = [a for a in view.patch_attempts if a.stage is PatchStage.REJECTED]
    assert rejected
    assert not any(a.stage is PatchStage.APPLIED for a in view.patch_attempts)


def test_pdb_handlers_emit_debugger_events(context: DemoToolContext):
    probe = context.probe
    started = _dispatch(
        context,
        "start_pdb_session",
        ControllerState.RUNTIME_EVIDENCE,
        {"breakpoint_line": probe.breakpoint_line},
    )
    assert started.status is ObservationStatus.OK
    stack = _dispatch(context, "get_stack_summary", ControllerState.RUNTIME_EVIDENCE, {})
    assert stack.status is ObservationStatus.OK
    payload = stack.payload
    frame_id = payload["frames"][0]["frame_id"]
    generation = payload["pause_generation"]
    locals_obs = _dispatch(
        context,
        "get_frame_locals",
        ControllerState.RUNTIME_EVIDENCE,
        {"frame_id": frame_id, "pause_generation": generation},
    )
    assert locals_obs.status is ObservationStatus.OK
    _dispatch(context, "stop_pdb_session", ControllerState.RUNTIME_EVIDENCE, {})
    kinds = [e.event_kind.value for e in context.observability.events()]
    assert "debugger.started" in kinds
    assert "debugger.location_changed" in kinds
    assert "debugger.stack_observed" in kinds
    assert "debugger.locals_observed" in kinds
    view = _view(context)
    assert view.debugger.session_started is True
    assert view.debugger.frames
    assert view.debugger.locals


def test_observability_never_changes_tool_results(context: DemoToolContext, tmp_path: Path):
    # Dispatching the same actions without observability produces identical
    # observations; the producer is strictly observational.
    (tmp_path / "plain").mkdir()
    plain_workspace = TaskWorkspace(str(FIXTURE), parent_dir=str(tmp_path / "plain"))
    plain = DemoToolContext(
        task=context.task,
        workspace=plain_workspace,
        patch=context.patch,
        probe=context.probe,
    )
    try:
        obs_with = _dispatch(
            context, "apply_patch", ControllerState.PATCH, {"patch": context.patch}
        )
        obs_without = _dispatch(
            plain, "apply_patch", ControllerState.PATCH, {"patch": context.patch}
        )
        assert obs_with.status is obs_without.status
        assert obs_with.payload["applied"] == obs_without.payload["applied"]
        assert obs_with.payload["changed_files"] == obs_without.payload["changed_files"]
    finally:
        plain.release_pdb()
        plain_workspace.cleanup()
