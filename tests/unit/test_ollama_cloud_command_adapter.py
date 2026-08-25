"""Synthetic contract tests for the Ollama Cloud Local Application adapter.

These tests use only a task-owned loopback HTTP server. They never contact
Ollama and never generate model tokens.
"""

from __future__ import annotations

import io
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentic_debugger.application.command_transport import CancellableJsonlCommandTransport
from agentic_debugger.cancellation import CancellationError, CancellationToken
from agentic_debugger.evaluation.live import LiveModelConfig
from agentic_debugger.evaluation.transport_qualification import (
    main as transport_qualification_main,
    run_transport_qualification,
    TransportQualificationError,
)
from agentic_debugger.runtime.exceptions import PatchValidationError
from agentic_debugger.runtime.patcher import PatchManager, _parse_unified_diff
from agentic_debugger.runtime.workspace import TaskWorkspace
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


def stream_body(*frames: dict[str, Any]) -> bytes:
    return b"".join(
        json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        for frame in frames
    )


def stream_frame(
    *,
    content: str = "",
    thinking: str = "",
    done: bool = False,
    frame_model: str | None = None,
) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "model": frame_model or adapter.EXPECTED_CLOUD_REMOTE_MODEL,
        "done": done,
        "message": {
            "role": "assistant",
            "thinking": thinking,
            "content": content,
        },
    }
    if done:
        frame["done_reason"] = "stop"
    return frame


def test_exact_proof_guidance_marks_breakpoint_example_as_structural_only() -> None:
    request = sample_request()
    request["proof_gate"] = {
        "next_required_actions": ["start_pdb_session"],
        "pre_diagnosis_ready": False,
        "session_active": False,
    }
    request["action_contracts"] = {
        "start_pdb_session": {
            "properties": {"breakpoint_line": {"type": "integer", "minimum": 1}},
            "required": ["breakpoint_line"],
            "additional_properties": False,
        }
    }
    request["controller"]["allowed_actions"] = ["start_pdb_session"]

    guidance = adapter.build_request_guidance(request)

    assert "Exact-proof next required actions: start_pdb_session." in guidance
    assert "shown breakpoint number is only a shape" in guidance
    assert "visible executable target-function line" in guidance
    assert "not def/import/module code" in guidance
    assert '"breakpoint_line":1' in guidance
    assert '"breakpoint_line":0' not in guidance


def test_exact_proof_hypothesis_runtime_flag_is_guided_and_enforced() -> None:
    request = sample_request()
    request["directive_schema"] = {
        "add_hypothesis": {
            "kind": "add_hypothesis",
            "required": [
                "hypothesis_id",
                "statement",
                "confidence",
                "evidence_refs",
                "requires_runtime_evidence",
            ],
            "constraints": {
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "requires_runtime_evidence": {"type": "boolean", "enum": [True]},
            },
        }
    }
    request["controller"]["allowed_actions"] = []
    request["controller"]["legal_transition_targets"] = []
    candidate = {
        "kind": "add_hypothesis",
        "hypothesis_id": "hypothesis-1",
        "statement": "runtime evidence is required",
        "confidence": "low",
        "evidence_refs": [],
        "requires_runtime_evidence": True,
    }

    guidance = adapter.build_request_guidance(request)

    assert '"requires_runtime_evidence":true' in guidance
    # Acceptance belongs to the live adapter's typed parser.  The command
    # adapter only forwards final content and transport telemetry.
    assert json.dumps(candidate, separators=(",", ":"))
    assert not hasattr(adapter, "parse_directive_content")
    assert not hasattr(adapter, "validate_directive_candidate")


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
        version: str | None = None,
        chat_chunks: tuple[tuple[float, bytes], ...] | None = None,
    ) -> None:
        self.chat_body = chat_body or self._chat_envelope(valid_content())
        self.chat_status = chat_status
        self.delay = delay
        self.tags_delay = tags_delay
        self.show_delay = show_delay
        self.tags_body = tags_payload or encode_tags()
        self.show_body = show_payload or encode_show()
        self.version = version or adapter.EXPECTED_OLLAMA_VERSION
        self.chat_chunks = chat_chunks
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
            self._send(200, json.dumps({"version": self.state.version}).encode())
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
        if self.state.chat_chunks is not None:
            body_length = sum(len(chunk) for _delay, chunk in self.state.chat_chunks)
            self.send_response(self.state.chat_status)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(body_length))
            self.end_headers()
            for delay, chunk in self.state.chat_chunks:
                if delay:
                    time.sleep(delay)
                self.wfile.write(chunk)
                self.wfile.flush()
            return
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


