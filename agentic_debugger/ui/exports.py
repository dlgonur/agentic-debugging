"""Deterministic text exports for live, activity, and timeline views.

This module owns the plain-text export builders used by copy/export
actions: the live execution trace export, the activity (timeline)
export, and the timeline report export.  All are pure functions over
presentation state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from agentic_debugger.application.events import SessionStatus, SourceKind
from agentic_debugger.application.presentation import SessionViewState
from agentic_debugger.ui.render_helpers import (
    _ACTIVITY_FILTER_KINDS,
    _change_stats_text,
    _kind_badge,
    _kind_style,
)
from agentic_debugger.ui.timing import compute_session_timing, render_timeline_report
from agentic_debugger.application.workstream import WorkstreamStatus

def live_export_text(
    view: SessionViewState,
    *,
    task_title: Optional[str] = None,
) -> str:
    """Deterministic text export for the Live execution trace.

    Full logical operational trace in chronological order including
    timestamps, semantic unit badges, targets, details, durations, and
    diff preview lines.  Pure.
    """
    from agentic_debugger.ui.app import task_display_title

    title = task_title if task_title is not None else task_display_title(view.task_id)
    status_label = (
        "Completed"
        if view.status is SessionStatus.SUCCEEDED
        else view.status.value.replace("_", " ").capitalize()
    )
    try:
        from agentic_debugger.application.level32 import LEVEL32_TASK_ID, is_ladder_task, ladder_task_metadata
        if view.source_kind is SourceKind.LEVEL32_OPERATOR and view.model_provenance and view.model_provenance.treatment_revision is not None:
            treatment = f"V{view.model_provenance.treatment_revision}"
        elif is_ladder_task(view.task_id):
            if view.task_id == LEVEL32_TASK_ID and view.source_kind is not SourceKind.LEVEL32_OPERATOR:
                treatment = "Interactive Level-32 · non-official"
            else:
                treatment = ladder_task_metadata(view.task_id).treatment
        else:
            treatment = "—"
    except Exception:
        treatment = "—"

    lines = [
        title,
        f"Status: {status_label}",
        f"Treatment: {treatment}",
        f"View: Live",
        "",
    ]
    if not view.workstream:
        lines.append("No operational activity recorded.")
        return "\n".join(lines)

    start_dt = None
    for entry in view.timeline:
        if entry.timestamp_utc:
            try:
                start_dt = datetime.fromisoformat(entry.timestamp_utc.replace("Z", "+00:00"))
                break
            except Exception:
                pass

    for entry in view.workstream:
        time_str = "       "
        if entry.timestamp_utc and start_dt:
            try:
                entry_dt = datetime.fromisoformat(entry.timestamp_utc.replace("Z", "+00:00"))
                secs = max(0, int((entry_dt - start_dt).total_seconds()))
                time_str = f"{secs // 60:02d}:{secs % 60:02d}  "
            except Exception:
                pass

        badge = _kind_badge(entry)
        target_str = f"  {entry.target}" if entry.target else ""
        stats_str = f"  {_change_stats_text(entry.change)}" if entry.change is not None else ""
        detail_str = f"  · {entry.detail}" if entry.detail else ""
        dur_str = ""
        if entry.duration_seconds is not None:
            dur_str = f"  ({entry.duration_seconds:.1f}s)"
        elif entry.status is WorkstreamStatus.ACTIVE:
            dur_str = "  (running…)"

        lines.append(f"{time_str}{badge:<14} {entry.label}{target_str}{stats_str}{detail_str}{dur_str}")
        if entry.change is not None:
            change = entry.change
            if change.multi_file:
                for f_sum in change.files:
                    lines.append(f"                {f_sum.operation.value} {f_sum.path} +{f_sum.additions} -{f_sum.deletions}")
            for d_line in change.lines:
                prefix = "+" if d_line.kind is DiffLineKind.ADDED else ("-" if d_line.kind is DiffLineKind.REMOVED else " ")
                lineno = str(d_line.old_lineno or d_line.new_lineno or "")
                lines.append(f"                {lineno:>4} {prefix}{d_line.text}")

    return "\n".join(lines)


def activity_export_text(
    view: SessionViewState,
    *,
    filter_name: str = "all",
    task_title: Optional[str] = None,
) -> str:
    """Deterministic text export for the Activity ledger.

    Uses the same filter/order semantics as the current tab and the same
    safe presentation summaries.  The result is the complete logical
    Activity contents, not the visible viewport.  Pure; never touches
    clipboard, journal, or ephemeral liveness.
    """
    from agentic_debugger.ui.app import task_display_title

    title = task_title if task_title is not None else task_display_title(view.task_id)
    status_label = (
        "Completed"
        if view.status is SessionStatus.SUCCEEDED
        else view.status.value.replace("_", " ").capitalize()
    )
    # Treatment label: reuse the same derivation as the context panel.
    try:
        from agentic_debugger.application.level32 import LEVEL32_TASK_ID, is_ladder_task, ladder_task_metadata
        if view.source_kind is SourceKind.LEVEL32_OPERATOR and view.model_provenance and view.model_provenance.treatment_revision is not None:
            treatment = f"V{view.model_provenance.treatment_revision}"
        elif is_ladder_task(view.task_id):
            if view.task_id == LEVEL32_TASK_ID and view.source_kind is not SourceKind.LEVEL32_OPERATOR:
                treatment = "Interactive Level-32 · non-official"
            else:
                treatment = ladder_task_metadata(view.task_id).treatment
        else:
            treatment = "—"
    except Exception:
        treatment = "—"
    allowed = _ACTIVITY_FILTER_KINDS.get(filter_name, frozenset())
    entries = [
        entry
        for entry in view.timeline
        if not allowed or entry.event_kind.value in allowed
    ]
    lines = [
        title,
        f"Status: {status_label}",
        f"Treatment: {treatment}",
        f"View: Activity",
        f"Filter: {filter_name}",
        "",
    ]
    # Activity renders newest first (reversed), so export preserves that.
    for entry in reversed(entries):
        lines.append(f"#{entry.sequence} {entry.summary}")
    if not entries:
        lines.append("No activity recorded for this filter.")
    return "\n".join(lines)
