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
    ProviderCatalogSnapshot,
    ProviderConfig,
    _normalize_catalog,
    add_manual_model,
    add_provider_config,
    clear_all_session_keys,
    connection_statuses,
    delete_cached_catalog,
    delete_provider_config,
    get_provider_config,
    has_session_key,
    is_known_provider,
    list_configured_providers,
    load_cached_catalog,
    load_provider_configurations,
    provider_api_model_id,
    provider_base_url,
    provider_transport_credential_environment,
    refresh_provider_catalog,
    resolve_model_protocol,
    resolve_runtime_credential,
    save_cached_catalog,
    save_provider_configurations,
    save_secure_credential,
    set_session_key,
    update_provider_config,
    ProviderConnectionError,
    credential_source_for,
    has_secure_credential,
    load_secure_credential,
    CREDENTIAL_SOURCE_SAVED,
    CREDENTIAL_SOURCE_SESSION_KEY,
    CREDENTIAL_SOURCE_ENVIRONMENT,
    CREDENTIAL_SOURCE_CLI_AUTH_STORE,
    _PROVIDER_CREDENTIAL_SOURCE_LABELS,
    PROTOCOL_CHAT_COMPLETIONS,
    PROTOCOL_MESSAGES,
    PROTOCOL_RESPONSES,
)
from agentic_debugger.application import provider_connections as pc
from agentic_debugger.application.model_providers import (
    list_provider_models,
    resolve_provider_live_config,
    provider_transport_environment,
    format_model_display_name,
)
from agentic_debugger.application.level32 import level32_model_profiles

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))
from fake_provider_server import FakeProviderServer, catalog_payload, scripted_chat_completion


@pytest.fixture(autouse=True)
def _isolate_provider_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate provider configuration file and credentials for every test."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
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


def test_fresh_provider_config_is_empty():
    """A fresh installation has zero configured providers (no implicit or pre-seeded entries)."""
    configs = list_configured_providers()
    assert configs == []
    assert get_provider_config("opencode_go") is None
    assert get_provider_config("commandcode_goat") is None
    assert get_provider_config("ollama_cloud") is None


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


def test_user_created_provider_can_be_deleted():
    """User-created provider configurations can be deleted to return registry to zero providers."""
    add_provider_config(
        name="OpenCode Go",
        base_url="https://opencode.ai/zen/go/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="opencode_go",
    )
    assert get_provider_config("opencode_go") is not None
    assert delete_provider_config("opencode_go") is True
    assert get_provider_config("opencode_go") is None
    assert list_configured_providers() == []


def test_builtin_provider_edit_is_authoritative_for_general_runtime():
    """Persisted edits to provider base URL are respected by runtime helpers."""
    add_provider_config(
        name="OpenCode Go",
        base_url="https://opencode.ai/zen/go/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="opencode_go",
    )
    updated = update_provider_config(
        provider_id="opencode_go",
        base_url="https://custom.opencode.gateway.test/v1",
    )
    assert updated.base_url == "https://custom.opencode.gateway.test/v1"
    assert provider_base_url("opencode_go") == "https://custom.opencode.gateway.test/v1"


def test_edit_provider_credential_update_and_preservation():
    """Editing a provider with a new API key updates secure storage; blank API key preserves existing credential."""
    # 1. Add custom provider with initial API key
    cfg = add_provider_config(
        name="Mistral Direct",
        base_url="https://api.mistral.ai/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        api_key="initial-mistral-key-111",
    )
    assert has_secure_credential("mistral_direct") is True
    assert credential_source_for("mistral_direct") == "saved"
    assert resolve_runtime_credential("mistral_direct") == "initial-mistral-key-111"

    # 2. Update with new API key -> replaces credential
    update_provider_config(
        provider_id="mistral_direct",
        api_key="updated-mistral-key-222",
    )
    assert resolve_runtime_credential("mistral_direct") == "updated-mistral-key-222"

    # 3. Update unrelated metadata with blank / None api_key -> PRESERVES existing credential
    update_provider_config(
        provider_id="mistral_direct",
        base_url="https://api.mistral.ai/v2",
        api_key=None,
    )
    assert resolve_runtime_credential("mistral_direct") == "updated-mistral-key-222"
    assert get_provider_config("mistral_direct").base_url == "https://api.mistral.ai/v2"

    # 4. CommandCode GOAT credential update and preservation
    add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
    )
    update_provider_config(
        provider_id="commandcode_goat",
        api_key="goat-fake-key-999",
    )
    assert resolve_runtime_credential("commandcode_goat") == "goat-fake-key-999"

    # Update CommandCode GOAT base_url leaving api_key blank -> credential preserved
    update_provider_config(
        provider_id="commandcode_goat",
        base_url="https://api.commandcode.ai/provider/v2",
        api_key=None,
    )
    assert resolve_runtime_credential("commandcode_goat") == "goat-fake-key-999"
    assert get_provider_config("commandcode_goat").base_url == "https://api.commandcode.ai/provider/v2"


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
    """Malformed config JSON fails closed and raises ProviderConnectionError."""
    config_file = tmp_path / "provider-configurations.json"
    config_file.write_text("{corrupt json content...", encoding="utf-8")

    with pytest.raises(ProviderConnectionError):
        load_provider_configurations()
    assert config_file.read_text(encoding="utf-8") == "{corrupt json content..."


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
    add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
    )
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


