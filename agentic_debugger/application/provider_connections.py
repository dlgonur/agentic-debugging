"""Application-owned provider connections, secure credentials, and live catalog discovery.

One coherent, UI-free domain for the user-owned model-provider registry:

- Persistent, provider-neutral configuration model (:class:`ProviderConfig`);
- Secure durable credential storage (Windows Credential Manager via native OS APIs,
  failing over safely to session-only memory store when unavailable);
- Connection status and presence-only credential checks (never leaking credential bytes);
- Model discovery GET /models with bounded normalization and safe error handling;
- Manual model fallback when live discovery is not supported;
- Protocol resolution for direct API execution (Chat Completions, Responses, Anthropic Messages).

Concepts that stay separate by contract:

- **Discovery & Management** (this module) is catalog configuration and GET /models.
  It performs no generation inference and confers no scientific status.
- **General-runtime execution** resolves through
  :mod:`agentic_debugger.application.model_providers`, which selects the
  explicit route (``direct_api`` / ``legacy_cli``) per model.
- **Capability Ladder qualification** is untouched by this module: adding a
  generic provider or Ollama connection never changes Capability Ladder eligibility.

Credential security rules:

- Credentials are NEVER written to repository files, ordinary JSON provider configs,
  event journals, provenance, session params, logs, screenshots, review artifacts, or argv.
- On Windows, credentials are stored in Windows Credential Manager under the user's
  vault (``CRED_PERSIST_USER``).  On other platforms without OS credential support,
  credentials remain process-local in memory.
- The application truthfully distinguishes between:
  ``saved`` (OS secure store), ``session_key`` (memory-only), ``environment``, and ``cli_auth_store``.
- A provider whose credential/config association is indeterminate (catastrophic
  rollback failure) is durably quarantined: credential resolution returns no
  source and no request is issued until the operator explicitly re-establishes
  a coherent credential pair by saving the provider again.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agentic_debugger.application.provider_http import (
    ProviderHttpError,
    describe_url,
    request_json,
    sanitize_text,
    validate_and_canonicalize_url,
)

__all__ = [
    "AUTH_ANTHROPIC",
    "AUTH_BEARER",
    "AUTH_MODES",
    "AUTH_NONE",
    "CATALOG_DISABLED",
    "CATALOG_MODES",
    "CATALOG_OPENAI",
    "CREDENTIAL_SOURCE_CLI_AUTH_STORE",
    "CREDENTIAL_SOURCE_ENVIRONMENT",
    "CREDENTIAL_SOURCE_SAVED",
    "CREDENTIAL_SOURCE_SESSION_KEY",
    "DIRECT_API_PROVIDER_KINDS",
    "ENDPOINT_CONTRACT_DISPLAY_LABELS",
    "TRANSPORT_COMMANDCODE_GOAT",
    "TRANSPORT_GENERIC",
    "TRANSPORT_MODES",
    "TRANSPORT_OLLAMA_CLOUD",
    "TRANSPORT_OPENCODE_GO",
    "DiscoveredProviderModel",
    "PROTOCOL_CHAT_COMPLETIONS",
    "PROTOCOL_MESSAGES",
    "PROTOCOL_RESPONSES",
    "PROVIDER_CONFIG_SCHEMA_VERSION",
    "PROVIDER_CONFIG_SCHEMA_VERSIONS",
    "ProviderCatalogSnapshot",
    "ProviderConfig",
    "ProviderConnectionError",
    "ProviderConnectionStatus",
    "add_manual_model",
    "add_provider_config",
    "catalog_cache_path",
    "clear_all_session_keys",
    "clear_session_key",
    "connection_statuses",
    "credential_source_for",
    "delete_cached_catalog",
    "delete_provider_config",
    "delete_secure_credential",
    "describe_transport_gap",
    "effective_model_protocol",
    "get_provider_config",
    "protocol_blocker_reason",
    "has_secure_credential",
    "has_session_key",
    "inference_path_for",
    "is_known_provider",
    "is_protocol_executable",
    "is_provider_quarantined",
    "is_valid_provider_id",
    "list_configured_providers",
    "load_cached_catalog",
    "load_provider_configurations",
    "load_secure_credential",
    "peek_session_key",
    "provider_api_model_id",
    "provider_auth_mode",
    "provider_base_url",
    "provider_catalog_mode",
    "provider_configurations_path",
    "provider_connection_status",
    "provider_environment_variable",
    "provider_legacy_cli_auth_file",
    "provider_quarantine_path",
    "provider_session_credential_environment",
    "provider_tls_signature_blocked",
    "provider_transport_credential_environment",
    "provider_transport_network_environment",
    "provider_transport_profile",
    "quarantine_provider",
    "quarantined_providers",
    "refresh_provider_catalog",
    "resolve_model_protocol",
    "resolve_runtime_credential",
    "save_cached_catalog",
    "save_provider_configurations",
    "save_secure_credential",
    "set_session_key",
    "test_provider_connection",
    "update_provider_config",
    "validate_and_canonicalize_url",
    "validate_auth_protocol_combination",
    "validate_provider_config_for_write",
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

PROTOCOL_DISPLAY_LABELS = {
    PROTOCOL_CHAT_COMPLETIONS: "Chat Completions (/chat/completions)",
    PROTOCOL_RESPONSES: "Responses (/responses)",
    PROTOCOL_MESSAGES: "Anthropic Messages (/messages)",
}

#: Explicit authentication modes.  Protocol family and authentication mode
#: are independent configuration axes: Bearer covers OpenAI-compatible Chat
#: Completions / Responses / Bearer Messages gateways, ``anthropic`` covers
#: native Anthropic Messages header semantics (x-api-key +
#: anthropic-version), and ``none`` covers loopback/self-hosted
#: OpenAI-compatible endpoints that legitimately require no credential.
#: Every runnable provider/model combination must have a real implemented
#: transport; unsupported combinations fail before execution.
AUTH_BEARER = "bearer"
AUTH_ANTHROPIC = "anthropic"
AUTH_NONE = "none"
AUTH_MODES = (AUTH_BEARER, AUTH_ANTHROPIC, AUTH_NONE)

AUTH_DISPLAY_LABELS = {
    AUTH_BEARER: "Bearer API key",
    AUTH_ANTHROPIC: "Anthropic API key",
    AUTH_NONE: "No authentication (loopback only)",
}

#: Explicit catalog modes.  Model discovery never determines whether a
#: provider can exist: a provider is either catalog-discoverable via an
#: OpenAI-style ``/models`` endpoint or manual-model-only.
CATALOG_OPENAI = "openai"
CATALOG_DISABLED = "disabled"
CATALOG_MODES = (CATALOG_OPENAI, CATALOG_DISABLED)

#: Explicit transport profiles.  Generic provider identity and transport
#: semantics are separate concepts: an arbitrary user-owned provider NEVER
#: gains historical OpenCode / CommandCode / Ollama behavior merely
#: because its generated or explicit technical ID happens to equal a
#: historical identifier.  Historical transport behavior (catalog path,
#: inference path set, TLS-signature behavior, canonical endpoint binding
#: for ambient credentials, provider environment-variable authority,
#: CLI-auth-store authority, per-model historical protocol resolvers, and
#: legacy CLI fallback eligibility) applies only when the provider
#: configuration explicitly carries the corresponding historical transport
#: profile.  ``generic`` is always the default; historical connections
#: remain expressible intentionally via an explicit profile selection.
TRANSPORT_GENERIC = "generic"
TRANSPORT_OLLAMA_CLOUD = "ollama_cloud"
TRANSPORT_OPENCODE_GO = "opencode_go"
TRANSPORT_COMMANDCODE_GOAT = "commandcode_goat"
TRANSPORT_MODES = (
    TRANSPORT_GENERIC,
    TRANSPORT_OLLAMA_CLOUD,
    TRANSPORT_OPENCODE_GO,
    TRANSPORT_COMMANDCODE_GOAT,
)

#: Historical transport-profile names (the only profiles with
#: ID-independent historical semantics).  Kept identical to the
#: historical provider identifiers so explicitly migrated records keep
#: readable values — but the profile is read from configuration metadata,
#: never inferred from the technical ID string.
HISTORICAL_TRANSPORT_PROFILES = frozenset(
    {TRANSPORT_OLLAMA_CLOUD, TRANSPORT_OPENCODE_GO, TRANSPORT_COMMANDCODE_GOAT}
)

ENDPOINT_CONTRACT_DISPLAY_LABELS = {
    TRANSPORT_GENERIC: "Generic / OpenAI-compatible",
    TRANSPORT_COMMANDCODE_GOAT: "CommandCode",
    TRANSPORT_OPENCODE_GO: "OpenCode",
    TRANSPORT_OLLAMA_CLOUD: "Ollama",
}

TRANSPORT_DISPLAY_LABELS = {
    TRANSPORT_GENERIC: "Generic / OpenAI-compatible",
    TRANSPORT_OLLAMA_CLOUD: "Ollama",
    TRANSPORT_OPENCODE_GO: "OpenCode",
    TRANSPORT_COMMANDCODE_GOAT: "CommandCode",
}


def validate_auth_protocol_combination(auth_mode: str, api_format: str) -> None:
    """Fail closed when an auth/protocol pair has no implemented transport.

    Supported matrix (truthful, extensible):
    - bearer + chat_completions / responses / messages;
    - anthropic + messages only (native Anthropic header contract);
    - none + chat_completions only (loopback/self-hosted OpenAI-compatible).
    """
    if type(auth_mode) is not str or auth_mode not in AUTH_MODES:
        raise ProviderConnectionError(f"unknown authentication mode: {auth_mode!r}")
    if type(api_format) is not str or api_format not in _PROTOCOL_FAMILIES:
        raise ProviderConnectionError(f"unknown API protocol format: {api_format!r}")
    if auth_mode == AUTH_ANTHROPIC and api_format != PROTOCOL_MESSAGES:
        raise ProviderConnectionError(
            "Anthropic authentication requires the Messages protocol; "
            f"got {api_format!r}"
        )
    if auth_mode == AUTH_NONE and api_format != PROTOCOL_CHAT_COMPLETIONS:
        raise ProviderConnectionError(
            "no-auth providers support only the Chat Completions protocol; "
            f"got {api_format!r}"
        )

#: Providers historically bundled with direct API contracts.  Under the
#: user-owned provider registry these ids exist only after the operator
#: explicitly configures them; the tuple only preserves their endpoint
#: contracts (catalog path, inference paths, credential sources).
DIRECT_API_PROVIDER_KINDS = ("opencode_go", "commandcode_goat", "ollama_cloud")

# -- credential sources ------------------------------------------------------

CREDENTIAL_SOURCE_SAVED = "saved"
CREDENTIAL_SOURCE_SESSION_KEY = "session_key"
CREDENTIAL_SOURCE_ENVIRONMENT = "environment"
CREDENTIAL_SOURCE_CLI_AUTH_STORE = "cli_auth_store"

_PROVIDER_CREDENTIAL_SOURCE_LABELS = {
    CREDENTIAL_SOURCE_SAVED: "saved",
    CREDENTIAL_SOURCE_SESSION_KEY: "session only",
    CREDENTIAL_SOURCE_ENVIRONMENT: "environment",
    CREDENTIAL_SOURCE_CLI_AUTH_STORE: "CLI auth",
}

# -- provider endpoint contracts for built-in providers -----------------------

@dataclass(frozen=True)
class _BuiltinProviderContract:
    kind: str
    base_url: str
    catalog_path: str
    inference_paths: Mapping[str, str]
    tls_signature_blocked: bool
    catalog_model_id_pattern: str
    env_var: Optional[str]
    session_env_var: str
    auth_store_consumable: bool


_BUILTIN_CONTRACTS: Mapping[str, _BuiltinProviderContract] = {
    "opencode_go": _BuiltinProviderContract(
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
    "commandcode_goat": _BuiltinProviderContract(
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
    "ollama_cloud": _BuiltinProviderContract(
        kind="ollama_cloud",
        base_url="https://ollama.com",
        catalog_path="/v1/models",
        inference_paths={
            PROTOCOL_CHAT_COMPLETIONS: "/v1/chat/completions",
        },
        tls_signature_blocked=False,
        catalog_model_id_pattern=r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}$",
        env_var="OLLAMA_API_KEY",
        session_env_var="AGENTIC_DEBUGGER_OLLAMA_API_KEY",
        auth_store_consumable=False,
    ),
}

# Preserve _CONTRACTS alias for internal compatibility
_CONTRACTS = _BUILTIN_CONTRACTS


def _contract_for_profile(transport_profile: str) -> Optional[_BuiltinProviderContract]:
    """Historical contract for an explicit transport profile, if historical.

    The lookup key is the EXPLICIT ``transport_profile`` configuration
    value — never the technical provider ID.  Generic providers always
    yield ``None`` regardless of their ID string.
    """
    if type(transport_profile) is not str:
        return None
    if transport_profile not in HISTORICAL_TRANSPORT_PROFILES:
        return None
    return _BUILTIN_CONTRACTS.get(transport_profile)


def _contract_for_config(cfg: Any) -> Optional[_BuiltinProviderContract]:
    """Historical contract for a provider configuration, if historical."""
    profile = getattr(cfg, "transport_profile", None)
    return _contract_for_profile(profile)


def _profile_for_kind(kind: str) -> str:
    """Explicit transport profile for one provider ID.

    Reads configuration metadata.  Only when the provider is NOT
    configured (no durable evidence exists) does it fall back to the
    historical identifier for offline machine probes that cannot consult
    configuration.  Configured providers ALWAYS use their explicit
    profile — an arbitrary user-owned ``ollama_cloud`` record with the
    generic profile never receives historical semantics here.
    """
    try:
        cfg = get_provider_config(kind)
    except Exception:
        cfg = None
    if cfg is not None:
        profile = getattr(cfg, "transport_profile", TRANSPORT_GENERIC)
        if type(profile) is str and profile in TRANSPORT_MODES:
            return profile
        return TRANSPORT_GENERIC
    if type(kind) is str and kind in HISTORICAL_TRANSPORT_PROFILES:
        return kind
    return TRANSPORT_GENERIC


def _contract_for_kind(kind: str) -> Optional[_BuiltinProviderContract]:
    """Historical contract selected through the explicit profile authority."""
    return _contract_for_profile(_profile_for_kind(kind))


class ProviderConnectionError(RuntimeError):
    """Fail-closed provider connection error (credential-safe text)."""


# -- process-local session credentials ---------------------------------------

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
    if kind not in DIRECT_API_PROVIDER_KINDS and not is_known_provider(kind):
        raise ProviderConnectionError(f"unknown direct-API provider: {kind!r}")
    if not _credential_is_usable(value):
        raise ProviderConnectionError("API key is missing, invalid, or oversized")
    _SESSION_KEYS[kind] = value.strip()


def has_session_key(kind: str) -> bool:
    return kind in _SESSION_KEYS


def peek_session_key(kind: str) -> Optional[str]:
    """The stored key (runtime boundary only; never for presentation)."""
    return _SESSION_KEYS.get(kind)


def clear_session_key(kind: str) -> None:
    _SESSION_KEYS.pop(kind, None)


def clear_all_session_keys() -> None:
    _SESSION_KEYS.clear()


# -- OS-level secure durable credential storage (Windows Credential Manager) ---

_WIN_CRED_PREFIX = "AgenticDebugger:provider:"


def _wincred_write(target: str, secret: str) -> bool:
    if sys.platform != "win32" or os.environ.get("AGENTIC_DEBUGGER_DISABLE_SECURE_STORE") == "1":
        return False
    try:
        import ctypes
        import ctypes.wintypes

        advapi32 = ctypes.windll.advapi32

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", ctypes.wintypes.DWORD),
                ("Type", ctypes.wintypes.DWORD),
                ("TargetName", ctypes.wintypes.LPWSTR),
                ("Comment", ctypes.wintypes.LPWSTR),
                ("LastWritten", ctypes.wintypes.FILETIME),
                ("CredentialBlobSize", ctypes.wintypes.DWORD),
                ("CredentialBlob", ctypes.c_char_p),
                ("Persist", ctypes.wintypes.DWORD),
                ("AttributeCount", ctypes.wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", ctypes.wintypes.LPWSTR),
                ("UserName", ctypes.wintypes.LPWSTR),
            ]

        secret_bytes = secret.encode("utf-8")
        cred = CREDENTIAL()
        cred.Flags = 0
        cred.Type = 1  # CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.CredentialBlobSize = len(secret_bytes)
        cred.CredentialBlob = secret_bytes
        cred.Persist = 2  # CRED_PERSIST_USER
        cred.UserName = "AgenticDebugger"

        advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), ctypes.wintypes.DWORD]
        advapi32.CredWriteW.restype = ctypes.wintypes.BOOL
        return bool(advapi32.CredWriteW(ctypes.byref(cred), 0))
    except Exception:
        return False


def _wincred_read(target: str) -> Optional[str]:
    if sys.platform != "win32" or os.environ.get("AGENTIC_DEBUGGER_DISABLE_SECURE_STORE") == "1":
        return None
    try:
        import ctypes
        import ctypes.wintypes

        advapi32 = ctypes.windll.advapi32

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", ctypes.wintypes.DWORD),
                ("Type", ctypes.wintypes.DWORD),
                ("TargetName", ctypes.wintypes.LPWSTR),
                ("Comment", ctypes.wintypes.LPWSTR),
                ("LastWritten", ctypes.wintypes.FILETIME),
                ("CredentialBlobSize", ctypes.wintypes.DWORD),
                ("CredentialBlob", ctypes.c_char_p),
                ("Persist", ctypes.wintypes.DWORD),
                ("AttributeCount", ctypes.wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", ctypes.wintypes.LPWSTR),
                ("UserName", ctypes.wintypes.LPWSTR),
            ]

        pcred = ctypes.POINTER(CREDENTIAL)()
        advapi32.CredReadW.argtypes = [
            ctypes.wintypes.LPWSTR,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(CREDENTIAL)),
        ]
        advapi32.CredReadW.restype = ctypes.wintypes.BOOL
        ok = advapi32.CredReadW(target, 1, 0, ctypes.byref(pcred))
        if not ok or not pcred:
            return None
        blob = ctypes.string_at(pcred.contents.CredentialBlob, pcred.contents.CredentialBlobSize)
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree(pcred)
        decoded = blob.decode("utf-8", errors="replace")
        return decoded if _credential_is_usable(decoded) else None
    except Exception:
        return None


def _wincred_delete(target: str) -> bool:
    if sys.platform != "win32" or os.environ.get("AGENTIC_DEBUGGER_DISABLE_SECURE_STORE") == "1":
        return False
    try:
        import ctypes
        import ctypes.wintypes

        advapi32 = ctypes.windll.advapi32
        advapi32.CredDeleteW.argtypes = [
            ctypes.wintypes.LPWSTR,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
        ]
        advapi32.CredDeleteW.restype = ctypes.wintypes.BOOL
        return bool(advapi32.CredDeleteW(target, 1, 0))
    except Exception:
        return False


def save_secure_credential(provider_id: str, secret: str) -> bool:
    """Save an API key in the OS secure credential store if available."""
    if not _credential_is_usable(secret):
        return False
    return _wincred_write(_WIN_CRED_PREFIX + provider_id, secret.strip())


def load_secure_credential(provider_id: str) -> Optional[str]:
    """Load an API key from the OS secure credential store if available."""
    return _wincred_read(_WIN_CRED_PREFIX + provider_id)


def delete_secure_credential(provider_id: str) -> bool:
    """Delete an API key from the OS secure credential store."""
    return _wincred_delete(_WIN_CRED_PREFIX + provider_id)


def has_secure_credential(provider_id: str) -> bool:
    """Presence-only check for a stored credential in the OS secure store."""
    val = load_secure_credential(provider_id)
    return bool(val)


# -- OpenCode CLI auth store --------------------------------------------------

_MAX_AUTH_STORE_BYTES = 64 * 1024


def opencode_auth_store_path() -> Path:
    profile = os.environ.get("OPENCODE_CONFIG_DIR") or str(Path.home())
    return Path(profile) / ".local" / "share" / "opencode" / "auth.json"


def _read_opencode_auth_store_key(path: Path) -> Optional[str]:
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


# -- bounded execution-environment authority ----------------------------------
#
# One authority owns every operator environment variable a provider
# transport child may legitimately require.  The adapter subprocess starts
# from a minimal environment (never the full process environment), so only
# what this authority returns — plus the single private credential hop —
# can reach the direct adapter, the legacy CLI adapter, and (inside the
# adapter process) the stdlib/curl HTTP engines and their child curl
# process (which inherits the adapter environment).
#
# Exact allowlist and why each entry exists:
#
# - ``SSL_CERT_FILE``: file path of an additional CA bundle.  Consumed by
#   the stdlib engine via a per-request fresh ``ssl.create_default_context()``
#   (which honors ``SSL_CERT_FILE`` through OpenSSL default verify paths;
#   the context is built per request because urllib's module-global opener
#   would otherwise freeze the process's first-observed trust/proxy state)
#   and by the curl engine (libcurl honors ``SSL_CERT_FILE``/
#   ``CURL_CA_BUNDLE`` for CA-bundle lookup).  Required so a
#   connection/catalog check that succeeded in the parent with a custom
#   CA also succeeds in the child.
# - ``SSL_CERT_DIR``: directory of hashed CA certificates.  Consumed by
#   the stdlib engine via OpenSSL default verify paths (capath) and by
#   curl for the same purpose.
# - ``CURL_CA_BUNDLE``: explicit CA-bundle path honored by the curl
#   engine (libcurl).  The stdlib engine does not read it, but the curl
#   child inside the adapter process does.
# - ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY`` and
#   their lowercase variants (``http_proxy`` etc.): proxy routing state.
#   Consumed by the stdlib engine via ``urllib.request.getproxies()``
#   (which reads both casings) and by the curl engine (libcurl honors
#   both casings).  Both casings are forwarded when present because the
#   underlying engines honor both; on Windows the OS treats names
#   case-insensitively but ``os.environ`` preserves the operator's
#   original casing, so each present spelling is preserved verbatim.
#
# Deliberately NOT included:
#
# - ``OPENCODE_CONFIG_DIR``: after the direct-route credential fix below,
#   the direct adapter never needs the operator auth-store location (the
#   winning CLI-auth credential is forwarded once as a value under the
#   provider's private session credential variable).  The legacy CLI
#   route uses an explicit bounded ``--auth-file`` path instead of
#   inheriting this directory.  No generic provider inherits OpenCode
#   auth-store semantics.
# - ``REQUESTS_CA_BUNDLE`` / ``CERTIFI_*`` / any other variable: neither
#   the stdlib provider HTTP engine, the curl engine, nor an accepted
#   provider adapter consumes them; they are not forwarded.
#
# Privacy: proxy values may embed ``user:password@`` credentials and CA
# paths may disclose machine layout.  Values returned here must never
# appear in command argv, session journals, evidence/event payloads,
# exception text, UI diagnostics, review artifacts, or patch fixtures.
# Only isolated synthetic values appear in tests.

PROVIDER_TRANSPORT_NETWORK_ENV_ALLOWLIST: Tuple[str, ...] = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)

#: Bound for one forwarded network-environment value (paths and proxy
#: URLs are short; anything larger is rejected rather than truncated).
_PROVIDER_TRANSPORT_NETWORK_ENV_MAX_CHARS = 8192


def _bounded_network_env_value(value: Any) -> Optional[str]:
    if type(value) is not str or not value:
        return None
    if len(value) > _PROVIDER_TRANSPORT_NETWORK_ENV_MAX_CHARS:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def provider_transport_network_environment() -> Dict[str, str]:
    """Bounded operator network/trust environment for transport children.

    Returns the present subset of
    :data:`PROVIDER_TRANSPORT_NETWORK_ENV_ALLOWLIST` with bounded,
    control-character-free values.  Never raises: an unreadable
    environment yields an empty mapping.  Callers merge the result into
    the adapter child environment; the full process environment is never
    inherited.
    """
    result: Dict[str, str] = {}
    try:
        for name in PROVIDER_TRANSPORT_NETWORK_ENV_ALLOWLIST:
            try:
                raw = os.environ.get(name)
            except Exception:
                continue
            bounded = _bounded_network_env_value(raw) if raw is not None else None
            if bounded is not None:
                result[name] = bounded
    except Exception:
        return {}
    return result


def provider_legacy_cli_auth_file(kind: str) -> Optional[str]:
    """Explicit bounded auth-store path for the accepted legacy CLI route.

    Returns the absolute operator auth-store path ONLY for providers
    explicitly carrying the historical ``opencode_go`` transport profile
    whose endpoint binding is still valid and whose auth-store file is
    present.  Generic providers — even ones identified ``opencode_go`` —
    always yield ``None`` and never gain CLI/auth-store semantics.
    The path itself is non-secret (safe for argv); its content is never
    returned, logged, or embedded here.
    """
    try:
        cfg = get_provider_config(kind)
    except Exception:
        return None
    if cfg is None or not cfg.enabled:
        return None
    if getattr(cfg, "transport_profile", TRANSPORT_GENERIC) != TRANSPORT_OPENCODE_GO:
        return None
    if not _endpoint_binding_valid(kind, cfg):
        return None
    try:
        path = opencode_auth_store_path()
    except Exception:
        return None
    try:
        text = str(path)
    except Exception:
        return None
    if not text or len(text) > 8192:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        return None
    candidate = Path(text)
    try:
        if not candidate.is_absolute():
            return None
    except Exception:
        return None
    try:
        if not candidate.is_file():
            return None
    except OSError:
        return None
    return text


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
        proto = protocol if protocol is not None else resolve_model_protocol(kind, model_id)
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
            validate_auth_protocol_combination(auth_raw, api_format)
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
                        validate_auth_protocol_combination(auth_raw, proto)
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
    _commit_provider_and_credential) abort before any credential mutation.
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

    previous_cred = load_secure_credential(provider_id)
    if previous_cred is not None:
        deleted = delete_secure_credential(provider_id)
        remaining = load_secure_credential(provider_id)
        if not deleted or remaining is not None:
            raise ProviderConnectionError(
                "provider credential cleanup could not be completed"
            )

    clear_session_key(provider_id)
    _purge_legacy_cached_catalog(provider_id)
    clear_provider_quarantine(provider_id)
    if is_provider_quarantined(provider_id):
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
    for pid in legacy_ids:
        previous_cred = load_secure_credential(pid)
        if previous_cred is not None:
            deleted = delete_secure_credential(pid)
            remaining = load_secure_credential(pid)
            if not deleted or remaining is not None:
                raise ProviderConnectionError(
                    f"legacy provider credential cleanup failed for {pid!r}"
                )

        clear_session_key(pid)
        _purge_legacy_cached_catalog(pid)
        clear_provider_quarantine(pid)
        if is_provider_quarantined(pid):
            raise ProviderConnectionError(
                f"legacy provider quarantine cleanup failed for {pid!r}"
            )

    try:
        save_provider_configurations(cleaned_configs)
    except Exception as exc:
        raise ProviderConnectionError(
            "legacy provider migration could not save updated configuration"
        ) from exc


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
        _migrate_legacy_builtin_records(configs, legacy_builtin_ids)

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
    if base not in existing and is_valid_provider_id(base):
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
        if candidate not in existing and is_valid_provider_id(candidate):
            return candidate
        counter += 1
        if counter > 100000:
            raise ProviderConnectionError(
                "provider identity could not be generated within its bound"
            )


def _commit_provider_and_credential(
    provider_id: str,
    updated_configs: List[ProviderConfig],
    api_key: Optional[str],
) -> None:
    """Commit one logical provider save with pre-armed durable quarantine.

    Invariant: NO credential mutation may begin unless the provider is already
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
        save_provider_configurations(updated_configs)
        return
    if not _credential_is_usable(stripped):
        raise ProviderConnectionError("API key is missing, invalid, or oversized")
    # Durably arm fail-closed marker before mutating any credential
    try:
        quarantine_provider(provider_id)
    except ProviderConnectionError:
        raise ProviderConnectionError("provider credential quarantine could not be armed; no provider changes were applied") from None
    previous_credential: Optional[str] = load_secure_credential(provider_id)
    if not save_secure_credential(provider_id, stripped):
        # Credential save failed after arming: original pair still coherent
        try:
            clear_provider_quarantine(provider_id)
        except ProviderConnectionError:
            pass
        raise ProviderConnectionError("Could not save API key securely.") from None
    try:
        save_provider_configurations(updated_configs)
    except ProviderConnectionError:
        if previous_credential is not None and save_secure_credential(
            provider_id, previous_credential
        ):
            try:
                clear_provider_quarantine(provider_id)
            except ProviderConnectionError:
                pass
            rollback_note = "the previous credential state was restored"
        elif delete_secure_credential(provider_id):
            rollback_note = (
                "no API key remains stored for this provider"
                if previous_credential is None
                else "the stored API key could not be restored and was removed; re-enter it before retrying"
            )
        else:
            clear_session_key(provider_id)
            rollback_note = (
                "the credential state requires recovery; "
                "edit this provider and save an API key again"
            )
        raise ProviderConnectionError(
            "provider configuration could not be written; no provider changes "
            f"were applied and {rollback_note}"
        ) from None
    try:
        clear_provider_quarantine(provider_id)
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
        validate_auth_protocol_combination(auth_mode, api_format)
    except ProviderConnectionError as exc:
        raise ProviderConnectionError(str(exc)) from None
    try:
        _inference_path_for_profile(transport_profile, api_format)
    except ProviderConnectionError as exc:
        raise ProviderConnectionError(str(exc)) from None
    for m in models:
        effective = m.protocol if m.protocol is not None else api_format
        try:
            validate_auth_protocol_combination(auth_mode, effective)
        except ProviderConnectionError as exc:
            raise ProviderConnectionError(
                f"model {m.model_id!r}: {exc}"
            ) from None
        try:
            _inference_path_for_profile(transport_profile, effective)
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

    configs = load_provider_configurations()
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
        if not is_valid_provider_id(pid):
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
    validate_provider_config_for_write(new_cfg)
    updated = [c for c in configs if c.provider_id != pid] + [new_cfg]
    _commit_provider_and_credential(pid, updated, api_key)
    if api_key is not None and api_key.strip():
        clear_session_key(pid)
        try:
            clear_provider_quarantine(pid)
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
    configs = load_provider_configurations()
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
        if credential_source_for(provider_id) is not None:
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
        delete_cached_catalog(provider_id)
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
    validate_provider_config_for_write(updated_cfg)
    updated = [updated_cfg if c.provider_id == provider_id else c for c in configs]
    _commit_provider_and_credential(provider_id, updated, api_key)
    if api_key is not None and api_key.strip():
        clear_session_key(provider_id)
        try:
            clear_provider_quarantine(provider_id)
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

    configs = load_provider_configurations()
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
        save_provider_configurations(filtered)
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
    cfg = get_provider_config(provider_id)
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
        proto = cfg.api_format
    # The effective model protocol must satisfy the provider
    # authentication matrix and the explicit transport-profile
    # capability: unsupported effective combinations are rejected here,
    # before persistence, rather than surfacing as harness failures later.
    try:
        validate_auth_protocol_combination(cfg.auth_mode, proto)
    except ProviderConnectionError as exc:
        raise ProviderConnectionError(f"model {mid!r}: {exc}") from None
    try:
        _inference_path_for_profile(cfg.transport_profile, proto)
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


