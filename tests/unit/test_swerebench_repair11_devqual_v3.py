"""Repair-11 zero-provider regressions for the V3 treatment boundary."""

from __future__ import annotations

import json
import io
import sys
from pathlib import Path

import pytest

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    ControllerSnapshot,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry, ToolResult, ToolSpec
from agentic_debugger.application.command_transport import CancellableJsonlCommandTransport
from agentic_debugger.evaluation.live import (
    JsonlCommandTransport,
    LiveModelAdapter,
    LiveModelConfig,
    LiveModelAdapterError,
    LiveRunLimits,
    LiveTransportError,
    _legal_transition_targets,
)
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.events.schema import ObservationStatus
from scripts import ollama_cloud_command_adapter as ollama


ROOT = Path(__file__).resolve().parents[2]


def _command_with_stderr(stderr: str, code: int = 1) -> tuple[str, ...]:
    return (
        sys.executable,
        "-c",
        f"import sys; sys.stderr.write({stderr!r}); sys.exit({code})",
    )


def test_command_adapter_error_envelope_is_machine_readable_and_bounded():
    envelope = {
        "schema_version": "live-command-error-v1",
        "kind": "invalid_directive",
        "message": "directive rejected",
    }
    transport = JsonlCommandTransport(
        LiveModelConfig("adapter", _command_with_stderr(json.dumps(envelope))),
        max_output_bytes=1024,
    )
    with pytest.raises(LiveTransportError) as raised:
        transport.request({}, 5)
    assert raised.value.kind == "invalid_directive"
    assert raised.value.adapter_error is True
    assert len(raised.value.adapter_error_message) <= 256


def test_ollama_adapter_emits_one_bounded_error_envelope():
    stderr = io.StringIO()
    stdout = io.StringIO()
    assert ollama.run_adapter(io.StringIO("{}\n"), stdout, stderr, argv=["--model", ollama.MODEL_ID]) == 1
    payload = json.loads(stderr.getvalue())
    assert set(payload) == {"schema_version", "kind", "message"}
    assert payload["schema_version"] == "live-command-error-v1"
    assert payload["kind"] == "invalid_request"
    assert len(stderr.getvalue().encode("utf-8")) <= 1024


def test_unknown_stderr_remains_generic_process_error():
    transport = JsonlCommandTransport(
        LiveModelConfig("adapter", _command_with_stderr("human stderr\n")),
        max_output_bytes=1024,
    )
    with pytest.raises(LiveTransportError) as raised:
        transport.request({}, 5)
    assert raised.value.kind == "process_error"
    assert raised.value.adapter_error is False


def test_cancellable_transport_preserves_typed_adapter_error():
    envelope = json.dumps({
        "schema_version": "live-command-error-v1",
        "kind": "http_error",
        "message": "bounded provider failure",
    })
    transport = CancellableJsonlCommandTransport(
        LiveModelConfig("adapter", _command_with_stderr(envelope)),
        max_output_bytes=1024,
    )
    with pytest.raises(LiveTransportError) as raised:
        transport.request({}, 5)
    assert raised.value.kind == "http_error"
    assert raised.value.adapter_error is True


class _ExternalTask:
    class _Source:
        kind = "external"

    class _Isolation:
        hide_test_identities_from_model = True

    source = _Source()
    evaluation_isolation = _Isolation()
    task_id = "external-repair-11"

    def agent_visible_mapping(self):
        return {
            "task_id": self.task_id,
            "source": {"kind": "external"},
            "description": "public issue",
        }


def _snapshot(state: ControllerState, *, last_observation=None) -> ControllerSnapshot:
    return ControllerSnapshot(
        "repair-11", "external-repair-11", state, 0,
        ControllerBudgetLimits(3, 12, 0, max_source_observations=12),
        ControllerBudgetState(), HypothesisLedger(), last_observation=last_observation,
    )


def _source_registry() -> ToolRegistry:
    def validator(arguments):
        return arguments

    def handler(_action, arguments):
        return ToolResult(
            status=ObservationStatus.OK,
            payload={"path": arguments.get("path", "pkg/module.py"), "source": "def f(): pass"},
            summary="source window",
        )

    return ToolRegistry((ToolSpec(ActionName.GET_SOURCE_WINDOW, validator, handler),))


def test_controller_rejects_direct_external_understand_to_patch_before_context():
    controller = DeterministicController(
        ToolRegistry(),
        type("Adapter", (), {"next_directive": lambda self, snapshot: TransitionDirective(
            ControllerState.PATCH if snapshot.state is ControllerState.UNDERSTAND else ControllerState.UNDERSTAND,
            "direct",
        )})(),
        ControllerRunConfig(2, require_external_source_context=True),
    )
    result = controller.run(_snapshot(ControllerState.REPRODUCE))
    assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
    assert result.final_state is ControllerState.FAILED


