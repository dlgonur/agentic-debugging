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
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application import (
    ApplicationContractError,
    ApplicationInputError,
)
from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    OperatorStage,
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
from agentic_debugger.application.workstream import (
    WorkstreamEntry,
    apply_workstream_event,
)
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
from agentic_debugger.evaluation.runner import EvaluationStatus

#: Deterministic tail cap for the derived activity timeline.
MAX_TIMELINE_ENTRIES = 2000
MAX_TIMELINE_SUMMARY_CHARS = 240

def reduce_event(state: SessionViewState, event: SessionEvent) -> SessionViewState:
    """Public reducer: core presentation fold plus workstream curation.

    The workstream is derived with pre-event hints (in-flight attempt
    ordinal, last debugger location) computed from ``state`` before the
    core fold, then folded from the same validated event.  It is additive
    presentation state: the core fields and timeline are identical with or
    without it, so live/replay parity and scientific facts are unchanged.
    """
    in_flight_ordinal = _in_flight_attempt_ordinal(state)
    debugger_target = _debugger_location_target(state)
    op_key = _operation_key(event.event_kind, event.payload)
    duration_seconds = _compute_timeline_duration(state, event, op_key)
    next_state = _reduce_event_core(state, event)
    workstream = apply_workstream_event(
        next_state.workstream,
        event_kind=event.event_kind.value,
        payload=dict(event.payload),
        sequence=event.sequence,
        in_flight_attempt_ordinal=in_flight_ordinal,
        debugger_target=debugger_target,
        timestamp_utc=event.timestamp_utc,
        duration_seconds=duration_seconds,
    )
    # Terminal truth: a terminal session must not retain a stale
    # "applying" workstream entry.  Candidate proposal != application,
    # and official semantic rejection must not retroactively label a
    # patch as "apply failed".  Settle any remaining ACTIVE change
    # units at terminal without inventing a new ordinal.
    if next_state.status.terminal:
        from agentic_debugger.application.workstream import WorkstreamStatus as _WStatus
        from agentic_debugger.application.workstream import WorkstreamKind as _WKind
        # Max authoritative patch ordinal from durable patch attempts.
        max_patch_ordinal = 0
        for attempt in next_state.patch_attempts:
            ordinal = attempt.attempt_index + 1
            if ordinal > max_patch_ordinal:
                max_patch_ordinal = ordinal
        # Terminal settlement: no ACTIVE change may remain.  A proposal is
        # not an application, and official rejection must not become
        # "apply failed".  Also, a fabricated ordinal beyond the
        # authoritative patch attempts (e.g., final-candidate snapshot
        # mis-attributed as a new attempt) must not be retained.
        new_entries: list = []
        settled = False
        for entry in workstream:
            if entry.kind is _WKind.CHANGE and entry.status is _WStatus.ACTIVE:
                settled = True
                # Fabricated ordinal beyond authoritative attempts: drop it.
                # The canonical candidate body, if any, was already deduplicated
                # onto its provenance attempt via preview/sha equality.
                if entry.ordinal is not None and entry.ordinal > max_patch_ordinal:
                    continue
                label = "Final candidate" if entry.change is not None else "Change"
                new_entries.append(
                    replace(entry, status=_WStatus.COMPLETED, label=label, detail=None)
                )
            else:
                new_entries.append(entry)
        if settled or len(new_entries) != len(workstream):
            workstream = tuple(new_entries)
    if workstream is not next_state.workstream:
        next_state = replace(next_state, workstream=workstream)
    return next_state



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
    "WorkstreamEntry",
    "active_candidate_attempt",
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
    classification: Optional[str] = None
    official_test_execution_proven: Optional[bool] = None


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
    """Recorded diagnosis presentation copy (never chain-of-thought).

    ``observed_values`` is the bounded structured mapping the durable
    ``diagnosis.recorded`` event carried (for example the Local Project
    session's recorded repository basename and source HEAD).  It is copied
    verbatim from the event payload, never inferred.
    """

    text: Optional[str] = None
    file_path: Optional[str] = None
    symbol: Optional[str] = None
    confidence: Optional[str] = None
    observed_values: Optional[Mapping[str, Any]] = None
    evidence_refs: Tuple[str, ...] = ()
    proof_contract: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class TimelineEntry:
    """One derived activity timeline entry."""

    sequence: int
    event_kind: SessionEventKind
    summary: str
    timestamp_utc: Optional[str] = None
    duration_seconds: Optional[float] = None
    operation_key: Optional[str] = None