# -- credential resolution ----------------------------------------------------

def _session_env_var_for(kind: str) -> str:
    contract = _contract_for_kind(kind)
    if contract is not None:
        return contract.session_env_var
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", kind.upper())
    return f"AGENTIC_DEBUGGER_PROVIDER_{normalized}_API_KEY"


def _canonical_base_for(kind: str) -> Optional[str]:
    contract = _contract_for_kind(kind)
    return contract.base_url if contract is not None else None


def _endpoint_binding_valid(kind: str, cfg: ProviderConfig) -> bool:
    """Whether ambient canonical credentials remain bound to this endpoint.

    A credential implicitly tied to a historical/canonical provider
    endpoint must never become usable against an arbitrary edited
    endpoint.  For historical provider identities, ambient environment /
    CLI-auth credentials are usable only while the configured Base URL
    still matches the canonical contract endpoint.  Explicitly saved or
    session credentials rebound via the update path remain usable after
    an endpoint change because the operator re-entered them for the new
    endpoint.
    """
    canonical = _canonical_base_for(kind)
    if canonical is None:
        return True
    try:
        return cfg.base_url == validate_and_canonicalize_url(canonical)
    except Exception:
        return False


def _forwarded_matches_explicit_credential(kind: str, forwarded: str) -> bool:
    """Whether a forwarded session-env value equals an explicit credential."""
    try:
        saved = load_secure_credential(kind)
        if _credential_is_usable(saved) and saved.strip() == forwarded.strip():
            return True
    except Exception:
        pass
    try:
        session_value = peek_session_key(kind)
        if _credential_is_usable(session_value) and session_value.strip() == forwarded.strip():
            return True
    except Exception:
        pass
    return False


