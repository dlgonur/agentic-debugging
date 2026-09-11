"""The pure core session-event reducer for the presentation projection.

``_reduce_event_core`` folds one validated application event into a new
immutable :class:`~agentic_debugger.application.presentation_views.SessionViewState`.
It performs no I/O, never mutates its inputs, and never touches
controller, PDB, patch, verifier, demo, or model state.  Live sessions and
replay cursors feed the same reducer, so live and replay presentation
cannot diverge.

This module owns the per-event reduction, the upsert/merge helpers for
patch attempts, verifier stages, and source snapshots, the paired
operation-correlation keys, and the timeline duration derivation.  The
public :func:`agentic_debugger.application.presentation.reduce_event` wrap
adds workstream curation on top of this core fold.

Dependency rule: imports only view contracts, summaries, token folding,
event vocabulary, and taxonomy enums.  No I/O, no authority over anything
but the presentation fold.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Mapping, Optional, Tuple

from agentic_debugger.application import ApplicationContractError
from agentic_debugger.application.event_contracts import (
    SessionEventKind,
    SessionPhase,
    SessionStatus,
    SessionTerminationReason,
    SourceSnapshotStage,
    VerifierStage,
    VerifierStageStatus,
    OperatorStage,
    can_transition,
)
from agentic_debugger.application.events import SessionEvent
from agentic_debugger.application.presentation_summaries import (
    _trim_summary,
    summarize_event,
)
from agentic_debugger.application.presentation_tokens import _fold_request_usage
from agentic_debugger.application.presentation_views import (
    MAX_TIMELINE_ENTRIES,
    DebuggerViewState,
    DiagnosisView,
    FrameRecord,
    LocalRecord,
    ModelProvenanceView,
    PatchAttemptView,
    PatchStage,
    SessionViewState,
    SourceView,
    TimelineEntry,
    VerifierStageView,
    VerifierSummaryView,
)
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
from agentic_debugger.evaluation.runner import EvaluationStatus


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
        error_kind = (
            payload.get("error_kind")
            if payload.get("status") != "ok"
            else None
        )
        error_message = (
            payload.get("error_message")
            if payload.get("status") != "ok"
            else None
        )
        return replace(
            state, latest_model_request_index=payload["request_index"],
            outstanding_model_request_index=outstanding,
            latest_model_error_kind=(
                error_kind if type(error_kind) is str else state.latest_model_error_kind
            ),
            latest_model_error_message=(
                error_message if type(error_message) is str else state.latest_model_error_message
            ),
            token_usage=_fold_request_usage(state.token_usage, payload),
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
