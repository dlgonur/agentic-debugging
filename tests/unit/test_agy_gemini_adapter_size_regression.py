"""Trajectory size regression for the AGY Gemini command adapter.

Reuses the accepted Local Application ``curated-none-handling-001``
``pdb-on-uncertainty`` reference reconstruction and the real configured-command
trajectory fixture.  Every request must construct an AGY ``--print`` command
that stays under the 30,000-character native command-line guard.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scripts import agy_gemini_command_adapter as adapter

sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))
import test_opencode_go_adapter_size_regression as _reference_size

MEASURED_REFERENCE_BYTES = _reference_size.MEASURED_REFERENCE_BYTES
build_measured_reference_trajectory = _reference_size.build_measured_reference_trajectory
run_real_trajectory = _reference_size.run_real_trajectory

REPRESENTATIVE_EXECUTABLE = r"C:\Users\benya\AppData\Local\agy\bin\agy.exe"
REPRESENTATIVE_SCHEMA = (
    r"C:\Users\benya\AppData\Local\Temp\agy-gemini-run-abcdefgh\workspace\directive-schema.json"
)


def _agy_command_line(message: str) -> str:
    command = adapter.build_agy_command(
        REPRESENTATIVE_EXECUTABLE,
        adapter.FIRST_RUN_MODEL_ID,
        message,
        REPRESENTATIVE_SCHEMA,
        adapter.DEFAULT_TIMEOUT_SECONDS,
    )
    adapter.assert_command_is_fresh_print(command)
    return subprocess.list2cmdline(command)


def test_measured_21_request_trajectory_fits_agy_command_line() -> None:
    requests, sizes = build_measured_reference_trajectory()
    assert len(requests) == 21
    assert sizes == MEASURED_REFERENCE_BYTES

    max_canonical = 0
    max_prompt = 0
    max_command_line = 0
    for request in requests:
        message = adapter.build_protocol_message(request)
        canonical = len(adapter.canonical_public_request(request).encode("utf-8"))
        command_line = _agy_command_line(message)
        max_canonical = max(max_canonical, canonical)
        max_prompt = max(max_prompt, len(message.encode("utf-8")))
        max_command_line = max(max_command_line, len(command_line))

    assert max_canonical == 23_824
    assert max_canonical <= adapter.MAX_PUBLIC_REQUEST_BYTES
    assert max_prompt < adapter.MAX_NATIVE_COMMAND_LINE_CHARS
    assert max_command_line <= adapter.MAX_NATIVE_COMMAND_LINE_CHARS
    assert max_command_line > max_prompt
    adapter._MAX_MEASURED_PROMPT_BYTES = max_prompt
    adapter._MAX_MEASURED_COMMAND_LINE_CHARS = max_command_line


def test_ceiling_plus_one_fails_closed_on_measured_max() -> None:
    requests, _sizes = build_measured_reference_trajectory()
    last = dict(requests[-1])
    last["_pad"] = ""
    current = len(adapter.canonical_public_request(last).encode("utf-8"))
    last["_pad"] = "x" * (adapter.MAX_PUBLIC_REQUEST_BYTES + 1 - current)
    assert len(adapter.canonical_public_request(last).encode("utf-8")) == adapter.MAX_PUBLIC_REQUEST_BYTES + 1
    with pytest.raises(ValueError, match="exceeds the Local Application ceiling"):
        adapter.build_protocol_message(last)


def test_real_configured_command_trajectory_requests_all_build(tmp_path: Path) -> None:
    requests, result = run_real_trajectory(tmp_path)
    assert requests, "recording transport received no requests"
    from agentic_debugger.agent.state_machine import ControllerState

    assert result.final_state is ControllerState.DONE, (
        f"real trajectory did not complete: {result.final_state.value} ({result.stop_reason})"
    )
    assert result.model_calls == len(requests)
    assert len(requests) >= 21

    max_canonical = 0
    max_prompt = 0
    max_command_line = 0
    for request in requests:
        message = adapter.build_protocol_message(request)
        canonical = len(adapter.canonical_public_request(request).encode("utf-8"))
        command_line = _agy_command_line(message)
        max_canonical = max(max_canonical, canonical)
        max_prompt = max(max_prompt, len(message.encode("utf-8")))
        max_command_line = max(max_command_line, len(command_line))

    adapter._REAL_TRAJECTORY = {
        "request_count": len(requests),
        "max_canonical_request_bytes": max_canonical,
        "max_constructed_prompt_bytes": max_prompt,
        "max_simulated_command_line_chars": max_command_line,
    }
    assert max_canonical <= adapter.MAX_PUBLIC_REQUEST_BYTES
    assert max_prompt < adapter.MAX_NATIVE_COMMAND_LINE_CHARS
    assert max_command_line <= adapter.MAX_NATIVE_COMMAND_LINE_CHARS
