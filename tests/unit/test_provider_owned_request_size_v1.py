"""Task 43 — Provider-Owned Model Request Size v1.

Repository-wide runtime rule: Agentic Debugger must never reject, truncate,
compact, shorten, or otherwise alter a model request solely because Agentic
Debugger considers the request too large.  Request-size authority belongs to
the selected model/provider/transport: Agentic Debugger sends the intended
request, and a provider that rejects it for its actual context/request
limits surfaces a provider failure truthfully.

Candidate-48 ceilings removed by this task (exact historical values, kept as
deprecated provenance-only constants, never enforced):

* 20,000 bytes — frozen public-evidence budget (QuixBugs/OpenCode campaign
  transport, live-adapter RAG gate);
* 25,000 bytes — Local Application OpenCode/AGY stdin + shaping ceilings;
* 32,768 bytes — CommandCode/direct-API stdin + shared shaping ceiling
  (the owner-observed ``request exceeds the public request ceiling``
  failure at Level-32 Model Request #8);
* 1,048,576 bytes — ``MAX_MODEL_RESPONSE_BYTES`` misapplied as a request
  gate in the live adapter;
* 4,194,304 bytes — ``_MAX_REQUEST_BYTES`` request-body ceiling in the
  common provider HTTP client (``provider_http._body_bytes``), removed by
  the Candidate-50 repair;
* 30,000 characters — native command-line preflight on CLI-arg routes
  (physical representability is now judged by the host OS at process
  creation, whose failures surface as launch failures, never as policy).

Every test below uses the real production seam (real ``LiveModelAdapter``
request construction, real adapter shaping/parsing functions) with a
deterministic fake transport — never a monkeypatched model boundary — and a
deterministic request larger than the old thresholds.  Each fails against
Candidate 48.  No external provider is contacted.
"""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agentic_debugger.agent.controller_policy import (  # noqa: E402
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot  # noqa: E402
from agentic_debugger.agent.state_machine import ControllerState  # noqa: E402
from agentic_debugger.agent.tool_registry import (  # noqa: E402
    ActionName,
    ObservationStatus,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from agentic_debugger.demo.policies import DemoPolicy  # noqa: E402
from agentic_debugger.evaluation.live import (  # noqa: E402
    LiveModelAdapter,
    LiveModelAdapterError,
    LiveModelConfig,
    LiveRunLimits,
    LiveTransportError,
)
from agentic_debugger.evaluation.task_schema import DebugTask  # noqa: E402
from agentic_debugger.events.schema import Observation  # noqa: E402

import commandcode_goat_adapter  # noqa: E402
import opencode_go_command_adapter  # noqa: E402
import agy_gemini_command_adapter  # noqa: E402
import opencode_protocol_transport  # noqa: E402
import opencode_provider_adapter  # noqa: E402
import provider_direct_api_adapter  # noqa: E402
import ollama_cloud_command_adapter  # noqa: E402
import protocol_prompt_shaper as prompt_shaper  # noqa: E402
from agentic_debugger.rag.schema import PUBLIC_REQUEST_BYTE_BUDGET  # noqa: E402

TASK_ID = "curated-none-handling-001"

# Every historical internal request-size threshold.  A deterministic request
# larger than the largest of these fails against Candidate 48 on every route
# and must flow to the transport after this task.
HISTORICAL_PUBLIC_EVIDENCE_BUDGET = 20_000
HISTORICAL_LOCAL_APP_CEILING = 25_000
HISTORICAL_SHARED_SHAPING_CEILING = 32_768
HISTORICAL_RESPONSE_BOUND_AS_REQUEST_GATE = 1_048_576
HISTORICAL_PROVIDER_HTTP_REQUEST_BOUND = 4 * 1024 * 1024
ABOVE_ALL_OLD_CEILINGS = 40_000


def _task() -> DebugTask:
    return DebugTask.from_file(
        str(REPO_ROOT / "agentic_debugger" / "datasets" / "curated" / TASK_ID / "task.json")
    )


def _config() -> LiveModelConfig:
    return LiveModelConfig("test-model", ("test-model-command",))


def _registry() -> ToolRegistry:
    return ToolRegistry(
        (
            ToolSpec(
                ActionName.RUN_REPRODUCTION,
                lambda arguments: dict(arguments),
                lambda _action, _arguments: ToolResult(ObservationStatus.OK, {}, "ok"),
                argument_contract={
                    "required": ["phase"],
                    "properties": {"phase": {"type": "string", "min_length": 1}},
                    "additional_properties": False,
                },
            ),
        )
    )


def _snapshot(model_call_index: int = 0) -> ControllerSnapshot:
    task = _task()
    return ControllerSnapshot(
        "run-size",
        task.task_id,
        ControllerState.REPRODUCE,
        model_call_index,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(),
        HypothesisLedger(),
    )


def _reproduce_directive() -> dict:
    return {
        "directive": {
            "kind": "action",
            "name": "run_reproduction",
            "arguments": {"phase": "baseline"},
        }
    }


class RecordingTransport:
    """Deterministic fake transport: records every payload, answers legally."""

    def __init__(self) -> None:
        self.calls = 0
        self.payloads: list[dict] = []

    def request(self, payload, timeout_seconds):
        self.calls += 1
        self.payloads.append(payload)
        return _reproduce_directive()


def test_historical_ceiling_values_are_exact() -> None:
    """Pin the exact Candidate-48 thresholds this task removes, so a silent
    redefinition is caught."""
    assert PUBLIC_REQUEST_BYTE_BUDGET == HISTORICAL_PUBLIC_EVIDENCE_BUDGET == 20_000
    assert opencode_protocol_transport.MAX_PUBLIC_EVIDENCE_BYTES == 20_000
    assert opencode_go_command_adapter.MAX_PUBLIC_REQUEST_BYTES == 25_000
    assert agy_gemini_command_adapter.MAX_PUBLIC_REQUEST_BYTES == 25_000
    assert prompt_shaper.MAX_PUBLIC_REQUEST_BYTES == 32_768
    assert ollama_cloud_command_adapter.MAX_PUBLIC_REQUEST_BYTES == 32_768
    from agentic_debugger.evaluation.live import MAX_MODEL_RESPONSE_BYTES

    assert MAX_MODEL_RESPONSE_BYTES == 1_048_576
    # The common-HTTP request-body ceiling is gone entirely (Candidate 50):
    # no request-size bound may remain in provider_http.
    from agentic_debugger.application import provider_http

    assert HISTORICAL_PROVIDER_HTTP_REQUEST_BOUND == 4 * 1024 * 1024
    assert not hasattr(provider_http, "_MAX_REQUEST_BYTES")


def test_large_request_reaches_transport_once_complete_and_continues() -> None:
    """Above every old sub-1MiB ceiling: constructed, threshold crossed, no
    internal ``request_too_large``, transport invoked once with the complete
    request, and execution continues on the provider answer."""
    transport = RecordingTransport()
    adapter = LiveModelAdapter(
        task=_task(),
        policy=DemoPolicy.STATIC_BASELINE,
        config=_config(),
        transport=transport,
        limits=LiveRunLimits(max_model_requests=4, max_retries=0),
        registry=_registry(),
        evaluation_id="eval-size",
        case_id="case-size",
        run_id="run-size",
        trajectory_id="run-size",
    )
    # Deterministic growth past every old ceiling: one padded history entry.
    adapter.history.append(
        {
            "request_index": 0,
            "state": ControllerState.REPRODUCE.value,
            "allowed_actions": [],
            "last_observation": {"evidence_blob": "x" * ABOVE_ALL_OLD_CEILINGS},
        }
    )
    directive = adapter.next_directive(_snapshot(0))
    assert directive.name is ActionName.RUN_REPRODUCTION
    assert transport.calls == 1
    assert adapter.metrics.termination_reason is None
    sent = transport.payloads[0]
    sent_bytes = json.dumps(sent, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert len(sent_bytes) > HISTORICAL_SHARED_SHAPING_CEILING
    assert len(sent_bytes) > HISTORICAL_LOCAL_APP_CEILING
    assert len(sent_bytes) > HISTORICAL_PUBLIC_EVIDENCE_BUDGET
    # No silent truncation to any old threshold: the intended evidence
    # arrives byte-for-byte.
    assert sent["history"][0]["last_observation"]["evidence_blob"] == "x" * ABOVE_ALL_OLD_CEILINGS
    assert adapter.metrics.max_request_bytes == len(sent_bytes)
    # Execution continues on the fake provider response.
    directive2 = adapter.next_directive(_snapshot(1))
    assert directive2.name is ActionName.RUN_REPRODUCTION
    assert transport.calls == 2


def test_request_above_old_1mib_gate_reaches_transport_complete() -> None:
    """The misapplied 1 MiB response-bound request gate is gone: a request
    larger than 1,048,576 bytes is handed to the transport in full."""
    blob = "x" * (HISTORICAL_RESPONSE_BOUND_AS_REQUEST_GATE + 1_024)
    task = _task()
    observation = Observation(
        observation_id="obs-huge-1",
        action_id="act-huge-1",
        run_id="run-size",
        task_id=task.task_id,
        name="run_reproduction",
        status=ObservationStatus.OK,
        payload={"phase": "baseline", "evidence_blob": blob},
        summary="baseline reproduction executed",
        truncated=False,
    )
    snapshot = ControllerSnapshot(
        "run-size",
        task.task_id,
        ControllerState.REPRODUCE,
        0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(),
        HypothesisLedger(),
        last_observation=observation,
    )
    transport = RecordingTransport()
    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=_config(),
        transport=transport,
        limits=LiveRunLimits(max_model_requests=2, max_retries=0),
        registry=_registry(),
        evaluation_id="eval-size",
        case_id="case-size",
        run_id="run-size",
        trajectory_id="run-size",
    )
    directive = adapter.next_directive(snapshot)
    assert directive.name is ActionName.RUN_REPRODUCTION
    assert transport.calls == 1
    assert adapter.metrics.termination_reason is None
    sent_bytes = json.dumps(
        transport.payloads[0], ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    assert len(sent_bytes) > HISTORICAL_RESPONSE_BOUND_AS_REQUEST_GATE
    assert transport.payloads[0]["controller"]["last_observation"]["payload"]["evidence_blob"] == blob


def test_provider_originated_size_rejection_surfaces_truthfully() -> None:
    """The opposite boundary: the large request IS sent, the provider
    rejects it for its own limits, and Agentic Debugger surfaces that
    provider failure truthfully (transport was invoked; no internal
    preflight; provider provenance preserved)."""

    class RejectingTransport(RecordingTransport):
        def request(self, payload, timeout_seconds):
            self.calls += 1
            self.payloads.append(payload)
            raise LiveTransportError(
                "Provider rejected request as too large (HTTP 413)",
                kind="request_too_large",
                safe_message="Provider rejected request as too large (HTTP 413)",
            )

    transport = RejectingTransport()
    adapter = LiveModelAdapter(
        task=_task(),
        policy=DemoPolicy.STATIC_BASELINE,
        config=_config(),
        transport=transport,
        limits=LiveRunLimits(max_model_requests=2, max_retries=0),
        registry=_registry(),
        evaluation_id="eval-size",
        case_id="case-size",
        run_id="run-size",
        trajectory_id="run-size",
    )
    adapter.history.append(
        {
            "request_index": 0,
            "state": ControllerState.REPRODUCE.value,
            "allowed_actions": [],
            "last_observation": {"evidence_blob": "x" * ABOVE_ALL_OLD_CEILINGS},
        }
    )
    with pytest.raises(LiveModelAdapterError) as info:
        adapter.next_directive(_snapshot(0))
    # The request was sent (provider received it and rejected it).
    assert transport.calls == 1
    sent_bytes = json.dumps(
        transport.payloads[0], ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    assert len(sent_bytes) > HISTORICAL_SHARED_SHAPING_CEILING
    # ...and the failure is classified as a provider/transport error with
    # the provider's size-rejection kind preserved — never an internal
    # preflight and never a directive rejection.
    assert adapter.metrics.termination_reason == "provider_or_transport_error"
    assert info.value.error_kind == "request_too_large"
    assert info.value.directive_rejection is False
    assert "Provider rejected request as too large" in (info.value.safe_message or "")


def _stdin_bytes(payload: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        buffer=io.BytesIO((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    )


def _large_protocol_request(size: int = ABOVE_ALL_OLD_CEILINGS) -> dict:
    return {
        "protocol": {"version": "1.3", "logical_model_call_index": 0},
        "context": {"task_id": "task", "state": "UNDERSTAND"},
        "evidence_blob": "x" * size,
    }


@pytest.mark.parametrize(
    "read_request",
    [
        commandcode_goat_adapter.read_request,
        opencode_provider_adapter.read_request,
        provider_direct_api_adapter.read_request,
    ],
)
def test_command_adapter_stdin_has_no_request_ceiling(read_request) -> None:
    """Every command-route stdin reader accepts a request above all old
    stdin ceilings (25,000 / 32,768) complete and untruncated."""
    payload = _large_protocol_request()
    result = read_request(_stdin_bytes(payload))
    assert result["evidence_blob"] == "x" * ABOVE_ALL_OLD_CEILINGS
    assert result["protocol"]["logical_model_call_index"] == 0


def test_ollama_stdin_has_no_request_ceiling() -> None:
    """The Ollama stdin reader accepts a request above even the old
    128 KiB stdin bound complete and untruncated."""
    payload = _large_protocol_request(size=140_000)
    stream = io.StringIO(json.dumps(payload, ensure_ascii=False) + "\n")
    result = ollama_cloud_command_adapter._read_request(stream)
    assert result["evidence_blob"] == "x" * 140_000


def test_shared_shaper_has_no_request_ceiling() -> None:
    """The shared shaping authority serializes a 40,000-byte request in
    full, and the deprecated bound argument is ignored."""
    payload = _large_protocol_request()
    canonical = prompt_shaper.canonical_public_request(payload)
    assert len(canonical.encode("utf-8")) > HISTORICAL_SHARED_SHAPING_CEILING
    assert json.loads(canonical)["evidence_blob"] == "x" * ABOVE_ALL_OLD_CEILINGS
    assert prompt_shaper.canonical_public_request(payload, max_request_bytes=8) == canonical
    user = prompt_shaper.build_user_protocol_message(payload, max_request_bytes=8)
    assert canonical in user


@pytest.mark.parametrize(
    "build_message",
    [
        opencode_go_command_adapter.build_protocol_message,
        agy_gemini_command_adapter.build_protocol_message,
    ],
)
def test_local_app_message_shaping_has_no_request_ceiling(build_message) -> None:
    """OpenCode-Go and AGY message shaping embed a request above the old
    25,000-byte ceiling complete."""
    payload = _large_protocol_request()
    message = build_message(payload)
    assert json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) in message


def test_opencode_transport_message_and_command_have_no_request_ceiling() -> None:
    """The OpenCode inline message and native command construct a request
    above the old 20,000-byte budget complete."""
    payload = _large_protocol_request()
    canonical = opencode_protocol_transport.canonical_public_request(payload)
    assert len(canonical.encode("utf-8")) > HISTORICAL_PUBLIC_EVIDENCE_BUDGET
    message = opencode_protocol_transport.build_user_message(payload)
    assert canonical in message
    command = opencode_protocol_transport.build_opencode_command(
        "opencode-go/deepseek-v4-pro",
        "max",
        Path("C:/tmp/agentic-isolation"),
        message,
        executable="C:/tools/opencode.exe",
    )
    assert command[command.index("run") + 1] == message


def test_provider_http_body_above_old_4mib_reaches_provider_complete() -> None:
    """Candidate-50 repair: a serialized POST body larger than the removed
    historical 4 MiB common-HTTP ceiling is handed to the engine complete.

    Uses the REAL ``provider_http.request_json()`` path against a local
    loopback fake HTTP endpoint.  Fails on Candidate 49 with
    ``request payload exceeded the transport bound`` and zero provider
    hits.
    """
    import sys as _sys

    _unit_dir = str(REPO_ROOT / "tests" / "unit")
    if _unit_dir not in _sys.path:
        _sys.path.insert(0, _unit_dir)
    from fake_provider_server import FakeProviderServer

    from agentic_debugger.application.provider_http import request_json

    pad = "x" * (HISTORICAL_PROVIDER_HTTP_REQUEST_BOUND + 1024)
    with FakeProviderServer(lambda request: (200, {"ok": True})) as server:
        payload = request_json(
            "POST",
            server.base_url + "/chat/completions",
            engine="stdlib",
            json_payload={"model": "m", "input": pad},
            timeout_seconds=30,
        )
        assert payload == {"ok": True}
        # 1. body > 4,194,304 bytes; 2. exactly one provider hit;
        # 3-4. complete and untruncated; 5. success when accepted.
        assert len(server.requests) == 1
        body = json.loads(server.requests[0]["body"].decode("utf-8"))
        assert body["input"] == pad
        assert len(server.requests[0]["body"]) > HISTORICAL_PROVIDER_HTTP_REQUEST_BOUND


@pytest.fixture
def _commandcode_direct_provider(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Isolated Direct API provider setup reusing the repository's fake
    provider machinery (config store + session credential channel)."""
    from agentic_debugger.application import provider_connections as pc

    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH",
        str(tmp_path / "provider-configurations.json"),
    )
    pc.clear_all_session_keys()
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
    )
    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
        "adapter-test-credential-not-real",
    )
    yield pc
    pc.clear_all_session_keys()


_DIRECTIVE_JSON = (
    '{"kind": "action", "name": "get_source_window", '
    '"arguments": {"path": "pkg/mod.py", "start_line": 1, "end_line": 40}}'
)


def _chat_completion(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def test_direct_api_inference_above_old_4mib_reaches_provider_complete(
    _commandcode_direct_provider,
) -> None:
    """Candidate-50 repair end to end on the real model path:

    controller/adapter-shaped request → real
    ``provider_direct_api_adapter.perform_inference()`` → real
    ``provider_http.request_json()`` → loopback fake HTTP provider.

    ``request_json`` itself is never monkeypatched.  Fails on
    Candidate 49 with ``request payload exceeded the transport bound``
    and zero provider hits.
    """
    import sys as _sys

    _unit_dir = str(REPO_ROOT / "tests" / "unit")
    if _unit_dir not in _sys.path:
        _sys.path.insert(0, _unit_dir)
    from fake_provider_server import FakeProviderServer

    pad = "y" * (HISTORICAL_PROVIDER_HTTP_REQUEST_BOUND + 65_536)
    with FakeProviderServer(
        lambda request: (200, _chat_completion(_DIRECTIVE_JSON))
    ) as server:
        text, usage = provider_direct_api_adapter.perform_inference(
            "commandcode_goat",
            "deepseek/deepseek-v4-flash",
            "chat_completions",
            system_prompt="system",
            user_prompt=pad,
            timeout_seconds=30.0,
            engine="stdlib",
            base_url=server.base_url,
            auth_mode="bearer",
        )
        assert json.loads(text)["name"] == "get_source_window"
        assert usage == {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
        assert len(server.requests) == 1
        body = json.loads(server.requests[0]["body"].decode("utf-8"))
        content = body["messages"][0]["content"]
        assert pad in content
        assert len(server.requests[0]["body"]) > HISTORICAL_PROVIDER_HTTP_REQUEST_BOUND


def test_direct_api_provider_413_above_old_bound_surfaces_external(
    _commandcode_direct_provider,
) -> None:
    """Same above-4-MiB shape, but the fake external endpoint deliberately
    returns HTTP 413: the provider WAS contacted (exactly one hit) and its
    rejection surfaces as external ``request_too_large`` truth.

    This distinguishes Candidate-49 behavior (internal reject,
    provider_hits == 0) from Candidate-50 behavior (provider_hits == 1,
    provider 413, external request_too_large).
    """
    import sys as _sys

    _unit_dir = str(REPO_ROOT / "tests" / "unit")
    if _unit_dir not in _sys.path:
        _sys.path.insert(0, _unit_dir)
    from fake_provider_server import FakeProviderServer

    pad = "y" * (HISTORICAL_PROVIDER_HTTP_REQUEST_BOUND + 65_536)
    with FakeProviderServer(
        lambda request: (413, {"error": {"message": "payload too large"}})
    ) as server:
        with pytest.raises(
            provider_direct_api_adapter.ProviderDirectApiError
        ) as info:
            provider_direct_api_adapter.perform_inference(
                "commandcode_goat",
                "deepseek/deepseek-v4-flash",
                "chat_completions",
                system_prompt="system",
                user_prompt=pad,
                timeout_seconds=30.0,
                engine="stdlib",
                base_url=server.base_url,
                auth_mode="bearer",
            )
        assert len(server.requests) == 1
        assert info.value.kind == "request_too_large"
        assert "Provider rejected request as too large (HTTP 413)" in str(info.value)
