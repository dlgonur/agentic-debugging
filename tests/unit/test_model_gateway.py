"""Unit tests for ModelGateway and ModelBinding (V2-03).

Verifies ModelBinding immutability, credential scrubbing, deterministic
fingerprinting, error taxonomy, static preflight, reachability probing,
and catalog refresh.
"""

from __future__ import annotations

import dataclasses
import http.server
import json
import re
import threading
from pathlib import Path
from typing import Any, Generator
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
    StaleModelBindingError,
    ROUTE_DIRECT_API,
    ROUTE_CONFIGURED_PROFILE,
    ROUTE_QUALIFIED_LADDER,
    ROUTE_LEGACY_CLI,
    ROUTE_OFFLINE,
    contains_credential_shape,
    provider_runtime_identity,
    is_loopback_url,
)
from agentic_debugger.application.provider_connections import (
    ProviderConfig,
    add_provider_config,
    get_provider_config,
    update_provider_config,
    quarantine_provider,
    PROTOCOL_CHAT_COMPLETIONS,
    PROTOCOL_RESPONSES,
    AUTH_BEARER,
    AUTH_NONE,
    TRANSPORT_GENERIC,
    TRANSPORT_OLLAMA_CLOUD,
    TRANSPORT_COMMANDCODE_GOAT,
    TRANSPORT_OPENCODE_GO,
    ProviderConnectionError,
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
        provider_runtime_identity="0" * 64,
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
        provider_runtime_identity="0" * 64,
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
        provider_runtime_identity="0" * 64,
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
        provider_runtime_identity="0" * 64,
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
        provider_runtime_identity="0" * 64,
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
        provider_runtime_identity="0" * 64,
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
        provider_runtime_identity="0" * 64,
    )
    payload = binding.model_configured_payload()
    assert payload["provider"] == "openai"
    assert payload["profile_id"] == "gpt-4o"
    assert payload["api_protocol"] == PROTOCOL_CHAT_COMPLETIONS
    assert payload["route"] == ROUTE_DIRECT_API
    assert payload["provider_runtime_identity"] == "0" * 64


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


def test_model_gateway_resolve_unconfigured_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    with pytest.raises(ProviderConfigurationError, match="not configured"):
        gateway.resolve("unconfigured_mock", "mock-model-v1")


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
        provider_runtime_identity="0" * 64,
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


# ---------------------------------------------------------------------------
# Regression Tests: Review Findings 1, 2, 3, 5
# ---------------------------------------------------------------------------


def test_binding_stability_provider_config_mutation_raises_stale_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1: create_transport must fail closed with StaleModelBindingError if config drifted."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="Stable Provider",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="stable_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    binding = gateway.resolve("stable_p", "m1")
    assert binding.route == ROUTE_DIRECT_API
    assert binding.endpoint == "http://127.0.0.1:8000"

    # Transport creation succeeds initially with corroboration
    transport = gateway.create_transport(binding)
    assert transport is not None

    # Mutate provider configuration: change base_url
    update_provider_config("stable_p", base_url="http://127.0.0.1:8001")

    with pytest.raises(StaleModelBindingError, match="drifted"):
        gateway.create_transport(binding)

    # Mutate base_url back, but change auth_mode
    update_provider_config("stable_p", base_url="http://127.0.0.1:8000", auth_mode=AUTH_BEARER)

    with pytest.raises(StaleModelBindingError, match="drifted"):
        gateway.create_transport(binding)


def test_binding_stability_command_profile_mutation_raises_stale_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1: Profile-backed bindings must corroborate fingerprint against store before execution."""
    from types import SimpleNamespace

    gateway = ModelGateway(config_root=tmp_path)
    binding = ModelBinding(
        provider_id=None,
        model_id="prof-1",
        provider_model_id=None,
        display_name="Prof 1",
        route=ROUTE_CONFIGURED_PROFILE,
        effective_protocol=None,
        endpoint_contract=TRANSPORT_GENERIC,
        endpoint=None,
        auth_mode=None,
        config_fingerprint="original_fingerprint_hash",
        tool_version="1.0",
    )

    # Store returns a profile with drifted fingerprint
    drifted_profile = SimpleNamespace(
        profile_id="prof-1",
        configuration_fingerprint="mutated_fingerprint_hash",
        tool_version="1.0",
    )
    monkeypatch.setattr(
        "agentic_debugger.application.command_config.CommandModelConfigStore.get",
        lambda self, profile_id: drifted_profile,
    )

    with pytest.raises(StaleModelBindingError, match="drifted"):
        gateway.create_transport(binding)


def test_resolve_incompatible_model_protocol_raises_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2: Incompatible model/protocol fails closed with IncompatibleModelError."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="Contract Provider",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="contract_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.effective_model_protocol",
        lambda kind, model: (_ for _ in ()).throw(
            ProviderConnectionError("incompatible protocol format")
        ),
    )
    monkeypatch.setattr(
        "agentic_debugger.application.model_gateway.effective_model_protocol",
        lambda kind, model: (_ for _ in ()).throw(
            ProviderConnectionError("incompatible protocol format")
        ),
    )

    with pytest.raises(IncompatibleModelError, match="incompatible"):
        gateway.resolve("contract_p", "incompatible_model")


def test_resolve_missing_credential_safe_static_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2: Missing credentials on known provider produce safe static binding, preflight reports needs_auth."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="Needs Auth Provider",
        base_url="https://api.openai.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="needs_auth_p",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_GENERIC,
    )

    binding = gateway.resolve("needs_auth_p", "gpt-4o")
    assert binding.provider_id == "needs_auth_p"
    assert binding.auth_mode == AUTH_BEARER
    assert binding.endpoint == "https://api.openai.com/v1"

    preflight = gateway.static_preflight(binding)
    assert preflight.is_runnable is False
    assert preflight.blocker_reason is not None
    assert "credential" in preflight.blocker_reason.lower() or "auth" in preflight.blocker_reason.lower()


def test_probe_reachability_runtime_identity_invalidation(
    tmp_path: Path,
    mock_http_server: tuple[str, int, type[_MockHandler]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 3: Probe results are bound to runtime identity; config mutation invalidates without cache flush."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    base_url, port, handler_cls = mock_http_server
    handler_cls.status_to_return = 200

    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="Probed Provider",
        base_url=base_url,
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="probed_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    probe_res = gateway.probe_reachability("probed_p")
    assert probe_res.get("connected") is True
    status = gateway.get_provider_status("probed_p")
    assert status.live_verified is True

    # Mutate base_url to a different endpoint
    update_provider_config("probed_p", base_url="http://127.0.0.1:9999")

    # Status must now report live_verified=False because runtime identity changed
    status_mutated = gateway.get_provider_status("probed_p")
    assert status_mutated.live_verified is False


def test_runtime_success_identity_bound_to_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3: Runtime success requires model.configured endpoint matching target config base_url."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="Historical Provider",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="hist_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    sessions_dir = tmp_path / "sessions" / "s1"
    sessions_dir.mkdir(parents=True)
    events = [
        {
            "kind": "model.configured",
            "timestamp": "2026-09-05T10:00:00Z",
            "provider": "hist_p",
            "endpoint": "http://127.0.0.1:8000",
            "auth_mode": "none",
            "endpoint_contract": "generic",
            "api_format": "chat_completions",
            "model": "m1",
        },
        {
            "kind": "llm.request",
            "timestamp": "2026-09-05T10:01:00Z",
            "provider": "hist_p",
            "status": "success",
        },
    ]
    with open(sessions_dir / "journal.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    # Matched endpoint returns timestamp
    last_succ = gateway.inspect_last_runtime_success("hist_p", sessions_root=tmp_path / "sessions")
    assert last_succ == "2026-09-05T10:01:00Z"

    # Mutate provider config to a new base_url
    update_provider_config("hist_p", base_url="http://127.0.0.1:9000")

    # Inspecting runtime success for the mutated provider returns None because endpoint changed
    last_succ_mutated = gateway.inspect_last_runtime_success("hist_p", sessions_root=tmp_path / "sessions")
    assert last_succ_mutated is None


def test_runtime_success_ignores_unbound_legacy_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3: Historical journals without endpoint payload must not be bound to arbitrary endpoints."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="Legacy Provider",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="legacy_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    sessions_dir = tmp_path / "sessions" / "s_legacy"
    sessions_dir.mkdir(parents=True)
    events = [
        {
            "kind": "model.configured",
            "timestamp": "2026-09-05T10:00:00Z",
            "provider": "legacy_p",
            # Note: No 'endpoint' field present
            "model": "m1",
        },
        {
            "kind": "llm.request",
            "timestamp": "2026-09-05T10:01:00Z",
            "provider": "legacy_p",
            "status": "success",
        },
    ]
    with open(sessions_dir / "journal.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    last_succ = gateway.inspect_last_runtime_success("legacy_p", sessions_root=tmp_path / "sessions")
    assert last_succ is None


def test_ollama_route_dispatch_distinction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5: Qualified Ollama ladder model produces ROUTE_QUALIFIED_LADDER; configured produces ROUTE_DIRECT_API."""
    from types import SimpleNamespace

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    # 1. Qualified Ollama ladder
    ladder_profile = SimpleNamespace(
        alias="qwen2.5-coder:7b",
        display_name="Qwen 2.5 Coder 7B",
        transport_config_fingerprint="fp_qwen_7b",
    )
    monkeypatch.setattr(
        "agentic_debugger.application.level32.level32_model_profiles",
        lambda: [ladder_profile],
    )
    monkeypatch.setattr(
        "scripts.ollama_cloud_command_adapter.build_ollama_live_config",
        lambda alias, logical_call_ceiling=64: SimpleNamespace(tool_version="1.0"),
    )

    ladder_binding = gateway.resolve(
        None, "qwen2.5-coder:7b", is_ollama=True, ollama_alias="qwen2.5-coder:7b"
    )
    assert ladder_binding.route == ROUTE_QUALIFIED_LADDER
    assert ladder_binding.endpoint_contract == TRANSPORT_OLLAMA_CLOUD

    # 2. Configured Ollama provider
    add_provider_config(
        name="Local Ollama",
        base_url="http://127.0.0.1:11434",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="ollama",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_OLLAMA_CLOUD,
    )

    cfg_binding = gateway.resolve("ollama", "qwen2.5-coder:7b", is_ollama=False)
    assert cfg_binding.route == ROUTE_DIRECT_API
    assert cfg_binding.endpoint == "http://127.0.0.1:11434"