def credential_source_for(kind: str) -> Optional[str]:
    """Which credential source the direct route can use right now.

    Authority model (no partial special cases): saved/session/forwarded
    session sources are provider-identity-bound and survive an explicit
    endpoint rebinding once the operator re-enters them; ambient
    environment / CLI-auth sources are canonical-endpoint-bound and are
    never selected once the configured endpoint deviates from the
    historical canonical endpoint.  The update path blocks silent
    rebinding whenever ANY of these sources is present, so runtime
    selection here is safe.
    """
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        return None
    if cfg.auth_mode == AUTH_NONE:
        return None
    if is_provider_quarantined(kind):
        # Indeterminate credential/config association: never select a source.
        return None
    if has_secure_credential(kind):
        return CREDENTIAL_SOURCE_SAVED
    if has_session_key(kind):
        return CREDENTIAL_SOURCE_SESSION_KEY
    session_var = _session_env_var_for(kind)
    forwarded = os.environ.get(session_var)
    if _credential_is_usable(forwarded):
        # Provider-identity-bound session hop (UI -> worker -> adapter).
        # The child transport uses a minimal environment plus this explicit
        # override, so ambient leakage cannot reach the adapter except
        # through this variable — and the update path already requires
        # explicit rebinding whenever it is present during an endpoint
        # change.  Trust it here; the transport forwarder applies the same
        # rule when choosing what to propagate.
        return CREDENTIAL_SOURCE_SESSION_KEY

    contract = _contract_for_config(cfg)
    if contract is not None:
        if not _endpoint_binding_valid(kind, cfg):
            return None
        if contract.env_var and _credential_is_usable(os.environ.get(contract.env_var)):
            return CREDENTIAL_SOURCE_ENVIRONMENT
        if contract.auth_store_consumable and _read_opencode_auth_store_key(
            opencode_auth_store_path()
        ):
            return CREDENTIAL_SOURCE_CLI_AUTH_STORE
    return None


