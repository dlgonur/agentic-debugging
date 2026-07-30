from __future__ import annotations

import pytest

from agentic_debugger.bugsinpy.wsl import (
    MINIFORGE_SHA256_URL, MINIFORGE_URL, RUNNER_ID, ResourceIsolationUnavailable,
    WslBubblewrapRunner, WslGateError, WslProcess, build_bwrap_command,
    build_wsl_command, fingerprint_environment, to_wsl_path, wsl_unc_path,
)


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
