"""Provider-free tests for the operational workstream + change preview.

Covers the LIVE-WORKSTREAM-02 contract: bounded change previews from real
unified-diff text only, semantic work units with active→completed identity,
candidate outcome truth (rejected/applied labels), late patch-body
enrichment, model-request compaction, ordering, replay determinism, and
boundedness.  No provider, no worker, no terminal.
"""

from __future__ import annotations

import pytest

from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
)
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.application.workstream import (
    ChangePreviewLimits,
    DiffLineKind,
    MAX_WORKSTREAM_ENTRIES,
    WorkstreamKind,
    WorkstreamStatus,
    apply_workstream_event,
    build_change_preview,
)

PATCH = """--- a/cookiecutter/config.py
+++ b/cookiecutter/config.py
@@ -54,6 +54,6 @@ def prompt_and_delete(config, key, no_input=False):
     value = config.get(key)

     if value is None:
-        return None
+        return ""

     return value
"""

MULTI_FILE_PATCH = """--- a/cookiecutter/config.py
+++ b/cookiecutter/config.py
@@ -1,3 +1,4 @@
 context
-old line c1
+new line c1
+new line c2
 context
--- a/cookiecutter/utils.py
+++ b/cookiecutter/utils.py
@@ -10,3 +10,3 @@
 context
-old line u1
+new line u1
 context
"""


class Stream:
    """Schema-valid canned event stream (the same shape as production)."""

    def __init__(self, source: SourceKind = SourceKind.LEVEL32_OPERATOR):
        self.source = source
        self.session_id = "sess-workstream-test"
        self.identity = PresentationIdentity(
            task_id="workstream-task", source_kind=source, session_id=self.session_id
        )
        self.view = initial_session_view(self.identity)
        self.sequence = -1

    def emit(self, kind: SessionEventKind, payload: dict) -> SessionEvent:
        self.sequence += 1
        event = SessionEvent.from_mapping(
            {
                "schema_version": SESSION_EVENT_SCHEMA_VERSION,
                "session_id": self.session_id,
                "task_id": "workstream-task",
                "run_id": "run-w",
                "sequence": self.sequence,
                "timestamp_utc": "2026-08-25T10:00:00Z",
                "source_kind": self.source.value,
                "event_kind": kind.value,
                "controller_phase": None,
                "payload": payload,
            }
        )
        self.view = reduce_event(self.view, event)
        return event


# ---------------------------------------------------------------------------
# change preview
# ---------------------------------------------------------------------------


class TestAddedDeletedFiles:
    ADDED = """--- /dev/null
+++ b/tests/test_new_behavior.py
@@ -0,0 +1,4 @@
+import pytest
+
+def test_new_behavior():
+    assert True
"""

    DELETED = """--- a/old_helper.py
+++ /dev/null
@@ -1,5 +0,0 @@
-def old_helper():
-    return "stale"
-
-
-# removed
"""

    def test_added_file_summary(self) -> None:
        preview = build_change_preview(self.ADDED)
        assert preview is not None
        assert len(preview.files) == 1
        summary = preview.files[0]
        assert summary.operation.value == "A"
        assert summary.path == "tests/test_new_behavior.py"
        assert summary.additions == 4
        assert summary.deletions == 0
        assert preview.additions == 4
        assert preview.deletions == 0

    def test_deleted_file_summary(self) -> None:
        preview = build_change_preview(self.DELETED)
        assert preview is not None
        summary = preview.files[0]
        assert summary.operation.value == "D"
        assert summary.path == "old_helper.py"
        assert summary.additions == 0
        assert summary.deletions == 5

    def test_added_and_modified_multi_file(self) -> None:
        patch = (
            "--- a/cookiecutter/config.py\n"
            "+++ b/cookiecutter/config.py\n"
            "@@ -54,6 +54,6 @@\n"
            "     value = config.get(key)\n"
            "\n"
            "     if value is None:\n"
            "-        return None\n"
            "+        return \"\"\n"
            "\n"
            "     return value\n"
            "--- /dev/null\n"
            "+++ b/tests/test_config.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+import pytest\n"
            "+\n"
            "+\n"
        )
        preview = build_change_preview(patch)
        assert preview is not None
        assert preview.multi_file
        by_path = {summary.path: summary for summary in preview.files}
        assert by_path["cookiecutter/config.py"].operation.value == "M"
        assert by_path["tests/test_config.py"].operation.value == "A"