def resolve_runtime_credential(kind: str) -> Optional[str]:
    """The credential value for one direct-API request (runtime only).

    Saved/session/forwarded session sources are provider-identity-bound
    and resolved in that order so an explicitly rebound saved credential
    always wins over a stale forwarded ambient value.  Ambient
    environment / CLI-auth sources are canonical-endpoint-bound and are
    never resolved once the endpoint deviates.  No-auth providers always
    resolve to ``None``.
    """
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        return None
    if cfg.auth_mode == AUTH_NONE:
        return None
    if is_provider_quarantined(kind):
        return None

    saved_value = load_secure_credential(kind)
    if saved_value and _credential_is_usable(saved_value):
        return saved_value.strip()

    session_value = peek_session_key(kind)
    if session_value and _credential_is_usable(session_value):
        return session_value.strip()

    session_var = _session_env_var_for(kind)
    forwarded_value = os.environ.get(session_var)
    if _credential_is_usable(forwarded_value):
        return forwarded_value.strip()

    contract = _contract_for_config(cfg)
    if contract is not None:
        if not _endpoint_binding_valid(kind, cfg):
            return None
        if contract.env_var:
            env_value = os.environ.get(contract.env_var)
            if _credential_is_usable(env_value):
                return env_value.strip()
        if contract.auth_store_consumable:
            return _read_opencode_auth_store_key(opencode_auth_store_path())
    return None


