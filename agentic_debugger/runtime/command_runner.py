from __future__ import annotations

import codecs
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from agentic_debugger.cancellation import CancellationError
from agentic_debugger.runtime.exceptions import (
    CommandExecutionError,
    CommandRequestError,
)
from agentic_debugger.runtime.execution import VerifiedExecutionContext
from agentic_debugger.runtime.workspace import TaskWorkspace

_MAX_OUTPUT_CHARS = 20_000
_TRUNCATION_MARKER = "\n... [output truncated] ...\n"
_THREAD_JOIN_TIMEOUT = 5.0
_POLL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class CommandResult:
    argv: List[str]
    cwd: str
    exit_code: Optional[int]
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
        }


class _BoundedStreamAccumulator:
    """Accumulates stream output; stores up to max_chars before truncating.

    Before overflow, all output is stored verbatim.
    On the first character beyond max_chars the entire content is converted into
    a head portion (first half) and a rolling tail window (last half).
    Subsequent output goes only into the tail window.
    Uses incremental UTF-8 decoding so multi-byte characters are not corrupted.

    An optional neutral output ``sanitizer`` (duck-typed ``feed(str)->str`` /
    ``flush()->str``, one independent instance per stream) is applied to the
    complete decoded stream text BEFORE any bounding: text it emits is what
    gets bounded.  When absent, behavior is byte-identical to the historical
    runner.  When present, ``stdout_truncated``/``stderr_truncated`` keep
    their truthful meaning with respect to the stream the caller exposes:
    the flag reports that the produced (sanitized) output exceeded the
    retention bound, not a claim about raw pre-sanitization bytes.
    """

    def __init__(
        self,
        max_chars: int = _MAX_OUTPUT_CHARS,
        sanitizer: Optional[Any] = None,
    ) -> None:
        self._max = max_chars
        self._half = max_chars // 2
        self._sanitizer = sanitizer
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer: List[str] = []
        self._buffered = 0
        self._overflowed = False
        self._head: List[str] = []
        self._head_len = 0
        self._tail: deque = deque()
        self._tail_len = 0
        self._sealed = False

    def decode_and_add(self, data: bytes) -> None:
        if self._sealed or not data:
            return
        text = self._decoder.decode(data)
        if not text:
            return
        if self._sanitizer is not None:
            text = self._sanitizer.feed(text)
            if not text:
                return
        self._add_text(text)

    def _add_text(self, text: str) -> None:
        n = len(text)
        if not self._overflowed:
            self._buffer.append(text)
            self._buffered += n
            if self._buffered > self._max:
                self._overflow()
        else:
            self._tail_append_and_trim(text)

    def _overflow(self) -> None:
        full = "".join(self._buffer)
        self._buffer = None
        self._head.append(full[:self._half])
        self._head_len = self._half
        rest = full[self._half:]
        if rest:
            if len(rest) > self._half:
                rest = rest[-self._half:]
            self._tail.append(rest)
            self._tail_len = len(rest)
        self._overflowed = True

    def _tail_append_and_trim(self, text: str) -> None:
        self._tail.append(text)
        self._tail_len += len(text)
        while self._tail_len > self._half:
            oldest = self._tail[0]
            excess = self._tail_len - self._half
            if len(oldest) <= excess:
                self._tail.popleft()
                self._tail_len -= len(oldest)
            else:
                self._tail[0] = oldest[excess:]
                self._tail_len -= excess

    def finalize(self) -> Tuple[str, bool]:
        residual = self._decoder.decode(b"", final=True)
        if residual:
            self._add_text(residual)
        if self._sanitizer is not None:
            flushed = self._sanitizer.flush()
            if flushed:
                self._add_text(flushed)

        self._sealed = True
        self._decoder = None

        if not self._overflowed:
            return "".join(self._buffer), False

        head = "".join(self._head)
        tail = "".join(self._tail)
        if len(tail) > self._half:
            tail = tail[-self._half:]
        return head + _TRUNCATION_MARKER + tail, True


