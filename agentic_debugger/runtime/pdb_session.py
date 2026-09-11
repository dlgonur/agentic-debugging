"""Client-side PDB session authority (decomposed).

:class:`PdbSession` remains the single stateful owner of the client-side
session: subprocess identity, request-sequence allocation, request
serialization lock, persistent-target lifecycle, diagnostics accumulator,
reader/queue ownership and stop/failure transitions.  Cohesive behavior is
delegated to single-authority helpers; nothing here duplicates that state:

* :mod:`pdb_session_limits` -- bounds, timeouts, field sets (no drift).
* :mod:`pdb_session_diagnostics` -- bounded stderr truncation behavior.
* :mod:`pdb_session_validation` -- target/argv/breakpoint/identifier input
  validation, workspace script IO and shared protocol primitives.
* :mod:`pdb_session_inspection` -- stack/frame/locals/safe-eval schemas.
* :mod:`pdb_session_outcomes` -- run/persistent/status/terminate schemas.
* :mod:`pdb_session_transport` -- reader threads, request exchange,
  shutdown/cleanup, process-group handling.
* :mod:`pdb_session_control` -- paused-target inspection dispatch and
  resume/terminate orchestration (via this session's authorities).

Dependency direction (acyclic)::

    limits <- diagnostics -> session
    limits <- validation <- inspection/outcomes <- control -> session
    limits/exceptions/protocol <- transport -> session

The four contained-launch hooks (``_get_worker_argv``, ``_worker_env``,
``_worker_cwd``, ``_expected_worker_pid``) stay instance methods called
through ``self`` so :class:`ContainedPdbSession` overrides remain
effective.  Strict fail-closed validation is preserved: malformed worker
data raises instead of being inferred into success.
"""

from __future__ import annotations

import math
import os
import queue
import subprocess
import sys
import threading
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence

from agentic_debugger.runtime.exceptions import (
    PdbProtocolError,
    PdbSessionError,
    PdbSessionStateError,
    PdbWorkerExitedError,
)
from agentic_debugger.runtime.pdb_protocol import (
    PROTOCOL_VERSION,
    MAX_LINE_LENGTH,
    PdbRequest,
    PdbResponse,
    PdbWorkerInfo,
)
from agentic_debugger.runtime.pdb_session_control import (
    perform_inspection as _control_perform_inspection,
    resume_paused_target as _control_resume,
    terminate_paused_target as _control_terminate,
)
from agentic_debugger.runtime.pdb_session_diagnostics import (
    _BoundedDiagnostics,
)
from agentic_debugger.runtime.pdb_session_limits import (
    _BINARY_OPEN_FLAG,
    _DEFAULT_MAX_DIAGNOSTICS,
    _DEFAULT_MAX_LINE,
    _DEFAULT_REQUEST_TIMEOUT,
    _DEFAULT_SHUTDOWN_TIMEOUT,
    _DEFAULT_STARTUP_TIMEOUT,
    _MAX_TARGET_SOURCE_BYTES,
    _QUEUE_CAPACITY,
    _STOP_REQUEST_LOCK_TIMEOUT,
    _TRUNCATION_MARKER,  # noqa: F401  (compatibility re-export)
)
from agentic_debugger.runtime.pdb_session_outcomes import (
    validate_run_result as _validate_run_result_fn,
    validate_start_paused_result as _validate_start_paused_fn,
    validate_status_result as _validate_status_fn,
)
from agentic_debugger.runtime.pdb_session_transport import (
    finalize_after_stop as _transport_finalize,
    send_and_receive as _transport_send,
    shutdown_worker_if_ready as _transport_shutdown,
    start_reader_threads as _transport_start_readers,
    terminate_and_cleanup as _transport_terminate,
    validate_ping_response as _transport_validate_ping,
)
from agentic_debugger.runtime.pdb_session_validation import (
    validate_argv as _validate_argv_fn,
    validate_breakpoints as _validate_bps_fn,
    validate_inspection_identifiers as _validate_insp_ids_fn,
    validate_safe_eval_expression_input as _validate_safe_expr_fn,
    validate_safe_eval_identifiers as _validate_safe_ids_fn,
    validate_script_and_read as _validate_script_fn,
)
from agentic_debugger.runtime.python_launcher import (
    build_worker_env,
    resolve_worker_executable,
)
from agentic_debugger.runtime.workspace import TaskWorkspace

