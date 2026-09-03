"""Unit gates for the provider direct-API command adapter.

Exercises the accepted protocol-1.3 JSONL command contract over the
three provider protocol families against a local fake provider server:
strict request/response parsing, usage passthrough (never fabricated),
exactly-one-inference (zero adapter retry), typed failure envelopes,
credential-boundary behavior, and timeout/oversize fail-closed paths.
No real provider is contacted and no generation spend occurs.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any, Callable, Optional

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

import provider_direct_api_adapter as adapter  # noqa: E402

SECRET = "adapter-test-credential-not-real"

_DIRECTIVE = (
    '{"kind": "action", "name": "get_source_window", '
    '"arguments": {"path": "pkg/mod.py", "start_line": 1, "end_line": 40}}'
)


def _protocol_request(index: int = 0) -> dict:
    return {
        "protocol": {"version": "1.3", "logical_model_call_index": index},
        "context": {"task_id": "task", "state": "UNDERSTAND"},
    }


class _FakeStdin:
    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


@pytest.fixture(autouse=True)
def _clean_session_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pc.clear_all_session_keys()
    monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: tmp_path / "missing-auth.json")
    monkeypatch.setattr(pc, "load_secure_credential", lambda kind: None)
    monkeypatch.setattr(pc, "has_secure_credential", lambda kind: False)
    monkeypatch.setattr(pc, "provider_configurations_path", lambda: tmp_path / "provider-configurations.json")
    for name in (
        "OPENCODE_API_KEY",
        "COMMAND_CODE_API_KEY",
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    # The provider store is user-owned: the two builtin direct-API providers
    # must exist explicitly (as in production) so model/protocol resolution
    # follows the real configured path instead of an auto-seeded fallback.
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
    )
    pc.add_provider_config(
        name="OpenCode Go",
        base_url="https://opencode.ai/provider/v1",
        api_format=pc.PROTOCOL_RESPONSES,
        provider_id="opencode_go",
        transport_profile=pc.TRANSPORT_OPENCODE_GO,
    )
    yield
    pc.clear_all_session_keys()


@pytest.fixture
def fake_commandcode(monkeypatch: pytest.MonkeyPatch):
    """Adapter runs against a local fake of the CommandCode endpoint."""

    @contextmanager
    def factory(responder: Callable[[Any], Any]) -> Any:
        with FakeProviderServer(responder) as server:
            original = pc._CONTRACTS["commandcode_goat"]
            fake = dataclass_replace(
                original, base_url=server.base_url, tls_signature_blocked=False
            )
            monkeypatch.setitem(pc._CONTRACTS, "commandcode_goat", fake)
            # The user-owned provider configuration owns the runtime base
            # URL; point it at the fake server like an operator would.
            pc.update_provider_config(
                "commandcode_goat", base_url=server.base_url
            )
            monkeypatch.setenv("COMMAND_CODE_API_KEY", SECRET)
            yield server

    return factory


@pytest.fixture
def fake_opencode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Adapter runs against a local fake of the OpenCode Go endpoint,
    with a scripted consumable CLI auth store for credential resolution."""

    @contextmanager
    def factory(responder: Callable[[Any], Any]) -> Any:
        with FakeProviderServer(responder) as server:
            original = pc._CONTRACTS["opencode_go"]
            fake = dataclass_replace(
                original, base_url=server.base_url, tls_signature_blocked=False
            )
            monkeypatch.setitem(pc._CONTRACTS, "opencode_go", fake)
            pc.update_provider_config("opencode_go", base_url=server.base_url)
            store = tmp_path / "auth.json"
            store.write_text(
                json.dumps({"opencode-go": {"type": "api", "key": SECRET}}),
                encoding="utf-8",
            )
            monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: store)
            monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
            yield server

    return factory


def run_adapter(
    provider: str = "commandcode_goat",
    model: str = "deepseek/deepseek-v4-flash",
    protocol: str = "chat_completions",
    request: Optional[dict] = None,
    timeout: float = 20.0,
) -> tuple[int, str, str]:
    request = request if request is not None else _protocol_request()
    stdin = _FakeStdin(json.dumps(request).encode("utf-8"))
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        code = adapter.run_adapter(
            stdin,
            stdout,
            provider=provider,
            model=model,
            protocol=protocol,
            timeout_seconds=timeout,
        )
    except adapter.ProviderDirectApiError as exc:
        # Mirror the main() envelope contract for typed failures.
        return (
            1,
            "",
            json.dumps(
                {
                    "schema_version": "command-error-v1",
                    "kind": exc.kind,
                    "message": str(exc),
                }
            ),
        )
    return code, stdout.getvalue(), stderr.getvalue()


