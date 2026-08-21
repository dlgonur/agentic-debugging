from __future__ import annotations

import subprocess
import time
import json
from agentic_debugger.swerebench.execution import OfficialSWERebenchVerifier

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
        timed_out_once = False

        def communicate(self, timeout=None):
            if not self.timed_out_once and timeout is not None and timeout <= official_eval.OFFICIAL_EVALUATOR_WATCHDOG_SECONDS:
                self.timed_out_once = True
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


def test_official_eval_retains_report_hash_and_lifecycle_before_cleanup(monkeypatch, tmp_path) -> None:
    evaluator = tmp_path / "official-evaluator"
    (evaluator / "scripts").mkdir(parents=True)
    spec = tmp_path / "spec.json"
    spec.write_text('[{"image_name":"example/image:latest"}]', encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text('{"items": [{"instance_id": "owner__repo-1", "passed_match": false, "from_fail_to_pass": [], "failed_from_pass_to_pass": [], "error": null}]}', encoding="utf-8")
    monkeypatch.setattr(official_eval, "ensure_official_evaluator", lambda: evaluator)
    monkeypatch.setattr(official_eval, "_docker_probe", lambda: {"ps": {"exit_code": 0}, "ps_all": {"exit_code": 0}})
    monkeypatch.setattr(official_eval, "_docker_events", lambda *args: {"status": "completed", "events": [{"status": "die"}]})
    monkeypatch.setattr(official_eval, "_process_snapshot", lambda pid: [{"pid": pid}])

    class CompletedProcess:
        pid = 1234
        returncode = 1
        def communicate(self, timeout=None):
            return "JSON report written", ""

    monkeypatch.setattr(official_eval.subprocess, "Popen", lambda *args, **kwargs: CompletedProcess())
    result = official_eval._run_official_eval(spec, report, tmp_path)
    assert result["report_sha256"] == __import__("hashlib").sha256(report.read_bytes()).hexdigest()
    assert result["lifecycle"]["evaluator_pid"] == 1234
    assert result["lifecycle"]["docker_events"]["events"][0]["status"] == "die"


def test_valid_report_remains_correct_when_lifecycle_telemetry_is_unknown(tmp_path) -> None:
    class Public:
        instance_id = "example__repo-1"
        repo = "example/repo"
        base_commit = "a" * 40
        problem_statement = "public issue"
        language = "python"
        license = "MIT"

    class Bundle:
        public = Public()
        def gold_patch(self): return "gold"
        def test_patch(self): return "test"
        def hidden_tests(self): return (("tests/test_bug.py::test_bug",), ())
        def image_name(self): return "image"
        def install_config(self): return {}

    def evaluator(_spec, _report, _workdir):
        return {
            "report": {"items": [{"instance_id": "example__repo-1", "from_fail_to_pass": ["tests/test_bug.py::test_bug"], "failed_from_pass_to_pass": [], "passed_match": True, "error": None}]},
            "exit_code": 0,
            "lifecycle": {"docker_events": {"status": "unknown", "events": []}},
        }

    result = OfficialSWERebenchVerifier(Bundle(), work_root=tmp_path, baseline_valid=True, evaluate_fn=evaluator).evaluate("diff --git a/x b/x\n")
    assert result["verifier_infrastructure_valid"] is True
    assert result["resolved"] is True
    assert result["verifier_outcome"] == "RESOLVED"
    assert result["official_lifecycle_evidence_status"] == "unknown"


def test_valid_report_remains_correct_after_watchdog_cleanup(tmp_path) -> None:
    class Public:
        instance_id = "example__repo-1"
        repo = "example/repo"
        base_commit = "a" * 40
        problem_statement = "public issue"
        language = "python"
        license = "MIT"

    class Bundle:
        public = Public()
        def gold_patch(self): return "gold"
        def test_patch(self): return "test"
        def hidden_tests(self): return (("tests/test_bug.py::test_bug",), ())
        def image_name(self): return "image"
        def install_config(self): return {}

    def evaluator(_spec, _report, _workdir):
        return {
            "report": {"items": [{
                "instance_id": "example__repo-1",
                "from_fail_to_pass": ["tests/test_bug.py::test_bug"],
                "failed_from_pass_to_pass": [],
                "passed_match": True,
                "error": None,
            }]},
            "exit_code": None,
            "failure_kind": "timeout",
            "lifecycle": {"watchdog_expired": True, "docker_events": {"status": "unknown"}},
        }

    result = OfficialSWERebenchVerifier(
        Bundle(), work_root=tmp_path, baseline_valid=True, evaluate_fn=evaluator
    ).evaluate("diff --git a/x b/x\n")
    assert result["verifier_infrastructure_valid"] is True
    assert result["resolved"] is True
    assert result["verifier_outcome"] == "RESOLVED"
    assert result["official_failure_kind"] == "timeout"


def test_lifecycle_status_requires_completed_event_mapping(tmp_path) -> None:
    class Public:
        instance_id = "example__repo-1"; repo = "example/repo"; base_commit = "a" * 40; problem_statement = "issue"; language = "python"; license = "MIT"
    class Bundle:
        public = Public()
        def gold_patch(self): return "gold"
        def test_patch(self): return "test"
        def hidden_tests(self): return (("tests/test_bug.py::test_bug",), ())
        def image_name(self): return "image"
        def install_config(self): return {}
    report = {"items": [{"instance_id": "example__repo-1", "from_fail_to_pass": ["tests/test_bug.py::test_bug"], "failed_from_pass_to_pass": [], "passed_match": True, "error": None}]}
    for lifecycle, expected in (({}, "unknown"), ({"docker_events": None}, "unknown"), ({"docker_events": {"status": "completed"}}, "available")):
        def evaluator(_spec, _report, _workdir, lifecycle=lifecycle):
            return {"report": report, "exit_code": 0, "lifecycle": lifecycle}
        result = OfficialSWERebenchVerifier(Bundle(), work_root=tmp_path, baseline_valid=True, evaluate_fn=evaluator).evaluate("patch")
        assert result["official_lifecycle_evidence_status"] == expected


def test_docker_event_history_covers_full_invocation_and_allowlists_actor(monkeypatch) -> None:
    captured = {}
    def run(args, **kwargs):
        captured["args"] = args
        class Result:
            returncode = 0
            stdout = "\n".join([
                json.dumps({"timeNano": 1, "Action": "create", "id": "c" * 200, "Actor": {"ID": "c" * 200, "Attributes": {"image": "img", "name": "name"}}}),
                json.dumps({"timeNano": 2, "Action": "start", "id": "c" * 200, "Actor": {"ID": "c" * 200, "Attributes": {"image": "img", "name": "name", "exitCode": "0", "secret": "do-not-persist"}}}),
            ])
            stderr = ""
        return Result()
    monkeypatch.setattr(official_eval.subprocess, "run", run)
    result = official_eval._docker_events("img", 1234.5)
    assert result["status"] == "completed"
    assert captured["args"][captured["args"].index("--since") + 1] == "1234.500000"
    event = result["events"][0]
    assert set(event) == {"timestamp", "action", "container_id", "image", "name", "exit_code"}
    assert len(event["container_id"]) <= 128
    assert [item["action"] for item in result["events"]] == ["create", "start"]


def test_initial_probe_does_not_extend_evaluator_watchdog(monkeypatch, tmp_path) -> None:
    evaluator = tmp_path / "official-evaluator"
    (evaluator / "scripts").mkdir(parents=True)
    monkeypatch.setattr(official_eval, "ensure_official_evaluator", lambda: evaluator)
    monkeypatch.setattr(official_eval, "_docker_probe", lambda: (time.sleep(0.05) or {"ps": {"exit_code": 0}}))
    observed = {}
    class Process:
        pid = 4321
        returncode = 0
        def communicate(self, timeout=None):
            observed["timeout"] = timeout
            return "", ""
    monkeypatch.setattr(official_eval.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(official_eval, "_process_snapshot", lambda pid: {"status": "observed", "evaluator_alive": False, "descendants": []})
    monkeypatch.setattr(official_eval, "_docker_events", lambda *args: {"status": "completed", "exit_code": 0, "events": []})
    result = official_eval._run_official_eval(tmp_path / "spec.json", tmp_path / "report.json", tmp_path)
    assert result["failure_kind"] is None
    assert observed["timeout"] > official_eval.OFFICIAL_EVALUATOR_WATCHDOG_SECONDS - 0.05


def test_docker_event_collection_failure_is_unknown_not_no_container(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(official_eval.subprocess, "run", fail)
    result = official_eval._docker_events("img", 1234.5)
    assert result["status"] == "unknown"
    assert result["events"] == []


def test_docker_event_bound_discloses_truncation_and_keeps_bookends(monkeypatch) -> None:
    def run(args, **kwargs):
        class Result:
            returncode = 0
            stdout = "\n".join(
                json.dumps({"timeNano": i, "Action": "create" if i == 0 else "noise" if i < 100 else "destroy", "id": str(i), "Actor": {"ID": str(i), "Attributes": {"image": "img"}}})
                for i in range(101)
            )
            stderr = ""
        return Result()
    monkeypatch.setattr(official_eval.subprocess, "run", run)
    result = official_eval._docker_events("img", 1.0)
    assert result["truncated"] is True
    assert result["event_count"] == 101
    assert result["events"][0]["action"] == "create"
    assert result["events"][-1]["action"] == "destroy"


def test_watchdog_preserves_valid_report_projection_and_hash(monkeypatch, tmp_path) -> None:
    evaluator = tmp_path / "official-evaluator"
    (evaluator / "scripts").mkdir(parents=True)
    spec = tmp_path / "spec.json"
    report = tmp_path / "report.json"
    report_payload = {"items": [{"instance_id": "example__repo-1", "from_fail_to_pass": [], "failed_from_pass_to_pass": [], "passed_match": False, "error": None}]}
    report.write_text(json.dumps(report_payload), encoding="utf-8")
    monkeypatch.setattr(official_eval, "ensure_official_evaluator", lambda: evaluator)
    monkeypatch.setattr(official_eval, "_docker_probe", lambda: {"ps": {"exit_code": 0}})
    monkeypatch.setattr(official_eval, "_docker_events", lambda *args: {"status": "completed", "exit_code": 0, "events": []})
    monkeypatch.setattr(official_eval, "_process_snapshot", lambda pid: {"status": "observed", "evaluator_alive": False, "descendants": []})
    class Process:
        pid = 77
        returncode = -9
        calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("eval", timeout)
            return "", ""
        def kill(self): pass
    monkeypatch.setattr(official_eval.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(official_eval, "_terminate_official_process", lambda process: None)
    result = official_eval._run_official_eval(spec, report, tmp_path)
    assert result["failure_kind"] == "timeout"
    assert result["report"] == report_payload
    assert result["report_sha256"] == __import__("hashlib").sha256(report.read_bytes()).hexdigest()
    assert result["lifecycle"]["watchdog_expired"] is True


def test_watchdog_malformed_report_remains_fail_closed(monkeypatch, tmp_path) -> None:
    evaluator = tmp_path / "official-evaluator"
    (evaluator / "scripts").mkdir(parents=True)
    report = tmp_path / "report.json"
    report.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(official_eval, "ensure_official_evaluator", lambda: evaluator)
    monkeypatch.setattr(official_eval, "_docker_probe", lambda: {"ps": {"exit_code": 0}})
    monkeypatch.setattr(official_eval, "_docker_events", lambda *args: {"status": "completed", "exit_code": 0, "events": []})
    monkeypatch.setattr(official_eval, "_process_snapshot", lambda pid: {"status": "observed", "evaluator_alive": False, "descendants": []})
    class Process:
        pid = 78
        returncode = -9
        calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1: raise subprocess.TimeoutExpired("eval", timeout)
            return "", ""
        def kill(self): pass
    monkeypatch.setattr(official_eval.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(official_eval, "_terminate_official_process", lambda process: None)
    result = official_eval._run_official_eval(tmp_path / "spec.json", report, tmp_path)
    assert result["report"] == {}
    assert result["report_error"]
    assert result["report_sha256"]
