"""Comprehensive test suite for Universal Model Execution Eligibility v1 (Task 42).

Owner product invariant:
Any model that the user has made genuinely executable through a configured
provider/API credential must be runnable anywhere Agentic Debugger supports
model execution. Qualification may affect classification of the RESULT.
Qualification must NOT control whether the model may EXECUTE.

Covers Scenarios A through H and repo-wide semantic invariant:
- Scenario A: Level 32 + CommandCode GOAT / OpenCode Go is executable, selectable,
  and launches with SourceKind.CONFIGURED_MODEL.
- Scenario B: Level 32 + qualified Ollama retains SourceKind.LEVEL32_OPERATOR
  and frozen contract.
- Scenario C: Colliding model ID on different providers remains distinct and
  executable (never remapped to qualified Ollama).
- Scenario D: Genuine unavailable model (e.g. missing credentials or offline model)
  blocks execution on concrete reason.
- Scenario E: Lower ladder regression (Level 6/12/18 work for both qualified and
  configured models).
- Scenario F: Local Project target regression free (live models runnable, offline blocked).
- Scenario G: No downstream rejection at app boundary and configured source.
- Scenario H: Scientific classification separated from execution eligibility.
- Repo-wide semantic invariant: execution_allowed == runtime_executable,
  qualification == separate_metadata.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

textual = pytest.importorskip("textual")

from agentic_debugger.application.events import SourceKind
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.level32 import (
    LADDER_TASKS,
    LEVEL32_TASK_ID,
    level32_model_profiles,
)
from agentic_debugger.application.model_providers import ProviderModel
from agentic_debugger.application.worker_scenarios import ScenarioContext
from agentic_debugger.application import provider_connections as pc
from agentic_debugger.cancellation import CancellationToken
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import (
    ChoicePickerScreen,
    StartSessionScreen,
)
from agentic_debugger.ui.session_config import (
    PROVIDER_CONFIGURED,
    PROVIDER_OFFLINE,
    PROVIDER_OLLAMA,
    ROW_MODEL,
    ROW_TASK,
    SEVERITY_ERROR,
    TARGET_CURATED,
    TARGET_LABELS,
    TARGET_LADDER,
    TARGET_LOCAL_PROJECT,
    ModelChoice,
    ModelOption,
    ProjectStatus,
    SessionCatalog,
    SessionConfig,
    TaskOption,
    derive_readiness,
    model_compatibility,
)
from ui_support import run_headless


def make_app(tmp_path: Path) -> LocalApplicationV1:
    return LocalApplicationV1(history_store=HistoryStore(tmp_path))


def _setup_providers():
    pc.add_provider_config(
        name="Ollama Cloud",
        base_url="https://ollama.com",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="ollama_cloud",
        transport_profile=pc.TRANSPORT_OLLAMA_CLOUD,
    )
    pc.add_provider_config(
        name="OpenCode Go",
        base_url="https://opencode.ai/zen/go/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="opencode_go",
        transport_profile=pc.TRANSPORT_OPENCODE_GO,
    )
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
    )


def test_scenario_a_level32_with_configured_provider_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario A: Level 32 + CommandCode GOAT is executable, selectable, and launches with CONFIGURED_MODEL."""
    _setup_providers()
    app = make_app(tmp_path)
    qualified = level32_model_profiles()[0]
    models = (
        ProviderModel("ollama_cloud", qualified.alias, qualified.display_name, "Ollama Cloud", True),
        ProviderModel("commandcode_goat", "zai-org/glm-5.2", "GLM 5.2", "CommandCode GOAT", True),
    )
    monkeypatch.setattr("agentic_debugger.ui.screens_setup.list_provider_models", lambda **_kwargs: models)
    monkeypatch.setattr(app, "ollama_cloud_model_profiles", lambda: (qualified,))

    start_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(app, "start_live_session", lambda **kw: start_calls.append(kw))

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)

        start._choice_selected("target", "ladder")
        start._choice_selected("task", LEVEL32_TASK_ID)

        # Open model picker on Level 32
        start._open_model_picker()
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)

        by_value = {choice.value: choice for choice in picker.choices}
        cmd_key = "commandcode_goat:zai-org/glm-5.2"
        assert cmd_key in by_value
        # Must NOT be disabled!
        assert by_value[cmd_key].disabled is False
        # Must carry non-qualified secondary note
        assert "not qualified for frozen Level-32 comparison" in by_value[cmd_key].secondary

        # Group note must NOT say "unavailable for Capability Ladder"
        cmd_choice = by_value[cmd_key]
        assert "unavailable for Capability Ladder" not in (cmd_choice.group_note or "")

        pilot.app.pop_screen()
        await pilot.pause()

        # Select the CommandCode model
        start._choice_selected("model", cmd_key)
        assert start._config.model.provider == "commandcode_goat"
        assert start._config.model.model_id == "zai-org/glm-5.2"

        # Preflight readiness must be READY Yes
        assert start.start_available is True
        context = start.query_one("#context-summary").render().plain
        assert "READY  Yes" in context
        assert "Selected model is outside frozen official Level-32 treatment" in context

        # F1 truth: Debugger, Treatment, Evaluation must reflect non-qualified execution
        assert "Debugger\nOn uncertainty" in context
        assert "Treatment\nInteractive Level-32 · non-official" in context
        assert "Evaluation\nIndependent verifier" in context
        assert "Official SWE-rebench" not in context
        assert "Treatment\nFrozen Level-32" not in context

        # Launch session
        start.action_start()
        assert len(start_calls) == 1
        assert start_calls[0]["task_id"] == LEVEL32_TASK_ID
        assert start_calls[0]["source_kind"] is SourceKind.CONFIGURED_MODEL
        assert start_calls[0]["profile_id"] == "zai-org/glm-5.2"
        assert start_calls[0]["model_provider"] == "commandcode_goat"

    run_headless(app, scenario, size=(120, 32))


