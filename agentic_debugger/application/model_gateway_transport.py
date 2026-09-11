"""Model-gateway transport and credential-egress authority.

Single implementation authority beneath :class:`ModelGateway` for the
trusted credential-egress and transport-construction boundary:

* credential-binding coherence enforcement
  (:func:`assert_credential_binding_coherent`);
* credential readiness/session credential environment access
  (:func:`credential_readiness`,
  :func:`session_credential_environment`);
* transport environment materialization
  (:func:`transport_environment`);
* route-specific transport construction (:func:`create_transport`).

Security contract (preserved exactly):

* :class:`CredentialVault` remains the sole credential authority; this
  module never caches API keys and never copies secret values into
  bindings, snapshots, errors, logs, events, or review artifacts;
* a retained credential ticket without its required binding fails closed;
* a stale provider-runtime identity fails before credential
  materialization where the current contract requires that ordering;
* route-specific checks, fingerprint checks, tool/version checks, and
  request/response limits are enforced without fallback transports.

Dependency direction (acyclic)::

    contracts <- transport <- model_gateway (facade)

This module owns no mutable gateway state and no credential store.  It
operates on the gateway context passed as ``gateway``
(``gateway._vault``, ``gateway._config_root``/``gateway.config_root``)
and never instantiates its own ``CredentialVault`` or live-probe store.
Existing lazy imports (credential source kinds, command transport, live
evaluation config, provider registry, command profiles, ladder
adapters) are preserved inside function bodies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from agentic_debugger.application.credential_vault import CredentialReadiness
from agentic_debugger.application.model_gateway_contracts import (
    ROUTE_CONFIGURED_PROFILE,
    ROUTE_DIRECT_API,
    ROUTE_LEGACY_CLI,
    ROUTE_OFFLINE,
    ROUTE_QUALIFIED_LADDER,
    IncoherentCredentialBindingError,
    IncompatibleModelError,
    ModelBinding,
    ModelRuntimeError,
    StaleModelBindingError,
    provider_runtime_identity,
)
from agentic_debugger.application.provider_connections import (
    TRANSPORT_COMMANDCODE_GOAT,
    TRANSPORT_GENERIC,
    TRANSPORT_OPENCODE_GO,
    ProviderConnectionError,
    effective_model_protocol,
    get_provider_config,
    is_provider_quarantined,
    provider_api_model_id,
)

__all__ = [
    "assert_credential_binding_coherent",
    "create_transport",
    "credential_readiness",
    "session_credential_environment",
    "transport_environment",
]

#: Task 44: unbounded sentinel for provider-adapter logical ceilings
#: (0 = no upper-bound termination).  The historical finite defaults
#: below are retained as provenance constants only; generic execution
#: passes 0/None and never consults them.
_DEFAULT_MAX_MODEL_REQUESTS = 64
_DEFAULT_MAX_CONTROLLER_STEPS = 128
_DEFAULT_MAX_RETRIES = 2
_MAX_MODEL_RESPONSE_BYTES = 32 * 1024

#: Task 44: unbounded sentinel for provider-adapter logical ceilings
#: (0 = no upper-bound termination).  The historical finite defaults
#: above are retained as provenance constants only; generic execution
#: passes 0/None and never consults them.
_UNBOUNDED_LOGICAL_CEILING = 0

def assert_credential_binding_coherent(
    model_binding: Any, credential_binding: Any
) -> None:
    """Prove a CredentialBinding is coherent with its ModelBinding (repair 22, F3).

    Applied at BOTH trust boundaries — SessionLaunch construction and the
    ModelGateway transport boundary (defense in depth for direct callers)
    — before any secret materialization:

    - same provider id;
    - for registry routes: same provider runtime authority and the same
      auth mode;
    - route/source-kind compatibility: a direct route never accepts an
      ``external_cli`` authority; a legacy CLI route accepts only the
      external/none authority; profile/ladder/offline routes accept no
      registry credential authority at all.
    """
    from agentic_debugger.application.credential_vault import (
        CREDENTIAL_SOURCE_EXTERNAL_CLI,
        CREDENTIAL_SOURCE_NONE,
    )

    if credential_binding is None:
        return
    if model_binding is None:
        raise IncoherentCredentialBindingError(
            "a credential binding was supplied without a model binding"
        )
    if credential_binding.provider_id != model_binding.provider_id:
        raise IncoherentCredentialBindingError(
            "the credential binding certifies a different provider than the "
            "model binding"
        )
    route = model_binding.route
    if route in (ROUTE_DIRECT_API, ROUTE_LEGACY_CLI):
        if credential_binding.provider_authority != model_binding.provider_runtime_identity:
            raise StaleModelBindingError(
                "the credential binding's provider runtime authority does not "
                "match the model binding (stale pair)"
            )
    if route == ROUTE_DIRECT_API and credential_binding.auth_mode != model_binding.auth_mode:
        # On the direct route the credential authenticates the HTTP
        # contract itself; its auth mode must equal the model binding's.
        # (A legacy CLI binding records the provider auth mode as safe
        # metadata only — the model binding legitimately carries none.)
        raise IncoherentCredentialBindingError(
            "the credential binding's authentication mode does not match "
            "the model binding"
        )
    if route == ROUTE_DIRECT_API and credential_binding.source_kind == (
        CREDENTIAL_SOURCE_EXTERNAL_CLI
    ):
        raise IncoherentCredentialBindingError(
            "an external CLI credential authority does not authorize a "
            "direct-API credential lease"
        )
    if route == ROUTE_LEGACY_CLI and credential_binding.source_kind not in (
        CREDENTIAL_SOURCE_EXTERNAL_CLI,
        CREDENTIAL_SOURCE_NONE,
    ):
        raise IncoherentCredentialBindingError(
            "the legacy CLI route uses the external CLI credential authority; "
            "a raw API credential binding is not authorized for this route"
        )
    if route in (ROUTE_CONFIGURED_PROFILE, ROUTE_QUALIFIED_LADDER, ROUTE_OFFLINE):
        raise IncoherentCredentialBindingError(
            "the session route does not accept a registry-provider credential "
            "authority"
        )




def credential_readiness(gateway, provider_id: str) -> CredentialReadiness:
    """Safe credential readiness facts via the CredentialVault (V2-04).

    The single credential-fact authority for this gateway: Provider
    Manager and status snapshots consume these facts instead of
    consulting credential stores or environment state directly.
    """
    return gateway._vault.readiness(provider_id)


def session_credential_environment(
    gateway, provider_id: Optional[str]
) -> Optional[Dict[str, str]]:
    """Worker spawn credential forwarding authority (vault-owned, V2-04)."""
    if not provider_id:
        return None
    return gateway._vault.session_forwarding_environment(provider_id)


def transport_environment(
    gateway,
    binding: ModelBinding,
    credential_binding: Optional[Any] = None,
    credential_ticket: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """Adapter child credential environment (model channel only, V2-04).

    Resolved through the CredentialVault.  When the session-fixed
    ``credential_binding`` (from the SessionLaunch) is supplied, the
    lease is materialized EXACTLY from that issued session authority —
    durable state changed after SESSION_STARTED can never outrank it.
    Otherwise a fresh lease is resolved for the binding's route:
    no-auth and external-CLI-authority routes intentionally forward
    nothing; a direct route with a missing/stale authority FAILS
    CLOSED here rather than producing a no-credential environment.

    Repair 23: a retained ticket is inseparable from its binding (a
    ticket without its binding fails closed before materialization);
    retained-ticket egress re-authorizes the SAFE binding against
    CURRENT provider configuration; and a registry ModelBinding whose
    captured provider runtime identity no longer equals CURRENT
    configuration fails stale here (defense in depth — never rely
    solely on an earlier create_transport validation).
    """
    if not binding.provider_id:
        return None
    # Finding 1: a ticket without its binding authority is never
    # redeemed — fail closed BEFORE any vault access.
    if credential_ticket is not None and credential_binding is None:
        raise IncoherentCredentialBindingError(
            "a session credential ticket requires its credential binding"
        )
    assert_credential_binding_coherent(binding, credential_binding)
    # Finding 2: this public trusted egress boundary itself fails
    # closed on stale model authority BEFORE credential
    # materialization.  Safe comparison only — never a secret value.
    if binding.route in (ROUTE_DIRECT_API, ROUTE_LEGACY_CLI):
        if binding.provider_runtime_identity is not None:
            current_cfg = get_provider_config(binding.provider_id)
            current_identity = (
                provider_runtime_identity(current_cfg)
                if current_cfg is not None
                else None
            )
            if current_identity != binding.provider_runtime_identity:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} runtime configuration "
                    "drifted (stale model binding)"
                )
    # Finding 2: the SAFE credential binding must still certify
    # CURRENT provider authority before ANY retained or fresh egress.
    if credential_binding is not None:
        gateway._vault.authorize_binding_for_transport(credential_binding)
    return gateway._vault.transport_materialization(
        binding.provider_id,
        route=binding.route,
        credential_binding=credential_binding,
        credential_ticket=credential_ticket,
    )


def create_transport(
    gateway,
    binding: ModelBinding,
    *,
    cancel_check: Optional[Callable[[], None]] = None,
    activity_observer: Optional[Callable[..., None]] = None,
    # Task 44 (repair F3): generic omission means unbounded (0 = no
    # upper-bound termination in the corroborating re-resolution).
    max_model_requests: int = 0,
    max_controller_steps: int = 0,
    max_response_bytes: int = _MAX_MODEL_RESPONSE_BYTES,
    credential_binding: Optional[Any] = None,
    credential_ticket: Optional[str] = None,
    config_root: Optional[Any] = None,
) -> Tuple[Any, Any]:
    """Create the (CancellableJsonlCommandTransport, LiveModelConfig) pair.

    ``credential_binding`` (optional, V2-04): the session-fixed safe
    credential authority established at the SessionLaunch boundary;
    ``credential_ticket`` (repair 22): the opaque session ticket that
    redeems the lease resolved and pinned at that boundary.  The pair
    is proven coherent with the ModelBinding (provider, runtime
    authority, auth mode, route/source compatibility) before any
    secret materialization.
    """
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

        effective_root = config_root if config_root is not None else gateway.config_root
        store = CommandModelConfigStore(Path(effective_root) if effective_root else Path("."))
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

        # Repair 22 (F3): prove the credential authority is coherent
        # with THIS model binding BEFORE any corroboration, live-config
        # resolution, or secret materialization.
        assert_credential_binding_coherent(binding, credential_binding)


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
            try:
                cur_api_model = (
                    provider_api_model_id(cfg.provider_id, binding.model_id or "")
                    if binding.model_id else None
                )
            except ProviderConnectionError as exc:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} model API id resolution failed: {exc}"
                ) from exc
            except Exception as exc:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} model API id resolution failed: {exc}"
                ) from exc

            if binding.provider_model_id != cur_api_model and (binding.provider_model_id or cur_api_model):
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} model {binding.model_id!r} API model id drifted "
                    f"(expected {binding.provider_model_id!r}, found {cur_api_model!r})"
                )

            try:
                cur_proto = effective_model_protocol(cfg.provider_id, binding.model_id or "")
            except ProviderConnectionError as exc:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} model {binding.model_id!r} direct protocol unavailable: {exc}"
                ) from exc
            except Exception as exc:
                raise StaleModelBindingError(
                    f"Provider {binding.provider_id!r} direct protocol resolution failed: {exc}"
                ) from exc

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

        env = gateway.transport_environment(
            binding,
            credential_binding=credential_binding,
            credential_ticket=credential_ticket,
        )
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
