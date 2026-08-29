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
import hashlib
import http.client
import json
import re
import socket
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TextIO
from urllib.parse import urlsplit


@dataclass(frozen=True)
class CloudModelSpec:
    """One accepted Ollama Cloud alias and its fail-closed provenance contract.

    ``local_alias`` is the Ollama CLI / ``/api/chat`` request identity.
    ``upstream_model`` is the observed Cloud chat/parent identity:
    ``/api/show`` ``details.parent_model`` and the ``/api/chat`` response
    ``model``.  For most aliases ``/api/tags`` ``remote_model`` equals the
    same value, but a small number of Cloud aliases expose a versioned
    ``remote_model`` (e.g. ``deepseek-v4-flash:0731``) while ``show`` and
    chat use the unversioned parent.  ``tags_remote_model`` holds that
    versioned value when it diverges; otherwise it defaults to
    ``upstream_model``.  Capabilities, family and parameter metadata are
    recorded from ``/api/show``/``/api/tags`` where exposed and are never
    fabricated.
    """

    local_alias: str
    upstream_model: str
    tags_remote_model: str | None = None
    family: str | None = None
    parameter_count: int | None = None
    context_length: int | None = None
    capabilities: tuple[str, ...] = ()
    # Readiness is three-state: every alias is catalogued/selectable, a
    # subset declares a provisional transport profile (same-family ``think``
    # and streaming contract), and only empirically qualified models are
    # live-transport verified.  ``transport_profile_declared`` means the
    # registry carries an explicit ``thinking_level`` and intends the GPT-OSS
    # streaming path; ``transport_verified`` means a bounded live
    # qualification (real /api/chat streaming) has been recorded.  A model
    # whose live transport has never been exercised is not verified, even
    # if it shares a family with a verified sibling.
    transport_profile_declared: bool = False
    transport_verified: bool = False
    thinking_level: str | None = None
    idle_timeout_seconds: float = 20.0
    request_timeout_seconds: float = 60.0

    @property
    def effective_tags_remote_model(self) -> str:
        return self.tags_remote_model if self.tags_remote_model is not None else self.upstream_model

    @property
    def readiness(self) -> str:
        if self.transport_verified:
            return "live_verified"
        if self.transport_profile_declared:
            return "profile_declared"
        return "catalog"


