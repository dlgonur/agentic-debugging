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
used to run untrusted candidate code. Policy is hard-locked to
``DemoPolicy.STATIC_BASELINE`` -- there is no parameter that selects
``pdb-on-uncertainty`` for a QuixBugs live case, so PDB can neither be
requested nor silently substituted. Source acquisition never clones or
resets the pinned checkout: a missing or non-pinned checkout fails closed
rather than acquiring one.
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
from agentic_debugger.runtime.workspace import TaskWorkspace


class QuixBugsLiveConfigurationError(LiveConfigurationError):
    """A QuixBugs live-case precondition (manifest, source, context,
    authorization) is unmet; the case fails closed before any provider or
    WSL/Bubblewrap contact."""


#: The only policy a QuixBugs live case may run under. There is no PDB probe
#: infrastructure for QuixBugs tasks, and static policy must remain genuinely
#: static, so this is a module constant rather than an accepted parameter.
QUIXBUGS_LIVE_POLICY = DemoPolicy.STATIC_BASELINE

#: This integration proves the smallest coherent feasibility path for exactly
#: one QuixBugs task and one repetition; a caller asking for anything else is
#: a configuration error, not a silently-widened scope.
QUIXBUGS_LIVE_REPETITIONS = 1


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
) -> LiveCaseResult:
    """Run exactly one QuixBugs live case through the accepted live pipeline.

    Fails closed (returns a :class:`LiveCaseStatus.HARNESS_ERROR` case with
    no provider/WSL contact) when: the manifest is invalid, the QuixBugs
    preflight gate is not authorized, the pinned source checkout is missing
    or does not match the pinned revision, or no verified execution context
    was supplied. The pinned checkout is only ever re-verified here, never
    cloned, reset, or cleaned.
    """
    if type(facts) is not QuixBugsPreflightFacts:
        raise QuixBugsLiveConfigurationError("QuixBugs preflight facts are required")
    if facts.execution_context is None:
        raise QuixBugsLiveConfigurationError("QuixBugs live case requires a verified execution context")
    if type(repetition) is not int or repetition != QUIXBUGS_LIVE_REPETITIONS:
        raise QuixBugsLiveConfigurationError("QuixBugs live case supports exactly one repetition")

    adapter = QuixBugsAdapter.from_manifest(manifest_path)
    task_id = adapter.manifest.task_id
    policy = QUIXBUGS_LIVE_POLICY
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

        smoke = QuixBugsSmokeRunner(adapter, QuixBugsSourceAcquirer())
        discovery_workspace = TaskWorkspace(str(project_root), parent_dir=str(external.verifier_workspace_parent))
        try:
            discovery = smoke.discover(facts.execution_context, discovery_workspace)
        finally:
            discovery_workspace.cleanup()

        commands = adapter.build_commands(fail_to_pass=discovery.f2p_candidates, pass_to_pass=discovery.p2p_candidates)
        source = TaskSource("external", "quixbugs", adapter.source_provenance())
        task = adapter.to_debug_task(source, commands)

        workspace = TaskWorkspace(str(project_root), parent_dir=str(external.verifier_workspace_parent))
        context = DemoToolContext(
            task=task, workspace=workspace, patch="", probe=None, execution_context=facts.execution_context,
        )
        registry = build_registry(context, pdb_policy=pdb_policy_for(policy))
        live_adapter = LiveModelAdapter(
            task=task, policy=policy, config=config, transport=transport, limits=limits, registry=registry,
            evaluation_id=evaluation_id, case_id=case_id, run_id=run_id, trajectory_id=run_id,
        )
        metrics = live_adapter.metrics
        controller = DeterministicController(registry, live_adapter, ControllerRunConfig(max_model_calls=limits.max_controller_steps))
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

    return _finalize_live_case(
        task_id=task_id, policy=policy, repetition=repetition, case_id=case_id, run_id=run_id, config=config,
        task=task, context=context, workspace=workspace, result=result, metrics=metrics, live_adapter=live_adapter,
        started=started, interrupted=interrupted, controller_failed=controller_failed, diagnostics=diagnostics,
        verify=lambda: EvaluationVerifier(
            str(sources_dir), workspace_parent=str(external.verifier_workspace_parent), execution_context=facts.execution_context,
        ).evaluate(task, context.candidate_patch),
        extra_cleanup=cleanup_external,
        extra_cleanup_owned=external is not None,
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
) -> dict[str, Any]:
    """Run the complete one-task, one-policy, one-repetition QuixBugs live report.

    Structurally restricted to exactly the accepted feasibility scope: one
    manifest-selected QuixBugs task, ``static-baseline`` only, and exactly
    one repetition. There is no parameter that can widen this to additional
    tasks, policies, or repetitions.
    """
    if type(authorization) is not LiveExecutionAuthorization:
        raise LiveOptInError("live execution requires explicit authorization")
    if type(repetitions) is not int or repetitions != QUIXBUGS_LIVE_REPETITIONS:
        raise LiveConfigurationError("QuixBugs live evaluation supports exactly one repetition")

    evaluation_id, run_label = _new_evaluation_identity(evaluation_id)
    adapter = QuixBugsAdapter.from_manifest(manifest_path)
    task_id = adapter.manifest.task_id
    policy = QUIXBUGS_LIVE_POLICY

    transport = transport_factory() if transport_factory else JsonlCommandTransport(config, max_output_bytes=limits.max_response_bytes)
    case = run_live_quixbugs_case(
        repository_root=repository_root, manifest_path=manifest_path, sources_parent=sources_parent,
        facts=facts, config=config, limits=limits, transport=transport, evaluation_id=evaluation_id, repetition=1,
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
    "QuixBugsLiveConfigurationError",
    "run_live_quixbugs_case",
    "run_live_quixbugs_evaluation",
]