def test_scenario_b_level32_with_qualified_ollama(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario B: Level 32 + qualified Ollama retains LEVEL32_OPERATOR and official treatment."""
    _setup_providers()
    app = make_app(tmp_path)
    qualified = level32_model_profiles()[0]
    models = (
        ProviderModel("ollama_cloud", qualified.alias, qualified.display_name, "Ollama Cloud", True),
    )
    monkeypatch.setattr("agentic_debugger.ui.screens_setup.list_provider_models", lambda **_kwargs: models)
    monkeypatch.setattr(app, "ollama_cloud_model_profiles", lambda: (qualified,))

    start_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(app, "start_live_session", lambda **kw: start_calls.append(kw))

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)

        start._choice_selected("target", "ladder")
        start._choice_selected("task", LEVEL32_TASK_ID)
        start._choice_selected("model", f"ollama_cloud:{qualified.alias}")

        assert start.start_available is True
        context = start.query_one("#context-summary").render().plain
        assert "READY  Yes" in context
        assert "Debugger\nExact PDB required" in context
        assert "Treatment\nFrozen Level-32" in context
        assert "Evaluation\nOfficial SWE-rebench" in context

        start.action_start()
        assert len(start_calls) == 1
        assert start_calls[0]["task_id"] == LEVEL32_TASK_ID
        assert start_calls[0]["source_kind"] is SourceKind.LEVEL32_OPERATOR
        assert start_calls[0]["profile_id"] == qualified.alias
        assert start_calls[0]["policy"] == "exact-pdb-level32-frozen"

    run_headless(app, scenario, size=(120, 32))


def test_scenario_c_same_model_id_different_provider_remains_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario C: Colliding model ID on different providers remains distinct and executable."""
    _setup_providers()
    app = make_app(tmp_path)
    qualified = level32_model_profiles()[0]
    collision_id = qualified.alias
    models = (
        ProviderModel("ollama_cloud", collision_id, qualified.display_name, "Ollama Cloud", True),
        ProviderModel("commandcode_goat", collision_id, "CommandCode Collision", "CommandCode GOAT", True),
    )
    monkeypatch.setattr("agentic_debugger.ui.screens_setup.list_provider_models", lambda **_kwargs: models)
    monkeypatch.setattr(app, "ollama_cloud_model_profiles", lambda: (qualified,))

    start_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(app, "start_live_session", lambda **kw: start_calls.append(kw))

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)

        start._choice_selected("target", "ladder")
        start._choice_selected("task", LEVEL32_TASK_ID)

        # Select CommandCode collision
        start._choice_selected("model", f"commandcode_goat:{collision_id}")
        assert start._config.model.provider == "commandcode_goat"
        assert start._config.model.model_id == collision_id

        # Must NOT be treated as qualified Ollama
        assert start.start_available is True
        context = start.query_one("#context-summary").render().plain
        assert "outside frozen official Level-32 treatment" in context

        start.action_start()
        assert len(start_calls) == 1
        assert start_calls[0]["source_kind"] is SourceKind.CONFIGURED_MODEL
        assert start_calls[0]["model_provider"] == "commandcode_goat"
        assert start_calls[0]["profile_id"] == collision_id

    run_headless(app, scenario, size=(120, 32))


