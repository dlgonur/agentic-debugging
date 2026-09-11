"""Live report serialization and validation.

This module owns the report boundary: human-readable rendering and
the fail-closed ``validate_live_report`` schema authority (case,
configuration, count, identity, and cross-field consistency checks).
``LiveConfigurationError`` stays the single schema-failure signal."""
from __future__ import annotations

import re

from dataclasses import field
from typing import Any, Mapping
from agentic_debugger.agent.controller import ControllerStopReason

from agentic_debugger.evaluation.live_contracts import LIVE_CONFIG_SCHEMA_VERSION, LIVE_PROTOCOL_VERSION, LIVE_SCHEMA_VERSION, LiveCaseStatus, LiveConfigurationError, redact_for_recording
from agentic_debugger.evaluation.live_finalize import RejectedLiveReport


def render_live_report(report):
    payload=report.to_mapping() if isinstance(report,RejectedLiveReport) else dict(report)
    payload=redact_for_recording(payload)
    configuration=payload.get("configuration") or {}
    lines=["Task 10A real-model evaluation",f"schema: {payload.get('schema_version',LIVE_SCHEMA_VERSION)}",f"mode: {payload.get('mode','unknown')}",f"disposition: {payload.get('disposition','unknown')}",f"completion: {payload.get('completion','unknown')}",f"evaluation_id: {payload.get('evaluation_id') or 'none'}",f"model: {payload.get('model') or 'unknown'}",f"configuration_fingerprint: {configuration.get('configuration_fingerprint') or 'none'}",f"cases: {payload.get('completed_case_count',len(payload.get('cases',[])))}/{payload.get('expected_case_count',len(payload.get('cases',[])))}"]
    if payload.get("evaluation_cleanup") == "failed": lines.append("evaluation cleanup: failed")
    if payload.get("rejection_reason"): lines.append(f"rejection: {payload['rejection_reason']}")
    for case in payload.get("cases",[]):
        measurements=case.get("measurements",{}); usage=measurements.get("token_usage",{})
        lines.append(f"- {case.get('case_id',case.get('task_id'))} status={case.get('status')} requests={measurements.get('model_request_count')} retries={measurements.get('retry_count')} tokens={usage.get('total_tokens')} termination={measurements.get('termination_reason')}")
    return "\n".join(lines)+"\n"

def _schema_error(message: str):
    raise LiveConfigurationError("live report schema is invalid: "+message)

def _require_fields(value: Mapping[str,Any], fields: tuple[str,...], label: str):
    missing=[field for field in fields if field not in value]
    if missing:
        _schema_error(f"{label} is missing fields: {','.join(missing)}")

def _string(value: Any, label: str, *, nullable: bool=False):
    if nullable and value is None:
        return
    if type(value) is not str or not value:
        _schema_error(f"{label} must be a non-empty string")

def _boolean(value: Any, label: str):
    if type(value) is not bool:
        _schema_error(f"{label} must be boolean")

def _count(value: Any, label: str):
    if type(value) is not int or value < 0:
        _schema_error(f"{label} must be a non-negative integer")

def _optional_mapping(value: Any, label: str):
    if value is not None and not isinstance(value,Mapping):
        _schema_error(f"{label} must be an object or null")

def _validate_counter_pair(value: Any, label: str):
    if not isinstance(value,Mapping):
        _schema_error(f"{label} must be an object")
    _require_fields(value,("passed","total"),label)
    _count(value["passed"],label+".passed")
    _count(value["total"],label+".total")
    if value["passed"] > value["total"]:
        _schema_error(f"{label}.passed exceeds total")