class TestInferenceFamilies:
    def test_chat_completions_direct_inference(
        self, fake_commandcode
    ) -> None:
        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(_DIRECTIVE))
        ) as server:
            code, out, err = run_adapter()
            assert code == 0
        payload = json.loads(out)
        assert payload["provider_completion_schema_version"] == "provider-completion-v1"
        assert payload["directive_content"] == _DIRECTIVE
        assert payload["usage"] == {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
        request_record = server.requests[0]
        assert request_record["path"] == "/chat/completions"
        body = json.loads(request_record["body"].decode("utf-8"))
        assert body["model"] == "deepseek/deepseek-v4-flash"
        assert body["stream"] is False

    def test_responses_family_direct_inference(self, fake_opencode) -> None:
        with fake_opencode(
            lambda request: (200, scripted_responses_output(_DIRECTIVE))
        ) as server:
            code, out, err = run_adapter(
                provider="opencode_go",
                model="opencode-go/gpt-5.6-luna",
                protocol="responses",
            )
            assert code == 0
        payload = json.loads(out)
        assert payload["directive_content"] == _DIRECTIVE
        assert server.requests[0]["path"] == "/responses"
        body = json.loads(server.requests[0]["body"].decode("utf-8"))
        # The Responses family uses its dedicated instructions channel for
        # the system instruction; the user message carries the public
        # request (never a system+user concatenation).
        assert body["instructions"].startswith("You are")
        assert "=== BEGIN PUBLIC REQUEST ===" in body["input"]
        assert not body["input"].startswith("You are")

    def test_messages_family_direct_inference(self, fake_commandcode) -> None:
        with fake_commandcode(
            lambda request: (200, scripted_messages_output(_DIRECTIVE))
        ) as server:
            code, out, err = run_adapter(
                model="claude-sonnet-5", protocol="messages"
            )
            assert code == 0
        payload = json.loads(out)
        assert payload["directive_content"] == _DIRECTIVE
        assert server.requests[0]["path"] == "/messages"
        body = json.loads(server.requests[0]["body"].decode("utf-8"))
        assert body["max_tokens"] == adapter._MESSAGES_MAX_TOKENS
        # The Anthropic Messages family uses the dedicated top-level system
        # channel; the user message is bounded user input only.
        assert body["system"].startswith("You are")
        assert [m["role"] for m in body["messages"]] == ["user"]
        assert "=== BEGIN PUBLIC REQUEST ===" in body["messages"][-1]["content"]

    def test_messages_family_maps_input_tokens(self, fake_commandcode) -> None:
        payload_body = scripted_messages_output(_DIRECTIVE)
        with fake_commandcode(lambda request: (200, payload_body)):
            code, out, err = run_adapter(
                model="claude-sonnet-5", protocol="messages"
            )
        usage = json.loads(out)["usage"]
        assert usage == {"prompt_tokens": 11, "completion_tokens": 7}

    def test_missing_usage_is_not_fabricated(self, fake_commandcode) -> None:
        body = scripted_chat_completion(_DIRECTIVE)
        body.pop("usage")
        with fake_commandcode(lambda request: (200, body)):
            code, out, err = run_adapter()
        payload = json.loads(out)
        assert "usage" not in payload

    def test_prompt_uses_shared_protocol_shaping(self, fake_commandcode) -> None:
        """One provider-neutral protocol-1.3 shaping, imported not duplicated:
        a real system role plus the request-shaped user message."""

        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(_DIRECTIVE))
        ) as server:
            run_adapter()
        body = json.loads(server.requests[0]["body"].decode("utf-8"))
        import protocol_prompt_shaper as shaper

        assert [message["role"] for message in body["messages"]] == ["user"]
        assert body["messages"][0]["content"].startswith(shaper.SYSTEM_PROMPT.rstrip())
        assert (
            "=== BEGIN PUBLIC REQUEST ==="
            in body["messages"][0]["content"]
        )
        assert (
            "Current request legal decision surface:"
            in body["messages"][0]["content"]
        )