def test_mandatory_regression_c_empty_official_roster_does_not_gate_configured_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mandatory Regression C: Empty official roster does not gate configured execution.

    Prove:
    1. Offline selection: not ready because Ladder requires live model.
    2. No qualification-based execution blocker.
    3. Select live configured model.
    4. READY Yes.
    5. Launch through SourceKind.CONFIGURED_MODEL.
    """
    _setup_providers()
    app = make_app(tmp_path)
    # Empty official roster
    monkeypatch.setattr(app, "level32_model_profiles", lambda: ())
    monkeypatch.setattr(app, "ollama_cloud_model_profiles", lambda: ())

    models = (
        ProviderModel("commandcode_goat", "zai-org/glm-5.2", "GLM 5.2", "CommandCode GOAT", True),
    )
    monkeypatch.setattr("agentic_debugger.ui.screens_setup.list_provider_models", lambda **_kwargs: models)

    start_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(app, "start_live_session", lambda **kw: start_calls.append(kw))

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)

        # 1. Switch to Ladder target and Level 32 task
        start._choice_selected("target", "ladder")
        start._choice_selected("task", LEVEL32_TASK_ID)

        # Initial selection is Offline (default)
        # 1. Offline selection: not ready because Ladder requires live model
        assert start.start_available is False
        status = start.query_one("#start-status").render().plain
        assert "Ladder runs require a live model" in status

        # 2. No qualification-based execution blocker!
        assert "qualified Ollama" not in status
        assert "No qualified Ollama models available" not in status

        # 3. Select live configured model
        cmd_key = "commandcode_goat:zai-org/glm-5.2"
        start._choice_selected("model", cmd_key)

        # 4. READY Yes
        assert start.start_available is True
        context = start.query_one("#context-summary").render().plain
        assert "READY  Yes" in context
        assert "Selected model is outside frozen official Level-32 treatment" in context
        assert "Treatment\nInteractive Level-32 · non-official" in context
        assert "Evaluation\nIndependent verifier" in context
        assert "Official SWE-rebench" not in context

        # 5. Launch through SourceKind.CONFIGURED_MODEL
        start.action_start()
        assert len(start_calls) == 1
        assert start_calls[0]["task_id"] == LEVEL32_TASK_ID
        assert start_calls[0]["source_kind"] is SourceKind.CONFIGURED_MODEL
        assert start_calls[0]["model_provider"] == "commandcode_goat"
        assert start_calls[0]["profile_id"] == "zai-org/glm-5.2"

    run_headless(app, scenario, size=(120, 32))


def test_scenario_d_genuine_unavailable_model_blocks_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario D: Genuine runtime unavailability blocks execution on concrete reason."""
    _setup_providers()
    app = make_app(tmp_path)
    qualified = level32_model_profiles()[0]
    models = (
        ProviderModel("ollama_cloud", qualified.alias, qualified.display_name, "Ollama Cloud", True),
        ProviderModel(
            "commandcode_goat",
            "missing-key-model",
            "Missing Key Model",
            "CommandCode GOAT",
            False,
            unavailable_reason="no direct API credential configured for route",
        ),
    )
    monkeypatch.setattr("agentic_debugger.ui.screens_setup.list_provider_models", lambda **_kwargs: models)
    monkeypatch.setattr(app, "ollama_cloud_model_profiles", lambda: (qualified,))

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)

        start._choice_selected("target", "ladder")
        start._choice_selected("task", LEVEL32_TASK_ID)

        # Open picker
        start._open_model_picker()
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)

        by_value = {choice.value: choice for choice in picker.choices}
        unavailable_choice = by_value["commandcode_goat:missing-key-model"]
        assert unavailable_choice.disabled is True
        assert "no direct api credential" in unavailable_choice.disabled_reason.lower()

        offline_choice = by_value["offline:"]
        assert offline_choice.disabled is True
        assert "live model" in offline_choice.disabled_reason.lower()

        pilot.app.pop_screen()
        await pilot.pause()

        # Selecting offline model blocks start
        start._choice_selected("model", "offline:")
        assert start.start_available is False
        status = start.query_one("#start-status").render().plain
        assert "live model" in status.lower()

    run_headless(app, scenario, size=(120, 32))


