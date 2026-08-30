"""Deterministic, policy-aware tool registration and dispatch boundary."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping, TypeAlias

from agentic_debugger.agent.controller_policy import (
    ActionName,
    is_action_allowed,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.cancellation import CancellationError
from agentic_debugger.events.schema import (
    Action,
    Observation,
    ObservationStatus,
)


MAX_TOOL_ARGUMENT_BYTES = 32768
MAX_TOOL_RESULT_PAYLOAD_BYTES = 65536
MAX_TOOL_SUMMARY_BYTES = 4096
MAX_TOOL_JSON_DEPTH = 32
MAX_TOOL_JSON_NODES = 4096
MAX_TOOL_DIAGNOSTIC_BYTES = 400

_DIAGNOSTIC_SECRET = re.compile(
    r"(?i)\b(?:bearer|basic)\s+\S+|\b(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|token)\s*[:=]\s*\S+"
)


class ToolRegistryError(ValueError):
    """Base error for invalid registry inputs and registry operations."""


class DuplicateToolError(ToolRegistryError):
    """Raised when a registry would contain a duplicate tool name."""


class ToolArgumentError(ToolRegistryError):
    """Raised for invalid or oversized tool arguments."""


class ToolRejectedError(ToolRegistryError):
    """Raised by a handler when it rejects an otherwise valid invocation."""

    def __init__(
        self,
        message: str,
        *,
        safe_diagnostic: str | None = None,
        recoverable: bool | None = None,
        payload_data: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.safe_diagnostic = safe_diagnostic
        self.recoverable = recoverable
        self.payload_data = payload_data or {}


class ToolTimeoutError(ToolRegistryError):
    """Raised by a handler when its operation times out."""


class ToolExecutionError(ToolRegistryError):
    """Raised by a handler for a bounded, expected execution failure.

    Carries an optional ``safe_diagnostic`` string that the dispatch layer
    propagates through the same bounded/redaction pipeline used for
    :class:`ToolRejectedError`.  The diagnostic must already be safe to surface
    to the model (no raw exception strings, no secrets, no workspace paths);
    ``_bounded_safe_diagnostic`` applies a second safety pass regardless.
    """

    def __init__(
        self,
        message: str,
        *,
        safe_diagnostic: str | None = None,
        recoverable: bool | None = None,
        payload_data: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.safe_diagnostic = safe_diagnostic
        self.recoverable = recoverable
        self.payload_data = payload_data or {}


ToolArgumentValidator: TypeAlias = Callable[
    [dict[str, object]], dict[str, object]
]


class ToolDispatchReason(str, Enum):
    OK = "ok"
    UNKNOWN_ACTION = "unknown_action"
    STATE_ACTION_NOT_ALLOWED = "state_action_not_allowed"
    TOOL_NOT_REGISTERED = "tool_not_registered"
    INVALID_ARGUMENTS = "invalid_arguments"
    TOOL_REJECTED = "tool_rejected"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_ERROR = "tool_error"
    INVALID_TOOL_RESULT = "invalid_tool_result"


_FIXED_SUMMARIES = {
    ToolDispatchReason.UNKNOWN_ACTION: "Unknown controller action.",
    ToolDispatchReason.STATE_ACTION_NOT_ALLOWED:
        "Action is not allowed in the current controller state.",
    ToolDispatchReason.TOOL_NOT_REGISTERED: "Tool is not registered.",
    ToolDispatchReason.INVALID_ARGUMENTS: "Action arguments were rejected.",
    ToolDispatchReason.TOOL_REJECTED: "Tool rejected the action.",
    ToolDispatchReason.TOOL_TIMEOUT: "Tool execution timed out.",
    ToolDispatchReason.TOOL_ERROR: "Tool execution failed.",
    ToolDispatchReason.INVALID_TOOL_RESULT:
        "Tool returned an invalid result.",
}


def _bounded_safe_diagnostic(value: object) -> str | None:
    if type(value) is not str or not value:
        return None
    value = _DIAGNOSTIC_SECRET.sub("<redacted>", value)
    value = "".join(char if 0x20 <= ord(char) != 0x7F else " " for char in value).strip()
    if not value:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_TOOL_DIAGNOSTIC_BYTES:
        value = encoded[: MAX_TOOL_DIAGNOSTIC_BYTES - 3].decode("utf-8", errors="ignore") + "..."
    return value


class _JsonCopyError(Exception):
    """Private sentinel for bounded JSON validation failures."""


@dataclass
class _JsonCopyState:
    max_bytes: int
    nodes: int = 0
    active_containers: set[int] | None = None

    def __post_init__(self) -> None:
        self.active_containers = set()


def _copy_json_value(
    value: object,
    state: _JsonCopyState,
    *,
    depth: int = 1,
) -> object:
    if depth > MAX_TOOL_JSON_DEPTH:
        raise _JsonCopyError
    state.nodes += 1
    if state.nodes > MAX_TOOL_JSON_NODES:
        raise _JsonCopyError

    value_type = type(value)
    if value is None or value_type is bool or value_type is int:
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise _JsonCopyError
        return value
    if value_type is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _JsonCopyError from exc
        return value

    if value_type is list:
        identity = id(value)
        if identity in state.active_containers:
            raise _JsonCopyError
        state.active_containers.add(identity)
        try:
            return [
                _copy_json_value(item, state, depth=depth + 1)
                for item in value
            ]
        finally:
            state.active_containers.remove(identity)

    if value_type is dict:
        identity = id(value)
        if identity in state.active_containers:
            raise _JsonCopyError
        state.active_containers.add(identity)
        try:
            copied: dict[str, object] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise _JsonCopyError
                copied[key] = _copy_json_value(item, state, depth=depth + 1)
            return copied
        finally:
            state.active_containers.remove(identity)

    raise _JsonCopyError


def _compact_json_bytes(value: object) -> int:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return len(serialized.encode("utf-8"))
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise _JsonCopyError from exc


def _detach_json_dict(value: object, *, max_bytes: int) -> dict[str, object]:
    if type(value) is not dict:
        raise _JsonCopyError
    state = _JsonCopyState(max_bytes=max_bytes)
    copied = _copy_json_value(value, state)
    if type(copied) is not dict or _compact_json_bytes(copied) > max_bytes:
        raise _JsonCopyError
    return copied


def _validate_summary(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _JsonCopyError
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _JsonCopyError from exc
    if len(encoded) > MAX_TOOL_SUMMARY_BYTES:
        raise _JsonCopyError
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise _JsonCopyError
    return value


def _validate_version(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ToolRegistryError("invalid tool version")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolRegistryError("invalid tool version") from exc
    if len(encoded) > 64:
        raise ToolRegistryError("invalid tool version")
    if any(
        not (
            "A" <= char <= "Z"
            or "a" <= char <= "z"
            or "0" <= char <= "9"
            or char in ".-_"
        )
        for char in value
    ):
        raise ToolRegistryError("invalid tool version")
    return value


@dataclass(frozen=True)
class ToolResult:
    status: ObservationStatus
    payload: dict[str, object]
    summary: str
    truncated: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not ObservationStatus:
            raise ToolRegistryError("invalid tool result status")
        try:
            payload = _detach_json_dict(
                self.payload, max_bytes=MAX_TOOL_RESULT_PAYLOAD_BYTES
            )
            summary = _validate_summary(self.summary)
        except _JsonCopyError as exc:
            raise ToolRegistryError("invalid tool result") from exc
        if type(self.truncated) is not bool:
            raise ToolRegistryError("invalid tool result truncation")
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "summary", summary)


ToolHandler: TypeAlias = Callable[[Action, dict[str, object]], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: ActionName
    argument_validator: ToolArgumentValidator
    handler: ToolHandler
    version: str = "1"
    # Live adapters use this declaration to expose the exact argument shape
    # accepted by the validator. It is optional for older/offline callers,
    # which may register a private test tool without a wire contract.
    argument_contract: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if type(self.name) is not ActionName:
            raise ToolRegistryError("invalid tool name")
        if not callable(self.argument_validator):
            raise ToolRegistryError("invalid argument validator")
        if not callable(self.handler):
            raise ToolRegistryError("invalid tool handler")
        _validate_version(self.version)
        if self.argument_contract is not None:
            if type(self.argument_contract) is not dict:
                raise ToolRegistryError("invalid argument contract")
            try:
                contract = _detach_json_dict(
                    self.argument_contract, max_bytes=MAX_TOOL_ARGUMENT_BYTES
                )
            except _JsonCopyError as exc:
                raise ToolRegistryError("invalid argument contract") from exc
            object.__setattr__(self, "argument_contract", contract)


def action_name_from_action(action: Action) -> ActionName | None:
    """Resolve an action's stored name without normalization or aliases."""

    if type(action) is not Action:
        raise ToolRegistryError("invalid action")
    if type(action.name) is not str:
        return None
    for action_name in ActionName:
        if action_name.value == action.name:
            return action_name
    return None


