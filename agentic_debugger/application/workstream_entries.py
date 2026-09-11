"""Workstream entry contracts and fold primitives.

This module owns the semantic work-unit vocabulary
(:class:`WorkstreamKind`/:class:`WorkstreamStatus`), the immutable
:class:`WorkstreamEntry` record, the bounded retention cap, and the pure
fold primitives (append with tail bound, identity-matched settlement,
coalescing, tool-unit mapping, change-unit lookup/update) used by the
workstream event fold in :mod:`agentic_debugger.application.workstream`.

Dependency rule: imports only the change-preview projection types (a
change unit carries a bounded :class:`ChangePreview`).  No application
package imports; event facts arrive as plain arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

from agentic_debugger.application.change_preview import ChangePreview

__all__ = [
    "MAX_WORKSTREAM_ENTRIES",
    "WorkstreamEntry",
    "WorkstreamKind",
    "WorkstreamStatus",
]

#: Durable retained workstream entries (tail-bounded; rendering shows far
#: fewer).  Small because entries are semantic units, not raw events.
MAX_WORKSTREAM_ENTRIES = 500

#: Tool names that read source (rendered as READ SOURCE units).
_SOURCE_READ_TOOLS = frozenset(
    {"get_source_window", "search_code", "find_function", "find_class"}
)

#: Tool names that drive the debugger session (rendered as DEBUGGER units).
_DEBUGGER_TOOLS = frozenset(
    {
        "start_pdb_session",
        "continue_pdb_session",
        "step_pdb_session",
        "next_pdb_session",
        "stop_pdb_session",
    }
)

#: Tool names that observe PDB state (rendered as PDB units).
_PDB_OBSERVE_TOOLS = frozenset(
    {
        "get_stack_summary",
        "get_frame",
        "get_frame_locals",
        "safe_eval_expression",
        "inspect_caller_frame",
    }
)


class WorkstreamKind(str, Enum):
    """Semantic type of one operational work unit."""

    WORKSPACE = "workspace"
    MODEL_REQUEST = "model_request"
    SOURCE_READ = "source_read"
    TOOL = "tool"
    DEBUGGER = "debugger"
    PDB = "pdb"
    DIAGNOSIS = "diagnosis"
    CHANGE = "change"
    VERIFICATION = "verification"
    OFFICIAL_VERIFICATION = "official_verification"
    CLEANUP = "cleanup"
    ERROR = "error"
    SESSION = "session"


class WorkstreamStatus(str, Enum):
    """Lifecycle status of one operational work unit."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING = "waiting"


@dataclass(frozen=True)
class WorkstreamEntry:
    """One curated operational work unit.

    ``sequence`` is the durable event sequence that last touched the unit
    (ordering authority); ``ordinal`` is the one-based user-facing
    request/attempt number when the unit has one.  ``change`` carries the
    bounded diff preview for code-changing units only.
    """

    kind: WorkstreamKind
    status: WorkstreamStatus
    label: str
    sequence: int
    target: Optional[str] = None
    detail: Optional[str] = None
    ordinal: Optional[int] = None
    change: Optional[ChangePreview] = None
    timestamp_utc: Optional[str] = None
    duration_seconds: Optional[float] = None


def _append(
    entries: Tuple[WorkstreamEntry, ...], entry: WorkstreamEntry
) -> Tuple[WorkstreamEntry, ...]:
    updated = entries + (entry,)
    if len(updated) > MAX_WORKSTREAM_ENTRIES:
        updated = updated[len(updated) - MAX_WORKSTREAM_ENTRIES :]
    return updated


def _settle(
    entries: Tuple[WorkstreamEntry, ...],
    *,
    kind: WorkstreamKind,
    status: WorkstreamStatus,
    sequence: int,
    label: Optional[str] = None,
    detail: Optional[str] = None,
    target: Optional[str] = None,
    ordinal: Optional[int] = None,
    match_detail: Optional[str] = None,
    timestamp_utc: Optional[str] = None,
    duration_seconds: Optional[float] = None,
) -> Tuple[WorkstreamEntry, ...]:
    """Settle the most recent ACTIVE entry of one kind (identity match).

    Returns the input unchanged when no matching active unit exists.
    """
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if entry.kind is not kind or entry.status is not WorkstreamStatus.ACTIVE:
            continue
        if ordinal is not None and entry.ordinal is not None and entry.ordinal != ordinal:
            continue
        if match_detail is not None and entry.detail != match_detail:
            continue
        settled = replace(
            entry,
            status=status,
            sequence=sequence,
            label=label or entry.label,
            detail=detail if detail is not None else entry.detail,
            target=target or entry.target,
            timestamp_utc=entry.timestamp_utc or timestamp_utc,
            duration_seconds=duration_seconds if duration_seconds is not None else entry.duration_seconds,
        )
        return entries[:index] + (settled,) + entries[index + 1 :]
    return entries


