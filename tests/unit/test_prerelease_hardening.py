"""Pre-release hardening regression coverage (audit PRE-RELEASE-HARDENING-01).

Each test protects one repaired defect:

- ``active_candidate_attempt`` session-ledger provenance (Apply To Project
  must use the authoritative active candidate, never a later rejected body);
- Local Project command semantics: Windows quote stripping, bounded
  timeouts, UTF-8 output decoding;
- supervisor post-mortem removal of the isolated worktree (crash without a
  terminal must not leak the temp worktree nor a stale ``git worktree``
  registration in the owner repository);
- Apply gate UTF-8 patch bytes (non-ASCII patches must pass through the
  locale-independent path);
- unified-diff parser tolerance for legal in-source Unicode line characters
  (form feed) that ``str.splitlines`` wrongly treats as line boundaries;
- revert rollback restoring the PRE-REVERT bytes on partial failure;
- TaskWorkspace cleanup surviving read-only files (Windows temp leak);
- Local Project sidebar identity flowing through the durable reducer;
- ``model.configured`` durable provenance for Local Project sessions;
- professor trace reproduction truth derived from observations.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agentic_debugger.application.presentation import (
    DiagnosisView,
    PatchAttemptView,
    PatchStage,
    PresentationIdentity,
    SessionViewState,
    active_candidate_attempt,
    initial_session_view,
)
from agentic_debugger.application import events as app_events
from agentic_debugger.runtime.patcher import PatchManager, PatchValidationError
from agentic_debugger.runtime.workspace import TaskWorkspace


# ---------------------------------------------------------------------------
# Session-ledger active-candidate provenance
# ---------------------------------------------------------------------------


def _attempt(index: int, stage: PatchStage, text: str | None = None) -> PatchAttemptView:
    return PatchAttemptView(attempt_index=index, stage=stage, patch_text=text)


def test_active_candidate_survives_later_failed_attempt():
    """Applied A + proposed/rejected B -> the active candidate stays A."""
    state = SessionViewState(
        patch_attempts=(
            _attempt(0, PatchStage.VERIFIED, "--- a/x\n+++ b/x\n"),
            _attempt(1, PatchStage.REJECTED, "--- a/y\n+++ b/y\n"),
        )
    )
    active = active_candidate_attempt(state)
    assert active is not None and active.attempt_index == 0


def test_active_candidate_cleared_by_revert_and_replaced_by_later_apply():
    state = SessionViewState(
        patch_attempts=(
            _attempt(0, PatchStage.APPLIED, "A"),
            _attempt(0, PatchStage.REVERTED),
            _attempt(1, PatchStage.APPLIED, "B"),
        )
    )
    active = active_candidate_attempt(state)
    assert active is not None and active.attempt_index == 1 and active.patch_text == "B"


def test_active_candidate_none_when_only_rejected_or_all_reverted():
    only_rejected = SessionViewState(patch_attempts=(_attempt(0, PatchStage.REJECTED, "bad"),))
    assert active_candidate_attempt(only_rejected) is None
    reverted = SessionViewState(
        patch_attempts=(
            _attempt(0, PatchStage.APPLIED, "A"),
            _attempt(0, PatchStage.REVERTED),
        )
    )
    assert active_candidate_attempt(reverted) is None
    assert active_candidate_attempt(SessionViewState()) is None


# ---------------------------------------------------------------------------
# Local Project command semantics
# ---------------------------------------------------------------------------


def test_split_command_strips_windows_quotes():
    from agentic_debugger.application.local_project_source import _split_command

    argv = _split_command('python "my script.py" --flag "value with spaces"')
    assert argv == ["python", "my script.py", "--flag", "value with spaces"]
    # Unterminated quotes fail closed at split time (honest parse failure)
    with pytest.raises(ValueError):
        _split_command("python 'x")
    assert _split_command('echo ""') == ["echo"]


def test_run_command_bounded_timeout_and_utf8(tmp_path):
    from agentic_debugger.application.local_project_source import _run_command_bounded

    code = "import sys; sys.stdout.write('caf\\u00e9 ok\\n'); sys.stderr.write('err\\u00e9\\n')"
    exit_code, out, err, _ = _run_command_bounded(
        f'python -c "{code}"', tmp_path, timeout=10.0
    )
    assert exit_code == 0
    assert "café ok" in out
    assert "erré" in err

    start = time.monotonic()
    exit_code, out, err, _ = _run_command_bounded(
        'python -c "import time; time.sleep(30)"', tmp_path, timeout=1.0
    )
    assert exit_code == 124
    assert "timed out" in err
    assert time.monotonic() - start < 15.0


# ---------------------------------------------------------------------------
# Supervisor post-mortem isolated-worktree cleanup
# ---------------------------------------------------------------------------


def _git_repo_with_file(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir()
    (repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=str(repo), check=True)
    subprocess.run(["git", "add", "--all"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "init"],
        cwd=str(repo), check=True,
    )
    return repo


def test_worker_crash_post_mortem_cleans_isolated_worktree(tmp_path):
    """A hard-killed Local Project worker must not leak the worktree nor a
    stale ``git worktree`` registration in the owner repository."""
    from agentic_debugger.application.local_project import (
        cleanup_parent_tmpdir,
        create_isolated_worktree,
        validate_local_project,
    )
    from agentic_debugger.application.session import SessionBudgets, SessionSpec
    from agentic_debugger.application.sources import ExecutionSourceSpec
    from agentic_debugger.application.worker_process import SessionWorkerProcess
    from agentic_debugger.application.events import SourceKind

    repo = _git_repo_with_file(tmp_path, "crash_repo")
    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    wt = create_isolated_worktree(validated.repo_root, validated.head_commit)
    spec = SessionSpec(
        task_id="local-project-debug",
        source=ExecutionSourceSpec(
            kind=SourceKind.LOCAL_PROJECT,
            task_id="local-project-debug",
            policy=None,
            model_config_ref="unused",
        ),
        budgets=SessionBudgets(),
    )
    session_root = tmp_path / "hist_crash"
    session_root.mkdir()
    worker = SessionWorkerProcess(
        session_dir=session_root / "sess-crash-postmortem",
        session_id="sess-crash-postmortem",
        spec=spec,
        run_id="run-crash",
        scenario="local_project",
        scenario_params={
            "project_repo_path": str(validated.repo_root),
            "project_head": validated.head_commit,
            "isolated_workspace": str(wt.isolated_path),
            "bug_description": "crash before scenario start",
            "config_root": str(session_root),
            "profile_id": "unused",
            "parent_tmpdir": str(wt.parent_tmpdir),
        },
        pre_start_delay_seconds=30.0,
        cooperative_grace_seconds=2.0,
    )
    try:
        assert worker.start() is None  # ready arrived; worker now sleeps pre-start
        assert wt.parent_tmpdir.exists()
        # Simulate a hard crash (no cooperative cancellation, no terminal).
        worker._proc.kill()
        worker._proc.wait(timeout=10)
        result = worker.wait()
        assert result.status.value == "interrupted"
        assert not wt.parent_tmpdir.exists()
        listing = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(validated.repo_root), capture_output=True, text=True, check=True,
        ).stdout
        assert str(wt.isolated_path) not in listing
        assert any(
            "post-mortem isolated worktree cleanup" in d for d in result.diagnostics
        )
    finally:
        worker.close()
        cleanup_parent_tmpdir(wt.parent_tmpdir, validated.repo_root)


# ---------------------------------------------------------------------------
# Apply-gate UTF-8 patch bytes
# ---------------------------------------------------------------------------


NON_ASCII_PATCH = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a + b  # café
"""


