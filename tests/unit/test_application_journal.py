import json

import pytest

from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
    validate_session_event_stream,
)
from agentic_debugger.application.journal import (
    JournalClosedError,
    JournalError,
    JournalReadState,
    MAX_JOURNAL_RECORD_BYTES,
    SessionEventJournal,
    read_session_journal,
)

SESSION_ID = "session.journal.001"
TASK_ID = "curated-off-by-one-002"
SOURCE_KIND = SourceKind.OFFLINE_DEMO


def make_event(sequence, kind=SessionEventKind.SESSION_CREATED, payload=None, run_id=None):
    if payload is None:
        payload = {"spec_fingerprint": "a" * 64} if kind is SessionEventKind.SESSION_CREATED else {}
    return SessionEvent(
        schema_version=SESSION_EVENT_SCHEMA_VERSION,
        session_id=SESSION_ID,
        task_id=TASK_ID,
        run_id=run_id,
        sequence=sequence,
        timestamp_utc="2026-08-14T00:00:00Z",
        source_kind=SOURCE_KIND,
        event_kind=kind,
        controller_phase=None,
        payload=payload,
    )


def make_terminal(status, reason, sequence, run_id=None):
    return make_event(
        sequence,
        SessionEventKind.SESSION_COMPLETED,
        {"status": status, "termination_reason": reason},
        run_id=run_id,
    )


class TestSessionEventJournal:
    def test_append_flushes_and_is_readable_before_close(self, tmp_path):
        path = tmp_path / "events.jsonl"
        journal = SessionEventJournal(
            path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND
        )
        journal.append(make_event(0))
        journal.append(make_event(1, SessionEventKind.SESSION_STARTED, run_id="run-1"))
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event_kind"] == "session.created"
        assert json.loads(lines[1])["run_id"] == "run-1"
        journal.close()

    def test_sequence_must_be_contiguous(self, tmp_path):
        journal = SessionEventJournal(
            tmp_path / "events.jsonl",
            session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND,
        )
        journal.append(make_event(0))
        with pytest.raises(JournalError):
            journal.append(make_event(2))
        journal.close()

    def test_identity_is_enforced(self, tmp_path):
        journal = SessionEventJournal(
            tmp_path / "events.jsonl",
            session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND,
        )
        other_session = SessionEvent(
            schema_version=SESSION_EVENT_SCHEMA_VERSION,
            session_id="other.session",
            task_id=TASK_ID,
            run_id=None,
            sequence=0,
            timestamp_utc="2026-08-14T00:00:00Z",
            source_kind=SOURCE_KIND,
            event_kind=SessionEventKind.SESSION_CREATED,
            controller_phase=None,
            payload={"spec_fingerprint": "a" * 64},
        )
        with pytest.raises(JournalError):
            journal.append(other_session)
        journal.close()

    def test_non_event_rejected(self, tmp_path):
        journal = SessionEventJournal(
            tmp_path / "events.jsonl",
            session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND,
        )
        with pytest.raises(JournalError):
            journal.append({"not": "an event"})
        journal.close()

    def test_exclusive_create(self, tmp_path):
        path = tmp_path / "events.jsonl"
        SessionEventJournal(path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND).close()
        with pytest.raises(JournalError):
            SessionEventJournal(path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND)

    def test_closed_journal_rejects_appends(self, tmp_path):
        journal = SessionEventJournal(
            tmp_path / "events.jsonl",
            session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND,
        )
        journal.close()
        with pytest.raises(JournalClosedError):
            journal.append(make_event(0))

    def test_missing_parent_directory_fails_closed(self, tmp_path):
        with pytest.raises(JournalError):
            SessionEventJournal(
                tmp_path / "missing" / "events.jsonl",
                session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND,
            )

    def test_write_failure_propagates(self, tmp_path):
        journal = SessionEventJournal(
            tmp_path / "events.jsonl",
            session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND,
        )
        journal.close()
        # closing twice is idempotent
        journal.close()
        # a closed stream cannot fsync a second time via append
        with pytest.raises(JournalError):
            journal.append(make_event(0))


