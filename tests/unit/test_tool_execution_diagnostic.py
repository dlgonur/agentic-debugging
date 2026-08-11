"""Unit tests for ToolExecutionError safe_diagnostic propagation (Repair D).

Verifies that ``ToolExecutionError`` now carries ``safe_diagnostic`` and
``ToolRegistry.dispatch`` extracts it through the existing
``_bounded_safe_diagnostic`` safety pipeline into the error observation
payload.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.controller_policy import ActionName
from agentic_debugger.agent.tool_registry import (
    ToolExecutionError,
    ToolRegistry,
    ToolSpec,
    ToolDispatchReason,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.events.schema import Action, ObservationStatus


def _make_action(name: ActionName, state: ControllerState = ControllerState.RUNTIME_EVIDENCE) -> Action:
    return Action(
        action_id="act-1",
        run_id="r",
        task_id="t",
        name=name.value,
        state=state,
        arguments={},
    )


def _always_reject_validator(args):
    return args


def _raising_handler(action, args):
    raise ToolExecutionError(
        "breakpoint line 20 exceeds source length (19)",
        safe_diagnostic="breakpoint line 20 exceeds source length (19)",
    )


def _no_diagnostic_handler(action, args):
    raise ToolExecutionError("some failure without diagnostic")


class TestToolExecutionErrorDiagnostic:
    def test_safe_diagnostic_field_exists(self):
        err = ToolExecutionError("msg", safe_diagnostic="diag")
        assert err.safe_diagnostic == "diag"

    def test_safe_diagnostic_defaults_none(self):
        err = ToolExecutionError("msg")
        assert err.safe_diagnostic is None

    def test_dispatch_extracts_diagnostic_into_payload(self):
        spec = ToolSpec(
            ActionName.START_PDB_SESSION,
            _always_reject_validator,
            _raising_handler,
            version="test-1",
            argument_contract={"required": [], "properties": {}},
        )
        registry = ToolRegistry((spec,))
        action = _make_action(ActionName.START_PDB_SESSION)
        obs = registry.dispatch(action, observation_id="obs-1")

        assert obs.status == ObservationStatus.ERROR
        payload = obs.payload
        assert payload.get("dispatch_reason") == ToolDispatchReason.TOOL_ERROR.value
        assert "diagnostic" in payload
        assert "breakpoint line 20 exceeds source length (19)" in payload["diagnostic"]

    def test_dispatch_without_diagnostic(self):
        spec = ToolSpec(
            ActionName.START_PDB_SESSION,
            _always_reject_validator,
            _no_diagnostic_handler,
            version="test-1",
            argument_contract={"required": [], "properties": {}},
        )
        registry = ToolRegistry((spec,))
        action = _make_action(ActionName.START_PDB_SESSION)
        obs = registry.dispatch(action, observation_id="obs-2")

        assert obs.status == ObservationStatus.ERROR
        payload = obs.payload
        assert payload.get("dispatch_reason") == ToolDispatchReason.TOOL_ERROR.value
        # No diagnostic key when safe_diagnostic is None
        assert "diagnostic" not in payload

    def test_secret_redaction_still_applies(self):
        """The existing _bounded_safe_diagnostic redaction must still apply."""
        spec = ToolSpec(
            ActionName.START_PDB_SESSION,
            _always_reject_validator,
            lambda action, args: _raise_with_secret(),
            version="test-1",
            argument_contract={"required": [], "properties": {}},
        )
        registry = ToolRegistry((spec,))
        action = _make_action(ActionName.START_PDB_SESSION)
        obs = registry.dispatch(action, observation_id="obs-3")

        payload = obs.payload
        diagnostic = payload.get("diagnostic", "")
        assert "sk-secret123" not in diagnostic
        assert "<redacted>" in diagnostic


def _raise_with_secret():
    raise ToolExecutionError(
        "failure with secret",
        safe_diagnostic="error: api_key=sk-secret123 in the diagnostic",
    )