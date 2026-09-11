"""Paused-target control plane for the PDB worker.

Owns the persistent paused-target lifecycle transitions
(continue/step/next/resume, status query, terminate, termination join, and
invariant fail-closed cleanup) as a mixin consumed only by
:class:`pdb_worker.PdbWorker`. The mixin holds no state of its own: every
method operates on the single worker-owned lifecycle dict/condition pair
(``self._lifecycle``/``self._condition``) and the worker's send helpers.
There is exactly one controller and one lifecycle; this module is its
control-plane implementation, not a second one.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from agentic_debugger.runtime.pdb_protocol import (
    PROTOCOL_VERSION,
    PdbRequest,
    PdbResponse,
)
from agentic_debugger.runtime.pdb_worker_limits import (
    _WORKER_TERMINATION_TIMEOUT,
)


class PdbLifecycleMixin:
    """Control-plane transitions for the single worker lifecycle.

    The declarations below describe the worker-owned state this mixin
    operates on (provided by ``PdbWorker``); the mixin itself creates no
    state, thread, or lifecycle.
    """

    _condition: threading.Condition
    _lifecycle: Dict[str, Any]
    _target_thread: Optional[threading.Thread]
    _unsafe: bool
    _running: bool

    def _request_target_termination(self) -> Dict[str, Any]:
        with self._condition:
            if self._lifecycle['state'] == 'paused':
                self._lifecycle['state'] = 'terminating'
                self._condition.notify_all()
        target_thread = self._target_thread
        if target_thread is not None and target_thread is not threading.current_thread():
            target_thread.join(timeout=_WORKER_TERMINATION_TIMEOUT)
            if target_thread.is_alive():
                self._unsafe = True
                self._running = False
                self._target_thread = None
                return {'error': "Target termination did not complete within timeout", 'timeout': True}
            self._target_thread = None
        with self._condition:
            final_state = self._lifecycle['state']
        if final_state == 'terminated':
            return {'state': 'terminated'}
        return {'error': f"Target termination produced unexpected state: {final_state}"}

    def _fail_paused_target_invariant(self, error: str) -> str:
        """Clean up a false paused state without writing a response."""
        safe_error = ''.join(
            c if c.isprintable() or c in (' ', '\t') else '?'
            for c in error
        )
        safe_error = safe_error.encode(
            'utf-8', errors='replace'
        )[:4096].decode('utf-8', errors='replace')
        if not safe_error:
            safe_error = "Internal paused-target invariant failure"

        with self._condition:
            target_thread = self._target_thread
            if target_thread is not None and target_thread.is_alive():
                self._lifecycle['state'] = 'terminating'
                self._condition.notify_all()

        cleanup_safe = True
        if target_thread is threading.current_thread():
            cleanup_safe = False
        elif target_thread is not None and target_thread.is_alive():
            target_thread.join(timeout=_WORKER_TERMINATION_TIMEOUT)
            cleanup_safe = not target_thread.is_alive()

        if not cleanup_safe:
            safe_error += "; target invariant cleanup timed out"
            safe_error = safe_error.encode(
                'utf-8', errors='replace'
            )[:4096].decode('utf-8', errors='replace')
            self._unsafe = True
            self._running = False

        with self._condition:
            self._target_thread = None
            self._lifecycle['state'] = 'failed'
            self._lifecycle['error'] = safe_error
            self._lifecycle['_paused_frame'] = None
            self._lifecycle['_resume_mode'] = None
            self._lifecycle['_resume_frame'] = None
            self._condition.notify_all()
        return safe_error

    def _handle_continue_paused_target(self, request: PdbRequest) -> None:
        self._handle_resume_paused_target(request, "continue")

    def _handle_step_paused_target(self, request: PdbRequest) -> None:
        self._handle_resume_paused_target(request, "step")

    def _handle_next_paused_target(self, request: PdbRequest) -> None:
        self._handle_resume_paused_target(request, "next")

    def _handle_resume_paused_target(
        self,
        request: PdbRequest,
        mode: str,
    ) -> None:
        if mode not in {"continue", "step", "next"}:
            self._send_error(request.request_id, "Unsupported resume mode")
            return
        verb = mode
        past_tense = {"continue": "continued", "step": "stepped", "next": "nexted"}[mode]
        payload = request.payload
        if not isinstance(payload, dict):
            self._send_error(request.request_id, "payload must be a mapping")
            return
        for field in payload:
            self._send_error(
                request.request_id,
                f"Unknown payload field: {field}"
            )
            return

        failure: Optional[str] = None
        invariant_failure: Optional[str] = None
        result: Optional[Dict[str, Any]] = None
        terminal_state: Optional[str] = None
        with self._condition:
            state = self._lifecycle['state']
            target_thread = self._target_thread
            if state != 'paused':
                failure = f"Cannot {verb} target in state: {state}"
            elif target_thread is None:
                invariant_failure = (
                    "Paused target invariant failure: target thread is missing"
                )
            elif not target_thread.is_alive():
                invariant_failure = (
                    "Paused target invariant failure: target thread is not alive"
                )
            else:
                paused_frame = self._lifecycle.get('_paused_frame')
                if mode == 'next' and paused_frame is None:
                    invariant_failure = (
                        "Paused target invariant failure: paused frame is missing"
                    )
                    paused_frame = None
                if invariant_failure is not None:
                    pass
                else:
                    self._lifecycle['_resume_mode'] = (
                        mode if mode in {'step', 'next'} else None
                    )
                    self._lifecycle['_resume_frame'] = (
                        paused_frame if mode == 'next' else None
                    )
                pause_generation = self._lifecycle['pause_generation']
                if invariant_failure is None:
                    self._lifecycle['state'] = 'running'
                    self._condition.notify_all()

                while invariant_failure is None:
                    state = self._lifecycle['state']
                    current_generation = self._lifecycle['pause_generation']
                    if state == 'paused':
                        if current_generation <= pause_generation:
                            invariant_failure = (
                                "Paused target invariant failure: stale pause "
                                f"generation {current_generation} did not "
                                f"advance beyond {pause_generation}"
                            )
                        else:
                            result = {
                                'state': 'paused',
                                'script': self._lifecycle['script'],
                                'line': self._lifecycle['line'],
                                'function': self._lifecycle['function'],
                            }
                        break
                    if state in ('exited', 'failed'):
                        terminal_state = state
                        break
                    if state != 'running':
                        invariant_failure = (
                            "Paused target invariant failure: unexpected "
                            f"lifecycle state after {verb}: {state}"
                        )
                        break
                    self._condition.wait()

        if invariant_failure is not None:
            error = self._fail_paused_target_invariant(invariant_failure)
            self._send_error(request.request_id, error)
            return

        if failure is not None:
            self._send_error(request.request_id, failure)
            return

        if result is not None:
            self._send_response(PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            ))
            return

        target_thread = self._target_thread
        if target_thread is not None and target_thread is not threading.current_thread():
            target_thread.join(timeout=_WORKER_TERMINATION_TIMEOUT)
            if target_thread.is_alive():
                self._unsafe = True
                self._running = False
                self._target_thread = None
                self._send_error(
                    request.request_id,
                    f"Target thread did not complete after {past_tense} outcome"
                )
                return
            self._target_thread = None

        with self._condition:
            state = self._lifecycle['state']
            script = self._lifecycle['script']
            exit_code = self._lifecycle['exit_code']
            error = self._lifecycle['error']

        if state != terminal_state:
            self._send_error(
                request.request_id,
                f"{past_tense.capitalize()} target terminal state changed "
                f"unexpectedly: {state}"
            )
        elif state == 'exited':
            self._send_response(PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result={
                    'state': 'exited',
                    'script': script,
                    'exit_code': exit_code,
                },
                error="",
            ))
        else:
            self._send_error(request.request_id, error)

    def _handle_get_target_status(self, request: PdbRequest) -> None:
        payload = request.payload
        if not isinstance(payload, dict):
            self._send_error(request.request_id, "payload must be a mapping")
            return
        for field in payload:
            self._send_error(
                request.request_id,
                f"Unknown payload field: {field}"
            )
            return

        invariant_failure: Optional[str] = None
        with self._condition:
            state = self._lifecycle['state']
            if state == 'paused':
                target_thread = self._target_thread
                if target_thread is None:
                    invariant_failure = (
                        "Paused target invariant failure during status: "
                        "target thread is missing"
                    )
                elif not target_thread.is_alive():
                    invariant_failure = (
                        "Paused target invariant failure during status: "
                        "target thread is not alive"
                    )

        if invariant_failure is not None:
            self._fail_paused_target_invariant(invariant_failure)
            with self._condition:
                state = self._lifecycle['state']

        if state == 'idle':
            result = {'state': 'idle'}
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            )
        elif state == 'paused':
            result = {
                'state': 'paused',
                'script': self._lifecycle['script'],
                'line': self._lifecycle['line'],
                'function': self._lifecycle['function'],
            }
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            )
        elif state == 'exited':
            result = {
                'state': 'exited',
                'script': self._lifecycle['script'],
                'exit_code': self._lifecycle['exit_code'],
            }
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            )
        elif state == 'failed':
            result = {
                'state': 'failed',
                'script': self._lifecycle['script'],
                'error': self._lifecycle['error'],
            }
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            )
        elif state == 'terminated':
            result = {
                'state': 'terminated',
                'script': self._lifecycle['script'],
            }
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            )
        else:
            self._send_error(
                request.request_id,
                f"Unexpected lifecycle state: {state}"
            )
            return
        self._send_response(response)

    def _handle_terminate_paused_target(self, request: PdbRequest) -> None:
        payload = request.payload
        if not isinstance(payload, dict):
            self._send_error(request.request_id, "payload must be a mapping")
            return
        for field in payload:
            self._send_error(
                request.request_id,
                f"Unknown payload field: {field}"
            )
            return

        with self._condition:
            state = self._lifecycle['state']

        if state != 'paused':
            self._send_error(
                request.request_id,
                f"Cannot terminate target in state: {state}"
            )
            return

        term_result = self._request_target_termination()
        if term_result.get('error'):
            self._send_error(request.request_id, term_result['error'])
            return

        result = {
            'state': 'terminated',
            'script': self._lifecycle['script'],
        }
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            success=True,
            result=result,
            error="",
        )
        self._send_response(response)
