"""Application-owned provider connections, secure credentials, and live catalog discovery.

One coherent, UI-free domain for both built-in and user-configured model providers:

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
    "CREDENTIAL_SOURCE_CLI_AUTH_STORE",
    "CREDENTIAL_SOURCE_ENVIRONMENT",
    "CREDENTIAL_SOURCE_SAVED",
    "CREDENTIAL_SOURCE_SESSION_KEY",
    "DIRECT_API_PROVIDER_KINDS",
    "DiscoveredProviderModel",
    "PROTOCOL_CHAT_COMPLETIONS",
    "PROTOCOL_MESSAGES",
    "PROTOCOL_RESPONSES",
    "PROVIDER_CONFIG_SCHEMA_VERSION",
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
    "get_provider_config",
    "has_secure_credential",
    "has_session_key",
    "inference_path_for",
    "is_known_provider",
    "is_provider_quarantined",
    "list_configured_providers",
    "load_cached_catalog",
    "load_provider_configurations",
    "load_secure_credential",
    "peek_session_key",
    "provider_api_model_id",
    "provider_base_url",
    "provider_configurations_path",
    "provider_connection_status",
    "provider_environment_variable",
    "provider_quarantine_path",
    "provider_session_credential_environment",
    "provider_tls_signature_blocked",
    "provider_transport_credential_environment",
    "quarantine_provider",
    "quarantined_providers",
    "refresh_provider_catalog",
    "resolve_model_protocol",
    "resolve_runtime_credential",
    "save_cached_catalog",
    "save_provider_configurations",
    "save_secure_credential",
    "set_session_key",
    "update_provider_config",
    "validate_and_canonicalize_url",
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

#: Providers historically built-in with direct API contracts
DIRECT_API_PROVIDER_KINDS = ("opencode_go", "commandcode_goat", "ollama_cloud")

_BUILTIN_PROVIDER_LABELS = {
    "opencode_go": "OpenCode Go",
    "commandcode_goat": "CommandCode GOAT",
    "ollama_cloud": "Ollama",
}

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


# -- provider data model & persistence ---------------------------------------

PROVIDER_CONFIG_SCHEMA_VERSION = "provider-configurations-v1"
_MAX_PROVIDERS_CONFIGURED = 32
_MAX_CONFIG_FILE_BYTES = 1024 * 1024


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "base_url": self.base_url,
            "api_format": self.api_format,
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
        if not isinstance(data, Mapping):
            return None
        pid = data.get("provider_id")
        name = data.get("name")
        base_url = data.get("base_url")
        api_format = data.get("api_format", PROTOCOL_CHAT_COMPLETIONS)
        if not pid or not isinstance(pid, str) or not name or not isinstance(name, str):
            return None
        if not base_url or not isinstance(base_url, str):
            return None
        try:
            clean_url = validate_and_canonicalize_url(base_url)
        except Exception:
            return None
        if api_format not in _PROTOCOL_FAMILIES:
            api_format = PROTOCOL_CHAT_COMPLETIONS
        models_raw = data.get("models", [])
        models: List[DiscoveredProviderModel] = []
        if isinstance(models_raw, list):
            for m in models_raw:
                if isinstance(m, Mapping) and m.get("model_id"):
                    mid = str(m["model_id"])
                    disp = str(m.get("display_name") or mid)
                    proto = m.get("protocol")
                    if proto not in _PROTOCOL_FAMILIES:
                        try:
                            proto = resolve_model_protocol(pid, mid) or api_format
                        except Exception:
                            proto = api_format
                    models.append(
                        DiscoveredProviderModel(
                            kind=pid,
                            model_id=mid,
                            display_name=disp,
                            protocol=proto,
                            runnable=proto is not None,
                            unavailable_reason=None if proto else "Protocol unresolved",
                        )
                    )
        return cls(
            provider_id=pid,
            name=name,
            base_url=clean_url,
            api_format=api_format,
            models=tuple(models),
            last_refresh_utc=data.get("last_refresh_utc"),
            last_refresh_source=data.get("last_refresh_source"),
            enabled=bool(data.get("enabled", True)),
            is_builtin=bool(data.get("is_builtin", False)),
            builtin_kind=data.get("builtin_kind"),
            tls_signature_blocked=bool(data.get("tls_signature_blocked", False)),
        )


PROVIDER_CONFIG_SCHEMA_VERSION = "provider-configurations-v1"
_MAX_CONFIG_FILE_BYTES = 256 * 1024
_MAX_PROVIDERS_CONFIGURED = 64


def _default_builtin_configs() -> List[ProviderConfig]:
    """Default built-in presets for initial startup."""
    configs: List[ProviderConfig] = []
    for kind, contract in _BUILTIN_CONTRACTS.items():
        configs.append(
            ProviderConfig(
                provider_id=kind,
                name=_BUILTIN_PROVIDER_LABELS.get(kind, kind),
                base_url=contract.base_url,
                api_format=PROTOCOL_CHAT_COMPLETIONS,
                is_builtin=True,
                builtin_kind=kind,
                tls_signature_blocked=contract.tls_signature_blocked,
            )
        )
    return configs


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
    schema (fails closed by raising ProviderConnectionError).

    Automatically migrates away any legacy auto-seeded built-in records
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

    if not isinstance(data, Mapping) or data.get("schema_version") != PROVIDER_CONFIG_SCHEMA_VERSION:
        raise ProviderConnectionError("provider configuration has an invalid schema version")
    providers_raw = data.get("providers")
    if not isinstance(providers_raw, list):
        raise ProviderConnectionError("provider configuration payload is invalid")

    configs: List[ProviderConfig] = []
    seen_ids = set()
    has_legacy_builtins = False
    legacy_builtin_ids: List[str] = []

    for item in providers_raw:
        if not isinstance(item, Mapping):
            raise ProviderConnectionError("provider configuration entry is invalid")
        cfg = ProviderConfig.from_dict(item)
        if cfg is None:
            raise ProviderConnectionError("provider configuration entry is invalid")
        if cfg.provider_id in seen_ids:
            continue
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
    """Atomically persist non-secret provider configurations."""
    path = provider_configurations_path()
    payload = json.dumps(
        {
            "schema_version": PROVIDER_CONFIG_SCHEMA_VERSION,
            "providers": [c.to_dict() for c in configs[:_MAX_PROVIDERS_CONFIGURED]],
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


def add_provider_config(
    name: str,
    base_url: str,
    api_format: str,
    *,
    api_key: Optional[str] = None,
    provider_id: Optional[str] = None,
    models: Tuple[DiscoveredProviderModel, ...] = (),
) -> ProviderConfig:
    """Add a new configured model provider."""
    if not name or not name.strip():
        raise ProviderConnectionError("Provider name is required")
    if not base_url or not base_url.strip():
        raise ProviderConnectionError("Base URL is required")
    try:
        clean_url = validate_and_canonicalize_url(base_url)
    except ProviderHttpError as exc:
        raise ProviderConnectionError(str(exc)) from None
    clean_format = api_format if api_format in _PROTOCOL_FAMILIES else PROTOCOL_CHAT_COMPLETIONS

    configs = load_provider_configurations()
    if provider_id is None:
        base_slug = _clean_slug(name)
        candidate = base_slug
        counter = 1
        while any(c.provider_id == candidate for c in configs):
            candidate = f"{base_slug}_{counter}"
            counter += 1
        pid = candidate
    else:
        pid = provider_id.strip()

    new_cfg = ProviderConfig(
        provider_id=pid,
        name=name.strip(),
        base_url=clean_url,
        api_format=clean_format,
        models=models,
        enabled=True,
        is_builtin=False,
    )
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
) -> ProviderConfig:
    """Update metadata for an existing provider."""
    configs = load_provider_configurations()
    existing = next((c for c in configs if c.provider_id == provider_id), None)
    if existing is None:
        raise ProviderConnectionError(f"Provider {provider_id!r} not found")

    new_name = name.strip() if name and name.strip() else existing.name
    if base_url is not None:
        if not base_url.strip():
            raise ProviderConnectionError("Base URL is required")
        try:
            new_url = validate_and_canonicalize_url(base_url)
        except ProviderHttpError as exc:
            raise ProviderConnectionError(str(exc)) from None
    else:
        new_url = existing.base_url
    new_format = (
        api_format
        if api_format in _PROTOCOL_FAMILIES
        else existing.api_format
    )
    new_models = models if models is not None else existing.models
    new_enabled = enabled if enabled is not None else existing.enabled

    updated_cfg = ProviderConfig(
        provider_id=existing.provider_id,
        name=new_name,
        base_url=new_url,
        api_format=new_format,
        models=new_models,
        last_refresh_utc=existing.last_refresh_utc,
        last_refresh_source=existing.last_refresh_source,
        enabled=new_enabled,
        is_builtin=existing.is_builtin,
        builtin_kind=existing.builtin_kind,
        tls_signature_blocked=existing.tls_signature_blocked,
    )
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
    """Add one model manually to a provider's catalog (fallback for no /models)."""
    cfg = get_provider_config(provider_id)
    if cfg is None:
        raise ProviderConnectionError(f"Provider {provider_id!r} not found")
    mid = model_id.strip()
    if not mid:
        raise ProviderConnectionError("Model ID is required")
    from agentic_debugger.application.model_providers import format_model_display_name

    disp = display_name.strip() if display_name and display_name.strip() else format_model_display_name(mid)
    proto = protocol if protocol in _PROTOCOL_FAMILIES else cfg.api_format
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
    contract = _BUILTIN_CONTRACTS.get(kind)
    if contract is not None:
        return contract.session_env_var
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", kind.upper())
    return f"AGENTIC_DEBUGGER_PROVIDER_{normalized}_API_KEY"


