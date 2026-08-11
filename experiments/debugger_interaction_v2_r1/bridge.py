"""R1 — repaired model-facing debugger interface bridge.

This is a NEW treatment revision of the S1 ``bridge.py``.  It adds four
repairs that remove interface confounds blocking the first real-model
debugger interaction checkpoint (R1):

A. Source visibility + breakpoint-eligible line affordance at the ``break``
   decision point.  The model sees the production target source with line
   numbers and the set of lines where Python's trace function may fire
   ``user_line`` events, derived mechanically from ``compile()`` +
   ``co_lines()`` — no oracle, no anchor, no root-cause information.

B. ``get_source_window`` observations render the actual source lines, not
   just the summary string.

C. Error observations render the bounded/redacted diagnostic, not fake
   success fields ("Paused at line None …").

D. (applied in ``tool_registry.py`` / ``tools.py``) ``ToolExecutionError``
   now carries ``safe_diagnostic``.

E. Debugger lifecycle visibility: the prompt shows the current debugger
   lifecycle state (NOT_STARTED / PAUSED / CONSUMED_OR_ENDED) and advertises
   only the commands that are legal in that state.  The model still chooses
   the action; the interface does not present known-illegal commands.

The bridge is a pure module: no I/O, no model calls, no side effects.  The
adapter (``adapter.py``) owns transport calls, telemetry, and retry.

Non-oracle contract
-------------------
The breakpoint affordance is derived from the **original fixture source**
(before the harness-appended probe driver) using ``compile()`` and recursive
``co_lines()``.  These are Python runtime semantics available to any
developer with the source.  No ``RuntimeProbe.anchor``,
``RuntimeProbe.focus_function``, ``DemoScenario.root_cause_statement``,
``ReferenceRepair``, or task oracle field is used.

The ``co_lines()`` output is treated as *mechanically traceable line
candidates*: lines where the Python tracer *may* fire a ``user_line`` event
on some execution path.  A candidate is not a guarantee that the specific
probe invocation will pause there; a breakpoint on a candidate that is not
reached by the probe's execution path will cause the target to exit without
pausing.  The affordance does not claim otherwise.

Module-definition lines (``def`` / ``class`` statements at module level)
are excluded from the advertised breakpoint set where the distinction is
deterministic: they fire ``user_line`` during module-level execution, not
inside a function call, so they are not meaningful debugger targets for
the interactive pilot.  This exclusion uses only AST structure, not the
oracle.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

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
# Rejection categories (unchanged from S1)
# ---------------------------------------------------------------------------


class BridgeRejection(str, Enum):
    EMPTY_RESPONSE = "empty_response"
    UNRECOGNIZED_COMMAND = "unrecognized_command"
    MISSING_ARGUMENT = "missing_argument"
    INVALID_ARGUMENT_TYPE = "invalid_argument_type"
    NO_PAUSE_GENERATION = "no_pause_generation"
    INVALID_PATCH = "invalid_patch"
    UNEXPECTED_CONTENT = "unexpected_content"
    COMMAND_NOT_IN_STATE = "command_not_in_state"
    ILLEGAL_TRANSITION = "illegal_transition"
    COMMAND_NOT_IN_LIFECYCLE = "command_not_in_lifecycle"


class BridgeParseError(Exception):
    def __init__(self, category: BridgeRejection, detail: str) -> None:
        super().__init__(f"{category.value}: {detail}")
        self.category = category
        self.detail = detail


# ---------------------------------------------------------------------------
# Parse result (unchanged)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeResult:
    command_token: str
    normalized_command: str
    directive: object  # ActionDirective | TransitionDirective
    is_diagnosis: bool = False
    diagnosis_text: Optional[str] = None


# ---------------------------------------------------------------------------
# Phase aliases + command maps (unchanged from S1)
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

_ALL_COMMANDS: frozenset[str] = frozenset(
    set(_ACTION_COMMANDS.keys()) | set(_PHASE_ALIASES.keys()) | {"diagnosis"}
)

_PDB_FRAME_COMMANDS: frozenset[str] = frozenset({"locals", "print"})


# ---------------------------------------------------------------------------
# Debugger lifecycle (R1 Repair E)
# ---------------------------------------------------------------------------


class DebuggerLifecycle(str, Enum):
    """The three debugger lifecycle phases the interactive pilot can be in.

    The lifecycle is derived from the actual ``DemoToolContext`` state by
    the adapter (which holds a session-state callback), not from prompt
    text or model claims.
    """

    NOT_STARTED = "not_started"       # no session ever started; break is legal
    PAUSED = "paused"                  # session active and paused; inspection/control legal
    CONSUMED_OR_ENDED = "consumed_or_ended"  # one-session guard consumed or session ended; break NOT legal


# Commands legal without an active PDB session.
# R1.1: while waiting for the FIRST debugger session, NOT_STARTED is a
# bounded breakpoint-selection substate.  The complete target source is
# already rendered inline in the prompt, so ``source`` is informationally
# redundant and is NOT advertised.  Static escape actions (diagnosis,
# patch, understand) are NOT advertised as substitutes for the debugger
# treatment.  Only ``break`` (the meaningful debugger action) and
# ``failed`` (honest escape) are legal.
_NO_SESSION_COMMANDS: frozenset[str] = frozenset({"break", "failed"})

# Commands legal when a PDB session is paused.
_PAUSED_COMMANDS: frozenset[str] = frozenset({
    "stack", "locals", "print", "step", "next", "continue", "stop",
    "source", "diagnosis", "understand", "patch", "failed",
})

# When the one-session guard is consumed or the session ended, the model
# can only escape honestly (failed) or transition to patch/diagnosis.
# ``source`` is not advertised (the source is already in the prompt).
_CONSUMED_COMMANDS: frozenset[str] = frozenset({
    "diagnosis", "understand", "patch", "failed",
})


# ---------------------------------------------------------------------------
# State-specific command surface (base, before lifecycle filter)
# ---------------------------------------------------------------------------

_STATE_COMMANDS: dict[ControllerState, frozenset[str]] = {
    ControllerState.REPRODUCE: frozenset({"reproduce", "understand", "failed"}),
    ControllerState.UNDERSTAND: frozenset(
        {"source", "diagnosis", "failed"}
        | {"runtime", "patch"}
    ),
    ControllerState.RUNTIME_EVIDENCE: frozenset(
        {
            "break", "stack", "locals", "print", "step", "next",
            "continue", "stop", "source", "diagnosis",
            "understand", "patch", "failed",
        }
    ),
    ControllerState.PATCH: frozenset(
        {"patch", "syntax", "understand", "validate", "failed"}
    ),
    ControllerState.VALIDATE: frozenset(
        {
            "reproduce", "regression", "classify",
            "understand", "runtime", "patch", "done", "failed",
        }
    ),
    ControllerState.DONE: frozenset(),
    ControllerState.FAILED: frozenset(),
}


def visible_commands(
    state: ControllerState,
    lifecycle: DebuggerLifecycle = DebuggerLifecycle.NOT_STARTED,
) -> tuple[str, ...]:
    """Return the commands legal in ``state`` ∩ ``lifecycle``, sorted.

    In ``RUNTIME_EVIDENCE`` the lifecycle filter narrows the base command
    set to the commands that are actually legal given the current debugger
    session state.  In other states the lifecycle is not applicable and the
    base set is returned unchanged.
    """

    base = _STATE_COMMANDS.get(state, frozenset())
    if state is not ControllerState.RUNTIME_EVIDENCE:
        return tuple(sorted(base))

    if lifecycle is DebuggerLifecycle.NOT_STARTED:
        legal = base & _NO_SESSION_COMMANDS
    elif lifecycle is DebuggerLifecycle.PAUSED:
        legal = base & _PAUSED_COMMANDS
    else:
        legal = base & _CONSUMED_COMMANDS
    return tuple(sorted(legal))


# ---------------------------------------------------------------------------
# Breakpoint eligibility (R1 Repair A) — non-oracle, production-only
# ---------------------------------------------------------------------------


def _collect_traceable_lines(source: str) -> frozenset[int]:
    """Return the set of line numbers where Python's tracer *may* fire.

    Uses ``compile()`` + recursive ``co_lines()`` over all nested code
    objects.  These are *mechanically traceable line candidates*: lines
    where a ``user_line`` event may occur on some execution path.  A
    candidate is not a guarantee that the specific probe invocation will
    pause there.
    """

    code = compile(source, "<affordance>", "exec")
    traceable: set[int] = set()

    def _walk(co: object) -> None:
        if not hasattr(co, "co_lines"):
            return
        for _start, _end, line in co.co_lines():  # type: ignore[attr-defined]
            if line is not None and line > 0:
                traceable.add(line)
        for const in co.co_consts:  # type: ignore[attr-defined]
            if hasattr(const, "co_lines"):
                _walk(const)

    _walk(code)
    return frozenset(traceable)


def _module_def_lines(source: str) -> frozenset[int]:
    """Return the line numbers of module-level ``def``/``class`` statements.

    These fire ``user_line`` during module-level execution, not inside a
    function call, so they are not meaningful interactive-debugger targets.
    The exclusion uses only AST structure, not the oracle.
    """

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()
    lines: set[int] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lines.add(node.lineno)
    return frozenset(lines)


def breakpoint_eligible_lines(source: str) -> tuple[int, ...]:
    """Return the sorted, production-only breakpoint-eligible line numbers.

    The set is:
      ``_collect_traceable_lines(source)`` − ``_module_def_lines(source)``

    No oracle information is used.  The source is the **original fixture**
    (before the harness-appended probe driver), so harness driver lines are
    excluded by construction.
    """

    traceable = _collect_traceable_lines(source)
    defs = _module_def_lines(source)
    return tuple(sorted(traceable - defs))


def format_source_with_lines(
    source: str,
    eligible: Sequence[int],
) -> str:
    """Render source with line numbers, marking breakpoint-eligible lines.

    Example::

        >   2:     sequence_length = len(values)
            3:     requested_size = size

    The ``>`` prefix marks eligible lines; spaces mark non-eligible lines.
    """

    eligible_set = frozenset(eligible)
    lines = source.splitlines()
    parts: list[str] = []
    for i, line in enumerate(lines, 1):
        marker = ">" if i in eligible_set else " "
        parts.append(f"{marker} {i:3d}: {line}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Observation helpers (unchanged from S1)
# ---------------------------------------------------------------------------


def _derive_frame_id_and_generation(
    last_observation: Optional[Observation],
) -> tuple[int, int]:
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
    if observation is None:
        return False
    if observation.name != "run_reproduction":
        return False
    payload = observation.payload
    if type(payload) is not dict:
        return False
    return payload.get("failure_reproduced") is True


# ---------------------------------------------------------------------------
# Text validation (unchanged)
# ---------------------------------------------------------------------------


def _validate_text(value: str, field: str, maximum_bytes: int) -> str:
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
# Core parse function (unchanged from S1 — the model still makes the choice)
# ---------------------------------------------------------------------------


def parse(
    raw_text: str,
    state: ControllerState,
    last_observation: Optional[Observation] = None,
    lifecycle: Optional[DebuggerLifecycle] = None,
) -> BridgeResult:
    """Parse a model response into a typed directive.

    The parser performs syntactic 1:1 mapping and, when ``lifecycle`` is
    supplied for ``RUNTIME_EVIDENCE``, enforces the same lifecycle action mask
    rendered in the prompt.  A hidden/non-legal command is rejected with
    concise feedback before it reaches the tool layer.  When ``lifecycle``
    is omitted, the state-only compatibility behavior is retained for
    callers/tests that do not provide debugger context.
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

    if token not in _ALL_COMMANDS:
        raise BridgeParseError(
            BridgeRejection.UNRECOGNIZED_COMMAND,
            f"unrecognized command {token!r}",
        )

    state_cmds = _STATE_COMMANDS.get(state, frozenset())
    if token not in state_cmds:
        raise BridgeParseError(
            BridgeRejection.COMMAND_NOT_IN_STATE,
            f"command {token!r} is not available in state {state.value}",
        )

    if lifecycle is not None and state is ControllerState.RUNTIME_EVIDENCE:
        lifecycle_commands = set(visible_commands(state, lifecycle))
        if token not in lifecycle_commands:
            raise BridgeParseError(
                BridgeRejection.COMMAND_NOT_IN_LIFECYCLE,
                f"command {token!r} is not available while debugger lifecycle "
                f"is {lifecycle.value}; choose one of: "
                f"{', '.join(sorted(lifecycle_commands))}",
            )

    # Deterministic baseline-reproduction gate (S1, unchanged).
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

    # --- Diagnosis ---
    if token == "diagnosis":
        if len(lines) > 1:
            raise BridgeParseError(
                BridgeRejection.UNEXPECTED_CONTENT,
                "diagnosis must be a single line",
            )
        text = first_line[len("diagnosis"):].strip()
        if not text:
            raise BridgeParseError(
                BridgeRejection.MISSING_ARGUMENT,
                "diagnosis requires text after the command",
            )
        text = _validate_text(text, "diagnosis_text", MAX_MODEL_REASON_BYTES)
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

    # --- Phase transitions ---
    _PATCH_AS_TRANSITION_STATES = {
        ControllerState.UNDERSTAND,
        ControllerState.RUNTIME_EVIDENCE,
    }
    is_patch_transition = (
        token == "patch" and state in _PATCH_AS_TRANSITION_STATES
    )

    if token in _PHASE_ALIASES and (token not in _ACTION_COMMANDS or is_patch_transition):
        target = _PHASE_ALIASES[token]
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

    # --- Action commands ---
    if token in _ACTION_COMMANDS:
        action_name = _ACTION_COMMANDS[token]

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

        if token == "print":
            expr = first_line[len("print"):].strip()
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

        if token == "patch":
            if len(lines) < 2:
                raise BridgeParseError(
                    BridgeRejection.INVALID_PATCH,
                    "patch requires a unified diff after the command line",
                )
            diff = "\n".join(lines[1:])
            diff = diff.strip("\n")
            if diff and not diff.endswith("\n"):
                diff = diff + "\n"
            if not diff.strip():
                raise BridgeParseError(
                    BridgeRejection.INVALID_PATCH,
                    "patch diff is empty",
                )
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

    raise BridgeParseError(
        BridgeRejection.UNRECOGNIZED_COMMAND,
        f"command {token!r} was recognised but not dispatched (bridge bug)",
    )


