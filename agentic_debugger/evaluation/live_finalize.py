"""Live case/evaluation finalization.

This module owns the terminal layer shared by the curated and QuixBugs
live case pipelines: case/result report types, event projection, owned
directory lifecycle, interruption handling, and the single accepted
``_finalize_live_case`` implementation (verifier invocation, cleanup
accounting, status classification, report assembly)."""
from __future__ import annotations

import io, shutil, tempfile, time, uuid

from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from agentic_debugger.agent.controller import ControllerRunResult, ControllerStopReason
from agentic_debugger.agent.controller_policy import ActionName
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.demo.runner import localization_record
from agentic_debugger.evaluation.runner import bounded_error
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.events.schema import RunEvent

from agentic_debugger.evaluation.live_contracts import LIVE_SCHEMA_VERSION, LiveCaseStatus, LiveConfigurationError, LiveEvaluationError, LiveModelConfig, redact_for_recording
from agentic_debugger.evaluation.live_usage import LiveModelMetrics


@dataclass(frozen=True)
class LiveCaseResult:
    task_id:str; policy:str; repetition:int; status:LiveCaseStatus; controller:dict[str,Any]; verifier:dict[str,Any]; measurements:dict[str,Any]; reporting:dict[str,Any]; events_jsonl:str; diagnostics:tuple[str,...]=(); case_id:str=""; run_id:str=""; trajectory_id:str=""; evidence:dict[str,Any]|None=None
    def to_mapping(self):
        result={"schema_version":LIVE_SCHEMA_VERSION,"case_id":self.case_id,"run_id":self.run_id,"trajectory_id":self.trajectory_id,"task_id":self.task_id,"policy":self.policy,"repetition":self.repetition,"status":self.status.value,"controller":self.controller,"verifier":self.verifier,"measurements":self.measurements,"reporting":self.reporting,"events_jsonl":self.events_jsonl,"diagnostics":list(self.diagnostics)}
        if self.evidence is not None:
            result["evidence"]=self.evidence
        return redact_for_recording(result)

@dataclass(frozen=True)
class RejectedLiveReport:
    reason: str
    def to_mapping(self):
        return {"schema_version":LIVE_SCHEMA_VERSION,"report_id":"rejected-live-evaluation","evaluation_id":None,"run_label":None,"mode":"live","disposition":"attempted_but_rejected","completion":"not_started","model":None,"configuration":None,"selected_tasks":[],"selected_policies":[],"repetitions":0,"expected_case_count":0,"started_case_count":0,"completed_case_count":0,"incomplete_case_count":0,"unstarted_case_count":0,"interrupted":False,"evaluation_cleanup":"not_started","evaluation_cleanup_error":None,"rejection_reason":redact_for_recording(self.reason),"cases":[]}

def rejected_live_report(reason: str) -> RejectedLiveReport:
    return RejectedLiveReport(str(redact_for_recording(reason)))

def _project_events_safe(result: ControllerRunResult, config: LiveModelConfig) -> str:
    stream=io.StringIO()
    logger=JsonlEventLogger(result.run_id,result.task_id,stream=stream)
    try:
        for event in project_controller_run(result,tool_version=config.tool_version,model=config.model_name,timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),duration_ms=None):
            logger.append(RunEvent.from_mapping(redact_for_recording(event.to_mapping())))
        logger.flush()
        return stream.getvalue()
    finally:
        logger.close()

def _owned_case_dir(parent: Path) -> Path:
    if not parent.is_dir(): raise LiveConfigurationError("workspace parent is not an existing directory")
    for _ in range(16):
        path=parent/f"agentic-live-case-{uuid.uuid4().hex}"
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise LiveEvaluationError("live case directory collision limit reached")

def _owned_evaluation_dir() -> Path:
    parent_root=Path(tempfile.gettempdir())
    for _ in range(16):
        path=parent_root/f"agentic-live-evaluation-{uuid.uuid4().hex}"
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise LiveEvaluationError("live evaluation directory collision limit reached")

def _remove_owned_evaluation_dir(path: Path|None) -> tuple[bool,str|None]:
    if path is None: return True,None
    try:
        if path.parent.resolve()!=Path(tempfile.gettempdir()).resolve() or not path.name.startswith("agentic-live-evaluation-"):
            return False,"evaluation directory ownership check failed"
        shutil.rmtree(path)
        return (not path.exists()),None if not path.exists() else "evaluation directory remains"
    except KeyboardInterrupt:
        return False,"evaluation cleanup interrupted"
    except Exception as exc:
        return False,redact_for_recording(bounded_error(exc))

