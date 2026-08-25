"""SessionEvent schema, payload bounds, safe-data, and stream contract tests."""

from __future__ import annotations

import pytest

from agentic_debugger import SchemaValidationError
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application import ApplicationContractError
from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
    validate_session_event_stream,
)
from application_support import (
    VALID_PATCH_SHA256,
    VALID_PAYLOADS,
    VALID_RUN_ID,
    VALID_SPEC_FINGERPRINT,
    VALID_TASK_ID,
    make_completed_stream,
    make_event,
)

VALID_PAYLOADS = VALID_PAYLOADS


def make_event_mapping(kind: SessionEventKind, payload=None, **overrides):
    mapping = {
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "session_id": "session-test-001",
        "task_id": VALID_TASK_ID,
        "run_id": None,
        "sequence": 0,
        "timestamp_utc": "2026-08-14T08:00:00Z",
        "source_kind": "offline_demo",
        "event_kind": kind.value,
        "controller_phase": None,
        "payload": VALID_PAYLOADS[kind] if payload is None else payload,
    }
    mapping.update(overrides)
    return mapping


class TestSessionEventSchema:
    def test_every_kind_has_a_valid_payload(self):
        assert set(VALID_PAYLOADS) == set(SessionEventKind)
        # 29 Task-1 kinds + 4 bounded additive Task-4 kinds
        # (controller.transition, patch.apply_failed, source.snapshot,
        # diagnosis.recorded) + 1 bounded additive Task-8 kind
        # (model.configured, operator.progress).
        assert len(SessionEventKind) == 35

    @pytest.mark.parametrize("kind", list(SessionEventKind))
    def test_valid_event_round_trip(self, kind):
        event = SessionEvent.from_mapping(make_event_mapping(kind))
        assert event.event_kind is kind
        assert event.schema_version == SESSION_EVENT_SCHEMA_VERSION
        back = SessionEvent.from_mapping(event.to_mapping())
        assert back == event

    @pytest.mark.parametrize("kind", list(SessionEventKind))
    def test_unknown_payload_field_rejected(self, kind):
        payload = dict(VALID_PAYLOADS[kind])
        payload["extra_field"] = "x"
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(make_event_mapping(kind, payload))

    def test_payload_must_be_a_mapping(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(make_event_mapping(SessionEventKind.TOOL_STARTED, "nope"))

    def test_unknown_top_level_field_rejected(self):
        mapping = make_event_mapping(SessionEventKind.SESSION_CREATED)
        mapping["extra"] = 1
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(mapping)

    def test_missing_top_level_field_rejected(self):
        mapping = make_event_mapping(SessionEventKind.SESSION_CREATED)
        del mapping["payload"]
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(mapping)

    def test_wrong_schema_version_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(SessionEventKind.SESSION_CREATED, schema_version="1.0")
            )

    @pytest.mark.parametrize(
        "session_id",
        ["Bad ID", "S-1", "s" * 129, "", None],
    )
    def test_invalid_session_id_rejected(self, session_id):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(SessionEventKind.SESSION_CREATED, session_id=session_id)
            )

    def test_invalid_task_id_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(SessionEventKind.SESSION_CREATED, task_id="x" * 257)
            )

    def test_invalid_run_id_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.SESSION_STARTED, run_id="bad\x00run"
                )
            )

    @pytest.mark.parametrize("sequence", [-1, True, "0"])
    def test_invalid_sequence_rejected(self, sequence):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(SessionEventKind.SESSION_CREATED, sequence=sequence)
            )

    @pytest.mark.parametrize(
        "timestamp",
        ["2026-08-14T08:00:00", "garbage", "", "2026-08-14T08:00:00+02:00", None],
    )
    def test_invalid_timestamp_rejected(self, timestamp):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(SessionEventKind.SESSION_CREATED, timestamp_utc=timestamp)
            )

    def test_unknown_source_kind_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(SessionEventKind.SESSION_CREATED, source_kind="web_ide")
            )

    def test_unknown_event_kind_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(SessionEventKind.SESSION_CREATED, event_kind="controller.step2")
            )

    def test_invalid_controller_phase_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.CONTROLLER_STEP, controller_phase="NotAState"
                )
            )

    def test_valid_controller_phase_accepted(self):
        event = SessionEvent.from_mapping(
            make_event_mapping(
                SessionEventKind.CONTROLLER_STEP,
                controller_phase=ControllerState.REPRODUCE.value,
            )
        )
        assert event.controller_phase is ControllerState.REPRODUCE


