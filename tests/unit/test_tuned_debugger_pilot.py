import json
import subprocess
import sys
from pathlib import Path


def test_frozen_tuned_debugger_pilot_validate_only() -> None:
    repo = Path(__file__).resolve().parents[2]
    script = repo / "experiments" / "tuned_debugger_pilot_v1" / "run_pilot.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--validate-only"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    payload = json.loads(completed.stdout)
    assert payload["status"] == "PASS"
    assert payload["validated_case_count"] == 10
    assert len(payload["task_evidence"]) == 5
    assert all(
        item["agent_visible_mapping_identical_A_B"] is True
        for item in payload["task_evidence"].values()
    )


def test_real_pilot_fails_closed_without_chat_b_adapter() -> None:
    repo = Path(__file__).resolve().parents[2]
    script = repo / "experiments" / "tuned_debugger_pilot_v1" / "run_pilot.py"
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode != 0
    assert "waiting for frozen tuned adapter from Chat B" in (
        completed.stdout + completed.stderr
    )