class TestStrictHunkCounts:
    def test_underfilled_old_count_returns_none(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1,3 +1,3 @@\n"
            " line 1\n"
            "-line 2\n"
            "+line 2\n"
        )
        assert build_change_preview(patch) is None

    def test_underfilled_new_count_returns_none(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1,3 +1,3 @@\n"
            " line 1\n"
            "-line 2\n"
            " line 3\n"
        )
        assert build_change_preview(patch) is None

    def test_malformed_transition_to_next_file_returns_none(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1,3 +1,3 @@\n"
            " line 1\n"
            "-line 2\n"
            "+line 2\n"
            "--- b/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,1 +1,1 @@\n"
            "+x\n"
        )
        # The second file's hunk header declares 1/1 but only one added
        # line follows before end of patch: new count underfilled.
        assert build_change_preview(patch) is None

    def test_underfilled_hunk_then_new_hunk_returns_none(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1,3 +1,3 @@\n"
            " line 1\n"
            "-line 2\n"
            "@@ -5,1 +5,1 @@\n"
            "+x\n"
        )
        assert build_change_preview(patch) is None

    def test_exact_counts_still_parse(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1,3 +1,3 @@\n"
            " line 1\n"
            "-line 2\n"
            "+line 2\n"
            " line 3\n"
        )
        preview = build_change_preview(patch)
        assert preview is not None
        assert preview.additions == 1
        assert preview.deletions == 1




class TestChangePreview:
    def test_single_file_stats_and_hunk_lines(self) -> None:
        preview = build_change_preview(PATCH)
        assert preview is not None
        assert preview.additions == 1
        assert preview.deletions == 1
        assert preview.primary_path == "cookiecutter/config.py"
        assert len(preview.files) == 1
        kinds = [line.kind for line in preview.lines]
        assert DiffLineKind.HUNK in kinds
        assert DiffLineKind.ADDED in kinds
        assert DiffLineKind.REMOVED in kinds
        assert not preview.truncated

    def test_removed_keeps_old_lineno_and_added_keeps_new_lineno(self) -> None:
        preview = build_change_preview(PATCH)
        assert preview is not None
        removed = [line for line in preview.lines if line.kind is DiffLineKind.REMOVED]
        added = [line for line in preview.lines if line.kind is DiffLineKind.ADDED]
        assert removed[0].old_lineno is not None
        assert removed[0].new_lineno is None
        assert added[0].new_lineno is not None
        assert added[0].old_lineno is None

    def test_multi_file_summary_and_primary_is_most_recent(self) -> None:
        preview = build_change_preview(MULTI_FILE_PATCH)
        assert preview is not None
        assert len(preview.files) == 2
        assert preview.additions == 3
        assert preview.deletions == 2
        assert preview.primary_path == "cookiecutter/utils.py"
        assert preview.multi_file

    def test_line_bound_and_truncation_marker(self) -> None:
        long_patch = PATCH.replace(
            '+        return ""', "+        " + "x" * 300
        )
        preview = build_change_preview(
            long_patch, ChangePreviewLimits(max_lines=8, max_line_chars=96)
        )
        assert preview is not None
        added = [line for line in preview.lines if line.kind is DiffLineKind.ADDED]
        assert max(len(line.text) for line in added) <= 96
        assert added[0].text.endswith("…")

    def test_hunk_and_line_budgets_set_truncated(self) -> None:
        preview = build_change_preview(
            MULTI_FILE_PATCH, ChangePreviewLimits(max_lines=2, max_hunks=2)
        )
        assert preview is not None
        assert preview.truncated
        assert preview.omitted_lines >= 1
        assert len(preview.lines) <= 2

    def test_malformed_diff_fails_closed_to_none(self) -> None:
        assert build_change_preview("not a diff at all") is None
        assert build_change_preview("") is None
        assert build_change_preview("--- a/x.py\n+++ b/other.py\n@@ -1 +1 @@\n+x\n") is None

    def test_never_renders_unapproved_content(self) -> None:
        # Only diff structure is parsed: any body failing the unified-diff
        # shape yields no preview rather than a partial rendering.
        assert build_change_preview("@@ -1 +1 @@\n+orphan hunk\n") is None


# ---------------------------------------------------------------------------
# projection semantics
# ---------------------------------------------------------------------------


def _labels(stream: Stream):
    return [entry.label for entry in stream.view.workstream]


def _kinds(stream: Stream):
    return [entry.kind for entry in stream.view.workstream]


