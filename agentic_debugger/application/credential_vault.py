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

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agentic_debugger.application.events import contains_credential_shape
from agentic_debugger.application import provider_connections as _pc

#: Accepted direct-credential source kinds re-used verbatim from the
#: provider core (safe constants, never patched).
CREDENTIAL_SOURCE_SAVED = _pc.CREDENTIAL_SOURCE_SAVED
CREDENTIAL_SOURCE_SESSION_KEY = _pc.CREDENTIAL_SOURCE_SESSION_KEY
CREDENTIAL_SOURCE_ENVIRONMENT = _pc.CREDENTIAL_SOURCE_ENVIRONMENT
CREDENTIAL_SOURCE_CLI_AUTH_STORE = _pc.CREDENTIAL_SOURCE_CLI_AUTH_STORE

__all__ = [
    "CREDENTIAL_SOURCE_EXTERNAL_CLI",
    "CREDENTIAL_SOURCE_NONE",
    "CredentialBinding",
    "CredentialLease",
    "CredentialReadiness",
    "CredentialUnavailableError",
    "CredentialVault",
    "CredentialVaultError",
    "StaleCredentialBindingError",
]

#: Safe binding source kinds owned by this module (beyond the accepted
#: direct-credential sources re-used from the provider core).
CREDENTIAL_SOURCE_EXTERNAL_CLI = "external_cli"
CREDENTIAL_SOURCE_NONE = "none"

_BINDING_SOURCE_KINDS = (
    CREDENTIAL_SOURCE_SAVED,
    CREDENTIAL_SOURCE_SESSION_KEY,
    CREDENTIAL_SOURCE_ENVIRONMENT,
    CREDENTIAL_SOURCE_CLI_AUTH_STORE,
    CREDENTIAL_SOURCE_EXTERNAL_CLI,
    CREDENTIAL_SOURCE_NONE,
)

#: Safe, stable identities for the external CLI credential authorities.
#: These name the OWNING TOOL, never a secret, a path, or its contents.
_EXTERNAL_CLI_SOURCE_REFS = {
    _pc.TRANSPORT_OPENCODE_GO: "opencode CLI auth (external)",
    _pc.TRANSPORT_COMMANDCODE_GOAT: "commandcode CLI auth (external)",
}


class CredentialVaultError(RuntimeError):
    """Base error for CredentialVault operations (credential-free text)."""


class CredentialUnavailableError(CredentialVaultError):
    """No usable credential could be resolved for a required binding."""


class StaleCredentialBindingError(CredentialVaultError):
    """A binding no longer certifies the current provider runtime authority."""


def _scrub(field: str, value: Any) -> None:
    """Fail closed when a credential-shaped value reaches a safe field."""
    if isinstance(value, str) and contains_credential_shape(value):
        raise CredentialVaultError(
            f"CredentialBinding field {field!r} contains a credential-shaped value"
        )


