"""BLOCKER 1: one authoritative SessionEvent sequence across all producers.

Proves that the controller adapter, the debugger/source/patch observability
producer, and the verifier adapter compose through one shared
:class:`SessionEventEmitter` into a single contiguous sequence (``0..N``)
that feeds one Task-3 durable journal, that no producer guesses another
producer's next sequence, that a journal failure cannot disappear silently
from the session owner's observable state, and that best-effort
observability without a journal stays non-invasive.
"""

from __future__ import annotations

import pytest

from agentic_debugger.application import ApplicationContractError, ApplicationInputError
from agentic_debugger.application.controller_adapter import (
    ControllerObservationContext,
    ControllerSessionEventAdapter,
)
from agentic_debugger.application.emitter import (
    EmitterFatalError,
    SessionEventEmitter,
)
from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
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
from agentic_debugger.application.verifier_observer import VerifierSessionEventAdapter
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
    make_event,
)

FIXED = "2026-08-14T08:00:00Z"
SESSION_ID = "sess-emitter-001"


def make_emitter(*, sink=None, run_id=None, initial_sequence=0):
    return SessionEventEmitter(
        session_id=SESSION_ID,
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
        run_id=run_id,
        clock=lambda: FIXED,
        sink=sink,
        initial_sequence=initial_sequence,
    )


def controller_adapter(emitter=None, *, run_id=VALID_RUN_ID, initial_sequence=0):
    context = ControllerObservationContext(
        session_id=SESSION_ID,
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
        run_id=run_id,
        initial_sequence=initial_sequence,
    )
    if emitter is not None:
        return ControllerSessionEventAdapter(context, emitter=emitter)
    return ControllerSessionEventAdapter(context, clock=lambda: FIXED)


def observability(emitter=None, *, run_id=VALID_RUN_ID, initial_sequence=0):
    context = ObservabilityContext(
        session_id=SESSION_ID,
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
        run_id=run_id,
        initial_sequence=initial_sequence,
    )
    if emitter is not None:
        return SessionObservability(context, emitter=emitter)
    return SessionObservability(context, clock=lambda: FIXED)


def verifier_adapter(emitter=None, *, run_id=VALID_RUN_ID, initial_sequence=0):
    context = ObservabilityContext(
        session_id=SESSION_ID,
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
        run_id=run_id,
        initial_sequence=initial_sequence,
    )
    if emitter is not None:
        return VerifierSessionEventAdapter(context, emitter=emitter)
    return VerifierSessionEventAdapter(context, clock=lambda: FIXED)


def observation(kind, **fields):
    values = {"run_id": VALID_RUN_ID, "task_id": VALID_TASK_ID}
    values.update(fields)
    return ControllerObservation(kind=kind, **values)


class FailingSink:
    """Task-1 sink whose append always fails (a broken journal)."""

    def __init__(self):
        self.append_calls = 0

    def append(self, event):
        self.append_calls += 1
        raise OSError("disk full (injected)")

    def flush(self):
        pass

    def close(self):
        pass


