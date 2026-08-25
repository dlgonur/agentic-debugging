"""Bounded JSON-lines protocol between the session worker and its supervisor.

The worker boundary is a local, bounded process protocol over the worker's
standard input/output pipes (mirroring the accepted PDB worker boundary).
This module owns the strict envelope vocabulary only; it never spawns
processes and never executes controller/PDB/verifier code.

Parent -> worker:

- ``start`` — one envelope carrying the validated request (spec mapping,
  run identity, session paths, scenario name and bounded params);
- ``cancel`` — cooperative cancellation request.

Worker -> parent:

- ``ready`` — startup completed, journal opened, ``session.created`` durable;
- ``event`` — one bounded sequence notification (``sequence`` only; the
  durable journal is the event authority and the parent catches up from it);
- ``terminal`` — the operational ``SessionResult`` mapping, exactly once;
- ``fatal`` — out-of-band fatal failure (e.g. journal write/fsync failure)
  when the journal cannot record its own terminal state;
- ``error`` — pre-ready startup failure.

All messages are single JSON lines, strictly validated, bounded in size, and
fail closed: unknown fields, unknown types, malformed JSON, and oversized
lines are rejected and never acted upon.  No live Python objects cross the
boundary; only the Task-1 validated mappings do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from agentic_debugger.application import ApplicationError, ApplicationInputError
from agentic_debugger.application.events import (
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
    validate_session_id,
)
from agentic_debugger.application.session import (
    MAX_DIAGNOSTICS,
    MAX_DIAGNOSTIC_CHARS,
    SessionBudgets,
    SessionId,
    SessionResult,
    SessionSpec,
)
from agentic_debugger.application.sources import ExecutionSourceSpec

#: Maximum serialized size of one control message line.
#:
#: Full ``SessionEvent`` bodies never cross the pipe (worker -> parent event
#: notifications carry only the sequence; the journal is the event
#: authority), so the bound must cover the largest remaining valid message:
#: the terminal result, whose diagnostics may reach
#: ``MAX_DIAGNOSTICS * MAX_DIAGNOSTIC_CHARS`` (~512 KiB with worst-case
#: JSON escaping).  ``1 MiB`` covers every valid control message while
#: still failing closed on anything outside the accepted contract.
MAX_WORKER_LINE_BYTES = 1024 * 1024

_MSG_START = "start"
_MSG_CANCEL = "cancel"
_MSG_READY = "ready"
_MSG_EVENT = "event"
_MSG_TERMINAL = "terminal"
_MSG_FATAL = "fatal"
_MSG_ERROR = "error"

_PARENT_TO_WORKER_TYPES = frozenset({_MSG_START, _MSG_CANCEL})
_WORKER_TO_PARENT_TYPES = frozenset({_MSG_READY, _MSG_EVENT, _MSG_TERMINAL, _MSG_FATAL, _MSG_ERROR})

_MAX_PATH_CHARS = 2048
_MAX_RUN_ID_CHARS = 256
_MAX_SCENARIO_CHARS = 128
_MAX_PARAM_KEYS = 32
_MAX_PARAM_KEY_CHARS = 64
_MAX_PARAM_VALUE_CHARS = 4096
_MAX_ERROR_KIND_CHARS = 128
_MAX_PRE_START_DELAY_SECONDS = 60.0
_MAX_ELAPSED_SECONDS = 86400


class WorkerProtocolError(ApplicationError):
    """Raised when a worker protocol message is malformed."""


def _invalid(message: str) -> WorkerProtocolError:
    return WorkerProtocolError(message)


def _bounded_text(value: Any, label: str, max_chars: int) -> str:
    if type(value) is not str or not value:
        raise _invalid(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise _invalid(f"{label} exceeds the {max_chars}-byte bound")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise _invalid(f"{label} contains control characters")
    return value


def _bounded_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, label, max_chars)


def _positive_int_or_none(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise _invalid(f"{label} must be a positive integer or null")
    if value > _MAX_ELAPSED_SECONDS:
        raise _invalid(f"{label} exceeds the {_MAX_ELAPSED_SECONDS}-second bound")
    return value


def _delay_seconds(value: Any) -> float:
    if type(value) is not int and type(value) is not float:
        raise _invalid("pre_start_delay_seconds must be a number")
    if isinstance(value, bool):
        raise _invalid("pre_start_delay_seconds must be a number")
    number = float(value)
    if not (0.0 <= number <= _MAX_PRE_START_DELAY_SECONDS):
        raise _invalid(
            f"pre_start_delay_seconds must be within "
            f"[0, {_MAX_PRE_START_DELAY_SECONDS}]"
        )
    return number


def _diagnostics(value: Any) -> Tuple[str, ...]:
    if type(value) is not list:
        raise _invalid("diagnostics must be a list of strings")
    if len(value) > MAX_DIAGNOSTICS:
        raise _invalid(f"diagnostics exceeds the {MAX_DIAGNOSTICS}-item bound")
    result: list[str] = []
    for index, item in enumerate(value):
        if type(item) is not str:
            raise _invalid(f"diagnostics[{index}] must be a string")
        try:
            encoded = item.encode("utf-8")
        except UnicodeEncodeError:
            raise _invalid(f"diagnostics[{index}] must be UTF-8 text")
        if len(encoded) > MAX_DIAGNOSTIC_CHARS:
            raise _invalid(
                f"diagnostics[{index}] exceeds the {MAX_DIAGNOSTIC_CHARS}-byte bound"
            )
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in item):
            raise _invalid(f"diagnostics[{index}] contains control characters")
        result.append(item)
    return tuple(result)


def _scenario_params(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid("scenario_params must be a mapping")
    if len(value) > _MAX_PARAM_KEYS:
        raise _invalid(f"scenario_params exceeds the {_MAX_PARAM_KEYS}-key bound")
    result: Dict[str, Any] = {}
    for key, item in value.items():
        _bounded_text(key, "scenario_params key", _MAX_PARAM_KEY_CHARS)
        if item is None:
            result[key] = None
        elif type(item) is str:
            result[key] = _bounded_text(item, "scenario_params value", _MAX_PARAM_VALUE_CHARS)
        elif type(item) is bool:
            result[key] = item
        elif type(item) is int:
            if not -2**63 <= item <= 2**63 - 1:
                raise _invalid("scenario_params integer value out of range")
            result[key] = item
        elif type(item) is float:
            number = float(item)
            if number != number or number in (float("inf"), float("-inf")):
                raise _invalid("scenario_params float value must be finite")
            result[key] = number
        else:
            raise _invalid(
                "scenario_params values must be strings, numbers, booleans or null"
            )
    return result


def session_spec_from_mapping(m: Any) -> SessionSpec:
    """Reconstruct a validated Task-1 :class:`SessionSpec` from its mapping.

    Every field passes through the existing Task-1 validated constructors, so
    a malformed mapping fails closed here.
    """
    if not isinstance(m, Mapping):
        raise _invalid("spec must be a mapping")
    required = {"task_id", "source", "budgets", "artifact_destination"}
    missing = required - set(m.keys())
    if missing:
        raise _invalid(f"spec is missing required fields: {sorted(missing)}")
    extra = set(m.keys()) - required
    if extra:
        raise _invalid(f"spec has unknown fields: {sorted(extra)}")
    source = m["source"]
    if not isinstance(source, Mapping):
        raise _invalid("spec.source must be a mapping")
    source_required = {"kind", "task_id", "policy", "model_config_ref"}
    if set(source.keys()) != source_required:
        raise _invalid("spec.source fields are invalid")
    try:
        source_kind = SourceKind(source["kind"])
    except ValueError:
        raise _invalid(f"spec.source.kind is unknown: {source['kind']!r}")
    if not can_start_new_session_kind(source_kind):
        raise _invalid(
            f"source kind {source_kind.value!r} is recorded and cannot start a worker session"
        )
    budgets = m["budgets"]
    if not isinstance(budgets, Mapping) or set(budgets.keys()) != {
        "max_model_calls",
        "max_controller_steps",
        "max_elapsed_seconds",
    }:
        raise _invalid("spec.budgets fields are invalid")
    try:
        source_spec = ExecutionSourceSpec(
            kind=source_kind,
            task_id=source["task_id"],
            policy=source["policy"],
            model_config_ref=source["model_config_ref"],
        )
        budget_spec = SessionBudgets(
            max_model_calls=budgets["max_model_calls"],
            max_controller_steps=budgets["max_controller_steps"],
            max_elapsed_seconds=budgets["max_elapsed_seconds"],
        )
        return SessionSpec(
            task_id=m["task_id"],
            source=source_spec,
            budgets=budget_spec,
            artifact_destination=m["artifact_destination"],
        )
    except ApplicationInputError as exc:
        raise _invalid(f"spec is invalid: {exc}") from exc


def can_start_new_session_kind(kind: SourceKind) -> bool:
    """Live-startable source kinds only (mirrors Task-1 authority)."""
    return kind in (
        SourceKind.OFFLINE_DEMO,
        SourceKind.CONFIGURED_MODEL,
        SourceKind.OLLAMA_CLOUD_LADDER,
        SourceKind.LEVEL32_OPERATOR,
    )


def session_result_from_mapping(m: Any, spec: SessionSpec) -> SessionResult:
    """Reconstruct a validated :class:`SessionResult` from its mapping.

    The result mapping does not carry the full spec; the caller's validated
    ``spec`` supplies it and identity must agree with the mapping.
    """
    if not isinstance(m, Mapping):
        raise _invalid("result must be a mapping")
    required = {
        "session_id", "task_id", "source_kind", "status", "termination_reason",
        "run_id", "started_at_utc", "ended_at_utc", "sequence",
        "cleanup_verified", "diagnostics",
    }
    missing = required - set(m.keys())
    if missing:
        raise _invalid(f"result is missing required fields: {sorted(missing)}")
    extra = set(m.keys()) - required
    if extra:
        raise _invalid(f"result has unknown fields: {sorted(extra)}")
    try:
        validate_session_id(m["session_id"])
    except Exception as exc:
        raise _invalid(f"result session_id is invalid: {exc}") from exc
    if m["task_id"] != spec.task_id:
        raise _invalid("result task_id does not match the session spec")
    try:
        source_kind = SourceKind(m["source_kind"])
    except ValueError:
        raise _invalid(f"result source_kind is unknown: {m['source_kind']!r}")
    if source_kind is not spec.source.kind:
        raise _invalid("result source_kind does not match the session spec")
    try:
        status = SessionStatus(m["status"])
        reason = SessionTerminationReason(m["termination_reason"])
    except ValueError:
        raise _invalid("result status or termination_reason is unknown")
    if type(m["sequence"]) is not int or isinstance(m["sequence"], bool) or m["sequence"] < 0:
        raise _invalid("result sequence must be a non-negative integer")
    if type(m["cleanup_verified"]) is not bool:
        raise _invalid("result cleanup_verified must be a boolean")
    return SessionResult(
        session_id=SessionId(m["session_id"]),
        spec=spec,
        status=status,
        termination_reason=reason,
        run_id=m["run_id"],
        started_at_utc=m["started_at_utc"],
        ended_at_utc=m["ended_at_utc"],
        sequence=m["sequence"],
        cleanup_verified=m["cleanup_verified"],
        diagnostics=_diagnostics(m["diagnostics"]),
    )


@dataclass(frozen=True)
class StartRequest:
    """Validated worker start request (parent -> worker)."""

    session_id: str
    spec: SessionSpec
    run_id: str
    work_dir: str
    journal_path: str
    scenario: str
    scenario_params: Dict[str, Any]
    max_elapsed_seconds: Optional[int]
    pre_start_delay_seconds: float


def parse_start_request(m: Any) -> StartRequest:
    """Strictly validate and detach one ``start`` envelope."""
    if not isinstance(m, Mapping):
        raise _invalid("start message must be a mapping")
    required = {
        "type", "session_id", "spec", "run_id", "work_dir", "journal_path",
        "scenario", "scenario_params", "max_elapsed_seconds",
        "pre_start_delay_seconds",
    }
    missing = required - set(m.keys())
    if missing:
        raise _invalid(f"start message is missing required fields: {sorted(missing)}")
    extra = set(m.keys()) - required
    if extra:
        raise _invalid(f"start message has unknown fields: {sorted(extra)}")
    if m["type"] != _MSG_START:
        raise _invalid("start message type must be 'start'")
    try:
        validate_session_id(m["session_id"])
    except Exception as exc:
        raise _invalid(f"start session_id is invalid: {exc}") from exc
    return StartRequest(
        session_id=m["session_id"],
        spec=session_spec_from_mapping(m["spec"]),
        run_id=_bounded_text(m["run_id"], "run_id", _MAX_RUN_ID_CHARS),
        work_dir=_bounded_text(m["work_dir"], "work_dir", _MAX_PATH_CHARS),
        journal_path=_bounded_text(m["journal_path"], "journal_path", _MAX_PATH_CHARS),
        scenario=_bounded_text(m["scenario"], "scenario", _MAX_SCENARIO_CHARS),
        scenario_params=_scenario_params(m["scenario_params"]),
        max_elapsed_seconds=_positive_int_or_none(
            m["max_elapsed_seconds"], "max_elapsed_seconds"
        ),
        pre_start_delay_seconds=_delay_seconds(m["pre_start_delay_seconds"]),
    )


def parse_cancel_message(m: Any) -> None:
    """Strictly validate one ``cancel`` envelope (type field only)."""
    if not isinstance(m, Mapping):
        raise _invalid("cancel message must be a mapping")
    extra = set(m.keys()) - {"type"}
    if extra:
        raise _invalid(f"cancel message has unknown fields: {sorted(extra)}")
    if m.get("type") != _MSG_CANCEL:
        raise _invalid("cancel message type must be 'cancel'")


def serialize_message(mapping: Mapping[str, Any]) -> bytes:
    """Serialize one message as a single bounded JSON line."""
    try:
        encoded = json.dumps(mapping, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid(f"message is not JSON-serializable: {exc}") from exc
    if len(encoded) > MAX_WORKER_LINE_BYTES:
        raise _invalid(
            f"serialized message exceeds MAX_WORKER_LINE_BYTES "
            f"({len(encoded)} > {MAX_WORKER_LINE_BYTES})"
        )
    return encoded + b"\n"


def _message_mapping(m: Any) -> Dict[str, Any]:
    if not isinstance(m, Mapping):
        raise _invalid("message must be a JSON object")
    if "type" not in m:
        raise _invalid("message is missing the type field")
    if type(m["type"]) is not str:
        raise _invalid("message type must be a string")
    return dict(m)


def parse_parent_message(line: str) -> Dict[str, Any]:
    """Parse and validate one parent -> worker message line.

    Returns a detached mapping with the validated ``type``; the caller
    dispatches to :func:`parse_start_request` / :func:`parse_cancel_message`.
    """
    try:
        m = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _invalid(f"parent message is not valid JSON: {exc}") from exc
    message = _message_mapping(m)
    if message["type"] not in _PARENT_TO_WORKER_TYPES:
        raise _invalid(f"unknown parent message type: {message['type']!r}")
    return message


@dataclass(frozen=True)
class WorkerNotification:
    """One validated worker -> parent notification.

    ``sequence`` is the notified journal sequence (``ready``/``event``);
    ``result`` is the validated terminal :class:`SessionResult`;
    ``error_kind``/``diagnostics`` belong to ``fatal``/``error``.
    """

    kind: str
    sequence: Optional[int] = None
    result: Optional[SessionResult] = None
    error_kind: Optional[str] = None
    diagnostics: Tuple[str, ...] = ()


def parse_worker_message(line: str, spec: SessionSpec) -> WorkerNotification:
    """Parse and validate one worker -> parent message line.

    ``spec`` is required to reconstruct the terminal :class:`SessionResult`.
    """
    try:
        m = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _invalid(f"worker message is not valid JSON: {exc}") from exc
    message = _message_mapping(m)
    kind = message["type"]
    if kind not in _WORKER_TO_PARENT_TYPES:
        raise _invalid(f"unknown worker message type: {kind!r}")
    if kind == _MSG_READY:
        sequence = message.get("sequence")
        if type(sequence) is not int or isinstance(sequence, bool) or sequence < 0:
            raise _invalid("ready message sequence must be a non-negative integer")
        return WorkerNotification(kind=kind, sequence=sequence)
    if kind == _MSG_EVENT:
        # Notifications carry only the sequence; the durable journal is the
        # event authority and the parent catches up from it.
        sequence = message.get("sequence")
        if type(sequence) is not int or isinstance(sequence, bool) or sequence < 0:
            raise _invalid("event message sequence must be a non-negative integer")
        return WorkerNotification(kind=kind, sequence=sequence)
    if kind == _MSG_TERMINAL:
        raw_result = message.get("result")
        return WorkerNotification(
            kind=kind, result=session_result_from_mapping(raw_result, spec)
        )
    if kind in (_MSG_FATAL, _MSG_ERROR):
        error_kind = _bounded_text(
            message.get("error_kind"), "error_kind", _MAX_ERROR_KIND_CHARS
        )
        return WorkerNotification(
            kind=kind,
            error_kind=error_kind,
            diagnostics=_diagnostics(message.get("diagnostics")),
        )
    raise _invalid(f"unknown worker message type: {kind!r}")


def start_message(
    *,
    session_id: str,
    spec: SessionSpec,
    run_id: str,
    work_dir: str,
    journal_path: str,
    scenario: str,
    scenario_params: Optional[Mapping[str, Any]] = None,
    max_elapsed_seconds: Optional[int] = None,
    pre_start_delay_seconds: float = 0.0,
) -> bytes:
    """Build the validated ``start`` envelope bytes."""
    mapping: Dict[str, Any] = {
        "type": _MSG_START,
        "session_id": session_id,
        "spec": spec.to_mapping(),
        "run_id": run_id,
        "work_dir": work_dir,
        "journal_path": journal_path,
        "scenario": scenario,
        "scenario_params": dict(scenario_params or {}),
        "max_elapsed_seconds": max_elapsed_seconds,
        "pre_start_delay_seconds": pre_start_delay_seconds,
    }
    parse_start_request(mapping)  # fail closed before any byte crosses the pipe
    return serialize_message(mapping)


def cancel_message() -> bytes:
    return serialize_message({"type": _MSG_CANCEL})


def ready_message(sequence: int) -> bytes:
    return serialize_message({"type": _MSG_READY, "sequence": sequence})


def event_notification(sequence: int) -> bytes:
    """Notify the parent that one event was appended at ``sequence``.

    The notification is intentionally small: the durable journal is the
    event authority and the parent catches up from it.
    """
    if type(sequence) is not int or isinstance(sequence, bool) or sequence < 0:
        raise _invalid("event notification sequence must be a non-negative integer")
    return serialize_message({"type": _MSG_EVENT, "sequence": sequence})


def terminal_message(result: SessionResult) -> bytes:
    return serialize_message({"type": _MSG_TERMINAL, "result": result.to_mapping()})


def fatal_message(error_kind: str, diagnostics: Sequence[str]) -> bytes:
    _bounded_text(error_kind, "error_kind", _MAX_ERROR_KIND_CHARS)
    return serialize_message(
        {
            "type": _MSG_FATAL,
            "error_kind": error_kind,
            "diagnostics": _diagnostics(list(diagnostics)),
        }
    )


def error_message(error_kind: str, diagnostics: Sequence[str]) -> bytes:
    _bounded_text(error_kind, "error_kind", _MAX_ERROR_KIND_CHARS)
    return serialize_message(
        {
            "type": _MSG_ERROR,
            "error_kind": error_kind,
            "diagnostics": _diagnostics(list(diagnostics)),
        }
    )


__all__ = [
    "MAX_WORKER_LINE_BYTES",
    "StartRequest",
    "WorkerNotification",
    "WorkerProtocolError",
    "cancel_message",
    "error_message",
    "event_notification",
    "fatal_message",
    "parse_cancel_message",
    "parse_parent_message",
    "parse_start_request",
    "parse_worker_message",
    "ready_message",
    "serialize_message",
    "session_result_from_mapping",
    "session_spec_from_mapping",
    "start_message",
    "terminal_message",
]
