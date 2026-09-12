"""Catalog discovery, cache, presence-only status, and live probing.

Single responsibility: OBSERVATION of providers -- normalized catalog snapshots,
the bounded catalog cache (including strict legacy-entry purges owned here),
the presence-only connection/readiness view (never a reachability claim, never
carrying secrets), explicit live catalog refresh, and the side-effect-free
connection probe.

Cross-module reads resolve through the owning module (``_config`` /
``_credentials`` / ``_protocols`` / ``_identity``). One documented lazy edge:
refresh persists discovered models through :mod:`provider_management` inside
the function (avoids a static cycle; the lookup was a plain module-global
before the split). The model display-name helper stays a preserved lazy
import from :mod:`agentic_debugger.application.model_providers`.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agentic_debugger.application import provider_config as _config
from agentic_debugger.application import provider_credentials as _credentials
from agentic_debugger.application import provider_identity as _identity
from agentic_debugger.application import provider_protocols as _protocols
from agentic_debugger.application.provider_config import DiscoveredProviderModel
from agentic_debugger.application.provider_http import (
    ProviderHttpError,
    describe_url,
    request_json,
    sanitize_text,
)
from agentic_debugger.application.provider_identity import (
    AUTH_BEARER,
    AUTH_NONE,
    CATALOG_DISABLED,
    CATALOG_OPENAI,
    DIRECT_API_PROVIDER_KINDS,
    PROTOCOL_CHAT_COMPLETIONS,
    TRANSPORT_GENERIC,
    ProviderConnectionError,
    _PROTOCOL_FAMILIES,
)
# -- discovered models and snapshots -----------------------------------------

_MAX_CATALOG_MODELS = 256
_MAX_MODEL_ID_CHARS = 128
_CATALOG_FETCH_TIMEOUT_SECONDS = 30.0
_CATALOG_MAX_RESPONSE_BYTES = 1024 * 1024
_SNAPSHOT_SOURCE_LIVE = "live"


@dataclass(frozen=True)
class ProviderCatalogSnapshot:
    """One bounded normalized catalog result."""

    kind: str
    fetched_at_utc: str
    source: str
    models: Tuple[DiscoveredProviderModel, ...]
    truncated: bool = False

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "fetched_at_utc": self.fetched_at_utc,
            "source": self.source,
            "truncated": self.truncated,
            "models": [
                {"model_id": item.model_id, "protocol": item.protocol}
                for item in self.models
            ],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Optional["ProviderCatalogSnapshot"]:
        if not isinstance(value, Mapping):
            return None
        kind = value.get("kind")
        fetched = value.get("fetched_at_utc")
        source = value.get("source")
        models = value.get("models")
        truncated = value.get("truncated")
        if not kind or not isinstance(kind, str):
            return None
        if kind not in DIRECT_API_PROVIDER_KINDS and not _config.is_known_provider(kind):
            return None
        if type(fetched) is not str or not _config._valid_utc(fetched):
            return None
        if source != _SNAPSHOT_SOURCE_LIVE:
            return None
        if type(truncated) is not bool:
            return None
        if type(models) is not list or len(models) > _MAX_CATALOG_MODELS:
            return None
        pattern = _protocols._catalog_pattern_for(kind)
        decoded: List[DiscoveredProviderModel] = []
        for entry in models:
            if not isinstance(entry, Mapping):
                return None
            model_id = entry.get("model_id")
            protocol = entry.get("protocol")
            if (
                type(model_id) is not str
                or not model_id
                or len(model_id) > _MAX_MODEL_ID_CHARS
                or re.match(pattern, model_id) is None
            ):
                return None
            if protocol is not None and protocol not in _PROTOCOL_FAMILIES:
                return None
            # Self-healing: derived route truth is recomputed from CURRENT
            # provider-runtime authority.  A stale cached derived protocol
            # (e.g. None persisted before the route table knew this id)
            # must neither permanently override current truth nor discard
            # the entire snapshot.  The stored value is validated as
            # known-or-None above (fail closed for unknown strings) but the
            # EFFECTIVE protocol below is always the current resolution
            # (explicit override -> profile table -> default -> unresolved).
            try:
                current = _protocols.resolve_model_protocol(kind, model_id)
            except Exception:
                # Provider missing/disabled/unreadable: preserve stored
                # verbatim (fail safe, neither heal nor discard).
                current = protocol
            display = _display_name(kind, model_id)
            decoded.append(
                DiscoveredProviderModel(
                    kind=kind,
                    model_id=model_id,
                    display_name=display,
                    protocol=current,
                    runnable=current is not None,
                    unavailable_reason=None if current is not None else "Protocol not yet resolved for direct API",
                )
            )
        ids = [item.model_id for item in decoded]
        if ids != sorted(set(ids), key=lambda item: (item.lower(), item)):
            return None
        return cls(
            kind=kind,
            fetched_at_utc=fetched,
            source=source,
            models=tuple(decoded),
            truncated=truncated,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _display_name(kind: str, model_id: str) -> str:
    from agentic_debugger.application.model_providers import format_model_display_name

    return format_model_display_name(model_id)


def _normalize_catalog(
    kind: str, payload: Mapping[str, Any]
) -> Tuple[Tuple[DiscoveredProviderModel, ...], bool]:
    pattern = re.compile(_protocols._catalog_pattern_for(kind))

    data = payload.get("data")
    if data is None:
        data = payload.get("models")
    if type(data) is not list:
        raise ProviderConnectionError("catalog response has no model list")
    seen: Dict[str, None] = {}
    for entry in data:
        if isinstance(entry, str):
            model_id = entry
        elif isinstance(entry, Mapping):
            model_id = entry.get("id") or entry.get("name") or entry.get("model")
        else:
            continue
        if type(model_id) is not str:
            continue
        stripped = model_id.strip()
        if not stripped or len(stripped) > _MAX_MODEL_ID_CHARS:
            continue
        if pattern.match(stripped) is None:
            continue
        seen.setdefault(stripped, None)
    ordered = sorted(seen, key=lambda item: (item.lower(), item))
    truncated = len(ordered) > _MAX_CATALOG_MODELS
    if truncated:
        ordered = ordered[:_MAX_CATALOG_MODELS]
    models = tuple(
        DiscoveredProviderModel.create(kind, model_id, _display_name(kind, model_id))
        for model_id in ordered
    )
    return models, truncated


# -- catalog cache ------------------------------------------------------------

_CACHE_SCHEMA_VERSION = "provider-catalog-cache-v1"
_MAX_CACHE_FILE_BYTES = 512 * 1024


def catalog_cache_path() -> Path:
    override = os.environ.get("AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH")
    if override and override.strip():
        return Path(override.strip())
    config_path = _config.provider_configurations_path()
    return config_path.with_name("provider-catalog-cache.json")


def load_cached_catalog(kind: str) -> Optional[ProviderCatalogSnapshot]:
    if not _config.is_known_provider(kind):
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    path = catalog_cache_path()
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > _MAX_CACHE_FILE_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping) or value.get("schema_version") != _CACHE_SCHEMA_VERSION:
        return None
    providers = value.get("providers")
    if not isinstance(providers, Mapping):
        return None
    return ProviderCatalogSnapshot.from_mapping(providers.get(kind))


def save_cached_catalog(snapshot: ProviderCatalogSnapshot) -> None:
    if not _config.is_known_provider(snapshot.kind):
        raise ProviderConnectionError(
            f"provider {snapshot.kind!r} is not configured"
        )
    path = catalog_cache_path()
    existing: Dict[str, Any] = {}
    try:
        raw = path.read_bytes()
        if len(raw) <= _MAX_CACHE_FILE_BYTES:
            decoded = json.loads(raw.decode("utf-8"))
            if (
                isinstance(decoded, Mapping)
                and decoded.get("schema_version") == _CACHE_SCHEMA_VERSION
                and isinstance(decoded.get("providers"), Mapping)
            ):
                for k, v in decoded["providers"].items():
                    preserved = ProviderCatalogSnapshot.from_mapping(v)
                    if preserved is not None:
                        existing[k] = preserved.to_mapping()
    except (OSError, UnicodeError, json.JSONDecodeError):
        existing = {}
    existing[snapshot.kind] = snapshot.to_mapping()
    payload = json.dumps(
        {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "providers": existing,
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=1,
    ).encode("utf-8")
    if len(payload) > _MAX_CACHE_FILE_BYTES:
        raise ProviderConnectionError("provider catalog cache exceeded its bound")
    temporary: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise ProviderConnectionError("provider catalog cache could not be written") from None


def delete_cached_catalog(kind: str) -> None:
    """Purge any cached catalog entry for one provider."""
    path = catalog_cache_path()
    try:
        raw = path.read_bytes()
    except OSError:
        return
    if len(raw) > _MAX_CACHE_FILE_BYTES:
        return
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return
    if (
        not isinstance(decoded, Mapping)
        or decoded.get("schema_version") != _CACHE_SCHEMA_VERSION
        or not isinstance(decoded.get("providers"), dict)
    ):
        return
    if kind not in decoded["providers"]:
        return
    del decoded["providers"][kind]
    payload = json.dumps(
        {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "providers": decoded["providers"],
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=1,
    ).encode("utf-8")
    if len(payload) > _MAX_CACHE_FILE_BYTES:
        return
    temporary: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


# -- strict legacy cache purge (owned by the cache authority) ----------------
#
# Strictly purges one legacy provider entry from the catalog cache file.
# Registry orchestration (:mod:`provider_management`) calls this during
# migration and deletion; the file-format authority stays here.

def _purge_legacy_cached_catalog(kind: str) -> None:
    """Strictly purge cached catalog for one legacy provider during migration.

    Fails closed if the cache file cannot be read, is corrupt/oversized/invalid,
    or cannot be written back cleanly.
    """
    path = catalog_cache_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ProviderConnectionError(
            "provider catalog cache could not be read during legacy migration"
        ) from exc

    if len(raw) > _MAX_CACHE_FILE_BYTES:
        raise ProviderConnectionError(
            "provider catalog cache exceeded file bound during legacy migration"
        )
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderConnectionError(
            "provider catalog cache is malformed during legacy migration"
        ) from exc

    if (
        not isinstance(decoded, Mapping)
        or decoded.get("schema_version") != _CACHE_SCHEMA_VERSION
        or not isinstance(decoded.get("providers"), dict)
    ):
        raise ProviderConnectionError(
            "provider catalog cache is invalid during legacy migration"
        )

    providers = decoded["providers"]
    if kind not in providers:
        return

    del providers[kind]
    payload = json.dumps(
        {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "providers": providers,
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=1,
    ).encode("utf-8")
    if len(payload) > _MAX_CACHE_FILE_BYTES:
        raise ProviderConnectionError(
            "provider catalog cache exceeded file bound during legacy migration"
        )

    temporary: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise ProviderConnectionError(
            "provider catalog cache could not be written during legacy migration"
        ) from exc
# -- connection status --------------------------------------------------------

_CATALOG_STALE_AFTER = timedelta(days=7)


@dataclass(frozen=True)
class ProviderConnectionStatus:
    """One provider's current connection view (credential-free)."""

    kind: str
    label: str
    base_url: str
    connected: bool
    credential_source: Optional[str]
    model_count: int
    last_refresh_utc: Optional[str]
    last_refresh_source: Optional[str]
    stale: bool
    status_message: Optional[str] = None
    cached_models: Tuple[DiscoveredProviderModel, ...] = ()
    api_format: str = PROTOCOL_CHAT_COMPLETIONS
    enabled: bool = True
    is_builtin: bool = False
    auth_mode: str = AUTH_BEARER
    catalog_mode: str = CATALOG_OPENAI
    transport_profile: str = TRANSPORT_GENERIC
    runnable: bool = False
    runnable_reason: Optional[str] = None
    is_configured: bool = True
    credential_ready: bool = False
    live_verified: bool = False
    live_verified_at_utc: Optional[str] = None
    runtime_succeeded_at_utc: Optional[str] = None


