"""Unit gates for Local Project provider/model params and resolution.

Coverage pins the strict ``provider``/``model_id`` contract and the
registry-provider resolution path inside the Local Project source
(monkeypatched registry; no real provider contact).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.application import model_providers as mp  # noqa: E402
from agentic_debugger.application.local_project_source import _validate_params  # noqa: E402
from agentic_debugger.application.worker_scenarios import ScenarioInputError  # noqa: E402


def _base_params() -> dict:
    return {
        "project_repo_path": "C:/repo",
        "project_head": "a" * 40,
        "isolated_workspace": "C:/tmp/iso",
        "bug_description": "a bug",
        "config_root": "C:/cfg",
        "profile_id": "some-profile",
    }


class TestParamContract:
    def test_legacy_params_still_valid(self) -> None:
        validated = _validate_params(_base_params())
        assert validated["provider"] is None
        assert validated["model_id"] is None
        assert validated["is_ollama"] is False

    def test_provider_must_be_known(self) -> None:
        params = _base_params() | {"provider": "mystery"}
        with pytest.raises(ScenarioInputError):
            _validate_params(params)

    def test_registry_provider_requires_model_id(self) -> None:
        for provider in ("ollama_cloud", "opencode_go", "commandcode_goat"):
            params = _base_params() | {"provider": provider}
            with pytest.raises(ScenarioInputError):
                _validate_params(params)

    def test_valid_subscription_params(self) -> None:
        params = _base_params() | {
            "provider": "commandcode_goat",
            "model_id": "deepseek/deepseek-v4-flash",
        }
        validated = _validate_params(params)
        assert validated["provider"] == "commandcode_goat"
        assert validated["model_id"] == "deepseek/deepseek-v4-flash"

    def test_model_id_rejects_credential_shapes(self) -> None:
        params = _base_params() | {"provider": "opencode_go", "model_id": "api_key=SECRETVALUE123"}
        with pytest.raises(ScenarioInputError):
            _validate_params(params)

    def test_ollama_provider_via_model_id(self) -> None:
        params = _base_params() | {"provider": "ollama_cloud", "model_id": "qwen3.5:cloud"}
        validated = _validate_params(params)
        assert validated["provider"] == "ollama_cloud"
        assert validated["model_id"] == "qwen3.5:cloud"

    def test_arbitrary_configured_provider_is_valid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An arbitrary user-configured provider passes Local Project
        parameter validation (registry authority, not a fixed id set)."""
        from agentic_debugger.application import provider_connections as pc

        monkeypatch.setattr(
            pc, "provider_configurations_path", lambda: tmp_path / "providers.json"
        )
        pc.add_provider_config(
            name="My Custom Gateway",
            base_url="https://gateway.internal.example/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="my_custom_gateway",
        )
        params = _base_params() | {
            "provider": "my_custom_gateway",
            "model_id": "custom-model-x",
        }
        validated = _validate_params(params)
        assert validated["provider"] == "my_custom_gateway"

    def test_unconfigured_arbitrary_provider_fails_validation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Provider ids outside the known kinds must be explicitly
        configured; unknown arbitrary ids fail closed."""
        from agentic_debugger.application import provider_connections as pc

        monkeypatch.setattr(
            pc, "provider_configurations_path", lambda: tmp_path / "providers.json"
        )
        params = _base_params() | {"provider": "never_configured", "model_id": "m"}
        with pytest.raises(ScenarioInputError):
            _validate_params(params)


class TestSourceResolution:
    def test_registry_error_maps_to_scenario_input_error(self, tmp_path: Path) -> None:
        """A ProviderRegistryError must surface as the worker's honest
        ScenarioInputError before any execution starts."""

        from agentic_debugger.application.local_project_source import (
            run_local_project_session,
        )
        from agentic_debugger.application.worker_scenarios import ScenarioContext

        (tmp_path / "iso").mkdir()

        class _Emitter:
            session_id = "s"
            task_id = "t"

            def emit(self, *a, **k):  # pragma: no cover - not reached
                raise AssertionError("no events may be emitted before validation")

        class _Ctx(ScenarioContext):
            def __init__(self) -> None:
                self.emitter = _Emitter()

        def _raise(provider, model_id, **kwargs):
            raise mp.ProviderRegistryError("provider down")

        # The source imports resolve_provider_live_config at call time, so
        # patching the registry module attribute intercepts the resolution.
        monkey_target = mp.resolve_provider_live_config
        mp.resolve_provider_live_config = _raise
        try:
            params = _base_params() | {
                "provider": "commandcode_goat",
                "model_id": "deepseek/deepseek-v4-flash",
                "isolated_workspace": str(tmp_path / "iso"),
            }
            with pytest.raises(ScenarioInputError) as excinfo:
                run_local_project_session(_Ctx(), params)
            assert "provider down" in str(excinfo.value)
        finally:
            mp.resolve_provider_live_config = monkey_target

    def test_general_ollama_resolves_registry_and_emits_provider_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """General Ollama is a registry model, not a Level-32/profile alias."""
        from agentic_debugger.application import (
            command_config,
            level32,
            local_project_source,
        )
        from agentic_debugger.application.events import SessionEventKind, SourceKind
        from agentic_debugger.application.local_project_source import (
            run_local_project_session,
        )
        from agentic_debugger.application.worker_scenarios import ScenarioContext
        from agentic_debugger.cancellation import CancellationToken
        from agentic_debugger.evaluation.live import LiveModelConfig

        repo = tmp_path / "isolated"
        repo.mkdir()
        (repo / "sample.py").write_text("value = 1\n", encoding="utf-8")

        live_config = LiveModelConfig(
            model_name="glm-5.3-flash:cloud",
            command=(sys.executable, "-c", "raise SystemExit(0)"),
            request_timeout_seconds=60.0,
            tool_version="1.3",
        )
        resolver_calls: list[tuple[str, str]] = []

        def resolve_registry(provider, model_id, **_kwargs):
            resolver_calls.append((provider, model_id))
            return live_config, {
                "provider": provider,
                "profile_id": model_id,
                "display_name": "glm-5.3-flash",
                "protocol_version": "1.3",
                "tool_version": "1.3",
            }

        monkeypatch.setattr(mp, "resolve_provider_live_config", resolve_registry)
        monkeypatch.setattr(
            level32,
            "level32_model_profiles",
            lambda: (_ for _ in ()).throw(
                AssertionError("general Ollama must not require Level-32 qualification")
            ),
        )
        monkeypatch.setattr(
            command_config.CommandModelConfigStore,
            "get",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("general Ollama must not fall through to profile storage")
            ),
        )

        class NoopObservability:
            def diagnosis_recorded(self, **_kwargs):
                pass

            def source_snapshot(self, _snapshot):
                pass

        monkeypatch.setattr(
            local_project_source,
            "SessionObservability",
            lambda *_args, **_kwargs: NoopObservability(),
        )
        monkeypatch.setattr(
            local_project_source,
            "_inventory_tracked_python_files",
            lambda _isolated: ["sample.py"],
        )

        class StopAfterProvenance(RuntimeError):
            pass

        class Emitter:
            session_id = "session-general-ollama"
            task_id = "local-project-debug"
            source_kind = SourceKind.LOCAL_PROJECT

            def __init__(self) -> None:
                self.provenance = None

            def emit(self, kind, payload):
                if kind is SessionEventKind.MODEL_CONFIGURED:
                    self.provenance = dict(payload)
                    raise StopAfterProvenance

        emitter = Emitter()
        ctx = ScenarioContext(
            work_dir=tmp_path,
            token=CancellationToken(),
            emitter=emitter,
            run_id="run-general-ollama",
        )
        params = _base_params() | {
            "project_repo_path": str(repo),
            "isolated_workspace": str(repo),
            "provider": "ollama_cloud",
            "model_id": "glm-5.3-flash:cloud",
            "profile_id": "glm-5.3-flash:cloud",
        }

        with pytest.raises(StopAfterProvenance):
            run_local_project_session(ctx, params)

        assert resolver_calls == [("ollama_cloud", "glm-5.3-flash:cloud")]
        assert emitter.provenance is not None
        assert emitter.provenance["provider"] == "ollama_cloud"
        assert emitter.provenance["profile_id"] == "glm-5.3-flash:cloud"
        assert emitter.provenance["display_name"] == "glm-5.3-flash"

    def test_arbitrary_custom_provider_routes_through_registry_not_profile_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An arbitrary user-configured provider executes through the
        canonical registry resolver in Local Project — never falling into
        the legacy CommandModelConfigStore path — with truthful
        provider/model provenance."""

        from agentic_debugger.application import (
            command_config,
            provider_connections as pc,
        )
        from agentic_debugger.application.events import SessionEventKind, SourceKind
        from agentic_debugger.application.local_project_source import (
            run_local_project_session,
        )
        from agentic_debugger.application.worker_scenarios import ScenarioContext
        from agentic_debugger.cancellation import CancellationToken

        monkeypatch.setenv(
            "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH",
            str(tmp_path / "provider-configurations.json"),
        )
        pc.add_provider_config(
            name="My Custom Gateway",
            base_url="https://gateway.internal.example/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="my_custom_gateway",
        )
        pc.add_manual_model("my_custom_gateway", "custom-model-x", "Custom Model X")
        pc.set_session_key("my_custom_gateway", "custom-gateway-key-not-real")

        monkeypatch.setattr(
            command_config.CommandModelConfigStore,
            "get",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError(
                    "a registry provider must never fall through to the "
                    "command-profile store"
                )
            ),
        )

        from agentic_debugger.application import local_project_source

        class NoopObservability:
            def diagnosis_recorded(self, **_kwargs):
                pass

            def source_snapshot(self, _snapshot):
                pass

        monkeypatch.setattr(
            local_project_source,
            "SessionObservability",
            lambda *_args, **_kwargs: NoopObservability(),
        )
        monkeypatch.setattr(
            local_project_source,
            "_inventory_tracked_python_files",
            lambda _isolated: ["sample.py"],
        )

        repo = tmp_path / "isolated"
        repo.mkdir()
        (repo / "sample.py").write_text("value = 1\n", encoding="utf-8")

        class StopAfterProvenance(RuntimeError):
            pass

        class Emitter:
            session_id = "session-custom-provider"
            task_id = "local-project-debug"
            source_kind = SourceKind.LOCAL_PROJECT

            def __init__(self) -> None:
                self.provenance = None

            def emit(self, kind, payload):
                if kind is SessionEventKind.MODEL_CONFIGURED:
                    self.provenance = dict(payload)
                    raise StopAfterProvenance

        ctx = ScenarioContext(
            work_dir=tmp_path,
            token=CancellationToken(),
            emitter=Emitter(),
            run_id="run-custom-provider",
        )
        params = _base_params() | {
            "project_repo_path": str(repo),
            "isolated_workspace": str(repo),
            "provider": "my_custom_gateway",
            "model_id": "custom-model-x",
            "profile_id": "custom-model-x",
        }

        with pytest.raises(StopAfterProvenance):
            run_local_project_session(ctx, params)

        provenance = ctx.emitter.provenance
        assert provenance is not None
        assert provenance["provider"] == "my_custom_gateway"
        assert provenance["profile_id"] == "custom-model-x"
        assert provenance["route"] == "direct_api"
        assert provenance["api_protocol"] == "chat_completions"
        assert provenance["provider_model_id"] == "custom-model-x"
        assert provenance["endpoint"] == "https://gateway.internal.example/v1"
        assert provenance["display_name"] == "Custom Model X"
        assert "custom-gateway-key-not-real" not in str(provenance)
