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
from agentic_debugger.quixbugs.pdb_bundle import (
    _DEFAULT_REQUEST_TIMEOUT_SECONDS,
    _DEFAULT_SESSION_WALL_CLOCK_SECONDS,
    _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    _DEFAULT_STARTUP_TIMEOUT_SECONDS,
    _GCD_RUNTIME_PROBE,
    _PDB_RUNTIME_MODULES,
    QUIXBUGS_GCD_RUNTIME_PROBE,
    QUIXBUGS_PDB_OBSERVATION_BUDGET,
    QUIXBUGS_PDB_POLICY,
    QUIXBUGS_PDB_REPETITIONS,
    QUIXBUGS_PDB_TASK_ID,
    ContainedPdbError,
    ContainedPdbSession,
    prepare_quixbugs_gcd_pdb_probe,
    prepare_quixbugs_pdb_probe,
    _is_within,
    _resolve_probe_breakpoint_checked,
    _sha256_file,
    build_contained_pdb_worker_argv,
    materialize_pdb_runtime_bundle,
)
from agentic_debugger.quixbugs.pdb_preflight import (
    ContainedPdbGateName,
    ContainedPdbGateResult,
    ContainedPdbGateStatus,
    ContainedPdbPreflightReport,
    _boolean_gate,
    contained_pdb_preflight,
)
from agentic_debugger.quixbugs.pdb_verdict import (
    ContainedPdbReachabilityResult,
    DeterministicPdbReachabilityDriver,
    ReachabilitySequenceEvidence,
    _after,
    _find_event_index,
    _pdb_observation_counts,
    _project_events,
    determine_reachability_verdict,
    evaluate_reachability_sequence_from_events,
    validate_events_jsonl,
)
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
