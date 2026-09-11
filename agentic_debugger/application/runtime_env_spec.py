"""The explicit Local Project runtime-environment ingress and materialization.

This module owns the project runtime-environment declaration layer:

- :class:`ProjectExplicitValue` / :class:`ProjectEnvDeclaration` — one
  declared explicit value or declared variable NAME (by-name binding);
- :class:`ProjectRuntimeEnvironmentSpec` — the immutable explicit ingress
  (values, inherit names, project-secret binding NAMES) with strict
  declaration validation, platform-correct duplicate/essential rejection,
  safe serialization, and safe fingerprinting;
- the worker transport pair :func:`spec_to_param`/:func:`spec_from_param`;
- :class:`ProjectRuntimeMaterialization` and
  :func:`materialize_project_runtime` — the ONE fixed per-session resolved
  runtime held only in trusted session-process memory.

Secret VALUES are never stored in the spec, never serialized, never
journaled, never fingerprinted, never repr'd.  Resolved values (which may
include project secrets) live only in the in-memory materialization.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

from agentic_debugger.application.events import contains_credential_shape
from agentic_debugger.application.runtime_env_contracts import (
    ProjectRuntimeError,
    SessionRuntimeError,
    _reject_control_authority_name,
    canonical_env_name,
    is_platform_essential_name,
    resolve_env_name_platform,
    validate_env_name,
)

#: Version of the project-runtime ingress contract (durable provenance).
PROJECT_RUNTIME_SPEC_VERSION = "project-runtime/v1"

#: Transport bound: the worker start pipe carries scenario params as
#: strings of at most 4096 bytes, so the serialized spec must fit.
SPEC_PARAM_MAX_CHARS = 4096

_MAX_DECLARATIONS_PER_CATEGORY = 32
_MAX_EXPLICIT_VALUE_BYTES = 1024


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

    def validate_for_platform(self, platform: Any = None) -> None:
        """Fail closed on platform-specific declaration conflicts.

        Applies the worker/canonical platform's environment-name identity
        (case-insensitive on Windows, case-sensitive on POSIX) to:

        - duplicate detection within AND across all three categories
          (on Windows ``FOO``/``foo`` — e.g. ``inherit: FOO`` plus
          ``secret: foo``, or explicit value ``Foo`` plus inherit ``FOO``
          — are one variable and are rejected; on POSIX distinct
          spellings remain distinct);
        - platform-essential collisions (``PATH`` everywhere; ``Path`` or
          ``SystemRoot``-case variants additionally on Windows): essentials
          are derived by the execution environment and must never be
          declared.

        The worker always runs this with its own platform before
        materializing, so a duplicate that is only judgable
        platform-specifically still fails closed even if the declaring UI
        ran its readiness check elsewhere.  Name-only errors; values are
        never mentioned (secret values do not exist in this object).
        """
        plat = resolve_env_name_platform(platform)
        seen: Dict[str, str] = {}

        def _claim(original: str, kind: str) -> None:
            canonical = canonical_env_name(original, platform=plat)
            if canonical in seen:
                raise SessionRuntimeError(
                    f"project variable {original!r} is declared more than once"
                )
            seen[canonical] = kind
            if is_platform_essential_name(original, platform=plat):
                raise SessionRuntimeError(
                    f"project variable {original!r} is a platform essential "
                    "and must not be declared"
                )

        for entry in self.values:
            _claim(entry.name, "explicit value")
        for entry in self.inherit:
            _claim(entry.name, "inherit")
        for entry in self.secrets:
            _claim(entry.name, "secret")

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
    *,
    platform: Any = None,
) -> ProjectRuntimeMaterialization:
    """Resolve declared NAMES once against the launch snapshot.

    The snapshot is the single fixed per-session parent view (copied on
    the boundary); every later child derives from the returned fixed
    mapping.  Lookup uses the platform's environment-name identity
    (case-insensitive on Windows: a ``MyProjectFlag`` declaration resolves
    a ``MYPROJECTFLAG`` snapshot key; case-sensitive on POSIX).  A Windows
    snapshot carrying conflicting case variants of one variable fails
    closed (name-only error) rather than picking nondeterministically.
    Missing REQUIRED names fail closed with a safe name-only error;
    missing optional names are skipped.  No value is ever logged,
    fingerprinted, or rendered.
    """
    if type(spec) is not ProjectRuntimeEnvironmentSpec:
        raise ProjectRuntimeError("spec must be a ProjectRuntimeEnvironmentSpec")
    if not isinstance(launch_snapshot, Mapping):
        raise ProjectRuntimeError("launch snapshot must be a mapping")
    for name, val in launch_snapshot.items():
        if type(name) is not str or type(val) is not str:
            raise ProjectRuntimeError("launch snapshot must map strings to strings")
    plat = resolve_env_name_platform(platform)
    try:
        spec.validate_for_platform(plat)
    except SessionRuntimeError as exc:
        raise ProjectRuntimeError(str(exc)) from exc

    # Canonical snapshot index (original spellings retained for the fixed
    # mapping; canonical forms for lookup only — values never inspected).
    index: Dict[str, Tuple[str, str]] = {}
    for name, val in launch_snapshot.items():
        try:
            canonical = canonical_env_name(name, platform=plat)
        except SessionRuntimeError:
            continue
        if canonical in index and index[canonical][0] != name:
            raise ProjectRuntimeError(
                f"Conflicting launch environment variables for {canonical!r}."
            )
        index.setdefault(canonical, (name, val))

    def _lookup(name: str) -> Optional[Tuple[str, str]]:
        return index.get(canonical_env_name(name, platform=plat))

    resolved: Dict[str, str] = {}
    provenance: list[Tuple[str, str, bool]] = []
    for entry in spec.values:
        resolved[entry.name] = entry.value
        provenance.append((entry.name, "value", True))
    for entry in spec.inherit:
        found = _lookup(entry.name)
        if found is not None:
            resolved[found[0]] = found[1]
            provenance.append((found[0], "inherit", entry.required))
        elif entry.required:
            raise ProjectRuntimeError(
                f"Required project environment variable {entry.name} is unavailable."
            )
    for entry in spec.secrets:
        found = _lookup(entry.name)
        if found is not None:
            resolved[found[0]] = found[1]
            provenance.append((found[0], "secret", entry.required))
        elif entry.required:
            raise ProjectRuntimeError(
                f"Required project environment variable {entry.name} is unavailable."
            )
    return ProjectRuntimeMaterialization(
        spec_version=spec.version,
        resolved=resolved,
        provenance=tuple(sorted(provenance)),
    )