def _validate_case(case: Any):
    if not isinstance(case,Mapping):
        _schema_error("case must be an object")
    required=("schema_version","case_id","run_id","trajectory_id","task_id","policy","repetition","status","controller","verifier","measurements","reporting","events_jsonl","diagnostics")
    _require_fields(case,required,"case")
    if case["schema_version"] != LIVE_SCHEMA_VERSION:
        _schema_error("case schema version is unsupported")
    for field in ("case_id","run_id","trajectory_id","task_id","policy"):
        _string(case[field],"case."+field)
    if type(case["repetition"]) is not int or case["repetition"] < 1:
        _schema_error("case.repetition must be a positive integer")
    status=case["status"]
    if status not in {item.value for item in LiveCaseStatus}:
        _schema_error("case.status is unsupported")
    controller=case["controller"]
    if not isinstance(controller,Mapping):
        _schema_error("case.controller must be an object")
    _require_fields(controller,("completed","final_state","stop_reason","model_calls","exception"),"case.controller")
    _boolean(controller["completed"],"case.controller.completed")
    _string(controller["final_state"],"case.controller.final_state",nullable=True)
    _string(controller["stop_reason"],"case.controller.stop_reason",nullable=True)
    _count(controller["model_calls"],"case.controller.model_calls")
    _boolean(controller["exception"],"case.controller.exception")
    verifier=case["verifier"]
    if not isinstance(verifier,Mapping):
        _schema_error("case.verifier must be an object")
    _require_fields(verifier,("executed","failure","status","outcome","baseline_valid","patch_application","fail_to_pass","pass_to_pass","workspace_cleaned","canonical_fixture_unchanged","localization"),"case.verifier")
    for field in ("executed","failure"):
        _boolean(verifier[field],"case.verifier."+field)
    for field in ("status","outcome"):
        _string(verifier[field],"case.verifier."+field,nullable=True)
    for field in ("baseline_valid","workspace_cleaned","canonical_fixture_unchanged"):
        if verifier[field] is not None:
            _boolean(verifier[field],"case.verifier."+field)
    for field in ("patch_application","fail_to_pass","pass_to_pass"):
        _optional_mapping(verifier[field],"case.verifier."+field)
    _optional_mapping(verifier["localization"],"case.verifier.localization")
    measurements=case["measurements"]
    if not isinstance(measurements,Mapping):
        _schema_error("case.measurements must be an object")
    measurement_fields=("model_request_count","model_response_count","retry_count","provider_error_count","provider_error_kinds","directive_rejection_count","directive_rejection_categories","token_usage","termination_reason","successful_pdb_observation_count","failed_pdb_observation_count","tool_call_count","case_elapsed_duration_ms","model_phase_elapsed_duration_ms","model_transport_duration_ms","elapsed_scope")
    _require_fields(measurements,measurement_fields,"case.measurements")
    for field in ("model_request_count","model_response_count","retry_count","provider_error_count","successful_pdb_observation_count","failed_pdb_observation_count","tool_call_count","case_elapsed_duration_ms","model_phase_elapsed_duration_ms","model_transport_duration_ms"):
        _count(measurements[field],"case.measurements."+field)
    for field in ("stream_frame_count","thinking_bytes","action_content_bytes"):
        if field in measurements:
            _count(measurements[field],"case.measurements."+field)
    if not isinstance(measurements["provider_error_kinds"],list) or any(type(item) is not str or not item for item in measurements["provider_error_kinds"]):
        _schema_error("case.measurements.provider_error_kinds must be a string array")
    _count(measurements["directive_rejection_count"],"case.measurements.directive_rejection_count")
    if not isinstance(measurements["directive_rejection_categories"],list) or any(type(item) is not str or not item for item in measurements["directive_rejection_categories"]):
        _schema_error("case.measurements.directive_rejection_categories must be a string array")
    _string(measurements["termination_reason"],"case.measurements.termination_reason",nullable=True)
    _string(measurements["elapsed_scope"],"case.measurements.elapsed_scope")
    if measurements["model_phase_elapsed_duration_ms"] != measurements["model_transport_duration_ms"] or measurements["elapsed_scope"] != "case_observed; model_phase=transport_only":
        _schema_error("model timing scope is inconsistent")
    usage=measurements["token_usage"]
    if not isinstance(usage,Mapping):
        _schema_error("case.measurements.token_usage must be an object")
    _require_fields(usage,("prompt_tokens","completion_tokens","total_tokens","provider_reported","missing_fields"),"case.measurements.token_usage")
    for field in ("prompt_tokens","completion_tokens","total_tokens"):
        if usage[field] is not None:
            _count(usage[field],"case.measurements.token_usage."+field)
    for field in ("cached_input_tokens","cache_write_input_tokens"):
        if field in usage and usage[field] is not None:
            _count(usage[field],"case.measurements.token_usage."+field)
    _boolean(usage["provider_reported"],"case.measurements.token_usage.provider_reported")
    if not isinstance(usage["missing_fields"],list) or any(type(item) is not str or not item for item in usage["missing_fields"]):
        _schema_error("case.measurements.token_usage.missing_fields must be a string array")
    reporting=case["reporting"]
    if not isinstance(reporting,Mapping):
        _schema_error("case.reporting must be an object")
    _require_fields(reporting,("mode","completed","partial","interrupted","event_recorded","cleanup","case_directory_owned"),"case.reporting")
    _string(reporting["mode"],"case.reporting.mode")
    if reporting["mode"] != "live":
        _schema_error("case.reporting.mode is unsupported")
    for field in ("completed","partial","interrupted","event_recorded","case_directory_owned"):
        _boolean(reporting[field],"case.reporting."+field)
    if reporting["partial"] != (not reporting["completed"]):
        _schema_error("case.reporting completed/partial values are inconsistent")
    if reporting["cleanup"] not in {"cleaned","failed","not_started"}:
        _schema_error("case.reporting.cleanup is unsupported")
    if type(case["events_jsonl"]) is not str:
        _schema_error("case.events_jsonl must be a string")
    if not isinstance(case["diagnostics"],list) or any(type(item) is not str for item in case["diagnostics"]):
        _schema_error("case.diagnostics must be a string array")
    if reporting["event_recorded"] != bool(case["events_jsonl"]):
        _schema_error("case event reporting is inconsistent")
    if reporting["cleanup"] == "not_started" and reporting["case_directory_owned"]:
        _schema_error("case cleanup was not started for an owned directory")
    if reporting["cleanup"] == "failed" and reporting["completed"]:
        _schema_error("cleanup failure cannot be a completed case")
    if status == LiveCaseStatus.RESOLVED.value and not (reporting["completed"] and controller["completed"] and verifier["executed"] and verifier["outcome"] == "RESOLVED" and not reporting["interrupted"]):
        _schema_error("resolved case state is inconsistent")
    if status == LiveCaseStatus.INCOMPLETE.value and not (not reporting["completed"] and reporting["partial"] and reporting["interrupted"] and measurements["termination_reason"] == "interrupted"):
        _schema_error("incomplete case state is inconsistent")
    if status == LiveCaseStatus.CLEANUP_FAILED.value and not (not reporting["completed"] and reporting["partial"] and reporting["cleanup"] == "failed"):
        _schema_error("cleanup-failed case state is inconsistent")
    if reporting["interrupted"] and status != LiveCaseStatus.INCOMPLETE.value:
        _schema_error("interrupted case has a non-incomplete status")
    if status == LiveCaseStatus.PROVIDER_ERROR and not (measurements["provider_error_count"] > 0 and measurements["termination_reason"] == "provider_or_transport_error"):
        _schema_error("provider-error case measurements are inconsistent")
    if status == LiveCaseStatus.MODEL_DIRECTIVE_REJECTED.value and not (measurements["provider_error_count"] == 0 and measurements["directive_rejection_count"] > 0 and measurements["termination_reason"] == "directive_rejected"):
        _schema_error("model-directive-rejected case measurements are inconsistent")
    if status == LiveCaseStatus.TIMED_OUT and measurements["termination_reason"] not in {"request_timeout","elapsed_time_limit"}:
        _schema_error("timed-out case termination is inconsistent")
    if status == LiveCaseStatus.BUDGET_LIMITED and measurements["termination_reason"] not in {"model_request_limit","controller_step_limit"} and controller["stop_reason"] != ControllerStopReason.MODEL_CALL_LIMIT.value:
        _schema_error("budget-limited case termination is inconsistent")
    if status == LiveCaseStatus.CONTROLLER_REJECTED and controller["stop_reason"] != ControllerStopReason.DIRECTIVE_REJECTED.value:
        _schema_error("controller-rejected case state is inconsistent")
    if status == LiveCaseStatus.EVENT_REPORTING_FAILED and reporting["event_recorded"]:
        _schema_error("event-reporting failure has recorded events")
    if status == LiveCaseStatus.VERIFIER_FAILED and not verifier["failure"] and verifier.get("status") == "COMPLETED":
        _schema_error("verifier-failed case state is inconsistent")

