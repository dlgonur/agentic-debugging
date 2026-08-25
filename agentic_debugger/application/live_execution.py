"""Typed, observer-only projection for a live execution session.

This module intentionally has no dependency on the controller, verifier,
journal, history, transport, or Textual.  Durable facts come from the shared
``SessionViewState`` reducer; the optional snapshot is a bounded latest-value
side-band and is never persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from agentic_debugger.application.events import (
    OperatorStage,
    SessionEventKind,
)
from agentic_debugger.application.presentation import SessionViewState, TimelineEntry


class ExecutionMode(str, Enum):
    LIVE = "live"
    REPLAY = "replay"


class OperationKind(str, Enum):
    STARTING = "starting"
    PREPARING = "preparing"
    CONTROLLER = "controller"
    MODEL_REQUEST = "model_request"
    WAITING_FOR_MODEL = "waiting_for_model"
    TOOL = "tool"
    DEBUGGER = "debugger"
    CANDIDATE = "candidate"
    VERIFIER = "verifier"
    OFFICIAL_VERIFIER = "official_verifier"
    CLEANUP = "cleanup"
    CANCELLING = "cancelling"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class EphemeralSnapshot:
    """Safe worker-local timing facts advanced from a parent clock.

    Durations are measured by the worker and carry no monotonic clock origin,
    token text, prompt, completion, source, or tool payload.
    """

    generation: int
    request_index: Optional[int]
    request_elapsed_seconds: Optional[float]
    last_activity_age_seconds: Optional[float]
    transport_alive: bool
    watchdog_idle_seconds: Optional[float]
    received_monotonic: float

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a non-negative int")
        if self.request_index is not None and (
            type(self.request_index) is not int or self.request_index < 0
        ):
            raise ValueError("request_index must be a non-negative int or None")
        for name in (
            "request_elapsed_seconds",
            "last_activity_age_seconds",
            "watchdog_idle_seconds",
            "received_monotonic",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, (int, float)) or value < 0):
                raise ValueError(f"{name} must be a non-negative number or None")


@dataclass(frozen=True)
class KnownCeilings:
    model_requests: Optional[int] = None
    controller_steps: Optional[int] = None
    candidate_attempts: Optional[int] = None


@dataclass(frozen=True)
class LiveExecutionState:
    """Presentation-ready live/replay state without changing durable truth."""

    view: SessionViewState
    mode: ExecutionMode
    ceilings: KnownCeilings
    snapshot: Optional[EphemeralSnapshot]
    now_monotonic: float
    operation: OperationKind
    operation_label: str
    request_index: Optional[int]
    controller_step: Optional[int]
    candidate_attempt: Optional[int]
    current_target: Optional[str]
    elapsed_seconds: Optional[float]
    request_elapsed_seconds: Optional[float]
    last_activity_age_seconds: Optional[float]
    recent_operations: Tuple[TimelineEntry, ...]

    @property
    def live(self) -> bool:
        return self.mode is ExecutionMode.LIVE and not self.view.status.terminal

    @property
    def waiting_for_model(self) -> bool:
        return self.operation is OperationKind.WAITING_FOR_MODEL

    @property
    def request_ordinal(self) -> Optional[int]:
        """One-based UI ordinal; durable/transport identities stay zero-based.

        ``Request N / 40`` means: N actual provider/model transport
        requests have been made so far in this session (one-based display
        of the runtime request index), against the 40-request treatment
        budget.  The authoritative runtime counter
        (``LiveModelAdapter.metrics.model_requests``) increments for every
        ``transport.request()`` attempt, so a transport retry consumes the
        next Request ordinal -- retries are never hidden from the counter.
        Controller steps (accepted directive cycles) and candidate attempts
        (real apply outcomes) are independent counters with the same
        one-based display rule; internal ids everywhere remain zero-based.
        """
        return self.request_index + 1 if self.request_index is not None else None

    @property
    def controller_step_ordinal(self) -> Optional[int]:
        return self.controller_step + 1 if self.controller_step is not None else None

    @property
    def candidate_attempt_ordinal(self) -> Optional[int]:
        return self.candidate_attempt + 1 if self.candidate_attempt is not None else None


def _target(view: SessionViewState) -> Optional[str]:
    if view.debugger.script and view.debugger.line:
        return f"{view.debugger.script}:{view.debugger.line}"
    if view.current_tool_target:
        return view.current_tool_target
    if view.sources:
        return view.sources[-1].path
    if view.diagnosis and view.diagnosis.file_path:
        return view.diagnosis.file_path
    return None


def _operation(view: SessionViewState, outstanding: Optional[int], snapshot: Optional[EphemeralSnapshot], now: float) -> tuple[OperationKind, str]:
    # Precedence is deliberately explicit: a changing candidate or request
    # never mutates the durable global lifecycle phase.
    if view.status.terminal:
        return OperationKind.TERMINAL, view.status.value.replace("_", " ").title()
    if any(entry.event_kind is SessionEventKind.SESSION_CANCEL_REQUESTED for entry in view.timeline):
        return OperationKind.CANCELLING, "Cancelling"
    stage = view.operator_stage
    if stage in (OperatorStage.OFFICIAL_VERIFICATION_PREPARING, OperatorStage.OFFICIAL_EVALUATOR_STARTED, OperatorStage.OFFICIAL_EVALUATOR_COMPLETED):
        return OperationKind.OFFICIAL_VERIFIER, "Official verification"
    if stage is OperatorStage.VERIFICATION or view.verifier_stages:
        return OperationKind.VERIFIER, "Verifier running"
    if stage in (OperatorStage.CLEANUP, OperatorStage.FINALIZING):
        return OperationKind.CLEANUP, stage.value.replace("_", " ").title()
    if view.current_tool_name == "apply_patch":
        # The in-flight candidate ordinal is derived from completed typed
        # candidate facts only: the attempt being applied is always the
        # one-based successor of the last recorded attempt outcome.
        attempt = (
            view.patch_attempts[-1].attempt_index + 2
            if view.patch_attempts
            else 1
        )
        return OperationKind.CANDIDATE, f"Applying candidate (attempt {attempt})"
    if view.current_tool_name:
        label = f"Running {view.current_tool_name}"
        if view.current_tool_target:
            label = f"{label} ({view.current_tool_target})"
        return OperationKind.TOOL, label
    if outstanding is not None:
        if snapshot is not None and snapshot.transport_alive and snapshot.last_activity_age_seconds is not None:
            age = snapshot.last_activity_age_seconds + max(0.0, now - snapshot.received_monotonic)
            if age > 5.0:
                return OperationKind.WAITING_FOR_MODEL, "Waiting for model response"
        return OperationKind.MODEL_REQUEST, f"Model request {outstanding + 1}"
    if view.debugger.session_started:
        # A started debugger session is displayed as active the moment the
        # typed debugger event exists; ``pdb_observed`` (the exact-proof
        # observation fact) is the only upgrade to Observed and is carried
        # separately by the PDB field, never by this label.
        return OperationKind.DEBUGGER, "Debugger active"
    if view.patch_attempts and view.patch_attempts[-1].stage.value in {"proposed", "applied"}:
        return OperationKind.CANDIDATE, f"Candidate attempt {view.patch_attempts[-1].attempt_index + 1}"
    if view.controller_phase is not None:
        return OperationKind.CONTROLLER, f"Controller: {view.controller_phase.value}"
    return OperationKind.PREPARING, "Preparing execution"


def project_live_execution(
    view: SessionViewState,
    *,
    mode: ExecutionMode,
    ceilings: KnownCeilings = KnownCeilings(),
    snapshot: Optional[EphemeralSnapshot] = None,
    now_monotonic: float = 0.0,
    elapsed_seconds: Optional[float] = None,
) -> LiveExecutionState:
    """Project durable state plus an optional correctly-scoped live snapshot."""
    if mode is ExecutionMode.REPLAY or view.status.terminal:
        snapshot = None
    outstanding = view.outstanding_model_request_index
    request_index = outstanding if outstanding is not None else view.latest_model_request_index
    if snapshot is not None and snapshot.request_index is not None:
        request_index = snapshot.request_index
    request_elapsed = None
    activity_age = None
    if snapshot is not None:
        advance = max(0.0, now_monotonic - snapshot.received_monotonic)
        request_elapsed = (snapshot.request_elapsed_seconds + advance if snapshot.request_elapsed_seconds is not None else None)
        activity_age = (snapshot.last_activity_age_seconds + advance if snapshot.last_activity_age_seconds is not None else None)
    operation, label = _operation(view, outstanding, snapshot, now_monotonic)
    candidate = view.patch_attempts[-1].attempt_index if view.patch_attempts else None
    if operation is OperationKind.CANDIDATE and view.current_tool_name == "apply_patch":
        # In-flight apply: ATTEMPT must agree with NOW.  The attempt being
        # applied is the one-based successor of the last recorded outcome.
        candidate = (
            view.patch_attempts[-1].attempt_index + 1
            if view.patch_attempts
            else 0
        )
    return LiveExecutionState(
        view=view, mode=mode, ceilings=ceilings, snapshot=snapshot,
        now_monotonic=now_monotonic, operation=operation, operation_label=label,
        request_index=request_index,
        controller_step=view.latest_controller_step_index,
        candidate_attempt=candidate, current_target=_target(view),
        elapsed_seconds=elapsed_seconds, request_elapsed_seconds=request_elapsed,
        last_activity_age_seconds=activity_age,
        recent_operations=view.timeline[-6:],
    )


__all__ = [
    "EphemeralSnapshot", "ExecutionMode", "KnownCeilings", "LiveExecutionState",
    "OperationKind", "project_live_execution",
]
