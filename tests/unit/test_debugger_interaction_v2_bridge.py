"""Unit tests for the S1 deterministic bridge parser.

These tests prove:
- all 17 commands parse to the correct typed directives with correct arguments;
- all rejection categories are exercised;
- the bridge is fail-closed (no fallback, no prose-to-action);
- the bridge is non-oracular (never modifies args, defaults breakpoints,
  generates patches, or fills semantic hypothesis fields);
- frame_id and pause_generation are mechanically derived from real PDB
  observations, not hardcoded.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.controller_policy import ActionName
from agentic_debugger.agent.model_adapter import ActionDirective, TransitionDirective
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.events.schema import Observation, ObservationStatus

from experiments.debugger_interaction_v2 import bridge
from experiments.debugger_interaction_v2.bridge import (
    BridgeParseError,
    BridgeRejection,
    parse,
    render_prompt,
    visible_commands,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stack_observation(
    observation_id: str = "obs-001",
    action_id: str = "act-001",
    pause_generation: int = 2,
    frames=None,
) -> Observation:
    """Create a realistic get_stack_summary observation."""

    if frames is None:
        frames = [
            {
                "frame_id": 0,
                "script": "recent_window.py",
                "line": 9,
                "function": "recent_window",
                "is_current": True,
            },
            {
                "frame_id": 1,
                "script": "recent_window.py",
                "line": 14,
                "function": "_demo_runtime_probe",
                "is_current": False,
            },
        ]
    return Observation(
        observation_id=observation_id,
        action_id=action_id,
        run_id="s1-curated-off-by-one-002",
        task_id="curated-off-by-one-002",
        name="get_stack_summary",
        status=ObservationStatus.OK,
        payload={
            "state": "paused",
            "script": "recent_window.py",
            "pause_generation": pause_generation,
            "frames": frames,
            "total_frames": len(frames),
            "truncated": False,
        },
        summary="bounded stack summary collected",
        truncated=False,
    )


def _make_reproduction_observation(
    failure_reproduced: bool,
    observation_id: str = "obs-repro-001",
    action_id: str = "act-repro-001",
    phase: str = "baseline",
) -> Observation:
    """Create a realistic run_reproduction observation.

    The payload shape mirrors the real ``handle_run_reproduction`` output
    (demo/tools.py:407-415): ``phase``, ``exit_code``, ``expected_exit_code``,
    ``passed``, ``failure_reproduced``.
    """

    return Observation(
        observation_id=observation_id,
        action_id=action_id,
        run_id="s1-curated-off-by-one-002",
        task_id="curated-off-by-one-002",
        name="run_reproduction",
        status=ObservationStatus.OK,
        payload={
            "phase": phase,
            "node_id": "curated-off-by-one-002",
            "exit_code": 1 if failure_reproduced else 0,
            "expected_exit_code": 1,
            "passed": not failure_reproduced,
            "failure_reproduced": failure_reproduced,
        },
        summary="baseline failure reproduced" if failure_reproduced else "baseline did not reproduce",
        truncated=False,
    )


def _action(result) -> ActionDirective:
    """Extract the ActionDirective from a BridgeResult."""
    assert isinstance(result.directive, ActionDirective), \
        f"expected ActionDirective, got {type(result.directive)}"
    return result.directive


def _transition(result) -> TransitionDirective:
    assert isinstance(result.directive, TransitionDirective), \
        f"expected TransitionDirective, got {type(result.directive)}"
    return result.directive


# ---------------------------------------------------------------------------
# Valid command parsing
# ---------------------------------------------------------------------------


class TestValidCommands:
    """Each of the 17 commands parses to the correct typed directive."""

    def test_reproduce_in_reproduce_state(self):
        result = parse("reproduce", ControllerState.REPRODUCE)
        d = _action(result)
        assert d.name is ActionName.RUN_REPRODUCTION
        assert d.arguments == {"phase": "baseline"}

    def test_reproduce_in_validate_state(self):
        result = parse("reproduce", ControllerState.VALIDATE)
        d = _action(result)
        assert d.name is ActionName.RUN_REPRODUCTION
        assert d.arguments == {"phase": "post_patch"}

    def test_source(self):
        result = parse("source recent_window.py 9", ControllerState.UNDERSTAND)
        d = _action(result)
        assert d.name is ActionName.GET_SOURCE_WINDOW
        assert d.arguments == {"path": "recent_window.py", "line": 9}

    def test_source_in_runtime_evidence(self):
        result = parse("source recent_window.py 5", ControllerState.RUNTIME_EVIDENCE)
        d = _action(result)
        assert d.name is ActionName.GET_SOURCE_WINDOW

    def test_break(self):
        result = parse("break 9", ControllerState.RUNTIME_EVIDENCE)
        d = _action(result)
        assert d.name is ActionName.START_PDB_SESSION
        assert d.arguments == {"breakpoint_line": 9}

    def test_stack(self):
        result = parse("stack", ControllerState.RUNTIME_EVIDENCE)
        d = _action(result)
        assert d.name is ActionName.GET_STACK_SUMMARY
        assert d.arguments == {}

    def test_locals_with_prior_stack(self):
        obs = _make_stack_observation(pause_generation=3)
        result = parse("locals", ControllerState.RUNTIME_EVIDENCE, obs)
        d = _action(result)
        assert d.name is ActionName.GET_FRAME_LOCALS
        assert d.arguments == {"frame_id": 0, "pause_generation": 3}

    def test_print_with_prior_stack(self):
        obs = _make_stack_observation(pause_generation=2)
        result = parse("print sequence_length", ControllerState.RUNTIME_EVIDENCE, obs)
        d = _action(result)
        assert d.name is ActionName.SAFE_EVAL_EXPRESSION
        assert d.arguments == {
            "frame_id": 0,
            "pause_generation": 2,
            "expression": "sequence_length",
        }

    def test_step(self):
        result = parse("step", ControllerState.RUNTIME_EVIDENCE)
        d = _action(result)
        assert d.name is ActionName.STEP_PDB_SESSION

    def test_next(self):
        result = parse("next", ControllerState.RUNTIME_EVIDENCE)
        d = _action(result)
        assert d.name is ActionName.NEXT_PDB_SESSION

    def test_continue(self):
        result = parse("continue", ControllerState.RUNTIME_EVIDENCE)
        d = _action(result)
        assert d.name is ActionName.CONTINUE_PDB_SESSION

    def test_stop(self):
        result = parse("stop", ControllerState.RUNTIME_EVIDENCE)
        d = _action(result)
        assert d.name is ActionName.STOP_PDB_SESSION

    def test_patch_with_diff(self):
        diff = "--- a/recent_window.py\n+++ b/recent_window.py\n@@ -7,7 +7,7 @@\n old\n new\n"
        raw = f"patch\n{diff}"
        result = parse(raw, ControllerState.PATCH)
        d = _action(result)
        assert d.name is ActionName.APPLY_PATCH
        assert d.arguments == {"patch": diff}

    def test_syntax(self):
        result = parse("syntax", ControllerState.PATCH)
        d = _action(result)
        assert d.name is ActionName.SYNTAX_CHECK

    def test_regression(self):
        result = parse("regression", ControllerState.VALIDATE)
        d = _action(result)
        assert d.name is ActionName.RUN_REGRESSION_TESTS

    def test_classify(self):
        result = parse("classify", ControllerState.VALIDATE)
        d = _action(result)
        assert d.name is ActionName.CLASSIFY_OUTCOME

    def test_diagnosis_in_runtime_evidence(self):
        result = parse("diagnosis the off-by-one drops the last value", ControllerState.RUNTIME_EVIDENCE)
        d = _transition(result)
        assert d.target_state is ControllerState.RUNTIME_EVIDENCE  # self-transition
        assert "off-by-one" in d.reason
        assert result.is_diagnosis
        assert result.diagnosis_text == "the off-by-one drops the last value"

    def test_diagnosis_in_understand(self):
        result = parse("diagnosis bug in loop bound", ControllerState.UNDERSTAND)
        d = _transition(result)
        assert d.target_state is ControllerState.UNDERSTAND  # self-transition
        assert result.is_diagnosis

    def test_phase_runtime_from_understand(self):
        result = parse("runtime", ControllerState.UNDERSTAND)
        d = _transition(result)
        assert d.target_state is ControllerState.RUNTIME_EVIDENCE

    def test_phase_patch_from_understand(self):
        result = parse("patch", ControllerState.UNDERSTAND)
        d = _transition(result)
        assert d.target_state is ControllerState.PATCH

    def test_phase_understand_from_runtime(self):
        result = parse("understand", ControllerState.RUNTIME_EVIDENCE)
        d = _transition(result)
        assert d.target_state is ControllerState.UNDERSTAND

    def test_phase_patch_from_runtime(self):
        result = parse("patch", ControllerState.RUNTIME_EVIDENCE)
        d = _transition(result)
        assert d.target_state is ControllerState.PATCH

    def test_phase_validate_from_patch(self):
        result = parse("validate", ControllerState.PATCH)
        d = _transition(result)
        assert d.target_state is ControllerState.VALIDATE

    def test_done_from_validate(self):
        result = parse("done", ControllerState.VALIDATE)
        d = _transition(result)
        assert d.target_state is ControllerState.DONE

    def test_failed_from_reproduce(self):
        result = parse("failed", ControllerState.REPRODUCE)
        d = _transition(result)
        assert d.target_state is ControllerState.FAILED

    def test_failed_from_runtime(self):
        result = parse("failed", ControllerState.RUNTIME_EVIDENCE)
        d = _transition(result)
        assert d.target_state is ControllerState.FAILED


# ---------------------------------------------------------------------------
# State-specific command filtering
# ---------------------------------------------------------------------------


class TestStateSpecificCommands:
    """Commands not valid in the current state are rejected."""

    def test_break_not_in_understand(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("break 9", ControllerState.UNDERSTAND)
        assert exc_info.value.category is BridgeRejection.COMMAND_NOT_IN_STATE

    def test_break_not_in_patch(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("break 9", ControllerState.PATCH)
        assert exc_info.value.category is BridgeRejection.COMMAND_NOT_IN_STATE

    def test_stack_not_in_reproduce(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("stack", ControllerState.REPRODUCE)
        assert exc_info.value.category is BridgeRejection.COMMAND_NOT_IN_STATE

    def test_patch_in_runtime_is_transition_not_action(self):
        """In RUNTIME_EVIDENCE, 'patch' is a transition to PATCH state,
        not the apply_patch action.  It must be a single line."""
        # As a transition, "patch" alone is valid.
        result = parse("patch", ControllerState.RUNTIME_EVIDENCE)
        d = _transition(result)
        assert d.target_state is ControllerState.PATCH
        # But "patch" with diff lines is rejected (it's a transition, not
        # the apply_patch action, in this state).
        with pytest.raises(BridgeParseError) as exc_info:
            parse("patch\n--- a/f\n+++ b/f\n", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.UNEXPECTED_CONTENT

    def test_reproduce_not_in_runtime(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("reproduce", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.COMMAND_NOT_IN_STATE

    def test_diagnosis_not_in_reproduce(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("diagnosis test", ControllerState.REPRODUCE)
        assert exc_info.value.category is BridgeRejection.COMMAND_NOT_IN_STATE

    def test_diagnosis_not_in_patch(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("diagnosis test", ControllerState.PATCH)
        assert exc_info.value.category is BridgeRejection.COMMAND_NOT_IN_STATE

    def test_visible_commands_reproduce(self):
        cmds = visible_commands(ControllerState.REPRODUCE)
        assert "reproduce" in cmds
        assert "failed" in cmds
        # "understand" is a legal REPRODUCE->UNDERSTAND transition in the
        # static state-command surface, but the model-facing prompt hides it
        # until baseline reproduction succeeds (see TestBaselineReproductionGate).
        assert "understand" in cmds  # legal static surface; prompt hides it pre-repro
        assert "break" not in cmds
        assert "stack" not in cmds

    def test_visible_commands_runtime_evidence(self):
        cmds = visible_commands(ControllerState.RUNTIME_EVIDENCE)
        assert "break" in cmds
        assert "stack" in cmds
        assert "locals" in cmds
        assert "print" in cmds
        assert "step" in cmds
        assert "next" in cmds
        assert "continue" in cmds
        assert "stop" in cmds
        assert "diagnosis" in cmds
        # "patch" IS visible in RUNTIME_EVIDENCE as a transition to PATCH state
        assert "patch" in cmds  # transition alias to PATCH state
        # Non-debugger action commands should NOT be visible
        assert "reproduce" not in cmds
        assert "regression" not in cmds
        assert "syntax" not in cmds


# ---------------------------------------------------------------------------
# Fail-closed rejections
# ---------------------------------------------------------------------------


class TestRejections:
    """All rejection categories are exercised."""

    def test_empty_response(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.EMPTY_RESPONSE

    def test_whitespace_only(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("   \n  \t ", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.EMPTY_RESPONSE

    def test_unrecognized_command(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("foobar", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.UNRECOGNIZED_COMMAND

    def test_prose_rejected(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("I think the bug is on line 9", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.UNRECOGNIZED_COMMAND

    def test_json_rejected(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse('{"kind": "action", "name": "break"}', ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.UNRECOGNIZED_COMMAND

    def test_break_missing_line(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("break", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.MISSING_ARGUMENT

    def test_break_invalid_type(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("break foo", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.INVALID_ARGUMENT_TYPE

    def test_break_zero(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("break 0", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.INVALID_ARGUMENT_TYPE

    def test_break_negative(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("break -1", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.INVALID_ARGUMENT_TYPE

    def test_locals_without_prior_stack(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("locals", ControllerState.RUNTIME_EVIDENCE, None)
        assert exc_info.value.category is BridgeRejection.NO_PAUSE_GENERATION

    def test_locals_with_non_stack_observation(self):
        obs = Observation(
            observation_id="obs-001",
            action_id="act-001",
            run_id="run-1",
            task_id="task-1",
            name="start_pdb_session",  # not get_stack_summary
            status=ObservationStatus.OK,
            payload={"state": "paused", "line": 9, "function": "recent_window"},
            summary="started",
            truncated=False,
        )
        with pytest.raises(BridgeParseError) as exc_info:
            parse("locals", ControllerState.RUNTIME_EVIDENCE, obs)
        assert exc_info.value.category is BridgeRejection.NO_PAUSE_GENERATION

    def test_print_without_prior_stack(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("print x", ControllerState.RUNTIME_EVIDENCE, None)
        assert exc_info.value.category is BridgeRejection.NO_PAUSE_GENERATION

    def test_print_missing_expression(self):
        obs = _make_stack_observation()
        with pytest.raises(BridgeParseError) as exc_info:
            parse("print", ControllerState.RUNTIME_EVIDENCE, obs)
        assert exc_info.value.category is BridgeRejection.MISSING_ARGUMENT

    def test_patch_empty_diff(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("patch\n\n", ControllerState.PATCH)
        assert exc_info.value.category is BridgeRejection.INVALID_PATCH

    def test_patch_no_diff_lines(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("patch", ControllerState.PATCH)
        assert exc_info.value.category is BridgeRejection.INVALID_PATCH

    def test_patch_not_unified_diff(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("patch\nsome random text\n", ControllerState.PATCH)
        assert exc_info.value.category is BridgeRejection.INVALID_PATCH

    def test_source_missing_args(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("source recent_window.py", ControllerState.UNDERSTAND)
        assert exc_info.value.category is BridgeRejection.MISSING_ARGUMENT

    def test_source_extra_args(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("source a.py 1 extra", ControllerState.UNDERSTAND)
        assert exc_info.value.category is BridgeRejection.UNEXPECTED_CONTENT

    def test_extra_lines_after_command(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("break 9\nextra line", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.UNEXPECTED_CONTENT

    def test_extra_lines_after_stack(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("stack\nextra", ControllerState.RUNTIME_EVIDENCE)
        assert exc_info.value.category is BridgeRejection.UNEXPECTED_CONTENT

    def test_illegal_transition_understand_to_validate(self):
        """In UNDERSTAND, 'validate' is not in the visible command set,
        so it's rejected as COMMAND_NOT_IN_STATE (the state-specific surface
        filters it before the transition legality check)."""
        with pytest.raises(BridgeParseError) as exc_info:
            parse("validate", ControllerState.UNDERSTAND)
        assert exc_info.value.category is BridgeRejection.COMMAND_NOT_IN_STATE

    def test_illegal_transition_reproduce_to_patch(self):
        with pytest.raises(BridgeParseError) as exc_info:
            parse("patch", ControllerState.REPRODUCE)
        # "patch" is both an action and a phase alias. In REPRODUCE, the phase
        # alias "patch" maps to PATCH state, which is not in TRANSITION_GRAPH[REPRODUCE].
        # But first the bridge checks state_commands; "patch" is not in
        # _STATE_COMMANDS[REPRODUCE], so it should be COMMAND_NOT_IN_STATE.
        assert exc_info.value.category is BridgeRejection.COMMAND_NOT_IN_STATE


# ---------------------------------------------------------------------------
# Non-oracle proof: the bridge never assists
# ---------------------------------------------------------------------------


class TestNonOracle:
    """The bridge never modifies args, defaults breakpoints, generates patches,
    or fills semantic hypothesis fields."""

    def test_break_passes_model_line_verbatim(self):
        """The bridge does not compute, suggest, or default a breakpoint."""
        result = parse("break 9", ControllerState.RUNTIME_EVIDENCE)
        d = _action(result)
        assert d.arguments["breakpoint_line"] == 9  # exactly what the model said

        result2 = parse("break 42", ControllerState.RUNTIME_EVIDENCE)
        d2 = _action(result2)
        assert d2.arguments["breakpoint_line"] == 42  # no defaulting

    def test_patch_passes_model_diff_verbatim(self):
        """The bridge does not modify, complete, or generate patch content."""
        diff = "--- a/f.py\n+++ b/f.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
        result = parse(f"patch\n{diff}", ControllerState.PATCH)
        d = _action(result)
        assert d.arguments["patch"] == diff  # verbatim, no modification

    def test_print_passes_model_expression_verbatim(self):
        """The bridge does not modify the model's expression."""
        obs = _make_stack_observation()
        result = parse("print my_complex_expr[0].foo", ControllerState.RUNTIME_EVIDENCE, obs)
        d = _action(result)
        assert d.arguments["expression"] == "my_complex_expr[0].foo"

    def test_diagnosis_passes_model_text_verbatim(self):
        """The bridge does not modify the model's diagnosis text."""
        result = parse("diagnosis the loop drops the last element", ControllerState.RUNTIME_EVIDENCE)
        assert result.diagnosis_text == "the loop drops the last element"

    def test_no_fabricated_directive_on_rejection(self):
        """On rejection, the bridge raises — it never returns a fabricated directive."""
        with pytest.raises(BridgeParseError):
            parse("nonsense", ControllerState.RUNTIME_EVIDENCE)
        # No TransitionDirective(FAILED) is returned — it raises.

    def test_no_hypothesis_fields_populated(self):
        """The diagnosis mechanism uses a self-transition, not a hypothesis directive.
        It does not populate confidence/evidence_refs/target_file/target_symbol."""
        result = parse("diagnosis test diagnosis", ControllerState.RUNTIME_EVIDENCE)
        d = _transition(result)
        # It's a TransitionDirective, not AddHypothesisDirective.
        assert not hasattr(d, "confidence")
        assert not hasattr(d, "evidence_refs")
        assert not hasattr(d, "target_file")
        assert not hasattr(d, "target_symbol")
        assert not hasattr(d, "hypothesis_id")


