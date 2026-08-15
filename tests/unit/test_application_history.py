"""Task-5 app-owned history tests: manifests, discovery, replay, adapters.

Covers strict manifest validation, atomic writes, registration/list/reopen,
honest interrupted/malformed classification, the read-only replay cursor,
replay parity with live reduction, NOT_RECORDED semantics, historical
adapters, and read-only evidence safety.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agentic_debugger.application.adapters import (
    adapt_canonical_trajectory,
    adapt_professor_trace,
    adapt_r5_evidence,
    derive_recorded_session_id,
    open_recorded_file,
)
from agentic_debugger.application.events import (
    SessionEventKind,
    SessionStatus,
    SourceKind,
)
from agentic_debugger.application.history import (
    HistoryClassification,
    HistoryError,
    HistoryInputError,
    HistoryStore,
    MANIFEST_SCHEMA_VERSION,
    SessionHistoryEntry,
    SessionManifest,
    atomic_write_json,
    validate_manifest_mapping,
)
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.application.replay import SessionReplaySource, phase_boundaries
from application_support import (
    VALID_PATCH_SHA256,
    VALID_RUN_ID,
    VALID_SPEC_FINGERPRINT,
    VALID_TASK_ID,
    make_event,
)

FIXED = "2026-08-14T08:00:00Z"

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_manifest(**overrides) -> SessionManifest:
    values = dict(
        schema_version=MANIFEST_SCHEMA_VERSION,
        session_id="sess-history-001",
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
        run_id=VALID_RUN_ID,
        started_at_utc=FIXED,
        ended_at_utc=FIXED,
        status=SessionStatus.SUCCEEDED,
        termination_reason="done",
        config_fingerprint=VALID_SPEC_FINGERPRINT,
        cleanup_verified=True,
        journal_path="session.events.jsonl",
        journal_sha256="a" * 64,
        artifacts=(),
        verifier_status="COMPLETED",
        verifier_outcome="RESOLVED",
    )
    values.update(overrides)
    return SessionManifest(**values)


def _write_complete_session(root: Path, session_id: str, *, events=None) -> Path:
    store = HistoryStore(root)
    directory = store.session_dir(session_id)
    return _write_session_journal(directory, session_id, events=events)


def _write_session_journal(directory: Path, session_id: str, *, events=None) -> Path:
    """Write the standard complete journal into ``directory`` (any location)."""
    directory.mkdir(parents=True)
    journal = SessionEventJournal(
        directory / "session.events.jsonl",
        session_id=session_id,
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
    )
    if events is None:
        events = (
            make_event(
                SessionEventKind.SESSION_CREATED,
                {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
                sequence=0,
                session_id=session_id,
            ),
            make_event(
                SessionEventKind.SESSION_STARTED,
                {},
                sequence=1,
                run_id=VALID_RUN_ID,
                session_id=session_id,
            ),
            make_event(
                SessionEventKind.SESSION_STATUS_CHANGED,
                {"status": "running", "phase": "executing_tool"},
                sequence=2,
                run_id=VALID_RUN_ID,
                session_id=session_id,
            ),
            make_event(
                SessionEventKind.CLEANUP_STARTED,
                {},
                sequence=3,
                run_id=VALID_RUN_ID,
                session_id=session_id,
            ),
            make_event(
                SessionEventKind.CLEANUP_COMPLETED,
                {"verified": True},
                sequence=4,
                run_id=VALID_RUN_ID,
                session_id=session_id,
            ),
            make_event(
                SessionEventKind.SESSION_COMPLETED,
                {"status": "succeeded", "termination_reason": "done"},
                sequence=5,
                run_id=VALID_RUN_ID,
                session_id=session_id,
            ),
        )
    for event in events:
        journal.append(event)
    journal.close()
    return directory


class TestManifest:
    def test_valid_manifest(self):
        manifest = _make_manifest()
        assert manifest.session_id == "sess-history-001"
        assert manifest.status is SessionStatus.SUCCEEDED

    def test_unknown_fields_rejected(self):
        mapping = _make_manifest().to_mapping()
        mapping["extra"] = "x"
        with pytest.raises(HistoryInputError):
            validate_manifest_mapping(mapping)

    def test_missing_fields_rejected(self):
        mapping = _make_manifest().to_mapping()
        del mapping["journal_sha256"]
        with pytest.raises(HistoryInputError):
            validate_manifest_mapping(mapping)

    def test_non_terminal_status_rejected(self):
        with pytest.raises(HistoryInputError):
            _make_manifest(status=SessionStatus.RUNNING)

    def test_incompatible_reason_rejected(self):
        with pytest.raises(HistoryInputError):
            _make_manifest(termination_reason="timeout")

    def test_reason_without_status_rejected(self):
        with pytest.raises(HistoryInputError):
            _make_manifest(status=None, termination_reason="done")

    def test_round_trip(self):
        manifest = _make_manifest()
        restored = validate_manifest_mapping(manifest.to_mapping())
        assert restored == manifest

    def test_traversal_path_rejected(self):
        with pytest.raises(HistoryInputError):
            _make_manifest(journal_path="../evil.jsonl")

    def test_bad_sha_rejected(self):
        with pytest.raises(HistoryInputError):
            _make_manifest(journal_sha256="not-a-hash")

    def test_atomic_write_creates_no_tmp_leftover(self, tmp_path):
        target = tmp_path / "manifest.json"
        atomic_write_json(target, {"a": 1})
        assert target.is_file()
        assert not (tmp_path / "manifest.json.tmp").exists()
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}

    def test_atomic_update_replaces_content(self, tmp_path):
        target = tmp_path / "manifest.json"
        atomic_write_json(target, {"a": 1})
        atomic_write_json(target, {"a": 2})
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}


class TestRegistration:
    def test_register_complete(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = _write_complete_session(tmp_path, "sess-history-001")
        entry = store.register(directory)
        assert entry.classification is HistoryClassification.COMPLETE
        assert entry.is_success is True
        assert entry.status is SessionStatus.SUCCEEDED
        assert (directory / "manifest.json").is_file()
        assert (directory / "result.json").is_file()

    def test_register_idempotent(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = _write_complete_session(tmp_path, "sess-history-001")
        store.register(directory)
        second = store.register(directory)
        assert second.classification is HistoryClassification.COMPLETE

    def test_register_directory_name_mismatch_rejected(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = _write_complete_session(tmp_path, "sess-history-001")
        # Rename the directory so the name no longer matches the journal.
        moved = directory.parent / "other-name"
        directory.rename(moved)
        with pytest.raises(HistoryInputError):
            store.register(moved)

    def test_register_empty_journal_rejected(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = store.session_dir("sess-history-001")
        directory.mkdir(parents=True)
        with pytest.raises(HistoryInputError):
            store.register(directory)


class TestRegistrationContainment:
    """BLOCKER 2: register() may mutate only an app-owned runs/ child."""

    def test_external_directory_rejected_and_untouched(self, tmp_path):
        store = HistoryStore(tmp_path)
        external = _write_session_journal(tmp_path / "external" / "sess-ext-001", "sess-ext-001")
        evidence = external / "evidence.json"
        evidence.write_text('{"frozen": true}', encoding="utf-8")
        before = evidence.read_bytes()
        with pytest.raises(HistoryInputError):
            store.register(external)
        # The external directory was never mutated: no manifest/result were
        # written and the frozen bytes are byte-for-byte untouched.
        assert not (external / "manifest.json").exists()
        assert not (external / "result.json").exists()
        assert evidence.read_bytes() == before

    def test_sibling_outside_runs_root_rejected(self, tmp_path):
        store = HistoryStore(tmp_path)
        sibling = _write_session_journal(tmp_path / "sess-sibling-001", "sess-sibling-001")
        with pytest.raises(HistoryInputError):
            store.register(sibling)
        assert not (sibling / "manifest.json").exists()

    def test_traversal_rejected(self, tmp_path):
        store = HistoryStore(tmp_path)
        # ``runs/../sess-x`` resolves outside the runs root.
        escaped = _write_session_journal(tmp_path / "sess-traversal-001", "sess-traversal-001")
        traversal = (store.runs_dir / ".." / "sess-traversal-001")
        with pytest.raises(HistoryInputError):
            store.register(traversal)
        assert not (escaped / "manifest.json").exists()

    def test_symlink_escape_rejected(self, tmp_path):
        store = HistoryStore(tmp_path)
        outside = _write_session_journal(tmp_path / "outside" / "sess-link-001", "sess-link-001")
        link = store.runs_dir / "sess-link-001"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are not permitted in this environment")
        with pytest.raises(HistoryInputError):
            store.register(link)
        assert not (outside / "manifest.json").exists()

    def test_valid_direct_child_registers(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = _write_complete_session(tmp_path, "sess-valid-001")
        entry = store.register(directory)
        assert entry.classification is HistoryClassification.COMPLETE
        assert (directory / "manifest.json").is_file()

    def test_nested_child_rejected(self, tmp_path):
        store = HistoryStore(tmp_path)
        nested = store.runs_dir / "sess-nested-001" / "inner"
        _write_session_journal(nested, "sess-nested-001")
        with pytest.raises(HistoryInputError):
            store.register(nested)


class TestManifestIntegrity:
    """BLOCKER 3: COMPLETE requires the manifest to describe the current
    authoritative journal/artifacts; stale/tampered manifests are
    INVALID_MANIFEST and reopen fails closed."""

    def _registered(self, tmp_path, session_id="sess-integrity-001"):
        store = HistoryStore(tmp_path)
        directory = _write_complete_session(tmp_path, session_id)
        store.register(directory)
        return store, directory

    def _tamper_manifest(self, directory, **changes):
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(changes)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_valid_manifest_still_complete(self, tmp_path):
        store, _ = self._registered(tmp_path)
        entry = store.reopen("sess-integrity-001").entry
        assert entry.classification is HistoryClassification.COMPLETE
        assert entry.is_success is True

    def test_journal_changed_after_registration_is_invalid(self, tmp_path):
        """Case A: a structurally valid journal record was modified."""
        store, directory = self._registered(tmp_path)
        journal_path = directory / "session.events.jsonl"
        lines = journal_path.read_text(encoding="utf-8").splitlines()
        lines[2] = lines[2].replace('"executing_tool"', '"verifying"')
        journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        entry = entries["sess-integrity-001"]
        assert entry.classification is HistoryClassification.INVALID_MANIFEST
        assert entry.is_success is False
        with pytest.raises(HistoryError):
            store.reopen("sess-integrity-001")

    def test_terminal_state_tamper_is_invalid(self, tmp_path):
        """Case B: manifest terminal status/reason no longer matches the
        journal's SUCCEEDED/DONE records."""
        store, directory = self._registered(tmp_path)
        self._tamper_manifest(
            directory, status="failed", termination_reason="controller_failed"
        )
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        assert entries["sess-integrity-001"].classification is HistoryClassification.INVALID_MANIFEST
        with pytest.raises(HistoryError):
            store.reopen("sess-integrity-001")

    def test_run_id_tamper_is_invalid(self, tmp_path):
        store, directory = self._registered(tmp_path)
        self._tamper_manifest(directory, run_id="run-tampered-999")
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        assert entries["sess-integrity-001"].classification is HistoryClassification.INVALID_MANIFEST
        with pytest.raises(HistoryError):
            store.reopen("sess-integrity-001")

    def test_verifier_metadata_tamper_is_invalid(self, tmp_path):
        store, directory = self._registered(tmp_path)
        self._tamper_manifest(directory, verifier_status="FAILED")
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        assert entries["sess-integrity-001"].classification is HistoryClassification.INVALID_MANIFEST
        with pytest.raises(HistoryError):
            store.reopen("sess-integrity-001")

    def test_artifact_hash_tamper_is_invalid(self, tmp_path):
        store, directory = self._registered(tmp_path)
        assert (directory / "result.json").is_file()
        (directory / "result.json").write_text('{"tampered": true}', encoding="utf-8")
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        assert entries["sess-integrity-001"].classification is HistoryClassification.INVALID_MANIFEST
        with pytest.raises(HistoryError):
            store.reopen("sess-integrity-001")

    def test_missing_referenced_artifact_is_invalid(self, tmp_path):
        store, directory = self._registered(tmp_path)
        (directory / "result.json").unlink()
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        assert entries["sess-integrity-001"].classification is HistoryClassification.INVALID_MANIFEST
        with pytest.raises(HistoryError):
            store.reopen("sess-integrity-001")

    def test_reopen_does_not_rewrite_tampered_manifest(self, tmp_path):
        """list/reopen never rewrite the manifest; registration is the only
        mutation action."""
        store, directory = self._registered(tmp_path)
        manifest_path = directory / "manifest.json"
        self._tamper_manifest(directory, run_id="run-tampered-999")
        tampered_bytes = manifest_path.read_bytes()
        list(store.list_sessions())
        with pytest.raises(HistoryError):
            store.reopen("sess-integrity-001")
        assert manifest_path.read_bytes() == tampered_bytes