@dataclass(frozen=True)
class ModelProvenanceView:
    """Safe recorded provenance of a configured command-model session.

    Carries only the safe fields recorded by ``model.configured``: the
    selected profile id, the safe configuration fingerprint, the display
    label, and protocol/tool version metadata.  It never carries the
    executable, argv, or environment values, and it never claims a provider
    or model identity merely because a command was named that way.
    """

    profile_id: Optional[str] = None
    config_fingerprint: Optional[str] = None
    display_name: Optional[str] = None
    protocol_version: Optional[str] = None
    tool_version: Optional[str] = None
    treatment_revision: Optional[int] = None
    treatment_id: Optional[str] = None
    result_location: Optional[str] = None


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
    #: Explicit positive proof that cleanup was not required because no
    #: disposable runtime resources were ever created. Presence means
    #: "Not required"; absence means unknown unless proven. Never inferred
    #: from missing timeline entries.
    cleanup_not_required: bool = False
    model_provenance: Optional[ModelProvenanceView] = None
    operator_stage: Optional[OperatorStage] = None
    #: Typed operational facts retained by the reducer so live widgets never
    #: parse display summaries.  They are derived exclusively from v1 events.
    latest_model_request_index: Optional[int] = None
    outstanding_model_request_index: Optional[int] = None
    latest_controller_step_index: Optional[int] = None
    current_tool_name: Optional[str] = None
    #: Last structured target a tool event carried (e.g. a source range).
    #: Cleared only when a later tool event carries a different/absent
    #: target; it never claims more than the producing boundary recorded.
    current_tool_target: Optional[str] = None
    pdb_observed: bool = False
    #: Typed official-verifier milestone: True only after the operator
    #: observed real official test execution (never inferred from stage).
    official_execution_proven: Optional[bool] = None
    timeline: Tuple[TimelineEntry, ...] = ()
    #: Curated operational workstream (semantic work units, bounded).  The
    #: complete forensic ledger remains ``timeline``; this is presentation
    #: curation over the same durable facts, never a second authority.
    workstream: Tuple[WorkstreamEntry, ...] = ()


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
        phase = str(payload.get("phase", "")).replace("_", " ")
        return f"Session running ({phase})"
    if kind is SessionEventKind.SESSION_CANCEL_REQUESTED:
        return "Cancel requested"
    if kind in (
        SessionEventKind.SESSION_COMPLETED,
        SessionEventKind.SESSION_FAILED,
        SessionEventKind.SESSION_CANCELLED,
    ):
        reason = str(payload.get("termination_reason", "")).replace("_", " ")
        return f"session {payload['status']} ({reason})"
    if kind is SessionEventKind.CONTROLLER_STEP:
        step_num = payload["step_index"] + 1
        directive = payload.get("directive_kind") or payload.get("stop_reason") or "step"
        directive_map = {
            "add_hypothesis": "hypothesis added",
            "read_source": "source read",
            "run_debugger": "PDB inspection",
            "apply_patch": "patch candidate",
            "verify": "verification requested",
            "stop": "controller stopped",
        }
        detail = directive_map.get(directive, str(directive).replace("_", " "))
        return f"Controller step {step_num} ({detail})"
    if kind is SessionEventKind.CONTROLLER_TRANSITION:
        src = str(payload.get("source_state", "")).replace("_", " ")
        tgt = str(payload.get("target_state", "")).replace("_", " ")
        return f"controller transition: {src} -> {tgt}"
    if kind is SessionEventKind.MODEL_REQUEST_STARTED:
        return f"Model request {payload['request_index'] + 1} started"
    if kind is SessionEventKind.MODEL_REQUEST_COMPLETED:
        if payload["status"] != "ok" and payload.get("error_kind"):
            return (
                f"model request {payload['request_index'] + 1} failed — "
                f"{payload['error_kind']}: {payload['error_message']}"
            )
        return f"Model request {payload['request_index'] + 1} completed"
    if kind is SessionEventKind.MODEL_DIRECTIVE_ACCEPTED:
        detail = payload.get("action_name") or payload.get("target_state") or "directive"
        return f"Directive accepted ({str(detail).replace('_', ' ')})"
    if kind is SessionEventKind.MODEL_DIRECTIVE_REJECTED:
        return f"Directive rejected ({payload['rejection_category']})"
    if kind is SessionEventKind.MODEL_CONFIGURED:
        return f"Model configured ({payload['profile_id']})"
    if kind is SessionEventKind.OPERATOR_PROGRESS:
        stage = payload.get("stage", "")
        stage_labels = {
            "starting": "Session starting",
            "preflight": "Preflight complete",
            "preparing_workspace": "Workspace prepared",
            "model_running": "Model request in progress",
            "debugger": "Debugger inspection",
            "candidate": "Candidate patch received",
            "verification": "Running verifier",
            "official_verification": "Running official verifier",
            "official_verification_preparing": "Preparing verification",
            "official_evaluator_started": "Official evaluator started",
            "official_evaluator_completed": "Official evaluator completed",
            "finalizing": "Finalizing results",
            "cleanup": "Cleaning workspace",
            "completed": "Session complete",
        }
        label = stage_labels.get(stage, f"Stage: {str(stage).replace('_', ' ')}")
        detail = payload.get("detail")
        return f"{label}: {detail}" if detail else label
    if kind is SessionEventKind.TOOL_STARTED:
        tool_map = {
            "read_source": "Source read",
            "set_breakpoint": "Set breakpoint",
            "run_to_breakpoint": "Run to breakpoint",
            "step_over": "Step over",
            "step_into": "Step into",
            "step": "Step",
            "get_stack_summary": "Inspect stack",
            "get_locals": "Inspect locals",
            "apply_patch": "Apply patch",
            "revert_patch": "Revert patch",
            "run_repro": "Run reproduction",
            "run_tests": "Run tests",
        }
        tool_name = payload["tool_name"]
        action = tool_map.get(tool_name, f"Tool {tool_name}")
        target = payload.get("target")
        return f"{action} started ({target})" if target else f"{action} started"
    if kind is SessionEventKind.TOOL_COMPLETED:
        tool_map = {
            "read_source": "Source read",
            "set_breakpoint": "Breakpoint set",
            "run_to_breakpoint": "Run to breakpoint",
            "step_over": "Step complete",
            "step_into": "Step into complete",
            "step": "Step complete",
            "get_stack_summary": "Stack inspected",
            "get_locals": "Locals inspected",
            "apply_patch": "Patch applied",
            "revert_patch": "Patch reverted",
            "run_repro": "Reproduction complete",
            "run_tests": "Tests complete",
        }
        tool_name = payload["tool_name"]
        action = tool_map.get(tool_name, f"Tool {tool_name}")
        target = payload.get("target")
        status = payload.get("status", "ok")
        if target:
            return f"{action} ({target}) ({status})" if status != "ok" else f"{action} ({target})"
        return f"{action} ({status})" if status != "ok" else f"{action}"
    if kind is SessionEventKind.DEBUGGER_STARTED:
        return "Debugger started"
    if kind is SessionEventKind.DEBUGGER_LOCATION_CHANGED:
        fn = payload.get("function")
        script = payload.get("script")
        line = payload.get("line")
        loc = f"{script}:{line}" if script and line is not None else script or f"line {line}"
        if fn:
            return f"Paused at {fn} ({loc})"
        return f"Paused at {loc}"
    if kind is SessionEventKind.DEBUGGER_STACK_OBSERVED:
        return f"Stack observed ({len(payload['frames'])} frames)"
    if kind is SessionEventKind.DEBUGGER_LOCALS_OBSERVED:
        return f"Locals observed ({len(payload['locals'])} values)"
    if kind is SessionEventKind.PATCH_PROPOSED:
        return f"patch attempt {payload['attempt_index'] + 1} proposed"
    if kind is SessionEventKind.PATCH_REJECTED:
        return f"Patch attempt {payload['attempt_index'] + 1} rejected"
    if kind is SessionEventKind.PATCH_APPLY_FAILED:
        return f"Patch attempt {payload['attempt_index'] + 1} apply failed"
    if kind is SessionEventKind.PATCH_APPLIED:
        return f"Patch attempt {payload['attempt_index'] + 1} applied"
    if kind is SessionEventKind.PATCH_REVERTED:
        return f"Patch attempt {payload['attempt_index'] + 1} reverted"
    if kind is SessionEventKind.SOURCE_SNAPSHOT:
        return f"Source snapshot: {payload['path']} ({payload['line_count']} lines)"
    if kind is SessionEventKind.DIAGNOSIS_RECORDED:
        detail = payload.get("text")
        return f"Diagnosis: {_trim_summary(detail)}" if detail else "Diagnosis recorded"
    if kind is SessionEventKind.VERIFIER_STARTED:
        return "Verifier started"
    if kind is SessionEventKind.VERIFIER_STAGE_STARTED:
        stage = str(payload.get("stage", "")).replace("_", " ")
        return f"Verifier stage started: {stage}"
    if kind is SessionEventKind.VERIFIER_STAGE_COMPLETED:
        stage = str(payload.get("stage", "")).replace("_", " ")
        return f"Verifier stage: {stage} ({payload['status']})"
    if kind is SessionEventKind.VERIFIER_COMPLETED:
        outcome = payload.get("outcome") or "no outcome"
        return f"verifier completed ({outcome})"
    if kind is SessionEventKind.CLEANUP_STARTED:
        return "Cleanup started"
    if kind is SessionEventKind.CLEANUP_COMPLETED:
        verified = "verified" if payload["verified"] else "unverified"
        return f"Cleanup completed ({verified})"
    if kind is SessionEventKind.CLEANUP_NOT_REQUIRED:
        return "Cleanup not required"
    if kind is SessionEventKind.ARTIFACT_WRITTEN:
        return f"Artifact written: {payload['path']}"
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


