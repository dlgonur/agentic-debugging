"""Central Windows-venv interpreter launch authority.

On Windows, a CPython virtual environment's ``Scripts\\python.exe`` is a
small redirector executable: it reads ``pyvenv.cfg`` to locate the base
installation, sets ``__PYVENV_LAUNCHER__`` to its own path, and launches
the real base interpreter as a *separate* child process.  A parent that
``Popen``\\ s the redirector therefore observes the redirector's PID while
the actual Python worker reports its own (different) PID -- the
``worker reported PID == Popen(...).pid`` identity invariant cannot hold.

This module implements the exact pattern CPython itself uses for the same
problem (``Lib/multiprocessing/popen_spawn_win32.py``, bpo-35797): when the
current process runs inside a Windows virtual environment, launch the real
base interpreter directly and preserve the venv identity explicitly via
``__PYVENV_LAUNCHER__``.  The child then computes the identical
``sys.executable`` / ``sys.prefix`` / ``sys.path`` (including venv
``site-packages``) as if it had been launched through the redirector --
including under the worker's ``-I`` isolated flag -- while its PID is the
``Popen`` PID again, so the strict handshake/JOB-object identity checks
keep working unchanged.

Deterministic venv recognition (same comparison as CPython)::

    WINENV = not _path_eq(sys.executable, sys._base_executable)

Only ``sys.platform == "win32"`` with a present, non-empty
``sys._base_executable`` that is not path-equal to ``sys.executable``
counts as a redirector launch.  Every other configuration (POSIX, system
interpreter, frozen binaries without the attribute, unresolvable paths)
returns the same worker executable (``sys.executable`` unchanged), so
normal execution semantics are unchanged; the child environment
additionally scrubs a stale or forged ``__PYVENV_LAUNCHER__`` where it
must not propagate, so a system-interpreter child keeps its own prefix
instead of inheriting a foreign venv identity.

Fail-closed properties:

* The ``__PYVENV_LAUNCHER__`` value is always the current
  ``sys.executable`` (the venv redirector itself) -- never a
  caller-supplied string -- so a child environment cannot forge a venv
  identity the parent does not have.
* Outside a Windows venv the authority never injects
  ``__PYVENV_LAUNCHER__``; a stale value inherited from the operator
  environment is scrubbed from an explicitly built child environment so a
  system-interpreter child cannot be tricked into the wrong prefix.
* Unknown, missing, or wrongly typed inputs raise instead of guessing.
"""

from __future__ import annotations

import ntpath
import os
import sys
from typing import Mapping, Optional, Tuple

_LAUNCHER_ENV_VAR = "__PYVENV_LAUNCHER__"


def _path_eq(p1: str, p2: str, *, platform: Optional[str] = None) -> bool:
    """Path equality under the platform semantics being evaluated.

    CPython ``_path_eq`` (``Lib/multiprocessing/popen_spawn_win32.py``)::

        p1 == p2 or os.path.normcase(p1) == os.path.normcase(p2)

    On a live Windows interpreter ``os.path`` is ``ntpath``, so using
    ``ntpath.normcase`` for injected ``platform="win32"`` state is exactly
    equivalent there while staying deterministic on another host (where
    the host ``os.path.normcase`` would be POSIX and would not case-fold
    Windows paths).  Non-Windows evaluations use the host normalization,
    which is the correct POSIX semantics there.
    """
    if p1 == p2:
        return True
    plat = sys.platform if platform is None else platform
    if plat == "win32":
        return ntpath.normcase(p1) == ntpath.normcase(p2)
    return os.path.normcase(p1) == os.path.normcase(p2)


def is_windows_venv_redirector(
    *,
    executable: Optional[str] = None,
    base_executable: Optional[str] = None,
    platform: Optional[str] = None,
) -> bool:
    """Whether the current interpreter is a Windows venv redirector launch.

    All parameters default to the live interpreter state
    (``sys.executable``, ``sys._base_executable``, ``sys.platform``) and are
    injectable only so unit tests can prove the decision table without a
    real virtual environment.
    """
    exe = sys.executable if executable is None else executable
    if platform is None:
        platform = sys.platform
    if base_executable is None:
        base_executable = getattr(sys, "_base_executable", None)
    if platform != "win32":
        return False
    if type(exe) is not str or not exe:
        return False
    if type(base_executable) is not str or not base_executable:
        return False
    return not _path_eq(exe, base_executable, platform=platform)