def test_base_url_validation_rejects_credentials_queries_and_fragments(tmp_path: Path):
    """Base URLs with userinfo, queries, fragments, non-loopback http, or control chars are rejected."""
    from agentic_debugger.application.provider_connections import (
        ProviderConnectionError,
        add_provider_config,
        update_provider_config,
        ProviderConfig,
    )

    invalid_urls = [
        "https://user:password@api.example.com/v1",
        "https://api.example.com/v1?token=secret123",
        "https://api.example.com/v1#section",
        "http://insecure.remote.host.com/v1",
        "https://api.example.com/v1\r\ninjected:header",
        "ftp://api.example.com/v1",
        "",
        "https://" + "a" * 2050 + ".com",
    ]

    for bad_url in invalid_urls:
        with pytest.raises(ProviderConnectionError):
            add_provider_config(name="Bad Provider", base_url=bad_url, api_format=PROTOCOL_CHAT_COMPLETIONS)

        # Also rejected by from_dict
        parsed = ProviderConfig.from_dict({
            "provider_id": "test_bad",
            "name": "Bad",
            "base_url": bad_url,
        })
        assert parsed is None


def test_base_url_validation_accepts_valid_https_and_loopback_http():
    """Valid HTTPS endpoints and loopback HTTP endpoints are accepted and canonicalized."""
    cfg1 = add_provider_config(
        name="Valid HTTPS",
        base_url="https://api.groq.com/openai/v1/",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    assert cfg1.base_url == "https://api.groq.com/openai/v1"

    cfg2 = add_provider_config(
        name="Localhost HTTP",
        base_url="http://localhost:11434/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    assert cfg2.base_url == "http://localhost:11434/v1"

    cfg3 = add_provider_config(
        name="Loopback IP HTTP",
        base_url="http://127.0.0.1:8000/",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    assert cfg3.base_url == "http://127.0.0.1:8000"


def test_builtin_edits_authoritative_across_app_restart():
    """Edits to configured providers are persisted and remain authoritative across restarts."""
    add_provider_config(name="OpenCode Go", base_url="https://opencode.ai/zen/go/v1", api_format=PROTOCOL_CHAT_COMPLETIONS, provider_id="opencode_go")
    add_provider_config(name="CommandCode GOAT", base_url="https://api.commandcode.ai/provider/v1", api_format=PROTOCOL_CHAT_COMPLETIONS, provider_id="commandcode_goat")
    add_provider_config(name="Ollama", base_url="https://ollama.com", api_format=PROTOCOL_CHAT_COMPLETIONS, provider_id="ollama_cloud")

    # 1. Update endpoints
    update_provider_config("opencode_go", base_url="https://custom.opencode.proxy.corp/v1")
    update_provider_config("commandcode_goat", base_url="https://custom.commandcode.proxy.corp/v1")
    update_provider_config("ollama_cloud", base_url="http://127.0.0.1:11434")

    # 2. Simulate fresh application restart by calling load_provider_configurations
    reloaded = load_provider_configurations()
    reloaded_map = {c.provider_id: c for c in reloaded}

    assert reloaded_map["opencode_go"].base_url == "https://custom.opencode.proxy.corp/v1"
    assert reloaded_map["commandcode_goat"].base_url == "https://custom.commandcode.proxy.corp/v1"
    assert reloaded_map["ollama_cloud"].base_url == "http://127.0.0.1:11434"

    # 3. provider_base_url also resolves the persisted edits
    assert provider_base_url("opencode_go") == "https://custom.opencode.proxy.corp/v1"
    assert provider_base_url("commandcode_goat") == "https://custom.commandcode.proxy.corp/v1"
    assert provider_base_url("ollama_cloud") == "http://127.0.0.1:11434"


def test_ollama_general_runtime_common_provider_contract_end_to_end(tmp_path: Path):
    """General runtime Ollama uses the common Provider Manager contract end-to-end."""
    directive = (
        '{"kind": "action", "name": "get_source_window", '
        '"arguments": {"path": "pkg/mod.py", "start_line": 1, "end_line": 40}}'
    )
    secret_key = "ollama-secret-session-token"

    def fake_ollama_handler(request):
        if request["path"] == "/v1/models":
            return 200, {
                "object": "list",
                "data": [
                    {"id": "ollama/custom-llama-3", "name": "Custom Llama 3"},
                    {"id": "ollama/deepseek-r1-custom", "name": "DeepSeek R1 Custom"},
                ],
            }
        elif request["path"] == "/v1/chat/completions":
            return 200, scripted_chat_completion(directive)
        return 404, {"error": "not found"}

    with FakeProviderServer(fake_ollama_handler) as server:
        # 1. Add Ollama provider configuration pointing to the fake server endpoint
        add_provider_config(
            name="Ollama",
            base_url=server.base_url,
            api_format=PROTOCOL_CHAT_COMPLETIONS,
            provider_id="ollama_cloud",
        )
        set_session_key("ollama_cloud", secret_key)

        # 2. Refresh catalog via common Provider Manager
        snapshot = refresh_provider_catalog("ollama_cloud", engine="stdlib")
        assert len(snapshot.models) == 2
        assert snapshot.models[0].model_id == "ollama/custom-llama-3"

        # 3. Verify general picker lists the refreshed models
        picker_models = list_provider_models(include_ollama=True)
        ollama_models = [m for m in picker_models if m.kind == "ollama_cloud"]
        ollama_ids = [m.model_id for m in ollama_models]
        assert "ollama/custom-llama-3" in ollama_ids
        assert "ollama/deepseek-r1-custom" in ollama_ids

        # 4. Resolve live config for selected Ollama model
        live_cfg, provenance = resolve_provider_live_config("ollama_cloud", "ollama/custom-llama-3")
        assert provenance["provider"] == "ollama_cloud"
        assert provenance["route"] == "direct_api"
        assert provenance["api_protocol"] == "chat_completions"
        assert provenance["profile_id"] == "ollama/custom-llama-3"
        assert "--base-url" in live_cfg.command
        assert server.base_url in live_cfg.command

        # 5. Execute live config in child process against fake Ollama endpoint
        child_env = dict(os.environ)
        child_env.update(provider_transport_credential_environment("ollama_cloud"))
        child_env["PYTHONPATH"] = f"{str(REPO_ROOT)};{str(REPO_ROOT / 'scripts')}"

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
        assert secret_key not in stdout_text
        assert secret_key not in result.stderr.decode("utf-8", errors="replace")

        # 6. Verify fake server received request with authorization and correct path
        assert any(r["path"] == "/v1/chat/completions" for r in server.requests)
        chat_req = next(r for r in server.requests if r["path"] == "/v1/chat/completions")
        assert chat_req["authorization"] == f"Bearer {secret_key}"


def test_rejected_credential_url_never_enters_persistence_or_provenance(tmp_path: Path):
    """Rejected URLs containing secrets never touch disk or provenance."""
    secret = "leak-test-secret-998877"
    bad_url = f"https://user:{secret}@malicious.endpoint.corp/v1"
    from agentic_debugger.application.provider_connections import (
        ProviderConnectionError,
        add_provider_config,
        provider_configurations_path,
    )

    with pytest.raises(ProviderConnectionError):
        add_provider_config(name="Leak Test", base_url=bad_url, api_format=PROTOCOL_CHAT_COMPLETIONS)

    config_path = provider_configurations_path()
    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
        assert secret not in content


def test_config_isolation_environment_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH and AGENTIC_DEBUGGER_CONFIG_DIR override config path."""
    explicit_file = tmp_path / "custom-provider-config.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(explicit_file))
    from agentic_debugger.application.provider_connections import provider_configurations_path
    assert provider_configurations_path() == explicit_file

    monkeypatch.delenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", raising=False)
    custom_dir = tmp_path / "custom_config_dir"
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(custom_dir))
    assert provider_configurations_path() == custom_dir / "provider-configurations.json"


def test_test_provider_creation_never_leaks_to_default_operator_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Creating a test custom provider writes only to the isolated test config path."""
    operator_path = tmp_path / "operator_profile" / "AgenticDebugger" / "provider-configurations.json"
    isolated_test_path = tmp_path / "test_env" / "provider-configurations.json"

    # Seed operator config with only built-in default schema
    operator_path.parent.mkdir(parents=True, exist_ok=True)
    operator_path.write_text(
        json.dumps({
            "schema_version": "provider-configurations-v1",
            "providers": [
                {"provider_id": "opencode_go", "name": "OpenCode Go", "base_url": "https://opencode.ai/zen/go/v1", "api_format": "chat_completions", "is_builtin": True, "enabled": True, "models": []},
                {"provider_id": "commandcode_goat", "name": "CommandCode GOAT", "base_url": "https://api.commandcode.ai/provider/v1", "api_format": "chat_completions", "is_builtin": True, "enabled": True, "models": []},
                {"provider_id": "ollama_cloud", "name": "Ollama", "base_url": "https://ollama.com", "api_format": "chat_completions", "is_builtin": True, "enabled": True, "models": []},
            ],
        }),
        encoding="utf-8",
    )

    # Point active configuration to isolated test path
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(isolated_test_path))

    # Add fake review/test provider
    fake_cfg = add_provider_config(
        name="Groq Direct",
        base_url="https://api.groq.com/openai/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    assert fake_cfg.provider_id == "groq_direct"

    # Verify isolated test path contains the fake provider
    assert isolated_test_path.exists()
    test_content = isolated_test_path.read_text(encoding="utf-8")
    assert "groq_direct" in test_content

    # Verify operator path was NOT mutated
    operator_content = operator_path.read_text(encoding="utf-8")
    assert "groq_direct" not in operator_content
    assert "Groq Direct" not in operator_content


def test_legitimate_groq_direct_provider_survives_save_reload_and_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A legitimate user-created custom provider named 'Groq Direct' survives save and reload unchanged."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))

    # Add custom provider with exact name "Groq Direct" and base URL "https://api.groq.com/openai/v1"
    created = add_provider_config(
        name="Groq Direct",
        base_url="https://api.groq.com/openai/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    assert created.provider_id == "groq_direct"
    assert created.name == "Groq Direct"
    assert created.base_url == "https://api.groq.com/openai/v1"
    assert created.models == ()

    # Reload from disk using standard load_provider_configurations()
    loaded = load_provider_configurations()
    loaded_map = {c.provider_id: c for c in loaded}

    assert "groq_direct" in loaded_map
    groq_loaded = loaded_map["groq_direct"]
    assert groq_loaded.name == "Groq Direct"
    assert groq_loaded.base_url == "https://api.groq.com/openai/v1"
    assert groq_loaded.is_builtin is False
    assert groq_loaded.models == ()

    # Verify no implicit providers were created
    assert len(loaded_map) == 1
    assert "opencode_go" not in loaded_map
    assert "commandcode_goat" not in loaded_map
    assert "ollama_cloud" not in loaded_map


def test_environment_credential_and_no_secure_store_state_preserves_custom_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Custom providers without OS secure credentials or using environment keys are preserved and never deleted."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))

    # Write a custom provider directly to disk with no secure credential stored
    data = {
        "schema_version": "provider-configurations-v1",
        "providers": [
            {
                "provider_id": "custom_prod_proxy",
                "name": "Production Proxy",
                "base_url": "https://proxy.internal.corp/v1",
                "api_format": "chat_completions",
                "is_builtin": False,
                "enabled": True,
                "models": [],
            }
        ],
    }
    config_file.write_text(json.dumps(data, indent=1), encoding="utf-8")

    # Load configurations in absence of any OS secure credential
    configs = load_provider_configurations()
    pids = [c.provider_id for c in configs]
    assert "custom_prod_proxy" in pids

    # Verify on-disk file was not deleted or rewritten
    on_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert any(p["provider_id"] == "custom_prod_proxy" for p in on_disk["providers"])


def test_headless_render_execution_uses_isolated_config_and_never_mutates_operator_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Headless render/review executions isolate config path and leave operator configuration byte-for-byte untouched."""
    operator_file = tmp_path / "operator_config" / "provider-configurations.json"
    operator_file.parent.mkdir(parents=True, exist_ok=True)
    initial_operator_bytes = json.dumps({
        "schema_version": "provider-configurations-v1",
        "providers": [
            {"provider_id": "opencode_go", "name": "OpenCode Go", "base_url": "https://opencode.ai/zen/go/v1", "api_format": "chat_completions", "is_builtin": True, "enabled": True, "models": []},
            {"provider_id": "commandcode_goat", "name": "CommandCode GOAT", "base_url": "https://api.commandcode.ai/provider/v1", "api_format": "chat_completions", "is_builtin": True, "enabled": True, "models": []},
            {"provider_id": "ollama_cloud", "name": "Ollama", "base_url": "https://ollama.com", "api_format": "chat_completions", "is_builtin": True, "enabled": True, "models": []},
        ],
    }, indent=2).encode("utf-8")
    operator_file.write_bytes(initial_operator_bytes)

    # Point default operator config path to operator_file
    monkeypatch.setenv("LOCALAPPDATA", str(operator_file.parent.parent))

    # Run a subprocess that invokes run_headless to add a test custom provider
    import textwrap
    integration_dir = REPO_ROOT / "tests" / "integration"
    script = textwrap.dedent(f"""
        import os, sys, tempfile
        from pathlib import Path
        sys.path.insert(0, {repr(str(REPO_ROOT))})
        sys.path.insert(0, {repr(str(integration_dir))})
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.application.history import HistoryStore
        from agentic_debugger.ui.screens import ProviderConnectionsScreen, AddProviderDialogScreen
        from ui_support import run_headless
        from textual.widgets import Input

        app = LocalApplicationV1(history_store=HistoryStore(Path(tempfile.mkdtemp())))

        async def actions(pilot):
            await pilot.press("m")
            await pilot.pause()
            await pilot.click("#provider-add-button")
            await pilot.pause()
            add_dlg = pilot.app.screen
            add_dlg.query_one("#input-name", Input).value = "Render Fake Provider"
            add_dlg.query_one("#input-url", Input).value = "https://api.fake.render/v1"
            await pilot.click("#btn-save-dialog")
            await pilot.pause()

        run_headless(app, actions, size=(100, 30))
    """)
    cmd = [sys.executable, "-c", script]
    import subprocess
    # Run without AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH / AGENTIC_DEBUGGER_CONFIG_DIR in env
    env = os.environ.copy()
    env.pop("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", None)
    env.pop("AGENTIC_DEBUGGER_CONFIG_DIR", None)
    env["LOCALAPPDATA"] = str(operator_file.parent.parent)

    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert res.returncode == 0, f"Subprocess failed: {res.stderr}"

    # Operator file must remain 100% byte-for-byte identical
    assert operator_file.read_bytes() == initial_operator_bytes


def test_delete_provider_cleans_up_catalog_cache_and_secure_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Deleting a custom provider purges config, secure credentials, and cached catalog entries."""
    cache_path = tmp_path / "provider-catalog-cache.json"
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.catalog_cache_path",
        lambda: cache_path,
    )

    # Add custom provider with API key
    cfg = add_provider_config(
        name="Purge Target Provider",
        base_url="https://api.purge-target.test/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        api_key="secret-key-to-purge",
    )
    pid = cfg.provider_id
    assert get_provider_config(pid) is not None

    # Save a cached catalog snapshot for this provider
    snapshot = ProviderCatalogSnapshot(
        kind=pid,
        fetched_at_utc="2026-08-31T12:00:00Z",
        source="live",
        models=(
            DiscoveredProviderModel(
                kind=pid,
                model_id="purge-model-1",
                display_name="Purge Model 1",
                protocol=PROTOCOL_CHAT_COMPLETIONS,
                runnable=True,
            ),
        ),
    )
    save_cached_catalog(snapshot)
    assert load_cached_catalog(pid) is not None

    # Delete provider
    deleted = delete_provider_config(pid)
    assert deleted is True

    # Verify config removed
    assert get_provider_config(pid) is None

    # Verify catalog cache entry removed from disk
    raw_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert pid not in raw_cache.get("providers", {})

    # Loading cached catalog for a deleted unknown provider fails closed
    with pytest.raises(Exception):
        load_cached_catalog(pid)


def test_durable_credential_source_after_restart_and_reload():
    """Saving credential persists to secure store and survives simulated restart (cleared session memory)."""
    cfg = add_provider_config(
        name="Durable AI Provider",
        base_url="https://api.durable.ai/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        api_key="durable-secret-key-12345",
    )
    pid = cfg.provider_id
    assert has_secure_credential(pid) is True
    assert credential_source_for(pid) == CREDENTIAL_SOURCE_SAVED

    # Simulate restart: clear all in-memory process/session state
    clear_all_session_keys()
    assert has_session_key(pid) is False

    # After restart, the saved secure credential remains authoritative and resolves without user re-entry
    assert has_secure_credential(pid) is True
    assert credential_source_for(pid) == CREDENTIAL_SOURCE_SAVED
    assert resolve_runtime_credential(pid) == "durable-secret-key-12345"


def test_explicit_failure_on_secure_save_failure_no_silent_session_fallback(monkeypatch: pytest.MonkeyPatch):
    """When secure store save fails, error is explicit and never silently falls back to session memory."""
    # Force secure store save to fail
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_secure_credential",
        lambda kind, val: False,
    )
    clear_all_session_keys()

    with pytest.raises(ProviderConnectionError, match="Could not save API key securely"):
        add_provider_config(
            name="Failing Secure Provider",
            base_url="https://api.failing.test/v1",
            api_format=PROTOCOL_CHAT_COMPLETIONS,
            api_key="failing-secret-key",
        )

    # Must NOT have silently populated session key
    assert has_session_key("failing_secure_provider") is False
    assert credential_source_for("failing_secure_provider") is None

    # Also test update path
    cfg = add_provider_config(
        name="Update Fail Target",
        base_url="https://api.update-fail.test/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
    )
    with pytest.raises(ProviderConnectionError, match="Could not save API key securely"):
        update_provider_config(
            provider_id=cfg.provider_id,
            api_key="new-failing-secret",
        )
    assert has_session_key(cfg.provider_id) is False


def test_source_priority_contract(monkeypatch: pytest.MonkeyPatch):
    """Explicit source priority: saved > session_key > environment > cli_auth_store for configured provider."""
    pid = "commandcode_goat"
    add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
    )
    pc.clear_all_session_keys()
    monkeypatch.delenv("COMMAND_CODE_API_KEY", raising=False)
    monkeypatch.delenv("AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY", raising=False)

    # 1. No credential configured
    assert pc.credential_source_for(pid) is None
    assert pc.resolve_runtime_credential(pid) is None

    # 2. Environment variable present
    monkeypatch.setenv("COMMAND_CODE_API_KEY", "env-key-111")
    assert pc.credential_source_for(pid) == pc.CREDENTIAL_SOURCE_ENVIRONMENT
    assert pc.resolve_runtime_credential(pid) == "env-key-111"

    # 3. Session key present (takes priority over environment)
    pc.set_session_key(pid, "session-key-222")
    assert pc.credential_source_for(pid) == pc.CREDENTIAL_SOURCE_SESSION_KEY
    assert pc.resolve_runtime_credential(pid) == "session-key-222"

    # 4. Saved secure credential present (takes priority over both session key and environment)
    pc.save_secure_credential(pid, "saved-key-333")
    assert pc.credential_source_for(pid) == pc.CREDENTIAL_SOURCE_SAVED
    assert pc.resolve_runtime_credential(pid) == "saved-key-333"


def test_truthful_credential_source_labels():
    """Credential source labels map truthfully to distinct states."""
    assert _PROVIDER_CREDENTIAL_SOURCE_LABELS[CREDENTIAL_SOURCE_SAVED] == "saved"
    assert _PROVIDER_CREDENTIAL_SOURCE_LABELS[CREDENTIAL_SOURCE_SESSION_KEY] == "session only"
    assert _PROVIDER_CREDENTIAL_SOURCE_LABELS[CREDENTIAL_SOURCE_ENVIRONMENT] == "environment"
    assert _PROVIDER_CREDENTIAL_SOURCE_LABELS[CREDENTIAL_SOURCE_CLI_AUTH_STORE] == "CLI auth"


# -- transactional provider/credential atomicity (regressions against c8aef31) ---


def test_add_provider_atomic_when_secure_save_fails(monkeypatch: pytest.MonkeyPatch):
    """A failed secure save during Add Provider commits nothing at all.

    Against c8aef318 the provider configuration was persisted BEFORE the
    secure-store attempt, so the supposedly failed provider existed on disk.
    """
    from agentic_debugger.application.provider_connections import (
        provider_configurations_path,
    )

    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_secure_credential",
        lambda kind, val: False,
    )
    clear_all_session_keys()

    with pytest.raises(ProviderConnectionError, match="Could not save API key securely"):
        add_provider_config(
            name="Atomic Failure",
            base_url="https://new.example/v1",
            api_format=PROTOCOL_CHAT_COMPLETIONS,
            api_key="fake-new-key",
        )

    assert get_provider_config("atomic_failure") is None
    assert is_known_provider("atomic_failure") is False
    assert has_session_key("atomic_failure") is False
    assert has_secure_credential("atomic_failure") is False

    config_path = provider_configurations_path()
    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
        assert "atomic_failure" not in content
        assert "new.example" not in content


