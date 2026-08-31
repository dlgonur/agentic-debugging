"""Application-owned provider connections and live catalog discovery.

One coherent, UI-free domain for the two built-in direct-API subscription
providers (``opencode_go`` and ``commandcode_goat``):

- which built-in direct-API providers exist and their safe endpoint
  identity;
- whether a credential source is currently usable (presence-only; the
  credential bytes are never read for a status answer);
- the last discovered :class:`ProviderCatalogSnapshot` (bounded,
  deterministic, safely cacheable — model identities and protocol
  metadata are not secrets);
- each model's protocol family when deterministically known, and a
  bounded reason when it is not.

Concepts that stay separate by contract:

- **Discovery** (this module) is a catalog GET.  It performs no
  generation inference and confers no scientific status.
- **General-runtime execution** resolves through
  :mod:`agentic_debugger.application.model_providers`, which selects the
  explicit route (``direct_api`` / ``legacy_cli``) per model.
- **Capability Ladder qualification** is untouched by this module: a
  discovered model is never treatment-eligible and never enters the
  scientific roster because it exists or can execute.

Credential security rules:

- Raw keys live only in one process-local session store
  (:func:`set_session_key`) or in their existing operator-owned sources
  (provider environment variables, the OpenCode CLI auth store read in
  place).  Nothing here writes a credential to disk, a catalog, a
  journal, a log, or an exception.
- The CommandCode CLI auth store is NOT parsed: its schema cannot be
  reliably established from this machine, so the direct route fails
  closed to the environment / session-key sources instead of
  speculatively parsing an unknown external format.
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

from agentic_debugger.application.provider_http import (
    ProviderHttpError,
    describe_url,
    request_json,
)

__all__ = [
    "DIRECT_API_PROVIDER_KINDS",
    "DiscoveredProviderModel",
    "PROTOCOL_CHAT_COMPLETIONS",
    "PROTOCOL_MESSAGES",
    "PROTOCOL_RESPONSES",
    "ProviderCatalogSnapshot",
    "ProviderConnectionError",
    "ProviderConnectionStatus",
    "clear_all_session_keys",
    "clear_session_key",
    "credential_source_for",
    "catalog_cache_path",
    "has_session_key",
    "load_cached_catalog",
    "peek_session_key",
    "provider_connection_status",
    "provider_api_model_id",
    "provider_environment_variable",
    "provider_session_credential_environment",
    "provider_transport_credential_environment",
    "resolve_model_protocol",
    "resolve_runtime_credential",
    "refresh_provider_catalog",
    "set_session_key",
]

# -- protocol families -------------------------------------------------------

PROTOCOL_CHAT_COMPLETIONS = "chat_completions"
PROTOCOL_RESPONSES = "responses"
PROTOCOL_MESSAGES = "messages"
_PROTOCOL_FAMILIES = (
    PROTOCOL_CHAT_COMPLETIONS,
    PROTOCOL_RESPONSES,
    PROTOCOL_MESSAGES,
)

#: Providers this module connects through their documented direct APIs.
DIRECT_API_PROVIDER_KINDS = ("opencode_go", "commandcode_goat")

_PROVIDER_LABELS = {
    "opencode_go": "OpenCode Go",
    "commandcode_goat": "CommandCode GOAT",
}

# -- provider endpoint contracts (verified against official provider
#    documentation and live catalogs, 2026-08-30) -----------------------------

@dataclass(frozen=True)
class _ProviderContract:
    """Provider-owned endpoint facts for one built-in direct API."""

    kind: str
    base_url: str
    catalog_path: str
    inference_paths: Mapping[str, str]
    #: Both endpoints sit behind a bot-protection layer that rejects the
    #: stdlib TLS signature (HTTP 403 error code 1010, verified); the OS
    #: curl engine is the deterministic network client when present.
    tls_signature_blocked: bool
    catalog_model_id_pattern: str
    #: App-supported environment variable.  Command Code documents its
    #: variable; OpenCode Go currently documents /connect/auth-store setup,
    #: so OPENCODE_API_KEY is an optional Agentic Debugger source, not a
    #: claimed canonical OpenCode contract.
    env_var: Optional[str]
    #: Private process-environment hop for a memory-only UI session key.
    session_env_var: str
    #: Whether the provider CLI auth store can supply the direct-API
    #: credential (only when its schema is reliably established).
    auth_store_consumable: bool


_CONTRACTS: Mapping[str, _ProviderContract] = {
    "opencode_go": _ProviderContract(
        kind="opencode_go",
        base_url="https://opencode.ai/zen/go/v1",
        catalog_path="/models",
        inference_paths={
            PROTOCOL_CHAT_COMPLETIONS: "/chat/completions",
            PROTOCOL_RESPONSES: "/responses",
            PROTOCOL_MESSAGES: "/messages",
        },
        tls_signature_blocked=True,
        catalog_model_id_pattern=r"^[a-z0-9][a-z0-9._-]{0,79}$",
        env_var="OPENCODE_API_KEY",
        session_env_var="AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
        auth_store_consumable=True,
    ),
    "commandcode_goat": _ProviderContract(
        kind="commandcode_goat",
        base_url="https://api.commandcode.ai/provider/v1",
        catalog_path="/models",
        inference_paths={
            PROTOCOL_CHAT_COMPLETIONS: "/chat/completions",
            PROTOCOL_MESSAGES: "/messages",
        },
        tls_signature_blocked=True,
        catalog_model_id_pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$",
        env_var="COMMAND_CODE_API_KEY",
        session_env_var="AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
        auth_store_consumable=False,
    ),
}


class ProviderConnectionError(RuntimeError):
    """Fail-closed provider connection error (credential-safe text)."""


# -- process-local session credentials ---------------------------------------

#: Bounded process-local API keys keyed by provider kind.  Never
#: persisted, never logged, never repr'd; cleared when the process ends.
_SESSION_KEYS: Dict[str, str] = {}

_MAX_SESSION_KEY_CHARS = 4096


def _credential_is_usable(value: Any) -> bool:
    """Presence/shape gate shared by every process-local credential source."""

    return (
        type(value) is str
        and bool(value.strip())
        and len(value) <= _MAX_SESSION_KEY_CHARS
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def set_session_key(kind: str, value: str) -> None:
    """Store one process-local API key for the app session."""

    if kind not in DIRECT_API_PROVIDER_KINDS:
        raise ProviderConnectionError(f"unknown direct-API provider: {kind!r}")
    if not _credential_is_usable(value):
        raise ProviderConnectionError("API key is missing, invalid, or oversized")
    stripped = value.strip()
    _SESSION_KEYS[kind] = stripped


def has_session_key(kind: str) -> bool:
    return kind in _SESSION_KEYS


def peek_session_key(kind: str) -> Optional[str]:
    """The stored key (runtime boundary only; never for presentation)."""

    return _SESSION_KEYS.get(kind)


def clear_session_key(kind: str) -> None:
    _SESSION_KEYS.pop(kind, None)


def clear_all_session_keys() -> None:
    _SESSION_KEYS.clear()


# -- OpenCode CLI auth store (read in place; schema verified locally
#    against the operator store: {"opencode-go": {"type": ..., "key": ...}})

_MAX_AUTH_STORE_BYTES = 64 * 1024


def opencode_auth_store_path() -> Path:
    profile = os.environ.get("OPENCODE_CONFIG_DIR") or str(Path.home())
    return Path(profile) / ".local" / "share" / "opencode" / "auth.json"


def _read_opencode_auth_store_key(path: Path) -> Optional[str]:
    """The ``opencode-go`` key from the CLI auth store, or ``None``.

    Strict, bounded, fail-closed: any structural mismatch means "not
    consumable here", never an inferred credential.  The value is
    returned only inside the runtime boundary.
    """

    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > _MAX_AUTH_STORE_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    entry = value.get("opencode-go")
    if not isinstance(entry, Mapping):
        return None
    key = entry.get("key")
    if not _credential_is_usable(key):
        return None
    return key


# -- credential sources ------------------------------------------------------

CREDENTIAL_SOURCE_SESSION_KEY = "session_key"
CREDENTIAL_SOURCE_ENVIRONMENT = "environment"
CREDENTIAL_SOURCE_CLI_AUTH_STORE = "cli_auth_store"


def credential_source_for(kind: str) -> Optional[str]:
    """Which credential source the direct route can use right now.

    Presence-ordered resolution: process-local session key, then the
    forwarded session credential, the app-supported provider environment
    variable, then (OpenCode only)
    the CLI auth store read in place.  Returns the source label or
    ``None``; credential bytes are only read when the source is the
    auth store itself (the store is the value).
    """

    contract = _CONTRACTS.get(kind)
    if contract is None:
        return None
    if has_session_key(kind):
        return CREDENTIAL_SOURCE_SESSION_KEY
    if _credential_is_usable(os.environ.get(contract.session_env_var)):
        return CREDENTIAL_SOURCE_SESSION_KEY
    if contract.env_var and _credential_is_usable(os.environ.get(contract.env_var)):
        return CREDENTIAL_SOURCE_ENVIRONMENT
    if contract.auth_store_consumable and _read_opencode_auth_store_key(
        opencode_auth_store_path()
    ):
        return CREDENTIAL_SOURCE_CLI_AUTH_STORE
    return None


def resolve_runtime_credential(kind: str) -> Optional[str]:
    """The credential value for one direct-API request (runtime only).

    Resolution order matches :func:`credential_source_for`.  The value
    must never cross into presentation, provenance, argv, or evidence;
    callers pass it only into the HTTP boundary.
    """

    contract = _CONTRACTS.get(kind)
    if contract is None:
        return None
    session_value = peek_session_key(kind)
    if session_value:
        return session_value
    forwarded_value = os.environ.get(contract.session_env_var)
    if _credential_is_usable(forwarded_value):
        return forwarded_value.strip()
    if contract.env_var:
        env_value = os.environ.get(contract.env_var)
        if _credential_is_usable(env_value):
            return env_value
    if contract.auth_store_consumable:
        return _read_opencode_auth_store_key(opencode_auth_store_path())
    return None


def provider_environment_variable(kind: str) -> Optional[str]:
    """The app-supported provider credential variable, if any."""

    contract = _CONTRACTS.get(kind)
    return contract.env_var if contract is not None else None


def provider_session_credential_environment(
    kind: str,
) -> Optional[Mapping[str, str]]:
    """Exactly one private UI-to-worker credential hop, or ``None``."""

    contract = _CONTRACTS.get(kind)
    session_value = peek_session_key(kind)
    if contract is None or not _credential_is_usable(session_value):
        return None
    return {contract.session_env_var: session_value.strip()}


def provider_transport_credential_environment(
    kind: str,
) -> Optional[Mapping[str, str]]:
    """Exactly one credential variable for the direct adapter child.

    The worker may hold a UI session key only in its private forwarded
    variable.  Preserve that narrow name for the adapter child; otherwise
    forward the app-supported provider variable.  Auth-store credentials
    are read in place and need no child override.
    """

    contract = _CONTRACTS.get(kind)
    if contract is None:
        return None
    session_value = peek_session_key(kind)
    if _credential_is_usable(session_value):
        return {contract.session_env_var: session_value.strip()}
    forwarded_value = os.environ.get(contract.session_env_var)
    if _credential_is_usable(forwarded_value):
        return {contract.session_env_var: forwarded_value.strip()}
    if contract.env_var:
        env_value = os.environ.get(contract.env_var)
        if _credential_is_usable(env_value):
            return {contract.env_var: env_value.strip()}
    return None


# -- protocol resolution ------------------------------------------------------

#: OpenCode Go — provider-owned, explicit model→protocol mapping grounded
#: in the official Go endpoint table (https://opencode.ai/docs/go/,
#: "Endpoints", verified 2026-08-30).  The live ``/models`` payload
#: carries NO protocol metadata, and the general Zen catalog routes some
#: shared ids differently (e.g. MiniMax uses /chat/completions on
#: ``zen/v1`` but /messages on ``zen/go/v1``), so this mapping is
#: per-provider and intentionally NOT extended by family heuristics:
#: a model absent from the documented table stays unresolved.
_OPENCODE_GO_DOCUMENTED_PROTOCOLS: Mapping[str, str] = {
    # /responses (OpenAI Responses family)
    "grok-4.6": PROTOCOL_RESPONSES,
    "gpt-5.6-luna": PROTOCOL_RESPONSES,
    "muse-spark-1.2-contributor": PROTOCOL_RESPONSES,
    # /messages (Anthropic Messages family)
    "minimax-m3": PROTOCOL_MESSAGES,
    "minimax-m2.7": PROTOCOL_MESSAGES,
    "minimax-m2.5": PROTOCOL_MESSAGES,
    "qwen3.8-max": PROTOCOL_MESSAGES,
    "qwen3.8-flash": PROTOCOL_MESSAGES,
    "qwen3.7-max": PROTOCOL_MESSAGES,
    "qwen3.7-plus": PROTOCOL_MESSAGES,
    "qwen3.6-plus": PROTOCOL_MESSAGES,
    # /chat/completions (OpenAI-compatible family)
    "glm-5.3-flash": PROTOCOL_CHAT_COMPLETIONS,
    "glm-5.3": PROTOCOL_CHAT_COMPLETIONS,
    "glm-5.2": PROTOCOL_CHAT_COMPLETIONS,
    "glm-5.1": PROTOCOL_CHAT_COMPLETIONS,
    "kimi-k3": PROTOCOL_CHAT_COMPLETIONS,
    "kimi-k2.7-code": PROTOCOL_CHAT_COMPLETIONS,
    "kimi-k2.6": PROTOCOL_CHAT_COMPLETIONS,
    "longcat-2.0": PROTOCOL_CHAT_COMPLETIONS,
    "deepseek-v4-pro": PROTOCOL_CHAT_COMPLETIONS,
    "deepseek-v4-flash": PROTOCOL_CHAT_COMPLETIONS,
    "deepseek-v4-flash-vision-exp": PROTOCOL_CHAT_COMPLETIONS,
    "mimo-v2.5": PROTOCOL_CHAT_COMPLETIONS,
    "mimo-v2.5-pro": PROTOCOL_CHAT_COMPLETIONS,
    "hy4-preview": PROTOCOL_CHAT_COMPLETIONS,
    "hy3": PROTOCOL_CHAT_COMPLETIONS,
}

_OPENCODE_GO_MODEL_PREFIX = "opencode-go/"


def resolve_opencode_go_protocol(model_id: str) -> Optional[str]:
    """Explicit documented protocol for one OpenCode Go model id.

    Accepts the subscription-prefixed (``opencode-go/<id>``) or bare id
    form.  Unknown models resolve to ``None`` — discovered, never
    guessed — and the caller must keep them unavailable for direct
    execution.
    """

    if type(model_id) is not str or not model_id:
        return None
    text = model_id.strip()
    if text.startswith(_OPENCODE_GO_MODEL_PREFIX):
        text = text[len(_OPENCODE_GO_MODEL_PREFIX):]
    return _OPENCODE_GO_DOCUMENTED_PROTOCOLS.get(text)


def resolve_commandcode_protocol(model_id: str) -> Optional[str]:
    """Deterministic provider-documented endpoint routing rule.

    CommandCode documents exactly two families and validates the split
    server-side: Anthropic models call ``/messages``; OpenAI and
    open-source models call ``/chat/completions``.  The Anthropic
    family is identified by the documented model identity shape
    (``claude-*`` ids or an ``anthropic/`` owner prefix); every other
    catalog id routes to the OpenAI-compatible family.
    """

    if type(model_id) is not str or not model_id.strip():
        return None
    text = model_id.strip()
    lowered = text.lower()
    if lowered.startswith("anthropic/"):
        return PROTOCOL_MESSAGES
    base = lowered.rsplit("/", 1)[-1]
    if base.startswith("claude"):
        return PROTOCOL_MESSAGES
    return PROTOCOL_CHAT_COMPLETIONS


def resolve_model_protocol(kind: str, model_id: str) -> Optional[str]:
    """The deterministic protocol family for one provider model, or ``None``."""

    if kind == "opencode_go":
        return resolve_opencode_go_protocol(model_id)
    if kind == "commandcode_goat":
        return resolve_commandcode_protocol(model_id)
    raise ProviderConnectionError(f"unknown direct-API provider: {kind!r}")


def provider_api_model_id(kind: str, model_id: str) -> str:
    """Exact model identity sent to a provider's direct API.

    OpenCode's TUI/config namespace uses ``opencode-go/<id>`` while its Go
    HTTP endpoint documents the bare ``<id>`` value.  Live discovery already
    returns bare IDs; this narrow conversion keeps the curated offline fallback
    compatible without sending the TUI namespace to the provider endpoint.
    CommandCode catalog IDs are already direct-API identities.
    """

    if kind not in DIRECT_API_PROVIDER_KINDS:
        raise ProviderConnectionError(f"unknown direct-API provider: {kind!r}")
    if type(model_id) is not str or not model_id.strip():
        raise ProviderConnectionError("provider model id is missing")
    value = model_id.strip()
    if kind == "opencode_go" and value.startswith(_OPENCODE_GO_MODEL_PREFIX):
        value = value[len(_OPENCODE_GO_MODEL_PREFIX):]
    return value


def inference_path_for(kind: str, protocol: str) -> str:
    contract = _CONTRACTS.get(kind)
    if contract is None:
        raise ProviderConnectionError(f"unknown direct-API provider: {kind!r}")
    path = contract.inference_paths.get(protocol)
    if path is None:
        raise ProviderConnectionError(
            f"provider {contract.kind!r} does not expose the {protocol!r} protocol"
        )
    return path


def provider_base_url(kind: str) -> str:
    contract = _CONTRACTS.get(kind)
    if contract is None:
        raise ProviderConnectionError(f"unknown direct-API provider: {kind!r}")
    return contract.base_url


def provider_tls_signature_blocked(kind: str) -> bool:
    contract = _CONTRACTS.get(kind)
    if contract is None:
        raise ProviderConnectionError(f"unknown direct-API provider: {kind!r}")
    return contract.tls_signature_blocked


# -- discovered models and snapshots -----------------------------------------

_MAX_CATALOG_MODELS = 256
_MAX_MODEL_ID_CHARS = 128
_CATALOG_FETCH_TIMEOUT_SECONDS = 30.0
_CATALOG_MAX_RESPONSE_BYTES = 1024 * 1024

_SNAPSHOT_SOURCE_LIVE = "live"


@dataclass(frozen=True)
class DiscoveredProviderModel:
    """One model identity from a provider catalog.

    ``protocol`` is the deterministic protocol family, or ``None`` when
    the provider contract does not resolve it — such a model is
    discovered but NOT runnable through the direct API, and the reason
    is bounded and explicit rather than guessed.
    """

    kind: str
    model_id: str
    display_name: str
    protocol: Optional[str]
    runnable: bool
    unavailable_reason: Optional[str] = None

    @classmethod
    def create(
        cls, kind: str, model_id: str, display_name: str
    ) -> "DiscoveredProviderModel":
        protocol = resolve_model_protocol(kind, model_id)
        if protocol is not None:
            return cls(
                kind=kind,
                model_id=model_id,
                display_name=display_name,
                protocol=protocol,
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
        """Strict cache decode; ``None`` on any structural mismatch."""

        if not isinstance(value, Mapping):
            return None
        kind = value.get("kind")
        fetched = value.get("fetched_at_utc")
        source = value.get("source")
        models = value.get("models")
        truncated = value.get("truncated")
        if kind not in DIRECT_API_PROVIDER_KINDS:
            return None
        if type(fetched) is not str or not _valid_utc(fetched):
            return None
        if source != _SNAPSHOT_SOURCE_LIVE:
            return None
        if type(truncated) is not bool:
            return None
        if type(models) is not list or len(models) > _MAX_CATALOG_MODELS:
            return None
        pattern = _CONTRACTS[kind].catalog_model_id_pattern
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
            expected = resolve_model_protocol(kind, model_id)
            if protocol != expected:
                # A cached protocol must agree with the current resolver;
                # a stale cache entry is discarded rather than trusted.
                return None
            display = _display_name(kind, model_id)
            decoded.append(
                DiscoveredProviderModel(
                    kind=kind,
                    model_id=model_id,
                    display_name=display,
                    protocol=protocol,
                    runnable=protocol is not None,
                    unavailable_reason=(
                        None
                        if protocol is not None
                        else "Protocol not yet resolved for direct API"
                    ),
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


def _valid_utc(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    except ValueError:
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _display_name(kind: str, model_id: str) -> str:
    from agentic_debugger.application.model_providers import format_model_display_name

    return format_model_display_name(model_id)


def _normalize_catalog(
    kind: str, payload: Mapping[str, Any]
) -> Tuple[Tuple[DiscoveredProviderModel, ...], bool]:
    """Deterministic normalization: validate, dedupe, sort, bound.

    Duplicated, invalid, or oversized entries are ignored; ordering is
    deterministic; no generation inference and no protocol guessing
    happen here.
    """

    contract = _CONTRACTS[kind]
    pattern = re.compile(contract.catalog_model_id_pattern)
    data = payload.get("data")
    if data is None:
        data = payload.get("models")
    if type(data) is not list:
        raise ProviderConnectionError("catalog response has no model list")
    seen: Dict[str, None] = {}
    for entry in data:
        if not isinstance(entry, Mapping):
            continue
        model_id = entry.get("id")
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
    """App-owned cache location (outside the repository and session evidence).

    Model identities and protocol metadata are not secrets; the cache
    never stores credentials, auth headers, or prompts.
    """

    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "AgenticDebugger" / "provider-catalog-cache.json"


def load_cached_catalog(kind: str) -> Optional[ProviderCatalogSnapshot]:
    """The most recent valid cached snapshot, or ``None``.

    Malformed, oversized, or stale-schema caches fail closed to
    ``None`` (treated as absent); they are never partially trusted.
    """

    if kind not in DIRECT_API_PROVIDER_KINDS:
        raise ProviderConnectionError(f"unknown direct-API provider: {kind!r}")
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
    """Atomically persist one snapshot (bounded; never credentials)."""

    if snapshot.kind not in DIRECT_API_PROVIDER_KINDS:
        raise ProviderConnectionError(
            f"unknown direct-API provider: {snapshot.kind!r}"
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
                # Preserve only snapshots that pass the current strict
                # schema/resolver contract.  Unknown keys and malformed
                # entries are never carried forward into the app-owned cache.
                for kind in DIRECT_API_PROVIDER_KINDS:
                    preserved = ProviderCatalogSnapshot.from_mapping(
                        decoded["providers"].get(kind)
                    )
                    if preserved is not None:
                        existing[kind] = preserved.to_mapping()
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
        raise ProviderConnectionError(
            "provider catalog cache could not be written"
        ) from None


# -- connection status --------------------------------------------------------

#: A cached catalog older than this is presented as stale/unverified.
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
    """Presence-only connection view; never contacts the provider."""

    contract = _CONTRACTS.get(kind)
    if contract is None:
        raise ProviderConnectionError(f"unknown direct-API provider: {kind!r}")
    source = credential_source_for(kind)
    model_count, last_refresh, refresh_source, stale, cached = _cached_status_fields(kind)
    message: Optional[str] = None
    if source is None:
        if kind == "commandcode_goat":
            message = (
                f"Not connected — direct API needs {contract.env_var} or an API key "
                "entered for this app session (the CLI auth store is not "
                "readable by the direct route)"
            )
        else:
            message = "Not connected — no usable credential source found"
    return ProviderConnectionStatus(
        kind=kind,
        label=_PROVIDER_LABELS[kind],
        base_url=describe_url(contract.base_url + contract.catalog_path),
        connected=source is not None,
        credential_source=source,
        model_count=model_count,
        last_refresh_utc=last_refresh,
        last_refresh_source=refresh_source,
        stale=stale,
        status_message=message,
        cached_models=cached,
    )


def connection_statuses() -> List[ProviderConnectionStatus]:
    return [provider_connection_status(kind) for kind in DIRECT_API_PROVIDER_KINDS]


# -- explicit catalog refresh --------------------------------------------------


def refresh_provider_catalog(
    kind: str,
    *,
    timeout_seconds: float = _CATALOG_FETCH_TIMEOUT_SECONDS,
    engine: Optional[str] = None,
    credential: Optional[str] = None,
) -> ProviderCatalogSnapshot:
    """Fetch, normalize, cache, and return one live catalog snapshot.

    Explicit, user-initiated catalog discovery: a read-only GET that
    performs no generation inference and consumes no model credits.
    Failures raise :class:`ProviderConnectionError` with bounded,
    credential-safe text; a failed refresh never fabricates an empty
    successful catalog and leaves the previous cache untouched.
    """

    contract = _CONTRACTS.get(kind)
    if contract is None:
        raise ProviderConnectionError(f"unknown direct-API provider: {kind!r}")
    resolved = credential if credential is not None else resolve_runtime_credential(kind)
    if not resolved:
        env_hint = f"set {contract.env_var}" if contract.env_var else "connect an API key"
        raise ProviderConnectionError(
            f"{_PROVIDER_LABELS[kind]}: no usable credential source — "
            f"{env_hint} or connect an API key for this app session"
        )
    url = contract.base_url + contract.catalog_path
    try:
        payload = request_json(
            "GET",
            url,
            credential=resolved,
            timeout_seconds=timeout_seconds,
            max_response_bytes=_CATALOG_MAX_RESPONSE_BYTES,
            engine=engine,
            tls_signature_blocked=contract.tls_signature_blocked,
        )
    except ProviderHttpError as exc:
        raise ProviderConnectionError(
            f"{_PROVIDER_LABELS[kind]} catalog refresh failed: {exc}"
        ) from exc
    models, truncated = _normalize_catalog(kind, payload)
    if not models:
        raise ProviderConnectionError(
            f"{_PROVIDER_LABELS[kind]} catalog refresh returned no usable models"
        )
    snapshot = ProviderCatalogSnapshot(
        kind=kind,
        fetched_at_utc=_utc_now(),
        source=_SNAPSHOT_SOURCE_LIVE,
        models=models,
        truncated=truncated,
    )
    save_cached_catalog(snapshot)
    return snapshot