class TestWorkstreamProjection:
    def test_operation_ordering_is_chronological(self) -> None:
        stream = Stream()
        stream.emit(SessionEventKind.OPERATOR_PROGRESS, {"stage": "preparing_workspace"})
        stream.emit(
            SessionEventKind.TOOL_COMPLETED,
            {"tool_name": "get_source_window", "status": "ok", "target": "config.py:40-80"},
        )
        stream.emit(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0})
        labels = _labels(stream)
        assert labels == ["Preparing workspace", "Read source", "Model request"]

    def test_active_tool_settles_to_completed(self) -> None:
        stream = Stream()
        stream.emit(SessionEventKind.TOOL_STARTED, {"tool_name": "run_reproduction"})
        assert stream.view.workstream[-1].status is WorkstreamStatus.ACTIVE
        stream.emit(
            SessionEventKind.TOOL_COMPLETED, {"tool_name": "run_reproduction", "status": "ok"}
        )
        settled = stream.view.workstream[-1]
        assert settled.status is WorkstreamStatus.COMPLETED
        assert settled.label == "Run reproduction"

    def test_failed_tool_completion_settles_to_failed(self) -> None:
        stream = Stream()
        stream.emit(SessionEventKind.TOOL_STARTED, {"tool_name": "run_reproduction"})
        stream.emit(
            SessionEventKind.TOOL_COMPLETED, {"tool_name": "run_reproduction", "status": "error"}
        )
        assert stream.view.workstream[-1].status is WorkstreamStatus.FAILED

    def test_identical_consecutive_pdb_observations_coalesce(self) -> None:
        stream = Stream()
        payload = {"pause_generation": 1, "frames": ()}
        stream.emit(SessionEventKind.DEBUGGER_STACK_OBSERVED, payload)
        stream.emit(SessionEventKind.DEBUGGER_STACK_OBSERVED, payload)
        pdb_entries = [
            entry
            for entry in stream.view.workstream
            if entry.kind is WorkstreamKind.PDB
        ]
        assert len(pdb_entries) == 1

    def test_distinct_operations_are_preserved(self) -> None:
        stream = Stream()
        stream.emit(
            SessionEventKind.TOOL_COMPLETED,
            {"tool_name": "get_source_window", "status": "ok", "target": "config.py:1-10"},
        )
        stream.emit(
            SessionEventKind.TOOL_COMPLETED,
            {"tool_name": "get_source_window", "status": "ok", "target": "config.py:40-80"},
        )
        reads = [
            entry
            for entry in stream.view.workstream
            if entry.kind is WorkstreamKind.SOURCE_READ
        ]
        assert {entry.target for entry in reads} == {"config.py:1-10", "config.py:40-80"}

    def test_source_read_carries_structured_target(self) -> None:
        stream = Stream()
        stream.emit(
            SessionEventKind.TOOL_COMPLETED,
            {"tool_name": "get_source_window", "status": "ok", "target": "config.py:42-66"},
        )
        entry = stream.view.workstream[-1]
        assert entry.kind is WorkstreamKind.SOURCE_READ
        assert entry.target == "config.py:42-66"

    def test_pdb_observation_carries_debugger_location(self) -> None:
        stream = Stream()
        stream.emit(
            SessionEventKind.DEBUGGER_STARTED,
            {"script": "config.py", "breakpoints": ("config.py:58",)},
        )
        stream.emit(
            SessionEventKind.DEBUGGER_LOCATION_CHANGED,
            {"script": "config.py", "line": 58, "function": None, "pause_generation": 1},
        )
        stream.emit(
            SessionEventKind.DEBUGGER_STACK_OBSERVED, {"pause_generation": 1, "frames": ()}
        )
        pdb_entry = stream.view.workstream[-1]
        assert pdb_entry.kind is WorkstreamKind.PDB
        assert pdb_entry.target == "config.py:58"

    def test_model_request_settles_by_ordinal(self) -> None:
        stream = Stream()
        stream.emit(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0})
        stream.emit(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 1})
        stream.emit(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 0, "status": "ok"})
        entries = [
            entry
            for entry in stream.view.workstream
            if entry.kind is WorkstreamKind.MODEL_REQUEST
        ]
        by_ordinal = {entry.ordinal: entry for entry in entries}
        assert by_ordinal[1].status is WorkstreamStatus.COMPLETED
        assert by_ordinal[2].status is WorkstreamStatus.ACTIVE

    def test_failed_model_request_kept_with_error_kind(self) -> None:
        stream = Stream()
        stream.emit(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0})
        stream.emit(
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            {"request_index": 0, "status": "error", "error_kind": "transport", "error_message": "x"},
        )
        entry = stream.view.workstream[-1]
        assert entry.status is WorkstreamStatus.FAILED
        assert entry.detail == "transport"

    def test_settled_model_requests_retained_in_chronological_order(self) -> None:
        stream = Stream()
        for index in range(6):
            stream.emit(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": index})
            stream.emit(
                SessionEventKind.MODEL_DIRECTIVE_ACCEPTED,
                {"action_name": "get_source_window", "directive_kind": "action", "target_state": None},
            )
            stream.emit(
                SessionEventKind.MODEL_REQUEST_COMPLETED,
                {"request_index": index, "status": "ok"},
            )
        settled = [
            entry
            for entry in stream.view.workstream
            if entry.kind is WorkstreamKind.MODEL_REQUEST
            and entry.status is WorkstreamStatus.COMPLETED
        ]
        assert len(settled) == 6
        assert [entry.ordinal for entry in settled] == [1, 2, 3, 4, 5, 6]
        for entry in settled:
            assert entry.detail == "Inspect source"

    def test_change_applied_without_patch_text_lacks_preview(self) -> None:
        stream = Stream()
        stream.emit(
            SessionEventKind.PATCH_APPLIED,
            {"attempt_index": 0, "changed_files": ("config.py",), "syntax_passed": None},
        )
        entry = stream.view.workstream[-1]
        assert entry.kind is WorkstreamKind.CHANGE
        assert entry.status is WorkstreamStatus.COMPLETED
        assert entry.label == "Applied change"
        assert entry.change is None

    def test_rejected_change_is_labelled_rejected(self) -> None:
        stream = Stream()
        stream.emit(
            SessionEventKind.PATCH_REJECTED,
            {"attempt_index": 0, "rejection_reason": "context mismatch"},
        )
        entry = stream.view.workstream[-1]
        assert entry.status is WorkstreamStatus.FAILED
        assert entry.label == "Rejected change"
        assert entry.detail == "context mismatch"

    def test_in_flight_apply_enriches_proposed_unit_not_duplicate(self) -> None:
        stream = Stream(SourceKind.OFFLINE_DEMO)
        stream.emit(
            SessionEventKind.PATCH_PROPOSED,
            {"attempt_index": 0, "patch_sha256": "a" * 64, "patch_text": PATCH},
        )
        stream.emit(SessionEventKind.TOOL_STARTED, {"tool_name": "apply_patch"})
        changes = [
            entry for entry in stream.view.workstream if entry.kind is WorkstreamKind.CHANGE
        ]
        assert len(changes) == 1
        assert changes[0].detail == "applying"
        assert changes[0].status is WorkstreamStatus.ACTIVE
        assert changes[0].change is not None

    def test_applied_after_proposed_keeps_single_completed_unit(self) -> None:
        stream = Stream(SourceKind.OFFLINE_DEMO)
        stream.emit(
            SessionEventKind.PATCH_PROPOSED,
            {"attempt_index": 0, "patch_sha256": "a" * 64, "patch_text": PATCH},
        )
        stream.emit(SessionEventKind.TOOL_STARTED, {"tool_name": "apply_patch"})
        stream.emit(
            SessionEventKind.PATCH_APPLIED,
            {"attempt_index": 0, "changed_files": ("config.py",), "syntax_passed": True},
        )
        changes = [
            entry for entry in stream.view.workstream if entry.kind is WorkstreamKind.CHANGE
        ]
        assert len(changes) == 1
        assert changes[0].label == "Applied change"
        assert changes[0].status is WorkstreamStatus.COMPLETED

    def test_late_patch_body_enriches_settled_applied_unit(self) -> None:
        """The Level-32 finalization path: outcome streamed live, patch
        body recorded afterwards on the same semantic unit."""
        stream = Stream()
        stream.emit(
            SessionEventKind.PATCH_APPLIED,
            {"attempt_index": 0, "changed_files": ("config.py",), "syntax_passed": None},
        )
        assert stream.view.workstream[-1].change is None
        stream.emit(
            SessionEventKind.PATCH_PROPOSED,
            {"attempt_index": 0, "patch_sha256": "b" * 64, "patch_text": PATCH},
        )
        entry = stream.view.workstream[-1]
        assert entry.label == "Applied change"
        assert entry.status is WorkstreamStatus.COMPLETED
        assert entry.change is not None
        assert entry.change.additions == 1

    def test_late_unparseable_body_never_erases_existing_preview(self) -> None:
        stream = Stream()
        stream.emit(
            SessionEventKind.PATCH_PROPOSED,
            {"attempt_index": 0, "patch_sha256": "a" * 64, "patch_text": PATCH},
        )
        stream.emit(
            SessionEventKind.PATCH_PROPOSED,
            {"attempt_index": 0, "patch_sha256": "b" * 64, "patch_text": "garbage"},
        )
        entry = stream.view.workstream[-1]
        assert entry.change is not None

    def test_rejected_then_second_attempt_stays_distinct(self) -> None:
        stream = Stream()
        stream.emit(
            SessionEventKind.PATCH_REJECTED,
            {"attempt_index": 0, "rejection_reason": "bad context"},
        )
        stream.emit(SessionEventKind.TOOL_STARTED, {"tool_name": "apply_patch"})
        stream.emit(
            SessionEventKind.PATCH_APPLIED,
            {"attempt_index": 1, "changed_files": ("config.py",), "syntax_passed": None},
        )
        changes = [
            entry for entry in stream.view.workstream if entry.kind is WorkstreamKind.CHANGE
        ]
        assert [entry.ordinal for entry in changes] == [1, 2]
        assert [entry.label for entry in changes] == [
            "Rejected change",
            "Applied change",
        ]

    def test_verifier_unit_settles_with_outcome(self) -> None:
        stream = Stream(SourceKind.OFFLINE_DEMO)
        stream.emit(SessionEventKind.VERIFIER_STARTED, {})
        stream.emit(
            SessionEventKind.VERIFIER_COMPLETED,
            {
                "status": "COMPLETED",
                "outcome": "RESOLVED",
                "f2p_passed": 1,
                "f2p_total": 1,
                "p2p_passed": 1,
                "p2p_total": 1,
                "workspace_cleaned": True,
            },
        )
        verifier = [
            entry
            for entry in stream.view.workstream
            if entry.kind is WorkstreamKind.VERIFICATION
        ]
        assert len(verifier) == 1
        assert verifier[0].status is WorkstreamStatus.COMPLETED
        assert verifier[0].detail == "resolved"

    def test_official_verification_lifecycle(self) -> None:
        stream = Stream()
        stream.emit(
            SessionEventKind.OPERATOR_PROGRESS,
            {"stage": "official_verification_preparing"},
        )
        stream.emit(
            SessionEventKind.OPERATOR_PROGRESS, {"stage": "official_evaluator_started"}
        )
        stream.emit(
            SessionEventKind.OPERATOR_PROGRESS,
            {
                "stage": "official_evaluator_completed",
                "detail": "official execution proven",
                "official_execution_proven": True,
            },
        )
        official = [
            entry
            for entry in stream.view.workstream
            if entry.kind is WorkstreamKind.OFFICIAL_VERIFICATION
        ]
        assert len(official) == 1
        assert official[0].status is WorkstreamStatus.COMPLETED
        assert official[0].detail == "execution proven"

    def test_session_failure_creates_error_unit(self) -> None:
        stream = Stream()
        stream.emit(
            SessionEventKind.SESSION_FAILED,
            {"status": "failed", "termination_reason": "model_error"},
        )
        entry = stream.view.workstream[-1]
        assert entry.kind is WorkstreamKind.ERROR
        assert entry.status is WorkstreamStatus.FAILED

    def test_unknown_facts_leave_workstream_unchanged(self) -> None:
        entries = ()
        result = apply_workstream_event(
            entries,
            event_kind="session.created",
            payload={"spec_fingerprint": "a" * 64},
            sequence=0,
            in_flight_attempt_ordinal=1,
        )
        assert result == ()

    def test_workstream_bounded(self) -> None:
        stream = Stream()
        for index in range(400):
            stream.emit(
                SessionEventKind.TOOL_COMPLETED,
                {"tool_name": f"tool_{index}", "status": "ok"},
            )
        assert len(stream.view.workstream) <= MAX_WORKSTREAM_ENTRIES


class TestScientificInvariance:
    def test_core_view_identical_with_and_without_workstream_fold(self) -> None:
        """The workstream is additive presentation state: core fields
        (status, debugger, patch attempts incl. patch bytes, verifier,
        sources, timeline) are identical with or without the fold."""
        from dataclasses import replace

        stream = Stream(SourceKind.OFFLINE_DEMO)
        stream.emit(
            SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0}
        )
        stream.emit(
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            {"request_index": 0, "status": "ok"},
        )
        stream.emit(
            SessionEventKind.TOOL_COMPLETED,
            {"tool_name": "get_source_window", "status": "ok", "target": "a.py:1-9"},
        )
        stream.emit(
            SessionEventKind.PATCH_PROPOSED,
            {"attempt_index": 0, "patch_sha256": "a" * 64, "patch_text": PATCH},
        )
        stream.emit(
            SessionEventKind.PATCH_APPLIED,
            {"attempt_index": 0, "changed_files": ("a.py",), "syntax_passed": True},
        )
        view = stream.view
        without = replace(view, workstream=())
        assert without.patch_attempts == view.patch_attempts
        assert without.patch_attempts[0].patch_text == PATCH
        assert without.timeline == view.timeline
        assert without.debugger == view.debugger
        assert without.status == view.status

    def test_replay_and_live_reductions_are_identical(self) -> None:
        import dataclasses

        def build() -> Stream:
            stream = Stream()
            stream.emit(SessionEventKind.OPERATOR_PROGRESS, {"stage": "preflight"})
            stream.emit(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0})
            stream.emit(
                SessionEventKind.MODEL_REQUEST_COMPLETED,
                {"request_index": 0, "status": "ok"},
            )
            stream.emit(
                SessionEventKind.PATCH_APPLIED,
                {"attempt_index": 0, "changed_files": ("config.py",), "syntax_passed": None},
            )
            stream.emit(
                SessionEventKind.OPERATOR_PROGRESS,
                {"stage": "official_verification_preparing"},
            )
            return stream

        assert build().view == build().view

