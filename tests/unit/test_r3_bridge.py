"""R3 bridge: diagnosis->PATCH not loop, bounded PATCH checkpoint."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from agentic_debugger.agent.controller_policy import ActionName
from agentic_debugger.agent.state_machine import ControllerState
from experiments.debugger_interaction_v2_r3.bridge import (
    BridgeRejection,
    DebuggerContext,
    R2Stage,
    R3PatchStage,
    breakpoint_eligible_lines,
    parse,
    render_prompt,
    visible_commands_r2,
    visible_commands_r3_patch,
    SYSTEM_PROMPT,
)
from agentic_debugger.events.schema import Observation, ObservationStatus


def _make_stack_obs():
    return Observation(
        observation_id="obs-0001",
        action_id="action-00000000",
        run_id="r", task_id="curated-off-by-one-002",
        name="get_stack_summary", status=ObservationStatus.OK,
        payload={"frames": [{"frame_id": 0, "function": "recent_window", "line": 2, "script": "recent_window.py", "is_current": True}], "pause_generation": 1},
        summary="ok", truncated=False,
    )


class TestR3DiagnosisToPatch:
    def test_diagnosis_in_ready_transitions_to_patch(self):
        result = parse("diagnosis off-by-one boundary", ControllerState.RUNTIME_EVIDENCE, _make_stack_obs(), r2_stage=R2Stage.READY_FOR_DIAGNOSIS)
        assert result.is_diagnosis is True
        assert result.directive.target_state is ControllerState.PATCH
        assert result.diagnosis_text == "off-by-one boundary"

    def test_diagnosis_not_self_transition(self):
        result = parse("diagnosis text", ControllerState.RUNTIME_EVIDENCE, _make_stack_obs(), r2_stage=R2Stage.READY_FOR_DIAGNOSIS)
        assert result.directive.target_state is not ControllerState.RUNTIME_EVIDENCE

    def test_diagnosis_before_ready_rejected(self):
        with pytest.raises(Exception) as exc:
            parse("diagnosis premature", ControllerState.RUNTIME_EVIDENCE, _make_stack_obs(), r2_stage=R2Stage.PAUSED_NEEDS_STACK)
        assert exc.value.category in (BridgeRejection.COMMAND_NOT_IN_LIFECYCLE, BridgeRejection.COMMAND_NOT_IN_STATE)

    def test_diagnosis_in_patch_state_rejected(self):
        with pytest.raises(Exception) as exc:
            parse("diagnosis nonsense", ControllerState.PATCH, None, r2_stage=None)
        assert exc.value.category == BridgeRejection.COMMAND_NOT_IN_STATE

    def test_patch_alias_in_ready_rejected(self):
        # patch as alias in RuntimeEvidence READY should be masked by stage mask
        with pytest.raises(Exception) as exc:
            parse("patch", ControllerState.RUNTIME_EVIDENCE, _make_stack_obs(), r2_stage=R2Stage.READY_FOR_DIAGNOSIS)
        assert exc.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_patch_in_patch_requires_diff(self):
        with pytest.raises(Exception) as exc:
            parse("patch", ControllerState.PATCH, None)
        assert exc.value.category == BridgeRejection.INVALID_PATCH

    def test_patch_in_patch_with_diff_accepted(self):
        diff = "--- a/recent_window.py\n+++ b/recent_window.py\n@@ -1 +1 @@\n-old\n+new\n"
        result = parse(f"patch\n{diff}", ControllerState.PATCH, None)
        assert result.command_token == "patch"
        assert result.directive.name == ActionName.APPLY_PATCH

    def test_patch_checkpoint_no_syntax(self):
        with pytest.raises(Exception) as exc:
            parse("syntax", ControllerState.PATCH, None)
        assert exc.value.category == BridgeRejection.COMMAND_NOT_IN_STATE

    def test_patch_checkpoint_no_validate(self):
        with pytest.raises(Exception) as exc:
            parse("validate", ControllerState.PATCH, None)
        assert exc.value.category == BridgeRejection.COMMAND_NOT_IN_STATE

    def test_patch_checkpoint_no_understand(self):
        with pytest.raises(Exception) as exc:
            parse("understand", ControllerState.PATCH, None)
        assert exc.value.category == BridgeRejection.COMMAND_NOT_IN_STATE

    # ---- R3.1: forced first repair + retry surface ----

    def test_first_patch_turn_only_patch(self):
        cmds = visible_commands_r3_patch(R3PatchStage.NEEDS_FIRST_REPAIR)
        assert cmds == ("patch",)

    def test_failed_rejected_on_first_patch_turn(self):
        with pytest.raises(Exception) as exc:
            parse("failed", ControllerState.PATCH, None, patch_stage=R3PatchStage.NEEDS_FIRST_REPAIR)
        assert exc.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE

    def test_retry_turn_exposes_patch_failed(self):
        cmds = visible_commands_r3_patch(R3PatchStage.RETRY)
        assert set(cmds) == {"patch", "failed"}

    def test_first_genuine_malformed_patch_advances_to_retry(self):
        # bridge rejects malformed diff; retry surface (patch|failed) is what
        # the next PATCH turn exposes (stage advancement asserted in adapter test)
        with pytest.raises(Exception) as exc:
            parse("patch\n--- a/recent_window.py\nbad", ControllerState.PATCH, None, patch_stage=R3PatchStage.NEEDS_FIRST_REPAIR)
        assert exc.value.category == BridgeRejection.INVALID_PATCH

    def test_valid_patch_accepted_in_first_turn(self):
        diff = "--- a/recent_window.py\n+++ b/recent_window.py\n@@ -1 +1 @@\n-old\n+new\n"
        result = parse(f"patch\n{diff}", ControllerState.PATCH, None, patch_stage=R3PatchStage.NEEDS_FIRST_REPAIR)
        assert result.command_token == "patch"
        assert result.directive.name == ActionName.APPLY_PATCH


class TestR3PatchPromptBounded:
    def test_patch_prompt_shows_source_and_checkpoint(self):
        ctx = DebuggerContext(
            script_path="recent_window.py",
            source_text="def recent_window(values,size):\n    return []\n",
            eligible_lines=(2,),
            retained_diagnosis="off-by-one in end_index",
            runtime_slice={"stack_G1": "[get_stack_summary] G1=1", "inspection": "[get_frame_locals] size=4"},
        )
        prompt = render_prompt(ControllerState.PATCH, None, "Task: fix it", debugger=ctx)
        assert "No further debugger commands are available in Patch phase." in prompt
        assert "off-by-one in end_index" in prompt
        assert "recent_window.py" in prompt
        assert "Available commands:" in prompt
        # PATCH must only show patch,failed
        assert "patch" in prompt.lower()
        assert "failed" in prompt.lower()
        # Must NOT show debugger step/stack
        assert "inspect stack" not in prompt

    def test_patch_prompt_excludes_oracle_strings(self):
        ctx = DebuggerContext(
            script_path="recent_window.py",
            source_text="def recent_window(values,size): return []",
            eligible_lines=(1,),
            retained_diagnosis="diagnosis text",
            runtime_slice={"stack_G1": "G1 content"},
        )
        prompt = render_prompt(ControllerState.PATCH, None, "Task", debugger=ctx)
        for forbidden in ("root_cause_summary", "target_symbols", "runtime_evidence_hint", "inspect_expressions", "reference_repair"):
            assert forbidden not in prompt
            assert forbidden not in SYSTEM_PROMPT

    def test_patch_only_advertises_patch_failed(self):
        cmds = visible_commands_r2(ControllerState.PATCH, R2Stage.CONSUMED_OR_ENDED)
        # R3 PATCH uses bounded set regardless of stage
        from experiments.debugger_interaction_v2_r3.bridge import _R3_PATCH_COMMANDS
        assert set(cmds) == set(_R3_PATCH_COMMANDS) or set(cmds) == {"failed", "patch"}

    def test_patch_prompt_includes_diff_affordance(self):
        ctx = DebuggerContext(
            script_path="recent_window.py",
            source_text="def recent_window(values,size): return []",
            eligible_lines=(1,),
            retained_diagnosis="diag",
            runtime_slice={"stack_G1": "G1"},
        )
        prompt = render_prompt(ControllerState.PATCH, None, "Task", debugger=ctx, patch_stage=R3PatchStage.NEEDS_FIRST_REPAIR)
        assert "Required response now:" in prompt
        assert "--- a/recent_window.py" in prompt
        assert "Produce your best minimal repair" in prompt
        # Oracle still absent even with affordance
        for forbidden in ("root_cause_summary", "target_symbols", "runtime_evidence_hint", "inspect_expressions", "reference_repair"):
            assert forbidden not in prompt
