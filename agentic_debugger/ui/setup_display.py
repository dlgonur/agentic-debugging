"""Pure display/formatting helpers for the session-setup screen.

This module owns the pure presentation derivations used by
:class:`~agentic_debugger.ui.screens_setup.StartSessionScreen`: the
task display name, model display pair, ladder presentation,
debugger label, and bug preview — all pure functions over the
screen's configuration state.
"""

from __future__ import annotations

from typing import Optional, Tuple

from agentic_debugger.application.model_providers import format_model_display_name
from agentic_debugger.application.level32 import (
LEVEL32_TASK_ID,
is_ladder_task,
ladder_task_metadata,
)
from agentic_debugger.ui.session_config import (
    GROUP_BOUNDS,
    GROUP_HOW,
    GROUP_WHAT,
    GROUP_WHERE,
    LOCAL_SETUP_GROUPS,
    POLICY_LABELS,
    POLICY_ON_UNCERTAINTY,
    POLICY_STATIC_BASELINE,
    PROVIDER_CONFIGURED,
    PROVIDER_LABELS,
    SEVERITY_ERROR,
    TARGET_LADDER,
    TARGET_LOCAL_PROJECT,
    ModelChoice,
    ModelOption,
)

# One-line Apply-eligibility consequence for the Repro/Verify editors.
# Empty commands silently narrow the verifier certificate to a state that
# can never satisfy permits_apply (1/1 F2P + 1/1 P2P).
APPLY_ELIGIBILITY_CAPTION = (
    "Empty \u2192 this session can never become Apply-eligible "
    "(needs 1/1 F2P + 1/1 P2P)."
)


def local_context_notes_text(screen) -> str:
    """One-line context notes replacing Task/Debugger rows when Local.

    Reuses the exact RowState vocabulary: the repository (not a fixture
    task) plus the fixed-by-contract debugger.
    """
    debugger = debugger_display(screen)
    return (
        "Local Project uses your repository, not a fixture task "
        f"\u00b7 Debugger fixed by contract ({debugger})"
    )


def repro_auto_tag(screen) -> str:
    """Visible marker distinguishing auto-filled Repro from typed input."""
    if getattr(screen, "_repro_is_auto", False) and screen._config.reproduction_command:
        return "auto"
    return ""


def verify_auto_tag(screen) -> str:
    """Visible marker distinguishing auto-filled Verify from typed input."""
    if getattr(screen, "_verify_is_auto", False) and screen._config.verification_command:
        return "auto"
    return ""


def local_group_error_counts(readiness) -> dict:
    """Error counts per Local setup group from the single SessionReadiness."""
    by_field: dict[str, int] = {}
    for issue in readiness.issues:
        if issue.severity == SEVERITY_ERROR:
            by_field[issue.field] = by_field.get(issue.field, 0) + 1
    counts: dict[str, int] = {}
    for group_id, _title, fields in LOCAL_SETUP_GROUPS:
        counts[group_id] = sum(by_field.get(field, 0) for field in fields)
    return counts


def local_group_status_label(group_id: str, error_count: int) -> str:
    """Compact per-group status suffix (one short token, never a gate)."""
    if error_count <= 0:
        return "\u2713"
    if error_count == 1:
        return "! 1 to fix"
    return f"! {error_count} to fix"

def task_display_name(screen) -> str:
    task = screen._catalog.find_task(screen._config.task_id)
    if task is not None:
        title = task.title
    elif screen._config.task_id:
        title = next(
            (
                label.split("·", 1)[0].strip()
                for label, task_id in screen._task_options
                if task_id == screen._config.task_id
            ),
            screen._config.task_id,
        )
    else:
        title = "Not selected"
    if screen.size.width and screen.size.width < 70:
        available = max(18, screen.size.width - 20)
        if len(title) > available:
            return f"{title[: available - 1]}…"
    return title


def model_display(screen) -> tuple[str, str]:
    choice = screen._config.model
    if choice.is_offline:
        return "Offline", "Offline"
    label = _provider_label(choice.provider)
    if choice.provider == PROVIDER_CONFIGURED:
        display = choice.display or choice.model_id
    else:
        display = format_model_display_name(choice.display or choice.model_id)
    return display, label


