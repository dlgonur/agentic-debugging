"""Integration tests for the cancellable session worker boundary.

These tests run the real worker subprocess (with the Windows job-object
process-tree boundary) and exercise the full lifecycle: handshake, normal
completion, cooperative cancellation, pre-start cancellation, duplicate
cancellation, timeout, crash/interruption, cleanup failure, out-of-band
journal failure, PDB worker teardown, and forced escalation with descendant
termination.  No GPU, network, provider, or external service is required.

The ``*_escalation`` tests are the mandatory Windows acceptance gate: a
worker that ignores cooperative cancellation and owns at least one real
descendant process must be force-terminated together with its descendants.
On POSIX the single-group ladder cannot reach detached grandchildren, so
those descendant assertions are skipped there with the limitation stated.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.events import SourceKind, validate_session_event_stream
from agentic_debugger.application.journal import JournalReadState, read_session_journal
from agentic_debugger.application.process_tree import (
    ProcessTreeError,
    pid_is_alive,
)
from agentic_debugger.application.session import (
    SessionBudgets,
    SessionSpec,
    SessionStatus,
    SessionTerminationReason,
)
from agentic_debugger.application.sources import ExecutionSourceSpec
from agentic_debugger.application.worker_process import (
    SessionWorkerProcess,
    WorkerLifecycleError,
    WorkerStartupError,
)

TASK_ID = "curated-off-by-one-002"
MODULE = "recent_window.py"
FOCUS = "recent_window"


def make_spec():
    return SessionSpec(
        task_id=TASK_ID,
        source=ExecutionSourceSpec(
            kind=SourceKind.OFFLINE_DEMO, task_id=TASK_ID, policy="static-baseline"
        ),
        budgets=SessionBudgets(),
    )


def make_worker(tmp_path, session_id, scenario, params, **kwargs):
    kwargs.setdefault("cooperative_grace_seconds", 5.0)
    kwargs.setdefault("ready_timeout_seconds", 30.0)
    return SessionWorkerProcess(
        session_dir=tmp_path / session_id,
        session_id=session_id,
        spec=make_spec(),
        run_id=f"run-{session_id}",
        scenario=scenario,
        scenario_params=params,
        **kwargs,
    )


class _RaisingJobFactory:
    """Injected job factory: creation fails."""

    def __call__(self):
        raise ProcessTreeError("injected job creation failure")


class _NoAssignJob:
    """Injected job stub: assignment fails."""

    assigned = False

    def assign(self, pid):
        return False

    def resume(self, pid):
        return True

    def terminate(self, exit_code=1):
        return True

    def close(self):
        return None


class _NoResumeJob:
    """Injected job stub: resume fails.

    ``assigned`` is True (matching a real job whose assignment succeeded),
    so the supervisor terminates through the job; the stub must therefore
    actually kill the process it was assigned.
    """

    assigned = True

    def __init__(self):
        self._pid = None

    def assign(self, pid):
        self._pid = pid
        return True

    def resume(self, pid):
        return False

    def terminate(self, exit_code=1):
        if self._pid is not None:
            # Windows: os.kill(pid, 9) is TerminateProcess (SIGKILL is not
            # defined on win32); works on suspended processes too.
            os.kill(self._pid, 9)
        return True

    def close(self):
        return None


def wait_for_file(path: Path, timeout_seconds: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def wait_for_json(path: Path, timeout_seconds: float = 25.0):
    """Wait until ``path`` exists and parses as JSON (writes are not atomic)."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.1)
    return None