def provider_environment_variable(kind: str) -> Optional[str]:
    contract = _contract_for_kind(kind)
    return contract.env_var if contract is not None else None


# Provider-owned environment authority that does not carry the Agentic
# Debugger namespace prefix: the operator CLI auth-store location.  The
# built-in credential variables and every private session hop variable are
# derived from the contracts below so there is one authority, not a
# duplicated drifting list at each child spawn site.
_PROVIDER_CLI_AUTH_ENV_NAME = "OPENCODE_CONFIG_DIR"


def provider_authority_environment_names() -> Tuple[str, ...]:
    """Environment identities owned by the provider credential authority.

    The centralized classification source for the V2 execution-environment
    authority: built-in provider credential variables (``OPENCODE_API_KEY``,
    ``COMMAND_CODE_API_KEY``, ``OLLAMA_API_KEY``), every built-in contract's
    private session credential hop variable, and the provider CLI
    auth-store location (``OPENCODE_CONFIG_DIR``).  The remainder of the
    Agentic Debugger-owned control namespace is classified structurally by
    its ``AGENTIC_DEBUGGER_`` prefix and is intentionally not duplicated
    here.  Names only — never values.
    """
    names = {_PROVIDER_CLI_AUTH_ENV_NAME}
    for contract in _BUILTIN_CONTRACTS.values():
        if contract.env_var:
            names.add(contract.env_var)
        names.add(contract.session_env_var)
    return tuple(sorted(names))


