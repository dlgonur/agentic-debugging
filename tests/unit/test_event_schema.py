import copy
from datetime import datetime, timezone

import pytest

from agentic_debugger import SchemaValidationError
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.events.schema import (
    Action,
    EventType,
    Metadata,
    Observation,
    ObservationStatus,
    RunEvent,
)


def make_valid_action_mapping(**overrides):
    m = {
        "action_id": "act-001",
        "run_id": "run-001",
        "task_id": "task-001",
        "state": "Reproduce",
        "name": "run_tests",
        "arguments": {"argv": ["pytest", "test.py"]},
    }
    m.update(overrides)
    return m


def make_valid_observation_mapping(**overrides):
    m = {
        "observation_id": "obs-001",
        "action_id": "act-001",
        "run_id": "run-001",
        "task_id": "task-001",
        "name": "test_result",
        "status": "ok",
        "payload": {"exit_code": 0, "stdout_lines": 10},
        "summary": "Tests passed",
        "truncated": False,
    }
    m.update(overrides)
    return m


def make_valid_event_mapping(**overrides):
    m = {
        "schema_version": "1.0",
        "event_id": "evt-001",
        "run_id": "run-001",
        "task_id": "task-001",
        "sequence": 0,
        "timestamp": "2026-07-18T12:00:00Z",
        "event_type": "action",
        "name": "run_tests",
        "state": "Reproduce",
        "payload": {"argv": ["pytest", "test.py"]},
        "metadata": {
            "duration_ms": None,
            "tool_version": None,
            "model": None,
            "tokens": None,
            "cost": None,
        },
    }
    m.update(overrides)
    return m


class TestAction:
    def test_valid_action_from_mapping(self):
        action = Action.from_mapping(make_valid_action_mapping())
        assert action.action_id == "act-001"
        assert action.run_id == "run-001"
        assert action.task_id == "task-001"
        assert action.state == ControllerState.REPRODUCE
        assert action.name == "run_tests"
        assert action.arguments == {"argv": ["pytest", "test.py"]}

    def test_valid_action_serializes(self):
        action = Action.from_mapping(make_valid_action_mapping())
        mapping = action.to_mapping()
        assert mapping["action_id"] == "act-001"
        assert mapping["state"] == "Reproduce"
        assert mapping["arguments"] == {"argv": ["pytest", "test.py"]}

    def test_deterministic_serialization(self):
        a1 = Action.from_mapping(make_valid_action_mapping())
        a2 = Action.from_mapping(make_valid_action_mapping())
        assert a1.to_mapping() == a2.to_mapping()

    def test_invalid_state_rejected(self):
        m = make_valid_action_mapping(state="InvalidState")
        with pytest.raises(SchemaValidationError, match="Unknown controller state"):
            Action.from_mapping(m)

    def test_empty_action_id_rejected(self):
        m = make_valid_action_mapping(action_id="")
        with pytest.raises(SchemaValidationError, match="non-empty"):
            Action.from_mapping(m)

    def test_non_dict_arguments_rejected(self):
        m = make_valid_action_mapping(arguments="not-a-dict")
        with pytest.raises(SchemaValidationError, match="mapping"):
            Action.from_mapping(m)

    def test_non_json_argument_values_rejected(self):
        m = make_valid_action_mapping(arguments={"fn": lambda: 1})
        with pytest.raises(SchemaValidationError):
            Action.from_mapping(m)


class TestObservation:
    def test_valid_observation_from_mapping(self):
        obs = Observation.from_mapping(make_valid_observation_mapping())
        assert obs.observation_id == "obs-001"
        assert obs.status == ObservationStatus.OK
        assert obs.summary == "Tests passed"
        assert obs.truncated is False

    def test_valid_observation_serializes(self):
        obs = Observation.from_mapping(make_valid_observation_mapping())
        mapping = obs.to_mapping()
        assert mapping["observation_id"] == "obs-001"
        assert mapping["status"] == "ok"
        assert mapping["summary"] == "Tests passed"
        assert mapping["truncated"] is False

    def test_deterministic_serialization(self):
        o1 = Observation.from_mapping(make_valid_observation_mapping())
        o2 = Observation.from_mapping(make_valid_observation_mapping())
        assert o1.to_mapping() == o2.to_mapping()

    def test_invalid_status_rejected(self):
        m = make_valid_observation_mapping(status="bogus")
        with pytest.raises(SchemaValidationError, match="Unknown observation status"):
            Observation.from_mapping(m)

    def test_missing_status_rejected(self):
        m = make_valid_observation_mapping()
        del m["status"]
        with pytest.raises(SchemaValidationError, match="Missing required fields"):
            Observation.from_mapping(m)

    def test_truncation_explicit(self):
        obs = Observation.from_mapping(make_valid_observation_mapping(truncated=True))
        assert obs.truncated is True

    def test_truncation_false_explicit(self):
        obs = Observation.from_mapping(make_valid_observation_mapping(truncated=False))
        assert obs.truncated is False

    def test_status_error_explicit(self):
        obs = Observation.from_mapping(
            make_valid_observation_mapping(status="error")
        )
        assert obs.status == ObservationStatus.ERROR

    def test_status_rejected_explicit(self):
        obs = Observation.from_mapping(
            make_valid_observation_mapping(status="rejected")
        )
        assert obs.status == ObservationStatus.REJECTED

    def test_status_timeout_explicit(self):
        obs = Observation.from_mapping(
            make_valid_observation_mapping(status="timeout")
        )
        assert obs.status == ObservationStatus.TIMEOUT

    def test_summary_empty_string_allowed(self):
        obs = Observation.from_mapping(make_valid_observation_mapping(summary=""))
        assert obs.summary == ""


