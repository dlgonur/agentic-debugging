from __future__ import annotations

import json
import math
import os
import queue
import signal
import stat
import subprocess
import sys
import threading
import time
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from agentic_debugger.runtime.exceptions import (
    PdbProtocolError,
    PdbSessionError,
    PdbSessionStateError,
    PdbSessionTimeoutError,
    PdbWorkerExitedError,
)
from agentic_debugger.runtime.pdb_protocol import (
    PROTOCOL_VERSION,
    MAX_LINE_LENGTH,
    PdbRequest,
    PdbResponse,
    PdbWorkerInfo,
    serialize_request,
    deserialize_response,
    serialize_response,
    deserialize_request,
)
from agentic_debugger.runtime.python_launcher import (
    build_worker_env,
    resolve_worker_executable,
)
from agentic_debugger.runtime.workspace import TaskWorkspace

_DEFAULT_STARTUP_TIMEOUT = 5.0
_DEFAULT_REQUEST_TIMEOUT = 5.0
_DEFAULT_SHUTDOWN_TIMEOUT = 2.0
_DEFAULT_MAX_DIAGNOSTICS = 20000
_DEFAULT_MAX_LINE = 65536

_THREAD_JOIN_TIMEOUT = 3.0
_CLEANUP_WAIT_TIMEOUT = 5.0
_PROCESS_TERMINATE_WAIT = 1.0
_PROCESS_KILL_WAIT = 1.0
_STOP_REQUEST_LOCK_TIMEOUT = 0.5

_QUEUE_CAPACITY = 2

_MAX_SCRIPT_PATH_UTF8 = 4096
_MAX_ARGV_ENTRY_UTF8 = 1024
_BINARY_OPEN_FLAG = getattr(os, "O_BINARY", 0)
_MAX_TARGET_SOURCE_BYTES = 16 * 1024 * 1024

_TRUNCATION_MARKER = "\n... [diagnostics truncated] ...\n"

_PING_REQUIRED_FIELDS = frozenset({"status", "pdb_created"})
_PING_KNOWN_FIELDS = frozenset({"status", "pdb_created"})
_SHUTDOWN_REQUIRED_FIELDS = frozenset({"shutdown"})
_SHUTDOWN_KNOWN_FIELDS = frozenset({"shutdown"})

_MAX_RESULT_FUNCTION_UTF8 = 4096
_MAX_RESULT_ERROR_UTF8 = 4096
_MAX_INSPECTION_NAME_UTF8 = 512
_MAX_TYPE_NAME_UTF8 = 512
_MAX_STACK_FRAMES = 64
_MAX_LOCAL_NAMES = 128
_MAX_CONTAINER_ITEMS = 16
_MAX_CONTAINER_DEPTH = 2
_MAX_STRING_PREVIEW_UTF8 = 2048
_MAX_BYTES_PREVIEW = 1024
_MAX_SERIALIZED_INT_BITS = 4096
_MAX_LOCALS_RESULT_BYTES = 32768
_MAX_SAFE_EVAL_RESULT_BYTES = 32768
_MAX_EXPRESSION_UTF8 = 1024

_PAUSED_RESULT_FIELDS = frozenset({"state", "script", "line", "function"})
_EXITED_RESULT_FIELDS = frozenset({"state", "script", "exit_code"})
_TERMINATED_RESULT_FIELDS = frozenset({"state", "script"})
_STATUS_IDLE_FIELDS = frozenset({"state"})
_STATUS_PAUSED_FIELDS = frozenset({"state", "script", "line", "function"})
_STATUS_EXITED_FIELDS = frozenset({"state", "script", "exit_code"})
_STATUS_FAILED_FIELDS = frozenset({"state", "script", "error"})
_STATUS_TERMINATED_FIELDS = frozenset({"state", "script"})
_STACK_RESULT_FIELDS = frozenset({
    "state", "script", "pause_generation", "frames", "total_frames",
    "truncated",
})
_FRAME_SUMMARY_FIELDS = frozenset({
    "frame_id", "script", "line", "function", "is_current",
})
_FRAME_RESULT_FIELDS = frozenset({"state", "pause_generation", "frame"})
_FRAME_DETAIL_FIELDS = frozenset({
    "frame_id", "script", "line", "function", "is_current",
    "argument_names", "local_names", "locals_count", "locals_truncated",
})
_LOCALS_RESULT_FIELDS = frozenset({
    "state", "pause_generation", "frame_id", "locals", "total_count",
    "truncated",
})
_SAFE_EVAL_RESULT_FIELDS = frozenset({
    "state", "pause_generation", "frame", "expression", "value",
})
_LOCAL_ENTRY_FIELDS = frozenset({"name", "value"})
_VALUE_SUMMARY_FIELDS = frozenset({
    "kind", "type", "value", "special", "size", "items", "entries",
    "truncated",
})
_DICT_ENTRY_FIELDS = frozenset({"key", "value"})
_VALUE_KINDS = frozenset({
    "none", "bool", "int", "float", "str", "bytes", "list", "tuple",
    "dict", "set", "frozenset", "object",
})
_CANONICAL_VALUE_TYPES = MappingProxyType({
    "none": "builtins.NoneType",
    "bool": "builtins.bool",
    "int": "builtins.int",
    "float": "builtins.float",
    "str": "builtins.str",
    "bytes": "builtins.bytes",
    "list": "builtins.list",
    "tuple": "builtins.tuple",
    "dict": "builtins.dict",
    "set": "builtins.set",
    "frozenset": "builtins.frozenset",
})

# Public target states the worker may legitimately return.
_PUBLIC_TARGET_STATES = frozenset({
    "idle", "paused", "exited", "failed", "terminated",
})

# Private transient values "starting" and "continuing" are used only while
# their execution-control call owns _request_lock.  They are never valid
# worker results or stable local lifecycle states.
# Local \"unknown\" is set only when a correlated worker response
# is operationally failed and the true lifecycle is not known.


def _has_raw_dotdot(script: str) -> bool:
    parts = script.replace('\\', '/').split('/')
    return '..' in parts