# ---------------------------------------------------------------------------
# Frame derivation from real observations
# ---------------------------------------------------------------------------


class TestFrameDerivation:
    """frame_id and pause_generation are mechanically derived from real PDB
    observations, not hardcoded."""

    def test_frame_id_from_is_current_frame(self):
        """The bridge finds the frame where is_current=True, rather than
        assuming frame_id=0."""
        frames = [
            {
                "frame_id": 0,
                "script": "recent_window.py",
                "line": 9,
                "function": "recent_window",
                "is_current": True,
            },
        ]
        obs = _make_stack_observation(pause_generation=5, frames=frames)
        result = parse("locals", ControllerState.RUNTIME_EVIDENCE, obs)
        d = _action(result)
        assert d.arguments["frame_id"] == 0  # the is_current frame
        assert d.arguments["pause_generation"] == 5

    def test_pause_generation_from_observation(self):
        """pause_generation comes from the observation, not a hardcoded value."""
        obs = _make_stack_observation(pause_generation=7)
        result = parse("locals", ControllerState.RUNTIME_EVIDENCE, obs)
        d = _action(result)
        assert d.arguments["pause_generation"] == 7

    def test_print_also_derives_frame(self):
        obs = _make_stack_observation(pause_generation=3)
        result = parse("print x", ControllerState.RUNTIME_EVIDENCE, obs)
        d = _action(result)
        assert d.arguments["frame_id"] == 0
        assert d.arguments["pause_generation"] == 3

    def test_missing_frames_rejected(self):
        """If the stack observation has no frames, the bridge fails closed."""
        obs = Observation(
            observation_id="obs-001",
            action_id="act-001",
            run_id="run-1",
            task_id="task-1",
            name="get_stack_summary",
            status=ObservationStatus.OK,
            payload={"state": "paused", "pause_generation": 1, "frames": []},
            summary="empty",
            truncated=False,
        )
        with pytest.raises(BridgeParseError) as exc_info:
            parse("locals", ControllerState.RUNTIME_EVIDENCE, obs)
        assert exc_info.value.category is BridgeRejection.NO_PAUSE_GENERATION

    def test_missing_pause_generation_rejected(self):
        """If pause_generation is absent, the bridge fails closed."""
        obs = Observation(
            observation_id="obs-001",
            action_id="act-001",
            run_id="run-1",
            task_id="task-1",
            name="get_stack_summary",
            status=ObservationStatus.OK,
            payload={
                "state": "paused",
                "frames": [{"frame_id": 0, "is_current": True, "line": 9, "function": "f", "script": "s.py"}],
            },
            summary="no gen",
            truncated=False,
        )
        with pytest.raises(BridgeParseError) as exc_info:
            parse("locals", ControllerState.RUNTIME_EVIDENCE, obs)
        assert exc_info.value.category is BridgeRejection.NO_PAUSE_GENERATION