class TestWorkerLifecycle:
    def test_start_handshake_and_normal_completion(self, tmp_path):
        worker = make_worker(
            tmp_path, "lifecycle.normal", "synthetic_work",
            {"steps": 3, "step_interval_seconds": 0.01},
        )
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.SUCCEEDED
        assert result.termination_reason is SessionTerminationReason.DONE
        assert result.run_id == "run-lifecycle.normal"
        assert result.cleanup_verified is True
        assert result.sequence == 6
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.COMPLETE
        validate_session_event_stream(read.events)
        assert [event.event_kind.value for event in read.events] == [
            "session.created",
            "session.started",
            "session.status_changed",
            "session.status_changed",
            "cleanup.started",
            "cleanup.completed",
            "session.completed",
        ]
        assert worker.work_dir.exists() is False
        assert worker.pid is not None
        assert pid_is_alive(worker.pid) is False
        worker.close()

    def test_events_arrive_ordered_over_the_pipe(self, tmp_path):
        worker = make_worker(
            tmp_path, "lifecycle.events", "synthetic_work",
            {"steps": 2, "step_interval_seconds": 0.01},
        )
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.SUCCEEDED
        sequences = [event.sequence for event in worker.events]
        assert sequences == list(range(len(sequences)))
        assert worker.events[0].event_kind.value == "session.created"
        assert worker.events[-1].event_kind.value == "session.completed"
        worker.close()

    def test_startup_failure_unknown_scenario(self, tmp_path):
        worker = make_worker(tmp_path, "lifecycle.unknown", "no_such_scenario", {})
        result = worker.start()
        assert result is not None
        assert result.status is SessionStatus.FAILED
        assert "unknown_scenario" in result.diagnostics[0]
        # the worker behind the terminal startup result is reaped, and the
        # disposable work directory was never created (execution never began)
        assert pid_is_alive(worker.pid) is False
        assert worker.work_dir.exists() is False
        worker.close()

    def test_startup_failure_journal_conflict(self, tmp_path):
        worker = make_worker(tmp_path, "lifecycle.conflict", "synthetic_work", {})
        worker.journal_path.write_text("already here\n", encoding="utf-8")
        result = worker.start()
        assert result is not None
        assert result.status is SessionStatus.FAILED
        assert result.termination_reason is SessionTerminationReason.JOURNAL_ERROR
        assert pid_is_alive(worker.pid) is False
        assert worker.work_dir.exists() is False
        worker.close()

    def test_harness_exception_is_failed_with_complete_journal(self, tmp_path):
        worker = make_worker(tmp_path, "lifecycle.crash", "crash", {})
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.FAILED
        assert result.termination_reason is SessionTerminationReason.CONTROLLER_FAILED
        assert result.cleanup_verified is True
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.COMPLETE
        worker.close()

    def test_hard_crash_is_interrupted_with_readable_prefix(self, tmp_path):
        worker = make_worker(tmp_path, "lifecycle.hard", "crash_hard", {})
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.INTERRUPTED
        assert result.termination_reason is SessionTerminationReason.INTERRUPTED
        assert result.cleanup_verified is False
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.INTERRUPTED
        assert read.is_success is False
        assert read.events[0].event_kind.value == "session.created"
        assert worker.work_dir.exists() is False  # post-mortem removal
        assert any("post-mortem" in d for d in result.diagnostics)
        worker.close()

    def test_duplicate_cancellation_is_benign(self, tmp_path):
        worker = make_worker(
            tmp_path, "lifecycle.dup", "synthetic_work",
            {"steps": 100000, "step_interval_seconds": 0.05},
        )
        assert worker.start() is None
        time.sleep(0.8)
        worker.cancel()
        worker.cancel()
        worker.cancel()
        result = worker.wait()
        assert result.status is SessionStatus.CANCELLED
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.COMPLETE
        cancel_events = [
            e for e in read.events if e.event_kind.value == "session.cancel_requested"
        ]
        assert len(cancel_events) == 1
        terminals = [
            e for e in read.events
            if e.event_kind.value in ("session.completed", "session.failed", "session.cancelled")
        ]
        assert len(terminals) == 1
        worker.close()


    def test_large_valid_event_survives_journal_and_parent_catch_up(self, tmp_path):
        """A valid Task-1 event well above 64 KiB persists in the journal
        and reaches the parent via journal catch-up (the pipe carries only
        sequence notifications)."""
        worker = make_worker(
            tmp_path, "lifecycle.large", "emit_large_event", {},
        )
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.SUCCEEDED
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.COMPLETE
        large = [
            event for event in read.events
            if event.event_kind.value == "debugger.locals_observed"
        ]
        assert len(large) == 1
        serialized = len(
            json.dumps(large[0].to_mapping(), ensure_ascii=False)
        )
        assert serialized > 64 * 1024
        # the parent surfaces the same event via journal catch-up
        received = [
            event for event in worker.events
            if event.event_kind.value == "debugger.locals_observed"
        ]
        assert len(received) == 1
        assert received[0].to_mapping() == large[0].to_mapping()
        worker.close()

    def test_enriched_task4_stream_survives_worker_journal(self, tmp_path):
        """Every new Task-4 event kind survives the real worker journal and
        parent catch-up as one coherent enriched session stream."""
        worker = make_worker(
            tmp_path, "lifecycle.enriched", "emit_enriched_stream", {},
        )
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.SUCCEEDED
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.COMPLETE
        kinds = {event.event_kind.value for event in read.events}
        for expected in (
            "controller.transition",
            "debugger.started",
            "debugger.location_changed",
            "debugger.stack_observed",
            "debugger.locals_observed",
            "source.snapshot",
            "diagnosis.recorded",
            "patch.proposed",
            "patch.applied",
            "patch.reverted",
            "patch.apply_failed",
            "verifier.started",
            "verifier.stage_started",
            "verifier.stage_completed",
            "verifier.completed",
        ):
            assert expected in kinds, expected
        # The parent's journal catch-up surfaces the same enriched events.
        parent_kinds = {event.event_kind.value for event in worker.events}
        assert "source.snapshot" in parent_kinds
        assert "patch.apply_failed" in parent_kinds
        worker.close()

    def test_worker_session_registers_and_replays_through_history(self, tmp_path):
        """One real worker session registers into app-owned history and
        replays read-only through the shared presentation reducer."""
        from agentic_debugger.application.history import HistoryStore
        from agentic_debugger.application.presentation import (
            PresentationIdentity,
            initial_session_view,
            reduce_event,
        )

        store = HistoryStore(tmp_path)
        session_id = "lifecycle.enriched.history"
        # Run the worker inside the store's app-owned run root so the
        # session directory is exactly what the store indexes.
        worker = SessionWorkerProcess(
            session_dir=store.session_dir(session_id),
            session_id=session_id,
            spec=make_spec(),
            run_id=f"run-{session_id}",
            scenario="emit_enriched_stream",
            scenario_params={},
            cooperative_grace_seconds=5.0,
            ready_timeout_seconds=30.0,
        )
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.SUCCEEDED
        worker.close()

        entry = store.register(worker.session_dir)
        assert entry.is_success is True
        reopened = store.reopen(session_id)
        assert reopened.entry.classification.value == "complete"

        identity = PresentationIdentity(
            task_id=TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
            session_id=session_id,
        )
        view = initial_session_view(identity)
        replay = reopened.replay
        while True:
            event = replay.next_event()
            if event is None:
                break
            view = reduce_event(view, event)
        # The enriched stream's recorded facts are present in the replayed
        # presentation: source snapshots, patch lifecycle, diagnosis,
        # verifier summary, and terminal status.
        assert view.sources
        assert any(attempt.stage.value == "reverted" for attempt in view.patch_attempts)
        assert any(attempt.stage.value == "apply_failed" for attempt in view.patch_attempts)
        assert view.diagnosis is not None
        assert view.verifier_summary is not None
        assert view.verifier_summary.outcome.value == "RESOLVED"
        assert view.status is SessionStatus.SUCCEEDED
        assert view.cleanup_verified is True
        assert replay.at_end


