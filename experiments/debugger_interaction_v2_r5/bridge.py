"""R5 — generalized staged debugger bridge.

Derived from the accepted R3 bridge (immutable).  R5 deltas:

1. ``build_system_prompt(module_path)`` — the system prompt is a frozen
   template with the per-task writable production path substituted only in
   the patch-format example (the path is public task data derived from
   ``constraints.allowed_write_paths``, never oracle data).
2. ``patch_diff_affordance(module_path)`` — same substitution for the
   in-prompt diff affordance.
3. ``_render_observation(..., filter_scripts=None, original_line_count=None)``
   — model-facing stack rendering may be restricted to production-script
   frames whose line falls inside the original production source region
   (``1 <= line <= original_line_count``).  This excludes the appended
   neutral pytest launcher harness, which shares the same script path but
   lives beyond the original line count.  Filtering is render-only: raw
   payload frame ids are never renumbered, and ``parse`` derives
   frame_id/pause_generation from the authoritative raw observation payload.

Everything else — staged PAUSED progression, lifecycle classification via
the public ``PdbSession.get_target_status()``, breakpoint eligibility from
``compile()+co_lines()`` on the original source minus module-level
``def``/``class`` lines, diagnosis->PATCH retention, bounded PATCH
checkpoint — is identical to the accepted R3 bridge.

The bridge is a pure module: no I/O, no model calls, no side effects.
"""

from __future__ import annotations

import ast
import re
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
# Rejection categories
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
# Parse result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeResult:
    command_token: str
    normalized_command: str
    directive: object  # ActionDirective | TransitionDirective
    is_diagnosis: bool = False
    diagnosis_text: Optional[str] = None


# ---------------------------------------------------------------------------
# Phase aliases + command maps
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
# R2 lifecycle + stage (accepted R3 semantics)
# ---------------------------------------------------------------------------


class DebuggerLifecycle(str, Enum):
    """Debugger lifecycle — controls the top-level session view."""

    NOT_STARTED = "not_started"
    PAUSED = "paused"
    CONSUMED_OR_ENDED = "consumed_or_ended"


class R2Stage(str, Enum):
    """Staged progression inside PAUSED — R2/R3 accepted."""

    NOT_STARTED = "not_started"  # alias for lifecycle NOT_STARTED
    PAUSED_NEEDS_STACK = "paused_needs_stack"
    PAUSED_NEEDS_INSPECTION = "paused_needs_inspection"
    PAUSED_NEEDS_STEP = "paused_needs_step"
    PAUSED_AFTER_STEP_NEEDS_STACK = "paused_after_step_needs_stack"
    READY_FOR_DIAGNOSIS = "ready_for_diagnosis"
    # R5.2: the target exited/failed/terminated during a step/next control
    # action (crash-on-step bug class).  The real terminal observation (exit
    # code / error) plus the real reproduction failure output are the
    # diagnosis evidence; no second PAUSED pause can exist by construction.
    PAUSED_AFTER_TERMINAL_STEP = "paused_after_terminal_step"
    CONSUMED_OR_ENDED = "consumed_or_ended"


# R2 per-stage commands — ONLY these are legal in RUNTIME_EVIDENCE.
_R2_STAGE_COMMANDS: dict[R2Stage, frozenset[str]] = {
    R2Stage.NOT_STARTED: frozenset({"break", "failed"}),
    R2Stage.PAUSED_NEEDS_STACK: frozenset({"stack", "failed"}),
    R2Stage.PAUSED_NEEDS_INSPECTION: frozenset({"locals", "print", "failed"}),
    R2Stage.PAUSED_NEEDS_STEP: frozenset({"step", "next", "failed"}),
    R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK: frozenset({"stack", "failed"}),
    R2Stage.READY_FOR_DIAGNOSIS: frozenset({"diagnosis", "failed"}),
    R2Stage.PAUSED_AFTER_TERMINAL_STEP: frozenset({"diagnosis", "failed"}),
    R2Stage.CONSUMED_OR_ENDED: frozenset({"diagnosis", "failed"}),
}

