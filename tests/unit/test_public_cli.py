"""Public CLI ergonomics and install-diagnostics coverage."""

from __future__ import annotations

import platform
import sys
import tomllib
from pathlib import Path

import pytest

from agentic_debugger import __version__
from agentic_debugger.ui import __main__ as ui_cli


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_package_version_matches_project_metadata() -> None:
    metadata = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert metadata["project"]["version"] == __version__


def test_version_does_not_require_textual(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        ui_cli.main(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"agentic-debugger {__version__}"


def test_doctor_reports_packaged_offline_resources(capsys) -> None:
    diagnostics = ui_cli.collect_diagnostics()
    assert diagnostics["python_supported"] is True
    assert int(diagnostics["curated_tasks"]) >= 5

    status = ui_cli.render_diagnostics(diagnostics)
    output = capsys.readouterr().out
    assert f"Agentic Debugger {__version__}" in output
    assert "Curated task manifests:" in output
    assert "Status:" in output
    assert status in (0, 2)


def test_doctor_missing_textual_is_actionable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        ui_cli,
        "collect_diagnostics",
        lambda: {
            "version": __version__,
            "python_version": "3.11.0",
            "python_supported": True,
            "textual_version": None,
            "curated_tasks": 8,
            "providers": [],
            "ready": False,
        },
    )
    assert ui_cli.main(["--doctor"]) == 2
    output = capsys.readouterr().out
    assert "Status: NOT READY" in output
    assert 'pip install -e ".[app]"' in output


def test_package_scripts_metadata_defines_both_launch_aliases() -> None:
    metadata = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    scripts = metadata.get("project", {}).get("scripts", {})
    assert "agentic-debugger" in scripts
    assert "agenticdebugger" in scripts
    assert scripts["agentic-debugger"] == "agentic_debugger.ui.__main__:main"
    assert scripts["agenticdebugger"] == "agentic_debugger.ui.__main__:main"
    # Ensure generic namespace 'agentic' is NOT claimed
    assert "agentic" not in scripts
    # Ensure no extraneous scripts are defined
    assert set(scripts.keys()) == {"agentic-debugger", "agenticdebugger"}

    # Validate target resolves to actual callable entry point
    module_name, func_name = scripts["agenticdebugger"].split(":")
    import importlib

    mod = importlib.import_module(module_name)
    func = getattr(mod, func_name)
    assert callable(func)
    assert func is ui_cli.main


def test_prog_detection_from_sys_argv(monkeypatch) -> None:
    # Explicit prog always wins
    assert ui_cli._detect_prog("custom") == "custom"

    # Auto-detection from sys.argv[0] with host-neutral basename parsing
    # Handles bare commands
    monkeypatch.setattr("sys.argv", ["agenticdebugger"])
    assert ui_cli._detect_prog() == "agenticdebugger"

    monkeypatch.setattr("sys.argv", ["agenticdebugger.exe"])
    assert ui_cli._detect_prog() == "agenticdebugger"

    monkeypatch.setattr("sys.argv", ["agentic-debugger"])
    assert ui_cli._detect_prog() == "agentic-debugger"

    monkeypatch.setattr("sys.argv", ["agentic-debugger.exe"])
    assert ui_cli._detect_prog() == "agentic-debugger"

    # Windows paths with backslashes (must resolve on POSIX and Windows hosts)
    monkeypatch.setattr(
        "sys.argv", [r"C:\Python314\Scripts\agenticdebugger.exe", "--version"]
    )
    assert ui_cli._detect_prog() == "agenticdebugger"

    monkeypatch.setattr(
        "sys.argv",
        [
            r"C:\Users\User\AppData\Local\AgenticDebugger\cli-venv\Scripts\agenticdebugger.exe",
            "--help",
        ],
    )
    assert ui_cli._detect_prog() == "agenticdebugger"

    monkeypatch.setattr(
        "sys.argv", [r"C:\Python314\Scripts\agentic-debugger.exe", "--version"]
    )
    assert ui_cli._detect_prog() == "agentic-debugger"

    monkeypatch.setattr(
        "sys.argv",
        [
            r"C:\Users\User\AppData\Local\AgenticDebugger\cli-venv\Scripts\agentic-debugger.exe",
            "--help",
        ],
    )
    assert ui_cli._detect_prog() == "agentic-debugger"

    # POSIX paths with forward slashes
    monkeypatch.setattr(
        "sys.argv", ["/usr/local/bin/agenticdebugger", "--version"]
    )
    assert ui_cli._detect_prog() == "agenticdebugger"

    monkeypatch.setattr(
        "sys.argv", ["/usr/local/bin/agentic-debugger", "--version"]
    )
    assert ui_cli._detect_prog() == "agentic-debugger"

    # Case-insensitive matching
    monkeypatch.setattr("sys.argv", [r"C:\BIN\AGENTICDEBUGGER.EXE"])
    assert ui_cli._detect_prog() == "agenticdebugger"

    monkeypatch.setattr("sys.argv", [r"C:\BIN\AGENTIC-DEBUGGER.EXE"])
    assert ui_cli._detect_prog() == "agentic-debugger"

    # Fallback to canonical agentic-debugger when called under pytest or python
    monkeypatch.setattr("sys.argv", ["pytest"])
    assert ui_cli._detect_prog() == "agentic-debugger"

    monkeypatch.setattr("sys.argv", [])
    assert ui_cli._detect_prog() == "agentic-debugger"


def test_cli_help_parity_between_aliases(capsys) -> None:
    # Help under agenticdebugger
    with pytest.raises(SystemExit) as exc1:
        ui_cli.main(["--help"], prog="agenticdebugger")
    assert exc1.value.code == 0
    help_unhyphenated = capsys.readouterr().out

    # Help under agentic-debugger
    with pytest.raises(SystemExit) as exc2:
        ui_cli.main(["--help"], prog="agentic-debugger")
    assert exc2.value.code == 0
    help_hyphenated = capsys.readouterr().out

    assert "usage: agenticdebugger" in help_unhyphenated
    assert "usage: agentic-debugger" in help_hyphenated

    # Both parsers share the exact same description
    p1 = ui_cli.build_parser("agenticdebugger")
    p2 = ui_cli.build_parser("agentic-debugger")
    assert p1.description == p2.description

    # Both parsers define identical actions, option strings, and help strings
    actions1 = [(a.option_strings, a.dest, a.help) for a in p1._actions]
    actions2 = [(a.option_strings, a.dest, a.help) for a in p2._actions]
    assert actions1 == actions2

    # Both contain all identical options in the rendered help text
    for opt in (
        "--version",
        "--doctor",
        "--list-sessions",
        "--export-session",
        "--root",
        "--project",
        "--output",
    ):
        assert opt in help_unhyphenated
        assert opt in help_hyphenated


def test_cli_version_parity_between_aliases(capsys) -> None:
    with pytest.raises(SystemExit) as exc1:
        ui_cli.main(["--version"], prog="agenticdebugger")
    assert exc1.value.code == 0
    assert capsys.readouterr().out.strip() == f"agenticdebugger {__version__}"

    with pytest.raises(SystemExit) as exc2:
        ui_cli.main(["--version"], prog="agentic-debugger")
    assert exc2.value.code == 0
    assert capsys.readouterr().out.strip() == f"agentic-debugger {__version__}"


def test_cli_doctor_parity_between_aliases(capsys) -> None:
    status1 = ui_cli.main(["--doctor"], prog="agenticdebugger")
    out1 = capsys.readouterr().out

    status2 = ui_cli.main(["--doctor"], prog="agentic-debugger")
    out2 = capsys.readouterr().out

    assert status1 == status2
    assert out1 == out2
    assert f"Agentic Debugger {__version__}" in out1


def test_cli_list_sessions_parity_between_aliases(tmp_path: Path, capsys) -> None:
    rc1 = ui_cli.main(
        ["--root", str(tmp_path), "--list-sessions"], prog="agenticdebugger"
    )
    out1 = capsys.readouterr().out

    rc2 = ui_cli.main(
        ["--root", str(tmp_path), "--list-sessions"], prog="agentic-debugger"
    )
    out2 = capsys.readouterr().out

    assert rc1 == 0
    assert rc2 == 0
    assert out1 == out2


def test_cli_argument_validation_parity(capsys) -> None:
    with pytest.raises(SystemExit) as exc1:
        ui_cli.main(["--output", "report.md"], prog="agenticdebugger")
    assert exc1.value.code == 2
    err1 = capsys.readouterr().err

    with pytest.raises(SystemExit) as exc2:
        ui_cli.main(["--output", "report.md"], prog="agentic-debugger")
    assert exc2.value.code == 2
    err2 = capsys.readouterr().err

    assert "--output requires --export-session" in err1
    assert "--output requires --export-session" in err2


def test_cli_tui_startup_parity_single_launch_authority(monkeypatch) -> None:
    calls: list[tuple[str | None, str | None]] = []
    capture_calls = []

    class DummyApp:
        def __init__(self, history_root=None, initial_project=None):
            calls.append((history_root, initial_project))

        def run(self):
            return 0

    monkeypatch.setattr(ui_cli, "_require_textual", lambda: None)
    monkeypatch.setattr(
        "agentic_debugger.application.local_project.capture_launch_cwd",
        lambda: capture_calls.append(True),
    )
    monkeypatch.setattr("agentic_debugger.ui.app.LocalApplicationV1", DummyApp)

    # 1. Launch via agenticdebugger alias with defaults
    rc1 = ui_cli.main([], prog="agenticdebugger")
    assert rc1 == 0
    assert len(capture_calls) == 1
    assert calls[-1] == (None, None)

    # 2. Launch via agentic-debugger canonical with defaults
    rc2 = ui_cli.main([], prog="agentic-debugger")
    assert rc2 == 0
    assert len(capture_calls) == 2
    assert calls[-1] == (None, None)

    # 3. Launch via agenticdebugger with --root and --project
    rc3 = ui_cli.main(
        ["--root", "my-root", "--project", "my-proj"], prog="agenticdebugger"
    )
    assert rc3 == 0
    assert len(capture_calls) == 3
    assert calls[-1] == ("my-root", "my-proj")

    # 4. Launch via agentic-debugger with --root and --project
    rc4 = ui_cli.main(
        ["--root", "my-root", "--project", "my-proj"], prog="agentic-debugger"
    )
    assert rc4 == 0
    assert len(capture_calls) == 4
    assert calls[-1] == ("my-root", "my-proj")


def test_powershell_installer_script_contract() -> None:
    ps1_path = REPO_ROOT / "scripts" / "install_windows_alias.ps1"
    assert ps1_path.is_file(), "scripts/install_windows_alias.ps1 must exist"

    content = ps1_path.read_text(encoding="utf-8")
    # Must never embed machine-specific paths
    assert "C:\\Users\\" not in content
    assert "benya" not in content

    # Must contain essential parameters and guards
    assert "[switch]$Uninstall" in content
    assert "agenticdebugger.exe" in content
    assert "agentic-debugger.exe" in content
    assert "-e $repoRoot" in content or "pip install" in content

    # Regression check (F1, F2): Must NOT use caller interpreter's shared sysconfig scripts path
    assert "sysconfig.get_path" not in content, (
        "Installer must not write caller interpreter's sysconfig.get_path('scripts') "
        "into persistent User PATH."
    )

    # Must target dedicated application-owned venv directory
    assert "cli-venv" in content
    assert "AgenticDebugger" in content

    # Must manage User PATH via HKCU\Environment safely without admin
    assert "Environment]::SetEnvironmentVariable('Path'" in content

    # Collision detection must check both alias names against external executables (F3)
    assert "Aborting installation to prevent collision" in content

    # Uninstall must remove app-owned venv and remove launcher directory without global pip uninstall (F4)
    assert "Remove-Item -Recurse -Force $cliVenv" in content
    assert "pip uninstall -y" not in content


@pytest.mark.skipif(
    platform.system() != "Windows",
    reason="Windows PowerShell installer integration smoke",
)
def test_windows_installer_isolated_lifecycle_smoke(tmp_path: Path) -> None:
    import subprocess
    import winreg

    # Read current User PATH from registry
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
        ) as key:
            orig_user_path, _ = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        orig_user_path = ""

    ps1_path = REPO_ROOT / "scripts" / "install_windows_alias.ps1"
    test_install_dir = tmp_path / "test_cli_venv"

    try:
        # 1. Fresh install into isolated test directory
        proc_install = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1_path),
                "-InstallDir",
                str(test_install_dir),
            ],
            capture_output=True,
            text=True,
        )
        assert proc_install.returncode == 0, (
            f"Installer failed:\nSTDOUT:\n{proc_install.stdout}\nSTDERR:\n{proc_install.stderr}"
        )

        scripts_dir = test_install_dir / "Scripts"
        assert (scripts_dir / "agenticdebugger.exe").is_file()
        assert (scripts_dir / "agentic-debugger.exe").is_file()

        # Verify app-owned launcher dir was added to User PATH
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
        ) as key:
            updated_path, _ = winreg.QueryValueEx(key, "Path")
        assert str(scripts_dir).lower() in updated_path.lower()

        # 2. Re-run: install twice must succeed without duplicating PATH entry
        proc_reinstall = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1_path),
                "-InstallDir",
                str(test_install_dir),
            ],
            capture_output=True,
            text=True,
        )
        assert proc_reinstall.returncode == 0, f"Re-install failed:\n{proc_reinstall.stderr}"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
        ) as key:
            recheck_path, _ = winreg.QueryValueEx(key, "Path")
        entries = [
            e.strip().rstrip("\\").lower()
            for e in recheck_path.split(";")
            if e.strip()
        ]
        assert entries.count(str(scripts_dir).lower().rstrip("\\")) == 1

        # 3. Uninstall: must remove app-owned directory and remove launcher from User PATH
        proc_uninstall = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1_path),
                "-InstallDir",
                str(test_install_dir),
                "-Uninstall",
            ],
            capture_output=True,
            text=True,
        )
        assert proc_uninstall.returncode == 0, f"Uninstall failed:\n{proc_uninstall.stderr}"
        assert not test_install_dir.exists()

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
        ) as key:
            clean_path, _ = winreg.QueryValueEx(key, "Path")
        assert str(scripts_dir).lower() not in clean_path.lower()

        # 4. Repeated uninstall must be harmless/idempotent
        proc_uninstall2 = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1_path),
                "-InstallDir",
                str(test_install_dir),
                "-Uninstall",
            ],
            capture_output=True,
            text=True,
        )
        assert proc_uninstall2.returncode == 0, f"Second uninstall failed:\n{proc_uninstall2.stderr}"

    finally:
        # Guarantee restoration of developer's exact original User PATH
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "Path", 0, winreg.REG_SZ, orig_user_path)


