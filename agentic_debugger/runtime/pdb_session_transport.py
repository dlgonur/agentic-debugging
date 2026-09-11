"""Process/transport authority for the PDB session.

Owns worker-reader threads, bounded diagnostics capture, overflow detection
and automatic cleanup, JSON request/response exchange, shutdown
acknowledgement, process-group termination, pipe closure and reader-thread
cleanup.  The session remains the sole owner of all mutable state
(subprocess identity, request IDs, locks, queues, diagnostics accumulator,
stop/failure flags); every function here operates on the session object
passed in and never duplicates that state.

The four contained-launch extension points (``_get_worker_argv``,
``_worker_env``, ``_worker_cwd``, ``_expected_worker_pid``) stay on the
session so ``ContainedPdbSession`` overrides remain effective; nothing here
binds to base-class helpers directly.

Dependency direction: depends on ``pdb_session_limits``, exceptions and
protocol.  It must not import validation, inspection, outcome, control or
session modules at runtime (``TYPE_CHECKING`` only), keeping the graph
acyclic: session -> transport.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Optional

from agentic_debugger.runtime.exceptions import (
    PdbProtocolError,
    PdbSessionError,
    PdbSessionTimeoutError,
    PdbWorkerExitedError,
)
from agentic_debugger.runtime.pdb_protocol import (
    PROTOCOL_VERSION,
    PdbRequest,
    PdbResponse,
    deserialize_response,
    serialize_request,
)
from agentic_debugger.runtime.pdb_session_limits import (
    _CLEANUP_WAIT_TIMEOUT,
    _PING_KNOWN_FIELDS,
    _PING_REQUIRED_FIELDS,
    _PROCESS_KILL_WAIT,
    _PROCESS_TERMINATE_WAIT,
    _QUEUE_CAPACITY,
    _SHUTDOWN_KNOWN_FIELDS,
    _SHUTDOWN_REQUIRED_FIELDS,
    _THREAD_JOIN_TIMEOUT,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime cycle
    from agentic_debugger.runtime.pdb_session import PdbSession


def validate_ping_response(response: PdbResponse) -> None:
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


def validate_shutdown_ack(ack: PdbResponse, expected_id: int) -> bool:
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


def terminate_process_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            wait_proc(proc, _PROCESS_TERMINATE_WAIT)
            if proc.poll() is not None:
                return
            proc.terminate()
            wait_proc(proc, _PROCESS_TERMINATE_WAIT)
            if proc.poll() is not None:
                return
            proc.kill()
            wait_proc(proc, _PROCESS_KILL_WAIT)
        else:
            try:
                child_pgid = os.getpgid(proc.pid)
                own_pgid = os.getpgid(os.getpid())
                if child_pgid != own_pgid:
                    os.killpg(child_pgid, signal.SIGTERM)
                    wait_proc(proc, _PROCESS_TERMINATE_WAIT)
                    if proc.poll() is not None:
                        return
                    os.killpg(child_pgid, signal.SIGKILL)
                else:
                    proc.terminate()
                    wait_proc(proc, _PROCESS_TERMINATE_WAIT)
                    if proc.poll() is not None:
                        return
                    proc.kill()
            except ProcessLookupError:
                return
            except OSError:
                proc.terminate()
            wait_proc(proc, _PROCESS_TERMINATE_WAIT)
            if proc.poll() is not None:
                return
            proc.kill()
            wait_proc(proc, _PROCESS_KILL_WAIT)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    wait_proc(proc, _PROCESS_KILL_WAIT)


def wait_proc(proc: subprocess.Popen, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def close_proc_pipes(proc: subprocess.Popen) -> None:
    for handle_name in ("stdin", "stdout", "stderr"):
        handle = getattr(proc, handle_name, None)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def make_stderr_reader(session: PdbSession, proc: subprocess.Popen):
    stop_ev = session._stop_event

    def _stderr_reader() -> None:
        if proc.stderr is None:
            return
        try:
            while not stop_ev.is_set():
                chunk = proc.stderr.read1(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                with session._diag_lock:
                    session._diag_accum.add(text)
        except Exception:
            pass

    return _stderr_reader


def start_reader_threads(session: PdbSession) -> None:
    proc = session._proc
    if proc is None:
        return

    session._response_queue = queue.Queue(maxsize=_QUEUE_CAPACITY)
    max_line = session._max_line
    stop_ev = session._stop_event
    reader_err = session._reader_error

    def _stdout_reader() -> None:
        if proc.stdout is None:
            reader_err.set()
            schedule_overflow_cleanup(session)
            return
        try:
            while not stop_ev.is_set() and not reader_err.is_set():
                line = proc.stdout.readline(max_line + 1)
                if not line:
                    break
                try:
                    session._response_queue.put(line, timeout=0.5)
                except queue.Full:
                    reader_err.set()
                    schedule_overflow_cleanup(session)
                    return
        except Exception:
            pass
        finally:
            if not reader_err.is_set():
                try:
                    session._response_queue.put(None, timeout=0.5)
                except queue.Full:
                    reader_err.set()
                    schedule_overflow_cleanup(session)

    t_stdout = threading.Thread(target=_stdout_reader, daemon=True)
    t_stderr = threading.Thread(
        target=make_stderr_reader(session, proc), daemon=True
    )

    if session._reader_cleanup_done.is_set():
        return

    session._stdout_thread = t_stdout
    session._stderr_thread = t_stderr

    started_any = False
    try:
        t_stderr.start()
        started_any = True
        t_stdout.start()
    except Exception as e:
        session._stop_event.set()
        if proc.poll() is None:
            terminate_process_group(proc)
        close_proc_pipes(proc)
        if started_any:
            t_stderr.join(timeout=_THREAD_JOIN_TIMEOUT)
        session._stderr_thread = None
        session._stdout_thread = None
        session._proc = None
        session._transition_to_failed()
        raise PdbSessionError(
            f"Failed to start reader thread: {e}"
        ) from e


def schedule_overflow_cleanup(session: PdbSession) -> None:
    """Schedule a one-shot cleanup coordinator thread on overflow."""
    if session._reader_cleanup_done.is_set() or session._reader_cleanup_started.is_set():
        return
    if not session._reader_cleanup_lock.acquire(blocking=False):
        return
    try:
        if session._reader_cleanup_started.is_set():
            return
        session._reader_cleanup_started.set()
    finally:
        session._reader_cleanup_lock.release()

    t = threading.Thread(
        target=lambda: automatic_overflow_cleanup(session),
        daemon=True,
    )
    session._reader_cleanup_thread = t
    t.start()


def automatic_overflow_cleanup(session: PdbSession) -> None:
    """One-shot cleanup coordinator.  Runs on its own daemon thread."""
    cleanup_error: Optional[Exception] = None
    try:
        session._transition_to_failed()
        session._reader_cleanup_reason = PdbProtocolError(
            "Response channel integrity lost"
        )
        session._stop_event.set()
        proc = session._proc
        if proc is not None:
            if proc.poll() is None:
                terminate_process_group(proc)
            close_proc_pipes(proc)

        for t in (session._stdout_thread, session._stderr_thread):
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
            session._stdout_thread = None
            session._stderr_thread = None
            session._proc = None
    except Exception as e:
        if cleanup_error is None:
            cleanup_error = PdbSessionError(
                f"Cleanup coordinator error: {e}"
            )
    finally:
        session._reader_cleanup_error = cleanup_error
        session._reader_cleanup_done.set()


def wait_for_reader_cleanup(session: PdbSession) -> None:
    """Wait for reader cleanup to complete, running it if needed."""
    if not session._reader_error.is_set():
        return
    if not session._reader_cleanup_started.is_set():
        schedule_overflow_cleanup(session)

    if session._reader_cleanup_done.wait(timeout=_CLEANUP_WAIT_TIMEOUT):
        if session._reader_cleanup_thread is not None:
            ct = session._reader_cleanup_thread
            if ct is not threading.current_thread():
                ct.join(timeout=_THREAD_JOIN_TIMEOUT)
            session._reader_cleanup_thread = None

        reason = session._reader_cleanup_reason
        cerr = session._reader_cleanup_error
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


def send_and_receive(
    session: PdbSession, request: PdbRequest, timeout: float
) -> PdbResponse:
    proc = session._proc
    if proc is None:
        session._transition_to_failed()
        raise PdbSessionError("Worker process not available")

    if proc.stdin is None:
        session._transition_to_failed()
        raise PdbSessionError("Worker stdin pipe not available")

    if session._reader_error.is_set():
        wait_for_reader_cleanup(session)

    data = serialize_request(request)
    try:
        proc.stdin.write(data)
        proc.stdin.flush()
    except OSError as e:
        session._fail_and_cleanup(
            PdbWorkerExitedError(
                f"Failed to write to worker stdin: {e}"
            )
        )

    try:
        line_data = session._response_queue.get(timeout=timeout)
    except queue.Empty:
        if session._reader_error.is_set():
            wait_for_reader_cleanup(session)
        poll = proc.poll()
        if poll is not None:
            session._fail_and_cleanup(
                PdbWorkerExitedError(
                    f"Worker exited with code {poll} "
                    f"while waiting for response"
                )
            )
        else:
            session._fail_and_cleanup(
                PdbSessionTimeoutError(
                    f"Request timed out after {timeout}s"
                )
            )

    if line_data is None:
        session._fail_and_cleanup(
            PdbWorkerExitedError(
                "Worker closed stdout while waiting for response "
                f"(exit code: {proc.poll()})"
            )
        )

    if session._reader_error.is_set():
        wait_for_reader_cleanup(session)

    if len(line_data) > session._max_line:
        session._fail_and_cleanup(
            PdbProtocolError(
                f"Response line exceeds maximum length "
                f"({len(line_data)} > {session._max_line})"
            )
        )

    try:
        response = deserialize_response(line_data)
    except PdbProtocolError as e:
        session._fail_and_cleanup(e)

    if response.protocol_version != PROTOCOL_VERSION:
        session._fail_and_cleanup(
            PdbProtocolError(
                f"Protocol version mismatch: "
                f"{response.protocol_version} != {PROTOCOL_VERSION}"
            )
        )

    if response.request_id != request.request_id:
        session._fail_and_cleanup(
            PdbProtocolError(
                f"Request ID mismatch: "
                f"sent {request.request_id}, "
                f"got {response.request_id}"
            )
        )

    return response


def shutdown_worker_if_ready(session: PdbSession) -> None:
    proc = session._proc
    if proc is None:
        return
    if proc.stdin is None:
        return
    if proc.poll() is not None:
        return

    request = PdbRequest(
        protocol_version=PROTOCOL_VERSION,
        request_id=session._allocate_request_id(),
        operation="shutdown",
        payload={},
    )
    data = serialize_request(request)
    try:
        proc.stdin.write(data)
        proc.stdin.flush()
    except OSError:
        terminate_and_cleanup(session)
        return

    ack_line = None
    try:
        ack_line = session._response_queue.get(
            timeout=session._shutdown_timeout
        )
    except queue.Empty:
        terminate_and_cleanup(session)
        return

    if ack_line is None:
        terminate_and_cleanup(session)
        return

    try:
        ack = deserialize_response(ack_line)
    except PdbProtocolError:
        terminate_and_cleanup(session)
        return

    if not validate_shutdown_ack(ack, request.request_id):
        terminate_and_cleanup(session)
        return

    if not wait_proc(proc, session._shutdown_timeout):
        terminate_and_cleanup(session)


def terminate_and_cleanup(session: PdbSession) -> None:
    proc = session._proc
    if proc is None:
        return

    session._stop_event.set()

    if proc.poll() is None:
        terminate_process_group(proc)

    close_proc_pipes(proc)

    for t in (session._stdout_thread, session._stderr_thread):
        if t is not None and t is not threading.current_thread():
            t.join(timeout=_THREAD_JOIN_TIMEOUT)
            if t.is_alive():
                raise PdbSessionError(
                    f"Reader thread {t.name} did not stop "
                    f"within {_THREAD_JOIN_TIMEOUT}s timeout"
                )

    session._stdout_thread = None
    session._stderr_thread = None
    session._proc = None


def finalize_after_stop(session: PdbSession) -> None:
    session._stop_event.set()
    proc = session._proc
    if proc is not None:
        close_proc_pipes(proc)
    for t in (session._stdout_thread, session._stderr_thread):
        if t is not None and t is not threading.current_thread():
            t.join(timeout=_THREAD_JOIN_TIMEOUT)
            if t.is_alive():
                raise PdbSessionError(
                    f"Reader thread {t.name} did not stop "
                    f"within {_THREAD_JOIN_TIMEOUT}s timeout"
                )
    session._stdout_thread = None
    session._stderr_thread = None
    session._proc = None
