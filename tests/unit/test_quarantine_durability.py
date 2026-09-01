"""Fail-closed quarantine durability regressions.

These tests fail against 8e5692b where quarantine was only attempted after
all rollback operations failed and OSError was swallowed, and where
_load_quarantined_providers swallowed malformed/oversized state.

The repaired transaction arms a durable quarantine BEFORE any credential
mutation and treats any existing but unreadable/corrupt quarantine file as
fail-closed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_debugger.application import provider_connections as pc
from agentic_debugger.application.provider_connections import (
    ProviderConnectionError,
    add_provider_config,
    get_provider_config,
    update_provider_config,
    credential_source_for,
    resolve_runtime_credential,
    refresh_provider_catalog,
    save_provider_configurations,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))
from fake_provider_server import FakeProviderServer, catalog_payload

@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    store: dict[str, str] = {}
    monkeypatch.setattr(pc, "save_secure_credential", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in store)
    monkeypatch.setattr(pc, "delete_secure_credential", lambda k: store.pop(k, None) is not None)
    # ensure clean quarantine state
    pc._QUARANTINED_PROVIDERS.clear()
    try:
        pc.provider_quarantine_path().unlink(missing_ok=True)
    except OSError:
        pass
    pc.clear_all_session_keys()
    # expose store for tests via monkeypatch attribute
    monkeypatch.setattr(pc, "_test_store", store, raising=False)
    yield
    pc._QUARANTINED_PROVIDERS.clear()
    pc.clear_all_session_keys()
    try:
        pc.provider_quarantine_path().unlink(missing_ok=True)
    except OSError:
        pass


def test_quarantine_write_failure_before_mutation_aborts_without_credential_change(monkeypatch: pytest.MonkeyPatch):
    """Force durable quarantine persistence to fail and attempt endpoint+api-key edit.

    Assert save_secure_credential(new key) was NEVER called, config unchanged,
    old credential unchanged, no request/discovery occurred.
    Fails against 8e5692b which had no pre-arm.
    """
    # seed original coherent pair
    seed = add_provider_config(
        name="Quarantine Arm Target",
        base_url="https://old.example/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        api_key="old-secret-111",
        provider_id="quarantine_arm_target",
    )
    assert get_provider_config("quarantine_arm_target").base_url == "https://old.example/v1"
    assert resolve_runtime_credential("quarantine_arm_target") == "old-secret-111"

    # track new-key save attempts
    new_key_calls: list[str] = []
    orig_save = pc.save_secure_credential
    def tracking_save(kind: str, val: str):
        if val == "new-secret-999":
            new_key_calls.append(val)
        return orig_save(kind, val)
    monkeypatch.setattr(pc, "save_secure_credential", tracking_save)

    # force durable quarantine arm to fail
    def failing_write(providers):
        raise ProviderConnectionError("injected quarantine arm failure")
    monkeypatch.setattr(pc, "_write_quarantine_state", failing_write)

    # also track any discovery/request
    requested: list[str] = []
    orig_request = pc.request_json
    def tracking_request(method, url, **kw):
        requested.append(url)
        return orig_request(method, url, **kw)
    monkeypatch.setattr(pc, "request_json", tracking_request)

    with pytest.raises(ProviderConnectionError, match="quarantine could not be armed"):
        update_provider_config(
            provider_id="quarantine_arm_target",
            base_url="https://new.example/v1",
            api_key="new-secret-999",
        )

    assert new_key_calls == [], "save_secure_credential(new key) must not be called when durable arm fails"
    assert get_provider_config("quarantine_arm_target").base_url == "https://old.example/v1"
    assert resolve_runtime_credential("quarantine_arm_target") == "old-secret-111"
    assert requested == [], "no discovery/request must occur after arm failure"
    # error must be credential-free
    try:
        update_provider_config(provider_id="quarantine_arm_target", base_url="https://new.example/v1", api_key="new-secret-999")
    except ProviderConnectionError as exc:
        assert "new-secret-999" not in str(exc)
        assert "old-secret-111" not in str(exc)


def test_catastrophic_rollback_restart_remains_blocked_zero_requests(monkeypatch: pytest.MonkeyPatch):
    """Prove final design cannot reach 8e5692b unsafe state.

    Transaction either aborts before mutation or has already-durable block
    before mutation. After fresh-process simulation: zero credential
    resolution, zero HTTP requests for any indeterminate association.
    """
    # seed
    add_provider_config(
        name="Catastrophic Prov",
        base_url="https://old.example/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        api_key="old-key-123",
        provider_id="catastrophic_prov",
    )
    # instrument order: _write should happen before save_secure_credential(new)
    order: list[str] = []
    orig_write = pc._write_quarantine_state
    orig_save = pc.save_secure_credential
    def recording_write(providers):
        order.append("write_quarantine")
        return orig_write(providers)
    def recording_save(kind, val):
        order.append(f"save:{val[:8]}")
        return orig_save(kind, val)
    monkeypatch.setattr(pc, "_write_quarantine_state", recording_write)
    monkeypatch.setattr(pc, "save_secure_credential", recording_save)

    # force catastrophic: new-key write succeeds, config fails, restore/delete fail
    call = 0
    def flaky_save(kind, val):
        nonlocal call
        call += 1
        if call == 1:
            # new key
            return orig_save(kind, val)
        return False
    # after first add, re-patch flaky for update
    monkeypatch.setattr(pc, "save_secure_credential", flaky_save)
    # need to keep write recording: wrap flaky with order tracking
    def flaky_save_with_order(kind, val):
        order.append(f"save:{val[:8]}")
        return flaky_save(kind, val)
    monkeypatch.setattr(pc, "save_secure_credential", flaky_save_with_order)
    monkeypatch.setattr(pc, "_write_quarantine_state", recording_write)
    monkeypatch.setattr(pc, "delete_secure_credential", lambda k: False)
    monkeypatch.setattr(pc, "save_provider_configurations", lambda configs: (_ for _ in ()).throw(ProviderConnectionError("provider configuration could not be written")))

    # need to restore write tracker for arm
    # arm happens inside update_provider_config -> quarantine_provider -> _write
    # So order should be write_quarantine before save:new-key
    with pytest.raises(ProviderConnectionError):
        update_provider_config(provider_id="catastrophic_prov", base_url="https://new.example/v1", api_key="new-key-999")

    # verify durable quarantine was armed before credential mutation
    # In our recording, first write should be arm, then save new-key
    # Find indices
    if "write_quarantine" in order and any("save:new-key" in x for x in order):
        w_idx = order.index("write_quarantine")
        s_idx = next(i for i, x in enumerate(order) if "new-key" in x)
        assert w_idx < s_idx, f"quarantine must be armed before credential mutation order={order}"
    else:
        # at least we have quarantine durably
        assert pc.provider_quarantine_path().exists(), "quarantine file must exist before mutation"

    # verify current process blocked
    assert credential_source_for("catastrophic_prov") is None
    assert resolve_runtime_credential("catastrophic_prov") is None

    # simulate fresh process: clear in-memory, keep durable file and store
    pc._QUARANTINED_PROVIDERS.clear()
    pc.clear_all_session_keys()
    # restore real save/load for fresh simulation (store still has new-key residue)
    # Use original store-backed functions but keep quarantine file
    store = {}
    # Re-create store with residue to simulate OS secure-store still containing new credential
    # In this test, the OS store still has new-key because delete failed
    # We need to simulate that: set store to have new-key
    # We have lost store reference due to monkeypatch; reconstruct from isolation fixture's store is not accessible, so we directly set via mock
    # We'll just ensure has_secure_credential returns True to simulate residue
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k == "catastrophic_prov")
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: "new-key-999" if k == "catastrophic_prov" else None)
    # but _read still returns quarantine
    assert pc.provider_quarantine_path().exists()
    # after restart, still blocked
    assert pc.is_provider_quarantined("catastrophic_prov") is True
    assert credential_source_for("catastrophic_prov") is None
    assert resolve_runtime_credential("catastrophic_prov") is None

    # zero HTTP requests after restart
    def responder(req):
        return (200, catalog_payload(["model-1"]))
    with FakeProviderServer(responder) as server:
        with pytest.raises(ProviderConnectionError, match="requires recovery"):
            refresh_provider_catalog("catastrophic_prov")
        assert server.request_count == 0, "zero HTTP requests for indeterminate association after restart"
        assert resolve_runtime_credential("catastrophic_prov") is None


def test_malformed_quarantine_file_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """Seed an existing malformed durable quarantine file and simulate fresh process.

    Assert credential resolution / catalog refresh fails closed rather than
    treating the file as empty. Fails against 8e5692b.
    """
    qp = pc.provider_quarantine_path()
    qp.parent.mkdir(parents=True, exist_ok=True)
    qp.write_text("{ not json }", encoding="utf-8")
    # seed a provider config and credential (in-memory store)
    store = {}
    monkeypatch.setattr(pc, "save_secure_credential", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in store)
    # need config for provider
    save_provider_configurations([pc.ProviderConfig(provider_id="malformed_prov", name="Malformed", base_url="https://api.test.com/v1", api_format=pc.PROTOCOL_CHAT_COMPLETIONS)])
    store["malformed_prov"] = "secret-123"
    pc._QUARANTINED_PROVIDERS.clear()

    # credential resolution must fail closed (raise), not return saved
    with pytest.raises(ProviderConnectionError, match="quarantine state"):
        credential_source_for("malformed_prov")
    with pytest.raises(ProviderConnectionError, match="quarantine state"):
        resolve_runtime_credential("malformed_prov")
    # ensure secret not in error
    try:
        credential_source_for("malformed_prov")
    except ProviderConnectionError as exc:
        assert "secret-123" not in str(exc)

    # catalog refresh must fail before HTTP
    def responder(req):
        return (200, catalog_payload(["m1"]))
    with FakeProviderServer(responder) as server:
        with pytest.raises(ProviderConnectionError, match="quarantine state"):
            refresh_provider_catalog("malformed_prov", credential="secret-123")
        assert server.request_count == 0


def test_oversized_and_unreadable_quarantine_state_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """Cover oversized and unreadable variants; neither must fail open."""
    qp = pc.provider_quarantine_path()
    qp.parent.mkdir(parents=True, exist_ok=True)

    # oversized
    oversized = "x" * (pc._MAX_QUARANTINE_FILE_BYTES + 1)
    qp.write_text(oversized, encoding="utf-8")
    store = {}
    monkeypatch.setattr(pc, "save_secure_credential", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in store)
    save_provider_configurations([pc.ProviderConfig(provider_id="oversized_prov", name="Oversized", base_url="https://api.test.com/v1", api_format=pc.PROTOCOL_CHAT_COMPLETIONS)])
    store["oversized_prov"] = "secret-456"
    pc._QUARANTINED_PROVIDERS.clear()
    with pytest.raises(ProviderConnectionError, match="exceeded its bound"):
        credential_source_for("oversized_prov")
    with pytest.raises(ProviderConnectionError, match="exceeded its bound"):
        resolve_runtime_credential("oversized_prov")

    # invalid schema version
    qp.write_text(json.dumps({"schema_version": "wrong-version", "providers": []}), encoding="utf-8")
    with pytest.raises(ProviderConnectionError, match="invalid"):
        credential_source_for("oversized_prov")

    # providers not a list
    qp.write_text(json.dumps({"schema_version": pc._QUARANTINE_SCHEMA_VERSION, "providers": "not-a-list"}), encoding="utf-8")
    with pytest.raises(ProviderConnectionError, match="invalid"):
        credential_source_for("oversized_prov")

    # unreadable (simulated by mocking _read to raise)
    qp.write_text(json.dumps({"schema_version": pc._QUARANTINE_SCHEMA_VERSION, "providers": []}), encoding="utf-8")
    def failing_read():
        raise ProviderConnectionError("provider credential quarantine state could not be read")
    monkeypatch.setattr(pc, "_read_quarantine_file", failing_read)
    with pytest.raises(ProviderConnectionError, match="could not be read"):
        credential_source_for("oversized_prov")
    with pytest.raises(ProviderConnectionError, match="could not be read"):
        resolve_runtime_credential("oversized_prov")

    # also ensure refresh fails before HTTP for unreadable
    def responder(req):
        return (200, catalog_payload(["m1"]))
    with FakeProviderServer(responder) as server:
        with pytest.raises(ProviderConnectionError):
            refresh_provider_catalog("oversized_prov", credential="secret-456")
        assert server.request_count == 0


def test_successful_save_clears_quarantine_and_permits_request(monkeypatch: pytest.MonkeyPatch):
    """Normal explicit save still commits coherent config+saved credential, clears
    quarantine, credential_source_for == saved, permits normal fake-provider request.
    """
    store = {}
    monkeypatch.setattr(pc, "save_secure_credential", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in store)
    monkeypatch.setattr(pc, "delete_secure_credential", lambda k: store.pop(k, None) is not None)

    def responder(req):
        if req["path"] == "/v1/models":
            return (200, catalog_payload(["recovered-model-1"]))
        return (404, {})

    with FakeProviderServer(responder) as server:
        cfg = add_provider_config(
            name="Recover Prov",
            base_url=server.base_url + "/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="recover_prov",
            api_key="recovered-new-key",
        )
        assert cfg.base_url == server.base_url + "/v1"
        assert credential_source_for("recover_prov") == pc.CREDENTIAL_SOURCE_SAVED
        assert resolve_runtime_credential("recover_prov") == "recovered-new-key"
        assert not pc.is_provider_quarantined("recover_prov")
        # quarantine file should not contain this provider
        if pc.provider_quarantine_path().exists():
            assert "recover_prov" not in pc.provider_quarantine_path().read_text(encoding="utf-8")

        snap = refresh_provider_catalog("recover_prov")
        assert server.request_count == 1
        assert snap.models[0].model_id == "recovered-model-1"
        assert server.requests[0]["authorization"] == "Bearer recovered-new-key"
        # secret must not be in any error/comparison
        assert "recovered-new-key" not in json.dumps(snap.to_mapping())
