"""Versioned application-owned session vocabulary and :class:`SessionEvent`.

This module is the public facade of the application event architecture.
It owns the strict :class:`SessionEvent` model and the complete-stream
validation contract, and re-exports the versioned vocabulary, lifecycle
rules, and identity/credential-shape validators so that
``agentic_debugger.application.events`` remains the single import surface
for the application event contract.

``SessionEvent`` is a separate application-owned contract.  It is not a
canonical ``RunEvent`` 1.0 record, never mixes into canonical trajectory
files, and carries only bounded data already safe for model/tool observation
(see the safe-data rules below).

Architecture (one-way dependency graph):

* :mod:`agentic_debugger.application.event_contracts` — vocabulary enums,
  lifecycle transition rules, session/timestamp identity validation, and
  the shared credential-shape policies;
* :mod:`agentic_debugger.application.event_fields` — bounded-value
  validation primitives and the freeze/thaw machinery;
* :mod:`agentic_debugger.application.event_payloads` — per-kind payload
  validators and their registry;
* this module — :class:`SessionEvent` and stream validation.

Dependency rule: the event modules import only lightweight existing enums
and taxonomies for validation (``ControllerState``, ``ObservationStatus``,
``SemanticOutcome``, ``EvaluationStatus``).  They never import or execute
controller, verifier, PDB, patch, demo, live-model, GPU, or experiment code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from agentic_debugger import SchemaValidationError
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application import ApplicationContractError
from agentic_debugger.application.event_contracts import (
    MAX_BREAKPOINTS,
    MAX_CHANGED_FILES,
    MAX_IDENTIFIER_CHARS,
    MAX_JSON_DEPTH,
    MAX_LOCALS,
    MAX_PATCH_TEXT_CHARS,
    MAX_SHA256_HEX,
    MAX_SHORT_TEXT_CHARS,
    MAX_SOURCE_TEXT_CHARS,
    MAX_STACK_FRAMES,
    MAX_TEXT_CHARS,
    MAX_TUPLE_TEXT_CHARS,
    SESSION_EVENT_SCHEMA_VERSION,
    ModelRequestStatus,
    OperatorStage,
    SessionEventKind,
    SessionPhase,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
    SourceSnapshotStage,
    VerifierStage,
    VerifierStageStatus,
    allowed_transitions,
    can_transition,
    compatible_reasons,
    contains_credential_shape,
    is_credential_name,
    terminal_status_for,
    validate_session_id,
    validate_utc_timestamp,
)
from agentic_debugger.application.event_fields import (
    _check_no_unknown,
    _check_required,
    _detached,
    _enum,
    _freeze,
    _identifier,
    _nonneg_int,
    _thaw,
)
from agentic_debugger.application.event_payloads import _PAYLOAD_VALIDATORS

# ---------------------------------------------------------------------------
# SessionEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionEvent:
    """One immutable, JSON-compatible application-owned session event.

    ``sequence`` is authoritative ordering (contiguous, starting at 0);
    ``timestamp_utc`` is informational.  ``run_id`` is null until
    ``session.started`` binds the underlying execution run and therefore
    serves as the started indicator for cleanup semantics.

    Construction is validated: ``__post_init__`` re-validates every field
    through the same strict rules as ``from_mapping``, so no public
    construction path can produce an invalid event.  The payload is
    canonicalized into a frozen nested JSON structure (tuple-backed, no
    mutating protocol); caller-owned input and ``to_mapping()`` output are
    never shared with the event.
    """

    schema_version: str
    session_id: str
    task_id: str
    run_id: Optional[str]
    sequence: int
    timestamp_utc: str
    source_kind: SourceKind
    event_kind: SessionEventKind
    controller_phase: Optional[ControllerState]
    payload: Mapping[str, Any]

    _KNOWN_FIELDS = {
        "schema_version", "session_id", "task_id", "run_id", "sequence",
        "timestamp_utc", "source_kind", "event_kind", "controller_phase",
        "payload",
    }
    _REQUIRED_FIELDS = _KNOWN_FIELDS

    def __post_init__(self) -> None:
        if self.schema_version != SESSION_EVENT_SCHEMA_VERSION:
            raise SchemaValidationError(
                f"Unsupported session event schema version: {self.schema_version!r}"
            )
        validate_session_id(self.session_id)
        _identifier(self.task_id, "task_id")
        if self.run_id is not None:
            _identifier(self.run_id, "run_id")
        _nonneg_int(self.sequence, "sequence")
        validate_utc_timestamp(self.timestamp_utc)
        if type(self.source_kind) is not SourceKind:
            raise SchemaValidationError("source_kind must be a SourceKind")
        if type(self.event_kind) is not SessionEventKind:
            raise SchemaValidationError("event_kind must be a SessionEventKind")
        if self.controller_phase is not None and type(self.controller_phase) is not ControllerState:
            raise SchemaValidationError(
                "controller_phase must be a ControllerState or null"
            )
        if not isinstance(self.payload, Mapping):
            raise SchemaValidationError("event.payload must be a mapping")
        canonical = _PAYLOAD_VALIDATORS[self.event_kind](self.payload)
        object.__setattr__(self, "payload", _freeze(_detached(canonical)))

    @staticmethod
    def from_mapping(m: Any) -> SessionEvent:
        if not isinstance(m, Mapping):
            raise SchemaValidationError("event must be a mapping")
        _check_required(m, SessionEvent._REQUIRED_FIELDS, "event")
        _check_no_unknown(m, SessionEvent._KNOWN_FIELDS, "event")
        controller_phase_raw = m["controller_phase"]
        controller_phase = (
            None
            if controller_phase_raw is None
            else _enum(controller_phase_raw, "controller_phase", ControllerState)
        )
        return SessionEvent(
            schema_version=m["schema_version"],
            session_id=m["session_id"],
            task_id=m["task_id"],
            run_id=m["run_id"],
            sequence=m["sequence"],
            timestamp_utc=m["timestamp_utc"],
            source_kind=_enum(m["source_kind"], "source_kind", SourceKind),
            event_kind=_enum(m["event_kind"], "event_kind", SessionEventKind),
            controller_phase=controller_phase,
            payload=m["payload"],
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "timestamp_utc": self.timestamp_utc,
            "source_kind": self.source_kind.value,
            "event_kind": self.event_kind.value,
            "controller_phase": (
                self.controller_phase.value if self.controller_phase is not None else None
            ),
            "payload": _thaw(self.payload),
        }


# ---------------------------------------------------------------------------
# Complete-stream contract
# ---------------------------------------------------------------------------


def validate_session_event_stream(events: Sequence[SessionEvent]) -> None:
    """Validate one complete session event stream without reordering it.

    Rules: contiguous sequences from 0; constant session/task/source
    identity; ``session.created`` first; at most one ``session.started``
    which binds a constant ``run_id``; lifecycle transitions legal; at most
    one ``cancel_requested`` before the terminal event; exactly one terminal
    event in terminal position.

    Cleanup follows one deterministic lifecycle: ``cleanup.completed`` must
    follow an active ``cleanup.started``; a new ``cleanup.started`` may not
    begin while one is active; ``session.completed`` and (for a started
    session) ``session.cancelled`` require the terminal cleanup cycle to be
    completed with ``verified=True``; ``cleanup_failed`` requires an
    attempted cleanup that did not end verified.

    Incomplete (crash-interrupted) journals are a history concern and are
    not classified here.
    """

    if not events:
        raise ApplicationContractError("session event stream is empty")
    if events[0].event_kind is not SessionEventKind.SESSION_CREATED:
        raise ApplicationContractError(
            "session event stream must begin with session.created"
        )
    session_id = events[0].session_id
    task_id = events[0].task_id
    source_kind = events[0].source_kind
    status: SessionStatus = SessionStatus.CREATED
    run_id: Optional[str] = None
    started = False
    cancel_requested = False
    terminal_index: Optional[int] = None
    cleanup_active = False
    cleanup_started_ever = False
    last_completed_verified: Optional[bool] = None
    for index, event in enumerate(events):
        if event.sequence != index:
            raise ApplicationContractError(
                f"non-contiguous sequence at event {index}: "
                f"expected {index}, got {event.sequence}"
            )
        if event.session_id != session_id:
            raise ApplicationContractError("mixed session IDs in stream")
        if event.task_id != task_id:
            raise ApplicationContractError("mixed task IDs in stream")
        if event.source_kind is not source_kind:
            raise ApplicationContractError("mixed source kinds in stream")
        if terminal_index is not None:
            raise ApplicationContractError(
                f"events after terminal event at {terminal_index}"
            )
        kind = event.event_kind
        if started:
            if event.run_id is None:
                raise ApplicationContractError(
                    f"run_id missing after session.started at event {index}"
                )
            if run_id is not None and event.run_id != run_id:
                raise ApplicationContractError(
                    f"run_id changed at event {index}"
                )
        elif kind is not SessionEventKind.SESSION_STARTED and event.run_id is not None:
            raise ApplicationContractError(
                f"run_id present before session.started at event {index}"
            )
        if kind is SessionEventKind.SESSION_STARTED:
            if started:
                raise ApplicationContractError("duplicate session.started")
            if event.run_id is None:
                raise ApplicationContractError(
                    "session.started requires a run_id"
                )
            started = True
            run_id = event.run_id
            _apply_status(status, SessionStatus.STARTING, index)
            status = SessionStatus.STARTING
        elif kind is SessionEventKind.SESSION_STATUS_CHANGED:
            _apply_status(status, SessionStatus.RUNNING, index)
            status = SessionStatus.RUNNING
        elif kind is SessionEventKind.SESSION_CANCEL_REQUESTED:
            if cancel_requested:
                raise ApplicationContractError("duplicate session.cancel_requested")
            cancel_requested = True
        elif kind is SessionEventKind.CLEANUP_STARTED:
            if cleanup_active:
                raise ApplicationContractError(
                    f"duplicate cleanup.started at event {index} "
                    "(a cleanup cycle is already active)"
                )
            cleanup_active = True
            cleanup_started_ever = True
        elif kind is SessionEventKind.CLEANUP_COMPLETED:
            if not cleanup_active:
                raise ApplicationContractError(
                    f"cleanup.completed at event {index} without a "
                    "preceding cleanup.started"
                )
            cleanup_active = False
            last_completed_verified = event.payload["verified"]
        elif kind is SessionEventKind.CLEANUP_NOT_REQUIRED:
            if cleanup_active:
                raise ApplicationContractError(
                    f"cleanup.not_required at event {index} while cleanup is active"
                )
            # Explicit positive proof that no disposable resources were created.
            # Does not require a preceding cleanup.started and does not set
            # last_completed_verified — it authorizes a terminal without
            # verified cleanup.
            pass
        elif kind in (
            SessionEventKind.SESSION_COMPLETED,
            SessionEventKind.SESSION_FAILED,
            SessionEventKind.SESSION_CANCELLED,
        ):
            new_status = SessionStatus(event.payload["status"])
            _apply_status(status, new_status, index)
            # A previously verified cleanup does not authorize terminal
            # completion when a later cleanup cycle was started and remains
            # incomplete (or was never started for this terminal).
            effective_verified = (
                not cleanup_active and last_completed_verified is True
            )
            if kind is SessionEventKind.SESSION_COMPLETED:
                if not effective_verified:
                    raise ApplicationContractError(
                        "session.completed requires the terminal cleanup "
                        "cycle to be completed with verified=True"
                    )
            elif kind is SessionEventKind.SESSION_CANCELLED:
                # A session cancelled before it started has nothing to clean.
                if started and not effective_verified:
                    raise ApplicationContractError(
                        "session.cancelled requires the terminal cleanup "
                        "cycle to be completed with verified=True"
                    )
            elif new_status is SessionStatus.CLEANUP_FAILED:
                if not cleanup_started_ever:
                    raise ApplicationContractError(
                        "cleanup_failed requires an attempted cleanup"
                    )
                if effective_verified:
                    raise ApplicationContractError(
                        "cleanup_failed cannot follow verified cleanup"
                    )
            status = new_status
            terminal_index = index
    if terminal_index is None:
        raise ApplicationContractError("session event stream has no terminal event")
    if terminal_index != len(events) - 1:
        raise ApplicationContractError(
            f"events after terminal event at {terminal_index}"
        )


def _apply_status(current: SessionStatus, target: SessionStatus, index: int) -> None:
    if not can_transition(current, target):
        raise ApplicationContractError(
            f"illegal session status transition at event {index}: "
            f"{current.value} -> {target.value}"
        )


__all__ = [
    "MAX_BREAKPOINTS",
    "MAX_CHANGED_FILES",
    "MAX_IDENTIFIER_CHARS",
    "MAX_JSON_DEPTH",
    "MAX_LOCALS",
    "MAX_PATCH_TEXT_CHARS",
    "MAX_SHORT_TEXT_CHARS",
    "MAX_SHA256_HEX",
    "MAX_SOURCE_TEXT_CHARS",
    "MAX_STACK_FRAMES",
    "MAX_TEXT_CHARS",
    "MAX_TUPLE_TEXT_CHARS",
    "ModelRequestStatus",
    "SESSION_EVENT_SCHEMA_VERSION",
    "SessionEvent",
    "SessionEventKind",
    "OperatorStage",
    "SessionPhase",
    "SessionStatus",
    "SessionTerminationReason",
    "SourceKind",
    "SourceSnapshotStage",
    "VerifierStage",
    "VerifierStageStatus",
    "allowed_transitions",
    "can_transition",
    "compatible_reasons",
    "contains_credential_shape",
    "is_credential_name",
    "terminal_status_for",
    "validate_session_event_stream",
    "validate_session_id",
    "validate_utc_timestamp",
]