CLOUD_MODELS: dict[str, CloudModelSpec] = {
    "gpt-oss:20b-cloud": CloudModelSpec(
        local_alias="gpt-oss:20b-cloud",
        upstream_model="gpt-oss:20b",
        family="gptoss",
        parameter_count=20914757184,
        context_length=131072,
        capabilities=("completion", "thinking", "tools"),
        transport_profile_declared=True,
        transport_verified=True,
        thinking_level="high",
    ),
    "gpt-oss:120b-cloud": CloudModelSpec(
        local_alias="gpt-oss:120b-cloud",
        upstream_model="gpt-oss:120b",
        family="gptoss",
        parameter_count=116829156672,
        context_length=131072,
        capabilities=("completion", "thinking", "tools"),
        transport_profile_declared=True,
        # Promoted from profile_declared after the immutable bounded
        # qualification artifact at
        # experiments/pdb_capability_ladder/transport_qualifications/
        # gpt-oss-120b-v1.json was inspected and accepted.
        transport_verified=True,
        thinking_level="high",
    ),
    "glm-5.1:cloud": CloudModelSpec(
        local_alias="glm-5.1:cloud",
        upstream_model="glm-5.1",
        family="glm5.1",
        parameter_count=756162687872,
        context_length=202752,
        capabilities=("completion", "thinking", "tools"),
        # Generic streaming profile declared for the frozen Level-32 queue.
        # Qualification determines transport viability without assuming a
        # model-specific think-level vocabulary.
        transport_profile_declared=True,
        # Promoted after independent review of the retained bounded
        # qualification artifact at
        # experiments/pdb_capability_ladder/transport_qualifications/
        # glm-5.1-v1.json.
        transport_verified=True,
        thinking_level=None,
    ),
    "glm-5.2:cloud": CloudModelSpec(
        local_alias="glm-5.2:cloud",
        upstream_model="glm-5.2",
        family="glm5.2",
        parameter_count=756162687872,
        context_length=1000000,
        capabilities=("completion", "thinking", "tools"),
        # Promoted from the accepted immutable Qualification V2 artifact at
        # experiments/pdb_capability_ladder/transport_qualifications/
        # glm-5.2-v2.json.  Preserve its no-thinking streaming profile.
        transport_profile_declared=True,
        transport_verified=True,
        thinking_level=None,
        idle_timeout_seconds=20.0,
        request_timeout_seconds=60.0,
    ),
    "glm-5.3-flash:cloud": CloudModelSpec(
        local_alias="glm-5.3-flash:cloud",
        upstream_model="glm-5.3-flash",
        family="glm5.3",
        parameter_count=None,
        context_length=1000000,
        capabilities=("completion", "thinking", "tools", "vision"),
        # General Ollama Cloud catalog entry for Local Project debugging.
        # Not scientifically qualified for the Level-32 ladder.
        transport_profile_declared=True,
        transport_verified=False,
        thinking_level=None,
        idle_timeout_seconds=20.0,
        request_timeout_seconds=60.0,
    ),
    "deepseek-v4-flash:cloud": CloudModelSpec(
        local_alias="deepseek-v4-flash:cloud",
        upstream_model="deepseek-v4-flash",
        tags_remote_model="deepseek-v4-flash:0731",
        family="deepseek4",
        parameter_count=304180418494,
        context_length=1048576,
        capabilities=("completion", "thinking", "tools"),
        # Generic streaming profile declared for the frozen Level-32 queue.
        # Qualification determines transport viability without assuming a
        # model-specific think-level vocabulary.
        transport_profile_declared=True,
        # Promoted after the retained bounded qualification artifact at
        # experiments/pdb_capability_ladder/transport_qualifications/
        # deepseek-v4-flash-v1.json was inspected and accepted.
        transport_verified=True,
        thinking_level=None,
        # The accepted Level-6/12/18 runs showed that DeepSeek can remain
        # validly quiet for longer than the generic 20-second watchdog.  Keep
        # the evidence-backed Level-32 profile identity-bound: 300 seconds of
        # stream inactivity and a 3,600-second outer request bound.
        idle_timeout_seconds=300.0,
        request_timeout_seconds=3600.0,
    ),
    "deepseek-v4-pro:cloud": CloudModelSpec(
        local_alias="deepseek-v4-pro:cloud",
        upstream_model="deepseek-v4-pro",
        tags_remote_model="deepseek-v4-pro:0813",
        family="deepseek4",
        parameter_count=1650497936906,
        context_length=1048576,
        capabilities=("completion", "thinking", "tools"),
        # Generic streaming profile declared for the frozen Level-32 queue.
        # Qualification determines transport viability without assuming a
        # model-specific think-level vocabulary.
        transport_profile_declared=True,
        # Promoted after independent review of the retained bounded
        # qualification artifact at
        # experiments/pdb_capability_ladder/transport_qualifications/
        # deepseek-v4-pro-v1.json.
        transport_verified=True,
        thinking_level=None,
    ),
    "kimi-k2.6:cloud": CloudModelSpec(
        local_alias="kimi-k2.6:cloud",
        upstream_model="kimi-k2.6",
        family="kimi-k2",
        parameter_count=1042000000000,
        context_length=262144,
        capabilities=("completion", "thinking", "tools", "vision"),
        # Ollama metadata exposes thinking and tools, so qualify the generic
        # streaming path without imposing a GPT-OSS-specific think level.
        transport_profile_declared=True,
        # Promoted after the retained bounded qualification artifact at
        # experiments/pdb_capability_ladder/transport_qualifications/
        # kimi-k2.6-v1.json was inspected and accepted.
        transport_verified=True,
        thinking_level=None,
        idle_timeout_seconds=45.0,
        request_timeout_seconds=75.0,
    ),
    "kimi-k2.7-code:cloud": CloudModelSpec(
        local_alias="kimi-k2.7-code:cloud",
        upstream_model="kimi-k2.7-code",
        family="kimi-k2",
        parameter_count=1042000000000,
        context_length=262144,
        capabilities=("completion", "thinking", "tools", "vision"),
        # Generic streaming profile declared for the frozen Level-32 queue.
        # Qualification determines transport viability without assuming a
        # model-specific think-level vocabulary.
        transport_profile_declared=True,
        transport_verified=False,
        thinking_level=None,
    ),
    "kimi-k3:cloud": CloudModelSpec(
        local_alias="kimi-k3:cloud",
        upstream_model="kimi-k3",
        family="kimi-k3",
        parameter_count=2812000000000,
        context_length=1048576,
        capabilities=("completion", "thinking", "tools", "vision"),
        transport_verified=False,
        thinking_level=None,
    ),
    "minimax-m2.7:cloud": CloudModelSpec(
        local_alias="minimax-m2.7:cloud",
        upstream_model="minimax-m2.7",
        family="minimax-m2",
        parameter_count=229000000000,
        context_length=196608,
        capabilities=("completion", "thinking", "tools"),
        # Generic streaming profile declared for the frozen Level-32 queue.
        # Qualification determines transport viability without assuming a
        # model-specific think-level vocabulary.
        transport_profile_declared=True,
        # Promoted after the retained bounded qualification artifact at
        # experiments/pdb_capability_ladder/transport_qualifications/
        # minimax-m2.7-v1.json was inspected and accepted.
        transport_verified=True,
        thinking_level=None,
    ),
    "minimax-m3:cloud": CloudModelSpec(
        local_alias="minimax-m3:cloud",
        upstream_model="minimax-m3",
        family="minimax-m3",
        parameter_count=0,
        context_length=524288,
        capabilities=("completion", "thinking", "tools", "vision"),
        # Generic streaming profile declared for the frozen Level-32 queue.
        # Qualification determines transport viability without assuming a
        # model-specific think-level vocabulary.
        transport_profile_declared=True,
        # Promoted after inspection of the retained bounded qualification
        # artifact at experiments/pdb_capability_ladder/transport_qualifications/
        # minimax-m3-v1.json.
        transport_verified=True,
        thinking_level=None,
    ),
    "nemotron-3-nano:30b-cloud": CloudModelSpec(
        local_alias="nemotron-3-nano:30b-cloud",
        upstream_model="nemotron-3-nano:30b",
        family="nemotron-3-nano",
        parameter_count=32000000000,
        context_length=262144,
        capabilities=("completion", "thinking", "tools"),
        transport_profile_declared=True,
        transport_verified=True,
        thinking_level="high",
    ),
    "nemotron-3-super:cloud": CloudModelSpec(
        local_alias="nemotron-3-super:cloud",
        upstream_model="nemotron-3-super",
        family="nemotron_h_moe",
        parameter_count=120000000000,
        context_length=262144,
        capabilities=("completion", "thinking", "tools"),
        # Generic streaming profile declared for the frozen Level-32 queue.
        # Qualification determines transport viability without assuming a
        # model-specific think-level vocabulary.
        transport_profile_declared=True,
        # Promoted after the retained bounded qualification artifact at
        # experiments/pdb_capability_ladder/transport_qualifications/
        # nemotron-3-super-v1.json was inspected and accepted.
        transport_verified=True,
        thinking_level=None,
        idle_timeout_seconds=45.0,
        request_timeout_seconds=75.0,
    ),
    "nemotron-3-ultra:cloud": CloudModelSpec(
        local_alias="nemotron-3-ultra:cloud",
        upstream_model="nemotron-3-ultra",
        family="",
        parameter_count=550000000000,
        context_length=262144,
        capabilities=("completion", "thinking", "tools"),
        # Generic streaming profile declared for the frozen Level-32 queue.
        # Qualification determines transport viability without assuming a
        # model-specific think-level vocabulary.
        transport_profile_declared=True,
        # Promoted after the retained bounded qualification artifact at
        # experiments/pdb_capability_ladder/transport_qualifications/
        # nemotron-3-ultra-v1.json was independently inspected and accepted.
        transport_verified=True,
        thinking_level=None,
        idle_timeout_seconds=45.0,
        request_timeout_seconds=75.0,
    ),
    "qwen3.5:cloud": CloudModelSpec(
        local_alias="qwen3.5:cloud",
        upstream_model="qwen3.5",
        tags_remote_model="qwen3.5:397b",
        family="qwen3.5",
        parameter_count=397000000000,
        context_length=262144,
        capabilities=("completion", "thinking", "tools", "vision"),
        # Ollama metadata exposes thinking and tools; qualify streaming
        # without assuming a model-specific think-level vocabulary.
        transport_profile_declared=True,
        # Promoted after the retained bounded qualification artifact at
        # experiments/pdb_capability_ladder/transport_qualifications/
        # qwen3.5-v1.json was inspected and accepted.
        transport_verified=True,
        thinking_level=None,
    ),
    "gemma4:31b-cloud": CloudModelSpec(
        local_alias="gemma4:31b-cloud",
        upstream_model="gemma4:31b",
        family="gemma4",
        parameter_count=32682372656,
        context_length=262144,
        capabilities=("completion", "thinking", "tools", "vision"),
        # Ollama metadata exposes thinking and tools; qualify generic
        # streaming without forcing a model-specific think level.
        transport_profile_declared=True,
        # Promoted after the retained bounded qualification artifact at
        # experiments/pdb_capability_ladder/transport_qualifications/
        # gemma4-31b-v1.json was inspected and accepted.
        transport_verified=True,
        thinking_level=None,
    ),
    "mistral-large-3:675b-cloud": CloudModelSpec(
        local_alias="mistral-large-3:675b-cloud",
        upstream_model="mistral-large-3:675b",
        family="mistral3",
        parameter_count=675000000000,
        context_length=262144,
        capabilities=("completion", "tools", "vision"),
        # Generic streaming profile declared for the frozen Level-32 queue.
        # This model exposes no thinking capability, so qualification must
        # exercise the NDJSON path without a fabricated `think` setting.
        transport_profile_declared=True,
        # Promoted after the retained bounded qualification artifact at
        # experiments/pdb_capability_ladder/transport_qualifications/
        # mistral-large-3-675b-v1.json was independently inspected and accepted.
        transport_verified=True,
        thinking_level=None,
    ),
}

