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

**V2-02 project-secret egress seal (repair 10).**  The declarative path
derives ONE per-session project-secret redaction authority from the same
fixed materialization that supplies the role child environments
(:class:`ProjectSecretRedactor`, exposed via
:meth:`ExecutionEnvironment.project_secret_redactor`).  The project child
receives the exact raw secret; text (and string-bearing structures)
crossing from the project execution domain back into the Agentic Debugger
control/model/evidence domain (executor command output, product PDB
responses, product verifier evidence) is redacted through that one
authority before exposure.  The legacy bridge path exposes no redaction
authority (``None``) and keeps its historical behavior.

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
import re
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

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


#: Deterministic replacement marker for redacted raw project-secret values.
#: The secret NAME inside the marker is already safe durable provenance
#: (the spec carries names durably); the VALUE never appears.
PROJECT_SECRET_MARKER_TEMPLATE = "<PROJECT_SECRET:{name}>"

#: Bounded-text truncation marker appended by Agentic Debugger's own PDB
#: worker when it renders a bounded exception/diagnostic text
#: (``runtime/pdb_worker.py::_POST_MORTEM_TRUNCATION_MARKER``).  A text that
#: ends with this marker is an explicitly application-truncated prefix, so a
#: raw secret fragment at that cut boundary is application-owned egress and
#: must be redacted (see :meth:`ProjectSecretRedactor.redact_bounded_text`).
#: Kept here as a literal (with a drift test) so the application layer never
#: imports the generic PDB worker module for redaction purposes.
PDB_BOUNDED_TEXT_TRUNCATION_MARKER = "…"