def provider_session_credential_environment(
    kind: str,
) -> Optional[Mapping[str, str]]:
    """Private UI-to-worker credential hop (exactly one variable).

    Forwards the winning credential source under the same authority as
    :func:`resolve_runtime_credential`: saved/session values are
    provider-identity-bound; ambient environment and CLI-auth sources are
    forwarded only while the endpoint binding remains valid.  The winning
    CLI-auth credential is resolved once here and forwarded as a VALUE
    under the provider's private session credential variable, so the
    worker never needs the operator's ``OPENCODE_CONFIG_DIR`` location.
    No-auth providers forward nothing.  Values must never enter argv,
    journals, evidence, or diagnostics.
    """
    if is_provider_quarantined(kind):
        return None
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        return None
    secret = load_secure_credential(kind)
    if not secret or not _credential_is_usable(secret):
        secret = peek_session_key(kind)
    if secret and _credential_is_usable(secret):
        session_var = _session_env_var_for(kind)
        return {session_var: secret.strip()}
    contract = _contract_for_config(cfg)
    if contract is not None and contract.auth_store_consumable:
        if not _endpoint_binding_valid(kind, cfg):
            return None
        try:
            cli_key = _read_opencode_auth_store_key(opencode_auth_store_path())
        except Exception:
            cli_key = None
        if _credential_is_usable(cli_key):
            session_var = _session_env_var_for(kind)
            assert isinstance(cli_key, str)
            return {session_var: cli_key.strip()}
    return None