class TestPayloadBounds:
    def test_verifier_execution_proven_is_optional_and_backward_compatible(self):
        historical = SessionEvent.from_mapping(
            make_event_mapping(
                SessionEventKind.VERIFIER_COMPLETED,
                {
                    "status": "COMPLETED",
                    "outcome": "RESOLVED",
                    "f2p_passed": 1,
                    "f2p_total": 1,
                    "p2p_passed": 1,
                    "p2p_total": 1,
                    "workspace_cleaned": True,
                },
            )
        )
        assert "official_test_execution_proven" not in historical.payload
        current = SessionEvent.from_mapping(
            make_event_mapping(
                SessionEventKind.VERIFIER_COMPLETED,
                {
                    "status": "COMPLETED",
                    "outcome": None,
                    "f2p_passed": None,
                    "f2p_total": None,
                    "p2p_passed": None,
                    "p2p_total": None,
                    "workspace_cleaned": False,
                    "official_test_execution_proven": False,
                },
            )
        )
        assert current.payload["official_test_execution_proven"] is False

    def test_oversized_text_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.TOOL_STARTED,
                    {"tool_name": "t" * 257},
                )
            )

    def test_control_characters_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.TOOL_STARTED,
                    {"tool_name": "apply\x00patch"},
                )
            )

    def test_non_finite_float_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.VERIFIER_COMPLETED,
                    {
                        "status": None,
                        "outcome": None,
                        "f2p_passed": float("nan"),
                        "f2p_total": None,
                        "p2p_passed": None,
                        "p2p_total": None,
                        "workspace_cleaned": None,
                    },
                )
            )

    def test_bad_sha256_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.PATCH_PROPOSED,
                    {"attempt_index": 0, "patch_sha256": "not-hex"},
                )
            )

    def test_too_many_frames_rejected(self):
        frames = [
            {"index": i, "function": "f", "file": "x.py", "line": 1, "is_current": False}
            for i in range(65)
        ]
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.DEBUGGER_STACK_OBSERVED,
                    {"pause_generation": 1, "frames": frames},
                )
            )

    def test_frame_unknown_field_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.DEBUGGER_STACK_OBSERVED,
                    {
                        "pause_generation": 1,
                        "frames": [
                            {
                                "index": 0,
                                "function": "f",
                                "file": "x.py",
                                "line": 1,
                                "is_current": True,
                                "extra": 1,
                            }
                        ],
                    },
                )
            )

    def test_bad_line_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.DEBUGGER_LOCATION_CHANGED,
                    {"script": "x.py", "line": 0, "function": "f", "pause_generation": 1},
                )
            )

    def test_empty_payload_kind_rejects_fields(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.CLEANUP_STARTED,
                    {"unexpected": 1},
                )
            )

    @pytest.mark.parametrize(
        "payload",
        [
            {"status": "succeeded", "phase": "verifying"},
            {"status": "failed", "phase": "verifying"},
            {"status": "running", "phase": "not_a_phase"},
            {"status": "running"},
        ],
    )
    def test_status_changed_contract(self, payload):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(SessionEventKind.SESSION_STATUS_CHANGED, payload)
            )

    def test_terminal_status_must_match_kind(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.SESSION_COMPLETED,
                    {"status": "failed", "termination_reason": "controller_failed"},
                )
            )

    def test_terminal_reason_must_be_compatible(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.SESSION_FAILED,
                    {"status": "failed", "termination_reason": "cancelled"},
                )
            )

    def test_cancelled_kind_requires_cancelled_status(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.SESSION_CANCELLED,
                    {"status": "failed", "termination_reason": "controller_failed"},
                )
            )


