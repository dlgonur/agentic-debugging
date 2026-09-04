"""V2 product execution-environment authority (Local Project sessions).

One authority per session snapshots and classifies the product execution
environment once and derives explicit, role-scoped child environments for
the product child roles: ordinary project/reproduction/test commands, the
ordinary product PDB worker, LocalProjectVerifier command children, and
(least-authority) terminal cleanup Git children.  Child process sites never
inspect ``os.environ`` themselves; the session/source boundary constructs
this authority and threads its derived mappings explicitly to every
consumer.

**V2-02 declarative target (normal product path).**  Normal newly launched
Local Project sessions are built with
:meth:`ExecutionEnvironment.for_local_project` from an explicit
:class:`~agentic_debugger.application.session_runtime.ProjectRuntimeEnvironmentSpec`:

.. code-block:: text

    role environment =
        platform/runtime essentials (fixed allowlist, derived here)
      + fixed per-session project runtime materialization (declared only)

There is no arbitrary ambient inheritance on this path: a project that
depends on a custom ambient variable ``FOO`` must now explicitly
declare/inherit ``FOO``.  Undeclared parent variables are never visible to
project code.  The per-session materialization is resolved ONCE at session
launch, so post-start parent mutations cannot alter the session.

**LEGACY PROJECT AMBIENT bridge (retired from the normal product path).**
The V2-01 transitional compatibility snapshot
(``legacy-project-ambient/v1``) remains ONLY for test-only compatibility
and legacy direct-API callers (:meth:`snapshot_process` / the plain
:class:`ExecutionEnvironment` constructor).  It must not be the path used
by a newly launched Local Project session: the worker and the source
fallback both construct through ``for_local_project``.  There is no
user-facing "full environment" switch and no full-environment escape
hatch.  :attr:`uses_legacy_bridge` lets tests prove which path an
authority took.

**Classification is provenance-based, not name-shape guessing.**  The
principal exclusion authority identifies Agentic Debugger-owned channels
structurally:

* the whole ``AGENTIC_DEBUGGER_`` namespace (repository-owned control
  plane: private UI→worker provider credential hops — including the
  dynamic ``AGENTIC_DEBUGGER_PROVIDER_<KIND>_API_KEY`` shape for generic
  providers — provider config/catalog/quarantine/secure-store control
  variables, and any future repository-owned control variable);
* the built-in provider credential environment authorities
  (``OPENCODE_API_KEY``, ``COMMAND_CODE_API_KEY``, ``OLLAMA_API_KEY``) and
  the provider CLI auth-store location authority
  (``OPENCODE_CONFIG_DIR``), centralized in
  :func:`agentic_debugger.application.provider_connections.provider_authority_environment_names`
  rather than duplicated here.

Values are never logged, repr'd, journaled, or fingerprinted by this
module.  ``__repr__`` exposes counts (and safe declaration NAMES for the
declarative path) only — never values.
"""

from __future__ import annotations

import os
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional

from agentic_debugger.application.session_runtime import (
    PLATFORM_ESSENTIAL_NAMES,
    PROJECT_RUNTIME_SPEC_VERSION,
    ProjectRuntimeEnvironmentSpec,
    SessionCapability,
    is_platform_essential_name,
    materialize_project_runtime,
    resolve_env_name_platform,
)

#: Re-exported single authority for the platform-essentials allowlist
#: (canonical owner: ``session_runtime``).  Kept importable here so
#: existing importers keep working (see ``__all__`` below).

#: Named compatibility identity of the retired transitional bridge.  Kept
#: for test-only compatibility and legacy direct-API callers; the normal
#: Local Project product path no longer uses it (see ``for_local_project``
#: and ``uses_legacy_bridge``).
BRIDGE_COMPATIBILITY_IDENTITY = "legacy-project-ambient/v1"

_NAMESPACE_PREFIX = "AGENTIC_DEBUGGER_"


class ExecutionEnvironmentError(ValueError):
    """The product execution-environment authority received invalid input."""


class ExecutionRole(Enum):
    """Product child roles the authority derives explicit environments for."""

    #: Ordinary project reproduction / regression / test commands.
    PROJECT_COMMAND = "project_command"
    #: Ordinary product PDB worker (base mapping; Windows venv identity is
    #: still applied by the established ``build_worker_env`` authority).
    PRODUCT_PDB = "product_pdb"
    #: LocalProjectVerifier command children (fixed for the whole
    #: verification; controller/model cannot mutate it afterwards).
    VERIFIER = "verifier"
    #: Terminal cleanup Git children (``git worktree prune`` / ``list``):
    #: least authority — platform essentials only, never project
    #: application variables or project secrets (cleanup needs no
    #: application state to remove a worktree).
    CLEANUP = "cleanup"


