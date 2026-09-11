"""PDB worker process entrypoint and protocol-dispatch authority.

This module is the executable worker (``runpy.run_module(
"agentic_debugger.runtime.pdb_worker", run_name="__main__")``) and the
single owner of the worker lifecycle dict and request/response envelope
handling. Implementation work is delegated to focused runtime modules:

* :mod:`pdb_worker_limits` — shared safety/bounding constants,
* :mod:`pdb_worker_paths` — workspace path canonicalization,
* :mod:`pdb_worker_values` — bounded value summarization,
* :mod:`pdb_worker_frames` — bounded frame-local access,
* :mod:`pdb_worker_safeeval` — safe-expression parsing/evaluation,
* :mod:`pdb_worker_postmortem` — post-mortem evidence construction,
* :mod:`pdb_worker_runners` — PDB trace machinery and stdio isolation,
* :mod:`pdb_worker_execution` — validated loading and supervised runs,
* :mod:`pdb_worker_inspection` — paused-target stack/frame/locals/safe-eval.

The lifecycle dict (``idle``/``starting``/``paused``/``running``/
``terminating``/``exited``/``failed``/``terminated``) is created here,
mutated here on the control plane, and passed explicitly to the
supervised-run and pause-protocol helpers. No other module owns a
lifecycle; the persistent runner only pauses on the dict handed to it.
"""
from __future__ import annotations

import io
import os
import pdb
import sys
import threading
import traceback
from typing import Any, Dict, List, Optional, Tuple

from agentic_debugger.runtime.pdb_protocol import (
    PROTOCOL_VERSION,
    MAX_LINE_LENGTH,
    PdbRequest,
    PdbResponse,
    serialize_response,
    deserialize_request,
)
from agentic_debugger.runtime.exceptions import PdbProtocolError
from agentic_debugger.runtime import pdb_worker_execution as _execution
from agentic_debugger.runtime import pdb_worker_inspection as _inspection
from agentic_debugger.runtime.pdb_worker_lifecycle import PdbLifecycleMixin
from agentic_debugger.runtime.pdb_worker_limits import (
    _WORKER_TERMINATION_TIMEOUT,
)
from agentic_debugger.runtime.pdb_worker_postmortem import (
    _bounded_local_repr_pure,
    _capture_post_mortem_evidence_pure,
    _post_mortem_missing_traceback_response,
    _safe_exception_error_message,
)


