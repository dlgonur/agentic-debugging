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


def test_official_bounds_are_separate_and_above_historical_120_seconds() -> None:
    assert official_eval.OFFICIAL_GIT_COMMAND_TIMEOUT_SECONDS == 90.0
    assert official_eval.OFFICIAL_DOCKER_COMMAND_TIMEOUT_SECONDS == 300.0
    assert official_eval.OFFICIAL_EVALUATOR_WATCHDOG_SECONDS == 360.0
    assert official_eval.OFFICIAL_EVALUATOR_WATCHDOG_SECONDS == (
        official_eval.OFFICIAL_TASK_TIMEOUT_SECONDS
        + official_eval.OFFICIAL_EVALUATOR_STARTUP_MARGIN_SECONDS
    )
    assert official_eval.OFFICIAL_DOCKER_COMMAND_TIMEOUT_SECONDS > official_eval.OFFICIAL_GIT_COMMAND_TIMEOUT_SECONDS
    assert official_eval.OFFICIAL_EVALUATOR_WATCHDOG_SECONDS > official_eval.OFFICIAL_DOCKER_COMMAND_TIMEOUT_SECONDS


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


def test_official_eval_timeout_preserves_typed_failure_evidence(monkeypatch, tmp_path) -> None:
    evaluator = tmp_path / "official-evaluator"
    (evaluator / "scripts").mkdir(parents=True)
    monkeypatch.setattr(official_eval, "ensure_official_evaluator", lambda: evaluator)

    class TimedOutProcess:
        returncode = -9

        def communicate(self, timeout=None):
            if timeout == official_eval.OFFICIAL_EVALUATOR_WATCHDOG_SECONDS:
                raise subprocess.TimeoutExpired("official-eval", timeout)
            return "", ""

        def kill(self):
            return None

    monkeypatch.setattr(official_eval.subprocess, "Popen", lambda *args, **kwargs: TimedOutProcess())
    monkeypatch.setattr(official_eval, "_terminate_official_process", lambda process: None)
    result = official_eval._run_official_eval(
        tmp_path / "spec.json", tmp_path / "report.json", tmp_path
    )
    assert result["failure_kind"] == "timeout"
    assert result["exit_code"] is None
    assert result["elapsed_seconds"] >= 0
    assert f"watchdog timed out after {official_eval.OFFICIAL_EVALUATOR_WATCHDOG_SECONDS:g}" in result["stderr_tail"]
    assert result["timeout_stage"] == "child_process_wait"
    assert result["stage_evidence"]


def test_pull_timeout_uses_short_post_timeout_probe(monkeypatch) -> None:
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs.get("timeout")))
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(official_eval.subprocess, "run", run)
    ok, detail = official_eval._docker_pull("example/image:missing")
    assert ok is False
    assert "timed out after 300 seconds" in detail
    assert calls[-1][1] == official_eval.OFFICIAL_DOCKER_POST_TIMEOUT_PROBE_SECONDS


def test_timeout_stage_reports_running_container(monkeypatch, tmp_path) -> None:
    report = tmp_path / "missing-report.json"

    def run(args, **kwargs):
        class Result:
            returncode = 0
            stdout = "abc123 Up 5 minutes example/image:cached\n"
            stderr = ""
        assert args[:2] == ["docker", "ps"]
        return Result()

    monkeypatch.setattr(official_eval.subprocess, "run", run)
    evidence = []
    assert official_eval._timeout_stage(
        image="example/image:cached",
        report_path=report,
        stage_evidence=evidence,
    ) == "test_execution"
    assert evidence[0]["stage"] == "test_execution"


def test_baseline_projection_keeps_evaluator_timeout_typed(monkeypatch, tmp_path) -> None:
    class Public:
        instance_id = "owner__repo-1"

    class Bundle:
        public = Public()

        def image_name(self):
            return "example/image:latest"

        def test_patch(self):
            return "diff --git a/test b/test\n"

        def hidden_tests(self):
            return (["test_fail"], ["test_pass"])

    monkeypatch.setattr(official_eval, "_docker_available", lambda: True)
    monkeypatch.setattr(official_eval, "_docker_pull", lambda *args, **kwargs: (True, "pulled"))
    monkeypatch.setattr(official_eval.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(official_eval, "_write_isolated_spec", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        official_eval,
        "_run_official_eval",
        lambda *args, **kwargs: {
            "exit_code": None,
            "report": {},
            "report_error": None,
            "failure_kind": "timeout",
            "elapsed_seconds": 120.0,
                "stderr_tail": f"official evaluator watchdog timed out after {official_eval.OFFICIAL_EVALUATOR_WATCHDOG_SECONDS:g} seconds",
        },
    )
    result = official_eval.run_official_baseline_check(Bundle(), work_root=tmp_path)
    assert result["reason"] == "official_evaluator_timeout"
    assert result["failure_kind"] == "timeout"
    assert result["elapsed_seconds"] == 120.0
