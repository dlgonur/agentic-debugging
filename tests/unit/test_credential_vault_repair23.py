"""V2-04 Candidate-23 REPAIR acceptance tests.

Closes the third FirstMate review's findings against Candidate 22:

- Finding 1: credential_ticket is inseparable from its CredentialBinding
  (ticket without binding fails closed; retain/redeem require pair
  coherence; SessionLaunch construction enforces the invariant; correct
  pairs remain green and redeem exactly once).
- Finding 2: retained-ticket egress re-authorizes the SAFE binding against
  CURRENT provider configuration (identity drift / disabled / quarantined
  blocks release; secret-value rotation with unchanged identity preserves
  session stability).
- Finding 3: configured_source binds executable authority A with credential
  authority A (no A/B pairing; unchanged happy path; legacy no-raw-secret;
  generic stays direct).
- Finding 4: retained-ticket lifecycle has an explicit idempotent
  release_ticket authority.

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
    CREDENTIAL_SOURCE_EXTERNAL_CLI,
    CREDENTIAL_SOURCE_NONE,
    CredentialBinding,
    CredentialLease,
    CredentialRouteError,
    CredentialUnavailableError,
    CredentialVault,
    CredentialVaultError,
    StaleCredentialBindingError,
)
from agentic_debugger.application.model_gateway import (  # noqa: E402
    IncoherentCredentialBindingError,
    ModelBinding,
    ModelGateway,
    StaleModelBindingError,
    provider_runtime_identity,
)

SECRET_A = "r23-synthetic-credential-alpha-not-real"
SECRET_B = "r23-synthetic-credential-beta-not-real"
SECRET_P = "r23-synthetic-credential-papa-not-real"
SECRET_Q = "r23-synthetic-credential-quebec-not-real"


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
        "AGENTIC_DEBUGGER_PROVIDER_PROV_P_API_KEY",
        "AGENTIC_DEBUGGER_PROVIDER_PROV_Q_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    pc.clear_all_session_keys()
    pc._QUARANTINED_PROVIDERS.clear()
    vault = CredentialVault.default()
    vault._session_leases.clear()
    # Fresh gateway state (Task 29 resolved: ModelGateway.default() owns
    # canonical process-level gateway state while contextual config_root
    # instances remain isolated; tests preserve fresh ModelGateway() instances).
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


def _saved_binding(provider_id: str) -> CredentialBinding:
    return CredentialBinding(
        provider_id=provider_id,
        source_kind=pc.CREDENTIAL_SOURCE_SAVED,
        source_ref=pc.credential_slot_name(provider_id),
        auth_mode="bearer",
        provider_authority=_authority(provider_id),
        endpoint_bound=False,
    )


def _direct_model_binding(provider_id: str) -> ModelBinding:
    return ModelGateway().resolve(provider_id, "deepseek/deepseek-v4-flash")


def _build_launch(provider_id: str):
    from agentic_debugger.application.session_runtime import (
        ProjectRuntimeEnvironmentSpec,
        build_local_project_launch,
    )

    return build_local_project_launch(
        session_id="sess-r23-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        task_id="local-project-debug",
        policy="static-baseline",
        provider_id=provider_id,
        model_id="deepseek/deepseek-v4-flash",
        profile_id="deepseek/deepseek-v4-flash",
        launch_snapshot=dict(os.environ),
        project_spec=ProjectRuntimeEnvironmentSpec(),
    )


# ---------------------------------------------------------------------------
# FINDING 1 — ticket inseparable from its binding
# ---------------------------------------------------------------------------


class TestFinding1TicketBindingInseparable:
    def test_q_ticket_with_p_model_and_no_binding_fails_closed(
        self, _hermetic_vault
    ) -> None:
        _configure_generic("prov_p", "https://p.example.invalid/v1")
        _configure_generic("prov_q", "https://q.example.invalid/v1")
        _hermetic_vault["prov_p"] = SECRET_P
        _hermetic_vault["prov_q"] = SECRET_Q
        vault = CredentialVault.default()
        binding_q = vault.safe_binding("prov_q")
        assert binding_q is not None
        lease_q = vault.resolve_lease(binding_q)
        assert lease_q is not None
        ticket_q = vault.retain_lease(lease_q, binding_q)
        gateway = ModelGateway()
        model_p = _direct_model_binding("prov_p")
        # Gateway boundary: ticket without binding fails closed, no env.
        with pytest.raises(
            (
                IncoherentCredentialBindingError,
                StaleCredentialBindingError,
                CredentialVaultError,
            )
        ) as excinfo:
            gateway.transport_environment(
                model_p, credential_binding=None, credential_ticket=ticket_q
            )
        assert SECRET_Q not in str(excinfo.value)
        # Vault boundary likewise fails closed (never redeems without binding).
        with pytest.raises(CredentialVaultError) as excinfo2:
            vault.transport_materialization(
                "prov_p",
                route="direct_api",
                credential_binding=None,
                credential_ticket=ticket_q,
            )
        assert SECRET_Q not in str(excinfo2.value)

    def test_retain_lease_with_mismatched_binding_fails_closed(
        self, _hermetic_vault
    ) -> None:
        _configure_generic("prov_p", "https://p.example.invalid/v1")
        _configure_generic("prov_q", "https://q.example.invalid/v1")
        _hermetic_vault["prov_p"] = SECRET_P
        _hermetic_vault["prov_q"] = SECRET_Q
        vault = CredentialVault.default()
        binding_p = _saved_binding("prov_p")
        binding_q = _saved_binding("prov_q")
        lease_q = vault.resolve_lease(binding_q)
        assert lease_q is not None
        with pytest.raises(CredentialVaultError):
            vault.retain_lease(lease_q, binding_p)

    def test_ticket_q_with_binding_p_fails_closed(self, _hermetic_vault) -> None:
        _configure_generic("prov_p", "https://p.example.invalid/v1")
        _configure_generic("prov_q", "https://q.example.invalid/v1")
        _hermetic_vault["prov_p"] = SECRET_P
        _hermetic_vault["prov_q"] = SECRET_Q
        vault = CredentialVault.default()
        binding_p = _saved_binding("prov_p")
        binding_q = _saved_binding("prov_q")
        lease_q = vault.resolve_lease(binding_q)
        assert lease_q is not None
        ticket_q = vault.retain_lease(lease_q, binding_q)
        # Direct redeem with the wrong expected binding fails stale.
        with pytest.raises(StaleCredentialBindingError):
            vault.redeem_lease(ticket_q, expected_binding=binding_p)
        # A fresh ticket for the same check through the gateway boundary.
        lease_q2 = vault.resolve_lease(binding_q)
        assert lease_q2 is not None
        ticket_q2 = vault.retain_lease(lease_q2, binding_q)
        model_p = _direct_model_binding("prov_p")
        # Pair incoherence (Q ticket vs P model/binding) fails closed.
        with pytest.raises(
            (
                IncoherentCredentialBindingError,
                StaleCredentialBindingError,
                StaleModelBindingError,
                CredentialVaultError,
            )
        ):
            ModelGateway().transport_environment(
                model_p, credential_binding=binding_p, credential_ticket=ticket_q2
            )

    def test_session_launch_ticket_without_binding_fails(self, _hermetic_vault) -> None:
        _configure_generic("prov_p", "https://p.example.invalid/v1")
        _hermetic_vault["prov_p"] = SECRET_P
        from agentic_debugger.application.execution_environment import (
            ExecutionEnvironment,
        )
        from agentic_debugger.application.session import SessionBudgets
        from agentic_debugger.application.session_runtime import (
            AgentDefinition,
            EffectiveSessionCapabilities,
            ProjectRuntimeEnvironmentSpec,
            SessionLaunch,
            SessionCapability,
        )

        agent = AgentDefinition(
            controller_policy="static-baseline",
            provider_id="prov_p",
            model_id="m1",
        )
        env = ExecutionEnvironment.for_local_project(
            dict(os.environ), ProjectRuntimeEnvironmentSpec()
        )
        caps = EffectiveSessionCapabilities(
            capabilities=frozenset({SessionCapability.PROJECT_COMMAND})
        )
        model_binding = _direct_model_binding("prov_p")
        with pytest.raises(Exception):
            SessionLaunch(
                session_id="sess-r23-11111111-2222-3333-4444-555555555555",
                task_id="local-project-debug",
                agent=agent,
                execution_environment=env,
                project_spec=ProjectRuntimeEnvironmentSpec(),
                capabilities=caps,
                profile_id="m1",
                budgets=SessionBudgets(),
                model_binding=model_binding,
                credential_binding=None,
                credential_ticket="a" * 32,
            )

    def test_session_launch_ticket_on_non_materializable_authority_fails(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_generic("prov_p", "https://p.example.invalid/v1")
        _hermetic_vault["prov_p"] = SECRET_P
        from agentic_debugger.application.execution_environment import (
            ExecutionEnvironment,
        )
        from agentic_debugger.application.session import SessionBudgets
        from agentic_debugger.application.session_runtime import (
            AgentDefinition,
            EffectiveSessionCapabilities,
            ProjectRuntimeEnvironmentSpec,
            SessionLaunch,
            SessionCapability,
        )

        agent = AgentDefinition(
            controller_policy="static-baseline",
            provider_id="prov_p",
            model_id="m1",
        )
        env = ExecutionEnvironment.for_local_project(
            dict(os.environ), ProjectRuntimeEnvironmentSpec()
        )
        caps = EffectiveSessionCapabilities(
            capabilities=frozenset({SessionCapability.PROJECT_COMMAND})
        )
        model_binding = _direct_model_binding("prov_p")
        # external_cli authority can never carry a ticket: build a historical
        # provider whose external authority is canonical, then prove a launch
        # carrying it plus a ticket fails construction.
        pc.add_provider_config(
            name="Historical",
            base_url="https://hist.example.invalid/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="hist_prov",
            transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
        )
        from agentic_debugger.application import model_providers as _mp

        _orig_legacy = _mp._legacy_for_config
        monkeypatch.setattr(
            _mp, "_legacy_for_config", lambda cfg: (True, None)
        )
        try:
            external_binding = CredentialVault.default().external_cli_authority(
                "hist_prov"
            )
        finally:
            monkeypatch.setattr(_mp, "_legacy_for_config", _orig_legacy)
        if external_binding is not None:
            with pytest.raises(Exception):
                SessionLaunch(
                    session_id="sess-r23-33333333-2222-3333-4444-555555555555",
                    task_id="local-project-debug",
                    agent=agent,
                    execution_environment=env,
                    project_spec=ProjectRuntimeEnvironmentSpec(),
                    capabilities=caps,
                    profile_id="m1",
                    budgets=SessionBudgets(),
                    model_binding=model_binding,
                    credential_binding=external_binding,
                    credential_ticket="c" * 32,
                )
        # NOTE: the canonical external ref depends on transport profile;
        # use the vault authority when available, else skip to a direct
        # none-authority check below.  Build via a historical provider.
        # For determinism, test the none-authority ticket rejection with a
        # loopback no-auth provider.
        pc.add_provider_config(
            name="Loopback",
            base_url="http://127.0.0.1:9/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="loop_none",
            transport_profile=pc.TRANSPORT_GENERIC,
            auth_mode=pc.AUTH_NONE,
        )
        none_binding = CredentialVault.default().safe_binding("loop_none")
        assert none_binding is not None
        assert none_binding.source_kind == CREDENTIAL_SOURCE_NONE
        with pytest.raises(Exception):
            SessionLaunch(
                session_id="sess-r23-22222222-2222-3333-4444-555555555555",
                task_id="local-project-debug",
                agent=agent,
                execution_environment=env,
                project_spec=ProjectRuntimeEnvironmentSpec(),
                capabilities=caps,
                profile_id="m1",
                budgets=SessionBudgets(),
                model_binding=model_binding,
                credential_binding=none_binding,
                credential_ticket="b" * 32,
            )

    def test_correct_pair_remains_green_and_redeems_once(
        self, _hermetic_vault
    ) -> None:
        _configure_generic("prov_p", "https://p.example.invalid/v1")
        _hermetic_vault["prov_p"] = SECRET_P
        launch = _build_launch("prov_p")
        assert launch.credential_binding is not None
        assert launch.credential_ticket is not None
        env = ModelGateway().transport_environment(
            launch.model_binding,
            credential_binding=launch.credential_binding,
            credential_ticket=launch.credential_ticket,
        )
        assert env == {
            pc.provider_session_credential_variable("prov_p"): SECRET_P
        }
        # Exactly once: the ticket is consumed.
        with pytest.raises(CredentialUnavailableError):
            CredentialVault.default().redeem_lease(
                launch.credential_ticket,
                expected_binding=launch.credential_binding,
            )


# ---------------------------------------------------------------------------
# FINDING 2 — retained egress gated by CURRENT authority
# ---------------------------------------------------------------------------


class TestFinding2CurrentAuthorityGate:
    def test_ticket_after_identity_change_fails_stale(self, _hermetic_vault) -> None:
        _configure_generic("prov_g", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g"] = SECRET_A
        launch = _build_launch("prov_g")
        old_mb = launch.model_binding
        old_binding = launch.credential_binding
        old_ticket = launch.credential_ticket
        assert old_binding is not None and old_ticket is not None
        # Mutate ONLY the configured api_format: identity A -> B.
        pc.update_provider_config("prov_g", api_format=pc.PROTOCOL_RESPONSES)
        assert _authority("prov_g") != old_binding.provider_authority
        with pytest.raises(
            (StaleCredentialBindingError, StaleModelBindingError, CredentialVaultError)
        ) as excinfo:
            ModelGateway().transport_environment(
                old_mb, credential_binding=old_binding, credential_ticket=old_ticket
            )
        assert SECRET_A not in str(excinfo.value)

    def test_ticket_after_disable_blocks_egress(self, _hermetic_vault) -> None:
        _configure_generic("prov_g", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g"] = SECRET_A
        launch = _build_launch("prov_g")
        pc.update_provider_config("prov_g", enabled=False)
        with pytest.raises(CredentialVaultError):
            ModelGateway().transport_environment(
                launch.model_binding,
                credential_binding=launch.credential_binding,
                credential_ticket=launch.credential_ticket,
            )

    def test_ticket_after_quarantine_blocks_egress(self, _hermetic_vault) -> None:
        _configure_generic("prov_g", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g"] = SECRET_A
        launch = _build_launch("prov_g")
        pc.quarantine_provider("prov_g")
        with pytest.raises(CredentialVaultError):
            ModelGateway().transport_environment(
                launch.model_binding,
                credential_binding=launch.credential_binding,
                credential_ticket=launch.credential_ticket,
            )

    def test_value_rotation_keeps_existing_session_on_a(self, _hermetic_vault) -> None:
        _configure_generic("prov_g", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g"] = SECRET_A
        launch = _build_launch("prov_g")
        # Same identity A, durable slot rotates A -> B AFTER pinning.
        _hermetic_vault["prov_g"] = SECRET_B
        assert _authority("prov_g") == launch.credential_binding.provider_authority
        env = ModelGateway().transport_environment(
            launch.model_binding,
            credential_binding=launch.credential_binding,
            credential_ticket=launch.credential_ticket,
        )
        assert env == {pc.provider_session_credential_variable("prov_g"): SECRET_A}

    def test_new_launch_after_value_rotation_gets_b(self, _hermetic_vault) -> None:
        _configure_generic("prov_g", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g"] = SECRET_A
        first = _build_launch("prov_g")
        _hermetic_vault["prov_g"] = SECRET_B
        second = _build_launch("prov_g")
        assert second.credential_ticket is not None
        env = ModelGateway().transport_environment(
            second.model_binding,
            credential_binding=second.credential_binding,
            credential_ticket=second.credential_ticket,
        )
        assert env == {pc.provider_session_credential_variable("prov_g"): SECRET_B}
        # The first session still authenticates with A (consumed separately).
        # Build a fresh first-like launch before rotation would have A; here
        # prove the second binding carries the SAME authority (value-only
        # rotation) yet resolves the NEW value.
        assert (
            second.credential_binding.provider_authority
            == first.credential_binding.provider_authority
        )

    def test_new_launch_after_authority_change_gets_new_binding(
        self, _hermetic_vault
    ) -> None:
        _configure_generic("prov_g", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g"] = SECRET_A
        first = _build_launch("prov_g")
        old_authority = first.credential_binding.provider_authority
        # Authority mutation with explicit re-entry (accepted rebinding rule).
        pc.update_provider_config(
            "prov_g", base_url="https://b.example.invalid/v1", api_key=SECRET_B
        )
        new_authority = _authority("prov_g")
        assert new_authority != old_authority
        # Old ticket is stale and never materializes.
        with pytest.raises(
            (StaleCredentialBindingError, StaleModelBindingError, CredentialVaultError)
        ):
            ModelGateway().transport_environment(
                first.model_binding,
                credential_binding=first.credential_binding,
                credential_ticket=first.credential_ticket,
            )
        # New launch mints binding B and materializes only under B.
        second = _build_launch("prov_g")
        assert second.credential_binding.provider_authority == new_authority
        assert second.credential_binding.fingerprint() != first.credential_binding.fingerprint()
        env = ModelGateway().transport_environment(
            second.model_binding,
            credential_binding=second.credential_binding,
            credential_ticket=second.credential_ticket,
        )
        assert env == {pc.provider_session_credential_variable("prov_g"): SECRET_B}


# ---------------------------------------------------------------------------
# FINDING 3 — configured_source executable/credential coherence
# ---------------------------------------------------------------------------


class TestFinding3ConfiguredSourceAuthority:
    def test_executable_a_with_credential_b_fails_closed(self, _hermetic_vault) -> None:
        _configure_generic("prov_g2", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g2"] = SECRET_A
        from agentic_debugger.application.configured_source import (
            _resolve_registry_model,
        )

        live_config, provenance, _fp = _resolve_registry_model("prov_g2", "m1")
        assert "a.example.invalid" in str(live_config.command)
        assert provenance.get("route") == "direct_api"
        expected = _authority("prov_g2")
        # Mutate provider to B with explicit re-entry BEFORE materialization.
        pc.update_provider_config(
            "prov_g2", base_url="https://b.example.invalid/v1", api_key=SECRET_B
        )
        with pytest.raises(
            (StaleCredentialBindingError, CredentialVaultError)
        ) as excinfo:
            CredentialVault.default().transport_materialization(
                "prov_g2",
                route="direct_api",
                expected_provider_authority=expected,
            )
        assert SECRET_A not in str(excinfo.value)
        assert SECRET_B not in str(excinfo.value)

    def test_unchanged_config_materializes_a(self, _hermetic_vault) -> None:
        _configure_generic("prov_g2", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g2"] = SECRET_A
        from agentic_debugger.application.configured_source import (
            _resolve_registry_model,
        )

        live_config, provenance, _fp = _resolve_registry_model("prov_g2", "m1")
        expected = _authority("prov_g2")
        env = CredentialVault.default().transport_materialization(
            "prov_g2",
            route=str(provenance.get("route") or "direct_api"),
            expected_provider_authority=expected,
        )
        assert env == {pc.provider_session_credential_variable("prov_g2"): SECRET_A}
        assert "a.example.invalid" in str(live_config.command)

    def test_value_rotation_same_identity_new_session_gets_b(
        self, _hermetic_vault
    ) -> None:
        _configure_generic("prov_g2", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g2"] = SECRET_A
        launch = _build_launch("prov_g2")
        # Value-only rotation preserves the runtime identity.
        _hermetic_vault["prov_g2"] = SECRET_B
        assert _authority("prov_g2") == launch.credential_binding.provider_authority
        # Pinned session stays on A.
        env_old = ModelGateway().transport_environment(
            launch.model_binding,
            credential_binding=launch.credential_binding,
            credential_ticket=launch.credential_ticket,
        )
        assert env_old == {pc.provider_session_credential_variable("prov_g2"): SECRET_A}
        # Fresh configured_source resolution sees B under the same authority.
        from agentic_debugger.application.configured_source import (
            _resolve_registry_model,
        )

        _live, provenance, _fp = _resolve_registry_model("prov_g2", "m1")
        expected = _authority("prov_g2")
        env_new = CredentialVault.default().transport_materialization(
            "prov_g2",
            route=str(provenance.get("route") or "direct_api"),
            expected_provider_authority=expected,
        )
        assert env_new == {pc.provider_session_credential_variable("prov_g2"): SECRET_B}

    def test_legacy_cli_route_materializes_no_raw_secret(
        self, _hermetic_vault
    ) -> None:
        _configure_generic("prov_g2", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g2"] = SECRET_A
        vault = CredentialVault.default()
        assert (
            vault.transport_materialization("prov_g2", route="legacy_cli") is None
        )
        # Even with an expected authority pin, the legacy route carries no
        # Agentic-Debugger raw credential.
        expected = _authority("prov_g2")
        assert (
            vault.transport_materialization(
                "prov_g2",
                route="legacy_cli",
                expected_provider_authority=expected,
            )
            is None
        )

    def test_generic_direct_route_remains_direct(self, _hermetic_vault) -> None:
        _configure_generic("prov_g2", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g2"] = SECRET_A
        from agentic_debugger.application.configured_source import (
            _resolve_registry_model,
        )

        _live, provenance, _fp = _resolve_registry_model("prov_g2", "m1")
        assert provenance.get("route") == "direct_api"


# ---------------------------------------------------------------------------
# FINDING 4 — retained-ticket lifecycle
# ---------------------------------------------------------------------------


class TestFinding4TicketLifecycle:
    def test_release_ticket_is_idempotent_and_credential_free(
        self, _hermetic_vault
    ) -> None:
        _configure_generic("prov_g", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g"] = SECRET_A
        vault = CredentialVault.default()
        binding = vault.safe_binding("prov_g")
        assert binding is not None
        lease = vault.resolve_lease(binding)
        assert lease is not None
        ticket = vault.retain_lease(lease, binding)
        assert vault.release_ticket(ticket) is True
        # Idempotent: second release reports no entry, never raises.
        assert vault.release_ticket(ticket) is False
        assert vault.release_ticket("deadbeef" * 4) is False
        # Released tickets never redeem.
        with pytest.raises(CredentialUnavailableError):
            vault.redeem_lease(ticket, expected_binding=binding)

    def test_abandoned_ticket_does_not_leak_and_can_be_released(
        self, _hermetic_vault
    ) -> None:
        _configure_generic("prov_g", "https://a.example.invalid/v1")
        _hermetic_vault["prov_g"] = SECRET_A
        launch = _build_launch("prov_g")
        vault = CredentialVault.default()
        # Simulate a launch that exits before transport construction in a
        # still-live process: the ticket is retained but unredeemed.
        assert launch.credential_ticket in vault._session_leases
        assert vault.release_ticket(launch.credential_ticket) is True
        with pytest.raises(CredentialUnavailableError):
            vault.redeem_lease(
                launch.credential_ticket, expected_binding=launch.credential_binding
            )