def test_configured_command_profile_missing_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 1: Configured command profile resolution fails closed when missing/unreadable."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    from agentic_debugger.application.command_config import (
        CommandModelConfigStore,
        COMMAND_CONFIG_SCHEMA_VERSION,
    )
    store = CommandModelConfigStore(tmp_path)
    gateway = ModelGateway(config_root=tmp_path)

    # Missing profile must fail closed immediately
    with pytest.raises(ProviderConfigurationError) as excinfo:
        gateway.resolve("configured", "nonexistent-profile", profile_id="nonexistent-profile")
    assert "not found" in str(excinfo.value).lower()

    # Create profile afterwards on disk in store
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    profile_data = {
        "schema_version": COMMAND_CONFIG_SCHEMA_VERSION,
        "profiles": [
            {
                "profile_id": "now-exists",
                "display_name": "Now Exists",
                "executable": "python",
                "argv": ["-c", "pass"],
                "tool_version": "live-v1",
                "protocol_version": "1.3",
            }
        ],
    }
    (config_dir / "command-models.json").write_text(json.dumps(profile_data), encoding="utf-8")

    profile = store.get("now-exists")

    # Now resolving succeeds and captures real fingerprint and tool_version
    binding = gateway.resolve("configured", "now-exists", profile_id="now-exists")
    assert binding.route == ROUTE_CONFIGURED_PROFILE
    assert binding.model_id == "now-exists"
    assert binding.config_fingerprint == profile.configuration_fingerprint
    assert binding.tool_version == "live-v1"


def test_command_profile_stale_binding_on_fingerprint_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 1 & 4: Profile mutation invalidates old binding at preflight and transport."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    from agentic_debugger.application.command_config import COMMAND_CONFIG_SCHEMA_VERSION

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    profile_data_v1 = {
        "schema_version": COMMAND_CONFIG_SCHEMA_VERSION,
        "profiles": [
            {
                "profile_id": "profile-drift",
                "display_name": "Drift Test",
                "executable": "python",
                "argv": ["-c", "print('v1')"],
                "tool_version": "v1",
                "protocol_version": "1.3",
            }
        ],
    }
    (config_dir / "command-models.json").write_text(json.dumps(profile_data_v1), encoding="utf-8")

    binding = gateway.resolve("configured", "profile-drift", profile_id="profile-drift")
    pf = gateway.static_preflight(binding)
    assert pf.is_runnable is True

    # Mutate profile command on disk (changes fingerprint)
    profile_data_v2 = {
        "schema_version": COMMAND_CONFIG_SCHEMA_VERSION,
        "profiles": [
            {
                "profile_id": "profile-drift",
                "display_name": "Drift Test",
                "executable": "python",
                "argv": ["-c", "print('v2')"],
                "tool_version": "v1",
                "protocol_version": "1.3",
            }
        ],
    }
    (config_dir / "command-models.json").write_text(json.dumps(profile_data_v2), encoding="utf-8")

    # static_preflight detects fingerprint drift
    pf_stale = gateway.static_preflight(binding)
    assert pf_stale.is_runnable is False
    assert "drifted" in str(pf_stale.blocker_reason).lower()

    # create_transport raises StaleModelBindingError
    with pytest.raises(StaleModelBindingError) as excinfo:
        gateway.create_transport(binding)
    assert "drifted" in str(excinfo.value).lower()


def test_narrow_provider_registry_error_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 2: Structured facts govern fallback: missing credential or quarantine falls back to static binding; unexpected failure fails closed."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    from agentic_debugger.application import model_providers
    from agentic_debugger.application.model_providers import ProviderRegistryError
    from agentic_debugger.application.provider_connections import quarantine_provider

    gateway = ModelGateway(config_root=tmp_path)

    # Case A: Missing credential -> returns static binding
    add_provider_config(
        name="Narrow Missing Cred",
        base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="narrow_no_cred",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_GENERIC,
    )
    binding_cred = gateway.resolve("narrow_no_cred", "m1")
    assert binding_cred.provider_id == "narrow_no_cred"
    assert binding_cred.config_fingerprint is None

    # Case B: Quarantined provider -> returns static binding
    add_provider_config(
        name="Narrow Quarantined",
        base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="narrow_quarantine",
        auth_mode=AUTH_BEARER,
        api_key="test-bearer-key-123",
        transport_profile=TRANSPORT_GENERIC,
    )
    quarantine_provider("narrow_quarantine")
    binding_rec = gateway.resolve("narrow_quarantine", "m1")
    assert binding_rec.provider_id == "narrow_quarantine"
    assert binding_rec.config_fingerprint is None

    # Case C: Unexpected failure with valid credentials -> fails closed with ProviderConfigurationError
    add_provider_config(
        name="Narrow Unexpected",
        base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="narrow_unexpected",
        auth_mode=AUTH_BEARER,
        api_key="test-bearer-key-123",
        transport_profile=TRANSPORT_GENERIC,
    )
    def _raise_structural(*args, **kwargs):
        raise ProviderRegistryError("malformed adapter configuration in registry")

    monkeypatch.setattr(model_providers, "resolve_provider_live_config", _raise_structural)
    with pytest.raises(ProviderConfigurationError) as excinfo:
        gateway.resolve("narrow_unexpected", "m1")
    assert "live configuration resolution failed" in str(excinfo.value)


