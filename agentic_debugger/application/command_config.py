"""App-owned validated command-model configuration (Task 8).

The Local Application V1 supports one configured local execution mode: a
user-defined command model profile executed through the accepted existing
JSON-lines command transport (``evaluation.live.JsonlCommandTransport``
protocol).  This module owns the smallest professional V1 configuration
surface:

- :class:`CommandModelProfile` — one validated, immutable profile (stable
  profile id, display name, explicit executable + argv, optional working
  directory, request timeout, optional bounded environment overrides, and
  protocol/version metadata);
- :class:`CommandModelConfigStore` — one bounded app-owned configuration
  location (``<app-root>/config/command-models.json``) with deterministic
  loading, strict schema validation, clear startup errors, and no recursive
  discovery, no automatic execution, no hidden migration, and no database.

Security boundary rules (Task 8 Part A4/A5):

- The configuration is loaded with ``json.loads`` only.  No YAML object
  constructors, no Python config execution, no code evaluation of any kind.
- Execution is always explicit ``argv`` through ``shell=False``; there is
  no implicit ``cmd /c`` / PowerShell evaluation and no shell
  metacharacter interpretation by the application.
- The executable must be either a bare command name (resolved through the
  process ``PATH`` like any explicit CLI) or an absolute path; a relative
  path with separators is rejected as ambiguous.
- Credential-shaped values (argv tokens, environment overrides, display
  names, paths) are rejected fail-closed, so no secret literal can be
  persisted into history, rendered in the UI, or serialized into a
  configuration fingerprint.
- Environment overrides are bounded explicit values; the inherited process
  environment is never serialized into evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Mapping, Optional, Tuple

from agentic_debugger.application import (
    ApplicationError,
    ApplicationInputError,
)
from agentic_debugger.application.events import (
    contains_credential_shape,
    is_credential_name,
)
from agentic_debugger.evaluation.live import LIVE_PROTOCOL_VERSION

__all__ = [
    "COMMAND_CONFIG_SCHEMA_VERSION",
    "COMMAND_MODELS_FILE_NAME",
    "CONFIG_DIR_NAME",
    "CommandConfigError",
    "CommandConfigNotFoundError",
    "CommandModelConfigStore",
    "CommandModelProfile",
    "ProfileSummary",
]

COMMAND_CONFIG_SCHEMA_VERSION = "command-models-v1"
CONFIG_DIR_NAME = "config"
COMMAND_MODELS_FILE_NAME = "command-models.json"

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Credential-shaped *argument* tokens: ``--api-key=...`` / ``--token ...``
#: style flags are rejected even when the value alone would not match
#: ``contains_credential_shape`` (mirrors the accepted live-command policy).
_ARGUMENT_SECRET_RE = re.compile(
    r"^--?(?:api[_-]?key|access[_-]?token|authorization|credential|"
    r"password|secret|private[_-]?key|token)(?:=|$)",
    re.I,
)

_MAX_PROFILE_ID_CHARS = 64
_MAX_DISPLAY_NAME_CHARS = 128
_MAX_EXECUTABLE_CHARS = 2048
_MAX_ARGV_ENTRY_CHARS = 512
_MAX_ARGV_ENTRIES = 31  # plus the executable = the accepted live 32-item cap
_MAX_CWD_CHARS = 2048
_MAX_ENV_OVERRIDES = 8
_MAX_ENV_NAME_CHARS = 128
_MAX_ENV_VALUE_CHARS = 512
_MAX_PROTOCOL_CHARS = 64
_MAX_TOOL_VERSION_CHARS = 64
_MIN_TIMEOUT_SECONDS = 1.0
_MAX_TIMEOUT_SECONDS = 300.0
_MAX_CONFIG_FILE_BYTES = 256 * 1024


class CommandConfigError(ApplicationError):
    """Base class for command-model configuration failures."""


class CommandConfigNotFoundError(CommandConfigError):
    """The requested profile id is not defined by the app-owned config."""


def _reject_control(value: str, label: str) -> str:
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise CommandConfigError(f"{label} contains control characters")
    return value


def _bounded_text(value: Any, label: str, max_chars: int) -> str:
    if type(value) is not str or not value:
        raise CommandConfigError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise CommandConfigError(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise CommandConfigError(f"{label} exceeds the {max_chars}-byte bound")
    _reject_control(value, label)
    return value


def _bounded_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, label, max_chars)


def _bounded_float(value: Any, label: str, minimum: float, maximum: float) -> float:
    if type(value) is not int and type(value) is not float:
        raise CommandConfigError(f"{label} must be a number")
    if isinstance(value, bool):
        raise CommandConfigError(f"{label} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise CommandConfigError(f"{label} must be finite")
    if not minimum <= number <= maximum:
        raise CommandConfigError(
            f"{label} must be within [{minimum}, {maximum}] seconds"
        )
    return number


def _reject_credential(value: str, label: str) -> str:
    if contains_credential_shape(value):
        raise CommandConfigError(f"{label} contains a credential-shaped value")
    return value


def _validated_protocol_version(value: Any) -> str:
    """Pin ``protocol_version`` to the one truthful wire protocol.

    The command-model runtime speaks exactly the accepted live JSON-lines
    protocol (``evaluation.live.LIVE_PROTOCOL_VERSION``).  The config field
    is therefore only an explicit compatibility assertion: any value that is
    not the actually supported runtime protocol is rejected fail-closed, so
    ``model.configured.protocol_version`` can never persist false
    provenance.  An omitted field defaults to the runtime constant.
    """
    text = _reject_credential(
        _bounded_text(value, "protocol_version", _MAX_PROTOCOL_CHARS),
        "protocol_version",
    )
    if text != LIVE_PROTOCOL_VERSION:
        raise CommandConfigError(
            "protocol_version must equal the supported runtime protocol "
            f"{LIVE_PROTOCOL_VERSION!r}; got {text!r}"
        )
    return text


def _validated_executable(value: Any) -> str:
    """Validate the executable without accepting shell-string ambiguity.

    Accepted intentionally:

    - a bare command name (no path separators), resolved through the
      process ``PATH`` like any explicit CLI invocation;
    - a true Windows absolute drive path (``C:\\tools\\model.exe``);
    - a true UNC absolute path (``\\\\server\\share\\model.exe``);
    - a true POSIX absolute path (``/usr/bin/python``).

    Rejected: drive-relative Windows paths (``C:relative.exe``,
    ``C:..\\evil.exe``, or a bare drive spec) and relative paths with
    separators (``.\\model.exe``, ``..\\model.exe``, ``folder/tool``).
    Their resolution depends on the per-drive current directory or an
    implicit working directory, which violates the deliberate "bare
    executable name OR absolute path" contract.  The check uses correct
    ``ntpath``/``PureWindowsPath`` semantics rather than assuming that a
    drive letter implies an absolute path.
    """
    text = _bounded_text(value, "executable", _MAX_EXECUTABLE_CHARS)
    _reject_credential(text, "executable")
    if "\x00" in text:
        raise CommandConfigError("executable contains a null byte")
    windows = PureWindowsPath(text)
    if windows.is_absolute():
        # True absolute path: drive-rooted (C:\...) or UNC (\\server\...).
        return text
    if windows.drive:
        # Has a drive letter but no root: drive-relative, not absolute.
        raise CommandConfigError(
            "executable must be a bare command name or an absolute path "
            "(drive-relative paths are ambiguous)"
        )
    if "/" not in text and "\\" not in text:
        return text
    if PurePosixPath(text).is_absolute():
        return text
    raise CommandConfigError(
        "executable must be a bare command name or an absolute path "
        "(relative paths with separators are ambiguous)"
    )


def _validated_argv(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    # The public dataclass advertises a tuple of strings; the config JSON
    # supplies a list.  Both are the same bounded sequence contract.
    if type(value) is not list and type(value) is not tuple:
        raise CommandConfigError("argv must be an array of strings or null")
    if len(value) > _MAX_ARGV_ENTRIES:
        raise CommandConfigError(
            f"argv exceeds the {_MAX_ARGV_ENTRIES}-entry bound"
        )
    result: list[str] = []
    for index, item in enumerate(value):
        text = _bounded_text(item, f"argv[{index}]", _MAX_ARGV_ENTRY_CHARS)
        if "\x00" in text:
            raise CommandConfigError(f"argv[{index}] contains a null byte")
        if _ARGUMENT_SECRET_RE.search(text) or contains_credential_shape(text):
            raise CommandConfigError(
                f"argv[{index}] contains a credential-shaped value"
            )
        result.append(text)
    return tuple(result)


def _validated_cwd(value: Any) -> Optional[str]:
    text = _bounded_text_or_none(value, "cwd", _MAX_CWD_CHARS)
    if text is None:
        return None
    if not Path(text).is_absolute():
        raise CommandConfigError("cwd must be an absolute path or null")
    if "\x00" in text:
        raise CommandConfigError("cwd contains a null byte")
    _reject_credential(text, "cwd")
    return text


def _validated_environment(value: Any) -> Tuple[Tuple[str, str], ...]:
    if value is None or value == ():
        return ()
    # The public dataclass advertises a tuple of (name, value) pairs; the
    # config JSON supplies an object (a Mapping).  Both are accepted and
    # validated identically.  A JSON array decodes to a ``list``, which is
    # NOT part of the documented config schema, so it stays rejected -- this
    # keeps the constructor self-consistent without broadening the bounded
    # config JSON.
    if isinstance(value, Mapping):
        items = list(value.items())
    elif type(value) is tuple:
        items = []
        for index, item in enumerate(value):
            if type(item) is not tuple or len(item) != 2:
                raise CommandConfigError(
                    f"environment[{index}] must be a (name, value) pair"
                )
            items.append((item[0], item[1]))
    else:
        raise CommandConfigError(
            "environment must be an object or a tuple of pairs or null"
        )
    if len(items) > _MAX_ENV_OVERRIDES:
        raise CommandConfigError(
            f"environment exceeds the {_MAX_ENV_OVERRIDES}-override bound"
        )
    result: list[tuple[str, str]] = []
    for name, item in items:
        key = _bounded_text(name, "environment name", _MAX_ENV_NAME_CHARS)
        if not _ENV_NAME_RE.fullmatch(key):
            raise CommandConfigError(
                f"environment name {key!r} is not a valid environment variable name"
            )
        if (
            _ARGUMENT_SECRET_RE.search(key)
            or contains_credential_shape(key)
            or is_credential_name(key)
        ):
            raise CommandConfigError(
                f"environment name {key!r} contains a credential-shaped value"
            )
        val = _bounded_text(item, f"environment {key}", _MAX_ENV_VALUE_CHARS)
        if "\x00" in val:
            raise CommandConfigError(f"environment {key} contains a null byte")
        if contains_credential_shape(val):
            raise CommandConfigError(
                f"environment {key} contains a credential-shaped value"
            )
        result.append((key, val))
    return tuple(sorted(result))


@dataclass(frozen=True)
class CommandModelProfile:
    """One validated, immutable configured command-model profile.

    ``argv`` excludes the executable itself; the transport invokes
    ``[executable, *argv]`` explicitly through ``shell=False``.  Only the
    bounded configuration actually required for execution is captured here;
    no live process object ever crosses a session boundary.
    """

    profile_id: str
    display_name: str
    executable: str
    argv: Tuple[str, ...] = ()
    cwd: Optional[str] = None
    request_timeout_seconds: float = 60.0
    environment: Tuple[Tuple[str, str], ...] = ()
    protocol_version: str = LIVE_PROTOCOL_VERSION
    tool_version: str = "live-command-v1"

    def __post_init__(self) -> None:
        profile_id = _bounded_text(
            self.profile_id, "profile_id", _MAX_PROFILE_ID_CHARS
        )
        if not _PROFILE_ID_RE.fullmatch(profile_id):
            raise CommandConfigError(
                "profile_id must match [a-z0-9][a-z0-9._-]{0,63}"
            )
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(
            self,
            "display_name",
            _reject_credential(
                _bounded_text(
                    self.display_name, "display_name", _MAX_DISPLAY_NAME_CHARS
                ),
                "display_name",
            ),
        )
        object.__setattr__(self, "executable", _validated_executable(self.executable))
        object.__setattr__(self, "argv", _validated_argv(self.argv))
        object.__setattr__(self, "cwd", _validated_cwd(self.cwd))
        object.__setattr__(
            self,
            "request_timeout_seconds",
            _bounded_float(
                self.request_timeout_seconds,
                "request_timeout_seconds",
                _MIN_TIMEOUT_SECONDS,
                _MAX_TIMEOUT_SECONDS,
            ),
        )
        object.__setattr__(self, "environment", _validated_environment(self.environment))
        object.__setattr__(
            self, "protocol_version", _validated_protocol_version(self.protocol_version)
        )
        object.__setattr__(
            self,
            "tool_version",
            _reject_credential(
                _bounded_text(
                    self.tool_version, "tool_version", _MAX_TOOL_VERSION_CHARS
                ),
                "tool_version",
            ),
        )
        if len(self.argv) + 1 > 32:
            raise CommandConfigError(
                "executable plus argv exceeds the 32-argument command bound"
            )

    @classmethod
    def from_mapping(cls, value: Any) -> "CommandModelProfile":
        """Strictly validate one profile mapping (fail closed)."""
        if not isinstance(value, Mapping):
            raise CommandConfigError("profile must be an object")
        known = {
            "profile_id",
            "display_name",
            "executable",
            "argv",
            "cwd",
            "request_timeout_seconds",
            "environment",
            "protocol_version",
            "tool_version",
        }
        extra = set(value.keys()) - known
        if extra:
            raise CommandConfigError(
                f"profile has unknown fields: {sorted(extra)}"
            )
        missing = {"profile_id", "display_name", "executable"} - set(value.keys())
        if missing:
            raise CommandConfigError(
                f"profile is missing required fields: {sorted(missing)}"
            )
        return cls(
            profile_id=value["profile_id"],
            display_name=value["display_name"],
            executable=value["executable"],
            argv=value.get("argv"),
            cwd=value.get("cwd"),
            request_timeout_seconds=value.get("request_timeout_seconds", 60.0),
            environment=value.get("environment"),
            protocol_version=value.get("protocol_version", LIVE_PROTOCOL_VERSION),
            tool_version=value.get("tool_version", "live-command-v1"),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": COMMAND_CONFIG_SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "executable": self.executable,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "request_timeout_seconds": self.request_timeout_seconds,
            "environment": dict(self.environment),
            "protocol_version": self.protocol_version,
            "tool_version": self.tool_version,
        }

    @property
    def configuration_fingerprint(self) -> str:
        """Stable SHA-256 over the canonical validated configuration.

        Every profile value is validated fail-closed against the
        credential policy, so the fingerprint cannot embed a secret
        literal: no secret can be present in the configuration at all.
        """
        canonical = json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def live_command(self) -> Tuple[str, ...]:
        """The explicit ``shell=False`` command tuple for the transport."""
        return (self.executable,) + self.argv

    def summary(self) -> "ProfileSummary":
        return ProfileSummary(
            profile_id=self.profile_id,
            display_name=self.display_name,
            executable=self.executable,
            request_timeout_seconds=self.request_timeout_seconds,
            protocol_version=self.protocol_version,
            tool_version=self.tool_version,
            configuration_fingerprint=self.configuration_fingerprint,
        )


@dataclass(frozen=True)
class ProfileSummary:
    """Safe concise configuration information for the UI.

    Contains no environment overrides and no argv values: the UI needs only
    enough safe provenance to let the user identify and select a profile.
    """

    profile_id: str
    display_name: str
    executable: str
    request_timeout_seconds: float
    protocol_version: str
    tool_version: str
    configuration_fingerprint: str

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "executable": self.executable,
            "request_timeout_seconds": self.request_timeout_seconds,
            "protocol_version": self.protocol_version,
            "tool_version": self.tool_version,
            "configuration_fingerprint": self.configuration_fingerprint,
        }


class CommandModelConfigStore:
    """One bounded app-owned command-model configuration location.

    The store owns ``<root>/config/command-models.json`` only.  Loading is
    deterministic and executes no code: the file is read as UTF-8 JSON,
    every profile is validated fail-closed, duplicate profile ids are
    rejected, and the resulting tuple is immutable.  A missing file is the
    empty configuration (no profiles), never an error; a malformed file is
    a typed :class:`CommandConfigError` with a bounded diagnostic.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._config_dir = self._root / CONFIG_DIR_NAME
        self._config_path = self._config_dir / COMMAND_MODELS_FILE_NAME

    @property
    def root(self) -> Path:
        return self._root

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def config_path(self) -> Path:
        return self._config_path

    def load(self) -> Tuple[CommandModelProfile, ...]:
        """Load and strictly validate every defined profile.

        A missing file is the empty configuration.  Malformed JSON,
        oversized files, unknown fields, invalid profiles, and duplicate
        profile ids all fail closed with a bounded diagnostic.
        """
        if not self._config_path.is_file():
            return ()
        try:
            size = self._config_path.stat().st_size
        except OSError as exc:
            raise CommandConfigError(
                f"command-model configuration cannot be inspected: {exc}"
            ) from exc
        if size > _MAX_CONFIG_FILE_BYTES:
            raise CommandConfigError(
                "command-model configuration exceeds the "
                f"{_MAX_CONFIG_FILE_BYTES}-byte bound"
            )
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise CommandConfigError(
                f"command-model configuration could not be read: "
                f"{_bounded_diagnostic(exc)}"
            ) from None
        if not isinstance(raw, Mapping):
            raise CommandConfigError(
                "command-model configuration must be a JSON object"
            )
        known = {"schema_version", "profiles"}
        extra = set(raw.keys()) - known
        if extra:
            raise CommandConfigError(
                f"command-model configuration has unknown fields: {sorted(extra)}"
            )
        if raw.get("schema_version") != COMMAND_CONFIG_SCHEMA_VERSION:
            raise CommandConfigError(
                "unsupported command-model configuration version: "
                f"{raw.get('schema_version')!r}"
            )
        profiles_raw = raw.get("profiles")
        if type(profiles_raw) is not list:
            raise CommandConfigError(
                "command-model configuration 'profiles' must be an array"
            )
        if len(profiles_raw) > 64:
            raise CommandConfigError(
                "command-model configuration exceeds the 64-profile bound"
            )
        profiles: list[CommandModelProfile] = []
        seen: set[str] = set()
        for index, item in enumerate(profiles_raw):
            try:
                profile = CommandModelProfile.from_mapping(item)
            except CommandConfigError as exc:
                raise CommandConfigError(
                    f"profile {index} is invalid: {exc}"
                ) from exc
            if profile.profile_id in seen:
                raise CommandConfigError(
                    f"duplicate profile id: {profile.profile_id!r}"
                )
            seen.add(profile.profile_id)
            profiles.append(profile)
        return tuple(profiles)

    def list_profiles(self) -> Tuple[CommandModelProfile, ...]:
        """Load every profile (a missing/malformed config raises)."""
        return self.load()

    def get(self, profile_id: str) -> CommandModelProfile:
        """Return one validated profile; fail closed when absent."""
        if type(profile_id) is not str or not profile_id:
            raise CommandConfigNotFoundError("profile id must be a non-empty string")
        for profile in self.load():
            if profile.profile_id == profile_id:
                return profile
        raise CommandConfigNotFoundError(f"profile not found: {profile_id!r}")

    def summaries(self) -> Tuple[ProfileSummary, ...]:
        """Safe concise summaries of every defined profile (UI discovery)."""
        return tuple(profile.summary() for profile in self.load())


def _bounded_diagnostic(text: Any) -> str:
    cleaned = "".join(
        char if 0x20 <= ord(char) != 0x7F else " " for char in str(text)
    )
    if len(cleaned) > 400:
        cleaned = cleaned[:397] + "..."
    return cleaned or "unspecified"
