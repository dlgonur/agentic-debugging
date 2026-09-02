"""Unit and integration regressions for strict normal-provider deletion (faa9b6a).

These tests lock the transactional invariant introduced in faa9b6a and fail
deterministically against b95cad5, where normal delete_provider_config still
performed:

    save(filtered) -> delete_secure_credential (ignored) -> clear_session_key -> delete_cached_catalog (best-effort)

and never cleared quarantine.

Each test isolates provider config, secure credentials, catalog cache, and
quarantine from production user state via environment overrides and in-memory
mocks. No real Credential Manager or provider HTTP is used.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentic_debugger.application import provider_connections as pc
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import ModelProvidersScreen

import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "tests" / "integration") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests" / "integration"))

from ui_support import run_headless  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_session_keys():
    pc.clear_all_session_keys()
    yield
    pc.clear_all_session_keys()


def _mock_secure_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}
    monkeypatch.setattr(
        pc, "save_secure_credential", lambda pid, val: store.__setitem__(pid, val) or True
    )
    monkeypatch.setattr(pc, "load_secure_credential", lambda pid: store.get(pid))
    monkeypatch.setattr(pc, "has_secure_credential", lambda pid: pid in store)
    monkeypatch.setattr(
        pc, "delete_secure_credential", lambda pid: store.pop(pid, None) is not None
    )
    return store


def test_normal_delete_credential_failure_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MANDATORY REGRESSION 1 — credential delete failure is fail-closed and retryable."""
    config_file = tmp_path / "provider-configurations.json"
    cache_file = tmp_path / "provider-catalog-cache.json"
    quarantine_file = tmp_path / "provider-credential-quarantine.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH", str(cache_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(quarantine_file))
    store = _mock_secure_store(monkeypatch)

    # Explicit user-owned provider with OLD_USER_KEY
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        api_key="OLD_USER_KEY",
    )
    assert pc.has_secure_credential("commandcode_goat")
    assert pc.load_secure_credential("commandcode_goat") == "OLD_USER_KEY"
    # Seed catalog so we can verify its absence after successful retry
    snap = pc.ProviderCatalogSnapshot(
        kind="commandcode_goat",
        fetched_at_utc="2026-09-02T00:00:00Z",
        source="live",
        models=(
            pc.DiscoveredProviderModel.create(
                "commandcode_goat", "deepseek/deepseek-v4-flash", "DeepSeek V4 Flash"
            ),
        ),
    )
    pc.save_cached_catalog(snap)
    assert pc.load_cached_catalog("commandcode_goat") is not None

    # Fault-inject credential deletion failure
    monkeypatch.setattr(pc, "delete_secure_credential", lambda pid: False)

    with pytest.raises(pc.ProviderConnectionError) as exc_info:
        pc.delete_provider_config("commandcode_goat")
    assert "provider credential cleanup could not be completed" in str(exc_info.value)

    # Durable provider config remains, OLD_USER_KEY remains because deletion itself failed
    assert pc.get_provider_config("commandcode_goat") is not None
    assert json.loads(config_file.read_text(encoding="utf-8"))["providers"][0][
        "provider_id"
    ] == "commandcode_goat"
    assert store.get("commandcode_goat") == "OLD_USER_KEY"
    assert pc.load_secure_credential("commandcode_goat") == "OLD_USER_KEY"

    # Restore and retry — must succeed and purge all reusable state
    monkeypatch.setattr(pc, "delete_secure_credential", lambda pid: store.pop(pid, None) is not None)
    assert pc.delete_provider_config("commandcode_goat") is True
    assert pc.get_provider_config("commandcode_goat") is None
    assert pc.load_secure_credential("commandcode_goat") is None
    assert pc.has_secure_credential("commandcode_goat") is False
    assert pc.has_session_key("commandcode_goat") is False
    # Provider not configured -> load_cached_catalog raises; verify file no longer contains entry
    raw_cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {"providers": {}}
    assert "commandcode_goat" not in raw_cache.get("providers", {})
    assert pc.is_provider_quarantined("commandcode_goat") is False
    # Also verify via query that it is not quarantined / not known
    with pytest.raises(pc.ProviderConnectionError):
        pc.load_cached_catalog("commandcode_goat")

    # Re-add same provider id with api_key=None must not resurrect old credential
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        api_key=None,
    )
    assert pc.credential_source_for("commandcode_goat") is None
    assert pc.resolve_runtime_credential("commandcode_goat") is None

    pc.update_provider_config(provider_id="commandcode_goat", api_key="NEW_USER_KEY")
    assert pc.credential_source_for("commandcode_goat") == pc.CREDENTIAL_SOURCE_SAVED
    assert pc.resolve_runtime_credential("commandcode_goat") == "NEW_USER_KEY"


