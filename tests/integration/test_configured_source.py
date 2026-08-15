"""Task 8 backend gates: the production configured command-model source.

Runs real configured command-model sessions through the accepted worker
boundary with the production source name and the durable dummy-command
fixture, proving:

- a valid configured run reaches the real controller/tools/PDB/
  PatchManager/independent verifier and registers into app-owned history
  with safe provenance (``model.configured``: profile id + fingerprint +
  label, never the executable/argv/env);
- malformed protocol, non-zero exit, invalid directives, request timeout,
  and bounded-output violations fail honestly as FAILED/model_error with
  bounded sanitized diagnostics;
- cooperative cancellation interrupts the active command promptly and
  terminates with CANCELLED only after verified cleanup;
- configured command descendants cannot be orphaned (per-request timeout/
  cancel tree kill; job close at worker release);
- secret-looking stderr output is never persisted into history evidence;
- live/replay presentation parity for a successful configured run, and
  replay executes nothing (the recorded journal is the only input).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_debugger.application.configured_source import CONFIGURED_SOURCE_NAME
from agentic_debugger.application.events import (
    SessionEventKind,
    SourceKind,
    validate_session_event_stream,
)
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.journal import JournalReadState, read_session_journal
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.application.process_tree import pid_is_alive
from agentic_debugger.application.session import (
    SessionBudgets,
    SessionSpec,
    SessionStatus,
)
from agentic_debugger.application.sources import ExecutionSourceSpec
from agentic_debugger.application.worker_process import SessionWorkerProcess
from agentic_debugger.demo.catalog import build_reference_patch, scenario_for

TASK_ID = "curated-off-by-one-002"
PDB_POLICY = "pdb-on-uncertainty"
STATIC_POLICY = "static-baseline"
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "command_models"
    / "dummy_command_model.py"
)


def write_profile(
    root: Path,
    profile_id: str,
    mode: str,
    *,
    timeout: float = 60.0,
    extra_argv: tuple[str, ...] = (),
    display_name: str = "Dummy command model",
) -> None:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    argv = [str(FIXTURE), mode, "--state-dir", str(root / f"state-{profile_id}")]
    if extra_argv:
        argv.extend(extra_argv)
    (config_dir / "command-models.json").write_text(
        json.dumps(
            {
                "schema_version": "command-models-v1",
                "profiles": [
                    {
                        "profile_id": profile_id,
                        "display_name": display_name,
                        "executable": sys.executable,
                        "argv": argv,
                        "request_timeout_seconds": timeout,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def write_task_data(root: Path) -> Path:
    """The fixture's task-data file built from the accepted demo catalog."""
    scenario = scenario_for(TASK_ID)
    source_path = (
        Path("agentic_debugger/datasets/curated") / TASK_ID / scenario.reference_repair.target_path
    )
    patch_file = root / "reference.patch"
    patch_file.write_text(
        build_reference_patch(source_path.read_text(encoding="utf-8"), scenario.reference_repair),
        encoding="utf-8",
    )
    data_file = root / "data.json"
    data_file.write_text(
        json.dumps(
            {
                "symbol": scenario.localization.symbol,
                "file": scenario.localization.file_path,
                "hypothesis_id": scenario.hypothesis_id,
                "statement": scenario.root_cause_statement,
                "patch_file": str(patch_file),
                "expressions": list(scenario.runtime_probe.inspect_expressions),
            }
        ),
        encoding="utf-8",
    )
    return data_file


def make_spec(policy: str, profile_id: str = "dummy") -> SessionSpec:
    return SessionSpec(
        task_id=TASK_ID,
        source=ExecutionSourceSpec(
            kind=SourceKind.CONFIGURED_MODEL,
            task_id=TASK_ID,
            policy=policy,
            model_config_ref=profile_id,
        ),
        budgets=SessionBudgets(),
    )


def make_worker(
    store: HistoryStore,
    session_id: str,
    policy: str,
    profile_id: str = "dummy",
    **kwargs,
) -> SessionWorkerProcess:
    kwargs.setdefault("cooperative_grace_seconds", 15.0)
    kwargs.setdefault("ready_timeout_seconds", 90.0)
    return SessionWorkerProcess(
        session_dir=store.session_dir(session_id),
        session_id=session_id,
        spec=make_spec(policy, profile_id),
        run_id=f"run-{session_id}",
        scenario=CONFIGURED_SOURCE_NAME,
        scenario_params={
            "config_root": str(store.root),
            "profile_id": profile_id,
            "policy": policy,
        },
        **kwargs,
    )


