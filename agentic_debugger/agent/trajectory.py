"""Project a controller run into the canonical :class:`RunEvent` trajectory.

The controller returns immutable step records rather than accepting an event
sink, so the event boundary lives here.  Both the Task 8 golden-trajectory
harness and the Task 9 demonstration project through this single function so
their trajectories cannot drift apart.

The projection is pure: it reads a :class:`ControllerRunResult` and emits
events.  It performs no I/O and never inspects a workspace.

It lives in the ``agent`` package rather than in ``events`` because ``agent``
already depends on ``events``; the reverse dependency would be circular.
"""

from __future__ import annotations

from typing import Optional

from agentic_debugger.agent.controller import ControllerRunResult
from agentic_debugger.agent.controller_policy import ControllerBudgetState
from agentic_debugger.agent.state_machine import ControllerState, is_transition_allowed
from agentic_debugger.events.schema import EventType, Metadata, RunEvent

#: Fixed timestamp used when a caller wants a fully deterministic trajectory.
FIXED_TIMESTAMP = "2026-01-01T00:00:00Z"

#: Event name used for every controller decision record.
DECISION_EVENT_NAME = "controller_decision"

#: Event name used for every controller state transition record.
TRANSITION_EVENT_NAME = "state_transition"

#: Event name used for the single terminal record.
FINAL_EVENT_NAME = "run_finished"


def _budget_mapping(budget: ControllerBudgetState) -> dict[str, int]:
    return {
        "patch_attempts": budget.patch_attempts,
        "test_runs": budget.test_runs,
        "pdb_observations": budget.pdb_observations,
        "source_observations": budget.source_observations,
    }


def project_controller_run(
    result: ControllerRunResult,
    *,
    tool_version: Optional[str],
    model: Optional[str],
    timestamp: str = FIXED_TIMESTAMP,
    duration_ms: Optional[int] = 0,
) -> list[RunEvent]:
    """Return the ordered event trajectory for a completed controller run."""

    if type(result) is not ControllerRunResult:
        raise TypeError("result must be a ControllerRunResult")
    events: list[RunEvent] = []
    sequence = 0

    def add(
        event_type: EventType,
        name: str,
        state: Optional[ControllerState],
        payload: dict[str, object],
    ) -> None:
        nonlocal sequence
        events.append(
            RunEvent(
                "1.0",
                f"event-{sequence:09d}",
                result.run_id,
                result.task_id,
                sequence,
                timestamp,
                event_type,
                name,
                state.value if state is not None else None,
                payload,
                Metadata(duration_ms, tool_version, model, None, None),
            )
        )
        sequence += 1

    for step in result.steps:
        add(
            EventType.DECISION,
            DECISION_EVENT_NAME,
            step.state_before,
            {
                "directive_kind": step.directive_kind.value if step.directive_kind else None,
                "model_call_index": step.model_call_index,
                "stop_reason": step.stop_reason.value if step.stop_reason else None,
                "budget_before": _budget_mapping(step.budget_before),
                "budget_after": _budget_mapping(step.budget_after),
            },
        )
        if step.action is not None:
            add(
                EventType.ACTION,
                step.action.name,
                step.state_before,
                {"action": step.action.to_mapping()},
            )
        if step.observation is not None:
            add(
                EventType.OBSERVATION,
                step.observation.name,
                step.state_before,
                {"observation": step.observation.to_mapping()},
            )
        if step.state_before is not step.state_after:
            add(
                EventType.TRANSITION,
                TRANSITION_EVENT_NAME,
                step.state_after,
                {
                    "source_state": step.state_before.value,
                    "target_state": step.state_after.value,
                    "reason": step.transition_reason
                    or (step.stop_reason.value if step.stop_reason else "controller transition"),
                },
            )

    projected_state = (
        result.steps[-1].state_after
        if result.steps
        else result.initial_state
    )
    if projected_state is not result.final_state:
        # Terminal controller boundaries such as the model-call ceiling occur
        # between model decisions.  They are observable state transitions but
        # deliberately do not fabricate a model directive or tool step.
        if not is_transition_allowed(projected_state, result.final_state):
            raise ValueError("controller final state transition is invalid")
        add(
            EventType.TRANSITION,
            TRANSITION_EVENT_NAME,
            result.final_state,
            {
                "source_state": projected_state.value,
                "target_state": result.final_state.value,
                "reason": result.stop_reason.value,
            },
        )

    add(
        EventType.FINAL,
        FINAL_EVENT_NAME,
        result.final_state,
        {
            "final_state": result.final_state.value,
            "stop_reason": result.stop_reason.value,
            "model_calls": result.model_calls,
        },
    )
    return events


__all__ = [
    "DECISION_EVENT_NAME",
    "FINAL_EVENT_NAME",
    "FIXED_TIMESTAMP",
    "TRANSITION_EVENT_NAME",
    "project_controller_run",
]
