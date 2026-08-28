"""Unit gates for session retry linkage through the evidence chain.

Proves the linkage is journal-authoritative: session.created carries
``retry_of_session_id`` -> the derived manifest persists it -> the history
entry exposes it.  Also gates the worker protocol round-trip and the
manifest validator's fail-closed behavior for malformed linkage values.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))

from application_support import (  # noqa: E402
    VALID_RUN_ID,
    VALID_SESSION_ID,
    VALID_SPEC_FINGERPRINT,
    VALID_TASK_ID,
    make_event,
    make_spec,
)
from agentic_debugger.application.events import (  # noqa: E402
    SessionEventKind,
    SessionStatus,
    SourceKind,
)
from agentic_debugger.application.history import (  # noqa: E402
    HistoryStore,
    SessionManifest,
    validate_manifest_mapping,
)
from agentic_debugger.application.journal import SessionEventJournal  # noqa: E402
from agentic_debugger.application.worker_protocol import (  # noqa: E402
    parse_parent_message,
    parse_start_request,
    start_message,
)


def _write_retry_session(root: Path, session_id: str, retry_of: str) -> Path:
    store = HistoryStore(root)
    directory = store.session_dir(session_id)
    directory.mkdir(parents=True)
    journal = SessionEventJournal(
        directory / "session.events.jsonl",
        session_id=session_id,
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
    )
    events = [
        make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT, "retry_of_session_id": retry_of},
            sequence=0,
            session_id=session_id,
        ),
        make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID, session_id=session_id),
        make_event(SessionEventKind.SESSION_FAILED, {"status": "failed", "termination_reason": "model_error"}, sequence=2, run_id=VALID_RUN_ID, session_id=session_id),
    ]
    for event in events:
        journal.append(event)
    journal.close()
    return directory


class TestLinkageChain:
    def test_manifest_and_entry_carry_retry_linkage(self, tmp_path: Path) -> None:
        retry_session = "sess-20260827-120000-aaaa1111"
        directory = _write_retry_session(tmp_path, VALID_SESSION_ID, retry_session)
        store = HistoryStore(tmp_path)
        entry = store.register(directory)
        assert entry.retry_of_session_id == retry_session
        import json

        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["retry_of_session_id"] == retry_session

    def test_sessions_without_linkage_stay_none(self, tmp_path: Path) -> None:
        store = HistoryStore(tmp_path)
        directory = store.session_dir(VALID_SESSION_ID)
        directory.mkdir(parents=True)
        journal = SessionEventJournal(
            directory / "session.events.jsonl",
            session_id=VALID_SESSION_ID,
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
        )
        journal.append(make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
            sequence=0,
            session_id=VALID_SESSION_ID,
        ))
        journal.close()
        entry = store.register(directory)
        assert entry.retry_of_session_id is None


class TestManifestValidator:
    def test_manifest_mapping_round_trip_with_linkage(self) -> None:
        manifest = SessionManifest(
            schema_version="app-session-manifest-v1",
            session_id=VALID_SESSION_ID,
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
            journal_sha256="a" * 64,
            retry_of_session_id="sess-20260827-120000-bbbb2222",
        )
        parsed = validate_manifest_mapping(manifest.to_mapping())
        assert parsed.retry_of_session_id == "sess-20260827-120000-bbbb2222"

    def test_missing_linkage_field_fails_closed(self) -> None:
        manifest = SessionManifest(
            schema_version="app-session-manifest-v1",
            session_id=VALID_SESSION_ID,
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
            journal_sha256="a" * 64,
        )
        mapping = manifest.to_mapping()
        del mapping["retry_of_session_id"]
        with pytest.raises(Exception):
            validate_manifest_mapping(mapping)


class TestWorkerProtocol:
    def test_start_message_round_trip_with_linkage(self) -> None:
        spec = make_spec(VALID_TASK_ID)
        raw = start_message(
            session_id=VALID_SESSION_ID,
            spec=spec,
            run_id=VALID_RUN_ID,
            work_dir="C:/tmp/work",
            journal_path="C:/tmp/journal.jsonl",
            scenario="deterministic_offline_demo",
            retry_of_session_id="sess-20260827-120000-cccc3333",
        )
        request = parse_start_request(parse_parent_message(raw.decode("utf-8")))
        assert request.retry_of_session_id == "sess-20260827-120000-cccc3333"

    def test_start_message_rejects_oversized_linkage(self) -> None:
        spec = make_spec(VALID_TASK_ID)
        with pytest.raises(Exception):
            start_message(
                session_id=VALID_SESSION_ID,
                spec=spec,
                run_id=VALID_RUN_ID,
                work_dir="C:/tmp/work",
                journal_path="C:/tmp/journal.jsonl",
                scenario="deterministic_offline_demo",
                retry_of_session_id="x" * 200,
            )
