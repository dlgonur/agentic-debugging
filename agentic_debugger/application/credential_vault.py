"""V2-04 CredentialVault: the provider-secret authority (binding vs lease).

This module is the single product authority for provider credential
SECRETS.  It separates two concepts that must never be conflated:

- :class:`CredentialBinding` — safe, non-secret, serializable credential
  identity/provenance (provider identity, credential source kind, safe
  source reference, provider runtime authority identity).  Bindings may be
  held, compared, fingerprinted, and fixed at session start.

- :class:`CredentialLease` — an ephemeral, secret-bearing runtime
  materialization resolved ONCE from the accepted backend.  A lease exists
  only inside trusted process memory, is non-serializable (pickle/copy/
  deepcopy fail closed), has no secret-bearing repr, and exposes only
  narrowly-scoped capability methods for trusted provider execution (the
  model-adapter child environment materialization and, for the in-process
  provider HTTP paths, an explicitly documented reveal boundary).

Authority model (ADR 0001 / V2 Plan §6.2, §10, §11 V2-04):

- The VAULT owns secret retrieval/materialization.  Product, session, UI,
  and provider-configuration code owns only safe references and metadata
  (:class:`CredentialBinding`, readiness facts).
- The durable OS secure store (Windows Credential Manager), the
  process-local session store, the endpoint-bound ambient environment
  source, the consumable CLI auth store, and the external CLI credential
  authority are the accepted backends beneath this façade
  (``agentic_debugger.application.provider_connections``).  The binding
  rules — source priority, canonical endpoint binding, and credential
  quarantine — are the accepted provider-core rules, invoked verbatim.
- A credential bound under one provider runtime authority identity is
  never silently reused for a different authority: resolving a binding
  whose recorded authority no longer matches current configuration fails
  closed as stale.  Quarantine always blocks resolution.
- Resolution is once-per-resolution-request and session-stable by
  construction: the caller (the session's model transport) resolves the
  lease once when the session binding is established and reuses the fixed
  materialization; later changes to the underlying store cannot drift an
  already-resolved lease.

Architecture (one-way dependency graph):

* :mod:`agentic_debugger.application.credential_contracts` — the safe
  contract layer: source-kind vocabulary, error classes,
  ``CredentialBinding``/``CredentialLease``/``CredentialReadiness``, the
  canonical safe source-identity helpers, and the source-faithful value
  resolution beneath the vault;
* this module — the single stateful :class:`CredentialVault` authority
  (readiness/binding inspection, lease resolution and retention, session
  forwarding, transport materialization, save/revoke transactions) and
  the public import surface.

Secret-safety invariants (all enforced here and covered by tests):

- No credential VALUE ever appears in a :class:`CredentialBinding`
  (fields, mapping, fingerprint, or repr), in a
  :class:`CredentialReadiness`, in any error raised by this module, or in
  a :class:`CredentialLease` repr.
- :class:`CredentialLease` cannot be serialized, copied, compared by
  value, logged by this module, or journaled.  Its only egress points are
  the explicit capability methods documented below.
- No broad enumeration of raw secret values exists (no
  ``get_all_secrets()``/``dump()``/value-bearing ``to_mapping()``).

This is product/provider security infrastructure only: it never changes
scientific qualification, frozen treatment identity, or evidence formats.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agentic_debugger.application import provider_config as _provider_config
from agentic_debugger.application import provider_credentials as _credentials
from agentic_debugger.application import provider_identity as _identity
from agentic_debugger.application.credential_contracts import (
    CREDENTIAL_SOURCE_CLI_AUTH_STORE,
    CREDENTIAL_SOURCE_ENVIRONMENT,
    CREDENTIAL_SOURCE_EXTERNAL_CLI,
    CREDENTIAL_SOURCE_FORWARDED_SESSION,
    CREDENTIAL_SOURCE_NONE,
    CREDENTIAL_SOURCE_SAVED,
    CREDENTIAL_SOURCE_SESSION_KEY,
    CREDENTIAL_SOURCE_SESSION_MEMORY,
    CredentialBinding,
    CredentialLease,
    CredentialReadiness,
    CredentialRouteError,
    CredentialUnavailableError,
    CredentialVaultError,
    StaleCredentialBindingError,
    _AUTHORITY_RE,
    _ENDPOINT_BOUND_SOURCE_KINDS,
    _MATERIALABLE_SOURCE_KINDS,
    _canonical_source_ref,
    _cli_auth_store_location_fingerprint,
    _external_cli_ref_for_profile,
    _provider_authority,
    _resolve_bound_value,
)

__all__ = [
    "CREDENTIAL_SOURCE_EXTERNAL_CLI",
    "CREDENTIAL_SOURCE_NONE",
    "CredentialBinding",
    "CredentialLease",
    "CredentialReadiness",
    "CredentialRouteError",
    "CredentialUnavailableError",
    "CredentialVault",
    "CredentialVaultError",
    "StaleCredentialBindingError",
]


class CredentialVault:
    """The product credential-secret authority façade.

    Wraps the accepted durable/ephemeral/external credential backends
    (``provider_connections``) behind one narrow, safe interface:

    - :meth:`safe_binding` / :meth:`readiness` — inspect binding and
      readiness facts (no values);
    - :meth:`resolve_lease` — resolve one ephemeral
      :class:`CredentialLease` for trusted provider execution;
    - :meth:`session_forwarding_environment` — the private UI→worker
      credential hop materialization;
    - :meth:`transport_materialization` — the trusted model-channel child
      environment derived from a freshly resolved lease;
    - :meth:`commit_provider_save` — the atomic provider+credential save
      transaction (quarantine-armed, rollback-safe);
    - :meth:`revoke_credential` — local credential deletion/revocation;
    - :meth:`external_cli_authority` — safe representation of the
      external CLI credential authority for legacy CLI routes.

    There is intentionally NO value-enumeration API: no method returns
    secret material for more than one provider, and no method returns raw
    store contents.
    """

    _default_instance: Optional["CredentialVault"] = None

    def __init__(self) -> None:
        # Process-private launch-lease registry (repair 22, F6): opaque
        # random tickets -> (lease, binding) resolved at the authoritative
        # session-start boundary.  Never serialized, never journaled;
        # entries are released on redemption.
        self._session_leases: Dict[str, tuple] = {}

    @classmethod
    def default(cls) -> "CredentialVault":
        if cls._default_instance is None:
            cls._default_instance = cls()
        return cls._default_instance

    # -- Inspection (safe facts only) ----------------------------------------

    def readiness(self, provider_id: str) -> CredentialReadiness:
        """Safe readiness facts for one provider.

        Truthful semantics preserved from the accepted provider core:
        configured-but-quarantined stays ``is_configured``-shaped through
        the provider configuration itself while ``credential_ready`` is
        false and ``recovery_required`` is true; a no-auth provider is
        ready by construction without fabricating a credential.
        """
        from agentic_debugger.application.provider_identity import (
            AUTH_NONE,
        )
        from agentic_debugger.application.provider_credentials import (
            provider_endpoint_binding_valid,
            provider_environment_variable,
        )

        cfg = _provider_config.get_provider_config(provider_id)
        if cfg is None:
            return CredentialReadiness(
                provider_id=provider_id,
                is_configured=False,
                auth_mode=None,
                credential_ready=False,
                source_kind=None,
                recovery_required=False,
                rebinding_required=False,
                reason=f"Provider {provider_id!r} is not configured",
                is_enabled=False,
            )
        if not cfg.enabled:
            # V2-03 factual vocabulary: a disabled provider remains
            # durably CONFIGURED; enabled and credential-ready are the
            # separate facts that are false here.
            return CredentialReadiness(
                provider_id=provider_id,
                is_configured=True,
                auth_mode=cfg.auth_mode,
                credential_ready=False,
                source_kind=None,
                recovery_required=False,
                rebinding_required=False,
                reason="Provider is disabled (edit provider to re-enable it)",
                is_enabled=False,
            )
        if _provider_config.is_provider_quarantined(provider_id):
            return CredentialReadiness(
                provider_id=provider_id,
                is_configured=True,
                auth_mode=cfg.auth_mode,
                credential_ready=False,
                source_kind=None,
                recovery_required=True,
                rebinding_required=False,
                reason=(
                    "Credential state requires recovery. "
                    "Edit provider and save an API key again."
                ),
            )
        if cfg.auth_mode == AUTH_NONE:
            return CredentialReadiness(
                provider_id=provider_id,
                is_configured=True,
                auth_mode=cfg.auth_mode,
                credential_ready=True,
                source_kind=CREDENTIAL_SOURCE_NONE,
                recovery_required=False,
                rebinding_required=False,
                reason=None,
            )

        source = _credentials.credential_source_for(provider_id)
        if source is not None:
            return CredentialReadiness(
                provider_id=provider_id,
                is_configured=True,
                auth_mode=cfg.auth_mode,
                credential_ready=True,
                source_kind=source,
                recovery_required=False,
                rebinding_required=False,
                reason=None,
            )

        # Not ready: distinguish endpoint-binding invalidation (ambient
        # sources are canonical-endpoint-bound) from a plain missing
        # credential, using the accepted provider-core rules verbatim.
        from agentic_debugger.application.provider_identity import (
            _contract_for_config,
        )

        contract = _contract_for_config(cfg)
        rebinding_required = bool(
            contract is not None
            and contract.env_var
            and not provider_endpoint_binding_valid(provider_id)
        )
        if rebinding_required:
            reason = (
                "Credential binding requires recovery — the endpoint changed; "
                "re-enter the API key for the new endpoint"
            )
        elif contract is not None and contract.env_var:
            reason = (
                f"Not connected — direct API needs {contract.env_var} or an API key "
                "(edit provider to add an API key)"
            )
        else:
            reason = (
                "Not connected — no usable credential source found "
                "(edit provider to add an API key)"
            )
        return CredentialReadiness(
            provider_id=provider_id,
            is_configured=True,
            auth_mode=cfg.auth_mode,
            credential_ready=False,
            source_kind=None,
            recovery_required=False,
            rebinding_required=rebinding_required,
            reason=reason,
        )

    def safe_binding(self, provider_id: str) -> Optional[CredentialBinding]:
        """The current safe credential binding, or ``None`` when unusable.

        ``None`` means: unconfigured, disabled, quarantined, or (for
        credential-requiring auth modes) no usable source under the
        accepted authority rules.  A no-auth provider yields an explicit
        ``none`` binding — never a fabricated credential.
        """
        readiness = self.readiness(provider_id)
        if not readiness.credential_ready:
            return None
        cfg = _provider_config.get_provider_config(provider_id)
        if cfg is None:  # pragma: no cover - readiness already gated this
            return None
        if readiness.source_kind == CREDENTIAL_SOURCE_NONE:
            return CredentialBinding(
                provider_id=provider_id,
                source_kind=CREDENTIAL_SOURCE_NONE,
                source_ref=None,
                auth_mode=cfg.auth_mode,
                provider_authority=_provider_authority(cfg),
                endpoint_bound=False,
            )
        source_kind = readiness.source_kind or ""
        if source_kind == CREDENTIAL_SOURCE_SESSION_KEY:
            # The provider-core ladder entry spans two concrete
            # authorities; mint the binding for the ONE that actually won
            # (ladder priority: process-local store before the private
            # forwarded hop channel).  A forwarded channel WITHOUT valid
            # issuance provenance (repair 22, F5) is untrusted: the
            # binding fails closed instead of stamping a foreign secret
            # with the current authority.
            from agentic_debugger.application.provider_credentials import (
                _credential_is_usable,
                peek_session_key,
                provider_session_credential_authority_variable,
                provider_session_credential_variable,
            )

            if _credential_is_usable(peek_session_key(provider_id)):
                source_kind = CREDENTIAL_SOURCE_SESSION_MEMORY
            else:
                import os

                issued_authority = os.environ.get(
                    provider_session_credential_authority_variable(provider_id)
                )
                if (
                    isinstance(issued_authority, str)
                    and _AUTHORITY_RE.fullmatch(issued_authority) is not None
                    and issued_authority == _provider_authority(cfg)
                ):
                    source_kind = CREDENTIAL_SOURCE_FORWARDED_SESSION
                else:
                    return None
        return CredentialBinding(
            provider_id=provider_id,
            source_kind=source_kind,
            source_ref=_canonical_source_ref(provider_id, source_kind),
            auth_mode=cfg.auth_mode,
            provider_authority=_provider_authority(cfg),
            endpoint_bound=source_kind in _ENDPOINT_BOUND_SOURCE_KINDS,
        )

    def external_cli_authority(self, provider_id: str) -> Optional[CredentialBinding]:
        """Safe representation of the EXTERNAL CLI credential authority.

        For providers explicitly carrying a historical transport profile
        whose legacy CLI route is available, the credential is owned by
        the external CLI tool itself.  The vault reports readiness through
        the accepted presence-only availability probe and NEVER reads,
        copies, or imports the CLI-owned secret.  Generic providers never
        gain this authority.
        """
        from agentic_debugger.application.model_providers import _legacy_for_config

        cfg = _provider_config.get_provider_config(provider_id)
        if cfg is None or not cfg.enabled or _provider_config.is_provider_quarantined(provider_id):
            return None
        if cfg.auth_mode == _identity.AUTH_NONE:
            return None
        legacy_ok, _reason = _legacy_for_config(cfg)
        if not legacy_ok:
            return None
        source_ref = _external_cli_ref_for_profile(cfg.transport_profile)
        if source_ref is None:  # pragma: no cover - _legacy_for_config gates profiles
            return None
        return CredentialBinding(
            provider_id=provider_id,
            source_kind=CREDENTIAL_SOURCE_EXTERNAL_CLI,
            source_ref=source_ref,
            auth_mode=cfg.auth_mode,
            provider_authority=_provider_authority(cfg),
            endpoint_bound=True,
        )

    def session_authority(
        self, provider_id: str, *, route: Optional[str] = None
    ) -> Optional[CredentialBinding]:
        """The SESSION credential authority, fixed at the session-start boundary.

        Called ONCE by the authoritative launch boundary
        (``build_local_project_launch``) with the resolved ModelBinding
        ROUTE (repair 22, F2): the route decides whether Agentic Debugger
        may hold a raw provider credential for this session at all.

        - legacy CLI route: only the safe ``external_cli`` authority
          (safe provenance; never a materializable API credential).
        - configured-profile / qualified-ladder / offline routes: no
          registry-provider credential authority (``None``).
        - direct API route: the ISSUED private session credential channel
          — the variable the UI materialized into this process's
          environment at spawn together with its safe ISSUANCE AUTHORITY
          metadata (repair 22, F5) — outranks mutable durable state for
          the lifetime of the session.  A channel issued under a
          different provider runtime authority than the CURRENT one is
          STALE and raises instead of being stamped with the new
          authority; a channel without valid issuance provenance fails
          closed.  When no channel was issued, the current ladder binding
          is minted (the launch resolves and retains the actual lease, so
          the pinned SECRET — not just the source metadata — is fixed at
          this boundary).

        Returns ``None`` for unconfigured/disabled/quarantined providers
        and an explicit ``none`` binding for no-auth providers.  The
        result is SAFE metadata only and may be carried on the
        SessionLaunch.
        """
        from agentic_debugger.application.provider_credentials import (
            _credential_is_usable,
            provider_session_credential_authority_variable,
            provider_session_credential_variable,
        )

        cfg = _provider_config.get_provider_config(provider_id)
        if cfg is None or not cfg.enabled or _provider_config.is_provider_quarantined(provider_id):
            return None
        if route in ("configured_profile", "qualified_ladder", "offline"):
            return None
        if route == "legacy_cli":
            return self.external_cli_authority(provider_id)
        if cfg.auth_mode == _identity.AUTH_NONE:
            return self.safe_binding(provider_id)
        channel = provider_session_credential_variable(provider_id)
        authority_var = provider_session_credential_authority_variable(provider_id)
        import os

        issued = os.environ.get(channel)
        if _credential_is_usable(issued):
            issued_authority = os.environ.get(authority_var)
            current_authority = _provider_authority(cfg)
            if (
                not isinstance(issued_authority, str)
                or _AUTHORITY_RE.fullmatch(issued_authority) is None
            ):
                raise CredentialUnavailableError(
                    f"Credential channel for provider {provider_id!r} carries no "
                    "valid safe issuance authority; the session credential cannot "
                    "be pinned"
                )
            if issued_authority != current_authority:
                # F5 TOCTOU gate: the secret was ISSUED under authority A
                # but the provider configuration now says B.  The old
                # secret is never stamped with (or leaked to) the new
                # authority.
                raise StaleCredentialBindingError(
                    f"Credential channel for provider {provider_id!r} was issued "
                    "under a different provider runtime configuration; re-enter "
                    "the credential for the current authority"
                )
            return CredentialBinding(
                provider_id=provider_id,
                source_kind=CREDENTIAL_SOURCE_FORWARDED_SESSION,
                source_ref=_canonical_source_ref(
                    provider_id, CREDENTIAL_SOURCE_FORWARDED_SESSION
                ),
                auth_mode=cfg.auth_mode,
                provider_authority=issued_authority,
                endpoint_bound=False,
            )
        return self.safe_binding(provider_id)

    # -- Resolution (the only secret egress) ----------------------------------

    def authorize_binding_for_transport(
        self, binding: "CredentialBinding"
    ) -> None:
        """Authorize one SAFE binding for transport WITHOUT reading secrets.

        Repair 23 (Finding 2): the single safe binding-authorization seam
        used by BOTH fresh ``resolve_lease`` and retained-ticket transport
        materialization.  Checks SAFE current facts only — never the raw
        credential value:

        - the provider is still configured, enabled, and not quarantined;
        - the CURRENT provider runtime identity equals the binding's
          recorded ``provider_authority``;
        - the CURRENT auth mode is coherent;
        - the CURRENT provider contract still authorizes the named source
          kind where applicable (environment / CLI-auth endpoint binding).

        A secret-value rotation that preserves the provider runtime
        identity still authorizes (session stability); a provider
        configuration authority drift (endpoint / contract / auth mode /
        api_format) fails stale BEFORE any secret egress.  All failures
        are credential-free.
        """
        from agentic_debugger.application.credential_contracts import (
            CredentialBinding as _Binding,
        )

        if not isinstance(binding, _Binding):
            raise CredentialVaultError(
                "transport authorization requires a CredentialBinding"
            )
        provider_id = binding.provider_id
        cfg = _provider_config.get_provider_config(provider_id)
        if cfg is None:
            raise CredentialUnavailableError(
                f"Credential unavailable for provider {provider_id!r}: "
                "the provider is not configured"
            )
        if _provider_config.is_provider_quarantined(provider_id):
            raise CredentialUnavailableError(
                f"Credential unavailable for provider {provider_id!r}: "
                "credential state requires recovery. Edit provider and save "
                "an API key again."
            )
        if not cfg.enabled:
            raise CredentialUnavailableError(
                f"Credential unavailable for provider {provider_id!r}: "
                "the provider is disabled"
            )
        current_authority = _provider_authority(cfg)
        if current_authority != binding.provider_authority:
            raise StaleCredentialBindingError(
                f"Credential binding is stale for provider {provider_id!r}: the "
                "provider runtime configuration changed; re-enter the credential "
                "for the current endpoint"
            )
        if binding.auth_mode != cfg.auth_mode:
            raise StaleCredentialBindingError(
                f"Credential binding is stale for provider {provider_id!r}: the "
                "authentication mode changed; re-enter the credential for the "
                "current authority"
            )
        if binding.source_kind in (
            CREDENTIAL_SOURCE_ENVIRONMENT,
            CREDENTIAL_SOURCE_CLI_AUTH_STORE,
        ):
            from agentic_debugger.application.provider_identity import (
                _contract_for_config,
            )
            from agentic_debugger.application.provider_credentials import (
                provider_endpoint_binding_valid,
            )

            contract = _contract_for_config(cfg)
            if binding.source_kind == CREDENTIAL_SOURCE_ENVIRONMENT:
                contract_authorized = bool(
                    contract is not None
                    and contract.env_var
                    and binding.source_ref == contract.env_var
                )
            else:
                contract_authorized = bool(
                    contract is not None and contract.auth_store_consumable
                )
            if not contract_authorized or not provider_endpoint_binding_valid(
                provider_id
            ):
                raise CredentialUnavailableError(
                    f"Credential unavailable for provider {provider_id!r}: the "
                    "current provider contract does not authorize this credential "
                    "source for the current endpoint"
                )

    def resolve_lease(
        self, provider_or_binding: Any
    ) -> Optional[CredentialLease]:
        """Resolve one ephemeral :class:`CredentialLease`.

        Accepts a provider id (a fresh binding is derived from current
        state) or an explicit :class:`CredentialBinding` (its recorded
        authority is corroborated against current configuration first).

        Returns ``None`` only when the provider's auth mode requires no
        credential.  Raises:

        - :class:`CredentialUnavailableError` — the binding's source can
          no longer be resolved (missing, quarantined, endpoint-invalid,
          or overwritten by a rebinding requirement).  The message is
          always credential-free.
        - :class:`StaleCredentialBindingError` — the binding's recorded
          provider runtime authority no longer matches current provider
          configuration; the credential is never forwarded to a mutated
          authority.

        Resolution is SOURCE-FAITHFUL: the lease materializes exactly the
        source the binding names, once.  If that source disappears or
        changes afterwards, existing leases are unaffected (session
        stability), and a NEW resolution fails closed rather than
        silently switching to a different source.
        """
        if isinstance(provider_or_binding, CredentialBinding):
            binding = provider_or_binding
        else:
            provider_id = str(provider_or_binding)
            binding = self.safe_binding(provider_id)
            if binding is None:
                readiness = self.readiness(provider_id)
                if not readiness.is_configured:
                    raise CredentialUnavailableError(
                        f"Credential unavailable: provider {provider_id!r} is not configured"
                    )
                raise CredentialUnavailableError(
                    f"Credential unavailable for provider {provider_id!r}: "
                    + (readiness.reason or "no usable credential source")
                )

        provider_id = binding.provider_id

        # Repair 23: single safe authorization seam (Finding 2).  The same
        # operational + authority + contract checks gate BOTH fresh leases
        # and retained-ticket egress; secret-value rotation with an
        # unchanged runtime identity still authorizes, while provider
        # authority drift fails stale BEFORE any secret read.
        self.authorize_binding_for_transport(binding)
        cfg = _provider_config.get_provider_config(provider_id)

        if binding.source_kind == CREDENTIAL_SOURCE_NONE:
            return None
        if binding.source_kind == CREDENTIAL_SOURCE_EXTERNAL_CLI:
            raise CredentialUnavailableError(
                f"Credential for provider {provider_id!r} is owned by the external "
                "CLI authority; no Agentic Debugger credential lease exists for "
                "this route"
            )

        # NOTE (repair 23): source authorization already enforced by
        # authorize_binding_for_transport above (single seam); no second
        # contract check here — the value below is resolved source-faithfully.

        value = _resolve_bound_value(provider_id, binding)
        if value is None:
            readiness = self.readiness(provider_id)
            if readiness.recovery_required:
                raise CredentialUnavailableError(
                    f"Credential unavailable for provider {provider_id!r}: "
                    + (readiness.reason or "credential state requires recovery")
                )
            raise CredentialUnavailableError(
                f"Credential unavailable for provider {provider_id!r}: "
                + (readiness.reason or "the bound credential source is unavailable")
            )
        # Repair 24 (F1): bind the SAFE issuance authority into the lease
        # so retain can prove SAME-authority inseparability without ever
        # comparing secret values.
        return CredentialLease(provider_id, binding.source_kind, value, binding)

    def retain_lease(self, lease: "CredentialLease", binding: CredentialBinding) -> str:
        """Retain one launch-resolved lease under an OPAQUE session ticket.

        Used by the authoritative session-start boundary to pin the actual
        SECRET AUTHORITY once (repair 22, F6): the lease is resolved from
        the binding at launch and held ONLY in this process's private
        memory under a random opaque handle.  The ticket is a safe
        process-local capability — never a secret, never serialized, never
        journaled — and is released when redeemed.

        Repair 23 (Finding 1): a retained ticket is NOT a free-floating
        capability — it is inseparable from its CredentialBinding.  Retain
        requires pair coherence (same provider id, same source kind) and a
        materializable direct-provider authority; external/no-auth
        bindings are never retained.  No credential VALUES are compared.

        Repair 24 (F1): the lease carries its SAFE issuance authority
        (the full CredentialBinding it was resolved under).  Retain proves
        fingerprint equality between issuance and supplied binding, so
        ``lease_A + binding_B`` can never create a B-authorized ticket
        when A and B differ in provider runtime authority or any other
        binding identity.  Value-only rotation with unchanged authority
        keeps the same fingerprint and remains valid.
        """
        import uuid

        if not isinstance(lease, CredentialLease):
            raise CredentialVaultError("only a CredentialLease can be retained")
        if not isinstance(binding, CredentialBinding):
            raise CredentialVaultError("retain requires a CredentialBinding")
        if lease.provider_id != binding.provider_id:
            raise StaleCredentialBindingError(
                "the session credential lease does not belong to the session's "
                "credential binding"
            )
        if lease.source_kind != binding.source_kind:
            raise StaleCredentialBindingError(
                "the session credential lease does not belong to the session's "
                "credential binding"
            )
        if binding.source_kind not in _MATERIALABLE_SOURCE_KINDS:
            raise CredentialVaultError(
                "only a materializable direct provider credential authority "
                "may retain a session lease"
            )
        issuance = getattr(lease, "_issuance_binding", None)
        if not isinstance(issuance, CredentialBinding):
            raise StaleCredentialBindingError(
                "the session credential lease carries no proven issuance "
                "authority and can never be retained"
            )
        if issuance.fingerprint() != binding.fingerprint():
            raise StaleCredentialBindingError(
                "the session credential lease was issued under a different "
                "credential authority and cannot be retained under this binding"
            )
        handle = uuid.uuid4().hex
        self._session_leases[handle] = (lease, binding)
        return handle

    def release_ticket(self, ticket: Any) -> bool:
        """Discard one retained ticket WITHOUT materializing its lease.

        Repair 23 (Finding 4): credential-free, idempotent, process-local
        cleanup for a launch that retained a secret-bearing lease but exits
        before transport construction in a still-live process.  Returns
        True when an entry was discarded, False when the ticket was
        unknown or already redeemed.  Never raises for unknown tickets and
        never touches secret material.
        """
        try:
            key = str(ticket)
        except Exception:
            return False
        return self._session_leases.pop(key, None) is not None

    def redeem_lease(
        self,
        ticket: str,
        expected_binding: Optional[CredentialBinding] = None,
    ) -> "CredentialLease":
        """Redeem (and release) the launch-fixed lease for a session ticket.

        The redeemed lease is EXACTLY the one resolved at the session-start
        boundary: durable slot or environment changes since launch cannot
        alter it.  A ticket redeems exactly once, and only for the binding
        it was retained with.
        """
        entry = self._session_leases.pop(str(ticket), None)
        if entry is None:
            raise CredentialUnavailableError(
                "the session credential ticket is not redeemable (unknown or "
                "already redeemed)"
            )
        lease, binding = entry
        if expected_binding is not None and binding.fingerprint() != expected_binding.fingerprint():
            raise StaleCredentialBindingError(
                "the session credential ticket does not match the session's "
                "credential binding"
            )
        return lease

    def transport_materialization(
        self,
        provider_or_binding: Any,
        *,
        route: Optional[str] = None,
        credential_binding: Optional[CredentialBinding] = None,
        credential_ticket: Optional[str] = None,
        expected_provider_authority: Optional[str] = None,
    ) -> Optional[Dict[str, str]]:
        """Trusted model-channel child environment for ONE transport.

        Called when the session transport is established.  The ROUTE is
        examined FIRST (repair 22, F2): the model route decides whether
        Agentic Debugger may materialize a raw provider credential at all.

        - legacy CLI route: ``None`` — the route uses the external CLI
          credential authority; a supplied binding that is not the
          external/none authority raises :class:`CredentialRouteError`
          (a direct credential binding is never leaked or reinterpreted
          into this child).
        - configured-profile / qualified-ladder / offline routes: a
          supplied registry credential binding raises
          :class:`CredentialRouteError`; without one, the accepted route
          boundaries apply.
        - direct API route with an ``external_cli`` binding: raises
          :class:`CredentialRouteError` (an external authority is not a
          direct-API secret lease).
        - a retained session ticket (F6): the launch-fixed lease is
          redeemed EXACTLY once and materialized — durable state changed
          after launch cannot alter it.
        - a supplied binding without a ticket: resolved EXACTLY as that
          binding; missing/stale sources FAIL CLOSED.
        - no-auth provider: ``None`` intentionally (no credential needed).
        - direct route requiring auth with a missing/stale/unavailable
          authority: RAISES — transport construction fails closed before
          any adapter child can start.

        Repair 23:

        - a retained ticket is inseparable from its binding: a ticket
          without its binding fails closed BEFORE any redemption, and only
          a materializable direct-provider binding may redeem;
        - retained-ticket egress re-authorizes the SAFE binding against
          CURRENT provider configuration (disabled / quarantined / missing
          / authority drift blocks release; secret-value rotation with an
          unchanged identity still releases the pinned SECRET_A);
        - ``expected_provider_authority`` (Finding 3) optionally pins the
          executable authority captured at provider-resolution time: the
          CURRENT provider runtime identity must still equal it, otherwise
          the executable/credential pair fails stale before child
          construction (no A/B pairing).  Never a credential value.
        """
        if route in ("configured_profile", "qualified_ladder", "offline"):
            if credential_binding is not None or credential_ticket is not None:
                raise CredentialRouteError(
                    "the session route does not accept a registry-provider "
                    "credential authority"
                )
            return None
        if route == "legacy_cli":
            if credential_binding is not None and credential_binding.source_kind not in (
                CREDENTIAL_SOURCE_EXTERNAL_CLI,
                CREDENTIAL_SOURCE_NONE,
            ):
                raise CredentialRouteError(
                    "the legacy CLI route uses the external CLI credential "
                    "authority; a raw API credential binding is not authorized "
                    "for this route"
                )
            return None

        if credential_binding is not None and credential_binding.source_kind == (
            CREDENTIAL_SOURCE_EXTERNAL_CLI
        ):
            raise CredentialRouteError(
                "an external CLI credential authority does not authorize a "
                "direct-API credential lease"
            )

        # Finding 3 authority pin (configured_source executable/credential
        # coherence): when the caller captured the executable's provider
        # runtime identity at resolution time, the CURRENT identity must
        # still equal it before ANY credential egress.  Safe comparison
        # only — never a credential value, never a URL comparison.
        if expected_provider_authority is not None:
            import re as _re

            if (
                not isinstance(expected_provider_authority, str)
                or _re.fullmatch(r"[0-9a-f]{64}", expected_provider_authority)
                is None
            ):
                raise CredentialVaultError(
                    "expected provider authority is malformed"
                )
            if credential_binding is not None:
                authority_provider = credential_binding.provider_id
            else:
                try:
                    authority_provider = str(provider_or_binding)
                except Exception:
                    raise CredentialVaultError(
                        "expected provider authority requires a provider identity"
                    ) from None
            authority_cfg = _provider_config.get_provider_config(authority_provider)
            current_authority = (
                _provider_authority(authority_cfg)
                if authority_cfg is not None
                else None
            )
            if current_authority != expected_provider_authority:
                raise StaleCredentialBindingError(
                    f"Credential binding is stale for provider {authority_provider!r}: the "
                    "provider runtime configuration changed; re-enter the credential "
                    "for the current endpoint"
                )
            if (
                credential_binding is not None
                and credential_binding.provider_authority
                != expected_provider_authority
            ):
                raise StaleCredentialBindingError(
                    "the session credential ticket does not match the session's "
                    "credential binding"
                )

        if credential_ticket is not None:
            # Finding 1: a ticket is NOT a free-floating capability — it may
            # only redeem the lease retained for THAT binding.
            if credential_binding is None:
                raise StaleCredentialBindingError(
                    "the session credential ticket requires its credential "
                    "binding"
                )
            if credential_binding.source_kind not in _MATERIALABLE_SOURCE_KINDS:
                raise CredentialVaultError(
                    "the session credential ticket is not valid for this "
                    "credential authority"
                )
            # Finding 2: re-authorize the SAFE binding against CURRENT
            # provider configuration BEFORE releasing the pinned lease.
            # Disabled / quarantined / missing / authority-drift blocks
            # egress; the pinned lease itself stays inert in memory.
            self.authorize_binding_for_transport(credential_binding)
            lease = self.redeem_lease(credential_ticket, expected_binding=credential_binding)
            return lease.materialize_environment()

        if credential_binding is not None:
            lease = self.resolve_lease(credential_binding)
            return lease.materialize_environment() if lease is not None else None

        provider_id = str(provider_or_binding)
        if route == "legacy_cli":
            # F2: the external CLI authority owns this route's credential;
            # an existing direct credential is NEVER materialized into the
            # external CLI child merely because the source also exists.
            return None
        readiness = self.readiness(provider_id)
        if readiness.source_kind == CREDENTIAL_SOURCE_NONE:
            return None
        if not readiness.credential_ready:
            raise CredentialUnavailableError(
                f"Credential unavailable for provider {provider_id!r}: "
                + (readiness.reason or "no usable credential source")
            )
        lease = self.resolve_lease(provider_id)
        return lease.materialize_environment() if lease is not None else None

    def session_forwarding_environment(
        self, provider_id: str
    ) -> Optional[Dict[str, str]]:
        """The private UI→worker credential hop (exactly one variable).

        Wraps the accepted provider-core forwarding authority verbatim:
        the winning credential value is forwarded once under the
        provider's private session credential variable; ambient
        environment and CLI-auth sources are forwarded only while the
        canonical endpoint binding remains valid; quarantined and no-auth
        providers forward nothing.
        """
        from agentic_debugger.application.provider_credentials import (
            provider_session_credential_environment as _hop,
        )

        env = _hop(provider_id)
        return dict(env) if env else None

    # -- Write path ------------------------------------------------------------

    def commit_provider_save(
        self,
        provider_id: str,
        updated_configs: "List[ProviderConfig]",
        api_key: Optional[str],
    ) -> None:
        """Atomic provider+credential save transaction (vault-owned).

        Wraps the accepted backend transaction verbatim: the durable
        quarantine marker is armed BEFORE any credential mutation, the
        secure-store write and the configuration persistence commit
        atomically with verified rollback, and credential values never
        leave the transaction boundary (including error text).
        """
        from agentic_debugger.application.provider_management import (
            commit_provider_and_credential,
        )

        commit_provider_and_credential(provider_id, updated_configs, api_key)

    def revoke_credential(self, provider_id: str) -> None:
        """Delete/revoke the locally stored credential for one provider.

        Removes the durable vault material (verifying absence) and the
        process-local session credential.  This is local credential
        authority deletion only: it never touches the provider
        configuration, cached catalog, quarantine bookkeeping owned by
        the provider-deletion transaction, or any historical session evidence.
        """
        from agentic_debugger.application.provider_credentials import (
            clear_session_key,
            delete_secure_credential,
            load_secure_credential,
        )

        previous = load_secure_credential(provider_id)
        if previous is not None:
            deleted = delete_secure_credential(provider_id)
            remaining = load_secure_credential(provider_id)
            if not deleted or remaining is not None:
                raise _identity.ProviderConnectionError(
                    "provider credential cleanup could not be completed"
                )
        clear_session_key(provider_id)