def _validate_action(action: object) -> Action:
    if type(action) is not Action:
        raise ToolRegistryError("invalid action")
    if (
        type(action.action_id) is not str
        or not action.action_id
        or type(action.run_id) is not str
        or not action.run_id
        or type(action.task_id) is not str
        or not action.task_id
        or type(action.state) is not ControllerState
        or type(action.name) is not str
        or not action.name
    ):
        raise ToolRegistryError("invalid action")
    return action


def _validate_observation_id(observation_id: object) -> str:
    if type(observation_id) is not str or not observation_id:
        raise ToolRegistryError("invalid observation id")
    return observation_id


def _bound_payload_string(value: object, max_bytes: int) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        value = str(value)
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        value = encoded[: max(0, max_bytes - 3)].decode("utf-8", errors="ignore") + "..."
    return value


def _build_safe_patch_failure(
    raw_pf: object,
    *,
    default_recoverable: bool | None = None,
    include_context: bool = True,
) -> dict[str, object] | None:
    if not isinstance(raw_pf, dict):
        return None
    raw_kind = raw_pf.get("kind", "patch_error")
    kind = _bound_payload_string(raw_kind, 100) or "patch_error"
    raw_rec = raw_pf.get("recoverable")
    if raw_rec is None:
        raw_rec = True if default_recoverable is None else default_recoverable
    rec = bool(raw_rec)
    safe_pf: dict[str, object] = {
        "kind": kind,
        "recoverable": rec,
    }
    raw_path = raw_pf.get("path")
    if raw_path is not None:
        bounded_path = _bound_payload_string(raw_path, 500)
        if bounded_path is not None:
            safe_pf["path"] = bounded_path
    line_number = raw_pf.get("line_number")
    if isinstance(line_number, int) and not isinstance(line_number, bool):
        safe_pf["line_number"] = line_number
    hunk_index = raw_pf.get("hunk_index")
    if isinstance(hunk_index, int) and not isinstance(hunk_index, bool):
        safe_pf["hunk_index"] = hunk_index
    if include_context:
        expected = raw_pf.get("expected")
        if expected is not None:
            safe_pf["expected"] = _bound_payload_string(expected, 500) or ""
        actual = raw_pf.get("actual")
        if actual is not None:
            safe_pf["actual"] = _bound_payload_string(actual, 500) or ""
        source = raw_pf.get("current_source_window")
        if source is not None:
            safe_pf["current_source_window"] = _bound_payload_string(source, 1800) or ""
    return safe_pf


