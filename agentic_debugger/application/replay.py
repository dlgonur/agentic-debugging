"""Read-only replay cursor over a validated session event stream.

Replay uses the same presentation reducer as live application events:
:class:`SessionReplaySource` is a :class:`SessionEventSource` cursor over an
already-loaded tuple of validated :class:`SessionEvent` values.  It never
invokes the controller, model, PDB, patch application, verifier, or cleanup:
it is pure read-only navigation over recorded events.

The cursor supports the presentation controls a future UI needs: next/
previous event, seek to an index, jump to a sequence, rewind, end detection,
and derived phase-boundary indices.  UI-owned selection, scroll/filter, and
cursor state beyond this contract is not part of the application model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    SourceKind,
)

__all__ = ["SessionReplaySource", "phase_boundaries"]


@dataclass(frozen=True)
class SessionReplaySource:
    """Read-only replay cursor over one validated session event stream.

    ``events`` must already be validated (a journal read or an adapter
    result); the cursor itself performs no I/O and never executes any
    session logic.  ``next_event`` returns the event at the current index
    and advances; ``None`` means the stream is exhausted.  ``seek`` and
    ``previous_event`` move the cursor without re-running anything.
    """

    events: Tuple[SessionEvent, ...]
    source_kind: SourceKind
    task_id: str
    session_id: Optional[str] = None
    index: int = 0

    def __post_init__(self) -> None:
        if type(self.source_kind) is not SourceKind:
            raise ApplicationInputError("source_kind must be a SourceKind")
        if type(self.task_id) is not str or not self.task_id:
            raise ApplicationInputError("task_id must be a non-empty string")
        if self.session_id is not None and type(self.session_id) is not str:
            raise ApplicationInputError("session_id must be a string or None")
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "index", self._validated_index(self.index))
        for position, event in enumerate(self.events):
            if event.sequence != position:
                raise ApplicationInputError(
                    f"replay events must be contiguous from 0; "
                    f"event {position} has sequence {event.sequence}"
                )
            if event.task_id != self.task_id:
                raise ApplicationInputError(
                    f"replay event task_id {event.task_id!r} does not match "
                    f"the cursor task_id {self.task_id!r}"
                )
            if event.source_kind is not self.source_kind:
                raise ApplicationInputError(
                    "replay event source_kind does not match the cursor"
                )
            if (
                self.session_id is not None
                and event.session_id != self.session_id
            ):
                raise ApplicationInputError(
                    "replay event session_id does not match the cursor"
                )

    def _validated_index(self, value: int) -> int:
        if type(value) is not int or isinstance(value, bool) or value < 0:
            raise ApplicationInputError("replay index must be a non-negative integer")
        if value > len(self.events):
            raise ApplicationInputError("replay index is beyond the stream")
        return value

    # -- SessionEventSource contract ----------------------------------------

    def next_event(self) -> Optional[SessionEvent]:
        """Return the event at the cursor and advance; ``None`` at the end."""
        if self.index >= len(self.events):
            return None
        event = self.events[self.index]
        object.__setattr__(self, "index", self.index + 1)
        return event

    def close(self) -> None:
        """Read-only cursor holds no resources; no-op."""

    # -- navigation ---------------------------------------------------------

    @property
    def total_events(self) -> int:
        return len(self.events)

    @property
    def at_beginning(self) -> bool:
        return self.index == 0

    @property
    def at_end(self) -> bool:
        return self.index >= len(self.events)

    def rewind(self) -> None:
        """Move the cursor to the beginning."""
        object.__setattr__(self, "index", 0)

    def seek(self, index: int) -> None:
        """Move the cursor to ``index`` (0..total_events inclusive)."""
        object.__setattr__(self, "index", self._validated_index(index))

    def seek_to_sequence(self, sequence: int) -> bool:
        """Jump to the first event with ``sequence``; False when absent."""
        if type(sequence) is not int or isinstance(sequence, bool) or sequence < 0:
            raise ApplicationInputError("sequence must be a non-negative integer")
        for position, event in enumerate(self.events):
            if event.sequence == sequence:
                object.__setattr__(self, "index", position)
                return True
        return False

    def previous_event(self) -> Optional[SessionEvent]:
        """Move back one event and return it; ``None`` at the beginning."""
        if self.index == 0:
            return None
        object.__setattr__(self, "index", self.index - 1)
        return self.events[self.index]


def phase_boundaries(
    events: Tuple[SessionEvent, ...],
) -> Tuple[int, ...]:
    """Indices where the effective presentation phase/controller phase changes.

    Derived for replay navigation ("jump to next controller phase"); purely
    informational and read-only.  The helper tracks *effective* state
    (Repair Pass 2): a session phase changes only when a
    ``session.status_changed`` event actually records a new phase, and a
    controller phase changes only when an event actually carries a non-null
    ``controller_phase``.  Ordinary events that omit these values never
    reset the current effective value to ``None``, so a phase persists
    across tool/debugger/verifier events that do not restate it.
    """
    boundaries: list[int] = []
    previous_phase: Optional[str] = None
    previous_controller: Optional[str] = None
    for index, event in enumerate(events):
        phase = previous_phase
        if event.event_kind is SessionEventKind.SESSION_STATUS_CHANGED:
            phase = event.payload.get("phase")
        controller: Optional[str] = previous_controller
        if event.controller_phase is not None:
            controller = event.controller_phase.value
        if index == 0 or phase != previous_phase or controller != previous_controller:
            boundaries.append(index)
        previous_phase = phase
        previous_controller = controller
    return tuple(boundaries)
