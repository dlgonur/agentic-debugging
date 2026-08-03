"""Contained PDB runtime path for pinned QuixBugs Python tasks.

This module adds the smallest coherent path that lets the already-accepted
controller, PDB protocol, and QuixBugs infrastructure collect real runtime
(PDB) evidence for the pinned QuixBugs ``gcd`` task, with the PDB worker and
the debug target executing entirely inside the accepted verified
WSL/Bubblewrap boundary (:class:`agentic_debugger.bugsinpy.wsl.WslBubblewrapRunner`).

It is a composition, not a new framework:

* :class:`ContainedPdbSession` is a thin :class:`~agentic_debugger.runtime.pdb_session.PdbSession`
  subclass that only overrides how the worker process is launched (through
  ``wsl.exe``/Bubblewrap instead of a host-local ``subprocess.Popen``); the
  protocol, validation, and lifecycle are entirely the accepted implementation.
* The worker launch argv is built by composing the exact existing
  ``build_bwrap_command``/``build_prlimit_argv``/``build_linux_timeout_argv``/
  ``build_env_wrapped_command``/``build_wsl_command`` helpers already accepted
  for one-shot benchmark commands -- nothing here reimplements containment.
* The gcd runtime probe reuses :mod:`agentic_debugger.demo.catalog`'s
  ``RuntimeProbe``/``resolve_probe_breakpoint``/``probe_driver_source`` and
  :class:`agentic_debugger.demo.tools.PdbProbe` verbatim.
* The controller run reuses :class:`~agentic_debugger.agent.controller.DeterministicController`,
  :func:`~agentic_debugger.agent.controller_policy.decide_pdb_access`,
  :class:`~agentic_debugger.demo.tools.DemoToolContext`, and
  :func:`~agentic_debugger.demo.tools.build_registry` unchanged.

Only :class:`DeterministicPdbReachabilityDriver` is new "model" surface, and it
is a fixed, no-model script -- not a provider call -- that exercises exactly
the reviewed sequence needed to prove reachability. It does not read the
manifest's ``oracle`` fields (no gold patch, no root-cause prose) and does not
produce or verify a patch.
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

_PDB_RUNTIME_MODULES = ("pdb_worker.py", "pdb_protocol.py", "exceptions.py")

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
    (``agentic_debugger/__init__.py``, ``agentic_debugger/runtime/exceptions.py``,
    ``agentic_debugger/runtime/pdb_protocol.py``, ``agentic_debugger/runtime/pdb_worker.py``),
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


class ContainedPdbGateName(str, Enum):
    EXECUTION_CONTEXT_READY = "execution_context_ready"
    RESOURCE_ISOLATION_READY = "resource_isolation_ready"
    CONTAINMENT_READY = "containment_ready"
    LAUNCH_PLAN_IDENTITY = "launch_plan_identity"
    OBSERVATION_BUDGET_POSITIVE = "observation_budget_positive"


class ContainedPdbGateStatus(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ContainedPdbGateResult:
    name: ContainedPdbGateName
    status: ContainedPdbGateStatus
    reason: str

    def to_mapping(self) -> dict[str, str]:
        return {"name": self.name.value, "status": self.status.value, "reason": self.reason}


@dataclass(frozen=True)
class ContainedPdbPreflightReport:
    task_id: str
    gates: tuple[ContainedPdbGateResult, ...]

    @property
    def authorized(self) -> bool:
        return all(gate.status is ContainedPdbGateStatus.PASS for gate in self.gates)

    @property
    def blocked_gates(self) -> tuple[str, ...]:
        return tuple(gate.name.value for gate in self.gates if gate.status is not ContainedPdbGateStatus.PASS)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "authorized": self.authorized,
            "blocked_gates": list(self.blocked_gates),
            "gates": [gate.to_mapping() for gate in self.gates],
        }


def _boolean_gate(name: ContainedPdbGateName, ok: bool, reason: str) -> ContainedPdbGateResult:
    return ContainedPdbGateResult(name, ContainedPdbGateStatus.PASS if ok else ContainedPdbGateStatus.BLOCKED, reason if not ok else "explicitly verified")


def contained_pdb_preflight(
    *,
    task_id: str,
    execution_context: Optional[VerifiedExecutionContext],
    external_parent: Optional[str],
    repository_root: str,
    launch_plan: Optional[PdbLaunchPlan],
    expected_python_executable: Optional[str],
    expected_cwd: Optional[str],
    expected_target: Optional[str],
    expected_breakpoints: Optional[tuple[int, ...]],
    pdb_observation_budget: int,
) -> ContainedPdbPreflightReport:
    """Fail-closed gate for everything the accepted QuixBugs preflight does not
    already cover: a verified, resource-isolation-ready execution context, an
    owned containment relationship, an exact-identity-matched launch plan, and
    a positive observation budget. Every condition is derived from real
    objects (the runner's own non-forgeable ``resource_isolation_ready``
    attribute and ``boundary_guarantee``, the actual ``PdbLaunchPlan`` fields);
    none of it is a caller-supplied boolean.
    """

    context = execution_context
    execution_ready = context is not None
    gates = [_boolean_gate(ContainedPdbGateName.EXECUTION_CONTEXT_READY, execution_ready, "a verified execution context is required")]

    resource_ready = (
        execution_ready
        and getattr(context.runner, "resource_isolation_ready", False) is True
        and dict(getattr(context.runner, "boundary_guarantee", {})) == context.containment.to_mapping()
    )
    gates.append(_boolean_gate(ContainedPdbGateName.RESOURCE_ISOLATION_READY, resource_ready, "resource-isolation gate is not open on the supplied runner"))

    parent = Path(external_parent).resolve() if external_parent else None
    root = Path(context.containment.root).resolve() if execution_ready else None
    repository = Path(repository_root).resolve()
    containment_ready = (
        execution_ready
        and parent is not None
        and root is not None
        and root != root.parent
        and _is_within(parent, root)
        and not _is_within(repository, root)
    )
    gates.append(_boolean_gate(ContainedPdbGateName.CONTAINMENT_READY, containment_ready, "external parent/containment-root relationship is not verified"))

    plan_ok = (
        launch_plan is not None
        and expected_python_executable is not None
        and expected_cwd is not None
        and expected_target is not None
        and expected_breakpoints is not None
        and launch_plan.python_executable == expected_python_executable
        and launch_plan.cwd == expected_cwd
        and launch_plan.target == expected_target
        and launch_plan.driver == expected_target
        and launch_plan.breakpoints == expected_breakpoints
        and execution_ready
        and dict(launch_plan.environment) == dict(context.environment.environment)
    )
    gates.append(_boolean_gate(ContainedPdbGateName.LAUNCH_PLAN_IDENTITY, plan_ok, "PDB launch plan does not match the reviewed task/target/breakpoint/environment identity"))

    budget_ok = type(pdb_observation_budget) is int and pdb_observation_budget > 0
    gates.append(_boolean_gate(ContainedPdbGateName.OBSERVATION_BUDGET_POSITIVE, budget_ok, "PDB observation budget must be a positive int"))

    return ContainedPdbPreflightReport(task_id, tuple(gates))


@dataclass
class DeterministicPdbReachabilityDriver:
    """Fixed, no-model directive script that exercises exactly one reviewed
    PDB reachability sequence: reproduce -> low-confidence runtime-evidence
    hypothesis -> real controller PDB gate check -> start session -> one
    bounded stack observation -> one bounded frame-locals observation -> stop
    session -> intentional early termination (no patch is required by scope).

    This is shaped like a :class:`~agentic_debugger.agent.model_adapter.ModelAdapter`
    (``next_directive(snapshot) -> ModelDirective``) so it can drive the real
    :class:`~agentic_debugger.agent.controller.DeterministicController`
    unchanged, but it never contacts a model or provider: every directive is
    fixed ahead of time, and the only "decision" it makes is calling the real
    :func:`decide_pdb_access` gate and refusing to proceed if it denies access.
    """

    hypothesis_id: str
    hypothesis_statement: str
    gate_policy: PdbPolicy = PdbPolicy.ON_UNCERTAINTY
    model_name: str = "deterministic-pdb-reachability-v1"
    _failure_reproduced: bool = field(default=False, init=False, repr=False)
    _pause_generation: Optional[int] = field(default=None, init=False, repr=False)
    _runtime_cursor: int = field(default=0, init=False, repr=False)
    gate_decisions: list[PdbGateDecision] = field(default_factory=list, init=False, repr=False)

    def _pdb_gate(self, snapshot: ControllerSnapshot) -> PdbGateDecision:
        active = snapshot.hypotheses.active_hypotheses()
        return decide_pdb_access(
            self.gate_policy,
            PdbGateContext(
                source_state=ControllerState.UNDERSTAND,
                failure_reproduced=self._failure_reproduced,
                remaining_pdb_observations=max(0, snapshot.budget_limits.max_pdb_observations - snapshot.budget_state.pdb_observations),
                failed_patch_attempts=snapshot.budget_state.patch_attempts,
                active_hypothesis=active[0] if active else None,
            ),
        )

    def _observe(self, snapshot: ControllerSnapshot) -> None:
        observation = snapshot.last_observation
        if observation is None or observation.status.value != "ok":
            return
        if observation.name == ActionName.RUN_REPRODUCTION.value and observation.payload.get("phase") == "baseline":
            self._failure_reproduced = bool(observation.payload.get("failure_reproduced"))
        elif observation.name == ActionName.GET_STACK_SUMMARY.value:
            generation = observation.payload.get("pause_generation")
            if type(generation) is int:
                self._pause_generation = generation

    def next_directive(self, snapshot: ControllerSnapshot) -> ModelDirective:
        self._observe(snapshot)
        state = snapshot.state

        if state is ControllerState.REPRODUCE:
            if snapshot.model_call_index == 0:
                return ActionDirective(ActionName.RUN_REPRODUCTION, {"phase": "baseline"})
            return TransitionDirective(
                ControllerState.UNDERSTAND,
                "baseline failure reproduced through the contained external runner",
            )

        if state is ControllerState.UNDERSTAND:
            if not snapshot.hypotheses.active_hypotheses():
                return AddHypothesisDirective(
                    self.hypothesis_id, self.hypothesis_statement, HypothesisConfidence.LOW, (), True,
                )
            decision = self._pdb_gate(snapshot)
            self.gate_decisions.append(decision)
            if not decision.allowed:
                raise ModelAdapterError(f"controller PDB gate denied runtime-evidence access: {decision.reason.value}")
            return TransitionDirective(
                ControllerState.RUNTIME_EVIDENCE,
                f"controller PDB gate allowed access ({decision.reason.value}); collecting bounded runtime evidence",
            )

        if state is ControllerState.RUNTIME_EVIDENCE:
            if self._runtime_cursor == 0:
                self._runtime_cursor = 1
                return ActionDirective(ActionName.START_PDB_SESSION, {})
            if self._runtime_cursor == 1:
                self._runtime_cursor = 2
                return ActionDirective(ActionName.GET_STACK_SUMMARY, {})
            if self._runtime_cursor == 2:
                if self._pause_generation is None:
                    raise ModelAdapterError("stack summary did not report a pause generation")
                self._runtime_cursor = 3
                return ActionDirective(
                    ActionName.GET_FRAME_LOCALS, {"frame_id": 0, "pause_generation": self._pause_generation},
                )
            if self._runtime_cursor == 3:
                self._runtime_cursor = 4
                return ActionDirective(ActionName.STOP_PDB_SESSION, {})
            return TransitionDirective(
                ControllerState.FAILED,
                "deterministic reachability case complete; patch verification is out of scope for this infrastructure task",
            )

        raise ModelAdapterError(f"deterministic reachability driver has no scripted step for state {state.value!r}")


def _project_events(result: ControllerRunResult, *, tool_version: str) -> str:
    stream = io.StringIO()
    logger = JsonlEventLogger(result.run_id, result.task_id, stream=stream)
    try:
        for event in project_controller_run(
            result, tool_version=tool_version, model=None,
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), duration_ms=None,
        ):
            logger.append(RunEvent.from_mapping(event.to_mapping()))
        logger.flush()
        return stream.getvalue()
    finally:
        logger.close()


def _pdb_observation_counts(result: ControllerRunResult) -> dict[str, int]:
    """Aggregate observation counts, retained for reporting only.

    This is deliberately never the sole (or even primary) basis for the
    PASSED verdict -- see :func:`evaluate_reachability_sequence_from_events`,
    which requires the exact named actions to have succeeded, in order, with
    the expected payload identity at each step.
    """
    observed_names = {ActionName.GET_STACK_SUMMARY.value, ActionName.GET_FRAME_LOCALS.value, ActionName.SAFE_EVAL_EXPRESSION.value}
    successful = 0
    failed = 0
    for step in result.steps:
        if step.action is None or step.action.name not in observed_names or step.observation is None:
            continue
        if step.observation.status.value == "ok":
            successful += 1
        else:
            failed += 1
    return {"successful_pdb_observation_count": successful, "failed_pdb_observation_count": failed}


#: Required for a valid, complete event trail: one event of each name below
#: must be present in the serialized ``events_jsonl``, independent of the
#: structural ``result.steps`` check (this validates that projection/
#: serialization itself actually produced complete, parseable evidence).
_REQUIRED_EVENT_NAMES = frozenset({
    "run_reproduction", "start_pdb_session", "get_stack_summary",
    "get_frame_locals", "stop_pdb_session", "run_finished",
})
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _after(index: Optional[int]) -> int:
    return -1 if index is None else index


def _find_event_index(events: tuple[RunEvent, ...], *, event_type: str, name: str, after: Optional[int] = None) -> Optional[int]:
    """Index of the first recorded event of ``event_type``/``name`` strictly after ``after``."""
    start = _after(after)
    for index, event in enumerate(events):
        if index <= start:
            continue
        if event.event_type.value == event_type and event.name == name:
            return index
    return None


@dataclass(frozen=True)
class ReachabilitySequenceEvidence:
    """Structural, per-event evidence that the exact reviewed action sequence
    (reproduce -> start -> stack -> locals -> stop -> intentional terminal
    transition) succeeded in order, not merely an aggregate observation
    count.

    Evaluated directly against the recorded, parsed ``RunEvent`` trail -- the
    same artifact that is actually persisted and reported -- rather than the
    in-memory ``ControllerRunResult``, so the identical check can be replayed
    offline against a previously captured ``events_jsonl`` (see
    ``scripts/quixbugs_gcd_pdb_reachability_offline_revalidation.py``).
    """

    ok: bool
    reasons: tuple[str, ...]
    steps: dict[str, Any]

    def to_mapping(self) -> dict[str, Any]:
        return {"ok": self.ok, "reasons": list(self.reasons), "steps": self.steps}


def evaluate_reachability_sequence_from_events(
    events: tuple[RunEvent, ...], *, expected_script: str, expected_function: str, expected_breakpoint_line: int,
) -> ReachabilitySequenceEvidence:
    reasons: list[str] = []
    evidence: dict[str, Any] = {}

    reproduction_index = _find_event_index(events, event_type="observation", name=ActionName.RUN_REPRODUCTION.value)
    if reproduction_index is None:
        reasons.append("no run_reproduction observation event was recorded")
    else:
        observation = events[reproduction_index].payload.get("observation", {})
        payload = observation.get("payload", {}) if isinstance(observation, dict) else {}
        evidence["run_reproduction"] = payload
        ok = (
            isinstance(observation, dict) and observation.get("status") == "ok"
            and payload.get("phase") == "baseline"
            and payload.get("failure_reproduced") is True
        )
        if not ok:
            reasons.append("baseline run_reproduction did not succeed with failure_reproduced=true")

    start_index = _find_event_index(events, event_type="observation", name=ActionName.START_PDB_SESSION.value, after=reproduction_index)
    if start_index is None:
        reasons.append("no start_pdb_session observation event was recorded after baseline reproduction")
    else:
        observation = events[start_index].payload.get("observation", {})
        payload = observation.get("payload", {}) if isinstance(observation, dict) else {}
        evidence["start_pdb_session"] = payload
        ok = (
            isinstance(observation, dict) and observation.get("status") == "ok"
            and payload.get("state") == "paused"
            and payload.get("script") == expected_script
            and payload.get("line") == expected_breakpoint_line
            and payload.get("function") == expected_function
        )
        if not ok:
            reasons.append("start_pdb_session did not pause at the reviewed script/breakpoint/function")

    stack_index = _find_event_index(events, event_type="observation", name=ActionName.GET_STACK_SUMMARY.value, after=start_index)
    if start_index is None or stack_index is None:
        reasons.append("no get_stack_summary observation event was recorded after a successful start_pdb_session")
    else:
        observation = events[stack_index].payload.get("observation", {})
        payload = observation.get("payload", {}) if isinstance(observation, dict) else {}
        evidence["get_stack_summary"] = payload
        frames = payload.get("frames")
        ok = (
            isinstance(observation, dict) and observation.get("status") == "ok"
            and isinstance(frames, list) and len(frames) >= 1
            and payload.get("script") == expected_script
        )
        if not ok:
            reasons.append("get_stack_summary did not return a successful bounded stack observation")

    locals_index = _find_event_index(events, event_type="observation", name=ActionName.GET_FRAME_LOCALS.value, after=stack_index)
    if stack_index is None or locals_index is None:
        reasons.append("no get_frame_locals observation event was recorded after a successful get_stack_summary")
    else:
        observation = events[locals_index].payload.get("observation", {})
        payload = observation.get("payload", {}) if isinstance(observation, dict) else {}
        evidence["get_frame_locals"] = payload
        locals_list = payload.get("locals")
        ok = (
            isinstance(observation, dict) and observation.get("status") == "ok"
            and isinstance(locals_list, list) and len(locals_list) >= 1
        )
        if not ok:
            reasons.append("get_frame_locals did not return a successful bounded locals observation")

    stop_index = _find_event_index(events, event_type="observation", name=ActionName.STOP_PDB_SESSION.value, after=locals_index)
    if locals_index is None or stop_index is None:
        reasons.append("no stop_pdb_session observation event was recorded after a successful get_frame_locals")
    else:
        observation = events[stop_index].payload.get("observation", {})
        payload = observation.get("payload", {}) if isinstance(observation, dict) else {}
        evidence["stop_pdb_session"] = payload
        ok = (
            isinstance(observation, dict) and observation.get("status") == "ok"
            and payload.get("stopped") is True
            and payload.get("workspace_removed") is True
        )
        if not ok:
            reasons.append("stop_pdb_session did not report stopped=true and workspace_removed=true")

    if stop_index is None:
        reasons.append("the intentional terminal transition could not be evaluated without a successful stop_pdb_session")
    else:
        transition_index = _find_event_index(events, event_type="transition", name="state_transition", after=stop_index)
        final_index = _find_event_index(events, event_type="final", name="run_finished", after=stop_index)
        terminal_ok = (
            transition_index is not None
            and events[transition_index].payload.get("target_state") == ControllerState.FAILED.value
            and final_index is not None
            and final_index == len(events) - 1
            and events[final_index].payload.get("final_state") == ControllerState.FAILED.value
            and transition_index < final_index
        )
        if not terminal_ok:
            reasons.append("no intentional terminal transition to Failed immediately followed stop_pdb_session")
        else:
            evidence["terminal_transition"] = {
                "reason": events[transition_index].payload.get("reason"),
                "target_state": events[transition_index].payload.get("target_state"),
            }

    return ReachabilitySequenceEvidence(ok=not reasons, reasons=tuple(reasons), steps=evidence)


def validate_events_jsonl(
    events_jsonl: str, *, run_id: Optional[str] = None, task_id: Optional[str] = None,
) -> tuple[bool, tuple[str, ...], tuple[RunEvent, ...]]:
    """Independently validate the serialized event trail: non-empty, every
    line parses and validates as a real ``RunEvent``, all events share one
    consistent run_id/task_id, sequence numbers are contiguous from 0, and
    every required event name is present. This is what actually proves
    "successful event projection/serialization" and "complete, valid,
    non-empty event evidence" rather than merely the absence of an exception
    from ``_project_events``.

    ``run_id``/``task_id`` are optional so this same function can revalidate
    a previously captured ``events_jsonl`` offline, without a live
    ``ControllerRunResult`` to cross-check against; when supplied, they must
    match what the events themselves carry.
    """
    if not events_jsonl.strip():
        return False, ("events_jsonl is empty",), ()
    lines = [line for line in events_jsonl.split("\n") if line]
    if not lines:
        return False, ("events_jsonl has no lines",), ()
    parsed: list[RunEvent] = []
    for line in lines:
        try:
            mapping = json.loads(line)
            event = RunEvent.from_mapping(mapping)
        except Exception as exc:  # noqa: BLE001 - any parse/validation failure fails closed
            return False, (f"events_jsonl line failed to parse/validate: {bounded_error(exc)}",), ()
        parsed.append(event)
    observed_run_ids = {event.run_id for event in parsed}
    observed_task_ids = {event.task_id for event in parsed}
    if len(observed_run_ids) != 1 or len(observed_task_ids) != 1:
        return False, ("events_jsonl does not share a single consistent run_id/task_id",), ()
    if run_id is not None and run_id not in observed_run_ids:
        return False, ("events_jsonl run_id does not match the controller run",), ()
    if task_id is not None and task_id not in observed_task_ids:
        return False, ("events_jsonl task_id does not match the controller run",), ()
    for index, event in enumerate(parsed):
        if event.sequence != index:
            return False, (f"events_jsonl sequence is not contiguous at index {index}",), ()
    names = {event.name for event in parsed}
    missing = _REQUIRED_EVENT_NAMES - names
    if missing:
        return False, (f"events_jsonl is missing required event names: {sorted(missing)}",), ()
    return True, (), tuple(parsed)


def _provenance_present(launch_plan: Optional[PdbLaunchPlan], bundle_hashes: Optional[dict[str, str]]) -> bool:
    return (
        launch_plan is not None
        and bundle_hashes is not None
        and len(bundle_hashes) >= len(_PDB_RUNTIME_MODULES) + 1  # +1 for agentic_debugger/__init__.py
        and all(_SHA256_HEX.fullmatch(value) for value in bundle_hashes.values())
    )


def determine_reachability_verdict(
    *,
    result_present: bool,
    quixbugs_authorized: bool,
    contained_authorized: bool,
    any_gate_allowed: bool,
    sequence_ok: bool,
    events_valid: bool,
    stop_reason_is_failed: bool,
    final_state_is_failed: bool,
    cleanup_succeeded: bool,
    canonical_source_unchanged: bool,
    provenance_present: bool,
    diagnostics_empty: bool,
) -> str:
    """The single, fail-closed PASSED/FAILED decision for one reachability case.

    Every argument is a fact that must independently hold; there is no
    aggregate count or "no exception was raised" shortcut. In particular,
    ``sequence_ok`` (from :func:`evaluate_reachability_sequence_from_events`)
    requires a successful ``start_pdb_session`` paused at the reviewed
    script/breakpoint/function, a successful bounded stack observation, a
    successful bounded frame-locals observation, a successful
    ``stop_pdb_session`` with ``stopped=true``/``workspace_removed=true``,
    and the intentional terminal transition immediately following it --
    ``events_valid`` requires the serialized event trail to be non-empty,
    fully parseable, contiguous, and complete. A caller cannot pass a single
    aggregate observation count in place of these.
    """

    passed = (
        result_present
        and quixbugs_authorized
        and contained_authorized
        and any_gate_allowed
        and sequence_ok
        and events_valid
        and stop_reason_is_failed
        and final_state_is_failed
        and cleanup_succeeded
        and canonical_source_unchanged
        and provenance_present
        and diagnostics_empty
    )
    return "REACHABILITY_CASE_PASSED" if passed else "REACHABILITY_CASE_FAILED"


@dataclass(frozen=True)
class ContainedPdbReachabilityResult:
    task_id: str
    verdict: str
    quixbugs_preflight: QuixPreflightReport
    contained_preflight: Optional[ContainedPdbPreflightReport]
    controller_final_state: Optional[str]
    controller_stop_reason: Optional[str]
    gate_decisions: tuple[dict[str, str], ...]
    pdb_observations: dict[str, int]
    events_jsonl: str
    launch_plan: Optional[dict[str, Any]]
    pdb_runtime_bundle_hashes: Optional[dict[str, str]]
    cleanup_attempted: bool
    cleanup_succeeded: bool
    cleanup_error: Optional[str]
    canonical_source_unchanged: Optional[bool]
    sequence_evidence: Optional[dict[str, Any]] = None
    events_valid: Optional[bool] = None
    events_validation_reasons: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "verdict": self.verdict,
            "quixbugs_preflight": self.quixbugs_preflight.to_mapping(),
            "contained_preflight": self.contained_preflight.to_mapping() if self.contained_preflight else None,
            "controller_final_state": self.controller_final_state,
            "controller_stop_reason": self.controller_stop_reason,
            "gate_decisions": list(self.gate_decisions),
            "pdb_observations": dict(self.pdb_observations),
            "events_jsonl": self.events_jsonl,
            "launch_plan": self.launch_plan,
            "pdb_runtime_bundle_hashes": self.pdb_runtime_bundle_hashes,
            "cleanup_attempted": self.cleanup_attempted,
            "cleanup_succeeded": self.cleanup_succeeded,
            "cleanup_error": self.cleanup_error,
            "canonical_source_unchanged": self.canonical_source_unchanged,
            "sequence_evidence": self.sequence_evidence,
            "events_valid": self.events_valid,
            "events_validation_reasons": list(self.events_validation_reasons),
            "diagnostics": list(self.diagnostics),
        }


def _blocked_result(
    task_id: str,
    quixbugs_report: QuixPreflightReport,
    contained_report: Optional[ContainedPdbPreflightReport],
    reason: str,
    *,
    cleanup_attempted: bool = False,
    cleanup_succeeded: bool = True,
    cleanup_error: Optional[str] = None,
) -> ContainedPdbReachabilityResult:
    return ContainedPdbReachabilityResult(
        task_id=task_id, verdict="REACHABILITY_CASE_BLOCKED", quixbugs_preflight=quixbugs_report,
        contained_preflight=contained_report, controller_final_state=None, controller_stop_reason=None,
        gate_decisions=(), pdb_observations={"successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0},
        events_jsonl="", launch_plan=None, pdb_runtime_bundle_hashes=None,
        cleanup_attempted=cleanup_attempted, cleanup_succeeded=cleanup_succeeded, cleanup_error=cleanup_error,
        canonical_source_unchanged=None, diagnostics=(reason,),
    )


class _Blocked(Exception):
    """Internal control-flow signal: a gate inside the try block was not
    authorized. Caught separately from generic failures so the eventual
    report says ``REACHABILITY_CASE_BLOCKED`` (a gate declined cleanly) rather
    than ``REACHABILITY_CASE_FAILED`` (an unexpected error), while still
    running through the exact same ``finally`` cleanup path so the reported
    cleanup outcome reflects what actually happened.
    """

    def __init__(self, reason: str, contained_report: Optional["ContainedPdbPreflightReport"] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.contained_report = contained_report


def _validate_quixbugs_runtime_probe_identity(adapter: QuixBugsAdapter, runtime_probe: RuntimeProbe, sources_parent: str) -> int:
    """Validate task-local probe identity before creating any owned workspace.

    The generic boundary cannot trust a caller that merely supplies a probe
    object.  The probe must be an exact :class:`RuntimeProbe`, the default
    gcd probe is locked to :data:`QUIXBUGS_PDB_TASK_ID` (a caller that wants
    PDB on another selected task must supply that task's own reviewed probe),
    and the module, focus symbol, and resolved breakpoint are checked against
    the selected task manifest and its pinned checkout first.
    """
    if type(runtime_probe) is not RuntimeProbe:
        raise ContainedPdbError("runtime probe must be an exact RuntimeProbe")
    manifest = adapter.manifest
    if runtime_probe is _GCD_RUNTIME_PROBE and manifest.task_id != QUIXBUGS_PDB_TASK_ID:
        raise ContainedPdbError(
            f"the default gcd runtime probe is locked to {QUIXBUGS_PDB_TASK_ID!r}, got {manifest.task_id!r}"
        )
    module_path = runtime_probe.module_path
    if module_path == manifest.corrected_path or module_path == manifest.pytest_path or module_path in manifest.support_paths:
        raise ContainedPdbError("runtime probe points to corrected, test, or support material")
    if module_path != manifest.buggy_path:
        raise ContainedPdbError("runtime probe module is not the selected task buggy path")
    if runtime_probe.focus_function not in manifest.oracle["target_symbols"]:
        raise ContainedPdbError("runtime probe focus is not a reviewed target symbol")
    project_root = Path(sources_parent).resolve() / "quixbugs"
    module = project_root / module_path
    try:
        module.relative_to(project_root)
    except ValueError as exc:
        raise ContainedPdbError("runtime probe escapes the selected QuixBugs source") from exc
    if not module.is_file():
        raise ContainedPdbError("runtime probe module is not present in the pinned checkout")
    breakpoint_line = _resolve_probe_breakpoint_checked(module.read_text(encoding="utf-8"), runtime_probe)
    if not isinstance(breakpoint_line, int) or breakpoint_line <= 0:
        raise ContainedPdbError("runtime probe breakpoint did not resolve inside the selected module")
    return breakpoint_line


#: Public alias of the task-local probe-identity validator, used by the
#: live-case path (:mod:`agentic_debugger.evaluation.live_quixbugs`) to gate
#: an explicit task-local ``RuntimeProbe`` before any owned workspace,
#: provider, or WSL/Bubblewrap contact.
validate_quixbugs_runtime_probe_identity = _validate_quixbugs_runtime_probe_identity


def run_quixbugs_gcd_pdb_reachability_case(
    *,
    repository_root: str,
    manifest_path: str,
    sources_parent: str,
    facts: QuixBugsPreflightFacts,
    resource_limits: ResourceLimits,
    tool_version: str = "quixbugs-gcd-pdb-reachability-v1",
    runtime_probe: Optional[RuntimeProbe] = None,
    hypothesis_id: Optional[str] = None,
    hypothesis_statement: Optional[str] = None,
    _enforce_generic_probe_identity: bool = False,
) -> ContainedPdbReachabilityResult:
    """Run exactly one deterministic, no-model PDB reachability case.

    Fails closed (returns ``REACHABILITY_CASE_BLOCKED`` with no WSL/Bubblewrap
    contact beyond what the accepted QuixBugs preflight already performs) when
    the accepted QuixBugs gate is not authorized, the pinned checkout does not
    verify, or the additional contained-PDB gate (verified execution context,
    open resource-isolation, containment, launch-plan identity, positive
    budget) is not satisfied.
    """

    adapter = QuixBugsAdapter.from_manifest(manifest_path)
    runtime_probe = runtime_probe or _GCD_RUNTIME_PROBE
    if hypothesis_id is None:
        hypothesis_id = "quixbugs-gcd-runtime-evidence-v1"
    if hypothesis_statement is None:
        hypothesis_statement = (
            "Low-confidence hypothesis: the defect concerns the target function's "
            "argument or state transition; bounded runtime evidence is required "
            "before proposing a root cause."
        )
    if runtime_probe is _GCD_RUNTIME_PROBE and adapter.manifest.task_id != QUIXBUGS_PDB_TASK_ID:
        raise ContainedPdbError(f"the default reachability probe is scoped to {QUIXBUGS_PDB_TASK_ID!r}, got {adapter.manifest.task_id!r}")

    # The exported generic entry point enables this check.  The historical gcd
    # wrapper deliberately retains its accepted preflight-blocked behavior.
    if _enforce_generic_probe_identity:
        _validate_quixbugs_runtime_probe_identity(adapter, runtime_probe, sources_parent)

    repo = Path(repository_root).resolve()
    quixbugs_report = adapter.preflight(facts, repository_root=str(repo))
    if not quixbugs_report.authorized:
        return _blocked_result(adapter.manifest.task_id, quixbugs_report, None, "QuixBugs preflight blocked: " + ",".join(quixbugs_report.blocked_gates))
    if facts.execution_context is None:
        return _blocked_result(adapter.manifest.task_id, quixbugs_report, None, "no verified execution context supplied")

    execution_context = facts.execution_context
    project_root = Path(sources_parent).resolve() / "quixbugs"
    external: Optional[ExternalWorkspace] = None
    workspace: Optional[TaskWorkspace] = None
    context: Optional[DemoToolContext] = None
    result: Optional[ControllerRunResult] = None
    contained_report: Optional[ContainedPdbPreflightReport] = None
    launch_plan: Optional[PdbLaunchPlan] = None
    bundle_hashes: Optional[dict[str, str]] = None
    diagnostics: list[str] = []
    driver: Optional[DeterministicPdbReachabilityDriver] = None
    blocked_reason: Optional[str] = None
    probe: Optional[PdbProbe] = None

    try:
        if not project_root.is_dir():
            raise _Blocked("pinned QuixBugs source is not already acquired; refusing to clone during a reachability case")
        # Real, non-forgeable re-verification: raises on any revision/origin/cleanliness mismatch.
        QuixBugsSourceAcquirer().verify_pinned(project_root, adapter.manifest.authority_revision)

        external = ExternalWorkspace.create(
            facts.external_parent, repository_root=str(repo), containment_root=execution_context.containment.root,
        )
        external.verifier_workspace_parent.mkdir(parents=True, exist_ok=True)
        external.assert_contained(external.verifier_workspace_parent)

        probe = prepare_quixbugs_pdb_probe(project_root, external.verifier_workspace_parent, runtime_probe)

        launch_plan = PdbLaunchPlan(
            python_executable=execution_context.environment.python_executable,
            driver=probe.script,
            target=probe.script,
            breakpoints=(probe.breakpoint_line,),
            cwd=adapter.manifest.cwd,
            argv=(probe.script,),
            environment=dict(execution_context.environment.environment),
        )

        contained_report = contained_pdb_preflight(
            task_id=adapter.manifest.task_id,
            execution_context=execution_context,
            external_parent=facts.external_parent,
            repository_root=str(repo),
            launch_plan=launch_plan,
            expected_python_executable=execution_context.environment.python_executable,
            expected_cwd=adapter.manifest.cwd,
            expected_target=probe.script,
            expected_breakpoints=(probe.breakpoint_line,),
            pdb_observation_budget=QUIXBUGS_PDB_OBSERVATION_BUDGET,
        )
        if not contained_report.authorized:
            raise _Blocked(
                "contained-PDB preflight blocked: " + ",".join(contained_report.blocked_gates), contained_report,
            )

        bundle_dir = external.root / "pdb-runtime-bundle"
        bundle_hashes = materialize_pdb_runtime_bundle(bundle_dir)
        pdb_runtime_root_posix = to_wsl_path(str(bundle_dir), execution_context.runner.process.distro)

        discovery_workspace = TaskWorkspace(str(project_root), parent_dir=str(external.verifier_workspace_parent))
        try:
            from agentic_debugger.quixbugs.adapter import QuixBugsSmokeRunner

            discovery = QuixBugsSmokeRunner(adapter, QuixBugsSourceAcquirer()).discover(execution_context, discovery_workspace)
        finally:
            discovery_workspace.cleanup()

        commands = adapter.build_commands(fail_to_pass=discovery.f2p_candidates, pass_to_pass=discovery.p2p_candidates)
        source = TaskSource("external", "quixbugs", adapter.source_provenance())
        task: DebugTask = adapter.to_debug_task(source, commands, pdb_observation_budget=QUIXBUGS_PDB_OBSERVATION_BUDGET)

        workspace = TaskWorkspace(str(project_root), parent_dir=str(external.verifier_workspace_parent))

        def _pdb_session_factory(ws: TaskWorkspace) -> ContainedPdbSession:
            return ContainedPdbSession(
                ws,
                runner=execution_context.runner,
                pdb_runtime_root_posix=pdb_runtime_root_posix,
                resource_limits=resource_limits,
            )

        context = DemoToolContext(
            task=task, workspace=workspace, patch="", probe=probe, execution_context=execution_context,
            pdb_session_factory=_pdb_session_factory,
        )
        registry = build_registry(context, pdb_policy=pdb_policy_for(QUIXBUGS_PDB_POLICY))

        driver = DeterministicPdbReachabilityDriver(
            hypothesis_id=hypothesis_id,
            hypothesis_statement=hypothesis_statement,
            gate_policy=pdb_policy_for(QUIXBUGS_PDB_POLICY),
        )
        controller = DeterministicController(registry, driver, ControllerRunConfig(max_model_calls=16))
        run_id = f"pdb-reachability-{uuid.uuid4().hex}"
        budget_limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
        result = controller.run(
            ControllerSnapshot(
                run_id, task.task_id, ControllerState.REPRODUCE, 0, budget_limits, ControllerBudgetState(), HypothesisLedger(),
            )
        )
    except _Blocked as exc:
        blocked_reason = exc.reason
        if exc.contained_report is not None:
            contained_report = exc.contained_report
        diagnostics.append(exc.reason)
    except Exception as exc:
        diagnostics.append(bounded_error(exc))
    finally:
        cleanup_errors: list[str] = []
        if context is not None:
            cleanup_errors.extend(bounded_error(exc) for exc in context.release_pdb())
        if workspace is not None:
            try:
                workspace.cleanup()
            except Exception as exc:  # noqa: BLE001 - cleanup must continue
                cleanup_errors.append(bounded_error(exc))
        cleanup_attempted = external is not None
        cleanup_succeeded = True
        cleanup_error: Optional[str] = None
        if cleanup_errors:
            cleanup_succeeded = False
            cleanup_error = cleanup_errors[0]
        if external is not None:
            root = external.root
            try:
                external.cleanup()
                removed = not root.exists()
                cleanup_succeeded = cleanup_succeeded and removed
                if not removed and cleanup_error is None:
                    cleanup_error = "owned external workspace remains"
            except Exception as exc:  # noqa: BLE001 - report, do not raise from a finally block
                cleanup_succeeded = False
                cleanup_error = cleanup_error or bounded_error(exc)

    if blocked_reason is not None:
        return _blocked_result(
            adapter.manifest.task_id, quixbugs_report, contained_report, blocked_reason,
            cleanup_attempted=cleanup_attempted, cleanup_succeeded=cleanup_succeeded, cleanup_error=cleanup_error,
        )

    canonical_source_unchanged: Optional[bool] = None
    if project_root.is_dir():
        try:
            QuixBugsSourceAcquirer().verify_pinned(project_root, adapter.manifest.authority_revision)
            canonical_source_unchanged = True
        except Exception as exc:  # noqa: BLE001 - a real mismatch must be reported, not raised late
            canonical_source_unchanged = False
            diagnostics.append(bounded_error(exc))

    events = ""
    pdb_observations = {"successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0}
    controller_final_state = None
    controller_stop_reason = None
    if result is not None:
        controller_final_state = result.final_state.value
        controller_stop_reason = result.stop_reason.value
        pdb_observations = _pdb_observation_counts(result)
        try:
            events = _project_events(result, tool_version=tool_version)
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(bounded_error(exc))

    gate_decisions = tuple({"allowed": decision.allowed, "reason": decision.reason.value} for decision in (driver.gate_decisions if driver else []))

    events_valid = False
    events_validation_reasons: tuple[str, ...] = ("no controller result",)
    sequence_evidence: Optional[ReachabilitySequenceEvidence] = None
    if result is not None:
        events_valid, events_validation_reasons, parsed_events = validate_events_jsonl(
            events, run_id=result.run_id, task_id=result.task_id,
        )
        if events_valid and probe is not None:
            sequence_evidence = evaluate_reachability_sequence_from_events(
                parsed_events, expected_script=probe.script, expected_function=probe.focus_function,
                expected_breakpoint_line=probe.breakpoint_line,
            )
        elif probe is None:
            sequence_evidence = ReachabilitySequenceEvidence(False, ("reviewed probe identity is unavailable",), {})

    provenance_present = _provenance_present(launch_plan, bundle_hashes)

    verdict = determine_reachability_verdict(
        result_present=result is not None,
        quixbugs_authorized=quixbugs_report.authorized,
        contained_authorized=contained_report is not None and contained_report.authorized,
        any_gate_allowed=any(decision["allowed"] for decision in gate_decisions),
        sequence_ok=sequence_evidence is not None and sequence_evidence.ok,
        events_valid=events_valid,
        stop_reason_is_failed=result is not None and result.stop_reason is ControllerStopReason.FAILED,
        final_state_is_failed=result is not None and result.final_state is ControllerState.FAILED,
        cleanup_succeeded=cleanup_succeeded,
        canonical_source_unchanged=canonical_source_unchanged is True,
        provenance_present=provenance_present,
        diagnostics_empty=not diagnostics,
    )

    return ContainedPdbReachabilityResult(
        task_id=adapter.manifest.task_id,
        verdict=verdict,
        quixbugs_preflight=quixbugs_report,
        contained_preflight=contained_report,
        controller_final_state=controller_final_state,
        controller_stop_reason=controller_stop_reason,
        gate_decisions=gate_decisions,
        pdb_observations=pdb_observations,
        events_jsonl=events,
        launch_plan=launch_plan.to_mapping() if launch_plan else None,
        pdb_runtime_bundle_hashes=bundle_hashes,
        cleanup_attempted=cleanup_attempted,
        cleanup_succeeded=cleanup_succeeded,
        cleanup_error=cleanup_error,
        canonical_source_unchanged=canonical_source_unchanged,
        sequence_evidence=sequence_evidence.to_mapping() if sequence_evidence else None,
        events_valid=events_valid,
        events_validation_reasons=events_validation_reasons,
        diagnostics=tuple(diagnostics),
    )


def run_quixbugs_pdb_reachability_case(
    *,
    repository_root: str,
    manifest_path: str,
    sources_parent: str,
    facts: QuixBugsPreflightFacts,
    resource_limits: ResourceLimits,
    runtime_probe: RuntimeProbe,
    hypothesis_id: str,
    hypothesis_statement: str,
    tool_version: str = "quixbugs-paired-pilot-pdb-qualification-v1",
) -> ContainedPdbReachabilityResult:
    """Run the same real contained PDB path with a task-local reviewed probe."""

    return run_quixbugs_gcd_pdb_reachability_case(
        repository_root=repository_root,
        manifest_path=manifest_path,
        sources_parent=sources_parent,
        facts=facts,
        resource_limits=resource_limits,
        runtime_probe=runtime_probe,
        hypothesis_id=hypothesis_id,
        hypothesis_statement=hypothesis_statement,
        tool_version=tool_version,
        _enforce_generic_probe_identity=True,
    )


__all__ = [
    "QUIXBUGS_PDB_TASK_ID",
    "QUIXBUGS_PDB_POLICY",
    "QUIXBUGS_PDB_REPETITIONS",
    "QUIXBUGS_PDB_OBSERVATION_BUDGET",
    "QUIXBUGS_GCD_RUNTIME_PROBE",
    "ContainedPdbError",
    "ContainedPdbGateName",
    "ContainedPdbGateStatus",
    "ContainedPdbGateResult",
    "ContainedPdbPreflightReport",
    "ContainedPdbReachabilityResult",
    "ContainedPdbSession",
    "DeterministicPdbReachabilityDriver",
    "ReachabilitySequenceEvidence",
    "build_contained_pdb_worker_argv",
    "contained_pdb_preflight",
    "determine_reachability_verdict",
    "evaluate_reachability_sequence_from_events",
    "materialize_pdb_runtime_bundle",
    "prepare_quixbugs_gcd_pdb_probe",
    "prepare_quixbugs_pdb_probe",
    "run_quixbugs_pdb_reachability_case",
    "run_quixbugs_gcd_pdb_reachability_case",
    "validate_events_jsonl",
    "validate_quixbugs_runtime_probe_identity",
]