def test_add_provider_unusable_key_rejected_before_any_mutation():
    """An invalid API key is rejected before the configuration is written."""
    with pytest.raises(
        ProviderConnectionError, match="API key is missing, invalid, or oversized"
    ):
        add_provider_config(
            name="Bad Key Provider",
            base_url="https://badkey.example/v1",
            api_format=PROTOCOL_CHAT_COMPLETIONS,
            api_key="bad\x07key",
        )
    assert get_provider_config("bad_key_provider") is None
    assert has_session_key("bad_key_provider") is False


def test_update_provider_atomic_when_secure_save_fails(monkeypatch: pytest.MonkeyPatch):
    """A failed secure save during Edit Provider keeps the original provider fully authoritative."""
    from agentic_debugger.application.provider_connections import (
        provider_configurations_path,
    )

    seed = add_provider_config(
        name="Original",
        base_url="https://old.example/v1",
        api_format=PROTOCOL_MESSAGES,
        api_key="fake-old-key",
    )
    pid = seed.provider_id
    add_manual_model(pid, "original-model-1", "Original Model 1")

    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_secure_credential",
        lambda kind, val: False,
    )

    with pytest.raises(ProviderConnectionError, match="Could not save API key securely"):
        update_provider_config(
            provider_id=pid,
            name="Changed",
            base_url="https://new.example/v1",
            api_format=PROTOCOL_CHAT_COMPLETIONS,
            api_key="fake-new-key",
        )

    reloaded = get_provider_config(pid)
    assert reloaded is not None
    assert reloaded.name == "Original"
    assert reloaded.base_url == "https://old.example/v1"
    assert reloaded.api_format == PROTOCOL_MESSAGES
    assert [m.model_id for m in reloaded.models] == ["original-model-1"]
    assert reloaded.enabled is True

    # No accepted configuration references the rejected endpoint
    config_path = provider_configurations_path()
    assert "https://new.example" not in config_path.read_text(encoding="utf-8")

    # The existing credential remains safely associated with the original provider
    assert resolve_runtime_credential(pid) == "fake-old-key"
    assert credential_source_for(pid) == CREDENTIAL_SOURCE_SAVED
    assert has_session_key(pid) is False


