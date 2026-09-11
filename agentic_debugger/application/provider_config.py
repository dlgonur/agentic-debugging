"""Durable provider registry: models, validation, paths, persistence, quarantine.

Single responsibility: the durable, NON-SECRET provider truth -- the
:class:`ProviderConfig` / :class:`DiscoveredProviderModel` data model and its
strict fail-closed validation, the configuration/quarantine filesystem paths
(config-root isolation preserved), atomic load/save/query, the durable
credential-quarantine recovery markers, and the auth/catalog/transport
accessors over live configuration.

Two documented lazy edges keep the static DAG acyclic: model creation resolves
its default protocol through :mod:`provider_protocols` inside the method, and
legacy-builtin migration delegates to :mod:`provider_management` inside the
loader (both were plain module-global lookups before the split).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agentic_debugger.application import provider_identity as _identity
from agentic_debugger.application.provider_http import validate_and_canonicalize_url
from agentic_debugger.application.provider_identity import (
    AUTH_BEARER,
    AUTH_MODES,
    AUTH_NONE,
    CATALOG_MODES,
    CATALOG_OPENAI,
    HISTORICAL_TRANSPORT_PROFILES,
    TRANSPORT_GENERIC,
    TRANSPORT_MODES,
    ProviderConnectionError,
    _PROTOCOL_FAMILIES,
)
# -- provider data model & persistence ---------------------------------------
#
# One authoritative definition of the durable configuration contract:
# schema identity, the configured-provider bound, and the file-size bound.
# Every reader/writer in this module uses these values.

#: Current durable schema.  V1 files (bearer-only, catalog always enabled,
#: no auth/catalog keys) remain loadable and migrate deterministically to
#: V2 defaults (auth=bearer, catalog=openai); V2 is always written.
PROVIDER_CONFIG_SCHEMA_VERSION = "provider-configurations-v2"
PROVIDER_CONFIG_SCHEMA_VERSIONS = frozenset(
    {"provider-configurations-v1", "provider-configurations-v2"}
)
_PROVIDER_CONFIG_SCHEMA_V1 = "provider-configurations-v1"
_MAX_PROVIDERS_CONFIGURED = 64
_MAX_CONFIG_FILE_BYTES = 256 * 1024
_MAX_PROVIDER_NAME_CHARS = 128
_MAX_DISPLAY_NAME_CHARS = 256
_MAX_MODEL_ID_CHARS = 128
_MAX_MODELS_PER_PROVIDER = 256
_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}$")

#: Bounded safe technical-ID grammar for explicit and generated provider
#: ids: lowercase ASCII alphanumeric/underscore, 1-32 chars, starting with
#: an alphanumeric.  Generated ids (:func:`_clean_slug`) and the historical
#: ids (``ollama_cloud`` et al.) both conform; anything else fails closed.
_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
_MAX_PROVIDER_ID_CHARS = 32


def is_valid_provider_id(provider_id: Any) -> bool:
    """Whether ``provider_id`` matches the bounded safe technical-ID grammar."""
    return (
        type(provider_id) is str
        and len(provider_id) <= _MAX_PROVIDER_ID_CHARS
        and _PROVIDER_ID_PATTERN.fullmatch(provider_id) is not None
    )


@dataclass(frozen=True)
class DiscoveredProviderModel:
    """One model identity in a provider's catalog."""

    kind: str
    model_id: str
    display_name: str
    protocol: Optional[str]
    runnable: bool
    unavailable_reason: Optional[str] = None

    @property
    def provider_id(self) -> str:
        return self.kind

    @classmethod
    def create(
        cls, kind: str, model_id: str, display_name: str, protocol: Optional[str] = None
    ) -> "DiscoveredProviderModel":
        # Decomposition v1 (lazy edge E1): default protocol resolution
        # lives in provider_protocols, above this module in the static
        # DAG (was a plain module-global lookup).
        from agentic_debugger.application import provider_protocols as _protocols
        proto = protocol if protocol is not None else _protocols.resolve_model_protocol(kind, model_id)
        if proto is not None:
            return cls(
                kind=kind,
                model_id=model_id,
                display_name=display_name,
                protocol=proto,
                runnable=True,
                unavailable_reason=None,
            )
        return cls(
            kind=kind,
            model_id=model_id,
            display_name=display_name,
            protocol=None,
            runnable=False,
            unavailable_reason="Protocol not yet resolved for direct API",
        )


