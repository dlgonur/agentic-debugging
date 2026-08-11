"""Unit tests for the R2 staged bridge."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.events.schema import Observation, ObservationStatus
from experiments.debugger_interaction_v2_r2 import bridge
from experiments.debugger_interaction_v2_r2.bridge import (
    BridgeParseError,
    BridgeRejection,
    DebuggerContext,
    DebuggerLifecycle,
    R2Stage,
    breakpoint_eligible_lines,
    parse,
    render_prompt,
    visible_commands_r2,
)

TASK_ID = "curated-off-by-one-002"
FIXTURE = REPO_ROOT / "agentic_debugger" / "datasets" / "curated" / TASK_ID
SOURCE = (FIXTURE / "recent_window.py").read_text(encoding="utf-8")


def _stack_obs(generation: int = 1):
    return Observation(
        observation_id="obs-stack", action_id="act-1", run_id="r", task_id="t",
        name="get_stack_summary", status=ObservationStatus.OK,
        payload={
            "state": "paused", "script": "recent_window.py",
            "pause_generation": generation,
            "frames": [
                {"frame_id": 0, "script": "recent_window.py", "line": 2,
                 "function": "recent_window", "is_current": True},
                {"frame_id": 1, "script": "recent_window.py", "line": 16,
                 "function": "_demo_runtime_probe", "is_current": False},
            ],
            "total_frames": 2, "truncated": False,
        },
        summary="bounded stack summary collected", truncated=False,
    )


# ---------------------------------------------------------------------------
# R2 staged command visibility
# ---------------------------------------------------------------------------

class TestR2StagedCommands:
    def test_not_started_only_break_failed(self):
        cmds = visible_commands_r2(ControllerState.RUNTIME_EVIDENCE, R2Stage.NOT_STARTED)
        assert cmds == ("break", "failed")

    def test_needs_stack_only_stack_failed(self):
        cmds = visible_commands_r2(ControllerState.RUNTIME_EVIDENCE, R2Stage.PAUSED_NEEDS_STACK)
        assert cmds == ("failed", "stack")

    def test_needs_inspection_only_locals_print_failed(self):
        cmds = visible_commands_r2(ControllerState.RUNTIME_EVIDENCE, R2Stage.PAUSED_NEEDS_INSPECTION)
        assert set(cmds) == {"locals", "print", "failed"}
        assert "stack" not in cmds
        assert "step" not in cmds
        assert "continue" not in cmds

    def test_needs_step_only_step_next_failed(self):
        cmds = visible_commands_r2(ControllerState.RUNTIME_EVIDENCE, R2Stage.PAUSED_NEEDS_STEP)
        assert set(cmds) == {"step", "next", "failed"}
        assert "locals" not in cmds
        assert "stack" not in cmds
        assert "continue" not in cmds

    def test_after_step_needs_stack_only_stack_failed(self):
        cmds = visible_commands_r2(ControllerState.RUNTIME_EVIDENCE, R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK)
        assert cmds == ("failed", "stack")

    def test_ready_only_diagnosis_failed(self):
        cmds = visible_commands_r2(ControllerState.RUNTIME_EVIDENCE, R2Stage.READY_FOR_DIAGNOSIS)
        assert set(cmds) == {"diagnosis", "failed"}
        assert "stack" not in cmds
        assert "continue" not in cmds

    def test_consumed_only_diagnosis_failed(self):
        cmds = visible_commands_r2(ControllerState.RUNTIME_EVIDENCE, R2Stage.CONSUMED_OR_ENDED)
        assert set(cmds) == {"diagnosis", "failed"}

    def test_non_runtime_ignores_stage(self):
        cmds = visible_commands_r2(ControllerState.REPRODUCE, R2Stage.PAUSED_NEEDS_STACK)
        assert "reproduce" in cmds


# ---------------------------------------------------------------------------
# R2 parser staging
# ---------------------------------------------------------------------------

class TestR2ParserStaging:
    def test_not_started_rejects_stack(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("stack", ControllerState.RUNTIME_EVIDENCE, r2_stage=R2Stage.NOT_STARTED)
        assert exc_info.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_not_started_rejects_continue(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("continue", ControllerState.RUNTIME_EVIDENCE, r2_stage=R2Stage.NOT_STARTED)
        assert exc_info.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_not_started_rejects_diagnosis(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("diagnosis some text", ControllerState.RUNTIME_EVIDENCE, r2_stage=R2Stage.NOT_STARTED)
        assert exc_info.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_needs_stack_accepts_stack(self):
        result = parse("stack", ControllerState.RUNTIME_EVIDENCE, r2_stage=R2Stage.PAUSED_NEEDS_STACK)
        assert result.command_token == "stack"

    def test_needs_stack_rejects_continue(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("continue", ControllerState.RUNTIME_EVIDENCE, r2_stage=R2Stage.PAUSED_NEEDS_STACK)
        assert exc_info.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_needs_stack_rejects_step(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("step", ControllerState.RUNTIME_EVIDENCE, r2_stage=R2Stage.PAUSED_NEEDS_STACK)
        assert exc_info.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_needs_inspection_accepts_locals(self):
        obs = _stack_obs()
        result = parse("locals", ControllerState.RUNTIME_EVIDENCE, last_observation=obs, r2_stage=R2Stage.PAUSED_NEEDS_INSPECTION)
        assert result.command_token == "locals"

    def test_needs_inspection_accepts_print(self):
        obs = _stack_obs()
        result = parse("print values", ControllerState.RUNTIME_EVIDENCE, last_observation=obs, r2_stage=R2Stage.PAUSED_NEEDS_INSPECTION)
        assert result.command_token == "print"

    def test_needs_inspection_rejects_stack(self):
        obs = _stack_obs()
        with pytest.raises(BridgeParseError) as exc_info:
            parse("stack", ControllerState.RUNTIME_EVIDENCE, last_observation=obs, r2_stage=R2Stage.PAUSED_NEEDS_INSPECTION)
        assert exc_info.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_needs_inspection_rejects_continue(self):
        obs = _stack_obs()
        with pytest.raises(BridgeParseError) as exc_info:
            parse("continue", ControllerState.RUNTIME_EVIDENCE, last_observation=obs, r2_stage=R2Stage.PAUSED_NEEDS_INSPECTION)
        assert exc_info.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_needs_step_accepts_step(self):
        result = parse("step", ControllerState.RUNTIME_EVIDENCE, r2_stage=R2Stage.PAUSED_NEEDS_STEP)
        assert result.command_token == "step"

    def test_needs_step_accepts_next(self):
        result = parse("next", ControllerState.RUNTIME_EVIDENCE, r2_stage=R2Stage.PAUSED_NEEDS_STEP)
        assert result.command_token == "next"

    def test_needs_step_rejects_locals(self):
        obs = _stack_obs()
        with pytest.raises(BridgeParseError) as exc_info:
            parse("locals", ControllerState.RUNTIME_EVIDENCE, last_observation=obs, r2_stage=R2Stage.PAUSED_NEEDS_STEP)
        assert exc_info.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_after_step_accepts_only_stack(self):
        result = parse("stack", ControllerState.RUNTIME_EVIDENCE, r2_stage=R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK)
        assert result.command_token == "stack"

    def test_after_step_rejects_step(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("step", ControllerState.RUNTIME_EVIDENCE, r2_stage=R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK)
        assert exc_info.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_ready_accepts_diagnosis(self):
        result = parse("diagnosis the bug is off-by-one", ControllerState.RUNTIME_EVIDENCE, r2_stage=R2Stage.READY_FOR_DIAGNOSIS)
        assert result.command_token == "diagnosis"
        assert result.is_diagnosis is True

    def test_ready_rejects_stack(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("stack", ControllerState.RUNTIME_EVIDENCE, r2_stage=R2Stage.READY_FOR_DIAGNOSIS)
        assert exc_info.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_source_never_legal_in_r2(self):
        for stage in R2Stage:
            with pytest.raises(BridgeParseError):
                parse("source recent_window.py 2", ControllerState.RUNTIME_EVIDENCE, r2_stage=stage)

    def test_patch_never_legal_in_r2(self):
        for stage in R2Stage:
            with pytest.raises(BridgeParseError):
                parse("patch", ControllerState.RUNTIME_EVIDENCE, r2_stage=stage)


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

class TestR2PromptRendering:
    def test_needs_stack_shows_next_inspect_stack(self):
        ctx = DebuggerContext(
            script_path="recent_window.py", source_text=SOURCE,
            eligible_lines=breakpoint_eligible_lines(SOURCE),
            r2_stage=R2Stage.PAUSED_NEEDS_STACK,
            paused_line=2, paused_function="recent_window",
        )
        prompt = render_prompt(ControllerState.RUNTIME_EVIDENCE, None, "test task", debugger=ctx)
        assert "inspect stack with 'stack'" in prompt

    def test_needs_inspection_shows_locals_or_print(self):
        ctx = DebuggerContext(
            script_path="recent_window.py", source_text=SOURCE,
            eligible_lines=breakpoint_eligible_lines(SOURCE),
            r2_stage=R2Stage.PAUSED_NEEDS_INSPECTION,
        )
        prompt = render_prompt(ControllerState.RUNTIME_EVIDENCE, None, "test task", debugger=ctx)
        assert "locals" in prompt and "print" in prompt

    def test_needs_step_shows_step_or_next(self):
        ctx = DebuggerContext(
            script_path="recent_window.py", source_text=SOURCE,
            eligible_lines=breakpoint_eligible_lines(SOURCE),
            r2_stage=R2Stage.PAUSED_NEEDS_STEP,
        )
        prompt = render_prompt(ControllerState.RUNTIME_EVIDENCE, None, "test task", debugger=ctx)
        assert "'step' or 'next'" in prompt

    def test_ready_shows_diagnosis(self):
        ctx = DebuggerContext(
            script_path="recent_window.py", source_text=SOURCE,
            eligible_lines=breakpoint_eligible_lines(SOURCE),
            r2_stage=R2Stage.READY_FOR_DIAGNOSIS,
        )
        prompt = render_prompt(ControllerState.RUNTIME_EVIDENCE, None, "test task", debugger=ctx)
        assert "ready for diagnosis" in prompt

    def test_consumed_shows_ended(self):
        ctx = DebuggerContext(
            script_path="recent_window.py", source_text=SOURCE,
            eligible_lines=breakpoint_eligible_lines(SOURCE),
            r2_stage=R2Stage.CONSUMED_OR_ENDED,
        )
        prompt = render_prompt(ControllerState.RUNTIME_EVIDENCE, None, "test task", debugger=ctx)
        assert "PDB session ended" in prompt