def test_strict_model_binding_semantic_invariants() -> None:
    """Requirement 3: Strict route/provider invariants in constructor and from_mapping."""
    from agentic_debugger.application.model_gateway import ROUTE_OFFLINE, ROUTE_LEGACY_CLI

    # 1. ROUTE_CONFIGURED_PROFILE cannot have arbitrary provider_id
    with pytest.raises(ModelGatewayError) as excinfo:
        ModelBinding(
            provider_id="arbitrary_provider",
            model_id="prof1",
            provider_model_id="prof1",
            display_name="prof1",
            route=ROUTE_CONFIGURED_PROFILE,
            effective_protocol="live_command",
            endpoint_contract="command_profile",
            endpoint=None,
            auth_mode=None,
            config_fingerprint="fp",
            tool_version="1.0",
        )
    assert "configured_profile requires provider_id" in str(excinfo.value)

    # 2. ROUTE_CONFIGURED_PROFILE cannot have endpoint URL
    with pytest.raises(ModelGatewayError) as excinfo:
        ModelBinding(
            provider_id="configured",
            model_id="prof1",
            provider_model_id="prof1",
            display_name="prof1",
            route=ROUTE_CONFIGURED_PROFILE,
            effective_protocol="live_command",
            endpoint_contract="command_profile",
            endpoint="http://127.0.0.1:8000",
            auth_mode=None,
            config_fingerprint="fp",
            tool_version="1.0",
        )
    assert "endpoint URL" in str(excinfo.value)

    # 3. ROUTE_QUALIFIED_LADDER requires provider_id="ollama" and endpoint_contract="ollama_cloud"
    with pytest.raises(ModelGatewayError) as excinfo:
        ModelBinding(
            provider_id="openai",
            model_id="m1",
            provider_model_id="m1",
            display_name="m1",
            route=ROUTE_QUALIFIED_LADDER,
            effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
            endpoint_contract=TRANSPORT_OLLAMA_CLOUD,
            endpoint="http://127.0.0.1:11434",
            auth_mode=AUTH_NONE,
            config_fingerprint=None,
            tool_version="1.0",
        )
    assert "qualified_ladder requires provider_id" in str(excinfo.value)

    # 4. ROUTE_DIRECT_API cannot have provider_id="configured" or None
    with pytest.raises(ModelGatewayError) as excinfo:
        ModelBinding(
            provider_id="configured",
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
    assert "explicit provider identity" in str(excinfo.value)

    # 5. ROUTE_OFFLINE cannot carry provider execution identity or endpoint
    with pytest.raises(ModelGatewayError) as excinfo:
        ModelBinding(
            provider_id="real_provider",
            model_id=None,
            provider_model_id=None,
            display_name="Offline",
            route=ROUTE_OFFLINE,
            effective_protocol=None,
            endpoint_contract="offline",
            endpoint=None,
            auth_mode=None,
            config_fingerprint=None,
            tool_version="1.0",
        )
    assert "offline cannot carry provider execution identity" in str(excinfo.value)

    # 6. from_mapping propagates the same fail-closed semantics
    bad_mapping = {
        "provider_id": "configured",
        "model_id": "m1",
        "provider_model_id": "m1",
        "display_name": "m1",
        "route": ROUTE_DIRECT_API,
        "effective_protocol": PROTOCOL_CHAT_COMPLETIONS,
        "endpoint_contract": TRANSPORT_GENERIC,
        "endpoint": "http://127.0.0.1:8000",
        "auth_mode": "none",
        "config_fingerprint": None,
        "tool_version": "1.0",
    }
    with pytest.raises(ModelGatewayError):
        ModelBinding.from_mapping(bad_mapping)


def test_static_preflight_configuration_drift_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 4: static_preflight detects drift in contract, endpoint, auth_mode, protocol."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Preflight Drift Provider",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="pf_drift_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    binding = gateway.resolve("pf_drift_p", "m1")
    pf = gateway.static_preflight(binding)
    assert pf.is_runnable is True

    # 1. Endpoint drift
    update_provider_config("pf_drift_p", base_url="http://127.0.0.1:9999")
    pf_ep = gateway.static_preflight(binding)
    assert pf_ep.is_runnable is False
    assert "endpoint drifted" in str(pf_ep.blocker_reason).lower()

    # Reset endpoint, mutate auth_mode
    update_provider_config("pf_drift_p", base_url="http://127.0.0.1:8000", auth_mode=AUTH_BEARER)
    pf_auth = gateway.static_preflight(binding)
    assert pf_auth.is_runnable is False
    assert "auth mode drifted" in str(pf_auth.blocker_reason).lower()

    # Reset auth_mode, mutate transport_profile (endpoint contract)
    update_provider_config("pf_drift_p", auth_mode=AUTH_NONE, transport_profile=TRANSPORT_OLLAMA_CLOUD)
    pf_contract = gateway.static_preflight(binding)
    assert pf_contract.is_runnable is False
    assert "endpoint contract drifted" in str(pf_contract.blocker_reason).lower()


def test_runtime_success_invalidated_by_contract_or_format_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 5: Runtime success invalidated by contract or api_format drift despite identical URL/auth."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Contract Drift Provider",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="contract_drift_p",
        auth_mode=AUTH_BEARER,
        api_key="test-bearer-key-123",
        transport_profile=TRANSPORT_GENERIC,
    )

    sessions_dir = tmp_path / "sessions" / "s_drift"
    sessions_dir.mkdir(parents=True)
    cfg = get_provider_config("contract_drift_p")
    rt_id = provider_runtime_identity(cfg)
    events = [
        {
            "kind": "model.configured",
            "timestamp": "2026-09-05T10:00:00Z",
            "provider": "contract_drift_p",
            "endpoint": "http://127.0.0.1:8000",
            "auth_mode": "bearer",
            "endpoint_contract": TRANSPORT_GENERIC,
            "api_format": PROTOCOL_CHAT_COMPLETIONS,
            "model": "m1",
            "provider_runtime_identity": rt_id,
        },
        {
            "kind": "llm.request",
            "timestamp": "2026-09-05T10:01:00Z",
            "provider": "contract_drift_p",
            "status": "success",
        },
    ]
    with open(sessions_dir / "journal.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    # Authoritative match succeeds
    last_succ = gateway.inspect_last_runtime_success("contract_drift_p", sessions_root=tmp_path / "sessions")
    assert last_succ == "2026-09-05T10:01:00Z"

    # Drift contract (transport_profile) while keeping endpoint and auth identical
    update_provider_config("contract_drift_p", transport_profile=TRANSPORT_OLLAMA_CLOUD)
    assert gateway.inspect_last_runtime_success("contract_drift_p", sessions_root=tmp_path / "sessions") is None

    # Reset transport_profile, drift api_format
    update_provider_config("contract_drift_p", transport_profile=TRANSPORT_GENERIC, api_format=PROTOCOL_RESPONSES)
    assert gateway.inspect_last_runtime_success("contract_drift_p", sessions_root=tmp_path / "sessions") is None

    # Reset api_format, drift base_url
    update_provider_config("contract_drift_p", api_format=PROTOCOL_CHAT_COMPLETIONS, base_url="http://127.0.0.1:8001", api_key="valid-key")
    assert gateway.inspect_last_runtime_success("contract_drift_p", sessions_root=tmp_path / "sessions") is None

    # Reset base_url, drift auth_mode
    update_provider_config("contract_drift_p", base_url="http://127.0.0.1:8000", auth_mode=AUTH_NONE)
    assert gateway.inspect_last_runtime_success("contract_drift_p", sessions_root=tmp_path / "sessions") is None


def test_is_known_provider_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement 10: ModelGateway provides is_known_provider authority for runtime."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    assert not ModelGateway.is_known_provider(None)
    assert not ModelGateway.is_known_provider("")
    assert not ModelGateway.is_known_provider("unknown_provider_xyz")

    from agentic_debugger.application.provider_connections import add_provider_config
    add_provider_config(
        name="Known Test",
        base_url="http://127.0.0.1:8000/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        auth_mode="none",
        provider_id="known_test_p",
    )
    assert ModelGateway.is_known_provider("known_test_p")


def test_historical_profile_preserves_legacy_cli_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 1: ModelGateway delegates to provider core and preserves ROUTE_LEGACY_CLI for historical profiles."""
    from unittest.mock import patch
    from agentic_debugger.application.model_gateway import ROUTE_LEGACY_CLI
    from agentic_debugger.application.provider_connections import TRANSPORT_COMMANDCODE_GOAT, TRANSPORT_GENERIC
    from agentic_debugger.evaluation.live import LiveModelConfig

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    # 1. Historical profile commandcode with no credentials falls back to ROUTE_LEGACY_CLI when CLI is available
    add_provider_config(
        name="CommandCode GOAT",
        base_url="http://127.0.0.1:57788",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="cc_legacy_test",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_COMMANDCODE_GOAT,
    )

    with patch(
        "agentic_debugger.application.model_providers._legacy_for_config",
        return_value=(True, None),
    ), patch(
        "scripts.commandcode_goat_adapter.build_commandcode_live_config",
        return_value=LiveModelConfig("goat-default", ("cmdc",), 30.0, "1.0"),
    ):
        binding = gateway.resolve("cc_legacy_test", "goat-default")
        assert binding.route == ROUTE_LEGACY_CLI
        assert binding.endpoint_contract == TRANSPORT_COMMANDCODE_GOAT

        # static_preflight verifies legacy route is runnable
        pf = gateway.static_preflight(binding)
        assert pf.route == ROUTE_LEGACY_CLI
        assert pf.is_runnable is True

        # static_preflight with provider_id string also yields ROUTE_LEGACY_CLI
        pf_str = gateway.static_preflight("cc_legacy_test", "goat-default")
        assert pf_str.route == ROUTE_LEGACY_CLI
        assert pf_str.is_runnable is True

    # 2. Generic provider with no credentials NEVER falls back to legacy CLI
    add_provider_config(
        name="Generic Provider",
        base_url="https://api.generic.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="generic_no_cred",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_GENERIC,
    )
    binding_gen = gateway.resolve("generic_no_cred", "gen-model")
    assert binding_gen.route == ROUTE_DIRECT_API
    pf_gen = gateway.static_preflight(binding_gen)
    assert pf_gen.is_runnable is False
    assert "credential" in (pf_gen.blocker_reason or "").lower()


def test_model_specific_protocol_matches_provider_runtime_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 4: Model-specific protocol (e.g. Claude 'messages' on CommandCode 'chat_completions') matches provider runtime identity."""
    from agentic_debugger.application.provider_connections import PROTOCOL_MESSAGES, TRANSPORT_COMMANDCODE_GOAT
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="CommandCode Provider",
        base_url="http://127.0.0.1:57788",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="cc_proto_test",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_COMMANDCODE_GOAT,
    )

    cfg = get_provider_config("cc_proto_test")
    rt_id = provider_runtime_identity(cfg)

    sessions_dir = tmp_path / "sessions" / "s_claude"
    sessions_dir.mkdir(parents=True)
    events = [
        {
            "kind": "model.configured",
            "timestamp": "2026-09-05T10:00:00Z",
            "provider": "cc_proto_test",
            "endpoint": "http://127.0.0.1:57788",
            "auth_mode": "none",
            "endpoint_contract": TRANSPORT_COMMANDCODE_GOAT,
            "api_format": PROTOCOL_MESSAGES,
            "model": "claude-3-7-sonnet",
            "provider_runtime_identity": rt_id,
        },
        {
            "kind": "llm.request",
            "timestamp": "2026-09-05T10:01:00Z",
            "provider": "cc_proto_test",
            "status": "success",
        },
    ]
    with open(sessions_dir / "journal.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    # Provider-level runtime success matches despite model-specific format
    last_succ = gateway.inspect_last_runtime_success("cc_proto_test", sessions_root=tmp_path / "sessions")
    assert last_succ == "2026-09-05T10:01:00Z"


def test_per_event_journal_scanning_resets_provider_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 5: In inspect_last_runtime_success, each model.configured resets session_matches_provider, preventing leakage."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Provider Alpha",
        base_url="http://127.0.0.1:8001",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="prov_alpha",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )
    add_provider_config(
        name="Provider Beta",
        base_url="http://127.0.0.1:8002",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="prov_beta",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    rt_alpha = provider_runtime_identity(get_provider_config("prov_alpha"))
    rt_beta = provider_runtime_identity(get_provider_config("prov_beta"))

    # Session configures Alpha, then configures Beta, then Beta completes a request
    sessions_dir = tmp_path / "sessions" / "s_multi"
    sessions_dir.mkdir(parents=True)
    events = [
        {
            "kind": "model.configured",
            "timestamp": "2026-09-05T10:00:00Z",
            "provider": "prov_alpha",
            "endpoint": "http://127.0.0.1:8001",
            "auth_mode": "none",
            "endpoint_contract": TRANSPORT_GENERIC,
            "api_format": PROTOCOL_CHAT_COMPLETIONS,
            "model": "alpha-1",
            "provider_runtime_identity": rt_alpha,
        },
        {
            "kind": "model.configured",
            "timestamp": "2026-09-05T10:02:00Z",
            "provider": "prov_beta",
            "endpoint": "http://127.0.0.1:8002",
            "auth_mode": "none",
            "endpoint_contract": TRANSPORT_GENERIC,
            "api_format": PROTOCOL_CHAT_COMPLETIONS,
            "model": "beta-1",
            "provider_runtime_identity": rt_beta,
        },
        {
            "kind": "llm.request",
            "timestamp": "2026-09-05T10:03:00Z",
            "provider": "prov_beta",
            "status": "success",
        },
    ]
    with open(sessions_dir / "journal.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    # Alpha must NOT inherit Beta's success!
    assert gateway.inspect_last_runtime_success("prov_alpha", sessions_root=tmp_path / "sessions") is None
    # Beta matches
    assert gateway.inspect_last_runtime_success("prov_beta", sessions_root=tmp_path / "sessions") == "2026-09-05T10:03:00Z"


def test_inspect_last_runtime_success_target_binding_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 4 & 5: When target_binding is supplied, model_binding_fingerprint must strictly match."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Binding Prov",
        base_url="http://127.0.0.1:8001",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="binding_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    binding1 = gateway.resolve("binding_p", "model-a")
    binding2 = gateway.resolve("binding_p", "model-b")

    sessions_dir = tmp_path / "sessions" / "s_bind"
    sessions_dir.mkdir(parents=True)
    events = [
        {
            "kind": "model.configured",
            "timestamp": "2026-09-05T10:00:00Z",
            "provider": "binding_p",
            "endpoint": "http://127.0.0.1:8001",
            "auth_mode": "none",
            "endpoint_contract": TRANSPORT_GENERIC,
            "api_format": PROTOCOL_CHAT_COMPLETIONS,
            "model": "model-a",
            "provider_runtime_identity": binding1.provider_runtime_identity,
            "model_binding_fingerprint": binding1.fingerprint(),
        },
        {
            "kind": "llm.request",
            "timestamp": "2026-09-05T10:01:00Z",
            "provider": "binding_p",
            "status": "success",
        },
    ]
    with open(sessions_dir / "journal.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    # binding1 matches
    assert gateway.inspect_last_runtime_success("binding_p", target_binding=binding1, sessions_root=tmp_path / "sessions") == "2026-09-05T10:01:00Z"
    # binding2 does NOT match (fingerprint mismatch)
    assert gateway.inspect_last_runtime_success("binding_p", target_binding=binding2, sessions_root=tmp_path / "sessions") is None


def test_strict_model_binding_invariants_candidate_16() -> None:
    """Finding 3: ModelBinding strictly validates route invariants on construction and from_mapping."""
    from agentic_debugger.application.model_gateway import ROUTE_LEGACY_CLI
    from agentic_debugger.application.provider_connections import TRANSPORT_OPENCODE_GO, TRANSPORT_GENERIC

    # 1. ROUTE_DIRECT_API with empty endpoint
    with pytest.raises(ModelGatewayError, match="non-empty endpoint URL"):
        ModelBinding(
            provider_id="my_p",
            model_id="m1",
            provider_model_id="m1",
            display_name="m1",
            route=ROUTE_DIRECT_API,
            effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
            endpoint_contract=TRANSPORT_GENERIC,
            endpoint="",
            auth_mode="none",
            config_fingerprint=None,
            tool_version="1.0",
        )

    with pytest.raises(ModelGatewayError, match="non-empty endpoint URL"):
        ModelBinding.from_mapping({
            "provider_id": "my_p",
            "model_id": "m1",
            "route": ROUTE_DIRECT_API,
            "effective_protocol": PROTOCOL_CHAT_COMPLETIONS,
            "endpoint_contract": TRANSPORT_GENERIC,
            "endpoint": "",
            "auth_mode": "none",
        })

    # 2. ROUTE_DIRECT_API with invalid auth_mode
    with pytest.raises(ModelGatewayError, match="auth_mode"):
        ModelBinding(
            provider_id="my_p",
            model_id="m1",
            provider_model_id="m1",
            display_name="m1",
            route=ROUTE_DIRECT_API,
            effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
            endpoint_contract=TRANSPORT_GENERIC,
            endpoint="http://127.0.0.1:8000",
            auth_mode="invalid_auth",
            config_fingerprint=None,
            tool_version="1.0",
        )

    with pytest.raises(ModelGatewayError, match="auth_mode"):
        ModelBinding.from_mapping({
            "provider_id": "my_p",
            "model_id": "m1",
            "route": ROUTE_DIRECT_API,
            "effective_protocol": PROTOCOL_CHAT_COMPLETIONS,
            "endpoint_contract": TRANSPORT_GENERIC,
            "endpoint": "http://127.0.0.1:8000",
            "auth_mode": "invalid_auth",
        })

    # 3. ROUTE_DIRECT_API with unsupported effective_protocol
    with pytest.raises(ModelGatewayError, match="supported effective_protocol"):
        ModelBinding(
            provider_id="my_p",
            model_id="m1",
            provider_model_id="m1",
            display_name="m1",
            route=ROUTE_DIRECT_API,
            effective_protocol="unknown_protocol",
            endpoint_contract=TRANSPORT_GENERIC,
            endpoint="http://127.0.0.1:8000",
            auth_mode="none",
            config_fingerprint=None,
            tool_version="1.0",
        )

    # 4. ROUTE_DIRECT_API with unknown endpoint_contract
    with pytest.raises(ModelGatewayError, match="known endpoint_contract"):
        ModelBinding(
            provider_id="my_p",
            model_id="m1",
            provider_model_id="m1",
            display_name="m1",
            route=ROUTE_DIRECT_API,
            effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
            endpoint_contract="unknown_contract_xyz",
            endpoint="http://127.0.0.1:8000",
            auth_mode="none",
            config_fingerprint=None,
            tool_version="1.0",
        )

    # 5. ROUTE_LEGACY_CLI with generic contract
    with pytest.raises(ModelGatewayError, match="historical endpoint_contract"):
        ModelBinding(
            provider_id="my_p",
            model_id="m1",
            provider_model_id="m1",
            display_name="m1",
            route=ROUTE_LEGACY_CLI,
            effective_protocol=None,
            endpoint_contract=TRANSPORT_GENERIC,
            endpoint=None,
            auth_mode=None,
            config_fingerprint=None,
            tool_version="1.0",
        )

    with pytest.raises(ModelGatewayError, match="historical endpoint_contract"):
        ModelBinding.from_mapping({
            "provider_id": "my_p",
            "model_id": "m1",
            "route": ROUTE_LEGACY_CLI,
            "endpoint_contract": TRANSPORT_GENERIC,
        })

    # 6. ROUTE_LEGACY_CLI with missing model_id
    with pytest.raises(ModelGatewayError, match="non-empty model_id"):
        ModelBinding(
            provider_id="my_p",
            model_id="",
            provider_model_id="",
            display_name="m1",
            route=ROUTE_LEGACY_CLI,
            effective_protocol=None,
            endpoint_contract=TRANSPORT_OPENCODE_GO,
            endpoint=None,
            auth_mode=None,
            config_fingerprint=None,
            tool_version="1.0",
        )


def test_finding_1_a_opencode_historical_profile_unknown_direct_protocol_resolves_legacy_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1A: OpenCode historical profile with unknown direct protocol resolves to ROUTE_LEGACY_CLI when CLI is available."""
    from unittest.mock import patch
    from agentic_debugger.application.model_gateway import ROUTE_LEGACY_CLI
    from agentic_debugger.application.provider_connections import (
        TRANSPORT_OPENCODE_GO,
        PROTOCOL_RESPONSES,
        resolve_model_protocol,
    )
    from agentic_debugger.evaluation.live import LiveModelConfig

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "opencode_empty"))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="OpenCode Historical",
        base_url="https://opencode.ai/zen/go/v1",
        api_format=PROTOCOL_RESPONSES,
        provider_id="oc_finding1a",
        auth_mode=AUTH_BEARER,
        api_key="valid-key",
        transport_profile=TRANSPORT_OPENCODE_GO,
    )

    # Direct protocol resolver returns None for unknown model
    assert resolve_model_protocol("oc_finding1a", "opencode-go/unknown-model-xyz") is None

    with patch(
        "agentic_debugger.application.model_providers._opencode_availability",
        return_value=(True, None),
    ), patch(
        "scripts.opencode_provider_adapter.build_opencode_live_config",
        return_value=LiveModelConfig("opencode-go/unknown-model-xyz", ("opencode",), 30.0, "1.0"),
    ):
        binding = gateway.resolve("oc_finding1a", "opencode-go/unknown-model-xyz")
        assert binding.route == ROUTE_LEGACY_CLI
        assert binding.endpoint_contract == TRANSPORT_OPENCODE_GO
        assert binding.model_id == "opencode-go/unknown-model-xyz"


def test_finding_1_b_direct_credential_absent_legacy_route_available_resolves_legacy_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1B: Direct credential absent on historical contract resolves to ROUTE_LEGACY_CLI when legacy CLI is available."""
    from unittest.mock import patch
    from agentic_debugger.application.model_gateway import ROUTE_LEGACY_CLI
    from agentic_debugger.application.provider_connections import (
        TRANSPORT_OPENCODE_GO,
        PROTOCOL_RESPONSES,
        credential_source_for,
    )
    from agentic_debugger.evaluation.live import LiveModelConfig

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "opencode_empty"))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="OpenCode No Cred",
        base_url="https://opencode.ai/zen/go/v1",
        api_format=PROTOCOL_RESPONSES,
        provider_id="oc_finding1b",
        auth_mode=AUTH_BEARER,
        # No api_key provided!
        transport_profile=TRANSPORT_OPENCODE_GO,
    )

    assert credential_source_for("oc_finding1b") is None

    with patch(
        "agentic_debugger.application.model_providers._opencode_availability",
        return_value=(True, None),
    ), patch(
        "scripts.opencode_provider_adapter.build_opencode_live_config",
        return_value=LiveModelConfig("opencode-go/deepseek-v4-flash", ("opencode",), 30.0, "1.0"),
    ):
        binding = gateway.resolve("oc_finding1b", "opencode-go/deepseek-v4-flash")
        # Must resolve to ROUTE_LEGACY_CLI, NOT static direct_api needs_auth!
        assert binding.route == ROUTE_LEGACY_CLI
        assert binding.endpoint_contract == TRANSPORT_OPENCODE_GO