#: Lifecycle rank of one patch-attempt stage.  A ``PROPOSED`` record that
#: arrives *after* a later stage (the Level-32 patch body is finalized only
#: after the live apply outcome was streamed) enriches the attempt's patch
#: text without regressing the authoritative outcome.
_PATCH_STAGE_RANK = {
    PatchStage.PROPOSED: 0,
    PatchStage.REJECTED: 1,
    PatchStage.APPLY_FAILED: 1,
    PatchStage.APPLIED: 2,
    PatchStage.REVERTED: 3,
    PatchStage.VERIFIED: 4,
}


def _upsert_patch_attempt(
    attempts: Tuple[PatchAttemptView, ...], attempt: PatchAttemptView
) -> Tuple[PatchAttemptView, ...]:
    for index, existing in enumerate(attempts):
        if existing.attempt_index == attempt.attempt_index:
            # One attempt accumulates fields across its lifecycle events
            # (proposed carries the hash and patch text; applied carries
            # files/syntax; later stages carry a failure/revert reason).
            # A late PROPOSED never regresses a recorded outcome.
            if (
                attempt.stage is PatchStage.PROPOSED
                and _PATCH_STAGE_RANK[existing.stage] > _PATCH_STAGE_RANK[attempt.stage]
            ):
                # Late proposal: keep the recorded outcome and its fields,
                # enriching only the patch hash/body it carries.
                attempt = replace(
                    attempt,
                    stage=existing.stage,
                    changed_files=(
                        attempt.changed_files
                        if attempt.changed_files
                        else existing.changed_files
                    ),
                    syntax_passed=(
                        attempt.syntax_passed
                        if attempt.syntax_passed is not None
                        else existing.syntax_passed
                    ),
                    rejection_reason=(
                        attempt.rejection_reason
                        if attempt.rejection_reason is not None
                        else existing.rejection_reason
                    ),
                    apply_failure_reason=(
                        attempt.apply_failure_reason
                        if attempt.apply_failure_reason is not None
                        else existing.apply_failure_reason
                    ),
                )
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


