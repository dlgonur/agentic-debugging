"""Saved-state healing regression: stale OpenCode Go catalog must self-heal.

Recreates the actual failure class observed on the repaired product:

- existing saved OpenCode Go provider;
- previously discovered model records with stored/derived protocol
  missing (None) because the route table did not know the ids at
  discovery time;
- current provider_runtime mapping knows the exact models.

Expected (offline, no live inference, no provider recreation, no
catalog deletion):

- Model Browser projection derives from CURRENT runtime truth
  (proven via protocol badges and transport resolution; picker rows
  stay name + provider only with no routing text);
- transport resolution (ModelGateway.resolve / LiveModelConfig) uses the
  same current truth;
- unknown models remain Unresolved / disabled / fail-closed;
- the stale durable records are upgraded IN-MEMORY at projection time;
  the config/cache files are NOT destructively rewritten by a read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.application import provider_connections as pc
from agentic_debugger.application.model_gateway import ModelGateway
from agentic_debugger.application.model_providers import list_provider_models
from agentic_debugger.application.provider_catalog import (
    ProviderCatalogSnapshot,
    catalog_cache_path,
    load_cached_catalog,
)
from agentic_debugger.application.provider_config import (
    DiscoveredProviderModel,
    provider_configurations_path,
)
from agentic_debugger.application.provider_management import update_provider_config
from agentic_debugger.ui.model_browser import (
    details_for_option,
    is_selectable,
    protocol_badge,
    render_model_row,
)
from agentic_debugger.ui.session_config import ModelOption

SECRET = "stale-healing-test-credential-not-real"

PID = "opencode_go"
BASE_URL = "https://opencode.ai/zen/go/v1"

STALE_IDS = (
    "muse-spark-1.3-contributor",
    "deepseek-v4.1-flash",
    "qwen3.8-max",
)
UNKNOWN_ID = "future-unknown-zzz-9"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(tmp_path / "c.json")
    )
    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_CATALOG_CACHE_PATH",
        str(tmp_path / "cache.json"),
    )
    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_QUARANTINE_PATH", str(tmp_path / "q.json")
    )
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "oc-home"))
    monkeypatch.setenv("AGENTIC_DEBUGGER_DISABLE_SECURE_STORE", "1")
    for var in (
        "OPENCODE_API_KEY",
        "COMMAND_CODE_API_KEY",
        "OLLAMA_API_KEY",
        "AGENTIC_DEBUGGER_OPENCODE_GO_API_KEY",
        "AGENTIC_DEBUGGER_COMMANDCODE_GOAT_API_KEY",
        "AGENTIC_DEBUGGER_MODEL_SESSION_ID",
        "AGENTIC_DEBUGGER_OPENCODE_SESSION_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    pc.clear_all_session_keys()
    ModelGateway._reset_default_instance()
    yield
    pc.clear_all_session_keys()
    ModelGateway._reset_default_instance()


def _stale_record(model_id: str) -> DiscoveredProviderModel:
    # Simulates a record persisted BEFORE the route table knew this id:
    # derived protocol missing (None).  Constructed directly to bypass
    # current-table resolution in DiscoveredProviderModel.create.
    from agentic_debugger.application.model_providers import (
        format_model_display_name,
    )

    return DiscoveredProviderModel(
        kind=PID,
        model_id=model_id,
        display_name=format_model_display_name(model_id),
        protocol=None,
        runnable=False,
        unavailable_reason="Protocol not yet resolved for direct API",
    )


def _install_stale_saved_state() -> None:
    # Existing saved provider (no credential yet; session key below makes
    # the direct route runnable offline).
    pc.add_provider_config(
        name="OpenCode Go",
        base_url=BASE_URL,
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id=PID,
        transport_profile=pc.TRANSPORT_OPENCODE_GO,
    )
    stale_models = tuple(
        sorted(
            (_stale_record(mid) for mid in (*STALE_IDS, UNKNOWN_ID)),
            key=lambda m: (m.model_id.lower(), m.model_id),
        )
    )
    update_provider_config(PID, models=stale_models)
    snapshot = ProviderCatalogSnapshot(
        kind=PID,
        fetched_at_utc="2026-09-01T00:00:00Z",
        source="live",
        models=stale_models,
        truncated=False,
    )
    from agentic_debugger.application.provider_catalog import save_cached_catalog

    save_cached_catalog(snapshot)
    # Direct-route credential for readiness (session key only; offline).
    pc.set_session_key(PID, SECRET)


def _provider_model_map():
    return {
        m.model_id: m
        for m in list_provider_models()
        if m.kind == PID
    }


class TestStaleCatalogHealsWithoutRecreation:
    def test_catalog_load_heals_in_memory(self) -> None:
        _install_stale_saved_state()
        snapshot = load_cached_catalog(PID)
        assert snapshot is not None, (
            "stale cache must heal on load instead of discarding the snapshot"
        )
        healed = {m.model_id: m.protocol for m in snapshot.models}
        assert healed["muse-spark-1.3-contributor"] == "responses"
        assert healed["deepseek-v4.1-flash"] == "chat_completions"
        assert healed["qwen3.8-max"] == "messages"
        assert healed[UNKNOWN_ID] is None

    def test_browser_projection_heals_to_ready(self) -> None:
        _install_stale_saved_state()
        by_id = _provider_model_map()
        # muse-spark-1.3 -> Responses / Ready / selectable.
        spark = by_id["muse-spark-1.3-contributor"]
        assert spark.protocol == "responses"
        assert spark.available is True
        assert spark.unavailable_reason is None
        assert "responses" in (spark.note or "")
        # deepseek-v4.1-flash -> Chat Completions / Ready.
        flash = by_id["deepseek-v4.1-flash"]
        assert flash.protocol == "chat_completions"
        assert flash.available is True
        # qwen3.8-max -> Messages / Ready.
        qwen = by_id["qwen3.8-max"]
        assert qwen.protocol == "messages"
        assert qwen.available is True
        # Unknown remains Unresolved / disabled / fail-closed.
        unknown = by_id[UNKNOWN_ID]
        assert unknown.protocol is None
        assert unknown.available is False
        assert unknown.unavailable_reason is not None

    def test_browser_helpers_render_healed_state(self) -> None:
        _install_stale_saved_state()
        by_id = _provider_model_map()

        def _as_option(pm) -> ModelOption:
            return ModelOption(
                pm.kind,
                pm.model_id,
                pm.display_name,
                detail=pm.note or "",
                available=pm.available,
                unavailable_reason=pm.unavailable_reason,
                protocol=pm.protocol,
                provider_label=pm.provider_label,
            )

        spark = _as_option(by_id["muse-spark-1.3-contributor"])
        assert protocol_badge(spark.protocol) == "Responses"
        assert is_selectable(spark) is True
        # Quiet hierarchy: healed ready rows show name + provider only;
        # details stay empty.  Routing truth is proven via protocol_badge
        # and the transport tests below, not via row copy.
        spark_row = str(render_model_row(spark, 100))
        assert "Muse Spark" in spark_row or "muse-spark" in spark_row.lower()
        assert "Responses" not in spark_row
        assert details_for_option(spark) == ""

        flash = _as_option(by_id["deepseek-v4.1-flash"])
        assert protocol_badge(flash.protocol) == "Chat Completions"
        assert is_selectable(flash) is True

        qwen = _as_option(by_id["qwen3.8-max"])
        assert protocol_badge(qwen.protocol) == "Messages"
        assert is_selectable(qwen) is True

        unknown = _as_option(by_id[UNKNOWN_ID])
        assert protocol_badge(unknown.protocol) == "Unresolved"
        assert is_selectable(unknown) is False
        # Presentation-only: the picker shows no routing text, so the
        # unknown row carries name + provider only while staying
        # disabled/muted; routing truth is proven via protocol_badge
        # and the transport tests below, not via row copy.
        unknown_row = str(render_model_row(unknown, 100))
        assert "Route needed" not in unknown_row
        assert "Route unresolved" not in unknown_row
        assert "Unresolved" not in unknown_row
        assert "!" not in unknown_row
        assert details_for_option(unknown) == ""

    def test_transport_resolves_from_current_truth(self) -> None:
        _install_stale_saved_state()
        gateway = ModelGateway.default()
        spark = gateway.resolve(PID, "muse-spark-1.3-contributor")
        assert spark.effective_protocol == "responses"
        assert spark.route == "direct_api"
        flash = gateway.resolve(PID, "deepseek-v4.1-flash")
        assert flash.effective_protocol == "chat_completions"
        qwen = gateway.resolve(PID, "qwen3.8-max")
        assert qwen.effective_protocol == "messages"
        # Same truth through the LiveModelConfig path (no HTTP executed).
        from agentic_debugger.application.model_providers import (
            resolve_provider_live_config,
        )

        _, prov = resolve_provider_live_config(PID, "muse-spark-1.3-contributor")
        assert prov.get("api_protocol") == "responses"
        # Unknown stays fail-closed at transport selection.
        with pytest.raises(Exception):
            gateway.resolve(PID, UNKNOWN_ID)

    def test_healing_is_nondestructive(self) -> None:
        _install_stale_saved_state()
        config_path = provider_configurations_path()
        cache_path = catalog_cache_path()
        before_config = config_path.read_bytes()
        before_cache = cache_path.read_bytes()
        # Exercise every read/projection path (must not rewrite durables).
        assert load_cached_catalog(PID) is not None
        assert _provider_model_map()["muse-spark-1.3-contributor"].available is True
        gateway = ModelGateway.default()
        assert (
            gateway.resolve(PID, "muse-spark-1.3-contributor").effective_protocol
            == "responses"
        )
        assert config_path.read_bytes() == before_config, (
            "projection must not destructively rewrite provider configuration"
        )
        assert cache_path.read_bytes() == before_cache, (
            "projection must not destructively rewrite the catalog cache"
        )
        # Durable records still carry the stale derived value; truth is
        # derived at read time from the current profile mapping.
        stored_cfg = pc.get_provider_config(PID)
        assert stored_cfg is not None
        stored = {m.model_id: m.protocol for m in stored_cfg.models}
        assert stored["muse-spark-1.3-contributor"] is None
        assert stored["deepseek-v4.1-flash"] is None
        raw_cache = json.loads(cache_path.read_text(encoding="utf-8"))
        raw_models = {
            m["model_id"]: m["protocol"]
            for m in raw_cache["providers"][PID]["models"]
        }
        assert raw_models["muse-spark-1.3-contributor"] is None
        # No provider recreation and no catalog deletion were required.
        assert stored_cfg.provider_id == PID
        assert load_cached_catalog(PID) is not None

    def test_explicit_override_still_wins(self) -> None:
        _install_stale_saved_state()
        # muse-spark-1.3 is documented as responses; an explicit manual
        # override to chat_completions must keep winning over the table
        # (precedence 1 before 2), including through the healed projection.
        pc.add_manual_model(PID, "muse-spark-1.3-contributor", protocol=pc.PROTOCOL_CHAT_COMPLETIONS)
        assert pc.resolve_model_protocol(PID, "muse-spark-1.3-contributor") == pc.PROTOCOL_CHAT_COMPLETIONS
        by_id = _provider_model_map()
        assert by_id["muse-spark-1.3-contributor"].protocol == "chat_completions"
