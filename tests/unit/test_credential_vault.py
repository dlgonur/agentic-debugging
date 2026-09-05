"""V2-04 CredentialVault acceptance tests.

Covers the mandated V2-04 test sets over the authoritative
``application.credential_vault`` seam:

- core vault (store / binding / lease / revoke / restart / persistence);
- binding vs lease separation and safe serialization/repr/fingerprint;
- session-stable lease semantics (secret authority does not drift);
- provider runtime identity rebinding (stale binding fails closed);
- endpoint edit re-entry behavior (accepted safety rules preserved);
- environment-backed sources (name-only durable binding, snapshot lease);
- external CLI credential authority (safe representation, no fake lease);
- quarantine integration (configured but not ready, recovery required);
- credential-free errors, no plaintext persistence, no value enumeration.

Offline only: synthetic credentials, an in-memory secure-store fake, and a
hermetic provider configuration.  No real credential, no provider contact.
"""

from __future__ import annotations

import copy
import json
import os
import pickle
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.application import provider_connections as pc  # noqa: E402
from agentic_debugger.application.credential_vault import (  # noqa: E402
    CREDENTIAL_SOURCE_EXTERNAL_CLI,
    CREDENTIAL_SOURCE_NONE,
    CredentialBinding,
    CredentialLease,
    CredentialUnavailableError,
    CredentialVault,
    StaleCredentialBindingError,
)
from agentic_debugger.application.model_gateway import (  # noqa: E402
    ModelBinding,
    ModelGateway,
    provider_runtime_identity,
)

SECRET_A = "v204-synthetic-credential-alpha-not-real"
SECRET_B = "v204-synthetic-credential-beta-not-real"