class TestDiscovery:
    def test_list_complete_and_interrupted(self, tmp_path):
        store = HistoryStore(tmp_path)
        complete_dir = _write_complete_session(tmp_path, "sess-complete-001")
        store.register(complete_dir)
        interrupted_dir = _write_complete_session(tmp_path, "sess-interrupted-002")
        store.register(interrupted_dir)
        # Truncate the terminal event by rewriting a prefix journal.
        journal_path = interrupted_dir / "session.events.jsonl"
        lines = journal_path.read_text(encoding="utf-8").splitlines()
        journal_path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8", newline="\n")
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        assert entries["sess-complete-001"].classification is HistoryClassification.COMPLETE
        assert entries["sess-interrupted-002"].classification is HistoryClassification.INTERRUPTED
        assert entries["sess-interrupted-002"].is_success is False

    def test_malformed_never_success(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = store.session_dir("sess-malformed-003")
        directory.mkdir(parents=True)
        (directory / "session.events.jsonl").write_text("{not json}\n", encoding="utf-8")
        (directory / "manifest.json").write_text(json.dumps(_make_manifest(session_id="sess-malformed-003").to_mapping()), encoding="utf-8")
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        entry = entries["sess-malformed-003"]
        assert entry.classification is HistoryClassification.MALFORMED
        assert entry.is_success is False

    def test_invalid_manifest_never_success(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = _write_complete_session(tmp_path, "sess-badmanifest-004")
        (directory / "manifest.json").write_text("{broken", encoding="utf-8")
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        entry = entries["sess-badmanifest-004"]
        assert entry.classification is HistoryClassification.INVALID_MANIFEST
        assert entry.is_success is False

    def test_unregistered_complete_journal_not_success(self, tmp_path):
        store = HistoryStore(tmp_path)
        _write_complete_session(tmp_path, "sess-unregistered-005")
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        entry = entries["sess-unregistered-005"]
        assert entry.classification is HistoryClassification.UNREGISTERED
        assert entry.is_success is False

    def test_random_folder_not_misclassified(self, tmp_path):
        store = HistoryStore(tmp_path)
        random = store.runs_dir / "random-experiment-dir"
        random.mkdir(parents=True)
        (random / "evidence.json").write_text("{}", encoding="utf-8")
        (random / "random.txt").write_text("x", encoding="utf-8")
        assert store.list_sessions() == ()

    def test_no_recursive_scan(self, tmp_path):
        store = HistoryStore(tmp_path)
        nested = store.runs_dir / "outer" / "inner"
        nested.mkdir(parents=True)
        (nested / "session.events.jsonl").write_text("x", encoding="utf-8")
        assert store.list_sessions() == ()


class TestReadContainment:
    """BLOCKER 4 (Repair Pass 2): discovery/reopen must enforce the same
    app-owned boundary as registration; escaping symlinks are never read as
    app-owned history."""

    def _external_registered_store(self, tmp_path):
        """A fully registered external session inside a *victim* runs/ root
        via a symlink, mirroring the FirstMate reproduction."""
        victim = HistoryStore(tmp_path / "victim")
        external = HistoryStore(tmp_path / "external-store")
        external_dir = _write_session_journal(
            tmp_path / "external-store" / "runs" / "sess-link-001",
            "sess-link-001",
        )
        external.register(external_dir)
        link = victim.runs_dir / "sess-link-001"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(external_dir, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are not permitted in this environment")
        return victim, external_dir

    def test_list_never_classifies_external_symlink_evidence(self, tmp_path):
        victim, external_dir = self._external_registered_store(tmp_path)
        assert victim.list_sessions() == ()
        # No external bytes were read or modified by the listing.
        assert (external_dir / "manifest.json").is_file()

    def test_reopen_fails_closed_on_external_symlink(self, tmp_path):
        victim, external_dir = self._external_registered_store(tmp_path)
        with pytest.raises(HistoryError):
            victim.reopen("sess-link-001")
        # The external store's own view of the session is untouched.
        external = HistoryStore(tmp_path / "external-store")
        assert external.reopen("sess-link-001").entry.is_success is True

    def test_internal_symlink_to_another_session_not_listed(self, tmp_path):
        """A symlink inside runs/ pointing at another app-owned session is
        not a genuine child with its own identity and must not be listed."""
        store = HistoryStore(tmp_path)
        target = _write_complete_session(tmp_path, "sess-real-001")
        store.register(target)
        link = store.runs_dir / "sess-link-002"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are not permitted in this environment")
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        assert set(entries.keys()) == {"sess-real-001"}
        with pytest.raises(HistoryError):
            store.reopen("sess-link-002")

    def test_valid_app_owned_child_still_lists_and_reopens(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = _write_complete_session(tmp_path, "sess-valid-001")
        store.register(directory)
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        assert entries["sess-valid-001"].classification is HistoryClassification.COMPLETE
        assert store.reopen("sess-valid-001").entry.is_success is True


class TestReopen:
    def test_reopen_complete(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = _write_complete_session(tmp_path, "sess-history-001")
        store.register(directory)
        reopened = store.reopen("sess-history-001")
        assert reopened.entry.is_success is True
        assert reopened.replay.total_events == 6
        assert reopened.replay.index == 0

    def test_reopen_interrupted_preserves_prefix(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = _write_complete_session(tmp_path, "sess-interrupted-002")
        lines = (directory / "session.events.jsonl").read_text(encoding="utf-8").splitlines()
        (directory / "session.events.jsonl").write_text(
            "\n".join(lines[:2]) + "\n", encoding="utf-8", newline="\n"
        )
        store.register(directory)
        reopened = store.reopen("sess-interrupted-002")
        assert reopened.entry.classification is HistoryClassification.INTERRUPTED
        assert reopened.replay.total_events == 2

    def test_reopen_malformed_raises(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = store.session_dir("sess-malformed-003")
        directory.mkdir(parents=True)
        (directory / "session.events.jsonl").write_text("{bad", encoding="utf-8")
        (directory / "manifest.json").write_text(json.dumps(_make_manifest(session_id="sess-malformed-003").to_mapping()), encoding="utf-8")
        with pytest.raises(HistoryError):
            store.reopen("sess-malformed-003")

    def test_reopen_invalid_manifest_raises(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = _write_complete_session(tmp_path, "sess-badmanifest-004")
        (directory / "manifest.json").write_text("{broken", encoding="utf-8")
        with pytest.raises(HistoryInputError):
            store.reopen("sess-badmanifest-004")

    def test_reopen_missing_raises(self, tmp_path):
        store = HistoryStore(tmp_path)
        with pytest.raises(HistoryError):
            store.reopen("sess-missing-006")


class TestReplayCursor:
    def test_navigation(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = _write_complete_session(tmp_path, "sess-history-001")
        store.register(directory)
        replay = store.reopen("sess-history-001").replay
        assert replay.at_beginning
        first = replay.next_event()
        assert first.sequence == 0
        assert replay.index == 1
        replay.rewind()
        assert replay.at_beginning
        assert replay.next_event().sequence == 0
        replay.seek(replay.total_events)
        assert replay.at_end
        assert replay.next_event() is None
        assert replay.seek_to_sequence(3)
        assert replay.previous_event().sequence == 2
        assert replay.seek_to_sequence(99) is False

    def test_cursor_validates_contiguity(self):
        events = (
            make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0),
            make_event(SessionEventKind.SESSION_STARTED, {}, sequence=5, run_id=VALID_RUN_ID),
        )
        with pytest.raises(Exception):
            SessionReplaySource(
                events=events,
                source_kind=SourceKind.OFFLINE_DEMO,
                task_id=VALID_TASK_ID,
                session_id="session-test-001",
            )

    def test_phase_boundaries_derived(self):
        from agentic_debugger.agent.state_machine import ControllerState

        events = (
            make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0),
            make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID),
            make_event(
                SessionEventKind.SESSION_STATUS_CHANGED,
                {"status": "running", "phase": "executing_tool"},
                sequence=2,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.CONTROLLER_STEP,
                {"step_index": 0, "directive_kind": "action", "stop_reason": None},
                sequence=3,
                run_id=VALID_RUN_ID,
                controller_phase=ControllerState.REPRODUCE,
            ),
            make_event(
                SessionEventKind.CONTROLLER_STEP,
                {"step_index": 1, "directive_kind": "transition", "stop_reason": None},
                sequence=4,
                run_id=VALID_RUN_ID,
                controller_phase=ControllerState.UNDERSTAND,
            ),
        )
        boundaries = phase_boundaries(events)
        assert boundaries == (0, 2, 3, 4)


class TestEffectivePhaseBoundaries:
    """BLOCKER 6 (Repair Pass 2): phase boundaries track effective state;
    ordinary events never reset a phase to None."""

    def test_status_phase_followed_by_ordinary_events(self):
        # The FirstMate reproduction: tool.started/tool.completed do not
        # restate the session phase, so no phase transition occurs there.
        events = (
            make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0),
            make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID),
            make_event(
                SessionEventKind.SESSION_STATUS_CHANGED,
                {"status": "running", "phase": "executing_tool"},
                sequence=2,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.TOOL_STARTED,
                {"tool_name": "get_stack_summary"},
                sequence=3,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.TOOL_COMPLETED,
                {"tool_name": "get_stack_summary", "status": "ok"},
                sequence=4,
                run_id=VALID_RUN_ID,
            ),
        )
        assert phase_boundaries(events) == (0, 2)

    def test_same_controller_phase_repeated_is_not_a_boundary(self):
        from agentic_debugger.agent.state_machine import ControllerState

        events = (
            make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0),
            make_event(
                SessionEventKind.CONTROLLER_STEP,
                {"step_index": 0, "directive_kind": "action", "stop_reason": None},
                sequence=1,
                run_id=VALID_RUN_ID,
                controller_phase=ControllerState.REPRODUCE,
            ),
            make_event(
                SessionEventKind.CONTROLLER_STEP,
                {"step_index": 1, "directive_kind": "action", "stop_reason": None},
                sequence=2,
                run_id=VALID_RUN_ID,
                controller_phase=ControllerState.REPRODUCE,
            ),
        )
        assert phase_boundaries(events) == (0, 1)

    def test_actual_controller_phase_change_is_a_boundary(self):
        from agentic_debugger.agent.state_machine import ControllerState

        events = (
            make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0),
            make_event(
                SessionEventKind.CONTROLLER_STEP,
                {"step_index": 0, "directive_kind": "action", "stop_reason": None},
                sequence=1,
                run_id=VALID_RUN_ID,
                controller_phase=ControllerState.REPRODUCE,
            ),
            make_event(
                SessionEventKind.CONTROLLER_STEP,
                {"step_index": 1, "directive_kind": "transition", "stop_reason": None},
                sequence=2,
                run_id=VALID_RUN_ID,
                controller_phase=ControllerState.UNDERSTAND,
            ),
        )
        assert phase_boundaries(events) == (0, 1, 2)

    def test_actual_session_phase_change_is_a_boundary(self):
        events = (
            make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0),
            make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID),
            make_event(
                SessionEventKind.SESSION_STATUS_CHANGED,
                {"status": "running", "phase": "waiting_model"},
                sequence=2,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.SESSION_STATUS_CHANGED,
                {"status": "running", "phase": "executing_tool"},
                sequence=3,
                run_id=VALID_RUN_ID,
            ),
        )
        assert phase_boundaries(events) == (0, 2, 3)

    def test_controller_phase_none_never_resets_effective_state(self):
        from agentic_debugger.agent.state_machine import ControllerState

        events = (
            make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0),
            make_event(
                SessionEventKind.CONTROLLER_STEP,
                {"step_index": 0, "directive_kind": "action", "stop_reason": None},
                sequence=1,
                run_id=VALID_RUN_ID,
                controller_phase=ControllerState.REPRODUCE,
            ),
            # Ordinary events carry controller_phase=None; the effective
            # controller phase stays REPRODUCE.
            make_event(
                SessionEventKind.DEBUGGER_STACK_OBSERVED,
                {
                    "pause_generation": 1,
                    "frames": [
                        {"index": 0, "function": "f", "file": "x.py", "line": 1, "is_current": True}
                    ],
                },
                sequence=2,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.CONTROLLER_STEP,
                {"step_index": 1, "directive_kind": "transition", "stop_reason": None},
                sequence=3,
                run_id=VALID_RUN_ID,
                controller_phase=ControllerState.UNDERSTAND,
            ),
        )
        assert phase_boundaries(events) == (0, 1, 3)

    def test_mixed_lifecycle_and_controller_stream(self):
        from agentic_debugger.agent.state_machine import ControllerState

        events = (
            make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0),
            make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID),
            make_event(
                SessionEventKind.SESSION_STATUS_CHANGED,
                {"status": "running", "phase": "executing_tool"},
                sequence=2,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.TOOL_STARTED,
                {"tool_name": "start_pdb_session"},
                sequence=3,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.DEBUGGER_LOCATION_CHANGED,
                {"script": "x.py", "line": 1, "function": "f", "pause_generation": 1},
                sequence=4,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.CONTROLLER_STEP,
                {"step_index": 0, "directive_kind": "action", "stop_reason": None},
                sequence=5,
                run_id=VALID_RUN_ID,
                controller_phase=ControllerState.REPRODUCE,
            ),
            make_event(
                SessionEventKind.CONTROLLER_STEP,
                {"step_index": 1, "directive_kind": "transition", "stop_reason": None},
                sequence=6,
                run_id=VALID_RUN_ID,
                controller_phase=ControllerState.UNDERSTAND,
            ),
            make_event(
                SessionEventKind.CLEANUP_STARTED,
                {},
                sequence=7,
                run_id=VALID_RUN_ID,
            ),
            make_event(
                SessionEventKind.SESSION_COMPLETED,
                {"status": "succeeded", "termination_reason": "done"},
                sequence=8,
                run_id=VALID_RUN_ID,
            ),
        )
        assert phase_boundaries(events) == (0, 2, 5, 6)


