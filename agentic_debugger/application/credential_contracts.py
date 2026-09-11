"""Credential contract layer beneath the CredentialVault authority.

This module owns the safe, non-secret credential vocabulary and identity
helpers used by :mod:`agentic_debugger.application.credential_vault` (the
single stateful secret authority):

- the credential source-kind constants and binding-source tables;
- the fail-closed error vocabulary;
- :class:`CredentialBinding` — safe, non-secret, serializable credential
  identity/provenance;
- :class:`CredentialLease` — the ephemeral, secret-bearing,
  non-serializable runtime materialization;
- :class:`CredentialReadiness` — safe readiness facts (never values);
- the canonical safe source-identity helpers (slot names, endpoint-bound
  variables, the auth-store LOCATION fingerprint) and the source-faithful
  value resolution beneath the vault.

Secret-safety invariants are identical to the vault module: no credential
VALUE ever appears in a binding, readiness fact, error text, or repr; the
lease cannot be serialized or copied; value resolution is source-faithful
with no fallback ladder.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from agentic_debugger.application import provider_config as _provider_config
from agentic_debugger.application import provider_credentials as _credentials
from agentic_debugger.application import provider_identity as _identity
from agentic_debugger.application.events import contains_credential_shape

#: Accepted direct-credential source kinds re-used verbatim from the
#: provider core (safe constants, never patched).
CREDENTIAL_SOURCE_SAVED = _credentials.CREDENTIAL_SOURCE_SAVED
CREDENTIAL_SOURCE_SESSION_KEY = _credentials.CREDENTIAL_SOURCE_SESSION_KEY
CREDENTIAL_SOURCE_ENVIRONMENT = _credentials.CREDENTIAL_SOURCE_ENVIRONMENT
CREDENTIAL_SOURCE_CLI_AUTH_STORE = _credentials.CREDENTIAL_SOURCE_CLI_AUTH_STORE

#: Safe binding source kinds (beyond the accepted direct-credential
#: sources re-used from the provider core).
#:
#: The provider core's ``session_key`` ladder entry spans TWO concrete
#: authorities; a CredentialBinding names exactly ONE of them so explicit
#: binding resolution can never silently switch sources:
#: - ``session_memory``: the process-local session key store;
#: - ``forwarded_session``: the private issued UI-to-worker session
#:   credential channel variable (the fixed session authority: once the
#:   UI issued the session credential hop, mutable durable state never
#:   outranks it for the lifetime of that session).
CREDENTIAL_SOURCE_SESSION_MEMORY = "session_memory"
CREDENTIAL_SOURCE_FORWARDED_SESSION = "forwarded_session"
CREDENTIAL_SOURCE_EXTERNAL_CLI = "external_cli"
CREDENTIAL_SOURCE_NONE = "none"

_BINDING_SOURCE_KINDS = (
    CREDENTIAL_SOURCE_SAVED,
    CREDENTIAL_SOURCE_SESSION_MEMORY,
    CREDENTIAL_SOURCE_FORWARDED_SESSION,
    CREDENTIAL_SOURCE_ENVIRONMENT,
    CREDENTIAL_SOURCE_CLI_AUTH_STORE,
    CREDENTIAL_SOURCE_EXTERNAL_CLI,
    CREDENTIAL_SOURCE_NONE,
)

#: Sources whose authority is structurally bound to the canonical provider
#: endpoint.  ``endpoint_bound`` is DERIVED from the source kind, never
#: caller-chosen metadata.
_ENDPOINT_BOUND_SOURCE_KINDS = frozenset(
    {
        CREDENTIAL_SOURCE_ENVIRONMENT,
        CREDENTIAL_SOURCE_CLI_AUTH_STORE,
        CREDENTIAL_SOURCE_EXTERNAL_CLI,
    }
)

#: Safe, stable identities for the external CLI credential authorities.
#: These name the OWNING TOOL, never a secret, a path, or its contents.
_EXTERNAL_CLI_SOURCE_REFS = {
    _identity.TRANSPORT_OPENCODE_GO: "opencode CLI auth (external)",
    _identity.TRANSPORT_COMMANDCODE_GOAT: "commandcode CLI auth (external)",
}

_AUTHORITY_RE = re.compile(r"[0-9a-f]{64}")

#: Source kinds for which an Agentic-Debugger-held secret lease may exist.
_MATERIALABLE_SOURCE_KINDS = frozenset(
    {
        CREDENTIAL_SOURCE_SAVED,
        CREDENTIAL_SOURCE_SESSION_MEMORY,
        CREDENTIAL_SOURCE_FORWARDED_SESSION,
        CREDENTIAL_SOURCE_ENVIRONMENT,
        CREDENTIAL_SOURCE_CLI_AUTH_STORE,
    }
)


def _cli_auth_store_location_fingerprint() -> str:
    """Safe normalized LOCATION fingerprint of the consumable auth store.

    Derived from the resolved store PATH only — never from any secret
    content — so a binding can detect when the machine-local authority
    moved to a different location.  The hex digest form keeps the
    machine-local path itself out of provenance.
    """
    import os

    from agentic_debugger.application.provider_credentials import (
        opencode_auth_store_path,
    )

    path = os.path.abspath(str(opencode_auth_store_path()))
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


class CredentialVaultError(RuntimeError):
    """Base error for CredentialVault operations (credential-free text)."""


class CredentialUnavailableError(CredentialVaultError):
    """No usable credential could be resolved for a required binding."""


class StaleCredentialBindingError(CredentialVaultError):
    """A binding no longer certifies the current provider runtime authority."""


class CredentialRouteError(CredentialVaultError):
    """A credential authority is incompatible with the model transport route.

    The ROUTE decides whether Agentic Debugger may materialize a raw
    provider credential at all: legacy CLI routes use the external CLI
    credential authority (never an Agentic-Debugger-held API key), and
    profile/ladder/offline routes carry no registry-provider credential.
    A supplied binding that contradicts the route fails closed BEFORE any
    child construction — it is never silently leaked or reinterpreted.
    """


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
    - ``source_kind``: ONE concrete credential authority — ``saved`` (OS
      secure store), ``session_memory`` (process-local session store),
      ``forwarded_session`` (the private issued session credential
      channel), ``environment`` (endpoint-bound ambient variable),
      ``cli_auth_store`` (consumable CLI auth store), ``external_cli``
      (credential owned by the external CLI tool itself), or ``none``
      (auth mode requires no credential).
    - ``source_ref``: the CANONICAL safe authority identity for the kind —
      the durable vault slot NAME, the private session credential
      variable NAME, the accepted provider environment-variable NAME, the
      safe normalized auth-store LOCATION fingerprint, or the bounded
      external authority identity.  Names/fingerprints only; never
      values.  ``None`` only where no separate identity exists
      (``session_memory``, ``none``).
    - ``auth_mode``: the provider authentication mode the binding applies to.
    - ``provider_authority``: the provider runtime authority identity the
      binding was created under (the safe V2-03 provider runtime identity
      hex digest).  MANDATORY: a binding always certifies exactly one
      authority; ``resolve_lease`` corroborates it unconditionally.
    - ``endpoint_bound``: DERIVED from the source kind — ambient
      environment / CLI-auth / external-CLI authorities are structurally
      canonical-endpoint-bound; contradictory input fails construction.

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
        """Structural (semantic-shape) validation — the safety authority.

        Safe provenance is proven by SEMANTIC SHAPE, not by credential-shape
        heuristics: every field must be exactly the canonical safe identity
        its source kind requires (from_mapping enforces the identical
        rules through this constructor).  Credential-shape detection remains
        only as a secondary fail-safe.

        ERROR SAFETY (repair 22): rejection messages are constant text
        naming the FIELD and the expectation — untrusted rejected field
        values (source_ref, source_kind, auth_mode, provider_authority, an
        unvalidated provider_id) are NEVER echoed, because a
        malformed/from_mapping caller may supply arbitrary raw strings.
        """
        if not self.provider_id or not self.provider_id.strip():
            raise CredentialVaultError(
                "CredentialBinding requires a non-empty provider identity"
            )
        if self.source_kind not in _BINDING_SOURCE_KINDS:
            raise CredentialVaultError(
                "CredentialBinding source_kind is not an accepted credential "
                "source kind"
            )
        # Auth-mode coherence is structural: a no-auth binding carries no
        # credential source, and a credential-requiring auth mode can never
        # carry a no-auth source.
        if self.auth_mode not in _identity.AUTH_MODES:
            raise CredentialVaultError(
                "CredentialBinding auth_mode is not an accepted authentication mode"
            )
        if self.auth_mode == _identity.AUTH_NONE and self.source_kind != CREDENTIAL_SOURCE_NONE:
            raise CredentialVaultError(
                "CredentialBinding with no-auth mode must use the 'none' source kind"
            )
        if self.source_kind == CREDENTIAL_SOURCE_NONE and self.auth_mode != _identity.AUTH_NONE:
            raise CredentialVaultError(
                "CredentialBinding 'none' source kind requires the no-auth mode"
            )
        # provider_runtime_authority is MANDATORY: every binding certifies
        # one exact V2-03 provider runtime identity, and resolve_lease
        # corroborates it unconditionally.
        if (
            not isinstance(self.provider_authority, str)
            or _AUTHORITY_RE.fullmatch(self.provider_authority) is None
        ):
            raise CredentialVaultError(
                "CredentialBinding provider_authority is missing or malformed "
                "(64-hex provider runtime identity required)"
            )
        # endpoint_bound is structural, not metadata: contradictory input
        # fails rather than silently disagreeing with the source semantics.
        structurally_bound = self.source_kind in _ENDPOINT_BOUND_SOURCE_KINDS
        if bool(self.endpoint_bound) is not structurally_bound:
            raise CredentialVaultError(
                "CredentialBinding endpoint_bound contradicts its source kind"
            )
        # source_ref must be exactly the canonical safe identity for the kind.
        expected_ref = _canonical_source_ref(self.provider_id, self.source_kind)
        if self.source_ref != expected_ref:
            raise CredentialVaultError(
                "CredentialBinding source_ref is not the canonical safe "
                "credential authority identity for this provider and source kind"
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

    __slots__ = ("_provider_id", "_source_kind", "_value", "_issuance_binding")

    def __init__(
        self,
        provider_id: str,
        source_kind: str,
        value: str,
        issuance_binding: Optional["CredentialBinding"] = None,
    ) -> None:
        # Constructed only by CredentialVault.resolve_lease; the value is
        # held privately and never rendered by this object.  Repair 24
        # (F1): the lease cryptographically/structurally binds the SAFE
        # issuance authority it was resolved under (the full
        # CredentialBinding, safe metadata only — never secret material).
        # retain_lease() proves fingerprint equality so a lease issued
        # under authority A can never be retained under authority B.
        # issuance_binding is required for all vault-issued leases; None
        # is accepted only to keep direct construction fail-closed at
        # retain time (a lease without proven issuance can never be
        # retained).
        object.__setattr__(self, "_provider_id", provider_id)
        object.__setattr__(self, "_source_kind", source_kind)
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_issuance_binding", issuance_binding)

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
        from agentic_debugger.application.provider_credentials import (
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

    - ``is_configured``: a durable provider configuration exists (V2-03
      factual vocabulary: configured and enabled are SEPARATE facts — a
      disabled provider remains durably configured).
    - ``is_enabled``: the durable configuration is enabled.
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
    is_enabled: bool = True

    @property
    def has_credential_source(self) -> bool:
        """Whether a REAL credential source exists (auth none excluded)."""
        return self.source_kind is not None and self.source_kind != CREDENTIAL_SOURCE_NONE

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "is_configured": self.is_configured,
            "is_enabled": self.is_enabled,
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


# -- safe identity helpers (single derivation points) -----------------------


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


def _canonical_source_ref(provider_id: str, source_kind: str) -> Optional[str]:
    """The ONE canonical safe source authority identity for a kind.

    Every binding must carry exactly this value for its kind — arbitrary
    strings are never accepted merely because they do not resemble an API
    key.  Names and path-derived fingerprints only; never secret content.
    """
    from agentic_debugger.application.provider_credentials import (
        credential_slot_name,
        provider_environment_variable,
        provider_session_credential_variable,
    )

    if source_kind == CREDENTIAL_SOURCE_SAVED:
        return credential_slot_name(provider_id)
    if source_kind == CREDENTIAL_SOURCE_SESSION_MEMORY:
        # The process-local store is keyed by the provider identity
        # itself; no separate reference exists.
        return None
    if source_kind == CREDENTIAL_SOURCE_FORWARDED_SESSION:
        return provider_session_credential_variable(provider_id)
    if source_kind == CREDENTIAL_SOURCE_ENVIRONMENT:
        return provider_environment_variable(provider_id)
    if source_kind == CREDENTIAL_SOURCE_CLI_AUTH_STORE:
        # Safe normalized LOCATION fingerprint of the consumable store
        # (path-derived, never content-derived): sufficient to detect
        # machine-local source drift without persisting or exposing the
        # path or any CLI-owned secret.
        return _cli_auth_store_location_fingerprint()
    if source_kind == CREDENTIAL_SOURCE_EXTERNAL_CLI:
        cfg = _provider_config.get_provider_config(provider_id)
        profile = getattr(cfg, "transport_profile", None) if cfg is not None else None
        return _EXTERNAL_CLI_SOURCE_REFS.get(profile)
    return None  # none


def _external_cli_ref_for_profile(transport_profile: str) -> Optional[str]:
    return _EXTERNAL_CLI_SOURCE_REFS.get(transport_profile)


def _resolve_bound_value(provider_id: str, binding: CredentialBinding) -> Optional[str]:
    """Resolve the raw value of EXACTLY the binding's named source.

    Source-faithful materialization beneath the vault: each accessor is
    the accepted backend primitive, applied to the one source the binding
    names.  No fallback ladder here — a missing bound source resolves to
    ``None`` and the caller fails closed instead of silently switching
    sources.
    """
    from agentic_debugger.application.provider_credentials import (
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
    elif kind == CREDENTIAL_SOURCE_SESSION_MEMORY:
        # SOURCE-FAITHFUL: this binding names the process-local session
        # store and NOTHING else.  If the entry disappears, resolution
        # fails — it never falls back to the forwarded channel or any
        # other authority.
        value = peek_session_key(provider_id)
        if not _credential_is_usable(value):
            value = None
    elif kind == CREDENTIAL_SOURCE_FORWARDED_SESSION:
        # SOURCE-FAITHFUL: this binding names the private ISSUED session
        # channel (fixed for the worker process at spawn) and NOTHING
        # else — mutable durable state can never outrank it.
        import os

        raw = os.environ.get(provider_session_credential_variable(provider_id))
        value = raw if _credential_is_usable(raw) else None
    elif kind == CREDENTIAL_SOURCE_ENVIRONMENT:
        from agentic_debugger.application.provider_identity import (
            _contract_for_config,
        )
        from agentic_debugger.application.provider_credentials import (
            provider_environment_variable,
        )

        cfg = _provider_config.get_provider_config(provider_id)
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
        # SOURCE-FAITHFUL location pinning: the binding carries the safe
        # normalized LOCATION fingerprint of the consumable store it was
        # minted from (path-derived only, never secret content).  A moved
        # store is a DIFFERENT authority — resolution fails instead of
        # silently reading the new location.
        import os

        current = os.path.abspath(str(opencode_auth_store_path()))
        import hashlib

        current_fp = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if current_fp != binding.source_ref:
            value = None
        else:
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
