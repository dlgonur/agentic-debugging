"""Live operation lifecycle: observer, case, and evaluation runners.

This module owns execution orchestration: the observer-only controller
operation projection, the curated acceptance case runner (controller
construction, dispatch reconciliation, observable-evidence capture),
and the evaluation loop with cleanup ownership.  Case outcomes always
converge on ``live_finalize._finalize_live_case``."""
from __future__ import annotations

import json, time

from pathlib import Path
from typing import Any, Callable, Mapping
from agentic_debugger.agent.controller import ControllerRunConfig, DeterministicController
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.observer import ControllerObservationKind, NoopControllerObserver
from agentic_debugger.demo.catalog import DemoScenario, scenario_for
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.runner import CURATED_RELATIVE_ROOT
from agentic_debugger.demo.tools import DemoToolContext, build_registry, prepare_pdb_probe
from agentic_debugger.evaluation.runner import bounded_error, load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.runtime.workspace import TaskWorkspace

from agentic_debugger.evaluation.live_adapter import LiveModelAdapter
from agentic_debugger.evaluation.live_contracts import LIVE_SCHEMA_VERSION, LiveCaseStatus, LiveConfigurationError, LiveExecutionAuthorization, LiveOptInError, _new_evaluation_identity, redact_for_recording
from agentic_debugger.evaluation.live_finalize import _finalize_live_case, _interrupted_case_result, _owned_case_dir, _owned_evaluation_dir, _remove_owned_case_dir, _remove_owned_evaluation_dir
from agentic_debugger.evaluation.live_transport import JsonlCommandTransport
from agentic_debugger.evaluation.live_usage import LiveModelMetrics


class _ControllerOperationObserver:
    """Observer-only projection of controller-owned execution boundaries.

    Converts controller-native tool-dispatch and step-completion
    observations into bounded structured operation records.  Ordinary
    exceptions are swallowed (mirroring the controller's own observer
    contract) so telemetry can never alter a decision, budget, step, or
    result.  Records carry tool names, statuses and step ordinals only --
    never model text, prompts, arguments, or observation payloads.  Model
    request boundaries are intentionally not projected here: the transport
    activity channel owns logical request identity (including retries).
    """

    def __init__(self, sink: Callable[[Mapping[str, Any]], None]) -> None:
        self._sink = sink

    def notify(self, observation: Any) -> None:
        try:
            self._notify(observation)
        except Exception:
            pass

    def _notify(self, observation: Any) -> None:
        kind = observation.kind
        if kind is ControllerObservationKind.TOOL_STARTED:
            if observation.tool_name:
                self._sink({
                    "operation": "tool",
                    "phase": "started",
                    "tool": observation.tool_name,
                })
        elif kind is ControllerObservationKind.TOOL_COMPLETED:
            if observation.tool_name:
                status = (
                    observation.observation_status.value
                    if observation.observation_status is not None
                    else "error"
                )
                self._sink({
                    "operation": "tool",
                    "phase": "completed",
                    "tool": observation.tool_name,
                    "status": status,
                })
        elif kind is ControllerObservationKind.STEP_COMPLETED:
            self._sink({
                "operation": "controller_step",
                "step_index": observation.step_index or 0,
                "directive_kind": observation.directive_kind,
            })