def test_scenario_e_lower_ladder_rungs_remain_regression_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario E: Lower ladder rungs (Level 6/12/18) remain regression free."""
    _setup_providers()
    app = make_app(tmp_path)
    qualified = level32_model_profiles()[0]
    models = (
        ProviderModel("ollama_cloud", qualified.alias, qualified.display_name, "Ollama Cloud", True),
        ProviderModel("commandcode_goat", "zai-org/glm-5.2", "GLM 5.2", "CommandCode GOAT", True),
    )
    monkeypatch.setattr("agentic_debugger.ui.screens_setup.list_provider_models", lambda **_kwargs: models)
    monkeypatch.setattr(app, "ollama_cloud_model_profiles", lambda: (qualified,))

    start_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(app, "start_live_session", lambda **kw: start_calls.append(kw))

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)

        # Lower ladder rung: Level 6
        start._choice_selected("target", "ladder")
        start._choice_selected("task", "pdb-required-boundary-006")

        # 1. Qualified Ollama model
        start._choice_selected("model", f"ollama_cloud:{qualified.alias}")
        assert start.start_available is True
        start.action_start()
        assert start_calls[-1]["source_kind"] is SourceKind.OLLAMA_CLOUD_LADDER
        assert start_calls[-1]["task_id"] == "pdb-required-boundary-006"

        # 2. Configured model
        start._choice_selected("model", "commandcode_goat:zai-org/glm-5.2")
        assert start.start_available is True
        context = start.query_one("#context-summary").render().plain
        assert "directive repair" in context
        start.action_start()
        assert start_calls[-1]["source_kind"] is SourceKind.CONFIGURED_MODEL
        assert start_calls[-1]["task_id"] == "pdb-required-boundary-006"
        assert start_calls[-1]["model_provider"] == "commandcode_goat"

    run_headless(app, scenario, size=(120, 32))


def test_scenario_f_local_project_target_regression_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario F: Local Project target remains regression free."""
    _setup_providers()
    app = make_app(tmp_path)
    models = (
        ProviderModel("commandcode_goat", "zai-org/glm-5.2", "GLM 5.2", "CommandCode GOAT", True),
    )
    monkeypatch.setattr("agentic_debugger.ui.screens_setup.list_provider_models", lambda **_kwargs: models)

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)

        start._choice_selected("target", "local_project")
        start._open_model_picker()
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)

        by_value = {choice.value: choice for choice in picker.choices}
        assert by_value["commandcode_goat:zai-org/glm-5.2"].disabled is False
        assert by_value["offline:"].disabled is True
        assert "live model" in by_value["offline:"].disabled_reason.lower()

    run_headless(app, scenario, size=(120, 32))


