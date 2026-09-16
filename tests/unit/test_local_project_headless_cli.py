"""Headless Local Project CLI: validation contract + one real offline run.

Uses the scripted ``local_project_dummy.py`` model through the production
configured-profile boundary (no provider, no network).  The end-to-end case
runs one real worker session; the rest fail fast before any execution.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.local_project_headless import build_parser, main, run_session

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "command_models"
    / "local_project_dummy.py"
)


def _run(cmd, cwd):
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"{cmd} failed: {result.stderr}"
    return result.stdout.strip()


def _make_repo(root: Path, name: str = "proj") -> Path:
    repo = root / name
    repo.mkdir()
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "test@test.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    (repo / "calculator.py").write_text(
        "def add(a,b):\n    return a - b\n", encoding="utf-8"
    )
    (repo / "test_calculator.py").write_text(
        "from calculator import add\n"
        "def test_add():\n"
        "    assert add(1,2)==3\n",
        encoding="utf-8",
    )
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-m", "initial"], repo)
    return repo


def _write_profile(root: Path, profile_id: str = "dummy-headless") -> None:
    root.mkdir(parents=True, exist_ok=True)
    patch_path = root / "fix.patch"
    patch_path.write_text(
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a,b):\n"
        "-    return a - b\n"
        "+    return a + b\n",
        encoding="utf-8",
    )
    state_dir = root / f"state-{profile_id}"
    state_dir.mkdir(parents=True, exist_ok=True)
    data_file = root / f"data-{profile_id}.json"
    data_file.write_text(
        json.dumps(
            {
                "symbol": "add",
                "file": "calculator.py",
                "hypothesis_id": "h1",
                "statement": "add returns a - b instead of a + b",
                "patch_file": str(patch_path),
                "expressions": [],
            }
        ),
        encoding="utf-8",
    )
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "command-models.json").write_text(
        json.dumps(
            {
                "schema_version": "command-models-v1",
                "profiles": [
                    {
                        "profile_id": profile_id,
                        "display_name": "Dummy headless",
                        "executable": sys.executable,
                        "argv": [
                            str(FIXTURE),
                            "--state-dir",
                            str(state_dir),
                            "--data",
                            str(data_file),
                        ],
                        "request_timeout_seconds": 10,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _args(**overrides):
    base = {
        "project": "",
        "bug": "add returns a - b",
        "bug_file": None,
        "profile": "dummy-headless",
        "repro": 'python -c "from calculator import add; raise SystemExit(0 if add(1,2)==3 else 1)"',
        "verify": 'python -c "from calculator import add; raise SystemExit(0 if add(0,0)==0 else 1)"',
        "root": "",
        "max_elapsed_seconds": 180,
        "verifier_timeout_seconds": None,
        "project_env": "",
    }
    base.update(overrides)
    import argparse

    return argparse.Namespace(**base)


def test_end_to_end_configured_profile(tmp_path):
    """One real headless session: terminal verdict, cleanup, registration."""
    from agentic_debugger.application.history import HistoryStore

    repo = _make_repo(tmp_path, "proj")
    home = tmp_path / "home"
    _write_profile(home)
    outcome = run_session(_args(project=str(repo), root=str(home)))
    assert outcome.session_id is not None
    assert outcome.status == "SUCCEEDED"
    assert outcome.cleanup_verified is True
    assert outcome.registered is True
    assert outcome.registration_error is None
    store = HistoryStore(home)
    assert outcome.session_id in {entry.session_id for entry in store.list_sessions()}
    reopened = store.reopen(outcome.session_id)
    assert reopened.entry.session_id == outcome.session_id
    assert reopened.entry.source_kind is not None


def test_rejects_unknown_profile_before_execution(tmp_path):
    repo = _make_repo(tmp_path, "proj")
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(ValueError, match="profile"):
        run_session(_args(project=str(repo), root=str(home), profile="nope"))


def test_rejects_dirty_repo(tmp_path):
    repo = _make_repo(tmp_path, "proj")
    (repo / "calculator.py").write_text(
        "def add(a,b):\n    return a - b # dirty\n", encoding="utf-8"
    )
    home = tmp_path / "home"
    _write_profile(home)
    with pytest.raises(ValueError, match="uncommitted"):
        run_session(_args(project=str(repo), root=str(home)))


def test_rejects_bad_verifier_timeout(tmp_path):
    repo = _make_repo(tmp_path, "proj")
    home = tmp_path / "home"
    _write_profile(home)
    with pytest.raises(ValueError, match="verifier timeout"):
        run_session(
            _args(project=str(repo), root=str(home), verifier_timeout_seconds=0)
        )


def test_rejects_empty_bug(tmp_path):
    repo = _make_repo(tmp_path, "proj")
    home = tmp_path / "home"
    _write_profile(home)
    with pytest.raises(ValueError, match="non-empty"):
        run_session(_args(project=str(repo), root=str(home), bug="   "))


def test_bug_file_roundtrip(tmp_path):
    repo = _make_repo(tmp_path, "proj")
    home = tmp_path / "home"
    _write_profile(home)
    bug_file = tmp_path / "bug.txt"
    bug_file.write_text("add returns a - b", encoding="utf-8")
    outcome_args = _args(
        project=str(repo), root=str(home), bug=None, bug_file=str(bug_file)
    )
    from scripts.local_project_headless import _read_bug_text

    assert _read_bug_text(outcome_args) == "add returns a - b"


def test_main_exit_code_2_on_unknown_profile(tmp_path, capsys):
    repo = _make_repo(tmp_path, "proj")
    home = tmp_path / "home"
    home.mkdir()
    code = main(
        [
            "--project",
            str(repo),
            "--bug",
            "x",
            "--profile",
            "nope",
            "--root",
            str(home),
        ]
    )
    assert code == 2
    assert "profile" in capsys.readouterr().err


def test_parser_requires_bug_source():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--project", ".", "--profile", "p"])
    assert exc.value.code == 2