@dataclass(frozen=True)
class ProviderConfig:
    """Persisted, provider-neutral configuration (NON-SECRET metadata)."""

    provider_id: str
    name: str
    base_url: str
    api_format: str
    models: Tuple[DiscoveredProviderModel, ...] = ()
    last_refresh_utc: Optional[str] = None
    last_refresh_source: Optional[str] = None
    enabled: bool = True
    is_builtin: bool = False
    builtin_kind: Optional[str] = None
    tls_signature_blocked: bool = False
    auth_mode: str = AUTH_BEARER
    catalog_mode: str = CATALOG_OPENAI
    transport_profile: str = TRANSPORT_GENERIC

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "base_url": self.base_url,
            "api_format": self.api_format,
            "auth_mode": self.auth_mode,
            "catalog_mode": self.catalog_mode,
            "transport_profile": self.transport_profile,
            "models": [
                {
                    "model_id": m.model_id,
                    "display_name": m.display_name,
                    "protocol": m.protocol,
                }
                for m in self.models
            ],
            "last_refresh_utc": self.last_refresh_utc,
            "last_refresh_source": self.last_refresh_source,
            "enabled": self.enabled,
            "is_builtin": self.is_builtin,
            "builtin_kind": self.builtin_kind,
            "tls_signature_blocked": self.tls_signature_blocked,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Optional["ProviderConfig"]:
        """Strictly decode one persisted entry, or None when malformed.

        Single coherent fail-closed authority for the durable provider
        contract (used by both read and write boundaries):

        - exact types are validated, never truthiness or ``bool(...)``
          coercion (so ``"enabled": "false"`` cannot become ``True``);
        - ``models`` must be a list; malformed entries fail instead of
          disappearing; model IDs must satisfy the bounded grammar with
          no control characters; protocols must be known or explicitly
          ``None`` and are never inferred;
        - ``is_builtin``/``builtin_kind`` compatibility is enforced so a
          string ``"false"`` cannot enter migration behavior;
        - auth/transport metadata introduced by the provider-platform
          goal (``auth_mode``, ``catalog_mode``) is validated, with
          deterministic V1 defaults (bearer/openai) when absent;
        - ``transport_profile`` selects historical transport semantics
          explicitly (``generic`` default).  Records predating the profile
          field keep generic semantics automatically — EXCEPT records
          whose technical ID equals a historical identifier
          (``ollama_cloud`` / ``opencode_go`` / ``commandcode_goat``):
          those are ambiguous (historical behavior can no longer be
          inferred from the ID) and fail so the loader can report
          actionable migration guidance instead of guessing;
        - every model with an explicit protocol is validated against the
          provider authentication mode, so a model-specific protocol can
          never bypass the auth matrix at the durable boundary;
        - duplicate model identities, oversized collections, unbounded
          names, bad timestamps, and invalid TLS flags all fail closed.
        """
        if not isinstance(data, Mapping):
            return None
        pid = data.get("provider_id")
        name = data.get("name")
        base_url = data.get("base_url")
        api_format = data.get("api_format")
        if type(pid) is not str or not is_valid_provider_id(pid):
            return None
        if "transport_profile" in data:
            profile_raw = data.get("transport_profile")
            if type(profile_raw) is not str or profile_raw not in TRANSPORT_MODES:
                return None
            transport_profile = profile_raw
        elif pid in HISTORICAL_TRANSPORT_PROFILES and data.get("is_builtin") is True:
            # Legacy auto-seeded records predate profiles but have a
            # deterministic disposition (durable purge via migration), so
            # they keep their historical profile for the migration pass
            # and never reach runtime semantics.
            transport_profile = pid
        elif pid in HISTORICAL_TRANSPORT_PROFILES:
            # Ambiguous durable state: this record predates explicit
            # transport profiles yet carries a historical identifier, so
            # historical behavior can no longer be inferred safely.
            # Fail here; the loader reports migration guidance.
            return None
        else:
            transport_profile = TRANSPORT_GENERIC
        if type(name) is not str or not name.strip():
            return None
        if len(name.strip()) > _MAX_PROVIDER_NAME_CHARS:
            return None
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            return None
        if type(base_url) is not str or not base_url.strip():
            return None
        try:
            clean_url = validate_and_canonicalize_url(base_url)
        except Exception:
            return None
        if type(api_format) is not str or api_format not in _PROTOCOL_FAMILIES:
            return None
        auth_raw = data.get("auth_mode", AUTH_BEARER)
        if type(auth_raw) is not str or auth_raw not in AUTH_MODES:
            return None
        catalog_raw = data.get("catalog_mode", CATALOG_OPENAI)
        if type(catalog_raw) is not str or catalog_raw not in CATALOG_MODES:
            return None
        try:
            _identity.validate_auth_protocol_combination(auth_raw, api_format)
        except ProviderConnectionError:
            return None
        if auth_raw == AUTH_NONE:
            # No-auth is a loopback-only contract; a persisted non-loopback
            # no-auth endpoint is contradictory durable state.
            try:
                from urllib.parse import urlsplit as _split

                host = (_split(clean_url).hostname or "").lower()
                if host not in ("127.0.0.1", "::1", "localhost"):
                    return None
            except Exception:
                return None
        if "models" in data:
            models_raw = data.get("models")
            if type(models_raw) is not list:
                return None
        else:
            models_raw = []
        if len(models_raw) > _MAX_MODELS_PER_PROVIDER:
            return None
        models: List[DiscoveredProviderModel] = []
        seen_model_ids: set[str] = set()
        for m in models_raw:
            if not isinstance(m, Mapping):
                return None
            mid = m.get("model_id")
            if type(mid) is not str or not mid.strip():
                return None
            mid_stripped = mid.strip()
            if len(mid_stripped) > _MAX_MODEL_ID_CHARS:
                return None
            if _MODEL_ID_PATTERN.fullmatch(mid_stripped) is None:
                return None
            if any(ord(c) < 32 or ord(c) == 127 for c in mid_stripped):
                return None
            if mid_stripped in seen_model_ids:
                return None
            seen_model_ids.add(mid_stripped)
            disp_raw = m.get("display_name", mid_stripped)
            if disp_raw is None:
                disp_raw = mid_stripped
            if type(disp_raw) is not str or not disp_raw.strip():
                return None
            disp = disp_raw.strip()
            if len(disp) > _MAX_DISPLAY_NAME_CHARS:
                return None
            if any(ord(c) < 32 or ord(c) == 127 for c in disp):
                return None
            if "protocol" in m:
                proto = m.get("protocol")
                if proto is not None and (
                    type(proto) is not str or proto not in _PROTOCOL_FAMILIES
                ):
                    return None
                if proto is not None:
                    # The effective model protocol must satisfy the
                    # provider authentication matrix: a model-specific
                    # protocol can never bypass it at the durable boundary.
                    try:
                        _identity.validate_auth_protocol_combination(auth_raw, proto)
                    except ProviderConnectionError:
                        return None
            else:
                proto = None
            models.append(
                DiscoveredProviderModel(
                    kind=pid,
                    model_id=mid_stripped,
                    display_name=disp,
                    protocol=proto,
                    runnable=proto is not None,
                    unavailable_reason=None if proto else "Protocol unresolved",
                )
            )
        if "enabled" in data:
            enabled_raw = data.get("enabled")
            if type(enabled_raw) is not bool:
                return None
            enabled = enabled_raw
        else:
            enabled = True
        if "is_builtin" in data:
            builtin_raw = data.get("is_builtin")
            if type(builtin_raw) is not bool:
                return None
            is_builtin = builtin_raw
        else:
            is_builtin = False
        builtin_kind = data.get("builtin_kind")
        if builtin_kind is not None and type(builtin_kind) is not str:
            return None
        if not is_builtin and builtin_kind is not None:
            return None
        if "tls_signature_blocked" in data:
            tls_raw = data.get("tls_signature_blocked")
            if type(tls_raw) is not bool:
                return None
            tls_blocked = tls_raw
        else:
            tls_blocked = False
        refresh_utc = data.get("last_refresh_utc")
        if refresh_utc is not None:
            if type(refresh_utc) is not str or not _valid_utc(refresh_utc):
                return None
        refresh_source = data.get("last_refresh_source")
        if refresh_source is not None:
            if type(refresh_source) is not str or not refresh_source.strip():
                return None
            if len(refresh_source) > 32:
                return None
            if any(ord(c) < 32 or ord(c) == 127 for c in refresh_source):
                return None
        # Reject unknown persisted keys that could hide a forked schema.
        # Known keys are exactly the to_dict() contract (V1 files simply
        # omit auth_mode/catalog_mode/transport_profile and receive
        # deterministic defaults, except ambiguous historical IDs which
        # fail closed with migration guidance).
        allowed = {
            "provider_id",
            "name",
            "base_url",
            "api_format",
            "auth_mode",
            "catalog_mode",
            "transport_profile",
            "models",
            "last_refresh_utc",
            "last_refresh_source",
            "enabled",
            "is_builtin",
            "builtin_kind",
            "tls_signature_blocked",
        }
        for key in data:
            if key not in allowed:
                return None
        return cls(
            provider_id=pid,
            name=name.strip(),
            base_url=clean_url,
            api_format=api_format,
            models=tuple(models),
            last_refresh_utc=refresh_utc,
            last_refresh_source=refresh_source,
            enabled=enabled,
            is_builtin=is_builtin,
            builtin_kind=builtin_kind,
            tls_signature_blocked=tls_blocked,
            auth_mode=auth_raw,
            catalog_mode=catalog_raw,
            transport_profile=transport_profile,
        )


