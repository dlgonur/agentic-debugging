"""Rendering-safety unit tests for the Local Application V1 panes.

The TUI renders recorded evidence through ``rich.text.Text``.  Rich ``Text``
does not parse markup: recorded values (session ids, task ids, paths,
locals, patch text, source, summaries) must stay literal plain text, and
styling must always be supplied separately.  These tests pin that rule for
the header and every workspace pane: no style tags may leak into
``Text.plain``, bracket-shaped evidence keeps its literal brackets without
backslash escaping, and markup-looking recorded text is never interpreted
as styling.

These are pure rendering tests: they fold events through the shared reducer
and call the pane render methods directly, without a terminal.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentic_debugger.application.events import SessionEventKind, SourceKind
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    TimelineEntry,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.ui.screens import render_view_header
from agentic_debugger.ui.widgets import (
    ActivityPanel,
    DebuggerPanel,
    EvidenceState,
    PatchPanel,
    SourcePanel,
    TimelinePanel,
    VerifierPanel,
    _ACTIVITY_FILTER_KINDS,
    _KIND_STYLE,
    _highlight_source_lines,
    _entry_style,
)

from application_support import (
    VALID_RUN_ID,
    VALID_SESSION_ID,
    VALID_TASK_ID,
    make_event,
)

BRACKET_SOURCE = (
    "def recent_window(days, limit):\n"
    "    # [bold red]not markup[/]\n"
    "    return days[-limit:]\n"
)

BRACKET_PATCH = (
    "--- a/recent_window.py\n"
    "+++ b/recent_window.py\n"
    "@@ -1,3 +1,3 @@\n"
    "-def recent_window(days, limit):\n"
    "+def recent_window(days, limit=3):\n"
    " # [bold red]not markup[/]\n"
)


def make_identity() -> PresentationIdentity:
    return PresentationIdentity(
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
        session_id=VALID_SESSION_ID,
    )


def fold(events) -> object:
    view = initial_session_view(make_identity())
    for event in events:
        view = reduce_event(view, event)
    return view


def bracket_stream(session_id: str = VALID_SESSION_ID):
    """A complete stream whose recorded evidence is full of brackets and
    Rich-looking markup (all literal recorded data)."""
    return [
        make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": "a" * 64},
            sequence=0,
            session_id=session_id,
        ),
        make_event(
            SessionEventKind.SESSION_STARTED,
            {},
            sequence=1,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": "executing_tool"},
            sequence=2,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.DEBUGGER_STARTED,
            {"script": "recent_window.py", "breakpoints": ["task[0]:7", "value[x]:12"]},
            sequence=3,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.DEBUGGER_LOCATION_CHANGED,
            {
                "script": "recent_window.py",
                "line": 7,
                "function": "recent_window",
                "pause_generation": 1,
            },
            sequence=4,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.DEBUGGER_STACK_OBSERVED,
            {
                "pause_generation": 1,
                "frames": [
                    {
                        "index": 0,
                        "function": "value[x]",
                        "file": "recent_window.py",
                        "line": 7,
                        "is_current": True,
                    }
                ],
            },
            sequence=5,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.DEBUGGER_LOCALS_OBSERVED,
            {
                "pause_generation": 1,
                "locals": [
                    {"name": "task[0]", "summary": "value[x]"},
                    {
                        "name": "api_key",
                        "summary": "<redacted: credential-shaped local name>",
                    },
                ],
            },
            sequence=6,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.SOURCE_SNAPSHOT,
            {
                "path": "recent_window.py",
                "sha256": "b" * 64,
                "text": BRACKET_SOURCE,
                "line_count": 3,
                "truncated": False,
                "stage": "initial",
            },
            sequence=7,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.PATCH_PROPOSED,
            {
                "attempt_index": 0,
                "patch_sha256": "c" * 64,
                "patch_text": BRACKET_PATCH,
            },
            sequence=8,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.PATCH_APPLIED,
            {"attempt_index": 0, "changed_files": ["recent_window.py"], "syntax_passed": True},
            sequence=9,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": "verifying"},
            sequence=10,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.VERIFIER_STARTED,
            {},
            sequence=11,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.VERIFIER_STAGE_STARTED,
            {"stage": "prepare_workspace"},
            sequence=12,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.VERIFIER_STAGE_COMPLETED,
            {"stage": "prepare_workspace", "status": "completed"},
            sequence=13,
            session_id=session_id,
            run_id=VALID_RUN_ID,
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
            },
            sequence=14,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.CLEANUP_STARTED,
            {},
            sequence=15,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.CLEANUP_COMPLETED,
            {"verified": True},
            sequence=16,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.SESSION_COMPLETED,
            {"status": "succeeded", "termination_reason": "done"},
            sequence=17,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
    ]


STYLE_TAGS = ("[bold", "[dim", "[/", "[red", "[yellow", "[green", "\\[")


def assert_no_style_tags(plain: str) -> None:
    for tag in STYLE_TAGS:
        assert tag not in plain, f"style tag {tag!r} leaked into Text.plain"


class TestHeaderRendering:
    def test_live_header_plain_has_mode_but_no_markup(self):
        view = fold(bracket_stream())
        header = render_view_header(
            view, mode="LIVE", mode_style="bold white on #1f6feb"
        )
        plain = header.plain
        assert "LIVE" in plain
        assert "Return the complete recent window" in plain
        assert "deterministic offline" in plain
        assert "session-test-001" not in plain
        assert "curated-off-by-one-002" not in plain
        assert_no_style_tags(plain)

    def test_replay_header_plain_has_mode_but_no_markup(self):
        view = fold(bracket_stream())
        header = render_view_header(
            view,
            mode="REPLAY",
            mode_style="bold white on #238636",
            replay_position="28/28 events  ·  at end",
        )
        plain = header.plain
        assert "REPLAY" in plain
        assert "28/28 events" in plain
        assert "Completed" in plain
        assert "Succeeded" not in plain
        assert_no_style_tags(plain)

    def test_header_status_and_verifier_text_is_plain(self):
        view = fold(bracket_stream())
        header = render_view_header(
            view, mode="REPLAY", mode_style="bold white on #238636"
        )
        plain = header.plain
        assert "verifier: RESOLVED" in plain
        assert "cleanup verified" in plain
        assert "fail-to-pass" not in plain
        assert_no_style_tags(plain)

    def test_completed_header_can_show_baseline_invalid_verifier_outcome(self):
        view = fold(bracket_stream())
        assert view.verifier_summary is not None
        view = replace(
            view,
            verifier_summary=replace(
                view.verifier_summary,
                status="BASELINE_INVALID",
                outcome=None,
            ),
        )
        plain = render_view_header(
            view, mode="LIVE", mode_style="bold white on #1f6feb"
        ).plain
        assert "Completed" in plain
        assert "Succeeded" not in plain
        assert "verifier: BASELINE_INVALID" in plain
        assert "cleanup verified" in plain
        assert_no_style_tags(plain)

    def test_running_header_is_concise_and_keeps_phase_and_verifier_state(self):
        view = fold(bracket_stream()[:3])
        plain = render_view_header(
            view, mode="LIVE", mode_style="bold white on #1f6feb"
        ).plain
        assert "Running" in plain
        assert "Executing Tool" in plain
        assert "verifier pending" in plain
        assert "executing_tool" not in plain


class TestPaneRendering:
    def test_source_uses_filename_aware_syntax_highlighting(self):
        lines = _highlight_source_lines(
            "def answer(value):\n"
            "    # keep this boundary\n"
            "    return value + 7\n",
            "answer.py",
        )
        assert [line.plain for line in lines] == [
            "def answer(value):",
            "    # keep this boundary",
            "    return value + 7",
        ]
        styles = {str(span.style) for line in lines for span in line.spans}
        assert any("#ff7b72" in style for style in styles)  # keyword/operator
        assert any("#d2a8ff" in style for style in styles)  # function name
        assert any("#8b949e" in style for style in styles)  # comment
        assert any("#79c0ff" in style for style in styles)  # number

    def test_source_unknown_extension_falls_back_to_plain_text(self):
        lines = _highlight_source_lines("plain [text]\n", "notes.unknownext")
        assert [line.plain for line in lines] == ["plain [text]"]
        assert not lines[0].spans

    def test_debugger_pane_headings_and_evidence_are_plain(self):
        view = fold(bracket_stream())
        plain = DebuggerPanel._render_view(view).plain
        assert "Current location" in plain
        assert "Breakpoints" in plain
        assert "Locals (current recorded frame)" in plain
        assert "task[0]:7" in plain
        assert "value[x]" in plain
        assert "recent_window" in plain
        assert_no_style_tags(plain)

    def test_bracket_evidence_keeps_literal_brackets(self):
        view = fold(bracket_stream())
        debugger = DebuggerPanel._render_view(view).plain
        assert "task[0]:7" in debugger
        assert "value[x]" in debugger
        assert "\\[task" not in debugger
        assert "\\[value" not in debugger

    def test_patch_pane_evidence_and_note_are_plain(self):
        view = fold(bracket_stream())
        plain = PatchPanel._render_view(view).plain
        assert "does not mean FIXED" in plain
        assert "independent verifier only" in plain
        # The explanatory note itself is plain text: no markup tags wrap it.
        assert "[dim]Patch application only mutates" not in plain
        assert "independent verifier only.[/]" not in plain
        assert "\\[bold red]" not in plain

    def test_patch_text_markup_looking_lines_stay_literal(self):
        view = fold(bracket_stream())
        plain = PatchPanel._render_view(view).plain
        assert "[bold red]not markup[/]" in plain
        assert "\\[bold red]" not in plain
        assert "-def recent_window(days, limit):" in plain

    def test_source_pane_keeps_source_literal_never_markup(self):
        view = fold(bracket_stream())
        plain = SourcePanel._render_view(view).plain
        assert "def recent_window(days, limit):" in plain
        # Recorded source that looks like Rich markup stays literal code:
        # it is never interpreted as styling and never backslash-escaped.
        assert "[bold red]not markup[/]" in plain
        assert "\\[bold red]" not in plain
        assert "# [bold red]not markup[/]" in plain

    def test_verifier_pane_authority_note_is_plain(self):
        view = fold(bracket_stream())
        plain = VerifierPanel._render_view(view).plain
        assert "The verifier result is the correctness authority." in plain
        assert "Application completion is operational only." in plain
        assert "COMPLETED" in plain
        assert "RESOLVED" in plain
        assert_no_style_tags(plain)

    def test_verifier_pane_shows_explicit_official_execution_evidence(self):
        view = fold(bracket_stream())
        assert view.verifier_summary is not None
        view = replace(
            view,
            verifier_summary=replace(
                view.verifier_summary,
                official_test_execution_proven=True,
            ),
        )
        plain = VerifierPanel._render_view(view).plain
        assert "official tests executed" in plain
        assert "Yes" in plain

    def test_redaction_marker_remains_visible(self):
        view = fold(bracket_stream())
        plain = DebuggerPanel._render_view(view).plain
        assert "<redacted: credential-shaped local name>" in plain
        assert "redacted" in plain

    def test_activity_pane_filter_line_is_plain(self):
        view = fold(bracket_stream())
        panel = ActivityPanel()
        panel.filter = "all"
        plain = panel._render_view(view).plain
        assert "Filter: all" in plain
        assert "keys: 1..7 filter (1 = all)" in plain
        assert_no_style_tags(plain)

    def test_timeline_pane_entries_are_plain(self):
        view = fold(bracket_stream())
        panel = TimelinePanel()
        plain = panel._render_view(view).plain
        assert "#17" in plain
        assert_no_style_tags(plain)

    def test_not_recorded_state_has_no_markup(self):
        view = initial_session_view(make_identity())
        assert "NOT RECORDED" in DebuggerPanel._render_view(view).plain
        assert "NOT RECORDED" in PatchPanel._render_view(view).plain
        assert "NOT RECORDED" in VerifierPanel._render_view(view).plain
        for plain in (
            DebuggerPanel._render_view(view).plain,
            PatchPanel._render_view(view).plain,
            VerifierPanel._render_view(view).plain,
        ):
            assert_no_style_tags(plain)

    def test_live_pending_pane_rendering_has_no_replay_wording(self):
        view = initial_session_view(make_identity())

        src_plain = SourcePanel._render_view(view, evidence_state=EvidenceState.LIVE_PENDING).plain
        assert "Source evidence not available yet." in src_plain
        assert "Waiting for source evidence..." in src_plain
        assert "Waiting for the live session..." not in src_plain
        assert "replay position" not in src_plain
        assert "Advance the replay" not in src_plain

        dbg_plain = DebuggerPanel._render_view(view, evidence_state=EvidenceState.LIVE_PENDING).plain
        assert "Debugger evidence not available yet." in dbg_plain
        assert "Waiting for debugger activity..." in dbg_plain
        assert "replay position" not in dbg_plain
        assert "Advance the replay" not in dbg_plain

        patch_plain = PatchPanel._render_view(view, evidence_state=EvidenceState.LIVE_PENDING).plain
        assert "No patch attempt yet." in patch_plain
        assert "Waiting for patch generation..." in patch_plain
        assert "replay position" not in patch_plain

        verifier_plain = VerifierPanel._render_view(view, evidence_state=EvidenceState.LIVE_PENDING).plain
        assert "Verifier has not started yet." in verifier_plain
        assert "Waiting for independent verification..." in verifier_plain
        assert "replay position" not in verifier_plain

    def test_replay_pending_pane_rendering_has_advance_guidance(self):
        view = initial_session_view(make_identity())

        src_plain = SourcePanel._render_view(view, evidence_state=EvidenceState.REPLAY_PENDING).plain
        assert "Source snapshot not yet available at this replay position." in src_plain
        assert "Advance the replay to the first source event." in src_plain

        dbg_plain = DebuggerPanel._render_view(view, evidence_state=EvidenceState.REPLAY_PENDING).plain
        assert "Debugger evidence not yet available at this replay position." in dbg_plain
        assert "Advance the replay to the first debugger event." in dbg_plain

        patch_plain = PatchPanel._render_view(view, evidence_state=EvidenceState.REPLAY_PENDING).plain
        assert "Patch attempts not yet available at this replay position." in patch_plain
        assert "Advance the replay to the first patch event." in patch_plain

        verifier_plain = VerifierPanel._render_view(view, evidence_state=EvidenceState.REPLAY_PENDING).plain
        assert "Verifier evidence not yet available at this replay position." in verifier_plain
        assert "Advance the replay to the verifier events." in verifier_plain

    def test_session_absent_pane_rendering_shows_not_recorded(self):
        view = initial_session_view(make_identity())

        src_plain = SourcePanel._render_view(view, evidence_state=EvidenceState.SESSION_ABSENT).plain
        assert "No source snapshot was recorded for this session." in src_plain
        assert "NOT RECORDED" in src_plain

        dbg_plain = DebuggerPanel._render_view(view, evidence_state=EvidenceState.SESSION_ABSENT).plain
        assert "No debugger evidence was recorded for this session." in dbg_plain
        assert "NOT RECORDED" in dbg_plain

        patch_plain = PatchPanel._render_view(view, evidence_state=EvidenceState.SESSION_ABSENT).plain
        assert "No patch attempts were recorded for this session." in patch_plain
        assert "NOT RECORDED" in patch_plain

        verifier_plain = VerifierPanel._render_view(view, evidence_state=EvidenceState.SESSION_ABSENT).plain
        assert "No verifier result was recorded for this session." in verifier_plain
        assert "NOT RECORDED" in verifier_plain


class TestActivityFilterVocabularyAndStyling:
    def test_all_filters_contain_only_authoritative_session_event_kinds(self):
        all_accepted_kinds = {kind.value for kind in SessionEventKind}
        for filter_name, kinds in _ACTIVITY_FILTER_KINDS.items():
            if filter_name == "all":
                assert kinds == frozenset()
            else:
                for k in kinds:
                    assert k in all_accepted_kinds, f"Filter {filter_name} has invalid kind {k}"

    def test_controller_filter_contents(self):
        kinds = _ACTIVITY_FILTER_KINDS["controller"]
        assert SessionEventKind.CONTROLLER_STEP.value in kinds
        assert SessionEventKind.CONTROLLER_TRANSITION.value in kinds

    def test_model_filter_contents(self):
        kinds = _ACTIVITY_FILTER_KINDS["model"]
        assert SessionEventKind.MODEL_REQUEST_STARTED.value in kinds
        assert SessionEventKind.MODEL_REQUEST_COMPLETED.value in kinds
        assert SessionEventKind.MODEL_DIRECTIVE_ACCEPTED.value in kinds
        assert SessionEventKind.MODEL_DIRECTIVE_REJECTED.value in kinds
        assert SessionEventKind.MODEL_CONFIGURED.value in kinds

    def test_debugger_filter_contents(self):
        kinds = _ACTIVITY_FILTER_KINDS["debugger"]
        assert SessionEventKind.DEBUGGER_STARTED.value in kinds
        assert SessionEventKind.DEBUGGER_LOCATION_CHANGED.value in kinds
        assert SessionEventKind.DEBUGGER_STACK_OBSERVED.value in kinds
        assert SessionEventKind.DEBUGGER_LOCALS_OBSERVED.value in kinds

    def test_patch_filter_contents(self):
        kinds = _ACTIVITY_FILTER_KINDS["patch"]
        assert SessionEventKind.PATCH_PROPOSED.value in kinds
        assert SessionEventKind.PATCH_REJECTED.value in kinds
        assert SessionEventKind.PATCH_APPLY_FAILED.value in kinds
        assert SessionEventKind.PATCH_APPLIED.value in kinds
        assert SessionEventKind.PATCH_REVERTED.value in kinds
        assert SessionEventKind.SOURCE_SNAPSHOT.value in kinds
        assert SessionEventKind.DIAGNOSIS_RECORDED.value in kinds

    def test_verifier_filter_contents(self):
        kinds = _ACTIVITY_FILTER_KINDS["verifier"]
        assert SessionEventKind.VERIFIER_STARTED.value in kinds
        assert SessionEventKind.VERIFIER_STAGE_STARTED.value in kinds
        assert SessionEventKind.VERIFIER_STAGE_COMPLETED.value in kinds
        assert SessionEventKind.VERIFIER_COMPLETED.value in kinds

    def test_lifecycle_filter_contents(self):
        kinds = _ACTIVITY_FILTER_KINDS["lifecycle"]
        assert SessionEventKind.SESSION_CREATED.value in kinds
        assert SessionEventKind.SESSION_STARTED.value in kinds
        assert SessionEventKind.SESSION_STATUS_CHANGED.value in kinds
        assert SessionEventKind.SESSION_CANCEL_REQUESTED.value in kinds
        assert SessionEventKind.SESSION_COMPLETED.value in kinds
        assert SessionEventKind.SESSION_FAILED.value in kinds
        assert SessionEventKind.SESSION_CANCELLED.value in kinds
        assert SessionEventKind.CLEANUP_STARTED.value in kinds
        assert SessionEventKind.CLEANUP_COMPLETED.value in kinds
        assert SessionEventKind.ARTIFACT_WRITTEN.value in kinds

    def test_tool_event_styling(self):
        ok_entry = TimelineEntry(sequence=1, event_kind=SessionEventKind.TOOL_COMPLETED, summary="tool get_stack_summary completed (ok)")
        assert _entry_style(ok_entry) == "dark_cyan"

        err_entry = TimelineEntry(sequence=2, event_kind=SessionEventKind.TOOL_COMPLETED, summary="tool step completed (error: timeout)")
        assert _entry_style(err_entry) == "bold red"

    def test_negative_event_styling(self):
        assert _KIND_STYLE[SessionEventKind.SESSION_FAILED.value] == "bold red"
        assert _KIND_STYLE[SessionEventKind.PATCH_APPLY_FAILED.value] == "bold red"
        assert _KIND_STYLE[SessionEventKind.MODEL_DIRECTIVE_REJECTED.value] == "yellow"
        assert _KIND_STYLE[SessionEventKind.PATCH_REJECTED.value] == "yellow"
        assert _KIND_STYLE[SessionEventKind.SESSION_CANCELLED.value] == "bold yellow"