def test_finding_1_c_generic_provider_remains_isolated_never_resolves_legacy_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1C: Generic provider with TRANSPORT_GENERIC never selects legacy_cli even if CLIs are available."""
    from unittest.mock import patch
    from agentic_debugger.application.model_gateway import ROUTE_DIRECT_API, ROUTE_LEGACY_CLI
    from agentic_debugger.application.provider_connections import TRANSPORT_GENERIC

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Generic Provider Isolated",
        base_url="https://api.generic.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="gen_finding1c",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_GENERIC,
    )

    with patch(
        "agentic_debugger.application.model_providers._opencode_availability",
        return_value=(True, None),
    ), patch(
        "agentic_debugger.application.model_providers._commandcode_availability",
        return_value=(True, None),
    ):
        binding = gateway.resolve("gen_finding1c", "any-model")
        assert binding.route == ROUTE_DIRECT_API
        assert binding.route != ROUTE_LEGACY_CLI


def test_finding_1_d_static_preflight_reports_route_consistent_with_gateway_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1D: static_preflight(provider_id, model_id) reports route consistent with authoritative binding resolution."""
    from unittest.mock import patch
    from agentic_debugger.application.model_gateway import ROUTE_LEGACY_CLI
    from agentic_debugger.application.provider_connections import TRANSPORT_OPENCODE_GO, PROTOCOL_RESPONSES
    from agentic_debugger.evaluation.live import LiveModelConfig

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "opencode_empty"))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="OpenCode Legacy Preflight",
        base_url="https://opencode.ai/zen/go/v1",
        api_format=PROTOCOL_RESPONSES,
        provider_id="oc_finding1d",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_OPENCODE_GO,
    )

    with patch(
        "agentic_debugger.application.model_providers._opencode_availability",
        return_value=(True, None),
    ), patch(
        "scripts.opencode_provider_adapter.build_opencode_live_config",
        return_value=LiveModelConfig("opencode-go/deepseek-v4-flash", ("opencode",), 30.0, "1.0"),
    ):
        binding = gateway.resolve("oc_finding1d", "opencode-go/deepseek-v4-flash")
        assert binding.route == ROUTE_LEGACY_CLI

        # String preflight must be consistent with binding resolution
        pf = gateway.static_preflight("oc_finding1d", "opencode-go/deepseek-v4-flash")
        assert pf.route == ROUTE_LEGACY_CLI
        assert pf.is_runnable is True
        assert pf.blocker_reason is None


