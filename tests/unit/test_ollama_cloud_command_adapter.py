"""Synthetic contract tests for the Ollama Cloud Local Application adapter.

These tests use only a task-owned loopback HTTP server. They never contact
Ollama and never generate model tokens.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentic_debugger.application.command_transport import CancellableJsonlCommandTransport
from agentic_debugger.cancellation import CancellationError, CancellationToken
from agentic_debugger.evaluation.live import LiveModelConfig
from scripts import ollama_cloud_command_adapter as adapter


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_SCRIPT = REPO_ROOT / "scripts" / "ollama_cloud_command_adapter.py"


def sample_request(*, logical_call_index: int = 1) -> dict[str, Any]:
    return {
        "protocol": {
            "name": adapter.PROTOCOL_NAME,
            "version": adapter.PROTOCOL_VERSION,
            "request_id": "test-run:model-call:1:attempt:1",
            "logical_model_call_index": logical_call_index,
            "transport_attempt_index": 1,
        },
        "identity": {
            "evaluation_id": "eval-ollama-test",
            "case_id": "eval-ollama-test:curated-none-handling-001",
            "run_id": "run-ollama-test",
            "trajectory_id": "run-ollama-test",
        },
        "task": {"task_id": "curated-none-handling-001", "instruction": "Fix the bounded synthetic task."},
        "policy": "static-baseline",
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
            "model_call_index": logical_call_index - 1,
            "allowed_actions": ["run_reproduction"],
            "legal_transition_targets": ["Understand", "Failed"],
            "budget_limits": {},
            "budget_state": {},
            "hypotheses": [],
            "last_observation": None,
        },
        "history": [],
        "directive_feedback": None,
        "instructions": "Return one directive JSON object.",
    }


def valid_content() -> str:
    return json.dumps({"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}, separators=(",", ":"))


def valid_tags_entry(**overrides: Any) -> dict[str, Any]:
    entry = {
        "name": adapter.MODEL_ID,
        "model": adapter.MODEL_ID,
        "remote_model": adapter.EXPECTED_CLOUD_REMOTE_MODEL,
        "remote_host": adapter.EXPECTED_CLOUD_REMOTE_HOST,
        "digest": "synthetic-digest",
    }
    entry.update(overrides)
    return entry


def encode_tags(entry: dict[str, Any] | None = None) -> bytes:
    return json.dumps({"models": [entry or valid_tags_entry()]}).encode()


def encode_show(*, parent_model: str = adapter.EXPECTED_CLOUD_REMOTE_MODEL) -> bytes:
    return json.dumps(
        {
            "details": {"family": "gptoss", "parent_model": parent_model},
            "capabilities": ["completion", "tools", "thinking"],
            "model_info": {"gptoss.context_length": 131072},
        }
    ).encode()


class _FixtureState:
    def __init__(
        self,
        *,
        chat_body: bytes | None = None,
        chat_status: int = 200,
        delay: float = 0.0,
        tags_delay: float = 0.0,
        show_delay: float = 0.0,
        tags_payload: bytes | None = None,
        show_payload: bytes | None = None,
    ) -> None:
        self.chat_body = chat_body or self._chat_envelope(valid_content())
        self.chat_status = chat_status
        self.delay = delay
        self.tags_delay = tags_delay
        self.show_delay = show_delay
        self.tags_body = tags_payload or encode_tags()
        self.show_body = show_payload or encode_show()
        self.requests: list[tuple[str, dict[str, Any] | None]] = []
        self.chat_started = threading.Event()
        self.lock = threading.Lock()

    @staticmethod
    def _chat_envelope(content: str, **message_fields: Any) -> bytes:
        model = message_fields.pop("model", adapter.EXPECTED_CLOUD_REMOTE_MODEL)
        remote_model = message_fields.pop("remote_model", None)
        remote_host = message_fields.pop("remote_host", None)
        message = {"role": "assistant", "content": content, **message_fields}
        envelope: dict[str, Any] = {
            "model": model,
            "done": True,
            "done_reason": "stop",
            "message": message,
        }
        if remote_model is not None:
            envelope["remote_model"] = remote_model
        if remote_host is not None:
            envelope["remote_host"] = remote_host
        return json.dumps(envelope).encode("utf-8")

    def chat(self, content: str, **message_fields: Any) -> None:
        self.chat_body = self._chat_envelope(content, **message_fields)

    def record(self, path: str, payload: dict[str, Any] | None) -> None:
        with self.lock:
            self.requests.append((path, payload))


class _Handler(BaseHTTPRequestHandler):
    state: _FixtureState

    def log_message(self, *_args: Any) -> None:
        return

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self.state.record(self.path, None)
        if self.path == "/api/version":
            self._send(200, json.dumps({"version": adapter.EXPECTED_OLLAMA_VERSION}).encode())
            return
        if self.path == "/api/tags":
            if self.state.tags_delay:
                time.sleep(self.state.tags_delay)
            self._send(200, self.state.tags_body)
            return
        self._send(404, b"{}")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        self.state.record(self.path, payload)
        if self.path == "/api/show":
            if self.state.show_delay:
                time.sleep(self.state.show_delay)
            self._send(200, self.state.show_body)
            return
        if self.path != "/api/chat":
            self._send(404, b"{}")
            return
        self.state.chat_started.set()
        if self.state.delay:
            time.sleep(self.state.delay)
        self._send(self.state.chat_status, self.state.chat_body)


@pytest.fixture
def fixture_server():
    servers: list[ThreadingHTTPServer] = []

    def start(state: _FixtureState | None = None) -> tuple[_FixtureState, ThreadingHTTPServer, str]:
        state = state or _FixtureState()
        handler = type("FixtureHandler", (_Handler,), {"state": state})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return state, server, f"http://127.0.0.1:{server.server_port}/api"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def invoke(endpoint: str, request: dict[str, Any] | None = None, *, timeout: float = 2.0) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = adapter.run_adapter(
        stdin_stream=io.StringIO(json.dumps(request or sample_request()) + "\n"),
        stdout_stream=stdout,
        stderr_stream=stderr,
        argv=["--endpoint", endpoint, "--timeout", str(timeout)],
    )
    return rc, stdout.getvalue(), stderr.getvalue()


def request_paths(state: _FixtureState) -> list[str]:
    return [path for path, _payload in state.requests]


def chat_payloads(state: _FixtureState) -> list[dict[str, Any]]:
    return [payload for path, payload in state.requests if path == "/api/chat" and payload is not None]


def test_valid_directive_and_request_contract(fixture_server) -> None:
    state, _server, endpoint = fixture_server()
    rc, stdout, stderr = invoke(endpoint)
    assert rc == 0, stderr
    assert json.loads(stdout) == {"directive": json.loads(valid_content())}
    assert stderr == ""
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]
    payload = chat_payloads(state)[0]
    assert payload["model"] == adapter.MODEL_ID
    assert payload["stream"] is False
    assert payload["think"] == "low"
    assert "tools" not in payload
    assert "functions" not in payload
    assert "format" not in payload
    messages = payload["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert adapter.ADAPTER_RETRY_COUNT == 0
    assert adapter.FALLBACK_COUNT == 0


def test_system_prompt_teaches_exact_validator_field_names() -> None:
    assert adapter._directive_fields_match_validator()
    system = adapter.SYSTEM_PROMPT
    assert '"kind"' in system
    assert '{"kind":"action","name":"<allowed action>","arguments":{...}}' in system
    assert '{"kind":"transition","target_state":"<legal target>","reason":"<bounded reason>"}' in system
    for kind, fields in adapter.DIRECTIVE_TOP_LEVEL_FIELDS.items():
        assert kind in system
        for field in fields:
            assert f'"{field}"' in system
    assert "Do not use top-level keys named action, payload, or transition." in system
    assert "Never combine an action and a transition" in system


def test_request_guidance_uses_exact_run_reproduction_shape() -> None:
    messages = adapter.build_chat_messages(sample_request())
    assert [message["role"] for message in messages] == ["system", "user"]
    user = messages[1]["content"]
    expected = json.dumps(
        {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
        separators=(",", ":"),
    )
    assert expected in user
    assert '"kind"' in messages[0]["content"]
    assert "name" in messages[0]["content"]
    assert "arguments" in messages[0]["content"]
    assert "target_state" in messages[0]["content"]
    assert "reason" in messages[0]["content"]


def test_real_cloud_chat_shape_without_remote_fields_succeeds(fixture_server) -> None:
    state = _FixtureState(
        chat_body=_FixtureState._chat_envelope(
            valid_content(),
            model=adapter.EXPECTED_CLOUD_REMOTE_MODEL,
        )
    )
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 0, stderr
    assert json.loads(stdout) == {"directive": json.loads(valid_content())}
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]
    assert chat_payloads(state)[0]["model"] == adapter.MODEL_ID


def test_wrong_upstream_model_is_rejected(fixture_server) -> None:
    state = _FixtureState(
        chat_body=_FixtureState._chat_envelope(
            valid_content(),
            model="gpt-oss:21b",
        )
    )
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""
    assert request_paths(state)[-1] == "/api/chat"


def test_chat_local_alias_is_rejected_as_metadata_disagreement(fixture_server) -> None:
    state = _FixtureState(
        chat_body=_FixtureState._chat_envelope(
            valid_content(),
            model=adapter.MODEL_ID,
        )
    )
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]


@pytest.mark.parametrize(
    "entry",
    [
        valid_tags_entry(remote_model="other-model"),
        valid_tags_entry(remote_host="https://example.com"),
        valid_tags_entry(remote_host="https://ollama.com.evil.example"),
        valid_tags_entry(remote_model=None),
        valid_tags_entry(remote_host=None),
        valid_tags_entry(name="other-cloud", model="other-cloud"),
        valid_tags_entry(model="gpt-oss:20b"),
    ],
)
def test_wrong_or_missing_tags_provenance_is_rejected_before_chat(fixture_server, entry: dict[str, Any]) -> None:
    mutated = dict(entry)
    if mutated.get("remote_model") is None:
        mutated.pop("remote_model", None)
    if mutated.get("remote_host") is None:
        mutated.pop("remote_host", None)
    state = _FixtureState(tags_payload=encode_tags(mutated))
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""
    assert "/api/chat" not in request_paths(state)
    assert "/api/generate" not in request_paths(state)


def test_wrong_show_parent_model_is_rejected_before_chat(fixture_server) -> None:
    state = _FixtureState(show_payload=encode_show(parent_model="other-parent"))
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""
    assert request_paths(state) == ["/api/tags", "/api/show"]
    assert "/api/chat" not in request_paths(state)


def test_missing_show_parent_model_is_rejected_before_chat(fixture_server) -> None:
    state = _FixtureState(show_payload=encode_show(parent_model=""))
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""
    assert "/api/chat" not in request_paths(state)


def test_metadata_host_accepts_default_https_port(fixture_server) -> None:
    state = _FixtureState(
        tags_payload=encode_tags(valid_tags_entry(remote_host="https://ollama.com:443"))
    )
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, stderr = invoke(endpoint)
    assert rc == 0, stderr
    assert json.loads(stdout) == {"directive": json.loads(valid_content())}
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]


def test_legal_repository_action_is_a_local_application_directive(fixture_server) -> None:
    request = sample_request()
    request["controller"]["allowed_actions"] = ["apply_patch"]
    request["action_contracts"]["apply_patch"] = {
        "properties": {"patch": {"type": "string", "min_length": 1}},
        "required": ["patch"],
        "additional_properties": False,
    }
    content = json.dumps(
        {
            "kind": "action",
            "name": "apply_patch",
            "arguments": {"patch": "*** Begin Patch\n*** End Patch"},
        },
        separators=(",", ":"),
    )
    state = _FixtureState(chat_body=_FixtureState._chat_envelope(content))
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint, request)

    assert rc == 0, stderr
    assert json.loads(stdout)["directive"]["name"] == "apply_patch"
    payload = chat_payloads(state)[0]
    assert "tools" not in payload
    assert "functions" not in payload
    assert "format" not in payload
    messages = payload["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    system = messages[0]["content"]
    user = messages[1]["content"]
    assert "directly invoke tools or functions" in system
    assert "legal action directive" in system
    assert "Local Application performs every actual action" in system
    assert '{"kind":"action","name":"apply_patch","arguments":{"patch":"<patch>"}}' in user
    assert "run_reproduction" not in user.split(adapter.PUBLIC_REQUEST_START, 1)[0]


@pytest.mark.parametrize("content", [
    "{not-json}",
    "```json\n" + valid_content() + "\n```",
    "Here is the directive: " + valid_content(),
    valid_content() + " trailing prose",
    valid_content() + " " + valid_content(),
    "[]",
])
def test_final_content_must_be_one_json_object(fixture_server, content: str) -> None:
    state = _FixtureState(chat_body=_FixtureState._chat_envelope(content))
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]
    assert len(chat_payloads(state)) == 1


def test_observed_alias_payload_shape_remains_rejected(fixture_server) -> None:
    content = '{"action":"run_reproduction","transition":"Understand","payload":{"phase":"baseline"}}'
    state = _FixtureState(chat_body=_FixtureState._chat_envelope(content))
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""
    assert "directive kind is not allowed" in stderr
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]


@pytest.mark.parametrize("content", [
    "{\"kind\":\"unknown\"}",
    "{\"kind\":\"action\",\"name\":\"apply_patch\",\"arguments\":{}}",
])
def test_illegal_directive_is_rejected_before_success(fixture_server, content: str) -> None:
    state = _FixtureState(chat_body=_FixtureState._chat_envelope(content))
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""


@pytest.mark.parametrize("response", [
    {"model": adapter.EXPECTED_CLOUD_REMOTE_MODEL, "done": True, "message": {"role": "assistant"}},
    {"model": adapter.EXPECTED_CLOUD_REMOTE_MODEL, "done": True, "message": {"role": "user", "content": valid_content()}},
    {"model": adapter.EXPECTED_CLOUD_REMOTE_MODEL, "done": True, "message": {"role": "assistant", "content": 3}},
    {"model": "other-model", "done": True, "message": {"role": "assistant", "content": valid_content()}},
    {"model": adapter.EXPECTED_CLOUD_REMOTE_MODEL, "done": False, "done_reason": "stop", "message": {"role": "assistant", "content": valid_content()}},
    {"model": adapter.EXPECTED_CLOUD_REMOTE_MODEL, "done": True, "done_reason": 3, "message": {"role": "assistant", "content": valid_content()}},
    {
        "model": adapter.EXPECTED_CLOUD_REMOTE_MODEL,
        "done": True,
        "done_reason": "stop",
        "message": {"role": "assistant", "content": valid_content(), "tool_calls": []},
    },
])
def test_response_shape_completion_model_and_tools_fail_closed(fixture_server, response: dict[str, Any]) -> None:
    state = _FixtureState(chat_body=json.dumps(response).encode())
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""


def test_thinking_is_discarded_and_not_emitted(fixture_server) -> None:
    secret = "synthetic-thinking-secret-never-persist"
    state = _FixtureState()
    state.chat(valid_content(), thinking=secret)
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, stderr = invoke(endpoint)
    assert rc == 0
    assert secret not in stdout
    assert secret not in stderr
    assert json.loads(stdout)["directive"] == json.loads(valid_content())
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]


def test_oversized_http_body_and_http_error_are_bounded(fixture_server) -> None:
    oversized = _FixtureState(chat_body=b"x" * (adapter.MAX_RAW_RESPONSE_BYTES + 1))
    _state, _server, endpoint = fixture_server(oversized)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""

    failed = _FixtureState(chat_status=503)
    _state, _server, endpoint = fixture_server(failed)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""
    assert request_paths(failed) == ["/api/tags", "/api/show", "/api/chat"]
    assert len(chat_payloads(failed)) == 1


def test_timeout_is_bounded_and_no_retry_occurs(fixture_server) -> None:
    state = _FixtureState(delay=2.0)
    _state, _server, endpoint = fixture_server(state)
    started = time.monotonic()
    rc, stdout, _stderr = invoke(endpoint, timeout=0.1)
    elapsed = time.monotonic() - started
    assert rc == 1
    assert stdout == ""
    assert elapsed < 2.0
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]
    assert len(chat_payloads(state)) == 1
    assert adapter.ADAPTER_RETRY_COUNT == 0
    assert adapter.FALLBACK_COUNT == 0


def test_shared_deadline_includes_metadata_cost(fixture_server) -> None:
    state = _FixtureState(tags_delay=0.2, show_delay=0.2)
    _state, _server, endpoint = fixture_server(state)
    started = time.monotonic()
    rc, stdout, _stderr = invoke(endpoint, timeout=0.3)
    elapsed = time.monotonic() - started
    assert rc == 1
    assert stdout == ""
    assert elapsed < 0.9
    assert "/api/chat" not in request_paths(state)
    assert "/api/generate" not in request_paths(state)
    assert adapter.ADAPTER_RETRY_COUNT == 0
    assert adapter.FALLBACK_COUNT == 0


def test_request_and_logical_call_bounds_are_fail_closed(fixture_server) -> None:
    state, _server, endpoint = fixture_server()
    oversized = sample_request()
    oversized["_pad"] = "x" * (adapter.MAX_PUBLIC_REQUEST_BYTES + 1)
    rc, stdout, _stderr = invoke(endpoint, oversized)
    assert rc == 1
    assert stdout == ""
    assert state.requests == []

    out_of_range = sample_request(logical_call_index=25)
    rc, stdout, _stderr = invoke(endpoint, out_of_range)
    assert rc == 1
    assert stdout == ""
    assert state.requests == []


@pytest.mark.parametrize("endpoint", ["https://127.0.0.1:11434/api", "http://localhost:11434/api", "http://example.com/api", "http://127.0.0.1:11434/other"])
def test_endpoint_is_loopback_api_only(endpoint: str) -> None:
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""


def test_preflight_uses_only_metadata_endpoints(fixture_server) -> None:
    state, _server, endpoint = fixture_server()
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = adapter.run_adapter(
        stdin_stream=io.StringIO(""),
        stdout_stream=stdout,
        stderr_stream=stderr,
        argv=["--endpoint", endpoint, "--preflight"],
    )
    assert rc == 0, stderr.getvalue()
    result = json.loads(stdout.getvalue())
    assert result["local_daemon_api_ready"] is True
    assert result["model_available"] is True
    assert result["model_metadata_readable"] is True
    assert result["model_remote_model"] == adapter.EXPECTED_CLOUD_REMOTE_MODEL
    assert result["model_remote_host"] == adapter.EXPECTED_CLOUD_REMOTE_HOST
    assert result["provider_inference_started"] is False
    assert result["cloud_inference_verified"] is False
    paths = [path for path, _payload in state.requests]
    assert paths == ["/api/version", "/api/tags", "/api/show"]
    assert all(path not in {"/api/chat", "/api/generate"} for path in paths)


@pytest.mark.parametrize("field", ["remote_model", "remote_host"])
def test_preflight_rejects_unexpected_cloud_provenance(fixture_server, field: str) -> None:
    model_entry = {
        "name": adapter.MODEL_ID,
        "model": adapter.MODEL_ID,
        "remote_model": adapter.EXPECTED_CLOUD_REMOTE_MODEL,
        "remote_host": adapter.EXPECTED_CLOUD_REMOTE_HOST,
    }
    model_entry[field] = "unexpected-model" if field == "remote_model" else "https://example.com"
    tags_payload = json.dumps({"models": [model_entry]}).encode()
    state = _FixtureState(tags_payload=tags_payload)
    _state, _server, endpoint = fixture_server(state)
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = adapter.run_adapter(
        stdin_stream=io.StringIO(""),
        stdout_stream=stdout,
        stderr_stream=stderr,
        argv=["--endpoint", endpoint, "--preflight"],
    )
    assert rc == 1
    assert stdout.getvalue() == ""
    assert "/api/chat" not in [path for path, _payload in state.requests]


def test_external_cancellation_terminates_adapter_promptly(fixture_server, tmp_path: Path) -> None:
    state = _FixtureState(delay=30.0)
    _state, _server, endpoint = fixture_server(state)
    token = CancellationToken()
    config = LiveModelConfig(
        model_name="Ollama Cloud synthetic",
        command=(sys.executable, str(ADAPTER_SCRIPT), "--endpoint", endpoint, "--timeout", "20"),
        request_timeout_seconds=20.0,
    )
    transport = CancellableJsonlCommandTransport(config, cancel_check=token.check, cwd=str(tmp_path))
    outcome: list[BaseException] = []

    def drive() -> None:
        try:
            transport.request(sample_request(), timeout_seconds=20.0)
        except BaseException as exc:  # asserted below
            outcome.append(exc)

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    assert state.chat_started.wait(10.0)
    token.request()
    thread.join(10.0)
    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], CancellationError)
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]
    assert len(chat_payloads(state)) == 1
