"""Acceptance integration test suite for Level-32 Configured Runtime (Task 46/47).

Validates the complete execution pipeline for Level-32 (Cookiecutter #967) launched
with a configured command model (SourceKind.CONFIGURED_MODEL):
- StartSession -> MODEL_CONFIGURED -> Level-32 workspace materialization ->
  real run_local_session -> real DeterministicController -> real CancellableJsonlCommandTransport ->
  MODEL_REQUEST_STARTED emitted -> model directives dispatched -> honest completion/failure.
- Ensures no monkeypatching of run_local_session, _curated_fixture_dir, or controller.run.
- Proves real Level-32 workspace files exist under work_dir / level32_staging.
- Proves provider and model identity survive into journal provenance.
- Proves interactive Level-32 enforces pdb-on-uncertainty with proof_required=False and
  require_pdb_evidence_before_patch=False (F2).
- Proves materialization works when pyarrow is unavailable (F1).
- Proves the materialization failure path emits DIAGNOSIS_RECORDED and raises ConfiguredSourceError.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentic_debugger.agent.controller import DeterministicController
from agentic_debugger.application.configured_source import (
    ConfiguredSourceError,
    run_configured_session,
)
from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.events import SessionEventKind, SourceKind
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.application.level32 import LEVEL32_TASK_ID
from agentic_debugger.application.level32_materialization import (
    LEVEL32_INTERNAL_TASK_ID,
    LEVEL32_PUBLIC_F2P,
    LEVEL32_PUBLIC_P2P,
    Level32MaterializationError,
    SourceAcquisitionMode,
    build_level32_interactive_scenario,
    build_level32_official_scenario,
    default_base_source_cache_dir,
    materialize_level32_task,
)
from agentic_debugger.application.model_gateway import (
    ModelGateway,
    provider_runtime_identity,
)
from agentic_debugger.application.model_providers import ProviderModel
from agentic_debugger.application import provider_connections as pc
from agentic_debugger.application.sources import ModelExecutionError
from agentic_debugger.application.worker_scenarios import ScenarioContext
from agentic_debugger.cancellation import CancellationToken
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import LiveModelAdapter, LiveModelConfig
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import StartSessionScreen
from ui_support import run_headless


def _setup_test_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path / "config-dir"))
    _secure_store: dict[str, str] = {}
    monkeypatch.setattr(
        pc, "save_secure_credential", lambda k, v: _secure_store.setdefault(k, v) or True
    )
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: _secure_store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in _secure_store)
    monkeypatch.setattr(
        pc,
        "delete_secure_credential",
        lambda k: bool(_secure_store.pop(k, None) is not None),
    )
    monkeypatch.setattr(
        pc, "provider_quarantine_path", lambda: tmp_path / "quarantine.json"
    )
    pc._QUARANTINED_PROVIDERS.clear()
    pc.clear_all_session_keys()
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
    )
    pc.set_session_key("commandcode_goat", "sk-live-test-level32-synthetic-key")


def test_level32_ui_start_session_selects_and_launches_configured_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner scenario: launch Level 32 with a configured command model through UI StartSession."""
    _setup_test_providers(tmp_path, monkeypatch)
    app = LocalApplicationV1(history_store=HistoryStore(tmp_path / "history"))
    models = (
        ProviderModel(
            "commandcode_goat",
            "muse/muse-spark-1.3",
            "Muse Spark 1.3 Contributor",
            "CommandCode GOAT",
            True,
        ),
    )
    monkeypatch.setattr(
        "agentic_debugger.ui.screens.list_provider_models", lambda **_kwargs: models
    )
    start_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(app, "start_live_session", lambda **kw: start_calls.append(kw))

    monkeypatch.setattr(
        ModelGateway.default(),
        "get_provider_status",
        lambda p: SimpleNamespace(is_configured=True),
    )

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)

        # Select Capability Ladder -> Level 32
        start._choice_selected("target", "ladder")
        start._choice_selected("task", LEVEL32_TASK_ID)

        # Select Configured Model: Muse Spark 1.3 Contributor
        start._choice_selected("model", "commandcode_goat:muse/muse-spark-1.3")
        assert start.start_available is True
        start.action_start()

    run_headless(app, scenario, size=(120, 32))

    assert len(start_calls) == 1
    call = start_calls[0]
    assert call["task_id"] == LEVEL32_TASK_ID
    assert call["source_kind"] is SourceKind.CONFIGURED_MODEL
    assert call["model_provider"] == "commandcode_goat"
    assert call["profile_id"] == "muse/muse-spark-1.3"


