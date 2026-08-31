"""Unit tests for generic provider configurations, persistence, credentials, and cross-process execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
    provider_base_url,
    provider_transport_credential_environment,
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
    provider_transport_environment,
    format_model_display_name,
)
from agentic_debugger.application.level32 import level32_model_profiles

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))
from fake_provider_server import FakeProviderServer, scripted_chat_completion


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
    """Default configuration contains built-in providers (OpenCode, CommandCode, and Ollama)."""
    configs = list_configured_providers()
    provider_ids = [c.provider_id for c in configs]
    assert "opencode_go" in provider_ids
    assert "commandcode_goat" in provider_ids
    assert "ollama_cloud" in provider_ids

    opencode = get_provider_config("opencode_go")
    assert opencode is not None
    assert opencode.is_builtin is True
    assert opencode.api_format == PROTOCOL_CHAT_COMPLETIONS
    assert opencode.base_url == "https://opencode.ai/zen/go/v1"

    commandcode = get_provider_config("commandcode_goat")
    assert commandcode is not None
    assert commandcode.is_builtin is True
    assert commandcode.api_format == PROTOCOL_CHAT_COMPLETIONS
    assert commandcode.base_url == "https://api.commandcode.ai/provider/v1"

    ollama = get_provider_config("ollama_cloud")
    assert ollama is not None
    assert ollama.is_builtin is True
    assert ollama.api_format == PROTOCOL_CHAT_COMPLETIONS
    assert ollama.base_url == "https://ollama.com"


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
    assert delete_provider_config("commandcode_goat") is False
    assert get_provider_config("commandcode_goat") is not None
    assert delete_provider_config("ollama_cloud") is False
    assert get_provider_config("ollama_cloud") is not None


def test_builtin_provider_edit_is_authoritative_for_general_runtime():
    """Persisted edits to built-in provider base URL are respected by runtime helpers."""
    updated = update_provider_config(
        provider_id="opencode_go",
        base_url="https://custom.opencode.gateway.test/v1",
    )
    assert updated.base_url == "https://custom.opencode.gateway.test/v1"
    assert provider_base_url("opencode_go") == "https://custom.opencode.gateway.test/v1"


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
    assert len(configs) >= 3
    assert any(c.provider_id == "opencode_go" for c in configs)
    assert any(c.provider_id == "commandcode_goat" for c in configs)
    assert any(c.provider_id == "ollama_cloud" for c in configs)


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


def test_capability_ladder_isolation_with_ollama_and_custom_providers():
    """Adding custom providers or general Ollama models never affects Capability Ladder qualification."""
    add_provider_config(
        name="Custom Fast Provider",
        base_url="https://api.fast.ai/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    add_manual_model("custom_fast_provider", "llama-3.3-70b", "Llama 3.3 70B")

    ladder_profiles = level32_model_profiles()
    ladder_profile_ids = {p.profile_id for p in ladder_profiles}

    # Custom model is NOT in ladder roster
    assert "llama-3.3-70b" not in ladder_profile_ids
    # General Ollama model not in scientific roster is absent
    assert "glm-5.3-flash:cloud" not in ladder_profile_ids
    # Qualified scientific models are present
    assert "deepseek-v4-flash:cloud" in ladder_profile_ids


def test_adapter_command_contract_reconciled_with_timeout_flag():
    """Application builder constructs adapter command with --timeout, not --request-timeout-seconds."""
    set_session_key("commandcode_goat", "secret-cc-key")
    live_cfg, prov = resolve_provider_live_config(
        "commandcode_goat", "deepseek/deepseek-v4-flash", request_timeout_seconds=45.0
    )
    cmd = list(live_cfg.command)
    assert "--timeout" in cmd
    assert "--request-timeout-seconds" not in cmd
    idx = cmd.index("--timeout")
    assert cmd[idx + 1] == "45"
    assert "--base-url" in cmd
    assert "secret-cc-key" not in cmd


def test_custom_provider_cross_process_inference_survives_process_boundary():
    """Deterministic cross-process regression for custom provider adapter execution."""
    directive_json = '{"kind": "action", "name": "get_source_window", "arguments": {"path": "a.py", "start_line": 1, "end_line": 10}}'

    with FakeProviderServer(
        lambda req: (200, scripted_chat_completion(directive_json))
    ) as server:
        # 1. Persist custom provider
        cfg = add_provider_config(
            name="Custom Fake Provider",
            base_url=server.base_url,
            api_format=PROTOCOL_CHAT_COMPLETIONS,
            provider_id="custom_fake_prov",
        )
        # 2. Add manual model
        add_manual_model("custom_fake_prov", "my-test-model-1", "My Test Model 1")
        # 3. Set session key
        secret_key = "custom-secret-key-xyz-777"
        set_session_key("custom_fake_prov", secret_key)

        # 4. Resolve live config via real application resolver
        live_cfg, prov = resolve_provider_live_config("custom_fake_prov", "my-test-model-1")
        assert prov["provider"] == "custom_fake_prov"
        assert prov["route"] == "direct_api"
        assert prov["endpoint"] == server.base_url

        # 5. Build transport environment and check credential absence from argv
        child_env = dict(os.environ)
        transport_env = provider_transport_environment("custom_fake_prov")
        assert transport_env is not None
        child_env.update(transport_env)
        # Strip home/localappdata to prove isolation survival
        child_env.pop("LOCALAPPDATA", None)
        child_env.pop("USERPROFILE", None)
        child_env.pop("HOME", None)
        child_env["PYTHONIOENCODING"] = "utf-8"

        assert secret_key not in live_cfg.command

        # 6. Execute actual adapter subprocess
        request_data = {
            "protocol": {"version": "1.3", "logical_model_call_index": 0},
            "context": {"task_id": "test-task", "state": "UNDERSTAND"},
        }
        result = subprocess.run(
            list(live_cfg.command),
            input=(json.dumps(request_data) + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            env=child_env,
        )
        assert result.returncode == 0, f"Adapter failed: {result.stderr.decode('utf-8', errors='replace')}"
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        stderr_text = result.stderr.decode("utf-8", errors="replace")

        assert secret_key not in stdout_text
        assert secret_key not in stderr_text

        # 7. Check that fake server received the request with custom endpoint and bearer header
        assert len(server.requests) == 1
        assert server.requests[0]["authorization"] == f"Bearer {secret_key}"
        assert server.requests[0]["path"] == "/chat/completions"


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
