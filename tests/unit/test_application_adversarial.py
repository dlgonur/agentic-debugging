"""Adversarial self-review of the Task-4/5 candidate.

Attempts to break the accepted boundaries: stale pause generations,
path traversal / absolute-path leakage, mutation after capture, oversized
data, credential-shaped values, hidden-test leakage, patch-apply-as-success,
verifier-progress-as-correctness, replay purity, adapter invention,
interrupted-as-success, malformed-manifest-as-valid, provenance mismatch,
and sequence corruption.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agentic_debugger import SchemaValidationError
from agentic_debugger.application import ApplicationContractError, ApplicationInputError
from agentic_debugger.application.adapters import adapt_professor_trace
from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
)
from agentic_debugger.application.history import (
    HistoryClassification,
    HistoryStore,
)
from agentic_debugger.application.journal import (
    SessionEventJournal,
    read_session_journal,
)
from agentic_debugger.application.observability import (
    ObservabilityContext,
    SessionObservability,
)
from agentic_debugger.application.presentation import (
    PatchStage,
    PresentationIdentity,
    current_source,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.application.source_snapshots import (
    SourceSnapshotStage,
    capture_source_snapshot,
)
from application_support import (
    VALID_PATCH_SHA256,
    VALID_RUN_ID,
    VALID_SPEC_FINGERPRINT,
    VALID_TASK_ID,
    make_event,
)

FIXED = "2026-08-14T08:00:00Z"


def make_obs(**overrides) -> SessionObservability:
    context = ObservabilityContext(
        session_id=overrides.pop("session_id", "sess-adv-001"),
        task_id=overrides.pop("task_id", VALID_TASK_ID),
        source_kind=SourceKind.OFFLINE_DEMO,
        run_id=overrides.pop("run_id", VALID_RUN_ID),
        initial_sequence=overrides.pop("initial_sequence", 0),
    )
    return SessionObservability(context, clock=lambda: FIXED)


def started_view(session_id="sess-adv-001", task_id=VALID_TASK_ID):
    view = initial_session_view(
        PresentationIdentity(task_id=task_id, source_kind=SourceKind.OFFLINE_DEMO)
    )
    return reduce_event(
        reduce_event(
            view,
            make_event(
                SessionEventKind.SESSION_CREATED,
                {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
                sequence=0,
                session_id=session_id,
                task_id=task_id,
            ),
        ),
        make_event(
            SessionEventKind.SESSION_STARTED,
            {},
            sequence=1,
            run_id=VALID_RUN_ID,
            session_id=session_id,
            task_id=task_id,
        ),
    )


class TestStaleDebuggerData:
    def test_stale_locals_cannot_overwrite_newer_pause(self):
        obs = make_obs()
        obs.locals_observed(
            {
                "pause_generation": 3,
                "locals": [{"name": "x", "value": {"kind": "int", "type": "builtins.int", "value": 3, "size": 2}}],
            }
        )
        obs.locals_observed(
            {
                "pause_generation": 2,
                "locals": [{"name": "x", "value": {"kind": "int", "type": "builtins.int", "value": 99, "size": 8}}],
            }
        )
        view = started_view()
        for event in obs.events():
            view = reduce_event(view, event)
        assert view.debugger.locals[0].summary == "3"


class TestOversizedData:
    def test_oversized_patch_text_rejected(self):
        obs = make_obs()
        with pytest.raises(Exception):
            obs.patch_proposed(0, VALID_PATCH_SHA256, patch_text="x" * 200000)

    def test_oversized_locals_rejected(self):
        obs = make_obs()
        with pytest.raises(Exception):
            obs.locals_observed(
                {
                    "pause_generation": 1,
                    "locals": [
                        {"name": f"v{i}", "value": {"kind": "int", "type": "builtins.int", "value": 1, "size": 1}}
                        for i in range(600)
                    ],
                }
            )

    def test_oversized_source_truncated_not_rejected(self, tmp_path):
        (tmp_path / "big.py").write_bytes(b"x" * (65536 * 2))
        snapshot = capture_source_snapshot(tmp_path, "big.py", SourceSnapshotStage.INITIAL)
        assert snapshot.truncated is True
        assert len(snapshot.text.encode("utf-8")) <= 65536


class TestCredentialShapes:
    def test_secret_shaped_diagnosis_file_rejected(self):
        obs = make_obs()
        with pytest.raises(Exception):
            obs.diagnosis_recorded(
                text="x", file_path="password=hunter2", symbol=None, confidence=None
            )

    def test_secret_shaped_rejection_reason_rejected(self):
        obs = make_obs()
        with pytest.raises(Exception):
            obs.patch_rejected(0, "authorization: Bearer abc123")

    def test_secret_shaped_patch_body_omitted_but_hash_retained(self):
        # Producer-side content policy: a patch body matching the shared
        # credential-shape policy is withheld from the event (never silently
        # rewritten) while the patch hash and lifecycle are retained.
        obs = make_obs()
        event = obs.patch_proposed(
            0, VALID_PATCH_SHA256, patch_text='+API_KEY="supersecret"\n'
        )
        assert "patch_text" not in event.payload
        assert event.payload["patch_sha256"] == VALID_PATCH_SHA256

    def test_harmless_patch_body_preserved(self):
        # Identifiers like token_count/secretary must never be falsely
        # rejected merely because of substring matching.
        obs = make_obs()
        text = "def f():\n    total = token_count(secretary)\n    return total\n"
        event = obs.patch_proposed(0, VALID_PATCH_SHA256, patch_text=text)
        assert event.payload["patch_text"] == text

    def test_secret_shaped_source_snapshot_withheld(self, tmp_path):
        (tmp_path / "leaky.py").write_text('API_KEY="supersecret"\n', encoding="utf-8")
        with pytest.raises(Exception):
            capture_source_snapshot(tmp_path, "leaky.py", SourceSnapshotStage.INITIAL)

    def test_secret_shaped_source_snapshot_event_rejected(self, tmp_path):
        (tmp_path / "leaky.py").write_text('password = "hunter2"\n', encoding="utf-8")
        with pytest.raises(Exception):
            capture_source_snapshot(tmp_path, "leaky.py", SourceSnapshotStage.INITIAL)

    def test_harmless_source_not_falsely_rejected(self, tmp_path):
        content = (
            "def token_count(value):\n"
            "    return len(value)\n"
            "def greet(secretary):\n"
            "    return f'hello {secretary}'\n"
        )
        (tmp_path / "ok.py").write_text(content, encoding="utf-8", newline="")
        snapshot = capture_source_snapshot(tmp_path, "ok.py", SourceSnapshotStage.INITIAL)
        assert snapshot.text == content
        assert snapshot.sha256

    # -- Repair Pass 2: quoted Python credential shapes ----------------------

    def test_quoted_dict_key_credential_shape_withheld(self, tmp_path):
        (tmp_path / "leaky.py").write_text(
            'config = {"api_key": "supersecret"}\n', encoding="utf-8"
        )
        with pytest.raises(Exception):
            capture_source_snapshot(tmp_path, "leaky.py", SourceSnapshotStage.INITIAL)

    def test_env_assignment_credential_shape_withheld(self, tmp_path):
        (tmp_path / "leaky.py").write_text(
            'os.environ["API_KEY"] = "supersecret"\n', encoding="utf-8"
        )
        with pytest.raises(Exception):
            capture_source_snapshot(tmp_path, "leaky.py", SourceSnapshotStage.INITIAL)

    def test_quoted_patch_body_credential_shape_omitted(self):
        obs = make_obs()
        event = obs.patch_proposed(
            0,
            VALID_PATCH_SHA256,
            patch_text='+os.environ["API_KEY"] = "supersecret"\n',
        )
        assert "patch_text" not in event.payload
        assert event.payload["patch_sha256"] == VALID_PATCH_SHA256

    def test_harmless_assignment_source_not_falsely_rejected(self, tmp_path):
        content = (
            "token_count = 3\n"
            "secretary = 'alice'\n"
            "password_length = 16\n"
        )
        (tmp_path / "ok.py").write_text(content, encoding="utf-8", newline="")
        snapshot = capture_source_snapshot(tmp_path, "ok.py", SourceSnapshotStage.INITIAL)
        assert snapshot.text == content

    def test_harmless_assignment_patch_body_preserved(self):
        obs = make_obs()
        text = "token_count = 3\nsecretary = 'alice'\npassword_length = 16\n"
        event = obs.patch_proposed(0, VALID_PATCH_SHA256, patch_text=text)
        assert event.payload["patch_text"] == text

    # -- Repair Pass 2: runtime-local name redaction -------------------------

    def test_credential_named_local_value_never_exposed(self):
        obs = make_obs()
        obs.locals_observed(
            {
                "pause_generation": 1,
                "locals": [
                    {
                        "name": "api_key",
                        "value": {"kind": "str", "type": "builtins.str",
                                  "value": "supersecret", "size": 11},
                    }
                ],
            }
        )
        event = obs.events()[0]
        records = [dict(item) for item in event.payload["locals"]]
        assert records[0]["name"] == "api_key"
        assert records[0]["summary"] == "<redacted: credential-shaped local name>"
        # The original secret never appears anywhere in the event mapping.
        assert "supersecret" not in json.dumps(event.to_mapping())

    def test_other_credential_names_redacted(self):
        obs = make_obs()
        names = [
            "access_token", "authorization", "credential", "password",
            "secret", "token", "API_KEY",
        ]
        obs.locals_observed(
            {
                "pause_generation": 1,
                "locals": [
                    {"name": name, "value": {"kind": "str", "type": "builtins.str",
                                             "value": "hunter2", "size": 7}}
                    for name in names
                ],
            }
        )
        event = obs.events()[0]
        for record in event.payload["locals"]:
            assert record["summary"] == "<redacted: credential-shaped local name>"

    def test_harmless_local_names_remain_visible(self):
        obs = make_obs()
        obs.locals_observed(
            {
                "pause_generation": 1,
                "locals": [
                    {"name": "token_count", "value": {"kind": "int", "type": "builtins.int", "value": 3, "size": 2}},
                    {"name": "secretary", "value": {"kind": "str", "type": "builtins.str", "value": "alice", "size": 5}},
                    {"name": "password_length", "value": {"kind": "int", "type": "builtins.int", "value": 16, "size": 2}},
                ],
            }
        )
        event = obs.events()[0]
        records = {item["name"]: item["summary"] for item in event.payload["locals"]}
        assert records["token_count"] == "3"
        assert records["secretary"] == "alice"
        assert records["password_length"] == "16"


class TestUtf8TruncationBoundary:
    """BLOCKER 3 (Repair Pass 2): every captured snapshot must fit the event
    source-text contract, including multi-byte characters at the cutoff."""

    MAX = 65536
    # One character per encoded byte width, chosen so the byte prefix ends
    # inside the character (2/3/4-byte split) or exactly at its boundary.
    CHARS = {2: "é", 3: "€", 4: "𐍈"}

    def _assert_snapshot_ok(self, obs, snapshot):
        assert len(snapshot.text.encode("utf-8")) <= self.MAX
        assert snapshot.truncated is True
        # The accepted event schema must accept the snapshot unchanged.
        event = obs.source_snapshot(snapshot)
        assert event.payload["text"] == snapshot.text

    def _capture_with_char_near_boundary(self, tmp_path, width, prefix_len):
        content = "a" * prefix_len + self.CHARS[width] + "tail\n"
        (tmp_path / "big.py").write_bytes(content.encode("utf-8"))
        snapshot = capture_source_snapshot(
            tmp_path, "big.py", SourceSnapshotStage.INITIAL
        )
        # Full-byte SHA-256 over the exact original bytes is preserved.
        assert snapshot.sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
        return snapshot

    @pytest.mark.parametrize("width", [2, 3, 4])
    def test_split_multi_byte_char_at_cutoff(self, tmp_path, width):
        # The byte prefix cuts inside the character: kept bytes end with a
        # partial character that errors="replace" would expand past the bound.
        obs = make_obs()
        snapshot = self._capture_with_char_near_boundary(
            tmp_path, width, self.MAX - (width - 1)
        )
        self._assert_snapshot_ok(obs, snapshot)
        # The retained text is a clean character prefix (either the trimmed
        # 'a' run or the single replacement character for the split tail).
        assert snapshot.text.endswith(("a", "\ufffd"))

    @pytest.mark.parametrize("width", [2, 3, 4])
    def test_complete_multi_byte_char_right_at_cutoff(self, tmp_path, width):
        # The character ends exactly at the byte cutoff; the decode must not
        # be pushed over the bound either.
        obs = make_obs()
        snapshot = self._capture_with_char_near_boundary(
            tmp_path, width, self.MAX - width
        )
        self._assert_snapshot_ok(obs, snapshot)
        assert snapshot.text.endswith(self.CHARS[width])

    def test_firstmate_reproduction(self, tmp_path):
        # The exact reproduction: 'a' * 65535 + first byte of '€' expands to
        # 65538 bytes after replace-decoding and must not be returned.
        obs = make_obs()
        content = "a" * 65535 + "€" + "x"
        (tmp_path / "big.py").write_bytes(content.encode("utf-8"))
        snapshot = capture_source_snapshot(
            tmp_path, "big.py", SourceSnapshotStage.INITIAL
        )
        self._assert_snapshot_ok(obs, snapshot)
        assert snapshot.truncated is True
        assert snapshot.sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()

    def test_invalid_bytes_mid_file_still_bounded(self, tmp_path):
        # Invalid bytes in the middle also expand under errors="replace"; the
        # retained text must still re-encode within the event bound.
        obs = make_obs()
        content = b"a" * 30000 + b"\xff\xfe" + b"b" * 35536
        (tmp_path / "big.py").write_bytes(content)
        snapshot = capture_source_snapshot(
            tmp_path, "big.py", SourceSnapshotStage.INITIAL
        )
        self._assert_snapshot_ok(obs, snapshot)
        assert snapshot.truncated is True


class TestPatchApplyNotSuccess:
    def test_applied_without_verifier_never_verified(self):
        obs = make_obs()
        obs.patch_proposed(0, VALID_PATCH_SHA256)
        obs.patch_applied(0, ["x.py"], syntax_passed=True)
        view = started_view()
        for event in obs.events():
            view = reduce_event(view, event)
        assert view.patch_attempts[0].stage is PatchStage.APPLIED
        assert view.patch_attempts[0].stage is not PatchStage.VERIFIED

    def test_failed_verifier_does_not_mark_verified(self):
        obs = make_obs()
        obs.patch_proposed(0, VALID_PATCH_SHA256)
        obs.patch_applied(0, ["x.py"], syntax_passed=True)
        from agentic_debugger.application.events import SessionEvent

        verifier_event = SessionEvent(
            schema_version=SESSION_EVENT_SCHEMA_VERSION,
            session_id="sess-adv-001",
            task_id=VALID_TASK_ID,
            run_id=VALID_RUN_ID,
            sequence=2,
            timestamp_utc=FIXED,
            source_kind=SourceKind.OFFLINE_DEMO,
            event_kind=SessionEventKind.VERIFIER_COMPLETED,
            controller_phase=None,
            payload={
                "status": "SYNTAX_FAILED",
                "outcome": None,
                "f2p_passed": 0,
                "f2p_total": 1,
                "p2p_passed": 0,
                "p2p_total": 2,
                "workspace_cleaned": True,
            },
        )
        view = started_view()
        for event in obs.events() + (verifier_event,):
            view = reduce_event(view, event)
        assert view.patch_attempts[0].stage is PatchStage.APPLIED


class TestVerifierProgressNotCorrectness:
    def test_stage_events_alone_produce_no_summary(self):
        from agentic_debugger.application.verifier_observer import VerifierSessionEventAdapter

        adapter = VerifierSessionEventAdapter(
            ObservabilityContext(
                session_id="sess-adv-001",
                task_id=VALID_TASK_ID,
                source_kind=SourceKind.OFFLINE_DEMO,
                run_id=VALID_RUN_ID,
            ),
            clock=lambda: FIXED,
        )
        adapter.started()
        adapter.stage_started("prepare_workspace")
        adapter.stage_completed("prepare_workspace", "completed")
        view = started_view()
        for event in adapter.events():
            view = reduce_event(view, event)
        assert view.verifier_summary is None
        assert view.verifier_stages  # progress is informational only


class TestReplayPurity:
    def test_replay_never_touches_filesystem(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = store.session_dir("sess-pure-001")
        directory.mkdir(parents=True)
        journal = SessionEventJournal(
            directory / "session.events.jsonl",
            session_id="sess-pure-001",
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
        )
        for event in (
            make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0, session_id="sess-pure-001"),
            make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID, session_id="sess-pure-001"),
        ):
            journal.append(event)
        journal.close()
        store.register(directory)
        reopened = store.reopen("sess-pure-001")
        assert reopened.replay.next_event().event_kind is SessionEventKind.SESSION_CREATED
        # Delete the journal and the whole session dir: the already-loaded
        # replay cursor is pure and keeps working without any executable
        # resource or file.
        (directory / "session.events.jsonl").unlink()
        assert reopened.replay.next_event().event_kind is SessionEventKind.SESSION_STARTED
        assert reopened.replay.next_event() is None


class TestProvenanceMismatch:
    def test_reducer_fails_closed_on_wrong_session(self):
        view = started_view(session_id="sess-adv-001")
        wrong = make_event(
            SessionEventKind.CONTROLLER_STEP,
            {"step_index": 0, "directive_kind": "action", "stop_reason": None},
            sequence=2,
            run_id=VALID_RUN_ID,
            session_id="different-session",
        )
        with pytest.raises(ApplicationContractError):
            reduce_event(view, wrong)

    def test_reducer_fails_closed_on_wrong_task(self):
        view = started_view(task_id=VALID_TASK_ID)
        wrong = make_event(
            SessionEventKind.CONTROLLER_STEP,
            {"step_index": 0, "directive_kind": "action", "stop_reason": None},
            sequence=2,
            run_id=VALID_RUN_ID,
            session_id="sess-adv-001",
            task_id="other-task",
        )
        with pytest.raises(ApplicationContractError):
            reduce_event(view, wrong)

    def test_tampered_manifest_identity_never_success(self, tmp_path):
        store = HistoryStore(tmp_path)
        directory = store.session_dir("sess-tamper-001")
        directory.mkdir(parents=True)
        journal = SessionEventJournal(
            directory / "session.events.jsonl",
            session_id="sess-tamper-001",
            task_id=VALID_TASK_ID,
            source_kind=SourceKind.OFFLINE_DEMO,
        )
        stream = (
            make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0, session_id="sess-tamper-001"),
            make_event(SessionEventKind.SESSION_STARTED, {}, sequence=1, run_id=VALID_RUN_ID, session_id="sess-tamper-001"),
            make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, sequence=2, run_id=VALID_RUN_ID, session_id="sess-tamper-001"),
            make_event(SessionEventKind.CLEANUP_STARTED, {}, sequence=3, run_id=VALID_RUN_ID, session_id="sess-tamper-001"),
            make_event(SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, sequence=4, run_id=VALID_RUN_ID, session_id="sess-tamper-001"),
            make_event(SessionEventKind.SESSION_COMPLETED, {"status": "succeeded", "termination_reason": "done"}, sequence=5, run_id=VALID_RUN_ID, session_id="sess-tamper-001"),
        )
        for event in stream:
            journal.append(event)
        journal.close()
        store.register(directory)
        # Tamper with the manifest identity (point it at another session).
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["session_id"] = "sess-other-999"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        entries = {entry.session_id: entry for entry in store.list_sessions()}
        assert entries["sess-tamper-001"].classification is HistoryClassification.INVALID_MANIFEST
        assert entries["sess-tamper-001"].is_success is False
        with pytest.raises(Exception):
            store.reopen("sess-tamper-001")


class TestCurrentSource:
    """BLOCKER 6: current_source() must never present the wrong file."""

    def _view_with_sources(self, sources, script):
        view = started_view()
        for source in sources:
            view = reduce_event(
                view,
                make_event(
                    SessionEventKind.SOURCE_SNAPSHOT,
                    {
                        "path": source[0],
                        "sha256": VALID_PATCH_SHA256,
                        "text": source[1],
                        "line_count": 1,
                        "truncated": False,
                        "stage": "initial",
                    },
                    sequence=2,
                    run_id=VALID_RUN_ID,
                    session_id="sess-adv-001",
                ),
            )
        if script is not None:
            view = reduce_event(
                view,
                make_event(
                    SessionEventKind.DEBUGGER_LOCATION_CHANGED,
                    {
                        "script": script,
                        "line": 1,
                        "function": "f",
                        "pause_generation": 1,
                    },
                    sequence=3,
                    run_id=VALID_RUN_ID,
                    session_id="sess-adv-001",
                ),
            )
        return view

    def test_mismatched_script_returns_none(self):
        # debugger.script = foo.py but only bar.py was snapshotted: the
        # mismatched file must NOT be presented as the current source.
        view = self._view_with_sources([("bar.py", "x\n")], script="foo.py")
        assert view.debugger.script == "foo.py"
        assert current_source(view) is None

    def test_matching_script_returns_that_snapshot(self):
        view = self._view_with_sources(
            [("bar.py", "x\n"), ("foo.py", "y\n")], script="foo.py"
        )
        source = current_source(view)
        assert source is not None
        assert source.path == "foo.py"
        assert source.text == "y\n"

    def test_no_script_returns_latest_snapshot(self):
        view = self._view_with_sources([("bar.py", "x\n")], script=None)
        source = current_source(view)
        assert source is not None
        assert source.path == "bar.py"

    def test_no_sources_returns_none(self):
        view = self._view_with_sources([], script="foo.py")
        assert current_source(view) is None


class TestAdapterNeverInvents:
    def test_professor_adapter_absent_data_stays_not_recorded(self):
        mapping = {
            "schema_version": "professor_debug_trace_v1",
            "task_id": "curated-off-by-one-002",
            "diagnosis": {"model_authored": False, "text": ""},
            "repair_attempts": [
                {
                    "attempt": 1,
                    "model_patch_raw_sha256": "abcd",  # not a real 64-hex hash
                }
            ],
            "final_verification": {"verifier_status": "COMPLETED", "outcome": "RESOLVED"},
        }
        source = adapt_professor_trace(mapping, path="t.json", clock=lambda: FIXED)
        view = initial_session_view(source.identity)
        for event in source.events:
            view = reduce_event(view, event)
        # No diagnosis (not authored), no patch (invalid recorded hash),
        # verifier summary only from the recorded final verification.
        assert view.diagnosis is None
        assert view.patch_attempts == ()
        assert view.verifier_summary is not None
        assert view.sources == ()
        assert view.debugger.locals == ()


class TestSequenceCorruption:
    def test_replay_rejects_non_contiguous_sequences(self):
        from agentic_debugger.application.replay import SessionReplaySource

        events = (
            make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0, session_id="sess-adv-001"),
            make_event(SessionEventKind.SESSION_STARTED, {}, sequence=3, run_id=VALID_RUN_ID, session_id="sess-adv-001"),
        )
        with pytest.raises(ApplicationInputError):
            SessionReplaySource(
                events=events,
                source_kind=SourceKind.OFFLINE_DEMO,
                task_id=VALID_TASK_ID,
                session_id="sess-adv-001",
            )

    def test_journal_classifies_sequence_corruption_as_malformed(self, tmp_path):
        journal_path = tmp_path / "session.events.jsonl"
        event = make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, sequence=0, session_id="sess-adv-001")
        journal_path.write_text(
            json.dumps(event.to_mapping()) + "\n" + json.dumps(event.to_mapping()) + "\n",
            encoding="utf-8",
        )
        read = read_session_journal(journal_path)
        assert read.state.value == "malformed"
