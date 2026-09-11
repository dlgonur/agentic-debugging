"""Session timing computation and the timeline panel/report.

This module owns the timing aggregation (per-operation durations derived
from timeline duration/operation-key facts, category summaries, totals)
and the deterministic timeline text report plus the ``TimelinePanel``
that renders it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from rich.text import Text
from textual.widgets import Static

from agentic_debugger.application.events import (
    OperatorStage,
    SessionEventKind,
    SessionStatus,
    SourceKind,
)
from agentic_debugger.application.presentation import (
    SessionViewState,
    TimelineEntry,
)
from agentic_debugger.ui.render_helpers import (
    _entry_style,
    _markup_escape,
    _operator_stage_label,
)
from agentic_debugger.ui.theme import (
    DEBUGGER,
    ERROR,
    EVIDENCE,
    FAINT,
    FOREGROUND,
    LINE,
    MUTED,
    PRIMARY,
    SECONDARY,
    SUCCESS,
    TOOL,
    WARNING,
)
from textual.app import ComposeResult
from textual.containers import VerticalScroll

_START_TO_COMPLETION_KINDS: dict[SessionEventKind, SessionEventKind] = {
    SessionEventKind.MODEL_REQUEST_STARTED: SessionEventKind.MODEL_REQUEST_COMPLETED,
    SessionEventKind.TOOL_STARTED: SessionEventKind.TOOL_COMPLETED,
    SessionEventKind.VERIFIER_STAGE_STARTED: SessionEventKind.VERIFIER_STAGE_COMPLETED,
    SessionEventKind.VERIFIER_STARTED: SessionEventKind.VERIFIER_COMPLETED,
    SessionEventKind.CLEANUP_STARTED: SessionEventKind.CLEANUP_COMPLETED,
}


def _find_in_flight_sequences(view: SessionViewState) -> set[int]:
    """Find sequence numbers of operations that have started but not yet completed."""
    if view.status is not SessionStatus.RUNNING:
        return set()
    active_starts: dict[tuple[SessionEventKind, Optional[str]], list[int]] = {}
    for entry in view.timeline:
        if entry.event_kind in _START_TO_COMPLETION_KINDS:
            comp_kind = _START_TO_COMPLETION_KINDS[entry.event_kind]
            key = (comp_kind, entry.operation_key)
            active_starts.setdefault(key, []).append(entry.sequence)
        elif entry.event_kind in (
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            SessionEventKind.TOOL_COMPLETED,
            SessionEventKind.VERIFIER_STAGE_COMPLETED,
            SessionEventKind.VERIFIER_COMPLETED,
            SessionEventKind.CLEANUP_COMPLETED,
        ):
            key = (entry.event_kind, entry.operation_key)
            if key in active_starts and active_starts[key]:
                active_starts[key].pop(0)
    in_flight: set[int] = set()
    for seqs in active_starts.values():
        in_flight.update(seqs)
    return in_flight


@dataclass(frozen=True)
class TimingCategorySummary:
    name: str
    total_seconds: Optional[float]
    count: int
    percentage: Optional[float]
    detail: str


@dataclass(frozen=True)
class TimedOperation:
    sequence: int
    start_time_offset: Optional[float]
    duration_seconds: float
    category: str
    label: str
    in_flight: bool = False


@dataclass(frozen=True)
class SessionTiming:
    has_timestamps: bool
    total_elapsed_seconds: Optional[float]
    accounted_seconds: float
    categories: tuple[TimingCategorySummary, ...]
    timed_operations: tuple[TimedOperation, ...]


def compute_session_timing(view: SessionViewState) -> SessionTiming:
    """Aggregate session time consumption by category and timed operations.

    Categorizes non-overlapping operations (Model, Debugger/PDB, Tools,
    Verification, Patch, Cleanup) and derives wall-clock elapsed time and
    honest category accounting.  Fails closed when timestamps are missing.
    """
    if not view.timeline and not view.workstream:
        return SessionTiming(
            has_timestamps=False,
            total_elapsed_seconds=None,
            accounted_seconds=0.0,
            categories=(),
            timed_operations=(),
        )

    start_dt = None
    last_dt = None
    for entry in view.timeline:
        if entry.timestamp_utc:
            try:
                dt = datetime.fromisoformat(entry.timestamp_utc.replace("Z", "+00:00"))
                if start_dt is None:
                    start_dt = dt
                last_dt = dt
            except Exception:
                pass

    has_timestamps = (start_dt is not None and last_dt is not None)
    total_elapsed = max(0.0, (last_dt - start_dt).total_seconds()) if has_timestamps else None

    model_durations: list[float] = []
    tool_durations: list[float] = []
    debugger_durations: list[float] = []
    verifier_durations: list[float] = []
    patch_durations: list[float] = []
    cleanup_durations: list[float] = []
    timed_ops: list[TimedOperation] = []

    seq_dt: dict[int, datetime] = {}
    for entry in view.timeline:
        if entry.timestamp_utc:
            try:
                seq_dt[entry.sequence] = datetime.fromisoformat(entry.timestamp_utc.replace("Z", "+00:00"))
            except Exception:
                pass

    in_flight_seqs = _find_in_flight_sequences(view)
    for entry in view.timeline:
        kind = entry.event_kind
        dur = entry.duration_seconds
        offset = None
        if entry.sequence in seq_dt and start_dt:
            offset = max(0.0, (seq_dt[entry.sequence] - start_dt).total_seconds())

        cat_name = None
        if entry.sequence in in_flight_seqs:
            summary_lower = entry.summary.lower()
            if kind == SessionEventKind.MODEL_REQUEST_STARTED:
                cat_name = "Model requests"
            elif kind == SessionEventKind.TOOL_STARTED:
                if any(dbg in summary_lower for dbg in ("pdb", "stack", "frame", "safe_eval", "debugger")):
                    cat_name = "Debugger / PDB"
                else:
                    cat_name = "Tools"
            elif kind in (SessionEventKind.VERIFIER_STARTED, SessionEventKind.VERIFIER_STAGE_STARTED):
                cat_name = "Verification"
            elif kind == SessionEventKind.CLEANUP_STARTED:
                cat_name = "Cleanup"
            if cat_name is not None:
                timed_ops.append(
                    TimedOperation(
                        sequence=entry.sequence,
                        start_time_offset=offset,
                        duration_seconds=0.0,
                        category=cat_name,
                        label=entry.summary,
                        in_flight=True,
                    )
                )
        elif kind == SessionEventKind.MODEL_REQUEST_COMPLETED:
            cat_name = "Model requests"
            if dur is not None:
                model_durations.append(dur)
        elif kind == SessionEventKind.TOOL_COMPLETED:
            summary_lower = entry.summary.lower()
            if any(dbg in summary_lower for dbg in ("pdb", "stack", "frame", "safe_eval", "debugger")):
                cat_name = "Debugger / PDB"
                if dur is not None:
                    debugger_durations.append(dur)
            elif "apply_patch" in summary_lower:
                cat_name = "Patch lifecycle"
                if dur is not None:
                    patch_durations.append(dur)
            else:
                cat_name = "Tools"
                if dur is not None:
                    tool_durations.append(dur)
        elif kind in (
            SessionEventKind.DEBUGGER_STARTED,
            SessionEventKind.DEBUGGER_LOCATION_CHANGED,
            SessionEventKind.DEBUGGER_STACK_OBSERVED,
            SessionEventKind.DEBUGGER_LOCALS_OBSERVED,
        ):
            cat_name = "Debugger / PDB"
            if dur is not None:
                debugger_durations.append(dur)
        elif kind in (
            SessionEventKind.VERIFIER_STAGE_COMPLETED,
            SessionEventKind.VERIFIER_COMPLETED,
        ):
            cat_name = "Verification"
            if kind == SessionEventKind.VERIFIER_COMPLETED and verifier_durations:
                pass
            elif dur is not None:
                verifier_durations.append(dur)
        elif kind in (
            SessionEventKind.PATCH_APPLIED,
            SessionEventKind.PATCH_REJECTED,
            SessionEventKind.PATCH_APPLY_FAILED,
            SessionEventKind.PATCH_REVERTED,
        ):
            cat_name = "Patch lifecycle"
            if dur is not None:
                patch_durations.append(dur)
        elif kind == SessionEventKind.CLEANUP_COMPLETED:
            cat_name = "Cleanup"
            if dur is not None:
                cleanup_durations.append(dur)

        if dur is not None and cat_name is not None and entry.sequence not in in_flight_seqs:
            timed_ops.append(
                TimedOperation(
                    sequence=entry.sequence,
                    start_time_offset=offset,
                    duration_seconds=dur,
                    category=cat_name,
                    label=entry.summary,
                    in_flight=False,
                )
            )

    cats: list[TimingCategorySummary] = []

    # 1. Model requests
    model_ops = [e for e in view.timeline if e.event_kind in (SessionEventKind.MODEL_REQUEST_COMPLETED, SessionEventKind.MODEL_REQUEST_STARTED)]
    model_count = len(model_durations) or (len(model_ops) // 2 or len(model_ops))
    if model_durations:
        tot = sum(model_durations)
        pct = (tot / total_elapsed) * 100.0 if (total_elapsed and total_elapsed > 0) else None
        avg_str = f"avg {tot / len(model_durations):.1f}s"
        count_str = f"{len(model_durations)} request{'s' if len(model_durations) != 1 else ''}"
        cats.append(
            TimingCategorySummary(
                name="Model requests",
                total_seconds=tot,
                count=len(model_durations),
                percentage=pct,
                detail=f"{count_str} ({avg_str})",
            )
        )
    elif model_count > 0 or view.model_provenance is not None:
        count_str = f"{model_count} request{'s' if model_count != 1 else ''}" if model_count > 0 else "—"
        cats.append(
            TimingCategorySummary(
                name="Model requests",
                total_seconds=None,
                count=model_count,
                percentage=None,
                detail=f"{count_str} (unmeasured)" if count_str != "—" else "unmeasured",
            )
        )

    # 2. Debugger / PDB
    dbg_ops = [e for e in view.timeline if "debugger" in e.event_kind.value or "pdb" in e.summary.lower()]
    dbg_count = len(debugger_durations) or len(dbg_ops)
    if debugger_durations:
        tot = sum(debugger_durations)
        pct = (tot / total_elapsed) * 100.0 if (total_elapsed and total_elapsed > 0) else None
        count_str = f"{len(debugger_durations)} operation{'s' if len(debugger_durations) != 1 else ''}"
        cats.append(
            TimingCategorySummary(
                name="Debugger / PDB",
                total_seconds=tot,
                count=len(debugger_durations),
                percentage=pct,
                detail=count_str,
            )
        )
    elif dbg_count > 0 or view.debugger.session_started or view.pdb_observed:
        count_str = f"{dbg_count} observation{'s' if dbg_count != 1 else ''}" if dbg_count > 0 else "1 session"
        cats.append(
            TimingCategorySummary(
                name="Debugger / PDB",
                total_seconds=None,
                count=dbg_count or 1,
                percentage=None,
                detail=f"{count_str} (unmeasured)",
            )
        )

    # 3. Tools
    tool_ops = [e for e in view.timeline if e.event_kind == SessionEventKind.TOOL_COMPLETED]
    tool_count = len(tool_durations) or len(tool_ops)
    if tool_durations:
        tot = sum(tool_durations)
        pct = (tot / total_elapsed) * 100.0 if (total_elapsed and total_elapsed > 0) else None
        count_str = f"{len(tool_durations)} execution{'s' if len(tool_durations) != 1 else ''}"
        cats.append(
            TimingCategorySummary(
                name="Tools",
                total_seconds=tot,
                count=len(tool_durations),
                percentage=pct,
                detail=count_str,
            )
        )
    elif tool_count > 0:
        count_str = f"{tool_count} execution{'s' if tool_count != 1 else ''}"
        cats.append(
            TimingCategorySummary(
                name="Tools",
                total_seconds=None,
                count=tool_count,
                percentage=None,
                detail=f"{count_str} (unmeasured)",
            )
        )

    # 4. Verification
    ver_ops = [e for e in view.timeline if "verifier" in e.event_kind.value]
    ver_count = len(verifier_durations) or len(ver_ops)
    if verifier_durations:
        tot = sum(verifier_durations)
        pct = (tot / total_elapsed) * 100.0 if (total_elapsed and total_elapsed > 0) else None
        count_str = f"{len(verifier_durations)} stage{'s' if len(verifier_durations) != 1 else ''}"
        cats.append(
            TimingCategorySummary(
                name="Verification",
                total_seconds=tot,
                count=len(verifier_durations),
                percentage=pct,
                detail=count_str,
            )
        )
    elif ver_count > 0 or view.verifier_summary or view.verifier_stages:
        count_str = f"{len(view.verifier_stages)} stage{'s' if len(view.verifier_stages) != 1 else ''}" if view.verifier_stages else "1 verification"
        cats.append(
            TimingCategorySummary(
                name="Verification",
                total_seconds=None,
                count=ver_count or len(view.verifier_stages) or 1,
                percentage=None,
                detail=f"{count_str} (unmeasured)",
            )
        )

    # 5. Patch lifecycle
    patch_ops = [e for e in view.timeline if "patch" in e.event_kind.value]
    patch_count = len(view.patch_attempts) or len(patch_durations) or len(patch_ops)
    if patch_durations:
        tot = sum(patch_durations)
        pct = (tot / total_elapsed) * 100.0 if (total_elapsed and total_elapsed > 0) else None
        count_str = f"{patch_count} attempt{'s' if patch_count != 1 else ''}"
        cats.append(
            TimingCategorySummary(
                name="Patch lifecycle",
                total_seconds=tot,
                count=patch_count,
                percentage=pct,
                detail=count_str,
            )
        )
    elif patch_count > 0:
        count_str = f"{patch_count} attempt{'s' if patch_count != 1 else ''}"
        cats.append(
            TimingCategorySummary(
                name="Patch lifecycle",
                total_seconds=None,
                count=patch_count,
                percentage=None,
                detail=f"{count_str} (unmeasured)",
            )
        )

    # 6. Cleanup
    clean_count = len(cleanup_durations)
    if cleanup_durations:
        tot = sum(cleanup_durations)
        pct = (tot / total_elapsed) * 100.0 if (total_elapsed and total_elapsed > 0) else None
        count_str = f"{len(cleanup_durations)} step{'s' if len(cleanup_durations) != 1 else ''}"
        cats.append(
            TimingCategorySummary(
                name="Cleanup",
                total_seconds=tot,
                count=len(cleanup_durations),
                percentage=pct,
                detail=count_str,
            )
        )
    elif view.cleanup_verified is not None:
        cats.append(
            TimingCategorySummary(
                name="Cleanup",
                total_seconds=None,
                count=1,
                percentage=None,
                detail="1 step (unmeasured)",
            )
        )

    accounted = sum(c.total_seconds for c in cats if c.total_seconds is not None)

    return SessionTiming(
        has_timestamps=has_timestamps,
        total_elapsed_seconds=total_elapsed,
        accounted_seconds=accounted,
        categories=tuple(cats),
        timed_operations=tuple(timed_ops),
    )


def render_timeline_report(
    view: SessionViewState,
    timing: SessionTiming,
    boundaries: Optional[frozenset[int]] = None,
) -> Text:
    """Render the structured time-consumption summary report and timed operations."""
    text = Text()
    if not view.timeline:
        text.append("No events recorded.\n", style="dim")
        return text

    if not timing.has_timestamps and not timing.timed_operations and timing.accounted_seconds == 0.0:
        text.append("SESSION TIME BREAKDOWN\n", style=f"bold {PRIMARY}")
        text.append("Total Elapsed: Not recorded\n", style="dim")
        text.append("─" * 72 + "\n", style=LINE)
        text.append("Session timing data was not recorded for this session.\n\n", style="dim")
        return text

    text.append("SESSION TIME BREAKDOWN\n", style=f"bold {PRIMARY}")
    if timing.total_elapsed_seconds is not None:
        secs = timing.total_elapsed_seconds
        mins = int(secs // 60)
        rem = secs % 60
        time_str = f"{mins:02d}:{rem:04.1f} ({secs:.1f}s)" if mins > 0 else f"{secs:.1f}s"
        text.append(f"Total Elapsed: {time_str}\n", style=f"bold {FOREGROUND}")
        if timing.accounted_seconds > 0:
            acc_secs = timing.accounted_seconds
            acc_mins = int(acc_secs // 60)
            acc_rem = acc_secs % 60
            acc_str = f"{acc_mins:02d}:{acc_rem:04.1f} ({acc_secs:.1f}s)" if acc_mins > 0 else f"{acc_secs:.1f}s"
            if secs > 0:
                acc_pct = (acc_secs / secs) * 100.0
                if acc_secs <= secs:
                    text.append(f"Accounted:     {acc_str} / {time_str} ({acc_pct:.1f}%)\n", style=FOREGROUND)
                else:
                    text.append(f"Accounted:     {acc_str} / {time_str} ({acc_pct:.1f}%, overlapping measurements)\n", style=WARNING)
            else:
                text.append(f"Accounted:     {acc_str}\n", style=FOREGROUND)
    else:
        text.append("Total Elapsed: Not recorded\n", style="dim")
        if timing.accounted_seconds > 0:
            acc_secs = timing.accounted_seconds
            acc_mins = int(acc_secs // 60)
            acc_rem = acc_secs % 60
            acc_str = f"{acc_mins:02d}:{acc_rem:04.1f} ({acc_secs:.1f}s)" if acc_mins > 0 else f"{acc_secs:.1f}s"
            text.append(f"Accounted:     {acc_str} (total elapsed unmeasured)\n", style=FOREGROUND)
    text.append("─" * 72 + "\n", style=LINE)

    text.append(f"{'CATEGORY':<24} {'TIME':<14} {'% OF TOTAL':<12} {'OPERATIONS'}\n", style=f"bold {MUTED}")
    text.append("─" * 72 + "\n", style=LINE)

    for cat in timing.categories:
        dur_str = f"{cat.total_seconds:.1f}s" if cat.total_seconds is not None else "Not recorded"
        pct_str = f"{cat.percentage:.1f}%" if cat.percentage is not None else "—"
        cat_style = FOREGROUND
        if "Model" in cat.name:
            cat_style = SECONDARY
        elif "Debugger" in cat.name:
            cat_style = DEBUGGER
        elif "Tool" in cat.name:
            cat_style = TOOL
        elif "Verification" in cat.name:
            cat_style = SUCCESS
        elif "Patch" in cat.name:
            cat_style = EVIDENCE
        elif "Cleanup" in cat.name:
            cat_style = SUCCESS

        dur_style = FOREGROUND if cat.total_seconds is not None else "dim"
        text.append(f"{cat.name:<24}", style=f"bold {cat_style}")
        text.append(f"{dur_str:<14}", style=dur_style)
        text.append(f"{pct_str:<12}", style=MUTED)
        text.append(f"{cat.detail}\n", style=FAINT)

    if timing.total_elapsed_seconds is not None and timing.total_elapsed_seconds > timing.accounted_seconds:
        unattributed = timing.total_elapsed_seconds - timing.accounted_seconds
        unatt_pct = (unattributed / timing.total_elapsed_seconds) * 100.0 if timing.total_elapsed_seconds > 0 else 0.0
        text.append(f"{'Unattributed':<24}", style=f"bold {MUTED}")
        text.append(f"{unattributed:.1f}s{'':<9}", style="dim")
        text.append(f"{unatt_pct:.1f}%{'':<6}", style=MUTED)
        text.append("Not measured\n", style=FAINT)

    bar_width = 48
    if timing.total_elapsed_seconds is not None and timing.total_elapsed_seconds > 0:
        if timing.accounted_seconds <= timing.total_elapsed_seconds:
            if timing.accounted_seconds > 0:
                text.append("\n[", style=MUTED)
                allocated_chars = 0
                for cat in timing.categories:
                    if cat.total_seconds and cat.total_seconds > 0:
                        cat_chars = int(round((cat.total_seconds / timing.total_elapsed_seconds) * bar_width))
                        if cat_chars < 1:
                            cat_chars = 1
                        allocated_chars += cat_chars
                        char_style = SECONDARY if "Model" in cat.name else (DEBUGGER if "Debugger" in cat.name else (TOOL if "Tool" in cat.name else (SUCCESS if "Verification" in cat.name else (EVIDENCE if "Patch" in cat.name else SUCCESS))))
                        text.append("█" * cat_chars, style=char_style)
                unmeasured_chars = max(0, bar_width - allocated_chars)
                if unmeasured_chars > 0:
                    text.append("░" * unmeasured_chars, style=FAINT)
                text.append("]\n\n", style=MUTED)
            else:
                text.append("\n")
        else:
            text.append("\n[", style=MUTED)
            text.append("Overlapping measurements — categories exceed wall-clock time", style=WARNING)
            text.append("]\n\n", style=MUTED)
    elif timing.accounted_seconds > 0:
        text.append("\n[", style=MUTED)
        for cat in timing.categories:
            if cat.total_seconds and cat.total_seconds > 0:
                cat_chars = max(1, int(round((cat.total_seconds / timing.accounted_seconds) * bar_width)))
                char_style = SECONDARY if "Model" in cat.name else (DEBUGGER if "Debugger" in cat.name else (TOOL if "Tool" in cat.name else (SUCCESS if "Verification" in cat.name else EVIDENCE)))
                text.append("█" * cat_chars, style=char_style)
        text.append("]  (wall-clock total elapsed not recorded)\n\n", style=FAINT)
    else:
        text.append("\n")

    # TIMED OPERATIONS section
    text.append("TIMED OPERATIONS\n", style=f"bold {PRIMARY}")
    text.append("─" * 72 + "\n", style=LINE)
    if not timing.timed_operations:
        text.append("No individual timed operations recorded.\n", style="dim")
    else:
        for op in timing.timed_operations:
            time_str = ""
            if op.start_time_offset is not None:
                secs = int(op.start_time_offset)
                time_str = f"{secs // 60:02d}:{secs % 60:02d} "
            if time_str:
                text.append(time_str, style=f"bold {EVIDENCE}")
            text.append(f"#{op.sequence:<4} ", style="dim")
            text.append(f"{op.label:<34} ", style=FOREGROUND)
            if op.in_flight:
                text.append("(running…)", style=f"bold {WARNING}")
            else:
                text.append(f"{op.duration_seconds:.1f}s", style=f"bold {FOREGROUND}")
                pct_str = f"({(op.duration_seconds / timing.total_elapsed_seconds) * 100.0:.1f}%)" if (timing.total_elapsed_seconds and timing.total_elapsed_seconds > 0) else ""
                if pct_str:
                    text.append(f"  {pct_str}", style="dim")
            text.append("\n")

    return text


class TimelinePanel(VerticalScroll):
    """Session time consumption breakdown and chronological timed operations."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text: Static = Static("")
        self._view: Optional[SessionViewState] = None
        self._boundaries: frozenset[int] = frozenset()

    def compose(self) -> ComposeResult:
        yield self._text

    def _render_view(self, view: SessionViewState) -> Text:
        timing = compute_session_timing(view)
        return render_timeline_report(view, timing, boundaries=self._boundaries)

    def update_view(
        self,
        view: SessionViewState,
        phase_boundary_sequences: Optional[frozenset[int]] = None,
    ) -> None:
        self._view = view
        if phase_boundary_sequences is not None:
            self._boundaries = phase_boundary_sequences
        self._text.update(self._render_view(view))

    def export_text(self, view: Optional[SessionViewState] = None) -> str:
        """Full logical Timeline export for clipboard."""
        target = view if view is not None else self._view
        if target is None:
            return "No events recorded."
        return timeline_export_text(target, phase_boundary_sequences=self._boundaries)


