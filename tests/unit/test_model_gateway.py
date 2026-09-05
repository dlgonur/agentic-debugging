"""Unit tests for ModelGateway and ModelBinding (V2-03).

Verifies ModelBinding immutability, credential scrubbing, deterministic
fingerprinting, error taxonomy, static preflight, reachability probing,
and catalog refresh.
"""

from __future__ import annotations

import dataclasses
import http.server
import json
import threading
from pathlib import Path
from typing import Generator
import pytest

from agentic_debugger.application.model_gateway import (
    ModelBinding,
    ModelGateway,
    ModelStaticPreflight,
    ProviderStatusSnapshot,
    ModelGatewayError,
    ProviderConfigurationError,
    CredentialUnavailableError,
    IncompatibleModelError,
    EndpointUnreachableError,
    CatalogProbeError,
    ROUTE_DIRECT_API,
    contains_credential_shape,
)
from agentic_debugger.application.provider_connections import (
    add_provider_config,
    update_provider_config,
    quarantine_provider,
    PROTOCOL_CHAT_COMPLETIONS,
    AUTH_BEARER,
    AUTH_NONE,
    TRANSPORT_GENERIC,
)


class _MockHandler(http.server.BaseHTTPRequestHandler):
    status_to_return = 200
    response_body = b'{"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}'

    def do_GET(self) -> None:
        self.send_response(self.status_to_return)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.response_body)

    def log_message(self, format: str, *args: object) -> None:
        pass  # Quiet down server logs in tests


@pytest.fixture
def mock_http_server() -> Generator[tuple[str, int, type[_MockHandler]], None, None]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _MockHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}", port, _MockHandler
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# ModelBinding Tests
# ---------------------------------------------------------------------------

