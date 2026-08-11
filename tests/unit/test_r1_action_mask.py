"""R1.1 NOT_STARTED breakpoint-selection action-mask tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.state_machine import ControllerState
from experiments.debugger_interaction_v2_r1.bridge import (
    BridgeParseError,
    BridgeRejection,
    DebuggerLifecycle,
    parse,
    visible_commands,
)


def test_not_started_only_break_and_failed_are_visible():
    commands = visible_commands(
        ControllerState.RUNTIME_EVIDENCE, DebuggerLifecycle.NOT_STARTED
    )
    assert commands == ("break", "failed")


@pytest.mark.parametrize("command", [
    "source recent_window.py 2",
    "diagnosis inspect",
    "patch",
    "understand",
])
def test_not_started_hidden_commands_are_rejected(command):
    with pytest.raises(BridgeParseError) as exc_info:
        parse(
            command,
            ControllerState.RUNTIME_EVIDENCE,
            lifecycle=DebuggerLifecycle.NOT_STARTED,
        )
    assert exc_info.value.category == BridgeRejection.COMMAND_NOT_IN_LIFECYCLE


def test_not_started_break_remains_model_authored():
    result = parse(
        "break 9",
        ControllerState.RUNTIME_EVIDENCE,
        lifecycle=DebuggerLifecycle.NOT_STARTED,
    )
    assert result.command_token == "break"
    assert result.directive.arguments == {"breakpoint_line": 9}


def test_paused_mask_retains_debugger_commands():
    commands = visible_commands(
        ControllerState.RUNTIME_EVIDENCE, DebuggerLifecycle.PAUSED
    )
    for command in ("stack", "locals", "print", "step", "next", "continue", "stop"):
        assert command in commands
    assert "break" not in commands
