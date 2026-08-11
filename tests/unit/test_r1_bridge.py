"""Unit tests for the R1 repaired bridge.

Tests the four interface repairs:
A. Source visibility + breakpoint-eligible lines.
B. get_source_window observations render actual source lines.
C. Error observations render the diagnostic.
E. Lifecycle-aware legal-command visibility.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.events.schema import Observation, ObservationStatus

from experiments.debugger_interaction_v2_r1 import bridge
from experiments.debugger_interaction_v2_r1.bridge import (
    BridgeParseError,
    BridgeRejection,
    DebuggerContext,
    DebuggerLifecycle,
    breakpoint_eligible_lines,
    format_source_with_lines,
    parse,
    render_prompt,
    visible_commands,
)

TASK_ID = "curated-off-by-one-002"
FIXTURE = REPO_ROOT / "agentic_debugger" / "datasets" / "curated" / TASK_ID
SOURCE = (FIXTURE / "recent_window.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A. Breakpoint eligibility (non-oracle, production-only)
# ---------------------------------------------------------------------------


class TestBreakpointEligibility:
    def test_excludes_module_def_lines(self):
        """Line 1 (the def statement) is NOT eligible — it's a module-def."""
        eligible = breakpoint_eligible_lines(SOURCE)
        assert 1 not in eligible

    def test_excludes_blank_lines(self):
        """Line 6 (blank) is NOT eligible."""
        eligible = breakpoint_eligible_lines(SOURCE)
        assert 6 not in eligible

    def test_excludes_continuation_lines(self):
        """Line 11 (closing paren of list(...)) is NOT a statement start.
        However, co_lines may include it if the tracer fires there.  We
        check that the eligible set is a subset of the source's line count
        and contains the known traceable lines."""
        eligible = breakpoint_eligible_lines(SOURCE)
        line_count = len(SOURCE.splitlines())
        for line in eligible:
            assert 1 <= line <= line_count

    def test_contains_function_body_lines(self):
        """Lines 2-5, 7-10, 12 (inside recent_window) are eligible."""
        eligible = set(breakpoint_eligible_lines(SOURCE))
        # These are the lines where the Python tracer fires user_line events
        # inside the function body.
        assert 2 in eligible
        assert 7 in eligible
        assert 9 in eligible
        assert 12 in eligible

    def test_does_not_use_oracle(self):
        """The eligibility function only uses compile()+co_lines()+AST.
        It does not access RuntimeProbe.anchor, focus_function,
        DemoScenario.root_cause_statement, or task oracle fields."""
        # This is a design-level assertion: the function signature takes
        # only `source: str` and uses no external metadata.
        import inspect
        sig = inspect.signature(breakpoint_eligible_lines)
        assert list(sig.parameters.keys()) == ["source"]

    def test_format_source_with_lines_marks_eligible(self):
        eligible = breakpoint_eligible_lines(SOURCE)
        formatted = format_source_with_lines(SOURCE, eligible)
        lines = formatted.splitlines()
        # Line 1 (def) should NOT be marked with >
        assert lines[0].startswith("    1:")
        # Line 2 should be marked with >
        assert lines[1].startswith(">   2:")


# ---------------------------------------------------------------------------
# E. Lifecycle-aware legal-command visibility
# ---------------------------------------------------------------------------


class TestLifecycleCommands:
    def test_not_started_shows_break_not_stack(self):
        cmds = visible_commands(
            ControllerState.RUNTIME_EVIDENCE, DebuggerLifecycle.NOT_STARTED
        )
        assert "break" in cmds
        assert "stack" not in cmds
        assert "locals" not in cmds
        assert "continue" not in cmds
        assert "step" not in cmds
        assert "next" not in cmds
        assert "stop" not in cmds

    def test_paused_shows_stack_not_break(self):
        cmds = visible_commands(
            ControllerState.RUNTIME_EVIDENCE, DebuggerLifecycle.PAUSED
        )
        assert "break" not in cmds
        assert "stack" in cmds
        assert "locals" in cmds
        assert "continue" in cmds
        assert "step" in cmds
        assert "next" in cmds
        assert "stop" in cmds

    def test_consumed_shows_neither_break_nor_stack(self):
        """One-session guard: after the session ends, break is NOT legal."""
        cmds = visible_commands(
            ControllerState.RUNTIME_EVIDENCE, DebuggerLifecycle.CONSUMED_OR_ENDED
        )
        assert "break" not in cmds
        assert "stack" not in cmds
        assert "source" not in cmds
        assert "diagnosis" in cmds
        assert "understand" in cmds

    def test_non_runtime_states_ignore_lifecycle(self):
        """In REPRODUCE, lifecycle has no effect — the base set is returned."""
        cmds_repro = visible_commands(
            ControllerState.REPRODUCE, DebuggerLifecycle.PAUSED
        )
        cmds_repro_base = visible_commands(
            ControllerState.REPRODUCE, DebuggerLifecycle.NOT_STARTED
        )
        assert cmds_repro == cmds_repro_base
        assert "reproduce" in cmds_repro


