"""UI-owned presentation control models for the Local Application V1 TUI.

This module is deliberately Textual-free so the navigation and live-session
state machines stay unit-testable without a terminal.  It owns only UI
concerns that the application layer deliberately excludes from
``SessionViewState`` (architecture section 7.4):

- the read-only replay cursor and its derived presentation state;
- the live worker driver that consumes worker notifications and the
  authoritative journal and hands the app new event prefixes.

Everything here is presentation-only:

- :class:`ReplayController` never invokes the controller, model, PDB, patch
  application, verifier, or cleanup: navigation only moves the cursor,
  reduces the persisted prefix, and renders ``SessionViewState``;
- :class:`LiveSessionRunner` only supervises the accepted
  :class:`~agentic_debugger.application.worker_process.SessionWorkerProcess`
  boundary: it starts the worker, forwards new journal prefixes, waits for
  the operational terminal result, registers the session into app-owned
  history, and never runs controller/PDB/verifier work on the Textual event
  loop (callbacks are invoked from the runner's own background thread).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Tuple, runtime_checkable

from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
)
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    SessionViewState,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.application.process_tree import pid_is_alive
from agentic_debugger.application.replay import SessionReplaySource, phase_boundaries
from agentic_debugger.application.session import SessionResult
from agentic_debugger.application.worker_process import SessionWorkerProcess

_TERMINAL_KINDS = frozenset(
    {
        SessionEventKind.SESSION_COMPLETED,
        SessionEventKind.SESSION_FAILED,
        SessionEventKind.SESSION_CANCELLED,
    }
)

_DEFAULT_POLL_INTERVAL_SECONDS = 0.05
_CLOSE_JOIN_TIMEOUT_SECONDS = 60.0


@runtime_checkable
class LiveWorker(Protocol):
    """The worker supervision surface the live runner drives.

    The accepted :class:`SessionWorkerProcess` satisfies this protocol; the
    protocol keeps the runner testable with a scripted double without
    weakening any supervision semantics.
    """

    pid: Optional[int]
    events: Tuple[SessionEvent, ...]
    session_dir: Path

    def start(self) -> Optional[SessionResult]: ...
    def cancel(self) -> None: ...
    def wait(self) -> SessionResult: ...
    def close(self) -> None: ...


class ReplayController:
    """Read-only replay navigation over one recorded session.

    The controller owns the cursor index, the phase-boundary indices, and
    the reduced presentation state for the current prefix.  All navigation
    is pure: ``next``/``previous``/``begin``/``end``/``seek`` only move the
    cursor and re-reduce persisted events.  No executable resource is ever
    touched.
    """

    def __init__(
        self,
        replay: SessionReplaySource,
        identity: PresentationIdentity,
    ) -> None:
        if type(replay) is not SessionReplaySource:
            raise TypeError("replay must be a SessionReplaySource")
        if type(identity) is not PresentationIdentity:
            raise TypeError("identity must be a PresentationIdentity")
        self._replay = replay
        self._identity = identity
        self._index = 0
        self._view = initial_session_view(identity)
        self._boundaries = phase_boundaries(replay.events)

    # -- cursor state -------------------------------------------------------

    @property
    def replay(self) -> SessionReplaySource:
        return self._replay

    @property
    def identity(self) -> PresentationIdentity:
        return self._identity

    @property
    def index(self) -> int:
        """Number of events reduced into ``view`` (the cursor position)."""
        return self._index

    @property
    def total_events(self) -> int:
        return self._replay.total_events

    @property
    def view(self) -> SessionViewState:
        return self._view

    @property
    def at_beginning(self) -> bool:
        return self._index == 0

    @property
    def at_end(self) -> bool:
        return self._index >= self.total_events

    @property
    def phase_boundaries(self) -> Tuple[int, ...]:
        """Effective phase-boundary indices (derived, read-only)."""
        return self._boundaries

    def current_boundary(self) -> int:
        """The greatest boundary index at or before the cursor (or 0)."""
        boundary = 0
        for item in self._boundaries:
            if item > self._index:
                break
            boundary = item
        return boundary

    # -- navigation ---------------------------------------------------------

    def next(self) -> Optional[SessionEvent]:
        """Reduce the next recorded event and return it; None at the end."""
        event = self._replay.next_event()
        if event is None:
            return None
        self._index += 1
        self._view = reduce_event(self._view, event)
        return event

    def previous(self) -> Optional[SessionEvent]:
        """Move back one event and re-reduce the prefix; None at the start."""
        if self._index == 0:
            return None
        self._index -= 1
        self._replay.seek(self._index)
        self._view = self._reduce_prefix(self._index)
        return self._replay.events[self._index]

    def begin(self) -> None:
        """Rewind to the beginning (no events reduced)."""
        self._index = 0
        self._replay.rewind()
        self._view = initial_session_view(self._identity)

    def end(self) -> None:
        """Jump to the end and reduce the complete recorded stream."""
        self._index = self.total_events
        self._replay.seek(self._index)
        self._view = self._reduce_prefix(self._index)

    def seek(self, index: int) -> None:
        """Move the cursor to ``index`` (0..total_events)."""
        if type(index) is not int or isinstance(index, bool) or index < 0:
            raise ValueError("index must be a non-negative integer")
        index = min(index, self.total_events)
        self._index = index
        self._replay.seek(index)
        self._view = self._reduce_prefix(index)

    def seek_sequence(self, sequence: int) -> bool:
        """Jump to the first event with ``sequence``; False when absent."""
        if self._replay.seek_to_sequence(sequence):
            self._index = self._replay.index
            self._view = self._reduce_prefix(self._index)
            return True
        return False

    def next_phase(self) -> bool:
        """Jump to the next effective phase boundary; False when already
        at the last boundary."""
        for boundary in self._boundaries:
            if boundary > self._index:
                self.seek(boundary)
                return True
        return False

    def previous_phase(self) -> bool:
        """Jump back to the previous effective phase boundary; False when
        there is none before the cursor."""
        target: Optional[int] = None
        for boundary in self._boundaries:
            if boundary >= self._index:
                break
            target = boundary
        if target is None:
            return False
        self.seek(target)
        return True

    def _reduce_prefix(self, index: int) -> SessionViewState:
        """Re-reduce the recorded prefix ``events[0:index]`` from scratch.

        Pure in-memory reduction; used for backward navigation where the
        forward-incremental view cannot be reused.
        """
        view = initial_session_view(self._identity)
        for event in self._replay.events[:index]:
            view = reduce_event(view, event)
        return view


class LiveSessionRunner:
    """Background driver of one live :class:`SessionWorkerProcess`.

    The driver thread owns the blocking supervision calls
    (``worker.start()`` / ``worker.wait()``) so the Textual event loop never
    runs worker, controller, PDB, patch, or verifier work.  Callbacks are
    invoked from the driver thread; the app must marshal them back to the
    event loop (``App.call_from_thread``) when they touch widgets.

    Lifecycle:

    - ``start()`` spawns the driver thread; the worker handshake happens in
      it (a startup failure surfaces through ``on_failure``);
    - new journal prefixes surface through ``on_events`` (the durable
      journal is authoritative; the pipe carries only sequence
      notifications, so a missed notification cannot lose evidence);
    - the operational terminal result surfaces through ``on_terminal``
      after the accepted ``worker.wait()`` classification, together with
      the app-owned history registration outcome;
    - ``cancel()`` requests cooperative cancellation (idempotent);
    - ``close()`` shuts the driver down, requests cancellation, waits only
      within the accepted bounded worker semantics (cooperative grace then
      Task-3 escalation), and closes every handle so no worker process can
      be stranded by UI teardown.
    """

    def __init__(
        self,
        worker: SessionWorkerProcess,
        *,
        history_store: Optional[HistoryStore] = None,
        on_started: Callable[[], None],
        on_events: Callable[[Tuple[SessionEvent, ...]], None],
        on_terminal: Callable[[SessionResult, Optional[str]], None],
        on_failure: Callable[[str], None],
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        if not isinstance(worker, LiveWorker):
            raise TypeError(
                "worker must satisfy the LiveWorker supervision protocol "
                "(SessionWorkerProcess)"
            )
        if history_store is not None and type(history_store) is not HistoryStore:
            raise TypeError("history_store must be a HistoryStore or None")
        for name, callback in (
            ("on_started", on_started),
            ("on_events", on_events),
            ("on_terminal", on_terminal),
            ("on_failure", on_failure),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        if type(poll_interval_seconds) not in (int, float) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be a positive number")
        self._worker = worker
        self._history_store = history_store
        self._on_started = on_started
        self._on_events = on_events
        self._on_terminal = on_terminal
        self._on_failure = on_failure
        self._poll_interval = float(poll_interval_seconds)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._terminal: Optional[SessionResult] = None
        self._delivered_count = 0

    # -- state --------------------------------------------------------------

    @property
    def worker(self) -> SessionWorkerProcess:
        return self._worker

    @property
    def started(self) -> bool:
        return self._started

    @property
    def terminal(self) -> Optional[SessionResult]:
        return self._terminal

    @property
    def is_alive(self) -> bool:
        """Whether a live session may still be producing events."""
        return (
            self._started
            and self._terminal is None
            and self._thread is not None
            and self._thread.is_alive()
        )

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Spawn the background driver thread (idempotent)."""
        if self._thread is not None:
            return
        thread = threading.Thread(
            target=self._drive, name="live-session-driver", daemon=True
        )
        self._thread = thread
        thread.start()

    def cancel(self) -> None:
        """Request cooperative cancellation through the accepted worker
        boundary; safe to repeat."""
        self._worker.cancel()

    def close(self) -> None:
        """Shut the driver down and release the worker without orphaning it.

        Sets the stop flag, requests cooperative cancellation, and waits for
        the driver thread only within the accepted bounded worker semantics
        (the worker itself enforces the cooperative grace and then the
        Task-3 forced escalation).  ``worker.close()`` then releases every
        handle; on Windows closing the job object terminates any remaining
        job member, so no worker descendant survives UI teardown.
        """
        self._stop.set()
        self._worker.cancel()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_CLOSE_JOIN_TIMEOUT_SECONDS)
        self._worker.close()

    # -- driver -------------------------------------------------------------

    def _call(self, callback: Callable[..., None], *args: Any) -> None:
        try:
            callback(*args)
        except Exception:
            # A UI callback must never kill the supervision thread; the
            # worker/journal remain authoritative.
            pass

    def _drive(self) -> None:
        try:
            startup = self._worker.start()
        except Exception as exc:
            self._call(self._on_failure, f"worker startup failed: {exc}")
            self._worker.close()
            return
        if startup is not None:
            diagnostic = "; ".join(startup.diagnostics[:3]) or "unspecified"
            self._call(self._on_failure, f"worker startup failed: {diagnostic}")
            self._best_effort_register()
            self._worker.close()
            return
        self._started = True
        self._call(self._on_started)
        self._drive_events()
        if self._stop.is_set():
            # UI teardown requested: cancel and reap only within the
            # accepted bounded worker semantics; no callback is needed.
            self._worker.cancel()
            try:
                self._worker.wait()
            except Exception:
                pass
            self._worker.close()
            return
        self._finish_terminal()

    def _drive_events(self) -> None:
        """Surface new journal prefixes; stop on terminal or worker exit."""
        last = 0
        while not self._stop.is_set():
            events = self._worker.events
            if len(events) > last:
                last = len(events)
                self._delivered_count = last
                self._call(self._on_events, events)
            if events and events[-1].event_kind in _TERMINAL_KINDS:
                return
            pid = self._worker.pid
            if pid is not None and not pid_is_alive(pid):
                # The worker exited without a durable terminal event (crash/
                # interruption): let the accepted supervisor classification
                # produce the honest terminal result.
                return
            time.sleep(self._poll_interval)

    def _finish_terminal(self) -> None:
        try:
            result = self._worker.wait()
        except Exception as exc:
            self._call(self._on_failure, f"worker supervision failed: {exc}")
            self._worker.close()
            return
        # The worker can write its terminal event and exit between two polls;
        # ``wait()``'s final journal catch-up then holds events the driver
        # never delivered.  Deliver the remainder before the terminal result
        # so the presentation model always reaches the recorded final state.
        remaining = self._worker.events
        if len(remaining) > self._delivered_count:
            self._delivered_count = len(remaining)
            self._call(self._on_events, remaining)
        self._terminal = result
        registration_error = self._best_effort_register()
        self._call(self._on_terminal, result, registration_error)
        self._worker.close()

    def _best_effort_register(self) -> Optional[str]:
        """Register the finished session into app-owned history.

        Interrupted/crash sessions register with their honest classification;
        a missing/invalid journal simply cannot register and is reported, it
        is never rewritten into a successful-looking session.
        """
        if self._history_store is None:
            return None
        try:
            self._history_store.register(self._worker.session_dir)
            return None
        except Exception as exc:
            return f"history registration failed: {exc}"


__all__ = [
    "LiveSessionRunner",
    "LiveWorker",
    "ReplayController",
]
