"""Unit tests for truthful provider status semantics (V2-03, ADR 0001 §9, V2 Plan §9).

Validates:
1. Credential presence or static configuration alone is NEVER reported as "Connected".
2. The owner-observed CommandCode incident (loopback offline reported as Connected) is closed.
3. Live verified probes set `live_verified=True` and `connected=True` truthfully.
4. Runtime success is observational history derived from session journals.
5. Catalog cache is invalidated on endpoint, auth, or profile mutations.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from agentic_debugger.application.model_gateway import ModelGateway
from agentic_debugger.application.provider_connections import (
    add_provider_config,
    update_provider_config,
    get_provider_config,
    save_cached_catalog,
    load_cached_catalog,
    ProviderCatalogSnapshot,
    quarantine_provider,
    PROTOCOL_CHAT_COMPLETIONS,
    AUTH_NONE,
    AUTH_BEARER,
    TRANSPORT_COMMANDCODE_GOAT,
    TRANSPORT_GENERIC,
)


def test_commandcode_offline_loopback_is_never_labeled_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner-observed defect: CommandCode GOAT at 127.0.0.1:57788 with no listener

    must report is_configured=True, credential_ready=True, live_verified=False,
    and connected=False. Headline must be 'Configured · loopback', NEVER 'Connected'.
    """
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="CommandCode GOAT",
        base_url="http://127.0.0.1:57788",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_COMMANDCODE_GOAT,
    )

    status = gateway.get_provider_status("commandcode_goat")
    assert status.is_configured is True
    assert status.credential_ready is True
    assert status.live_verified is False
    assert status.live_verified_at_utc is None
    # connected MUST be False because no live probe or session success has occurred
    assert status.connected is False
    assert "Connected" not in status.summary_headline
    assert status.summary_headline == "Configured · loopback"


def test_saved_credential_provider_offline_is_not_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider with credential present is Configured · saved, never Connected."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="Custom Provider",
        base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="custom_prov",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_GENERIC,
    )

    status = gateway.get_provider_status("custom_prov")
    assert status.is_configured is True
    assert status.live_verified is False
    assert status.connected is False
    assert "Connected" not in status.summary_headline


def test_runtime_succeeded_derived_from_session_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime succeeded is observational history derived from durable session events."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="My Provider",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="my_prov",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    # No sessions yet -> runtime_succeeded_at_utc is None
    status = gateway.get_provider_status("my_prov", sessions_root=tmp_path / "sessions")
    assert status.runtime_succeeded_at_utc is None

    # Simulate a durable session journal with a successful model interaction
    sessions_dir = tmp_path / "sessions" / "session_1"
    sessions_dir.mkdir(parents=True)
    journal_file = sessions_dir / "journal.jsonl"
    events = [
        {
            "kind": "model.configured",
            "timestamp": "2026-09-05T09:00:00Z",
            "provider": "my_prov",
            "endpoint": "http://127.0.0.1:8000",
            "model": "model-1",
        },
        {
            "kind": "llm.request",
            "timestamp": "2026-09-05T09:01:00Z",
            "provider": "my_prov",
            "status": "success",
        },
    ]
    with open(journal_file, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    # Now inspect runtime success
    last_succ = gateway.inspect_last_runtime_success("my_prov", sessions_root=tmp_path / "sessions")
    assert last_succ == "2026-09-05T09:01:00Z"

    # Status snapshot derives runtime success
    status = gateway.get_provider_status("my_prov", sessions_root=tmp_path / "sessions")
    assert status.runtime_succeeded_at_utc == "2026-09-05T09:01:00Z"
    assert status.summary_headline == "Runtime succeeded"


def test_catalog_cache_invalidation_on_config_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing base_url, auth_mode, or transport_profile invalidates cached catalog."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    add_provider_config(
        name="Mutating Provider",
        base_url="https://api.v1.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="mutating_prov",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_GENERIC,
    )
    from agentic_debugger.application.provider_connections import DiscoveredProviderModel
    save_cached_catalog(
        ProviderCatalogSnapshot(
            kind="mutating_prov",
            models=(
                DiscoveredProviderModel(
                    kind="mutating_prov",
                    model_id="old-m1",
                    display_name="Old M1",
                    protocol="chat_completions",
                    runnable=True,
                ),
                DiscoveredProviderModel(
                    kind="mutating_prov",
                    model_id="old-m2",
                    display_name="Old M2",
                    protocol="chat_completions",
                    runnable=True,
                ),
            ),
            fetched_at_utc="2026-09-01T00:00:00Z",
            source="live",
        )
    )
    cat = load_cached_catalog("mutating_prov")
    assert cat is not None
    assert [m.model_id for m in cat.models] == ["old-m1", "old-m2"]

    # Mutation: change base_url
    update_provider_config(
        provider_id="mutating_prov",
        base_url="https://api.v2.com/v1",
    )

    # Cached catalog must be deleted
    cat_after = load_cached_catalog("mutating_prov")
    assert cat_after is None

    cfg = get_provider_config("mutating_prov")
    assert cfg is not None
    assert cfg.last_refresh_utc is None
    assert cfg.models == ()