# R3 PATCH checkpoint — bounded to patch+failed only (not syntax/validate/understand).
_R3_PATCH_COMMANDS: frozenset[str] = frozenset({"patch", "failed"})

# R3.1 PATCH progression — first PATCH turn forces a genuine repair attempt.
class R3PatchStage(str, Enum):
    """Bounded PATCH checkpoint progression (R3.1)."""

    NEEDS_FIRST_REPAIR = "patch_needs_first_repair"  # only `patch`
    RETRY = "patch_retry"                            # patch | failed


# R3.1 per-stage commands in ControllerState.PATCH.
_R3_PATCH_STAGE_COMMANDS: dict[R3PatchStage, frozenset[str]] = {
    R3PatchStage.NEEDS_FIRST_REPAIR: frozenset({"patch"}),
    R3PatchStage.RETRY: frozenset({"patch", "failed"}),
}


def patch_diff_affordance(module_path: str) -> str:
    """Local diff affordance shown immediately before final output instruction.

    The example diff names the per-task writable production path (public task
    data); no oracle or test metadata is involved.
    """
    return (
        "Required response now:\n"
        "patch\n"
        f"--- a/{module_path}\n"
        f"+++ b/{module_path}\n"
        "@@ ...\n"
        " <context>\n"
        "-<old line>\n"
        "+<new line>\n"
        "\n"
        "Produce your best minimal repair using the source, debugger observations, "
        "and your diagnosis above."
    )


# ---------------------------------------------------------------------------
# R5.2 mechanical crash summary from the real failure output (non-oracle)
# ---------------------------------------------------------------------------

_CRASH_SUMMARY_LINE_RE = re.compile(
    r"^(.+?\.py):(\d+):\s*([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))"
)
_EXCEPTION_LINE_RE = re.compile(r"^E\s+(.+)$")


def crash_summary_from_failure_output(
    failure_output: str, script_path: str
) -> Optional[str]:
    """Mechanically summarize the crash point from the REAL reproduction
    output (pytest failure report): the last ``<script>:<line>: <ErrorType>``
    summary line for the production script plus the last ``E ...`` exception
    line.  Returns ``None`` (fail closed) when no such evidence exists."""
    if type(failure_output) is not str or not failure_output:
        return None
    if type(script_path) is not str or not script_path:
        return None
    normalized_script = script_path.replace("\\", "/")
    location: Optional[str] = None
    exception_line: Optional[str] = None
    for line in failure_output.splitlines():
        stripped = line.strip()
        match = _CRASH_SUMMARY_LINE_RE.match(stripped)
        if match is not None:
            path = match.group(1).replace("\\", "/")
            if path.endswith(normalized_script) or normalized_script.endswith(path):
                location = (
                    f"{match.group(1)}:{match.group(2)}: {match.group(3)}"
                )
        exception_match = _EXCEPTION_LINE_RE.match(stripped)
        if exception_match is not None:
            exception_line = exception_match.group(1).strip()
    if location is None:
        return None
    if exception_line:
        return f"{location} — {exception_line}"
    return location

_FENCE_LINE_RE = re.compile(r"^```[A-Za-z0-9_+.-]*[ \t]*$")

@dataclass(frozen=True)
class FenceUnwrapRecord:
    """Explicit machine-checkable record of a deterministic fence unwrap."""

    unwrapped: bool
    fence_language: Optional[str] = None
    closing_fence_present: bool = False
    trailing_prose_bytes: int = 0
    synthesized_patch_command: bool = False
    shape: str = "none"  # "bare_fence" | "patch_plus_fence"
    content_sha256: Optional[str] = None
    original_sha256: Optional[str] = None

    def to_mapping(self) -> dict[str, object]:
        return {
            "unwrapped": self.unwrapped,
            "fence_language": self.fence_language,
            "closing_fence_present": self.closing_fence_present,
            "trailing_prose_bytes": self.trailing_prose_bytes,
            "synthesized_patch_command": self.synthesized_patch_command,
            "shape": self.shape,
            "content_sha256": self.content_sha256,
            "original_sha256": self.original_sha256,
        }


