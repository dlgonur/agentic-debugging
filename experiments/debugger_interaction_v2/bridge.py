"""Deterministic, fail-closed bridge from a small model-facing grammar to
existing typed ``ModelDirective`` dataclasses.

This module is the S1 *interface bridge*.  It performs purely syntactic 1:1
mapping of short natural debugger commands to the existing internal typed
action/transition directives.  It never:

* chooses the correct debugger action for the model;
* infers semantic intent beyond deterministic parsing;
* injects oracle information;
* rewrites hypotheses;
* chooses breakpoints;
* repairs patches;
* performs semantic fallback;
* silently turns prose into a likely action.

Everything is deterministic, bounded and fail-closed.  An unrecognised command
is a parse rejection, never an action.

The bridge is a pure module: it has no I/O, no model calls, and no side
effects.  The adapter (``adapter.py``) owns transport calls, telemetry, and
retry; this module only translates text to directives.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from agentic_debugger.agent.controller_policy import ActionName
from agentic_debugger.agent.model_adapter import (
    MAX_MODEL_REASON_BYTES,
    ActionDirective,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import (
    ControllerState,
    TRANSITION_GRAPH,
    is_transition_allowed,
)
from agentic_debugger.events.schema import Observation


# ---------------------------------------------------------------------------
# Rejection categories
# ---------------------------------------------------------------------------


class BridgeRejection(str, Enum):
    """Deterministic categories for bridge parse failures."""

    EMPTY_RESPONSE = "empty_response"
    UNRECOGNIZED_COMMAND = "unrecognized_command"
    MISSING_ARGUMENT = "missing_argument"
    INVALID_ARGUMENT_TYPE = "invalid_argument_type"
    NO_PAUSE_GENERATION = "no_pause_generation"
    INVALID_PATCH = "invalid_patch"
    UNEXPECTED_CONTENT = "unexpected_content"
    COMMAND_NOT_IN_STATE = "command_not_in_state"
    ILLEGAL_TRANSITION = "illegal_transition"


class BridgeParseError(Exception):
    """Raised when the bridge cannot parse a model response.

    Carries a deterministic ``BridgeRejection`` category and a human-readable
    detail string.  The caller (adapter) retains the full raw text in
    telemetry *before* raising this, so no evidence is lost.
    """

    def __init__(
        self,
        category: BridgeRejection,
        detail: str,
    ) -> None:
        super().__init__(f"{category.value}: {detail}")
        self.category = category
        self.detail = detail


# ---------------------------------------------------------------------------
# Parse result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeResult:
    """The output of a successful bridge parse.

    ``command_token`` is the normalised first token (e.g. ``"break"``).
    ``normalized_command`` is a stable string representation of the parsed
    command for telemetry.  ``directive`` is the typed ``ModelDirective`` that
    the controller will consume.
    """

    command_token: str
    normalized_command: str
    directive: object  # ActionDirective | TransitionDirective
    is_diagnosis: bool = False
    diagnosis_text: Optional[str] = None


# ---------------------------------------------------------------------------
# Phase aliases (deterministic alias -> ControllerState mapping)
# ---------------------------------------------------------------------------

_PHASE_ALIASES: dict[str, ControllerState] = {
    "reproduce": ControllerState.REPRODUCE,
    "understand": ControllerState.UNDERSTAND,
    "runtime": ControllerState.RUNTIME_EVIDENCE,
    "patch": ControllerState.PATCH,
    "validate": ControllerState.VALIDATE,
    "done": ControllerState.DONE,
    "failed": ControllerState.FAILED,
}

# Commands that map to a typed ActionName (not a transition).
_ACTION_COMMANDS: dict[str, ActionName] = {
    "reproduce": ActionName.RUN_REPRODUCTION,
    "source": ActionName.GET_SOURCE_WINDOW,
    "break": ActionName.START_PDB_SESSION,
    "stack": ActionName.GET_STACK_SUMMARY,
    "locals": ActionName.GET_FRAME_LOCALS,
    "print": ActionName.SAFE_EVAL_EXPRESSION,
    "step": ActionName.STEP_PDB_SESSION,
    "next": ActionName.NEXT_PDB_SESSION,
    "continue": ActionName.CONTINUE_PDB_SESSION,
    "stop": ActionName.STOP_PDB_SESSION,
    "patch": ActionName.APPLY_PATCH,
    "syntax": ActionName.SYNTAX_CHECK,
    "regression": ActionName.RUN_REGRESSION_TESTS,
    "classify": ActionName.CLASSIFY_OUTCOME,
}

# All known command tokens (actions + phase + diagnosis).
_ALL_COMMANDS: frozenset[str] = frozenset(
    set(_ACTION_COMMANDS.keys()) | set(_PHASE_ALIASES.keys()) | {"diagnosis"}
)

# PDB commands that require a prior stack observation (for pause_generation
# and frame_id derivation).
_PDB_FRAME_COMMANDS: frozenset[str] = frozenset({"locals", "print"})

# PDB commands that do not need a prior observation.
_PDB_NO_FRAME_COMMANDS: frozenset[str] = frozenset(
    {"break", "stack", "step", "next", "continue", "stop"}
)


# ---------------------------------------------------------------------------
# State-specific command surface
# ---------------------------------------------------------------------------

# The visible commands per state are a projection of:
#   _ALLOWED_ACTIONS_BY_STATE (controller_policy.py:162)
#   ∩ registered tools (demo/tools.py:build_registry with interactive controls)
#   ∩ transition graph self-loops + legal transitions
# This is NOT a new state machine — it is a deterministic projection of the
# existing controller contracts.

_STATE_COMMANDS: dict[ControllerState, frozenset[str]] = {
    ControllerState.REPRODUCE: frozenset({"reproduce", "understand", "failed"}),
    ControllerState.UNDERSTAND: frozenset(
        {"source", "diagnosis", "failed"}
        # transitions: runtime, patch (not "phase" prefix — they are bare aliases)
        | {"runtime", "patch"}
    ),
    ControllerState.RUNTIME_EVIDENCE: frozenset(
        {
            "break",
            "stack",
            "locals",
            "print",
            "step",
            "next",
            "continue",
            "stop",
            "source",
            "diagnosis",
            "understand",
            "patch",
            "failed",
        }
    ),
    ControllerState.PATCH: frozenset(
        {"patch", "syntax", "understand", "validate", "failed"}
    ),
    ControllerState.VALIDATE: frozenset(
        {
            "reproduce",
            "regression",
            "classify",
            "understand",
            "runtime",
            "patch",
            "done",
            "failed",
        }
    ),
    ControllerState.DONE: frozenset(),
    ControllerState.FAILED: frozenset(),
}


def visible_commands(state: ControllerState) -> tuple[str, ...]:
    """Return the commands the model should see in a given state, sorted."""
    return tuple(sorted(_STATE_COMMANDS.get(state, frozenset())))


# ---------------------------------------------------------------------------
# Frame and pause_generation derivation from real PDB observations
# ---------------------------------------------------------------------------


def _derive_frame_id_and_generation(
    last_observation: Optional[Observation],
) -> tuple[int, int]:
    """Mechanically extract frame_id and pause_generation from the last PDB
    stack observation.

    frame_id: the frame where ``is_current`` is True.  The PDB protocol
    guarantees (pdb_session.py:1845) that ``is_current == (frame_id == 0)``
    and exactly one current frame exists.  We derive it from the observation
    rather than hardcoding the index — this is deterministic wiring from
    actual tool output, not semantic assistance.

    pause_generation: always present in a ``get_stack_summary`` result
    (pdb_session.py:86-89, a required positive integer field).

    Raises ``BridgeParseError(NO_PAUSE_GENERATION)`` if no prior stack
    observation exists or the required fields are absent.
    """

    if last_observation is None:
        raise BridgeParseError(
            BridgeRejection.NO_PAUSE_GENERATION,
            "no prior observation — run 'stack' before 'locals' or 'print'",
        )
    payload = last_observation.payload
    if type(payload) is not dict:
        raise BridgeParseError(
            BridgeRejection.NO_PAUSE_GENERATION,
            "last observation payload is not a mapping",
        )
    frames = payload.get("frames")
    if type(frames) is not list or not frames:
        raise BridgeParseError(
            BridgeRejection.NO_PAUSE_GENERATION,
            "no frames in last stack observation — run 'stack' first",
        )
    # Find the current frame (is_current == True).
    # Protocol guarantees exactly one such frame with frame_id == 0.
    frame_id: Optional[int] = None
    for frame in frames:
        if type(frame) is dict and frame.get("is_current") is True:
            fid = frame.get("frame_id")
            if type(fid) is int:
                frame_id = fid
                break
    if frame_id is None:
        raise BridgeParseError(
            BridgeRejection.NO_PAUSE_GENERATION,
            "no current frame in last stack observation",
        )
    pause_gen = payload.get("pause_generation")
    if type(pause_gen) is not int or pause_gen <= 0:
        raise BridgeParseError(
            BridgeRejection.NO_PAUSE_GENERATION,
            "no valid pause_generation in last stack observation",
        )
    return frame_id, pause_gen


def _is_pdb_stack_observation(observation: Optional[Observation]) -> bool:
    """Check whether an observation is a get_stack_summary result with frames."""

    if observation is None:
        return False
    if observation.name != "get_stack_summary":
        return False
    payload = observation.payload
    if type(payload) is not dict:
        return False
    return "frames" in payload and "pause_generation" in payload


def _baseline_reproduction_succeeded(
    observation: Optional[Observation],
) -> bool:
    """Return True only when ``observation`` is a successful baseline
    ``run_reproduction`` result.

    A successful baseline reproduction is established ONLY from the real
    reproduction observation produced by ``handle_run_reproduction``
    (demo/tools.py:407-415): the action/observation corresponds to
    ``RUN_REPRODUCTION`` and the payload records ``failure_reproduced is True``.

    This is the deterministic gate for the S1 frozen claim that debugger
    access is directly available AFTER required baseline failure
    reproduction.  Reproduction is never inferred from prompt text or
    model statements — only from the real observation payload.  Fail closed.
    """

    if observation is None:
        return False
    if observation.name != "run_reproduction":
        return False
    payload = observation.payload
    if type(payload) is not dict:
        return False
    return payload.get("failure_reproduced") is True


# ---------------------------------------------------------------------------
# Text validation (reuse the model_adapter contract)
# ---------------------------------------------------------------------------


def _validate_text(value: str, field: str, maximum_bytes: int) -> str:
    """Validate text the same way TransitionDirective validates reason."""

    if type(value) is not str or not value or value != value.strip():
        raise BridgeParseError(
            BridgeRejection.INVALID_ARGUMENT_TYPE,
            f"{field} must be non-empty and trimmed",
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise BridgeParseError(
            BridgeRejection.INVALID_ARGUMENT_TYPE,
            f"{field} must be UTF-8 encodable",
        ) from None
    if len(encoded) > maximum_bytes:
        raise BridgeParseError(
            BridgeRejection.INVALID_ARGUMENT_TYPE,
            f"{field} exceeds {maximum_bytes} bytes",
        )
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise BridgeParseError(
            BridgeRejection.INVALID_ARGUMENT_TYPE,
            f"{field} must not contain control characters",
        )
    return value


def _parse_int(token: str, field: str) -> int:
    """Parse a strict positive integer token."""

    token = token.strip()
    if not token:
        raise BridgeParseError(
            BridgeRejection.MISSING_ARGUMENT,
            f"missing {field} value",
        )
    try:
        value = int(token)
    except ValueError:
        raise BridgeParseError(
            BridgeRejection.INVALID_ARGUMENT_TYPE,
            f"{field} must be an integer, got {token!r}",
        ) from None
    return value


# ---------------------------------------------------------------------------
# Core parse function
# ---------------------------------------------------------------------------


def parse(
    raw_text: str,
    state: ControllerState,
    last_observation: Optional[Observation] = None,
) -> BridgeResult:
    """Parse a model response into a typed directive.

    Parameters
    ----------
    raw_text
        The exact decoded model text (one response, possibly multi-line).
    state
        The current controller state — used for state-specific command
        filtering and transition legality.
    last_observation
        The last observation the controller holds.  Used to mechanically
        derive ``frame_id`` and ``pause_generation`` for ``locals``/``print``
        commands.  This is deterministic wiring from real tool output.

    Returns
    -------
    BridgeResult
        On success.

    Raises
    ------
    BridgeParseError
        On any parse failure.  The caller must retain ``raw_text`` in
        telemetry before calling this function.
    """

    if type(raw_text) is not str:
        raise BridgeParseError(
            BridgeRejection.EMPTY_RESPONSE,
            "raw text is not a string",
        )
    stripped = raw_text.strip()
    if not stripped:
        raise BridgeParseError(
            BridgeRejection.EMPTY_RESPONSE,
            "model response is empty",
        )

    lines = stripped.split("\n")
    first_line = lines[0].strip()
    parts = first_line.split()
    token = parts[0].lower() if parts else ""

    if not token:
        raise BridgeParseError(
            BridgeRejection.EMPTY_RESPONSE,
            "first line has no command token",
        )

    # ------------------------------------------------------------------
    # 1. Check the command is recognised at all.
    # ------------------------------------------------------------------

    if token not in _ALL_COMMANDS:
        raise BridgeParseError(
            BridgeRejection.UNRECOGNIZED_COMMAND,
            f"unrecognized command {token!r}",
        )

    # ------------------------------------------------------------------
    # 2. Check the command is legal in the current state.
    # ------------------------------------------------------------------

    state_cmds = _STATE_COMMANDS.get(state, frozenset())
    if token not in state_cmds:
        raise BridgeParseError(
            BridgeRejection.COMMAND_NOT_IN_STATE,
            f"command {token!r} is not available in state {state.value}",
        )

    # ------------------------------------------------------------------
    # 2b. Deterministic baseline-reproduction gate (S1 frozen claim).
    # ------------------------------------------------------------------
    # In REPRODUCE, the transition to UNDERSTAND is available only AFTER a
    # successful baseline failure reproduction.  A successful reproduction
    # is established ONLY from a real ``run_reproduction`` observation whose
    # payload records ``failure_reproduced is True`` (see
    # ``_baseline_reproduction_succeeded``).  This is enforced here, in the
    # parser, so it holds regardless of prompt visibility: even if the model
    # emits ``understand`` before reproduction, the parser rejects it.  Fail
    # closed.  This does not change the production state machine, controller,
    # or tool handlers — it is an S1-experiment-local bridge guard.
    if (
        state is ControllerState.REPRODUCE
        and token == "understand"
        and not _baseline_reproduction_succeeded(last_observation)
    ):
        raise BridgeParseError(
            BridgeRejection.ILLEGAL_TRANSITION,
            "baseline failure reproduction required before transitioning to "
            "UNDERSTAND (no successful run_reproduction observation)",
        )

    # ------------------------------------------------------------------
    # 3. Dispatch by command family.
    # ------------------------------------------------------------------

    # --- Diagnosis (self-transition with model-authored text) -------------

    if token == "diagnosis":
        # diagnosis <model-authored text>
        # The text is everything after "diagnosis " on the first line.
        # Multi-line diagnosis is not supported (the model should put the
        # diagnosis on one line).  Extra lines are rejected.
        if len(lines) > 1:
            raise BridgeParseError(
                BridgeRejection.UNEXPECTED_CONTENT,
                "diagnosis must be a single line",
            )
        text = first_line[len("diagnosis") :].strip()
        if not text:
            raise BridgeParseError(
                BridgeRejection.MISSING_ARGUMENT,
                "diagnosis requires text after the command",
            )
        text = _validate_text(text, "diagnosis_text", MAX_MODEL_REASON_BYTES)
        # Self-transition: legal in UNDERSTAND and RUNTIME_EVIDENCE (both
        # allow self-loops per TRANSITION_GRAPH).  The diagnosis text rides
        # in the existing `reason` field.  No fabricated hypothesis fields.
        if not is_transition_allowed(state, state):
            raise BridgeParseError(
                BridgeRejection.ILLEGAL_TRANSITION,
                f"self-transition not allowed in state {state.value}",
            )
        directive = TransitionDirective(state, text)
        return BridgeResult(
            command_token="diagnosis",
            normalized_command=f"diagnosis {text[:120]}",
            directive=directive,
            is_diagnosis=True,
            diagnosis_text=text,
        )

    # --- Phase transitions (alias -> ControllerState) --------------------

    # "patch" and "reproduce" are overloaded: they are both action commands
    # AND phase transition aliases.  The interpretation depends on the state:
    #   - "patch" in PATCH state → APPLY_PATCH action
    #   - "patch" in UNDERSTAND/RUNTIME_EVIDENCE → transition to PATCH
    #   - "reproduce" in REPRODUCE/VALIDATE → RUN_REPRODUCTION action
    #   - "reproduce" is NOT a transition alias in any state (it's only an action)
    # For "patch", check if we're in a state where it's a transition vs action.
    _PATCH_AS_TRANSITION_STATES = {
        ControllerState.UNDERSTAND,
        ControllerState.RUNTIME_EVIDENCE,
    }

    is_patch_transition = (
        token == "patch" and state in _PATCH_AS_TRANSITION_STATES
    )

    if token in _PHASE_ALIASES and (token not in _ACTION_COMMANDS or is_patch_transition):
        target = _PHASE_ALIASES[token]
        # "done" and "failed" are bare aliases, not "phase <alias>"
        if token in ("done", "failed"):
            if len(parts) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    f"{token} takes no arguments",
                )
            if len(lines) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    f"{token} must be a single line",
                )
        else:
            # "runtime", "patch", "validate", "understand", "reproduce"
            # are bare phase aliases (the model emits just the alias).
            if len(parts) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    f"{token} takes no arguments",
                )
            if len(lines) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    f"{token} must be a single line",
                )
        if not is_transition_allowed(state, target):
            raise BridgeParseError(
                BridgeRejection.ILLEGAL_TRANSITION,
                f"transition from {state.value} to {target.value} is not allowed",
            )
        directive = TransitionDirective(target, f"model requested {token}")
        return BridgeResult(
            command_token=token,
            normalized_command=token,
            directive=directive,
        )

    # --- Action commands -------------------------------------------------

    if token in _ACTION_COMMANDS:
        action_name = _ACTION_COMMANDS[token]

        # -- Commands with no arguments --------------------------------

        if token in ("stack", "step", "next", "continue", "stop", "syntax",
                      "regression", "classify"):
            if len(parts) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    f"{token} takes no arguments",
                )
            if len(lines) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    f"{token} must be a single line",
                )
            directive = ActionDirective(action_name, {})
            return BridgeResult(
                command_token=token,
                normalized_command=token,
                directive=directive,
            )

        # -- reproduce (phase is state-deterministic) -------------------

        if token == "reproduce":
            if len(parts) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    "reproduce takes no arguments",
                )
            if len(lines) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    "reproduce must be a single line",
                )
            phase = "baseline" if state is ControllerState.REPRODUCE else "post_patch"
            directive = ActionDirective(action_name, {"phase": phase})
            return BridgeResult(
                command_token=token,
                normalized_command=f"reproduce {phase}",
                directive=directive,
            )

        # -- source <path> <line> ---------------------------------------

        if token == "source":
            if len(parts) < 3:
                raise BridgeParseError(
                    BridgeRejection.MISSING_ARGUMENT,
                    "source requires <path> <line>",
                )
            if len(parts) > 3:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    "source takes exactly <path> <line>",
                )
            path = parts[1]
            line = _parse_int(parts[2], "line")
            if line < 1:
                raise BridgeParseError(
                    BridgeRejection.INVALID_ARGUMENT_TYPE,
                    "line must be >= 1",
                )
            if len(lines) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    "source must be a single line",
                )
            directive = ActionDirective(
                action_name, {"path": path, "line": line}
            )
            return BridgeResult(
                command_token=token,
                normalized_command=f"source {path} {line}",
                directive=directive,
            )

        # -- break <line> -----------------------------------------------

        if token == "break":
            if len(parts) != 2:
                raise BridgeParseError(
                    BridgeRejection.MISSING_ARGUMENT,
                    "break requires <line>",
                )
            line = _parse_int(parts[1], "breakpoint_line")
            if line < 1:
                raise BridgeParseError(
                    BridgeRejection.INVALID_ARGUMENT_TYPE,
                    "breakpoint_line must be >= 1",
                )
            if len(lines) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    "break must be a single line",
                )
            directive = ActionDirective(
                action_name, {"breakpoint_line": line}
            )
            return BridgeResult(
                command_token=token,
                normalized_command=f"break {line}",
                directive=directive,
            )

        # -- locals (frame_id + pause_generation derived from last stack) -

        if token == "locals":
            if len(parts) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    "locals takes no arguments",
                )
            if len(lines) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    "locals must be a single line",
                )
            if not _is_pdb_stack_observation(last_observation):
                raise BridgeParseError(
                    BridgeRejection.NO_PAUSE_GENERATION,
                    "run 'stack' before 'locals'",
                )
            frame_id, pause_gen = _derive_frame_id_and_generation(last_observation)
            directive = ActionDirective(
                action_name,
                {"frame_id": frame_id, "pause_generation": pause_gen},
            )
            return BridgeResult(
                command_token=token,
                normalized_command=f"locals frame={frame_id} gen={pause_gen}",
                directive=directive,
            )

        # -- print <expr> (frame_id + pause_generation derived) -----------

        if token == "print":
            # print <safe expression>
            expr = first_line[len("print") :].strip()
            if not expr:
                raise BridgeParseError(
                    BridgeRejection.MISSING_ARGUMENT,
                    "print requires an expression",
                )
            if len(lines) > 1:
                raise BridgeParseError(
                    BridgeRejection.UNEXPECTED_CONTENT,
                    "print must be a single line",
                )
            if not _is_pdb_stack_observation(last_observation):
                raise BridgeParseError(
                    BridgeRejection.NO_PAUSE_GENERATION,
                    "run 'stack' before 'print'",
                )
            frame_id, pause_gen = _derive_frame_id_and_generation(last_observation)
            # Validate the expression text (reuse the same text validator).
            expr = _validate_text(expr, "expression", MAX_MODEL_REASON_BYTES)
            directive = ActionDirective(
                action_name,
                {
                    "frame_id": frame_id,
                    "pause_generation": pause_gen,
                    "expression": expr,
                },
            )
            return BridgeResult(
                command_token=token,
                normalized_command=f"print {expr[:80]}",
                directive=directive,
            )

        # -- patch (remaining lines are the unified diff) ----------------

        if token == "patch":
            # The diff is everything after the "patch" line.  We strip
            # leading/trailing whitespace from the whole block but preserve
            # internal newlines (the trailing newline in a unified diff is
            # part of the content and must not be stripped).
            if len(lines) < 2:
                raise BridgeParseError(
                    BridgeRejection.INVALID_PATCH,
                    "patch requires a unified diff after the command line",
                )
            diff = "\n".join(lines[1:])
            # Strip only leading/trailing whitespace around the whole block,
            # not internal content.  A trailing newline is preserved because
            # the join does not add one and the raw text was already stripped
            # at the top of parse().
            diff = diff.strip("\n")
            # Re-add the trailing newline that unified diffs conventionally end with.
            if diff and not diff.endswith("\n"):
                diff = diff + "\n"
            if not diff.strip():
                raise BridgeParseError(
                    BridgeRejection.INVALID_PATCH,
                    "patch diff is empty",
                )
            # Basic sanity: a unified diff must start with --- and +++.
            if not diff.startswith("---"):
                raise BridgeParseError(
                    BridgeRejection.INVALID_PATCH,
                    "patch diff must start with '---' (unified diff header)",
                )
            diff_lines = diff.split("\n")
            if len(diff_lines) < 2 or "+++" not in diff_lines[1]:
                raise BridgeParseError(
                    BridgeRejection.INVALID_PATCH,
                    "patch diff must contain '+++' on the second line",
                )
            # Validate the diff text size.
            try:
                diff_bytes = diff.encode("utf-8")
            except UnicodeEncodeError:
                raise BridgeParseError(
                    BridgeRejection.INVALID_PATCH,
                    "patch diff must be UTF-8 encodable",
                ) from None
            if len(diff_bytes) > 32768:
                raise BridgeParseError(
                    BridgeRejection.INVALID_PATCH,
                    "patch diff exceeds 32768 bytes",
                )
            directive = ActionDirective(action_name, {"patch": diff})
            return BridgeResult(
                command_token="patch",
                normalized_command=f"patch ({len(diff_bytes)} bytes)",
                directive=directive,
            )

    # If we reach here, the token was recognised but not handled — a bug.
    raise BridgeParseError(
        BridgeRejection.UNRECOGNIZED_COMMAND,
        f"command {token!r} was recognised but not dispatched (bridge bug)",
    )


# ---------------------------------------------------------------------------
# Prompt rendering (state-specific)
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = (
    "You are the debugging model component of a typed debugging controller.\n"
    "You communicate with the controller using one command per response.\n"
    "Each command is a single line of text, except 'patch' which is followed by\n"
    "a unified diff.\n\n"
    "Available commands depend on the current phase.  The user message lists\n"
    "the commands available right now.  Use only those commands.\n\n"
    "Commands:\n"
    "  reproduce          — run the failing test to reproduce the bug\n"
    "  source <path> <n>  — show source code around line n in a file\n"
    "  break <n>          — start a PDB session paused at line n\n"
    "  stack              — show the call stack at the current pause\n"
    "  locals             — show variables in the current frame\n"
    "  print <expr>       — evaluate a safe expression in the current frame\n"
    "  step               — step to the next line (enters calls)\n"
    "  next               — step over to the next line in the current frame\n"
    "  continue           — resume execution to the next breakpoint or exit\n"
    "  stop               — stop the PDB session\n"
    "  diagnosis <text>   — record your root-cause diagnosis after debugging\n"
    "  patch              — apply a unified diff (followed by diff lines)\n"
    "  syntax             — check patch syntax\n"
    "  regression         — run pass-to-pass tests\n"
    "  classify           — classify the repair outcome\n"
    "  reproduce/understand/runtime/patch/validate\n"
    "                     — transition to a different phase\n"
    "  done               — signal completion\n"
    "  failed             — signal failure\n\n"
    "Rules:\n"
    "  - Emit exactly one command per response.\n"
    "  - Do not emit prose, markdown, or explanations.\n"
    "  - Do not emit JSON.\n"
    "  - After 'stack', use 'locals' or 'print <expr>' to inspect the frame.\n"
    "  - After debugging, use 'diagnosis <text>' to record your diagnosis,\n"
    "    then transition with 'patch' to apply a fix.\n"
)


def render_prompt(
    state: ControllerState,
    last_observation: Optional[Observation],
    task_description: str,
    feedback: Optional[str] = None,
) -> str:
    """Render the state-specific user prompt for the model.

    The prompt shows:
    - the current phase;
    - the available commands in this phase;
    - the task description (agent-visible, oracle stripped);
    - the last observation as natural text (if any);
    - optional rejection feedback from a previous attempt.

    It does NOT:
    - describe the task's oracle or correct answer;
    - suggest which command to use next;
    - hint at the bug location;
    - show the full global command vocabulary.
    """

    commands = visible_commands(state)

    # S1 frozen claim: debugger access is directly available AFTER required
    # baseline failure reproduction.  Before a successful reproduction, the
    # model-facing REPRODUCE prompt must not advertise ``understand``.  This
    # is a prompt-visibility filter only; the parser's deterministic gate
    # (``parse``) rejects an emitted ``understand`` regardless.  The static
    # ``visible_commands``/``_STATE_COMMANDS`` surface still documents
    # ``understand`` as a legal REPRODUCE transition (it remains legal once
    # reproduction succeeds); only the prompt hides it until then.
    if (
        state is ControllerState.REPRODUCE
        and "understand" in commands
        and not _baseline_reproduction_succeeded(last_observation)
    ):
        commands = tuple(c for c in commands if c != "understand")

    commands_text = "\n".join(f"  - {c}" for c in commands)

    obs_text = _render_observation(last_observation)

    parts = [
        f"Current phase: {state.value}",
        f"Available commands:\n{commands_text}",
        f"\nTask:\n{task_description}",
    ]
    if obs_text:
        parts.append(f"\nLast observation:\n{obs_text}")
    if feedback:
        parts.append(f"\nPrevious response was rejected: {feedback}")
    parts.append(
        "\nEmit exactly one command from the available list above. "
        "Do not emit prose, markdown, or JSON."
    )
    return "\n".join(parts)


def _render_observation(observation: Optional[Observation]) -> str:
    """Render an observation as natural text for the model.

    This is a deterministic projection of the observation payload — it shows
    exactly what the tool returned, with no commentary, hints, or suggested
    next steps.
    """

    if observation is None:
        return ""

    name = observation.name
    payload = observation.payload if type(observation.payload) is dict else {}
    status = observation.status.value if hasattr(observation.status, "value") else str(observation.status)
    summary = observation.summary

    lines = [f"[{name}] status={status}"]

    if name == "start_pdb_session":
        lines.append(
            f"  Paused at line {payload.get('line')} in function "
            f"'{payload.get('function')}' (breakpoint at line "
            f"{payload.get('breakpoint_line')})"
        )
    elif name == "get_stack_summary":
        gen = payload.get("pause_generation")
        frames = payload.get("frames")
        lines.append(f"  pause_generation={gen}")
        if type(frames) is list:
            for frame in frames:
                if type(frame) is dict:
                    marker = "* " if frame.get("is_current") else "  "
                    lines.append(
                        f"  {marker}frame_id={frame.get('frame_id')} "
                        f"{frame.get('function')} line={frame.get('line')} "
                        f"script={frame.get('script')}"
                    )
    elif name == "get_frame_locals":
        locals_list = payload.get("locals")
        if type(locals_list) is list:
            for var in locals_list:
                if type(var) is dict:
                    val = var.get("value")
                    val_repr = _render_value(val) if type(val) is dict else str(val)
                    lines.append(f"  {var.get('name')} = {val_repr}")
    elif name == "safe_eval_expression":
        val = payload.get("value")
        val_repr = _render_value(val) if type(val) is dict else str(val)
        expr = payload.get("expression", "")
        lines.append(f"  {expr} = {val_repr}")
    elif name in ("continue_pdb_session", "step_pdb_session", "next_pdb_session"):
        state_val = payload.get("state")
        if state_val == "paused":
            lines.append(
                f"  Paused at line {payload.get('line')} in "
                f"'{payload.get('function')}'"
            )
        elif state_val == "exited":
            lines.append(f"  Execution exited (exit_code={payload.get('exit_code')})")
        else:
            lines.append(f"  state={state_val}")
    elif name == "stop_pdb_session":
        lines.append(
            f"  stopped={payload.get('stopped')} "
            f"session_started={payload.get('session_started')}"
        )
    elif name == "run_reproduction":
        lines.append(
            f"  phase={payload.get('phase')} exit_code={payload.get('exit_code')} "
            f"passed={payload.get('passed')} "
            f"failure_reproduced={payload.get('failure_reproduced')}"
        )
    elif name == "get_source_window":
        lines.append(f"  {summary}")
    elif name == "apply_patch":
        lines.append(
            f"  applied={payload.get('applied')} "
            f"changed_files={payload.get('changed_files')}"
        )
    elif name == "syntax_check":
        lines.append(f"  all_passed={payload.get('all_passed')}")
    elif name == "run_regression_tests":
        lines.append(
            f"  all_passed={payload.get('all_passed')} "
            f"exit_code={payload.get('exit_code')}"
        )
    elif name == "classify_outcome":
        lines.append(f"  outcome={payload.get('outcome')}")
    else:
        # Generic fallback: show the summary only, no payload details.
        lines.append(f"  {summary}")

    return "\n".join(lines)


def _render_value(val: dict) -> str:
    """Render a PDB value summary dict as a short string."""

    if type(val) is not dict:
        return str(val)
    kind = val.get("kind", "?")
    value = val.get("value")
    if kind in ("int", "float", "str", "bool", "none"):
        return repr(value) if kind == "str" else str(value)
    type_name = val.get("type", kind)
    size = val.get("size")
    if size is not None:
        return f"<{type_name} len={size}>"
    return f"<{type_name}>"


__all__ = [
    "BridgeRejection",
    "BridgeParseError",
    "BridgeResult",
    "SYSTEM_PROMPT",
    "parse",
    "render_prompt",
    "visible_commands",
]