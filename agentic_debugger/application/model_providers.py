"""Unified model-provider registry for the application runtime.

One place answers: which model providers can this machine serve, which
models do they offer, and how does a live session construct their
transport configuration.

Supported providers:

- ``ollama_cloud``  — the repository-owned general Ollama Cloud catalog;
  scientific qualification is a separate capability-ladder contract.
- ``opencode_go``   — the operator's OpenCode Go subscription.  The
  general catalog is discovered from the provider's live ``/models``
  endpoint (:mod:`agentic_debugger.application.provider_connections`);
  execution prefers the direct provider API (``scripts/provider_direct_api_adapter.py``)
  and keeps the verified local OpenCode CLI
  (``scripts/opencode_provider_adapter.py``) as the explicit legacy route.
- ``commandcode_goat`` — the operator's Command Code GOAT plan, with the
  same direct-API-first routing over the CommandCode Provider API and
  the local CommandCode CLI as the explicit legacy route.
- Custom configured providers — user-configured direct-API endpoints
  stored in persistent provider configurations.
- ``configured``    — the existing app-owned command-model profile store.

Design rules:

- Availability probes are read-only and offline: they check operator
  auth stores / executables by presence, never contacting a provider,
  never reading credential bytes, never printing credential material.
- Discovered catalogs are presentation/catalog data only (model
  identities and protocol metadata are not secrets).  The curated
  default model lists below remain the offline fail-safe when no
  catalog has been refreshed yet; they are not scientific rosters.
- Transport construction is fail-closed and provider-owned.  The route
  is an explicit, deterministic decision recorded in provenance:
  ``direct_api`` when the model's protocol family is resolved and a
  credential source exists, otherwise ``legacy_cli`` when the provider
  CLI is available.  There is no runtime fallback between routes.
- A dynamically discovered model never becomes scientifically
  selectable: capability-ladder eligibility is untouched by discovery.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

from agentic_debugger.application.provider_connections import (
    DIRECT_API_PROVIDER_KINDS,
    ProviderConnectionError,
    credential_source_for,
    get_provider_config,
    is_known_provider,
    list_configured_providers,
    load_cached_catalog,
    provider_api_model_id,
    provider_base_url,
    provider_environment_variable,
    resolve_model_protocol,
)

__all__ = [
    "PROVIDER_KIND_COMMANDCODE",
    "PROVIDER_KIND_CONFIGURED",
    "PROVIDER_KIND_OLLAMA",
    "PROVIDER_KIND_OPENCODE",
    "PROVIDER_KINDS",
    "ROUTE_DIRECT_API",
    "ROUTE_LEGACY_CLI",
    "ProviderModel",
    "ProviderRegistryError",
    "format_model_display_name",
    "list_live_models",
    "list_provider_models",
    "provider_availability",
    "provider_transport_environment",
    "resolve_provider_live_config",
]

#: Explicit route identities recorded in durable session provenance.
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
    """Format any model identifier or alias into a clean human-readable name."""
    if not raw or not isinstance(raw, str):
        return raw

    s = raw.strip()
    if s.endswith(":cloud"):
        s = s[:-6]
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    key = _normalize_key(s)
    if key in _KNOWN_MODEL_MAP:
        return _KNOWN_MODEL_MAP[key]

    parts = s.replace("_", "-").split("-")
    formatted_parts: List[str] = []
    for p in parts:
        p_lower = p.lower()
        if p_lower in ("glm", "gpt", "oss", "v4", "v3", "v2", "v1", "ai", "pro", "exp"):
            formatted_parts.append(p.upper())
        elif p_lower.startswith("v") and p[1:].isdigit():
            formatted_parts.append(p.upper())
        elif p_lower in ("flash", "deepseek", "kimi", "minimax", "qwen", "mistral", "gemma", "nemotron"):
            formatted_parts.append(p.capitalize())
        else:
            formatted_parts.append(p.capitalize())
    return " ".join(formatted_parts)


@dataclass(frozen=True)
class ProviderModel:
    """One model entry exposed by a provider registry."""

    kind: str
    model_id: str
    display_name: str
    provider_label: str
    available: bool
    unavailable_reason: Optional[str] = None
    note: Optional[str] = None


class ProviderRegistryError(RuntimeError):
    """Fail-closed registry error with credential-safe text."""


_OPENCODE_DEFAULT_MODELS: Tuple[str, ...] = (
    "opencode-go/glm-5.3-flash",
    "opencode-go/kimi-k3",
    "opencode-go/minimax-m3",
    "opencode-go/deepseek-v4-flash",
    "opencode-go/qwen3.8-max",
    "opencode-go/gpt-5.6-luna",
)

_COMMANDCODE_DEFAULT_MODELS: Tuple[str, ...] = (
    "deepseek/deepseek-v4-flash",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-haiku-4.5",
    "moonshotai/Kimi-K3",
    "google/gemini-3.7-flash",
    "zai-org/glm-5.2",
    "gpt-5.6-sol",
)


def _ollama_availability() -> Tuple[bool, Optional[str]]:
    from scripts.ollama_cloud_command_adapter import (
        OLLAMA_API_KEY_ENV_VAR,
        resolve_ollama_credential,
    )

    credential = resolve_ollama_credential()
    if credential:
        return True, None
    return (
        False,
        f"{OLLAMA_API_KEY_ENV_VAR} is unset and no ~/.ollama/id_* key is readable",
    )


def _opencode_availability() -> Tuple[bool, Optional[str]]:
    from agentic_debugger.application.provider_connections import (
        opencode_auth_store_path,
    )

    env_var = provider_environment_variable(PROVIDER_KIND_OPENCODE)
    if env_var and os.environ.get(env_var):
        return True, None
    if credential_source_for(PROVIDER_KIND_OPENCODE) is not None:
        return True, None
    auth_file = opencode_auth_store_path()
    if auth_file.is_file():
        return True, None
    if shutil.which("opencode") is not None:
        return True, None
    return (
        False,
        f"neither direct API credential ({env_var or 'API key'}) nor local "
        f"opencode CLI ({auth_file} / PATH) was found",
    )


def _commandcode_availability() -> Tuple[bool, Optional[str]]:
    env_var = provider_environment_variable(PROVIDER_KIND_COMMANDCODE)
    if env_var and os.environ.get(env_var):
        return True, None
    if credential_source_for(PROVIDER_KIND_COMMANDCODE) is not None:
        return True, None
    if shutil.which("commandcode") is not None:
        return True, None
    return (
        False,
        f"neither direct API credential ({env_var or 'API key'}) nor local "
        "commandcode CLI was found",
    )


def provider_availability(kind: str) -> Tuple[bool, Optional[str]]:
    """Offline presence check for one provider's readiness."""
    if kind == PROVIDER_KIND_OLLAMA:
        return _ollama_availability()
    if kind == PROVIDER_KIND_OPENCODE:
        return _opencode_availability()
    if kind == PROVIDER_KIND_COMMANDCODE:
        return _commandcode_availability()
    if is_known_provider(kind):
        source = credential_source_for(kind)
        return (
            source is not None,
            None if source else "No API credential — connect in Model Providers (press m)",
        )
    return False, f"unknown provider {kind!r}"


