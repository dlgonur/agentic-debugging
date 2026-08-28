"""Honest effort projection: what did the agent actually try?

A failed session is not an empty session.  The controller may have made a
dozen model requests, run tools, inspected source, attached the debugger,
proposed a patch, and lost only at the last verification step — or it may
have failed at the very first transport call.  Owners need that difference
visible at a glance, on the terminal and in exported reports.

This module is a pure, fail-safe projection over validated session events.
It invents nothing: every number is a count of journal records.  Unknown
payload shapes degrade to omission, never to failure — the summary must
render even for a truncated or historical journal.

Counts are derived from the authoritative event kinds only; sidecar files,
model claims, and controller classifications are not consulted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from agentic_debugger.application.events import SessionEvent, SessionEventKind

__all__ = [
    "EffortSummary",
    "render_effort_summary",
    "summarize_events",
]


@dataclass(frozen=True)
class EffortSummary:
    """Counted journal evidence of one session's agent effort."""

    event_count: int = 0
    duration_seconds: Optional[int] = None
    model_requests: int = 0
    model_requests_ok: int = 0
    model_requests_error: int = 0
    model_requests_timeout: int = 0
    directives_accepted: int = 0
    directives_rejected: int = 0
    rejection_categories: Tuple[Tuple[str, int], ...] = ()
    tool_calls: int = 0
    tools_failed: int = 0
    tool_calls_by_name: Tuple[Tuple[str, int], ...] = ()
    debugger_sessions: int = 0
    debugger_observations: int = 0
    patches_proposed: int = 0
    patches_rejected: int = 0
    patches_applied: int = 0
    diagnosis_notes: int = 0
    transitions: int = 0
    states_visited: Tuple[str, ...] = ()
    verifier_outcome: Optional[str] = None
    verifier_f2p: Optional[str] = None
    verifier_p2p: Optional[str] = None


