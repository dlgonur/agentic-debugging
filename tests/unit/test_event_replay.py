from __future__ import annotations

import io
import json
from dataclasses import replace

import pytest

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.events.replay import (
    ReplayInputError,
    ReplayValidationError,
    ReplayTrajectory,
    compare_trajectories,
    replay_events,
    semantic_projection,
)
from agentic_debugger.events.schema import (
    Action,
    EventType,
    Metadata,
    Observation,
    ObservationStatus,
    RunEvent,
)


RUN = "run-replay"
TASK = "task-replay"
TS = "2026-01-01T00:00:00Z"


def event(sequence: int, event_type: EventType, name: str, state: ControllerState | None, payload: dict, *, event_id: str | None = None, timestamp: str = TS, duration_ms: int | None = 3) -> RunEvent:
    return RunEvent(
        "1.0", event_id or f"event-{sequence}", RUN, TASK, sequence, timestamp,
        event_type, name, state.value if state else None, payload,
        Metadata(duration_ms, "test", "scripted", None, None),
    )


def action(sequence: int, state: ControllerState, name: str, arguments: dict) -> Action:
    return Action(f"action-{sequence:09d}", RUN, TASK, state, name, arguments)


def observation(sequence: int, action_value: Action, payload: dict, status: ObservationStatus = ObservationStatus.OK) -> Observation:
    return Observation(
        f"observation-{sequence:09d}", action_value.action_id, RUN, TASK,
        action_value.name, status, payload, "bounded", False,
    )


def valid_events() -> list[RunEvent]:
    run = action(1, ControllerState.REPRODUCE, "run_tests", {"node_id": "tests/test.py::test_x"})
    run_obs = observation(1, run, {"dispatch_reason": "ok", "status": "fail"})
    patch = action(5, ControllerState.PATCH, "apply_patch", {"patch": "diff"})
    patch_obs = observation(5, patch, {"dispatch_reason": "ok", "changed_files": ["module.py"]})
    validate = action(7, ControllerState.VALIDATE, "run_tests", {"node_id": "tests/test.py::test_x"})
    validate_obs = observation(7, validate, {"dispatch_reason": "ok", "status": "pass"})
    return [
        event(0, EventType.MESSAGE, "run_started", ControllerState.REPRODUCE, {"message": "start"}),
        event(1, EventType.ACTION, "run_tests", ControllerState.REPRODUCE, {"action": run.to_mapping()}),
        event(2, EventType.OBSERVATION, "run_tests_result", ControllerState.REPRODUCE, {"observation": run_obs.to_mapping()}),
        event(3, EventType.TRANSITION, "state_transition", ControllerState.UNDERSTAND, {"source_state": "Reproduce", "target_state": "Understand", "reason": "failure collected"}),
        event(4, EventType.TRANSITION, "state_transition", ControllerState.PATCH, {"source_state": "Understand", "target_state": "Patch", "reason": "root cause located"}),
        event(5, EventType.ACTION, "apply_patch", ControllerState.PATCH, {"action": patch.to_mapping()}),
        event(6, EventType.OBSERVATION, "apply_patch_result", ControllerState.PATCH, {"observation": patch_obs.to_mapping()}),
        event(7, EventType.TRANSITION, "state_transition", ControllerState.VALIDATE, {"source_state": "Patch", "target_state": "Validate", "reason": "patch applied"}),
        event(8, EventType.ACTION, "run_tests", ControllerState.VALIDATE, {"action": validate.to_mapping()}),
        event(9, EventType.OBSERVATION, "run_tests_result", ControllerState.VALIDATE, {"observation": validate_obs.to_mapping()}),
        event(10, EventType.TRANSITION, "state_transition", ControllerState.DONE, {"source_state": "Validate", "target_state": "Done", "reason": "verified"}),
        event(11, EventType.FINAL, "run_finished", ControllerState.DONE, {"final_state": "Done", "stop_reason": "done"}),
    ]


def mappings(events: list[RunEvent]) -> list[dict]:
    return [item.to_mapping() for item in events]


def test_valid_event_sequence_replay_and_jsonl() -> None:
    source = valid_events()
    replay = replay_events(source)
    assert replay.run_id == RUN
    assert replay.task_id == TASK
    assert len(replay.events) == len(source)
    assert json.loads(replay.to_jsonl().splitlines()[0])["sequence"] == 0
    assert replay_events(replay.to_jsonl()).to_records() == replay.to_records()


