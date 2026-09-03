"""Parent-side supervisor of one session worker process.

Owns the worker process lifecycle: spawn (with Windows job-object process
tree assignment), startup handshake, cancellation request, bounded
cooperative grace, final process-tree escalation, process exit observation,
protocol consumption, crash/interruption classification, and post-mortem
cleanup where the worker could not execute cleanup.

The supervisor creates only the durable session directory (the journal/
evidence container).  The disposable execution work directory is
worker-owned: the worker creates it only when execution actually begins and
removes it in its cleanup cycle, so no startup or pre-start path can leak it.
Every failed ``start()`` leaves the supervisor in a deterministic failed-start
state: ``wait()`` fails immediately, ``close()`` stays safe, and a second
``start()`` is rejected (one-shot supervisor semantics).

The worker is the authoritative writer of the session journal; the
supervisor never writes it.  The notification pipe is fail-open; the
journal is the evidence authority and supports classification after an
abrupt worker death.

Windows process-tree guarantee: the worker is spawned suspended, assigned to
a job object with ``KILL_ON_JOB_CLOSE``, and resumed; every descendant the
worker creates inherits the job, so forced escalation terminates the whole
tree (and supervisor death closes the job and kills remaining members).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from agentic_debugger.application import ApplicationError, ApplicationInputError
from agentic_debugger.application.events import SessionEvent
from agentic_debugger.application.journal import (
    JournalReadState,
    read_session_journal,
)
from agentic_debugger.application.process_tree import (
    WindowsProcessTreeJob,
    spawn_suspended_on_windows,
    terminate_process_tree,
)
from agentic_debugger.application.session import (
    MAX_DIAGNOSTIC_CHARS,
    SessionId,
    SessionResult,
    SessionSpec,
    SessionStatus,
    SessionTerminationReason,
)
from agentic_debugger.application.worker_protocol import (
    MAX_WORKER_LINE_BYTES,
    WorkerNotification,
    WorkerLiveness,
    cancel_message,
    parse_worker_message,
    start_message,
)


_POLL_INTERVAL_SECONDS = 0.05
_READER_JOIN_TIMEOUT = 3.0


class WorkerStartupError(ApplicationError):
    """Raised when the worker process itself cannot be spawned."""


class WorkerLifecycleError(ApplicationError):
    """Raised when the supervisor is used outside its valid lifecycle."""



def _bounded_diagnostic(text: str) -> str:
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        return "unrepresentable diagnostic"
    if len(encoded) <= MAX_DIAGNOSTIC_CHARS:
        return text
    return encoded[: MAX_DIAGNOSTIC_CHARS - 3].decode("utf-8", "replace") + "..."


def _sanitize_diagnostic(text: str) -> str:
    """Replace control characters (SessionResult diagnostics forbid them)."""
    cleaned = "".join(
        char if ord(char) >= 0x20 and ord(char) != 0x7F else " " for char in text
    )
    return _bounded_diagnostic(cleaned)


def _compute_project_root() -> str:
    import agentic_debugger

    pkg_dir = os.path.dirname(os.path.abspath(agentic_debugger.__file__))
    return os.path.dirname(pkg_dir)


class SessionWorkerProcess:
    """Supervisor of one cancellable session worker process."""

    def __init__(
        self,
        *,
        session_dir: str | os.PathLike[str],
        session_id: str,
        spec: SessionSpec,
        run_id: str,
        scenario: str,
        scenario_params: Optional[Mapping[str, Any]] = None,
        cooperative_grace_seconds: float = 30.0,
        ready_timeout_seconds: float = 30.0,
        max_elapsed_seconds: Optional[int] = None,
        pre_start_delay_seconds: float = 0.0,
        retry_of_session_id: Optional[str] = None,
        job_factory: Optional[Callable[[], WindowsProcessTreeJob]] = None,
        child_environment: Optional[Mapping[str, str]] = None,
    ) -> None:
        from agentic_debugger.application.events import validate_session_id

        try:
            validate_session_id(session_id)
        except Exception as exc:
            raise ApplicationInputError(f"invalid session id: {session_id!r}") from exc
        self._session_id = session_id
        if type(spec) is not SessionSpec:
            raise ApplicationInputError("spec must be a SessionSpec")
        self._spec = spec
        # Bounded, in-memory child-environment overrides (e.g. one
        # provider credential variable for direct-API routes).  Values
        # travel only into the worker process environment — never into
        # argv, the start message, scenario params, or the journal.
        self._child_environment: Optional[Dict[str, str]] = None
        if child_environment is not None:
            if not isinstance(child_environment, Mapping):
                raise ApplicationInputError(
                    "child_environment must be a mapping of strings or None"
                )
            for name, value in child_environment.items():
                if type(name) is not str or not name or type(value) is not str:
                    raise ApplicationInputError(
                        "child_environment overrides must be string pairs"
                    )
            self._child_environment = dict(child_environment)
        if type(run_id) is not str or not run_id:
            raise ApplicationInputError("run_id must be a non-empty string")
        self._run_id = run_id
        if type(scenario) is not str or not scenario:
            raise ApplicationInputError("scenario must be a non-empty string")
        self._scenario = scenario
        self._scenario_params: Dict[str, Any] = dict(scenario_params or {})
        for value in (cooperative_grace_seconds, ready_timeout_seconds):
            if type(value) not in (int, float) or isinstance(value, bool) or value <= 0:
                raise ApplicationInputError("timeouts must be positive numbers")
        self._cooperative_grace_seconds = float(cooperative_grace_seconds)
        self._ready_timeout_seconds = float(ready_timeout_seconds)
        if max_elapsed_seconds is not None and (
            type(max_elapsed_seconds) is not int or max_elapsed_seconds < 1
        ):
            raise ApplicationInputError("max_elapsed_seconds must be a positive int or None")
        self._max_elapsed_seconds = max_elapsed_seconds
        if type(pre_start_delay_seconds) not in (int, float) or isinstance(
            pre_start_delay_seconds, bool
        ):
            raise ApplicationInputError("pre_start_delay_seconds must be a number")
        self._pre_start_delay_seconds = float(pre_start_delay_seconds)
        if retry_of_session_id is not None:
            if type(retry_of_session_id) is not str or not retry_of_session_id:
                raise ApplicationInputError("retry_of_session_id must be a non-empty string or None")
            if len(retry_of_session_id.encode("utf-8")) > 128:
                raise ApplicationInputError("retry_of_session_id exceeds the bound")
        self._retry_of_session_id = retry_of_session_id
        if job_factory is not None and not callable(job_factory):
            raise ApplicationInputError("job_factory must be callable or None")
        self._job_factory: Callable[[], WindowsProcessTreeJob] = (
            job_factory if job_factory is not None else WindowsProcessTreeJob
        )

        session_dir_path = Path(session_dir).resolve()
        try:
            session_dir_path.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise ApplicationInputError(
                f"session directory already exists: {session_dir_path}"
            ) from exc
        except OSError as exc:
            raise ApplicationInputError(
                f"cannot create session directory {session_dir_path}: {exc}"
            ) from exc
        self._session_dir = session_dir_path
        self._journal_path = session_dir_path / "session.events.jsonl"
        # The disposable execution work directory is worker-owned and is
        # created by the worker only when execution actually begins (after
        # ``session.started``); before that, no execution-owned disposable
        # resource exists, so no startup or pre-start path can leak one.
        self._work_dir = session_dir_path / "work"

        self._proc: Optional[subprocess.Popen] = None
        self._job: Optional[WindowsProcessTreeJob] = None
        self._job_assigned = False
        self._reader_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._ready_event = threading.Event()
        self._terminal_event = threading.Event()
        self._terminal: Optional[WorkerNotification] = None
        self._fatal: Optional[WorkerNotification] = None
        self._error: Optional[WorkerNotification] = None
        self._events: List[SessionEvent] = []
        self._protocol_diagnostics: List[str] = []
        self._cancel_requested = False
        self._cancel_sent = False
        self._started = False
        self._closed = False
        self._result: Optional[SessionResult] = None
        self._startup_error: Optional[WorkerStartupError] = None
        self._journal_offset = 0
        self._liveness: Optional[WorkerLiveness] = None

    # -- properties ---------------------------------------------------------

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    @property
    def journal_path(self) -> Path:
        return self._journal_path

    @property
    def work_dir(self) -> Path:
        return self._work_dir

    @property
    def events(self) -> Tuple[SessionEvent, ...]:
        """Events surfaced via journal catch-up (the pipe carries only
        sequence notifications; the durable journal is authoritative)."""
        with self._lock:
            return tuple(self._events)

    @property
    def liveness(self) -> Optional[WorkerLiveness]:
        """Latest safe side-band snapshot; it is never journaled."""
        with self._lock:
            return self._liveness

    @property
    def job_assigned(self) -> bool:
        return self._job_assigned

    def _worker_argv(self) -> List[str]:
        project_root = _compute_project_root().replace("\\", "/")
        bootstrap = (
            "import sys; import runpy; "
            "sys.path.insert(0, " + repr(project_root) + "); "
            "runpy.run_module("
            "'agentic_debugger.application.worker', run_name='__main__')"
        )
        # Same central Windows-venv launch authority as the PDB worker:
        # inside a Windows virtual environment launch the real base
        # interpreter directly so the Popen PID is the worker itself
        # (the suspended-spawn JOB assignment and resume below must
        # target the actual worker, not the venv redirector).  The venv
        # identity travels via ``__PYVENV_LAUNCHER__`` in the spawn
        # environment (see ``start``).
        from agentic_debugger.runtime.python_launcher import (
            resolve_worker_executable,
        )

        return [resolve_worker_executable(), "-I", "-u", "-c", bootstrap]

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> Optional[SessionResult]:
        """Spawn the worker and complete the startup handshake.

        Returns ``None`` when the worker reached ``ready``; returns a
        terminal :class:`SessionResult` (FAILED) when startup failed.
        Raises :class:`WorkerStartupError` when the worker cannot be
        spawned or contained (Windows job assignment/resume failure) or the
        start message cannot be delivered; every such failure records the
        failed start as the lifecycle authority and unwinds the process and
        its resources so no half-started worker survives and ``wait()``
        fails immediately.

        On Windows the containment steps are mandatory and fail closed: the
        suspended worker is only allowed to execute after the job object was
        created, the worker was assigned to it, and the worker was resumed.
        """
        if self._closed:
            raise WorkerLifecycleError("cannot start a closed worker supervisor")
        if self._started:
            raise ApplicationInputError("worker already started")
        self._started = True
        creationflags = spawn_suspended_on_windows()
        try:
            from agentic_debugger.runtime.python_launcher import build_worker_env

            merged_environment = (
                {**os.environ, **self._child_environment}
                if self._child_environment
                else None
            )
            # Central Windows-venv launch authority: inside a Windows
            # virtual environment the worker is the directly launched
            # base interpreter and this environment carries the standard
            # ``__PYVENV_LAUNCHER__`` identity so it keeps the venv
            # prefix/packages.  Outside a venv a stale launcher variable
            # is scrubbed so the child keeps its own prefix.
            spawn_environment = build_worker_env(merged_environment)
            self._proc = subprocess.Popen(
                self._worker_argv(),
                # The worker is spawned with the durable session directory as
                # its bootstrap cwd; the disposable work directory does not
                # exist yet and is created by the worker only at execution
                # start (Windows also refuses to remove a running process's
                # cwd, and the session directory is never removed).
                cwd=str(self._session_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=spawn_environment,
                start_new_session=sys.platform != "win32",
                creationflags=creationflags,
            )
        except Exception as exc:
            # A raw spawn failure leaves no process: record the failed start
            # as the lifecycle authority so wait() fails immediately instead
            # of waiting indefinitely on a worker that does not exist.
            error = WorkerStartupError(f"failed to spawn the session worker: {exc}")
            self._startup_error = error
            raise error from exc

        if sys.platform == "win32" and creationflags:
            self._contain_worker()

        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(
                start_message(
                    session_id=self._session_id,
                    spec=self._spec,
                    run_id=self._run_id,
                    work_dir=str(self._work_dir),
                    journal_path=str(self._journal_path),
                    scenario=self._scenario,
                    scenario_params=self._scenario_params,
                    max_elapsed_seconds=self._max_elapsed_seconds,
                    pre_start_delay_seconds=self._pre_start_delay_seconds,
                    retry_of_session_id=self._retry_of_session_id,
                )
            )
            self._proc.stdin.flush()
        except Exception as exc:
            error = WorkerStartupError(f"failed to send the start message: {exc}")
            self._unwind_after_spawn()
            self._startup_error = error
            raise error from exc

        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="worker-supervisor-reader", daemon=True
        )
        self._reader_thread.start()

        deadline = time.monotonic() + self._ready_timeout_seconds
        while True:
            if self._ready_event.is_set():
                if self._cancel_requested:
                    self._send_cancel()
                return None
            if self._error is not None:
                result = self._startup_failure_result(self._error)
                self._reap_or_terminate()
                self._result = result
                return result
            if self._fatal is not None:
                result = self._fatal_result(self._fatal)
                self._reap_or_terminate()
                self._result = result
                return result
            if self._proc.poll() is not None:
                return self._classify_drained_exit()
            if time.monotonic() >= deadline:
                self._escalate()
                return self._classify_drained_exit()
            time.sleep(_POLL_INTERVAL_SECONDS)

    def _contain_worker(self) -> None:
        """Establish the Windows job-object containment boundary; fail closed.

        On Windows every containment step is mandatory before the suspended
        worker may execute: job creation, worker assignment, worker resume.
        Any failure terminates and reaps the suspended worker, closes the
        job handle and the process pipes, and raises
        :class:`WorkerStartupError`; the supervisor then never proceeds with
        an uncontained live worker.
        """
        job: Optional[WindowsProcessTreeJob] = None
        try:
            job = self._job_factory()
            if not job.assign(self._proc.pid):
                raise WorkerStartupError(
                    "worker could not be assigned to its job object"
                )
            if not job.resume(self._proc.pid):
                raise WorkerStartupError("worker could not be resumed")
        except WorkerStartupError:
            self._job = job
            self._unwind_after_spawn()
            self._startup_error = WorkerStartupError(
                "Windows job containment failed; the worker was terminated"
            )
            raise
        except Exception as exc:
            self._job = job
            self._unwind_after_spawn()
            self._startup_error = WorkerStartupError(
                f"Windows job containment failed: {exc}"
            )
            raise self._startup_error from exc
        self._job = job
        self._job_assigned = True

    def _unwind_after_spawn(self) -> None:
        """Terminate, reap, and release everything created by this startup.

        Kills the spawned worker (via the job when assigned, otherwise the
        group ladder — the worker may still be suspended, which the ladder's
        terminate/kill steps handle), closes its pipes, and releases the job
        handle.  Never raises.
        """
        if self._proc is not None:
            try:
                terminate_process_tree(self._proc, self._job)
            except Exception:
                pass
            try:
                self._proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
            for name in ("stdin", "stdout", "stderr"):
                stream = getattr(self._proc, name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
        if self._job is not None:
            try:
                self._job.close()
            except Exception:
                pass
            self._job = None
        self._job_assigned = False

    def cancel(self) -> None:
        """Request cooperative cancellation; idempotent."""
        with self._lock:
            if self._cancel_requested:
                return
            self._cancel_requested = True
        self._send_cancel()

    def _send_cancel(self) -> None:
        with self._lock:
            if self._cancel_sent or self._proc is None or self._proc.stdin is None:
                return
            if self._proc.poll() is not None:
                self._cancel_sent = True
                return
            try:
                self._proc.stdin.write(cancel_message())
                self._proc.stdin.flush()
                self._cancel_sent = True
            except Exception:
                pass

    def wait(self) -> SessionResult:
        """Wait for the operational terminal result.

        Cooperative cancellation is honored for one bounded grace period;
        afterwards (or when the elapsed budget is exceeded) the worker tree
        is force-terminated and the outcome is classified honestly from the
        durable journal.  The supervisor fails closed on invalid lifecycle
        usage: waiting before a successful :meth:`start`, after
        :meth:`close`, or after a raised startup failure raises immediately
        instead of waiting indefinitely.
        """
        if self._closed:
            raise WorkerLifecycleError("cannot wait() after close()")
        if self._startup_error is not None:
            raise self._startup_error
        if not self._started:
            raise WorkerLifecycleError("wait() requires a successful start()")
        if self._result is not None:
            self._catch_up_journal()
            return self._result
        cancel_deadline: Optional[float] = None
        hard_deadline: Optional[float] = None
        if self._cancel_requested:
            cancel_deadline = time.monotonic() + self._cooperative_grace_seconds
        if self._max_elapsed_seconds is not None:
            hard_deadline = (
                time.monotonic()
                + self._max_elapsed_seconds
                + self._cooperative_grace_seconds
            )
        while True:
            with self._lock:
                if self._terminal is not None:
                    self._result = self._terminal.result
                    break
                if self._fatal is not None:
                    self._result = self._fatal_result(self._fatal)
                    break
                if self._error is not None and not self._ready_event.is_set():
                    self._result = self._startup_failure_result(self._error)
                    break
            if self._proc is not None and self._proc.poll() is not None:
                self._result = self._classify_drained_exit()
                break
            now = time.monotonic()
            if cancel_deadline is not None and now >= cancel_deadline:
                self._escalate()
                self._result = self._classify_drained_exit()
                break
            if hard_deadline is not None and now >= hard_deadline:
                self._escalate()
                self._result = self._classify_drained_exit()
                break
            time.sleep(_POLL_INTERVAL_SECONDS)
        self._reap()
        self._catch_up_journal()
        return self._result

    def _catch_up_journal(self) -> None:
        """Read journal records not yet surfaced and append them to events.

        The durable journal is the event authority; pipe notifications carry
        only sequences.  A record without a terminating newline is still
        being written and is re-read on the next catch-up.  Malformed
        records stop the catch-up with a diagnostic (the journal reader
        classifies the file honestly; a corrupt journal is never success).
        """
        if not os.path.isfile(self._journal_path):
            return
        import json

        try:
            stream = open(self._journal_path, "r", encoding="utf-8", newline="\n")
        except OSError as exc:
            self._protocol_diagnostics.append(
                _bounded_diagnostic(f"journal catch-up open failed: {exc}")
            )
            return
        with stream:
            try:
                stream.seek(self._journal_offset)
                while True:
                    position = stream.tell()
                    line = stream.readline()
                    if not line or not line.endswith("\n"):
                        # EOF or a partial final record: keep the offset at
                        # the start of the incomplete record.
                        self._journal_offset = position
                        return
                    try:
                        event = SessionEvent.from_mapping(json.loads(line))
                    except Exception as exc:
                        self._protocol_diagnostics.append(
                            _bounded_diagnostic(
                                f"invalid journal record during catch-up: {exc}"
                            )
                        )
                        return
                    if event.task_id != self._spec.task_id:
                        self._protocol_diagnostics.append(
                            "journal record task_id does not match the session spec"
                        )
                        return
                    with self._lock:
                        self._events.append(event)
                    self._journal_offset = stream.tell()
            except OSError as exc:
                self._protocol_diagnostics.append(
                    _bounded_diagnostic(f"journal catch-up read failed: {exc}")
                )

    def close(self) -> None:
        """Release the worker handles; remaining job members are killed on
        close (the worker is bound to the supervisor lifetime)."""
        if self._closed:
            return
        self._closed = True
        if self._proc is not None:
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            except Exception:
                pass
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=_READER_JOIN_TIMEOUT)
            try:
                if self._proc.stdout is not None:
                    self._proc.stdout.close()
            except Exception:
                pass
            try:
                if self._proc.stderr is not None:
                    self._proc.stderr.close()
            except Exception:
                pass
        if self._job is not None:
            self._job.close()
            self._job = None

    # -- protocol consumption ------------------------------------------------

    def _reader_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        while True:
            line = self._proc.stdout.readline(MAX_WORKER_LINE_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_WORKER_LINE_BYTES:
                self._protocol_diagnostics.append("oversized worker message ignored")
                continue
            try:
                notification = parse_worker_message(line.decode("utf-8"), self._spec)
            except Exception as exc:
                self._protocol_diagnostics.append(
                    _bounded_diagnostic(f"invalid worker message ignored: {exc}")
                )
                continue
            if notification.kind == "ready":
                self._ready_event.set()
            elif notification.kind == "event":
                # The notification carries only the sequence; the durable
                # journal is the event authority and the parent catches up.
                self._catch_up_journal()
            elif notification.kind == "liveness":
                with self._lock:
                    self._liveness = notification.liveness
            elif notification.kind == "terminal":
                with self._lock:
                    self._terminal = notification
                self._terminal_event.set()
            elif notification.kind == "fatal":
                with self._lock:
                    self._fatal = notification
            elif notification.kind == "error":
                with self._lock:
                    self._error = notification

    # -- escalation and classification --------------------------------------

    def _escalate(self) -> None:
        if self._proc is None:
            return
        terminate_process_tree(self._proc, self._job)
        try:
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass

    def _reap_or_terminate(self) -> None:
        """Bounded reap of the worker; escalate if it will not exit.

        Startup-failure return paths must not leave a live worker behind a
        supposedly terminal startup result, and must not require the caller
        to remember an extra cleanup action.
        """
        if self._proc is None:
            return
        try:
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            terminate_process_tree(self._proc, self._job)
            try:
                self._proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass

    def _reap(self) -> None:
        """Best-effort bounded wait so the worker is fully reaped."""
        if self._proc is not None:
            try:
                self._proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass

    def _stderr_tail(self) -> List[str]:
        tail: List[str] = []
        if self._proc is None or self._proc.stderr is None:
            return tail
        try:
            data = self._proc.stderr.read()
            text = data.decode("utf-8", errors="replace")
            if text:
                tail.append(_sanitize_diagnostic(f"worker stderr: {text[:2000]}"))
        except Exception:
            pass
        return tail

    def _post_mortem_cleanup(self) -> List[str]:
        diagnostics: List[str] = []
        try:
            if self._work_dir.exists():
                shutil.rmtree(self._work_dir)
            if self._work_dir.exists():
                diagnostics.append("work directory remains after post-mortem cleanup")
            else:
                diagnostics.append("post-mortem work directory removal: ok")
        except BaseException as exc:  # noqa: BLE001 - cleanup must not raise
            diagnostics.append(
                _bounded_diagnostic(f"post-mortem cleanup failed: {exc}")
            )
        diagnostics.extend(self._post_mortem_local_project_cleanup())
        return diagnostics

    def _post_mortem_local_project_cleanup(self) -> List[str]:
        """Remove a Local Project isolated worktree the worker could not.

        The worker's own cleanup cycle owns the normal path; this runs only
        post-mortem (worker confirmed dead without a terminal), so it never
        races a live worker.  Without it, a crashed/killed Local Project
        worker would leak the temporary worktree AND leave a stale ``git
        worktree`` registration inside the owner repository's metadata.
        Verification uses the accepted ``cleanup_parent_tmpdir`` contract
        (filesystem gone + registration pruned).
        """
        from agentic_debugger.application.local_project import cleanup_parent_tmpdir
        from agentic_debugger.application.local_project_source import (
            LOCAL_PROJECT_SOURCE_NAME,
        )

        if self._scenario != LOCAL_PROJECT_SOURCE_NAME:
            return []
        parent = self._scenario_params.get("parent_tmpdir")
        repo = self._scenario_params.get("project_repo_path")
        if not parent or not repo:
            return []

        # The worker must be confirmed dead before supervisor-side removal.
        self._reap_or_terminate()
        try:
            verified = cleanup_parent_tmpdir(Path(parent), Path(repo))
        except Exception as exc:
            return [
                _bounded_diagnostic(
                    f"post-mortem isolated worktree cleanup failed: {exc}"
                )
            ]
        return [
            "post-mortem isolated worktree cleanup: "
            + ("verified" if verified else "NOT verified")
        ]

    def _startup_failure_result(self, notification: WorkerNotification) -> SessionResult:
        reason = SessionTerminationReason.CONTROLLER_FAILED
        if notification.error_kind == "journal_creation_failed":
            reason = SessionTerminationReason.JOURNAL_ERROR
        diagnostics = [
            _bounded_diagnostic(f"worker startup failed: {notification.error_kind}"),
            *list(notification.diagnostics),
            *self._protocol_diagnostics,
            *self._post_mortem_local_project_cleanup(),
        ]
        return SessionResult(
            session_id=SessionId(self._session_id),
            spec=self._spec,
            status=SessionStatus.FAILED,
            termination_reason=reason,
            run_id=None,
            started_at_utc=None,
            ended_at_utc=None,
            sequence=0,
            cleanup_verified=False,
            diagnostics=tuple(diagnostics[:64]),
        )

    def _fatal_result(self, notification: WorkerNotification) -> SessionResult:
        read = read_session_journal(self._journal_path)
        run_id: Optional[str] = None
        for event in read.events:
            if event.event_kind.value == "session.started":
                run_id = event.run_id
                break
        diagnostics = [
            _bounded_diagnostic(
                f"journal failure (out of band): {notification.error_kind}"
            ),
            *list(notification.diagnostics),
            *self._post_mortem_local_project_cleanup(),
        ]
        return SessionResult(
            session_id=SessionId(self._session_id),
            spec=self._spec,
            status=SessionStatus.FAILED,
            termination_reason=SessionTerminationReason.JOURNAL_ERROR,
            run_id=run_id,
            started_at_utc=None,
            ended_at_utc=None,
            sequence=read.events[-1].sequence if read.events else 0,
            cleanup_verified=False,
            diagnostics=tuple(diagnostics[:64]),
        )

    def _drain_reader(self) -> None:
        """Give the reader a bounded chance to drain the protocol pipe.

        The worker's whole lifetime can be shorter than one supervisor poll
        interval, so by the time an exit is observed the pipe may still hold
        an in-flight ``terminal``/``fatal``/``error`` envelope.  Once the
        worker has exited the pipe reaches EOF quickly, so the join returns
        promptly; the timeout only guards a pathological stall.
        """
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=_READER_JOIN_TIMEOUT)

    def _classify_drained_exit(self) -> SessionResult:
        """Classify an exited worker without swallowing in-flight envelopes.

        An exit classification must never lose the out-of-band ``fatal``
        (journal failure) or pre-ready ``error`` envelope to a race with the
        reader thread; the delivered envelope wins, otherwise the honest
        exit classification stands.
        """
        self._drain_reader()
        with self._lock:
            if self._terminal is not None:
                return self._terminal.result
            if self._fatal is not None:
                return self._fatal_result(self._fatal)
            if self._error is not None:
                return self._startup_failure_result(self._error)
        return self._classify_after_exit()

    def _classify_after_exit(self) -> SessionResult:
        read = read_session_journal(self._journal_path)
        last_sequence = read.events[-1].sequence if read.events else 0
        if read.state is JournalReadState.COMPLETE:
            try:
                return self._result_from_journal_events(read.events)
            except Exception as exc:
                diagnostics = [
                    _bounded_diagnostic(
                        f"journal terminal present but result derivation failed: {exc}"
                    ),
                    *self._protocol_diagnostics,
                    *self._stderr_tail(),
                ]
                return SessionResult(
                    session_id=SessionId(self._session_id),
                    spec=self._spec,
                    status=SessionStatus.INTERRUPTED,
                    termination_reason=SessionTerminationReason.INTERRUPTED,
                    run_id=None,
                    started_at_utc=None,
                    ended_at_utc=None,
                    sequence=last_sequence,
                    cleanup_verified=False,
                    diagnostics=tuple(diagnostics[:64]),
                )
        run_id: Optional[str] = None
        for event in read.events:
            if event.event_kind.value == "session.started":
                run_id = event.run_id
                break
        exit_code = self._proc.returncode if self._proc is not None else None
        if read.state is JournalReadState.MALFORMED:
            status = SessionStatus.FAILED
            reason = SessionTerminationReason.JOURNAL_ERROR
            summary = "journal is malformed; it can never classify as success"
        else:
            status = SessionStatus.INTERRUPTED
            reason = SessionTerminationReason.INTERRUPTED
            summary = "worker exited without a terminal event"
        diagnostics = [
            _bounded_diagnostic(
                f"{summary} (journal state: {read.state.value}, "
                f"worker exit code: {exit_code})"
            ),
            *[
                _sanitize_diagnostic(line)
                for line in (read.error or "").split("\n")[:4]
                if line
            ],
            *self._protocol_diagnostics,
            *self._stderr_tail(),
            *self._post_mortem_cleanup(),
        ]
        return SessionResult(
            session_id=SessionId(self._session_id),
            spec=self._spec,
            status=status,
            termination_reason=reason,
            run_id=run_id,
            started_at_utc=None,
            ended_at_utc=None,
            sequence=last_sequence,
            cleanup_verified=False,
            diagnostics=tuple(diagnostics[:64]),
        )

    def _result_from_journal_events(
        self, events: Tuple[SessionEvent, ...]
    ) -> SessionResult:
        terminal_event = events[-1]
        payload = dict(terminal_event.payload)
        status = SessionStatus(payload["status"])
        reason = SessionTerminationReason(payload["termination_reason"])
        run_id: Optional[str] = None
        started_at: Optional[str] = None
        cleanup_verified = False
        for event in events:
            if event.event_kind.value == "session.started":
                run_id = event.run_id
                started_at = event.timestamp_utc
            if event.event_kind.value == "cleanup.completed":
                cleanup_verified = bool(event.payload["verified"])
        return SessionResult(
            session_id=SessionId(self._session_id),
            spec=self._spec,
            status=status,
            termination_reason=reason,
            run_id=run_id,
            started_at_utc=started_at,
            ended_at_utc=terminal_event.timestamp_utc,
            sequence=len(events) - 1,
            cleanup_verified=cleanup_verified,
            diagnostics=tuple(self._protocol_diagnostics[:64]),
        )


__all__ = [
    "SessionWorkerProcess",
    "WorkerStartupError",
]