def _rejection_payload(
    reason: ToolDispatchReason,
    *,
    diagnostic: object = None,
    recoverable: bool | None = None,
    extra_data: dict[str, object] | None = None,
) -> dict[str, object]:
    bounded_diag = _bounded_safe_diagnostic(diagnostic)
    rec: bool | None = None
    if recoverable is not None:
        rec = bool(recoverable)
    elif isinstance(extra_data, dict):
        if "recoverable" in extra_data and isinstance(extra_data["recoverable"], bool):
            rec = extra_data["recoverable"]
        elif isinstance(extra_data.get("patch_failure"), dict):
            pf_rec = extra_data["patch_failure"].get("recoverable")
            if isinstance(pf_rec, bool):
                rec = pf_rec

    # Tier 1: Try full direct detachment if extra_data is clean JSON and fits within limits.
    if isinstance(extra_data, dict):
        try:
            detached_extra = _detach_json_dict(
                extra_data, max_bytes=MAX_TOOL_RESULT_PAYLOAD_BYTES
            )
            candidate: dict[str, object] = dict(detached_extra)
            candidate["dispatch_reason"] = reason.value
            if rec is not None:
                candidate["recoverable"] = rec
            if bounded_diag is not None and "diagnostic" not in candidate:
                candidate["diagnostic"] = bounded_diag
            return _detach_json_dict(
                candidate, max_bytes=MAX_TOOL_RESULT_PAYLOAD_BYTES
            )
        except _JsonCopyError:
            pass

    # Tier 2: Build structured degraded payload with bounded context.
    try:
        candidate = {"dispatch_reason": reason.value}
        if rec is not None:
            candidate["recoverable"] = rec
        if bounded_diag is not None:
            candidate["diagnostic"] = bounded_diag
        if isinstance(extra_data, dict):
            if "applied" in extra_data:
                candidate["applied"] = bool(extra_data["applied"])
            if "error" in extra_data:
                err_str = _bound_payload_string(extra_data["error"], 200)
                if err_str is not None:
                    candidate["error"] = err_str
            pf = _build_safe_patch_failure(
                extra_data.get("patch_failure"),
                default_recoverable=rec,
                include_context=True,
            )
            if pf is not None:
                candidate["patch_failure"] = pf
        return _detach_json_dict(
            candidate, max_bytes=MAX_TOOL_RESULT_PAYLOAD_BYTES
        )
    except _JsonCopyError:
        pass

    # Tier 3: Degrade further without large context windows (expected/actual/source_window).
    try:
        candidate = {"dispatch_reason": reason.value}
        if rec is not None:
            candidate["recoverable"] = rec
        if bounded_diag is not None:
            candidate["diagnostic"] = bounded_diag
        if isinstance(extra_data, dict):
            if "applied" in extra_data:
                candidate["applied"] = bool(extra_data["applied"])
            if "error" in extra_data:
                err_str = _bound_payload_string(extra_data["error"], 100)
                if err_str is not None:
                    candidate["error"] = err_str
            pf = _build_safe_patch_failure(
                extra_data.get("patch_failure"),
                default_recoverable=rec,
                include_context=False,
            )
            if pf is not None:
                candidate["patch_failure"] = pf
        return _detach_json_dict(
            candidate, max_bytes=MAX_TOOL_RESULT_PAYLOAD_BYTES
        )
    except _JsonCopyError:
        pass

    # Tier 4: Minimal guaranteed fallback (always fits, always valid JSON dict).
    fallback: dict[str, object] = {"dispatch_reason": reason.value}
    if rec is not None:
        fallback["recoverable"] = rec
    if isinstance(extra_data, dict) and "applied" in extra_data:
        fallback["applied"] = bool(extra_data["applied"])
    if isinstance(extra_data, dict) and isinstance(extra_data.get("patch_failure"), dict):
        raw_kind = extra_data["patch_failure"].get("kind", "patch_error")
        fallback["patch_failure"] = {
            "kind": _bound_payload_string(raw_kind, 50) or "patch_error",
            "recoverable": bool(extra_data["patch_failure"].get("recoverable", True if rec is None else rec)),
        }
    return _detach_json_dict(fallback, max_bytes=MAX_TOOL_RESULT_PAYLOAD_BYTES)