def test_failed_update_never_sends_credential_to_rejected_endpoint(
    monkeypatch: pytest.MonkeyPatch,
):
    """After a rejected endpoint+credential edit, the normal resolution/catalog-refresh
    boundary never forwards the stored credential to the rejected endpoint."""
    def responder(request):
        if request["path"] == "/v1/models":
            return 200, catalog_payload(["original-model-1"])
        return 404, {"error": "not scripted"}

    with FakeProviderServer(responder) as server:
        seed = add_provider_config(
            name="Endpoint Guard",
            base_url=f"{server.base_url}/v1",
            api_format=PROTOCOL_CHAT_COMPLETIONS,
            api_key="fake-old-key",
        )
        pid = seed.provider_id

        monkeypatch.setattr(
            "agentic_debugger.application.provider_connections.save_secure_credential",
            lambda kind, val: False,
        )
        with pytest.raises(ProviderConnectionError):
            update_provider_config(
                provider_id=pid,
                name="Endpoint Guard Changed",
                base_url="https://new.example/v1",
                api_key="fake-new-key",
            )

        # Normal provider resolution after the failed edit resolves the ORIGINAL endpoint
        assert provider_base_url(pid) == f"{server.base_url}/v1"

        recorded_urls: list[str] = []
        real_request_json = pc.request_json

        def recording_request_json(method, url, **kwargs):
            recorded_urls.append(url)
            return real_request_json(method, url, **kwargs)

        monkeypatch.setattr(pc, "request_json", recording_request_json)

        snapshot = refresh_provider_catalog(pid)

        # Every catalog request went to the original loopback endpoint only
        assert recorded_urls == [f"{server.base_url}/v1/models"]
        assert all("new.example" not in url for url in recorded_urls)
        assert len(server.requests) == 1
        assert server.requests[0]["path"] == "/v1/models"
        assert server.requests[0]["authorization"] == "Bearer fake-old-key"
        assert snapshot.models[0].model_id == "original-model-1"