def run_to_terminal(worker: SessionWorkerProcess, cancel_after: float | None = None):
    result = worker.start()
    assert result is None, result
    if cancel_after is not None:
        def _cancel() -> None:
            time.sleep(cancel_after)
            worker.cancel()

        threading.Thread(target=_cancel, daemon=True).start()
    return worker.wait()


def journal_of(store: HistoryStore, session_id: str):
    return read_session_journal(store.session_dir(session_id) / "session.events.jsonl")


class TestConfiguredSourceHappyPath:
    def test_valid_configured_run_reaches_verifier_and_history(self, tmp_path):
        data_file = write_task_data(tmp_path)
        write_profile(
            tmp_path, "dummy", "valid",
            extra_argv=("--data", str(data_file)),
        )
        store = HistoryStore(tmp_path)
        session_id = "sess-cfg-valid-0001"
        worker = make_worker(store, session_id, PDB_POLICY)
        result = run_to_terminal(worker)
        assert result.status is SessionStatus.SUCCEEDED
        assert result.termination_reason.value == "done"
        assert result.cleanup_verified is True

        read = journal_of(store, session_id)
        assert read.state is JournalReadState.COMPLETE
        validate_session_event_stream(read.events)
        kinds = [event.event_kind for event in read.events]
        # one shared contiguous journal through the real pipeline
        assert SessionEventKind.MODEL_CONFIGURED in kinds
        assert SessionEventKind.PATCH_APPLIED in kinds
        assert SessionEventKind.VERIFIER_COMPLETED in kinds
        assert SessionEventKind.SOURCE_SNAPSHOT in kinds
        assert kinds[-1] is SessionEventKind.SESSION_COMPLETED
        provenance = next(
            event.payload
            for event in read.events
            if event.event_kind is SessionEventKind.MODEL_CONFIGURED
        )
        assert provenance["profile_id"] == "dummy"
        assert len(provenance["config_fingerprint"]) == 64
        assert provenance["display_name"] == "Dummy command model"
        # provenance never carries the executable/argv/env
        for forbidden in ("executable", "argv", "environment", "cwd"):
            assert forbidden not in provenance
        verifier = next(
            event.payload
            for event in read.events
            if event.event_kind is SessionEventKind.VERIFIER_COMPLETED
        )
        assert verifier["status"] == "COMPLETED"
        assert verifier["outcome"] == "RESOLVED"
        assert verifier["f2p_passed"] == 1 and verifier["p2p_passed"] == 2

        # app-owned history registers the configured session as complete
        store.register(store.session_dir(session_id))
        entry = next(
            entry for entry in store.list_sessions() if entry.session_id == session_id
        )
        assert entry.classification.is_success
        assert entry.source_kind is SourceKind.CONFIGURED_MODEL
        assert entry.status is SessionStatus.SUCCEEDED

        # live/replay presentation parity
        live_view = initial_session_view(
            PresentationIdentity(
                task_id=TASK_ID, source_kind=SourceKind.CONFIGURED_MODEL, session_id=session_id
            )
        )
        for event in read.events:
            live_view = reduce_event(live_view, event)
        assert live_view.status is SessionStatus.SUCCEEDED
        assert live_view.cleanup_verified is True
        assert live_view.model_provenance is not None
        assert live_view.model_provenance.profile_id == "dummy"
        assert live_view.model_provenance.display_name == "Dummy command model"
        assert live_view.verifier_summary is not None
        assert live_view.verifier_summary.outcome.value == "RESOLVED"
        worker.close()

    def test_static_baseline_policy_run(self, tmp_path):
        data_file = write_task_data(tmp_path)
        write_profile(tmp_path, "dummy", "valid", extra_argv=("--data", str(data_file)))
        store = HistoryStore(tmp_path)
        session_id = "sess-cfg-static-0001"
        worker = make_worker(store, session_id, STATIC_POLICY)
        result = run_to_terminal(worker)
        assert result.status is SessionStatus.SUCCEEDED
        kinds = [e.event_kind for e in journal_of(store, session_id).events]
        # static baseline never opens a debugger session
        assert SessionEventKind.DEBUGGER_STARTED not in kinds
        assert SessionEventKind.VERIFIER_COMPLETED in kinds
        worker.close()


