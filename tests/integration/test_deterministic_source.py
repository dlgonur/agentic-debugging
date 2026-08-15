"""Task 7 backend gates: the production deterministic execution source.

Runs one real deterministic offline session through the accepted worker
boundary with the production source name (never a synthetic worker scenario)
and proves:

- the real controller/tool/PDB/PatchManager/verifier stack executed (the
  journal carries truthful controller, model, tool, debugger, source, patch,
  verifier, artifact, cleanup, and terminal events);
- one contiguous session journal through one shared emission authority;
- source snapshots survive the disposable workspace cleanup;
- patch lifecycle and verifier progress/final result are truthful;
- cancellation produces CANCELLED only after verified cleanup and leaves no
  worker process behind;
- HistoryStore registration, reopen, and full replay with live/replay
  presentation parity.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agentic_debugger.application.deterministic_source import (
    DETERMINISTIC_SOURCE_NAME,
)
from agentic_debugger.application.events import (
    SourceKind,
    validate_session_event_stream,
)
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.journal import JournalReadState, read_session_journal
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.application.process_tree import pid_is_alive
from agentic_debugger.application.session import (
    SessionBudgets,
    SessionSpec,
    SessionStatus,
    SessionTerminationReason,
)
from agentic_debugger.application.sources import ExecutionSourceSpec
from agentic_debugger.application.worker_process import SessionWorkerProcess

TASK_ID = "curated-off-by-one-002"
PDB_POLICY = "pdb-on-uncertainty"
STATIC_POLICY = "static-baseline"


def make_spec(policy: str) -> SessionSpec:
    return SessionSpec(
        task_id=TASK_ID,
        source=ExecutionSourceSpec(
            kind=SourceKind.OFFLINE_DEMO,
            task_id=TASK_ID,
            policy=policy,
            model_config_ref=None,
        ),
        budgets=SessionBudgets(),
    )


def make_worker(
    store: HistoryStore,
    session_id: str,
    policy: str,
    **kwargs,
) -> SessionWorkerProcess:
    kwargs.setdefault("cooperative_grace_seconds", 10.0)
    kwargs.setdefault("ready_timeout_seconds", 90.0)
    return SessionWorkerProcess(
        session_dir=store.session_dir(session_id),
        session_id=session_id,
        spec=make_spec(policy),
        run_id=f"run-{session_id}",
        scenario=DETERMINISTIC_SOURCE_NAME,
        scenario_params={"task_id": TASK_ID, "policy": policy},
        **kwargs,
    )


def wait_for_journal_events(journal_path: Path, minimum: int, timeout_seconds: float = 120.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        read = read_session_journal(journal_path)
        if len(read.events) >= minimum:
            return len(read.events)
        time.sleep(0.2)
    raise AssertionError(
        f"journal never reached {minimum} events (last {len(read.events)})"
    )


@pytest.fixture(scope="module")
def completed_run(tmp_path_factory):
    """One real deterministic run (module-scoped so the suite runs it once)."""
    root = tmp_path_factory.mktemp("deterministic")
    store = HistoryStore(root)
    session_id = "sess.deterministic.full"
    worker = make_worker(store, session_id, PDB_POLICY)
    assert worker.start() is None
    worker_pid = worker.pid
    result = worker.wait()
    worker.close()
    return {
        "store": store,
        "session_id": session_id,
        "worker_pid": worker_pid,
        "result": result,
        "journal": read_session_journal(worker.journal_path),
        "session_dir": worker.session_dir,
    }


class TestProductionSourceCompletes:
    def test_session_succeeds_with_verified_cleanup(self, completed_run):
        result = completed_run["result"]
        assert result.status is SessionStatus.SUCCEEDED
        assert result.termination_reason is SessionTerminationReason.DONE
        assert result.cleanup_verified is True
        assert result.run_id == f"run-{completed_run['session_id']}"
        # the disposable execution workspace is gone
        assert (completed_run["session_dir"] / "work").exists() is False

    def test_journal_is_contiguous_and_complete(self, completed_run):
        journal = completed_run["journal"]
        assert journal.state is JournalReadState.COMPLETE
        validate_session_event_stream(journal.events)
        assert [e.sequence for e in journal.events] == list(range(len(journal.events)))
        kinds = [event.event_kind.value for event in journal.events]
        assert kinds[0] == "session.created"
        assert kinds[-1] == "session.completed"

    def test_real_controller_tool_pdb_patch_verifier_stack_executed(self, completed_run):
        kinds = {event.event_kind.value for event in completed_run["journal"].events}
        for expected in (
            "session.started",
            "session.status_changed",
            "controller.step",
            "controller.transition",
            "model.request_started",
            "model.request_completed",
            "model.directive_accepted",
            "tool.started",
            "tool.completed",
            "debugger.started",
            "debugger.location_changed",
            "debugger.stack_observed",
            "debugger.locals_observed",
            "source.snapshot",
            "diagnosis.recorded",
            "patch.proposed",
            "patch.applied",
            "verifier.started",
            "verifier.stage_started",
            "verifier.stage_completed",
            "verifier.completed",
            "artifact.written",
            "cleanup.started",
            "cleanup.completed",
        ):
            assert expected in kinds, expected

    def test_source_snapshots_persist_after_workspace_cleanup(self, completed_run):
        snapshots = [
            event for event in completed_run["journal"].events
            if event.event_kind.value == "source.snapshot"
        ]
        assert snapshots, "no source snapshots recorded"
        for event in snapshots:
            payload = dict(event.payload)
            assert payload["path"]
            assert len(payload["sha256"]) == 64
            assert payload["text"], "recorded source text must be displayable"
            assert payload["stage"] in ("initial", "applied", "reverted")
        stages = {payload["stage"] for payload in [dict(e.payload) for e in snapshots]}
        assert "initial" in stages

    def test_patch_lifecycle_and_verifier_authority_are_truthful(self, completed_run):
        events = completed_run["journal"].events
        kinds = [e.event_kind.value for e in events]
        proposed = [e for e in events if e.event_kind.value == "patch.proposed"]
        applied = [e for e in events if e.event_kind.value == "patch.applied"]
        assert proposed and applied
        # the applied patch is the proposed candidate (same attempt index)
        assert proposed[0].payload["attempt_index"] == applied[0].payload["attempt_index"]
        # verifier progress then final authority, with progress != correctness
        verifier_completed = [
            e for e in events if e.event_kind.value == "verifier.completed"
        ]
        assert len(verifier_completed) == 1
        payload = dict(verifier_completed[0].payload)
        assert payload["status"] == "COMPLETED"
        assert payload["outcome"] is not None
        # application completion and scientific outcome stay distinct: the
        # session terminal never carries a correctness verdict
        terminal = dict(events[-1].payload)
        assert "status" in terminal and "outcome" not in terminal

    def test_artifacts_persisted_in_session_directory(self, completed_run):
        session_dir = Path(completed_run["session_dir"])
        candidate = session_dir / "candidate.patch"
        evaluation = session_dir / "evaluation.json"
        assert candidate.is_file()
        assert candidate.read_text(encoding="utf-8").startswith("--- ")
        assert evaluation.is_file()
        record = json.loads(evaluation.read_text(encoding="utf-8"))
        assert record["status"] == "COMPLETED"
        artifact_events = [
            e for e in completed_run["journal"].events
            if e.event_kind.value == "artifact.written"
        ]
        assert len(artifact_events) == 2

    def test_static_baseline_policy_legitimately_skips_debugger(self, tmp_path):
        """A policy that never reaches PDB leaves the debugger pane empty --
        the truthful not-recorded state, never synthetic UI events."""
        store = HistoryStore(tmp_path)
        worker = make_worker(store, "sess.deterministic.static", STATIC_POLICY)
        assert worker.start() is None
        result = worker.wait()
        worker.close()
        assert result.status is SessionStatus.SUCCEEDED
        kinds = {e.event_kind.value for e in read_session_journal(worker.journal_path).events}
        assert "debugger.started" not in kinds
        assert "source.snapshot" in kinds

    def test_worker_process_is_reaped(self, completed_run):
        assert pid_is_alive(completed_run["worker_pid"]) is False


class TestHistoryAndReplayParity:
    def test_registration_reopen_and_full_replay_parity(self, completed_run):
        store = completed_run["store"]
        session_id = completed_run["session_id"]
        entry = store.register(completed_run["session_dir"])
        assert entry.is_success is True
        assert entry.status is SessionStatus.SUCCEEDED
        assert entry.verifier_status == "COMPLETED"

        reopened = store.reopen(session_id)
        assert reopened.entry.classification.value == "complete"

        # live final presentation state (fold of the complete journal)
        events = completed_run["journal"].events
        identity = PresentationIdentity(
            task_id=events[0].task_id,
            source_kind=events[0].source_kind,
            session_id=events[0].session_id,
        )
        live_view = initial_session_view(identity)
        for event in events:
            live_view = reduce_event(live_view, event)

        # replay the complete journal through the same pure reducer
        replay_view = initial_session_view(identity)
        while True:
            event = reopened.replay.next_event()
            if event is None:
                break
            replay_view = reduce_event(replay_view, event)

        assert replay_view == live_view
        assert live_view.status is SessionStatus.SUCCEEDED
        assert live_view.sources
        assert live_view.debugger.frames
        assert live_view.debugger.locals
        assert live_view.verifier_summary is not None
        assert any(a.stage.value == "verified" for a in live_view.patch_attempts)
        assert live_view.cleanup_verified is True

    def test_representative_prefix_parity(self, completed_run):
        """Intermediate prefixes replay to the same presentation state."""
        store = completed_run["store"]
        reopened = store.reopen(completed_run["session_id"])
        events = completed_run["journal"].events
        identity = PresentationIdentity(
            task_id=events[0].task_id,
            source_kind=events[0].source_kind,
            session_id=events[0].session_id,
        )
        for prefix in (1, 5, 12, len(events) // 2, len(events) - 2):
            live_view = initial_session_view(identity)
            for event in events[:prefix]:
                live_view = reduce_event(live_view, event)
            reopened.replay.rewind()
            replay_view = initial_session_view(identity)
            for _ in range(prefix):
                event = reopened.replay.next_event()
                assert event is not None
                replay_view = reduce_event(replay_view, event)
            assert replay_view == live_view, f"prefix parity failed at {prefix}"


class TestCancellation:
    def test_cancel_mid_run_is_honest_and_leaves_no_worker(self, tmp_path):
        store = HistoryStore(tmp_path)
        worker = make_worker(store, "sess.deterministic.cancel", PDB_POLICY)
        assert worker.start() is None
        worker_pid = worker.pid
        # cancel once real execution events are flowing
        wait_for_journal_events(worker.journal_path, minimum=6)
        worker.cancel()
        worker.cancel()  # repeated cancel stays safe
        started = time.monotonic()
        result = worker.wait()
        elapsed = time.monotonic() - started
        assert result.status is SessionStatus.CANCELLED
        assert result.termination_reason is SessionTerminationReason.CANCELLED
        assert result.cleanup_verified is True
        assert elapsed < 60.0
        read = read_session_journal(worker.journal_path)
        assert read.state is JournalReadState.COMPLETE
        validate_session_event_stream(read.events)
        cancel_events = [
            e for e in read.events if e.event_kind.value == "session.cancel_requested"
        ]
        assert len(cancel_events) == 1
        assert read.events[-1].event_kind.value == "session.cancelled"
        assert pid_is_alive(worker_pid) is False
        assert (worker.session_dir / "work").exists() is False
        worker.close()

    def test_cancelled_session_registers_honestly(self, tmp_path):
        store = HistoryStore(tmp_path)
        worker = make_worker(store, "sess.deterministic.cancel2", PDB_POLICY)
        assert worker.start() is None
        wait_for_journal_events(worker.journal_path, minimum=6)
        worker.cancel()
        result = worker.wait()
        worker.close()
        assert result.status is SessionStatus.CANCELLED
        entry = store.register(worker.session_dir)
        assert entry.is_success is True
        assert entry.status is SessionStatus.CANCELLED
        reopened = store.reopen("sess.deterministic.cancel2")
        assert reopened.entry.classification.value == "complete"
        # the cancelled journal replays read-only to its terminal state
        view = initial_session_view(
            PresentationIdentity(
                task_id=TASK_ID,
                source_kind=SourceKind.OFFLINE_DEMO,
                session_id="sess.deterministic.cancel2",
            )
        )
        while True:
            event = reopened.replay.next_event()
            if event is None:
                break
            view = reduce_event(view, event)
        assert view.status is SessionStatus.CANCELLED
        assert view.cleanup_verified is True
