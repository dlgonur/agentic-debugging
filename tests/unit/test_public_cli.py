"""Public CLI ergonomics and install-diagnostics coverage."""

from __future__ import annotations

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
            "ready": False,
        },
    )
    assert ui_cli.main(["--doctor"]) == 2
    output = capsys.readouterr().out
    assert "Status: NOT READY" in output
    assert 'pip install -e ".[app]"' in output
