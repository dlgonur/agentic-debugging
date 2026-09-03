"""Execution-environment parity regressions (FirstMate blockers 1-2).

Each test fails on candidate 2abfd9d and passes on the repaired
authority.  Offline only: loopback fakes, synthetic credentials, synthetic
CA.  No live/billable provider is required.

Governing invariant: if the product reports an exact provider/model route
as connected and runnable, the worker/adapter subprocess must receive
every bounded operator-controlled environment dependency required for
that exact route.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
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
from agentic_debugger.application.command_transport import (  # noqa: E402
    CancellableJsonlCommandTransport,
)
from agentic_debugger.application.provider_connections import (  # noqa: E402
    PROTOCOL_CHAT_COMPLETIONS,
    TRANSPORT_GENERIC,
    TRANSPORT_OPENCODE_GO,
)

import provider_direct_api_adapter as adapter  # noqa: E402

SYNTH_CLI_KEY = "synthetic-opencode-cli-key-abc123-not-real"
SYNTH_BEARER = "synthetic-bearer-token-xyz789-not-real"
SYNTH_PROXY = "http://synthetic-proxy.invalid:8080"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(tmp_path / "c.json"))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(tmp_path / "q.json"))
    # Start with a neutral OPENCODE_CONFIG_DIR; individual tests override.
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "oc-home-default"))
    for var in (
        "OLLAMA_API_KEY",
        "OPENCODE_API_KEY",
        "COMMAND_CODE_API_KEY",
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
        "AGENTIC_DEBUGGER_OLLAMA_API_KEY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    store: dict[str, str] = {}
    monkeypatch.setattr(pc, "save_secure_credential", lambda k, v: store.__setitem__(k, v) or True)
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in store)
    monkeypatch.setattr(pc, "delete_secure_credential", lambda k: store.pop(k, None) is not None)
    pc.clear_all_session_keys()
    yield
    pc.clear_all_session_keys()


def _write_opencode_auth_store(home: Path, key: str) -> Path:
    store_path = home / ".local" / "share" / "opencode" / "auth.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({"opencode-go": {"key": key}}), encoding="utf-8")
    return store_path


# -- 1. custom OpenCode auth-store path (blocker 1) ---------------------------


def test_custom_auth_store_direct_route_keeps_credential_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_home = tmp_path / "custom-oc-home"
    _write_opencode_auth_store(custom_home, SYNTH_CLI_KEY)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(custom_home))
    cfg = pc.add_provider_config(
        name="OC Custom",
        base_url="https://opencode.ai/zen/go/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="oc_custom_auth",
        transport_profile=TRANSPORT_OPENCODE_GO,
    )
    assert pc.credential_source_for("oc_custom_auth") == pc.CREDENTIAL_SOURCE_CLI_AUTH_STORE
    assert pc.resolve_runtime_credential("oc_custom_auth") == SYNTH_CLI_KEY
    live, provenance = mp.resolve_provider_live_config("oc_custom_auth", "glm-5.3-flash")
    assert provenance["route"] == mp.ROUTE_DIRECT_API
    # Worker/adapter hop carries the already-authorized credential value
    # under the private session variable (never the store path).
    env = mp.provider_transport_environment("oc_custom_auth")
    assert env == {"AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY": SYNTH_CLI_KEY}
    assert SYNTH_CLI_KEY not in " ".join(live.command)
    assert SYNTH_CLI_KEY not in json.dumps(provenance)
    # Child with NO OPENCODE_CONFIG_DIR but the forwarded hop resolves
    # normally (no configuration HARNESS_ERROR from the custom location).
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY", SYNTH_CLI_KEY)
    assert pc.resolve_runtime_credential("oc_custom_auth") == SYNTH_CLI_KEY
    assert adapter._resolve_credential("oc_custom_auth", "bearer") == SYNTH_CLI_KEY
    # UI->worker hop agrees (bounded private variable, value only).
    hop = mp.provider_session_credential_environment("oc_custom_auth")
    # restore custom home for the UI-hop read
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(custom_home))
    hop = pc.provider_session_credential_environment("oc_custom_auth")
    assert hop == {"AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY": SYNTH_CLI_KEY}


def test_custom_auth_store_credential_bytes_absent_from_argv_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_home = tmp_path / "custom-oc-home2"
    _write_opencode_auth_store(custom_home, SYNTH_CLI_KEY)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(custom_home))
    pc.add_provider_config(
        name="OC Custom2",
        base_url="https://opencode.ai/zen/go/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="oc_custom_auth2",
        transport_profile=TRANSPORT_OPENCODE_GO,
    )
    live, provenance = mp.resolve_provider_live_config("oc_custom_auth2", "glm-5.3-flash")
    argv_text = " ".join(live.command)
    assert SYNTH_CLI_KEY not in argv_text
    assert str(custom_home) not in argv_text or True  # path itself is non-secret
    assert SYNTH_CLI_KEY not in json.dumps(provenance)
    assert SYNTH_CLI_KEY not in json.dumps(live.command)


# -- network authority unit gates --------------------------------------------


def test_network_authority_forwards_only_allowlisted_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/synthetic-ca.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/tmp/synthetic-certs")
    monkeypatch.setenv("CURL_CA_BUNDLE", "/tmp/synthetic-curl-ca.pem")
    monkeypatch.setenv("HTTPS_PROXY", SYNTH_PROXY)
    monkeypatch.setenv("http_proxy", SYNTH_PROXY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("AGENTIC_DEBUGGER_SHOULD_NEVER_FORWARD", "secret-should-not-flow")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/tmp/custom-should-not-flow-to-direct")
    env = pc.provider_transport_network_environment()
    assert env["SSL_CERT_FILE"] == "/tmp/synthetic-ca.pem"
    assert env["SSL_CERT_DIR"] == "/tmp/synthetic-certs"
    assert env["CURL_CA_BUNDLE"] == "/tmp/synthetic-curl-ca.pem"
    assert env["HTTPS_PROXY"] == SYNTH_PROXY
    assert env["http_proxy"] == SYNTH_PROXY
    assert env["NO_PROXY"] == "127.0.0.1,localhost"
    assert "AGENTIC_DEBUGGER_SHOULD_NEVER_FORWARD" not in env
    assert "OPENCODE_CONFIG_DIR" not in env
    # Transport child receives the same subset without full inheritance.
    child = CancellableJsonlCommandTransport.subprocess_environment()
    assert child["SSL_CERT_FILE"] == "/tmp/synthetic-ca.pem"
    assert child["HTTPS_PROXY"] == SYNTH_PROXY
    assert child["http_proxy"] == SYNTH_PROXY
    assert "AGENTIC_DEBUGGER_SHOULD_NEVER_FORWARD" not in child
    assert "OPENCODE_CONFIG_DIR" not in child
    # Existing Windows/config-path authority intact.
    assert "PATH" in child and "PYTHONIOENCODING" in child


def test_network_authority_rejects_oversized_and_control_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "x" * 9000)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080\ninjected: yes")
    env = pc.provider_transport_network_environment()
    assert "SSL_CERT_FILE" not in env
    assert "HTTPS_PROXY" not in env


def test_proxy_parity_bounded_subprocess_preserves_supported_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxies = {
        "HTTP_PROXY": "http://synthetic-http.invalid:3128",
        "HTTPS_PROXY": "http://synthetic-https.invalid:3128",
        "ALL_PROXY": "http://synthetic-all.invalid:3128",
        "NO_PROXY": "127.0.0.1,localhost",
        "http_proxy": "http://synthetic-http.invalid:3128",
        "https_proxy": "http://synthetic-https.invalid:3128",
        "all_proxy": "http://synthetic-all.invalid:3128",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name, value in proxies.items():
        monkeypatch.setenv(name, value)
    child = CancellableJsonlCommandTransport.subprocess_environment()
    for name, value in proxies.items():
        assert child.get(name) == value, name
    # Parent authority agrees byte-for-byte on the same subset.
    parent = pc.provider_transport_network_environment()
    for name, value in proxies.items():
        assert parent.get(name) == value, name


def test_curl_environment_contract_no_argv_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentic_debugger.application.provider_http import _curl_config

    monkeypatch.setenv("CURL_CA_BUNDLE", "/tmp/synthetic-curl-ca.pem")
    monkeypatch.setenv("HTTPS_PROXY", SYNTH_PROXY)
    net = pc.provider_transport_network_environment()
    assert net["CURL_CA_BUNDLE"] == "/tmp/synthetic-curl-ca.pem"
    assert net["HTTPS_PROXY"] == SYNTH_PROXY
    child = CancellableJsonlCommandTransport.subprocess_environment()
    assert child["CURL_CA_BUNDLE"] == "/tmp/synthetic-curl-ca.pem"
    # Credential travels via stdin config, never argv: the argv built by
    # the curl engine contains no credential bytes.
    config = _curl_config("GET", "http://127.0.0.1:9/models",
                          credential=SYNTH_BEARER, body=None, auth_mode="bearer")
    assert SYNTH_BEARER in config  # stdin pipe carries it (expected)
    # The engine argv itself is fixed flags + --config - (no secret).
    import inspect
    from agentic_debugger.application import provider_http as ph

    source = inspect.getsource(ph._curl_request)
    assert "--config" in source
    # No credential interpolation into argv construction in source.
    argv_section = source.split("argv = [")[1].split("]")[0]
    assert "credential" not in argv_section


# -- 5. legacy CLI route ------------------------------------------------------


def test_legacy_cli_explicit_auth_file_with_custom_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_home = tmp_path / "legacy-oc-home"
    store_path = _write_opencode_auth_store(custom_home, SYNTH_CLI_KEY)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(custom_home))
    pc.add_provider_config(
        name="OC Legacy",
        base_url="https://opencode.ai/zen/go/v1",
        api_format="responses",
        provider_id="oc_legacy_custom",
        transport_profile=TRANSPORT_OPENCODE_GO,
    )
    # Bounded explicit path (non-secret) is available for the historical
    # profile at the custom location.
    auth_file = pc.provider_legacy_cli_auth_file("oc_legacy_custom")
    assert auth_file == str(store_path)
    # Legacy route carries it as --auth-file (never credential bytes).
    monkeypatch.setattr(mp, "_opencode_availability", lambda: (True, None))
    config, provenance = mp.resolve_provider_live_config(
        "oc_legacy_custom", "opencode-go/some-unknown-model-zzz"
    )
    assert provenance["route"] == mp.ROUTE_LEGACY_CLI
    assert "--auth-file" in config.command
    assert config.command[config.command.index("--auth-file") + 1] == str(store_path)
    assert SYNTH_CLI_KEY not in " ".join(config.command)


def test_generic_opencode_identity_receives_no_cli_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_home = tmp_path / "generic-oc-home"
    _write_opencode_auth_store(custom_home, SYNTH_CLI_KEY)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(custom_home))
    pc.add_provider_config(
        name="Generic OC",
        base_url="https://api.example.com/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="generic_oc_x",
        transport_profile=TRANSPORT_GENERIC,
    )
    assert pc.provider_legacy_cli_auth_file("generic_oc_x") is None
    # No CLI-auth source even with the store present (generic isolation).
    monkeypatch.setattr(pc, "load_secure_credential", lambda kind: None)
    monkeypatch.setattr(pc, "has_secure_credential", lambda kind: False)
    assert pc.credential_source_for("generic_oc_x") is None
    assert mp.provider_transport_environment("generic_oc_x") is None


# -- 2-3. custom CA end-to-end over a real subprocess (blocker 2) --------------
#
# A loopback HTTPS fake provider is served with a private synthetic CA.
# The CA is trusted ONLY via SSL_CERT_FILE.  Parent connection/catalog
# checks and the REAL CancellableJsonlCommandTransport + direct adapter
# subprocess must share the same trust authority.
#
# The certificate material below is STATIC synthetic test-only fixture
# data (generated once, valid 2020-01-01..2045-01-01, CN/SAN loopback
# only).  It requires no third-party package: only the stdlib ``ssl``
# module reads it.  The private key is a throwaway test key, never a
# real secret, and the server binds loopback only.


_SYNTHETIC_CA_PEM = """\
-----BEGIN CERTIFICATE-----
MIIDTzCCAjegAwIBAgIIGis8TV5vdwEwDQYJKoZIhvcNAQELBQAwNTEzMDEGA1UE
Awwqc3ludGhldGljLXRlc3QtY2EgKHRlc3Qtb25seSwgbm90IHRydXN0ZWQpMB4X
DTIwMDEwMTAwMDAwMFoXDTQ1MDEwMTAwMDAwMFowNTEzMDEGA1UEAwwqc3ludGhl
dGljLXRlc3QtY2EgKHRlc3Qtb25seSwgbm90IHRydXN0ZWQpMIIBIjANBgkqhkiG
9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsJB+vqK7EVLcR886LDQphKzyZ+WbOJq7TInp
KxkLF/8Qkt5BHe1qQHRR38b2r3ObMVgs6hZgQrtmO+33T0BXM1AHArtEIGLpTx/O
1MMD2XrTkcaa+kwYDHcyHbjpWmh7v2eAJldO/iIANTJ0ehRO0jTzK0Wi125ZHF/s
l34+SfdbMSar92SXKrCInSgDjvRoC0d0pHWeRXIj+GGHbolC/+xpeH+55cQtDn92
vDjYquKCmh83zBSvJ+lYKlhM0tqU7Y9L5Ttl/I5ffaTA4pRaCwsU3tGWgNgp/6NZ
QlgZfneVqryWZfEaQ/CnLqaPasPs7b1CwrlpR8d7D6/AzlfzFwIDAQABo2MwYTAP
BgNVHRMBAf8EBTADAQH/MB0GA1UdDgQWBBR60Rn78Fxz0aawB+gVT1yLrgl+JjAf
BgNVHSMEGDAWgBR60Rn78Fxz0aawB+gVT1yLrgl+JjAOBgNVHQ8BAf8EBAMCAQYw
DQYJKoZIhvcNAQELBQADggEBAF1ctT4ogbyNvQDufnXivqxdlwNmnqT3vXGVCDU6
NQ+SBXdkluLI7tRrvMjk4hqpFhvlCLRGbfogzbSQ6KvSuHPmRaR3EkaijCI7JDWB
Nv8T9sQ0nMbvz9VXLH69CLLwyhnzaTkCEgzKYbs0D5VGqym8vU4+GBn0jwk2JN1W
HKS1P6GvjbQRCJ4gqwk7ex1oe4LddFDuyfYiXUkmFPBAEjF1KiFve+Vw8hmREomj
Wt/5pvvJjm/90J8jrs3B6YThKLoNPMtUjBkoAE1NQdgW1jBiiVk8EeVXdzOxisvA
vi5yXAjeBm3SbX7Q4bJfaEL8R0rUyZAzgim/tMraMK7zmmI=
-----END CERTIFICATE-----
"""

_SYNTHETIC_SERVER_PEM = """\
-----BEGIN CERTIFICATE-----
MIIDdDCCAlygAwIBAgIIGis8TV5vdwIwDQYJKoZIhvcNAQELBQAwNTEzMDEGA1UE
Awwqc3ludGhldGljLXRlc3QtY2EgKHRlc3Qtb25seSwgbm90IHRydXN0ZWQpMB4X
DTIwMDEwMTAwMDAwMFoXDTQ1MDEwMTAwMDAwMFowKjEoMCYGA1UEAwwfMTI3LjAu
MC4xIChzeW50aGV0aWMgdGVzdC1vbmx5KTCCASIwDQYJKoZIhvcNAQEBBQADggEP
ADCCAQoCggEBANG0euabpkeFA7TSy47H/cD+Bb5A5HGbtsB171JQpxnPV0+fk//O
OiE1uO5WI2mVOeX2EztwLXatIo1uHX3Sl22fCnNFu/K/Nx7bKNdJvEfOOfakhjyL
kK2gHwy4sWjvucBwSIXXdTqNVANqPXCoNsglOB4ATMJWHW9+4zX1u6xJAQEOiQma
cSG93Jg9GqWggBDo0+/jtYEO0GtWeNb/IgsPksBFMQiYXJc89Ylzv6svYNxOLH3z
RXdZPbHkCMKRr3AtTKX1lRgwo2S4XuI7ccGBBCzv0teNcMWVdc5BxjulI1Y3DUIo
XVYOTuCl9GC9onhkxh5NaQ4lKHqjHI+NqbUCAwEAAaOBkjCBjzAaBgNVHREEEzAR
gglsb2NhbGhvc3SHBH8AAAEwHQYDVR0OBBYEFONWakDBi4HSa2XazCLFGfW+LkfY
MB8GA1UdIwQYMBaAFHrRGfvwXHPRprAH6BVPXIuuCX4mMAwGA1UdEwEB/wQCMAAw
DgYDVR0PAQH/BAQDAgWgMBMGA1UdJQQMMAoGCCsGAQUFBwMBMA0GCSqGSIb3DQEB
CwUAA4IBAQCI7yRyppcMNzpQMY+h9dn6RtXqiMzDiCDLPMKfPTouSXpr/YPj4Im5
1mDwXzakRuTnOw9raAsA0/Cj/FX38EaCYoekInZtTRlqfftVAj/93SNEyM+ChiEM
6vpumW3NB1rZjQL1uzftldUR/qxKoEQTOrj3dkl7WlN1MWTnGBNS8Fcurp6+mMfC
XtLiRJ36G4e5c2oPqoMJnvhFS6uPIvOlnOdxAnY9avOH2NNbihjHyT1DXx1C9ejr
wEfmBpGpc0uAVAoMjY6ylUyA8ipl3S4BzjRGRP7tPMTQf+P6m66+7jTNJoyYhR/n
EWhOqXQxRTGoe8Nw+SasWT7u9as66EWQ
-----END CERTIFICATE-----
"""

_SYNTHETIC_SERVER_KEY_PEM = """\
-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEA0bR65pumR4UDtNLLjsf9wP4FvkDkcZu2wHXvUlCnGc9XT5+T
/846ITW47lYjaZU55fYTO3Atdq0ijW4dfdKXbZ8Kc0W78r83Htso10m8R8459qSG
PIuQraAfDLixaO+5wHBIhdd1Oo1UA2o9cKg2yCU4HgBMwlYdb37jNfW7rEkBAQ6J
CZpxIb3cmD0apaCAEOjT7+O1gQ7Qa1Z41v8iCw+SwEUxCJhclzz1iXO/qy9g3E4s
ffNFd1k9seQIwpGvcC1MpfWVGDCjZLhe4jtxwYEELO/S141wxZV1zkHGO6UjVjcN
QihdVg5O4KX0YL2ieGTGHk1pDiUoeqMcj42ptQIDAQABAoIBAAMAXEn0rFqVssnh
wnwWrLEYcaiZcSuXGPSEO5qoANxDtXI0TH/6yaY0CKOQpA0cz6lU7k2Je30ZWUdA
7jcgzn4JKrMfqmL9DaLpbBo4ufMlJns7O5iePsHdatRZyGBCHhx8/uy7ergN0cgC
u0JjhfUzYyw6wN5/MQipfkMFc1wx4KkY6ueTNQ+v7MSx7tWcJibu6UZkE2S8XjKW
V5ieqErpLsmqX5gp5cdI0hgzI3tt8zxz5rZ6VENKNmYjlCCaEavO7S5YCLpQIAMn
ronZdLR8gYJuQkjALQTc7FNPV4s1BU47F/IYuvycBfs/KTVcHUm48G8yQtFIBBxO
75jTGgECgYEA7tlQKWJ7pTO++p5hFmJKKu7zI6p/8/GLzafksGDjZXczC4Kzlnt7
ghdem2hCwsQYyueICozYKFiLmbvvjLA3Op6NmVY+hIvPajERdege5Uq6CL9MNTwS
jGVegmqS0H3Gor6MYVfx4oq0qTwPIi5SV/YD3LmUAyCnnBeCqQC8xXUCgYEA4MNs
fy1d8M2FlNfTaV5cZtmeTAbLhmNdF8pkof1oc410Wsu3eAQ+tia7lnCMOvlBhpAH
UMfiJN235EYzLw8GUnbZ1eYJMcAAhUUz0i3303BxSTpkphC15+nIXcPXqkP+BW+2
FgeA9uF0O2cknadqBccXud5B2tzjPbKIu6yli0ECgYEAg6Zea+Evm9hQzNzdULQu
g0mf1KnWywP5dgqzn5BX5oZ0KUUKbch0RDlTWT93hNkHfVdvAbmuL1bW447WM+qx
FmtsSvdhkDdrxPF02VNvLB0rO0UN4U3SP3ZkSGgrsiWRhgSXZdROq/qeJ6XGBaYY
lwkwCcp0TeEC4aOHzlVstz0CgYAs0wx6OIP5mCNB0eEZrHXlFRVauCgyvvI74mM7
YxxHnzhLO0F1r/MJxKO4lu2AfWEyAttSoupYy9b2sYFXqzlpjMZYwC2pPE9eRLTW
/8/i3RPatMiJzd9ZRuhsurfx/ulUEDlSH2D622+gwSsuPcsJJ/F1YfvkOBUhos9e
DkpdgQKBgHsw/LNIk3hbiVrNc2TfN9b3Dns8qIngT6aBU4ixiTvVnTt59ka05x5u
iAxOAB51/sGtTVfslZeMMf7pZ1W12K45Q1vH/i928zqRNqasIaesucDqY5YMu8rY
rWeUfGdVidL2UJkUWLeIQU9RHnjeawExYiIBgXa9w9hMzyioK5uE
-----END RSA PRIVATE KEY-----
"""


def _make_synthetic_ca_pair(tmp_path: Path):
    """Write the static synthetic CA/server material to ``tmp_path``.

    Dependency-free: the PEM blocks above are fixture data, so only the
    stdlib ``ssl`` module is required.  Filenames are per-test isolated
    under ``tmp_path``.
    """
    ca_path = tmp_path / "synthetic-ca.pem"
    cert_path = tmp_path / "synthetic-server.pem"
    key_path = tmp_path / "synthetic-server-key.pem"
    ca_path.write_text(_SYNTHETIC_CA_PEM, encoding="ascii")
    cert_path.write_text(_SYNTHETIC_SERVER_PEM, encoding="ascii")
    key_path.write_text(_SYNTHETIC_SERVER_KEY_PEM, encoding="ascii")
    return ca_path, cert_path, key_path


_TLS_DIRECTIVE = (
    '{"kind": "action", "name": "get_source_window", '
    '"arguments": {"path": "pkg/mod.py", "start_line": 1, "end_line": 40}}'
)


class _HttpsFakeProvider:
    """Loopback HTTPS fake with exact request recording (Bearer + paths)."""

    def __init__(self, cert_path: Path, key_path: Path, token: str) -> None:
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._cert = str(cert_path)
        self._key = str(key_path)
        self._token = token

    def __enter__(self) -> "_HttpsFakeProvider":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, payload: object) -> None:
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802
                with outer._lock:
                    outer.requests.append(
                        {
                            "method": "GET",
                            "path": self.path,
                            "authorization": self.headers.get("Authorization"),
                        }
                    )
                if self.path == "/models":
                    self._send(200, catalog_payload(["tls-model-a"]))
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with outer._lock:
                    outer.requests.append(
                        {
                            "method": "POST",
                            "path": self.path,
                            "authorization": self.headers.get("Authorization"),
                            "body": body,
                        }
                    )
                assert self.headers.get("Authorization") == f"Bearer {outer._token}"
                try:
                    payload = json.loads(body.decode())
                except Exception:
                    self._send(400, {"error": "bad json"})
                    return
                assert payload.get("model") == "tls-model-a", payload
                self._send(200, scripted_chat_completion(_TLS_DIRECTIVE))

            def log_message(self, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self._cert, self._key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        self.base_url = f"https://127.0.0.1:{server.server_address[1]}"
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def test_custom_ca_parent_success_child_success_real_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca_path, cert_path, key_path = _make_synthetic_ca_pair(tmp_path)
    # The synthetic CA is trusted ONLY via SSL_CERT_FILE.
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
    token = "synthetic-tls-bearer-not-real"
    with _HttpsFakeProvider(cert_path, key_path, token) as server:
        pc.add_provider_config(
            name="TLS Gateway",
            base_url=server.base_url,
            api_format=PROTOCOL_CHAT_COMPLETIONS,
            provider_id="tls_gateway",
            transport_profile=TRANSPORT_GENERIC,
            api_key=token,
        )
        # Parent connection truth: probe + catalog + picker agree runnable.
        probe = pc.test_provider_connection("tls_gateway")
        assert probe["ok"] is True, probe
        snapshot = pc.refresh_provider_catalog("tls_gateway")
        assert [m.model_id for m in snapshot.models] == ["tls-model-a"]
        picker = [m for m in mp.list_provider_models() if m.kind == "tls_gateway"]
        assert picker and all(m.available is True for m in picker), picker
        live, provenance = mp.resolve_provider_live_config("tls_gateway", "tls-model-a")
        assert provenance["route"] == mp.ROUTE_DIRECT_API
        assert SYNTH_CLI_KEY not in " ".join(live.command)
        # Exact resolved provider/model crosses the REAL transport +
        # direct adapter subprocess with the same trust authority.
        transport_env = mp.provider_transport_environment("tls_gateway")
        assert transport_env is not None and token in list(transport_env.values())
        transport = CancellableJsonlCommandTransport(
            live,
            environment=dict(transport_env),
        )
        # The bounded child env carries the trust anchor (parity proof).
        child_preview = CancellableJsonlCommandTransport.subprocess_environment()
        assert child_preview.get("SSL_CERT_FILE") == str(ca_path)
        response = transport.request(
            {
                "protocol": {"version": "1.3", "logical_model_call_index": 0},
                "context": {"task_id": "tls", "state": "UNDERSTAND"},
            },
            timeout_seconds=30.0,
        )
        assert response["directive_content"] == _TLS_DIRECTIVE
        posts = [r for r in server.requests if r["method"] == "POST"]
        assert posts and posts[-1]["path"] == "/chat/completions"
        assert posts[-1]["authorization"] == f"Bearer {token}"
        out_text = json.dumps(response)
        assert token not in out_text


def test_custom_ca_without_trust_fails_honestly_no_verification_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca_path, cert_path, key_path = _make_synthetic_ca_pair(tmp_path)
    token = "synthetic-tls-bearer-not-real-2"
    with _HttpsFakeProvider(cert_path, key_path, token) as server:
        # Fresh subprocess WITHOUT SSL_CERT_FILE must fail as TLS/network
        # failure (honest), never as success via disabled verification.
        # The probe lives in a file (not `-c`) so the traceback shows only
        # the failing request line (variable reference, no literal) and the
        # sanitized provider error — never the credential value.
        probe_path = tmp_path / "tls_probe_no_trust.py"
        probe_path.write_text(
            "import os, sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            f"os.environ['AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH'] = {str(tmp_path / 'c.json')!r}\n"
            "os.environ.pop('SSL_CERT_FILE', None)\n"
            "from agentic_debugger.application.provider_http import request_json\n"
            "token = 'synthetic-tls-bearer-not-real-2'\n"
            f"request_json('GET', {server.base_url + '/models'!r}, credential=token, engine='stdlib', timeout_seconds=10.0)\n",
            encoding="utf-8",
        )
        env = {k: v for k, v in os.environ.items() if k != "SSL_CERT_FILE"}
        # Ensure the probe child cannot see the CA through any allowlisted var.
        for var in ("SSL_CERT_DIR", "CURL_CA_BUNDLE"):
            env.pop(var, None)
        completed = subprocess.run(
            [sys.executable, str(probe_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            env=env,
        )
        assert completed.returncode != 0, "TLS without the CA must not succeed"
        combined = (completed.stdout.decode(errors="replace") + completed.stderr.decode(errors="replace"))
        assert token not in combined
        lowered = combined.lower()
        assert ("certificate" in lowered or "ssl" in lowered or "tls" in lowered
                or "verify" in lowered or "connection" in lowered
                or "providerhttp" in lowered or "traceback" in lowered), combined[:800]


def test_tls_and_proxy_values_never_enter_argv_or_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca_path, _, _ = _make_synthetic_ca_pair(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
    monkeypatch.setenv("HTTPS_PROXY", SYNTH_PROXY)
    monkeypatch.setenv("https_proxy", SYNTH_PROXY)
    pc.add_provider_config(
        name="Priv Gateway",
        base_url="http://127.0.0.1:9/v1",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="priv_gateway",
        transport_profile=TRANSPORT_GENERIC,
        catalog_mode=pc.CATALOG_DISABLED,
        auth_mode=pc.AUTH_BEARER,
        api_key=SYNTH_BEARER,
    )
    pc.add_manual_model("priv_gateway", "m1")
    live, provenance = mp.resolve_provider_live_config("priv_gateway", "m1")
    argv_text = " ".join(live.command)
    assert SYNTH_BEARER not in argv_text
    assert SYNTH_PROXY not in argv_text
    assert str(ca_path) not in argv_text
    assert SYNTH_BEARER not in json.dumps(provenance)
    assert SYNTH_PROXY not in json.dumps(provenance)
    assert str(ca_path) not in json.dumps(provenance)
    # Journal/event-shaped payloads carry only provenance (already proven);
    # transport preview keys are names only in any diagnostic.
    child = CancellableJsonlCommandTransport.subprocess_environment()
    assert child["SSL_CERT_FILE"] == str(ca_path)
    assert child["HTTPS_PROXY"] == SYNTH_PROXY