__all__ = [
    "PdbSessionState",
    "PdbSession",
    "_DEFAULT_STARTUP_TIMEOUT",
    "_DEFAULT_REQUEST_TIMEOUT",
    "_DEFAULT_SHUTDOWN_TIMEOUT",
    "_DEFAULT_MAX_DIAGNOSTICS",
    "_DEFAULT_MAX_LINE",
    "_TRUNCATION_MARKER",
    "_BINARY_OPEN_FLAG",
    "_MAX_TARGET_SOURCE_BYTES",
    "_BoundedDiagnostics",
]


class PdbSessionState(Enum):
    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class PdbSession:
    def __init__(
        self,
        workspace: TaskWorkspace,
        *,
        startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT,
        shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT,
        max_diagnostics: int = _DEFAULT_MAX_DIAGNOSTICS,
        max_line: int = _DEFAULT_MAX_LINE,
        proof_pytest_dependencies: bool = False,
        worker_environment: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._validate_timeout(startup_timeout, "startup_timeout")
        self._validate_timeout(request_timeout, "request_timeout")
        self._validate_timeout(shutdown_timeout, "shutdown_timeout")
        self._validate_bound(max_diagnostics, "max_diagnostics")
        self._validate_bound(max_line, "max_line")

        if max_line > MAX_LINE_LENGTH:
            raise PdbSessionError(
                f"max_line ({max_line}) exceeds protocol MAX_LINE_LENGTH "
                f"({MAX_LINE_LENGTH})"
            )

        if worker_environment is not None:
            if not isinstance(worker_environment, Mapping):
                raise PdbSessionError(
                    "worker_environment must be a mapping of strings or None"
                )
            for name, value in worker_environment.items():
                if type(name) is not str or not name or type(value) is not str:
                    raise PdbSessionError(
                        "worker_environment must map non-empty strings to strings"
                    )

        self._workspace = workspace
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._max_diagnostics = max_diagnostics
        self._max_line = max_line
        if type(proof_pytest_dependencies) is not bool:
            raise PdbSessionError("proof_pytest_dependencies must be a boolean")
        self._proof_pytest_dependencies = proof_pytest_dependencies
        self._worker_environment = (
            dict(worker_environment) if worker_environment is not None else None
        )

        self._state = PdbSessionState.NEW
        self._proc: Optional[subprocess.Popen] = None
        self._next_request_id = 1
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()

        self._diag_accum = _BoundedDiagnostics(max_chars=max_diagnostics)
        self._diag_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._reader_error = threading.Event()

        self._reader_cleanup_lock = threading.Lock()
        self._reader_cleanup_started = threading.Event()
        self._reader_cleanup_done = threading.Event()
        self._reader_cleanup_reason: Optional[Exception] = None
        self._reader_cleanup_error: Optional[Exception] = None
        self._reader_cleanup_thread: Optional[threading.Thread] = None

        self._target_consumed = False
        self._target_lifecycle_state: str = "idle"
        self._active_script: Optional[str] = None
        self._active_breakpoints: Optional[Sequence[int]] = None

        self._response_queue: queue.Queue[Optional[bytes]] = queue.Queue(
            maxsize=_QUEUE_CAPACITY
        )
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

    @property
    def state(self) -> PdbSessionState:
        with self._state_lock:
            return self._state

    @property
    def is_alive(self) -> bool:
        with self._state_lock:
            if self._state == PdbSessionState.READY:
                if self._proc is not None and self._proc.poll() is None:
                    return True
                return False
            return False

    def _validate_timeout(self, value: float, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PdbSessionError(
                f"{name} must be a number, got {type(value).__name__}"
            )
        if not math.isfinite(value) or value <= 0:
            raise PdbSessionError(
                f"{name} must be a positive finite number, got {value!r}"
            )

    def _validate_bound(self, value: int, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise PdbSessionError(
                f"{name} must be an integer, got {type(value).__name__}"
            )
        if value <= 0:
            raise PdbSessionError(
                f"{name} must be positive, got {value!r}"
            )

    @staticmethod
    def _compute_project_root() -> str:
        import agentic_debugger
        pkg_dir = os.path.dirname(os.path.abspath(agentic_debugger.__file__))
        return os.path.dirname(pkg_dir)

    def _get_worker_argv(self) -> List[str]:
        project_root = self._compute_project_root().replace("\\", "/")
        if self._proof_pytest_dependencies:
            bootstrap = (
                "import sys; import site; import runpy; "
                "sys.path.append(site.getusersitepackages()); "
                "sys.path.insert(0, __import__('os').getcwd()); "
                "sys.path.insert(0, " + repr(project_root) + "); "
                "runpy.run_module("
                "'agentic_debugger.runtime.pdb_worker', run_name='__main__')"
            )
        else:
            bootstrap = (
                "import sys; import runpy; "
                "sys.path.insert(0, " + repr(project_root) + "); "
                "runpy.run_module("
                "'agentic_debugger.runtime.pdb_worker', run_name='__main__')"
            )
        return [
            resolve_worker_executable(),
            "-I",
            "-u",
            "-c",
            bootstrap,
        ]

    def _worker_env(self) -> Optional[Dict[str, str]]:
        """Environment for the worker subprocess (``None`` = inherit).

        V2-01: the ordinary product PDB worker receives the explicit
        project/PDB role environment derived by the session's
        execution-environment authority (supplied via
        ``worker_environment``); the mapping still passes through the
        established :func:`build_worker_env` authority, which is the only
        place Windows venv identity is decided.  ``None`` (no explicit
        role environment — harness/scientific callers) preserves the
        historical inherit-from-parent behavior unchanged.

        Inside a Windows virtual environment either path carries the
        standard ``__PYVENV_LAUNCHER__`` identity (CPython bpo-35797) so
        the directly launched base interpreter computes the same
        ``sys.executable``/``sys.prefix``/``sys.path`` as the redirector
        would have.  A subclass that launches through a non-Python
        bridge (e.g. WSL) overrides this to ``None``: the launcher
        identity must never leak into a foreign PID namespace.
        """
        return build_worker_env(self._worker_environment)

    def _worker_cwd(self) -> str:
        """Windows-side ``Popen`` cwd for the worker process.

        Defaults to the workspace root (unchanged behavior for the host-local
        launch path). A subclass whose ``_get_worker_argv`` launches the
        worker through an external bridge (e.g. WSL) may override this: the
        worker's real working directory is then controlled entirely by that
        bridge, and the Windows-side cwd only needs to be a directory
        ``subprocess.Popen`` can actually start from (a UNC workspace root is
        not always accepted there).
        """

        return self._workspace.root

    def start(self) -> None:
        with self._state_lock:
            if self._state != PdbSessionState.NEW:
                raise PdbSessionStateError(
                    f"Cannot start from state {self._state.value}; "
                    f"expected NEW"
                )
            self._state = PdbSessionState.STARTING

        argv = self._get_worker_argv()
        self._stop_event.clear()
        self._reader_error.clear()
        self._reader_cleanup_started.clear()
        self._reader_cleanup_done.clear()
        self._reader_cleanup_reason = None
        self._reader_cleanup_error = None
        self._reader_cleanup_thread = None

        try:
            proc = subprocess.Popen(
                argv,
                cwd=self._worker_cwd(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=self._worker_env(),
                start_new_session=sys.platform != "win32",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32"
                    else 0
                ),
            )
        except Exception as e:
            self._transition_to_failed()
            raise PdbSessionError(
                f"Failed to launch worker: {e}"
            ) from e

        with self._state_lock:
            self._proc = proc

        _transport_start_readers(self)

        try:
            self._handshake()
        except Exception:
            _transport_terminate(self)
            self._transition_to_failed()
            raise

        with self._state_lock:
            if self._reader_error.is_set():
                self._state = PdbSessionState.FAILED
                raise PdbProtocolError(
                    "Response channel integrity lost during startup"
                )
            self._state = PdbSessionState.READY

    def _expected_worker_pid(self) -> Optional[int]:
        """PID the handshake must match against ``self._proc.pid``, or ``None``
        to skip that check.

        Defaults to the spawned process's own PID (unchanged host-local
        behavior): the worker IS that direct child process, so an exact match
        is a meaningful confused-deputy defense. A subclass that launches the
        worker through an external bridge into a different PID namespace
        (e.g. WSL2, whose Linux PIDs cannot equal a Windows process ID by
        construction) overrides this to ``None``; the handshake still checks
        protocol version and process liveness.
        """

        return self._proc.pid

    def _handshake(self) -> None:
        request = PdbRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=self._allocate_request_id(),
            operation="hello",
            payload={},
        )
        response = self._send_and_receive(request, self._startup_timeout)

        if response.protocol_version != PROTOCOL_VERSION:
            raise PdbProtocolError(
                f"Protocol version mismatch: worker sent "
                f"{response.protocol_version}, expected {PROTOCOL_VERSION}"
            )
        if response.request_id != request.request_id:
            raise PdbProtocolError(
                f"Request ID mismatch in hello handshake: "
                f"sent {request.request_id}, got {response.request_id}"
            )
        if not response.success:
            raise PdbSessionError(
                f"Handshake failed: {response.error}"
            )

        worker_info = PdbWorkerInfo.from_mapping(response.result)

        expected_pid = self._expected_worker_pid()
        if expected_pid is not None and worker_info.pid != expected_pid:
            raise PdbSessionError(
                f"Worker PID mismatch: handshake reported "
                f"{worker_info.pid}, actual {expected_pid}"
            )
        if worker_info.protocol_version != PROTOCOL_VERSION:
            raise PdbProtocolError(
                f"Worker protocol version mismatch: "
                f"{worker_info.protocol_version} != {PROTOCOL_VERSION}"
            )

        if self._proc.poll() is not None:
            raise PdbWorkerExitedError(
                f"Worker exited after handshake (code {self._proc.poll()})"
            )

    def _allocate_request_id(self) -> int:
        rid = self._next_request_id
        self._next_request_id += 1
        return rid

    def ping(self) -> PdbResponse:
        with self._state_lock:
            if self._state != PdbSessionState.READY:
                raise PdbSessionStateError(
                    f"Cannot ping from state {self._state.value}; "
                    f"expected READY"
                )
        if not self._request_lock.acquire(timeout=self._request_timeout):
            raise PdbSessionError(
                "A request is already in flight; only one "
                "in-flight request is supported"
            )
        try:
            request = PdbRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id=self._allocate_request_id(),
                operation="ping",
                payload={},
            )
            response = self._send_and_receive(
                request, self._request_timeout
            )
            try:
                _transport_validate_ping(response)
            except (PdbProtocolError, PdbSessionError) as e:
                self._fail_and_cleanup(e)
            return response
        except Exception:
            raise
        finally:
            self._request_lock.release()

    def run_to_breakpoint(
        self,
        script: str,
        breakpoints: Sequence[int],
        argv: Sequence[str] = (),
    ) -> PdbResponse:
        if not self._request_lock.acquire(timeout=self._request_timeout):
            raise PdbSessionError(
                "A request is already in flight; only one "
                "in-flight request is supported"
            )
        try:
            with self._state_lock:
                if self._state != PdbSessionState.READY:
                    raise PdbSessionStateError(
                        f"Cannot run_to_breakpoint from state "
                        f"{self._state.value}; expected READY"
                    )
                if self._target_consumed:
                    raise PdbSessionStateError(
                        "Target execution already completed on this session; "
                        "exactly one execution is allowed"
                    )

            script_val, source_bytes = _validate_script_fn(
                self._workspace.root, script
            )
            breakpoints_val = _validate_bps_fn(breakpoints, source_bytes)
            argv_val = _validate_argv_fn(argv)

            with self._state_lock:
                self._target_consumed = True

            payload: Dict[str, Any] = {
                "script": script_val,
                "breakpoints": breakpoints_val,
                "argv": argv_val,
            }

            request = PdbRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id=self._allocate_request_id(),
                operation="run_to_breakpoint",
                payload=payload,
            )

            response = self._send_and_receive(
                request, self._request_timeout
            )

            if response.success:
                try:
                    _validate_run_result_fn(
                        response, script_val, breakpoints_val
                    )
                except (PdbProtocolError, PdbSessionError) as e:
                    self._fail_and_cleanup(e)
                status = response.result.get("status")
                with self._state_lock:
                    if status == "breakpoint":
                        self._target_lifecycle_state = "terminated"
                    elif status == "exited":
                        self._target_lifecycle_state = "exited"
                    self._active_script = script_val
                    self._active_breakpoints = None
            else:
                if response.result != {}:
                    self._fail_and_cleanup(
                        PdbProtocolError(
                            "Failed response must have empty result, "
                            f"got {response.result}"
                        )
                    )
                with self._state_lock:
                    self._target_lifecycle_state = "failed"
                    self._active_script = script_val
                    self._active_breakpoints = None

            return response
        except Exception:
            raise
        finally:
            self._request_lock.release()

    def run_post_mortem(
        self,
        script: str,
        argv: Sequence[str] = (),
    ) -> PdbResponse:
        """Run a Python script to completion and capture post-mortem evidence
        if it terminates with an unhandled exception.

        This is the offline-capable post-mortem entry point (TODO 6.1.3): it
        reuses the existing PDB protocol/worker channel, requires the same
        READY state and one-execution-per-session invariant as
        :meth:`run_to_breakpoint`, and never enters an interactive paused
        session.  On a successful exit the response carries
        ``status: "exited"`` with ``post_mortem: false``; on an unhandled
        exception it carries ``status: "post_mortem"`` with the bounded,
        structured traceback evidence (exception type/message, traceback
        frames, innermost-frame locals snapshot).  A failure without a
        traceback fails closed with ``success: false`` and no fabricated
        frame evidence."""
        if not self._request_lock.acquire(timeout=self._request_timeout):
            raise PdbSessionError(
                "A request is already in flight; only one "
                "in-flight request is supported"
            )
        try:
            with self._state_lock:
                if self._state != PdbSessionState.READY:
                    raise PdbSessionStateError(
                        f"Cannot run_post_mortem from state "
                        f"{self._state.value}; expected READY"
                    )
                if self._target_consumed:
                    raise PdbSessionStateError(
                        "Target execution already completed on this session; "
                        "exactly one execution is allowed"
                    )

            script_val, source_bytes = _validate_script_fn(
                self._workspace.root, script
            )
            argv_val = _validate_argv_fn(argv)

            with self._state_lock:
                self._target_consumed = True

            payload: Dict[str, Any] = {
                "script": script_val,
                "argv": argv_val,
            }

            request = PdbRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id=self._allocate_request_id(),
                operation="run_post_mortem",
                payload=payload,
            )

            response = self._send_and_receive(
                request, self._request_timeout
            )

            if response.success:
                status = response.result.get("status")
                with self._state_lock:
                    if status == "post_mortem":
                        self._target_lifecycle_state = "failed"
                    elif status == "exited":
                        self._target_lifecycle_state = "exited"
                    self._active_script = script_val
                    self._active_breakpoints = None
            else:
                if response.result != {}:
                    self._fail_and_cleanup(
                        PdbProtocolError(
                            "Failed response must have empty result, "
                            f"got {response.result}"
                        )
                    )
                with self._state_lock:
                    self._target_lifecycle_state = "failed"
                    self._active_script = script_val
                    self._active_breakpoints = None

            return response
        except Exception:
            raise
        finally:
            self._request_lock.release()

    def start_paused_target(
        self,
        script: str,
        breakpoints: Sequence[int],
        argv: Sequence[str] = (),
    ) -> Dict[str, object]:
        if not self._request_lock.acquire(timeout=self._request_timeout):
            raise PdbSessionError(
                "A request is already in flight; only one "
                "in-flight request is supported"
            )
        try:
            with self._state_lock:
                if self._state != PdbSessionState.READY:
                    raise PdbSessionStateError(
                        f"Cannot start_paused_target from state "
                        f"{self._state.value}; expected READY"
                    )
                if self._target_consumed:
                    raise PdbSessionStateError(
                        "Target execution already completed on this session; "
                        "exactly one execution is allowed"
                    )

            script_val, source_bytes = _validate_script_fn(
                self._workspace.root, script
            )
            breakpoints_val = _validate_bps_fn(breakpoints, source_bytes)
            argv_val = _validate_argv_fn(argv)

            with self._state_lock:
                self._target_consumed = True
                self._target_lifecycle_state = "starting"
                self._active_script = script_val
                self._active_breakpoints = list(breakpoints_val)

            payload: Dict[str, Any] = {
                "script": script_val,
                "breakpoints": breakpoints_val,
                "argv": argv_val,
            }

            request = PdbRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id=self._allocate_request_id(),
                operation="start_paused_target",
                payload=payload,
            )

            response = self._send_and_receive(
                request, self._request_timeout
            )

            if response.success:
                try:
                    state = _validate_start_paused_fn(
                        response, script_val, breakpoints_val
                    )
                except (PdbProtocolError, PdbSessionError) as e:
                    self._fail_and_cleanup(e)

                if state == "paused":
                    self._update_local_lifecycle("paused", script_val)
                    return {
                        "state": "paused",
                        "script": response.result["script"],
                        "line": response.result["line"],
                        "function": response.result["function"],
                    }
                else:
                    self._update_local_lifecycle("exited", script_val)
                    return {
                        "state": "exited",
                        "script": response.result["script"],
                        "exit_code": response.result["exit_code"],
                    }
            else:
                if response.result != {}:
                    self._fail_and_cleanup(
                        PdbProtocolError(
                            "Failed start_paused_target response must have "
                            f"empty result, got {response.result}"
                        )
                    )
                self._update_local_lifecycle("failed", script_val)
                raise PdbSessionError(
                    f"Target failed to start: {response.error}"
                )
        except Exception:
            raise
        finally:
            self._request_lock.release()

    def get_target_status(self) -> Dict[str, object]:
        if not self._request_lock.acquire(timeout=self._request_timeout):
            raise PdbSessionError(
                "A request is already in flight; only one "
                "in-flight request is supported"
            )
        try:
            with self._state_lock:
                if self._state != PdbSessionState.READY:
                    raise PdbSessionStateError(
                        f"Cannot get_target_status from state "
                        f"{self._state.value}; expected READY"
                    )

            request = PdbRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id=self._allocate_request_id(),
                operation="get_target_status",
                payload={},
            )

            response = self._send_and_receive(
                request, self._request_timeout
            )

            if not response.success:
                if response.result != {}:
                    self._fail_and_cleanup(
                        PdbProtocolError(
                            "Failed get_target_status response must have "
                            f"empty result, got {response.result}"
                        )
                    )
                raise PdbSessionError(
                    f"get_target_status failed: {response.error}"
                )

            with self._state_lock:
                _budget = self._target_consumed
                _script = self._active_script
                _bps = self._active_breakpoints
            try:
                state = _validate_status_fn(
                    response,
                    budget_consumed=_budget,
                    active_script=_script,
                    active_breakpoints=_bps,
                )
            except (PdbProtocolError, PdbSessionError) as e:
                self._fail_and_cleanup(e)

            self._update_local_lifecycle(state)

            return dict(response.result)
        except Exception:
            raise
        finally:
            self._request_lock.release()

    def get_stack_summary(self) -> Dict[str, object]:
        return _control_perform_inspection(
            self, "get_stack_summary", {}, None, None
        )

    def get_frame(
        self,
        frame_id: int,
        pause_generation: int,
    ) -> Dict[str, object]:
        frame_id_val, generation_val = _validate_insp_ids_fn(
            frame_id, pause_generation
        )
        return _control_perform_inspection(
            self,
            "get_frame",
            {
                "frame_id": frame_id_val,
                "pause_generation": generation_val,
            },
            frame_id_val,
            generation_val,
        )

    def get_frame_locals(
        self,
        frame_id: int,
        pause_generation: int,
    ) -> Dict[str, object]:
        frame_id_val, generation_val = _validate_insp_ids_fn(
            frame_id, pause_generation
        )
        return _control_perform_inspection(
            self,
            "get_frame_locals",
            {
                "frame_id": frame_id_val,
                "pause_generation": generation_val,
            },
            frame_id_val,
            generation_val,
        )

    def safe_eval_expression(
        self,
        frame_id: int,
        pause_generation: int,
        expression: str,
    ) -> Dict[str, object]:
        frame_id_val, generation_val = _validate_safe_ids_fn(
            frame_id, pause_generation
        )
        expression_val = _validate_safe_expr_fn(expression)
        return _control_perform_inspection(
            self,
            "safe_eval_expression",
            {
                "frame_id": frame_id_val,
                "pause_generation": generation_val,
                "expression": expression_val,
            },
            frame_id_val,
            generation_val,
        )

    def continue_paused_target(self) -> Dict[str, object]:
        return _control_resume(
            self,
            operation="continue_paused_target",
            verb="continue",
            transient_state="continuing",
            require_breakpoint_result=True,
        )

    def step_paused_target(self) -> Dict[str, object]:
        """Advance to the next traced line in the active target script."""

        return _control_resume(
            self,
            operation="step_paused_target",
            verb="step",
            transient_state="stepping",
            require_breakpoint_result=False,
        )

    def next_paused_target(self) -> Dict[str, object]:
        """Advance to the next traced line in the currently paused frame."""

        return _control_resume(
            self,
            operation="next_paused_target",
            verb="next",
            transient_state="nexting",
            require_breakpoint_result=False,
        )

    def terminate_paused_target(self) -> Dict[str, object]:
        return _control_terminate(self)

    def _update_local_lifecycle(
        self,
        state: str,
        script: Optional[str] = None,
    ) -> None:
        with self._state_lock:
            self._target_lifecycle_state = state
            if script is not None:
                self._active_script = script
            if state == "idle":
                self._active_script = None
                self._active_breakpoints = None
            elif state == "paused":
                pass
            elif state in ("exited", "failed", "terminated"):
                self._active_breakpoints = None

    def _send_and_receive(
        self, request: PdbRequest, timeout: float
    ) -> PdbResponse:
        return _transport_send(self, request, timeout)

    def stop(self) -> None:
        with self._state_lock:
            if self._state == PdbSessionState.STOPPED:
                return
            self._state = PdbSessionState.STOPPING

        if self._request_lock.acquire(
            timeout=_STOP_REQUEST_LOCK_TIMEOUT
        ):
            try:
                _transport_shutdown(self)
            finally:
                self._request_lock.release()
        else:
            _transport_terminate(self)

        _transport_finalize(self)

        with self._state_lock:
            self._state = PdbSessionState.STOPPED

    def _fail_and_cleanup(self, reason: Exception) -> None:
        self._transition_to_failed()
        _transport_terminate(self)
        raise reason

    def _transition_to_failed(self) -> None:
        with self._state_lock:
            if self._state in (
                PdbSessionState.STOPPING,
                PdbSessionState.STOPPED,
                PdbSessionState.FAILED,
            ):
                return
            self._state = PdbSessionState.FAILED

    @property
    def diagnostics(self) -> str:
        with self._diag_lock:
            return self._diag_accum.getvalue()

    def __enter__(self) -> PdbSession:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_val: object,
        exc_tb: object,
    ) -> None:
        self.stop()
