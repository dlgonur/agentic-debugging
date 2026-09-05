"""V2-03 ModelGateway and ModelBinding product runtime authority.

This module provides the single product/runtime seam for model providers:
the rest of Agentic Debugger requests a logical provider/model binding, and
:class:`ModelGateway` owns provider-runtime details beneath that boundary.

Architecture contract (ADR 0001 / V2 Plan §6.2, §9, §11 V2-03):

- :class:`ModelBinding` — immutable runtime/session provenance object containing
  only SAFE runtime facts (logical provider id, logical model id, API model id,
  effective protocol, endpoint contract / transport profile, resolved route,
  safe endpoint identity, auth mode, tool version, protocol version, and safe
  config fingerprint).  It NEVER carries credential values, API keys, CLI auth
  store contents, or secret material.
- :class:`ModelGateway` — the single product provider-runtime authority.
  Resolves logical requests into :class:`ModelBinding`, performs static runtime
  preflight, executes explicit reachability and catalog probes, inspects
  authoritative session history for runtime-success facts, and produces
  truthful :class:`ProviderStatusSnapshot` records.
- Status semantics: truthful, precise status facts.  Credential/config presence
  is NEVER reported as "Connected".  "Connected" / "Live verified" is reserved
  for explicit live probes.  `Runtime succeeded at T` is observational history
  derived from durable event history, never persisted as provider-config truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from agentic_debugger.application.events import (
    contains_credential_shape,
    MAX_IDENTIFIER_CHARS,
    MAX_SHORT_TEXT_CHARS,
)
from agentic_debugger.application.provider_connections import (
    AUTH_BEARER,
    AUTH_MODES,
    AUTH_NONE,
    CATALOG_DISABLED,
    CATALOG_OPENAI,
    DiscoveredProviderModel,
    ENDPOINT_CONTRACT_DISPLAY_LABELS,
    PROTOCOL_CHAT_COMPLETIONS,
    ProviderCatalogSnapshot,
    ProviderConfig,
    ProviderConnectionError,
    TRANSPORT_COMMANDCODE_GOAT,
    TRANSPORT_GENERIC,
    TRANSPORT_MODES,
    TRANSPORT_OLLAMA_CLOUD,
    TRANSPORT_OPENCODE_GO,
    _PROVIDER_CREDENTIAL_SOURCE_LABELS,
    credential_source_for,
    delete_cached_catalog,
    describe_transport_gap,
    effective_model_protocol,
    get_provider_config,
    is_known_provider,
    is_protocol_executable,
    is_provider_quarantined,
    list_configured_providers,
    load_cached_catalog,
    protocol_blocker_reason,
    provider_api_model_id,
    provider_base_url,
    provider_session_credential_environment,
    refresh_provider_catalog,
    resolve_model_protocol,
    resolve_runtime_credential,
    test_provider_connection,
)
from agentic_debugger.application.model_providers import (
    provider_transport_environment,
)

__all__ = [
    "CatalogProbeError",
    "CredentialUnavailableError",
    "EndpointUnreachableError",
    "IncompatibleModelError",
    "ModelBinding",
    "ModelGateway",
    "ModelGatewayError",
    "ModelRuntimeError",
    "ModelStaticPreflight",
    "ProtocolViolationError",
    "ProviderConfigurationError",
    "ProviderHttpRejectionError",
    "ProviderStatusSnapshot",
    "ROUTE_CONFIGURED_PROFILE",
    "ROUTE_DIRECT_API",
    "ROUTE_LEGACY_CLI",
    "ROUTE_OFFLINE",
    "ROUTE_QUALIFIED_LADDER",
    "StaleModelBindingError",
    "is_loopback_url",
    "provider_runtime_identity",
]

#: Explicit route identities recorded in ModelBinding and durable provenance.
ROUTE_DIRECT_API = "direct_api"
ROUTE_LEGACY_CLI = "legacy_cli"
ROUTE_CONFIGURED_PROFILE = "configured_profile"
ROUTE_OFFLINE = "offline"
ROUTE_QUALIFIED_LADDER = "qualified_ladder"

_DEFAULT_MAX_MODEL_REQUESTS = 64
_DEFAULT_MAX_CONTROLLER_STEPS = 128
_DEFAULT_MAX_RETRIES = 2
_MAX_MODEL_RESPONSE_BYTES = 32 * 1024


def is_loopback_url(url: Optional[str]) -> bool:
    """True only if the URL points to a loopback address (127.0.0.1, localhost, ::1)."""
    if not url:
        return False
    try:
        from urllib.parse import urlsplit

        hostname = urlsplit(url).hostname
        if not hostname:
            return False
        hostname = hostname.strip("[]").lower()
        return hostname in ("127.0.0.1", "localhost", "::1")
    except Exception:
        return False


def provider_runtime_identity(cfg: Any) -> Optional[str]:
    """Safe deterministic fingerprint representing current provider runtime contract.

    Requires complete explicit provider configuration provenance:
    all 5 safe facts (provider_id, base_url/endpoint, endpoint_contract/transport_profile,
    auth_mode, api_format) must be present, strings, and non-empty.
    If any fact is absent, non-string, or empty/whitespace, returns None.

    Excludes secret credentials, catalog timestamps, and mutable cached model list.
    """
    if cfg is None:
        return None

    if isinstance(cfg, dict):
        p_id = cfg.get("provider_id") or cfg.get("provider")
        b_url = cfg.get("base_url") or cfg.get("endpoint")
        e_contract = cfg.get("endpoint_contract") or cfg.get("transport_profile")
        a_mode = cfg.get("auth_mode")
        a_format = cfg.get("api_format")
    else:
        p_id = getattr(cfg, "provider_id", None) or getattr(cfg, "provider", None)
        b_url = getattr(cfg, "base_url", None) or getattr(cfg, "endpoint", None)
        e_contract = getattr(cfg, "transport_profile", None) or getattr(cfg, "endpoint_contract", None)
        a_mode = getattr(cfg, "auth_mode", None)
        a_format = getattr(cfg, "api_format", None)

    if not (
        isinstance(p_id, str)
        and isinstance(b_url, str)
        and isinstance(e_contract, str)
        and isinstance(a_mode, str)
        and isinstance(a_format, str)
    ):
        return None

    p_id = p_id.strip()
    b_url = b_url.strip().rstrip("/")
    e_contract = e_contract.strip()
    a_mode = a_mode.strip()
    a_format = a_format.strip()

    if not (p_id and b_url and e_contract and a_mode and a_format):
        return None

    raw = {
        "api_format": a_format,
        "auth_mode": a_mode,
        "base_url": b_url,
        "endpoint_contract": e_contract,
        "provider_id": p_id,
    }
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class ModelGatewayError(RuntimeError):
    """Base error for ModelGateway operations (never carries credentials)."""


class StaleModelBindingError(ModelGatewayError):
    """ModelBinding runtime identity does not match current provider/profile configuration."""


class ProviderConfigurationError(ModelGatewayError):
    """Provider configuration is invalid, missing, disabled, or quarantined."""


class CredentialUnavailableError(ModelGatewayError):
    """Required credential authority cannot be obtained or bound."""


class IncompatibleModelError(ModelGatewayError):
    """Requested model is incompatible with provider protocol or contract."""


class EndpointUnreachableError(ModelGatewayError):
    """Provider endpoint is offline or unreachable."""


class CatalogProbeError(ModelGatewayError):
    """Catalog discovery or models endpoint probe failed."""


class ProviderHttpRejectionError(ModelGatewayError):
    """Provider HTTP request was rejected by the remote service."""


class ProtocolViolationError(ModelGatewayError):
    """Adapter or provider violated the expected protocol contract."""


class ModelRuntimeError(ModelGatewayError):
    """Model execution failed at runtime."""


# ---------------------------------------------------------------------------
# ModelBinding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelBinding:
    """Immutable runtime/session model provenance object.

    Produced by :class:`ModelGateway` from a logical provider/model request.
    Carries only safe non-secret facts needed downstream for session
    execution, observability, and durable event journaling:

    - ``provider_id``: logical provider identifier (e.g. "commandcode_goat",
      "opencode_go", "my-openai", or None for offline/profile).
    - ``model_id``: logical / requested model identifier.
    - ``provider_model_id``: API-level model identifier if distinct.
    - ``display_name``: safe human-readable model label.
    - ``route``: concrete execution route ("direct_api", "legacy_cli",
      "configured_profile", "offline").
    - ``effective_protocol``: resolved protocol family ("chat_completions",
      "messages", "responses", or None).
    - ``endpoint_contract``: explicit endpoint contract / transport profile
      ("generic", "commandcode_goat", "opencode_go", "ollama_cloud").
    - ``endpoint``: safe described endpoint URL or base URL (never credentials).
    - ``auth_mode``: safe authentication mode metadata ("bearer", "anthropic",
      "none", or None).
    - ``config_fingerprint``: safe configuration fingerprint.
    - ``tool_version``: adapter / tool contract version.
    - ``protocol_version``: wire protocol version (default "1.3").
    - ``provider_runtime_identity``: provider configuration runtime contract fingerprint.

    Invariants:
    - Immutable (frozen dataclass).
    - Contains NO secrets, NO API keys, NO CLI auth contents, NO environment
      dictionaries, and NO bearer tokens.
    - Session-stable: once bound to a :class:`SessionLaunch`, it does not
      mutate if durable provider configuration changes later.
    """

    provider_id: Optional[str]
    model_id: Optional[str]
    provider_model_id: Optional[str]
    display_name: str
    route: str
    effective_protocol: Optional[str]
    endpoint_contract: str
    endpoint: Optional[str]
    auth_mode: Optional[str]
    config_fingerprint: Optional[str]
    tool_version: str
    protocol_version: str = "1.3"
    provider_runtime_identity: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.endpoint_contract or not self.endpoint_contract.strip():
            raise ModelGatewayError("endpoint_contract cannot be empty")
        if not self.route or not self.route.strip():
            raise ModelGatewayError("route cannot be empty")
        if not self.tool_version or not self.tool_version.strip():
            raise ModelGatewayError("tool_version cannot be empty")

        # Fail-closed secret scrubbing: verify no credential-shaped string leaked
        for f_name in (
            "provider_id",
            "model_id",
            "provider_model_id",
            "display_name",
            "route",
            "effective_protocol",
            "endpoint_contract",
            "endpoint",
            "auth_mode",
            "config_fingerprint",
            "tool_version",
            "protocol_version",
            "provider_runtime_identity",
        ):
            val = getattr(self, f_name)
            if isinstance(val, str) and contains_credential_shape(val):
                raise ModelGatewayError(
                    f"ModelBinding field {f_name!r} contains a credential-shaped value"
                )

        # Semantic route and provider invariants
        valid_routes = (
            ROUTE_DIRECT_API,
            ROUTE_LEGACY_CLI,
            ROUTE_CONFIGURED_PROFILE,
            ROUTE_QUALIFIED_LADDER,
            ROUTE_OFFLINE,
        )
        if self.route not in valid_routes:
            raise ModelGatewayError(f"Unknown route: {self.route!r}")

        from agentic_debugger.application.model_providers import (
            PROVIDER_KIND_CONFIGURED,
            PROVIDER_KIND_OLLAMA,
        )
        from agentic_debugger.application.provider_connections import (
            AUTH_MODES,
            TRANSPORT_COMMANDCODE_GOAT,
            TRANSPORT_MODES,
            TRANSPORT_OLLAMA_CLOUD,
            TRANSPORT_OPENCODE_GO,
            PROTOCOL_CHAT_COMPLETIONS,
            PROTOCOL_MESSAGES,
            PROTOCOL_RESPONSES,
        )

        if self.route == ROUTE_CONFIGURED_PROFILE:
            if self.provider_id not in (PROVIDER_KIND_CONFIGURED, None):
                raise ModelGatewayError(
                    f"Route configured_profile requires provider_id={PROVIDER_KIND_CONFIGURED!r}, "
                    f"got {self.provider_id!r}"
                )
            if not self.model_id or not str(self.model_id).strip():
                raise ModelGatewayError("Route configured_profile requires non-empty model_id")
            if self.endpoint:
                raise ModelGatewayError("Route configured_profile cannot define an endpoint URL")
            if self.provider_runtime_identity is not None:
                raise ModelGatewayError(
                    f"Route configured_profile cannot carry provider_runtime_identity; "
                    f"got {self.provider_runtime_identity!r}"
                )

        elif self.route == ROUTE_QUALIFIED_LADDER:
            if not self.model_id or not str(self.model_id).strip():
                raise ModelGatewayError("Route qualified_ladder requires non-empty model_id")
            if self.provider_id not in (PROVIDER_KIND_OLLAMA, "ollama", "ollama_cloud"):
                raise ModelGatewayError(
                    f"Route qualified_ladder requires provider_id={PROVIDER_KIND_OLLAMA!r} or 'ollama', "
                    f"got {self.provider_id!r}"
                )
            if self.endpoint_contract != TRANSPORT_OLLAMA_CLOUD:
                raise ModelGatewayError(
                    f"Route qualified_ladder requires endpoint_contract={TRANSPORT_OLLAMA_CLOUD!r}, "
                    f"got {self.endpoint_contract!r}"
                )
            if self.provider_runtime_identity is not None:
                raise ModelGatewayError(
                    f"Route qualified_ladder cannot carry provider_runtime_identity; "
                    f"got {self.provider_runtime_identity!r}"
                )

        elif self.route == ROUTE_DIRECT_API:
            if not self.provider_id or self.provider_id == PROVIDER_KIND_CONFIGURED:
                raise ModelGatewayError(
                    f"Route direct_api requires an explicit provider identity and cannot be {self.provider_id!r}"
                )
            if not self.endpoint or not str(self.endpoint).strip():
                raise ModelGatewayError("Route direct_api requires non-empty endpoint URL")
            if self.auth_mode not in AUTH_MODES:
                raise ModelGatewayError(
                    f"Route direct_api requires auth_mode in {AUTH_MODES!r}, got {self.auth_mode!r}"
                )
            supported_protocols = (PROTOCOL_CHAT_COMPLETIONS, PROTOCOL_MESSAGES, PROTOCOL_RESPONSES)
            if not self.effective_protocol or self.effective_protocol not in supported_protocols:
                raise ModelGatewayError(
                    f"Route direct_api requires supported effective_protocol in {supported_protocols!r}, got {self.effective_protocol!r}"
                )
            if self.endpoint_contract not in TRANSPORT_MODES:
                raise ModelGatewayError(
                    f"Route direct_api requires known endpoint_contract in {TRANSPORT_MODES!r}, got {self.endpoint_contract!r}"
                )
            if not self.provider_runtime_identity or not isinstance(self.provider_runtime_identity, str):
                raise ModelGatewayError(
                    f"Route direct_api requires provider_runtime_identity to be present"
                )
            if not re.fullmatch(r"[0-9a-f]{64}", self.provider_runtime_identity):
                raise ModelGatewayError(
                    f"Route direct_api has invalid provider_runtime_identity shape: {self.provider_runtime_identity!r}"
                )

        elif self.route == ROUTE_LEGACY_CLI:
            if not self.provider_id or self.provider_id == PROVIDER_KIND_CONFIGURED:
                raise ModelGatewayError(
                    f"Route legacy_cli requires an explicit provider identity and cannot be {self.provider_id!r}"
                )
            if not self.model_id or not str(self.model_id).strip():
                raise ModelGatewayError("Route legacy_cli requires non-empty model_id")
            historical_profiles = (TRANSPORT_OPENCODE_GO, TRANSPORT_COMMANDCODE_GOAT)
            if self.endpoint_contract not in historical_profiles:
                raise ModelGatewayError(
                    f"Route legacy_cli requires historical endpoint_contract in {historical_profiles!r}, got {self.endpoint_contract!r}"
                )
            if not self.provider_runtime_identity or not isinstance(self.provider_runtime_identity, str):
                raise ModelGatewayError(
                    f"Route legacy_cli requires provider_runtime_identity to be present"
                )
            if not re.fullmatch(r"[0-9a-f]{64}", self.provider_runtime_identity):
                raise ModelGatewayError(
                    f"Route legacy_cli has invalid provider_runtime_identity shape: {self.provider_runtime_identity!r}"
                )

        elif self.route == ROUTE_OFFLINE:
            if self.provider_id is not None and self.provider_id not in ("", "offline"):
                raise ModelGatewayError(
                    f"Route offline cannot carry provider execution identity: {self.provider_id!r}"
                )
            if self.endpoint:
                raise ModelGatewayError("Route offline cannot define an endpoint URL")
            if self.provider_runtime_identity is not None:
                raise ModelGatewayError(
                    f"Route offline cannot carry provider_runtime_identity; "
                    f"got {self.provider_runtime_identity!r}"
                )

    def to_mapping(self) -> Dict[str, Any]:
        """Safe serializable dictionary representation (no secrets exist)."""
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "provider_model_id": self.provider_model_id,
            "display_name": self.display_name,
            "route": self.route,
            "effective_protocol": self.effective_protocol,
            "endpoint_contract": self.endpoint_contract,
            "endpoint": self.endpoint,
            "auth_mode": self.auth_mode,
            "config_fingerprint": self.config_fingerprint,
            "tool_version": self.tool_version,
            "protocol_version": self.protocol_version,
            "provider_runtime_identity": self.provider_runtime_identity,
        }

    def fingerprint(self) -> str:
        """Deterministic sha256 fingerprint of the safe binding metadata."""
        canonical = json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def model_configured_payload(self) -> Dict[str, Any]:
        """Safe payload dictionary suitable for ``model.configured`` event emission."""
        payload: Dict[str, Any] = {
            "profile_id": self.model_id or "offline",
            "config_fingerprint": self.config_fingerprint or self.fingerprint(),
            "display_name": self.display_name,
            "protocol_version": self.protocol_version,
            "tool_version": self.tool_version,
            "model_binding_fingerprint": self.fingerprint(),
        }
        if self.provider_id:
            payload["provider"] = self.provider_id
        if self.provider_runtime_identity:
            payload["provider_runtime_identity"] = self.provider_runtime_identity
        if self.route:
            payload["route"] = self.route
        if self.effective_protocol:
            payload["api_protocol"] = self.effective_protocol
            payload["effective_protocol"] = self.effective_protocol
        if self.endpoint_contract:
            payload["endpoint_contract"] = self.endpoint_contract
            payload["transport_profile"] = self.endpoint_contract
        if self.auth_mode:
            payload["auth_mode"] = self.auth_mode
        if self.provider_model_id:
            payload["provider_model_id"] = self.provider_model_id
        if self.endpoint:
            payload["endpoint"] = self.endpoint
        return payload

    @property
    def provider(self) -> Optional[str]:
        """Convenience alias for provider_id."""
        return self.provider_id

    @property
    def model(self) -> Optional[str]:
        """Convenience alias for model_id."""
        return self.model_id

    @property
    def transport_profile(self) -> str:
        """Convenience alias for endpoint_contract."""
        return self.endpoint_contract

    @property
    def base_url(self) -> Optional[str]:
        """Convenience alias for endpoint."""
        return self.endpoint

    @property
    def binding_id(self) -> str:
        """Convenience alias for fingerprint."""
        return self.fingerprint()

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> ModelBinding:
        """Reconstitute a ModelBinding from a safe dictionary mapping."""
        return cls(
            provider_id=mapping.get("provider_id") or mapping.get("provider"),
            model_id=mapping.get("model_id") or mapping.get("model"),
            provider_model_id=mapping.get("provider_model_id") or mapping.get("api_model"),
            display_name=mapping.get("display_name", ""),
            route=mapping.get("route", ROUTE_DIRECT_API),
            effective_protocol=mapping.get("effective_protocol") or mapping.get("protocol"),
            endpoint_contract=mapping.get("endpoint_contract") or mapping.get("transport_profile", TRANSPORT_GENERIC),
            endpoint=mapping.get("endpoint") or mapping.get("base_url"),
            auth_mode=mapping.get("auth_mode"),
            config_fingerprint=mapping.get("config_fingerprint"),
            tool_version=mapping.get("tool_version", "1.0"),
            protocol_version=mapping.get("protocol_version", "1.3"),
            provider_runtime_identity=mapping.get("provider_runtime_identity"),
        )

    def __repr__(self) -> str:
        return (
            f"ModelBinding("
            f"provider={self.provider_id!r}, "
            f"model={self.model_id!r}, "
            f"api_model={self.provider_model_id!r}, "
            f"route={self.route!r}, "
            f"protocol={self.effective_protocol!r}, "
            f"contract={self.endpoint_contract!r})"
        )


# ---------------------------------------------------------------------------
# Status snapshot and preflight models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderStatusSnapshot:
    """Truthful provider status snapshot with distinct factual dimensions.

    Minimum distinguished facts (ADR 0001 §9, V2 Plan §9):
    - ``is_configured``: valid enabled durable configuration exists (no network claim);
    - ``credential_ready``: credential authority required by auth mode is available (no network claim);
    - ``is_runnable``: static auth × protocol × contract preflight succeeds (no network claim);
    - ``catalog_refreshed_at_utc``: timestamp of last successful catalog refresh (historical fact);
    - ``live_verified`` / ``live_verified_at_utc``: explicit live probe succeeded (historical evidence);
    - ``runtime_succeeded_at_utc``: real session model request succeeded (derived observational history).
    """

    provider_id: str
    label: str
    base_url: str
    endpoint_contract: str
    auth_mode: str
    api_format: str

    # 1. Configured
    is_configured: bool
    is_enabled: bool
    is_quarantined: bool

    # 2. Credential ready
    credential_ready: bool
    credential_source: Optional[str]

    # 3. Provider static readiness (static preflight for provider)
    is_provider_ready: bool = True
    provider_readiness_reason: Optional[str] = None
    is_runnable: bool = True  # Backward compatibility alias
    runnable_reason: Optional[str] = None  # Backward compatibility alias

    # 4. Catalog refreshed at T
    catalog_model_count: int = 0
    catalog_refreshed_at_utc: Optional[str] = None
    catalog_refreshed_source: Optional[str] = None
    catalog_stale: bool = False
    catalog_error: Optional[str] = None

    # 5. Live verified at T / Reachable at T
    live_verified: bool = False
    live_verified_at_utc: Optional[str] = None
    live_probe_error: Optional[str] = None

    # 6. Runtime succeeded at T (derived observational history)
    runtime_succeeded_at_utc: Optional[str] = None

    # Cached models
    cached_models: Tuple[DiscoveredProviderModel, ...] = ()

    # Backward compatibility property
    @property
    def kind(self) -> str:
        return self.provider_id

    @property
    def connected(self) -> bool:
        """Connected is strictly reserved for explicit live verification when currently ready."""
        return bool(
            self.live_verified
            and self.credential_ready
            and self.is_provider_ready
            and not self.is_quarantined
            and self.is_enabled
        )

    @property
    def transport_profile(self) -> str:
        return self.endpoint_contract

    @property
    def model_count(self) -> int:
        return self.catalog_model_count

    @property
    def last_refresh_utc(self) -> Optional[str]:
        return self.catalog_refreshed_at_utc

    @property
    def last_refresh_source(self) -> Optional[str]:
        return self.catalog_refreshed_source

    @property
    def stale(self) -> bool:
        return self.catalog_stale

    @property
    def summary_headline(self) -> str:
        """Truthful status headline adhering to ADR 0001 §9 and V2 Plan §9."""
        if not self.is_enabled:
            return "Disabled"
        if self.is_quarantined:
            return "Quarantined · recovery required"
        if not self.is_provider_ready and self.provider_readiness_reason and "Status evaluation error" in self.provider_readiness_reason:
            return "Degraded · status error"
        if not self.credential_ready:
            if self.is_configured:
                return "Configured · no credential"
            return "Not configured"
        if not self.is_provider_ready:
            return "Degraded · not ready"
        if self.live_verified:
            return "Live verified"
        if self.auth_mode == "none":
            if is_loopback_url(self.base_url):
                return "Configured · loopback"
            return "Configured · no auth"
        if self.credential_source and self.credential_source != "none":
            src_label = _PROVIDER_CREDENTIAL_SOURCE_LABELS.get(
                self.credential_source, self.credential_source
            )
            return f"Configured · {src_label}"
        return "Configured · credential ready"

    @property
    def status_message(self) -> Optional[str]:
        if not self.is_enabled:
            return "Provider is disabled (edit provider to re-enable it)"
        if self.is_quarantined:
            return "Credential state requires recovery. Edit provider and save an API key again."
        if not self.credential_ready:
            return "Credential missing or rebinding required — edit provider to set an API key"
        if not self.is_provider_ready:
            return self.provider_readiness_reason or self.runnable_reason
        if self.live_probe_error:
            return f"Live verification failed: {self.live_probe_error}"
        return None


@dataclass(frozen=True)
class ModelStaticPreflight:
    """Static preflight result for a specific provider/model pair (no network)."""

    provider_id: str
    model_id: str
    is_runnable: bool
    blocker_reason: Optional[str]
    effective_protocol: Optional[str]
    endpoint_contract: str
    route: Optional[str]


# ---------------------------------------------------------------------------
# ModelGateway
# ---------------------------------------------------------------------------


class ModelGateway:
    """Single product provider-runtime authority façade.

    Absorbs product-level calls that independently ask provider core for:
    - configuration resolution;
    - effective protocol resolution;
    - route selection (direct API vs legacy CLI);
    - API model id mapping;
    - transport construction;
    - static readiness/preflight;
    - truthful status snapshots.
    """

    _default_instance: Optional[ModelGateway] = None

    def __init__(self, config_root: Optional[Any] = None) -> None:
        self.config_root = config_root
        # In-memory record of explicit live probe outcomes during application run
        # Key: provider_id -> {"verified": bool, "timestamp": str, "error": Optional[str]}
        self._live_probe_results: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def default(cls, config_root: Optional[Any] = None) -> ModelGateway:
        if cls._default_instance is None:
            cls._default_instance = cls(config_root=config_root)
        elif config_root is not None:
            cls._default_instance.config_root = config_root
        return cls._default_instance

    def invalidate_provider(self, provider_id: str) -> None:
        """Clear cached probe results when a provider configuration changes."""
        self._live_probe_results.pop(provider_id, None)

    @staticmethod
    def is_known_provider(provider_id: Optional[str]) -> bool:
        """Whether a provider identity is known in durable configuration or presets."""
        if not provider_id or not isinstance(provider_id, str):
            return False
        return is_known_provider(provider_id)

    # -- Resolution ----------------------------------------------------------

    def resolve(
        self,
        provider_id: Optional[str],
        model_id: Optional[str],
        *,
        profile_id: Optional[str] = None,
        logical_call_ceiling: int = _DEFAULT_MAX_MODEL_REQUESTS,
        request_timeout_seconds: Optional[float] = None,
        config_root: Optional[Any] = None,
        is_ollama: bool = False,
        ollama_alias: Optional[str] = None,
    ) -> ModelBinding:
        """Resolve a logical provider/model request into an immutable ModelBinding."""
        from agentic_debugger.application.model_providers import (
            format_model_display_name,
            PROVIDER_KIND_COMMANDCODE,
            PROVIDER_KIND_CONFIGURED,
            PROVIDER_KIND_OLLAMA,
            PROVIDER_KIND_OPENCODE,
            ProviderRegistryError,
            resolve_provider_live_config,
        )

        effective_provider = provider_id
        effective_model = model_id or profile_id

        # 1. Qualified Ollama Cloud ladder model (explicit ladder contract)
        if (effective_provider is None or effective_provider == PROVIDER_KIND_OLLAMA) and (is_ollama and ollama_alias):
            from scripts.ollama_cloud_command_adapter import build_ollama_live_config
            from agentic_debugger.application.level32 import level32_model_profiles

            ollama_profile = None
            for m in level32_model_profiles():
                if m.alias == ollama_alias:
                    ollama_profile = m
                    break
            if ollama_profile is None:
                raise IncompatibleModelError(
                    f"Ollama model not in qualified roster: {ollama_alias}"
                )

            live_config = build_ollama_live_config(
                ollama_profile.alias, logical_call_ceiling=logical_call_ceiling
            )
            return ModelBinding(
                provider_id=PROVIDER_KIND_OLLAMA,
                model_id=ollama_profile.alias,
                provider_model_id=ollama_profile.alias,
                display_name=ollama_profile.display_name,
                route=ROUTE_QUALIFIED_LADDER,
                effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
                endpoint_contract=TRANSPORT_OLLAMA_CLOUD,
                endpoint=None,
                auth_mode=AUTH_BEARER,
                config_fingerprint=ollama_profile.transport_config_fingerprint,
                tool_version=live_config.tool_version,
                protocol_version="1.3",
            )

        # 2. Configured registry provider (e.g. commandcode, opencode, configured ollama, or custom)
        if effective_provider is not None and effective_provider != PROVIDER_KIND_CONFIGURED:
            cfg = get_provider_config(effective_provider)
            if cfg is None:
                raise ProviderConfigurationError(
                    f"Provider {effective_provider!r} is not configured"
                )
            if not effective_model or not str(effective_model).strip():
                raise ProviderConfigurationError("model_id must be a non-empty string")

            endpoint_contract = cfg.transport_profile or TRANSPORT_GENERIC
            disp_name = cfg.name
            runtime_id = provider_runtime_identity(cfg)
            if runtime_id is None:
                raise ProviderConfigurationError(
                    f"Provider {effective_provider!r} configuration is incomplete or missing runtime provenance facts"
                )

            # Check static readiness conditions (disabled or quarantined)
            if not cfg.enabled or is_provider_quarantined(cfg.provider_id):
                api_model = provider_api_model_id(cfg.provider_id, effective_model)
                proto = cfg.api_format
                return ModelBinding(
                    provider_id=cfg.provider_id,
                    model_id=effective_model,
                    provider_model_id=api_model or effective_model,
                    display_name=str(effective_model or cfg.name),
                    route=ROUTE_DIRECT_API,
                    effective_protocol=proto,
                    endpoint_contract=endpoint_contract,
                    endpoint=cfg.base_url,
                    auth_mode=cfg.auth_mode,
                    config_fingerprint=None,
                    tool_version="live-command-v1",
                    protocol_version="1.3",
                    provider_runtime_identity=runtime_id,
                )

            # Delegate based on explicit structured provider facts
            from agentic_debugger.application.model_providers import _legacy_for_config

            api_model = provider_api_model_id(cfg.provider_id, effective_model)
            historical_profiles = (TRANSPORT_OPENCODE_GO, TRANSPORT_COMMANDCODE_GOAT)
            is_historical = endpoint_contract in historical_profiles
            direct_cred_missing = (
                cfg.auth_mode != AUTH_NONE and credential_source_for(cfg.provider_id) is None
            )

            if not is_historical:
                # Direct-only / Generic provider
                if direct_cred_missing:
                    # Missing credentials on a direct-only provider:
                    # Statically determine protocol executability without calling live resolution.
                    try:
                        proto = effective_model_protocol(cfg.provider_id, effective_model)
                    except ProviderConnectionError as p_exc:
                        raise IncompatibleModelError(
                            f"Provider {effective_provider!r} model {effective_model!r} incompatible: {p_exc}"
                        ) from p_exc
                    except Exception as p_exc:
                        raise ProviderConfigurationError(
                            f"Provider {effective_provider!r} protocol resolution error: {p_exc}"
                        ) from p_exc

                    if proto is None or not is_protocol_executable(cfg.provider_id, proto):
                        raise IncompatibleModelError(
                            f"Provider {effective_provider!r} model {effective_model!r} protocol {proto!r} is not executable"
                        )

                    return ModelBinding(
                        provider_id=cfg.provider_id,
                        model_id=effective_model,
                        provider_model_id=api_model or effective_model,
                        display_name=str(effective_model or cfg.name),
                        route=ROUTE_DIRECT_API,
                        effective_protocol=proto,
                        endpoint_contract=endpoint_contract,
                        endpoint=cfg.base_url,
                        auth_mode=cfg.auth_mode,
                        config_fingerprint=None,
                        tool_version="live-command-v1",
                        protocol_version="1.3",
                        provider_runtime_identity=runtime_id,
                    )

                # Direct credentials present: resolve live config
                try:
                    live_config, provenance = resolve_provider_live_config(
                        effective_provider,
                        effective_model,
                        logical_call_ceiling=logical_call_ceiling,
                        request_timeout_seconds=request_timeout_seconds,
                    )
                except ProviderRegistryError as exc:
                    try:
                        proto = effective_model_protocol(cfg.provider_id, effective_model)
                    except ProviderConnectionError as p_exc:
                        raise IncompatibleModelError(
                            f"Provider {effective_provider!r} model {effective_model!r} incompatible: {p_exc}"
                        ) from p_exc
                    except Exception:
                        pass
                    raise ProviderConfigurationError(
                        f"Provider {effective_provider!r} live configuration resolution failed: {exc}"
                    ) from exc

                route = str(provenance.get("route") or ROUTE_DIRECT_API)
                api_proto = provenance.get("api_protocol")
                endpoint = provenance.get("endpoint") or (cfg.base_url if route == ROUTE_DIRECT_API else None)
                auth = provenance.get("auth_mode") or (cfg.auth_mode if route == ROUTE_DIRECT_API else None)
                return ModelBinding(
                    provider_id=effective_provider,
                    model_id=effective_model,
                    provider_model_id=provenance.get("provider_model_id") or api_model or effective_model,
                    display_name=str(provenance.get("display_name") or effective_model or disp_name),
                    route=route,
                    effective_protocol=api_proto,
                    endpoint_contract=endpoint_contract,
                    endpoint=endpoint,
                    auth_mode=auth,
                    config_fingerprint=live_config.configuration_fingerprint,
                    tool_version=live_config.tool_version,
                    protocol_version=str(provenance.get("protocol_version") or "1.3"),
                    provider_runtime_identity=runtime_id,
                )
            else:
                # Historical provider (carrying TRANSPORT_OPENCODE_GO or TRANSPORT_COMMANDCODE_GOAT)
                legacy_ok, _ = _legacy_for_config(cfg)
                if not legacy_ok and direct_cred_missing:
                    # Legacy CLI is not available AND direct credentials are missing:
                    # Statically determine direct protocol executability from explicit structured facts BEFORE live resolution.
                    try:
                        proto = effective_model_protocol(cfg.provider_id, effective_model)
                    except ProviderConnectionError as p_exc:
                        raise IncompatibleModelError(
                            f"Provider {effective_provider!r} model {effective_model!r} incompatible: {p_exc}"
                        ) from p_exc
                    except Exception as p_exc:
                        raise ProviderConfigurationError(
                            f"Provider {effective_provider!r} protocol resolution error: {p_exc}"
                        ) from p_exc

                    if proto is not None and is_protocol_executable(cfg.provider_id, proto):
                        return ModelBinding(
                            provider_id=cfg.provider_id,
                            model_id=effective_model,
                            provider_model_id=api_model or effective_model,
                            display_name=str(effective_model or cfg.name),
                            route=ROUTE_DIRECT_API,
                            effective_protocol=proto,
                            endpoint_contract=endpoint_contract,
                            endpoint=cfg.base_url,
                            auth_mode=cfg.auth_mode,
                            config_fingerprint=None,
                            tool_version="live-command-v1",
                            protocol_version="1.3",
                            provider_runtime_identity=runtime_id,
                        )
                    raise IncompatibleModelError(
                        f"Provider {effective_provider!r} model {effective_model!r} protocol {proto!r} is not executable"
                    )

                # Either legacy CLI is available OR direct credentials exist:
                # Resolve live config. Any ProviderRegistryError fails closed!
                try:
                    live_config, provenance = resolve_provider_live_config(
                        effective_provider,
                        effective_model,
                        logical_call_ceiling=logical_call_ceiling,
                        request_timeout_seconds=request_timeout_seconds,
                    )
                except ProviderRegistryError as exc:
                    legacy_ok, _ = _legacy_for_config(cfg)
                    if legacy_ok:
                        raise ProviderConfigurationError(
                            f"Provider {effective_provider!r} legacy CLI resolution failed: {exc}"
                        ) from exc
                    raise ProviderConfigurationError(
                        f"Provider {effective_provider!r} live configuration resolution failed: {exc}"
                    ) from exc

                route = str(provenance.get("route") or ROUTE_DIRECT_API)
                api_proto = provenance.get("api_protocol")
                endpoint = provenance.get("endpoint") or (cfg.base_url if route == ROUTE_DIRECT_API else None)
                auth = provenance.get("auth_mode") or (cfg.auth_mode if route == ROUTE_DIRECT_API else None)
                return ModelBinding(
                    provider_id=effective_provider,
                    model_id=effective_model,
                    provider_model_id=provenance.get("provider_model_id") or api_model or effective_model,
                    display_name=str(provenance.get("display_name") or effective_model or disp_name),
                    route=route,
                    effective_protocol=api_proto,
                    endpoint_contract=endpoint_contract,
                    endpoint=endpoint,
                    auth_mode=auth,
                    config_fingerprint=live_config.configuration_fingerprint,
                    tool_version=live_config.tool_version,
                    protocol_version=str(provenance.get("protocol_version") or "1.3"),
                    provider_runtime_identity=runtime_id,
                )

        # 3. Custom command profile store (configured source / legacy profile)
        if effective_provider == PROVIDER_KIND_CONFIGURED or profile_id is not None:
            target_profile_id = profile_id or effective_model
            if not target_profile_id or not str(target_profile_id).strip():
                raise ProviderConfigurationError("Configured profile id must be a non-empty string")

            root_path = Path(config_root or self.config_root or ".")
            from agentic_debugger.application.command_config import (
                CommandConfigError,
                CommandConfigNotFoundError,
                CommandModelConfigStore,
            )

            try:
                store = CommandModelConfigStore(root_path)
                profile = store.get(target_profile_id)
            except CommandConfigNotFoundError as exc:
                raise ProviderConfigurationError(
                    f"Configured command profile {target_profile_id!r} not found: {exc}"
                ) from exc
            except (CommandConfigError, OSError) as exc:
                raise ProviderConfigurationError(
                    f"Configured command profile {target_profile_id!r} invalid or unreadable: {exc}"
                ) from exc
            except Exception as exc:
                raise ProviderConfigurationError(
                    f"Configured command profile {target_profile_id!r} resolution failed: {exc}"
                ) from exc

            return ModelBinding(
                provider_id=PROVIDER_KIND_CONFIGURED,
                model_id=profile.profile_id,
                provider_model_id=profile.profile_id,
                display_name=profile.display_name,
                route=ROUTE_CONFIGURED_PROFILE,
                effective_protocol=None,
                endpoint_contract=TRANSPORT_GENERIC,
                endpoint=None,
                auth_mode=None,
                config_fingerprint=profile.configuration_fingerprint,
                tool_version=profile.tool_version,
                protocol_version="1.3",
            )

        # 4. Offline mode / default fallback
        return ModelBinding(
            provider_id=None,
            model_id=effective_model or "offline",
            provider_model_id=None,
            display_name="Offline",
            route=ROUTE_OFFLINE,
            effective_protocol=None,
            endpoint_contract=TRANSPORT_GENERIC,
            endpoint=None,
            auth_mode=None,
            config_fingerprint=None,
            tool_version="offline-v1",
            protocol_version="1.3",
        )

    # -- Static Preflight (no network) ---------------------------------------

    def provider_readiness(self, provider_id: str) -> Tuple[bool, Optional[str]]:
        """Static provider-level readiness check without requiring a specific model."""
        cfg = get_provider_config(provider_id)
        if cfg is None:
            return False, f"Provider {provider_id!r} is not configured"
        if not cfg.enabled:
            return False, "Provider is disabled"
        if is_provider_quarantined(provider_id):
            return False, "Credential state requires recovery (quarantined)"
        if cfg.auth_mode != AUTH_NONE and credential_source_for(provider_id) is None:
            return False, "No usable credential source found"
        if not cfg.base_url or not cfg.base_url.strip():
            return False, "Provider base URL is not configured"
        return True, None

    def static_preflight(
        self, provider_or_binding: Any, model_id: Optional[str] = None
    ) -> ModelStaticPreflight:
        """Perform static runtime preflight check without network I/O.

        - ``static_preflight(provider_id, model_id)``: preflights CURRENT durable configuration.
        - ``static_preflight(ModelBinding)``: preflights THIS binding for execution now,
          corroborating it against current state and failing closed on drift.
        """
        if isinstance(provider_or_binding, ModelBinding):
            binding = provider_or_binding
            if binding.route == ROUTE_OFFLINE:
                return ModelStaticPreflight(
                    provider_id=binding.provider_id or "",
                    model_id=binding.model_id or "offline",
                    is_runnable=False,
                    blocker_reason="Offline binding is not runnable",
                    effective_protocol=None,
                    endpoint_contract=binding.endpoint_contract,
                    route=ROUTE_OFFLINE,
                )

            if binding.route == ROUTE_CONFIGURED_PROFILE:
                from agentic_debugger.application.command_config import CommandModelConfigStore
                store = CommandModelConfigStore(Path(self.config_root) if self.config_root else Path("."))
                try:
                    profile = store.get(binding.model_id or "")
                except Exception as exc:
                    return ModelStaticPreflight(
                        provider_id=binding.provider_id or "configured",
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason=f"Command profile unavailable (stale binding): {exc}",
                        effective_protocol=None,
                        endpoint_contract=binding.endpoint_contract,
                        route=ROUTE_CONFIGURED_PROFILE,
                    )
                if binding.config_fingerprint is not None and profile.configuration_fingerprint != binding.config_fingerprint:
                    return ModelStaticPreflight(
                        provider_id=binding.provider_id or "configured",
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason="Command profile configuration drifted (stale binding)",
                        effective_protocol=None,
                        endpoint_contract=binding.endpoint_contract,
                        route=ROUTE_CONFIGURED_PROFILE,
                    )
                if binding.tool_version and profile.tool_version != binding.tool_version:
                    return ModelStaticPreflight(
                        provider_id=binding.provider_id or "configured",
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason="Command profile tool version drifted (stale binding)",
                        effective_protocol=None,
                        endpoint_contract=binding.endpoint_contract,
                        route=ROUTE_CONFIGURED_PROFILE,
                    )
                return ModelStaticPreflight(
                    provider_id=binding.provider_id or "configured",
                    model_id=binding.model_id or "",
                    is_runnable=True,
                    blocker_reason=None,
                    effective_protocol=None,
                    endpoint_contract=binding.endpoint_contract,
                    route=ROUTE_CONFIGURED_PROFILE,
                )

            if binding.route == ROUTE_QUALIFIED_LADDER:
                from agentic_debugger.application.level32 import level32_model_profiles
                ollama_profile = None
                for m in level32_model_profiles():
                    if m.alias == (binding.model_id or ""):
                        ollama_profile = m
                        break
                if ollama_profile is None:
                    return ModelStaticPreflight(
                        provider_id=binding.provider_id or "ollama",
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason=f"Qualified model {binding.model_id!r} not found (stale binding)",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=ROUTE_QUALIFIED_LADDER,
                    )
                if binding.config_fingerprint is not None and ollama_profile.transport_config_fingerprint != binding.config_fingerprint:
                    return ModelStaticPreflight(
                        provider_id=binding.provider_id or "ollama",
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason="Qualified ladder configuration drifted (stale binding)",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=ROUTE_QUALIFIED_LADDER,
                    )
                return ModelStaticPreflight(
                    provider_id=binding.provider_id or "ollama",
                    model_id=binding.model_id or "",
                    is_runnable=True,
                    blocker_reason=None,
                    effective_protocol=binding.effective_protocol,
                    endpoint_contract=binding.endpoint_contract,
                    route=ROUTE_QUALIFIED_LADDER,
                )

            # Direct API or Legacy CLI route
            prov_id = binding.provider_id or ""
            cfg = get_provider_config(prov_id)
            if cfg is None:
                return ModelStaticPreflight(
                    provider_id=prov_id,
                    model_id=binding.model_id or "",
                    is_runnable=False,
                    blocker_reason=f"Provider {prov_id!r} is not configured (stale binding)",
                    effective_protocol=binding.effective_protocol,
                    endpoint_contract=binding.endpoint_contract,
                    route=binding.route,
                )

            # Corroborate binding facts against current cfg
            cur_identity = provider_runtime_identity(cfg)
            expected_contract = cfg.transport_profile or TRANSPORT_GENERIC
            clean_binding_endpoint = (binding.endpoint or "").strip().rstrip("/")
            clean_cfg_endpoint = (cfg.base_url or "").strip().rstrip("/")
            if not cfg.enabled or is_provider_quarantined(cfg.provider_id):
                cur_proto = cfg.api_format
            else:
                try:
                    cur_proto = resolve_model_protocol(cfg.provider_id, binding.model_id or "") or cfg.api_format
                except Exception:
                    cur_proto = cfg.api_format
            cur_api_model = provider_api_model_id(cfg.provider_id, binding.model_id or "") if binding.model_id else None

            if binding.route == ROUTE_DIRECT_API:
                if binding.endpoint_contract != expected_contract:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason=f"Provider {prov_id!r} endpoint contract drifted (stale binding)",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                if clean_binding_endpoint != clean_cfg_endpoint:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason=f"Provider {prov_id!r} endpoint drifted (stale binding)",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                if binding.auth_mode != cfg.auth_mode:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason=f"Provider {prov_id!r} auth mode drifted (stale binding)",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                if binding.effective_protocol != cur_proto:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason=f"Provider {prov_id!r} effective protocol drifted (stale binding)",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                if binding.provider_model_id != cur_api_model and (binding.provider_model_id or cur_api_model):
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason=f"Provider {prov_id!r} model API id drifted (stale binding)",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                if cur_identity != binding.provider_runtime_identity:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason=f"Provider {prov_id!r} runtime configuration drifted (stale binding)",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )

                # Corroboration passed: evaluate current readiness for this binding
                if not cfg.enabled:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason="Provider is disabled",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                if is_provider_quarantined(prov_id):
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason="Credential state requires recovery (quarantined)",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                cred_source = credential_source_for(prov_id)
                if cfg.auth_mode != AUTH_NONE and cred_source is None:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason="No usable credential source found",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                if binding.effective_protocol is not None and not is_protocol_executable(prov_id, binding.effective_protocol):
                    blocker = protocol_blocker_reason(prov_id, binding.effective_protocol)
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason=blocker or "Protocol not executable under current auth/contract",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                return ModelStaticPreflight(
                    provider_id=prov_id,
                    model_id=binding.model_id or "",
                    is_runnable=True,
                    blocker_reason=None,
                    effective_protocol=binding.effective_protocol,
                    endpoint_contract=binding.endpoint_contract,
                    route=binding.route,
                )

            elif binding.route == ROUTE_LEGACY_CLI:
                historical_profiles = (TRANSPORT_OPENCODE_GO, TRANSPORT_COMMANDCODE_GOAT)
                if binding.endpoint_contract not in historical_profiles or binding.endpoint_contract != expected_contract:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason=f"Provider {prov_id!r} endpoint contract drifted (stale binding)",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                if cur_identity != binding.provider_runtime_identity:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason=f"Provider {prov_id!r} runtime configuration drifted (stale binding)",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                if not cfg.enabled:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason="Provider is disabled",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                if is_provider_quarantined(prov_id):
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason="Credential state requires recovery (quarantined)",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                from agentic_debugger.application.model_providers import _legacy_for_config
                legacy_ok, legacy_reason = _legacy_for_config(cfg)
                if not legacy_ok:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id or "",
                        is_runnable=False,
                        blocker_reason=legacy_reason or "Legacy CLI route is unavailable",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                return ModelStaticPreflight(
                    provider_id=prov_id,
                    model_id=binding.model_id or "",
                    is_runnable=True,
                    blocker_reason=None,
                    effective_protocol=binding.effective_protocol,
                    endpoint_contract=binding.endpoint_contract,
                    route=binding.route,
                )

            return ModelStaticPreflight(
                provider_id=prov_id,
                model_id=binding.model_id or "",
                is_runnable=True,
                blocker_reason=None,
                effective_protocol=binding.effective_protocol,
                endpoint_contract=binding.endpoint_contract,
                route=binding.route,
            )

        # Provider ID string branch (evaluates current durable configuration)
        provider_id = str(provider_or_binding)
        cfg = get_provider_config(provider_id)
        if cfg is None:
            return ModelStaticPreflight(
                provider_id=provider_id,
                model_id=model_id or "",
                is_runnable=False,
                blocker_reason=f"Provider {provider_id!r} is not configured",
                effective_protocol=None,
                endpoint_contract=TRANSPORT_GENERIC,
                route=None,
            )
        if not cfg.enabled:
            return ModelStaticPreflight(
                provider_id=provider_id,
                model_id=model_id or "",
                is_runnable=False,
                blocker_reason="Provider is disabled",
                effective_protocol=None,
                endpoint_contract=cfg.transport_profile,
                route=None,
            )
        if is_provider_quarantined(provider_id):
            return ModelStaticPreflight(
                provider_id=provider_id,
                model_id=model_id or "",
                is_runnable=False,
                blocker_reason="Credential state requires recovery (quarantined)",
                effective_protocol=None,
                endpoint_contract=cfg.transport_profile,
                route=None,
            )

        if model_id:
            try:
                binding = self.resolve(provider_id, model_id)
                return self.static_preflight(binding)
            except (IncompatibleModelError, ProviderConfigurationError) as exc:
                return ModelStaticPreflight(
                    provider_id=provider_id,
                    model_id=model_id,
                    is_runnable=False,
                    blocker_reason=str(exc),
                    effective_protocol=None,
                    endpoint_contract=cfg.transport_profile or TRANSPORT_GENERIC,
                    route=None,
                )

        from agentic_debugger.application.model_providers import _legacy_for_config
        legacy_ok, legacy_reason = _legacy_for_config(cfg)

        cred_source = credential_source_for(provider_id)
        direct_cred_ok = (cfg.auth_mode == AUTH_NONE or cred_source is not None)
        effective_proto = cfg.api_format
        direct_proto_ok = effective_proto is not None and is_protocol_executable(provider_id, effective_proto)
        if direct_cred_ok and direct_proto_ok:
            return ModelStaticPreflight(
                provider_id=provider_id,
                model_id="",
                is_runnable=True,
                blocker_reason=None,
                effective_protocol=effective_proto,
                endpoint_contract=cfg.transport_profile,
                route=ROUTE_DIRECT_API,
            )

        if legacy_ok:
            return ModelStaticPreflight(
                provider_id=provider_id,
                model_id="",
                is_runnable=True,
                blocker_reason=None,
                effective_protocol=None,
                endpoint_contract=cfg.transport_profile,
                route=ROUTE_LEGACY_CLI,
            )

        blocker = "No usable credential source found" if not direct_cred_ok else (legacy_reason or "Provider is not runnable")
        return ModelStaticPreflight(
            provider_id=provider_id,
            model_id="",
            is_runnable=False,
            blocker_reason=blocker,
            effective_protocol=effective_proto,
            endpoint_contract=cfg.transport_profile,
        )

    # -- Explicit Probes (requires explicit user action) ----------------------

    def probe_reachability(
        self,
        provider_id: str,
        *,
        model_id: Optional[str] = None,
        timeout_seconds: float = 10.0,
        engine: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Perform an explicit reachability probe of the provider endpoint."""
        cfg = get_provider_config(provider_id)
        current_identity = provider_runtime_identity(cfg) if cfg is not None else None
        now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            result = test_provider_connection(
                provider_id,
                model_id=model_id,
                timeout_seconds=timeout_seconds,
                engine=engine,
            )
        except ProviderConnectionError as exc:
            self._live_probe_results[provider_id] = {
                "verified": False,
                "timestamp": now_utc,
                "error": str(exc),
                "runtime_identity": current_identity,
            }
            return {
                "ok": False,
                "connected": False,
                "reachable": False,
                "reason": str(exc),
                "error": str(exc),
                "verified_at_utc": None,
                "timestamp": now_utc,
                "endpoint": None,
            }

        ok = bool(result.get("ok", False))
        reason = str(result.get("reason") or "")
        if ok:
            self._live_probe_results[provider_id] = {
                "verified": True,
                "timestamp": now_utc,
                "error": None,
                "runtime_identity": current_identity,
            }
            return {
                "ok": True,
                "connected": True,
                "reachable": True,
                "reason": reason,
                "error": None,
                "verified_at_utc": now_utc,
                "timestamp": now_utc,
                "endpoint": result.get("endpoint"),
                "model_count": result.get("model_count", 0),
            }
        else:
            self._live_probe_results[provider_id] = {
                "verified": False,
                "timestamp": now_utc,
                "error": reason,
                "runtime_identity": current_identity,
            }
            return {
                "ok": False,
                "connected": False,
                "reachable": False,
                "reason": reason,
                "error": reason,
                "verified_at_utc": None,
                "timestamp": now_utc,
                "endpoint": result.get("endpoint"),
            }

    def refresh_catalog(self, provider_id: str) -> ProviderCatalogSnapshot:
        """Explicitly refresh the provider's live model catalog (GET /models)."""
        cfg = get_provider_config(provider_id)
        current_identity = provider_runtime_identity(cfg) if cfg is not None else None
        now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            from agentic_debugger.application import provider_connections as pc

            snapshot = pc.refresh_provider_catalog(provider_id)
        except ProviderConnectionError as exc:
            self._live_probe_results[provider_id] = {
                "verified": False,
                "timestamp": now_utc,
                "error": str(exc),
                "runtime_identity": current_identity,
            }
            raise CatalogProbeError(str(exc)) from exc

        # Successful catalog refresh is reachability evidence for tested identity
        self._live_probe_results[provider_id] = {
            "verified": True,
            "timestamp": now_utc,
            "error": None,
            "runtime_identity": current_identity,
        }
        return snapshot

    # -- History-Derived Observational Metadata ------------------------------

    def inspect_last_runtime_success(
        self,
        provider_id_or_history_root: Any,
        second_arg: Any = None,
        model_id: Optional[str] = None,
        *,
        history_root: Optional[Path | str] = None,
        sessions_root: Optional[Path | str] = None,
        target_config: Optional[ProviderConfig] = None,
        target_binding: Optional[ModelBinding] = None,
    ) -> Optional[str]:
        """Derive the timestamp of the last successful model request from durable history.

        Pure observational derivation: inspects authoritative session history
        without mutating durable provider configuration. Bounded to scanning
        recent completed sessions. Requires journal provenance to match the
        current target provider runtime identity; unbound historical runs do
        not verify the current configuration. Returns ISO UTC timestamp or None.
        """
        effective_root = history_root or sessions_root
        if second_arg is not None:
            # Called as inspect_last_runtime_success(history_root, provider_id)
            # or inspect_last_runtime_success(provider_id, model_id, history_root=...)
            if isinstance(provider_id_or_history_root, (Path, str)) and Path(str(provider_id_or_history_root)).is_dir():
                effective_root = effective_root or provider_id_or_history_root
                provider_id = str(second_arg)
            else:
                provider_id = str(provider_id_or_history_root)
                model_id = str(second_arg)
        else:
            provider_id = str(provider_id_or_history_root)

        if not effective_root:
            return None
        root_path = Path(effective_root)
        if not root_path.is_dir():
            return None

        effective_target_cfg = target_config
        if effective_target_cfg is None and provider_id:
            effective_target_cfg = get_provider_config(provider_id)

        from agentic_debugger.application.history import JOURNAL_FILE_NAME
        from agentic_debugger.application.events import ModelRequestStatus, SessionEventKind
        from agentic_debugger.application.journal import read_session_journal

        def _find_journal(s_dir: Path) -> Optional[Path]:
            for name in (JOURNAL_FILE_NAME, "journal.jsonl", "events.jsonl", "session.jsonl"):
                p = s_dir / name
                if p.is_file():
                    return p
            return None

        # Collect session dirs sorted by modification time (newest first), bounded to 30
        try:
            candidates = [
                p for p in root_path.iterdir()
                if p.is_dir() and _find_journal(p) is not None
            ]
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            candidates = candidates[:30]
        except OSError:
            return None

        for s_dir in candidates:
            journal_path = _find_journal(s_dir)
            if journal_path is None:
                continue
            session_matches_provider = False
            last_request_success_at: Optional[str] = None

            try:
                journal = read_session_journal(journal_path)
                events = journal.events
            except Exception:
                events = ()

            if events:
                for ev in events:
                    if ev.event_kind is SessionEventKind.MODEL_CONFIGURED:
                        session_matches_provider = False
                        p_prov = ev.payload.get("provider")
                        p_model = ev.payload.get("profile_id")
                        p_api_model = ev.payload.get("provider_model_id")
                        p_endpoint = (ev.payload.get("endpoint") or "").strip().rstrip("/")
                        p_auth = ev.payload.get("auth_mode")
                        p_contract = ev.payload.get("endpoint_contract") or ev.payload.get("transport_profile")
                        p_format = ev.payload.get("api_format")
                        p_runtime_id = ev.payload.get("provider_runtime_identity")
                        p_fp = ev.payload.get("config_fingerprint")
                        p_binding_fp = ev.payload.get("model_binding_fingerprint")

                        if p_prov != provider_id:
                            continue
                        if model_id is not None and not (p_model == model_id or p_api_model == model_id):
                            continue

                        if target_binding is not None:
                            if p_binding_fp:
                                session_matches_provider = (p_binding_fp == target_binding.fingerprint())
                            else:
                                session_matches_provider = False
                        elif effective_target_cfg is not None:
                            cur_endpoint = (effective_target_cfg.base_url or "").strip().rstrip("/")
                            cur_contract = effective_target_cfg.transport_profile or TRANSPORT_GENERIC
                            cur_auth = effective_target_cfg.auth_mode
                            cur_format = effective_target_cfg.api_format
                            cur_runtime_id = provider_runtime_identity(effective_target_cfg)

                            if p_runtime_id:
                                session_matches_provider = (p_runtime_id == cur_runtime_id)
                            else:
                                if not (p_endpoint and p_auth and p_contract and p_format):
                                    session_matches_provider = False
                                else:
                                    session_matches_provider = (
                                        p_endpoint == cur_endpoint
                                        and p_auth == cur_auth
                                        and p_contract == cur_contract
                                        and p_format == cur_format
                                    )
                        else:
                            session_matches_provider = False

                    elif ev.event_kind is SessionEventKind.MODEL_REQUEST_COMPLETED:
                        if session_matches_provider:
                            status_val = ev.payload.get("status")
                            if status_val == ModelRequestStatus.OK.value or status_val == "ok":
                                last_request_success_at = ev.timestamp_utc
            elif journal_path.is_file():
                try:
                    with open(journal_path, "r", encoding="utf-8") as f:
                        for line in f:
                            if not line.strip():
                                continue
                            raw = json.loads(line)
                            kind = raw.get("kind") or raw.get("event_kind")
                            if kind in ("model.configured", "model_configured"):
                                session_matches_provider = False
                                p_prov = raw.get("provider") or raw.get("payload", {}).get("provider")
                                p_model = raw.get("model") or raw.get("profile_id") or raw.get("payload", {}).get("profile_id")
                                p_api_model = raw.get("provider_model_id") or raw.get("payload", {}).get("provider_model_id")
                                p_endpoint = (raw.get("endpoint") or raw.get("payload", {}).get("endpoint") or "").strip().rstrip("/")
                                p_auth = raw.get("auth_mode") or raw.get("payload", {}).get("auth_mode")
                                p_contract = (
                                    raw.get("endpoint_contract")
                                    or raw.get("transport_profile")
                                    or raw.get("payload", {}).get("endpoint_contract")
                                    or raw.get("payload", {}).get("transport_profile")
                                )
                                p_format = raw.get("api_format") or raw.get("payload", {}).get("api_format")
                                p_runtime_id = (
                                    raw.get("provider_runtime_identity")
                                    or raw.get("payload", {}).get("provider_runtime_identity")
                                )
                                p_fp = raw.get("config_fingerprint") or raw.get("payload", {}).get("config_fingerprint")
                                p_binding_fp = (
                                    raw.get("model_binding_fingerprint")
                                    or raw.get("payload", {}).get("model_binding_fingerprint")
                                )

                                if p_prov != provider_id:
                                    continue
                                if model_id is not None and not (p_model == model_id or p_api_model == model_id):
                                    continue

                                if target_binding is not None:
                                    if p_binding_fp:
                                        session_matches_provider = (p_binding_fp == target_binding.fingerprint())
                                    else:
                                        session_matches_provider = False
                                elif effective_target_cfg is not None:
                                    cur_endpoint = (effective_target_cfg.base_url or "").strip().rstrip("/")
                                    cur_contract = effective_target_cfg.transport_profile or TRANSPORT_GENERIC
                                    cur_auth = effective_target_cfg.auth_mode
                                    cur_format = effective_target_cfg.api_format
                                    cur_runtime_id = provider_runtime_identity(effective_target_cfg)

                                    if p_runtime_id:
                                        session_matches_provider = (p_runtime_id == cur_runtime_id)
                                    else:
                                        if not (p_endpoint and p_auth and p_contract and p_format):
                                            session_matches_provider = False
                                        else:
                                            session_matches_provider = (
                                                p_endpoint == cur_endpoint
                                                and p_auth == cur_auth
                                                and p_contract == cur_contract
                                                and p_format == cur_format
                                            )
                                else:
                                    session_matches_provider = False

                            elif kind in ("llm.request", "model_request_completed", "step.action"):
                                if session_matches_provider:
                                    st_val = raw.get("status") or raw.get("payload", {}).get("status")
                                    if st_val in ("ok", "success"):
                                        last_request_success_at = (
                                            raw.get("timestamp")
                                            or raw.get("timestamp_utc")
                                            or raw.get("payload", {}).get("timestamp")
                                        )
                except Exception:
                    pass

            if last_request_success_at:
                return last_request_success_at

        return None

    # -- Provider Status Facts -----------------------------------------------

    def get_provider_status(
        self,
        provider_id: str,
        *,
        history_root: Optional[Path | str] = None,
        sessions_root: Optional[Path | str] = None,
    ) -> ProviderStatusSnapshot:
        """Produce a truthful ProviderStatusSnapshot with distinct factual dimensions."""
        cfg = get_provider_config(provider_id)
        if cfg is None:
            raise ProviderConfigurationError(f"Provider {provider_id!r} is not configured")

        is_enabled = bool(cfg.enabled)
        is_quarantined = is_provider_quarantined(provider_id)
        is_configured = True

        cred_source = credential_source_for(provider_id)
        if cfg.auth_mode == AUTH_NONE:
            credential_ready = True
            cred_source = "none"
        else:
            credential_ready = cred_source is not None

        # Provider-level static readiness
        is_ready, readiness_reason = self.provider_readiness(provider_id)

        # Catalog state
        cat_snapshot = load_cached_catalog(provider_id)
        if cat_snapshot is not None and cat_snapshot.models:
            cat_count = len(cat_snapshot.models)
            cat_refreshed = cat_snapshot.fetched_at_utc
            cat_source = cat_snapshot.source
            try:
                fetched = datetime.fromisoformat(
                    cat_snapshot.fetched_at_utc.replace("Z", "+00:00")
                )
                from datetime import timedelta
                cat_stale = datetime.now(timezone.utc) - fetched > timedelta(days=7)
            except ValueError:
                cat_stale = False
            cached_models = cat_snapshot.models
        elif cfg.models:
            cat_count = len(cfg.models)
            cat_refreshed = cfg.last_refresh_utc
            cat_source = cfg.last_refresh_source
            cat_stale = False
            cached_models = cfg.models
        else:
            cat_count = 0
            cat_refreshed = None
            cat_source = None
            cat_stale = False
            cached_models = ()

        # Probe state bound strictly to current runtime identity
        current_identity = provider_runtime_identity(cfg)
        probe_record = self._live_probe_results.get(provider_id)
        live_verified = False
        live_verified_at = None
        live_probe_error = None
        if probe_record is not None and probe_record.get("runtime_identity") == current_identity:
            live_verified = bool(probe_record.get("verified", False))
            live_verified_at = probe_record.get("timestamp")
            live_probe_error = probe_record.get("error")

        # Runtime success fact derived from history (bound to current target_config)
        effective_history_root = history_root or sessions_root
        runtime_succeeded_at = self.inspect_last_runtime_success(
            provider_id, history_root=effective_history_root, target_config=cfg
        )

        return ProviderStatusSnapshot(
            provider_id=provider_id,
            label=cfg.name,
            base_url=cfg.base_url,
            endpoint_contract=cfg.transport_profile,
            auth_mode=cfg.auth_mode,
            api_format=cfg.api_format,
            is_configured=is_configured,
            is_enabled=is_enabled,
            is_quarantined=is_quarantined,
            credential_ready=credential_ready,
            credential_source=cred_source,
            is_provider_ready=is_ready,
            provider_readiness_reason=readiness_reason,
            is_runnable=is_ready,
            runnable_reason=readiness_reason,
            catalog_model_count=cat_count,
            catalog_refreshed_at_utc=cat_refreshed,
            catalog_refreshed_source=cat_source,
            catalog_stale=cat_stale,
            live_verified=live_verified,
            live_verified_at_utc=live_verified_at,
            live_probe_error=live_probe_error,
            runtime_succeeded_at_utc=runtime_succeeded_at,
            cached_models=cached_models,
        )

    def list_provider_statuses(
        self, *, history_root: Optional[Path | str] = None
    ) -> List[ProviderStatusSnapshot]:
        """Return truthful status snapshots for all configured providers."""
        configs = list_configured_providers()
        statuses = []
        for c in configs:
            try:
                statuses.append(self.get_provider_status(c.provider_id, history_root=history_root))
            except Exception as exc:
                err_msg = f"Status evaluation error: {type(exc).__name__}"
                statuses.append(
                    ProviderStatusSnapshot(
                        provider_id=c.provider_id,
                        label=c.name or c.provider_id,
                        base_url=c.base_url or "",
                        endpoint_contract=c.transport_profile or TRANSPORT_GENERIC,
                        auth_mode=c.auth_mode or "none",
                        api_format=c.api_format or PROTOCOL_CHAT_COMPLETIONS,
                        is_configured=True,
                        is_enabled=bool(c.enabled),
                        is_quarantined=is_provider_quarantined(c.provider_id),
                        credential_ready=False,
                        credential_source=None,
                        is_provider_ready=False,
                        provider_readiness_reason=err_msg,
                        is_runnable=False,
                        runnable_reason=err_msg,
                        catalog_model_count=0,
                        catalog_refreshed_at_utc=None,
                        catalog_refreshed_source=None,
                        catalog_stale=False,
                        catalog_error=err_msg,
                        live_verified=False,
                        live_verified_at_utc=None,
                        live_probe_error=None,
                        runtime_succeeded_at_utc=None,
                        cached_models=(),
                    )
                )
        return statuses

    # -- Model Listing -------------------------------------------------------

    def list_models(self, *, include_ollama: bool = True) -> List[Any]:
        """Return selectable models offered across configured providers."""
        from agentic_debugger.application.model_providers import list_provider_models
        return list_provider_models(include_ollama=include_ollama)

    # -- Transport and Credential Environment Execution ----------------------

    def session_credential_environment(
        self, provider_id: Optional[str]
    ) -> Optional[Dict[str, str]]:
        """Worker spawn credential forwarding authority (inherited through V2-03)."""
        if not provider_id:
            return None
        return provider_session_credential_environment(provider_id)

    def transport_environment(
        self, binding: ModelBinding
    ) -> Optional[Dict[str, str]]:
        """Adapter child credential/TLS environment (model channel only)."""
        if not binding.provider_id:
            return None
        env = provider_transport_environment(binding.provider_id)
        return dict(env) if env else None

    def create_transport(
        self,
        binding: ModelBinding,
        *,
        cancel_check: Optional[Callable[[], None]] = None,
        activity_observer: Optional[Callable[..., None]] = None,
        max_model_requests: int = _DEFAULT_MAX_MODEL_REQUESTS,
        max_controller_steps: int = _DEFAULT_MAX_CONTROLLER_STEPS,
        max_response_bytes: int = _MAX_MODEL_RESPONSE_BYTES,
    ) -> Tuple[Any, Any]:
        """Create the (CancellableJsonlCommandTransport, LiveModelConfig) pair for a binding."""
        from agentic_debugger.application.command_transport import CancellableJsonlCommandTransport
        from agentic_debugger.evaluation.live import LiveModelConfig
        from agentic_debugger.application.model_providers import (
            PROVIDER_KIND_CONFIGURED,
            PROVIDER_KIND_OLLAMA,
            resolve_provider_live_config,
        )

        if binding.route == ROUTE_OFFLINE:
            raise ModelRuntimeError("Cannot create transport for offline binding")

        # 1. Qualified Ollama Cloud ladder route (explicit ladder contract)
        if binding.route == ROUTE_QUALIFIED_LADDER:
            if binding.provider_id not in (PROVIDER_KIND_OLLAMA, "ollama"):
                raise StaleModelBindingError(
                    f"Route qualified_ladder expects provider {PROVIDER_KIND_OLLAMA!r} or 'ollama', got {binding.provider_id!r}"
                )
            from scripts.ollama_cloud_command_adapter import build_ollama_live_config
            from agentic_debugger.application.level32 import level32_model_profiles

            ollama_profile = None
            for m in level32_model_profiles():
                if m.alias == (binding.model_id or ""):
                    ollama_profile = m
                    break
            if ollama_profile is None:
                raise IncompatibleModelError(
                    f"Ollama model not in qualified roster: {binding.model_id}"
                )
            if binding.config_fingerprint is not None and ollama_profile.transport_config_fingerprint != binding.config_fingerprint:
                raise StaleModelBindingError(
                    f"Qualified model {binding.model_id!r} transport config fingerprint drifted "
                    f"(expected {binding.config_fingerprint!r}, found {ollama_profile.transport_config_fingerprint!r})"
                )

            live_config = build_ollama_live_config(
                binding.model_id or "", logical_call_ceiling=max_model_requests
            )
            if binding.tool_version and live_config.tool_version != binding.tool_version:
                raise StaleModelBindingError(
                    f"Qualified model {binding.model_id!r} tool version drifted "
                    f"(expected {binding.tool_version!r}, found {live_config.tool_version!r})"
                )

            transport = CancellableJsonlCommandTransport(
                live_config,
                max_output_bytes=max_response_bytes,
                cancel_check=cancel_check,
                activity_observer=activity_observer,
            )
            return transport, live_config

        # 2. Configured command profile store
        elif binding.route == ROUTE_CONFIGURED_PROFILE:
            if binding.provider_id not in (PROVIDER_KIND_CONFIGURED, None):
                raise StaleModelBindingError(
                    f"Route configured_profile expects provider {PROVIDER_KIND_CONFIGURED!r}, got {binding.provider_id!r}"
                )
            from agentic_debugger.application.command_config import CommandModelConfigStore

            store = CommandModelConfigStore(Path(self.config_root) if self.config_root else Path("."))
            try:
                profile = store.get(binding.model_id or "")
            except Exception as exc:
                raise StaleModelBindingError(
                    f"Command profile {binding.model_id!r} unavailable: {exc}"
                ) from exc

            # Corroborate profile facts
            if binding.config_fingerprint is not None and profile.configuration_fingerprint != binding.config_fingerprint:
                raise StaleModelBindingError(
                    f"Command profile {binding.model_id!r} configuration drifted "
                    f"(expected fingerprint {binding.config_fingerprint!r}, found {profile.configuration_fingerprint!r})"
                )
            if binding.tool_version and profile.tool_version != binding.tool_version:
                raise StaleModelBindingError(
                    f"Command profile {binding.model_id!r} tool version drifted "
                    f"(expected {binding.tool_version!r}, found {profile.tool_version!r})"
                )

            live_config = LiveModelConfig(
                model_name=profile.display_name,
                command=profile.live_command(),
                request_timeout_seconds=profile.request_timeout_seconds,
                tool_version=profile.tool_version,
            )
            transport = CancellableJsonlCommandTransport(
                live_config,
                max_output_bytes=max_response_bytes,
                cancel_check=cancel_check,
                activity_observer=activity_observer,
                cwd=profile.cwd,
                environment=dict(profile.environment) if profile.environment else None,
            )
            return transport, live_config

        # 3. Configured registry provider route (direct API or legacy CLI)
        elif binding.route in (ROUTE_DIRECT_API, ROUTE_LEGACY_CLI):
            if not binding.provider_id or binding.provider_id == PROVIDER_KIND_CONFIGURED:
                raise StaleModelBindingError(
                    f"Route {binding.route!r} requires configured provider identity, got {binding.provider_id!r}"
                )


            # Corroborate against current provider configuration
            cfg = get_provider_config(binding.provider_id)
            if cfg is None:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} is no longer configured"
                )
            if not cfg.enabled:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} has been disabled"
                )
            if is_provider_quarantined(binding.provider_id):
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} credential state requires recovery (quarantined)"
                )

            cur_identity = provider_runtime_identity(cfg)
            if cur_identity != binding.provider_runtime_identity:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} runtime configuration drifted (stale binding): "
                    f"expected {binding.provider_runtime_identity!r}, found {cur_identity!r}"
                )

            # Compare execution-critical facts:
            expected_contract = cfg.transport_profile or TRANSPORT_GENERIC
            if binding.endpoint_contract != expected_contract:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} endpoint contract drifted "
                    f"(expected {binding.endpoint_contract!r}, found {expected_contract!r})"
                )
            if binding.route == ROUTE_DIRECT_API:
                clean_binding_endpoint = (binding.endpoint or "").strip().rstrip("/")
                clean_cfg_endpoint = (cfg.base_url or "").strip().rstrip("/")
                if clean_binding_endpoint != clean_cfg_endpoint:
                    raise StaleModelBindingError(
                        f"Provider {binding.provider_id!r} endpoint drifted "
                        f"(expected {clean_binding_endpoint!r}, found {clean_cfg_endpoint!r})"
                    )
                if binding.auth_mode != cfg.auth_mode:
                    raise StaleModelBindingError(
                        f"Provider {binding.provider_id!r} auth mode drifted "
                        f"(expected {binding.auth_mode!r}, found {cfg.auth_mode!r})"
                    )
                cur_api_model = (
                    provider_api_model_id(cfg.provider_id, binding.model_id or "")
                    if binding.model_id else None
                )
                if binding.provider_model_id != cur_api_model and (binding.provider_model_id or cur_api_model):
                    raise StaleModelBindingError(
                        f"Provider {binding.provider_id!r} model {binding.model_id!r} API model id drifted "
                        f"(expected {binding.provider_model_id!r}, found {cur_api_model!r})"
                    )
                cur_proto = resolve_model_protocol(cfg.provider_id, binding.model_id or "") or cfg.api_format
                if binding.effective_protocol != cur_proto:
                    raise StaleModelBindingError(
                        f"Provider {binding.provider_id!r} effective protocol drifted "
                        f"(expected {binding.effective_protocol!r}, found {cur_proto!r})"
                    )
            elif binding.route == ROUTE_LEGACY_CLI:
                historical_profiles = (TRANSPORT_OPENCODE_GO, TRANSPORT_COMMANDCODE_GOAT)
                if binding.endpoint_contract not in historical_profiles:
                    raise StaleModelBindingError(
                        f"Legacy CLI route invalid for contract {binding.endpoint_contract!r}"
                    )

            # Re-read live config only as corroboration
            try:
                live_config, provenance = resolve_provider_live_config(
                    binding.provider_id,
                    binding.model_id or "",
                    logical_call_ceiling=max_model_requests,
                )
            except Exception as exc:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} live configuration resolution failed: {exc}"
                ) from exc

            prov_route = str(provenance.get("route") or ROUTE_DIRECT_API)
            if binding.route != prov_route:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} route drifted "
                    f"(expected {binding.route!r}, found {prov_route!r})"
                )
            if binding.config_fingerprint is not None and live_config.configuration_fingerprint != binding.config_fingerprint:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} configuration fingerprint drifted "
                    f"(expected {binding.config_fingerprint!r}, found {live_config.configuration_fingerprint!r})"
                )
            if binding.tool_version and live_config.tool_version != binding.tool_version:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} tool version drifted "
                    f"(expected {binding.tool_version!r}, found {live_config.tool_version!r})"
                )

            env = self.transport_environment(binding)
            transport = CancellableJsonlCommandTransport(
                live_config,
                max_output_bytes=max_response_bytes,
                cancel_check=cancel_check,
                activity_observer=activity_observer,
                environment=env,
            )
            return transport, live_config

        raise ModelRuntimeError(
            f"Unsupported binding route for transport creation: {binding.route!r}"
        )
