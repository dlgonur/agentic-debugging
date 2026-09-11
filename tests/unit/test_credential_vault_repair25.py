"""V2-04 Candidate-25 REPAIR acceptance tests.

- Mixed-snapshot ABA: every authority-relevant executable fact comes from ONE
  immutable ProviderConfig snapshot.  A real A->B->A mutation DURING resolver
  execution cannot inject B protocol/route facts into an A executable.
- Mandatory executable authority: real direct provenance without a
  well-formed provider_runtime_identity fails closed (no CURRENT fallback);
  fakes carry synthetic authority.  ModelGateway likewise rejects real
  direct executables without authority.
- Candidate-24 F1 issuance binding preserved.

Synthetic only, no network, no secret in errors.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.application import provider_connections as pc  # noqa: E402
from agentic_debugger.application.credential_vault import (  # noqa: E402
    CredentialVault,
    CredentialVaultError,
)
from agentic_debugger.application.model_gateway import (  # noqa: E402
    ModelGateway,
    ProviderConfigurationError,
    provider_runtime_identity,
)

SECRET_A = "r25-synthetic-credential-alpha-not-real"
SECRET_B = "r25-synthetic-credential-beta-not-real"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(tmp_path / "c.json"))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(tmp_path / "q.json"))
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
    for var in ("COMMAND_CODE_API_KEY", "OPENCODE_API_KEY", "OLLAMA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    pc.clear_all_session_keys()
    pc._QUARANTINED_PROVIDERS.clear()
    CredentialVault.default()._session_leases.clear()
    yield store
    pc.clear_all_session_keys()
    pc._QUARANTINED_PROVIDERS.clear()
    CredentialVault.default()._session_leases.clear()


def _configure(provider_id: str, base_url: str, api_format: str) -> None:
    pc.add_provider_config(
        name=f"Provider {provider_id}",
        base_url=base_url,
        api_format=api_format,
        provider_id=provider_id,
        transport_profile=pc.TRANSPORT_GENERIC,
    )


def _authority(provider_id: str) -> str:
    cfg = pc.get_provider_config(provider_id)
    assert cfg is not None
    ident = provider_runtime_identity(cfg)
    assert ident is not None
    return ident


class TestSingleSnapshotExecutable:
    def test_aba_during_protocol_resolution_cannot_mix(
        self, _hermetic, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deterministic DURING-resolution ABA: mutate A->B inside a helper the
        # resolver genuinely calls (_direct_runnable, hit by both Candidate 24
        # and repaired code after the entry snapshot), leave B active through
        # protocol resolution, restore B->A before asserting.  Candidate 24
        # folds the B protocol (responses) into an A executable; repaired code
        # builds protocol purely from the entry snapshot (chat).
        _configure("prov_p", "https://a.example.invalid/v1", pc.PROTOCOL_CHAT_COMPLETIONS)
        _hermetic["prov_p"] = SECRET_A
        auth_a = _authority("prov_p")
        import agentic_debugger.application.model_providers as mp

        real_runnable = mp._direct_runnable
        mutated = {"done": False}

        def mutating_runnable(kind: str):
            if kind == "prov_p" and not mutated["done"]:
                mutated["done"] = True
                # A(chat) -> B(responses); endpoint unchanged isolates protocol.
                # api_format change needs no credential re-entry (blank-key rule
                # only guards endpoint/auth changes).
                pc.update_provider_config("prov_p", api_format=pc.PROTOCOL_RESPONSES)
            return real_runnable(kind)

        monkeypatch.setattr(mp, "_direct_runnable", mutating_runnable)
        try:
            from agentic_debugger.application.configured_source import (
                _resolve_registry_model,
            )

            live, provenance, _fp = _resolve_registry_model("prov_p", "m1")
        finally:
            try:
                pc.update_provider_config("prov_p", api_format=pc.PROTOCOL_CHAT_COMPLETIONS)
            except Exception:
                pass
        assert mutated["done"], "mutation point was never hit"
        # Repaired executable must be entirely A: protocol chat (not responses),
        # endpoint A, authority A.  Candidate 24 mixed protocol B into authority A.
        assert provenance.get("api_protocol") == pc.PROTOCOL_CHAT_COMPLETIONS
        assert provenance.get("endpoint") == "https://a.example.invalid/v1"
        exe_auth = provenance.get("provider_runtime_identity")
        assert exe_auth == auth_a
        assert _authority("prov_p") == auth_a
        # And that coherent A executable authorizes A credential (no B pairing).
        env = CredentialVault.default().transport_materialization(
            "prov_p", route="direct_api", expected_provider_authority=exe_auth
        )
        assert env == {pc.provider_session_credential_variable("prov_p"): SECRET_A}

    def test_missing_provenance_authority_fails_closed_configured_source(
        self, _hermetic, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _configure("prov_p", "https://a.example.invalid/v1", pc.PROTOCOL_CHAT_COMPLETIONS)
        _hermetic["prov_p"] = SECRET_A
        from agentic_debugger.application import model_providers as mp
        from agentic_debugger.application.configured_source import run_configured_session
        from agentic_debugger.application.emitter import SessionEventEmitter
        from agentic_debugger.application.journal import SessionEventJournal
        from agentic_debugger.application.events import SourceKind
        from agentic_debugger.application.worker_scenarios import ScenarioContext
        from agentic_debugger.cancellation import CancellationToken
        from agentic_debugger.evaluation.live import LiveModelConfig

        def fake_no_auth(provider: str, model_id: str, **kw):
            cfg = LiveModelConfig(
                model_name=model_id, command=("echo", "hi"),
                request_timeout_seconds=30, tool_version="test",
            )
            # Real direct provenance WITHOUT authority proof (regression or stale fake).
            return cfg, {
                "display_name": model_id, "route": "direct_api",
                "api_protocol": "chat_completions", "provider_model_id": model_id,
                "endpoint": "https://a.example.invalid/v1",
            }

        monkeypatch.setattr(mp, "resolve_provider_live_config", fake_no_auth)
        journal = SessionEventJournal(
            tmp_path / "j.events.jsonl", session_id="sess-missing-auth",
            task_id="curated-off-by-one-002", source_kind=SourceKind.CONFIGURED_MODEL,
        )
        emitter = SessionEventEmitter(
            sink=journal, session_id="sess-missing-auth",
            task_id="curated-off-by-one-002", source_kind=SourceKind.CONFIGURED_MODEL,
        )
        ctx = ScenarioContext(work_dir=tmp_path / "w", emitter=emitter, token=CancellationToken())
        with pytest.raises(Exception) as excinfo:
            run_configured_session(
                ctx, {"provider": "prov_p", "model_id": "m1", "policy": "pdb-on-uncertainty"}
            )
        assert SECRET_A not in str(excinfo.value)
        assert SECRET_B not in str(excinfo.value)

    def test_gateway_rejects_direct_provenance_without_authority(
        self, _hermetic, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _configure("prov_p", "https://a.example.invalid/v1", pc.PROTOCOL_CHAT_COMPLETIONS)
        _hermetic["prov_p"] = SECRET_A
        from agentic_debugger.application import model_providers as mp
        from agentic_debugger.evaluation.live import LiveModelConfig

        def fake_no_auth(provider: str, model_id: str, **kw):
            cfg = LiveModelConfig(
                model_name=model_id, command=("echo", "hi"),
                request_timeout_seconds=30, tool_version="test",
            )
            return cfg, {
                "display_name": model_id, "route": "direct_api",
                "api_protocol": "chat_completions", "provider_model_id": model_id,
                "endpoint": "https://a.example.invalid/v1",
            }

        monkeypatch.setattr(mp, "resolve_provider_live_config", fake_no_auth)
        gateway = ModelGateway(config_root=tmp_path)
        with pytest.raises(ProviderConfigurationError):
            gateway.resolve("prov_p", "m1")

    def test_f1_issuance_binding_preserved(self, _hermetic) -> None:
        from agentic_debugger.application.model_gateway import StaleCredentialBindingError

        _configure("prov_p", "https://a.example.invalid/v1", pc.PROTOCOL_CHAT_COMPLETIONS)
        _hermetic["prov_p"] = SECRET_A
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
        with pytest.raises(CredentialVaultError):
            vault.retain_lease(lease_a, binding_b)
        # Legit path still green.
        lease_b = vault.resolve_lease(binding_b)
        assert lease_b is not None
        ticket_b = vault.retain_lease(lease_b, binding_b)
        model_b = ModelGateway().resolve("prov_p", "m1")
        env = ModelGateway().transport_environment(
            model_b, credential_binding=binding_b, credential_ticket=ticket_b
        )
        assert env == {pc.provider_session_credential_variable("prov_p"): SECRET_B}
