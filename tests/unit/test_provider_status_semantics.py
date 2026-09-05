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
            "auth_mode": "none",
            "endpoint_contract": "generic",
            "api_format": "chat_completions",
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

    # Status snapshot derives runtime success without replacing current headline
    status = gateway.get_provider_status("my_prov", sessions_root=tmp_path / "sessions")
    assert status.runtime_succeeded_at_utc == "2026-09-05T09:01:00Z"
    assert status.summary_headline == "Configured · loopback"


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
            "auth_mode": "none",
            "endpoint_contract": "generic",
            "api_format": "chat_completions",
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
        is_live = bool(getattr(st, "connected", False))
        dot = "● " if is_live else "○ "
        btn_labels.append(f"{dot}{st.label}")

    assert btn_labels == ["○ Historical Provider"]
    assert "●" not in btn_labels[0]


def test_runtime_success_does_not_mask_missing_credentials_headline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 7: Historical runtime success does not mask missing credentials in summary_headline."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    # Provider requiring credentials, but none entered
    add_provider_config(
        name="Bearer Prov",
        base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="bearer_nocred_p",
        auth_mode=AUTH_BEARER,
        transport_profile=TRANSPORT_GENERIC,
    )

    # Write a historical session journal
    sessions_dir = tmp_path / "sessions" / "s_cred"
    sessions_dir.mkdir(parents=True)
    events = [
        {
            "kind": "model.configured",
            "timestamp": "2026-09-05T10:00:00Z",
            "provider": "bearer_nocred_p",
            "endpoint": "https://api.example.com/v1",
            "auth_mode": "bearer",
            "endpoint_contract": TRANSPORT_GENERIC,
            "api_format": PROTOCOL_CHAT_COMPLETIONS,
            "model": "m1",
        },
        {
            "kind": "llm.request",
            "timestamp": "2026-09-05T10:01:00Z",
            "provider": "bearer_nocred_p",
            "status": "success",
        },
    ]
    with open(sessions_dir / "journal.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    status = gateway.get_provider_status("bearer_nocred_p", sessions_root=tmp_path / "sessions")
    assert status.runtime_succeeded_at_utc == "2026-09-05T10:01:00Z"
    # The headline MUST reflect missing credentials, NOT historical runtime success
    assert status.summary_headline == "Configured · no credential"
    assert status.is_provider_ready is False


def test_ui_renders_separate_historical_timestamps() -> None:
    """Requirement 8: UI renders Live verified and Runtime success as distinct timestamps."""
    from types import SimpleNamespace
    from agentic_debugger.ui.screens import ModelProvidersScreen
    from agentic_debugger.application.model_gateway import ProviderStatusSnapshot

    screen = ModelProvidersScreen()
    status = ProviderStatusSnapshot(
        provider_id="dual_ts_p",
        label="Dual Timestamp Provider",
        base_url="http://127.0.0.1:8000",
        endpoint_contract=TRANSPORT_GENERIC,
        auth_mode=AUTH_NONE,
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        is_configured=True,
        is_enabled=True,
        is_quarantined=False,
        credential_ready=True,
        credential_source=None,
        is_provider_ready=True,
        provider_readiness_reason="Ready",
        live_verified=True,
        live_verified_at_utc="2026-09-05T08:15:30Z",
        runtime_succeeded_at_utc="2026-09-05T09:45:00Z",
        catalog_model_count=1,
    )

    screen._statuses_cache = [status]
    screen._selected_index = 0

    captured_updates: dict = {}
    class _MockWidget:
        def __init__(self, wid: str):
            self.wid = wid
            self.display = True
            self.label = ""
        def update(self, val):
            captured_updates[self.wid] = str(val)
        def add_class(self, *a):
            pass
        def remove_class(self, *a):
            pass

    widgets = {
        "#provider-select-dual_ts_p": _MockWidget("#provider-select-dual_ts_p"),
        "#provider-panel-dual_ts_p": _MockWidget("#provider-panel-dual_ts_p"),
        "#provider-summary-dual_ts_p": _MockWidget("#provider-summary-dual_ts_p"),
        "#provider-refresh-dual_ts_p": _MockWidget("#provider-refresh-dual_ts_p"),
        "#provider-models-helper-dual_ts_p": _MockWidget("#provider-models-helper-dual_ts_p"),
    }
    screen.query_one = lambda sel, *args, **kwargs: widgets.setdefault(sel, _MockWidget(sel))

    screen.render_state()

    refresh_text = captured_updates.get("#provider-refresh-dual_ts_p", "")
    assert "Live verified  2026-09-05 08:15:30 UTC" in refresh_text
    assert "Runtime success 2026-09-05 09:45:00 UTC" in refresh_text


def test_list_provider_statuses_produces_degraded_snapshot_on_evaluation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 9: Status evaluation failure produces degraded snapshot; provider never disappears."""
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    add_provider_config(
        name="Healthy Provider",
        base_url="http://127.0.0.1:8001",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="healthy_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )
    add_provider_config(
        name="Faulty Provider",
        base_url="http://127.0.0.1:8002",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="faulty_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )

    # Monkeypatch inspect_last_runtime_success to raise an error for faulty_p
    orig_inspect = gateway.inspect_last_runtime_success
    def _faulty_inspect(pid, *args, **kwargs):
        if pid == "faulty_p":
            raise RuntimeError("disk I/O error during journal scan")
        return orig_inspect(pid, *args, **kwargs)

    monkeypatch.setattr(gateway, "inspect_last_runtime_success", _faulty_inspect)

    statuses = gateway.list_provider_statuses(history_root=tmp_path / "sessions")
    status_map = {s.provider_id: s for s in statuses}

    # Both providers must be present!
    assert "healthy_p" in status_map
    assert "faulty_p" in status_map

    healthy_status = status_map["healthy_p"]
    assert healthy_status.is_provider_ready is True

    faulty_status = status_map["faulty_p"]
    assert faulty_status.is_provider_ready is False
    assert "Status evaluation error" in (faulty_status.provider_readiness_reason or "")
    assert "RuntimeError" in (faulty_status.provider_readiness_reason or "")


def test_historical_live_verified_does_not_mask_missing_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 6: Live verified probe timestamp is preserved after credential deletion, but connected is False, headline reports no credential, and sidebar dot is ○."""
    from agentic_debugger.ui.screens import ModelProvidersScreen
    from agentic_debugger.application.provider_connections import (
        save_cached_catalog,
        delete_secure_credential,
    )

    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path))
    gateway = ModelGateway(config_root=tmp_path)

    # 1. Configure provider with credentials and record live verified probe in gateway
    add_provider_config(
        name="Live Probe Prov",
        base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="live_probe_p",
        auth_mode=AUTH_BEARER,
        api_key="sk-test-valid-key",
        transport_profile=TRANSPORT_GENERIC,
    )
    from agentic_debugger.application.model_gateway import provider_runtime_identity
    cfg = get_provider_config("live_probe_p")
    cur_id = provider_runtime_identity(cfg)
    gateway._live_probe_results["live_probe_p"] = {
        "verified": True,
        "timestamp": "2026-09-05T11:00:00Z",
        "runtime_identity": cur_id,
    }

    status = gateway.get_provider_status("live_probe_p")
    assert status.live_verified is True
    assert status.live_verified_at_utc == "2026-09-05T11:00:00Z"
    assert status.credential_ready is True
    assert status.connected is True
    assert "Live verified" in status.summary_headline

    # 2. Delete the credential (now no credential available)
    delete_secure_credential("live_probe_p")

    status_no_cred = gateway.get_provider_status("live_probe_p")
    # Live verified timestamp is preserved historically
    assert status_no_cred.live_verified is True
    assert status_no_cred.live_verified_at_utc == "2026-09-05T11:00:00Z"
    # Current readiness is deficient
    assert status_no_cred.credential_ready is False
    assert status_no_cred.connected is False
    # Headline reports current deficit rather than Live verified
    assert status_no_cred.summary_headline == "Configured · no credential"

    # UI renders hollow dot ○ rather than solid dot ●
    screen = ModelProvidersScreen()
    screen._statuses_cache = [status_no_cred]
    for st in screen._current_statuses():
        is_live = bool(getattr(st, "connected", False))
        dot = "● " if is_live else "○ "
        label = f"{dot}{st.label}"
        assert label == "○ Live Probe Prov"