MODEL_ID = "gpt-oss:20b-cloud"
DEFAULT_MODEL_ID = MODEL_ID
ALLOWED_MODEL_IDENTIFIERS = frozenset(CLOUD_MODELS)
EXPECTED_CLOUD_REMOTE_MODEL = CLOUD_MODELS[MODEL_ID].upstream_model
EXPECTED_CLOUD_REMOTE_HOST = "https://ollama.com"
DEFAULT_ENDPOINT = "http://127.0.0.1:11434/api"
EXPECTED_OLLAMA_VERSION = "0.33.0"
COMMAND_ERROR_SCHEMA_VERSION = "command-error-v1"
PROTOCOL_NAME = "agentic-debugger-live-jsonl"
PROTOCOL_VERSION = "1.3"
PROVIDER_COMPLETION_ENVELOPE_SCHEMA = "provider-completion-v1"
CONTENT_FRAGMENT_OBSERVABILITY_SCHEMA_VERSION = (
    "ollama-content-fragment-observability-v2"
)

DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_REQUEST_TIMEOUT_SECONDS = 3600.0
DEFAULT_THINKING_LEVEL = "high"
# The inherited 25,000-byte ceiling rejected Kimi's otherwise valid frozen
# Level-32 request at 26,622 bytes after successful PDB evidence.  32 KiB is
# the smallest bounded repair that admits the observed request while staying
# below MAX_RAW_RESPONSE_BYTES and preserving the same public task contract.
MAX_PUBLIC_REQUEST_BYTES = 32_768
MAX_RAW_RESPONSE_BYTES = 64 * 1024
MAX_STREAM_FRAME_BYTES = 1024 * 1024
MAX_RETAINED_CONTENT_FRAME_DIAGNOSTICS = 128
MAX_CONTENT_FRAGMENT_TEXT_BYTES = 4096
_SECRET_CONTENT = re.compile(
    r"(?i)(?:bearer\s+\S+|basic\s+\S+|(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|token|private[_-]?key)\s*[:=]\s*\S+)"
)
MAX_STDIN_REQUEST_BYTES = 128 * 1024
DEFAULT_MAX_LOGICAL_MODEL_CALLS = 25
MAX_CONFIGURED_LOGICAL_MODEL_CALLS = 512
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


def is_live_transport_ready(spec: CloudModelSpec) -> bool:
    """Return whether ``spec`` has passed the bounded live qualification gate."""

    return bool(spec.transport_verified)


def is_treatment_eligible(spec: CloudModelSpec) -> bool:
    """Return whether ``spec`` may enter the Level-32 scientific treatment."""

    return is_live_transport_ready(spec)


TRANSPORT_QUALIFICATION_COMMAND = (
    "python -m agentic_debugger.evaluation.transport_qualification "
    "--endpoint http://127.0.0.1:11434/api "
    "--model <alias> --confirm-live --json"
)


