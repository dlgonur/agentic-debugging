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
