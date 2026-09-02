"""Unit gates for route-aware provider resolution and the Capability
Ladder fail-closed regression.

Covers the deterministic route decision (direct_api vs legacy_cli),
fail-closed messages, credential-free provenance, the bounded transport
environment overrides, dynamic-catalog picker integration, and —
critically — that dynamic discovery NEVER makes a provider model
scientifically selectable on the Capability Ladder.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.application import model_providers as mp  # noqa: E402
from agentic_debugger.application import provider_connections as pc  # noqa: E402
from agentic_debugger.application.provider_connections import (  # noqa: E402
    DiscoveredProviderModel,
    ProviderCatalogSnapshot,
)

SECRET = "route-test-credential-not-real"


@pytest.fixture(autouse=True)
def _clean_session_keys():
    pc.clear_all_session_keys()
    yield
    pc.clear_all_session_keys()


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pc, "catalog_cache_path", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: tmp_path / "missing-auth.json")
    monkeypatch.setattr(
        pc, "provider_configurations_path", lambda: tmp_path / "provider-configurations.json"
    )
    for name in (
        "OPENCODE_API_KEY",
        "COMMAND_CODE_API_KEY",
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    # The provider store is user-owned (no auto-seeded builtins): create
    # the two builtin direct-API providers explicitly, as production does.
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
    )
    pc.add_provider_config(
        name="OpenCode Go",
        base_url="https://opencode.ai/provider/v1",
        api_format=pc.PROTOCOL_RESPONSES,
        provider_id="opencode_go",
    )


def _cached_snapshot(kind: str, model_ids: list[str]) -> ProviderCatalogSnapshot:
    return ProviderCatalogSnapshot(
        kind=kind,
        fetched_at_utc="2026-08-30T00:00:00Z",
        source="live",
        models=tuple(
            DiscoveredProviderModel.create(kind, model_id, model_id)
            for model_id in model_ids
        ),
    )


# -- route decisions ------------------------------------------------------------


class TestRouteDecision:
    def test_direct_route_when_protocol_resolved_and_credential_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mp, "credential_source_for", lambda kind: "session_key")
        config, provenance = mp.resolve_provider_live_config(
            "commandcode_goat", "deepseek/deepseek-v4-flash"
        )
        assert provenance["route"] == mp.ROUTE_DIRECT_API
        assert provenance["api_protocol"] == pc.PROTOCOL_CHAT_COMPLETIONS
        assert provenance["provider_model_id"] == "deepseek/deepseek-v4-flash"
        model_argument = config.command[config.command.index("--model") + 1]
        assert model_argument == "deepseek/deepseek-v4-flash"
        assert provenance["endpoint"] == "https://api.commandcode.ai/provider/v1"
        assert "provider_direct_api_adapter.py" in " ".join(config.command)
        assert config.tool_version == "provider-direct-api-adapter-v1"

    def test_direct_route_prefers_opencode_auth_store(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = tmp_path / "auth.json"
        store.write_text(
            json.dumps({"opencode-go": {"type": "api", "key": "store-key"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: store)
        monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
        config, provenance = mp.resolve_provider_live_config(
            "opencode_go", "opencode-go/deepseek-v4-flash"
        )
        assert provenance["route"] == mp.ROUTE_DIRECT_API
        assert provenance["api_protocol"] == pc.PROTOCOL_CHAT_COMPLETIONS
        assert provenance["provider_model_id"] == "deepseek-v4-flash"
        model_argument = config.command[config.command.index("--model") + 1]
        assert model_argument == "deepseek-v4-flash"

    def test_legacy_route_for_unresolved_protocol_when_cli_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mp, "credential_source_for", lambda kind: "session_key")
        monkeypatch.setattr(mp, "_opencode_availability", lambda: (True, None))
        config, provenance = mp.resolve_provider_live_config(
            "opencode_go", "opencode-go/glm-5"
        )
        assert provenance["route"] == mp.ROUTE_LEGACY_CLI
        assert "api_protocol" not in provenance
        assert "opencode_provider_adapter.py" in " ".join(config.command)

    def test_legacy_route_when_no_direct_credential_but_cli_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mp, "credential_source_for", lambda kind: None)
        monkeypatch.setattr(mp, "_commandcode_availability", lambda: (True, None))
        config, provenance = mp.resolve_provider_live_config(
            "commandcode_goat", "deepseek/deepseek-v4-flash"
        )
        assert provenance["route"] == mp.ROUTE_LEGACY_CLI

    def test_fail_closed_when_no_route_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mp, "credential_source_for", lambda kind: None)
        monkeypatch.setattr(mp, "_opencode_availability", lambda: (False, "opencode CLI not found on PATH"))
        with pytest.raises(mp.ProviderRegistryError) as excinfo:
            mp.resolve_provider_live_config(
                "opencode_go", "opencode-go/deepseek-v4-flash"
            )
        message = str(excinfo.value)
        assert "no usable credential source" in message
        assert "opencode CLI not found" in message

    def test_fail_closed_for_unresolved_protocol_without_cli(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mp, "credential_source_for", lambda kind: "session_key")
        monkeypatch.setattr(mp, "_opencode_availability", lambda: (False, "no auth"))
        with pytest.raises(mp.ProviderRegistryError) as excinfo:
            mp.resolve_provider_live_config("opencode_go", "opencode-go/glm-5")
        message = str(excinfo.value)
        assert "no resolved direct-API protocol" in message
        assert "no auth" in message

    def test_provenance_is_credential_free(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mp, "credential_source_for", lambda kind: "session_key")
        _, provenance = mp.resolve_provider_live_config(
            "commandcode_goat", "deepseek/deepseek-v4-flash"
        )
        rendered = json.dumps(provenance)
        assert SECRET not in rendered
        for forbidden in ("key", "token", "authorization"):
            assert forbidden not in rendered.lower()


# -- transport environment -------------------------------------------------------


class TestTransportEnvironment:
    def test_session_key_forwarded_to_adapter_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pc.set_session_key("commandcode_goat", SECRET)
        env = mp.provider_transport_environment("commandcode_goat")
        assert env == {"AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY": SECRET}

    def test_documented_env_var_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COMMAND_CODE_API_KEY", "env-key-value")
        env = mp.provider_transport_environment("commandcode_goat")
        assert env == {"COMMAND_CODE_API_KEY": "env-key-value"}

    def test_forwarded_worker_session_key_reaches_adapter_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY", "worker-hop-value"
        )
        assert mp.provider_transport_environment("opencode_go") == {
            "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY": "worker-hop-value"
        }

    def test_no_source_no_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COMMAND_CODE_API_KEY", raising=False)
        monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
        assert mp.provider_transport_environment("commandcode_goat") is None

    def test_ollama_gets_no_environment(self) -> None:
        assert mp.provider_transport_environment("ollama_cloud") is None

    def test_worker_hop_session_key_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pc.set_session_key("opencode_go", SECRET)
        hop = mp.provider_session_credential_environment("opencode_go")
        assert hop == {"AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY": SECRET}
        monkeypatch.setenv("COMMAND_CODE_API_KEY", "env-key-value")
        assert mp.provider_session_credential_environment("commandcode_goat") is None


# -- dynamic catalog in the general picker -----------------------------------------


class TestDynamicCatalogListing:
    def test_discovered_catalog_replaces_curated_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            mp,
            "load_cached_catalog",
            lambda kind: (
                _cached_snapshot(
                    "opencode_go",
                    [
                        "opencode-go/kimi-k3",
                        "opencode-go/glm-5.3",
                        "opencode-go/glm-5",
                    ],
                )
                if kind == "opencode_go"
                else None
            ),
        )
        monkeypatch.setattr(mp, "_opencode_availability", lambda: (True, None))
        monkeypatch.setattr(mp, "credential_source_for", lambda kind: "session_key")
        models = mp.list_provider_models(include_ollama=False)
        opencode = [m for m in models if m.kind == "opencode_go"]
        ids = [m.model_id for m in opencode]
        assert "opencode-go/kimi-k3" in ids
        assert "opencode-go/glm-5" in ids
        kimi = next(m for m in opencode if m.model_id == "opencode-go/kimi-k3")
        assert kimi.available is True
        assert kimi.note == f"direct API · {pc.PROTOCOL_CHAT_COMPLETIONS}"
        glm5 = next(m for m in opencode if m.model_id == "opencode-go/glm-5")
        assert glm5.available is True  # legacy CLI route remains explicit
        assert "protocol not yet resolved" in (glm5.note or "")

    def test_unresolved_protocol_model_disabled_without_any_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            mp,
            "load_cached_catalog",
            lambda kind: (
                _cached_snapshot("opencode_go", ["opencode-go/glm-5"])
                if kind == "opencode_go"
                else None
            ),
        )
        monkeypatch.setattr(
            mp, "_opencode_availability", lambda: (False, "opencode CLI not found on PATH")
        )
        monkeypatch.setattr(mp, "credential_source_for", lambda kind: "session_key")
        models = [
            m for m in mp.list_provider_models(include_ollama=False)
            if m.kind == "opencode_go"
        ]
        assert len(models) == 1
        assert models[0].available is False
        assert "protocol not yet resolved" in (models[0].unavailable_reason or "").lower()

    def test_resolved_protocol_model_disabled_without_credential_or_cli(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            mp,
            "load_cached_catalog",
            lambda kind: (
                _cached_snapshot("opencode_go", ["opencode-go/kimi-k3"])
                if kind == "opencode_go"
                else None
            ),
        )
        monkeypatch.setattr(
            mp, "_opencode_availability", lambda: (False, "opencode CLI not found on PATH")
        )
        monkeypatch.setattr(mp, "credential_source_for", lambda kind: None)
        models = [
            m for m in mp.list_provider_models(include_ollama=False)
            if m.kind == "opencode_go"
        ]
        assert models[0].available is False
        assert "no direct api credential" in (models[0].unavailable_reason or "").lower()

    def test_curated_defaults_remain_fail_safe_without_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mp, "load_cached_catalog", lambda kind: None)
        monkeypatch.setattr(mp, "_opencode_availability", lambda: (False, "no auth store"))
        models = [
            m for m in mp.list_provider_models(include_ollama=False)
            if m.kind == "opencode_go"
        ]
        assert "opencode-go/deepseek-v4-flash" in [m.model_id for m in models]
        assert all(m.available is False for m in models)
        assert all(m.unavailable_reason == "no auth store" for m in models)

    def test_identical_model_names_stay_provider_distinct(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same human-readable model on two providers is two routes."""

        monkeypatch.setattr(
            mp,
            "load_cached_catalog",
            lambda kind: _cached_snapshot(
                kind,
                (
                    ["opencode-go/deepseek-v4-flash"]
                    if kind == "opencode_go"
                    else ["deepseek/deepseek-v4-flash"]
                ),
            ),
        )
        monkeypatch.setattr(mp, "_opencode_availability", lambda: (True, None))
        monkeypatch.setattr(mp, "_commandcode_availability", lambda: (True, None))
        monkeypatch.setattr(mp, "credential_source_for", lambda kind: "session_key")
        models = mp.list_provider_models(include_ollama=False)
        by_provider = {
            m.kind: format_display for m, format_display in (
                (item, mp.format_model_display_name(item.model_id))
                for item in models
            )
        }
        assert (
            by_provider["opencode_go"] == by_provider["commandcode_goat"]
        )  # identical human-readable label
        keys = {(m.kind, m.model_id) for m in models}
        assert ("opencode_go", "opencode-go/deepseek-v4-flash") in keys
        assert ("commandcode_goat", "deepseek/deepseek-v4-flash") in keys


