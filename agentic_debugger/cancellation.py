"""Neutral operational cancellation contract for the session worker boundary.

Cancellation is an operational cross-cutting concern shared by the
controller, the worker, and subprocess execution.  This module defines the
one neutral contract that every layer can import without violating the
repository dependency direction:

- :class:`CancellationReason` — why execution was requested to stop
  (``cancelled`` vs ``timed_out``);
- :class:`CancellationError` — the single explicit cancellation signal;
- :class:`CancellationToken` — cancellable state with an optional monotonic
  deadline; ``check()`` raises :class:`CancellationError` when the token has
  been requested or its deadline passed.

Rules:

- ``agentic_debugger.agent`` (controller) may import this module and must
  never convert :class:`CancellationError` into a scientific stop reason
  (``MODEL_ERROR``, ``CONTROLLER_ERROR``, policy rejection, or budget
  exhaustion);
- ``agentic_debugger.runtime`` (command execution) may import this module
  without depending on the controller;
- the application worker owns and requests the token.

Cancellation is operational: it is deliberately separate from the scientific
``ControllerStopReason`` taxonomy and never appears inside a
``ControllerRunResult``.
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Optional

from agentic_debugger import AgenticDebuggerError


class CancellationReason(str, Enum):
    """Why a cooperative cancellation was requested."""

    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class CancellationError(AgenticDebuggerError):
    """The one explicit cancellation signal.

    Carries the :class:`CancellationReason` that fired.  Callers on the
    cooperative path catch this type; it must never be converted into a
    scientific controller stop reason or an ordinary failed test result.
    """

    def __init__(self, reason: CancellationReason) -> None:
        if type(reason) is not CancellationReason:
            raise TypeError("reason must be a CancellationReason")
        super().__init__(f"cancellation requested: {reason.value}")
        self.reason = reason


class CancellationToken:
    """Thread-safe cancellable state with an optional monotonic deadline.

    ``request`` is idempotent: the first reason wins.  ``check`` raises
    :class:`CancellationError` when the token was requested or (when a
    deadline was configured) the deadline passed; the deadline records
    :attr:`reason` as ``timed_out`` so the owner can distinguish the two.
    """

    def __init__(self, *, deadline_monotonic: Optional[float] = None) -> None:
        if deadline_monotonic is not None:
            if type(deadline_monotonic) is not float and type(deadline_monotonic) is not int:
                raise TypeError("deadline_monotonic must be a number or None")
            if not (deadline_monotonic > 0):
                raise ValueError("deadline_monotonic must be positive")
        self._deadline: Optional[float] = (
            None if deadline_monotonic is None else float(deadline_monotonic)
        )
        self._event = threading.Event()
        self._reason: Optional[CancellationReason] = None
        self._lock = threading.Lock()

    @property
    def deadline(self) -> Optional[float]:
        """The configured monotonic deadline, or ``None``."""
        return self._deadline

    @property
    def is_cancelled(self) -> bool:
        """Whether cancellation (or the deadline) has been requested/fired."""
        return self._event.is_set()

    @property
    def reason(self) -> Optional[CancellationReason]:
        """The recorded reason, or ``None`` before cancellation fires."""
        return self._reason

    def request(self, reason: CancellationReason = CancellationReason.CANCELLED) -> None:
        """Request cooperative cancellation; the first reason wins."""
        if type(reason) is not CancellationReason:
            raise TypeError("reason must be a CancellationReason")
        with self._lock:
            if self._reason is None:
                self._reason = reason
        self._event.set()

    def check(self) -> None:
        """Raise :class:`CancellationError` if cancellation was requested or
        the deadline passed.

        An explicit request always wins over the deadline: a user cancellation
        that races the deadline reports ``cancelled``.
        """
        if self._event.is_set():
            raise CancellationError(self._reason or CancellationReason.CANCELLED)
        if self._deadline is not None and time.monotonic() >= self._deadline:
            with self._lock:
                if self._reason is None:
                    self._reason = CancellationReason.TIMED_OUT
                reason = self._reason
            self._event.set()
            raise CancellationError(reason)


__all__ = [
    "CancellationError",
    "CancellationReason",
    "CancellationToken",
]