# ---------------------------------------------------------------------------
# System prompt (R1 — describes the source/lifecycle affordance)
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = (
    "You are the debugging model component of a typed debugging controller.\n"
    "You communicate with the controller using one command per response.\n"
    "Each command is a single line of text, except 'patch' which is followed by\n"
    "a unified diff.\n\n"
    "Available commands depend on the current phase and the debugger session\n"
    "state.  The user message lists the commands available right now and the\n"
    "current debugger lifecycle.  Use only the listed commands.\n\n"
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
    "  - The complete target production source is already shown in the user\n"
    "    message, with mechanically traceable breakpoint candidates marked.\n"
    "  - At the initial debugger decision, choose an eligible production line\n"
    "    yourself and emit exactly 'break <n>'.  Do not emit 'source' first:\n"
    "    the source is already present.\n"
    "  - Choose a line where execution may pause inside a production function;\n"
    "    candidates are mechanical affordances, not guarantees for every path.\n"
    "  - After 'stack', use 'locals' or 'print <expr>' to inspect the frame.\n"
    "  - After debugging, use 'diagnosis <text>' to record your diagnosis,\n"
    "    then transition with 'patch' to apply a fix.\n"
)


# ---------------------------------------------------------------------------
# Prompt rendering (R1 — with source + lifecycle + eligible lines)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DebuggerContext:
    """Model-facing debugger context passed from the adapter to the bridge.

    ``script_path``    — the probe script the PDB backend will debug.
    ``source_text``    — the **original fixture** source (before driver append).
    ``eligible_lines`` — breakpoint-eligible production lines (non-oracle).
    ``lifecycle``      — current debugger lifecycle state.
    ``paused_line``    — line where the session is paused (if PAUSED).
    ``paused_function`` — function name at the pause (if PAUSED).
    """

    script_path: Optional[str] = None
    source_text: Optional[str] = None
    eligible_lines: tuple[int, ...] = ()
    lifecycle: DebuggerLifecycle = DebuggerLifecycle.NOT_STARTED
    paused_line: Optional[int] = None
    paused_function: Optional[str] = None


