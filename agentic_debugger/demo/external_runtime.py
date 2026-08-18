"""General external-task runtime helpers.

These helpers are not SWE-rebench-specific and do not consult gold patches,
hidden FAIL_TO_PASS identities, or catalog RuntimeProbes. Curated single-module
contracts stay in ``task_target_module_path``.
"""

from __future__ import annotations

import ast
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.runtime.workspace import TaskWorkspace

_TEST_DIR_NAMES = frozenset({"tests", "test"})
_DENIED_BASENAMES = frozenset({"task.json"})


class PublicRuntimeClassification(str, Enum):
    """Classification of a model-selected public pytest target.

    This vocabulary is deliberately separate from the scientific verifier
    outcome.  In particular, dependency/import failures are not bug
    reproduction and must never open the debugger gate.
    """

    TARGET_FAILED = "target_test_failed"
    TARGET_PASSED = "target_test_passed"
    TARGET_INVALID = "collection_or_target_invalid"
    DEPENDENCY_FAILURE = "dependency_or_environment_failure"
    TIMEOUT = "timeout"


def classify_public_runtime_result(result: Any) -> PublicRuntimeClassification:
    """Classify one public pytest command without using hidden test identity."""

    if getattr(result, "timed_out", False):
        return PublicRuntimeClassification.TIMEOUT
    if getattr(result, "launch_error", False):
        return PublicRuntimeClassification.DEPENDENCY_FAILURE
    command = getattr(result, "command_result", None)
    if command is None or getattr(command, "exit_code", None) is None:
        return PublicRuntimeClassification.DEPENDENCY_FAILURE
    if getattr(result, "passed", False):
        return PublicRuntimeClassification.TARGET_PASSED
    output = (
        str(getattr(command, "stdout", ""))
        + "\n"
        + str(getattr(command, "stderr", ""))
    ).lower()
    dependency_markers = (
        "modulenotfounderror",
        "importerror",
        "no module named",
        "could not import",
        "error importing plugin",
        "internalerror",
        "permission denied",
    )
    invalid_markers = (
        "file or directory not found",
        "no tests ran",
        "unrecognized arguments",
        "usage: pytest",
        "collection error",
        "error collecting",
    )
    if any(marker in output for marker in dependency_markers):
        return PublicRuntimeClassification.DEPENDENCY_FAILURE
    if any(marker in output for marker in invalid_markers):
        return PublicRuntimeClassification.TARGET_INVALID
    return PublicRuntimeClassification.TARGET_FAILED


def is_external_isolated_task(task: DebugTask) -> bool:
    return (
        task.source is not None
        and task.source.kind == "external"
        and task.evaluation_isolation is not None
        and task.evaluation_isolation.hide_test_identities_from_model
    )


def normalize_relpath(path: str) -> str:
    text = path.replace("\\", "/").strip()
    if not text or text.startswith("/") or text.startswith("..") or ".." in text.split("/"):
        raise ValueError(f"unsafe relative path: {path!r}")
    return text


def _looks_like_test_path(path: str) -> bool:
    parts = normalize_relpath(path).split("/")
    if any(part in _TEST_DIR_NAMES for part in parts):
        return True
    name = parts[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def production_path_prefixes(task: DebugTask) -> tuple[str, ...]:
    prefixes: list[str] = []
    for item in task.constraints.allowed_write_paths:
        cleaned = item.replace("\\", "/").rstrip("/")
        if cleaned and cleaned not in {"tests", "test", "task.json"}:
            prefixes.append(cleaned)
    return tuple(prefixes)


def is_production_workspace_path(
    path: str, prefixes: Sequence[str]
) -> bool:
    rel = normalize_relpath(path)
    if rel in _DENIED_BASENAMES or _looks_like_test_path(rel):
        return False
    if not prefixes:
        return rel.endswith(".py")
    for prefix in prefixes:
        if rel == prefix or rel.startswith(prefix + "/") or rel.endswith("/" + prefix) or rel.endswith(prefix):
            return True
    return False


def validate_public_runtime_target(
    workspace: TaskWorkspace,
    target: str,
    *,
    hidden_identities: Iterable[str] = (),
) -> str:
    """Accept a model-selected public pytest path/node that already exists."""

    rel = normalize_relpath(target)
    for hidden in hidden_identities:
        if hidden and (rel == hidden or rel in hidden or hidden in rel):
            raise ValueError("public runtime target must not name a hidden verifier test")
    if rel.endswith("task.json"):
        raise ValueError("public runtime target cannot be task.json")
    node, _, _ = rel.partition("::")
    resolved = Path(workspace.root) / node
    if not resolved.exists():
        raise ValueError(f"public runtime target does not exist: {rel}")
    return rel


def validate_model_selected_pdb_target(
    workspace: TaskWorkspace,
    path: str,
    breakpoint_line: int,
    *,
    prefixes: Sequence[str],
    symbol: Optional[str] = None,
    hidden_identities: Iterable[str] = (),
) -> tuple[str, int, Optional[str]]:
    """Fail-closed validation of a model-selected production PDB target."""

    if type(breakpoint_line) is not int or breakpoint_line < 1:
        raise ValueError("breakpoint_line must be a positive integer")
    rel = normalize_relpath(path)
    for hidden in hidden_identities:
        if hidden and hidden in rel:
            raise ValueError("PDB target must not use a hidden verifier identity")
    if not rel.endswith(".py"):
        raise ValueError("PDB target must be a Python file")
    if not is_production_workspace_path(rel, prefixes):
        raise ValueError("PDB target is not a production workspace path")
    resolved = Path(workspace.root) / rel
    if not resolved.is_file():
        raise ValueError(f"PDB target does not exist: {rel}")
    source = resolved.read_text(encoding="utf-8")
    lines = source.splitlines()
    if breakpoint_line > len(lines):
        raise ValueError("breakpoint_line is outside the selected file")
    resolved_symbol = symbol
    if symbol:
        tree = ast.parse(source)
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        if symbol not in names:
            raise ValueError(f"symbol {symbol!r} is not defined in {rel}")
        resolved_symbol = symbol
    return rel, breakpoint_line, resolved_symbol