class ProjectSecretRedactor:
    """One session's project-secret OUTPUT redaction authority.

    Derived ONCE by :meth:`ExecutionEnvironment.for_local_project` from the
    SAME
    :class:`~agentic_debugger.application.session_runtime.ProjectRuntimeMaterialization`
    that supplies the role child environments: the redaction values are the
    materialized values of exactly those declarations whose provenance kind
    is ``secret``.  The project child still receives the exact raw secret;
    text (and string-bearing structures) crossing from the project execution
    domain back into the Agentic Debugger control/model/evidence domain is
    redacted through this one per-session authority first.

    Honest scope: this seals the application-owned raw-value boundary
    (``Agentic Debugger will not directly propagate the RAW materialized
    project-secret value across the product execution-result boundary``).
    It is not a hostile-project DLP system: a trusted project that
    deliberately transforms, encodes, hashes, splits, or writes a secret
    into unrelated files is not detected, and no such claim is made.

    Repair 11 (application-owned bounding) strengthens the same authority:
    when Agentic Debugger ITSELF bounds/truncates/previewed project output
    BEFORE this redactor sees it, the complete secret no longer exists in
    the text and plain replacement cannot match.  The redactor therefore
    also owns those known application-created cut boundaries: pre-bounding
    stream sanitization (:meth:`stream_sanitizer_factory` — the complete
    value is redacted before any head/tail truncation can cut it), marked
    bounded texts (:meth:`redact_bounded_text`), and explicitly marked
    bounded string-preview structures (:meth:`redact_truncated_string_preview`
    via :meth:`redact_structure`).  These operations remain deterministic,
    structure-driven (never generic substring guessing), and expose no
    secret values.

    Non-serializable by construction: no ``to_mapping``/``secret_values``
    API exists, pickling/copying fails closed, the repr exposes counts
    only, and the redaction dictionary is never rendered, logged, or
    journaled.  Replacement is deterministic: non-empty exact values only
    (an empty secret value contains no bytes to redact), longest value
    first with the name as tiebreak, and one single-pass combined match so
    inserted markers are never rescanned.
    """

    __slots__ = ("_bindings", "_pattern", "_markers")

    def __init__(self, bindings: Tuple[Tuple[str, str], ...]) -> None:
        from agentic_debugger.application.session_runtime import (
            validate_env_name,
        )

        if isinstance(bindings, (str, bytes)) or not isinstance(
            bindings, (list, tuple)
        ):
            raise ExecutionEnvironmentError(
                "secret bindings must be a tuple of (name, value) pairs"
            )
        seen: Dict[str, str] = {}
        for entry in bindings:
            if (
                type(entry) is not tuple
                or len(entry) != 2
                or type(entry[0]) is not str
                or type(entry[1]) is not str
            ):
                raise ExecutionEnvironmentError(
                    "secret bindings must be (name, value) string pairs"
                )
            validate_env_name(entry[0])
            if entry[0] in seen:
                raise ExecutionEnvironmentError(
                    f"duplicate secret binding for {entry[0]!r}"
                )
            seen[entry[0]] = entry[1]
        # Deterministic order: longest exact value first, name as the
        # tiebreak.  Empty values are excluded explicitly (no bytes to
        # redact; naive empty-string replacement would be degenerate).
        ordered = tuple(
            sorted(
                ((name, value) for name, value in seen.items() if value != ""),
                key=lambda item: (-len(item[1]), item[0]),
            )
        )
        self._bindings = ordered
        if ordered:
            self._pattern: Any = re.compile(
                "|".join(re.escape(value) for _, value in ordered)
            )
            markers: Dict[str, str] = {}
            for name, value in ordered:
                markers.setdefault(
                    value, PROJECT_SECRET_MARKER_TEMPLATE.format(name=name)
                )
            self._markers: Any = MappingProxyType(markers)
        else:
            self._pattern = None
            self._markers = MappingProxyType({})

    @classmethod
    def from_materialization(
        cls, materialization: Any
    ) -> "ProjectSecretRedactor":
        """Derive the redactor from the fixed session materialization.

        Only declarations whose provenance kind is ``secret`` contribute
        values; explicit values and benign inherits never do.
        """
        from agentic_debugger.application.session_runtime import (
            ProjectRuntimeMaterialization,
        )

        if type(materialization) is not ProjectRuntimeMaterialization:
            raise ExecutionEnvironmentError(
                "materialization must be a ProjectRuntimeMaterialization"
            )
        bindings = tuple(
            (name, materialization.resolved[name])
            for name, kind, _required in materialization.provenance
            if kind == "secret"
        )
        return cls(bindings)

    def redact(self, text: str) -> str:
        """Return ``text`` with every raw materialized secret value replaced.

        Deterministic and bounded: values that never occur leave the text
        unchanged; each occurrence is replaced by
        ``<PROJECT_SECRET:NAME>``.
        """
        if type(text) is not str:
            raise ExecutionEnvironmentError(
                "project-secret redaction operates on text values only"
            )
        if self._pattern is None or not text:
            return text
        return self._pattern.sub(
            lambda match: self._markers[match.group(0)], text
        )

    def _holdback(self) -> int:
        """Streaming holdback: a proper prefix of a secret has at most
        ``longest secret length - 1`` characters, so holding back that many
        redacted characters between feeds can never lose a cut secret."""
        return (len(self._bindings[0][1]) - 1) if self._bindings else 0

    def _longest_trailing_proper_prefix(self, text: str) -> Tuple[int, str]:
        """Longest suffix of ``text`` that is a PROPER prefix of one secret.

        Returns ``(length, marker)``; ``(0, "")`` when no suffix matches.
        Deterministic: the longest length wins; binding order (longest
        value first, name tiebreak) decides equal-length ties.  Only ever
        applied to text that Agentic Debugger itself is known to have cut
        (a marked bounded text or a marked truncated preview) — never as a
        generic heuristic on arbitrary project output.
        """
        best_k = 0
        best_marker = ""
        for _name, value in self._bindings:
            limit = min(len(value) - 1, len(text))
            for k in range(limit, best_k, -1):
                if text.endswith(value[:k]):
                    best_k = k
                    best_marker = self._markers[value]
                    break
        return best_k, best_marker

    def redact_bounded_text(self, text: str) -> str:
        """Redact complete secret values and application-cut tail fragments.

        Exact replacement first (as :meth:`redact`); then, when the text
        ends with the PDB worker's bounded-text truncation marker, the cut
        was created by Agentic Debugger and any raw fragment of a secret
        left at that boundary is replaced by the secret's marker.  Texts
        without the marker are never boundary-scanned: no guessing on
        unmarked output.
        """
        redacted = self.redact(text)
        if self._pattern is None or not redacted:
            return redacted
        marker = PDB_BOUNDED_TEXT_TRUNCATION_MARKER
        if not redacted.endswith(marker):
            return redacted
        candidate = redacted[: -len(marker)]
        cut, cut_marker = self._longest_trailing_proper_prefix(candidate)
        if cut:
            return candidate[: -cut] + cut_marker + marker
        return redacted

    def redact_truncated_string_preview(self, preview: str) -> str:
        """Redact a bounded string preview that WE truncated.

        Applied only to explicitly marked bounded-preview structures (the
        PDB worker's ``truncated: True`` string summaries): the complete
        original string no longer exists, so besides exact replacement any
        trailing raw fragment of a secret left by the application's own
        cut is replaced by the secret's marker.  Deterministic and
        structure-scoped; never a generic substring search.
        """
        redacted = self.redact(preview)
        if self._pattern is None or not redacted:
            return redacted
        cut, cut_marker = self._longest_trailing_proper_prefix(redacted)
        if cut:
            return redacted[: -cut] + cut_marker
        return redacted

    def redact_structure(self, value: Any) -> Any:
        """Recursively redact strings inside mappings/lists/tuples.

        Structural fields, keys, numbers, booleans, and ``None`` are
        returned unchanged; only string VALUES are redacted.

        The PDB worker's explicitly marked bounded string summaries
        (``kind == "str"`` with ``truncated is True`` and a string
        ``value``) are redacted through
        :meth:`redact_truncated_string_preview`: the structure proves
        Agentic Debugger performed the cut, so the cut-created fragment of
        a longer secret is removed while ``kind``/``type``/``size``/
        ``truncated`` and the collection structure stay untouched.
        """
        if type(value) is str:
            return self.redact(value)
        if isinstance(value, Mapping):
            if (
                value.get("kind") == "str"
                and value.get("truncated") is True
                and type(value.get("value")) is str
            ):
                return {
                    key: (
                        self.redact_truncated_string_preview(item)
                        if key == "value"
                        else self.redact_structure(item)
                    )
                    for key, item in value.items()
                }
            return {
                key: self.redact_structure(item)
                for key, item in value.items()
            }
        if type(value) is list:
            return [self.redact_structure(item) for item in value]
        if type(value) is tuple:
            return tuple(self.redact_structure(item) for item in value)
        return value

    def stream_sanitizer_factory(self) -> Callable[[], "_ProjectSecretStreamSanitizer"]:
        """Zero-arg factory producing per-stream pre-bounding sanitizers.

        Each product supplies ONE factory to its
        :class:`~agentic_debugger.runtime.command_runner.CommandRunner`;
        the runner calls it once per output stream (stdout/stderr) so each
        stream keeps independent holdback state.  The sanitizer applies
        :meth:`redact` incrementally to the complete decoded stream text
        BEFORE the runner's head/tail bounding can cut it, so the
        application's own truncation can never manufacture a raw secret
        fragment.  The returned objects expose only ``feed``/``flush`` and
        hold no secret material beyond what this authority already holds.
        """
        redactor = self

        def _factory() -> "_ProjectSecretStreamSanitizer":
            return _ProjectSecretStreamSanitizer(redactor)

        return _factory

    def __getstate__(self) -> None:
        raise TypeError(
            "project secret redaction authority must never be serialized"
        )

    def __repr__(self) -> str:
        return (
            f"ProjectSecretRedactor(redactable_secrets={len(self._bindings)})"
        )


