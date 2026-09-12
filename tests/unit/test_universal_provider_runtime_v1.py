"""Universal Provider Runtime v1 + Model Selection v2 focused gates.

Fake/local transports only — no OpenCode Go quota is spent.  Proves the
product contract for the live failure (MissingSessionID) without
one-off header hacks:

- OpenCode session semantics (stable x-opencode-session + User-Agent on
  ALL inference paths, never on unrelated providers);
- model-aware routing (documented families + explicit override +
  fail-closed unknown);
- generic uniform providers unchanged;
- transport environment propagation (stable per session).
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_provider_server import (  # noqa: E402
    FakeProviderServer,
    scripted_chat_completion,
    scripted_messages_output,
    scripted_responses_output,
)

from agentic_debugger.application import provider_connections as pc  # noqa: E402
from agentic_debugger.application import provider_runtime as rt  # noqa: E402

import provider_direct_api_adapter as adapter  # noqa: E402

SECRET = "universal-runtime-test-credential-not-real"
SESSION_A = "0123456789abcdef0123456789abcdef"
SESSION_B = "fedcba9876543210fedcba9876543210"

_DIRECTIVE = (
    '{"kind": "action", "name": "get_source_window", '
    '"arguments": {"path": "pkg/mod.py", "start_line": 1, "end_line": 40}}'
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(tmp_path / "c.json"))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(tmp_path / "q.json"))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "oc-home"))
    monkeypatch.setenv("AGENTIC_DEBUGGER_DISABLE_SECURE_STORE", "1")
    for var in (
        "OPENCODE_API_KEY",
        "COMMAND_CODE_API_KEY",
        "OLLAMA_API_KEY",
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
        "AGENTIC_DEBUGGER_MODEL_SESSION_ID",
        "AGENTIC_DEBUGGER_OPENCODE_SESSION_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    pc.clear_all_session_keys()
    yield
    pc.clear_all_session_keys()


def _protocol_request(index: int = 0) -> dict:
    return {
        "protocol": {"version": "1.3", "logical_model_call_index": index},
        "context": {"task_id": "t", "state": "UNDERSTAND"},
    }


class _FakeStdin:
    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


def _make_opencode_provider(monkeypatch: pytest.MonkeyPatch, base_url: str, pid: str = "oc_rt"):
    return pc.add_provider_config(
        name="OC Runtime",
        base_url=base_url,
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id=pid,
        transport_profile=pc.TRANSPORT_OPENCODE_GO,
        api_key=SECRET,
    )


def _run(provider: str, model: str, protocol: str, session_id: Any = None):
    stdin = _FakeStdin(json.dumps(_protocol_request()).encode())
    out, err = io.StringIO(), io.StringIO()
    kwargs: dict = {}
    if session_id is not None:
        kwargs["session_id"] = session_id
    try:
        code = adapter.run_adapter(
            stdin, out, provider=provider, model=model, protocol=protocol,
            timeout_seconds=10.0, **kwargs,
        )
    except adapter.ProviderDirectApiError as exc:
        return 1, "", exc.kind, str(exc)
    return code, out.getvalue(), "", ""


# -- runtime profile ownership ------------------------------------------------

class TestRuntimeProfileOwnership:
    def test_opencode_profile_requires_session_and_agent(self):
        profile = rt.runtime_profile_for_transport("opencode_go")
        assert profile.requires_session_header is True
        assert profile.session_header_name == "x-opencode-session"
        assert (profile.user_agent or "").startswith("AgenticDebugger/")

    def test_generic_profile_requires_nothing(self):
        profile = rt.runtime_profile_for_transport("generic")
        assert profile.requires_session_header is False
        assert rt.static_request_headers(profile) == {}
        assert rt.session_request_headers(profile, SESSION_A) == {}

    def test_session_headers_fail_closed_when_required_but_missing(self):
        profile = rt.runtime_profile_for_transport("opencode_go")
        with pytest.raises(ValueError):
            rt.session_request_headers(profile, None)
        with pytest.raises(ValueError):
            rt.session_request_headers(profile, "not a valid id!!!")

    def test_new_session_id_shape(self):
        first, second = rt.new_transport_session_id(), rt.new_transport_session_id()
        assert rt.is_valid_transport_session_id(first)
        assert rt.is_valid_transport_session_id(second)
        assert first != second


# -- routing -------------------------------------------------------------------

class TestModelAwareRouting:
    def test_documented_families(self, monkeypatch: pytest.MonkeyPatch):
        with FakeProviderServer(lambda req: (200, {"data": []})) as server:
            _make_opencode_provider(monkeypatch, server.base_url)
            assert pc.resolve_model_protocol("oc_rt", "muse-spark-1.2-contributor") == pc.PROTOCOL_RESPONSES
            assert pc.resolve_model_protocol("oc_rt", "muse-spark-1.3-contributor") == pc.PROTOCOL_RESPONSES
            assert pc.resolve_model_protocol("oc_rt", "opencode-go/muse-spark-1.3-contributor") == pc.PROTOCOL_RESPONSES
            assert pc.resolve_model_protocol("oc_rt", "gpt-5.6-luna") == pc.PROTOCOL_RESPONSES
            for mid in ("deepseek-v4-flash", "glm-5.3", "glm-5.1", "kimi-k3", "kimi-k2.7-code", "kimi-k2.6"):
                assert pc.resolve_model_protocol("oc_rt", mid) == pc.PROTOCOL_CHAT_COMPLETIONS, mid
            assert pc.resolve_model_protocol("oc_rt", "qwen3.8-max") == pc.PROTOCOL_MESSAGES
            assert pc.effective_model_protocol("oc_rt", "muse-spark-1.3-contributor") == pc.PROTOCOL_RESPONSES
            assert pc.effective_model_protocol("oc_rt", "gpt-5.6-luna") == pc.PROTOCOL_RESPONSES
            assert pc.effective_model_protocol("oc_rt", "deepseek-v4-flash") == pc.PROTOCOL_CHAT_COMPLETIONS
            assert pc.effective_model_protocol("oc_rt", "qwen3.8-max") == pc.PROTOCOL_MESSAGES

    def test_unknown_model_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        with FakeProviderServer(lambda req: (200, {"data": []})) as server:
            _make_opencode_provider(monkeypatch, server.base_url)
            assert pc.resolve_model_protocol("oc_rt", "future-unknown-zzz-9") is None
            with pytest.raises(pc.ProviderConnectionError):
                pc.effective_model_protocol("oc_rt", "future-unknown-zzz-9")
            with pytest.raises(Exception):
                from agentic_debugger.application.model_providers import resolve_provider_live_config
                resolve_provider_live_config("oc_rt", "future-unknown-zzz-9")

    def test_explicit_override_wins_over_table(self, monkeypatch: pytest.MonkeyPatch):
        with FakeProviderServer(lambda req: (200, {"data": []})) as server:
            _make_opencode_provider(monkeypatch, server.base_url)
            # glm-5.3 is documented as chat_completions; an explicit manual
            # override routes it to messages instead.
            pc.add_manual_model("oc_rt", "glm-5.3", protocol=pc.PROTOCOL_MESSAGES)
            assert pc.resolve_model_protocol("oc_rt", "glm-5.3") == pc.PROTOCOL_MESSAGES
            assert pc.effective_model_protocol("oc_rt", "glm-5.3") == pc.PROTOCOL_MESSAGES
            # A wholly unknown id becomes routable through the override.
            pc.add_manual_model("oc_rt", "future-unknown-zzz-9", protocol=pc.PROTOCOL_RESPONSES)
            assert pc.resolve_model_protocol("oc_rt", "future-unknown-zzz-9") == pc.PROTOCOL_RESPONSES


# -- session semantics across all three protocol paths ------------------------

def _opencode_server(monkeypatch: pytest.MonkeyPatch, responder: Callable):
    @contextmanager
    def factory():
        with FakeProviderServer(responder) as server:
            _make_opencode_provider(monkeypatch, server.base_url)
            # Credential channel (vault-issued in product; synthetic here).
            monkeypatch.setenv("AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY", SECRET)
            monkeypatch.setenv("AGENTIC_DEBUGGER_MODEL_SESSION_ID", SESSION_A)
            monkeypatch.setenv("AGENTIC_DEBUGGER_OPENCODE_SESSION_ID", SESSION_A)
            yield server
    return factory()


class TestOpenCodeSessionSemantics:
    def test_responses_carries_stable_session_and_agent(self, monkeypatch: pytest.MonkeyPatch):
        with _opencode_server(monkeypatch, lambda req: (200, scripted_responses_output(_DIRECTIVE))) as server:
            code, out, _, _ = _run("oc_rt", "gpt-5.6-luna", "responses", session_id=SESSION_A)
            assert code == 0, out
            first = server.requests[0]
            assert first["path"] == "/responses"
            assert first["x_opencode_session"] == SESSION_A
            assert "AgenticDebugger" in (first["user_agent"] or "")
            # Second request in the same session reuses the identical value.
            code, out, _, _ = _run("oc_rt", "muse-spark-1.3-contributor", "responses", session_id=SESSION_A)
            assert code == 0
            assert server.requests[1]["x_opencode_session"] == SESSION_A

    def test_chat_completions_path_carries_session(self, monkeypatch: pytest.MonkeyPatch):
        with _opencode_server(monkeypatch, lambda req: (200, scripted_chat_completion(_DIRECTIVE))) as server:
            code, out, _, _ = _run("oc_rt", "deepseek-v4-flash", "chat_completions", session_id=SESSION_A)
            assert code == 0
            assert server.requests[0]["path"] == "/chat/completions"
            assert server.requests[0]["x_opencode_session"] == SESSION_A
            assert "AgenticDebugger" in (server.requests[0]["user_agent"] or "")

    def test_messages_path_carries_session(self, monkeypatch: pytest.MonkeyPatch):
        with _opencode_server(monkeypatch, lambda req: (200, scripted_messages_output(_DIRECTIVE))) as server:
            code, out, _, _ = _run("oc_rt", "qwen3.8-max", "messages", session_id=SESSION_A)
            assert code == 0
            assert server.requests[0]["path"] == "/messages"
            assert server.requests[0]["x_opencode_session"] == SESSION_A

    def test_env_channel_resolves_when_argv_omitted(self, monkeypatch: pytest.MonkeyPatch):
        with _opencode_server(monkeypatch, lambda req: (200, scripted_chat_completion(_DIRECTIVE))) as server:
            code, out, _, _ = _run("oc_rt", "glm-5.3", "chat_completions")
            assert code == 0
            assert server.requests[0]["x_opencode_session"] == SESSION_A

    def test_distinct_sessions_may_differ(self, monkeypatch: pytest.MonkeyPatch):
        with _opencode_server(monkeypatch, lambda req: (200, scripted_chat_completion(_DIRECTIVE))) as server:
            assert _run("oc_rt", "kimi-k3", "chat_completions", session_id=SESSION_A)[0] == 0
            assert _run("oc_rt", "kimi-k3", "chat_completions", session_id=SESSION_B)[0] == 0
            assert server.requests[0]["x_opencode_session"] == SESSION_A
            assert server.requests[1]["x_opencode_session"] == SESSION_B

    def test_missing_session_fails_closed_with_zero_http(self, monkeypatch: pytest.MonkeyPatch):
        with FakeProviderServer(lambda req: (200, scripted_chat_completion(_DIRECTIVE))) as server:
            _make_opencode_provider(monkeypatch, server.base_url)
            monkeypatch.setenv("AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY", SECRET)
            monkeypatch.delenv("AGENTIC_DEBUGGER_MODEL_SESSION_ID", raising=False)
            monkeypatch.delenv("AGENTIC_DEBUGGER_OPENCODE_SESSION_ID", raising=False)
            code, _, kind, _ = _run("oc_rt", "deepseek-v4-flash", "chat_completions")
            assert code == 1 and kind == "configuration"
            assert server.request_count == 0

    def test_unrelated_provider_receives_no_opencode_header(self, monkeypatch: pytest.MonkeyPatch):
        with FakeProviderServer(lambda req: (200, scripted_chat_completion(_DIRECTIVE))) as server:
            pc.add_provider_config(
                name="Generic", base_url=server.base_url,
                api_format=pc.PROTOCOL_CHAT_COMPLETIONS, api_key=SECRET,
                provider_id="generic_rt",
            )
            pc.add_manual_model("generic_rt", "generic-model")
            monkeypatch.setenv("AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY", SECRET)
            from agentic_debugger.application.provider_credentials import provider_session_credential_variable
            monkeypatch.setenv(provider_session_credential_variable("generic_rt"), SECRET)
            # Even with an OpenCode session lingering in the environment,
            # the generic route must not emit it.
            monkeypatch.setenv("AGENTIC_DEBUGGER_MODEL_SESSION_ID", SESSION_A)
            stdin = _FakeStdin(json.dumps(_protocol_request()).encode())
            out = io.StringIO()
            code = adapter.run_adapter(
                stdin, out, provider="generic_rt", model="generic-model",
                protocol="chat_completions", timeout_seconds=10.0,
            )
            assert code == 0
            assert server.requests[0]["x_opencode_session"] is None
            assert "AgenticDebugger" not in (server.requests[0]["user_agent"] or "")


# -- generic + transport environment -------------------------------------------

class TestGenericAndTransportEnvironment:
    def test_generic_uniform_provider_works_with_key_only(self, monkeypatch: pytest.MonkeyPatch):
        with FakeProviderServer(lambda req: (200, scripted_chat_completion(_DIRECTIVE))) as server:
            pc.add_provider_config(
                name="Generic", base_url=server.base_url,
                api_format=pc.PROTOCOL_CHAT_COMPLETIONS, api_key=SECRET,
                provider_id="generic_only",
            )
            pc.add_manual_model("generic_only", "m1")
            from agentic_debugger.application.provider_credentials import provider_session_credential_variable
            monkeypatch.setenv(provider_session_credential_variable("generic_only"), SECRET)
            code, out, _, _ = _run("generic_only", "m1", "chat_completions")
            assert code == 0
            assert json.loads(out)["directive_content"] == _DIRECTIVE

    def test_transport_environment_carries_stable_session(self, monkeypatch: pytest.MonkeyPatch):
        from agentic_debugger.application.model_gateway import ModelGateway

        with FakeProviderServer(lambda req: (200, {"data": []})) as server:
            _make_opencode_provider(monkeypatch, server.base_url)
            gateway = ModelGateway.default()
            binding = gateway.resolve(provider_id="oc_rt", model_id="gpt-5.6-luna")
            env_a = gateway.transport_environment(binding, session_id=SESSION_A)
            assert env_a is not None
            assert env_a.get("AGENTIC_DEBUGGER_MODEL_SESSION_ID") == SESSION_A
            # Same session id reused verbatim; omitted identities are
            # freshly issued (different sessions differ).
            env_a2 = gateway.transport_environment(binding, session_id=SESSION_A)
            assert env_a2.get("AGENTIC_DEBUGGER_MODEL_SESSION_ID") == SESSION_A
            env_auto1 = gateway.transport_environment(binding)
            env_auto2 = gateway.transport_environment(binding)
            assert env_auto1.get("AGENTIC_DEBUGGER_MODEL_SESSION_ID")
            assert env_auto2.get("AGENTIC_DEBUGGER_MODEL_SESSION_ID")
            assert env_auto1["AGENTIC_DEBUGGER_MODEL_SESSION_ID"] != env_auto2["AGENTIC_DEBUGGER_MODEL_SESSION_ID"]

    def test_generic_transport_carries_no_session_env(self, monkeypatch: pytest.MonkeyPatch):
        from agentic_debugger.application.model_gateway import ModelGateway

        with FakeProviderServer(lambda req: (200, {"data": []})) as server:
            pc.add_provider_config(
                name="Generic", base_url=server.base_url,
                api_format=pc.PROTOCOL_CHAT_COMPLETIONS, api_key=SECRET,
                provider_id="generic_env",
            )
            pc.add_manual_model("generic_env", "m1")
            gateway = ModelGateway.default()
            binding = gateway.resolve(provider_id="generic_env", model_id="m1")
            env = gateway.transport_environment(binding, session_id=SESSION_A)
            assert env is not None
            assert "AGENTIC_DEBUGGER_MODEL_SESSION_ID" not in env
            assert "AGENTIC_DEBUGGER_OPENCODE_SESSION_ID" not in env
