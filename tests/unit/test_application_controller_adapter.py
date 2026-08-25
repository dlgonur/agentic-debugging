"""Focused tests for the Task-2 controller-to-SessionEvent adapter.

Covers: valid Task-1 SessionEvent production for controller-owned facts,
incremental-prefix semantics (no session lifecycle, cleanup, terminal, or
verifier fabrication), fail-closed identity validation, sequence ownership,
sink forwarding, deterministic clocks, and prefix composability into a
complete stream when later tasks supply lifecycle events.
"""

from __future__ import annotations

import pytest

from agentic_debugger.application import (
    ApplicationContractError,
    ApplicationInputError,
)
from agentic_debugger.application.controller_adapter import (
    ControllerObservationContext,
    ControllerSessionEventAdapter,
)
from agentic_debugger.application.events import (
    SessionEventKind,
    SourceKind,
    validate_session_event_stream,
)
from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisConfidence,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    ControllerSnapshot,
    ScriptedModelAdapter,
    ScriptedModelStep,
    TransitionDirective,
)
from agentic_debugger.agent.observer import (
    ControllerObservation,
    ControllerObservationKind,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry, ToolResult, ToolSpec
from agentic_debugger.events.schema import ObservationStatus
from application_support import (
    VALID_SPEC_FINGERPRINT,
    make_event,
)

FIXED_CLOCK = lambda: "2026-08-14T08:00:00Z"  # noqa: E731

SESSION_ID = "session-task2-001"
TASK_ID = "curated-off-by-one-002"
RUN_ID = "run-task2-001"


def _compose_complete_stream(events):
    """Wrap an adapter prefix with lifecycle events into a complete stream.

    ``session.created`` (0), ``session.started`` (1) binding ``RUN_ID``,
    ``session.status_changed`` running (2), the adapter events, then the
    terminal cleanup cycle and ``session.completed``.
    """
    return (
        make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
            sequence=0,
            session_id=SESSION_ID,
            task_id=TASK_ID,
            run_id=None,
        ),
        make_event(
            SessionEventKind.SESSION_STARTED,
            {},
            sequence=1,
            session_id=SESSION_ID,
            task_id=TASK_ID,
            run_id=RUN_ID,
        ),
        make_event(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": "waiting_model"},
            sequence=2,
            session_id=SESSION_ID,
            task_id=TASK_ID,
            run_id=RUN_ID,
        ),
    ) + tuple(events) + (
        make_event(
            SessionEventKind.CLEANUP_STARTED,
            {},
            sequence=3 + len(events),
            session_id=SESSION_ID,
            task_id=TASK_ID,
            run_id=RUN_ID,
        ),
        make_event(
            SessionEventKind.CLEANUP_COMPLETED,
            {"verified": True},
            sequence=4 + len(events),
            session_id=SESSION_ID,
            task_id=TASK_ID,
            run_id=RUN_ID,
        ),
        make_event(
            SessionEventKind.SESSION_COMPLETED,
            {"status": "succeeded", "termination_reason": "done"},
            sequence=5 + len(events),
            session_id=SESSION_ID,
            task_id=TASK_ID,
            run_id=RUN_ID,
        ),
    )

LIFECYCLE_KINDS = frozenset(
    {
        SessionEventKind.SESSION_CREATED,
        SessionEventKind.SESSION_STARTED,
        SessionEventKind.SESSION_STATUS_CHANGED,
        SessionEventKind.SESSION_CANCEL_REQUESTED,
        SessionEventKind.SESSION_COMPLETED,
        SessionEventKind.SESSION_FAILED,
        SessionEventKind.SESSION_CANCELLED,
        SessionEventKind.CLEANUP_STARTED,
        SessionEventKind.CLEANUP_COMPLETED,
        SessionEventKind.VERIFIER_STARTED,
        SessionEventKind.VERIFIER_STAGE_STARTED,
        SessionEventKind.VERIFIER_STAGE_COMPLETED,
        SessionEventKind.VERIFIER_COMPLETED,
        SessionEventKind.DEBUGGER_STARTED,
        SessionEventKind.DEBUGGER_LOCATION_CHANGED,
        SessionEventKind.DEBUGGER_STACK_OBSERVED,
        SessionEventKind.DEBUGGER_LOCALS_OBSERVED,
        SessionEventKind.PATCH_PROPOSED,
        SessionEventKind.PATCH_REJECTED,
        SessionEventKind.PATCH_APPLIED,
        SessionEventKind.PATCH_REVERTED,
        SessionEventKind.ARTIFACT_WRITTEN,
    }
)


