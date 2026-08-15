"""Task-4 observability producer tests: debugger/source/patch/diagnosis events.

Covers the application-facing producer seam (``SessionObservability``), safe
source snapshot capture, patch lifecycle normalization, pause-generation
staleness protection through the shared reducer, and journal survival of
large valid locals events.
"""

from __future__ import annotations

import hashlib

import pytest

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.events import (
    SourceKind,
    SourceSnapshotStage,
)
from agentic_debugger.application.journal import (
    JournalReadState,
    SessionEventJournal,
    read_session_journal,
)
from agentic_debugger.application.observability import (
    ObservabilityContext,
    SessionObservability,
    summarize_value_summary,
)
from agentic_debugger.application.presentation import (
    PatchStage,
    initial_session_view,
    presentation_identity,
    reduce_event,
)
from agentic_debugger.application.source_snapshots import (
    SourceSnapshotError,
    capture_source_snapshot,
)
from application_support import VALID_PATCH_SHA256, make_spec

VALID_TASK = "curated-off-by-one-002"
VALID_SESSION = "session-obs-001"
VALID_RUN = "run-obs-001"
FIXED = "2026-08-14T08:00:00Z"


def make_context(**overrides) -> ObservabilityContext:
    values = dict(
        session_id=VALID_SESSION,
        task_id=VALID_TASK,
        source_kind=SourceKind.OFFLINE_DEMO,
        run_id=VALID_RUN,
        initial_sequence=0,
    )
    values.update(overrides)
    return ObservabilityContext(**values)


def make_observability(**context_overrides) -> SessionObservability:
    return SessionObservability(make_context(**context_overrides), clock=lambda: FIXED)


def reduce_all(state, events):
    for event in events:
        state = reduce_event(state, event)
    return state


