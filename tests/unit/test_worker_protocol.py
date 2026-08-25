import json

import pytest

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.events import SessionEventKind, SourceKind
from agentic_debugger.application.session import SessionBudgets, SessionId
from agentic_debugger.application.sources import ExecutionSourceSpec
from agentic_debugger.application.worker_protocol import (
    MAX_WORKER_LINE_BYTES,
    StartRequest,
    WorkerProtocolError,
    cancel_message,
    error_message,
    event_notification,
    fatal_message,
    liveness_notification,
    parse_cancel_message,
    parse_parent_message,
    parse_start_request,
    parse_worker_message,
    ready_message,
    session_spec_from_mapping,
    start_message,
    terminal_message,
)
from agentic_debugger.application.session import SessionSpec


def make_spec():
    return SessionSpec(
        task_id="curated-off-by-one-002",
        source=ExecutionSourceSpec(
            kind=SourceKind.OFFLINE_DEMO,
            task_id="curated-off-by-one-002",
            policy="static-baseline",
        ),
        budgets=SessionBudgets(max_model_calls=8, max_elapsed_seconds=120),
        artifact_destination=None,
    )


def make_start_mapping():
    return {
        "type": "start",
        "session_id": "session.task3.001",
        "spec": make_spec().to_mapping(),
        "run_id": "run-task3-001",
        "work_dir": r"C:\tmp\work",
        "journal_path": r"C:\tmp\journal.jsonl",
        "scenario": "synthetic_work",
        "scenario_params": {"steps": 3, "step_interval_seconds": 0.01},
        "max_elapsed_seconds": None,
        "pre_start_delay_seconds": 0.0,
    }


class TestStartRequest:
    def test_valid_start_round_trips(self):
        request = parse_start_request(make_start_mapping())
        assert request.session_id == "session.task3.001"
        assert request.spec.task_id == "curated-off-by-one-002"
        assert request.run_id == "run-task3-001"
        assert request.scenario == "synthetic_work"
        assert request.scenario_params == {"steps": 3, "step_interval_seconds": 0.01}
        assert request.spec.fingerprint() == make_spec().fingerprint()

    def test_missing_fields_fail_closed(self):
        mapping = make_start_mapping()
        del mapping["run_id"]
        with pytest.raises(WorkerProtocolError):
            parse_start_request(mapping)

    def test_unknown_fields_fail_closed(self):
        mapping = make_start_mapping()
        mapping["extra"] = 1
        with pytest.raises(WorkerProtocolError):
            parse_start_request(mapping)

    def test_invalid_session_id_fails_closed(self):
        mapping = make_start_mapping()
        mapping["session_id"] = "UPPERCASE"
        with pytest.raises(WorkerProtocolError):
            parse_start_request(mapping)

    def test_recorded_source_kind_rejected(self):
        mapping = make_start_mapping()
        spec = make_spec().to_mapping()
        spec["source"]["kind"] = SourceKind.SESSION_BUNDLE.value
        mapping["spec"] = spec
        with pytest.raises(WorkerProtocolError):
            parse_start_request(mapping)

    def test_bad_scenario_params_fail_closed(self):
        mapping = make_start_mapping()
        mapping["scenario_params"] = {"steps": [1, 2]}
        with pytest.raises(WorkerProtocolError):
            parse_start_request(mapping)
        mapping["scenario_params"] = {"nested": {"a": 1}}
        with pytest.raises(WorkerProtocolError):
            parse_start_request(mapping)

    def test_pre_start_delay_bounds(self):
        mapping = make_start_mapping()
        mapping["pre_start_delay_seconds"] = 61.0
        with pytest.raises(WorkerProtocolError):
            parse_start_request(mapping)
        mapping["pre_start_delay_seconds"] = -0.5
        with pytest.raises(WorkerProtocolError):
            parse_start_request(mapping)

    def test_spec_fingerprint_is_stable(self):
        spec = make_spec()
        assert spec.fingerprint() == session_spec_from_mapping(spec.to_mapping()).fingerprint()

    def test_spec_from_mapping_round_trip(self):
        spec = make_spec()
        restored = session_spec_from_mapping(spec.to_mapping())
        assert restored == spec
        assert restored.to_mapping() == spec.to_mapping()