def _in_flight_attempt_ordinal(state: SessionViewState) -> int:
    """One-based ordinal of the candidate currently being applied.

    Matches the live-operation projection: with no recorded attempt the
    in-flight attempt is 1; while the latest attempt is still PROPOSED it is
    that attempt itself; after a settled outcome it is the successor.
    """
    attempts = state.patch_attempts
    if not attempts:
        return 1
    last = attempts[-1]
    if last.stage is PatchStage.PROPOSED:
        return last.attempt_index + 1
    return last.attempt_index + 2


def _debugger_location_target(state: SessionViewState) -> Optional[str]:
    debugger = state.debugger
    if debugger.script and debugger.line is not None:
        return f"{debugger.script}:{debugger.line}"
    return debugger.script


def active_candidate_attempt(state: SessionViewState) -> Optional["PatchAttemptView"]:
    """The authoritative active candidate attempt (session-ledger semantics).

    Mirrors the accepted SESSION-LEDGER provenance contract: an attempt
    becomes the active candidate only when it reaches ``APPLIED`` (or the
    verifier-upgraded ``VERIFIED``); a ``REVERTED`` recorded for that attempt
    clears it; a later successful apply replaces it.  ``PROPOSED``,
    ``REJECTED``, and ``APPLY_FAILED`` attempts never become the active
    candidate — a later failed attempt must not replace an earlier applied
    one.  Returns ``None`` when no candidate is active.
    """
    active: Optional[PatchAttemptView] = None
    for attempt in sorted(state.patch_attempts, key=lambda item: item.attempt_index):
        if attempt.stage in (PatchStage.APPLIED, PatchStage.VERIFIED):
            active = attempt
        elif attempt.stage is PatchStage.REVERTED:
            if active is not None and attempt.attempt_index == active.attempt_index:
                active = None
    return active