class TestSharedSequenceAuthority:
    def test_producers_interleave_through_one_shared_authority(self):
        """Controller + debugger + verifier events interleave with sequences
        0..N through one shared emitter, including nested tool emission."""
        emitter = make_emitter()
        controller = controller_adapter(emitter)
        obs = observability(emitter)
        verifier = verifier_adapter(emitter)

        controller.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            state_before=ControllerState.REPRODUCE,
        ))
        controller.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        # Nested: a tool starts, the debugger observes pause data while the
        # tool is still active, then the tool completes.
        controller.notify(observation(
            ControllerObservationKind.TOOL_STARTED,
            model_call_index=0,
            tool_name="start_pdb_session",
            state_before=ControllerState.REPRODUCE,
        ))
        obs.debugger_started("profile.py", ["profile.py:12"])
        obs.location_changed("profile.py", 12, "format_display_name", 1)
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
            tool_name="start_pdb_session",
            observation_status=ObservationStatus.OK,
            state_before=ControllerState.REPRODUCE,
        ))
        controller.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            model_call_index=0,
            request_status="ok",
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

        all_events = (
            controller.events() + obs.events() + verifier.events()
        )
        sequences = sorted(event.sequence for event in all_events)
        # One authoritative sequence space: contiguous 0..N, no duplicates.
        assert sequences == list(range(len(sequences)))
        assert len(set(sequences)) == len(sequences)
        # The emitter assigned every sequence (no producer guessed another's).
        assert emitter.last_sequence == len(sequences) - 1

    def test_all_producers_feed_one_task3_journal(self, tmp_path):
        """One shared emitter backed by a real Task-3 journal produces a
        complete contiguous stream the journal classifies as complete."""
        journal = SessionEventJournal(
            tmp_path / "session.events.jsonl",
            session_id=SESSION_ID,
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
        )
        emitter = make_emitter(sink=journal)
        controller = controller_adapter(emitter)
        obs = observability(emitter)
        verifier = verifier_adapter(emitter)

        # The session owner emits the lifecycle events through the same
        # authority (sequence 0..) and binds the run identity at
        # ``session.started`` (events before it carry null), then the
        # producers interleave.
        emitter.emit(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
        )
        emitter.bind_run_id(VALID_RUN_ID)
        emitter.emit(SessionEventKind.SESSION_STARTED, {})
        emitter.emit(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": "executing_tool"},
        )
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
        verifier.started()
        verifier.stage_started("prepare_workspace")
        verifier.stage_completed("prepare_workspace", "completed")
        emitter.emit(SessionEventKind.CLEANUP_STARTED, {})
        emitter.emit(SessionEventKind.CLEANUP_COMPLETED, {"verified": True})
        emitter.emit(
            SessionEventKind.SESSION_COMPLETED,
            {"status": "succeeded", "termination_reason": "done"},
        )
        journal.close()

        read = read_session_journal(tmp_path / "session.events.jsonl")
        assert read.state is JournalReadState.COMPLETE
        sequences = [event.sequence for event in read.events]
        assert sequences == list(range(len(read.events)))
        # Every producer's event reached the same journal without gaps.
        produced = len(controller.events()) + len(obs.events()) + len(verifier.events())
        assert len(read.events) == 6 + produced

    def test_standalone_producers_still_possible(self):
        """Without a shared emitter each producer keeps working (private
        emitters starting at their context's initial_sequence)."""
        obs = observability(initial_sequence=3)
        obs.location_changed("profile.py", 1, "f", 1)
        obs.patch_proposed(0, VALID_PATCH_SHA256)
        assert [e.sequence for e in obs.events()] == [3, 4]
        verifier = verifier_adapter(initial_sequence=10)
        verifier.started()
        assert verifier.events()[0].sequence == 10

    def test_shared_emitter_identity_mismatch_fails_closed(self):
        emitter = SessionEventEmitter(
            session_id="different-session",
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
            clock=lambda: FIXED,
        )
        with pytest.raises(ApplicationContractError):
            controller_adapter(emitter)
        with pytest.raises(ApplicationContractError):
            observability(emitter)
        with pytest.raises(ApplicationContractError):
            verifier_adapter(emitter)

    def test_shared_emitter_run_id_conflict_fails_closed(self):
        emitter = make_emitter(run_id="run-other-999")
        with pytest.raises(ApplicationContractError):
            observability(emitter)

    def test_clock_or_sink_with_shared_emitter_rejected(self):
        emitter = make_emitter()
        with pytest.raises(ApplicationInputError):
            SessionObservability(
                ObservabilityContext(
                    session_id=SESSION_ID,
                    task_id=VALID_TASK_ID,
                    source_kind=SourceKind.OFFLINE_DEMO,
                    run_id=VALID_RUN_ID,
                ),
                clock=lambda: FIXED,
                emitter=emitter,
            )

    def test_bind_run_id_conflict_fails_closed(self):
        emitter = make_emitter(run_id=VALID_RUN_ID)
        with pytest.raises(ApplicationContractError):
            emitter.bind_run_id("run-other-999")
        # Rebinding the same value is a no-op.
        emitter.bind_run_id(VALID_RUN_ID)
        assert emitter.run_id == VALID_RUN_ID

    def test_no_journal_observability_non_invasive(self):
        """Best-effort production with no journal stays fully functional."""
        emitter = make_emitter(sink=None)
        obs = observability(emitter)
        obs.debugger_started("profile.py", [])
        obs.patch_proposed(0, VALID_PATCH_SHA256)
        assert [e.sequence for e in obs.events()] == [0, 1]
        assert emitter.fatal is False


class TestJournalFailureVisibility:
    def test_sink_failure_marks_fatal_and_raises(self):
        sink = FailingSink()
        emitter = make_emitter(sink=sink)
        with pytest.raises(EmitterFatalError):
            emitter.emit(
                SessionEventKind.SESSION_CREATED,
                {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
            )
        assert emitter.fatal is True
        assert emitter.fatal_error
        # The rejected event did not advance the sequence.
        assert emitter.next_sequence == 0

    def test_failure_cannot_disappear_even_when_swallowed(self):
        """The Task-2/verifier observer layers swallow ordinary exceptions;
        the sticky fatal state must remain observable to the session owner."""
        sink = FailingSink()
        emitter = make_emitter(sink=sink)
        try:
            emitter.emit(
                SessionEventKind.SESSION_CREATED,
                {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
            )
        except Exception:
            pass  # an observer layer swallowed the first failure
        # The session owner can still detect the journal failure honestly.
        assert emitter.fatal is True
        assert "disk full" in (emitter.fatal_error or "")
        # And later emissions fail fast instead of silently continuing.
        with pytest.raises(EmitterFatalError):
            emitter.emit(SessionEventKind.SESSION_STARTED, {})

    def test_producer_emissions_after_sink_failure_fail_fast(self):
        sink = FailingSink()
        emitter = make_emitter(sink=sink)
        obs = observability(emitter)
        with pytest.raises(EmitterFatalError):
            obs.patch_proposed(0, VALID_PATCH_SHA256)
        with pytest.raises(EmitterFatalError):
            obs.patch_applied(0, ["x.py"])
        assert emitter.fatal is True
        assert obs.events() == ()
