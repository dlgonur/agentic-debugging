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

Architecture (one-way dependency graph):

* :mod:`agentic_debugger.application.presentation_views` — immutable view
  records, ``SessionViewState``, and ``PresentationIdentity`` contracts;
* :mod:`agentic_debugger.application.presentation_tokens` — cumulative
  token-usage folding and completeness semantics;
* :mod:`agentic_debugger.application.presentation_summaries` — bounded
  event summary text;
* :mod:`agentic_debugger.application.presentation_reduction` — the pure
  core reducer and its merge helpers;
* this module — the public facade owning ``reduce_event`` (core fold plus
  workstream curation) and the public import surface.

A projection must never invent or upgrade evidence: everything above is
derived presentation state over durable events, never a second authority.
"""

from __future__ import annotations

from dataclasses import replace

from agentic_debugger.application.presentation_reduction import (
    _compute_timeline_duration,
    _debugger_location_target,
    _in_flight_attempt_ordinal,
    _operation_key,
    _reduce_event_core,
    active_candidate_attempt,
    current_source,
)
from agentic_debugger.application.presentation_summaries import (
    MAX_TIMELINE_SUMMARY_CHARS,
    _trim_summary,
    summarize_event,
)
from agentic_debugger.application.presentation_tokens import (
    SessionTokenUsage,
    _fold_request_usage,
)
from agentic_debugger.application.presentation_views import (
    MAX_TIMELINE_ENTRIES,
    DebuggerViewState,
    DiagnosisView,
    FrameRecord,
    LocalRecord,
    ModelProvenanceView,
    PatchAttemptView,
    PatchStage,
    PresentationIdentity,
    SessionStatus,
    SessionViewState,
    SourceView,
    TimelineEntry,
    VerifierStageView,
    VerifierSummaryView,
    _initial_debugger,
    _validate_task_id,
    initial_session_view,
    presentation_identity,
)
from agentic_debugger.application.workstream import (
    WorkstreamEntry,
    apply_workstream_event,
)
from agentic_debugger.application.events import SessionEvent


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
