"""Focused provider-free tests for the Level-32 application bridge."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agentic_debugger.application.events import (
    SessionEventKind,
    SessionStatus,
    SessionTerminationReason,
    validate_session_event_stream,
)
from agentic_debugger.application.journal import JournalReadState, read_session_journal
from agentic_debugger.application.level32 import (
    Level32ModelProfile,
    Level32OperatorWorker,
    build_level32_spec,
    level32_model_profiles,
    next_level32_treatment,
)
from agentic_debugger.application.presentation import (
    initial_session_view,
    presentation_identity,
    reduce_event,
)


def test_roster_is_canonical_eligible_and_provider_free(monkeypatch):
    import http.client

    def fail_if_called(*args, **kwargs):
        raise AssertionError("opening the model picker must not call Ollama")

    monkeypatch.setattr(http.client, "HTTPConnection", fail_if_called)
    profiles = level32_model_profiles()
    aliases = {profile.alias for profile in profiles}
    assert "glm-5.2:cloud" in aliases
    assert "gpt-oss:120b-cloud" in aliases
    assert "kimi-k2.7-code:cloud" not in aliases
    assert "kimi-k3:cloud" not in aliases
    assert all(profile.readiness == "live_verified" for profile in profiles)


def test_next_revision_uses_existing_operator_directory_convention(tmp_path):
    root = tmp_path / "repo"
    experiment_root = root / "experiments" / "pdb_capability_ladder"
    experiment_root.mkdir(parents=True)
    (experiment_root / "level32-cookiecutter-967-glm-5.2-cloud-v1").mkdir()
    (experiment_root / "level32-cookiecutter-967-glm-5.2-cloud-v3-tool-accepted").mkdir()
    revision, treatment_id, output = next_level32_treatment(root, "glm-5.2:cloud")
    assert revision == 4
    assert treatment_id.endswith("glm-5.2-cloud-v4-workspace-derived-official-git-diff-v1")
    assert not output.exists()


class _FakeOperatorProcess:
    pid = 4242
    returncode = 1

    def __init__(self, argv, result):
        self.argv = list(argv)
        self.result = result

    def poll(self):
        return self.returncode

    def communicate(self):
        output = Path(self.argv[self.argv.index("--output-dir") + 1])
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.json").write_text(
            json.dumps(
                self.result
            ),
            encoding="utf-8",
        )
        return "", "semantic rejection"

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class _ProgressOperatorProcess(_FakeOperatorProcess):
    def __init__(self, argv, result):
        super().__init__(argv, result)
        self._polls = 0

    def poll(self):
        self._polls += 1
        if self._polls == 1:
            progress = Path(self.argv[self.argv.index("--progress-file") + 1])
            progress.write_text(
                "\n".join(
                    json.dumps({"schema_version": "operator-progress-v1", "stage": stage})
                    for stage in (
                        "preflight",
                        "model_running",
                        "debugger",
                        "official_verification_preparing",
                        "official_evaluator_started",
                        "official_evaluator_completed",
                        "finalizing",
                    )
                ) + "\n",
                encoding="utf-8",
            )
            return None
        return self.returncode


class _FailedOperatorProcess:
    pid = 4243
    returncode = 1

    def __init__(self, argv, *, result_text=None, cleanup_evidence=False):
        self.argv = list(argv)
        self.result_text = result_text
        self.cleanup_evidence = cleanup_evidence

    def poll(self):
        return self.returncode

    def communicate(self):
        output = Path(self.argv[self.argv.index("--output-dir") + 1])
        output.mkdir(parents=True, exist_ok=True)
        if self.cleanup_evidence:
            (output / "live-results.json").write_text(
                json.dumps(
                    {
                        "reporting": {"completed": True, "cleanup": "cleaned"},
                        "measurements": {"successful_pdb_observation_count": 3},
                        "events_jsonl": json.dumps(
                            {
                                "payload": {
                                    "observation": {
                                        "payload": {
                                            "proof": {
                                                "exact_reproduction": True,
                                                "production_file": "cookiecutter/config.py",
                                                "breakpoint_line": 58,
                                            }
                                        }
                                    }
                                }
                            }
                        ),
                    }
                ),
                encoding="utf-8",
            )
        if self.result_text is not None:
            (output / "result.json").write_text(self.result_text, encoding="utf-8")
        return "operator stdout", "operator stderr"

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class _CancellableOperatorProcess(_FailedOperatorProcess):
    returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15


def test_operator_progress_observer_is_structured_and_not_waiting_model(tmp_path):
    profile = Level32ModelProfile("glm-5.2:cloud", "glm-5.2", "live_verified", "a" * 64)

    def factory(argv, **kwargs):
        return _ProgressOperatorProcess(
            argv,
            {
                "accepted": False,
                "classification": "official_test_execution_unproven",
                "official_verifier": {},
                "cleanup": {
                    "temporary_source_removed": True,
                    "private_official_material_removed": True,
                },
            },
        )

    worker = Level32OperatorWorker(
        session_dir=tmp_path / "runs" / "progress",
        session_id="sess-level32-progress",
        run_id="run-sess-level32-progress",
        repository_root=tmp_path,
        model=profile,
        revision=1,
        treatment_id="level32-treatment-v1",
        output_dir=tmp_path / "experiments" / "progress",
        spec=build_level32_spec(profile.alias),
        process_factory=factory,
    )
    worker.start()
    worker.wait()
    journal = read_session_journal(worker.session_dir / "session.events.jsonl")
    progress = [
        event.payload["stage"]
        for event in journal.events
        if event.event_kind is SessionEventKind.OPERATOR_PROGRESS
    ]
    assert progress == [
        "starting",
        "preflight",
        "model_running",
        "debugger",
        "official_verification_preparing",
        "official_evaluator_started",
        "official_evaluator_completed",
        "finalizing",
        "cleanup",
        "completed",
    ]
    assert "official_evaluator_completed" in progress
    assert all(
        not (
            event.event_kind is SessionEventKind.SESSION_STATUS_CHANGED
            and event.payload["phase"] == "waiting_model"
        )
        for event in journal.events
    )
    validate_session_event_stream(journal.events)


def test_real_popen_finalizes_without_waiting_for_inherited_descendant_output(tmp_path):
    """A direct operator exit is terminal even when a child retains stdout."""

    profile = Level32ModelProfile("glm-5.2:cloud", "glm-5.2", "live_verified", "a" * 64)
    result_text = json.dumps(
        {
            "accepted": False,
            "classification": "official_test_execution_unproven",
            "official_verifier": {},
            "cleanup": {
                "temporary_source_removed": True,
                "private_official_material_removed": True,
            },
        }
    )

    def factory(argv, **kwargs):
        output = Path(argv[argv.index("--output-dir") + 1])
        child_code = "import time; time.sleep(2)"
        parent_code = (
            "import pathlib, subprocess, sys; "
            f"output=pathlib.Path({str(output)!r}); output.mkdir(parents=True, exist_ok=True); "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            f"(output / 'result.json').write_text({result_text!r}, encoding='utf-8')"
        )
        return subprocess.Popen(
            [sys.executable, "-c", parent_code],
            cwd=kwargs["cwd"],
            stdin=kwargs["stdin"],
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
            shell=False,
            close_fds=False,
        )

    worker = Level32OperatorWorker(
        session_dir=tmp_path / "runs" / "real-popen",
        session_id="sess-level32-real-popen",
        run_id="run-level32-real-popen",
        repository_root=tmp_path,
        model=profile,
        revision=1,
        treatment_id="level32-treatment-real-popen",
        output_dir=tmp_path / "experiments" / "real-popen",
        spec=build_level32_spec(profile.alias),
        process_factory=factory,
    )
    worker.start()
    outcome = {}
    waiter = threading.Thread(target=lambda: outcome.setdefault("result", worker.wait()))
    waiter.start()
    waiter.join(timeout=0.8)
    assert not waiter.is_alive(), "finalization waited on a descendant output handle"
    assert outcome["result"].termination_reason is SessionTerminationReason.UNRESOLVED


def test_one_start_passes_exact_alias_and_revision_and_preserves_classification(tmp_path):
    calls = []
    profile = Level32ModelProfile("glm-5.2:cloud", "glm-5.2", "live_verified", "a" * 64)

    def factory(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return _FakeOperatorProcess(
            argv,
            {
                "accepted": False,
                "classification": "official_rejection_semantic",
                "official_verifier": {},
                "cleanup": {
                    "temporary_source_removed": True,
                    "private_official_material_removed": True,
                },
            },
        )

    worker = Level32OperatorWorker(
        session_dir=tmp_path / "runs" / "sess-level32",
        session_id="sess-level32",
        run_id="run-sess-level32",
        repository_root=tmp_path,
        model=profile,
        revision=7,
        treatment_id="level32-treatment-v7",
        output_dir=tmp_path / "experiments" / "level32-v7",
        spec=build_level32_spec(profile.alias),
        process_factory=factory,
    )
    assert worker.start() is None
    result = worker.wait()
    with pytest.raises(RuntimeError, match="already started"):
        worker.start()

    assert len(calls) == 1
    argv = calls[0][0]
    assert argv[argv.index("--model") + 1] == "glm-5.2:cloud"
    assert argv[argv.index("--treatment-revision") + 1] == "7"
    assert "--live" in argv and "--confirm-live-model-access" in argv
    assert argv[0] == sys.executable
    assert Path(argv[1]).as_posix().endswith("scripts/run_cookiecutter_967_pdb_proof.py")
    assert calls[0][1]["cwd"] == str(tmp_path.resolve())
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["stdout"] is not subprocess.PIPE
    assert calls[0][1]["stderr"] is not subprocess.PIPE
    assert result.termination_reason.value == "unresolved"
    journal = read_session_journal(worker.session_dir / "session.events.jsonl")
    assert journal.state is JournalReadState.COMPLETE
    validate_session_event_stream(journal.events)
    verifier = next(event for event in journal.events if event.event_kind is SessionEventKind.VERIFIER_COMPLETED)
    assert verifier.payload["classification"] == "official_rejection_semantic"


def _run_fake_result(tmp_path, *, accepted, classification, official_verifier):
    profile = Level32ModelProfile("glm-5.2:cloud", "glm-5.2", "live_verified", "a" * 64)
    result = {
        "accepted": accepted,
        "classification": classification,
        "official_verifier": official_verifier,
        "cleanup": {
            "temporary_source_removed": True,
            "private_official_material_removed": True,
        },
    }

    def factory(argv, **kwargs):
        return _FakeOperatorProcess(argv, result)

    worker = Level32OperatorWorker(
        session_dir=tmp_path / "runs" / classification,
        session_id="sess-level32-" + classification,
        run_id="run-level32-" + classification,
        repository_root=tmp_path,
        model=profile,
        revision=7,
        treatment_id="level32-treatment-v7-" + classification,
        output_dir=tmp_path / "experiments" / classification,
        spec=build_level32_spec(profile.alias),
        process_factory=factory,
    )
    worker.start()
    worker.wait()
    journal = read_session_journal(worker.session_dir / "session.events.jsonl")
    event = next(item for item in journal.events if item.event_kind is SessionEventKind.VERIFIER_COMPLETED)
    view = reduce_event(
        initial_session_view(presentation_identity(build_level32_spec(profile.alias))),
        event,
    )
    assert view.verifier_summary is not None
    return event, view.verifier_summary


def test_authoritative_resolved_result_projects_safe_aggregates_and_execution_flag(tmp_path):
    event, summary = _run_fake_result(
        tmp_path,
        accepted=True,
        classification="authoritative_resolved",
        official_verifier={
            "official_test_execution_proven": True,
            "fail_to_pass_total": 5,
            "fail_to_pass_passed": 5,
            "pass_to_pass_total": 9,
            "pass_to_pass_failed": 0,
        },
    )
    assert event.payload["official_test_execution_proven"] is True
    assert (summary.f2p_passed, summary.f2p_total) == (5, 5)
    assert (summary.p2p_passed, summary.p2p_total) == (9, 9)
    assert summary.classification == "authoritative_resolved"


def test_semantic_rejection_with_proven_execution_projects_partial_aggregates(tmp_path):
    event, summary = _run_fake_result(
        tmp_path,
        accepted=False,
        classification="official_rejection_semantic",
        official_verifier={
            "official_test_execution_proven": True,
            "fail_to_pass_total": 5,
            "fail_to_pass_passed": 4,
            "pass_to_pass_total": 9,
            "pass_to_pass_failed": 0,
        },
    )
    assert event.payload["official_test_execution_proven"] is True
    assert (summary.f2p_passed, summary.f2p_total) == (4, 5)
    assert (summary.p2p_passed, summary.p2p_total) == (9, 9)
    assert summary.classification == "official_rejection_semantic"


def test_pre_test_failure_keeps_execution_false_and_counts_unrecorded(tmp_path):
    event, summary = _run_fake_result(
        tmp_path,
        accepted=False,
        classification="official_test_execution_unproven",
        official_verifier={
            "official_test_execution_proven": False,
            "fail_to_pass_total": 5,
            "fail_to_pass_passed": 0,
            "pass_to_pass_total": 9,
            "pass_to_pass_failed": 9,
        },
    )
    assert event.payload["official_test_execution_proven"] is False
    assert (summary.f2p_passed, summary.f2p_total) == (None, None)
    assert (summary.p2p_passed, summary.p2p_total) == (None, None)
    assert summary.classification == "official_test_execution_unproven"


def _failed_worker(tmp_path, factory, name="failed"):
    profile = Level32ModelProfile("glm-5.2:cloud", "glm-5.2", "live_verified", "a" * 64)
    worker = Level32OperatorWorker(
        session_dir=tmp_path / "runs" / name,
        session_id="sess-level32-" + name,
        run_id="run-level32-" + name,
        repository_root=tmp_path,
        model=profile,
        revision=7,
        treatment_id="level32-treatment-v7-" + name,
        output_dir=tmp_path / "experiments" / name,
        spec=build_level32_spec(profile.alias),
        process_factory=factory,
    )
    worker.start()
    return worker


def test_subprocess_failure_retains_safe_exit_diagnostic_and_command_evidence(tmp_path):
    worker = _failed_worker(
        tmp_path,
        lambda argv, **kwargs: _FailedOperatorProcess(argv),
        "missing-result",
    )
    result = worker.wait()
    journal = read_session_journal(worker.session_dir / "session.events.jsonl")
    diagnosis = next(
        event for event in journal.events if event.event_kind is SessionEventKind.DIAGNOSIS_RECORDED
    )
    assert result.termination_reason is SessionTerminationReason.SUBPROCESS_ERROR
    assert result.cleanup_verified is False
    assert "exit 1" in diagnosis.payload["text"]
    assert "operator stderr" in diagnosis.payload["text"]
    assert json.loads((worker.session_dir / "operator.process.json").read_text())["exit_code"] == 1
    assert json.loads((worker.session_dir / "operator.command.json").read_text())["shell"] is False
    assert (worker.session_dir / "operator.stderr.txt").read_text() == "operator stderr"


def test_malformed_result_is_distinguished_from_missing_result(tmp_path):
    worker = _failed_worker(
        tmp_path,
        lambda argv, **kwargs: _FailedOperatorProcess(argv, result_text="{not-json"),
        "malformed-result",
    )
    result = worker.wait()
    journal = read_session_journal(worker.session_dir / "session.events.jsonl")
    diagnosis = next(
        event for event in journal.events if event.event_kind is SessionEventKind.DIAGNOSIS_RECORDED
    )
    assert result.termination_reason is SessionTerminationReason.SUBPROCESS_ERROR
    assert "result parse failed" in diagnosis.payload["text"]
    assert "JSONDecodeError" in diagnosis.payload["text"]


def test_cleanup_projection_uses_structured_live_case_evidence(tmp_path):
    worker = _failed_worker(
        tmp_path,
        lambda argv, **kwargs: _FailedOperatorProcess(argv, cleanup_evidence=True),
        "cleanup-projection",
    )
    result = worker.wait()
    journal = read_session_journal(worker.session_dir / "session.events.jsonl")
    cleanup = next(
        event for event in journal.events if event.event_kind is SessionEventKind.CLEANUP_COMPLETED
    )
    assert result.cleanup_verified is True
    assert cleanup.payload["verified"] is True
    assert any(event.event_kind is SessionEventKind.DEBUGGER_STARTED for event in journal.events)


def test_structured_operator_failure_projects_pdb_proof_but_not_verifier(tmp_path):
    structured = json.dumps(
        {
            "accepted": False,
            "classification": "incomplete_provider_model_transport_failure",
            "official_verifier": None,
            "pdb_proof": {
                "observed": True,
                "successful_observation_count": 3,
                "script": "cookiecutter/config.py",
                "breakpoint_line": 54,
            },
            "operator_failure": {"kind": "candidate_unavailable", "message": "no active patch"},
            "cleanup": {
                "temporary_source_removed": True,
                "private_official_material_removed": True,
            },
        }
    )
    worker = _failed_worker(
        tmp_path,
        lambda argv, **kwargs: _FailedOperatorProcess(argv, result_text=structured),
        "structured-failure",
    )
    result = worker.wait()
    journal = read_session_journal(worker.session_dir / "session.events.jsonl")
    kinds = [event.event_kind for event in journal.events]
    assert result.termination_reason is SessionTerminationReason.SUBPROCESS_ERROR
    assert result.cleanup_verified is True
    assert SessionEventKind.DEBUGGER_STARTED in kinds
    assert SessionEventKind.VERIFIER_COMPLETED not in kinds


def test_cancellation_remains_cancelled_and_does_not_project_verifier(tmp_path):
    worker = _failed_worker(
        tmp_path,
        lambda argv, **kwargs: _CancellableOperatorProcess(argv, cleanup_evidence=True),
        "cancelled",
    )
    worker.cancel()
    result = worker.wait()
    journal = read_session_journal(worker.session_dir / "session.events.jsonl")
    assert result.status is SessionStatus.CANCELLED
    assert result.termination_reason is SessionTerminationReason.CANCELLED
    assert not any(event.event_kind is SessionEventKind.VERIFIER_COMPLETED for event in journal.events)
