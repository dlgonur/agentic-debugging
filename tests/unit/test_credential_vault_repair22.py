"""V2-04 Candidate-22 REPAIR acceptance tests.

Closes the second FirstMate review's findings against Candidate 21:

- F1: explicit-binding resolution is gated on current operational state
  (missing/disabled/quarantined) BEFORE any secret access;
  already-resolved leases remain session-stable.
- F2: credential authority is ROUTE-AWARE — legacy CLI routes never
  receive an Agentic-Debugger-held API credential; incompatible pairs
  fail closed before child construction.
- F3: ModelBinding and CredentialBinding must corroborate each other
  (provider id, runtime authority, auth mode, route/source kind) at the
  SessionLaunch and ModelGateway boundaries.
- F4: source kinds are authorized by the CURRENT provider contract
  (consumable CLI store, authorized environment source, endpoint
  binding, auth-mode coherence).
- F5: forwarded session secrets carry SAFE issuance authority; an A→B
  issuance race fails stale; issuance metadata stays out of project
  roles.
- F6: no-channel session paths pin the actual SECRET at launch via an
  opaque in-process ticket; the same session keeps SECRET_A after a
  durable overwrite; a new session gets SECRET_B.
- F7: structural rejection errors never echo the rejected raw values.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.application import provider_connections as pc  # noqa: E402
from agentic_debugger.application.credential_vault import (  # noqa: E402
    CREDENTIAL_SOURCE_EXTERNAL_CLI,
    CREDENTIAL_SOURCE_FORWARDED_SESSION,
    CREDENTIAL_SOURCE_NONE,
    CREDENTIAL_SOURCE_SESSION_MEMORY,
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
    assert_credential_binding_coherent,
    provider_runtime_identity,
)

SECRET_A = "r22-synthetic-credential-alpha-not-real"
SECRET_B = "r22-synthetic-credential-beta-not-real"
ADVERSARIAL = "some-synthetic-undetected-secret-value-xyz"


@pytest.fixture(autouse=True)
def _hermetic_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY_AUTHORITY",
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY_AUTHORITY",
    ):
        monkeypatch.delenv(var, raising=False)
    pc.clear_all_session_keys()
    pc._QUARANTINED_PROVIDERS.clear()
    yield store
    pc.clear_all_session_keys()
    pc._QUARANTINED_PROVIDERS.clear()


def _configure(provider_id: str = "commandcode_goat", profile: str = pc.TRANSPORT_COMMANDCODE_GOAT,
               base_url: str = "https://api.commandcode.ai/provider/v1") -> None:
    pc.add_provider_config(
        name=f"Provider {provider_id}",
        base_url=base_url,
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id=provider_id,
        transport_profile=profile,
    )


def _authority(provider_id: str) -> str:
    cfg = pc.get_provider_config(provider_id)
    assert cfg is not None
    identity = provider_runtime_identity(cfg)
    assert identity is not None
    return identity


def _direct_binding(provider_id: str, auth_mode: str = "bearer") -> CredentialBinding:
    return CredentialBinding(
        provider_id=provider_id,
        source_kind=pc.CREDENTIAL_SOURCE_SAVED,
        source_ref=pc.credential_slot_name(provider_id),
        auth_mode=auth_mode,
        provider_authority=_authority(provider_id),
        endpoint_bound=False,
    )


def _direct_model_binding(provider_id: str) -> ModelBinding:
    return ModelGateway().resolve(provider_id, "deepseek/deepseek-v4-flash")


def _legacy_model_binding(provider_id: str, auth_mode: str = "bearer") -> ModelBinding:
    return ModelBinding(
        provider_id=provider_id,
        model_id="deepseek/deepseek-v4-flash",
        provider_model_id="deepseek/deepseek-v4-flash",
        display_name="deepseek/deepseek-v4-flash",
        route="legacy_cli",
        effective_protocol=None,
        endpoint_contract=pc.TRANSPORT_COMMANDCODE_GOAT,
        endpoint=None,
        auth_mode=auth_mode,
        config_fingerprint=None,
        tool_version="legacy-command-v1",
        provider_runtime_identity=_authority(provider_id),
    )


# ---------------------------------------------------------------------------
# FINDING 1 — operational-state gate on NEW lease creation
# ---------------------------------------------------------------------------


class TestFinding1OperationalGate:
    def test_quarantined_blocks_explicit_binding_resolution(
        self, _hermetic_vault
    ) -> None:
        _configure()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        binding = _direct_binding("commandcode_goat")
        pc.quarantine_provider("commandcode_goat")  # A
        with pytest.raises(CredentialUnavailableError) as excinfo:
            CredentialVault.default().resolve_lease(binding)
        assert SECRET_A not in str(excinfo.value)
        assert "recovery" in str(excinfo.value)

    def test_disabled_blocks_explicit_binding_resolution(
        self, _hermetic_vault
    ) -> None:
        _configure()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        binding = _direct_binding("commandcode_goat")
        pc.update_provider_config("commandcode_goat", enabled=False)  # B
        with pytest.raises(CredentialUnavailableError) as excinfo:
            CredentialVault.default().resolve_lease(binding)
        assert SECRET_A not in str(excinfo.value)
        assert "disabled" in str(excinfo.value)

    def test_resolved_lease_survives_later_operational_and_store_mutation(
        self, _hermetic_vault
    ) -> None:
        _configure()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        vault = CredentialVault.default()
        lease = vault.resolve_lease("commandcode_goat")  # C: resolve FIRST
        assert lease.reveal() == SECRET_A
        pc.quarantine_provider("commandcode_goat")
        _hermetic_vault["commandcode_goat"] = SECRET_B
        # C: the already-resolved lease keeps its fixed in-memory authority.
        assert lease.reveal() == SECRET_A
        assert lease.materialize_environment()[
            pc.provider_session_credential_variable("commandcode_goat")
        ] == SECRET_A

    def test_new_resolution_follows_current_blocked_state(
        self, _hermetic_vault
    ) -> None:
        _configure()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        binding = _direct_binding("commandcode_goat")
        vault = CredentialVault.default()
        assert vault.resolve_lease(binding).reveal() == SECRET_A
        pc.quarantine_provider("commandcode_goat")  # D
        with pytest.raises(CredentialUnavailableError):
            vault.resolve_lease(binding)
        with pytest.raises(CredentialUnavailableError):
            vault.resolve_lease("commandcode_goat")


# ---------------------------------------------------------------------------
# FINDING 2 — route-aware credential authority
# ---------------------------------------------------------------------------


class TestFinding2RouteAwareAuthority:
    def test_legacy_launch_session_authority_is_external_only(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure()
        _hermetic_vault["commandcode_goat"] = SECRET_A  # A: direct credential exists
        from agentic_debugger.application import model_providers as mp

        monkeypatch.setattr(mp, "_commandcode_availability", lambda: (True, None))
        authority = CredentialVault.default().session_authority(
            "commandcode_goat", route="legacy_cli"
        )
        # A: the legacy launch carries the safe EXTERNAL authority only —
        # never a materializable direct credential authority.
        assert authority is not None
        assert authority.source_kind == CREDENTIAL_SOURCE_EXTERNAL_CLI

    def test_legacy_route_never_materializes_direct_credential(
        self, _hermetic_vault
    ) -> None:
        _configure()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        vault = CredentialVault.default()
        # Even a FRESH resolution on the legacy route materializes nothing.
        assert vault.transport_materialization(
            "commandcode_goat", route="legacy_cli"
        ) is None
        # And an explicitly supplied direct binding is refused, not leaked.
        binding = _direct_binding("commandcode_goat")
        with pytest.raises(CredentialRouteError):
            vault.transport_materialization(
                "commandcode_goat",
                route="legacy_cli",
                credential_binding=binding,
            )

    def test_gateway_rejects_direct_binding_on_legacy_route(
        self, _hermetic_vault
    ) -> None:
        _configure()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        gateway = ModelGateway()
        legacy_binding = _legacy_model_binding("commandcode_goat")
        direct_credential = _direct_binding("commandcode_goat")  # B
        with pytest.raises((CredentialRouteError, IncoherentCredentialBindingError)):
            gateway.transport_environment(
                legacy_binding, credential_binding=direct_credential
            )
        with pytest.raises((CredentialRouteError, IncoherentCredentialBindingError)):
            gateway.create_transport(
                legacy_binding, credential_binding=direct_credential
            )  # no child starts

    def test_profile_route_rejects_registry_credential(
        self, _hermetic_vault
    ) -> None:
        _configure()
        with pytest.raises(CredentialRouteError):
            CredentialVault.default().transport_materialization(
                "commandcode_goat",
                route="qualified_ladder",
                credential_binding=_direct_binding("commandcode_goat"),
            )


# ---------------------------------------------------------------------------
# FINDING 3 — ModelBinding/CredentialBinding coherence
# ---------------------------------------------------------------------------


class TestFinding3PairCoherence:
    def test_cross_provider_pair_fails_before_materialization(
        self, _hermetic_vault
    ) -> None:
        _configure("commandcode_goat")
        _configure("other_gateway_r22", profile=pc.TRANSPORT_GENERIC,
                   base_url="https://other-r22.example.invalid/v1")
        _hermetic_vault["commandcode_goat"] = SECRET_A
        model_p = _direct_model_binding("commandcode_goat")
        credential_q = _direct_binding("other_gateway_r22")  # Q
        gateway = ModelGateway()
        with pytest.raises(IncoherentCredentialBindingError):  # A
            gateway.transport_environment(model_p, credential_binding=credential_q)
        with pytest.raises(IncoherentCredentialBindingError):
            gateway.create_transport(model_p, credential_binding=credential_q)
        # Q's secret is absent from any P child environment.
        env = gateway.transport_environment(model_p)
        assert env is None or all(
            pc.provider_session_credential_variable("other_gateway_r22") != key
            for key in env
        )

    def test_same_provider_different_authority_fails_stale(
        self, _hermetic_vault
    ) -> None:
        _configure()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        model_binding = _direct_model_binding("commandcode_goat")
        stale_credential = CredentialBinding(
            provider_id="commandcode_goat",
            source_kind=pc.CREDENTIAL_SOURCE_SAVED,
            source_ref=pc.credential_slot_name("commandcode_goat"),
            auth_mode="bearer",
            provider_authority="f" * 64,  # B: different authority
            endpoint_bound=False,
        )
        with pytest.raises(StaleModelBindingError):
            ModelGateway().transport_environment(
                model_binding, credential_binding=stale_credential
            )

    def test_direct_model_with_external_credential_fails(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentic_debugger.application import model_providers as mp

        _configure()
        _hermetic_vault["commandcode_goat"] = SECRET_A  # the direct route resolves
        monkeypatch.setattr(mp, "_commandcode_availability", lambda: (True, None))
        external = CredentialVault.default().external_cli_authority("commandcode_goat")
        assert external is not None
        model_binding = _direct_model_binding("commandcode_goat")  # C
        with pytest.raises((IncoherentCredentialBindingError, CredentialRouteError)):
            ModelGateway().transport_environment(
                model_binding, credential_binding=external
            )

    def test_valid_direct_pair_remains_green(self, _hermetic_vault) -> None:
        _configure()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        model_binding = _direct_model_binding("commandcode_goat")
        credential = _direct_binding("commandcode_goat")
        env = ModelGateway().transport_environment(
            model_binding, credential_binding=credential
        )  # E
        assert env == {
            pc.provider_session_credential_variable("commandcode_goat"): SECRET_A
        }


# ---------------------------------------------------------------------------
# FINDING 4 — source kind authorized by the CURRENT provider contract
# ---------------------------------------------------------------------------


class TestFinding4ContractAuthorization:
    def test_generic_provider_cannot_fabricate_cli_auth_store_binding(
        self, _hermetic_vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure("generic_r22", profile=pc.TRANSPORT_GENERIC,
                   base_url="https://generic-r22.example.invalid/v1")
        store = tmp_path / "oc-auth"
        store.mkdir()
        (store / "auth.json").write_text(
            json.dumps({"opencode-go": {"type": "api", "key": SECRET_A}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: store / "auth.json")
        from agentic_debugger.application.credential_vault import (
            _cli_auth_store_location_fingerprint,
        )

        fabricated = CredentialBinding(
            provider_id="generic_r22",
            source_kind=pc.CREDENTIAL_SOURCE_CLI_AUTH_STORE,
            source_ref=_cli_auth_store_location_fingerprint(),
            auth_mode="bearer",
            provider_authority=_authority("generic_r22"),
            endpoint_bound=True,
        )
        # A: fails BEFORE reading the CLI auth-store contents.
        with pytest.raises(CredentialUnavailableError):
            CredentialVault.default().resolve_lease(fabricated)

    def test_commandcode_cannot_consume_opencode_store(
        self, _hermetic_vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure()  # commandcode_goat: auth_store_consumable is False  (B)
        store = tmp_path / "oc-auth"
        store.mkdir()
        (store / "auth.json").write_text(
            json.dumps({"opencode-go": {"type": "api", "key": SECRET_A}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: store / "auth.json")
        from agentic_debugger.application.credential_vault import (
            _canonical_source_ref,
        )

        fabricated = CredentialBinding(
            provider_id="commandcode_goat",
            source_kind=pc.CREDENTIAL_SOURCE_CLI_AUTH_STORE,
            source_ref=_canonical_source_ref(
                "commandcode_goat", pc.CREDENTIAL_SOURCE_CLI_AUTH_STORE
            ),
            auth_mode="bearer",
            provider_authority=_authority("commandcode_goat"),
            endpoint_bound=True,
        )
        with pytest.raises(CredentialUnavailableError):
            CredentialVault.default().resolve_lease(fabricated)

    def test_opencode_consumable_store_remains_green(
        self, _hermetic_vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(
            "opencode_r22",
            profile=pc.TRANSPORT_OPENCODE_GO,
            base_url="https://opencode.ai/zen/go/v1",
        )  # C: accepted consumable contract + canonical endpoint
        store = tmp_path / "oc-auth"
        store.mkdir()
        (store / "auth.json").write_text(
            json.dumps({"opencode-go": {"type": "api", "key": SECRET_A}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: store / "auth.json")
        binding = CredentialVault.default().safe_binding("opencode_r22")
        assert binding is not None
        assert binding.source_kind == pc.CREDENTIAL_SOURCE_CLI_AUTH_STORE
        assert CredentialVault.default().resolve_lease(binding).reveal() == SECRET_A

    def test_environment_binding_fails_after_endpoint_drift(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure()
        monkeypatch.setenv("COMMAND_CODE_API_KEY", SECRET_A)
        from agentic_debugger.application.credential_vault import (
            _canonical_source_ref,
        )

        binding = CredentialBinding(
            provider_id="commandcode_goat",
            source_kind=pc.CREDENTIAL_SOURCE_ENVIRONMENT,
            source_ref=_canonical_source_ref(
                "commandcode_goat", pc.CREDENTIAL_SOURCE_ENVIRONMENT
            ),
            auth_mode="bearer",
            provider_authority=_authority("commandcode_goat"),
            endpoint_bound=True,
        )
        assert CredentialVault.default().resolve_lease(binding).reveal() == SECRET_A
        # D: canonical endpoint drift invalidates the ambient source —
        # even for a previously valid explicit binding — BEFORE the read.
        pc.update_provider_config(
            "commandcode_goat",
            base_url="https://moved-r22.example.invalid/v1",
            api_key=SECRET_B,
        )
        with pytest.raises((CredentialUnavailableError, StaleCredentialBindingError)):
            CredentialVault.default().resolve_lease(binding)

    def test_environment_binding_requires_authorized_contract_source(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pc.add_provider_config(
            name="Generic R22b",
            base_url="https://generic-r22b.example.invalid/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="generic_r22b",
        )  # E: generic contract exposes NO authorized environment source
        monkeypatch.setenv("COMMAND_CODE_API_KEY", SECRET_A)
        # A fabricated binding naming a FOREIGN env var cannot even be
        # structurally formed (the canonical env ref is contract-derived):
        with pytest.raises(CredentialVaultError):
            CredentialBinding(
                provider_id="generic_r22b",
                source_kind=pc.CREDENTIAL_SOURCE_ENVIRONMENT,
                source_ref="COMMAND_CODE_API_KEY",
                auth_mode="bearer",
                provider_authority=_authority("generic_r22b"),
                endpoint_bound=True,
            )
        # And a structurally formable env binding (no contract env var ->
        # canonical ref None) still fails at RESOLUTION time: the current
        # contract authorizes no environment source.
        fabricated = CredentialBinding(
            provider_id="generic_r22b",
            source_kind=pc.CREDENTIAL_SOURCE_ENVIRONMENT,
            source_ref=None,
            auth_mode="bearer",
            provider_authority=_authority("generic_r22b"),
            endpoint_bound=True,
        )
        with pytest.raises(CredentialUnavailableError):
            CredentialVault.default().resolve_lease(fabricated)

    def test_none_source_contradictions_fail(
        self, _hermetic_vault
    ) -> None:
        _configure()
        # F: bearer auth + source_kind="none" cannot be constructed.
        with pytest.raises(CredentialVaultError):
            CredentialBinding(
                provider_id="commandcode_goat",
                source_kind=CREDENTIAL_SOURCE_NONE,
                source_ref=None,
                auth_mode="bearer",
                provider_authority=_authority("commandcode_goat"),
                endpoint_bound=False,
            )
        # G: AUTH_NONE + a raw secret source cannot be constructed.
        with pytest.raises(CredentialVaultError):
            CredentialBinding(
                provider_id="commandcode_goat",
                source_kind=pc.CREDENTIAL_SOURCE_SAVED,
                source_ref=pc.credential_slot_name("commandcode_goat"),
                auth_mode=pc.AUTH_NONE,
                provider_authority=_authority("commandcode_goat"),
                endpoint_bound=False,
            )


# ---------------------------------------------------------------------------
# FINDING 5 — forwarded secret carries safe issuance authority
# ---------------------------------------------------------------------------


class TestFinding5IssuanceAuthority:
    def _issue(self, monkeypatch: pytest.MonkeyPatch, identity: str) -> str:
        channel = pc.provider_session_credential_variable("commandcode_goat")
        authority_var = pc.provider_session_credential_authority_variable(
            "commandcode_goat"
        )
        monkeypatch.setenv(channel, SECRET_A)
        monkeypatch.setenv(authority_var, identity)
        return channel

    def test_forwarded_secret_issued_under_a_rejected_for_b(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure()
        self._issue(monkeypatch, _authority("commandcode_goat"))  # issued under A
        # Mutate the durable configuration to identity B AFTER issuance
        # (an api_format change alters the runtime identity without
        # triggering the endpoint-rebinding credential guard — exactly the
        # mid-flight external mutation the issuance gate exists for).
        pc.update_provider_config("commandcode_goat", api_format=pc.PROTOCOL_MESSAGES)
        # A: the worker session boundary fails stale; SECRET_A never
        # reaches a B transport and no lease under B is minted.
        with pytest.raises(StaleCredentialBindingError):
            CredentialVault.default().session_authority(
                "commandcode_goat", route="direct_api"
            )

    def test_forwarded_path_green_when_identity_unchanged(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure()
        channel = self._issue(monkeypatch, _authority("commandcode_goat"))  # B
        authority = CredentialVault.default().session_authority(
            "commandcode_goat", route="direct_api"
        )
        assert authority is not None
        assert authority.source_kind == CREDENTIAL_SOURCE_FORWARDED_SESSION
        assert authority.provider_authority == _authority("commandcode_goat")
        env = CredentialVault.default().transport_materialization(
            "commandcode_goat",
            route="direct_api",
            credential_binding=authority,
        )
        assert env == {channel: SECRET_A}

    def test_channel_without_valid_issuance_provenance_fails_closed(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure()
        channel = pc.provider_session_credential_variable("commandcode_goat")
        monkeypatch.setenv(channel, SECRET_A)  # no companion metadata
        with pytest.raises((CredentialUnavailableError, StaleCredentialBindingError)):
            CredentialVault.default().session_authority(
                "commandcode_goat", route="direct_api"
            )
        # And fresh minting refuses to stamp the unprovenanced channel.
        assert CredentialVault.default().safe_binding("commandcode_goat") is None

    def test_issuance_metadata_never_reaches_project_roles(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentic_debugger.application.execution_environment import (
            ExecutionEnvironment,
            ExecutionRole,
        )
        from agentic_debugger.application.session_runtime import (
            ProjectRuntimeEnvironmentSpec,
        )

        _configure()
        channel = self._issue(monkeypatch, _authority("commandcode_goat"))  # C
        authority_var = pc.provider_session_credential_authority_variable(
            "commandcode_goat"
        )
        # The V2-01 classification owns both names (structurally excluded).
        names = pc.provider_authority_environment_names()
        assert channel in names and authority_var in names
        snapshot = dict(os.environ)
        env = ExecutionEnvironment.for_local_project(
            snapshot, ProjectRuntimeEnvironmentSpec()
        )
        for role in (
            ExecutionRole.PROJECT_COMMAND,
            ExecutionRole.PRODUCT_PDB,
            ExecutionRole.VERIFIER,
        ):
            role_env = env.role_environment(role)
            assert channel not in role_env
            assert authority_var not in role_env


# ---------------------------------------------------------------------------
# FINDING 6 — no-channel session paths pin the actual secret at launch
# ---------------------------------------------------------------------------


class TestFinding6NoChannelSessionStability:
    def test_saved_launch_pins_secret_across_slot_overwrite(
        self, _hermetic_vault
    ) -> None:
        _configure()
        _hermetic_vault["commandcode_goat"] = SECRET_A  # 1
        from agentic_debugger.application.session_runtime import (
            ProjectRuntimeEnvironmentSpec,
            build_local_project_launch,
            local_project_model_call_ceiling,
        )

        launch = build_local_project_launch(  # 2
            session_id="sess-r22-nochannel",
            task_id="local-project-debug",
            policy="static-baseline",
            provider_id="commandcode_goat",
            model_id="deepseek/deepseek-v4-flash",
            profile_id="deepseek/deepseek-v4-flash",
            launch_snapshot=dict(os.environ),
            project_spec=ProjectRuntimeEnvironmentSpec(),
        )
        assert launch.credential_binding is not None
        assert launch.credential_binding.source_kind == pc.CREDENTIAL_SOURCE_SAVED
        assert launch.credential_ticket is not None
        # 3: durable slot overwritten BEFORE create_transport.
        _hermetic_vault["commandcode_goat"] = SECRET_B
        gateway = ModelGateway()
        # Task-26: materialize the transport under the SAME session-owned
        # model-call ceiling the launch resolved its ModelBinding with.
        transport, _config = gateway.create_transport(
            launch.model_binding,
            max_model_requests=local_project_model_call_ceiling(launch.budgets),
            credential_binding=launch.credential_binding,
            credential_ticket=launch.credential_ticket,
        )
        # The transport holds the LAUNCH-pinned SECRET_A.
        assert transport._environment == {
            pc.provider_session_credential_variable("commandcode_goat"): SECRET_A
        }
        # 5: a NEW session (fresh launch) resolves SECRET_B.
        new_launch = build_local_project_launch(
            session_id="sess-r22-nochannel-2",
            task_id="local-project-debug",
            policy="static-baseline",
            provider_id="commandcode_goat",
            model_id="deepseek/deepseek-v4-flash",
            profile_id="deepseek/deepseek-v4-flash",
            launch_snapshot=dict(os.environ),
            project_spec=ProjectRuntimeEnvironmentSpec(),
        )
        assert new_launch.credential_ticket is not None
        env = ModelGateway().transport_environment(
            new_launch.model_binding,
            credential_binding=new_launch.credential_binding,
            credential_ticket=new_launch.credential_ticket,
        )
        assert env == {
            pc.provider_session_credential_variable("commandcode_goat"): SECRET_B
        }  # 6/16

    def test_environment_backed_launch_snapshots_value(
        self, _hermetic_vault, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure()
        monkeypatch.setenv("COMMAND_CODE_API_KEY", SECRET_A)
        from agentic_debugger.application.session_runtime import (
            ProjectRuntimeEnvironmentSpec,
            build_local_project_launch,
        )

        launch = build_local_project_launch(
            session_id="sess-r22-env",
            task_id="local-project-debug",
            policy="static-baseline",
            provider_id="commandcode_goat",
            model_id="deepseek/deepseek-v4-flash",
            profile_id="deepseek/deepseek-v4-flash",
            launch_snapshot=dict(os.environ),
            project_spec=ProjectRuntimeEnvironmentSpec(),
        )
        assert launch.credential_binding.source_kind == pc.CREDENTIAL_SOURCE_ENVIRONMENT
        monkeypatch.setenv("COMMAND_CODE_API_KEY", SECRET_B)
        env = ModelGateway().transport_environment(
            launch.model_binding,
            credential_binding=launch.credential_binding,
            credential_ticket=launch.credential_ticket,
        )
        assert env == {
            pc.provider_session_credential_variable("commandcode_goat"): SECRET_A
        }

    def test_ticket_excluded_from_launch_provenance_and_redeems_once(
        self, _hermetic_vault
    ) -> None:
        _configure()
        _hermetic_vault["commandcode_goat"] = SECRET_A
        from agentic_debugger.application.session_runtime import (
            ProjectRuntimeEnvironmentSpec,
            build_local_project_launch,
        )

        launch = build_local_project_launch(
            session_id="sess-r22-ticket",
            task_id="local-project-debug",
            policy="static-baseline",
            provider_id="commandcode_goat",
            model_id="deepseek/deepseek-v4-flash",
            profile_id="deepseek/deepseek-v4-flash",
            launch_snapshot=dict(os.environ),
            project_spec=ProjectRuntimeEnvironmentSpec(),
        )
        assert launch.credential_ticket is not None
        assert launch.credential_ticket not in json.dumps(launch.to_mapping())
        vault = CredentialVault.default()
        lease = vault.redeem_lease(
            launch.credential_ticket, expected_binding=launch.credential_binding
        )
        assert isinstance(lease, CredentialLease)
        with pytest.raises(CredentialUnavailableError):
            vault.redeem_lease(launch.credential_ticket)  # exactly once


# ---------------------------------------------------------------------------
# FINDING 7 — structural errors never echo rejected values
# ---------------------------------------------------------------------------


class TestFinding7ErrorEchoSafety:
    def _assert_absent(self, exc: BaseException) -> None:
        assert ADVERSARIAL not in str(exc)
        assert ADVERSARIAL not in repr(exc)

    def test_invalid_source_ref_not_echoed(self) -> None:
        _configure()
        with pytest.raises(CredentialVaultError) as excinfo:
            CredentialBinding(
                provider_id="commandcode_goat",
                source_kind=pc.CREDENTIAL_SOURCE_SAVED,
                source_ref=ADVERSARIAL,
                auth_mode="bearer",
                provider_authority=_authority("commandcode_goat"),
                endpoint_bound=False,
            )
        self._assert_absent(excinfo.value)

    def test_invalid_source_kind_not_echoed(self) -> None:
        _configure()
        with pytest.raises(CredentialVaultError) as excinfo:
            CredentialBinding(
                provider_id="commandcode_goat",
                source_kind=ADVERSARIAL,
                source_ref=None,
                auth_mode="bearer",
                provider_authority=_authority("commandcode_goat"),
                endpoint_bound=False,
            )
        self._assert_absent(excinfo.value)

    def test_invalid_auth_mode_not_echoed(self) -> None:
        _configure()
        with pytest.raises(CredentialVaultError) as excinfo:
            CredentialBinding(
                provider_id="commandcode_goat",
                source_kind=pc.CREDENTIAL_SOURCE_SAVED,
                source_ref=pc.credential_slot_name("commandcode_goat"),
                auth_mode=ADVERSARIAL,
                provider_authority=_authority("commandcode_goat"),
                endpoint_bound=False,
            )
        self._assert_absent(excinfo.value)

    def test_invalid_provider_authority_not_echoed(self) -> None:
        _configure()
        with pytest.raises(CredentialVaultError) as excinfo:
            CredentialBinding(
                provider_id="commandcode_goat",
                source_kind=pc.CREDENTIAL_SOURCE_SAVED,
                source_ref=pc.credential_slot_name("commandcode_goat"),
                auth_mode="bearer",
                provider_authority=ADVERSARIAL,
                endpoint_bound=False,
            )
        self._assert_absent(excinfo.value)

    def test_invalid_provider_id_not_echoed(self) -> None:
        with pytest.raises(CredentialVaultError) as excinfo:
            CredentialBinding(
                provider_id=ADVERSARIAL,
                source_kind=pc.CREDENTIAL_SOURCE_SAVED,
                source_ref="irrelevant",
                auth_mode="bearer",
                provider_authority="a" * 64,
                endpoint_bound=False,
            )
        self._assert_absent(excinfo.value)

    def test_from_mapping_payloads_not_echoed(self) -> None:
        _configure()
        with pytest.raises(CredentialVaultError) as excinfo:
            CredentialBinding.from_mapping(
                {
                    "provider_id": "commandcode_goat",
                    "source_kind": pc.CREDENTIAL_SOURCE_SAVED,
                    "source_ref": ADVERSARIAL,
                    "auth_mode": "bearer",
                    "provider_authority": _authority("commandcode_goat"),
                    "endpoint_bound": False,
                }
            )
        self._assert_absent(excinfo.value)

    def test_actionable_safe_errors_preserved(self) -> None:
        _configure()
        with pytest.raises(CredentialVaultError) as excinfo:
            CredentialBinding(
                provider_id="commandcode_goat",
                source_kind=pc.CREDENTIAL_SOURCE_SAVED,
                source_ref="wrong",
                auth_mode="bearer",
                provider_authority=_authority("commandcode_goat"),
                endpoint_bound=False,
            )
        assert "canonical" in str(excinfo.value)