def _cached_status_fields(
    kind: str,
) -> Tuple[int, Optional[str], Optional[str], bool, Tuple[DiscoveredProviderModel, ...]]:
    snapshot = load_cached_catalog(kind)
    if snapshot is None:
        return 0, None, None, False, ()
    try:
        fetched = datetime.fromisoformat(
            snapshot.fetched_at_utc.replace("Z", "+00:00")
        )
    except ValueError:
        return 0, None, None, False, ()
    stale = datetime.now(timezone.utc) - fetched > _CATALOG_STALE_AFTER
    return (
        len(snapshot.models),
        snapshot.fetched_at_utc,
        snapshot.source,
        stale,
        snapshot.models,
    )


def provider_connection_status(kind: str) -> ProviderConnectionStatus:
    """Presence-only connection view; never contacts the provider.

    Truthful states (never mere persisted metadata as "connected"):
    - quarantined → recovery required, not connected, not runnable;
    - disabled → not connected, not runnable;
    - no-auth loopback → connected without a credential; runnable iff at
      least one runnable model exists;
    - bearer/anthropic → connected iff a usable credential source exists
      under the endpoint-binding authority; runnable iff connected and at
      least one runnable model exists;
    - manual-model-only providers are runnable without a catalog.
    """
    cfg = _config.get_provider_config(kind)
    if cfg is None:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")

    contract = _identity._contract_for_config(cfg)
    source = _credentials.credential_source_for(kind)
    model_count, last_refresh, refresh_source, stale, cached = _cached_status_fields(kind)

    label = cfg.name
    base_url_desc = describe_url(cfg.base_url)
    api_format = cfg.api_format
    enabled = cfg.enabled
    is_builtin = cfg.is_builtin
    auth_mode = cfg.auth_mode
    catalog_mode = cfg.catalog_mode
    transport_profile = cfg.transport_profile
    if not cached and cfg.models:
        cached = cfg.models
        model_count = len(cfg.models)
        last_refresh = cfg.last_refresh_utc
        refresh_source = cfg.last_refresh_source

    # "Runnable" means runnable through the exact execution route: the
    # model's EFFECTIVE protocol must satisfy the provider authentication
    # matrix and the explicit transport-profile capability.  A model with
    # an unsupported effective pair never counts as runnable here.
    # Self-healing: the effective protocol is recomputed from CURRENT
    # runtime truth (snapshot-pure, explicit -> table -> default ->
    # unresolved); a stale persisted derived protocol never overrides it.
    def _healed_status_protocol(model_id: str, stored: Optional[str]) -> Optional[str]:
        try:
            return _protocols.resolve_model_protocol_for_config(cfg, model_id)
        except Exception:
            return stored

    _healed_pairs = [
        (m, _healed_status_protocol(m.model_id, m.protocol)) for m in cached
    ]
    runnable_models = [
        m
        for m, cur in _healed_pairs
        if cur is not None
        and _protocols.is_protocol_executable_for_config(cfg, cur)
    ]
    incompatible_models = [
        m for m, cur in _healed_pairs if cur is not None and m not in runnable_models
    ]
    quarantined = _config.is_provider_quarantined(kind)

    if not enabled:
        connected = False
        runnable = False
        message: Optional[str] = "Provider is disabled (edit provider to re-enable it)"
        runnable_reason: Optional[str] = message
    elif quarantined:
        connected = False
        runnable = False
        message = "Credential state requires recovery. Edit provider and save an API key again."
        runnable_reason = message
    elif auth_mode == AUTH_NONE:
        connected = True
        if runnable_models:
            runnable = True
            runnable_reason = None
            message = None
            if not model_count:
                message = None
        elif incompatible_models:
            runnable = False
            runnable_reason = (
                f"{len(incompatible_models)} configured model(s) use a protocol "
                "with no no-auth transport — only Chat Completions is supported "
                "without authentication"
            )
            message = runnable_reason
        else:
            runnable = False
            if catalog_mode == CATALOG_DISABLED:
                runnable_reason = "No models yet — add a manual model to make this provider runnable"
            else:
                runnable_reason = "No models yet — refresh the catalog or add a manual model"
            message = runnable_reason
        if source is None and message is None:
            message = None
    else:
        connected = source is not None
        if not connected:
            runnable = False
            if contract is not None and contract.env_var and not _credentials._endpoint_binding_valid(kind, cfg):
                message = (
                    "Credential binding requires recovery — the endpoint changed; "
                    "re-enter the API key for the new endpoint"
                )
            elif contract is not None and contract.env_var:
                message = (
                    f"Not connected — direct API needs {contract.env_var} or an API key "
                    "(edit provider to add an API key)"
                )
            else:
                message = "Not connected — no usable credential source found (edit provider to add an API key)"
            runnable_reason = message
        elif not runnable_models:
            runnable = False
            if incompatible_models:
                runnable_reason = (
                    f"{len(incompatible_models)} configured model(s) have no "
                    "executable transport for this provider's authentication "
                    "and transport profile — edit or remove them"
                )
            elif catalog_mode == CATALOG_DISABLED:
                runnable_reason = "No models yet — add a manual model to make this provider runnable"
            else:
                runnable_reason = "No catalog yet — refresh models to discover the live catalog"
            message = runnable_reason
        else:
            runnable = True
            runnable_reason = None
            message = None

    is_configured = bool(enabled and not quarantined)
    credential_ready = bool((auth_mode == AUTH_NONE) or (source is not None))

    return ProviderConnectionStatus(
        kind=kind,
        label=label,
        base_url=base_url_desc,
        connected=connected,
        credential_source=source,
        model_count=model_count,
        last_refresh_utc=last_refresh,
        last_refresh_source=refresh_source,
        stale=stale,
        status_message=message,
        cached_models=cached,
        api_format=api_format,
        enabled=enabled,
        is_builtin=is_builtin,
        auth_mode=auth_mode,
        catalog_mode=catalog_mode,
        transport_profile=transport_profile,
        runnable=runnable,
        runnable_reason=runnable_reason,
        is_configured=is_configured,
        credential_ready=credential_ready,
        live_verified=False,
        live_verified_at_utc=None,
        runtime_succeeded_at_utc=None,
    )


