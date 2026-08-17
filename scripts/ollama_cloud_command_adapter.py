"""Bounded Ollama Cloud decision-model adapter for Local Application V1.

The adapter is deliberately provider-specific.  It accepts one Local
Application protocol-1.3 request on stdin, sends one request to the signed-in
local Ollama daemon, and emits only one validated directive envelope on
stdout.  Local Application remains the executor, controller, and verifier.

This module never reads or accepts Ollama credentials.  The daemon is a
persistent external service; killing this adapter closes the client request,
but does not claim to cancel work already accepted by Ollama Cloud.
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any, TextIO
from urllib.parse import urlsplit


MODEL_ID = "gpt-oss:20b-cloud"
DEFAULT_MODEL_ID = MODEL_ID
ALLOWED_MODEL_IDENTIFIERS = frozenset({MODEL_ID})
EXPECTED_CLOUD_REMOTE_MODEL = "gpt-oss:20b"
EXPECTED_CLOUD_REMOTE_HOST = "https://ollama.com"
DEFAULT_ENDPOINT = "http://127.0.0.1:11434/api"
EXPECTED_OLLAMA_VERSION = "0.32.14"
PROTOCOL_NAME = "agentic-debugger-live-jsonl"
PROTOCOL_VERSION = "1.3"

DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_PUBLIC_REQUEST_BYTES = 25_000
MAX_RAW_RESPONSE_BYTES = 64 * 1024
MAX_STDIN_REQUEST_BYTES = 128 * 1024
DEFAULT_MAX_LOGICAL_MODEL_CALLS = 25
MAX_DIRECTIVE_ARGUMENT_BYTES = 32_768
MAX_DIRECTIVE_REASON_BYTES = 2_048
MAX_DIRECTIVE_STATEMENT_BYTES = 4_096
MAX_DIRECTIVE_HYPOTHESIS_ID_BYTES = 128
MAX_DIRECTIVE_EVIDENCE_REF_BYTES = 256
MAX_DIRECTIVE_EVIDENCE_REF_COUNT = 64

ADAPTER_RETRY_COUNT = 0
FALLBACK_COUNT = 0

PREFLIGHT_SCHEMA = "ollama-cloud-preflight-v1"
PUBLIC_REQUEST_START = "=== BEGIN PUBLIC REQUEST ==="
PUBLIC_REQUEST_END = "=== END PUBLIC REQUEST ==="

DIRECTIVE_TOP_LEVEL_FIELDS = {
    "action": frozenset({"kind", "name", "arguments"}),
    "transition": frozenset({"kind", "target_state", "reason"}),
    "add_hypothesis": frozenset(
        {
            "kind",
            "hypothesis_id",
            "statement",
            "confidence",
            "evidence_refs",
            "requires_runtime_evidence",
        }
    ),
    "revise_hypothesis": frozenset(
        {
            "kind",
            "hypothesis_id",
            "statement",
            "confidence",
            "evidence_refs",
            "requires_runtime_evidence",
        }
    ),
    "set_hypothesis_status": frozenset(
        {"kind", "hypothesis_id", "status"}
    ),
}


class OllamaAdapterError(RuntimeError):
    """Safe, bounded adapter failure with no provider response content."""

    def __init__(self, message: str, *, kind: str = "adapter_error") -> None:
        super().__init__(message)
        self.kind = kind


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError):
        raise OllamaAdapterError("value is not bounded strict JSON") from None


def _bounded_utf8(value: Any, maximum: int, label: str) -> str:
    if type(value) is not str or not value:
        raise OllamaAdapterError(f"{label} is invalid", kind="invalid_request")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise OllamaAdapterError(f"{label} is invalid", kind="invalid_request") from None
    if len(encoded) > maximum:
        raise OllamaAdapterError(f"{label} exceeds its bound", kind="invalid_request")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise OllamaAdapterError(f"{label} contains control characters", kind="invalid_request")
    return value


def canonical_public_request(request: Mapping[str, Any]) -> str:
    if not isinstance(request, Mapping):
        raise OllamaAdapterError("protocol request must be an object", kind="invalid_request")
    try:
        canonical = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        size = len(canonical.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        raise OllamaAdapterError("protocol request is not strict JSON", kind="invalid_request") from None
    if size > MAX_PUBLIC_REQUEST_BYTES:
        raise OllamaAdapterError(
            "canonical public request exceeds the Local Application ceiling",
            kind="request_too_large",
        )
    return canonical


SYSTEM_PROMPT = (
    "You are the debugging decision model for Local Application V1.\n"
    "Return exactly one legal JSON protocol directive.\n"
    "Do not output Markdown, code fences, prose, explanations, or free-form text.\n"
    "Do not directly invoke tools or functions, and do not perform filesystem, command, or repository operations.\n"
    "When the supplied allowed_actions and action_contracts permit an action, you may and should return that legal action directive.\n"
    "Local Application performs every actual action described by an accepted directive.\n"
    "Obey the supplied directive schema, allowed actions, action contracts, and legal transitions."
)


def build_protocol_message(request: Mapping[str, Any]) -> str:
    canonical = canonical_public_request(request)
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"{PUBLIC_REQUEST_START}\n{canonical}\n{PUBLIC_REQUEST_END}"
    )


def validate_endpoint(endpoint: str) -> tuple[str, int, str]:
    """Validate and split the V1 loopback endpoint.

    A non-default loopback port remains useful for task-owned synthetic HTTP
    fixtures.  Remote hosts, HTTPS, query strings, fragments, and paths other
    than ``/api`` are never accepted.
    """

    if type(endpoint) is not str or not endpoint:
        raise OllamaAdapterError("Ollama endpoint is invalid", kind="configuration")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise OllamaAdapterError(
            "Ollama endpoint must be the 127.0.0.1 HTTP API",
            kind="configuration",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise OllamaAdapterError("Ollama endpoint has unsupported URL parts", kind="configuration")
    if parsed.path.rstrip("/") != "/api":
        raise OllamaAdapterError("Ollama endpoint path must be /api", kind="configuration")
    try:
        port = parsed.port or 80
    except ValueError:
        raise OllamaAdapterError("Ollama endpoint port is invalid", kind="configuration") from None
    if not 1 <= port <= 65_535:
        raise OllamaAdapterError("Ollama endpoint port is invalid", kind="configuration")
    return parsed.hostname, port, "/api"


def _normalize_cloud_remote_host(value: Any) -> str:
    """Accept only the documented Ollama Cloud host representation."""

    if type(value) is not str or not value:
        raise OllamaAdapterError("Ollama Cloud provenance host is invalid", kind="model_mismatch")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise OllamaAdapterError("Ollama Cloud provenance host is invalid", kind="model_mismatch") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "ollama.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise OllamaAdapterError("Ollama Cloud provenance host is invalid", kind="model_mismatch")
    return EXPECTED_CLOUD_REMOTE_HOST


def _path(base_path: str, suffix: str) -> str:
    return f"{base_path.rstrip('/')}/{suffix.lstrip('/')}"


def _read_http_body(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    *,
    deadline: float,
) -> bytes:
    content_length = response.getheader("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except (TypeError, ValueError):
            raise OllamaAdapterError("Ollama response length is invalid", kind="invalid_response") from None
        if declared < 0:
            raise OllamaAdapterError("Ollama response length is invalid", kind="invalid_response")
        if declared > MAX_RAW_RESPONSE_BYTES:
            raise OllamaAdapterError(
                "Ollama response exceeded the configured bound",
                kind="response_too_large",
            )

    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OllamaAdapterError("Ollama response timed out", kind="timeout")
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        try:
            chunk = response.read(min(8192, MAX_RAW_RESPONSE_BYTES + 1 - total))
        except (socket.timeout, TimeoutError):
            raise OllamaAdapterError("Ollama response timed out", kind="timeout") from None
        except (OSError, http.client.IncompleteRead):
            raise OllamaAdapterError("Ollama response could not be read", kind="http_error") from None
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_RAW_RESPONSE_BYTES:
            raise OllamaAdapterError(
                "Ollama response exceeded the configured bound",
                kind="response_too_large",
            )
    return b"".join(chunks)


def _http_json_request(
    endpoint: str,
    method: str,
    suffix: str,
    *,
    body: Mapping[str, Any] | None = None,
    timeout_seconds: float,
) -> Mapping[str, Any]:
    host, port, base_path = validate_endpoint(endpoint)
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 300:
        raise OllamaAdapterError("Ollama request timeout is invalid", kind="configuration")
    request_bytes = None
    headers = {"Accept": "application/json"}
    if body is not None:
        request_bytes = (_safe_json(body) + "\n").encode("utf-8")
        if len(request_bytes) > MAX_RAW_RESPONSE_BYTES:
            raise OllamaAdapterError("Ollama request exceeded the configured bound", kind="request_too_large")
        headers["Content-Type"] = "application/json"

    deadline = time.monotonic() + float(timeout_seconds)
    connection = http.client.HTTPConnection(host, port, timeout=float(timeout_seconds))
    try:
        try:
            connection.request(method, _path(base_path, suffix), body=request_bytes, headers=headers)
            response = connection.getresponse()
        except (socket.timeout, TimeoutError):
            raise OllamaAdapterError("Ollama request timed out", kind="timeout") from None
        except (OSError, http.client.HTTPException):
            raise OllamaAdapterError("Ollama HTTP request failed", kind="http_error") from None
        if response.status < 200 or response.status >= 300:
            raise OllamaAdapterError("Ollama HTTP request returned an error", kind="http_error")
        raw_body = _read_http_body(response, connection, deadline=deadline)
    finally:
        connection.close()

    try:
        value = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OllamaAdapterError("Ollama response was not valid JSON", kind="invalid_response") from None
    if not isinstance(value, Mapping):
        raise OllamaAdapterError("Ollama response was not an object", kind="invalid_response")
    return value


def validate_logical_call_index(request: Mapping[str, Any], maximum: int) -> None:
    if type(maximum) is not int or isinstance(maximum, bool) or not 1 <= maximum <= DEFAULT_MAX_LOGICAL_MODEL_CALLS:
        raise OllamaAdapterError("logical-call bound is invalid", kind="configuration")
    protocol = request.get("protocol")
    if not isinstance(protocol, Mapping):
        raise OllamaAdapterError("request carries no protocol metadata", kind="invalid_request")
    if protocol.get("name") != PROTOCOL_NAME or protocol.get("version") != PROTOCOL_VERSION:
        raise OllamaAdapterError("request protocol is not 1.3", kind="invalid_request")
    index = protocol.get("logical_model_call_index")
    # The live Local Application controller uses zero-based model-call
    # indices: the first request is 0 and the 25-call envelope is 0..24.
    if type(index) is not int or isinstance(index, bool) or not 0 <= index < maximum:
        raise OllamaAdapterError("logical model call is outside the 25-call bound", kind="logical_call_limit")


def _validate_text(value: Any, maximum: int, label: str) -> None:
    _bounded_utf8(value, maximum, label)
    if value != value.strip():
        raise OllamaAdapterError(f"{label} has surrounding whitespace", kind="invalid_directive")


def _validate_action_arguments(name: str, arguments: Any, contracts: Mapping[str, Any]) -> None:
    if not isinstance(arguments, Mapping):
        raise OllamaAdapterError("directive arguments are invalid", kind="invalid_directive")
    contract = contracts.get(name)
    if not isinstance(contract, Mapping):
        raise OllamaAdapterError("directive action is not contracted", kind="invalid_directive")
    properties = contract.get("properties")
    if not isinstance(properties, Mapping):
        properties = contract
    required = contract.get("required")
    if isinstance(required, list) and any(field not in arguments for field in required):
        raise OllamaAdapterError("directive action is missing an argument", kind="invalid_directive")
    if contract.get("additional_properties") is False and set(arguments) - set(properties):
        raise OllamaAdapterError("directive action contains an unknown argument", kind="invalid_directive")
    for field, spec in properties.items():
        if field not in arguments or not isinstance(spec, Mapping):
            continue
        value = arguments[field]
        type_name = spec.get("type")
        valid = {
            "string": type(value) is str,
            "integer": type(value) is int and not isinstance(value, bool),
            "number": type(value) in (int, float) and not isinstance(value, bool),
            "boolean": type(value) is bool,
            "array": type(value) is list,
            "object": type(value) is dict,
            "null": value is None,
        }.get(type_name, True)
        if not valid:
            raise OllamaAdapterError("directive argument has the wrong type", kind="invalid_directive")
        enum = spec.get("enum")
        if isinstance(enum, list) and value not in enum:
            raise OllamaAdapterError("directive argument is outside its enum", kind="invalid_directive")
        if type_name == "string" and isinstance(spec.get("min_length"), int) and len(value) < spec["min_length"]:
            raise OllamaAdapterError("directive argument is too short", kind="invalid_directive")
    try:
        serialized = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeError):
        raise OllamaAdapterError("directive arguments are not finite JSON", kind="invalid_directive") from None
    if len(serialized.encode("utf-8")) > MAX_DIRECTIVE_ARGUMENT_BYTES:
        raise OllamaAdapterError("directive arguments exceed the configured bound", kind="invalid_directive")


def validate_directive_candidate(candidate: Any, request: Mapping[str, Any]) -> None:
    """Apply the request-bound Local Application directive contract.

    ``LiveModelAdapter`` repeats this validation downstream with its typed
    snapshot.  This first gate prevents an invalid provider completion from
    becoming a successful adapter response.
    """

    if not isinstance(candidate, Mapping):
        raise OllamaAdapterError("directive must be a JSON object", kind="invalid_directive")
    kinds = request.get("directive_schema")
    if isinstance(kinds, Mapping):
        kinds = list(kinds)
    if not isinstance(kinds, list) or not kinds:
        raise OllamaAdapterError("request carries no directive schema", kind="invalid_request")
    kind = candidate.get("kind")
    if type(kind) is not str or kind not in kinds or kind not in DIRECTIVE_TOP_LEVEL_FIELDS:
        raise OllamaAdapterError("directive kind is not allowed", kind="invalid_directive")
    if set(candidate) - DIRECTIVE_TOP_LEVEL_FIELDS[kind]:
        raise OllamaAdapterError("directive contains unknown fields", kind="invalid_directive")
    controller = request.get("controller")
    if not isinstance(controller, Mapping):
        raise OllamaAdapterError("request carries no controller contract", kind="invalid_request")

    if kind == "action":
        name = candidate.get("name")
        allowed = controller.get("allowed_actions")
        if type(name) is not str or not isinstance(allowed, list) or name not in allowed:
            raise OllamaAdapterError("directive action is not allowed", kind="invalid_directive")
        contracts = request.get("action_contracts")
        if not isinstance(contracts, Mapping):
            raise OllamaAdapterError("request carries no action contracts", kind="invalid_request")
        _validate_action_arguments(name, candidate.get("arguments"), contracts)
        return
    if kind == "transition":
        targets = controller.get("legal_transition_targets")
        if not isinstance(targets, list) or candidate.get("target_state") not in targets:
            raise OllamaAdapterError("directive transition is not legal", kind="invalid_directive")
        _validate_text(candidate.get("reason"), MAX_DIRECTIVE_REASON_BYTES, "directive reason")
        return
    hypothesis_id = candidate.get("hypothesis_id")
    _validate_text(hypothesis_id, MAX_DIRECTIVE_HYPOTHESIS_ID_BYTES, "hypothesis id")
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for char in hypothesis_id):
        raise OllamaAdapterError("hypothesis id is invalid", kind="invalid_directive")
    if kind in {"add_hypothesis", "revise_hypothesis"}:
        _validate_text(candidate.get("statement"), MAX_DIRECTIVE_STATEMENT_BYTES, "hypothesis statement")
        if candidate.get("confidence") not in {"low", "medium", "high"}:
            raise OllamaAdapterError("hypothesis confidence is invalid", kind="invalid_directive")
        references = candidate.get("evidence_refs")
        if type(references) is not list or len(references) > MAX_DIRECTIVE_EVIDENCE_REF_COUNT:
            raise OllamaAdapterError("hypothesis evidence references are invalid", kind="invalid_directive")
        seen: set[str] = set()
        for reference in references:
            _validate_text(reference, MAX_DIRECTIVE_EVIDENCE_REF_BYTES, "evidence reference")
            if reference in seen:
                raise OllamaAdapterError("hypothesis evidence references are duplicated", kind="invalid_directive")
            seen.add(reference)
        if type(candidate.get("requires_runtime_evidence")) is not bool:
            raise OllamaAdapterError("hypothesis runtime-evidence flag is invalid", kind="invalid_directive")
        return
    if candidate.get("status") not in {"supported", "rejected", "discarded"}:
        raise OllamaAdapterError("hypothesis status is invalid", kind="invalid_directive")


def _extract_final_content(response: Mapping[str, Any]) -> str:
    model = response.get("model")
    if model not in {MODEL_ID, EXPECTED_CLOUD_REMOTE_MODEL}:
        raise OllamaAdapterError("Ollama returned an unexpected model", kind="model_mismatch")
    if response.get("remote_model") != EXPECTED_CLOUD_REMOTE_MODEL:
        raise OllamaAdapterError("Ollama returned unexpected remote model provenance", kind="model_mismatch")
    _normalize_cloud_remote_host(response.get("remote_host"))
    if type(response.get("done")) is not bool or response.get("done") is not True:
        raise OllamaAdapterError("Ollama response is incomplete", kind="invalid_completion")
    if type(response.get("done_reason")) is not str or not response["done_reason"]:
        raise OllamaAdapterError("Ollama completion metadata is invalid", kind="invalid_completion")
    message = response.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        raise OllamaAdapterError("Ollama assistant message is invalid", kind="invalid_response")
    if "tool_calls" in message:
        raise OllamaAdapterError("Ollama tool-call activity is not permitted", kind="tool_call_rejected")
    if "thinking" in message and message["thinking"] is not None and type(message["thinking"]) is not str:
        raise OllamaAdapterError("Ollama thinking field is invalid", kind="invalid_response")
    content = message.get("content")
    if type(content) is not str or not content.strip():
        raise OllamaAdapterError("Ollama assistant content is missing", kind="invalid_response")
    return content


def parse_directive_content(content: str, request: Mapping[str, Any]) -> dict[str, Any]:
    try:
        candidate = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        raise OllamaAdapterError("Ollama content was not exactly one JSON document", kind="invalid_directive") from None
    if not isinstance(candidate, dict):
        raise OllamaAdapterError("Ollama content was not a JSON object", kind="invalid_directive")
    validate_directive_candidate(candidate, request)
    return dict(candidate)


def _chat_request(
    endpoint: str,
    request: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> Mapping[str, Any]:
    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": build_protocol_message(request)}],
        "stream": False,
        "think": "low",
    }
    return _http_json_request(
        endpoint,
        "POST",
        "/chat",
        body=payload,
        timeout_seconds=timeout_seconds,
    )


def _preflight_model_entry(tags: Mapping[str, Any]) -> Mapping[str, Any]:
    models = tags.get("models")
    if not isinstance(models, list):
        raise OllamaAdapterError("Ollama tags response is invalid", kind="preflight_failed")
    for entry in models:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("name") == MODEL_ID and entry.get("model") == MODEL_ID:
            if entry.get("remote_model") != EXPECTED_CLOUD_REMOTE_MODEL:
                raise OllamaAdapterError("configured Ollama model has unexpected remote model", kind="preflight_failed")
            _normalize_cloud_remote_host(entry.get("remote_host"))
            return entry
    raise OllamaAdapterError("configured Ollama model is unavailable", kind="preflight_failed")


def run_preflight(
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    expected_version: str = EXPECTED_OLLAMA_VERSION,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Read only Ollama readiness and model metadata; never calls generation."""

    host, port, _ = validate_endpoint(endpoint)
    version_response = _http_json_request(endpoint, "GET", "/version", timeout_seconds=timeout_seconds)
    version = version_response.get("version")
    if type(version) is not str or version != expected_version:
        raise OllamaAdapterError("Ollama version is not the expected version", kind="preflight_failed")
    tags_response = _http_json_request(endpoint, "GET", "/tags", timeout_seconds=timeout_seconds)
    tag = _preflight_model_entry(tags_response)
    show_response = _http_json_request(
        endpoint,
        "POST",
        "/show",
        body={"model": MODEL_ID},
        timeout_seconds=timeout_seconds,
    )
    details = show_response.get("details")
    if not isinstance(details, Mapping) or not isinstance(show_response.get("model_info"), Mapping):
        raise OllamaAdapterError("Ollama model metadata is incomplete", kind="preflight_failed")
    if details.get("parent_model") != EXPECTED_CLOUD_REMOTE_MODEL:
        raise OllamaAdapterError("configured Ollama model has unexpected parent model", kind="preflight_failed")
    capabilities = show_response.get("capabilities")
    if not isinstance(capabilities, list) or any(type(item) is not str for item in capabilities):
        raise OllamaAdapterError("Ollama model capabilities are invalid", kind="preflight_failed")
    return {
        "schema_version": PREFLIGHT_SCHEMA,
        "ok": True,
        "endpoint": f"http://{host}:{port}/api",
        "local_daemon_api_ready": True,
        "ollama_version": version,
        "expected_model": MODEL_ID,
        "expected_remote_model": EXPECTED_CLOUD_REMOTE_MODEL,
        "expected_remote_host": EXPECTED_CLOUD_REMOTE_HOST,
        "model_available": True,
        "model_metadata_readable": True,
        "model_remote_model": tag["remote_model"],
        "model_remote_host": _normalize_cloud_remote_host(tag["remote_host"]),
        "model_capabilities": sorted(capabilities),
        "model_tag_digest": tag.get("digest") if type(tag.get("digest")) is str else None,
        "provider_inference_started": False,
        "cloud_inference_verified": False,
    }


