"""Protocol-1.3 live-model evaluation for one pinned QuixBugs task.

Carries a single QuixBugs task through the exact accepted live pipeline
(:mod:`agentic_debugger.evaluation.live`'s ``LiveModelAdapter``,
``DeterministicController``, ``EvaluationVerifier``, redaction, and report
schema) instead of the curated-fixture-only path. This module never imports
``agentic_debugger.demo.runner`` or otherwise touches the curated fixture
root -- there is no code path here that can fall back to a curated fixture
when a QuixBugs task is requested.

Only the resource-limited, WSL/Bubblewrap-verified execution boundary
already accepted for QuixBugs (:mod:`agentic_debugger.quixbugs.adapter`) is
used to run untrusted candidate code. Policy defaults to
``DemoPolicy.STATIC_BASELINE``: a static case never accepts a PDB probe and
never gains PDB access, because there is no parameter that requests a
probe under that policy. ``pdb-on-uncertainty`` requires an explicit
task-local ``RuntimeProbe`` that is identity-validated against the selected
task manifest and its pinned checkout; the historical default gcd probe
keeps its gcd lock, and PDB is never requested or silently substituted for
any other selected task without that task's own reviewed probe. Source
acquisition never clones or resets the pinned checkout: a missing or
non-pinned checkout fails closed rather than acquiring one.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from agentic_debugger.agent.controller import ControllerRunConfig, DeterministicController
from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.bugsinpy.adapter import ExternalWorkspace
from agentic_debugger.bugsinpy.wsl import ResourceLimits, to_wsl_path
from agentic_debugger.demo.catalog import RuntimeProbe
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.tools import DemoToolContext, build_registry
from agentic_debugger.evaluation.live import (
    LIVE_SCHEMA_VERSION,
    JsonlCommandTransport,
    LiveCaseResult,
    LiveCaseStatus,
    LiveConfigurationError,
    LiveExecutionAuthorization,
    LiveModelAdapter,
    LiveModelConfig,
    LiveModelMetrics,
    LiveOptInError,
    LiveRunLimits,
    ModelTransport,
    _finalize_live_case,
    _new_evaluation_identity,
    redact_for_recording,
)
from agentic_debugger.evaluation.runner import bounded_error
from agentic_debugger.evaluation.task_schema import TaskSource
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.quixbugs.adapter import (
    QuixBugsAdapter,
    QuixBugsPreflightFacts,
    QuixBugsSmokeRunner,
    QuixBugsSourceAcquirer,
    QuixPreflightReport,
)
from agentic_debugger.quixbugs.contained_pdb import (
    QUIXBUGS_GCD_RUNTIME_PROBE,
    QUIXBUGS_PDB_OBSERVATION_BUDGET,
    QUIXBUGS_PDB_TASK_ID,
    ContainedPdbError,
    ContainedPdbSession,
    contained_pdb_preflight,
    materialize_pdb_runtime_bundle,
    prepare_quixbugs_pdb_probe,
    validate_quixbugs_runtime_probe_identity,
)
from agentic_debugger.runtime.execution import PdbLaunchPlan
from agentic_debugger.runtime.workspace import TaskWorkspace


class QuixBugsLiveConfigurationError(LiveConfigurationError):
    """A QuixBugs live-case precondition (manifest, source, context,
    authorization) is unmet; the case fails closed before any provider or
    WSL/Bubblewrap contact."""


#: Compatibility default for the accepted static QuixBugs live path.
QUIXBUGS_LIVE_POLICY = DemoPolicy.STATIC_BASELINE

#: This integration proves the smallest coherent feasibility path for exactly
#: one QuixBugs task and one repetition; a caller asking for anything else is
#: a configuration error, not a silently-widened scope.
QUIXBUGS_LIVE_REPETITIONS = 1
QUIXBUGS_PDB_MODEL_ID = "opencode/deepseek-v4-flash-free"
QUIXBUGS_PDB_VARIANT = "max"
QUIXBUGS_PDB_PROVIDER = "OpenCode Zen"


def _validate_quixbugs_pdb_model_config(
    config: LiveModelConfig,
    *,
    binding: Optional[tuple[str, str, str]] = None,
) -> None:
    """Validate the PDB-case model identity binding.

    The historical default is the accepted OpenCode Zen identity
    (``QUIXBUGS_PDB_MODEL_ID`` / ``QUIXBUGS_PDB_VARIANT``).  An explicit
    ``binding`` (provider, model id, variant) supplied by the OpenCode Go
    execution adapter replaces that historical identity for the PDB case; the
    exact runtime model/catalog identity then always comes from validated
    authorization and route evidence, never from a hardcoded default.
    """
    if binding is None:
        if config.model_name != QUIXBUGS_PDB_MODEL_ID:
            raise QuixBugsLiveConfigurationError(
                f"QuixBugs PDB case requires model {QUIXBUGS_PDB_MODEL_ID!r}"
            )
        expected_model = QUIXBUGS_PDB_MODEL_ID
        expected_variant = QUIXBUGS_PDB_VARIANT
    else:
        provider, model_id, variant = binding
        if type(provider) is not str or type(model_id) is not str or type(variant) is not str:
            raise QuixBugsLiveConfigurationError("QuixBugs PDB identity binding is malformed")
        if not provider or not model_id or not variant:
            raise QuixBugsLiveConfigurationError("QuixBugs PDB identity binding is incomplete")
        if model_id == QUIXBUGS_PDB_MODEL_ID:
            raise QuixBugsLiveConfigurationError(
                "QuixBugs PDB identity binding must not reuse the historical OpenCode Zen model identity"
            )
        expected_model = model_id
        expected_variant = variant
    if config.model_name != expected_model:
        raise QuixBugsLiveConfigurationError(
            f"QuixBugs PDB case requires model {expected_model!r}"
        )
    command = list(config.command)
    pairs = {(command[index], command[index + 1]) for index in range(len(command) - 1)}
    if ("--model", expected_model) not in pairs or ("--variant", expected_variant) not in pairs:
        raise QuixBugsLiveConfigurationError(
            "QuixBugs PDB case requires the exact bound model and variant in the transport command"
        )


def _blocked_quixbugs_case(
    *, task_id: str, policy: DemoPolicy, repetition: int, case_id: str, run_id: str, reason: str,
) -> LiveCaseResult:
    """A case that never started because a precondition was unmet.

    No manifest/source/context/authorization gate ever ran a controller,
    contacted a provider, or touched WSL/Bubblewrap for this case -- the
    report says so honestly rather than fabricating an attempted run.
    """
    measurements = LiveModelMetrics(termination_reason="preflight_blocked").to_mapping()
    measurements.update(
        {
            "successful_pdb_observation_count": 0,
            "failed_pdb_observation_count": 0,
            "tool_call_count": 0,
            "case_elapsed_duration_ms": 0,
            "model_phase_elapsed_duration_ms": 0,
            "model_transport_duration_ms": 0,
            "elapsed_scope": "case_observed; model_phase=transport_only",
        }
    )
    return LiveCaseResult(
        task_id=task_id,
        policy=policy.value,
        repetition=repetition,
        status=LiveCaseStatus.HARNESS_ERROR,
        controller={"completed": False, "final_state": None, "stop_reason": None, "model_calls": 0, "exception": False},
        verifier={
            "executed": False, "failure": False, "status": None, "outcome": None, "baseline_valid": None,
            "patch_application": None, "fail_to_pass": None, "pass_to_pass": None, "workspace_cleaned": None,
            "canonical_fixture_unchanged": None, "localization": {"outcome": "NO_LOCALIZATION"},
        },
        measurements=measurements,
        reporting={
            "mode": "live", "completed": False, "partial": True, "interrupted": False, "event_recorded": False,
            "cleanup": "not_started", "case_directory_owned": False,
        },
        events_jsonl="",
        diagnostics=(redact_for_recording(reason),),
        case_id=case_id,
        run_id=run_id,
        trajectory_id=run_id,
    )


def _validate_quixbugs_pdb_probe(
    runtime_probe: Optional[RuntimeProbe],
    adapter: QuixBugsAdapter,
    sources_parent: str,
) -> RuntimeProbe:
    """Validate the explicit task-local PDB probe for the selected task.

    ``pdb-on-uncertainty`` requires an exact reviewed :class:`RuntimeProbe`
    for the selected task; the identity validator checks the probe against
    the selected task ID (the default gcd probe is locked to the gcd task),
    the buggy module path, corrected/test/support exclusion, the reviewed
    target symbol, source containment inside the pinned checkout, and a
    resolvable breakpoint anchor. A rejected probe raises a configuration
    error before any provider, owned workspace, or WSL/Bubblewrap contact.
    """
    if runtime_probe is None:
        raise QuixBugsLiveConfigurationError(
            "QuixBugs PDB-on-uncertainty requires an explicit task-local RuntimeProbe for the selected task"
        )
    try:
        validate_quixbugs_runtime_probe_identity(adapter, runtime_probe, sources_parent)
    except ContainedPdbError as exc:
        raise QuixBugsLiveConfigurationError(
            f"QuixBugs PDB probe rejected for the selected task: {exc}"
        ) from exc
    return runtime_probe


def run_live_quixbugs_case(
    *,
    repository_root: str,
    manifest_path: str,
    sources_parent: str,
    facts: QuixBugsPreflightFacts,
    config: LiveModelConfig,
    limits: LiveRunLimits,
    transport: ModelTransport,
    evaluation_id: str = "local",
    repetition: int = 1,
    policy: DemoPolicy = QUIXBUGS_LIVE_POLICY,
    runtime_probe: Optional[RuntimeProbe] = None,
    pdb_identity_binding: Optional[tuple[str, str, str]] = None,
    campaign_version: int = 2,
) -> LiveCaseResult:
    """Run exactly one QuixBugs live case through the accepted live pipeline.

    Fails closed (returns a :class:`LiveCaseStatus.HARNESS_ERROR` case with
    no provider/WSL contact) when: the manifest is invalid, the QuixBugs
    preflight gate is not authorized, the pinned source checkout is missing
    or does not match the pinned revision, or no verified execution context
    was supplied. The pinned checkout is only ever re-verified here, never
    cloned, reset, or cleaned.

    ``runtime_probe`` is the explicit task-local
    :class:`~agentic_debugger.demo.catalog.RuntimeProbe` required for
    ``pdb-on-uncertainty``. Static-baseline cases accept no probe and retain
    zero PDB access. ``pdb_identity_binding`` is the explicit (provider,
    model id, variant) runtime identity for a PDB-on-uncertainty case,
    supplied by the OpenCode Go execution adapter. When absent the accepted
    historical OpenCode Zen identity applies unchanged.

    ``campaign_version`` selects the versioned terminal-classification
    contract of :func:`agentic_debugger.evaluation.live._finalize_live_case`:
    campaigns below v4 keep the frozen classification unchanged; v4 uses the
    verifier-authoritative classification.
    """
    if type(campaign_version) is not int or campaign_version < 1:
        raise QuixBugsLiveConfigurationError("campaign version must be a positive integer")
    if type(facts) is not QuixBugsPreflightFacts:
        raise QuixBugsLiveConfigurationError("QuixBugs preflight facts are required")
    if facts.execution_context is None:
        raise QuixBugsLiveConfigurationError("QuixBugs live case requires a verified execution context")
    if type(repetition) is not int or repetition != QUIXBUGS_LIVE_REPETITIONS:
        raise QuixBugsLiveConfigurationError("QuixBugs live case supports exactly one repetition")
    if type(policy) is not DemoPolicy or policy not in {QUIXBUGS_LIVE_POLICY, DemoPolicy.PDB_ON_UNCERTAINTY}:
        raise QuixBugsLiveConfigurationError("unsupported QuixBugs live policy")
    if policy is DemoPolicy.STATIC_BASELINE and runtime_probe is not None:
        raise QuixBugsLiveConfigurationError("static-baseline accepts no PDB probe")
    if policy is DemoPolicy.PDB_ON_UNCERTAINTY:
        _validate_quixbugs_pdb_model_config(config, binding=pdb_identity_binding)

    adapter = QuixBugsAdapter.from_manifest(manifest_path)
    task_id = adapter.manifest.task_id
    if policy is DemoPolicy.PDB_ON_UNCERTAINTY:
        runtime_probe = _validate_quixbugs_pdb_probe(runtime_probe, adapter, sources_parent)
    case_id = f"{evaluation_id}:{task_id}:{policy.value}:r{repetition}"
    run_id = f"live-{case_id}"
    started = time.monotonic()
    repo = Path(repository_root).resolve()

    preflight: QuixPreflightReport = adapter.preflight(facts, repository_root=str(repo))
    if not preflight.authorized:
        return _blocked_quixbugs_case(
            task_id=task_id, policy=policy, repetition=repetition, case_id=case_id, run_id=run_id,
            reason="QuixBugs preflight blocked: " + ",".join(preflight.blocked_gates),
        )

    sources_dir = Path(sources_parent).resolve()
    project_root = sources_dir / "quixbugs"
    workspace: Optional[TaskWorkspace] = None
    context: Optional[DemoToolContext] = None
    result = None
    live_adapter: Optional[LiveModelAdapter] = None
    metrics = LiveModelMetrics()
    diagnostics: list[str] = []
    interrupted = False
    controller_failed = False
    external: Optional[ExternalWorkspace] = None
    task = None
    pdb_evidence: Optional[dict[str, Any]] = None
    try:
        if not project_root.is_dir():
            raise QuixBugsLiveConfigurationError(
                "pinned QuixBugs source is not already acquired at the expected location; "
                "refusing to clone or download during a live case"
            )
        # Re-verifies revision/origin/cleanliness only; never clones, resets, or cleans.
        QuixBugsSourceAcquirer().verify_pinned(project_root, adapter.manifest.authority_revision)

        external = ExternalWorkspace.create(
            facts.external_parent, repository_root=str(repo), containment_root=facts.execution_context.containment.root,
        )
        external.verifier_workspace_parent.mkdir(parents=True, exist_ok=True)
        external.assert_contained(external.verifier_workspace_parent)

        probe = None
        launch_plan = None
        bundle_hashes = None
        if policy is DemoPolicy.PDB_ON_UNCERTAINTY:
            binding_provider, binding_model_id, binding_variant = (
                pdb_identity_binding
                if pdb_identity_binding is not None
                else (QUIXBUGS_PDB_PROVIDER, QUIXBUGS_PDB_MODEL_ID, QUIXBUGS_PDB_VARIANT)
            )
            probe = prepare_quixbugs_pdb_probe(
                project_root, external.verifier_workspace_parent, runtime_probe,
            )
            launch_plan = PdbLaunchPlan(
                python_executable=facts.execution_context.environment.python_executable,
                driver=probe.script,
                target=probe.script,
                breakpoints=(probe.breakpoint_line,),
                cwd=adapter.manifest.cwd,
                argv=(probe.script,),
                environment=dict(facts.execution_context.environment.environment),
            )
            contained_report = contained_pdb_preflight(
                task_id=adapter.manifest.task_id,
                execution_context=facts.execution_context,
                external_parent=facts.external_parent,
                repository_root=str(repo),
                launch_plan=launch_plan,
                expected_python_executable=facts.execution_context.environment.python_executable,
                expected_cwd=adapter.manifest.cwd,
                expected_target=probe.script,
                expected_breakpoints=(probe.breakpoint_line,),
                pdb_observation_budget=QUIXBUGS_PDB_OBSERVATION_BUDGET,
            )
            pdb_evidence = {
                "provider": binding_provider,
                "model_id": binding_model_id,
                "variant": binding_variant,
                "policy": policy.value,
                "observation_budget": QUIXBUGS_PDB_OBSERVATION_BUDGET,
                "contained_preflight": contained_report.to_mapping(),
                "contained_preflight_gate_open": contained_report.authorized,
                "launch_plan": launch_plan.to_mapping(),
            }
            if not contained_report.authorized:
                raise QuixBugsLiveConfigurationError(
                    "contained-PDB preflight blocked: " + ",".join(contained_report.blocked_gates)
                )
            bundle_dir = external.root / "pdb-runtime-bundle"
            bundle_hashes = materialize_pdb_runtime_bundle(bundle_dir)
            pdb_evidence["pdb_runtime_bundle_hashes"] = dict(bundle_hashes)
            pdb_runtime_root_posix = to_wsl_path(
                str(bundle_dir), facts.execution_context.runner.process.distro
            )

        smoke = QuixBugsSmokeRunner(adapter, QuixBugsSourceAcquirer())
        discovery_workspace = TaskWorkspace(str(project_root), parent_dir=str(external.verifier_workspace_parent))
        try:
            discovery = smoke.discover(facts.execution_context, discovery_workspace)
        finally:
            discovery_workspace.cleanup()

        commands = adapter.build_commands(fail_to_pass=discovery.f2p_candidates, pass_to_pass=discovery.p2p_candidates)
        source = TaskSource("external", "quixbugs", adapter.source_provenance())
        task = adapter.to_debug_task(
            source,
            commands,
            pdb_observation_budget=(
                QUIXBUGS_PDB_OBSERVATION_BUDGET
                if policy is DemoPolicy.PDB_ON_UNCERTAINTY
                else 0
            ),
        )

        workspace = TaskWorkspace(str(project_root), parent_dir=str(external.verifier_workspace_parent))
        if policy is DemoPolicy.PDB_ON_UNCERTAINTY:
            def _pdb_session_factory(ws: TaskWorkspace) -> ContainedPdbSession:
                return ContainedPdbSession(
                    ws,
                    runner=facts.execution_context.runner,
                    pdb_runtime_root_posix=pdb_runtime_root_posix,
                    resource_limits=ResourceLimits(
                        cpu_seconds=int(adapter.manifest.resource_profile["cpu_seconds"]),
                        memory_bytes=int(adapter.manifest.resource_profile["memory_bytes"]),
                        max_processes=int(adapter.manifest.resource_profile["max_processes"]),
                    ),
                )
            context = DemoToolContext(
                task=task, workspace=workspace, patch="", probe=probe,
                execution_context=facts.execution_context,
                pdb_session_factory=_pdb_session_factory,
            )
        else:
            context = DemoToolContext(
                task=task, workspace=workspace, patch="", probe=None, execution_context=facts.execution_context,
            )
        registry = build_registry(context, pdb_policy=pdb_policy_for(policy))
        live_adapter = LiveModelAdapter(
            task=task, policy=policy, config=config, transport=transport, limits=limits, registry=registry,
            evaluation_id=evaluation_id, case_id=case_id, run_id=run_id, trajectory_id=run_id,
        )
        metrics = live_adapter.metrics
        controller = DeterministicController(
            registry,
            live_adapter,
            ControllerRunConfig(
                max_model_calls=limits.max_controller_steps,
                require_external_source_context=True,
            ),
        )
        try:
            result = controller.run(
        ControllerSnapshot(
                    run_id, task_id, ControllerState.REPRODUCE, 0,
                    ControllerBudgetLimits.from_task_constraints(task.constraints),
                    ControllerBudgetState(), HypothesisLedger(),
                )
            )
        except KeyboardInterrupt:
            interrupted = True
            diagnostics.append("controller interrupted by operator")
        except Exception as exc:
            controller_failed = True
            diagnostics.append(redact_for_recording(bounded_error(exc)))
    except KeyboardInterrupt:
        interrupted = True
        diagnostics.append("run interrupted by operator")
    except Exception as exc:
        diagnostics.append(redact_for_recording(bounded_error(exc)))

    def cleanup_external() -> tuple[bool, Optional[str]]:
        if external is None:
            return True, None
        root = external.root
        external.cleanup()
        removed = not root.exists()
        return removed, (None if removed else "owned external workspace remains")

    #: Bounded adapter-visible evidence for every policy: the controller PDB
    #: gate decisions and the bounded malformed-directive rejection records
    #: observed during this case.  For static-baseline cases the gate is
    #: disabled, so the decision list is empty by construction.
    case_evidence: dict[str, Any] = {
        "pdb_gate_decisions": list(live_adapter.pdb_gate_decisions) if live_adapter else [],
        "directive_rejections": list(live_adapter.directive_rejections) if live_adapter else [],
    }
    if pdb_evidence is not None:
        case_evidence = {**pdb_evidence, **case_evidence}

    return _finalize_live_case(
        task_id=task_id, policy=policy, repetition=repetition, case_id=case_id, run_id=run_id, config=config,
        task=task, context=context, workspace=workspace, result=result, metrics=metrics, live_adapter=live_adapter,
        started=started, interrupted=interrupted, controller_failed=controller_failed, diagnostics=diagnostics,
        verify=lambda: EvaluationVerifier(
            str(sources_dir), workspace_parent=str(external.verifier_workspace_parent), execution_context=facts.execution_context,
        ).evaluate(task, context.candidate_patch),
        extra_cleanup=cleanup_external,
        extra_cleanup_owned=external is not None,
        evidence=case_evidence,
        campaign_version=campaign_version,
    )


def run_live_quixbugs_evaluation(
    *,
    repository_root: str,
    authorization: LiveExecutionAuthorization,
    manifest_path: str,
    sources_parent: str,
    facts: QuixBugsPreflightFacts,
    config: LiveModelConfig,
    limits: LiveRunLimits,
    repetitions: int = 1,
    evaluation_id: Optional[str] = None,
    transport_factory=None,
    policy: DemoPolicy = QUIXBUGS_LIVE_POLICY,
    campaign_version: int = 2,
) -> dict[str, Any]:
    """Run the complete one-task, one-policy, one-repetition QuixBugs live report.

    Structurally restricted to exactly the accepted historical scope: one
    manifest-selected QuixBugs task and exactly one repetition. PDB-on-
    uncertainty here is the historical standalone gcd API: it is locked to
    the gcd task and uses the default gcd runtime probe; any other selected
    task must go through :func:`run_live_quixbugs_case` with its own explicit
    task-local probe.
    """
    if type(authorization) is not LiveExecutionAuthorization:
        raise LiveOptInError("live execution requires explicit authorization")
    if type(repetitions) is not int or repetitions != QUIXBUGS_LIVE_REPETITIONS:
        raise LiveConfigurationError("QuixBugs live evaluation supports exactly one repetition")
    if type(policy) is not DemoPolicy or policy not in {QUIXBUGS_LIVE_POLICY, DemoPolicy.PDB_ON_UNCERTAINTY}:
        raise LiveConfigurationError("unsupported QuixBugs live policy")

    evaluation_id, run_label = _new_evaluation_identity(evaluation_id)
    adapter = QuixBugsAdapter.from_manifest(manifest_path)
    task_id = adapter.manifest.task_id
    if policy is DemoPolicy.PDB_ON_UNCERTAINTY:
        if task_id != QUIXBUGS_PDB_TASK_ID:
            raise LiveConfigurationError(
                f"QuixBugs PDB evaluation is locked to {QUIXBUGS_PDB_TASK_ID!r}"
            )
        _validate_quixbugs_pdb_model_config(config)

    transport = transport_factory() if transport_factory else JsonlCommandTransport(config, max_output_bytes=limits.max_response_bytes)
    case = run_live_quixbugs_case(
        repository_root=repository_root, manifest_path=manifest_path, sources_parent=sources_parent,
        facts=facts, config=config, limits=limits, transport=transport, evaluation_id=evaluation_id, repetition=1,
        policy=policy,
        runtime_probe=(QUIXBUGS_GCD_RUNTIME_PROBE if policy is DemoPolicy.PDB_ON_UNCERTAINTY else None),
        campaign_version=campaign_version,
    )
    case_mapping = case.to_mapping()
    completed = bool(case_mapping["reporting"]["completed"])
    interrupted = bool(case_mapping["reporting"]["interrupted"])
    completion = "interrupted" if interrupted else ("complete" if completed else "partial")
    return {
        "schema_version": LIVE_SCHEMA_VERSION,
        "report_id": evaluation_id,
        "evaluation_id": evaluation_id,
        "run_label": run_label,
        "mode": "live",
        "disposition": "configured_live_execution",
        "completion": completion,
        "model": config.model_name,
        "configuration": config.to_metadata(limits),
        "selected_tasks": [task_id],
        "selected_policies": [policy.value],
        "repetitions": 1,
        "expected_case_count": 1,
        "started_case_count": 1,
        "completed_case_count": 1 if completed else 0,
        "incomplete_case_count": 0 if completed else 1,
        "unstarted_case_count": 0,
        "interrupted": interrupted,
        "evaluation_cleanup": "not_owned",
        "evaluation_cleanup_error": None,
        "cases": [case_mapping],
    }


__all__ = [
    "QUIXBUGS_LIVE_POLICY",
    "QUIXBUGS_LIVE_REPETITIONS",
    "QUIXBUGS_PDB_MODEL_ID",
    "QUIXBUGS_PDB_VARIANT",
    "QUIXBUGS_PDB_PROVIDER",
    "QUIXBUGS_GCD_RUNTIME_PROBE",
    "QuixBugsLiveConfigurationError",
    "run_live_quixbugs_case",
    "run_live_quixbugs_evaluation",
]