def _provider_authority_names_lower() -> frozenset:
    from agentic_debugger.application.provider_connections import (
        provider_authority_environment_names,
    )

    return frozenset(name.lower() for name in provider_authority_environment_names())


def is_control_or_provider_authority(name: str) -> bool:
    """Whether one environment *name* is an Agentic Debugger-owned
    control/model/provider authority channel (name-only classification;
    never inspects or returns values)."""
    if type(name) is not str or not name:
        return False
    uppered = name.upper()
    if uppered.startswith(_NAMESPACE_PREFIX):
        return True
    return name.lower() in _provider_authority_names_lower()


class ExecutionEnvironment:
    """The per-session product/local-session execution-environment authority.

    Two construction paths (never mixed):

    - :meth:`for_local_project` — the V2-02 declarative product path:
      platform essentials + the fixed per-session project runtime
      materialization.  Normal Local Project sessions MUST use this.
    - :meth:`snapshot_process` / the plain constructor — the retired
      LEGACY PROJECT AMBIENT bridge: test-only compatibility and legacy
      direct-API callers.  Not the product path.
    """

    __slots__ = (
        "_snapshot",
        "_project_ambient",
        "_uses_legacy_bridge",
        "_project_spec",
        "_essentials",
        "_materialized",
        "_role_proxies",
    )

    def __init__(self, snapshot: Mapping[str, str]) -> None:
        """Legacy bridge constructor (test-only / legacy direct-API use).

        Classifies the full snapshot minus Agentic Debugger-owned
        control/model/provider channels.  New product code must use
        :meth:`for_local_project` instead.
        """
        if not isinstance(snapshot, Mapping):
            raise ExecutionEnvironmentError(
                "snapshot must be a mapping of environment names to values"
            )
        copied: Dict[str, str] = {}
        for name, value in snapshot.items():
            if type(name) is not str or not name:
                raise ExecutionEnvironmentError(
                    "environment names must be non-empty strings"
                )
            if type(value) is not str:
                raise ExecutionEnvironmentError(
                    f"environment value for {name!r} must be a string"
                )
            copied[name] = value
        self._snapshot = MappingProxyType(copied)
        ambient = {
            name: value
            for name, value in copied.items()
            if not is_control_or_provider_authority(name)
        }
        self._project_ambient = MappingProxyType(ambient)
        self._uses_legacy_bridge = True
        self._project_spec: Optional[ProjectRuntimeEnvironmentSpec] = None
        essentials = {
            name: value
            for name, value in copied.items()
            if is_platform_essential_name(name)
            and not is_control_or_provider_authority(name)
        }
        self._essentials = MappingProxyType(essentials)
        self._materialized: Optional[Mapping[str, str]] = None
        self._role_proxies = MappingProxyType(
            {
                ExecutionRole.PROJECT_COMMAND: self._project_ambient,
                ExecutionRole.PRODUCT_PDB: self._project_ambient,
                ExecutionRole.VERIFIER: self._project_ambient,
                ExecutionRole.CLEANUP: self._essentials,
            }
        )

    @classmethod
    def for_local_project(
        cls,
        launch_snapshot: Mapping[str, str],
        project_spec: ProjectRuntimeEnvironmentSpec,
        *,
        platform: Any = None,
    ) -> "ExecutionEnvironment":
        """Build the declarative V2-02 product authority (normal path).

        ``launch_snapshot`` is the single fixed per-session parent view
        (copied on the boundary); declared project names resolve against
        it exactly once under the worker/canonical platform (live
        ``sys.platform`` unless overridden for tests).  Role environments
        are platform essentials plus that fixed materialization — never
        arbitrary ambient inheritance.
        """
        if not isinstance(launch_snapshot, Mapping):
            raise ExecutionEnvironmentError(
                "launch snapshot must be a mapping of environment names to values"
            )
        if type(project_spec) is not ProjectRuntimeEnvironmentSpec:
            raise ExecutionEnvironmentError(
                "project_spec must be a ProjectRuntimeEnvironmentSpec"
            )
        plat = resolve_env_name_platform(platform)
        copied: Dict[str, str] = {}
        for name, value in launch_snapshot.items():
            if type(name) is not str or not name:
                raise ExecutionEnvironmentError(
                    "environment names must be non-empty strings"
                )
            if type(value) is not str:
                raise ExecutionEnvironmentError(
                    f"environment value for {name!r} must be a string"
                )
            copied[name] = value
        try:
            materialization = materialize_project_runtime(
                project_spec, copied, platform=plat
            )
        except Exception as exc:
            raise ExecutionEnvironmentError(str(exc)) from exc
        essentials = {
            name: value
            for name, value in copied.items()
            if is_platform_essential_name(name, platform=plat)
        }
        for name in essentials:
            if is_control_or_provider_authority(name):
                raise ExecutionEnvironmentError(
                    "platform essentials must never carry a control authority"
                )
        materialized = materialization.to_child_mapping()
        for name in materialized:
            # Defense in depth (the spec ingress already rejects these):
            # a control authority must never become project runtime state,
            # and essentials must never be overridden by declarations.
            if is_control_or_provider_authority(name):
                raise ExecutionEnvironmentError(
                    f"project variable {name!r} is a control authority"
                )
            if is_platform_essential_name(name, platform=plat):
                raise ExecutionEnvironmentError(
                    f"project variable {name!r} is a platform essential "
                    "and must not be declared"
                )
        authority = cls.__new__(cls)
        authority._snapshot = MappingProxyType(copied)
        authority._project_ambient = MappingProxyType({})
        authority._uses_legacy_bridge = False
        authority._project_spec = project_spec
        authority._essentials = MappingProxyType(dict(essentials))
        authority._materialized = MappingProxyType(dict(materialized))
        project_env = MappingProxyType({**essentials, **materialized})
        authority._role_proxies = MappingProxyType(
            {
                ExecutionRole.PROJECT_COMMAND: project_env,
                ExecutionRole.PRODUCT_PDB: project_env,
                ExecutionRole.VERIFIER: project_env,
                ExecutionRole.CLEANUP: authority._essentials,
            }
        )
        return authority

    @classmethod
    def snapshot_process(cls) -> "ExecutionEnvironment":
        """Snapshot the current product ambient environment (legacy bridge).

        Test-only compatibility and legacy direct-API use.  The normal
        Local Project product path must use :meth:`for_local_project`.
        """
        return cls(dict(os.environ))

    @property
    def uses_legacy_bridge(self) -> bool:
        """Whether this authority took the retired bridge path (not product)."""
        return self._uses_legacy_bridge

    @property
    def available_capabilities(self) -> frozenset:
        """What this machine/session CAN physically provide (V2-02 minimal).

        The current machine provides the full small capability vocabulary
        (project commands via CommandRunner, product PDB, patch
        application, independent verification); task/product policy and
        the agent request narrow it per session into
        EffectiveSessionCapabilities.  Future stages may refine this per
        platform without changing the intersection contract.
        """
        return frozenset(
            {
                SessionCapability.PROJECT_COMMAND,
                SessionCapability.PDB,
                SessionCapability.PATCH,
                SessionCapability.VERIFIER,
            }
        )

    @property
    def bridge_identity(self) -> str:
        if self._uses_legacy_bridge:
            return BRIDGE_COMPATIBILITY_IDENTITY
        return PROJECT_RUNTIME_SPEC_VERSION

    @property
    def project_spec(self) -> Optional[ProjectRuntimeEnvironmentSpec]:
        """The declarative spec (None on the legacy bridge path)."""
        return self._project_spec

    def excluded_name_count(self) -> int:
        """How many snapshot names were classified as Agentic
        Debugger-owned authority channels (count only; never values)."""
        if self._uses_legacy_bridge:
            return len(self._snapshot) - len(self._project_ambient)
        count = 0
        for name in self._snapshot:
            if is_control_or_provider_authority(name):
                count += 1
        return count

    def role_environment(self, role: ExecutionRole) -> Mapping[str, str]:
        """Derived child-environment mapping for one explicit product role.

        Declarative path: every project role receives platform essentials
        plus the fixed per-session materialization; CLEANUP receives
        essentials only.  Legacy path: every project role receives the
        LEGACY PROJECT AMBIENT mapping (test-only compatibility).
        Mappings are immutable and session-stable.
        """
        if not isinstance(role, ExecutionRole):
            raise ExecutionEnvironmentError("role must be an ExecutionRole")
        return self._role_proxies[role]

    def __repr__(self) -> str:
        if self._uses_legacy_bridge:
            return (
                f"ExecutionEnvironment(bridge={BRIDGE_COMPATIBILITY_IDENTITY!r}, "
                f"variables={len(self._snapshot)}, "
                f"excluded={self.excluded_name_count()})"
            )
        spec = self._project_spec
        declared = len(spec.declared_names()) if spec is not None else 0
        materialized = len(self._materialized) if self._materialized is not None else 0
        return (
            f"ExecutionEnvironment(project_runtime={PROJECT_RUNTIME_SPEC_VERSION!r}, "
            f"variables={len(self._snapshot)}, "
            f"declared={declared}, materialized={materialized})"
        )


__all__ = [
    "BRIDGE_COMPATIBILITY_IDENTITY",
    "ExecutionEnvironment",
    "ExecutionEnvironmentError",
    "ExecutionRole",
    "PLATFORM_ESSENTIAL_NAMES",
    "is_control_or_provider_authority",
]