def test_level32_configured_runtime_end_to_end_materialization_and_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Execute Level-32 configured session without monkeypatching run_local_session.

    Proves:
    1. Level-32 workspace is truthfully materialized under work_dir / level32_staging.
    2. Real run_local_session runs with materialized fixture_dir and scenario.
    3. Real DeterministicController runs with real CancellableJsonlCommandTransport.
    4. MODEL_CONFIGURED and MODEL_REQUEST_STARTED events are recorded in journal.
    5. Real child model script receives request on stdin and executes.
    6. Interactive Level-32 scenario has exact_public_reproduction=False.
    7. Adapter proof_required=False, controller require_pdb_evidence_before_patch=False,
       and policy=pdb-on-uncertainty (F2).
    """
    _setup_test_providers(tmp_path, monkeypatch)

    # Use explicit temporary cache seam rather than implicit global cache
    explicit_cache = tmp_path / "explicit_cache" / "cookiecutter-967-base-source"
    shutil.copytree(default_base_source_cache_dir(), explicit_cache)
    monkeypatch.setattr(
        "agentic_debugger.application.level32_materialization.default_base_source_cache_dir",
        lambda: explicit_cache,
    )

    # Spy on adapter and controller construction to verify F2 invariants
    captured_adapter: dict[str, Any] = {}
    orig_adapter_init = LiveModelAdapter.__init__

    def spy_adapter_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_adapter_init(self, *args, **kwargs)
        captured_adapter["proof_required"] = self.proof_required
        captured_adapter["policy"] = self.policy

    monkeypatch.setattr(LiveModelAdapter, "__init__", spy_adapter_init)

    captured_controller_configs: list[Any] = []
    orig_controller_init = DeterministicController.__init__

    def spy_controller_init(
        self: Any, registry: Any, model: Any, config: Any, **kwargs: Any
    ) -> None:
        captured_controller_configs.append(config)
        orig_controller_init(self, registry, model, config, **kwargs)

    monkeypatch.setattr(DeterministicController, "__init__", spy_controller_init)

    # Create a deterministic fake model subprocess script
    fake_model_path = tmp_path / "fake_model_runner.py"
    marker_path = tmp_path / "model_invoked_marker.txt"
    fake_model_path.write_text(
        f"""from __future__ import annotations
import json
import sys
from pathlib import Path

marker = Path({repr(str(marker_path))})
line = sys.stdin.readline()
if not line:
    sys.exit(0)

# Record invocation marker proving real process execution
count = int(marker.read_text()) if marker.exists() else 0
marker.write_text(str(count + 1))

# Step 1: run reproduction; Step 2: terminate cleanly
if count == 0:
    content = json.dumps({{"kind": "action", "name": "run_reproduction", "arguments": {{"phase": "baseline"}}}})
else:
    content = json.dumps({{"kind": "transition", "state": "FAILED"}})