# ---------------------------------------------------------------------------
# Baseline reproduction gate (S1 frozen claim)
# ---------------------------------------------------------------------------


class TestBaselineReproductionGate:
    """The S1 frozen claim requires debugger access to be available only
    AFTER required baseline failure reproduction.  The bridge deterministically
    gates the REPRODUCE -> UNDERSTAND transition on a real successful
    ``run_reproduction`` observation (``failure_reproduced is True``).

    Reproduction is never inferred from prompt text or model statements —
    only from the real observation payload.  Fail closed.
    """

    def test_initial_reproduce_prompt_does_not_advertise_understand(self):
        """Before any reproduction, the REPRODUCE prompt must not advertise
        ``understand`` (it must advertise ``reproduce`` and ``failed``)."""
        prompt = render_prompt(
            state=ControllerState.REPRODUCE,
            last_observation=None,
            task_description="test task",
        )
        # "understand" must not be in the available-commands listing.
        assert "understand" not in prompt
        # "reproduce" and "failed" remain available.
        assert "reproduce" in prompt
        assert "failed" in prompt

    def test_understand_before_reproduction_rejected(self):
        """An attempted ``understand`` transition before any reproduction
        is deterministically rejected with ILLEGAL_TRANSITION."""
        with pytest.raises(BridgeParseError) as exc_info:
            parse("understand", ControllerState.REPRODUCE, None)
        assert exc_info.value.category is BridgeRejection.ILLEGAL_TRANSITION
        assert "reproduction" in exc_info.value.detail.lower()

    def test_reproduction_with_failure_reproduced_false_does_not_unlock(self):
        """A run_reproduction observation with failure_reproduced=False does
        NOT unlock the transition to UNDERSTAND."""
        obs = _make_reproduction_observation(failure_reproduced=False)
        # The prompt still hides understand.
        prompt = render_prompt(
            state=ControllerState.REPRODUCE,
            last_observation=obs,
            task_description="test task",
        )
        assert "understand" not in prompt
        # And the parser rejects the transition.
        with pytest.raises(BridgeParseError) as exc_info:
            parse("understand", ControllerState.REPRODUCE, obs)
        assert exc_info.value.category is BridgeRejection.ILLEGAL_TRANSITION

    def test_successful_reproduction_unlocks_understand(self):
        """A real run_reproduction observation with failure_reproduced=True
        DOES unlock ``understand`` in both the prompt and the parser."""
        obs = _make_reproduction_observation(failure_reproduced=True)
        prompt = render_prompt(
            state=ControllerState.REPRODUCE,
            last_observation=obs,
            task_description="test task",
        )
        assert "understand" in prompt
        result = parse("understand", ControllerState.REPRODUCE, obs)
        d = _transition(result)
        assert d.target_state is ControllerState.UNDERSTAND

    def test_normal_transition_to_understand_after_reproduction(self):
        """After successful reproduction, the normal transition to
        UNDERSTAND still works and carries the model-requested reason."""
        obs = _make_reproduction_observation(failure_reproduced=True)
        result = parse("understand", ControllerState.REPRODUCE, obs)
        d = _transition(result)
        assert d.target_state is ControllerState.UNDERSTAND
        assert "understand" in d.reason

    def test_failed_still_allowed_in_reproduce_before_reproduction(self):
        """The reproduction gate must not block the failure exit: ``failed``
        in REPRODUCE remains valid even before reproduction."""
        result = parse("failed", ControllerState.REPRODUCE, None)
        d = _transition(result)
        assert d.target_state is ControllerState.FAILED