class _ProjectSecretStreamSanitizer:
    """Incremental pre-bounding sanitizer for ONE command output stream.

    The neutral seam object consumed by
    :class:`~agentic_debugger.runtime.command_runner.CommandRunner` (plain
    duck-typed ``feed``/``flush`` — the runtime never imports this module).
    Between feeds it holds back the last ``longest secret length - 1``
    redacted characters, so a secret value whose bytes arrive across
    reader-thread chunks is still matched and replaced exactly once by the
    single-pass authority; the complete value is therefore always removed
    from the stream BEFORE any head/tail bounding can cut it, and ordinary
    (secret-free) text passes through unchanged byte for byte.
    """

    __slots__ = ("_redactor", "_pending")

    def __init__(self, redactor: "ProjectSecretRedactor") -> None:
        self._redactor = redactor
        self._pending: str = ""

    def feed(self, text: str) -> str:
        """Sanitize one decoded chunk; returns the emit-safe prefix."""
        if not text:
            return ""
        buf = self._pending + text
        hold = self._redactor._holdback()
        if hold <= 0:
            # No secrets (or only single-character values): every match is
            # complete within any buffer, nothing can be half-cut.
            processed = self._redactor.redact(buf)
            self._pending = ""
            return processed
        if len(buf) <= hold:
            self._pending = buf
            return ""
        processed = self._redactor.redact(buf)
        split = len(processed) - hold
        if split <= 0:
            # Pathologically heavy replacement: keep everything pending
            # (still bounded by chunk + holdback) rather than split inside
            # possibly incomplete text.
            self._pending = processed
            return ""
        self._pending = processed[split:]
        return processed[:split]

    def flush(self) -> str:
        """Emit the final held-back text (end of stream)."""
        out = self._redactor.redact(self._pending)
        self._pending = ""
        return out

    def __repr__(self) -> str:
        return f"_ProjectSecretStreamSanitizer(pending_chars={len(self._pending)})"


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
        "_secret_redactor",
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
        # The legacy bridge predates the declared project-secret contract;
        # it carries no redaction authority and keeps its historical
        # (unredacted) behavior for test-only/legacy callers.
        self._secret_redactor: Optional[ProjectSecretRedactor] = None
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
        # V2-02 egress seal: one redaction authority per session, derived
        # from the SAME materialization as the role child environments (no
        # re-resolution from os.environ, no second secret authority).
        authority._secret_redactor = ProjectSecretRedactor.from_materialization(
            materialization
        )
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

    def project_secret_redactor(self) -> Optional["ProjectSecretRedactor"]:
        """The one per-session project-secret redaction authority.

        Declarative path: a :class:`ProjectSecretRedactor` derived from the
        same fixed materialization as the role child environments (a
        no-op redactor when no secret is declared).  Legacy bridge path:
        ``None`` (historical behavior, no declared-secret contract).
        """
        return self._secret_redactor

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
    "PDB_BOUNDED_TEXT_TRUNCATION_MARKER",
    "PLATFORM_ESSENTIAL_NAMES",
    "PROJECT_SECRET_MARKER_TEMPLATE",
    "ProjectSecretRedactor",
    "is_control_or_provider_authority",
]
