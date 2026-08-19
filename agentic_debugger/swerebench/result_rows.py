"""Durable session evidence projection for SWE-rebench Pilot rows."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from agentic_debugger.agent.controller_policy import ActionName
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application.events import SessionEventKind
from agentic_debugger.application.journal import read_session_journal


_METRIC_FILES = (
    "session.metrics.json",
    "runtime.metrics.json",
    "metrics.json",
    "provider.metrics.json",
)
_PROVIDER_TERMINATIONS = {
    "provider_or_transport_error",
    "request_timeout",
    "transport_error",
    "provider_error",
}
_SOURCE_OPERATIONS = {
    ActionName.SEARCH_CODE.value,
    ActionName.FIND_FUNCTION.value,
    ActionName.FIND_CLASS.value,
    ActionName.GET_SOURCE_WINDOW.value,
    ActionName.EXTRACT_FAILING_TEST.value,
}
_TEST_OPERATIONS = {
    ActionName.RUN_REPRODUCTION.value,
    ActionName.RUN_TESTS.value,
    ActionName.RUN_REGRESSION_TESTS.value,
}


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if type(value) is bool or not isinstance(value, (int, float)):
        return None
    if integer and type(value) is not int:
        return None
    return value if value >= 0 else None


def _duration_seconds(start: Any, end: Any) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        left = datetime.fromisoformat(start.replace("Z", "+00:00"))
        right = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    value = (right - left).total_seconds()
    return value if value >= 0 else None


def _load_metric_mappings(session_dir: Path, session_result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for key in ("metrics", "measurements", "runtime_metrics"):
        value = session_result.get(key)
        if isinstance(value, Mapping):
            result.append(value)
    for name in _METRIC_FILES:
        path = session_dir / name
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, Mapping):
            result.append(value)
    return result


def _metric(metrics: list[Mapping[str, Any]], *keys: str) -> Any:
    for mapping in reversed(metrics):
        for key in keys:
            if key in mapping:
                return mapping[key]
    return None


def _token_usage(metrics: list[Mapping[str, Any]]) -> Any:
    value = _metric(metrics, "token_usage", "usage")
    if isinstance(value, Mapping):
        return dict(value)
    prompt = _metric(metrics, "prompt_tokens")
    completion = _metric(metrics, "completion_tokens")
    total = _metric(metrics, "total_tokens")
    if any(item is not None for item in (prompt, completion, total)):
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        }
    return None


def _provider_failure_count(metrics: list[Mapping[str, Any]], session_result: Mapping[str, Any]) -> int | None:
    value = _metric(metrics, "provider_failures", "provider_error_count")
    if value is not None:
        return _number(value, integer=True)
    kinds = _metric(metrics, "provider_error_kinds")
    if isinstance(kinds, list):
        return len(kinds)
    diagnostics = session_result.get("diagnostics")
    if isinstance(diagnostics, list) and any(
        isinstance(item, str) and "model transport:" in item for item in diagnostics
    ):
        return 1
    return None


def _provider_invalid(
    metrics: list[Mapping[str, Any]],
    session_result: Mapping[str, Any],
    failure_count: int | None,
) -> bool:
    termination = _metric(metrics, "termination_reason")
    if termination in _PROVIDER_TERMINATIONS:
        return True
    diagnostics = session_result.get("diagnostics")
    if isinstance(diagnostics, list) and any(
        isinstance(item, str)
        and any(f"model transport: {reason}" in item for reason in _PROVIDER_TERMINATIONS)
        for item in diagnostics
    ):
        return True
    # A nonzero provider_error_count is evidence, not a terminal verdict.
    # In particular, invalid_model_response is recorded for malformed
    # directive/schema retries and remains a model/controller outcome.
    return False


def _load_execution_evidence(session_dir: Path) -> Mapping[str, Any]:
    path = session_dir / "execution.evidence.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _runtime_infrastructure_invalid(execution_evidence: Mapping[str, Any]) -> bool:
    return execution_evidence.get("runtime_infrastructure_failure") is True


def _terminal_reason(
    metrics: list[Mapping[str, Any]], session_result: Mapping[str, Any]
) -> str | None:
    metric_reason = _metric(metrics, "termination_reason")
    if isinstance(metric_reason, str):
        return metric_reason
    result_reason = session_result.get("termination_reason")
    return result_reason if isinstance(result_reason, str) else None


def durable_session_evidence(
    session_dir: Path,
    session_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Project only facts present in the durable journal/result artifacts."""
    journal = read_session_journal(session_dir / "session.events.jsonl")
    events = journal.events
    kinds = [event.event_kind for event in events]
    metrics = _load_metric_mappings(session_dir, session_result)
    execution_evidence = _load_execution_evidence(session_dir)

    wall = _metric(metrics, "wall_clock_seconds", "case_wall_clock_seconds")
    if wall is None:
        wall = _duration_seconds(
            session_result.get("started_at_utc"), session_result.get("ended_at_utc")
        )
    # The controller journal is the only source of logical-call truth.  The
    # adapter's model_request_count is incremented inside its transport retry
    # loop and therefore represents transport attempts.
    logical_calls = sum(
        kind is SessionEventKind.MODEL_REQUEST_STARTED for kind in kinds
    )
    transport_attempts = _metric(
        metrics, "model_request_count", "transport_attempts", "provider_process_attempts"
    )
    retries = _metric(metrics, "adapter_retry_count", "retry_count", "retries")
    fallbacks = _metric(metrics, "fallback_count", "fallbacks")
    if fallbacks is None:
        # The frozen Pilot-10 Ollama adapter contract has no fallback route.
        fallbacks = 0
    provider_failures = _provider_failure_count(metrics, session_result)

    tool_names = [
        event.payload.get("tool_name")
        for event in events
        if event.event_kind is SessionEventKind.TOOL_STARTED
    ]
    source_operations = sum(name in _SOURCE_OPERATIONS for name in tool_names)
    test_operations = sum(name in _TEST_OPERATIONS for name in tool_names)
    validate_sequence = [
        event.payload.get("tool_name")
        for event in events
        if event.event_kind is SessionEventKind.TOOL_STARTED
        and event.controller_phase is ControllerState.VALIDATE
    ]
    rejected = sum(
        kind in {SessionEventKind.PATCH_REJECTED, SessionEventKind.PATCH_APPLY_FAILED}
        for kind in kinds
    )
    terminal_reason = _terminal_reason(metrics, session_result)
    baseline_reproduced = execution_evidence.get("baseline_failure_reproduced")
    if type(baseline_reproduced) is not bool:
        baseline_reproduced = None
    provider_invalid = _provider_invalid(metrics, session_result, provider_failures)
    setup_error_kinds = _metric(metrics, "setup_error_kinds")
    if not isinstance(setup_error_kinds, list):
        setup_error_kinds = []
    adapter_error_kinds = _metric(metrics, "adapter_error_kinds")
    if not isinstance(adapter_error_kinds, list):
        adapter_error_kinds = []
    invalid_model_responses = _metric(metrics, "invalid_model_response_count")
    infrastructure_invalid = _runtime_infrastructure_invalid(execution_evidence) or bool(
        setup_error_kinds
        or terminal_reason in {"setup_failure", "configuration_failure", "provenance_failure"}
    )
    cleanup_verified = None
    for kind, event in zip(kinds, events):
        if kind is SessionEventKind.CLEANUP_COMPLETED:
            verified = event.payload.get("verified")
            if type(verified) is bool:
                cleanup_verified = verified
    cleanup_invalid = (
        any(kind is SessionEventKind.SESSION_STARTED for kind in kinds)
        and (
            cleanup_verified is False
            or any(
                kind is SessionEventKind.SESSION_FAILED
                and event.payload.get("status") == "cleanup_failed"
                for kind, event in zip(kinds, events)
            )
        )
    )
    return {
        "journal_state": journal.state.value,
        "journal_error": journal.error,
        "runtime": {
            "wall_clock_seconds": _number(wall),
            "logical_model_calls": _number(logical_calls, integer=True),
            "transport_attempts": _number(transport_attempts, integer=True),
            "adapter_retry_count": _number(retries, integer=True),
            "fallback_count": _number(fallbacks, integer=True),
            "token_usage": _token_usage(metrics),
            "provider_failures": provider_failures,
            "provider_error_kinds": _metric(metrics, "provider_error_kinds") or [],
            "adapter_error_kinds": adapter_error_kinds,
            "invalid_model_response_count": _number(invalid_model_responses, integer=True),
            "setup_error_kinds": setup_error_kinds,
        },
        "trajectory": {
            "baseline_reproduced": baseline_reproduced,
            "understand_reached": any(
                event.controller_phase is ControllerState.UNDERSTAND
                or (
                    event.event_kind is SessionEventKind.CONTROLLER_TRANSITION
                    and event.payload.get("target_state")
                    == ControllerState.UNDERSTAND.value
                )
                for event in events
            ),
            "hypotheses": sum(
                kind is SessionEventKind.DIAGNOSIS_RECORDED for kind in kinds
            ),
            "source_operations": source_operations,
            "test_operations": test_operations,
            "patch_attempts": sum(
                kind is SessionEventKind.PATCH_PROPOSED for kind in kinds
            ),
            "patch_rejections": rejected,
            "candidate_applied": any(
                kind is SessionEventKind.PATCH_APPLIED for kind in kinds
            ),
            "validate_sequence": validate_sequence,
            "terminal_reason": terminal_reason,
        },
        "provider_invalid": provider_invalid,
        "infrastructure_invalid": infrastructure_invalid,
        "cleanup_verified": cleanup_verified,
        "cleanup_invalid": cleanup_invalid,
    }


__all__ = ["durable_session_evidence"]
