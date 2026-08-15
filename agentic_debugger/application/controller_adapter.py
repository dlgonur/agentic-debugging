"""Application-side adapter for controller-owned observations.

This module converts controller-native :class:`ControllerObservation`
records into Task-1 :class:`SessionEvent` values while the controller is
still running.  It is the Task-2 observability boundary only.

Boundary rules (see the Task-2 accepted corrections):

- This adapter maps only facts the controller authoritatively owns:
  model request boundaries, accepted/rejected directives, tool dispatch,
  and completed controller steps.  It never fabricates PDB, patch,
  verifier, cleanup, or application-session terminal events.
- It is NOT the session lifecycle owner.  ``session.created``,
  ``session.started``, cleanup events, and session terminal events belong
  to later worker/session/source tasks.  A controller terminal outcome is
  not an application-session terminal outcome.
- The produced output is therefore a valid incremental ``SessionEvent``
  prefix, not a complete stream that satisfies
  :func:`validate_session_event_stream` by itself.
- Identity is fail-closed: every observation must belong to the
  configured task (and, when the context knows the run, to that run).
  Run identity is taken from the authoritative ``RUN_STARTED``
  observation when the context does not pre-declare it; it is never
  derived from ordering or guessed.
- Only live-startable Task-1 source kinds (``offline_demo``,
  ``configured_model``) are accepted: this adapter represents a live
  controller execution and must never carry replay provenance.  The
  Task-1 authority ``can_start_new_session`` is reused for this gate.
- When the adapter context declares ``run_id``, produced events carry it
  and every observation must match it.  Without a declared ``run_id`` the
  adapter binds the authoritative run identity from the ``RUN_STARTED``
  observation and every subsequently produced event carries that bound
  run id, so the prefix remains composable with lifecycle events that
  bind the same run id at ``session.started``.

Import note: this module depends on the controller observer vocabulary,
so importing it loads ``agentic_debugger.agent`` observation types.  It is
deliberately NOT imported by ``agentic_debugger/application/__init__.py``
or the Task-1 contract modules, preserving the Task-1 import-isolation
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from agentic_debugger.agent.controller import ControllerStopReason
from agentic_debugger.agent.model_adapter import ModelDirectiveKind
from agentic_debugger.agent.observer import (
    ControllerObservation,
    ControllerObservationKind,
    ControllerObserver,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application import ApplicationContractError, ApplicationInputError
from agentic_debugger.application.events import (
    ModelRequestStatus,
    SessionEvent,
    SessionEventKind,
    SourceKind,
    validate_session_id,
    validate_utc_timestamp,
)
from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.sources import SessionEventSink, can_start_new_session

_MAX_TASK_ID_BYTES = 256
_MAX_RUN_ID_BYTES = 256
_MAX_SHORT_TEXT_BYTES = 64
_MAX_NAME_BYTES = 256

_VALID_DIRECTIVE_KINDS = frozenset(candidate.value for candidate in ModelDirectiveKind)
_VALID_STOP_REASONS = frozenset(candidate.value for candidate in ControllerStopReason)
_VALID_REQUEST_STATUSES = frozenset(
    {
        ModelRequestStatus.OK.value,
        ModelRequestStatus.ERROR.value,
    }
)
#: ``RUN_STARTED`` and ``TERMINAL`` only track the controller phase; every
#: other observation kind produces a Task-1 event (Task-4 promotes
#: ``STATE_TRANSITION`` into a ``controller.transition`` event carrying the
#: recorded source/target states and reason).
_NON_EVENT_KINDS = frozenset(
    {
        ControllerObservationKind.RUN_STARTED,
        ControllerObservationKind.TERMINAL,
    }
)

#: The controller observer allows transition reasons up to 2048 bytes; the
#: ``controller.transition`` payload bound (MAX_TEXT_CHARS) covers it.
_MAX_TRANSITION_REASON_BYTES = 2048


def _bounded_text(value: Any, label: str, max_chars: int) -> str:
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


def _bounded_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, label, max_chars)


def _nonneg_int(value: Any, label: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise ApplicationInputError(f"{label} must be a non-negative integer")
    return value


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_clock(clock: Callable[[], str]) -> Callable[[], str]:
    if not callable(clock):
        raise ApplicationInputError("clock must be callable")
    try:
        validate_utc_timestamp(clock())
    except Exception as exc:
        raise ApplicationInputError("clock must produce UTC timestamps") from exc
    return clock


@dataclass(frozen=True)
class ControllerObservationContext:
    """Immutable application context for one adapted controller run.

    Only live-startable Task-1 source kinds are accepted (the replay-only
    kinds never enter a live controller execution).  ``run_id`` is
    optional: when declared, produced events carry it and every
    observation must match it.  When absent, run identity is bound from
    the authoritative ``RUN_STARTED`` observation (validated, never
    guessed) and every subsequently produced event carries that bound
    run id.
    """

    session_id: str
    task_id: str
    source_kind: SourceKind
    run_id: Optional[str] = None
    initial_sequence: int = 0

    def __post_init__(self) -> None:
        try:
            validate_session_id(self.session_id)
        except Exception as exc:
            raise ApplicationInputError(
                f"invalid session id: {self.session_id!r}"
            ) from exc
        object.__setattr__(self, "task_id", _bounded_text(self.task_id, "task_id", _MAX_TASK_ID_BYTES))
        if type(self.source_kind) is not SourceKind:
            raise ApplicationInputError("source_kind must be a SourceKind")
        if not can_start_new_session(self.source_kind):
            raise ApplicationInputError(
                f"source kind {self.source_kind.value!r} is recorded and "
                "cannot start a live controller run"
            )
        object.__setattr__(
            self,
            "run_id",
            (
                None
                if self.run_id is None
                else _bounded_text(self.run_id, "run_id", _MAX_RUN_ID_BYTES)
            ),
        )
        object.__setattr__(
            self,
            "initial_sequence",
            _nonneg_int(self.initial_sequence, "initial_sequence"),
        )


class ControllerSessionEventAdapter(ControllerObserver):
    """Convert controller-native observations into Task-1 ``SessionEvent``s.

    Implements the controller observer contract; every produced event is a
    schema-validated :class:`SessionEvent` carrying a contiguous sequence
    and an injectable UTC clock (a fixed clock makes the output
    deterministic).  Events are appended in emission order and may
    optionally be forwarded through a Task-1 :class:`SessionEventSink`.

    Sequence authority (Repair Pass 3): by default the adapter owns a
    private :class:`SessionEventEmitter` starting at
    ``context.initial_sequence``, so standalone tests stay possible.  When a
    shared ``emitter`` is supplied, ALL producers of the session emit
    through it, making the emitter the one authoritative sequence/identity/
    clock owner and the journal-failure gate; the adapter then never guesses
    another producer's next sequence.  The shared emitter identity must
    match the adapter context (fail closed), and the adapter's run binding
    from the authoritative ``RUN_STARTED`` observation binds the shared
    emitter as well.

    Identity is fail-closed: observations whose ``task_id`` (or declared/
    bound ``run_id``) do not match the context raise
    :class:`ApplicationContractError` instead of producing wrong-provenance
    events.
    """

    def __init__(
        self,
        context: ControllerObservationContext,
        *,
        clock: Callable[[], str] | None = None,
        sink: SessionEventSink | None = None,
        emitter: SessionEventEmitter | None = None,
    ) -> None:
        if type(context) is not ControllerObservationContext:
            raise ApplicationInputError("context must be a ControllerObservationContext")
        self._context = context
        self._clock = _validated_clock(clock) if clock is not None else _default_clock
        self._events: list[SessionEvent] = []
        self._run_id: Optional[str] = context.run_id
        self._phase: Optional[ControllerState] = None
        self._emitter = self._resolve_emitter(context, emitter, clock, sink)

    def _resolve_emitter(
        self,
        context: ControllerObservationContext,
        emitter: SessionEventEmitter | None,
        clock: Callable[[], str] | None,
        sink: SessionEventSink | None,
    ) -> SessionEventEmitter:
        if emitter is not None:
            if type(emitter) is not SessionEventEmitter:
                raise ApplicationInputError("emitter must be a SessionEventEmitter")
            if (
                emitter.session_id != context.session_id
                or emitter.task_id != context.task_id
                or emitter.source_kind is not context.source_kind
            ):
                raise ApplicationContractError(
                    "shared emitter identity does not match the adapter context"
                )
            if clock is not None or sink is not None:
                raise ApplicationInputError(
                    "clock/sink belong to the shared emitter; pass them there"
                )
            if (
                context.run_id is not None
                and emitter.run_id is not None
                and emitter.run_id != context.run_id
            ):
                raise ApplicationContractError(
                    "shared emitter run id does not match the adapter context"
                )
            # The shared emitter's run_id is bound by the session owner at
            # ``session.started`` (events before it carry null), never by a
            # producer at construction time.
            return emitter
        return SessionEventEmitter(
            session_id=context.session_id,
            task_id=context.task_id,
            source_kind=context.source_kind,
            run_id=context.run_id,
            clock=self._clock,
            sink=sink,
            initial_sequence=context.initial_sequence,
        )

    @property
    def context(self) -> ControllerObservationContext:
        return self._context

    @property
    def emitter(self) -> SessionEventEmitter:
        """The session's shared emission authority (sequence owner)."""
        return self._emitter

    def events(self) -> Tuple[SessionEvent, ...]:
        """The produced session events in emission (sequence) order."""
        return tuple(self._events)

    def _validate_identity(self, observation: ControllerObservation) -> None:
        if observation.task_id != self._context.task_id:
            raise ApplicationContractError(
                f"observation task_id {observation.task_id!r} does not match "
                f"the adapter context task_id {self._context.task_id!r}"
            )
        if self._run_id is None:
            if observation.kind is not ControllerObservationKind.RUN_STARTED:
                raise ApplicationContractError(
                    "run identity is not established before RUN_STARTED"
                )
            self._run_id = observation.run_id
            # The shared emission authority binds the same authoritative run
            # identity, so every producer that shares it stays consistent.
            self._emitter.bind_run_id(observation.run_id)
        elif observation.run_id != self._run_id:
            raise ApplicationContractError(
                f"observation run_id {observation.run_id!r} does not match "
                f"the adapter run_id {self._run_id!r}"
            )

    def _track_phase(self, observation: ControllerObservation) -> None:
        phase = observation.state_after
        if phase is None:
            phase = observation.state_before
        if phase is not None:
            self._phase = phase

    def _emit(self, kind: SessionEventKind, payload: Dict[str, Any]) -> None:
        event = self._emitter.emit(kind, payload, controller_phase=self._phase)
        self._events.append(event)

    def _require_index(self, observation: ControllerObservation, label: str) -> int:
        if observation.model_call_index is None:
            raise ApplicationContractError(
                f"{observation.kind.value} observation requires {label}"
            )
        return observation.model_call_index

    def _require_step_index(self, observation: ControllerObservation) -> int:
        if observation.step_index is None:
            raise ApplicationContractError(
                "STEP_COMPLETED observation requires step_index"
            )
        return observation.step_index

    def _validated_directive_kind(self, observation: ControllerObservation) -> Optional[str]:
        value = _bounded_text_or_none(
            observation.directive_kind, "directive_kind", _MAX_SHORT_TEXT_BYTES
        )
        if value is not None and value not in _VALID_DIRECTIVE_KINDS:
            raise ApplicationContractError(
                f"unknown directive_kind {value!r} in {observation.kind.value} observation"
            )
        return value

    def _validated_stop_reason(self, observation: ControllerObservation) -> Optional[str]:
        value = _bounded_text_or_none(
            observation.stop_reason, "stop_reason", _MAX_SHORT_TEXT_BYTES
        )
        if value is not None and value not in _VALID_STOP_REASONS:
            raise ApplicationContractError(
                f"unknown stop_reason {value!r} in {observation.kind.value} observation"
            )
        return value

    def _validated_tool_name(self, observation: ControllerObservation, label: str) -> str:
        value = _bounded_text_or_none(observation.tool_name, label, _MAX_NAME_BYTES)
        if value is None:
            raise ApplicationContractError(
                f"{observation.kind.value} observation requires {label}"
            )
        return value

    def notify(self, observation: ControllerObservation) -> None:
        """Adapt one controller observation into session events.

        ``RUN_STARTED``, ``STATE_TRANSITION`` and ``TERMINAL`` update the
        tracked controller phase only: ``session.started``, cleanup, and
        session terminal events belong to later worker/session/source
        tasks and are never fabricated here.
        """
        if type(observation) is not ControllerObservation:
            raise ApplicationContractError(
                "adapter requires a ControllerObservation"
            )
        self._validate_identity(observation)
        self._track_phase(observation)
        if observation.kind in _NON_EVENT_KINDS:
            return

        if observation.kind is ControllerObservationKind.STATE_TRANSITION:
            if observation.state_before is None or observation.state_after is None:
                raise ApplicationContractError(
                    "STATE_TRANSITION observation requires source and target states"
                )
            self._emit(
                SessionEventKind.CONTROLLER_TRANSITION,
                {
                    "source_state": observation.state_before.value,
                    "target_state": observation.state_after.value,
                    "reason": _bounded_text_or_none(
                        observation.transition_reason,
                        "transition_reason",
                        _MAX_TRANSITION_REASON_BYTES,
                    ),
                },
            )
            return

        if observation.kind is ControllerObservationKind.MODEL_REQUEST_STARTED:
            self._emit(
                SessionEventKind.MODEL_REQUEST_STARTED,
                {"request_index": self._require_index(observation, "model_call_index")},
            )
            return

        if observation.kind is ControllerObservationKind.MODEL_REQUEST_COMPLETED:
            request_status = _bounded_text_or_none(
                observation.request_status, "request_status", _MAX_SHORT_TEXT_BYTES
            )
            if request_status not in _VALID_REQUEST_STATUSES:
                raise ApplicationContractError(
                    f"unknown request_status {request_status!r} in "
                    f"{observation.kind.value} observation"
                )
            self._emit(
                SessionEventKind.MODEL_REQUEST_COMPLETED,
                {
                    "request_index": self._require_index(observation, "model_call_index"),
                    "status": request_status,
                },
            )
            return

        if observation.kind is ControllerObservationKind.DIRECTIVE_ACCEPTED:
            target_state = (
                observation.target_state.value
                if observation.target_state is not None
                else None
            )
            self._emit(
                SessionEventKind.MODEL_DIRECTIVE_ACCEPTED,
                {
                    "directive_kind": self._validated_directive_kind(observation),
                    "action_name": _bounded_text_or_none(
                        observation.tool_name, "action_name", _MAX_NAME_BYTES
                    ),
                    "target_state": _bounded_text_or_none(
                        target_state, "target_state", _MAX_SHORT_TEXT_BYTES
                    ),
                },
            )
            return

        if observation.kind is ControllerObservationKind.DIRECTIVE_REJECTED:
            rejection_category = _bounded_text_or_none(
                observation.rejection_category,
                "rejection_category",
                _MAX_NAME_BYTES,
            )
            if rejection_category is None:
                raise ApplicationContractError(
                    "DIRECTIVE_REJECTED observation requires rejection_category"
                )
            self._emit(
                SessionEventKind.MODEL_DIRECTIVE_REJECTED,
                {
                    "directive_kind": self._validated_directive_kind(observation),
                    "rejection_category": rejection_category,
                },
            )
            return

        if observation.kind is ControllerObservationKind.TOOL_STARTED:
            self._emit(
                SessionEventKind.TOOL_STARTED,
                {"tool_name": self._validated_tool_name(observation, "tool_name")},
            )
            return

        if observation.kind is ControllerObservationKind.TOOL_COMPLETED:
            if observation.observation_status is None:
                raise ApplicationContractError(
                    "TOOL_COMPLETED observation requires observation_status"
                )
            self._emit(
                SessionEventKind.TOOL_COMPLETED,
                {
                    "tool_name": self._validated_tool_name(observation, "tool_name"),
                    "status": observation.observation_status.value,
                },
            )
            return

        if observation.kind is ControllerObservationKind.STEP_COMPLETED:
            self._emit(
                SessionEventKind.CONTROLLER_STEP,
                {
                    "step_index": self._require_step_index(observation),
                    "directive_kind": self._validated_directive_kind(observation),
                    "stop_reason": self._validated_stop_reason(observation),
                },
            )
            return

        raise ApplicationContractError(
            f"unsupported observation kind: {observation.kind.value!r}"
        )


__all__ = [
    "ControllerObservationContext",
    "ControllerSessionEventAdapter",
]