class TestGitDiffCompatibility:
    """Repair 2: real git-diff output, DEV_NULL vs INVALID, no-newline."""

    GIT_M_PATCH = (
        "diff --git a/cookiecutter/config.py b/cookiecutter/config.py\n"
        "index 7c2f1a2..9d8e6b0 100644\n"
        "--- a/cookiecutter/config.py\n"
        "+++ b/cookiecutter/config.py\n"
        "@@ -52,6 +52,6 @@\n"
        "     value = config.get(key)\n"
        "\n"
        "     if value is None:\n"
        "-        return None\n"
        '+        return ""\n'
        "\n"
        "     return value\n"
    )

    GIT_A_PATCH = (
        "diff --git a/tests/test_new_behavior.py b/tests/test_new_behavior.py\n"
        "new file mode 100644\n"
        "index 0000000..3f2c1d4\n"
        "--- /dev/null\n"
        "+++ b/tests/test_new_behavior.py\n"
        "@@ -0,0 +1,4 @@\n"
        "+import pytest\n"
        "+\n"
        "+def test_new_behavior():\n"
        "+    assert True\n"
    )

    GIT_D_PATCH = (
        "diff --git a/old_helper.py b/old_helper.py\n"
        "deleted file mode 100644\n"
        "index 8b3a1f0..0000000\n"
        "--- a/old_helper.py\n"
        "+++ /dev/null\n"
        "@@ -1,5 +0,0 @@\n"
        "-def old_helper():\n"
        '-    return "stale"\n'
        "-\n"
        "-\n"
        "-# removed\n"
    )

    def test_modified_file_with_git_metadata(self) -> None:
        preview = build_change_preview(self.GIT_M_PATCH)
        assert preview is not None
        assert len(preview.files) == 1
        summary = preview.files[0]
        assert summary.operation.value == "M"
        assert summary.path == "cookiecutter/config.py"
        assert summary.additions == 1
        assert summary.deletions == 1

    def test_added_file_with_new_file_mode(self) -> None:
        preview = build_change_preview(self.GIT_A_PATCH)
        assert preview is not None
        summary = preview.files[0]
        assert summary.operation.value == "A"
        assert summary.path == "tests/test_new_behavior.py"
        assert summary.additions == 4
        assert summary.deletions == 0

    def test_deleted_file_with_deleted_file_mode(self) -> None:
        preview = build_change_preview(self.GIT_D_PATCH)
        assert preview is not None
        summary = preview.files[0]
        assert summary.operation.value == "D"
        assert summary.path == "old_helper.py"
        assert summary.additions == 0
        assert summary.deletions == 5

    def test_realistic_two_file_git_patch(self) -> None:
        """diff --git before BOTH sections must yield two summaries and a
        valid primary-file preview."""
        patch = self.GIT_M_PATCH + (
            "diff --git a/tests/test_config.py b/tests/test_config.py\n"
            "new file mode 100644\n"
            "index 0000000..5a1f2c3\n"
            "--- /dev/null\n"
            "+++ b/tests/test_config.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+import pytest\n"
            "+\n"
            "+\n"
        )
        preview = build_change_preview(patch)
        assert preview is not None
        assert len(preview.files) == 2
        by_path = {summary.path: summary for summary in preview.files}
        assert by_path["cookiecutter/config.py"].operation.value == "M"
        assert by_path["tests/test_config.py"].operation.value == "A"
        # primary = most recent file, with a valid preview body
        assert preview.primary_path == "tests/test_config.py"
        assert preview.lines

    def test_unknown_metadata_fails_closed(self) -> None:
        patch = (
            "diff --git a/a.py b/a.py\n"
            "similarity index 90%\n"
            "rename from a.py\n"
            "rename to b.py\n"
            "--- a/a.py\n"
            "+++ b/b.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-x\n"
            "+y\n"
        )
        assert build_change_preview(patch) is None


