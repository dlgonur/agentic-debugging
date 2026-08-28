"""Unit gates for Local Project provider/model params and resolution.

Covers the strict-params contract for ``provider``/``model_id`` and the
subscription-provider resolution path inside the Local Project source
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

    def test_subscription_provider_requires_model_id(self) -> None:
        for provider in ("opencode_go", "commandcode_goat"):
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