def context(*, run_id=RUN_ID, initial_sequence=0, task_id=TASK_ID):
    return ControllerObservationContext(
        session_id=SESSION_ID,
        task_id=task_id,
        source_kind=SourceKind.OFFLINE_DEMO,
        run_id=run_id,
        initial_sequence=initial_sequence,
    )


def observation(kind, **fields):
    values = {"run_id": RUN_ID, "task_id": TASK_ID}
    values.update(fields)
    return ControllerObservation(kind=kind, **values)


def adapter_for(*, run_id=RUN_ID, initial_sequence=0, sink=None, clock=FIXED_CLOCK):
    return ControllerSessionEventAdapter(
        context(run_id=run_id, initial_sequence=initial_sequence),
        clock=clock,
        sink=sink,
    )


def run_controller(adapter):
    limits = ControllerBudgetLimits(2, 3, 2, max_active_hypotheses=2, max_source_observations=3)

    def handler(action, arguments):
        return ToolResult(ObservationStatus.OK, {"received": arguments}, "ok")

    def validator(arguments):
        return arguments

    registry = ToolRegistry(tuple(
        ToolSpec(name, validator, handler)
        for name in (ActionName.RUN_TESTS,)
    ))
    steps = tuple(
        ScriptedModelStep(state, directive)
        for state, directive in (
            (ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {})),
            (ControllerState.REPRODUCE, TransitionDirective(ControllerState.UNDERSTAND, "proceed")),
            (ControllerState.UNDERSTAND, TransitionDirective(ControllerState.PATCH, "candidate")),
            (ControllerState.PATCH, TransitionDirective(ControllerState.VALIDATE, "applied")),
            (ControllerState.VALIDATE, TransitionDirective(ControllerState.DONE, "resolved")),
        )
    )
    controller = DeterministicController(
        registry,
        ScriptedModelAdapter(steps),
        ControllerRunConfig(max_model_calls=16),
        observer=adapter,
    )
    return controller.run(
        ControllerSnapshot(
            RUN_ID, TASK_ID, ControllerState.REPRODUCE, 0,
            limits, ControllerBudgetState(), HypothesisLedger(),
        )
    )


