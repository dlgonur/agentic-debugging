"""Adapt one protocol-1.3 JSON request to the installed OpenCode CLI.

This is a command for the existing JsonlCommandTransport contract, not a
controller or model adapter. OpenCode runs in a fresh empty directory and
receives the sanitized public request inline inside the single user message
(between explicit delimiters), never through a model-readable file: every
permission is denied, so the model cannot read any file or call any tool.
The wrapper extracts the model's one JSON directive through the strict
protocol-1.3 schema-aware extraction (validating every JSON object candidate
against the directive schema, action contracts, and controller context
embedded in the request, accepting exactly one fully valid directive) and
records bounded, credential-redacted evidence.

Route capture and wrapper catalog verification share one explicit isolated
catalog-observation path (:func:`observe_isolated_catalog`): a temporary
deterministic isolation root prepared with the exact route-mode isolation
contract, the exact effective configuration is required, and the launcher
version and the exact selected catalog entry are observed under that isolated
environment. In OpenCode Go route mode the wrapper independently recomputes
the exact selected catalog entry's deterministic fingerprint under the same
isolated observation and compares it with the authorization-bound expected
fingerprint before any model process may run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping


_SECRET_KEY = re.compile(r"(?:api[_-]?key|access[_-]?key|auth(?:orization)?|credential|password|secret|token|private[_-]?key)", re.I)
_SECRET_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+\S+|\b(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|token)\s*[:=]\s*\S+")
_MAX_EVIDENCE_CHARS = 1_000_000
_MAX_EVIDENCE_FIELD_CHARS = 16_384
#: Explicit delimiters bounding the canonical public request inside the inline
#: OpenCode user message.  The model must never read files; the request is
#: supplied directly in the message between these exact markers.  The
#: test-only synthetic executable mirrors these exact delimiters to recover
#: the request from the message.
PUBLIC_REQUEST_START = "=== BEGIN PUBLIC REQUEST ==="
PUBLIC_REQUEST_END = "=== END PUBLIC REQUEST ==="
#: The frozen paired-pilot v2 public-evidence byte budget
#: (``max_public_evidence_bytes = 20000``).  The bound applies to the
#: canonical public request serialization
#: (:func:`canonical_public_request`), never to the complete inline user
#: message: a canonical request up to and including 20000 bytes is accepted
#: and its complete message is constructed unchanged (the fully constructed
#: native command is independently bounded by
#: :data:`MAX_NATIVE_COMMAND_LINE_CHARS`).
MAX_PUBLIC_EVIDENCE_BYTES = 20_000
#: Conservative native Windows command-line bound for the FULLY constructed
#: ``opencode run`` argv: ``subprocess.list2cmdline(command)`` must stay below
#: the Windows CreateProcess command-line maximum (32767 characters).  Model
#: execution invokes the native ``opencode.exe`` directly (never the cmd.exe
#: batch shim, whose ~8191-character line limit no longer applies), so the
#: inline message can carry the full public request up to the public-evidence
#: bound.
MAX_NATIVE_COMMAND_LINE_CHARS = 30_000
#: The trusted npm package root relative to the verified ``opencode.cmd``
#: launcher directory; the native executable must belong to this root.
NPM_PACKAGE_ROOT_RELATIVE = "node_modules/opencode-ai"
#: The exact package-relative native executable path selected by the
#: established npm shim: the ``opencode.cmd`` launcher invokes
#: ``node_modules\opencode-ai\bin\opencode.exe``.  Only this deterministic
#: target under the trusted ``opencode-ai`` package root is ever resolved;
#: platform and baseline package binaries are never enumerated or compared,
#: and there is no recursive search.  PATH lookup, environment-supplied
#: executable paths, shell interpolation, PowerShell execution, parsing an
#: unrestricted command from the batch file, and fallback to ``opencode.cmd``
#: are rejected by construction.
NATIVE_EXECUTABLE_RELATIVE = "bin/opencode.exe"
#: Mirrors of the accepted protocol-1.3 directive field bounds
#: (``agentic_debugger.agent.model_adapter`` / ``controller_policy``).
MAX_DIRECTIVE_ARGUMENT_BYTES = 32_768
MAX_DIRECTIVE_REASON_BYTES = 2_048
MAX_DIRECTIVE_STATEMENT_BYTES = 4_096
MAX_DIRECTIVE_HYPOTHESIS_ID_BYTES = 128
MAX_DIRECTIVE_EVIDENCE_REF_BYTES = 256
MAX_DIRECTIVE_EVIDENCE_REF_COUNT = 64
HYPOTHESIS_CONFIDENCE_VALUES = ("low", "medium", "high")
HYPOTHESIS_STATUS_VALUES = ("supported", "rejected", "discarded")
#: The exact allowed top-level directive fields per protocol-1.3 kind.
#: Candidates carrying any additional top-level field are rejected strictly;
#: nothing is normalized or stripped.
DIRECTIVE_TOP_LEVEL_FIELDS = {
    "action": frozenset({"kind", "name", "arguments"}),
    "transition": frozenset({"kind", "target_state", "reason"}),
    "add_hypothesis": frozenset({"kind", "hypothesis_id", "statement", "confidence", "evidence_refs", "requires_runtime_evidence"}),
    "revise_hypothesis": frozenset({"kind", "hypothesis_id", "statement", "confidence", "evidence_refs", "requires_runtime_evidence"}),
    "set_hypothesis_status": frozenset({"kind", "hypothesis_id", "status"}),
}
PROTOCOL_INSTRUCTION = (
    "Return exactly one protocol-1.3 directive JSON object. "
    "The public request between the delimiters below is the complete bounded "
    "context; the allowed directive kinds, action names, and argument "
    "contracts inside it are authoritative. "
    "Do not read files and do not call tools: every permission is denied."
)
DIRECTIVE_OUTPUT_EXAMPLES = (
    '{"kind":"action","name":"run_reproduction","arguments":{"phase":"baseline"}} '
    '{"kind":"transition","target_state":"Understand","reason":"baseline reproduced"} '
    '{"kind":"add_hypothesis","hypothesis_id":"h-1","statement":"suspected root cause","confidence":"medium","evidence_refs":[],"requires_runtime_evidence":false} '
    '{"kind":"revise_hypothesis","hypothesis_id":"h-1","statement":"narrowed root cause","confidence":"high","evidence_refs":["obs-1"],"requires_runtime_evidence":true}'
)
DIRECTIVE_OUTPUT_PROHIBITIONS = (
    "Return one JSON object only. No code fences, no explanation, no tool "
    "calls, no protocol or version wrapper fields, and no alternate envelope "
    "('action', 'params', 'payload'); use only the kinds and contracts from "
    "the embedded request."
)
_ISOLATION_PERMISSION_DENIALS = {
    "*": "deny",
    "read": "deny",
    "write": "deny",
    "edit": "deny",
    "bash": "deny",
    "external_directory": "deny",
    "webfetch": "deny",
    "websearch": "deny",
    "question": "deny",
    "task": "deny",
}

#: Explicit route modes.  ``legacy`` (default) preserves the historical
#: OpenCode Zen zero-price route behavior unchanged; ``opencode-go`` binds
#: the exact model, variant, OpenCode version, catalog fingerprint, runtime
#: model identity, account status, and billing-route evidence already
#: validated by the outer authorization/preflight contract and does not
#: require zero catalog prices.
ROUTE_MODES = ("legacy", "opencode-go")
#: The catalog provider queried by the legacy route mode (unchanged):
#: ``models opencode --verbose --pure``.
LEGACY_CATALOG_PROVIDER = "opencode"
#: The OpenCode Go catalog provider; Go mode queries exactly
#: ``models opencode-go --verbose --pure``.
OPENCODE_GO_CATALOG_PROVIDER = "opencode-go"
#: The required catalog-qualified runtime identity prefix in OpenCode Go
#: mode; ``opencode/`` (including the historical Zen free-model identity)
#: and any other provider is rejected before model execution.
OPENCODE_GO_RUNTIME_ID_PREFIX = "opencode-go/"
_AGENTS_CONTENT = (
    "This task-owned workspace carries only the bounded public protocol request, "
    "supplied inline in your message between the BEGIN/END PUBLIC REQUEST "
    "delimiters; every permission is denied. "
    "Return one protocol directive; do not use tools, do not read files, do not "
    "inspect repositories, edit files, or run shell commands."
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name in {"prompt_tokens", "completion_tokens", "total_tokens"} and (type(item) is int or item is None):
                result[name] = item
            else:
                result[name] = "<redacted>" if _SECRET_KEY.search(name) else _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("<redacted>", value)
    return value


class JsonExtractionError(ValueError):
    def __init__(self, classification: str, detail: str, reason: str | None = None) -> None:
        super().__init__(detail)
        self.classification = classification
        #: The precise bounded validation reason for a rejected directive
        #: (e.g. ``unknown argument field 'extra'``); never the full model
        #: output.
        self.reason = reason


class CatalogFailureError(RuntimeError):
    """A local catalog inspection command exited nonzero.

    Carries the typed catalog-failure classification and bounded, sanitized
    diagnostic detail (never credentials, auth contents, or unrestricted
    environment values).
    """

    def __init__(self, classification: str, detail: dict[str, Any]) -> None:
        self.classification = classification
        self.detail = detail
        super().__init__(f"OpenCode model catalog failed with {classification}")


_CATALOG_FAILURE_DIAGNOSTIC_LIMIT = 4_096


def _sanitized_stream_sample(value: str, limit: int = _CATALOG_FAILURE_DIAGNOSTIC_LIMIT) -> str:
    """A bounded, redacted sample of a provider stream for diagnostics.

    ANSI sequences are stripped, the sample is capped with a truncation
    note, and credential-shaped values are redacted before the sample may
    enter any error/evidence record.
    """
    text = _strip_ansi(value or "").strip()
    if not text:
        return ""
    if len(text) > limit:
        text = text[:limit] + f" <truncated {len(text) - limit} characters>"
    redacted = _redact(text)
    return redacted if isinstance(redacted, str) else ""


def _catalog_failure_detail(returncode: int, stdout: str, stderr: str, command: list[str]) -> dict[str, Any]:
    """Bounded, sanitized detail for a failed local catalog inspection.

    Records only the exact catalog command, the exit code, and bounded
    redacted stream samples; never credentials, auth contents, or
    unrestricted environment values.
    """
    return {
        "catalog_command": " ".join(command),
        "catalog_exit_code": returncode,
        "catalog_stdout": _sanitized_stream_sample(stdout),
        "catalog_stderr": _sanitized_stream_sample(stderr),
    }


def _failure_evidence(exc: Exception) -> dict[str, Any]:
    """Typed failure evidence: error text plus the typed classification and
    bounded, sanitized detail when the exception carries them."""
    evidence: dict[str, Any] = {"error": f"{type(exc).__name__}: {exc}"}
    classification = getattr(exc, "classification", None)
    if isinstance(classification, str) and classification:
        evidence["failure_classification"] = classification
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and detail:
        evidence["failure_detail"] = detail
    return evidence


def _json_from_text(text: str) -> dict[str, Any]:
    """Extract exactly one top-level JSON object without protocol validation.

    Historical behavior for requests that carry no protocol-1.3
    ``directive_schema`` (legacy route / minimal requests): any output with
    more than one JSON object stays ambiguous and is rejected unchanged.
    """
    decoder = json.JSONDecoder()
    clean = _strip_ansi(text)
    candidates: list[dict[str, Any]] = []
    offset = 0
    while offset < len(clean):
        if clean[offset] != "{":
            offset += 1
            continue
        try:
            value, end = decoder.raw_decode(clean[offset:])
        except json.JSONDecodeError:
            offset += 1
            continue
        if isinstance(value, dict):
            candidates.append(value)
            offset += end
        else:
            offset += 1
    if not candidates:
        raise JsonExtractionError("no_json_object", "OpenCode output did not contain a directive JSON object")
    if len(candidates) > 1:
        raise JsonExtractionError("ambiguous_json_output", "OpenCode output contained multiple JSON objects")
    return candidates[0]


def _request_directive_schema(request: Mapping[str, Any]) -> dict[str, Any]:
    schema = request.get("directive_schema")
    return schema if isinstance(schema, Mapping) and schema else {}


def _request_controller(request: Mapping[str, Any]) -> dict[str, Any]:
    controller = request.get("controller")
    return controller if isinstance(controller, Mapping) else {}


def _request_action_contracts(request: Mapping[str, Any]) -> dict[str, Any]:
    contracts = request.get("action_contracts")
    return contracts if isinstance(contracts, Mapping) else {}


def _validate_text_field(value: Any, maximum_bytes: int) -> str | None:
    if type(value) is not str or not value or value != value.strip():
        return "text field must be a non-empty string without surrounding whitespace"
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return "text field is not valid UTF-8"
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return "text field contains control characters"
    if len(encoded) > maximum_bytes:
        return "text field exceeds the byte bound"
    return None


def _validate_action_arguments(name: str, arguments: Any, contracts: Mapping[str, Any]) -> str | None:
    """Validate action arguments against the embedded argument contract.

    The embedded contracts are authoritative: required fields must be
    present, unknown fields are rejected when the contract declares
    ``additional_properties: false``, and every declared property type,
    enum, and minimum length is enforced.  Malformed arguments are rejected
    strictly; the bounded directive-feedback cycle performs correction.
    """
    contract = contracts.get(name)
    if not isinstance(contract, Mapping):
        return f"action '{name}' has no embedded argument contract"
    properties = contract.get("properties")
    if not isinstance(properties, Mapping):
        properties = contract
    required = contract.get("required")
    if isinstance(required, list):
        for field in required:
            if field not in arguments:
                return f"missing required argument '{field}'"
    if contract.get("additional_properties") is False:
        unknown = set(arguments) - set(properties)
        if unknown:
            return f"unknown argument field '{sorted(unknown)[0]}'"
    for field, spec in properties.items():
        if field not in arguments:
            continue
        if not isinstance(spec, Mapping):
            continue
        value = arguments[field]
        type_name = spec.get("type")
        type_ok = {
            "string": type(value) is str,
            "integer": type(value) is int and not isinstance(value, bool),
            "number": type(value) in (int, float) and not isinstance(value, bool),
            "boolean": type(value) is bool,
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
            "null": value is None,
        }.get(type_name, True)
        if not type_ok:
            return f"argument '{field}' has the wrong type"
        enum = spec.get("enum")
        if isinstance(enum, list) and value not in enum:
            return f"argument '{field}' must be one of {list(enum)}"
        if type_name == "string" and type(value) is str and isinstance(spec.get("min_length"), int) and len(value) < spec["min_length"]:
            return f"argument '{field}' is too short"
    return None


def _validate_directive_candidate(candidate: Any, request: Mapping[str, Any]) -> str | None:
    """Strict protocol-1.3 directive validation of one candidate.

    Mirrors the accepted protocol-1.3 directive parser
    (:func:`agentic_debugger.evaluation.live._parse`) against the state
    embedded in the request: the ``directive_schema`` declares the legal
    directive kinds, ``controller.allowed_actions`` and ``action_contracts``
    declare the legal actions and their argument contracts, and
    ``controller.legal_transition_targets`` declares the legal transition
    targets.  Returns ``None`` when the candidate is a fully valid directive
    and a bounded failure reason otherwise.  Wrong envelopes, unknown fields,
    and malformed arguments are never normalized.
    """
    if not isinstance(candidate, Mapping):
        return "directive must be a JSON object"
    schema = _request_directive_schema(request)
    if not schema:
        return "request carries no directive schema"
    kind = candidate.get("kind")
    if type(kind) is not str or kind not in schema:
        return "unrecognized or missing directive 'kind'"
    unknown_fields = set(candidate) - DIRECTIVE_TOP_LEVEL_FIELDS.get(kind, frozenset())
    if unknown_fields:
        return f"unknown top-level field '{sorted(unknown_fields)[0]}'"
    controller = _request_controller(request)
    state = controller.get("state")
    if kind == "action":
        name = candidate.get("name")
        if type(name) is not str:
            return "unrecognized action name"
        contracts = _request_action_contracts(request)
        if name not in contracts:
            return f"action '{name}' is not allowed in state '{state}'"
        allowed = controller.get("allowed_actions")
        if isinstance(allowed, list) and name not in allowed:
            return f"action '{name}' is not allowed in state '{state}'"
        arguments = candidate.get("arguments")
        if not isinstance(arguments, Mapping):
            return "'arguments' must be a JSON object"
        failure = _validate_action_arguments(name, arguments, contracts)
        if failure is not None:
            return failure
        try:
            serialized = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            if len(serialized.encode("utf-8")) > MAX_DIRECTIVE_ARGUMENT_BYTES:
                return "action arguments exceed the byte bound"
        except (TypeError, ValueError, OverflowError, UnicodeError):
            return "action arguments are not strict finite JSON"
        return None
    if kind == "transition":
        target = candidate.get("target_state")
        legal = controller.get("legal_transition_targets")
        if type(target) is not str or not isinstance(legal, list) or target not in legal:
            return f"'{target}' is not reachable from '{state}'"
        reason = candidate.get("reason")
        failure = _validate_text_field(reason, MAX_DIRECTIVE_REASON_BYTES)
        if failure is not None:
            return "'reason' failed validation"
        return None
    if kind in ("add_hypothesis", "revise_hypothesis"):
        hypothesis_id = candidate.get("hypothesis_id")
        failure = _validate_text_field(hypothesis_id, MAX_DIRECTIVE_HYPOTHESIS_ID_BYTES)
        if failure is not None or any(
            not ("a" <= char <= "z" or "A" <= char <= "Z" or "0" <= char <= "9" or char in "-_.")
            for char in str(hypothesis_id)
        ):
            return "'hypothesis_id' failed validation"
        failure = _validate_text_field(candidate.get("statement"), MAX_DIRECTIVE_STATEMENT_BYTES)
        if failure is not None:
            return "'statement' failed validation"
        confidence = candidate.get("confidence")
        if type(confidence) is not str or confidence not in HYPOTHESIS_CONFIDENCE_VALUES:
            return "'confidence' must be low, medium, or high"
        evidence_refs = candidate.get("evidence_refs")
        if not isinstance(evidence_refs, list) or len(evidence_refs) > MAX_DIRECTIVE_EVIDENCE_REF_COUNT:
            return "'evidence_refs' must be a JSON array within the bound"
        seen: set[str] = set()
        for reference in evidence_refs:
            failure = _validate_text_field(reference, MAX_DIRECTIVE_EVIDENCE_REF_BYTES)
            if failure is not None or reference in seen:
                return "'evidence_refs' failed validation"
            seen.add(reference)
        if type(candidate.get("requires_runtime_evidence")) is not bool:
            return "'requires_runtime_evidence' must be a boolean"
        return None
    if kind == "set_hypothesis_status":
        hypothesis_id = candidate.get("hypothesis_id")
        failure = _validate_text_field(hypothesis_id, MAX_DIRECTIVE_HYPOTHESIS_ID_BYTES)
        if failure is not None or any(
            not ("a" <= char <= "z" or "A" <= char <= "Z" or "0" <= char <= "9" or char in "-_.")
            for char in str(hypothesis_id)
        ):
            return "'hypothesis_id' failed validation"
        status = candidate.get("status")
        if type(status) is not str or status not in HYPOTHESIS_STATUS_VALUES:
            return "unrecognized hypothesis status"
        return None
    return "unrecognized or missing directive 'kind'"


def _extract_directive(text: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """Schema-aware protocol-1.3 directive extraction.

    Enumerates every complete JSON object in the model text and validates
    each candidate through the strict protocol-1.3 directive parser
    (:func:`_validate_directive_candidate`) against the schema embedded in
    the request.  The result is accepted only when exactly one candidate is a
    fully valid directive; zero valid candidates are rejected and more than
    one valid candidate is rejected as ambiguous.  Copied request/config JSON
    objects are ignored only because they fail directive validation, never
    through heuristic key stripping.

    Rejections carry a precise bounded reason for the bounded correction
    feedback: when exactly one JSON candidate exists but is invalid, the
    exact validation reason (e.g. ``unknown argument field 'extra'``); when
    multiple candidates exist and none validate, a deterministic bounded
    reason without the full model output.  Requests without a protocol-1.3
    ``directive_schema`` keep the historical single-object extraction
    (:func:`_json_from_text`) unchanged.
    """
    if not _request_directive_schema(request):
        return _json_from_text(text)
    candidates = _json_objects(text)
    if not candidates:
        raise JsonExtractionError(
            "no_json_object",
            "OpenCode output did not contain a directive JSON object",
            reason="no JSON object in the output",
        )
    valid: list[dict[str, Any]] = []
    for candidate in candidates:
        if _validate_directive_candidate(candidate, request) is None:
            valid.append(candidate)
    if not valid:
        if len(candidates) == 1:
            reason = _validate_directive_candidate(candidates[0], request) or "no valid protocol directive"
        else:
            reason = "no JSON object in the output was a valid protocol directive"
        raise JsonExtractionError(
            "no_valid_directive",
            "OpenCode output contained no valid protocol directive",
            reason=reason,
        )
    if len(valid) > 1:
        raise JsonExtractionError(
            "ambiguous_json_output",
            "OpenCode output contained multiple valid protocol directives",
            reason="more than one valid protocol directive in the output",
        )
    return valid[0]


def _correction_message(classification: str, request: Mapping[str, Any], reason: str | None = None) -> str:
    """One compact machine-generated correction message for a rejected
    directive.

    Carries the precise bounded validation reason (never the previous model
    response), the required top-level kind envelope for the currently allowed
    directive kinds, the one-JSON-object rule, and the no-tools/code-fence/
    explanation rule.  The message is at most 200 characters (the accepted
    bounded rejection-detail limit) so the exact correction survives the
    bounded directive-feedback cycle untouched; when a pathological reason
    and a five-kind envelope would overflow the bound, only the reason tail
    is truncated.
    """
    schema = _request_directive_schema(request)
    kinds = sorted(schema) if schema else ["action", "transition"]
    union = "|".join(kinds)
    if classification == "ambiguous_json_output":
        precise = reason or "more than one valid protocol directive"
    else:
        precise = reason or "no valid protocol directive"
    fixed = f". One JSON object only; kind: [{union}]. No tools/code fence/explanation."
    room = 200 - len(fixed)
    if len(precise) > room:
        precise = precise[: max(0, room - 1)] + "…"
    return precise + fixed


def _event_text(event: Any) -> list[str]:
    found: list[str] = []
    if isinstance(event, dict):
        if isinstance(event.get("text"), str):
            found.append(event["text"])
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            found.append(part["text"])
        for value in event.values():
            if value is event.get("text") or value is part:
                continue
            found.extend(_event_text(value))
    elif isinstance(event, list):
        for value in event:
            found.extend(_event_text(value))
    return found


def _provider_events(value: str) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    non_json: list[str] = []
    for line in _strip_ansi(value).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            non_json.append(line)
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            non_json.append(line)
    return events, non_json


def _event_type_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = event.get("type")
        name = str(event_type) if event_type is not None else "<missing>"
        counts[name] = counts.get(name, 0) + 1
    return counts


def _structured_error_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("type", "")).lower()
        if "error" in event_type or "error" in event:
            found.append(event)
    return found


def _parse_failure_classification(
    stdout: str,
    events: list[dict[str, Any]],
    text_parts: list[str],
    structured_errors: list[dict[str, Any]],
) -> str:
    if not stdout.strip():
        return "empty_output"
    if structured_errors:
        return "structured_provider_error"
    if text_parts:
        return "text_without_protocol_directive"
    if events:
        known_types = {"text", "step_finish", "step_start", "message", "assistant", "tool_use", "patch"}
        if any(str(event.get("type", "")) not in known_types for event in events):
            return "unsupported_event_shape"
        return "no_text_event"
    return "text_without_protocol_directive"


def _provider_diagnostics(stdout: str, stderr: str, returncode: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[str], dict[str, Any] | None]:
    events, non_json = _provider_events(stdout)
    text_parts: list[str] = []
    for event in events:
        text_parts.extend(_event_text(event))
    structured_errors = _structured_error_events(events)
    usage = _usage(events)
    telemetry = _provider_telemetry(events)
    record = {
        "event": "provider_result_diagnostics",
        "provider_exit_code": returncode,
        "provider_stdout": stdout,
        "provider_stderr": stderr,
        "provider_stdout_character_count": len(stdout),
        "provider_stderr_character_count": len(stderr),
        "provider_stdout_truncated": len(stdout) > _MAX_EVIDENCE_FIELD_CHARS,
        "provider_stderr_truncated": len(stderr) > _MAX_EVIDENCE_FIELD_CHARS,
        "parsed_event_count": len(events),
        "event_type_counts": _event_type_counts(events),
        "parsed_events": events,
        "non_json_line_count": len(non_json),
        "non_json_samples": non_json[:8],
        "extracted_text_part_count": len(text_parts),
        "extracted_text_values": text_parts,
        "structured_error_events": structured_errors,
        "provider_telemetry": telemetry,
        "usage": usage,
    }
    return record, events, text_parts, structured_errors, telemetry


def _usage(events: list[Any]) -> dict[str, Any] | None:
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        candidates = [event.get("usage")]
        part = event.get("part")
        if isinstance(part, dict):
            candidates.append(part.get("tokens"))
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            result: dict[str, Any] = {}
            for target, names in {
                "prompt_tokens": ("prompt_tokens", "promptTokens", "input_tokens", "inputTokens", "input"),
                "completion_tokens": ("completion_tokens", "completionTokens", "output_tokens", "outputTokens", "output"),
                "total_tokens": ("total_tokens", "totalTokens", "total"),
            }.items():
                for name in names:
                    if type(candidate.get(name)) is int and candidate[name] >= 0:
                        result[target] = candidate[name]
                        break
            if result:
                return result
    return None


def _numeric(value: Any) -> int | float | None:
    if type(value) in (int, float):
        return value
    return None


def _provider_telemetry(events: list[Any]) -> dict[str, Any] | None:
    """Collect fields emitted by OpenCode without filling absent fields.

    Independently observed identity fields (``observed_model``,
    ``observed_billing_route``, ``observed_model_substitution``) are passed
    through when a provider emits them so the outer execution adapter can
    revalidate the runtime identity binding from provider-reported state.
    """
    result: dict[str, Any] = {}
    _OBSERVED_IDENTITY_KEYS = ("observed_model", "observed_billing_route", "observed_model_substitution")

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"input", "output", "reasoning", "cost"}:
                    numeric = _numeric(item)
                    if numeric is not None:
                        result[key] = numeric
                elif key in _OBSERVED_IDENTITY_KEYS and item is not None:
                    result[key] = item
                elif key == "cache" and isinstance(item, dict):
                    cache: dict[str, Any] = dict(result.get("cache", {}))
                    for cache_key in ("read", "write"):
                        numeric = _numeric(item.get(cache_key))
                        if numeric is not None:
                            cache[cache_key] = numeric
                    if cache:
                        result["cache"] = cache
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for event in events:
        visit(event)
    return result or None


def _bound_evidence(value: Any, limit: int = _MAX_EVIDENCE_FIELD_CHARS) -> Any:
    """Bound strings before JSON serialization while retaining truncation facts."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return {"value": value[:limit], "truncated": True, "original_character_count": len(value)}
    if isinstance(value, dict):
        return {str(key): _bound_evidence(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_bound_evidence(item, limit) for item in value]
    return value


def _record(path: Path | None, record: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    redacted = _redact(record)
    payload = json.dumps(_bound_evidence(redacted), ensure_ascii=False, sort_keys=True)
    if len(payload) > _MAX_EVIDENCE_CHARS:
        original_character_count = len(payload)
        compact = {"truncated": True, "original_character_count": original_character_count, "record": _bound_evidence(redacted, limit=1_024)}
        payload = json.dumps(compact, ensure_ascii=False, sort_keys=True)
        if len(payload) > _MAX_EVIDENCE_CHARS:
            payload = json.dumps({
                "truncated": True, "original_character_count": original_character_count,
                "record_type": record.get("event", "transport"),
            }, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(payload + "\n")


def canonical_public_request(request: Mapping[str, Any]) -> str:
    """The canonical compact JSON serialization of the public request.

    Uses the project's canonical JSON rules (sorted keys, compact separators,
    ASCII-escaped, strict finite JSON) shared by the deterministic
    catalog-entry fingerprint contract (:func:`catalog_entry_fingerprint`).
    """
    if not isinstance(request, Mapping):
        raise ValueError("OpenCode protocol request must be an object")
    return json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def build_user_message(request: Mapping[str, Any]) -> str:
    """The single inline OpenCode user message for one protocol request.

    The message carries a brief protocol instruction, the canonical public
    request between the explicit :data:`PUBLIC_REQUEST_START` /
    :data:`PUBLIC_REQUEST_END` delimiters, compact exact output-shape examples
    (action, transition, add_hypothesis, revise_hypothesis), and explicit
    prohibitions against code fences, explanations, tool calls,
    protocol/version wrappers, and alternate envelopes.  The allowed actions
    and their argument contracts inside the embedded request are authoritative.

    The 20,000-byte public-evidence limit applies to the canonical public
    request serialization (:func:`canonical_public_request`), never to the
    complete user message: a canonical request up to and including
    :data:`MAX_PUBLIC_EVIDENCE_BYTES` bytes is accepted and its complete
    message is constructed unchanged (the canonical request is never
    truncated, reduced, summarized, split, or mutated).  The fully
    constructed native command is independently bounded by
    :data:`MAX_NATIVE_COMMAND_LINE_CHARS` in
    :func:`build_opencode_command`; exceeding either bound fails closed
    before any model process may run.
    """
    if not isinstance(request, Mapping):
        raise ValueError("OpenCode protocol request must be an object")
    canonical = canonical_public_request(request)
    request_byte_count = len(canonical.encode("utf-8"))
    if request_byte_count > MAX_PUBLIC_EVIDENCE_BYTES:
        raise ValueError(
            f"OpenCode canonical public request exceeds the public-evidence byte budget "
            f"({request_byte_count} > {MAX_PUBLIC_EVIDENCE_BYTES})"
        )
    message = (
        PROTOCOL_INSTRUCTION
        + " "
        + PUBLIC_REQUEST_START
        + " "
        + canonical
        + " "
        + PUBLIC_REQUEST_END
        + " "
        + "Exact output shapes: "
        + DIRECTIVE_OUTPUT_EXAMPLES
        + " "
        + DIRECTIVE_OUTPUT_PROHIBITIONS
    )
    return message


def build_opencode_command(model: str, variant: str, root: Path, message: str, executable: str | Path) -> list[str]:
    """The full ``opencode run`` argv for model execution.

    ``executable`` is the absolute native ``opencode.exe`` path resolved and
    version-proven from the verified batch launcher (see
    :func:`verify_opencode_native_executable`); it is used directly as
    argv[0] with ``shell=False`` and there is never a silent fallback to the
    ``opencode.cmd`` batch shim, PATH ambiguity, PowerShell, or shell
    interpolation.  The isolated ``--dir`` is retained.

    The fully constructed command must fit inside
    :data:`MAX_NATIVE_COMMAND_LINE_CHARS` (``subprocess.list2cmdline``
    character count, a documented bound below the Windows CreateProcess
    command-line maximum of 32767); exceeding it fails closed before process
    creation.
    """
    if not isinstance(message, str) or not message.strip():
        raise ValueError("OpenCode positional protocol message must be non-empty")
    if not isinstance(executable, (str, Path)) or not str(executable).strip():
        raise ValueError("OpenCode native executable path must be supplied")
    command = [
        str(executable), "run", message, "--pure", "--format", "json",
        "--model", model, "--variant", variant, "--dir", str(root),
    ]
    command_line = subprocess.list2cmdline(command)
    if len(command_line) > MAX_NATIVE_COMMAND_LINE_CHARS:
        raise ValueError(
            f"OpenCode native command line exceeds the safety bound "
            f"({len(command_line)} > {MAX_NATIVE_COMMAND_LINE_CHARS} characters)"
        )
    return command


def _windows_profile_path() -> Path:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, 40, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise RuntimeError("required Windows user profile could not be determined")
    profile = Path(buffer.value)
    if not profile.is_absolute() or not profile.is_dir():
        raise RuntimeError("Windows profile API returned an untrusted profile path")
    return profile


def _profile_path() -> Path:
    try:
        profile = Path.home()
        if profile.is_absolute() and profile.is_dir():
            return profile
    except RuntimeError:
        pass
    if os.name != "nt":
        raise RuntimeError("required user profile could not be determined")
    return _windows_profile_path()


def _auth_state_path() -> Path:
    profile = _profile_path()
    return profile / ".local" / "share" / "opencode" / "auth.json"


def _route_expected_provider(route_mode: str) -> str:
    """The exact provider the active route mode may allow, never inferred."""
    if route_mode not in ROUTE_MODES:
        raise RuntimeError(f"unsupported route mode: {route_mode!r}")
    return OPENCODE_GO_CATALOG_PROVIDER if route_mode == "opencode-go" else LEGACY_CATALOG_PROVIDER


def _isolation_config(route_mode: str = "legacy") -> dict[str, Any]:
    """The isolation configuration, with the exact enabled provider for the
    active route: ``opencode-go`` in OpenCode Go mode and the historical
    ``opencode`` allowlist in legacy mode.  All permission, MCP, plugin,
    instruction, sharing, and autoupdate denials are preserved."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": dict(_ISOLATION_PERMISSION_DENIALS),
        "mcp": {"*": {"enabled": False}},
        "plugin": [],
        "instructions": [],
        "share": "disabled",
        "enabled_providers": [_route_expected_provider(route_mode)],
        "autoupdate": False,
    }


def _prepare_isolation(root: Path, route_mode: str = "legacy") -> dict[str, Any]:
    isolation_root = root / "opencode-isolation"
    config_home = isolation_root / "config-home"
    data_home = isolation_root / "data-home"
    state_home = isolation_root / "state-home"
    cache_home = isolation_root / "cache-home"
    for path in (config_home, data_home, state_home, cache_home):
        path.mkdir(parents=True, exist_ok=True)
    config_path = isolation_root / "opencode.json"
    config = _isolation_config(route_mode=route_mode)
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    agents_path = root / "AGENTS.md"
    agents_path.write_text(_AGENTS_CONTENT + "\n", encoding="utf-8")
    auth_source = _auth_state_path()
    if not auth_source.is_file():
        raise RuntimeError("required OpenCode authentication state is unavailable")
    auth_copy = data_home / "opencode" / "auth.json"
    auth_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(auth_source, auth_copy)
    inherited_names = ("PATH", "PATHEXT", "SystemRoot", "WINDIR", "COMSPEC")
    environment = {name: os.environ[name] for name in inherited_names if os.environ.get(name)}
    home_drive, home_path = os.path.splitdrive(str(isolation_root))
    environment.update({
        "HOME": str(isolation_root),
        "USERPROFILE": str(isolation_root),
        "HOMEDRIVE": home_drive,
        "HOMEPATH": home_path,
        "APPDATA": str(isolation_root / "appdata"),
        "LOCALAPPDATA": str(isolation_root / "localappdata"),
        "TEMP": str(isolation_root / "tmp"),
        "TMP": str(isolation_root / "tmp"),
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_CONFIG_DIR": str(config_home),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
        "XDG_CACHE_HOME": str(cache_home),
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
    })
    Path(environment["TEMP"]).mkdir(parents=True, exist_ok=True)
    return {
        "route_mode": route_mode,
        "environment": environment,
        "config_path": config_path,
        "config": config,
        "agents_path": agents_path,
        "agents_sha256": hashlib.sha256(agents_path.read_bytes()).hexdigest(),
        "auth_copy": auth_copy,
        "auth_sha256": hashlib.sha256(auth_copy.read_bytes()).hexdigest(),
    }


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def _validate_effective_config(config: Any, *, route_mode: str = "legacy") -> dict[str, Any]:
    if not isinstance(config, dict):
        raise RuntimeError("OpenCode effective configuration was not an object")
    permission = config.get("permission")
    if not isinstance(permission, dict):
        raise RuntimeError("OpenCode effective configuration has no permission object")
    required_denials = ("read", "write", "edit", "bash", "task", "webfetch", "websearch", "external_directory")
    if permission.get("*") != "deny" or any(permission.get(name) != "deny" for name in required_denials):
        raise RuntimeError("OpenCode effective configuration does not deny required permissions")
    mcp = config.get("mcp")
    if not isinstance(mcp, dict) or any(
        not isinstance(value, dict) or value.get("enabled") is not False
        for value in mcp.values()
    ):
        raise RuntimeError("OpenCode effective configuration enables an MCP server")
    if config.get("plugin") != []:
        raise RuntimeError("OpenCode effective configuration enables a plugin")
    if config.get("instructions") != []:
        raise RuntimeError("OpenCode effective configuration includes unrelated instructions")
    if config.get("share") != "disabled":
        raise RuntimeError("OpenCode sharing is not disabled")
    expected_provider = _route_expected_provider(route_mode)
    if config.get("enabled_providers") != [expected_provider]:
        raise RuntimeError(
            f"OpenCode enabled provider allowlist is not exactly [{expected_provider}] for route mode {route_mode!r}"
        )
    if config.get("autoupdate") is not False:
        raise RuntimeError("OpenCode autoupdate is not disabled")
    return {
        "permission_default_denied": True,
        "required_permissions_denied": list(required_denials),
        "mcp_servers_disabled": True,
        "plugins_empty": True,
        "instructions_empty": True,
        "sharing_disabled": True,
        "enabled_providers": [expected_provider],
        "autoupdate_disabled": True,
    }


def verify_opencode_effective_config(
    environment: dict[str, str],
    cwd: Path,
    *,
    route_mode: str = "legacy",
) -> dict[str, Any]:
    completed = subprocess.run(
        ["opencode.cmd", "debug", "config", "--pure"],
        cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        env=environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"OpenCode effective config failed with exit code {completed.returncode}")
    try:
        config = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenCode effective config was not valid JSON") from exc
    return _validate_effective_config(config, route_mode=route_mode)


def _preflight(args: argparse.Namespace) -> int:
    """Perform all local wrapper gates without invoking ``opencode run``."""
    root = Path(tempfile.gettempdir()) / f"agentic-opencode-preflight-{uuid.uuid4().hex}"
    root.mkdir()
    evidence_path = Path(args.evidence_file) if args.evidence_file else None
    try:
        route_binding = _route_binding_evidence(args) if args.route_mode == "opencode-go" else None
        observation = observe_isolated_catalog(
            args.model, args.variant,
            route_mode=args.route_mode,
            expected_opencode_version=route_binding["expected_opencode_version"] if route_binding is not None else None,
            expected_runtime_model_id=route_binding["expected_runtime_model_id"] if route_binding is not None else None,
            expected_catalog_fingerprint=route_binding["expected_catalog_fingerprint"] if route_binding is not None else None,
            isolation_root=root,
        )
        launcher = observation["launcher"]
        native = observation["native"]
        catalog = observation["catalog"]
        effective_config = observation["effective_config"]
        isolation = observation["isolation"]
        message = build_user_message({})
        command = build_opencode_command(args.model, args.variant, root, message, executable=native["native_executable"])
        if not command[command.index("run") + 1].strip():
            raise RuntimeError("final OpenCode command failed message validation")
        if command.index("run") != 1 or command.index("--pure") != 3 or "--file" in command:
            raise RuntimeError("final OpenCode command failed the inline request message contract")
        if command[command.index("--dir") + 2:] != []:
            raise RuntimeError("final OpenCode command carries trailing positional values")
        assertions = {
            "preflight": "passed",
            "provider_inference_started": False,
            "route_mode": args.route_mode,
            "launcher": launcher,
            "native_executable": native,
            "catalog": catalog,
            "effective_config": effective_config,
            "message_nonempty": bool(command[2].strip()),
            "message_is_single_positional": command.index("run") == 1 and command.index("--pure") == 3,
            "file_argument_absent": "--file" not in command,
            "trailing_positional_values_absent": command[command.index("--dir") + 2:] == [],
            "message_inline_request_present": (
                PUBLIC_REQUEST_START in command[2] and PUBLIC_REQUEST_END in command[2]
            ),
            "message_byte_count": len(command[2].encode("utf-8")),
            "request_within_public_evidence_budget": (
                len(canonical_public_request({}).encode("utf-8")) <= MAX_PUBLIC_EVIDENCE_BYTES
            ),
            "command_line_character_count": len(subprocess.list2cmdline(command)),
            "command_line_within_native_bound": len(subprocess.list2cmdline(command)) <= MAX_NATIVE_COMMAND_LINE_CHARS,
            "agents_present_during_preflight": isolation["agents_path"].is_file(),
            "config_copy_present_during_preflight": isolation["config_path"].is_file(),
            "auth_copy_present_during_preflight": isolation["auth_copy"].is_file(),
            "auth_source_resolved": True,
            "auth_sha256": isolation["auth_sha256"],
            "config_sha256": hashlib.sha256(isolation["config_path"].read_bytes()).hexdigest(),
            "agents_sha256": isolation["agents_sha256"],
            "command": command,
        }
        if route_binding is not None:
            assertions["route_binding"] = route_binding
        _record(evidence_path, assertions)
        print(json.dumps(assertions, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        failure = {"preflight": "blocked", "provider_inference_started": False}
        failure.update(_failure_evidence(exc))
        _record(evidence_path, failure)
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        return 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _expected_opencode_version(args: argparse.Namespace) -> str | None:
    if args.route_mode != "opencode-go":
        return None
    value = (args.expected_opencode_version or "").strip()
    if not value:
        raise RuntimeError("OpenCode Go mode requires --expected-opencode-version")
    return value


def _expected_runtime_model_id(args: argparse.Namespace) -> str | None:
    if args.route_mode != "opencode-go":
        return None
    value = (args.expected_runtime_model_id or "").strip()
    if not value:
        raise RuntimeError("OpenCode Go mode requires --expected-runtime-model-id")
    return value


def _route_binding_evidence(args: argparse.Namespace) -> dict[str, Any]:
    """The identity/route binding carried into OpenCode Go mode.

    Every value was already validated by the outer authorization/preflight
    contract; the wrapper requires them explicitly (fail closed on absence)
    and records them, without re-querying any catalog/account/entitlement
    service and without inferring Zen/free-tier use.
    """
    fingerprint = (args.expected_catalog_fingerprint or "").strip()
    if not (len(fingerprint) == 64 and all(char in "0123456789abcdef" for char in fingerprint)):
        raise RuntimeError("OpenCode Go mode requires --expected-catalog-fingerprint as a 64-hex string")
    account_status = (args.expected_account_status or "").strip()
    if not account_status:
        raise RuntimeError("OpenCode Go mode requires --expected-account-status")
    billing_route = (args.expected_billing_route or "").strip()
    if not billing_route:
        raise RuntimeError("OpenCode Go mode requires --expected-billing-route")
    expected_version = _expected_opencode_version(args)
    expected_runtime_model_id = _expected_runtime_model_id(args)
    if args.model != expected_runtime_model_id:
        raise RuntimeError(
            f"model identity {args.model!r} does not match the expected runtime model identity {expected_runtime_model_id!r}"
        )
    return {
        "expected_runtime_model_id": expected_runtime_model_id,
        "expected_opencode_version": expected_version,
        "expected_catalog_fingerprint": fingerprint,
        "expected_account_status": account_status,
        "expected_billing_route": billing_route,
    }


def _json_objects(value: str) -> list[dict[str, Any]]:
    """Enumerate the TOP-LEVEL JSON objects in the text.

    Nested objects inside a decoded object are not separate candidates: a
    single directive attempt with nested ``arguments`` counts as exactly one
    candidate, so the schema-aware extraction can carry the exact validation
    reason of the one candidate the model produced.
    """
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    clean = _strip_ansi(value)
    offset = 0
    while offset < len(clean):
        if clean[offset] != "{":
            offset += 1
            continue
        try:
            candidate, end = decoder.raw_decode(clean[offset:])
        except json.JSONDecodeError:
            offset += 1
            continue
        if isinstance(candidate, dict):
            objects.append(candidate)
            offset += end
        else:
            offset += 1
    return objects


def catalog_entry_fingerprint(entry: Mapping[str, Any]) -> str:
    """The deterministic catalog-entry fingerprint contract.

    The exact selected catalog entry is serialized with the project's
    canonical JSON rules (sorted keys, compact separators, ASCII-escaped,
    strict finite JSON — the same canonical rules used by the paired-pilot
    validators) and SHA-256 of that canonical representation is returned.
    The same independently recomputed fingerprint is used in route evidence,
    the operator authorization, the adapter configuration, and the wrapper's
    OpenCode Go preflight comparison.
    """
    return hashlib.sha256(
        json.dumps(
            entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def select_catalog_entry(catalog_stdout: str, model: str) -> dict[str, Any]:
    """Locate exactly one active catalog entry for the exact model identity.

    ``model`` must be a catalog-qualified identity (``provider/id``).  Zero
    matches, multiple matches, or a non-object catalog shape fail closed.
    """
    provider, model_id = model.split("/", 1) if "/" in model else ("", model)
    entries = [
        item for item in _json_objects(catalog_stdout)
        if item.get("providerID") == provider and item.get("id") == model_id
    ]
    if len(entries) != 1:
        raise RuntimeError("exact OpenCode model was not uniquely present in the local catalog")
    return entries[0]


def catalog_entry_facts(entry: Mapping[str, Any], variant: str) -> dict[str, Any]:
    """Observed status, variant availability, and finite pricing metadata for
    the exact selected catalog entry.

    Shared by the wrapper's OpenCode Go preflight and the operator route
    capture so the observed facts and the fingerprint always come from one
    coherent parsing path.  Rejects inactive status, malformed or non-finite
    pricing metadata, and a missing requested variant.
    """
    costs = entry.get("cost")
    cache = costs.get("cache") if isinstance(costs, dict) else None
    if entry.get("status") != "active" or not isinstance(costs, dict) or not isinstance(cache, dict):
        raise RuntimeError("exact OpenCode model is not active or has incomplete pricing metadata")
    for name in ("input", "output"):
        value = costs.get(name)
        if type(value) not in (int, float) or isinstance(value, bool) or value < 0:
            raise RuntimeError("exact OpenCode model has malformed pricing metadata")
    for name in ("read", "write"):
        value = cache.get(name)
        if type(value) not in (int, float) or isinstance(value, bool) or value < 0:
            raise RuntimeError("exact OpenCode model has malformed cache pricing metadata")
    variants = entry.get("variants")
    if not isinstance(variants, dict) or variant not in variants:
        raise RuntimeError("requested OpenCode model variant is unavailable")
    return {
        "active_model_status": "ACTIVE",
        "variant_available": True,
        "input_price": costs["input"],
        "output_price": costs["output"],
        "cache_read_price": cache["read"],
        "cache_write_price": cache["write"],
    }


def verify_opencode_launcher(environment: dict[str, str] | None = None, *, expected_version: str | None = None) -> dict[str, Any]:
    """Verify the installed Windows launcher without contacting a model.

    ``expected_version`` (OpenCode Go mode) requires the observed launcher
    version to equal the authorization-bound version exactly; the legacy mode
    keeps the historical behavior (any non-empty version).
    """
    launcher = shutil.which("opencode.cmd")
    if not launcher:
        raise RuntimeError("opencode.cmd was not found on PATH")
    completed = subprocess.run(
        ["opencode.cmd", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", timeout=30, check=False, env=environment,
    )
    version = (completed.stdout or completed.stderr or "").strip()
    evidence = {"launcher": "opencode.cmd", "resolved_path": launcher, "returncode": completed.returncode, "version": version}
    if completed.returncode != 0 or not version:
        raise RuntimeError(f"opencode.cmd version preflight failed: {_redact(evidence)}")
    if expected_version is not None:
        if version != expected_version:
            raise RuntimeError(f"OpenCode version drift: observed {version!r} != expected {expected_version!r}")
        evidence["version_matches_expected"] = True
    return evidence


def _npm_package_root(launcher_path: str) -> Path:
    """The trusted npm package root derived ONLY from the verified launcher.

    ``<launcher-directory>\\node_modules\\opencode-ai`` resolved to an
    absolute path; the native executable must belong to this root.
    """
    if not isinstance(launcher_path, str) or not launcher_path:
        raise RuntimeError("OpenCode launcher path is missing")
    launcher = Path(launcher_path).resolve()
    package_root = (launcher.parent / "node_modules" / "opencode-ai").resolve()
    if not package_root.is_absolute():
        raise RuntimeError("trusted npm package root is not an absolute path")
    if not package_root.is_dir():
        raise RuntimeError(f"trusted npm package root is missing or not a directory: {package_root}")
    return package_root


def _resolve_native_executable(launcher_path: str) -> Path:
    """Resolve the native ``opencode.exe`` selected by the npm shim.

    Begins only from the independently verified absolute ``opencode.cmd``
    launcher path, derives the trusted npm package root
    ``<launcher-directory>\\node_modules\\opencode-ai``
    (:func:`_npm_package_root`), and resolves the single deterministic
    npm-shim target ``<package-root>\\bin\\opencode.exe``
    (:data:`NATIVE_EXECUTABLE_RELATIVE`).  The resolved absolute path must
    remain inside the trusted package root (no symlink/reparse/path escape)
    and must exist as a regular file; otherwise resolution fails closed.
    Platform and baseline package binaries are never enumerated or compared,
    and there is no recursive search.  Never PATH lookup,
    environment-supplied executable paths, shell interpolation, PowerShell
    execution, parsing an unrestricted command from the batch file, or a
    fallback to ``opencode.cmd``.
    """
    package_root = _npm_package_root(launcher_path)
    native = (package_root / NATIVE_EXECUTABLE_RELATIVE).resolve()
    if not native.is_absolute():
        raise RuntimeError("native OpenCode executable path is not an absolute path")
    try:
        native.relative_to(package_root)
    except ValueError:
        raise RuntimeError(
            "native OpenCode executable path escapes the trusted npm package root"
        ) from None
    if not native.is_file():
        raise RuntimeError(
            f"native OpenCode executable was not found under the trusted npm package root: {native}"
        )
    return native


def verify_opencode_native_executable(
    environment: dict[str, str],
    *,
    launcher_path: str,
    launcher_version: str,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Prove the native ``opencode.exe`` represents the same installation.

    Resolves the native executable through the trusted npm-installation
    resolution contract (:func:`_resolve_native_executable`), requires it to
    be a regular file contained in the trusted ``opencode-ai`` package root,
    and requires it to report the exact same OpenCode version as the batch
    launcher (proving both represent the same installed OpenCode
    installation).  OpenCode Go mode additionally requires the exact
    authorization-bound version.  Fails closed if the native executable
    cannot be resolved or its version differs.  Returns bounded resolution
    evidence only — resolution strategy, the package-relative native path,
    and the regular-file/root-containment/version-match flags — never
    executable bytes or unrestricted environment data.
    """
    native = _resolve_native_executable(launcher_path)
    package_root = _npm_package_root(launcher_path)
    completed = subprocess.run(
        [str(native), "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        env=environment,
    )
    version = (completed.stdout or completed.stderr or "").strip()
    evidence: dict[str, Any] = {
        "resolution_strategy": "npm-package-layout",
        "native_executable": str(native),
        "package_relative_path": str(native.relative_to(package_root)),
        "native_version": version,
        "regular_file": True,
        "root_containment": True,
        "returncode": completed.returncode,
    }
    if completed.returncode != 0 or not version:
        raise RuntimeError(f"native OpenCode executable version preflight failed: {_redact(evidence)}")
    if version != launcher_version:
        raise RuntimeError(
            f"native OpenCode executable version drift: observed {version!r} != launcher {launcher_version!r}"
        )
    evidence["version_matches_launcher"] = True
    if expected_version is not None:
        if version != expected_version:
            raise RuntimeError(f"OpenCode version drift: observed {version!r} != expected {expected_version!r}")
        evidence["version_matches_expected"] = True
    return evidence


def _catalog_command(route_mode: str) -> list[str]:
    """The single local/non-model catalog inspection command per route mode.

    Legacy mode keeps the historical query unchanged (``models opencode``);
    OpenCode Go mode queries exactly ``models opencode-go --verbose --pure``.
    ``opencode run`` is never constructed or executed here.
    """
    if route_mode == "opencode-go":
        return ["opencode.cmd", "models", OPENCODE_GO_CATALOG_PROVIDER, "--verbose", "--pure"]
    return ["opencode.cmd", "models", LEGACY_CATALOG_PROVIDER, "--verbose", "--pure"]


def _require_go_runtime_identity(model: str, route_mode: str) -> None:
    """Reject every non-OpenCode-Go catalog-qualified identity in Go mode.

    ``opencode/`` (including the historical
    ``opencode/deepseek-v4-flash-free`` Zen free-model identity) and any
    other provider is rejected before any model process may run.
    """
    if route_mode != "opencode-go":
        return
    if not isinstance(model, str) or not model.startswith(OPENCODE_GO_RUNTIME_ID_PREFIX):
        raise RuntimeError(
            f"OpenCode Go mode requires the exact opencode-go/ catalog-qualified runtime model identity; rejected {model!r}"
        )


def _enforce_catalog_route_checks(
    entry: Mapping[str, Any],
    model: str,
    fingerprint: str,
    *,
    route_mode: str,
    expected_runtime_model_id: str | None,
    expected_catalog_fingerprint: str | None,
) -> None:
    """Route-mode checks on the exact selected catalog entry.

    Legacy mode preserves the historical zero-cost requirement unchanged.
    OpenCode Go mode rejects runtime-identity drift and, when an
    authorization-bound expected fingerprint is supplied, independently
    compares the recomputed exact-entry fingerprint with it (the shared
    catalog observation never trusts the expected value; route capture
    supplies no expected fingerprint and performs pure observation).
    """
    if route_mode == "legacy":
        costs = entry.get("cost")
        if any(costs.get(name) != 0 for name in ("input", "output")) or any(costs["cache"].get(name) != 0 for name in ("read", "write")):
            raise RuntimeError("exact OpenCode model is not zero-cost")
        return
    if expected_runtime_model_id is not None and model != expected_runtime_model_id:
        raise RuntimeError(
            f"model identity {model!r} does not match the expected runtime model identity {expected_runtime_model_id!r}"
        )
    if expected_catalog_fingerprint is not None and fingerprint != expected_catalog_fingerprint:
        raise RuntimeError(
            f"catalog fingerprint drift: independently recomputed fingerprint {fingerprint} "
            f"does not equal the authorization-bound expected fingerprint {expected_catalog_fingerprint}"
        )


def _catalog_entry_observation(
    environment: dict[str, str],
    cwd: Path | None,
    *,
    route_mode: str,
    model: str,
    variant: str,
    expected_runtime_model_id: str | None = None,
    expected_catalog_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Run the route-mode catalog inspection and parse the exact selected
    entry through the single shared select/facts/fingerprint path.

    The one local/non-model inspection command is ``opencode.cmd models
    <provider> --verbose --pure``; ``opencode run`` is never constructed or
    executed here.  Returns the exact selected entry, the observed facts, the
    deterministic exact-entry canonical JSON SHA-256 fingerprint, and the
    route-mode catalog verification record.  A nonzero inspection exit is a
    typed :class:`CatalogFailureError` with bounded, sanitized detail.
    """
    completed = subprocess.run(
        _catalog_command(route_mode),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", timeout=30, check=False, env=environment,
        cwd=str(cwd) if cwd is not None else None,
    )
    if completed.returncode != 0:
        raise CatalogFailureError(
            "catalog_command_failed",
            _catalog_failure_detail(completed.returncode, completed.stdout, completed.stderr, _catalog_command(route_mode)),
        )
    entry = select_catalog_entry(completed.stdout, model)
    facts = catalog_entry_facts(entry, variant)
    fingerprint = catalog_entry_fingerprint(entry)
    _enforce_catalog_route_checks(
        entry, model, fingerprint,
        route_mode=route_mode,
        expected_runtime_model_id=expected_runtime_model_id,
        expected_catalog_fingerprint=expected_catalog_fingerprint,
    )
    return {
        "entry": entry,
        "facts": facts,
        "fingerprint": fingerprint,
        "catalog": {
            "model": model,
            "catalog_provider": LEGACY_CATALOG_PROVIDER if route_mode == "legacy" else OPENCODE_GO_CATALOG_PROVIDER,
            "active": True,
            "zero_cost": facts["input_price"] == 0 and facts["output_price"] == 0,
            "catalog_fingerprint": fingerprint,
            "variant": variant,
            "variant_available": True,
            "route_mode": route_mode,
        },
    }


def verify_opencode_catalog(
    model: str,
    variant: str,
    environment: dict[str, str],
    cwd: Path | None = None,
    *,
    route_mode: str = "legacy",
    expected_runtime_model_id: str | None = None,
    expected_catalog_fingerprint: str | None = None,
) -> dict[str, Any]:
    if route_mode not in ROUTE_MODES:
        raise RuntimeError(f"unsupported route mode: {route_mode!r}")
    _require_go_runtime_identity(model, route_mode)
    if route_mode == "opencode-go" and expected_catalog_fingerprint is None:
        raise RuntimeError("OpenCode Go mode requires --expected-catalog-fingerprint")
    observation = _catalog_entry_observation(
        environment, cwd, route_mode=route_mode, model=model, variant=variant,
        expected_runtime_model_id=expected_runtime_model_id,
        expected_catalog_fingerprint=expected_catalog_fingerprint,
    )
    return observation["catalog"]


def observe_isolated_catalog(
    model: str,
    variant: str,
    *,
    route_mode: str = "legacy",
    expected_opencode_version: str | None = None,
    expected_runtime_model_id: str | None = None,
    expected_catalog_fingerprint: str | None = None,
    isolation_root: Path | None = None,
) -> dict[str, Any]:
    """The shared isolated catalog-observation path.

    Operator route capture and wrapper catalog verification both observe the
    OpenCode launcher version and the exact selected catalog entry under ONE
    deterministic isolated OpenCode configuration: a fresh temporary
    isolation root prepared with the exact route-mode isolation contract
    (permission, MCP, plugin, instruction, sharing, and autoupdate denials
    and the exact enabled-provider allowlist), the exact effective
    configuration is required, and only the local/non-model inspection
    commands run (``opencode.cmd --version``, the native
    ``opencode.exe --version`` proof, and the route-mode ``models``
    inspection); ``opencode run`` is never constructed or executed here.
    The native ``opencode.exe`` selected by the npm shim (the
    ``bin\opencode.exe`` target under the trusted ``opencode-ai`` package
    root) is resolved and version-proven against the launcher (same
    installation) and, in OpenCode Go mode, against the authorization-bound
    expected version.

    When ``isolation_root`` is None the helper owns a temporary isolation
    root and always removes it (success or failure) before returning.  When a
    root is supplied the caller owns cleanup (the wrapper keeps the same
    isolation alive for the run phase).  In OpenCode Go mode the wrapper
    supplies the authorization-bound expected fingerprint and the helper
    independently compares the recomputed exact-entry fingerprint with it;
    route capture supplies no expected fingerprint (pure observation, no
    fabricated binding).

    Returns the launcher evidence, the bounded native-executable identity
    evidence, the catalog verification record, the exact selected catalog
    entry, the observed facts, the deterministic exact-entry canonical JSON
    SHA-256 fingerprint, the effective-configuration validation, the exact
    inspection command inventory, the isolation record (environment/config/
    auth copies needed by callers that continue into ``opencode run``), and
    whether the helper-owned temporary isolation root was cleaned.
    """
    if route_mode not in ROUTE_MODES:
        raise RuntimeError(f"unsupported route mode: {route_mode!r}")
    _require_go_runtime_identity(model, route_mode)
    created_root = isolation_root is None
    root = (
        isolation_root
        if isolation_root is not None
        else Path(tempfile.gettempdir()) / f"agentic-opencode-isolated-catalog-{uuid.uuid4().hex}"
    )
    if created_root:
        root.mkdir()
    try:
        isolation = _prepare_isolation(root, route_mode=route_mode)
        launcher = verify_opencode_launcher(isolation["environment"], expected_version=expected_opencode_version)
        native = verify_opencode_native_executable(
            isolation["environment"],
            launcher_path=launcher["resolved_path"],
            launcher_version=str(launcher["version"]),
            expected_version=expected_opencode_version,
        )
        observation = _catalog_entry_observation(
            isolation["environment"], root, route_mode=route_mode, model=model, variant=variant,
            expected_runtime_model_id=expected_runtime_model_id,
            expected_catalog_fingerprint=expected_catalog_fingerprint,
        )
        effective_config = verify_opencode_effective_config(isolation["environment"], cwd=root, route_mode=route_mode)
        return {
            "route_mode": route_mode,
            "launcher": launcher,
            "native": native,
            "catalog": observation["catalog"],
            "entry": observation["entry"],
            "facts": observation["facts"],
            "fingerprint": observation["fingerprint"],
            "effective_config": effective_config,
            "inspection_commands": [
                ["opencode.cmd", "--version"],
                [native["native_executable"], "--version"],
                _catalog_command(route_mode),
            ],
            "isolation": isolation,
            "temporary_isolation_cleaned": created_root,
        }
    finally:
        if created_root:
            shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--evidence-file")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--route-mode", choices=ROUTE_MODES, default="legacy")
    parser.add_argument("--expected-opencode-version")
    parser.add_argument("--expected-catalog-fingerprint")
    parser.add_argument("--expected-runtime-model-id")
    parser.add_argument("--expected-account-status")
    parser.add_argument("--expected-billing-route")
    args = parser.parse_args(argv)
    if args.preflight:
        return _preflight(args)
    request_line = sys.stdin.readline()
    try:
        request = json.loads(request_line)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"invalid protocol request: {exc}", file=sys.stderr)
        return 2

    root = Path(tempfile.gettempdir()) / f"agentic-opencode-transport-{uuid.uuid4().hex}"
    root.mkdir()
    command: list[str] = []
    request_sha256: str | None = None
    request_byte_count: int | None = None
    try:
        message = build_user_message(request)
        canonical_request = canonical_public_request(request)
        request_sha256 = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        request_byte_count = len(canonical_request.encode("utf-8"))
        route_binding = _route_binding_evidence(args) if args.route_mode == "opencode-go" else None
        observation = observe_isolated_catalog(
            args.model, args.variant,
            route_mode=args.route_mode,
            expected_opencode_version=route_binding["expected_opencode_version"] if route_binding is not None else None,
            expected_runtime_model_id=route_binding["expected_runtime_model_id"] if route_binding is not None else None,
            expected_catalog_fingerprint=route_binding["expected_catalog_fingerprint"] if route_binding is not None else None,
            isolation_root=root,
        )
        launcher = observation["launcher"]
        native = observation["native"]
        catalog = observation["catalog"]
        effective_config = observation["effective_config"]
        isolation = observation["isolation"]
        command = build_opencode_command(args.model, args.variant, root, message, executable=native["native_executable"])
        evidence_path = Path(args.evidence_file) if args.evidence_file else None
        _record(evidence_path, {
            "event": "transport_preflight",
            "route_mode": args.route_mode,
            "route_binding": route_binding,
            "launcher": launcher,
            "native_executable": native,
            "catalog": catalog,
            "effective_config": effective_config,
            "command": command,
            "request_sha256": request_sha256,
            "request_byte_count": request_byte_count,
            "message_byte_count": len(message.encode("utf-8")),
            "request_within_public_evidence_budget": request_byte_count <= MAX_PUBLIC_EVIDENCE_BYTES,
            "command_line_character_count": len(subprocess.list2cmdline(command)),
            "command_line_within_native_bound": len(subprocess.list2cmdline(command)) <= MAX_NATIVE_COMMAND_LINE_CHARS,
            "file_argument_absent": "--file" not in command,
            "isolation": {
                "config_path": str(isolation["config_path"]),
                "config_sha256": hashlib.sha256(isolation["config_path"].read_bytes()).hexdigest(),
                "agents_path": str(isolation["agents_path"]),
                "agents_sha256": isolation["agents_sha256"],
                "agents_present_during_preflight": isolation["agents_path"].is_file(),
                "auth_state_copied": True,
                "auth_sha256": isolation["auth_sha256"],
                "mcp_disabled": True,
                "plugins_disabled": True,
                "instructions_cleared": True,
                "project_config_disabled": True,
            },
        })
        completed = subprocess.run(
            command, cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=300, check=False,
            env=isolation["environment"],
        )
        raw_stdout = completed.stdout
        raw_stderr = completed.stderr
        diagnostics, events, text_parts, structured_errors, telemetry = _provider_diagnostics(
            raw_stdout, raw_stderr, completed.returncode,
        )
        _record(Path(args.evidence_file) if args.evidence_file else None, diagnostics)
        if completed.returncode != 0:
            _record(Path(args.evidence_file) if args.evidence_file else None, {
                "event": "provider_exit_failure", "model": args.model, "variant": args.variant,
                "command": command, "request_sha256": request_sha256, "request_byte_count": request_byte_count,
                "provider_exit_code": completed.returncode,
                "provider_stdout": raw_stdout, "provider_stderr": raw_stderr,
                "error": "OpenCode exited nonzero; directive parsing was not attempted",
            })
            print(f"OpenCode transport failed: provider exited with code {completed.returncode}", file=sys.stderr)
            return 1
        if structured_errors or (events and not text_parts):
            classification = _parse_failure_classification(raw_stdout, events, text_parts, structured_errors)
            _record(Path(args.evidence_file) if args.evidence_file else None, {
                "event": "directive_extraction_failure",
                "failure_classification": classification,
                "error": "provider output did not contain an extractable assistant directive text",
            })
            raise JsonExtractionError(classification, "provider output did not contain an extractable assistant directive text")
        text = "\n".join(text_parts) if text_parts else raw_stdout
        try:
            directive = _extract_directive(text, request)
        except ValueError as exc:
            raw_classification = getattr(exc, "classification", None)
            if raw_classification == "ambiguous_json_output":
                classification = "ambiguous_json_output"
            elif raw_classification == "no_json_object":
                if not (raw_stdout or "").strip():
                    classification = "empty_output"
                elif _request_directive_schema(request):
                    classification = "no_json_object"
                else:
                    classification = _parse_failure_classification(raw_stdout, events, text_parts, structured_errors)
            else:
                classification = raw_classification or _parse_failure_classification(raw_stdout, events, text_parts, structured_errors)
            _record(Path(args.evidence_file) if args.evidence_file else None, {
                "event": "directive_extraction_failure",
                "failure_classification": classification,
                "error": f"{type(exc).__name__}: {exc}",
            })
            if _request_directive_schema(request):
                # Protocol-1.3 path: a rejected directive returns a bounded
                # directive rejection with one compact machine-generated
                # correction message carrying the precise bounded validation
                # reason; the existing bounded directive-feedback cycle
                # carries it to the model on the next attempt.  The
                # provider-completed response keeps usage/cost truthful.
                rejection_reason = getattr(exc, "reason", None)
                response: dict[str, Any] = {
                    "directive_error": {
                        "classification": classification,
                        "message": _correction_message(classification, request, reason=rejection_reason),
                    }
                }
                usage = _usage(events)
                telemetry = _provider_telemetry(events)
                if usage:
                    response["usage"] = usage
                if telemetry:
                    response["provider_telemetry"] = telemetry
                _record(Path(args.evidence_file) if args.evidence_file else None, {
                    "event": "directive_rejection_response",
                    "model": args.model, "variant": args.variant, "command": command,
                    "request_sha256": request_sha256, "request_byte_count": request_byte_count,
                    "classification": classification,
                    "validation_reason": rejection_reason,
                    "correction_message": response["directive_error"]["message"],
                    "response": response,
                    "usage": usage,
                    "provider_telemetry": telemetry,
                })
                print(json.dumps(response, ensure_ascii=False), flush=True)
                return 0
            raise
        response: dict[str, Any] = {"directive": directive}
        usage = _usage(events)
        telemetry = _provider_telemetry(events)
        if usage:
            response["usage"] = usage
        if telemetry:
            response["provider_telemetry"] = telemetry
        _record(Path(args.evidence_file) if args.evidence_file else None, {
            "model": args.model, "variant": args.variant, "command": command,
            "request_sha256": request_sha256, "request_byte_count": request_byte_count,
            "provider_exit_code": completed.returncode, "provider_stdout": raw_stdout,
            "provider_stderr": raw_stderr, "response": response, "usage": usage,
            "provider_telemetry": telemetry,
        })
        print(json.dumps(response, ensure_ascii=False), flush=True)
        return 0
    except subprocess.TimeoutExpired as exc:
        _record(Path(args.evidence_file) if args.evidence_file else None, {
            "event": "provider_timeout", "model": args.model, "variant": args.variant,
            "command": command, "request_sha256": request_sha256, "request_byte_count": request_byte_count,
            "provider_stdout": exc.stdout or exc.output or "",
            "provider_stderr": exc.stderr or "", "error": f"TimeoutExpired: {exc}",
        })
        print(f"OpenCode transport failed: TimeoutExpired: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        record: dict[str, Any] = {"model": args.model, "variant": args.variant, "command": command}
        if request_sha256 is not None:
            record["request_sha256"] = request_sha256
            record["request_byte_count"] = request_byte_count
        record.update(_failure_evidence(exc))
        _record(Path(args.evidence_file) if args.evidence_file else None, record)
        print(f"OpenCode transport failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