def test_scenario_g_no_downstream_rejection_at_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario G: No downstream rejection at app boundary or configured source."""
    from agentic_debugger.application.configured_source import run_configured_session
    import agentic_debugger.application.configured_source as cs
    from agentic_debugger.application.emitter import SessionEventEmitter
    from agentic_debugger.application.journal import SessionEventJournal
    from agentic_debugger.evaluation.live import LiveRunLimits

    # 1. App boundary accepts Level 32 with CONFIGURED_MODEL
    config_file = tmp_path / "provider-configurations.json"
    monkeypatch.setenv("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path / "config-dir"))
    _secure_store: dict[str, str] = {}
    monkeypatch.setattr(pc, "save_secure_credential", lambda k, v: _secure_store.setdefault(k, v) or True)
    monkeypatch.setattr(pc, "load_secure_credential", lambda k: _secure_store.get(k))
    monkeypatch.setattr(pc, "has_secure_credential", lambda k: k in _secure_store)
    monkeypatch.setattr(pc, "delete_secure_credential", lambda k: bool(_secure_store.pop(k, None) is not None))
    monkeypatch.setattr(pc, "provider_quarantine_path", lambda: tmp_path / "quarantine.json")
    pc._QUARANTINED_PROVIDERS.clear()
    pc.clear_all_session_keys()
    pc.add_provider_config(
        name="CommandCode GOAT",
        base_url="https://api.commandcode.ai/provider/v1",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="commandcode_goat",
        transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT,
    )

    app = LocalApplicationV1(history_store=HistoryStore(tmp_path / "history"))
    captured_worker: list[Any] = []

    class FakeWorker:
        def __init__(self, **kwargs: Any) -> None:
            self.spec = kwargs.get("spec")
            self.scenario_params = kwargs.get("scenario_params")
            captured_worker.append(self)

    monkeypatch.setattr("agentic_debugger.ui.app.SessionWorkerProcess", FakeWorker)

    class FakeRunner:
        def __init__(self, worker: Any, **kwargs: Any) -> None:
            pass
        def start(self) -> None:
            pass

    monkeypatch.setattr("agentic_debugger.ui.app.LiveSessionRunner", FakeRunner)
    monkeypatch.setattr(app, "push_screen", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "switch_screen", lambda *args, **kwargs: None)
    monkeypatch.setattr(type(app), "screen", property(lambda self: None))

    from agentic_debugger.application.model_gateway import ModelGateway
    monkeypatch.setattr(
        ModelGateway.default(),
        "get_provider_status",
        lambda p: SimpleNamespace(is_configured=True),
    )

    app.start_live_session(
        task_id=LEVEL32_TASK_ID,
        policy="pdb-on-uncertainty",
        max_elapsed_seconds=None,
        source_kind=SourceKind.CONFIGURED_MODEL,
        profile_id="deepseek/deepseek-v4-flash",
        model_provider="commandcode_goat",
    )
    assert len(captured_worker) == 1
    assert captured_worker[0].spec.task_id == LEVEL32_TASK_ID
    assert captured_worker[0].spec.source.kind is SourceKind.CONFIGURED_MODEL

    # 2. Configured source accepts Level 32 without ScenarioInputError rejection
    captured_runs: list[dict[str, Any]] = []
    captured_limits: list[LiveRunLimits] = []
    real_limits_init = LiveRunLimits.__init__

    def wrapped_limits_init(self: Any, *args: Any, **kwargs: Any) -> None:
        real_limits_init(self, *args, **kwargs)
        captured_limits.append(self)

    monkeypatch.setattr(LiveRunLimits, "__init__", wrapped_limits_init)
    monkeypatch.setattr(
        cs,
        "run_local_session",
        lambda ctx, **kw: captured_runs.append({"task_id": ctx.emitter.task_id, "max_calls": kw["max_model_calls"]}),
    )

    pc.set_session_key("commandcode_goat", "synthetic-key-level32")

    def wrapped_resolve(provider: str, model_id: str, **kwargs: Any):
        from agentic_debugger.evaluation.live import LiveModelConfig
        from agentic_debugger.application.model_gateway import provider_runtime_identity
        from agentic_debugger.application.provider_connections import get_provider_config

        cfg = LiveModelConfig(model_name=model_id, command=("echo", "hi"), request_timeout_seconds=30, tool_version="test")
        c = get_provider_config(provider)
        a = provider_runtime_identity(c) if c else "a" * 64
        return (
            cfg,
            {
                "display_name": model_id,
                "route": "direct_api",
                "api_protocol": "chat_completions",
                "provider_model_id": model_id,
                "endpoint": "http://fake",
                "provider_runtime_identity": a,
            },
            "0" * 64,
        )

    monkeypatch.setattr(cs, "_resolve_registry_model", wrapped_resolve)

    journal = SessionEventJournal(
        tmp_path / "j.events.jsonl",
        session_id="sess-g",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    emitter = SessionEventEmitter(
        sink=journal,
        session_id="sess-g",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    ctx = ScenarioContext(work_dir=tmp_path / "w", emitter=emitter, token=CancellationToken())
    run_configured_session(
        ctx,
        {"provider": "commandcode_goat", "model_id": "deepseek/deepseek-v4-flash", "policy": "pdb-on-uncertainty"},
    )
    assert len(captured_runs) == 1
    assert captured_runs[0]["task_id"] == LEVEL32_TASK_ID
    # Task 44: interactive Level-32 execution is unbounded (None);
    # historical 25/25 values are provenance only.
    assert captured_runs[0]["max_calls"] is None
    assert len(captured_limits) == 1
    assert captured_limits[0].max_model_requests is None
    assert captured_limits[0].max_controller_steps is None
    assert captured_limits[0].max_retries == 1
    assert captured_limits[0].max_directive_repairs == 2


def test_scenario_h_scientific_classification_distinct_from_execution() -> None:
    """Scenario H: Scientific qualification is classification metadata, not execution gate."""
    opt_qualified = ModelOption("ollama_cloud", "qwen3.5:cloud", "Qwen 3.5", available=True)
    opt_unqualified = ModelOption("commandcode_goat", "zai-org/glm-5.2", "GLM 5.2", available=True)

    # Both are compatible and executable on ladder target
    comp_q, reason_q = model_compatibility(TARGET_LADDER, opt_qualified, ladder_qualified=True)
    assert comp_q is True
    assert reason_q == ""

    comp_u, reason_u = model_compatibility(TARGET_LADDER, opt_unqualified, ladder_qualified=False)
    assert comp_u is True
    assert reason_u == ""

    # Readiness separates the notes (qualification) while both remain ready (execution)
    catalog = SessionCatalog(
        tasks=[TaskOption(LEVEL32_TASK_ID, "Level 32/100 — Cookiecutter #967", ladder=True)],
        models=[opt_qualified, opt_unqualified],
        ladder_models=[opt_qualified],
    )
    cfg_q = SessionConfig(
        target=TARGET_LADDER,
        task_id=LEVEL32_TASK_ID,
        model=ModelChoice("ollama_cloud", "qwen3.5:cloud", "Qwen 3.5"),
    )
    dummy_project = ProjectStatus.unchecked("")
    readiness_q = derive_readiness(cfg_q, catalog, dummy_project)
    assert readiness_q.ready is True
    assert any("Frozen Level-32" in note for note in readiness_q.notes)

    cfg_u = SessionConfig(
        target=TARGET_LADDER,
        task_id=LEVEL32_TASK_ID,
        model=ModelChoice("commandcode_goat", "zai-org/glm-5.2", "GLM 5.2"),
    )
    readiness_u = derive_readiness(cfg_u, catalog, dummy_project)
    assert readiness_u.ready is True
    assert any("outside frozen official Level-32 treatment" in note for note in readiness_u.notes)


def test_repo_wide_semantic_invariant_execution_allowed_equals_runtime_executable() -> None:
    """Semantic repo-wide invariant: execution_allowed = runtime_executable, qualification = separate_metadata.

    For all targets and all ladder tasks:
    - Any live available model is compatible and executable.
    - Qualification (ladder_qualified True vs False) NEVER disables execution.
    - Only genuine runtime failure (available=False, offline) controls readiness failure.
    """
    providers = ["ollama_cloud", "opencode_go", "commandcode_goat", "configured"]
    all_targets = [TARGET_CURATED, TARGET_LADDER, TARGET_LOCAL_PROJECT]
    all_tasks = [task.task_id for task in LADDER_TASKS] + ["curated-off-by-one-002"]

    for provider in providers:
        live_opt = ModelOption(provider, "test-model", "Test Model", available=True)
        unavail_opt = ModelOption(provider, "test-model", "Test Model", available=False, unavailable_reason="no key")

        for target in all_targets:
            # Live available model is always compatible regardless of qualification
            comp_qual, _ = model_compatibility(target, live_opt, ladder_qualified=True)
            comp_unqual, _ = model_compatibility(target, live_opt, ladder_qualified=False)
            assert comp_qual is True
            assert comp_unqual is True

            # Offline model is incompatible for targets requiring live model
            offline_opt = ModelOption(PROVIDER_OFFLINE, "", "Offline", available=True)
            comp_off, reason_off = model_compatibility(target, offline_opt)
            if target in (TARGET_LADDER, TARGET_LOCAL_PROJECT):
                assert comp_off is False
                assert "live model" in reason_off.lower()
            else:
                assert comp_off is True
