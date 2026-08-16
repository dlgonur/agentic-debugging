"""Pure, fail-closed AGY executable identity resolution for Local Application V1.

The AGY Gemini command adapter must never execute whichever ``agy`` happens
to appear first on PATH.  Production and operator profiles supply one
explicit ABSOLUTE executable path; this module proves that path before any
``--print`` invocation:

* the path must be absolute (bare names and relative paths fail closed);
* the resolved path must be a regular file;
* the basename must be the expected AGY executable (``agy.exe`` on Windows,
  ``agy`` elsewhere);
* ``<executable> --version`` must succeed and equal the caller-supplied
  expected version (this BUILD: ``1.1.13``);
* no recursive search, no arbitrary PATH fallback, no shell interpolation.

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

EXPECTED_AGY_VERSION = "1.1.13"
WINDOWS_EXECUTABLE_NAME = "agy.exe"
POSIX_EXECUTABLE_NAME = "agy"

_VERSION_CHECK_TIMEOUT_SECONDS = 30.0
_VERSION_CHECK_ENV_NAMES = ("PATH", "PATHEXT", "SystemRoot", "WINDIR", "COMSPEC")


def minimal_environment() -> dict[str, str]:
    """Bounded environment for non-model identity checks.

    Carries only the process-launch and native-runtime basics.  Ambient
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
    return environment


def expected_executable_name() -> str:
    return WINDOWS_EXECUTABLE_NAME if sys.platform == "win32" else POSIX_EXECUTABLE_NAME


def _run_version(executable: str, environment: Mapping[str, str]) -> str:
    """Run ``<executable> --version`` once and return the bounded version."""
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
            f"AGY executable version preflight failed for {executable!r} "
            f"(exit {completed.returncode})"
        )
    # AGY 1.1.13 prints a single bare version token.  Take the first token so
    # a trailing diagnostic line cannot masquerade as a match.
    return version.split()[0]


def resolve_verified_agy_executable(
    executable: str,
    *,
    expected_version: str = EXPECTED_AGY_VERSION,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Resolve and prove one explicit AGY executable identity.

    Returns bounded resolution evidence only.  A bare name, a relative path,
    a wrong file name, a missing file, or a version mismatch all fail closed.
    """
    if not isinstance(executable, str) or not executable.strip():
        raise RuntimeError("AGY executable identity is missing")
    if not isinstance(expected_version, str) or not expected_version.strip():
        raise RuntimeError("AGY expected version is missing")
    if not os.path.isabs(executable):
        raise RuntimeError("AGY executable path must be absolute")
    raw = Path(executable)
    required_name = expected_executable_name()
    if raw.name.lower() != required_name.lower():
        raise RuntimeError(
            f"AGY executable must be an absolute {required_name} path; "
            f"rejected unexpected executable"
        )
    path = raw.resolve()
    if not path.is_file():
        raise RuntimeError(f"AGY executable is not a regular file: {path}")
    env = dict(environment) if environment is not None else minimal_environment()
    version = _run_version(str(path), env)
    if version != expected_version:
        raise RuntimeError(
            f"AGY executable version mismatch: observed {version!r} "
            f"!= expected {expected_version!r}"
        )
    return {
        "resolution_strategy": "verified-absolute-executable",
        "native_executable": str(path),
        "native_version": version,
        "expected_version": expected_version,
        "regular_file": True,
        "version_matches_expected": True,
    }


__all__ = [
    "EXPECTED_AGY_VERSION",
    "POSIX_EXECUTABLE_NAME",
    "WINDOWS_EXECUTABLE_NAME",
    "expected_executable_name",
    "minimal_environment",
    "resolve_verified_agy_executable",
]