class TestDevNullVsInvalidPath:
    def test_dev_null_old_is_added(self) -> None:
        patch = (
            "--- /dev/null\n"
            "+++ b/tests/new.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+x\n"
        )
        preview = build_change_preview(patch)
        assert preview is not None
        assert preview.files[0].operation.value == "A"
        assert preview.files[0].path == "tests/new.py"

    def test_dev_null_new_is_deleted(self) -> None:
        patch = (
            "--- a/old.py\n"
            "+++ /dev/null\n"
            "@@ -1,1 +0,0 @@\n"
            "-x\n"
        )
        preview = build_change_preview(patch)
        assert preview is not None
        assert preview.files[0].operation.value == "D"
        assert preview.files[0].path == "old.py"

    def test_absolute_old_path_is_invalid_not_added(self) -> None:
        patch = (
            "--- /etc/passwd\n"
            "+++ b/x.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+x\n"
        )
        assert build_change_preview(patch) is None

    def test_parent_traversal_old_path_is_invalid_not_added(self) -> None:
        patch = (
            "--- ../outside.py\n"
            "+++ b/x.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+x\n"
        )
        assert build_change_preview(patch) is None

    def test_drive_letter_new_path_is_invalid(self) -> None:
        patch = (
            "--- a/x.py\n"
            "+++ C:/outside/file.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-x\n"
            "+y\n"
        )
        assert build_change_preview(patch) is None

    def test_absolute_new_path_is_invalid(self) -> None:
        patch = (
            "--- a/x.py\n"
            "+++ /etc/passwd\n"
            "@@ -1,1 +1,1 @@\n"
            "-x\n"
            "+y\n"
        )
        assert build_change_preview(patch) is None

    def test_dev_null_both_sides_is_invalid(self) -> None:
        patch = (
            "--- /dev/null\n"
            "+++ /dev/null\n"
            "@@ -0,0 +0,0 @@\n"
        )
        assert build_change_preview(patch) is None


