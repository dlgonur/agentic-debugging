from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application.events import SessionEventKind, SourceKind
from agentic_debugger.application.worker_scenarios import ScenarioContext
from agentic_debugger.cancellation import CancellationToken
from agentic_debugger.swerebench.execution import (
    DockerPublicPytestRunner,
    OfficialSWERebenchVerifier,
)
from agentic_debugger.swerebench.preflight import docker_readiness
from agentic_debugger.swerebench.result_rows import durable_session_evidence
from agentic_debugger.swerebench.records import (
    OfficialInstanceBundle,
    PublicInstanceRecord,
    VerifierPrivateRecord,
)
from agentic_debugger.runtime.exceptions import CommandExecutionError


def _event(
    kind: SessionEventKind,
    sequence: int,
    payload: dict,
    *,
    phase: ControllerState | None = None,
    run_id: str | None = "run-1",
) -> dict:
    from agentic_debugger.application.events import SESSION_EVENT_SCHEMA_VERSION

    return {
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "session_id": "sess-repair6-001",
        "task_id": "swr-example",
        "run_id": run_id,
        "sequence": sequence,
        "timestamp_utc": "2026-08-18T00:00:00Z",
        "source_kind": SourceKind.CONFIGURED_MODEL.value,
        "event_kind": kind.value,
        "controller_phase": phase.value if phase is not None else None,
        "payload": payload,
    }


