"""Outer-cancellation process-tree tests for the AGY Gemini adapter.

Topology:

    Local Application CancellableJsonlCommandTransport
        -> adapter process
            -> synthetic AGY child
                -> descendant
                    -> grandchild

After outer cancellation: CancellationError, adapter dead, AGY child dead,
descendants dead.  Readiness markers, no fixed sleeps.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agentic_debugger.application.command_transport import CancellableJsonlCommandTransport
from agentic_debugger.application.process_tree import pid_is_alive
from agentic_debugger.cancellation import CancellationError, CancellationReason
from agentic_debugger.evaluation.live import LiveModelConfig
from scripts import agy_gemini_synthetic_executable as synthetic

SYNTHETIC_SCRIPT = REPO_ROOT / "scripts" / "agy_gemini_synthetic_executable.py"
ADAPTER_SCRIPT = REPO_ROOT / "scripts" / "agy_gemini_command_adapter.py"


def _request() -> dict[str, Any]:
    return {
        "protocol": {
            "name": "agentic-debugger-live-jsonl",
            "version": "1.3",
            "request_id": "external-cancel:model-call:1:attempt:1:uuid-x",
            "logical_model_call_index": 1,
            "transport_attempt_index": 1,
        },
        "identity": {
            "evaluation_id": "eval-external-cancel",
            "case_id": "eval-external-cancel:curated-none-handling-001",
            "run_id": "run-external-cancel",
            "trajectory_id": "run-external-cancel",
        },
        "task": {"task_id": "curated-none-handling-001", "instruction": "probe"},
        "policy": "pdb-on-uncertainty",
        "directive_schema": ["action", "transition"],
        "action_contracts": {
            "run_reproduction": {
                "properties": {"phase": {"type": "string", "enum": ["baseline", "post_patch"]}},
                "required": ["phase"],
                "additional_properties": False,
            }
        },
        "controller": {
            "state": "Reproduce",
            "task_id": "curated-none-handling-001",
            "model_call_index": 1,
            "allowed_actions": ["run_reproduction"],
            "legal_transition_targets": ["Understand", "Failed"],
            "budget_limits": {
                "max_patch_attempts": 3,
                "max_test_runs": 10,
                "max_pdb_observations": 15,
                "max_active_hypotheses": 3,
                "max_source_observations": 10,
            },
            "budget_state": {
                "patch_attempts": 0,
                "test_runs": 0,
                "pdb_observations": 0,
                "source_observations": 0,
            },
            "hypotheses": [],
            "last_observation": None,
        },
        "history": [],
        "directive_feedback": None,
        "instructions": "Return one directive JSON object.",
        "synthetic_scenario": "external-cancel-tree",
    }


def _fake_agy(tmp_path: Path) -> str:
    if sys.platform == "win32":
        fake_bin = tmp_path / "fake-agy"
        fake_bin.mkdir(parents=True, exist_ok=True)
        return str(synthetic.build_fake_agy_executable(fake_bin, target_script=SYNTHETIC_SCRIPT))
    executable = tmp_path / "agy"
    shutil.copy2(SYNTHETIC_SCRIPT, executable)
    executable.write_bytes(executable.read_bytes().replace(b"\r\n", b"\n"))
    executable.chmod(0o755)
    return str(executable)


def test_external_transport_cancel_kills_adapter_child_and_grandchild(tmp_path: Path) -> None:
    executable = _fake_agy(tmp_path)
    work_root = tmp_path / "work-root"
    work_root.mkdir()
    command = [
        sys.executable,
        str(ADAPTER_SCRIPT),
        "--executable", executable,
        "--model", "gemini-3.7-flash-medium",
        "--expected-version", synthetic.SYNTHETIC_VERSION,
        "--timeout", "120",
        "--work-root", str(work_root),
        "--max-logical-model-calls", "25",
    ]
    config = LiveModelConfig(
        model_name="AGY Gemini external-cancel fixture",
        command=command,
        request_timeout_seconds=120.0,
    )
    marker_seen = threading.Event()
    marker: dict[str, Any] = {}

    def cancel_check() -> None:
        if marker_seen.is_set():
            raise CancellationError(CancellationReason.CANCELLED)

    transport = CancellableJsonlCommandTransport(
        config,
        max_output_bytes=65536,
        cancel_check=cancel_check,
    )
    outcome: dict[str, Any] = {}

    def drive_request() -> None:
        try:
            transport.request(_request(), timeout_seconds=120.0)
            outcome["result"] = "returned"
        except BaseException as exc:  # noqa: BLE001
            outcome["result"] = type(exc).__name__
            outcome["detail"] = str(exc)

    thread = threading.Thread(target=drive_request, daemon=True)
    thread.start()

    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if not thread.is_alive():
            break
        candidates = list(work_root.rglob("agy-gemini-synthetic-tree-*.json"))
        if candidates:
            try:
                marker.update(json.loads(candidates[0].read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                marker.clear()
            if marker.get("grandchild_pid"):
                marker_seen.set()
                break
        time.sleep(0.1)

    assert marker_seen.is_set(), f"readiness marker never appeared; outcome={outcome}"
    adapter_pid = int(marker["adapter_pid"])
    agy_pid = int(marker["agy_pid"])
    child_pid = int(marker["child_pid"])
    grandchild_pid = int(marker["grandchild_pid"])
    assert adapter_pid > 0 and agy_pid > 0 and child_pid > 0 and grandchild_pid > 0
    assert pid_is_alive(adapter_pid), "adapter exited before cancellation"
    assert pid_is_alive(agy_pid), "AGY child exited before cancellation"
    assert pid_is_alive(child_pid), "child exited before cancellation"
    assert pid_is_alive(grandchild_pid), "grandchild exited before cancellation"

    marker_seen.set()
    thread.join(timeout=60.0)
    assert not thread.is_alive(), "request did not return within the bounded window"
    assert outcome.get("result") == "CancellationError", f"outcome={outcome}"

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and any(
        pid_is_alive(pid)
        for pid in (adapter_pid, agy_pid, child_pid, grandchild_pid)
    ):
        time.sleep(0.1)
    assert not pid_is_alive(adapter_pid), "adapter survived external cancellation"
    assert not pid_is_alive(agy_pid), "AGY child survived external cancellation"
    assert not pid_is_alive(child_pid), "child survived external cancellation"
    assert not pid_is_alive(grandchild_pid), "grandchild survived external cancellation"