def test_successful_get_source_window_unlocks_external_patch_transition():
    class Adapter:
        def next_directive(self, snapshot):
            if snapshot.last_observation is None:
                return ActionDirective(ActionName.GET_SOURCE_WINDOW, {"path": "pkg/module.py", "line": 1})
            if snapshot.state is ControllerState.PATCH:
                return TransitionDirective(ControllerState.FAILED, "gate test complete")
            return TransitionDirective(ControllerState.PATCH, "source observed")

    # Start in Understand so the first action establishes authoritative source
    # evidence and the second directive attempts the transition.
    controller = DeterministicController(
        _source_registry(), Adapter(), ControllerRunConfig(3, require_external_source_context=True)
    )
    result = controller.run(_snapshot(ControllerState.UNDERSTAND))
    assert any(step.state_after is ControllerState.PATCH for step in result.steps)


def test_search_and_symbol_metadata_do_not_unlock_patch():
    assert ControllerState.PATCH.value not in _legal_transition_targets(
        ControllerState.UNDERSTAND, external_source_context_observed=False
    )


@pytest.mark.parametrize("kind", ["invalid_directive", "invalid_response", "invalid_completion", "tool_call_rejected"])
def test_typed_model_output_error_enters_invalid_response_lifecycle(kind):
    class Transport:
        def request(self, _payload, _timeout):
            error = LiveTransportError("typed", kind=kind)
            error.adapter_error = True
            raise error

    adapter = LiveModelAdapter(
        task=_ExternalTask(), policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig("test", ("test",)), transport=Transport(),
        limits=LiveRunLimits(max_model_requests=1, max_retries=0), registry=_source_registry(),
    )
    with pytest.raises(LiveModelAdapterError):
        adapter.next_directive(_snapshot(ControllerState.UNDERSTAND))
    assert adapter.metrics.provider_errors == 0
    assert kind in adapter.metrics.adapter_error_kinds
    assert adapter.metrics.termination_reason == "invalid_model_response"


def test_typed_setup_error_is_fail_closed_without_provider_outage_accounting():
    class Transport:
        def request(self, _payload, _timeout):
            error = LiveTransportError("typed", kind="configuration")
            error.adapter_error = True
            raise error

    adapter = LiveModelAdapter(
        task=_ExternalTask(), policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig("test", ("test",)), transport=Transport(),
        limits=LiveRunLimits(max_model_requests=1, max_retries=0), registry=_source_registry(),
    )
    with pytest.raises(LiveModelAdapterError):
        adapter.next_directive(_snapshot(ControllerState.UNDERSTAND))
    assert adapter.metrics.provider_errors == 0
    assert adapter.metrics.setup_error_kinds == ["configuration"]
    assert adapter.metrics.termination_reason == "setup_failure"


@pytest.mark.parametrize("kind", ["http_error", "timeout"])
def test_typed_provider_error_retains_provider_accounting(kind):
    class Transport:
        def request(self, _payload, _timeout):
            error = LiveTransportError("typed", kind=kind)
            error.adapter_error = True
            raise error

    adapter = LiveModelAdapter(
        task=_ExternalTask(), policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig("test", ("test",)), transport=Transport(),
        limits=LiveRunLimits(max_model_requests=1, max_retries=0), registry=_source_registry(),
    )
    with pytest.raises(LiveModelAdapterError):
        adapter.next_directive(_snapshot(ControllerState.UNDERSTAND))
    assert adapter.metrics.provider_errors == 1
    assert kind in adapter.metrics.provider_error_kinds


def test_reasoning_effort_enum_and_high_chat_payload(monkeypatch):
    with pytest.raises(ollama.OllamaAdapterError):
        ollama.validate_reasoning_effort("extreme")
    captured = {}

    def fake_request(_endpoint, _method, _suffix, *, body, timeout_seconds):
        captured.update(body)
        return {"model": "gpt-oss:20b", "done": True, "done_reason": "stop", "message": {"role": "assistant", "content": "{}"}}

    monkeypatch.setattr(ollama, "_http_json_request", fake_request)
    ollama._chat_request(
        "http://127.0.0.1:11434/api", {"task": {}, "controller": {}},
        ollama.CLOUD_MODELS[ollama.MODEL_ID], timeout_seconds=1, reasoning_effort="high",
    )
    assert captured["think"] == "high"


def test_v3_validate_and_identity_are_zero_provider_and_first_ten_bound():
    import scripts.gpt_oss_swerebench_v2_devqual10_v3 as v3

    assert v3.REASONING_EFFORT == "high"
    identity = v3.validate_devqual_identity()
    assert len(identity["first_ten_instance_ids"]) == 10
    assert v3.load_devqual_contract()["devqual_v3_zero_provider_contract"]["preflight_generation_calls"] == 0
