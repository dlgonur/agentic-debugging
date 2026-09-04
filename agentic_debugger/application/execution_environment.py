"""V2-01 product execution-environment authority (Local Project sessions).

One authority per session snapshots and classifies the product execution
environment once and derives explicit, role-scoped child environments for
the product child roles this stage touches: ordinary project/reproduction/
test commands, the ordinary product PDB worker, and LocalProjectVerifier
command children.  Child process sites no longer inspect ``os.environ``
themselves; the session/source boundary constructs this authority and
threads its derived mappings explicitly to every consumer.

**LEGACY PROJECT AMBIENT bridge (transitional, V2-01 only).**  The V2
target is a positive/declarative ``ProjectRuntimeEnvironment``; its
product/session ingress does not exist yet (it arrives in V2-02 as
``ProjectRuntimeEnvironmentSpec``).  Until then, project roles receive a
classified compatibility snapshot of the ordinary ambient environment:

    LEGACY PROJECT AMBIENT =
        current product ambient environment
      MINUS Agentic Debugger-owned control/model/provider authorities

The bridge is project-role-only.  It must never feed the model adapter or
model prompts (the adapter keeps its own accepted transport environment
authority), never journal or fingerprint environment values, never add
values to diagnostics, and never become a user-facing "full environment"
mode.  There is no environment-wide rollback switch.

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

Everything else — including ordinary operator ambient network/trust state
such as ``HTTPS_PROXY``/``NO_PROXY``/CA variables — passes through the
bridge unchanged.  Per the accepted architecture, those generic ambient
names are NOT stripped merely because the provider transport also happens
to consume the same parent variables: V2-01 has no project-network
authorization ingress able to distinguish the operator's intended project
network policy.  That residual compatibility risk is documented and is
retired by the V2-02 ingress.  What V2-01 guarantees is provenance: no
provider/model child environment or provider-derived transport override is
ever merged or copied into a project role.

Values are never logged, repr'd, journaled, or fingerprinted by this
module.  ``__repr__`` exposes counts only.
"""

from __future__ import annotations

import os
from enum import Enum
from types import MappingProxyType
from typing import Dict, Mapping

#: Named compatibility identity of the transitional bridge.  The bridge is
#: removed once the V2-02 ``ProjectRuntimeEnvironmentSpec`` ingress exists
#: and ordinary Local Project execution no longer requires legacy ambient
#: inheritance (docs/architecture/agentic-debugger-v2-plan.md §6.3, §11).
BRIDGE_COMPATIBILITY_IDENTITY = "legacy-project-ambient/v1"

_NAMESPACE_PREFIX = "AGENTIC_DEBUGGER_"


class ExecutionEnvironmentError(ValueError):
    """The product execution-environment authority received invalid input."""


class ExecutionRole(Enum):
    """Product child roles V2-01 derives explicit environments for."""

    #: Ordinary project reproduction / regression / test commands.
    PROJECT_COMMAND = "project_command"
    #: Ordinary product PDB worker (base mapping; Windows venv identity is
    #: still applied by the established ``build_worker_env`` authority).
    PRODUCT_PDB = "product_pdb"
    #: LocalProjectVerifier command children (fixed for the whole
    #: verification; controller/model cannot mutate it afterwards).
    VERIFIER = "verifier"


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

    Constructed once at the session/source boundary from a single snapshot
    (``snapshot_process``); consumers receive explicit derived role
    mappings.  The snapshot is copied on the boundary and never mutated;
    derived role mappings are immutable and their derivation is
    deterministic, so one session has stable bridge semantics.
    """

    __slots__ = ("_snapshot", "_project_ambient")

    def __init__(self, snapshot: Mapping[str, str]) -> None:
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

    @classmethod
    def snapshot_process(cls) -> "ExecutionEnvironment":
        """Snapshot the current product ambient environment exactly once."""
        return cls(dict(os.environ))

    @property
    def bridge_identity(self) -> str:
        return BRIDGE_COMPATIBILITY_IDENTITY

    def excluded_name_count(self) -> int:
        """How many snapshot names were classified as Agentic
        Debugger-owned authority channels (count only; never values)."""
        return len(self._snapshot) - len(self._project_ambient)

    def role_environment(self, role: ExecutionRole) -> Mapping[str, str]:
        """Derived child-environment mapping for one explicit product role.

        V2-01 bridge semantics: every project role receives the same
        LEGACY PROJECT AMBIENT mapping.  The roles are still distinct
        identities so later stages can tighten them independently.
        """
        if not isinstance(role, ExecutionRole):
            raise ExecutionEnvironmentError("role must be an ExecutionRole")
        return self._project_ambient

    def __repr__(self) -> str:
        return (
            f"ExecutionEnvironment(bridge={BRIDGE_COMPATIBILITY_IDENTITY!r}, "
            f"variables={len(self._snapshot)}, "
            f"excluded={self.excluded_name_count()})"
        )


__all__ = [
    "BRIDGE_COMPATIBILITY_IDENTITY",
    "ExecutionEnvironment",
    "ExecutionEnvironmentError",
    "ExecutionRole",
    "is_control_or_provider_authority",
]
