"""V2-04 Candidate-24 REPAIR acceptance tests.

Closes the FirstMate review blockers against Candidate 23:

- F1: a stale lease cannot be rebound to a new provider authority.
  Candidate 23 checked provider ID + source kind only at retain time, so
  ``lease_A (authority A) + binding_B (authority B, same provider/source)``
  produced a usable B ticket that materialized SECRET_A into a B
  transport.  Repair 24 binds the SAFE issuance authority into the lease
  itself (no value comparison) and proves fingerprint equality at retain.
- F2: configured_source before/after sampling is ABA-vulnerable.
  Candidate 23 sampled authority before/after ``_resolve_registry_model``
  and accepted ``before == after`` even when the resolver genuinely built
  executable B during an A->B->A window, then paired B-executable with
  A-credential.  Repair 24 makes the resolver single-snapshot coherent
  (endpoint/auth/authority from ONE snapshot, authority carried in
  provenance) and binds executable authority to credential authority at
  egress.

Synthetic values only. No secret string appears in any error text.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.application import provider_connections as pc  # noqa: E402
from agentic_debugger.application.credential_vault import (  # noqa: E402
    CredentialBinding,
    CredentialUnavailableError,
    CredentialVault,
    CredentialVaultError,
    StaleCredentialBindingError,
)
from agentic_debugger.application.model_gateway import (  # noqa: E402
    ModelGateway,
    provider_runtime_identity,
)

SECRET_A = "r24-synthetic-credential-alpha-not-real"
SECRET_B = "r24-synthetic-credential-beta-not-real"


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
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY_AUTHORITY",
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY_AUTHORITY",
    ):
        monkeypatch.delenv(var, raising=False)
    pc.clear_all_session_keys()
    pc._QUARANTINED_PROVIDERS.clear()
    vault = CredentialVault.default()
    vault._session_leases.clear()
    yield store
    pc.clear_all_session_keys()
    pc._QUARANTINED_PROVIDERS.clear()
    vault._session_leases.clear()


def _configure_generic(provider_id: str, base_url: str) -> None:
    pc.add_provider_config(
        name=f"Provider {provider_id}",
        base_url=base_url,
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id=provider_id,
        transport_profile=pc.TRANSPORT_GENERIC,
    )


def _authority(provider_id: str) -> str:
    cfg = pc.get_provider_config(provider_id)
    assert cfg is not None
    identity = provider_runtime_identity(cfg)
    assert identity is not None
    return identity


class TestF1LeaseCannotBeRebound:
    def test_retain_lease_a_under_binding_b_fails_closed(self, _hermetic_vault) -> None:
        _configure_generic("prov_p", "https://a.example.invalid/v1")
        _hermetic_vault["prov_p"] = SECRET_A
        vault = CredentialVault.default()
        binding_a = vault.safe_binding("prov_p")
        assert binding_a is not None
        lease_a = vault.resolve_lease(binding_a)
        assert lease_a is not None
        # Reconfigure SAME provider A -> B with explicit re-entry.
        pc.update_provider_config(
            "prov_p", base_url="https://b.example.invalid/v1", api_key=SECRET_B
        )
        binding_b = vault.safe_binding("prov_p")
        assert binding_b is not None
        assert binding_b.provider_authority != binding_a.provider_authority
        assert binding_b.provider_id == binding_a.provider_id
        assert binding_b.source_kind == binding_a.source_kind
        # MUST fail on Candidate 23 (provider+source match); passes after repair.
        with pytest.raises(StaleCredentialBindingError) as excinfo:
            vault.retain_lease(lease_a, binding_b)
        assert SECRET_A not in str(excinfo.value)
        assert SECRET_B not in str(excinfo.value)

    def test_rebound_ticket_never_materializes_a_into_b(self, _hermetic_vault) -> None:
        _configure_generic("prov_p", "https://a.example.invalid/v1")
        _hermetic_vault["prov_p"] = SECRET_A
        vault = CredentialVault.default()
        binding_a = vault.safe_binding("prov_p")
        assert binding_a is not None
        lease_a = vault.resolve_lease(binding_a)
        assert lease_a is not None
        pc.update_provider_config(
            "prov_p", base_url="https://b.example.invalid/v1", api_key=SECRET_B
        )
        binding_b = vault.safe_binding("prov_p")
        assert binding_b is not None
        # Even if retain were bypassed, transport for ModelBinding_B must
        # never release SECRET_A.  Here retain itself fails, so there is no
        # B ticket at all; prove the legitimate B path yields B only.
        with pytest.raises(CredentialVaultError):
            vault.retain_lease(lease_a, binding_b)
        model_b = ModelGateway().resolve("prov_p", "m1")
        assert model_b.endpoint == "https://b.example.invalid/v1"
        lease_b = vault.resolve_lease(binding_b)
        assert lease_b is not None
        ticket_b = vault.retain_lease(lease_b, binding_b)
        env = ModelGateway().transport_environment(
            model_b, credential_binding=binding_b, credential_ticket=ticket_b
        )
        assert env == {pc.provider_session_credential_variable("prov_p"): SECRET_B}
        assert SECRET_A not in str(env)

    def test_legitimate_same_binding_remains_green(self, _hermetic_vault) -> None:
        _configure_generic("prov_p", "https://a.example.invalid/v1")
        _hermetic_vault["prov_p"] = SECRET_A
        vault = CredentialVault.default()
        binding_a = vault.safe_binding("prov_p")
        assert binding_a is not None
        lease_a = vault.resolve_lease(binding_a)
        assert lease_a is not None
        ticket_a = vault.retain_lease(lease_a, binding_a)
        model_a = ModelGateway().resolve("prov_p", "m1")
        env = ModelGateway().transport_environment(
            model_a, credential_binding=binding_a, credential_ticket=ticket_a
        )
        assert env == {pc.provider_session_credential_variable("prov_p"): SECRET_A}

    def test_value_rotation_same_authority_remains_valid(self, _hermetic_vault) -> None:
        _configure_generic("prov_p", "https://a.example.invalid/v1")
        _hermetic_vault["prov_p"] = SECRET_A
        vault = CredentialVault.default()
        binding = vault.safe_binding("prov_p")
        assert binding is not None
        lease_a = vault.resolve_lease(binding)
        assert lease_a is not None
        # Value-only rotation preserves runtime authority (same fingerprint).
        _hermetic_vault["prov_p"] = SECRET_B
        assert _authority("prov_p") == binding.provider_authority
        # Already-resolved lease_A retained under the same binding stays valid
        # (pinned SECRET_A may continue); no value comparison was used.
        ticket_a = vault.retain_lease(lease_a, binding)
        model = ModelGateway().resolve("prov_p", "m1")
        env = ModelGateway().transport_environment(
            model, credential_binding=binding, credential_ticket=ticket_a
        )
        assert env == {pc.provider_session_credential_variable("prov_p"): SECRET_A}
        # New session resolves SECRET_B under the same authority.
        binding2 = vault.safe_binding("prov_p")
        assert binding2 is not None
        assert binding2.fingerprint() == binding.fingerprint()
        lease_b = vault.resolve_lease(binding2)
        assert lease_b is not None
        ticket_b = vault.retain_lease(lease_b, binding2)
        env_b = ModelGateway().transport_environment(
            model, credential_binding=binding2, credential_ticket=ticket_b
        )
        assert env_b == {pc.provider_session_credential_variable("prov_p"): SECRET_B}


class TestF2AbaExecutableCredentialCoherence:
    def test_aba_during_resolution_fails_closed(self, _hermetic_vault) -> None:
        _configure_generic("prov_g", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g"] = SECRET_A
        from agentic_debugger.application.configured_source import (
            _resolve_registry_model,
        )

        auth_a = _authority("prov_g")
        # Pre-read A (what the vulnerable before-sampling observed).
        pre = auth_a
        # Change to B; resolver genuinely constructs B.
        pc.update_provider_config(
            "prov_g", base_url="https://b.example.invalid/v1", api_key=SECRET_B
        )
        live, provenance, _fp = _resolve_registry_model("prov_g", "m1")
        assert "b.example.invalid" in str(live.command)
        assert provenance.get("endpoint") == "https://b.example.invalid/v1"
        exe_auth = provenance.get("provider_runtime_identity")
        assert exe_auth is not None
        assert exe_auth != auth_a
        # Restore to A before post-read.
        pc.update_provider_config(
            "prov_g", base_url="https://a.example.invalid/v1", api_key=SECRET_A
        )
        post = _authority("prov_g")
        assert pre == post  # vulnerable check would accept
        assert post == auth_a
        # Repaired path binds executable authority (B) to credential egress:
        # CURRENT (A) != exe (B) must fail before any environment or child.
        with pytest.raises(CredentialVaultError) as excinfo:
            CredentialVault.default().transport_materialization(
                "prov_g",
                route="direct_api",
                expected_provider_authority=exe_auth,
            )
        assert SECRET_A not in str(excinfo.value)
        assert SECRET_B not in str(excinfo.value)
        # And the reverse (A executable + B credential) also fails: resolve
        # under A, mutate to B, require A while current is B.
        live_a, prov_a, _fp2 = _resolve_registry_model("prov_g", "m1")
        assert prov_a.get("endpoint") == "https://a.example.invalid/v1"
        exe_a = prov_a.get("provider_runtime_identity")
        assert exe_a == auth_a
        pc.update_provider_config(
            "prov_g", base_url="https://b.example.invalid/v1", api_key=SECRET_B
        )
        with pytest.raises(CredentialVaultError):
            CredentialVault.default().transport_materialization(
                "prov_g",
                route="direct_api",
                expected_provider_authority=exe_a,
            )

    def test_unchanged_authority_still_materializes(self, _hermetic_vault) -> None:
        _configure_generic("prov_g", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g"] = SECRET_A
        from agentic_debugger.application.configured_source import (
            _resolve_registry_model,
        )

        live, provenance, _fp = _resolve_registry_model("prov_g", "m1")
        exe_auth = provenance.get("provider_runtime_identity")
        assert exe_auth == _authority("prov_g")
        env = CredentialVault.default().transport_materialization(
            "prov_g",
            route="direct_api",
            expected_provider_authority=exe_auth,
        )
        assert env == {pc.provider_session_credential_variable("prov_g"): SECRET_A}