class TestConfiguredSourceFailures:
    @pytest.mark.parametrize(
        ("mode", "expected_transport"),
        [
            ("malformed", "provider_or_transport_error"),
            ("fail", "provider_or_transport_error"),
            ("invalid_directive", "invalid_model_response"),
            ("illegal_action", "invalid_model_response"),
            ("flood_stdout", "provider_or_transport_error"),
        ],
    )
    def test_honest_model_error_failures(self, tmp_path, mode, expected_transport):
        write_profile(tmp_path, "dummy", mode, timeout=10.0)
        store = HistoryStore(tmp_path)
        session_id = f"sess-cfg-fail-{mode}-0001"
        worker = make_worker(store, session_id, PDB_POLICY)
        result = run_to_terminal(worker)
        assert result.status is SessionStatus.FAILED
        assert result.termination_reason.value == "model_error"
        assert result.cleanup_verified is True
        diagnostics = " ".join(result.diagnostics)
        assert "model transport: " in diagnostics
        assert expected_transport in diagnostics
        kinds = [e.event_kind for e in journal_of(store, session_id).events]
        assert kinds[-1] is SessionEventKind.SESSION_FAILED
        worker.close()

    def test_request_timeout_is_not_success_and_not_cancellation(self, tmp_path):
        write_profile(
            tmp_path, "dummy", "slow", timeout=3.0, extra_argv=("--delay", "60")
        )
        store = HistoryStore(tmp_path)
        session_id = "sess-cfg-timeout-0001"
        worker = make_worker(store, session_id, PDB_POLICY)
        result = run_to_terminal(worker)
        assert result.status is SessionStatus.FAILED
        assert result.termination_reason.value == "model_error"
        assert "request_timeout" in " ".join(result.diagnostics)
        # timeout is never converted into successful empty output
        kinds = [e.event_kind for e in journal_of(store, session_id).events]
        assert kinds[-1] is SessionEventKind.SESSION_FAILED
        worker.close()


class TestConfiguredSourceCancellation:
    def test_cancel_interrupts_active_command_and_cleans(self, tmp_path):
        write_profile(
            tmp_path, "dummy", "slow", timeout=120.0, extra_argv=("--delay", "300")
        )
        store = HistoryStore(tmp_path)
        session_id = "sess-cfg-cancel-0001"
        worker = make_worker(store, session_id, PDB_POLICY)
        started = time.monotonic()
        result = run_to_terminal(worker, cancel_after=2.0)
        assert time.monotonic() - started < 60.0, "cancel did not interrupt promptly"
        assert result.status is SessionStatus.CANCELLED
        assert result.termination_reason.value == "cancelled"
        assert result.cleanup_verified is True
        kinds = [e.event_kind for e in journal_of(store, session_id).events]
        assert "session.cancel_requested" in [k.value for k in kinds]
        assert kinds[-1] is SessionEventKind.SESSION_CANCELLED
        assert worker.pid is None or not pid_is_alive(worker.pid)
        worker.close()


class TestConfiguredSourceProcessTree:
    def test_timeout_leaves_no_descendant_or_grandchild(self, tmp_path):
        child_pid_file = tmp_path / "child.pid"
        grandchild_pid_file = tmp_path / "grandchild.pid"
        write_profile(
            tmp_path,
            "dummy",
            "spawn_child",
            timeout=3.0,
            extra_argv=(
                "--child-pid-file",
                str(child_pid_file),
                "--grandchild-pid-file",
                str(grandchild_pid_file),
                "--delay",
                "60",
            ),
        )
        store = HistoryStore(tmp_path)
        session_id = "sess-cfg-tree-0001"
        worker = make_worker(store, session_id, PDB_POLICY)
        result = run_to_terminal(worker)
        assert result.status is SessionStatus.FAILED
        deadline = time.monotonic() + 15.0
        child_pid = int(child_pid_file.read_text()) if child_pid_file.is_file() else None
        grandchild_pid = (
            int(grandchild_pid_file.read_text()) if grandchild_pid_file.is_file() else None
        )
        assert child_pid is not None and grandchild_pid is not None
        while time.monotonic() < deadline and (
            pid_is_alive(child_pid) or pid_is_alive(grandchild_pid)
        ):
            time.sleep(0.1)
        assert not pid_is_alive(child_pid), "descendant survived the request timeout"
        assert not pid_is_alive(grandchild_pid), "grandchild survived the request timeout"
        worker.close()

    def test_cancel_leaves_no_descendant(self, tmp_path):
        child_pid_file = tmp_path / "child.pid"
        write_profile(
            tmp_path,
            "dummy",
            "spawn_child",
            timeout=120.0,
            extra_argv=(
                "--child-pid-file",
                str(child_pid_file),
                "--delay",
                "300",
            ),
        )
        store = HistoryStore(tmp_path)
        session_id = "sess-cfg-tree-cancel-0001"
        worker = make_worker(store, session_id, PDB_POLICY)
        result = run_to_terminal(worker, cancel_after=2.0)
        assert result.status is SessionStatus.CANCELLED
        deadline = time.monotonic() + 10.0
        child_pid = int(child_pid_file.read_text()) if child_pid_file.is_file() else None
        assert child_pid is not None
        while time.monotonic() < deadline and pid_is_alive(child_pid):
            time.sleep(0.1)
        assert not pid_is_alive(child_pid), "descendant survived cancellation"
        worker.close()

    def test_completed_session_job_close_leaves_no_descendant(self, tmp_path):
        data_file = write_task_data(tmp_path)
        child_pid_file = tmp_path / "child.pid"
        write_profile(
            tmp_path,
            "dummy",
            "spawn_child",
            extra_argv=(
                "--data",
                str(data_file),
                "--child-pid-file",
                str(child_pid_file),
            ),
        )
        store = HistoryStore(tmp_path)
        session_id = "sess-cfg-tree-done-0001"
        worker = make_worker(store, session_id, PDB_POLICY)
        result = run_to_terminal(worker)
        assert result.status is SessionStatus.SUCCEEDED
        child_pid = int(child_pid_file.read_text()) if child_pid_file.is_file() else None
        assert child_pid is not None
        # the descendant stays inside the accepted Windows job until the
        # supervisor releases it; after close nothing may survive
        worker.close()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and pid_is_alive(child_pid):
            time.sleep(0.1)
        assert not pid_is_alive(child_pid), "descendant survived worker close"


