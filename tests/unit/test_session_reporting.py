"""Headless, read-only session report coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_debugger.application.events import SessionEventKind, SourceKind
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.application.reporting import (
    SessionReportError,
    render_session_listing,
    render_session_report,
    write_session_report,
)
from agentic_debugger.ui import __main__ as ui_cli
from application_support import (
    VALID_PATCH_SHA256,
    VALID_RUN_ID,
    VALID_SPEC_FINGERPRINT,
    VALID_TASK_ID,
    make_event,
)


SESSION_ID = "sess-report-001"


def _registered_store(
    root: Path,
    *,
    diagnosis_text: str | None = None,
) -> HistoryStore:
    store = HistoryStore(root)
    session_dir = store.session_dir(SESSION_ID)
    session_dir.mkdir(parents=True)
    journal = SessionEventJournal(
        session_dir / "session.events.jsonl",
        session_id=SESSION_ID,
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
    )
    events = (
        make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
            sequence=0,
            session_id=SESSION_ID,
        ),
        make_event(
            SessionEventKind.SESSION_STARTED,
            {},
            sequence=1,
            run_id=VALID_RUN_ID,
            session_id=SESSION_ID,
        ),
        make_event(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": "executing_tool"},
            sequence=2,
            run_id=VALID_RUN_ID,
            session_id=SESSION_ID,
        ),
        make_event(
            SessionEventKind.DIAGNOSIS_RECORDED,
            {
                "text": diagnosis_text
                or (
                    "Failure observed at "
                    f"{session_dir / 'workspace' / 'package' / 'module.py'}"
                ),
                "file_path": str(session_dir / "workspace" / "package" / "module.py"),
                "symbol": "repair_target",
                "confidence": "observed",
            },
            sequence=3,
            run_id=VALID_RUN_ID,
            session_id=SESSION_ID,
        ),
        make_event(
            SessionEventKind.PATCH_PROPOSED,
            {"attempt_index": 0, "patch_sha256": VALID_PATCH_SHA256},
            sequence=4,
            run_id=VALID_RUN_ID,
            session_id=SESSION_ID,
        ),
        make_event(
            SessionEventKind.PATCH_APPLIED,
            {
                "attempt_index": 0,
                "changed_files": ["package/module.py"],
                "syntax_passed": True,
            },
            sequence=5,
            run_id=VALID_RUN_ID,
            session_id=SESSION_ID,
        ),
        make_event(
            SessionEventKind.VERIFIER_STARTED,
            {},
            sequence=6,
            run_id=VALID_RUN_ID,
            session_id=SESSION_ID,
        ),
        make_event(
            SessionEventKind.VERIFIER_COMPLETED,
            {
                "status": "COMPLETED",
                "outcome": "RESOLVED",
                "f2p_passed": 1,
                "f2p_total": 1,
                "p2p_passed": 2,
                "p2p_total": 2,
                "workspace_cleaned": True,
                "classification": "resolved",
                "official_test_execution_proven": True,
            },
            sequence=7,
            run_id=VALID_RUN_ID,
            session_id=SESSION_ID,
        ),
        make_event(
            SessionEventKind.CLEANUP_STARTED,
            {},
            sequence=8,
            run_id=VALID_RUN_ID,
            session_id=SESSION_ID,
        ),
        make_event(
            SessionEventKind.CLEANUP_COMPLETED,
            {"verified": True},
            sequence=9,
            run_id=VALID_RUN_ID,
            session_id=SESSION_ID,
        ),
        make_event(
            SessionEventKind.SESSION_COMPLETED,
            {"status": "succeeded", "termination_reason": "done"},
            sequence=10,
            run_id=VALID_RUN_ID,
            session_id=SESSION_ID,
        ),
    )
    for event in events:
        journal.append(event)
    journal.close()
    store.register(session_dir)
    return store


def _interrupted_store(root: Path) -> HistoryStore:
    store = HistoryStore(root)
    session_dir = store.session_dir(SESSION_ID)
    session_dir.mkdir(parents=True)
    journal = SessionEventJournal(
        session_dir / "session.events.jsonl",
        session_id=SESSION_ID,
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
    )
    events = (
        make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
            sequence=0,
            session_id=SESSION_ID,
        ),
        make_event(
            SessionEventKind.SESSION_STARTED,
            {},
            sequence=1,
            run_id=VALID_RUN_ID,
            session_id=SESSION_ID,
        ),
        make_event(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": "executing_tool"},
            sequence=2,
            run_id=VALID_RUN_ID,
            session_id=SESSION_ID,
        ),
    )
    for event in events:
        journal.append(event)
    journal.close()
    store.register(session_dir)
    return store


def test_report_projects_validated_evidence_without_private_paths(tmp_path: Path) -> None:
    store = _registered_store(tmp_path)
    report = render_session_report(store.reopen(SESSION_ID))

    assert report.startswith("# Agentic Debugger Session Report\n")
    assert "History classification: complete" in report
    assert "Semantic outcome: RESOLVED" in report
    assert "Fail-to-pass: 1/1" in report
    assert "Attempt 1: stage=verified" in report
    assert "#10 `session.completed`" in report
    assert str(tmp_path) not in report
    assert "\\[session\\]" in report
    assert "Source text and patch bodies are omitted" in report


def test_listing_is_deterministic_and_discovers_session_id(tmp_path: Path) -> None:
    store = _registered_store(tmp_path)
    listing = render_session_listing(store.list_sessions())
    assert listing.splitlines()[0].startswith("SESSION ID\tCLASSIFICATION")
    assert f"{SESSION_ID}\tcomplete\tsucceeded\t{VALID_TASK_ID}\tRESOLVED" in listing


def test_interrupted_session_report_remains_honest(tmp_path: Path) -> None:
    store = _interrupted_store(tmp_path)
    report = render_session_report(store.reopen(SESSION_ID))

    assert "History classification: interrupted" in report
    assert "Application status: running" in report
    assert "Cleanup: Not recorded" in report
    assert "## Independent verification\n\nNot recorded." in report
    assert "#2 `session.status_changed`" in report


def test_report_writer_is_create_once(tmp_path: Path) -> None:
    store = _registered_store(tmp_path / "history")
    reopened = store.reopen(SESSION_ID)
    destination = tmp_path / "session-report.md"

    written = write_session_report(reopened, destination)
    assert written == destination.resolve()
    assert destination.read_text(encoding="utf-8").startswith(
        "# Agentic Debugger Session Report"
    )
    with pytest.raises(SessionReportError, match="already exists"):
        write_session_report(reopened, destination)


def test_report_fails_closed_on_recorded_credential_shape(tmp_path: Path) -> None:
    store = _registered_store(tmp_path, diagnosis_text="api_key=do-not-export")
    with pytest.raises(SessionReportError, match="credential-shaped value"):
        render_session_report(store.reopen(SESSION_ID))


def test_cli_lists_and_exports_without_launching_textual(tmp_path: Path, capsys) -> None:
    _registered_store(tmp_path)

    assert ui_cli.main(["--root", str(tmp_path), "--list-sessions"]) == 0
    assert SESSION_ID in capsys.readouterr().out

    assert (
        ui_cli.main(["--root", str(tmp_path), "--export-session", SESSION_ID])
        == 0
    )
    assert "# Agentic Debugger Session Report" in capsys.readouterr().out

    destination = tmp_path / "cli-report.md"
    assert (
        ui_cli.main(
            [
                "--root",
                str(tmp_path),
                "--export-session",
                SESSION_ID,
                "--output",
                str(destination),
            ]
        )
        == 0
    )
    assert destination.is_file()
    assert "Session report written:" in capsys.readouterr().out


def test_cli_missing_session_is_concise(tmp_path: Path, capsys) -> None:
    assert (
        ui_cli.main(
            ["--root", str(tmp_path), "--export-session", "sess-missing-001"]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: session directory does not exist" in captured.err


def test_cli_invalid_session_id_is_concise(tmp_path: Path, capsys) -> None:
    assert (
        ui_cli.main(["--root", str(tmp_path), "--export-session", "BAD/ID"])
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error:")
    assert "Traceback" not in captured.err
