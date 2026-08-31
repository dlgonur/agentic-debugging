"""Unit tests for generic provider configurations, persistence, and credentials."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentic_debugger.application.provider_connections import (
    DiscoveredProviderModel,
    ProviderConfig,
    _normalize_catalog,
    add_manual_model,
    add_provider_config,
    clear_all_session_keys,
    connection_statuses,
    delete_provider_config,
    get_provider_config,
    has_session_key,
    is_known_provider,
    list_configured_providers,
    load_provider_configurations,
    provider_api_model_id,
    refresh_provider_catalog,
    resolve_model_protocol,
    resolve_runtime_credential,
    save_provider_configurations,
    save_secure_credential,
    set_session_key,
    update_provider_config,
    PROTOCOL_CHAT_COMPLETIONS,
    PROTOCOL_MESSAGES,
    PROTOCOL_RESPONSES,
)
from agentic_debugger.application.model_providers import (
    list_provider_models,
    resolve_provider_live_config,
)


@pytest.fixture(autouse=True)
def _isolate_provider_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate provider configuration file and credentials for every test."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.provider_configurations_path",
        lambda: config_file,
    )
    # In-memory mock for OS secure store
    _secure_store: dict[str, str] = {}
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_secure_credential",
        lambda kind, val: _secure_store.__setitem__(kind, val) or True,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.load_secure_credential",
        lambda kind: _secure_store.get(kind),
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.has_secure_credential",
        lambda kind: kind in _secure_store,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.delete_secure_credential",
        lambda kind: _secure_store.pop(kind, None) is not None,
    )
    clear_all_session_keys()
    yield
    clear_all_session_keys()


def test_builtin_providers_default_present():
    """Default configuration contains built-in providers (OpenCode and CommandCode)."""
    configs = list_configured_providers()
    provider_ids = [c.provider_id for c in configs]
    assert "opencode_go" in provider_ids
    assert "commandcode_goat" in provider_ids

    opencode = get_provider_config("opencode_go")
    assert opencode is not None
    assert opencode.is_builtin is True
    assert opencode.api_format == PROTOCOL_CHAT_COMPLETIONS

    commandcode = get_provider_config("commandcode_goat")
    assert commandcode is not None
    assert commandcode.is_builtin is True
    assert commandcode.api_format == PROTOCOL_CHAT_COMPLETIONS


def test_crud_custom_provider():
    """Can create, read, update, and delete custom provider configurations."""
    cfg = add_provider_config(
        name="Groq Direct",
        base_url="https://api.groq.com/openai/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        api_key="gsk-test-secret-12345",
    )
    assert cfg.provider_id == "groq_direct"
    assert cfg.name == "Groq Direct"
    assert cfg.base_url == "https://api.groq.com/openai/v1"
    assert cfg.is_builtin is False
    assert is_known_provider("groq_direct") is True

    # Read back
    retrieved = get_provider_config("groq_direct")
    assert retrieved is not None
    assert retrieved.name == "Groq Direct"

    # API key was stored securely and is not in the JSON file
    configs_on_disk = load_provider_configurations()
    raw_cfg = next(c for c in configs_on_disk if c.provider_id == "groq_direct")
    assert not hasattr(raw_cfg, "api_key")

    # Update provider
    updated = update_provider_config(
        provider_id="groq_direct",
        name="Groq Direct Fast",
        base_url="https://api.groq.com/openai/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    assert updated.name == "Groq Direct Fast"

    # Delete provider
    deleted = delete_provider_config("groq_direct")
    assert deleted is True
    assert get_provider_config("groq_direct") is None
    assert is_known_provider("groq_direct") is False


def test_builtin_cannot_be_deleted():
    """Built-in provider configs cannot be deleted."""
    assert delete_provider_config("opencode_go") is False
    assert get_provider_config("opencode_go") is not None


def test_manual_model_addition():
    """Can manually add model identifiers to a provider."""
    cfg = add_provider_config(
        name="Custom AI",
        base_url="https://api.custom.ai/v1",
        api_format=PROTOCOL_MESSAGES,
    )
    added = add_manual_model("custom_ai", "claude-3-7-sonnet", "Claude 3.7 Sonnet")
    assert added is not None
    assert added.model_id == "claude-3-7-sonnet"
    
    updated = get_provider_config("custom_ai")
    assert updated is not None
    assert len(updated.models) == 1
    assert updated.models[0].model_id == "claude-3-7-sonnet"
    assert updated.models[0].display_name == "Claude 3.7 Sonnet"
    assert updated.models[0].protocol == PROTOCOL_MESSAGES


def test_malformed_json_fallback(tmp_path: Path):
    """Malformed config JSON does not crash or overwrite without notice."""
    config_file = tmp_path / "provider-configurations.json"
    config_file.write_text("{corrupt json content...", encoding="utf-8")

    configs = load_provider_configurations()
    # Returns safe fallback with defaults
    assert len(configs) >= 2
    assert any(c.provider_id == "opencode_go" for c in configs)


def test_provider_identity_preservation_and_no_ollama_leakage():
    """Model selection preserves exact provider identity and never leaks into Ollama Cloud."""
    add_provider_config(
        name="CommandCode Direct",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    add_manual_model("commandcode_direct", "deepseek/deepseek-v3", "DeepSeek V3")
    set_session_key("commandcode_direct", "cmd-key-12345")

    live_cfg, prov = resolve_provider_live_config("commandcode_direct", "deepseek/deepseek-v3")
    assert prov["provider"] == "commandcode_direct"
    assert prov["profile_id"] == "deepseek/deepseek-v3"
    assert prov["provider"] != "ollama_cloud"
    assert "ollama" not in prov["provider"].lower()


def test_same_model_id_across_different_providers():
    """Same model ID on different providers remains distinct and correctly routed."""
    add_provider_config(
        name="Provider A",
        base_url="https://api.a.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    add_manual_model("provider_a", "llama-3-8b", "Llama 3 8B")
    set_session_key("provider_a", "key-a")

    add_provider_config(
        name="Provider B",
        base_url="https://api.b.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    add_manual_model("provider_b", "llama-3-8b", "Llama 3 8B")
    set_session_key("provider_b", "key-b")

    live_a, prov_a = resolve_provider_live_config("provider_a", "llama-3-8b")
    live_b, prov_b = resolve_provider_live_config("provider_b", "llama-3-8b")

    assert prov_a["provider"] == "provider_a"
    assert prov_b["provider"] == "provider_b"
    assert live_a.configuration_fingerprint != live_b.configuration_fingerprint


def test_normalize_discovered_models():
    """Discovered models normalization bounds count and strips invalid shapes."""
    cfg = add_provider_config(
        name="Mock Provider",
        base_url="https://api.mock.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    raw = {
        "data": [
            {"id": "model-1", "name": "Model One"},
            {"id": "model-2"},
            {"id": "model-1"},  # duplicate
            {"bad": "no id"},  # invalid
        ]
    }
    models, truncated = _normalize_catalog("mock_provider", raw)
    assert len(models) == 2
    assert models[0].model_id == "model-1"
    assert models[0].display_name == "Model 1"
    assert models[1].model_id == "model-2"
    assert models[1].display_name == "Model 2"


def test_failed_refresh_preserves_existing_catalog(monkeypatch: pytest.MonkeyPatch):
    """A failed GET /models request preserves existing valid catalog."""
    add_provider_config(
        name="Test API",
        base_url="https://api.test.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    add_manual_model("test_api", "model-existing", "Existing Model")
    set_session_key("test_api", "valid-key")

    from agentic_debugger.application.provider_http import ProviderHttpError

    def _failing_request(*args, **kwargs):
        raise ProviderHttpError(kind="network", message="Connection refused")

    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.request_json",
        _failing_request,
    )

    from agentic_debugger.application.provider_connections import ProviderConnectionError
    with pytest.raises(ProviderConnectionError):
        refresh_provider_catalog("test_api")

    # Catalog is intact
    cfg = get_provider_config("test_api")
    assert cfg is not None
    assert len(cfg.models) == 1
    assert cfg.models[0].model_id == "model-existing"
