"""Regression coverage for the provider-platform integrity goal.

Covers the FirstMate blockers and the deterministic offline
fake-provider validation matrix.  No live provider contact, no spend,
no credentials in tracked state: every secret here is a synthetic
test-only token.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))

from fake_provider_server import (
    FakeProviderServer,
    catalog_payload,
    scripted_chat_completion,
    scripted_messages_output,
    scripted_responses_output,
)

from agentic_debugger.application import provider_connections as pc
from agentic_debugger.application import model_providers as mp
from agentic_debugger.application.provider_connections import (
    AUTH_ANTHROPIC,
    AUTH_BEARER,
    AUTH_NONE,
    CATALOG_DISABLED,
    CATALOG_OPENAI,
    PROTOCOL_CHAT_COMPLETIONS,
    PROTOCOL_MESSAGES,
    PROTOCOL_RESPONSES,
    TRANSPORT_COMMANDCODE_GOAT,
    TRANSPORT_GENERIC,
    TRANSPORT_OLLAMA_CLOUD,
    TRANSPORT_OPENCODE_GO,
    ProviderConfig,
    ProviderConnectionError,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "opencode-home"))
    for var in (
        "OLLAMA_API_KEY",
        "OPENCODE_API_KEY",
        "COMMAND_CODE_API_KEY",
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
        "AGENTIC_DEBUGGER_OLLAMA_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    store: dict[str, str] = {}
    monkeypatch.setattr(pc, "save_secure_credential", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in store)
    monkeypatch.setattr(pc, "delete_secure_credential", lambda k: store.pop(k, None) is not None)
    monkeypatch.setattr(pc, "catalog_cache_path", lambda: tmp_path / "catalog-cache.json")
    monkeypatch.setattr(pc, "provider_quarantine_path", lambda: tmp_path / "quarantine.json")
    pc.clear_all_session_keys()
    yield
    pc.clear_all_session_keys()


def _directive() -> str:
    return '{"kind": "action", "name": "get_source_window", "arguments": {"path": "a.py", "start_line": 1, "end_line": 5}}'


# -- blocker 1: generated ID collision at the 32-char boundary ---------

def test_generated_id_at_max_length_survives_reload():
    name32 = "a" * 32
    first = pc.add_provider_config(
        name=name32, base_url="https://api.example.com/v1", api_format=PROTOCOL_CHAT_COMPLETIONS
    )
    assert pc.is_valid_provider_id(first.provider_id)
    assert len(first.provider_id) <= 32
    reloaded = pc.load_provider_configurations()
    assert any(c.provider_id == first.provider_id for c in reloaded)


def test_generated_id_collision_suffixes_stay_in_grammar():
    name32 = "b" * 32
    ids = set()
    for _ in range(5):
        cfg = pc.add_provider_config(
            name=name32, base_url="https://api.example.com/v1", api_format=PROTOCOL_CHAT_COMPLETIONS
        )
        assert pc.is_valid_provider_id(cfg.provider_id), cfg.provider_id
        assert len(cfg.provider_id) <= 32
        assert cfg.provider_id not in ids
        ids.add(cfg.provider_id)
        # Every successful add survives immediate reload.
        assert any(c.provider_id == cfg.provider_id for c in pc.load_provider_configurations())


def test_near_boundary_names_generate_bounded_ids():
    for name in ("c" * 31, "d" * 30 + "!!", "e" * 32 + " extra words !!!"):
        cfg = pc.add_provider_config(
            name=name, base_url="https://api.example.com/v1", api_format=PROTOCOL_CHAT_COMPLETIONS
        )
        assert pc.is_valid_provider_id(cfg.provider_id)


# -- blocker 3: duplicate ADD rejection --------------------------------

def test_duplicate_explicit_add_rejected():
    pc.add_provider_config(
        name="Dup", base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS, provider_id="dup_provider",
    )
    with pytest.raises(ProviderConnectionError, match="already exists"):
        pc.add_provider_config(
            name="Dup Two", base_url="https://api.other.example/v1",
            api_format=PROTOCOL_CHAT_COMPLETIONS, provider_id="dup_provider",
        )
    # Original untouched.
    assert pc.get_provider_config("dup_provider").base_url == "https://api.example.com/v1"


# -- blocker 4: strict durable validation ------------------------------

def test_strict_boolean_rejection():
    assert ProviderConfig.from_dict({
        "provider_id": "p1", "name": "P", "base_url": "https://api.example.com/v1",
        "api_format": PROTOCOL_CHAT_COMPLETIONS, "enabled": "false",
    }) is None
    assert ProviderConfig.from_dict({
        "provider_id": "p1", "name": "P", "base_url": "https://api.example.com/v1",
        "api_format": PROTOCOL_CHAT_COMPLETIONS, "is_builtin": "false",
    }) is None
    assert ProviderConfig.from_dict({
        "provider_id": "p1", "name": "P", "base_url": "https://api.example.com/v1",
        "api_format": PROTOCOL_CHAT_COMPLETIONS, "tls_signature_blocked": 1,
    }) is None


def test_malformed_models_collection_rejected():
    for bad_models in ("x", {"a": 1}, 123, None):
        assert ProviderConfig.from_dict({
            "provider_id": "p1", "name": "P", "base_url": "https://api.example.com/v1",
            "api_format": PROTOCOL_CHAT_COMPLETIONS, "models": bad_models,
        }) is None
    # Malformed entries fail instead of disappearing.
    assert ProviderConfig.from_dict({
        "provider_id": "p1", "name": "P", "base_url": "https://api.example.com/v1",
        "api_format": PROTOCOL_CHAT_COMPLETIONS, "models": ["not-a-mapping"],
    }) is None
    assert ProviderConfig.from_dict({
        "provider_id": "p1", "name": "P", "base_url": "https://api.example.com/v1",
        "api_format": PROTOCOL_CHAT_COMPLETIONS, "models": [{"display_name": "No ID"}],
    }) is None


def test_malformed_model_ids_rejected():
    for bad_id in ["", "x" * 129, "bad id with spaces!", "ctl\x01char", "-leading-dash"]:
        assert ProviderConfig.from_dict({
            "provider_id": "p1", "name": "P", "base_url": "https://api.example.com/v1",
            "api_format": PROTOCOL_CHAT_COMPLETIONS,
            "models": [{"model_id": bad_id, "display_name": "D", "protocol": PROTOCOL_CHAT_COMPLETIONS}],
        }) is None, bad_id
    # Duplicate model identities fail.
    assert ProviderConfig.from_dict({
        "provider_id": "p1", "name": "P", "base_url": "https://api.example.com/v1",
        "api_format": PROTOCOL_CHAT_COMPLETIONS,
        "models": [
            {"model_id": "m1", "display_name": "M1", "protocol": PROTOCOL_CHAT_COMPLETIONS},
            {"model_id": "m1", "display_name": "M1b", "protocol": PROTOCOL_CHAT_COMPLETIONS},
        ],
    }) is None
    # Unknown protocol fails; None is honestly unresolved.
    assert ProviderConfig.from_dict({
        "provider_id": "p1", "name": "P", "base_url": "https://api.example.com/v1",
        "api_format": PROTOCOL_CHAT_COMPLETIONS,
        "models": [{"model_id": "m1", "display_name": "M1", "protocol": "bogus"}],
    }) is None


def test_malformed_file_fails_closed_and_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = pc.provider_configurations_path()
    raw = json.dumps({
        "schema_version": "provider-configurations-v2",
        "providers": [{
            "provider_id": "p1", "name": "P", "base_url": "https://api.example.com/v1",
            "api_format": PROTOCOL_CHAT_COMPLETIONS, "enabled": "false", "models": [],
        }],
    }).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with pytest.raises(ProviderConnectionError):
        pc.load_provider_configurations()
    assert path.read_bytes() == raw


# -- blocker 5: save validates like load --------------------------------

def test_save_rejects_malformed_state():
    bad = ProviderConfig(
        provider_id="ok_id", name="P", base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS, enabled=True, is_builtin=False,
    )
    # Bypass constructor checks via object.__setattr__ on frozen dataclass.
    object.__setattr__(bad, "enabled", "false")  # type: ignore[assignment]
    with pytest.raises(ProviderConnectionError):
        pc.save_provider_configurations([bad])


def test_save_reload_round_trip():
    cfg = pc.add_provider_config(
        name="Round Trip", base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS, api_key="test-key-123",
    )
    pc.add_manual_model(cfg.provider_id, "model-a")
    reloaded = pc.get_provider_config(cfg.provider_id)
    assert reloaded is not None
    assert reloaded.name == "Round Trip"
    assert [m.model_id for m in reloaded.models] == ["model-a"]


def test_provider_count_limit():
    for i in range(pc._MAX_PROVIDERS_CONFIGURED):
        pc.add_provider_config(
            name=f"P{i}", base_url="https://api.example.com/v1",
            api_format=PROTOCOL_CHAT_COMPLETIONS, provider_id=f"p_{i:03d}",
        )
    with pytest.raises(ProviderConnectionError, match="limit"):
        pc.add_provider_config(
            name="Overflow", base_url="https://api.example.com/v1",
            api_format=PROTOCOL_CHAT_COMPLETIONS, provider_id="overflow_x",
        )


def test_duplicate_durable_identity_fails(tmp_path: Path):
    path = pc.provider_configurations_path()
    entry = {
        "provider_id": "dup", "name": "A", "base_url": "https://api.example.com/v1",
        "api_format": PROTOCOL_CHAT_COMPLETIONS, "models": [], "enabled": True,
        "is_builtin": False, "builtin_kind": None, "tls_signature_blocked": False,
        "auth_mode": AUTH_BEARER, "catalog_mode": CATALOG_OPENAI,
        "last_refresh_utc": None, "last_refresh_source": None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps({"schema_version": "provider-configurations-v2", "providers": [entry, entry]}).encode())
    with pytest.raises(ProviderConnectionError, match="duplicate"):
        pc.load_provider_configurations()


# -- blocker 6: manual models strict ------------------------------------

def test_manual_model_rejects_bad_ids_and_protocols():
    cfg = pc.add_provider_config(
        name="Manual", base_url="https://api.example.com/v1", api_format=PROTOCOL_CHAT_COMPLETIONS
    )
    with pytest.raises(ProviderConnectionError):
        pc.add_manual_model(cfg.provider_id, "x" * 200)
    with pytest.raises(ProviderConnectionError):
        pc.add_manual_model(cfg.provider_id, "bad\x00id")
    with pytest.raises(ProviderConnectionError):
        pc.add_manual_model(cfg.provider_id, "good-id", protocol="bogus-proto")
    # None means provider default per documented contract.
    m = pc.add_manual_model(cfg.provider_id, "good-id", protocol=None)
    assert m.protocol == PROTOCOL_CHAT_COMPLETIONS


# -- blocker 2: endpoint/credential binding ------------------------------

def test_endpoint_change_blocked_with_session_credential():
    cfg = pc.add_provider_config(
        name="Bind", base_url="https://api.example.com/v1", api_format=PROTOCOL_CHAT_COMPLETIONS
    )
    pc.set_session_key(cfg.provider_id, "session-secret-1")
    with pytest.raises(ProviderConnectionError, match="re-enter"):
        pc.update_provider_config(cfg.provider_id, base_url="https://evil.example/v1")
    assert pc.get_provider_config(cfg.provider_id).base_url == "https://api.example.com/v1"


def test_endpoint_change_blocked_with_forwarded_env(monkeypatch: pytest.MonkeyPatch):
    cfg = pc.add_provider_config(
        name="Fwd", base_url="https://api.example.com/v1", api_format=PROTOCOL_CHAT_COMPLETIONS
    )
    from agentic_debugger.application.provider_connections import _session_env_var_for

    monkeypatch.setenv(_session_env_var_for(cfg.provider_id), "forwarded-secret-1")
    with pytest.raises(ProviderConnectionError, match="re-enter"):
        pc.update_provider_config(cfg.provider_id, base_url="https://evil.example/v1")


def test_endpoint_change_blocked_with_provider_env(monkeypatch: pytest.MonkeyPatch):
    pc.add_provider_config(
        name="Ollama", base_url="https://ollama.com", api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="ollama_cloud", transport_profile=pc.TRANSPORT_OLLAMA_CLOUD,
    )
    monkeypatch.setenv("OLLAMA_API_KEY", "env-secret-1")
    with pytest.raises(ProviderConnectionError, match="re-enter"):
        pc.update_provider_config("ollama_cloud", base_url="https://evil.example/v1")
    # Ambient env no longer resolves against the edited endpoint even if
    # the file is hand-edited: runtime binding stays fail-closed.
    cfgs = pc.load_provider_configurations()
    edited = [c for c in cfgs if c.provider_id == "ollama_cloud"][0]
    object.__setattr__(edited, "base_url", "https://evil.example/v1")
    pc.save_provider_configurations([c for c in cfgs if c.provider_id != "ollama_cloud"] + [edited])
    assert pc.resolve_runtime_credential("ollama_cloud") is None


def test_endpoint_change_blocked_with_cli_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pc.add_provider_config(
        name="OpenCode Go", base_url="https://opencode.ai/zen/go/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS, provider_id="opencode_go",
        transport_profile=pc.TRANSPORT_OPENCODE_GO,
    )
    store = tmp_path / "auth.json"
    store.write_text(json.dumps({"opencode-go": {"key": "cli-secret-1"}}), encoding="utf-8")
    monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: store)
    with pytest.raises(ProviderConnectionError, match="re-enter"):
        pc.update_provider_config("opencode_go", base_url="https://evil.example/v1")


# -- blocker 7: corrupt registry surfaced --------------------------------

def test_corrupt_registry_surfaced_to_picker(tmp_path: Path):
    path = pc.provider_configurations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not json")
    with pytest.raises(mp.ProviderRegistryError, match="configuration error"):
        mp.list_provider_models()
    # Doctor surfaces corruption instead of healthy zero state.
    avail = mp.provider_availability()
    assert any("configuration error" in (reason or "") for _, _, reason in avail)


def test_fresh_install_zero_providers():
    assert pc.load_provider_configurations() == []
    assert mp.list_provider_models() == []
    assert mp.provider_availability() == []


def test_arbitrary_provider_in_doctor_and_picker():
    cfg = pc.add_provider_config(
        name="Groq Direct", base_url="https://api.groq.com/openai/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS, api_key="gsk-test-1",
    )
    pc.add_manual_model(cfg.provider_id, "llama-3.3-70b-versatile")
    avail = {k: (ok, r) for k, ok, r in mp.provider_availability()}
    assert avail[cfg.provider_id][0] is True
    models = mp.list_provider_models()
    assert any(m.kind == cfg.provider_id for m in models)


# -- provider compatibility: auth/protocol matrix -------------------------

def _add_loopback(name: str, auth: str, proto: str, key: str | None = None):
    with FakeProviderServer(lambda req: (200, catalog_payload(["m1"]))) as server:
        cfg = pc.add_provider_config(
            name=name, base_url=server.base_url, api_format=proto,
            auth_mode=auth, api_key=key,
        )
        return cfg, server.base_url


def test_auth_protocol_combinations_validated():
    with pytest.raises(ProviderConnectionError):
        pc.add_provider_config(
            name="Bad", base_url="https://api.example.com/v1",
            api_format=PROTOCOL_RESPONSES, auth_mode=AUTH_ANTHROPIC,
        )
    with pytest.raises(ProviderConnectionError):
        pc.add_provider_config(
            name="Bad", base_url="http://127.0.0.1:9/v1",
            api_format=PROTOCOL_MESSAGES, auth_mode=AUTH_NONE,
        )
    with pytest.raises(ProviderConnectionError):
        pc.add_provider_config(
            name="Bad", base_url="https://api.example.com/v1",
            api_format=PROTOCOL_CHAT_COMPLETIONS, auth_mode=AUTH_NONE,
        )


def test_bearer_chat_completions_round_trip():
    directive = _directive()

    def handler(request):
        assert request["authorization"] == "Bearer test-bearer-1"
        if request["path"] == "/models":
            return 200, catalog_payload(["chat-model-a"])
        return 200, scripted_chat_completion(directive)

    with FakeProviderServer(handler) as server:
        cfg = pc.add_provider_config(
            name="Bearer Chat", base_url=server.base_url,
            api_format=PROTOCOL_CHAT_COMPLETIONS, auth_mode=AUTH_BEARER,
            api_key="test-bearer-1",
        )
        snap = pc.refresh_provider_catalog(cfg.provider_id, engine="stdlib")
        assert [m.model_id for m in snap.models] == ["chat-model-a"]
        live, prov = mp.resolve_provider_live_config(cfg.provider_id, "chat-model-a")
        assert prov["route"] == "direct_api"
        assert prov["api_protocol"] == PROTOCOL_CHAT_COMPLETIONS


def test_bearer_responses_round_trip():
    directive = _directive()

    def handler(request):
        if request["path"] == "/models":
            return 200, catalog_payload(["resp-model"])
        assert request["path"] == "/responses"
        return 200, scripted_responses_output(directive)

    with FakeProviderServer(handler) as server:
        cfg = pc.add_provider_config(
            name="Bearer Resp", base_url=server.base_url,
            api_format=PROTOCOL_RESPONSES, auth_mode=AUTH_BEARER,
            api_key="test-bearer-2",
        )
        pc.refresh_provider_catalog(cfg.provider_id, engine="stdlib")
        live, prov = mp.resolve_provider_live_config(cfg.provider_id, "resp-model")
        assert prov["api_protocol"] == PROTOCOL_RESPONSES


def test_bearer_messages_gateway_round_trip():
    directive = _directive()

    def handler(request):
        assert request["authorization"] == "Bearer test-bearer-3"
        if request["path"] == "/models":
            return 200, catalog_payload(["msg-model"])
        assert request["path"] == "/messages"
        return 200, scripted_messages_output(directive)

    with FakeProviderServer(handler) as server:
        cfg = pc.add_provider_config(
            name="Bearer Msg", base_url=server.base_url,
            api_format=PROTOCOL_MESSAGES, auth_mode=AUTH_BEARER,
            api_key="test-bearer-3",
        )
        pc.refresh_provider_catalog(cfg.provider_id, engine="stdlib")
        live, prov = mp.resolve_provider_live_config(cfg.provider_id, "msg-model")
        assert prov["api_protocol"] == PROTOCOL_MESSAGES


def test_native_anthropic_messages_auth():
    directive = _directive()
    seen: dict = {}

    def handler(request):
        seen.update(request)
        if request["path"] == "/models":
            return 200, catalog_payload(["claude-fake"])
        assert request["path"] == "/messages"
        assert request["authorization"] is None
        assert request["x_api_key"] == "anthropic-test-1"
        assert request["anthropic_version"] == "2023-06-01"
        return 200, scripted_messages_output(directive)

    with FakeProviderServer(handler) as server:
        cfg = pc.add_provider_config(
            name="Anthropic Native", base_url=server.base_url,
            api_format=PROTOCOL_MESSAGES, auth_mode=AUTH_ANTHROPIC,
            api_key="anthropic-test-1",
        )
        pc.refresh_provider_catalog(cfg.provider_id, engine="stdlib")
        assert seen["x_api_key"] == "anthropic-test-1"
        live, prov = mp.resolve_provider_live_config(cfg.provider_id, "claude-fake")
        assert prov["auth_mode"] == AUTH_ANTHROPIC


def test_noauth_loopback_executes_without_credential():
    directive = _directive()

    def handler(request):
        assert request["authorization"] is None
        assert request["x_api_key"] is None
        if request["path"] == "/models":
            return 200, catalog_payload(["local-model"])
        return 200, scripted_chat_completion(directive)

    with FakeProviderServer(handler) as server:
        cfg = pc.add_provider_config(
            name="Local NoAuth", base_url=server.base_url,
            api_format=PROTOCOL_CHAT_COMPLETIONS, auth_mode=AUTH_NONE,
        )
        snap = pc.refresh_provider_catalog(cfg.provider_id, engine="stdlib")
        assert len(snap.models) == 1
        live, prov = mp.resolve_provider_live_config(cfg.provider_id, "local-model")
        assert prov["auth_mode"] == AUTH_NONE
        status = pc.provider_connection_status(cfg.provider_id)
        assert status.connected is True and status.runnable is True


def test_manual_only_provider_without_models_endpoint():
    def handler(request):
        raise AssertionError("catalog must never be contacted for manual-only providers")

    with FakeProviderServer(handler):
        cfg = pc.add_provider_config(
            name="Manual Only", base_url="http://127.0.0.1:9/v1",
            api_format=PROTOCOL_CHAT_COMPLETIONS, auth_mode=AUTH_BEARER,
            catalog_mode=CATALOG_DISABLED, api_key="k1",
        )
        with pytest.raises(ProviderConnectionError, match="disabled"):
            pc.refresh_provider_catalog(cfg.provider_id, engine="stdlib")
        pc.add_manual_model(cfg.provider_id, "hand-written-model")
        live, prov = mp.resolve_provider_live_config(cfg.provider_id, "hand-written-model")
        assert prov["route"] == "direct_api"


def test_catalog_failure_preserves_last_good():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, catalog_payload(["stable-model"])
        return 500, {"error": "boom"}

    with FakeProviderServer(handler) as server:
        cfg = pc.add_provider_config(
            name="Flaky", base_url=server.base_url,
            api_format=PROTOCOL_CHAT_COMPLETIONS, api_key="k1",
        )
        pc.refresh_provider_catalog(cfg.provider_id, engine="stdlib")
        with pytest.raises(ProviderConnectionError):
            pc.refresh_provider_catalog(cfg.provider_id, engine="stdlib")
        assert pc.get_provider_config(cfg.provider_id).models[0].model_id == "stable-model"


def test_http_error_surfaces_as_typed_failure():
    def handler(request):
        if request["path"] == "/models":
            return 200, catalog_payload(["m1"])
        return 401, {"error": {"message": "bad key", "type": "auth"}}

    with FakeProviderServer(handler) as server:
        cfg = pc.add_provider_config(
            name="Auth Fail", base_url=server.base_url,
            api_format=PROTOCOL_CHAT_COMPLETIONS, api_key="bad-key",
        )
        pc.refresh_provider_catalog(cfg.provider_id, engine="stdlib")
        import subprocess

        live, _ = mp.resolve_provider_live_config(cfg.provider_id, "m1")
        env = dict(__import__("os").environ)
        env.update(pc.provider_transport_credential_environment(cfg.provider_id) or {})
        env["PYTHONPATH"] = f"{REPO_ROOT}{';' if __import__('os').name == 'nt' else ':'}{REPO_ROOT / 'scripts'}"
        import json as _json

        req = {"protocol": {"version": "1.3", "logical_model_call_index": 0},
               "context": {"task_id": "t", "state": "UNDERSTAND"}}
        res = subprocess.run(list(live.command), input=(_json.dumps(req) + "\n").encode(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env=env)
        assert res.returncode == 1
        assert "bad-key" not in res.stderr.decode()


def test_missing_credential_fails_before_execution():
    cfg = pc.add_provider_config(
        name="NoCred", base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS, auth_mode=AUTH_BEARER,
    )
    pc.add_manual_model(cfg.provider_id, "m1")
    with pytest.raises(mp.ProviderRegistryError, match="credential"):
        mp.resolve_provider_live_config(cfg.provider_id, "m1")


def test_connection_probe_is_credential_safe():
    with FakeProviderServer(lambda req: (200, catalog_payload(["m1"]))) as server:
        cfg = pc.add_provider_config(
            name="Probe", base_url=server.base_url,
            api_format=PROTOCOL_CHAT_COMPLETIONS, api_key="probe-secret-xyz",
        )
        result = pc.test_provider_connection(cfg.provider_id, engine="stdlib")
        assert result["ok"] is True
        assert "probe-secret-xyz" not in json.dumps(result)


def test_v1_config_migrates_deterministically(tmp_path: Path):
    path = pc.provider_configurations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps({
        "schema_version": "provider-configurations-v1",
        "providers": [{
            "provider_id": "legacy_x", "name": "Legacy",
            "base_url": "https://api.example.com/v1",
            "api_format": PROTOCOL_CHAT_COMPLETIONS, "models": [],
        }],
    }).encode())
    cfgs = pc.load_provider_configurations()
    assert cfgs[0].auth_mode == AUTH_BEARER
    assert cfgs[0].catalog_mode == CATALOG_OPENAI
