"""Provider registry orchestration: mutation, transactions, migration, recovery.

Single responsibility: everything that MUTATES the durable registry or
orchestrates recovery -- collision-safe identity generation, the pre-armed
durable commit transaction (:func:`commit_provider_and_credential`), the
effective-model persistence gate, add/update/delete/manual-model flows,
strict state purges, and legacy auto-seeded builtin migration.

Orchestrator discipline: every sibling-authority call resolves through the
owning module (``_provider_config`` / ``_credentials`` / ``_catalog`` /
``_protocols`` / ``_identity``) so patch seams and credential backends stay
at their authorities. Preserved lazy edges: :class:`CredentialVault` and the
model display-name helper are imported inside functions exactly as before.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from agentic_debugger.application import provider_catalog as _catalog
from agentic_debugger.application import provider_config as _provider_config
from agentic_debugger.application import provider_credentials as _credentials
from agentic_debugger.application import provider_identity as _identity
from agentic_debugger.application import provider_protocols as _protocols
from agentic_debugger.application.provider_http import (
    ProviderHttpError,
    validate_and_canonicalize_url,
)
from agentic_debugger.application.provider_identity import (
    AUTH_BEARER,
    AUTH_MODES,
    AUTH_NONE,
    CATALOG_MODES,
    CATALOG_OPENAI,
    TRANSPORT_GENERIC,
    TRANSPORT_MODES,
    ProviderConnectionError,
    _PROTOCOL_FAMILIES,
)
from agentic_debugger.application.provider_config import (
    DiscoveredProviderModel,
    ProviderConfig,
    _MAX_DISPLAY_NAME_CHARS,
    _MAX_MODEL_ID_CHARS,
    _MAX_MODELS_PER_PROVIDER,
    _MAX_PROVIDER_ID_CHARS,
    _MAX_PROVIDER_NAME_CHARS,
    _MAX_PROVIDERS_CONFIGURED,
    _MODEL_ID_PATTERN,
)
def _purge_provider_state_strict(provider_id: str) -> None:
    """Strictly purge all reusable state for one user-owned provider.

    Used by :func:`delete_provider_config` and intentionally reuses the same
    strict boundaries as legacy migration so the two paths cannot diverge.

    1. If a saved credential exists, delete it and verify absence.
    2. Clear the process/session key.
    3. Strictly purge this provider's cached catalog entry (reusing the
       strict ``_purge_legacy_cached_catalog`` boundary — missing file/entry
       is success, corrupt/unreadable/unwritable is failure).
    4. Clear this provider's quarantine marker and verify it is no longer
       quarantined, preserving corrupt-quarantine fail-closed semantics.

    Any failure raises :class:`ProviderConnectionError` and leaves the
    provider configuration on disk so deletion is retryable.
    """

    from agentic_debugger.application.credential_vault import CredentialVault

    CredentialVault.default().revoke_credential(provider_id)
    _catalog._purge_legacy_cached_catalog(provider_id)
    _provider_config.clear_provider_quarantine(provider_id)
    if _provider_config.is_provider_quarantined(provider_id):
        raise ProviderConnectionError(
            "provider quarantine cleanup could not be completed"
        )


def _migrate_legacy_builtin_records(
    cleaned_configs: List[ProviderConfig],
    legacy_ids: List[str],
) -> None:
    """Migrate away legacy auto-seeded built-in provider records and purge their orphan state.

    Invariant: All associated legacy state (credentials, session keys, cached
    catalogs, and quarantine markers) MUST be successfully and verifiably purged
    BEFORE the cleaned configuration is committed to disk. If any cleanup step
    or the final configuration persistence fails, a ProviderConnectionError is
    raised and the durable legacy records remain on disk so migration remains
    retryable.
    """
    from agentic_debugger.application.credential_vault import CredentialVault

    for pid in legacy_ids:
        try:
            CredentialVault.default().revoke_credential(pid)
        except ProviderConnectionError:
            raise ProviderConnectionError(
                f"legacy provider credential cleanup failed for {pid!r}"
            ) from None
        _catalog._purge_legacy_cached_catalog(pid)
        _provider_config.clear_provider_quarantine(pid)
        if _provider_config.is_provider_quarantined(pid):
            raise ProviderConnectionError(
                f"legacy provider quarantine cleanup failed for {pid!r}"
            )

    try:
        _provider_config.save_provider_configurations(cleaned_configs)
    except Exception as exc:
        raise ProviderConnectionError(
            "legacy provider migration could not save updated configuration"
        ) from exc

def _clean_slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", text.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:32] or "custom_provider"


def _generate_provider_id(name: str, existing: set[str]) -> str:
    """Generate a collision-safe provider ID within the bounded grammar.

    The base slug is truncated to leave room for ``_<counter>`` suffixes
    so EVERY generated ID — including collision suffixes at the maximum
    length — satisfies the same 1-32 lowercase/digit/underscore grammar
    as explicit IDs.  Never overwrites an existing identity.
    """
    base = _clean_slug(name)
    if base not in existing and _provider_config.is_valid_provider_id(base):
        return base
    # Reserve room for the suffix: "_<counter>" must fit in 32 chars.
    counter = 1
    while True:
        suffix = f"_{counter}"
        room = _MAX_PROVIDER_ID_CHARS - len(suffix)
        truncated = base[:room].rstrip("_") or "custom_provider"
        # Ensure truncation did not break the leading-alnum rule.
        if not truncated or not truncated[0].isalnum():
            truncated = ("p" + truncated)[:room]
        candidate = f"{truncated}{suffix}"
        if candidate not in existing and _provider_config.is_valid_provider_id(candidate):
            return candidate
        counter += 1
        if counter > 100000:
            raise ProviderConnectionError(
                "provider identity could not be generated within its bound"
            )


def commit_provider_and_credential(
    provider_id: str,
    updated_configs: List[ProviderConfig],
    api_key: Optional[str],
) -> None:
    """Commit one logical provider save with pre-armed durable quarantine.

    Backend transaction beneath the V2-04 :class:`CredentialVault`: the
    product write path invokes the vault
    (``CredentialVault.commit_provider_save``), which delegates here
    verbatim.  Invariant: NO credential mutation may begin unless the
    provider is already
    protected by a restart-durable fail-closed marker.  The quarantine is
    armed BEFORE any secure-credential write; if arming fails the transaction
    aborts with no mutation.  After a fully coherent commit the quarantine is
    cleared.  Rollback paths keep the two durable stores consistent:

    - An unusable key or a failed secure-store write raises before the
      configuration is touched, so the persisted configuration stays
      authoritative.
    - If the configuration write fails after the credential was replaced, the
      previous credential is restored; when no previous credential existed
      (or restoration itself fails) the newly written credential is deleted,
      failing closed so no key is left bound to a configuration that never
      accepted it and no old credential can travel to a newly requested
      endpoint.  Credential values never leave this boundary, including in
      error text.
    - If restore AND delete both fail, the already-armed durable quarantine
      keeps the provider blocked in the current process and after restart
      until the operator explicitly saves a coherent credential pair again.
    """
    stripped = api_key.strip() if api_key is not None else ""
    if not stripped:
        _provider_config.save_provider_configurations(updated_configs)
        return
    if not _credentials._credential_is_usable(stripped):
        raise ProviderConnectionError("API key is missing, invalid, or oversized")
    # Durably arm fail-closed marker before mutating any credential
    try:
        _provider_config.quarantine_provider(provider_id)
    except ProviderConnectionError:
        raise ProviderConnectionError("provider credential quarantine could not be armed; no provider changes were applied") from None
    previous_credential: Optional[str] = _credentials.load_secure_credential(provider_id)
    if not _credentials.save_secure_credential(provider_id, stripped):
        # Credential save failed after arming: original pair still coherent
        try:
            _provider_config.clear_provider_quarantine(provider_id)
        except ProviderConnectionError:
            pass
        raise ProviderConnectionError("Could not save API key securely.") from None
    try:
        _provider_config.save_provider_configurations(updated_configs)
    except ProviderConnectionError:
        if previous_credential is not None and _credentials.save_secure_credential(
            provider_id, previous_credential
        ):
            try:
                _provider_config.clear_provider_quarantine(provider_id)
            except ProviderConnectionError:
                pass
            rollback_note = "the previous credential state was restored"
        elif _credentials.delete_secure_credential(provider_id):
            rollback_note = (
                "no API key remains stored for this provider"
                if previous_credential is None
                else "the stored API key could not be restored and was removed; re-enter it before retrying"
            )
        else:
            _credentials.clear_session_key(provider_id)
            rollback_note = (
                "the credential state requires recovery; "
                "edit this provider and save an API key again"
            )
        raise ProviderConnectionError(
            "provider configuration could not be written; no provider changes "
            f"were applied and {rollback_note}"
        ) from None
    try:
        _provider_config.clear_provider_quarantine(provider_id)
    except ProviderConnectionError:
        pass


def _check_effective_models(
    auth_mode: str,
    transport_profile: str,
    api_format: str,
    models: Tuple[DiscoveredProviderModel, ...],
) -> None:
    """Validate every model's effective protocol before persistence.

    Each model executes under its explicit protocol when set, otherwise
    the provider default.  Both the authentication matrix and the
    explicit transport-profile capability (inference-path availability)
    are enforced, so an unsupported effective combination can never
    persist, appear runnable, or reach LiveModelConfig creation.
    """
    try:
        _identity.validate_auth_protocol_combination(auth_mode, api_format)
    except ProviderConnectionError as exc:
        raise ProviderConnectionError(str(exc)) from None
    try:
        _protocols._inference_path_for_profile(transport_profile, api_format)
    except ProviderConnectionError as exc:
        raise ProviderConnectionError(str(exc)) from None
    for m in models:
        effective = m.protocol if m.protocol is not None else api_format
        try:
            _identity.validate_auth_protocol_combination(auth_mode, effective)
        except ProviderConnectionError as exc:
            raise ProviderConnectionError(
                f"model {m.model_id!r}: {exc}"
            ) from None
        try:
            _protocols._inference_path_for_profile(transport_profile, effective)
        except ProviderConnectionError as exc:
            raise ProviderConnectionError(
                f"model {m.model_id!r}: {exc}"
            ) from None


def add_provider_config(
    name: str,
    base_url: str,
    api_format: str,
    *,
    api_key: Optional[str] = None,
    provider_id: Optional[str] = None,
    models: Tuple[DiscoveredProviderModel, ...] = (),
    auth_mode: str = AUTH_BEARER,
    catalog_mode: str = CATALOG_OPENAI,
    transport_profile: str = TRANSPORT_GENERIC,
) -> ProviderConfig:
    """Add a new configured model provider.

    Strict ADD semantics: an explicit ``provider_id`` that already exists
    fails as "already exists" — mutation of an existing provider belongs
    only to :func:`update_provider_config`.  Generated IDs always satisfy
    the bounded grammar, including collision suffixes at the maximum
    length, and a successful add always survives immediate reload.
    Fails closed on unknown protocol/auth/catalog/transport families,
    unsupported auth/protocol combinations, transport-incapable protocols,
    or a registry already at its bound.

    ``transport_profile`` is always explicit and never inferred from the
    technical ID, name, or endpoint: ``generic`` (the default, even when
    the ID equals a historical identifier) selects OpenAI-compatible
    transport semantics, while the historical profiles preserve the
    corresponding endpoint contracts intentionally.
    """
    if type(name) is not str or not name.strip():
        raise ProviderConnectionError("Provider name is required")
    if len(name.strip()) > _MAX_PROVIDER_NAME_CHARS:
        raise ProviderConnectionError("Provider name is oversized")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ProviderConnectionError("Provider name contains control characters")
    if type(base_url) is not str or not base_url.strip():
        raise ProviderConnectionError("Base URL is required")
    try:
        clean_url = validate_and_canonicalize_url(base_url)
    except ProviderHttpError as exc:
        raise ProviderConnectionError(str(exc)) from None
    if type(api_format) is not str or api_format not in _PROTOCOL_FAMILIES:
        raise ProviderConnectionError(
            f"unknown API protocol format: {api_format!r}"
        )
    if type(auth_mode) is not str or auth_mode not in AUTH_MODES:
        raise ProviderConnectionError(f"unknown authentication mode: {auth_mode!r}")
    if type(catalog_mode) is not str or catalog_mode not in CATALOG_MODES:
        raise ProviderConnectionError(f"unknown catalog mode: {catalog_mode!r}")
    if type(transport_profile) is not str or transport_profile not in TRANSPORT_MODES:
        raise ProviderConnectionError(
            f"unknown transport profile: {transport_profile!r}"
        )
    if auth_mode == AUTH_NONE:
        from urllib.parse import urlsplit as _split

        host = (_split(clean_url).hostname or "").lower()
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ProviderConnectionError(
                "no-auth providers are accepted only for loopback/self-hosted endpoints"
            )
        if api_key is not None and api_key.strip():
            raise ProviderConnectionError(
                "no-auth providers must not store an API key"
            )

    configs = _provider_config.load_provider_configurations()
    existing_ids = {c.provider_id for c in configs}
    if provider_id is None:
        pid = _generate_provider_id(name, existing_ids)
    else:
        if type(provider_id) is not str:
            raise ProviderConnectionError(
                "provider id must be 1-32 characters of lowercase letters, "
                "digits, or underscores, starting with a letter or digit"
            )
        pid = provider_id.strip()
        if not _provider_config.is_valid_provider_id(pid):
            raise ProviderConnectionError(
                "provider id must be 1-32 characters of lowercase letters, "
                "digits, or underscores, starting with a letter or digit"
            )
        if pid in existing_ids:
            raise ProviderConnectionError(
                f"provider {pid!r} already exists; use update to modify it"
            )

    if len(configs) >= _MAX_PROVIDERS_CONFIGURED:
        # Fail before any credential or configuration mutation: an
        # over-bound add must never report success or truncate state.
        raise ProviderConnectionError(
            "provider limit reached "
            f"({_MAX_PROVIDERS_CONFIGURED} configured providers); "
            "delete one before adding another"
        )

    if type(models) is not tuple:
        raise ProviderConnectionError("provider configuration entry is invalid")
    for m in models:
        if type(m) is not DiscoveredProviderModel or m.kind != pid:
            raise ProviderConnectionError("provider configuration entry is invalid")
        if (
            type(m.model_id) is not str
            or not m.model_id.strip()
            or len(m.model_id.strip()) > _MAX_MODEL_ID_CHARS
            or _MODEL_ID_PATTERN.fullmatch(m.model_id.strip()) is None
        ):
            raise ProviderConnectionError("provider configuration entry is invalid")
    _check_effective_models(auth_mode, transport_profile, api_format, models)

    new_cfg = ProviderConfig(
        provider_id=pid,
        name=name.strip(),
        base_url=clean_url,
        api_format=api_format,
        models=models,
        enabled=True,
        is_builtin=False,
        auth_mode=auth_mode,
        catalog_mode=catalog_mode,
        transport_profile=transport_profile,
    )
    _provider_config.validate_provider_config_for_write(new_cfg)
    updated = [c for c in configs if c.provider_id != pid] + [new_cfg]
    from agentic_debugger.application.credential_vault import CredentialVault
    CredentialVault.default().commit_provider_save(pid, updated, api_key)
    if api_key is not None and api_key.strip():
        _credentials.clear_session_key(pid)
        try:
            _provider_config.clear_provider_quarantine(pid)
        except ProviderConnectionError:
            pass

    return new_cfg


def update_provider_config(
    provider_id: str,
    *,
    name: Optional[str] = None,
    base_url: Optional[str] = None,
    api_format: Optional[str] = None,
    models: Optional[Tuple[DiscoveredProviderModel, ...]] = None,
    enabled: Optional[bool] = None,
    api_key: Optional[str] = None,
    auth_mode: Optional[str] = None,
    catalog_mode: Optional[str] = None,
    transport_profile: Optional[str] = None,
) -> ProviderConfig:
    """Update metadata for an existing provider.

    UPDATE is the only mutation route for an existing provider identity.
    Fails closed on an explicitly requested unknown protocol/auth family
    (the previous value is never silently preserved for an invalid
    request) and on endpoint/credential rebinding: when the canonical
    Base URL or authentication contract changes and ANY reusable
    credential source (secure store, session key, forwarded session
    environment, provider environment variable, or consumable CLI auth
    store) is already associated with this provider, the API key must be
    re-entered in the same transaction — a blank key must never silently
    carry an implicitly endpoint-tied credential to the newly requested
    endpoint.  Blank-key edits that keep the endpoint and auth contract
    unchanged continue to preserve the credential.
    """
    configs = _provider_config.load_provider_configurations()
    existing = next((c for c in configs if c.provider_id == provider_id), None)
    if existing is None:
        raise ProviderConnectionError(f"Provider {provider_id!r} not found")

    if name is not None:
        if type(name) is not str or not name.strip():
            raise ProviderConnectionError("Provider name is required")
        if len(name.strip()) > _MAX_PROVIDER_NAME_CHARS:
            raise ProviderConnectionError("Provider name is oversized")
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ProviderConnectionError("Provider name contains control characters")
        new_name = name.strip()
    else:
        new_name = existing.name
    if base_url is not None:
        if type(base_url) is not str or not base_url.strip():
            raise ProviderConnectionError("Base URL is required")
        try:
            new_url = validate_and_canonicalize_url(base_url)
        except ProviderHttpError as exc:
            raise ProviderConnectionError(str(exc)) from None
    else:
        new_url = existing.base_url
    if api_format is not None:
        if type(api_format) is not str or api_format not in _PROTOCOL_FAMILIES:
            raise ProviderConnectionError(
                f"unknown API protocol format: {api_format!r}"
            )
        new_format = api_format
    else:
        new_format = existing.api_format
    if auth_mode is not None:
        if type(auth_mode) is not str or auth_mode not in AUTH_MODES:
            raise ProviderConnectionError(f"unknown authentication mode: {auth_mode!r}")
        new_auth = auth_mode
    else:
        new_auth = existing.auth_mode
    if catalog_mode is not None:
        if type(catalog_mode) is not str or catalog_mode not in CATALOG_MODES:
            raise ProviderConnectionError(f"unknown catalog mode: {catalog_mode!r}")
        new_catalog = catalog_mode
    else:
        new_catalog = existing.catalog_mode
    if transport_profile is not None:
        if type(transport_profile) is not str or transport_profile not in TRANSPORT_MODES:
            raise ProviderConnectionError(
                f"unknown transport profile: {transport_profile!r}"
            )
        new_profile = transport_profile
    else:
        new_profile = existing.transport_profile
    if models is not None:
        if type(models) is not tuple:
            raise ProviderConnectionError("provider configuration entry is invalid")
        new_models = models
    else:
        new_models = existing.models
    _check_effective_models(new_auth, new_profile, new_format, new_models)
    if new_auth == AUTH_NONE:
        from urllib.parse import urlsplit as _split

        host = (_split(new_url).hostname or "").lower()
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ProviderConnectionError(
                "no-auth providers are accepted only for loopback/self-hosted endpoints"
            )
    if enabled is not None:
        if type(enabled) is not bool:
            raise ProviderConnectionError("provider configuration entry is invalid")
        new_enabled = enabled
    else:
        new_enabled = existing.enabled

    blank_key = api_key is None or not api_key.strip()
    endpoint_changed = new_url != existing.base_url
    auth_changed = new_auth != existing.auth_mode
    profile_changed = new_profile != existing.transport_profile
    if (endpoint_changed or auth_changed) and blank_key and new_auth != AUTH_NONE:
        # Authority model: ANY pre-existing reusable credential source
        # blocks silent rebinding — not just saved/session.  Ambient
        # environment, forwarded session environment, and CLI-auth
        # credentials are implicitly tied to the historical endpoint and
        # must never become usable against an edited endpoint without an
        # explicitly valid rebinding.
        if _credentials.credential_source_for(provider_id) is not None:
            raise ProviderConnectionError(
                "Base URL changed while a reusable credential is stored for "
                "this provider; re-enter the API key so the credential is "
                "never silently bound to the new endpoint"
            )
    if new_auth == AUTH_NONE and api_key is not None and api_key.strip():
        raise ProviderConnectionError("no-auth providers must not store an API key")

    # When endpoint, auth mode, or transport profile changes, the old
    # catalog cache belongs to the previous endpoint and is invalidated.
    if endpoint_changed or auth_changed or profile_changed:
        _catalog.delete_cached_catalog(provider_id)
        if models is None:
            new_models = ()
        new_last_refresh_utc = None
        new_last_refresh_source = None
    else:
        new_last_refresh_utc = existing.last_refresh_utc
        new_last_refresh_source = existing.last_refresh_source

    updated_cfg = ProviderConfig(
        provider_id=existing.provider_id,
        name=new_name,
        base_url=new_url,
        api_format=new_format,
        models=new_models,
        last_refresh_utc=new_last_refresh_utc,
        last_refresh_source=new_last_refresh_source,
        enabled=new_enabled,
        is_builtin=existing.is_builtin,
        builtin_kind=existing.builtin_kind,
        tls_signature_blocked=existing.tls_signature_blocked,
        auth_mode=new_auth,
        catalog_mode=new_catalog,
        transport_profile=new_profile,
    )
    _provider_config.validate_provider_config_for_write(updated_cfg)
    updated = [updated_cfg if c.provider_id == provider_id else c for c in configs]
    from agentic_debugger.application.credential_vault import CredentialVault
    CredentialVault.default().commit_provider_save(provider_id, updated, api_key)
    if api_key is not None and api_key.strip():
        _credentials.clear_session_key(provider_id)
        try:
            _provider_config.clear_provider_quarantine(provider_id)
        except ProviderConnectionError:
            pass

    return updated_cfg


def delete_provider_config(provider_id: str) -> bool:
    """Delete a provider configuration and all of its reusable state.

    Strict invariant (see :func:`_purge_provider_state_strict`): a successful
    deletion guarantees that *all* of the following are true::

        provider configuration absent
        saved secure credential absent
        process/session credential absent
        cached provider catalog absent
        provider-specific valid quarantine marker absent

    Ordering is strict and fail-closed::

        verify provider exists
            ↓
        strictly purge reusable provider state
            ↓
        atomically persist filtered provider config LAST

    If any purge step fails, :class:`ProviderConnectionError` is raised and the
    provider configuration remains durably present so the operation is
    retryable.  If final config persistence fails after state cleanup, the
    provider remains configured but disconnected (no credential/catalog) until
    retry — fail-closed and truthful.

    Returns:
        True if the provider was present and fully deleted, False if no such
        provider existed.  Cleanup failures raise.

    Deleting provider A never touches provider B's credential, catalog entry,
    quarantine state, or configuration.
    """

    configs = _provider_config.load_provider_configurations()
    existing = next((c for c in configs if c.provider_id == provider_id), None)
    if existing is None:
        return False
    filtered = [c for c in configs if c.provider_id != provider_id]
    if len(filtered) == len(configs):
        return False
    # Strict purge BEFORE config persistence so no orphan credential/catalog
    # can survive a successful ``return True``.
    _purge_provider_state_strict(provider_id)
    try:
        _provider_config.save_provider_configurations(filtered)
    except ProviderConnectionError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise ProviderConnectionError(
            "provider configuration could not be written"
        ) from exc
    return True


def add_manual_model(
    provider_id: str,
    model_id: str,
    display_name: Optional[str] = None,
    protocol: Optional[str] = None,
) -> DiscoveredProviderModel:
    """Add one model manually to a provider's catalog (fallback for no /models).

    Manual models obey the same strict contract as discovered models:
    bounded model-ID grammar (no control characters, no oversize IDs),
    bounded display names, model-count and duplicate bounds, and explicit
    protocol validation.  An explicitly supplied unknown protocol fails;
    ``None`` deliberately means "use the provider default" per this
    documented API contract.
    """
    cfg = _provider_config.get_provider_config(provider_id)
    if cfg is None:
        raise ProviderConnectionError(f"Provider {provider_id!r} not found")
    if type(model_id) is not str or not model_id.strip():
        raise ProviderConnectionError("Model ID is required")
    mid = model_id.strip()
    if len(mid) > _MAX_MODEL_ID_CHARS:
        raise ProviderConnectionError("Model ID is oversized")
    if _MODEL_ID_PATTERN.fullmatch(mid) is None:
        raise ProviderConnectionError("Model ID contains unsupported characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in mid):
        raise ProviderConnectionError("Model ID contains control characters")
    from agentic_debugger.application.model_providers import format_model_display_name

    if display_name is not None:
        if type(display_name) is not str:
            raise ProviderConnectionError("Model display name is invalid")
        if display_name.strip():
            disp = display_name.strip()
            if len(disp) > _MAX_DISPLAY_NAME_CHARS:
                raise ProviderConnectionError("Model display name is oversized")
            if any(ord(c) < 32 or ord(c) == 127 for c in disp):
                raise ProviderConnectionError("Model display name contains control characters")
        else:
            disp = format_model_display_name(mid)
    else:
        disp = format_model_display_name(mid)
    if protocol is not None:
        if type(protocol) is not str or protocol not in _PROTOCOL_FAMILIES:
            raise ProviderConnectionError(f"unknown API protocol format: {protocol!r}")
        proto: Optional[str] = protocol
    else:
        # Provider default: for historical profiles the default is the
        # authoritative profile routing (table / routing rule), NOT the
        # uniform api_format — otherwise a manual "Provider default" would
        # mask profile routing (e.g. storing chat_completions for an
        # OpenCode Responses model).  Unknown historical ids stay None
        # (unresolved, fail closed) so the browser shows Unresolved with
        # an explicit-override tip instead of a misrouted Ready.
        _profile_default = getattr(cfg, "transport_profile", TRANSPORT_GENERIC)
        if _profile_default == _identity.TRANSPORT_OPENCODE_GO:
            try:
                proto = _protocols.resolve_opencode_go_protocol(mid)
            except Exception:
                proto = None
        elif _profile_default == _identity.TRANSPORT_COMMANDCODE_GOAT:
            try:
                proto = _protocols.resolve_commandcode_protocol(mid)
            except Exception:
                proto = None
        else:
            proto = cfg.api_format
    # The effective model protocol must satisfy the provider
    # authentication matrix and the explicit transport-profile
    # capability: unsupported effective combinations are rejected here,
    # before persistence, rather than surfacing as harness failures later.
    # A None derived default for an unknown historical model is allowed to
    # persist as Unresolved (fail closed at read/use time).
    if proto is not None:
        try:
            _identity.validate_auth_protocol_combination(cfg.auth_mode, proto)
        except ProviderConnectionError as exc:
            raise ProviderConnectionError(f"model {mid!r}: {exc}") from None
        try:
            _protocols._inference_path_for_profile(cfg.transport_profile, proto)
        except ProviderConnectionError as exc:
            raise ProviderConnectionError(f"model {mid!r}: {exc}") from None
    if len(cfg.models) >= _MAX_MODELS_PER_PROVIDER and all(
        m.model_id != mid for m in cfg.models
    ):
        raise ProviderConnectionError("provider model catalog exceeds its bound")
    new_model = DiscoveredProviderModel(
        kind=provider_id,
        model_id=mid,
        display_name=disp,
        protocol=proto,
        runnable=proto is not None,
        unavailable_reason=None if proto else "Protocol unresolved",
    )
    existing_models = [m for m in cfg.models if m.model_id != mid]
    updated_models = tuple(sorted(existing_models + [new_model], key=lambda m: (m.model_id.lower(), m.model_id)))
    update_provider_config(provider_id, models=updated_models)
    return new_model