def _write_journal(session: Path, *, logical_calls: int = 1) -> None:
    events = [
        _event(SessionEventKind.SESSION_CREATED, 0, {"spec_fingerprint": "a" * 64}, run_id=None),
        _event(SessionEventKind.SESSION_STARTED, 1, {}),
        _event(
            SessionEventKind.CONTROLLER_TRANSITION,
            2,
            {"source_state": "Reproduce", "target_state": "Understand", "reason": "reproduced"},
            phase=ControllerState.UNDERSTAND,
        ),
    ]
    sequence = 3
    for index in range(logical_calls):
        events.extend(
            [
                _event(
                    SessionEventKind.MODEL_REQUEST_STARTED,
                    sequence,
                    {"request_index": index},
                    phase=ControllerState.UNDERSTAND,
                ),
            ]
        )
        sequence += 1
    events.extend(
        [
            _event(SessionEventKind.TOOL_STARTED, sequence, {"tool_name": "search_code"}, phase=ControllerState.UNDERSTAND),
            _event(SessionEventKind.TOOL_COMPLETED, sequence + 1, {"tool_name": "search_code", "status": "ok"}, phase=ControllerState.UNDERSTAND),
            _event(
                SessionEventKind.CONTROLLER_TRANSITION,
                sequence + 2,
                {"source_state": "Understand", "target_state": "Patch", "reason": "diagnosed"},
                phase=ControllerState.PATCH,
            ),
            _event(SessionEventKind.PATCH_PROPOSED, sequence + 3, {"attempt_index": 0, "patch_sha256": "b" * 64}, phase=ControllerState.PATCH),
            _event(SessionEventKind.PATCH_APPLIED, sequence + 4, {"attempt_index": 0, "changed_files": ["src/module.py"], "syntax_passed": True}, phase=ControllerState.PATCH),
            _event(
                SessionEventKind.CONTROLLER_TRANSITION,
                sequence + 5,
                {"source_state": "Patch", "target_state": "Validate", "reason": "candidate applied"},
                phase=ControllerState.VALIDATE,
            ),
            _event(SessionEventKind.TOOL_STARTED, sequence + 6, {"tool_name": "run_reproduction"}, phase=ControllerState.VALIDATE),
            _event(SessionEventKind.TOOL_COMPLETED, sequence + 7, {"tool_name": "run_reproduction", "status": "ok"}, phase=ControllerState.VALIDATE),
            _event(SessionEventKind.TOOL_STARTED, sequence + 8, {"tool_name": "run_regression_tests"}, phase=ControllerState.VALIDATE),
            _event(SessionEventKind.TOOL_COMPLETED, sequence + 9, {"tool_name": "run_regression_tests", "status": "ok"}, phase=ControllerState.VALIDATE),
        ]
    )
    session.mkdir(parents=True, exist_ok=True)
    (session / "session.events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def _metrics(session: Path, **overrides) -> None:
    value = {
        "model_request_count": 1,
        "model_response_count": 1,
        "retry_count": 0,
        "provider_error_count": 0,
        "provider_error_kinds": [],
        "token_usage": {"total_tokens": 7},
        "termination_reason": "done",
    }
    value.update(overrides)
    (session / "provider.metrics.json").write_text(
        json.dumps(value, sort_keys=True), encoding="utf-8"
    )


def test_projection_counts_logical_calls_separately_from_transport_attempts(tmp_path: Path):
    session = tmp_path / "session"
    _write_journal(session, logical_calls=1)
    _metrics(session)
    evidence = durable_session_evidence(session, {"termination_reason": "done"})
    assert evidence["runtime"]["logical_model_calls"] == 1
    assert evidence["runtime"]["transport_attempts"] == 1

    _metrics(session, model_request_count=3, retry_count=2, provider_error_count=2, provider_error_kinds=["transport_error"])
    retry = durable_session_evidence(session, {"termination_reason": "done"})
    assert retry["runtime"]["logical_model_calls"] == 1
    assert retry["runtime"]["transport_attempts"] == 3
    assert retry["runtime"]["adapter_retry_count"] == 2
    assert retry["provider_invalid"] is False


@pytest.mark.parametrize("termination", ["provider_or_transport_error", "request_timeout"])
def test_terminal_transport_failure_is_provider_invalid(tmp_path: Path, termination: str):
    session = tmp_path / "session"
    _write_journal(session)
    _metrics(session, provider_error_count=1, provider_error_kinds=["transport_error"], termination_reason=termination)
    assert durable_session_evidence(session, {})["provider_invalid"] is True


@pytest.mark.parametrize("termination", ["invalid_model_response", "directive_exhausted"])
def test_model_directive_failure_is_not_provider_invalid_from_error_count(tmp_path: Path, termination: str):
    session = tmp_path / "session"
    _write_journal(session)
    _metrics(
        session,
        provider_error_count=3,
        provider_error_kinds=["invalid_model_response"],
        termination_reason=termination,
    )
    assert durable_session_evidence(session, {})["provider_invalid"] is False


def test_projection_uses_actual_execution_evidence_and_validate_phase(tmp_path: Path):
    session = tmp_path / "session"
    _write_journal(session)
    _metrics(session)
    (session / "execution.evidence.json").write_text(
        json.dumps({"baseline_failure_reproduced": False}), encoding="utf-8"
    )
    evidence = durable_session_evidence(session, {"termination_reason": "done"})
    assert evidence["trajectory"]["baseline_reproduced"] is False
    assert evidence["trajectory"]["understand_reached"] is True
    assert evidence["trajectory"]["source_operations"] == 1
    assert evidence["trajectory"]["validate_sequence"] == [
        "run_reproduction",
        "run_regression_tests",
    ]


def test_docker_launch_statuses_raise_typed_infrastructure_error(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = DockerPublicPytestRunner("example/image:1", root=tmp_path)
    for code in (125, 126, 127):
        monkeypatch.setattr(
            "agentic_debugger.swerebench.execution.subprocess.run",
            lambda *args, code=code, **kwargs: subprocess.CompletedProcess(args[0], code, "", "docker failed"),
        )
        with pytest.raises(CommandExecutionError):
            runner.run(["python", "-m", "pytest", "test.py"], str(workspace), 1, {})

    def missing(*args, **kwargs):
        raise OSError("docker missing")

    monkeypatch.setattr("agentic_debugger.swerebench.execution.subprocess.run", missing)
    with pytest.raises(CommandExecutionError):
        runner.run(["python", "-m", "pytest", "test.py"], str(workspace), 1, {})


def test_docker_readiness_requires_reachable_daemon(monkeypatch):
    monkeypatch.setattr("agentic_debugger.swerebench.preflight.shutil.which", lambda name: "docker.exe")
    monkeypatch.setattr(
        "agentic_debugger.swerebench.preflight.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "daemon unavailable"),
    )
    failed = docker_readiness()
    assert failed["executable_available"] is True
    assert failed["daemon_reachable"] is False

    monkeypatch.setattr(
        "agentic_debugger.swerebench.preflight.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "Server: ok", ""),
    )
    ready = docker_readiness()
    assert ready["daemon_reachable"] is True


def test_configured_provider_metrics_are_durable_on_terminal_failure(tmp_path: Path):
    from agentic_debugger.application.configured_source import _persist_provider_metrics

    mapping = {
        "model_request_count": 3,
        "model_response_count": 0,
        "retry_count": 2,
        "provider_error_count": 3,
        "provider_error_kinds": ["request_timeout"],
        "token_usage": {"total_tokens": None},
        "termination_reason": "request_timeout",
    }
    ctx = ScenarioContext(
        work_dir=tmp_path / "work",
        token=CancellationToken(),
        session_dir=tmp_path / "session",
    )
    _persist_provider_metrics(ctx, [SimpleNamespace(metrics=SimpleNamespace(to_mapping=lambda: mapping))])
    persisted = json.loads((tmp_path / "session" / "provider.metrics.json").read_text())
    assert persisted == mapping
    assert "secret" not in json.dumps(persisted).lower()


def test_configured_provider_metrics_are_durable_on_success(tmp_path: Path):
    from agentic_debugger.application.configured_source import _persist_provider_metrics

    mapping = {
        "model_request_count": 1,
        "model_response_count": 1,
        "retry_count": 0,
        "provider_error_count": 0,
        "provider_error_kinds": [],
        "token_usage": {"total_tokens": 11},
        "termination_reason": None,
    }
    ctx = ScenarioContext(
        work_dir=tmp_path / "work",
        token=CancellationToken(),
        session_dir=tmp_path / "session",
    )
    _persist_provider_metrics(ctx, [SimpleNamespace(metrics=SimpleNamespace(to_mapping=lambda: mapping))])
    assert json.loads((tmp_path / "session" / "provider.metrics.json").read_text()) == mapping


def test_verifier_removes_entire_unique_private_workspace(tmp_path: Path):
    instance_id = "example__repair6"
    public = PublicInstanceRecord(
        instance_id=instance_id,
        repo="example/repo",
        base_commit="a" * 40,
        problem_statement="issue",
        language="python",
        license="MIT",
        created_at="2024-01-01",
        problem_statement_sha256="c" * 64,
    )
    private = VerifierPrivateRecord(
        instance_id=instance_id,
        fail_to_pass=("tests/test_hidden.py::test_bug",),
        pass_to_pass=(),
        test_cmd="pytest",
        image_name="example/image:1",
        python_version="3.11",
        has_gold_patch=True,
        has_test_patch=True,
        gold_patch_sha256="d" * 64,
        test_patch_sha256="e" * 64,
    )
    bundle = OfficialInstanceBundle(
        public=public,
        private=private,
        _gold_patch="gold",
        _test_patch="test",
        _fail_to_pass=private.fail_to_pass,
        _pass_to_pass=(),
        _test_cmd="pytest",
        _install_config={},
        _image_name=private.image_name,
    )

    def evaluate(_spec: Path, _report: Path, workdir: Path):
        (workdir / "logs").mkdir()
        (workdir / "logs" / "private.log").write_text("private", encoding="utf-8")
        return {
            "exit_code": 1,
            "report": {
                "items": [{
                    "instance_id": instance_id,
                    "from_fail_to_pass": [],
                    "failed_from_pass_to_pass": [],
                    "passed_match": False,
                    "error": None,
                }]
            },
        }

    result = OfficialSWERebenchVerifier(
        bundle, work_root=tmp_path, baseline_valid=True, evaluate_fn=evaluate
    ).evaluate("candidate")
    assert result["cleanup"] is True
    assert list(tmp_path.glob("candidate-verification-private-*")) == []