def ladder_presentation(screen) -> tuple[str, str, str]:
    """Derive (debugger, treatment, evaluation) presentation for the current ladder selection."""
    task = screen._catalog.find_task(screen._config.task_id)
    if task is None or not task.ladder:
        return "Frozen contract", "—", "—"
    meta = ladder_task_metadata(task.task_id)
    if task.task_id == LEVEL32_TASK_ID:
        ladder_entry = screen._catalog.ladder_model(screen._config.model)
        if ladder_entry is not None:
            # Qualified official Level-32 route (dispatches to LEVEL32_OPERATOR)
            return meta.debugger, meta.treatment, meta.evaluation
        if not screen._config.model.is_offline:
            # Executable non-qualified Level-32 route (dispatches to CONFIGURED_MODEL)
            return (
                POLICY_LABELS.get(POLICY_ON_UNCERTAINTY, "On uncertainty"),
                "Interactive Level-32 · non-official",
                "Independent verifier",
            )
        if not screen._catalog.ladder_models:
            return (
                POLICY_LABELS.get(POLICY_ON_UNCERTAINTY, "On uncertainty"),
                "Interactive Level-32 · non-official",
                "Independent verifier",
            )
        return meta.debugger, meta.treatment, meta.evaluation
    # Lower ladder rungs (Level 6, 12, 18)
    return meta.debugger, meta.treatment, meta.evaluation


def debugger_display(screen) -> str:
    if screen._config.target == TARGET_LADDER:
        debugger, _, _ = screen._ladder_presentation()
        return debugger
    if screen._config.target == TARGET_LOCAL_PROJECT:
        return POLICY_LABELS[POLICY_ON_UNCERTAINTY]
    return POLICY_LABELS.get(screen._config.debugger_policy, screen._config.debugger_policy)


def bug_preview(screen) -> str:
    text = screen._config.bug_description.strip()
    if not text:
        return "—"
    first = text.splitlines()[0][:48] + ("…" if len(text.splitlines()[0]) > 48 else "")
    if "\n" in text:
        first = f"{first} [+]" if first else "Described [+]"
    return first or "Described"


def _clip_cells(value: str, room: int) -> str:
    """Ellipsize to a cell budget so content never hard-clips at a border."""
    if room <= 1:
        return "…"
    if len(value) <= room:
        return value
    return value[: room - 1].rstrip() + "…"


def _fit_row_cells(
    value: str,
    secondary: str,
    reason: str,
    budget: int,
) -> tuple[str, str, str]:
    """Fit one setting row's value, secondary, and reason into ``budget``
    cells (everything after the 16-cell prefix+label chrome).

    Priority keeps the value intact longest: the reason clips first,
    then the secondary, then the value itself.
    """
    def used(v: str, s: str, r: str) -> int:
        total = len(v)
        if s:
            total += 2 + len(s)
        if r:
            total += 4 + len(r)  # gap + parentheses
        return total

    if used(value, secondary, reason) <= budget:
        return value, secondary, reason
    room = budget - len(value) - (4 if reason else 0)
    if reason and room >= 4:
        reason = _clip_cells(reason, budget - len(value) - 4)
        if used(value, secondary, reason) <= budget:
            return value, secondary, reason
    if secondary:
        secondary = _clip_cells(secondary, max(1, budget - len(value) - (4 + len(reason) if reason else 0)))
        if used(value, secondary, reason) <= budget:
            return value, secondary, reason
    keep = budget - (4 + len(reason) if reason else 0)
    return _clip_cells(value, max(1, keep)), "", reason


def _short_unavailable_reason(reason: Optional[str]) -> str:
    """One bounded picker line for a provider unavailability reason."""
    if not reason:
        return "unavailable"
    text = reason.split("(", 1)[0].strip().rstrip(".")
    if len(text) > 60:
        text = text[:60].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text or "unavailable"


def _provider_label(provider: str) -> str:
    if provider in PROVIDER_LABELS:
        return PROVIDER_LABELS[provider]
    try:
        from agentic_debugger.application.provider_connections import get_provider_config
        cfg = get_provider_config(provider)
        if cfg is not None:
            return cfg.name
    except Exception:
        pass
    return provider