class TestCredentialBoundary:
    def test_bearer_header_carries_credential(self, fake_commandcode) -> None:
        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(_DIRECTIVE))
        ) as server:
            run_adapter()
        assert server.requests[0]["authorization"] == f"Bearer {SECRET}"

    def test_injected_session_credential_wins(self, fake_commandcode, monkeypatch) -> None:
        # The session key is installed after the fixture's endpoint
        # configuration: an endpoint change with an already-associated
        # credential requires explicit key re-entry (fail-closed binding).
        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(_DIRECTIVE))
        ) as server:
            pc.set_session_key("commandcode_goat", "session-key-value")
            monkeypatch.delenv("COMMAND_CODE_API_KEY", raising=False)
            code, out, err = run_adapter()
            assert code == 0
        assert server.requests[0]["authorization"] == "Bearer session-key-value"

    def test_missing_credential_fails_closed(self, fake_commandcode, monkeypatch) -> None:
        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(_DIRECTIVE))
        ):
            monkeypatch.delenv("COMMAND_CODE_API_KEY", raising=False)
            code, out, err = run_adapter()
        assert code == 1
        envelope = json.loads(err)
        assert envelope["kind"] == "configuration"
        assert SECRET not in err

    def test_credential_never_in_stdout_or_envelope(self, fake_commandcode) -> None:
        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(_DIRECTIVE))
        ):
            code, out, err = run_adapter()
        assert SECRET not in out
        assert SECRET not in err

    def test_credential_never_in_argv(self) -> None:
        """The subprocess contract: argv carries no credential material."""

        from agentic_debugger.application.model_providers import (
            resolve_provider_live_config,
        )

        pc.set_session_key("opencode_go", SECRET)
        config, provenance = resolve_provider_live_config(
            "opencode_go", "opencode-go/deepseek-v4-flash"
        )
        assert all(SECRET not in part for part in config.command)
        assert not any("key" in part.lower() for part in config.command)
        assert SECRET not in json.dumps(provenance)


class TestExactlyOneInference:
    def test_success_performs_exactly_one_request(self, fake_commandcode) -> None:
        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(_DIRECTIVE))
        ) as server:
            run_adapter()
            assert server.request_count == 1

    def test_provider_error_gets_no_retry(self, fake_commandcode) -> None:
        """A provider HTTP failure is terminal inside the adapter: the
        accepted LiveModelAdapter owns retry attempts above this boundary."""

        with fake_commandcode(lambda request: (500, {"error": "down"})) as server:
            code, out, err = run_adapter()
            assert server.request_count == 1
        assert code == 1
        envelope = json.loads(err)
        assert envelope["schema_version"] == "command-error-v1"
        assert envelope["kind"] == "http_error"
        assert "down" in envelope["message"]

    def test_malformed_provider_response_gets_no_retry(
        self, fake_commandcode
    ) -> None:
        with fake_commandcode(lambda request: (200, {"unexpected": "shape"})) as server:
            code, out, err = run_adapter()
            assert server.request_count == 1
        assert code == 1
        assert json.loads(err)["kind"] == "invalid_completion"

    def test_invalid_json_response_gets_no_retry(self, fake_commandcode) -> None:
        with fake_commandcode(lambda request: (200, "not json")) as server:
            code, out, err = run_adapter()
            assert server.request_count == 1
        assert code == 1
        assert json.loads(err)["kind"] == "invalid_response"

    def test_http_400_surfaces_status_and_sanitized_snippet_without_credentials(
        self, fake_commandcode
    ) -> None:
        """Provider 400 error surfaces HTTP status and sanitized message snippet,
        with zero credential or authorization leakage."""
        provider_err = {
            "error": {
                "message": "unsupported role: system",
                "type": "invalid_request_error",
            }
        }
        with fake_commandcode(lambda request: (400, provider_err)):
            code, out, err = run_adapter()
        assert code == 1
        envelope = json.loads(err)
        assert envelope["schema_version"] == "command-error-v1"
        assert envelope["kind"] == "http_error"
        msg = envelope["message"]
        # HTTP status visible
        assert "400" in msg
        # Bounded sanitized provider error visible
        assert "unsupported role: system" in msg
        # Credential absent
        assert SECRET not in msg
        assert SECRET not in err
        # Authorization value absent
        assert "Bearer" not in msg


