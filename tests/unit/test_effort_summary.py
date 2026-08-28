"""Unit gates for the effort projection ('what the agent tried').

The projection is a pure count over validated journal events: synthetic
streams prove each counter, the render block, and the empty-session
degradation.  A failed session with real work must never look empty.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))

from application_support import VALID_PAYLOADS, make_event  # noqa: E402
from agentic_debugger.application.effort_summary import (  # noqa: E402
    EffortSummary,
    render_effort_summary,
    summarize_events,
)
from agentic_debugger.application.events import SessionEventKind  # noqa: E402


def _stream(*pairs) -> list:
    events = []
    for index, (kind, payload) in enumerate(pairs):
        events.append(make_event(kind, payload, sequence=index))
    return events


class TestCounting:
    def test_empty_stream_renders_honestly(self) -> None:
        summary = summarize_events([])
        assert summary.event_count == 0
        text = render_effort_summary(summary)
        assert "No journal events" in text

    def test_model_request_statuses(self) -> None:
        events = _stream(
            (SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0}),
            (SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 0, "status": "ok"}),
            (SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 1}),
            (SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 1, "status": "error", "error_kind": "timeout", "error_message": "x"}),
            (SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 2}),
            (SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 2, "status": "timeout"}),
        )
        summary = summarize_events(events)
        assert summary.model_requests == 3
        assert summary.model_requests_ok == 1
        assert summary.model_requests_error == 1
        assert summary.model_requests_timeout == 1

    def test_directives_and_rejection_categories(self) -> None:
        events = _stream(
            (SessionEventKind.MODEL_DIRECTIVE_ACCEPTED, VALID_PAYLOADS[SessionEventKind.MODEL_DIRECTIVE_ACCEPTED]),
            (SessionEventKind.MODEL_DIRECTIVE_REJECTED, {"directive_kind": "action", "rejection_category": "invalid_arguments"}),
            (SessionEventKind.MODEL_DIRECTIVE_REJECTED, {"directive_kind": "transition", "rejection_category": "invalid_arguments"}),
            (SessionEventKind.MODEL_DIRECTIVE_REJECTED, {"directive_kind": "action", "rejection_category": "unknown_action"}),
        )
        summary = summarize_events(events)
        assert summary.directives_accepted == 1
        assert summary.directives_rejected == 3
        assert summary.rejection_categories == (("invalid_arguments", 2), ("unknown_action", 1))

    def test_tools_debugger_patches_states(self) -> None:
        events = _stream(
            (SessionEventKind.TOOL_STARTED, VALID_PAYLOADS[SessionEventKind.TOOL_STARTED] | {"tool_name": "find_function"}),
            (SessionEventKind.TOOL_COMPLETED, VALID_PAYLOADS[SessionEventKind.TOOL_COMPLETED] | {"tool_name": "find_function"}),
            (SessionEventKind.TOOL_STARTED, VALID_PAYLOADS[SessionEventKind.TOOL_STARTED] | {"tool_name": "run_reproduction"}),
            (SessionEventKind.TOOL_COMPLETED, VALID_PAYLOADS[SessionEventKind.TOOL_COMPLETED] | {"tool_name": "run_reproduction", "status": "error"}),
            (SessionEventKind.DEBUGGER_STARTED, VALID_PAYLOADS[SessionEventKind.DEBUGGER_STARTED]),
            (SessionEventKind.DEBUGGER_STACK_OBSERVED, VALID_PAYLOADS[SessionEventKind.DEBUGGER_STACK_OBSERVED]),
            (SessionEventKind.DEBUGGER_LOCALS_OBSERVED, VALID_PAYLOADS[SessionEventKind.DEBUGGER_LOCALS_OBSERVED]),
            (SessionEventKind.PATCH_PROPOSED, VALID_PAYLOADS[SessionEventKind.PATCH_PROPOSED]),
            (SessionEventKind.PATCH_APPLIED, VALID_PAYLOADS[SessionEventKind.PATCH_APPLIED]),
            (SessionEventKind.CONTROLLER_TRANSITION, {"source_state": "Understand", "target_state": "Reproduce", "reason": "r"}),
            (SessionEventKind.CONTROLLER_TRANSITION, {"source_state": "Reproduce", "target_state": "RuntimeEvidence", "reason": "r"}),
            (SessionEventKind.CONTROLLER_TRANSITION, {"source_state": "RuntimeEvidence", "target_state": "RuntimeEvidence", "reason": "r"}),
        )
        summary = summarize_events(events)
        assert summary.tool_calls == 2
        assert summary.tool_calls_by_name[0] == ("find_function", 1)
        assert summary.tools_failed == 1
        assert summary.debugger_sessions == 1
        assert summary.debugger_observations == 2
        assert summary.patches_proposed == 1
        assert summary.patches_applied == 1
        assert summary.states_visited == ("Reproduce", "RuntimeEvidence")

    def test_verifier_outcome_projection(self) -> None:
        events = _stream(
            (SessionEventKind.VERIFIER_COMPLETED, {
                "status": "COMPLETED", "outcome": "RESOLVED",
                "f2p_passed": 1, "f2p_total": 1,
                "p2p_passed": 2, "p2p_total": 2,
                "workspace_cleaned": True,
            }),
        )
        summary = summarize_events(events)
        assert summary.verifier_outcome == "RESOLVED"
        assert summary.verifier_f2p == "1/1"
        assert summary.verifier_p2p == "2/2"

    def test_duration_from_timestamps(self) -> None:
        first = make_event(SessionEventKind.SESSION_STARTED, {}, sequence=0, timestamp="2026-08-28T10:00:00Z")
        last = make_event(SessionEventKind.SESSION_FAILED, {"status": "failed", "termination_reason": "model_error"}, sequence=1, timestamp="2026-08-28T10:08:32Z")
        summary = summarize_events([first, last])
        assert summary.duration_seconds == 512

    def test_every_valid_event_kind_is_summarizable(self) -> None:
        """The projection never raises on any schema-valid event stream."""
        events = [
            make_event(kind, VALID_PAYLOADS[kind], sequence=index)
            for index, kind in enumerate(SessionEventKind)
        ]
        summary = summarize_events(events)  # must not raise
        assert summary.event_count == len(SessionEventKind)


class TestRender:
    def test_render_failed_but_active_session(self) -> None:
        summary = EffortSummary(
            event_count=140,
            duration_seconds=512,
            model_requests=12,
            model_requests_ok=9,
            model_requests_error=3,
            directives_accepted=9,
            directives_rejected=4,
            rejection_categories=(("invalid_arguments", 3), ("unknown_action", 1)),
            tool_calls=22,
            tool_calls_by_name=(("find_function", 6), ("get_source_window", 8)),
            debugger_observations=4,
            patches_proposed=1,
            patches_applied=1,
            states_visited=("REPRODUCE", "DIAGNOSE"),
        )
        text = render_effort_summary(summary)
        assert "12 model request(s)" in text
        assert "3 error" in text
        assert "invalid_arguments×3" in text
        assert "22 tool call(s)" in text
        assert "patches: 1 proposed" in text
        assert "states visited" in text

    def test_render_custom_title(self) -> None:
        text = render_effort_summary(EffortSummary(event_count=1), title="Custom")
        assert text.startswith("Custom")