def provider_transport_credential_environment(
    kind: str,
) -> Optional[Mapping[str, str]]:
    """Child environment forwarding for the direct adapter.

    Forwards the winning credential source under the same authority as
    :func:`resolve_runtime_credential`: saved/session/forwarded session
    values are provider-identity-bound; ambient environment sources are
    forwarded only while the endpoint binding remains valid.  The winning
    CLI-auth credential is resolved once here and forwarded as a VALUE
    under the provider's private session credential variable, so the
    child direct adapter resolves it normally without rediscovering
    operator auth state or requiring ``OPENCODE_CONFIG_DIR``.  No-auth
    providers forward nothing.  The child transport starts from a minimal
    environment, so only what is forwarded here (plus the bounded network
    authority in the transport environment) can reach the adapter.
    """
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        return None
    if cfg.auth_mode == AUTH_NONE:
        return None
    if is_provider_quarantined(kind):
        return None
    contract = _contract_for_config(cfg)
    session_var = _session_env_var_for(kind)
    saved_value = load_secure_credential(kind)
    if _credential_is_usable(saved_value):
        return {session_var: saved_value.strip()}
    session_value = peek_session_key(kind)
    if _credential_is_usable(session_value):
        return {session_var: session_value.strip()}
    forwarded_value = os.environ.get(session_var)
    if _credential_is_usable(forwarded_value):
        return {session_var: forwarded_value.strip()}
    if contract and contract.env_var:
        if not _endpoint_binding_valid(kind, cfg):
            return None
        env_value = os.environ.get(contract.env_var)
        if _credential_is_usable(env_value):
            return {contract.env_var: env_value.strip()}
    if contract is not None and contract.auth_store_consumable:
        if not _endpoint_binding_valid(kind, cfg):
            return None
        try:
            cli_key = _read_opencode_auth_store_key(opencode_auth_store_path())
        except Exception:
            cli_key = None
        if _credential_is_usable(cli_key):
            assert isinstance(cli_key, str)
            return {session_var: cli_key.strip()}
    return None


# -- protocol resolution ------------------------------------------------------

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
    if type(model_id) is not str or not model_id:
        return None
    text = model_id.strip()
    if text.startswith(_OPENCODE_GO_MODEL_PREFIX):
        text = text[len(_OPENCODE_GO_MODEL_PREFIX):]
    return _OPENCODE_GO_DOCUMENTED_PROTOCOLS.get(text)


def resolve_commandcode_protocol(model_id: str) -> Optional[str]:
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
    """Deterministic protocol family for one provider model, or None.

    Historical per-model resolvers apply only when the provider
    configuration explicitly carries the corresponding historical
    transport profile — never from the technical ID alone.  A generic
    provider (even one identified ``opencode_go``) resolves through its
    configured models and provider default.
    """
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    if cfg.transport_profile == TRANSPORT_OPENCODE_GO:
        return resolve_opencode_go_protocol(model_id)
    if cfg.transport_profile == TRANSPORT_COMMANDCODE_GOAT:
        return resolve_commandcode_protocol(model_id)
    for m in cfg.models:
        if m.model_id == model_id and m.protocol:
            return m.protocol
    return cfg.api_format


def effective_model_protocol(kind: str, model_id: str) -> str:
    """Effective executable protocol for one provider model.

    Resolves via :func:`resolve_model_protocol`, then validates the
    EFFECTIVE protocol against the provider's authentication mode and
    explicit transport-profile capability (inference-path availability).
    Raises :class:`ProviderConnectionError` when the model has no
    resolved protocol or the effective pair has no implemented transport.
    This is the single authority consulted before persistence (where
    knowable), picker availability, doctor/runnable status,
    connection/model testing, LiveModelConfig creation, and adapter
    execution — a model-specific protocol can never bypass the auth
    matrix or the transport capability set.
    """
    protocol = resolve_model_protocol(kind, model_id)
    if protocol is None:
        raise ProviderConnectionError(
            f"provider {kind!r} model {model_id!r} has no resolved protocol"
        )
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    try:
        validate_auth_protocol_combination(cfg.auth_mode, protocol)
    except ProviderConnectionError as exc:
        raise ProviderConnectionError(
            f"provider {kind!r} model {model_id!r}: {exc}"
        ) from None
    try:
        inference_path_for(kind, protocol)
    except ProviderConnectionError as exc:
        raise ProviderConnectionError(
            f"provider {kind!r} model {model_id!r}: {exc}"
        ) from None
    return protocol


def protocol_blocker_reason(kind: str, protocol: Optional[str]) -> Optional[str]:
    """Credential-safe reason a protocol is not executable, or ``None``.

    Single authority behind picker/doctor/status unavailability reasons:
    unknown protocols, auth-matrix violations, and transport-profile
    capability gaps each yield an actionable message; executable pairs
    yield ``None``.  Never raises.
    """
    try:
        cfg = get_provider_config(kind)
    except Exception:
        return f"provider {kind!r} configuration is unavailable"
    if cfg is None or not cfg.enabled:
        return f"provider {kind!r} is not configured"
    if type(protocol) is not str or protocol not in _PROTOCOL_FAMILIES:
        return f"unknown protocol: {protocol!r}"
    try:
        validate_auth_protocol_combination(cfg.auth_mode, protocol)
    except ProviderConnectionError as exc:
        return str(exc)
    try:
        inference_path_for(kind, protocol)
    except ProviderConnectionError as exc:
        return str(exc)
    return None