def test_model_binding_immutability() -> None:
    binding = ModelBinding(
        provider_id="openai",
        model_id="gpt-4o",
        provider_model_id="gpt-4o",
        display_name="GPT-4o",
        route=ROUTE_DIRECT_API,
        effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
        endpoint_contract=TRANSPORT_GENERIC,
        endpoint="https://api.openai.com/v1",
        auth_mode=AUTH_BEARER,
        config_fingerprint=None,
        tool_version="1.0",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.provider_id = "anthropic"  # type: ignore[misc]

    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.model_id = "claude-3-5-sonnet"  # type: ignore[misc]


def test_model_binding_empty_contract_or_route_fails_closed() -> None:
    with pytest.raises(ModelGatewayError, match="endpoint_contract cannot be empty"):
        ModelBinding(
            provider_id="openai",
            model_id="gpt-4o",
            provider_model_id="gpt-4o",
            display_name="GPT-4o",
            route=ROUTE_DIRECT_API,
            effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
            endpoint_contract="",
            endpoint="https://api.openai.com/v1",
            auth_mode=AUTH_BEARER,
            config_fingerprint=None,
            tool_version="1.0",
        )

    with pytest.raises(ModelGatewayError, match="route cannot be empty"):
        ModelBinding(
            provider_id="openai",
            model_id="gpt-4o",
            provider_model_id="gpt-4o",
            display_name="GPT-4o",
            route="",
            effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
            endpoint_contract=TRANSPORT_GENERIC,
            endpoint="https://api.openai.com/v1",
            auth_mode=AUTH_BEARER,
            config_fingerprint=None,
            tool_version="1.0",
        )


def test_model_binding_credential_scrubbing() -> None:
    with pytest.raises(ModelGatewayError, match="contains a credential-shaped value"):
        ModelBinding(
            provider_id="openai",
            model_id="gpt-4o",
            provider_model_id="gpt-4o",
            display_name="GPT-4o",
            route=ROUTE_DIRECT_API,
            effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
            endpoint_contract=TRANSPORT_GENERIC,
            endpoint="https://api.openai.com/v1?api_key=sk-1234567890abcdef1234567890abcdef",
            auth_mode=AUTH_BEARER,
            config_fingerprint=None,
            tool_version="1.0",
        )

    with pytest.raises(ModelGatewayError, match="contains a credential-shaped value"):
        ModelBinding(
            provider_id="openai",
            model_id="gpt-4o",
            provider_model_id="gpt-4o",
            display_name="GPT-4o",
            route="Bearer sk-1234567890abcdef1234567890abcdef",
            effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
            endpoint_contract=TRANSPORT_GENERIC,
            endpoint="https://api.openai.com/v1",
            auth_mode=AUTH_BEARER,
            config_fingerprint=None,
            tool_version="1.0",
        )


def test_contains_credential_shape_detects_patterns() -> None:
    assert contains_credential_shape("Bearer mysecrettoken1234567890") is True
    assert contains_credential_shape("api_key=secretvalue1234567890") is True
    assert contains_credential_shape("token=mytoken12345") is True
    assert contains_credential_shape("authorization: Bearer secret") is True
    assert contains_credential_shape("https://api.openai.com/v1") is False
    assert contains_credential_shape("openai_chat") is False
    assert contains_credential_shape("generic") is False
    assert contains_credential_shape("generic") is False


def test_model_binding_repr_is_secret_free() -> None:
    binding = ModelBinding(
        provider_id="openai",
        model_id="gpt-4o",
        provider_model_id="gpt-4o",
        display_name="GPT-4o",
        route=ROUTE_DIRECT_API,
        effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
        endpoint_contract=TRANSPORT_GENERIC,
        endpoint="https://api.openai.com/v1",
        auth_mode=AUTH_BEARER,
        config_fingerprint=None,
        tool_version="1.0",
    )
    r = repr(binding)
    assert "provider='openai'" in r
    assert "model='gpt-4o'" in r
    assert "sk-" not in r


def test_model_binding_deterministic_fingerprint() -> None:
    b1 = ModelBinding(
        provider_id="openai",
        model_id="gpt-4o",
        provider_model_id="gpt-4o",
        display_name="GPT-4o",
        route=ROUTE_DIRECT_API,
        effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
        endpoint_contract=TRANSPORT_GENERIC,
        endpoint="https://api.openai.com/v1",
        auth_mode=AUTH_BEARER,
        config_fingerprint=None,
        tool_version="1.0",
    )
    b2 = ModelBinding(
        provider_id="openai",
        model_id="gpt-4o",
        provider_model_id="gpt-4o",
        display_name="GPT-4o",
        route=ROUTE_DIRECT_API,
        effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
        endpoint_contract=TRANSPORT_GENERIC,
        endpoint="https://api.openai.com/v1",
        auth_mode=AUTH_BEARER,
        config_fingerprint=None,
        tool_version="1.0",
    )
    assert b1.fingerprint() == b2.fingerprint()
    assert b1.binding_id == b2.binding_id

    b3 = ModelBinding(
        provider_id="openai",
        model_id="gpt-4o-mini",
        provider_model_id="gpt-4o-mini",
        display_name="GPT-4o Mini",
        route=ROUTE_DIRECT_API,
        effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
        endpoint_contract=TRANSPORT_GENERIC,
        endpoint="https://api.openai.com/v1",
        auth_mode=AUTH_BEARER,
        config_fingerprint=None,
        tool_version="1.0",
    )
    assert b1.fingerprint() != b3.fingerprint()


def test_model_binding_serialization_roundtrip() -> None:
    original = ModelBinding(
        provider_id="openai",
        model_id="gpt-4o",
        provider_model_id="gpt-4o",
        display_name="GPT-4o",
        route=ROUTE_DIRECT_API,
        effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
        endpoint_contract=TRANSPORT_GENERIC,
        endpoint="https://api.openai.com/v1",
        auth_mode=AUTH_BEARER,
        config_fingerprint="fp123",
        tool_version="1.0",
    )
    mapping = original.to_mapping()
    restored = ModelBinding.from_mapping(mapping)
    assert restored == original
    assert restored.fingerprint() == original.fingerprint()


def test_model_binding_model_configured_payload() -> None:
    binding = ModelBinding(
        provider_id="openai",
        model_id="gpt-4o",
        provider_model_id="gpt-4o",
        display_name="GPT-4o",
        route=ROUTE_DIRECT_API,
        effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
        endpoint_contract=TRANSPORT_GENERIC,
        endpoint="https://api.openai.com/v1",
        auth_mode=AUTH_BEARER,
        config_fingerprint="fp_configured",
        tool_version="1.0",
    )
    payload = binding.model_configured_payload()
    assert payload["provider"] == "openai"
    assert payload["profile_id"] == "gpt-4o"
    assert payload["api_protocol"] == PROTOCOL_CHAT_COMPLETIONS
    assert payload["route"] == ROUTE_DIRECT_API


# ---------------------------------------------------------------------------
# ModelGateway Resolution & Preflight Tests
# ---------------------------------------------------------------------------

def test_model_gateway_resolve_configured_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="Custom OpenAI",
        base_url="https://api.custom.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="custom_openai",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_GENERIC,
    )

    binding = gateway.resolve("custom_openai", "gpt-4o")
    assert binding.provider == "custom_openai"
    assert binding.model == "gpt-4o"
    assert binding.base_url == "https://api.custom.com/v1"
    assert binding.effective_protocol == PROTOCOL_CHAT_COMPLETIONS
    assert binding.transport_profile == TRANSPORT_GENERIC


