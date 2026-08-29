"""Unified model-provider registry for the application runtime.

One place answers: which model providers can this machine serve, which
models do they offer, and how does a live session construct their
transport configuration.

Supported providers:

- ``ollama_cloud``  — the repository-owned Ollama Cloud roster (existing
  accepted product route, qualified aliases only).
- ``opencode_go``   — the operator's OpenCode Go subscription through the
  verified local OpenCode CLI (adapter: ``scripts/opencode_provider_adapter.py``).
- ``commandcode_goat`` — the operator's Command Code GOAT plan through the
  local CommandCode CLI (adapter: ``scripts/commandcode_goat_adapter.py``).
- ``configured``    — the existing app-owned command-model profile store.

Design rules:

- Availability probes are read-only and offline: they check operator
  auth stores / executables by presence, never contacting a provider,
  never reading credential bytes, never printing credential material.
- The curated default model lists below were captured from the live
  operator catalogs (2026-08-28) and are presentation conveniences only:
  any plan model id remains accepted; use the live listing helpers for
  the full current catalog.
- Transport construction is fail-closed and provider-owned: each
  provider resolves through its adapter's canonical builder so the
  product runtime and Local Project share one validated path.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

__all__ = [
    "PROVIDER_KIND_COMMANDCODE",
    "PROVIDER_KIND_CONFIGURED",
    "PROVIDER_KIND_OLLAMA",
    "PROVIDER_KIND_OPENCODE",
    "PROVIDER_KINDS",
    "ProviderModel",
    "ProviderRegistryError",
    "list_live_models",
    "list_provider_models",
    "provider_availability",
    "resolve_provider_live_config",
]

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

    if not (
        _commandcode_auth_store_path().is_file()
        or bool(os.environ.get("CMD_API_KEY"))
    ):
        return False, "Command Code auth store not found (~/.commandcode/auth.json or CMD_API_KEY)"
    if _first_on_path(_COMMANDCODE_CLI_CANDIDATES) is None:
        return False, "CommandCode CLI not found (expected cmdc/command-code/commandcode on PATH)"
    return True, None


def _opencode_availability() -> Tuple[bool, Optional[str]]:
    if not _opencode_auth_store_path().is_file():
        return False, "OpenCode auth store not found (~/.local/share/opencode/auth.json)"
    if shutil.which("opencode") is None:
        return False, "opencode CLI not found on PATH"
    return True, None


def provider_availability() -> List[Tuple[str, bool, Optional[str]]]:
    """(kind, available, reason) for each subscription provider."""

    results: List[Tuple[str, bool, Optional[str]]] = [
        (PROVIDER_KIND_OLLAMA, True, None),
        (PROVIDER_KIND_OPENCODE, *_opencode_availability()),
        (PROVIDER_KIND_COMMANDCODE, *_commandcode_availability()),
    ]
    return results


def _display_name(model_id: str) -> str:
    return model_id.rsplit("/", 1)[-1] if "/" in model_id else model_id


def list_provider_models(
    *,
    include_ollama: bool = True,
    ollama_limit: int = 32,
) -> List[ProviderModel]:
    """Grouped, availability-annotated model summaries for pickers.

    Offline and read-only: curated defaults for the subscription plans,
    the general catalog for Ollama Cloud.  Call :func:`list_live_models`
    for the operator's full current plan catalog.
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
                    display_name=spec.upstream_model,
                    provider_label=_PROVIDER_LABELS[PROVIDER_KIND_OLLAMA],
                    available=is_runnable,
                    unavailable_reason=unavailable_reason,
                )
            )
    opencode_ok, opencode_reason = _opencode_availability()
    for model_id in _OPENCODE_DEFAULT_MODELS:
        models.append(
            ProviderModel(
                kind=PROVIDER_KIND_OPENCODE,
                model_id=model_id,
                display_name=_display_name(model_id),
                provider_label=_PROVIDER_LABELS[PROVIDER_KIND_OPENCODE],
                available=opencode_ok,
                unavailable_reason=opencode_reason,
            )
        )
    commandcode_ok, commandcode_reason = _commandcode_availability()
    for model_id in _COMMANDCODE_DEFAULT_MODELS:
        models.append(
            ProviderModel(
                kind=PROVIDER_KIND_COMMANDCODE,
                model_id=model_id,
                display_name=_display_name(model_id),
                provider_label=_PROVIDER_LABELS[PROVIDER_KIND_COMMANDCODE],
                available=commandcode_ok,
                unavailable_reason=commandcode_reason,
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


def resolve_provider_live_config(
    kind: str,
    model_id: str,
    *,
    logical_call_ceiling: int = 32,
    request_timeout_seconds: Optional[float] = None,
) -> Tuple[Any, Mapping[str, Any]]:
    """(LiveModelConfig, provenance payload) for one provider model.

    Fail-closed: unknown providers, unavailable providers, and invalid
    model identities raise :class:`ProviderRegistryError`.  The provenance
    payload carries only provider identity fields, never credentials.
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
    if kind == PROVIDER_KIND_OPENCODE:
        available, reason = _opencode_availability()
        if not available:
            raise ProviderRegistryError(reason or "OpenCode provider unavailable")
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
        return config, {
            "provider": kind,
            "profile_id": model_id,
            "display_name": _display_name(model_id),
            "protocol_version": "1.3",
            "tool_version": config.tool_version,
        }
    if kind == PROVIDER_KIND_COMMANDCODE:
        available, reason = _commandcode_availability()
        if not available:
            raise ProviderRegistryError(reason or "CommandCode provider unavailable")
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
            "display_name": _display_name(model_id),
            "protocol_version": "1.3",
            "tool_version": config.tool_version,
        }
    raise ProviderRegistryError(
        "configured profiles resolve through CommandModelConfigStore, not the provider registry"
    )