def list_provider_models(
    *,
    include_ollama: bool = True,
    ollama_limit: int = 32,
) -> List[ProviderModel]:
    """Grouped, availability-annotated model summaries for pickers."""
    models: List[ProviderModel] = []
    if include_ollama:
        from scripts.ollama_cloud_command_adapter import CLOUD_MODELS

        for spec in sorted(CLOUD_MODELS.values(), key=lambda item: item.local_alias)[:ollama_limit]:
            is_runnable = bool(spec.transport_profile_declared or spec.transport_verified)
            unavailable_reason = (
                None
                if is_runnable
                else "Catalog entry only: no transport profile declared for local execution"
            )
            models.append(
                ProviderModel(
                    kind=PROVIDER_KIND_OLLAMA,
                    model_id=spec.local_alias,
                    display_name=format_model_display_name(spec.local_alias),
                    provider_label=_PROVIDER_LABELS[PROVIDER_KIND_OLLAMA],
                    available=is_runnable,
                    unavailable_reason=unavailable_reason,
                )
            )

    for cfg in list_configured_providers():
        if not cfg.enabled:
            continue
        discovered = _discovered_provider_models(cfg.provider_id)
        if discovered is not None:
            models.extend(discovered)
        elif cfg.is_builtin:
            models.extend(_subscription_models(cfg.provider_id))
        elif cfg.models:
            direct_ok = credential_source_for(cfg.provider_id) is not None
            for m in cfg.models:
                models.append(
                    ProviderModel(
                        kind=cfg.provider_id,
                        model_id=m.model_id,
                        display_name=m.display_name,
                        provider_label=cfg.name,
                        available=direct_ok,
                        unavailable_reason=None if direct_ok else "No direct API credential — connect in Model Providers (press m)",
                        note=f"direct API · {m.protocol or cfg.api_format}",
                    )
                )

    return models


