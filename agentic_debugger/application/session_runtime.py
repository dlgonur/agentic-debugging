"""V2-02 product session/runtime contracts (Local Project sessions).

This module is the explicit product-session launch authority vocabulary:

- :class:`AgentDefinition` — what the agent REQUESTS / is permitted to use
  (controller policy identity, requested provider/model logical identity,
  requested tool capabilities).  It never carries runtime-resolved provider
  route facts (route, protocol, endpoint, transport profile), credential
  values or bindings, catalog state, live status, or scientific
  qualification.  Those remain outside, owned by the current provider
  runtime (pre-ModelGateway) and the existing scientific authorities.
- :class:`ProjectRuntimeEnvironmentSpec` — the explicit Local Project
  runtime-environment ingress: platform essentials stay derived by the
  execution-environment/platform logic; everything else the project needs
  is declared here (explicit non-secret values, explicitly inherited
  variable NAMES, explicitly authorized project-secret binding NAMES).
  Secret VALUES are never stored in the spec, never serialized, never
  journaled, never fingerprinted, never repr'd.
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
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from agentic_debugger.application.events import (
    contains_credential_shape,
    validate_session_id,
)

#: Version of the project-runtime ingress contract (durable provenance).
PROJECT_RUNTIME_SPEC_VERSION = "project-runtime/v1"

#: Version of the computed capability contract (durable provenance).
SESSION_CAPABILITIES_VERSION = "session-capabilities/v1"

#: Transport bound: the worker start pipe carries scenario params as
#: strings of at most 4096 bytes, so the serialized spec must fit.
SPEC_PARAM_MAX_CHARS = 4096

_MAX_DECLARATIONS_PER_CATEGORY = 32
_MAX_ENV_NAME_CHARS = 128
_MAX_EXPLICIT_VALUE_BYTES = 1024
_MAX_ID_CHARS = 128

_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class SessionRuntimeError(ValueError):
    """A V2-02 session/runtime contract received invalid input."""


class ProjectRuntimeError(SessionRuntimeError):
    """A project-runtime declaration or materialization failed fail-closed."""


class CapabilityUnavailableError(SessionRuntimeError):
    """A required session capability is not granted to this session."""


class SessionCapability(str, Enum):
    """The small V2-02 product capability vocabulary.

    Grounded in current Local Project features only:

    - ``PROJECT_COMMAND`` — project reproduction/regression command
      execution (``run_reproduction`` / ``run_regression_tests``);
    - ``PDB`` — product PDB debugging (``start_pdb_session`` family);
    - ``PATCH`` — candidate patch application/revert/syntax;
    - ``VERIFIER`` — independent verification of the candidate.

    There is deliberately no network capability: project networking is not
    product-authoritative in V2-02 (``Constraints.network_allowed`` remains
    the authority); project-owned proxy/CA variables travel through the
    normal explicit inherit-by-name mechanism.
    """

    PROJECT_COMMAND = "project_command"
    PDB = "pdb"
    PATCH = "patch"
    VERIFIER = "verifier"


_ALL_CAPABILITIES = frozenset(SessionCapability)


def validate_env_name(name: Any, *, label: str = "environment variable name") -> str:
    """Validate one environment variable NAME (never a value)."""
    if type(name) is not str or not name:
        raise SessionRuntimeError(f"{label} must be a non-empty string")
    if len(name) > _MAX_ENV_NAME_CHARS:
        raise SessionRuntimeError(f"{label} {name!r} exceeds the length bound")
    if _ENV_NAME_PATTERN.match(name) is None:
        raise SessionRuntimeError(f"{label} {name!r} is not a valid variable name")
    return name


def _reject_control_authority_name(name: str, *, label: str) -> None:
    """Fail closed when a declaration claims platform/control ownership.

    Uses the same central provider-authority classification as V2-01 (no
    duplicated name list here): the repository namespace and the provider
    credential/config/auth-store authorities can never be declared as
    project runtime state.
    """
    from agentic_debugger.application.provider_connections import (
        provider_authority_environment_names,
    )

    if name.upper().startswith("AGENTIC_DEBUGGER_"):
        raise SessionRuntimeError(
            f"{label} {name!r} is an Agentic Debugger control variable "
            "and must not be declared as project runtime state"
        )
    if name.lower() in frozenset(
        known.lower() for known in provider_authority_environment_names()
    ):
        raise SessionRuntimeError(
            f"{label} {name!r} is a provider authority variable "
            "and must not be declared as project runtime state"
        )


def _bounded_id(value: Any, *, label: str) -> Optional[str]:
    if value is None:
        return None
    if type(value) is not str or not value:
        raise SessionRuntimeError(f"{label} must be a non-empty string or null")
    if len(value.encode("utf-8")) > _MAX_ID_CHARS:
        raise SessionRuntimeError(f"{label} exceeds the length bound")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise SessionRuntimeError(f"{label} contains control characters")
    if contains_credential_shape(value):
        raise SessionRuntimeError(f"{label} contains a credential-shaped value")
    return value


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
# ProjectRuntimeEnvironmentSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectExplicitValue:
    """One explicit non-secret project value (NAME = value)."""

    name: str
    value: str

    def __post_init__(self) -> None:
        validate_env_name(self.name)
        _reject_control_authority_name(self.name, label="explicit project value")
        if type(self.value) is not str:
            raise SessionRuntimeError(
                f"explicit project value for {self.name!r} must be a string"
            )
        if len(self.value.encode("utf-8")) > _MAX_EXPLICIT_VALUE_BYTES:
            raise SessionRuntimeError(
                f"explicit project value for {self.name!r} exceeds the size bound"
            )
        if contains_credential_shape(self.value):
            raise SessionRuntimeError(
                f"explicit project value for {self.name!r} looks like a secret; "
                "declare it as a project-secret binding instead"
            )


@dataclass(frozen=True)
class ProjectEnvDeclaration:
    """One explicitly declared project variable NAME (by-name binding).

    Used both for benign inheritance (``inherit``) and for project-secret
    bindings (``secrets``).  Only the NAME, the ``required`` flag, and the
    binding kind are ever durable; a secret VALUE is never stored here.
    """

    name: str
    required: bool = True

    def __post_init__(self) -> None:
        validate_env_name(self.name)
        _reject_control_authority_name(self.name, label="project environment declaration")
        if type(self.required) is not bool:
            raise SessionRuntimeError(
                f"declaration for {self.name!r} must carry a boolean required flag"
            )


@dataclass(frozen=True)
class ProjectRuntimeEnvironmentSpec:
    """The explicit Local Project runtime-environment ingress (immutable).

    - ``values``: explicit non-secret project values (NAME = value).
    - ``inherit``: benign variable NAMES imported from the launch
      environment (per-name, with required/optional semantics).
    - ``secrets``: explicitly authorized project-SECRET binding NAMES,
      resolved ephemerally from the launch environment at session start.
      The spec carries names only — never values.

    An empty spec (the default) is valid: the session then runs on
    platform/runtime essentials alone.
    """

    version: str = PROJECT_RUNTIME_SPEC_VERSION
    values: Tuple[ProjectExplicitValue, ...] = ()
    inherit: Tuple[ProjectEnvDeclaration, ...] = ()
    secrets: Tuple[ProjectEnvDeclaration, ...] = ()

    def __post_init__(self) -> None:
        if self.version != PROJECT_RUNTIME_SPEC_VERSION:
            raise SessionRuntimeError(
                f"unsupported project runtime spec version: {self.version!r}"
            )
        values = self._normalized_values(self.values)
        inherit = self._normalized_declarations(self.inherit, "inherit")
        secrets = self._normalized_declarations(self.secrets, "secrets")
        seen: Dict[str, str] = {}
        for entry in values:
            seen.setdefault(entry.name, "explicit value")
        for entry in inherit:
            if entry.name in seen:
                raise SessionRuntimeError(
                    f"project variable {entry.name!r} is declared more than once"
                )
            seen[entry.name] = "inherit"
        for entry in secrets:
            if entry.name in seen:
                raise SessionRuntimeError(
                    f"project variable {entry.name!r} is declared more than once"
                )
            seen[entry.name] = "secret"
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "inherit", inherit)
        object.__setattr__(self, "secrets", secrets)

    @staticmethod
    def _normalized_values(value: Any) -> Tuple[ProjectExplicitValue, ...]:
        if isinstance(value, Mapping):
            items = [ProjectExplicitValue(name, val) for name, val in value.items()]
        elif isinstance(value, (tuple, list)):
            items = list(value)
            for entry in items:
                if type(entry) is not ProjectExplicitValue:
                    raise SessionRuntimeError(
                        "explicit project values must be ProjectExplicitValue entries"
                    )
        else:
            raise SessionRuntimeError("explicit project values must be a mapping or tuple")
        if len(items) > _MAX_DECLARATIONS_PER_CATEGORY:
            raise SessionRuntimeError("too many explicit project values declared")
        return tuple(sorted(items, key=lambda entry: entry.name))

    @staticmethod
    def _normalized_declarations(value: Any, label: str) -> Tuple[ProjectEnvDeclaration, ...]:
        if isinstance(value, (tuple, list)):
            entries = list(value)
        else:
            raise SessionRuntimeError(f"project {label} declarations must be a tuple")
        for entry in entries:
            if type(entry) is not ProjectEnvDeclaration:
                raise SessionRuntimeError(
                    f"project {label} declarations must be ProjectEnvDeclaration entries"
                )
        if len(entries) > _MAX_DECLARATIONS_PER_CATEGORY:
            raise SessionRuntimeError(f"too many project {label} declarations")
        return tuple(sorted(entries, key=lambda entry: entry.name))

    def declared_names(self) -> Tuple[str, ...]:
        """All declared variable NAMES (safe provenance, never values)."""
        return tuple(
            sorted(
                [entry.name for entry in self.values]
                + [entry.name for entry in self.inherit]
                + [entry.name for entry in self.secrets]
            )
        )

    def to_mapping(self) -> Dict[str, Any]:
        """Safe durable serialization: names, flags, non-secret values.

        Secret bindings serialize as NAMES with their required flag only;
        secret values never exist in this object and cannot leak here.
        """
        return {
            "version": self.version,
            "values": {entry.name: entry.value for entry in self.values},
            "inherit": [
                {"name": entry.name, "required": entry.required}
                for entry in self.inherit
            ],
            "secrets": [
                {"name": entry.name, "required": entry.required}
                for entry in self.secrets
            ],
        }

    @staticmethod
    def from_mapping(value: Mapping[str, Any]) -> "ProjectRuntimeEnvironmentSpec":
        if not isinstance(value, Mapping):
            raise SessionRuntimeError("project runtime spec must be a mapping")
        if set(value) != {"version", "values", "inherit", "secrets"}:
            raise SessionRuntimeError("project runtime spec fields are invalid")

        def _declarations(raw: Any, label: str) -> Tuple[ProjectEnvDeclaration, ...]:
            if type(raw) is not list:
                raise SessionRuntimeError(f"project {label} declarations must be a list")
            entries = []
            for item in raw:
                if not isinstance(item, Mapping) or set(item) != {"name", "required"}:
                    raise SessionRuntimeError(
                        f"project {label} declaration fields are invalid"
                    )
                entries.append(
                    ProjectEnvDeclaration(name=item["name"], required=item["required"])
                )
            return tuple(entries)

        raw_values = value["values"]
        if not isinstance(raw_values, Mapping):
            raise SessionRuntimeError("explicit project values must be a mapping")
        try:
            return ProjectRuntimeEnvironmentSpec(
                version=value["version"],
                values=tuple(
                    ProjectExplicitValue(name, val) for name, val in raw_values.items()
                ),
                inherit=_declarations(value["inherit"], "inherit"),
                secrets=_declarations(value["secrets"], "secrets"),
            )
        except SessionRuntimeError:
            raise
        except Exception as exc:
            raise SessionRuntimeError(
                f"project runtime spec is invalid: {exc}"
            ) from exc

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
        # Names are safe provenance; VALUES are never rendered even though
        # explicit values are non-secret by construction.
        return (
            f"ProjectRuntimeEnvironmentSpec(version={self.version!r}, "
            f"values={len(self.values)}, inherit={[e.name for e in self.inherit]!r}, "
            f"secrets={[e.name for e in self.secrets]!r})"
        )


def spec_to_param(spec: ProjectRuntimeEnvironmentSpec) -> str:
    """Serialize one spec for the worker start transport (safe: no secrets)."""
    if type(spec) is not ProjectRuntimeEnvironmentSpec:
        raise SessionRuntimeError("spec must be a ProjectRuntimeEnvironmentSpec")
    try:
        encoded = json.dumps(
            spec.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SessionRuntimeError(f"project runtime spec is not serializable: {exc}") from exc
    if len(encoded.encode("utf-8")) > SPEC_PARAM_MAX_CHARS:
        raise SessionRuntimeError(
            "project runtime spec exceeds the worker transport bound; "
            "declare fewer variables"
        )
    return encoded


def spec_from_param(value: Any) -> ProjectRuntimeEnvironmentSpec:
    """Parse one transported spec; absent/empty means the empty spec."""
    if value is None or value == "":
        return ProjectRuntimeEnvironmentSpec()
    if type(value) is not str:
        raise SessionRuntimeError("project runtime spec transport must be a string or null")
    if len(value.encode("utf-8")) > SPEC_PARAM_MAX_CHARS:
        raise SessionRuntimeError("project runtime spec exceeds the worker transport bound")
    try:
        raw = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SessionRuntimeError(f"project runtime spec is not valid JSON: {exc}") from exc
    return ProjectRuntimeEnvironmentSpec.from_mapping(raw)


# ---------------------------------------------------------------------------
# Materialization (trusted session memory only — never serialized)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectRuntimeMaterialization:
    """The fixed per-session resolved project runtime (in-memory only).

    Created ONCE at session launch from the launch-environment snapshot;
    every session execution role derives from this fixed mapping, so a
    post-start mutation of the parent ``os.environ`` can never alter the
    session.  Deliberately has NO ``to_mapping``/``from_mapping``: resolved
    values (which may include project secrets) must never be serialized
    into params, journals, history, or review structures.
    """

    spec_version: str
    resolved: Mapping[str, str] = field(default_factory=dict)
    provenance: Tuple[Tuple[str, str, bool], ...] = ()

    def __post_init__(self) -> None:
        if self.spec_version != PROJECT_RUNTIME_SPEC_VERSION:
            raise SessionRuntimeError(
                f"unsupported project runtime spec version: {self.spec_version!r}"
            )
        if not isinstance(self.resolved, Mapping):
            raise SessionRuntimeError("materialized runtime must be a mapping")
        copied: Dict[str, str] = {}
        for name, val in self.resolved.items():
            validate_env_name(name)
            if type(val) is not str:
                raise SessionRuntimeError(
                    f"materialized value for {name!r} must be a string"
                )
            copied[name] = val
        object.__setattr__(self, "resolved", MappingProxyType(copied))
        provenance = tuple(self.provenance)
        for entry in provenance:
            if (
                type(entry) is not tuple
                or len(entry) != 3
                or type(entry[0]) is not str
                or entry[1] not in ("value", "inherit", "secret")
                or type(entry[2]) is not bool
            ):
                raise SessionRuntimeError("materialization provenance is malformed")
        object.__setattr__(self, "provenance", provenance)

    def to_child_mapping(self) -> Dict[str, str]:
        """One detached copy for a single child environment derivation."""
        return dict(self.resolved)

    def declared_names(self) -> Tuple[str, ...]:
        """Safe provenance: declared NAMES only, never values."""
        return tuple(sorted(dict(self.resolved).keys()))

    def __repr__(self) -> str:
        kinds = sorted({kind for _, kind, _ in self.provenance})
        return (
            f"ProjectRuntimeMaterialization(spec={self.spec_version!r}, "
            f"variables={len(self.resolved)}, kinds={kinds!r})"
        )


def materialize_project_runtime(
    spec: ProjectRuntimeEnvironmentSpec,
    launch_snapshot: Mapping[str, str],
) -> ProjectRuntimeMaterialization:
    """Resolve declared NAMES once against the launch snapshot.

    The snapshot is the single fixed per-session parent view (copied on
    the boundary); every later child derives from the returned fixed
    mapping.  Missing REQUIRED names fail closed with a safe name-only
    error; missing optional names are skipped.  No value is ever logged,
    fingerprinted, or rendered.
    """
    if type(spec) is not ProjectRuntimeEnvironmentSpec:
        raise ProjectRuntimeError("spec must be a ProjectRuntimeEnvironmentSpec")
    if not isinstance(launch_snapshot, Mapping):
        raise ProjectRuntimeError("launch snapshot must be a mapping")
    for name, val in launch_snapshot.items():
        if type(name) is not str or type(val) is not str:
            raise ProjectRuntimeError("launch snapshot must map strings to strings")

    resolved: Dict[str, str] = {}
    provenance: list[Tuple[str, str, bool]] = []
    for entry in spec.values:
        resolved[entry.name] = entry.value
        provenance.append((entry.name, "value", True))
    for entry in spec.inherit:
        if entry.name in launch_snapshot:
            resolved[entry.name] = launch_snapshot[entry.name]
            provenance.append((entry.name, "inherit", entry.required))
        elif entry.required:
            raise ProjectRuntimeError(
                f"Required project environment variable {entry.name} is unavailable."
            )
    for entry in spec.secrets:
        if entry.name in launch_snapshot:
            resolved[entry.name] = launch_snapshot[entry.name]
            provenance.append((entry.name, "secret", entry.required))
        elif entry.required:
            raise ProjectRuntimeError(
                f"Required project environment variable {entry.name} is unavailable."
            )
    return ProjectRuntimeMaterialization(
        spec_version=spec.version,
        resolved=resolved,
        provenance=tuple(sorted(provenance)),
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
    :class:`AgentDefinition`, the session
    :class:`~agentic_debugger.application.execution_environment.ExecutionEnvironment`
    authority (input identity, never serialized here), the
    :class:`ProjectRuntimeEnvironmentSpec`, the computed
    :class:`EffectiveSessionCapabilities`, the pre-ModelGateway
    provider/model request identity, and the session budgets.  The worker
    builds it once after the pre-start gate; the source consumes it
    without recomputing any session-start fact.
    """

    session_id: str
    task_id: str
    agent: AgentDefinition
    execution_environment: Any
    project_spec: ProjectRuntimeEnvironmentSpec
    capabilities: EffectiveSessionCapabilities
    provider_id: Optional[str]
    model_id: Optional[str]
    profile_id: str
    policy: str
    budgets: Any
    retry_of: Optional[str] = None

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
        object.__setattr__(
            self, "provider_id", _bounded_id(self.provider_id, label="provider_id")
        )
        object.__setattr__(
            self, "model_id", _bounded_id(self.model_id, label="model_id")
        )
        if type(self.profile_id) is not str or not self.profile_id:
            raise SessionRuntimeError("profile_id must be a non-empty string")
        if len(self.profile_id.encode("utf-8")) > _MAX_ID_CHARS:
            raise SessionRuntimeError("profile_id exceeds the length bound")
        if self.policy not in _demo_policy_values():
            raise SessionRuntimeError(f"unknown controller_policy: {self.policy!r}")
        if type(self.budgets) is not SessionBudgets:
            raise SessionRuntimeError("budgets must be a SessionBudgets")
        if self.retry_of is not None:
            try:
                validate_session_id(self.retry_of)
            except Exception as exc:
                raise SessionRuntimeError(f"invalid retry_of session id: {exc}") from exc

    def to_mapping(self) -> Dict[str, Any]:
        """Safe launch provenance (never the execution environment itself,
        never secret values — the spec mapping carries secret NAMES only)."""
        return {
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
) -> SessionLaunch:
    """Build the one Local Project SessionLaunch (single construction site).

    Both the worker (authoritative for worker sessions) and the source
    fallback (direct non-worker callers) construct through this factory so
    session-start facts are never independently reconstructed in multiple
    layers.  The launch snapshot is copied on the boundary; declared
    project names resolve once here.
    """
    from agentic_debugger.application.execution_environment import (
        ExecutionEnvironment,
    )
    from agentic_debugger.application.session import SessionBudgets

    if not isinstance(launch_snapshot, Mapping):
        raise SessionRuntimeError("launch snapshot must be a mapping")
    if type(project_spec) is not ProjectRuntimeEnvironmentSpec:
        raise SessionRuntimeError("project_spec must be a ProjectRuntimeEnvironmentSpec")
    agent = AgentDefinition(
        controller_policy=policy if isinstance(policy, str) and policy else "pdb-on-uncertainty",
        provider_id=provider_id,
        model_id=model_id,
    )
    # The snapshot is copied by the environment authority; this factory
    # never retains the caller's mapping.
    environment = ExecutionEnvironment.for_local_project(
        dict(launch_snapshot), project_spec
    )
    capabilities = compute_effective_capabilities(
        requested=agent.allowed_capabilities,
        available=environment.available_capabilities,
        task_allowed=task_allowed_capabilities(agent.controller_policy),
    )
    return SessionLaunch(
        session_id=session_id,
        task_id=task_id,
        agent=agent,
        execution_environment=environment,
        project_spec=project_spec,
        capabilities=capabilities,
        provider_id=agent.provider_id,
        model_id=agent.model_id,
        profile_id=profile_id,
        policy=agent.controller_policy,
        budgets=budgets if budgets is not None else SessionBudgets(),
        retry_of=retry_of,
    )


__all__ = [
    "AgentDefinition",
    "CapabilityUnavailableError",
    "EffectiveSessionCapabilities",
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
    "compute_effective_capabilities",
    "materialize_project_runtime",
    "spec_from_param",
    "spec_to_param",
    "task_allowed_capabilities",
    "validate_env_name",
]