class RecordingSink:
    def __init__(self):
        self.events = []
        self.closed = False

    def append(self, event):
        self.events.append(event)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class TestMapping:
    def test_request_boundaries_mapped(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            request_status="ok",
        ))
        events = adapter.events()
        assert [event.event_kind for event in events] == [
            SessionEventKind.MODEL_REQUEST_STARTED,
            SessionEventKind.MODEL_REQUEST_COMPLETED,
        ]
        assert dict(events[0].payload) == {"request_index": 0}
        assert dict(events[1].payload) == {"request_index": 0, "status": "ok"}
        assert [event.sequence for event in events] == [0, 1]
        assert all(event.run_id == RUN_ID for event in events)
        assert all(event.session_id == SESSION_ID for event in events)
        assert all(event.task_id == TASK_ID for event in events)
        assert all(event.controller_phase is ControllerState.REPRODUCE for event in events)

    def test_error_request_status_mapped(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            request_status="error",
        ))
        assert dict(adapter.events()[-1].payload) == {"request_index": 0, "status": "error"}

    def test_safe_model_error_detail_is_durably_mapped(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            request_status="error",
            error_kind="http_error",
            error_message="Ollama HTTP request returned status 401",
        ))
        assert dict(adapter.events()[-1].payload) == {
            "request_index": 0,
            "status": "error",
            "error_kind": "http_error",
            "error_message": "Ollama HTTP request returned status 401",
        }

    def test_credential_shaped_model_error_detail_is_replaced(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            request_status="error",
            error_kind="http_error",
            error_message="token=must-not-survive",
        ))
        payload = dict(adapter.events()[-1].payload)
        assert payload["error_kind"] == "model_error"
        assert payload["error_message"] == (
            "model request failed; sensitive detail was removed"
        )
        assert "must-not-survive" not in str(payload)

    def test_directive_accepted_action_mapped(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.DIRECTIVE_ACCEPTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            directive_kind="action",
            tool_name="run_tests",
        ))
        event = adapter.events()[0]
        assert event.event_kind is SessionEventKind.MODEL_DIRECTIVE_ACCEPTED
        assert dict(event.payload) == {
            "directive_kind": "action",
            "action_name": "run_tests",
            "target_state": None,
        }

    def test_directive_accepted_transition_mapped(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.DIRECTIVE_ACCEPTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            directive_kind="transition",
            target_state=ControllerState.UNDERSTAND,
        ))
        event = adapter.events()[0]
        assert dict(event.payload) == {
            "directive_kind": "transition",
            "action_name": None,
            "target_state": "Understand",
        }

    def test_directive_rejected_mapped(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.DIRECTIVE_REJECTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            directive_kind="action",
            rejection_category="state_action_not_allowed",
        ))
        event = adapter.events()[0]
        assert event.event_kind is SessionEventKind.MODEL_DIRECTIVE_REJECTED
        assert dict(event.payload) == {
            "directive_kind": "action",
            "rejection_category": "state_action_not_allowed",
        }

    def test_tool_boundaries_mapped(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.TOOL_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            tool_name="run_tests",
        ))
        adapter.notify(observation(
            ControllerObservationKind.TOOL_COMPLETED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            tool_name="run_tests",
            observation_status=ObservationStatus.OK,
        ))
        events = adapter.events()
        assert [event.event_kind for event in events] == [
            SessionEventKind.TOOL_STARTED,
            SessionEventKind.TOOL_COMPLETED,
        ]
        assert dict(events[0].payload) == {"tool_name": "run_tests"}
        assert dict(events[1].payload) == {"tool_name": "run_tests", "status": "ok"}

    def test_tool_error_status_mapped(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.TOOL_COMPLETED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            tool_name="run_tests",
            observation_status=ObservationStatus.ERROR,
        ))
        assert dict(adapter.events()[0].payload) == {
            "tool_name": "run_tests",
            "status": "error",
        }

    def test_step_completed_mapped(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.STEP_COMPLETED,
            model_call_index=0,
            step_index=0,
            state_before=ControllerState.REPRODUCE,
            state_after=ControllerState.REPRODUCE,
            directive_kind="action",
        ))
        event = adapter.events()[0]
        assert event.event_kind is SessionEventKind.CONTROLLER_STEP
        assert dict(event.payload) == {
            "step_index": 0,
            "directive_kind": "action",
            "stop_reason": None,
        }

    def test_terminal_step_carries_stop_reason(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.VALIDATE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.STEP_COMPLETED,
            model_call_index=0,
            step_index=0,
            state_before=ControllerState.VALIDATE,
            state_after=ControllerState.DONE,
            directive_kind="transition",
            stop_reason="done",
        ))
        assert adapter.events()[0].payload["stop_reason"] == "done"

    def test_non_event_kinds_only_track_phase(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.STATE_TRANSITION,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            state_after=ControllerState.UNDERSTAND,
            transition_reason="moved to understanding",
        ))
        adapter.notify(observation(
            ControllerObservationKind.TERMINAL,
            model_call_index=0,
            state_after=ControllerState.UNDERSTAND,
            stop_reason="done",
        ))
        # Task-4 promotes STATE_TRANSITION into a controller.transition event;
        # RUN_STARTED and TERMINAL still only track the controller phase.
        assert len(adapter.events()) == 1
        event = adapter.events()[0]
        assert event.event_kind is SessionEventKind.CONTROLLER_TRANSITION
        assert dict(event.payload) == {
            "source_state": "Reproduce",
            "target_state": "Understand",
            "reason": "moved to understanding",
        }


