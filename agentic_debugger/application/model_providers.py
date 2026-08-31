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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

from agentic_debugger.application.provider_connections import (
    DIRECT_API_PROVIDER_KINDS,
    ProviderConnectionError,
    credential_source_for,
    load_cached_catalog,
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
    """Format any model identifier or alias into a clean human-readable name.

    Examples:
        deepseek-v4-flash:cloud -> DeepSeek V4 Flash
        opencode-go/deepseek-v4-pro -> DeepSeek V4 Pro
        zai-org/glm-5.2 -> GLM 5.2
        gpt-oss:120b-cloud -> GPT-OSS 120B
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
    #: Informational (non-blocking) detail, e.g. the resolved direct-API
    #: protocol family for a discovered catalog entry.
    note: Optional[str] = None


def _commandcode_auth_store_path() -> Path:
    return Path.home() / ".commandcode" / "auth.json"


def _opencode_auth_store_path() -> Path:
    # Same location contract as the frozen OpenCode adapter's auth state.
    profile = os.environ.get("OPENCODE_CONFIG_DIR") or Path.home()
    return Path(profile) / ".local" / "share" / "opencode" / "auth.json"


def _first_on_path(candidates: Tuple[str, ...]) -> Optional[str]:
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None


def _commandcode_availability() -> Tuple[bool, Optional[str]]:
    """GOAT readiness = operator auth store (or env key) AND local CLI.

    Presence checks only: the key bytes are never read here.
    """

    environment_var = provider_environment_variable(PROVIDER_KIND_COMMANDCODE)
    if not (
        _commandcode_auth_store_path().is_file()
        or bool(environment_var and os.environ.get(environment_var))
    ):
        return False, (
            "Command Code auth store not found "
            f"(~/.commandcode/auth.json or {environment_var})"
        )
    if _first_on_path(_COMMANDCODE_CLI_CANDIDATES) is None:
        return False, "CommandCode CLI not found (expected cmdc/command-code/commandcode on PATH)"
    return True, None


def _opencode_availability() -> Tuple[bool, Optional[str]]:
    if not _opencode_auth_store_path().is_file():
        return False, "OpenCode auth store not found (~/.local/share/opencode/auth.json)"
    if shutil.which("opencode") is None:
        return False, "opencode CLI not found on PATH"
    return True, None


def _direct_connection_available(kind: str) -> Tuple[bool, Optional[str]]:
    """Whether the direct-API route is currently usable (presence-only).

    The answer never reads or emits credential bytes; a usable source is
    reported as a source label only.
    """

    from agentic_debugger.application.provider_connections import (
        CREDENTIAL_SOURCE_CLI_AUTH_STORE,
        CREDENTIAL_SOURCE_ENVIRONMENT,
        CREDENTIAL_SOURCE_SESSION_KEY,
        credential_source_for,
    )

    source = credential_source_for(kind)
    if source is None:
        return False, None
    if source == CREDENTIAL_SOURCE_SESSION_KEY:
        return True, None
    if source == CREDENTIAL_SOURCE_ENVIRONMENT:
        return True, None
    if source == CREDENTIAL_SOURCE_CLI_AUTH_STORE:
        return True, None
    return False, None


def provider_availability() -> List[Tuple[str, bool, Optional[str]]]:
    """(kind, available, reason) for each subscription provider.

    A provider is available when EITHER the direct-API route has a
    usable credential source OR the legacy CLI route is fully present
    (operator auth store + CLI).  The bounded reason names which side
    is missing; probes stay offline and credential-free.
    """

    def combined(
        direct: Tuple[bool, Optional[str]], legacy: Tuple[bool, Optional[str]]
    ) -> Tuple[bool, Optional[str]]:
        if direct[0] or legacy[0]:
            return True, None
        if legacy[1] is None:
            return False, "No usable direct-API credential source"
        return False, f"{legacy[1]}; no usable direct-API credential source"

    results: List[Tuple[str, bool, Optional[str]]] = [
        (PROVIDER_KIND_OLLAMA, True, None),
        (
            PROVIDER_KIND_OPENCODE,
            *combined(_direct_connection_available(PROVIDER_KIND_OPENCODE), _opencode_availability()),
        ),
        (
            PROVIDER_KIND_COMMANDCODE,
            *combined(_direct_connection_available(PROVIDER_KIND_COMMANDCODE), _commandcode_availability()),
        ),
    ]
    return results


def _display_name(model_id: str) -> str:
    return format_model_display_name(model_id)


def list_provider_models(
    *,
    include_ollama: bool = True,
    ollama_limit: int = 32,
) -> List[ProviderModel]:
    """Grouped, availability-annotated model summaries for pickers.

    Offline and read-only.  For the subscription providers the most
    recent valid discovered catalog is used when one has been refreshed
    (each entry annotated with its resolved direct-API protocol family
    or the bounded ``Protocol not yet resolved`` reason); otherwise the
    curated presentation defaults remain the offline fail-safe.  The
    Ollama Cloud group stays the repository-owned general catalog.
    Call :func:`list_live_models` for the legacy CLI listing and
    :func:`agentic_debugger.application.provider_connections.refresh_provider_catalog`
    for the explicit live refresh.
    """

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
    models.extend(_subscription_models(PROVIDER_KIND_OPENCODE))
    models.extend(_subscription_models(PROVIDER_KIND_COMMANDCODE))
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
    return [
        ProviderModel(
            kind=kind,
            model_id=model_id,
            display_name=format_model_display_name(model_id),
            provider_label=_PROVIDER_LABELS[kind],
            available=available,
            unavailable_reason=reason,
        )
        for model_id in model_ids
    ]


def _discovered_provider_models(kind: str) -> Optional[List[ProviderModel]]:
    """Picker entries from the cached discovered catalog (or ``None``).

    A cached snapshot is presented honestly: entries are the provider's
    current catalog identities; direct-API runnability follows the
    resolved protocol family; a selectable entry needs either a direct
    credential source or the legacy CLI route.  No cache (or a
    malformed one, which fails closed to absent) means the caller falls
    back to the curated presentation defaults.
    """

    if kind not in DIRECT_API_PROVIDER_KINDS:
        return None
    try:
        snapshot = load_cached_catalog(kind)
    except ProviderConnectionError:
        return None
    if snapshot is None or not snapshot.models:
        return None
    direct_ok = credential_source_for(kind) is not None
    if kind == PROVIDER_KIND_OPENCODE:
        legacy_ok, legacy_reason = _opencode_availability()
    else:
        legacy_ok, legacy_reason = _commandcode_availability()
    models: List[ProviderModel] = []
    for item in snapshot.models:
        note: Optional[str] = None
        if item.protocol is not None:
            available = direct_ok or legacy_ok
            unavailable_reason: Optional[str] = None
            if not available:
                unavailable_reason = (
                    f"{legacy_reason}; no direct API credential — connect in "
                    "Provider Connections (press c)"
                    if legacy_reason
                    else "no direct API credential — connect in Provider Connections (press c)"
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
                provider_label=_PROVIDER_LABELS[kind],
                available=available,
                unavailable_reason=unavailable_reason,
                note=note,
            )
        )
    return models


def list_live_models(kind: str) -> List[str]:
    """The operator's live plan catalog (explicit, provider-contacting).

    Read-only catalog listing; consumes no generation credits.  Only the
    subscription providers support this; Ollama Cloud exposes its curated
    roster offline and configured profiles are file-backed.
    """

    if kind == PROVIDER_KIND_COMMANDCODE:
        from scripts.commandcode_goat_adapter import CommandCodeAdapterError, run_list_models

        import io

        buffer = io.StringIO()
        try:
            run_list_models(buffer)
        except CommandCodeAdapterError as exc:
            raise ProviderRegistryError(str(exc)) from exc
        payload = buffer.getvalue()
        try:
            import json

            models = json.loads(payload).get("models")
        except (json.JSONDecodeError, AttributeError):
            raise ProviderRegistryError("model listing returned an invalid payload") from None
        if type(models) is not list:
            raise ProviderRegistryError("model listing returned an invalid payload")
        return [item for item in models if type(item) is str][:_MAX_MODELS_LISTED]
    if kind == PROVIDER_KIND_OPENCODE:
        executable = shutil.which("opencode")
        if executable is None:
            raise ProviderRegistryError("opencode CLI not found on PATH")
        import subprocess

        try:
            result = subprocess.run(
                [executable, "models"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderRegistryError("model listing timed out") from exc
        if result.returncode != 0:
            raise ProviderRegistryError(f"model listing failed: {result.stderr[:200]}")
        models = sorted(
            {
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip().startswith("opencode-go/")
            }
        )
        return models[:_MAX_MODELS_LISTED]
    raise ProviderRegistryError(f"provider {kind!r} has no live model listing")


def _direct_api_live_config(
    kind: str,
    model_id: str,
    protocol: str,
    *,
    logical_call_ceiling: int,
    request_timeout_seconds: Optional[float],
) -> Tuple[Any, Mapping[str, Any]]:
    """(LiveModelConfig, provenance) for the explicit direct-API route."""

    import sys as _sys

    from agentic_debugger.application.provider_connections import (
        provider_api_model_id,
        provider_base_url,
    )

    request_timeout = (
        _DIRECT_API_DEFAULT_TIMEOUT_SECONDS
        if request_timeout_seconds is None
        else float(request_timeout_seconds)
    )
    if not 1.0 <= request_timeout <= 3600.0:
        raise ProviderRegistryError(
            "request timeout must be within [1, 3600] seconds"
        )
    root = Path(__file__).resolve().parents[2]
    api_model_id = provider_api_model_id(kind, model_id)
    command = [
        _sys.executable,
        str(root / "scripts" / "provider_direct_api_adapter.py"),
        "--provider", kind,
        "--model", api_model_id,
        "--protocol", protocol,
        "--timeout", str(int(request_timeout)),
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
        "display_name": model_id.rsplit("/", 1)[-1] if "/" in model_id else model_id,
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
    """(LiveModelConfig, provenance payload) for one provider model.

    Fail-closed: unknown providers, unavailable providers, and invalid
    model identities raise :class:`ProviderRegistryError`.  The route is
    a deterministic pre-flight decision — ``direct_api`` when the model
    protocol family is resolved and a credential source exists,
    otherwise ``legacy_cli`` when the provider CLI is available — and is
    recorded in the provenance payload.  There is no runtime fallback
    between routes.  The provenance payload carries only provider
    identity fields, never credentials.
    """

    if kind not in PROVIDER_KINDS:
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
    if kind in DIRECT_API_PROVIDER_KINDS:
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
    """Direct-API-first route resolution for the two subscription providers."""

    protocol: Optional[str]
    try:
        protocol = resolve_model_protocol(kind, model_id)
    except ProviderConnectionError as exc:
        raise ProviderRegistryError(str(exc)) from exc
    direct_ok = credential_source_for(kind) is not None
    if kind == PROVIDER_KIND_OPENCODE:
        legacy_ok, legacy_reason = _opencode_availability()
    else:
        legacy_ok, legacy_reason = _commandcode_availability()
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
            f"{_PROVIDER_LABELS[kind]} model {model_id!r} has no resolved direct-API "
            "protocol and the legacy CLI route is unavailable "
            f"({legacy_reason or 'unknown reason'})"
        )
    raise ProviderRegistryError(
        f"{_PROVIDER_LABELS[kind]} has no usable credential source for the direct "
        "API route and the legacy CLI route is unavailable "
        f"({legacy_reason or 'unknown reason'}); connect in Provider Connections "
        "(press c from Session Setup)"
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
    else:
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
    """Session-key-only child-environment override (UI -> worker hop).

    Returns exactly one bounded variable/value pair when a process-local
    session key is the current credential source, else ``None``.
    """

    from agentic_debugger.application.provider_connections import (
        provider_session_credential_environment as build_environment,
    )

    return build_environment(kind)


def provider_transport_environment(kind: str) -> Optional[Mapping[str, str]]:
    """Bounded child-environment overrides for one provider transport.

    Forwards exactly one credential variable into the adapter child
    environment when a process-local session key or the provider's
    documented environment variable is the current credential source.
    Auth-store credentials need no forwarding: the adapter reads the
    store in place.  The mapping never contains more than one entry and
    never reaches evidence.
    """

    from agentic_debugger.application.provider_connections import (
        provider_transport_credential_environment,
    )

    return provider_transport_credential_environment(kind)
