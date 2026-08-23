from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_event_replay import valid_events

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.controller import ControllerRunConfig, DeterministicController
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import (
    ControllerSnapshot,
    ScriptedModelAdapter,
    ScriptedModelStep,
    TransitionDirective,
)
from agentic_debugger.agent.tool_registry import ToolRegistry
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.events.schema import EventType
from agentic_debugger.events.replay import ReplayValidationError, compare_trajectories, replay_events, semantic_projection


def test_initial_state_contract_accepts_reproduce_and_rejects_other_openings() -> None:
    source = valid_events()
    stateful = list(source)
    stateful[0] = replace(stateful[0], state=None)
    assert replay_events(stateful).events[1].state == ControllerState.REPRODUCE.value

    bad_state = list(source)
    bad_state[0] = replace(bad_state[0], state=ControllerState.UNDERSTAND.value)
    with pytest.raises(ReplayValidationError, match="begin in Reproduce"):
        replay_events(bad_state)

    bad_action = list(source)
    bad_action[0] = replace(bad_action[0], state=None)
    action = dict(bad_action[1].payload["action"])
    action["state"] = ControllerState.PATCH.value
    bad_action[1] = replace(bad_action[1], state=ControllerState.PATCH.value, payload={"action": action})
    with pytest.raises(ReplayValidationError, match="begin in Reproduce"):
        replay_events(bad_action)


def test_first_transition_must_start_from_reproduce_and_direct_failed_is_rejected() -> None:
    source = valid_events()
    transition = list(source)
    transition[0] = replace(transition[0], state=None)
    transition[3] = replace(
        transition[3],
        state=ControllerState.PATCH.value,
        payload={"source_state": "Understand", "target_state": "Patch", "reason": "bad opening"},
    )
    with pytest.raises(ReplayValidationError, match="illegal transition source"):
        replay_events(transition)

    direct_failed = [
        replace(source[0], sequence=0, state=None),
        replace(source[-1], sequence=1, event_id="final-only", state="Failed", payload={"final_state": "Failed"}),
    ]
    with pytest.raises(ReplayValidationError, match="establish Reproduce"):
        replay_events(direct_failed)


def test_model_call_limit_projects_terminal_transition_before_final() -> None:
    initial = ControllerSnapshot(
        "run-limit",
        "task-limit",
        ControllerState.REPRODUCE,
        0,
        ControllerBudgetLimits(1, 1, 1),
        ControllerBudgetState(),
        HypothesisLedger(),
        None,
    )
    adapter = ScriptedModelAdapter(
        (
            ScriptedModelStep(
                ControllerState.REPRODUCE,
                TransitionDirective(ControllerState.UNDERSTAND, "continue"),
            ),
        )
    )
    result = DeterministicController(
        ToolRegistry(),
        adapter,
        ControllerRunConfig(max_model_calls=1),
    ).run(initial)

    events = project_controller_run(
        result,
        tool_version="test",
        model="scripted",
    )
    assert events[-2].event_type is EventType.TRANSITION
    assert events[-2].payload == {
        "source_state": "Understand",
        "target_state": "Failed",
        "reason": "model_call_limit",
    }
    assert replay_events(events).events[-1].payload["stop_reason"] == "model_call_limit"

    without_transition = [
        *events[:-2],
        replace(
            events[-1],
            sequence=events[-2].sequence,
            event_id=events[-2].event_id,
        ),
    ]
    with pytest.raises(
        ReplayValidationError,
        match="final event does not match current controller state",
    ):
        replay_events(without_transition)


def test_existing_failure_step_does_not_gain_duplicate_terminal_transition() -> None:
    class InvalidAdapter:
        def next_directive(self, snapshot):
            return object()

    initial = ControllerSnapshot(
        "run-error",
        "task-error",
        ControllerState.REPRODUCE,
        0,
        ControllerBudgetLimits(1, 1, 1),
        ControllerBudgetState(),
        HypothesisLedger(),
        None,
    )
    result = DeterministicController(
        ToolRegistry(),
        InvalidAdapter(),
        ControllerRunConfig(max_model_calls=1),
    ).run(initial)
    events = project_controller_run(
        result,
        tool_version="test",
        model="invalid",
    )
    failed_transitions = [
        event
        for event in events
        if event.event_type is EventType.TRANSITION
        and event.payload["target_state"] == "Failed"
    ]
    assert len(failed_transitions) == 1
    assert replay_events(events).events[-1].state == "Failed"


def test_nested_material_diagnostics_are_retained() -> None:
    source = valid_events()
    first = list(source)
    payload = dict(first[2].payload)
    observation = dict(payload["observation"])
    observation["payload"] = {
        "timestamp": "state-A",
        "duration_ms": 4,
        "tokens": ["a", "b"],
        "cost": "algorithmic-cost",
    }
    payload["observation"] = observation
    first[2] = replace(first[2], payload=payload)
    second = list(first)
    changed_payload = dict(second[2].payload)
    changed_observation = dict(changed_payload["observation"])
    changed_observation["payload"] = dict(changed_observation["payload"])
    changed_observation["payload"]["timestamp"] = "state-B"
    changed_payload["observation"] = changed_observation
    second[2] = replace(second[2], payload=changed_payload)
    comparison = compare_trajectories(semantic_projection(first), semantic_projection(second))
    assert not comparison.equal
    assert comparison.mismatch is not None
    assert "timestamp" in comparison.mismatch.field_path


def test_raw_mapping_without_timestamp_is_schema_validated() -> None:
    raw = [item.to_mapping() for item in valid_events()]
    raw[0].pop("timestamp")
    with pytest.raises(ReplayValidationError, match="invalid event schema"):
        compare_trajectories(raw, raw)


def test_replay_public_accessors_are_detached_and_immutable() -> None:
    trajectory = replay_events(valid_events())
    first_events = trajectory.events
    first_events[0].payload["message"] = "changed"
    assert trajectory.events[0].payload["message"] == "start"

    record = trajectory.records[0]
    with pytest.raises(TypeError):
        record["sequence"] = 99
    with pytest.raises(TypeError):
        record |= {"x": 1}
    record_list = trajectory.records[1]["payload"]["action"]["arguments"]
    with pytest.raises(TypeError):
        record_list += ["x"]
    assert trajectory.records[0]["sequence"] == 0
    assert trajectory.to_records()[0]["payload"]["message"] == "start"
