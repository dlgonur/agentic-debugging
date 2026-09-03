"""Second-pass regressions: effective-model authority and explicit transport profiles.

Each test below fails on candidate 7ebfd2e (per-model auth bypass,
late transport-capability failure, hidden ID-derived semantics, child
Bearer guessing) and passes on the repaired authority.  Offline only:
loopback fakes, mocked vault, synthetic tokens.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from fake_provider_server import (  # noqa: E402
    FakeProviderServer,
    catalog_payload,
    scripted_chat_completion,
)

from agentic_debugger.application import provider_connections as pc  # noqa: E402
from agentic_debugger.application import model_providers as mp  # noqa: E402
from agentic_debugger.application.provider_connections import (  # noqa: E402
    AUTH_ANTHROPIC,
    AUTH_BEARER,
    AUTH_NONE,
    CATALOG_DISABLED,
    PROTOCOL_CHAT_COMPLETIONS,
    PROTOCOL_MESSAGES,
    PROTOCOL_RESPONSES,
    TRANSPORT_COMMANDCODE_GOAT,
    TRANSPORT_GENERIC,
    TRANSPORT_OLLAMA_CLOUD,
    TRANSPORT_OPENCODE_GO,
    ProviderConnectionError,
)

import provider_direct_api_adapter as adapter  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(tmp_path / "c.json"))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "oc-home"))
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
    monkeypatch.setattr(pc, "catalog_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(pc, "provider_quarantine_path", lambda: tmp_path / "q.json")
    pc.clear_all_session_keys()
    yield
    pc.clear_all_session_keys()


def _protocol_request(index: int = 0) -> dict:
    return {
        "protocol": {"version": "1.3", "logical_model_call_index": index},
        "context": {"task_id": "t", "state": "UNDERSTAND"},
    }


class _FakeStdin:
    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


# -- 1-2. no-auth manual models with Messages/Responses are rejected -----

def test_noauth_manual_messages_rejected_not_runnable():
    cfg = pc.add_provider_config(
        name="NoAuth", base_url="http://127.0.0.1:9/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS, auth_mode=AUTH_NONE,
        catalog_mode=CATALOG_DISABLED,
    )
    with pytest.raises(ProviderConnectionError):
        pc.add_manual_model(cfg.provider_id, "m1", protocol=PROTOCOL_MESSAGES)


def test_noauth_manual_responses_rejected_not_runnable():
    cfg = pc.add_provider_config(
        name="NoAuth", base_url="http://127.0.0.1:9/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS, auth_mode=AUTH_NONE,
        catalog_mode=CATALOG_DISABLED,
    )
    with pytest.raises(ProviderConnectionError):
        pc.add_manual_model(cfg.provider_id, "m1", protocol=PROTOCOL_RESPONSES)


# -- 3. anthropic manual models limited to Messages -----------------------

def test_anthropic_manual_chat_rejected():
    cfg = pc.add_provider_config(
        name="Anth", base_url="https://api.example.com/v1",
        api_format=PROTOCOL_MESSAGES, auth_mode=AUTH_ANTHROPIC, api_key="k1",
    )
    with pytest.raises(ProviderConnectionError):
        pc.add_manual_model(cfg.provider_id, "m1", protocol=PROTOCOL_CHAT_COMPLETIONS)
    with pytest.raises(ProviderConnectionError):
        pc.add_manual_model(cfg.provider_id, "m2", protocol=PROTOCOL_RESPONSES)
    ok = pc.add_manual_model(cfg.provider_id, "m3", protocol=PROTOCOL_MESSAGES)
    assert ok.protocol == PROTOCOL_MESSAGES


# -- 4. historical resolver cannot bypass the auth matrix ------------------

def test_historical_resolver_cannot_bypass_auth_matrix():
    cfg = pc.add_provider_config(
        name="OC", base_url="https://opencode.ai/zen/go/v1",
        api_format=PROTOCOL_MESSAGES, auth_mode=AUTH_ANTHROPIC, api_key="k1",
        provider_id="oc_auth_test", transport_profile=TRANSPORT_OPENCODE_GO,
    )
    # minimax-m3 resolves to Messages via the historical map: executable.
    assert pc.effective_model_protocol("oc_auth_test", "minimax-m3") == PROTOCOL_MESSAGES
    # glm-5.3-flash resolves to Chat Completions: incompatible with Anthropic.
    with pytest.raises(ProviderConnectionError):
        pc.effective_model_protocol("oc_auth_test", "glm-5.3-flash")
    with pytest.raises(mp.ProviderRegistryError):
        mp.resolve_provider_live_config("oc_auth_test", "glm-5.3-flash")


# -- 5-6. historical capability preflighted before LiveModelConfig --------

def test_ollama_profile_responses_rejected_before_live_config():
    with pytest.raises(ProviderConnectionError, match="does not expose"):
        pc.add_provider_config(
            name="OC", base_url="https://ollama.com",
            api_format=PROTOCOL_RESPONSES, auth_mode=AUTH_BEARER,
            provider_id="oc_resp", transport_profile=TRANSPORT_OLLAMA_CLOUD,
            api_key="k1",
        )


def test_ollama_legacy_record_not_runnable_and_never_reaches_adapter():
    path = pc.provider_configurations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps({
        "schema_version": "provider-configurations-v2",
        "providers": [{
            "provider_id": "oc_old", "name": "Old",
            "base_url": "https://ollama.com", "api_format": PROTOCOL_RESPONSES,
            "auth_mode": AUTH_BEARER, "catalog_mode": "openai",
            "transport_profile": TRANSPORT_OLLAMA_CLOUD,
            "models": [{"model_id": "m", "display_name": "M",
                        "protocol": PROTOCOL_RESPONSES}],
            "enabled": True, "is_builtin": False, "builtin_kind": None,
            "tls_signature_blocked": False, "last_refresh_utc": None,
            "last_refresh_source": None,
        }],
    }).encode())
    assert pc.get_provider_config("oc_old") is not None  # loads for recovery
    pc.set_session_key("oc_old", "k1")
    models = [m for m in mp.list_provider_models() if m.kind == "oc_old"]
    assert models and all(m.available is False for m in models)
    avail = dict((k, ok) for k, ok, _ in mp.provider_availability())
    # Doctor agrees: no executable model, so not available despite credential.
    assert avail["oc_old"] is False
    with pytest.raises(mp.ProviderRegistryError, match="does not expose"):
        mp.resolve_provider_live_config("oc_old", "m")
    assert pc.test_provider_connection("oc_old", model_id="m")["ok"] is False


# -- 7. generic ollama_cloud keeps generic semantics -----------------------

def test_generic_ollama_cloud_identity_uses_generic_transport():
    cfg = pc.add_provider_config(
        name="Ollama Cloud", base_url="http://127.0.0.1:9999/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS, auth_mode=AUTH_NONE,
        catalog_mode=CATALOG_DISABLED,
    )
    assert cfg.provider_id == "ollama_cloud"
    assert cfg.transport_profile == TRANSPORT_GENERIC
    assert pc.inference_path_for("ollama_cloud", PROTOCOL_CHAT_COMPLETIONS) == "/chat/completions"
    assert pc.provider_environment_variable("ollama_cloud") is None
    assert pc.provider_tls_signature_blocked("ollama_cloud") is False
    # No historical model resolver: unknown ids fall back to the default.
    assert pc.resolve_model_protocol("ollama_cloud", "anything-at-all") == PROTOCOL_CHAT_COMPLETIONS
    assert pc.provider_api_model_id("ollama_cloud", "opencode-go/x") == "opencode-go/x"


# -- 8-9. generic opencode/commandcode identities: no history, no CLI -----

def test_generic_opencode_identity_has_no_history_or_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg = pc.add_provider_config(
        name="OpenCode Go", base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS, api_key="k1",
    )
    assert cfg.provider_id == "opencode_go"
    assert cfg.transport_profile == TRANSPORT_GENERIC
    # No historical protocol map: claude- prefix does NOT imply Messages.
    pc.add_manual_model("opencode_go", "claude-like-model")
    assert pc.resolve_model_protocol("opencode_go", "claude-like-model") == PROTOCOL_CHAT_COMPLETIONS
    # CLI present + auth store present must not open a legacy route.
    store = tmp_path / "auth.json"
    store.write_text(json.dumps({"opencode-go": {"key": "cli-key"}}), encoding="utf-8")
    monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: store)
    monkeypatch.setattr(mp.shutil, "which", lambda name: "/usr/bin/opencode" if name == "opencode" else None)
    assert pc.credential_source_for("opencode_go") is not None  # saved key
    pc.clear_all_session_keys()
    # Drop the saved key to force direct-unavailable; legacy must NOT rescue.
    monkeypatch.setattr(pc, "load_secure_credential", lambda kind: None)
    monkeypatch.setattr(pc, "has_secure_credential", lambda kind: False)
    with pytest.raises(mp.ProviderRegistryError):
        mp.resolve_provider_live_config("opencode_go", "claude-like-model")


def test_generic_commandcode_identity_has_no_resolver_or_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg = pc.add_provider_config(
        name="CommandCode GOAT", base_url="https://api.example.com/v1",
        api_format=PROTOCOL_RESPONSES, api_key="k1",
    )
    assert cfg.provider_id == "commandcode_goat"
    assert cfg.transport_profile == TRANSPORT_GENERIC
    # No historical claude->messages rule: default (responses) applies.
    pc.add_manual_model("commandcode_goat", "anthropic/claude-fake")
    assert pc.resolve_model_protocol("commandcode_goat", "anthropic/claude-fake") == PROTOCOL_RESPONSES
    monkeypatch.setattr(mp, "_first_on_path", lambda candidates: "/usr/bin/cmdc")
    (tmp_path / "cc.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mp, "_commandcode_auth_store_path", lambda: tmp_path / "cc.json")
    monkeypatch.setattr(pc, "load_secure_credential", lambda kind: None)
    monkeypatch.setattr(pc, "has_secure_credential", lambda kind: False)
    with pytest.raises(mp.ProviderRegistryError):
        mp.resolve_provider_live_config("commandcode_goat", "anthropic/claude-fake")


# -- 10. explicit historical profiles retain paths/resolvers ---------------

def test_explicit_historical_profiles_keep_contracts():
    pc.add_provider_config(
        name="OC", base_url="https://opencode.ai/zen/go/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS, provider_id="h_oc",
        transport_profile=TRANSPORT_OPENCODE_GO, api_key="k1",
    )
    assert pc.inference_path_for("h_oc", PROTOCOL_RESPONSES) == "/responses"
    assert pc.resolve_model_protocol("h_oc", "minimax-m3") == PROTOCOL_MESSAGES
    assert pc.provider_api_model_id("h_oc", "opencode-go/x") == "x"
    pc.add_provider_config(
        name="CC", base_url="https://api.commandcode.ai/provider/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS, provider_id="h_cc",
        transport_profile=TRANSPORT_COMMANDCODE_GOAT, api_key="k1",
    )
    assert pc.resolve_model_protocol("h_cc", "anthropic/claude-x") == PROTOCOL_MESSAGES
    assert pc.inference_path_for("h_cc", PROTOCOL_MESSAGES) == "/messages"
    pc.add_provider_config(
        name="OL", base_url="https://ollama.com",
        api_format=PROTOCOL_CHAT_COMPLETIONS, provider_id="h_ol",
        transport_profile=TRANSPORT_OLLAMA_CLOUD, api_key="k1",
    )
    assert pc.inference_path_for("h_ol", PROTOCOL_CHAT_COMPLETIONS) == "/v1/chat/completions"
    assert pc.provider_environment_variable("h_ol") == "OLLAMA_API_KEY"


def test_explicit_historical_legacy_cli_preserved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pc.add_provider_config(
        name="OC", base_url="https://opencode.ai/zen/go/v1",
        api_format=PROTOCOL_RESPONSES, provider_id="h_oc2",
        transport_profile=TRANSPORT_OPENCODE_GO,
    )
    # Undocumented model: no direct protocol, but legacy CLI is eligible.
    assert pc.resolve_model_protocol("h_oc2", "opencode-go/some-unknown-model-zzz") is None
    (tmp_path / "auth.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
    monkeypatch.setattr(mp, "_opencode_availability", lambda: (True, None))
    config, provenance = mp.resolve_provider_live_config("h_oc2", "opencode-go/deepseek-v4-flash")
    assert provenance["route"] == "legacy_cli"


# -- 11. child never guesses Bearer; zero HTTP on metadata failure ---------

def _run_child(provider: str, model: str, protocol: str, **kw):
    stdin = _FakeStdin(json.dumps(_protocol_request()).encode())
    out, err = io.StringIO(), io.StringIO()
    try:
        code = adapter.run_adapter(
            stdin, out, provider=provider, model=model, protocol=protocol,
            timeout_seconds=10.0, **kw,
        )
    except adapter.ProviderDirectApiError as exc:
        return 1, "", exc.kind, str(exc)
    return code, out.getvalue(), "", ""


def test_child_mismatched_auth_mode_fails_with_zero_http():
    with FakeProviderServer(lambda req: (200, {"unexpected": True})) as server:
        pc.add_provider_config(
            name="P", base_url=server.base_url,
            api_format=PROTOCOL_CHAT_COMPLETIONS, api_key="k1",
            provider_id="p_mismatch",
        )
        pc.add_manual_model("p_mismatch", "m")
        code, _, kind, _ = _run_child(
            "p_mismatch", "m", PROTOCOL_CHAT_COMPLETIONS, auth_mode="anthropic",
        )
        assert code == 1 and kind == "configuration"
        assert server.request_count == 0


def test_child_missing_config_fails_with_zero_http():
    with FakeProviderServer(lambda req: (200, {"unexpected": True})) as server:
        code, _, kind, _ = _run_child("ghost_provider", "m", PROTOCOL_CHAT_COMPLETIONS)
        assert code == 1 and kind == "configuration"
        assert server.request_count == 0


def test_child_corrupt_registry_fails_with_zero_http(tmp_path: Path):
    with FakeProviderServer(lambda req: (200, {"unexpected": True})):
        path = pc.provider_configurations_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{corrupt")
        code, _, kind, _ = _run_child("any_provider", "m", PROTOCOL_CHAT_COMPLETIONS)
        assert code == 1 and kind == "configuration"


def test_child_endpoint_disagreement_fails_with_zero_http():
    with FakeProviderServer(lambda req: (200, {"unexpected": True})) as server:
        pc.add_provider_config(
            name="P", base_url=server.base_url,
            api_format=PROTOCOL_CHAT_COMPLETIONS, api_key="k1",
            provider_id="p_ep",
        )
        pc.add_manual_model("p_ep", "m")
        code, _, kind, _ = _run_child(
            "p_ep", "m", PROTOCOL_CHAT_COMPLETIONS,
            base_url="http://127.0.0.1:9/other",
        )
        assert code == 1 and kind == "configuration"
        assert server.request_count == 0


# -- 12. picker/doctor/test/live agree on runnable=false --------------------

@pytest.mark.parametrize("auth,default,model_proto", [
    (AUTH_NONE, PROTOCOL_CHAT_COMPLETIONS, PROTOCOL_MESSAGES),
    (AUTH_NONE, PROTOCOL_CHAT_COMPLETIONS, PROTOCOL_RESPONSES),
    (AUTH_ANTHROPIC, PROTOCOL_MESSAGES, PROTOCOL_CHAT_COMPLETIONS),
    (AUTH_ANTHROPIC, PROTOCOL_MESSAGES, PROTOCOL_RESPONSES),
])
def test_gates_agree_on_impossible_effective(auth, default, model_proto):
    # Bypass write validation via raw JSON to simulate pre-repair state;
    # every gate must still report not-runnable.
    path = pc.provider_configurations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    base = "http://127.0.0.1:9/v1" if auth == AUTH_NONE else "https://api.example.com/v1"
    pid = f"g_{default}_{model_proto}".replace("-", "_")[:30]
    path.write_bytes(json.dumps({
        "schema_version": "provider-configurations-v2",
        "providers": [{
            "provider_id": pid, "name": "G", "base_url": base,
            "api_format": default, "auth_mode": auth,
            "catalog_mode": "disabled", "transport_profile": "generic",
            "models": [{"model_id": "bad-m", "display_name": "Bad",
                        "protocol": model_proto}],
            "enabled": True, "is_builtin": False, "builtin_kind": None,
            "tls_signature_blocked": False, "last_refresh_utc": None,
            "last_refresh_source": None,
        }],
    }).encode())
    # NOTE: from_dict now rejects this at load; the registry fails closed
    # with an entry error rather than showing it runnable anywhere.
    with pytest.raises(ProviderConnectionError):
        pc.load_provider_configurations()


def test_migration_guidance_for_ambiguous_historical_record():
    path = pc.provider_configurations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps({
        "schema_version": "provider-configurations-v2",
        "providers": [{
            "provider_id": "ollama_cloud", "name": "Ollama",
            "base_url": "https://ollama.com",
            "api_format": PROTOCOL_CHAT_COMPLETIONS,
            "auth_mode": AUTH_BEARER, "catalog_mode": "openai",
            "models": [], "enabled": True, "is_builtin": False,
            "builtin_kind": None, "tls_signature_blocked": False,
            "last_refresh_utc": None, "last_refresh_source": None,
        }],
    }).encode())
    before = path.read_bytes()
    with pytest.raises(ProviderConnectionError, match="transport_profile"):
        pc.load_provider_configurations()
    assert path.read_bytes() == before  # byte-for-byte untouched
    # Deterministic V1 generic migration still works for non-historical IDs.
    path.write_bytes(json.dumps({
        "schema_version": "provider-configurations-v1",
        "providers": [{
            "provider_id": "plain_gateway", "name": "Plain",
            "base_url": "https://api.example.com/v1",
            "api_format": PROTOCOL_CHAT_COMPLETIONS, "models": [],
        }],
    }).encode())
    cfgs = pc.load_provider_configurations()
    assert cfgs[0].transport_profile == TRANSPORT_GENERIC
    assert cfgs[0].auth_mode == AUTH_BEARER


def test_reserved_names_default_to_generic_profiles():
    for name, want_id in [
        ("Ollama Cloud", "ollama_cloud"),
        ("OpenCode Go", "opencode_go"),
        ("CommandCode GOAT", "commandcode_goat"),
    ]:
        cfg = pc.add_provider_config(
            name=name, base_url="http://127.0.0.1:9/v1",
            api_format=PROTOCOL_CHAT_COMPLETIONS, auth_mode=AUTH_NONE,
            catalog_mode=CATALOG_DISABLED,
        )
        assert cfg.provider_id == want_id, (name, cfg.provider_id)
        assert cfg.transport_profile == TRANSPORT_GENERIC