def _subscription_models(kind: str) -> List[ProviderModel]:
    """Discovered catalog entries when available, curated defaults otherwise."""
    discovered = _discovered_provider_models(kind)
    if discovered is not None:
        return discovered
    if kind == PROVIDER_KIND_OPENCODE:
        available, reason = _opencode_availability()
        model_ids: Tuple[str, ...] = _OPENCODE_DEFAULT_MODELS
    else:
        available, reason = _commandcode_availability()
        model_ids = _COMMANDCODE_DEFAULT_MODELS
    label = _PROVIDER_LABELS.get(kind, kind)
    return [
        ProviderModel(
            kind=kind,
            model_id=model_id,
            display_name=format_model_display_name(model_id),
            provider_label=label,
            available=available,
            unavailable_reason=reason,
        )
        for model_id in model_ids
    ]


def _discovered_provider_models(kind: str) -> Optional[List[ProviderModel]]:
    """Picker entries from the cached discovered catalog (or ``None``)."""
    if kind not in DIRECT_API_PROVIDER_KINDS and not is_known_provider(kind):
        return None
    try:
        snapshot = load_cached_catalog(kind)
    except ProviderConnectionError:
        return None
    if snapshot is None or not snapshot.models:
        return None
    direct_ok = credential_source_for(kind) is not None
    cfg = get_provider_config(kind)
    label = cfg.name if cfg else _PROVIDER_LABELS.get(kind, kind)

    if kind == PROVIDER_KIND_OPENCODE:
        legacy_ok, legacy_reason = _opencode_availability()
    elif kind == PROVIDER_KIND_COMMANDCODE:
        legacy_ok, legacy_reason = _commandcode_availability()
    else:
        legacy_ok, legacy_reason = False, None

    models: List[ProviderModel] = []
    for item in snapshot.models:
        note: Optional[str] = None
        if item.protocol is not None:
            available = direct_ok or legacy_ok
            unavailable_reason: Optional[str] = None
            if not available:
                unavailable_reason = (
                    f"{legacy_reason}; no direct API credential — connect in "
                    "Model Providers (press m)"
                    if legacy_reason
                    else "no direct API credential — connect in Model Providers (press m)"
                )
            note = f"direct API · {item.protocol}"
        else:
            available = legacy_ok
            if available:
                unavailable_reason = None
                note = "direct API: protocol not yet resolved"
            elif legacy_reason:
                unavailable_reason = (
                    f"{item.unavailable_reason}; {legacy_reason}"
                )
                note = "direct API: protocol not yet resolved"
            else:
                unavailable_reason = item.unavailable_reason
        models.append(
            ProviderModel(
                kind=kind,
                model_id=item.model_id,
                display_name=item.display_name,
                provider_label=label,
                available=available,
                unavailable_reason=unavailable_reason,
                note=note,
            )
        )
    return models


