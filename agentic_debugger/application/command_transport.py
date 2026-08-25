"""Cancellable configured command-model transport for the application worker.

Task 8 reuses the accepted existing JSON-lines command transport
(``evaluation.live.JsonlCommandTransport``) for configured command-model
sessions.  The scientific transport has no external cancellation API and
terminates only the direct process on timeout; the application session needs
three bounded additions without changing the wire protocol or the response
validation contract:

- **Cooperative cancellation**: ``request`` polls an optional
  :class:`~agentic_debugger.cancellation.CancellationToken` check while the
  command is running; cancellation terminates the command tree promptly and
  re-raises :class:`CancellationError` (never a model/transport error), so
  the worker classifies the session as cancelled rather than failed;
- **Bounded explicit environment/cwd**: the profile's bounded environment
  overrides merge over the accepted minimal transport environment; the
  inherited process environment is never serialized into evidence;
- **Process-tree termination**: on Windows the command tree is terminated
  with ``taskkill /T /F`` (an explicit standard utility invocation, never a
  shell string), with the accepted CTRL_BREAK/terminate/kill group ladder as
  the fallback; POSIX uses the accepted process-group ladder.  On POSIX the
  request-owned group is additionally cleaned on EVERY request exit —
  including a normal successful or naturally failed command exit, when the
  direct process is already reaped — using the authoritative group id known
  at spawn time, so a completed request can never leave live descendants
  behind in its request-owned group.

The protocol contract is byte-for-byte the existing one: one JSON-lines
request on stdin, one JSON object on stdout, bounded stdout/stderr captures,
identical error kinds (``request_serialization``, ``launch_error``,
``request_timeout``, ``response_too_large``, ``process_error``,
``invalid_response``).  Malformed output is never reinterpreted as a valid
directive; the controller remains the directive authority.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Optional

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.process_tree import (
    register_request_group,
    terminate_process_group,
    terminate_request_group_id,
    terminate_request_process_group,
    unregister_request_group,
)
from agentic_debugger.evaluation.live import (
    JsonlCommandTransport,
    LiveConfigurationError,
    LiveModelConfig,
    LiveTransportError,
    MAX_MODEL_RESPONSE_BYTES,
    _typed_command_error_detail,
)

_POLL_INTERVAL_SECONDS = 0.05
_TERMINATE_JOIN_SECONDS = 2.0
_READ_CHUNK_BYTES = 8192
#: The stdin writer is joined in bounded slices so an explicit cancellation
#: interrupts a blocked request write promptly (a child that never reads
#: stdin fills the OS pipe and blocks the writer indefinitely); the request
#: inactivity watchdog is honored by the same loop.
_WRITE_JOIN_SLICE_SECONDS = 0.05


class _BoundedCapture:
    """Thread-safe bounded byte capture (mirrors the accepted live policy)."""

    def __init__(self, maximum_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        self._data = bytearray()
        self.truncated = False
        self._lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        with self._lock:
            remaining = self.maximum_bytes - len(self._data)
            if remaining > 0:
                self._data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.truncated = True

    def text(self) -> str:
        with self._lock:
            return bytes(self._data).decode("utf-8", errors="replace")


def _read_pipe(
    pipe: Any,
    capture: _BoundedCapture,
    activity: Optional[Callable[[], None]] = None,
) -> None:
    try:
        read_chunk = getattr(pipe, "read1", pipe.read)
        while True:
            chunk = read_chunk(_READ_CHUNK_BYTES)
            if not chunk:
                return
            capture.add(chunk)
            if activity is not None:
                activity()
    except Exception:
        return


def _terminate_command_tree(process: subprocess.Popen) -> None:
    """Terminate the command and its whole descendant tree, best effort.

    Windows: ``taskkill /PID <pid> /T /F`` terminates the tree without
    requiring a job object (the command already lives inside the session
    worker's accepted Windows job, which covers worker escalation and app
    exit; per-request timeout/cancel needs this explicit tree kill).  The
    accepted CTRL_BREAK/terminate/kill group ladder is the fallback on every
    platform and guarantees the direct process is reaped.

    POSIX: the command was spawned into its own request-owned process group
    (``start_new_session``), so the group ladder signals the command AND
    every descendant in that group; the worker-lifecycle registry + SIGTERM
    handler covers the forced/cooperative worker-shutdown path.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
                shell=False,
            )
        except Exception:
            pass
        terminate_process_group(process)
    else:
        terminate_request_process_group(process)


