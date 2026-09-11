"""Credential storage backends, discovery ladder, and forwarding authority.

Single responsibility: everything about WHERE a provider credential lives and
HOW it is resolved/forwarded -- process-local session keys, OS secure-store
backends (Windows Credential Manager with safe session-only fallback),
CLI-auth-store discovery, the credential-source ladder
(``saved`` / ``session_key`` / ``environment`` / ``cli_auth_store``),
canonical-endpoint binding for ambient sources, and the private
UI-to-worker and transport credential environments. Values never leave the
runtime boundary (names only in logs/diagnostics).

Cross-module reads resolve through the owning module (``_config`` /
``_identity``) so the test-isolation seam stays at the authority. One
preserved lazy edge: the vault-issued issuance authority comes from
:mod:`agentic_debugger.application.model_gateway` inside the function, exactly
as before (avoids a static cycle with the gateway layer).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from agentic_debugger.application import provider_config as _config
from agentic_debugger.application import provider_identity as _identity
from agentic_debugger.application.provider_config import ProviderConfig
from agentic_debugger.application.provider_http import validate_and_canonicalize_url
from agentic_debugger.application.provider_identity import (
    AUTH_NONE,
    DIRECT_API_PROVIDER_KINDS,
    TRANSPORT_GENERIC,
    TRANSPORT_OPENCODE_GO,
    ProviderConnectionError,
    _BUILTIN_CONTRACTS,
)
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
    if kind not in DIRECT_API_PROVIDER_KINDS and not _config.is_known_provider(kind):
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


def credential_slot_name(provider_id: str) -> str:
    """The durable vault slot NAME for one provider (safe metadata).

    This is the Windows Credential Manager target name under which the
    provider's credential is stored.  The name itself is non-secret safe
    metadata (it identifies the SLOT, never the credential value) and is
    the ``source_ref`` recorded by the V2-04 :class:`CredentialBinding`.
    """
    return _WIN_CRED_PREFIX + provider_id


def credential_value_is_usable(value: Any) -> bool:
    """Public presence/shape gate shared by every credential source."""
    return _credential_is_usable(value)


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
        cfg = _config.get_provider_config(kind)
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

# -- credential resolution ----------------------------------------------------

def _session_env_var_for(kind: str) -> str:
    contract = _identity._contract_for_kind(kind)
    if contract is not None:
        return contract.session_env_var
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", kind.upper())
    return f"AGENTIC_DEBUGGER_PROVIDER_{normalized}_API_KEY"


def provider_session_credential_variable(kind: str) -> str:
    """The single private credential CHANNEL variable for one provider.

    This variable is the vault-issued credential channel: the trusted
    UI→worker hop and the trusted model-adapter transport materialization
    both forward the winning credential VALUE under exactly this name, and
    the adapter child consumes ONLY this channel (V2-04 removed the
    adapter's ambient re-resolution).  The variable NAME is safe metadata;
    its value must never enter argv, journals, evidence, or diagnostics.
    """
    return _session_env_var_for(kind)


def provider_session_credential_authority_variable(kind: str) -> str:
    """The private ISSUANCE-AUTHORITY companion variable for one provider.

    Repair 22 (F5): whenever the trusted UI→worker hop forwards a
    credential VALUE, it also forwards the SAFE provider runtime identity
    under which that value was ISSUED in this companion variable (hex
    identity only — never secret material, never secret-derived).  The
    worker session boundary may pin the forwarded secret ONLY while this
    issuance authority still equals the current provider configuration;
    otherwise the channel is STALE and fails closed.  The name lives in
    the same Agentic Debugger control namespace the V2-01 execution
    authority structurally excludes from project/PDB/verifier children.
    """
    return _session_env_var_for(kind) + "_AUTHORITY"


def provider_endpoint_binding_valid(kind: str) -> bool:
    """Whether ambient canonical credentials remain bound to this endpoint.

    Public authority wrapper over the accepted canonical-endpoint binding
    rule: ambient environment / CLI-auth credentials are usable only while
    the configured Base URL still matches the historical contract
    endpoint.  Generic providers (no historical contract) are always
    considered valid (nothing is ambient-bound).
    """
    try:
        cfg = _config.get_provider_config(kind)
    except Exception:
        return False
    if cfg is None:
        return False
    return _endpoint_binding_valid(kind, cfg)


def _canonical_base_for(kind: str) -> Optional[str]:
    contract = _identity._contract_for_kind(kind)
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
    cfg = _config.get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        return None
    if cfg.auth_mode == AUTH_NONE:
        return None
    if _config.is_provider_quarantined(kind):
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

    contract = _identity._contract_for_config(cfg)
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
    cfg = _config.get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        return None
    if cfg.auth_mode == AUTH_NONE:
        return None
    if _config.is_provider_quarantined(kind):
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

    contract = _identity._contract_for_config(cfg)
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
    contract = _identity._contract_for_kind(kind)
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
        names.add(contract.session_env_var + "_AUTHORITY")
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
    if _config.is_provider_quarantined(kind):
        return None
    cfg = _config.get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        return None
    # Repair 22 (F5): every forwarded credential VALUE is accompanied by
    # the SAFE provider runtime identity under which it was issued, so
    # the worker can never stamp an old forwarded secret with a newer
    # configuration authority (no secret-derived hashes; hex identity of
    # safe configuration facts only).
    from agentic_debugger.application.model_gateway import provider_runtime_identity

    issuance_authority = provider_runtime_identity(cfg)
    session_var = _session_env_var_for(kind)
    authority_var = provider_session_credential_authority_variable(kind)
    secret = load_secure_credential(kind)
    if not secret or not _credential_is_usable(secret):
        secret = peek_session_key(kind)
    if secret and _credential_is_usable(secret):
        if issuance_authority is None:
            return None
        return {session_var: secret.strip(), authority_var: issuance_authority}
    contract = _identity._contract_for_config(cfg)
    if contract is not None and contract.auth_store_consumable:
        if not _endpoint_binding_valid(kind, cfg):
            return None
        try:
            cli_key = _read_opencode_auth_store_key(opencode_auth_store_path())
        except Exception:
            cli_key = None
        if _credential_is_usable(cli_key):
            if issuance_authority is None:
                return None
            assert isinstance(cli_key, str)
            return {session_var: cli_key.strip(), authority_var: issuance_authority}
    return None


def provider_transport_credential_environment(
    kind: str,
) -> Optional[Mapping[str, str]]:
    """Trusted model-channel credential materialization (one variable).

    V2-04 authority: the winning credential source is materialized under
    the provider's single private session credential variable — the ONLY
    channel the direct adapter consumes (the adapter no longer re-resolves
    ambient state).  Source rules are the accepted ladder verbatim:
    saved/session/forwarded session values are provider-identity-bound;
    ambient environment values are forwarded only while the endpoint
    binding remains valid; the winning CLI-auth credential is resolved
    once here and forwarded as a VALUE, so the child never rediscovers
    operator auth state or requires ``OPENCODE_CONFIG_DIR``.  No-auth,
    unconfigured, disabled, and quarantined providers forward nothing.
    The child transport starts from a minimal environment, so only what is
    forwarded here (plus the bounded network authority in the transport
    environment) can reach the adapter.  Values must never enter argv,
    journals, evidence, or diagnostics.
    """
    cfg = _config.get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        return None
    if cfg.auth_mode == AUTH_NONE:
        return None
    if _config.is_provider_quarantined(kind):
        return None
    contract = _identity._contract_for_config(cfg)
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
            return {session_var: env_value.strip()}
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

