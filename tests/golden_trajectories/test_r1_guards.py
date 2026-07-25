from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from support import ModelBackendError, require_scripted_model, run_trajectory


def test_golden_backend_and_network_sentinel(tmp_path: Path) -> None:
    run = run_trajectory("curated-none-handling-001", "static", None, tmp_path)
    assert run.model_backend == "scripted"
    assert run.network_attempts == 0
    assert run.provider_attempts == 0
    with pytest.raises(ModelBackendError, match="scripted model backend"):
        require_scripted_model(object())


def test_pdb_observation_is_linked_to_curated_fixture_and_runtime_clue(tmp_path: Path) -> None:
    run = run_trajectory("curated-none-handling-001", "pdb", None, tmp_path)
    observations = [
        event.payload["observation"]["payload"]
        for event in run.trajectory.events
        if event.event_type.value == "observation"
        and event.payload["observation"]["name"] in {"get_stack_summary", "get_frame_locals"}
    ]
    assert len(observations) == 2
    assert all(item["fixture_source"] == "display_name.py" for item in observations)
    assert all(item["driver"] == "display_name.py::task8_driver" for item in observations)
    assert all(item["target_symbol"] == "format_display_name" for item in observations)
    locals_payload = next(item for item in observations if "locals" in item)
    assert locals_payload["curated_input"] == {"name": None}
    assert any(entry["name"] == "name" and entry["value"]["value"] is None for entry in locals_payload["locals"])
    assert run.source_root is not None
    assert not run.source_root.exists()
