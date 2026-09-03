"""Provider-registry parameter contract for the configured command source.

The unified provider platform routes Ollama Cloud / OpenCode Go /
CommandCode GOAT models through the configured command source's registry
parameter contract.  These tests pin the fail-closed boundaries of that
contract (parameter shape and resolution) without launching executables;
the shared execution pipeline behind the resolved ``LiveModelConfig`` is
covered by the existing configured-source integration suite.
"""

from __future__ import annotations

import pytest

from agentic_debugger.application.configured_source import (
    _validate_registry_params,
    _validate_store_params,
)
from agentic_debugger.application.worker_scenarios import ScenarioInputError

_POLICY = "pdb-on-uncertainty"


@pytest.fixture(autouse=True)
def _isolated_configured_registry(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Isolate the machine-local provider store and explicitly configure
    the historical providers these tests exercise.

    The registry is user-owned (no implicit built-ins), so validation of
    registry provider ids must not depend on operator-local state.
    """
    from agentic_debugger.application import provider_connections as pc

    monkeypatch.setattr(
        pc, "provider_configurations_path", lambda: tmp_path / "provider-configurations.json"
    )
    for pid, name in (
        ("ollama_cloud", "Ollama"),
        ("opencode_go", "OpenCode Go"),
        ("commandcode_goat", "CommandCode GOAT"),
    ):
        pc.add_provider_config(
            name=name,
            base_url=f"https://{pid}.example/v1",
            api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
            provider_id=pid,
        )
    yield


def _registry_params(**overrides):
    params = {"provider": "opencode_go", "model_id": "opencode-go/glm-5.3", "policy": _POLICY}
    params.update(overrides)
    return params


class TestRegistryParamValidation:
    def test_valid_registry_params(self):
        provider, model_id, policy = _validate_registry_params(_registry_params())
        assert provider == "opencode_go"
        assert model_id == "opencode-go/glm-5.3"
        assert policy == _POLICY

    @pytest.mark.parametrize("provider", ["ollama_cloud", "commandcode_goat"])
    def test_all_registry_kinds_accepted(self, provider):
        assert _validate_registry_params(_registry_params(provider=provider))[0] == provider

    @pytest.mark.parametrize(
        "provider", ["configured", "", "openai", "OLLAMA_CLOUD", None, 7]
    )
    def test_unknown_provider_fails_closed(self, provider):
        with pytest.raises(ScenarioInputError, match="provider"):
            _validate_registry_params(_registry_params(provider=provider))

    @pytest.mark.parametrize("model_id", ["", None, 7])
    def test_missing_model_id_fails_closed(self, model_id):
        with pytest.raises(ScenarioInputError, match="model_id"):
            _validate_registry_params(_registry_params(model_id=model_id))

    def test_oversized_model_id_fails_closed(self):
        with pytest.raises(ScenarioInputError, match="byte bound"):
            _validate_registry_params(_registry_params(model_id="x" * 200))

    @pytest.mark.parametrize(
        "mixed", [{"config_root": "/x"}, {"profile_id": "p"}, {"expected_fingerprint": "a" * 64}]
    )
    def test_mixing_store_params_fails_closed(self, mixed):
        with pytest.raises(ScenarioInputError, match="profile-store"):
            _validate_registry_params(_registry_params(**mixed))

    def test_unknown_policy_fails_closed(self):
        with pytest.raises(ScenarioInputError, match="policy"):
            _validate_registry_params(_registry_params(policy="not-a-policy"))

    def test_unknown_param_fails_closed(self):
        with pytest.raises(ScenarioInputError, match="unknown configured source params"):
            _validate_registry_params(_registry_params(extra="x"))


class TestStoreParamValidation:
    def test_store_contract_rejects_provider_params(self):
        with pytest.raises(ScenarioInputError, match="config_root"):
            _validate_store_params(_registry_params())

    def test_store_contract_still_accepts_profile_params(self, tmp_path):
        config_root, profile_id, policy, fingerprint = _validate_store_params(
            {
                "config_root": str(tmp_path),
                "profile_id": "dummy",
                "policy": _POLICY,
                "expected_fingerprint": "a" * 64,
            }
        )
        assert (config_root, profile_id, policy, fingerprint) == (
            str(tmp_path), "dummy", _POLICY, "a" * 64,
        )


class TestRegistryResolutionGate:
    def test_unavailable_provider_fails_closed_before_launch(self, monkeypatch):
        import agentic_debugger.application.model_providers as mp
        from agentic_debugger.application.configured_source import (
            _resolve_registry_model,
        )

        def unavailable(kind, model_id, **kwargs):
            raise mp.ProviderRegistryError("OpenCode auth store not found")

        monkeypatch.setattr(mp, "resolve_provider_live_config", unavailable)
        with pytest.raises(ScenarioInputError, match="provider model is unavailable"):
            _resolve_registry_model("opencode_go", "opencode-go/glm-5.3")

    def test_resolution_builds_canonical_config_and_fingerprint(self, monkeypatch):
        import agentic_debugger.application.model_providers as mp
        from agentic_debugger.application.configured_source import (
            _resolve_registry_model,
        )
        from agentic_debugger.evaluation.live import LiveModelConfig

        sentinel = LiveModelConfig(
            model_name="glm-5.3",
            command=("python", "adapter"),
            request_timeout_seconds=120.0,
            tool_version="1.3",
        )

        def available(kind, model_id, **kwargs):
            assert kind == "opencode_go"
            assert model_id == "opencode-go/glm-5.3"
            return sentinel, {
                "provider": kind,
                "profile_id": model_id,
                "display_name": "glm-5.3",
                "protocol_version": "1.3",
                "tool_version": "1.3",
            }

        monkeypatch.setattr(mp, "resolve_provider_live_config", available)
        live_config, provenance, fingerprint = _resolve_registry_model(
            "opencode_go", "opencode-go/glm-5.3"
        )
        assert live_config is sentinel
        assert provenance["display_name"] == "glm-5.3"
        assert len(fingerprint) == 64 and all(c in "0123456789abcdef" for c in fingerprint)
