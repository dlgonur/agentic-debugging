"""Demo tool diagnostics, validation helpers, and bounded outputs.

This module owns the shared bounded-safe helper layer beneath the demo
tool registry: the diagnostic/output bounds, phase legality and
validation-readiness checks, the safe rejection wrapper, observation-id
derivation, bounded diagnostic text construction (path-normalized),
JSON-safe payload conversion, the task target module path, reproduction
failure output projection, the argument validator, and manifest pytest
argv rebuilding.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolExecutionError, ToolRejectedError
from agentic_debugger.demo.sanitize import sanitize_failure_output
from agentic_debugger.evaluation.runner import bounded_error, normalize_output
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.events.schema import Action

SOURCE_WINDOW_RADIUS = 6
#: The exact lowest-rung proof exposes one complete small target function so
#: breakpoint selection and diagnosis are based on public source, not guesses.
EXACT_PROOF_SOURCE_WINDOW_RADIUS = 12


def legal_reproduction_phases(state: ControllerState) -> tuple[str, ...]:
    """Return the phase values accepted by run_reproduction in a state."""

    if state is ControllerState.REPRODUCE:
        return ("baseline",)
    if state is ControllerState.VALIDATE:
        return ("post_patch",)
    return ()


def validation_classification_ready(
    post_patch_f2p_passed: object,
    regression_passed: object,
) -> bool:
    """Return whether both required Validate evidence values have been collected.

    ``False`` is collected evidence (the check failed).  Only ``None`` means
    the corresponding evidence has not been gathered yet.  This helper never
    invents a pass/fail value.
    """

    return post_patch_f2p_passed is not None and regression_passed is not None

#: Maximum characters of a bounded diagnostic retained for reporting.
MAX_DIAGNOSTIC_CHARS = 400

#: Maximum characters of the RAW reproduction failure output retained in
#: the evidence payload (audit-only; never rendered into a model prompt).
MAX_RAW_FAILURE_OUTPUT_CHARS = 4000

#: Tail window of a failing-test record output fed back to the model after a
#: real verifier run (the exception/assertion summary is at the end).
MAX_VERIFIER_FAILURE_DETAIL_CHARS = 900


def _safe_rejection(message: str) -> ToolRejectedError:
    return ToolRejectedError(message, safe_diagnostic=message)


class DemoToolError(RuntimeError):
    """Raised for demonstration harness misuse rather than tool failure."""


def _observation_id_for_action(action: Action) -> str:
    """Derive the controller's detached observation id for this action."""

    prefix = "action-"
    if not action.action_id.startswith(prefix):
        raise DemoToolError("controller action id is not canonical")
    suffix = action.action_id[len(prefix):]
    if not suffix.isdigit():
        raise DemoToolError("controller action id has no numeric observation index")
    return "observation-" + suffix


def bounded_diagnostic_text(text: str, workspace_root: Optional[str] = None) -> str:
    """Apply the established bounded-diagnostic pipeline to ready text.

    The ``bounded_diagnostic`` pipeline after exception formatting
    (normalize/bound, control-character strip, ``MAX_DIAGNOSTIC_CHARS``
    cut).  Split out so callers that must transform the FULL text first
    (e.g. redact before any cut) can reuse the identical bounding.
    """
    text = normalize_output(text, workspace_root)
    text = "".join(char if 0x20 <= ord(char) != 0x7F else " " for char in text).strip()
    if len(text) > MAX_DIAGNOSTIC_CHARS:
        text = text[: MAX_DIAGNOSTIC_CHARS - 3] + "..."
    return text or "tool failure"


def bounded_diagnostic(exc: BaseException, workspace_root: Optional[str] = None) -> str:
    """Bound a diagnostic and strip disposable workspace paths out of it.

    Diagnostics land in the deterministic section of the demonstration result
    document, so a raw ``PermissionError`` naming a ``mkdtemp`` directory would
    make that section unstable.  Normalisation reuses the accepted verifier
    helper so the demo and the verifier redact identically.
    """

    return bounded_diagnostic_text(bounded_error(exc), workspace_root)


