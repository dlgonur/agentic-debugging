"""Execution-source contracts: live-startable sources and replay sources.

Two ordered event boundaries are defined here:

- :class:`SessionEventSource` — the presentation-input contract satisfied by
  both a live session stream and a replay cursor.  Events arrive in
  contiguous ``sequence`` order starting at 0; a live source is incremental
  and a replay source is read-only (it never invokes tools, PDB, patch
  application, model calls, or verification).
- :class:`SessionEventSink` — the single-writer boundary of a live session
  (sequence assignment and durable journaling are later-task
  implementations; only the protocol is fixed here).

Recorded source kinds are first-class vocabulary but can never start a new
session: ``can_start_new_session`` and :class:`ExecutionSourceSpec`
validation keep replay material out of the live-start workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.events import (
    SessionTerminationReason,
    SourceKind,
    SessionEvent,
    contains_credential_shape,
)

__all__ = [
    "ExecutionSourceSpec",
    "ModelExecutionError",
    "SessionEventSink",
    "SessionEventSource",
    "can_start_new_session",
]


class ModelExecutionError(RuntimeError):
    """A live execution source failed at the model-execution boundary.

    Raised by the configured command-model source when the controller run
    ended with a scientific failure that belongs to the model-execution
    layer (transport/provider failure, directive exhaustion, controller
    failure).  It carries the exact Task-1 termination reason so the worker
    can classify the session terminal honestly (``model_error``,
    ``directive_exhausted``, ``controller_failed``) instead of reporting an
    orderly completion.  It is never raised for cancellation (the neutral
    :class:`~agentic_debugger.cancellation.CancellationError` owns that
    path) and never for scientific correctness: the independent verifier
    remains the correctness authority.
    """

    def __init__(self, message: str, termination_reason: SessionTerminationReason) -> None:
        if type(termination_reason) is not SessionTerminationReason:
            raise ApplicationInputError(
                "termination_reason must be a SessionTerminationReason"
            )
        super().__init__(message)
        self.termination_reason = termination_reason

_STARTABLE_KINDS = frozenset({
    SourceKind.OFFLINE_DEMO,
    SourceKind.CONFIGURED_MODEL,
    SourceKind.OLLAMA_CLOUD_LADDER,
    SourceKind.LEVEL32_OPERATOR,
    SourceKind.LOCAL_PROJECT,
})

_MAX_POLICY_CHARS = 64
_MAX_CONFIG_REF_CHARS = 512


def can_start_new_session(kind: SourceKind) -> bool:
    """Whether a source kind may start a new live session.

    Recorded kinds (session bundles, canonical trajectories, experiment
    evidence) are replay-only and must be opened through recorded-source
    paths, never the live-start workflow.
    """
    if type(kind) is not SourceKind:
        raise ApplicationInputError("source kind is required")
    return kind in _STARTABLE_KINDS


def _bounded_text(value: Any, label: str, max_chars: int) -> str:
    if type(value) is not str or not value.strip():
        raise ApplicationInputError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ApplicationInputError(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise ApplicationInputError(f"{label} exceeds the {max_chars}-byte bound")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ApplicationInputError(f"{label} contains control characters")
    # One shared credential-shape policy (see application.events); this
    # module no longer keeps a private copy that could drift.
    if contains_credential_shape(value):
        raise ApplicationInputError(f"{label} contains a credential-shaped value")
    return value


def _bounded_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, label, max_chars)


@dataclass(frozen=True)
class ExecutionSourceSpec:
    """Immutable description of one new session's execution source.

    Only live-startable kinds are accepted; recorded kinds are rejected so
    replay material can never enter the new-session workflow.  The
    ``model_config_ref`` is a validated configuration reference (file or
    profile name), never a credential.
    """

    kind: SourceKind
    task_id: str
    policy: Optional[str] = None
    model_config_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.kind) is not SourceKind:
            raise ApplicationInputError("kind must be a SourceKind")
        if not can_start_new_session(self.kind):
            raise ApplicationInputError(
                f"source kind {self.kind.value!r} is recorded and cannot "
                "start a new session"
            )
        _bounded_text(self.task_id, "task_id", 256)
        object.__setattr__(
            self, "policy", _bounded_text_or_none(self.policy, "policy", _MAX_POLICY_CHARS)
        )
        object.__setattr__(
            self,
            "model_config_ref",
            _bounded_text_or_none(
                self.model_config_ref, "model_config_ref", _MAX_CONFIG_REF_CHARS
            ),
        )
        if self.kind is SourceKind.CONFIGURED_MODEL:
            if self.model_config_ref is None:
                raise ApplicationInputError(
                    "configured_model sources require a model_config_ref"
                )
        elif self.kind in (SourceKind.OLLAMA_CLOUD_LADDER, SourceKind.LEVEL32_OPERATOR):
            if self.model_config_ref is None:
                raise ApplicationInputError(
                    "level32_operator sources require a canonical model alias"
                )
        elif self.kind is SourceKind.LOCAL_PROJECT:
            if self.model_config_ref is None:
                raise ApplicationInputError(
                    "local_project sources require a model_config_ref"
                )
        elif self.model_config_ref is not None:
            raise ApplicationInputError(
                "model_config_ref is only valid for configured_model sources"
            )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "task_id": self.task_id,
            "policy": self.policy,
            "model_config_ref": self.model_config_ref,
        }


@runtime_checkable
class SessionEventSource(Protocol):
    """Ordered session event stream or replay cursor.

    Contract:

    - ``next_event`` returns events in contiguous ``sequence`` order
      starting at 0 for the session; ``None`` means the stream is exhausted.
    - A live source is incremental; a replay source is read-only and never
      invokes tools, PDB, patch application, model calls, or verification.
    - ``close`` releases any held resources; a replay cursor holds none.
    """

    @property
    def source_kind(self) -> SourceKind: ...

    def next_event(self) -> Optional[SessionEvent]: ...

    def close(self) -> None: ...


@runtime_checkable
class SessionEventSink(Protocol):
    """Single-writer boundary for a live session's event stream.

    Contract:

    - ``append`` accepts one validated event and records it in sequence
      order; implementations enforce contiguous sequences and constant
      session identity per sink.
    - ``flush`` and ``close`` release buffered state; a closed sink rejects
      further appends.

    Durable journal writers (crash-durable flush/fsync, recovery) are later
    roadmap tasks; only this protocol is fixed here.
    """

    def append(self, event: SessionEvent) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...