class TestWorkerCancellation:
    def test_cooperative_cancel_mid_run(self, tmp_path):
        worker = make_worker(
            tmp_path, "cancel.mid", "synthetic_work",
            {"steps": 100000, "step_interval_seconds": 0.05},
        )
        assert worker.start() is None
        time.sleep(1.0)
        worker.cancel()
        started = time.monotonic()
        result = worker.wait()
        assert result.status is SessionStatus.CANCELLED
        assert result.termination_reason is SessionTerminationReason.CANCELLED
        assert result.cleanup_verified is True
        assert result.run_id == "run-cancel.mid"
        assert time.monotonic() - started < 10.0
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.COMPLETE
        validate_session_event_stream(read.events)
        worker.close()

    def test_pre_start_cancellation(self, tmp_path):
        worker = make_worker(
            tmp_path, "cancel.pre", "synthetic_work",
            {"steps": 3, "step_interval_seconds": 0.01},
            pre_start_delay_seconds=3.0,
        )
        assert worker.start() is None
        # during the pre-start delay no execution-owned resource exists
        assert worker.work_dir.exists() is False
        worker.cancel()
        result = worker.wait()
        assert result.status is SessionStatus.CANCELLED
        assert result.run_id is None  # the session never started
        assert result.cleanup_verified is False  # nothing session-owned to clean
        assert worker.work_dir.exists() is False  # no execution workspace leaked
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.COMPLETE
        assert [e.event_kind.value for e in read.events] == [
            "session.created",
            "session.cancel_requested",
            "session.cancelled",
        ]
        worker.close()

    def test_pre_start_timeout_is_timed_out_without_cleanup(self, tmp_path):
        # A deadline firing before session.started is a genuine pre-start
        # timeout: TIMED_OUT/TIMEOUT, run_id None, no fake cleanup cycle,
        # and no disposable work directory left behind.
        worker = make_worker(
            tmp_path, "timeout.pre", "synthetic_work",
            {"steps": 3, "step_interval_seconds": 0.01},
            max_elapsed_seconds=1,
            pre_start_delay_seconds=1.5,
        )
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.TIMED_OUT
        assert result.termination_reason is SessionTerminationReason.TIMEOUT
        assert result.run_id is None
        assert result.cleanup_verified is False
        assert worker.work_dir.exists() is False
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.COMPLETE
        validate_session_event_stream(read.events)
        assert [e.event_kind.value for e in read.events] == [
            "session.created",
            "session.failed",
        ]
        assert read.events[-1].payload["status"] == "timed_out"
        worker.close()

    def test_cancel_before_execution_begins_after_ready(self, tmp_path):
        # cancel immediately after ready: the worker may be pre-start or just
        # started; either way the terminal must be CANCELLED and honest.
        worker = make_worker(
            tmp_path, "cancel.early", "synthetic_work",
            {"steps": 100000, "step_interval_seconds": 0.05},
        )
        assert worker.start() is None
        worker.cancel()
        result = worker.wait()
        assert result.status is SessionStatus.CANCELLED
        assert result.cleanup_verified is True
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.COMPLETE
        worker.close()

    def test_elapsed_budget_times_out_cooperatively(self, tmp_path):
        worker = make_worker(
            tmp_path, "cancel.timeout", "synthetic_work",
            {"steps": 100000, "step_interval_seconds": 0.05},
            max_elapsed_seconds=1,
        )
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.TIMED_OUT
        assert result.termination_reason is SessionTerminationReason.TIMEOUT
        assert result.cleanup_verified is True
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.COMPLETE
        assert read.events[-1].event_kind.value == "session.failed"
        worker.close()

    def test_delayed_subprocess_terminated_on_cancel(self, tmp_path):
        pid_file = tmp_path / "child-pid.txt"
        worker = make_worker(
            tmp_path, "cancel.child", "sleep_child",
            {"duration_seconds": 300, "timeout_seconds": 300, "pid_file": str(pid_file)},
        )
        assert worker.start() is None
        assert wait_for_file(pid_file)
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        worker.cancel()
        started = time.monotonic()
        result = worker.wait()
        assert result.status is SessionStatus.CANCELLED
        assert result.cleanup_verified is True
        assert time.monotonic() - started < 15.0
        assert pid_is_alive(child_pid) is False
        worker.close()

    def test_pdb_worker_gone_after_cooperative_cancel(self, tmp_path):
        diag = tmp_path / "pdb-cancel.json"
        worker = make_worker(
            tmp_path, "cancel.pdb", "pdb_cooperative_cancel",
            {"task_id": TASK_ID, "module": MODULE, "focus": FOCUS, "diag_path": str(diag)},
        )
        assert worker.start() is None
        payload = wait_for_json(diag)
        assert payload is not None
        assert payload["pdb_worker_pid"] is not None
        worker.cancel()
        result = worker.wait()
        assert result.status is SessionStatus.CANCELLED
        assert result.cleanup_verified is True
        payload = json.loads(diag.read_text(encoding="utf-8"))
        assert payload["pdb_worker_pid"] is not None
        assert payload["pdb_worker_gone_after_stop"] is True
        assert pid_is_alive(payload["pdb_worker_pid"]) is False
        worker.close()

    def test_pdb_worker_gone_after_normal_completion(self, tmp_path):
        diag = tmp_path / "pdb-normal.json"
        worker = make_worker(
            tmp_path, "pdb.normal", "pdb_session",
            {"task_id": TASK_ID, "module": MODULE, "focus": FOCUS, "diag_path": str(diag)},
        )
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.SUCCEEDED
        payload = json.loads(diag.read_text(encoding="utf-8"))
        assert payload["pdb_worker_pid"] is not None
        assert payload["pdb_worker_gone_after_stop"] is True
        assert pid_is_alive(payload["pdb_worker_pid"]) is False
        worker.close()


