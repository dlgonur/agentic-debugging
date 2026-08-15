"""Unit tests for the UI presentation control models (Textual-free).

``ReplayController`` navigation must be pure read-only cursor movement over
the accepted replay cursor and the shared pure reducer: every prefix renders
exactly the fold of the persisted events, and no executable resource is
involved.  ``LiveSessionRunner`` is tested with a scripted worker double to
prove the supervision state machine (startup failure, event forwarding,
terminal + history registration, teardown never strands a worker).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    SourceKind,
    validate_session_event_stream,
)
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.application.replay import SessionReplaySource
from agentic_debugger.application.session import (
    SessionId,
    SessionResult,
    SessionStatus,
    SessionTerminationReason,
)
from agentic_debugger.ui.models import LiveSessionRunner, ReplayController

from application_support import (
    VALID_RUN_ID,
    VALID_SESSION_ID,
    VALID_TASK_ID,
    make_completed_stream,
    make_event,
    make_spec,
)


def make_identity() -> PresentationIdentity:
    return PresentationIdentity(
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
        session_id=VALID_SESSION_ID,
    )


def make_controller(events=None) -> ReplayController:
    if events is None:
        events = make_completed_stream()
    validate_session_event_stream(events)
    replay = SessionReplaySource(
        events=events,
        source_kind=events[0].source_kind,
        task_id=events[0].task_id,
        session_id=events[0].session_id,
    )
    return ReplayController(replay, make_identity())


def fold_prefix(events, index):
    view = initial_session_view(make_identity())
    for event in events[:index]:
        view = reduce_event(view, event)
    return view


def make_stream(session_id: str) -> tuple[SessionEvent, ...]:
    """A complete happy-path stream carrying one session id."""
    return (
        make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": "a" * 64},
            sequence=0,
            session_id=session_id,
        ),
        make_event(
            SessionEventKind.SESSION_STARTED,
            {},
            sequence=1,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": "executing_tool"},
            sequence=2,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.CLEANUP_STARTED,
            {},
            sequence=3,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.CLEANUP_COMPLETED,
            {"verified": True},
            sequence=4,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.SESSION_COMPLETED,
            {"status": "succeeded", "termination_reason": "done"},
            sequence=5,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
    )


class TestReplayController:
    def test_initial_state(self):
        controller = make_controller()
        assert controller.at_beginning
        assert not controller.at_end
        assert controller.index == 0
        assert controller.total_events == len(make_completed_stream())
        assert controller.view.status.value == "created"

    def test_next_reduces_incrementally(self):
        controller = make_controller()
        events = make_completed_stream()
        seen = []
        while controller.next() is not None:
            seen.append(controller.index)
        assert seen == list(range(1, len(events) + 1))
        assert controller.at_end
        assert controller.view == fold_prefix(events, len(events))
        assert controller.view.status is SessionStatus.SUCCEEDED

    def test_previous_returns_event_and_reduces_prefix(self):
        events = make_completed_stream()
        controller = make_controller(events)
        controller.end()
        assert controller.at_end
        for expected_index in range(len(events) - 1, -1, -1):
            event = controller.previous()
            assert event is events[expected_index]
            assert controller.index == expected_index
            assert controller.view == fold_prefix(events, expected_index)
        assert controller.previous() is None
        assert controller.at_beginning

    def test_begin_and_end(self):
        events = make_completed_stream()
        controller = make_controller(events)
        controller.next()
        controller.next()
        controller.end()
        assert controller.at_end
        assert controller.view == fold_prefix(events, controller.total_events)
        controller.begin()
        assert controller.at_beginning
        assert controller.index == 0

    def test_seek_and_seek_sequence(self):
        events = make_completed_stream()
        controller = make_controller(events)
        controller.seek(3)
        assert controller.index == 3
        assert controller.view == fold_prefix(events, 3)
        assert controller.seek_sequence(1)
        assert controller.index == 1
        assert not controller.seek_sequence(999)
        controller.seek(controller.total_events + 5)
        assert controller.index == controller.total_events

    def test_next_previous_phase_navigation(self):
        events = make_completed_stream()
        controller = make_controller(events)
        boundaries = controller.phase_boundaries
        assert 0 in boundaries
        assert boundaries == tuple(sorted(boundaries))
        controller.end()
        # walk backwards through every boundary (including the first one)
        walked = []
        while controller.previous_phase():
            walked.append(controller.index)
        assert walked == list(reversed(boundaries))
        controller.begin()
        walked = []
        while controller.next_phase():
            walked.append(controller.index)
        assert walked == list(boundaries[1:])

    def test_rapid_navigation_is_stable(self):
        events = make_completed_stream()
        controller = make_controller(events)
        for _ in range(500):
            controller.next()
            controller.previous()
        assert controller.index == 0
        controller.end()
        for _ in range(500):
            controller.previous()
            controller.next()
        assert controller.index == controller.total_events
        assert controller.view == fold_prefix(events, controller.total_events)


class _FakeWorker:
    """Scripted SessionWorkerProcess double for runner state-machine tests."""

    def __init__(self, session_dir: Path, events, *, startup_failure=False):
        self.session_dir = Path(session_dir)
        self.events = events
        self.startup_failure = startup_failure
        self.started = False
        self.cancelled = False
        self.closed = False
        self.pid = 12345

    def start(self):
        if self.startup_failure:
            return SessionResult(
                session_id=SessionId(self.session_dir.name),
                spec=make_spec(),
                status=SessionStatus.FAILED,
                termination_reason=SessionTerminationReason.CONTROLLER_FAILED,
                run_id=None,
                cleanup_verified=False,
                sequence=0,
                diagnostics=("injected",),
            )
        self.started = True
        return None

    def cancel(self):
        self.cancelled = True

    def wait(self):
        terminal = self.events[-1]
        payload = dict(terminal.payload)
        return SessionResult(
            session_id=SessionId(self.session_dir.name),
            spec=make_spec(),
            status=SessionStatus(payload["status"]),
            termination_reason=SessionTerminationReason(payload["termination_reason"]),
            run_id=VALID_RUN_ID,
            cleanup_verified=True,
            sequence=terminal.sequence,
        )

    def close(self):
        self.closed = True


class TestLiveSessionRunner:
    def test_startup_failure_surfaces_and_closes(self, tmp_path):
        events = make_completed_stream()
        worker = _FakeWorker(tmp_path, events, startup_failure=True)
        started = []
        events_seen = []
        terminals = []
        failures = []
        runner = LiveSessionRunner(
            worker,
            on_started=lambda: started.append(True),
            on_events=lambda e: events_seen.append(e),
            on_terminal=lambda result, error: terminals.append((result, error)),
            on_failure=lambda diagnostic: failures.append(diagnostic),
        )
        runner._drive()
        assert not started
        assert not events_seen
        assert not terminals
        assert failures == ["worker startup failed: injected"]
        assert worker.closed

    def test_event_forwarding_and_terminal_registration(self, tmp_path):
        store = HistoryStore(tmp_path)
        session_id = "sess.runner.normal"
        events = make_stream(session_id)
        # Simulate an app-owned session directory with its journal.
        from agentic_debugger.application.journal import SessionEventJournal

        session_dir = store.session_dir(session_id)
        session_dir.mkdir(parents=True)
        journal = SessionEventJournal(
            session_dir / "session.events.jsonl",
            session_id=session_id,
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
        )
        for event in events:
            journal.append(event)
        journal.close()

        worker = _FakeWorker(session_dir, events)
        started = []
        prefixes = []
        terminals = []
        failures = []
        runner = LiveSessionRunner(
            worker,
            history_store=store,
            on_started=lambda: started.append(True),
            on_events=lambda e: prefixes.append(e),
            on_terminal=lambda result, error: terminals.append((result, error)),
            on_failure=lambda diagnostic: failures.append(diagnostic),
            poll_interval_seconds=0.005,
        )
        runner._drive()
        assert started == [True]
        assert prefixes and prefixes[-1] == events
        assert len(terminals) == 1
        result, error = terminals[0]
        assert result.status is SessionStatus.SUCCEEDED
        assert error is None
        assert not failures
        entry = store.list_sessions()[0]
        assert entry.session_id == session_id
        assert entry.is_success

    def test_teardown_close_cancels_and_closes_worker(self, tmp_path):
        events = make_completed_stream()
        worker = _FakeWorker(tmp_path, events)
        runner = LiveSessionRunner(
            worker,
            on_started=lambda: None,
            on_events=lambda e: None,
            on_terminal=lambda result, error: None,
            on_failure=lambda diagnostic: None,
            poll_interval_seconds=0.005,
        )
        runner.start()
        deadline = time.monotonic() + 5.0
        while not runner.started and time.monotonic() < deadline:
            time.sleep(0.005)
        runner.close()
        assert worker.cancelled
        assert worker.closed
        assert not runner.is_alive

    def test_close_without_start_is_safe(self, tmp_path):
        events = make_completed_stream()
        worker = _FakeWorker(tmp_path, events)
        runner = LiveSessionRunner(
            worker,
            on_started=lambda: None,
            on_events=lambda e: None,
            on_terminal=lambda result, error: None,
            on_failure=lambda diagnostic: None,
        )
        runner.close()
        assert worker.closed

    def test_cancel_is_idempotent(self, tmp_path):
        events = make_completed_stream()
        worker = _FakeWorker(tmp_path, events)
        runner = LiveSessionRunner(
            worker,
            on_started=lambda: None,
            on_events=lambda e: None,
            on_terminal=lambda result, error: None,
            on_failure=lambda diagnostic: None,
        )
        runner.cancel()
        runner.cancel()
        assert worker.cancelled