def transport_config_fingerprint(spec: CloudModelSpec) -> str:
    """Bounded fingerprint of the model-relevant execution configuration.

    Covers every execution parameter that can materially change transport
    behavior.  Two treatments whose fingerprints differ must not silently
    reuse the same treatment identity.
    """

    payload = {
        "local_alias": spec.local_alias,
        "upstream_model": spec.upstream_model,
        "effective_tags_remote_model": spec.effective_tags_remote_model,
        "capabilities": list(spec.capabilities),
        "family": spec.family,
        "parameter_count": spec.parameter_count,
        "context_length": spec.context_length,
        "readiness": spec.readiness,
        "transport_profile_declared": spec.transport_profile_declared,
        "transport_verified": spec.transport_verified,
        "thinking_level": spec.thinking_level,
        "idle_timeout_seconds": spec.idle_timeout_seconds,
        "request_timeout_seconds": spec.request_timeout_seconds,
        "protocol_version": PROTOCOL_VERSION,
        "protocol_name": PROTOCOL_NAME,
        "expected_ollama_version": EXPECTED_OLLAMA_VERSION,
        "expected_cloud_remote_host": EXPECTED_CLOUD_REMOTE_HOST,
        "default_timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "max_public_request_bytes": MAX_PUBLIC_REQUEST_BYTES,
        "max_raw_response_bytes": MAX_RAW_RESPONSE_BYTES,
        "max_stream_frame_bytes": MAX_STREAM_FRAME_BYTES,
        "max_stdin_request_bytes": MAX_STDIN_REQUEST_BYTES,
        "max_directive_argument_bytes": MAX_DIRECTIVE_ARGUMENT_BYTES,
        "max_directive_reason_bytes": MAX_DIRECTIVE_REASON_BYTES,
        "max_directive_statement_bytes": MAX_DIRECTIVE_STATEMENT_BYTES,
        "max_directive_hypothesis_id_bytes": MAX_DIRECTIVE_HYPOTHESIS_ID_BYTES,
        "max_directive_evidence_ref_bytes": MAX_DIRECTIVE_EVIDENCE_REF_BYTES,
        "max_directive_evidence_ref_count": MAX_DIRECTIVE_EVIDENCE_REF_COUNT,
        "default_max_logical_model_calls": DEFAULT_MAX_LOGICAL_MODEL_CALLS,
        "max_configured_logical_model_calls": MAX_CONFIGURED_LOGICAL_MODEL_CALLS,
        "adapter_retry_count": ADAPTER_RETRY_COUNT,
        "fallback_count": FALLBACK_COUNT,
        "stream_mode": True,
        "provider_completion_envelope_schema": PROVIDER_COMPLETION_ENVELOPE_SCHEMA,
        "observability_schema": "directive-observability-v1",
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_cloud_model(model_id: Any) -> CloudModelSpec:
    """Return the accepted Cloud spec for ``model_id`` or fail closed."""

    if type(model_id) is not str or not model_id:
        raise OllamaAdapterError("requested Ollama Cloud model is invalid", kind="configuration")
    spec = CLOUD_MODELS.get(model_id)
    if spec is None:
        raise OllamaAdapterError("requested Ollama Cloud model is not supported", kind="configuration")
    return spec


def effective_thinking_level(spec: CloudModelSpec, requested: str | None) -> str | None:
    """Return the stream ``think`` value for ``spec``, validating explicit overrides."""

    if requested is not None:
        if requested not in {"low", "medium", "high"}:
            raise OllamaAdapterError("Ollama thinking level is invalid", kind="configuration")
        if spec.transport_profile_declared and requested != spec.thinking_level:
            raise OllamaAdapterError(
                "declared transport profile does not accept a different thinking level",
                kind="configuration",
            )
        return requested
    return spec.thinking_level


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
    "add_hypothesis and revise_hypothesis use exactly these fields: \"kind\", \"hypothesis_id\", \"statement\", \"confidence\", \"evidence_refs\", and \"requires_runtime_evidence\".\n"
    "For either hypothesis kind, copy the current user message's Legal hypothesis representation and obey every current directive_schema constraint; especially do not guess or invert the required requires_runtime_evidence boolean.\n"
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
        if "example" in spec:
            return spec["example"]
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            return enum[0]
        type_name = spec.get("type")
        if type_name == "boolean":
            return True
        if type_name == "integer":
            minimum = spec.get("minimum")
            if type(minimum) is int and not isinstance(minimum, bool):
                return minimum
            return 0
        if type_name == "number":
            minimum = spec.get("minimum")
            if type(minimum) in (int, float) and not isinstance(minimum, bool):
                return minimum
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


def _public_breakpoint_source(request: Mapping[str, Any]) -> tuple[str, int, str] | None:
    """Return a source line already visible in the current exact-PDB history."""

    controller = request.get("controller")
    observations = [
        entry.get("last_observation")
        for entry in request.get("history", [])
        if isinstance(entry, Mapping)
    ]
    if isinstance(controller, Mapping):
        observations.append(controller.get("last_observation"))
    breakpoint_line: int | None = None
    source_lines: dict[tuple[str, int], str] = {}
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        payload = observation.get("payload")
        if not isinstance(payload, Mapping):
            continue
        proof = payload.get("proof")
        if isinstance(proof, Mapping) and type(proof.get("breakpoint_line")) is int:
            breakpoint_line = proof["breakpoint_line"]
        lines = payload.get("lines")
        if isinstance(lines, list):
            for entry in lines:
                if not isinstance(entry, Mapping):
                    continue
                path = entry.get("path")
                number = entry.get("line_number")
                text = entry.get("text")
                if type(path) is str and type(number) is int and type(text) is str:
                    source_lines[(path, number)] = text
    if breakpoint_line is None:
        return None
    matches = [
        (path, number, text)
        for (path, number), text in source_lines.items()
        if number == breakpoint_line
    ]
    return matches[0] if len(matches) == 1 else None


def build_apply_patch_guidance(request: Mapping[str, Any]) -> str:
    """PatchManager-derived apply_patch format and recovery rules."""

    controller = request.get("controller")
    if not isinstance(controller, Mapping):
        controller = {}
    contracts = request.get("action_contracts")
    lines = [
        "apply_patch arguments.patch must be a complete unified diff accepted by Local Application's PatchManager. The Level-32 operator derives the strict official Git artifact from the accepted workspace state.",
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
        "Prefer the smallest valid hunk that uniquely expresses the edit. Include unchanged context lines when available; do not rely on manual hunk serialization for official evaluation.",
        "Hunk body prefixes are significant: one leading space for unchanged/context, - for removed, + for added.",
        "Use repository-relative paths only.",
        "Do not wrap the patch string in Markdown fences.",
        "The complete response is JSON: encode every patch line break as the JSON escape \\n inside arguments.patch; never place a literal unescaped newline inside that quoted JSON string.",
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
    breakpoint_source = _public_breakpoint_source(request)
    if breakpoint_source is not None:
        path, line_number, old_line = breakpoint_source
        lines.extend(
            [
                f"Current public PDB/source evidence binds the diagnosed line to {path}:{line_number}.",
                "Use that source line as the removed line in a normal context-bearing hunk when possible; the operator will serialize the accepted workspace state for official Git evaluation.",
            ]
        )
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
    proof_gate = request.get("proof_gate")
    if isinstance(proof_gate, Mapping):
        next_actions = proof_gate.get("next_required_actions")
        if isinstance(next_actions, list) and all(type(item) is str for item in next_actions):
            lines.append(
                "Exact-proof next required actions: "
                + (", ".join(next_actions) if next_actions else "none; use a legal transition")
                + "."
            )
    directive_schema = request.get("directive_schema")
    if isinstance(directive_schema, Mapping):
        for hypothesis_kind in ("add_hypothesis", "revise_hypothesis"):
            if hypothesis_kind not in kinds:
                continue
            schema = directive_schema.get(hypothesis_kind)
            constraints = schema.get("constraints") if isinstance(schema, Mapping) else None
            runtime_constraint = (
                constraints.get("requires_runtime_evidence")
                if isinstance(constraints, Mapping)
                else None
            )
            runtime_values = (
                runtime_constraint.get("enum")
                if isinstance(runtime_constraint, Mapping)
                else None
            )
            runtime_value = (
                runtime_values[0]
                if isinstance(runtime_values, list)
                and len(runtime_values) == 1
                and type(runtime_values[0]) is bool
                else False
            )
            def constrained_value(field: str, fallback: Any) -> Any:
                constraint = constraints.get(field) if isinstance(constraints, Mapping) else None
                if isinstance(constraint, Mapping) and "example" in constraint:
                    return constraint["example"]
                values = constraint.get("enum") if isinstance(constraint, Mapping) else None
                return values[0] if isinstance(values, list) and len(values) == 1 else fallback

            example = {
                "kind": hypothesis_kind,
                "hypothesis_id": constrained_value("hypothesis_id", "hypothesis-1"),
                "statement": "bounded hypothesis",
                "confidence": constrained_value("confidence", "low"),
                "evidence_refs": constrained_value("evidence_refs", []),
                "requires_runtime_evidence": runtime_value,
            }
            lines.append(
                "Legal hypothesis representation: "
                + json.dumps(example, ensure_ascii=False, separators=(",", ":"))
            )
            if hypothesis_kind == "revise_hypothesis":
                lines.append(
                    "For revise_hypothesis, replace evidence_refs with actual observation_id values from current history."
                )
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
            if any(
                type(value) is str and value.startswith("<") and value.endswith(">")
                for value in example.get("arguments", {}).values()
            ):
                lines.append(
                    "Every angle-bracket value in that action shape is structural; replace it with a substantive current value and never copy the placeholder literally."
                )
            if name == "start_pdb_session":
                lines.append(
                    "The shown breakpoint number is only a shape. Replace it with a visible executable target-function line; not def/import/module code."
                )
            if name == "apply_patch":
                lines.append(build_apply_patch_guidance(request))
    targets = controller.get("legal_transition_targets")
    if "transition" in kinds and isinstance(targets, list) and targets:
        legal_targets = [target for target in targets if type(target) is str]
        if len(legal_targets) == 1:
            lines.append(
                "Legal transition representation: "
                + json.dumps(
                    {
                        "kind": "transition",
                        "target_state": legal_targets[0],
                        "reason": "advance using the sole legal target",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        elif legal_targets:
            lines.append(
                "Legal transition representation: "
                '{"kind":"transition","target_state":"<one of '
                + ", ".join(legal_targets)
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


def _validate_timeout_seconds(timeout_seconds: float) -> float:
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= MAX_REQUEST_TIMEOUT_SECONDS:
        raise OllamaAdapterError("Ollama request timeout is invalid", kind="configuration")
    return float(timeout_seconds)


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
            raise OllamaAdapterError(
                f"Ollama HTTP request returned status {response.status}",
                kind="http_error",
            )
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
    if type(maximum) is not int or isinstance(maximum, bool) or not 1 <= maximum <= MAX_CONFIGURED_LOGICAL_MODEL_CALLS:
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
        raise OllamaAdapterError("logical model call is outside the configured bound", kind="logical_call_limit")


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


def _bounded_content_fragment(content: str) -> dict[str, Any]:
    """Apply the adapter's local bounded/redacted content policy.

    The command adapter is also launched from an isolated temporary working
    directory, so this small policy helper must not depend on importing the
    repository package merely to collect passive stream diagnostics.
    """

    secret_shaped = bool(_SECRET_CONTENT.search(content))
    sanitized = _SECRET_CONTENT.sub("<redacted>", content)
    raw = sanitized.encode("utf-8")
    truncated = len(raw) > MAX_CONTENT_FRAGMENT_TEXT_BYTES
    if truncated:
        raw = raw[: MAX_CONTENT_FRAGMENT_TEXT_BYTES - 3]
        while True:
            try:
                text = raw.decode("utf-8") + "..."
                break
            except UnicodeDecodeError:
                raw = raw[:-1]
    else:
        text = sanitized
    retained = text.encode("utf-8")
    return {
        "content_sha256": (
            None
            if secret_shaped
            else hashlib.sha256(content.encode("utf-8")).hexdigest()
        ),
        "content_text_byte_length": len(retained),
        "content_text_sha256": hashlib.sha256(retained).hexdigest(),
        "content_text": text,
        "content_text_redacted": secret_shaped,
        "content_text_truncated": truncated,
    }


def _validate_content_frame_diagnostic(
    *,
    diagnostic: Mapping[str, Any],
    original_content: str,
) -> None:
    """Fail closed when retained content evidence is internally inconsistent."""

    original_bytes = original_content.encode("utf-8")
    retained_text = diagnostic.get("content_text")
    retained_bytes = retained_text.encode("utf-8") if type(retained_text) is str else None
    if diagnostic.get("content_byte_length") != len(original_bytes):
        raise OllamaAdapterError(
            "content observability original byte length is inconsistent",
            kind="observability_error",
        )
    if type(diagnostic.get("content_text_byte_length")) is not int or retained_bytes is None:
        raise OllamaAdapterError(
            "content observability retained byte length is missing",
            kind="observability_error",
        )
    if diagnostic["content_text_byte_length"] != len(retained_bytes):
        raise OllamaAdapterError(
            "content observability retained byte length is inconsistent",
            kind="observability_error",
        )
    if diagnostic.get("content_text_sha256") != hashlib.sha256(retained_bytes).hexdigest():
        raise OllamaAdapterError(
            "content observability retained hash is inconsistent",
            kind="observability_error",
        )
    redacted = diagnostic.get("content_text_redacted") is True
    truncated = diagnostic.get("content_text_truncated") is True
    if not redacted and not truncated:
        if retained_text != original_content:
            raise OllamaAdapterError(
                "non-truncated content observability text is not exact",
                kind="observability_error",
            )
        if diagnostic.get("content_sha256") != hashlib.sha256(original_bytes).hexdigest():
            raise OllamaAdapterError(
                "content observability original hash is inconsistent",
                kind="observability_error",
            )


def _validate_content_fragment_aggregate(
    *,
    final_content: str,
    diagnostics: Sequence[Mapping[str, Any]],
    diagnostics_truncated: bool,
) -> None:
    """Verify exact retained fragments reconstruct the parser-authorized content."""

    if diagnostics_truncated:
        return
    parts: list[str] = []
    for diagnostic in diagnostics:
        if diagnostic.get("content_present") is not True:
            continue
        if diagnostic.get("content_text_redacted") or diagnostic.get("content_text_truncated"):
            return
        parts.append(diagnostic["content_text"])
    reconstructed = "".join(parts)
    if reconstructed != final_content:
        raise OllamaAdapterError(
            "content observability fragments do not reconstruct final content",
            kind="observability_error",
        )
    if hashlib.sha256(reconstructed.encode("utf-8")).hexdigest() != hashlib.sha256(final_content.encode("utf-8")).hexdigest():
        raise OllamaAdapterError(
            "content observability aggregate hash is inconsistent",
            kind="observability_error",
        )


def _content_frame_diagnostic(
    *,
    frame_index: int,
    done: bool,
    thinking: str,
    content: str,
) -> dict[str, Any]:
    """Build bounded diagnostics for one authorized content-channel frame.

    Thinking text is intentionally represented only by length and presence
    metadata.
    """

    bounded = _bounded_content_fragment(content)
    diagnostic = {
        "frame_index": frame_index,
        "done": done,
        "thinking_byte_length": len(thinking.encode("utf-8")),
        "content_byte_length": len(content.encode("utf-8")),
        "thinking_present": bool(thinking),
        "content_present": bool(content),
        "both_channels_nonempty": bool(thinking and content),
        **bounded,
    }
    _validate_content_frame_diagnostic(diagnostic=diagnostic, original_content=content)
    return diagnostic


def _stream_chat_request(
    endpoint: str,
    request: Mapping[str, Any],
    spec: CloudModelSpec,
    *,
    idle_timeout_seconds: float,
    request_deadline: float | None = None,
    thinking_level: str | None,
    activity_stream: TextIO,
 ) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Read Ollama NDJSON while treating every frame as progress.

    Thinking is a progress channel, never directive authority.  Its text is
    counted and immediately discarded.  Only ``message.content`` is retained
    under the directive-content bound.  Bounded frame diagnostics retain only
    authorized content text and safe thinking metadata.  One whitespace
    heartbeat per frame lets the parent command transport refresh its idle
    watchdog without ever receiving or persisting private reasoning text.
    """

    if thinking_level is not None and thinking_level not in {"low", "medium", "high"}:
        raise OllamaAdapterError("Ollama thinking level is invalid", kind="configuration")
    host, port, base_path = validate_endpoint(endpoint)
    idle_timeout_seconds = _validate_timeout_seconds(idle_timeout_seconds)
    payload: dict[str, Any] = {
        "model": spec.local_alias,
        "messages": build_chat_messages(request),
        "stream": True,
    }
    if thinking_level is not None:
        payload["think"] = thinking_level
    request_bytes = (_safe_json(payload) + "\n").encode("utf-8")
    if len(request_bytes) > MAX_RAW_RESPONSE_BYTES:
        raise OllamaAdapterError("Ollama request exceeded the configured bound", kind="request_too_large")

    if request_deadline is None:
        request_deadline = time.monotonic() + idle_timeout_seconds
    connection = http.client.HTTPConnection(
        host,
        port,
        timeout=min(idle_timeout_seconds, _remaining_timeout(request_deadline)),
    )
    content_parts: list[str] = []
    content_bytes = 0
    thinking_bytes = 0
    frame_count = 0
    content_frame_count = 0
    first_content_frame_index: int | None = None
    last_content_frame_index: int | None = None
    content_frame_diagnostics: list[dict[str, Any]] = []
    content_frame_diagnostics_truncated = False
    final: Mapping[str, Any] | None = None
    try:
        try:
            connection.request(
                "POST",
                _path(base_path, "/chat"),
                body=request_bytes,
                headers={
                    "Accept": "application/x-ndjson",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
        except (socket.timeout, TimeoutError):
            raise OllamaAdapterError("Ollama request was idle for too long", kind="timeout") from None
        except (OSError, http.client.HTTPException):
            raise OllamaAdapterError("Ollama HTTP request failed", kind="http_error") from None
        if response.status < 200 or response.status >= 300:
            raise OllamaAdapterError(
                f"Ollama HTTP request returned status {response.status}",
                kind="http_error",
            )

        while True:
            # Every received frame refreshes the liveness timeout. The
            # request deadline remains the outer treatment bound.
            read_timeout = min(
                idle_timeout_seconds,
                _remaining_timeout(request_deadline),
            )
            if connection.sock is not None:
                connection.sock.settimeout(read_timeout)
            try:
                raw_line = response.readline(MAX_STREAM_FRAME_BYTES + 1)
            except (socket.timeout, TimeoutError):
                raise OllamaAdapterError("Ollama stream was idle for too long", kind="timeout") from None
            except (OSError, http.client.IncompleteRead):
                raise OllamaAdapterError("Ollama stream could not be read", kind="http_error") from None
            if not raw_line:
                break
            if len(raw_line) > MAX_STREAM_FRAME_BYTES:
                raise OllamaAdapterError("Ollama stream frame exceeded the configured bound", kind="response_too_large")
            if not raw_line.strip():
                continue
            try:
                frame = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise OllamaAdapterError("Ollama stream frame was invalid", kind="invalid_response") from None
            if not isinstance(frame, Mapping):
                raise OllamaAdapterError("Ollama stream frame was not an object", kind="invalid_response")
            if frame.get("model") != spec.upstream_model:
                raise OllamaAdapterError("Ollama returned an unexpected model", kind="model_mismatch")
            done = frame.get("done")
            if type(done) is not bool:
                raise OllamaAdapterError("Ollama stream completion flag is invalid", kind="invalid_completion")
            message = frame.get("message")
            if not isinstance(message, Mapping) or message.get("role") != "assistant":
                raise OllamaAdapterError("Ollama assistant message is invalid", kind="invalid_response")
            if "tool_calls" in message:
                raise OllamaAdapterError("Ollama tool-call activity is not permitted", kind="tool_call_rejected")
            thinking = message.get("thinking", "")
            content = message.get("content", "")
            if type(thinking) is not str or type(content) is not str:
                raise OllamaAdapterError("Ollama stream message fields are invalid", kind="invalid_response")

            thinking_bytes += len(thinking.encode("utf-8"))
            content_bytes += len(content.encode("utf-8"))
            if content_bytes > MAX_RAW_RESPONSE_BYTES:
                raise OllamaAdapterError("Ollama directive content exceeded the configured bound", kind="response_too_large")
            content_parts.append(content)
            if content:
                content_frame_count += 1
                if first_content_frame_index is None:
                    first_content_frame_index = frame_count
                last_content_frame_index = frame_count
            if thinking or content or done:
                if len(content_frame_diagnostics) < MAX_RETAINED_CONTENT_FRAME_DIAGNOSTICS:
                    content_frame_diagnostics.append(
                        _content_frame_diagnostic(
                            frame_index=frame_count,
                            done=done,
                            thinking=thinking,
                            content=content,
                        )
                    )
                else:
                    content_frame_diagnostics_truncated = True
            frame_count += 1
            activity_stream.write("\n")
            activity_stream.flush()
            if done:
                if type(frame.get("done_reason")) is not str or not frame["done_reason"]:
                    raise OllamaAdapterError("Ollama completion metadata is invalid", kind="invalid_completion")
                final = frame
                break

        if final is None:
            raise OllamaAdapterError("Ollama stream ended without completion", kind="invalid_completion")
    finally:
        connection.close()

    final_content = "".join(content_parts)
    if not final_content.strip():
        raise OllamaAdapterError("Ollama assistant content is missing", kind="invalid_response")
    _validate_content_fragment_aggregate(
        final_content=final_content,
        diagnostics=content_frame_diagnostics,
        diagnostics_truncated=content_frame_diagnostics_truncated,
    )
    usage: dict[str, Any] = {}
    prompt_tokens = final.get("prompt_eval_count")
    completion_tokens = final.get("eval_count")
    if type(prompt_tokens) is int and prompt_tokens >= 0:
        usage["prompt_tokens"] = prompt_tokens
    if type(completion_tokens) is int and completion_tokens >= 0:
        usage["completion_tokens"] = completion_tokens
    if "prompt_tokens" in usage and "completion_tokens" in usage:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return final_content, usage, {
        "schema_version": CONTENT_FRAGMENT_OBSERVABILITY_SCHEMA_VERSION,
        "stream_frame_count": frame_count,
        "thinking_bytes": thinking_bytes,
        "content_bytes": content_bytes,
        "first_content_frame_index": first_content_frame_index,
        "last_content_frame_index": last_content_frame_index,
        "content_frame_count": content_frame_count,
        "final_content_byte_length": len(final_content.encode("utf-8")),
        "final_content_sha256": hashlib.sha256(final_content.encode("utf-8")).hexdigest(),
        "content_frame_diagnostics": content_frame_diagnostics,
        "content_frame_diagnostics_truncated": content_frame_diagnostics_truncated,
    }


def _chat_request(
    endpoint: str,
    request: Mapping[str, Any],
    spec: CloudModelSpec,
    *,
    timeout_seconds: float,
) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "model": spec.local_alias,
        "messages": build_chat_messages(request),
        "stream": False,
    }
    thinking = effective_thinking_level(spec, None)
    if thinking is not None:
        payload["think"] = thinking
    return _http_json_request(
        endpoint,
        "POST",
        "/chat",
        body=payload,
        timeout_seconds=timeout_seconds,
    )


def _preflight_model_entry(tags: Mapping[str, Any], spec: CloudModelSpec) -> Mapping[str, Any]:
    models = tags.get("models")
    if not isinstance(models, list):
        raise OllamaAdapterError("Ollama tags response is invalid", kind="preflight_failed")
    for entry in models:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("name") == spec.local_alias and entry.get("model") == spec.local_alias:
            if entry.get("remote_model") != spec.effective_tags_remote_model:
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
        # Use distinct, allowlistedkind for exact-version gate so the operator can preserve structured
        # expected/actual without exposing arbitrary daemon stderr. Version strings are bounded semver.
        raise OllamaAdapterError(
            f"Ollama version mismatch: expected {expected_version!r} actual {version!r}",
            kind="ollama_version_mismatch",
        )
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
        "expected_tags_remote_model": spec.effective_tags_remote_model,
        "model_available": True,
        "model_metadata_readable": True,
        "model_remote_model": tag["remote_model"],
        "model_remote_host": _normalize_cloud_remote_host(tag["remote_host"]),
        "model_capabilities": sorted(capabilities),
        "model_tag_digest": tag.get("digest") if type(tag.get("digest")) is str else None,
        "readiness": spec.readiness,
        "transport_profile_declared": spec.transport_profile_declared,
        "model_transport_verified": spec.transport_verified,
        "live_transport_ready": is_live_transport_ready(spec),
        "treatment_eligible": is_treatment_eligible(spec),
        "model_thinking_level": spec.thinking_level,
        "idle_timeout_seconds": spec.idle_timeout_seconds,
        "request_timeout_seconds": spec.request_timeout_seconds,
        "transport_config_fingerprint": transport_config_fingerprint(spec),
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
        default=DEFAULT_TIMEOUT_SECONDS,
        help="stream inactivity timeout in seconds",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=None,
        help="outer metadata/generation request timeout in seconds",
    )
    parser.add_argument("--max-logical-model-calls", type=int, default=DEFAULT_MAX_LOGICAL_MODEL_CALLS)
    parser.add_argument("--expected-version", default=EXPECTED_OLLAMA_VERSION)
    parser.add_argument(
        "--thinking-level",
        choices=("low", "medium", "high"),
        default=None,
    )
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    try:
        if args.list_models:
            if args.json_output:
                payload = {
                    spec.local_alias: {
                        "local_alias": spec.local_alias,
                        "upstream_model": spec.upstream_model,
                        "effective_tags_remote_model": spec.effective_tags_remote_model,
                        "family": spec.family,
                        "parameter_count": spec.parameter_count,
                        "context_length": spec.context_length,
                        "capabilities": list(spec.capabilities),
                        "readiness": spec.readiness,
                        "transport_profile_declared": spec.transport_profile_declared,
                        "transport_verified": spec.transport_verified,
                        "live_transport_ready": is_live_transport_ready(spec),
                        "treatment_eligible": is_treatment_eligible(spec),
                        "thinking_level": spec.thinking_level,
                        "transport_config_fingerprint": transport_config_fingerprint(spec),
                    }
                    for spec in sorted(CLOUD_MODELS.values(), key=lambda s: s.local_alias)
                }
                stdout_stream.write(_safe_json(payload) + "\n")
            else:
                for spec in sorted(CLOUD_MODELS.values(), key=lambda s: s.local_alias):
                    if spec.transport_verified:
                        flag = "live_verified"
                    elif spec.transport_profile_declared:
                        flag = "profile_declared"
                    else:
                        flag = "catalog"
                    think = spec.thinking_level if spec.thinking_level is not None else "default"
                    stdout_stream.write(
                        f"{spec.local_alias:30} -> {spec.upstream_model:25} [{flag}] think={think}\n"
                    )
            stdout_stream.flush()
            return 0
        spec = resolve_cloud_model(args.model)
        validate_endpoint(args.endpoint)
        if args.preflight:
            result = run_preflight(
                endpoint=args.endpoint,
                model=spec,
                expected_version=args.expected_version,
                timeout_seconds=args.timeout,
            )
            stdout_stream.write(_safe_json(result) + "\n")
            stdout_stream.flush()
            return 0

        request = _read_request(stdin_stream)
        validate_logical_call_index(request, args.max_logical_model_calls)
        canonical_public_request(request)
        idle_timeout = _validate_timeout_seconds(args.timeout)
        request_timeout = _validate_timeout_seconds(
            args.request_timeout if args.request_timeout is not None else args.timeout
        )
        deadline = time.monotonic() + request_timeout
        _read_cloud_metadata(args.endpoint, spec, deadline=deadline)
        thinking = effective_thinking_level(spec, args.thinking_level)
        content, usage, activity = _stream_chat_request(
            args.endpoint,
            request,
            spec,
            idle_timeout_seconds=idle_timeout,
            request_deadline=deadline,
            thinking_level=thinking,
            activity_stream=stderr_stream,
        )
        result: dict[str, Any] = {
            "provider_completion_schema_version": PROVIDER_COMPLETION_ENVELOPE_SCHEMA,
            "directive_content": content,
            "transport_activity": activity,
        }
        if usage:
            result["usage"] = usage
        stdout_stream.write(_safe_json(result) + "\n")
        stdout_stream.flush()
        return 0
    except OllamaAdapterError as exc:
        stderr_stream.write(
            _safe_json(
                {
                    "schema_version": COMMAND_ERROR_SCHEMA_VERSION,
                    "kind": exc.kind,
                    "message": str(exc),
                }
            )
            + "\n"
        )
        stderr_stream.flush()
        return 1
    except (BrokenPipeError, OSError):
        return 1


def build_ollama_live_config(
    alias: str,
    *,
    logical_call_ceiling: int = 32,
    idle_timeout_seconds: int | None = None,
    request_timeout_seconds: int | None = None,
):
    """Provider-free canonical Ollama LiveModelConfig construction.

    Validates the alias against the repository-owned Cloud roster, derives
    timeouts from the model's transport profile when not supplied, and builds
    the exact JSONL command the worker will execute. No network or daemon
    contact is performed.

    Requires a declared transport profile (either transport_profile_declared
    or transport_verified). Pure catalog-only aliases without declared profiles
    fail closed. Scientific Level-32 execution maintains a separate, stricter
    treatment qualification gate (is_treatment_eligible).
    """

    import sys as _sys
    from pathlib import Path as _Path

    from agentic_debugger.evaluation.live import LiveModelConfig as _LiveModelConfig

    spec = resolve_cloud_model(alias)
    if not spec.transport_profile_declared and not spec.transport_verified:
        raise OllamaAdapterError("selected Ollama Cloud alias is not supported", kind="configuration")
    if type(logical_call_ceiling) is not int or isinstance(logical_call_ceiling, bool) or not 1 <= logical_call_ceiling <= 512:
        raise OllamaAdapterError("logical call ceiling is invalid", kind="configuration")
    idle = spec.idle_timeout_seconds if idle_timeout_seconds is None else idle_timeout_seconds
    req = spec.request_timeout_seconds if request_timeout_seconds is None else request_timeout_seconds
    idle = _validate_timeout_seconds(float(idle))
    req = _validate_timeout_seconds(float(req))
    root = _Path(__file__).resolve().parents[1]
    command: list[str] = [
        _sys.executable,
        str(root / "scripts" / "ollama_cloud_command_adapter.py"),
        "--model",
        spec.local_alias,
        "--timeout",
        str(int(idle)),
        "--max-logical-model-calls",
        str(int(logical_call_ceiling)),
        "--expected-version",
        EXPECTED_OLLAMA_VERSION,
    ]
    if int(req) != int(idle):
        command.extend(("--request-timeout", str(int(req))))
    if spec.thinking_level is not None:
        command.extend(("--thinking-level", spec.thinking_level))
    return _LiveModelConfig(
        model_name=spec.local_alias,
        command=tuple(command),
        request_timeout_seconds=float(req),
        tool_version="ollama-cloud-adapter-v1.3-local",
    )


def main() -> None:
    raise SystemExit(run_adapter())


if __name__ == "__main__":
    main()
