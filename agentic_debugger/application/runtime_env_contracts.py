"""Session/runtime error, capability, and environment-name identity contracts.

This module owns the shared V2-02 contract primitives of the product
session/runtime architecture:

- the fail-closed error vocabulary (:class:`SessionRuntimeError` and its
  specialized subclasses);
- the small :class:`SessionCapability` product capability vocabulary;
- environment-variable NAME identity (platform-correct canonicalization,
  the platform-essentials allowlist, control/provider-authority rejection)
  — names only, never values.

These primitives are consumed by the runtime-environment spec layer
(:mod:`agentic_debugger.application.runtime_env_spec`) and the launch
facade (:mod:`agentic_debugger.application.session_runtime`).  Secret
VALUES never pass through this module.
"""

from __future__ import annotations

import re
import sys
from enum import Enum
from typing import Any, Optional

from agentic_debugger.application.events import contains_credential_shape

_MAX_ID_CHARS = 128
_MAX_ENV_NAME_CHARS = 128

_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

#: Platform/runtime essentials allowlist (canonical uppercase spelling).
#: These are NOT user project declarations: they are the fixed
#: Windows/POSIX execution essentials plus interpreter-locale state the
#: platform needs (``PATH`` for executable/Git resolution,
#: ``SystemRoot``/drive vars for Windows process startup, temp dirs,
#: home/profile dirs for tool config reads, ``APPDATA``/``LOCALAPPDATA``
#: so the interpreter resolves the same per-user site-packages as the
#: parent, locale).  Everything else a project needs must be explicitly
#: declared in the ProjectRuntimeEnvironmentSpec.  Deliberately excludes
#: interpreter-override state (``PYTHONPATH``/``PYTHONHOME``/``VIRTUAL_ENV``:
#: declare them if the project needs them), version-control state
#: (``GIT_*``), and every Agentic Debugger control/provider authority.
#: Single authority for this list: execution_environment.py re-exports it.
PLATFORM_ESSENTIAL_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    }
)


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


def resolve_env_name_platform(platform: Any = None) -> str:
    """Resolve the platform governing environment-name identity.

    Returns the live ``sys.platform`` when ``platform`` is None (the
    worker/canonical behavior); an explicit value keeps tests deterministic
    on any host without depending on running Windows.  Follows the
    established injectable-platform pattern (cf. ``python_launcher``).
    """
    if platform is None:
        return sys.platform
    if type(platform) is not str or not platform:
        raise SessionRuntimeError("platform must be a non-empty string")
    return platform


def canonical_env_name(name: str, *, platform: Any = None) -> str:
    """Canonical identity for one environment variable NAME (never a value).

    Environment variable names are case-insensitive on Windows and
    case-sensitive on POSIX: on ``win32`` the canonical form is the
    uppercased name, elsewhere the name itself.  The original spelling is
    always preserved by callers for safe provenance/UI; the canonical form
    is used ONLY for identity comparisons (duplicate detection,
    snapshot lookup, essential/control-authority matching).  Secret VALUES
    are never passed here and never inspected.
    """
    validate_env_name(name)
    if resolve_env_name_platform(platform) == "win32":
        return name.upper()
    return name


def is_platform_essential_name(name: str, *, platform: Any = None) -> bool:
    """Whether one environment NAME is a platform/runtime essential.

    Platform-correct: case-insensitive on Windows (``Path`` collides with
    ``PATH`` there), exact-match on POSIX (``path`` and ``PATH`` are
    distinct variables there).
    """
    if type(name) is not str or not name:
        return False
    if resolve_env_name_platform(platform) == "win32":
        return name.upper() in PLATFORM_ESSENTIAL_NAMES
    return name in PLATFORM_ESSENTIAL_NAMES


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