class TestCleanupAndJournalFailure:
    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="the cleanup-failure fixture relies on Windows filesystem "
        "semantics (an open handle blocks directory removal); on POSIX the "
        "same fixture may clean successfully, which is not a product failure",
    )
    def test_cleanup_failure_is_honest_terminal(self, tmp_path):
        worker = make_worker(tmp_path, "cleanup.fail", "cleanup_failure", {})
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.CLEANUP_FAILED
        assert result.termination_reason is SessionTerminationReason.CLEANUP_FAILED
        assert result.cleanup_verified is False
        assert worker.work_dir.exists() is True  # leftover is reported honestly
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.COMPLETE
        assert read.events[-1].event_kind.value == "session.failed"
        worker.close()

    def test_journal_failure_is_out_of_band_fatal(self, tmp_path):
        worker = make_worker(tmp_path, "journal.fatal", "break_journal", {})
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.FAILED
        assert result.termination_reason is SessionTerminationReason.JOURNAL_ERROR
        assert result.cleanup_verified is False
        # the failed journal is never upgraded to success
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.INTERRUPTED
        assert read.is_success is False
        assert result.sequence == (read.events[-1].sequence if read.events else 0)
        worker.close()

    def test_normal_completion_removes_work_directory(self, tmp_path):
        worker = make_worker(
            tmp_path, "cleanup.ok", "synthetic_work",
            {"steps": 2, "step_interval_seconds": 0.01},
        )
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.SUCCEEDED
        assert worker.work_dir.exists() is False
        worker.close()