class TestNoNewlineMarker:
    def test_valid_no_newline_marker_consumes_zero_lines(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,2 +1,2 @@\n"
            " line 1\n"
            "-line 2\n"
            "+line 2\n"
            "\\ No newline at end of file\n"
        )
        preview = build_change_preview(patch)
        assert preview is not None
        # the marker is not a displayed source line
        assert not any(line.text.startswith("No newline") for line in preview.lines)

    def test_malformed_marker_after_hunk_header_fails_closed(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,1 @@\n"
            "\\ No newline at end of file\n"
            "+x\n"
        )
        assert build_change_preview(patch) is None

    def test_marker_cannot_fill_underfilled_hunk(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,2 +1,2 @@\n"
            " line 1\n"
            "\\ No newline at end of file\n"
        )
        # marker after context is legal, but the hunk still declares 2/2
        # with only one context line consumed -> underfilled -> None
        assert build_change_preview(patch) is None

class TestHunkBodyPrecedence:
    """Hunk body records take precedence over file-header tokens."""

    def test_removed_source_line_beginning_with_dash_dash_space(self) -> None:
        patch = (
            "--- a/query.sql\n"
            "+++ b/query.sql\n"
            "@@ -1 +1 @@\n"
            "--- old comment\n"
            "+-- new comment\n"
        )
        preview = build_change_preview(patch)
        assert preview is not None
        kinds = [line.kind for line in preview.lines]
        assert DiffLineKind.REMOVED in kinds
        assert DiffLineKind.ADDED in kinds
        removed = next(line for line in preview.lines if line.kind is DiffLineKind.REMOVED)
        added = next(line for line in preview.lines if line.kind is DiffLineKind.ADDED)
        # the source line itself begins with "-- " (the diff's "-" prefix
        # is consumed by the parser)
        assert removed.text == "-- old comment"
        assert added.text == "-- new comment"

    def test_added_source_line_beginning_with_plus_plus_space(self) -> None:
        patch = (
            "--- a/query.sql\n"
            "+++ b/query.sql\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+++ new text\n"
        )
        preview = build_change_preview(patch)
        assert preview is not None
        added = next(line for line in preview.lines if line.kind is DiffLineKind.ADDED)
        assert added.text == "++ new text"

    def test_real_file_header_still_recognized_after_hunk_completes(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-x\n"
            "+y\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-a\n"
            "+b\n"
        )
        preview = build_change_preview(patch)
        assert preview is not None
        assert len(preview.files) == 2


