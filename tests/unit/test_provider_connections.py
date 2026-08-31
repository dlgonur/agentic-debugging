"""Unit gates for provider connections, catalog discovery, and caching.

Covers protocol resolution (documented OpenCode Go mapping; documented
CommandCode routing rule; unknown ids stay unresolved), credential
source resolution (session key, environment, CLI auth store), catalog
normalization bounds, cache round-trip and fail-closed decoding,
connection status, and the credential-free character of every surface.
No test contacts a real provider: catalog refresh runs against a local
fake provider server with the stdlib engine.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_provider_server import catalog_payload  # noqa: E402
from agentic_debugger.application import provider_connections as pc  # noqa: E402

SECRET = "test-session-key-not-a-real-credential"


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pc, "catalog_cache_path", lambda: tmp_path / "cache.json")


@pytest.fixture(autouse=True)
def _clean_session_keys():
    pc.clear_all_session_keys()
    yield
    pc.clear_all_session_keys()


# -- protocol resolution -------------------------------------------------------


class TestProtocolResolution:
    @pytest.mark.parametrize(
        "model_id,expected",
        [
            ("opencode-go/glm-5.3", pc.PROTOCOL_CHAT_COMPLETIONS),
            ("opencode-go/glm-5.3-flash", pc.PROTOCOL_CHAT_COMPLETIONS),
            ("opencode-go/deepseek-v4-flash", pc.PROTOCOL_CHAT_COMPLETIONS),
            ("opencode-go/kimi-k3", pc.PROTOCOL_CHAT_COMPLETIONS),
            ("opencode-go/longcat-2.0", pc.PROTOCOL_CHAT_COMPLETIONS),
            ("opencode-go/hy3", pc.PROTOCOL_CHAT_COMPLETIONS),
            ("opencode-go/minimax-m3", pc.PROTOCOL_MESSAGES),
            ("opencode-go/minimax-m2.7", pc.PROTOCOL_MESSAGES),
            ("opencode-go/qwen3.8-max", pc.PROTOCOL_MESSAGES),
            ("opencode-go/qwen3.7-plus", pc.PROTOCOL_MESSAGES),
            ("opencode-go/gpt-5.6-luna", pc.PROTOCOL_RESPONSES),
            ("opencode-go/grok-4.6", pc.PROTOCOL_RESPONSES),
            ("opencode-go/muse-spark-1.2-contributor", pc.PROTOCOL_RESPONSES),
        ],
    )
    def test_documented_mapping(self, model_id: str, expected: str) -> None:
        assert pc.resolve_opencode_go_protocol(model_id) == expected

    @pytest.mark.parametrize(
        "model_id",
        [
            "opencode-go/glm-5",  # in live catalog, absent from the documented table
            "opencode-go/kimi-k2.5",
            "opencode-go/mimo-v2-pro",
            "opencode-go/hy3-preview",
            "opencode-go/brand-new-model",
        ],
    )
    def test_unknown_models_stay_unresolved(self, model_id: str) -> None:
        """Discovery never guesses: an undocumented model resolves to None."""

        assert pc.resolve_opencode_go_protocol(model_id) is None

    def test_documented_mapping_is_not_reused_across_bases(self) -> None:
        """The general Zen table routes minimax via chat/completions while
        the Go table routes it via /messages; the Go resolver must follow
        the Go contract only."""
        assert (
            pc._OPENCODE_GO_DOCUMENTED_PROTOCOLS["minimax-m3"]
            == pc.PROTOCOL_MESSAGES
        )

    @pytest.mark.parametrize(
        "model_id,expected",
        [
            ("claude-sonnet-5", pc.PROTOCOL_MESSAGES),
            ("claude-haiku-4.5-20251001", pc.PROTOCOL_MESSAGES),
            ("anthropic/claude-opus-5", pc.PROTOCOL_MESSAGES),
            ("deepseek/deepseek-v4-flash", pc.PROTOCOL_CHAT_COMPLETIONS),
            ("zai-org/glm-5.2", pc.PROTOCOL_CHAT_COMPLETIONS),
            ("moonshotai/Kimi-K3", pc.PROTOCOL_CHAT_COMPLETIONS),
            ("gpt-5.6-sol", pc.PROTOCOL_CHAT_COMPLETIONS),
            ("google/gemini-3.7-flash", pc.PROTOCOL_CHAT_COMPLETIONS),
        ],
    )
    def test_commandcode_documented_split(
        self, model_id: str, expected: str
    ) -> None:
        assert pc.resolve_commandcode_protocol(model_id) == expected

    def test_commandcode_routing_is_total_and_deterministic(self) -> None:
        """Every non-Anthropic identity has exactly one deterministic route."""

        assert pc.resolve_commandcode_protocol("some-future/model") == (
            pc.PROTOCOL_CHAT_COMPLETIONS
        )
        assert pc.resolve_commandcode_protocol("some-future/model") == (
            pc.resolve_commandcode_protocol("some-future/model")
        )

    def test_inference_path_matches_contract(self) -> None:
        assert (
            pc.inference_path_for("commandcode_goat", pc.PROTOCOL_MESSAGES)
            == "/messages"
        )
        assert (
            pc.inference_path_for("commandcode_goat", pc.PROTOCOL_CHAT_COMPLETIONS)
            == "/chat/completions"
        )
        with pytest.raises(pc.ProviderConnectionError):
            pc.inference_path_for("commandcode_goat", pc.PROTOCOL_RESPONSES)

    def test_unknown_provider_protocol_fails_closed(self) -> None:
        with pytest.raises(pc.ProviderConnectionError):
            pc.resolve_model_protocol("mystery", "model")

    def test_direct_api_model_identity_removes_only_opencode_tui_namespace(self) -> None:
        assert pc.provider_api_model_id(
            "opencode_go", "opencode-go/kimi-k3"
        ) == "kimi-k3"
        assert pc.provider_api_model_id(
            "commandcode_goat", "deepseek/deepseek-v4-flash"
        ) == "deepseek/deepseek-v4-flash"


# -- discovered models ----------------------------------------------------------


class TestDiscoveredModels:
    def test_unresolved_model_not_runnable_with_reason(self) -> None:
        model = pc.DiscoveredProviderModel.create(
            "opencode_go", "opencode-go/glm-5", "GLM 5"
        )
        assert model.runnable is False
        assert model.protocol is None
        assert model.unavailable_reason == "Protocol not yet resolved for direct API"

    def test_resolved_model_runnable(self) -> None:
        model = pc.DiscoveredProviderModel.create(
            "opencode_go", "opencode-go/kimi-k3", "Kimi K3"
        )
        assert model.runnable is True
        assert model.protocol == pc.PROTOCOL_CHAT_COMPLETIONS
        assert model.unavailable_reason is None


# -- credentials ------------------------------------------------------------------


class TestCredentials:
    def test_session_key_store_round_trip(self) -> None:
        assert pc.has_session_key("opencode_go") is False
        pc.set_session_key("opencode_go", SECRET)
        assert pc.has_session_key("opencode_go") is True
        assert pc.peek_session_key("opencode_go") == SECRET
        pc.clear_session_key("opencode_go")
        assert pc.has_session_key("opencode_go") is False

    def test_session_key_rejects_unknown_provider(self) -> None:
        with pytest.raises(pc.ProviderConnectionError):
            pc.set_session_key("ollama_cloud", SECRET)

    def test_session_key_rejects_blank_and_oversized(self) -> None:
        with pytest.raises(pc.ProviderConnectionError):
            pc.set_session_key("commandcode_goat", "   ")
        with pytest.raises(pc.ProviderConnectionError):
            pc.set_session_key("commandcode_goat", "k" * 5000)

    def test_credential_sources_with_control_characters_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(pc.ProviderConnectionError):
            pc.set_session_key("commandcode_goat", "secret\nheader-injection")
        monkeypatch.setenv("CMD_API_KEY", "secret\rinvalid")
        assert pc.credential_source_for("commandcode_goat") is None
        assert pc.resolve_runtime_credential("commandcode_goat") is None

    def test_resolution_order_session_key_beats_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CMD_API_KEY", "env-value")
        pc.set_session_key("commandcode_goat", SECRET)
        assert pc.resolve_runtime_credential("commandcode_goat") == SECRET
        assert pc.credential_source_for("commandcode_goat") == "session_key"
        pc.clear_all_session_keys()
        assert pc.resolve_runtime_credential("commandcode_goat") == "env-value"
        assert pc.credential_source_for("commandcode_goat") == "environment"

    def test_opencode_auth_store_is_consumable_source(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = tmp_path / "auth.json"
        store.write_text(
            json.dumps({"opencode-go": {"type": "api", "key": "store-key-abc"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: store)
        monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
        assert pc.credential_source_for("opencode_go") == "cli_auth_store"
        assert pc.resolve_runtime_credential("opencode_go") == "store-key-abc"

    def test_commandcode_auth_store_not_consumed_by_direct_route(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The CommandCode CLI auth store schema is not reliably
        established here, so the direct route fails closed to the
        environment/session-key sources instead of parsing it."""

        store = tmp_path / "auth.json"
        store.write_text(json.dumps({"key": "some-credential"}), encoding="utf-8")
        monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: store)
        monkeypatch.delenv("CMD_API_KEY", raising=False)
        assert pc.credential_source_for("commandcode_goat") is None

    def test_malformed_opencode_store_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = tmp_path / "auth.json"
        store.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(pc, "opencode_auth_store_path", lambda: store)
        monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
        assert pc.credential_source_for("opencode_go") is None

    def test_no_credential_leakage_into_status(self, monkeypatch, tmp_path) -> None:
        pc.set_session_key("opencode_go", SECRET)
        status = pc.provider_connection_status("opencode_go")
        rendered = repr(status) + json.dumps(status.__dict__, default=str)
        assert SECRET not in rendered


