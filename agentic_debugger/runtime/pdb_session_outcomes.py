"""Execution-outcome validation authority for the PDB session.

Owns strict schemas for one-shot run results, persistent paused-target
outcomes (start/continue/step/next), target status and termination.
All functions are pure: session lifecycle snapshots (budget flag, active
script/breakpoints) are passed explicitly so the session remains the sole
mutable owner of that state.

Dependency direction: depends on ``pdb_session_limits`` and
``pdb_session_validation`` (shared primitives).  Control helpers depend on
this module; it depends on neither control nor transport nor session.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from agentic_debugger.runtime.exceptions import PdbProtocolError
from agentic_debugger.runtime.pdb_protocol import PdbResponse
from agentic_debugger.runtime.pdb_session_limits import (
    _EXITED_RESULT_FIELDS,
    _MAX_RESULT_ERROR_UTF8,
    _MAX_RESULT_FUNCTION_UTF8,
    _PAUSED_RESULT_FIELDS,
    _PUBLIC_TARGET_STATES,
    _STATUS_EXITED_FIELDS,
    _STATUS_FAILED_FIELDS,
    _STATUS_IDLE_FIELDS,
    _STATUS_PAUSED_FIELDS,
    _STATUS_TERMINATED_FIELDS,
    _TERMINATED_RESULT_FIELDS,
)
from agentic_debugger.runtime.pdb_session_validation import (
    check_exact_fields,
    validate_bounded_protocol_string,
    validate_int_strict_field,
    validate_result_script,
)


def validate_run_result(
    response: PdbResponse,
    expected_script: str = "",
    expected_breakpoints: Sequence[int] = (),
) -> None:
    result = response.result
    if not isinstance(result, dict):
        raise PdbProtocolError(
            "run_to_breakpoint result must be a mapping"
        )

    status = result.get("status")
    if status == "breakpoint":
        required = {"status", "script", "line", "function"}
        extra = set(result.keys()) - required
        if extra:
            raise PdbProtocolError(
                f"Unknown fields in breakpoint result: "
                f"{sorted(extra)}"
            )
        missing = required - set(result.keys())
        if missing:
            raise PdbProtocolError(
                f"Missing fields in breakpoint result: "
                f"{sorted(missing)}"
            )
        script = result["script"]
        if not isinstance(script, str) or not script:
            raise PdbProtocolError(
                "breakpoint result script must be a non-empty string"
            )
        if expected_script and script != expected_script:
            raise PdbProtocolError(
                f"breakpoint result script {script!r} does not match "
                f"expected {expected_script!r}"
            )
        line = result["line"]
        if isinstance(line, bool) or not isinstance(line, int):
            raise PdbProtocolError(
                "breakpoint result line must be an integer"
            )
        if line <= 0:
            raise PdbProtocolError(
                "breakpoint result line must be positive"
            )
        if expected_breakpoints and line not in expected_breakpoints:
            raise PdbProtocolError(
                f"breakpoint result line {line} is not among "
                f"requested breakpoints {list(expected_breakpoints)}"
            )
        fn = result["function"]
        if not isinstance(fn, str) or not fn:
            raise PdbProtocolError(
                "breakpoint result function must be a non-empty string"
            )
    elif status == "exited":
        required = {"status", "script", "exit_code"}
        extra = set(result.keys()) - required
        if extra:
            raise PdbProtocolError(
                f"Unknown fields in exited result: "
                f"{sorted(extra)}"
            )
        missing = required - set(result.keys())
        if missing:
            raise PdbProtocolError(
                f"Missing fields in exited result: "
                f"{sorted(missing)}"
            )
        script = result["script"]
        if not isinstance(script, str) or not script:
            raise PdbProtocolError(
                "exited result script must be a non-empty string"
            )
        if expected_script and script != expected_script:
            raise PdbProtocolError(
                f"exited result script {script!r} does not match "
                f"expected {expected_script!r}"
            )
        ec = result["exit_code"]
        if isinstance(ec, bool) or not isinstance(ec, int):
            raise PdbProtocolError(
                "exited result exit_code must be an integer"
            )
    else:
        raise PdbProtocolError(
            f"Unknown status in run_to_breakpoint result: "
            f"{status!r}"
        )


def validate_persistent_outcome_result(
    response: PdbResponse,
    expected_script: str,
    expected_breakpoints: Optional[Sequence[int]],
    operation: str,
) -> str:
    result = response.result
    if not isinstance(result, dict):
        raise PdbProtocolError(
            f"{operation} result must be a mapping"
        )
    state = result.get("state")
    if isinstance(state, bool) or not isinstance(state, str):
        raise PdbProtocolError(
            f"{operation} result state must be a string, "
            f"got {type(state).__name__}"
        )
    _START_VALID_STATES = frozenset({"paused", "exited"})
    if state not in _START_VALID_STATES:
        raise PdbProtocolError(
            f"{operation} result state must be 'paused' "
            f"or 'exited', got {state!r}"
        )
    if state == "paused":
        check_exact_fields(
            result, _PAUSED_RESULT_FIELDS,
            f"{operation} paused result"
        )
        script = validate_result_script(
            result["script"],
            f"{operation} paused result script"
        )
        if script != expected_script:
            raise PdbProtocolError(
                f"{operation} paused result script {script!r} "
                f"does not match expected {expected_script!r}"
            )
        line = validate_int_strict_field(
            result["line"],
            f"{operation} paused result line"
        )
        if line <= 0:
            raise PdbProtocolError(
                f"{operation} paused result line must be positive"
            )
        if (expected_breakpoints is not None and
                line not in expected_breakpoints):
            raise PdbProtocolError(
                f"{operation} paused result line {line} is not "
                f"among requested breakpoints "
                f"{list(expected_breakpoints)}"
            )
        validate_bounded_protocol_string(
            result["function"],
            f"{operation} paused result function",
            _MAX_RESULT_FUNCTION_UTF8,
        )
    elif state == "exited":
        check_exact_fields(
            result, _EXITED_RESULT_FIELDS,
            f"{operation} exited result"
        )
        script = validate_result_script(
            result["script"], f"{operation} exited result script"
        )
        if script != expected_script:
            raise PdbProtocolError(
                f"{operation} exited result script {script!r} "
                f"does not match expected {expected_script!r}"
            )
        validate_int_strict_field(
            result["exit_code"],
            f"{operation} exited result exit_code"
        )
    return state


def validate_start_paused_result(
    response: PdbResponse,
    expected_script: str,
    expected_breakpoints: Sequence[int],
) -> str:
    return validate_persistent_outcome_result(
        response,
        expected_script,
        expected_breakpoints,
        "start_paused_target",
    )


def validate_continue_result(
    response: PdbResponse,
    expected_script: str,
    expected_breakpoints: Optional[Sequence[int]],
) -> str:
    return validate_persistent_outcome_result(
        response,
        expected_script,
        expected_breakpoints,
        "continue_paused_target",
    )


def validate_status_result(
    response: PdbResponse,
    *,
    budget_consumed: bool,
    active_script: Any,
    active_breakpoints: Any,
) -> str:
    result = response.result
    if not isinstance(result, dict):
        raise PdbProtocolError(
            "get_target_status result must be a mapping"
        )
    state = result.get("state")
    if isinstance(state, bool) or not isinstance(state, str):
        raise PdbProtocolError(
            "get_target_status result state must be a string"
        )
    if state not in _PUBLIC_TARGET_STATES:
        raise PdbProtocolError(
            f"Unknown state in get_target_status result: {state!r}"
        )

    if state == "idle":
        check_exact_fields(
            result, _STATUS_IDLE_FIELDS,
            "get_target_status idle result"
        )
        if budget_consumed:
            raise PdbProtocolError(
                "get_target_status returned idle after "
                "execution budget was consumed"
            )
    else:
        if not budget_consumed:
            raise PdbProtocolError(
                f"get_target_status returned {state!r} before "
                "execution budget was consumed"
            )
        if state == "paused":
            check_exact_fields(
                result, _STATUS_PAUSED_FIELDS,
                "get_target_status paused result"
            )
            script = validate_result_script(
                result["script"],
                "get_target_status paused result script"
            )
            if script != active_script:
                raise PdbProtocolError(
                    f"get_target_status paused script {script!r} "
                    f"does not match active {active_script!r}"
                )
            line = validate_int_strict_field(
                result["line"],
                "get_target_status paused result line"
            )
            if line <= 0:
                raise PdbProtocolError(
                    "get_target_status paused result line "
                    "must be positive"
                )
            if active_breakpoints is not None and line not in active_breakpoints:
                raise PdbProtocolError(
                    f"get_target_status paused result line {line} "
                    f"is not among active breakpoints "
                    f"{list(active_breakpoints)}"
                )
            validate_bounded_protocol_string(
                result["function"],
                "get_target_status paused result function",
                _MAX_RESULT_FUNCTION_UTF8,
            )
        elif state == "exited":
            check_exact_fields(
                result, _STATUS_EXITED_FIELDS,
                "get_target_status exited result"
            )
            script = validate_result_script(
                result["script"],
                "get_target_status exited result script"
            )
            if script != active_script:
                raise PdbProtocolError(
                    f"get_target_status exited script {script!r} "
                    f"does not match active {active_script!r}"
                )
            validate_int_strict_field(
                result["exit_code"],
                "get_target_status exited result exit_code"
            )
        elif state == "failed":
            check_exact_fields(
                result, _STATUS_FAILED_FIELDS,
                "get_target_status failed result"
            )
            script = validate_result_script(
                result["script"],
                "get_target_status failed result script"
            )
            if script != active_script:
                raise PdbProtocolError(
                    f"get_target_status failed script {script!r} "
                    f"does not match active {active_script!r}"
                )
            validate_bounded_protocol_string(
                result["error"],
                "get_target_status failed result error",
                _MAX_RESULT_ERROR_UTF8,
            )
        elif state == "terminated":
            check_exact_fields(
                result, _STATUS_TERMINATED_FIELDS,
                "get_target_status terminated result"
            )
            script = validate_result_script(
                result["script"],
                "get_target_status terminated result script"
            )
            if script != active_script:
                raise PdbProtocolError(
                    f"get_target_status terminated script {script!r} "
                    f"does not match active {active_script!r}"
                )
    return state


def validate_terminate_result(
    response: PdbResponse,
    expected_script: str,
) -> None:
    result = response.result
    if not isinstance(result, dict):
        raise PdbProtocolError(
            "terminate_paused_target result must be a mapping"
        )
    check_exact_fields(
        result, _TERMINATED_RESULT_FIELDS,
        "terminate_paused_target result"
    )
    state = result.get("state")
    if state != "terminated":
        raise PdbProtocolError(
            f"terminate_paused_target result state must be "
            f"'terminated', got {state!r}"
        )
    script = validate_result_script(
        result["script"],
        "terminate_paused_target result script"
    )
    if script != expected_script:
        raise PdbProtocolError(
            f"terminate_paused_target result script {script!r} "
            f"does not match expected {expected_script!r}"
        )