def _local_project_identity(view: SessionViewState) -> tuple[str, str]:
    """(repo basename, short HEAD) from the durable diagnosis record.

    The Local Project session records both facts in the ``diagnosis.recorded``
    observed_values at session start; the reducer copies them verbatim.  When
    the record is absent the honest placeholder is shown — never an inferred
    value from task ids or aliases.
    """
    observed = view.diagnosis.observed_values if view.diagnosis is not None else None
    repo = "—"
    head = "—"
    if isinstance(observed, dict):
        candidate_repo = observed.get("repo_basename")
        candidate_head = observed.get("source_head")
        if isinstance(candidate_repo, str) and candidate_repo:
            repo = candidate_repo
        if isinstance(candidate_head, str) and candidate_head:
            head = candidate_head
    return repo, head


def _official_tests_label(view: SessionViewState) -> Optional[str]:
    """Official-verifier milestone label; only proven execution is 'Executed'."""
    if view.source_kind is not SourceKind.LEVEL32_OPERATOR:
        return None
    if view.official_execution_proven is True:
        return "Executed"
    if view.operator_stage is OperatorStage.OFFICIAL_EVALUATOR_COMPLETED:
        return "Completed (unproven)"
    if view.operator_stage is OperatorStage.OFFICIAL_EVALUATOR_STARTED:
        return "Evaluator launched"
    if view.operator_stage is OperatorStage.OFFICIAL_VERIFICATION_PREPARING:
        return "Preparing"
    if view.status.terminal:
        return "Not executed"
    return "Not started"


