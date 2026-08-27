"""Session worker child process for the cancellable worker boundary.

This module runs inside a dedicated child process launched by the supervisor
(``worker_process.SessionWorkerProcess``) through the accepted PDB-style
bootstrap (``sys.executable -I -u -c`` + ``runpy``).  It owns:

- the Task-3 session event journal (single durable writer) and the shared
  :class:`SessionEventEmitter` (the worker lifetime's one sequence
  authority, owned/exposed by :class:`SessionCoordinator`);
- the cooperative cancellation token and the stdin cancel reader;
- the disposable execution work directory, created only when execution
  actually begins (after ``session.started``) and removed by the worker's
  cleanup cycle;
- the worker-owned cleanup cycle and its verification;
- the honest terminal event/result when cooperative termination completes.

The worker never runs controller/PDB/verifier code in Task 3: it executes
one bounded internal scenario (see ``worker_scenarios``) that proves the
boundary.  Task 7 binds the real deterministic execution source.

Exit codes: 0 = clean terminal written; 2 = startup failure (error envelope);
3 = out-of-band journal fatal (fatal envelope).
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentic_debugger.application.emitter import (
    EmitterFatalError,
    SessionEventEmitter,
)
from agentic_debugger.application.events import (
    OperatorStage,
    SessionEventKind,
    SessionPhase,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
)
from agentic_debugger.application.journal import JournalError, SessionEventJournal
from agentic_debugger.application.session import (
    MAX_DIAGNOSTIC_CHARS,
    SessionId,
    SessionResult,
    SessionSpec,
)
from agentic_debugger.application.sources import ModelExecutionError
from agentic_debugger.application.worker_protocol import (
    MAX_WORKER_LINE_BYTES,
    StartRequest,
    WorkerProtocolError,
    error_message,
    event_notification,
    liveness_notification,
    fatal_message,
    parse_cancel_message,
    parse_parent_message,
    parse_start_request,
    ready_message,
    terminal_message,
)
from agentic_debugger.application.worker_scenarios import (
    SCENARIO_NAMES,
    ScenarioContext,
    ScenarioInputError,
    run_scenario,
)
from agentic_debugger.cancellation import (
    CancellationError,
    CancellationReason,
    CancellationToken,
)

EXIT_OK = 0
EXIT_STARTUP_ERROR = 2
EXIT_JOURNAL_FATAL = 3

_MAX_DIAGNOSTICS = 64
_LIVENESS_INTERVAL_SECONDS = 0.25


class _LivenessReporter:
    """Coalesce transport activity into latest-value side-band snapshots."""

    def __init__(self, watchdog_idle_seconds: float = 300.0) -> None:
        self._watchdog = watchdog_idle_seconds
        self._request_index = -1
        self._request_started: Optional[float] = None
        self._last_activity: Optional[float] = None
        self._last_sent = 0.0
        self._lock = threading.Lock()

    def __call__(self, activity: str) -> None:
        now = time.monotonic()
        with self._lock:
            if activity == "request_started":
                self._request_index += 1
                self._request_started = now
                self._last_activity = now
            elif activity == "activity":
                self._last_activity = now
            elif activity == "request_completed":
                self._send(now, alive=False, force=True)
                self._request_started = None
                return
            else:
                return
            self._send(now, alive=self._request_started is not None, force=activity == "request_started")

    def _send(self, now: float, *, alive: bool, force: bool) -> None:
        if not force and now - self._last_sent < _LIVENESS_INTERVAL_SECONDS:
            return
        started = self._request_started
        last = self._last_activity
        try:
            _send(liveness_notification(
                request_index=self._request_index if self._request_index >= 0 else None,
                request_elapsed_seconds=(now - started if started is not None else None),
                last_activity_age_seconds=(now - last if last is not None else None),
                transport_alive=alive,
                watchdog_idle_seconds=self._watchdog,
            ))
            self._last_sent = now
        except Exception:
            pass


def run_worker_source(
    name: str,
    ctx: ScenarioContext,
    params: Any,
) -> Optional[str]:
    """Dispatch one worker execution source.

    The internal Task-3 scenarios (``worker_scenarios``) remain the bounded
    non-product boundary harness.  The two production sources (Task 7
    deterministic offline, Task 8 configured command model) are dispatched
    separately so the product's live execution never uses a synthetic
    scenario mode.
    """
    from agentic_debugger.application.configured_source import (
        CONFIGURED_SOURCE_NAME,
        run_configured_session,
    )
    from agentic_debugger.application.deterministic_source import (
        DETERMINISTIC_SOURCE_NAME,
        run_deterministic_session,
    )
    from agentic_debugger.application.ollama_cloud_source import (
        OLLAMA_CLOUD_SOURCE_NAME,
        run_ollama_cloud_session,
    )
    from agentic_debugger.application.local_project_source import (
        LOCAL_PROJECT_SOURCE_NAME,
        run_local_project_session,
    )

    if name == DETERMINISTIC_SOURCE_NAME:
        run_deterministic_session(ctx, params)
        return
    if name == CONFIGURED_SOURCE_NAME:
        run_configured_session(ctx, params)
        return
    if name == OLLAMA_CLOUD_SOURCE_NAME:
        run_ollama_cloud_session(ctx, params)
        return
    if name == LOCAL_PROJECT_SOURCE_NAME:
        return run_local_project_session(ctx, params)
    run_scenario(name, ctx, params)
    return None


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_diagnostic(text: str) -> str:
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        return "unrepresentable diagnostic"
    if len(encoded) <= MAX_DIAGNOSTIC_CHARS:
        return text
    return encoded[: MAX_DIAGNOSTIC_CHARS - 3].decode("utf-8", "replace") + "..."


def _send(payload: bytes) -> None:
    """Write one protocol message to the parent; the pipe is fail-open."""
    try:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    except Exception:
        pass


class _NotifyingJournalSink:
    """Journal sink wrapper that notifies the parent per durable append.

    The shared emitter is the session's single sequence/identity authority;
    producers (controller adapter, debugger/source/patch observability,
    verifier adapter) append through it directly.  This wrapper is the
    coordinator's notification seam: every durable append -- lifecycle or
    producer -- forwards one small sequence notification to the supervisor,
    which then catches up from the authoritative journal.  The wrapper never
    changes journal semantics: an append failure still raises the journal
    error the emitter converts into its sticky fatal state.
    """

    def __init__(self, journal: SessionEventJournal) -> None:
        self._journal = journal

    def append(self, event: SessionEvent) -> None:
        self._journal.append(event)
        _send(event_notification(event.sequence))

    def flush(self) -> None:
        self._journal.flush()

    def close(self) -> None:
        self._journal.close()


class SessionCoordinator:
    """Worker-side session lifecycle: shared emitter + journal + notification.

    The coordinator is the single producer of ``SessionEvent`` records for
    one worker lifetime: it owns/exposes the one :class:`SessionEventEmitter`
    that assigns every contiguous sequence (lifecycle, controller, debugger,
    source, patch, verifier, cleanup, and terminal events), appends each
    event to the durable journal through that authority (which validates
    identity and contiguity fail-closed), and forwards a notification to the
    supervisor (the journal remains authoritative).

    Sink-failure semantics: when the journal rejects an event, the shared
    emitter records the sticky fatal state and raises
    :class:`EmitterFatalError` (the journal-failure visibility rule).  The
    worker recognizes that as the authoritative out-of-band journal fatal:
    best-effort cleanup, fatal envelope, incomplete/non-successful journal --
    never an ordinary harness/controller failure.
    """

    def __init__(
        self,
        *,
        journal: SessionEventJournal,
        session_id: str,
        task_id: str,
        source_kind: SourceKind,
        run_id: str,
        clock: Any = None,
        emitter: SessionEventEmitter | None = None,
    ) -> None:
        self._journal = journal
        self._session_id = session_id
        self._task_id = task_id
        self._source_kind = source_kind
        self._run_id = run_id
        self._clock = clock if clock is not None else _default_clock
        self._started = False
        self._terminal_emitted = False
        self._started_at_utc: Optional[str] = None
        self._ended_at_utc: Optional[str] = None
        self._emitter = self._resolve_emitter(emitter)

    def _resolve_emitter(
        self, emitter: SessionEventEmitter | None
    ) -> SessionEventEmitter:
        """Use the injected shared authority or build the session's own.

        The session's own emitter starts at sequence 0 and uses the same
        injectable clock, with the durable journal as its authoritative
        sink.  An injected emitter must carry the identical session/task/
        source identity (fail closed); its sink/clock are owned by whoever
        constructed it.
        """
        if emitter is not None:
            if type(emitter) is not SessionEventEmitter:
                raise JournalError("emitter must be a SessionEventEmitter")
            if (
                emitter.session_id != self._session_id
                or emitter.task_id != self._task_id
                or emitter.source_kind is not self._source_kind
            ):
                raise JournalError(
                    "shared emitter identity does not match the session"
                )
            return emitter
        return SessionEventEmitter(
            session_id=self._session_id,
            task_id=self._task_id,
            source_kind=self._source_kind,
            clock=self._clock,
            # The notifying sink forwards one sequence notification per
            # durable append, so EVERY producer emission (controller,
            # debugger/source/patch, verifier) reaches the supervisor live --
            # not only the lifecycle events routed through coordinator.emit.
            sink=_NotifyingJournalSink(self._journal),
            initial_sequence=0,
        )

    @property
    def started(self) -> bool:
        return self._started

    @property
    def started_at_utc(self) -> Optional[str]:
        return self._started_at_utc

    @property
    def ended_at_utc(self) -> Optional[str]:
        return self._ended_at_utc

    @property
    def last_sequence(self) -> int:
        return self._emitter.last_sequence

    @property
    def emitter(self) -> SessionEventEmitter:
        """The session's one shared emission authority (sequence owner).

        Every producer of this worker session (controller adapter,
        observability producer, verifier adapter) must receive this same
        authority so lifecycle and producer events form one contiguous
        journal sequence.
        """
        return self._emitter

    @property
    def fatal(self) -> bool:
        """Whether the authoritative journal rejected an event (sticky)."""
        return self._emitter.fatal

    @property
    def fatal_error(self) -> Optional[str]:
        return self._emitter.fatal_error

    def emit(self, kind: SessionEventKind, payload: Dict[str, Any]) -> SessionEvent:
        """Append one validated event durably and notify the parent.

        The emission goes through the shared :class:`SessionEventEmitter`:
        the emitter assigns the next contiguous sequence, validates the
        event, appends it to the journal (the authoritative sink), and only
        then advances.  A journal rejection becomes the sticky fatal state
        and raises :class:`EmitterFatalError`, which the worker treats as
        the out-of-band journal fatal.
        """
        if kind is SessionEventKind.SESSION_STARTED:
            if self._started:
                raise JournalError("duplicate session.started")
            self._started = True
            self._started_at_utc = self._clock()
            # The shared authority binds the session run identity exactly
            # when the lifecycle does: events before ``session.started``
            # carry null, everything after carries the bound run id.
            self._emitter.bind_run_id(self._run_id)
        if kind in (
            SessionEventKind.SESSION_COMPLETED,
            SessionEventKind.SESSION_FAILED,
            SessionEventKind.SESSION_CANCELLED,
        ):
            if self._terminal_emitted:
                raise JournalError("duplicate terminal event")
            self._terminal_emitted = True
            self._ended_at_utc = self._clock()
        event = self._emitter.emit(kind, payload)
        # The notification is forwarded by the notifying journal sink
        # (``_NotifyingJournalSink``) for every durable append, including
        # producer emissions that never pass through ``coordinator.emit``.
        return event

    def emit_status(self, phase: SessionPhase) -> SessionEvent:
        """Emit ``status_changed`` carrying the running status + one phase."""
        return self.emit(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": SessionStatus.RUNNING.value, "phase": phase.value},
        )

    def emit_cancel_requested(self) -> SessionEvent:
        return self.emit(SessionEventKind.SESSION_CANCEL_REQUESTED, {})

    def emit_terminal(self, status: SessionStatus, reason: SessionTerminationReason) -> SessionEvent:
        if status is SessionStatus.CANCELLED:
            kind = SessionEventKind.SESSION_CANCELLED
        elif status in (SessionStatus.SUCCEEDED, SessionStatus.UNRESOLVED):
            kind = SessionEventKind.SESSION_COMPLETED
        else:
            kind = SessionEventKind.SESSION_FAILED
        return self.emit(
            kind,
            {"status": status.value, "termination_reason": reason.value},
        )


def _start_cancel_reader(token: CancellationToken) -> threading.Thread:
    """Daemon reader that converts ``cancel`` messages into token requests.

    Malformed or oversized lines are ignored (never acted upon); EOF simply
    ends the reader (the worker continues, the journal remains authoritative).
    """

    def _read() -> None:
        while True:
            line = sys.stdin.buffer.readline(MAX_WORKER_LINE_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_WORKER_LINE_BYTES:
                continue
            try:
                message = parse_parent_message(line.decode("utf-8"))
            except Exception:
                continue
            if message["type"] != "cancel":
                continue
            try:
                parse_cancel_message(message)
            except Exception:
                continue
            token.request()

    thread = threading.Thread(target=_read, name="worker-cancel-reader", daemon=True)
    thread.start()
    return thread


def _cleanup_work_dir(work_dir: Path, diagnostics: List[str]) -> bool:
    """Remove the session work directory and verify; never raise."""
    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)
        if work_dir.exists():
            diagnostics.append("work directory remains after cleanup")
            return False
        return True
    except BaseException as exc:  # noqa: BLE001 - cleanup must not raise
        diagnostics.append(_bounded_diagnostic(f"work directory cleanup failed: {exc}"))
        return False


def _build_result(
    request: StartRequest,
    coordinator: SessionCoordinator,
    *,
    status: SessionStatus,
    reason: SessionTerminationReason,
    cleanup_ok: bool,
    diagnostics: List[str],
) -> SessionResult:
    return SessionResult(
        session_id=SessionId(request.session_id),
        spec=request.spec,
        status=status,
        termination_reason=reason,
        run_id=request.run_id if coordinator.started else None,
        started_at_utc=coordinator.started_at_utc,
        ended_at_utc=coordinator.ended_at_utc,
        sequence=coordinator.last_sequence,
        cleanup_verified=cleanup_ok,
        diagnostics=tuple(diagnostics[: _MAX_DIAGNOSTICS]),
    )


def _fatal_journal_exit(
    work_dir: Path,
    diagnostics: List[str],
    journal: SessionEventJournal,
    exc: Exception,
) -> int:
    """Out-of-band journal fatal: best-effort cleanup, fatal envelope.

    The failed journal cannot record its own terminal state, so the failure
    is reported to the parent out of band and the journal stays
    incomplete/non-successful (authoritative correction 3).
    """
    best_effort = _cleanup_work_dir(work_dir, diagnostics)
    diagnostics.append(
        _bounded_diagnostic(
            f"best-effort cleanup {'ok' if best_effort else 'failed'}"
        )
    )
    _send(fatal_message("journal_error", [*diagnostics, _bounded_diagnostic(str(exc))]))
    try:
        journal.close()
    except JournalError:
        pass
    return EXIT_JOURNAL_FATAL


def run_worker(request: StartRequest) -> int:
    """Execute one worker lifetime; returns the process exit code."""
    work_dir = Path(request.work_dir)

    # Worker-lifecycle cleanup ownership for request-owned process groups
    # (Task 8 configured commands): on POSIX each configured command runs in
    # its own detached group, so a forced/cooperative worker shutdown must
    # terminate every in-flight group explicitly or it would be orphaned.
    # Idempotent and a no-op on Windows (the accepted Job Object already
    # covers the worker-escalation topology there).
    from agentic_debugger.application.process_tree import (
        install_worker_request_group_cleanup,
    )

    install_worker_request_group_cleanup()

    # The worker is spawned with the durable session directory as its
    # bootstrap cwd; Windows refuses to remove the cwd of a running process,
    # so the worker steps into the journal directory (the durable session
    # container) before any cleanup can run.
    try:
        os.chdir(Path(request.journal_path).resolve().parent)
    except OSError:
        pass

    token = CancellationToken(
        deadline_monotonic=(
            None
            if request.max_elapsed_seconds is None
            else time.monotonic() + float(request.max_elapsed_seconds)
        )
    )
    try:
        journal = SessionEventJournal(
            request.journal_path,
            session_id=request.session_id,
            task_id=request.spec.task_id,
            source_kind=request.spec.source.kind,
        )
    except JournalError as exc:
        _send(error_message("journal_creation_failed", [_bounded_diagnostic(str(exc))]))
        return EXIT_STARTUP_ERROR

    coordinator = SessionCoordinator(
        journal=journal,
        session_id=request.session_id,
        task_id=request.spec.task_id,
        source_kind=request.spec.source.kind,
        run_id=request.run_id,
    )
    diagnostics: List[str] = []

    try:
        coordinator.emit(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": request.spec.fingerprint()},
        )
    except (JournalError, EmitterFatalError) as exc:
        return _fatal_journal_exit(work_dir, diagnostics, journal, exc)

    _send(ready_message(coordinator.last_sequence))
    _start_cancel_reader(token)

    outcome: str = "completed"
    failure_reason: Optional[SessionTerminationReason] = None
    try:
        if request.pre_start_delay_seconds > 0:
            time.sleep(request.pre_start_delay_seconds)
        token.check()  # pre-start gate: cancellation before any session work
        coordinator.emit(SessionEventKind.SESSION_STARTED, {})
        # The disposable execution workspace is worker-owned and is created
        # only now that execution is actually beginning; before
        # ``session.started`` no execution-owned disposable resource exists,
        # so pre-start cancel/timeout and every startup failure leave none
        # behind (Task-1 pre-start rule).
        try:
            work_dir.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            raise RuntimeError(
                f"cannot create the execution work directory {work_dir}: {exc}"
            ) from exc
        coordinator.emit_status(SessionPhase.EXECUTING_TOOL)
        token.check()  # close the started -> scenario window
        disposition = run_worker_source(
            request.scenario,
            ScenarioContext(
                work_dir=work_dir,
                token=token,
                journal=journal,
                emitter=coordinator.emitter,
                run_id=request.run_id,
                session_dir=Path(request.journal_path).resolve().parent,
                liveness_reporter=_LivenessReporter(),
            ),
            request.scenario_params,
        )
        # Local Project terminal authority: typed return, not sidecar file
        if request.scenario == "local_project":
            if disposition == "UNRESOLVED":
                outcome = "unresolved"
            elif disposition == "FIXED":
                outcome = "completed"
            # Fallback to file for backward compatibility (audit only, not authority)
            # but the return value is authoritative; file write failure does not affect outcome
    except CancellationError as exc:
        if exc.reason is CancellationReason.CANCELLED:
            try:
                coordinator.emit_cancel_requested()
            except (JournalError, EmitterFatalError) as journal_exc:
                return _fatal_journal_exit(work_dir, diagnostics, journal, journal_exc)
        outcome = (
            "cancelled"
            if exc.reason is CancellationReason.CANCELLED
            else "timed_out"
        )
    except (JournalError, EmitterFatalError) as exc:
        return _fatal_journal_exit(work_dir, diagnostics, journal, exc)
    except ScenarioInputError as exc:
        diagnostics.append(_bounded_diagnostic(f"scenario input error: {exc}"))
        outcome = "failed"
    except ModelExecutionError as exc:
        # A production source classified its controller run as a genuine
        # model-execution failure (transport/provider failure, directive
        # exhaustion, controller failure).  The exact Task-1 termination
        # reason travels with the exception so the session terminal is
        # honest (model_error / directive_exhausted / controller_failed)
        # instead of an orderly completion.
        failure_reason = exc.termination_reason
        diagnostics.append(_bounded_diagnostic(f"model execution failed: {exc}"))
        outcome = "failed"
    except Exception as exc:
        diagnostics.append(_bounded_diagnostic(f"scenario failed: {exc}"))
        outcome = "failed"

    try:
        if outcome == "completed" and token.is_cancelled:
            if token.reason is CancellationReason.CANCELLED:
                coordinator.emit_cancel_requested()
                outcome = "cancelled"
            else:
                outcome = "timed_out"

        # Terminal cleanup cycle (skipped only for a pre-start cancellation,
        # which never started session-owned work and must not claim cleanup).
        if coordinator.started:
            coordinator.emit_status(SessionPhase.CLEANING)
            if request.spec.source.kind in (
                SourceKind.OLLAMA_CLOUD_LADDER,
                SourceKind.LEVEL32_OPERATOR,
            ):
                coordinator.emit(
                    SessionEventKind.OPERATOR_PROGRESS,
                    {"stage": OperatorStage.CLEANUP.value},
                )
            coordinator.emit(SessionEventKind.CLEANUP_STARTED, {})
            work_ok = _cleanup_work_dir(work_dir, diagnostics)
            cleanup_ok = work_ok
            # Local Project isolated worktree is also session-owned; must be verified before terminal
            if request.scenario == "local_project":
                parent = request.scenario_params.get("parent_tmpdir")
                repo = request.scenario_params.get("project_repo_path")
                if parent and repo:
                    try:
                        from agentic_debugger.application.local_project import cleanup_parent_tmpdir
                        iso_ok = cleanup_parent_tmpdir(Path(parent), Path(repo))
                        cleanup_ok = work_ok and iso_ok
                        if not iso_ok:
                            diagnostics.append(_bounded_diagnostic("isolated worktree cleanup failed or not verified"))
                    except Exception as exc:
                        diagnostics.append(_bounded_diagnostic(f"isolated worktree cleanup failed: {exc}"))
                        cleanup_ok = False
            coordinator.emit(SessionEventKind.CLEANUP_COMPLETED, {"verified": cleanup_ok})
        else:
            cleanup_ok = False

        status, reason = _terminal_for(
            outcome, cleanup_ok, coordinator.started, failure_reason
        )
        if request.spec.source.kind in (
            SourceKind.OLLAMA_CLOUD_LADDER,
            SourceKind.LEVEL32_OPERATOR,
        ):
            coordinator.emit(
                SessionEventKind.OPERATOR_PROGRESS,
                {"stage": OperatorStage.COMPLETED.value},
            )
        coordinator.emit_terminal(status, reason)
        try:
            journal.close()
        except JournalError as exc:
            # Every record was fsync-ed on append; a close failure is a
            # diagnostic, never a fabricated failure.
            diagnostics.append(_bounded_diagnostic(f"journal close failed: {exc}"))
        result = _build_result(
            request,
            coordinator,
            status=status,
            reason=reason,
            cleanup_ok=cleanup_ok,
            diagnostics=diagnostics,
        )
        _send(terminal_message(result))
    except (JournalError, EmitterFatalError) as exc:
        return _fatal_journal_exit(work_dir, diagnostics, journal, exc)
    return EXIT_OK


def _terminal_for(
    outcome: str,
    cleanup_ok: bool,
    started: bool,
    failure_reason: Optional[SessionTerminationReason] = None,
) -> tuple[SessionStatus, SessionTerminationReason]:
    if not started:
        # Pre-start terminals: the session never began, so no execution-owned
        # resource existed and no cleanup cycle ran or is claimed.  A deadline
        # firing before ``session.started`` is a genuine pre-start timeout,
        # never a cleanup failure.
        if outcome == "cancelled":
            return SessionStatus.CANCELLED, SessionTerminationReason.CANCELLED
        if outcome == "timed_out":
            return SessionStatus.TIMED_OUT, SessionTerminationReason.TIMEOUT
        return SessionStatus.FAILED, SessionTerminationReason.CONTROLLER_FAILED
    if not cleanup_ok:
        return SessionStatus.CLEANUP_FAILED, SessionTerminationReason.CLEANUP_FAILED
    if outcome == "completed":
        return SessionStatus.SUCCEEDED, SessionTerminationReason.DONE
    if outcome == "unresolved":
        return SessionStatus.UNRESOLVED, SessionTerminationReason.UNRESOLVED
    if outcome == "cancelled":
        return SessionStatus.CANCELLED, SessionTerminationReason.CANCELLED
    if outcome == "timed_out":
        return SessionStatus.TIMED_OUT, SessionTerminationReason.TIMEOUT
    # A production source may carry the exact honest termination reason of a
    # model-execution failure; the generic harness failure stays the default.
    return SessionStatus.FAILED, failure_reason or SessionTerminationReason.CONTROLLER_FAILED


def main() -> None:
    """Worker entry point (executed as ``__main__`` by the supervisor).

    Every exit uses ``os._exit``: the cancel-reader daemon thread blocks on
    the protocol stdin, and interpreter finalization would otherwise abort
    with a buffered-stdin fatal error.  All protocol writes and journal
    records are flushed per message, so a hard exit loses nothing.
    """
    from agentic_debugger.application.configured_source import (
        CONFIGURED_SOURCE_NAME,
    )
    from agentic_debugger.application.deterministic_source import (
        DETERMINISTIC_SOURCE_NAME,
    )
    from agentic_debugger.application.local_project_source import LOCAL_PROJECT_SOURCE_NAME
    from agentic_debugger.application.ollama_cloud_source import OLLAMA_CLOUD_SOURCE_NAME

    first_line = sys.stdin.buffer.readline(MAX_WORKER_LINE_BYTES + 1)
    if not first_line:
        _send(error_message("invalid_request", ["worker received no start message"]))
        os._exit(EXIT_STARTUP_ERROR)
    if len(first_line) > MAX_WORKER_LINE_BYTES:
        _send(error_message("invalid_request", ["start message exceeds the line bound"]))
        os._exit(EXIT_STARTUP_ERROR)
    try:
        message = parse_parent_message(first_line.decode("utf-8"))
        if message["type"] != "start":
            raise WorkerProtocolError("expected a start message")
        request = parse_start_request(message)
    except Exception as exc:
        _send(error_message("invalid_request", [_bounded_diagnostic(str(exc))]))
        os._exit(EXIT_STARTUP_ERROR)
    if (
        request.scenario not in SCENARIO_NAMES
        and request.scenario != DETERMINISTIC_SOURCE_NAME
        and request.scenario != CONFIGURED_SOURCE_NAME
        and request.scenario != OLLAMA_CLOUD_SOURCE_NAME
        and request.scenario != LOCAL_PROJECT_SOURCE_NAME
    ):
        _send(error_message("unknown_scenario", [request.scenario]))
        os._exit(EXIT_STARTUP_ERROR)
    os._exit(run_worker(request))


if __name__ == "__main__":
    main()