def test_replay_accepts_local_path_and_detaches_mappings(tmp_path) -> None:
    source = valid_events()
    raw = mappings(source)
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(item) + "\n" for item in raw), encoding="utf-8")
    result = replay_events(path)
    raw[0]["payload"]["message"] = "changed"
    assert result.records[0]["payload"]["message"] == "start"
    with pytest.raises(TypeError):
        result.records[0]["sequence"] = 99


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda e: [replace(e[0], sequence=1), *e[1:]], "sequence must start"),
        (lambda e: [e[0], *e[2:]], "missing sequence"),
        (lambda e: [e[0], e[2], e[1], *e[3:]], "out-of-order"),
    ],
)
def test_sequence_corruption_is_rejected(mutator, match) -> None:
    source = valid_events()
    broken = mutator(source)
    with pytest.raises(ReplayValidationError, match=match):
        replay_events(broken)


def test_mixed_identity_unknown_linkage_and_illegal_transition() -> None:
    source = valid_events()
    mixed = list(source)
    mixed[1] = replace(mixed[1], run_id="other")
    with pytest.raises(ReplayValidationError, match="mixed run IDs"):
        replay_events(mixed)

    unknown = list(source)
    payload = dict(unknown[2].payload)
    item = dict(payload["observation"])
    item["action_id"] = "unknown"
    payload["observation"] = item
    unknown[2] = replace(unknown[2], payload=payload)
    with pytest.raises(ReplayValidationError, match="unknown action"):
        replay_events(unknown)

    illegal = list(source)
    illegal[4] = replace(illegal[4], payload={"source_state": "Understand", "target_state": "Done", "reason": "bad"}, state="Done")
    with pytest.raises(ReplayValidationError, match="illegal controller transition"):
        replay_events(illegal)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "after"])
def test_final_event_contract(kind: str) -> None:
    source = valid_events()
    if kind == "missing":
        source = source[:-1]
    elif kind == "duplicate":
        source = source + [replace(source[-1], sequence=len(source), event_id="another-final")]
    else:
        source = source + [replace(source[0], sequence=len(source), event_id="after-final")]
    with pytest.raises(ReplayValidationError):
        replay_events(source)


def test_malformed_jsonl_and_unknown_schema_version() -> None:
    with pytest.raises(ReplayInputError, match="malformed JSONL"):
        replay_events("{not-json}\n")
    source = valid_events()
    broken = list(source)
    broken[0] = replace(broken[0], schema_version="9.0")
    with pytest.raises(ReplayValidationError, match="invalid event schema"):
        replay_events(broken)


def test_semantic_projection_normalizes_unstable_fields_but_keeps_material_fields() -> None:
    first = valid_events()
    second = [replace(item, timestamp="2027-02-02T02:02:02Z", metadata=replace(item.metadata, duration_ms=999)) for item in first]
    assert semantic_projection(first) == semantic_projection(second)
    changed = list(second)
    payload = dict(changed[5].payload)
    action_mapping = dict(payload["action"])
    action_mapping["arguments"] = {"patch": "different"}
    payload["action"] = action_mapping
    changed[5] = replace(changed[5], payload=payload)
    comparison = compare_trajectories(semantic_projection(first), semantic_projection(changed))
    assert not comparison.equal
    assert comparison.mismatch is not None
    assert comparison.mismatch.event_index == 5
    assert "arguments" in comparison.mismatch.field_path


def test_semantic_projection_normalizes_workspace_paths_and_generated_ids() -> None:
    source = valid_events()
    altered = [replace(item, event_id=f"random-{item.sequence}") for item in source]
    first = semantic_projection(source, workspace_roots=[r"C:\one\workspace"])
    second = semantic_projection(altered, workspace_roots=[r"C:\two\workspace"])
    assert first == second


def test_material_observation_payload_difference_is_reported() -> None:
    source = valid_events()
    altered = list(source)
    payload = dict(altered[9].payload)
    observation_mapping = dict(payload["observation"])
    observation_mapping["status"] = "error"
    payload["observation"] = observation_mapping
    altered[9] = replace(altered[9], payload=payload)
    result = compare_trajectories(semantic_projection(source), semantic_projection(altered))
    assert not result.equal
    assert result.mismatch is not None
    assert result.mismatch.event_index == 9


