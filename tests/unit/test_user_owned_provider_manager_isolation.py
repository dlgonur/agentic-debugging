"""Unit and regression tests for User-Owned Provider Manager and Strict Test Isolation.

Verifies:
1. Fresh installations have 0 configured providers.
2. No implicit/pre-seeded provider cards (OpenCode Go, CommandCode GOAT, Ollama).
3. CLI auth detection alone does not instantiate or display provider cards.
4. Model pickers derive groups strictly from user-configured providers.
5. User-created providers are fully deletable and can return registry to 0.
6. Test isolation: test execution never mutates real user config, cache, or OS credentials.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional
import pytest

from agentic_debugger.application import provider_connections as pc
from agentic_debugger.application.model_providers import list_provider_models
from agentic_debugger.ui.screens import ModelProvidersScreen, StartSessionScreen, ChoicePickerScreen
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.application.history import HistoryStore
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests" / "integration") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests" / "integration"))

from ui_support import run_headless


def make_app(tmp_path: Path) -> LocalApplicationV1:
    return LocalApplicationV1(history_store=HistoryStore(tmp_path))


def test_fresh_installation_has_zero_configured_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean environment has 0 configured providers and 0 connection statuses."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))

    assert pc.list_configured_providers() == []
    assert pc.connection_statuses() == []
    assert pc.load_provider_configurations() == []
    assert list_provider_models() == []


def test_provider_manager_ui_renders_empty_state_on_fresh_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When no providers are configured, UI renders 'No providers configured.' and '+ Add provider'."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.press("m")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ModelProvidersScreen)

        # Sidebar displays 'No providers configured.' and '+ Add provider'
        empty_label = screen.query_one("#providers-empty-label")
        assert "No providers configured." in str(empty_label.render().plain)
        add_btn = screen.query_one("#provider-add-button")
        assert "+ Add provider" in str(add_btn.label)

        # Main panel displays empty state panel
        empty_panel = screen.query_one("#provider-empty-panel")
        assert empty_panel.visible
        detail = screen.query_one("#provider-empty-detail")
        assert "+ Add provider" in str(detail.render().plain)

        # No implicit provider buttons exist
        assert len(screen.query(".provider-item-button")) == 0
        assert len(screen.query(".provider-panel")) == 0

    run_headless(app, actions, size=(120, 32))


def test_opencode_cli_auth_alone_does_not_instantiate_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Presence of OpenCode CLI auth file does NOT create or expose OpenCode Go."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))

    # Mock OpenCode CLI auth store existing
    fake_auth = tmp_path / "opencode_auth.json"
    fake_auth.write_text(json.dumps({"token": "fake-opencode-cli-jwt"}), encoding="utf-8")
    monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: fake_auth)

    # Provider Manager remains 0 configured providers
    assert pc.list_configured_providers() == []
    assert pc.connection_statuses() == []
    assert pc.get_provider_config("opencode_go") is None

    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.press("m")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ModelProvidersScreen)
        assert len(screen.query(".provider-item-button")) == 0
        empty_label = screen.query_one("#providers-empty-label")
        assert "No providers configured." in str(empty_label.render().plain)

    run_headless(app, actions, size=(120, 32))


def test_model_picker_derives_groups_strictly_from_configured_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Model picker shows only groups for user-configured providers, never unconfigured defaults."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))

    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.press("s")
        await pilot.pause()
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)

        # 1. Fresh state: model picker has ONLY OFFLINE and CUSTOM COMMAND PROFILES (no implicit built-ins)
        start._open_model_picker()
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)
        groups = [c.group for c in picker.choices if c.group]
        assert "OPENCODE GO" not in groups
        assert "COMMANDCODE GOAT" not in groups
        assert "OLLAMA CLOUD" not in groups
        assert "OFFLINE" in groups
        assert "CUSTOM COMMAND PROFILES" in groups
        pilot.app.pop_screen()
        await pilot.pause()

        # 2. Add one provider: 'My CommandCode'
        pc.add_provider_config(
            name="My CommandCode",
            base_url="https://api.commandcode.ai/provider/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="my_commandcode",
        )
        pc.add_manual_model("my_commandcode", "deepseek/deepseek-v4-flash", "DeepSeek V4 Flash")

        # 3. Model picker now contains 'MY COMMANDCODE' and still NO OpenCode Go or Ollama Cloud
        start._open_model_picker()
        await pilot.pause()
        picker2 = pilot.app.screen
        assert isinstance(picker2, ChoicePickerScreen)
        groups2 = [c.group for c in picker2.choices if c.group]
        assert "MY COMMANDCODE" in groups2
        assert "OPENCODE GO" not in groups2
        assert "OLLAMA CLOUD" not in groups2

    run_headless(app, actions, size=(120, 32))


def test_explicit_provider_creation_lifecycle_and_deletion_to_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Complete lifecycle: Add provider -> models discoverable -> deletable back to zero."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    cache_file = tmp_path / "provider-catalog-cache.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH", str(cache_file))

    # Add custom provider
    created = pc.add_provider_config(
        name="Custom Endpoint",
        base_url="https://api.custom.endpoint/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="custom_endpoint",
        api_key="secret-key-12345",
    )
    assert created.provider_id == "custom_endpoint"
    assert len(pc.list_configured_providers()) == 1
    assert pc.has_secure_credential("custom_endpoint") is True

    # Save cached catalog snapshot
    snap = pc.ProviderCatalogSnapshot(
        kind="custom_endpoint",
        fetched_at_utc="2026-09-02T00:00:00Z",
        source="live",
        models=(
            pc.DiscoveredProviderModel.create("custom_endpoint", "custom/fast-model", "Fast Model"),
        ),
    )
    pc.save_cached_catalog(snap)
    assert pc.load_cached_catalog("custom_endpoint") is not None

    # Delete provider
    deleted = pc.delete_provider_config("custom_endpoint")
    assert deleted is True
    assert pc.list_configured_providers() == []
    assert pc.has_secure_credential("custom_endpoint") is False
    assert pc.get_provider_config("custom_endpoint") is None
    assert pc.is_known_provider("custom_endpoint") is False
    assert "custom_endpoint" not in cache_file.read_text(encoding="utf-8")


def test_test_suite_does_not_contaminate_production_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running fake provider configurations and caches does NOT touch production paths."""
    real_prod_base = os.environ.get("LOCALAPPDATA")
    if not real_prod_base:
        return

    prod_config_file = Path(real_prod_base) / "AgenticDebugger" / "provider-configurations.json"
    prod_cache_file = Path(real_prod_base) / "AgenticDebugger" / "provider-catalog-cache.json"
    prod_quarantine_file = Path(real_prod_base) / "AgenticDebugger" / "provider-credential-quarantine.json"

    prod_config_content = prod_config_file.read_bytes() if prod_config_file.exists() else None
    prod_cache_content = prod_cache_file.read_bytes() if prod_cache_file.exists() else None
    prod_quarantine_content = prod_quarantine_file.read_bytes() if prod_quarantine_file.exists() else None

    # Run isolated provider mutations
    isolated_config = tmp_path / "isolated-config.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(isolated_config))
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))

    pc.add_provider_config(
        name="Test Fake Localhost",
        base_url="http://127.0.0.1:49999/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
    )
    snap = pc.ProviderCatalogSnapshot(
        kind="test_fake_localhost",
        fetched_at_utc="2026-09-02T00:00:00Z",
        source="live",
        models=(
            pc.DiscoveredProviderModel.create("test_fake_localhost", "fake/model", "Fake Model"),
        ),
    )
    pc.save_cached_catalog(snap)

    # Verify production files remain byte-for-byte untouched
    current_config_content = prod_config_file.read_bytes() if prod_config_file.exists() else None
    current_cache_content = prod_cache_file.read_bytes() if prod_cache_file.exists() else None
    current_quarantine_content = prod_quarantine_file.read_bytes() if prod_quarantine_file.exists() else None

    assert current_config_content == prod_config_content
    assert current_cache_content == prod_cache_content
    assert current_quarantine_content == prod_quarantine_content