def test_check_apply_gates_non_ascii_patch_bytes(tmp_path):
    from agentic_debugger.application.local_project import (
        check_apply_gates,
        get_head_commit,
    )

    repo = _git_repo_with_file(tmp_path, "gates_repo")
    head = get_head_commit(repo)
    ok, reason = check_apply_gates(repo, head, NON_ASCII_PATCH)
    assert ok, reason


def test_check_apply_gates_blocks_dirty_and_head_change(tmp_path):
    from agentic_debugger.application.local_project import (
        check_apply_gates,
        get_head_commit,
    )

    repo = _git_repo_with_file(tmp_path, "gates_block")
    head = get_head_commit(repo)
    (repo / "uncommitted.txt").write_text("dirt", encoding="utf-8")
    ok, reason = check_apply_gates(repo, head, NON_ASCII_PATCH)
    assert not ok and "dirty" in reason
    (repo / "uncommitted.txt").unlink()
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "--allow-empty", "-m", "bump"],
        cwd=str(repo), check=True,
    )
    ok, reason = check_apply_gates(repo, head, NON_ASCII_PATCH)
    assert not ok and "HEAD changed" in reason


# ---------------------------------------------------------------------------
# Unified-diff parser: legal in-source Unicode characters
# ---------------------------------------------------------------------------


# The form-feed character (written \x0c below) is part of the source line
# content: the diff parser must treat it as content, never as a boundary.
FORM_FEED_PATCH = (
    "--- a/formfeed.py\n"
    "+++ b/formfeed.py\n"
    "@@ -1,3 +1,3 @@\n"
    " import os\n"
    "-def broken():\x0c\n"
    "+def fixed():\x0c\n"
    "     return 1\n"
)


def test_diff_parser_accepts_form_feed_in_source(tmp_path):
    src = tmp_path / "ff_src"
    src.mkdir()
    (src / "formfeed.py").write_bytes(b"import os\ndef broken():\x0c\n    return 1\n")
    ws = TaskWorkspace(str(src), parent_dir=str(tmp_path))
    try:
        pm = PatchManager(ws, ["formfeed.py"], ["tests", "task.json"])
        result = pm.apply_patch(FORM_FEED_PATCH)
        assert result.success is True
        assert (Path(ws.root) / "formfeed.py").read_bytes() == (
            b"import os\ndef fixed():\x0c\n    return 1\n"
        )
    finally:
        ws.cleanup()