def test_model_gateway_resolve_unconfigured_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    binding = gateway.resolve("unconfigured_mock", "mock-model-v1")
    assert binding.provider == "unconfigured_mock"
    assert binding.model == "mock-model-v1"
    assert binding.effective_protocol is None
    assert binding.transport_profile == TRANSPORT_GENERIC


def test_model_gateway_static_preflight_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    # 1. Unknown provider
    unknown_binding = ModelBinding(
        provider_id="does_not_exist",
        model_id="m1",
        provider_model_id="m1",
        display_name="m1",
        route=ROUTE_DIRECT_API,
        effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
        endpoint_contract=TRANSPORT_GENERIC,
        endpoint="http://127.0.0.1:8000",
        auth_mode=AUTH_NONE,
        config_fingerprint=None,
        tool_version="1.0",
    )
    pf_unknown = gateway.static_preflight(unknown_binding)
    assert pf_unknown.is_runnable is False
    assert pf_unknown.blocker_reason is not None
    assert "not configured" in pf_unknown.blocker_reason.lower()

    # 2. Quarantined provider
    add_provider_config(
        name="Quarantined Provider",
        base_url="http://127.0.0.1:8001",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="quarantined_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )
    quarantine_provider("quarantined_p")
    binding_q = gateway.resolve("quarantined_p", "m1")
    pf_q = gateway.static_preflight(binding_q)
    assert pf_q.is_runnable is False
    assert pf_q.blocker_reason is not None
    assert "quarantine" in pf_q.blocker_reason.lower() or "recovery" in pf_q.blocker_reason.lower()

    # 3. Disabled provider
    add_provider_config(
        name="Disabled Provider",
        base_url="http://127.0.0.1:8002",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="disabled_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )
    update_provider_config("disabled_p", enabled=False)
    binding_d = gateway.resolve("disabled_p", "m1")
    pf_d = gateway.static_preflight(binding_d)
    assert pf_d.is_runnable is False
    assert pf_d.blocker_reason is not None
    assert "disabled" in pf_d.blocker_reason.lower()

    # 4. Provider needing credentials
    add_provider_config(
        name="Auth Needed Provider",
        base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="auth_needed_p",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_GENERIC,
    )
    binding_auth = gateway.resolve("auth_needed_p", "m1")
    pf_auth = gateway.static_preflight(binding_auth)
    assert pf_auth.is_runnable is False
    assert pf_auth.blocker_reason is not None
    assert "credential" in pf_auth.blocker_reason.lower() or "auth" in pf_auth.blocker_reason.lower()

    # 5. Loopback provider with auth_mode="none" is ready
    add_provider_config(
        name="Loopback Provider",
        base_url="http://127.0.0.1:11434",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="loopback_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )
    binding_lb = gateway.resolve("loopback_p", "llama3.2")
    pf_lb = gateway.static_preflight(binding_lb)
    assert pf_lb.is_runnable is True
    assert pf_lb.blocker_reason is None


# ---------------------------------------------------------------------------
# ModelGateway Reachability Probing Tests
# ---------------------------------------------------------------------------

def test_model_gateway_probe_reachability_success(
    tmp_path: Path,
    mock_http_server: tuple[str, int, type[_MockHandler]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    base_url, port, handler_cls = mock_http_server
    handler_cls.status_to_return = 200

    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="Test Live",
        base_url=base_url,
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="test_live_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    res = gateway.probe_reachability("test_live_p")
    assert res["connected"] is True
    assert res["timestamp"] is not None

    status = gateway.get_provider_status("test_live_p")
    assert status.live_verified is True
    assert status.live_verified_at_utc == res["timestamp"]
    assert status.connected is True


def test_model_gateway_probe_reachability_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    # Port 59999 has no listener
    add_provider_config(
        name="Dead Provider",
        base_url="http://127.0.0.1:59999",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="dead_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    res = gateway.probe_reachability("dead_p")
    assert res["connected"] is False
    assert res["error"] is not None

    status = gateway.get_provider_status("dead_p")
    assert status.live_verified is False
    assert status.connected is False


# ---------------------------------------------------------------------------
# ModelGateway Catalog Refresh Tests
# ---------------------------------------------------------------------------

def test_model_gateway_refresh_catalog(
    tmp_path: Path,
    mock_http_server: tuple[str, int, type[_MockHandler]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    base_url, port, handler_cls = mock_http_server
    handler_cls.status_to_return = 200
    handler_cls.response_body = json.dumps({
        "data": [{"id": "model-alpha"}, {"id": "model-beta"}]
    }).encode("utf-8")

    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="Catalog Provider",
        base_url=base_url,
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="catalog_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    snapshot = gateway.refresh_catalog("catalog_p")
    model_ids = [m.model_id for m in snapshot.models]
    assert "model-alpha" in model_ids
    assert "model-beta" in model_ids