def test_legacy_auto_seeded_builtin_records_migrated_and_purged_on_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy is_builtin=True records are migrated away on load and orphan state purged."""
    config_file = tmp_path / "provider-configurations.json"
    cache_file = tmp_path / "provider-catalog-cache.json"
    quarantine_file = tmp_path / "provider-credential-quarantine.json"

    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH", str(cache_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(quarantine_file))

    # Seed old provider-configurations.json with legacy is_builtin=True records and a custom provider
    legacy_payload = {
        "schema_version": "provider-configurations-v1",
        "providers": [
            {
                "provider_id": "opencode_go",
                "name": "OpenCode Go",
                "base_url": "https://opencode.ai/zen/go/v1",
                "api_format": "chat_completions",
                "enabled": True,
                "is_builtin": True,
                "models": [],
            },
            {
                "provider_id": "commandcode_goat",
                "name": "CommandCode GOAT",
                "base_url": "http://127.0.0.1:56207",
                "api_format": "chat_completions",
                "enabled": True,
                "is_builtin": True,
                "models": [
                    {"model_id": "deepseek/deepseek-v4-flash", "display_name": "DeepSeek V4 Flash", "protocol": "chat_completions"},
                    {"model_id": "zai-org/glm-5.2", "display_name": "GLM 5.2", "protocol": "chat_completions"},
                ],
            },
            {
                "provider_id": "ollama_cloud",
                "name": "Ollama Cloud",
                "base_url": "https://ollama.com",
                "api_format": "chat_completions",
                "enabled": True,
                "is_builtin": True,
                "models": [],
            },
            {
                "provider_id": "user_custom_fast",
                "name": "User Custom Fast",
                "base_url": "https://api.fastuser.ai/v1",
                "api_format": "chat_completions",
                "enabled": True,
                "is_builtin": False,
                "models": [],
            },
        ],
    }
    config_file.write_text(json.dumps(legacy_payload, indent=2), encoding="utf-8")

    # Seed legacy credentials, cache, session key, and quarantine
    pc.save_secure_credential("commandcode_goat", "legacy-fake-cc-key")
    pc.save_secure_credential("user_custom_fast", "user-fast-key-12345")
    pc.set_session_key("commandcode_goat", "legacy-session-key")
    pc.quarantine_provider("commandcode_goat")

    # 1. Load configurations -> triggers automatic migration
    configs = pc.load_provider_configurations()

    # Assert: only user_custom_fast survived; all is_builtin=True records were filtered out
    assert len(configs) == 1
    assert configs[0].provider_id == "user_custom_fast"
    assert configs[0].is_builtin is False

    # Assert: disk configuration was updated cleanly
    persisted = json.loads(config_file.read_text(encoding="utf-8"))
    persisted_ids = [p["provider_id"] for p in persisted["providers"]]
    assert persisted_ids == ["user_custom_fast"]
    assert "commandcode_goat" not in persisted_ids
    assert "opencode_go" not in persisted_ids
    assert "ollama_cloud" not in persisted_ids

    # Assert: orphan state for legacy builtins was purged
    assert pc.has_secure_credential("commandcode_goat") is False
    assert pc.has_session_key("commandcode_goat") is False
    assert pc.is_provider_quarantined("commandcode_goat") is False

    # Assert: user-created credentials were NOT touched
    assert pc.has_secure_credential("user_custom_fast") is True
    assert pc.load_secure_credential("user_custom_fast") == "user-fast-key-12345"

    # 2. Re-creating CommandCode GOAT explicitly works cleanly as a user-owned provider
    created = pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        api_key="new-explicit-key-999",
    )
    assert created.provider_id == "commandcode_goat"
    assert created.is_builtin is False
    assert created.base_url == "https://api.commandcode.ai/provider/v1"
    assert pc.has_secure_credential("commandcode_goat") is True
    assert pc.load_secure_credential("commandcode_goat") == "new-explicit-key-999"
    assert pc.credential_source_for("commandcode_goat") == pc.CREDENTIAL_SOURCE_SAVED


def test_corrupt_config_fails_closed_and_preserves_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt config file fails closed with ProviderConnectionError and stays byte-for-byte unchanged."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))

    # Write corrupt JSON bytes
    corrupt_bytes = b"{\n  \"schema_version\": \"provider-configurations-v1\",\n  \"providers\": [CORRUPT SYNTAX HERE..."
    config_file.write_bytes(corrupt_bytes)

    # 1. Loading fails closed with ProviderConnectionError
    with pytest.raises(pc.ProviderConnectionError) as exc_info:
        pc.load_provider_configurations()
    assert "malformed" in str(exc_info.value) or "invalid" in str(exc_info.value)

    # Assert file on disk was NOT modified or overwritten
    assert config_file.read_bytes() == corrupt_bytes

    # 2. Mutation operations fail closed without corrupting or overwriting the file
    with pytest.raises(pc.ProviderConnectionError):
        pc.add_provider_config(
            name="Attempted Provider",
            base_url="https://api.attempt.com/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        )
    assert config_file.read_bytes() == corrupt_bytes

    with pytest.raises(pc.ProviderConnectionError):
        pc.delete_provider_config("any_provider")
    assert config_file.read_bytes() == corrupt_bytes

    # 3. ModelProvidersScreen displays Configuration Error banner rather than "No providers configured."
    app = make_app(tmp_path)

    async def actions(pilot):
        await pilot.press("m")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ModelProvidersScreen)

        # Header / sidebar shows configuration error
        empty_label = screen.query_one("#providers-empty-label")
        assert "Configuration Error" in str(empty_label.render().plain)

        # Main panel details show configuration error
        msg = screen.query_one("#provider-empty-message")
        assert "Configuration Error" in str(msg.render().plain)
        detail = screen.query_one("#provider-empty-detail")
        assert "Provider configuration error" in str(detail.render().plain)

    run_headless(app, actions, size=(120, 32))

    # Verify bytes still unchanged after UI render
    assert config_file.read_bytes() == corrupt_bytes


def test_corrupt_config_oversized_and_invalid_schema_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oversized file and invalid schema versions fail closed."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))

    # 1. Oversized file (> 256KB)
    huge_data = json.dumps({"schema_version": "provider-configurations-v1", "providers": []}).encode("utf-8") + (b" " * (300 * 1024))
    config_file.write_bytes(huge_data)
    with pytest.raises(pc.ProviderConnectionError) as exc:
        pc.load_provider_configurations()
    assert "exceeded" in str(exc.value) or "bound" in str(exc.value)

    # 2. Invalid schema version
    bad_schema = json.dumps({"schema_version": "unknown-v99", "providers": []}).encode("utf-8")
    config_file.write_bytes(bad_schema)
    with pytest.raises(pc.ProviderConnectionError) as exc2:
        pc.load_provider_configurations()
    assert "schema" in str(exc2.value)


def test_connection_authority_separation_unconfigured_builtins_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unconfigured built-ins fail closed before provider request, child process creation, or HTTP."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))

    # Environment variables set for built-in providers
    monkeypatch.setenv("COMMAND_CODE_API_KEY", "env-cc-key-12345")
    monkeypatch.setenv("OPENCODE_API_KEY", "env-oc-key-12345")

    # Built-ins are NOT in configured providers
    assert pc.is_known_provider("commandcode_goat") is False
    assert pc.is_known_provider("opencode_go") is False
    assert pc.credential_source_for("commandcode_goat") is None
    assert pc.credential_source_for("opencode_go") is None
    assert pc.resolve_runtime_credential("commandcode_goat") is None
    assert pc.resolve_runtime_credential("opencode_go") is None
    assert pc.provider_transport_credential_environment("commandcode_goat") is None
    assert pc.provider_transport_credential_environment("opencode_go") is None

    # Base URL raises ProviderConnectionError
    with pytest.raises(pc.ProviderConnectionError):
        pc.provider_base_url("commandcode_goat")

    # Protocol resolution raises ProviderConnectionError
    with pytest.raises(pc.ProviderConnectionError):
        pc.resolve_model_protocol("commandcode_goat", "deepseek/deepseek-v4-flash")

    # Model providers live config resolution raises ProviderRegistryError
    from agentic_debugger.application.model_providers import (
        ProviderRegistryError,
        resolve_provider_live_config,
    )
    with pytest.raises(ProviderRegistryError) as exc_reg:
        resolve_provider_live_config("commandcode_goat", "deepseek/deepseek-v4-flash")
    assert "not configured" in str(exc_reg.value)

    # Configured source validation fails closed
    from agentic_debugger.application.configured_source import _validate_registry_params
    from agentic_debugger.application.worker_scenarios import ScenarioInputError
    with pytest.raises(ScenarioInputError) as exc_scen:
        _validate_registry_params({
            "provider": "commandcode_goat",
            "model_id": "deepseek/deepseek-v4-flash",
        })
    assert "not a known provider" in str(exc_scen.value)


def test_orphan_credential_reuse_regression_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Secure credential deletion failure during migration fails closed, remains retryable, and never resurrects."""
    config_file = tmp_path / "provider-configurations.json"
    cache_file = tmp_path / "provider-catalog-cache.json"
    quarantine_file = tmp_path / "provider-credential-quarantine.json"

    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH", str(cache_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(quarantine_file))

    # 1. Seed legacy: commandcode_goat with is_builtin=True
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
    config_file.write_text(json.dumps(legacy_payload, indent=2), encoding="utf-8")

    # 2. Seed saved legacy credential in secure storage
    pc.save_secure_credential("commandcode_goat", "legacy-orphan-test-key-12345")
    assert pc.has_secure_credential("commandcode_goat") is True
    assert pc.load_secure_credential("commandcode_goat") == "legacy-orphan-test-key-12345"

    # 3. Fault-inject secure deletion failure
    original_delete = pc.delete_secure_credential
    monkeypatch.setattr(pc, "delete_secure_credential", lambda pid: False)

    # 4. Trigger migration -> MUST fail closed with ProviderConnectionError
    with pytest.raises(pc.ProviderConnectionError) as exc_info:
        pc.load_provider_configurations()
    assert "credential cleanup failed" in str(exc_info.value)

    # Assert: migration authority was NOT lost, provider config file still contains is_builtin=True
    persisted_on_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert len(persisted_on_disk["providers"]) == 1
    assert persisted_on_disk["providers"][0]["provider_id"] == "commandcode_goat"
    assert persisted_on_disk["providers"][0]["is_builtin"] is True
    assert pc.has_secure_credential("commandcode_goat") is True

    # 5. Restore credential deletion
    monkeypatch.setattr(pc, "delete_secure_credential", original_delete)

    # 6. Retry migration -> completes cleanly
    configs = pc.load_provider_configurations()
    assert configs == []
    assert pc.has_secure_credential("commandcode_goat") is False
    assert pc.load_secure_credential("commandcode_goat") is None
    persisted_after = json.loads(config_file.read_text(encoding="utf-8"))
    assert persisted_after["providers"] == []

    # 7. Explicitly recreate CommandCode with correct URL and api_key=None
    created = pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        api_key=None,
    )
    assert created.provider_id == "commandcode_goat"
    assert pc.has_secure_credential("commandcode_goat") is False
    assert pc.credential_source_for("commandcode_goat") is None
    assert pc.resolve_runtime_credential("commandcode_goat") is None

    # 8. Explicitly save a NEW key
    pc.update_provider_config(
        provider_id="commandcode_goat",
        api_key="new-explicit-key-99999",
    )
    assert pc.has_secure_credential("commandcode_goat") is True
    assert pc.credential_source_for("commandcode_goat") == pc.CREDENTIAL_SOURCE_SAVED
    assert pc.resolve_runtime_credential("commandcode_goat") == "new-explicit-key-99999"