class TestFailClosed:
    def test_oversized_completion_rejected(self, fake_commandcode) -> None:
        big_content = '{"kind": "action"}' + "x" * (adapter.frozen.MAX_RAW_RESPONSE_BYTES + 10)
        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(big_content))
        ):
            code, out, err = run_adapter()
        assert code == 1
        assert json.loads(err)["kind"] == "response_too_large"

    def test_completion_without_json_directive_rejected(self, fake_commandcode) -> None:
        with fake_commandcode(
            lambda request: (200, scripted_chat_completion("no directive here"))
        ):
            code, out, err = run_adapter()
        assert code == 1
        assert json.loads(err)["kind"] == "invalid_directive"

    def test_empty_completion_rejected(self, fake_commandcode) -> None:
        with fake_commandcode(lambda request: (200, scripted_chat_completion("   "))):
            code, out, err = run_adapter()
        assert code == 1
        assert json.loads(err)["kind"] == "invalid_completion"

    def test_declared_protocol_mismatch_rejected(self, fake_commandcode) -> None:
        """The adapter never executes a model under a guessed protocol."""

        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(_DIRECTIVE))
        ):
            code, out, err = run_adapter(protocol="messages")
        assert code == 1
        assert json.loads(err)["kind"] == "configuration"

    def test_unknown_provider_rejected(self) -> None:
        code, out, err = run_adapter(provider="mystery_provider")
        assert code == 1
        assert json.loads(err)["kind"] == "configuration"

    def test_missing_stdin_request_rejected(self, fake_commandcode) -> None:
        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(_DIRECTIVE))
        ):
            code, out, err = run_adapter(request={})
        assert code == 1
        assert json.loads(err)["kind"] == "invalid_request"

    def test_logical_call_limit_enforced(self, fake_commandcode) -> None:
        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(_DIRECTIVE))
        ):
            code, out, err = run_adapter(request=_protocol_request(index=999))
        assert code == 1
        assert json.loads(err)["kind"] == "logical_call_limit"

    def test_oversized_stdin_request_rejected(self, fake_commandcode) -> None:
        request = _protocol_request()
        request["padding"] = "z" * (adapter.frozen.MAX_PUBLIC_REQUEST_BYTES + 10)
        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(_DIRECTIVE))
        ):
            code, out, err = run_adapter(request=request)
        assert code == 1

    def test_timeout_typed_envelope(self, fake_commandcode) -> None:
        def slow_responder(request):
            import time

            time.sleep(2)
            return 200, scripted_chat_completion(_DIRECTIVE)

        with fake_commandcode(slow_responder):
            code, out, err = run_adapter(timeout=0.5)
        assert code == 1
        assert json.loads(err)["kind"] == "timeout"

    def test_error_message_is_bounded_single_line(self, fake_commandcode) -> None:
        with fake_commandcode(lambda request: (500, {"error": "boom " * 500})):
            code, out, err = run_adapter()
        envelope = json.loads(err)
        assert len(envelope["message"]) <= 400
        assert "\n" not in envelope["message"]


class TestSubprocessContract:
    def test_cli_invocation_end_to_end(self, fake_commandcode, tmp_path) -> None:
        """The full argv contract works in a real child process against
        the fake provider.  The bootstrap mirrors the accepted worker
        spawn: repo root on sys.path (the bare-child import path), then
        the adapter's real ``main()``."""

        with fake_commandcode(
            lambda request: (200, scripted_chat_completion(_DIRECTIVE))
        ) as server:
            # An isolated user-owned provider store for the child: the
            # child must never read (or hit) the operator's real
            # provider configuration.
            child_config = tmp_path / "child-provider-config.json"
            child_config.write_text(
                json.dumps(
                    {
                        "schema_version": "provider-configurations-v2",
                        "providers": [
                            {
                                "name": "CommandCode GOAT",
                                "provider_id": "commandcode_goat",
                                "base_url": server.base_url,
                                "api_format": "chat_completions",
                                "auth_mode": "bearer",
                                "catalog_mode": "openai",
                                "transport_profile": "commandcode_goat",
                                "models": [],
                                "enabled": True,
                                "is_builtin": False,
                                "builtin_kind": None,
                                "tls_signature_blocked": False,
                                "last_refresh_utc": None,
                                "last_refresh_source": None,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            bootstrap = (
                "import sys; "
                f"sys.path.insert(0, {str(REPO_ROOT)!r}); "
                f"sys.path.insert(0, {str(REPO_ROOT / 'scripts')!r}); "
                "from dataclasses import replace; "
                "from agentic_debugger.application import provider_connections as pc; "
                "pc._CONTRACTS['commandcode_goat'] = replace("
                "pc._CONTRACTS['commandcode_goat'], "
                f"base_url={server.base_url!r}, tls_signature_blocked=False); "
                "import runpy; runpy.run_path("
                f"{str(REPO_ROOT / 'scripts' / 'provider_direct_api_adapter.py')!r}, "
                "run_name='__main__')"
            )
            child_argv = [
                sys.executable,
                "-c",
                bootstrap,
                "--provider", "commandcode_goat",
                "--model", "deepseek/deepseek-v4-flash",
                "--protocol", "chat_completions",
                "--timeout", "20",
                "--engine", "stdlib",
            ]
            request_bytes = (json.dumps(_protocol_request()) + "\n").encode("utf-8")
            result = subprocess.run(
                child_argv,
                input=request_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                cwd=str(tmp_path),
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "COMMAND_CODE_API_KEY": SECRET,
                    "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH": str(child_config),
                    "PYTHONIOENCODING": "utf-8",
                    "SystemRoot": os.environ.get("SystemRoot", ""),
                },
            )
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        payload = json.loads(result.stdout.decode("utf-8"))
        assert payload["directive_content"] == _DIRECTIVE
        assert SECRET not in result.stdout.decode("utf-8")
