from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agentic_debugger.runtime.exceptions import PdbProtocolError

PROTOCOL_VERSION = 1

SUPPORTED_OPERATIONS = frozenset({
    "hello", "ping", "shutdown", "run_to_breakpoint",
    "start_paused_target", "continue_paused_target", "get_target_status",
    "terminate_paused_target", "get_stack_summary", "get_frame",
    "get_frame_locals", "safe_eval_expression", "run_post_mortem",
})

MAX_LINE_LENGTH = 65536


def _check_no_unknown_fields(
    m: Dict[str, Any], known: set, label: str
) -> None:
    extra = set(m.keys()) - known
    if extra:
        raise PdbProtocolError(
            f"Unknown fields in {label}: {sorted(extra)}"
        )


def _check_required_fields(
    m: Dict[str, Any], required: set, label: str
) -> None:
    missing = required - set(m.keys())
    if missing:
        raise PdbProtocolError(
            f"Missing required fields in {label}: {sorted(missing)}"
        )


def _ensure_dict(m: Any, label: str) -> Dict[str, Any]:
    if not isinstance(m, dict):
        raise PdbProtocolError(f"{label} must be a mapping")
    return m


def _validate_int_strict(v: Any, label: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise PdbProtocolError(
            f"{label} must be an integer, got {type(v).__name__}"
        )
    return v


def _validate_int_non_negative(v: Any, label: str) -> int:
    v = _validate_int_strict(v, label)
    if v < 0:
        raise PdbProtocolError(f"{label} must be non-negative, got {v}")
    return v


def _validate_int_positive(v: Any, label: str) -> int:
    v = _validate_int_strict(v, label)
    if v <= 0:
        raise PdbProtocolError(f"{label} must be positive, got {v}")
    return v


_JSON_SCALAR_TYPES = (type(None), bool, int, float, str)
_JSON_TYPES = (type(None), bool, int, float, str, list, dict)


def _validate_json_compatible(value: Any, path: str = "") -> None:
    if isinstance(value, tuple):
        raise PdbProtocolError(
            f"Tuple at {path} is not valid JSON; use list instead"
        )
    if not isinstance(value, _JSON_TYPES):
        raise PdbProtocolError(
            f"Value {value!r} at {path} is not JSON-compatible "
            f"(type={type(value).__name__})"
        )
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise PdbProtocolError(
                f"Non-finite float {value!r} at {path} is not valid JSON"
            )
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _validate_json_compatible(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise PdbProtocolError(
                    f"Non-string key {k!r} at {path}"
                )
            _validate_json_compatible(v, f"{path}.{k}")
        return


def _validate_payload(payload: Any, label: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise PdbProtocolError(f"{label}.payload must be a mapping")
    _validate_json_compatible(payload, f"{label}.payload")
    return payload


def _validate_result(result: Any, label: str) -> Dict[str, Any]:
    if not isinstance(result, dict):
        raise PdbProtocolError(f"{label}.result must be a mapping")
    _validate_json_compatible(result, f"{label}.result")
    return result


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_freeze(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_freeze(item) for item in value]
    return value


def _make_json_object_pairs_hook(label: str):
    """Return an object_pairs_hook that rejects duplicate keys."""

    def _hook(pairs: list) -> Dict[str, Any]:
        seen = set()
        for k, v in pairs:
            if not isinstance(k, str):
                raise PdbProtocolError(
                    f"Non-string key {k!r} in {label}"
                )
            if k in seen:
                raise PdbProtocolError(
                    f"Duplicate key {k!r} in {label}"
                )
            seen.add(k)
        return dict(pairs)
    return _hook


@dataclass(frozen=True)
class PdbRequest:
    protocol_version: int
    request_id: int
    operation: str
    payload: Dict[str, Any]

    def __post_init__(self) -> None:
        _validate_int_strict(
            self.protocol_version, "PdbRequest.protocol_version"
        )
        _validate_int_non_negative(
            self.request_id, "PdbRequest.request_id"
        )
        if not isinstance(self.operation, str) or not self.operation:
            raise PdbProtocolError(
                "PdbRequest.operation must be a non-empty string"
            )
        object.__setattr__(self, "payload", _deep_freeze(self.payload))
        _validate_payload(self.payload, "PdbRequest")

    _KNOWN_FIELDS = {
        "protocol_version", "request_id", "operation", "payload",
    }
    _REQUIRED_FIELDS = {
        "protocol_version", "request_id", "operation", "payload",
    }

    @staticmethod
    def from_mapping(m: Any) -> PdbRequest:
        m = _ensure_dict(m, "request")
        _check_required_fields(m, PdbRequest._REQUIRED_FIELDS, "request")
        _check_no_unknown_fields(m, PdbRequest._KNOWN_FIELDS, "request")

        protocol_version = _validate_int_strict(
            m["protocol_version"], "request.protocol_version"
        )
        request_id = _validate_int_non_negative(
            m["request_id"], "request.request_id"
        )
        operation = m["operation"]
        if not isinstance(operation, str) or not operation:
            raise PdbProtocolError(
                "request.operation must be a non-empty string"
            )
        payload = _validate_payload(m["payload"], "request")

        return PdbRequest(
            protocol_version=protocol_version,
            request_id=request_id,
            operation=operation,
            payload=payload,
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "operation": self.operation,
            "payload": copy.deepcopy(self.payload),
        }


@dataclass(frozen=True)
class PdbResponse:
    protocol_version: int
    request_id: int
    success: bool
    result: Dict[str, Any]
    error: str

    def __post_init__(self) -> None:
        _validate_int_strict(
            self.protocol_version, "PdbResponse.protocol_version"
        )
        _validate_int_non_negative(
            self.request_id, "PdbResponse.request_id"
        )
        if not isinstance(self.success, bool):
            raise PdbProtocolError(
                "PdbResponse.success must be a boolean, "
                f"got {type(self.success).__name__}"
            )
        object.__setattr__(self, "result", _deep_freeze(self.result))
        _validate_result(self.result, "PdbResponse")
        if not isinstance(self.error, str):
            raise PdbProtocolError(
                "PdbResponse.error must be a string, "
                f"got {type(self.error).__name__}"
            )
        if self.success and self.error:
            raise PdbProtocolError(
                "PdbResponse.success is True but error is non-empty"
            )
        if not self.success and not self.error:
            raise PdbProtocolError(
                "PdbResponse.success is False but error is empty"
            )

    _KNOWN_FIELDS = {
        "protocol_version", "request_id", "success", "result", "error",
    }
    _REQUIRED_FIELDS = {
        "protocol_version", "request_id", "success", "result", "error",
    }

    @staticmethod
    def from_mapping(m: Any) -> PdbResponse:
        m = _ensure_dict(m, "response")
        _check_required_fields(m, PdbResponse._REQUIRED_FIELDS, "response")
        _check_no_unknown_fields(m, PdbResponse._KNOWN_FIELDS, "response")

        protocol_version = _validate_int_strict(
            m["protocol_version"], "response.protocol_version"
        )
        request_id = _validate_int_non_negative(
            m["request_id"], "response.request_id"
        )
        success_raw = m["success"]
        if not isinstance(success_raw, bool):
            raise PdbProtocolError(
                "response.success must be a boolean, "
                f"got {type(success_raw).__name__}"
            )
        result = _validate_result(m["result"], "response")
        error_raw = m["error"]
        if not isinstance(error_raw, str):
            raise PdbProtocolError("response.error must be a string")

        if success_raw and error_raw:
            raise PdbProtocolError(
                "response.success is True but error is non-empty"
            )
        if not success_raw and not error_raw:
            raise PdbProtocolError(
                "response.success is False but error is empty"
            )

        return PdbResponse(
            protocol_version=protocol_version,
            request_id=request_id,
            success=success_raw,
            result=result,
            error=error_raw,
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "success": self.success,
            "result": copy.deepcopy(self.result),
            "error": self.error,
        }


@dataclass(frozen=True)
class PdbWorkerInfo:
    pid: int
    protocol_version: int

    def __post_init__(self) -> None:
        _validate_int_positive(self.pid, "PdbWorkerInfo.pid")
        _validate_int_strict(
            self.protocol_version, "PdbWorkerInfo.protocol_version"
        )

    _KNOWN_FIELDS = {"pid", "protocol_version"}
    _REQUIRED_FIELDS = {"pid", "protocol_version"}

    @staticmethod
    def from_mapping(m: Any) -> PdbWorkerInfo:
        m = _ensure_dict(m, "PdbWorkerInfo")
        _check_required_fields(
            m, PdbWorkerInfo._REQUIRED_FIELDS, "PdbWorkerInfo"
        )
        _check_no_unknown_fields(
            m, PdbWorkerInfo._KNOWN_FIELDS, "PdbWorkerInfo"
        )
        pid = _validate_int_positive(m["pid"], "PdbWorkerInfo.pid")
        protocol_version = _validate_int_strict(
            m["protocol_version"], "PdbWorkerInfo.protocol_version"
        )
        return PdbWorkerInfo(pid=pid, protocol_version=protocol_version)

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "protocol_version": self.protocol_version,
        }


def _serialize_json(mapping: Dict[str, Any]) -> str:
    return json.dumps(
        mapping,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        sort_keys=True,
    )


def serialize_request(req: PdbRequest) -> bytes:
    mapping = req.to_mapping()
    _validate_json_compatible(mapping, "request")
    line = _serialize_json(mapping)
    encoded = (line + "\n").encode("utf-8")
    if len(encoded) > MAX_LINE_LENGTH:
        raise PdbProtocolError(
            f"Serialized request exceeds MAX_LINE_LENGTH "
            f"({len(encoded)} > {MAX_LINE_LENGTH})"
        )
    return encoded


def _json_loads_strict(data: str, label: str) -> Any:
    """Load JSON with duplicate-key rejection at all nesting levels."""
    try:
        return json.loads(data, object_pairs_hook=_make_json_object_pairs_hook(label))
    except json.JSONDecodeError as e:
        raise PdbProtocolError(
            f"{label} is not valid JSON: {e}"
        ) from e


def deserialize_request(data: bytes) -> PdbRequest:
    if len(data) > MAX_LINE_LENGTH:
        raise PdbProtocolError(
            f"Request line exceeds maximum length ({len(data)} > "
            f"{MAX_LINE_LENGTH} bytes)"
        )
    try:
        line = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise PdbProtocolError(
            f"Request is not valid UTF-8: {e}"
        ) from e
    mapping = _json_loads_strict(line, "request")
    return PdbRequest.from_mapping(mapping)


def serialize_response(resp: PdbResponse) -> bytes:
    mapping = resp.to_mapping()
    _validate_json_compatible(mapping, "response")
    line = _serialize_json(mapping)
    encoded = (line + "\n").encode("utf-8")
    if len(encoded) > MAX_LINE_LENGTH:
        raise PdbProtocolError(
            f"Serialized response exceeds MAX_LINE_LENGTH "
            f"({len(encoded)} > {MAX_LINE_LENGTH})"
        )
    return encoded


def deserialize_response(data: bytes) -> PdbResponse:
    if len(data) > MAX_LINE_LENGTH:
        raise PdbProtocolError(
            f"Response line exceeds maximum length ({len(data)} > "
            f"{MAX_LINE_LENGTH} bytes)"
        )
    try:
        line = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise PdbProtocolError(
            f"Response is not valid UTF-8: {e}"
        ) from e
    mapping = _json_loads_strict(line, "response")
    return PdbResponse.from_mapping(mapping)