def test_config_write_failure_after_credential_mutation_restores_previous_pair(
    monkeypatch: pytest.MonkeyPatch,
):
    """If the configuration write fails after the secure store was updated, the
    previous credential/config pair is restored (never new endpoint + old key,
    never old endpoint + unintended new key)."""
    seed = add_provider_config(
        name="Rollback Target",
        base_url="https://old.example/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        api_key="fake-old-key",
    )
    pid = seed.provider_id

    def _failing_save(configs):
        raise ProviderConnectionError("provider configuration could not be written")

    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_provider_configurations",
        _failing_save,
    )

    with pytest.raises(
        ProviderConnectionError, match="provider configuration could not be written"
    ) as excinfo:
        update_provider_config(
            provider_id=pid,
            name="Rollback Target Changed",
            base_url="https://new.example/v1",
            api_key="fake-new-key",
        )

    # Rollback error text never exposes either credential value
    error_text = str(excinfo.value)
    assert "fake-old-key" not in error_text
    assert "fake-new-key" not in error_text

    reloaded = get_provider_config(pid)
    assert reloaded is not None
    assert reloaded.name == "Rollback Target"
    assert reloaded.base_url == "https://old.example/v1"
    assert resolve_runtime_credential(pid) == "fake-old-key"
    assert has_session_key(pid) is False


