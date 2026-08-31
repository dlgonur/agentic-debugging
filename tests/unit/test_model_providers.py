"""Unit gates for the unified model-provider registry.

Covers availability probing (presence-only, fail-closed reasons, never
credential material), grouped model listing with availability annotation,
fail-closed transport resolution per provider, and the live model-listing
helpers (monkeypatched; no real provider contact).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.application import model_providers as mp  # noqa: E402


class TestAvailability:
    def test_availability_shapes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(mp, "_commandcode_auth_store_path", lambda: tmp_path / "cc-auth.json")
        monkeypatch.setattr(mp, "_opencode_auth_store_path", lambda: tmp_path / "oc-auth.json")
        monkeypatch.setattr(mp, "_first_on_path", lambda candidates: None)
        monkeypatch.setattr(mp.shutil, "which", lambda name: None)
        monkeypatch.setattr(mp, "_direct_connection_available", lambda kind: (False, None))
        results = {kind: (ok, reason) for kind, ok, reason in mp.provider_availability()}
        assert results[mp.PROVIDER_KIND_OLLAMA] == (True, None)
        assert results[mp.PROVIDER_KIND_OPENCODE][0] is False
        assert results[mp.PROVIDER_KIND_COMMANDCODE][0] is False
        assert "auth store" in results[mp.PROVIDER_KIND_OPENCODE][1]

    def test_direct_credential_alone_satisfies_availability(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A usable direct-API credential source serves a provider even
        when the legacy CLI is entirely absent."""
        monkeypatch.setattr(mp, "_direct_connection_available", lambda kind: (True, None))
        monkeypatch.setattr(mp, "_first_on_path", lambda candidates: None)
        monkeypatch.setattr(mp.shutil, "which", lambda name: None)
        results = {kind: ok for kind, ok, _ in mp.provider_availability()}
        assert results[mp.PROVIDER_KIND_OPENCODE] is True
        assert results[mp.PROVIDER_KIND_COMMANDCODE] is True

    def test_ready_when_stores_and_clis_exist(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / "cc-auth.json").write_text("{}", encoding="utf-8")
        (tmp_path / "oc-auth.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(mp, "_commandcode_auth_store_path", lambda: tmp_path / "cc-auth.json")
        monkeypatch.setattr(mp, "_opencode_auth_store_path", lambda: tmp_path / "oc-auth.json")
        monkeypatch.setattr(mp, "_first_on_path", lambda candidates: "x")
        monkeypatch.setattr(mp.shutil, "which", lambda name: "x")
        monkeypatch.setattr(mp, "_direct_connection_available", lambda kind: (False, None))
        results = {kind: ok for kind, ok, _ in mp.provider_availability()}
        assert all(results.values())

    def test_system_cmd_exe_does_not_satisfy_commandcode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Windows-style: auth store present but only system cmd.exe on
        PATH => CommandCode provider is unavailable and its discovery
        must not resolve cmd.exe."""
        (tmp_path / "cc-auth.json").write_text("{}", encoding="utf-8")
        (tmp_path / "oc-auth.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(mp, "_commandcode_auth_store_path", lambda: tmp_path / "cc-auth.json")
        monkeypatch.setattr(mp, "_opencode_auth_store_path", lambda: tmp_path / "oc-auth.json")
        monkeypatch.setattr(mp, "_direct_connection_available", lambda kind: (False, None))

        system_cmd = r"C:\Windows\System32\cmd.exe"

        def fake_which(name: str):
            if name == "cmd":
                return system_cmd
            return None

        monkeypatch.setattr(mp.shutil, "which", fake_which)
        results = {kind: (ok, reason) for kind, ok, reason in mp.provider_availability()}
        ok, reason = results[mp.PROVIDER_KIND_COMMANDCODE]
        assert ok is False
        assert "CLI not found" in reason
        assert "cmd.exe" not in (reason or "")
        # The resolver candidate set itself never contains the system
        # shell name, so the fail-closed path is structural.
        import scripts.commandcode_goat_adapter as cca

        assert "cmd" not in cca._CANDIDATE_EXECUTABLES
        assert "cmd" not in mp._COMMANDCODE_CLI_CANDIDATES


class TestModelListing:
    @pytest.fixture(autouse=True)
    def _isolated_catalog_cache(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Isolate the machine-local catalog cache so listing always
        exercises the curated fail-safe unless a test supplies a cache."""
        from agentic_debugger.application import provider_connections as pc

        monkeypatch.setattr(pc, "catalog_cache_path", lambda: tmp_path / "absent-cache.json")
        monkeypatch.setattr(pc, "provider_configurations_path", lambda: tmp_path / "absent-config.json")
        monkeypatch.setattr(mp, "_direct_connection_available", lambda kind: (False, None))

    def test_grouped_listing_annotates_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mp, "_opencode_availability", lambda: (False, "no auth store"))
        monkeypatch.setattr(mp, "_commandcode_availability", lambda: (True, None))
        monkeypatch.setattr(
            mp,
            "list_provider_models",
            mp.list_provider_models,
        )
        models = mp.list_provider_models(include_ollama=False)
        kinds = {m.kind for m in models}
        assert kinds == {mp.PROVIDER_KIND_OPENCODE, mp.PROVIDER_KIND_COMMANDCODE}
        opencode = [m for m in models if m.kind == mp.PROVIDER_KIND_OPENCODE]
        assert opencode and all(not m.available for m in opencode)
        assert all(m.unavailable_reason == "no auth store" for m in opencode)
        commandcode = [m for m in models if m.kind == mp.PROVIDER_KIND_COMMANDCODE]
        assert commandcode and all(m.available for m in commandcode)

    def test_ollama_roster_included(self) -> None:
        models = mp.list_provider_models(include_ollama=True)
        assert any(m.kind == mp.PROVIDER_KIND_OLLAMA for m in models)

    def test_catalog_only_ollama_model_is_not_available_in_local_project(self) -> None:
        models = mp.list_provider_models(include_ollama=True)
        kimi3 = next((m for m in models if m.model_id == "kimi-k3:cloud"), None)
        assert kimi3 is not None
        assert kimi3.available is False
        assert kimi3.unavailable_reason is not None
        assert "Catalog entry only" in kimi3.unavailable_reason

        # In contrast, GLM-5.3-Flash declares a transport profile and is runnable locally
        glm = next((m for m in models if m.model_id == "glm-5.3-flash:cloud"), None)
        assert glm is not None
        assert glm.available is True
        assert glm.unavailable_reason is None

    def test_glm_5_3_flash_in_general_ollama_roster(self) -> None:
        models = mp.list_provider_models(include_ollama=True)
        glm = next((m for m in models if m.model_id == "glm-5.3-flash:cloud"), None)
        assert glm is not None
        assert glm.kind == mp.PROVIDER_KIND_OLLAMA
        assert glm.display_name == "GLM 5.3 Flash"
        assert glm.available is True

    def test_glm_5_3_flash_not_in_level32_qualified_roster(self) -> None:
        from agentic_debugger.application.level32 import level32_model_profiles

        profiles = level32_model_profiles()
        assert not any(p.alias == "glm-5.3-flash:cloud" for p in profiles)

    def test_custom_command_profile_provider_label_is_consistent(self) -> None:
        assert mp._PROVIDER_LABELS[mp.PROVIDER_KIND_CONFIGURED] == "Custom command profile"