def is_protocol_executable(kind: str, protocol: Optional[str]) -> bool:
    """Whether one protocol is executable for one provider (never raises)."""
    return protocol_blocker_reason(kind, protocol) is None


def provider_api_model_id(kind: str, model_id: str) -> str:
    """Exact model identity sent to a provider's direct API."""
    if type(model_id) is not str or not model_id.strip():
        raise ProviderConnectionError("provider model id is missing")
    value = model_id.strip()
    if _profile_for_kind(kind) == TRANSPORT_OPENCODE_GO and value.startswith(
        _OPENCODE_GO_MODEL_PREFIX
    ):
        value = value[len(_OPENCODE_GO_MODEL_PREFIX):]
    return value


def _inference_path_for_profile(transport_profile: str, protocol: str) -> str:
    """Pure profile/protocol path authority (no configuration lookup)."""
    if type(transport_profile) is str:
        contract = _BUILTIN_CONTRACTS.get(transport_profile)
        if contract is not None and transport_profile in HISTORICAL_TRANSPORT_PROFILES:
            path = contract.inference_paths.get(protocol)
            if path is None:
                raise ProviderConnectionError(
                    f"provider {contract.kind!r} does not expose the {protocol!r} protocol"
                )
            return path
    if protocol == PROTOCOL_CHAT_COMPLETIONS:
        return "/chat/completions"
    if protocol == PROTOCOL_RESPONSES:
        return "/responses"
    if protocol == PROTOCOL_MESSAGES:
        return "/messages"
    raise ProviderConnectionError(f"unsupported protocol: {protocol!r}")


def inference_path_for(kind: str, protocol: str) -> str:
    """Supported inference path for one provider protocol.

    One coherent authority: historical path sets apply only under an
    explicit historical transport profile; generic providers use the
    OpenAI-compatible path set.  Raises before execution for combinations
    the adapter could only reject later.
    """
    return _inference_path_for_profile(_profile_for_kind(kind), protocol)


def provider_base_url(kind: str) -> str:
    cfg = get_provider_config(kind)
    if cfg is not None and cfg.base_url:
        return cfg.base_url
    raise ProviderConnectionError(f"provider {kind!r} is not configured")


def provider_tls_signature_blocked(kind: str) -> bool:
    contract = _contract_for_kind(kind)
    if contract is not None:
        return contract.tls_signature_blocked
    cfg = get_provider_config(kind)
    if cfg is not None:
        return cfg.tls_signature_blocked
    return False


def _catalog_pattern_for(kind: str) -> str:
    """Model-ID pattern selected through the explicit profile authority."""
    contract = _contract_for_kind(kind)
    if contract is not None:
        return contract.catalog_model_id_pattern
    return r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}$"


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
        if kind not in DIRECT_API_PROVIDER_KINDS and not is_known_provider(kind):
            return None
        if type(fetched) is not str or not _valid_utc(fetched):
            return None
        if source != _SNAPSHOT_SOURCE_LIVE:
            return None
        if type(truncated) is not bool:
            return None
        if type(models) is not list or len(models) > _MAX_CATALOG_MODELS:
            return None
        pattern = _catalog_pattern_for(kind)
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
                return None
            display = _display_name(kind, model_id)
            decoded.append(
                DiscoveredProviderModel(
                    kind=kind,
                    model_id=model_id,
                    display_name=display,
                    protocol=protocol,
                    runnable=protocol is not None,
                    unavailable_reason=None if protocol is not None else "Protocol not yet resolved for direct API",
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
    pattern = re.compile(_catalog_pattern_for(kind))

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
    config_path = provider_configurations_path()
    return config_path.with_name("provider-catalog-cache.json")


def load_cached_catalog(kind: str) -> Optional[ProviderCatalogSnapshot]:
    if not is_known_provider(kind):
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
    if not is_known_provider(snapshot.kind):
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
    cfg = get_provider_config(kind)
    if cfg is None:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")

    contract = _contract_for_config(cfg)
    source = credential_source_for(kind)
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
    runnable_models = [
        m
        for m in cached
        if m.protocol is not None
        and is_protocol_executable(kind, m.protocol)
    ]
    incompatible_models = [
        m for m in cached if m.protocol is not None and m not in runnable_models
    ]
    quarantined = is_provider_quarantined(kind)

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
            if contract is not None and contract.env_var and not _endpoint_binding_valid(kind, cfg):
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
    configs = load_provider_configurations()
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
    if is_provider_quarantined(kind):
        raise ProviderConnectionError(
            "Credential state requires recovery. Edit provider and save an API key again."
        )
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    if cfg.catalog_mode == CATALOG_DISABLED:
        raise ProviderConnectionError(
            f"{cfg.name}: catalog discovery is disabled for this provider — "
            "add models manually"
        )

    contract = _contract_for_config(cfg)
    if cfg.auth_mode == AUTH_NONE:
        resolved = None
        if credential is not None:
            raise ProviderConnectionError(
                f"{cfg.name}: no-auth providers must not send a credential"
            )
    else:
        resolved = credential if credential is not None else resolve_runtime_credential(kind)
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
        update_provider_config(kind, models=models)

    return snapshot


def test_provider_connection(
    kind: str,
    *,
    model_id: Optional[str] = None,
    timeout_seconds: float = _CATALOG_FETCH_TIMEOUT_SECONDS,
    engine: Optional[str] = None,
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
    """
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    if is_provider_quarantined(kind):
        raise ProviderConnectionError(
            "Credential state requires recovery. Edit provider and save an API key again."
        )
    try:
        validate_auth_protocol_combination(cfg.auth_mode, cfg.api_format)
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
            protocol = effective_model_protocol(kind, model_id.strip())
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
        if not is_protocol_executable(kind, protocol):
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
    if cfg.auth_mode != AUTH_NONE and resolve_runtime_credential(kind) is None:
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
            if m.protocol is not None and is_protocol_executable(kind, m.protocol)
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
    contract = _contract_for_config(cfg)
    resolved = resolve_runtime_credential(kind) if cfg.auth_mode != AUTH_NONE else None
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