def test_finding_1_e_local_project_product_path_preserves_legacy_route_and_transports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1E & Local Project integration: UI/session preflight -> SessionLaunch -> ModelGateway -> ModelBinding -> create_transport."""
    from unittest.mock import patch
    from agentic_debugger.application.model_gateway import ROUTE_DIRECT_API, ROUTE_LEGACY_CLI, ROUTE_QUALIFIED_LADDER
    from agentic_debugger.application.provider_connections import (
        TRANSPORT_COMMANDCODE_GOAT,
        TRANSPORT_GENERIC,
    )
    from agentic_debugger.application.session_runtime import (
        ProjectRuntimeEnvironmentSpec,
        build_local_project_launch,
    )
    from agentic_debugger.evaluation.live import LiveModelConfig

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    # Case A: direct API route
    add_provider_config(
        name="Direct Prov",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="p_direct",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )
    pf_a = gateway.static_preflight("p_direct", "model-direct")
    assert pf_a.is_runnable is True
    assert pf_a.route == ROUTE_DIRECT_API

    launch_a = build_local_project_launch(
        session_id="s_direct",
        task_id="t_direct",
        policy="pdb-on-uncertainty",
        provider_id="p_direct",
        model_id="model-direct",
        profile_id="model-direct",
        launch_snapshot={"PATH": "/usr/bin"},
        project_spec=ProjectRuntimeEnvironmentSpec(),
        config_root=tmp_path,
    )
    assert launch_a.model_binding.route == ROUTE_DIRECT_API
    transport_a, live_cfg_a = gateway.create_transport(launch_a.model_binding)
    assert transport_a is not None
    assert live_cfg_a is not None

    # Case B: historical legacy CLI route
    add_provider_config(
        name="CommandCode Legacy",
        base_url="http://127.0.0.1:57788",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="p_legacy",
        auth_mode=AUTH_BEARER,  # No creds
        transport_profile=TRANSPORT_COMMANDCODE_GOAT,
    )
    with patch(
        "agentic_debugger.application.model_providers._legacy_for_config",
        return_value=(True, None),
    ), patch(
        "scripts.commandcode_goat_adapter.build_commandcode_live_config",
        return_value=LiveModelConfig("goat-default", ("cmdc",), 30.0, "1.0"),
    ):
        pf_b = gateway.static_preflight("p_legacy", "goat-default")
        assert pf_b.is_runnable is True
        assert pf_b.route == ROUTE_LEGACY_CLI

        launch_b = build_local_project_launch(
            session_id="s_legacy",
            task_id="t_legacy",
            policy="pdb-on-uncertainty",
            provider_id="p_legacy",
            model_id="goat-default",
            profile_id="goat-default",
            launch_snapshot={"PATH": "/usr/bin"},
            project_spec=ProjectRuntimeEnvironmentSpec(),
            config_root=tmp_path,
        )
        assert launch_b.model_binding.route == ROUTE_LEGACY_CLI
        transport_b, live_cfg_b = gateway.create_transport(launch_b.model_binding)
        assert transport_b is not None
        assert live_cfg_b is not None

    # Case C: qualified Ollama ladder
    launch_c = build_local_project_launch(
        session_id="s_ollama",
        task_id="t_ollama",
        policy="pdb-on-uncertainty",
        provider_id=None,
        model_id="qwen3.5:cloud",
        profile_id="ollama-qwen3.5",
        is_ollama=True,
        ollama_alias="qwen3.5:cloud",
        launch_snapshot={"PATH": "/usr/bin"},
        project_spec=ProjectRuntimeEnvironmentSpec(),
        config_root=tmp_path,
    )
    assert launch_c.model_binding.route == ROUTE_QUALIFIED_LADDER
    transport_c, live_cfg_c = gateway.create_transport(launch_c.model_binding)
    assert transport_c is not None
    assert live_cfg_c is not None


# ---------------------------------------------------------------------------
# Candidate 17 Tests: Seal V2-03 registry and history identity
# ---------------------------------------------------------------------------


def test_candidate_17_finding_1_generic_missing_cred_structured_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1A, 1B, 1E: Generic provider with missing credentials builds static direct binding
    without calling resolve_provider_live_config, fails closed on incompatible protocol,
    and never consults legacy CLI.
    """
    from unittest.mock import patch
    from agentic_debugger.application.provider_connections import (
        PROTOCOL_CHAT_COMPLETIONS,
        TRANSPORT_GENERIC,
    )

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Generic Provider 17",
        base_url="https://api.generic17.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="gen_17",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_GENERIC,
    )

    # 1A & 1E: Missing credential + valid config -> builds static direct binding directly.
    # Verify resolve_provider_live_config and _legacy_for_config are NEVER called!
    with patch(
        "agentic_debugger.application.model_providers.resolve_provider_live_config"
    ) as mock_live, patch(
        "agentic_debugger.application.model_providers._legacy_for_config"
    ) as mock_legacy:
        binding = gateway.resolve("gen_17", "gpt-4o")
        assert binding.route == ROUTE_DIRECT_API
        assert binding.provider_id == "gen_17"
        assert binding.model_id == "gpt-4o"
        assert binding.effective_protocol == PROTOCOL_CHAT_COMPLETIONS
        assert binding.config_fingerprint is None
        assert binding.tool_version == "live-command-v1"
        assert binding.provider_runtime_identity is not None
        mock_live.assert_not_called()
        mock_legacy.assert_not_called()

    # 1B: Missing credential + incompatible model protocol -> IncompatibleModelError
    with patch(
        "agentic_debugger.application.model_gateway.effective_model_protocol",
        side_effect=ProviderConnectionError("Unsupported protocol for model"),
    ):
        with pytest.raises(IncompatibleModelError, match="incompatible"):
            gateway.resolve("gen_17", "bad-proto-model")


