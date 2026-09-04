"""07 regression: a STARTED Local Project lifecycle always owns the V2 authority.

Repair 06 created the one per-session ExecutionEnvironment in
``worker.run_worker``.  Repair 07 orders it AFTER the true pre-start
gate but BEFORE ``SESSION_STARTED``, so a cancellation/timeout landing in
the started->scenario window still has the explicit project-safe
authority for terminal cleanup (no ``UnboundLocalError``, no
``environment=None`` fallback into full worker inheritance).

Deterministic: the second token checkpoint raises via monkeypatched
``CancellationToken.check`` — no timing race.  Only synthetic values are
used; no real credential is ever constructed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agentic_debugger.application.events import SourceKind
from agentic_debugger.application.session import SessionBudgets, SessionSpec
from agentic_debugger.application.sources import ExecutionSourceSpec
from agentic_debugger.cancellation import (
    CancellationError,
    CancellationReason,
    CancellationToken,
)

SYNTHETIC_HOP_VAR = "AGENTIC_DEBUGGER_PROVIDER_T07_API_KEY"
SYNTHETIC_HOP_VALUE = "sk-synthetic-v207-hop-value-not-a-real-credential"
CONTROL_VAR = "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH"
BENIGN_VAR = "V2_07_BENIGN_PROJECT_DSN"
BENIGN_VALUE = "service://synthetic/test-dsn"


def _make_git_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    for argv in (
        ["init"],
        ["config", "user.email", "t@t.t"],
        ["config", "user.name", "T"],
    ):
        result = subprocess.run(
            ["git", *argv],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    for argv in (["add", "."], ["commit", "-m", "init"]):
        result = subprocess.run(
            ["git", *argv],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    return repo


def test_started_cancellation_keeps_explicit_cleanup_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation at the started->scenario checkpoint stays CANCELLED
    (never CLEANUP_FAILED) and cleanup Git children get explicit env."""
    import agentic_debugger.application.worker as worker_module
    from agentic_debugger.application import local_project as lp
    from agentic_debugger.application.worker_protocol import StartRequest

    monkeypatch.setenv(SYNTHETIC_HOP_VAR, SYNTHETIC_HOP_VALUE)
    monkeypatch.setenv(CONTROL_VAR, "/synthetic/provider-config.json")
    monkeypatch.setenv(BENIGN_VAR, BENIGN_VALUE)

    repo = _make_git_repo(tmp_path)
    parent = tmp_path / "wt-parent"
    parent.mkdir()
    (parent / "worktree").mkdir()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    work_dir = tmp_path / "work"

    # Capture worker->parent messages (ready/event/terminal).
    sent: list[bytes] = []
    monkeypatch.setattr(
        worker_module, "_send", lambda payload: sent.append(bytes(payload))
    )
    # No protocol stdin in this test; the cancel reader is irrelevant.
    monkeypatch.setattr(worker_module, "_start_cancel_reader", lambda token: None)

    # Deterministic started->scenario cancellation: the first checkpoint
    # (pre-start gate) passes; the second one raises.
    real_check = CancellationToken.check
    calls = {"n": 0}

    def _counted_check(self) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise CancellationError(CancellationReason.CANCELLED)
        return real_check(self)

    monkeypatch.setattr(CancellationToken, "check", _counted_check)

    # The Local Project source must never execute in this window.
    source_calls: list = []

    def _forbidden_source(name, ctx, params):
        source_calls.append(name)
        raise AssertionError("source must not run after started->scenario cancel")

    monkeypatch.setattr(worker_module, "run_worker_source", _forbidden_source)

    # Observe the real cleanup Git children at the subprocess boundary.
    git_envs: list[dict] = []
    real_run = subprocess.run

    def _capture_run(argv, **kwargs):
        if isinstance(argv, list) and argv[:2] == ["git", "worktree"]:
            git_envs.append(dict(kwargs.get("env") or {}))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(lp.subprocess, "run", _capture_run)

    request = StartRequest(
        session_id="sess-v207-started-cleanup",
        spec=SessionSpec(
            task_id="local-project-debug",
            source=ExecutionSourceSpec(
                kind=SourceKind.LOCAL_PROJECT,
                task_id="local-project-debug",
                model_config_ref="v207-profile",
            ),
            budgets=SessionBudgets(),
        ),
        run_id="run-v207-started-cleanup",
        work_dir=str(work_dir),
        journal_path=str(session_dir / "session.events.jsonl"),
        scenario="local_project",
        scenario_params={
            "parent_tmpdir": str(parent),
            "project_repo_path": str(repo),
        },
        max_elapsed_seconds=None,
        pre_start_delay_seconds=0.0,
    )

    previous_cwd = os.getcwd()
    try:
        exit_code = worker_module.run_worker(request)
    finally:
        os.chdir(previous_cwd)
    assert exit_code == 0

    # 1+2: the session reached SESSION_STARTED, then cancelled at the
    # started->scenario checkpoint — before any source/model execution.
    assert calls["n"] >= 2
    assert source_calls == []
    from agentic_debugger.application.journal import read_session_journal

    journal = read_session_journal(session_dir / "session.events.jsonl")
    kinds = [event.event_kind.value for event in journal.events]
    assert "session.started" in kinds

    # 3+4: terminal cleanup executed and stayed truthful.
    assert "cleanup.completed" in kinds
    terminals = [
        json.loads(payload.decode("utf-8"))
        for payload in sent
        if json.loads(payload.decode("utf-8")).get("type") == "terminal"
    ]
    assert len(terminals) == 1
    result = terminals[0]["result"]
    # 8: legitimate cancellation, NOT a cleanup failure.
    assert result["status"] == "cancelled", result
    assert result["termination_reason"] == "cancelled", result
    assert result["cleanup_verified"] is True, result
    # 5: no UnboundLocalError lifecycle hole anywhere.
    assert "UnboundLocalError" not in json.dumps(result, sort_keys=True, default=str)
    journal_text = "\n".join(
        json.dumps(event.payload, sort_keys=True, default=str)
        for event in journal.events
    )
    assert "UnboundLocalError" not in journal_text

    # 6+7: cleanup Git children received explicit project-safe env.
    assert git_envs, "worker terminal cleanup Git children did not run"
    for env in git_envs:
        assert env, "cleanup Git child omitted env="
        assert SYNTHETIC_HOP_VAR not in env
        assert CONTROL_VAR not in env
        assert SYNTHETIC_HOP_VALUE not in list(env.values())
        # 9: benign bridge state preserved through the same authority.
        assert env[BENIGN_VAR] == BENIGN_VALUE


def test_started_cleanup_never_calls_cleanup_with_inherit() -> None:
    """Source assertion: the STARTED Local Project terminal-cleanup path
    cannot pass ``environment=None`` (the legacy inherit fallback)."""
    source = (
        Path(__file__).resolve().parents[2]
        / "agentic_debugger"
        / "application"
        / "worker.py"
    ).read_text(encoding="utf-8")
    # The fail-closed guard precedes the cleanup call on the same path.
    assert "local_project session authority missing" in source
    assert "environment=cleanup_environment" in source
    # No other cleanup_parent_tmpdir call in the worker may omit env.
    for line in source.splitlines():
        if "cleanup_parent_tmpdir(" in line and "environment=" not in line:
            following = source.split(line, 1)[1][:400]
            assert "environment=cleanup_environment" in following, (
                f"worker cleanup call without explicit env: {line.strip()}"
            )
