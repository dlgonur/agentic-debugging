from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from agentic_debugger import SchemaValidationError
from agentic_debugger.agent.state_machine import ControllerState

_UTC_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)$"
)


class EventType(Enum):
    MESSAGE = "message"
    ACTION = "action"
    OBSERVATION = "observation"
    DECISION = "decision"
    TRANSITION = "transition"
    FINAL = "final"


class ObservationStatus(Enum):
    OK = "ok"
    ERROR = "error"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


def validate_json_compatible(value: Any, path: str = "") -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise SchemaValidationError(
                f"Non-finite float {value!r} at {path} is not valid JSON"
            )
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            validate_json_compatible(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise SchemaValidationError(
                    f"Non-string key {k!r} at {path}"
                )
            validate_json_compatible(v, f"{path}.{k}")
        return
    raise SchemaValidationError(
        f"Value {value!r} at {path} is not JSON-compatible "
        f"(type={type(value).__name__})"
    )


def _ensure_non_empty(v: Any, label: str) -> str:
    if not isinstance(v, str) or not v:
        raise SchemaValidationError(f"{label} must be a non-empty string")
    return v


def _validate_int_pos(v: Any, label: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise SchemaValidationError(f"{label} must be a non-negative integer")
    return v


def _check_no_unknown_fields(m: Dict[str, Any], known: set, label: str) -> None:
    extra = set(m.keys()) - known
    if extra:
        raise SchemaValidationError(
            f"Unknown fields in {label}: {sorted(extra)}"
        )


def _check_required_fields(m: Dict[str, Any], required: set, label: str) -> None:
    missing = required - set(m.keys())
    if missing:
        raise SchemaValidationError(
            f"Missing required fields in {label}: {sorted(missing)}"
        )


def _validate_utc_timestamp(ts: str) -> None:
    if not isinstance(ts, str) or not ts:
        raise SchemaValidationError("timestamp must be a non-empty string")
    if _UTC_ISO_RE.match(ts) is None:
        raise SchemaValidationError(
            f"timestamp must be ISO-8601 UTC (ending with Z or +00:00), got {ts!r}"
        )
    _validate_actual_datetime(ts)


def _validate_actual_datetime(ts: str) -> None:
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        raise SchemaValidationError(
            f"timestamp is not a valid calendar date/time: {ts!r}"
        )
    if parsed.tzinfo is None:
        raise SchemaValidationError(
            f"timestamp must be timezone-aware UTC: {ts!r}"
        )
    if parsed.utcoffset().total_seconds() != 0:
        raise SchemaValidationError(
            f"timestamp must be UTC (Z or +00:00), got offset: {ts!r}"
        )


@dataclass(frozen=True)
class Metadata:
    duration_ms: Optional[int] = None
    tool_version: Optional[str] = None
    model: Optional[str] = None
    tokens: Optional[int] = None
    cost: Optional[float] = None

    _KNOWN_FIELDS = {"duration_ms", "tool_version", "model", "tokens", "cost"}
    _REQUIRED_FIELDS = {"duration_ms", "tool_version", "model", "tokens", "cost"}

    @staticmethod
    def from_mapping(m: Any) -> Metadata:
        if not isinstance(m, dict):
            raise SchemaValidationError("metadata must be a mapping")
        _check_required_fields(m, Metadata._REQUIRED_FIELDS, "metadata")
        _check_no_unknown_fields(m, Metadata._KNOWN_FIELDS, "metadata")

        duration_ms = Metadata._int_or_none(
            m["duration_ms"], "metadata.duration_ms"
        )
        tool_version = m["tool_version"]
        if tool_version is not None and not isinstance(tool_version, str):
            raise SchemaValidationError(
                "metadata.tool_version must be a string or null"
            )
        model = m["model"]
        if model is not None and not isinstance(model, str):
            raise SchemaValidationError(
                "metadata.model must be a string or null"
            )
        tokens = Metadata._int_or_none(
            m["tokens"], "metadata.tokens"
        )
        cost = Metadata._number_or_none(
            m["cost"], "metadata.cost"
        )
        if cost is not None and (math.isnan(cost) or math.isinf(cost)):
            raise SchemaValidationError(
                "metadata.cost must be a finite number or null"
            )

        return Metadata(
            duration_ms=duration_ms,
            tool_version=tool_version,
            model=model,
            tokens=tokens,
            cost=cost,
        )

    @staticmethod
    def _int_or_none(v: Any, label: str) -> Optional[int]:
        if v is None:
            return None
        if not isinstance(v, int) or isinstance(v, bool):
            raise SchemaValidationError(f"{label} must be an integer or null")
        return v

    @staticmethod
    def _number_or_none(v: Any, label: str) -> Optional[float]:
        if v is None:
            return None
        if isinstance(v, bool):
            raise SchemaValidationError(f"{label} must be a number or null")
        if not isinstance(v, (int, float)):
            raise SchemaValidationError(f"{label} must be a number or null")
        return float(v)

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "duration_ms": self.duration_ms,
            "tool_version": self.tool_version,
            "model": self.model,
            "tokens": self.tokens,
            "cost": self.cost,
        }


@dataclass(frozen=True)
class Action:
    action_id: str
    run_id: str
    task_id: str
    state: ControllerState
    name: str
    arguments: Dict[str, Any]

    _KNOWN_FIELDS = {
        "action_id", "run_id", "task_id", "state", "name", "arguments"
    }
    _REQUIRED_FIELDS = {
        "action_id", "run_id", "task_id", "state", "name", "arguments"
    }

    @staticmethod
    def from_mapping(m: Any) -> Action:
        if not isinstance(m, dict):
            raise SchemaValidationError("action must be a mapping")
        _check_required_fields(m, Action._REQUIRED_FIELDS, "action")
        _check_no_unknown_fields(m, Action._KNOWN_FIELDS, "action")
        action_id = _ensure_non_empty(m["action_id"], "action.action_id")
        run_id = _ensure_non_empty(m["run_id"], "action.run_id")
        task_id = _ensure_non_empty(m["task_id"], "action.task_id")
        state_raw = m["state"]
        if not isinstance(state_raw, str) or not state_raw:
            raise SchemaValidationError("action.state must be a non-empty string")
        try:
            state = ControllerState(state_raw)
        except ValueError:
            raise SchemaValidationError(
                f"Unknown controller state: {state_raw!r}"
            )
        name = _ensure_non_empty(m["name"], "action.name")
        arguments = m["arguments"]
        if not isinstance(arguments, dict):
            raise SchemaValidationError("action.arguments must be a mapping")
        validate_json_compatible(arguments, "action.arguments")
        return Action(
            action_id=action_id,
            run_id=run_id,
            task_id=task_id,
            state=state,
            name=name,
            arguments=arguments,
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "state": self.state.value,
            "name": self.name,
            "arguments": self.arguments,
        }


@dataclass(frozen=True)
class Observation:
    observation_id: str
    action_id: str
    run_id: str
    task_id: str
    name: str
    status: ObservationStatus
    payload: Dict[str, Any]
    summary: str
    truncated: bool

    _KNOWN_FIELDS = {
        "observation_id", "action_id", "run_id", "task_id",
        "name", "status", "payload", "summary", "truncated",
    }
    _REQUIRED_FIELDS = {
        "observation_id", "action_id", "run_id", "task_id",
        "name", "status", "payload", "summary", "truncated",
    }

    @staticmethod
    def from_mapping(m: Any) -> Observation:
        if not isinstance(m, dict):
            raise SchemaValidationError("observation must be a mapping")
        _check_required_fields(m, Observation._REQUIRED_FIELDS, "observation")
        _check_no_unknown_fields(m, Observation._KNOWN_FIELDS, "observation")
        observation_id = _ensure_non_empty(
            m["observation_id"], "observation.observation_id"
        )
        action_id = _ensure_non_empty(
            m["action_id"], "observation.action_id"
        )
        run_id = _ensure_non_empty(m["run_id"], "observation.run_id")
        task_id = _ensure_non_empty(m["task_id"], "observation.task_id")
        name = _ensure_non_empty(m["name"], "observation.name")
        status_raw = m["status"]
        if not isinstance(status_raw, str) or not status_raw:
            raise SchemaValidationError(
                "observation.status must be a non-empty string"
            )
        try:
            status = ObservationStatus(status_raw)
        except ValueError:
            raise SchemaValidationError(
                f"Unknown observation status: {status_raw!r}"
            )
        payload = m["payload"]
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                "observation.payload must be a mapping"
            )
        validate_json_compatible(payload, "observation.payload")
        summary_raw = m["summary"]
        if not isinstance(summary_raw, str):
            raise SchemaValidationError("observation.summary must be a string")
        truncated_raw = m["truncated"]
        if not isinstance(truncated_raw, bool):
            raise SchemaValidationError(
                "observation.truncated must be a boolean"
            )
        return Observation(
            observation_id=observation_id,
            action_id=action_id,
            run_id=run_id,
            task_id=task_id,
            name=name,
            status=status,
            payload=payload,
            summary=summary_raw,
            truncated=truncated_raw,
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "action_id": self.action_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "name": self.name,
            "status": self.status.value,
            "payload": self.payload,
            "summary": self.summary,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class RunEvent:
    schema_version: str
    event_id: str
    run_id: str
    task_id: str
    sequence: int
    timestamp: str
    event_type: EventType
    name: str
    state: Optional[str]
    payload: Dict[str, Any]
    metadata: Metadata

    _KNOWN_FIELDS = {
        "schema_version", "event_id", "run_id", "task_id",
        "sequence", "timestamp", "event_type", "name",
        "state", "payload", "metadata",
    }
    _REQUIRED_FIELDS = {
        "schema_version", "event_id", "run_id", "task_id",
        "sequence", "timestamp", "event_type", "name",
        "state", "payload", "metadata",
    }

    @staticmethod
    def from_mapping(m: Any) -> RunEvent:
        if not isinstance(m, dict):
            raise SchemaValidationError("event must be a mapping")
        _check_required_fields(m, RunEvent._REQUIRED_FIELDS, "event")
        _check_no_unknown_fields(m, RunEvent._KNOWN_FIELDS, "event")
        schema_version = m["schema_version"]
        if schema_version != "1.0":
            raise SchemaValidationError(
                f"Unsupported event schema version: {schema_version!r}"
            )
        event_id = _ensure_non_empty(m["event_id"], "event.event_id")
        run_id = _ensure_non_empty(m["run_id"], "event.run_id")
        task_id = _ensure_non_empty(m["task_id"], "event.task_id")
        sequence = m["sequence"]
        _validate_int_pos(sequence, "event.sequence")

        timestamp_raw = m["timestamp"]
        if not isinstance(timestamp_raw, str) or not timestamp_raw:
            raise SchemaValidationError("event.timestamp must be a non-empty string")
        _validate_utc_timestamp(timestamp_raw)

        event_type_raw = m["event_type"]
        if not isinstance(event_type_raw, str) or not event_type_raw:
            raise SchemaValidationError(
                "event.event_type must be a non-empty string"
            )
        try:
            event_type = EventType(event_type_raw)
        except ValueError:
            raise SchemaValidationError(
                f"Unknown event type: {event_type_raw!r}"
            )

        name = _ensure_non_empty(m["name"], "event.name")
        state_raw = m["state"]
        if state_raw is not None:
            if not isinstance(state_raw, str):
                raise SchemaValidationError(
                    "event.state must be a string or null"
                )
            try:
                ControllerState(state_raw)
            except ValueError:
                raise SchemaValidationError(
                    f"event.state is not a valid ControllerState: {state_raw!r}"
                )
        payload = m["payload"]
        if not isinstance(payload, dict):
            raise SchemaValidationError("event.payload must be a mapping")
        validate_json_compatible(payload, "event.payload")

        metadata_raw = m["metadata"]
        if not isinstance(metadata_raw, dict):
            raise SchemaValidationError("event.metadata must be a mapping")
        metadata = Metadata.from_mapping(metadata_raw)

        return RunEvent(
            schema_version=schema_version,
            event_id=event_id,
            run_id=run_id,
            task_id=task_id,
            sequence=sequence,
            timestamp=timestamp_raw,
            event_type=event_type,
            name=name,
            state=state_raw,
            payload=payload,
            metadata=metadata,
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event_type": self.event_type.value,
            "name": self.name,
            "state": self.state,
            "payload": self.payload,
            "metadata": self.metadata.to_mapping(),
        }