class TestReplayParity:
    def test_prefix_by_prefix_live_and_replay_parity(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = _write_complete_session(tmp_path, "sess-parity-001")
        store.register(directory)
        reopened = store.reopen("sess-parity-001")
        identity = PresentationIdentity(
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
            session_id="sess-parity-001",
        )
        live_view = initial_session_view(identity)
        replay_view = initial_session_view(identity)
        replay = reopened.replay
        for index in range(replay.total_events):
            event = replay.next_event()
            live_view = reduce_event(live_view, event)
            # Replay from the persisted prefix: reduce only the prefix
            # consumed so far and compare against the live view.
            prefix_view = initial_session_view(identity)
            for prefix_event in reopened.replay.events[: index + 1]:
                prefix_view = reduce_event(prefix_view, prefix_event)
            assert prefix_view == live_view
        assert replay.at_end


class TestHistoricalAdapters:
    def test_professor_trace_adapts_and_not_recorded(self):
        path = REPO_ROOT / "docs" / "professor_traces" / "r6_holdout_partial" / "professor_debug_trace_curated-off-by-one-002.json"
        source = open_recorded_file(path, clock=lambda: FIXED)
        assert source.info.format == "professor_trace"
        assert source.info.task_id == "curated-off-by-one-002"
        # Source text/locals are intentionally absent (NOT RECORDED).
        view = initial_session_view(source.identity)
        for event in source.events:
            view = reduce_event(view, event)
        assert view.sources == ()
        assert view.debugger.locals == ()
        assert source.info.sha256  # preserved provenance hash

    def test_r5_evidence_adapts(self):
        path = REPO_ROOT / "experiments" / "debugger_interaction_v2_r5" / "runs" / "R5.2-MATRIX-2026-08-11" / "curated-caller-callee-005" / "evidence.json"
        source = open_recorded_file(path, clock=lambda: FIXED)
        assert source.info.format == "r5_evidence"
        view = initial_session_view(source.identity)
        for event in source.events:
            view = reduce_event(view, event)
        assert view.verifier_summary is not None
        assert view.patch_attempts

    def test_canonical_trajectory_adapts(self):
        import io

        from agentic_debugger.agent.trajectory import project_controller_run
        from agentic_debugger.events.logger import JsonlEventLogger
        from test_application_controller_adapter import (
            adapter_for,
            run_controller,
        )

        adapter = adapter_for()
        result = run_controller(adapter)
        stream = io.StringIO()
        logger = JsonlEventLogger(result.run_id, result.task_id, stream=stream)
        for event in project_controller_run(
            result, tool_version="demo", model="demo", timestamp=FIXED
        ):
            logger.append(event)
        logger.flush()
        source = adapt_canonical_trajectory(stream.getvalue(), clock=lambda: FIXED)
        assert source.info.format == "canonical_trajectory"
        assert source.info.run_id == result.run_id
        view = initial_session_view(source.identity)
        for event in source.events:
            view = reduce_event(view, event)
        assert view.controller_phase.value == "Done"

    def test_adapters_never_modify_source(self, tmp_path):
        import shutil

        source_file = REPO_ROOT / "docs" / "professor_traces" / "r6_holdout_partial" / "professor_debug_trace_curated-off-by-one-002.json"
        copy = tmp_path / "trace_copy.json"
        shutil.copy2(source_file, copy)
        before = copy.read_bytes()
        open_recorded_file(copy, clock=lambda: FIXED)
        assert copy.read_bytes() == before

    def test_derive_recorded_session_id_preserves_valid_run_id(self):
        assert derive_recorded_session_id("run-abc-001", "task-x", "") == "run-abc-001"
        derived = derive_recorded_session_id("NOT A VALID ID!", "task-x", "/p")
        assert derived.startswith("recorded-")

    def test_unknown_format_fails_closed(self, tmp_path):
        unknown = tmp_path / "unknown.json"
        unknown.write_text('{"schema_version": "something-else"}', encoding="utf-8")
        with pytest.raises(Exception):
            open_recorded_file(unknown)

    def test_direct_adapters_preserve_original_identifiers(self):
        mapping = {
            "schema_version": "professor_debug_trace_v1",
            "task_id": "curated-off-by-one-002",
            "diagnosis": {"model_authored": True, "text": "x"},
            "final_verification": {"verifier_status": "COMPLETED", "outcome": "RESOLVED"},
        }
        source = adapt_professor_trace(mapping, path="t.json", clock=lambda: FIXED)
        assert source.info.task_id == "curated-off-by-one-002"
        assert source.identity.task_id == "curated-off-by-one-002"
        assert source.identity.source_kind is SourceKind.EXPERIMENT_EVIDENCE

    # -- BLOCKER 5: absence is preserved, never invented --------------------

    def test_professor_null_pause_generation_not_synthesized(self):
        """Trace entries without a recorded pause generation must NOT receive
        synthesized counters; recorded generations are preserved verbatim."""
        mapping = {
            "schema_version": "professor_debug_trace_v1",
            "task_id": "curated-off-by-one-002",
            "debugger_trace": [
                {
                    "turn": 1,
                    "production_file": "profile.py",
                    "line": 12,
                    "function": "f",
                    # no pause_generation recorded
                },
                {
                    "turn": 2,
                    "production_file": "profile.py",
                    "line": 13,
                    "function": "f",
                    "pause_generation": 7,
                    "frames": [
                        {"function": "f", "file": "profile.py", "line": 13, "is_current": True}
                    ],
                },
                {
                    "turn": 3,
                    "production_file": "profile.py",
                    "line": 1,
                    "function": "main",
                    "pause_generation": None,
                },
            ],
        }
        source = adapt_professor_trace(mapping, path="t.json", clock=lambda: FIXED)
        generations = [
            event.payload.get("pause_generation")
            for event in source.events
            if event.event_kind.value == "debugger.location_changed"
        ]
        # No synthetic 1,2,3... counters: absent stays None, recorded stays 7.
        assert generations == [None, 7, None]

    def test_professor_null_workspace_lifecycle_is_not_recorded(self):
        mapping = {
            "schema_version": "professor_debug_trace_v1",
            "task_id": "curated-off-by-one-002",
            "final_verification": {
                "verifier_status": "COMPLETED",
                "outcome": "RESOLVED",
                # workspace_lifecycle not recorded
            },
        }
        source = adapt_professor_trace(mapping, path="t.json", clock=lambda: FIXED)
        event = next(
            e for e in source.events if e.event_kind.value == "verifier.completed"
        )
        assert event.payload["workspace_cleaned"] is None

    def test_professor_recorded_workspace_lifecycle_mapped(self):
        mapping = {
            "schema_version": "professor_debug_trace_v1",
            "task_id": "curated-off-by-one-002",
            "final_verification": {
                "verifier_status": "COMPLETED",
                "outcome": "RESOLVED",
                "workspace_lifecycle": "CLEANED",
            },
        }
        source = adapt_professor_trace(mapping, path="t.json", clock=lambda: FIXED)
        event = next(
            e for e in source.events if e.event_kind.value == "verifier.completed"
        )
        assert event.payload["workspace_cleaned"] is True

    def test_r5_empty_diagnosis_provenance_produces_no_diagnosis_event(self):
        """``diagnosis_provenance: {}`` is provenance metadata, not a recorded
        diagnosis claim; no timeline fact may be invented from it."""
        mapping = {
            "schema_version": "debugger-interaction-v2-r5-evidence",
            "diagnosis_provenance": {},
        }
        source = adapt_r5_evidence(mapping, path="t.json", clock=lambda: FIXED)
        assert all(
            event.event_kind.value != "diagnosis.recorded" for event in source.events
        )

    def test_r5_recorded_diagnosis_text_is_preserved(self):
        """A real recorded diagnosis claim (post_debug_diagnoses) maps into
        one diagnosis event with the recorded text."""
        mapping = {
            "schema_version": "debugger-interaction-v2-r5-evidence",
            "diagnosis_provenance": {"diagnosis_text_sha256": "a" * 64},
            "post_debug_diagnoses": [
                {"text": "the caller and callee use different representations", "model_call_index": 8}
            ],
        }
        source = adapt_r5_evidence(mapping, path="t.json", clock=lambda: FIXED)
        events = [e for e in source.events if e.event_kind.value == "diagnosis.recorded"]
        assert len(events) == 1
        assert events[0].payload["text"] == (
            "the caller and callee use different representations"
        )

    # -- BLOCKER 5 (Repair Pass 2): identity is never invented ---------------

    def test_professor_source_commit_never_becomes_run_id(self):
        """``run_provenance.source_commit_sha`` is source provenance; with no
        genuine run identifier the trace keeps ``run_id=None``."""
        mapping = {
            "schema_version": "professor_debug_trace_v1",
            "task_id": "curated-off-by-one-002",
            "run_provenance": {"source_commit_sha": "79c614d34e83ce1eb2c4ad6c330644f33e1bbcfe"},
            "final_verification": {"verifier_status": "COMPLETED", "outcome": "RESOLVED"},
        }
        source = adapt_professor_trace(mapping, path="t.json", clock=lambda: FIXED)
        assert source.info.run_id is None
        assert source.info.provenance_mapping()["source_commit_sha"] == (
            "79c614d34e83ce1eb2c4ad6c330644f33e1bbcfe"
        )
        # The commit must never surface as the presentation session identity.
        assert source.identity.session_id != "79c614d34e83ce1eb2c4ad6c330644f33e1bbcfe"
        assert source.identity.session_id.startswith("recorded-")

    def test_professor_traces_with_same_commit_get_distinct_session_ids(self):
        """Two different trace files/tasks sharing one source commit must not
        collapse into one presentation session."""
        common_commit = "79c614d34e83ce1eb2c4ad6c330644f33e1bbcfe"
        base = {
            "schema_version": "professor_debug_trace_v1",
            "run_provenance": {"source_commit_sha": common_commit},
            "final_verification": {"verifier_status": "COMPLETED", "outcome": "RESOLVED"},
        }
        first = adapt_professor_trace(
            {**base, "task_id": "curated-off-by-one-002"}, path="a.json", clock=lambda: FIXED
        )
        second = adapt_professor_trace(
            {**base, "task_id": "curated-none-handling-001"}, path="b.json", clock=lambda: FIXED
        )
        assert first.info.session_id != second.info.session_id
        assert first.info.run_id is None and second.info.run_id is None
        assert first.info.provenance_mapping()["source_commit_sha"] == common_commit

    def test_eight_r6_validation_traces_do_not_collapse(self):
        """All eight tracked R6 validation professor traces share the same
        source commit; they must adapt to eight distinct presentation ids."""
        trace_dir = REPO_ROOT / "docs" / "professor_traces" / "r6_validation"
        traces = sorted(trace_dir.glob("professor_debug_trace_*.json"))
        assert len(traces) == 8
        ids = set()
        for path in traces:
            source = open_recorded_file(path, clock=lambda: FIXED)
            assert source.info.run_id is None
            assert "source_commit_sha" in source.info.provenance_mapping()
            ids.add(source.identity.session_id)
        assert len(ids) == 8

    def test_r5_experiment_id_is_provenance_not_run_id(self):
        """The recorded ``run_identity.experiment_id`` names the whole
        experiment matrix; the genuine per-task run id comes from the
        embedded trajectory and is preserved verbatim."""
        path = REPO_ROOT / "experiments" / "debugger_interaction_v2_r5" / "runs" / "R5.2-MATRIX-2026-08-11" / "curated-caller-callee-005" / "evidence.json"
        source = open_recorded_file(path, clock=lambda: FIXED)
        assert source.info.run_id == "r5-curated-caller-callee-005"
        provenance = source.info.provenance_mapping()
        assert provenance["experiment_id"] == "debugger-interaction-v2-r5"
        assert provenance["source_commit_sha"]
        assert source.identity.session_id == "r5-curated-caller-callee-005"

    def test_r5_without_trajectory_keeps_run_id_none(self):
        """An R5 evidence document without an embedded trajectory records no
        genuine run identifier; experiment id stays provenance only."""
        mapping = {
            "schema_version": "debugger-interaction-v2-r5-evidence",
            "run_identity": {"experiment_id": "debugger-interaction-v2-r5"},
        }
        source = adapt_r5_evidence(mapping, path="t.json", clock=lambda: FIXED)
        assert source.info.run_id is None
        assert source.info.provenance_mapping()["experiment_id"] == "debugger-interaction-v2-r5"
        assert source.identity.session_id != "debugger-interaction-v2-r5"

    def test_canonical_genuine_run_id_preserved_verbatim(self):
        """A canonical trajectory records a genuine run id; it is preserved
        verbatim as run id and presentation session id."""
        import io

        from agentic_debugger.agent.trajectory import project_controller_run
        from agentic_debugger.events.logger import JsonlEventLogger
        from test_application_controller_adapter import (
            adapter_for,
            run_controller,
        )

        adapter = adapter_for()
        result = run_controller(adapter)
        stream = io.StringIO()
        logger = JsonlEventLogger(result.run_id, result.task_id, stream=stream)
        for event in project_controller_run(
            result, tool_version="demo", model="demo", timestamp=FIXED
        ):
            logger.append(event)
        logger.flush()
        canonical = adapt_canonical_trajectory(
            stream.getvalue(), path="c.json", clock=lambda: FIXED
        )
        assert canonical.info.run_id == result.run_id
        assert canonical.identity.session_id == result.run_id