class TestMessages:
    def test_cancel_message_is_valid(self):
        payload = parse_parent_message(cancel_message().decode("utf-8"))
        assert payload["type"] == "cancel"
        parse_cancel_message(payload)

    def test_cancel_with_unknown_fields_fails_closed(self):
        with pytest.raises(WorkerProtocolError):
            parse_cancel_message({"type": "cancel", "extra": 1})

    def test_start_message_bytes_validate(self):
        payload = parse_parent_message(
            start_message(
                session_id="session.task3.001",
                spec=make_spec(),
                run_id="run-1",
                work_dir=r"C:\tmp\work",
                journal_path=r"C:\tmp\journal.jsonl",
                scenario="synthetic_work",
                scenario_params={"steps": 2},
            ).decode("utf-8")
        )
        assert parse_start_request(payload).scenario == "synthetic_work"

    def test_unknown_message_type_fails_closed(self):
        with pytest.raises(WorkerProtocolError):
            parse_parent_message(json.dumps({"type": "explode"}))

    def test_non_object_message_fails_closed(self):
        with pytest.raises(WorkerProtocolError):
            parse_parent_message(json.dumps([1, 2]))
        with pytest.raises(WorkerProtocolError):
            parse_parent_message(json.dumps("start"))

    def test_malformed_json_fails_closed(self):
        with pytest.raises(WorkerProtocolError):
            parse_parent_message("{not json")
        with pytest.raises(WorkerProtocolError):
            parse_parent_message("")

    def test_oversized_message_fails_closed(self):
        from agentic_debugger.application.worker_protocol import serialize_message

        mapping = {"type": "cancel", "pad": "x" * (MAX_WORKER_LINE_BYTES + 10)}
        with pytest.raises(WorkerProtocolError):
            serialize_message(mapping)

    def test_ready_message(self):
        notification = parse_worker_message(
            ready_message(3).decode("utf-8"), make_spec()
        )
        assert notification.kind == "ready"
        assert notification.sequence == 3

    def test_ready_requires_valid_sequence(self):
        with pytest.raises(WorkerProtocolError):
            parse_worker_message('{"type": "ready", "sequence": -1}', make_spec())
        with pytest.raises(WorkerProtocolError):
            parse_worker_message('{"type": "ready"}', make_spec())

    def test_event_notification_carries_only_the_sequence(self):
        notification = parse_worker_message(
            event_notification(7).decode("utf-8"), make_spec()
        )
        assert notification.kind == "event"
        assert notification.sequence == 7
        assert notification.result is None

    def test_event_notification_sequence_is_validated(self):
        with pytest.raises(WorkerProtocolError):
            parse_worker_message('{"type": "event", "sequence": -1}', make_spec())
        with pytest.raises(WorkerProtocolError):
            parse_worker_message('{"type": "event"}', make_spec())
        with pytest.raises(WorkerProtocolError):
            parse_worker_message('{"type": "event", "sequence": "x"}', make_spec())

    def test_full_event_payload_notification_fails_closed(self):
        # Legacy full-event notifications are rejected: the pipe carries
        # sequence notifications only, and the journal is the event authority.
        with pytest.raises(WorkerProtocolError):
            parse_worker_message(
                '{"type": "event", "event": {"schema_version": "session-event-v1"}}',
                make_spec(),
            )

    def test_terminal_message_round_trip(self):
        result = None
        from agentic_debugger.application.session import (
            SessionResult,
            SessionStatus,
            SessionTerminationReason,
        )

        result = SessionResult(
            session_id=SessionId("session.task3.001"),
            spec=make_spec(),
            status=SessionStatus.CANCELLED,
            termination_reason=SessionTerminationReason.CANCELLED,
            run_id="run-1",
            started_at_utc="2026-08-14T00:00:00Z",
            ended_at_utc="2026-08-14T00:00:01Z",
            sequence=7,
            cleanup_verified=True,
            diagnostics=("cleanup verified",),
        )
        notification = parse_worker_message(
            terminal_message(result).decode("utf-8"), make_spec()
        )
        assert notification.kind == "terminal"
        assert notification.result == result

    def test_terminal_mismatched_identity_fails_closed(self):
        from agentic_debugger.application.session import (
            SessionResult,
            SessionStatus,
            SessionTerminationReason,
        )

        result = SessionResult(
            session_id=SessionId("session.task3.001"),
            spec=make_spec(),
            status=SessionStatus.SUCCEEDED,
            termination_reason=SessionTerminationReason.DONE,
            run_id="run-1",
            started_at_utc="2026-08-14T00:00:00Z",
            ended_at_utc="2026-08-14T00:00:01Z",
            sequence=8,
            cleanup_verified=True,
            diagnostics=(),
        )
        mapping = json.loads(terminal_message(result).decode("utf-8"))
        mapping["result"]["task_id"] = "some-other-task"
        with pytest.raises(WorkerProtocolError):
            parse_worker_message(json.dumps(mapping), make_spec())

    def test_fatal_and_error_messages(self):
        for message in (fatal_message("journal_error", ["boom"]), error_message("unknown_scenario", ["x"])):
            notification = parse_worker_message(message.decode("utf-8"), make_spec())
            assert notification.kind in ("fatal", "error")
            assert notification.error_kind in ("journal_error", "unknown_scenario")
            assert notification.diagnostics == ("boom",) or notification.diagnostics == ("x",)

    def test_error_kind_must_be_present(self):
        with pytest.raises(WorkerProtocolError):
            parse_worker_message('{"type": "fatal"}', make_spec())

    def test_unknown_worker_message_type_fails_closed(self):
        with pytest.raises(WorkerProtocolError):
            parse_worker_message('{"type": "nope"}', make_spec())

    def test_liveness_is_typed_and_not_an_event_notification(self):
        notification = parse_worker_message(
            liveness_notification(
                request_index=4,
                request_elapsed_seconds=12.5,
                last_activity_age_seconds=0.25,
                transport_alive=True,
                watchdog_idle_seconds=300.0,
            ).decode("utf-8"),
            make_spec(),
        )
        assert notification.kind == "liveness"
        assert notification.liveness is not None
        assert notification.liveness.request_index == 4

    def test_malformed_liveness_fails_closed(self):
        with pytest.raises(WorkerProtocolError):
            parse_worker_message(
                '{"type":"liveness","request_index":1,"transport_alive":true}',
                make_spec(),
            )