def test_legacy_migration_stale_catalog_purge_failure_and_non_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale catalog purge failure during migration fails closed; after successful migration stale catalog never reappears."""
    config_file = tmp_path / "provider-configurations.json"
    cache_file = tmp_path / "provider-catalog-cache.json"

    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH", str(cache_file))

    # Seed legacy config
    legacy_payload = {
        "schema_version": "provider-configurations-v1",
        "providers": [
            {
                "provider_id": "commandcode_goat",
                "name": "CommandCode GOAT",
                "base_url": "http://127.0.0.1:56207",
                "api_format": "chat_completions",
                "enabled": True,
                "is_builtin": True,
                "models": [],
            }
        ],
    }
    config_file.write_text(json.dumps(legacy_payload, indent=2), encoding="utf-8")

    # Seed cache file with 2 models
    cache_data = {
        "schema_version": "provider-catalog-cache-v1",
        "providers": {
            "commandcode_goat": {
                "kind": "commandcode_goat",
                "fetched_at_utc": "2026-09-02T00:00:00Z",
                "source": "live",
                "truncated": False,
                "models": [
                    {"model_id": "localhost/local-model-1", "protocol": "chat_completions"},
                    {"model_id": "localhost/local-model-2", "protocol": "chat_completions"},
                ],
            }
        },
    }
    cache_file.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

    # Fault-inject cache write failure during purge
    original_replace = os.replace
    def failing_replace(src, dst):
        if "provider-catalog-cache.json" in str(dst):
            raise OSError("simulated disk error writing catalog cache")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)

    # Migration fails closed
    with pytest.raises(pc.ProviderConnectionError) as exc_info:
        pc.load_provider_configurations()
    assert "catalog cache could not be written" in str(exc_info.value)

    # Config file still has legacy record (not committed)
    persisted = json.loads(config_file.read_text(encoding="utf-8"))
    assert persisted["providers"][0]["is_builtin"] is True

    # Restore replace and retry migration
    monkeypatch.setattr(os, "replace", original_replace)
    configs = pc.load_provider_configurations()
    assert configs == []
    assert json.loads(config_file.read_text(encoding="utf-8"))["providers"] == []

    # Re-create CommandCode provider explicitly
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
    )

    # Previous localhost 2-model catalog must NOT reappear
    assert pc.load_cached_catalog("commandcode_goat") is None
    status = pc.provider_connection_status("commandcode_goat")
    assert status.model_count == 0
    assert status.cached_models == ()


def test_legacy_migration_corrupt_quarantine_fails_closed_and_preserves_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt quarantine file causes migration to fail closed without overwriting quarantine or config."""
    config_file = tmp_path / "provider-configurations.json"
    quarantine_file = tmp_path / "provider-credential-quarantine.json"

    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(quarantine_file))

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
    config_file.write_text(json.dumps(legacy_payload, indent=2), encoding="utf-8")

    corrupt_quarantine_bytes = b"{\n  \"schema_version\": \"unknown-v99\", [CORRUPT..."
    quarantine_file.write_bytes(corrupt_quarantine_bytes)

    # Migration fails closed
    with pytest.raises(pc.ProviderConnectionError):
        pc.load_provider_configurations()

    # Quarantine bytes and config bytes are untouched
    assert quarantine_file.read_bytes() == corrupt_quarantine_bytes
    assert json.loads(config_file.read_text(encoding="utf-8"))["providers"][0]["is_builtin"] is True

    # Fix quarantine file and retry
    quarantine_file.unlink()
    configs = pc.load_provider_configurations()
    assert configs == []