def test_replay_trajectory_public_constructor_is_factory_only() -> None:
    with pytest.raises(ReplayValidationError, match="created by replay_events"):
        ReplayTrajectory((), "run", "task")
    trajectory = replay_events(valid_events())
    assert trajectory.run_id == RUN


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (r"C:\project\one\display_name.py", r"D:\other\two\wrong.py"),
        ("/opt/project/one/display_name.py", "/srv/other/wrong.py"),
    ],
)
def test_unrelated_absolute_paths_remain_material(left: str, right: str) -> None:
    first = valid_events()
    second = valid_events()
    action_left = dict(first[5].payload["action"])
    action_right = dict(second[5].payload["action"])
    action_left["arguments"] = {"path": left}
    action_right["arguments"] = {"path": right}
    first[5] = replace(first[5], payload={"action": action_left})
    second[5] = replace(second[5], payload={"action": action_right})
    assert not compare_trajectories(semantic_projection(first), semantic_projection(second)).equal


def test_declared_workspace_roots_normalize_matching_suffixes_only() -> None:
    first = valid_events()
    second = valid_events()
    action_left = dict(first[5].payload["action"])
    action_right = dict(second[5].payload["action"])
    action_left["arguments"] = {"path": r"C:\one\workspace\display_name.py"}
    action_right["arguments"] = {"path": r"D:\two\workspace\display_name.py"}
    first[5] = replace(first[5], payload={"action": action_left})
    second[5] = replace(second[5], payload={"action": action_right})
    roots = [r"C:\one\workspace", r"D:\two\workspace"]
    assert compare_trajectories(
        semantic_projection(first, workspace_roots=roots),
        semantic_projection(second, workspace_roots=roots),
    ).equal
    action_right["arguments"] = {"path": r"D:\two\workspace\other.py"}
    second[5] = replace(second[5], payload={"action": action_right})
    assert not compare_trajectories(
        semantic_projection(first, workspace_roots=roots),
        semantic_projection(second, workspace_roots=roots),
    ).equal


def test_action_and_observation_path_differences_are_detected() -> None:
    first = valid_events()
    second = valid_events()
    action = dict(second[5].payload["action"])
    action["arguments"] = {"path": r"C:\project\one\display_name.py"}
    second[5] = replace(second[5], payload={"action": action})
    assert not compare_trajectories(first, second).equal
    observation = dict(first[6].payload["observation"])
    observation["payload"] = {"path": "/opt/project/one/display_name.py"}
    first[6] = replace(first[6], payload={"observation": observation})
    observation = dict(second[6].payload["observation"])
    observation["payload"] = {"path": "/srv/other/wrong.py"}
    second[6] = replace(second[6], payload={"observation": observation})
    assert not compare_trajectories(first, second).equal


@pytest.mark.parametrize("mutation", ["event_type", "state", "identity", "task", "sequence", "missing_final", "multiple_final", "after_final", "illegal_transition", "unknown_action"])
def test_invalid_preprojected_semantic_trajectories_are_rejected(mutation: str) -> None:
    import copy
    projected = [dict(item) for item in semantic_projection(valid_events())]
    broken = copy.deepcopy(projected)
    if mutation == "event_type":
        broken[0]["event_type"] = "not-a-real-event"
    elif mutation == "state":
        broken[0]["state"] = "NotAState"
    elif mutation == "identity":
        broken[0].pop("run_id")
    elif mutation == "task":
        broken[1]["task_id"] = "other-task"
    elif mutation == "sequence":
        broken[1]["sequence"] = 7
    elif mutation == "missing_final":
        broken.pop()
    elif mutation == "multiple_final":
        broken.insert(-1, copy.deepcopy(broken[-1]))
        broken[-2]["sequence"] = len(broken) - 2
    elif mutation == "after_final":
        extra = copy.deepcopy(broken[0])
        extra["sequence"] = len(broken)
        broken.append(extra)
    elif mutation == "illegal_transition":
        transition = next(item for item in broken if item["event_type"] == "transition")
        transition["payload"]["target_state"] = "Done"
        transition["state"] = "Done"
    else:
        observation = next(item for item in broken if item["event_type"] == "observation")
        observation["payload"]["observation"]["action_id"] = "unknown-action"
    with pytest.raises(ReplayValidationError):
        compare_trajectories(broken, broken)
