from __future__ import annotations

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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

        self._workspace = workspace
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._max_diagnostics = max_diagnostics
        self._max_line = max_line

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
        bootstrap = (
            "import sys; import runpy; "
            "sys.path.insert(0, " + repr(project_root) + "); "
            "runpy.run_module("
            "'agentic_debugger.runtime.pdb_worker', run_name='__main__')"
        )
        return [
            sys.executable,
            "-I",
            "-u",
            "-c",
            bootstrap,
        ]

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
                cwd=self._workspace.root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
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

        if worker_info.pid != self._proc.pid:
            raise PdbSessionError(
                f"Worker PID mismatch: handshake reported "
                f"{worker_info.pid}, actual {self._proc.pid}"
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
            else:
                if response.result != {}:
                    self._fail_and_cleanup(
                        PdbProtocolError(
                            "Failed response must have empty result, "
                            f"got {response.result}"
                        )
                    )

            return response
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
                    "Worker closed stdout while waiting for response"
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