def started_view():
    state = initial_session_view(presentation_identity(make_spec()))
    from agentic_debugger.application.events import SessionEventKind
    from application_support import VALID_SPEC_FINGERPRINT, make_event

    return reduce_all(
        state,
        (
            make_event(
                SessionEventKind.SESSION_CREATED,
                {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
                sequence=0,
                session_id=VALID_SESSION,
            ),
            make_event(
                SessionEventKind.SESSION_STARTED,
                {},
                sequence=1,
                run_id=VALID_RUN,
                session_id=VALID_SESSION,
            ),
        ),
    )


class TestObservabilityContext:
    def test_recorded_kinds_rejected(self):
        with pytest.raises(ApplicationInputError):
            make_context(source_kind=SourceKind.CANONICAL_TRAJECTORY)

    def test_invalid_session_id_rejected(self):
        with pytest.raises(ApplicationInputError):
            make_context(session_id="UPPER-CASE!")

    def test_contiguous_sequences(self):
        obs = make_observability(initial_sequence=4)
        obs.location_changed("profile.py", 1, "f", 1)
        obs.patch_proposed(0, VALID_PATCH_SHA256)
        assert [e.sequence for e in obs.events()] == [4, 5]


class TestDebuggerEvents:
    def test_started_and_location(self):
        obs = make_observability()
        obs.debugger_started("profile.py", ["profile.py:12"])
        obs.location_changed("profile.py", 12, "format_display_name", 1)
        kinds = [e.event_kind.value for e in obs.events()]
        assert kinds == ["debugger.started", "debugger.location_changed"]
        assert dict(obs.events()[0].payload) == {
            "script": "profile.py",
            "breakpoints": ("profile.py:12",),
        }
        assert dict(obs.events()[1].payload) == {
            "script": "profile.py",
            "line": 12,
            "function": "format_display_name",
            "pause_generation": 1,
        }

    def test_stack_observed_from_real_result_shape(self):
        obs = make_observability()
        obs.stack_observed(
            {
                "state": "paused",
                "script": "profile.py",
                "pause_generation": 1,
                "frames": [
                    {
                        "frame_id": 0,
                        "script": "profile.py",
                        "line": 12,
                        "function": "format_display_name",
                        "is_current": True,
                    },
                    {
                        "frame_id": 1,
                        "script": "profile.py",
                        "line": 30,
                        "function": "main",
                        "is_current": False,
                    },
                ],
                "total_frames": 2,
                "truncated": False,
            }
        )
        event = obs.events()[0]
        frames = [dict(f) for f in event.payload["frames"]]
        assert frames[0] == {
            "index": 0,
            "function": "format_display_name",
            "file": "profile.py",
            "line": 12,
            "is_current": True,
        }

    def test_locals_observed_bounded_summaries(self):
        obs = make_observability()
        obs.locals_observed(
            {
                "state": "paused",
                "pause_generation": 1,
                "frame_id": 0,
                "locals": [
                    {"name": "name", "value": {"kind": "str", "type": "builtins.str", "size": 4, "items": [], "entries": [], "value": "None", "special": None, "truncated": False}},
                    {"name": "items", "value": {"kind": "list", "type": "builtins.list", "size": 3, "items": [], "entries": [], "value": None, "special": None, "truncated": False}},
                ],
                "total_count": 2,
                "truncated": False,
            }
        )
        event = obs.events()[0]
        assert [dict(item) for item in event.payload["locals"]] == [
            {"name": "name", "summary": "None"},
            {"name": "items", "summary": "<list size=3>"},
        ]

    def test_stale_stack_does_not_overwrite_newer_pause(self):
        obs = make_observability()
        obs.stack_observed(
            {
                "pause_generation": 2,
                "frames": [
                    {"frame_id": 0, "script": "profile.py", "line": 20, "function": "f", "is_current": True}
                ],
            }
        )
        obs.stack_observed(
            {
                "pause_generation": 1,
                "frames": [
                    {"frame_id": 0, "script": "profile.py", "line": 5, "function": "g", "is_current": True}
                ],
            }
        )
        view = reduce_all(started_view(), obs.events())
        # The stale generation-1 observation must not replace generation 2.
        assert view.debugger.frames[0].line == 20
        assert view.debugger.pause_generation == 2

    def test_location_change_syncs_generation(self):
        obs = make_observability()
        obs.location_changed("profile.py", 12, "f", 3)
        view = reduce_all(started_view(), obs.events())
        assert view.debugger.line == 12
        assert view.debugger.pause_generation == 3


class TestPatchLifecycle:
    def test_apply_failed_distinct_from_rejected(self):
        obs = make_observability()
        obs.patch_proposed(0, VALID_PATCH_SHA256, patch_text="--- a/x.py\n+++ b/x.py\n")
        obs.patch_rejected(1, "validation error")
        obs.patch_apply_failed(2, "hunk does not apply")
        view = reduce_all(started_view(), obs.events())
        assert view.patch_attempts[0].stage is PatchStage.PROPOSED
        assert view.patch_attempts[1].stage is PatchStage.REJECTED
        assert view.patch_attempts[2].stage is PatchStage.APPLY_FAILED
        assert view.patch_attempts[2].apply_failure_reason == "hunk does not apply"

    def test_applied_is_not_correctness(self):
        obs = make_observability()
        obs.patch_proposed(0, VALID_PATCH_SHA256)
        obs.patch_applied(0, ["profile.py"], syntax_passed=True)
        view = reduce_all(started_view(), obs.events())
        assert view.patch_attempts[0].stage is PatchStage.APPLIED
        # No verifier.completed yet: applied must never imply verified.
        assert view.patch_attempts[0].stage is not PatchStage.VERIFIED

    def test_applied_becomes_verified_only_on_completed_verifier(self):
        from agentic_debugger.application.events import SessionEventKind
        from agentic_debugger.application.verifier_observer import VerifierSessionEventAdapter
        from application_support import make_event

        obs = make_observability()
        obs.patch_proposed(0, VALID_PATCH_SHA256)
        obs.patch_applied(0, ["profile.py"], syntax_passed=True)
        verifier = VerifierSessionEventAdapter(make_context(initial_sequence=len(obs.events())), clock=lambda: FIXED)
        verifier.started()
        verifier.stage_started("prepare_workspace")
        verifier.stage_completed("prepare_workspace", "completed")
        events = obs.events() + verifier.events()
        view = reduce_all(started_view(), events)
        assert view.patch_attempts[0].stage is PatchStage.APPLIED
        # The verifier started but did not complete: not verified.
        assert view.patch_attempts[0].stage is not PatchStage.VERIFIED
        assert view.verifier_stages[0].stage.value == "prepare_workspace"

    def test_proposed_patch_text_preserved_across_lifecycle(self):
        obs = make_observability()
        obs.patch_proposed(0, VALID_PATCH_SHA256, patch_text="--- a/x.py\n+++ b/x.py\n")
        obs.patch_applied(0, ["x.py"], syntax_passed=True)
        view = reduce_all(started_view(), obs.events())
        assert view.patch_attempts[0].patch_text == "--- a/x.py\n+++ b/x.py\n"
        assert view.patch_attempts[0].patch_sha256 == VALID_PATCH_SHA256


class TestSourceSnapshots:
    def test_capture_integrity(self, tmp_path):
        module = tmp_path / "profile.py"
        content = "def f():\n    return 1\n"
        open(module, "w", encoding="utf-8", newline="").write(content)
        snapshot = capture_source_snapshot(
            tmp_path, "profile.py", SourceSnapshotStage.INITIAL
        )
        assert snapshot.sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert snapshot.text == content
        assert snapshot.line_count == 2
        assert snapshot.truncated is False
        assert snapshot.stage is SourceSnapshotStage.INITIAL

    def test_snapshot_event_preserves_line_mapping(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "profile.py").write_text("a\nb\nc\n", encoding="utf-8")
            obs = make_observability()
            snapshot = capture_source_snapshot(root, "profile.py", SourceSnapshotStage.INITIAL)
            obs.source_snapshot(snapshot)
            view = reduce_all(started_view(), obs.events())
            assert len(view.sources) == 1
            source = view.sources[0]
            assert source.path == "profile.py"
            assert source.line_count == 3
            assert source.sha256 == snapshot.sha256

    def test_traversal_and_absolute_paths_rejected(self, tmp_path):
        open(tmp_path / "ok.py", "w", encoding="utf-8", newline="").write("x")
        for bad in ("../ok.py", "C:/ok.py", "/ok.py", "a/../../ok.py", "ok.py/../ok.py"):
            with pytest.raises(SourceSnapshotError):
                capture_source_snapshot(tmp_path, bad, SourceSnapshotStage.INITIAL)

    def test_oversize_truncated_with_exact_hash(self, tmp_path):
        content = ("x" * 70000) + "\n"
        open(tmp_path / "big.py", "w", encoding="utf-8", newline="").write(content)
        snapshot = capture_source_snapshot(
            tmp_path, "big.py", SourceSnapshotStage.INITIAL, max_chars=1024
        )
        assert snapshot.truncated is True
        assert len(snapshot.text.encode("utf-8")) <= 1024
        # The hash covers the exact full file bytes even when truncated.
        assert snapshot.sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()

    def test_snapshot_immutable_after_capture(self, tmp_path):
        module = tmp_path / "profile.py"
        module.write_text("def f():\n    return 1\n", encoding="utf-8")
        obs = make_observability()
        snapshot = capture_source_snapshot(tmp_path, "profile.py", SourceSnapshotStage.INITIAL)
        obs.source_snapshot(snapshot)
        event = obs.events()[0]
        original_text = event.payload["text"]
        # Mutating the workspace after capture must not change the event.
        module.write_text("def f():\n    return 999\n", encoding="utf-8")
        assert event.payload["text"] == original_text
        assert event.payload["sha256"] == snapshot.sha256

    def test_only_declared_path_captured(self, tmp_path):
        # A hidden test/oracle file beside the target must never be captured.
        (tmp_path / "profile.py").write_text("def f(): pass\n", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_profile.py").write_text("SECRET_ORACLE=1\n", encoding="utf-8")
        snapshot = capture_source_snapshot(tmp_path, "profile.py", SourceSnapshotStage.INITIAL)
        assert "SECRET_ORACLE" not in snapshot.text
        assert snapshot.logical_path == "profile.py"

    def test_latest_snapshot_per_path(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "profile.py"
            open(path, "w", encoding="utf-8", newline="").write("v1\n")
            obs = make_observability()
            obs.source_snapshot(capture_source_snapshot(root, "profile.py", SourceSnapshotStage.INITIAL))
            open(path, "w", encoding="utf-8", newline="").write("v2\n")
            obs.source_snapshot(capture_source_snapshot(root, "profile.py", SourceSnapshotStage.APPLIED))
            view = reduce_all(started_view(), obs.events())
            assert len(view.sources) == 1
            assert view.sources[0].stage is SourceSnapshotStage.APPLIED
            assert view.sources[0].text == "v2\n"


class TestDiagnosis:
    def test_diagnosis_recorded(self):
        obs = make_observability()
        obs.diagnosis_recorded(
            text="fallback is ignored",
            file_path="profile.py",
            symbol="format_display_name",
            confidence="medium",
        )
        view = reduce_all(started_view(), obs.events())
        assert view.diagnosis is not None
        assert view.diagnosis.text == "fallback is ignored"
        assert view.diagnosis.file_path == "profile.py"

    def test_multiline_diagnosis_text_allowed(self):
        obs = make_observability()
        obs.diagnosis_recorded(text="line one\nline two", file_path=None, symbol=None, confidence=None)
        assert obs.events()[0].payload["text"] == "line one\nline two"


class TestJournalSurvival:
    def test_large_locals_survive_journal(self, tmp_path):
        from agentic_debugger.application.events import (
            SESSION_EVENT_SCHEMA_VERSION,
            SessionEvent,
            SessionEventKind,
        )
        from application_support import VALID_SPEC_FINGERPRINT, make_event

        records = [
            {"name": f"var_{index:03d}", "summary": "value-" + "x" * 280}
            for index in range(512)
        ]
        stream = (
            make_event(
                SessionEventKind.SESSION_CREATED,
                {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
                sequence=0,
                session_id=VALID_SESSION,
            ),
            make_event(
                SessionEventKind.SESSION_STARTED,
                {},
                sequence=1,
                run_id=VALID_RUN,
                session_id=VALID_SESSION,
            ),
            SessionEvent(
                schema_version=SESSION_EVENT_SCHEMA_VERSION,
                session_id=VALID_SESSION,
                task_id=VALID_TASK,
                run_id=VALID_RUN,
                sequence=2,
                timestamp_utc=FIXED,
                source_kind=SourceKind.OFFLINE_DEMO,
                event_kind=SessionEventKind.DEBUGGER_LOCALS_OBSERVED,
                controller_phase=None,
                payload={"pause_generation": 1, "locals": records},
            ),
        )
        journal = SessionEventJournal(
            tmp_path / "session.events.jsonl",
            session_id=VALID_SESSION,
            task_id=VALID_TASK,
            source_kind=SourceKind.OFFLINE_DEMO,
        )
        for event in stream:
            journal.append(event)
        journal.close()
        read = read_session_journal(tmp_path / "session.events.jsonl")
        assert read.state is JournalReadState.INTERRUPTED  # no terminal event
        assert len(read.events) == 3
        assert len(read.events[2].payload["locals"]) == 512
        assert read.events[2].payload["locals"][511]["summary"] == "value-" + "x" * 280


class TestValueSummaries:
    def test_scalar_and_container_summaries(self):
        assert summarize_value_summary({"kind": "none", "type": "builtins.NoneType"}) == "None"
        assert summarize_value_summary({"kind": "int", "type": "builtins.int", "value": 3, "size": 2}) == "3"
        assert summarize_value_summary({"kind": "str", "type": "builtins.str", "value": "abc", "size": 3}) == "abc"
        assert summarize_value_summary({"kind": "dict", "type": "builtins.dict", "size": 2}) == "<dict size=2>"
        assert summarize_value_summary({"kind": "object", "type": "module.Thing"}) == "<module.Thing>"
