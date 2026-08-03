"""Shared fixtures: wrapper-form adapter configuration and the fake OpenCode
shim environment used by the OpenCode Go execution adapter tests.

The fake launcher directory provides BOTH the ``opencode.cmd`` batch shim
(for the short local inspection commands that may continue through the
launcher) AND a deterministic compiled fake native ``opencode.exe`` (the
real protocol wrapper resolves and version-proves the native executable and
invokes it directly for ``opencode run``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))
WRAPPER_PATH = REPO_ROOT / "scripts" / "opencode_protocol_transport.py"

import opencode_go_synthetic_executable as synthetic  # noqa: E402
from scripts import opencode_protocol_transport as transport  # noqa: E402


def synthetic_catalog_entry(runtime_model_id: str) -> dict:
    """The exact synthetic catalog entry for a synthetic runtime model id."""
    provider, model_id = runtime_model_id.split("/", 1) if "/" in runtime_model_id else ("", runtime_model_id)
    entry = next(
        item for item in synthetic.SYNTHETIC_CATALOG_ENTRIES
        if item.get("providerID") == provider and item.get("id") == model_id
    )
    return entry


def synthetic_catalog_fingerprint(runtime_model_id: str) -> str:
    """The deterministic catalog-entry fingerprint of the exact synthetic
    catalog entry; the real wrapper independently recomputes it during its
    OpenCode Go preflight, so every fixture must agree exactly."""
    return transport.catalog_entry_fingerprint(synthetic_catalog_entry(runtime_model_id))


def wrapper_command(interpreter: str, runtime_model_id: str, *, variant: str = "max", catalog_fingerprint: str | None = None, opencode_version: str = "1.0.0", account_status: str = "ACTIVE") -> list[str]:
    return [
        interpreter,
        str(WRAPPER_PATH),
        "--model", runtime_model_id,
        "--variant", variant,
        "--route-mode", "opencode-go",
        "--expected-opencode-version", opencode_version,
        "--expected-catalog-fingerprint", catalog_fingerprint if catalog_fingerprint is not None else synthetic_catalog_fingerprint(runtime_model_id),
        "--expected-runtime-model-id", runtime_model_id,
        "--expected-account-status", account_status,
        "--expected-billing-route", "SUBSCRIPTION",
    ]


def wrapper_environment_allowlist() -> list[str]:
    return ["PATH", "SystemRoot", "USERPROFILE", "HOME", "TMP", "TEMP"]


def prepare_wrapper_environment(tmp_path: Path, synthetic_executable: Path) -> dict[str, str]:
    """Create the fake ``opencode.cmd`` shim, the compiled fake native
    ``opencode.exe`` in the trusted npm package layout, and the fake profile
    with synthetic auth state, and return the transport environment
    override."""
    synthetic_executable = synthetic_executable.resolve()
    shim_dir = tmp_path / "fake-bin"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "opencode.cmd"
    shim.write_text(
        "@echo off\r\n" + f'"{sys.executable}" "{synthetic_executable}" %*\r\n',
        encoding="utf-8",
    )
    native_bin = shim_dir / "node_modules" / "opencode-ai" / "node_modules" / "opencode-windows-x64" / "bin"
    native_bin.mkdir(parents=True, exist_ok=True)
    synthetic.build_fake_native_executable(native_bin, target_script=synthetic_executable)
    profile = tmp_path / "fake-profile"
    auth = profile / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    return {
        "PATH": str(shim_dir) + os.pathsep + os.environ.get("PATH", ""),
        "USERPROFILE": str(profile),
        "HOME": str(profile),
        "TMP": str(tmp_path),
        "TEMP": str(tmp_path),
    }


def prepare_fake_launcher_dir(tmp_path: Path) -> dict[str, str]:
    """A deterministic fake launcher directory for MOCKED subprocess tests,
    mirroring the production npm layout.

    Creates ``opencode.cmd`` plus a dummy ``opencode.exe`` regular file at
    the trusted npm package location
    ``<launcher-dir>\\node_modules\\opencode-ai\\node_modules\\opencode-windows-x64\\bin\\opencode.exe``
    so the wrapper's trusted npm-package native resolution (root containment,
    regular file) succeeds.  ``shutil.which`` must be monkeypatched to return
    ``fixture["launcher"]`` and the mocked ``subprocess.run`` must answer
    ``[fixture["native"], "--version"]`` with the expected version.
    """
    fake_bin = tmp_path / "fake-launcher"
    fake_bin.mkdir(parents=True, exist_ok=True)
    launcher = fake_bin / "opencode.cmd"
    launcher.write_text("@echo off\r\n", encoding="utf-8")
    native = fake_bin / "node_modules" / "opencode-ai" / "node_modules" / "opencode-windows-x64" / "bin" / "opencode.exe"
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_bytes(b"dummy native executable fixture (mocked subprocess tests)")
    return {"launcher": str(launcher), "native": str(native), "bin": str(fake_bin)}