def _remove_owned_case_dir(path: Path|None,parent: Path) -> tuple[bool,str|None]:
    if path is None: return True,None
    try:
        if path.parent.resolve()!=parent.resolve() or not path.name.startswith("agentic-live-case-"): return False,"case directory ownership check failed"
        shutil.rmtree(path)
        return (not path.exists()),None if not path.exists() else "case directory remains"
    except KeyboardInterrupt:
        return False,"case directory cleanup interrupted"
    except Exception as exc:
        return False,redact_for_recording(bounded_error(exc))

def _interrupted_case_result(task_id:str,policy:DemoPolicy,repetition:int,evaluation_id:str,diagnostic:str)->LiveCaseResult:
    case_id=f"{evaluation_id}:{task_id}:{policy.value}:r{repetition}"
    measurements=LiveModelMetrics(termination_reason="interrupted").to_mapping()
    measurements.update({"successful_pdb_observation_count":0,"failed_pdb_observation_count":0,"tool_call_count":0,"case_elapsed_duration_ms":0,"model_phase_elapsed_duration_ms":0,"model_transport_duration_ms":0,"elapsed_scope":"case_observed; model_phase=transport_only"})
    return LiveCaseResult(
        task_id=task_id,
        policy=policy.value,
        repetition=repetition,
        status=LiveCaseStatus.INCOMPLETE,
        controller={"completed":False,"final_state":None,"stop_reason":"interrupted","model_calls":0,"exception":False},
        verifier={"executed":False,"failure":False,"status":None,"outcome":None,"baseline_valid":None,"patch_application":None,"fail_to_pass":None,"pass_to_pass":None,"workspace_cleaned":None,"canonical_fixture_unchanged":None,"localization":{"outcome":"NO_LOCALIZATION"}},
        measurements=measurements,
        reporting={"mode":"live","completed":False,"partial":True,"interrupted":True,"event_recorded":False,"cleanup":"not_started","case_directory_owned":False},
        events_jsonl="",
        diagnostics=("run interrupted before case setup: "+diagnostic,),
        case_id=case_id,
        run_id=f"live-{case_id}",
        trajectory_id=f"live-{case_id}",
    )

