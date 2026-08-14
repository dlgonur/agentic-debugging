"""Session specification, identity, lifecycle, and result contracts.

This module defines the immutable application-facing session contracts:

- :class:`SessionId` — an actually validated immutable session identity;
- :class:`SessionSpec` — the immutable request for a new session;
- :class:`SessionSnapshot` — immutable service-facing state at a point;
- :class:`SessionResult` — immutable terminal application result;
- :class:`SessionController` — the lifecycle command protocol.

The versioned lifecycle vocabulary (``SessionStatus``, ``SessionPhase``,
``SessionTerminationReason``) and its transition rules live in
:mod:`agentic_debugger.application.events` and are re-exported here.

Operational completion is deliberately separate from scientific outcome:
``SessionResult`` never carries a correctness verdict.  The independent
verifier's ``EvaluationResult`` remains the only correctness authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.events import (
    SessionPhase,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
    allowed_transitions,
    can_transition,
    compatible_reasons,
    terminal_status_for,
    validate_session_id,
    validate_utc_timestamp,
)
from agentic_debugger.application.sources import ExecutionSourceSpec

__all__ = [
    "SessionBudgets",
    "SessionController",
    "SessionId",
    "SessionPhase",
    "SessionResult",
    "SessionSnapshot",
    "SessionSpec",
    "SessionStatus",
    "SessionTerminationReason",
    "allowed_transitions",
    "can_transition",
    "compatible_reasons",
    "terminal_status_for",
]

MAX_SPEC_TEXT_CHARS = 512
MAX_DIAGNOSTIC_CHARS = 4000
MAX_DIAGNOSTICS = 64


def _bounded_text(value: Any, label: str, max_chars: int) -> str:
    if type(value) is not str or not value.strip():
        raise ApplicationInputError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ApplicationInputError(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise ApplicationInputError(
            f"{label} exceeds the {max_chars}-byte bound"
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ApplicationInputError(f"{label} contains control characters")
    return value


def _bounded_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, label, max_chars)


def _optional_positive_int(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise ApplicationInputError(
            f"{label} must be a positive integer or null"
        )
    return value


@dataclass(frozen=True)
class SessionId:
    """Immutable, validated application session identifier.

    Construction is the validated factory: any invalid value raises
    :class:`ApplicationInputError` instead of producing an invalid identity.
    """

    value: str

    def __post_init__(self) -> None:
        try:
            validate_session_id(self.value)
        except Exception as exc:
            raise ApplicationInputError(
                f"invalid session id: {self.value!r}"
            ) from exc

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class SessionBudgets:
    """Visible controller/model/time budgets of a new session."""

    max_model_calls: Optional[int] = None
    max_controller_steps: Optional[int] = None
    max_elapsed_seconds: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "max_model_calls",
            _optional_positive_int(self.max_model_calls, "budgets.max_model_calls"),
        )
        object.__setattr__(
            self, "max_controller_steps",
            _optional_positive_int(
                self.max_controller_steps, "budgets.max_controller_steps"
            ),
        )
        object.__setattr__(
            self, "max_elapsed_seconds",
            _optional_positive_int(
                self.max_elapsed_seconds, "budgets.max_elapsed_seconds"
            ),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "max_model_calls": self.max_model_calls,
            "max_controller_steps": self.max_controller_steps,
            "max_elapsed_seconds": self.max_elapsed_seconds,
        }


@dataclass(frozen=True)
class SessionSpec:
    """Immutable request describing one new application session."""

    task_id: str
    source: ExecutionSourceSpec
    budgets: SessionBudgets = field(default_factory=SessionBudgets)
    artifact_destination: Optional[str] = None

    def __post_init__(self) -> None:
        _bounded_text(self.task_id, "task_id", 256)
        if type(self.source) is not ExecutionSourceSpec:
            raise ApplicationInputError("source must be an ExecutionSourceSpec")
        if self.source.task_id != self.task_id:
            raise ApplicationInputError(
                "source task_id must match the session task_id"
            )
        if type(self.budgets) is not SessionBudgets:
            raise ApplicationInputError("budgets must be a SessionBudgets")
        object.__setattr__(
            self,
            "artifact_destination",
            _bounded_text_or_none(
                self.artifact_destination, "artifact_destination", MAX_SPEC_TEXT_CHARS
            ),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source": self.source.to_mapping(),
            "budgets": self.budgets.to_mapping(),
            "artifact_destination": self.artifact_destination,
        }

    def fingerprint(self) -> str:
        """Stable SHA-256 fingerprint of the canonical spec mapping."""
        canonical = json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_identifier(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, label, 256)


def _validate_optional_utc(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return validate_utc_timestamp(value)
    except Exception as exc:
        raise ApplicationInputError(f"{label} is invalid") from exc


@dataclass(frozen=True)
class SessionSnapshot:
    """Immutable application-facing state of a session at one point."""

    session_id: SessionId
    spec: SessionSpec
    status: SessionStatus
    phase: Optional[SessionPhase] = None
    run_id: Optional[str] = None
    started_at_utc: Optional[str] = None
    sequence: int = 0
    termination_reason: Optional[SessionTerminationReason] = None

    def __post_init__(self) -> None:
        if type(self.session_id) is not SessionId:
            raise ApplicationInputError("session_id must be a SessionId")
        if type(self.spec) is not SessionSpec:
            raise ApplicationInputError("spec must be a SessionSpec")
        if type(self.status) is not SessionStatus:
            raise ApplicationInputError("status must be a SessionStatus")
        if self.phase is not None:
            if type(self.phase) is not SessionPhase:
                raise ApplicationInputError("phase must be a SessionPhase")
            if self.status is not SessionStatus.RUNNING:
                raise ApplicationInputError(
                    "phase is only valid while the session is running"
                )
        if type(self.sequence) is not int or isinstance(self.sequence, bool) or self.sequence < 0:
            raise ApplicationInputError("sequence must be a non-negative integer")
        object.__setattr__(self, "run_id", _validate_identifier(self.run_id, "run_id"))
        object.__setattr__(
            self, "started_at_utc",
            _validate_optional_utc(self.started_at_utc, "started_at_utc"),
        )
        if self.status.terminal:
            if self.termination_reason is None:
                raise ApplicationInputError(
                    "a terminal snapshot requires a termination reason"
                )
            if self.termination_reason not in compatible_reasons(self.status):
                raise ApplicationInputError(
                    "termination reason is not compatible with the status"
                )
        elif self.termination_reason is not None:
            raise ApplicationInputError(
                "termination reason is only valid for a terminal snapshot"
            )


@dataclass(frozen=True)
class SessionResult:
    """Immutable terminal application result of a session.

    Carries operational completion only; scientific outcome stays in the
    independent verifier's records.

    Cleanup semantics (aligned with the complete-stream contract): ``run_id``
    is the accepted indicator that ``session.started`` occurred (the stream
    contract binds ``run_id`` exactly there).  ``SUCCEEDED`` and
    ``UNRESOLVED`` results require a ``run_id`` and verified cleanup; a
    ``CANCELLED`` result requires verified cleanup when the session started
    and represents a pre-start cancel (nothing cleaned) when ``run_id`` is
    null; ``CLEANUP_FAILED`` never claims verified cleanup.  These rules
    never infer scientific correctness.
    """

    session_id: SessionId
    spec: SessionSpec
    status: SessionStatus
    termination_reason: SessionTerminationReason
    run_id: Optional[str] = None
    started_at_utc: Optional[str] = None
    ended_at_utc: Optional[str] = None
    sequence: int = 0
    cleanup_verified: bool = False
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.session_id) is not SessionId:
            raise ApplicationInputError("session_id must be a SessionId")
        if type(self.spec) is not SessionSpec:
            raise ApplicationInputError("spec must be a SessionSpec")
        if type(self.status) is not SessionStatus or not self.status.terminal:
            raise ApplicationInputError("status must be a terminal SessionStatus")
        if type(self.termination_reason) is not SessionTerminationReason:
            raise ApplicationInputError(
                "termination_reason must be a SessionTerminationReason"
            )
        if self.termination_reason not in compatible_reasons(self.status):
            raise ApplicationInputError(
                "termination reason is not compatible with the status"
            )
        if type(self.sequence) is not int or isinstance(self.sequence, bool) or self.sequence < 0:
            raise ApplicationInputError("sequence must be a non-negative integer")
        if type(self.cleanup_verified) is not bool:
            raise ApplicationInputError("cleanup_verified must be a boolean")
        if self.status in (SessionStatus.SUCCEEDED, SessionStatus.UNRESOLVED):
            # Orderly completion always follows a started, cleaned session.
            if self.run_id is None:
                raise ApplicationInputError(
                    "succeeded/unresolved results require a run_id "
                    "(the session started)"
                )
            if not self.cleanup_verified:
                raise ApplicationInputError(
                    "succeeded/unresolved results require verified cleanup"
                )
        elif self.status is SessionStatus.CANCELLED:
            if self.run_id is None:
                # Pre-start cancellation: nothing existed to clean.
                if self.cleanup_verified:
                    raise ApplicationInputError(
                        "a cancelled result without a run_id is a pre-start "
                        "cancel and cannot claim verified cleanup"
                    )
            elif not self.cleanup_verified:
                raise ApplicationInputError(
                    "a cancelled session that started requires verified cleanup"
                )
        elif self.status is SessionStatus.CLEANUP_FAILED and self.cleanup_verified:
            raise ApplicationInputError(
                "cleanup_failed results cannot claim verified cleanup"
            )
        if type(self.diagnostics) is not tuple or len(self.diagnostics) > MAX_DIAGNOSTICS:
            raise ApplicationInputError(
                f"diagnostics must be a tuple of at most {MAX_DIAGNOSTICS} items"
            )
        for index, item in enumerate(self.diagnostics):
            if not isinstance(item, str):
                raise ApplicationInputError(
                    f"diagnostics[{index}] must be a string"
                )
            try:
                _bounded_text(item, f"diagnostics[{index}]", MAX_DIAGNOSTIC_CHARS)
            except ApplicationInputError as exc:
                raise ApplicationInputError(f"invalid diagnostics: {exc}") from exc
        object.__setattr__(self, "run_id", _validate_identifier(self.run_id, "run_id"))
        object.__setattr__(
            self, "started_at_utc",
            _validate_optional_utc(self.started_at_utc, "started_at_utc"),
        )
        object.__setattr__(
            self, "ended_at_utc",
            _validate_optional_utc(self.ended_at_utc, "ended_at_utc"),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id.value,
            "task_id": self.spec.task_id,
            "source_kind": self.spec.source.kind.value,
            "status": self.status.value,
            "termination_reason": self.termination_reason.value,
            "run_id": self.run_id,
            "started_at_utc": self.started_at_utc,
            "ended_at_utc": self.ended_at_utc,
            "sequence": self.sequence,
            "cleanup_verified": self.cleanup_verified,
            "diagnostics": list(self.diagnostics),
        }


class SessionController(Protocol):
    """Lifecycle command boundary of the application session layer.

    Implementations own worker supervision and artifact registration; the
    TUI may only issue these commands.  ``start`` returns the validated
    :class:`SessionId` of the new session.
    """

    def start(self, spec: SessionSpec) -> SessionId: ...

    def cancel(self, session_id: SessionId) -> None: ...

    def snapshot(self, session_id: SessionId) -> SessionSnapshot: ...