# ---------------------------------------------------------------------------
# Revert rollback restores pre-revert bytes
# ---------------------------------------------------------------------------


TWO_FILE_PATCH = """--- a/one.py
+++ b/one.py
@@ -1 +1 @@
-one
+ONE
--- a/two.py
+++ b/two.py
@@ -1 +1 @@
-two
+TWO
"""


def test_revert_rollback_restores_pre_revert_state(tmp_path):
    src = tmp_path / "revert_src"
    src.mkdir()
    (src / "one.py").write_bytes(b"one\n")
    (src / "two.py").write_bytes(b"two\n")
    ws = TaskWorkspace(str(src), parent_dir=str(tmp_path))
    try:
        import agentic_debugger.runtime.patcher as patcher_mod
        from agentic_debugger.runtime.exceptions import PatchRevertError

        original_one = (Path(ws.root) / "one.py").read_bytes()
        original_two = (Path(ws.root) / "two.py").read_bytes()
        pm = PatchManager(ws, ["one.py", "two.py"], ["tests", "task.json"])
        pm.apply_patch(TWO_FILE_PATCH)
        patched_one = (Path(ws.root) / "one.py").read_bytes()
        patched_two = (Path(ws.root) / "two.py").read_bytes()
        assert b"ONE" in patched_one and b"TWO" in patched_two

        original_verify = patcher_mod._verify_file_hash
        # Fail the revert verify for the SECOND file (sorted order: one.py, two.py)
        def _failing_verify(target, expected_content):
            if str(target).endswith("two.py") and expected_content == original_two:
                raise patcher_mod.PatchApplyError("simulated revert verify failure")
            original_verify(target, expected_content)

        patcher_mod._verify_file_hash = _failing_verify
        try:
            with pytest.raises(PatchRevertError, match="pre-revert state"):
                pm.revert_patch()
        finally:
            patcher_mod._verify_file_hash = original_verify
        # Rollback must have restored the PATCHED bytes for one.py (already
        # reverted when the failure hit), never leave the original bytes.
        assert (Path(ws.root) / "one.py").read_bytes() == patched_one
        assert (Path(ws.root) / "two.py").read_bytes() == patched_two
        # Snapshot preserved: an honest retry must still be possible.
        assert pm.has_active_patch is True
        pm.revert_patch()
        assert (Path(ws.root) / "one.py").read_bytes() == original_one
    finally:
        ws.cleanup()


# ---------------------------------------------------------------------------
# TaskWorkspace cleanup vs read-only files (Windows temp leak)
# ---------------------------------------------------------------------------


def test_workspace_cleanup_removes_readonly_fixture_files(tmp_path):
    src = tmp_path / "ro_src"
    src.mkdir()
    target = src / "locked.py"
    target.write_text("x = 1\n", encoding="utf-8")
    os.chmod(target, stat.S_IREAD)
    ws = TaskWorkspace(str(src), parent_dir=str(tmp_path))
    root = ws.root
    ws.cleanup()
    assert not os.path.isdir(root)
    os.chmod(target, stat.S_IWRITE | stat.S_IREAD)


# ---------------------------------------------------------------------------
# Local Project sidebar identity through the durable reducer
# ---------------------------------------------------------------------------


def _diagnosis_event(observed: dict) -> app_events.SessionEvent:
    from agentic_debugger.application.emitter import SessionEventEmitter

    em = SessionEventEmitter(
        session_id="sess-20260101-000000-abcdef",
        task_id="local-project-debug",
        source_kind=app_events.SourceKind.LOCAL_PROJECT,
    )
    return em.emit(
        app_events.SessionEventKind.DIAGNOSIS_RECORDED,
        {
            "text": "bug",
            "file_path": None,
            "symbol": None,
            "confidence": "user-reported",
            "observed_values": observed,
        },
    )


def test_sidebar_identity_from_durable_observed_values():
    from agentic_debugger.ui.widgets import _local_project_identity

    event = _diagnosis_event({"repo_basename": "my-proj", "source_head": "0123456789ab"})
    identity = PresentationIdentity(
        task_id="local-project-debug",
        source_kind=app_events.SourceKind.LOCAL_PROJECT,
    )
    view = initial_session_view(identity)
    from agentic_debugger.application.presentation import reduce_event

    view = reduce_event(view, event)
    assert isinstance(view.diagnosis, DiagnosisView)
    assert view.diagnosis.observed_values == {"repo_basename": "my-proj", "source_head": "0123456789ab"}
    assert _local_project_identity(view) == ("my-proj", "0123456789ab")

    empty = initial_session_view(identity)
    assert _local_project_identity(empty) == ("—", "—")


# ---------------------------------------------------------------------------
# model.configured durable provenance in Local Project sessions
# ---------------------------------------------------------------------------