def connection_statuses() -> List[ProviderConnectionStatus]:
    """Return connection statuses for all configured providers."""
    configs = _config.load_provider_configurations()
    seen = set()
    statuses = []
    for c in configs:
        if c.provider_id not in seen:
            seen.add(c.provider_id)
            statuses.append(provider_connection_status(c.provider_id))
    return statuses
# -- explicit catalog refresh --------------------------------------------------

def refresh_provider_catalog(
    kind: str,
    *,
    timeout_seconds: float = _CATALOG_FETCH_TIMEOUT_SECONDS,
    engine: Optional[str] = None,
    credential: Optional[str] = None,
) -> ProviderCatalogSnapshot:
    """Fetch, normalize, cache, and return one live catalog snapshot.

    Catalog credentials/headers use the same explicit provider auth
    contract as inference.  Manual-model-only providers fail before any
    network attempt with an actionable message (manual execution remains
    valid).  Catalog failure preserves the last known-good catalog and
    reports the exact safe failure.
    """
    if _config.is_provider_quarantined(kind):
        raise ProviderConnectionError(
            "Credential state requires recovery. Edit provider and save an API key again."
        )
    cfg = _config.get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    if cfg.catalog_mode == CATALOG_DISABLED:
        raise ProviderConnectionError(
            f"{cfg.name}: catalog discovery is disabled for this provider — "
            "add models manually"
        )

    contract = _identity._contract_for_config(cfg)
    if cfg.auth_mode == AUTH_NONE:
        resolved = None
        if credential is not None:
            raise ProviderConnectionError(
                f"{cfg.name}: no-auth providers must not send a credential"
            )
    else:
        resolved = credential if credential is not None else _credentials.resolve_runtime_credential(kind)
        provider_label = cfg.name
        if not resolved:
            env_hint = (
                f"set {contract.env_var} or "
                if (contract and contract.env_var)
                else ""
            )
            raise ProviderConnectionError(
                f"{provider_label}: no usable credential source — "
                f"{env_hint}edit provider to add an API key"
            )
    provider_label = cfg.name

    base = cfg.base_url.rstrip("/")
    if contract is not None:
        catalog_path = contract.catalog_path
        tls_blocked = contract.tls_signature_blocked
    else:
        catalog_path = "/models"
        tls_blocked = cfg.tls_signature_blocked

    if base.endswith(catalog_path):
        url = base
    else:
        url = base + catalog_path

    try:
        payload = request_json(
            "GET",
            url,
            credential=resolved,
            timeout_seconds=timeout_seconds,
            max_response_bytes=_CATALOG_MAX_RESPONSE_BYTES,
            engine=engine,
            tls_signature_blocked=tls_blocked,
            auth_mode=cfg.auth_mode,
        )
    except ProviderHttpError as exc:
        sanitized = sanitize_text(str(exc), active_credential=resolved)
        raise ProviderConnectionError(
            f"{provider_label} catalog refresh failed: {sanitized}"
        ) from None

    models, truncated = _normalize_catalog(kind, payload)
    if not models:
        raise ProviderConnectionError(
            f"{provider_label} catalog refresh returned no usable models"
        )

    now_str = _utc_now()
    snapshot = ProviderCatalogSnapshot(
        kind=kind,
        fetched_at_utc=now_str,
        source=_SNAPSHOT_SOURCE_LIVE,
        models=models,
        truncated=truncated,
    )
    save_cached_catalog(snapshot)

    # Also persist to ProviderConfig if known
    if cfg is not None:
        # Decomposition v1 (lazy edge E3): persisting discovered models is
        # registry orchestration in provider_management, above this observation
        # module in the static DAG (was a plain module-global lookup).
        from agentic_debugger.application import provider_management as _management
        _management.update_provider_config(kind, models=models)

    return snapshot


