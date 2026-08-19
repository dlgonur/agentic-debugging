"""Repair-14 V6 direct-entrypoint, classification, and provenance regressions."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry
from agentic_debugger.application.command_transport import CancellableJsonlCommandTransport
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import (
    LiveModelAdapter,
    LiveModelAdapterError,
    LiveModelConfig,
    LiveRunLimits,
    LiveTransportError,
    parse_command_adapter_error,
    parse_provider_generation_started,
)
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.swerebench.devqual_v6 import (
    DEVQUAL_FROZEN_DIR,
    load_devqual_contract,
    validate_devqual_identity,
)
from scripts import gpt_oss_swerebench_v2_pilot10 as pilot
from scripts import gpt_oss_swerebench_v2_devqual10_v6 as v6
from scripts import ollama_cloud_command_adapter as adapter


ROOT = Path(__file__).resolve().parents[2]
ADAPTER_SCRIPT = ROOT / "scripts" / "ollama_cloud_command_adapter.py"
TASK_PATH = ROOT / "agentic_debugger" / "datasets" / "curated" / "curated-none-handling-001" / "task.json"


def _snapshot(task: DebugTask) -> ControllerSnapshot:
    return ControllerSnapshot(
        "repair-14", task.task_id, ControllerState.REPRODUCE, 0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(), HypothesisLedger(),
    )


def _adapter_for(transport, *, max_retries: int = 0) -> LiveModelAdapter:
    task = DebugTask.from_mapping(json.loads(TASK_PATH.read_text(encoding="utf-8")))
    return LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig("test", ("test",)),
        transport=transport,
        limits=LiveRunLimits(max_model_requests=max_retries + 1, max_retries=max_retries),
        registry=ToolRegistry(),
    )


def _run_adapter_direct(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ADAPTER_SCRIPT), *args],
        cwd=Path(__file__).resolve().parent,
        input="",
        capture_output=True,
        text=True,
        check=False,
    )


def _transport(command: tuple[str, ...]) -> CancellableJsonlCommandTransport:
    return CancellableJsonlCommandTransport(
        LiveModelConfig("synthetic", command), max_output_bytes=4096
    )


def _stderr_command(text: str, code: int = 1) -> tuple[str, ...]:
    return (
        sys.executable,
        "-c",
        f"import sys; sys.stderr.write({text!r}); sys.exit({code})",
    )


def test_actual_adapter_direct_script_entrypoint_is_import_safe_and_typed():
    completed = _run_adapter_direct("--model", "unsupported-model")
    assert completed.returncode == 1
    assert "Traceback" not in completed.stderr
    assert "ModuleNotFoundError" not in completed.stderr
    payload = json.loads(completed.stderr)
    assert payload == {
        "schema_version": "live-command-error-v1",
        "kind": "configuration",
        "message": "requested Ollama Cloud model is not supported",
    }


def test_adapter_module_import_remains_available():
    assert adapter.MODEL_ID == "gpt-oss:20b-cloud"
    assert adapter.MAX_PUBLIC_REQUEST_BYTES == 131072


def test_generation_marker_is_bounded_and_parser_accepts_marker_plus_typed_error():
    marker = json.dumps({
        "schema_version": "live-command-event-v1",
        "event": "provider_generation_started",
    }, separators=(",", ":"))
    error = json.dumps({
        "schema_version": "live-command-error-v1",
        "kind": "http_error",
        "message": "bounded provider failure",
    }, separators=(",", ":"))
    stderr = marker + "\n" + error + "\n"
    assert parse_provider_generation_started(stderr) is True
    assert parse_command_adapter_error(stderr) == ("http_error", "bounded provider failure")


def test_chat_marker_is_emitted_only_at_generation_boundary(monkeypatch):
    captured = {}

    def fake_request(_endpoint, _method, _suffix, *, body, timeout_seconds):
        captured.update(body)
        return {"model": "gpt-oss:20b", "done": True}

    monkeypatch.setattr(adapter, "_http_json_request", fake_request)
    stderr = io.StringIO()
    adapter._chat_request(
        "http://127.0.0.1:11434/api",
        {"protocol": {"name": adapter.PROTOCOL_NAME, "version": adapter.PROTOCOL_VERSION, "logical_model_call_index": 0}, "task": {}, "controller": {}},
        adapter.CLOUD_MODELS[adapter.MODEL_ID],
        timeout_seconds=1,
        stderr_stream=stderr,
    )
    assert captured["model"] == adapter.MODEL_ID
    assert json.loads(stderr.getvalue()) == {
        "event": "provider_generation_started",
        "schema_version": "live-command-event-v1",
    }


def test_pre_generation_process_error_has_zero_generation_calls():
    transport = _transport(_stderr_command("local bootstrap failure\n"))
    with pytest.raises(LiveTransportError) as raised:
        transport.request({}, 5)
    assert raised.value.kind == "process_error"
    assert transport.last_provider_generation_started is False
    live = _adapter_for(transport)
    with pytest.raises(LiveModelAdapterError):
        live.next_directive(_snapshot(live.task))
    assert live.metrics.provider_generation_calls == 0
    assert live.metrics.provider_errors == 0
    assert live.metrics.setup_error_kinds == ["process_error"]
    assert live.metrics.termination_reason == "setup_failure"


def test_generation_marker_counts_once_and_transport_attempts_stay_distinct():
    marker = json.dumps({
        "schema_version": "live-command-event-v1",
        "event": "provider_generation_started",
    })
    transport = _transport(_stderr_command(marker + "\n"))
    with pytest.raises(LiveTransportError) as raised:
        transport.request({}, 5)
    assert raised.value.kind == "process_error"
    assert transport.last_provider_generation_started is True
    row = {"runtime": {"transport_attempts": 3, "provider_generation_calls": 1}}
    assert pilot.provider_execution_truth([row]) == {
        "provider_execution_authorized": True,
        "provider_inference_started": True,
        "tasks_with_transport_attempts": 1,
        "transport_attempts": 3,
        "provider_generation_calls": 1,
    }


def test_each_marked_retry_counts_as_one_generation_boundary():
    class RetryingProviderTransport:
        def __init__(self):
            self.calls = 0
            self.last_provider_generation_started = False

        def request(self, _payload, _timeout):
            self.calls += 1
            self.last_provider_generation_started = True
            error = LiveTransportError("bounded provider failure", kind="http_error")
            error.adapter_error = True
            raise error

    transport = RetryingProviderTransport()
    live = _adapter_for(transport, max_retries=2)
    with pytest.raises(LiveModelAdapterError):
        live.next_directive(_snapshot(live.task))
    assert transport.calls == 3
    assert live.metrics.model_requests == 3
    assert live.metrics.provider_errors == 3
    assert live.metrics.provider_generation_calls == 3


@pytest.mark.parametrize("kind", ["http_error", "timeout"])
def test_typed_provider_errors_remain_provider_evidence(kind):
    envelope = json.dumps({
        "schema_version": "live-command-error-v1",
        "kind": kind,
        "message": "bounded provider failure",
    })
    live = _adapter_for(_transport(_stderr_command(envelope)))
    with pytest.raises(LiveModelAdapterError):
        live.next_directive(_snapshot(live.task))
    assert live.metrics.provider_errors == 1
    assert live.metrics.provider_error_kinds == [kind]


def test_terminal_request_timeout_remains_provider_evidence():
    class TimeoutTransport:
        last_provider_generation_started = False

        def request(self, _payload, _timeout):
            raise LiveTransportError("timed out", kind="request_timeout", timed_out=True)

    live = _adapter_for(TimeoutTransport())
    with pytest.raises(LiveModelAdapterError):
        live.next_directive(_snapshot(live.task))
    assert live.metrics.provider_errors == 1
    assert live.metrics.provider_error_kinds == ["request_timeout"]
    assert live.metrics.termination_reason == "request_timeout"


def test_typed_model_output_error_remains_model_evidence():
    class OutputTransport:
        last_provider_generation_started = False

        def request(self, _payload, _timeout):
            error = LiveTransportError("invalid output", kind="invalid_directive")
            error.adapter_error = True
            raise error

    live = _adapter_for(OutputTransport())
    with pytest.raises(LiveModelAdapterError):
        live.next_directive(_snapshot(live.task))
    assert live.metrics.provider_errors == 0
    assert live.metrics.invalid_model_responses == 1
    assert live.metrics.termination_reason == "invalid_model_response"


def test_v6_is_direct_only_and_reaches_normal_runner_after_cheap_guards(monkeypatch, tmp_path):
    source = Path(v6.__file__).read_text(encoding="utf-8")
    assert "--preflight" not in source
    assert "run_task_preflight" not in source
    assert "run_official_infrastructure_gate" not in source
    calls = {}
    monkeypatch.setattr(v6, "_cheap_guards", lambda _args: ({"harness_sha256": "h" * 64, "runtime_git_head": "g" * 40}, "f" * 64))
    monkeypatch.setattr(v6, "_run_authorized_pilot10", lambda args, frozen, **kwargs: calls.update(kwargs) or 0)
    assert v6.main([
        "execute", "--live", "--config-root", str(tmp_path / "config"),
        "--external-root", str(tmp_path / "campaign"),
    ]) == 0
    assert calls["readiness_mode"] == "direct"
    assert calls["rows_filename"] == "devqual10_v6_rows.json"


def test_v6_identity_contract_and_treatment_values_are_frozen():
    contract = load_devqual_contract()
    identity = validate_devqual_identity()
    assert identity["experiment_id"] == "gpt_oss_swerebench_v2_devqual10_v6"
    assert contract["provider_generation_calls"] == 0
    assert contract["direct_execution_contract"] == {
        "explicit_live_flag": "--live",
        "execution_mode": "direct",
        "preflight_command": False,
        "readiness_command": False,
        "authorize_command": False,
        "ten_task_readiness_campaign": False,
        "official_verifier_sole_correctness_authority": True,
    }
    assert contract["provider"]["alias"] == "gpt-oss:20b-cloud"
    assert contract["provider"]["upstream"] == "gpt-oss:20b"
    assert contract["provider"]["reasoning_effort"] == "high"
    assert contract["provider"]["request_timeout_seconds"] == 60
    assert contract["request_envelope"] == {
        "canonical_public_request_bytes": 131072,
        "stdin_request_bytes": 196608,
        "http_request_body_bytes": 262144,
        "raw_response_bytes": 65536,
    }
    assert len(json.loads((DEVQUAL_FROZEN_DIR / "pilot10_manifest.json").read_text(encoding="utf-8"))["tasks"]) == 10


def test_historical_v1_to_v5_frozen_files_are_untouched():
    roots = [
        ROOT / "experiments" / name / "frozen"
        for name in [
            "gpt_oss_swerebench_v2_pilot10",
            "gpt_oss_swerebench_v2_devqual10_v2",
            "gpt_oss_swerebench_v2_devqual10_v3",
            "gpt_oss_swerebench_v2_devqual10_v4",
            "gpt_oss_swerebench_v2_devqual10_v5",
        ]
    ]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for root in roots for p in root.rglob("*") if p.is_file()}
    validate_devqual_identity()
    after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for root in roots for p in root.rglob("*") if p.is_file()}
    assert before == after


def test_repair_14_provider_generation_count_is_zero():
    assert load_devqual_contract()["provenance"]["repair_provider_generation_calls"] == 0