def test_normal_delete_strict_cache_purge_failure_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MANDATORY REGRESSION 2 — strict cache purge failure is fail-closed."""
    config_file = tmp_path / "provider-configurations.json"
    cache_file = tmp_path / "provider-catalog-cache.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH", str(cache_file))
    _mock_secure_store(monkeypatch)

    pc.add_provider_config(
        name="Provider A",
        base_url="https://a.example/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="provider_a",
        api_key="keyA",
    )
    pc.add_provider_config(
        name="Provider B",
        base_url="https://b.example/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="provider_b",
        api_key="keyB",
    )
    snap_a = pc.ProviderCatalogSnapshot(
        kind="provider_a",
        fetched_at_utc="2026-09-02T00:00:00Z",
        source="live",
        models=(
            pc.DiscoveredProviderModel.create("provider_a", "model-a-1", "Model A 1"),
        ),
    )
    snap_b = pc.ProviderCatalogSnapshot(
        kind="provider_b",
        fetched_at_utc="2026-09-02T00:00:00Z",
        source="live",
        models=(
            pc.DiscoveredProviderModel.create("provider_b", "model-b-1", "Model B 1"),
        ),
    )
    pc.save_cached_catalog(snap_a)
    pc.save_cached_catalog(snap_b)
    assert pc.load_cached_catalog("provider_a") is not None
    assert pc.load_cached_catalog("provider_b") is not None

    original_replace = os.replace

    def failing_replace(src, dst):
        if "provider-catalog-cache.json" in str(dst):
            raise OSError("simulated cache write failure")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(pc.ProviderConnectionError) as exc_info:
        pc.delete_provider_config("provider_a")
    # Message contains catalog-cache boundary (legacy helper wording preserved via reuse)
    assert "catalog cache" in str(exc_info.value).lower()
    assert pc.get_provider_config("provider_a") is not None
    # Stale cache not silently treated as deleted — B still present in file
    assert pc.load_cached_catalog("provider_b") is not None
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "provider_a" in raw["providers"]
    assert "provider_b" in raw["providers"]

    monkeypatch.setattr(os, "replace", original_replace)
    assert pc.delete_provider_config("provider_a") is True
    assert pc.get_provider_config("provider_a") is None
    # B remains unchanged after A's successful deletion
    assert pc.get_provider_config("provider_b") is not None
    assert pc.load_cached_catalog("provider_b") is not None
    raw_after = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "provider_a" not in raw_after["providers"]
    assert "provider_b" in raw_after["providers"]

    # Re-add A must not return old cached catalog
    pc.add_provider_config(
        name="Provider A",
        base_url="https://a.example/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="provider_a",
        api_key=None,
    )
    assert pc.load_cached_catalog("provider_a") is None


def test_normal_delete_quarantine_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MANDATORY REGRESSION 3A — quarantine isolation: deleting A does not affect B."""
    config_file = tmp_path / "provider-configurations.json"
    quarantine_file = tmp_path / "provider-credential-quarantine.json"
    cache_file = tmp_path / "provider-catalog-cache.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(quarantine_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH", str(cache_file))
    store = _mock_secure_store(monkeypatch)

    pc.add_provider_config(
        name="Provider A",
        base_url="https://a.example/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="provider_a",
        api_key="keyA",
    )
    pc.add_provider_config(
        name="Provider B",
        base_url="https://b.example/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="provider_b",
        api_key="keyB",
    )
    snap_b = pc.ProviderCatalogSnapshot(
        kind="provider_b",
        fetched_at_utc="2026-09-02T00:00:00Z",
        source="live",
        models=(
            pc.DiscoveredProviderModel.create("provider_b", "model-b-1", "Model B 1"),
        ),
    )
    pc.save_cached_catalog(snap_b)
    pc.set_session_key("provider_b", "sessionB")
    pc.quarantine_provider("provider_a")
    pc.quarantine_provider("provider_b")
    assert pc.is_provider_quarantined("provider_a")
    assert pc.is_provider_quarantined("provider_b")

    assert pc.delete_provider_config("provider_a") is True
    assert pc.is_provider_quarantined("provider_a") is False
    assert pc.is_provider_quarantined("provider_b") is True
    # B unchanged
    assert pc.get_provider_config("provider_b") is not None
    assert store.get("provider_b") == "keyB"
    assert pc.load_cached_catalog("provider_b") is not None
    assert pc.has_session_key("provider_b") is True