class TestStrictSectionFlush:
    """Malformed sections must fail closed, never be silently dropped."""

    def test_header_only_section_before_another_file_returns_none(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
        )
        assert build_change_preview(patch) is None

    def test_trailing_header_only_section_returns_none(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
        )
        assert build_change_preview(patch) is None

    def test_incomplete_added_file_header_returns_none(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
            "--- /dev/null\n"
        )
        assert build_change_preview(patch) is None

    def test_incomplete_old_header_without_plus_plus_returns_none(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
            "--- a/b.py\n"
        )
        assert build_change_preview(patch) is None

    def test_leading_header_only_section_returns_none(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
        )
        assert build_change_preview(patch) is None


class TestIncompleteGitSections:
    """A trailing diff --git section must never disappear silently."""

    def test_trailing_git_separator_without_headers_returns_none(self) -> None:
        patch = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
            "diff --git a/b.py b/b.py\n"
        )
        assert build_change_preview(patch) is None

    def test_trailing_git_separator_with_metadata_but_no_headers_returns_none(self) -> None:
        patch = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
            "diff --git a/b.py b/b.py\n"
            "new file mode 100644\n"
            "index 0000000..1234567\n"
        )
        assert build_change_preview(patch) is None

    def test_git_section_completing_with_headers_is_valid(self) -> None:
        patch = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
            "diff --git a/b.py b/b.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/b.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+z\n"
        )
        preview = build_change_preview(patch)
        assert preview is not None
        assert len(preview.files) == 2

    def test_non_git_unified_diff_still_supported(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-x\n"
            "+y\n"
        )
        preview = build_change_preview(patch)
        assert preview is not None
        assert len(preview.files) == 1