def test_local_project_session_records_model_configured(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "lp_tests_hardening", Path("tests/unit/test_local_project_debug.py")
    )
    t = importlib.util.module_from_spec(spec)
    sys.modules["lp_tests_hardening"] = t
    spec.loader.exec_module(t)

    from agentic_debugger.application.history import HistoryStore
    from agentic_debugger.application.journal import read_session_journal
    from agentic_debugger.application.local_project import (
        cleanup_parent_tmpdir,
        create_isolated_worktree,
        validate_local_project,
    )

    repo = _git_repo_with_file(tmp_path, "prov_repo")
    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    wt = create_isolated_worktree(validated.repo_root, validated.head_commit)
    patch_path = tmp_path / "prov.patch"
    t.write_calculator_patch(patch_path)
    store_root = tmp_path / "hist_prov"
    store_root.mkdir()
    t.write_local_profile(store_root, "prov-model", patch_path)
    store = HistoryStore(store_root)
    session_id = "sess-prov-hardening"
    worker = t.make_local_worker(
        store, session_id, repo, validated.head_commit,
        wt.isolated_path, wt.parent_tmpdir, "prov-model",
    )
    try:
        assert worker.start() is None
        worker.wait()
        journal = read_session_journal(store.session_dir(session_id) / "session.events.jsonl")
        kinds = [e.event_kind.value for e in journal.events]
        assert "model.configured" in kinds
        configured = next(e for e in journal.events if e.event_kind.value == "model.configured")
        assert configured.payload["profile_id"] == "prov-model"
        assert configured.payload["config_fingerprint"]
    finally:
        worker.close()
        cleanup_parent_tmpdir(wt.parent_tmpdir, repo)


# ---------------------------------------------------------------------------
# Professor trace reproduction truth
# ---------------------------------------------------------------------------


def _professor_trace_for_reproduction_observation(payload_fields: dict) -> dict:
    """Build one trace whose only observation is the given run_reproduction."""
    from agentic_debugger.evaluation.professor_trace import build_trace

    trajectory = json.dumps(
        {
            "event_type": "observation",
            "payload": {
                "observation": {
                    "observation_id": "o1",
                    "name": "run_reproduction",
                    "status": "ok",
                    "payload": payload_fields,
                }
            },
        }
    )
    return build_trace(
        {"trajectory_jsonl": trajectory, "task": {"task_id": "t1"}},
        {"provider": "x", "model": "y"},
    )


def test_professor_trace_reproduction_requires_observation():
    from agentic_debugger.evaluation.professor_trace import build_trace

    trace = _professor_trace_for_reproduction_observation(
        {"failure_reproduced": True, "failure_output": "boom"}
    )
    assert trace["failure_reproduction"]["reproduced"] is True
    assert trace["failure_reproduction"]["sanitized_summary"] == "boom"

    trace2 = build_trace(
        {"trajectory_jsonl": "", "task": {"task_id": "t1"}},
        {"provider": "x", "model": "y"},
    )
    assert trace2["failure_reproduction"]["reproduced"] is False
    assert "no successful baseline reproduction" in trace2["failure_reproduction"]["sanitized_summary"]


def test_professor_trace_reproduction_blank_output_does_not_erase_proof():
    """Proof and display text are independent (PRH-017 repair).

    An observed ``failure_reproduced is True`` stays proof even when the
    observation captured empty or no output text; only a missing or negative
    observation reports not reproduced.
    """
    # failure_reproduced=True with EMPTY output text.
    blank = _professor_trace_for_reproduction_observation(
        {"failure_reproduced": True, "failure_output": ""}
    )
    assert blank["failure_reproduction"]["reproduced"] is True
    assert "no output captured" in blank["failure_reproduction"]["sanitized_summary"]

    # failure_reproduced=True with the output field absent entirely.
    absent = _professor_trace_for_reproduction_observation({"failure_reproduced": True})
    assert absent["failure_reproduction"]["reproduced"] is True
    assert "no output captured" in absent["failure_reproduction"]["sanitized_summary"]

    # A negative observation is not proof, even with output text present.
    negative = _professor_trace_for_reproduction_observation(
        {"failure_reproduced": False, "failure_output": "boom"}
    )
    assert negative["failure_reproduction"]["reproduced"] is False
    assert (
        "no successful baseline reproduction"
        in negative["failure_reproduction"]["sanitized_summary"]
    )


# ---------------------------------------------------------------------------
# Apply To Project off-thread execution (PRH-007 real threading regression)
# ---------------------------------------------------------------------------