# -- catalog normalization / refresh ------------------------------------------------


@pytest.fixture
def fake_commandcode_endpoint(monkeypatch: pytest.MonkeyPatch):
    """Run refresh against a local fake of the CommandCode /models endpoint.

    The provider contract entry is replaced wholesale (loopback fake base
    URL, stdlib TLS profile), so no real provider is contacted.
    """
    from contextlib import contextmanager
    from dataclasses import replace as _replace
    from fake_provider_server import FakeProviderServer

    @contextmanager
    def factory(responder):
        with FakeProviderServer(responder) as server:
            original = pc._CONTRACTS["commandcode_goat"]
            fake = _replace(original, base_url=server.base_url, tls_signature_blocked=False)
            monkeypatch.setitem(pc._CONTRACTS, "commandcode_goat", fake)
            yield server

    return factory


@pytest.fixture
def fake_opencode_endpoint(monkeypatch: pytest.MonkeyPatch):
    """Run OpenCode discovery against a local fake of the documented endpoint."""

    from contextlib import contextmanager
    from dataclasses import replace as _replace
    from fake_provider_server import FakeProviderServer

    @contextmanager
    def factory(responder):
        with FakeProviderServer(responder) as server:
            original = pc._CONTRACTS["opencode_go"]
            fake = _replace(original, base_url=server.base_url, tls_signature_blocked=False)
            monkeypatch.setitem(pc._CONTRACTS, "opencode_go", fake)
            yield server

    return factory