class TestSafeData:
    @pytest.mark.parametrize(
        "text",
        ["token=abc123", "Authorization: Bearer xyz", "api_key = 12345", "password=qwerty"],
    )
    def test_credential_shaped_payload_text_rejected(self, text):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.TOOL_STARTED,
                    {"tool_name": text},
                )
            )

    def test_credential_shaped_rejection_reason_rejected(self):
        with pytest.raises(SchemaValidationError):
            SessionEvent.from_mapping(
                make_event_mapping(
                    SessionEventKind.PATCH_REJECTED,
                    {"attempt_index": 0, "rejection_reason": "secret=abc123"},
                )
            )

    def test_innocent_text_accepted(self):
        event = SessionEvent.from_mapping(
            make_event_mapping(
                SessionEventKind.TOOL_STARTED,
                {"tool_name": "read_file"},
            )
        )
        assert event.payload["tool_name"] == "read_file"


def _construct_event(**overrides):
    """Direct-construction helper with a fully valid tool.started event."""
    values = {
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "session_id": "session-test-001",
        "task_id": VALID_TASK_ID,
        "run_id": None,
        "sequence": 0,
        "timestamp_utc": "2026-08-14T08:00:00Z",
        "source_kind": SourceKind.OFFLINE_DEMO,
        "event_kind": SessionEventKind.TOOL_STARTED,
        "controller_phase": None,
        "payload": {"tool_name": "read_file"},
    }
    values.update(overrides)
    return SessionEvent(**values)


def _stack_event():
    return _construct_event(
        event_kind=SessionEventKind.DEBUGGER_STACK_OBSERVED,
        payload={
            "pause_generation": 1,
            "frames": [
                {"index": 0, "function": "main", "file": "buggy.py", "line": 12, "is_current": True}
            ],
        },
    )