def list_live_models(kind: str) -> List[str]:
    """Legacy helper returning bare model ID strings."""
    return [model.model_id for model in list_provider_models() if model.kind == kind]


_DIRECT_API_TOOL_VERSION = "provider-direct-api-adapter-v1"


def _direct_api_live_config(
    kind: str,
    model_id: str,
    protocol: str,
    *,
    logical_call_ceiling: int,
    request_timeout_seconds: Optional[float],
) -> Tuple[Any, Mapping[str, Any]]:
    """(LiveModelConfig, provenance) for the direct API transport route."""
    adapter = (
        Path(__file__).resolve().parent.parent.parent
        / "scripts"
        / "provider_direct_api_adapter.py"
    )
    if not adapter.is_file():
        raise ProviderRegistryError(
            f"direct API adapter script is missing at {adapter}"
        )
    request_timeout = (
        float(request_timeout_seconds)
        if request_timeout_seconds is not None
        else 60.0
    )
    api_model_id = provider_api_model_id(kind, model_id)
    cfg = get_provider_config(kind)
    disp = model_id.rsplit("/", 1)[-1] if "/" in model_id else model_id
    if cfg is not None:
        for m in cfg.models:
            if m.model_id == model_id and m.display_name:
                disp = m.display_name
                break

    command = [
        sys.executable,
        str(adapter),
        "--provider", kind,
        "--model", api_model_id,
        "--protocol", protocol,
        "--request-timeout-seconds", str(request_timeout),
        "--max-logical-model-calls", str(int(logical_call_ceiling)),
    ]
    from agentic_debugger.evaluation.live import LiveModelConfig as _LiveModelConfig

    config = _LiveModelConfig(
        model_name=model_id,
        command=tuple(command),
        request_timeout_seconds=request_timeout,
        tool_version=_DIRECT_API_TOOL_VERSION,
    )
    return config, {
        "provider": kind,
        "profile_id": model_id,
        "display_name": disp,
        "protocol_version": "1.3",
        "tool_version": config.tool_version,
        "route": ROUTE_DIRECT_API,
        "api_protocol": protocol,
        "provider_model_id": api_model_id,
        "endpoint": provider_base_url(kind),
    }


def resolve_provider_live_config(
    kind: str,
    model_id: str,
    *,
    logical_call_ceiling: int = 32,
    request_timeout_seconds: Optional[float] = None,
) -> Tuple[Any, Mapping[str, Any]]:
    """(LiveModelConfig, provenance payload) for one provider model."""
    if kind not in PROVIDER_KINDS and not is_known_provider(kind):
        raise ProviderRegistryError(f"unknown provider kind: {kind!r}")
    if type(model_id) is not str or not model_id.strip():
        raise ProviderRegistryError("model_id must be a non-empty string")
    if kind == PROVIDER_KIND_OLLAMA:
        from scripts.ollama_cloud_command_adapter import (
            OllamaAdapterError,
            build_ollama_live_config,
        )

        try:
            config = build_ollama_live_config(
                model_id,
                logical_call_ceiling=logical_call_ceiling,
                request_timeout_seconds=request_timeout_seconds,
            )
        except OllamaAdapterError as exc:
            raise ProviderRegistryError(str(exc)) from exc
        return config, {
            "provider": kind,
            "profile_id": model_id,
            "display_name": model_id,
            "protocol_version": "1.3",
            "tool_version": config.tool_version,
        }
    if kind in DIRECT_API_PROVIDER_KINDS or is_known_provider(kind):
        return _resolve_subscription_live_config(
            kind,
            model_id,
            logical_call_ceiling=logical_call_ceiling,
            request_timeout_seconds=request_timeout_seconds,
        )
    raise ProviderRegistryError(
        "configured profiles resolve through CommandModelConfigStore, not the provider registry"
    )


