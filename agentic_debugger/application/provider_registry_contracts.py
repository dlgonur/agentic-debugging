"""Provider registry contracts: kinds, routes, display names, catalog bounds.

This module owns the static provider registry vocabulary beneath the
model-provider surface: explicit route identities, provider kind
constants and labels, the known-model display map and formatting, the
bounded default model catalogs, CLI executable candidates, listing
bounds, and the direct-API transport constants, together with the
fail-closed :class:`ProviderRegistryError` and the immutable
:class:`ProviderModel` record.

The availability/listing/resolution chain (whose module-attribute
monkeypatch seams are load-bearing for tests) stays in
:mod:`agentic_debugger.application.model_providers`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

ROUTE_DIRECT_API = "direct_api"
ROUTE_LEGACY_CLI = "legacy_cli"

PROVIDER_KIND_OLLAMA = "ollama_cloud"
PROVIDER_KIND_OPENCODE = "opencode_go"
PROVIDER_KIND_COMMANDCODE = "commandcode_goat"
PROVIDER_KIND_CONFIGURED = "configured"

PROVIDER_KINDS = (
    PROVIDER_KIND_OLLAMA,
    PROVIDER_KIND_OPENCODE,
    PROVIDER_KIND_COMMANDCODE,
    PROVIDER_KIND_CONFIGURED,
)

_PROVIDER_LABELS = {
    PROVIDER_KIND_OLLAMA: "Ollama Cloud",
    PROVIDER_KIND_OPENCODE: "OpenCode Go",
    PROVIDER_KIND_COMMANDCODE: "CommandCode GOAT",
    PROVIDER_KIND_CONFIGURED: "Custom command profile",
}

_KNOWN_MODEL_MAP: Mapping[str, str] = {
    # DeepSeek
    "deepseekv4flash": "DeepSeek V4 Flash",
    "deepseekv4pro": "DeepSeek V4 Pro",
    # GLM
    "glm5.1": "GLM 5.1",
    "glm5.2": "GLM 5.2",
    "glm5.2fast": "GLM 5.2 Fast",
    "glm5.3": "GLM 5.3",
    "glm5.3flash": "GLM 5.3 Flash",
    # GPT-OSS / GPT
    "gptoss20b": "GPT-OSS 20B",
    "gptoss120b": "GPT-OSS 120B",
    "gpt5.6sol": "GPT-5.6 Sol",
    "gpt5.6terra": "GPT-5.6 Terra",
    # Kimi
    "kimik2.6": "Kimi K2.6",
    "kimik2.7code": "Kimi K2.7 Code",
    "kimik3": "Kimi K3",
    # MiniMax
    "minimaxm2.7": "MiniMax M2.7",
    "minimaxm3": "MiniMax M3",
    # Nemotron
    "nemotron3nano30b": "Nemotron 3 Nano 30B",
    "nemotron3super": "Nemotron 3 Super",
    "nemotron3ultra": "Nemotron 3 Ultra",
    # Qwen
    "qwen3.5": "Qwen 3.5",
    "qwen3.8max": "Qwen 3.8 Max",
    # Gemma
    "gemma431b": "Gemma 4 31B",
    # Mistral
    "mistrallarge3675b": "Mistral Large 3 675B",
    # MiMo
    "mimov2.5pro": "MiMo V2.5 Pro",
    # Grok
    "grok4.6": "Grok 4.6",
}


def _normalize_key(s: str) -> str:
    return s.strip().lower().replace("-", "").replace("_", "").replace(" ", "").replace(":", "")


def format_model_display_name(raw: str) -> str:
    """Format any model identifier or alias into a clean human-readable name.

    Examples:
        deepseek-v4-flash:cloud -> DeepSeek V4 Flash
        opencode-go/deepseek-v4-pro -> DeepSeek V4 Pro
        zai-org/glm-5.2 -> GLM 5.2
        gpt-oss:20b-cloud -> GPT-OSS 20B
        nemotron-3-super:cloud -> Nemotron 3 Super
    """
    if not raw or raw.strip().lower() in ("offline", ""):
        return "Offline"
    text = raw.strip()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    if text.endswith(":cloud"):
        text = text[:-6]
    elif text.endswith("-cloud"):
        text = text[:-6]

    norm = _normalize_key(text)
    if norm in _KNOWN_MODEL_MAP:
        return _KNOWN_MODEL_MAP[norm]

    acronyms = {
        "gpt": "GPT",
        "oss": "OSS",
        "glm": "GLM",
        "llm": "LLM",
        "v1": "V1",
        "v2": "V2",
        "v3": "V3",
        "v4": "V4",
        "v5": "V5",
        "v6": "V6",
        "moe": "MoE",
        "ai": "AI",
        "pdb": "PDB",
        "cli": "CLI",
        "deepseek": "DeepSeek",
        "minimax": "MiniMax",
        "mimo": "MiMo",
    }
    parts = []
    for chunk in text.replace("_", " ").replace("-", " ").replace(":", " ").split():
        lower = chunk.lower()
        if lower in acronyms:
            parts.append(acronyms[lower])
        elif lower.endswith("b") and lower[:-1].isdigit():
            parts.append(f"{lower[:-1]}B")
        else:
            parts.append(chunk.capitalize())
    return " ".join(parts) or text


#: Curated presentation defaults captured from the live GOAT catalog
#: (2026-08-28).  Any plan model id remains accepted at the adapter.
_COMMANDCODE_DEFAULT_MODELS: Tuple[str, ...] = (
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "zai-org/glm-5.2",
    "zai-org/glm-5.2-fast",
    "moonshotai/kimi-k3",
    "qwen/qwen3.8-max",
    "minimaxai/minimax-m3",
    "xiaomi/mimo-v2.5-pro",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
)

#: Curated presentation defaults captured from the live OpenCode Go
#: catalog (2026-08-28).
_OPENCODE_DEFAULT_MODELS: Tuple[str, ...] = (
    "opencode-go/deepseek-v4-flash",
    "opencode-go/deepseek-v4-pro",
    "opencode-go/glm-5.3",
    "opencode-go/glm-5.2",
    "opencode-go/kimi-k3",
    "opencode-go/kimi-k2.7-code",
    "opencode-go/qwen3.8-max",
    "opencode-go/grok-4.6",
    "opencode-go/minimax-m3",
)

#: Curated presentation defaults for general-runtime Ollama Cloud when
#: unrefreshed (live catalog discovery is preferred).
_OLLAMA_DEFAULT_MODELS: Tuple[str, ...] = (
    "deepseek-v4-flash:cloud",
    "deepseek-v4-pro:cloud",
    "glm-5.3-flash:cloud",
    "glm-5.2:cloud",
    "gpt-oss:20b-cloud",
    "gpt-oss:120b-cloud",
    "kimi-k3:cloud",
    "minimax-m3:cloud",
    "qwen3.8-max:cloud",
)

# Actual CommandCode executable names only; the Windows system shell
# (cmd.exe) must never be treated as the CommandCode CLI.
_COMMANDCODE_CLI_CANDIDATES = ("cmdc", "command-code", "commandcode")
_MAX_MODELS_LISTED = 128

#: Direct-API adapter construction defaults (bounded, explicit).
_DIRECT_API_DEFAULT_TIMEOUT_SECONDS = 300.0
_DIRECT_API_TOOL_VERSION = "provider-direct-api-adapter-v1"


class ProviderRegistryError(RuntimeError):
    """Fail-closed provider registry error (never carries credentials)."""


@dataclass(frozen=True)
class ProviderModel:
    """One selectable model offered by one provider."""

    kind: str
    model_id: str
    display_name: str
    provider_label: str
    available: bool
    unavailable_reason: Optional[str] = None
    note: Optional[str] = None