def test_candidate_17_finding_1_historical_structural_adapter_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1C, 1D: Historical provider with missing direct credentials calls live resolution.
    If legacy is available but constructor raises structural adapter error, it fails closed as ProviderConfigurationError.
    """
    from unittest.mock import patch
    from agentic_debugger.application.model_providers import ProviderRegistryError
    from agentic_debugger.application.provider_connections import (
        TRANSPORT_COMMANDCODE_GOAT,
    )

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="CommandCode 17",
        base_url="http://127.0.0.1:57788",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="cc_17",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_COMMANDCODE_GOAT,
    )

    # 1D: Legacy reports available, but resolve_provider_live_config raises ProviderRegistryError
    # (e.g. structural adapter error). Must fail closed as ProviderConfigurationError!
    with patch(
        "agentic_debugger.application.model_providers._legacy_for_config",
        return_value=(True, None),
    ), patch(
        "agentic_debugger.application.model_providers.resolve_provider_live_config",
        side_effect=ProviderRegistryError("CommandCode adapter crashed: binary corrupted"),
    ):
        with pytest.raises(ProviderConfigurationError, match="legacy CLI resolution failed"):
            gateway.resolve("cc_17", "goat-default")


def test_candidate_17_finding_2_legacy_history_record_requires_all_fields_and_strict_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2: Legacy fallback records lacking provider_runtime_identity require ALL explicit fields
    (endpoint, auth_mode, endpoint_contract, api_format).
    Strictly uses api_format, failing match if any is missing or if api_format drifted.
    Tested on both typed events and raw JSON lines paths.
    """
    from unittest.mock import MagicMock, patch
    from agentic_debugger.application.events import SessionEventKind
    from agentic_debugger.application.provider_connections import (
        PROTOCOL_CHAT_COMPLETIONS,
        PROTOCOL_RESPONSES,
        TRANSPORT_GENERIC,
    )

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Strict Hist Prov",
        base_url="http://127.0.0.1:9001",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="strict_hist_p",
        auth_mode=AUTH_BEARER,
        api_key="valid-key",
        transport_profile=TRANSPORT_GENERIC,
    )

    # --- Part A: Typed SessionEvents path ---
    sess_dir_a = tmp_path / "sessions_typed" / "s1"
    sess_dir_a.mkdir(parents=True)
    # Put a dummy journal file so candidates list finds the session directory
    (sess_dir_a / "journal.jsonl").write_text("{}", encoding="utf-8")

    ev_conf = MagicMock()
    ev_conf.event_kind = SessionEventKind.MODEL_CONFIGURED
    ev_conf.payload = {
        "provider": "strict_hist_p",
        "endpoint": "http://127.0.0.1:9001",
        "auth_mode": "bearer",
        "endpoint_contract": TRANSPORT_GENERIC,
        "api_format": PROTOCOL_CHAT_COMPLETIONS,
        "profile_id": "model-1",
    }
    ev_req = MagicMock()
    ev_req.event_kind = SessionEventKind.MODEL_REQUEST_COMPLETED
    ev_req.payload = {"status": "ok"}
    ev_req.timestamp_utc = "2026-09-05T10:01:00Z"

    mock_journal = MagicMock()
    mock_journal.events = (ev_conf, ev_req)

    with patch("agentic_debugger.application.journal.read_session_journal", return_value=mock_journal):
        # 1. Matching legacy record (all 4 fields present, api_format matches)
        succ_a = gateway.inspect_last_runtime_success("strict_hist_p", sessions_root=tmp_path / "sessions_typed")
        assert succ_a == "2026-09-05T10:01:00Z"

        # 2. Format drifted: current config updated to PROTOCOL_RESPONSES
        update_provider_config("strict_hist_p", api_format=PROTOCOL_RESPONSES)
        succ_drift = gateway.inspect_last_runtime_success("strict_hist_p", sessions_root=tmp_path / "sessions_typed")
        assert succ_drift is None

        # Reset format back to PROTOCOL_CHAT_COMPLETIONS
        update_provider_config("strict_hist_p", api_format=PROTOCOL_CHAT_COMPLETIONS)

        # 3. Missing api_format in event payload (has only api_protocol)
        ev_conf.payload = {
            "provider": "strict_hist_p",
            "endpoint": "http://127.0.0.1:9001",
            "auth_mode": "bearer",
            "endpoint_contract": TRANSPORT_GENERIC,
            "api_protocol": PROTOCOL_CHAT_COMPLETIONS,  # ONLY api_protocol, NO api_format!
            "profile_id": "model-1",
        }
        assert gateway.inspect_last_runtime_success("strict_hist_p", history_root=tmp_path / "sessions_typed") is None

        # 4. Missing endpoint in event payload
        ev_conf.payload = {
            "provider": "strict_hist_p",
            "endpoint": "",
            "auth_mode": "bearer",
            "endpoint_contract": TRANSPORT_GENERIC,
            "api_format": PROTOCOL_CHAT_COMPLETIONS,
            "profile_id": "model-1",
        }
        assert gateway.inspect_last_runtime_success("strict_hist_p", history_root=tmp_path / "sessions_typed") is None

    # --- Part B: Raw JSON lines path ---
    raw_root = tmp_path / "raw_sessions"
    raw_s1 = raw_root / "raw_ok"
    raw_s1.mkdir(parents=True)
    with open(raw_s1 / "events.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "kind": "model.configured",
            "provider": "strict_hist_p",
            "endpoint": "http://127.0.0.1:9001",
            "auth_mode": "bearer",
            "endpoint_contract": TRANSPORT_GENERIC,
            "api_format": PROTOCOL_CHAT_COMPLETIONS,
            "model": "model-1",
        }) + "\n")
        f.write(json.dumps({
            "kind": "llm.request",
            "timestamp": "2026-09-05T11:00:00Z",
            "status": "success",
        }) + "\n")

    assert gateway.inspect_last_runtime_success("strict_hist_p", history_root=raw_root) == "2026-09-05T11:00:00Z"

    # Raw JSON lines with drifted format returns None
    update_provider_config("strict_hist_p", api_format=PROTOCOL_RESPONSES)
    assert gateway.inspect_last_runtime_success("strict_hist_p", history_root=raw_root) is None
    update_provider_config("strict_hist_p", api_format=PROTOCOL_CHAT_COMPLETIONS)

    # Raw JSON lines with only api_protocol (no api_format) must return None
    raw_iso = tmp_path / "raw_isolated"
    (raw_iso / "s_no_fmt").mkdir(parents=True)
    with open(raw_iso / "s_no_fmt" / "events.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "kind": "model.configured",
            "provider": "strict_hist_p",
            "endpoint": "http://127.0.0.1:9001",
            "auth_mode": "bearer",
            "endpoint_contract": TRANSPORT_GENERIC,
            "api_protocol": PROTOCOL_CHAT_COMPLETIONS,
            "model": "model-1",
        }) + "\n")
        f.write(json.dumps({
            "kind": "llm.request",
            "timestamp": "2026-09-05T11:05:00Z",
            "status": "success",
        }) + "\n")
    assert gateway.inspect_last_runtime_success("strict_hist_p", history_root=raw_iso) is None


def test_candidate_17_finding_3_provider_runtime_identity_emission_and_model_binding_separation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3:
    1. provider_runtime_identity(cfg) removes all fallbacks to api_protocol/effective_protocol.
    2. ModelBinding.model_configured_payload() emits provider_runtime_identity ONLY if captured at resolution time.
    3. Qualified ladder, command profile, and offline bindings omit provider_runtime_identity.
    4. Model-specific protocol keeps identical provider_runtime_identity while binding.fingerprint() differs.
    """
    from agentic_debugger.application.model_gateway import ROUTE_OFFLINE, ROUTE_CONFIGURED_PROFILE, ROUTE_QUALIFIED_LADDER
    from agentic_debugger.application.provider_connections import (
        PROTOCOL_CHAT_COMPLETIONS,
        PROTOCOL_MESSAGES,
        TRANSPORT_GENERIC,
    )

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    # 1. provider_runtime_identity with dict having only api_protocol does NOT match config with api_format
    dict_with_proto_only = {
        "provider_id": "prov17",
        "api_protocol": "chat_completions",
        "auth_mode": "none",
        "base_url": "http://127.0.0.1:8000",
        "endpoint_contract": TRANSPORT_GENERIC,
    }
    dict_with_format = {
        "provider_id": "prov17",
        "api_format": "chat_completions",
        "auth_mode": "none",
        "base_url": "http://127.0.0.1:8000",
        "endpoint_contract": TRANSPORT_GENERIC,
    }
    assert provider_runtime_identity(dict_with_proto_only) != provider_runtime_identity(dict_with_format)

    # 2. Registry provider captures provider_runtime_identity from ProviderConfig; payload emits it
    add_provider_config(
        name="Candidate 17 Provider",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="c17_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )
    cfg = get_provider_config("c17_p")
    cfg_identity = provider_runtime_identity(cfg)

    binding_chat = gateway.resolve("c17_p", "model-chat")
    assert binding_chat.provider_runtime_identity == cfg_identity
    payload_chat = binding_chat.model_configured_payload()
    assert payload_chat["provider_runtime_identity"] == cfg_identity
    assert payload_chat["provider"] == "c17_p"

    # 3. Model-specific protocol keeps identical provider_runtime_identity while fingerprint() differs
    binding_msg = ModelBinding(
        provider_id="c17_p",
        model_id="claude-3-7",
        provider_model_id="claude-3-7",
        display_name="Claude 3.7",
        route=ROUTE_DIRECT_API,
        effective_protocol=PROTOCOL_MESSAGES,
        endpoint_contract=TRANSPORT_GENERIC,
        endpoint="http://127.0.0.1:8000",
        auth_mode=AUTH_NONE,
        config_fingerprint=None,
        tool_version="live-command-v1",
        provider_runtime_identity=cfg_identity,
    )
    assert binding_msg.provider_runtime_identity == binding_chat.provider_runtime_identity
    assert binding_msg.fingerprint() != binding_chat.fingerprint()
    payload_msg = binding_msg.model_configured_payload()
    assert payload_msg["provider_runtime_identity"] == cfg_identity

    # 4. Qualified ladder binding omits provider_runtime_identity
    binding_ladder = ModelBinding(
        provider_id="ollama",
        model_id="qwen3.5:cloud",
        provider_model_id="qwen3.5:cloud",
        display_name="Qwen 3.5 Cloud",
        route=ROUTE_QUALIFIED_LADDER,
        effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
        endpoint_contract=TRANSPORT_OLLAMA_CLOUD,
        endpoint=None,
        auth_mode=AUTH_BEARER,
        config_fingerprint=None,
        tool_version="1.0",
    )
    assert binding_ladder.provider_runtime_identity is None
    payload_ladder = binding_ladder.model_configured_payload()
    assert "provider_runtime_identity" not in payload_ladder
    assert payload_ladder["provider"] == "ollama"

    # 5. Configured command profile binding omits provider_runtime_identity
    binding_cmd = ModelBinding(
        provider_id="configured",
        model_id="my-profile",
        provider_model_id="my-profile",
        display_name="My Profile",
        route=ROUTE_CONFIGURED_PROFILE,
        effective_protocol=None,
        endpoint_contract=TRANSPORT_GENERIC,
        endpoint=None,
        auth_mode=None,
        config_fingerprint=None,
        tool_version="1.0",
    )
    assert binding_cmd.provider_runtime_identity is None
    payload_cmd = binding_cmd.model_configured_payload()
    assert "provider_runtime_identity" not in payload_cmd
    assert payload_cmd["provider"] == "configured"

    # 6. Offline binding omits provider_runtime_identity
    binding_off = gateway.resolve(None, "offline")
    assert binding_off.route == ROUTE_OFFLINE
    assert binding_off.provider_runtime_identity is None
    payload_off = binding_off.model_configured_payload()
    assert "provider_runtime_identity" not in payload_off
    assert "provider" not in payload_off


# ---------------------------------------------------------------------------
# Candidate 18 Tests: Findings 1, 2, 3, 4
# ---------------------------------------------------------------------------


def test_candidate_18_finding_1_historical_missing_cred_decided_before_live_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1A: Historical provider with missing creds & legacy unavailable decides statically before live resolution."""
    from unittest.mock import patch
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Historical CommandCode",
        base_url="http://127.0.0.1:57788",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="cc_hist_18",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_COMMANDCODE_GOAT,
    )

    def _bomb(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("resolve_provider_live_config must not be called")

    with patch("agentic_debugger.application.model_providers._legacy_for_config", return_value=(False, "no CLI")), \
         patch("agentic_debugger.application.model_providers.resolve_provider_live_config", side_effect=_bomb):
        binding = gateway.resolve("cc_hist_18", "model-1")

    assert binding.route == ROUTE_DIRECT_API
    assert binding.auth_mode == AUTH_BEARER
    assert binding.provider_id == "cc_hist_18"
    assert binding.provider_runtime_identity == provider_runtime_identity(get_provider_config("cc_hist_18"))


def test_candidate_18_finding_1_historical_incompatible_protocol_fails_before_live_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1B: Incompatible protocol on missing-cred historical provider fails before live resolution."""
    from unittest.mock import patch
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Historical CC Incompatible",
        base_url="http://127.0.0.1:57788",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="cc_incompat_18",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_COMMANDCODE_GOAT,
    )

    def _bomb(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("resolve_provider_live_config must not be called")

    with patch("agentic_debugger.application.model_providers._legacy_for_config", return_value=(False, "no CLI")), \
         patch("agentic_debugger.application.model_providers.resolve_provider_live_config", side_effect=_bomb), \
         patch("agentic_debugger.application.model_gateway.is_protocol_executable", return_value=False):
        with pytest.raises(IncompatibleModelError, match="not executable"):
            gateway.resolve("cc_incompat_18", "model-1")


def test_candidate_18_finding_1_historical_available_legacy_adapter_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1C: Historical provider with available legacy CLI fails closed on adapter error."""
    from unittest.mock import patch
    from agentic_debugger.application.model_providers import ProviderRegistryError
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Historical CC Adapter Error",
        base_url="http://127.0.0.1:57788",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="cc_fail_18",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_COMMANDCODE_GOAT,
    )

    with patch("agentic_debugger.application.model_providers._legacy_for_config", return_value=(True, None)), \
         patch("agentic_debugger.application.model_providers.resolve_provider_live_config",
               side_effect=ProviderRegistryError("CLI adapter binary crashed")):
        with pytest.raises(ProviderConfigurationError, match="legacy CLI resolution failed"):
            gateway.resolve("cc_fail_18", "model-1")


