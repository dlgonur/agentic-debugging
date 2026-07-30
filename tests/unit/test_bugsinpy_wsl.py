from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentic_debugger.bugsinpy.wsl import (
    MINIFORGE_SHA256_URL, MINIFORGE_URL, RUNNER_ID, ResourceIsolationUnavailable,
    ResourceLimits, WslBubblewrapRunner, WslGateError, WslProcess, build_bwrap_command,
    build_linux_timeout_argv, build_prlimit_argv, build_wsl_command, create_verified_context,
    fingerprint_environment, to_wsl_path, wsl_unc_path,
    _validate_cpu_probe, _validate_memory_probe, _validate_output_probe,
    _validate_process_probe, _validate_wall_clock_probe,
)
from agentic_debugger.runtime.command_runner import CommandResult
from agentic_debugger.runtime.execution import DependencyPreparation


def test_non_login_wsl_transport_preserves_python_code_and_boundaries() -> None:
    command = build_wsl_command(["python", "-c", "print(\"a b\")"])
    assert command[:3] == ["wsl.exe", "-d", "Ubuntu-22.04"]
    assert command[command.index("--noprofile") : command.index("--noprofile") + 3] == ["--noprofile", "--norc", "-c"]
    assert "print(\"a b\")" in command[-1]
    assert "bash -l" not in " ".join(command)


def test_wsl_path_translation_rejects_windows_and_foreign_paths() -> None:
    assert to_wsl_path(wsl_unc_path("/home/test/work")) == "/home/test/work"
    assert to_wsl_path("\\\\wsl$\\Ubuntu-22.04\\home\\test\\work") == "/home/test/work"
    with pytest.raises(ValueError):
        to_wsl_path("C:\\repo")
    with pytest.raises(ValueError):
        to_wsl_path("\\\\wsl.localhost\\Ubuntu-22.04\\home\\test\\..\\secret")


def test_bwrap_policy_has_one_host_backed_persistent_rw_mount() -> None:
    command = build_bwrap_command(["/opt/python/bin/python", "-m", "pytest", "x y"], workspace="/owned/work", python_root="/owned/env", empty_dir="/owned/empty")
    assert "--unshare-all" in command and "--die-with-parent" in command
    assert command[command.index("--bind") + 1 : command.index("--bind") + 3] == ["/owned/work", "/workspace"]
    assert command.count("--bind") == 1
    for hidden in ("/home", "/mnt", "/media", "/tmp", "/var"):
        assert ["--ro-bind", "/owned/empty", hidden] == command[command.index(hidden) - 2 : command.index(hidden) + 1]


def test_runner_identity_resource_gate_and_environment_are_fail_closed() -> None:
    root = wsl_unc_path("/home/test/root")
    runner = WslBubblewrapRunner(root_host=root, python_root_posix="/home/test/root/env", python_executable_posix="/home/test/root/env/bin/python")
    assert runner.runner_id == RUNNER_ID
    assert runner.resource_isolation_ready is False
    assert set(runner.boundary_guarantee["resource_limits"]) == {"timeout"}
    with pytest.raises(ResourceIsolationUnavailable):
        runner.run(["/opt/python/bin/python", "-m", "pytest", "x"], root + "\\work", 5, {})
    with pytest.raises(WslGateError):
        WslProcess().run(["true"], environment={"AWS_SECRET_ACCESS_KEY": "no"})


def test_miniforge_url_and_checksum_are_pinned_to_official_release() -> None:
    assert MINIFORGE_URL.startswith("https://github.com/conda-forge/miniforge/releases/download/")
    assert MINIFORGE_SHA256_URL == MINIFORGE_URL + ".sha256"
    assert "Linux-x86_64.sh" in MINIFORGE_URL


def test_environment_fingerprint_is_canonical_and_sensitive() -> None:
    assert fingerprint_environment({"B": "2", "A": "1"}) == fingerprint_environment({"A": "1", "B": "2"})
    assert fingerprint_environment({"A": "1"}) != fingerprint_environment({"A": "2"})


def test_resource_limits_validates_positive_ints() -> None:
    ResourceLimits(cpu_seconds=5, memory_bytes=1024, max_processes=4)
    with pytest.raises(ValueError):
        ResourceLimits(cpu_seconds=0, memory_bytes=1024, max_processes=4)
    with pytest.raises(ValueError):
        ResourceLimits(cpu_seconds=5, memory_bytes=-1, max_processes=4)
    with pytest.raises(ValueError):
        ResourceLimits(cpu_seconds=5, memory_bytes=1024, max_processes=0)


