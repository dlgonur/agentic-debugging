"""V2-04 Candidate-21 REPAIR acceptance tests.

Closes the FirstMate review findings against Candidate 20:

- F1: product probe / catalog refresh materialize through the vault lease
  (``resolve_runtime_credential`` sentinel is NEVER hit); explicit
  lease value at the fake endpoint; auth_none sends nothing;
  missing/quarantined fail safely before HTTP; no secret in
  errors/status/snapshots.
- F2: the session credential authority is fixed at the SessionLaunch
  boundary (issued private channel outranks durable state after
  SESSION_STARTED).  (The full lifecycle regression lives in
  ``tests/integration/test_credential_vault_product_path.py``.)
- F3: CredentialBinding structural semantic-shape validation (mandatory
  provider authority, canonical source refs, coherent endpoint_bound,
  identical from_mapping enforcement, unconditional resolve-time
  corroboration incl. auth mode and none/external order).
- F4: session_memory vs forwarded_session are DISTINCT authorities;
  explicit binding resolution is source-faithful (no silent switching).
- F5: consumable cli_auth_store bindings pin the safe location
  fingerprint; A->B path drift fails closed.
- F6: transport materialization never converts an auth-required vault
  failure into "no credential needed"; direct routes fail closed before
  transport construction.
- F7: a disabled provider remains CONFIGURED (separate is_enabled fact).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))

from fake_provider_server import FakeProviderServer, scripted_chat_completion  # noqa: E402

from agentic_debugger.application import provider_connections as pc  # noqa: E402
from agentic_debugger.application.credential_vault import (  # noqa: E402
    CREDENTIAL_SOURCE_EXTERNAL_CLI,
    CREDENTIAL_SOURCE_FORWARDED_SESSION,
    CREDENTIAL_SOURCE_NONE,
    CREDENTIAL_SOURCE_SESSION_MEMORY,
    CredentialBinding,
    CredentialUnavailableError,
    CredentialVault,
    CredentialVaultError,
    StaleCredentialBindingError,
)
from agentic_debugger.application.model_gateway import (  # noqa: E402
    CatalogProbeError,
    ModelGateway,
    provider_runtime_identity,
)

SECRET_A = "r21-synthetic-credential-alpha-not-real"
SECRET_B = "r21-synthetic-credential-beta-not-real"
DIRECTIVE = '{"kind": "transition", "target_state": "Understand", "reason": "r"}'


@pytest.fixture(autouse=True)
def _hermetic_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(tmp_path / "c.json")
    )
    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(tmp_path / "q.json")
    )
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path / "config-dir"))
    monkeypatch.setattr("agentic_debugger.application.provider_catalog.catalog_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr("agentic_debugger.application.provider_connections.catalog_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr("agentic_debugger.application.provider_credentials.opencode_auth_store_path", lambda: tmp_path / "missing-auth.json")
    monkeypatch.setattr("agentic_debugger.application.provider_connections.opencode_auth_store_path", lambda: tmp_path / "missing-auth.json")
    store: dict[str, str] = {}
    monkeypatch.setattr("agentic_debugger.application.provider_credentials.save_secure_credential", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr("agentic_debugger.application.provider_connections.save_secure_credential", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr("agentic_debugger.application.provider_credentials.load_secure_credential", lambda k: store.get(k))
    monkeypatch.setattr("agentic_debugger.application.provider_connections.load_secure_credential", lambda k: store.get(k))
    monkeypatch.setattr("agentic_debugger.application.provider_credentials.has_secure_credential", lambda k: k in store)
    monkeypatch.setattr("agentic_debugger.application.provider_connections.has_secure_credential", lambda k: k in store)
    monkeypatch.setattr("agentic_debugger.application.provider_credentials.delete_secure_credential", lambda k: store.pop(k, None) is not None)
    monkeypatch.setattr("agentic_debugger.application.provider_connections.delete_secure_credential", lambda k: store.pop(k, None) is not None)
    for var in (
        "COMMAND_CODE_API_KEY",
        "OPENCODE_API_KEY",
        "OLLAMA_API_KEY",
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
        "AGENTIC_DEBUGGER_OLLAMA_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    pc.clear_all_session_keys()
    # The quarantine overlay is process-module state: isolate it per test.
    pc._QUARANTINED_PROVIDERS.clear()
    yield store
    pc.clear_all_session_keys()
    pc._QUARANTINED_PROVIDERS.clear()


def _issue_channel(monkeypatch: pytest.MonkeyPatch, provider_id: str, value: str) -> str:
    """Simulate the UI-issued hop: channel VALUE + safe issuance authority."""
    channel = pc.provider_session_credential_variable(provider_id)
    authority_var = pc.provider_session_credential_authority_variable(provider_id)
    cfg = pc.get_provider_config(provider_id)
    identity = provider_runtime_identity(cfg)
    assert identity is not None
    monkeypatch.setenv(channel, value)
    monkeypatch.setenv(authority_var, identity)
    return channel


def _configure_commandcode(base_url: str = "https://api.commandcode.ai/provider/v1") -> None:
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url=base_url,
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
    )


def _http_responder(request: dict):
    """Catalog GET /models for probe/refresh; scripted POST otherwise."""
    from fake_provider_server import catalog_payload

    if request.get("method") == "GET":
        return 200, catalog_payload(["fake-model-r21"])
    return 200, scripted_chat_completion(DIRECTIVE)


# ---------------------------------------------------------------------------
# FINDING 1 — product probe/catalog refresh materialize through the vault
# ---------------------------------------------------------------------------


class TestFinding1ProductHttpThroughVault:
    def _sentinel(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        calls = {"count": 0}

        def _forbidden(kind: str) -> None:
            calls["count"] += 1
            raise AssertionError(
                "resolve_runtime_credential must never be called on the "
                "ModelGateway product path"
            )

        monkeypatch.setattr("agentic_debugger.application.provider_credentials.resolve_runtime_credential", _forbidden)
        monkeypatch.setattr("agentic_debugger.application.provider_connections.resolve_runtime_credential", _forbidden)
        return calls

    def test_refresh_catalog_uses_vault_lease_never_ambient(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = self._sentinel(monkeypatch)  # A
        with FakeProviderServer(_http_responder) as server:
            _configure_commandcode(base_url=server.base_url)
            _hermetic_vault["commandcode_goat"] = SECRET_A
            snapshot = ModelGateway().refresh_catalog("commandcode_goat")
        assert snapshot.models
        assert sentinel["count"] == 0  # A: sentinel NEVER called
        request = server.requests[0]
        assert request["authorization"] == f"Bearer {SECRET_A}"  # C: lease value

    def test_probe_reachability_uses_vault_lease_never_ambient(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = self._sentinel(monkeypatch)  # B
        with FakeProviderServer(_http_responder) as server:
            _configure_commandcode(base_url=server.base_url)
            _hermetic_vault["commandcode_goat"] = SECRET_A
            result = ModelGateway().probe_reachability("commandcode_goat")
        assert result["ok"] is True
        assert sentinel["count"] == 0  # B
        assert server.requests[0]["authorization"] == f"Bearer {SECRET_A}"  # C

    def test_auth_none_sends_no_credential_and_no_lease(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = self._sentinel(monkeypatch)  # D: even the sentinel must not run
        pc.add_provider_config(
            name="Loopback",
            base_url="http://127.0.0.1:59991/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="loopback_r21",
            auth_mode=pc.AUTH_NONE,
        )
        with FakeProviderServer(_http_responder) as server:
            cfg = pc.get_provider_config("loopback_r21")
            assert cfg is not None
            updated = pc.update_provider_config(
                "loopback_r21",
                base_url=server.base_url,
            )
            assert updated is not None
            gateway = ModelGateway()
            assert gateway._probe_credential("loopback_r21") is None
            probe = gateway.probe_reachability("loopback_r21")
            assert probe["ok"] is True
            snapshot = gateway.refresh_catalog("loopback_r21")
            assert snapshot.models
        assert sentinel["count"] == 0
        assert server.requests[0]["authorization"] is None  # D: no credential

    def test_missing_credential_fails_safely_before_http(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with FakeProviderServer(_http_responder) as server:
            _configure_commandcode(base_url=server.base_url)  # E: no credential
            gateway = ModelGateway()
            probe = gateway.probe_reachability("commandcode_goat")
            assert probe["ok"] is False
            assert "Credential unavailable" in probe["reason"]
            with pytest.raises(CatalogProbeError):
                gateway.refresh_catalog("commandcode_goat")
        assert server.request_count == 0  # E: failed BEFORE any HTTP

    def test_quarantined_credential_fails_safely_before_http(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        pc.quarantine_provider("commandcode_goat")
        with FakeProviderServer(_http_responder) as server:
            gateway = ModelGateway()
            probe = gateway.probe_reachability("commandcode_goat")
            assert probe["ok"] is False
            assert "recovery" in probe["reason"]
            with pytest.raises(CatalogProbeError):
                gateway.refresh_catalog("commandcode_goat")
        assert server.request_count == 0  # E

    def test_secret_absent_from_probe_errors_status_and_snapshot(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_commandcode()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        pc.quarantine_provider("commandcode_goat")  # F
        import dataclasses

        gateway = ModelGateway()
        probe = gateway.probe_reachability("commandcode_goat")
        assert SECRET_A not in json.dumps(probe)
        status = gateway.get_provider_status("commandcode_goat")
        assert SECRET_A not in json.dumps(dataclasses.asdict(status))
        with pytest.raises(CatalogProbeError) as excinfo:
            gateway.refresh_catalog("commandcode_goat")
        assert SECRET_A not in str(excinfo.value)  # F




# ---------------------------------------------------------------------------
# FINDING 2 — session authority fixed at the launch boundary (unit half;
# the lifecycle regression lives in the integration file)
# ---------------------------------------------------------------------------


class TestFinding2SessionAuthority:
    def test_issued_channel_outranks_durable_state_after_session_start(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_commandcode()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        # The real worker receives the UI-issued hop (channel + issuance
        # authority) in its environment at spawn; simulate exactly that.
        channel = _issue_channel(monkeypatch, "commandcode_goat", SECRET_A)
        vault = CredentialVault.default()
        authority = vault.session_authority("commandcode_goat")
        assert authority is not None
        assert authority.source_kind == CREDENTIAL_SOURCE_FORWARDED_SESSION
        # Durable slot replaced WITHOUT changing the runtime identity: the
        # session authority still materializes the ISSUED value.
        _hermetic_vault["commandcode_goat"] = SECRET_B
        assert (
            vault.transport_materialization(
                "commandcode_goat",
                route="direct_api",
                credential_binding=authority,
            )
            == {channel: SECRET_A}
        )
        # A NEW session (no issued channel in its process) resolves the
        # new durable state.
        monkeypatch.delenv(channel, raising=False)
        fresh = vault.session_authority("commandcode_goat")
        assert fresh is not None
        assert (
            vault.resolve_lease(fresh).reveal() == SECRET_B
        )

    def test_session_authority_fresh_ladder_when_no_channel_issued(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        authority = CredentialVault.default().session_authority("commandcode_goat")
        assert authority is not None
        assert authority.source_kind == pc.CREDENTIAL_SOURCE_SAVED


# ---------------------------------------------------------------------------
# FINDING 3 — structural semantic-shape validation
# ---------------------------------------------------------------------------


class TestFinding3StructuralBindings:
    def _authority(self) -> str:
        from agentic_debugger.application.model_gateway import (
            provider_runtime_identity,
        )

        cfg = pc.get_provider_config("commandcode_goat")
        if cfg is None:
            _configure_commandcode()
            cfg = pc.get_provider_config("commandcode_goat")
        assert cfg is not None
        identity = provider_runtime_identity(cfg)
        assert identity is not None
        return identity

    def test_fabricated_binding_without_authority_is_rejected(self) -> None:
        with pytest.raises(CredentialVaultError):
            CredentialBinding(
                provider_id="commandcode_goat",
                source_kind=pc.CREDENTIAL_SOURCE_SAVED,
                source_ref=pc.credential_slot_name("commandcode_goat"),
                auth_mode="bearer",
                provider_authority=None,
                endpoint_bound=False,
            )

    def test_fabricated_binding_with_wrong_slot_ref_is_rejected(self) -> None:
        secret_like_ref = "some-synthetic-undetected-secret-value-xyz"
        with pytest.raises(Exception) as excinfo:
            CredentialBinding(
                provider_id="commandcode_goat",
                source_kind=pc.CREDENTIAL_SOURCE_SAVED,
                source_ref=secret_like_ref,
                auth_mode="bearer",
                provider_authority=self._authority(),
                endpoint_bound=False,
            )
        assert "canonical" in str(excinfo.value)

    def test_contradictory_endpoint_bound_is_rejected(self) -> None:
        with pytest.raises(Exception) as excinfo:
            CredentialBinding(
                provider_id="commandcode_goat",
                source_kind=pc.CREDENTIAL_SOURCE_SAVED,
                source_ref=pc.credential_slot_name("commandcode_goat"),
                auth_mode="bearer",
                provider_authority=self._authority(),
                endpoint_bound=True,
            )
        assert "endpoint_bound" in str(excinfo.value)

    def test_invalid_auth_mode_is_rejected(self) -> None:
        with pytest.raises(Exception) as excinfo:
            CredentialBinding(
                provider_id="commandcode_goat",
                source_kind=pc.CREDENTIAL_SOURCE_SAVED,
                source_ref=pc.credential_slot_name("commandcode_goat"),
                auth_mode="bearer2",
                provider_authority=self._authority(),
                endpoint_bound=False,
            )
        assert "auth_mode" in str(excinfo.value)

    def test_from_mapping_enforces_identical_invariants(self) -> None:
        impossible = {
            "provider_id": "commandcode_goat",
            "source_kind": pc.CREDENTIAL_SOURCE_SAVED,
            "source_ref": "made-up-ref-not-canonical",
            "auth_mode": "bearer",
            "provider_authority": self._authority(),
            "endpoint_bound": False,
        }
        with pytest.raises(Exception) as excinfo:
            CredentialBinding.from_mapping(impossible)
        assert "canonical" in str(excinfo.value)
        missing_authority = dict(impossible, source_ref=pc.credential_slot_name("commandcode_goat"), provider_authority=None)
        with pytest.raises(Exception):
            CredentialBinding.from_mapping(missing_authority)

    def test_resolve_lease_requires_exact_authority_and_auth_corroboration(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        binding = CredentialVault.default().safe_binding("commandcode_goat")
        assert binding is not None
        # Endpoint drift (identity change) -> stale.
        pc.update_provider_config(
            "commandcode_goat", base_url="https://moved-r21b.example.invalid/v1", api_key=SECRET_B
        )
        with pytest.raises(StaleCredentialBindingError):
            CredentialVault.default().resolve_lease(binding)
        # Auth-mode drift with otherwise identical authority -> stale.
        binding2 = CredentialVault.default().safe_binding("commandcode_goat")
        assert binding2 is not None
        drifted = CredentialBinding(
            provider_id=binding2.provider_id,
            source_kind=binding2.source_kind,
            source_ref=binding2.source_ref,
            auth_mode="anthropic" if binding2.auth_mode != "anthropic" else "bearer",
            provider_authority=binding2.provider_authority,
            endpoint_bound=binding2.endpoint_bound,
        )
        if drifted.auth_mode != binding2.auth_mode:
            with pytest.raises(StaleCredentialBindingError):
                CredentialVault.default().resolve_lease(drifted)

    def test_none_and_external_bindings_still_corroborate_authority(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pc.add_provider_config(
            name="Loopback",
            base_url="http://127.0.0.1:59992/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="loopback_r21c",
            auth_mode=pc.AUTH_NONE,
        )
        binding = CredentialVault.default().safe_binding("loopback_r21c")
        assert binding is not None
        assert binding.source_kind == CREDENTIAL_SOURCE_NONE
        pc.update_provider_config(
            "loopback_r21c", base_url="http://127.0.0.1:59993/v1"
        )
        # A stale no-auth binding must not certify the new configuration.
        with pytest.raises(StaleCredentialBindingError):
            CredentialVault.default().resolve_lease(binding)

    def test_minted_forwarded_session_binding_is_canonical(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_commandcode()
        channel = _issue_channel(monkeypatch, "commandcode_goat", SECRET_A)
        binding = CredentialVault.default().session_authority("commandcode_goat")
        assert binding is not None
        assert binding.source_kind == CREDENTIAL_SOURCE_FORWARDED_SESSION
        assert binding.source_ref == channel
        assert binding.endpoint_bound is False
        # Round trip through from_mapping stays valid and identical.
        restored = CredentialBinding.from_mapping(binding.to_mapping())
        assert restored.fingerprint() == binding.fingerprint()


# ---------------------------------------------------------------------------
# FINDING 4 — session_memory vs forwarded_session are distinct authorities
# ---------------------------------------------------------------------------


class TestFinding4SourceFaithfulResolution:
    def test_memory_binding_never_switches_to_forwarded(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_commandcode()
        pc.set_session_key("commandcode_goat", SECRET_A)  # A. memory A
        binding = CredentialVault.default().safe_binding("commandcode_goat")
        assert binding is not None
        assert binding.source_kind == CREDENTIAL_SOURCE_SESSION_MEMORY
        assert binding.source_ref is None
        # B/C. memory removed; forwarded B appears.
        pc.clear_session_key("commandcode_goat")
        channel = pc.provider_session_credential_variable("commandcode_goat")
        monkeypatch.setenv(channel, SECRET_B)
        # D. resolve(binding A) must FAIL, not return B.
        with pytest.raises(CredentialUnavailableError):
            CredentialVault.default().resolve_lease(binding)

    def test_forwarded_binding_never_switches_to_memory(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_commandcode()
        channel = _issue_channel(monkeypatch, "commandcode_goat", SECRET_A)  # A.
        binding = CredentialVault.default().safe_binding("commandcode_goat")
        assert binding is not None
        assert binding.source_kind == CREDENTIAL_SOURCE_FORWARDED_SESSION
        # B/C. forwarded removed; memory B appears.
        monkeypatch.delenv(channel, raising=False)
        pc.set_session_key("commandcode_goat", SECRET_B)
        # D. resolve(binding A) must FAIL, not return B.
        with pytest.raises(CredentialUnavailableError):
            CredentialVault.default().resolve_lease(binding)
        # A newly minted binding selects the current winner by policy.
        fresh = CredentialVault.default().safe_binding("commandcode_goat")
        assert fresh is not None
        assert fresh.source_kind == CREDENTIAL_SOURCE_SESSION_MEMORY
        assert CredentialVault.default().resolve_lease(fresh).reveal() == SECRET_B


# ---------------------------------------------------------------------------
# FINDING 5 — consumable cli_auth_store location pinning
# ---------------------------------------------------------------------------


class TestFinding5CliAuthStoreLocationPinning:
    def test_binding_pins_location_and_path_drift_fails_closed(
        self, _hermetic_vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only the OpenCode historical contract consumes the CLI auth
        # store; the canonical endpoint binding must stay valid, so the
        # provider keeps its canonical base URL (no HTTP involved here).
        pc.add_provider_config(
            name="OpenCode Go R21",
            base_url="https://opencode.ai/zen/go/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="opencode_r21",
            transport_profile=pc.TRANSPORT_OPENCODE_GO,
        )
        store_a = tmp_path / "auth-store-a"
        store_a.mkdir()
        (store_a / "auth.json").write_text(
            json.dumps({"opencode-go": {"type": "api", "key": SECRET_A}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("agentic_debugger.application.provider_credentials.opencode_auth_store_path", lambda: store_a / "auth.json")
        monkeypatch.setattr("agentic_debugger.application.provider_connections.opencode_auth_store_path", lambda: store_a / "auth.json")
        vault = CredentialVault.default()
        binding = vault.safe_binding("opencode_r21")
        assert binding is not None
        assert binding.source_kind == pc.CREDENTIAL_SOURCE_CLI_AUTH_STORE
        # The safe location fingerprint: 64-hex, path-derived only.
        assert len(binding.source_ref) == 64
        assert SECRET_A not in binding.source_ref
        assert str(store_a) not in binding.source_ref
        assert vault.resolve_lease(binding).reveal() == SECRET_A

        # A→B drift: the authority moved to a different location.
        store_b = tmp_path / "auth-store-b"
        store_b.mkdir()
        (store_b / "auth.json").write_text(
            json.dumps({"opencode-go": {"type": "api", "key": SECRET_B}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("agentic_debugger.application.provider_credentials.opencode_auth_store_path", lambda: store_b / "auth.json")
        monkeypatch.setattr("agentic_debugger.application.provider_connections.opencode_auth_store_path", lambda: store_b / "auth.json")
        with pytest.raises(CredentialUnavailableError):
            vault.resolve_lease(binding)
        # Same location again: the named authority resolves again.
        monkeypatch.setattr("agentic_debugger.application.provider_credentials.opencode_auth_store_path", lambda: store_a / "auth.json")
        monkeypatch.setattr("agentic_debugger.application.provider_connections.opencode_auth_store_path", lambda: store_a / "auth.json")
        assert vault.resolve_lease(binding).reveal() == SECRET_A


# ---------------------------------------------------------------------------
# FINDING 6 — transport materialization never swallows auth-required failures
# ---------------------------------------------------------------------------


class TestFinding6FailClosedMaterialization:
    def test_direct_route_missing_credential_fails_closed(self, _hermetic_vault) -> None:
        _configure_commandcode()  # no credential at all
        with pytest.raises(CredentialUnavailableError):
            CredentialVault.default().transport_materialization(
                "commandcode_goat", route="direct_api"
            )

    def test_direct_route_disappearing_source_fails_closed_race(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        vault = CredentialVault.default()
        # 1. preflight/readiness says direct credential ready.
        assert vault.readiness("commandcode_goat").credential_ready is True
        # 2. bound source disappears before transport materialization.
        del _hermetic_vault["commandcode_goat"]
        # 3. transport creation fails closed (no "no credential needed" env).
        with pytest.raises(CredentialUnavailableError):
            vault.transport_materialization("commandcode_goat", route="direct_api")

    def test_legacy_route_keeps_external_authority_intentionally(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode()  # no direct credential: CLI owns auth
        assert (
            CredentialVault.default().transport_materialization(
                "commandcode_goat", route="legacy_cli"
            )
            is None
        )

    def test_auth_none_materializes_nothing_intentionally(self) -> None:
        pc.add_provider_config(
            name="Loopback",
            base_url="http://127.0.0.1:59994/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="loopback_r21d",
            auth_mode=pc.AUTH_NONE,
        )
        assert (
            CredentialVault.default().transport_materialization(
                "loopback_r21d", route="direct_api"
            )
            is None
        )

    def test_gateway_transport_environment_fails_closed_for_direct_race(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        gateway = ModelGateway()
        binding = gateway.resolve("commandcode_goat", "deepseek/deepseek-v4-flash")
        assert binding.route == "direct_api"
        del _hermetic_vault["commandcode_goat"]
        # 4. no adapter child starts: transport construction itself fails.
        with pytest.raises(CredentialUnavailableError):
            gateway.transport_environment(binding)


# ---------------------------------------------------------------------------
# FINDING 7 — disabled provider remains configured
# ---------------------------------------------------------------------------


class TestFinding7DisabledStillConfigured:
    def test_disabled_provider_is_configured_false_ready(self, _hermetic_vault) -> None:
        _configure_commandcode()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        vault = CredentialVault.default()
        assert vault.readiness("commandcode_goat").credential_ready is True
        pc.update_provider_config("commandcode_goat", enabled=False)
        readiness = vault.readiness("commandcode_goat")
        assert readiness.is_configured is True  # durably configured
        assert readiness.is_enabled is False  # separate fact
        assert readiness.credential_ready is False
        assert "disabled" in (readiness.reason or "")
        # Provider config remains intact and reloadable.
        cfg = pc.get_provider_config("commandcode_goat")
        assert cfg is not None and cfg.enabled is False

    def test_unconfigured_provider_still_reports_not_configured(
        self, _hermetic_vault
    ) -> None:
        readiness = CredentialVault.default().readiness("never_configured_r21")
        assert readiness.is_configured is False
        assert readiness.is_enabled is False
