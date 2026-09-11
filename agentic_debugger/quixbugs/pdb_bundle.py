"""Minimal PDB runtime-bundle materialization and the contained session.

This module owns the runtime-infrastructure layer of the contained PDB
architecture: the minimal ``pdb``-only runtime module closure with
byte/provenance checks (``_PDB_RUNTIME_MODULES``,
``materialize_pdb_runtime_bundle``), the contained worker argv
construction, and ``ContainedPdbSession`` with its contained
worker-executable identity and session overrides.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerRunResult,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisConfidence,
    HypothesisLedger,
    PdbGateContext,
    PdbGateDecision,
    PdbPolicy,
    decide_pdb_access,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    AddHypothesisDirective,
    ControllerSnapshot,
    ModelAdapterError,
    ModelDirective,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.bugsinpy.adapter import ExternalWorkspace
from agentic_debugger.bugsinpy.wsl import (
    DISTRO,
    ResourceLimits,
    WslBubblewrapRunner,
    build_bwrap_command,
    build_env_wrapped_command,
    build_linux_timeout_argv,
    build_prlimit_argv,
    build_wsl_command,
    to_wsl_path,
)
from agentic_debugger.demo.catalog import DemoCatalogError, RuntimeProbe, probe_driver_source, resolve_probe_breakpoint
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.tools import DemoToolContext, PdbProbe, build_registry
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.events.schema import RunEvent
from agentic_debugger.evaluation.runner import bounded_error
from agentic_debugger.evaluation.task_schema import DebugTask, TaskSource
from agentic_debugger.quixbugs.adapter import (
    QuixBugsAdapter,
    QuixBugsPreflightFacts,
    QuixBugsSourceAcquirer,
    QuixPreflightReport,
)
from agentic_debugger.runtime.exceptions import PdbSessionError
from agentic_debugger.runtime.execution import PdbLaunchPlan, VerifiedExecutionContext
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.workspace import TaskWorkspace

class ContainedPdbError(RuntimeError):
    """A contained-PDB precondition, identity check, or invariant is unmet."""


#: The historical default reachability scope remains exactly one pinned
#: QuixBugs task; the task-local probe path below is what lets a caller bind
#: PDB-on-uncertainty to another selected task with an explicit reviewed probe.
QUIXBUGS_PDB_TASK_ID = "quixbugs-gcd-smoke-v1"
QUIXBUGS_PDB_POLICY = DemoPolicy.PDB_ON_UNCERTAINTY
QUIXBUGS_PDB_REPETITIONS = 1

#: Small explicit PDB observation budget: exactly enough for one bounded stack
#: observation and one bounded frame-locals observation, plus one spare.
QUIXBUGS_PDB_OBSERVATION_BUDGET = 3

_DEFAULT_SESSION_WALL_CLOCK_SECONDS = 60.0
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 10.0

#: Exact transitive closure of the worker's in-package runtime imports.
#: ``pdb_worker.py`` is the bootstrap/dispatch facade; the remaining
#: ``pdb_worker_*`` modules are its focused responsibilities (limits, paths,
#: values, frames, safeeval, postmortem, runners, execution, inspection,
#: lifecycle). Every entry is required to launch the worker; the bundle test
#: proves a missing entry fails the import deterministically.
_PDB_RUNTIME_MODULES = (
    "pdb_worker.py",
    "pdb_worker_execution.py",
    "pdb_worker_inspection.py",
    "pdb_worker_lifecycle.py",
    "pdb_worker_limits.py",
    "pdb_worker_paths.py",
    "pdb_worker_values.py",
    "pdb_worker_frames.py",
    "pdb_worker_safeeval.py",
    "pdb_worker_postmortem.py",
    "pdb_worker_runners.py",
    "pdb_protocol.py",
    "exceptions.py",
)

#: The reviewed runtime probe for the pinned buggy python_programs/gcd.py.
#: ``call_source`` is drawn from the docstring example already present in the
#: buggy file itself (``gcd(35, 21) -> 7``), not from the manifest oracle.
#: This object is also the historical default probe: the standalone GCD entry
#: points keep their default GCD lock, and the shared identity validator
#: rejects using this default probe for any other selected task.
QUIXBUGS_GCD_RUNTIME_PROBE = RuntimeProbe(
    module_path="python_programs/gcd.py",
    focus_function="gcd",
    call_source="gcd(35, 21)",
    anchor="return gcd(",
    inspect_expressions=("a", "b"),
)

_GCD_RUNTIME_PROBE = QUIXBUGS_GCD_RUNTIME_PROBE


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def materialize_pdb_runtime_bundle(destination: Path) -> dict[str, str]:
    """Copy the minimal, pure-stdlib PDB worker code into ``destination``.

    Only the exact files the worker needs to run are copied
    (``agentic_debugger/__init__.py``, the ``pdb_worker*`` facade plus its
    focused responsibility modules, ``pdb_protocol.py``, and
    ``exceptions.py`` — see ``_PDB_RUNTIME_MODULES`` for the exact
    transitive closure),
    byte-identical to the accepted repository copies (hashes are returned for
    provenance evidence). ``agentic_debugger/runtime/__init__.py`` is
    deliberately written as an empty stub rather than copied verbatim: the
    real one imports unrelated runtime modules (workspace, patcher, test
    runner, ...) that the contained worker never needs and that are not
    present in this bundle.
    """

    if destination.exists():
        raise ContainedPdbError(f"pdb-runtime bundle destination already exists: {destination}")
    import agentic_debugger

    pkg_dir = Path(agentic_debugger.__file__).resolve().parent
    package_dir = destination / "agentic_debugger"
    runtime_dir = package_dir / "runtime"
    runtime_dir.mkdir(parents=True)

    hashes: dict[str, str] = {}
    shutil.copy2(pkg_dir / "__init__.py", package_dir / "__init__.py")
    hashes["agentic_debugger/__init__.py"] = _sha256_file(package_dir / "__init__.py")

    stub = runtime_dir / "__init__.py"
    stub.write_text("", encoding="utf-8")
    hashes["agentic_debugger/runtime/__init__.py"] = _sha256_file(stub)

    for name in _PDB_RUNTIME_MODULES:
        source = pkg_dir / "runtime" / name
        target = runtime_dir / name
        shutil.copy2(source, target)
        relative = f"agentic_debugger/runtime/{name}"
        source_hash = _sha256_file(source)
        target_hash = _sha256_file(target)
        if source_hash != target_hash:
            raise ContainedPdbError(f"pdb-runtime bundle copy of {relative} does not match the source byte-for-byte")
        hashes[relative] = target_hash
    return hashes


def build_contained_pdb_worker_argv(
    *,
    runner: WslBubblewrapRunner,
    workspace_host: str,
    pdb_runtime_root_posix: str,
    resource_limits: ResourceLimits,
    session_timeout_seconds: float,
    distro: str = DISTRO,
) -> list[str]:
    """Build the full ``wsl.exe``/Bubblewrap argv for a persistent PDB worker.

    Composes the exact existing containment primitives (`prlimit` CPU/memory/
    process-count caps, the accepted Bubblewrap policy with one extra
    read-only bind for the pdb-runtime bundle, the Linux-side ``timeout``
    wall-clock backstop, and the reviewed environment allowlist) the same way
    :meth:`WslBubblewrapRunner.run` does for one-shot commands -- only the
    execution model (persistent ``Popen`` with pipes instead of a
    run-to-completion call) differs.
    """

    if not isinstance(runner, WslBubblewrapRunner) or runner.resource_isolation_ready is not True:
        raise ContainedPdbError("contained PDB worker launch requires an open, non-forgeable resource-isolation gate")
    if not isinstance(resource_limits, ResourceLimits):
        raise ContainedPdbError("resource_limits must be a validated ResourceLimits profile")
    expected_limits = dict(runner.boundary_guarantee.get("resource_limits", {}))
    for field_name, value in (
        ("cpu_seconds", resource_limits.cpu_seconds),
        ("memory_bytes", resource_limits.memory_bytes),
        ("max_processes", resource_limits.max_processes),
    ):
        if expected_limits.get(field_name) != f"prlimit-enforced:{value}":
            raise ContainedPdbError("resource_limits does not match the runner's actually-open resource-isolation gate")
    if not isinstance(session_timeout_seconds, (int, float)) or isinstance(session_timeout_seconds, bool) or session_timeout_seconds <= 0:
        raise ContainedPdbError("session_timeout_seconds must be a positive number")

    workspace_posix = to_wsl_path(workspace_host, distro)
    bootstrap = (
        "import sys; import runpy; "
        "sys.path.insert(0, '/opt/pdb_runtime'); "
        "runpy.run_module('agentic_debugger.runtime.pdb_worker', run_name='__main__')"
    )
    inner = ["/opt/python/bin/python", "-I", "-u", "-c", bootstrap]
    limited = build_prlimit_argv(
        inner,
        cpu_seconds=resource_limits.cpu_seconds,
        memory_bytes=resource_limits.memory_bytes,
        max_processes=resource_limits.max_processes,
    )
    bwrap_cmd = build_bwrap_command(
        limited,
        workspace=workspace_posix,
        python_root=runner.python_root_posix,
        empty_dir=runner.empty_dir_posix,
        extra_ro_binds=((pdb_runtime_root_posix, "/opt/pdb_runtime"),),
    )
    timed = build_linux_timeout_argv(bwrap_cmd, timeout_seconds=session_timeout_seconds)
    env_wrapped = build_env_wrapped_command(timed)
    return build_wsl_command(env_wrapped, distro=distro)


class ContainedPdbSession(PdbSession):
    """A :class:`PdbSession` whose worker runs inside the verified WSL/Bubblewrap boundary.

    Overrides only the two extension points needed to relocate the worker
    process: ``_get_worker_argv`` (what to launch) and ``_worker_cwd`` (the
    Windows-side ``Popen`` cwd, which cannot be the WSL UNC workspace path --
    the worker's real working directory is controlled entirely by
    Bubblewrap's ``--chdir /workspace``). Everything else -- handshake,
    request/response validation, bounded diagnostics, stack/locals/safe-eval
    result bounding, and shutdown -- is the unmodified accepted implementation.
    """

    def __init__(
        self,
        workspace: TaskWorkspace,
        *,
        runner: WslBubblewrapRunner,
        pdb_runtime_root_posix: str,
        resource_limits: ResourceLimits,
        session_timeout_seconds: float = _DEFAULT_SESSION_WALL_CLOCK_SECONDS,
        startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            workspace,
            startup_timeout=startup_timeout,
            request_timeout=request_timeout,
            shutdown_timeout=shutdown_timeout,
        )
        self._runner = runner
        self._pdb_runtime_root_posix = pdb_runtime_root_posix
        self._resource_limits = resource_limits
        self._session_timeout_seconds = session_timeout_seconds

    def _worker_cwd(self) -> str:
        return tempfile.gettempdir()

    def _worker_env(self) -> None:
        # The worker is reached through the ``wsl.exe`` bridge into a
        # different OS's PID namespace; the Windows venv launcher identity
        # must never leak into that bridge environment.
        return None

    def _expected_worker_pid(self) -> Optional[int]:
        # ``self._proc.pid`` is the Windows process ID of the spawned
        # ``wsl.exe`` bridge; the worker reports its own PID from inside a
        # freshly unshared Linux PID namespace two process boundaries away.
        # These can never be numerically equal, host-local vs. WSL is not a
        # namespacing detail here, it is a different OS's PID space. The
        # equivalent confused-deputy defense for this path is structural: the
        # pipe pair is private to the exact ``subprocess.Popen`` we just
        # created, so nothing else can be answering on it. Handshake still
        # requires a matching protocol version and a live process.
        return None

    def _get_worker_argv(self) -> list[str]:
        try:
            return build_contained_pdb_worker_argv(
                runner=self._runner,
                workspace_host=self._workspace.root,
                pdb_runtime_root_posix=self._pdb_runtime_root_posix,
                resource_limits=self._resource_limits,
                session_timeout_seconds=self._session_timeout_seconds,
                distro=self._runner.process.distro,
            )
        except ContainedPdbError as exc:
            raise PdbSessionError(str(exc)) from exc


def _resolve_probe_breakpoint_checked(source_text: str, runtime_probe: RuntimeProbe) -> int:
    """Resolve the probe anchor, converting the catalog's typed anchor error
    into a :class:`ContainedPdbError`.

    ``resolve_probe_breakpoint`` raises :class:`DemoCatalogError` for an
    unresolvable or ambiguous anchor; the contained boundary converts exactly
    that catalog error (never unrelated exceptions) so callers such as the
    live-case path can expose one consistent typed configuration error.
    """
    try:
        return resolve_probe_breakpoint(source_text, runtime_probe)
    except DemoCatalogError as exc:
        raise ContainedPdbError(str(exc)) from exc


def prepare_quixbugs_pdb_probe(project_root: Path, parent_dir: Path, runtime_probe: RuntimeProbe) -> PdbProbe:
    """Copy one pinned QuixBugs checkout and append a reviewed probe driver.

    Mirrors :func:`agentic_debugger.demo.tools.prepare_pdb_probe` exactly: the
    pinned checkout is never written to; a disposable copy receives one
    appended driver call, and the breakpoint is resolved from the buggy
    source's own AST via :func:`resolve_probe_breakpoint` -- never from a
    hard-coded line number and never from the manifest's oracle fields.
    """

    workspace = TaskWorkspace(str(project_root), parent_dir=str(parent_dir))
    module = Path(workspace.root) / runtime_probe.module_path
    if not module.is_file():
        raise ContainedPdbError(f"probe module is missing from the pinned checkout copy: {runtime_probe.module_path}")
    original = module.read_text(encoding="utf-8")
    breakpoint_line = _resolve_probe_breakpoint_checked(original, runtime_probe)
    module.write_text(original + probe_driver_source(runtime_probe), encoding="utf-8", newline="\n")
    return PdbProbe(
        source_dir=Path(workspace.root),
        parent_dir=parent_dir,
        script=runtime_probe.module_path,
        breakpoint_line=breakpoint_line,
        focus_function=runtime_probe.focus_function,
    )


def prepare_quixbugs_gcd_pdb_probe(project_root: Path, parent_dir: Path) -> PdbProbe:
    """Backward-compatible wrapper for the accepted gcd reachability case."""

    return prepare_quixbugs_pdb_probe(project_root, parent_dir, _GCD_RUNTIME_PROBE)