def _json_safe(value: Any, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ToolExecutionError(f"{label} is not JSON-compatible") from exc


def _bounded_tail(text: str, maximum: int) -> str:
    """Keep the tail of ``text`` at most ``maximum`` characters."""
    if len(text) <= maximum:
        return text
    marker = "... [output truncated] ...\n"
    return marker + text[-(maximum - len(marker)):]


def task_target_module_path(task: DebugTask) -> str:
    """Mechanically select the single writable production module.

    Uses the public task constraint ``constraints.allowed_write_paths``:
    exactly one writable ``.py`` path outside ``tests``.  Identical rule to
    the R5 launcher (reported design choice; no oracle data).
    """
    allowed = list(task.constraints.allowed_write_paths)
    candidates = [p for p in allowed if p.endswith(".py") and not p.startswith("tests/")]
    if len(candidates) != 1:
        raise DemoToolError(
            "task must declare exactly one writable production .py path, "
            f"got {sorted(allowed)!r}"
        )
    return candidates[0]


def reproduction_failure_output(
    result: Any,
    workspace_root: Optional[str],
    script_path: str,
    original_line_count: int,
) -> str:
    """SANITIZED reproduction diagnostic of the executed test command.

    ``result`` is a ``TestRunResult``.  The raw stdout/stderr of the
    executed command is consumed by the common deterministic sanitizer
    (``sanitize.sanitize_failure_output``), which derives the bounded
    structured production diagnostic and never forwards hidden-test
    content (test source, assertions, node ids, literals).  Empty when the
    command produced nothing.
    """
    command = result.command_result
    raw = (command.stdout or "") + "\n" + (command.stderr or "")
    if not raw.strip():
        return ""
    diagnostic = sanitize_failure_output(
        raw, workspace_root, script_path, original_line_count
    )
    return diagnostic.text


def reproduction_failure_output_raw(
    result: Any, workspace_root: Optional[str]
) -> str:
    """Bounded, normalized RAW failure output — evidence only.

    Retained for auditability of the sanitizer's mechanical derivation;
    never rendered into a model prompt.
    """
    command = result.command_result
    raw = (command.stdout or "") + "\n" + (command.stderr or "")
    if not raw.strip():
        return ""
    normalized = normalize_output(raw, workspace_root)
    return _bounded_tail(normalized, MAX_RAW_FAILURE_OUTPUT_CHARS)


def _validator(
    required: dict[str, type],
    optional: Optional[dict[str, type]] = None,
    *,
    enums: Optional[dict[str, tuple[object, ...]]] = None,
    minimums: Optional[dict[str, int]] = None,
) -> Callable[[dict[str, object]], dict[str, object]]:
    """Build a strict argument validator that rejects unknown keys."""

    optional = optional or {}
    enums = enums or {}
    minimums = minimums or {}
    known = set(required) | set(optional)

    def validate(arguments: dict[str, object]) -> dict[str, object]:
        if type(arguments) is not dict:
            raise _safe_rejection("arguments must be a mapping")
        unknown = sorted(set(arguments) - known)
        if unknown:
            raise _safe_rejection(f"unknown argument: {unknown[0]}")
        missing = sorted(set(required) - set(arguments))
        if missing:
            raise _safe_rejection(f"missing argument: {missing[0]}")
        for name, expected in {**required, **optional}.items():
            if name not in arguments:
                continue
            value = arguments[name]
            if type(value) is not expected:
                raise _safe_rejection(f"argument {name} has the wrong type")
            if expected is str and not value:
                raise _safe_rejection(f"argument {name} must be non-empty")
            if expected is int and value < 0:
                raise _safe_rejection(f"argument {name} must be non-negative")
            if expected is int and name in minimums and value < minimums[name]:
                raise _safe_rejection(
                    f"argument {name} must be at least {minimums[name]}"
                )
            if name in enums and value not in enums[name]:
                raise _safe_rejection(f"argument {name} has an unsupported value")
        return dict(arguments)

    def type_name(expected: type) -> str:
        return {str: "string", int: "integer", bool: "boolean"}.get(
            expected, expected.__name__
        )

    properties = {}
    for name, expected in {**required, **optional}.items():
        constraint = {"type": type_name(expected)}
        if expected is str:
            constraint["min_length"] = 1
        if expected is int:
            constraint["minimum"] = minimums.get(name, 0)
        if name in enums:
            constraint["enum"] = list(enums[name])
        properties[name] = constraint
    validate.argument_contract = {  # type: ignore[attr-defined]
        "required": list(required),
        "properties": properties,
        "additional_properties": False,
    }
    return validate


def pytest_argv(base: Sequence[str], node_ids: Sequence[str]) -> list[str]:
    """Rebuild a manifest pytest argv for an explicit set of node ids."""

    argv: list[str] = []
    replaced = False
    for item in base:
        if "::" in item:
            if not replaced:
                argv.extend(node_ids)
                replaced = True
            continue
        argv.append(item)
    if not replaced:
        argv.extend(node_ids)
    return argv
