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
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

try:
    import protocol_prompt_shaper as prompt_shaper
except ImportError:  # pragma: no cover - bare-child import path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import protocol_prompt_shaper as prompt_shaper


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
    # Provider-neutral shaping authority: same ceiling, typed adapter error.
    try:
        return prompt_shaper.canonical_public_request(request)
    except prompt_shaper.ProtocolPromptError as exc:
        raise OllamaAdapterError(str(exc), kind=exc.kind) from None


#: Provider-neutral prompt shaping, imported not duplicated.  The mature
#: ladder-facing semantics (system instruction, legal directive forms,
#: request-specific action/transition/hypothesis/diagnosis representations)
#: live in one authority shared by every model transport.
SYSTEM_PROMPT = prompt_shaper.SYSTEM_PROMPT
PUBLIC_REQUEST_START = prompt_shaper.PUBLIC_REQUEST_START
PUBLIC_REQUEST_END = prompt_shaper.PUBLIC_REQUEST_END
NEUTRAL_UNIFIED_DIFF_EXAMPLE = prompt_shaper.NEUTRAL_UNIFIED_DIFF_EXAMPLE
OLD_COUNT_FORMULA = prompt_shaper.OLD_COUNT_FORMULA
NEW_COUNT_FORMULA = prompt_shaper.NEW_COUNT_FORMULA
APPLY_PATCH_DIRECTIVE_SHAPE = prompt_shaper.APPLY_PATCH_DIRECTIVE_SHAPE

#: Explicit prompt-profile identity for this transport.
#: Qualified scientific Ollama ladder and Level-32 use the frozen profile
#: to preserve byte-for-byte pre-9fab308 provenance.
OLLAMA_PROMPT_PROFILE = prompt_shaper.PromptProfile.FROZEN_SCIENTIFIC_V1
PROMPT_PROFILE = OLLAMA_PROMPT_PROFILE

illustrative_action_directive = prompt_shaper.illustrative_action_directive


def build_apply_patch_guidance(request: Mapping[str, Any]) -> str:  # type: ignore[override]
    try:
        return prompt_shaper.build_apply_patch_guidance(
            request, prompt_profile=prompt_shaper.PromptProfile.FROZEN_SCIENTIFIC_V1
        )
    except prompt_shaper.ProtocolPromptError as exc:
        raise OllamaAdapterError(str(exc), kind=exc.kind) from None


def build_system_instructions(request: Mapping[str, Any]) -> str:  # type: ignore[override]
    try:
        return prompt_shaper.build_system_instructions(
            request, prompt_profile=prompt_shaper.PromptProfile.FROZEN_SCIENTIFIC_V1
        )
    except prompt_shaper.ProtocolPromptError as exc:
        raise OllamaAdapterError(str(exc), kind=exc.kind) from None


def build_request_guidance(request: Mapping[str, Any]) -> str:  # type: ignore[override]
    try:
        return prompt_shaper.build_request_guidance(
            request, prompt_profile=prompt_shaper.PromptProfile.FROZEN_SCIENTIFIC_V1
        )
    except prompt_shaper.ProtocolPromptError as exc:
        raise OllamaAdapterError(str(exc), kind=exc.kind) from None


def build_user_protocol_message(request: Mapping[str, Any]) -> str:  # type: ignore[override]
    try:
        return prompt_shaper.build_user_protocol_message(
            request, prompt_profile=prompt_shaper.PromptProfile.FROZEN_SCIENTIFIC_V1
        )
    except prompt_shaper.ProtocolPromptError as exc:
        raise OllamaAdapterError(str(exc), kind=exc.kind) from None


def _directive_fields_match_validator() -> bool:
    return prompt_shaper.directive_fields_match_validator(DIRECTIVE_TOP_LEVEL_FIELDS)


if not _directive_fields_match_validator():
    raise RuntimeError("directive prompt field names drifted from the adapter validator")


def build_chat_messages(request: Mapping[str, Any]) -> list[dict[str, str]]:
    try:
        return prompt_shaper.build_chat_messages(
            request, prompt_profile=prompt_shaper.PromptProfile.FROZEN_SCIENTIFIC_V1
        )
    except prompt_shaper.ProtocolPromptError as exc:
        raise OllamaAdapterError(str(exc), kind=exc.kind) from None


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
