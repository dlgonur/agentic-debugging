"""Application adapter for the independent verifier's progress observation.

This module converts the real verifier's optional stage observation (see
``evaluation.verifier.VerifierProgressObserver``) and its authoritative final
:class:`EvaluationResult` into Task-1 :class:`SessionEvent` values.

Boundary rules (Task-4 observability):

- Stage events are progress only.  They are never a correctness authority:
  the final existing :class:`EvaluationResult` remains the only authority,
  and this adapter never changes verifier behavior, result, classification,
  or workspace semantics (the verifier swallows ordinary observer failures).
- ``verifier.completed`` is built only from a real returned
  ``EvaluationResult``; a cancelled/interrupted verifier run emits no
  ``verifier.completed`` event, so cancellation stays operational and is
  never converted into a scientific verdict.
- The adapter is NOT the session lifecycle owner and never fabricates
  ``session.*`` / ``cleanup.*`` events.
- Identity is fail-closed: every emitted event carries the configured
  session/task/run identity and a contiguous sequence owned by this adapter
  (starting at ``context.initial_sequence``).

Import note: this module imports the verifier's observer protocol only; it
executes no verifier logic itself.  Like ``application.controller_adapter``,
it is deliberately not imported by ``application/__init__.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from agentic_debugger.application import ApplicationContractError, ApplicationInputError
from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    VerifierStage,
    VerifierStageStatus,
    validate_utc_timestamp,
)
from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.observability import ObservabilityContext
from agentic_debugger.application.sources import SessionEventSink


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


class VerifierSessionEventAdapter:
    """Emit validated verifier progress/final events for one session.

    Implements the verifier's observer protocol (``stage_started`` /
    ``stage_completed``) so it can be passed directly as the
    ``progress_observer`` of :class:`EvaluationVerifier`; the caller emits
    ``started()`` before ``evaluate()`` and ``completed(result)`` after a
    normal return.

    Sequence authority (Repair Pass 3): by default this adapter owns a
    private :class:`SessionEventEmitter` starting at
    ``context.initial_sequence``, so standalone tests stay possible.  When a
    shared ``emitter`` is supplied, ALL producers of the session emit
    through it, making the emitter the one authoritative sequence/identity/
    clock owner and the journal-failure gate.  The shared emitter identity
    must match the adapter context (fail closed).
    """

    def __init__(
        self,
        context: ObservabilityContext,
        *,
        clock: Callable[[], str] | None = None,
        sink: SessionEventSink | None = None,
        emitter: SessionEventEmitter | None = None,
    ) -> None:
        if type(context) is not ObservabilityContext:
            raise ApplicationInputError("context must be an ObservabilityContext")
        self._context = context
        self._clock = _validated_clock(clock) if clock is not None else _default_clock
        self._events: list[SessionEvent] = []
        self._emitter = self._resolve_emitter(context, emitter, clock, sink)

    def _resolve_emitter(
        self,
        context: ObservabilityContext,
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
            # ``session.started``, never by a producer at construction time.
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
    def context(self) -> ObservabilityContext:
        return self._context

    @property
    def emitter(self) -> SessionEventEmitter:
        """The session's shared emission authority (sequence owner)."""
        return self._emitter

    def events(self) -> Tuple[SessionEvent, ...]:
        """The produced session events in emission (sequence) order."""
        return tuple(self._events)

    def _emit(self, kind: SessionEventKind, payload: Dict[str, Any]) -> SessionEvent:
        event = self._emitter.emit(kind, payload)
        self._events.append(event)
        return event

    def started(self) -> SessionEvent:
        """One ``verifier.started`` event (call before ``evaluate``)."""
        return self._emit(SessionEventKind.VERIFIER_STARTED, {})

    def stage_started(self, stage: str) -> None:
        """Verifier observer protocol: emit ``verifier.stage_started``."""
        try:
            validated = VerifierStage(stage)
        except ValueError:
            raise ApplicationInputError(
                f"verifier reported an unknown stage: {stage!r}"
            ) from None
        self._emit(
            SessionEventKind.VERIFIER_STAGE_STARTED, {"stage": validated.value}
        )

    def stage_completed(self, stage: str, status: str) -> None:
        """Verifier observer protocol: emit ``verifier.stage_completed``."""
        try:
            validated = VerifierStage(stage)
            validated_status = VerifierStageStatus(status)
        except ValueError:
            raise ApplicationInputError(
                f"verifier reported an unknown stage/status: {stage!r}/{status!r}"
            ) from None
        self._emit(
            SessionEventKind.VERIFIER_STAGE_COMPLETED,
            {"stage": validated.value, "status": validated_status.value},
        )

    def completed(self, evaluation: Any) -> SessionEvent:
        """One ``verifier.completed`` event from a real ``EvaluationResult``.

        Only ever called with the verifier's own returned result; the payload
        carries the exact recorded status, outcome, F2P/P2P counts, and
        workspace-cleanup flag.  This is the final verifier authority copied
        into presentation, never a derived verdict.
        """
        return self._emit(
            SessionEventKind.VERIFIER_COMPLETED,
            {
                "status": (
                    evaluation.status.value if getattr(evaluation, "status", None) is not None else None
                ),
                "outcome": (
                    evaluation.outcome.value if getattr(evaluation, "outcome", None) is not None else None
                ),
                "f2p_passed": getattr(evaluation, "f2p_passed", None),
                "f2p_total": getattr(evaluation, "f2p_total", None),
                "p2p_passed": getattr(evaluation, "p2p_passed", None),
                "p2p_total": getattr(evaluation, "p2p_total", None),
                "workspace_cleaned": getattr(
                    getattr(evaluation, "workspace", None), "cleaned", None
                ),
                **(
                    {"private_checks_passed": evaluation.private_checks_passed}
                    if getattr(evaluation, "private_checks_passed", None) is not None
                    else {}
                ),
            },
        )


__all__ = ["VerifierSessionEventAdapter"]