@pytest.mark.skipif(
    platform.system() != "Windows",
    reason="Windows PowerShell installer integration smoke",
)
def test_windows_installer_active_unrelated_venv_not_persisted_to_path(
    tmp_path: Path,
) -> None:
    import subprocess
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
        ) as key:
            orig_user_path, _ = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        orig_user_path = ""

    ps1_path = REPO_ROOT / "scripts" / "install_windows_alias.ps1"
    unrelated_venv = tmp_path / "project_active_venv"
    app_owned_venv = tmp_path / "app_owned_venv"

    unrelated_scripts = unrelated_venv / "Scripts"
    unrelated_scripts.mkdir(parents=True)
    unrelated_python = sys.executable

    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1_path),
                "-Python",
                unrelated_python,
                "-InstallDir",
                str(app_owned_venv),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"Installer failed:\n{proc.stderr}"

        # Read User PATH from registry
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
        ) as key:
            user_path, _ = winreg.QueryValueEx(key, "Path")

        # Prove the active unrelated venv's Scripts is NOT persisted to User PATH
        assert str(unrelated_scripts).lower() not in user_path.lower()

        # Prove ONLY the app-owned venv's Scripts directory was persisted
        app_scripts = app_owned_venv / "Scripts"
        assert str(app_scripts).lower() in user_path.lower()

        # Clean up via installer uninstall
        proc_uninst = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1_path),
                "-InstallDir",
                str(app_owned_venv),
                "-Uninstall",
            ],
            capture_output=True,
            text=True,
        )
        assert proc_uninst.returncode == 0
    finally:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "Path", 0, winreg.REG_SZ, orig_user_path)
