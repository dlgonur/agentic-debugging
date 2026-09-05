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
    StaleModelBindingError,
    ROUTE_DIRECT_API,
    ROUTE_CONFIGURED_PROFILE,
    ROUTE_QUALIFIED_LADDER,
    contains_credential_shape,
    provider_runtime_identity,
    is_loopback_url,
)
from agentic_debugger.application.provider_connections import (
    add_provider_config,
    update_provider_config,
    quarantine_provider,
    PROTOCOL_CHAT_COMPLETIONS,
    AUTH_BEARER,
    AUTH_NONE,
    TRANSPORT_GENERIC,
    TRANSPORT_OLLAMA_CLOUD,
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