def unwrap_single_fence(raw_text: str) -> tuple[str, Optional[FenceUnwrapRecord]]:
    """Deterministically unwrap exactly one markdown code fence.

    ``raw_text`` is returned unchanged (record ``None``) when it does not
    start with a fence line or with ``patch`` + a fence line, so the normal
    parser handles it.  Two deterministic shapes are supported:

    - ``bare_fence``: the whole response is one fenced block; the content
      between the fences becomes the parse text (a ``patch`` command line is
      synthesized mechanically when the content begins with ``---``);
    - ``patch_plus_fence``: the response starts with the ``patch`` command
      line followed by one fenced block containing the diff.

    For both shapes: exactly one closing fence line must exist, trailing
    prose after the closing fence is discarded (byte count recorded), and
    any shape that does not fit fails closed (the original text is returned
    unchanged and the normal parser rejects it).

    The unwrap never invents or changes code semantics — it only removes
    deterministic markdown framing around a model-authored command/diff.
    """
    if type(raw_text) is not str:
        return raw_text, None
    stripped = raw_text.strip()
    if not stripped:
        return raw_text, None
    lines = stripped.split("\n")

    shape = "none"
    body_start = 0
    first_line = lines[0].strip()
    fence_match = _FENCE_LINE_RE.match(first_line)
    if fence_match is not None:
        shape = "bare_fence"
        body_start = 1
    elif first_line.lower() == "patch" and len(lines) >= 2:
        second_line = lines[1].strip()
        if _FENCE_LINE_RE.match(second_line) is not None:
            shape = "patch_plus_fence"
            body_start = 2
    if shape == "none":
        return raw_text, None

    if shape == "bare_fence":
        fence_language = first_line[3:].strip() or None
    else:
        fence_language = lines[1].strip()[3:] or None

    # Find the closing fence (strictly after the opening line).
    closing_index: Optional[int] = None
    for index in range(body_start, len(lines)):
        if _FENCE_LINE_RE.match(lines[index].strip()):
            closing_index = index
            break
    if closing_index is None:
        return raw_text, None
    content = "\n".join(lines[body_start:closing_index])
    trailing = lines[closing_index + 1:]
    trailing_text = "\n".join(trailing).strip()
    # Fail closed if another fence appears in the trailing region or the
    # content is empty.
    if any(_FENCE_LINE_RE.match(line.strip()) for line in trailing):
        return raw_text, None
    if not content.strip():
        return raw_text, None

    synthesized = False
    parse_text = content
    if shape == "bare_fence":
        if content.lstrip().startswith("---"):
            parse_text = "patch\n" + content
            synthesized = True
    else:
        parse_text = "patch\n" + content

    import hashlib as _hashlib
    return parse_text, FenceUnwrapRecord(
        unwrapped=True,
        fence_language=fence_language,
        closing_fence_present=True,
        trailing_prose_bytes=len(trailing_text.encode("utf-8")),
        synthesized_patch_command=synthesized,
        shape=shape,
        content_sha256=_hashlib.sha256(parse_text.encode("utf-8")).hexdigest(),
        original_sha256=_hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
    )


def _r2_lifecycle_for_stage(stage: R2Stage) -> DebuggerLifecycle:
    if stage is R2Stage.NOT_STARTED:
        return DebuggerLifecycle.NOT_STARTED
    if stage is R2Stage.CONSUMED_OR_ENDED:
        return DebuggerLifecycle.CONSUMED_OR_ENDED
    if stage is R2Stage.PAUSED_AFTER_TERMINAL_STEP:
        # The target has ended; only the terminal observation remains.
        return DebuggerLifecycle.CONSUMED_OR_ENDED
    return DebuggerLifecycle.PAUSED


# ---------------------------------------------------------------------------
# State-specific command surface (base, before lifecycle filter) — legacy
# ---------------------------------------------------------------------------

