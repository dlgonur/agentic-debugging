"""Level-32 operator progress-record consumption.

This module owns the v1/v2 operator progress-record consumption beneath
the :class:`~agentic_debugger.application.level32.Level32OperatorWorker`:
strict additive schema checks, bounded text extraction, patch/controller
operation projection, pre-resource-abort and resource-creation
milestones.  The functions operate on the WORKER AUTHORITY passed
explicitly (the single process/lifecycle authority stays in the worker
class); malformed records always drop fail-closed.
"""

from __future__ import annotations

from typing import Any, Mapping

from agentic_debugger.application.events import (
    OperatorStage,
    SessionEventKind,
    contains_credential_shape,
)
from agentic_debugger.application.worker_protocol import WorkerLiveness

def consume_operation_record(worker, record) -> None:
    """Map one strictly validated structured operation to typed events.

    Malformed records drop silently: the authoritative operator result
    remains the source of truth and no fact is ever reconstructed from
    an invalid observation channel record.
    """
    operation = record.get("operation")
    if operation == "candidate_patch_available":
        worker._consume_candidate_patch_milestone(record)
        return
    if operation == "tool":
        phase = record.get("phase")
        tool = worker._bounded_operation_text(record.get("tool"), 64)
        if tool is None or phase not in ("started", "completed"):
            return
        if phase == "started":
            if set(record) != {"schema_version", "kind", "operation", "phase", "tool"}:
                return
            worker._emit(SessionEventKind.TOOL_STARTED, {"tool_name": tool})
            return
        if set(record) != {
            "schema_version", "kind", "operation", "phase", "tool", "status",
        }:
            return
        status = record.get("status")
        if status not in worker._TOOL_STATUSES:
            return
        worker._emit(SessionEventKind.TOOL_COMPLETED, {"tool_name": tool, "status": status})
        return
    if operation == "source_inspection":
        if set(record) != {
            "schema_version", "kind", "operation", "tool", "file",
            "start_line", "end_line",
        }:
            return
        tool = worker._bounded_operation_text(record.get("tool"), 64)
        file_name = worker._relative_operation_path(record.get("file"))
        start = worker._operation_line(record.get("start_line"))
        end = worker._operation_line(record.get("end_line"))
        if tool is None or file_name is None or start is None or end is None:
            return
        if start > end:
            return
        target = f"{file_name}:{start}-{end}"
        worker._emit(
            SessionEventKind.TOOL_COMPLETED,
            {"tool_name": tool, "status": "ok", "target": target},
        )
        return
    if operation == "debugger_active":
        if set(record) != {"schema_version", "kind", "operation", "script", "breakpoint_line"}:
            return
        script = worker._relative_operation_path(record.get("script"))
        line = worker._operation_line(record.get("breakpoint_line"))
        if script is None or line is None:
            return
        if not worker._streamed_debugger_started:
            worker._streamed_debugger_started = True
            worker._emit(
                SessionEventKind.DEBUGGER_STARTED,
                {"script": script, "breakpoints": (f"{script}:{line}",)},
            )
        if worker._pause_generation == 0:
            worker._pause_generation = 1
            worker._emit(
                SessionEventKind.DEBUGGER_LOCATION_CHANGED,
                {"script": script, "line": line, "function": None, "pause_generation": 1},
            )
        return
    if operation == "pdb_observation":
        allowed = {"schema_version", "kind", "operation"}
        if not allowed.issubset(set(record)) or set(record) - allowed - {"script", "line"}:
            return
        script = worker._relative_operation_path(record.get("script"))
        line = worker._operation_line(record.get("line"))
        if ("script" in record or "line" in record) and (script is None or line is None):
            return
        worker._pause_generation += 1
        if script is not None and line is not None:
            if worker._streamed_pdb_observation is None:
                worker._streamed_pdb_observation = (script, line)
            worker._emit(
                SessionEventKind.DEBUGGER_LOCATION_CHANGED,
                {"script": script, "line": line, "function": None, "pause_generation": worker._pause_generation},
            )
        worker._emit(
            SessionEventKind.DEBUGGER_STACK_OBSERVED,
            {"pause_generation": worker._pause_generation, "frames": ()},
        )
        return
    if operation == "candidate":
        phase = record.get("phase")
        if phase not in worker._CANDIDATE_PHASES:
            return
        attempt = record.get("attempt")
        if type(attempt) is not int or isinstance(attempt, bool) or attempt < 1:
            return
        reason = worker._bounded_operation_text(record.get("reason"))
        index = attempt - 1
        if phase == "applied":
            changed = record.get("changed_files")
            if type(changed) is not list or len(changed) > 16:
                return
            files: list[str] = []
            for item in changed:
                path = worker._relative_operation_path(item)
                if path is None:
                    return
                files.append(path)
            base = {"attempt_index": index, "changed_files": tuple(files), "syntax_passed": None}
            worker._last_applied_attempt_index = index
            worker._emit(SessionEventKind.PATCH_APPLIED, base)
        elif phase == "rejected":
            worker._emit(
                SessionEventKind.PATCH_REJECTED,
                {"attempt_index": index, "rejection_reason": reason or "candidate rejected by patch validation"},
            )
        elif phase == "failed":
            worker._emit(
                SessionEventKind.PATCH_APPLY_FAILED,
                {"attempt_index": index, "apply_failure_reason": reason or "candidate patch apply failed"},
            )
        else:
            worker._emit(SessionEventKind.PATCH_REVERTED, {"attempt_index": index})
        return
    if operation == "controller_step":
        if set(record) != {"schema_version", "kind", "operation", "step_index", "directive_kind"}:
            return
        step_index = record.get("step_index")
        if type(step_index) is not int or isinstance(step_index, bool) or step_index < 0:
            return
        directive_kind = worker._bounded_operation_text(record.get("directive_kind"), 64)
        worker._emit(
            SessionEventKind.CONTROLLER_STEP,
            {"step_index": step_index, "directive_kind": directive_kind, "stop_reason": None},
        )
        return
    # Unknown operation kinds are ignored (fail closed).