response = {{
    "provider_completion_schema_version": "provider-completion-v1",
    "directive_content": content,
    "usage": {{"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}},
}}
print(json.dumps(response))
sys.stdout.flush()
""",
        encoding="utf-8",
    )

    # Bind provider resolution to the fake model command
    import agentic_debugger.application.configured_source as cs

    c = pc.get_provider_config("commandcode_goat")
    authority = provider_runtime_identity(c)

    def wrapped_resolve(provider: str, model_id: str, **kwargs: Any):
        cfg = LiveModelConfig(
            model_name="muse-spark-1.3",
            command=(sys.executable, str(fake_model_path)),
            request_timeout_seconds=30,
            tool_version="test-v1",
        )
        return (
            cfg,
            {
                "display_name": "Muse Spark 1.3 Contributor",
                "route": "direct_api",
                "api_protocol": "chat_completions",
                "provider_model_id": model_id,
                "endpoint": "https://api.commandcode.ai/provider/v1",
                "provider_runtime_identity": authority,
                "protocol_version": "1.3",
            },
            "f" * 64,
        )

    monkeypatch.setattr(cs, "_resolve_registry_model", wrapped_resolve)

    journal_path = tmp_path / "journal.events.jsonl"
    journal = SessionEventJournal(
        journal_path,
        session_id="sess-l32-acceptance",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    emitter = SessionEventEmitter(
        sink=journal,
        session_id="sess-l32-acceptance",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    ctx = ScenarioContext(
        work_dir=work_dir,
        emitter=emitter,
        token=CancellationToken(),
    )

    # Execute configured session through real local execution pipeline
    with pytest.raises(ModelExecutionError, match="controller run ended without completion"):
        run_configured_session(
            ctx,
            {
                "provider": "commandcode_goat",
                "model_id": "muse/muse-spark-1.3",
                "policy": "pdb-on-uncertainty",
            },
        )

    # 1. Verify Level-32 workspace was materialized under work_dir / level32_staging
    staging_fixture = (
        work_dir
        / "level32_staging"
        / "agentic_debugger"
        / "datasets"
        / "curated"
        / LEVEL32_INTERNAL_TASK_ID
    )
    assert staging_fixture.is_dir()
    assert (staging_fixture / "cookiecutter" / "config.py").is_file()
    assert (staging_fixture / "tests" / "test_pdb_public_config_merge.py").is_file()
    assert (staging_fixture / "poyo.py").is_file()
    assert (staging_fixture / "task.json").is_file()

    # 2. Verify fake model runner was actually executed
    assert marker_path.is_file()
    assert int(marker_path.read_text()) >= 1

    # 3. Verify authoritative journal records
    events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    event_kinds = [e["event_kind"] for e in events]

    assert SessionEventKind.MODEL_CONFIGURED.value in event_kinds
    assert SessionEventKind.MODEL_REQUEST_STARTED.value in event_kinds
    assert SessionEventKind.MODEL_REQUEST_COMPLETED.value in event_kinds
    assert SessionEventKind.MODEL_DIRECTIVE_ACCEPTED.value in event_kinds

    configured_events = [e for e in events if e["event_kind"] == SessionEventKind.MODEL_CONFIGURED.value]
    assert len(configured_events) == 1
    cfg_payload = configured_events[0]["payload"]
    assert cfg_payload["provider"] == "commandcode_goat"
    assert cfg_payload["profile_id"] == "muse/muse-spark-1.3"
    assert cfg_payload["display_name"] == "Muse Spark 1.3 Contributor"

    # 4. Mandatory F2 regression: interactive route proof invariants
    assert captured_adapter.get("proof_required") is False
    assert captured_adapter.get("policy") == DemoPolicy.PDB_ON_UNCERTAINTY
    assert len(captured_controller_configs) >= 1
    assert captured_controller_configs[0].require_pdb_evidence_before_patch is False


def test_level32_materialization_failure_emits_diagnosis_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Level-32 workspace materialization fails, emit DIAGNOSIS_RECORDED and fail honestly."""
    _setup_test_providers(tmp_path, monkeypatch)
    import agentic_debugger.application.configured_source as cs

    def fail_materialization(_staging_root: Path, **_kwargs: Any) -> Path:
        raise Level32MaterializationError("simulated Docker export failure")

    monkeypatch.setattr(cs, "materialize_level32_task", fail_materialization)

    c = pc.get_provider_config("commandcode_goat")
    authority = provider_runtime_identity(c)

    def wrapped_resolve(provider: str, model_id: str, **kwargs: Any):
        cfg = LiveModelConfig(
            model_name="muse-spark-1.3",
            command=("echo", "hi"),
            request_timeout_seconds=30,
            tool_version="test-v1",
        )
        return (
            cfg,
            {
                "display_name": "Muse Spark 1.3 Contributor",
                "route": "direct_api",
                "api_protocol": "chat_completions",
                "provider_model_id": model_id,
                "endpoint": "https://api.commandcode.ai/provider/v1",
                "provider_runtime_identity": authority,
            },
            "e" * 64,
        )

    monkeypatch.setattr(cs, "_resolve_registry_model", wrapped_resolve)

    journal_path = tmp_path / "journal_fail.events.jsonl"
    journal = SessionEventJournal(
        journal_path,
        session_id="sess-l32-fail",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    emitter = SessionEventEmitter(
        sink=journal,
        session_id="sess-l32-fail",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    work_dir = tmp_path / "work_fail"
    work_dir.mkdir(parents=True, exist_ok=True)
    ctx = ScenarioContext(
        work_dir=work_dir,
        emitter=emitter,
        token=CancellationToken(),
    )

    with pytest.raises(ConfiguredSourceError, match="Level-32 workspace preparation failed"):
        run_configured_session(
            ctx,
            {
                "provider": "commandcode_goat",
                "model_id": "muse/muse-spark-1.3",
                "policy": "pdb-on-uncertainty",
            },
        )

    events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    diagnosis_events = [e for e in events if e["event_kind"] == SessionEventKind.DIAGNOSIS_RECORDED.value]
    assert len(diagnosis_events) == 1
    diag = diagnosis_events[0]["payload"]
    assert "Level-32 workspace preparation failed: simulated Docker export failure" in diag["text"]
    assert diag["confidence"] == "observed"


def test_interactive_level32_materialization_without_pyarrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive Level-32 workspace materialization succeeds when pyarrow is unavailable (F1)."""
    # Force pyarrow to be unimportable
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)

    explicit_cache = tmp_path / "cache" / "cookiecutter-967-base-source"
    shutil.copytree(default_base_source_cache_dir(), explicit_cache)

    staging_root = tmp_path / "staging"
    fixture = materialize_level32_task(
        staging_root,
        mode=SourceAcquisitionMode.INTERACTIVE_CACHE_FIRST,
        cache_dir=explicit_cache,
    )

    assert fixture.is_dir()
    assert (fixture / "cookiecutter" / "config.py").is_file()
    assert (fixture / "tests" / "test_pdb_public_config_merge.py").is_file()
    assert (fixture / "poyo.py").is_file()
    assert (fixture / "task.json").is_file()

    task_data = json.loads((fixture / "task.json").read_text(encoding="utf-8"))
    assert task_data["task_id"] == LEVEL32_INTERNAL_TASK_ID
    assert task_data["tests"]["fail_to_pass"] == [LEVEL32_PUBLIC_F2P]
    assert task_data["tests"]["pass_to_pass"] == [LEVEL32_PUBLIC_P2P]


def test_official_versus_interactive_scenario_proof_contracts() -> None:
    """Prove official scenario preserves exact PDB proof while interactive route does not (F2)."""
    official = build_level32_official_scenario()
    interactive = build_level32_interactive_scenario()

    assert official.runtime_probe.exact_public_reproduction is True
    assert official.runtime_probe.call_source == "get_config('unused-public-driver-path')"

    assert interactive.runtime_probe.exact_public_reproduction is False
    assert interactive.runtime_probe.call_source == "get_config('tests/test-config/valid-config.yaml')"
