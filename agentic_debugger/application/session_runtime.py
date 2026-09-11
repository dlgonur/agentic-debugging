"""V2-02 product session/runtime contracts (Local Project sessions).

This module is the explicit product-session launch authority vocabulary:

- :class:`AgentDefinition` — what the agent REQUESTS / is permitted to use
  (controller policy identity, requested provider/model logical identity,
  requested tool capabilities).  It never carries runtime-resolved provider
  route facts (route, protocol, endpoint, transport profile), credential
  values or bindings, catalog state, live status, or scientific
  qualification.  Those remain outside, owned by the current provider
  runtime (pre-ModelGateway) and the existing scientific authorities.
- :class:`~agentic_debugger.application.runtime_env_spec.ProjectRuntimeEnvironmentSpec`
  — the explicit Local Project runtime-environment ingress: platform
  essentials stay derived by the execution-environment/platform logic;
  everything else the project needs is declared there (explicit non-secret
  values, explicitly inherited variable NAMES, explicitly authorized
  project-secret binding NAMES).
- :class:`EffectiveSessionCapabilities` — the one computed session
  authority: ``AgentDefinition.allowed ∩ ExecutionEnvironment.available ∩
  task/product policy``, computed ONCE per session.  Consumers use the
  result; nobody recomputes the intersection.
- :class:`SessionLaunch` — the immutable session-start authority binding
  the session-start facts (task/scenario/source identity, agent
  definition, execution-environment input identity, project runtime spec,
  effective capabilities, provider/model request identity, budgets).
  :class:`~agentic_debugger.application.session.SessionSpec` remains the
  serialized Task-1 compatibility representation; ``SessionLaunch`` is the
  authoritative in-process launch object composed from it (plus scenario
  params and the session execution environment).  It is never
  deserialized: its serializable components round-trip individually.

Architecture (one-way dependency graph):

* :mod:`agentic_debugger.application.runtime_env_contracts` — error
  vocabulary, capability enum, environment-NAME identity/platform-essential
  contracts;
* :mod:`agentic_debugger.application.runtime_env_spec` — the project
  runtime-environment spec, worker transport, and one-shot
  materialization;
* this module — agent definition, capability computation, and the
  session-launch authority, plus the public import surface.

Secret-value lifetime (honest): a declared project-secret name resolves to
its value ONCE at session launch from the trusted launch environment
snapshot (:func:`materialize_project_runtime`); the resolved mapping lives
only in trusted session-process memory (the session
:class:`~agentic_debugger.application.execution_environment.ExecutionEnvironment`
and the explicit role mappings derived from it) and in the child-process
environments of the authorized project roles.  Values never enter
spec/history/journal/params/repr/fingerprints/diagnostics, never flow to
the model adapter or prompts, and never become provider credentials.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional

from agentic_debugger.application.events import validate_session_id
from agentic_debugger.application.runtime_env_contracts import (
    PLATFORM_ESSENTIAL_NAMES,
    CapabilityUnavailableError,
    ProjectRuntimeError,
    SessionCapability,
    SessionRuntimeError,
    _ALL_CAPABILITIES,
    _bounded_id,
    canonical_env_name,
    is_platform_essential_name,
    resolve_env_name_platform,
    validate_env_name,
)
from agentic_debugger.application.runtime_env_spec import (
    PROJECT_RUNTIME_SPEC_VERSION,
    SPEC_PARAM_MAX_CHARS,
    ProjectEnvDeclaration,
    ProjectExplicitValue,
    ProjectRuntimeEnvironmentSpec,
    ProjectRuntimeMaterialization,
    materialize_project_runtime,
    spec_from_param,
    spec_to_param,
)

#: Version of the computed capability contract (durable provenance).
SESSION_CAPABILITIES_VERSION = "session-capabilities/v1"

#: Historical Local Project default model-call ceiling (Task-26).
#: Task 44: retained as provenance for pre-Task-44 evidence only.
#: Generic Local Project execution is unbounded (None/0) and never
#: consults this value or :func:`local_project_model_call_ceiling` for
#: execution authority.  An explicit finite value is honored only by
#: explicit callers (frozen/operator) that intentionally request a bound.
LOCAL_PROJECT_DEFAULT_MAX_MODEL_CALLS = 32


# ---------------------------------------------------------------------------
# AgentDefinition
# ---------------------------------------------------------------------------


def _demo_policy_values() -> frozenset:
    """The stable agent controller-policy identities (single authority).

    Imported lazily so this contract module never pays for the demo
    package import chain at module load; the values still come from the
    one :class:`~agentic_debugger.demo.policies.DemoPolicy` authority
    rather than a duplicated literal.
    """
    from agentic_debugger.demo.policies import DemoPolicy

    return frozenset(item.value for item in DemoPolicy)


@dataclass(frozen=True)
class AgentDefinition:
    """What the agent REQUESTS / is permitted to use (immutable).

    - ``controller_policy``: the stable controller/prompt-policy identity
      (a :class:`~agentic_debugger.demo.policies.DemoPolicy` value, which
      references — never replaces — the existing policy authority).
    - ``provider_id`` / ``model_id``: the REQUESTED provider logical
      identity and model identity (pre-ModelGateway request identity, as
      carried by current scenario params).  Runtime-resolved route facts
      (direct vs legacy, protocol, endpoint, transport profile, credential
      binding) are deliberately absent.
    - ``allowed_capabilities``: the requested tool capabilities.

    Never contains: transport route/protocol/endpoint, credential values
    or bindings, catalog state, live provider status, or scientific
    qualification.
    """

    controller_policy: str
    provider_id: Optional[str] = None
    model_id: Optional[str] = None
    allowed_capabilities: frozenset = field(
        default_factory=lambda: frozenset(_ALL_CAPABILITIES)
    )

    def __post_init__(self) -> None:
        if type(self.controller_policy) is not str:
            raise SessionRuntimeError("controller_policy must be a string")
        if self.controller_policy not in _demo_policy_values():
            raise SessionRuntimeError(
                f"unknown controller_policy: {self.controller_policy!r}"
            )
        object.__setattr__(
            self, "provider_id", _bounded_id(self.provider_id, label="provider_id")
        )
        object.__setattr__(
            self, "model_id", _bounded_id(self.model_id, label="model_id")
        )
        allowed = self.allowed_capabilities
        if isinstance(allowed, (set, list, tuple)):
            try:
                allowed = frozenset(allowed)
            except TypeError as exc:
                raise SessionRuntimeError(
                    "allowed_capabilities must be an iterable of SessionCapability"
                ) from exc
        if type(allowed) is not frozenset or not allowed:
            raise SessionRuntimeError(
                "allowed_capabilities must be a non-empty frozenset of SessionCapability"
            )
        for capability in allowed:
            if type(capability) is not SessionCapability:
                raise SessionRuntimeError(
                    "allowed_capabilities must contain only SessionCapability values"
                )
        object.__setattr__(self, "allowed_capabilities", allowed)

    def to_mapping(self) -> Dict[str, Any]:
        """Safe serialization (request identities only; no secrets exist)."""
        return {
            "controller_policy": self.controller_policy,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "allowed_capabilities": sorted(item.value for item in self.allowed_capabilities),
        }

    @staticmethod
    def from_mapping(value: Mapping[str, Any]) -> "AgentDefinition":
        if not isinstance(value, Mapping):
            raise SessionRuntimeError("agent definition must be a mapping")
        if set(value) != {
            "controller_policy",
            "provider_id",
            "model_id",
            "allowed_capabilities",
        }:
            raise SessionRuntimeError("agent definition fields are invalid")
        raw_capabilities = value["allowed_capabilities"]
        if type(raw_capabilities) is not list:
            raise SessionRuntimeError("allowed_capabilities must be a list of strings")
        try:
            capabilities = frozenset(SessionCapability(item) for item in raw_capabilities)
        except ValueError as exc:
            raise SessionRuntimeError(
                f"unknown session capability: {exc}"
            ) from exc
        try:
            return AgentDefinition(
                controller_policy=value["controller_policy"],
                provider_id=value["provider_id"],
                model_id=value["model_id"],
                allowed_capabilities=capabilities,
            )
        except SessionRuntimeError:
            raise
        except Exception as exc:
            raise SessionRuntimeError(f"agent definition is invalid: {exc}") from exc

    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        capabilities = sorted(item.value for item in self.allowed_capabilities)
        return (
            f"AgentDefinition(policy={self.controller_policy!r}, "
            f"provider={self.provider_id!r}, model={self.model_id!r}, "
            f"capabilities={capabilities!r})"
        )


# ---------------------------------------------------------------------------
# EffectiveSessionCapabilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectiveSessionCapabilities:
    """The one computed session capability authority (immutable).

    Computed ONCE per session from requested ∩ available ∩ task policy;
    every consumer uses this result instead of recomputing the
    intersection.  Session-stable by construction (frozen, no inputs).
    Safe to serialize: capability NAMES only, never secrets.
    """

    version: str = SESSION_CAPABILITIES_VERSION
    capabilities: frozenset = frozenset()

    def __post_init__(self) -> None:
        if self.version != SESSION_CAPABILITIES_VERSION:
            raise SessionRuntimeError(
                f"unsupported session capabilities version: {self.version!r}"
            )
        capabilities = self.capabilities
        if isinstance(capabilities, (set, list, tuple)):
            capabilities = frozenset(capabilities)
        if type(capabilities) is not frozenset:
            raise SessionRuntimeError("capabilities must be a frozenset of SessionCapability")
        for capability in capabilities:
            if type(capability) is not SessionCapability:
                raise SessionRuntimeError(
                    "capabilities must contain only SessionCapability values"
                )
        object.__setattr__(self, "capabilities", capabilities)

    def has(self, capability: SessionCapability) -> bool:
        if type(capability) is not SessionCapability:
            raise SessionRuntimeError("capability must be a SessionCapability")
        return capability in self.capabilities

    def require(self, capability: SessionCapability) -> None:
        """Fail closed when a required capability was not granted."""
        if not self.has(capability):
            raise CapabilityUnavailableError(
                f"Required session capability {capability.value!r} is unavailable."
            )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "capabilities": sorted(item.value for item in self.capabilities),
        }

    @staticmethod
    def from_mapping(value: Mapping[str, Any]) -> "EffectiveSessionCapabilities":
        if not isinstance(value, Mapping):
            raise SessionRuntimeError("session capabilities must be a mapping")
        if set(value) != {"version", "capabilities"}:
            raise SessionRuntimeError("session capabilities fields are invalid")
        raw = value["capabilities"]
        if type(raw) is not list:
            raise SessionRuntimeError("capabilities must be a list of strings")
        try:
            capabilities = frozenset(SessionCapability(item) for item in raw)
        except ValueError as exc:
            raise SessionRuntimeError(f"unknown session capability: {exc}") from exc
        return EffectiveSessionCapabilities(
            version=value["version"], capabilities=capabilities
        )

    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return (
            f"EffectiveSessionCapabilities("
            f"{sorted(item.value for item in self.capabilities)!r})"
        )


def task_allowed_capabilities(controller_policy: str) -> frozenset:
    """Task/product policy input to the capability intersection.

    Today the only task-policy distinction is the accepted PDB gate: a
    ``static-baseline`` session (``PdbPolicy.DISABLED``) denies the PDB
    capability even when requested and available; every other policy
    allows the full requested set.  Scientific qualification never feeds
    this function.
    """
    from agentic_debugger.agent.controller_policy import PdbPolicy
    from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for

    try:
        policy = DemoPolicy(controller_policy)
    except ValueError as exc:
        raise SessionRuntimeError(
            f"unknown controller_policy: {controller_policy!r}"
        ) from exc
    if pdb_policy_for(policy) is PdbPolicy.DISABLED:
        return frozenset(_ALL_CAPABILITIES - {SessionCapability.PDB})
    return frozenset(_ALL_CAPABILITIES)


def compute_effective_capabilities(
    *,
    requested: Iterable[SessionCapability],
    available: Iterable[SessionCapability],
    task_allowed: Iterable[SessionCapability],
) -> EffectiveSessionCapabilities:
    """Compute the single session authority (no silent enablement).

    A capability is granted only when requested AND available AND allowed
    by task policy: machine support alone never enables it, and a request
    alone never conjures availability.
    """
    try:
        result = (
            frozenset(requested) & frozenset(available) & frozenset(task_allowed)
        )
    except TypeError as exc:
        raise SessionRuntimeError(
            f"capability inputs must be iterables of SessionCapability: {exc}"
        ) from exc
    for capability in result:
        if type(capability) is not SessionCapability:
            raise SessionRuntimeError(
                "capability inputs must contain only SessionCapability values"
            )
    return EffectiveSessionCapabilities(capabilities=result)


# ---------------------------------------------------------------------------
# SessionLaunch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionLaunch:
    """The authoritative in-process product-session launch object.

    Binds the immutable session-start inputs exactly once: task identity,
    :class:`AgentDefinition` (which owns the requested controller policy
    and provider/model identities — there is exactly one copy of each),
    the session
    :class:`~agentic_debugger.application.execution_environment.ExecutionEnvironment`
    authority (input identity, never serialized here), the
    :class:`ProjectRuntimeEnvironmentSpec`, the computed
    :class:`EffectiveSessionCapabilities`, the pre-ModelGateway profile
    request identity, and the session budgets.  The worker builds it once
    after the pre-start gate; the source consumes it without recomputing
    any session-start fact.
    """

    session_id: str
    task_id: str
    agent: AgentDefinition
    execution_environment: Any
    project_spec: ProjectRuntimeEnvironmentSpec
    capabilities: EffectiveSessionCapabilities
    profile_id: str
    budgets: Any
    retry_of: Optional[str] = None
    model_binding: Optional[Any] = None
    #: V2-04 (repair 21): the SESSION credential authority, fixed at this
    #: authoritative session-start boundary.  SAFE metadata only (a
    #: CredentialBinding names exactly one credential source authority —
    #: never a secret, never a lease): the transport materializes from
    #: exactly this issued source, so durable credential state changed
    #: after SESSION_STARTED can never silently switch this session.
    credential_binding: Optional[Any] = None
    #: V2-04 (repair 22, F6): the OPAQUE process-local ticket redeeming the
    #: lease that was resolved and pinned at THIS boundary.  This is a
    #: safe in-process capability (random hex, no secret material); it is
    #: deliberately EXCLUDED from to_mapping()/fingerprint() so it can
    #: never enter durable provenance, and it is never serialized.
    credential_ticket: Optional[str] = None

    @property
    def provider_id(self) -> Optional[str]:
        """Requested provider identity (read-only view of the agent)."""
        return self.agent.provider_id

    @property
    def model_id(self) -> Optional[str]:
        """Requested model identity (read-only view of the agent)."""
        return self.agent.model_id

    @property
    def policy(self) -> str:
        """Requested controller-policy identity (read-only view of the agent)."""
        return self.agent.controller_policy

    def __post_init__(self) -> None:
        from agentic_debugger.application.execution_environment import (
            ExecutionEnvironment,
        )
        from agentic_debugger.application.session import SessionBudgets

        try:
            validate_session_id(self.session_id)
        except Exception as exc:
            raise SessionRuntimeError(f"invalid session id: {exc}") from exc
        if type(self.task_id) is not str or not self.task_id.strip():
            raise SessionRuntimeError("task_id must be a non-empty string")
        if len(self.task_id.encode("utf-8")) > 256:
            raise SessionRuntimeError("task_id exceeds the length bound")
        if type(self.agent) is not AgentDefinition:
            raise SessionRuntimeError("agent must be an AgentDefinition")
        if type(self.execution_environment) is not ExecutionEnvironment:
            raise SessionRuntimeError(
                "execution_environment must be an ExecutionEnvironment"
            )
        if type(self.project_spec) is not ProjectRuntimeEnvironmentSpec:
            raise SessionRuntimeError(
                "project_spec must be a ProjectRuntimeEnvironmentSpec"
            )
        if type(self.capabilities) is not EffectiveSessionCapabilities:
            raise SessionRuntimeError(
                "capabilities must be an EffectiveSessionCapabilities"
            )
        if type(self.profile_id) is not str or not self.profile_id:
            raise SessionRuntimeError("profile_id must be a non-empty string")
        if len(self.profile_id.encode("utf-8")) > 128:
            raise SessionRuntimeError("profile_id exceeds the length bound")
        if type(self.budgets) is not SessionBudgets:
            raise SessionRuntimeError("budgets must be a SessionBudgets")
        if self.retry_of is not None:
            try:
                validate_session_id(self.retry_of)
            except Exception as exc:
                raise SessionRuntimeError(f"invalid retry_of session id: {exc}") from exc
        if self.model_binding is not None:
            from agentic_debugger.application.model_gateway import ModelBinding
            if not isinstance(self.model_binding, ModelBinding):
                raise SessionRuntimeError("model_binding must be a ModelBinding")
        if self.credential_binding is not None:
            from agentic_debugger.application.credential_vault import (
                CredentialBinding,
            )
            if not isinstance(self.credential_binding, CredentialBinding):
                raise SessionRuntimeError(
                    "credential_binding must be a CredentialBinding"
                )
        if self.credential_ticket is not None:
            import re as _re

            if (
                not isinstance(self.credential_ticket, str)
                or _re.fullmatch(r"[0-9a-f]{32}", self.credential_ticket) is None
            ):
                raise SessionRuntimeError(
                    "credential_ticket must be a 32-hex opaque session ticket"
                )
        # Repair 23 (Finding 1): a retained ticket is inseparable from its
        # binding — it is NOT a free-floating capability.  A ticket without
        # its binding fails construction, and only a materializable direct
        # provider authority may carry a ticket (never external_cli / none,
        # never a non-direct route).  The ModelBinding/CredentialBinding
        # coherence check below remains mandatory.
        if self.credential_ticket is not None:
            if self.credential_binding is None:
                raise SessionRuntimeError(
                    "credential_ticket requires its credential_binding"
                )
            from agentic_debugger.application.credential_vault import (
                _MATERIALABLE_SOURCE_KINDS,
            )

            if (
                self.credential_binding.source_kind
                not in _MATERIALABLE_SOURCE_KINDS
            ):
                raise SessionRuntimeError(
                    "credential_ticket is not valid for this credential authority"
                )
            if self.model_binding is not None and getattr(
                self.model_binding, "route", None
            ) != "direct_api":
                raise SessionRuntimeError(
                    "credential_ticket is not valid for this session route"
                )
        # Repair 22 (F3): the credential authority must be COHERENT with
        # the model binding — same provider, same runtime authority, same
        # auth mode, route-compatible source kind — at construction time,
        # not only at the gateway boundary.
        if self.credential_binding is not None and self.model_binding is not None:
            from agentic_debugger.application.model_gateway import (
                assert_credential_binding_coherent,
            )

            assert_credential_binding_coherent(
                self.model_binding, self.credential_binding
            )

    def to_mapping(self) -> Dict[str, Any]:
        """Safe launch provenance (never the execution environment itself,
        never secret values — the spec mapping carries secret NAMES only)."""
        mapping = {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "agent": self.agent.to_mapping(),
            "project_spec": self.project_spec.to_mapping(),
            "capabilities": self.capabilities.to_mapping(),
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "profile_id": self.profile_id,
            "policy": self.policy,
            "budgets": self.budgets.to_mapping(),
            "retry_of": self.retry_of,
        }
        if self.model_binding is not None:
            mapping["model_binding"] = self.model_binding.to_mapping()
        if self.credential_binding is not None:
            mapping["credential_binding"] = self.credential_binding.to_mapping()
        # credential_ticket is deliberately EXCLUDED: it is a process-local
        # redemption capability, never durable provenance.
        return mapping

    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return (
            f"SessionLaunch(session={self.session_id!r}, task={self.task_id!r}, "
            f"agent={self.agent!r}, capabilities={self.capabilities!r}, "
            f"project_spec={self.project_spec!r})"
        )


def local_project_model_call_ceiling(budgets: Any) -> int:
    """Historical effective model-call ceiling of a Local Project session.

    Task 44: retained for provenance/compat only.  Generic Local Project
    execution is unbounded and never consults this function for
    execution authority — the runtime passes ``None`` (Live/Controller)
    and ``0`` (provider adapter) directly.  An explicit finite value is
    honored only by explicit callers (frozen/operator) that intentionally
    request a bound.  Fails closed on a non-SessionBudgets input.
    """
    from agentic_debugger.application.session import SessionBudgets

    if type(budgets) is not SessionBudgets:
        raise SessionRuntimeError("budgets must be a SessionBudgets")
    if budgets.max_model_calls is not None:
        return budgets.max_model_calls
    return LOCAL_PROJECT_DEFAULT_MAX_MODEL_CALLS


def build_local_project_launch(
    *,
    session_id: str,
    task_id: str,
    policy: str,
    provider_id: Optional[str],
    model_id: Optional[str],
    profile_id: str,
    launch_snapshot: Mapping[str, str],
    project_spec: ProjectRuntimeEnvironmentSpec,
    budgets: Any = None,
    retry_of: Optional[str] = None,
    platform: Any = None,
    model_binding: Optional[Any] = None,
    is_ollama: bool = False,
    ollama_alias: Optional[str] = None,
    config_root: Optional[Any] = None,
) -> SessionLaunch:
    """Build the one Local Project SessionLaunch (single construction site).

    Both the worker (authoritative for worker sessions) and the source
    fallback (direct non-worker callers) construct through this factory so
    session-start facts are never independently reconstructed in multiple
    layers.  The launch snapshot is copied on the boundary; declared
    project names resolve once here under the worker/canonical platform
    (live ``sys.platform`` unless overridden for tests).
    """
    from agentic_debugger.application.execution_environment import (
        ExecutionEnvironment,
    )
    from agentic_debugger.application.session import SessionBudgets

    if not isinstance(launch_snapshot, Mapping):
        raise SessionRuntimeError("launch snapshot must be a mapping")
    if type(project_spec) is not ProjectRuntimeEnvironmentSpec:
        raise SessionRuntimeError("project_spec must be a ProjectRuntimeEnvironmentSpec")
    if budgets is None:
        budgets = SessionBudgets()
    agent = AgentDefinition(
        controller_policy=policy if isinstance(policy, str) and policy else "pdb-on-uncertainty",
        provider_id=provider_id,
        model_id=model_id,
    )
    # The snapshot is copied by the environment authority; this factory
    # never retains the caller's mapping.
    environment = ExecutionEnvironment.for_local_project(
        dict(launch_snapshot), project_spec, platform=platform
    )
    capabilities = compute_effective_capabilities(
        requested=agent.allowed_capabilities,
        available=environment.available_capabilities,
        task_allowed=task_allowed_capabilities(agent.controller_policy),
    )

    if model_binding is None:
        from agentic_debugger.application.model_gateway import ModelGateway

        gateway = ModelGateway.default(config_root=config_root)
        # Task 44: generic Local Project execution is unbounded.  The
        # SessionLaunch ModelBinding resolves under the SAME unbounded
        # logical-call authority (0 = no upper-bound termination) the
        # source later uses for transport materialization
        # (``ModelGateway.create_transport``), the live adapter
        # (``max_model_requests=None``) and the controller
        # (``max_model_calls=None``).  SessionBudgets count dimensions
        # and the historical Local Project default (32) are
        # compatibility/provenance metadata only: they never enter the
        # executable command/fingerprint and never gate execution.  An
        # explicit scientific caller needing a finite envelope supplies
        # its own pre-resolved ``model_binding`` instead.
        model_binding = gateway.resolve(
            provider_id=provider_id,
            model_id=model_id,
            profile_id=profile_id,
            logical_call_ceiling=0,
            is_ollama=is_ollama,
            ollama_alias=ollama_alias,
        )

    # V2-04 (repairs 21+22): fix the SESSION credential authority ONCE at
    # this authoritative session-start boundary, ROUTE-AWARE: the resolved
    # ModelBinding route decides whether a registry-provider credential
    # authority exists at all (legacy CLI routes carry only the safe
    # external_cli authority; profile/ladder/offline routes carry none).
    # For materializable authorities the vault resolves the lease ONCE
    # here and retains it under an opaque process-local ticket, so the
    # pinned SECRET — not just the source metadata — is fixed at this
    # boundary; durable slot/environment changes after this point can
    # never switch the session.  The binding is SAFE metadata and the
    # ticket a safe in-process capability: neither carries secret
    # material, and neither is durable.
    credential_binding = None
    credential_ticket = None
    if (
        provider_id is not None
        and provider_id != "configured"
        and model_binding is not None
        and model_binding.route in ("direct_api", "legacy_cli")
    ):
        from agentic_debugger.application.credential_vault import (
            _MATERIALABLE_SOURCE_KINDS,
            CredentialVault,
        )

        vault = CredentialVault.default()
        credential_binding = vault.session_authority(
            provider_id, route=model_binding.route
        )
        if (
            credential_binding is not None
            and credential_binding.source_kind in _MATERIALABLE_SOURCE_KINDS
        ):
            # Repair 23 (Finding 1): a session-stable DIRECT_API launch
            # with a materializable binding MUST carry its retained ticket
            # — never silently downgrade to later fresh secret resolution.
            lease = vault.resolve_lease(credential_binding)
            if lease is None:
                from agentic_debugger.application.credential_vault import (
                    CredentialUnavailableError as _Unavailable,
                )

                raise _Unavailable(
                    f"Credential unavailable for provider {provider_id!r}: "
                    "the session credential authority is materializable but "
                    "no lease could be pinned"
                )
            credential_ticket = vault.retain_lease(lease, credential_binding)

    return SessionLaunch(
        session_id=session_id,
        task_id=task_id,
        agent=agent,
        execution_environment=environment,
        project_spec=project_spec,
        capabilities=capabilities,
        profile_id=profile_id,
        budgets=budgets,
        retry_of=retry_of,
        model_binding=model_binding,
        credential_binding=credential_binding,
        credential_ticket=credential_ticket,
    )


def check_launch_matches_params(
    launch: SessionLaunch,
    *,
    policy: Any,
    provider_id: Any,
    model_id: Any,
    profile_id: Any,
    project_spec: Any,
) -> None:
    """Corroborate legacy transport params against the authoritative launch.

    Legacy scenario params may corroborate the launch; they may never
    override it.  Any difference in the mirrored session-start facts
    (controller policy, requested provider/model, profile identity, or the
    project runtime spec) fails closed with a safe name-only error before
    any project/model execution.  Specs compare by safe fingerprint —
    never by materialized values (which are not present here at all).
    """
    if type(launch) is not SessionLaunch:
        raise SessionRuntimeError("launch must be a SessionLaunch")
    if type(project_spec) is not ProjectRuntimeEnvironmentSpec:
        raise SessionRuntimeError("project_spec must be a ProjectRuntimeEnvironmentSpec")
    if policy != launch.agent.controller_policy:
        raise SessionRuntimeError(
            "session launch policy does not match the session params"
        )
    if provider_id != launch.agent.provider_id:
        raise SessionRuntimeError(
            "session launch provider does not match the session params"
        )
    if model_id != launch.agent.model_id:
        raise SessionRuntimeError(
            "session launch model does not match the session params"
        )
    if profile_id != launch.profile_id:
        raise SessionRuntimeError(
            "session launch profile does not match the session params"
        )
    if project_spec.fingerprint() != launch.project_spec.fingerprint():
        raise SessionRuntimeError(
            "session launch project runtime spec does not match the session params"
        )
    if launch.model_binding is not None:
        if (
            launch.agent.provider_id is not None
            and launch.model_binding.provider_id != launch.agent.provider_id
        ):
            raise SessionRuntimeError(
                "session launch model_binding provider does not match agent definition"
            )
        if (
            launch.agent.model_id is not None
            and launch.model_binding.model_id != launch.agent.model_id
        ):
            raise SessionRuntimeError(
                "session launch model_binding model does not match agent definition"
            )


__all__ = [
    "AgentDefinition",
    "CapabilityUnavailableError",
    "EffectiveSessionCapabilities",
    "PLATFORM_ESSENTIAL_NAMES",
    "ProjectEnvDeclaration",
    "ProjectExplicitValue",
    "ProjectRuntimeEnvironmentSpec",
    "ProjectRuntimeError",
    "ProjectRuntimeMaterialization",
    "PROJECT_RUNTIME_SPEC_VERSION",
    "SESSION_CAPABILITIES_VERSION",
    "SessionCapability",
    "SessionLaunch",
    "SessionRuntimeError",
    "SPEC_PARAM_MAX_CHARS",
    "build_local_project_launch",
    "canonical_env_name",
    "check_launch_matches_params",
    "compute_effective_capabilities",
    "is_platform_essential_name",
    "local_project_model_call_ceiling",
    "LOCAL_PROJECT_DEFAULT_MAX_MODEL_CALLS",
    "materialize_project_runtime",
    "resolve_env_name_platform",
    "spec_from_param",
    "spec_to_param",
    "task_allowed_capabilities",
    "validate_env_name",
]
