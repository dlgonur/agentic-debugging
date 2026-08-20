from __future__ import annotations

import subprocess

from agentic_debugger.swerebench import official_eval


def test_official_git_runner_has_bounded_timeout(monkeypatch, tmp_path) -> None:
    def timed_out(*args, **kwargs):
        assert kwargs["timeout"] == official_eval.OFFICIAL_COMMAND_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(official_eval.subprocess, "run", timed_out)
    try:
        official_eval._run(["git", "fetch"], tmp_path)
    except RuntimeError as exc:
        assert "timed out after" in str(exc)
    else:
        raise AssertionError("expected bounded official git timeout")


def test_docker_pull_timeout_is_reported_without_raising(monkeypatch) -> None:
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(official_eval.subprocess, "run", timed_out)
    ok, detail = official_eval._docker_pull("example/image:latest")
    assert ok is False
    assert "timed out after" in detail


def test_cached_docker_image_bypasses_registry_pull(monkeypatch) -> None:
    calls = []

    def inspect(args, **kwargs):
        calls.append(args)
        class Result:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return Result()

    monkeypatch.setattr(official_eval.subprocess, "run", inspect)
    ok, detail = official_eval._docker_pull("example/image:cached")
    assert ok is True
    assert "already present" in detail
    assert calls == [["docker", "image", "inspect", "example/image:cached"]]


def test_pull_timeout_accepts_image_that_is_available_after_wait(monkeypatch) -> None:
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            class Result:
                returncode = 1 if len(calls) == 1 else 0
                stdout = ""
                stderr = "missing" if len(calls) == 1 else ""
            return Result()
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(official_eval.subprocess, "run", run)
    ok, detail = official_eval._docker_pull("example/image:slow")
    assert ok is True
    assert "became available" in detail


def test_pinned_external_evaluator_is_reused_without_fetch(monkeypatch, tmp_path) -> None:
    root = tmp_path / "official-evaluator"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "refs" / "heads" / "main").write_text(official_eval.OFFICIAL_EVALUATOR_COMMIT + "\n", encoding="utf-8")
    calls = []

    def fake_run(args, cwd):
        calls.append(args)
        return official_eval.OFFICIAL_EVALUATOR_COMMIT + "\n"

    monkeypatch.setattr(official_eval, "official_evaluator_root", lambda: root)
    monkeypatch.setattr(official_eval, "_run", fake_run)
    assert official_eval.ensure_official_evaluator() == root
    assert calls == []