def _finalize_live_case(*,task_id,policy,repetition,case_id,run_id,config,task,context,workspace,result,metrics,live_adapter,started,interrupted,controller_failed,diagnostics,verify,extra_cleanup,extra_cleanup_owned,evidence=None,campaign_version=2):
    """Shared verifier/event/cleanup/status/report tail for one live case.

    Both the curated (:func:`_acceptance_live_case`) and QuixBugs
    (:mod:`agentic_debugger.evaluation.live_quixbugs`) live case pipelines
    converge here so verifier invocation, event projection, cleanup
    accounting, status classification, and report assembly stay a single
    accepted implementation rather than two independently maintained copies.
    ``verify`` is a zero-argument callable invoked only under the exact same
    guarded conditions the original curated-only implementation used;
    ``extra_cleanup`` performs the caller-owned case-directory cleanup (a
    local temp directory for curated cases, an owned external WSL workspace
    for QuixBugs cases) and returns ``(removed, error)``.

    ``campaign_version`` selects the versioned terminal-classification
    contract.  Campaigns below v4 keep the frozen classification unchanged
    (a ``pdb-on-uncertainty`` case that completed without PDB stack
    observations classifies as ``PDB_NOT_REACHED`` even when the verifier
    executed).  v4 campaigns classify a case whose verifier executed by the
    verifier semantic outcome (``RESOLVED`` / ``UNRESOLVED``, or
    ``VERIFIER_FAILED`` when the verifier did not complete) before any
    ``PDB_NOT_REACHED`` rule; ``PDB_NOT_REACHED`` then applies only when no
    authoritative verifier result exists.
    """
    verifier=None; verifier_started=False; verifier_failed=False; event_failed=False; events=""
    if result is not None and context is not None and result.final_state is ControllerState.DONE and context.patch_applied and context.candidate_patch:
        try:
            verifier_started=True
            verifier_clock_started = time.monotonic()
            verifier=verify()
        except KeyboardInterrupt:
            interrupted=True; diagnostics.append("verifier interrupted by operator")
        except Exception as exc:
            verifier_failed=True; diagnostics.append(redact_for_recording(bounded_error(exc)))
        finally:
            metrics.verifier_wall_duration_ms = int((time.monotonic() - verifier_clock_started) * 1000)
    elif result is not None and result.final_state is ControllerState.DONE:
        diagnostics.append("controller completed without an accepted patch")
    if result is not None:
        try: events=_project_events_safe(result,config)
        except KeyboardInterrupt:
            interrupted=True; diagnostics.append("event projection interrupted by operator")
        except Exception as exc:
            event_failed=True; diagnostics.append(redact_for_recording(bounded_error(exc)))
    cleanup_errors=[]
    if context is not None:
        try: cleanup_errors.extend(redact_for_recording(bounded_error(exc)) for exc in context.release_pdb())
        except KeyboardInterrupt:
            interrupted=True; cleanup_errors.append("PDB cleanup interrupted")
        except Exception as exc: cleanup_errors.append(redact_for_recording(bounded_error(exc)))
    if workspace is not None:
        try:
            workspace.cleanup()
            if Path(workspace.root).exists(): cleanup_errors.append("task workspace root remains after cleanup")
        except KeyboardInterrupt:
            interrupted=True; cleanup_errors.append("task workspace cleanup interrupted")
        except Exception as exc: cleanup_errors.append(redact_for_recording(bounded_error(exc)))
    try:
        case_removed,case_error=extra_cleanup()
    except KeyboardInterrupt:
        case_removed=False; case_error="case cleanup interrupted"; interrupted=True
    except Exception as exc:
        case_removed=False; case_error=redact_for_recording(bounded_error(exc))
    if case_error:
        cleanup_errors.append(case_error)
        if "interrupted" in case_error:
            interrupted=True
    diagnostics.extend(cleanup_errors)
    if interrupted: metrics.termination_reason="interrupted"
    if interrupted: status=LiveCaseStatus.INCOMPLETE
    elif cleanup_errors: status=LiveCaseStatus.CLEANUP_FAILED
    elif event_failed: status=LiveCaseStatus.EVENT_REPORTING_FAILED
    elif verifier_failed: status=LiveCaseStatus.VERIFIER_FAILED
    elif metrics.termination_reason=="public_evidence_budget_exceeded" and metrics.model_responses>=1:
        # Historical compatibility: frozen campaign evidence may carry the
        # pre-Task-43 budget-exhausted termination reason.  New execution
        # never sets it (request size is provider-owned).  The pre-PDB
        # completed-response shape is terminalized as PDB_NOT_REACHED with the
        # completed-response terminal transport evidence bound to the last
        # completed provider response.  When the controller reached Patch and
        # applied a candidate but the next public request would have exceeded
        # the budget before the transition to Validate, the case is
        # terminalized as VALIDATION_NOT_REACHED (the verifier never ran).
        # Zero-contact budget stops keep the existing fail-closed
        # infrastructure classification below.
        if context is not None and context.patch_applied and context.candidate_patch and result is not None and result.final_state is not ControllerState.DONE:
            status=LiveCaseStatus.VALIDATION_NOT_REACHED
        else:
            status=LiveCaseStatus.PDB_NOT_REACHED
    elif metrics.termination_reason in {"model_request_limit","controller_step_limit"} or (result is not None and result.stop_reason is ControllerStopReason.MODEL_CALL_LIMIT): status=LiveCaseStatus.BUDGET_LIMITED
    elif metrics.termination_reason in {"request_timeout","elapsed_time_limit"}: status=LiveCaseStatus.TIMED_OUT
    elif metrics.termination_reason == "directive_rejected": status=LiveCaseStatus.MODEL_DIRECTIVE_REJECTED
    elif metrics.termination_reason == "provider_or_transport_error": status=LiveCaseStatus.PROVIDER_ERROR
    elif controller_failed: status=LiveCaseStatus.CONTROLLER_FAILED
    elif result is None: status=LiveCaseStatus.HARNESS_ERROR
    elif result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED: status=LiveCaseStatus.CONTROLLER_REJECTED
    elif result.final_state is not ControllerState.DONE: status=LiveCaseStatus.CONTROLLER_FAILED
    elif campaign_version >= 4 and verifier is not None:
        # v4 verifier-authoritative classification: a case whose independent
        # verifier executed is classified by the verifier semantic outcome
        # before any PDB_NOT_REACHED rule.  A verifier that ran but did not
        # complete stays an honest VERIFIER_FAILED infrastructure outcome.
        if verifier.status.value != "COMPLETED": status=LiveCaseStatus.VERIFIER_FAILED
        elif verifier.outcome is not None and verifier.outcome.value=="RESOLVED": status=LiveCaseStatus.RESOLVED
        else: status=LiveCaseStatus.UNRESOLVED
    elif policy is DemoPolicy.PDB_ON_UNCERTAINTY and not any(
        step.action and step.action.name in {
            ActionName.GET_STACK_SUMMARY.value,
            ActionName.GET_FRAME_LOCALS.value,
            ActionName.SAFE_EVAL_EXPRESSION.value,
            ActionName.CONTINUE_PDB_SESSION.value,
            ActionName.STEP_PDB_SESSION.value,
            ActionName.NEXT_PDB_SESSION.value,
        }
        and step.observation and step.observation.status.value == "ok"
        for step in (result.steps if result is not None else ())
    ): status=LiveCaseStatus.PDB_NOT_REACHED
    elif verifier is None: status=LiveCaseStatus.UNRESOLVED
    elif verifier.status.value!="COMPLETED": status=LiveCaseStatus.VERIFIER_FAILED
    elif verifier.outcome is not None and verifier.outcome.value=="RESOLVED": status=LiveCaseStatus.RESOLVED
    else: status=LiveCaseStatus.UNRESOLVED
    controller_data={"completed":bool(result and result.final_state is ControllerState.DONE),"final_state":result.final_state.value if result else None,"stop_reason":result.stop_reason.value if result else ("controller_exception" if controller_failed else ("interrupted" if interrupted else None)),"model_calls":result.model_calls if result else metrics.model_requests,"exception":controller_failed}
    verification={"executed":verifier_started and verifier is not None,"failure":verifier_failed,"status":verifier.status.value if verifier else None,"outcome":verifier.outcome.value if verifier and verifier.outcome else None,"baseline_valid":verifier.baseline.valid if verifier else None,"patch_application":verifier.patch_application.to_mapping() if verifier else None,"fail_to_pass":{"passed":verifier.f2p_passed,"total":verifier.f2p_total} if verifier else None,"pass_to_pass":{"passed":verifier.p2p_passed,"total":verifier.p2p_total} if verifier else None,"workspace_cleaned":verifier.workspace.cleaned if verifier else None,"canonical_fixture_unchanged":verifier.workspace.canonical_fixture_unchanged if verifier else None}
    verification["localization"]=localization_record(context.declared_localization,context.patch_changed_files,context.patch_applied,task.oracle.target_files,task.oracle.target_symbols) if context else {"outcome":"NO_LOCALIZATION"}
    pdb_success=0; pdb_failed=0
    if result is not None:
        for step in result.steps:
            if step.action and step.action.name in {
                ActionName.GET_STACK_SUMMARY,
                ActionName.GET_FRAME_LOCALS,
                ActionName.SAFE_EVAL_EXPRESSION,
                ActionName.CONTINUE_PDB_SESSION,
                ActionName.STEP_PDB_SESSION,
                ActionName.NEXT_PDB_SESSION,
            }:
                if step.observation and step.observation.status.value=="ok": pdb_success+=1
                else: pdb_failed+=1
    model_phase_ms=int(live_adapter.model_phase_elapsed_seconds*1000) if live_adapter else 0
    measurements=metrics.to_mapping(); measurements.update({"successful_pdb_observation_count":pdb_success,"failed_pdb_observation_count":pdb_failed,"tool_call_count":len(context.tool_calls) if context else 0,"case_elapsed_duration_ms":int((time.monotonic()-started)*1000),"model_phase_elapsed_duration_ms":model_phase_ms,"model_transport_duration_ms":model_phase_ms,"elapsed_scope":"case_observed; model_phase=transport_only"})
    completed=status not in {LiveCaseStatus.INCOMPLETE,LiveCaseStatus.CLEANUP_FAILED}
    reporting={"mode":"live","completed":completed,"partial":not completed,"interrupted":interrupted,"event_recorded":bool(events),"cleanup":"cleaned" if case_removed and not cleanup_errors else "failed","case_directory_owned":extra_cleanup_owned}
    return LiveCaseResult(
        task_id=task_id,
        policy=policy.value,
        repetition=repetition,
        status=status,
        controller=controller_data,
        verifier=verification,
        measurements=measurements,
        reporting=reporting,
        events_jsonl=events,
        diagnostics=tuple(diagnostics),
        case_id=case_id,
        run_id=run_id,
        trajectory_id=run_id,
        evidence=evidence,
    )