class TestForcedEscalation:
    """Mandatory Windows acceptance gate: the worker ignoring cooperative
    cancellation and owning real descendants must be terminated together
    with its whole process tree, and the session must never claim a
    cooperative CANCELLED outcome."""

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="the Windows job-object process-tree guarantee is the acceptance "
        "gate; POSIX escalation covers only the worker's own process group",
    )
    def test_escalation_terminates_worker_and_descendant(self, tmp_path):
        pid_file = tmp_path / "child-pid.txt"
        worker = make_worker(
            tmp_path, "esc.child", "ignore_cancel_with_child",
            {"hold_seconds": 300, "child_duration_seconds": 300, "pid_file": str(pid_file)},
            cooperative_grace_seconds=2.0,
        )
        assert worker.start() is None
        assert wait_for_file(pid_file)
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        worker_pid = worker.pid
        assert worker.job_assigned is True
        worker.cancel()
        result = worker.wait()
        assert result.status is SessionStatus.INTERRUPTED
        assert result.cleanup_verified is False
        assert pid_is_alive(worker_pid) is False
        assert pid_is_alive(child_pid) is False
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.INTERRUPTED
        assert read.is_success is False
        assert worker.work_dir.exists() is False  # post-mortem cleanup ran
        worker.close()

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="the Windows job-object process-tree guarantee is the acceptance gate",
    )
    def test_escalation_terminates_real_pdb_worker_descendant(self, tmp_path):
        diag = tmp_path / "pdb-esc.json"
        worker = make_worker(
            tmp_path, "esc.pdb", "pdb_ignore_cancel",
            {"task_id": TASK_ID, "module": MODULE, "focus": FOCUS,
             "diag_path": str(diag), "hold_seconds": 300},
            cooperative_grace_seconds=2.0,
        )
        assert worker.start() is None
        payload = wait_for_json(diag)
        assert payload is not None
        pdb_pid = payload["pdb_worker_pid"]
        assert pdb_pid is not None
        worker_pid = worker.pid
        worker.cancel()
        result = worker.wait()
        assert result.status is SessionStatus.INTERRUPTED
        assert pid_is_alive(worker_pid) is False
        assert pid_is_alive(pdb_pid) is False
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.INTERRUPTED
        worker.close()

    def test_escalation_never_claims_cancelled(self, tmp_path):
        # On any platform, forced termination without verified cooperative
        # cleanup must never produce CANCELLED.
        worker = make_worker(
            tmp_path, "esc.never", "ignore_cancel",
            {"hold_seconds": 300},
            cooperative_grace_seconds=2.0,
        )
        assert worker.start() is None
        worker.cancel()
        result = worker.wait()
        assert result.status is not SessionStatus.CANCELLED
        assert result.status is SessionStatus.INTERRUPTED
        assert result.cleanup_verified is False
        worker.close()