class TestConfiguredSourceEvidenceSafety:
    def test_secret_on_stderr_is_never_persisted(self, tmp_path):
        data_file = write_task_data(tmp_path)
        secret = "sk-live-secret-7f3a9c"
        write_profile(
            tmp_path,
            "dummy",
            "secret_on_stderr",
            extra_argv=("--data", str(data_file)),
        )
        store = HistoryStore(tmp_path)
        session_id = "sess-cfg-secret-0001"
        worker = make_worker(store, session_id, PDB_POLICY)
        result = run_to_terminal(worker)
        worker.close()
        # the session still completes; the secret must never appear in any
        # persisted app-owned evidence
        assert secret not in json.dumps(result.to_mapping())
        for artifact in store.session_dir(session_id).iterdir():
            if artifact.is_file() and artifact.name.endswith((".json", ".jsonl")):
                assert secret not in artifact.read_text(encoding="utf-8", errors="replace")
        assert secret not in json.dumps(journal_of(store, session_id).events[-1].to_mapping())

    def test_secret_in_malformed_output_is_never_persisted(self, tmp_path):
        secret = "sk-live-malformed-9c41f2"
        write_profile(tmp_path, "dummy", "malformed_secret", timeout=10.0)
        store = HistoryStore(tmp_path)
        session_id = "sess-cfg-secret-malformed-0001"
        worker = make_worker(store, session_id, PDB_POLICY)
        result = run_to_terminal(worker)
        worker.close()
        assert result.status is SessionStatus.FAILED
        assert secret not in json.dumps(result.to_mapping())
        for artifact in store.session_dir(session_id).iterdir():
            if artifact.is_file() and artifact.name.endswith((".json", ".jsonl")):
                assert secret not in artifact.read_text(encoding="utf-8", errors="replace")

    def test_replay_of_configured_session_executes_nothing(self, tmp_path):
        data_file = write_task_data(tmp_path)
        state_dir = tmp_path / "state-dummy"
        write_profile(tmp_path, "dummy", "valid", extra_argv=("--data", str(data_file)))
        store = HistoryStore(tmp_path)
        session_id = "sess-cfg-replay-0001"
        worker = make_worker(store, session_id, PDB_POLICY)
        result = run_to_terminal(worker)
        assert result.status is SessionStatus.SUCCEEDED
        store.register(store.session_dir(session_id))
        phase_before = (
            (state_dir / "phase.json").read_text(encoding="utf-8")
            if (state_dir / "phase.json").is_file()
            else None
        )
        reopened = store.reopen(session_id)
        replayed_view = initial_session_view(
            PresentationIdentity(
                task_id=TASK_ID,
                source_kind=SourceKind.CONFIGURED_MODEL,
                session_id=session_id,
            )
        )
        while True:
            event = reopened.replay.next_event()
            if event is None:
                break
            replayed_view = reduce_event(replayed_view, event)
        assert replayed_view.status is SessionStatus.SUCCEEDED
        assert replayed_view.model_provenance is not None
        # the fixture's sidecar state is untouched: no command was launched
        phase_after = (
            (state_dir / "phase.json").read_text(encoding="utf-8")
            if (state_dir / "phase.json").is_file()
            else None
        )
        assert phase_after == phase_before
        worker.close()