def test_provider_connection(
    kind: str,
    *,
    model_id: Optional[str] = None,
    timeout_seconds: float = _CATALOG_FETCH_TIMEOUT_SECONDS,
    engine: Optional[str] = None,
    credential: Optional[str] = None,
) -> Dict[str, Any]:
    """Bounded connection/model probe exercising the exact transport contract.

    Credential-safe and side-effect-free (never persists catalog state,
    never modifies treatment qualification): for catalog-enabled providers
    it performs one authenticated ``GET`` catalog probe with the same
    auth/credential contract as inference; for manual-model-only
    providers it validates runnable state without network I/O.  Returns a
    plain mapping with ``ok``, ``reason``, ``endpoint`` (credential-free),
    ``auth_mode``, ``protocol``, and ``model_count``.  Raises
    :class:`ProviderConnectionError` for configuration failures.

    V2-04: ``credential`` is the EXPLICIT vault-issued credential for the
    normal ModelGateway product path — when supplied, this function never
    rediscovers a credential.  The internal resolution fallback remains
    ONLY as the documented compatibility backend for legacy low-level
    callers (no product/UI/session caller).
    """
    cfg = _config.get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    if _config.is_provider_quarantined(kind):
        raise ProviderConnectionError(
            "Credential state requires recovery. Edit provider and save an API key again."
        )
    try:
        _identity.validate_auth_protocol_combination(cfg.auth_mode, cfg.api_format)
    except ProviderConnectionError as exc:
        raise ProviderConnectionError(str(exc)) from None
    endpoint = describe_url(cfg.base_url)
    if cfg.auth_mode == AUTH_NONE:
        from urllib.parse import urlsplit as _split

        host = (_split(cfg.base_url).hostname or "").lower()
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ProviderConnectionError(
                "no-auth providers are accepted only for loopback/self-hosted endpoints"
            )
    if model_id is not None:
        if type(model_id) is not str or not model_id.strip():
            raise ProviderConnectionError("provider model id is missing")
        # The SAME effective contract as the real session resolver: the
        # model's effective protocol must satisfy the provider auth matrix
        # and the explicit transport-profile capability.  An impossible
        # effective route reports ok=False here and never reaches execution.
        try:
            protocol = _protocols.effective_model_protocol(kind, model_id.strip())
        except ProviderConnectionError as exc:
            return {
                "ok": False,
                "reason": str(exc),
                "endpoint": endpoint,
                "auth_mode": cfg.auth_mode,
                "protocol": cfg.api_format,
                "model_count": len(cfg.models),
            }
    else:
        protocol = cfg.api_format
        if not _protocols.is_protocol_executable(kind, protocol):
            return {
                "ok": False,
                "reason": (
                    f"provider {cfg.name!r} has no executable transport for "
                    f"protocol {protocol!r} under authentication "
                    f"{cfg.auth_mode!r}"
                ),
                "endpoint": endpoint,
                "auth_mode": cfg.auth_mode,
                "protocol": protocol,
                "model_count": len(cfg.models),
            }
    if cfg.auth_mode == AUTH_NONE:
        effective_credential = None
    else:
        effective_credential = (
            credential if credential is not None else _credentials.resolve_runtime_credential(kind)
        )
    if cfg.auth_mode != AUTH_NONE and effective_credential is None:
        return {
            "ok": False,
            "reason": "missing credential — edit provider to add an API key",
            "endpoint": endpoint,
            "auth_mode": cfg.auth_mode,
            "protocol": protocol,
            "model_count": len(cfg.models),
        }
    if cfg.catalog_mode == CATALOG_DISABLED:
        models = [
            m
            for m in cfg.models
            if m.protocol is not None and _protocols.is_protocol_executable(kind, m.protocol)
        ]
        if model_id is not None and all(m.model_id != model_id.strip() for m in models):
            return {
                "ok": False,
                "reason": f"model {model_id.strip()!r} is not configured for this provider",
                "endpoint": endpoint,
                "auth_mode": cfg.auth_mode,
                "protocol": protocol,
                "model_count": len(models),
            }
        if not models:
            return {
                "ok": False,
                "reason": "no models yet — add a manual model to make this provider runnable",
                "endpoint": endpoint,
                "auth_mode": cfg.auth_mode,
                "protocol": protocol,
                "model_count": 0,
            }
        return {
            "ok": True,
            "reason": "manual-model runnable (catalog discovery disabled)",
            "endpoint": endpoint,
            "auth_mode": cfg.auth_mode,
            "protocol": protocol,
            "model_count": len(models),
        }
    # Catalog-enabled: one live probe, no persistence (last known-good
    # catalog is preserved on failure by construction).
    contract = _identity._contract_for_config(cfg)
    resolved = effective_credential if cfg.auth_mode != AUTH_NONE else None
    base = cfg.base_url.rstrip("/")
    catalog_path = contract.catalog_path if contract is not None else "/models"
    tls_blocked = (
        contract.tls_signature_blocked if contract is not None else cfg.tls_signature_blocked
    )
    url = base if base.endswith(catalog_path) else base + catalog_path
    try:
        payload = request_json(
            "GET",
            url,
            credential=resolved,
            timeout_seconds=timeout_seconds,
            max_response_bytes=_CATALOG_MAX_RESPONSE_BYTES,
            engine=engine,
            tls_signature_blocked=tls_blocked,
            auth_mode=cfg.auth_mode,
        )
    except ProviderHttpError as exc:
        sanitized = sanitize_text(str(exc), active_credential=resolved)
        return {
            "ok": False,
            "reason": f"catalog probe failed: {sanitized}",
            "endpoint": endpoint,
            "auth_mode": cfg.auth_mode,
            "protocol": protocol,
            "model_count": len(cfg.models),
        }
    try:
        models, _ = _normalize_catalog(kind, payload)
    except ProviderConnectionError as exc:
        return {
            "ok": False,
            "reason": f"catalog probe returned no usable models: {exc}",
            "endpoint": endpoint,
            "auth_mode": cfg.auth_mode,
            "protocol": protocol,
            "model_count": len(cfg.models),
        }
    return {
        "ok": True,
        "reason": f"catalog reachable ({len(models)} models)",
        "endpoint": endpoint,
        "auth_mode": cfg.auth_mode,
        "protocol": protocol,
        "model_count": len(models),
    }