class TestJobContainmentFailClosed:
    """Windows containment is mandatory: any job-setup failure must fail
    startup with the suspended worker terminated and every resource closed
    (never an uncontained live worker)."""

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="the Windows job-object containment gate is the acceptance boundary",
    )
    def test_job_creation_failure_fails_closed(self, tmp_path):
        worker = make_worker(
            tmp_path, "contain.create", "synthetic_work",
            {"steps": 2, "step_interval_seconds": 0.01},
            job_factory=_RaisingJobFactory(),
        )
        with pytest.raises(WorkerStartupError):
            worker.start()
        assert worker.pid is not None
        assert pid_is_alive(worker.pid) is False
        assert worker.job_assigned is False
        assert worker.work_dir.exists() is False
        # wait() fails fast instead of hanging
        with pytest.raises(WorkerStartupError):
            worker.wait()
        worker.close()

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="the Windows job-object containment gate is the acceptance boundary",
    )
    def test_job_assignment_failure_fails_closed(self, tmp_path):
        worker = make_worker(
            tmp_path, "contain.assign", "synthetic_work",
            {"steps": 2, "step_interval_seconds": 0.01},
            job_factory=lambda: _NoAssignJob(),
        )
        with pytest.raises(WorkerStartupError):
            worker.start()
        assert worker.pid is not None
        assert pid_is_alive(worker.pid) is False
        assert worker.job_assigned is False
        assert worker.work_dir.exists() is False
        with pytest.raises(WorkerStartupError):
            worker.wait()
        worker.close()

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="the Windows job-object containment gate is the acceptance boundary",
    )
    def test_job_resume_failure_fails_closed(self, tmp_path):
        worker = make_worker(
            tmp_path, "contain.resume", "synthetic_work",
            {"steps": 2, "step_interval_seconds": 0.01},
            job_factory=lambda: _NoResumeJob(),
        )
        with pytest.raises(WorkerStartupError):
            worker.start()
        assert worker.pid is not None
        assert pid_is_alive(worker.pid) is False
        assert worker.job_assigned is False
        assert worker.work_dir.exists() is False
        with pytest.raises(WorkerStartupError):
            worker.wait()
        worker.close()


class TestSpawnFailureLifecycle:
    """Raw process-spawn and start-message failures fail closed.

    A failed ``start()`` must leave the supervisor in a deterministic
    failed-start state: ``wait()`` fails immediately, ``close()`` stays
    safe and idempotent, a second ``start()`` is rejected (one-shot
    semantics), and no disposable work directory was ever created.
    """

    def test_raw_spawn_failure_fails_closed(self, tmp_path, monkeypatch):
        worker = make_worker(
            tmp_path, "spawn.raw", "synthetic_work",
            {"steps": 2, "step_interval_seconds": 0.01},
        )

        def _boom(*args, **kwargs):
            raise OSError("spawn boom")

        monkeypatch.setattr(
            "agentic_debugger.application.worker_process.subprocess.Popen", _boom
        )
        with pytest.raises(WorkerStartupError, match="failed to spawn"):
            worker.start()
        assert worker.pid is None  # no process exists
        assert worker.work_dir.exists() is False
        # wait() fails immediately and deterministically instead of hanging
        started = time.monotonic()
        with pytest.raises(WorkerStartupError, match="failed to spawn"):
            worker.wait()
        assert time.monotonic() - started < 2.0
        # a second start() is rejected: one-shot supervisor semantics
        with pytest.raises(ApplicationInputError):
            worker.start()
        # close() remains safe and idempotent
        worker.close()
        worker.close()

    def test_start_message_delivery_failure_fails_closed(self, tmp_path, monkeypatch):
        worker = make_worker(
            tmp_path, "spawn.msg", "synthetic_work",
            {"steps": 2, "step_interval_seconds": 0.01},
        )

        def _boom(*args, **kwargs):
            raise OSError("pipe boom")

        monkeypatch.setattr(
            "agentic_debugger.application.worker_process.start_message", _boom
        )
        with pytest.raises(WorkerStartupError, match="failed to send the start message"):
            worker.start()
        assert worker.pid is not None
        assert pid_is_alive(worker.pid) is False  # the spawned worker was unwound
        assert worker.work_dir.exists() is False
        with pytest.raises(WorkerStartupError):
            worker.wait()
        worker.close()

    def test_started_execution_creates_and_removes_work_directory(self, tmp_path):
        # The disposable execution workspace is created only when execution
        # actually begins and is removed by the cooperative cleanup cycle.
        # (The worker can create it inside the supervisor's poll window, so
        # the deterministic "nothing before execution" assertion lives in
        # the pre-start cancellation test.)
        worker = make_worker(
            tmp_path, "workdir.mid", "synthetic_work",
            {"steps": 100000, "step_interval_seconds": 0.05},
        )
        assert worker.start() is None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if worker.work_dir.exists():
                break
            time.sleep(0.05)
        assert worker.work_dir.exists() is True  # created during started execution
        worker.cancel()
        result = worker.wait()
        assert result.status is SessionStatus.CANCELLED
        assert result.cleanup_verified is True
        assert worker.work_dir.exists() is False  # cooperative cancel removes it
        worker.close()