@dataclass(frozen=True)
class CredentialBinding:
    """Safe, non-secret credential identity/provenance.

    A binding states WHICH credential authority applies to a provider —
    never any secret material.  Safe fields:

    - ``provider_id``: logical provider identifier.
    - ``source_kind``: one of ``saved`` (OS secure store), ``session_key``
      (memory-only session store or the private forwarded session
      variable), ``environment`` (endpoint-bound ambient variable),
      ``cli_auth_store`` (consumable CLI auth store), ``external_cli``
      (credential owned by the external CLI tool itself), or ``none``
      (auth mode requires no credential).
    - ``source_ref``: safe reference for sources that have one — the
      environment variable NAME, the durable vault slot NAME, or the
      private session credential variable NAME.  Names only; never values.
      ``None`` for sources without a stable slot identity.
    - ``auth_mode``: the provider authentication mode the binding applies to.
    - ``provider_authority``: the provider runtime authority identity the
      binding was created under (the safe provider runtime identity hex
      digest).  A binding never certifies a different authority.
    - ``endpoint_bound``: whether the underlying source is
      canonical-endpoint-bound (ambient environment / CLI-auth sources for
      historical contracts are usable only while the configured endpoint
      still matches the canonical contract endpoint).

    Bindings are deterministic and serializable; the fingerprint is
    computed over safe metadata only.  A binding never carries enough
    information to reconstruct a secret.
    """

    provider_id: str
    source_kind: str
    source_ref: Optional[str]
    auth_mode: str
    provider_authority: Optional[str]
    endpoint_bound: bool

    def __post_init__(self) -> None:
        if not self.provider_id or not self.provider_id.strip():
            raise CredentialVaultError("CredentialBinding requires a provider identity")
        if self.source_kind not in _BINDING_SOURCE_KINDS:
            raise CredentialVaultError(
                f"unknown credential source kind: {self.source_kind!r}"
            )
        if self.source_kind == CREDENTIAL_SOURCE_NONE and self.source_ref is not None:
            raise CredentialVaultError(
                "a no-auth binding carries no credential source reference"
            )
        for field_name in ("provider_id", "source_kind", "source_ref", "auth_mode", "provider_authority"):
            _scrub(field_name, getattr(self, field_name))

    def to_mapping(self) -> Dict[str, Any]:
        """Safe serializable mapping (names and kinds only; never values)."""
        return {
            "provider_id": self.provider_id,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "auth_mode": self.auth_mode,
            "provider_authority": self.provider_authority,
            "endpoint_bound": self.endpoint_bound,
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "CredentialBinding":
        """Reconstitute a binding from its safe mapping."""
        return cls(
            provider_id=str(mapping.get("provider_id") or ""),
            source_kind=str(mapping.get("source_kind") or ""),
            source_ref=mapping.get("source_ref"),
            auth_mode=str(mapping.get("auth_mode") or ""),
            provider_authority=mapping.get("provider_authority"),
            endpoint_bound=bool(mapping.get("endpoint_bound", False)),
        )

    def fingerprint(self) -> str:
        """Deterministic fingerprint over safe binding metadata only."""
        canonical = json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return (
            f"CredentialBinding(provider={self.provider_id!r}, "
            f"source_kind={self.source_kind!r}, auth_mode={self.auth_mode!r})"
        )


class CredentialLease:
    """Ephemeral secret-bearing credential materialization.

    Resolved ONCE by :meth:`CredentialVault.resolve_lease` for trusted
    provider execution.  Lifetime and egress contract:

    - exists only inside trusted process memory; never durable;
    - non-serializable: pickle (and any ``__reduce__``-based copying)
      fails closed; ``copy``/``deepcopy`` fail closed;
    - no secret-bearing repr; no value-bearing mapping;
    - no equality/hash semantics beyond object identity, so values are
      never compared or rendered;
    - exactly two capability methods, and nothing else:

      - :meth:`materialize_environment` — derive the authorized child
        environment for the model-adapter request child (the single
        private credential channel variable, value only);
      - :meth:`reveal` — the narrow, documented access point for the
        trusted IN-PROCESS provider HTTP paths (catalog refresh and
        connection checks) which require the credential string itself.
        No other consumer is authorized.

    The lease is never logged, journaled, emitted as an event, exposed to
    a model, or placed in project/PDB/verifier child environments.
    """

    __slots__ = ("_provider_id", "_source_kind", "_value")

    def __init__(self, provider_id: str, source_kind: str, value: str) -> None:
        # Constructed only by CredentialVault.resolve_lease; the value is
        # held privately and never rendered by this object.
        object.__setattr__(self, "_provider_id", provider_id)
        object.__setattr__(self, "_source_kind", source_kind)
        object.__setattr__(self, "_value", value)

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def source_kind(self) -> str:
        return self._source_kind

    def materialize_environment(self) -> Dict[str, str]:
        """The authorized model-adapter child credential environment.

        Exactly one variable: the provider's private credential channel
        (the same variable the trusted transport merges into the adapter
        request child).  Never argv, never journals, never other roles.
        """
        from agentic_debugger.application.provider_connections import (
            provider_session_credential_variable,
        )

        return {provider_session_credential_variable(self._provider_id): self._value}

    def reveal(self) -> str:
        """Trusted in-process provider HTTP materialization boundary.

        FOR THE PROVIDER HTTP PATH ONLY (catalog refresh, connection
        checks, and the direct adapter's issued-channel consumption via
        the derived environment).  This is the one narrow string access
        point; product/UI/session code must never call it.
        """
        return self._value

    def __repr__(self) -> str:
        return (
            f"CredentialLease(provider={self._provider_id!r}, "
            f"source_kind={self._source_kind!r}, materialized=True)"
        )

    def __getstate__(self) -> None:
        raise TypeError("credential lease must never be serialized")

    def __reduce__(self) -> None:
        raise TypeError("credential lease must never be serialized")

    def __copy__(self) -> "CredentialLease":
        raise TypeError("credential lease must never be copied")

    def __deepcopy__(self, memo: Any) -> "CredentialLease":
        raise TypeError("credential lease must never be copied")


@dataclass(frozen=True)
class CredentialReadiness:
    """Safe credential readiness facts for one provider (never values).

    - ``is_configured``: a valid enabled durable configuration exists.
    - ``credential_ready``: the credential authority required by the auth
      mode is currently available (no network claim).
    - ``source_kind``: the winning safe source kind, ``"none"`` when the
      auth mode requires no credential, or ``None`` when not ready.
    - ``recovery_required``: the provider's credential/config association
      is indeterminate (quarantined) and requires explicit re-entry.
    - ``rebinding_required``: a previously usable credential no longer
      certifies the current endpoint/authority and must be re-entered.
    - ``reason``: safe, actionable, credential-free explanation.
    """

    provider_id: str
    is_configured: bool
    auth_mode: Optional[str]
    credential_ready: bool
    source_kind: Optional[str]
    recovery_required: bool
    rebinding_required: bool
    reason: Optional[str] = None

    @property
    def has_credential_source(self) -> bool:
        """Whether a REAL credential source exists (auth none excluded)."""
        return self.source_kind is not None and self.source_kind != CREDENTIAL_SOURCE_NONE

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "is_configured": self.is_configured,
            "auth_mode": self.auth_mode,
            "credential_ready": self.credential_ready,
            "source_kind": self.source_kind,
            "recovery_required": self.recovery_required,
            "rebinding_required": self.rebinding_required,
            "reason": self.reason,
        }

    def __repr__(self) -> str:
        return (
            f"CredentialReadiness(provider={self.provider_id!r}, "
            f"credential_ready={self.credential_ready!r}, "
            f"source_kind={self.source_kind!r})"
        )


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
        from agentic_debugger.application.provider_connections import (
            AUTH_NONE,
            provider_endpoint_binding_valid,
            provider_environment_variable,
        )

        cfg = _pc.get_provider_config(provider_id)
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
            )
        if not cfg.enabled:
            return CredentialReadiness(
                provider_id=provider_id,
                is_configured=False,
                auth_mode=cfg.auth_mode,
                credential_ready=False,
                source_kind=None,
                recovery_required=False,
                rebinding_required=False,
                reason="Provider is disabled (edit provider to re-enable it)",
            )
        if _pc.is_provider_quarantined(provider_id):
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

        source = _pc.credential_source_for(provider_id)
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
        from agentic_debugger.application.provider_connections import (
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
        cfg = _pc.get_provider_config(provider_id)
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
        return CredentialBinding(
            provider_id=provider_id,
            source_kind=readiness.source_kind or "",
            source_ref=_source_ref_for(provider_id, cfg, readiness.source_kind or ""),
            auth_mode=cfg.auth_mode,
            provider_authority=_provider_authority(cfg),
            endpoint_bound=readiness.source_kind
            in (CREDENTIAL_SOURCE_ENVIRONMENT, CREDENTIAL_SOURCE_CLI_AUTH_STORE),
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

        cfg = _pc.get_provider_config(provider_id)
        if cfg is None or not cfg.enabled or _pc.is_provider_quarantined(provider_id):
            return None
        if cfg.auth_mode == _pc.AUTH_NONE:
            return None
        legacy_ok, _reason = _legacy_for_config(cfg)
        if not legacy_ok:
            return None
        source_ref = _EXTERNAL_CLI_SOURCE_REFS.get(cfg.transport_profile)
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

    # -- Resolution (the only secret egress) ----------------------------------

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
        if binding.source_kind == CREDENTIAL_SOURCE_NONE:
            return None
        if binding.source_kind == CREDENTIAL_SOURCE_EXTERNAL_CLI:
            raise CredentialUnavailableError(
                f"Credential for provider {provider_id!r} is owned by the external "
                "CLI authority; no Agentic Debugger credential lease exists for "
                "this route"
            )

        # Provider runtime authority corroboration (V2-03 identity rules):
        # a credential bound to authority A is never materialized for
        # authority B — even when provider id, auth mode, and slot are
        # unchanged.
        cfg = _pc.get_provider_config(provider_id)
        current_authority = _provider_authority(cfg) if cfg is not None else None
        if (
            binding.provider_authority is not None
            and current_authority != binding.provider_authority
        ):
            raise StaleCredentialBindingError(
                f"Credential binding is stale for provider {provider_id!r}: the "
                "provider runtime configuration changed; re-enter the credential "
                "for the current endpoint"
            )

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
        return CredentialLease(provider_id, binding.source_kind, value)

    def transport_materialization(self, provider_id: str) -> Optional[Dict[str, str]]:
        """Trusted model-channel child environment from a fresh lease.

        Used by the session runtime when establishing the model transport:
        the lease is resolved once here and the returned mapping is then
        held fixed by the transport for the session.  Returns ``None``
        when no credential is required or when the route's credential is
        owned by the external CLI authority (legacy CLI routes read the
        operator auth store in place).  Stale bindings fail closed.
        """
        try:
            lease = self.resolve_lease(provider_id)
        except CredentialUnavailableError:
            return None
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
        from agentic_debugger.application.provider_connections import (
            provider_session_credential_environment as _hop,
        )

        env = _hop(provider_id)
        return dict(env) if env else None

    # -- Write path ------------------------------------------------------------

    def commit_provider_save(
        self,
        provider_id: str,
        updated_configs: List[ProviderConfig],
        api_key: Optional[str],
    ) -> None:
        """Atomic provider+credential save transaction (vault-owned).

        Wraps the accepted backend transaction verbatim: the durable
        quarantine marker is armed BEFORE any credential mutation, the
        secure-store write and the configuration persistence commit
        atomically with verified rollback, and credential values never
        leave the transaction boundary (including error text).
        """
        from agentic_debugger.application.provider_connections import (
            commit_provider_and_credential,
        )

        commit_provider_and_credential(provider_id, updated_configs, api_key)

    def revoke_credential(self, provider_id: str) -> None:
        """Delete/revoke the locally stored credential for one provider.

        Removes the durable vault material (verifying absence) and the
        process-local session credential.  This is local credential
        authority deletion only: it never touches the provider
        configuration, cached catalog, quarantine bookkeeping owned by the
        provider-deletion transaction, or any historical session evidence.
        """
        from agentic_debugger.application.provider_connections import (
            clear_session_key,
            delete_secure_credential,
            load_secure_credential,
        )

        previous = load_secure_credential(provider_id)
        if previous is not None:
            deleted = delete_secure_credential(provider_id)
            remaining = load_secure_credential(provider_id)
            if not deleted or remaining is not None:
                raise _pc.ProviderConnectionError(
                    "provider credential cleanup could not be completed"
                )
        clear_session_key(provider_id)


# -- module-level authorities (single derivation points) -----------------------


def _provider_authority(cfg: Any) -> Optional[str]:
    """Safe provider runtime authority identity for a configuration.

    The same safe deterministic identity V2-03 established
    (``model_gateway.provider_runtime_identity``); resolved lazily so the
    vault never imports the gateway at module load time.
    """
    if cfg is None:
        return None
    from agentic_debugger.application.model_gateway import provider_runtime_identity

    return provider_runtime_identity(cfg)


def _source_ref_for(provider_id: str, cfg: Any, source_kind: str) -> Optional[str]:
    """Safe source reference (names only) for one winning source kind."""
    from agentic_debugger.application.provider_connections import (
        credential_slot_name,
        provider_environment_variable,
        provider_session_credential_variable,
        _contract_for_config,
    )

    if source_kind == CREDENTIAL_SOURCE_SAVED:
        return credential_slot_name(provider_id)
    if source_kind == CREDENTIAL_SOURCE_SESSION_KEY:
        return provider_session_credential_variable(provider_id)
    if source_kind == CREDENTIAL_SOURCE_ENVIRONMENT:
        return provider_environment_variable(provider_id)
    if source_kind == CREDENTIAL_SOURCE_CLI_AUTH_STORE:
        # The consumable CLI auth store has no product-owned slot name;
        # the source kind itself is the safe identity.  The store PATH is
        # operator-machine-local and is never recorded here.
        return None
    if source_kind == CREDENTIAL_SOURCE_EXTERNAL_CLI:
        return _EXTERNAL_CLI_SOURCE_REFS.get(cfg.transport_profile)
    return None


def _resolve_bound_value(provider_id: str, binding: CredentialBinding) -> Optional[str]:
    """Resolve the raw value of EXACTLY the binding's named source.

    Source-faithful materialization beneath the vault: each accessor is
    the accepted backend primitive, applied to the one source the binding
    names.  No fallback ladder here — a missing bound source resolves to
    ``None`` and the caller fails closed instead of silently switching
    sources.
    """
    from agentic_debugger.application.provider_connections import (
        _read_opencode_auth_store_key,
        load_secure_credential,
        opencode_auth_store_path,
        peek_session_key,
        provider_session_credential_variable,
        _credential_is_usable,
    )

    kind = binding.source_kind
    if kind == CREDENTIAL_SOURCE_SAVED:
        value = load_secure_credential(provider_id)
    elif kind == CREDENTIAL_SOURCE_SESSION_KEY:
        # The accepted session_key source spans BOTH session-scoped
        # sub-cases, read once here in the ladder's priority order: the
        # process-local store and the private forwarded hop variable
        # (what a trusted worker process receives from the UI hop).
        value = peek_session_key(provider_id)
        if not _credential_is_usable(value):
            import os

            raw = os.environ.get(provider_session_credential_variable(provider_id))
            value = raw if _credential_is_usable(raw) else None
    elif kind == CREDENTIAL_SOURCE_ENVIRONMENT:
        from agentic_debugger.application.provider_connections import (
            provider_environment_variable,
            _contract_for_config,
        )

        cfg = _pc.get_provider_config(provider_id)
        contract = _contract_for_config(cfg) if cfg is not None else None
        var = (
            contract.env_var
            if contract is not None and contract.env_var
            else provider_environment_variable(provider_id)
        )
        value = None
        if var:
            import os

            raw = os.environ.get(var)
            if _credential_is_usable(raw):
                value = raw
    elif kind == CREDENTIAL_SOURCE_CLI_AUTH_STORE:
        try:
            value = _read_opencode_auth_store_key(opencode_auth_store_path())
        except Exception:
            value = None
    else:  # pragma: no cover - none/external handled before resolution
        value = None
    if value is None:
        return None
    stripped = value.strip()
    return stripped if _credential_is_usable(stripped) else None
