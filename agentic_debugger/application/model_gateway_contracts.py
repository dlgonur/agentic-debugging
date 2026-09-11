"""Model-gateway immutable contracts and provenance authority.

Single implementation authority for the product provider-runtime
contracts beneath :class:`ModelGateway`:

* explicit route identities (``direct_api`` / ``legacy_cli`` /
  ``configured_profile`` / ``offline`` / ``qualified_ladder``);
* safe provider-runtime identity/fingerprinting
  (:func:`provider_runtime_identity`, :func:`is_loopback_url`);
* gateway error taxonomy (fail-closed, credential-free);
* :class:`ModelBinding` immutable safe provenance;
* :class:`ProviderStatusSnapshot` truthful status facts;
* :class:`ModelStaticPreflight` static readiness facts.

Dependency direction (acyclic)::

    contracts <- resolution / status / transport <- model_gateway (facade)

This module never imports the gateway facade, the credential vault, or
the resolution/status/transport helpers — not even lazily — so
lower-level provider/credential code may resolve
:func:`provider_runtime_identity` through the gateway re-export without
creating a cycle.  ``ModelBinding`` validation keeps its existing lazy
imports of ``model_providers``/``provider_connections`` inside
``__post_init__`` exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from agentic_debugger.application.events import contains_credential_shape
from agentic_debugger.application.provider_connections import (
    TRANSPORT_GENERIC,
    DiscoveredProviderModel,
    _PROVIDER_CREDENTIAL_SOURCE_LABELS,
)

__all__ = [
    "CatalogProbeError",
    "CredentialUnavailableError",
    "EndpointUnreachableError",
    "IncompatibleModelError",
    "IncoherentCredentialBindingError",
    "ModelBinding",
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
    "contains_credential_shape",
    "is_loopback_url",
    "provider_runtime_identity",
]

#: Explicit route identities recorded in ModelBinding and durable provenance.
ROUTE_DIRECT_API = "direct_api"
ROUTE_LEGACY_CLI = "legacy_cli"
ROUTE_CONFIGURED_PROFILE = "configured_profile"
ROUTE_OFFLINE = "offline"
ROUTE_QUALIFIED_LADDER = "qualified_ladder"


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


class IncoherentCredentialBindingError(ModelGatewayError):
    """A CredentialBinding does not certify the paired ModelBinding's authority.

    Before any secret materialization, the credential authority must be
    coherent with the model binding: same provider id, same provider
    runtime authority, compatible auth mode, and a route-compatible
    source kind.  A valid binding for provider Q never reaches provider
    P's adapter child.
    """


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