# -- capability ladder non-goal ----------------------------------------------------


class TestCapabilityLadderRemainsFailClosed:
    def test_discovered_subscription_models_are_ladder_incompatible(self) -> None:
        # Subscription models are now executable for interactive lower
        # ladder rungs (via the shared provider runtime), but they remain
        # distinct from the frozen qualified Ollama treatment.
        from agentic_debugger.ui.session_config import (
            TARGET_LADDER,
            ModelChoice,
            ModelOption,
            SessionCatalog,
            model_compatibility,
        )

        for provider, model_id in (
            ("opencode_go", "opencode-go/kimi-k3"),
            ("commandcode_goat", "deepseek/deepseek-v4-flash"),
        ):
            option = ModelOption(
                provider, model_id, "Some Model", available=True
            )
            compatible, reason = model_compatibility(TARGET_LADDER, option)
            # Executable for interactive ladder
            assert compatible is True
            assert reason == ""
            # But never satisfies the qualified roster
            catalog = SessionCatalog(
                models=(option,),
                ladder_models=(ModelOption("ollama_cloud", model_id, "Some Model"),),
            )
            choice = ModelChoice(provider, model_id, "Some Model")
            assert catalog.ladder_model(choice) is None

    def test_ladder_catalog_never_resolves_subscription_models(self) -> None:
        """``SessionCatalog.ladder_model`` binds qualification to provider
        identity: a discovered subscription model is never reinterpreted
        as a qualified Ollama roster entry, even with an identical id."""

        from agentic_debugger.ui.session_config import (
            ModelChoice,
            ModelOption,
            SessionCatalog,
        )

        alias = "kimi-k3:cloud"
        catalog = SessionCatalog(
            models=(
                ModelOption("opencode_go", alias, "Kimi K3", available=True),
                ModelOption("commandcode_goat", alias, "Kimi K3", available=True),
            ),
            ladder_models=(ModelOption("ollama_cloud", alias, "Kimi K3"),),
        )
        for provider in ("opencode_go", "commandcode_goat"):
            choice = ModelChoice(provider, alias, "Kimi K3")
            assert catalog.ladder_model(choice) is None

    def test_discovery_never_enters_level32_roster(self) -> None:
        """The Level-32 qualified roster is static Ollama Cloud entries; a
        refreshed subscription catalog cannot add to it."""

        from agentic_debugger.application.level32 import level32_model_profiles

        profiles = level32_model_profiles()
        aliases = {p.alias for p in profiles}
        assert all(not alias.startswith("opencode-go/") for alias in aliases)
        assert "opencode-go/kimi-k3" not in aliases
        assert "deepseek/deepseek-v4-flash" not in aliases

    def test_level32_treatment_gate_rejects_discovered_models(self) -> None:
        """The Level-32 operator proof gate fails closed for subscription
        model identities — including live discovered ones."""

        import scripts.ollama_cloud_command_adapter as ollama_adapter
        import scripts.run_cookiecutter_967_pdb_proof as proof_mod

        for model_id in ("deepseek/deepseek-v4-flash", "opencode-go/kimi-k3"):
            with pytest.raises((proof_mod.ProofError, ollama_adapter.OllamaAdapterError)):
                proof_mod._require_treatment_eligible(model_id)

    def test_treatment_eligibility_untouched_by_discovery(self) -> None:
        """``is_treatment_eligible`` is a pure function of the Ollama
        Cloud spec contract; discovery artifacts cannot influence it."""

        import scripts.ollama_cloud_command_adapter as ollama_adapter

        spec = ollama_adapter.CLOUD_MODELS["glm-5.3-flash:cloud"]
        assert ollama_adapter.is_treatment_eligible(spec) is False

        def is_treatment_eligible(spec) -> bool:  # the forbidden mutation
            return True  # pragma: no cover - must never be installed

        # Source-level guard: the accepted implementation does not consult
        # provider-connection state.
        import inspect

        source = inspect.getsource(ollama_adapter.is_treatment_eligible)
        assert "provider_connections" not in source
        assert "opencode" not in source.lower()
        assert "commandcode" not in source.lower()

    def test_provider_registry_rejects_ladder_surface_for_subscription_models(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with a fully connected provider, Level-32 resolution
        paths remain untouched: the registry has no Level-32 route."""

        monkeypatch.setattr(mp, "credential_source_for", lambda kind: "session_key")
        config, provenance = mp.resolve_provider_live_config(
            "commandcode_goat", "deepseek/deepseek-v4-flash"
        )
        assert "treatment" not in json.dumps(provenance).lower()
        assert provenance["route"] == mp.ROUTE_DIRECT_API


# -- worker environment hop ----------------------------------------------------------


class TestWorkerEnvironmentHop:
    def test_child_environment_reaches_worker_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The UI -> worker session-key hop is bounded, in-memory, and
        never serialized into scenario params."""

        from agentic_debugger.application.events import SourceKind
        from agentic_debugger.application.session import SessionBudgets, SessionSpec
        from agentic_debugger.application.sources import ExecutionSourceSpec
        from agentic_debugger.application.worker_process import SessionWorkerProcess

        spec = SessionSpec(
            task_id="curated-off-by-one-002",
            source=ExecutionSourceSpec(
                kind=SourceKind.CONFIGURED_MODEL,
                task_id="curated-off-by-one-002",
                policy="pdb-on-uncertainty",
                model_config_ref="commandcode_goat:deepseek/deepseek-v4-flash",
            ),
            budgets=SessionBudgets(),
        )
        pc.set_session_key("commandcode_goat", SECRET)
        supervisor = SessionWorkerProcess(
            session_dir=tmp_path / "session",
            session_id="sess-test-worker-hop-0001",
            spec=spec,
            run_id="run-hop",
            scenario="configured_model_source",
            scenario_params={"provider": "commandcode_goat", "model_id": "x"},
            child_environment={"AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY": SECRET},
        )
        # The override is retained in memory for spawn only; scenario
        # params stay credential-free.
        assert supervisor._child_environment == {
            "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY": SECRET
        }
        assert SECRET not in json.dumps(supervisor._scenario_params)


