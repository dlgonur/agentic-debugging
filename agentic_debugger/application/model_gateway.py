"""V2-03 ModelGateway product runtime authority (decomposed facade).

:class:`ModelGateway` remains the single product provider-runtime
authority.  Cohesive behavior is delegated to single-authority helpers;
nothing here duplicates that state:

* :mod:`model_gateway_contracts` -- immutable routes, runtime identity,
  error taxonomy, :class:`ModelBinding`, status/preflight contracts;
* :mod:`model_gateway_resolution` -- logical resolution, route selection,
  static preflight;
* :mod:`model_gateway_status` -- explicit probes, catalog refresh,
  durable-history inspection, status snapshots, model listing;
* :mod:`model_gateway_transport` -- credential-coherence enforcement,
  credential egress, route-specific transport construction.

Dependency direction (acyclic)::

    contracts <- resolution <- status
    contracts <- transport
    contracts/resolution/status/transport <- model_gateway (facade)

``ModelGateway`` owns the single mutable authority: ``config_root``,
``CredentialVault`` binding/reference, in-memory explicit live-probe
results, and canonical/default singleton behavior.  Helpers operate on
the gateway context passed as ``gateway`` and never own those stores.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from agentic_debugger.application.credential_vault import (
    CredentialReadiness,
    CredentialVault,
    StaleCredentialBindingError,
)
from agentic_debugger.application.model_gateway_contracts import (
    CatalogProbeError,
    CredentialUnavailableError,
    EndpointUnreachableError,
    IncompatibleModelError,
    IncoherentCredentialBindingError,
    ModelBinding,
    ModelGatewayError,
    ModelRuntimeError,
    ModelStaticPreflight,
    ProtocolViolationError,
    ProviderConfigurationError,
    ProviderHttpRejectionError,
    ProviderStatusSnapshot,
    ROUTE_CONFIGURED_PROFILE,
    ROUTE_DIRECT_API,
    ROUTE_LEGACY_CLI,
    ROUTE_OFFLINE,
    ROUTE_QUALIFIED_LADDER,
    StaleModelBindingError,
    contains_credential_shape,
    is_loopback_url,
    provider_runtime_identity,
)
from agentic_debugger.application.model_gateway_resolution import (
    provider_readiness as _resolution_provider_readiness,
)
from agentic_debugger.application.model_gateway_resolution import (
    resolve_binding as _resolution_resolve,
)
from agentic_debugger.application.model_gateway_resolution import (
    static_preflight as _resolution_preflight,
)
from agentic_debugger.application.model_gateway_status import (
    get_provider_status as _status_get_provider,
)
from agentic_debugger.application.model_gateway_status import (
    inspect_last_runtime_success as _status_inspect_history,
)
from agentic_debugger.application.model_gateway_status import (
    list_models as _status_list_models,
)
from agentic_debugger.application.model_gateway_status import (
    list_provider_statuses as _status_list_providers,
)
from agentic_debugger.application.model_gateway_status import (
    probe_credential as _status_probe_credential,
)
from agentic_debugger.application.model_gateway_status import (
    probe_reachability as _status_probe,
)
from agentic_debugger.application.model_gateway_status import (
    refresh_catalog as _status_refresh,
)
from agentic_debugger.application.model_gateway_transport import (
    _MAX_MODEL_RESPONSE_BYTES,
    assert_credential_binding_coherent,
    create_transport as _transport_create,
)
from agentic_debugger.application.model_gateway_transport import (
    credential_readiness as _transport_cred_readiness,
)
from agentic_debugger.application.model_gateway_transport import (
    session_credential_environment as _transport_session_env,
)
from agentic_debugger.application.model_gateway_transport import (
    transport_environment as _transport_env,
)
from agentic_debugger.application.provider_connections import (
    ProviderCatalogSnapshot,
    ProviderConfig,
    is_known_provider as _is_known_provider,
)

__all__ = [
    "CatalogProbeError",
    "CredentialUnavailableError",
    "EndpointUnreachableError",
    "IncompatibleModelError",
    "IncoherentCredentialBindingError",
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
    "assert_credential_binding_coherent",
    "is_loopback_url",
    "provider_runtime_identity",
]


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
        self._config_root = config_root
        # V2-04: the single provider-secret authority beneath this gateway.
        self._vault = CredentialVault.default()
        # In-memory record of explicit live probe outcomes during application run
        # Key: provider_id -> {"verified": bool, "timestamp": str, "error": Optional[str]}
        self._live_probe_results: Dict[str, Dict[str, Any]] = {}

    @property
    def config_root(self) -> Optional[Any]:
        """Configured-profile store root for this gateway instance (immutable)."""
        return self._config_root

    @classmethod
    def default(cls, config_root: Optional[Any] = None) -> ModelGateway:
        """Process-level default gateway or context-scoped gateway.

        - ``ModelGateway.default()``: returns the stable, process-global
          canonical gateway singleton used for UI provider-runtime status,
          credential readiness, and live probe state.  Its ``config_root`` is
          always ``None`` and is never mutated by contextual callers.
        - ``ModelGateway.default(config_root=X)``: returns a context-scoped
          gateway instance bound to ``config_root=X`` (e.g. for a Local Project
          session or configured profile store).  Contextual acquisition never
          mutates the canonical default singleton or sibling gateway contexts.
        """
        if config_root is not None:
            return cls(config_root=config_root)
        if cls._default_instance is None:
            cls._default_instance = cls(config_root=None)
        return cls._default_instance

    @classmethod
    def _reset_default_instance(cls) -> None:
        """Reset canonical process singleton (for test isolation)."""
        cls._default_instance = None

    def invalidate_provider(self, provider_id: str) -> None:
        """Clear cached probe results when a provider configuration changes."""
        self._live_probe_results.pop(provider_id, None)

    @staticmethod
    def is_known_provider(provider_id: Optional[str]) -> bool:
        """Whether a provider identity is known in durable configuration or presets."""
        if not provider_id or not isinstance(provider_id, str):
            return False
        return _is_known_provider(provider_id)

    # -- Resolution ----------------------------------------------------------

    def resolve(
        self,
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
        return _resolution_resolve(
            self,
            provider_id,
            model_id,
            profile_id=profile_id,
            logical_call_ceiling=logical_call_ceiling,
            request_timeout_seconds=request_timeout_seconds,
            config_root=config_root,
            is_ollama=is_ollama,
            ollama_alias=ollama_alias,
        )

    # -- Static Preflight (no network) ---------------------------------------

    def provider_readiness(self, provider_id: str) -> Tuple[bool, Optional[str]]:
        """Static provider-level readiness check without requiring a specific model."""
        return _resolution_provider_readiness(self, provider_id)

    def static_preflight(
        self,
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
        return _resolution_preflight(
            self, provider_or_binding, model_id, config_root=config_root
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
        """Perform an explicit reachability probe of the provider endpoint.

        V2-04: the probe credential is materialized through the
        CredentialVault (safe binding -> one lease -> explicit credential
        at this trusted HTTP boundary).  A missing/quarantined/stale
        credential authority fails safely BEFORE any HTTP attempt; the
        low-level probe never rediscovers a credential for this product
        path.
        """
        return _status_probe(
            self, provider_id, model_id=model_id, timeout_seconds=timeout_seconds, engine=engine
        )

    def _probe_credential(self, provider_id: str) -> Optional[str]:
        """Vault-issued credential for the trusted provider HTTP boundary.

        The ONLY normal product materialization path for the in-process
        provider HTTP operations (probe / catalog refresh): safe binding
        -> one lease -> ``lease.reveal()`` HERE and only here.  Returns
        ``None`` intentionally for no-auth providers; raises typed vault
        errors (credential-free) when a required authority is missing,
        quarantined, stale, or otherwise unavailable.
        """
        return _status_probe_credential(self, provider_id)

    def refresh_catalog(self, provider_id: str) -> ProviderCatalogSnapshot:
        """Explicitly refresh the provider's live model catalog (GET /models).

        V2-04: the catalog credential is materialized through the
        CredentialVault (safe binding -> one lease -> explicit credential
        at this trusted HTTP boundary).  A missing/quarantined/stale
        credential authority fails safely BEFORE any HTTP attempt; the
        low-level refresh never rediscovers a credential for this product
        path.
        """
        return _status_refresh(self, provider_id)

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
        return _status_inspect_history(
            provider_id_or_history_root,
            second_arg,
            model_id,
            history_root=history_root,
            sessions_root=sessions_root,
            target_config=target_config,
            target_binding=target_binding,
        )

    # -- Provider Status Facts -----------------------------------------------

    def get_provider_status(
        self,
        provider_id: str,
        *,
        history_root: Optional[Path | str] = None,
        sessions_root: Optional[Path | str] = None,
    ) -> ProviderStatusSnapshot:
        """Produce a truthful ProviderStatusSnapshot with distinct factual dimensions."""
        return _status_get_provider(
            self, provider_id, history_root=history_root, sessions_root=sessions_root
        )

    def list_provider_statuses(
        self, *, history_root: Optional[Path | str] = None
    ) -> List[ProviderStatusSnapshot]:
        """Return truthful status snapshots for all configured providers."""
        return _status_list_providers(self, history_root=history_root)

    # -- Model Listing -------------------------------------------------------

    def list_models(self, *, include_ollama: bool = True) -> List[Any]:
        """Return selectable models offered across configured providers."""
        return _status_list_models(include_ollama=include_ollama)

    # -- Transport and Credential Environment Execution ----------------------

    def credential_readiness(self, provider_id: str) -> CredentialReadiness:
        """Safe credential readiness facts via the CredentialVault (V2-04).

        The single credential-fact authority for this gateway: Provider
        Manager and status snapshots consume these facts instead of
        consulting credential stores or environment state directly.
        """
        return _transport_cred_readiness(self, provider_id)

    def session_credential_environment(
        self, provider_id: Optional[str]
    ) -> Optional[Dict[str, str]]:
        """Worker spawn credential forwarding authority (vault-owned, V2-04)."""
        return _transport_session_env(self, provider_id)

    def transport_environment(
        self,
        binding: ModelBinding,
        credential_binding: Optional[Any] = None,
        credential_ticket: Optional[str] = None,
        session_id: Optional[str] = None,
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

        ``session_id`` is the stable non-secret transport/session identity
        for providers whose runtime profile requires session-scoped
        metadata (OpenCode Go).  It is merged alongside the credential
        channel; generic providers never receive provider-specific values.

        Repair 23: a retained ticket is inseparable from its binding (a
        ticket without its binding fails closed before materialization);
        retained-ticket egress re-authorizes the SAFE binding against
        CURRENT provider configuration; and a registry ModelBinding whose
        captured provider runtime identity no longer equals CURRENT
        configuration fails stale here (defense in depth — never rely
        solely on an earlier create_transport validation).
        """
        return _transport_env(
            self,
            binding,
            credential_binding=credential_binding,
            credential_ticket=credential_ticket,
            session_id=session_id,
        )

    def create_transport(
        self,
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
        session_id: Optional[str] = None,
    ) -> Tuple[Any, Any]:
        """Create the (CancellableJsonlCommandTransport, LiveModelConfig) pair.

        ``credential_binding`` (optional, V2-04): the session-fixed safe
        credential authority established at the SessionLaunch boundary;
        ``credential_ticket`` (repair 22): the opaque session ticket that
        redeems the lease resolved and pinned at that boundary.  The pair
        is proven coherent with the ModelBinding (provider, runtime
        authority, auth mode, route/source compatibility) before any
        secret materialization.

        ``session_id`` (optional): the stable non-secret transport/session
        identity for providers whose runtime profile requires
        session-scoped metadata (OpenCode Go ``x-opencode-session``).
        Omitted identities are issued once per transport; supplied
        identities must be well-formed and are reused verbatim across the
        session's repeated requests.
        """
        return _transport_create(
            self,
            binding,
            cancel_check=cancel_check,
            activity_observer=activity_observer,
            max_model_requests=max_model_requests,
            max_controller_steps=max_controller_steps,
            max_response_bytes=max_response_bytes,
            credential_binding=credential_binding,
            credential_ticket=credential_ticket,
            config_root=config_root,
            session_id=session_id,
        )