def timeline_export_text(
    view: SessionViewState,
    *,
    task_title: Optional[str] = None,
    phase_boundary_sequences: Optional[frozenset[int]] = None,
) -> str:
    """Deterministic text export for the Timeline timing breakdown."""
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
        f"View: Timeline",
        "",
    ]

    timing = compute_session_timing(view)
    if timing.has_timestamps or timing.accounted_seconds > 0:
        lines.append("SESSION TIME BREAKDOWN")
        if timing.total_elapsed_seconds is not None:
            secs = timing.total_elapsed_seconds
            mins = int(secs // 60)
            rem = secs % 60
            time_str = f"{mins:02d}:{rem:04.1f} ({secs:.1f}s)" if mins > 0 else f"{secs:.1f}s"
            lines.append(f"Total Elapsed: {time_str}")
            if timing.accounted_seconds > 0:
                acc_secs = timing.accounted_seconds
                acc_mins = int(acc_secs // 60)
                acc_rem = acc_secs % 60
                acc_str = f"{acc_mins:02d}:{acc_rem:04.1f} ({acc_secs:.1f}s)" if acc_mins > 0 else f"{acc_secs:.1f}s"
                if secs > 0:
                    acc_pct = (acc_secs / secs) * 100.0
                    if acc_secs <= secs:
                        lines.append(f"Accounted:     {acc_str} / {time_str} ({acc_pct:.1f}%)")
                    else:
                        lines.append(f"Accounted:     {acc_str} / {time_str} ({acc_pct:.1f}%, overlapping measurements)")
                else:
                    lines.append(f"Accounted:     {acc_str}")
        else:
            lines.append("Total Elapsed: Not recorded")
            if timing.accounted_seconds > 0:
                acc_secs = timing.accounted_seconds
                acc_mins = int(acc_secs // 60)
                acc_rem = acc_secs % 60
                acc_str = f"{acc_mins:02d}:{acc_rem:04.1f} ({acc_secs:.1f}s)" if acc_mins > 0 else f"{acc_secs:.1f}s"
                lines.append(f"Accounted:     {acc_str} (total elapsed unmeasured)")
        lines.append("")
        lines.append(f"{'CATEGORY':<24} {'TIME':<14} {'% OF TOTAL':<12} {'OPERATIONS'}")
        for cat in timing.categories:
            dur_str = f"{cat.total_seconds:.1f}s" if cat.total_seconds is not None else "Not recorded"
            pct_str = f"{cat.percentage:.1f}%" if cat.percentage is not None else "—"
            lines.append(f"{cat.name:<24} {dur_str:<14} {pct_str:<12} {cat.detail}")
        if timing.total_elapsed_seconds is not None and timing.total_elapsed_seconds > timing.accounted_seconds:
            unattributed = timing.total_elapsed_seconds - timing.accounted_seconds
            unatt_pct = (unattributed / timing.total_elapsed_seconds) * 100.0 if timing.total_elapsed_seconds > 0 else 0.0
            lines.append(f"{'Unattributed':<24} {unattributed:.1f}s{'':<9} {unatt_pct:.1f}%{'':<6} Not measured")
        lines.append("")

        lines.append("TIMED OPERATIONS")
        if not timing.timed_operations:
            lines.append("No individual timed operations recorded.")
        else:
            for op in timing.timed_operations:
                off_str = f"{int(op.start_time_offset // 60):02d}:{op.start_time_offset % 60:04.1f}" if op.start_time_offset is not None else ""
                if op.in_flight:
                    lines.append(f"{off_str:<7} #{op.sequence:<4} {op.label:<34} (running…)")
                else:
                    pct_str = f"({(op.duration_seconds / timing.total_elapsed_seconds) * 100.0:.1f}%)" if (timing.total_elapsed_seconds and timing.total_elapsed_seconds > 0) else ""
                    lines.append(f"{off_str:<7} #{op.sequence:<4} {op.label:<34} {op.duration_seconds:.1f}s  {pct_str}")
    else:
        lines.append("Session timing data not recorded for this session.")
    return "\n".join(lines)
