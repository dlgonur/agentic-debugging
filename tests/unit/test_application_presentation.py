"""Presentation reducer purity, coverage, and parity tests."""

from __future__ import annotations

import pytest

from agentic_debugger.application import ApplicationContractError
from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    SessionStatus,
    SourceKind,
    VerifierStage,
    VerifierStageStatus,
)
from agentic_debugger.application.presentation import (
    MAX_TIMELINE_ENTRIES,
    PatchStage,
    PresentationIdentity,
    SessionViewState,
    VerifierStageView,
    initial_session_view,
    presentation_identity,
    reduce_event,
)
from agentic_debugger.agent.state_machine import ControllerState
from application_support import (
    VALID_PATCH_SHA256,
    VALID_PAYLOADS,
    VALID_RUN_ID,
    VALID_SESSION_ID,
    VALID_SPEC_FINGERPRINT,
    VALID_TASK_ID,
    make_completed_stream,
    make_event,
    make_spec,
)

VERIFIER_COMPLETED_PAYLOAD = {
    "status": "COMPLETED",
    "outcome": "RESOLVED",
    "f2p_passed": 1,
    "f2p_total": 1,
    "p2p_passed": 2,
    "p2p_total": 2,
    "workspace_cleaned": True,
}


def reduce_all(state, events):
    for event in events:
        state = reduce_event(state, event)
    return state


def state_created():
    return initial_session_view(presentation_identity(make_spec()))


def live_identity():
    return presentation_identity(make_spec())