def credential_source_for(kind: str) -> Optional[str]:
    """Which credential source the direct route can use right now."""
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        return None
    if is_provider_quarantined(kind):
        # Indeterminate credential/config association: never select a source.
        return None
    if has_secure_credential(kind):
        return CREDENTIAL_SOURCE_SAVED
    if has_session_key(kind):
        return CREDENTIAL_SOURCE_SESSION_KEY
    session_var = _session_env_var_for(kind)
    if _credential_is_usable(os.environ.get(session_var)):
        return CREDENTIAL_SOURCE_SESSION_KEY

    contract = _BUILTIN_CONTRACTS.get(kind)
    if contract is not None:
        if contract.env_var and _credential_is_usable(os.environ.get(contract.env_var)):
            return CREDENTIAL_SOURCE_ENVIRONMENT
        if contract.auth_store_consumable and _read_opencode_auth_store_key(
            opencode_auth_store_path()
        ):
            return CREDENTIAL_SOURCE_CLI_AUTH_STORE
    return None


def resolve_runtime_credential(kind: str) -> Optional[str]:
    """The credential value for one direct-API request (runtime only)."""
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        return None
    if is_provider_quarantined(kind):
        return None

    session_var = _session_env_var_for(kind)
    forwarded_value = os.environ.get(session_var)
    if _credential_is_usable(forwarded_value):
        return forwarded_value.strip()

    saved_value = load_secure_credential(kind)
    if saved_value and _credential_is_usable(saved_value):
        return saved_value.strip()

    session_value = peek_session_key(kind)
    if session_value and _credential_is_usable(session_value):
        return session_value.strip()

    contract = _BUILTIN_CONTRACTS.get(kind)
    if contract is not None:
        if contract.env_var:
            env_value = os.environ.get(contract.env_var)
            if _credential_is_usable(env_value):
                return env_value.strip()
        if contract.auth_store_consumable:
            return _read_opencode_auth_store_key(opencode_auth_store_path())
    return None