class TestPrefixSemantics:
    def test_no_session_lifecycle_kinds_fabricated(self):
        adapter = adapter_for()
        run_controller(adapter)
        assert adapter.events()
        produced = {event.event_kind for event in adapter.events()}
        assert produced & LIFECYCLE_KINDS == set()
        assert produced == {
            SessionEventKind.MODEL_REQUEST_STARTED,
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            SessionEventKind.MODEL_DIRECTIVE_ACCEPTED,
            SessionEventKind.TOOL_STARTED,
            SessionEventKind.TOOL_COMPLETED,
            SessionEventKind.CONTROLLER_STEP,
            SessionEventKind.CONTROLLER_TRANSITION,
        }

    def test_terminal_outcome_produces_no_session_terminal(self):
        adapter = adapter_for()
        result = run_controller(adapter)
        assert result.final_state is ControllerState.DONE
        assert SessionEventKind.SESSION_COMPLETED not in {
            event.event_kind for event in adapter.events()
        }
        assert SessionEventKind.SESSION_FAILED not in {
            event.event_kind for event in adapter.events()
        }

    def test_full_controller_run_prefix_is_valid_and_composable(self):
        adapter = adapter_for(initial_sequence=3)
        run_controller(adapter)
        events = adapter.events()
        assert [event.sequence for event in events] == list(
            range(3, 3 + len(events))
        )
        assert [event.event_kind for event in events] == [
            SessionEventKind.MODEL_REQUEST_STARTED,
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            SessionEventKind.MODEL_DIRECTIVE_ACCEPTED,
            SessionEventKind.TOOL_STARTED,
            SessionEventKind.TOOL_COMPLETED,
            SessionEventKind.CONTROLLER_STEP,
            SessionEventKind.MODEL_REQUEST_STARTED,
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            SessionEventKind.MODEL_DIRECTIVE_ACCEPTED,
            SessionEventKind.CONTROLLER_TRANSITION,
            SessionEventKind.CONTROLLER_STEP,
            SessionEventKind.MODEL_REQUEST_STARTED,
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            SessionEventKind.MODEL_DIRECTIVE_ACCEPTED,
            SessionEventKind.CONTROLLER_TRANSITION,
            SessionEventKind.CONTROLLER_STEP,
            SessionEventKind.MODEL_REQUEST_STARTED,
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            SessionEventKind.MODEL_DIRECTIVE_ACCEPTED,
            SessionEventKind.CONTROLLER_TRANSITION,
            SessionEventKind.CONTROLLER_STEP,
            SessionEventKind.MODEL_REQUEST_STARTED,
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            SessionEventKind.MODEL_DIRECTIVE_ACCEPTED,
            SessionEventKind.CONTROLLER_TRANSITION,
            SessionEventKind.CONTROLLER_STEP,
        ]

        stream = _compose_complete_stream(events)
        validate_session_event_stream(stream)

    def test_bound_run_prefix_composes_with_lifecycle_events(self):
        # Blocker-3 repair: without a declared run_id the adapter binds the
        # authoritative run identity from RUN_STARTED and every produced
        # event carries it, so the prefix composes with lifecycle events
        # that bind the same run id at session.started.
        adapter = adapter_for(run_id=None, initial_sequence=3)
        run_controller(adapter)
        events = adapter.events()
        assert events
        assert all(event.run_id == RUN_ID for event in events)
        stream = _compose_complete_stream(events)
        validate_session_event_stream(stream)

    def test_prefix_alone_is_not_a_complete_stream(self):
        adapter = adapter_for()
        run_controller(adapter)
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(adapter.events())