def render_prompt(
    state: ControllerState,
    last_observation: Optional[Observation],
    task_description: str,
    feedback: Optional[str] = None,
    *,
    debugger: Optional[DebuggerContext] = None,
) -> str:
    """Render the state-specific user prompt for the model.

    In ``RUNTIME_EVIDENCE`` the prompt includes:
    - the current debugger lifecycle state;
    - the target script path and numbered source with breakpoint-eligible
      lines (when no session is active);
    - only the commands legal in the current lifecycle state.
    """

    lifecycle = (
        debugger.lifecycle if debugger is not None
        else DebuggerLifecycle.NOT_STARTED
    )
    commands = visible_commands(state, lifecycle)

    # S1 frozen claim: hide 'understand' in REPRODUCE before reproduction.
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
    ]

    # Debugger lifecycle line (R1 Repair E).
    if state is ControllerState.RUNTIME_EVIDENCE and debugger is not None:
        if lifecycle is DebuggerLifecycle.NOT_STARTED:
            parts.append("Debugger: no active PDB session (you may start one with 'break')")
        elif lifecycle is DebuggerLifecycle.PAUSED:
            line_info = ""
            if debugger.paused_line is not None and debugger.paused_function is not None:
                line_info = f" at line {debugger.paused_line} in function '{debugger.paused_function}'"
            parts.append(f"Debugger: PDB session paused{line_info}")
        else:
            parts.append(
                "Debugger: PDB session ended (one session per case — 'break' is no longer available)"
            )

    # Source affordance (R1 Repair A) — only when no session is active and
    # we have the source.
    if (
        state is ControllerState.RUNTIME_EVIDENCE
        and lifecycle is DebuggerLifecycle.NOT_STARTED
        and debugger is not None
        and debugger.script_path is not None
        and debugger.source_text is not None
    ):
        formatted = format_source_with_lines(
            debugger.source_text, debugger.eligible_lines
        )
        eligible_str = ", ".join(str(n) for n in debugger.eligible_lines)
        parts.append(
            f"\nTarget script for debugging: {debugger.script_path}\n"
            f"Source (lines marked with '>' are breakpoint-eligible):\n{formatted}\n"
            f"Breakpoint-eligible lines: {eligible_str}\n"
            f"Use 'break <n>' with an eligible line number to start debugging."
        )

    parts.append(f"\nTask:\n{task_description}")

    if obs_text:
        parts.append(f"\nLast observation:\n{obs_text}")
    if feedback:
        parts.append(f"\nPrevious response was rejected: {feedback}")
    parts.append(
        "\nEmit exactly one command from the available list above. "
        "Do not emit prose, markdown, or JSON."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Observation rendering (R1 — Repairs B and C)
# ---------------------------------------------------------------------------


def _render_observation(observation: Optional[Observation]) -> str:
    """Render an observation as natural text for the model.

    R1 repairs:
    - ``get_source_window`` now renders the actual source lines (B).
    - Error/rejected/timeout observations render the diagnostic, not fake
      success fields (C).
    """

    if observation is None:
        return ""

    name = observation.name
    payload = observation.payload if type(observation.payload) is dict else {}
    status = observation.status.value if hasattr(observation.status, "value") else str(observation.status)
    summary = observation.summary

    # --- Non-OK observations: render the diagnostic (R1 Repair C) ---
    if status not in ("ok", "completed"):
        reason = payload.get("dispatch_reason", status)
        diagnostic = payload.get("diagnostic")
        if diagnostic:
            return f"[{name}] status={status}\n  ERROR: {reason} — {diagnostic}"
        return f"[{name}] status={status}\n  ERROR: {reason}"

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
        # R1 Repair B: render the actual source lines, not just the summary.
        source_lines = payload.get("lines")
        if type(source_lines) is list and source_lines:
            path = payload.get("path", "?")
            lines.append(f"  {path}")
            for entry in source_lines:
                if type(entry) is dict:
                    ln = entry.get("line_number", "?")
                    text = entry.get("text", "")
                    focal = ">>" if entry.get("is_focal") else "  "
                    lines.append(f"  {focal} {ln:>4d}: {text}")
        else:
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
        lines.append(f"  {summary}")

    return "\n".join(lines)


def _render_value(val: dict) -> str:
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
    "DebuggerContext",
    "DebuggerLifecycle",
    "SYSTEM_PROMPT",
    "breakpoint_eligible_lines",
    "format_source_with_lines",
    "parse",
    "render_prompt",
    "visible_commands",
]