class TestResolution:
    def test_unknown_provider_fails_closed(self) -> None:
        with pytest.raises(mp.ProviderRegistryError):
            mp.resolve_provider_live_config("mystery", "model")

    def test_empty_model_fails_closed(self) -> None:
        with pytest.raises(mp.ProviderRegistryError):
            mp.resolve_provider_live_config(mp.PROVIDER_KIND_COMMANDCODE, "  ")

    def test_unavailable_commandcode_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mp, "_commandcode_availability", lambda: (False, "no auth"))
        with pytest.raises(mp.ProviderRegistryError) as excinfo:
            mp.resolve_provider_live_config(mp.PROVIDER_KIND_COMMANDCODE, "deepseek/deepseek-v4-flash")
        assert "no auth" in str(excinfo.value)

    def test_commandcode_resolution_and_provenance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mp, "_commandcode_availability", lambda: (True, None))
        config, provenance = mp.resolve_provider_live_config(
            mp.PROVIDER_KIND_COMMANDCODE, "deepseek/deepseek-v4-flash"
        )
        assert provenance["provider"] == mp.PROVIDER_KIND_COMMANDCODE
        assert provenance["profile_id"] == "deepseek/deepseek-v4-flash"
        assert provenance["display_name"] == "deepseek-v4-flash"
        assert config.request_timeout_seconds > 0

    def test_opencode_resolution_rejects_free_tier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mp, "_opencode_availability", lambda: (True, None))
        with pytest.raises(mp.ProviderRegistryError):
            mp.resolve_provider_live_config(mp.PROVIDER_KIND_OPENCODE, "opencode/hy3-free")

    def test_configured_profiles_do_not_resolve_here(self) -> None:
        with pytest.raises(mp.ProviderRegistryError):
            mp.resolve_provider_live_config(mp.PROVIDER_KIND_CONFIGURED, "anything")

    def test_catalog_only_ollama_model_fails_resolution(self) -> None:
        with pytest.raises(mp.ProviderRegistryError) as excinfo:
            mp.resolve_provider_live_config(mp.PROVIDER_KIND_OLLAMA, "kimi-k3:cloud")
        assert "not supported" in str(excinfo.value).casefold() or "not eligible" in str(excinfo.value).casefold()

    def test_glm_5_3_flash_resolves_live_config_and_provenance(self) -> None:
        config, provenance = mp.resolve_provider_live_config(
            mp.PROVIDER_KIND_OLLAMA, "glm-5.3-flash:cloud"
        )
        assert provenance["provider"] == mp.PROVIDER_KIND_OLLAMA
        assert provenance["profile_id"] == "glm-5.3-flash:cloud"
        assert config.model_name == "glm-5.3-flash:cloud"

    def test_level32_fails_closed_on_unqualified_model(self) -> None:
        import scripts.run_cookiecutter_967_pdb_proof as proof_mod
        from agentic_debugger.application.ollama_cloud_source import ScenarioInputError, _config

        # Level-32 proof operator gate rejects non-live-verified models
        with pytest.raises(proof_mod.ProofError) as excinfo:
            proof_mod._require_treatment_eligible("glm-5.3-flash:cloud")
        assert "not yet live-transport eligible for Level-32" in str(excinfo.value)

        # Level-32 session config builder also rejects non-treatment-eligible models
        with pytest.raises(ScenarioInputError) as excinfo2:
            _config("glm-5.3-flash:cloud", logical_call_ceiling=32)
        assert "not eligible" in str(excinfo2.value)


