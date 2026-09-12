"""Model protocol resolution and endpoint-path authority.

Single responsibility: WHICH protocol a provider model executes under and
WHERE the request goes -- the historical per-model resolvers (OpenCode Go
documented table, CommandCode routing rule), global and snapshot-pure
protocol resolution, effective-protocol validation against the auth matrix
and transport capability, API model identities, inference paths,
blocker reasons, executability, and endpoint metadata (base URL, TLS
behavior, catalog ID pattern).

Cross-module reads resolve through the owning module (``_config`` /
``_identity``).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from agentic_debugger.application import provider_config as _config
from agentic_debugger.application import provider_identity as _identity
from agentic_debugger.application import provider_runtime as _runtime
from agentic_debugger.application.provider_identity import (
    HISTORICAL_TRANSPORT_PROFILES,
    PROTOCOL_CHAT_COMPLETIONS,
    PROTOCOL_MESSAGES,
    PROTOCOL_RESPONSES,
    TRANSPORT_COMMANDCODE_GOAT,
    TRANSPORT_GENERIC,
    TRANSPORT_OPENCODE_GO,
    ProviderConnectionError,
    _BUILTIN_CONTRACTS,
    _PROTOCOL_FAMILIES,
)
# -- protocol resolution ------------------------------------------------------

#: Single-sourced OpenCode Go documented table.  The runtime-profile layer
#: owns the mapping; this alias preserves the historical import surface so
#: existing seams keep resolving to the identical object.
_OPENCODE_GO_DOCUMENTED_PROTOCOLS: Mapping[str, str] = _runtime.OPENCODE_GO_MODEL_PROTOCOLS

_OPENCODE_GO_MODEL_PREFIX = _runtime.OPENCODE_GO_MODEL_PREFIX


def resolve_opencode_go_protocol(model_id: str) -> Optional[str]:
    return _runtime.resolve_opencode_go_protocol(model_id)


def resolve_commandcode_protocol(model_id: str) -> Optional[str]:
    return _runtime.resolve_commandcode_protocol(model_id)


def resolve_model_protocol(kind: str, model_id: str) -> Optional[str]:
    """Deterministic protocol family for one provider model, or None.

    Historical per-model resolvers apply only when the provider
    configuration explicitly carries the corresponding historical
    transport profile — never from the technical ID alone.  A generic
    provider (even one identified ``opencode_go``) resolves through its
    configured models and provider default.

    An explicit per-model protocol stored on the configuration always
    wins over the documented historical table, so operators can route a
    newly discovered model (or correct a stale mapping) without a source
    change.  Unknown OpenCode Go models without an override resolve to
    ``None`` (discovered but not runnable) and must never be silently
    sent through the provider default.
    """
    cfg = _config.get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    if cfg.transport_profile == TRANSPORT_OPENCODE_GO:
        explicit = _runtime.find_explicit_model_protocol(cfg, model_id)
        if explicit is not None:
            return explicit
        return resolve_opencode_go_protocol(model_id)
    if cfg.transport_profile == TRANSPORT_COMMANDCODE_GOAT:
        explicit = _runtime.find_explicit_model_protocol(cfg, model_id)
        if explicit is not None:
            return explicit
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
    cfg = _config.get_provider_config(kind)
    if cfg is None or not cfg.enabled:
        raise ProviderConnectionError(f"provider {kind!r} is not configured")
    try:
        _identity.validate_auth_protocol_combination(cfg.auth_mode, protocol)
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


def resolve_model_protocol_for_config(cfg: Any, model_id: str) -> Optional[str]:
    """Snapshot-pure model protocol resolution (no global re-read).

    Repair 25: all authority-relevant config facts come from the passed
    ``cfg`` snapshot.  Global wrapper :func:`resolve_model_protocol` reads
    current config once and delegates here, so normal behavior is unchanged
    while snapshot resolution never observes a different authority.

    An explicit per-model protocol on the snapshot always wins over the
    documented historical table; unknown OpenCode Go models without an
    override resolve to ``None`` (discovered but not runnable).
    """
    if cfg is None or not getattr(cfg, "enabled", False):
        raise ProviderConnectionError(f"provider {getattr(cfg, 'provider_id', '?')!r} is not configured")
    if getattr(cfg, "transport_profile", None) == TRANSPORT_OPENCODE_GO:
        explicit = _runtime.find_explicit_model_protocol(cfg, model_id)
        if explicit is not None:
            return explicit
        return resolve_opencode_go_protocol(model_id)
    if getattr(cfg, "transport_profile", None) == TRANSPORT_COMMANDCODE_GOAT:
        explicit = _runtime.find_explicit_model_protocol(cfg, model_id)
        if explicit is not None:
            return explicit
        return resolve_commandcode_protocol(model_id)
    for m in getattr(cfg, "models", ()):
        if getattr(m, "model_id", None) == model_id and getattr(m, "protocol", None):
            return m.protocol
    return getattr(cfg, "api_format", None)


def effective_model_protocol_for_config(cfg: Any, model_id: str) -> str:
    """Snapshot-pure effective protocol (no global re-read).

    Repair 25: resolves via :func:`resolve_model_protocol_for_config` on the
    SAME snapshot, then validates auth/profile/inference-path against THAT
    snapshot only.
    """
    kind = getattr(cfg, "provider_id", "?")
    protocol = resolve_model_protocol_for_config(cfg, model_id)
    if protocol is None:
        raise ProviderConnectionError(
            f"provider {kind!r} model {model_id!r} has no resolved protocol"
        )
    try:
        _identity.validate_auth_protocol_combination(getattr(cfg, "auth_mode", None), protocol)
    except ProviderConnectionError as exc:
        raise ProviderConnectionError(
            f"provider {kind!r} model {model_id!r}: {exc}"
        ) from None
    try:
        _inference_path_for_profile(
            getattr(cfg, "transport_profile", TRANSPORT_GENERIC), protocol
        )
    except ProviderConnectionError as exc:
        raise ProviderConnectionError(
            f"provider {kind!r} model {model_id!r}: {exc}"
        ) from None
    return protocol


def provider_api_model_id_for_config(cfg: Any, model_id: str) -> str:
    """Snapshot-pure API model id (no global re-read)."""
    if type(model_id) is not str or not model_id.strip():
        raise ProviderConnectionError("provider model id is missing")
    value = model_id.strip()
    profile = getattr(cfg, "transport_profile", TRANSPORT_GENERIC)
    if profile == TRANSPORT_OPENCODE_GO and value.startswith(
        _OPENCODE_GO_MODEL_PREFIX
    ):
        value = value[len(_OPENCODE_GO_MODEL_PREFIX):]
    return value


def inference_path_for_config(cfg: Any, protocol: str) -> str:
    """Snapshot-pure inference path (no global re-read)."""
    return _inference_path_for_profile(
        getattr(cfg, "transport_profile", TRANSPORT_GENERIC), protocol
    )


def protocol_blocker_reason(kind: str, protocol: Optional[str]) -> Optional[str]:
    """Credential-safe reason a protocol is not executable, or ``None``.

    Single authority behind picker/doctor/status unavailability reasons:
    unknown protocols, auth-matrix violations, and transport-profile
    capability gaps each yield an actionable message; executable pairs
    yield ``None``.  Never raises.
    """
    try:
        cfg = _config.get_provider_config(kind)
    except Exception:
        return f"provider {kind!r} configuration is unavailable"
    if cfg is None or not cfg.enabled:
        return f"provider {kind!r} is not configured"
    if type(protocol) is not str or protocol not in _PROTOCOL_FAMILIES:
        return f"unknown protocol: {protocol!r}"
    try:
        _identity.validate_auth_protocol_combination(cfg.auth_mode, protocol)
    except ProviderConnectionError as exc:
        return str(exc)
    try:
        inference_path_for(kind, protocol)
    except ProviderConnectionError as exc:
        return str(exc)
    return None


def protocol_blocker_reason_for_config(cfg: Any, protocol: Optional[str]) -> Optional[str]:
    """Snapshot-pure protocol blocker reason (no global re-read)."""
    kind = getattr(cfg, "provider_id", "?")
    if cfg is None or not getattr(cfg, "enabled", False):
        return f"provider {kind!r} is not configured"
    if type(protocol) is not str or protocol not in _PROTOCOL_FAMILIES:
        return f"unknown protocol: {protocol!r}"
    try:
        _identity.validate_auth_protocol_combination(getattr(cfg, "auth_mode", None), protocol)
    except ProviderConnectionError as exc:
        return str(exc)
    try:
        _inference_path_for_profile(
            getattr(cfg, "transport_profile", TRANSPORT_GENERIC), protocol
        )
    except ProviderConnectionError as exc:
        return str(exc)
    return None


def is_protocol_executable_for_config(cfg: Any, protocol: Optional[str]) -> bool:
    """Snapshot-pure executability (no global re-read)."""
    return protocol_blocker_reason_for_config(cfg, protocol) is None


def is_protocol_executable(kind: str, protocol: Optional[str]) -> bool:
    """Whether one protocol is executable for one provider (never raises)."""
    return protocol_blocker_reason(kind, protocol) is None


def provider_api_model_id(kind: str, model_id: str) -> str:
    """Exact model identity sent to a provider's direct API."""
    if type(model_id) is not str or not model_id.strip():
        raise ProviderConnectionError("provider model id is missing")
    value = model_id.strip()
    if _identity._profile_for_kind(kind) == TRANSPORT_OPENCODE_GO and value.startswith(
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
    return _inference_path_for_profile(_identity._profile_for_kind(kind), protocol)


def provider_base_url(kind: str) -> str:
    cfg = _config.get_provider_config(kind)
    if cfg is not None and cfg.base_url:
        return cfg.base_url
    raise ProviderConnectionError(f"provider {kind!r} is not configured")


def provider_tls_signature_blocked(kind: str) -> bool:
    contract = _identity._contract_for_kind(kind)
    if contract is not None:
        return contract.tls_signature_blocked
    cfg = _config.get_provider_config(kind)
    if cfg is not None:
        return cfg.tls_signature_blocked
    return False


def _catalog_pattern_for(kind: str) -> str:
    """Model-ID pattern selected through the explicit profile authority."""
    contract = _identity._contract_for_kind(kind)
    if contract is not None:
        return contract.catalog_model_id_pattern
    return r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}$"