def test_non_loopback_auth_none_is_configured_no_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-loopback endpoint with auth_mode='none' must report 'Configured · no auth', never 'loopback'."""
    from agentic_debugger.application.provider_connections import ProviderConfig

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    cfg = ProviderConfig(
        name="Remote None Auth",
        base_url="https://api.remote-open.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="remote_none_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.provider_connections.get_provider_config",
        lambda pid: cfg if pid == "remote_none_p" else None,
    )
    monkeypatch.setattr(
        "agentic_debugger.application.model_gateway.get_provider_config",
        lambda pid: cfg if pid == "remote_none_p" else None,
    )

    status = gateway.get_provider_status("remote_none_p")
    assert status.is_configured is True
    assert status.credential_ready is True
    assert status.summary_headline == "Configured · no auth"
    assert "loopback" not in status.summary_headline


def test_quarantined_provider_is_configured_true_and_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quarantined provider has is_configured=True and headline 'Quarantined · recovery required'."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="Quarantined Provider",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="quarantine_sem_p",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_GENERIC,
    )
    quarantine_provider("quarantine_sem_p")

    status = gateway.get_provider_status("quarantine_sem_p")
    assert status.is_configured is True
    assert status.is_quarantined is True
    assert status.summary_headline == "Quarantined · recovery required"


def test_ui_solid_dot_driven_only_by_live_verified_not_runtime_succeeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sidebar dot ● is driven exclusively by live_verified=True, NOT by historical runtime success."""
    from agentic_debugger.ui.screens import ModelProvidersScreen
    from textual.widgets import Button

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)
    add_provider_config(
        name="Historical Provider",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="hist_dot_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    # Set up session journal to create runtime success
    sessions_dir = tmp_path / "sessions" / "s1"
    sessions_dir.mkdir(parents=True)
    events = [
        {
            "kind": "model.configured",
            "timestamp": "2026-09-05T10:00:00Z",
            "provider": "hist_dot_p",
            "endpoint": "http://127.0.0.1:8000",
            "model": "m1",
        },
        {
            "kind": "llm.request",
            "timestamp": "2026-09-05T10:01:00Z",
            "provider": "hist_dot_p",
            "status": "success",
        },
    ]
    with open(sessions_dir / "journal.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    status = gateway.get_provider_status("hist_dot_p", sessions_root=tmp_path / "sessions")
    assert status.runtime_succeeded_at_utc is not None
    assert status.live_verified is False

    # In ModelProvidersScreen, the dot for hist_dot_p must be ○ because live_verified is False
    screen = ModelProvidersScreen()
    screen._statuses_cache = [status]
    # Compose screen buttons
    btn_labels = []
    for st in screen._current_statuses():
        is_live = bool(getattr(st, "live_verified", False))
        dot = "● " if is_live else "○ "
        btn_labels.append(f"{dot}{st.label}")

    assert btn_labels == ["○ Historical Provider"]
    assert "●" not in btn_labels[0]