class PdbSessionState(Enum):
    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class _BoundedDiagnostics:
    def __init__(self, max_chars: int = _DEFAULT_MAX_DIAGNOSTICS) -> None:
        self._max = max_chars
        self._half = max_chars // 2 if max_chars > 1 else 0
        self._parts: List[str] = []
        self._total = 0
        self._overflowed = False
        self._head: List[str] = []
        self._head_len = 0
        self._tail: List[str] = []
        self._tail_len = 0

    def add(self, text: str) -> None:
        if not text:
            return
        n = len(text)
        if not self._overflowed:
            self._parts.append(text)
            self._total += n
            if self._total > self._max:
                self._flush_overflow()
        else:
            self._tail.append(text)
            self._tail_len += n
            self._trim_tail()

    def _flush_overflow(self) -> None:
        full = "".join(self._parts)
        self._parts = []
        self._head.append(full[: self._half])
        self._head_len = self._half
        rest = full[self._half:]
        if rest:
            if len(rest) > self._half:
                rest = rest[-self._half:]
            self._tail.append(rest)
            self._tail_len = len(rest)
        self._overflowed = True

    def _trim_tail(self) -> None:
        while self._tail_len > self._half:
            oldest = self._tail[0]
            excess = self._tail_len - self._half
            if len(oldest) <= excess:
                self._tail.pop(0)
                self._tail_len -= len(oldest)
            else:
                self._tail[0] = oldest[excess:]
                self._tail_len -= excess

    def getvalue(self) -> str:
        return self._build()

    def _build(self) -> str:
        if not self._overflowed:
            text = "".join(self._parts)
            if len(text) > self._max:
                text = text[: self._max]
            return text

        head = "".join(self._head)
        tail = "".join(self._tail)
        marker = _TRUNCATION_MARKER
        mlen = len(marker)

        if mlen >= self._max:
            return head[: self._max]

        half = self._half
        if half <= 0:
            budget = self._max - mlen
            if budget <= 0:
                return head[: self._max]
            return head[:budget] + marker

        max_tail = self._max - half - mlen
        if max_tail <= 0:
            budget = self._max - mlen
            if budget <= 0:
                return head[: self._max]
            return head[:budget] + marker

        if len(head) > half:
            head = head[:half]
        if len(tail) > max_tail:
            tail_start = len(tail) - max_tail
            if tail_start > 0:
                tail = tail[tail_start:]
            else:
                tail = tail[:max_tail]
        return head + marker + tail


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
        # The normal worker remains isolated from site packages.  The exact
        # public pytest proof opts into the single dependency root it needs;
        # this flag is never enabled by the default controller path.
        if type(proof_pytest_dependencies) is not bool:
            raise PdbSessionError("proof_pytest_dependencies must be a boolean")
        self._proof_pytest_dependencies = proof_pytest_dependencies
        # V2-01: an explicit base environment for the ordinary product PDB
        # worker (the project/PDB role derived by the session's
        # execution-environment authority).  ``None`` preserves the
        # historical inherit-from-parent behavior for harness/scientific
        # callers; contained/scientific subclasses override
        # ``_worker_env`` themselves either way.
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
        # Central Windows-venv launch authority: inside a Windows virtual
        # environment this is the real base interpreter (the venv
        # redirector would otherwise fork a grandchild whose PID can never
        # equal the Popen PID checked by the handshake).  The venv
        # identity itself travels via ``_worker_env``.
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

        self._start_reader_threads()

        try:
            self._handshake()
        except Exception as e:
            self._terminate_and_cleanup()
            self._transition_to_failed()
            raise

        with self._state_lock:
            if self._reader_error.is_set():
                self._state = PdbSessionState.FAILED
                raise PdbProtocolError(
                    "Response channel integrity lost during startup"
                )
            self._state = PdbSessionState.READY

    def _start_reader_threads(self) -> None:
        proc = self._proc
        if proc is None:
            return

        self._response_queue = queue.Queue(maxsize=_QUEUE_CAPACITY)
        max_line = self._max_line
        stop_ev = self._stop_event
        reader_err = self._reader_error

        def _stdout_reader() -> None:
            if proc.stdout is None:
                reader_err.set()
                self._schedule_overflow_cleanup()
                return
            try:
                while not stop_ev.is_set() and not reader_err.is_set():
                    line = proc.stdout.readline(max_line + 1)
                    if not line:
                        break
                    try:
                        self._response_queue.put(line, timeout=0.5)
                    except queue.Full:
                        reader_err.set()
                        self._schedule_overflow_cleanup()
                        return
            except Exception:
                pass
            finally:
                if not reader_err.is_set():
                    try:
                        self._response_queue.put(None, timeout=0.5)
                    except queue.Full:
                        reader_err.set()
                        self._schedule_overflow_cleanup()

        t_stdout = threading.Thread(target=_stdout_reader, daemon=True)
        t_stderr = threading.Thread(target=self._make_stderr_reader(proc), daemon=True)

        if self._reader_cleanup_done.is_set():
            return

        self._stdout_thread = t_stdout
        self._stderr_thread = t_stderr

        started_any = False
        try:
            t_stderr.start()
            started_any = True
            t_stdout.start()
        except Exception as e:
            self._stop_event.set()
            if proc.poll() is None:
                _terminate_process_group(proc)
            self._close_proc_pipes(proc)
            if started_any:
                t_stderr.join(timeout=_THREAD_JOIN_TIMEOUT)
            self._stderr_thread = None
            self._stdout_thread = None
            self._proc = None
            self._transition_to_failed()
            raise PdbSessionError(
                f"Failed to start reader thread: {e}"
            ) from e

    def _make_stderr_reader(self, proc: subprocess.Popen):
        stop_ev = self._stop_event

        def _stderr_reader() -> None:
            if proc.stderr is None:
                return
            try:
                while not stop_ev.is_set():
                    chunk = proc.stderr.read1(4096)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    with self._diag_lock:
                        self._diag_accum.add(text)
            except Exception:
                pass

        return _stderr_reader

    def _schedule_overflow_cleanup(self) -> None:
        """Schedule a one-shot cleanup coordinator thread on overflow."""
        if self._reader_cleanup_done.is_set() or self._reader_cleanup_started.is_set():
            return
        if not self._reader_cleanup_lock.acquire(blocking=False):
            return
        try:
            if self._reader_cleanup_started.is_set():
                return
            self._reader_cleanup_started.set()
        finally:
            self._reader_cleanup_lock.release()

        t = threading.Thread(
            target=self._automatic_overflow_cleanup,
            daemon=True,
        )
        self._reader_cleanup_thread = t
        t.start()

    def _automatic_overflow_cleanup(self) -> None:
        """One-shot cleanup coordinator.  Runs on its own daemon thread."""
        cleanup_error: Optional[Exception] = None
        try:
            self._transition_to_failed()
            self._reader_cleanup_reason = PdbProtocolError(
                "Response channel integrity lost"
            )
            self._stop_event.set()
            proc = self._proc
            if proc is not None:
                if proc.poll() is None:
                    _terminate_process_group(proc)
                self._close_proc_pipes(proc)

            for t in (self._stdout_thread, self._stderr_thread):
                if t is not None and t is not threading.current_thread():
                    try:
                        t.join(timeout=_THREAD_JOIN_TIMEOUT)
                        if t.is_alive():
                            raise PdbSessionError(
                                f"Reader thread {t.name} did not stop "
                                f"within {_THREAD_JOIN_TIMEOUT}s timeout"
                            )
                    except PdbSessionError as e:
                        if cleanup_error is None:
                            cleanup_error = e

            if cleanup_error is None:
                self._stdout_thread = None
                self._stderr_thread = None
                self._proc = None
        except Exception as e:
            if cleanup_error is None:
                cleanup_error = PdbSessionError(
                    f"Cleanup coordinator error: {e}"
                )
        finally:
            self._reader_cleanup_error = cleanup_error
            self._reader_cleanup_done.set()

    def _wait_for_reader_cleanup(self) -> None:
        """Wait for reader cleanup to complete, running it if needed."""
        if not self._reader_error.is_set():
            return
        if not self._reader_cleanup_started.is_set():
            self._schedule_overflow_cleanup()

        if self._reader_cleanup_done.wait(timeout=_CLEANUP_WAIT_TIMEOUT):
            if self._reader_cleanup_thread is not None:
                ct = self._reader_cleanup_thread
                if ct is not threading.current_thread():
                    ct.join(timeout=_THREAD_JOIN_TIMEOUT)
                self._reader_cleanup_thread = None

            reason = self._reader_cleanup_reason
            cerr = self._reader_cleanup_error
            if reason is not None:
                if cerr is not None:
                    raise reason from cerr
                raise reason
            if cerr is not None:
                raise PdbSessionError(
                    "Cleanup failed without primary error"
                ) from cerr
            return

        raise PdbSessionError(
            "Automatic reader cleanup did not complete "
            f"within {_CLEANUP_WAIT_TIMEOUT}s"
        )

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
                _validate_ping_response(response)
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

            script_val, source_bytes = self._validate_script_and_read(script)
            breakpoints_val = self._validate_breakpoints(
                breakpoints, source_bytes
            )
            argv_val = self._validate_argv(argv)

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
                    self._validate_run_result(
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

            script_val, source_bytes = self._validate_script_and_read(script)
            argv_val = self._validate_argv(argv)

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

            script_val, source_bytes = self._validate_script_and_read(script)
            breakpoints_val = self._validate_breakpoints(
                breakpoints, source_bytes
            )
            argv_val = self._validate_argv(argv)

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
                    state = self._validate_start_paused_result(
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

            try:
                state = self._validate_status_result(response)
            except (PdbProtocolError, PdbSessionError) as e:
                self._fail_and_cleanup(e)

            self._update_local_lifecycle(state)

            return dict(response.result)
        except Exception:
            raise
        finally:
            self._request_lock.release()

    def get_stack_summary(self) -> Dict[str, object]:
        return self._perform_inspection(
            "get_stack_summary", {}, None, None
        )

    def get_frame(
        self,
        frame_id: int,
        pause_generation: int,
    ) -> Dict[str, object]:
        frame_id_val, generation_val = self._validate_inspection_identifiers(
            frame_id, pause_generation
        )
        return self._perform_inspection(
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
        frame_id_val, generation_val = self._validate_inspection_identifiers(
            frame_id, pause_generation
        )
        return self._perform_inspection(
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
        frame_id_val, generation_val = self._validate_safe_eval_identifiers(
            frame_id, pause_generation
        )
        expression_val = self._validate_safe_eval_expression_input(expression)
        return self._perform_inspection(
            "safe_eval_expression",
            {
                "frame_id": frame_id_val,
                "pause_generation": generation_val,
                "expression": expression_val,
            },
            frame_id_val,
            generation_val,
        )

    @staticmethod
    def _validate_safe_eval_identifiers(
        frame_id: Any, pause_generation: Any
    ) -> Tuple[int, int]:
        if type(frame_id) is not int:
            raise PdbSessionError("frame_id must be an integer")
        if frame_id < 0:
            raise PdbSessionError("frame_id must be non-negative")
        if type(pause_generation) is not int:
            raise PdbSessionError("pause_generation must be an integer")
        if pause_generation <= 0:
            raise PdbSessionError("pause_generation must be positive")
        return frame_id, pause_generation

    @staticmethod
    def _validate_safe_eval_expression_input(expression: Any) -> str:
        if type(expression) is not str:
            raise PdbSessionError("expression must be a string")
        if not expression:
            raise PdbSessionError("expression must be non-empty")
        if expression != expression.strip():
            raise PdbSessionError(
                "expression must not have surrounding whitespace"
            )
        if any(ord(character) <= 0x1f or ord(character) == 0x7f
               for character in expression):
            raise PdbSessionError(
                "expression contains a prohibited control character"
            )
        try:
            encoded = expression.encode('utf-8')
        except UnicodeEncodeError as e:
            raise PdbSessionError("expression must be valid UTF-8") from e
        if len(encoded) > _MAX_EXPRESSION_UTF8:
            raise PdbSessionError("expression exceeds 1024 UTF-8 bytes")
        return expression

    @staticmethod
    def _validate_inspection_identifiers(
        frame_id: Any, pause_generation: Any
    ) -> Tuple[int, int]:
        if isinstance(frame_id, bool) or not isinstance(frame_id, int):
            raise PdbSessionError("frame_id must be an integer")
        if frame_id < 0:
            raise PdbSessionError("frame_id must be non-negative")
        if (isinstance(pause_generation, bool) or
                not isinstance(pause_generation, int)):
            raise PdbSessionError("pause_generation must be an integer")
        if pause_generation <= 0:
            raise PdbSessionError("pause_generation must be positive")
        return frame_id, pause_generation

    def _perform_inspection(
        self,
        operation: str,
        payload: Dict[str, Any],
        requested_frame_id: Optional[int],
        requested_generation: Optional[int],
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
                        f"Cannot {operation} from state "
                        f"{self._state.value}; expected READY"
                    )
                if self._target_lifecycle_state != "paused":
                    raise PdbSessionStateError(
                        f"Cannot {operation} in local lifecycle state "
                        f"{self._target_lifecycle_state!r}; expected 'paused'"
                    )
                active_script = self._active_script
                if not isinstance(active_script, str):
                    raise PdbSessionStateError(
                        f"Cannot {operation} without active target metadata"
                    )

            request = PdbRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id=self._allocate_request_id(),
                operation=operation,
                payload=payload,
            )
            response = self._send_and_receive(
                request, self._request_timeout
            )
            if not isinstance(response, PdbResponse):
                self._fail_and_cleanup(PdbProtocolError(
                    f"{operation} response must be a PdbResponse"
                ))
            if not response.success:
                if response.result != {}:
                    self._fail_and_cleanup(PdbProtocolError(
                        f"Failed {operation} response must have empty result, "
                        f"got {response.result}"
                    ))
                raise PdbSessionError(f"{operation} failed: {response.error}")

            try:
                if operation == "get_stack_summary":
                    self._validate_stack_summary_result(
                        response.result, active_script
                    )
                elif operation == "get_frame":
                    self._validate_frame_result(
                        response.result,
                        requested_frame_id,
                        requested_generation,
                        active_script,
                    )
                elif operation == "get_frame_locals":
                    self._validate_locals_result(
                        response.result,
                        requested_frame_id,
                        requested_generation,
                    )
                elif operation == "safe_eval_expression":
                    self._validate_safe_eval_result(
                        response.result,
                        requested_frame_id,
                        requested_generation,
                        payload['expression'],
                        active_script,
                    )
                else:
                    raise PdbProtocolError(
                        f"Unsupported inspection operation: {operation!r}"
                    )
                self._validate_successful_inspection_response_size(
                    response, operation
                )
            except PdbProtocolError as e:
                self._fail_and_cleanup(e)
            except Exception as e:
                self._fail_and_cleanup(PdbProtocolError(
                    f"Malformed {operation} successful result: "
                    f"{type(e).__name__}"
                ))
            return dict(response.result)
        finally:
            self._request_lock.release()

    @staticmethod
    def _validate_successful_inspection_response_size(
        response: PdbResponse,
        operation: str,
    ) -> None:
        try:
            serialize_response(response)
        except PdbProtocolError as e:
            raise PdbProtocolError(
                f"{operation} successful response exceeds the protocol "
                "line limit or is not serializable"
            ) from e

    def continue_paused_target(self) -> Dict[str, object]:
        return self._resume_paused_target(
            operation="continue_paused_target",
            verb="continue",
            transient_state="continuing",
            require_breakpoint_result=True,
        )

    def step_paused_target(self) -> Dict[str, object]:
        """Advance to the next traced line in the active target script."""

        return self._resume_paused_target(
            operation="step_paused_target",
            verb="step",
            transient_state="stepping",
            require_breakpoint_result=False,
        )

    def next_paused_target(self) -> Dict[str, object]:
        """Advance to the next traced line in the currently paused frame."""

        return self._resume_paused_target(
            operation="next_paused_target",
            verb="next",
            transient_state="nexting",
            require_breakpoint_result=False,
        )

    def _resume_paused_target(
        self,
        *,
        operation: str,
        verb: str,
        transient_state: str,
        require_breakpoint_result: bool,
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
                        f"Cannot {operation} from state "
                        f"{self._state.value}; expected READY"
                    )
                if self._target_lifecycle_state != "paused":
                    raise PdbSessionStateError(
                        f"Cannot {operation} in local lifecycle "
                        f"state {self._target_lifecycle_state!r}; "
                        f"expected 'paused'"
                    )
                active_script = self._active_script
                active_breakpoints = self._active_breakpoints
                if not isinstance(active_script, str):
                    raise PdbSessionStateError(
                        f"Cannot {operation} without active "
                        "target metadata"
                    )
                self._target_lifecycle_state = transient_state

            request = PdbRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id=self._allocate_request_id(),
                operation=operation,
                payload={},
            )
            response = self._send_and_receive(
                request, self._request_timeout
            )

            if not response.success:
                if response.result != {}:
                    self._fail_and_cleanup(
                        PdbProtocolError(
                            f"Failed {operation} response must "
                            f"have empty result, got {response.result}"
                        )
                    )
                self._update_local_lifecycle("unknown", active_script)
                raise PdbSessionError(
                    f"{verb.capitalize()} failed: {response.error}"
                )

            try:
                state = self._validate_persistent_outcome_result(
                    response,
                    active_script,
                    active_breakpoints if require_breakpoint_result else None,
                    operation,
                )
            except (PdbProtocolError, PdbSessionError) as e:
                self._fail_and_cleanup(e)

            self._update_local_lifecycle(state, active_script)
            return dict(response.result)
        except Exception:
            raise
        finally:
            with self._state_lock:
                if self._target_lifecycle_state == transient_state:
                    self._target_lifecycle_state = "unknown"
            self._request_lock.release()

    def terminate_paused_target(self) -> Dict[str, object]:
        if not self._request_lock.acquire(timeout=self._request_timeout):
            raise PdbSessionError(
                "A request is already in flight; only one "
                "in-flight request is supported"
            )
        try:
            with self._state_lock:
                if self._state != PdbSessionState.READY:
                    raise PdbSessionStateError(
                        f"Cannot terminate_paused_target from state "
                        f"{self._state.value}; expected READY"
                    )
                if self._target_lifecycle_state != "paused":
                    raise PdbSessionStateError(
                        f"Cannot terminate_paused_target in local lifecycle "
                        f"state {self._target_lifecycle_state!r}; "
                        f"expected 'paused'"
                    )
                active_script = self._active_script

            request = PdbRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id=self._allocate_request_id(),
                operation="terminate_paused_target",
                payload={},
            )

            response = self._send_and_receive(
                request, self._request_timeout
            )

            if not response.success:
                if response.result != {}:
                    self._fail_and_cleanup(
                        PdbProtocolError(
                            "Failed terminate_paused_target response must "
                            f"have empty result, got {response.result}"
                        )
                    )
                self._update_local_lifecycle("unknown", active_script)
                raise PdbSessionError(
                    f"Terminate failed: {response.error}"
                )

            try:
                self._validate_terminate_result(response, active_script)
            except (PdbProtocolError, PdbSessionError) as e:
                self._fail_and_cleanup(e)

            self._update_local_lifecycle("terminated", active_script)

            return dict(response.result)
        except Exception:
            raise
        finally:
            self._request_lock.release()

    @staticmethod
    def _check_utf8_strict(value: str, label: str) -> None:
        try:
            encoded = value.encode('utf-8')
        except UnicodeEncodeError as e:
            raise PdbProtocolError(
                f"{label} contains non-UTF-8-representable characters: {e}"
            ) from e
        return encoded

    @staticmethod
    def _read_bounded_fd(fd: int) -> bytes:
        buffer = bytearray()
        while True:
            remaining = _MAX_TARGET_SOURCE_BYTES + 1 - len(buffer)
            if remaining <= 0:
                break
            try:
                chunk = os.read(fd, min(64 * 1024, remaining))
            except OSError as e:
                raise PdbProtocolError(
                    f"cannot read script: {e}"
                ) from e
            if not chunk:
                break
            buffer.extend(chunk)
        if len(buffer) > _MAX_TARGET_SOURCE_BYTES:
            raise PdbProtocolError(
                "script exceeds maximum source size"
            )
        return bytes(buffer)

    def _read_validated_workspace_script(self, script_normalized: str) -> bytes:
        workspace_root = self._workspace.root
        abs_path = os.path.normpath(
            os.path.join(workspace_root, script_normalized)
        )

        try:
            fd = os.open(abs_path, os.O_RDONLY | _BINARY_OPEN_FLAG)
        except (FileNotFoundError, IsADirectoryError) as e:
            if os.path.isdir(abs_path):
                raise PdbProtocolError(
                    f"script is a directory: {script_normalized}"
                ) from e
            raise PdbProtocolError(
                f"script not found: {script_normalized}"
            ) from e
        except OSError as e:
            raise PdbProtocolError(
                f"cannot open script: {e}"
            ) from e

        try:
            try:
                opened_stat = os.fstat(fd)
            except OSError as e:
                raise PdbProtocolError(
                    f"cannot stat opened script: {e}"
                ) from e

            if not stat.S_ISREG(opened_stat.st_mode):
                raise PdbProtocolError(
                    f"script is not a regular file: {script_normalized}"
                )

            try:
                real_root = os.path.realpath(workspace_root)
                real_path = os.path.realpath(abs_path)
            except (ValueError, OSError) as e:
                raise PdbProtocolError(
                    f"cannot resolve script path: {e}"
                ) from e

            try:
                common = os.path.commonpath([real_root, real_path])
            except (ValueError, OSError) as e:
                raise PdbProtocolError(
                    f"script path containment check failed: {e}"
                ) from e

            if os.path.normcase(common) != os.path.normcase(real_root):
                raise PdbProtocolError(
                    "script escapes workspace via symlink or junction"
                )

            try:
                current_path_stat = os.stat(real_path)
            except OSError as e:
                raise PdbProtocolError(
                    f"cannot stat resolved script: {e}"
                ) from e

            if not os.path.samestat(opened_stat, current_path_stat):
                raise PdbProtocolError(
                    "script file changed between validation and open"
                )

            source_bytes = self._read_bounded_fd(fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        return source_bytes

    def _validate_script_and_read(self, script: str) -> Tuple[str, bytes]:
        if not isinstance(script, str) or not script:
            raise PdbProtocolError("script must be a non-empty string")

        if '\0' in script:
            raise PdbProtocolError("script contains NUL byte")

        if not script.endswith('.py'):
            raise PdbProtocolError(
                "script must end with .py"
            )

        self._check_utf8_strict(script, "script")

        if len(script.encode('utf-8')) > _MAX_SCRIPT_PATH_UTF8:
            raise PdbProtocolError(
                f"script path exceeds {_MAX_SCRIPT_PATH_UTF8} UTF-8 bytes"
            )

        if len(script) >= 2 and script[1] == ':':
            raise PdbProtocolError("script must be a relative path")

        if script.startswith('/') or script.startswith('\\'):
            raise PdbProtocolError("script must be a relative path")

        if _has_raw_dotdot(script):
            raise PdbProtocolError(
                "script must not contain .. traversal"
            )

        normalized = os.path.normpath(script)

        if os.path.isabs(normalized):
            raise PdbProtocolError("script must be a relative path")

        normalized = normalized.replace('\\', '/')

        source_bytes = self._read_validated_workspace_script(normalized)

        return (normalized, source_bytes)

    def _validate_breakpoints(
        self, breakpoints: Sequence[int], source_bytes: bytes = b""
    ) -> List[int]:
        if not isinstance(breakpoints, (list, tuple)):
            raise PdbProtocolError("breakpoints must be a list")

        if len(breakpoints) < 1 or len(breakpoints) > 16:
            raise PdbProtocolError(
                "breakpoints must have 1-16 entries"
            )

        bps: List[int] = []
        for bp in breakpoints:
            if isinstance(bp, bool) or not isinstance(bp, int):
                raise PdbProtocolError(
                    "breakpoints must contain only integers"
                )
            if bp <= 0:
                raise PdbProtocolError(
                    "breakpoints must be positive integers"
                )
            bps.append(bp)

        if len(set(bps)) != len(bps):
            raise PdbProtocolError(
                "breakpoints must not contain duplicates"
            )

        bps.sort()

        if source_bytes:
            line_count = len(source_bytes.splitlines())
            for bp_line in bps:
                if bp_line > line_count:
                    raise PdbProtocolError(
                        f"breakpoint line {bp_line} exceeds "
                        f"source length ({line_count})"
                    )

        return bps

    def _validate_argv(self, argv: Sequence[str]) -> List[str]:
        if not isinstance(argv, (list, tuple)):
            raise PdbProtocolError("argv must be a list")

        if len(argv) > 32:
            raise PdbProtocolError(
                "argv must have at most 32 entries"
            )

        av: List[str] = []
        for a in argv:
            if isinstance(a, bool) or not isinstance(a, str):
                raise PdbProtocolError(
                    "argv entries must be strings"
                )
            if '\0' in a:
                raise PdbProtocolError(
                    "argv entry contains NUL byte"
                )
            self._check_utf8_strict(a, "argv entry")
            if len(a.encode('utf-8')) > 1024:
                raise PdbProtocolError(
                    f"argv entry exceeds 1024 UTF-8 bytes"
                )
            av.append(a)

        return av

    @staticmethod
    def _validate_run_result(
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

    @staticmethod
    def _validate_bounded_protocol_string(
        v: Any, label: str, max_utf8: int = _MAX_SCRIPT_PATH_UTF8,
    ) -> str:
        if not isinstance(v, str):
            raise PdbProtocolError(f"{label} must be a string")
        if not v:
            raise PdbProtocolError(f"{label} must be non-empty")
        if '\0' in v:
            raise PdbProtocolError(f"{label} contains NUL byte")
        try:
            encoded = v.encode('utf-8')
        except UnicodeEncodeError as e:
            raise PdbProtocolError(
                f"{label} contains non-UTF-8-representable characters: {e}"
            ) from e
        if len(encoded) > max_utf8:
            raise PdbProtocolError(
                f"{label} exceeds {max_utf8} UTF-8 bytes"
            )
        return v

    @staticmethod
    def _validate_result_script(v: Any, label: str) -> str:
        import posixpath
        script = PdbSession._validate_bounded_protocol_string(
            v, label, _MAX_SCRIPT_PATH_UTF8
        )
        if '\\' in script:
            raise PdbProtocolError(
                f"{label} must use forward slashes, got {script!r}"
            )
        if not script.endswith('.py'):
            raise PdbProtocolError(f"{label} must end with .py")
        if len(script) >= 2 and script[1] == ':':
            raise PdbProtocolError(f"{label} must be a relative path")
        if script.startswith('/'):
            raise PdbProtocolError(f"{label} must be a relative path")
        if _has_raw_dotdot(script):
            raise PdbProtocolError(
                f"{label} must not contain .. traversal"
            )
        normalized = posixpath.normpath(script)
        if normalized != script:
            raise PdbProtocolError(
                f"{label} must already be a normalized forward-slash "
                f"path, got {script!r}"
            )
        if normalized in ("", ".", "..") or normalized.startswith("/"):
            raise PdbProtocolError(
                f"{label} is not a valid relative path, got {script!r}"
            )
        return script

    @staticmethod
    def _validate_int_strict_field(v: Any, label: str) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise PdbProtocolError(
                f"{label} must be an integer, got {type(v).__name__}"
            )
        return v

    @staticmethod
    def _check_exact_fields(
        result: dict, required: frozenset, label: str
    ) -> None:
        extra = set(result.keys()) - required
        if extra:
            raise PdbProtocolError(
                f"Unknown fields in {label}: {sorted(extra)}"
            )
        missing = required - set(result.keys())
        if missing:
            raise PdbProtocolError(
                f"Missing fields in {label}: {sorted(missing)}"
            )

    @staticmethod
    def _validate_bool_field(v: Any, label: str) -> bool:
        if not isinstance(v, bool):
            raise PdbProtocolError(f"{label} must be a boolean")
        return v

    def _validate_inspection_script(self, v: Any, label: str) -> str:
        script = self._validate_result_script(v, label)
        root = os.path.realpath(os.path.abspath(self._workspace.root))
        candidate = os.path.realpath(os.path.abspath(os.path.join(
            root, script.replace('/', os.sep)
        )))
        try:
            common = os.path.commonpath((root, candidate))
        except ValueError as e:
            raise PdbProtocolError(
                f"{label} does not resolve inside the active workspace"
            ) from e
        if os.path.normcase(common) != os.path.normcase(root):
            raise PdbProtocolError(
                f"{label} resolves outside the active workspace"
            )
        return script

    def _validate_frame_summary_mapping(
        self,
        frame: Any,
        expected_id: int,
        label: str,
    ) -> None:
        if not isinstance(frame, dict):
            raise PdbProtocolError(f"{label} must be a mapping")
        self._check_exact_fields(frame, _FRAME_SUMMARY_FIELDS, label)
        frame_id = self._validate_int_strict_field(
            frame['frame_id'], f"{label} frame_id"
        )
        if frame_id != expected_id:
            raise PdbProtocolError(
                f"{label} frame_id must be {expected_id}, got {frame_id}"
            )
        self._validate_inspection_script(frame['script'], f"{label} script")
        line = self._validate_int_strict_field(
            frame['line'], f"{label} line"
        )
        if line <= 0:
            raise PdbProtocolError(f"{label} line must be positive")
        self._validate_bounded_protocol_string(
            frame['function'], f"{label} function", _MAX_RESULT_FUNCTION_UTF8
        )
        is_current = self._validate_bool_field(
            frame['is_current'], f"{label} is_current"
        )
        if is_current != (frame_id == 0):
            raise PdbProtocolError(
                f"{label} is_current does not match frame_id"
            )

    def _validate_stack_summary_result(
        self, result: Any, active_script: str
    ) -> None:
        if not isinstance(result, dict):
            raise PdbProtocolError(
                "get_stack_summary result must be a mapping"
            )
        self._check_exact_fields(
            result, _STACK_RESULT_FIELDS, "get_stack_summary result"
        )
        if type(result['state']) is not str or result['state'] != 'paused':
            raise PdbProtocolError(
                "get_stack_summary result state must be 'paused'"
            )
        script = self._validate_inspection_script(
            result['script'], "get_stack_summary result script"
        )
        if script != active_script:
            raise PdbProtocolError(
                f"get_stack_summary script {script!r} does not match "
                f"active {active_script!r}"
            )
        generation = self._validate_int_strict_field(
            result['pause_generation'],
            "get_stack_summary pause_generation",
        )
        if generation <= 0:
            raise PdbProtocolError(
                "get_stack_summary pause_generation must be positive"
            )
        frames = result['frames']
        if not isinstance(frames, list):
            raise PdbProtocolError("get_stack_summary frames must be a list")
        if not frames:
            raise PdbProtocolError(
                "get_stack_summary must contain current frame zero"
            )
        if len(frames) > _MAX_STACK_FRAMES:
            raise PdbProtocolError(
                "get_stack_summary returned too many frames"
            )
        for expected_id, frame in enumerate(frames):
            self._validate_frame_summary_mapping(
                frame, expected_id,
                f"get_stack_summary frame {expected_id}",
            )
        if frames[0]['script'] != script:
            raise PdbProtocolError(
                "get_stack_summary current frame script does not match target"
            )
        current_count = sum(
            1 for frame in frames if frame['is_current'] is True
        )
        if current_count != 1:
            raise PdbProtocolError(
                "get_stack_summary must contain exactly one current frame"
            )
        total = self._validate_int_strict_field(
            result['total_frames'], "get_stack_summary total_frames"
        )
        if total < 0 or total < len(frames):
            raise PdbProtocolError(
                "get_stack_summary total_frames is inconsistent"
            )
        truncated = self._validate_bool_field(
            result['truncated'], "get_stack_summary truncated"
        )
        if truncated != (total > len(frames)):
            raise PdbProtocolError(
                "get_stack_summary count/truncated fields are inconsistent"
            )

    def _validate_name_list(
        self,
        value: Any,
        label: str,
        *,
        sorted_required: bool,
        maximum_count: Optional[int] = None,
    ) -> List[str]:
        if not isinstance(value, list):
            raise PdbProtocolError(f"{label} must be a list")
        if maximum_count is not None and len(value) > maximum_count:
            raise PdbProtocolError(f"{label} contains too many names")
        names: List[str] = []
        for index, name in enumerate(value):
            names.append(self._validate_bounded_protocol_string(
                name, f"{label}[{index}]", _MAX_INSPECTION_NAME_UTF8
            ))
        if len(set(names)) != len(names):
            raise PdbProtocolError(f"{label} contains duplicate names")
        if sorted_required and names != sorted(names):
            raise PdbProtocolError(f"{label} must be sorted")
        return names

    def _validate_frame_result(
        self,
        result: Any,
        requested_frame_id: Optional[int],
        requested_generation: Optional[int],
        active_script: str,
    ) -> None:
        if not isinstance(result, dict):
            raise PdbProtocolError("get_frame result must be a mapping")
        self._check_exact_fields(result, _FRAME_RESULT_FIELDS, "get_frame result")
        if type(result['state']) is not str or result['state'] != 'paused':
            raise PdbProtocolError("get_frame result state must be 'paused'")
        generation = self._validate_int_strict_field(
            result['pause_generation'], "get_frame pause_generation"
        )
        if generation <= 0 or generation != requested_generation:
            raise PdbProtocolError(
                "get_frame result pause_generation does not match request"
            )
        frame = result['frame']
        if not isinstance(frame, dict):
            raise PdbProtocolError("get_frame frame must be a mapping")
        self._check_exact_fields(frame, _FRAME_DETAIL_FIELDS, "get_frame frame")
        if requested_frame_id is None:
            raise PdbProtocolError("get_frame request frame_id is unavailable")
        summary = {
            key: frame[key] for key in _FRAME_SUMMARY_FIELDS
        }
        self._validate_frame_summary_mapping(
            summary, requested_frame_id, "get_frame frame"
        )
        if frame['function'] == '<module>':
            raise PdbProtocolError(
                "get_frame successful result must not expose a module frame"
            )
        if requested_frame_id == 0 and frame['script'] != active_script:
            raise PdbProtocolError(
                "get_frame current frame script does not match active target"
            )
        self._validate_name_list(
            frame['argument_names'], "get_frame argument_names",
            sorted_required=False,
        )
        local_names = self._validate_name_list(
            frame['local_names'], "get_frame local_names",
            sorted_required=True, maximum_count=_MAX_LOCAL_NAMES,
        )
        count = self._validate_int_strict_field(
            frame['locals_count'], "get_frame locals_count"
        )
        if count < 0 or count < len(local_names):
            raise PdbProtocolError("get_frame locals_count is inconsistent")
        truncated = self._validate_bool_field(
            frame['locals_truncated'], "get_frame locals_truncated"
        )
        if truncated != (count > len(local_names)):
            raise PdbProtocolError(
                "get_frame locals count/truncation is inconsistent"
            )

    @staticmethod
    def _validate_nonnegative_int(v: Any, label: str) -> int:
        value = PdbSession._validate_int_strict_field(v, label)
        if value < 0:
            raise PdbProtocolError(f"{label} must be non-negative")
        return value

    def _validate_value_summary(
        self,
        summary: Any,
        label: str,
        depth: int = 0,
    ) -> None:
        if not isinstance(summary, dict):
            raise PdbProtocolError(f"{label} must be a mapping")
        self._check_exact_fields(summary, _VALUE_SUMMARY_FIELDS, label)
        kind = summary['kind']
        if type(kind) is not str or kind not in _VALUE_KINDS:
            raise PdbProtocolError(f"{label} has invalid kind")
        type_name = self._validate_bounded_protocol_string(
            summary['type'], f"{label} type", _MAX_TYPE_NAME_UTF8
        )
        if kind == 'object':
            if type_name != 'unknown':
                module_name, separator, qualname = type_name.partition('.')
                if not separator or not module_name or not qualname:
                    raise PdbProtocolError(
                        f"{label} object type must be module.qualname or unknown"
                    )
        elif type_name != _CANONICAL_VALUE_TYPES[kind]:
            raise PdbProtocolError(
                f"{label} type does not match kind {kind!r}"
            )
        truncated = self._validate_bool_field(
            summary['truncated'], f"{label} truncated"
        )
        items = summary['items']
        entries = summary['entries']
        if not isinstance(items, list) or not isinstance(entries, list):
            raise PdbProtocolError(f"{label} items and entries must be lists")

        value = summary['value']
        special = summary['special']
        size = summary['size']
        if kind == 'none':
            if (value is not None or special is not None or size is not None or
                    items or entries or truncated):
                raise PdbProtocolError(f"{label} none summary is inconsistent")
            return
        if kind == 'bool':
            if (type(value) is not bool or special is not None or
                    size is not None or items or entries or truncated):
                raise PdbProtocolError(f"{label} bool summary is inconsistent")
            return
        if kind == 'int':
            bits = self._validate_nonnegative_int(size, f"{label} size")
            if special is not None or items or entries:
                raise PdbProtocolError(f"{label} int summary is inconsistent")
            if truncated:
                if value is not None or bits <= _MAX_SERIALIZED_INT_BITS:
                    raise PdbProtocolError(
                        f"{label} truncated int is inconsistent"
                    )
            elif (type(value) is not int or int.bit_length(value) != bits or
                  bits > _MAX_SERIALIZED_INT_BITS):
                raise PdbProtocolError(f"{label} int summary is inconsistent")
            return
        if kind == 'float':
            if size is not None or items or entries or truncated:
                raise PdbProtocolError(f"{label} float summary is inconsistent")
            if special is None:
                if type(value) is not float or not math.isfinite(value):
                    raise PdbProtocolError(
                        f"{label} finite float summary is inconsistent"
                    )
            elif (special not in ('nan', 'inf', '-inf') or
                  value is not None):
                raise PdbProtocolError(
                    f"{label} special float summary is inconsistent"
                )
            return
        if kind == 'str':
            count = self._validate_nonnegative_int(size, f"{label} size")
            if type(value) is not str or special is not None or items or entries:
                raise PdbProtocolError(f"{label} str summary is inconsistent")
            try:
                encoded = value.encode('utf-8')
            except UnicodeEncodeError as e:
                raise PdbProtocolError(f"{label} str value is not UTF-8") from e
            if len(encoded) > _MAX_STRING_PREVIEW_UTF8 or count < len(value):
                raise PdbProtocolError(f"{label} str summary exceeds bounds")
            if truncated != (count > len(value)):
                raise PdbProtocolError(
                    f"{label} str truncation is inconsistent"
                )
            return
        if kind == 'bytes':
            count = self._validate_nonnegative_int(size, f"{label} size")
            if (type(value) is not str or special is not None or items or entries or
                    len(value) % 2 or len(value) > _MAX_BYTES_PREVIEW * 2 or
                    any(c not in '0123456789abcdef' for c in value)):
                raise PdbProtocolError(f"{label} bytes summary is inconsistent")
            preview_size = len(value) // 2
            if count < preview_size or truncated != (count > preview_size):
                raise PdbProtocolError(
                    f"{label} bytes truncation is inconsistent"
                )
            return

        if kind == 'object':
            if (value is not None or special is not None or size is not None or
                    items or entries or truncated):
                raise PdbProtocolError(f"{label} object summary is inconsistent")
            return

        count = self._validate_nonnegative_int(size, f"{label} size")
        if value is not None or special is not None:
            raise PdbProtocolError(f"{label} container scalar fields are invalid")
        if kind in ('set', 'frozenset'):
            if items or entries or truncated != (count > 0):
                raise PdbProtocolError(f"{label} set summary is inconsistent")
            return
        if depth >= _MAX_CONTAINER_DEPTH and (items or entries):
            raise PdbProtocolError(f"{label} exceeds maximum recursion depth")
        if kind in ('list', 'tuple'):
            if entries or len(items) > _MAX_CONTAINER_ITEMS or count < len(items):
                raise PdbProtocolError(f"{label} sequence summary is inconsistent")
            if not truncated and count != len(items):
                raise PdbProtocolError(f"{label} sequence omission is unmarked")
            for index, item in enumerate(items):
                self._validate_value_summary(
                    item, f"{label} items[{index}]", depth + 1
                )
            return
        if items or len(entries) > _MAX_CONTAINER_ITEMS or count < len(entries):
            raise PdbProtocolError(f"{label} dict summary is inconsistent")
        if not truncated and count != len(entries):
            raise PdbProtocolError(f"{label} dict omission is unmarked")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise PdbProtocolError(
                    f"{label} entries[{index}] must be a mapping"
                )
            self._check_exact_fields(
                entry, _DICT_ENTRY_FIELDS, f"{label} entries[{index}]"
            )
            self._validate_value_summary(
                entry['key'], f"{label} entries[{index}] key", depth + 1
            )
            self._validate_value_summary(
                entry['value'], f"{label} entries[{index}] value", depth + 1
            )

    def _validate_locals_result(
        self,
        result: Any,
        requested_frame_id: Optional[int],
        requested_generation: Optional[int],
    ) -> None:
        if not isinstance(result, dict):
            raise PdbProtocolError("get_frame_locals result must be a mapping")
        self._check_exact_fields(
            result, _LOCALS_RESULT_FIELDS, "get_frame_locals result"
        )
        if type(result['state']) is not str or result['state'] != 'paused':
            raise PdbProtocolError(
                "get_frame_locals result state must be 'paused'"
            )
        generation = self._validate_int_strict_field(
            result['pause_generation'],
            "get_frame_locals pause_generation",
        )
        if generation <= 0 or generation != requested_generation:
            raise PdbProtocolError(
                "get_frame_locals pause_generation does not match request"
            )
        frame_id = self._validate_int_strict_field(
            result['frame_id'], "get_frame_locals frame_id"
        )
        if frame_id < 0 or frame_id != requested_frame_id:
            raise PdbProtocolError(
                "get_frame_locals frame_id does not match request"
            )
        locals_value = result['locals']
        if not isinstance(locals_value, list):
            raise PdbProtocolError("get_frame_locals locals must be a list")
        if len(locals_value) > _MAX_LOCAL_NAMES:
            raise PdbProtocolError("get_frame_locals returned too many locals")
        names: List[str] = []
        for index, entry in enumerate(locals_value):
            if not isinstance(entry, dict):
                raise PdbProtocolError(
                    f"get_frame_locals locals[{index}] must be a mapping"
                )
            self._check_exact_fields(
                entry, _LOCAL_ENTRY_FIELDS,
                f"get_frame_locals locals[{index}]",
            )
            names.append(self._validate_bounded_protocol_string(
                entry['name'], f"get_frame_locals locals[{index}] name",
                _MAX_INSPECTION_NAME_UTF8,
            ))
            self._validate_value_summary(
                entry['value'], f"get_frame_locals locals[{index}] value"
            )
        if names != sorted(names) or len(set(names)) != len(names):
            raise PdbProtocolError(
                "get_frame_locals names must be unique and sorted"
            )
        total = self._validate_nonnegative_int(
            result['total_count'], "get_frame_locals total_count"
        )
        if total < len(locals_value):
            raise PdbProtocolError(
                "get_frame_locals total_count is inconsistent"
            )
        truncated = self._validate_bool_field(
            result['truncated'], "get_frame_locals truncated"
        )
        if truncated != (total > len(locals_value)):
            raise PdbProtocolError(
                "get_frame_locals count/truncation is inconsistent"
            )
        try:
            encoded = json.dumps(
                result,
                ensure_ascii=False,
                separators=(',', ':'),
                sort_keys=True,
                allow_nan=False,
            ).encode('utf-8')
        except (TypeError, ValueError, UnicodeEncodeError) as e:
            raise PdbProtocolError(
                "get_frame_locals result is not compact valid JSON"
            ) from e
        if len(encoded) > _MAX_LOCALS_RESULT_BYTES:
            raise PdbProtocolError(
                "get_frame_locals result exceeds 32768-byte budget"
            )

    def _validate_safe_eval_result(
        self,
        result: Any,
        requested_frame_id: Optional[int],
        requested_generation: Optional[int],
        requested_expression: Any,
        active_script: str,
    ) -> None:
        if not isinstance(result, dict):
            raise PdbProtocolError(
                "safe_eval_expression result must be a mapping"
            )
        self._check_exact_fields(
            result, _SAFE_EVAL_RESULT_FIELDS,
            "safe_eval_expression result",
        )
        if type(result['state']) is not str or result['state'] != 'paused':
            raise PdbProtocolError(
                "safe_eval_expression result state must be 'paused'"
            )
        if type(result['pause_generation']) is not int:
            raise PdbProtocolError(
                "safe_eval_expression pause_generation must be an integer"
            )
        generation = result['pause_generation']
        if generation <= 0 or generation != requested_generation:
            raise PdbProtocolError(
                "safe_eval_expression pause_generation does not match request"
            )
        if requested_frame_id is None:
            raise PdbProtocolError(
                "safe_eval_expression request frame_id is unavailable"
            )
        frame = result['frame']
        if not isinstance(frame, dict):
            raise PdbProtocolError(
                "safe_eval_expression frame must be a mapping"
            )
        if (type(frame.get('script')) is not str or
                type(frame.get('function')) is not str or
                type(frame.get('frame_id')) is not int or
                type(frame.get('line')) is not int):
            raise PdbProtocolError(
                "safe_eval_expression frame strings must be exact strings"
            )
        self._validate_frame_summary_mapping(
            frame, requested_frame_id, "safe_eval_expression frame"
        )
        if frame['function'] == '<module>':
            raise PdbProtocolError(
                "safe_eval_expression successful result must not expose "
                "a module frame"
            )
        if requested_frame_id == 0 and frame['script'] != active_script:
            raise PdbProtocolError(
                "safe_eval_expression current frame does not match target"
            )
        expression = result['expression']
        if type(expression) is not str or expression != requested_expression:
            raise PdbProtocolError(
                "safe_eval_expression expression does not match request"
            )
        try:
            self._validate_safe_eval_expression_input(expression)
        except PdbSessionError as e:
            raise PdbProtocolError(
                "safe_eval_expression result expression is invalid"
            ) from e
        self._validate_value_summary(
            result['value'], "safe_eval_expression value"
        )
        try:
            encoded = json.dumps(
                result,
                ensure_ascii=False,
                separators=(',', ':'),
                sort_keys=True,
                allow_nan=False,
            ).encode('utf-8')
        except (
            TypeError, ValueError, UnicodeEncodeError, MemoryError,
            RecursionError,
        ) as e:
            raise PdbProtocolError(
                "safe_eval_expression result is not compact valid JSON"
            ) from e
        if len(encoded) > _MAX_SAFE_EVAL_RESULT_BYTES:
            raise PdbProtocolError(
                "safe_eval_expression result exceeds 32768-byte budget"
            )

    def _validate_start_paused_result(
        self,
        response: PdbResponse,
        expected_script: str,
        expected_breakpoints: Sequence[int],
    ) -> str:
        return self._validate_persistent_outcome_result(
            response,
            expected_script,
            expected_breakpoints,
            "start_paused_target",
        )

    def _validate_continue_result(
        self,
        response: PdbResponse,
        expected_script: str,
        expected_breakpoints: Optional[Sequence[int]],
    ) -> str:
        return self._validate_persistent_outcome_result(
            response,
            expected_script,
            expected_breakpoints,
            "continue_paused_target",
        )

    def _validate_persistent_outcome_result(
        self,
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
            self._check_exact_fields(
                result, _PAUSED_RESULT_FIELDS,
                f"{operation} paused result"
            )
            script = self._validate_result_script(
                result["script"],
                f"{operation} paused result script"
            )
            if script != expected_script:
                raise PdbProtocolError(
                    f"{operation} paused result script {script!r} "
                    f"does not match expected {expected_script!r}"
                )
            line = self._validate_int_strict_field(
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
            self._validate_bounded_protocol_string(
                result["function"],
                f"{operation} paused result function",
                _MAX_RESULT_FUNCTION_UTF8,
            )
        elif state == "exited":
            self._check_exact_fields(
                result, _EXITED_RESULT_FIELDS,
                f"{operation} exited result"
            )
            script = self._validate_result_script(
                result["script"], f"{operation} exited result script"
            )
            if script != expected_script:
                raise PdbProtocolError(
                    f"{operation} exited result script {script!r} "
                    f"does not match expected {expected_script!r}"
                )
            self._validate_int_strict_field(
                result["exit_code"],
                f"{operation} exited result exit_code"
            )
        return state

    def _validate_status_result(
        self, response: PdbResponse
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
        with self._state_lock:
            budget_consumed = self._target_consumed
            active_script = self._active_script
            active_bps = self._active_breakpoints

        if state == "idle":
            self._check_exact_fields(
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
                self._check_exact_fields(
                    result, _STATUS_PAUSED_FIELDS,
                    "get_target_status paused result"
                )
                script = self._validate_result_script(
                    result["script"],
                    "get_target_status paused result script"
                )
                if script != active_script:
                    raise PdbProtocolError(
                        f"get_target_status paused script {script!r} "
                        f"does not match active {active_script!r}"
                    )
                line = self._validate_int_strict_field(
                    result["line"],
                    "get_target_status paused result line"
                )
                if line <= 0:
                    raise PdbProtocolError(
                        "get_target_status paused result line "
                        "must be positive"
                    )
                if active_bps is not None and line not in active_bps:
                    raise PdbProtocolError(
                        f"get_target_status paused result line {line} "
                        f"is not among active breakpoints "
                        f"{list(active_bps)}"
                    )
                self._validate_bounded_protocol_string(
                    result["function"],
                    "get_target_status paused result function",
                    _MAX_RESULT_FUNCTION_UTF8,
                )
            elif state == "exited":
                self._check_exact_fields(
                    result, _STATUS_EXITED_FIELDS,
                    "get_target_status exited result"
                )
                script = self._validate_result_script(
                    result["script"],
                    "get_target_status exited result script"
                )
                if script != active_script:
                    raise PdbProtocolError(
                        f"get_target_status exited script {script!r} "
                        f"does not match active {active_script!r}"
                    )
                self._validate_int_strict_field(
                    result["exit_code"],
                    "get_target_status exited result exit_code"
                )
            elif state == "failed":
                self._check_exact_fields(
                    result, _STATUS_FAILED_FIELDS,
                    "get_target_status failed result"
                )
                script = self._validate_result_script(
                    result["script"],
                    "get_target_status failed result script"
                )
                if script != active_script:
                    raise PdbProtocolError(
                        f"get_target_status failed script {script!r} "
                        f"does not match active {active_script!r}"
                    )
                self._validate_bounded_protocol_string(
                    result["error"],
                    "get_target_status failed result error",
                    _MAX_RESULT_ERROR_UTF8,
                )
            elif state == "terminated":
                self._check_exact_fields(
                    result, _STATUS_TERMINATED_FIELDS,
                    "get_target_status terminated result"
                )
                script = self._validate_result_script(
                    result["script"],
                    "get_target_status terminated result script"
                )
                if script != active_script:
                    raise PdbProtocolError(
                        f"get_target_status terminated script {script!r} "
                        f"does not match active {active_script!r}"
                    )
        return state

    def _validate_terminate_result(
        self,
        response: PdbResponse,
        expected_script: str,
    ) -> None:
        result = response.result
        if not isinstance(result, dict):
            raise PdbProtocolError(
                "terminate_paused_target result must be a mapping"
            )
        self._check_exact_fields(
            result, _TERMINATED_RESULT_FIELDS,
            "terminate_paused_target result"
        )
        state = result.get("state")
        if state != "terminated":
            raise PdbProtocolError(
                f"terminate_paused_target result state must be "
                f"'terminated', got {state!r}"
            )
        script = self._validate_result_script(
            result["script"],
            "terminate_paused_target result script"
        )
        if script != expected_script:
            raise PdbProtocolError(
                f"terminate_paused_target result script {script!r} "
                f"does not match expected {expected_script!r}"
            )

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
        proc = self._proc
        if proc is None:
            self._transition_to_failed()
            raise PdbSessionError("Worker process not available")

        if proc.stdin is None:
            self._transition_to_failed()
            raise PdbSessionError("Worker stdin pipe not available")

        if self._reader_error.is_set():
            self._wait_for_reader_cleanup()

        data = serialize_request(request)
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except OSError as e:
            self._fail_and_cleanup(
                PdbWorkerExitedError(
                    f"Failed to write to worker stdin: {e}"
                )
            )

        try:
            line_data = self._response_queue.get(timeout=timeout)
        except queue.Empty:
            if self._reader_error.is_set():
                self._wait_for_reader_cleanup()
            poll = proc.poll()
            if poll is not None:
                self._fail_and_cleanup(
                    PdbWorkerExitedError(
                        f"Worker exited with code {poll} "
                        f"while waiting for response"
                    )
                )
            else:
                self._fail_and_cleanup(
                    PdbSessionTimeoutError(
                        f"Request timed out after {timeout}s"
                    )
                )

        if line_data is None:
            self._fail_and_cleanup(
                PdbWorkerExitedError(
                    "Worker closed stdout while waiting for response "
                    f"(exit code: {proc.poll()})"
                )
            )

        if self._reader_error.is_set():
            self._wait_for_reader_cleanup()

        if len(line_data) > self._max_line:
            self._fail_and_cleanup(
                PdbProtocolError(
                    f"Response line exceeds maximum length "
                    f"({len(line_data)} > {self._max_line})"
                )
            )

        try:
            response = deserialize_response(line_data)
        except PdbProtocolError as e:
            self._fail_and_cleanup(e)

        if response.protocol_version != PROTOCOL_VERSION:
            self._fail_and_cleanup(
                PdbProtocolError(
                    f"Protocol version mismatch: "
                    f"{response.protocol_version} != {PROTOCOL_VERSION}"
                )
            )

        if response.request_id != request.request_id:
            self._fail_and_cleanup(
                PdbProtocolError(
                    f"Request ID mismatch: "
                    f"sent {request.request_id}, "
                    f"got {response.request_id}"
                )
            )

        return response

    def stop(self) -> None:
        with self._state_lock:
            if self._state == PdbSessionState.STOPPED:
                return
            self._state = PdbSessionState.STOPPING

        if self._request_lock.acquire(
            timeout=_STOP_REQUEST_LOCK_TIMEOUT
        ):
            try:
                self._shutdown_worker_if_ready()
            finally:
                self._request_lock.release()
        else:
            self._terminate_and_cleanup()

        self._finalize_after_stop()

        with self._state_lock:
            self._state = PdbSessionState.STOPPED

    def _shutdown_worker_if_ready(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is None:
            return
        if proc.poll() is not None:
            return

        request = PdbRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=self._allocate_request_id(),
            operation="shutdown",
            payload={},
        )
        data = serialize_request(request)
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except OSError:
            self._terminate_and_cleanup()
            return

        ack_line = None
        try:
            ack_line = self._response_queue.get(
                timeout=self._shutdown_timeout
            )
        except queue.Empty:
            self._terminate_and_cleanup()
            return

        if ack_line is None:
            self._terminate_and_cleanup()
            return

        try:
            ack = deserialize_response(ack_line)
        except PdbProtocolError:
            self._terminate_and_cleanup()
            return

        if not _validate_shutdown_ack(ack, request.request_id):
            self._terminate_and_cleanup()
            return

        if not _wait_proc(proc, self._shutdown_timeout):
            self._terminate_and_cleanup()

    def _terminate_and_cleanup(self) -> None:
        proc = self._proc
        if proc is None:
            return

        self._stop_event.set()

        if proc.poll() is None:
            _terminate_process_group(proc)

        self._close_proc_pipes(proc)

        for t in (self._stdout_thread, self._stderr_thread):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=_THREAD_JOIN_TIMEOUT)
                if t.is_alive():
                    raise PdbSessionError(
                        f"Reader thread {t.name} did not stop "
                        f"within {_THREAD_JOIN_TIMEOUT}s timeout"
                    )

        self._stdout_thread = None
        self._stderr_thread = None
        self._proc = None

    def _finalize_after_stop(self) -> None:
        self._stop_event.set()
        proc = self._proc
        if proc is not None:
            self._close_proc_pipes(proc)
        for t in (self._stdout_thread, self._stderr_thread):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=_THREAD_JOIN_TIMEOUT)
                if t.is_alive():
                    raise PdbSessionError(
                        f"Reader thread {t.name} did not stop "
                        f"within {_THREAD_JOIN_TIMEOUT}s timeout"
                    )
        self._stdout_thread = None
        self._stderr_thread = None
        self._proc = None

    def _close_proc_pipes(self, proc: subprocess.Popen) -> None:
        for handle_name in ("stdin", "stdout", "stderr"):
            handle = getattr(proc, handle_name, None)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

    def _fail_and_cleanup(self, reason: Exception) -> None:
        self._transition_to_failed()
        self._terminate_and_cleanup()
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


def _validate_ping_response(response: PdbResponse) -> None:
    if not response.success:
        raise PdbSessionError(
            f"Ping failed: {response.error}"
        )
    if response.error:
        raise PdbProtocolError(
            f"Ping response has error field set despite success=True: "
            f"{response.error!r}"
        )
    result = response.result
    if not isinstance(result, dict):
        raise PdbProtocolError(
            "Ping response result must be a mapping"
        )
    extra = set(result.keys()) - _PING_KNOWN_FIELDS
    if extra:
        raise PdbProtocolError(
            f"Unknown fields in ping result: {sorted(extra)}"
        )
    missing = _PING_REQUIRED_FIELDS - set(result.keys())
    if missing:
        raise PdbProtocolError(
            f"Missing required ping result fields: {sorted(missing)}"
        )
    if result.get("status") != "ok":
        raise PdbProtocolError(
            f"Ping response status is not 'ok': "
            f"{result.get('status')!r}"
        )
    if result.get("pdb_created") is not True:
        raise PdbProtocolError(
            f"Ping response pdb_created is not True: "
            f"{result.get('pdb_created')!r}"
        )


def _validate_shutdown_ack(ack: PdbResponse, expected_id: int) -> bool:
    if not ack.success:
        return False
    if ack.error:
        return False
    if ack.protocol_version != PROTOCOL_VERSION:
        return False
    if ack.request_id != expected_id:
        return False
    result = ack.result
    if not isinstance(result, dict):
        return False
    extra = set(result.keys()) - _SHUTDOWN_KNOWN_FIELDS
    if extra:
        return False
    missing = _SHUTDOWN_REQUIRED_FIELDS - set(result.keys())
    if missing:
        return False
    if result.get("shutdown") is not True:
        return False
    return True


def _terminate_process_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            _wait_proc(proc, _PROCESS_TERMINATE_WAIT)
            if proc.poll() is not None:
                return
            proc.terminate()
            _wait_proc(proc, _PROCESS_TERMINATE_WAIT)
            if proc.poll() is not None:
                return
            proc.kill()
            _wait_proc(proc, _PROCESS_KILL_WAIT)
        else:
            try:
                child_pgid = os.getpgid(proc.pid)
                own_pgid = os.getpgid(os.getpid())
                if child_pgid != own_pgid:
                    os.killpg(child_pgid, signal.SIGTERM)
                    _wait_proc(proc, _PROCESS_TERMINATE_WAIT)
                    if proc.poll() is not None:
                        return
                    os.killpg(child_pgid, signal.SIGKILL)
                else:
                    proc.terminate()
                    _wait_proc(proc, _PROCESS_TERMINATE_WAIT)
                    if proc.poll() is not None:
                        return
                    proc.kill()
            except ProcessLookupError:
                return
            except OSError:
                proc.terminate()
            _wait_proc(proc, _PROCESS_TERMINATE_WAIT)
            if proc.poll() is not None:
                return
            proc.kill()
            _wait_proc(proc, _PROCESS_KILL_WAIT)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    _wait_proc(proc, _PROCESS_KILL_WAIT)


def _wait_proc(proc: subprocess.Popen, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False
