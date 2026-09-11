"""Model-gateway logical resolution and static preflight authority.

Single implementation authority for gateway resolution/preflight beneath
:class:`ModelGateway` (the sole product provider-runtime authority):

* logical provider/model resolution and route selection
  (:func:`resolve_binding`);
* provider-level static readiness (:func:`provider_readiness`);
* static execution preflight without network I/O
  (:func:`static_preflight`).

Dependency direction (acyclic)::

    contracts <- resolution <- model_gateway (facade)

This module owns no mutable gateway state.  It operates on the gateway
context passed as ``gateway`` (``gateway._vault`` for credential
readiness facts, ``gateway.config_root``/``gateway._config_root`` for
configured-profile scope) and never instantiates its own
``CredentialVault`` or live-probe store.  Existing lazy imports of
``model_providers``/``command_config``/``level32``/ollama adapters are
preserved inside function bodies exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Tuple

from agentic_debugger.application.model_gateway_contracts import (
    ROUTE_CONFIGURED_PROFILE,
    ROUTE_DIRECT_API,
    ROUTE_LEGACY_CLI,
    ROUTE_OFFLINE,
    ROUTE_QUALIFIED_LADDER,
    IncompatibleModelError,
    ModelBinding,
    ModelStaticPreflight,
    ProviderConfigurationError,
    provider_runtime_identity,
)
from agentic_debugger.application.provider_connections import (
    AUTH_BEARER,
    AUTH_NONE,
    PROTOCOL_CHAT_COMPLETIONS,
    TRANSPORT_COMMANDCODE_GOAT,
    TRANSPORT_GENERIC,
    TRANSPORT_OLLAMA_CLOUD,
    TRANSPORT_OPENCODE_GO,
    ProviderConnectionError,
    effective_model_protocol,
    effective_model_protocol_for_config,
    get_provider_config,
    is_protocol_executable,
    is_protocol_executable_for_config,
    is_provider_quarantined,
    protocol_blocker_reason,
    provider_api_model_id,
    provider_api_model_id_for_config,
)

__all__ = [
    "provider_readiness",
    "resolve_binding",
    "static_preflight",
]

def resolve_binding(
    gateway,
    provider_id: Optional[str],
    model_id: Optional[str],
    *,
    profile_id: Optional[str] = None,
    # Task 44 (repair F3): generic omission means unbounded (0 = no
    # upper-bound termination).  Finite values are honored only when
    # an explicit caller (frozen/operator/harness) supplies one.
    logical_call_ceiling: int = 0,
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
            api_model = provider_api_model_id_for_config(cfg, effective_model)
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

        api_model = provider_api_model_id_for_config(cfg, effective_model)
        historical_profiles = (TRANSPORT_OPENCODE_GO, TRANSPORT_COMMANDCODE_GOAT)
        is_historical = endpoint_contract in historical_profiles
        direct_cred_missing = not gateway._vault.readiness(cfg.provider_id).credential_ready

        if not is_historical:
            # Direct-only / Generic provider
            if direct_cred_missing:
                # Missing credentials on a direct-only provider:
                # Statically determine protocol executability without calling live resolution.
                try:
                    proto = effective_model_protocol_for_config(cfg, effective_model)
                except ProviderConnectionError as p_exc:
                    raise IncompatibleModelError(
                        f"Provider {effective_provider!r} model {effective_model!r} incompatible: {p_exc}"
                    ) from p_exc
                except Exception as p_exc:
                    raise ProviderConfigurationError(
                        f"Provider {effective_provider!r} protocol resolution error: {p_exc}"
                    ) from p_exc

                if proto is None or not is_protocol_executable_for_config(cfg, proto):
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
                    proto = effective_model_protocol_for_config(cfg, effective_model)
                except ProviderConnectionError as p_exc:
                    raise IncompatibleModelError(
                        f"Provider {effective_provider!r} model {effective_model!r} incompatible: {p_exc}"
                    ) from p_exc
                except Exception:
                    pass
                raise ProviderConfigurationError(
                    f"Provider {effective_provider!r} live configuration resolution failed: {exc}"
                ) from exc

            # Repair 24 (F2): executable coherence — the live_config
            # provenance carries the safe authority from the SAME
            # snapshot that built it.  It must equal the pre-captured
            # runtime_id, otherwise the configuration mutated during
            # resolution and the pair would mix authorities.
            _exe_auth = provenance.get("provider_runtime_identity")
            # Repair 25: real direct executables must prove authority;
            # missing/malformed authority fails closed (no fallback to CURRENT).
            _exe_route = str(provenance.get("route") or ROUTE_DIRECT_API)
            if _exe_route == ROUTE_DIRECT_API:
                import re as _re_auth
                if not isinstance(_exe_auth, str) or _re_auth.fullmatch(r"[0-9a-f]{64}", _exe_auth) is None:
                    raise ProviderConfigurationError(
                        f"Provider {effective_provider!r} executable authority is missing or malformed"
                    )
                if _exe_auth != runtime_id:
                    raise ProviderConfigurationError(
                        f"Provider {effective_provider!r} configuration changed "
                        "during resolution"
                    )
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
                    proto = effective_model_protocol_for_config(cfg, effective_model)
                except ProviderConnectionError as p_exc:
                    raise IncompatibleModelError(
                        f"Provider {effective_provider!r} model {effective_model!r} incompatible: {p_exc}"
                    ) from p_exc
                except Exception as p_exc:
                    raise ProviderConfigurationError(
                        f"Provider {effective_provider!r} protocol resolution error: {p_exc}"
                    ) from p_exc

                if proto is not None and is_protocol_executable_for_config(cfg, proto):
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

            # Repair 24 (F2): same executable-coherence gate for the
            # historical branch.
            _exe_auth2 = provenance.get("provider_runtime_identity")
            _exe_route2 = str(provenance.get("route") or ROUTE_DIRECT_API)
            if _exe_route2 == ROUTE_DIRECT_API:
                import re as _re_auth2
                if not isinstance(_exe_auth2, str) or _re_auth2.fullmatch(r"[0-9a-f]{64}", _exe_auth2) is None:
                    raise ProviderConfigurationError(
                        f"Provider {effective_provider!r} executable authority is missing or malformed"
                    )
                if _exe_auth2 != runtime_id:
                    raise ProviderConfigurationError(
                        f"Provider {effective_provider!r} configuration changed "
                        "during resolution"
                    )
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

        root_path = Path(config_root or gateway.config_root or ".")
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


def provider_readiness(gateway, provider_id: str) -> Tuple[bool, Optional[str]]:
    """Static provider-level readiness check without requiring a specific model."""
    cfg = get_provider_config(provider_id)
    if cfg is None:
        return False, f"Provider {provider_id!r} is not configured"
    if not cfg.enabled:
        return False, "Provider is disabled"
    if is_provider_quarantined(provider_id):
        return False, "Credential state requires recovery (quarantined)"
    if not gateway._vault.readiness(provider_id).credential_ready:
        return False, "No usable credential source found"
    if not cfg.base_url or not cfg.base_url.strip():
        return False, "Provider base URL is not configured"
    return True, None


def static_preflight(
    gateway,
    provider_or_binding: Any,
    model_id: Optional[str] = None,
    *,
    config_root: Optional[Any] = None,
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
            effective_root = config_root if config_root is not None else gateway.config_root
            store = CommandModelConfigStore(Path(effective_root) if effective_root else Path("."))
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

        if binding.route == ROUTE_DIRECT_API:
            clean_binding_endpoint = (binding.endpoint or "").strip().rstrip("/")
            clean_cfg_endpoint = (cfg.base_url or "").strip().rstrip("/")
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

            # Corroborate provider_api_model_id
            if binding.model_id:
                try:
                    cur_api_model = provider_api_model_id(cfg.provider_id, binding.model_id)
                except ProviderConnectionError as exc:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id,
                        is_runnable=False,
                        blocker_reason=str(exc),
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
                except Exception:
                    return ModelStaticPreflight(
                        provider_id=prov_id,
                        model_id=binding.model_id,
                        is_runnable=False,
                        blocker_reason="Provider model API id resolution failed",
                        effective_protocol=binding.effective_protocol,
                        endpoint_contract=binding.endpoint_contract,
                        route=binding.route,
                    )
            else:
                cur_api_model = None

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

            # Authoritatively resolve current effective direct protocol
            try:
                cur_proto = effective_model_protocol(cfg.provider_id, binding.model_id or "")
            except ProviderConnectionError as exc:
                return ModelStaticPreflight(
                    provider_id=prov_id,
                    model_id=binding.model_id or "",
                    is_runnable=False,
                    blocker_reason=str(exc),
                    effective_protocol=binding.effective_protocol,
                    endpoint_contract=binding.endpoint_contract,
                    route=binding.route,
                )
            except Exception:
                return ModelStaticPreflight(
                    provider_id=prov_id,
                    model_id=binding.model_id or "",
                    is_runnable=False,
                    blocker_reason="Provider model protocol resolution failed",
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

            cred_ready = gateway._vault.readiness(prov_id).credential_ready
            if cfg.auth_mode != AUTH_NONE and not cred_ready:
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
            binding = gateway.resolve(provider_id, model_id)
            return gateway.static_preflight(binding)
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

    cred_source = gateway._vault.readiness(provider_id).source_kind
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
