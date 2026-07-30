"""Single Ubuntu-22.04 WSL2/Bubblewrap boundary for BugsInPy.

Acquisition/preparation uses an explicit cleared WSL process. Benchmark
execution is deliberately fail-closed until concrete CPU, memory, and PID
limits exist; the implemented policy only claims the guarantees it enforces.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from agentic_debugger.runtime.command_runner import CommandResult
from agentic_debugger.runtime.execution import (
    ContainmentGuarantee,
    DependencyPreparation,
    PreparedEnvironment,
    VerifiedExecutionContext,
)

DISTRO = "Ubuntu-22.04"
MINIFORGE_VERSION = "26.3.2-2"
MINIFORGE_NAME = f"Miniforge3-{MINIFORGE_VERSION}-Linux-x86_64.sh"
MINIFORGE_URL = f"https://github.com/conda-forge/miniforge/releases/download/{MINIFORGE_VERSION}/{MINIFORGE_NAME}"
MINIFORGE_SHA256_URL = MINIFORGE_URL + ".sha256"
PYTHON_VERSION = "3.6.9"
RUNNER_ID = "bugsinpy-wsl-ubuntu-22.04-bwrap-v1"
_SAFE_ENV_KEYS = frozenset({
    "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONPATH", "PYTHONNOUSERSITE",
    "PIP_NO_INPUT", "PIP_DISABLE_PIP_VERSION_CHECK",
})


class WslGateError(RuntimeError):
    """A required WSL, runtime, checksum, or containment gate failed."""


class WslUnavailableError(WslGateError):
    """The explicitly selected WSL distro or required tool is unavailable."""


class ResourceIsolationUnavailable(WslGateError):
    """Benchmark execution lacks enforced CPU, memory, and PID limits."""


class MiniforgeSpec:
    def __init__(self, url: str = MINIFORGE_URL, checksum_url: str = MINIFORGE_SHA256_URL, filename: str = MINIFORGE_NAME, published_sha256: str | None = None) -> None:
        self.url = url
        self.checksum_url = checksum_url
        self.filename = filename
        self.published_sha256 = published_sha256


def wsl_unc_path(posix_path: str, distro: str = DISTRO) -> str:
    if not posix_path.startswith("/") or ".." in Path(posix_path).parts:
        raise ValueError("WSL paths must be absolute and traversal-free")
    return "\\\\wsl.localhost\\" + distro + posix_path.replace("/", "\\")


def to_wsl_path(value: str, distro: str = DISTRO) -> str:
    """Translate only paths in the selected distro; reject Windows mounts."""
    if value.startswith("/"):
        if ".." in Path(value).parts:
            raise ValueError("WSL path traversal")
        return value
    prefixes = ("\\\\wsl.localhost\\" + distro + "\\", "\\\\wsl$\\" + distro + "\\")
    for prefix in prefixes:
        if value.lower().startswith(prefix.lower()):
            result = "/" + value[len(prefix):].replace("\\", "/")
            if ".." in Path(result).parts:
                raise ValueError("WSL path traversal")
            return result
    raise ValueError(f"path is not owned by {distro}: {value!r}")


def build_wsl_command(command: Sequence[str], *, distro: str = DISTRO, cwd: str | None = None) -> list[str]:
    """Build a non-login WSL argv with safely preserved embedded arguments."""
    if not command or any(not isinstance(item, str) or not item or "\x00" in item for item in command):
        raise ValueError("WSL command must be a non-empty argv")
    result = ["wsl.exe", "-d", distro]
    if cwd is not None:
        result += ["--cd", to_wsl_path(cwd, distro)]
    # bash is invoked without profiles; shlex.join makes the single -c value
    # preserve every original argv boundary, including quotes and spaces.
    return result + ["--", "bash", "--noprofile", "--norc", "-c", shlex.join(list(command))]


def build_bwrap_command(command: Sequence[str], *, workspace: str, python_root: str, empty_dir: str, cwd: str = "/workspace") -> list[str]:
    """Build the Bubblewrap policy with one host-backed persistent RW mount."""
    for name, value in (("workspace", workspace), ("python_root", python_root), ("empty_dir", empty_dir)):
        if not value.startswith("/") or ".." in Path(value).parts:
            raise ValueError(f"{name} must be an absolute WSL path without traversal")
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ValueError("Bubblewrap command must be a non-empty argv")
    # The tmpfs root removes unrelated WSL roots. Runtime paths are read-only;
    # only /workspace is a persistent host-backed writable mount. /tmp and the
    # hidden locations are read-only binds of an owned empty directory.
    return [
        "bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--tmpfs", "/",
        "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin", "--ro-bind", "/sbin", "/sbin",
        "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64", "--ro-bind", "/etc", "/etc",
        "--proc", "/proc", "--dev", "/dev", "--ro-bind", empty_dir, "/home",
        "--ro-bind", empty_dir, "/mnt", "--ro-bind", empty_dir, "/media", "--ro-bind", empty_dir, "/tmp",
        "--ro-bind", empty_dir, "/var", "--dir", "/opt", "--ro-bind", python_root, "/opt/python",
        "--bind", workspace, "/workspace", "--chdir", cwd, "--", *command,
    ]


def fingerprint_environment(values: Mapping[str, str]) -> str:
    canonical = json.dumps(dict(sorted(values.items())), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WslProcess:
    """Argument-only explicit WSL bridge; no login profile is loaded."""

    def __init__(self, distro: str = DISTRO) -> None:
        if distro != DISTRO:
            raise WslUnavailableError(f"only {DISTRO} is authorized, got {distro}")
        self.distro = distro

    def run(self, argv: Sequence[str], *, cwd: str | None = None, environment: Mapping[str, str] | None = None, timeout_seconds: float = 120) -> CommandResult:
        values = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}
        values.update(environment or {})
        allowed = _SAFE_ENV_KEYS | {"PATH", "HOME", "PWD", "TMPDIR"}
        if any(key not in allowed for key in values):
            raise WslGateError("unreviewed environment variable requested")
        command = ["env", "-i", *[f"{key}={value}" for key, value in sorted(values.items())], *argv]
        return _subprocess_result(build_wsl_command(command, distro=self.distro, cwd=cwd), timeout_seconds=timeout_seconds)


class WslBubblewrapRunner:
    """ContainmentRunner with truthful resource guarantees."""

    runner_id = RUNNER_ID

    def __init__(self, *, root_host: str, python_root_posix: str, python_executable_posix: str, distro: str = DISTRO) -> None:
        self.process = WslProcess(distro)
        self.root_host = str(Path(root_host).resolve())
        self.root_posix = to_wsl_path(self.root_host, distro)
        self.python_root_posix = python_root_posix
        self.python_executable_posix = python_executable_posix
        self.empty_dir_posix = self.root_posix + "/runtime/empty"
        self.resource_isolation_ready = False
        self._self_test_active = False
        self.boundary_guarantee = ContainmentGuarantee(
            self.root_host,
            self.runner_id,
            resource_limits={"timeout": "enforced-by-parent-runner"},
        ).to_mapping()

    def run(self, argv: list[str], cwd: str, timeout_seconds: float, env: Mapping[str, str]) -> CommandResult:
        if not self._self_test_active and not self.resource_isolation_ready:
            raise ResourceIsolationUnavailable("benchmark execution blocked: CPU, memory, and PID limits are not enforced")
        workspace_host = str(Path(cwd).resolve())
        workspace_posix = to_wsl_path(workspace_host, self.process.distro)
        if not workspace_posix.startswith(self.root_posix + "/"):
            raise WslGateError("benchmark cwd is outside the owned WSL root")
        reviewed = {key: value for key, value in env.items() if key in _SAFE_ENV_KEYS}
        if len(reviewed) != len(env):
            raise WslGateError("runner received an unreviewed environment variable")
        reviewed.update({"HOME": "/workspace/.home", "TMPDIR": "/workspace/.tmp", "PYTHONNOUSERSITE": "1"})
        translated = []
        for item in argv:
            if item == self.python_executable_posix or item == wsl_unc_path(self.python_executable_posix, self.process.distro):
                translated.append("/opt/python/bin/python")
            elif item.startswith(self.root_host):
                translated.append("/workspace" + item[len(workspace_host):])
            else:
                translated.append(item)
        if translated and translated[0] == self.python_executable_posix:
            translated[0] = "/opt/python/bin/python"
        path_value = reviewed.get("PYTHONPATH")
        if path_value:
            reviewed["PYTHONPATH"] = ":".join(
                "/workspace" + item[len(workspace_host):] if item.startswith(workspace_host) else item
                for item in path_value.split(os.pathsep)
            )
        command = build_bwrap_command(translated, workspace=workspace_posix, python_root=self.python_root_posix, empty_dir=self.empty_dir_posix)
        result = self.process.run(command, environment=reviewed, timeout_seconds=timeout_seconds)
        return CommandResult(list(argv), cwd, result.exit_code, result.timed_out, result.duration_ms, result.stdout, result.stderr, result.stdout_truncated, result.stderr_truncated)

    def self_test(self, workspace_host: str, unrelated_home_name: str = ".agentic-unrelated-home-sentinel") -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", unrelated_home_name):
            raise WslGateError("invalid self-test sentinel name")
        commands = {
            "network_denied": (["/opt/python/bin/python", "-c", "import socket; socket.create_connection(('1.1.1.1', 80), 1)"], 1),
            "windows_mounts_hidden": (["/opt/python/bin/python", "-c", "import os; assert not os.path.exists('/mnt/c')"], 0),
            "unrelated_home_hidden": (["/opt/python/bin/python", "-c", f"import os; assert not os.path.exists('/home/{unrelated_home_name}')"], 0),
            "owned_workspace_write": (["/opt/python/bin/python", "-c", "open('/workspace/.selftest-write', 'w').write('owned')"], 0),
            "runtime_mount_read_only": (["/opt/python/bin/python", "-c", "open('/usr/bin/.agentic-write', 'w')"], 1),
            "child_process_isolated": (["/opt/python/bin/python", "-c", "import os, subprocess; assert os.getppid() == 1; assert subprocess.run(['/bin/true']).returncode == 0"], 0),
            "exact_interpreter": (["/opt/python/bin/python", "-c", "import sys; assert '.'.join(map(str, sys.version_info[:3])) == '3.6.9'"], 0),
        }
        self._self_test_active = True
        results: dict[str, Any] = {}
        try:
            for name, (command, expected) in commands.items():
                result = self.run(command, str(Path(workspace_host).resolve()), 15, {})
                passed = result.exit_code == expected
                results[name] = {"passed": passed, "exit_code": result.exit_code, "stderr": result.stderr[-2000:]}
                if not passed:
                    raise WslGateError(f"Bubblewrap self-test failed: {name}: {result.stderr[-500:]}")
        finally:
            self._self_test_active = False
        return results


def create_verified_context(*, root_host: str, python_root_posix: str, python_executable_posix: str, python_version: str, project_cwd: str, pythonpath: Sequence[str], reviewed_environment: Mapping[str, str], dependencies: DependencyPreparation) -> VerifiedExecutionContext:
    runner = WslBubblewrapRunner(root_host=root_host, python_root_posix=python_root_posix, python_executable_posix=python_executable_posix)
    environment = PreparedEnvironment(wsl_unc_path(python_executable_posix), python_version, project_cwd, tuple(pythonpath), dict(reviewed_environment), dependencies)
    containment = ContainmentGuarantee(
        str(Path(root_host).resolve()), runner.runner_id,
        resource_limits={"timeout": "enforced-by-parent-runner"},
    )
    return VerifiedExecutionContext(environment, containment, runner)


def _subprocess_result(argv: Sequence[str], *, timeout_seconds: float) -> CommandResult:
    start = time.monotonic()
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True, shell=False, timeout=timeout_seconds)
        return CommandResult(list(argv), "", completed.returncode, False, int((time.monotonic() - start) * 1000), completed.stdout[-20000:], completed.stderr[-20000:], len(completed.stdout) > 20000, len(completed.stderr) > 20000)
    except subprocess.TimeoutExpired as exc:
        return CommandResult(list(argv), "", None, True, int((time.monotonic() - start) * 1000), str(exc.stdout or "")[-20000:], str(exc.stderr or "")[-20000:], False, False)
    except OSError as exc:
        raise WslUnavailableError(f"failed to invoke explicit wsl.exe: {exc}") from exc
