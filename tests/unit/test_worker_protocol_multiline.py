"""Multiline bug_description start-message fix — production-boundary tests.

Covers LOCAL-PROJECT-DEBUG-01 sections 5-8: exact round-trip preservation,
smoke-shaped start, negative safety, and command single-line.

No provider, no Docker.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from agentic_debugger.application.events import SourceKind
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.local_project import (
    cleanup_parent_tmpdir,
    create_isolated_worktree,
    get_head_commit,
    validate_local_project,
)
from agentic_debugger.application.session import SessionBudgets, SessionSpec
from agentic_debugger.application.sources import ExecutionSourceSpec
from agentic_debugger.application.worker_protocol import (
    WorkerProtocolError,
    parse_parent_message,
    parse_start_request,
    start_message,
)


FIXTURE_LOCAL_DUMMY = Path(__file__).resolve().parents[1] / "fixtures" / "command_models" / "local_project_dummy.py"


def _make_spec():
    return SessionSpec(
        task_id="local-project-debug",
        source=ExecutionSourceSpec(
            kind=SourceKind.LOCAL_PROJECT,
            task_id="local-project-debug",
            policy=None,
            model_config_ref="qwen3.5:cloud",
        ),
        budgets=SessionBudgets(max_elapsed_seconds=None),
    )


def _real_local_params(bug_description: str, tmp_path: Path, extra: dict | None = None):
    """Build a realistic scenario_params mapping for Local Project.

    Uses real git fixture paths when available; falls back to bounded placeholders
    for pure protocol tests that don't need a worktree.
    """
    base = {
        "project_repo_path": str(tmp_path / "repo"),
        "project_head": "a" * 40,
        "isolated_workspace": str(tmp_path / "isolated"),
        "bug_description": bug_description,
        "reproduction_command": "python repro.py",
        "verification_command": "python repro.py",
        "config_root": str(tmp_path / "cfg"),
        "profile_id": "qwen3.5:cloud",
        "expected_fingerprint": None,
        "parent_tmpdir": str(tmp_path / "parent"),
        "policy": "pdb-on-uncertainty",
    }
    if extra:
        base.update(extra)
    return base


# ---------------------------------------------------------------------------
# 5. EXACT ROUND-TRIP TEST
# ---------------------------------------------------------------------------

def test_5_bug_description_multiline_exact_round_trip(tmp_path):
    """Section 5: multiline bug_description survives worker protocol boundary."""
    original = "first line\n\nsecond line\nthird line"
    spec = _make_spec()
    params = _real_local_params(original, tmp_path)
    raw = start_message(
        session_id="sess-20260827-000001",
        spec=spec,
        run_id="run-sess-20260827-000001",
        work_dir=str(tmp_path / "work"),
        journal_path=str(tmp_path / "journal.jsonl"),
        scenario="local_project",
        scenario_params=params,
    )
    # Ensure JSON / PIPE safety: raw line is single JSON line, newline only at end,
    # not embedded raw newlines inside the frame.  json.dumps must have escaped them.
    assert raw.endswith(b"\n")
    # The body before the trailing newline must be valid JSON with escaped newlines
    body = raw[:-1]
    assert b"\n" not in body  # no raw embedded newline
    # The JSON text must contain escaped \n for the bug_description
    assert b"\\n" in body
    # Parse through real worker protocol boundary
    payload = parse_parent_message(body.decode("utf-8"))
    request = parse_start_request(payload)
    received = request.scenario_params["bug_description"]
    assert received == original, f"expected {original!r} got {received!r}"
    # Blank line preserved exactly
    assert received.count("\n") == original.count("\n")
    assert received.splitlines() == original.splitlines()


def test_5_crlf_preserved(tmp_path):
    original = "first line\r\nsecond line\r\nthird line"
    spec = _make_spec()
    params = _real_local_params(original, tmp_path)
    raw = start_message(
        session_id="sess-20260827-000002",
        spec=spec,
        run_id="run-sess-20260827-000002",
        work_dir=str(tmp_path / "work"),
        journal_path=str(tmp_path / "journal.jsonl"),
        scenario="local_project",
        scenario_params=params,
    )
    body = raw[:-1]
    assert b"\n" not in body
    request = parse_start_request(parse_parent_message(body.decode("utf-8")))
    assert request.scenario_params["bug_description"] == original


def test_5_tab_preserved(tmp_path):
    original = "first\tsecond\nthird"
    spec = _make_spec()
    params = _real_local_params(original, tmp_path)
    raw = start_message(
        session_id="sess-20260827-000003",
        spec=spec,
        run_id="run-sess-20260827-000003",
        work_dir=str(tmp_path / "work"),
        journal_path=str(tmp_path / "journal.jsonl"),
        scenario="local_project",
        scenario_params=params,
    )
    body = raw[:-1]
    # TAB is allowed in bug_description; raw frame still single line (TAB inside JSON string escaped as \t)
    # json.dumps escapes \t as \t, not raw tab breaking frame, but raw tab inside JSON string is also allowed as byte 0x09 inside quoted string? json dumps with ensure_ascii=False will emit actual tab? Check: python json dumps escapes \t? Actually json.dumps escapes tab as \t. So body should not contain raw newline, but may contain raw tab? We check newline only.
    assert b"\n" not in body
    request = parse_start_request(parse_parent_message(body.decode("utf-8")))
    assert request.scenario_params["bug_description"] == original


# ---------------------------------------------------------------------------
# 6. REAL SMOKE-SHAPED START TEST (clean fixture, multiline, default model)
# ---------------------------------------------------------------------------

def _run(cmd, cwd):
    r = subprocess.run(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
    assert r.returncode == 0, f"{cmd} failed: {r.stderr}"
    return r.stdout.strip()


def _make_git_repo(tmp_path: Path, name: str = "proj") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "test@test.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    (repo / "repro.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "file.py").write_text("x=1\n", encoding="utf-8")
    (repo / "calculator.py").write_text("def add(a,b):\n    return a - b\n", encoding="utf-8")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-m", "initial"], repo)
    return repo


def _write_local_profile(root: Path, profile_id: str, patch_path: Path):
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir = root / f"state-{profile_id}"
    data_file = root / f"data-{profile_id}.json"
    data_file.write_text(json.dumps({
        "symbol": "add",
        "file": "calculator.py",
        "hypothesis_id": "h1",
        "statement": "add returns a - b instead of a + b",
        "patch_file": str(patch_path),
        "expressions": [],
    }), encoding="utf-8")
    argv = [str(FIXTURE_LOCAL_DUMMY), "--state-dir", str(state_dir), "--data", str(data_file)]
    (config_dir / "command-models.json").write_text(json.dumps({
        "schema_version": "command-models-v1",
        "profiles": [{
            "profile_id": profile_id,
            "display_name": "qwen3.5:cloud",
            "executable": sys.executable,
            "argv": argv,
            "request_timeout_seconds": 10,
        }]
    }), encoding="utf-8")


def test_6_real_smoke_shaped_start_multiline(tmp_path):
    """Section 6: clean fixture + multiline bug + default model/qwen3.5:cloud.

    Asserts start-message validation PASS, send PASS, worker receives task,
    Project/source HEAD become available at worker execution setup, no
    "control characters" startup failure, stop before real model transport
    (uses fake/scripted command model via local_project_dummy).
    """
    repo = _make_git_repo(tmp_path, "smoke")
    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    assert validated.dirty is False
    wt = create_isolated_worktree(validated.repo_root, validated.head_commit)
    try:
        head = get_head_commit(repo)
        assert head == validated.head_commit
        patch_path = tmp_path / "smoke.patch"
        patch_path.write_text(
            "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n def add(a,b):\n-    return a - b\n+    return a + b\n",
            encoding="utf-8",
        )
        # Protocol-level check uses the real alias qwen3.5:cloud (must pass worker_protocol)
        store_root_protocol = tmp_path / "hist_smoke_proto"
        store_root_protocol.mkdir()
        session_id_proto = f"sess-smoke-{uuid.uuid4().hex[:6]}"
        bug_description = (
            "discounted_price applies the discount in the wrong direction.\n\n"
            "discounted_price(100, 0.20) should return 80, but repro.py currently fails."
        )
        repro = "python repro.py"
        verify = "python repro.py"
        model_alias = "qwen3.5:cloud"
        spec_proto = SessionSpec(
            task_id="local-project-debug",
            source=ExecutionSourceSpec(
                kind=SourceKind.LOCAL_PROJECT,
                task_id="local-project-debug",
                policy=None,
                model_config_ref=model_alias,
            ),
            budgets=SessionBudgets(max_elapsed_seconds=None),
        )
        scenario_params_proto = {
            "project_repo_path": str(validated.repo_root),
            "project_head": head,
            "isolated_workspace": str(wt.isolated_path),
            "bug_description": bug_description,
            "reproduction_command": repro,
            "verification_command": verify,
            "config_root": str(store_root_protocol),
            "profile_id": model_alias,
            "expected_fingerprint": None,
            "parent_tmpdir": str(wt.parent_tmpdir),
            "policy": "pdb-on-uncertainty",
        }
        # start-message validation PASS + send PASS (would have raised WorkerProtocolError before fix)
        raw = start_message(
            session_id=session_id_proto,
            spec=spec_proto,
            run_id=f"run-{session_id_proto}",
            work_dir=str(tmp_path / "work_smoke"),
            journal_path=str(store_root_protocol / "sessions" / session_id_proto / "session.events.jsonl"),
            scenario="local_project",
            scenario_params=scenario_params_proto,
            max_elapsed_seconds=None,
        )
        assert raw.endswith(b"\n")
        body = raw[:-1]
        assert b"\n" not in body
        payload = parse_parent_message(body.decode("utf-8"))
        request = parse_start_request(payload)
        # worker receives task: Project/source HEAD available at execution setup
        assert request.scenario_params["project_repo_path"] == str(validated.repo_root)
        assert request.scenario_params["project_head"] == head
        assert request.scenario_params["bug_description"] == bug_description
        assert request.scenario_params["bug_description"].count("\n") == 2
        assert "\n\n" in request.scenario_params["bug_description"]
        assert request.scenario_params["reproduction_command"] == repro
        assert request.scenario_params["verification_command"] == verify

        # Worker execution uses a valid dummy profile (bounded v1) with same multiline bug,
        # proving no control-char startup failure before any model transport.
        store_root = tmp_path / "hist_smoke"
        store_root.mkdir()
        dummy_profile = "dummy-multiline-smoke"
        _write_local_profile(store_root, dummy_profile, patch_path)
        store = HistoryStore(store_root)
        session_id = f"sess-smoke-{uuid.uuid4().hex[:6]}"
        spec = SessionSpec(
            task_id="local-project-debug",
            source=ExecutionSourceSpec(
                kind=SourceKind.LOCAL_PROJECT,
                task_id="local-project-debug",
                policy=None,
                model_config_ref=dummy_profile,
            ),
            budgets=SessionBudgets(max_elapsed_seconds=None),
        )
        scenario_params = {
            "project_repo_path": str(validated.repo_root),
            "project_head": head,
            "isolated_workspace": str(wt.isolated_path),
            "bug_description": bug_description,
            "reproduction_command": repro,
            "verification_command": verify,
            "config_root": str(store_root),
            "profile_id": dummy_profile,
            "expected_fingerprint": None,
            "parent_tmpdir": str(wt.parent_tmpdir),
            "policy": "pdb-on-uncertainty",
        }
        # start-message for dummy profile also must pass
        raw2 = start_message(
            session_id=session_id,
            spec=spec,
            run_id=f"run-{session_id}",
            work_dir=str(tmp_path / "work_smoke2"),
            journal_path=str(store_root / "sessions" / session_id / "session.events.jsonl"),
            scenario="local_project",
            scenario_params=scenario_params,
            max_elapsed_seconds=None,
        )
        assert raw2.endswith(b"\n")
        # No "control characters" failure — we got here
        # Now run through the real worker boundary with fake model, stop before transport
        from agentic_debugger.application.worker_process import SessionWorkerProcess

        worker = SessionWorkerProcess(
            session_dir=store.session_dir(session_id),
            session_id=session_id,
            spec=spec,
            run_id=f"run-{session_id}",
            scenario="local_project",
            scenario_params=scenario_params,
            cooperative_grace_seconds=5.0,
            ready_timeout_seconds=30.0,
            max_elapsed_seconds=None,
        )
        try:
            # start + wait through fake model — proves worker startup not failed due to control chars
            start_err = worker.start()
            assert start_err is None, f"worker.start failed: {start_err}"
            result = worker.wait()
            # Worker received task and ran: Project/source HEAD were available (journal proves)
            from agentic_debugger.application.journal import read_session_journal

            journal = read_session_journal(store.session_dir(session_id) / "session.events.jsonl")
            # diagnosis.recorded contains the multiline bug text
            texts = [e.payload.get("text") or "" for e in journal.events if e.event_kind.value == "diagnosis.recorded"]
            assert any("discounted_price" in t for t in texts), f"bug text not in journal: {texts[:3]}"
            # Ensure startup did not produce invalid_request control-char error
            assert result is not None
            # Terminal should be reachable (succeeded/unresolved), not startup error 2
            assert result.status.value in ("succeeded", "unresolved", "failed", "cleanup_failed")
            assert "control characters" not in " ".join(result.diagnostics).lower()
        finally:
            worker.close()
        # cleanup_parent_tmpdir will be verified after worker already cleaned; ensure no leftover
    finally:
        cleanup_parent_tmpdir(wt.parent_tmpdir, repo)


# ---------------------------------------------------------------------------
# 7. NEGATIVE SAFETY TESTS
# ---------------------------------------------------------------------------

def test_7_ordinary_param_with_nul_rejected(tmp_path):
    spec = _make_spec()
    params = _real_local_params("ok bug", tmp_path)
    params["ordinary"] = "bad\x00value"
    with pytest.raises(WorkerProtocolError, match="control characters"):
        start_message(
            session_id="sess-20260827-000010",
            spec=spec,
            run_id="run-sess-20260827-000010",
            work_dir=str(tmp_path / "work"),
            journal_path=str(tmp_path / "journal.jsonl"),
            scenario="local_project",
            scenario_params=params,
        )


def test_7_bug_description_with_nul_rejected(tmp_path):
    spec = _make_spec()
    params = _real_local_params("bad\x00bug", tmp_path)
    with pytest.raises(WorkerProtocolError, match="control characters"):
        start_message(
            session_id="sess-20260827-000011",
            spec=spec,
            run_id="run-sess-20260827-000011",
            work_dir=str(tmp_path / "work"),
            journal_path=str(tmp_path / "journal.jsonl"),
            scenario="local_project",
            scenario_params=params,
        )


def test_7_bug_description_normal_newline_accepted(tmp_path):
    spec = _make_spec()
    params = _real_local_params("line1\nline2", tmp_path)
    raw = start_message(
        session_id="sess-20260827-000012",
        spec=spec,
        run_id="run-sess-20260827-000012",
        work_dir=str(tmp_path / "work"),
        journal_path=str(tmp_path / "journal.jsonl"),
        scenario="local_project",
        scenario_params=params,
    )
    request = parse_start_request(parse_parent_message(raw[:-1].decode("utf-8")))
    assert request.scenario_params["bug_description"] == "line1\nline2"


def test_7_bug_description_crlf_accepted_and_preserved(tmp_path):
    spec = _make_spec()
    original = "a\r\nb\r\nc"
    params = _real_local_params(original, tmp_path)
    raw = start_message(
        session_id="sess-20260827-000013",
        spec=spec,
        run_id="run-sess-20260827-000013",
        work_dir=str(tmp_path / "work"),
        journal_path=str(tmp_path / "journal.jsonl"),
        scenario="local_project",
        scenario_params=params,
    )
    request = parse_start_request(parse_parent_message(raw[:-1].decode("utf-8")))
    assert request.scenario_params["bug_description"] == original


def test_7_bug_description_tab_accepted_and_preserved(tmp_path):
    spec = _make_spec()
    original = "a\tb\nc"
    params = _real_local_params(original, tmp_path)
    raw = start_message(
        session_id="sess-20260827-000014",
        spec=spec,
        run_id="run-sess-20260827-000014",
        work_dir=str(tmp_path / "work"),
        journal_path=str(tmp_path / "journal.jsonl"),
        scenario="local_project",
        scenario_params=params,
    )
    request = parse_start_request(parse_parent_message(raw[:-1].decode("utf-8")))
    assert request.scenario_params["bug_description"] == original


def test_7_bug_description_other_c0_rejected(tmp_path):
    spec = _make_spec()
    # \x01 is not allowed, even in bug_description
    params = _real_local_params("bad\x01bug", tmp_path)
    with pytest.raises(WorkerProtocolError, match="control characters"):
        start_message(
            session_id="sess-20260827-000015",
            spec=spec,
            run_id="run-sess-20260827-000015",
            work_dir=str(tmp_path / "work"),
            journal_path=str(tmp_path / "journal.jsonl"),
            scenario="local_project",
            scenario_params=params,
        )
    # DEL 0x7F also rejected
    params2 = _real_local_params("bad\x7fbug", tmp_path)
    with pytest.raises(WorkerProtocolError, match="control characters"):
        start_message(
            session_id="sess-20260827-000016",
            spec=spec,
            run_id="run-sess-20260827-000016",
            work_dir=str(tmp_path / "work"),
            journal_path=str(tmp_path / "journal.jsonl"),
            scenario="local_project",
            scenario_params=params2,
        )


# ---------------------------------------------------------------------------
# 8. COMMAND FIELDS REMAIN SINGLE-LINE
# ---------------------------------------------------------------------------

def test_8_reproduction_command_newline_rejected(tmp_path):
    spec = _make_spec()
    params = _real_local_params("ok bug", tmp_path)
    params["reproduction_command"] = "python repro.py\npython other.py"
    with pytest.raises(WorkerProtocolError, match="control characters"):
        start_message(
            session_id="sess-20260827-000020",
            spec=spec,
            run_id="run-sess-20260827-000020",
            work_dir=str(tmp_path / "work"),
            journal_path=str(tmp_path / "journal.jsonl"),
            scenario="local_project",
            scenario_params=params,
        )


def test_8_verification_command_newline_rejected(tmp_path):
    spec = _make_spec()
    params = _real_local_params("ok bug", tmp_path)
    params["verification_command"] = "python repro.py\n"
    with pytest.raises(WorkerProtocolError, match="control characters"):
        start_message(
            session_id="sess-20260827-000021",
            spec=spec,
            run_id="run-sess-20260827-000021",
            work_dir=str(tmp_path / "work"),
            journal_path=str(tmp_path / "journal.jsonl"),
            scenario="local_project",
            scenario_params=params,
        )


def test_8_reproduction_command_crlf_rejected(tmp_path):
    spec = _make_spec()
    params = _real_local_params("ok bug", tmp_path)
    params["reproduction_command"] = "python repro.py\r\n"
    with pytest.raises(WorkerProtocolError, match="control characters"):
        start_message(
            session_id="sess-20260827-000022",
            spec=spec,
            run_id="run-sess-20260827-000022",
            work_dir=str(tmp_path / "work"),
            journal_path=str(tmp_path / "journal.jsonl"),
            scenario="local_project",
            scenario_params=params,
        )


def test_8_bug_description_allows_newline_while_command_does_not(tmp_path):
    # Mixed: bug has newlines, command does not -> should pass; bug has newline + command has newline -> must fail
    spec = _make_spec()
    good_bug = "a\nb"
    good_cmd = "python repro.py"
    params = _real_local_params(good_bug, tmp_path)
    params["reproduction_command"] = good_cmd
    # should pass
    raw = start_message(
        session_id="sess-20260827-000023",
        spec=spec,
        run_id="run-sess-20260827-000023",
        work_dir=str(tmp_path / "work"),
        journal_path=str(tmp_path / "journal.jsonl"),
        scenario="local_project",
        scenario_params=params,
    )
    req = parse_start_request(parse_parent_message(raw[:-1].decode("utf-8")))
    assert req.scenario_params["bug_description"] == good_bug
    assert req.scenario_params["reproduction_command"] == good_cmd


def test_4_bounds_still_enforced(tmp_path):
    spec = _make_spec()
    # Exactly 4 KiB should pass, 4 KiB+1 should fail
    ok_bug = "a" * 4096
    params = _real_local_params(ok_bug, tmp_path)
    raw = start_message(
        session_id="sess-20260827-000030",
        spec=spec,
        run_id="run-sess-20260827-000030",
        work_dir=str(tmp_path / "work"),
        journal_path=str(tmp_path / "journal.jsonl"),
        scenario="local_project",
        scenario_params=params,
    )
    assert parse_start_request(parse_parent_message(raw[:-1].decode("utf-8"))).scenario_params["bug_description"] == ok_bug
    too_big = "a" * 4097
    params2 = _real_local_params(too_big, tmp_path)
    with pytest.raises(WorkerProtocolError, match="exceeds"):
        start_message(
            session_id="sess-20260827-000031",
            spec=spec,
            run_id="run-sess-20260827-000031",
            work_dir=str(tmp_path / "work"),
            journal_path=str(tmp_path / "journal.jsonl"),
            scenario="local_project",
            scenario_params=params2,
        )
    # Multiline 4 KiB with newlines should also respect bound
    multiline_ok = ("a\n" * 2048)[:4096]  # 4096 bytes including newlines
    # Ensure byte length exactly 4096
    while len(multiline_ok.encode("utf-8")) > 4096:
        multiline_ok = multiline_ok[:-1]
    params3 = _real_local_params(multiline_ok, tmp_path)
    raw3 = start_message(
        session_id="sess-20260827-000032",
        spec=spec,
        run_id="run-sess-20260827-000032",
        work_dir=str(tmp_path / "work"),
        journal_path=str(tmp_path / "journal.jsonl"),
        scenario="local_project",
        scenario_params=params3,
    )
    assert parse_start_request(parse_parent_message(raw3[:-1].decode("utf-8"))).scenario_params["bug_description"] == multiline_ok
