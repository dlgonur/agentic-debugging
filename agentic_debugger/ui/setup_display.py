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
    POLICY_LABELS,
    POLICY_ON_UNCERTAINTY,
    POLICY_STATIC_BASELINE,
    PROVIDER_CONFIGURED,
    PROVIDER_LABELS,
    TARGET_LADDER,
    TARGET_LOCAL_PROJECT,
    ModelChoice,
    ModelOption,
)

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
