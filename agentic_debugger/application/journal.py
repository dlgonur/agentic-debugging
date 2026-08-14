"""Crash-durable Task-3 session event journal.

The journal is the durable evidence authority of one application session:
the worker is the single writer and single sequence owner, and every record
is the exact JSON mapping of one Task-1 :class:`SessionEvent` (no extra
envelope fields are invented).  Writes are flushed and ``fsync``-ed per
record using the proven R6 lifecycle pattern.

Failure semantics (authoritative):

- exclusive file creation so a journal can never be silently reopened or
  overwritten;
- an append/flush/fsync failure raises :class:`JournalError`; the journal is
  then incomplete/failed and must never be upgraded to success;
- the reader classifies a journal as ``complete`` (a valid full
  ``SessionEvent`` stream), ``interrupted`` (valid readable prefix without a
  terminal event, including a truncated trailing line left by a crash), or
  ``malformed`` (corruption that is not a simple crash tail).  Only
  ``complete`` may represent success.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional, Sequence

from agentic_debugger.application import ApplicationError
from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    SourceKind,
    validate_session_event_stream,
)

#: Maximum serialized size of one journal record.
#:
#: The bound is derived from the largest valid Task-1 ``SessionEvent``
#: shape: ``debugger.locals_observed`` may carry up to 512 locals, each
#: with a 256-byte name and a 4096-byte summary; worst-case JSON escaping
#: roughly doubles that, giving ~4.5 MiB.  ``8 MiB`` covers every valid
#: Task-1 event with margin while still failing closed on data outside the
#: accepted contract.  The writer and reader agree on this exact bound.
MAX_JOURNAL_RECORD_BYTES = 8 * 1024 * 1024

_TERMINAL_KINDS = frozenset(
    {
        SessionEventKind.SESSION_COMPLETED,
        SessionEventKind.SESSION_FAILED,
        SessionEventKind.SESSION_CANCELLED,
    }
)


class JournalError(ApplicationError):
    """Raised when the journal cannot append/flush/fsync or is already closed."""


class JournalClosedError(JournalError):
    """Raised when an append is attempted on a closed journal."""


class SessionEventJournal:
    """Single-writer, crash-durable sink of exact ``SessionEvent`` mappings.

    Implements the Task-1 :class:`SessionEventSink` contract: appends enforce
    contiguous sequences and constant session/task/source identity, and
    every record is flushed and ``fsync``-ed before the next one is written.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        session_id: str,
        task_id: str,
        source_kind: SourceKind,
    ) -> None:
        self._path = str(path)
        self._session_id = session_id
        self._task_id = task_id
        self._source_kind = source_kind
        self._next_sequence = 0
        self._lock = threading.Lock()
        self._closed = False
        try:
            self._stream = open(
                self._path, "x", encoding="utf-8", newline="\n"
            )
        except OSError as exc:
            raise JournalError(
                f"cannot create journal exclusively at {self._path!r}: {exc}"
            ) from exc

    @property
    def path(self) -> str:
        return self._path

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def source_kind(self) -> SourceKind:
        return self._source_kind

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    def append(self, event: SessionEvent) -> None:
        """Durably append one validated event; fail closed on any violation."""
        if type(event) is not SessionEvent:
            raise JournalError("journal append requires a SessionEvent")
        if event.sequence != self._next_sequence:
            raise JournalError(
                f"non-contiguous journal sequence: expected "
                f"{self._next_sequence}, got {event.sequence}"
            )
        if event.session_id != self._session_id:
            raise JournalError("journal event session_id does not match the journal")
        if event.task_id != self._task_id:
            raise JournalError("journal event task_id does not match the journal")
        if event.source_kind is not self._source_kind:
            raise JournalError("journal event source_kind does not match the journal")
        with self._lock:
            if self._closed:
                raise JournalClosedError("journal is closed")
            try:
                encoded = json.dumps(
                    event.to_mapping(),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise JournalError(
                    f"journal event is not JSON-serializable: {exc}"
                ) from exc
            if len(encoded.encode("utf-8")) > MAX_JOURNAL_RECORD_BYTES:
                raise JournalError(
                    f"serialized event exceeds MAX_JOURNAL_RECORD_BYTES "
                    f"({len(encoded.encode('utf-8'))} > {MAX_JOURNAL_RECORD_BYTES})"
                )
            try:
                self._stream.write(encoded + "\n")
                self._stream.flush()
                os.fsync(self._stream.fileno())
            except OSError as exc:
                raise JournalError(
                    f"journal write/fsync failed at {self._path!r}: {exc}"
                ) from exc
            self._next_sequence += 1

    def flush(self) -> None:
        """Flush and ``fsync`` buffered state; fails closed on error."""
        with self._lock:
            if self._closed:
                raise JournalClosedError("journal is closed")
            try:
                self._stream.flush()
                os.fsync(self._stream.fileno())
            except OSError as exc:
                raise JournalError(
                    f"journal flush/fsync failed at {self._path!r}: {exc}"
                ) from exc

    def close(self) -> None:
        """Flush, ``fsync``, and close the journal; idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._stream.flush()
                os.fsync(self._stream.fileno())
            except OSError as exc:
                raise JournalError(
                    f"journal final flush/fsync failed at {self._path!r}: {exc}"
                ) from exc
            finally:
                try:
                    self._stream.close()
                except OSError:
                    pass


class JournalReadState(str, Enum):
    """Classification of one journal file."""

    COMPLETE = "complete"
    INTERRUPTED = "interrupted"
    MALFORMED = "malformed"
    MISSING = "missing"


@dataclass(frozen=True)
class JournalReadResult:
    """One journal classification with its validated event prefix."""

    state: JournalReadState
    events: tuple[SessionEvent, ...] = ()
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        """Only a complete, valid stream may represent success."""
        return self.state is JournalReadState.COMPLETE


def read_session_journal(path: str | os.PathLike[str]) -> JournalReadResult:
    """Classify one session journal file without reordering it.

    Rules:

    - missing file -> ``missing``;
    - any line longer than :data:`MAX_JOURNAL_RECORD_BYTES` is outside the
      accepted contract -> ``malformed`` (a crash mid-write can never
      exceed the writer's own record bound);
    - a final record WITHOUT a terminating newline is a crash tail ->
      ``interrupted``, preserving every previously validated event;
    - a newline-terminated record that cannot be parsed as an exact
      ``SessionEvent`` mapping -> ``malformed`` (never a crash tail);
    - a valid event list ending in a terminal event must satisfy the Task-1
      complete-stream contract -> ``complete``, otherwise ``malformed``;
    - a valid prefix without a terminal event must still satisfy the prefix
      rules (created first, contiguous sequences, constant identity) ->
      ``interrupted``, otherwise ``malformed``.
    """
    journal_path = str(path)
    if not os.path.isfile(journal_path):
        return JournalReadResult(JournalReadState.MISSING)
    try:
        with open(journal_path, "r", encoding="utf-8", newline="\n") as stream:
            raw_lines = stream.readlines()
    except OSError as exc:
        return JournalReadResult(
            JournalReadState.MALFORMED, error=f"cannot read journal: {exc}"
        )

    events: List[SessionEvent] = []
    for index, raw in enumerate(raw_lines):
        terminated = raw.endswith("\n")
        line = raw[:-1] if terminated else raw
        is_final = index == len(raw_lines) - 1
        if len(line.encode("utf-8", errors="replace")) > MAX_JOURNAL_RECORD_BYTES:
            return JournalReadResult(
                JournalReadState.MALFORMED,
                events=tuple(events),
                error="line exceeds the journal record bound",
            )
        try:
            mapping = json.loads(line)
            parsed = SessionEvent.from_mapping(mapping)
        except Exception as exc:
            if not terminated and is_final:
                return JournalReadResult(
                    JournalReadState.INTERRUPTED,
                    events=tuple(events),
                    error=f"invalid journal line: {exc} (truncated crash tail)",
                )
            return JournalReadResult(
                JournalReadState.MALFORMED,
                events=tuple(events),
                error=f"invalid journal line: {exc}",
            )
        if not terminated and is_final:
            # The record parsed but lacks its terminating newline: the
            # write did not complete, so it is not durably terminated and
            # is excluded from the preserved prefix.
            return JournalReadResult(
                JournalReadState.INTERRUPTED,
                events=tuple(events),
                error="final record lacks a terminating newline (crash tail)",
            )
        events.append(parsed)

    if not events:
        return JournalReadResult(
            JournalReadState.INTERRUPTED,
            error="journal is empty (no durable events)",
        )

    if events[-1].event_kind in _TERMINAL_KINDS:
        try:
            validate_session_event_stream(events)
        except Exception as exc:
            return JournalReadResult(
                JournalReadState.MALFORMED,
                events=tuple(events),
                error=f"journal stream is invalid: {exc}",
            )
        return JournalReadResult(JournalReadState.COMPLETE, events=tuple(events))

    prefix_error = _validate_prefix(events)
    if prefix_error is not None:
        return JournalReadResult(
            JournalReadState.MALFORMED,
            events=tuple(events),
            error=prefix_error,
        )
    return JournalReadResult(
        JournalReadState.INTERRUPTED,
        events=tuple(events),
        error="journal ends without a terminal event",
    )


def _validate_prefix(events: Sequence[SessionEvent]) -> Optional[str]:
    """Validate a terminal-less journal prefix stays internally coherent."""
    if events[0].event_kind is not SessionEventKind.SESSION_CREATED:
        return "journal prefix does not begin with session.created"
    session_id = events[0].session_id
    task_id = events[0].task_id
    source_kind = events[0].source_kind
    started = False
    for index, event in enumerate(events):
        if event.sequence != index:
            return f"non-contiguous sequence at event {index}"
        if event.session_id != session_id:
            return "mixed session IDs in journal prefix"
        if event.task_id != task_id:
            return "mixed task IDs in journal prefix"
        if event.source_kind is not source_kind:
            return "mixed source kinds in journal prefix"
        if started and event.run_id is None:
            return f"run_id missing after session.started at event {index}"
        if event.event_kind is SessionEventKind.SESSION_STARTED:
            if started:
                return "duplicate session.started in journal prefix"
            if event.run_id is None:
                return "session.started requires a run_id"
            started = True
    return None


__all__ = [
    "JournalClosedError",
    "JournalError",
    "JournalReadResult",
    "JournalReadState",
    "MAX_JOURNAL_RECORD_BYTES",
    "SessionEventJournal",
    "read_session_journal",
]
