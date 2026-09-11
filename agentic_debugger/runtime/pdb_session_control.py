"""Paused-target control authority for the PDB session.

Owns the inspection-dispatch flow (``get_stack_summary``/``get_frame``/
``get_frame_locals``/``safe_eval_expression``), the resume flow
(``continue``/``step``/``next``) and the terminate flow.  The session
remains the sole owner of mutable state (session state, lifecycle state,
request IDs, locks, queues); these helpers operate on the session object
passed in and delegate state transitions, request exchange and cleanup to
the session's single authorities.

Dependency direction: depends on validation, inspection and outcome
validators, exceptions and protocol.  It must not import transport or the
session at runtime (``TYPE_CHECKING`` only): request exchange goes through
``session._send_and_receive`` so request-ID and transport authority stay
singular.  The session depends on this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from agentic_debugger.runtime.exceptions import (
    PdbProtocolError,
    PdbSessionError,
    PdbSessionStateError,
)
from agentic_debugger.runtime.pdb_protocol import (
    PROTOCOL_VERSION,
    PdbRequest,
    PdbResponse,
)
from agentic_debugger.runtime.pdb_session_inspection import (
    validate_frame_result,
    validate_locals_result,
    validate_safe_eval_result,
    validate_stack_summary_result,
    validate_successful_inspection_response_size,
)
from agentic_debugger.runtime.pdb_session_outcomes import (
    validate_persistent_outcome_result,
    validate_terminate_result,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime cycle
    from agentic_debugger.runtime.pdb_session import PdbSession


def perform_inspection(
    session: PdbSession,
    operation: str,
    payload: Dict[str, Any],
    requested_frame_id: Optional[int],
    requested_generation: Optional[int],
) -> Dict[str, object]:
    if not session._request_lock.acquire(timeout=session._request_timeout):
        raise PdbSessionError(
            "A request is already in flight; only one "
            "in-flight request is supported"
        )
    try:
        with session._state_lock:
            if session._state.value != "ready":
                raise PdbSessionStateError(
                    f"Cannot {operation} from state "
                    f"{session._state.value}; expected READY"
                )
            if session._target_lifecycle_state != "paused":
                raise PdbSessionStateError(
                    f"Cannot {operation} in local lifecycle state "
                    f"{session._target_lifecycle_state!r}; expected 'paused'"
                )
            active_script = session._active_script
            if not isinstance(active_script, str):
                raise PdbSessionStateError(
                    f"Cannot {operation} without active target metadata"
                )

        request = PdbRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=session._allocate_request_id(),
            operation=operation,
            payload=payload,
        )
        response = session._send_and_receive(
            request, session._request_timeout
        )
        if not isinstance(response, PdbResponse):
            session._fail_and_cleanup(PdbProtocolError(
                f"{operation} response must be a PdbResponse"
            ))
        if not response.success:
            if response.result != {}:
                session._fail_and_cleanup(PdbProtocolError(
                    f"Failed {operation} response must have empty result, "
                    f"got {response.result}"
                ))
            raise PdbSessionError(f"{operation} failed: {response.error}")

        try:
            workspace_root = session._workspace.root
            if operation == "get_stack_summary":
                validate_stack_summary_result(
                    response.result, active_script, workspace_root
                )
            elif operation == "get_frame":
                validate_frame_result(
                    response.result,
                    requested_frame_id,
                    requested_generation,
                    active_script,
                    workspace_root,
                )
            elif operation == "get_frame_locals":
                validate_locals_result(
                    response.result,
                    requested_frame_id,
                    requested_generation,
                )
            elif operation == "safe_eval_expression":
                validate_safe_eval_result(
                    response.result,
                    requested_frame_id,
                    requested_generation,
                    payload['expression'],
                    active_script,
                    workspace_root,
                )
            else:
                raise PdbProtocolError(
                    f"Unsupported inspection operation: {operation!r}"
                )
            validate_successful_inspection_response_size(
                response, operation
            )
        except PdbProtocolError as e:
            session._fail_and_cleanup(e)
        except Exception as e:
            session._fail_and_cleanup(PdbProtocolError(
                f"Malformed {operation} successful result: "
                f"{type(e).__name__}"
            ))
        return dict(response.result)
    finally:
        session._request_lock.release()


def resume_paused_target(
    session: PdbSession,
    *,
    operation: str,
    verb: str,
    transient_state: str,
    require_breakpoint_result: bool,
) -> Dict[str, object]:
    if not session._request_lock.acquire(timeout=session._request_timeout):
        raise PdbSessionError(
            "A request is already in flight; only one "
            "in-flight request is supported"
        )
    try:
        with session._state_lock:
            if session._state.value != "ready":
                raise PdbSessionStateError(
                    f"Cannot {operation} from state "
                    f"{session._state.value}; expected READY"
                )
            if session._target_lifecycle_state != "paused":
                raise PdbSessionStateError(
                    f"Cannot {operation} in local lifecycle "
                    f"state {session._target_lifecycle_state!r}; "
                    f"expected 'paused'"
                )
            active_script = session._active_script
            active_breakpoints = session._active_breakpoints
            if not isinstance(active_script, str):
                raise PdbSessionStateError(
                    f"Cannot {operation} without active "
                    "target metadata"
                )
            session._target_lifecycle_state = transient_state

        request = PdbRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=session._allocate_request_id(),
            operation=operation,
            payload={},
        )
        response = session._send_and_receive(
            request, session._request_timeout
        )

        if not response.success:
            if response.result != {}:
                session._fail_and_cleanup(
                    PdbProtocolError(
                        f"Failed {operation} response must "
                        f"have empty result, got {response.result}"
                    )
                )
            session._update_local_lifecycle("unknown", active_script)
            raise PdbSessionError(
                f"{verb.capitalize()} failed: {response.error}"
            )

        try:
            state = validate_persistent_outcome_result(
                response,
                active_script,
                active_breakpoints if require_breakpoint_result else None,
                operation,
            )
        except (PdbProtocolError, PdbSessionError) as e:
            session._fail_and_cleanup(e)

        session._update_local_lifecycle(state, active_script)
        return dict(response.result)
    except Exception:
        raise
    finally:
        with session._state_lock:
            if session._target_lifecycle_state == transient_state:
                session._target_lifecycle_state = "unknown"
        session._request_lock.release()


def terminate_paused_target(session: PdbSession) -> Dict[str, object]:
    if not session._request_lock.acquire(timeout=session._request_timeout):
        raise PdbSessionError(
            "A request is already in flight; only one "
            "in-flight request is supported"
        )
    try:
        with session._state_lock:
            if session._state.value != "ready":
                raise PdbSessionStateError(
                    f"Cannot terminate_paused_target from state "
                    f"{session._state.value}; expected READY"
                )
            if session._target_lifecycle_state != "paused":
                raise PdbSessionStateError(
                    f"Cannot terminate_paused_target in local lifecycle "
                    f"state {session._target_lifecycle_state!r}; "
                    f"expected 'paused'"
                )
            active_script = session._active_script

        request = PdbRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=session._allocate_request_id(),
            operation="terminate_paused_target",
            payload={},
        )

        response = session._send_and_receive(
            request, session._request_timeout
        )

        if not response.success:
            if response.result != {}:
                session._fail_and_cleanup(
                    PdbProtocolError(
                        "Failed terminate_paused_target response must "
                        f"have empty result, got {response.result}"
                    )
                )
            session._update_local_lifecycle("unknown", active_script)
            raise PdbSessionError(
                f"Terminate failed: {response.error}"
            )

        try:
            validate_terminate_result(response, active_script)
        except (PdbProtocolError, PdbSessionError) as e:
            session._fail_and_cleanup(e)

        session._update_local_lifecycle("terminated", active_script)

        return dict(response.result)
    except Exception:
        raise
    finally:
        session._request_lock.release()