def _validate_configuration_metadata(value: Any):
    if not isinstance(value,Mapping):
        _schema_error("report.configuration must be an object")
    required=("schema_version","protocol_version","model_name","tool_version","configuration_fingerprint","request_timeout_seconds","continue_on_task_failure","limits")
    _require_fields(value,required,"report.configuration")
    if value["schema_version"] != LIVE_CONFIG_SCHEMA_VERSION or value["protocol_version"] != LIVE_PROTOCOL_VERSION:
        _schema_error("report.configuration schema/protocol version is unsupported")
    _string(value["model_name"],"report.configuration.model_name")
    _string(value["tool_version"],"report.configuration.tool_version")
    if type(value["configuration_fingerprint"]) is not str or not re.fullmatch(r"[0-9a-f]{64}",value["configuration_fingerprint"]):
        _schema_error("report.configuration fingerprint is invalid")
    # Level-32's evidence-backed DeepSeek profile uses the same 3,600-second
    # outer model-phase bound accepted by LiveModelConfig.  Lower-rung cases
    # retain their existing, smaller values; this is only the schema ceiling.
    if type(value["request_timeout_seconds"]) not in (int,float) or not 0 < value["request_timeout_seconds"] <= 3600:
        _schema_error("report.configuration request timeout is invalid")
    _boolean(value["continue_on_task_failure"],"report.configuration.continue_on_task_failure")
    limits=value["limits"]
    if not isinstance(limits,Mapping):
        _schema_error("report.configuration.limits must be an object")
    limit_fields=("max_model_requests","max_controller_steps","max_model_phase_seconds","max_retries","max_response_bytes","continue_on_task_failure")
    _require_fields(limits,limit_fields,"report.configuration.limits")
    for field in limit_fields[:-1]:
        _count(limits[field],"report.configuration.limits."+field)
    _boolean(limits["continue_on_task_failure"],"report.configuration.limits.continue_on_task_failure")
    if limits["continue_on_task_failure"] != value["continue_on_task_failure"]:
        _schema_error("report configuration failure policy is inconsistent")