def test_candidate_18_finding_1_historical_direct_creds_present_live_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1D: Historical provider with direct creds fails closed on ProviderRegistryError (no static fallback)."""
    from unittest.mock import patch
    from agentic_debugger.application.model_providers import ProviderRegistryError
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Historical CC Creds Present",
        base_url="http://127.0.0.1:57788",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="cc_cred_18",
        auth_mode=AUTH_BEARER,
        api_key="sk-real-credential",
        transport_profile=TRANSPORT_COMMANDCODE_GOAT,
    )

    with patch("agentic_debugger.application.model_providers._legacy_for_config", return_value=(False, "no CLI")), \
         patch("agentic_debugger.application.model_providers.resolve_provider_live_config",
               side_effect=ProviderRegistryError("Remote service 503 unavailable")):
        with pytest.raises(ProviderConfigurationError, match="live configuration resolution failed"):
            gateway.resolve("cc_cred_18", "model-1")


def test_candidate_18_finding_1_direct_only_provider_cred_present_live_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1E: Direct-only provider with creds present fails closed on live resolution error."""
    from unittest.mock import patch
    from agentic_debugger.application.model_providers import ProviderRegistryError
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Direct Generic Creds Present",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="gen_cred_18",
        auth_mode=AUTH_BEARER,
        api_key="sk-test-key",
        transport_profile=TRANSPORT_GENERIC,
    )

    with patch("agentic_debugger.application.model_providers.resolve_provider_live_config",
               side_effect=ProviderRegistryError("Direct API connection refused")):
        with pytest.raises(ProviderConfigurationError, match="live configuration resolution failed"):
            gateway.resolve("gen_cred_18", "model-1")


def test_candidate_18_finding_2_provider_runtime_identity_complete_facts_produce_valid_hex() -> None:
    """Finding 2A: Complete 5 facts produce deterministic 64-char lowercase hex digest."""
    data = {
        "provider_id": "prov_alpha",
        "base_url": "https://api.example.com/v1",
        "endpoint_contract": TRANSPORT_GENERIC,
        "auth_mode": AUTH_BEARER,
        "api_format": PROTOCOL_CHAT_COMPLETIONS,
    }
    digest = provider_runtime_identity(data)
    assert digest is not None
    assert len(digest) == 64
    assert bool(re.fullmatch(r"[0-9a-f]{64}", digest))

    cfg = ProviderConfig(
        name="Prov Alpha",
        base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="prov_alpha",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_GENERIC,
    )
    assert provider_runtime_identity(cfg) == digest


def test_candidate_18_finding_2_provider_runtime_identity_none_on_missing_or_empty_facts() -> None:
    """Finding 2B: provider_runtime_identity returns None if any of the 5 safe facts is missing or empty."""
    assert provider_runtime_identity(None) is None
    assert provider_runtime_identity({}) is None

    base = {
        "provider_id": "p1",
        "base_url": "https://api.example.com/v1",
        "endpoint_contract": TRANSPORT_GENERIC,
        "auth_mode": AUTH_BEARER,
        "api_format": PROTOCOL_CHAT_COMPLETIONS,
    }

    for key in ("provider_id", "base_url", "endpoint_contract", "auth_mode", "api_format"):
        corrupted = dict(base)
        corrupted[key] = ""
        assert provider_runtime_identity(corrupted) is None, f"Expected None for empty {key}"

        corrupted[key] = "   "
        assert provider_runtime_identity(corrupted) is None, f"Expected None for whitespace {key}"

        del corrupted[key]
        assert provider_runtime_identity(corrupted) is None, f"Expected None for missing {key}"

        corrupted[key] = None
        assert provider_runtime_identity(corrupted) is None, f"Expected None for None {key}"

        corrupted[key] = 12345
        assert provider_runtime_identity(corrupted) is None, f"Expected None for non-string {key}"


def test_candidate_18_finding_2_provider_runtime_identity_no_api_protocol_substitution() -> None:
    """Finding 2C: api_protocol is never substituted for api_format in provider_runtime_identity."""
    dict_proto_only = {
        "provider_id": "p1",
        "base_url": "https://api.example.com/v1",
        "endpoint_contract": TRANSPORT_GENERIC,
        "auth_mode": AUTH_BEARER,
        "api_protocol": PROTOCOL_CHAT_COMPLETIONS,
    }
    assert provider_runtime_identity(dict_proto_only) is None

    dict_both = {
        "provider_id": "p1",
        "base_url": "https://api.example.com/v1",
        "endpoint_contract": TRANSPORT_GENERIC,
        "auth_mode": AUTH_BEARER,
        "api_format": PROTOCOL_CHAT_COMPLETIONS,
        "api_protocol": "messages",
    }
    dict_standard = {
        "provider_id": "p1",
        "base_url": "https://api.example.com/v1",
        "endpoint_contract": TRANSPORT_GENERIC,
        "auth_mode": AUTH_BEARER,
        "api_format": PROTOCOL_CHAT_COMPLETIONS,
    }
    assert provider_runtime_identity(dict_both) == provider_runtime_identity(dict_standard)


def test_candidate_18_finding_2_provider_runtime_identity_normalization() -> None:
    """Finding 2D: provider_runtime_identity normalizes whitespace and trailing URL slashes."""
    d1 = {
        "provider_id": "  p1  ",
        "base_url": "  https://api.example.com/v1/  ",
        "endpoint_contract": "  generic  ",
        "auth_mode": "  bearer  ",
        "api_format": "  chat_completions  ",
    }
    d2 = {
        "provider_id": "p1",
        "base_url": "https://api.example.com/v1",
        "endpoint_contract": "generic",
        "auth_mode": "bearer",
        "api_format": "chat_completions",
    }
    assert provider_runtime_identity(d1) == provider_runtime_identity(d2)