class TestIdentityFailClosed:
    def test_task_id_mismatch_fails_closed(self):
        adapter = adapter_for()
        with pytest.raises(ApplicationContractError):
            adapter.notify(observation(
                ControllerObservationKind.RUN_STARTED,
                model_call_index=0,
                state_before=ControllerState.REPRODUCE,
                task_id="other-task",
            ))

    def test_run_id_mismatch_fails_closed(self):
        adapter = adapter_for()
        with pytest.raises(ApplicationContractError):
            adapter.notify(observation(
                ControllerObservationKind.RUN_STARTED,
                model_call_index=0,
                state_before=ControllerState.REPRODUCE,
                run_id="other-run",
            ))

    def test_run_binding_requires_run_started_first(self):
        adapter = adapter_for(run_id=None)
        with pytest.raises(ApplicationContractError):
            adapter.notify(observation(
                ControllerObservationKind.MODEL_REQUEST_STARTED,
                model_call_index=0,
                state_before=ControllerState.REPRODUCE,
            ))

    def test_declared_run_id_stamped_on_events_unchanged(self):
        adapter = adapter_for(run_id=RUN_ID)
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            request_status="ok",
        ))
        assert adapter.events()
        assert all(event.run_id == RUN_ID for event in adapter.events())

    def test_run_bound_from_authoritative_run_started(self):
        adapter = adapter_for(run_id=None)
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            request_status="ok",
        ))
        adapter.notify(observation(
            ControllerObservationKind.TOOL_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
            tool_name="run_tests",
        ))
        events = adapter.events()
        assert events
        # The first subsequently mapped event carries the bound non-null
        # run id, and every later event carries the same one.
        assert events[0].run_id == RUN_ID
        assert all(event.run_id == RUN_ID for event in events)
        with pytest.raises(ApplicationContractError):
            adapter.notify(observation(
                ControllerObservationKind.TOOL_COMPLETED,
                model_call_index=0,
                state_before=ControllerState.REPRODUCE,
                tool_name="run_tests",
                observation_status=ObservationStatus.OK,
                run_id="other-run",
            ))


