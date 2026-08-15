"""Verifier progress event adapter unit tests (Task-4 observability)."""

from __future__ import annotations

import pytest

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.events import (
    SessionEventKind,
    SourceKind,
)
from agentic_debugger.application.observability import ObservabilityContext
from agentic_debugger.application.presentation import (
    initial_session_view,
    presentation_identity,
    reduce_event,
)
from agentic_debugger.application.verifier_observer import VerifierSessionEventAdapter
from application_support import make_spec

FIXED = "2026-08-14T08:00:00Z"


def make_adapter(**overrides):
    context = ObservabilityContext(
        session_id=overrides.pop("session_id", "session-verifier-001"),
        task_id=overrides.pop("task_id", "curated-off-by-one-002"),
        source_kind=SourceKind.OFFLINE_DEMO,
        run_id=overrides.pop("run_id", "run-verifier-001"),
        initial_sequence=overrides.pop("initial_sequence", 0),
    )
    return VerifierSessionEventAdapter(context, clock=lambda: FIXED)


class TestVerifierEventAdapter:
    def test_started_event(self):
        adapter = make_adapter()
        event = adapter.started()
        assert event.event_kind is SessionEventKind.VERIFIER_STARTED
        assert dict(event.payload) == {}

    def test_stage_events_ordered(self):
        adapter = make_adapter()
        adapter.started()
        adapter.stage_started("prepare_workspace")
        adapter.stage_completed("prepare_workspace", "completed")
        adapter.stage_started("baseline_reproduction")
        adapter.stage_completed("baseline_reproduction", "failed")
        kinds = [e.event_kind for e in adapter.events()]
        assert kinds == [
            SessionEventKind.VERIFIER_STARTED,
            SessionEventKind.VERIFIER_STAGE_STARTED,
            SessionEventKind.VERIFIER_STAGE_COMPLETED,
            SessionEventKind.VERIFIER_STAGE_STARTED,
            SessionEventKind.VERIFIER_STAGE_COMPLETED,
        ]
        assert adapter.events()[1].payload["stage"] == "prepare_workspace"
        assert adapter.events()[2].payload["status"] == "completed"
        assert adapter.events()[4].payload["status"] == "failed"

    def test_unknown_stage_fails_closed(self):
        adapter = make_adapter()
        with pytest.raises(ApplicationInputError):
            adapter.stage_started("not_a_stage")

    def test_unknown_status_fails_closed(self):
        adapter = make_adapter()
        with pytest.raises(ApplicationInputError):
            adapter.stage_completed("prepare_workspace", "not_a_status")

    def test_completed_from_evaluation_result(self):
        from types import SimpleNamespace

        from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
        from agentic_debugger.evaluation.runner import EvaluationStatus

        # The adapter reads only the authoritative EvaluationResult fields
        # it copies into the presentation event (plus workspace.cleaned).
        evaluation = SimpleNamespace(
            status=EvaluationStatus.COMPLETED,
            outcome=SemanticOutcome.RESOLVED,
            f2p_passed=1,
            f2p_total=1,
            p2p_passed=2,
            p2p_total=2,
            workspace=SimpleNamespace(cleaned=True),
        )
        adapter = make_adapter()
        event = adapter.completed(evaluation)
        assert event.event_kind is SessionEventKind.VERIFIER_COMPLETED
        assert dict(event.payload) == {
            "status": "COMPLETED",
            "outcome": "RESOLVED",
            "f2p_passed": 1,
            "f2p_total": 1,
            "p2p_passed": 2,
            "p2p_total": 2,
            "workspace_cleaned": True,
        }

    def test_events_reduce_into_view(self):
        adapter = make_adapter()
        adapter.started()
        adapter.stage_started("prepare_workspace")
        adapter.stage_completed("prepare_workspace", "completed")
        view = initial_session_view(presentation_identity(make_spec()))
        for event in adapter.events():
            view = reduce_event(view, event)
        assert [s.stage.value for s in view.verifier_stages] == ["prepare_workspace"]
        assert view.verifier_stages[0].status.value == "completed"
        assert view.verifier_summary is None  # no completed event yet
