"""PDB probe preparation for the deterministic demonstration.

This module owns the disposable debugger-target preparation: the
immutable :class:`PdbProbe` record, the provider-safe opaque workspace
identity, and :func:`prepare_pdb_probe` (copy the canonical fixture,
append the probe driver or the exact public pytest driver, and record
the fixed breakpoint and production-file identity).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from agentic_debugger.demo.catalog import (
    DemoScenario,
    exact_pytest_driver_source,
    probe_driver_source,
    resolve_probe_breakpoint,
)
from agentic_debugger.demo.diagnostics import DemoToolError
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.runtime.workspace import TaskWorkspace

@dataclass(frozen=True)
class PdbProbe:
    """A prepared, disposable debugger target derived from the fixture."""

    source_dir: Path
    parent_dir: Path
    script: str
    breakpoint_line: int
    focus_function: str
    exact_public_reproduction: bool = False
    reproduction_argv: tuple[str, ...] = ()
    reproduction_node: str = ""
    workspace_id: str = ""
    production_file_sha256: str = ""


def opaque_workspace_id(workspace: TaskWorkspace) -> str:
    """Return a provider-safe identity derived from the actual workspace root."""

    return hashlib.sha256(
        str(Path(workspace.root).resolve()).encode("utf-8")
    ).hexdigest()[:24]


def prepare_pdb_probe(
    fixture_dir: Path,
    scenario: DemoScenario,
    parent_dir: Path,
    *,
    model_selects_breakpoint: bool = False,
    task: Optional[DebugTask] = None,
    model_visible_task_mapping: Optional[Mapping[str, Any]] = None,
) -> PdbProbe:
    """Copy the canonical fixture and append one module-level probe driver.

    The canonical fixture is never written to.  The copy receives a single
    appended driver function plus its call so the focus function actually runs
    under the debugger.  The accepted demo resolves its fixed breakpoint from
    the fixture AST.  The tuned-debugger pilot can instead leave the stored
    breakpoint unset (0) so the live model must supply ``breakpoint_line``.
    """

    probe = scenario.runtime_probe
    source_dir = parent_dir / f"probe-{scenario.task_id}"
    if source_dir.exists():
        raise DemoToolError(f"probe source directory already exists: {source_dir}")
    shutil.copytree(fixture_dir, source_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    module = source_dir / probe.module_path
    if not module.is_file():
        raise DemoToolError(f"probe module is missing from the fixture: {probe.module_path}")
    original = module.read_text(encoding="utf-8")
    breakpoint_line = (
        0 if model_selects_breakpoint else resolve_probe_breakpoint(original, probe)
    )
    exact = probe.exact_public_reproduction
    if exact and task is not None:
        # The probe workspace is provider-visible execution state.  Keep the
        # evaluator oracle and fixed revision out of it while retaining the
        # public task contract needed by tooling.
        visible_task = (
            model_visible_task_mapping
            if model_visible_task_mapping is not None
            else task.agent_visible_mapping()
        )
        (source_dir / "task.json").write_text(
            json.dumps(visible_task, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    reproduction_argv: tuple[str, ...] = ()
    reproduction_node = ""
    if exact:
        if task is None:
            raise DemoToolError("exact PDB probe requires the loaded task")
        reproduction_argv = tuple(task.reproduction.argv)
        nodes = tuple(task.tests.fail_to_pass)
        if len(nodes) != 1:
            raise DemoToolError("exact PDB proof requires one public failing pytest node")
        reproduction_node = nodes[0]
        marker = ("-m", "pytest")
        try:
            marker_index = next(
                index for index in range(len(reproduction_argv) - 1)
                if reproduction_argv[index:index + 2] == marker
            )
        except StopIteration as exc:
            raise DemoToolError("exact PDB reproduction must use python -m pytest") from exc
        pytest_args = reproduction_argv[marker_index + 2:]
        if reproduction_node not in pytest_args:
            raise DemoToolError("exact PDB reproduction argv does not name the public failing node")
        driver = exact_pytest_driver_source(probe, tuple(pytest_args))
    else:
        driver = probe_driver_source(probe)
    module.write_text(original + driver, encoding="utf-8", newline="\n")
    production_file_sha256 = hashlib.sha256(module.read_bytes()).hexdigest()
    workspace_id = hashlib.sha256(str(source_dir.resolve()).encode("utf-8")).hexdigest()[:24]
    return PdbProbe(
        source_dir=source_dir,
        parent_dir=parent_dir,
        script=probe.module_path,
        breakpoint_line=breakpoint_line,
        focus_function=probe.focus_function,
        exact_public_reproduction=exact,
        reproduction_argv=reproduction_argv,
        reproduction_node=reproduction_node,
        workspace_id=workspace_id,
        production_file_sha256=production_file_sha256,
    )
