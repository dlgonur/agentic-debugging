from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden_trajectories"))

from support import run_trajectory  # noqa: E402
from agentic_debugger.events.replay import ReplayValidationError, compare_trajectories, replay_events


def test_duplicate_event_id_and_mismatched_observation_are_rejected(tmp_path: Path) -> None:
    run = run_trajectory("curated-none-handling-001", "static", None, tmp_path)
    records = list(run.trajectory.to_records())
    records[1]["event_id"] = records[0]["event_id"]
    with pytest.raises(ReplayValidationError, match="duplicate event ID"):
        replay_events(records)

    records = list(run.trajectory.to_records())
    observation_index = next(index for index, item in enumerate(records) if item["event_type"] == "observation")
    records[observation_index]["payload"]["observation"]["name"] = "wrong_action"
    with pytest.raises(ReplayValidationError, match="observation name"):
        replay_events(records)


def test_raw_mapping_comparison_still_normalizes_timestamp_and_duration(tmp_path: Path) -> None:
    run = run_trajectory("curated-none-handling-001", "rejected", None, tmp_path)
    first = list(run.trajectory.to_records())
    second = list(run.trajectory.to_records())
    for record in second:
        record["timestamp"] = "2029-01-01T00:00:00Z"
        record["metadata"]["duration_ms"] = 999
    comparison = compare_trajectories(first, second)
    assert comparison.equal
