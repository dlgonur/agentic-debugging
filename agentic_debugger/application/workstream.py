"""Operational workstream projection and bounded change previews.

This module is a pure, Textual-free presentation projection over facts the
durable session-event path already owns.  It creates no evidence: every
work unit and every diff line is derived from a :class:`SessionEvent` (or a
patch body such an event already carries), so observer-only presentation
cannot alter controller, model, patch, PDB, verifier, or cleanup behavior.

Two layers live in this architecture:

- a bounded **change preview** (:mod:`agentic_debugger.application.change_preview`,
  :func:`build_change_preview`) that turns the authoritative candidate
  patch text (``patch.proposed`` → ``PatchAttemptView.patch_text``) into a
  small terminal-native diff projection; anything unparseable fails closed
  to ``None`` rather than a fabricated or misleading rendering;

- the **workstream** (:class:`WorkstreamEntry`, owned by
  :mod:`agentic_debugger.application.workstream_entries`) maintained
  incrementally by the shared reducer via :func:`apply_workstream_event`.
  Entries have semantic identity (tool identity, request ordinal, attempt
  ordinal, verifier unit), so a started operation settles its own entry
  instead of producing endless duplicate rows, and distinct operations are
  never merged.

Dependency rule: the workstream modules import nothing from the
application package (the reducer imports *these* types).  Event facts
arrive as plain arguments.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional, Tuple

from agentic_debugger.application.change_preview import (
    ChangeFileSummary,
    ChangePreview,
    ChangePreviewLimits,
    DEFAULT_PREVIEW_LIMITS,
    DiffLine,
    DiffLineKind,
    DiffPathKind,
    build_change_preview,
)
from agentic_debugger.application.workstream_entries import (
    MAX_WORKSTREAM_ENTRIES,
    WorkstreamEntry,
    WorkstreamKind,
    WorkstreamStatus,
    _active_model_request_detail,
    _append,
    _change_index,
    _coalesce_completed,
    _current_frame_target,
    _settle,
    _token_usage_detail,
    _tool_detail,
    _tool_unit,
    _update_change,
)

__all__ = [
    "ChangeFileSummary",
    "ChangePreview",
    "ChangePreviewLimits",
    "DEFAULT_PREVIEW_LIMITS",
    "DiffLine",
    "DiffLineKind",
    "DiffPathKind",
    "MAX_WORKSTREAM_ENTRIES",
    "WorkstreamEntry",
    "WorkstreamKind",
    "WorkstreamStatus",
    "apply_workstream_event",
    "build_change_preview",
]


def apply_workstream_event(
    entries: Tuple[WorkstreamEntry, ...],
    *,
    event_kind: str,
    payload: dict,
    sequence: int,
    in_flight_attempt_ordinal: int,
    debugger_target: Optional[str] = None,
    timestamp_utc: Optional[str] = None,
    duration_seconds: Optional[float] = None,
) -> Tuple[WorkstreamEntry, ...]:
    """Fold one durable session event into the curated workstream.

    Pure and total: unknown facts leave the workstream unchanged.
    ``in_flight_attempt_ordinal`` is the one-based ordinal of the candidate
    currently being applied (derived by the reducer from recorded attempts);
    ``debugger_target`` is the last recorded debugger location
    (``script:line``) used only to label PDB observation units.
    """
    folded = _fold_workstream_event(
        entries,
        event_kind=event_kind,
        payload=payload,
        sequence=sequence,
        in_flight_attempt_ordinal=in_flight_attempt_ordinal,
        debugger_target=debugger_target,
        timestamp_utc=timestamp_utc,
        duration_seconds=duration_seconds,
    )
    if folded is entries:
        return entries
    return folded


def _fold_workstream_event(
    entries: Tuple[WorkstreamEntry, ...],
    *,
    event_kind: str,
    payload: dict,
    sequence: int,
    in_flight_attempt_ordinal: int,
    debugger_target: Optional[str] = None,
    timestamp_utc: Optional[str] = None,
    duration_seconds: Optional[float] = None,
) -> Tuple[WorkstreamEntry, ...]:
    kind = event_kind

    if kind == "session.started":
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.SESSION,
                status=WorkstreamStatus.COMPLETED,
                label="Session started",
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "model.configured":
        display = payload.get("display_name") or payload.get("profile_id")
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.SESSION,
                status=WorkstreamStatus.COMPLETED,
                label="Model configured",
                detail=display,
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "model.request_started":
        entries = _settle(
            entries,
            kind=WorkstreamKind.WORKSPACE,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            timestamp_utc=timestamp_utc,
        )
        continuing_detail = None
        for prev in reversed(entries):
            if prev.kind is WorkstreamKind.CHANGE:
                if prev.status is WorkstreamStatus.FAILED:
                    continuing_detail = "Continuing after patch failure"
                break
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.MODEL_REQUEST,
                status=WorkstreamStatus.ACTIVE,
                label="Model request",
                sequence=sequence,
                ordinal=payload["request_index"] + 1,
                detail=continuing_detail,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "model.directive_accepted":
        action_name = payload.get("action_name")
        directive_kind = payload.get("directive_kind")
        target_state = payload.get("target_state")
        action_desc = None
        if action_name:
            _action_map = {
                "get_source_window": "Inspect source",
                "get_stack_summary": "Inspect stack",
                "get_frame": "Inspect frame",
                "get_frame_locals": "Inspect locals",
                "step_over": "Step over",
                "step_into": "Step into",
                "continue_execution": "Continue execution",
                "apply_patch": "Apply change",
                "run_reproduction": "Run reproduction",
                "run_tests": "Run tests",
                "run_regression_tests": "Run regression tests",
                "express_root_cause_hypothesis": "Formulate diagnosis",
            }
            action_desc = _action_map.get(action_name, action_name.replace("_", " ").capitalize())
        elif directive_kind == "transition" and target_state:
            action_desc = f"Transition to {target_state}"

        if action_desc:
            # Find the most recent active or recent MODEL_REQUEST entry and annotate detail
            for index in range(len(entries) - 1, -1, -1):
                entry = entries[index]
                if entry.kind is WorkstreamKind.MODEL_REQUEST:
                    updated_detail = f"{entry.detail} · {action_desc}" if entry.detail and "Continuing" in entry.detail else action_desc
                    updated_entry = replace(entry, detail=updated_detail)
                    return entries[:index] + (updated_entry,) + entries[index + 1 :]
        return entries

    if kind == "model.request_completed":
        ordinal = payload["request_index"] + 1
        failed = payload.get("status") in ("error", "timeout")
        error_detail: Optional[str] = None
        if failed:
            error_kind = payload.get("error_kind")
            error_message = payload.get("error_message")
            if error_kind and error_message and error_message != error_kind:
                error_detail = f"{error_kind} · {error_message}"
            elif error_kind:
                error_detail = error_kind
            elif error_message:
                error_detail = error_message
        # Provider token usage (when reported) joins the row detail after
        # the operational text: rejected directives and retried attempts
        # consumed real tokens too.
        usage_detail = _token_usage_detail(payload)
        detail = error_detail
        if usage_detail is not None:
            base = detail if detail is not None else _active_model_request_detail(entries, ordinal)
            detail = f"{base} · {usage_detail}" if base else usage_detail
        return _settle(
            entries,
            kind=WorkstreamKind.MODEL_REQUEST,
            status=WorkstreamStatus.FAILED if failed else WorkstreamStatus.COMPLETED,
            sequence=sequence,
            ordinal=ordinal,
            detail=detail,
            timestamp_utc=timestamp_utc,
            duration_seconds=duration_seconds,
        )

    if kind == "tool.started":
        tool_name = payload["tool_name"]
        target = payload.get("target")
        if tool_name == "apply_patch":
            # The same semantic change unit: an already-proposed attempt is
            # enriched to "applying", never duplicated.
            existing = _update_change(
                entries,
                in_flight_attempt_ordinal,
                sequence=sequence,
                detail="applying",
                active_only=True,
            )
            if existing is not entries:
                return existing
            return _append(
                entries,
                WorkstreamEntry(
                    kind=WorkstreamKind.CHANGE,
                    status=WorkstreamStatus.ACTIVE,
                    label="Change",
                    sequence=sequence,
                    ordinal=in_flight_attempt_ordinal,
                    detail="applying",
                    timestamp_utc=timestamp_utc,
                ),
            )
        unit_kind, unit_label = _tool_unit(tool_name)
        return _append(
            entries,
            WorkstreamEntry(
                kind=unit_kind,
                status=WorkstreamStatus.ACTIVE,
                label=unit_label,
                sequence=sequence,
                target=target,
                detail=_tool_detail(tool_name, unit_label),
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "tool.completed":
        tool_name = payload["tool_name"]
        target = payload.get("target")
        failed = payload.get("status", "ok") != "ok"
        unit_kind, unit_label = _tool_unit(tool_name)
        if unit_kind is WorkstreamKind.CHANGE:
            # apply_patch completion: the candidate lifecycle event owns the
            # change unit; nothing to settle here.
            return entries
        detail = _tool_detail(tool_name, unit_label)
        settled = _settle(
            entries,
            kind=unit_kind,
            status=WorkstreamStatus.FAILED if failed else WorkstreamStatus.COMPLETED,
            sequence=sequence,
            target=target,
            match_detail=detail,
            timestamp_utc=timestamp_utc,
            duration_seconds=duration_seconds,
        )
        if settled is not entries:
            return settled
        # A completed tool without a started twin (the structured operator
        # channel emits source inspections as completions with the range)
        # becomes its own completed unit.
        return _coalesce_completed(
            entries,
            WorkstreamEntry(
                kind=unit_kind,
                status=WorkstreamStatus.FAILED if failed else WorkstreamStatus.COMPLETED,
                label=unit_label,
                sequence=sequence,
                target=target,
                detail=detail,
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "debugger.started":
        script = payload.get("script")
        breakpoints = payload.get("breakpoints") or ()
        target = None
        if script and breakpoints:
            first = breakpoints[0]
            tail = first.split(":")[-1]
            if tail.isdigit():
                target = f"{script}:{tail}"
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.DEBUGGER,
                status=WorkstreamStatus.ACTIVE,
                label="Start debugger",
                sequence=sequence,
                target=target or script,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "debugger.location_changed":
        script = payload.get("script")
        line = payload.get("line")
        target = f"{script}:{line}" if script and line is not None else script
        return _settle(
            entries,
            kind=WorkstreamKind.DEBUGGER,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            label="Debugger",
            target=target,
            timestamp_utc=timestamp_utc,
            duration_seconds=duration_seconds,
        )

    if kind == "debugger.stack_observed":
        target = _current_frame_target(payload.get("frames")) or debugger_target
        return _coalesce_completed(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.PDB,
                status=WorkstreamStatus.COMPLETED,
                label="PDB observed",
                sequence=sequence,
                target=target,
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "debugger.locals_observed":
        return _coalesce_completed(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.PDB,
                status=WorkstreamStatus.COMPLETED,
                label="PDB locals",
                sequence=sequence,
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "operator.progress":
        return _apply_operator_progress(entries, payload, sequence, timestamp_utc=timestamp_utc)

    if kind == "verifier.started":
        existing = _settle(
            entries,
            kind=WorkstreamKind.VERIFICATION,
            status=WorkstreamStatus.ACTIVE,
            sequence=sequence,
            label="Verifier",
            detail="running",
            timestamp_utc=timestamp_utc,
        )
        if existing is not entries:
            return existing
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.VERIFICATION,
                status=WorkstreamStatus.ACTIVE,
                label="Verifier",
                sequence=sequence,
                detail="running",
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "verifier.stage_completed":
        stage = payload.get("stage")
        return _settle(
            entries,
            kind=WorkstreamKind.VERIFICATION,
            status=WorkstreamStatus.ACTIVE,
            sequence=sequence,
            label="Verifier",
            detail=stage.replace("_", " ") if isinstance(stage, str) else None,
            timestamp_utc=timestamp_utc,
            duration_seconds=duration_seconds,
        )

    if kind == "verifier.completed":
        outcome = payload.get("outcome")
        detail = outcome.lower() if isinstance(outcome, str) else "completed"
        settled = _settle(
            entries,
            kind=WorkstreamKind.VERIFICATION,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            label="Verifier",
            detail=detail,
            timestamp_utc=timestamp_utc,
            duration_seconds=duration_seconds,
        )
        if settled is not entries:
            return settled
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.VERIFICATION,
                status=WorkstreamStatus.COMPLETED,
                label="Verifier",
                sequence=sequence,
                detail=detail,
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "patch.proposed":
        ordinal = payload["attempt_index"] + 1
        patch_text = payload.get("patch_text")
        preview = build_change_preview(patch_text) if patch_text else None
        index = _change_index(entries, ordinal)
        if index is not None:
            # Enrich the same semantic unit when the patch body arrives --
            # including late, after the apply outcome was recorded.  The
            # settled status/label of the unit are never regressed, and an
            # unparseable body never erases an existing preview.
            entry = entries[index]
            fields: dict = {"sequence": sequence}
            if entry.status is WorkstreamStatus.ACTIVE:
                fields["detail"] = "proposed"
            if preview is not None:
                fields["change"] = preview
            if timestamp_utc is not None and entry.timestamp_utc is None:
                fields["timestamp_utc"] = timestamp_utc
            updated = replace(entry, **fields)
            return entries[:index] + (updated,) + entries[index + 1 :]
        # No cross-ordinal deduplication: distinct patch attempts remain
        # distinct even when patch bodies are identical.  Same-ordinal
        # late enrichment is handled above; different ordinals are never
        # collapsed based on SHA/body/preview equality.
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CHANGE,
                status=WorkstreamStatus.ACTIVE,
                label="Change",
                sequence=sequence,
                ordinal=ordinal,
                detail="proposed",
                change=preview,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "patch.applied":
        ordinal = payload["attempt_index"] + 1
        changed = payload.get("changed_files") or ()
        detail = f"+{len(changed) - 1} more" if len(changed) > 1 else None
        fields: dict = {
            "status": WorkstreamStatus.COMPLETED,
            "sequence": sequence,
            "label": "Applied change",
            "detail": detail,
        }
        if changed:
            fields["target"] = changed[0]
        if duration_seconds is not None:
            fields["duration_seconds"] = duration_seconds
        updated = _update_change(entries, ordinal, **fields)
        if updated is not entries:
            return updated
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CHANGE,
                status=WorkstreamStatus.COMPLETED,
                label="Applied change",
                sequence=sequence,
                ordinal=ordinal,
                target=changed[0] if changed else None,
                detail=detail,
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "patch.rejected":
        ordinal = payload["attempt_index"] + 1
        updated = _update_change(
            entries,
            ordinal,
            status=WorkstreamStatus.FAILED,
            sequence=sequence,
            label="Rejected change",
            detail=payload.get("rejection_reason"),
            duration_seconds=duration_seconds,
        )
        if updated is not entries:
            return updated
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CHANGE,
                status=WorkstreamStatus.FAILED,
                label="Rejected change",
                sequence=sequence,
                ordinal=ordinal,
                detail=payload.get("rejection_reason"),
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "patch.apply_failed":
        ordinal = payload["attempt_index"] + 1
        updated = _update_change(
            entries,
            ordinal,
            status=WorkstreamStatus.FAILED,
            sequence=sequence,
            label="Apply failed",
            detail=payload.get("apply_failure_reason"),
            duration_seconds=duration_seconds,
        )
        if updated is not entries:
            return updated
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CHANGE,
                status=WorkstreamStatus.FAILED,
                label="Apply failed",
                sequence=sequence,
                ordinal=ordinal,
                detail=payload.get("apply_failure_reason"),
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "patch.reverted":
        ordinal = payload["attempt_index"] + 1
        updated = _update_change(
            entries,
            ordinal,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            label="Change reverted",
            duration_seconds=duration_seconds,
        )
        if updated is not entries:
            return updated
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CHANGE,
                status=WorkstreamStatus.COMPLETED,
                label="Change reverted",
                sequence=sequence,
                ordinal=ordinal,
                timestamp_utc=timestamp_utc,
                duration_seconds=duration_seconds,
            ),
        )

    if kind == "diagnosis.recorded":
        return _coalesce_completed(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.DIAGNOSIS,
                status=WorkstreamStatus.COMPLETED,
                label="Diagnosis recorded",
                sequence=sequence,
                target=payload.get("file_path"),
                detail=payload.get("text"),
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "cleanup.started":
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CLEANUP,
                status=WorkstreamStatus.ACTIVE,
                label="Cleanup",
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "cleanup.completed":
        verified = payload.get("verified") is True
        return _settle(
            entries,
            kind=WorkstreamKind.CLEANUP,
            status=WorkstreamStatus.COMPLETED if verified else WorkstreamStatus.FAILED,
            sequence=sequence,
            label="Cleanup",
            detail="verified" if verified else "unverified",
            timestamp_utc=timestamp_utc,
            duration_seconds=duration_seconds,
        )

    if kind == "cleanup.not_required":
        # Explicit positive proof: no disposable resources were created.
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.CLEANUP,
                status=WorkstreamStatus.COMPLETED,
                label="Cleanup not required",
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind == "session.failed":
        reason = payload.get("termination_reason")
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.ERROR,
                status=WorkstreamStatus.FAILED,
                label="Session failed",
                sequence=sequence,
                detail=reason.replace("_", " ") if isinstance(reason, str) else None,
                timestamp_utc=timestamp_utc,
            ),
        )

    if kind in ("session.completed", "session.cancelled"):
        label = (
            "Session completed" if kind == "session.completed" else "Session cancelled"
        )
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.SESSION,
                status=WorkstreamStatus.COMPLETED,
                label=label,
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )

    return entries


def _apply_operator_progress(
    entries: Tuple[WorkstreamEntry, ...],
    payload: dict,
    sequence: int,
    timestamp_utc: Optional[str] = None,
) -> Tuple[WorkstreamEntry, ...]:
    stage = payload.get("stage")
    if stage == "preparing_workspace":
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.WORKSPACE,
                status=WorkstreamStatus.ACTIVE,
                label="Preparing workspace",
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )
    if stage in ("starting", "preflight"):
        return _coalesce_completed(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.WORKSPACE,
                status=WorkstreamStatus.COMPLETED,
                label="Preflight",
                sequence=sequence,
                timestamp_utc=timestamp_utc,
            ),
        )
    if stage == "model_running":
        return _settle(
            entries,
            kind=WorkstreamKind.WORKSPACE,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            timestamp_utc=timestamp_utc,
        )
    if stage == "verification":
        existing = _settle(
            entries,
            kind=WorkstreamKind.VERIFICATION,
            status=WorkstreamStatus.ACTIVE,
            sequence=sequence,
            label="Verifier",
            detail="running",
            timestamp_utc=timestamp_utc,
        )
        if existing is not entries:
            return existing
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.VERIFICATION,
                status=WorkstreamStatus.ACTIVE,
                label="Verifier",
                sequence=sequence,
                detail="running",
                timestamp_utc=timestamp_utc,
            ),
        )
    if stage == "official_verification_preparing":
        return _append(
            entries,
            WorkstreamEntry(
                kind=WorkstreamKind.OFFICIAL_VERIFICATION,
                status=WorkstreamStatus.ACTIVE,
                label="Official verification",
                sequence=sequence,
                detail="preparing",
                timestamp_utc=timestamp_utc,
            ),
        )
    if stage == "official_evaluator_started":
        return _settle(
            entries,
            kind=WorkstreamKind.OFFICIAL_VERIFICATION,
            status=WorkstreamStatus.ACTIVE,
            sequence=sequence,
            label="Official verification",
            detail="evaluator running",
            timestamp_utc=timestamp_utc,
        )
    if stage == "official_evaluator_completed":
        proven = payload.get("official_execution_proven") is True
        return _settle(
            entries,
            kind=WorkstreamKind.OFFICIAL_VERIFICATION,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            label="Official verification",
            detail="execution proven" if proven else "completed (unproven)",
            timestamp_utc=timestamp_utc,
        )
    if stage in ("cleanup", "finalizing"):
        return _settle(
            entries,
            kind=WorkstreamKind.VERIFICATION,
            status=WorkstreamStatus.COMPLETED,
            sequence=sequence,
            timestamp_utc=timestamp_utc,
        )
    return entries