def _resolve_subscription_live_config(
    kind: str,
    model_id: str,
    *,
    logical_call_ceiling: int,
    request_timeout_seconds: Optional[float],
) -> Tuple[Any, Mapping[str, Any]]:
    """Direct-API-first route resolution for built-in and configured providers."""
    protocol: Optional[str]
    try:
        protocol = resolve_model_protocol(kind, model_id)
    except ProviderConnectionError as exc:
        raise ProviderRegistryError(str(exc)) from exc
    direct_ok = credential_source_for(kind) is not None
    if kind == PROVIDER_KIND_OPENCODE:
        legacy_ok, legacy_reason = _opencode_availability()
    elif kind == PROVIDER_KIND_COMMANDCODE:
        legacy_ok, legacy_reason = _commandcode_availability()
    else:
        legacy_ok, legacy_reason = False, None

    cfg = get_provider_config(kind)
    label = cfg.name if cfg else _PROVIDER_LABELS.get(kind, kind)

    if protocol is not None and direct_ok:
        return _direct_api_live_config(
            kind,
            model_id,
            protocol,
            logical_call_ceiling=logical_call_ceiling,
            request_timeout_seconds=request_timeout_seconds,
        )
    if legacy_ok:
        return _legacy_cli_live_config(
            kind,
            model_id,
            logical_call_ceiling=logical_call_ceiling,
            request_timeout_seconds=request_timeout_seconds,
        )
    if protocol is None:
        raise ProviderRegistryError(
            f"{label} model {model_id!r} has no resolved direct-API "
            "protocol and the legacy CLI route is unavailable "
            f"({legacy_reason or 'unknown reason'})"
        )
    raise ProviderRegistryError(
        f"{label} has no usable credential source for the direct "
        "API route and the legacy CLI route is unavailable "
        f"({legacy_reason or 'unknown reason'}); connect in Model Providers "
        "(press m from Home or Session Setup)"
    )


def _legacy_cli_live_config(
    kind: str,
    model_id: str,
    *,
    logical_call_ceiling: int,
    request_timeout_seconds: Optional[float],
) -> Tuple[Any, Mapping[str, Any]]:
    """(LiveModelConfig, provenance) for the explicit legacy CLI route."""
    if kind == PROVIDER_KIND_OPENCODE:
        from scripts.opencode_provider_adapter import (
            OpenCodeProviderAdapterError,
            build_opencode_live_config,
        )

        try:
            config = build_opencode_live_config(
                model_id,
                logical_call_ceiling=logical_call_ceiling,
                request_timeout_seconds=request_timeout_seconds,
            )
        except OpenCodeProviderAdapterError as exc:
            raise ProviderRegistryError(str(exc)) from exc
    elif kind == PROVIDER_KIND_COMMANDCODE:
        from scripts.commandcode_goat_adapter import (
            CommandCodeAdapterError,
            build_commandcode_live_config,
        )

        try:
            config = build_commandcode_live_config(
                model_id,
                logical_call_ceiling=logical_call_ceiling,
                request_timeout_seconds=request_timeout_seconds,
            )
        except CommandCodeAdapterError as exc:
            raise ProviderRegistryError(str(exc)) from exc
    else:
        raise ProviderRegistryError(f"provider {kind!r} has no legacy CLI route")

    return config, {
        "provider": kind,
        "profile_id": model_id,
        "display_name": model_id.rsplit("/", 1)[-1] if "/" in model_id else model_id,
        "protocol_version": "1.3",
        "tool_version": config.tool_version,
        "route": ROUTE_LEGACY_CLI,
    }


def provider_session_credential_environment(
    kind: str,
) -> Optional[Mapping[str, str]]:
    """Session-key-only child-environment override (UI -> worker hop)."""
    from agentic_debugger.application.provider_connections import (
        provider_session_credential_environment as build_environment,
    )

    return build_environment(kind)


def provider_transport_environment(kind: str) -> Optional[Mapping[str, str]]:
    """Bounded child-environment overrides for one provider transport."""
    from agentic_debugger.application.provider_connections import (
        provider_transport_credential_environment,
    )

    return provider_transport_credential_environment(kind)