def resolve_worker_executable(
    *,
    executable: Optional[str] = None,
    base_executable: Optional[str] = None,
    platform: Optional[str] = None,
) -> str:
    """Executable path a worker subprocess must be launched with.

    Inside a Windows venv this is the real base interpreter
    (``sys._base_executable``); everywhere else it is ``sys.executable``
    unchanged.
    """
    exe = sys.executable if executable is None else executable
    if type(exe) is not str or not exe:
        raise ValueError("executable must be a non-empty string")
    base = (
        getattr(sys, "_base_executable", None)
        if base_executable is None
        else base_executable
    )
    plat = sys.platform if platform is None else platform
    if is_windows_venv_redirector(
        executable=exe, base_executable=base, platform=plat
    ):
        assert type(base) is str and base
        return base
    return exe


def build_worker_env(
    base_env: Optional[Mapping[str, str]] = None,
    *,
    executable: Optional[str] = None,
    base_executable: Optional[str] = None,
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[dict]:
    """Child environment that preserves the venv identity of a worker spawn.

    * ``base_env=None`` means "inherit the current process environment"
      (the ``PdbSession`` convention): returns ``None`` when no fixup is
      needed so ``Popen`` inherits unchanged, otherwise a copy with the
      fixup applied.
    * A mapping ``base_env`` (the ``SessionWorkerProcess`` convention,
      already merged over ``os.environ`` as needed) is copied and fixed;
      the input mapping is never mutated.

    When the current process is a Windows venv redirector launch, the
    returned environment sets ``__PYVENV_LAUNCHER__`` to the venv
    redirector path (``sys.executable``), exactly as CPython's own spawn
    path does.  The value is always parent-derived, never caller-supplied,
    and any caller-supplied ``__PYVENV_LAUNCHER__`` entry is overwritten.
    Outside a Windows venv a stale ``__PYVENV_LAUNCHER__`` is scrubbed so a
    system-interpreter child keeps its own prefix.
    """
    exe = sys.executable if executable is None else executable
    if type(exe) is not str or not exe:
        raise ValueError("executable must be a non-empty string")
    base = (
        getattr(sys, "_base_executable", None)
        if base_executable is None
        else base_executable
    )
    plat = sys.platform if platform is None else platform
    env_source: Mapping[str, str] = (
        os.environ if environ is None else environ
    )
    redirector = is_windows_venv_redirector(
        executable=exe, base_executable=base, platform=plat
    )
    if redirector:
        if base_env is None:
            child = dict(env_source)
        else:
            if not isinstance(base_env, Mapping):
                raise ValueError("base_env must be a mapping or None")
            child = dict(base_env)
        child[_LAUNCHER_ENV_VAR] = exe
        return child
    if base_env is None:
        if _LAUNCHER_ENV_VAR in env_source:
            child = dict(env_source)
            child.pop(_LAUNCHER_ENV_VAR, None)
            return child
        return None
    if not isinstance(base_env, Mapping):
        raise ValueError("base_env must be a mapping or None")
    child = dict(base_env)
    # A non-venv parent must never hand its worker a launcher identity:
    # drop a stale inherited value and refuse a caller-forged one.
    child.pop(_LAUNCHER_ENV_VAR, None)
    return child


def resolve_worker_spawn(
    base_env: Optional[Mapping[str, str]] = None,
    *,
    executable: Optional[str] = None,
    base_executable: Optional[str] = None,
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Tuple[str, Optional[dict]]:
    """Convenience pair: ``(worker_executable, worker_env)`` for one spawn."""
    return (
        resolve_worker_executable(
            executable=executable,
            base_executable=base_executable,
            platform=platform,
        ),
        build_worker_env(
            base_env,
            executable=executable,
            base_executable=base_executable,
            platform=platform,
            environ=environ,
        ),
    )


__all__ = [
    "is_windows_venv_redirector",
    "resolve_worker_executable",
    "build_worker_env",
    "resolve_worker_spawn",
]
