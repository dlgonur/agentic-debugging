"""Facade/architecture regression suite for Task 47 (Candidate 59).

Proves the provider-connections decomposition is behavior-preserving:

1. baseline public symbols resolve from the facade (exact ``__all__``);
2. identity-sensitive exports are identical to canonical implementation objects;
3. representative provider configuration discovery still works;
4. credential-source behavior remains;
5. provider readiness/status remains;
6. model enumeration remains;
7. CommandCode GOAT path remains;
8. Direct API path remains;
9. config-root isolation remains;
10. no import cycle exists among the authorities and the facade.

No live provider calls. Configuration/credential state is isolated per test
through environment overrides; the OS secure store is disabled.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from agentic_debugger.application import provider_connections as facade

BASELINE_ALL = [
    "AUTH_ANTHROPIC",
    "AUTH_BEARER",
    "AUTH_MODES",
    "AUTH_NONE",
    "CATALOG_DISABLED",
    "CATALOG_MODES",
    "CATALOG_OPENAI",
    "CREDENTIAL_SOURCE_CLI_AUTH_STORE",
    "CREDENTIAL_SOURCE_ENVIRONMENT",
    "CREDENTIAL_SOURCE_SAVED",
    "CREDENTIAL_SOURCE_SESSION_KEY",
    "DIRECT_API_PROVIDER_KINDS",
    "ENDPOINT_CONTRACT_DISPLAY_LABELS",
    "TRANSPORT_COMMANDCODE_GOAT",
    "TRANSPORT_GENERIC",
    "TRANSPORT_MODES",
    "TRANSPORT_OLLAMA_CLOUD",
    "TRANSPORT_OPENCODE_GO",
    "DiscoveredProviderModel",
    "PROTOCOL_CHAT_COMPLETIONS",
    "PROTOCOL_MESSAGES",
    "PROTOCOL_RESPONSES",
    "PROVIDER_CONFIG_SCHEMA_VERSION",
    "PROVIDER_CONFIG_SCHEMA_VERSIONS",
    "ProviderCatalogSnapshot",
    "ProviderConfig",
    "ProviderConnectionError",
    "ProviderConnectionStatus",
    "add_manual_model",
    "add_provider_config",
    "catalog_cache_path",
    "clear_all_session_keys",
    "clear_session_key",
    "commit_provider_and_credential",
    "connection_statuses",
    "credential_slot_name",
    "credential_source_for",
    "credential_value_is_usable",
    "delete_cached_catalog",
    "delete_provider_config",
    "delete_secure_credential",
    "describe_transport_gap",
    "effective_model_protocol",
    "get_provider_config",
    "protocol_blocker_reason",
    "has_secure_credential",
    "has_session_key",
    "inference_path_for",
    "is_known_provider",
    "is_protocol_executable",
    "is_provider_quarantined",
    "is_valid_provider_id",
    "list_configured_providers",
    "load_cached_catalog",
    "load_provider_configurations",
    "load_secure_credential",
    "peek_session_key",
    "provider_api_model_id",
    "provider_auth_mode",
    "provider_base_url",
    "provider_catalog_mode",
    "provider_configurations_path",
    "provider_connection_status",
    "provider_environment_variable",
    "provider_legacy_cli_auth_file",
    "provider_quarantine_path",
    "provider_endpoint_binding_valid",
    "provider_session_credential_environment",
    "provider_session_credential_authority_variable",
    "provider_session_credential_variable",
    "provider_tls_signature_blocked",
    "provider_transport_credential_environment",
    "provider_transport_network_environment",
    "provider_transport_profile",
    "quarantine_provider",
    "quarantined_providers",
    "refresh_provider_catalog",
    "resolve_model_protocol",
    "resolve_runtime_credential",
    "save_cached_catalog",
    "save_provider_configurations",
    "save_secure_credential",
    "set_session_key",
    "test_provider_connection",
    "update_provider_config",
    "validate_and_canonicalize_url",
    "validate_auth_protocol_combination",
    "validate_provider_config_for_write",
]

# Spot-checked identity map: facade name -> canonical module attribute path.
IDENTITY_SPOTS = {
    "ProviderConfig": "provider_config.ProviderConfig",
    "DiscoveredProviderModel": "provider_config.DiscoveredProviderModel",
    "ProviderCatalogSnapshot": "provider_catalog.ProviderCatalogSnapshot",
    "ProviderConnectionStatus": "provider_catalog.ProviderConnectionStatus",
    "ProviderConnectionError": "provider_identity.ProviderConnectionError",
    "add_provider_config": "provider_management.add_provider_config",
    "update_provider_config": "provider_management.update_provider_config",
    "delete_provider_config": "provider_management.delete_provider_config",
    "add_manual_model": "provider_management.add_manual_model",
    "commit_provider_and_credential": "provider_management.commit_provider_and_credential",
    "load_provider_configurations": "provider_config.load_provider_configurations",
    "save_provider_configurations": "provider_config.save_provider_configurations",
    "get_provider_config": "provider_config.get_provider_config",
    "credential_source_for": "provider_credentials.credential_source_for",
    "resolve_runtime_credential": "provider_credentials.resolve_runtime_credential",
    "resolve_model_protocol": "provider_protocols.resolve_model_protocol",
    "effective_model_protocol": "provider_protocols.effective_model_protocol",
    "inference_path_for": "provider_protocols.inference_path_for",
    "provider_api_model_id": "provider_protocols.provider_api_model_id",
    "refresh_provider_catalog": "provider_catalog.refresh_provider_catalog",
    "test_provider_connection": "provider_catalog.test_provider_connection",
    "provider_connection_status": "provider_catalog.provider_connection_status",
    "connection_statuses": "provider_catalog.connection_statuses",
    "quarantine_provider": "provider_config.quarantine_provider",
    "validate_auth_protocol_combination": "provider_identity.validate_auth_protocol_combination",
}

# Non-__all__ names with real repo consumers; must keep resolving (same object).
COMPAT_EXTRAS = {
    "AUTH_DISPLAY_LABELS": "provider_identity.AUTH_DISPLAY_LABELS",
    "PROTOCOL_DISPLAY_LABELS": "provider_identity.PROTOCOL_DISPLAY_LABELS",
    "TRANSPORT_DISPLAY_LABELS": "provider_identity.TRANSPORT_DISPLAY_LABELS",
    "HISTORICAL_TRANSPORT_PROFILES": "provider_identity.HISTORICAL_TRANSPORT_PROFILES",
    "_PROVIDER_CREDENTIAL_SOURCE_LABELS": "provider_credentials._PROVIDER_CREDENTIAL_SOURCE_LABELS",
    "_normalize_catalog": "provider_catalog._normalize_catalog",
    "_session_env_var_for": "provider_credentials._session_env_var_for",
    "_OPENCODE_GO_MODEL_PREFIX": "provider_protocols._OPENCODE_GO_MODEL_PREFIX",
    "_OPENCODE_GO_DOCUMENTED_PROTOCOLS": "provider_protocols._OPENCODE_GO_DOCUMENTED_PROTOCOLS",
    "_contract_for_config": "provider_identity._contract_for_config",
    "_contract_for_kind": "provider_identity._contract_for_kind",
    "_credential_is_usable": "provider_credentials._credential_is_usable",
    "_read_opencode_auth_store_key": "provider_credentials._read_opencode_auth_store_key",
    "opencode_auth_store_path": "provider_credentials.opencode_auth_store_path",
    "_MAX_PROVIDERS_CONFIGURED": "provider_config._MAX_PROVIDERS_CONFIGURED",
    "_CONTRACTS": "provider_identity._CONTRACTS",
    "_QUARANTINED_PROVIDERS": "provider_config._QUARANTINED_PROVIDERS",
    "_SESSION_KEYS": "provider_credentials._SESSION_KEYS",
    "_CACHE_SCHEMA_VERSION": "provider_catalog._CACHE_SCHEMA_VERSION",
    "_MAX_CACHE_FILE_BYTES": "provider_catalog._MAX_CACHE_FILE_BYTES",
    "_MAX_CATALOG_MODELS": "provider_catalog._MAX_CATALOG_MODELS",
    "_write_quarantine_state": "provider_config._write_quarantine_state",
    "_read_quarantine_file": "provider_config._read_quarantine_file",
    "resolve_opencode_go_protocol": "provider_protocols.resolve_opencode_go_protocol",
    "resolve_commandcode_protocol": "provider_protocols.resolve_commandcode_protocol",
    "resolve_model_protocol_for_config": "provider_protocols.resolve_model_protocol_for_config",
    "effective_model_protocol_for_config": "provider_protocols.effective_model_protocol_for_config",
    "provider_api_model_id_for_config": "provider_protocols.provider_api_model_id_for_config",
    "inference_path_for_config": "provider_protocols.inference_path_for_config",
    "protocol_blocker_reason_for_config": "provider_protocols.protocol_blocker_reason_for_config",
    "is_protocol_executable_for_config": "provider_protocols.is_protocol_executable_for_config",
    "provider_authority_environment_names": "provider_credentials.provider_authority_environment_names",
    "clear_provider_quarantine": "provider_config.clear_provider_quarantine",
    "provider_auth_mode": "provider_config.provider_auth_mode",
    "provider_catalog_mode": "provider_config.provider_catalog_mode",
    "provider_transport_profile": "provider_config.provider_transport_profile",
    "request_json": "provider_catalog.request_json",
}


def _canonical(dotted):
    module_name, attr = dotted.rsplit(".", 1)
    module = importlib.import_module("agentic_debugger.application." + module_name)
    return getattr(module, attr)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(tmp_path / "pcs.json"))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(tmp_path / "q.json"))
    monkeypatch.setenv("AGENTIC_DEBUGGER_DISABLE_SECURE_STORE", "1")
    for var in ("OPENCODE_API_KEY", "COMMAND_CODE_API_KEY", "OLLAMA_API_KEY",
                "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
                "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
                "AGENTIC_DEBUGGER_OLLAMA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "oc-home"))
    facade.clear_all_session_keys()
    facade._QUARANTINED_PROVIDERS.clear()
    yield tmp_path
    facade.clear_all_session_keys()
    facade._QUARANTINED_PROVIDERS.clear()


class TestFacadeSurface:
    def test_all_exact_order(self):
        assert facade.__all__ == BASELINE_ALL

    def test_every_public_symbol_resolves(self):
        for name in BASELINE_ALL:
            assert getattr(facade, name, None) is not None, name

    def test_star_import_matches_all(self):
        namespace = {}
        exec("from agentic_debugger.application.provider_connections import *", namespace)
        exported = {k for k in namespace if not k.startswith("__")}
        assert exported == set(BASELINE_ALL)

    def test_identity_spots_are_canonical_objects(self):
        for name, dotted in IDENTITY_SPOTS.items():
            assert getattr(facade, name) is _canonical(dotted), name

    def test_compat_extras_resolve_to_canonical_objects(self):
        for name, dotted in COMPAT_EXTRAS.items():
            assert getattr(facade, name) is _canonical(dotted), name

    def test_facade_defines_no_classes_or_functions(self):
        src = Path(facade.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        defined = [n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        assert defined == []

    def test_shared_mutable_singletons_are_canonical(self):
        from agentic_debugger.application import provider_config, provider_credentials, provider_identity
        assert facade._SESSION_KEYS is provider_credentials._SESSION_KEYS
        assert facade._QUARANTINED_PROVIDERS is provider_config._QUARANTINED_PROVIDERS
        assert facade._CONTRACTS is provider_identity._CONTRACTS
        assert facade._CONTRACTS is provider_identity._BUILTIN_CONTRACTS

    def test_secure_backends_exposed_and_canonical(self):
        # The global conftest isolation fixture installs in-memory doubles
        # for these four backends on both namespaces, so strict ``is``
        # identity cannot be observed in-process here. Prove it in a fresh
        # interpreter without conftest (no pytest plugins loaded there):
        # facade attributes must be the canonical authority objects.
        import subprocess
        import sys
        probe = (
            "from agentic_debugger.application import provider_connections as f; "
            "from agentic_debugger.application import provider_credentials as c; "
            "names = ('save_secure_credential', 'load_secure_credential', "
            "'has_secure_credential', 'delete_secure_credential'); "
            "missing = [n for n in names if getattr(f, n) is not getattr(c, n)]; "
            "raise SystemExit(1 if missing else 0)"
        )
        completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
        assert completed.returncode == 0, completed.stderr[-2000:]


class TestConfigurationDiscovery:
    def test_add_get_list_round_trip(self, isolated):
        cfg = facade.add_provider_config(
            name="Facade Probe",
            base_url="https://api.example.com/v1",
            api_format=facade.PROTOCOL_CHAT_COMPLETIONS,
        )
        assert facade.get_provider_config(cfg.provider_id) == cfg
        assert [c.provider_id for c in facade.list_configured_providers()] == [cfg.provider_id]
        assert facade.is_known_provider(cfg.provider_id) is True

    def test_config_root_isolation(self, isolated):
        cfg = facade.add_provider_config(
            name="Root Probe",
            base_url="https://api.example.com/v1",
            api_format=facade.PROTOCOL_CHAT_COMPLETIONS,
        )
        assert facade.provider_configurations_path() == isolated / "pcs.json"
        assert (isolated / "pcs.json").is_file()
        assert facade.catalog_cache_path() == isolated / "cache.json"
        assert facade.provider_quarantine_path() == isolated / "q.json"
        assert facade.get_provider_config(cfg.provider_id) is not None


class TestCredentialAndStatus:
    def test_session_key_source_and_resolution(self, isolated):
        facade.add_provider_config(
            name="Cred Probe",
            base_url="https://api.example.com/v1",
            api_format=facade.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="cred_probe",
        )
        facade.set_session_key("cred_probe", "facade-secret-1")
        assert facade.credential_source_for("cred_probe") == facade.CREDENTIAL_SOURCE_SESSION_KEY
        assert facade.resolve_runtime_credential("cred_probe") == "facade-secret-1"

    def test_presence_only_status(self, isolated):
        facade.add_provider_config(
            name="Status Probe",
            base_url="https://api.example.com/v1",
            api_format=facade.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="status_probe",
        )
        before = facade.provider_connection_status("status_probe")
        assert before.connected is False
        facade.set_session_key("status_probe", "facade-secret-2")
        after = facade.provider_connection_status("status_probe")
        assert after.connected is True
        assert after.credential_source == "session_key"
        assert "facade-secret-2" not in repr(after)


class TestModelEnumeration:
    def test_historical_enumeration(self, isolated):
        facade.add_provider_config(
            name="OpenCode Go",
            base_url="https://opencode.ai/zen/go/v1",
            api_format=facade.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="opencode_go",
            transport_profile=facade.TRANSPORT_OPENCODE_GO,
        )
        assert facade.resolve_model_protocol("opencode_go", "opencode-go/kimi-k3") == \
            facade.PROTOCOL_CHAT_COMPLETIONS
        assert facade.resolve_model_protocol("opencode_go", "opencode-go/minimax-m3") == \
            facade.PROTOCOL_MESSAGES
        assert facade.effective_model_protocol("opencode_go", "opencode-go/kimi-k3") == \
            facade.PROTOCOL_CHAT_COMPLETIONS
        assert facade.is_protocol_executable("opencode_go", facade.PROTOCOL_CHAT_COMPLETIONS) is True

    def test_commandcode_goat_path(self, isolated):
        facade.add_provider_config(
            name="CommandCode GOAT",
            base_url="https://api.commandcode.ai/provider/v1",
            api_format=facade.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="commandcode_goat",
            transport_profile=facade.TRANSPORT_COMMANDCODE_GOAT,
        )
        assert facade.resolve_commandcode_protocol("anthropic/claude-opus-5") == facade.PROTOCOL_MESSAGES
        assert facade.resolve_commandcode_protocol("deepseek/deepseek-v4-flash") == \
            facade.PROTOCOL_CHAT_COMPLETIONS
        assert facade.inference_path_for("commandcode_goat", facade.PROTOCOL_MESSAGES) == "/messages"
        assert facade.provider_api_model_id(
            "commandcode_goat", "deepseek/deepseek-v4-flash") == "deepseek/deepseek-v4-flash"

    def test_direct_api_path(self, isolated):
        facade.add_provider_config(
            name="Direct Probe",
            base_url="https://api.example.com/v1",
            api_format=facade.PROTOCOL_CHAT_COMPLETIONS,
            provider_id="direct_probe",
        )
        assert facade.inference_path_for("direct_probe", facade.PROTOCOL_CHAT_COMPLETIONS) == \
            "/chat/completions"
        assert facade.provider_api_model_id("direct_probe", "gpt-x") == "gpt-x"
        assert facade.provider_base_url("direct_probe") == "https://api.example.com/v1"


class TestImportGraph:
    IMPLEMENTATION = (
        "provider_identity",
        "provider_config",
        "provider_credentials",
        "provider_protocols",
        "provider_catalog",
        "provider_management",
    )

    def _top_level_deps(self, module_name):
        from agentic_debugger.application import provider_connections as _unused  # noqa: F401
        path = Path("agentic_debugger/application") / (module_name + ".py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        deps = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "agentic_debugger.application":
                    deps.update(a.name for a in node.names if a.name in self.IMPLEMENTATION)
                elif mod.startswith("agentic_debugger.application.provider_"):
                    leaf = mod.rsplit(".", 1)[-1]
                    if leaf in self.IMPLEMENTATION:
                        deps.add(leaf)
        return deps

    def test_no_implementation_module_imports_facade(self):
        for module_name in self.IMPLEMENTATION:
            path = Path("agentic_debugger/application") / (module_name + ".py")
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                        "provider_connections"):
                    raise AssertionError(f"{module_name} imports the facade")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name != "agentic_debugger.application.provider_connections", \
                            module_name

    def test_static_dag_is_layered_and_acyclic(self):
        order = list(self.IMPLEMENTATION)
        pos = {name: index for index, name in enumerate(order)}
        for module_name in self.IMPLEMENTATION:
            for dep in self._top_level_deps(module_name):
                assert pos[dep] < pos[module_name], (module_name, dep)