def _coalesce_completed(
    entries: Tuple[WorkstreamEntry, ...], entry: WorkstreamEntry
) -> Tuple[WorkstreamEntry, ...]:
    """Coalesce a completed unit with an identical immediately-preceding one.

    Only *identical operational identity* (same kind, label, and target)
    coalesces: repeated PDB observations of the same pause location collapse
    to a single observed row while distinct locations stay distinct rows.
    """
    if entries:
        last = entries[-1]
        if (
            last.kind is entry.kind
            and last.label == entry.label
            and last.target == entry.target
            and last.status is WorkstreamStatus.COMPLETED
        ):
            return entries[:-1] + (entry,)
    return _append(entries, entry)


def _token_usage_detail(payload: Mapping[str, Any]) -> Optional[str]:
    """Compact per-request provider token usage (known dimensions only).

    Unknown dimensions are omitted rather than rendered as fake zeroes;
    a request with no usage block produces no detail at all.  Counts are
    the durable event's validated provider-reported values — never
    locally estimated.  Partial/lower-bound dimensions carry the truthful
    '+' indicator.
    """
    usage = payload.get("token_usage")
    if not isinstance(usage, Mapping):
        return None
    coverage = payload.get("token_usage_coverage")
    cov_map = coverage if isinstance(coverage, Mapping) else {}
    parts: list[str] = []
    for label, field in (
        ("Input", "input_tokens"),
        ("Cached", "cached_input_tokens"),
        ("Output", "output_tokens"),
        ("Total", "total_tokens"),
    ):
        count = usage.get(field)
        if type(count) is int:
            suffix = "+" if cov_map.get(field) is False else ""
            parts.append(f"{label} {count:,}{suffix}")
    return " · ".join(parts) if parts else None


def _active_model_request_detail(
    entries: Tuple[WorkstreamEntry, ...], ordinal: int
) -> Optional[str]:
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if (
            entry.kind is WorkstreamKind.MODEL_REQUEST
            and entry.status is WorkstreamStatus.ACTIVE
            and entry.ordinal == ordinal
        ):
            return entry.detail
    return None


def _tool_unit(tool_name: str) -> Tuple[WorkstreamKind, str]:
    """Map one tool identity to its human-facing work-unit class."""
    if tool_name in _SOURCE_READ_TOOLS:
        return WorkstreamKind.SOURCE_READ, "Read source"
    if tool_name in _DEBUGGER_TOOLS:
        return WorkstreamKind.DEBUGGER, "Debugger"
    if tool_name in _PDB_OBSERVE_TOOLS:
        return WorkstreamKind.PDB, "PDB observe"
    if tool_name == "express_root_cause_hypothesis":
        return WorkstreamKind.DIAGNOSIS, "Diagnosis"
    if tool_name == "apply_patch":
        return WorkstreamKind.CHANGE, "Change"
    if tool_name == "run_reproduction":
        return WorkstreamKind.TOOL, "Run reproduction"
    if tool_name in ("run_tests", "run_regression_tests"):
        return WorkstreamKind.TOOL, "Run tests"
    return WorkstreamKind.TOOL, tool_name


def _tool_detail(tool_name: str, unit_label: str) -> Optional[str]:
    """Secondary tool text: kept only when it is not the label itself."""
    if tool_name == unit_label or tool_name.replace("_", " ") == unit_label.lower():
        return None
    return tool_name


def _change_index(
    entries: Tuple[WorkstreamEntry, ...], ordinal: int
) -> Optional[int]:
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if entry.kind is WorkstreamKind.CHANGE and entry.ordinal == ordinal:
            return index
    return None


def _update_change(
    entries: Tuple[WorkstreamEntry, ...],
    ordinal: int,
    *,
    active_only: bool = False,
    **fields: object,
) -> Tuple[WorkstreamEntry, ...]:
    """Update one change unit by attempt ordinal (no-op when absent).

    ``active_only`` restricts the update to units still in flight so a late
    lifecycle fact can never regress a settled apply/reject outcome.
    """
    index = _change_index(entries, ordinal)
    if index is None:
        return entries
    entry = entries[index]
    if active_only and entry.status is not WorkstreamStatus.ACTIVE:
        return entries
    updated = replace(entry, **fields)  # type: ignore[arg-type]
    return entries[:index] + (updated,) + entries[index + 1 :]


def _current_frame_target(frames: object) -> Optional[str]:
    """``script:line`` of the current frame of one recorded stack."""
    if not isinstance(frames, (list, tuple)):
        return None
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        if frame.get("is_current") is not True:
            continue
        script = frame.get("file")
        line = frame.get("line")
        if isinstance(script, str) and isinstance(line, int):
            return f"{script}:{line}"
    return None