_STATE_COMMANDS: dict[ControllerState, frozenset[str]] = {
    ControllerState.REPRODUCE: frozenset({"reproduce", "understand", "failed"}),
    ControllerState.UNDERSTAND: frozenset(
        {"source", "diagnosis", "failed"} | {"runtime", "patch"}
    ),
    ControllerState.RUNTIME_EVIDENCE: frozenset(
        {
            "break", "stack", "locals", "print", "step", "next",
            "continue", "stop", "source", "diagnosis",
            "understand", "patch", "failed",
        }
    ),
    # R3 PATCH is bounded to patch+failed (capability = repair generation only)
    ControllerState.PATCH: frozenset({"patch", "failed"}),
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
    """Legacy helper — delegates to R2 staged view in RUNTIME_EVIDENCE."""

    if state is not ControllerState.RUNTIME_EVIDENCE:
        base = _STATE_COMMANDS.get(state, frozenset())
        return tuple(sorted(base))
    # Map legacy lifecycle to R2 stage best-effort for old callers
    if lifecycle is DebuggerLifecycle.NOT_STARTED:
        return visible_commands_r2(state, R2Stage.NOT_STARTED)
    if lifecycle is DebuggerLifecycle.PAUSED:
        # generic paused — return the widest staged view that is still
        # stage-aware (needs_stack is the earliest)
        return visible_commands_r2(state, R2Stage.PAUSED_NEEDS_STACK)
    return visible_commands_r2(state, R2Stage.CONSUMED_OR_ENDED)


def visible_commands_r2(
    state: ControllerState,
    r2_stage: R2Stage,
) -> tuple[str, ...]:
    """Return the commands legal in ``state`` ∩ ``r2_stage``."""

    if state is not ControllerState.RUNTIME_EVIDENCE:
        base = _STATE_COMMANDS.get(state, frozenset())
        return tuple(sorted(base))
    cmds = _R2_STAGE_COMMANDS.get(r2_stage, frozenset())
    return tuple(sorted(cmds))


def visible_commands_r3_patch(
    patch_stage: R3PatchStage,
) -> tuple[str, ...]:
    """Return the commands legal in ControllerState.PATCH for an R3.1 stage."""
    cmds = _R3_PATCH_STAGE_COMMANDS.get(patch_stage, frozenset())
    return tuple(sorted(cmds))


# ---------------------------------------------------------------------------
# Breakpoint eligibility (identical to R1/R3 — non-oracle)
# ---------------------------------------------------------------------------


def _collect_traceable_lines(source: str) -> frozenset[int]:
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
    traceable = _collect_traceable_lines(source)
    defs = _module_def_lines(source)
    return tuple(sorted(traceable - defs))


def format_source_with_lines(
    source: str,
    eligible: Sequence[int],
) -> str:
    eligible_set = frozenset(eligible)
    lines = source.splitlines()
    parts: list[str] = []
    for i, line in enumerate(lines, 1):
        marker = ">" if i in eligible_set else " "
        parts.append(f"{marker} {i:3d}: {line}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Observation helpers
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
# Text validation
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
# Core parse function — R2 staged (accepted R3 semantics)
# ---------------------------------------------------------------------------


def parse(
    raw_text: str,
    state: ControllerState,
    last_observation: Optional[Observation] = None,
    lifecycle: Optional[DebuggerLifecycle] = None,
    r2_stage: Optional[R2Stage] = None,
    patch_stage: Optional[R3PatchStage] = None,
) -> BridgeResult:
    """Parse a model response into a typed directive.

    When ``r2_stage`` is supplied and state is RUNTIME_EVIDENCE, the R2 staged
    action mask is enforced.  When ``patch_stage`` is supplied and state is
    PATCH, the R3.1 PATCH stage mask is enforced (first turn: only ``patch``;
    retry: ``patch`` | ``failed``).  A hidden command is rejected with
    COMMAND_NOT_IN_LIFECYCLE before reaching the tool layer.
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

    # R2 staged mask — takes precedence when supplied
    if r2_stage is not None and state is ControllerState.RUNTIME_EVIDENCE:
        staged_commands = set(visible_commands_r2(state, r2_stage))
        if token not in staged_commands:
            raise BridgeParseError(
                BridgeRejection.COMMAND_NOT_IN_LIFECYCLE,
                f"command {token!r} is not available in R2 stage "
                f"{r2_stage.value}; choose one of: "
                f"{', '.join(sorted(staged_commands))}",
            )
    # R3.1 PATCH stage mask — first PATCH turn only `patch` (no `failed`)
    elif patch_stage is not None and state is ControllerState.PATCH:
        patch_commands = set(visible_commands_r3_patch(patch_stage))
        if token not in patch_commands:
            raise BridgeParseError(
                BridgeRejection.COMMAND_NOT_IN_LIFECYCLE,
                f"command {token!r} is not available in PATCH stage "
                f"{patch_stage.value}; choose one of: "
                f"{', '.join(sorted(patch_commands))}",
            )
    # Legacy lifecycle mask (for non-R2 callers / compatibility)
    elif lifecycle is not None and state is ControllerState.RUNTIME_EVIDENCE:
        lifecycle_commands = set(visible_commands(state, lifecycle))
        if token not in lifecycle_commands:
            raise BridgeParseError(
                BridgeRejection.COMMAND_NOT_IN_LIFECYCLE,
                f"command {token!r} is not available while debugger lifecycle "
                f"is {lifecycle.value}; choose one of: "
                f"{', '.join(sorted(lifecycle_commands))}",
            )

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

    # --- Diagnosis -> PATCH (R3 fix: not self-transition) ---
    if token == "diagnosis":
        if state is not ControllerState.RUNTIME_EVIDENCE:
            raise BridgeParseError(
                BridgeRejection.COMMAND_NOT_IN_STATE,
                f"diagnosis is not available in state {state.value}",
            )
        # Diagnosis is only legal in READY_FOR_DIAGNOSIS or the R5.2 terminal
        # stage (r2_stage check already enforced the lifecycle mask above).
        # Double-check stage if provided.
        if r2_stage is not None and r2_stage not in (
            R2Stage.READY_FOR_DIAGNOSIS,
            R2Stage.PAUSED_AFTER_TERMINAL_STEP,
        ):
            raise BridgeParseError(
                BridgeRejection.COMMAND_NOT_IN_LIFECYCLE,
                "diagnosis is only available after completing the full "
                f"debugger chain (current stage: {r2_stage.value})",
            )
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
        # R3: transitions RuntimeEvidence -> PATCH (bounded repair checkpoint)
        if not is_transition_allowed(state, ControllerState.PATCH):
            raise BridgeParseError(
                BridgeRejection.ILLEGAL_TRANSITION,
                f"transition from {state.value} to Patch is not allowed",
            )
        directive = TransitionDirective(ControllerState.PATCH, text)
        return BridgeResult(
            command_token="diagnosis",
            normalized_command=f"diagnosis {text[:120]}",
            directive=directive,
            is_diagnosis=True,
            diagnosis_text=text,
        )

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
# System prompt — frozen template, per-task production path substitution
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = (
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
    "  diagnosis <text>   — record your root-cause diagnosis after debugging\n"
    "  patch              — apply a unified diff (followed by diff lines)\n"
    "  reproduce/understand/runtime/patch/validate\n"
    "                     — transition to a different phase\n"
    "  done               — signal completion\n"
    "  failed             — signal failure\n\n"
    "Patch format (when in Patch phase, emit exactly):\n"
    "  patch\n"
    "  --- a/{module_path}\n"
    "  +++ b/{module_path}\n"
    "  @@ -... +... @@\n"
    "   <context>\n"
    "  -<removed>\n"
    "  +<added>\n\n"
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
    "  - If the program exits or crashes during 'step' or 'next', the\n"
    "    debugger session ends: that exit is real terminal runtime evidence.\n"
    "    Record your diagnosis from the real failure output and the locals\n"
    "    you already observed.\n"
    "  - After debugging, use 'diagnosis <text>' to record your diagnosis.\n"
    "    After diagnosis you will enter the Patch phase — then emit 'patch'\n"
    "    with a unified diff.\n"
    "  - The 'patch' diff may optionally be wrapped in exactly one markdown\n"
    "    code fence (``` or ```diff ... ```); the controller unwraps a single\n"
    "    fence deterministically.  A plain diff without a fence is also\n"
    "    accepted.\n"
)


def build_system_prompt(module_path: str) -> str:
    """Return the frozen system prompt with the per-task writable production
    path substituted into the patch-format example only."""
    return SYSTEM_PROMPT_TEMPLATE.format(module_path=module_path)


# ---------------------------------------------------------------------------
# Prompt rendering — R2 staged
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DebuggerContext:
    script_path: Optional[str] = None
    source_text: Optional[str] = None
    eligible_lines: tuple[int, ...] = ()
    lifecycle: DebuggerLifecycle = DebuggerLifecycle.NOT_STARTED
    r2_stage: Optional[R2Stage] = None
    paused_line: Optional[int] = None
    paused_function: Optional[str] = None
    # R3: retained diagnosis + correlated runtime slice for PATCH prompt
    retained_diagnosis: Optional[str] = None
    runtime_slice: Optional[dict[str, str]] = None  # rendered observation texts


def render_prompt(
    state: ControllerState,
    last_observation: Optional[Observation],
    task_description: str,
    feedback: Optional[str] = None,
    *,
    debugger: Optional[DebuggerContext] = None,
    patch_stage: Optional[R3PatchStage] = None,
) -> str:
    if debugger is not None and debugger.r2_stage is not None:
        r2_stage = debugger.r2_stage
        lifecycle = _r2_lifecycle_for_stage(r2_stage)
        # R3 PATCH is bounded regardless of r2_stage; R3.1 stage mask applies
        if state is ControllerState.PATCH:
            if patch_stage is not None:
                commands = visible_commands_r3_patch(patch_stage)
            else:
                commands = tuple(sorted(_R3_PATCH_COMMANDS))
        else:
            commands = visible_commands_r2(state, r2_stage)
    else:
        lifecycle = (
            debugger.lifecycle if debugger is not None
            else DebuggerLifecycle.NOT_STARTED
        )
        if state is ControllerState.PATCH:
            if patch_stage is not None:
                commands = visible_commands_r3_patch(patch_stage)
            else:
                commands = tuple(sorted(_R3_PATCH_COMMANDS))
        else:
            commands = visible_commands(state, lifecycle)
        r2_stage = None

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

    if state is ControllerState.RUNTIME_EVIDENCE and debugger is not None:
        eff_stage = r2_stage if r2_stage is not None else None
        if eff_stage is not None:
            if eff_stage is R2Stage.NOT_STARTED:
                parts.append("Debugger: no active PDB session (you may start one with 'break')")
            elif eff_stage is R2Stage.PAUSED_NEEDS_STACK:
                line_info = ""
                if debugger.paused_line is not None and debugger.paused_function is not None:
                    line_info = f" at line {debugger.paused_line} in function '{debugger.paused_function}'"
                parts.append(f"Debugger: PDB session paused{line_info} — next: inspect stack with 'stack'")
            elif eff_stage is R2Stage.PAUSED_NEEDS_INSPECTION:
                parts.append("Debugger: PDB session paused — next: inspect variables with 'locals' or 'print <expr>'")
            elif eff_stage is R2Stage.PAUSED_NEEDS_STEP:
                parts.append("Debugger: PDB session paused — next: single-step with 'step' or 'next'")
            elif eff_stage is R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK:
                line_info = ""
                if debugger.paused_line is not None and debugger.paused_function is not None:
                    line_info = f" at line {debugger.paused_line} in function '{debugger.paused_function}'"
                parts.append(f"Debugger: PDB session paused{line_info} — next: refresh stack with 'stack'")
            elif eff_stage is R2Stage.READY_FOR_DIAGNOSIS:
                parts.append("Debugger: PDB session paused — ready for diagnosis with 'diagnosis <text>'")
            elif eff_stage is R2Stage.PAUSED_AFTER_TERMINAL_STEP:
                line_info = ""
                if debugger.paused_line is not None and debugger.paused_function is not None:
                    line_info = f" (was paused at line {debugger.paused_line} in function '{debugger.paused_function}')"
                parts.append(
                    f"Debugger: PDB session ended{line_info} — the target "
                    "exited or crashed during the last step/next and no "
                    "further pause is possible.  Use the real failure "
                    "evidence below and the observed locals as terminal "
                    "runtime evidence, then record your diagnosis with "
                    "'diagnosis <text>'."
                )
                crash_summary = (
                    debugger.runtime_slice.get("crash_summary")
                    if debugger.runtime_slice else None
                )
                if type(crash_summary) is str and crash_summary:
                    parts.append(f"Terminal failure evidence: {crash_summary}")
                if debugger.runtime_slice:
                    slice_lines = []
                    for key in ("reproduction", "inspection", "step"):
                        val = debugger.runtime_slice.get(key)
                        if val:
                            slice_lines.append(f"[{key}]\n{val}")
                    if slice_lines:
                        parts.append(
                            "Correlated terminal runtime observations (bounded, from real debugger):\n"
                            + "\n\n".join(slice_lines)
                        )
            else:
                parts.append(
                    "Debugger: PDB session ended (one session per case — 'break' is no longer available)"
                )
        else:
            # legacy fallback
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

    # Source affordance — RUNTIME_EVIDENCE only when no session active
    if (
        state is ControllerState.RUNTIME_EVIDENCE
        and debugger is not None
        and debugger.script_path is not None
        and debugger.source_text is not None
        and (r2_stage is R2Stage.NOT_STARTED if r2_stage is not None else lifecycle is DebuggerLifecycle.NOT_STARTED)
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

    # R3 PATCH context: source + retained diagnosis + correlated runtime slice
    if state is ControllerState.PATCH and debugger is not None:
        # Source is always shown in PATCH (model needs it to author diff)
        if debugger.source_text is not None and debugger.script_path is not None:
            formatted = format_source_with_lines(
                debugger.source_text, debugger.eligible_lines or ()
            )
            parts.append(
                f"\nTarget script: {debugger.script_path}\n"
                f"Source:\n{formatted}"
            )
        parts.append(
            "\nDebugger interaction checkpoint complete. "
            "No further debugger commands are available in Patch phase."
        )
        if debugger.retained_diagnosis:
            parts.append(
                f"\nYour diagnosis (from debugger evidence):\n{debugger.retained_diagnosis}"
            )
        if debugger.runtime_slice:
            slice_lines = []
            for key in ("crash_summary", "reproduction", "stack_G1", "inspection", "step", "stack_G2"):
                val = debugger.runtime_slice.get(key)
                if val:
                    if key == "crash_summary":
                        slice_lines.append(f"[terminal failure evidence]\n{val}")
                    else:
                        slice_lines.append(f"[{key}]\n{val}")
            if slice_lines:
                parts.append(
                    "\nCorrelated runtime observations (bounded, from real debugger):\n"
                    + "\n\n".join(slice_lines)
                )

    parts.append(f"\nTask:\n{task_description}")

    if obs_text:
        parts.append(f"\nLast observation:\n{obs_text}")
    if feedback:
        parts.append(f"\nPrevious response was rejected: {feedback}")
    if state is ControllerState.PATCH:
        # R3.1 local diff affordance — concise, non-oracle, right before output
        module_path = debugger.script_path if debugger is not None else ""
        parts.append("\n" + patch_diff_affordance(module_path or "target.py"))
    parts.append(
        "\nEmit exactly one command from the available list above. "
        "Do not emit prose, markdown, or JSON."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Observation rendering (identical to R1/R3, plus R5 stack region filtering)
# ---------------------------------------------------------------------------


def _render_observation(
    observation: Optional[Observation],
    *,
    filter_scripts: Optional[frozenset[str]] = None,
    original_line_count: Optional[int] = None,
) -> str:
    """Render an observation for the model.

    ``filter_scripts`` + ``original_line_count`` restrict stack-frame
    rendering to production-script frames inside the original source region.
    This is render-only: raw payload frame ids are never renumbered and the
    full raw payload remains in stored evidence.
    """
    if observation is None:
        return ""

    name = observation.name
    payload = observation.payload if type(observation.payload) is dict else {}
    status = observation.status.value if hasattr(observation.status, "value") else str(observation.status)
    summary = observation.summary

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
            shown = 0
            raw_count = len(frames)
            for frame in frames:
                if type(frame) is dict:
                    script = frame.get("script")
                    line = frame.get("line")
                    if filter_scripts is not None:
                        if script not in filter_scripts:
                            continue
                        if (
                            original_line_count is not None
                            and not (type(line) is int and 1 <= line <= original_line_count)
                        ):
                            continue
                    marker = "* " if frame.get("is_current") else "  "
                    lines.append(
                        f"  {marker}frame_id={frame.get('frame_id')} "
                        f"{frame.get('function')} line={frame.get('line')} "
                        f"script={frame.get('script')}"
                    )
                    shown += 1
            if filter_scripts is not None:
                lines.append(
                    f"  (stack rendering filtered to the target production "
                    f"region; {shown} frame(s) shown of {raw_count} raw)"
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
            lines.append(
                "  Terminal: no further pause is available; use the real "
                "reproduction failure output and the observed locals to "
                "determine the crash point."
            )
        elif state_val == "failed":
            error = payload.get("error")
            lines.append(
                f"  Execution failed: {error if error else 'unknown target error'}"
            )
            lines.append(
                "  Terminal: no further pause is available; use the real "
                "reproduction failure output and the observed locals to "
                "determine the crash point."
            )
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
        failure_output = payload.get("failure_output")
        if type(failure_output) is str and failure_output:
            lines.append("  Real failure output (from the reproduction run):")
            for failure_line in failure_output.splitlines():
                lines.append(f"    {failure_line}")
    elif name == "get_source_window":
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
        adjustments = payload.get("hunk_adjustments")
        if adjustments:
            lines.append(f"  hunk_adjustments={adjustments}")
        feedback = payload.get("verifier_feedback")
        if type(feedback) is dict:
            if feedback.get("error"):
                lines.append(
                    f"  Real verifier error: {feedback.get('error')}"
                )
            else:
                lines.append(
                    "  Real verifier (independent EvaluationVerifier): "
                    f"status={feedback.get('status')} "
                    f"outcome={feedback.get('outcome')} "
                    f"f2p={feedback.get('f2p_passed')}/{feedback.get('f2p_total')} "
                    f"p2p={feedback.get('p2p_passed')}/{feedback.get('p2p_total')} "
                    f"full_suite={feedback.get('full_suite')} "
                    f"syntax={feedback.get('syntax')}"
                )
                failures = feedback.get("failures") or []
                if failures:
                    lines.append("  Failing checks:")
                    for failure in failures[:3]:
                        lines.append(
                            f"    [{failure.get('kind')}] {failure.get('node_id')} "
                            f"({failure.get('status')})"
                        )
                        detail = failure.get("detail")
                        if type(detail) is str and detail:
                            for detail_line in detail.splitlines()[-14:]:
                                lines.append(f"      {detail_line}")
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
    "FenceUnwrapRecord",
    "R2Stage",
    "SYSTEM_PROMPT_TEMPLATE",
    "breakpoint_eligible_lines",
    "build_system_prompt",
    "format_source_with_lines",
    "parse",
    "patch_diff_affordance",
    "render_prompt",
    "unwrap_single_fence",
    "visible_commands",
    "visible_commands_r2",
]