def _safe_summary(value: object, default: str) -> str:
    if type(value) is not str or not value:
        return default
    clean = "".join(char if 0x20 <= ord(char) != 0x7F else " " for char in value).strip()
    if not clean:
        return default
    encoded = clean.encode("utf-8", errors="replace")
    if len(encoded) > MAX_TOOL_SUMMARY_BYTES:
        clean = encoded[: MAX_TOOL_SUMMARY_BYTES - 3].decode("utf-8", errors="ignore") + "..."
    return clean


def _make_observation(
    action: Action,
    observation_id: str,
    status: ObservationStatus,
    reason: ToolDispatchReason,
    summary: str,
    *,
    payload: dict[str, object] | None = None,
    truncated: bool = False,
) -> Observation:
    safe_summary = _safe_summary(
        summary,
        _FIXED_SUMMARIES.get(reason, "Tool operation completed."),
    )
    if payload is None:
        payload = {"dispatch_reason": reason.value}
    try:
        validated_payload = _detach_json_dict(
            payload, max_bytes=MAX_TOOL_RESULT_PAYLOAD_BYTES
        )
    except _JsonCopyError:
        if isinstance(payload, dict):
            validated_payload = _rejection_payload(
                reason,
                extra_data=payload,
            )
        else:
            validated_payload = _detach_json_dict(
                {"dispatch_reason": reason.value},
                max_bytes=MAX_TOOL_RESULT_PAYLOAD_BYTES,
            )
    return Observation(
        observation_id=observation_id,
        action_id=action.action_id,
        run_id=action.run_id,
        task_id=action.task_id,
        name=action.name,
        status=status,
        payload=validated_payload,
        summary=safe_summary,
        truncated=truncated,
    )