def test_forwarded_credential_authority_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forwarded private credential is rejected when provider is unconfigured, accepted when configured."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))

    # Set ambient env and private session env
    monkeypatch.setenv("COMMAND_CODE_API_KEY", "ambient-test-key-111")
    monkeypatch.setenv("AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY", "forwarded-private-key-222")

    # 1. Unconfigured provider -> all credential resolution helpers return None
    assert pc.is_known_provider("commandcode_goat") is False
    assert pc.credential_source_for("commandcode_goat") is None
    assert pc.resolve_runtime_credential("commandcode_goat") is None
    assert pc.provider_transport_credential_environment("commandcode_goat") is None
    assert pc.provider_session_credential_environment("commandcode_goat") is None

    # 2. Explicitly configure provider with the historical transport
    # profile (the historical forwarded session variable belongs to that
    # explicit contract, never to the bare technical ID).
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
    )

    # 3. Configured provider -> forwarded private session variable is accepted for worker hop
    assert pc.is_known_provider("commandcode_goat") is True
    assert pc.credential_source_for("commandcode_goat") == pc.CREDENTIAL_SOURCE_SESSION_KEY
    assert pc.resolve_runtime_credential("commandcode_goat") == "forwarded-private-key-222"
    assert pc.provider_transport_credential_environment("commandcode_goat") == {
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY": "forwarded-private-key-222"
    }


