"""Execution-source contract tests: live-vs-replay rules and protocols."""

from __future__ import annotations

import pytest

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEventKind,
    SourceKind,
)
from agentic_debugger.application.sources import (
    ExecutionSourceSpec,
    SessionEventSink,
    SessionEventSource,
    can_start_new_session,
)
from application_support import VALID_TASK_ID, make_event


class TestSourceKind:
    def test_recorded_kinds(self):
        assert SourceKind.SESSION_BUNDLE.recorded is True
        assert SourceKind.CANONICAL_TRAJECTORY.recorded is True
        assert SourceKind.EXPERIMENT_EVIDENCE.recorded is True
        assert SourceKind.OFFLINE_DEMO.recorded is False
        assert SourceKind.CONFIGURED_MODEL.recorded is False

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            (SourceKind.OFFLINE_DEMO, True),
            (SourceKind.CONFIGURED_MODEL, True),
            (SourceKind.SESSION_BUNDLE, False),
            (SourceKind.CANONICAL_TRAJECTORY, False),
            (SourceKind.EXPERIMENT_EVIDENCE, False),
        ],
    )
    def test_can_start_new_session(self, kind, expected):
        assert can_start_new_session(kind) is expected

    def test_can_start_new_session_rejects_non_kind(self):
        with pytest.raises(ApplicationInputError):
            can_start_new_session("offline_demo")  # type: ignore[arg-type]


class TestExecutionSourceSpec:
    def test_valid_offline_source(self):
        spec = ExecutionSourceSpec(
            kind=SourceKind.OFFLINE_DEMO, task_id=VALID_TASK_ID, policy="static-baseline"
        )
        assert spec.kind is SourceKind.OFFLINE_DEMO
        assert spec.task_id == VALID_TASK_ID
        assert spec.policy == "static-baseline"
        assert spec.model_config_ref is None

    def test_valid_configured_model_source(self):
        spec = ExecutionSourceSpec(
            kind=SourceKind.CONFIGURED_MODEL,
            task_id=VALID_TASK_ID,
            model_config_ref="profiles/local.json",
        )
        assert spec.model_config_ref == "profiles/local.json"

    def test_recorded_kind_cannot_start(self):
        with pytest.raises(ApplicationInputError):
            ExecutionSourceSpec(
                kind=SourceKind.SESSION_BUNDLE, task_id=VALID_TASK_ID
            )

    def test_configured_model_requires_config_ref(self):
        with pytest.raises(ApplicationInputError):
            ExecutionSourceSpec(kind=SourceKind.CONFIGURED_MODEL, task_id=VALID_TASK_ID)

    def test_offline_source_rejects_config_ref(self):
        with pytest.raises(ApplicationInputError):
            ExecutionSourceSpec(
                kind=SourceKind.OFFLINE_DEMO,
                task_id=VALID_TASK_ID,
                model_config_ref="profiles/local.json",
            )

    def test_credential_shaped_config_ref_rejected(self):
        with pytest.raises(ApplicationInputError):
            ExecutionSourceSpec(
                kind=SourceKind.CONFIGURED_MODEL,
                task_id=VALID_TASK_ID,
                model_config_ref="token=abc123",
            )

    def test_invalid_task_id_rejected(self):
        with pytest.raises(ApplicationInputError):
            ExecutionSourceSpec(kind=SourceKind.OFFLINE_DEMO, task_id="")

    def test_to_mapping_round_trip(self):
        spec = ExecutionSourceSpec(
            kind=SourceKind.OFFLINE_DEMO, task_id=VALID_TASK_ID, policy="static-baseline"
        )
        assert spec.to_mapping() == {
            "kind": "offline_demo",
            "task_id": VALID_TASK_ID,
            "policy": "static-baseline",
            "model_config_ref": None,
        }


class _ListSource:
    """Minimal conforming in-memory SessionEventSource double."""

    def __init__(self, events, kind=SourceKind.OFFLINE_DEMO):
        self._events = list(events)
        self._kind = kind
        self._closed = False

    @property
    def source_kind(self):
        return self._kind

    def next_event(self):
        if self._closed or not self._events:
            return None
        return self._events.pop(0)

    def close(self):
        self._closed = True


class _ListSink:
    """Minimal conforming in-memory SessionEventSink double."""

    def __init__(self):
        self.events = []
        self.closed = False

    def append(self, event):
        if self.closed:
            raise ApplicationInputError("sink is closed")
        self.events.append(event)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class TestProtocolContracts:
    def test_source_protocol_structural(self):
        source = _ListSource([])
        assert isinstance(source, SessionEventSource)

    def test_source_protocol_rejects_unrelated(self):
        assert not isinstance(object(), SessionEventSource)

    def test_source_returns_ordered_events_then_none(self):
        events = [
            make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0),
            make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id="run-1"),
        ]
        source = _ListSource(events)
        assert source.next_event() == events[0]
        assert source.next_event() == events[1]
        assert source.next_event() is None

    def test_sink_protocol_structural(self):
        sink = _ListSink()
        assert isinstance(sink, SessionEventSink)

    def test_sink_append_and_close(self):
        sink = _ListSink()
        event = make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64})
        sink.append(event)
        assert sink.events == [event]
        sink.flush()
        sink.close()
        with pytest.raises(ApplicationInputError):
            sink.append(event)

    def test_replay_cursor_is_read_only_by_contract(self):
        # Replay sources are labeled with a recorded kind and never enter the
        # live-start workflow.
        source = _ListSource([], kind=SourceKind.CANONICAL_TRAJECTORY)
        assert isinstance(source, SessionEventSource)
        assert source.source_kind.recorded is True
        assert can_start_new_session(source.source_kind) is False