class PdbWorker(PdbLifecycleMixin):
    def __init__(self) -> None:
        self._pdb_stdin = io.StringIO()
        self._pdb_stdout = io.StringIO()
        self._pdb = pdb.Pdb(
            readrc=False,
            stdin=self._pdb_stdin,
            stdout=self._pdb_stdout,
        )
        self._running = True
        self._target_started = False
        self._protocol_stdin = sys.stdin
        # Pytest's fd-level capture redirects descriptor 1 inside an
        # in-process target.  Keep the JSON protocol on a protected duplicate
        # so exact pytest reproductions cannot redirect or close the channel.
        self._protocol_stdout = os.fdopen(
            os.dup(sys.stdout.fileno()),
            "w",
            encoding="utf-8",
            newline="",
        )
        self._condition = threading.Condition()
        self._lifecycle: Dict[str, Any] = {
            'state': 'idle',
            'script': '',
            'line': 0,
            'function': '',
            'exit_code': None,
            'error': '',
            '_start_script': '',
            'pause_generation': 0,
            '_paused_frame': None,
            '_resume_mode': None,
            '_resume_frame': None,
        }
        self._target_thread: Optional[threading.Thread] = None
        self._unsafe = False
        self._workspace_root_real = os.path.realpath(os.path.abspath(os.getcwd()))

    def run(self) -> None:
        while self._running and not self._unsafe:
            try:
                data = self._protocol_stdin.buffer.readline(MAX_LINE_LENGTH + 1)
            except OSError:
                self._diag("stdin read error")
                break

            if not data:
                break

            if len(data) > MAX_LINE_LENGTH:
                self._send_error(
                    request_id=0,
                    error=(
                        f"Input line exceeds maximum length "
                        f"({len(data)} > {MAX_LINE_LENGTH} bytes)"
                    ),
                )
                self._running = False
                break

            try:
                request = deserialize_request(data)
            except PdbProtocolError as e:
                self._send_error(
                    request_id=0,
                    error=str(e),
                )
                continue

            try:
                self._handle(request)
            except Exception as e:
                self._diag(f"Unhandled error: {traceback.format_exc()}")
                self._send_error(
                    request_id=request.request_id,
                    error=f"Internal worker error: {e}",
                )

    def _handle(self, request: PdbRequest) -> None:
        if request.protocol_version != PROTOCOL_VERSION:
            self._send_error(
                request_id=request.request_id,
                error=(
                    f"Unsupported protocol version: "
                    f"{request.protocol_version}, "
                    f"expected {PROTOCOL_VERSION}"
                ),
            )
            return
        op = request.operation
        if op == "hello":
            self._handle_hello(request)
        elif op == "ping":
            self._handle_ping(request)
        elif op == "shutdown":
            self._handle_shutdown(request)
        elif op == "run_to_breakpoint":
            self._handle_run_to_breakpoint(request)
        elif op == "start_paused_target":
            self._handle_start_paused_target(request)
        elif op == "continue_paused_target":
            self._handle_continue_paused_target(request)
        elif op == "step_paused_target":
            self._handle_step_paused_target(request)
        elif op == "next_paused_target":
            self._handle_next_paused_target(request)
        elif op == "get_target_status":
            self._handle_get_target_status(request)
        elif op == "terminate_paused_target":
            self._handle_terminate_paused_target(request)
        elif op == "get_stack_summary":
            self._handle_get_stack_summary(request)
        elif op == "get_frame":
            self._handle_get_frame(request)
        elif op == "get_frame_locals":
            self._handle_get_frame_locals(request)
        elif op == "safe_eval_expression":
            self._handle_safe_eval_expression(request)
        elif op == "run_post_mortem":
            self._handle_run_post_mortem(request)
        else:
            self._send_error(
                request_id=request.request_id,
                error=f"Unsupported operation: {op!r}",
            )

    def _handle_hello(self, request: PdbRequest) -> None:
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            success=True,
            result={
                "pid": os.getpid(),
                "protocol_version": PROTOCOL_VERSION,
            },
            error="",
        )
        self._send_response(response)

    def _handle_ping(self, request: PdbRequest) -> None:
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            success=True,
            result={"status": "ok", "pdb_created": True},
            error="",
        )
        self._send_response(response)

    def _handle_shutdown(self, request: PdbRequest) -> None:
        with self._condition:
            is_paused = self._lifecycle['state'] == 'paused'
        if is_paused:
            term_result = self._request_target_termination()
            if term_result.get('error'):
                self._send_error(request.request_id, term_result['error'])
                self._running = False
                self._unsafe = True
                return
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            success=True,
            result={"shutdown": True},
            error="",
        )
        self._send_response(response)
        self._running = False

    def _handle_run_to_breakpoint(self, request: PdbRequest) -> None:
        payload = request.payload

        for field in ('script', 'breakpoints', 'argv'):
            if field not in payload:
                self._send_error(
                    request.request_id,
                    f"Missing required payload field: {field}"
                )
                return

        for field in payload:
            if field not in ('script', 'breakpoints', 'argv'):
                self._send_error(
                    request.request_id,
                    f"Unknown payload field: {field}"
                )
                return

        if self._target_started:
            self._send_error(
                request.request_id,
                "Target execution already completed on this worker"
            )
            return

        workspace_root = os.getcwd()

        script = payload['script']
        breakpoints_raw = payload['breakpoints']
        argv_raw = payload['argv']

        sv = self._read_validated_workspace_script(script, workspace_root, request.request_id)
        if sv is None:
            return
        script_normalized, script_abs, source_bytes = sv

        bps = self._validate_breakpoints(breakpoints_raw, source_bytes, request.request_id)
        if bps is None:
            return

        av = self._validate_argv(argv_raw, request.request_id)
        if av is None:
            return

        self._target_started = True
        self._execute_target(
            script_normalized, script_abs, bps, av, source_bytes,
            request.request_id,
        )

    def _execute_target(
        self,
        script_normalized: str,
        script_abs: str,
        breakpoints: List[int],
        argv: List[str],
        source_bytes: bytes,
        request_id: int,
    ) -> None:
        """One-shot breakpoint execution operation (handler + test entry)."""
        outcome = _execution.run_one_shot(
            script_normalized, script_abs, breakpoints, argv, source_bytes,
        )
        self._report_one_shot(request_id, script_normalized, outcome)

    def _report_one_shot(
        self,
        request_id: int,
        script_normalized: str,
        outcome: Dict[str, Any],
    ) -> None:
        kind = outcome.get('kind')
        if kind == 'breakpoint':
            result = {
                'status': 'breakpoint',
                'script': script_normalized,
                'line': outcome['line'],
                'function': outcome['function'],
            }
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request_id,
                success=True,
                result=result,
                error="",
            )
            self._send_response(response)
            with self._condition:
                self._lifecycle['state'] = 'terminated'
                self._lifecycle['script'] = script_normalized
                self._condition.notify_all()
        elif kind == 'exited':
            result = {
                'status': 'exited',
                'script': script_normalized,
                'exit_code': outcome['exit_code'],
            }
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request_id,
                success=True,
                result=result,
                error="",
            )
            self._send_response(response)
            with self._condition:
                self._lifecycle['state'] = 'exited'
                self._lifecycle['script'] = script_normalized
                self._lifecycle['exit_code'] = outcome['exit_code']
                self._condition.notify_all()
        elif kind == 'compile_error':
            error_msg = outcome['error']
            self._send_error(request_id, error_msg)
            with self._condition:
                self._lifecycle['state'] = 'failed'
                self._lifecycle['script'] = script_normalized
                self._lifecycle['error'] = error_msg
                self._condition.notify_all()
        else:
            error_msg = outcome.get('error', 'Target execution failed')
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request_id,
                success=False,
                result={},
                error=error_msg,
            )
            self._send_response(response)
            with self._condition:
                self._lifecycle['state'] = 'failed'
                self._lifecycle['script'] = script_normalized
                self._lifecycle['error'] = error_msg
                self._condition.notify_all()

    def _handle_run_post_mortem(self, request: PdbRequest) -> None:
        """Run a Python script to completion; if it terminates with an
        unhandled exception, capture the structured traceback as post-mortem
        runtime evidence.  No interactive PDB session is entered: the worker
        captures the failure's call stack, exception identity, and the
        innermost frame's locals snapshot deterministically, then reports it
        as a ``post_mortem`` result.  A successful exit produces no
        post-mortem evidence (the result reports ``status: "exited"`` with
        ``post_mortem: false``); a failure without a traceback (e.g. a bare
        ``SystemExit``) fails closed with ``status: "failed"`` and no
        fabricated traceback.  Exactly one execution is allowed per worker."""
        payload = request.payload

        for field in ('script', 'argv'):
            if field not in payload:
                self._send_error(
                    request.request_id,
                    f"Missing required payload field: {field}"
                )
                return

        for field in payload:
            if field not in ('script', 'argv'):
                self._send_error(
                    request.request_id,
                    f"Unknown payload field: {field}"
                )
                return

        if self._target_started:
            self._send_error(
                request.request_id,
                "Target execution already completed on this worker"
            )
            return

        workspace_root = os.getcwd()
        script = payload['script']
        argv_raw = payload['argv']

        sv = self._read_validated_workspace_script(script, workspace_root, request.request_id)
        if sv is None:
            return
        script_normalized, script_abs, source_bytes = sv

        av = self._validate_argv(argv_raw, request.request_id)
        if av is None:
            return

        self._target_started = True
        self._execute_post_mortem_target(
            script_normalized, script_abs, av, source_bytes,
            request.request_id,
        )

    def _execute_post_mortem_target(
        self,
        script_normalized: str,
        script_abs: str,
        argv: List[str],
        source_bytes: bytes,
        request_id: int,
    ) -> None:
        """Completion-run execution operation with post-mortem capture."""
        outcome = _execution.run_post_mortem(
            script_normalized, script_abs, argv, source_bytes,
        )
        self._report_post_mortem(request_id, script_normalized, outcome)

    def _report_post_mortem(
        self,
        request_id: int,
        script_normalized: str,
        outcome: Dict[str, Any],
    ) -> None:
        kind = outcome.get('kind')
        if kind == 'compile_error':
            error_msg = outcome['error']
            self._send_error(request_id, error_msg)
            with self._condition:
                self._lifecycle['state'] = 'failed'
                self._lifecycle['script'] = script_normalized
                self._lifecycle['error'] = error_msg
                self._condition.notify_all()
        elif kind == 'exited':
            result = {
                'status': 'exited',
                'script': script_normalized,
                'exit_code': outcome['exit_code'],
                'post_mortem': False,
            }
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request_id,
                success=True,
                result=result,
                error="",
            )
            self._send_response(response)
            with self._condition:
                self._lifecycle['state'] = 'exited'
                self._lifecycle['script'] = script_normalized
                self._lifecycle['exit_code'] = outcome['exit_code']
                self._condition.notify_all()
        elif kind == 'missing_traceback':
            response = _post_mortem_missing_traceback_response(request_id)
            self._send_response(response)
            with self._condition:
                self._lifecycle['state'] = 'failed'
                self._lifecycle['script'] = script_normalized
                self._lifecycle['error'] = response.error
                self._condition.notify_all()
        else:
            evidence = outcome['evidence']
            result: Dict[str, Any] = {
                'status': 'post_mortem',
                'script': evidence.get('script', script_normalized),
                'post_mortem': True,
                'exception': evidence['exception'],
                'traceback_frames': evidence['traceback_frames'],
                'innermost_frame': evidence['innermost_frame'],
                'frames_truncated': evidence.get('frames_truncated', False),
            }
            if evidence.get('traceback_error'):
                result['traceback_error'] = evidence['traceback_error']
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request_id,
                success=True,
                result=result,
                error="",
            )
            self._send_response(response)
            with self._condition:
                self._lifecycle['state'] = 'failed'
                self._lifecycle['script'] = script_normalized
                self._lifecycle['error'] = evidence['exception']['repr']
                self._condition.notify_all()

    def _capture_post_mortem_evidence(
        self,
        script_normalized: str,
        exc_type: type,
        exc_value: BaseException,
        tb: Any,
    ) -> Dict[str, Any]:
        """Build bounded, sanitized post-mortem evidence from a captured
        exception (delegates to the module-level pure helper with the
        side-effect-safe exception message function, so no target-defined
        ``__str__``/``__repr__``/metaclass presentation code is invoked)."""
        return _capture_post_mortem_evidence_pure(
            script_normalized, exc_type, exc_value, tb,
            _safe_exception_error_message,
        )

    def _bounded_local_repr(self, value: Any) -> str:
        """Return a bounded, sanitized repr of a local variable value."""
        return _bounded_local_repr_pure(value)

    def _read_validated_workspace_script(
        self,
        script: Any,
        workspace_root: str,
        request_id: int,
    ) -> Optional[Tuple[str, str, bytes]]:
        validated, error = _execution.validate_workspace_script(
            script, workspace_root,
        )
        if validated is None:
            self._send_error(request_id, error or "script validation failed")
            return None
        return validated

    def _read_bounded_fd(self, fd: int, request_id: int) -> Optional[bytes]:
        data, error = _execution.read_bounded_fd(fd)
        if data is None:
            self._send_error(request_id, error or "cannot read script")
            return None
        return data

    def _validate_breakpoints(
        self,
        breakpoints_raw: Any,
        source_bytes: bytes,
        request_id: int,
    ) -> Optional[List[int]]:
        bps, error = _execution.validate_breakpoints(
            breakpoints_raw, source_bytes,
        )
        if bps is None:
            self._send_error(request_id, error or "invalid breakpoints")
            return None
        return bps

    def _validate_argv(
        self,
        argv_raw: Any,
        request_id: int,
    ) -> Optional[List[str]]:
        av, error = _execution.validate_argv(argv_raw)
        if av is None:
            self._send_error(request_id, error or "invalid argv")
            return None
        return av

    def _safe_error_message(self, exc: BaseException) -> str:
        return _execution.safe_error_message(exc)


    def _execute_target_persistent(
        self,
        script_normalized: str,
        script_abs: str,
        breakpoints: List[int],
        argv: List[str],
        source_bytes: bytes,
    ) -> None:
        """Persistent paused-target thread body (worker-owned lifecycle)."""
        _execution.run_persistent_target(
            self._lifecycle, self._condition,
            script_normalized, script_abs, breakpoints, argv, source_bytes,
        )

    def _handle_start_paused_target(self, request: PdbRequest) -> None:
        payload = request.payload

        for field in ('script', 'breakpoints', 'argv'):
            if field not in payload:
                self._send_error(
                    request.request_id,
                    f"Missing required payload field: {field}"
                )
                return

        for field in payload:
            if field not in ('script', 'breakpoints', 'argv'):
                self._send_error(
                    request.request_id,
                    f"Unknown payload field: {field}"
                )
                return

        with self._condition:
            if self._target_started:
                self._send_error(
                    request.request_id,
                    "Target execution already completed on this worker"
                )
                return
            if self._lifecycle['state'] != 'idle':
                self._send_error(
                    request.request_id,
                    "Target execution already completed on this worker"
                )
                return

        workspace_root = os.getcwd()
        script = payload['script']
        breakpoints_raw = payload['breakpoints']
        argv_raw = payload['argv']

        sv = self._read_validated_workspace_script(script, workspace_root, request.request_id)
        if sv is None:
            return
        script_normalized, script_abs, source_bytes = sv

        bps = self._validate_breakpoints(breakpoints_raw, source_bytes, request.request_id)
        if bps is None:
            return

        av = self._validate_argv(argv_raw, request.request_id)
        if av is None:
            return

        self._target_started = True

        with self._condition:
            self._lifecycle['state'] = 'starting'
            self._lifecycle['_start_script'] = script_normalized
            self._lifecycle['script'] = script_normalized
            self._lifecycle['line'] = 0
            self._lifecycle['function'] = ''
            self._lifecycle['exit_code'] = None
            self._lifecycle['error'] = ''
            self._lifecycle['pause_generation'] = 0
            self._lifecycle['_paused_frame'] = None
            self._lifecycle['_resume_mode'] = None
            self._lifecycle['_resume_frame'] = None

        self._target_thread = threading.Thread(
            target=self._execute_target_persistent,
            args=(script_normalized, script_abs, bps, av, source_bytes),
            daemon=True,
        )
        self._target_thread.start()

        with self._condition:
            while self._lifecycle['state'] == 'starting':
                self._condition.wait()
            state = self._lifecycle['state']

        if state == 'paused':
            result: Dict[str, Any] = {
                'state': 'paused',
                'script': script_normalized,
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
            self._send_response(response)
        elif state in ('exited', 'failed'):
            target_thread = self._target_thread
            if target_thread is not None and target_thread is not threading.current_thread():
                target_thread.join(timeout=_WORKER_TERMINATION_TIMEOUT)
                if target_thread.is_alive():
                    self._unsafe = True
                    self._running = False
                    self._send_error(
                        request.request_id,
                        "Target thread did not complete after outcome"
                    )
                    return
                self._target_thread = None
            if state == 'exited':
                result = {
                    'state': 'exited',
                    'script': script_normalized,
                    'exit_code': self._lifecycle['exit_code'],
                }
                response = PdbResponse(
                    protocol_version=PROTOCOL_VERSION,
                    request_id=request.request_id,
                    success=True,
                    result=result,
                    error="",
                )
                self._send_response(response)
            else:
                error_msg = self._lifecycle['error']
                response = PdbResponse(
                    protocol_version=PROTOCOL_VERSION,
                    request_id=request.request_id,
                    success=False,
                    result={},
                    error=error_msg,
                )
                self._send_response(response)
        else:
            self._send_error(
                request.request_id,
                f"Unexpected target lifecycle state: {state}"
            )

    def _handle_get_stack_summary(self, request: PdbRequest) -> None:
        if request.payload:
            field = sorted(request.payload.keys())[0]
            self._send_error(
                request.request_id, f"Unknown payload field: {field}"
            )
            return
        response: Optional[PdbResponse] = None
        failure: Optional[str] = None
        invariant_failure: Optional[str] = None
        frames: Any = None
        with self._condition:
            (frames, total, generation, script,
             failure, invariant_failure) = _inspection.inspection_snapshot(
                self._lifecycle, self._target_thread,
                self._workspace_root_real,
            )
            if frames is not None:
                response, failure = _inspection.stack_summary_response(
                    request.request_id, frames, total, generation, script,
                )
        frames = None
        if invariant_failure is not None:
            error = self._fail_paused_target_invariant(invariant_failure)
            self._send_error(request.request_id, error)
        elif failure is not None:
            self._send_error(request.request_id, failure)
        elif response is not None:
            self._send_response(response)
        else:
            self._send_error(request.request_id, "Inspection failed closed")

    def _handle_get_frame(self, request: PdbRequest) -> None:
        frame_id, requested_generation, payload_error = (
            _inspection.validate_frame_inspection_payload(request.payload)
        )
        if payload_error is not None:
            self._send_error(request.request_id, payload_error)
            return
        response: Optional[PdbResponse] = None
        failure: Optional[str] = None
        invariant_failure: Optional[str] = None
        frames: Any = None
        with self._condition:
            (frames, _total, generation, _script,
             failure, invariant_failure) = _inspection.inspection_snapshot(
                self._lifecycle, self._target_thread,
                self._workspace_root_real,
            )
            if frames is not None:
                if requested_generation != generation:
                    failure = "Stale or unknown pause generation"
                elif frame_id is None or frame_id >= len(frames):
                    failure = "Unknown frame_id for current pause"
                else:
                    response, failure = _inspection.frame_response(
                        request.request_id, frames, generation, frame_id,
                    )
        frames = None
        if invariant_failure is not None:
            error = self._fail_paused_target_invariant(invariant_failure)
            self._send_error(request.request_id, error)
        elif failure is not None:
            self._send_error(request.request_id, failure)
        elif response is not None:
            self._send_response(response)
        else:
            self._send_error(request.request_id, "Inspection failed closed")

    def _handle_get_frame_locals(self, request: PdbRequest) -> None:
        frame_id, requested_generation, payload_error = (
            _inspection.validate_frame_inspection_payload(request.payload)
        )
        if payload_error is not None:
            self._send_error(request.request_id, payload_error)
            return
        response: Optional[PdbResponse] = None
        failure: Optional[str] = None
        invariant_failure: Optional[str] = None
        frames: Any = None
        with self._condition:
            (frames, _total, generation, _script,
             failure, invariant_failure) = _inspection.inspection_snapshot(
                self._lifecycle, self._target_thread,
                self._workspace_root_real,
            )
            if frames is not None:
                if requested_generation != generation:
                    failure = "Stale or unknown pause generation"
                elif frame_id is None or frame_id >= len(frames):
                    failure = "Unknown frame_id for current pause"
                else:
                    response, failure = _inspection.frame_locals_response(
                        request.request_id, frames, generation, frame_id,
                    )
        frames = None
        if invariant_failure is not None:
            error = self._fail_paused_target_invariant(invariant_failure)
            self._send_error(request.request_id, error)
        elif failure is not None:
            self._send_error(request.request_id, failure)
        elif response is not None:
            self._send_response(response)
        else:
            self._send_error(request.request_id, "Inspection failed closed")

    def _handle_safe_eval_expression(self, request: PdbRequest) -> None:
        (frame_id, requested_generation, expression,
         payload_error) = _inspection.validate_safe_eval_payload(request.payload)
        if payload_error is not None:
            self._send_error(request.request_id, payload_error)
            return

        failure: Optional[str] = None
        invariant_failure: Optional[str] = None
        response: Optional[PdbResponse] = None
        frames: Any = None
        with self._condition:
            (frames, _total, generation, _script,
             failure, invariant_failure) = _inspection.inspection_snapshot(
                self._lifecycle, self._target_thread,
                self._workspace_root_real,
            )
            if frames is not None:
                if requested_generation != generation:
                    failure = "Stale or unknown pause generation"
                elif frame_id is None or frame_id >= len(frames):
                    failure = "Unknown frame_id for current pause"
                else:
                    response, failure = _inspection.safe_eval_response(
                        request.request_id, frames, generation,
                        frame_id, expression,
                    )
        frames = None

        if invariant_failure is not None:
            error = self._fail_paused_target_invariant(invariant_failure)
            self._send_error(request.request_id, error)
        elif failure is not None:
            self._send_error(request.request_id, failure)
        elif response is not None:
            self._send_response(response)
        else:
            self._send_error(
                request.request_id, "Safe evaluation failed closed"
            )

    def _send_response(self, response: PdbResponse) -> None:
        data = serialize_response(response)
        try:
            self._protocol_stdout.buffer.write(data)
            self._protocol_stdout.buffer.flush()
        except OSError:
            self._running = False

    def _send_error(
        self, request_id: int, error: str
    ) -> None:
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request_id,
            success=False,
            result={},
            error=error,
        )
        self._send_response(response)

    @staticmethod
    def _diag(message: str) -> None:
        try:
            print(f"[pdb_worker] {message}", file=sys.stderr)
        except OSError:
            pass


def main() -> None:
    worker = PdbWorker()
    worker.run()


if __name__ == "__main__":
    main()
