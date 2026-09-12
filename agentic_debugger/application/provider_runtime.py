"""Provider runtime profiles: the single owner of provider-specific wire behavior.

One authoritative layer describes, per transport profile, the runtime facts
that generic OpenAI-compatible execution does not need but specific
providers require:

- authentication strategy (delegated to the configured ``auth_mode``);
- catalog strategy (delegated to the configured ``catalog_mode``);
- default protocol (delegated to the configured ``api_format``);
- model-specific protocol/endpoint routing (the OpenCode Go documented
  table and the CommandCode routing rule live here as pure data/rules);
- static request metadata (the Agentic Debugger ``User-Agent`` for
  providers that require an identifiable coding-agent client);
- session-scoped request metadata (the stable ``x-opencode-session``
  identity for OpenCode Go inference);
- provider-specific transport constraints (delegated to the existing
  endpoint-contract authority; this module never duplicates them).

Generic providers never need a custom profile: when the transport profile
is ``generic`` this module yields empty extra headers and no session
requirement, so standard Bearer/``/models``/uniform-protocol behavior is
untouched.

Leaf discipline: this module imports only the immutable vocabulary
(``provider_identity``) at top level.  Live configuration is consulted
through lazy ``provider_config`` reads inside functions so the static
import DAG stays acyclic and no second transport/runtime authority is
introduced.  HTTP execution stays in ``provider_http``; credential egress
stays in the vault/transport boundary; this module only *describes* the
extra wire facts those boundaries must send.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from agentic_debugger.application.provider_identity import (
    PROTOCOL_CHAT_COMPLETIONS,
    PROTOCOL_MESSAGES,
    PROTOCOL_RESPONSES,
    TRANSPORT_COMMANDCODE_GOAT,
    TRANSPORT_GENERIC,
    TRANSPORT_MODES,
    TRANSPORT_OLLAMA_CLOUD,
    TRANSPORT_OPENCODE_GO,
)

__all__ = [
    "AGENTIC_DEBUGGER_USER_AGENT",
    "OPENCODE_GO_MODEL_PREFIX",
    "OPENCODE_GO_MODEL_PROTOCOLS",
    "OPENCODE_SESSION_ENV_VAR",
    "OPENCODE_SESSION_HEADER",
    "TRANSPORT_SESSION_ENV_VAR",
    "USER_AGENT_HEADER",
    "RuntimeProfile",
    "build_inference_headers",
    "extra_inference_headers",
    "find_explicit_model_protocol",
    "is_valid_transport_session_id",
    "new_transport_session_id",
    "normalize_opencode_model_key",
    "resolve_commandcode_protocol",
    "resolve_opencode_go_protocol",
    "resolve_profile_model_protocol",
    "runtime_profile_for_config",
    "runtime_profile_for_kind",
    "runtime_profile_for_transport",
    "session_request_headers",
    "static_request_headers",
    "transport_session_environment",
]

#: Stable non-secret session header required by OpenCode Go inference.
OPENCODE_SESSION_HEADER = "x-opencode-session"

#: Standard User-Agent header name.
USER_AGENT_HEADER = "User-Agent"

#: Identifiable coding-agent client string sent on OpenCode Go requests.
#: Deterministic, version-pinned, and credential-free.
AGENTIC_DEBUGGER_USER_AGENT = "AgenticDebugger/0.1.0"

#: Generic non-secret transport/session identity channel consumed by
#: direct-API adapter children.  One variable for every provider; the
#: adapter emits it as a wire header only when the provider's runtime
#: profile requires it, so unrelated providers never receive
#: OpenCode-only headers.
TRANSPORT_SESSION_ENV_VAR = "AGENTIC_DEBUGGER_MODEL_SESSION_ID"

#: Backwards-compatible provider-scoped alias for the same session value.
#: Retained so operator diagnostics can distinguish the OpenCode session
#: channel; the generic variable above remains canonical.
OPENCODE_SESSION_ENV_VAR = "AGENTIC_DEBUGGER_OPENCODE_SESSION_ID"

#: Model-id prefix used by the OpenCode Go catalog/CLI surface.
OPENCODE_GO_MODEL_PREFIX = "opencode-go/"

#: Authoritative OpenCode Go model -> protocol table (single source).
#:
#: Each entry is provider-contract evidence for which protocol family the
#: named model serves under (``/responses`` vs ``/chat/completions`` vs
#: ``/messages``).  Unknown ids resolve to ``None`` (discovered but not
#: runnable) so callers fail closed instead of misrouting.  An explicit
#: per-model protocol stored on the provider configuration always wins
#: over this table (see :func:`find_explicit_model_protocol`).
OPENCODE_GO_MODEL_PROTOCOLS: Mapping[str, str] = {
    # /responses (OpenAI Responses family)
    "grok-4.6": PROTOCOL_RESPONSES,
    "gpt-5.6-luna": PROTOCOL_RESPONSES,
    "muse-spark-1.2-contributor": PROTOCOL_RESPONSES,
    "muse-spark-1.3-contributor": PROTOCOL_RESPONSES,
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
    "deepseek-v4.1-flash": PROTOCOL_CHAT_COMPLETIONS,
    "deepseek-v4-pro": PROTOCOL_CHAT_COMPLETIONS,
    "deepseek-v4-flash": PROTOCOL_CHAT_COMPLETIONS,
    "deepseek-v4-flash-vision-exp": PROTOCOL_CHAT_COMPLETIONS,
    "mimo-v2.5": PROTOCOL_CHAT_COMPLETIONS,
    "mimo-v2.5-pro": PROTOCOL_CHAT_COMPLETIONS,
    "hy4-preview": PROTOCOL_CHAT_COMPLETIONS,
    "hy3": PROTOCOL_CHAT_COMPLETIONS,
}

_TRANSPORT_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Operational session identities may also arrive as the product's own
# session ids (``sess-<utc>-<rand>``): bounded, non-secret, and stable per
# Agentic Debugger session.  Both shapes are accepted on the wire; fresh
# identities are always minted as 32-hex.
_PRODUCT_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_MAX_HEADER_VALUE_CHARS = 512


def normalize_opencode_model_key(model_id: str) -> str:
    """Strip the ``opencode-go/`` prefix and whitespace (pure)."""
    text = model_id.strip() if isinstance(model_id, str) else ""
    if text.startswith(OPENCODE_GO_MODEL_PREFIX):
        text = text[len(OPENCODE_GO_MODEL_PREFIX):]
    return text.strip()


def resolve_opencode_go_protocol(model_id: str) -> Optional[str]:
    """Documented OpenCode Go protocol for one model id, or ``None``.

    Pure table lookup (prefix-tolerant).  ``None`` means discovered but
    not runnable: the caller must expose the model as unresolved with an
    actionable reason and must not silently route it through a default.
    """
    if type(model_id) is not str or not model_id:
        return None
    return OPENCODE_GO_MODEL_PROTOCOLS.get(normalize_opencode_model_key(model_id))


def resolve_commandcode_protocol(model_id: str) -> Optional[str]:
    """CommandCode routing rule (pure, prefix-based)."""
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


def find_explicit_model_protocol(cfg: Any, model_id: str) -> Optional[str]:
    """Explicit per-model protocol stored on the configuration, if any.

    An operator-supplied protocol override always wins over the
    documented table.  Matches the exact id first, then the
    prefix-stripped OpenCode variant in both directions so
    ``opencode-go/x`` and ``x`` forms agree.
    """
    if cfg is None or type(model_id) is not str or not model_id.strip():
        return None
    wanted = model_id.strip()
    candidates = {wanted}
    stripped = normalize_opencode_model_key(wanted)
    candidates.add(stripped)
    candidates.add(OPENCODE_GO_MODEL_PREFIX + stripped)
    try:
        models = getattr(cfg, "models", ())
    except Exception:
        return None
    try:
        for entry in models or ():
            entry_id = getattr(entry, "model_id", None)
            entry_proto = getattr(entry, "protocol", None)
            if type(entry_id) is not str or entry_proto is None:
                continue
            if entry_id.strip() in candidates and type(entry_proto) is str:
                return entry_proto
    except Exception:
        return None
    return None


def resolve_profile_model_protocol(
    transport_profile: str,
    model_id: str,
    explicit_protocol: Optional[str] = None,
) -> Optional[str]:
    """Model-aware protocol for one transport profile (pure).

    Explicit per-model overrides win; historical profiles then apply
    their documented rule; generic profiles have no model-specific rule
    (the caller falls back to the provider default).
    """
    if type(explicit_protocol) is str and explicit_protocol:
        return explicit_protocol
    if transport_profile == TRANSPORT_OPENCODE_GO:
        return resolve_opencode_go_protocol(model_id)
    if transport_profile == TRANSPORT_COMMANDCODE_GOAT:
        return resolve_commandcode_protocol(model_id)
    return None


@dataclass(frozen=True)
class RuntimeProfile:
    """Provider-specific wire behavior for one transport profile."""

    transport_profile: str
    requires_session_header: bool = False
    session_header_name: Optional[str] = None
    user_agent: Optional[str] = None

    @property
    def session_env_vars(self) -> tuple:
        if not self.requires_session_header:
            return ()
        return (TRANSPORT_SESSION_ENV_VAR, OPENCODE_SESSION_ENV_VAR)


def runtime_profile_for_transport(transport_profile: str) -> RuntimeProfile:
    """Runtime profile for one transport-profile value (pure, never raises)."""
    if transport_profile == TRANSPORT_OPENCODE_GO:
        return RuntimeProfile(
            transport_profile=transport_profile,
            requires_session_header=True,
            session_header_name=OPENCODE_SESSION_HEADER,
            user_agent=AGENTIC_DEBUGGER_USER_AGENT,
        )
    if type(transport_profile) is str and transport_profile in TRANSPORT_MODES:
        return RuntimeProfile(transport_profile=transport_profile)
    return RuntimeProfile(transport_profile=TRANSPORT_GENERIC)


def runtime_profile_for_config(cfg: Any) -> RuntimeProfile:
    """Runtime profile for one provider configuration snapshot (pure)."""
    try:
        profile = getattr(cfg, "transport_profile", TRANSPORT_GENERIC)
    except Exception:
        profile = TRANSPORT_GENERIC
    if type(profile) is not str or profile not in TRANSPORT_MODES:
        profile = TRANSPORT_GENERIC
    # Historical constants that never appear as configured profiles still
    # map safely to generic (no extra wire behavior).
    if profile not in (TRANSPORT_GENERIC, TRANSPORT_OLLAMA_CLOUD,
                       TRANSPORT_OPENCODE_GO, TRANSPORT_COMMANDCODE_GOAT):
        profile = TRANSPORT_GENERIC
    return runtime_profile_for_transport(profile)


def runtime_profile_for_kind(kind: str) -> RuntimeProfile:
    """Runtime profile for one provider id (explicit config, never ID-guess).

    Configured providers use their explicit transport profile.  Only when
    the provider is not configured does the historical identifier serve
    as an offline fallback (mirroring the transport-profile authority).
    """
    try:
        from agentic_debugger.application import provider_config as _config
    except Exception:
        return runtime_profile_for_transport(TRANSPORT_GENERIC)
    try:
        cfg = _config.get_provider_config(kind)
    except Exception:
        cfg = None
    if cfg is not None:
        return runtime_profile_for_config(cfg)
    if type(kind) is str and kind == TRANSPORT_OPENCODE_GO:
        return runtime_profile_for_transport(TRANSPORT_OPENCODE_GO)
    return runtime_profile_for_transport(TRANSPORT_GENERIC)


def new_transport_session_id() -> str:
    """One fresh non-secret transport/session identity (32 lowercase hex)."""
    return uuid.uuid4().hex


def is_valid_transport_session_id(value: Any) -> bool:
    """Whether a value is a well-formed transport session identity.

    Accepts freshly minted 32-hex identities and the product's own
    ``sess-...`` session ids (both bounded, non-secret, and stable per
    session).  Credential-shaped values are never valid here.
    """
    if type(value) is not str or not value:
        return False
    if _TRANSPORT_SESSION_ID_RE.fullmatch(value) is not None:
        return True
    if len(value) > 128:
        return False
    if _PRODUCT_SESSION_ID_RE.fullmatch(value) is None:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    return True


def transport_session_environment(session_id: str) -> Dict[str, str]:
    """Child-environment mapping carrying one session identity (non-secret)."""
    if not is_valid_transport_session_id(session_id):
        raise ValueError("transport session id is invalid")
    return {
        TRANSPORT_SESSION_ENV_VAR: session_id,
        OPENCODE_SESSION_ENV_VAR: session_id,
    }


def _checked_header_value(value: str) -> str:
    if type(value) is not str or not value:
        raise ValueError("header value is missing")
    if len(value) > _MAX_HEADER_VALUE_CHARS:
        raise ValueError("header value is oversized")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("header value contains control characters")
    return value


def static_request_headers(profile: RuntimeProfile) -> Dict[str, str]:
    """Static (non-session) extra headers for one runtime profile."""
    if not isinstance(profile, RuntimeProfile):
        return {}
    if profile.transport_profile == TRANSPORT_OPENCODE_GO and profile.user_agent:
        try:
            return {USER_AGENT_HEADER: _checked_header_value(profile.user_agent)}
        except ValueError:
            return {}
    return {}


def session_request_headers(
    profile: RuntimeProfile, session_id: Optional[str]
) -> Dict[str, str]:
    """Session-scoped extra headers for one runtime profile.

    Generic profiles yield no headers regardless of input.  Profiles that
    require a session header yield exactly that header when the session
    id is well-formed, and raise otherwise (fail closed: a required
    session identity is never silently omitted).
    """
    if not isinstance(profile, RuntimeProfile):
        return {}
    if not profile.requires_session_header:
        return {}
    if not is_valid_transport_session_id(session_id):
        raise ValueError(
            "a stable transport session identity is required for this provider"
        )
    assert isinstance(session_id, str)
    name = profile.session_header_name or OPENCODE_SESSION_HEADER
    if _HEADER_NAME_RE.fullmatch(name) is None:
        raise ValueError("session header name is invalid")
    return {name: _checked_header_value(session_id)}


def build_inference_headers(
    profile: RuntimeProfile, session_id: Optional[str]
) -> Dict[str, str]:
    """All extra inference headers for one profile+session (fail closed)."""
    headers: Dict[str, str] = {}
    headers.update(static_request_headers(profile))
    headers.update(session_request_headers(profile, session_id))
    return headers


def extra_inference_headers(
    kind_or_cfg: Any, session_id: Optional[str] = None
) -> Dict[str, str]:
    """Convenience: extra inference headers for a provider id or config."""
    if isinstance(kind_or_cfg, str):
        profile = runtime_profile_for_kind(kind_or_cfg)
    else:
        profile = runtime_profile_for_config(kind_or_cfg)
    if not profile.requires_session_header:
        # Generic path never demands a session identity and never emits
        # provider-specific headers, even when a session id happens to be
        # present in the environment.
        return dict(static_request_headers(profile))
    return build_inference_headers(profile, session_id)
