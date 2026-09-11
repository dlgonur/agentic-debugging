"""Application-level naming, identity, and curated-task helpers.

This module owns the pure application-shell helpers: task display
titles/options, source names, default history root, repository root,
session id minting, and the curated task id/option derivations
operating on the application passed explicitly.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from agentic_debugger.application.level32 import (
    LEVEL32_TASK_ID,
    LADDER_TASK_IDS,
    ladder_task_metadata,
    ladder_task_options,
)
from agentic_debugger.application.history import default_history_root as application_default_history_root
from agentic_debugger.evaluation.runner import load_task

_CURATED_TASK_TITLES: dict[str, str] = {
    "curated-none-handling-001": "Format an optional display name",
    "curated-off-by-one-002": "Return the complete recent window",
    "curated-wrong-branch-003": "Select the correct access branch",
    "curated-mutation-alias-004": "Append a label without mutating the caller",
    "curated-caller-callee-005": "Convert the caller representation at the boundary",
}


def task_display_title(task_id: str, repo_root: Optional[Path] = None) -> str:
    """Return a human-readable title for a task id.

    Tries loading the task title from task.json under the repository root,
    then checks curated mapping, and falls back to task_id if unavailable.
    """
    if repo_root is not None:
        task_json = (
            Path(repo_root)
            / "agentic_debugger"
            / "datasets"
            / "curated"
            / task_id
            / "task.json"
        )
        if task_json.is_file():
            try:
                import json

                with open(task_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and data.get("title"):
                        return str(data["title"])
            except Exception:
                pass
    if task_id == "local-project-debug":
        return "Local Project Debug"
    if task_id in _CURATED_TASK_TITLES:
        return _CURATED_TASK_TITLES[task_id]
    if task_id in LADDER_TASK_IDS:
        return ladder_task_metadata(task_id).title
    return task_id


def task_display_option(
    task_id: str, repo_root: Optional[Path] = None
) -> tuple[str, str]:
    """Return (label, task_id) for dropdown selectors."""
    title = task_display_title(task_id, repo_root)
    if title != task_id:
        return f"{title} · {task_id}", task_id
    return task_id, task_id


def deterministic_source_name() -> str:
    """The one production deterministic worker source (Task 7)."""
    from agentic_debugger.application.deterministic_source import (
        DETERMINISTIC_SOURCE_NAME,
    )

    return DETERMINISTIC_SOURCE_NAME


def configured_source_name() -> str:
    """The one production configured command-model worker source (Task 8)."""
    from agentic_debugger.application.configured_source import (
        CONFIGURED_SOURCE_NAME,
    )

    return CONFIGURED_SOURCE_NAME


def default_history_root() -> Path:
    """The application-owned run root (``%LOCALAPPDATA%/AgenticDebugger``)."""
    return application_default_history_root()


def repository_root() -> Path:
    """The repository root owning the installed package."""
    import agentic_debugger

    return Path(agentic_debugger.__file__).resolve().parent.parent


def make_session_id() -> str:
    """One validated application session id (``sess-<utc>-<rand>``)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"sess-{stamp}-{secrets.token_hex(3)}"


def curated_task_ids(app) -> Tuple[str, ...]:
    """The canonical deterministic-session task catalog.

    Discovery comes from the live curated fixture directory; only tasks
    the accepted deterministic demo source actually has a scenario for
    are offered (starting any other fixture would fail at scenario
    resolution).  This is the repository's own catalog, never a second
    list.
    """
    from agentic_debugger.demo.catalog import scenario_ids
    from agentic_debugger.demo.runner import curated_task_ids

    supported = set(scenario_ids())
    return tuple(
        task_id
        for task_id in curated_task_ids(app._repository_root)
        if task_id in supported
    )


def curated_task_options(app) -> Tuple[Tuple[str, str], ...]:
    """All accepted session tasks exposed by the product picker.

    Repository-native curated tasks come first so a fresh installation
    opens on a provider-free, runnable workflow.  Research capability
    rungs follow in their frozen relative order and remain available when
    their source-checkout operator is present.
    """

    ladder = ladder_task_options()
    ladder_ids = {task_id for _, task_id in ladder}
    curated = tuple(
        task_display_option(task_id, app._repository_root)
        for task_id in app.curated_task_ids()
        if task_id not in ladder_ids
    )
    return curated + ladder