def test_apply_to_project_runs_off_ui_thread(tmp_path, monkeypatch):
    """The Apply gate/apply chain must run on a Textual worker thread.

    A deliberately blocked mocked gate proves ``action_apply_to_project``
    returns to the event loop before the gate finishes; recorded thread
    identities prove the gate and the apply executed OFF the UI thread; the
    recorded notifications prove the outcome is marshalled back to the UI.
    Gates/apply are mocks, so no owner repository is mutated.
    """
    asyncio = pytest.importorskip("asyncio")
    pytest.importorskip("textual")

    import threading

    import agentic_debugger.application.local_project as local_project_module
    from agentic_debugger.application.events import SessionStatus
    from agentic_debugger.application.history import (
        HistoryClassification,
        SessionHistoryEntry,
    )
    from agentic_debugger.ui.app import LocalApplicationV1
    from agentic_debugger.ui.screens import WorkspaceMode, WorkspaceScreen

    gate_entered = threading.Event()
    gate_release = threading.Event()
    apply_done = threading.Event()
    gate_thread: dict[str, int] = {}
    apply_thread: dict[str, int] = {}

    def fake_check_apply_gates(repo_path, expected_head, patch_text):  # type: ignore[no-untyped-def]
        gate_thread["ident"] = threading.get_ident()
        gate_entered.set()
        # Deliberate block: the UI action must return while the gate holds.
        if not gate_release.wait(timeout=10.0):
            return False, "test gate block timed out"
        return True, "gates ok (mock)"

    def fake_apply_patch_to_project(repo_path, patch_text):  # type: ignore[no-untyped-def]
        apply_thread["ident"] = threading.get_ident()
        apply_done.set()
        return True, "applied (mock)"

    monkeypatch.setattr(
        local_project_module, "check_apply_gates", fake_check_apply_gates
    )
    monkeypatch.setattr(
        local_project_module, "apply_patch_to_project", fake_apply_patch_to_project
    )

    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir()
    session_dir = tmp_path / "sess_apply_thread"
    session_dir.mkdir()
    (session_dir / "local_project_task.json").write_text(
        json.dumps(
            {
                "source_repo_path": str(owner_repo),
                "source_head_commit": "0" * 40,
            }
        ),
        encoding="utf-8",
    )

    view = SessionViewState(
        source_kind=app_events.SourceKind.LOCAL_PROJECT,
        status=SessionStatus.SUCCEEDED,
        patch_attempts=(
            _attempt(0, PatchStage.APPLIED, "--- a/calculator.py\n+++ b/calculator.py\n"),
        ),
    )

    async def scenario() -> None:
        app = LocalApplicationV1(history_root=str(tmp_path / "history"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = WorkspaceScreen(
                mode=WorkspaceMode.LIVE,
                identity=PresentationIdentity(
                    task_id="local-project-debug",
                    source_kind=app_events.SourceKind.LOCAL_PROJECT,
                ),
                view=view,
            )
            screen.entry = SessionHistoryEntry(
                session_id="sess-apply-thread",
                classification=HistoryClassification.COMPLETE,
                directory=str(session_dir),
                task_id="local-project-debug",
                source_kind=app_events.SourceKind.LOCAL_PROJECT,
            )
            notifications: list[str] = []
            original_notify = screen.notify

            def recording_notify(message, **kwargs):  # type: ignore[no-untyped-def]
                notifications.append(str(message))
                return original_notify(message, **kwargs)

            screen.notify = recording_notify  # type: ignore[method-assign]
            app.push_screen(screen)
            await pilot.pause()

            ui_thread_id = threading.get_ident()
            # UI-thread action: must schedule the worker and return while the
            # mocked gate is still blocked.
            screen.action_apply_to_project()

            # to_thread keeps the event loop responsive while the worker
            # thread starts and reaches the gate.
            started = await asyncio.to_thread(gate_entered.wait, 10.0)
            assert started, "apply gate never started on the worker thread"
            # We are executing AFTER action_apply_to_project() returned and
            # the gate is STILL blocked (gate_release unset): synchronous
            # gate execution could not reach this point.
            assert gate_thread["ident"] != ui_thread_id
            assert not apply_done.is_set()

            gate_release.set()
            applied = await asyncio.to_thread(apply_done.wait, 10.0)
            assert applied, "apply never executed on the worker thread"
            assert apply_thread["ident"] == gate_thread["ident"]

            # The final outcome must be marshalled back onto the UI thread.
            for _ in range(20):
                if any("applied (mock)" in message for message in notifications):
                    break
                await pilot.pause()
            assert any(
                "applied (mock)" in message for message in notifications
            ), f"apply outcome never reached the UI: {notifications!r}"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Local Project baseline reproduction truth (exit code is the only proof)
# ---------------------------------------------------------------------------


def _dispatch_local_reproduction(isolated: Path, command: str, phase: str):
    """Dispatch run_reproduction through the REAL Local Project registry."""
    from agentic_debugger.agent.controller_policy import ActionName, ControllerState
    from agentic_debugger.application.local_project_source import (
        LocalProjectTask,
        _LocalToolContext,
        _build_local_registry,
    )
    from agentic_debugger.events.schema import Action

    context = _LocalToolContext(
        isolated=isolated,
        tracked=[],
        task=LocalProjectTask(
            bug_description="user-reported bug in add()",
            reproduction_command=command,
        ),
        probe=None,
        observability=None,
    )
    registry = _build_local_registry(context)
    state = (
        ControllerState.REPRODUCE if phase == "baseline" else ControllerState.VALIDATE
    )
    action = Action(
        action_id="action-000000000",
        run_id="run-repro-truth",
        task_id="local-project-debug",
        state=state,
        name=ActionName.RUN_REPRODUCTION.value,
        arguments={"phase": phase},
    )
    observation = registry.dispatch(action, observation_id="observation-000000000")
    return context, observation


def test_local_project_baseline_reproduction_exit_nonzero_is_reproduced(tmp_path):
    from agentic_debugger.events.schema import ObservationStatus

    isolated = tmp_path / "iso_fail"
    isolated.mkdir()
    (isolated / "fail.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    context, observation = _dispatch_local_reproduction(
        isolated, "python fail.py", "baseline"
    )
    assert observation.status is ObservationStatus.OK
    assert observation.payload["failure_reproduced"] is True
    assert observation.payload["passed"] is False
    assert observation.payload["exit_code"] == 1
    assert context.baseline_failure_reproduced is True


def test_local_project_baseline_reproduction_exit_zero_is_not_reproduced(tmp_path):
    from agentic_debugger.events.schema import ObservationStatus

    isolated = tmp_path / "iso_pass"
    isolated.mkdir()
    (isolated / "pass.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    context, observation = _dispatch_local_reproduction(
        isolated, "python pass.py", "baseline"
    )
    assert observation.status is ObservationStatus.OK
    # A bug description exists, but a passing baseline command is NOT
    # reproduction proof.  The truthful False in this payload is exactly the
    # value LiveModelAdapter reads for its failure_trace_allowed gate.
    assert observation.payload["failure_reproduced"] is False
    assert observation.payload["passed"] is True
    assert observation.payload["exit_code"] == 0
    assert context.baseline_failure_reproduced is False


def test_local_project_post_patch_reproduction_exit_zero(tmp_path):
    from agentic_debugger.events.schema import ObservationStatus

    isolated = tmp_path / "iso_post"
    isolated.mkdir()
    (isolated / "pass.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    context, observation = _dispatch_local_reproduction(
        isolated, "python pass.py", "post_patch"
    )
    assert observation.status is ObservationStatus.OK
    assert observation.payload["passed"] is True
    assert observation.payload["failure_reproduced"] is False
    assert context.post_patch_f2p_passed is True


# ---------------------------------------------------------------------------
# MODEL_CONFIGURED journal failure fails closed (journal = durable truth)
# ---------------------------------------------------------------------------


def test_model_configured_journal_failure_fails_closed(tmp_path):
    """A journal that cannot record model.configured must stop the session
    before any model request starts (fail-closed repair).

    The durable sink rejects exactly the MODEL_CONFIGURED append; the
    emitter converts that into the authoritative EmitterFatalError, which
    must propagate out of the Local Project source before the controller
    (and the transport) ever run.
    """
    from agentic_debugger.application.emitter import (
        EmitterFatalError,
        SessionEventEmitter,
    )
    from agentic_debugger.application.events import SessionEventKind, SourceKind
    from agentic_debugger.application.history import HistoryStore
    from agentic_debugger.application.journal import (
        SessionEventJournal,
        read_session_journal,
    )
    from agentic_debugger.application.local_project import (
        cleanup_parent_tmpdir,
        create_isolated_worktree,
        validate_local_project,
    )
    from agentic_debugger.application.local_project_source import (
        run_local_project_session,
    )
    from agentic_debugger.application.worker_scenarios import ScenarioContext
    from agentic_debugger.cancellation import CancellationToken

    session_id = "sess-mcfail"

    # Counting model stand-in: every real transport invocation appends here.
    request_log = tmp_path / "model_requests.log"
    counting_model = tmp_path / "counting_model.py"
    counting_model.write_text(
        "import sys\n"
        f"with open({str(request_log)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write('request\\n')\n"
        "sys.stdout.write('{}\\n')\n",
        encoding="utf-8",
    )

    repo = _git_repo_with_file(tmp_path, "mc_repo")
    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    wt = create_isolated_worktree(validated.repo_root, validated.head_commit)
    store_root = tmp_path / "hist_mcfail"
    config_dir = store_root / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "command-models.json").write_text(
        json.dumps(
            {
                "schema_version": "command-models-v1",
                "profiles": [
                    {
                        "profile_id": "counting-model",
                        "display_name": "Counting local",
                        "executable": sys.executable,
                        "argv": [str(counting_model)],
                        "request_timeout_seconds": 10,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = HistoryStore(store_root)
    session_dir = store.session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    journal = SessionEventJournal(
        session_dir / "session.events.jsonl",
        session_id=session_id,
        task_id="local-project-debug",
        source_kind=SourceKind.LOCAL_PROJECT,
    )

    class _ModelConfiguredRejectingSink:
        """Durable-sink double rejecting exactly the MODEL_CONFIGURED append."""

        def __init__(self, inner):  # type: ignore[no-untyped-def]
            self._inner = inner

        def append(self, event):  # type: ignore[no-untyped-def]
            if event.event_kind is SessionEventKind.MODEL_CONFIGURED:
                raise OSError("injected durable journal failure")
            return self._inner.append(event)

        def flush(self):  # type: ignore[no-untyped-def]
            return self._inner.flush()

        def close(self):  # type: ignore[no-untyped-def]
            return self._inner.close()

    emitter = SessionEventEmitter(
        session_id=session_id,
        task_id="local-project-debug",
        source_kind=SourceKind.LOCAL_PROJECT,
        sink=_ModelConfiguredRejectingSink(journal),
    )
    ctx = ScenarioContext(
        work_dir=tmp_path / "work_mcfail",
        token=CancellationToken(),
        emitter=emitter,
        journal=journal,
        run_id=f"run-{session_id}",
        session_dir=session_dir,
    )
    params = {
        "project_repo_path": str(repo),
        "project_head": validated.head_commit,
        "isolated_workspace": str(wt.isolated_path),
        "bug_description": "add returns a - b",
        "reproduction_command": 'python -c "import sys; sys.exit(1)"',
        "verification_command": 'python -c "import sys; sys.exit(0)"',
        "config_root": str(store_root),
        "profile_id": "counting-model",
        "expected_fingerprint": None,
        "parent_tmpdir": str(wt.parent_tmpdir),
        "policy": "pdb-on-uncertainty",
    }
    try:
        with pytest.raises(EmitterFatalError):
            run_local_project_session(ctx, params)

        recorded = [
            event.event_kind.value
            for event in read_session_journal(
                session_dir / "session.events.jsonl"
            ).events
        ]
        # The mandatory provenance was durably rejected, and no model request
        # followed it.
        assert "model.configured" not in recorded
        assert "model.request.started" not in recorded
        # The fake transport/model adapter received zero requests.
        assert not request_log.exists()
        # Sticky fail-closed journal state: nothing further can be recorded.
        with pytest.raises(EmitterFatalError):
            emitter.emit(
                SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running"}
            )
    finally:
        try:
            journal.close()
        except Exception:
            pass
        cleanup_parent_tmpdir(wt.parent_tmpdir, repo)


# ---------------------------------------------------------------------------
# PRH-029 — completed Local Project sessions preserve the canonical task spec
# ---------------------------------------------------------------------------


def _write_scripted_profile(config_dir, profile_id, script_path):  # type: ignore[no-untyped-def]
    """Write a CommandModelConfigStore profile executing the given script."""
    import json as _json

    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "command-models.json").write_text(
        _json.dumps(
            {
                "schema_version": "command-models-v1",
                "profiles": [
                    {
                        "profile_id": profile_id,
                        "display_name": "Scripted local",
                        "executable": sys.executable,
                        "argv": [str(script_path)],
                        "request_timeout_seconds": 10,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_completed_local_project_session_preserves_canonical_task_spec(tmp_path):
    """A real completed Local Project session must not clobber the canonical
    ``local_project_task.json`` (PRH-029).

    Runs the REAL production worker/source path to terminal completion,
    then reads the artifact back with ``LocalProjectTaskSpec.from_mapping``,
    asserts the original repo/HEAD survived, removes the isolated worktree
    (history/reopen must not depend on it), and exercises the REAL
    Apply-To-Project provenance reader against that completed session —
    proving it no longer fails with "Local project provenance not found".
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "lp_tests_hardening", Path("tests/unit/test_local_project_debug.py")
    )
    t = importlib.util.module_from_spec(spec)
    sys.modules["lp_tests_hardening"] = t
    spec.loader.exec_module(t)

    from agentic_debugger.application.history import HistoryStore
    from agentic_debugger.application.local_project import (
        LocalProjectTaskSpec,
        cleanup_parent_tmpdir,
        create_isolated_worktree,
        validate_local_project,
    )

    # The fixture must carry the buggy calculator (a - b) that the scripted
    # model's patch fixes, exactly like the accepted local-project harness.
    repo = _git_repo_with_file(tmp_path, "prh029_repo")
    (repo / "calculator.py").write_text(
        "def add(a,b):\n    return a - b\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "--all"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "buggy"],
        cwd=str(repo), check=True,
    )
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head_before != (subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip())
    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    assert validated.head_commit == head_before
    wt = create_isolated_worktree(validated.repo_root, validated.head_commit)

    patch_path = tmp_path / "prh029.patch"
    t.write_calculator_patch(patch_path)
    store_root = tmp_path / "hist_prh029"
    store_root.mkdir()
    t.write_local_profile(store_root, "prh029-model", patch_path)
    store = HistoryStore(store_root)
    session_id = "sess-prh029-canonical"
    worker = t.make_local_worker(
        store, session_id, repo, head_before,
        wt.isolated_path, wt.parent_tmpdir, "prh029-model",
    )
    try:
        # A. real worker/source path to terminal completion.
        assert worker.start() is None
        result = worker.wait()
        assert result.status.value == "succeeded", result
        assert result.termination_reason.value == "done", result

        # C. the artifact AFTER the worker/source has finished.
        task_path = store.session_dir(session_id) / "local_project_task.json"
        assert task_path.is_file(), "canonical local_project_task.json missing"

        # D. parses under the canonical contract.
        raw = json.loads(task_path.read_text(encoding="utf-8"))
        task_spec = LocalProjectTaskSpec.from_mapping(raw)
        # The mapping is canonical (no competing schema keys).
        assert "source_repo_path" in raw and "source_head_commit" in raw
        assert "project_repo_path" not in raw and "project_head" not in raw

        # E. original repo/HEAD survived unchanged.
        assert Path(task_spec.source_repo_path).resolve() == repo.resolve()
        assert task_spec.source_head_commit == head_before
        assert task_spec.session_id == session_id
        assert task_spec.isolated_workspace_path == str(wt.isolated_path)
        assert task_spec.bug_description == "add returns a - b"
    finally:
        worker.close()
        cleanup_parent_tmpdir(wt.parent_tmpdir, repo)

    # The isolated worktree is now GONE; reopen/replay must not depend on it.
    assert not wt.isolated_path.exists()
    reopened = store.reopen(session_id)
    assert reopened.entry.session_id == session_id
    # The durable journal replays without the workspace (reopen reads only
    # the journal; manifest registration is the runner's job, not the raw
    # harness worker's — replay here is the dependency-free proof).
    assert [e.event_kind.value for e in reopened.replay.events]

    # F. the REAL Apply provenance reader recovers repo+HEAD from the
    #    completed session (no manual artifact construction, worktree gone).
    _apply_provenance_task(session_id, task_path, repo, head_before, tmp_path)


def _apply_provenance_task(session_id, task_path, owner_repo, expected_head, tmp_path):  # type: ignore[no-untyped-def]
    """Exercise the real action_apply_to_project provenance path headlessly.

    The completed session's session dir is opened directly in the live
    workspace (the same provenance read Apply uses); the gates are stubbed
    OUTSIDE the provenance path, and notifications are recorded.  If the
    provenance cannot be recovered, the action notifies
    "Local project provenance not found" and never schedules a worker.
    """
    asyncio = pytest.importorskip("asyncio")
    pytest.importorskip("textual")

    import threading

    import agentic_debugger.application.local_project as local_project_module
    from agentic_debugger.application.events import SessionStatus
    from agentic_debugger.application.history import (
        HistoryClassification,
        SessionHistoryEntry,
    )
    from agentic_debugger.ui.app import LocalApplicationV1
    from agentic_debugger.ui.screens import WorkspaceMode, WorkspaceScreen

    gate_called: dict[str, bool] = {}
    apply_called: dict[str, bool] = {}

    def fake_check_apply_gates(repo_path, expected_head_actual, patch_text):  # type: ignore[no-untyped-def]
        gate_called["yes"] = True
        assert Path(repo_path).resolve() == owner_repo.resolve()
        assert expected_head_actual == expected_head
        return True, "gates ok"

    def fake_apply_patch_to_project(repo_path, patch_text):  # type: ignore[no-untyped-def]
        apply_called["yes"] = True
        return True, "applied (mock)"

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        local_project_module, "check_apply_gates", fake_check_apply_gates
    )
    monkeypatch.setattr(
        local_project_module, "apply_patch_to_project", fake_apply_patch_to_project
    )
    try:
        view = SessionViewState(
            source_kind=app_events.SourceKind.LOCAL_PROJECT,
            status=SessionStatus.SUCCEEDED,
            patch_attempts=(
                _attempt(0, PatchStage.APPLIED, "--- a/calculator.py\n+++ b/calculator.py\n"),
            ),
        )

        async def scenario() -> None:
            app = LocalApplicationV1(history_root=str(tmp_path / "hist_apply"))
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen = WorkspaceScreen(
                    mode=WorkspaceMode.LIVE,
                    identity=PresentationIdentity(
                        task_id="local-project-debug",
                        source_kind=app_events.SourceKind.LOCAL_PROJECT,
                    ),
                    view=view,
                )
                screen.entry = SessionHistoryEntry(
                    session_id=session_id,
                    classification=HistoryClassification.COMPLETE,
                    directory=str(task_path.parent),
                    task_id="local-project-debug",
                    source_kind=app_events.SourceKind.LOCAL_PROJECT,
                )
                notifications: list[str] = []
                original_notify = screen.notify

                def recording_notify(message, **kwargs):  # type: ignore[no-untyped-def]
                    notifications.append(str(message))
                    return original_notify(message, **kwargs)

                screen.notify = recording_notify  # type: ignore[method-assign]
                app.push_screen(screen)
                await pilot.pause()

                screen.action_apply_to_project()

                for _ in range(30):
                    if gate_called.get("yes") and apply_called.get("yes"):
                        break
                    await asyncio.sleep(0.05)
                    await pilot.pause()

                assert gate_called.get("yes"), (
                    f"Apply gates never ran; provenance not recovered: {notifications!r}"
                )
                assert apply_called.get("yes"), f"Apply never ran: {notifications!r}"
                assert not any(
                    "Local project provenance not found" in message
                    for message in notifications
                ), f"provenance block still present: {notifications!r}"

        asyncio.run(scenario())
    finally:
        monkeypatch.undo()