def test_build_prlimit_argv_wraps_command_with_all_three_caps() -> None:
    argv = build_prlimit_argv(["/opt/python/bin/python", "-m", "pytest", "x"], cpu_seconds=5, memory_bytes=268435456, max_processes=8)
    assert argv == ["prlimit", "--cpu=5", "--as=268435456", "--nproc=8", "--", "/opt/python/bin/python", "-m", "pytest", "x"]
    with pytest.raises(ValueError):
        build_prlimit_argv([], cpu_seconds=5, memory_bytes=1, max_processes=1)
    with pytest.raises(ValueError):
        build_prlimit_argv(["cmd"], cpu_seconds=0, memory_bytes=1, max_processes=1)


def _runner() -> WslBubblewrapRunner:
    root = wsl_unc_path("/home/test/root")
    return WslBubblewrapRunner(root_host=root, python_root_posix="/home/test/root/env", python_executable_posix="/home/test/root/env/bin/python")


def test_build_linux_timeout_argv_wraps_command_for_process_tree_kill() -> None:
    argv = build_linux_timeout_argv(["bwrap", "--unshare-all", "--", "python"], timeout_seconds=5)
    assert argv == ["timeout", "--kill-after=2", "--signal=KILL", "5", "bwrap", "--unshare-all", "--", "python"]
    with pytest.raises(ValueError):
        build_linux_timeout_argv([], timeout_seconds=5)
    with pytest.raises(ValueError):
        build_linux_timeout_argv(["cmd"], timeout_seconds=0)


def test_no_public_api_accepts_fabricated_evidence_dicts() -> None:
    runner = _runner()
    assert not hasattr(runner, "prepare_resource_isolation")
    assert not hasattr(runner, "self_test_resource_limits")
    assert hasattr(runner, "verify_and_open_resource_isolation")


def _result(*, exit_code=None, timed_out=False, stdout="", stderr="", stdout_truncated=False, duration_ms=10) -> CommandResult:
    return CommandResult(["argv"], "cwd", exit_code, timed_out, duration_ms, stdout, stderr, stdout_truncated, False)


# ---- Pure probe validators: adversarial / unrelated-failure coverage -------


def test_cpu_probe_validator_requires_signal_kill_not_timeout_or_unrelated_exit() -> None:
    assert _validate_cpu_probe(_result(exit_code=137))[0] is True
    assert _validate_cpu_probe(_result(exit_code=152))[0] is True
    ok, detail = _validate_cpu_probe(_result(exit_code=137, timed_out=True))
    assert ok is False and "timeout" in detail
    ok, detail = _validate_cpu_probe(_result(exit_code=1))
    assert ok is False and "SIGKILL" in detail


def test_memory_probe_validator_requires_memoryerror_evidence_not_bare_exit_code() -> None:
    assert _validate_memory_probe(_result(exit_code=1, stderr="Traceback...\nMemoryError"))[0] is True
    ok, detail = _validate_memory_probe(_result(exit_code=1, stderr="Traceback...\nTypeError: boom"))
    assert ok is False and "MemoryError" in detail
    ok, detail = _validate_memory_probe(_result(exit_code=1, stderr="MemoryError", timed_out=True))
    assert ok is False and "timeout" in detail


def test_process_probe_validator_requires_eagain_evidence_not_any_oserror() -> None:
    assert _validate_process_probe(_result(exit_code=3, stderr="[Errno 11] Resource temporarily unavailable"))[0] is True
    ok, detail = _validate_process_probe(_result(exit_code=3, stderr="[Errno 12] Cannot allocate memory"))
    assert ok is False and "EAGAIN" in detail
    ok, detail = _validate_process_probe(_result(exit_code=0, stderr="[Errno 11] Resource temporarily unavailable"))
    assert ok is False