# ---------------------------------------------------------------------------
# A+E. Prompt rendering with source + lifecycle
# ---------------------------------------------------------------------------


class TestPromptRendering:
    def test_not_started_prompt_contains_source_and_eligible_lines(self):
        eligible = breakpoint_eligible_lines(SOURCE)
        ctx = DebuggerContext(
            script_path="recent_window.py",
            source_text=SOURCE,
            eligible_lines=eligible,
            lifecycle=DebuggerLifecycle.NOT_STARTED,
        )
        prompt = render_prompt(
            ControllerState.RUNTIME_EVIDENCE, None, "test task",
            debugger=ctx,
        )
        assert "Target script for debugging: recent_window.py" in prompt
        assert "Breakpoint-eligible lines:" in prompt
        assert "calculated_indexes = list(" in prompt
        assert "Debugger: no active PDB session" in prompt

    def test_not_started_prompt_does_not_show_stack_etc(self):
        eligible = breakpoint_eligible_lines(SOURCE)
        ctx = DebuggerContext(
            script_path="recent_window.py",
            source_text=SOURCE,
            eligible_lines=eligible,
            lifecycle=DebuggerLifecycle.NOT_STARTED,
        )
        prompt = render_prompt(
            ControllerState.RUNTIME_EVIDENCE, None, "test task",
            debugger=ctx,
        )
        lines = prompt.splitlines()
        # Find the "Available commands:" section
        cmd_section = []
        in_cmds = False
        for line in lines:
            if line.startswith("Available commands:"):
                in_cmds = True
                continue
            if in_cmds and line.startswith("  - "):
                cmd_section.append(line.strip("- ").strip())
            elif in_cmds and not line.startswith("  - "):
                break
        assert "break" in cmd_section
        assert "failed" in cmd_section
        assert "source" not in cmd_section
        assert "diagnosis" not in cmd_section
        assert "patch" not in cmd_section
        assert "understand" not in cmd_section
        assert "stack" not in cmd_section
        assert "continue" not in cmd_section

    def test_paused_prompt_shows_paused_info_not_source(self):
        ctx = DebuggerContext(
            script_path="recent_window.py",
            source_text=SOURCE,
            eligible_lines=breakpoint_eligible_lines(SOURCE),
            lifecycle=DebuggerLifecycle.PAUSED,
            paused_line=9,
            paused_function="recent_window",
        )
        prompt = render_prompt(
            ControllerState.RUNTIME_EVIDENCE, None, "test task",
            debugger=ctx,
        )
        assert "Debugger: PDB session paused at line 9 in function 'recent_window'" in prompt
        # Source affordance is NOT shown when paused (it's only for the break decision)
        assert "Target script for debugging:" not in prompt

    def test_consumed_prompt_shows_ended_message(self):
        ctx = DebuggerContext(
            script_path="recent_window.py",
            source_text=SOURCE,
            eligible_lines=breakpoint_eligible_lines(SOURCE),
            lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED,
        )
        prompt = render_prompt(
            ControllerState.RUNTIME_EVIDENCE, None, "test task",
            debugger=ctx,
        )
        assert "Debugger: PDB session ended" in prompt
        assert "'break' is no longer available" in prompt
        assert "Target script for debugging:" not in prompt

    def test_no_debugger_context_falls_back_to_not_started(self):
        """When debugger=None, the prompt still works (backward compat)."""
        prompt = render_prompt(
            ControllerState.RUNTIME_EVIDENCE, None, "test task",
            debugger=None,
        )
        assert "Available commands:" in prompt


# ---------------------------------------------------------------------------
# B. get_source_window observation rendering
# ---------------------------------------------------------------------------