def test_config_write_failure_after_first_add_leaves_no_provider_and_no_credential(
    monkeypatch: pytest.MonkeyPatch,
):
    """A failed Add Provider whose configuration write fails leaves no provider,
    no stored credential, and no session residue."""

    def _failing_save(configs):
        raise ProviderConnectionError("provider configuration could not be written")

    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_provider_configurations",
        _failing_save,
    )

    with pytest.raises(
        ProviderConnectionError, match="provider configuration could not be written"
    ):
        add_provider_config(
            name="No Residue",
            base_url="https://residue.example/v1",
            api_format=PROTOCOL_CHAT_COMPLETIONS,
            api_key="fake-new-key",
        )

    assert get_provider_config("no_residue") is None
    assert has_secure_credential("no_residue") is False
    assert credential_source_for("no_residue") is None
    assert has_session_key("no_residue") is False


def test_unrestorable_credential_rolls_back_to_deleted_key(monkeypatch: pytest.MonkeyPatch):
    """When the previous credential cannot be restored after a failed configuration
    write, the stored key is deleted so no unintended pair remains active."""
    seed = add_provider_config(
        name="Fail Closed Target",
        base_url="https://old.example/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        api_key="fake-old-key",
    )
    pid = seed.provider_id

    secure_calls: list[str] = []

    def flaky_save(kind, val):
        # The new-key write succeeds; the restore write of the old key fails.
        secure_calls.append("save")
        return len(secure_calls) == 1

    deleted: list[str] = []

    def recording_delete(kind):
        deleted.append(kind)
        return True

    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_secure_credential",
        flaky_save,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.delete_secure_credential",
        recording_delete,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.load_secure_credential",
        lambda kind: "fake-old-key" if kind == pid else None,
    )

    def _failing_save(configs):
        raise ProviderConnectionError("provider configuration could not be written")

    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_provider_configurations",
        _failing_save,
    )

    with pytest.raises(
        ProviderConnectionError, match="could not be restored and was removed"
    ):
        update_provider_config(
            provider_id=pid,
            base_url="https://new.example/v1",
            api_key="fake-new-key",
        )

    # Fail closed: the stored key was deleted rather than left paired with the
    # original configuration as an unintended replacement credential.
    assert deleted == [pid]