class TestDevNullIncompleteHeader:
    """An old header awaiting its +++ is ALWAYS incomplete, including when
    the old side is DEV_NULL (which has no repository path yet)."""

    def test_dev_null_then_git_separator_returns_none(self) -> None:
        """Reproduced Repair-4 case: valid first file, then --- /dev/null,
        then diff --git for the next file.  The incomplete old-header
        section must fail closed instead of being silently discarded."""
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
            "--- /dev/null\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
        )
        assert build_change_preview(patch) is None

    def test_dev_null_then_another_old_header_returns_none(self) -> None:
        patch = (
            "--- /dev/null\n"
            "--- a/b.py\n"
        )
        assert build_change_preview(patch) is None

    def test_dev_null_then_another_old_header_after_valid_file_returns_none(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
            "--- /dev/null\n"
            "--- a/b.py\n"
        )
        assert build_change_preview(patch) is None

    def test_valid_added_file_still_parses(self) -> None:
        """Retained: --- /dev/null + +++ b/new.py is a valid A file."""
        patch = (
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1 @@\n"
            "+x\n"
        )
        preview = build_change_preview(patch)
        assert preview is not None
        assert len(preview.files) == 1
        assert preview.files[0].operation.value == "A"
        assert preview.files[0].path == "new.py"

    def test_incomplete_dev_null_at_eof_returns_none(self) -> None:
        """Retained: --- /dev/null at EOF fails closed."""
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
            "--- /dev/null\n"
        )
        assert build_change_preview(patch) is None

    def test_dev_null_then_metadata_without_new_header_returns_none(self) -> None:
        patch = (
            "--- a/a.py\n"
            "+++ a/a.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
            "--- /dev/null\n"
            "index 0000000..1234567\n"
        )
        assert build_change_preview(patch) is None