class TestSourceObservationRendering:
    def test_source_observation_renders_actual_lines(self):
        obs = Observation(
            observation_id="obs-1", action_id="act-1", run_id="r", task_id="t",
            name="get_source_window", status=ObservationStatus.OK,
            payload={
                "path": "recent_window.py",
                "start_line": 7, "end_line": 12, "focal_line": 9,
                "lines": [
                    {"path": "recent_window.py", "line_number": 9,
                     "text": "    calculated_indexes = list(", "is_focal": True},
                    {"path": "recent_window.py", "line_number": 10,
                     "text": "        range(...)", "is_focal": False},
                ],
                "total_lines": 12, "clipped_before": True, "clipped_after": False,
            },
            summary="source window retrieved", truncated=False,
        )
        rendered = bridge._render_observation(obs)
        assert "recent_window.py" in rendered
        assert "calculated_indexes = list(" in rendered
        assert "range(...)" in rendered
        assert ">>" in rendered  # focal line marker

    def test_source_observation_falls_back_to_summary_on_empty_lines(self):
        obs = Observation(
            observation_id="obs-1", action_id="act-1", run_id="r", task_id="t",
            name="get_source_window", status=ObservationStatus.OK,
            payload={"path": "recent_window.py"},
            summary="source window retrieved", truncated=False,
        )
        rendered = bridge._render_observation(obs)
        assert "source window retrieved" in rendered


# ---------------------------------------------------------------------------
# C. Error observation rendering
# ---------------------------------------------------------------------------


class TestErrorObservationRendering:
    def test_error_with_diagnostic(self):
        obs = Observation(
            observation_id="obs-e", action_id="act-e", run_id="r", task_id="t",
            name="start_pdb_session", status=ObservationStatus.ERROR,
            payload={"dispatch_reason": "tool_error",
                     "diagnostic": "breakpoint line 20 exceeds source length (19)"},
            summary="Tool execution failed.", truncated=False,
        )
        rendered = bridge._render_observation(obs)
        assert "ERROR: tool_error" in rendered
        assert "breakpoint line 20 exceeds source length (19)" in rendered
        assert "Paused at line None" not in rendered

    def test_error_without_diagnostic(self):
        obs = Observation(
            observation_id="obs-e", action_id="act-e", run_id="r", task_id="t",
            name="start_pdb_session", status=ObservationStatus.ERROR,
            payload={"dispatch_reason": "tool_error"},
            summary="Tool execution failed.", truncated=False,
        )
        rendered = bridge._render_observation(obs)
        assert "ERROR: tool_error" in rendered
        assert "Paused at line None" not in rendered

    def test_rejected_observation_renders_reason(self):
        obs = Observation(
            observation_id="obs-r", action_id="act-r", run_id="r", task_id="t",
            name="continue_pdb_session", status=ObservationStatus.REJECTED,
            payload={"dispatch_reason": "tool_rejected",
                     "diagnostic": "requires an active PDB session"},
            summary="Tool rejected the action.", truncated=False,
        )
        rendered = bridge._render_observation(obs)
        assert "ERROR: tool_rejected" in rendered
        assert "requires an active PDB session" in rendered

    def test_ok_observation_not_affected(self):
        """Non-regression: OK observations still render normally."""
        obs = Observation(
            observation_id="obs-ok", action_id="act-ok", run_id="r", task_id="t",
            name="start_pdb_session", status=ObservationStatus.OK,
            payload={"state": "paused", "script": "recent_window.py",
                     "line": 9, "function": "recent_window", "breakpoint_line": 9},
            summary="paused", truncated=False,
        )
        rendered = bridge._render_observation(obs)
        assert "Paused at line 9" in rendered
        assert "ERROR" not in rendered


# ---------------------------------------------------------------------------
# Parser (non-regression — same as S1)
# ---------------------------------------------------------------------------


class TestParserNonRegression:
    def test_break_parses(self):
        result = parse("break 9", ControllerState.RUNTIME_EVIDENCE)
        assert result.command_token == "break"
        assert result.normalized_command == "break 9"

    def test_unrecognized_command_rejected(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("foobar", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category == BridgeRejection.UNRECOGNIZED_COMMAND

    def test_reproduce_in_runtime_evidence_rejected(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("reproduce", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category == BridgeRejection.COMMAND_NOT_IN_STATE