class TestSequenceAndSink:
    def test_sequence_is_owned_by_context(self):
        adapter = adapter_for(initial_sequence=7)
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        adapter.notify(observation(
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        assert [event.sequence for event in adapter.events()] == [7]

    def test_sink_forwarding_in_order(self):
        sink = RecordingSink()
        adapter = adapter_for(sink=sink)
        run_controller(adapter)
        assert sink.events == list(adapter.events())
        assert not sink.closed

    def test_fixed_clock_is_deterministic(self):
        first = adapter_for(clock=FIXED_CLOCK)
        second = adapter_for(clock=FIXED_CLOCK)
        run_controller(first)
        run_controller(second)
        assert first.events() == second.events()
        assert all(
            event.timestamp_utc == "2026-08-14T08:00:00Z"
            for event in first.events()
        )


class TestFailClosedVocabulary:
    def test_unknown_directive_kind_fails_closed(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        with pytest.raises(ApplicationContractError):
            adapter.notify(observation(
                ControllerObservationKind.DIRECTIVE_ACCEPTED,
                model_call_index=0,
                state_before=ControllerState.REPRODUCE,
                directive_kind="bogus",
            ))

    def test_unknown_stop_reason_fails_closed(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        with pytest.raises(ApplicationContractError):
            adapter.notify(observation(
                ControllerObservationKind.STEP_COMPLETED,
                model_call_index=0,
                step_index=0,
                state_before=ControllerState.REPRODUCE,
                state_after=ControllerState.REPRODUCE,
                directive_kind="action",
                stop_reason="bogus",
            ))

    def test_unknown_request_status_fails_closed(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        with pytest.raises(ApplicationContractError):
            adapter.notify(observation(
                ControllerObservationKind.MODEL_REQUEST_COMPLETED,
                model_call_index=0,
                state_before=ControllerState.REPRODUCE,
                request_status="timeout",
            ))

    def test_missing_required_fields_fail_closed(self):
        adapter = adapter_for()
        adapter.notify(observation(
            ControllerObservationKind.RUN_STARTED,
            model_call_index=0,
            state_before=ControllerState.REPRODUCE,
        ))
        with pytest.raises(ApplicationContractError):
            adapter.notify(observation(
                ControllerObservationKind.MODEL_REQUEST_STARTED,
                state_before=ControllerState.REPRODUCE,
            ))
        with pytest.raises(ApplicationContractError):
            adapter.notify(observation(
                ControllerObservationKind.DIRECTIVE_REJECTED,
                model_call_index=0,
                state_before=ControllerState.REPRODUCE,
                directive_kind="action",
            ))

    def test_non_observation_input_rejected(self):
        adapter = adapter_for()
        with pytest.raises(ApplicationContractError):
            adapter.notify("not an observation")  # type: ignore[arg-type]


class TestContextValidation:
    def test_invalid_context_rejected(self):
        with pytest.raises(ApplicationInputError):
            ControllerObservationContext(
                session_id="INVALID!", task_id=TASK_ID,
                source_kind=SourceKind.OFFLINE_DEMO,
            )
        with pytest.raises(ApplicationInputError):
            ControllerObservationContext(
                session_id=SESSION_ID, task_id="",
                source_kind=SourceKind.OFFLINE_DEMO,
            )
        with pytest.raises(ApplicationInputError):
            ControllerObservationContext(
                session_id=SESSION_ID, task_id=TASK_ID,
                source_kind="bogus",
            )

    def test_live_startable_source_kinds_accepted(self):
        for kind in (SourceKind.OFFLINE_DEMO, SourceKind.CONFIGURED_MODEL):
            ControllerObservationContext(
                session_id=SESSION_ID,
                task_id=TASK_ID,
                source_kind=kind,
            )

    def test_replay_only_source_kinds_rejected(self):
        for kind in (
            SourceKind.SESSION_BUNDLE,
            SourceKind.CANONICAL_TRAJECTORY,
            SourceKind.EXPERIMENT_EVIDENCE,
        ):
            with pytest.raises(ApplicationInputError):
                ControllerObservationContext(
                    session_id=SESSION_ID,
                    task_id=TASK_ID,
                    source_kind=kind,
                )

    @pytest.mark.parametrize(
        "value",
        [None, -1, 1.5, "1", True, False],
    )
    def test_initial_sequence_requires_strict_non_negative_int(self, value):
        with pytest.raises(ApplicationInputError):
            ControllerObservationContext(
                session_id=SESSION_ID,
                task_id=TASK_ID,
                source_kind=SourceKind.OFFLINE_DEMO,
                initial_sequence=value,
            )

    def test_initial_sequence_accepts_valid_values(self):
        for value in (0, 7, 1000):
            ControllerObservationContext(
                session_id=SESSION_ID,
                task_id=TASK_ID,
                source_kind=SourceKind.OFFLINE_DEMO,
                initial_sequence=value,
            )

    def test_adapter_requires_context_instance(self):
        with pytest.raises(ApplicationInputError):
            ControllerSessionEventAdapter(
                "not-a-context",  # type: ignore[arg-type]
            )

    def test_invalid_clock_rejected(self):
        with pytest.raises(ApplicationInputError):
            ControllerSessionEventAdapter(
                context(), clock=lambda: "not-a-timestamp"
            )
        with pytest.raises(ApplicationInputError):
            ControllerSessionEventAdapter(
                context(), clock="not-callable"  # type: ignore[arg-type]
            )