def _valid_utc(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    except ValueError:
        return False


def validate_provider_config_for_write(cfg: ProviderConfig) -> None:
    """Enforce the canonical durable invariant before any write.

    The same strict authority protects both read and write so a
    successful save always round-trips through the loader to equivalent
    state.  Raises :class:`ProviderConnectionError` on any violation.
    """
    if type(cfg) is not ProviderConfig:
        raise ProviderConnectionError("provider configuration entry is invalid")
    # Re-validate through the canonical decoder: to_dict() output must
    # decode to an equivalent config.  This guarantees read/write
    # coherence by construction rather than by parallel rule sets.
    try:
        payload = cfg.to_dict()
    except Exception:
        raise ProviderConnectionError("provider configuration entry is invalid") from None
    decoded = ProviderConfig.from_dict(payload)
    if decoded is None:
        raise ProviderConnectionError("provider configuration entry is invalid")
    if (
        decoded.provider_id != cfg.provider_id
        or decoded.name != cfg.name.strip()
        or decoded.base_url != cfg.base_url
        or decoded.api_format != cfg.api_format
        or decoded.auth_mode != cfg.auth_mode
        or decoded.catalog_mode != cfg.catalog_mode
        or decoded.transport_profile != cfg.transport_profile
        or decoded.enabled is not cfg.enabled
        or decoded.is_builtin is not cfg.is_builtin
        or decoded.builtin_kind != cfg.builtin_kind
        or decoded.tls_signature_blocked is not cfg.tls_signature_blocked
        or len(decoded.models) != len(cfg.models)
    ):
        raise ProviderConnectionError("provider configuration entry is invalid")
    for want, got in zip(cfg.models, decoded.models):
        if (
            want.model_id.strip() != got.model_id
            or want.display_name.strip() != got.display_name
            or want.protocol != got.protocol
        ):
            raise ProviderConnectionError("provider configuration entry is invalid")


def describe_transport_gap(data: Any) -> Optional[str]:
    """Actionable migration guidance for an undecodable provider entry.

    Returns a credential-safe message when a mapping entry fails ONLY
    because it predates explicit transport profiles while carrying a
    historical identifier — i.e. the one ambiguous case where the loader
    must not guess.  Returns ``None`` for any other malformed entry.
    """
    if not isinstance(data, Mapping):
        return None
    pid = data.get("provider_id")
    if type(pid) is not str or pid not in HISTORICAL_TRANSPORT_PROFILES:
        return None
    if "transport_profile" in data:
        return None
    if data.get("is_builtin") is True:
        # Legacy auto-seeded records have a deterministic purge
        # disposition; they are never ambiguous.
        return None
    probe = dict(data)
    probe["transport_profile"] = TRANSPORT_GENERIC
    if ProviderConfig.from_dict(probe) is None:
        return None
    return (
        f"provider {pid!r} was stored without an explicit transport profile, "
        "so its historical behavior can no longer be inferred from its "
        "technical ID; edit provider-configurations.json and add "
        f'"transport_profile": "{pid}" to keep the historical '
        f"{pid} transport, or "
        '"transport_profile": "generic" for generic OpenAI-compatible '
        "behavior (or delete the entry and re-add the provider explicitly)"
    )


def provider_auth_mode(kind: str) -> str:
    """Explicit authentication mode for one configured provider."""
    cfg = get_provider_config(kind)
    if cfg is None:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    return cfg.auth_mode


def provider_catalog_mode(kind: str) -> str:
    """Explicit catalog mode for one configured provider."""
    cfg = get_provider_config(kind)
    if cfg is None:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    return cfg.catalog_mode


def provider_transport_profile(kind: str) -> str:
    """Explicit transport profile for one configured provider."""
    cfg = get_provider_config(kind)
    if cfg is None:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    return cfg.transport_profile


def provider_configurations_path() -> Path:
    """User-level persistent configuration path (NOT in Git repository)."""
    override = os.environ.get("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH")
    if override and override.strip():
        return Path(override.strip())
    config_dir = os.environ.get("AGENTIC_DEBUGGER_CONFIG_DIR")
    if config_dir and config_dir.strip():
        return Path(config_dir.strip()) / "provider-configurations.json"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if not base:
        try:
            base = str(Path.home())
        except Exception:
            base = tempfile.gettempdir()
    return Path(base) / "AgenticDebugger" / "provider-configurations.json"
# -- durable credential quarantine (pre-armed fail-closed) ---------
#
# The provider/credential transaction is restart-safe: a durable quarantine
# marker MUST be present BEFORE any credential mutation begins.  If the
# durable marker cannot be established, the transaction aborts with no
# credential or configuration mutation.  Once armed, the quarantine is
# cleared only after a fully coherent config+credential pair is committed.
# Any existing but unreadable/corrupt quarantine file fails closed: it is
# not treated as empty, instead credential resolution raises a bounded
# recovery-state error and no request is issued.

_QUARANTINE_SCHEMA_VERSION = "provider-credential-quarantine-v1"
_MAX_QUARANTINE_FILE_BYTES = 256 * 1024

_QUARANTINED_PROVIDERS: set[str] = set()


def provider_quarantine_path() -> Path:
    """Durable credential-quarantine state path (next to the provider config)."""
    override = os.environ.get("AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH")
    if override and override.strip():
        return Path(override.strip())
    config_path = provider_configurations_path()
    return config_path.with_name("provider-credential-quarantine.json")


def _write_quarantine_state(providers: set[str]) -> None:
    """Atomically write quarantine state or unlink when empty.

    Raises ProviderConnectionError (credential-free) on any I/O failure.
    """
    path = provider_quarantine_path()
    if not providers:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise ProviderConnectionError("provider credential quarantine state could not be cleared") from None
        return
    payload = json.dumps(
        {
            "schema_version": _QUARANTINE_SCHEMA_VERSION,
            "providers": sorted(providers),
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=1,
    ).encode("utf-8")
    if len(payload) > _MAX_QUARANTINE_FILE_BYTES:
        raise ProviderConnectionError("provider credential quarantine state exceeded its bound")
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
        raise ProviderConnectionError("provider credential quarantine state could not be written") from None


def quarantine_provider(provider_id: str) -> None:
    """Durably mark one provider as requiring credential recovery.

    The durable write is authoritative; the in-memory overlay is updated only
    after the durable write succeeds.  Any I/O failure raises a bounded
    credential-free ProviderConnectionError and leaves in-memory state
    unchanged (abort before mutation).

    An existing but unreadable/corrupt quarantine file is UNKNOWN durable
    state and must remain fail-closed: the error is propagated, no mutation
    occurs, and the file is left untouched.  Callers (notably
    commit_provider_and_credential) abort before any credential mutation.
    """
    if not provider_id or not isinstance(provider_id, str):
        return
    existing = _read_quarantine_file()
    existing.update(_QUARANTINED_PROVIDERS)
    existing.add(provider_id)
    _write_quarantine_state(existing)
    _QUARANTINED_PROVIDERS.update(existing)


def clear_provider_quarantine(provider_id: str) -> None:
    """Clear quarantine after a coherent credential/config pair was re-established.

    The in-memory entry is cleared only after the durable state is
    successfully updated.  If the durable clear fails, the provider remains
    blocked (fail closed) and a bounded error is raised.

    An existing but unreadable/corrupt quarantine file is UNKNOWN durable
    state and must remain fail-closed: the error is propagated and the file
    is left byte-for-byte untouched.  No reconstruction from in-memory state
    is attempted.
    """
    if not provider_id or not isinstance(provider_id, str):
        return
    existing = _read_quarantine_file()
    durable_contains = provider_id in existing
    memory_contains = provider_id in _QUARANTINED_PROVIDERS
    if not durable_contains and not memory_contains:
        return
    existing.discard(provider_id)
    _write_quarantine_state(existing)
    _QUARANTINED_PROVIDERS.discard(provider_id)


def _read_quarantine_file() -> set[str]:
    path = provider_quarantine_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return set()
    except OSError:
        raise ProviderConnectionError("provider credential quarantine state could not be read")
    if len(raw) > _MAX_QUARANTINE_FILE_BYTES:
        raise ProviderConnectionError("provider credential quarantine state exceeded its bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ProviderConnectionError("provider credential quarantine state is malformed")
    if not isinstance(value, Mapping) or value.get("schema_version") != _QUARANTINE_SCHEMA_VERSION:
        raise ProviderConnectionError("provider credential quarantine state is invalid")
    providers = value.get("providers")
    if not isinstance(providers, list):
        raise ProviderConnectionError("provider credential quarantine state is invalid")
    return {str(p) for p in providers if isinstance(p, str) and p}


def _load_quarantined_providers() -> set[str]:
    result = set(_QUARANTINED_PROVIDERS)
    result.update(_read_quarantine_file())
    return result


def is_provider_quarantined(provider_id: str) -> bool:
    """Whether one provider's credential association is in an indeterminate state."""
    return provider_id in _load_quarantined_providers()


def quarantined_providers() -> List[str]:
    """Sorted durable quarantine list (credential-free)."""
    return sorted(_load_quarantined_providers())
def load_provider_configurations() -> List[ProviderConfig]:
    """Load persistent non-secret provider configurations safely.

    Distinguishes strictly between a genuinely absent file (returns []) and an
    existing file that is unreadable, oversized, malformed, or has an invalid
    schema (fails closed by raising ProviderConnectionError).  The file is
    left byte-for-byte untouched on any failure.

    Accepts both ``provider-configurations-v1`` (deterministically migrated
    to V2 defaults: auth=bearer, catalog=openai) and V2.  Automatically
    migrates away any legacy auto-seeded built-in records
    (is_builtin=True) so the registry reflects only user-owned connections.
    """
    path = provider_configurations_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError:
        raise ProviderConnectionError("provider configuration could not be read") from None

    if len(raw) > _MAX_CONFIG_FILE_BYTES:
        raise ProviderConnectionError("provider configuration exceeded file bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ProviderConnectionError("provider configuration file is malformed") from None

    if (
        not isinstance(data, Mapping)
        or data.get("schema_version") not in PROVIDER_CONFIG_SCHEMA_VERSIONS
    ):
        raise ProviderConnectionError("provider configuration has an invalid schema version")
    providers_raw = data.get("providers")
    if type(providers_raw) is not list:
        raise ProviderConnectionError("provider configuration payload is invalid")
    if len(providers_raw) > _MAX_PROVIDERS_CONFIGURED:
        raise ProviderConnectionError("provider configuration exceeds the configured-provider bound")

    configs: List[ProviderConfig] = []
    seen_ids = set()
    has_legacy_builtins = False
    legacy_builtin_ids: List[str] = []

    for item in providers_raw:
        if not isinstance(item, Mapping):
            raise ProviderConnectionError("provider configuration entry is invalid")
        cfg = ProviderConfig.from_dict(item)
        if cfg is None:
            gap = describe_transport_gap(item)
            if gap is not None:
                raise ProviderConnectionError(gap)
            raise ProviderConnectionError("provider configuration entry is invalid")
        if cfg.provider_id in seen_ids:
            # A contradictory durable identity is never silently resolved
            # by order: loading fails closed and the file stays untouched.
            raise ProviderConnectionError(
                "provider configuration contains a duplicate provider identity"
            )
        seen_ids.add(cfg.provider_id)
        if cfg.is_builtin is True:
            has_legacy_builtins = True
            legacy_builtin_ids.append(cfg.provider_id)
        else:
            configs.append(cfg)

    if has_legacy_builtins:
        # Decomposition v1 (lazy edge E4): legacy migration is registry
        # orchestration in provider_management, above this store module in the
        # static DAG (was a plain module-global lookup).
        from agentic_debugger.application import provider_management as _management
        _management._migrate_legacy_builtin_records(configs, legacy_builtin_ids)

    return configs


def save_provider_configurations(configs: List[ProviderConfig]) -> None:
    """Atomically persist non-secret provider configurations.

    The same canonical validation contract protects both read and write:
    every entry is strictly validated before serialization, duplicate
    identities and over-bound registries fail before any write, and a
    successful save always round-trips through the loader to equivalent
    state.  A caller can never receive successful write completion for
    state the loader would subsequently reject.
    """
    if type(configs) is not list:
        raise ProviderConnectionError("provider configuration entry is invalid")
    if len(configs) > _MAX_PROVIDERS_CONFIGURED:
        raise ProviderConnectionError(
            "provider configuration exceeds the configured-provider bound "
            f"({_MAX_PROVIDERS_CONFIGURED}); nothing was written"
        )
    seen: set[str] = set()
    for cfg in configs:
        validate_provider_config_for_write(cfg)
        if cfg.provider_id in seen:
            raise ProviderConnectionError(
                "provider configuration contains a duplicate provider identity"
            )
        seen.add(cfg.provider_id)
    path = provider_configurations_path()
    payload = json.dumps(
        {
            "schema_version": PROVIDER_CONFIG_SCHEMA_VERSION,
            "providers": [c.to_dict() for c in configs],
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=1,
    ).encode("utf-8")
    if len(payload) > _MAX_CONFIG_FILE_BYTES:
        raise ProviderConnectionError("provider configuration exceeded file bound")
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
        raise ProviderConnectionError("provider configuration could not be written") from None


def list_configured_providers() -> List[ProviderConfig]:
    """Return all configured providers."""
    return load_provider_configurations()


def get_provider_config(provider_id: str) -> Optional[ProviderConfig]:
    """Get a single provider configuration by technical ID."""
    for cfg in load_provider_configurations():
        if cfg.provider_id == provider_id:
            return cfg
    return None


def is_known_provider(provider_id: str) -> bool:
    """Check if a provider ID corresponds to an active configured provider."""
    try:
        cfg = get_provider_config(provider_id)
        return cfg is not None and cfg.enabled
    except ProviderConnectionError:
        return False