# -- catastrophic rollback: restore AND delete both fail (regression vs c7adaad) ---


def _catastrophic_fixture(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str,
    provider_id: str = "catastrophic",
):
    """Force restore and delete to fail after a successful new-key write.

    Returns (provider_id, fake server) with the durable failure state applied.
    """
    _secure: dict[str, str] = {}
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_secure_credential",
        lambda kind, val: _secure.__setitem__(kind, val) or True,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.load_secure_credential",
        lambda kind: _secure.get(kind),
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.has_secure_credential",
        lambda kind: kind in _secure,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.delete_secure_credential",
        lambda kind: _secure.pop(kind, None) is not None,
    )
    add_provider_config(
        name="Catastrophic",
        base_url=server_url,
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id=provider_id,
        api_key="fake-old-key",
    )
    real_save_configurations = save_provider_configurations
    # The new-key write succeeds; restore AND delete both fail afterward.
    call_count = 0

    def failing_save(kind, val):
        nonlocal call_count
        call_count += 1
        return call_count == 1

    def undo_failure_mocks() -> None:
        """Restore working secure-store/config mocks (used to prove recovery)."""
        monkeypatch.setattr(
            "agentic_debugger.application.provider_connections.save_secure_credential",
            lambda kind, val: _secure.__setitem__(kind, val) or True,
        )
        monkeypatch.setattr(
            "agentic_debugger.application.provider_connections.load_secure_credential",
            lambda kind: _secure.get(kind),
        )
        monkeypatch.setattr(
            "agentic_debugger.application.provider_connections.has_secure_credential",
            lambda kind: kind in _secure,
        )
        monkeypatch.setattr(
            "agentic_debugger.application.provider_connections.delete_secure_credential",
            lambda kind: _secure.pop(kind, None) is not None,
        )
        monkeypatch.setattr(
            "agentic_debugger.application.provider_connections.save_provider_configurations",
            real_save_configurations,
        )

    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_secure_credential",
        failing_save,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.load_secure_credential",
        lambda kind: "fake-old-key" if kind == provider_id else None,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.delete_secure_credential",
        lambda kind: False,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.save_provider_configurations",
        lambda configs: (_ for _ in ()).throw(
            ProviderConnectionError("provider configuration could not be written")
        ),
    )

    with pytest.raises(ProviderConnectionError):
        update_provider_config(
            provider_id=provider_id,
            base_url="https://new.example/v1",
            api_key="fake-new-key",
        )
    return provider_id, undo_failure_mocks


def test_catastrophic_rollback_failure_quarantines_durably(monkeypatch: pytest.MonkeyPatch):
    """restore-fails AND delete-fails leaves a durable fail-closed quarantine state
    that survives a fresh process/reload simulation."""
    pid, _ = _catastrophic_fixture(monkeypatch, "https://old.example/v1")

    # Persisted configuration still describes the ORIGINAL endpoint
    cfg = get_provider_config(pid)
    assert cfg is not None
    assert cfg.base_url == "https://old.example/v1"

    # Simulate a fresh process: drop all in-memory state and reload everything
    clear_all_session_keys()
    load_provider_configurations()

    # The quarantine is durable: the provider resolves no credential at all.
    # The unintended key may still occupy the OS slot (deletion failed) but it
    # must never be SELECTED, resolved, or forwarded.
    assert has_secure_credential(pid) is True  # uncommitted residue remains
    assert credential_source_for(pid) is None
    assert resolve_runtime_credential(pid) is None
    assert has_session_key(pid) is False
    assert provider_transport_environment(pid) is None
    assert pc.provider_session_credential_environment(pid) is None


def test_catastrophic_rollback_failure_blocks_refresh_before_http_in_current_process(
    monkeypatch: pytest.MonkeyPatch,
):
    """A quarantined provider fails refresh BEFORE HTTP transport in the current process."""
    def responder(request):
        return 200, catalog_payload(["model-1"])

    with FakeProviderServer(responder) as server:
        pid, _ = _catastrophic_fixture(monkeypatch, server.base_url)

        with pytest.raises(
            ProviderConnectionError, match="requires recovery"
        ):
            refresh_provider_catalog(pid)

        assert server.request_count == 0


