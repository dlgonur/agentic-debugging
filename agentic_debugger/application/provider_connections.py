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

- **Discovery & Management** (this domain) is catalog configuration and GET /models.
  It performs no generation inference and confers no scientific status.
- **General-runtime execution** resolves through
  :mod:`agentic_debugger.application.model_providers`, which selects the
  explicit route (``direct_api`` / ``legacy_cli``) per model.
- **Capability Ladder qualification** is untouched by this domain: adding a
  generic provider or Ollama connection never confers scientific qualification,
  while any runtime-executable configured model remains executable across the
  Capability Ladder under Task 42.

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

Decomposition v1 (Task 47): this module is the thin compatibility facade over
the provider-connection authorities. Canonical implementations live in:

- :mod:`agentic_debugger.application.provider_identity` -- immutable
  vocabulary, historical endpoint contracts, auth/protocol matrix;
- :mod:`agentic_debugger.application.provider_config` -- durable registry
  models, validation, paths, persistence, quarantine state;
- :mod:`agentic_debugger.application.provider_credentials` -- credential
  backends, discovery ladder, endpoint binding, forwarding environments;
- :mod:`agentic_debugger.application.provider_protocols` -- model protocol
  resolution and endpoint-path authority;
- :mod:`agentic_debugger.application.provider_catalog` -- catalog snapshots,
  cache, presence-only status, live refresh/probe;
- :mod:`agentic_debugger.application.provider_management` -- registry
  mutation, commit transactions, migration, recovery.