def test_wall_clock_probe_validator_requires_timeout_and_genuine_process_death() -> None:
    # Windows-side wait() itself timing out is one valid signature.
    assert _validate_wall_clock_probe(_result(timed_out=True, duration_ms=3000), marker_exists=False, max_expected_duration_ms=8000)[0] is True
    # The Linux-side `timeout` wrapper can kill the process tree quickly
    # enough that the whole WSL invocation completes within its own budget,
    # so Windows never itself times out even though the kill genuinely
    # happened. In that case the recognized Linux-side kill signature
    # (empirically observed live: WSL reports a SIGKILL-terminated command's
    # exit code as the raw signal number 9; 137 is the documented 128+signal
    # equivalent) plus a bounded duration and marker absence must still pass.
    assert _validate_wall_clock_probe(_result(timed_out=False, exit_code=9, duration_ms=2950), marker_exists=False, max_expected_duration_ms=8000)[0] is True
    assert _validate_wall_clock_probe(_result(timed_out=False, exit_code=137, duration_ms=2950), marker_exists=False, max_expected_duration_ms=8000)[0] is True
    # A fast, unrelated exit (e.g. a genuine command failure) must never be
    # mistaken for timeout enforcement, even with the marker absent and the
    # duration short -- exit code alone must match a recognized signature.
    ok, detail = _validate_wall_clock_probe(_result(timed_out=False, exit_code=1, duration_ms=50), marker_exists=False, max_expected_duration_ms=8000)
    assert ok is False and "does not match the recognized" in detail
    # A recognized signature that nonetheless ran too long is still rejected.
    ok, detail = _validate_wall_clock_probe(_result(timed_out=False, exit_code=9, duration_ms=30000), marker_exists=False, max_expected_duration_ms=8000)
    assert ok is False and "longer than the" in detail
    # The process surviving to write its marker is disqualifying regardless
    # of what the duration/timed_out/exit_code fields claim.
    ok, detail = _validate_wall_clock_probe(_result(timed_out=True, duration_ms=3000), marker_exists=True, max_expected_duration_ms=8000)
    assert ok is False and "survived" in detail


def test_output_probe_validator_requires_matching_the_configured_bound() -> None:
    # Clean exit(0), within the configured bound (head+marker+tail): passes.
    assert _validate_output_probe(_result(exit_code=0, stdout="A" * 20029, stdout_truncated=True), configured_bound=20000)[0] is True
    # Shorter than the 50000-char uncapped probe input, but still far more
    # than the configured 20000-char bound would ever retain -- a
    # partially-bounded implementation must be caught, not waved through
    # just because it retained less than the raw input.
    ok, detail = _validate_output_probe(_result(exit_code=0, stdout="A" * 35000, stdout_truncated=True), configured_bound=20000)
    assert ok is False and "exceeds the configured" in detail
    ok, detail = _validate_output_probe(_result(exit_code=0, stdout="A" * 100, stdout_truncated=False), configured_bound=20000)
    assert ok is False and "truncated" in detail
    # An unrelated failed command producing truncated output (e.g. large
    # error spam) is not proof of the retention bound: a non-zero exit code
    # must be rejected even though stdout_truncated and the length both
    # look fine.
    ok, detail = _validate_output_probe(_result(exit_code=1, stdout="A" * 20029, stdout_truncated=True), configured_bound=20000)
    assert ok is False and "clean exit(0)" in detail


# ---- End-to-end gate: real internal probe wiring via a fake transport -----


class _FakeProbeRunner(WslBubblewrapRunner):
    """Overrides only the transport (run()); exercises the real, unmodified
    verify_and_open_resource_isolation()/_run_readiness_probes() pipeline and
    every strict validator against scripted CommandResults -- there is no way
    to reach a passing gate except through this exact code path."""

    def __init__(self, *, script: dict[str, CommandResult], survive_wall_clock: bool = False) -> None:
        super().__init__(
            root_host=wsl_unc_path("/home/test/root"),
            python_root_posix="/home/test/root/env",
            python_executable_posix="/home/test/root/env/bin/python",
        )
        self._script = script
        self._survive_wall_clock = survive_wall_clock
        self.calls: list[list[str]] = []

    def run(self, argv, cwd, timeout_seconds, env):  # type: ignore[override]
        self.calls.append(list(argv))
        code = argv[-1]
        if "time.sleep(30)" in code and self._survive_wall_clock:
            match = re.search(r"/workspace/([^']+)", code)
            assert match is not None
            (Path(cwd) / match.group(1)).write_text("done", encoding="utf-8")
        for marker, result in self._script.items():
            if marker in code:
                return result
        raise AssertionError(f"no scripted probe result for: {argv}")


def _all_pass_script() -> dict[str, CommandResult]:
    return {
        "while True": _result(exit_code=137),
        "bytearray": _result(exit_code=1, stderr="MemoryError"),
        "os.fork": _result(exit_code=3, stderr="[Errno 11] Resource temporarily unavailable"),
        "time.sleep(30)": _result(timed_out=True),
        "sys.stdout.write": _result(exit_code=0, stdout="A" * 20000, stdout_truncated=True),
    }


def test_verify_and_open_resource_isolation_opens_gate_via_real_internal_probes(tmp_path) -> None:
    runner = _FakeProbeRunner(script=_all_pass_script())
    profile = ResourceLimits(cpu_seconds=5, memory_bytes=268435456, max_processes=8)

    results = runner.verify_and_open_resource_isolation(str(tmp_path), profile)

    assert runner.resource_isolation_ready is True
    assert all(entry["passed"] for entry in results.values())
    assert set(results) == {
        "cpu_limit_enforced", "memory_limit_enforced", "process_limit_enforced",
        "wall_clock_timeout_enforced", "retained_output_bounded",
    }
    limits = runner.boundary_guarantee["resource_limits"]
    assert limits["cpu_seconds"] == "prlimit-enforced:5"
    assert limits["memory_bytes"] == "prlimit-enforced:268435456"
    assert limits["max_processes"] == "prlimit-enforced:8"
    assert "bounded-streaming" in limits["retained_output_chars"]