class CancellableJsonlCommandTransport(JsonlCommandTransport):
    """The accepted JSON-lines command transport with session cancellation.

    Same protocol and error vocabulary as the scientific transport; the
    only behavioral additions are the cooperative cancellation poll, the
    bounded explicit environment/cwd, and tree-wide termination.
    """

    def __init__(
        self,
        config: LiveModelConfig,
        *,
        max_output_bytes: int = MAX_MODEL_RESPONSE_BYTES,
        cancel_check: Optional[Callable[[], None]] = None,
        cwd: Optional[str] = None,
        environment: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(config, max_output_bytes=max_output_bytes)
        if cancel_check is not None and not callable(cancel_check):
            raise ApplicationInputError("cancel_check must be callable or None")
        if cwd is not None:
            if type(cwd) is not str or not cwd:
                raise ApplicationInputError("cwd must be a non-empty string or None")
            if not os.path.isabs(cwd):
                raise ApplicationInputError("cwd must be an absolute path or None")
        validated_environment: Optional[dict[str, str]] = None
        if environment is not None:
            if not isinstance(environment, Mapping):
                raise ApplicationInputError(
                    "environment must be a mapping of strings or None"
                )
            validated_environment = {}
            for name, value in environment.items():
                if type(name) is not str or type(value) is not str:
                    raise ApplicationInputError(
                        "environment overrides must be string pairs"
                    )
                validated_environment[name] = value
        self._cancel_check = cancel_check
        self._cwd = cwd
        self._environment = validated_environment
        # NOTE: the worker-lifecycle cleanup ownership for request-owned
        # process groups (the POSIX SIGTERM handler that kills every in-flight
        # group on worker shutdown) is installed by the worker process itself
        # (``worker.run_worker``), not here.  Installing a signal handler in
        # the transport constructor would mutate the signal state of any
        # process that merely constructs a transport (e.g. the unit-test
        # runner); the worker is the correct lifecycle owner.  The transport
        # still registers/unregisters each request group below.

    def request(self, payload: Any, timeout_seconds: float) -> Mapping[str, Any]:
        """One bounded JSON-lines model request with cancellation polling.

        The wire protocol, stdout/stderr bounds, and response validation are
        identical to the accepted scientific transport.  Cooperative
        cancellation terminates the command tree and re-raises
        :class:`CancellationError` (the worker's neutral cancellation
        signal); a request timeout terminates the tree and raises
        ``LiveTransportError(request_timeout)`` exactly like the accepted
        transport.

        Cancellation is honored at every wait, including while the request
        writer is blocked on a full stdin pipe (a child that never reads
        stdin): the writer is joined in bounded slices that poll both the
        cancellation check and the request inactivity watchdog, so an explicit user
        cancellation can never be masked into ``request_timeout`` by a
        blocked write.
        """
        if self._cancel_check is not None:
            # Cancellation boundary before any process is spawned.
            self._cancel_check()

        try:
            request_bytes = (
                json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise LiveTransportError(
                "model request could not be serialized", kind="request_serialization"
            ) from None

        environment = dict(self.subprocess_environment())
        if self._environment is not None:
            environment.update(self._environment)
        try:
            process = subprocess.Popen(
                list(self.config.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=environment,
                cwd=self._cwd,
                # POSIX: the command runs in its own request-owned process
                # group/session so the per-request cancellation/timeout ladder
                # can kill the command AND every descendant in that group.
                # The worker-lifecycle registry + SIGTERM handler covers the
                # forced/cooperative worker-shutdown path.  Windows keeps the
                # accepted CREATE_NEW_PROCESS_GROUP (the Job Object owns the
                # tree there).
                start_new_session=sys.platform != "win32",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt"
                    else 0
                ),
            )
        except (OSError, ValueError):
            raise LiveTransportError(
                "model command could not be launched", kind="launch_error"
            ) from None

        # With start_new_session the child is its own group leader, so the
        # request-owned group id is the child pid.  Register it for the
        # worker-lifecycle cleanup and unregister on every exit path below.
        request_group_id = process.pid if sys.platform != "win32" else None
        if request_group_id is not None:
            register_request_group(request_group_id)
        try:
            return self._run_request(process, request_bytes, timeout_seconds)
        finally:
            # Request-owned group ownership invariant (POSIX): when request()
            # leaves for ANY reason — success, invalid response, non-zero
            # exit, cancellation, timeout, or a bounded transport failure —
            # no ordinary descendant may remain in the request-owned group.
            # The explicit cancellation/timeout ladders already terminate the
            # tree while the command is alive; this final cleanup covers the
            # normal/natural exit paths, where the direct process is already
            # reaped and os.getpgid(proc.pid) can no longer resolve the group
            # even while descendants with the original group id are alive.
            # The authoritative group id is the one known at spawn time.
            if request_group_id is not None:
                try:
                    terminate_request_group_id(request_group_id)
                finally:
                    # Unregister only after the final group cleanup was
                    # attempted, so the worker-lifecycle SIGTERM handler
                    # still owns any in-flight group up to this point.
                    unregister_request_group(request_group_id)

    def _run_request(
        self,
        process: subprocess.Popen,
        request_bytes: bytes,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        """Drive one spawned request to a validated response (internal).

        Split out of :meth:`request` so the request-owned process group can be
        registered/unregistered around the whole request lifetime in one
        place; every cancellation, timeout, and error path still terminates
        the tree before returning, and :meth:`request` performs the final
        request-owned group cleanup on every exit path (including a normal
        command exit) before unregistering the group.
        """
        stdout = _BoundedCapture(self.max_output_bytes)
        stderr = _BoundedCapture(self.max_output_bytes)
        activity_lock = threading.Lock()
        last_activity = [time.monotonic()]

        def mark_activity() -> None:
            with activity_lock:
                last_activity[0] = time.monotonic()

        def idle_expired() -> bool:
            with activity_lock:
                return time.monotonic() - last_activity[0] >= timeout_seconds

        threads = [
            threading.Thread(target=_read_pipe, args=(process.stdout, stdout, mark_activity), daemon=True),
            threading.Thread(target=_read_pipe, args=(process.stderr, stderr, mark_activity), daemon=True),
        ]
        for thread in threads:
            thread.start()

        write_error: list[BaseException] = []

        def write_request() -> None:
            try:
                assert process.stdin is not None
                process.stdin.write(request_bytes)
                process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                write_error.append(exc)

        writer = threading.Thread(target=write_request, daemon=True)
        writer.start()

        def _interrupt_writer() -> None:
            """Unblock a stdin writer safely after the tree is terminated.

            Terminating the child closes the pipe's read end, so the blocked
            write fails promptly and the writer thread exits; only then is the
            parent's stdin handle closed (closing it while the writer still
            holds the buffer lock would serialize on the same lock).  Every
            step is bounded; the writer is a daemon thread, so a residual
            writer can never block the transport's return.
            """
            writer.join(timeout=_TERMINATE_JOIN_SECONDS)
            if not writer.is_alive():
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                except Exception:
                    pass

        # Wait for the request writer in bounded slices: both an explicit
        # cancellation and the request inactivity watchdog must win promptly, even when
        # the write itself is blocked on a full pipe.
        while writer.is_alive():
            if self._cancel_check is not None:
                try:
                    self._cancel_check()
                except BaseException:
                    _terminate_command_tree(process)
                    _interrupt_writer()
                    for thread in threads:
                        thread.join(timeout=_TERMINATE_JOIN_SECONDS)
                    raise
            if idle_expired():
                _terminate_command_tree(process)
                _interrupt_writer()
                for thread in threads:
                    thread.join(timeout=_TERMINATE_JOIN_SECONDS)
                raise LiveTransportError(
                    "model request stdin was idle for too long",
                    kind="request_timeout",
                    timed_out=True,
                ) from None
            writer.join(timeout=_WRITE_JOIN_SLICE_SECONDS)

        mark_activity()

        while True:
            if self._cancel_check is not None:
                try:
                    self._cancel_check()
                except BaseException:
                    # Cancellation (or any terminal signal from the session
                    # token) must interrupt the command promptly and is
                    # re-raised unchanged: it is never converted into a
                    # model/transport failure.
                    _terminate_command_tree(process)
                    for thread in threads:
                        thread.join(timeout=_TERMINATE_JOIN_SECONDS)
                    raise
            try:
                process.wait(timeout=_POLL_INTERVAL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if idle_expired():
                    _terminate_command_tree(process)
                    for thread in threads:
                        thread.join(timeout=_TERMINATE_JOIN_SECONDS)
                    raise LiveTransportError(
                        "model request was idle for too long",
                        kind="request_timeout",
                        timed_out=True,
                    ) from None

        for thread in threads:
            thread.join(timeout=_TERMINATE_JOIN_SECONDS)
        if stdout.truncated:
            raise LiveTransportError(
                "model response exceeded the configured output bound",
                kind="response_too_large",
            )
        if process.returncode != 0:
            typed_detail = _typed_command_error_detail(stderr.text())
            raise LiveTransportError(
                "model command failed",
                kind=(typed_detail[0] if typed_detail is not None else "process_error"),
                safe_message=(typed_detail[1] if typed_detail is not None else None),
            )
        try:
            value = json.loads(stdout.text())
        except (UnicodeError, json.JSONDecodeError):
            raise LiveTransportError(
                "model response was invalid JSON", kind="invalid_response"
            ) from None
        if not isinstance(value, Mapping):
            raise LiveTransportError(
                "model response was not an object", kind="invalid_response"
            )
        return value


__all__ = [
    "CancellableJsonlCommandTransport",
    "_BoundedCapture",
    "_terminate_command_tree",
]