def _acceptance_live_case(*,repository_root,task_id,policy,repetition,workspace_parent,config,limits,transport,evaluation_id="local",interactive_debugger_controls=False,retain_observable_model_directives=False,scenario_override=None,progress_observer: Callable[[str], None] | None = None,operation_observer: Callable[[Mapping[str, Any]], None] | None = None):
    repo=Path(repository_root).resolve(); parent=Path(workspace_parent).resolve()
    if scenario_override is None:
        scenario=scenario_for(task_id)
    elif isinstance(scenario_override, DemoScenario) and scenario_override.task_id == task_id:
        scenario=scenario_override
    else:
        raise LiveConfigurationError("scenario override does not match the live task")
    task=load_task(str(repo/CURATED_RELATIVE_ROOT/task_id/"task.json"))
    controller_limits = limits.treatment_budget.controller_limits() if limits.treatment_budget is not None else ControllerBudgetLimits.from_task_constraints(task.constraints)
    model_visible_resource_limits = None
    if limits.treatment_budget is not None:
        model_visible_resource_limits = {
            "max_patch_attempts": controller_limits.max_patch_attempts,
            "max_test_runs": controller_limits.max_test_runs,
            "max_pdb_observations": controller_limits.max_pdb_observations,
        }
    model_visible_task = task.agent_visible_mapping(resource_limits=model_visible_resource_limits)
    case_id=f"{evaluation_id}:{task_id}:{policy.value}:r{repetition}"; run_id=f"live-{case_id}"; started=time.monotonic()
    case_dir=None; workspace=None; context=None; result=None; live_adapter=None; metrics=LiveModelMetrics(); diagnostics=[]; interrupted=False; controller_failed=False
    try:
        case_dir=_owned_case_dir(parent)
        workspace=TaskWorkspace(str(repo/CURATED_RELATIVE_ROOT/task_id),parent_dir=str(case_dir))
        if scenario.runtime_probe.exact_public_reproduction:
            (Path(workspace.root) / "task.json").write_text(
                json.dumps(model_visible_task, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        probe=prepare_pdb_probe(
            repo/CURATED_RELATIVE_ROOT/task_id,
            scenario,
            case_dir,
            model_selects_breakpoint=interactive_debugger_controls,
            task=task,
            model_visible_task_mapping=model_visible_task,
        ) if policy is DemoPolicy.PDB_ON_UNCERTAINTY else None
        context=DemoToolContext(
            task=task,
            workspace=workspace,
            patch="",
            probe=probe,
            # Level-32 model patches are first materialized by the tolerant
            # authorized PatchManager.  Its operator derives the strict
            # official Git artifact from that accepted workspace delta.
            official_patch_compatibility=False,
        )
        registry=build_registry(
            context,
            pdb_policy=pdb_policy_for(policy),
            interactive_debugger_controls=(
                interactive_debugger_controls
                or scenario.runtime_probe.exact_public_reproduction
            ),
        )
        live_adapter=LiveModelAdapter(task=task,policy=policy,config=config,transport=transport,limits=limits,registry=registry,evaluation_id=evaluation_id,case_id=case_id,run_id=run_id,trajectory_id=run_id,proof_required=scenario.runtime_probe.exact_public_reproduction,proof_source_line=scenario.runtime_probe.breakpoint_line if scenario_override is not None else 1,proof_observed_local_names=scenario.runtime_probe.inspect_expressions if scenario.runtime_probe.exact_public_reproduction else (),model_visible_budget_limits=controller_limits,model_visible_task=model_visible_task,progress_observer=progress_observer,operation_observer=operation_observer)
        metrics=live_adapter.metrics
        controller=DeterministicController(
            registry,
            live_adapter,
            ControllerRunConfig(
                max_model_calls=limits.max_controller_steps,
                require_pdb_evidence_before_patch=(
                    scenario.runtime_probe.exact_public_reproduction
                ),
            ),
            observer=(
                _ControllerOperationObserver(operation_observer)
                if operation_observer is not None
                else NoopControllerObserver()
            ),
        )
        try:
            controller_clock_started = time.monotonic()
            result=controller.run(ControllerSnapshot(run_id,task_id,ControllerState.REPRODUCE,0,controller_limits,ControllerBudgetState(),HypothesisLedger()))
            live_adapter.reconcile_tool_dispatch(result)
            metrics.controller_wall_duration_ms = int((time.monotonic() - controller_clock_started) * 1000)
        except KeyboardInterrupt:
            interrupted=True; diagnostics.append("controller interrupted by operator")
        except Exception as exc:
            if 'controller_clock_started' in locals():
                metrics.controller_wall_duration_ms = int((time.monotonic() - controller_clock_started) * 1000)
            controller_failed=True; diagnostics.append(redact_for_recording(bounded_error(exc)))
    except KeyboardInterrupt:
        interrupted=True; diagnostics.append("run interrupted by operator")
    except Exception as exc:
        diagnostics.append(redact_for_recording(bounded_error(exc)))
    observable_evidence = None
    if live_adapter is not None and (retain_observable_model_directives or live_adapter.directive_rejection_evidence):
        observable_evidence = {
            "observable_model_directive_attempts": list(live_adapter.directive_attempts),
            "observable_model_rejection_evidence": list(live_adapter.directive_rejection_evidence),
            "proof_cycle_events": list(live_adapter.proof_cycle_events),
            "observable_model_directives": [
                {
                    "model_call_index": entry.get("request_index"),
                    "state": entry.get("state"),
                    "directive": entry.get("directive"),
                    "last_observation": entry.get("last_observation"),
                }
                for entry in live_adapter.history
                if entry.get("directive") is not None
            ]
        }
    return _finalize_live_case(
        task_id=task_id,policy=policy,repetition=repetition,case_id=case_id,run_id=run_id,config=config,
        task=task,context=context,workspace=workspace,result=result,metrics=metrics,live_adapter=live_adapter,
        started=started,interrupted=interrupted,controller_failed=controller_failed,diagnostics=diagnostics,
        verify=lambda: EvaluationVerifier(str(repo),workspace_parent=str(case_dir)).evaluate(task,context.candidate_patch),
        extra_cleanup=lambda: _remove_owned_case_dir(case_dir,parent),
        extra_cleanup_owned=case_dir is not None,
        evidence=observable_evidence,
    )

run_live_case=_acceptance_live_case

def _acceptance_live_evaluation(*,repository_root,authorization,config,limits,task_ids=None,policies=None,repetitions=1,workspace_parent=None,transport_factory=None,evaluation_id=None,interactive_debugger_controls=False,retain_observable_model_directives=False):
    if type(authorization) is not LiveExecutionAuthorization: raise LiveOptInError("live execution requires explicit authorization")
    if type(repetitions) is not int or not 1<=repetitions<=100: raise LiveConfigurationError("repetitions is invalid")
    evaluation_id,run_label=_new_evaluation_identity(evaluation_id)
    repo=Path(repository_root).resolve()
    available=tuple(sorted(path.name for path in (repo/CURATED_RELATIVE_ROOT).iterdir() if (path/"task.json").is_file()))
    selected=tuple(task_ids) if task_ids is not None else available
    if not selected or set(selected)-set(available): raise LiveConfigurationError("unknown or empty curated task selection")
    if len(set(selected)) != len(selected): raise LiveConfigurationError("duplicate task selection is invalid")
    chosen=tuple(policies) if policies is not None else (DemoPolicy.STATIC_BASELINE,DemoPolicy.PDB_ON_UNCERTAINTY)
    if not chosen or any(type(item) is not DemoPolicy for item in chosen): raise LiveConfigurationError("invalid live policy selection")
    if len(set(chosen)) != len(chosen): raise LiveConfigurationError("duplicate policy selection is invalid")
    expected=len(selected)*len(chosen)*repetitions; owned=workspace_parent is None
    parent=_owned_evaluation_dir() if owned else Path(workspace_parent).resolve()
    cases=[]; interrupted=False; stop=False; evaluation_cleanup_error=None
    try:
        for task_id in selected:
            for policy in chosen:
                for repetition in range(1,repetitions+1):
                    try:
                        transport=transport_factory(load_task(str(repo/CURATED_RELATIVE_ROOT/task_id/"task.json")),policy,repetition) if transport_factory else JsonlCommandTransport(config,max_output_bytes=limits.max_response_bytes)
                        cases.append(run_live_case(repository_root=repo,task_id=task_id,policy=policy,repetition=repetition,workspace_parent=parent,config=config,limits=limits,transport=transport,evaluation_id=evaluation_id,interactive_debugger_controls=interactive_debugger_controls,retain_observable_model_directives=retain_observable_model_directives))
                    except KeyboardInterrupt:
                        interrupted=True; stop=True; cases.append(_interrupted_case_result(task_id,policy,repetition,evaluation_id,"transport setup")); break
                    if cases[-1].status is LiveCaseStatus.INCOMPLETE:
                        interrupted=True; stop=True; break
                    if cases[-1].status not in {LiveCaseStatus.RESOLVED,LiveCaseStatus.UNRESOLVED} and not limits.continue_on_task_failure:
                        stop=True; break
                if stop: break
            if stop: break
    finally:
        if owned:
            try:
                evaluation_removed,evaluation_cleanup_error=_remove_owned_evaluation_dir(parent)
            except KeyboardInterrupt:
                evaluation_removed=False; evaluation_cleanup_error="evaluation cleanup interrupted"; interrupted=True
            except Exception as exc:
                evaluation_removed=False; evaluation_cleanup_error=redact_for_recording(bounded_error(exc))
            if evaluation_cleanup_error and "interrupted" in evaluation_cleanup_error:
                interrupted=True
    started_count=len(cases); completed_count=sum(1 for case in cases if case.reporting.get("completed")); incomplete_count=started_count-completed_count; unstarted_count=expected-started_count
    completion="interrupted" if interrupted else ("partial" if unstarted_count or incomplete_count or evaluation_cleanup_error else "complete")
    return {"schema_version":LIVE_SCHEMA_VERSION,"report_id":evaluation_id,"evaluation_id":evaluation_id,"run_label":run_label,"mode":"live","disposition":"configured_live_execution","completion":completion,"model":config.model_name,"configuration":config.to_metadata(limits),"selected_tasks":list(selected),"selected_policies":[item.value for item in chosen],"repetitions":repetitions,"expected_case_count":expected,"started_case_count":started_count,"completed_case_count":completed_count,"incomplete_case_count":incomplete_count,"unstarted_case_count":unstarted_count,"interrupted":interrupted,"evaluation_cleanup":"failed" if evaluation_cleanup_error else ("cleaned" if owned else "not_owned"),"evaluation_cleanup_error":evaluation_cleanup_error,"cases":[case.to_mapping() for case in cases]}

run_live_evaluation=_acceptance_live_evaluation