def consume_progress_v2(worker, record) -> None:
    """Accept additive safe v2 observer records; malformed records drop."""
    if record.get("kind") == "pre_resource_abort":
        # Explicit positive proof that operator aborted before any
        # disposable resource could be created. Fail-open: absence proves
        # nothing; malformed records drop.
        allowed = {"schema_version", "kind", "reason"}
        if set(record) != allowed:
            return
        reason = record.get("reason")
        if reason not in ("image_gate", "ollama_preflight", "launch_failed", "unknown"):
            return
        worker._pre_resource_abort_observed = True
        return
    if record.get("kind") == "resource_creation_started":
        # Emitted immediately BEFORE first disposable resource may be created.
        # Presence means resources MAY exist; absence proves nothing.
        if set(record) != {"schema_version", "kind"}:
            return
        worker._resource_creation_started_observed = True
        # Also treat as PREPARING_WORKSPACE for backward compatibility
        worker._emit_progress(OperatorStage.PREPARING_WORKSPACE)
        return
    kind = record.get("kind")
    if kind == "operation":
        worker._consume_operation_record(record)
        return
    if kind == "liveness":
        allowed = {
            "schema_version", "kind", "request_index", "request_elapsed_seconds",
            "last_activity_age_seconds", "transport_alive", "watchdog_idle_seconds",
        }
        if set(record) != allowed:
            return
        request_index = record.get("request_index")
        if request_index is not None and (type(request_index) is not int or request_index < 0):
            return
        values = [record.get("request_elapsed_seconds"), record.get("last_activity_age_seconds"), record.get("watchdog_idle_seconds")]
        if any(type(value) not in (int, float) or isinstance(value, bool) or value < 0 for value in values):
            return
        if type(record.get("transport_alive")) is not bool:
            return
        with worker._lock:
            worker._liveness = WorkerLiveness(
                request_index=request_index,
                request_elapsed_seconds=float(values[0]),
                last_activity_age_seconds=float(values[1]),
                transport_alive=record["transport_alive"],
                watchdog_idle_seconds=float(values[2]),
            )
        return
    if kind == "model_request":
        detail = record.get("detail")
        if type(detail) is not str or not detail.startswith("request "):
            return
        number = detail.split(" ", 2)[1]
        if not number.isdigit() or int(number) < 1:
            return
        index = int(number) - 1
        if index not in worker._streamed_request_indexes:
            worker._streamed_request_indexes.add(index)
            worker._emit(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": index})
        worker._emit_progress(OperatorStage.MODEL_RUNNING, detail)
        return
    if kind == "model_request_completed":
        detail = record.get("detail")
        if type(detail) is not str or not detail.startswith("request "):
            return
        number = detail.split(" ", 2)[1]
        if not number.isdigit() or int(number) < 1:
            return
        index = int(number) - 1
        if index in worker._streamed_request_indexes:
            worker._emit(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": index, "status": "ok"})
            worker._streamed_request_indexes.remove(index)
        return
    if kind == "official_execution_proven":
        # The typed milestone is durable operator evidence: real official
        # test execution was observed.  Stage/detail remain unchanged for
        # v1 history readability.
        allowed = {
            "schema_version", "kind", "stage", "detail",
            "official_execution_proven",
        }
        if set(record) != allowed or record.get("official_execution_proven") is not True:
            return
        try:
            stage = OperatorStage(record["stage"])
        except (KeyError, ValueError):
            return
        detail = record.get("detail")
        if type(detail) is not str or not detail or contains_credential_shape(detail):
            return
        current = (stage, detail)
        if current == worker._last_progress:
            # Same fact already emitted: enrich nothing, re-mark typed.
            return
        worker._last_progress = current
        worker._emit(
            SessionEventKind.OPERATOR_PROGRESS,
            {"stage": stage.value, "detail": detail, "official_execution_proven": True},
        )
        return
    # Durable v2 operational records have a safe stage and optional
    # bounded label only. They intentionally remain one SessionEvent-v1
    # ``operator.progress`` fact, so v1 history stays readable and final
    # result projection never re-emits a duplicate tool/PDB/verifier fact.
    allowed = {"schema_version", "kind", "stage", "detail"}
    if set(record) != allowed or type(kind) is not str or type(record.get("detail")) not in (str, type(None)):
        return
    try:
        stage = OperatorStage(record["stage"])
    except (KeyError, ValueError):
        return
    detail = record.get("detail")
    if detail is not None and (not detail or len(detail.encode("utf-8")) > 512 or contains_credential_shape(detail)):
        return
    worker._emit_progress(stage, detail)