class CommandRunner:
    """Safe command runner that executes argv-based commands inside a TaskWorkspace.

    Uses ``shell=False``, enforces timeouts, bounds output, and cleans up
    processes including descendant processes where the platform permits.
    Children never inherit the parent's stdin (``DEVNULL``): a session
    worker's protocol stdin must not be shared with a subprocess, and
    non-interactive commands must observe EOF on stdin.

    Execution authorities (exactly one per runner, never merged):

    * ``execution_context`` — the existing specialized verified execution
      authority (:class:`VerifiedExecutionContext`); behavior unchanged.
    * ``environment`` — the V2 product authority: an explicit child
      environment mapping derived and classified by the session's
      execution-environment authority
      (:class:`agentic_debugger.application.execution_environment.ExecutionEnvironment`).
      Product Local Project call sites always supply it; the runner does
      not decide the product environment by reading ``os.environ``.
    * neither — narrow non-product compatibility (worker boundary harness,
      generic tests): the historical full parent-environment inheritance.
      This fallback is not the product path.

    Optional neutral output-sanitization seam (``output_sanitizer_factory``):
    a zero-arg factory invoked once per output stream to build an object
    with ``feed(str) -> str`` / ``flush() -> str`` that each decoded chunk
    passes through BEFORE bounding.  It is plain duck typing — this module
    never imports the application layer, the factory stays ``None`` on the
    generic/scientific paths (byte-identical historical behavior), and the
    seam is refused together with a ``VerifiedExecutionContext`` (verified
    execution has no product secret authority to sanitize with).  The
    product boundary supplies it so raw secret values are redacted from
    the complete stream text before the head/tail bounding can cut a
    fragment out of them.  Sanitizers must be deterministic and must not
    raise on text input.
    """

    def __init__(
        self,
        workspace: TaskWorkspace,
        execution_context: Optional[VerifiedExecutionContext] = None,
        *,
        environment: Optional[Mapping[str, str]] = None,
        output_sanitizer_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        if execution_context is not None and environment is not None:
            raise CommandRequestError(
                "conflicting execution authorities: a VerifiedExecutionContext "
                "and an explicit product environment must not be supplied "
                "together; they are never merged"
            )
        if output_sanitizer_factory is not None:
            if execution_context is not None:
                raise CommandRequestError(
                    "conflicting execution authorities: an output "
                    "sanitization seam and a VerifiedExecutionContext must "
                    "not be supplied together; verified execution carries "
                    "no product secret authority"
                )
            if not callable(output_sanitizer_factory):
                raise CommandRequestError(
                    "output_sanitizer_factory must be callable or None"
                )
        if environment is not None:
            if not isinstance(environment, Mapping):
                raise CommandRequestError("environment must be a string mapping or None")
            for name, value in environment.items():
                if not isinstance(name, str) or not name or not isinstance(value, str):
                    raise CommandRequestError(
                        "environment must map non-empty strings to strings"
                    )
        self._workspace = workspace
        self._execution_context = execution_context
        self._environment = dict(environment) if environment is not None else None
        self._output_sanitizer_factory = output_sanitizer_factory

    def run(
        self,
        argv: List[str],
        cwd: str,
        timeout_seconds: float,
        *,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> CommandResult:
        """Execute ``argv`` bounded by ``timeout_seconds``.

        ``cancel_check`` is an optional cooperative cancellation checkpoint:
        when supplied it is polled while the child runs and must raise
        :class:`CancellationError` to request cancellation.  Cancellation
        terminates the child/process tree and re-raises the cancellation
        signal; it never returns an ordinary failed result.  When
        ``cancel_check`` is ``None`` behavior is unchanged (a single bounded
        ``wait``).
        """
        _validate_argv(argv)
        _validate_timeout(timeout_seconds)
        if cancel_check is not None and not callable(cancel_check):
            raise CommandRequestError("cancel_check must be callable or None")

        resolved_cwd = self._workspace.resolve_path(cwd, must_exist=True)
        if self._execution_context is not None:
            self._execution_context.bind_cwd(cwd)
            bound_argv = self._execution_context.bind_argv(argv)
            bound_env = self._execution_context.build_environment(self._workspace.root)
            result = self._execution_context.runner.run(bound_argv, resolved_cwd, timeout_seconds, bound_env)
            if not isinstance(result, CommandResult):
                raise CommandExecutionError("containment runner returned an invalid command result")
            return result

        start = time.monotonic()
        timed_out = False
        exit_code: Optional[int] = None

        sanitizer_factory = self._output_sanitizer_factory
        stdout_accum = _BoundedStreamAccumulator(
            sanitizer=(
                sanitizer_factory() if sanitizer_factory is not None else None
            )
        )
        stderr_accum = _BoundedStreamAccumulator(
            sanitizer=(
                sanitizer_factory() if sanitizer_factory is not None else None
            )
        )
        stdout_lock = threading.Lock()
        stderr_lock = threading.Lock()

        run_env = _product_environment(self._environment)

        try:
            proc = subprocess.Popen(
                argv,
                cwd=resolved_cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=run_env,
                start_new_session=sys.platform != "win32",
                creationflags=_get_creationflags(),
            )
        except Exception as e:
            raise CommandExecutionError(
                f"Failed to launch command: {e}"
            ) from e

        reader_threads = _start_reader_threads(
            proc, stdout_accum, stderr_accum, stdout_lock, stderr_lock
        )

        try:
            if cancel_check is None:
                proc.wait(timeout=timeout_seconds)
            else:
                _wait_polling(proc, timeout_seconds, cancel_check)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(proc)
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        except CancellationError:
            _kill_process_tree(proc)
            raise
        finally:
            deadline = time.monotonic() + _THREAD_JOIN_TIMEOUT
            for t in reader_threads:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    t.join(timeout=remaining)

        elapsed_ms = _elapsed_ms(start)

        if not timed_out:
            exit_code = proc.returncode

        with stdout_lock:
            decoded_stdout, stdout_truncated = stdout_accum.finalize()
        with stderr_lock:
            decoded_stderr, stderr_truncated = stderr_accum.finalize()

        return CommandResult(
            argv=list(argv),
            cwd=cwd,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=elapsed_ms,
            stdout=decoded_stdout,
            stderr=decoded_stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


def _validate_argv(argv: List[str]) -> None:
    if not isinstance(argv, list):
        raise CommandRequestError("argv must be a list of strings")
    if len(argv) == 0:
        raise CommandRequestError("argv must be non-empty")
    for i, arg in enumerate(argv):
        if not isinstance(arg, str):
            raise CommandRequestError(f"argv[{i}] must be a string")
        if arg == "":
            raise CommandRequestError(f"argv[{i}] must be non-empty")
        if "\0" in arg:
            raise CommandRequestError(f"argv[{i}] contains NUL byte")


def _validate_timeout(timeout_seconds: float) -> None:
    if not isinstance(timeout_seconds, (int, float)) or isinstance(
        timeout_seconds, bool
    ):
        raise CommandRequestError("timeout_seconds must be a number")
    if timeout_seconds <= 0:
        raise CommandRequestError("timeout_seconds must be positive")
    if not math.isfinite(timeout_seconds):
        raise CommandRequestError("timeout_seconds must be finite")


def _product_environment(environment: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Child environment for product/plain execution (non-verified mode).

    V2-01: an explicit environment supplied by the session's
    execution-environment authority is the product authority and is copied
    here untouched.  ``None`` keeps the narrow non-product compatibility
    fallback (full parent-environment inheritance) for the worker boundary
    harness and generic test/harness callers; real Local Project call
    sites always pass the V2-derived role environment.  The runner's own
    IO-encoding contract (``PYTHONIOENCODING``) applies on both paths.
    """
    if environment is not None:
        env = dict(environment)
    else:
        env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _get_creationflags() -> int:
    if sys.platform == "win32":
        return subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def _start_reader_threads(
    proc: subprocess.Popen,
    stdout_accum: _BoundedStreamAccumulator,
    stderr_accum: _BoundedStreamAccumulator,
    stdout_lock: threading.Lock,
    stderr_lock: threading.Lock,
) -> List[threading.Thread]:
    threads: List[threading.Thread] = []

    def _read_stream(
        stream_handle: Any,
        accum: _BoundedStreamAccumulator,
        lock: threading.Lock,
    ) -> None:
        try:
            while True:
                chunk = stream_handle.read(4096)
                if not chunk:
                    break
                with lock:
                    accum.decode_and_add(chunk)
        except Exception:
            pass

    for stream_handle, accum, lock in [
        (proc.stdout, stdout_accum, stdout_lock),
        (proc.stderr, stderr_accum, stderr_lock),
    ]:
        t = threading.Thread(
            target=_read_stream,
            args=(stream_handle, accum, lock),
            daemon=True,
        )
        t.start()
        threads.append(t)

    return threads


def _kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            _wait_pid(proc, timeout=1.0)
            proc.terminate()
            _wait_pid(proc, timeout=1.0)
            proc.kill()
            _wait_pid(proc, timeout=1.0)
        else:
            try:
                child_pgid = os.getpgid(proc.pid)
                own_pgid = os.getpgid(os.getpid())
                if child_pgid == own_pgid:
                    proc.terminate()
                else:
                    os.killpg(child_pgid, signal.SIGTERM)
                    _wait_pid(proc, timeout=2.0)
                    os.killpg(child_pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                proc.terminate()
            _wait_pid(proc, timeout=1.0)
            proc.kill()
            _wait_pid(proc, timeout=1.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _wait_pid(proc: subprocess.Popen, timeout: float) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass


def _wait_polling(
    proc: subprocess.Popen,
    timeout_seconds: float,
    cancel_check: Callable[[], None],
) -> None:
    """Poll ``cancel_check`` while waiting for ``proc`` to exit.

    The manifest timeout is still honored exactly: once it expires a
    ``TimeoutExpired`` is raised so the caller runs the normal timeout
    termination path.  Cancellation (``CancellationError`` from the check)
    propagates to the caller, which terminates the process tree and
    re-raises.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        cancel_check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout_seconds)
        try:
            proc.wait(timeout=min(_POLL_INTERVAL_SECONDS, remaining))
            return
        except subprocess.TimeoutExpired:
            continue


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))
