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


class _FixtureState:
    def __init__(
        self,
        *,
        chat_body: bytes | None = None,
        chat_status: int = 200,
        delay: float = 0.0,
        tags_body: bytes | None = None,
        show_body: bytes | None = None,
    ) -> None:
        self.chat_body = chat_body or self._chat_envelope(valid_content())
        self.chat_status = chat_status
        self.delay = delay
        self.tags_body = tags_body or json.dumps(
            {
                "models": [
                    {
                        "name": adapter.MODEL_ID,
                        "model": adapter.MODEL_ID,
                        "remote_model": adapter.EXPECTED_CLOUD_REMOTE_MODEL,
                        "remote_host": adapter.EXPECTED_CLOUD_REMOTE_HOST,
                        "digest": "synthetic-digest",
                    }
                ]
            }
        ).encode()
        self.show_body = show_body or json.dumps(
            {
                "details": {"family": "gptoss", "parent_model": adapter.EXPECTED_CLOUD_REMOTE_MODEL},
                "capabilities": ["completion", "tools", "thinking"],
                "model_info": {"gptoss.context_length": 131072},
            }
        ).encode()
        self.requests: list[tuple[str, dict[str, Any] | None]] = []
        self.chat_started = threading.Event()
        self.lock = threading.Lock()

    @staticmethod
    def _chat_envelope(content: str, **message_fields: Any) -> bytes:
        model = message_fields.pop("model", adapter.MODEL_ID)
        remote_model = message_fields.pop("remote_model", adapter.EXPECTED_CLOUD_REMOTE_MODEL)
        remote_host = message_fields.pop("remote_host", adapter.EXPECTED_CLOUD_REMOTE_HOST)
        message = {"role": "assistant", "content": content, **message_fields}
        return json.dumps(
            {
                "model": model,
                "remote_model": remote_model,
                "remote_host": remote_host,
                "done": True,
                "done_reason": "stop",
                "message": message,
            }
        ).encode("utf-8")

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
            self._send(200, self.state.tags_body)
            return
        self._send(404, b"{}")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        self.state.record(self.path, payload)
        if self.path == "/api/show":
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


def test_valid_directive_and_request_contract(fixture_server) -> None:
    state, _server, endpoint = fixture_server()
    rc, stdout, stderr = invoke(endpoint)
    assert rc == 0, stderr
    assert json.loads(stdout) == {"directive": json.loads(valid_content())}
    assert stderr == ""
    path, payload = state.requests[0]
    assert path == "/api/chat"
    assert payload is not None
    assert payload["model"] == adapter.MODEL_ID
    assert payload["stream"] is False
    assert payload["think"] == "low"
    assert "tools" not in payload
    assert "format" not in payload


def test_real_cloud_provenance_shape_is_accepted(fixture_server) -> None:
    state = _FixtureState(
        chat_body=_FixtureState._chat_envelope(
            valid_content(),
            model=adapter.EXPECTED_CLOUD_REMOTE_MODEL,
            remote_model=adapter.EXPECTED_CLOUD_REMOTE_MODEL,
            remote_host=adapter.EXPECTED_CLOUD_REMOTE_HOST,
        )
    )
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 0, stderr
    assert json.loads(stdout) == {"directive": json.loads(valid_content())}


def test_wrong_upstream_model_is_rejected(fixture_server) -> None:
    state = _FixtureState(
        chat_body=_FixtureState._chat_envelope(
            valid_content(),
            model="gpt-oss:21b",
            remote_model=adapter.EXPECTED_CLOUD_REMOTE_MODEL,
            remote_host=adapter.EXPECTED_CLOUD_REMOTE_HOST,
        )
    )
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""


@pytest.mark.parametrize(
    ("remote_model", "remote_host"),
    [
        ("other-model", adapter.EXPECTED_CLOUD_REMOTE_HOST),
        (adapter.EXPECTED_CLOUD_REMOTE_MODEL, "https://example.com"),
        (adapter.EXPECTED_CLOUD_REMOTE_MODEL, "https://ollama.com.evil.example"),
    ],
)
def test_cloud_provenance_mismatch_is_rejected(fixture_server, remote_model: str, remote_host: str) -> None:
    state = _FixtureState(
        chat_body=_FixtureState._chat_envelope(
            valid_content(),
            model=adapter.EXPECTED_CLOUD_REMOTE_MODEL,
            remote_model=remote_model,
            remote_host=remote_host,
        )
    )
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""


def test_cloud_provenance_missing_remote_model_is_rejected(fixture_server) -> None:
    response = json.loads(
        _FixtureState._chat_envelope(
            valid_content(),
            model=adapter.EXPECTED_CLOUD_REMOTE_MODEL,
        )
    )
    response.pop("remote_model")
    state = _FixtureState(chat_body=json.dumps(response).encode())
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 1
    assert stdout == ""


def test_cloud_provenance_accepts_default_https_port(fixture_server) -> None:
    state = _FixtureState(
        chat_body=_FixtureState._chat_envelope(
            valid_content(),
            model=adapter.EXPECTED_CLOUD_REMOTE_MODEL,
            remote_host="https://ollama.com:443",
        )
    )
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, stderr = invoke(endpoint)
    assert rc == 0, stderr
    assert json.loads(stdout) == {"directive": json.loads(valid_content())}


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
    _path, payload = state.requests[0]
    assert payload is not None
    assert "tools" not in payload
    assert "functions" not in payload
    assert "format" not in payload
    prompt = payload["messages"][0]["content"]
    assert "directly invoke tools or functions" in prompt
    assert "legal action directive" in prompt
    assert "Local Application performs every actual action" in prompt


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
    assert len(state.requests) == 1


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
    {"model": adapter.MODEL_ID, "done": True, "message": {"role": "assistant"}},
    {"model": adapter.MODEL_ID, "done": True, "message": {"role": "user", "content": valid_content()}},
    {"model": adapter.MODEL_ID, "done": True, "message": {"role": "assistant", "content": 3}},
    {"model": "other-model", "done": True, "message": {"role": "assistant", "content": valid_content()}},
    {"model": adapter.MODEL_ID, "done": False, "message": {"role": "assistant", "content": valid_content()}},
    {"model": adapter.MODEL_ID, "done": True, "done_reason": 3, "message": {"role": "assistant", "content": valid_content()}},
    {"model": adapter.MODEL_ID, "done": True, "message": {"role": "assistant", "content": valid_content(), "tool_calls": []}},
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
    assert [path for path, _payload in failed.requests] == ["/api/chat"]


def test_timeout_is_bounded_and_no_retry_occurs(fixture_server) -> None:
    state = _FixtureState(delay=2.0)
    _state, _server, endpoint = fixture_server(state)
    started = time.monotonic()
    rc, stdout, _stderr = invoke(endpoint, timeout=0.1)
    elapsed = time.monotonic() - started
    assert rc == 1
    assert stdout == ""
    assert elapsed < 2.0
    assert len(state.requests) == 1
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
    tags_body = json.dumps({"models": [model_entry]}).encode()
    state = _FixtureState(tags_body=tags_body)
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
    assert len(state.requests) == 1