def _sorted_counts(counter: Dict[str, int]) -> Tuple[Tuple[str, int], ...]:
    return tuple(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def summarize_events(events: Iterable[SessionEvent]) -> EffortSummary:
    """Project one session's journal events into counted effort evidence."""

    model_requests = model_ok = model_error = model_timeout = 0
    accepted = rejected = 0
    rejections: Dict[str, int] = {}
    tool_names: Dict[str, int] = {}
    tools_failed = 0
    debugger_sessions = debugger_observations = 0
    proposed = patch_rejected = applied = 0
    diagnosis_notes = 0
    transitions = 0
    states: List[str] = []
    verifier_outcome: Optional[str] = None
    verifier_f2p: Optional[str] = None
    verifier_p2p: Optional[str] = None
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    count = 0

    for event in events:
        count += 1
        try:
            ts = datetime.fromisoformat(
                event.timestamp_utc.replace("Z", "+00:00")
            )
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        except (AttributeError, TypeError, ValueError):
            pass
        kind = event.event_kind
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        if kind is SessionEventKind.MODEL_REQUEST_STARTED:
            model_requests += 1
        elif kind is SessionEventKind.MODEL_REQUEST_COMPLETED:
            status = payload.get("status")
            if status == "error":
                model_error += 1
            elif status == "timeout":
                model_timeout += 1
            elif status == "ok":
                model_ok += 1
        elif kind is SessionEventKind.MODEL_DIRECTIVE_ACCEPTED:
            accepted += 1
        elif kind is SessionEventKind.MODEL_DIRECTIVE_REJECTED:
            rejected += 1
            category = payload.get("rejection_category")
            if isinstance(category, str) and category:
                rejections[category] = rejections.get(category, 0) + 1
        elif kind is SessionEventKind.TOOL_STARTED:
            name = payload.get("tool_name")
            if isinstance(name, str) and name:
                tool_names[name] = tool_names.get(name, 0) + 1
        elif kind is SessionEventKind.TOOL_COMPLETED:
            if payload.get("status") not in ("ok", "success", None):
                tools_failed += 1
        elif kind is SessionEventKind.DEBUGGER_STARTED:
            debugger_sessions += 1
        elif kind in (
            SessionEventKind.DEBUGGER_STACK_OBSERVED,
            SessionEventKind.DEBUGGER_LOCALS_OBSERVED,
        ):
            debugger_observations += 1
        elif kind is SessionEventKind.PATCH_PROPOSED:
            proposed += 1
        elif kind is SessionEventKind.PATCH_REJECTED:
            patch_rejected += 1
        elif kind is SessionEventKind.PATCH_APPLIED:
            applied += 1
        elif kind is SessionEventKind.DIAGNOSIS_RECORDED:
            diagnosis_notes += 1
        elif kind is SessionEventKind.CONTROLLER_TRANSITION:
            transitions += 1
            target = payload.get("target_state")
            if isinstance(target, str) and target and target not in states:
                states.append(target)
        elif kind is SessionEventKind.VERIFIER_COMPLETED:
            outcome = payload.get("outcome")
            verifier_outcome = outcome if isinstance(outcome, str) else verifier_outcome
            f2p_total = payload.get("f2p_total")
            if isinstance(f2p_total, int):
                verifier_f2p = f"{payload.get('f2p_passed')}/{f2p_total}"
            p2p_total = payload.get("p2p_total")
            if isinstance(p2p_total, int):
                verifier_p2p = f"{payload.get('p2p_passed')}/{p2p_total}"

    duration: Optional[int] = None
    if first_ts is not None and last_ts is not None:
        duration = max(0, int((last_ts - first_ts).total_seconds()))
    return EffortSummary(
        event_count=count,
        duration_seconds=duration,
        model_requests=model_requests,
        model_requests_ok=model_ok,
        model_requests_error=model_error,
        model_requests_timeout=model_timeout,
        directives_accepted=accepted,
        directives_rejected=rejected,
        rejection_categories=_sorted_counts(rejections),
        tool_calls=sum(tool_names.values()),
        tools_failed=tools_failed,
        tool_calls_by_name=_sorted_counts(tool_names),
        debugger_sessions=debugger_sessions,
        debugger_observations=debugger_observations,
        patches_proposed=proposed,
        patches_rejected=patch_rejected,
        patches_applied=applied,
        diagnosis_notes=diagnosis_notes,
        transitions=transitions,
        states_visited=tuple(states),
        verifier_outcome=verifier_outcome,
        verifier_f2p=verifier_f2p,
        verifier_p2p=verifier_p2p,
    )


def _fmt_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "—"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def render_effort_summary(summary: EffortSummary, *, title: str = "What the agent tried") -> str:
    """Render counted effort as a compact human-readable block.

    Every line is journal-derived; nothing is inferred or promised.
    """

    lines: List[str] = [title, "=" * len(title)]
    if summary.event_count == 0:
        lines.append("No journal events: the session produced no recorded effort.")
        return "\n".join(lines)
    lines.append(f"{summary.event_count} journal events in {_fmt_duration(summary.duration_seconds)}")
    requests = f"{summary.model_requests} model request(s)"
    detail: List[str] = []
    if summary.model_requests_ok:
        detail.append(f"{summary.model_requests_ok} ok")
    if summary.model_requests_error:
        detail.append(f"{summary.model_requests_error} error")
    if summary.model_requests_timeout:
        detail.append(f"{summary.model_requests_timeout} timeout")
    if detail:
        requests += f" ({', '.join(detail)})"
    lines.append(requests)
    directives = f"{summary.directives_accepted} directive(s) accepted"
    if summary.directives_rejected:
        directives += f", {summary.directives_rejected} rejected"
        if summary.rejection_categories:
            cats = ", ".join(f"{name}×{count}" for name, count in summary.rejection_categories[:4])
            directives += f" [{cats}]"
    lines.append(directives)
    if summary.tool_calls:
        tools = ", ".join(f"{name}×{count}" for name, count in summary.tool_calls_by_name[:6])
        suffix = f" ({summary.tools_failed} failed)" if summary.tools_failed else ""
        lines.append(f"{summary.tool_calls} tool call(s){suffix}: {tools}")
    if summary.debugger_sessions or summary.debugger_observations:
        lines.append(
            f"debugger: {summary.debugger_sessions} session(s), "
            f"{summary.debugger_observations} observation(s)"
        )
    if summary.patches_proposed or summary.patches_rejected or summary.patches_applied:
        lines.append(
            f"patches: {summary.patches_proposed} proposed, "
            f"{summary.patches_rejected} rejected, {summary.patches_applied} applied"
        )
    if summary.diagnosis_notes:
        lines.append(f"{summary.diagnosis_notes} diagnosis note(s) recorded")
    if summary.states_visited:
        lines.append("states visited: " + " -> ".join(summary.states_visited[:8]))
    if summary.verifier_outcome is not None or summary.verifier_f2p is not None:
        outcome = summary.verifier_outcome or "—"
        checks = []
        if summary.verifier_f2p is not None:
            checks.append(f"F2P {summary.verifier_f2p}")
        if summary.verifier_p2p is not None:
            checks.append(f"P2P {summary.verifier_p2p}")
        suffix = f" ({', '.join(checks)})" if checks else ""
        lines.append(f"independent verifier: {outcome}{suffix}")
    return "\n".join(lines)