class TestReadSessionJournal:
    def test_missing_journal(self, tmp_path):
        result = read_session_journal(tmp_path / "nope.jsonl")
        assert result.state is JournalReadState.MISSING
        assert result.is_success is False

    def test_complete_stream_classified_complete(self, tmp_path):
        path = tmp_path / "events.jsonl"
        journal = SessionEventJournal(path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND)
        journal.append(make_event(0))
        journal.append(make_event(1, SessionEventKind.SESSION_STARTED, run_id="run-1"))
        journal.append(make_event(2, SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, run_id="run-1"))
        journal.append(make_event(3, SessionEventKind.CLEANUP_STARTED, run_id="run-1"))
        journal.append(make_event(4, SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, run_id="run-1"))
        journal.append(make_terminal("succeeded", "done", 5, run_id="run-1"))
        journal.close()
        result = read_session_journal(path)
        assert result.state is JournalReadState.COMPLETE
        assert result.is_success is True
        validate_session_event_stream(result.events)

    def test_interrupted_prefix_never_success(self, tmp_path):
        path = tmp_path / "events.jsonl"
        journal = SessionEventJournal(path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND)
        journal.append(make_event(0))
        journal.append(make_event(1, SessionEventKind.SESSION_STARTED, run_id="run-1"))
        journal.close()
        result = read_session_journal(path)
        assert result.state is JournalReadState.INTERRUPTED
        assert result.is_success is False
        assert len(result.events) == 2

    def test_truncated_tail_line_is_interrupted_and_preserves_prefix(self, tmp_path):
        path = tmp_path / "events.jsonl"
        journal = SessionEventJournal(path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND)
        journal.append(make_event(0))
        journal.append(make_event(1, SessionEventKind.SESSION_STARTED, run_id="run-1"))
        journal.close()
        # simulate a crash mid-write: append a partial line without newline
        with open(path, "a", encoding="utf-8") as stream:
            stream.write('{"schema_version": "session-')
        result = read_session_journal(path)
        assert result.state is JournalReadState.INTERRUPTED
        assert result.is_success is False
        assert "truncated crash tail" in (result.error or "")
        # every previously validated event is preserved
        assert [event.sequence for event in result.events] == [0, 1]
        assert result.events[0].event_kind.value == "session.created"

    def test_complete_valid_record_without_newline_is_interrupted(self, tmp_path):
        path = tmp_path / "events.jsonl"
        journal = SessionEventJournal(path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND)
        journal.append(make_event(0))
        journal.close()
        # a complete record whose terminating newline was never written
        line = json.dumps(make_event(1, SessionEventKind.SESSION_STARTED, run_id="run-1").to_mapping())
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(line)  # no trailing newline
        result = read_session_journal(path)
        assert result.state is JournalReadState.INTERRUPTED
        assert result.is_success is False
        assert [event.sequence for event in result.events] == [0]

    def test_malformed_newline_terminated_final_line_is_malformed(self, tmp_path):
        path = tmp_path / "events.jsonl"
        journal = SessionEventJournal(path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND)
        journal.append(make_event(0))
        journal.close()
        # a complete, newline-terminated record that is not a valid event
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("not-json\n")
        result = read_session_journal(path)
        assert result.state is JournalReadState.MALFORMED
        assert result.is_success is False
        assert "truncated crash tail" not in (result.error or "")
        # the validated prefix is preserved
        assert [event.sequence for event in result.events] == [0]

    def test_oversized_line_is_malformed(self, tmp_path):
        path = tmp_path / "events.jsonl"
        journal = SessionEventJournal(path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND)
        journal.append(make_event(0))
        journal.close()
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("x" * (MAX_JOURNAL_RECORD_BYTES + 1) + "\n")
        result = read_session_journal(path)
        assert result.state is JournalReadState.MALFORMED
        assert result.is_success is False

    def test_large_locals_event_survives_journal_round_trip(self, tmp_path):
        """A valid Task-1 event well above 64 KiB persists and reads back."""
        from agentic_debugger.application.events import SESSION_EVENT_SCHEMA_VERSION

        locals_records = [
            {"name": f"var_{index:03d}", "summary": "value-" + "x" * 280}
            for index in range(512)
        ]
        large = SessionEvent(
            schema_version=SESSION_EVENT_SCHEMA_VERSION,
            session_id=SESSION_ID,
            task_id=TASK_ID,
            run_id="run-1",
            sequence=3,
            timestamp_utc="2026-08-14T00:00:00Z",
            source_kind=SOURCE_KIND,
            event_kind=SessionEventKind.DEBUGGER_LOCALS_OBSERVED,
            controller_phase=None,
            payload={"pause_generation": 0, "locals": locals_records},
        )
        serialized = len(json.dumps(large.to_mapping(), ensure_ascii=False))
        assert serialized > 64 * 1024
        path = tmp_path / "events.jsonl"
        journal = SessionEventJournal(path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND)
        journal.append(make_event(0))
        journal.append(make_event(1, SessionEventKind.SESSION_STARTED, run_id="run-1"))
        journal.append(make_event(2, SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, run_id="run-1"))
        journal.append(large)
        journal.append(make_event(4, SessionEventKind.CLEANUP_STARTED, run_id="run-1"))
        journal.append(make_event(5, SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, run_id="run-1"))
        journal.append(make_terminal("succeeded", "done", 6, run_id="run-1"))
        journal.close()
        result = read_session_journal(path)
        assert result.state is JournalReadState.COMPLETE
        assert result.is_success is True
        assert len(result.events) == 7
        assert result.events[3].to_mapping() == large.to_mapping()

    def test_malformed_middle_line_is_malformed(self, tmp_path):
        path = tmp_path / "events.jsonl"
        journal = SessionEventJournal(path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND)
        journal.append(make_event(0))
        journal.close()
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("not-json\n")
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(make_event(2).to_mapping()) + "\n")
        result = read_session_journal(path)
        assert result.state is JournalReadState.MALFORMED
        assert result.is_success is False

    def test_invalid_stream_with_terminal_is_malformed(self, tmp_path):
        path = tmp_path / "events.jsonl"
        journal = SessionEventJournal(path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND)
        journal.append(make_event(0))
        # two terminals: the stream validator must reject
        journal.append(make_terminal("succeeded", "done", 1))
        journal.append(make_terminal("succeeded", "done", 2))
        journal.close()
        result = read_session_journal(path)
        assert result.state is JournalReadState.MALFORMED
        assert result.is_success is False

    def test_invalid_prefix_is_malformed(self, tmp_path):
        path = tmp_path / "events.jsonl"
        # written directly: the journal writer itself enforces contiguity,
        # so a non-contiguous prefix can only arrive via corruption
        line = json.dumps(make_event(1).to_mapping()) + "\n"
        path.write_text(line, encoding="utf-8")
        result = read_session_journal(path)
        assert result.state is JournalReadState.MALFORMED
        assert result.is_success is False

    def test_empty_journal_is_interrupted(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.write_text("", encoding="utf-8")
        result = read_session_journal(path)
        assert result.state is JournalReadState.INTERRUPTED
        assert result.is_success is False

    def test_cancelled_stream_with_verified_cleanup_is_complete(self, tmp_path):
        path = tmp_path / "events.jsonl"
        journal = SessionEventJournal(path, session_id=SESSION_ID, task_id=TASK_ID, source_kind=SOURCE_KIND)
        journal.append(make_event(0))
        journal.append(make_event(1, SessionEventKind.SESSION_STARTED, run_id="run-1"))
        journal.append(make_event(2, SessionEventKind.SESSION_CANCEL_REQUESTED, run_id="run-1"))
        journal.append(make_event(3, SessionEventKind.CLEANUP_STARTED, run_id="run-1"))
        journal.append(make_event(4, SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, run_id="run-1"))
        journal.append(make_event(
            5,
            SessionEventKind.SESSION_CANCELLED,
            {"status": "cancelled", "termination_reason": "cancelled"},
            run_id="run-1",
        ))
        journal.close()
        result = read_session_journal(path)
        assert result.state is JournalReadState.COMPLETE
        validate_session_event_stream(result.events)