def provider_environment_variable(kind: str) -> Optional[str]:
    contract = _BUILTIN_CONTRACTS.get(kind)
    return contract.env_var if contract is not None else None


def provider_session_credential_environment(
    kind: str,
) -> Optional[Mapping[str, str]]:
    """Private UI-to-worker credential hop (exactly one variable)."""
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
    return None


def provider_transport_credential_environment(
    kind: str,
) -> Optional[Mapping[str, str]]:
    """Child environment forwarding for the direct adapter."""
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        return None
    if is_provider_quarantined(kind):
        return None
    contract = _BUILTIN_CONTRACTS.get(kind)
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
        env_value = os.environ.get(contract.env_var)
        if _credential_is_usable(env_value):
            return {contract.env_var: env_value.strip()}
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
    """Deterministic protocol family for one provider model, or None."""
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    if kind == "opencode_go":
        return resolve_opencode_go_protocol(model_id)
    if kind == "commandcode_goat":
        return resolve_commandcode_protocol(model_id)
    for m in cfg.models:
        if m.model_id == model_id and m.protocol:
            return m.protocol
    return cfg.api_format


def provider_api_model_id(kind: str, model_id: str) -> str:
    """Exact model identity sent to a provider's direct API."""
    if type(model_id) is not str or not model_id.strip():
        raise ProviderConnectionError("provider model id is missing")
    value = model_id.strip()
    if kind == "opencode_go" and value.startswith(_OPENCODE_GO_MODEL_PREFIX):
        value = value[len(_OPENCODE_GO_MODEL_PREFIX):]
    return value


