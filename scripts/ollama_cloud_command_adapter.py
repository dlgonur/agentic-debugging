"""Bounded Ollama Cloud decision-model adapter for Local Application V1.

The adapter is deliberately provider-specific and model-profile-driven.
It accepts one Local Application protocol-1.3 request on stdin, selects one
accepted Cloud alias from ``--model``, sends one request to the signed-in
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
from pathlib import Path
import socket
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TextIO
from urllib.parse import urlsplit

if __package__ in (None, ""):
    repo_root = str(Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

from agentic_debugger.evaluation.request_envelope import (
    MAX_HTTP_REQUEST_BODY_BYTES,
    MAX_PUBLIC_REQUEST_BYTES,
    MAX_RAW_RESPONSE_BYTES,
    MAX_STDIN_REQUEST_BYTES,
)


@dataclass(frozen=True)
class CloudModelSpec:
    """One accepted Ollama Cloud alias and its fail-closed provenance contract.

    ``local_alias`` is the Ollama CLI / ``/api/chat`` request identity.
    ``upstream_model`` is the observed Cloud provenance: ``/api/tags``
    ``remote_model``, ``/api/show`` ``details.parent_model``, and the
    ``/api/chat`` response ``model``.  Those three fields share one
    expected value for every currently accepted alias.
    """

    local_alias: str
    upstream_model: str


CLOUD_MODELS: dict[str, CloudModelSpec] = {
    "gpt-oss:20b-cloud": CloudModelSpec(
        local_alias="gpt-oss:20b-cloud",
        upstream_model="gpt-oss:20b",
    ),
    "nemotron-3-nano:30b-cloud": CloudModelSpec(
        local_alias="nemotron-3-nano:30b-cloud",
        upstream_model="nemotron-3-nano:30b",
    ),
}

MODEL_ID = "gpt-oss:20b-cloud"
DEFAULT_MODEL_ID = MODEL_ID
ALLOWED_MODEL_IDENTIFIERS = frozenset(CLOUD_MODELS)
EXPECTED_CLOUD_REMOTE_MODEL = CLOUD_MODELS[MODEL_ID].upstream_model
EXPECTED_CLOUD_REMOTE_HOST = "https://ollama.com"
DEFAULT_ENDPOINT = "http://127.0.0.1:11434/api"
EXPECTED_OLLAMA_VERSION = "0.32.14"
PROTOCOL_NAME = "agentic-debugger-live-jsonl"
PROTOCOL_VERSION = "1.3"

DEFAULT_TIMEOUT_SECONDS = 20.0
# The request envelope is finite but must accommodate the V10+ high-reasoning
# treatment (metadata 60s, generation 1080s, and a 45s idle bound).  Keep one
# adapter-side ceiling so CLI validation cannot reject an otherwise frozen
# treatment before a provider request is attempted.
MAX_REQUEST_TIMEOUT_SECONDS = 1200.0
# V7/live profiles pass an explicit larger timeout.  The historical default is
# intentionally retained so frozen V1-V6 evidence remains reproducible.
DEFAULT_STREAM_RESPONSE_BYTES = 4 * 1024 * 1024
# Wire input and retained controller content have different semantics.  Private
# reasoning may be large, but it is discarded and must not consume the retained
# response budget.  The wire and per-item bounds remain finite and fail closed.
DEFAULT_STREAM_WIRE_BYTES = 8 * 1024 * 1024
DEFAULT_STREAM_LINE_BYTES = 4 * 1024 * 1024
DEFAULT_STREAM_RETAINED_CONTENT_BYTES = MAX_RAW_RESPONSE_BYTES
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 45.0
PROGRESS_RECENCY_SECONDS = 5.0
DEFAULT_MAX_LOGICAL_MODEL_CALLS = 25
MAX_DIRECTIVE_ARGUMENT_BYTES = 32_768
MAX_DIRECTIVE_REASON_BYTES = 2_048
MAX_DIRECTIVE_STATEMENT_BYTES = 4_096
MAX_DIRECTIVE_HYPOTHESIS_ID_BYTES = 128
MAX_DIRECTIVE_EVIDENCE_REF_BYTES = 256
MAX_DIRECTIVE_EVIDENCE_REF_COUNT = 64

ADAPTER_RETRY_COUNT = 0
FALLBACK_COUNT = 0
REASONING_EFFORTS = frozenset({"low", "medium", "high"})
DEFAULT_REASONING_EFFORT = "low"

PREFLIGHT_SCHEMA = "ollama-cloud-preflight-v1"
GENERATION_EVENT_SCHEMA = "live-command-event-v1"
GENERATION_EVENT = "provider_generation_started"
GENERATION_TELEMETRY_SCHEMA = "live-command-telemetry-v1"
GENERATION_TELEMETRY_EVENT = "provider_generation_telemetry"
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

    def __init__(
        self,
        message: str,
        *,
        kind: str = "adapter_error",
        timeout_phase: str | None = None,
        telemetry: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.timeout_phase = timeout_phase
        self.telemetry = dict(telemetry) if isinstance(telemetry, Mapping) else None


def resolve_cloud_model(model_id: Any) -> CloudModelSpec:
    """Return the accepted Cloud spec for ``model_id`` or fail closed."""

    if type(model_id) is not str or not model_id:
        raise OllamaAdapterError("requested Ollama Cloud model is invalid", kind="configuration")
    spec = CLOUD_MODELS.get(model_id)
    if spec is None:
        raise OllamaAdapterError("requested Ollama Cloud model is not supported", kind="configuration")
    return spec


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
            f"canonical public request exceeds the configured bound ({size} > {MAX_PUBLIC_REQUEST_BYTES} bytes)",
            kind="request_too_large",
        )
    return canonical


_DIRECTIVE_FIELD_ORDER: dict[str, tuple[str, ...]] = {
    "action": ("kind", "name", "arguments"),
    "transition": ("kind", "target_state", "reason"),
    "add_hypothesis": (
        "kind",
        "hypothesis_id",
        "statement",
        "confidence",
        "evidence_refs",
        "requires_runtime_evidence",
    ),
    "revise_hypothesis": (
        "kind",
        "hypothesis_id",
        "statement",
        "confidence",
        "evidence_refs",
        "requires_runtime_evidence",
    ),
    "set_hypothesis_status": ("kind", "hypothesis_id", "status"),
}


def _directive_fields_match_validator() -> bool:
    return all(
        frozenset(fields) == DIRECTIVE_TOP_LEVEL_FIELDS[kind]
        for kind, fields in _DIRECTIVE_FIELD_ORDER.items()
    )


if not _directive_fields_match_validator():
    raise RuntimeError("directive prompt field names drifted from the adapter validator")


SYSTEM_PROMPT = (
    "You are the debugging decision model for Local Application V1.\n"
    "Return exactly one legal JSON protocol directive.\n"
    "Output exactly one JSON object. Do not output Markdown, code fences, prose, explanations, or free-form text before or after it.\n"
    "Use only the exact protocol field names. Never invent semantic aliases.\n"
    "Never combine an action and a transition into one object. Choose one legal directive only.\n"
    "Do not directly invoke tools or functions, and do not perform filesystem, command, or repository operations.\n"
    "When the supplied allowed_actions and action_contracts permit an action, you may and should return that legal action directive.\n"
    "Local Application performs every actual action described by an accepted directive.\n"
    "The top-level field identifying directive type is always \"kind\".\n"
    "Do not use top-level keys named action, payload, or transition.\n"
    "\n"
    "Exact legal top-level forms. The first key is always \"kind\".\n"
    "Action: {\"kind\":\"action\",\"name\":\"<allowed action>\",\"arguments\":{...}}\n"
    "kind must literally be \"action\". name must come from controller.allowed_actions. arguments must satisfy that action's supplied action_contracts. Do not use top-level keys named action or payload.\n"
    "Transition: {\"kind\":\"transition\",\"target_state\":\"<legal target>\",\"reason\":\"<bounded reason>\"}\n"
    "kind must literally be \"transition\". target_state must come from controller.legal_transition_targets. Do not use a top-level key named transition.\n"
    "add_hypothesis: {\"kind\":\"add_hypothesis\",\"hypothesis_id\":\"<id>\",\"statement\":\"<statement>\",\"confidence\":\"low|medium|high\",\"evidence_refs\":[],\"requires_runtime_evidence\":false}\n"
    "revise_hypothesis: {\"kind\":\"revise_hypothesis\",\"hypothesis_id\":\"<id>\",\"statement\":\"<statement>\",\"confidence\":\"low|medium|high\",\"evidence_refs\":[],\"requires_runtime_evidence\":false}\n"
    "set_hypothesis_status: {\"kind\":\"set_hypothesis_status\",\"hypothesis_id\":\"<id>\",\"status\":\"supported|rejected|discarded\"}\n"
    "confidence must be exactly one of low, medium, high. status must be exactly one of supported, rejected, discarded. evidence_refs is a list of strings. requires_runtime_evidence is a boolean."
)


def _directive_kinds(request: Mapping[str, Any]) -> list[str]:
    kinds = request.get("directive_schema")
    if isinstance(kinds, Mapping):
        kinds = list(kinds)
    if not isinstance(kinds, list):
        return []
    return [kind for kind in kinds if type(kind) is str]


def _illustrative_argument_value(field: str, spec: Mapping[str, Any] | None) -> Any:
    if isinstance(spec, Mapping):
        enum = spec.get("enum")
        if isinstance(enum, list) and enum and type(enum[0]) in (str, int, float, bool):
            return enum[0]
        type_name = spec.get("type")
        if type_name == "boolean":
            return True
        if type_name == "integer":
            return 0
        if type_name == "number":
            return 0
        if type_name == "array":
            return []
        if type_name == "object":
            return {}
        if type_name == "null":
            return None
    return f"<{field}>"


NEUTRAL_UNIFIED_DIFF_EXAMPLE = (
    "--- a/example.py\n"
    "+++ b/example.py\n"
    "@@ -1,3 +1,4 @@\n"
    " keep = True\n"
    "-old_a = 1\n"
    "-old_b = 2\n"
    "+new_a = 1\n"
    "+new_b = 2\n"
    "+new_c = 3\n"
)

OLD_COUNT_FORMULA = (
    'OLD_COUNT = number of lines beginning with " " + number of lines beginning with "-"'
)
NEW_COUNT_FORMULA = (
    'NEW_COUNT = number of lines beginning with " " + number of lines beginning with "+"'
)

APPLY_PATCH_DIRECTIVE_SHAPE = (
    '{"kind":"action","name":"apply_patch","arguments":{"patch":"..."}}'
)


def _syntax_check_advertises_path(contracts: Any) -> bool:
    if not isinstance(contracts, Mapping):
        return False
    contract = contracts.get("syntax_check")
    if not isinstance(contract, Mapping):
        return False
    properties = contract.get("properties")
    if not isinstance(properties, Mapping):
        properties = contract
    return "path" in properties or "paths" in properties


def _patch_budget_remaining(controller: Mapping[str, Any]) -> str | None:
    limits = controller.get("budget_limits")
    state = controller.get("budget_state")
    if not isinstance(limits, Mapping) or not isinstance(state, Mapping):
        return None
    maximum = limits.get("max_patch_attempts")
    used = state.get("patch_attempts")
    if (
        type(maximum) is not int
        or isinstance(maximum, bool)
        or type(used) is not int
        or isinstance(used, bool)
        or maximum <= 0
        or used < 0
    ):
        return None
    remaining = maximum - used
    if remaining > 0:
        return f"Patch-attempt budget remaining for this request: {remaining} of {maximum}."
    return "Patch-attempt budget for this request is exhausted."


def build_apply_patch_guidance(request: Mapping[str, Any]) -> str:
    """PatchManager-derived apply_patch format and recovery rules."""

    controller = request.get("controller")
    if not isinstance(controller, Mapping):
        controller = {}
    contracts = request.get("action_contracts")
    lines = [
        "apply_patch arguments.patch must be a complete unified diff accepted by Local Application's PatchManager.",
        "File headers must contain both lines, in this order:",
        "--- a/<relative-path>",
        "+++ b/<same-relative-path>",
        "Every hunk requires a complete numeric header of the form:",
        "@@ -OLD_START,OLD_COUNT +NEW_START,NEW_COUNT @@",
        "OLD_START and NEW_START are 1-based line positions.",
        "Always emit the complete form. Never emit bare @@.",
        "Never leave symbolic placeholders such as OLD_COUNT in the actual patch.",
        "After composing each hunk body, count prefixes mechanically before returning the JSON directive:",
        OLD_COUNT_FORMULA,
        NEW_COUNT_FORMULA,
        'Lines beginning with "-" do not count toward NEW_COUNT.',
        'Lines beginning with "+" do not count toward OLD_COUNT.',
        'Context lines beginning with exactly one space count toward both OLD_COUNT and NEW_COUNT.',
        "Hunk counts must exactly match the hunk body.",
        "If the header counts do not equal the body counts, correct the header before output.",
        "Prefer the smallest valid hunk that uniquely expresses the edit. Zero-context hunks, with no lines beginning with a single space, are accepted when the removed and added lines uniquely locate the edit.",
        "Hunk body prefixes are significant: one leading space for unchanged/context, - for removed, + for added.",
        "Use repository-relative paths only.",
        "Do not wrap the patch string in Markdown fences.",
        "Do not include unsupported Git metadata such as diff --git, new file, deleted file, rename, or copy lines.",
        f"The entire patch remains the value of {APPLY_PATCH_DIRECTIVE_SHAPE}",
        "Neutral arithmetic example. For this body, OLD_COUNT = 1 context + 2 removed = 3 and NEW_COUNT = 1 context + 3 added = 4:",
        NEUTRAL_UNIFIED_DIFF_EXAMPLE.rstrip("\n"),
        "Before emitting the JSON, verify:",
        "1. --- and +++ headers both exist and refer to the same repository-relative path.",
        "2. Every hunk header contains four numeric values.",
        "3. Count every hunk body line by prefix.",
        "4. Recompute OLD_COUNT from context + removed.",
        "5. Recompute NEW_COUNT from context + added.",
        "6. Header counts exactly equal those totals.",
        '7. Every hunk body line starts with " ", "-", or "+".',
        "8. No Markdown fences or unsupported Git metadata.",
        f"9. The complete patch is inside {APPLY_PATCH_DIRECTIVE_SHAPE}",
        "A rejected apply_patch does not create an active patch and does not mutate the workspace.",
        "After a rejected patch, do not call revert_patch merely to undo that rejected patch.",
    ]
    if _syntax_check_advertises_path(contracts):
        lines.append(
            "Do not call patch-dependent syntax_check without an active successfully applied patch unless using the advertised path argument."
        )
    else:
        lines.append(
            "Do not call patch-dependent syntax_check without an active successfully applied patch."
        )
    lines.append(
        "If apply_patch remains legal and patch-attempt budget remains, correct the patch format or content and submit a new valid apply_patch."
    )
    lines.append(
        "After a patch is successfully applied, use the legal validation lifecycle exposed by the current controller and tools, including revert_patch only for an active successfully applied patch."
    )
    budget = _patch_budget_remaining(controller)
    if budget is not None:
        lines.append(budget)
    return "\n".join(lines)


def illustrative_action_directive(name: str, contracts: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    if isinstance(contracts, Mapping):
        contract = contracts.get(name)
        if isinstance(contract, Mapping):
            properties = contract.get("properties")
            if not isinstance(properties, Mapping):
                properties = contract
            required = contract.get("required")
            if isinstance(required, list):
                for field in required:
                    if type(field) is not str:
                        continue
                    spec = properties.get(field) if isinstance(properties, Mapping) else None
                    arguments[field] = _illustrative_argument_value(
                        field,
                        spec if isinstance(spec, Mapping) else None,
                    )
    return {"kind": "action", "name": name, "arguments": arguments}


def build_request_guidance(request: Mapping[str, Any]) -> str:
    """Request-specific legal shapes derived from the current protocol request."""

    kinds = set(_directive_kinds(request))
    controller = request.get("controller")
    if not isinstance(controller, Mapping):
        controller = {}
    lines = [
        "Current request legal decision surface:",
        "Return exactly one JSON object using only the exact protocol field names.",
        "Do not invent keys named action, payload, or transition.",
        "Do not combine an action and a transition.",
    ]
    task = request.get("task")
    source = task.get("source") if isinstance(task, Mapping) else None
    external = isinstance(source, Mapping) and source.get("kind") == "external"
    state = controller.get("state")
    if external and state == "Understand":
        lines.extend([
            "Understand-state procedure: hypotheses from the issue statement are provisional.",
            "Use bounded public repository tools to locate the relevant implementation and inspect actual source context before patching.",
            "Do not guess repository paths or source bodies. Do not enter Patch until the target source region has been observed.",
            "If a search is insufficient, continue public source inspection.",
        ])
    elif external and state == "Patch":
        lines.extend([
            "Patch-state procedure: build the patch from the exact observed repository-relative path and source context.",
            "Never fabricate source context or submit a placeholder/dummy patch merely to satisfy the lifecycle.",
            "Use the strict unified-diff rules below; bare @@ and invented hunk start locations remain invalid.",
            "After a meaningful candidate succeeds, normally follow the legal Validate lifecycle. If context is insufficient, return to Understand.",
        ])
    allowed = controller.get("allowed_actions")
    contracts = request.get("action_contracts")
    if "action" in kinds and isinstance(allowed, list):
        for name in allowed:
            if type(name) is not str or not name:
                continue
            example = illustrative_action_directive(name, contracts)
            lines.append(
                "Legal action representation: "
                + json.dumps(example, ensure_ascii=False, separators=(",", ":"))
            )
            if name == "apply_patch":
                lines.append(build_apply_patch_guidance(request))
    targets = controller.get("legal_transition_targets")
    if "transition" in kinds and isinstance(targets, list) and targets:
        legal = ", ".join(target for target in targets if type(target) is str)
        if legal:
            lines.append(
                "Legal transition representation: "
                '{"kind":"transition","target_state":"<one of '
                + legal
                + '>","reason":"<bounded reason>"}'
            )
    return "\n".join(lines)


def build_chat_messages(request: Mapping[str, Any]) -> list[dict[str, str]]:
    canonical = canonical_public_request(request)
    user_content = (
        f"{build_request_guidance(request)}\n\n"
        f"{PUBLIC_REQUEST_START}\n{canonical}\n{PUBLIC_REQUEST_END}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def build_protocol_message(request: Mapping[str, Any]) -> str:
    """Compatibility wrapper: user-message body only. Prefer build_chat_messages."""

    messages = build_chat_messages(request)
    return messages[1]["content"]


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


def _read_stream_response(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    *,
    deadline: float,
    idle_timeout_seconds: float = DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    telemetry_sink: dict[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Read Ollama chat NDJSON without persisting the thinking trace.

    The REST stream contains partial ``message.thinking`` and
    ``message.content`` chunks followed by a ``done`` envelope.  Thinking is
    deliberately discarded as it is not part of the controller protocol;
    content is accumulated under a hard byte bound and the final envelope is
    returned in the same shape as the non-streaming API.
    """

    started = time.monotonic()
    last_progress = started
    total = 0
    retained_bytes = 0
    discarded_thinking_bytes = 0
    discarded_thinking_chunks = 0
    chunks_observed = 0
    content_parts: list[str] = []
    final: dict[str, Any] | None = None
    done = False

    def snapshot(*, timeout_phase: str | None = None, completed: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        telemetry = {
            "first_response_chunk_latency_seconds": (
                first_chunk_at - started if first_chunk_at is not None else None
            ),
            "last_chunk_elapsed_seconds": (
                last_progress - started if chunks_observed else None
            ),
            "completion_elapsed_seconds": now - started if completed else None,
            "timeout_phase": timeout_phase,
            "progress_occurred": chunks_observed > 0,
            "progress_before_timeout": (
                timeout_phase is not None
                and chunks_observed > 0
                and now - last_progress <= PROGRESS_RECENCY_SECONDS
            ),
            "wire_bytes_observed": total,
            "retained_content_bytes": retained_bytes,
            "discarded_thinking_bytes": discarded_thinking_bytes,
            "discarded_thinking_chunks": discarded_thinking_chunks,
            "stream_chunks_observed": chunks_observed,
        }
        if telemetry_sink is not None:
            telemetry_sink.clear()
            telemetry_sink.update(telemetry)
        return telemetry

    def fail(message: str, *, kind: str, timeout_phase: str | None = None) -> None:
        telemetry = snapshot(timeout_phase=timeout_phase)
        raise OllamaAdapterError(
            message,
            kind=kind,
            timeout_phase=timeout_phase,
            telemetry=telemetry,
        )

    first_chunk_at: float | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail("Ollama response timed out", kind="timeout", timeout_phase="generation")
        idle_remaining = idle_timeout_seconds - (time.monotonic() - last_progress)
        if idle_remaining <= 0:
            fail("Ollama response stream went idle", kind="timeout", timeout_phase="stream_idle")
        if connection.sock is not None:
            connection.sock.settimeout(min(remaining, idle_remaining))
        try:
            line = response.readline(DEFAULT_STREAM_LINE_BYTES + 1)
        except (socket.timeout, TimeoutError):
            now = time.monotonic()
            if idle_timeout_seconds - (now - last_progress) <= 0:
                fail("Ollama response stream went idle", kind="timeout", timeout_phase="stream_idle")
            fail("Ollama response timed out", kind="timeout", timeout_phase="generation")
        except (OSError, http.client.IncompleteRead):
            fail("Ollama response could not be read", kind="http_error")
        if time.monotonic() - last_progress > idle_timeout_seconds:
            fail("Ollama response stream went idle", kind="timeout", timeout_phase="stream_idle")
        if not line:
            break
        if len(line) > DEFAULT_STREAM_LINE_BYTES or (
            len(line) == DEFAULT_STREAM_LINE_BYTES and not line.endswith(b"\n")
        ):
            fail(
                "Ollama streamed response item exceeded the configured bound",
                kind="response_too_large",
            )
        total += len(line)
        if total > DEFAULT_STREAM_WIRE_BYTES:
            fail(
                "Ollama streamed response exceeded the configured bound",
                kind="response_too_large",
            )
        if not line.endswith(b"\n"):
            fail("Ollama streamed response line is incomplete", kind="invalid_response")
        now = time.monotonic()
        chunks_observed += 1
        last_progress = now
        if first_chunk_at is None:
            first_chunk_at = now
        try:
            chunk = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            fail("Ollama streamed response was not valid JSON", kind="invalid_response")
        if not isinstance(chunk, Mapping):
            fail("Ollama streamed response item is not an object", kind="invalid_response")
        message = chunk.get("message")
        if message is not None:
            if not isinstance(message, Mapping) or message.get("role") not in (None, "assistant"):
                fail("Ollama streamed assistant message is invalid", kind="invalid_response")
            if "tool_calls" in message:
                fail("Ollama tool-call activity is not permitted", kind="tool_call_rejected")
            thinking = message.get("thinking")
            if thinking is not None and type(thinking) is not str:
                fail("Ollama thinking field is invalid", kind="invalid_response")
            if thinking is not None:
                discarded_thinking_bytes += len(thinking.encode("utf-8"))
                discarded_thinking_chunks += 1
            piece = message.get("content", "")
            if type(piece) is not str:
                fail("Ollama streamed content is invalid", kind="invalid_response")
            piece_bytes = len(piece.encode("utf-8"))
            retained_bytes += piece_bytes
            if retained_bytes > DEFAULT_STREAM_RETAINED_CONTENT_BYTES:
                fail(
                    "Ollama retained assistant content exceeded the configured bound",
                    kind="response_too_large",
                )
            content_parts.append(piece)
        if chunk.get("done") is True:
            final = dict(chunk)
            done = True
            break
    if not done or final is None:
        fail("Ollama streamed response is incomplete", kind="invalid_completion")
    final_message = final.get("message")
    if not isinstance(final_message, Mapping):
        final_message = {"role": "assistant"}
    final["message"] = {
        "role": "assistant",
        "content": "".join(content_parts),
    }
    snapshot(completed=True)
    return final


def _validate_timeout_seconds(timeout_seconds: float) -> float:
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= MAX_REQUEST_TIMEOUT_SECONDS:
        raise OllamaAdapterError("Ollama request timeout is invalid", kind="configuration")
    return float(timeout_seconds)


def validate_reasoning_effort(value: Any) -> str:
    if type(value) is not str or value not in REASONING_EFFORTS:
        raise OllamaAdapterError(
            "reasoning effort is invalid",
            kind="configuration",
        )
    return value


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OllamaAdapterError("Ollama response timed out", kind="timeout")
    return remaining


def _http_json_request(
    endpoint: str,
    method: str,
    suffix: str,
    *,
    body: Mapping[str, Any] | None = None,
    timeout_seconds: float,
) -> Mapping[str, Any]:
    host, port, base_path = validate_endpoint(endpoint)
    timeout_seconds = _validate_timeout_seconds(timeout_seconds)
    request_bytes = None
    headers = {"Accept": "application/json"}
    if body is not None:
        request_bytes = (_safe_json(body) + "\n").encode("utf-8")
        if len(request_bytes) > MAX_HTTP_REQUEST_BODY_BYTES:
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


def _http_stream_request(
    endpoint: str,
    method: str,
    suffix: str,
    *,
    body: Mapping[str, Any] | None = None,
    timeout_seconds: float,
    idle_timeout_seconds: float = DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    telemetry_sink: dict[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Perform one bounded HTTP request whose response is Ollama NDJSON."""

    host, port, base_path = validate_endpoint(endpoint)
    timeout_seconds = _validate_timeout_seconds(timeout_seconds)
    request_bytes = None
    headers = {"Accept": "application/x-ndjson"}
    if body is not None:
        request_bytes = (_safe_json(body) + "\n").encode("utf-8")
        if len(request_bytes) > MAX_HTTP_REQUEST_BODY_BYTES:
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
        return _read_stream_response(
            response,
            connection,
            deadline=deadline,
            idle_timeout_seconds=idle_timeout_seconds,
            telemetry_sink=telemetry_sink,
        )
    finally:
        connection.close()


def _emit_provider_generation_started(stderr_stream: TextIO) -> None:
    """Emit the bounded marker immediately before the generation request."""

    stderr_stream.write(
        _safe_json(
            {
                "event": GENERATION_EVENT,
                "schema_version": GENERATION_EVENT_SCHEMA,
            }
        )
        + "\n"
    )
    stderr_stream.flush()


def _emit_provider_generation_telemetry(
    stderr_stream: TextIO,
    telemetry: Mapping[str, Any],
) -> None:
    """Emit bounded numeric progress telemetry without response content."""

    allowed = {
        "first_response_chunk_latency_seconds",
        "last_chunk_elapsed_seconds",
        "completion_elapsed_seconds",
        "timeout_phase",
        "progress_occurred",
        "progress_before_timeout",
        "wire_bytes_observed",
        "retained_content_bytes",
        "discarded_thinking_bytes",
        "discarded_thinking_chunks",
        "stream_chunks_observed",
    }
    bounded = {key: telemetry.get(key) for key in allowed}
    stderr_stream.write(
        _safe_json(
            {
                "schema_version": GENERATION_TELEMETRY_SCHEMA,
                "event": GENERATION_TELEMETRY_EVENT,
                **bounded,
            }
        )
        + "\n"
    )
    stderr_stream.flush()


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


def _extract_final_content(response: Mapping[str, Any], spec: CloudModelSpec) -> str:
    if response.get("model") != spec.upstream_model:
        raise OllamaAdapterError("Ollama returned an unexpected model", kind="model_mismatch")
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
    spec: CloudModelSpec,
    *,
    timeout_seconds: float,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    stream: bool = False,
    stderr_stream: TextIO | None = None,
    stream_idle_timeout_seconds: float = DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    telemetry_sink: dict[str, Any] | None = None,
) -> Mapping[str, Any]:
    reasoning_effort = validate_reasoning_effort(reasoning_effort)
    payload = {
        "model": spec.local_alias,
        "messages": build_chat_messages(request),
        "stream": bool(stream),
        "think": reasoning_effort,
    }
    # Validate all local request bounds before recording the provider boundary.
    # The next operation is the actual /api/chat request in _http_json_request.
    validate_endpoint(endpoint)
    _validate_timeout_seconds(timeout_seconds)
    request_bytes = (_safe_json(payload) + "\n").encode("utf-8")
    if len(request_bytes) > MAX_HTTP_REQUEST_BODY_BYTES:
        raise OllamaAdapterError(
            "Ollama request exceeded the configured bound", kind="request_too_large"
        )
    if stderr_stream is not None:
        _emit_provider_generation_started(stderr_stream)
    request_fn = _http_stream_request if stream else _http_json_request
    if stream:
        return request_fn(
            endpoint,
            "POST",
            "/chat",
            body=payload,
            timeout_seconds=timeout_seconds,
            idle_timeout_seconds=stream_idle_timeout_seconds,
            telemetry_sink=telemetry_sink,
        )
    return request_fn(endpoint, "POST", "/chat", body=payload, timeout_seconds=timeout_seconds)


def _preflight_model_entry(tags: Mapping[str, Any], spec: CloudModelSpec) -> Mapping[str, Any]:
    models = tags.get("models")
    if not isinstance(models, list):
        raise OllamaAdapterError("Ollama tags response is invalid", kind="preflight_failed")
    for entry in models:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("name") == spec.local_alias and entry.get("model") == spec.local_alias:
            if entry.get("remote_model") != spec.upstream_model:
                raise OllamaAdapterError("configured Ollama model has unexpected remote model", kind="preflight_failed")
            _normalize_cloud_remote_host(entry.get("remote_host"))
            return entry
    raise OllamaAdapterError("configured Ollama model is unavailable", kind="preflight_failed")


def _read_cloud_metadata(
    endpoint: str,
    spec: CloudModelSpec,
    *,
    deadline: float,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Establish Cloud provenance from zero-inference metadata only."""

    tags_response = _http_json_request(
        endpoint,
        "GET",
        "/tags",
        timeout_seconds=_remaining_timeout(deadline),
    )
    tag = _preflight_model_entry(tags_response, spec)
    show_response = _http_json_request(
        endpoint,
        "POST",
        "/show",
        body={"model": spec.local_alias},
        timeout_seconds=_remaining_timeout(deadline),
    )
    details = show_response.get("details")
    if not isinstance(details, Mapping) or not isinstance(show_response.get("model_info"), Mapping):
        raise OllamaAdapterError("Ollama model metadata is incomplete", kind="preflight_failed")
    if details.get("parent_model") != spec.upstream_model:
        raise OllamaAdapterError("configured Ollama model has unexpected parent model", kind="preflight_failed")
    return tag, show_response


def run_preflight(
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    model: str | CloudModelSpec = MODEL_ID,
    expected_version: str = EXPECTED_OLLAMA_VERSION,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Read only Ollama readiness and model metadata; never calls generation."""

    spec = model if isinstance(model, CloudModelSpec) else resolve_cloud_model(model)
    host, port, _ = validate_endpoint(endpoint)
    deadline = time.monotonic() + _validate_timeout_seconds(timeout_seconds)
    version_response = _http_json_request(
        endpoint,
        "GET",
        "/version",
        timeout_seconds=_remaining_timeout(deadline),
    )
    version = version_response.get("version")
    if type(version) is not str or version != expected_version:
        raise OllamaAdapterError("Ollama version is not the expected version", kind="preflight_failed")
    tag, show_response = _read_cloud_metadata(endpoint, spec, deadline=deadline)
    capabilities = show_response.get("capabilities")
    if not isinstance(capabilities, list) or any(type(item) is not str for item in capabilities):
        raise OllamaAdapterError("Ollama model capabilities are invalid", kind="preflight_failed")
    return {
        "schema_version": PREFLIGHT_SCHEMA,
        "ok": True,
        "endpoint": f"http://{host}:{port}/api",
        "local_daemon_api_ready": True,
        "ollama_version": version,
        "expected_model": spec.local_alias,
        "expected_remote_model": spec.upstream_model,
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
    parser.add_argument("--model", default=DEFAULT_MODEL_ID)
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="legacy alias: use one value for metadata and generation deadlines",
    )
    parser.add_argument("--metadata-timeout", type=float, default=60.0)
    parser.add_argument("--generation-timeout", type=float, default=240.0)
    parser.add_argument("--stream-idle-timeout", type=float, default=DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS)
    parser.add_argument("--max-logical-model-calls", type=int, default=DEFAULT_MAX_LOGICAL_MODEL_CALLS)
    parser.add_argument("--expected-version", default=EXPECTED_OLLAMA_VERSION)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT, choices=sorted(REASONING_EFFORTS))
    parser.add_argument(
        "--stream",
        action="store_true",
        help="consume bounded Ollama NDJSON chat output with thinking discarded",
    )
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)

    try:
        spec = resolve_cloud_model(args.model)
        validate_endpoint(args.endpoint)
        metadata_timeout = args.timeout if args.timeout is not None else args.metadata_timeout
        generation_timeout = args.timeout if args.timeout is not None else args.generation_timeout
        metadata_timeout = _validate_timeout_seconds(metadata_timeout)
        generation_timeout = _validate_timeout_seconds(generation_timeout)
        stream_idle_timeout = _validate_timeout_seconds(args.stream_idle_timeout)
        if args.preflight:
            result = run_preflight(
                endpoint=args.endpoint,
                model=spec,
                expected_version=args.expected_version,
                timeout_seconds=metadata_timeout,
            )
            stdout_stream.write(_safe_json(result) + "\n")
            stdout_stream.flush()
            return 0

        request = _read_request(stdin_stream)
        validate_logical_call_index(request, args.max_logical_model_calls)
        canonical_public_request(request)
        metadata_deadline = time.monotonic() + metadata_timeout
        try:
            _read_cloud_metadata(args.endpoint, spec, deadline=metadata_deadline)
        except OllamaAdapterError as exc:
            if exc.kind == "timeout" and exc.timeout_phase is None:
                exc.timeout_phase = "metadata"
            raise
        telemetry: dict[str, Any] = {}
        try:
            response = _chat_request(
                args.endpoint,
                request,
                spec,
                timeout_seconds=generation_timeout,
                reasoning_effort=args.reasoning_effort,
                stream=args.stream,
                stderr_stream=stderr_stream,
                stream_idle_timeout_seconds=stream_idle_timeout,
                telemetry_sink=telemetry,
            )
        except OllamaAdapterError as exc:
            if exc.telemetry:
                telemetry.update(exc.telemetry)
            if args.stream and telemetry:
                _emit_provider_generation_telemetry(stderr_stream, telemetry)
            raise
        if args.stream and telemetry:
            _emit_provider_generation_telemetry(stderr_stream, telemetry)
        content = _extract_final_content(response, spec)
        directive = parse_directive_content(content, request)
        usage: dict[str, Any] = {}
        for source, target in (("prompt_eval_count", "prompt_tokens"), ("eval_count", "completion_tokens")):
            value = response.get(source)
            if type(value) is int and value >= 0:
                usage[target] = value
        if "prompt_tokens" in usage and "completion_tokens" in usage:
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        output: dict[str, Any] = {"directive": directive}
        if usage:
            output["usage"] = usage
        stdout_stream.write(_safe_json(output) + "\n")
        stdout_stream.flush()
        return 0
    except OllamaAdapterError as exc:
        if exc.timeout_phase is not None and exc.telemetry is None:
            _emit_provider_generation_telemetry(
                stderr_stream,
                {
                    "timeout_phase": exc.timeout_phase,
                    "progress_occurred": False,
                    "progress_before_timeout": False,
                    "wire_bytes_observed": 0,
                    "retained_content_bytes": 0,
                    "discarded_thinking_bytes": 0,
                    "discarded_thinking_chunks": 0,
                    "stream_chunks_observed": 0,
                },
            )
        message = str(exc).replace("\r", " ").replace("\n", " ")
        encoded = message.encode("utf-8", errors="replace")[:256]
        message = encoded.decode("utf-8", errors="ignore") or "adapter failure"
        stderr_stream.write(
            _safe_json({
                "schema_version": "live-command-error-v1",
                "kind": exc.kind,
                "message": message,
            })
            + "\n"
        )
        stderr_stream.flush()
        return 1
    except (BrokenPipeError, OSError):
        return 1


def main() -> None:
    raise SystemExit(run_adapter())


if __name__ == "__main__":
    main()