def invoke(
    endpoint: str,
    request: dict[str, Any] | None = None,
    *,
    timeout: float = 2.0,
    request_timeout: float | None = None,
    model: str | None = None,
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    argv = ["--endpoint", endpoint, "--timeout", str(timeout)]
    if request_timeout is not None:
        argv.extend(["--request-timeout", str(request_timeout)])
    if model is not None:
        argv.extend(["--model", model])
    rc = adapter.run_adapter(
        stdin_stream=io.StringIO(json.dumps(request or sample_request()) + "\n"),
        stdout_stream=stdout,
        stderr_stream=stderr,
        argv=argv,
    )
    return rc, stdout.getvalue(), stderr.getvalue()


def request_paths(state: _FixtureState) -> list[str]:
    return [path for path, _payload in state.requests]


def chat_payloads(state: _FixtureState) -> list[dict[str, Any]]:
    return [payload for path, payload in state.requests if path == "/api/chat" and payload is not None]


def assert_success_envelope(stdout: str, *, thinking_bytes: int = 0, frame_count: int = 1) -> None:
    value = json.loads(stdout)
    assert value["provider_completion_schema_version"] == "provider-completion-v1"
    assert value["directive_content"] == valid_content()
    activity = value["transport_activity"]
    assert activity["schema_version"] == adapter.CONTENT_FRAGMENT_OBSERVABILITY_SCHEMA_VERSION
    assert activity["stream_frame_count"] == frame_count
    assert activity["thinking_bytes"] == thinking_bytes
    assert activity["content_bytes"] == len(valid_content().encode("utf-8"))
    assert activity["first_content_frame_index"] == frame_count - 1
    assert activity["last_content_frame_index"] == frame_count - 1
    assert activity["content_frame_count"] == 1
    assert activity["final_content_byte_length"] == len(valid_content().encode("utf-8"))
    assert activity["final_content_sha256"] == hashlib.sha256(valid_content().encode("utf-8")).hexdigest()
    assert activity["content_frame_diagnostics_truncated"] is False


NEMOTRON_ALIAS = "nemotron-3-nano:30b-cloud"
NEMOTRON_UPSTREAM = "nemotron-3-nano:30b"


def nemotron_tags_entry(**overrides: Any) -> dict[str, Any]:
    entry = {
        "name": NEMOTRON_ALIAS,
        "model": NEMOTRON_ALIAS,
        "remote_model": NEMOTRON_UPSTREAM,
        "remote_host": adapter.EXPECTED_CLOUD_REMOTE_HOST,
        "digest": "synthetic-nemotron-digest",
    }
    entry.update(overrides)
    return entry


def encode_nemotron_show(*, parent_model: str = NEMOTRON_UPSTREAM) -> bytes:
    return json.dumps(
        {
            "details": {"family": "nemotron-3-nano", "parent_model": parent_model},
            "capabilities": ["completion", "tools", "thinking"],
            "model_info": {"nemotron-3-nano.context_length": 262144},
        }
    ).encode()


def nemotron_state(*, chat_model: str = NEMOTRON_UPSTREAM, **overrides: Any) -> _FixtureState:
    return _FixtureState(
        tags_payload=encode_tags(nemotron_tags_entry()),
        show_payload=encode_nemotron_show(),
        chat_body=_FixtureState._chat_envelope(valid_content(), model=chat_model),
        **overrides,
    )


SEVENTEEN_ALIASES = [
    "gpt-oss:20b-cloud",
    "gpt-oss:120b-cloud",
    "glm-5.1:cloud",
    "glm-5.2:cloud",
    "deepseek-v4-flash:cloud",
    "deepseek-v4-pro:cloud",
    "kimi-k2.6:cloud",
    "kimi-k2.7-code:cloud",
    "kimi-k3:cloud",
    "minimax-m2.7:cloud",
    "minimax-m3:cloud",
    "nemotron-3-nano:30b-cloud",
    "nemotron-3-super:cloud",
    "nemotron-3-ultra:cloud",
    "qwen3.5:cloud",
    "gemma4:31b-cloud",
    "mistral-large-3:675b-cloud",
]


def test_registry_keeps_gpt_oss_default_and_accepted_aliases() -> None:
    assert adapter.DEFAULT_MODEL_ID == "gpt-oss:20b-cloud"
    assert adapter.MODEL_ID == "gpt-oss:20b-cloud"
    assert adapter.EXPECTED_CLOUD_REMOTE_MODEL == "gpt-oss:20b"
    assert adapter.ALLOWED_MODEL_IDENTIFIERS == frozenset(SEVENTEEN_ALIASES)
    gpt = adapter.resolve_cloud_model("gpt-oss:20b-cloud")
    nemotron = adapter.resolve_cloud_model("nemotron-3-nano:30b-cloud")
    assert gpt.local_alias == "gpt-oss:20b-cloud"
    assert gpt.upstream_model == "gpt-oss:20b"
    assert nemotron.local_alias == NEMOTRON_ALIAS
    assert nemotron.upstream_model == NEMOTRON_UPSTREAM


def test_registry_contains_all_17_aliases_with_verified_upstream():
    assert set(adapter.CLOUD_MODELS) == set(SEVENTEEN_ALIASES)
    assert len(adapter.CLOUD_MODELS) == 17
    for alias in SEVENTEEN_ALIASES:
        spec = adapter.resolve_cloud_model(alias)
        assert spec.local_alias == alias
        assert type(spec.upstream_model) is str and spec.upstream_model
        assert spec.effective_tags_remote_model  # never empty
        assert isinstance(spec.capabilities, tuple)
        assert type(spec.transport_verified) is bool
        assert type(spec.transport_profile_declared) is bool
        assert spec.readiness in ("catalog", "profile_declared", "live_verified")
    # Catalog: every alias is selectable.
    # Profile_declared: same-family streaming intent (gpt-oss 120b) but not yet live qualified.
    # Live_verified: empirically streaming-qualified with recorded /api/chat exercise.
    assert adapter.CLOUD_MODELS["gpt-oss:20b-cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["nemotron-3-nano:30b-cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["gpt-oss:120b-cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["kimi-k2.6:cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["qwen3.5:cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["gemma4:31b-cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["nemotron-3-super:cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["minimax-m2.7:cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["nemotron-3-ultra:cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["mistral-large-3:675b-cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["glm-5.1:cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["glm-5.2:cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["kimi-k2.7-code:cloud"].readiness == "profile_declared"
    assert adapter.CLOUD_MODELS["deepseek-v4-pro:cloud"].readiness == "live_verified"
    assert adapter.CLOUD_MODELS["minimax-m3:cloud"].readiness == "live_verified"
    assert all(adapter.CLOUD_MODELS[a].readiness == "catalog" for a in SEVENTEEN_ALIASES if a not in {"gpt-oss:20b-cloud", "gpt-oss:120b-cloud", "kimi-k2.6:cloud", "qwen3.5:cloud", "gemma4:31b-cloud", "nemotron-3-nano:30b-cloud", "nemotron-3-super:cloud", "nemotron-3-ultra:cloud", "minimax-m2.7:cloud", "deepseek-v4-flash:cloud", "mistral-large-3:675b-cloud", "glm-5.1:cloud", "glm-5.2:cloud", "kimi-k2.7-code:cloud", "deepseek-v4-pro:cloud", "minimax-m3:cloud"})
    assert adapter.is_live_transport_ready(adapter.CLOUD_MODELS["gpt-oss:20b-cloud"]) is True
    assert adapter.is_live_transport_ready(adapter.CLOUD_MODELS["gpt-oss:120b-cloud"]) is True
    assert adapter.is_treatment_eligible(adapter.CLOUD_MODELS["gpt-oss:120b-cloud"]) is True
    assert adapter.is_treatment_eligible(adapter.CLOUD_MODELS["kimi-k2.6:cloud"]) is True
    assert adapter.is_treatment_eligible(adapter.CLOUD_MODELS["qwen3.5:cloud"]) is True
    assert adapter.is_treatment_eligible(adapter.CLOUD_MODELS["gemma4:31b-cloud"]) is True
    # 120b declares the same high-thinking profile as 20b and is now
    # empirically live verified by the retained qualification artifact.
    assert adapter.CLOUD_MODELS["gpt-oss:120b-cloud"].thinking_level == "high"
    assert adapter.CLOUD_MODELS["gpt-oss:120b-cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["kimi-k2.6:cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["kimi-k2.6:cloud"].thinking_level is None
    assert adapter.CLOUD_MODELS["qwen3.5:cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["qwen3.5:cloud"].thinking_level is None
    assert adapter.CLOUD_MODELS["gemma4:31b-cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["gemma4:31b-cloud"].thinking_level is None
    assert adapter.CLOUD_MODELS["nemotron-3-super:cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["nemotron-3-super:cloud"].transport_verified is True
    assert adapter.CLOUD_MODELS["nemotron-3-super:cloud"].thinking_level is None
    assert adapter.CLOUD_MODELS["nemotron-3-super:cloud"].idle_timeout_seconds == 45.0
    assert adapter.CLOUD_MODELS["nemotron-3-super:cloud"].request_timeout_seconds == 75.0
    assert adapter.CLOUD_MODELS["minimax-m2.7:cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["minimax-m2.7:cloud"].transport_verified is True
    assert adapter.CLOUD_MODELS["minimax-m2.7:cloud"].thinking_level is None
    assert adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"].transport_verified is True
    assert adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"].thinking_level is None
    deepseek = adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"]
    assert deepseek.idle_timeout_seconds == 300.0
    assert deepseek.request_timeout_seconds == 3600.0
    assert adapter.CLOUD_MODELS["nemotron-3-ultra:cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["nemotron-3-ultra:cloud"].transport_verified is True
    assert adapter.CLOUD_MODELS["nemotron-3-ultra:cloud"].thinking_level is None
    assert adapter.CLOUD_MODELS["nemotron-3-ultra:cloud"].idle_timeout_seconds == 45.0
    assert adapter.CLOUD_MODELS["nemotron-3-ultra:cloud"].request_timeout_seconds == 75.0
    assert adapter.CLOUD_MODELS["glm-5.1:cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["glm-5.1:cloud"].transport_verified is True
    assert adapter.is_treatment_eligible(adapter.CLOUD_MODELS["glm-5.1:cloud"]) is True
    assert adapter.CLOUD_MODELS["glm-5.1:cloud"].thinking_level is None
    glm52 = adapter.CLOUD_MODELS["glm-5.2:cloud"]
    assert glm52.local_alias == "glm-5.2:cloud"
    assert glm52.upstream_model == "glm-5.2"
    assert glm52.effective_tags_remote_model == "glm-5.2"
    assert glm52.family == "glm5.2"
    assert glm52.parameter_count == 756162687872
    assert glm52.context_length == 1000000
    assert glm52.capabilities == ("completion", "thinking", "tools")
    assert glm52.transport_profile_declared is True
    assert glm52.transport_verified is True
    assert glm52.readiness == "live_verified"
    assert adapter.is_live_transport_ready(glm52) is True
    assert adapter.is_treatment_eligible(glm52) is True
    assert glm52.thinking_level is None
    assert glm52.idle_timeout_seconds == 20.0
    assert glm52.request_timeout_seconds == 60.0
    assert adapter.transport_config_fingerprint(glm52) == (
        "0685fad3a22efa7ba8a4776729f2f552e89d66f1032c9ad1fcb344557759dad9"
    )
    assert adapter.CLOUD_MODELS["kimi-k2.7-code:cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["kimi-k2.7-code:cloud"].transport_verified is False
    assert adapter.CLOUD_MODELS["kimi-k2.7-code:cloud"].thinking_level is None
    assert adapter.CLOUD_MODELS["deepseek-v4-pro:cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["deepseek-v4-pro:cloud"].transport_verified is True
    assert adapter.CLOUD_MODELS["deepseek-v4-pro:cloud"].thinking_level is None
    assert adapter.is_treatment_eligible(adapter.CLOUD_MODELS["deepseek-v4-pro:cloud"]) is True
    assert adapter.transport_config_fingerprint(adapter.CLOUD_MODELS["deepseek-v4-pro:cloud"]) == (
        "43d64e327b205ec770eb91cf25bcd98eab9b1fef035cfd52687d26b46b6d0994"
    )
    assert adapter.CLOUD_MODELS["minimax-m3:cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["minimax-m3:cloud"].transport_verified is True
    assert adapter.CLOUD_MODELS["minimax-m3:cloud"].thinking_level is None
    assert adapter.CLOUD_MODELS["minimax-m3:cloud"].effective_tags_remote_model == "minimax-m3"
    assert adapter.CLOUD_MODELS["minimax-m3:cloud"].idle_timeout_seconds == 20.0
    assert adapter.CLOUD_MODELS["minimax-m3:cloud"].request_timeout_seconds == 60.0
    assert adapter.is_live_transport_ready(adapter.CLOUD_MODELS["minimax-m3:cloud"]) is True
    assert adapter.is_treatment_eligible(adapter.CLOUD_MODELS["minimax-m3:cloud"]) is True
    assert adapter.transport_config_fingerprint(adapter.CLOUD_MODELS["minimax-m3:cloud"]) == (
        "27eedd82055634fd1c3d2ec733f1f4445e52a3db6c6c3e500ed3a80a952cb6e0"
    )
    assert adapter.CLOUD_MODELS["mistral-large-3:675b-cloud"].transport_profile_declared is True
    assert adapter.CLOUD_MODELS["mistral-large-3:675b-cloud"].transport_verified is True
    assert adapter.CLOUD_MODELS["mistral-large-3:675b-cloud"].thinking_level is None


def test_minimax_m3_promotion_preserves_identity_and_fingerprint():
    spec = adapter.resolve_cloud_model("minimax-m3:cloud")
    assert spec.local_alias == "minimax-m3:cloud"
    assert spec.upstream_model == "minimax-m3"
    assert spec.effective_tags_remote_model == "minimax-m3"
    assert spec.family == "minimax-m3"
    assert spec.parameter_count == 0
    assert spec.context_length == 524288
    assert spec.capabilities == ("completion", "thinking", "tools", "vision")
    assert spec.transport_profile_declared is True
    assert spec.transport_verified is True
    assert spec.thinking_level is None
    assert spec.idle_timeout_seconds == 20.0
    assert spec.request_timeout_seconds == 60.0
    assert spec.readiness == "live_verified"
    assert adapter.is_live_transport_ready(spec) is True
    assert adapter.is_treatment_eligible(spec) is True
    assert adapter.transport_config_fingerprint(spec) == (
        "27eedd82055634fd1c3d2ec733f1f4445e52a3db6c6c3e500ed3a80a952cb6e0"
    )


def test_minimax_m3_registry_and_qualification_artifact_are_stable():
    from scripts import run_cookiecutter_967_pdb_proof as operator

    assert operator.PREPARED_TREATMENT_REVISIONS.get("minimax-m3:cloud") is None
    assert operator._treatment_id_for_model("minimax-m3:cloud") == (
        "pdb-capability-level32-cookiecutter-967-minimax-m3-cloud-v1-"
        "workspace-derived-official-git-diff-v1"
    )
    assert operator._treatment_fingerprint("minimax-m3:cloud", operator.LEVEL32_TREATMENT_BUDGET) == (
        "551a0b4ec74878bea5eba90bede31503823b0f046871b5d05f2516fa12faecb0"
    )

    artifact = REPO_ROOT / "experiments" / "pdb_capability_ladder" / "transport_qualifications" / "minimax-m3-v1.json"
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == (
        "7f59f50a02903f1c13c6d46ef04e4bba3df6daf5b0082a7c08375ed4c12db14f"
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["expected_model"] == "minimax-m3:cloud"
    assert payload["expected_remote_model"] == "minimax-m3"
    assert payload["expected_tags_remote_model"] == "minimax-m3"
    assert payload["model_tag_digest"].startswith("8cd948b96f47")
    assert payload["provider_inference_started"] is False


def test_tags_remote_model_divergence_is_explicit():
    assert adapter.CLOUD_MODELS["deepseek-v4-flash:cloud"].effective_tags_remote_model == "deepseek-v4-flash:0731"
    assert adapter.CLOUD_MODELS["deepseek-v4-pro:cloud"].effective_tags_remote_model == "deepseek-v4-pro:0813"
    assert adapter.CLOUD_MODELS["qwen3.5:cloud"].effective_tags_remote_model == "qwen3.5:397b"
    assert adapter.CLOUD_MODELS["gpt-oss:20b-cloud"].effective_tags_remote_model == "gpt-oss:20b"


def test_transport_fingerprint_varies_per_model():
    fps = {alias: adapter.transport_config_fingerprint(adapter.resolve_cloud_model(alias)) for alias in SEVENTEEN_ALIASES}
    assert all(len(fp) == 64 for fp in fps.values())
    assert len(set(fps.values())) == 17
    assert fps["gpt-oss:20b-cloud"] != fps["gpt-oss:120b-cloud"]


def test_public_request_ceiling_admits_observed_kimi_level32_request():
    assert adapter.MAX_PUBLIC_REQUEST_BYTES == 32_768


def test_transport_fingerprint_covers_all_material_fields():
    # Every material field must affect the fingerprint. We prove it by mutating
    # a copy of a known spec and verifying inequality.
    import dataclasses

    base = adapter.CLOUD_MODELS["gpt-oss:20b-cloud"]
    original = adapter.transport_config_fingerprint(base)
    for field, replacement in [
        ("local_alias", "gpt-oss:20b-cloud-mut"),
        ("upstream_model", "gpt-oss:20b-mut"),
        ("tags_remote_model", "gpt-oss:20b:0000"),
        ("thinking_level", "low"),
        ("transport_profile_declared", False),
        ("transport_verified", False),
        ("family", "other"),
        ("parameter_count", 999),
        ("context_length", 999),
        ("capabilities", ("completion",)),
    ]:
        mutated = dataclasses.replace(base, **{field: replacement})
        assert adapter.transport_config_fingerprint(mutated) != original, f"fingerprint must change when {field} changes"


def test_list_models_projection_is_complete():
    import io

    out = io.StringIO()
    rc = adapter.run_adapter(stdin_stream=io.StringIO(""), stdout_stream=out, stderr_stream=io.StringIO(), argv=["--list-models"])
    assert rc == 0
    text = out.getvalue()
    for alias in SEVENTEEN_ALIASES:
        assert alias in text
    assert "live_verified" in text
    assert "catalog" in text
    assert "catalog" in text

    out2 = io.StringIO()
    rc = adapter.run_adapter(stdin_stream=io.StringIO(""), stdout_stream=out2, stderr_stream=io.StringIO(), argv=["--list-models", "--json"])
    assert rc == 0
    payload = json.loads(out2.getvalue())
    assert set(payload) == set(SEVENTEEN_ALIASES)
    for alias in SEVENTEEN_ALIASES:
        assert payload[alias]["transport_config_fingerprint"]
        assert payload[alias]["readiness"] in ("catalog", "profile_declared", "live_verified")
    assert payload["gpt-oss:20b-cloud"]["readiness"] == "live_verified"
    assert payload["gpt-oss:120b-cloud"]["readiness"] == "live_verified"
    assert payload["kimi-k2.6:cloud"]["readiness"] == "live_verified"
    assert payload["qwen3.5:cloud"]["readiness"] == "live_verified"
    assert payload["gemma4:31b-cloud"]["readiness"] == "live_verified"
    assert payload["glm-5.1:cloud"]["readiness"] == "live_verified"


def test_preflight_provenance_includes_config_fingerprint(fixture_server):
    tags_entry = {
        "name": "gpt-oss:120b-cloud",
        "model": "gpt-oss:120b-cloud",
        "remote_model": "gpt-oss:120b",
        "remote_host": adapter.EXPECTED_CLOUD_REMOTE_HOST,
        "digest": "syn-120b",
    }
    show_payload = json.dumps(
        {"details": {"family": "gptoss", "parent_model": "gpt-oss:120b"}, "capabilities": ["completion", "tools", "thinking"], "model_info": {"gptoss.context_length": 131072}}
    ).encode()
    state = _FixtureState(
        tags_payload=json.dumps({"models": [tags_entry]}).encode(),
        show_payload=show_payload,
    )
    _state, _server, endpoint = fixture_server(state)
    out = io.StringIO()
    rc = adapter.run_adapter(stdin_stream=io.StringIO(""), stdout_stream=out, stderr_stream=io.StringIO(), argv=["--endpoint", endpoint, "--model", "gpt-oss:120b-cloud", "--preflight"])
    assert rc == 0, out.getvalue()
    result = json.loads(out.getvalue())
    assert result["expected_model"] == "gpt-oss:120b-cloud"
    assert result["expected_remote_model"] == "gpt-oss:120b"
    assert result["expected_tags_remote_model"] == "gpt-oss:120b"
    assert result["readiness"] == "live_verified"
    assert result["transport_profile_declared"] is True
    assert result["model_transport_verified"] is True
    assert result["live_transport_ready"] is True
    assert result["treatment_eligible"] is True
    assert result["model_thinking_level"] == "high"
    assert len(result["transport_config_fingerprint"]) == 64
    assert result["provider_inference_started"] is False


def test_catalog_model_omits_think_from_chat_payload(fixture_server):
    # Synthetic check: a catalog model with thinking_level=None must omit "think"
    # from the chat request, yet still succeed through provenance.
    tags_entry = {
        "name": "qwen3.5:cloud",
        "model": "qwen3.5:cloud",
        "remote_model": "qwen3.5:397b",
        "remote_host": adapter.EXPECTED_CLOUD_REMOTE_HOST,
        "digest": "synthetic",
    }
    show_payload = json.dumps({"details": {"family": "qwen3.5", "parent_model": "qwen3.5"}, "capabilities": ["completion", "thinking", "tools", "vision"], "model_info": {"qwen3.5.context_length": 262144}}).encode()
    tags_payload = json.dumps({"models": [tags_entry]}).encode()
    state = _FixtureState(
        tags_payload=tags_payload,
        show_payload=show_payload,
        chat_body=_FixtureState._chat_envelope(valid_content(), model="qwen3.5"),
    )
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, stderr = invoke(endpoint, model="qwen3.5:cloud")
    assert rc == 0, stderr
    payload = chat_payloads(state)[0]
    assert "think" not in payload
    assert payload["model"] == "qwen3.5:cloud"


def test_valid_directive_and_request_contract(fixture_server) -> None:
    state, _server, endpoint = fixture_server()
    rc, stdout, stderr = invoke(endpoint)
    assert rc == 0, stderr
    assert_success_envelope(stdout)
    assert stderr == "\n"
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]
    payload = chat_payloads(state)[0]
    assert payload["model"] == adapter.MODEL_ID
    assert payload["stream"] is True
    assert payload["think"] == "high"
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
    assert '"requires_runtime_evidence":false' not in system
    assert "copy the current user message's Legal hypothesis representation" in system


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
    assert_success_envelope(stdout)
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


def test_unsupported_model_is_rejected_before_http(fixture_server) -> None:
    state, _server, endpoint = fixture_server()
    rc, stdout, stderr = invoke(endpoint, model="kimi-k2.5-cloud")
    assert rc == 1
    assert stdout == ""
    assert "not supported" in stderr
    assert state.requests == []


def test_opencode_display_name_is_not_an_accepted_cli_identifier(fixture_server) -> None:
    state, _server, endpoint = fixture_server()
    rc, stdout, stderr = invoke(endpoint, model="ollama-cloud/nemotron-3-nano:30b")
    assert rc == 1
    assert stdout == ""
    assert "not supported" in stderr
    assert state.requests == []


def test_explicit_gpt_oss_model_flag_remains_compatible(fixture_server) -> None:
    state, _server, endpoint = fixture_server()
    rc, stdout, stderr = invoke(endpoint, model="gpt-oss:20b-cloud")
    assert rc == 0, stderr
    assert_success_envelope(stdout)
    assert chat_payloads(state)[0]["model"] == "gpt-oss:20b-cloud"


def test_nemotron_model_is_passed_to_chat_and_validates_provenance(fixture_server) -> None:
    state = nemotron_state()
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, stderr = invoke(endpoint, model=NEMOTRON_ALIAS)
    assert rc == 0, stderr
    assert_success_envelope(stdout)
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]
    payload = chat_payloads(state)[0]
    assert payload["model"] == NEMOTRON_ALIAS
    assert payload["stream"] is True
    assert payload["think"] == "high"
    assert "tools" not in payload
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]


def test_nemotron_wrong_upstream_chat_model_is_rejected(fixture_server) -> None:
    state = nemotron_state(chat_model="nemotron-3-nano:31b")
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint, model=NEMOTRON_ALIAS)
    assert rc == 1
    assert stdout == ""
    assert request_paths(state)[-1] == "/api/chat"


def test_nemotron_local_alias_chat_model_is_rejected(fixture_server) -> None:
    state = nemotron_state(chat_model=NEMOTRON_ALIAS)
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint, model=NEMOTRON_ALIAS)
    assert rc == 1
    assert stdout == ""
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]


def test_gpt_oss_request_rejects_nemotron_tags_before_chat(fixture_server) -> None:
    state = _FixtureState(tags_payload=encode_tags(nemotron_tags_entry()))
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint, model="gpt-oss:20b-cloud")
    assert rc == 1
    assert stdout == ""
    assert "/api/chat" not in request_paths(state)


def test_nemotron_wrong_or_missing_tags_provenance_is_rejected_before_chat(
    fixture_server,
) -> None:
    state = _FixtureState(
        tags_payload=encode_tags(nemotron_tags_entry(remote_model="gpt-oss:20b")),
        show_payload=encode_nemotron_show(),
    )
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint, model=NEMOTRON_ALIAS)
    assert rc == 1
    assert stdout == ""
    assert "/api/chat" not in request_paths(state)


def test_nemotron_wrong_show_parent_model_is_rejected_before_chat(fixture_server) -> None:
    state = _FixtureState(
        tags_payload=encode_tags(nemotron_tags_entry()),
        show_payload=encode_nemotron_show(parent_model="nemotron-3-super"),
    )
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint, model=NEMOTRON_ALIAS)
    assert rc == 1
    assert stdout == ""
    assert request_paths(state) == ["/api/tags", "/api/show"]
    assert "/api/chat" not in request_paths(state)


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
    assert_success_envelope(stdout)
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
    assert json.loads(json.loads(stdout)["directive_content"])["name"] == "apply_patch"
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
    assert adapter.APPLY_PATCH_DIRECTIVE_SHAPE in user
    assert "run_reproduction" not in user.split(adapter.PUBLIC_REQUEST_START, 1)[0]


def _apply_patch_request() -> dict[str, Any]:
    request = sample_request()
    request["controller"]["allowed_actions"] = ["apply_patch", "revert_patch", "syntax_check"]
    request["action_contracts"]["apply_patch"] = {
        "properties": {"patch": {"type": "string", "min_length": 1}},
        "required": ["patch"],
        "additional_properties": False,
    }
    request["action_contracts"]["revert_patch"] = {
        "properties": {},
        "required": [],
        "additional_properties": False,
    }
    request["action_contracts"]["syntax_check"] = {
        "properties": {},
        "required": [],
        "additional_properties": False,
    }
    return request


ZERO_CONTEXT_UNIFIED_DIFF_EXAMPLE = (
    "--- a/example.py\n"
    "+++ b/example.py\n"
    "@@ -2,2 +2,3 @@\n"
    "-old_a = 1\n"
    "-old_b = 2\n"
    "+new_a = 1\n"
    "+new_b = 2\n"
    "+new_c = 3\n"
)

SECOND_SESSION_COUNT_MISMATCH_DIFF = (
    "--- a/display_name.py\n"
    "+++ b/display_name.py\n"
    "@@ -1,2 +1,6 @@\n"
    " def format_display_name(name: str | None) -> str:\n"
    "-    normalized_name = name.strip()\n"
    "-    return normalized_name\n"
    "+    if name is None:\n"
    "+        return \"Anonymous\"\n"
    "+    normalized_name = name.strip()\n"
    "+    return normalized_name\n"
)


def _raw_hunk_body_counts(diff: str) -> tuple[int, int]:
    """Count hunk-body prefixes from the raw patch text, independent of header numbers."""

    in_hunk = False
    old_count = 0
    new_count = 0
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            break
        if line.startswith(" "):
            old_count += 1
            new_count += 1
        elif line.startswith("-"):
            old_count += 1
        elif line.startswith("+"):
            new_count += 1
    return old_count, new_count


def test_apply_patch_guidance_teaches_exact_unified_diff_requirements() -> None:
    messages = adapter.build_chat_messages(_apply_patch_request())
    user = messages[1]["content"]
    guidance = user.split(adapter.PUBLIC_REQUEST_START, 1)[0]
    assert "--- a/<relative-path>" in guidance
    assert "+++ b/<same-relative-path>" in guidance
    assert "@@ -OLD_START,OLD_COUNT +NEW_START,NEW_COUNT @@" in guidance
    assert "OLD_START and NEW_START are 1-based line positions." in guidance
    assert "Never emit bare @@." in guidance
    assert "Never leave symbolic placeholders such as OLD_COUNT in the actual patch." in guidance
    assert adapter.OLD_COUNT_FORMULA in guidance
    assert adapter.NEW_COUNT_FORMULA in guidance
    assert "count toward both OLD_COUNT and NEW_COUNT" in guidance
    assert "Hunk counts must exactly match the hunk body." in guidance
    assert "If the header counts do not equal the body counts, correct the header before output." in guidance
    assert "Prefer the smallest valid hunk that uniquely expresses the edit." in guidance
    assert "Zero-context hunks" not in guidance
    assert "official evaluator's direct git apply check" not in guidance
    assert "one leading space for unchanged/context" in guidance
    assert "repository-relative paths" in guidance
    assert "Do not wrap the patch string in Markdown fences." in guidance
    assert "diff --git" in guidance
    assert adapter.APPLY_PATCH_DIRECTIVE_SHAPE in guidance
    assert adapter.NEUTRAL_UNIFIED_DIFF_EXAMPLE.rstrip("\n") in guidance
    assert "OLD_COUNT = 1 context + 2 removed = 3" in guidance
    assert "NEW_COUNT = 1 context + 3 added = 4" in guidance
    assert "display_name.py" not in guidance
    assert ".strip()" not in guidance
    assert "Anonymous" not in guidance


def test_apply_patch_guidance_includes_pre_output_count_checklist() -> None:
    guidance = adapter.build_apply_patch_guidance(_apply_patch_request())
    assert "Before emitting the JSON, verify:" in guidance
    assert "1. --- and +++ headers both exist and refer to the same repository-relative path." in guidance
    assert "2. Every hunk header contains four numeric values." in guidance
    assert "3. Count every hunk body line by prefix." in guidance
    assert "4. Recompute OLD_COUNT from context + removed." in guidance
    assert "5. Recompute NEW_COUNT from context + added." in guidance
    assert "6. Header counts exactly equal those totals." in guidance
    assert '7. Every hunk body line starts with " ", "-", or "+".' in guidance
    assert "8. No Markdown fences or unsupported Git metadata." in guidance
    assert f"9. The complete patch is inside {adapter.APPLY_PATCH_DIRECTIVE_SHAPE}" in guidance


def test_apply_patch_guidance_distinguishes_rejected_and_applied_lifecycle() -> None:
    guidance = adapter.build_apply_patch_guidance(_apply_patch_request())
    assert "does not create an active patch" in guidance
    assert "does not mutate the workspace" in guidance
    assert "do not call revert_patch merely to undo that rejected patch" in guidance
    assert "Do not call patch-dependent syntax_check without an active successfully applied patch." in guidance
    assert "correct the patch format or content and submit a new valid apply_patch" in guidance
    assert "successfully applied" in guidance
    assert "legal validation lifecycle" in guidance


def test_neutral_unified_diff_example_is_accepted_by_real_patch_manager(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "example.py").write_text("keep = True\nold_a = 1\nold_b = 2\n", encoding="utf-8")
    workspace = TaskWorkspace(str(source))
    try:
        parsed = _parse_unified_diff(adapter.NEUTRAL_UNIFIED_DIFF_EXAMPLE)
        assert len(parsed) == 1
        assert parsed[0].path == "example.py"
        manager = PatchManager(workspace, allowed_paths=["example.py"], denied_paths=[])
        result = manager.apply_patch(adapter.NEUTRAL_UNIFIED_DIFF_EXAMPLE)
        assert result.success is True
        assert result.hunk_count == 1
        patched = Path(workspace.resolve_path("example.py")).read_text(encoding="utf-8")
        assert patched == "keep = True\nnew_a = 1\nnew_b = 2\nnew_c = 3\n"
        manager.revert_patch()
    finally:
        workspace.cleanup()


def test_arithmetic_example_header_counts_equal_body_prefix_counts() -> None:
    parsed = _parse_unified_diff(adapter.NEUTRAL_UNIFIED_DIFF_EXAMPLE)
    hunk = parsed[0].hunks[0]
    body_old, body_new = _raw_hunk_body_counts(adapter.NEUTRAL_UNIFIED_DIFF_EXAMPLE)
    context = sum(1 for line in hunk.lines if line.prefix == " ")
    removed = sum(1 for line in hunk.lines if line.prefix == "-")
    added = sum(1 for line in hunk.lines if line.prefix == "+")
    assert (context, removed, added) == (1, 2, 3)
    assert hunk.old_count == body_old == context + removed == 3
    assert hunk.new_count == body_new == context + added == 4
    assert hunk.old_start == 1
    assert hunk.new_start == 1


def test_zero_context_hunk_is_accepted_by_unchanged_real_patch_manager(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "example.py").write_text("keep = True\nold_a = 1\nold_b = 2\n", encoding="utf-8")
    workspace = TaskWorkspace(str(source))
    try:
        parsed = _parse_unified_diff(ZERO_CONTEXT_UNIFIED_DIFF_EXAMPLE)
        hunk = parsed[0].hunks[0]
        assert sum(1 for line in hunk.lines if line.prefix == " ") == 0
        body_old, body_new = _raw_hunk_body_counts(ZERO_CONTEXT_UNIFIED_DIFF_EXAMPLE)
        assert hunk.old_count == body_old == 2
        assert hunk.new_count == body_new == 3
        manager = PatchManager(workspace, allowed_paths=["example.py"], denied_paths=[])
        result = manager.apply_patch(ZERO_CONTEXT_UNIFIED_DIFF_EXAMPLE)
        assert result.success is True
        assert result.hunk_count == 1
        patched = Path(workspace.resolve_path("example.py")).read_text(encoding="utf-8")
        assert patched == "keep = True\nnew_a = 1\nnew_b = 2\nnew_c = 3\n"
        manager.revert_patch()
    finally:
        workspace.cleanup()


def test_second_session_count_mismatch_remains_rejected_by_real_patch_manager() -> None:
    with pytest.raises(
        PatchValidationError,
        match="old_count=2 but body has 3 context/removed lines",
    ):
        _parse_unified_diff(SECOND_SESSION_COUNT_MISMATCH_DIFF)


def test_adapter_does_not_normalize_or_rewrite_patches() -> None:
    source = Path(adapter.__file__).read_text(encoding="utf-8")
    assert "agentic_debugger.runtime.patcher" not in source
    assert "from agentic_debugger" not in source
    assert "import agentic_debugger" not in source
    assert "_parse_unified_diff" not in source
    assert "def normalize_patch" not in source
    assert "def rewrite_patch" not in source
    assert "def repair_patch" not in source
    assert "def fix_hunk" not in source
    assert not hasattr(adapter, "normalize_patch")
    assert not hasattr(adapter, "rewrite_patch")
    assert not hasattr(adapter, "repair_patch")


@pytest.mark.parametrize(
    "diff",
    [
        "--- a/example.py\n@@\n",
        "--- a/example.py\n+++ b/example.py\n@@\n",
        "--- a/display_name.py\n@@\n",
        "--- a/display_name.py\n+++ b/display_name.py\n@@\n",
        "--- a/example.py\n",
        "--- a/example.py\n-value = 1\n+value = 2\n",
        SECOND_SESSION_COUNT_MISMATCH_DIFF,
    ],
)
def test_malformed_unified_diffs_remain_rejected_without_normalization(diff: str) -> None:
    with pytest.raises(PatchValidationError):
        _parse_unified_diff(diff)


@pytest.mark.parametrize("content", ["{not-json}", "[]", '{"kind":"unknown"}'])
def test_final_content_is_forwarded_for_canonical_downstream_parsing(fixture_server, content: str) -> None:
    state = _FixtureState(chat_body=_FixtureState._chat_envelope(content))
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 0
    assert json.loads(stdout)["directive_content"] == content
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]
    assert len(chat_payloads(state)) == 1


def test_provider_completed_invalid_content_is_not_adapter_failure(fixture_server) -> None:
    invalid_content = '{"kind":"action","name":"not-advertised","arguments":{}}'
    state = _FixtureState(chat_body=_FixtureState._chat_envelope(invalid_content))
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 0
    assert json.loads(stdout)["directive_content"] == invalid_content
    assert stderr == "\n"
    assert len(chat_payloads(state)) == 1


def test_observed_alias_payload_shape_is_forwarded_for_downstream_rejection(fixture_server) -> None:
    content = '{"action":"run_reproduction","transition":"Understand","payload":{"phase":"baseline"}}'
    state = _FixtureState(chat_body=_FixtureState._chat_envelope(content))
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, stderr = invoke(endpoint)
    assert rc == 0
    assert json.loads(stdout)["directive_content"] == content
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]


@pytest.mark.parametrize("content", [
    "{\"kind\":\"unknown\"}",
    "{\"kind\":\"action\",\"name\":\"apply_patch\",\"arguments\":{}}",
])
def test_illegal_directive_is_forwarded_before_success(fixture_server, content: str) -> None:
    state = _FixtureState(chat_body=_FixtureState._chat_envelope(content))
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, _stderr = invoke(endpoint)
    assert rc == 0
    assert json.loads(stdout)["directive_content"] == content


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
    assert_success_envelope(
        stdout,
        thinking_bytes=len(secret.encode("utf-8")),
    )
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]


def test_streamed_thinking_is_progress_but_only_content_has_authority(fixture_server) -> None:
    secret = "private-reasoning-must-not-cross-the-boundary"
    content = valid_content()
    frames = [
        {
            "model": adapter.EXPECTED_CLOUD_REMOTE_MODEL,
            "done": False,
            "message": {"role": "assistant", "thinking": secret, "content": ""},
        },
        {
            "model": adapter.EXPECTED_CLOUD_REMOTE_MODEL,
            "done": True,
            "done_reason": "stop",
            "message": {"role": "assistant", "thinking": "", "content": content},
        },
    ]
    state = _FixtureState(
        chat_body=b"".join(
            json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"
            for frame in frames
        )
    )
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 0
    assert secret not in stdout
    assert secret not in stderr
    assert stderr == "\n\n"
    assert_success_envelope(
        stdout,
        thinking_bytes=len(secret.encode("utf-8")),
        frame_count=2,
    )
    activity = json.loads(stdout)["transport_activity"]
    assert activity["first_content_frame_index"] == 1
    assert activity["last_content_frame_index"] == 1
    assert activity["content_frame_count"] == 1
    assert activity["content_frame_diagnostics"][0]["content_text"] == ""
    assert activity["content_frame_diagnostics"][0]["thinking_present"] is True
    assert secret not in json.dumps(activity)


def test_content_fragments_preserve_exact_order_and_first_prefix(fixture_server) -> None:
    content = valid_content()
    frames = [
        stream_frame(content=content[:6]),
        stream_frame(content=content[6:], done=True),
    ]
    state = _FixtureState(chat_body=stream_body(*frames))
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 0, stderr
    payload = json.loads(stdout)
    assert payload["directive_content"] == content[:6] + content[6:]
    activity = payload["transport_activity"]
    assert activity["content_frame_count"] == 2
    assert [item["frame_index"] for item in activity["content_frame_diagnostics"]] == [0, 1]
    assert [item["content_text"] for item in activity["content_frame_diagnostics"]] == [content[:6], content[6:]]
    assert activity["content_frame_diagnostics"][0]["content_sha256"] == hashlib.sha256(content[:6].encode()).hexdigest()


def test_punctuation_sensitive_content_fragments_preserve_exact_text_lengths_hashes_and_aggregate(fixture_server) -> None:
    fragments = [
        '{"', "kind", '\":\"', "action", '\",\"', "name", '\":\"',
        "run", "_re", "production", '\",\"', "arguments", '\":{"',
        "phase", '\":\"', "baseline", '"}}',
    ]
    content = "".join(fragments)
    frames = [stream_frame(content=fragment, done=index == len(fragments) - 1) for index, fragment in enumerate(fragments)]
    state = _FixtureState(chat_body=stream_body(*frames))
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 0, stderr
    payload = json.loads(stdout)
    activity = payload["transport_activity"]
    diagnostics = activity["content_frame_diagnostics"]
    assert [item["content_text"] for item in diagnostics] == fragments
    for fragment, item in zip(fragments, diagnostics):
        encoded = fragment.encode("utf-8")
        assert item["content_byte_length"] == len(encoded)
        assert item["content_sha256"] == hashlib.sha256(encoded).hexdigest()
        assert item["content_text_byte_length"] == len(encoded)
        assert item["content_text_sha256"] == hashlib.sha256(encoded).hexdigest()
        assert item["content_text_redacted"] is False
        assert item["content_text_truncated"] is False
    assert payload["directive_content"] == content
    assert activity["final_content_byte_length"] == len(content.encode("utf-8"))
    assert activity["final_content_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_truncated_content_observability_exposes_original_and_retained_semantics(fixture_server) -> None:
    content = "x" * (adapter.MAX_CONTENT_FRAGMENT_TEXT_BYTES + 17)
    state = _FixtureState(chat_body=stream_body(stream_frame(content=content, done=True)))
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 0, stderr
    record = json.loads(stdout)["transport_activity"]["content_frame_diagnostics"][0]
    assert record["content_text_truncated"] is True
    assert record["content_byte_length"] == len(content.encode("utf-8"))
    assert record["content_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert record["content_text_byte_length"] == len(record["content_text"].encode("utf-8"))
    assert record["content_text_sha256"] == hashlib.sha256(record["content_text"].encode("utf-8")).hexdigest()
    assert record["content_text"].endswith("...")


def test_thinking_and_content_same_frame_are_separately_observed(fixture_server) -> None:
    secret = "private-thinking-must-not-persist"
    frames = [stream_frame(content=valid_content(), thinking=secret, done=True)]
    state = _FixtureState(chat_body=stream_body(*frames))
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 0, stderr
    activity = json.loads(stdout)["transport_activity"]
    record = activity["content_frame_diagnostics"][0]
    assert record["both_channels_nonempty"] is True
    assert record["thinking_byte_length"] == len(secret.encode())
    assert secret not in stdout
    assert secret not in stderr
    assert secret not in json.dumps(activity)


def test_empty_content_fragments_and_done_content_are_retained_in_aggregate(fixture_server) -> None:
    content = valid_content()
    frames = [
        stream_frame(content="", thinking="progress"),
        stream_frame(content=content, done=True),
    ]
    state = _FixtureState(chat_body=stream_body(*frames))
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 0, stderr
    payload = json.loads(stdout)
    assert payload["directive_content"] == content
    activity = payload["transport_activity"]
    assert activity["stream_frame_count"] == 2
    assert activity["content_frame_count"] == 1
    assert activity["content_frame_diagnostics"][0]["content_byte_length"] == 0
    assert activity["content_frame_diagnostics"][1]["done"] is True


def test_unicode_content_is_not_sliced_and_uses_utf8_lengths(fixture_server) -> None:
    content = '{"kind":"action","name":"run_reproduction","arguments":{"phase":"baseline","note":"日本語🙂"}}'
    frames = [stream_frame(content=content[:30]), stream_frame(content=content[30:], done=True)]
    state = _FixtureState(chat_body=stream_body(*frames))
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 0, stderr
    payload = json.loads(stdout)
    assert payload["directive_content"] == content
    assert payload["transport_activity"]["final_content_byte_length"] == len(content.encode("utf-8"))
    assert payload["transport_activity"]["final_content_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_content_fragment_diagnostics_are_bounded_without_changing_content(fixture_server) -> None:
    fragments = [stream_frame(content="x") for _ in range(adapter.MAX_RETAINED_CONTENT_FRAME_DIAGNOSTICS + 1)]
    state = _FixtureState(chat_body=stream_body(*fragments, stream_frame(done=True)))
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 0, stderr
    payload = json.loads(stdout)
    assert payload["directive_content"] == "x" * (adapter.MAX_RETAINED_CONTENT_FRAME_DIAGNOSTICS + 1)
    activity = payload["transport_activity"]
    assert len(activity["content_frame_diagnostics"]) == adapter.MAX_RETAINED_CONTENT_FRAME_DIAGNOSTICS
    assert activity["content_frame_diagnostics_truncated"] is True
    assert activity["content_frame_count"] == adapter.MAX_RETAINED_CONTENT_FRAME_DIAGNOSTICS + 1


def test_malformed_ndjson_frame_fails_without_prefix_repair(fixture_server) -> None:
    state = _FixtureState(chat_body=b'{"model":"gpt-oss:20b"\n')
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, _stderr = invoke(endpoint)

    assert rc == 1
    assert stdout == ""


def test_transport_qualification_v2_separates_stream_and_protocol_results(fixture_server) -> None:
    state, _server, endpoint = fixture_server()
    with tempfile.TemporaryDirectory() as isolated_cwd:
        result = run_transport_qualification(
            endpoint=endpoint,
            model="gpt-oss:20b-cloud",
            adapter_command=(sys.executable, str(ADAPTER_SCRIPT)),
            cwd=isolated_cwd,
        )

    assert result["qualification_schema_version"] == "transport-qualification-v2"
    assert result["measurement_completed"] is True
    assert result["preflight_ok"] is True
    assert result["stream_transport_ok"] is True
    assert result["directive_protocol_ok"] is True
    assert result["directive_protocol"]["category"] == "DIRECTIVE_PROTOCOL_VERIFIED"
    assert result["effective_idle_timeout_seconds"] == 20.0
    assert result["effective_request_timeout_seconds"] == 60.0
    assert result["qualification"]["provider_completion"]["directive_content"] == valid_content()
    assert [path for path, _payload in state.requests] == [
        "/api/version",
        "/api/tags",
        "/api/show",
        "/api/tags",
        "/api/show",
        "/api/chat",
    ]
    preflight = result["qualification"]["preflight"]
    assert preflight["schema_version"] == "ollama-cloud-preflight-v1"
    assert preflight["ollama_version"] == adapter.EXPECTED_OLLAMA_VERSION
    assert preflight["expected_model"] == adapter.MODEL_ID
    assert preflight["expected_remote_model"] == adapter.EXPECTED_CLOUD_REMOTE_MODEL
    assert preflight["expected_tags_remote_model"] == adapter.EXPECTED_CLOUD_REMOTE_MODEL
    assert preflight["expected_remote_host"] == adapter.EXPECTED_CLOUD_REMOTE_HOST
    assert preflight["model_tag_digest"] == "synthetic-digest"
    assert preflight["model_capabilities"] == ["completion", "thinking", "tools"]
    assert preflight["readiness"] == "live_verified"
    assert preflight["transport_profile_declared"] is True
    assert preflight["model_transport_verified"] is True
    assert preflight["live_transport_ready"] is True
    assert preflight["treatment_eligible"] is True
    assert preflight["model_thinking_level"] == "high"
    assert preflight["idle_timeout_seconds"] == 20.0
    assert preflight["request_timeout_seconds"] == 60.0
    assert len(preflight["transport_config_fingerprint"]) == 64
    assert preflight["provider_inference_started"] is False
    assert preflight["cloud_inference_verified"] is False
    assert "private-thinking" not in json.dumps(result)


def test_transport_qualification_v2_rejects_historical_minimax_suffix_without_repair(fixture_server) -> None:
    rejected = '\":\"action\",\"name\":\"run_reproduction\",\"arguments\":{\"phase\":\"baseline\"}}'
    state = _FixtureState(chat_body=_FixtureState._chat_envelope(rejected, thinking="private-thinking"))
    _state, _server, endpoint = fixture_server(state)
    with tempfile.TemporaryDirectory() as isolated_cwd:
        result = run_transport_qualification(
            endpoint=endpoint,
            model="gpt-oss:20b-cloud",
            adapter_command=(sys.executable, str(ADAPTER_SCRIPT)),
            cwd=isolated_cwd,
        )

    assert result["measurement_completed"] is True
    assert result["preflight_ok"] is True
    assert result["stream_transport_ok"] is True
    assert result["directive_protocol_ok"] is False
    assert result["directive_protocol"]["category"] == "DIRECTIVE_INVALID_JSON"
    assert result["qualification"]["provider_completion"]["directive_content"] == rejected
    assert '{"kind' not in result["qualification"]["provider_completion"]["directive_content"]
    assert [path for path, _payload in state.requests] == [
        "/api/version",
        "/api/tags",
        "/api/show",
        "/api/tags",
        "/api/show",
        "/api/chat",
    ]
    assert "private-thinking" not in json.dumps(result)


def test_transport_qualification_preflight_failures_prevent_chat(fixture_server) -> None:
    wrong_tags = valid_tags_entry(remote_model="wrong-remote-model")
    cases = [
        _FixtureState(version="0.0.0"),
        _FixtureState(show_payload=encode_show(parent_model="wrong-parent-model")),
        _FixtureState(tags_payload=encode_tags(wrong_tags)),
        _FixtureState(tags_payload=b"not-json"),
    ]
    for state in cases:
        _state, _server, endpoint = fixture_server(state)
        with tempfile.TemporaryDirectory() as isolated_cwd:
            with pytest.raises(TransportQualificationError, match="preflight"):
                run_transport_qualification(
                    endpoint=endpoint,
                    model="gpt-oss:20b-cloud",
                    adapter_command=(sys.executable, str(ADAPTER_SCRIPT)),
                    cwd=isolated_cwd,
                )
        assert "/api/chat" not in request_paths(state)


def test_preflight_reports_canonical_timeout_profiles(fixture_server) -> None:
    profiles = [
        ("minimax-m3:cloud", "minimax-m3", 20.0, 60.0, "minimax-m3-digest"),
        ("nemotron-3-super:cloud", "nemotron-3-super", 45.0, 75.0, "nemotron-super-digest"),
    ]
    for alias, upstream, expected_idle, expected_request, digest in profiles:
        state = _FixtureState(
            tags_payload=encode_tags(
                valid_tags_entry(
                    name=alias,
                    model=alias,
                    remote_model=upstream,
                    digest=digest,
                )
            ),
            show_payload=encode_show(parent_model=upstream),
        )
        _state, _server, endpoint = fixture_server(state)
        stdout = io.StringIO()
        rc = adapter.run_adapter(
            stdin_stream=io.StringIO(""),
            stdout_stream=stdout,
            stderr_stream=io.StringIO(),
            argv=["--endpoint", endpoint, "--model", alias, "--preflight"],
        )
        assert rc == 0
        result = json.loads(stdout.getvalue())
        assert result["idle_timeout_seconds"] == expected_idle
        assert result["request_timeout_seconds"] == expected_request
        assert result["transport_config_fingerprint"] == adapter.transport_config_fingerprint(
            adapter.CLOUD_MODELS[alias]
        )


def test_standalone_adapter_process_boundary_returns_raw_completion_and_activity(fixture_server) -> None:
    sentinel = "private-thinking-process-boundary"
    content = valid_content()
    state = _FixtureState(chat_body=_FixtureState._chat_envelope(content, thinking=sentinel))
    _state, _server, endpoint = fixture_server(state)
    request = sample_request()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    with tempfile.TemporaryDirectory() as isolated_cwd:
        completed = subprocess.run(
            [
                sys.executable,
                str(ADAPTER_SCRIPT),
                "--endpoint",
                endpoint,
                "--model",
                "gpt-oss:20b-cloud",
            ],
            input=(json.dumps(request) + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=isolated_cwd,
            env=environment,
            check=False,
        )

    assert completed.returncode == 0
    assert sentinel.encode() not in completed.stdout
    assert sentinel.encode() not in completed.stderr
    provider_completion = json.loads(completed.stdout.decode("utf-8"))
    assert provider_completion["directive_content"] == content
    assert provider_completion["transport_activity"]["thinking_bytes"] == len(sentinel.encode("utf-8"))
    assert provider_completion["transport_activity"]["content_frame_diagnostics"]


def test_qualification_cli_zero_means_measurement_completed_not_protocol_pass(fixture_server, capsys) -> None:
    rejected = '\":\"action\",\"name\":\"run_reproduction\",\"arguments\":{\"phase\":\"baseline\"}}'
    state = _FixtureState(chat_body=_FixtureState._chat_envelope(rejected, thinking="private-thinking-cli"))
    _state, _server, endpoint = fixture_server(state)
    with tempfile.TemporaryDirectory() as isolated_cwd:
        rc = transport_qualification_main(
            [
                "--endpoint",
                endpoint,
                "--model",
                "gpt-oss:20b-cloud",
                "--adapter-script",
                str(ADAPTER_SCRIPT),
                "--adapter-cwd",
                isolated_cwd,
                "--confirm-live",
                "--json",
            ]
        )
    captured = capsys.readouterr()
    assert rc == 0
    result = json.loads(captured.out)
    assert result["measurement_completed"] is True
    assert result["stream_transport_ok"] is True
    assert result["directive_protocol_ok"] is False
    assert "private-thinking-cli" not in captured.out
    assert "private-thinking-cli" not in captured.err


def test_completed_thinking_only_stream_fails_closed_without_exposing_thinking(fixture_server) -> None:
    secret = "private-thinking-with-no-directive-content"
    frame = {
        "model": adapter.EXPECTED_CLOUD_REMOTE_MODEL,
        "done": True,
        "done_reason": "stop",
        "message": {"role": "assistant", "thinking": secret, "content": ""},
    }
    state = _FixtureState(
        chat_body=json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"
    )
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 1
    assert stdout == ""
    assert secret not in stderr
    errors = [json.loads(line) for line in stderr.splitlines() if line.strip().startswith("{")]
    assert errors == [
        {
            "kind": "invalid_response",
            "message": "Ollama assistant content is missing",
            "schema_version": "command-error-v1",
        }
    ]
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


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_streaming_chat_http_error_preserves_only_safe_status(
    fixture_server, status: int
) -> None:
    secret_body = b'{"error":"provider-secret-body","token":"not-for-telemetry"}'
    state = _FixtureState(chat_status=status, chat_body=secret_body)
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(endpoint)

    assert rc == 1
    assert stdout == ""
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]
    assert len(chat_payloads(state)) == 1
    errors = [json.loads(line) for line in stderr.splitlines() if line.strip()]
    assert errors == [
        {
            "kind": "http_error",
            "message": f"Ollama HTTP request returned status {status}",
            "schema_version": "command-error-v1",
        }
    ]
    assert "provider-secret-body" not in stderr
    assert "not-for-telemetry" not in stderr


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


def test_idle_inactivity_fails_before_outer_request_deadline(fixture_server) -> None:
    first = (
        json.dumps(stream_frame(content="partial"), separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    final = (
        json.dumps(stream_frame(done=True), separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    state = _FixtureState(chat_chunks=((0.0, first), (0.08, final)))
    _state, _server, endpoint = fixture_server(state)
    rc, stdout, stderr = invoke(endpoint, timeout=0.05, request_timeout=0.5)
    assert rc == 1
    assert stdout == ""
    errors = [json.loads(line) for line in stderr.splitlines() if line.strip().startswith("{")]
    assert errors[0]["kind"] == "timeout"
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]


def test_stream_quiet_longer_than_old_watchdog_survives_inside_outer_deadline(fixture_server) -> None:
    first = (
        json.dumps(stream_frame(content="partial"), separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    final = (
        json.dumps(stream_frame(done=True), separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    state = _FixtureState(chat_chunks=((0.0, first), (0.12, final)))
    _state, _server, endpoint = fixture_server(state)

    rc, stdout, stderr = invoke(
        endpoint,
        timeout=0.2,
        request_timeout=0.5,
    )

    assert rc == 0, stderr
    value = json.loads(stdout)
    assert value["directive_content"] == "partial"
    assert value["transport_activity"]["stream_frame_count"] == 2
    assert request_paths(state) == ["/api/tags", "/api/show", "/api/chat"]


def test_deepseek_stream_liveness_refreshes_after_a_synthetic_21_second_quiet_period(monkeypatch):
    spec = adapter.resolve_cloud_model("deepseek-v4-flash:cloud")
    clock = [1000.0]
    connections: list[object] = []

    class FakeSocket:
        def __init__(self):
            self.timeouts: list[float] = []

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

    class FakeResponse:
        status = 200

        def __init__(self, sock):
            self.sock = sock
            self._frames = iter(
                [
                    json.dumps(stream_frame(content="partial", frame_model=spec.upstream_model)).encode() + b"\n",
                    json.dumps(stream_frame(done=True, frame_model=spec.upstream_model)).encode() + b"\n",
                    b"",
                ]
            )

        def readline(self, _maximum: int) -> bytes:
            value = next(self._frames)
            if value:
                clock[0] += 21.0
            return value

    class FakeConnection:
        def __init__(self, *_args, **_kwargs):
            self.sock = FakeSocket()
            self.response = FakeResponse(self.sock)
            connections.append(self)

        def request(self, *_args, **_kwargs) -> None:
            return None

        def getresponse(self):
            return self.response

        def close(self) -> None:
            return None

    monkeypatch.setattr(adapter.http.client, "HTTPConnection", FakeConnection)
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    content, _usage, activity = adapter._stream_chat_request(
        "http://127.0.0.1:11434/api",
        sample_request(),
        spec,
        idle_timeout_seconds=300.0,
        request_deadline=4600.0,
        thinking_level=None,
        activity_stream=io.StringIO(),
    )
    assert content == "partial"
    assert activity["stream_frame_count"] == 2
    assert connections[0].sock.timeouts[0] == 300.0
    assert connections[0].sock.timeouts[1] == 300.0


def test_deepseek_stream_still_times_out_after_a_synthetic_301_second_quiet_period(monkeypatch):
    spec = adapter.resolve_cloud_model("deepseek-v4-flash:cloud")
    clock = [1000.0]
    class FakeResponse:
        status = 200
        def __init__(self):
            self.sock = SimpleNamespace(settimeout=lambda _value: None)
        def readline(self, _maximum: int) -> bytes:
            clock[0] += 301.0
            raise TimeoutError

    class FakeConnection:
        def __init__(self, *_args, **_kwargs):
            self.sock = SimpleNamespace(settimeout=lambda _value: None)
        def request(self, *_args, **_kwargs) -> None:
            return None
        def getresponse(self):
            return FakeResponse()
        def close(self) -> None:
            return None

    monkeypatch.setattr(adapter.http.client, "HTTPConnection", FakeConnection)
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    with pytest.raises(adapter.OllamaAdapterError, match="idle for too long") as exc_info:
        adapter._stream_chat_request(
            "http://127.0.0.1:11434/api",
            sample_request(),
            spec,
            idle_timeout_seconds=300.0,
            request_deadline=4600.0,
            thinking_level=None,
            activity_stream=io.StringIO(),
        )
    assert exc_info.value.kind == "timeout"


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
    assert result["expected_model"] == adapter.MODEL_ID
    assert result["expected_remote_model"] == adapter.EXPECTED_CLOUD_REMOTE_MODEL


def test_nemotron_preflight_uses_only_metadata_and_reports_nemotron_identity(fixture_server) -> None:
    state = nemotron_state()
    _state, _server, endpoint = fixture_server(state)
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = adapter.run_adapter(
        stdin_stream=io.StringIO(""),
        stdout_stream=stdout,
        stderr_stream=stderr,
        argv=["--endpoint", endpoint, "--model", NEMOTRON_ALIAS, "--preflight"],
    )
    assert rc == 0, stderr.getvalue()
    result = json.loads(stdout.getvalue())
    assert result["expected_model"] == NEMOTRON_ALIAS
    assert result["expected_remote_model"] == NEMOTRON_UPSTREAM
    assert result["model_remote_model"] == NEMOTRON_UPSTREAM
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