def test_catastrophic_rollback_failure_blocks_refresh_after_restart(
    monkeypatch: pytest.MonkeyPatch,
):
    """After a fresh process/reload, the durable quarantine still blocks refresh
    before HTTP transport."""
    def responder(request):
        return 200, catalog_payload(["model-1"])

    with FakeProviderServer(responder) as server:
        pid, _ = _catastrophic_fixture(monkeypatch, server.base_url)

        # Simulate a fresh process/reload preserving the durable failure state
        clear_all_session_keys()
        load_provider_configurations()
        _current_statuses = pc.connection_statuses()
        assert any(s.kind == pid and not s.connected for s in _current_statuses)

        with pytest.raises(
            ProviderConnectionError, match="requires recovery"
        ):
            refresh_provider_catalog(pid)

        assert server.request_count == 0
        assert resolve_runtime_credential(pid) is None


def test_catastrophic_recovery_via_explicit_successful_save(monkeypatch: pytest.MonkeyPatch):
    """A subsequent successful Edit Provider save clears quarantine and restores
    normal request capability."""
    def responder(request):
        if request["path"] == "/v1/models":
            return 200, catalog_payload(["recovered-model-1"])
        return 404, {"error": "not scripted"}

    with FakeProviderServer(responder) as server:
        pid, undo_failure_mocks = _catastrophic_fixture(monkeypatch, f"{server.base_url}/v1")

        # Fresh process/reload preserving the durable failure state
        clear_all_session_keys()
        load_provider_configurations()
        with pytest.raises(ProviderConnectionError, match="requires recovery"):
            refresh_provider_catalog(pid)
        assert server.request_count == 0

        # Explicit successful re-save establishes a coherent config+credential pair
        undo_failure_mocks()
        recovered = update_provider_config(
            provider_id=pid,
            name="Catastrophic Recovered",
            base_url=f"{server.base_url}/v1",
            api_key="recovered-new-key",
        )
        assert recovered.name == "Catastrophic Recovered"
        assert credential_source_for(pid) == CREDENTIAL_SOURCE_SAVED
        assert resolve_runtime_credential(pid) == "recovered-new-key"

        # Normal provider request becomes possible again
        snapshot = refresh_provider_catalog(pid)
        assert server.request_count == 1
        assert snapshot.models[0].model_id == "recovered-model-1"
        assert server.requests[0]["authorization"] == "Bearer recovered-new-key"


def test_migration_transaction_ordering_clean_state_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Migration strictly cleans state before committing config; if credential deletion fails, config marker remains."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))

    # Seed legacy config
    legacy_payload = {
        "schema_version": "provider-configurations-v1",
        "providers": [
            {
                "provider_id": "commandcode_goat",
                "name": "CommandCode GOAT",
                "base_url": "https://api.commandcode.ai/provider/v1",
                "api_format": "chat_completions",
                "enabled": True,
                "is_builtin": True,
                "models": [],
            }
        ],
    }
    config_file.write_text(json.dumps(legacy_payload), encoding="utf-8")

    # Mock secure store with a saved key
    store = {"commandcode_goat": "legacy-secret-key"}
    monkeypatch.setattr(pc, "load_secure_credential", lambda pid: store.get(pid))
    monkeypatch.setattr(pc, "has_secure_credential", lambda pid: pid in store)

    # 1. Fault-inject deletion failure (returns False, key still in store)
    monkeypatch.setattr(pc, "delete_secure_credential", lambda pid: False)

    # Migration fails closed
    with pytest.raises(ProviderConnectionError) as exc_info:
        load_provider_configurations()
    assert "credential cleanup failed" in str(exc_info.value)

    # Durable configuration on disk was NOT updated
    raw_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert len(raw_disk["providers"]) == 1
    assert raw_disk["providers"][0]["is_builtin"] is True

    # 2. Fix deletion and retry
    def successful_delete(pid):
        store.pop(pid, None)
        return True

    monkeypatch.setattr(pc, "delete_secure_credential", successful_delete)
    configs = load_provider_configurations()
    assert configs == []
    raw_disk_after = json.loads(config_file.read_text(encoding="utf-8"))
    assert raw_disk_after["providers"] == []
    assert store == {}


def test_credential_helpers_reject_unconfigured_provider_with_forwarded_and_ambient_env(monkeypatch: pytest.MonkeyPatch):
    """resolve_runtime_credential and provider_transport_credential_environment reject unconfigured providers."""
    monkeypatch.setenv("COMMAND_CODE_API_KEY", "ambient-test-key")
    monkeypatch.setenv("AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY", "forwarded-session-key")

    assert is_known_provider("commandcode_goat") is False
    assert credential_source_for("commandcode_goat") is None
    assert resolve_runtime_credential("commandcode_goat") is None
    assert provider_transport_credential_environment("commandcode_goat") is None
    assert provider_transport_environment("commandcode_goat") is None

    # Recreating provider explicitly permits forwarded private session variable
    add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
    )
    assert is_known_provider("commandcode_goat") is True
    assert credential_source_for("commandcode_goat") == CREDENTIAL_SOURCE_SESSION_KEY
    assert resolve_runtime_credential("commandcode_goat") == "forwarded-session-key"
    assert provider_transport_credential_environment("commandcode_goat") == {
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY": "forwarded-session-key"
    }
