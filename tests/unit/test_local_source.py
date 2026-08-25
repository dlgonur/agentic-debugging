"""Focused provider-free tests for local controller failure classification."""

from types import SimpleNamespace

from agentic_debugger.agent.controller import ControllerStopReason
from agentic_debugger.application.events import SessionTerminationReason
from agentic_debugger.application.local_source import _controller_failure_category


def test_failed_state_after_patch_attempt_budget_is_not_a_generic_controller_error():
    result = SimpleNamespace(
        stop_reason=ControllerStopReason.FAILED,
        steps=(
            SimpleNamespace(
                transition_reason=(
                    "Patch-attempt budget exhausted (2/2) with no successfully applied patch"
                )
            ),
        ),
    )

    category, reason = _controller_failure_category(result)

    assert category == "controller budget exhausted"
    assert reason is SessionTerminationReason.DIRECTIVE_EXHAUSTED


def test_controller_failure_without_budget_evidence_remains_distinct():
    result = SimpleNamespace(
        stop_reason=ControllerStopReason.FAILED,
        steps=(SimpleNamespace(transition_reason="controller invariant failed"),),
    )

    category, reason = _controller_failure_category(result)

    assert category == "controller failed"
    assert reason is SessionTerminationReason.CONTROLLER_FAILED