class TestEventImmutability:
    """Blocker-1 adversarial coverage: validated construction + frozen payload."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {"schema_version": "bogus"},
            {"session_id": "BAD!"},
            {"session_id": "s" * 129},
            {"task_id": ""},
            {"task_id": "x" * 257},
            {"run_id": "bad\x00run"},
            {"sequence": -5},
            {"sequence": "0"},
            {"timestamp_utc": "nope"},
            {"timestamp_utc": "2026-08-14T08:00:00"},
            {"source_kind": "offline_demo"},  # string, not the enum
            {"source_kind": "web_ide"},
            {"event_kind": "tool.started"},  # string, not the enum
            {"event_kind": "controller.step2"},
            {"controller_phase": "Reproduce"},  # string, not the enum
            {"payload": {"tool_name": "read_file", "extra": 1}},
            {"payload": {"tool_name": "t" * 257}},
            {"payload": "nope"},
            {"payload": {"tool_name": "token=abc123"}},
        ],
    )
    def test_invalid_direct_construction_rejected(self, overrides):
        with pytest.raises(SchemaValidationError):
            _construct_event(**overrides)

    def test_invalid_direct_construction_rejected_for_created_kind(self):
        with pytest.raises(SchemaValidationError):
            _construct_event(
                event_kind=SessionEventKind.SESSION_CREATED,
                payload={"spec_fingerprint": "not-hex"},
            )

    def test_valid_direct_construction_canonicalizes_payload(self):
        event = _construct_event()
        assert event.event_kind is SessionEventKind.TOOL_STARTED
        assert event.payload["tool_name"] == "read_file"
        # The payload is a frozen mapping, not the caller's dict.
        assert type(event.payload).__name__ == "_FrozenDict"
        assert event.to_mapping()["payload"] == {"tool_name": "read_file"}

    def test_event_is_hashable(self):
        event = _construct_event()
        assert hash(event) == hash(_construct_event())

    def test_input_mapping_mutation_cannot_change_event(self):
        mapping = make_event_mapping(
            SessionEventKind.TOOL_STARTED,
            {"tool_name": "read_file"},
        )
        event = SessionEvent.from_mapping(mapping)
        mapping["payload"]["tool_name"] = "mutated"
        mapping["payload"]["extra"] = 1
        mapping["sequence"] = 99
        mapping["session_id"] = "session-other"
        assert event.payload["tool_name"] == "read_file"
        assert event.sequence == 0
        assert event.session_id == "session-test-001"

    def test_nested_input_mutation_cannot_change_event(self):
        frames = [
            {"index": 0, "function": "main", "file": "buggy.py", "line": 12, "is_current": True}
        ]
        mapping = make_event_mapping(
            SessionEventKind.DEBUGGER_STACK_OBSERVED,
            {"pause_generation": 1, "frames": frames},
        )
        event = SessionEvent.from_mapping(mapping)
        frames[0]["function"] = "mutated"
        frames[0]["extra"] = 1
        frames.append(
            {"index": 1, "function": "other", "file": "x.py", "line": 1, "is_current": False}
        )
        mapping["payload"]["frames"][0]["line"] = 999
        assert event.payload["frames"][0]["function"] == "main"
        assert event.payload["frames"][0]["line"] == 12
        assert len(event.payload["frames"]) == 1

    def test_payload_mutation_attempts_fail(self):
        event = _stack_event()
        with pytest.raises(TypeError):
            event.payload["pause_generation"] = 5
        with pytest.raises(TypeError):
            event.payload["frames"][0] = {"index": 9}
        with pytest.raises(TypeError):
            event.payload["frames"][0]["function"] = "mutated"
        with pytest.raises(AttributeError):
            event.payload["frames"].append({"index": 9})
        with pytest.raises(Exception):
            event.payload = {"pause_generation": 1, "frames": []}  # type: ignore[misc]

    def test_to_mapping_returns_plain_independent_json(self):
        event = _stack_event()
        mapping = event.to_mapping()
        assert type(mapping) is dict
        assert type(mapping["payload"]) is dict
        assert type(mapping["payload"]["frames"]) is list
        assert type(mapping["payload"]["frames"][0]) is dict
        import json as _json

        assert _json.loads(_json.dumps(mapping, allow_nan=False)) == mapping
        # Mutating the returned mapping must not change the event.
        mapping["payload"]["frames"][0]["function"] = "mutated"
        mapping["payload"]["pause_generation"] = 99
        assert event.payload["frames"][0]["function"] == "main"
        assert event.payload["pause_generation"] == 1

    def test_round_trip_after_adversarial_attempts_stays_stable(self):
        event = _stack_event()
        back = SessionEvent.from_mapping(event.to_mapping())
        assert back == event
        assert back.to_mapping() == event.to_mapping()


class TestStreamContract:
    def test_completed_stream_valid(self):
        validate_session_event_stream(make_completed_stream())

    def test_empty_stream_rejected(self):
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(())

    def test_first_event_must_be_created(self):
        events = make_completed_stream()
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events[1:])

    def test_non_contiguous_sequence_rejected(self):
        events = list(make_completed_stream())
        events[2] = make_event(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": "waiting_model"},
            sequence=9,
            run_id=VALID_RUN_ID,
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_mixed_session_ids_rejected(self):
        events = list(make_completed_stream())
        events[3] = make_event(
            SessionEventKind.CLEANUP_STARTED,
            {},
            sequence=3,
            session_id="session-other",
            run_id=VALID_RUN_ID,
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_run_id_before_start_rejected(self):
        events = list(make_completed_stream())
        events[0] = make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
            sequence=0,
            run_id=VALID_RUN_ID,
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_started_requires_run_id(self):
        events = list(make_completed_stream())
        events[1] = make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1)
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_run_id_change_rejected(self):
        events = list(make_completed_stream())
        events[4] = make_event(
            SessionEventKind.CLEANUP_COMPLETED,
            {"verified": True},
            sequence=4,
            run_id="run-other",
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_duplicate_started_rejected(self):
        events = list(make_completed_stream())
        extra = make_event(
            SessionEventKind.SESSION_STARTED,
            {},
            sequence=2,
            run_id=VALID_RUN_ID,
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(
                events[:2] + [extra] + list(events[2:])
            )

    def test_illegal_status_transition_rejected(self):
        events = (
            make_event(
                SessionEventKind.SESSION_CREATED,
                {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
                sequence=0,
            ),
            make_event(
                SessionEventKind.SESSION_STATUS_CHANGED,
                {"status": "running", "phase": "verifying"},
                sequence=1,
                run_id=VALID_RUN_ID,
            ),
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_two_terminal_events_rejected(self):
        events = list(make_completed_stream())
        extra = make_event(
            SessionEventKind.SESSION_COMPLETED,
            {"status": "succeeded", "termination_reason": "done"},
            sequence=6,
            run_id=VALID_RUN_ID,
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(tuple(events) + (extra,))

    def test_missing_terminal_event_rejected(self):
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(make_completed_stream()[:5])

    def test_completed_requires_verified_cleanup(self):
        events = list(make_completed_stream())
        events[4] = make_event(
            SessionEventKind.CLEANUP_COMPLETED,
            {"verified": False},
            sequence=4,
            run_id=VALID_RUN_ID,
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_completed_without_cleanup_rejected(self):
        stream = make_completed_stream()
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(stream[:3] + stream[5:])

    def test_cancelled_requires_verified_cleanup(self):
        events = (
            make_completed_stream()[:5]
            + (
                make_event(
                    SessionEventKind.SESSION_CANCELLED,
                    {"status": "cancelled", "termination_reason": "cancelled"},
                    sequence=5,
                    run_id=VALID_RUN_ID,
                ),
            )
        )
        validate_session_event_stream(events)

    def test_cancelled_without_cleanup_rejected(self):
        events = (
            make_completed_stream()[:3]
            + (
                make_event(
                    SessionEventKind.SESSION_CANCEL_REQUESTED,
                    {},
                    sequence=3,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.SESSION_CANCELLED,
                    {"status": "cancelled", "termination_reason": "cancelled"},
                    sequence=4,
                    run_id=VALID_RUN_ID,
                ),
            )
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_duplicate_cancel_requested_rejected(self):
        events = (
            make_completed_stream()[:3]
            + (
                make_event(
                    SessionEventKind.SESSION_CANCEL_REQUESTED,
                    {},
                    sequence=3,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.SESSION_CANCEL_REQUESTED,
                    {},
                    sequence=4,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.CLEANUP_STARTED,
                    {},
                    sequence=5,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.CLEANUP_COMPLETED,
                    {"verified": True},
                    sequence=6,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.SESSION_CANCELLED,
                    {"status": "cancelled", "termination_reason": "cancelled"},
                    sequence=7,
                    run_id=VALID_RUN_ID,
                ),
            )
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_failed_before_start_valid(self):
        events = (
            make_event(
                SessionEventKind.SESSION_CREATED,
                {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
                sequence=0,
            ),
            make_event(
                SessionEventKind.SESSION_FAILED,
                {"status": "failed", "termination_reason": "journal_error"},
                sequence=1,
            ),
        )
        validate_session_event_stream(events)

    def test_cancelled_before_start_valid(self):
        events = (
            make_event(
                SessionEventKind.SESSION_CREATED,
                {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
                sequence=0,
            ),
            make_event(
                SessionEventKind.SESSION_CANCEL_REQUESTED,
                {},
                sequence=1,
            ),
            make_event(
                SessionEventKind.SESSION_CANCELLED,
                {"status": "cancelled", "termination_reason": "cancelled"},
                sequence=2,
            ),
        )
        validate_session_event_stream(events)

    def test_cleanup_failed_requires_cleanup_started(self):
        events = (
            make_completed_stream()[:3]
            + (
                make_event(
                    SessionEventKind.SESSION_FAILED,
                    {"status": "cleanup_failed", "termination_reason": "cleanup_failed"},
                    sequence=3,
                    run_id=VALID_RUN_ID,
                ),
            )
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_cleanup_failed_valid_after_attempted_cleanup(self):
        events = (
            make_completed_stream()[:3]
            + (
                make_event(
                    SessionEventKind.CLEANUP_STARTED,
                    {},
                    sequence=3,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.SESSION_FAILED,
                    {"status": "cleanup_failed", "termination_reason": "cleanup_failed"},
                    sequence=4,
                    run_id=VALID_RUN_ID,
                ),
            )
        )
        validate_session_event_stream(events)

    def test_cleanup_failed_cannot_follow_verified_cleanup(self):
        events = (
            make_completed_stream()[:5]
            + (
                make_event(
                    SessionEventKind.SESSION_FAILED,
                    {"status": "cleanup_failed", "termination_reason": "cleanup_failed"},
                    sequence=5,
                    run_id=VALID_RUN_ID,
                ),
            )
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_cleanup_completed_without_started_rejected(self):
        # Blocker-2 invalid case A: cleanup.completed without cleanup.started.
        events = (
            make_completed_stream()[:3]
            + (
                make_event(
                    SessionEventKind.CLEANUP_COMPLETED,
                    {"verified": True},
                    sequence=3,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.SESSION_COMPLETED,
                    {"status": "succeeded", "termination_reason": "done"},
                    sequence=4,
                    run_id=VALID_RUN_ID,
                ),
            )
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_duplicate_cleanup_started_rejected(self):
        events = (
            make_completed_stream()[:3]
            + (
                make_event(
                    SessionEventKind.CLEANUP_STARTED,
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
                    SessionEventKind.SESSION_COMPLETED,
                    {"status": "succeeded", "termination_reason": "done"},
                    sequence=6,
                    run_id=VALID_RUN_ID,
                ),
            )
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_new_unmatched_cleanup_cycle_blocks_completion(self):
        # Blocker-2 invalid case B: an earlier verified cleanup must not
        # authorize completion while a later cleanup cycle is incomplete.
        events = (
            make_completed_stream()[:5]
            + (
                make_event(
                    SessionEventKind.CLEANUP_STARTED,
                    {},
                    sequence=5,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.SESSION_COMPLETED,
                    {"status": "succeeded", "termination_reason": "done"},
                    sequence=6,
                    run_id=VALID_RUN_ID,
                ),
            )
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_new_unmatched_cleanup_cycle_blocks_cancellation(self):
        events = (
            make_completed_stream()[:5]
            + (
                make_event(
                    SessionEventKind.CLEANUP_STARTED,
                    {},
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
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_completed_after_second_verified_cleanup_cycle_valid(self):
        events = (
            make_completed_stream()[:5]
            + (
                make_event(
                    SessionEventKind.CLEANUP_STARTED,
                    {},
                    sequence=5,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.CLEANUP_COMPLETED,
                    {"verified": True},
                    sequence=6,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.SESSION_COMPLETED,
                    {"status": "succeeded", "termination_reason": "done"},
                    sequence=7,
                    run_id=VALID_RUN_ID,
                ),
            )
        )
        validate_session_event_stream(events)

    def test_completed_after_unverified_cleanup_rejected(self):
        events = (
            make_completed_stream()[:4]
            + (
                make_event(
                    SessionEventKind.CLEANUP_COMPLETED,
                    {"verified": False},
                    sequence=4,
                    run_id=VALID_RUN_ID,
                ),
                make_event(
                    SessionEventKind.SESSION_COMPLETED,
                    {"status": "succeeded", "termination_reason": "done"},
                    sequence=5,
                    run_id=VALID_RUN_ID,
                ),
            )
        )
        with pytest.raises(ApplicationContractError):
            validate_session_event_stream(events)

    def test_unresolved_terminal_valid(self):
        events = (
            make_completed_stream()[:5]
            + (
                make_event(
                    SessionEventKind.SESSION_COMPLETED,
                    {"status": "unresolved", "termination_reason": "unresolved"},
                    sequence=5,
                    run_id=VALID_RUN_ID,
                ),
            )
        )
        validate_session_event_stream(events)

    def test_replay_source_kind_stream_valid(self):
        events = []
        for index, event in enumerate(make_completed_stream()):
            mapping = event.to_mapping()
            mapping["source_kind"] = SourceKind.SESSION_BUNDLE.value
            events.append(SessionEvent.from_mapping(mapping))
        validate_session_event_stream(events)