class TestCatalogRefresh:
    def test_opencode_live_catalog_normalizes_bare_provider_ids(
        self, fake_opencode_endpoint
    ) -> None:
        with fake_opencode_endpoint(
            lambda request: (
                200,
                catalog_payload(["kimi-k3", "minimax-m3", "future-model"]),
            )
        ) as server:
            snapshot = pc.refresh_provider_catalog(
                "opencode_go", engine="stdlib", credential=SECRET
            )
        assert server.requests[0]["path"] == "/models"
        by_id = {model.model_id: model for model in snapshot.models}
        assert by_id["kimi-k3"].protocol == pc.PROTOCOL_CHAT_COMPLETIONS
        assert by_id["minimax-m3"].protocol == pc.PROTOCOL_MESSAGES
        assert by_id["future-model"].runnable is False
        assert by_id["future-model"].unavailable_reason

    def test_refresh_normalizes_and_resolves_protocols(
        self, fake_commandcode_endpoint
    ) -> None:
        with fake_commandcode_endpoint(
            lambda request: (
                200,
                catalog_payload(
                    [
                        "claude-sonnet-5",
                        "deepseek/deepseek-v4-flash",
                        "zai-org/glm-5.2",
                    ]
                ),
            )
        ) as server:
            snapshot = pc.refresh_provider_catalog(
                "commandcode_goat", engine="stdlib", credential=SECRET
            )
        ids = [m.model_id for m in snapshot.models]
        assert ids == sorted(ids, key=lambda item: (item.lower(), item))
        assert "deepseek/deepseek-v4-flash" in ids
        by_id = {m.model_id: m for m in snapshot.models}
        assert (
            by_id["deepseek/deepseek-v4-flash"].protocol
            == pc.PROTOCOL_CHAT_COMPLETIONS
        )
        assert by_id["claude-sonnet-5"].protocol == pc.PROTOCOL_MESSAGES
        assert snapshot.source == "live"

    def test_refresh_hits_the_contract_catalog_path(
        self, fake_commandcode_endpoint
    ) -> None:
        with fake_commandcode_endpoint(
            lambda request: (200, catalog_payload(["deepseek/deepseek-v4-flash"]))
        ) as server:
            pc.refresh_provider_catalog(
                "commandcode_goat", engine="stdlib", credential=SECRET
            )
            assert server.requests[0]["method"] == "GET"
            assert server.requests[0]["path"] == "/models"
            assert server.requests[0]["authorization"] == f"Bearer {SECRET}"

    def test_refresh_dedupes_and_ignores_invalid_entries(
        self, fake_commandcode_endpoint
    ) -> None:
        entries = [
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-flash",
            "",
            "x" * 500,
        ]
        with fake_commandcode_endpoint(
            lambda request: (200, catalog_payload(entries))
        ) as server:
            snapshot = pc.refresh_provider_catalog(
                "commandcode_goat", engine="stdlib", credential=SECRET
            )
        ids = [m.model_id for m in snapshot.models]
        assert ids.count("deepseek/deepseek-v4-flash") == 1
        assert all(type(item) is str and len(item) <= 128 for item in ids)

    def test_refresh_bounds_catalog_size(self, fake_commandcode_endpoint) -> None:
        ids = [f"model-{index:03d}" for index in range(300)]
        with fake_commandcode_endpoint(
            lambda request: (200, catalog_payload(ids))
        ) as server:
            snapshot = pc.refresh_provider_catalog(
                "commandcode_goat", engine="stdlib", credential=SECRET
            )
        assert len(snapshot.models) == pc._MAX_CATALOG_MODELS
        assert snapshot.truncated is True

    def test_refresh_requires_credential(
        self, fake_commandcode_endpoint, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CMD_API_KEY", raising=False)
        with fake_commandcode_endpoint(
            lambda request: (200, catalog_payload(["deepseek/deepseek-v4-flash"]))
        ):
            with pytest.raises(pc.ProviderConnectionError) as excinfo:
                pc.refresh_provider_catalog("commandcode_goat", engine="stdlib")
        assert "CMD_API_KEY" in str(excinfo.value)

    def test_refresh_failure_is_bounded_and_sanitized(
        self, fake_commandcode_endpoint
    ) -> None:
        with fake_commandcode_endpoint(
            lambda request: (500, {"error": "backend down"})
        ):
            with pytest.raises(pc.ProviderConnectionError) as excinfo:
                pc.refresh_provider_catalog(
                    "commandcode_goat", engine="stdlib", credential=SECRET
                )
        message = str(excinfo.value)
        assert "CommandCode GOAT catalog refresh failed" in message
        assert SECRET not in message
        assert len(message) <= 400

    def test_failed_refresh_never_fabricates_empty_catalog(
        self, fake_commandcode_endpoint
    ) -> None:
        with fake_commandcode_endpoint(
            lambda request: (500, {"error": "backend down"})
        ):
            with pytest.raises(pc.ProviderConnectionError):
                pc.refresh_provider_catalog(
                    "commandcode_goat", engine="stdlib", credential=SECRET
                )
        assert pc.load_cached_catalog("commandcode_goat") is None

    def test_refresh_failure_keeps_previous_cache(
        self, fake_commandcode_endpoint
    ) -> None:
        with fake_commandcode_endpoint(
            lambda request: (200, catalog_payload(["deepseek/deepseek-v4-flash"]))
        ):
            pc.refresh_provider_catalog(
                "commandcode_goat", engine="stdlib", credential=SECRET
            )
        with fake_commandcode_endpoint(
            lambda request: (503, {"error": "down"})
        ):
            with pytest.raises(pc.ProviderConnectionError):
                pc.refresh_provider_catalog(
                    "commandcode_goat", engine="stdlib", credential=SECRET
                )
        snapshot = pc.load_cached_catalog("commandcode_goat")
        assert snapshot is not None
        assert [m.model_id for m in snapshot.models] == [
            "deepseek/deepseek-v4-flash"
        ]

    def test_refresh_empty_catalog_fails(self, fake_commandcode_endpoint) -> None:
        with fake_commandcode_endpoint(lambda request: (200, catalog_payload([]))):
            with pytest.raises(pc.ProviderConnectionError):
                pc.refresh_provider_catalog(
                    "commandcode_goat", engine="stdlib", credential=SECRET
                )

    def test_refresh_performs_no_generation_inference(
        self, fake_commandcode_endpoint
    ) -> None:
        """Catalog refresh is one GET; no inference endpoint is contacted."""

        with fake_commandcode_endpoint(
            lambda request: (200, catalog_payload(["deepseek/deepseek-v4-flash"]))
        ) as server:
            pc.refresh_provider_catalog(
                "commandcode_goat", engine="stdlib", credential=SECRET
            )
            assert len(server.requests) == 1
            assert server.requests[0]["method"] == "GET"


# -- cache --------------------------------------------------------------------------


class TestCatalogCache:
    def test_cache_round_trip(self, fake_commandcode_endpoint) -> None:
        with fake_commandcode_endpoint(
            lambda request: (
                200,
                catalog_payload(["deepseek/deepseek-v4-flash", "claude-sonnet-5"]),
            )
        ):
            snapshot = pc.refresh_provider_catalog(
                "commandcode_goat", engine="stdlib", credential=SECRET
            )
        loaded = pc.load_cached_catalog("commandcode_goat")
        assert loaded is not None
        assert [m.model_id for m in loaded.models] == [
            m.model_id for m in snapshot.models
        ]
        assert [m.protocol for m in loaded.models] == [
            m.protocol for m in snapshot.models
        ]
        assert loaded.fetched_at_utc == snapshot.fetched_at_utc

    def test_cache_never_stores_credentials(self, fake_commandcode_endpoint) -> None:
        with fake_commandcode_endpoint(
            lambda request: (200, catalog_payload(["deepseek/deepseek-v4-flash"]))
        ):
            pc.refresh_provider_catalog(
                "commandcode_goat", engine="stdlib", credential=SECRET
            )
        raw = pc.catalog_cache_path().read_text(encoding="utf-8")
        assert SECRET not in raw
        assert "authorization" not in raw.lower()

    def test_malformed_cache_fails_closed(self, tmp_path: Path) -> None:
        pc.catalog_cache_path().write_text("{broken", encoding="utf-8")
        assert pc.load_cached_catalog("opencode_go") is None

    def test_wrong_schema_cache_fails_closed(self) -> None:
        pc.catalog_cache_path().write_text(
            json.dumps({"schema_version": "provider-catalog-cache-v0", "providers": {}}),
            encoding="utf-8",
        )
        assert pc.load_cached_catalog("opencode_go") is None

    def test_naive_timestamp_cache_fails_closed(self) -> None:
        pc.catalog_cache_path().write_text(
            json.dumps(
                {
                    "schema_version": pc._CACHE_SCHEMA_VERSION,
                    "providers": {
                        "commandcode_goat": {
                            "kind": "commandcode_goat",
                            "fetched_at_utc": "2026-08-31T10:00:00",
                            "source": "live",
                            "truncated": False,
                            "models": [],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        assert pc.load_cached_catalog("commandcode_goat") is None

    def test_save_drops_unknown_cache_content(self, fake_commandcode_endpoint) -> None:
        path = pc.catalog_cache_path()
        path.write_text(
            json.dumps(
                {
                    "schema_version": pc._CACHE_SCHEMA_VERSION,
                    "providers": {
                        "unknown": {"api_key": SECRET},
                        "opencode_go": {"authorization": f"Bearer {SECRET}"},
                    },
                }
            ),
            encoding="utf-8",
        )
        with fake_commandcode_endpoint(
            lambda request: (200, catalog_payload(["deepseek/deepseek-v4-flash"]))
        ):
            pc.refresh_provider_catalog(
                "commandcode_goat", engine="stdlib", credential=SECRET
            )
        raw = path.read_text(encoding="utf-8")
        assert SECRET not in raw
        assert "authorization" not in raw.lower()
        assert "unknown" not in json.loads(raw)["providers"]

    def test_oversized_cache_fails_closed(self) -> None:
        pc.catalog_cache_path().write_text("z" * (pc._MAX_CACHE_FILE_BYTES + 1), encoding="utf-8")
        assert pc.load_cached_catalog("opencode_go") is None

    def test_missing_cache_is_absent_not_error(self) -> None:
        assert pc.load_cached_catalog("opencode_go") is None

    def test_stale_protocol_cache_entry_discarded(self) -> None:
        """A cached protocol must agree with the current resolver."""

        snapshot = pc.ProviderCatalogSnapshot(
            kind="commandcode_goat",
            fetched_at_utc="2026-08-30T00:00:00Z",
            source="live",
            models=(
                pc.DiscoveredProviderModel(
                    kind="commandcode_goat",
                    model_id="deepseek/deepseek-v4-flash",
                    display_name="DeepSeek V4 Flash",
                    protocol=pc.PROTOCOL_MESSAGES,  # contradicts the resolver
                    runnable=True,
                ),
            ),
        )
        pc.save_cached_catalog(snapshot)
        assert pc.load_cached_catalog("commandcode_goat") is None

    def test_invalid_cache_model_id_discarded(self) -> None:
        snapshot = pc.ProviderCatalogSnapshot(
            kind="opencode_go",
            fetched_at_utc="2026-08-30T00:00:00Z",
            source="live",
            models=(
                pc.DiscoveredProviderModel(
                    kind="opencode_go",
                    model_id="INVALID ID WITH SPACES",
                    display_name="X",
                    protocol=None,
                    runnable=False,
                ),
            ),
        )
        pc.save_cached_catalog(snapshot)
        assert pc.load_cached_catalog("opencode_go") is None


# -- connection status ---------------------------------------------------------------


class TestConnectionStatus:
    def test_not_connected_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CMD_API_KEY", raising=False)
        status = pc.provider_connection_status("commandcode_goat")
        assert status.connected is False
        assert status.credential_source is None
        assert status.model_count == 0
        assert status.status_message and "CMD_API_KEY" in status.status_message

    def test_connected_with_session_key(self) -> None:
        pc.set_session_key("commandcode_goat", SECRET)
        status = pc.provider_connection_status("commandcode_goat")
        assert status.connected is True
        assert status.credential_source == "session_key"

    def test_model_count_after_refresh(self, fake_commandcode_endpoint) -> None:
        with fake_commandcode_endpoint(
            lambda request: (
                200,
                catalog_payload(["deepseek/deepseek-v4-flash", "claude-sonnet-5"]),
            )
        ):
            pc.refresh_provider_catalog(
                "commandcode_goat", engine="stdlib", credential=SECRET
            )
        status = pc.provider_connection_status("commandcode_goat")
        assert status.model_count == 2
        assert status.last_refresh_utc is not None
        assert status.last_refresh_source == "live"

    def test_status_covers_both_builtins(self) -> None:
        statuses = pc.connection_statuses()
        assert [s.kind for s in statuses] == list(pc.DIRECT_API_PROVIDER_KINDS)

    def test_endpoint_identity_is_safe(self) -> None:
        status = pc.provider_connection_status("opencode_go")
        assert status.base_url == "https://opencode.ai/zen/go/v1/models"
