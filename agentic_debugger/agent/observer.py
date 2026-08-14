"""Controller-native typed observation seam for incremental observability.

The :class:`DeterministicController` remains a closed, synchronous execution
loop.  This module defines the smallest optional seam through which the
controller reports facts it authoritatively owns while execution is happening:

- run started and the terminal controller outcome;
- each model request boundary;
- accepted and rejected directives (after the controller's own
  state/policy/budget gates, never before them);
- tool dispatch start and completion;
- controller state transitions;
- completed controller steps.

Every observation belongs to one concrete controller run and therefore
carries that run's ``run_id`` and ``task_id`` explicitly.

Boundary rules:

- This module must not be imported by the application package; the
  controller must not depend on ``agentic_debugger.application``.
- Observations are immutable, validated, and detached: an observer can
  never mutate controller state, steps, snapshots, or the final result.
- The observer is optional, non-authoritative telemetry.  An ordinary
  observer ``Exception`` is swallowed by the controller and never changes
  a controller decision, budget, step, stop reason, or result.
  ``BaseException`` (``KeyboardInterrupt``/``SystemExit``) is never
  swallowed.
- No asynchronous infrastructure is introduced; observation is a
  synchronous side channel.

``directive_kind`` and ``stop_reason`` are carried as bounded strings
(the ``.value`` of ``ModelDirectiveKind``/``ControllerStopReason``) so this
module does not need to import the controller or model-adapter modules.
The controller is the sole producer and asserts those values at emit time;
the application adapter re-validates them against its own vocabulary and
fails closed on unknown values.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.events.schema import ObservationStatus


MAX_OBSERVATION_IDENTIFIER_BYTES = 256
MAX_OBSERVATION_SHORT_TEXT_BYTES = 64
MAX_OBSERVATION_NAME_BYTES = 256
MAX_OBSERVATION_TRANSITION_REASON_BYTES = 2048


class ControllerObservationError(ValueError):
    """Raised when a controller observation is malformed."""


class ControllerObservationKind(str, Enum):
    """The controller-native observation vocabulary.

    ``directive_kind`` and ``stop_reason`` strings are the ``.value`` of
    ``ModelDirectiveKind`` and ``ControllerStopReason`` respectively.
    """

    RUN_STARTED = "run_started"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_REQUEST_COMPLETED = "model_request_completed"
    DIRECTIVE_ACCEPTED = "directive_accepted"
    DIRECTIVE_REJECTED = "directive_rejected"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    STATE_TRANSITION = "state_transition"
    STEP_COMPLETED = "step_completed"
    TERMINAL = "terminal"


def _invalid(field_name: str) -> ControllerObservationError:
    return ControllerObservationError(f"invalid {field_name}")


def _bounded_text(value: object, field_name: str, maximum_bytes: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _invalid(field_name)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid(field_name)
    if len(encoded) > maximum_bytes:
        raise _invalid(field_name)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise _invalid(field_name)
    return value


def _identifier(value: object, field_name: str) -> str:
    return _bounded_text(value, field_name, MAX_OBSERVATION_IDENTIFIER_BYTES)


def _short_text_or_none(value: object, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, field_name, MAX_OBSERVATION_SHORT_TEXT_BYTES)


def _name_or_none(value: object, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, field_name, MAX_OBSERVATION_NAME_BYTES)


def _index_or_none(value: object, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise _invalid(field_name)
    return value


def _state_or_none(value: object, field_name: str) -> Optional[ControllerState]:
    if value is None:
        return None
    if type(value) is not ControllerState:
        raise _invalid(field_name)
    return value


def _enum_or_none(value: object, enum_type: type, field_name: str) -> Optional[object]:
    if value is None:
        return None
    if type(value) is not enum_type:
        raise _invalid(field_name)
    return value


@dataclass(frozen=True)
class ControllerObservation:
    """One immutable, validated controller-native observation.

    All fields are detached immutable values: enums, bounded strings and
    integers.  An observer holding a reference to an observation can never
    change controller state, steps, snapshots, or the final result.
    """

    kind: ControllerObservationKind
    run_id: str
    task_id: str
    model_call_index: Optional[int] = None
    step_index: Optional[int] = None
    state_before: Optional[ControllerState] = None
    state_after: Optional[ControllerState] = None
    target_state: Optional[ControllerState] = None
    directive_kind: Optional[str] = None
    request_status: Optional[str] = None
    tool_name: Optional[str] = None
    observation_status: Optional[ObservationStatus] = None
    rejection_category: Optional[str] = None
    transition_reason: Optional[str] = None
    stop_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.kind) is not ControllerObservationKind:
            raise _invalid("kind")
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        object.__setattr__(self, "task_id", _identifier(self.task_id, "task_id"))
        object.__setattr__(
            self, "model_call_index", _index_or_none(self.model_call_index, "model_call_index")
        )
        object.__setattr__(self, "step_index", _index_or_none(self.step_index, "step_index"))
        object.__setattr__(self, "state_before", _state_or_none(self.state_before, "state_before"))
        object.__setattr__(self, "state_after", _state_or_none(self.state_after, "state_after"))
        object.__setattr__(self, "target_state", _state_or_none(self.target_state, "target_state"))
        object.__setattr__(
            self, "directive_kind", _short_text_or_none(self.directive_kind, "directive_kind")
        )
        object.__setattr__(
            self, "request_status", _short_text_or_none(self.request_status, "request_status")
        )
        object.__setattr__(self, "tool_name", _name_or_none(self.tool_name, "tool_name"))
        object.__setattr__(
            self,
            "observation_status",
            _enum_or_none(self.observation_status, ObservationStatus, "observation_status"),
        )
        object.__setattr__(
            self,
            "rejection_category",
            _name_or_none(self.rejection_category, "rejection_category"),
        )
        object.__setattr__(
            self,
            "transition_reason",
            (
                None
                if self.transition_reason is None
                else _bounded_text(
                    self.transition_reason,
                    "transition_reason",
                    MAX_OBSERVATION_TRANSITION_REASON_BYTES,
                )
            ),
        )
        object.__setattr__(
            self, "stop_reason", _short_text_or_none(self.stop_reason, "stop_reason")
        )


class ControllerObserver(Protocol):
    """Optional synchronous observer of controller-native observations.

    Implementations must be side-effect safe: an ordinary ``Exception``
    raised here is swallowed by the controller and never changes its
    decisions; observations must be treated as read-only.
    """

    def notify(self, observation: ControllerObservation) -> None: ...


class NoopControllerObserver:
    """Default observer: receives observations and does nothing."""

    def notify(self, observation: ControllerObservation) -> None:
        return None


__all__ = [
    "MAX_OBSERVATION_IDENTIFIER_BYTES",
    "MAX_OBSERVATION_NAME_BYTES",
    "MAX_OBSERVATION_SHORT_TEXT_BYTES",
    "MAX_OBSERVATION_TRANSITION_REASON_BYTES",
    "ControllerObservation",
    "ControllerObservationError",
    "ControllerObservationKind",
    "ControllerObserver",
    "NoopControllerObserver",
]
