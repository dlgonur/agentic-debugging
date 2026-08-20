from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentic_debugger.swerebench import materialize


def test_git_materialization_is_bounded_and_reports_timeout(monkeypatch, tmp_path) -> None:
    def timed_out(*args, **kwargs):
        assert kwargs["timeout"] == materialize.GIT_COMMAND_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(materialize.subprocess, "run", timed_out)
    with pytest.raises(materialize.MaterializationError, match="timed out after"):
        materialize._run_git(["fetch", "origin", "deadbeef"], tmp_path)


def test_git_timeout_falls_back_to_exact_b14_cache(monkeypatch, tmp_path) -> None:
    dest_parent = tmp_path / "dest"
    dest_parent.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    calls: list[list[str]] = []

    def fake_run_git(args, cwd):
        calls.append(list(args))
        if args[:1] == ["fetch"]:
            raise materialize.MaterializationError("git fetch timed out after 90 seconds")
        if args[:1] == ["clone"]:
            Path(args[-1]).mkdir()
            return ""
        if args[:1] == ["rev-parse"]:
            return "deadbeef"
        return ""

    monkeypatch.setattr(materialize, "_run_git", fake_run_git)
    result = materialize.materialize_base_commit(
        instance_id="example-1",
        repo="owner/repo",
        repo_canonical="owner/repo",
        base_commit="deadbeef",
        dest_parent=dest_parent,
        cache_index={"owner/repo": cache},
    )
    assert result == dest_parent / "example-1"
    assert any(call[:1] == ["clone"] for call in calls)
