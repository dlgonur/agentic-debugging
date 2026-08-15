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
  the fallback; POSIX uses the accepted process-group ladder.

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
from agentic_debugger.application.process_tree import terminate_process_group
from agentic_debugger.evaluation.live import (
    JsonlCommandTransport,
    LiveConfigurationError,
    LiveModelConfig,
    LiveTransportError,
    MAX_MODEL_RESPONSE_BYTES,
)

_POLL_INTERVAL_SECONDS = 0.05
_TERMINATE_JOIN_SECONDS = 2.0
_READ_CHUNK_BYTES = 8192
#: The stdin writer is joined in bounded slices so an explicit cancellation
#: interrupts a blocked request write promptly (a child that never reads
#: stdin fills the OS pipe and blocks the writer indefinitely); the request
#: deadline is honored by the same loop.
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


def _read_pipe(pipe: Any, capture: _BoundedCapture) -> None:
    try:
        while True:
            chunk = pipe.read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            capture.add(chunk)
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
        cancellation check and the request deadline, so an explicit user
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

        stdout = _BoundedCapture(self.max_output_bytes)
        stderr = _BoundedCapture(self.max_output_bytes)
        threads = [
            threading.Thread(target=_read_pipe, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=_read_pipe, args=(process.stderr, stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()

        deadline = time.monotonic() + timeout_seconds
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
        # cancellation and the request deadline must win promptly, even when
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
            if time.monotonic() >= deadline:
                _terminate_command_tree(process)
                _interrupt_writer()
                for thread in threads:
                    thread.join(timeout=_TERMINATE_JOIN_SECONDS)
                raise LiveTransportError(
                    "model request stdin write timed out",
                    kind="request_timeout",
                    timed_out=True,
                ) from None
            writer.join(timeout=_WRITE_JOIN_SLICE_SECONDS)

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
                if time.monotonic() >= deadline:
                    _terminate_command_tree(process)
                    for thread in threads:
                        thread.join(timeout=_TERMINATE_JOIN_SECONDS)
                    raise LiveTransportError(
                        "model request timed out",
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
            raise LiveTransportError("model command failed", kind="process_error")
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