class TestLiveModelListing:
    def test_commandcode_listing_parses_models(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import io

        def fake_run_list_models(stream, executable=None, timeout_seconds=60.0):
            stream.write('{"schema_version":"commandcode-models-v1","models":["zai-org/glm-5.2","gpt-5.6-sol"]}')

        monkeypatch.setattr(mp, "run_list_models", None, raising=False)
        import scripts.commandcode_goat_adapter as cca

        monkeypatch.setattr(cca, "run_list_models", fake_run_list_models)
        models = mp.list_live_models(mp.PROVIDER_KIND_COMMANDCODE)
        assert "zai-org/glm-5.2" in models and "gpt-5.6-sol" in models

    def test_opencode_listing_filters_go_models(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Result:
            returncode = 0
            stdout = "opencode-go/glm-5.3\nopencode-go/kimi-k3\nopencode/hy3-free\nrandom text\n"
            stderr = ""

        monkeypatch.setattr(mp.shutil, "which", lambda name: "opencode")
        monkeypatch.setattr("subprocess.run", lambda *a, **k: _Result())
        models = mp.list_live_models(mp.PROVIDER_KIND_OPENCODE)
        assert "opencode-go/glm-5.3" in models
        assert all(m.startswith("opencode-go/") for m in models)

    def test_unsupported_listing_fails_closed(self) -> None:
        with pytest.raises(mp.ProviderRegistryError):
            mp.list_live_models(mp.PROVIDER_KIND_OLLAMA)

class TestFormatModelDisplayName:
    def test_known_ollama_models(self) -> None:
        assert mp.format_model_display_name("deepseek-v4-flash:cloud") == "DeepSeek V4 Flash"
        assert mp.format_model_display_name("deepseek-v4-pro:cloud") == "DeepSeek V4 Pro"
        assert mp.format_model_display_name("glm-5.1:cloud") == "GLM 5.1"
        assert mp.format_model_display_name("glm-5.2:cloud") == "GLM 5.2"
        assert mp.format_model_display_name("glm-5.3-flash:cloud") == "GLM 5.3 Flash"
        assert mp.format_model_display_name("gpt-oss:20b-cloud") == "GPT-OSS 20B"
        assert mp.format_model_display_name("gpt-oss:120b-cloud") == "GPT-OSS 120B"
        assert mp.format_model_display_name("nemotron-3-super:cloud") == "Nemotron 3 Super"
        assert mp.format_model_display_name("nemotron-3-nano:30b-cloud") == "Nemotron 3 Nano 30B"
        assert mp.format_model_display_name("gemma4:31b-cloud") == "Gemma 4 31B"
        assert mp.format_model_display_name("mistral-large-3:675b-cloud") == "Mistral Large 3 675B"

    def test_known_opencode_models(self) -> None:
        assert mp.format_model_display_name("opencode-go/deepseek-v4-flash") == "DeepSeek V4 Flash"
        assert mp.format_model_display_name("opencode-go/glm-5.3") == "GLM 5.3"
        assert mp.format_model_display_name("opencode-go/kimi-k2.7-code") == "Kimi K2.7 Code"
        assert mp.format_model_display_name("opencode-go/grok-4.6") == "Grok 4.6"
        assert mp.format_model_display_name("opencode-go/minimax-m3") == "MiniMax M3"

    def test_known_commandcode_models(self) -> None:
        assert mp.format_model_display_name("deepseek/deepseek-v4-flash") == "DeepSeek V4 Flash"
        assert mp.format_model_display_name("zai-org/glm-5.2-fast") == "GLM 5.2 Fast"
        assert mp.format_model_display_name("moonshotai/kimi-k3") == "Kimi K3"
        assert mp.format_model_display_name("xiaomi/mimo-v2.5-pro") == "MiMo V2.5 Pro"
        assert mp.format_model_display_name("gpt-5.6-sol") == "GPT-5.6 Sol"

    def test_offline_and_blank(self) -> None:
        assert mp.format_model_display_name("") == "Offline"
        assert mp.format_model_display_name("offline") == "Offline"
        assert mp.format_model_display_name("  Offline  ") == "Offline"