def test_verify_and_open_resource_isolation_rejects_unrelated_crash_and_stays_closed(tmp_path) -> None:
    script = _all_pass_script()
    script["bytearray"] = _result(exit_code=1, stderr="TypeError: unrelated crash")
    runner = _FakeProbeRunner(script=script)
    profile = ResourceLimits(cpu_seconds=5, memory_bytes=268435456, max_processes=8)

    with pytest.raises(ResourceIsolationUnavailable, match="memory_limit_enforced"):
        runner.verify_and_open_resource_isolation(str(tmp_path), profile)
    assert runner.resource_isolation_ready is False
    assert set(runner.boundary_guarantee["resource_limits"]) == {"timeout"}


def test_verify_and_open_resource_isolation_rejects_a_wall_clock_timeout_misclassified_as_a_limit_kill(tmp_path) -> None:
    script = _all_pass_script()
    script["while True"] = _result(exit_code=137, timed_out=True)
    runner = _FakeProbeRunner(script=script)
    profile = ResourceLimits(cpu_seconds=5, memory_bytes=268435456, max_processes=8)

    with pytest.raises(ResourceIsolationUnavailable, match="cpu_limit_enforced"):
        runner.verify_and_open_resource_isolation(str(tmp_path), profile)
    assert runner.resource_isolation_ready is False


def test_verify_and_open_resource_isolation_rejects_a_process_that_survives_the_wall_clock_timeout(tmp_path) -> None:
    runner = _FakeProbeRunner(script=_all_pass_script(), survive_wall_clock=True)
    profile = ResourceLimits(cpu_seconds=5, memory_bytes=268435456, max_processes=8)

    with pytest.raises(ResourceIsolationUnavailable, match="wall_clock_timeout_enforced"):
        runner.verify_and_open_resource_isolation(str(tmp_path), profile)
    assert runner.resource_isolation_ready is False


def _dependencies() -> DependencyPreparation:
    return DependencyPreparation(
        "quixbugs-gcd-smoke-v1", "f" * 64, "4257f44b0ff1181dedaedee6a447e133219fcebf",
        "quixbugs", "gcd", "4257f44b0ff1181dedaedee6a447e133219fcebf",
        "pytest==7.4.4", "a" * 64, "b" * 64,
    )


def test_create_verified_context_default_matches_prior_metadata_only_behavior() -> None:
    root = wsl_unc_path("/home/test/root")
    context = create_verified_context(
        root_host=root, python_root_posix="/home/test/root/env",
        python_executable_posix="/home/test/root/env/bin/python", python_version="3.6.9",
        project_cwd=".", pythonpath=(), reviewed_environment={}, dependencies=_dependencies(),
    )
    assert context.runner.resource_isolation_ready is False
    assert set(context.containment.resource_limits) == {"timeout"}


def test_create_verified_context_reuses_a_prepared_runner_and_matches_boundary(tmp_path) -> None:
    runner = _FakeProbeRunner(script=_all_pass_script())
    profile = ResourceLimits(cpu_seconds=5, memory_bytes=268435456, max_processes=8)
    runner.verify_and_open_resource_isolation(str(tmp_path), profile)
    context = create_verified_context(
        root_host=runner.root_host, python_root_posix="/home/test/root/env",
        python_executable_posix="/home/test/root/env/bin/python", python_version="3.6.9",
        project_cwd=".", pythonpath=(), reviewed_environment={}, dependencies=_dependencies(),
        runner=runner,
    )
    assert context.runner is runner
    assert context.containment.resource_limits == runner.boundary_guarantee["resource_limits"]
    assert context.containment.resource_limits["cpu_seconds"] == "prlimit-enforced:5"


def test_self_test_accepts_a_different_expected_python_version() -> None:
    runner = _runner()
    # Self-test issues a real WSL/Bubblewrap call; here we only assert the
    # fail-closed resource gate still blocks unrelated benchmark execution,
    # confirming self_test()'s new parameter does not bypass resource_isolation_ready.
    assert runner.resource_isolation_ready is False
    with pytest.raises(ResourceIsolationUnavailable):
        runner.run(["/opt/python/bin/python", "-m", "pytest", "x"], runner.root_host + "\\work", 5, {})