def _finalize_success_result(
    result: ToolResult,
) -> tuple[ObservationStatus, str, bool, dict[str, object]]:
    """Validate an untrusted result and its complete augmented payload."""

    if type(result) is not ToolResult:
        raise _JsonCopyError
    status = result.status
    if type(status) is not ObservationStatus:
        raise _JsonCopyError
    summary = _validate_summary(result.summary)
    truncated = result.truncated
    if type(truncated) is not bool:
        raise _JsonCopyError
    payload = _detach_json_dict(
        result.payload, max_bytes=MAX_TOOL_RESULT_PAYLOAD_BYTES
    )
    if "dispatch_reason" in payload:
        raise _JsonCopyError
    final_payload = dict(payload)
    final_payload["dispatch_reason"] = ToolDispatchReason.OK.value
    final_payload = _detach_json_dict(
        final_payload, max_bytes=MAX_TOOL_RESULT_PAYLOAD_BYTES
    )
    return status, summary, truncated, final_payload



@dataclass(frozen=True)
class ToolRegistry:
    specs: tuple[ToolSpec, ...] = ()

    def __post_init__(self) -> None:
        if type(self.specs) is not tuple:
            raise ToolRegistryError("invalid tool specs")
        seen: set[ActionName] = set()
        for spec in self.specs:
            if type(spec) is not ToolSpec:
                raise ToolRegistryError("invalid tool spec")
            if spec.name in seen:
                raise DuplicateToolError("duplicate tool name")
            seen.add(spec.name)

    def names(self) -> tuple[ActionName, ...]:
        return tuple(spec.name for spec in self.specs)

    def argument_contracts(self) -> dict[str, dict[str, object]]:
        """Return declared wire contracts for tools that provide one."""
        contracts: dict[str, dict[str, object]] = {}
        for spec in self.specs:
            if spec.argument_contract is None:
                continue
            try:
                contracts[spec.name.value] = _detach_json_dict(
                    spec.argument_contract,
                    max_bytes=MAX_TOOL_ARGUMENT_BYTES,
                )
            except _JsonCopyError as exc:
                raise ToolRegistryError("invalid argument contract") from exc
        return contracts

    def get(self, name: ActionName) -> ToolSpec:
        if type(name) is not ActionName:
            raise ToolRegistryError("invalid tool name")
        for spec in self.specs:
            if spec.name is name:
                return spec
        raise ToolRegistryError("tool is not registered")

    def register(self, spec: ToolSpec) -> "ToolRegistry":
        if type(spec) is not ToolSpec:
            raise ToolRegistryError("invalid tool spec")
        if any(existing.name is spec.name for existing in self.specs):
            raise DuplicateToolError("duplicate tool name")
        return ToolRegistry(self.specs + (spec,))

    def dispatch(
        self,
        action: Action,
        *,
        observation_id: str,
    ) -> Observation:
        action = _validate_action(action)
        observation_id = _validate_observation_id(observation_id)

        action_name = action_name_from_action(action)
        if action_name is None:
            return _make_observation(
                action,
                observation_id,
                ObservationStatus.REJECTED,
                ToolDispatchReason.UNKNOWN_ACTION,
                _FIXED_SUMMARIES[ToolDispatchReason.UNKNOWN_ACTION],
            )
        if not is_action_allowed(action.state, action_name):
            return _make_observation(
                action,
                observation_id,
                ObservationStatus.REJECTED,
                ToolDispatchReason.STATE_ACTION_NOT_ALLOWED,
                _FIXED_SUMMARIES[ToolDispatchReason.STATE_ACTION_NOT_ALLOWED],
            )
        try:
            spec = self.get(action_name)
        except ToolRegistryError:
            return _make_observation(
                action,
                observation_id,
                ObservationStatus.REJECTED,
                ToolDispatchReason.TOOL_NOT_REGISTERED,
                _FIXED_SUMMARIES[ToolDispatchReason.TOOL_NOT_REGISTERED],
            )

        try:
            raw_arguments = _detach_json_dict(
                action.arguments, max_bytes=MAX_TOOL_ARGUMENT_BYTES
            )
        except _JsonCopyError:
            return _make_observation(
                action,
                observation_id,
                ObservationStatus.REJECTED,
                ToolDispatchReason.INVALID_ARGUMENTS,
                _FIXED_SUMMARIES[ToolDispatchReason.INVALID_ARGUMENTS],
                payload=_rejection_payload(ToolDispatchReason.INVALID_ARGUMENTS),
            )

        try:
            validated_arguments = spec.argument_validator(raw_arguments)
            validated_arguments = _detach_json_dict(
                validated_arguments, max_bytes=MAX_TOOL_ARGUMENT_BYTES
            )
        except ToolRejectedError as exc:
            return _make_observation(
                action,
                observation_id,
                ObservationStatus.REJECTED,
                ToolDispatchReason.INVALID_ARGUMENTS,
                _FIXED_SUMMARIES[ToolDispatchReason.INVALID_ARGUMENTS],
                payload=_rejection_payload(
                    ToolDispatchReason.INVALID_ARGUMENTS,
                    diagnostic=exc.safe_diagnostic,
                    recoverable=exc.recoverable,
                    extra_data=exc.payload_data,
                ),
            )
        except Exception:
            return _make_observation(
                action,
                observation_id,
                ObservationStatus.REJECTED,
                ToolDispatchReason.INVALID_ARGUMENTS,
                _FIXED_SUMMARIES[ToolDispatchReason.INVALID_ARGUMENTS],
                payload=_rejection_payload(ToolDispatchReason.INVALID_ARGUMENTS),
            )

        try:
            result = spec.handler(action, validated_arguments)
        except CancellationError:
            raise
        except ToolRejectedError as exc:
            diagnostic = _bounded_safe_diagnostic(exc.safe_diagnostic)
            summary = diagnostic or _FIXED_SUMMARIES[ToolDispatchReason.TOOL_REJECTED]
            if len(summary) > MAX_TOOL_SUMMARY_BYTES:
                summary = summary[: MAX_TOOL_SUMMARY_BYTES - 3] + "..."
            return _make_observation(
                action,
                observation_id,
                ObservationStatus.REJECTED,
                ToolDispatchReason.TOOL_REJECTED,
                summary,
                payload=_rejection_payload(
                    ToolDispatchReason.TOOL_REJECTED,
                    diagnostic=exc.safe_diagnostic,
                    recoverable=exc.recoverable,
                    extra_data=exc.payload_data,
                ),
            )
        except ToolTimeoutError:
            return _make_observation(
                action,
                observation_id,
                ObservationStatus.TIMEOUT,
                ToolDispatchReason.TOOL_TIMEOUT,
                _FIXED_SUMMARIES[ToolDispatchReason.TOOL_TIMEOUT],
            )
        except ToolExecutionError as exc:
            diagnostic = _bounded_safe_diagnostic(exc.safe_diagnostic)
            summary = diagnostic or _FIXED_SUMMARIES[ToolDispatchReason.TOOL_ERROR]
            if len(summary) > MAX_TOOL_SUMMARY_BYTES:
                summary = summary[: MAX_TOOL_SUMMARY_BYTES - 3] + "..."
            return _make_observation(
                action,
                observation_id,
                ObservationStatus.ERROR,
                ToolDispatchReason.TOOL_ERROR,
                summary,
                payload=_rejection_payload(
                    ToolDispatchReason.TOOL_ERROR,
                    diagnostic=exc.safe_diagnostic,
                    recoverable=exc.recoverable,
                    extra_data=exc.payload_data,
                ),
            )
        except Exception:
            return _make_observation(
                action,
                observation_id,
                ObservationStatus.ERROR,
                ToolDispatchReason.TOOL_ERROR,
                _FIXED_SUMMARIES[ToolDispatchReason.TOOL_ERROR],
            )

        try:
            status, summary, truncated, payload = _finalize_success_result(result)
        except CancellationError:
            raise
        except Exception:
            return _make_observation(
                action,
                observation_id,
                ObservationStatus.ERROR,
                ToolDispatchReason.INVALID_TOOL_RESULT,
                _FIXED_SUMMARIES[ToolDispatchReason.INVALID_TOOL_RESULT],
            )

        return _make_observation(
            action,
            observation_id,
            status,
            ToolDispatchReason.OK,
            summary,
            payload=payload,
            truncated=truncated,
        )


__all__ = [
    "DuplicateToolError",
    "MAX_TOOL_ARGUMENT_BYTES",
    "MAX_TOOL_JSON_DEPTH",
    "MAX_TOOL_JSON_NODES",
    "MAX_TOOL_RESULT_PAYLOAD_BYTES",
    "MAX_TOOL_SUMMARY_BYTES",
    "ToolArgumentError",
    "ToolArgumentValidator",
    "ToolDispatchReason",
    "ToolExecutionError",
    "ToolHandler",
    "ToolRejectedError",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResult",
    "ToolSpec",
    "ToolTimeoutError",
    "action_name_from_action",
]