def _operation_key(kind: SessionEventKind, payload: Mapping[str, Any]) -> Optional[str]:
    """Derive a safe identity key for correlating paired start/completion events."""
    if kind in (SessionEventKind.MODEL_REQUEST_STARTED, SessionEventKind.MODEL_REQUEST_COMPLETED):
        req_idx = payload.get("request_index")
        return f"model_request:{req_idx}" if req_idx is not None else "model_request"
    if kind in (SessionEventKind.TOOL_STARTED, SessionEventKind.TOOL_COMPLETED):
        tool_name = payload.get("tool_name", "")
        call_id = payload.get("tool_call_id")
        target = payload.get("target")
        if call_id:
            return f"tool:{tool_name}:{call_id}"
        if target:
            return f"tool:{tool_name}:{target}"
        return f"tool:{tool_name}" if tool_name else "tool"
    if kind in (SessionEventKind.VERIFIER_STAGE_STARTED, SessionEventKind.VERIFIER_STAGE_COMPLETED):
        stage = payload.get("stage")
        return f"verifier_stage:{stage}" if stage else "verifier_stage"
    if kind in (SessionEventKind.VERIFIER_STARTED, SessionEventKind.VERIFIER_COMPLETED):
        return "verifier"
    if kind in (SessionEventKind.CLEANUP_STARTED, SessionEventKind.CLEANUP_COMPLETED):
        return "cleanup"
    if kind in (
        SessionEventKind.SESSION_STARTED,
        SessionEventKind.SESSION_COMPLETED,
        SessionEventKind.SESSION_FAILED,
        SessionEventKind.SESSION_CANCELLED,
    ):
        return "session"
    return None


def _compute_timeline_duration(
    state: SessionViewState,
    event: SessionEvent,
    op_key: Optional[str] = None,
) -> Optional[float]:
    """Derive actual duration for paired operation start+completion events."""
    if not event.timestamp_utc:
        return None
    try:
        event_dt = datetime.fromisoformat(event.timestamp_utc.replace("Z", "+00:00"))
    except Exception:
        return None

    target_start_kind: Optional[SessionEventKind] = None
    kind = event.event_kind
    if kind is SessionEventKind.MODEL_REQUEST_COMPLETED:
        target_start_kind = SessionEventKind.MODEL_REQUEST_STARTED
    elif kind is SessionEventKind.TOOL_COMPLETED:
        target_start_kind = SessionEventKind.TOOL_STARTED
    elif kind is SessionEventKind.VERIFIER_STAGE_COMPLETED:
        target_start_kind = SessionEventKind.VERIFIER_STAGE_STARTED
    elif kind is SessionEventKind.VERIFIER_COMPLETED:
        target_start_kind = SessionEventKind.VERIFIER_STARTED
    elif kind is SessionEventKind.CLEANUP_COMPLETED:
        target_start_kind = SessionEventKind.CLEANUP_STARTED
    elif kind in (
        SessionEventKind.SESSION_COMPLETED,
        SessionEventKind.SESSION_FAILED,
        SessionEventKind.SESSION_CANCELLED,
    ):
        target_start_kind = SessionEventKind.SESSION_STARTED

    if target_start_kind is None:
        return None

    for prev in reversed(state.timeline):
        if prev.event_kind is target_start_kind:
            # Identity-aware correlation: if keys are present, they must match
            if op_key is not None and prev.operation_key is not None:
                if prev.operation_key != op_key:
                    continue
            elif op_key is not None or prev.operation_key is not None:
                continue
            if prev.timestamp_utc:
                try:
                    start_dt = datetime.fromisoformat(prev.timestamp_utc.replace("Z", "+00:00"))
                    secs = (event_dt - start_dt).total_seconds()
                    return max(0.0, secs)
                except Exception:
                    return None
    return None