class TestSupervisorLifecycle:
    """The supervisor is a lifecycle boundary: invalid usage fails closed."""

    def test_wait_before_start_fails_promptly(self, tmp_path):
        worker = make_worker(
            tmp_path, "lifecycle.waitfirst", "synthetic_work",
            {"steps": 2, "step_interval_seconds": 0.01},
        )
        started = time.monotonic()
        with pytest.raises(WorkerLifecycleError):
            worker.wait()
        assert time.monotonic() - started < 2.0
        worker.close()

    def test_start_after_close_fails(self, tmp_path):
        worker = make_worker(
            tmp_path, "lifecycle.afterclose", "synthetic_work",
            {"steps": 2, "step_interval_seconds": 0.01},
        )
        assert worker.start() is None
        worker.wait()
        worker.close()
        with pytest.raises(WorkerLifecycleError):
            worker.start()

    def test_wait_after_close_fails(self, tmp_path):
        worker = make_worker(
            tmp_path, "lifecycle.waitclosed", "synthetic_work",
            {"steps": 2, "step_interval_seconds": 0.01},
        )
        assert worker.start() is None
        worker.wait()
        worker.close()
        with pytest.raises(WorkerLifecycleError):
            worker.wait()

    def test_cancel_before_start_remains_supported(self, tmp_path):
        # cancel-before-start is the valid Task-1 pre-start cancellation
        # path and must not raise.
        worker = make_worker(
            tmp_path, "lifecycle.cancelfirst", "synthetic_work",
            {"steps": 100000, "step_interval_seconds": 0.05},
            pre_start_delay_seconds=3.0,
        )
        worker.cancel()
        assert worker.start() is None
        result = worker.wait()
        assert result.status is SessionStatus.CANCELLED
        worker.close()


class TestSupervisorClassification:
    def test_ready_timeout_escalates_and_classifies(self, tmp_path):
        # A handshake bound far below the worker's import time guarantees the
        # timeout fires: the worker is terminated and classified honestly
        # (never a fabricated terminal).
        worker = make_worker(
            tmp_path, "class.ready", "synthetic_work",
            {"steps": 3, "step_interval_seconds": 0.01},
            ready_timeout_seconds=0.05,
        )
        result = worker.start()
        assert result is not None
        assert result.status in (SessionStatus.FAILED, SessionStatus.INTERRUPTED)
        worker.close()

    def test_pipes_and_handles_released_on_close(self, tmp_path):
        worker = make_worker(
            tmp_path, "class.close", "synthetic_work",
            {"steps": 2, "step_interval_seconds": 0.01},
        )
        assert worker.start() is None
        worker.wait()
        worker.close()
        # close() is idempotent and must not raise
        worker.close()
        assert worker._reader_thread is None or not worker._reader_thread.is_alive()