def test_add_provider_dialog_discovers_catalog_after_screen_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Add Provider (dialog UI, credentialed) auto-refreshes the catalog.

    Regression: the post-add auto-refresh once called ``action_refresh``
    on the replacement ModelProvidersScreen before that screen mounted,
    so the worker never ran and the provider stayed stuck in the
    refreshing set with an empty catalog.  The refresh must actually
    execute once the new screen is mounted (loopback fake endpoint).
    """
    import asyncio
    import time

    from fake_provider_server import FakeProviderServer, catalog_payload
    from textual.widgets import Input

    from agentic_debugger.ui.screens import AddProviderDialogScreen

    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH",
        str(tmp_path / "provider-catalog-cache.json"),
    )
    secure_store: Dict[str, str] = {}
    monkeypatch.setattr(
        pc, "save_secure_credential", lambda k, v: secure_store.__setitem__(k, v) or True
    )
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: secure_store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in secure_store)
    pc.clear_all_session_keys()

    app = make_app(tmp_path)

    with FakeProviderServer(
        lambda req: (200, catalog_payload(["dialog-model-1", "dialog-model-2"]))
    ) as server:

        async def actions(pilot) -> None:
            await pilot.press("m")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ModelProvidersScreen)
            assert pc.list_configured_providers() == []

            pilot.app.screen.action_add_provider()
            await pilot.pause()
            dialog = pilot.app.screen
            assert isinstance(dialog, AddProviderDialogScreen)
            dialog.query_one("#input-name", Input).value = "Dialog Provider"
            dialog.query_one("#input-url", Input).value = server.base_url
            dialog.query_one("#input-key", Input).value = "dialog-key-not-real"
            dialog._do_save()
            await pilot.pause()

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                cfg = pc.get_provider_config("dialog_provider")
                if cfg is not None and len(cfg.models) >= 2:
                    break
                await asyncio.sleep(0.05)
                await pilot.pause()

            cfg = pc.get_provider_config("dialog_provider")
            assert cfg is not None, "provider missing after dialog save"
            assert {m.model_id for m in cfg.models} >= {"dialog-model-1", "dialog-model-2"}
            assert server.request_count >= 1
            screen = pilot.app.screen
            assert isinstance(screen, ModelProvidersScreen)
            assert screen._refreshing == set(), "refresh must complete, not stick"

        run_headless(app, actions, size=(120, 32))