def _reduce_event_core(state: SessionViewState, event: SessionEvent) -> SessionViewState:
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
    op_key = _operation_key(kind, payload)
    duration_seconds = _compute_timeline_duration(state, event, op_key)
    entry = TimelineEntry(
        sequence=event.sequence,
        event_kind=kind,
        summary=_trim_summary(summarize_event(event)),
        timestamp_utc=event.timestamp_utc,
        duration_seconds=duration_seconds,
        operation_key=op_key,
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
            latest_controller_step_index=payload["step_index"],
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.MODEL_REQUEST_STARTED:
        return replace(
            state, latest_model_request_index=payload["request_index"],
            outstanding_model_request_index=payload["request_index"],
            controller_phase=controller_phase, run_id=run_id, timeline=timeline,
        )

    if kind is SessionEventKind.MODEL_REQUEST_COMPLETED:
        outstanding = state.outstanding_model_request_index
        if outstanding == payload["request_index"]:
            outstanding = None
        return replace(
            state, latest_model_request_index=payload["request_index"],
            outstanding_model_request_index=outstanding,
            controller_phase=controller_phase, run_id=run_id, timeline=timeline,
        )

    if kind is SessionEventKind.TOOL_STARTED:
        return replace(
            state, current_tool_name=payload["tool_name"],
            current_tool_target=payload.get("target"),
            controller_phase=controller_phase, run_id=run_id, timeline=timeline,
        )

    if kind is SessionEventKind.TOOL_COMPLETED:
        return replace(
            state, current_tool_name=None,
            current_tool_target=(
                payload["target"]
                if payload.get("target") is not None
                else state.current_tool_target
            ),
            controller_phase=controller_phase, run_id=run_id, timeline=timeline,
        )

    if kind in (
        SessionEventKind.CONTROLLER_TRANSITION,
        SessionEventKind.MODEL_DIRECTIVE_ACCEPTED,
        SessionEventKind.MODEL_DIRECTIVE_REJECTED,
        SessionEventKind.ARTIFACT_WRITTEN,
        SessionEventKind.VERIFIER_STARTED,
    ):
        return replace(
            state,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    if kind is SessionEventKind.MODEL_CONFIGURED:
        return replace(
            state,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
            model_provenance=ModelProvenanceView(
                profile_id=payload.get("profile_id"),
                config_fingerprint=payload.get("config_fingerprint"),
                display_name=payload.get("display_name"),
                protocol_version=payload.get("protocol_version"),
                tool_version=payload.get("tool_version"),
                treatment_revision=payload.get("treatment_revision"),
                treatment_id=payload.get("treatment_id"),
                result_location=payload.get("result_location"),
            ),
        )

    if kind is SessionEventKind.OPERATOR_PROGRESS:
        proven = payload.get("official_execution_proven")
        return replace(
            state,
            operator_stage=OperatorStage(payload["stage"]),
            official_execution_proven=(
                state.official_execution_proven if proven is None else proven
            ),
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
            pdb_observed=True,
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
            pdb_observed=True,
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
        observed = payload.get("observed_values")
        proof_contract = payload.get("proof_contract")
        # Live events carry a frozen Mapping; replayed JSON carries a dict.
        # Accept both (the durable payload contract is a JSON mapping).
        if not isinstance(observed, Mapping):
            observed = None
        if not isinstance(proof_contract, Mapping):
            proof_contract = None
        return replace(
            state,
            diagnosis=DiagnosisView(
                text=payload.get("text"),
                file_path=payload.get("file_path"),
                symbol=payload.get("symbol"),
                confidence=payload.get("confidence"),
                observed_values=dict(observed) if observed is not None else None,
                evidence_refs=tuple(payload.get("evidence_refs", ())),
                proof_contract=(
                    dict(proof_contract) if proof_contract is not None else None
                ),
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
            classification=payload.get("classification"),
            official_test_execution_proven=payload.get("official_test_execution_proven"),
        )
        attempts = state.patch_attempts
        if payload["status"] == EvaluationStatus.COMPLETED.value:
            attempts = _mark_applied_verified(attempts)
        proven = payload.get("official_test_execution_proven")
        return replace(
            state,
            verifier_summary=summary,
            patch_attempts=attempts,
            official_execution_proven=(
                state.official_execution_proven if proven is None else proven
            ),
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

    if kind is SessionEventKind.CLEANUP_NOT_REQUIRED:
        return replace(
            state,
            cleanup_verified=None,
            cleanup_not_required=True,
            controller_phase=controller_phase,
            run_id=run_id,
            timeline=timeline,
        )

    raise ApplicationContractError(f"unsupported event kind: {kind.value!r}")