@pytest.fixture(autouse=True)
def _hermetic_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Hermetic provider registry + in-memory durable store backend."""
    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(tmp_path / "c.json")
    )
    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(tmp_path / "q.json")
    )
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path / "config-dir"))
    monkeypatch.setattr(pc, "catalog_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: tmp_path / "missing-auth.json")
    store: dict[str, str] = {}
    monkeypatch.setattr(pc, "save_secure_credential", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in store)
    monkeypatch.setattr(pc, "delete_secure_credential", lambda k: store.pop(k, None) is not None)
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
    yield store
    pc.clear_all_session_keys()


def _configure_commandcode(**kwargs) -> None:
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
        **kwargs,
    )


def _configure_generic(provider_id: str = "my_gateway") -> None:
    pc.add_provider_config(
        name="My Gateway",
        base_url="https://gateway.example.invalid/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id=provider_id,
    )


# ---------------------------------------------------------------------------
# Core vault (§30)
# ---------------------------------------------------------------------------


class TestCoreVault:
    def test_store_then_binding_then_lease(self, _hermetic_vault) -> None:
        _configure_commandcode(api_key=SECRET_A)  # A. store
        vault = CredentialVault.default()
        binding = vault.safe_binding("commandcode_goat")  # B. safe binding
        assert binding is not None
        assert binding.source_kind == pc.CREDENTIAL_SOURCE_SAVED
        assert binding.provider_id == "commandcode_goat"
        assert binding.auth_mode == "bearer"
        assert binding.endpoint_bound is False
        lease = vault.resolve_lease(binding)  # D. resolve lease
        assert isinstance(lease, CredentialLease)
        env = lease.materialize_environment()
        assert env == {"AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY": SECRET_A}

    def test_binding_serialization_carries_no_secret(self, _hermetic_vault) -> None:
        _configure_commandcode(api_key=SECRET_A)
        binding = CredentialVault.default().safe_binding("commandcode_goat")
        assert binding is not None
        rendered = json.dumps(binding.to_mapping())  # C. serialization
        assert SECRET_A not in rendered
        assert SECRET_A not in binding.fingerprint()
        assert SECRET_A not in repr(binding)
        # Round-trip through the safe mapping stays value-free and faithful.
        restored = CredentialBinding.from_mapping(binding.to_mapping())
        assert restored.fingerprint() == binding.fingerprint()

    def test_lease_repr_has_no_secret_and_serialization_fails_closed(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode(api_key=SECRET_A)
        lease = CredentialVault.default().resolve_lease("commandcode_goat")
        assert lease is not None
        assert SECRET_A not in repr(lease)  # E
        with pytest.raises(TypeError):
            pickle.dumps(lease)  # F
        with pytest.raises(TypeError):
            copy.copy(lease)
        with pytest.raises(TypeError):
            copy.deepcopy(lease)
        with pytest.raises(TypeError):
            lease.__getstate__()

    def test_revoke_makes_readiness_false_and_keeps_config(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode(api_key=SECRET_A)
        vault = CredentialVault.default()
        assert vault.readiness("commandcode_goat").credential_ready is True
        vault.revoke_credential("commandcode_goat")  # G. delete
        readiness = vault.readiness("commandcode_goat")
        assert readiness.credential_ready is False  # H
        assert readiness.source_kind is None
        cfg = pc.get_provider_config("commandcode_goat")  # I. config intact
        assert cfg is not None and cfg.enabled is True
        with pytest.raises(CredentialUnavailableError) as excinfo:
            vault.resolve_lease("commandcode_goat")
        assert SECRET_A not in str(excinfo.value)

    def test_new_vault_instance_resolves_stored_binding_after_restart(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode(api_key=SECRET_A)
        fresh = CredentialVault()  # J. restart: second instance, same backend
        binding = fresh.safe_binding("commandcode_goat")
        assert binding is not None
        lease = fresh.resolve_lease(binding)
        assert lease is not None
        assert lease.reveal() == SECRET_A
        # No prior lease was restored from anywhere: the lease type itself
        # cannot be serialized, so nothing durable can carry one.

    def test_no_plaintext_credential_in_provider_config_persistence(
        self, _hermetic_vault, tmp_path: Path
    ) -> None:
        _configure_commandcode(api_key=SECRET_A)
        raw = Path(os.environ["AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH"]).read_text(
            encoding="utf-8"
        )
        assert SECRET_A not in raw  # K
        assert "api_key" not in raw.lower().replace("api_key_format", "")

    def test_no_value_enumeration_api_exists(self) -> None:
        public_api = {name for name in dir(CredentialVault) if not name.startswith("_")}
        forbidden = {"dump", "get_all_secrets", "values", "export", "to_mapping"}
        assert not (public_api & forbidden)
        # CredentialLease exposes exactly the two capability methods.
        lease_api = {
            name for name in dir(CredentialLease) if not name.startswith("_")
        }
        assert lease_api == {"materialize_environment", "provider_id", "reveal", "source_kind"}


# ---------------------------------------------------------------------------
# Session-stable lease semantics (§15 / §31)
# ---------------------------------------------------------------------------


class TestSessionStableLease:
    def test_existing_lease_survives_durable_overwrite(self, _hermetic_vault) -> None:
        _configure_commandcode(api_key=SECRET_A)  # A/B
        vault = CredentialVault.default()
        binding = vault.safe_binding("commandcode_goat")
        lease = vault.resolve_lease(binding)  # C. session A lease
        assert lease.reveal() == SECRET_A
        _hermetic_vault["commandcode_goat"] = SECRET_B  # D. slot replaced
        # E. existing session still materializes SECRET_A.
        env = lease.materialize_environment()
        assert env == {"AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY": SECRET_A}
        new_lease = vault.resolve_lease("commandcode_goat")  # F. new session
        assert new_lease is not None and new_lease.reveal() == SECRET_B
        # Neither value appears in durable session/model/config metadata.
        assert SECRET_A not in json.dumps(binding.to_mapping())
        assert SECRET_B not in json.dumps(binding.to_mapping())
        raw = Path(os.environ["AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH"]).read_text(
            encoding="utf-8"
        )
        assert SECRET_A not in raw and SECRET_B not in raw

    def test_independent_leases_are_distinct_objects(self, _hermetic_vault) -> None:
        _configure_commandcode(api_key=SECRET_A)
        vault = CredentialVault.default()
        lease_one = vault.resolve_lease("commandcode_goat")
        lease_two = vault.resolve_lease("commandcode_goat")
        assert lease_one is not lease_two  # §23: independent ephemeral objects
        vault.revoke_credential("commandcode_goat")
        # Revocation never mutates an already-resolved lease in place.
        assert lease_one.reveal() == SECRET_A


# ---------------------------------------------------------------------------
# Provider runtime identity rebinding (§16 / §32 / §13)
# ---------------------------------------------------------------------------


class TestProviderIdentityRebinding:
    def test_stale_binding_fails_closed_and_reentry_mints_new_binding(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode(api_key=SECRET_A)  # A. endpoint A + credential A
        vault = CredentialVault.default()
        binding_a = vault.safe_binding("commandcode_goat")
        assert binding_a is not None
        # B. edit to endpoint B: a blank-key edit is refused so the old
        # credential is never silently carried to the new authority.
        with pytest.raises(pc.ProviderConnectionError) as excinfo:
            pc.update_provider_config("commandcode_goat", base_url="https://gateway-b.example.invalid/v1")
        assert SECRET_A not in str(excinfo.value)
        # C. the old binding is no longer valid FOR authority B: a stale
        # binding fails closed when resolution is attempted against it.
        pc.update_provider_config(
            "commandcode_goat",
            base_url="https://gateway-b.example.invalid/v1",
            api_key=SECRET_B,  # D. explicit re-entry
        )
        with pytest.raises(StaleCredentialBindingError) as stale:
            vault.resolve_lease(binding_a)
        assert SECRET_A not in str(stale.value) and SECRET_B not in str(stale.value)
        binding_b = vault.safe_binding("commandcode_goat")
        assert binding_b is not None
        # E. distinct safe identity; F. no credential value in either.
        assert binding_b.provider_authority != binding_a.provider_authority
        assert binding_b.fingerprint() != binding_a.fingerprint()
        for binding in (binding_a, binding_b):
            rendered = json.dumps(binding.to_mapping())
            assert SECRET_A not in rendered and SECRET_B not in rendered
        # The provider is Credential ready again under binding B.
        assert vault.readiness("commandcode_goat").credential_ready is True
        assert vault.resolve_lease("commandcode_goat").reveal() == SECRET_B

    def test_quarantine_blocks_resolution_but_keeps_provider_configured(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode(api_key=SECRET_A)
        vault = CredentialVault.default()
        pc.quarantine_provider("commandcode_goat")
        readiness = vault.readiness("commandcode_goat")
        assert readiness.recovery_required is True
        assert readiness.credential_ready is False  # §6 truthful semantics
        assert readiness.source_kind is None
        cfg = pc.get_provider_config("commandcode_goat")
        assert cfg is not None and cfg.enabled  # provider stays configured
        with pytest.raises(CredentialUnavailableError) as excinfo:
            vault.resolve_lease("commandcode_goat")
        assert SECRET_A not in str(excinfo.value)  # §20 credential-free errors
        # Re-establishing a coherent credential pair clears recovery
        # (accepted rules: only an explicit re-entry clears quarantine).
        pc.update_provider_config("commandcode_goat", api_key=SECRET_B)
        readiness = vault.readiness("commandcode_goat")
        assert readiness.recovery_required is False
        assert readiness.credential_ready is True


# ---------------------------------------------------------------------------
# Environment-backed credentials (§33 / §26 / §7)
# ---------------------------------------------------------------------------


class TestEnvironmentSource:
    def test_env_binding_stores_name_only_and_lease_snapshots_value(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_commandcode()  # no saved key: ambient env source applies
        vault = CredentialVault.default()
        monkeypatch.setenv("COMMAND_CODE_API_KEY", SECRET_A)  # B. value A
        binding = vault.safe_binding("commandcode_goat")
        assert binding is not None
        assert binding.source_kind == pc.CREDENTIAL_SOURCE_ENVIRONMENT
        assert binding.source_ref == "COMMAND_CODE_API_KEY"  # F. name only
        assert binding.endpoint_bound is True
        rendered = json.dumps(binding.to_mapping())
        assert SECRET_A not in rendered
        lease = vault.resolve_lease(binding)
        assert lease.reveal() == SECRET_A
        monkeypatch.setenv("COMMAND_CODE_API_KEY", SECRET_B)  # C. parent change
        # D. existing lease remains A.
        assert lease.reveal() == SECRET_A
        # E. new lease sees B.
        assert vault.resolve_lease("commandcode_goat").reveal() == SECRET_B

    def test_env_source_invalid_after_endpoint_change_fails_closed(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_commandcode()
        monkeypatch.setenv("COMMAND_CODE_API_KEY", SECRET_A)
        vault = CredentialVault.default()
        assert vault.readiness("commandcode_goat").credential_ready is True
        # An endpoint change is blocked while the ambient source exists —
        # the accepted rebinding rule (§5/§13) — so use a fresh provider to
        # prove the endpoint-binding invalidation semantics directly.
        pc.add_provider_config(
            name="CommandCode GOAT moved",
            base_url="https://moved.example.invalid/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="commandcode_moved",
            transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
        )
        readiness = vault.readiness("commandcode_moved")
        assert readiness.credential_ready is False
        assert readiness.rebinding_required is True
        assert SECRET_A not in (readiness.reason or "")
        with pytest.raises(CredentialUnavailableError):
            vault.resolve_lease("commandcode_moved")

    def test_transport_materialization_uses_single_issued_channel(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_commandcode(api_key=SECRET_A)
        env = CredentialVault.default().transport_materialization("commandcode_goat")
        assert env == {"AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY": SECRET_A}
        assert "COMMAND_CODE_API_KEY" not in env


# ---------------------------------------------------------------------------
# External CLI credential authority (§34 / §27)
# ---------------------------------------------------------------------------


class TestExternalCliAuthority:
    def _enable_legacy_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentic_debugger.application import model_providers as mp

        monkeypatch.setattr(mp, "_commandcode_availability", lambda: (True, None))

    def test_external_authority_is_safe_and_never_mints_a_lease(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_commandcode()  # no direct credential at all
        self._enable_legacy_cli(monkeypatch)
        vault = CredentialVault.default()
        external = vault.external_cli_authority("commandcode_goat")
        assert external is not None  # A/B. safe external representation
        assert external.source_kind == CREDENTIAL_SOURCE_EXTERNAL_CLI
        assert SECRET_A not in json.dumps(external.to_mapping())
        with pytest.raises(CredentialUnavailableError) as excinfo:
            vault.resolve_lease(external)  # C. no fake raw-secret lease
        assert "external" in str(excinfo.value)
        # D. the legacy route remains runnable through the accepted probe.
        gateway = ModelGateway()
        preflight = gateway.static_preflight("commandcode_goat")
        assert preflight.route == "legacy_cli"
        assert preflight.is_runnable is True

    def test_generic_provider_never_gains_external_cli_authority(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_generic()  # E. generic provider, no CLI semantics
        self._enable_legacy_cli(monkeypatch)
        assert CredentialVault.default().external_cli_authority("my_gateway") is None

    def test_unavailable_cli_yields_no_external_authority(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentic_debugger.application import model_providers as mp

        _configure_commandcode()
        monkeypatch.setattr(mp, "_commandcode_availability", lambda: (False, "no cli"))
        assert CredentialVault.default().external_cli_authority("commandcode_goat") is None


# ---------------------------------------------------------------------------
# No-auth providers (§7)
# ---------------------------------------------------------------------------


class TestAuthNone:
    def test_no_auth_yields_explicit_none_binding_and_no_lease(self) -> None:
        pc.add_provider_config(
            name="Loopback",
            base_url="http://127.0.0.1:8123/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="loopback_local",
            auth_mode=pc.AUTH_NONE,
        )
        vault = CredentialVault.default()
        readiness = vault.readiness("loopback_local")
        assert readiness.credential_ready is True
        assert readiness.source_kind == CREDENTIAL_SOURCE_NONE
        binding = vault.safe_binding("loopback_local")
        assert binding is not None
        assert binding.source_kind == CREDENTIAL_SOURCE_NONE
        assert binding.source_ref is None
        assert vault.resolve_lease("loopback_local") is None  # no fake credential


# ---------------------------------------------------------------------------
# Gateway integration facts (§10 / §11)
# ---------------------------------------------------------------------------


class TestGatewayVaultIntegration:
    def test_status_reads_credential_facts_through_vault(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode(api_key=SECRET_A)
        gateway = ModelGateway()
        status = gateway.get_provider_status("commandcode_goat")
        assert status.credential_ready is True
        assert status.credential_source == pc.CREDENTIAL_SOURCE_SAVED
        CredentialVault.default().revoke_credential("commandcode_goat")
        status = gateway.get_provider_status("commandcode_goat")
        assert status.credential_ready is False
        assert status.credential_source is None

    def test_session_hop_and_transport_env_route_through_vault(
        self, _hermetic_vault
    ) -> None:
        _configure_commandcode()
        pc.set_session_key("commandcode_goat", SECRET_B)
        gateway = ModelGateway()
        binding = gateway.resolve("commandcode_goat", "deepseek/deepseek-v4-flash")
        assert isinstance(binding, ModelBinding)
        hop = gateway.session_credential_environment("commandcode_goat")
        authority_var = pc.provider_session_credential_authority_variable(
            "commandcode_goat"
        )
        assert hop == {
            "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY": SECRET_B,
            authority_var: provider_runtime_identity(
                pc.get_provider_config("commandcode_goat")
            ),
        }
        transport_env = gateway.transport_environment(binding)
        assert transport_env == {
            "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY": SECRET_B
        }
        # No secret material anywhere in the binding provenance payload.
        payload = json.dumps(binding.model_configured_payload())
        assert SECRET_A not in payload and SECRET_B not in payload

    def test_binding_carries_provider_runtime_authority(self, _hermetic_vault) -> None:
        _configure_commandcode(api_key=SECRET_A)
        binding = CredentialVault.default().safe_binding("commandcode_goat")
        cfg = pc.get_provider_config("commandcode_goat")
        assert cfg is not None
        assert binding.provider_authority == provider_runtime_identity(cfg)