def state_started():
    return reduce_all(
        state_created(),
        (
            make_event(
                SessionEventKind.SESSION_CREATED,
                {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
                sequence=0,
            ),
            make_event(
                SessionEventKind.SESSION_STARTED,
                {},
                sequence=1,
                run_id=VALID_RUN_ID,
            ),
        ),
    )


def state_running():
    return reduce_all(
        state_started(),
        (
            make_event(
                SessionEventKind.SESSION_STATUS_CHANGED,
                {"status": "running", "phase": "waiting_model"},
                sequence=2,
                run_id=VALID_RUN_ID,
            ),
        ),
    )


class TestInitialView:
    def test_initial_view_from_live_spec_identity(self):
        spec = make_spec()
        view = initial_session_view(presentation_identity(spec))
        assert view.task_id == spec.task_id
        assert view.source_kind is SourceKind.OFFLINE_DEMO
        assert view.status is SessionStatus.CREATED
        assert view.session_id is None
        assert view.run_id is None
        assert view.termination_reason is None
        assert view.debugger.script is None
        assert view.debugger.frames == ()
        assert view.cleanup_verified is None
        assert view.timeline == ()
        assert view.patch_attempts == ()
        assert view.verifier_stages == ()


def test_model_error_timeline_exposes_safe_concrete_reason() -> None:
    view = reduce_event(
        state_running(),
        make_event(
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            {
                "request_index": 0,
                "status": "error",
                "error_kind": "http_error",
                "error_message": "Ollama HTTP request returned status 401",
            },
            sequence=3,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.REPRODUCE,
        ),
    )
    assert view.timeline[-1].summary == (
        "model request 1 failed — http_error: "
        "Ollama HTTP request returned status 401"
    )


def replay_completed_stream():
    """A valid completed stream whose provenance is a recorded source kind."""
    events = []
    for event in make_completed_stream():
        mapping = event.to_mapping()
        mapping["source_kind"] = SourceKind.SESSION_BUNDLE.value
        events.append(SessionEvent.from_mapping(mapping))
    return tuple(events)


class TestPresentationIdentity:
    """Blocker-1 coverage: unified live/replay provenance and fail-closed binding."""

    def test_recorded_stream_preserves_recorded_provenance(self):
        identity = PresentationIdentity(
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.SESSION_BUNDLE,
            session_id=VALID_SESSION_ID,
        )
        view = reduce_all(initial_session_view(identity), replay_completed_stream())
        assert view.source_kind is SourceKind.SESSION_BUNDLE
        assert view.task_id == VALID_TASK_ID
        assert view.session_id == VALID_SESSION_ID
        assert view.status is SessionStatus.SUCCEEDED
        assert len(view.timeline) == 6

    def test_live_and_serialized_replay_reduce_identically_with_same_identity(self):
        events = make_completed_stream()
        serialized = tuple(
            SessionEvent.from_mapping(event.to_mapping()) for event in events
        )
        identity = presentation_identity(make_spec())
        live_view = reduce_all(initial_session_view(identity), events)
        replay_view = reduce_all(initial_session_view(identity), serialized)
        assert replay_view == live_view

    def test_live_and_replay_views_differ_only_in_provenance(self):
        live_view = reduce_all(
            initial_session_view(presentation_identity(make_spec())),
            make_completed_stream(),
        )
        replay_view = reduce_all(
            initial_session_view(
                PresentationIdentity(
                    task_id=VALID_TASK_ID,
                    source_kind=SourceKind.SESSION_BUNDLE,
                    session_id=VALID_SESSION_ID,
                )
            ),
            replay_completed_stream(),
        )
        assert replay_view.source_kind is SourceKind.SESSION_BUNDLE
        assert live_view.source_kind is SourceKind.OFFLINE_DEMO
        # Everything except provenance is identical.
        assert replay_view.status == live_view.status
        assert replay_view.task_id == live_view.task_id
        assert replay_view.timeline == live_view.timeline
        assert replay_view.cleanup_verified == live_view.cleanup_verified

    def test_task_id_mismatch_fails_closed(self):
        view = initial_session_view(live_identity())
        event = make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
            sequence=0,
            task_id="curated-none-handling-001",
        )
        with pytest.raises(ApplicationContractError):
            reduce_event(view, event)

    def test_source_kind_mismatch_fails_closed(self):
        view = initial_session_view(live_identity())
        event = make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
            sequence=0,
            source_kind=SourceKind.SESSION_BUNDLE,
        )
        with pytest.raises(ApplicationContractError):
            reduce_event(view, event)

    def test_session_id_mismatch_fails_closed_after_binding(self):
        identity = PresentationIdentity(
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
            session_id=VALID_SESSION_ID,
        )
        view = reduce_all(
            initial_session_view(identity),
            (make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": VALID_SPEC_FINGERPRINT}, sequence=0),),
        )
        with pytest.raises(ApplicationContractError):
            reduce_event(
                view,
                make_event(
                    SessionEventKind.SESSION_STARTED,
                    {},
                    sequence=1,
                    session_id="session-other",
                    run_id=VALID_RUN_ID,
                ),
            )

    def test_session_id_binds_from_first_event_when_unset(self):
        identity = PresentationIdentity(
            task_id=VALID_TASK_ID, source_kind=SourceKind.OFFLINE_DEMO
        )
        view = reduce_all(initial_session_view(identity), make_completed_stream())
        assert view.session_id == VALID_SESSION_ID

    def test_session_id_mismatch_fails_closed_after_implicit_binding(self):
        view = reduce_all(
            initial_session_view(live_identity()),
            (make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": VALID_SPEC_FINGERPRINT}, sequence=0),),
        )
        with pytest.raises(ApplicationContractError):
            reduce_event(
                view,
                make_event(
                    SessionEventKind.SESSION_STARTED,
                    {},
                    sequence=1,
                    session_id="session-other",
                    run_id=VALID_RUN_ID,
                ),
            )

    def test_identity_validation(self):
        with pytest.raises(Exception):
            PresentationIdentity(task_id="", source_kind=SourceKind.OFFLINE_DEMO)
        with pytest.raises(Exception):
            PresentationIdentity(
                task_id=VALID_TASK_ID, source_kind="offline_demo"  # type: ignore[arg-type]
            )
        with pytest.raises(Exception):
            PresentationIdentity(
                task_id=VALID_TASK_ID,
                source_kind=SourceKind.OFFLINE_DEMO,
                session_id="BAD!",
            )

    def test_replay_kinds_cannot_start_live_source(self):
        from agentic_debugger.application.sources import (
            ExecutionSourceSpec,
            can_start_new_session,
        )

        assert can_start_new_session(SourceKind.SESSION_BUNDLE) is False
        assert can_start_new_session(SourceKind.CANONICAL_TRAJECTORY) is False
        assert can_start_new_session(SourceKind.EXPERIMENT_EVIDENCE) is False
        with pytest.raises(Exception):
            ExecutionSourceSpec(kind=SourceKind.SESSION_BUNDLE, task_id=VALID_TASK_ID)


class TestReducerPurity:
    def test_reduce_does_not_mutate_input(self):
        state = state_running()
        before = state
        event = make_event(
            SessionEventKind.CONTROLLER_STEP,
            {"step_index": 0, "directive_kind": "action", "stop_reason": None},
            sequence=3,
            run_id=VALID_RUN_ID,
        )
        result = reduce_event(state, event)
        assert result is not state
        assert len(result.timeline) == len(before.timeline) + 1
        assert result.timeline[-1].sequence == event.sequence
        assert state.timeline == before.timeline
        assert state.status is SessionStatus.RUNNING
        assert result.status is SessionStatus.RUNNING

    def test_reduce_does_not_mutate_event(self):
        state = state_created()
        event = make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
            sequence=0,
        )
        payload_before = dict(event.payload)
        reduce_event(state, event)
        assert dict(event.payload) == payload_before
        assert dict(event.payload) == {"spec_fingerprint": VALID_SPEC_FINGERPRINT}

    def test_round_trip_parity(self):
        """Live and replay presentation must be identical by construction."""
        events = make_completed_stream()
        live_state = reduce_all(state_created(), events)
        replayed = tuple(
            SessionEvent.from_mapping(event.to_mapping()) for event in events
        )
        replay_state = reduce_all(state_created(), replayed)
        assert replay_state == live_state

    def test_reduce_returns_frozen_state(self):
        view = state_running()
        with pytest.raises(Exception):
            view.status = SessionStatus.FAILED  # type: ignore[misc]


class TestEveryKindReduces:
    @pytest.mark.parametrize("kind", list(SessionEventKind))
    def test_every_kind_reduces(self, kind):
        if kind is SessionEventKind.SESSION_STARTED:
            state = state_created()
        elif kind is SessionEventKind.SESSION_STATUS_CHANGED:
            state = state_started()
        elif kind in (
            SessionEventKind.SESSION_COMPLETED,
            SessionEventKind.SESSION_FAILED,
            SessionEventKind.SESSION_CANCELLED,
        ):
            state = state_running()
        else:
            state = state_running()
        event = make_event(kind, VALID_PAYLOADS[kind], sequence=9, run_id=VALID_RUN_ID)
        result = reduce_event(state, event)
        assert result.timeline[-1].event_kind is kind


class TestLifecycleScenarios:
    def test_completed_session_view(self):
        view = reduce_all(state_created(), make_completed_stream())
        assert view.session_id == "session-test-001"
        assert view.run_id == VALID_RUN_ID
        assert view.status is SessionStatus.SUCCEEDED
        assert view.termination_reason.value == "done"
        assert view.cleanup_verified is True
        assert view.phase is None
        assert len(view.timeline) == 6
        assert [entry.event_kind for entry in view.timeline] == [
            SessionEventKind.SESSION_CREATED,
            SessionEventKind.SESSION_STARTED,
            SessionEventKind.SESSION_STATUS_CHANGED,
            SessionEventKind.CLEANUP_STARTED,
            SessionEventKind.CLEANUP_COMPLETED,
            SessionEventKind.SESSION_COMPLETED,
        ]

    def test_cancel_sequence_view(self):
        events = (
            make_event(
                SessionEventKind.SESSION_CREATED,
                {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
                sequence=0,
            ),
            make_event(
                SessionEventKind.SESSION_STARTED,
                {},
                sequence=1,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.SESSION_STATUS_CHANGED,
                {"status": "running", "phase": "cleaning"},
                sequence=2,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.SESSION_CANCEL_REQUESTED,
                {},
                sequence=3,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.CLEANUP_STARTED,
                {},
                sequence=4,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.CLEANUP_COMPLETED,
                {"verified": True},
                sequence=5,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.SESSION_CANCELLED,
                {"status": "cancelled", "termination_reason": "cancelled"},
                sequence=6,
                run_id=VALID_RUN_ID,
            ),
        )
        view = reduce_all(state_created(), events)
        assert view.status is SessionStatus.CANCELLED
        assert view.cleanup_verified is True
        assert view.phase is None

    def test_cleanup_progress_tracking(self):
        view = state_running()
        assert view.cleanup_verified is None
        view = reduce_event(
            view,
            make_event(SessionEventKind.CLEANUP_STARTED, {}, sequence=3, run_id=VALID_RUN_ID),
        )
        assert view.cleanup_verified is False
        view = reduce_event(
            view,
            make_event(
                SessionEventKind.CLEANUP_COMPLETED,
                {"verified": False},
                sequence=4,
                run_id=VALID_RUN_ID,
            ),
        )
        assert view.cleanup_verified is False

    def test_controller_phase_tracking(self):
        view = state_running()
        assert view.controller_phase is None
        view = reduce_event(
            view,
            make_event(
                SessionEventKind.CONTROLLER_STEP,
                {"step_index": 0, "directive_kind": "action", "stop_reason": None},
                sequence=3,
                controller_phase=ControllerState.PATCH,
                run_id=VALID_RUN_ID,
            ),
        )
        assert view.controller_phase is ControllerState.PATCH
        view = reduce_event(
            view,
            make_event(
                SessionEventKind.TOOL_STARTED,
                {"tool_name": "apply_patch"},
                sequence=4,
                run_id=VALID_RUN_ID,
            ),
        )
        # Non-bearing events keep the latest controller phase.
        assert view.controller_phase is ControllerState.PATCH

    def test_illegal_transition_fails_closed(self):
        view = state_created()
        with pytest.raises(ApplicationContractError):
            reduce_event(
                view,
                make_event(
                    SessionEventKind.SESSION_COMPLETED,
                    {"status": "succeeded", "termination_reason": "done"},
                    sequence=0,
                ),
            )


class TestDebuggerView:
    def _location(self, line, pause_generation, function="main"):
        return make_event(
            SessionEventKind.DEBUGGER_LOCATION_CHANGED,
            {
                "script": "buggy.py",
                "line": line,
                "function": function,
                "pause_generation": pause_generation,
            },
            sequence=9,
            run_id=VALID_RUN_ID,
        )

    def test_not_recorded_before_debugger_events(self):
        view = state_running()
        assert view.debugger.script is None
        assert view.debugger.line is None
        assert view.debugger.function is None
        assert view.debugger.frames == ()
        assert view.debugger.locals == ()
        assert view.debugger.session_started is False

    def test_debugger_started_and_location(self):
        view = reduce_all(
            state_running(),
            (
                make_event(
                    SessionEventKind.DEBUGGER_STARTED,
                    {"script": "buggy.py", "breakpoints": ["buggy.py:12"]},
                    sequence=3,
                    run_id=VALID_RUN_ID,
                ),
                self._location(12, 1),
            ),
        )
        assert view.debugger.session_started is True
        assert view.debugger.script == "buggy.py"
        assert view.debugger.line == 12
        assert view.debugger.function == "main"
        assert view.debugger.pause_generation == 1
        assert view.debugger.breakpoints == ("buggy.py:12",)

    def test_stack_and_locals_recorded(self):
        frames = [
            {"index": 0, "function": "main", "file": "buggy.py", "line": 12, "is_current": True}
        ]
        locals_values = [{"name": "count", "summary": "3"}]
        view = reduce_all(
            state_running(),
            (
                self._location(12, 1),
                make_event(
                    SessionEventKind.DEBUGGER_STACK_OBSERVED,
                    {"pause_generation": 1, "frames": frames},
                    sequence=10,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.DEBUGGER_LOCALS_OBSERVED,
                    {"pause_generation": 1, "locals": locals_values},
                    sequence=11,
                    run_id=VALID_RUN_ID,
                ),
            ),
        )
        assert view.debugger.frames[0].function == "main"
        assert view.debugger.frames[0].is_current is True
        assert view.debugger.locals[0].name == "count"
        assert view.debugger.locals[0].summary == "3"

    def test_stale_stack_cannot_replace_newer_pause(self):
        newer_frames = [
            {"index": 0, "function": "helper", "file": "buggy.py", "line": 30, "is_current": True}
        ]
        stale_frames = [
            {"index": 0, "function": "old_frame", "file": "buggy.py", "line": 5, "is_current": True}
        ]
        view = reduce_all(
            state_running(),
            (
                self._location(30, 2),
                make_event(
                    SessionEventKind.DEBUGGER_STACK_OBSERVED,
                    {"pause_generation": 2, "frames": newer_frames},
                    sequence=10,
                    run_id=VALID_RUN_ID,
                ),
                # A stale observation for pause 1 must not replace pause-2 data.
                make_event(
                    SessionEventKind.DEBUGGER_STACK_OBSERVED,
                    {"pause_generation": 1, "frames": stale_frames},
                    sequence=11,
                    run_id=VALID_RUN_ID,
                ),
            ),
        )
        assert view.debugger.frames[0].function == "helper"
        assert view.debugger.pause_generation == 2

    def test_same_pause_observation_replaces(self):
        first = [
            {"index": 0, "function": "first", "file": "buggy.py", "line": 1, "is_current": True}
        ]
        second = [
            {"index": 0, "function": "second", "file": "buggy.py", "line": 2, "is_current": True}
        ]
        view = reduce_all(
            state_running(),
            (
                make_event(
                    SessionEventKind.DEBUGGER_STACK_OBSERVED,
                    {"pause_generation": 1, "frames": first},
                    sequence=9,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.DEBUGGER_STACK_OBSERVED,
                    {"pause_generation": 1, "frames": second},
                    sequence=10,
                    run_id=VALID_RUN_ID,
                ),
            ),
        )
        assert view.debugger.frames[0].function == "second"


class TestPatchView:
    def test_proposed_then_verified(self):
        events = (
            make_event(
                SessionEventKind.PATCH_PROPOSED,
                {"attempt_index": 0, "patch_sha256": VALID_PATCH_SHA256},
                sequence=3,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.PATCH_APPLIED,
                {
                    "attempt_index": 0,
                    "changed_files": ["buggy.py"],
                    "syntax_passed": True,
                },
                sequence=4,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.VERIFIER_COMPLETED,
                VERIFIER_COMPLETED_PAYLOAD,
                sequence=5,
                run_id=VALID_RUN_ID,
            ),
        )
        view = reduce_all(state_running(), events)
        assert len(view.patch_attempts) == 1
        attempt = view.patch_attempts[0]
        assert attempt.stage is PatchStage.VERIFIED
        assert attempt.patch_sha256 == VALID_PATCH_SHA256
        assert attempt.changed_files == ("buggy.py",)
        assert attempt.syntax_passed is True
        assert view.verifier_summary is not None
        assert view.verifier_summary.outcome.value == "RESOLVED"
        assert view.verifier_summary.f2p_passed == 1

    def test_proposed_then_rejected(self):
        view = reduce_all(
            state_running(),
            (
                make_event(
                    SessionEventKind.PATCH_PROPOSED,
                    {"attempt_index": 0, "patch_sha256": VALID_PATCH_SHA256},
                    sequence=3,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.PATCH_REJECTED,
                    {"attempt_index": 0, "rejection_reason": "malformed_diff"},
                    sequence=4,
                    run_id=VALID_RUN_ID,
                ),
            ),
        )
        assert view.patch_attempts[0].stage is PatchStage.REJECTED
        assert view.patch_attempts[0].rejection_reason == "malformed_diff"

    def test_verifier_execution_proven_is_preserved_when_present(self):
        view = reduce_all(
            state_running(),
            (
                make_event(
                    SessionEventKind.VERIFIER_COMPLETED,
                    {**VERIFIER_COMPLETED_PAYLOAD, "official_test_execution_proven": True},
                    sequence=3,
                    run_id=VALID_RUN_ID,
                ),
            ),
        )
        assert view.verifier_summary is not None
        assert view.verifier_summary.official_test_execution_proven is True

    def test_applied_then_reverted(self):
        view = reduce_all(
            state_running(),
            (
                make_event(
                    SessionEventKind.PATCH_APPLIED,
                    {
                        "attempt_index": 0,
                        "changed_files": ["buggy.py"],
                        "syntax_passed": None,
                    },
                    sequence=3,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.PATCH_REVERTED,
                    {"attempt_index": 0},
                    sequence=4,
                    run_id=VALID_RUN_ID,
                ),
            ),
        )
        assert view.patch_attempts[0].stage is PatchStage.REVERTED

    def test_verifier_marks_latest_applied_attempt(self):
        events = (
            make_event(
                SessionEventKind.PATCH_APPLIED,
                {"attempt_index": 0, "changed_files": ["a.py"], "syntax_passed": None},
                sequence=3,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.PATCH_APPLIED,
                {"attempt_index": 1, "changed_files": ["b.py"], "syntax_passed": None},
                sequence=4,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.VERIFIER_COMPLETED,
                VERIFIER_COMPLETED_PAYLOAD,
                sequence=5,
                run_id=VALID_RUN_ID,
            ),
        )
        view = reduce_all(state_running(), events)
        stages = {item.attempt_index: item.stage for item in view.patch_attempts}
        assert stages[0] is PatchStage.APPLIED
        assert stages[1] is PatchStage.VERIFIED

    def test_uncompleted_verifier_does_not_verify(self):
        payload = dict(VERIFIER_COMPLETED_PAYLOAD)
        payload["status"] = "BASELINE_INVALID"
        payload["outcome"] = None
        view = reduce_all(
            state_running(),
            (
                make_event(
                    SessionEventKind.PATCH_APPLIED,
                    {"attempt_index": 0, "changed_files": ["a.py"], "syntax_passed": None},
                    sequence=3,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.VERIFIER_COMPLETED,
                    payload,
                    sequence=4,
                    run_id=VALID_RUN_ID,
                ),
            ),
        )
        assert view.patch_attempts[0].stage is PatchStage.APPLIED

    def test_verifier_stage_progress(self):
        events = (
            make_event(SessionEventKind.VERIFIER_STARTED, {}, sequence=3, run_id=VALID_RUN_ID),
            make_event(
                SessionEventKind.VERIFIER_STAGE_STARTED,
                {"stage": "prepare_workspace"},
                sequence=4,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.VERIFIER_STAGE_COMPLETED,
                {"stage": "prepare_workspace", "status": "completed"},
                sequence=5,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.VERIFIER_STAGE_STARTED,
                {"stage": "classification"},
                sequence=6,
                run_id=VALID_RUN_ID,
            ),
        )
        view = reduce_all(state_running(), events)
        assert view.verifier_stages == (
            VerifierStageView(
                stage=VerifierStage.PREPARE_WORKSPACE,
                status=VerifierStageStatus.COMPLETED,
            ),
            VerifierStageView(
                stage=VerifierStage.CLASSIFICATION,
                status=VerifierStageStatus.RUNNING,
            ),
        )


class TestTimeline:
    def test_timeline_is_bounded_tail(self):
        state = state_running()
        for index in range(MAX_TIMELINE_ENTRIES + 50):
            state = reduce_event(
                state,
                make_event(
                    SessionEventKind.TOOL_STARTED,
                    {"tool_name": "run_tests"},
                    sequence=100 + index,
                    run_id=VALID_RUN_ID,
                ),
            )
        assert len(state.timeline) == MAX_TIMELINE_ENTRIES
        assert state.timeline[0].sequence == 100 + 50
        assert state.timeline[-1].sequence == 100 + MAX_TIMELINE_ENTRIES + 49

    def test_timeline_summaries_bounded(self):
        view = reduce_all(
            state_running(),
            (
                make_event(
                    SessionEventKind.TOOL_STARTED,
                    {"tool_name": "t" * 256},
                    sequence=3,
                    run_id=VALID_RUN_ID,
                ),
            ),
        )
        assert len(view.timeline[-1].summary) <= 240
        assert view.timeline[-1].summary.endswith("...")


class TestStructuredOperationFacts:
    """Reducer coverage for the typed structured-operation refinements."""

    def test_tool_events_carry_and_replace_typed_target(self):
        state = state_started()
        state = reduce_event(
            state,
            make_event(SessionEventKind.TOOL_STARTED, {"tool_name": "get_source_window"}, sequence=2),
        )
        assert state.current_tool_name == "get_source_window"
        assert state.current_tool_target is None
        state = reduce_event(
            state,
            make_event(
                SessionEventKind.TOOL_COMPLETED,
                {"tool_name": "get_source_window", "status": "ok", "target": "cookiecutter/config.py:40-80"},
                sequence=3,
            ),
        )
        assert state.current_tool_name is None
        assert state.current_tool_target == "cookiecutter/config.py:40-80"
        # A later tool boundary replaces the target with its own.
        state = reduce_event(
            state,
            make_event(SessionEventKind.TOOL_STARTED, {"tool_name": "run_tests"}, sequence=4),
        )
        assert state.current_tool_target is None

    def test_operator_progress_official_execution_proven_is_typed_and_sticky(self):
        state = state_started()
        state = reduce_event(
            state,
            make_event(
                SessionEventKind.OPERATOR_PROGRESS,
                {"stage": "official_evaluator_completed", "detail": "official execution proven", "official_execution_proven": True},
                sequence=2,
            ),
        )
        assert state.official_execution_proven is True
        # A later plain progress event never clears the proven fact.
        state = reduce_event(
            state,
            make_event(SessionEventKind.OPERATOR_PROGRESS, {"stage": "cleanup"}, sequence=3),
        )
        assert state.official_execution_proven is True
        assert state.operator_stage.value == "cleanup"

    def test_verifier_completed_sets_official_execution_from_authoritative_result(self):
        state = state_started()
        state = reduce_event(
            state,
            make_event(
                SessionEventKind.VERIFIER_COMPLETED,
                {**VERIFIER_COMPLETED_PAYLOAD, "official_test_execution_proven": False},
                sequence=2,
            ),
        )
        assert state.official_execution_proven is False