def validate_live_report(report):
    payload=report.to_mapping() if isinstance(report,RejectedLiveReport) else report
    if not isinstance(payload,Mapping):
        _schema_error("report must be an object")
    required=("schema_version","report_id","evaluation_id","run_label","mode","disposition","completion","model","configuration","selected_tasks","selected_policies","repetitions","expected_case_count","started_case_count","completed_case_count","incomplete_case_count","unstarted_case_count","interrupted","evaluation_cleanup","evaluation_cleanup_error","cases")
    _require_fields(payload,required,"report")
    if payload["schema_version"] != LIVE_SCHEMA_VERSION:
        _schema_error("report schema version is unsupported")
    for field in ("report_id","mode","disposition","completion","evaluation_cleanup"):
        _string(payload[field],"report."+field)
    _string(payload["evaluation_id"],"report.evaluation_id",nullable=True)
    _string(payload["run_label"],"report.run_label",nullable=True)
    if payload["mode"] != "live":
        _schema_error("report.mode is unsupported")
    if payload["disposition"] not in {"configured_live_execution","attempted_but_rejected"}:
        _schema_error("report.disposition is unsupported")
    if payload["completion"] not in {"complete","partial","interrupted","not_started"}:
        _schema_error("report.completion is unsupported")
    if payload["model"] is not None:
        _string(payload["model"],"report.model")
    if not isinstance(payload["selected_tasks"],list) or any(type(item) is not str or not item for item in payload["selected_tasks"]):
        _schema_error("report.selected_tasks must be a string array")
    if not isinstance(payload["selected_policies"],list) or any(type(item) is not str or not item for item in payload["selected_policies"]):
        _schema_error("report.selected_policies must be a string array")
    if len(set(payload["selected_tasks"])) != len(payload["selected_tasks"]):
        _schema_error("report.selected_tasks are not unique")
    if len(set(payload["selected_policies"])) != len(payload["selected_policies"]):
        _schema_error("report.selected_policies are not unique")
    if type(payload["repetitions"]) is not int or payload["repetitions"] < 0:
        _schema_error("report.repetitions must be a non-negative integer")
    for field in ("expected_case_count","started_case_count","completed_case_count","incomplete_case_count","unstarted_case_count"):
        _count(payload[field],"report."+field)
    _boolean(payload["interrupted"],"report.interrupted")
    if payload["evaluation_cleanup"] not in {"cleaned","failed","not_owned","not_started"}:
        _schema_error("report.evaluation_cleanup is unsupported")
    if payload["evaluation_cleanup_error"] is not None:
        _string(payload["evaluation_cleanup_error"],"report.evaluation_cleanup_error")
    cases=payload["cases"]
    if not isinstance(cases,list):
        _schema_error("report.cases must be an array")
    expected=payload["expected_case_count"]; started=payload["started_case_count"]; completed=payload["completed_case_count"]; incomplete=payload["incomplete_case_count"]; unstarted=payload["unstarted_case_count"]
    if started != len(cases) or started > expected or completed + incomplete != started or unstarted != expected-started:
        _schema_error("report case counts are inconsistent")
    if payload["disposition"] == "attempted_but_rejected":
        if not (payload["evaluation_id"] is None and payload["run_label"] is None and payload["configuration"] is None and payload["model"] is None and payload["completion"] == "not_started" and payload["repetitions"] == 0 and not payload["selected_tasks"] and not payload["selected_policies"] and expected == started == completed == incomplete == unstarted == 0 and not payload["interrupted"] and payload["evaluation_cleanup"] == "not_started" and not cases):
            _schema_error("rejected report state is inconsistent")
        _string(payload.get("rejection_reason"),"report.rejection_reason")
        return payload
    if payload["evaluation_id"] is None or payload["evaluation_id"] != payload["report_id"] or payload["model"] is None or not payload["selected_tasks"] or not payload["selected_policies"] or payload["repetitions"] < 1:
        _schema_error("configured report is missing execution identity")
    if payload["completion"] == "not_started":
        _schema_error("configured report cannot use not_started completion")
    _validate_configuration_metadata(payload["configuration"])
    if expected != len(payload["selected_tasks"])*len(payload["selected_policies"])*payload["repetitions"]:
        _schema_error("report expected case count is inconsistent")
    if payload["completion"] == "interrupted" and not payload["interrupted"]:
        _schema_error("interrupted completion requires interrupted flag")
    if payload["interrupted"] and payload["completion"] != "interrupted":
        _schema_error("interrupted report has invalid completion")
    if payload["interrupted"] and not any(isinstance(case,Mapping) and isinstance(case.get("reporting"),Mapping) and case["reporting"].get("interrupted") is True for case in cases):
        if not (payload["evaluation_cleanup_error"] and "interrupted" in payload["evaluation_cleanup_error"]):
            _schema_error("interrupted report has no interrupted case or cleanup event")
    if payload["completion"] == "complete" and (payload["interrupted"] or unstarted or incomplete or payload["evaluation_cleanup"] == "failed"):
        _schema_error("complete report has incomplete execution or cleanup")
    if payload["completion"] == "partial" and not (unstarted or incomplete or payload["evaluation_cleanup"] == "failed"):
        _schema_error("partial report has no partial condition")
    identities={"case_id":set(),"run_id":set(),"trajectory_id":set()}; combinations=set()
    for case in cases:
        _validate_case(case)
        combination=(case["task_id"],case["policy"],case["repetition"])
        if combination in combinations or combination[0] not in payload["selected_tasks"] or combination[1] not in payload["selected_policies"] or not 1 <= combination[2] <= payload["repetitions"]:
            _schema_error("case task/policy/repetition coverage is inconsistent")
        combinations.add(combination)
        for field in identities:
            if case[field] in identities[field]:
                _schema_error("case identities are not unique")
            identities[field].add(case[field])
    if sum(1 for case in cases if case["reporting"]["completed"]) != completed:
        _schema_error("report completed count is inconsistent")
    return payload