def inference_path_for(kind: str, protocol: str) -> str:
    contract = _BUILTIN_CONTRACTS.get(kind)
    if contract is not None:
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


def provider_base_url(kind: str) -> str:
    cfg = get_provider_config(kind)
    if cfg is not None and cfg.base_url:
        return cfg.base_url
    raise ProviderConnectionError(f"provider {kind!r} is not configured")


def provider_tls_signature_blocked(kind: str) -> bool:
    contract = _BUILTIN_CONTRACTS.get(kind)
    if contract is not None:
        return contract.tls_signature_blocked
    cfg = get_provider_config(kind)
    if cfg is not None:
        return cfg.tls_signature_blocked
    return False


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
        pattern = _BUILTIN_CONTRACTS[kind].catalog_model_id_pattern if kind in _BUILTIN_CONTRACTS else r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}$"
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
    pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}$", re.IGNORECASE)
    if kind in _BUILTIN_CONTRACTS:
        pattern = re.compile(_BUILTIN_CONTRACTS[kind].catalog_model_id_pattern)

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
    cfg = get_provider_config(kind)
    if cfg is None:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")

    contract = _BUILTIN_CONTRACTS.get(kind)
    source = credential_source_for(kind)
    model_count, last_refresh, refresh_source, stale, cached = _cached_status_fields(kind)

    label = cfg.name
    base_url_desc = describe_url(cfg.base_url)
    api_format = cfg.api_format
    enabled = cfg.enabled
    is_builtin = cfg.is_builtin
    if not cached and cfg.models:
        cached = cfg.models
        model_count = len(cfg.models)
        last_refresh = cfg.last_refresh_utc
        refresh_source = cfg.last_refresh_source

    message: Optional[str] = None
    if source is None:
        if is_provider_quarantined(kind):
            message = "Credential state requires recovery. Edit provider and save an API key again."
        elif contract is not None and contract.env_var:
            message = (
                f"Not connected — direct API needs {contract.env_var} or an API key "
                "(edit provider to add an API key)"
            )
        else:
            message = "Not connected — no usable credential source found (edit provider to add an API key)"

    return ProviderConnectionStatus(
        kind=kind,
        label=label,
        base_url=base_url_desc,
        connected=source is not None,
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
    """Fetch, normalize, cache, and return one live catalog snapshot."""
    if is_provider_quarantined(kind):
        raise ProviderConnectionError(
            "Credential state requires recovery. Edit provider and save an API key again."
        )
    cfg = get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")

    contract = _BUILTIN_CONTRACTS.get(kind)
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
