"""BLOCKER 1 (Repair Pass 2): the real SessionCoordinator is the shared
sequence authority.

Proves that the actual Task-3 ``SessionCoordinator`` -- not a manually
simulated lifecycle -- owns/exposes the one ``SessionEventEmitter`` that the
controller adapter, the debugger/source/patch observability producer, and
the verifier adapter share; that lifecycle and producer events interleave
into one contiguous complete Task-3 journal; and that an authoritative
journal failure stays sticky/out-of-band fatal through the integrated
coordinator/emitter instead of degrading into an ordinary failure.
"""

from __future__ import annotations

import pytest

from agentic_debugger.application import ApplicationError
from agentic_debugger.application.controller_adapter import (
    ControllerObservationContext,
    ControllerSessionEventAdapter,
)
from agentic_debugger.application.emitter import (
    EmitterFatalError,
    SessionEventEmitter,
)
from agentic_debugger.application.events import (
    SessionEventKind,
    SourceKind,
    validate_session_event_stream,
)
from agentic_debugger.application.journal import (
    JournalReadState,
    SessionEventJournal,
    read_session_journal,
)
from agentic_debugger.application.observability import (
    ObservabilityContext,
    SessionObservability,
)
from agentic_debugger.application.session import SessionPhase
from agentic_debugger.application.verifier_observer import VerifierSessionEventAdapter
from agentic_debugger.application.worker import SessionCoordinator
from agentic_debugger.agent.controller import ControllerStopReason
from agentic_debugger.agent.observer import (
    ControllerObservation,
    ControllerObservationKind,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.events.schema import ObservationStatus
from application_support import (
    VALID_PATCH_SHA256,
    VALID_RUN_ID,
    VALID_SPEC_FINGERPRINT,
    VALID_TASK_ID,
)

FIXED = "2026-08-14T08:00:00Z"
SESSION_ID = "sess-coord-001"


def make_coordinator(tmp_path, *, session_id=SESSION_ID, run_id=VALID_RUN_ID):
    journal = SessionEventJournal(
        tmp_path / "session.events.jsonl",
        session_id=session_id,
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
    )
    coordinator = SessionCoordinator(
        journal=journal,
        session_id=session_id,
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
        run_id=run_id,
        clock=lambda: FIXED,
    )
    return coordinator, journal


def controller_adapter(emitter, *, run_id=VALID_RUN_ID):
    return ControllerSessionEventAdapter(
        ControllerObservationContext(
            session_id=SESSION_ID,
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
            run_id=run_id,
        ),
        emitter=emitter,
    )


def observability(emitter, *, run_id=VALID_RUN_ID):
    return SessionObservability(
        ObservabilityContext(
            session_id=SESSION_ID,
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
            run_id=run_id,
        ),
        emitter=emitter,
    )


def verifier_adapter(emitter, *, run_id=VALID_RUN_ID):
    return VerifierSessionEventAdapter(
        ObservabilityContext(
            session_id=SESSION_ID,
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
            run_id=run_id,
        ),
        emitter=emitter,
    )


def observation(kind, **fields):
    values = {"run_id": VALID_RUN_ID, "task_id": VALID_TASK_ID}
    values.update(fields)
    return ControllerObservation(kind=kind, **values)


def make_producers(emitter):
    controller = controller_adapter(emitter)
    obs = observability(emitter)
    verifier = verifier_adapter(emitter)
    return controller, obs, verifier


def run_standard_interleave(coordinator, controller, obs, verifier):
    """Emit the standard mixed stream and return the coordinator/terminal
    events the caller must also append (cleanup + terminal)."""
    coordinator.emit(
        SessionEventKind.SESSION_CREATED,
        {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
    )
    coordinator.emit(SessionEventKind.SESSION_STARTED, {})
    coordinator.emit_status(SessionPhase.EXECUTING_TOOL)
    controller.notify(observation(
        ControllerObservationKind.RUN_STARTED,
        state_before=ControllerState.REPRODUCE,
    ))
    controller.notify(observation(
        ControllerObservationKind.TOOL_STARTED,
        model_call_index=0,
        tool_name="get_stack_summary",
        state_before=ControllerState.REPRODUCE,
    ))
    obs.stack_observed(
        {
            "pause_generation": 1,
            "frames": [
                {"frame_id": 0, "script": "profile.py", "line": 12,
                 "function": "format_display_name", "is_current": True}
            ],
        }
    )
    controller.notify(observation(
        ControllerObservationKind.TOOL_COMPLETED,
        model_call_index=0,
        tool_name="get_stack_summary",
        observation_status=ObservationStatus.OK,
        state_before=ControllerState.REPRODUCE,
    ))
    obs.patch_proposed(0, VALID_PATCH_SHA256, patch_text="--- a/x.py\n+++ b/x.py\n")
    verifier.started()
    verifier.stage_started("prepare_workspace")
    verifier.stage_completed("prepare_workspace", "completed")
    obs.patch_applied(0, ["x.py"], syntax_passed=True)
    controller.notify(observation(
        ControllerObservationKind.STEP_COMPLETED,
        step_index=0,
        directive_kind="action",
        stop_reason=ControllerStopReason.DONE.value,
        state_before=ControllerState.REPRODUCE,
        state_after=ControllerState.UNDERSTAND,
    ))


def emit_terminal_cycle(coordinator):
    from agentic_debugger.application.events import (
        SessionStatus,
        SessionTerminationReason,
    )

    coordinator.emit_status(SessionPhase.CLEANING)
    coordinator.emit(SessionEventKind.CLEANUP_STARTED, {})
    coordinator.emit(SessionEventKind.CLEANUP_COMPLETED, {"verified": True})
    coordinator.emit_terminal(SessionStatus.SUCCEEDED, SessionTerminationReason.DONE)


class TestCoordinatorSharedAuthority:
    def test_coordinator_emits_lifecycle_through_shared_authority(self, tmp_path):
        coordinator, journal = make_coordinator(tmp_path)
        coordinator.emit(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
        )
        coordinator.emit(SessionEventKind.SESSION_STARTED, {})
        coordinator.emit_status(SessionPhase.EXECUTING_TOOL)
        read = read_session_journal(tmp_path / "session.events.jsonl")
        assert [event.sequence for event in read.events] == [0, 1, 2]
        assert coordinator.emitter.last_sequence == 2
        # The shared authority binds the run id exactly at ``session.started``.
        assert read.events[0].run_id is None
        assert read.events[1].run_id == VALID_RUN_ID
        assert read.events[2].run_id == VALID_RUN_ID
        journal.close()

    def test_producers_receive_the_same_authority(self, tmp_path):
        coordinator, _ = make_coordinator(tmp_path)
        controller, obs, verifier = make_producers(coordinator.emitter)
        assert controller.emitter is coordinator.emitter
        assert obs.emitter is coordinator.emitter
        assert verifier.emitter is coordinator.emitter

    def test_lifecycle_and_producer_events_interleave_without_gap(self, tmp_path):
        coordinator, journal = make_coordinator(tmp_path)
        controller, obs, verifier = make_producers(coordinator.emitter)
        run_standard_interleave(coordinator, controller, obs, verifier)
        emit_terminal_cycle(coordinator)
        # The coordinator's lifecycle events and every producer's events all
        # land in the one journal through the one shared authority: the
        # journal must read back one contiguous 0..N stream, no gaps or
        # duplicates, ending with the terminal event.
        journal_events = read_session_journal(
            tmp_path / "session.events.jsonl"
        ).events
        sequences = [event.sequence for event in journal_events]
        assert sequences == list(range(len(journal_events)))
        assert len(set(sequences)) == len(sequences)
        assert journal_events[-1].event_kind is SessionEventKind.SESSION_COMPLETED
        assert coordinator.emitter.last_sequence == len(journal_events) - 1
        journal.close()

    def test_cleanup_and_terminal_continue_with_next_sequence(self, tmp_path):
        coordinator, journal = make_coordinator(tmp_path)
        controller, obs, verifier = make_producers(coordinator.emitter)
        run_standard_interleave(coordinator, controller, obs, verifier)
        before = coordinator.emitter.next_sequence
        emit_terminal_cycle(coordinator)
        after = coordinator.emitter.next_sequence
        # Cleanup + terminal events continue exactly after the producers.
        assert after - before == 4
        read = read_session_journal(tmp_path / "session.events.jsonl")
        kinds = [event.event_kind.value for event in read.events]
        assert kinds[-4:] == [
            "session.status_changed",
            "cleanup.started",
            "cleanup.completed",
            "session.completed",
        ]
        assert [event.sequence for event in read.events] == list(
            range(len(read.events))
        )
        journal.close()

    def test_one_real_journal_reads_back_contiguous_and_complete(self, tmp_path):
        coordinator, journal = make_coordinator(tmp_path)
        controller, obs, verifier = make_producers(coordinator.emitter)
        run_standard_interleave(coordinator, controller, obs, verifier)
        emit_terminal_cycle(coordinator)
        journal.close()
        read = read_session_journal(tmp_path / "session.events.jsonl")
        assert read.state is JournalReadState.COMPLETE
        assert [event.sequence for event in read.events] == list(
            range(len(read.events))
        )
        validate_session_event_stream(read.events)

    def test_no_producer_owns_an_independent_sequence(self, tmp_path):
        coordinator, journal = make_coordinator(tmp_path)
        controller, obs, verifier = make_producers(coordinator.emitter)
        # Two lifecycle events precede every producer emission; a producer
        # with its own counter starting at 0 would collide with the
        # coordinator's sequence 0/1.
        coordinator.emit(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
        )
        coordinator.emit(SessionEventKind.SESSION_STARTED, {})
        controller.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            state_before=ControllerState.REPRODUCE,
        ))
        obs.debugger_started("profile.py", ["profile.py:12"])
        verifier.started()
        emitted = (
            controller.events() + obs.events() + verifier.events()
        )
        # RUN_STARTED tracks the controller phase only (no event), so the
        # two producer events continue exactly after the coordinator's two
        # lifecycle events: 2, 3.
        assert [event.sequence for event in emitted] == [2, 3]
        # Every producer emits through the one shared emitter object.
        assert len({id(controller.emitter), id(obs.emitter), id(verifier.emitter)}) == 1
        journal.close()

    def test_authoritative_journal_failure_stays_sticky_out_of_band_fatal(self, tmp_path):
        coordinator, journal = make_coordinator(tmp_path)
        coordinator.emit(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
        )
        journal.close()  # the durable sink breaks out from under the worker
        with pytest.raises(EmitterFatalError):
            coordinator.emit(SessionEventKind.SESSION_STARTED, {})
        assert coordinator.fatal is True
        assert coordinator.fatal_error
        # Later emissions fail fast instead of silently continuing.
        with pytest.raises(EmitterFatalError):
            coordinator.emit_status(SessionPhase.EXECUTING_TOOL)
        # The journal stays incomplete/non-successful -- never upgraded.
        read = read_session_journal(tmp_path / "session.events.jsonl")
        assert read.state is not JournalReadState.COMPLETE
        assert read.state is JournalReadState.INTERRUPTED

    def test_producer_emission_after_journal_failure_fails_fast(self, tmp_path):
        coordinator, journal = make_coordinator(tmp_path)
        coordinator.emit(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
        )
        journal.close()
        obs = observability(coordinator.emitter)
        with pytest.raises(EmitterFatalError):
            obs.patch_proposed(0, VALID_PATCH_SHA256)
        assert coordinator.fatal is True
        assert obs.events() == ()

    def test_best_effort_no_journal_producer_use_still_available(self):
        """Isolated producers without a shared authority keep working
        best-effort (private emitters), as the Task-2 observer rule expects."""
        obs = SessionObservability(
            ObservabilityContext(
                session_id=SESSION_ID,
                task_id=VALID_TASK_ID,
                source_kind=SourceKind.OFFLINE_DEMO,
                run_id=VALID_RUN_ID,
                initial_sequence=3,
            ),
            clock=lambda: FIXED,
        )
        obs.debugger_started("profile.py", [])
        obs.patch_proposed(0, VALID_PATCH_SHA256)
        assert [event.sequence for event in obs.events()] == [3, 4]
        assert obs.emitter.fatal is False

    def test_mismatched_injected_emitter_fails_closed(self, tmp_path):
        journal = SessionEventJournal(
            tmp_path / "session.events.jsonl",
            session_id=SESSION_ID,
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
        )
        foreign = SessionEventEmitter(
            session_id="different-session",
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
            clock=lambda: FIXED,
        )
        with pytest.raises(ApplicationError):
            SessionCoordinator(
                journal=journal,
                session_id=SESSION_ID,
                task_id=VALID_TASK_ID,
                source_kind=SourceKind.OFFLINE_DEMO,
                run_id=VALID_RUN_ID,
                clock=lambda: FIXED,
                emitter=foreign,
            )
        journal.close()