Every name previously importable from this module resolves to the identical
canonical object (``facade.X is canonical.X``); ``__all__`` is byte-identical
to the pre-decomposition surface. See ``_ai-review/`` architecture notes.
"""

from __future__ import annotations

from agentic_debugger.application.provider_http import (
    ProviderHttpError,
    describe_url,
    request_json,
    sanitize_text,
    validate_and_canonicalize_url,
)
from agentic_debugger.application.provider_identity import (
    AUTH_ANTHROPIC,
    AUTH_BEARER,
    AUTH_DISPLAY_LABELS,
    AUTH_MODES,
    AUTH_NONE,
    CATALOG_DISABLED,
    CATALOG_MODES,
    CATALOG_OPENAI,
    DIRECT_API_PROVIDER_KINDS,
    ENDPOINT_CONTRACT_DISPLAY_LABELS,
    HISTORICAL_TRANSPORT_PROFILES,
    PROTOCOL_CHAT_COMPLETIONS,
    PROTOCOL_DISPLAY_LABELS,
    PROTOCOL_MESSAGES,
    PROTOCOL_RESPONSES,
    TRANSPORT_COMMANDCODE_GOAT,
    TRANSPORT_DISPLAY_LABELS,
    TRANSPORT_GENERIC,
    TRANSPORT_MODES,
    TRANSPORT_OLLAMA_CLOUD,
    TRANSPORT_OPENCODE_GO,
    ProviderConnectionError,
    _BuiltinProviderContract,
    _BUILTIN_CONTRACTS,
    _CONTRACTS,
    _PROTOCOL_FAMILIES,
    _contract_for_config,
    _contract_for_kind,
    _contract_for_profile,
    _profile_for_kind,
    validate_auth_protocol_combination,
)
from agentic_debugger.application.provider_config import (
    PROVIDER_CONFIG_SCHEMA_VERSION,
    PROVIDER_CONFIG_SCHEMA_VERSIONS,
    DiscoveredProviderModel,
    ProviderConfig,
    _load_quarantined_providers,
    _MAX_CONFIG_FILE_BYTES,
    _MAX_DISPLAY_NAME_CHARS,
    _MAX_MODEL_ID_CHARS,
    _MAX_MODELS_PER_PROVIDER,
    _MAX_PROVIDER_ID_CHARS,
    _MAX_PROVIDER_NAME_CHARS,
    _MAX_PROVIDERS_CONFIGURED,
    _MAX_QUARANTINE_FILE_BYTES,
    _MODEL_ID_PATTERN,
    _PROVIDER_CONFIG_SCHEMA_V1,
    _PROVIDER_ID_PATTERN,
    _QUARANTINED_PROVIDERS,
    _QUARANTINE_SCHEMA_VERSION,
    _read_quarantine_file,
    _valid_utc,
    _write_quarantine_state,
    clear_provider_quarantine,
    describe_transport_gap,
    get_provider_config,
    is_known_provider,
    is_provider_quarantined,
    is_valid_provider_id,
    list_configured_providers,
    load_provider_configurations,
    provider_auth_mode,
    provider_catalog_mode,
    provider_configurations_path,
    provider_quarantine_path,
    provider_transport_profile,
    quarantined_providers,
    quarantine_provider,
    save_provider_configurations,
    validate_provider_config_for_write,
)
from agentic_debugger.application.provider_credentials import (
    CREDENTIAL_SOURCE_CLI_AUTH_STORE,
    CREDENTIAL_SOURCE_ENVIRONMENT,
    CREDENTIAL_SOURCE_SAVED,
    CREDENTIAL_SOURCE_SESSION_KEY,
    PROVIDER_TRANSPORT_NETWORK_ENV_ALLOWLIST,
    _bounded_network_env_value,
    _canonical_base_for,
    _credential_is_usable,
    _endpoint_binding_valid,
    _forwarded_matches_explicit_credential,
    _MAX_AUTH_STORE_BYTES,
    _MAX_SESSION_KEY_CHARS,
    _PROVIDER_CLI_AUTH_ENV_NAME,
    _PROVIDER_CREDENTIAL_SOURCE_LABELS,
    _PROVIDER_TRANSPORT_NETWORK_ENV_MAX_CHARS,
    _read_opencode_auth_store_key,
    _SESSION_KEYS,
    _session_env_var_for,
    _WIN_CRED_PREFIX,
    _wincred_delete,
    _wincred_read,
    _wincred_write,
    clear_all_session_keys,
    clear_session_key,
    credential_slot_name,
    credential_source_for,
    credential_value_is_usable,
    delete_secure_credential,
    has_secure_credential,
    has_session_key,
    load_secure_credential,
    opencode_auth_store_path,
    peek_session_key,
    provider_authority_environment_names,
    provider_endpoint_binding_valid,
    provider_environment_variable,
    provider_legacy_cli_auth_file,
    provider_session_credential_authority_variable,
    provider_session_credential_environment,
    provider_session_credential_variable,
    provider_transport_credential_environment,
    provider_transport_network_environment,
    resolve_runtime_credential,
    save_secure_credential,
    set_session_key,
)
from agentic_debugger.application.provider_protocols import (
    _catalog_pattern_for,
    _inference_path_for_profile,
    _OPENCODE_GO_DOCUMENTED_PROTOCOLS,
    _OPENCODE_GO_MODEL_PREFIX,
    effective_model_protocol,
    effective_model_protocol_for_config,
    inference_path_for,
    inference_path_for_config,
    is_protocol_executable,
    is_protocol_executable_for_config,
    protocol_blocker_reason,
    protocol_blocker_reason_for_config,
    provider_api_model_id,
    provider_api_model_id_for_config,
    provider_base_url,
    provider_tls_signature_blocked,
    resolve_commandcode_protocol,
    resolve_model_protocol,
    resolve_model_protocol_for_config,
    resolve_opencode_go_protocol,
)
from agentic_debugger.application.provider_catalog import (
    _cached_status_fields,
    _CACHE_SCHEMA_VERSION,
    _CATALOG_FETCH_TIMEOUT_SECONDS,
    _CATALOG_MAX_RESPONSE_BYTES,
    _CATALOG_STALE_AFTER,
    _display_name,
    _MAX_CACHE_FILE_BYTES,
    _MAX_CATALOG_MODELS,
    _normalize_catalog,
    _purge_legacy_cached_catalog,
    _SNAPSHOT_SOURCE_LIVE,
    _utc_now,
    catalog_cache_path,
    connection_statuses,
    delete_cached_catalog,
    load_cached_catalog,
    ProviderCatalogSnapshot,
    ProviderConnectionStatus,
    provider_connection_status,
    refresh_provider_catalog,
    save_cached_catalog,
    test_provider_connection,
)
from agentic_debugger.application.provider_management import (
    _check_effective_models,
    _clean_slug,
    _generate_provider_id,
    _migrate_legacy_builtin_records,
    _purge_provider_state_strict,
    add_manual_model,
    add_provider_config,
    commit_provider_and_credential,
    delete_provider_config,
    update_provider_config,
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
    "commit_provider_and_credential",
    "connection_statuses",
    "credential_slot_name",
    "credential_source_for",
    "credential_value_is_usable",
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
    "provider_endpoint_binding_valid",
    "provider_session_credential_environment",
    "provider_session_credential_authority_variable",
    "provider_session_credential_variable",
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
