"""Versioned, JSON-compatible golden trajectory artifact contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agentic_debugger import SchemaValidationError
from agentic_debugger.events.schema import validate_json_compatible


class GoldenArtifactError(ValueError):
    """Raised when a golden trajectory artifact is malformed."""


_REQUIRED = {
    "schema_version",
    "trajectory_name",
    "task_id",
    "policy_name",
    "fixed_scripted_model_sequence",
    "expected_semantic_events",
    "expected_controller_terminal_state",
    "expected_patch_assertion",
    "expected_evaluator_status",
    "expected_evaluator_outcome",
    "expected_f2p_totals",
    "expected_p2p_totals",
    "expected_pdb_usage",
    "expected_model_call_count",
}
_KNOWN = _REQUIRED | {"metadata"}


def _required_text(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GoldenArtifactError(f"{key} must be a non-empty string")
    return value


def _totals(mapping: Mapping[str, Any], key: str) -> dict[str, int]:
    value = mapping.get(key)
    if not isinstance(value, Mapping) or set(value) != {"passed", "total"}:
        raise GoldenArtifactError(f"{key} must contain passed and total")
    passed = value["passed"]
    total = value["total"]
    if type(passed) is not int or type(total) is not int or passed < 0 or total < 0 or passed > total:
        raise GoldenArtifactError(f"{key} totals are invalid")
    return {"passed": passed, "total": total}


class GoldenArtifact:
    """Validated artifact backed by private canonical JSON bytes."""

    __slots__ = ("_canonical_json",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        validated = self._validate_mapping(data)
        self._canonical_json = json.dumps(
            validated, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @property
    def data(self) -> dict[str, Any]:
        return json.loads(self._canonical_json.decode("utf-8"))

    @staticmethod
    def _validate_mapping(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise GoldenArtifactError("golden artifact must be an object")
        candidate = dict(value)
        try:
            validate_json_compatible(candidate, "golden")
            json.dumps(candidate, ensure_ascii=False, allow_nan=False)
        except (SchemaValidationError, TypeError, ValueError, OverflowError) as exc:
            raise GoldenArtifactError(f"golden artifact is not JSON-compatible: {exc}") from exc
        missing = _REQUIRED - set(candidate)
        if missing:
            raise GoldenArtifactError(f"missing golden fields: {sorted(missing)}")
        extra = set(candidate) - _KNOWN
        if extra:
            raise GoldenArtifactError(f"unknown golden fields: {sorted(extra)}")
        if candidate["schema_version"] != "1.0":
            raise GoldenArtifactError(f"unsupported golden schema version: {candidate['schema_version']!r}")
        _required_text(candidate, "trajectory_name")
        _required_text(candidate, "task_id")
        _required_text(candidate, "policy_name")

        from agentic_debugger.agent.controller_policy import ActionName
        from agentic_debugger.agent.model_adapter import ModelDirectiveKind
        from agentic_debugger.agent.state_machine import ControllerState
        from agentic_debugger.events.schema import EventType

        sequence = candidate["fixed_scripted_model_sequence"]
        if not isinstance(sequence, list) or not sequence:
            raise GoldenArtifactError("fixed_scripted_model_sequence must be a non-empty list")
        for index, item in enumerate(sequence):
            if not isinstance(item, Mapping):
                raise GoldenArtifactError(f"scripted sequence item {index} must be a mapping")
            expected_state = item.get("expected_state")
            kind = item.get("kind")
            try:
                ControllerState(expected_state)
                directive_kind = ModelDirectiveKind(kind)
            except (TypeError, ValueError):
                raise GoldenArtifactError(f"scripted sequence item {index} has invalid state or kind")
            common = {"expected_state", "kind"}
            if directive_kind is ModelDirectiveKind.ACTION:
                if set(item) != common | {"action", "arguments"}:
                    raise GoldenArtifactError(f"scripted action item {index} has unknown or missing fields")
                try:
                    ActionName(item["action"])
                except (TypeError, ValueError):
                    raise GoldenArtifactError(f"scripted action item {index} has invalid action")
                if not isinstance(item["arguments"], Mapping):
                    raise GoldenArtifactError(f"scripted action item {index} arguments must be a mapping")
                try:
                    validate_json_compatible(item["arguments"], f"fixed_scripted_model_sequence[{index}].arguments")
                except SchemaValidationError as exc:
                    raise GoldenArtifactError(str(exc)) from exc
            elif directive_kind is ModelDirectiveKind.TRANSITION:
                if set(item) != common | {"target_state", "reason"}:
                    raise GoldenArtifactError(f"scripted transition item {index} has unknown or missing fields")
                try:
                    ControllerState(item["target_state"])
                except (TypeError, ValueError):
                    raise GoldenArtifactError(f"scripted transition item {index} has invalid target state")
                if not isinstance(item["reason"], str) or not item["reason"].strip():
                    raise GoldenArtifactError(f"scripted transition item {index} reason is invalid")
            else:
                raise GoldenArtifactError(f"unsupported scripted directive kind: {kind!r}")

        expected_model_calls = candidate["expected_model_call_count"]
        if type(expected_model_calls) is not int or expected_model_calls != len(sequence):
            raise GoldenArtifactError("expected_model_call_count must equal scripted sequence length")

        events = candidate["expected_semantic_events"]
        if not isinstance(events, list) or not events or not all(isinstance(item, Mapping) for item in events):
            raise GoldenArtifactError("expected_semantic_events must be a non-empty list of objects")
        from agentic_debugger.events.replay import ReplayValidationError, validate_semantic_projection
        try:
            validate_semantic_projection(events)
        except ReplayValidationError as exc:
            raise GoldenArtifactError(f"invalid semantic events: {exc}") from exc
        required_event = {"_semantic_projection", "schema_version", "event_id", "run_id", "task_id", "sequence", "event_type", "name", "state", "metadata", "payload"}
        for index, event in enumerate(events):
            if not required_event.issubset(event):
                raise GoldenArtifactError(f"semantic event {index} is incomplete")
            if event["_semantic_projection"] != "trajectory-semantic-v1":
                raise GoldenArtifactError("semantic event projection marker is invalid")
            if event["schema_version"] != "1.0":
                raise GoldenArtifactError("semantic event schema version is invalid")
            for key in ("event_id", "run_id", "task_id", "name"):
                if not isinstance(event[key], str) or not event[key]:
                    raise GoldenArtifactError(f"semantic event {key} is invalid")
            try:
                EventType(event["event_type"])
            except (TypeError, ValueError):
                raise GoldenArtifactError("semantic event type is invalid")
            if type(event["sequence"]) is not int or event["sequence"] != index:
                raise GoldenArtifactError("semantic event sequences must be contiguous")
            if not isinstance(event["event_type"], str) or not event["event_type"]:
                raise GoldenArtifactError("semantic event type is invalid")
            if not isinstance(event["name"], str) or not event["name"]:
                raise GoldenArtifactError("semantic event name is invalid")
            if event["state"] is not None:
                try:
                    ControllerState(event["state"])
                except (TypeError, ValueError):
                    raise GoldenArtifactError("semantic event state is invalid")
            if not isinstance(event["metadata"], Mapping) or set(event["metadata"]) != {"model", "tool_version"}:
                raise GoldenArtifactError("semantic event metadata is invalid")
            if not all(value is None or isinstance(value, str) for value in event["metadata"].values()):
                raise GoldenArtifactError("semantic event metadata values are invalid")
            if not isinstance(event["payload"], Mapping):
                raise GoldenArtifactError("semantic event payload is invalid")

        if any(event["task_id"] != candidate["task_id"] for event in events):
            raise GoldenArtifactError("semantic event task ID does not match artifact task ID")
        run_ids = {event["run_id"] for event in events}
        if len(run_ids) != 1:
            raise GoldenArtifactError("semantic event run IDs are inconsistent")
        final_events = [event for event in events if event["event_type"] == EventType.FINAL.value]
        if len(final_events) != 1 or final_events[0] is not events[-1]:
            raise GoldenArtifactError("semantic events must contain one final event last")
        terminal = ControllerState(candidate["expected_controller_terminal_state"])
        final_event = final_events[0]
        if final_event["state"] != terminal.value or final_event["payload"].get("final_state") != terminal.value:
            raise GoldenArtifactError("final event does not match expected controller terminal state")
        sequence_decisions = [event for event in events if event["event_type"] == EventType.DECISION.value]
        if len(sequence_decisions) != len(sequence):
            raise GoldenArtifactError("each scripted directive must have one controller decision")
        indices = [event["payload"].get("model_call_index") for event in sequence_decisions]
        if indices != list(range(len(sequence))):
            raise GoldenArtifactError("controller decision model_call_index values are incoherent")
        event_positions = {id(event): index for index, event in enumerate(events)}
        for index, item in enumerate(sequence):
            decision = sequence_decisions[index]
            if decision["state"] != item["expected_state"] or decision["payload"].get("directive_kind") != item["kind"]:
                raise GoldenArtifactError("scripted directive and controller decision are inconsistent")
            decision_position = event_positions[id(decision)]
            next_position = event_positions[id(sequence_decisions[index + 1])] if index + 1 < len(sequence_decisions) else len(events) - 1
            section = events[decision_position + 1:next_position]
            if item["kind"] == "action":
                action_events = [event for event in section if event["event_type"] == EventType.ACTION.value]
                observation_events = [event for event in section if event["event_type"] == EventType.OBSERVATION.value]
                if not action_events and not observation_events and not candidate["expected_patch_assertion"]["executed"] and item["action"] == "apply_patch":
                    continue
                if len(action_events) != 1 or len(observation_events) != 1:
                    raise GoldenArtifactError("action directive must have one action and observation")
                action = action_events[0]["payload"].get("action", {})
                if action.get("name") != item["action"] or action.get("arguments") != item["arguments"]:
                    raise GoldenArtifactError("scripted action does not match semantic action event")
            else:
                transition_events = [event for event in section if event["event_type"] == EventType.TRANSITION.value]
                if len(transition_events) != 1:
                    raise GoldenArtifactError("transition directive must have one transition event")
                transition = transition_events[0]["payload"]
                if transition.get("source_state") != item["expected_state"] or transition.get("target_state") != item["target_state"] or transition.get("reason") != item["reason"]:
                    raise GoldenArtifactError("scripted transition does not match semantic transition event")
        final_model_calls = final_event["payload"].get("model_calls")
        if final_model_calls != candidate["expected_model_call_count"]:
            raise GoldenArtifactError("final model_calls does not match expected model call count")
        pdb_action_names = {"start_pdb_session", "get_failure_trace", "get_stack_summary", "get_frame_locals", "stop_pdb_session"}
        pdb_observation_names = {"get_failure_trace", "get_stack_summary", "get_frame_locals"}
        derived_pdb = {
            "actions": sum(event["name"] in pdb_action_names for event in events if event["event_type"] == EventType.ACTION.value),
            "observations": sum(event["name"] in pdb_observation_names for event in events if event["event_type"] == EventType.OBSERVATION.value),
        }
        if derived_pdb != dict(candidate["expected_pdb_usage"]):
            raise GoldenArtifactError("expected PDB usage does not match semantic events")
        apply_actions = [event for event in events if event["event_type"] == EventType.ACTION.value and event["name"] == "apply_patch"]
        apply_observations = [event for event in events if event["event_type"] == EventType.OBSERVATION.value and event["name"] == "apply_patch"]
        patch = candidate["expected_patch_assertion"]
        if patch.get("executed"):
            if len(apply_actions) != 1 or len(apply_observations) != 1:
                raise GoldenArtifactError("executed patch must have one action and observation")
            action = apply_actions[0]["payload"]["action"]
            observation = apply_observations[0]["payload"]["observation"]
            if observation["action_id"] != action["action_id"] or observation["status"] != "ok":
                raise GoldenArtifactError("patch observation linkage or status is invalid")
            payload = observation["payload"]
            if payload.get("applied") is not True or payload.get("target") != patch["target_file"] or payload.get("patch_sha256") != patch["patch_sha256"] or payload.get("valid_unified_diff") is not True:
                raise GoldenArtifactError("patch observation does not match expected patch assertion")
        elif apply_actions or apply_observations:
            raise GoldenArtifactError("rejection artifact must not contain patch execution")
        _totals(candidate, "expected_f2p_totals")
        _totals(candidate, "expected_p2p_totals")
        _required_text(candidate, "expected_controller_terminal_state")
        _required_text(candidate, "expected_evaluator_status")
        _required_text(candidate, "expected_evaluator_outcome")
        patch = candidate["expected_patch_assertion"]
        if not isinstance(patch, Mapping) or type(patch.get("executed")) is not bool:
            raise GoldenArtifactError("expected_patch_assertion.executed must be boolean")
        if patch["executed"]:
            required_patch = {"executed", "target_file", "patch_sha256", "valid_unified_diff", "semantic_change"}
            if set(patch) != required_patch:
                raise GoldenArtifactError("successful patch assertion is incomplete")
            if not isinstance(patch["target_file"], str) or not patch["target_file"]:
                raise GoldenArtifactError("patch target_file is invalid")
            if not isinstance(patch["patch_sha256"], str) or len(patch["patch_sha256"]) != 64:
                raise GoldenArtifactError("patch_sha256 is invalid")
            if not all(char in "0123456789abcdef" for char in patch["patch_sha256"].lower()):
                raise GoldenArtifactError("patch_sha256 is invalid")
            if type(patch["valid_unified_diff"]) is not bool or not patch["valid_unified_diff"]:
                raise GoldenArtifactError("valid_unified_diff must be true")
            if not isinstance(patch["semantic_change"], str) or not patch["semantic_change"].strip():
                raise GoldenArtifactError("semantic_change is invalid")
            if candidate["expected_controller_terminal_state"] != ControllerState.DONE.value:
                raise GoldenArtifactError("successful artifact must terminate in Done")
            if candidate["expected_evaluator_status"] != "COMPLETED" or candidate["expected_evaluator_outcome"] != "RESOLVED":
                raise GoldenArtifactError("successful artifact must be COMPLETED / RESOLVED")
            for key in ("expected_f2p_totals", "expected_p2p_totals"):
                totals = candidate[key]
                if totals["total"] <= 0 or totals["passed"] != totals["total"]:
                    raise GoldenArtifactError("successful artifact totals must be fully passed and non-zero")
        else:
            if set(patch) != {"executed", "reason"} or not isinstance(patch["reason"], str) or not patch["reason"].strip():
                raise GoldenArtifactError("rejected patch assertion requires only executed and reason")
            if candidate["expected_controller_terminal_state"] != ControllerState.FAILED.value:
                raise GoldenArtifactError("rejection artifact must terminate in Failed")
            if candidate["expected_evaluator_status"] != "not_run" or candidate["expected_evaluator_outcome"] not in {"not_run", "null"}:
                raise GoldenArtifactError("rejection artifact evaluator must be not_run")
            if any(candidate[key] != {"passed": 0, "total": 0} for key in ("expected_f2p_totals", "expected_p2p_totals")):
                raise GoldenArtifactError("rejection artifact totals must be zero")

        pdb = candidate["expected_pdb_usage"]
        if not isinstance(pdb, Mapping) or set(pdb) != {"actions", "observations"}:
            raise GoldenArtifactError("expected_pdb_usage must contain only actions and observations")
        if any(type(pdb[name]) is not int or pdb[name] < 0 for name in ("actions", "observations")):
            raise GoldenArtifactError("expected_pdb_usage counts are invalid")
        model_calls = candidate["expected_model_call_count"]
        if type(model_calls) is not int or model_calls != len(sequence):
            raise GoldenArtifactError("expected_model_call_count must equal scripted sequence length")
        return candidate

    @classmethod
    def from_mapping(cls, value: Any) -> "GoldenArtifact":
        return cls(value)

    @classmethod
    def from_file(cls, path: str | Path) -> "GoldenArtifact":
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GoldenArtifactError(f"cannot read golden artifact: {exc}") from exc
        return cls.from_mapping(value)

    def to_mapping(self) -> dict[str, Any]:
        return self.data


def load_golden_artifact(value: str | Path | Mapping[str, Any]) -> GoldenArtifact:
    if isinstance(value, (str, Path)):
        return GoldenArtifact.from_file(value)
    return GoldenArtifact.from_mapping(value)


__all__ = ["GoldenArtifactError", "GoldenArtifact", "load_golden_artifact"]
