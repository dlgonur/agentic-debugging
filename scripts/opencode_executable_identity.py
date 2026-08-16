"""Pure, fail-closed OpenCode executable identity resolution for Local Application V1.

The OpenCode Go command adapter must never execute whichever ``opencode``
happens to appear first on PATH.  This module ports the accepted pure
executable-identity primitives of the historical protocol transport
(``scripts/opencode_protocol_transport.py``) without importing any QuixBugs
campaign/authorization behavior:

* begin from an operator-resolved ABSOLUTE ``opencode.cmd`` launcher path on
  Windows;
* resolve the trusted npm ``opencode-ai`` package root derived ONLY from the
  verified launcher directory;
* resolve the deterministic native ``opencode.exe`` target selected by the
  established npm shim (``<package-root>\\bin\\opencode.exe``);
* require root containment (no symlink/reparse/path escape) and a regular
  file;
* require launcher/native version equality (both ``--version`` invocations
  under the caller-supplied bounded environment);
* no recursive search, no arbitrary PATH fallback, no PowerShell/shell
  interpolation, no fallback to the ``opencode.cmd`` batch shim for model
  execution;

For non-Windows platforms, an explicit absolute executable plus a bounded
identity/version check (``<executable> --version`` must succeed with a
non-empty version) replaces the bare PATH lookup.

Module-level import side effects: none (stdlib only, no subprocess spawned
at import time).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

#: The trusted npm package root relative to the verified ``opencode.cmd``
#: launcher directory; the native executable must belong to this root.
NPM_PACKAGE_ROOT_RELATIVE = "node_modules/opencode-ai"
#: The exact package-relative native executable target selected by the
#: established npm shim.  Only this deterministic target under the trusted
#: ``opencode-ai`` package root is ever resolved; platform and baseline
#: package binaries are never enumerated or compared, and there is no
#: recursive search.
NATIVE_EXECUTABLE_RELATIVE = "bin/opencode.exe"
#: The exact launcher file name required on Windows.
OPENCODE_LAUNCHER_NAME = "opencode.cmd"

#: Bounded timeout for the non-model ``--version`` identity checks.
_VERSION_CHECK_TIMEOUT_SECONDS = 30.0

_VERSION_CHECK_ENV_NAMES = ("PATH", "PATHEXT", "SystemRoot", "WINDIR", "COMSPEC")


def minimal_environment() -> dict[str, str]:
    """The bounded environment used for non-model identity checks.

    Carries only the process-launch and native-runtime basics the checks
    need (PATH/PATHEXT, Windows system dirs, COMSPEC, a working temp dir,
    and the user profile when the caller environment provides it).  Ambient
    variables — including any credential-shaped ones — are never forwarded.
    """
    environment = {
        name: os.environ[name]
        for name in _VERSION_CHECK_ENV_NAMES
        if os.environ.get(name)
    }
    if not environment.get("TMP"):
        environment["TMP"] = tempfile.gettempdir()
    if not environment.get("TEMP"):
        environment["TEMP"] = environment["TMP"]
    for name in ("USERPROFILE", "HOME"):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    return environment


def _run_version(executable: str, environment: Mapping[str, str]) -> str:
    """Run ``<executable> --version`` once and return the bounded version.

    ``shell=False`` always; never PowerShell, never shell interpolation.
    A nonzero exit or an empty version fails closed.
    """
    completed = subprocess.run(
        [executable, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_VERSION_CHECK_TIMEOUT_SECONDS,
        check=False,
        shell=False,
        env=dict(environment),
    )
    version = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0 or not version:
        raise RuntimeError(
            f"OpenCode executable version preflight failed for {executable!r} "
            f"(exit {completed.returncode})"
        )
    return version


def _npm_package_root(launcher: Path) -> Path:
    """The trusted npm package root derived ONLY from the verified launcher."""
    package_root = (launcher.parent / NPM_PACKAGE_ROOT_RELATIVE).resolve()
    if not package_root.is_absolute():
        raise RuntimeError("trusted npm package root is not an absolute path")
    if not package_root.is_dir():
        raise RuntimeError(
            f"trusted npm package root is missing or not a directory: {package_root}"
        )
    return package_root


def _resolve_native_executable(launcher: Path) -> Path:
    """Resolve the native ``opencode.exe`` selected by the npm shim.

    Mirrors the accepted transport contract: the resolved absolute path must
    remain inside the trusted package root (no symlink/reparse/path escape)
    and must exist as a regular file; otherwise resolution fails closed.
    """
    package_root = _npm_package_root(launcher)
    native = (package_root / NATIVE_EXECUTABLE_RELATIVE).resolve()
    if not native.is_absolute():
        raise RuntimeError("native OpenCode executable path is not an absolute path")
    try:
        native.relative_to(package_root)
    except ValueError:
        raise RuntimeError(
            "native OpenCode executable path escapes the trusted npm package root"
        ) from None
    if not native.is_file():
        raise RuntimeError(
            f"native OpenCode executable was not found under the trusted npm package root: {native}"
        )
    return native


def resolve_verified_opencode_executable(
    executable: str,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Resolve and prove one explicit OpenCode executable identity.

    Windows: ``executable`` must be an absolute ``opencode.cmd`` launcher
    path.  The trusted npm package root is derived from the launcher
    directory, the deterministic npm-shim native ``opencode.exe`` target is
    resolved with root containment + regular-file proof, and the native and
    launcher ``--version`` outputs must be equal (same installation).

    POSIX: ``executable`` must be an absolute regular file whose
    ``--version`` invocation succeeds with a non-empty version.

    Returns bounded resolution evidence only (resolution strategy,
    launcher/native paths, versions, proof flags) — never executable bytes
    or unrestricted environment data.  Every failure mode raises
    ``RuntimeError`` with a bounded message; a bare name, a relative path, a
    wrong file name, a missing file, a missing npm layout, a path escape, or
    a version drift all fail closed.
    """
    if not isinstance(executable, str) or not executable.strip():
        raise RuntimeError("OpenCode executable identity is missing")
    env = dict(environment) if environment is not None else minimal_environment()
    if sys.platform == "win32":
        raw = Path(executable)
        if not os.path.isabs(executable):
            raise RuntimeError("OpenCode launcher path must be absolute")
        if raw.name.lower() != OPENCODE_LAUNCHER_NAME:
            raise RuntimeError(
                f"OpenCode launcher must be an absolute {OPENCODE_LAUNCHER_NAME} path; "
                f"rejected {executable!r}"
            )
        launcher = raw.resolve()
        if not launcher.is_file():
            raise RuntimeError(f"OpenCode launcher is not a regular file: {launcher}")
        package_root = _npm_package_root(launcher)
        native = _resolve_native_executable(launcher)
        launcher_version = _run_version(str(launcher), env)
        native_version = _run_version(str(native), env)
        if native_version != launcher_version:
            raise RuntimeError(
                f"native OpenCode executable version drift: observed {native_version!r} "
                f"!= launcher {launcher_version!r}"
            )
        return {
            "resolution_strategy": "npm-package-layout",
            "launcher": str(launcher),
            "native_executable": str(native),
            "package_relative_path": str(native.relative_to(package_root)),
            "launcher_version": launcher_version,
            "native_version": native_version,
            "regular_file": True,
            "root_containment": True,
            "version_matches_launcher": True,
        }
    path = Path(executable).resolve()
    if not os.path.isabs(executable):
        raise RuntimeError("OpenCode executable path must be absolute")
    if not path.is_file():
        raise RuntimeError(f"OpenCode executable is not a regular file: {path}")
    version = _run_version(str(path), env)
    return {
        "resolution_strategy": "verified-absolute-executable",
        "native_executable": str(path),
        "native_version": version,
        "regular_file": True,
    }


__all__ = [
    "NATIVE_EXECUTABLE_RELATIVE",
    "NPM_PACKAGE_ROOT_RELATIVE",
    "OPENCODE_LAUNCHER_NAME",
    "minimal_environment",
    "resolve_verified_opencode_executable",
]