def _read_request(stdin_stream: TextIO) -> Mapping[str, Any]:
    line = stdin_stream.readline(MAX_STDIN_REQUEST_BYTES + 1)
    if not line:
        raise OllamaAdapterError("empty request on stdin", kind="invalid_request")
    try:
        if len(line.encode("utf-8")) > MAX_STDIN_REQUEST_BYTES:
            raise OllamaAdapterError("stdin request exceeded the configured bound", kind="request_too_large")
        value = json.loads(line)
    except OllamaAdapterError:
        raise
    except (UnicodeError, json.JSONDecodeError):
        raise OllamaAdapterError("stdin request was not valid JSON", kind="invalid_request") from None
    if not isinstance(value, Mapping):
        raise OllamaAdapterError("stdin request was not an object", kind="invalid_request")
    return value


def run_adapter(
    stdin_stream: TextIO = sys.stdin,
    stdout_stream: TextIO = sys.stdout,
    stderr_stream: TextIO = sys.stderr,
    argv: Sequence[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Ollama Cloud Local Application V1 command adapter")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-logical-model-calls", type=int, default=DEFAULT_MAX_LOGICAL_MODEL_CALLS)
    parser.add_argument("--expected-version", default=EXPECTED_OLLAMA_VERSION)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.model != MODEL_ID:
            raise OllamaAdapterError("only gpt-oss:20b-cloud is supported", kind="configuration")
        validate_endpoint(args.endpoint)
        if args.preflight:
            result = run_preflight(
                endpoint=args.endpoint,
                expected_version=args.expected_version,
                timeout_seconds=args.timeout,
            )
            stdout_stream.write(_safe_json(result) + "\n")
            stdout_stream.flush()
            return 0

        request = _read_request(stdin_stream)
        validate_logical_call_index(request, args.max_logical_model_calls)
        response = _chat_request(args.endpoint, request, timeout_seconds=args.timeout)
        content = _extract_final_content(response)
        directive = parse_directive_content(content, request)
        stdout_stream.write(_safe_json({"directive": directive}) + "\n")
        stdout_stream.flush()
        return 0
    except OllamaAdapterError as exc:
        stderr_stream.write(f"Error: {exc}\n")
        stderr_stream.flush()
        return 1
    except (BrokenPipeError, OSError):
        return 1


def main() -> None:
    raise SystemExit(run_adapter())


if __name__ == "__main__":
    main()