class TestRunEvent:
    def test_valid_event_from_mapping(self):
        event = RunEvent.from_mapping(make_valid_event_mapping())
        assert event.event_id == "evt-001"
        assert event.sequence == 0
        assert event.event_type == EventType.ACTION
        assert event.state == "Reproduce"
        assert event.metadata.duration_ms is None

    def test_valid_event_serializes(self):
        event = RunEvent.from_mapping(make_valid_event_mapping())
        mapping = event.to_mapping()
        assert mapping["event_id"] == "evt-001"
        assert mapping["event_type"] == "action"
        assert mapping["sequence"] == 0

    def test_deterministic_serialization(self):
        e1 = RunEvent.from_mapping(make_valid_event_mapping())
        e2 = RunEvent.from_mapping(make_valid_event_mapping())
        assert e1.to_mapping() == e2.to_mapping()

    def test_negative_sequence_rejected(self):
        m = make_valid_event_mapping(sequence=-1)
        with pytest.raises(SchemaValidationError, match="non-negative"):
            RunEvent.from_mapping(m)

    def test_invalid_event_type_rejected(self):
        m = make_valid_event_mapping(event_type="bogus")
        with pytest.raises(SchemaValidationError, match="Unknown event type"):
            RunEvent.from_mapping(m)

    def test_unsupported_schema_version_rejected(self):
        m = make_valid_event_mapping(schema_version="0.5")
        with pytest.raises(SchemaValidationError, match="Unsupported event schema"):
            RunEvent.from_mapping(m)

    def test_non_utc_timestamp_rejected(self):
        m = make_valid_event_mapping(timestamp="2026-07-18T12:00:00")
        with pytest.raises(SchemaValidationError, match="ISO-8601 UTC"):
            RunEvent.from_mapping(m)

    def test_non_iso_timestamp_rejected(self):
        m = make_valid_event_mapping(timestamp="not-a-timestamp")
        with pytest.raises(SchemaValidationError, match="ISO-8601 UTC"):
            RunEvent.from_mapping(m)

    def test_utc_plus_00_00_timestamp_accepted(self):
        m = make_valid_event_mapping(timestamp="2026-07-18T12:00:00+00:00")
        event = RunEvent.from_mapping(m)
        assert event.timestamp == "2026-07-18T12:00:00+00:00"

    def test_non_json_payload_rejected(self):
        m = make_valid_event_mapping(payload={"fn": lambda: 1})
        with pytest.raises(SchemaValidationError, match="JSON-compatible"):
            RunEvent.from_mapping(m)

    def test_state_none_allowed(self):
        m = make_valid_event_mapping(state=None)
        event = RunEvent.from_mapping(m)
        assert event.state is None

    def test_metadata_with_values(self):
        m = make_valid_event_mapping(
            metadata={
                "duration_ms": 150,
                "tool_version": "1.0",
                "model": "test-model",
                "tokens": 42,
                "cost": 0.001,
            }
        )
        event = RunEvent.from_mapping(m)
        assert event.metadata.duration_ms == 150
        assert event.metadata.model == "test-model"
        assert event.metadata.cost == 0.001

    def test_event_type_message(self):
        m = make_valid_event_mapping(event_type="message")
        event = RunEvent.from_mapping(m)
        assert event.event_type == EventType.MESSAGE

    def test_event_type_observation(self):
        m = make_valid_event_mapping(event_type="observation")
        event = RunEvent.from_mapping(m)
        assert event.event_type == EventType.OBSERVATION

    def test_event_type_decision(self):
        m = make_valid_event_mapping(event_type="decision")
        event = RunEvent.from_mapping(m)
        assert event.event_type == EventType.DECISION

    def test_event_type_transition(self):
        m = make_valid_event_mapping(event_type="transition")
        event = RunEvent.from_mapping(m)
        assert event.event_type == EventType.TRANSITION

    def test_event_type_final(self):
        m = make_valid_event_mapping(event_type="final")
        event = RunEvent.from_mapping(m)
        assert event.event_type == EventType.FINAL

    def test_empty_event_id_rejected(self):
        m = make_valid_event_mapping(event_id="")
        with pytest.raises(SchemaValidationError, match="non-empty"):
            RunEvent.from_mapping(m)

    def test_timestamp_with_fractional_seconds_z(self):
        m = make_valid_event_mapping(timestamp="2026-07-18T12:00:00.123456Z")
        event = RunEvent.from_mapping(m)
        assert event.timestamp == "2026-07-18T12:00:00.123456Z"

    # --- Issue 3: non-finite JSON tests ---

    def test_nan_in_action_arguments_rejected(self):
        m = make_valid_action_mapping(arguments={"score": float("nan")})
        with pytest.raises(SchemaValidationError, match="Non-finite"):
            Action.from_mapping(m)

    def test_infinity_in_action_arguments_rejected(self):
        m = make_valid_action_mapping(arguments={"limit": float("inf")})
        with pytest.raises(SchemaValidationError, match="Non-finite"):
            Action.from_mapping(m)

    def test_neg_infinity_in_action_arguments_rejected(self):
        m = make_valid_action_mapping(arguments={"limit": float("-inf")})
        with pytest.raises(SchemaValidationError, match="Non-finite"):
            Action.from_mapping(m)

    def test_nan_in_observation_payload_rejected(self):
        m = make_valid_observation_mapping(payload={"value": float("nan")})
        with pytest.raises(SchemaValidationError, match="Non-finite"):
            Observation.from_mapping(m)

    def test_infinity_in_observation_payload_rejected(self):
        m = make_valid_observation_mapping(payload={"value": float("inf")})
        with pytest.raises(SchemaValidationError, match="Non-finite"):
            Observation.from_mapping(m)

    def test_nan_in_event_payload_rejected(self):
        m = make_valid_event_mapping(payload={"x": float("nan")})
        with pytest.raises(SchemaValidationError, match="Non-finite"):
            RunEvent.from_mapping(m)

    def test_nan_in_metadata_cost_rejected(self):
        m = make_valid_event_mapping(
            metadata={
                "duration_ms": None,
                "tool_version": None,
                "model": None,
                "tokens": None,
                "cost": float("nan"),
            }
        )
        with pytest.raises(SchemaValidationError, match="finite"):
            RunEvent.from_mapping(m)

    def test_infinity_in_metadata_cost_rejected(self):
        m = make_valid_event_mapping(
            metadata={
                "duration_ms": None,
                "tool_version": None,
                "model": None,
                "tokens": None,
                "cost": float("inf"),
            }
        )
        with pytest.raises(SchemaValidationError, match="finite"):
            RunEvent.from_mapping(m)

    def test_finite_float_in_payload_accepted(self):
        m = make_valid_event_mapping(payload={"pi": 3.14, "e": 2.718})
        event = RunEvent.from_mapping(m)
        assert event.payload["pi"] == 3.14

    def test_finite_float_in_arguments_accepted(self):
        m = make_valid_action_mapping(arguments={"threshold": 0.5})
        action = Action.from_mapping(m)
        assert action.arguments["threshold"] == 0.5

    # --- Issue 4: strict and lossless event tests ---

    def test_event_missing_required_field_rejected(self):
        m = make_valid_event_mapping()
        del m["event_id"]
        with pytest.raises(SchemaValidationError, match="Missing required fields"):
            RunEvent.from_mapping(m)

    def test_event_unknown_field_rejected(self):
        m = make_valid_event_mapping(extra_key="unexpected")
        with pytest.raises(SchemaValidationError, match="Unknown fields"):
            RunEvent.from_mapping(m)

    def test_action_unknown_field_rejected(self):
        m = make_valid_action_mapping(extra_key="unexpected")
        with pytest.raises(SchemaValidationError, match="Unknown fields"):
            Action.from_mapping(m)

    def test_observation_unknown_field_rejected(self):
        m = make_valid_observation_mapping(extra_key="unexpected")
        with pytest.raises(SchemaValidationError, match="Unknown fields"):
            Observation.from_mapping(m)

    def test_metadata_unknown_field_rejected(self):
        m = make_valid_event_mapping(
            metadata={
                "duration_ms": None,
                "tool_version": None,
                "model": None,
                "tokens": None,
                "cost": None,
                "unknown_meta": "bad",
            }
        )
        with pytest.raises(SchemaValidationError, match="Unknown fields"):
            RunEvent.from_mapping(m)

    def test_metadata_missing_field_rejected(self):
        m = make_valid_event_mapping(
            metadata={
                "duration_ms": None,
                "tool_version": None,
                "model": None,
                "tokens": None,
            }
        )
        with pytest.raises(SchemaValidationError, match="Missing required fields"):
            RunEvent.from_mapping(m)

    def test_action_missing_required_field_rejected(self):
        m = make_valid_action_mapping()
        del m["action_id"]
        with pytest.raises(SchemaValidationError, match="Missing required fields"):
            Action.from_mapping(m)

    def test_observation_missing_required_field_rejected(self):
        m = make_valid_observation_mapping()
        del m["observation_id"]
        with pytest.raises(SchemaValidationError, match="Missing required fields"):
            Observation.from_mapping(m)

    def test_wrong_tool_version_type_rejected(self):
        m = make_valid_event_mapping(
            metadata={
                "duration_ms": None,
                "tool_version": 123,
                "model": None,
                "tokens": None,
                "cost": None,
            }
        )
        with pytest.raises(SchemaValidationError, match="string or null"):
            RunEvent.from_mapping(m)

    def test_wrong_model_type_rejected(self):
        m = make_valid_event_mapping(
            metadata={
                "duration_ms": None,
                "tool_version": None,
                "model": 456,
                "tokens": None,
                "cost": None,
            }
        )
        with pytest.raises(SchemaValidationError, match="string or null"):
            RunEvent.from_mapping(m)

    def test_invalid_event_state_rejected(self):
        m = make_valid_event_mapping(state="BogusState")
        with pytest.raises(SchemaValidationError, match="not a valid ControllerState"):
            RunEvent.from_mapping(m)

    def test_event_state_non_string_rejected(self):
        m = make_valid_event_mapping(state=42)
        with pytest.raises(SchemaValidationError, match="string or null"):
            RunEvent.from_mapping(m)

    def test_impossible_date_rejected(self):
        m = make_valid_event_mapping(timestamp="2026-02-30T12:00:00Z")
        with pytest.raises(SchemaValidationError, match="not a valid"):
            RunEvent.from_mapping(m)

    def test_non_utc_positive_offset_rejected(self):
        m = make_valid_event_mapping(timestamp="2026-07-18T12:00:00+05:00")
        with pytest.raises(SchemaValidationError, match="ISO-8601 UTC"):
            RunEvent.from_mapping(m)

    def test_metadata_event_required(self):
        m = make_valid_event_mapping()
        del m["metadata"]
        with pytest.raises(SchemaValidationError, match="Missing required fields"):
            RunEvent.from_mapping(m)

    def test_wrong_duration_ms_type_rejected(self):
        m = make_valid_event_mapping(
            metadata={
                "duration_ms": "150",
                "tool_version": None,
                "model": None,
                "tokens": None,
                "cost": None,
            }
        )
        with pytest.raises(SchemaValidationError, match="integer or null"):
            RunEvent.from_mapping(m)

    def test_wrong_tokens_type_rejected(self):
        m = make_valid_event_mapping(
            metadata={
                "duration_ms": None,
                "tool_version": None,
                "model": None,
                "tokens": 12.5,
                "cost": None,
            }
        )
        with pytest.raises(SchemaValidationError, match="integer or null"):
            RunEvent.from_mapping(m)

    def test_wrong_cost_type_rejected_bool(self):
        m = make_valid_event_mapping(
            metadata={
                "duration_ms": None,
                "tool_version": None,
                "model": None,
                "tokens": None,
                "cost": True,
            }
        )
        with pytest.raises(SchemaValidationError, match="number or null"):
            RunEvent.from_mapping(m)

    def test_event_type_invalid_event_type_name(self):
        m = make_valid_event_mapping(event_type="")
        with pytest.raises(SchemaValidationError, match="non-empty string"):
            RunEvent.from_mapping(m)

    def test_negative_zero_offset_rejected(self):
        m = make_valid_event_mapping(timestamp="2026-07-18T12:00:00-00:00")
        with pytest.raises(SchemaValidationError, match="ISO-8601 UTC"):
            RunEvent.from_mapping(m)
