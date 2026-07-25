"""Validation, replay, and semantic comparison for Task 8 event trajectories.

The controller currently returns immutable step records rather than accepting an
event sink.  This module deliberately stays at the event boundary: callers may
project those real records into :class:`RunEvent` values, then use this module
to validate and compare the resulting JSONL trajectory.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from agentic_debugger import SchemaValidationError
from agentic_debugger.agent.state_machine import ControllerState, is_transition_allowed
from agentic_debugger.events.schema import (
    Action,
    EventType,
    Observation,
    RunEvent,
    validate_json_compatible,
)


MAX_REPLAY_DIAGNOSTIC = 512
MAX_COMPARISON_SUMMARY = 240


class ReplayError(ValueError):
    """Base class for bounded replay failures."""


class ReplayInputError(ReplayError):
    """Raised when the replay source is not a supported input."""


class ReplayValidationError(ReplayError):
    """Raised when a supported input violates the replay contract."""

    def __init__(self, message: str, *, event_index: int | None = None) -> None:
        self.event_index = event_index
        detail = message[:MAX_REPLAY_DIAGNOSTIC]
        if event_index is not None:
            detail = f"event {event_index}: {detail}"
        super().__init__(detail)


class _FrozenList(tuple):
    """Tuple-backed JSON sequence with no mutating list protocol."""

    def __new__(cls, values: Iterable[Any] = ()) -> "_FrozenList":
        return tuple.__new__(cls, values)


class _FrozenDict(tuple, Mapping[str, Any]):
    """Tuple-backed mapping whose canonical pairs are the tuple itself."""

    def __new__(cls, values: Mapping[str, Any] | Iterable[tuple[str, Any]]) -> "_FrozenDict":
        items = tuple(values.items()) if isinstance(values, Mapping) else tuple(values)
        return tuple.__new__(cls, tuple((str(key), value) for key, value in items))

    def __iter__(self):
        return (key for key, _ in tuple.__iter__(self))

    def __len__(self) -> int:
        return tuple.__len__(self)

    def __getitem__(self, key: str) -> Any:
        for item_key, item_value in tuple.__iter__(self):
            if item_key == key:
                return item_value
        raise KeyError(key)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenDict((str(key), _freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return _FrozenList(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _bounded(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) <= MAX_COMPARISON_SUMMARY:
        return text
    return text[: MAX_COMPARISON_SUMMARY - 19] + "... [bounded]"


class ReplayTrajectory:
    """A validated trajectory backed by private immutable canonical records."""

    __slots__ = ("_records", "run_id", "task_id")

    def __init__(self, records: tuple[_FrozenDict, ...], run_id: str, task_id: str) -> None:
        raise ReplayValidationError("ReplayTrajectory must be created by replay_events")

    @classmethod
    def _from_records(cls, records: tuple[_FrozenDict, ...], run_id: str, task_id: str) -> "ReplayTrajectory":
        instance = object.__new__(cls)
        object.__setattr__(instance, "_records", tuple(records))
        object.__setattr__(instance, "run_id", run_id)
        object.__setattr__(instance, "task_id", task_id)
        return instance

    def __setattr__(self, name: str, value: Any) -> None:
        if hasattr(self, name):
            raise AttributeError("ReplayTrajectory is immutable")
        object.__setattr__(self, name, value)

    @property
    def events(self) -> tuple[RunEvent, ...]:
        return tuple(RunEvent.from_mapping(_thaw(record)) for record in self._records)

    @property
    def records(self) -> tuple[_FrozenDict, ...]:
        return self._records

    def to_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(_thaw(record) for record in self._records)

    def to_jsonl(self) -> str:
        return "".join(
            json.dumps(_thaw(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for record in self._records
        )


def _iter_source(source: Any) -> Iterator[Any]:
    if isinstance(source, ReplayTrajectory):
        yield from source.events
        return
    if isinstance(source, os.PathLike):
        try:
            yield from _iter_jsonl(Path(source).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ReplayInputError(f"cannot read replay path: {exc}") from exc
        return
    if isinstance(source, str):
        if "\n" not in source and "\r" not in source:
            candidate = Path(source)
            if candidate.exists():
                try:
                    yield from _iter_jsonl(candidate.read_text(encoding="utf-8"))
                except OSError as exc:
                    raise ReplayInputError(f"cannot read replay path: {exc}") from exc
                return
        yield from _iter_jsonl(source)
        return
    try:
        iterator = iter(source)
    except TypeError as exc:
        raise ReplayInputError("replay source must be RunEvent iterable, JSONL text, or local path") from exc
    yield from iterator


def _iter_jsonl(text: str) -> Iterator[dict[str, Any]]:
    if not isinstance(text, str):
        raise ReplayInputError("JSONL source must be text")
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise ReplayInputError(f"malformed JSONL at line {line_number}: blank line")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayInputError(f"malformed JSONL at line {line_number}: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise ReplayInputError(f"malformed JSONL at line {line_number}: event must be an object")
        yield record


def _event_mapping(event: RunEvent) -> dict[str, Any]:
    return event.to_mapping()


def _payload_mapping(event: RunEvent, key: str) -> Mapping[str, Any]:
    candidate = event.payload.get(key)
    if candidate is None and key in event.payload:
        raise ReplayValidationError(f"payload.{key} must be a mapping")
    if candidate is None:
        # Direct payloads are accepted for hand-authored logs, while the
        # golden harness uses explicit action/observation keys.
        candidate = event.payload
    if not isinstance(candidate, Mapping):
        raise ReplayValidationError(f"payload.{key} must be a mapping")
    return candidate


def _state_from(value: Any, label: str) -> ControllerState:
    if not isinstance(value, str):
        raise ReplayValidationError(f"{label} must be a controller state")
    try:
        return ControllerState(value)
    except ValueError as exc:
        raise ReplayValidationError(f"{label} is not a controller state: {value!r}") from exc


def _validate_events(events: list[RunEvent]) -> None:
    if not events:
        raise ReplayValidationError("trajectory is empty")
    run_id = events[0].run_id
    task_id = events[0].task_id
    expected_sequence = 0
    seen_sequences: set[int] = set()
    previous_sequence: int | None = None
    current_state: ControllerState | None = None
    initial_state_established = False
    if events[0].sequence != 0:
        raise ReplayValidationError("sequence must start at 0", event_index=0)
    for position, candidate in enumerate(events[1:], start=1):
        prior = events[position - 1].sequence
        if candidate.sequence in {item.sequence for item in events[:position]}:
            raise ReplayValidationError("duplicate sequence", event_index=position)
        if candidate.sequence < prior:
            raise ReplayValidationError("out-of-order sequence", event_index=position)
    action_ids: set[str] = set()
    observed_action_ids: set[str] = set()
    observation_ids: set[str] = set()
    event_ids: set[str] = set()
    final_index: int | None = None

    for index, event in enumerate(events):
        if event.run_id != run_id:
            raise ReplayValidationError("mixed run IDs", event_index=index)
        if event.task_id != task_id:
            raise ReplayValidationError("mixed task IDs", event_index=index)
        if event.event_id in event_ids:
            raise ReplayValidationError("duplicate event ID", event_index=index)
        event_ids.add(event.event_id)
        if event.sequence in seen_sequences:
            raise ReplayValidationError("duplicate sequence", event_index=index)
        if previous_sequence is not None and event.sequence < previous_sequence:
            raise ReplayValidationError("out-of-order sequence", event_index=index)
        if event.sequence != expected_sequence:
            raise ReplayValidationError(
                f"missing sequence; expected {expected_sequence}, got {event.sequence}",
                event_index=index,
            )
        seen_sequences.add(event.sequence)
        previous_sequence = event.sequence
        expected_sequence += 1

        if final_index is not None:
            raise ReplayValidationError("events after final event", event_index=index)

        if event.event_type is EventType.ACTION:
            try:
                action = Action.from_mapping(dict(_payload_mapping(event, "action")))
            except SchemaValidationError as exc:
                raise ReplayValidationError(f"invalid action record: {exc}", event_index=index) from exc
            if action.run_id != run_id or action.task_id != task_id:
                raise ReplayValidationError("action identity does not match trajectory", event_index=index)
            if event.state != action.state.value:
                raise ReplayValidationError("action state does not match event state", event_index=index)
            if action.action_id in action_ids:
                raise ReplayValidationError("duplicate action ID", event_index=index)
            action_ids.add(action.action_id)
            if current_state is None:
                if action.state is not ControllerState.REPRODUCE:
                    raise ReplayValidationError("trajectory must begin in Reproduce", event_index=index)
                current_state = action.state
                initial_state_established = True
            elif action.state is not current_state:
                raise ReplayValidationError("action state does not match current controller state", event_index=index)

        elif event.event_type is EventType.OBSERVATION:
            try:
                observation = Observation.from_mapping(dict(_payload_mapping(event, "observation")))
            except SchemaValidationError as exc:
                raise ReplayValidationError(f"invalid observation record: {exc}", event_index=index) from exc
            if observation.run_id != run_id or observation.task_id != task_id:
                raise ReplayValidationError("observation identity does not match trajectory", event_index=index)
            if observation.action_id not in action_ids:
                raise ReplayValidationError("observation references unknown action", event_index=index)
            if observation.action_id in observed_action_ids:
                raise ReplayValidationError("duplicate observation for action", event_index=index)
            action_event = next(
                item for item in events[:index]
                if item.event_type is EventType.ACTION
                and item.payload.get("action", {}).get("action_id") == observation.action_id
            )
            action_mapping = dict(_payload_mapping(action_event, "action"))
            if observation.name != action_mapping.get("name"):
                raise ReplayValidationError("observation name does not match action", event_index=index)
            if event.state != current_state.value:
                raise ReplayValidationError("observation state does not match current controller state", event_index=index)
            if observation.observation_id in observation_ids:
                raise ReplayValidationError("duplicate observation ID", event_index=index)
            observed_action_ids.add(observation.action_id)
            observation_ids.add(observation.observation_id)
            if current_state is None:
                raise ReplayValidationError("observation has no current controller state", event_index=index)

        elif event.event_type is EventType.TRANSITION:
            source = _state_from(event.payload.get("source_state"), "payload.source_state")
            target = _state_from(event.payload.get("target_state"), "payload.target_state")
            if current_state is None:
                if source is not ControllerState.REPRODUCE:
                    raise ReplayValidationError("trajectory must begin in Reproduce", event_index=index)
                current_state = source
                initial_state_established = True
            if source is not current_state:
                raise ReplayValidationError("illegal transition source", event_index=index)
            if not is_transition_allowed(source, target):
                raise ReplayValidationError(
                    f"illegal controller transition {source.value} -> {target.value}", event_index=index
                )
            if event.state != target.value:
                raise ReplayValidationError("transition target does not match event state", event_index=index)
            current_state = target
            initial_state_established = True

        elif event.event_type is EventType.FINAL:
            if final_index is not None:
                raise ReplayValidationError("multiple final events", event_index=index)
            final_state = _state_from(event.payload.get("final_state"), "payload.final_state")
            if final_state not in (ControllerState.DONE, ControllerState.FAILED):
                raise ReplayValidationError("final state must be Done or Failed", event_index=index)
            if event.state != final_state.value:
                raise ReplayValidationError("final state does not match event state", event_index=index)
            if not initial_state_established or current_state is None:
                raise ReplayValidationError("trajectory must establish Reproduce before final", event_index=index)
            if current_state is not final_state:
                raise ReplayValidationError("final event does not match current controller state", event_index=index)
            final_index = index

        elif event.state is not None:
            state = _state_from(event.state, "event.state")
            if current_state is None:
                if state is not ControllerState.REPRODUCE:
                    raise ReplayValidationError("trajectory must begin in Reproduce", event_index=index)
                current_state = state
                initial_state_established = True
            elif state is not current_state:
                raise ReplayValidationError("event state does not match current controller state", event_index=index)

    missing_observations = action_ids - observed_action_ids
    if missing_observations:
        raise ReplayValidationError(
            f"action-to-observation linkage is incomplete: {sorted(missing_observations)[:3]}"
        )
    if final_index is None:
        raise ReplayValidationError("missing final event")
    if final_index != len(events) - 1:
        raise ReplayValidationError("events after final event", event_index=final_index + 1)


def replay_events(source: Iterable[RunEvent] | str | os.PathLike[str] | ReplayTrajectory) -> ReplayTrajectory:
    """Read and validate actual :class:`RunEvent` records without reordering."""

    parsed: list[RunEvent] = []
    for index, raw in enumerate(_iter_source(source)):
        if isinstance(raw, RunEvent):
            mapping = _event_mapping(raw)
        elif isinstance(raw, Mapping):
            mapping = dict(raw)
        else:
            raise ReplayInputError(f"event {index} is not a RunEvent or mapping")
        try:
            event = RunEvent.from_mapping(mapping)
            detached = RunEvent.from_mapping(json.loads(json.dumps(event.to_mapping(), ensure_ascii=False, allow_nan=False)))
        except (SchemaValidationError, TypeError, ValueError, OverflowError) as exc:
            raise ReplayValidationError(f"invalid event schema: {exc}", event_index=index) from exc
        parsed.append(detached)
    _validate_events(parsed)
    records = tuple(_freeze(event.to_mapping()) for event in parsed)
    return ReplayTrajectory._from_records(records, parsed[0].run_id, parsed[0].task_id)


def _normalize_text(value: str, workspace_roots: Sequence[str]) -> str:
    """Normalize separators and only declared workspace roots."""

    result = value.replace("\\", "/")
    normalized_roots: list[tuple[str, str]] = []
    for item in workspace_roots:
        root = str(item).replace("\\", "/").rstrip("/")
        if root:
            key = root.casefold() if len(root) > 1 and root[1] == ":" else root
            normalized_roots.append((root, key))
    candidate_key = result.casefold() if len(result) > 1 and result[1] == ":" else result
    for root, root_key in sorted(normalized_roots, key=lambda pair: len(pair[0]), reverse=True):
        if candidate_key == root_key:
            return "<WORKSPACE>"
        if candidate_key.startswith(root_key + "/"):
            return "<WORKSPACE>" + result[len(root):]
    return result


_SEMANTIC_MARKER = "trajectory-semantic-v1"
_IDENTIFIER_KEYS = {"event_id", "run_id", "action_id", "observation_id", "workspace_id", "session_id", "pdb_session_id"}


def _semantic_value(value: Any, *, workspace_roots: Sequence[str], aliases: dict[str, dict[str, str]]) -> Any:
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for child_key in sorted(value):
            child = value[child_key]
            if child_key in _IDENTIFIER_KEYS and isinstance(child, str):
                namespace = aliases.setdefault(child_key, {})
                child = namespace.setdefault(child, f"<{child_key.upper()}-{len(namespace)}>")
            output[child_key] = _semantic_value(child, workspace_roots=workspace_roots, aliases=aliases)
        return output
    if isinstance(value, (list, tuple)):
        return [_semantic_value(item, workspace_roots=workspace_roots, aliases=aliases) for item in value]
    if isinstance(value, str):
        return _normalize_text(value, workspace_roots)
    return value


def semantic_projection(source: Iterable[RunEvent] | str | os.PathLike[str] | ReplayTrajectory, *, workspace_roots: Iterable[str] = ()) -> tuple[dict[str, Any], ...]:
    """Return stable semantic events while retaining material payload fields."""

    trajectory = source if isinstance(source, ReplayTrajectory) else replay_events(source)
    roots = tuple(str(root) for root in workspace_roots)
    aliases: dict[str, dict[str, str]] = {}
    projected: list[dict[str, Any]] = []
    for record in trajectory.to_records():
        event = dict(record)
        event.pop("timestamp", None)
        metadata = event.get("metadata")
        if isinstance(metadata, Mapping):
            event["metadata"] = {
                key: value
                for key, value in metadata.items()
                if key not in {"duration_ms", "tokens", "cost"}
            }
        normalized = _semantic_value(event, workspace_roots=roots, aliases=aliases)
        normalized["_semantic_projection"] = _SEMANTIC_MARKER
        projected.append(normalized)
    return tuple(projected)


@dataclass(frozen=True)
class TrajectoryMismatch:
    event_index: int
    field_path: str
    expected: Any
    actual: Any
    summary: str


@dataclass(frozen=True)
class TrajectoryComparison:
    equal: bool
    mismatch: TrajectoryMismatch | None = None


def _first_mismatch(expected: Any, actual: Any, path: str = "") -> tuple[str, Any, Any] | None:
    if type(expected) is not type(actual):
        return path or "$", expected, actual
    if isinstance(expected, dict):
        keys = sorted(set(expected) | set(actual))
        for key in keys:
            child_path = f"{path}.{key}" if path else key
            if key not in expected or key not in actual:
                return child_path, expected.get(key), actual.get(key)
            mismatch = _first_mismatch(expected[key], actual[key], child_path)
            if mismatch is not None:
                return mismatch
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return path or "$", expected, actual
        for index, (left, right) in enumerate(zip(expected, actual)):
            mismatch = _first_mismatch(left, right, f"{path}[{index}]")
            if mismatch is not None:
                return mismatch
        return None
    if expected != actual:
        return path or "$", expected, actual
    return None


def compare_trajectories(expected: Any, actual: Any, *, workspace_roots: Iterable[str] = ()) -> TrajectoryComparison:
    expected_projection = validate_semantic_projection(expected) if _looks_like_projection(expected) else semantic_projection(expected, workspace_roots=workspace_roots)
    actual_projection = validate_semantic_projection(actual) if _looks_like_projection(actual) else semantic_projection(actual, workspace_roots=workspace_roots)
    mismatch = _first_mismatch(list(expected_projection), list(actual_projection))
    if mismatch is None:
        return TrajectoryComparison(True)
    path, expected_value, actual_value = mismatch
    event_match = re.match(r"\[(\d+)\](?:\.|$)", path)
    event_index = int(event_match.group(1)) if event_match else 0
    return TrajectoryComparison(
        False,
        TrajectoryMismatch(event_index, path, expected_value, actual_value, f"expected={_bounded(expected_value)} actual={_bounded(actual_value)}"),
    )


def validate_semantic_projection(value: Any) -> tuple[dict[str, Any], ...]:
    """Validate a projected trajectory through the replay contract."""

    if not isinstance(value, (list, tuple)) or not value:
        raise ReplayValidationError("semantic projection must be a non-empty sequence")
    reconstructed: list[RunEvent] = []
    detached: list[dict[str, Any]] = []
    required = {
        "_semantic_projection", "schema_version", "event_id", "run_id", "task_id",
        "sequence", "event_type", "name", "state", "metadata", "payload",
    }
    for index, candidate in enumerate(value):
        if not isinstance(candidate, Mapping):
            raise ReplayValidationError("semantic projection event must be a mapping", event_index=index)
        if not required.issubset(candidate):
            raise ReplayValidationError("semantic projection event is incomplete", event_index=index)
        if candidate["_semantic_projection"] != _SEMANTIC_MARKER:
            raise ReplayValidationError("semantic projection marker is invalid", event_index=index)
        metadata = candidate["metadata"]
        if not isinstance(metadata, Mapping) or set(metadata) != {"model", "tool_version"}:
            raise ReplayValidationError("semantic projection metadata is invalid", event_index=index)
        if not all(item is None or isinstance(item, str) for item in metadata.values()):
            raise ReplayValidationError("semantic projection metadata values are invalid", event_index=index)
        mapping = {
            "schema_version": candidate["schema_version"],
            "event_id": candidate["event_id"],
            "run_id": candidate["run_id"],
            "task_id": candidate["task_id"],
            "sequence": candidate["sequence"],
            "timestamp": "2026-01-01T00:00:00Z",
            "event_type": candidate["event_type"],
            "name": candidate["name"],
            "state": candidate["state"],
            "payload": dict(candidate["payload"]) if isinstance(candidate["payload"], Mapping) else candidate["payload"],
            "metadata": {
                "duration_ms": 0,
                "tool_version": metadata.get("tool_version"),
                "model": metadata.get("model"),
                "tokens": None,
                "cost": None,
            },
        }
        try:
            event = RunEvent.from_mapping(mapping)
        except (SchemaValidationError, TypeError, ValueError, OverflowError) as exc:
            raise ReplayValidationError(f"invalid semantic event: {exc}", event_index=index) from exc
        reconstructed.append(event)
        detached.append(json.loads(json.dumps(dict(candidate), ensure_ascii=False, allow_nan=False)))
    _validate_events(reconstructed)
    return tuple(detached)


def _looks_like_projection(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or not value:
        return False
    return any(isinstance(item, Mapping) and "_semantic_projection" in item for item in value)


def assert_trajectories_equal(expected: Any, actual: Any, *, workspace_roots: Iterable[str] = ()) -> None:
    comparison = compare_trajectories(expected, actual, workspace_roots=workspace_roots)
    if not comparison.equal:
        mismatch = comparison.mismatch
        assert mismatch is not None
        raise AssertionError(
            f"trajectory mismatch at event {mismatch.event_index}, {mismatch.field_path}: {mismatch.summary}"
        )


__all__ = [
    "ReplayError",
    "ReplayInputError",
    "ReplayValidationError",
    "ReplayTrajectory",
    "replay_events",
    "semantic_projection",
    "validate_semantic_projection",
    "TrajectoryMismatch",
    "TrajectoryComparison",
    "compare_trajectories",
    "assert_trajectories_equal",
]