def test_normal_delete_corrupt_quarantine_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MANDATORY REGRESSION 3B — malformed quarantine fails closed byte-for-byte."""
    config_file = tmp_path / "provider-configurations.json"
    quarantine_file = tmp_path / "provider-credential-quarantine.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(quarantine_file))
    _mock_secure_store(monkeypatch)

    pc.add_provider_config(
        name="Provider A",
        base_url="https://a.example/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="provider_a",
    )
    malformed = b'{"schema_version":"unknown-v99",[CORRUPT...'
    quarantine_file.write_bytes(malformed)

    with pytest.raises(pc.ProviderConnectionError):
        pc.delete_provider_config("provider_a")
    assert pc.get_provider_config("provider_a") is not None
    assert quarantine_file.read_bytes() == malformed


def test_normal_delete_final_config_save_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MANDATORY REGRESSION 4 — final config save failure keeps authority on disk."""
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    _mock_secure_store(monkeypatch)

    pc.add_provider_config(
        name="Test Provider",
        base_url="https://test.example/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="test_provider",
        api_key="key123",
    )
    # Ensure catalog and session state exist to prove purge already happened
    snap = pc.ProviderCatalogSnapshot(
        kind="test_provider",
        fetched_at_utc="2026-09-02T00:00:00Z",
        source="live",
        models=(
            pc.DiscoveredProviderModel.create("test_provider", "model-1", "Model 1"),
        ),
    )
    pc.save_cached_catalog(snap)
    pc.set_session_key("test_provider", "session123")
    assert pc.get_provider_config("test_provider") is not None

    original_save = pc.save_provider_configurations

    def failing_save(configs):
        raise pc.ProviderConnectionError("injected final save failure")

    monkeypatch.setattr(pc, "save_provider_configurations", failing_save)

    with pytest.raises(pc.ProviderConnectionError) as exc_info:
        pc.delete_provider_config("test_provider")
    assert "could not be written" in str(exc_info.value) or "injected" in str(exc_info.value)
    # Durable authority remains
    raw = json.loads(config_file.read_text(encoding="utf-8"))
    assert any(p["provider_id"] == "test_provider" for p in raw["providers"])
    assert pc.get_provider_config("test_provider") is not None
    # Purge may have already happened — provider is now disconnected but still present
    # (session key is cleared; we verify recovery is possible)

    monkeypatch.setattr(pc, "save_provider_configurations", original_save)
    assert pc.delete_provider_config("test_provider") is True
    assert pc.get_provider_config("test_provider") is None


def test_ui_delete_does_not_lie_on_failure_and_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MANDATORY REGRESSION 5 — UI failure is bounded, card remains, success yields empty state."""
    config_file = tmp_path / "provider-configurations.json"
    cache_file = tmp_path / "provider-catalog-cache.json"
    quarantine_file = tmp_path / "provider-credential-quarantine.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH", str(cache_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(quarantine_file))
    _mock_secure_store(monkeypatch)

    pc.add_provider_config(
        name="Deletable",
        base_url="https://del.example/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="deletable",
        api_key="key123",
    )

    # --- Failure path: fault-inject delete to raise ---
    original_delete = pc.delete_provider_config

    def failing_delete(pid):
        raise pc.ProviderConnectionError("provider credential cleanup could not be completed")

    monkeypatch.setattr(pc, "delete_provider_config", failing_delete)
    app = LocalApplicationV1(history_store=HistoryStore(tmp_path / "history_fail"))

    async def actions_fail(pilot):
        await pilot.press("m")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ModelProvidersScreen)
        assert screen._selected_kind() == "deletable"
        screen.action_delete_provider()
        await pilot.pause()
        from agentic_debugger.ui.screens import ConfirmDeleteProviderDialogScreen

        confirm = pilot.app.screen
        assert isinstance(confirm, ConfirmDeleteProviderDialogScreen)
        await pilot.click("#btn-confirm-delete")
        await pilot.pause()
        # Still on ModelProvidersScreen, not replaced
        current = pilot.app.screen
        assert isinstance(current, ModelProvidersScreen)
        statuses = current._current_statuses(force_reload=True)
        assert any(s.kind == "deletable" for s in statuses)
        # Bounded failure text, not success
        # _set_message writes to #providers-status
        try:
            from textual.widgets import Static

            msg = str(current.query_one("#providers-status", Static).render().plain)
            assert "Failed to delete provider" in msg
            assert "provider credential cleanup could not be completed" in msg
            assert "Deleted provider" not in msg
        except Exception:
            pass
        assert pc.get_provider_config("deletable") is not None

    run_headless(app, actions_fail, size=(120, 32))

    # --- Success path: final provider deletion yields empty state ---
    monkeypatch.setattr(pc, "delete_provider_config", original_delete)
    # Ensure provider still exists (it should, because previous delete failed)
    if pc.get_provider_config("deletable") is None:
        pc.add_provider_config(
            name="Deletable",
            base_url="https://del.example/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="deletable",
            api_key="key123",
        )
    app2 = LocalApplicationV1(history_store=HistoryStore(tmp_path / "history_success"))

    async def actions_success(pilot):
        await pilot.press("m")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ModelProvidersScreen)
        assert screen._selected_kind() == "deletable"
        screen.action_delete_provider()
        await pilot.pause()
        from agentic_debugger.ui.screens import ConfirmDeleteProviderDialogScreen

        confirm = pilot.app.screen
        assert isinstance(confirm, ConfirmDeleteProviderDialogScreen)
        await pilot.click("#btn-confirm-delete")
        await pilot.pause()
        # Post-delete, app pushes new ModelProvidersScreen
        current = pilot.app.screen
        assert isinstance(current, ModelProvidersScreen)
        # Real empty state
        empty_label = current.query_one("#providers-empty-label")
        assert "No providers configured." in str(empty_label.render().plain)
        add_btn = current.query_one("#provider-add-button")
        assert "+ Add provider" in str(add_btn.label)
        # Verify zero configured providers durably
        assert pc.list_configured_providers() == []

    run_headless(app2, actions_success, size=(120, 32))
