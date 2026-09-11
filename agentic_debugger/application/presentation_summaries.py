"""Bounded human-readable event summaries for the presentation projection.

``summarize_event`` returns one bounded, deterministic activity summary per
event kind.  These strings are presentation text derived exclusively from
already-validated event payloads; they are never evidence and never parsed
back by the reducer (typed fields exist on ``SessionViewState`` for that).

Dependency rule: pure text derivation over validated events.  No I/O, no
state, no controller/verifier/PDB/patch/model code.
"""

from __future__ import annotations

from agentic_debugger.application import ApplicationContractError
from agentic_debugger.application.event_contracts import SessionEventKind
from agentic_debugger.application.events import SessionEvent

MAX_TIMELINE_SUMMARY_CHARS = 240


def _trim_summary(text: str) -> str:
    if len(text) <= MAX_TIMELINE_SUMMARY_CHARS:
        return text
    return text[: MAX_TIMELINE_SUMMARY_CHARS - 3] + "..."


def summarize_event(event: SessionEvent) -> str:
    """Return one bounded, human-readable activity summary for an event."""
    kind = event.event_kind
    payload = event.payload
    if kind is SessionEventKind.SESSION_CREATED:
        return "session created"
    if kind is SessionEventKind.SESSION_STARTED:
        return "session started"
    if kind is SessionEventKind.SESSION_STATUS_CHANGED:
        phase = str(payload.get("phase", "")).replace("_", " ")
        return f"Session running ({phase})"
    if kind is SessionEventKind.SESSION_CANCEL_REQUESTED:
        return "Cancel requested"
    if kind in (
        SessionEventKind.SESSION_COMPLETED,
        SessionEventKind.SESSION_FAILED,
        SessionEventKind.SESSION_CANCELLED,
    ):
        reason = str(payload.get("termination_reason", "")).replace("_", " ")
        return f"session {payload['status']} ({reason})"
    if kind is SessionEventKind.CONTROLLER_STEP:
        step_num = payload["step_index"] + 1
        directive = payload.get("directive_kind") or payload.get("stop_reason") or "step"
        directive_map = {
            "add_hypothesis": "hypothesis added",
            "read_source": "source read",
            "run_debugger": "PDB inspection",
            "apply_patch": "patch candidate",
            "verify": "verification requested",
            "stop": "controller stopped",
        }
        detail = directive_map.get(directive, str(directive).replace("_", " "))
        return f"Controller step {step_num} ({detail})"
    if kind is SessionEventKind.CONTROLLER_TRANSITION:
        src = str(payload.get("source_state", "")).replace("_", " ")
        tgt = str(payload.get("target_state", "")).replace("_", " ")
        return f"controller transition: {src} -> {tgt}"
    if kind is SessionEventKind.MODEL_REQUEST_STARTED:
        return f"Model request {payload['request_index'] + 1} started"
    if kind is SessionEventKind.MODEL_REQUEST_COMPLETED:
        if payload["status"] != "ok" and payload.get("error_kind"):
            return (
                f"model request {payload['request_index'] + 1} failed — "
                f"{payload['error_kind']}: {payload['error_message']}"
            )
        return f"Model request {payload['request_index'] + 1} completed"
    if kind is SessionEventKind.MODEL_DIRECTIVE_ACCEPTED:
        detail = payload.get("action_name") or payload.get("target_state") or "directive"
        return f"Directive accepted ({str(detail).replace('_', ' ')})"
    if kind is SessionEventKind.MODEL_DIRECTIVE_REJECTED:
        return f"Directive rejected ({payload['rejection_category']})"
    if kind is SessionEventKind.MODEL_CONFIGURED:
        return f"Model configured ({payload['profile_id']})"
    if kind is SessionEventKind.OPERATOR_PROGRESS:
        stage = payload.get("stage", "")
        stage_labels = {
            "starting": "Session starting",
            "preflight": "Preflight complete",
            "preparing_workspace": "Workspace prepared",
            "model_running": "Model request in progress",
            "debugger": "Debugger inspection",
            "candidate": "Candidate patch received",
            "verification": "Running verifier",
            "official_verification": "Running official verifier",
            "official_verification_preparing": "Preparing verification",
            "official_evaluator_started": "Official evaluator started",
            "official_evaluator_completed": "Official evaluator completed",
            "finalizing": "Finalizing results",
            "cleanup": "Cleaning workspace",
            "completed": "Session complete",
        }
        label = stage_labels.get(stage, f"Stage: {str(stage).replace('_', ' ')}")
        detail = payload.get("detail")
        return f"{label}: {detail}" if detail else label
    if kind is SessionEventKind.TOOL_STARTED:
        tool_map = {
            "read_source": "Source read",
            "set_breakpoint": "Set breakpoint",
            "run_to_breakpoint": "Run to breakpoint",
            "step_over": "Step over",
            "step_into": "Step into",
            "step": "Step",
            "get_stack_summary": "Inspect stack",
            "get_locals": "Inspect locals",
            "apply_patch": "Apply patch",
            "revert_patch": "Revert patch",
            "run_repro": "Run reproduction",
            "run_tests": "Run tests",
        }
        tool_name = payload["tool_name"]
        action = tool_map.get(tool_name, f"Tool {tool_name}")
        target = payload.get("target")
        return f"{action} started ({target})" if target else f"{action} started"
    if kind is SessionEventKind.TOOL_COMPLETED:
        tool_map = {
            "read_source": "Source read",
            "set_breakpoint": "Breakpoint set",
            "run_to_breakpoint": "Run to breakpoint",
            "step_over": "Step complete",
            "step_into": "Step into complete",
            "step": "Step complete",
            "get_stack_summary": "Stack inspected",
            "get_locals": "Locals inspected",
            "apply_patch": "Patch applied",
            "revert_patch": "Patch reverted",
            "run_repro": "Reproduction complete",
            "run_tests": "Tests complete",
        }
        tool_name = payload["tool_name"]
        action = tool_map.get(tool_name, f"Tool {tool_name}")
        target = payload.get("target")
        status = payload.get("status", "ok")
        if target:
            return f"{action} ({target}) ({status})" if status != "ok" else f"{action} ({target})"
        return f"{action} ({status})" if status != "ok" else f"{action}"
    if kind is SessionEventKind.DEBUGGER_STARTED:
        return "Debugger started"
    if kind is SessionEventKind.DEBUGGER_LOCATION_CHANGED:
        fn = payload.get("function")
        script = payload.get("script")
        line = payload.get("line")
        loc = f"{script}:{line}" if script and line is not None else script or f"line {line}"
        if fn:
            return f"Paused at {fn} ({loc})"
        return f"Paused at {loc}"
    if kind is SessionEventKind.DEBUGGER_STACK_OBSERVED:
        return f"Stack observed ({len(payload['frames'])} frames)"
    if kind is SessionEventKind.DEBUGGER_LOCALS_OBSERVED:
        return f"Locals observed ({len(payload['locals'])} values)"
    if kind is SessionEventKind.PATCH_PROPOSED:
        return f"patch attempt {payload['attempt_index'] + 1} proposed"
    if kind is SessionEventKind.PATCH_REJECTED:
        return f"Patch attempt {payload['attempt_index'] + 1} rejected"
    if kind is SessionEventKind.PATCH_APPLY_FAILED:
        return f"Patch attempt {payload['attempt_index'] + 1} apply failed"
    if kind is SessionEventKind.PATCH_APPLIED:
        return f"Patch attempt {payload['attempt_index'] + 1} applied"
    if kind is SessionEventKind.PATCH_REVERTED:
        return f"Patch attempt {payload['attempt_index'] + 1} reverted"
    if kind is SessionEventKind.SOURCE_SNAPSHOT:
        return f"Source snapshot: {payload['path']} ({payload['line_count']} lines)"
    if kind is SessionEventKind.DIAGNOSIS_RECORDED:
        detail = payload.get("text")
        return f"Diagnosis: {_trim_summary(detail)}" if detail else "Diagnosis recorded"
    if kind is SessionEventKind.VERIFIER_STARTED:
        return "Verifier started"
    if kind is SessionEventKind.VERIFIER_STAGE_STARTED:
        stage = str(payload.get("stage", "")).replace("_", " ")
        return f"Verifier stage started: {stage}"
    if kind is SessionEventKind.VERIFIER_STAGE_COMPLETED:
        stage = str(payload.get("stage", "")).replace("_", " ")
        return f"Verifier stage: {stage} ({payload['status']})"
    if kind is SessionEventKind.VERIFIER_COMPLETED:
        outcome = payload.get("outcome") or "no outcome"
        return f"verifier completed ({outcome})"
    if kind is SessionEventKind.CLEANUP_STARTED:
        return "Cleanup started"
    if kind is SessionEventKind.CLEANUP_COMPLETED:
        verified = "verified" if payload["verified"] else "unverified"
        return f"Cleanup completed ({verified})"
    if kind is SessionEventKind.CLEANUP_NOT_REQUIRED:
        return "Cleanup not required"
    if kind is SessionEventKind.ARTIFACT_WRITTEN:
        return f"Artifact written: {payload['path']}"
    raise ApplicationContractError(f"unsupported event kind: {kind.value!r}")