def test_candidate_18_finding_3_direct_api_requires_valid_runtime_identity() -> None:
    """Finding 3A: ROUTE_DIRECT_API requires provider_runtime_identity to be non-empty 64-char hex."""
    valid_hex = "f" * 64

    # None raises ModelGatewayError
    with pytest.raises(ModelGatewayError, match="requires provider_runtime_identity"):
        ModelBinding(
            provider_id="p1",
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
            provider_runtime_identity=None,
        )

    # Empty raises ModelGatewayError
    with pytest.raises(ModelGatewayError, match="requires provider_runtime_identity"):
        ModelBinding(
            provider_id="p1",
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
            provider_runtime_identity="",
        )

    # Invalid length raises ModelGatewayError
    with pytest.raises(ModelGatewayError, match="invalid provider_runtime_identity shape"):
        ModelBinding(
            provider_id="p1",
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
            provider_runtime_identity="f" * 63,
        )

    # Uppercase raises ModelGatewayError
    with pytest.raises(ModelGatewayError, match="invalid provider_runtime_identity shape"):
        ModelBinding(
            provider_id="p1",
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
            provider_runtime_identity="F" * 64,
        )

    # Valid lowercase 64-char hex succeeds
    b = ModelBinding(
        provider_id="p1",
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
        provider_runtime_identity=valid_hex,
    )
    assert b.provider_runtime_identity == valid_hex


def test_candidate_18_finding_3_legacy_cli_requires_valid_runtime_identity() -> None:
    """Finding 3B: ROUTE_LEGACY_CLI requires provider_runtime_identity to be non-empty 64-char hex."""
    with pytest.raises(ModelGatewayError, match="requires provider_runtime_identity"):
        ModelBinding(
            provider_id="p1",
            model_id="m1",
            provider_model_id="m1",
            display_name="m1",
            route=ROUTE_LEGACY_CLI,
            effective_protocol=None,
            endpoint_contract=TRANSPORT_OPENCODE_GO,
            endpoint=None,
            auth_mode=None,
            config_fingerprint=None,
            tool_version="1.0",
            provider_runtime_identity=None,
        )

    with pytest.raises(ModelGatewayError, match="invalid provider_runtime_identity shape"):
        ModelBinding(
            provider_id="p1",
            model_id="m1",
            provider_model_id="m1",
            display_name="m1",
            route=ROUTE_LEGACY_CLI,
            effective_protocol=None,
            endpoint_contract=TRANSPORT_OPENCODE_GO,
            endpoint=None,
            auth_mode=None,
            config_fingerprint=None,
            tool_version="1.0",
            provider_runtime_identity="not_hex_chars!",
        )

    b = ModelBinding(
        provider_id="p1",
        model_id="m1",
        provider_model_id="m1",
        display_name="m1",
        route=ROUTE_LEGACY_CLI,
        effective_protocol=None,
        endpoint_contract=TRANSPORT_OPENCODE_GO,
        endpoint=None,
        auth_mode=None,
        config_fingerprint=None,
        tool_version="1.0",
        provider_runtime_identity="e" * 64,
    )
    assert b.provider_runtime_identity == "e" * 64


def test_candidate_18_finding_3_non_registry_routes_forbid_runtime_identity() -> None:
    """Finding 3C: Non-registry routes (profile, ladder, offline) must have provider_runtime_identity=None."""
    hex64 = "a" * 64

    # Profile route with identity fails
    with pytest.raises(ModelGatewayError, match="cannot carry provider_runtime_identity"):
        ModelBinding(
            provider_id="configured",
            model_id="prof1",
            provider_model_id="prof1",
            display_name="prof1",
            route=ROUTE_CONFIGURED_PROFILE,
            effective_protocol=None,
            endpoint_contract=TRANSPORT_GENERIC,
            endpoint=None,
            auth_mode=None,
            config_fingerprint=None,
            tool_version="1.0",
            provider_runtime_identity=hex64,
        )

    # Qualified ladder with identity fails
    with pytest.raises(ModelGatewayError, match="cannot carry provider_runtime_identity"):
        ModelBinding(
            provider_id="ollama",
            model_id="qwen",
            provider_model_id="qwen",
            display_name="qwen",
            route=ROUTE_QUALIFIED_LADDER,
            effective_protocol=PROTOCOL_CHAT_COMPLETIONS,
            endpoint_contract=TRANSPORT_OLLAMA_CLOUD,
            endpoint=None,
            auth_mode=AUTH_BEARER,
            config_fingerprint=None,
            tool_version="1.0",
            provider_runtime_identity=hex64,
        )

    # Offline with identity fails
    with pytest.raises(ModelGatewayError, match="cannot carry provider_runtime_identity"):
        ModelBinding(
            provider_id=None,
            model_id="offline",
            provider_model_id=None,
            display_name="Offline",
            route=ROUTE_OFFLINE,
            effective_protocol=None,
            endpoint_contract=TRANSPORT_GENERIC,
            endpoint=None,
            auth_mode=None,
            config_fingerprint=None,
            tool_version="1.0",
            provider_runtime_identity=hex64,
        )


def test_candidate_18_finding_3_from_mapping_enforces_strict_runtime_identity_invariants() -> None:
    """Finding 3D: ModelBinding.from_mapping enforces identical strict runtime identity validation."""
    # Direct API mapping missing provider_runtime_identity
    with pytest.raises(ModelGatewayError, match="requires provider_runtime_identity"):
        ModelBinding.from_mapping({
            "provider_id": "p1",
            "model_id": "m1",
            "route": ROUTE_DIRECT_API,
            "effective_protocol": PROTOCOL_CHAT_COMPLETIONS,
            "endpoint_contract": TRANSPORT_GENERIC,
            "endpoint": "http://127.0.0.1:8000",
            "auth_mode": "none",
        })

    # Direct API mapping with non-hex identity
    with pytest.raises(ModelGatewayError, match="invalid provider_runtime_identity shape"):
        ModelBinding.from_mapping({
            "provider_id": "p1",
            "model_id": "m1",
            "route": ROUTE_DIRECT_API,
            "effective_protocol": PROTOCOL_CHAT_COMPLETIONS,
            "endpoint_contract": TRANSPORT_GENERIC,
            "endpoint": "http://127.0.0.1:8000",
            "auth_mode": "none",
            "provider_runtime_identity": "short_not_hex",
        })

    # Profile mapping with non-None identity
    with pytest.raises(ModelGatewayError, match="cannot carry provider_runtime_identity"):
        ModelBinding.from_mapping({
            "provider_id": "configured",
            "model_id": "prof1",
            "route": ROUTE_CONFIGURED_PROFILE,
            "endpoint_contract": TRANSPORT_GENERIC,
            "provider_runtime_identity": "b" * 64,
        })


def test_candidate_18_finding_3_resolve_incomplete_provider_config_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3E: resolve fails closed if provider configuration cannot establish valid runtime identity."""
    from types import SimpleNamespace
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    incomplete_cfg = SimpleNamespace(
        provider_id="incomplete_p",
        name="Incomplete Provider",
        base_url="",  # empty base_url!
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        auth_mode="none",
        transport_profile=TRANSPORT_GENERIC,
        enabled=True,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.model_gateway.get_provider_config",
        lambda p: incomplete_cfg if p == "incomplete_p" else None,
    )

    with pytest.raises(ProviderConfigurationError, match="missing runtime provenance facts"):
        gateway.resolve("incomplete_p", "m1")


def test_candidate_18_finding_4_static_preflight_direct_api_corroborates_runtime_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 4A: static_preflight for ROUTE_DIRECT_API corroborates runtime identity and fails on drift."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Preflight Corroborate",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="corrob_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    binding = gateway.resolve("corrob_p", "m1")
    pf_init = gateway.static_preflight(binding)
    assert pf_init.is_runnable is True

    # Mutate binding runtime identity (drifts from current config)
    tampered_binding = dataclasses.replace(binding, provider_runtime_identity="f" * 64)
    pf_drift = gateway.static_preflight(tampered_binding)
    assert pf_drift.is_runnable is False
    assert "runtime configuration drifted (stale binding)" in str(pf_drift.blocker_reason).lower()


def test_candidate_18_finding_4_static_preflight_legacy_cli_corroborates_runtime_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 4B: static_preflight for ROUTE_LEGACY_CLI corroborates runtime identity and fails on drift."""
    from unittest.mock import patch
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Legacy Preflight Corroborate",
        base_url="http://127.0.0.1:57788",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="leg_corrob_p",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_OPENCODE_GO,
    )

    cfg = get_provider_config("leg_corrob_p")
    rt_id = provider_runtime_identity(cfg)

    binding = ModelBinding(
        provider_id="leg_corrob_p",
        model_id="m1",
        provider_model_id="m1",
        display_name="m1",
        route=ROUTE_LEGACY_CLI,
        effective_protocol=None,
        endpoint_contract=TRANSPORT_OPENCODE_GO,
        endpoint=None,
        auth_mode=None,
        config_fingerprint=None,
        tool_version="1.0",
        provider_runtime_identity=rt_id,
    )

    with patch("agentic_debugger.application.model_providers._legacy_for_config", return_value=(True, None)):
        pf = gateway.static_preflight(binding)
        assert pf.is_runnable is True

        # Mutate base_url (drifts runtime identity)
        update_provider_config("leg_corrob_p", base_url="http://127.0.0.1:59999")
        pf_drift = gateway.static_preflight(binding)
        assert pf_drift.is_runnable is False
        assert "runtime configuration drifted (stale binding)" in str(pf_drift.blocker_reason).lower()


def test_candidate_18_finding_4_create_transport_corroborates_runtime_identity_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 4C: create_transport fails closed with StaleModelBindingError on drifted runtime identity."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Transport Corroborate",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="trans_corrob_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    binding = gateway.resolve("trans_corrob_p", "m1")

    # Initial transport creation succeeds
    transport, live_cfg = gateway.create_transport(binding)
    assert transport is not None

    # Mutate binding runtime identity (drifts from current config)
    tampered_binding = dataclasses.replace(binding, provider_runtime_identity="f" * 64)

    with pytest.raises(StaleModelBindingError, match="runtime configuration drifted"):
        gateway.create_transport(tampered_binding)






