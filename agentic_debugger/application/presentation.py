"""Immutable presentation state and the pure session-event reducer.

``SessionViewState`` is the immutable presentation model derived only from
application-owned :class:`SessionEvent` records.  ``reduce_event`` is a pure
function: it performs no I/O, never mutates its inputs, and never touches
controller, PDB, patch, verifier, demo, or model state.

Live sessions and replay cursors feed the *same* reducer, so live and replay
presentation cannot diverge.  Presentation is initialized with a
:class:`PresentationIdentity` (task, source kind, optional session id):
the live path derives it from a :class:`SessionSpec`
(:func:`presentation_identity`), and recorded/replay paths derive it from
the recorded material.  Recorded source kinds therefore have an explicit
supported initialization path while remaining unable to start a new live
execution session.  Once identity is bound, events whose ``task_id``,
``source_kind``, or ``session_id`` mismatch the view fail closed instead of
silently reducing into a wrong-provenance view.

UI-owned selection, scroll/filter and replay cursor state is deliberately
not part of this model (architecture §7.4).

Absent historical data is represented honestly: optional fields stay
``None``/empty rather than being reconstructed, matching the ``NOT
RECORDED`` display rule for recorded material.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional, Tuple

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application import (
    ApplicationContractError,
    ApplicationInputError,
)
from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    SessionPhase,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
    SourceSnapshotStage,
    VerifierStage,
    VerifierStageStatus,
    can_transition,
    validate_session_id,
)
from agentic_debugger.application.session import SessionSpec
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
from agentic_debugger.evaluation.runner import EvaluationStatus

#: Deterministic tail cap for the derived activity timeline.
MAX_TIMELINE_ENTRIES = 2000
MAX_TIMELINE_SUMMARY_CHARS = 240

__all__ = [
    "DebuggerViewState",
    "DiagnosisView",
    "FrameRecord",
    "LocalRecord",
    "MAX_TIMELINE_ENTRIES",
    "PatchAttemptView",
    "PatchStage",
    "PresentationIdentity",
    "SessionViewState",
    "SourceView",
    "TimelineEntry",
    "VerifierStageView",
    "VerifierSummaryView",
    "current_source",
    "initial_session_view",
    "presentation_identity",
    "reduce_event",
    "summarize_event",
]


@dataclass(frozen=True)
class FrameRecord:
    """One bounded presentation copy of a recorded stack frame."""

    index: int
    function: str
    file: str
    line: int
    is_current: bool


@dataclass(frozen=True)
class LocalRecord:
    """One bounded presentation copy of a recorded local summary."""

    name: str
    summary: str


@dataclass(frozen=True)
class DebuggerViewState:
    """Recorded debugger presentation data (never live PDB state).

    ``pause_generation`` is nullable (Repair Pass 3): historical traces that
    never recorded a generation stay ``None`` (``NOT RECORDED``) instead of
    receiving a synthesized counter.
    """

    script: Optional[str] = None
    line: Optional[int] = None
    function: Optional[str] = None
    pause_generation: Optional[int] = None
    frames: Tuple[FrameRecord, ...] = ()
    locals: Tuple[LocalRecord, ...] = ()
    breakpoints: Tuple[str, ...] = ()
    session_started: bool = False


class PatchStage(str, Enum):
    """Presentation stage of one normalized patch attempt.

    ``VERIFIED`` means the independent verifier completed and the applied
    candidate was part of that completed evaluation; it never means the
    repair is correct.  ``APPLY_FAILED`` is the real PatchManager apply
    failure path (distinct from a validation/authorization ``REJECTED``).
    """

    PROPOSED = "proposed"
    REJECTED = "rejected"
    APPLY_FAILED = "apply_failed"
    APPLIED = "applied"
    REVERTED = "reverted"
    VERIFIED = "verified"


@dataclass(frozen=True)
class PatchAttemptView:
    """One normalized patch attempt in presentation order."""

    attempt_index: int
    stage: PatchStage
    patch_sha256: Optional[str] = None
    patch_text: Optional[str] = None
    changed_files: Tuple[str, ...] = ()
    syntax_passed: Optional[bool] = None
    rejection_reason: Optional[str] = None
    apply_failure_reason: Optional[str] = None


@dataclass(frozen=True)
class VerifierStageView:
    """Informational progress of one verifier stage."""

    stage: VerifierStage
    status: VerifierStageStatus


@dataclass(frozen=True)
class VerifierSummaryView:
    """Final verifier summary copied from the terminal verifier event."""

    status: Optional[str]
    outcome: Optional[SemanticOutcome]
    f2p_passed: Optional[int]
    f2p_total: Optional[int]
    p2p_passed: Optional[int]
    p2p_total: Optional[int]
    workspace_cleaned: Optional[bool]


@dataclass(frozen=True)
class SourceView:
    """One bounded app-owned source snapshot (initial/applied/reverted).

    ``text`` is the bounded captured content; ``truncated=True`` means the
    original file exceeded the capture bound and only a prefix is present.
    ``line_count`` counts the captured text.  The logical ``path`` is the
    repository/workspace-relative identity used by debugger location events.
    """

    path: str
    sha256: str
    text: str
    line_count: int
    truncated: bool
    stage: SourceSnapshotStage


@dataclass(frozen=True)
class DiagnosisView:
    """Recorded diagnosis presentation copy (never chain-of-thought)."""

    text: Optional[str] = None
    file_path: Optional[str] = None
    symbol: Optional[str] = None
    confidence: Optional[str] = None


@dataclass(frozen=True)
class TimelineEntry:
    """One derived activity timeline entry."""

    sequence: int
    event_kind: SessionEventKind
    summary: str


@dataclass(frozen=True)
class SessionViewState:
    """Immutable presentation state of one session or replay."""

    session_id: Optional[str] = None
    task_id: str = ""
    source_kind: SourceKind = SourceKind.OFFLINE_DEMO
    status: SessionStatus = SessionStatus.CREATED
    phase: Optional[SessionPhase] = None
    controller_phase: Optional[ControllerState] = None
    run_id: Optional[str] = None
    termination_reason: Optional[SessionTerminationReason] = None
    debugger: DebuggerViewState = DebuggerViewState()
    patch_attempts: Tuple[PatchAttemptView, ...] = ()
    verifier_stages: Tuple[VerifierStageView, ...] = ()
    verifier_summary: Optional[VerifierSummaryView] = None
    diagnosis: Optional[DiagnosisView] = None
    sources: Tuple[SourceView, ...] = ()
    cleanup_verified: Optional[bool] = None
    timeline: Tuple[TimelineEntry, ...] = ()


def _trim_summary(text: str) -> str:
    if len(text) <= MAX_TIMELINE_SUMMARY_CHARS:
        return text
    return text[: MAX_TIMELINE_SUMMARY_CHARS - 3] + "..."


def summarize_event(event: SessionEvent) -> str:
    """Return one bounded, human-readable activity summary for an event."""
    kind = event.event_kind
    payload = event.payload
    if kind is SessionEventKind.SESSION_CREATED:
        return "session created"
    if kind is SessionEventKind.SESSION_STARTED:
        return "session started"
    if kind is SessionEventKind.SESSION_STATUS_CHANGED:
        return f"session running ({payload['phase']})"
    if kind is SessionEventKind.SESSION_CANCEL_REQUESTED:
        return "cancel requested"
    if kind in (
        SessionEventKind.SESSION_COMPLETED,
        SessionEventKind.SESSION_FAILED,
        SessionEventKind.SESSION_CANCELLED,
    ):
        return (
            f"session {payload['status']} "
            f"({payload['termination_reason']})"
        )
    if kind is SessionEventKind.CONTROLLER_STEP:
        detail = payload.get("directive_kind") or payload.get("stop_reason") or "step"
        return f"controller step {payload['step_index']} ({detail})"
    if kind is SessionEventKind.CONTROLLER_TRANSITION:
        return (
            f"controller transition "
            f"({payload['source_state']} -> {payload['target_state']})"
        )
    if kind is SessionEventKind.MODEL_REQUEST_STARTED:
        return f"model request {payload['request_index']} started"
    if kind is SessionEventKind.MODEL_REQUEST_COMPLETED:
        return (
            f"model request {payload['request_index']} completed "
            f"({payload['status']})"
        )
    if kind is SessionEventKind.MODEL_DIRECTIVE_ACCEPTED:
        detail = payload.get("action_name") or payload.get("target_state") or "directive"
        return f"directive accepted ({detail})"
    if kind is SessionEventKind.MODEL_DIRECTIVE_REJECTED:
        return f"directive rejected ({payload['rejection_category']})"
    if kind is SessionEventKind.TOOL_STARTED:
        return f"tool {payload['tool_name']} started"
    if kind is SessionEventKind.TOOL_COMPLETED:
        return f"tool {payload['tool_name']} completed ({payload['status']})"
    if kind is SessionEventKind.DEBUGGER_STARTED:
        return "debugger started"
    if kind is SessionEventKind.DEBUGGER_LOCATION_CHANGED:
        location = payload.get("function") or payload.get("script") or "?"
        return f"debugger location ({location} line {payload.get('line')})"
    if kind is SessionEventKind.DEBUGGER_STACK_OBSERVED:
        return f"stack observed ({len(payload['frames'])} frames)"
    if kind is SessionEventKind.DEBUGGER_LOCALS_OBSERVED:
        return f"locals observed ({len(payload['locals'])} values)"
    if kind is SessionEventKind.PATCH_PROPOSED:
        return f"patch attempt {payload['attempt_index']} proposed"
    if kind is SessionEventKind.PATCH_REJECTED:
        return f"patch attempt {payload['attempt_index']} rejected"
    if kind is SessionEventKind.PATCH_APPLY_FAILED:
        return f"patch attempt {payload['attempt_index']} apply failed"
    if kind is SessionEventKind.PATCH_APPLIED:
        return f"patch attempt {payload['attempt_index']} applied"
    if kind is SessionEventKind.PATCH_REVERTED:
        return f"patch attempt {payload['attempt_index']} reverted"
    if kind is SessionEventKind.SOURCE_SNAPSHOT:
        return (
            f"source snapshot ({payload['path']}, {payload['stage']}, "
            f"{payload['line_count']} lines)"
        )
    if kind is SessionEventKind.DIAGNOSIS_RECORDED:
        return "diagnosis recorded"
    if kind is SessionEventKind.VERIFIER_STARTED:
        return "verifier started"
    if kind is SessionEventKind.VERIFIER_STAGE_STARTED:
        return f"verifier stage started: {payload['stage']}"
    if kind is SessionEventKind.VERIFIER_STAGE_COMPLETED:
        return f"verifier stage completed: {payload['stage']} ({payload['status']})"
    if kind is SessionEventKind.VERIFIER_COMPLETED:
        outcome = payload.get("outcome") or "no outcome"
        return f"verifier completed ({outcome})"
    if kind is SessionEventKind.CLEANUP_STARTED:
        return "cleanup started"
    if kind is SessionEventKind.CLEANUP_COMPLETED:
        verified = "verified" if payload["verified"] else "unverified"
        return f"cleanup completed ({verified})"
    if kind is SessionEventKind.ARTIFACT_WRITTEN:
        return f"artifact written: {payload['path']}"
    raise ApplicationContractError(f"unsupported event kind: {kind.value!r}")


def _initial_debugger() -> DebuggerViewState:
    return DebuggerViewState()


@dataclass(frozen=True)
class PresentationIdentity:
    """Immutable presentation identity of one live or recorded session.

    ``source_kind`` may be any live or recorded :class:`SourceKind`;
    recorded kinds initialize replay presentation without weakening the
    live-start rules of :class:`ExecutionSourceSpec`.  ``session_id`` is
    optional at initialization: when absent it is bound from the first
    reduced event (the ``session.created`` event of a valid stream).
    """

    task_id: str
    source_kind: SourceKind
    session_id: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.source_kind) is not SourceKind:
            raise ApplicationInputError("source_kind must be a SourceKind")
        _validate_task_id(self.task_id)
        if self.session_id is not None:
            try:
                validate_session_id(self.session_id)
            except Exception as exc:
                raise ApplicationInputError(
                    f"invalid session id: {self.session_id!r}"
                ) from exc


def _validate_task_id(value: str) -> None:
    if type(value) is not str or not value.strip():
        raise ApplicationInputError("task_id must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ApplicationInputError("task_id must be UTF-8 text")
    if len(encoded) > 256:
        raise ApplicationInputError("task_id exceeds the 256-byte bound")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ApplicationInputError("task_id contains control characters")


def presentation_identity(spec: SessionSpec) -> PresentationIdentity:
    """Presentation identity of a live session spec (offline or configured)."""
    if type(spec) is not SessionSpec:
        raise ApplicationInputError("spec must be a SessionSpec")
    return PresentationIdentity(task_id=spec.task_id, source_kind=spec.source.kind)


def initial_session_view(identity: PresentationIdentity) -> SessionViewState:
    """Return the initial presentation state for a presentation identity."""
    if type(identity) is not PresentationIdentity:
        raise ApplicationInputError("identity must be a PresentationIdentity")
    return SessionViewState(
        session_id=identity.session_id,
        task_id=identity.task_id,
        source_kind=identity.source_kind,
    )


def _append_timeline(
    timeline: Tuple[TimelineEntry, ...], entry: TimelineEntry
) -> Tuple[TimelineEntry, ...]:
    updated = timeline + (entry,)
    if len(updated) > MAX_TIMELINE_ENTRIES:
        updated = updated[len(updated) - MAX_TIMELINE_ENTRIES :]
    return updated


def _transition(state: SessionViewState, target: SessionStatus) -> SessionStatus:
    if not can_transition(state.status, target):
        raise ApplicationContractError(
            f"illegal session status transition: "
            f"{state.status.value} -> {target.value}"
        )
    return target


def _upsert_patch_attempt(
    attempts: Tuple[PatchAttemptView, ...], attempt: PatchAttemptView
) -> Tuple[PatchAttemptView, ...]:
    for index, existing in enumerate(attempts):
        if existing.attempt_index == attempt.attempt_index:
            # One attempt accumulates fields across its lifecycle events
            # (proposed carries the hash and patch text; applied carries
            # files/syntax; later stages carry a failure/revert reason).
            merged = replace(
                attempt,
                patch_sha256=(
                    attempt.patch_sha256
                    if attempt.patch_sha256 is not None
                    else existing.patch_sha256
                ),
                patch_text=(
                    attempt.patch_text
                    if attempt.patch_text is not None
                    else existing.patch_text
                ),
            )
            return attempts[:index] + (merged,) + attempts[index + 1 :]
    return attempts + (attempt,)


def _upsert_verifier_stage(
    stages: Tuple[VerifierStageView, ...], stage: VerifierStageView
) -> Tuple[VerifierStageView, ...]:
    for index, existing in enumerate(stages):
        if existing.stage is stage.stage:
            return stages[:index] + (stage,) + stages[index + 1 :]
    return stages + (stage,)


def _upsert_source(
    sources: Tuple[SourceView, ...], source: SourceView
) -> Tuple[SourceView, ...]:
    """Keep the latest snapshot per logical path (the current source state)."""
    for index, existing in enumerate(sources):
        if existing.path == source.path:
            return sources[:index] + (source,) + sources[index + 1 :]
    return sources + (source,)


def _mark_applied_verified(
    attempts: Tuple[PatchAttemptView, ...],
) -> Tuple[PatchAttemptView, ...]:
    applied = [item for item in attempts if item.stage is PatchStage.APPLIED]
    if not applied:
        return attempts
    latest = max(item.attempt_index for item in applied)
    return tuple(
        replace(item, stage=PatchStage.VERIFIED)
        if item.attempt_index == latest
        else item
        for item in attempts
    )


def _frames_from_payload(payload: dict) -> Tuple[FrameRecord, ...]:
    return tuple(
        FrameRecord(
            index=item["index"],
            function=item["function"],
            file=item["file"],
            line=item["line"],
            is_current=item["is_current"],
        )
        for item in payload["frames"]
    )


def _locals_from_payload(payload: dict) -> Tuple[LocalRecord, ...]:
    return tuple(
        LocalRecord(name=item["name"], summary=item["summary"])
        for item in payload["locals"]
    )


def _newer_or_unknown(generation: Optional[int], current: Optional[int]) -> bool:
    """Whether a recorded observation may update the debugger view.

    An observation without a recorded generation is applied in stream order
    (replay order is authoritative; live producers always record one).  With
    a recorded generation, the stale-data guard applies: it may not replace
    data for a newer pause.
    """
    if generation is None or current is None:
        return True
    return generation >= current


def current_source(state: SessionViewState) -> Optional[SourceView]:
    """The recorded source view matching the debugger's current location.

    When the debugger has a concrete current ``script``, only the recorded
    snapshot whose logical path equals that script may be returned; if none
    exists the result is ``None`` (``NOT RECORDED``) -- a mismatched file is
    never presented as the current source.  When the debugger has no
    location/script at all, the most recent recorded snapshot may still be
    returned.  Pure: never reads the filesystem.
    """
    if not state.sources:
        return None
    script = state.debugger.script
    if script is None:
        return state.sources[-1]
    for source in reversed(state.sources):
        if source.path == script:
            return source
    return None


def reduce_event(state: SessionViewState, event: SessionEvent) -> SessionViewState:
    """Reduce one validated event into a new immutable view state.

    Pure: no I/O, no mutation of ``state`` or ``event``.  Events whose
    ``task_id`` or ``source_kind`` mismatch the bound view identity fail
    closed, as do events with a different ``session_id`` once the view
    identity is bound (the view session id is bound from the identity or
    from the first reduced event).  Unknown event kinds and illegal
    lifecycle transitions fail closed.  Events are assumed
    schema-validated; malformed payload access still raises rather than
    corrupting presentation.
    """
    if event.task_id != state.task_id:
        raise ApplicationContractError(
            f"event task_id {event.task_id!r} does not match the view "
            f"task_id {state.task_id!r}"
        )
    if event.source_kind is not state.source_kind:
        raise ApplicationContractError(
            f"event source_kind {event.source_kind.value!r} does not match "
            f"the view source_kind {state.source_kind.value!r}"
        )
    if state.session_id is not None and event.session_id != state.session_id:
        raise ApplicationContractError(
            f"event session_id {event.session_id!r} does not match the view "
            f"session_id {state.session_id!r}"
        )
    if state.session_id is None:
        state = replace(state, session_id=event.session_id)

    kind = event.event_kind
    payload = event.payload
    entry = TimelineEntry(
        sequence=event.sequence, event_kind=kind, summary=_trim_summary(summarize_event(event))
    )
    timeline = _append_timeline(state.timeline, entry)
    controller_phase = event.controller_phase
    if controller_phase is None:
        controller_phase = state.controller_phase
    run_id = event.run_id if event.run_id is not None else state.run_id

    if kind is SessionEventKind.SESSION_CREATED:
        return replace(
            state,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.SESSION_STARTED:
        return replace(
            state,
            status=_transition(state, SessionStatus.STARTING),
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.SESSION_STATUS_CHANGED:
        return replace(
            state,
            status=_transition(state, SessionStatus.RUNNING),
            phase=SessionPhase(payload["phase"]),
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.SESSION_CANCEL_REQUESTED:
        return replace(
            state,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind in (
        SessionEventKind.SESSION_COMPLETED,
        SessionEventKind.SESSION_FAILED,
        SessionEventKind.SESSION_CANCELLED,
    ):
        terminal = SessionStatus(payload["status"])
        return replace(
            state,
            status=_transition(state, terminal),
            phase=None,
            termination_reason=SessionTerminationReason(payload["termination_reason"]),
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.CONTROLLER_STEP:
        return replace(
            state,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind in (
        SessionEventKind.CONTROLLER_TRANSITION,
        SessionEventKind.MODEL_REQUEST_STARTED,
        SessionEventKind.MODEL_REQUEST_COMPLETED,
        SessionEventKind.MODEL_DIRECTIVE_ACCEPTED,
        SessionEventKind.MODEL_DIRECTIVE_REJECTED,
        SessionEventKind.TOOL_STARTED,
        SessionEventKind.TOOL_COMPLETED,
        SessionEventKind.ARTIFACT_WRITTEN,
        SessionEventKind.VERIFIER_STARTED,
    ):
        return replace(
            state,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.DEBUGGER_STARTED:
        debugger = replace(
            state.debugger,
            script=payload.get("script"),
            breakpoints=tuple(payload["breakpoints"]),
            session_started=True,
        )
        return replace(
            state,
            debugger=debugger,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.DEBUGGER_LOCATION_CHANGED:
        debugger = replace(
            state.debugger,
            script=payload.get("script"),
            line=payload.get("line"),
            function=payload.get("function"),
            pause_generation=payload["pause_generation"],
        )
        return replace(
            state,
            debugger=debugger,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.DEBUGGER_STACK_OBSERVED:
        debugger = state.debugger
        # Stale observations for an older pause must never replace
        # information for the newer pause; an observation without a recorded
        # generation is applied in stream order (see ``_newer_or_unknown``).
        if _newer_or_unknown(
            payload["pause_generation"], debugger.pause_generation
        ):
            debugger = replace(
                debugger,
                pause_generation=payload["pause_generation"],
                frames=_frames_from_payload(payload),
            )
        return replace(
            state,
            debugger=debugger,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.DEBUGGER_LOCALS_OBSERVED:
        debugger = state.debugger
        if _newer_or_unknown(
            payload["pause_generation"], debugger.pause_generation
        ):
            debugger = replace(
                debugger,
                pause_generation=payload["pause_generation"],
                locals=_locals_from_payload(payload),
            )
        return replace(
            state,
            debugger=debugger,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.PATCH_PROPOSED:
        attempt = PatchAttemptView(
            attempt_index=payload["attempt_index"],
            stage=PatchStage.PROPOSED,
            patch_sha256=payload["patch_sha256"],
            patch_text=payload.get("patch_text"),
        )
        return replace(
            state,
            patch_attempts=_upsert_patch_attempt(state.patch_attempts, attempt),
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.PATCH_REJECTED:
        attempt = PatchAttemptView(
            attempt_index=payload["attempt_index"],
            stage=PatchStage.REJECTED,
            rejection_reason=payload["rejection_reason"],
        )
        return replace(
            state,
            patch_attempts=_upsert_patch_attempt(state.patch_attempts, attempt),
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.PATCH_APPLY_FAILED:
        attempt = PatchAttemptView(
            attempt_index=payload["attempt_index"],
            stage=PatchStage.APPLY_FAILED,
            apply_failure_reason=payload["apply_failure_reason"],
        )
        return replace(
            state,
            patch_attempts=_upsert_patch_attempt(state.patch_attempts, attempt),
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.SOURCE_SNAPSHOT:
        source = SourceView(
            path=payload["path"],
            sha256=payload["sha256"],
            text=payload["text"],
            line_count=payload["line_count"],
            truncated=payload["truncated"],
            stage=SourceSnapshotStage(payload["stage"]),
        )
        return replace(
            state,
            sources=_upsert_source(state.sources, source),
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.DIAGNOSIS_RECORDED:
        return replace(
            state,
            diagnosis=DiagnosisView(
                text=payload.get("text"),
                file_path=payload.get("file_path"),
                symbol=payload.get("symbol"),
                confidence=payload.get("confidence"),
            ),
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.PATCH_APPLIED:
        attempt = PatchAttemptView(
            attempt_index=payload["attempt_index"],
            stage=PatchStage.APPLIED,
            changed_files=tuple(payload["changed_files"]),
            syntax_passed=payload["syntax_passed"],
        )
        return replace(
            state,
            patch_attempts=_upsert_patch_attempt(state.patch_attempts, attempt),
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.PATCH_REVERTED:
        attempt = PatchAttemptView(
            attempt_index=payload["attempt_index"], stage=PatchStage.REVERTED
        )
        return replace(
            state,
            patch_attempts=_upsert_patch_attempt(state.patch_attempts, attempt),
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.VERIFIER_STAGE_STARTED:
        stage_view = VerifierStageView(
            stage=VerifierStage(payload["stage"]),
            status=VerifierStageStatus.RUNNING,
        )
        return replace(
            state,
            verifier_stages=_upsert_verifier_stage(state.verifier_stages, stage_view),
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.VERIFIER_STAGE_COMPLETED:
        stage_view = VerifierStageView(
            stage=VerifierStage(payload["stage"]),
            status=VerifierStageStatus(payload["status"]),
        )
        return replace(
            state,
            verifier_stages=_upsert_verifier_stage(state.verifier_stages, stage_view),
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.VERIFIER_COMPLETED:
        summary = VerifierSummaryView(
            status=payload["status"],
            outcome=(
                SemanticOutcome(payload["outcome"])
                if payload["outcome"] is not None
                else None
            ),
            f2p_passed=payload["f2p_passed"],
            f2p_total=payload["f2p_total"],
            p2p_passed=payload["p2p_passed"],
            p2p_total=payload["p2p_total"],
            workspace_cleaned=payload["workspace_cleaned"],
        )
        attempts = state.patch_attempts
        if payload["status"] == EvaluationStatus.COMPLETED.value:
            attempts = _mark_applied_verified(attempts)
        return replace(
            state,
            verifier_summary=summary,
            patch_attempts=attempts,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.CLEANUP_STARTED:
        return replace(
            state,
            cleanup_verified=False,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.CLEANUP_COMPLETED:
        return replace(
            state,
            cleanup_verified=payload["verified"],
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    raise ApplicationContractError(f"unsupported event kind: {kind.value!r}")
