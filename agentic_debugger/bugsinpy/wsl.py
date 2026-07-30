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
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from agentic_debugger.runtime.command_runner import (
    _MAX_OUTPUT_CHARS,
    CommandResult,
    _BoundedStreamAccumulator,
    _get_creationflags,
    _kill_process_tree,
)
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


@dataclass(frozen=True)
class ResourceLimits:
    """Live-tested prlimit profile: CPU-time, address-space, and process-count caps."""

    cpu_seconds: int
    memory_bytes: int
    max_processes: int

    def __post_init__(self) -> None:
        for name in ("cpu_seconds", "memory_bytes", "max_processes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive int")

    def to_mapping(self) -> dict[str, str]:
        return {
            "cpu_seconds": f"prlimit-enforced:{self.cpu_seconds}",
            "memory_bytes": f"prlimit-enforced:{self.memory_bytes}",
            "max_processes": f"prlimit-enforced:{self.max_processes}",
        }


def build_prlimit_argv(command: Sequence[str], *, cpu_seconds: int, memory_bytes: int, max_processes: int) -> list[str]:
    """Wrap ``command`` so the kernel enforces CPU-time/address-space/nproc caps."""
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ValueError("prlimit command must be a non-empty argv")
    for name, value in (("cpu_seconds", cpu_seconds), ("memory_bytes", memory_bytes), ("max_processes", max_processes)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive int")
    return [
        "prlimit",
        f"--cpu={cpu_seconds}",
        f"--as={memory_bytes}",
        f"--nproc={max_processes}",
        "--",
        *command,
    ]


def build_linux_timeout_argv(command: Sequence[str], *, timeout_seconds: float) -> list[str]:
    """Wrap ``command`` with GNU coreutils ``timeout`` so the WSL/Bubblewrap
    process tree is genuinely killed on the Linux side, independent of
    whatever the Windows-side ``wsl.exe`` wrapper does with its own process."""
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ValueError("timeout command must be a non-empty argv")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive number")
    return ["timeout", "--kill-after=2", "--signal=KILL", str(timeout_seconds), *command]


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
        self._resource_profile: Optional[ResourceLimits] = None
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
        if self._resource_profile is not None:
            profile = self._resource_profile
            translated = build_prlimit_argv(translated, cpu_seconds=profile.cpu_seconds, memory_bytes=profile.memory_bytes, max_processes=profile.max_processes)
        command = build_bwrap_command(translated, workspace=workspace_posix, python_root=self.python_root_posix, empty_dir=self.empty_dir_posix)
        command = build_linux_timeout_argv(command, timeout_seconds=timeout_seconds)
        result = self.process.run(command, environment=reviewed, timeout_seconds=timeout_seconds)
        return CommandResult(list(argv), cwd, result.exit_code, result.timed_out, result.duration_ms, result.stdout, result.stderr, result.stdout_truncated, result.stderr_truncated)

    def verify_and_open_resource_isolation(self, workspace_host: str, profile: ResourceLimits) -> dict[str, Any]:
        """The only way to set ``resource_isolation_ready = True``.

        Runs every readiness probe live, through this exact runner instance,
        strictly validates each result, and only then binds the profile and
        opens the gate. There is no public entry point that accepts
        externally-constructed evidence (a plain ``{"passed": True}``
        mapping cannot open the gate); the probes are always produced by a
        real call to :meth:`run` against this runner's ``runner_id``.
        """
        if not isinstance(profile, ResourceLimits):
            raise ResourceIsolationUnavailable("a validated ResourceLimits profile is required")
        results = self._run_readiness_probes(workspace_host, profile)
        for name, entry in results.items():
            if not entry["passed"]:
                raise ResourceIsolationUnavailable(f"resource readiness probe failed strict validation: {name}: {entry['detail']}")
        self._resource_profile = profile
        self.resource_isolation_ready = True
        resource_limits = dict(profile.to_mapping())
        resource_limits["timeout"] = "linux-timeout+prlimit+wsl-process-tree-enforced"
        resource_limits["retained_output_chars"] = f"bounded-streaming:{_MAX_OUTPUT_CHARS}"
        self.boundary_guarantee = ContainmentGuarantee(self.root_host, self.runner_id, resource_limits=resource_limits).to_mapping()
        return results

    def _run_readiness_probes(self, workspace_host: str, profile: ResourceLimits) -> dict[str, Any]:
        """Bounded, harmless live probes for CPU/memory/process/wall-clock/output limits."""
        if not isinstance(profile, ResourceLimits):
            raise ResourceIsolationUnavailable("a validated ResourceLimits profile is required")
        resolved_workspace = str(Path(workspace_host).resolve())
        probe_timeout = max(5.0, profile.cpu_seconds + 5.0)
        saved_profile, self._resource_profile = self._resource_profile, profile
        self._self_test_active = True
        results: dict[str, Any] = {}
        try:
            cpu_result = self.run(
                ["/opt/python/bin/python", "-c", "i = 0\nwhile True:\n    i += 1"],
                resolved_workspace, probe_timeout, {},
            )
            passed, detail = _validate_cpu_probe(cpu_result)
            results["cpu_limit_enforced"] = _probe_entry(cpu_result, passed, detail)

            memory_result = self.run(
                ["/opt/python/bin/python", "-c", f"bytearray({profile.memory_bytes * 4})"],
                resolved_workspace, probe_timeout, {},
            )
            passed, detail = _validate_memory_probe(memory_result)
            results["memory_limit_enforced"] = _probe_entry(memory_result, passed, detail)

            process_result = self.run(
                [
                    "/opt/python/bin/python", "-c",
                    "import os, sys\ntry:\n    [os.fork() for _ in range(64)]\n"
                    "except OSError as e:\n    sys.stderr.write(str(e))\n    sys.exit(3)\nsys.exit(0)",
                ],
                resolved_workspace, probe_timeout, {},
            )
            passed, detail = _validate_process_probe(process_result)
            results["process_limit_enforced"] = _probe_entry(process_result, passed, detail)

            marker_name = f".walltest-marker-{os.urandom(4).hex()}"
            wall_timeout = 3.0
            wall_result = self.run(
                ["/opt/python/bin/python", "-c", f"import time\ntime.sleep(30)\nopen('/workspace/{marker_name}', 'w').write('done')"],
                resolved_workspace, wall_timeout, {},
            )
            marker_exists = (Path(resolved_workspace) / marker_name).exists()
            passed, detail = _validate_wall_clock_probe(wall_result, marker_exists, max_expected_duration_ms=int(wall_timeout * 1000) + 5000)
            results["wall_clock_timeout_enforced"] = _probe_entry(wall_result, passed, detail)

            output_result = self.run(
                ["/opt/python/bin/python", "-c", "import sys; sys.stdout.write('A' * 50000)"],
                resolved_workspace, probe_timeout, {},
            )
            passed, detail = _validate_output_probe(output_result, configured_bound=_MAX_OUTPUT_CHARS)
            results["retained_output_bounded"] = _probe_entry(output_result, passed, detail)
        finally:
            self._self_test_active = False
            self._resource_profile = saved_profile
        return results

    def self_test(self, workspace_host: str, unrelated_home_name: str = ".agentic-unrelated-home-sentinel", expected_python_version: str = PYTHON_VERSION) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", unrelated_home_name):
            raise WslGateError("invalid self-test sentinel name")
        commands = {
            "network_denied": (["/opt/python/bin/python", "-c", "import socket; socket.create_connection(('1.1.1.1', 80), 1)"], 1),
            "windows_mounts_hidden": (["/opt/python/bin/python", "-c", "import os; assert not os.path.exists('/mnt/c')"], 0),
            "unrelated_home_hidden": (["/opt/python/bin/python", "-c", f"import os; assert not os.path.exists('/home/{unrelated_home_name}')"], 0),
            "owned_workspace_write": (["/opt/python/bin/python", "-c", "open('/workspace/.selftest-write', 'w').write('owned')"], 0),
            "runtime_mount_read_only": (["/opt/python/bin/python", "-c", "open('/usr/bin/.agentic-write', 'w')"], 1),
            "child_process_isolated": (["/opt/python/bin/python", "-c", "import os, subprocess; assert os.getppid() == 1; assert subprocess.run(['/bin/true']).returncode == 0"], 0),
            "exact_interpreter": (["/opt/python/bin/python", "-c", f"import sys; assert '.'.join(map(str, sys.version_info[:3])) == {expected_python_version!r}"], 0),
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


def create_verified_context(*, root_host: str, python_root_posix: str, python_executable_posix: str, python_version: str, project_cwd: str, pythonpath: Sequence[str], reviewed_environment: Mapping[str, str], dependencies: DependencyPreparation, runner: Optional["WslBubblewrapRunner"] = None) -> VerifiedExecutionContext:
    active_runner = runner or WslBubblewrapRunner(root_host=root_host, python_root_posix=python_root_posix, python_executable_posix=python_executable_posix)
    environment = PreparedEnvironment(wsl_unc_path(python_executable_posix), python_version, project_cwd, tuple(pythonpath), dict(reviewed_environment), dependencies)
    resource_limits = dict(active_runner.boundary_guarantee["resource_limits"]) if active_runner.resource_isolation_ready else {"timeout": "enforced-by-parent-runner"}
    containment = ContainmentGuarantee(
        str(Path(root_host).resolve()), active_runner.runner_id,
        resource_limits=resource_limits,
    )
    return VerifiedExecutionContext(environment, containment, active_runner)


def _probe_entry(result: CommandResult, passed: bool, detail: str) -> dict[str, Any]:
    return {
        "passed": passed,
        "detail": detail,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        "stderr": result.stderr[-2000:],
        "stdout_truncated": result.stdout_truncated,
        "stdout_len": len(result.stdout),
    }


def _validate_cpu_probe(result: CommandResult) -> tuple[bool, str]:
    """Require the exact RLIMIT_CPU termination signature: a real kill, not a timeout."""
    if result.timed_out:
        return False, "wall-clock timeout fired before the CPU-time limit could kill the process"
    if result.exit_code not in (137, 152):
        return False, f"expected SIGKILL(137) or SIGXCPU(152) termination from the CPU-time limit, got exit_code={result.exit_code!r}"
    return True, "CPU-time limit terminated the process with the expected signal"


def _validate_memory_probe(result: CommandResult) -> tuple[bool, str]:
    """Require a non-timeout exit plus explicit MemoryError evidence, not merely exit(1)."""
    if result.timed_out:
        return False, "wall-clock timeout fired before the address-space limit could reject the allocation"
    if result.exit_code != 1:
        return False, f"expected a clean Python exit(1) from the address-space limit, got exit_code={result.exit_code!r}"
    if "MemoryError" not in result.stderr:
        return False, "expected explicit MemoryError evidence in stderr; exit(1) alone does not prove the address-space limit fired"
    return True, "address-space limit produced a clean MemoryError"


def _validate_process_probe(result: CommandResult) -> tuple[bool, str]:
    """Require the exact RLIMIT_NPROC/EAGAIN signature, not any unrelated OSError."""
    if result.timed_out:
        return False, "wall-clock timeout fired before the process-count limit could reject a fork"
    if result.exit_code != 3:
        return False, f"expected the fork-rejection sentinel exit(3), got exit_code={result.exit_code!r}"
    if "Resource temporarily unavailable" not in result.stderr and "Errno 11" not in result.stderr:
        return False, "expected explicit EAGAIN/'Resource temporarily unavailable' evidence in stderr; an unrelated OSError does not prove the process-count limit fired"
    return True, "process-count limit rejected fork() with the expected EAGAIN behavior"


# Live-observed WSL behavior: a SIGKILL-terminated command's exit code is
# reported as the raw signal number (9), not the 128+signal convention GNU
# `timeout` documents (137). Both are accepted as valid Linux-side
# timeout-kill signatures; an unrelated fast exit (e.g. 1, from a genuine
# command failure) must never be mistaken for one.
_LINUX_TIMEOUT_KILL_EXIT_CODES = frozenset({9, 137})


def _validate_wall_clock_probe(result: CommandResult, marker_exists: bool, *, max_expected_duration_ms: int) -> tuple[bool, str]:
    """Require genuine, early process-tree death, using objective evidence
    that cannot be spoofed by exit-code alone -- and require a specific,
    recognized termination signature rather than accepting any fast exit.

    The Windows-side ``Popen.wait()`` timing out (``result.timed_out``) is
    one valid signature. When the Linux-side ``timeout`` wrapper kills the
    process tree quickly, the whole WSL invocation can instead complete
    within its own Windows-side budget, so Windows never itself times out
    even though the kill genuinely happened; in that case the exit code
    must match the observed/documented Linux-side timeout-kill signature.
    The marker file the sandboxed process only writes *after* completing
    its full sleep is the definitive, mandatory proof of survival in every
    case.
    """
    if marker_exists:
        return False, "the sandboxed process survived past the wall-clock timeout and wrote its marker file; the process tree was not genuinely terminated"
    if result.timed_out:
        return True, "wall-clock timeout fired on the Windows side and the sandboxed process tree was genuinely terminated before it could finish"
    if result.exit_code not in _LINUX_TIMEOUT_KILL_EXIT_CODES:
        return False, (
            f"no Windows-side timeout, and exit_code={result.exit_code!r} does not match the recognized "
            f"Linux-side timeout-kill signature {sorted(_LINUX_TIMEOUT_KILL_EXIT_CODES)}; a fast unrelated "
            "exit is not proof of timeout enforcement"
        )
    if result.duration_ms > max_expected_duration_ms:
        return False, (
            f"command ran for {result.duration_ms}ms, longer than the {max_expected_duration_ms}ms wall-clock "
            "enforcement budget expected even for a Linux-side kill"
        )
    return True, "wall-clock timeout fired (Linux-side kill signature) and the sandboxed process tree was genuinely terminated before it could finish"


_OUTPUT_BOUND_MARGIN = 64  # _TRUNCATION_MARKER text (~29 chars) + chunk-boundary slack


def _validate_output_probe(result: CommandResult, *, configured_bound: int) -> tuple[bool, str]:
    """Require a clean, successful probe run whose retained output matches
    the exact configured retention bound (head+marker+tail, per
    ``_BoundedStreamAccumulator``), not merely be shorter than the uncapped
    probe input -- a partially-bounded implementation that still retains
    far more than the configured limit must fail this check. Truncated
    output alone is not proof of the retention bound: an unrelated failed
    command could also produce truncated output (e.g. large error spam),
    so a clean exit(0) is required first.
    """
    if result.timed_out:
        return False, "wall-clock timeout fired during the output-bound probe"
    if result.exit_code != 0:
        return False, (
            f"expected a clean exit(0) from the output-bound probe, got exit_code={result.exit_code!r}; "
            "truncated output from a failed/unrelated command is not proof of the retention bound"
        )
    if not result.stdout_truncated:
        return False, "expected retained stdout to be marked truncated for output exceeding the bound"
    if len(result.stdout) > configured_bound + _OUTPUT_BOUND_MARGIN:
        return False, (
            f"retained stdout ({len(result.stdout)} chars) exceeds the configured "
            f"retention bound ({configured_bound} chars, +{_OUTPUT_BOUND_MARGIN} marker/boundary margin)"
        )
    return True, "retained stdout matched the configured retention bound"


def _subprocess_result(argv: Sequence[str], *, timeout_seconds: float) -> CommandResult:
    """Bounded streaming capture: stdout/stderr are decoded and capped while the
    process runs, never buffered unbounded in memory, and the process tree is
    killed (not merely abandoned) on a wall-clock timeout."""
    start = time.monotonic()
    stdout_accum = _BoundedStreamAccumulator(_MAX_OUTPUT_CHARS)
    stderr_accum = _BoundedStreamAccumulator(_MAX_OUTPUT_CHARS)
    stdout_lock = threading.Lock()
    stderr_lock = threading.Lock()
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=_get_creationflags(),
        )
    except OSError as exc:
        raise WslUnavailableError(f"failed to invoke explicit wsl.exe: {exc}") from exc

    def _drain(stream: Any, accum: _BoundedStreamAccumulator, lock: threading.Lock) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                with lock:
                    accum.decode_and_add(chunk)
        except Exception:
            pass

    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, stdout_accum, stdout_lock), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, stderr_accum, stderr_lock), daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc)
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
    finally:
        for thread in threads:
            thread.join(timeout=5.0)

    exit_code = None if timed_out else proc.returncode
    with stdout_lock:
        stdout, stdout_truncated = stdout_accum.finalize()
    with stderr_lock:
        stderr, stderr_truncated = stderr_accum.finalize()
    return CommandResult(
        list(argv), "", exit_code, timed_out, int((time.monotonic() - start) * 1000),
        stdout, stderr, stdout_truncated, stderr_truncated,
    )
