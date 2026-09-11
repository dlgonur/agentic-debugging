"""Provider vocabulary, historical endpoint contracts, and the auth/protocol matrix.

Single responsibility: the IMMUTABLE provider-connection vocabulary -- protocol
families, authentication modes, catalog modes, transport profiles, display
labels, the explicit historical endpoint contracts (``opencode_go`` /
``commandcode_goat`` / ``ollama_cloud``), and the fail-closed
auth/protocol combination matrix. Everything here is a constant, a pure
selector over an explicit profile, or the shared :class:`ProviderConnectionError`.

Leaf module: no sibling provider_* imports at top level. The one upward read
(:func:`_profile_for_kind` consulting live configuration) is a documented lazy
edge inside the function so the static import DAG stays acyclic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional
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
    # Decomposition v1 (lazy edge E2): live configuration lives in
    # provider_config, above this leaf in the static DAG (was a plain
    # module-global lookup).
    from agentic_debugger.application import provider_config as _provider_config
    try:
        cfg = _provider_config.get_provider_config(kind)
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
