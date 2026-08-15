"""One shared application-owned event emission authority.

Repair Pass 3 (Blockers 1 and 4): every live producer -- the Task-2
controller adapter, the Task-4 debugger/source/patch observability producer,
and the Task-4 verifier adapter -- must compose into exactly one
authoritative ``SessionEvent`` sequence per application session.  Each of
them used to own an independent counter starting at ``context.initial_sequence``,
so they could never form one real live journal.

:class:`SessionEventEmitter` is that single authority.  It owns:

- session/task/source identity (fail-closed on construction);
- the optional ``run_id`` (bindable once through :meth:`bind_run_id`, the
  same binding the session lifecycle performs at ``session.started``);
- the clock (injectable for deterministic tests);
- the next sequence (contiguous from ``initial_sequence``; producers never
  guess or share another producer's next sequence);
- the optional authoritative sink (a Task-3 :class:`SessionEventJournal` or
  any Task-1 :class:`SessionEventSink`).

Journal-failure visibility (Task-3 rule): when the authoritative sink
rejects an event, the emitter records a sticky fatal state
(``fatal``/``fatal_error``) and raises :class:`EmitterFatalError`.  A
Task-2-style observer layer may swallow ordinary exceptions, but it cannot
make a journal failure disappear: the sticky state stays observable to the
session owner, and every later emission fails fast.  The session owner must
check ``fatal`` and fail the application session honestly instead of
continuing as if the evidence were durable.  Producers with no journal (a
``sink=None`` emitter) remain best-effort and non-invasive.

Standalone producer construction keeps working: an adapter without a shared
emitter creates its own private emitter (starting at
``context.initial_sequence``), so isolated producer tests stay possible
without compromising integrated behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from agentic_debugger.application import (
    ApplicationContractError,
    ApplicationError,
    ApplicationInputError,
)
from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
    validate_session_id,
    validate_utc_timestamp,
)
from agentic_debugger.application.sources import SessionEventSink

_MAX_TASK_ID_BYTES = 256
_MAX_RUN_ID_BYTES = 256


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_identifier(value: Any, label: str, max_chars: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ApplicationInputError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ApplicationInputError(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise ApplicationInputError(f"{label} exceeds the {max_chars}-byte bound")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ApplicationInputError(f"{label} contains control characters")
    return value


def _validated_clock(clock: Callable[[], str]) -> Callable[[], str]:
    if not callable(clock):
        raise ApplicationInputError("clock must be callable")
    try:
        validate_utc_timestamp(clock())
    except Exception as exc:
        raise ApplicationInputError("clock must produce UTC timestamps") from exc
    return clock


class EmitterFatalError(ApplicationError):
    """The authoritative sink rejected an event; the session must fail.

    The emitter records the sticky ``fatal``/``fatal_error`` state before
    raising, so even an observer layer that swallows the exception cannot
    hide the journal failure from the session owner.
    """


class SessionEventEmitter:
    """One session's shared sequence/identity/clock authority and sink gate.

    Every event produced through this emitter carries the emitter's identity
    and the next contiguous sequence.  The optional sink is the durable
    authority: an append failure becomes the sticky fatal state described in
    the module docstring.
    """

    def __init__(
        self,
        *,
        session_id: str,
        task_id: str,
        source_kind: SourceKind,
        run_id: Optional[str] = None,
        clock: Callable[[], str] | None = None,
        sink: SessionEventSink | None = None,
        initial_sequence: int = 0,
    ) -> None:
        try:
            validate_session_id(session_id)
        except Exception as exc:
            raise ApplicationInputError(
                f"invalid session id: {session_id!r}"
            ) from exc
        self._session_id = session_id
        self._task_id = _bounded_identifier(task_id, "task_id", _MAX_TASK_ID_BYTES)
        if type(source_kind) is not SourceKind:
            raise ApplicationInputError("source_kind must be a SourceKind")
        self._source_kind = source_kind
        if run_id is not None:
            run_id = _bounded_identifier(run_id, "run_id", _MAX_RUN_ID_BYTES)
        self._run_id = run_id
        self._run_id_bound = run_id is not None
        self._clock = _validated_clock(clock) if clock is not None else _default_clock
        self._sink = sink
        if type(initial_sequence) is not int or isinstance(
            initial_sequence, bool
        ) or initial_sequence < 0:
            raise ApplicationInputError("initial_sequence must be a non-negative integer")
        self._next_sequence = initial_sequence
        self._fatal = False
        self._fatal_error: Optional[str] = None

    # -- identity ------------------------------------------------------------

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
    def run_id(self) -> Optional[str]:
        return self._run_id

    def bind_run_id(self, run_id: str) -> None:
        """Bind (or confirm) the session run identity; conflicts fail closed.

        Binding is a one-time operation per distinct value: rebinding the
        same value is a no-op, while a different value after binding is an
        identity conflict.
        """
        validated = _bounded_identifier(run_id, "run_id", _MAX_RUN_ID_BYTES)
        if self._run_id_bound:
            if validated != self._run_id:
                raise ApplicationContractError(
                    f"run id conflict: already bound to {self._run_id!r}, "
                    f"cannot bind {validated!r}"
                )
            return
        self._run_id = validated
        self._run_id_bound = True

    # -- sequence ------------------------------------------------------------

    @property
    def next_sequence(self) -> int:
        """The sequence the next emission will carry (the journal's next)."""
        return self._next_sequence

    @property
    def last_sequence(self) -> int:
        """The sequence of the last accepted emission (-1 before any)."""
        return self._next_sequence - 1

    # -- fatal sink state ----------------------------------------------------

    @property
    def fatal(self) -> bool:
        """Whether the authoritative sink rejected an event (sticky)."""
        return self._fatal

    @property
    def fatal_error(self) -> Optional[str]:
        """Bounded diagnostic of the sink failure, if one occurred."""
        return self._fatal_error

    # -- emission ------------------------------------------------------------

    def emit(
        self,
        kind: SessionEventKind,
        payload: Mapping[str, Any],
        *,
        controller_phase: Any = None,
    ) -> SessionEvent:
        """Assign the next sequence, validate, and durably append one event.

        The event is built with the emitter's identity and next sequence,
        appended to the authoritative sink when one is configured, and only
        then is the sequence advanced.  A sink failure marks the sticky fatal
        state and raises :class:`EmitterFatalError`; the sequence is not
        advanced for a rejected event.
        """
        if type(kind) is not SessionEventKind:
            raise ApplicationInputError("kind must be a SessionEventKind")
        if self._fatal:
            raise EmitterFatalError(
                "emitter is fatal after an authoritative sink failure: "
                f"{self._fatal_error}"
            )
        event = SessionEvent(
            schema_version=SESSION_EVENT_SCHEMA_VERSION,
            session_id=self._session_id,
            task_id=self._task_id,
            run_id=self._run_id,
            sequence=self._next_sequence,
            timestamp_utc=self._clock(),
            source_kind=self._source_kind,
            event_kind=kind,
            controller_phase=controller_phase,
            payload=dict(payload),
        )
        if self._sink is not None:
            try:
                self._sink.append(event)
            except Exception as exc:
                self._fatal = True
                self._fatal_error = _bounded_diagnostic(
                    f"sink append failed for sequence {event.sequence}: {exc}"
                )
                raise EmitterFatalError(
                    f"authoritative sink rejected event {event.sequence}: "
                    f"{self._fatal_error}"
                ) from exc
        self._next_sequence += 1
        return event


def _bounded_diagnostic(text: str) -> str:
    cleaned = "".join(
        char if 0x20 <= ord(char) != 0x7F else " " for char in str(text)
    )
    if len(cleaned) > 400:
        cleaned = cleaned[:397] + "..."
    return cleaned or "unspecified"


__all__ = [
    "EmitterFatalError",
    "SessionEventEmitter",
]
