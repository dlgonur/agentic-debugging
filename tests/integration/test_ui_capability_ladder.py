"""Focused product-surface coverage for the capability-ladder workflow."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

textual = pytest.importorskip("textual")

from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    OperatorStage,
    SessionEvent,
    SessionEventKind,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
)
from agentic_debugger.application.level32 import LADDER_TASKS, level32_model_profiles
from agentic_debugger.application.model_providers import ProviderModel
from agentic_debugger.application.presentation import (
    ModelProvenanceView,
    PresentationIdentity,
    SessionViewState,
    initial_session_view,
)
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.screens import (
    ChoicePickerScreen,
    StartSessionScreen,
    WorkspaceMode,
    WorkspaceScreen,
    render_view_header,
)
from agentic_debugger.ui.widgets import LiveBar, LiveRunContextPanel
from ui_support import run_headless


def make_app(tmp_path: Path) -> LocalApplicationV1:
    return LocalApplicationV1(history_store=HistoryStore(tmp_path))


def test_new_session_catalog_keeps_provider_free_tasks_first_and_exposes_ladder(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    options = app.curated_task_options()
    ladder = tuple(
        (f"{item.title} · {item.task_id}", item.task_id)
        for item in LADDER_TASKS
    )
    ladder_ids = {item.task_id for item in LADDER_TASKS}
    curated = tuple(
        option for option in options if option[1] not in ladder_ids
    )
    assert options[: len(curated)] == curated
    assert [task_id for _, task_id in curated] == [
        task_id for task_id in app.curated_task_ids() if task_id not in ladder_ids
    ]
    assert options[len(curated) :] == ladder
    assert [task_id for _, task_id in options[len(curated) :]] == [
        "pdb-required-boundary-006",
        "pdb-required-caller-callee-007",
        "pdb-required-multistage-units-008",
        "audreyr__cookiecutter-967",
    ]


def test_ladder_setup_and_start_use_one_unified_surface(tmp_path: Path, monkeypatch) -> None:
    app = make_app(tmp_path)
    start_calls = []

    def record_start(**kwargs):
        start_calls.append(kwargs)

    monkeypatch.setattr(app, "start_live_session", record_start)

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)
        # One screen, one stack: target -> task -> model, explicitly.
        start._choice_selected("target", "ladder")
        start._choice_selected("task", "pdb-required-boundary-006")
        profiles = level32_model_profiles()
        assert profiles, "qualified roster expected on this machine"
        # Before a model is chosen the run is honestly blocked; no silent
        # default mutates the user's (offline) selection.
        # For lower ladder rungs any live provider is now executable
        assert start.start_available is False
        assert "live model" in start.query_one("#start-status").render().plain.lower()
        start._choice_selected("model", f"ollama_cloud:{profiles[0].alias}")
        assert start.profile_id == profiles[0].alias
        assert start.start_available is True
        context = start.query_one("#context-summary").render().plain
        assert "Task\nLevel 6/100" in context
        assert "Treatment\nAccepted Level-6 contract" in context
        assert "Evaluation\nIndependent verifier" in context
        assert "READY  Yes" in context
        assert "operator contract" in context  # canonical-operator notice
        assert "Frozen Level-32" not in context
        assert "Official SWE-rebench" not in context

        # Frozen controls stay visible but disabled, with their reasons.
        assert start.query_one("#debugger-row").is_disabled
        assert start.query_one("#time-limit-row").is_disabled
        assert start.query_one("#auto-retry-row").is_disabled

        start.action_start()
        assert start_calls[0]["task_id"] == "pdb-required-boundary-006"
        assert start_calls[0]["profile_id"] == profiles[0].alias
        assert start_calls[0]["source_kind"] is SourceKind.OLLAMA_CLOUD_LADDER

        # The task picker keeps every task visible; under the ladder
        # target the curated entries are disabled with a reason, and the
        # rungs stay selectable.
        start._open_task_picker()
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)
        assert picker.title == "Select task"
        assert [choice.value for choice in picker.choices] == [
            task_id for _, task_id in app.curated_task_options()
        ]
        ladder_ids = {item.task_id for item in LADDER_TASKS}
        for choice in picker.choices:
            if choice.value in ladder_ids:
                assert choice.disabled is False
            else:
                assert choice.disabled is True
                assert choice.disabled_reason
        await pilot.press("escape")

        start._choice_selected("task", "audreyr__cookiecutter-967")
        start._choice_selected("model", f"ollama_cloud:{profiles[0].alias}")
        level32_context = start.query_one("#context-summary").render().plain
        assert "Level 32/100 — Cookiecutter #967" in level32_context
        assert "Frozen Level-32" in level32_context
        assert "Official SWE-rebench" in level32_context
        assert "Accepted Level-6 contract" not in level32_context

    run_headless(app, scenario, size=(120, 32))


def test_wide_ladder_header_is_concise_and_context_panel_is_truthful() -> None:
    view = SessionViewState(
        task_id="audreyr__cookiecutter-967",
        source_kind=SourceKind.LEVEL32_OPERATOR,
        status=SessionStatus.RUNNING,
        operator_stage=OperatorStage.MODEL_RUNNING,
        model_provenance=ModelProvenanceView(
            profile_id="deepseek-v4-flash:cloud",
            display_name="DeepSeek V4 Flash",
            treatment_revision=7,
        ),
    )
    header = render_view_header(view, mode="LIVE", mode_style="bold")
    assert "Level 32/100 — Cookiecutter #967" in header.plain
    assert "Model running" in header.plain
    assert "deepseek-v4-flash:cloud" not in header.plain
    assert "Level-32 authoritative operator" not in header.plain

    panel = LiveRunContextPanel()
    panel.update_view(view)
    context = panel._text.render().plain
    assert "RUN CONTEXT" in context
    assert "DeepSeek V4 Flash" in context
    # Header owns task identity, stage, and elapsed; rail does not duplicate them
    assert "Level 32/100" not in context
    assert "Cookiecutter" not in context
    assert "Model running" not in context
    assert "01:42" not in context
    assert "PDB" in context
    assert "Pending" in context
    assert "VERIFIER" in context
    assert "Pending" in context


def test_terminal_model_error_header_and_sidebar_are_truthful() -> None:
    view = SessionViewState(
        task_id="pdb-required-boundary-006",
        source_kind=SourceKind.OLLAMA_CLOUD_LADDER,
        status=SessionStatus.FAILED,
        termination_reason=SessionTerminationReason.MODEL_ERROR,
        operator_stage=OperatorStage.COMPLETED,
        cleanup_verified=True,
        model_provenance=ModelProvenanceView(
            profile_id="deepseek-v4-flash:cloud",
            display_name="deepseek-v4-flash",
        ),
    )
    header = render_view_header(view, mode="LIVE", mode_style="bold")
    assert "Failed" in header.plain
    assert "model error" in header.plain
    assert "cleanup verified" in header.plain
    assert "verifier: —" not in header.plain

    panel = LiveRunContextPanel()
    panel.update_view(view)
    context = panel._text.render().plain
    assert "PDB" in context
    assert "Not reached" in context
    assert "VERIFIER" in context
    assert "Not started" in context
    assert "Failed" not in context
    assert "Model error" not in context

    cancelled = replace(
        view,
        status=SessionStatus.CANCELLED,
        termination_reason=SessionTerminationReason.CANCELLED,
    )
    panel.update_view(cancelled)
    cancelled_context = panel._text.render().plain
    assert "PDB" in cancelled_context
    assert "Not reached" in cancelled_context
    assert "VERIFIER" in cancelled_context
    assert "Not started" in cancelled_context
    assert "Cancelled" not in cancelled_context


def test_terminal_level32_operator_error_header_is_specific() -> None:
    view = SessionViewState(
        task_id="audreyr__cookiecutter-967",
        source_kind=SourceKind.LEVEL32_OPERATOR,
        status=SessionStatus.FAILED,
        termination_reason=SessionTerminationReason.SUBPROCESS_ERROR,
        cleanup_verified=False,
    )
    header = render_view_header(view, mode="LIVE", mode_style="bold")
    assert "Failed" in header.plain
    assert "operator error" in header.plain
    assert "cleanup failed" in header.plain
    assert "verifier: —" not in header.plain


def test_level6_controller_budget_failure_is_specific_and_verifier_is_not_claimed() -> None:
    view = SessionViewState(
        task_id="pdb-required-boundary-006",
        source_kind=SourceKind.OLLAMA_CLOUD_LADDER,
        status=SessionStatus.FAILED,
        termination_reason=SessionTerminationReason.DIRECTIVE_EXHAUSTED,
        cleanup_verified=True,
    )
    header = render_view_header(view, mode="LIVE", mode_style="bold")
    assert "controller budget exhausted" in header.plain
    assert "verifier: —" not in header.plain

    panel = LiveRunContextPanel()
    panel.update_view(view)
    context = panel._text.render().plain
    assert "PDB" in context
    assert "Not reached" in context
    assert "VERIFIER" in context
    assert "Not started" in context
    assert "Budget exhausted" not in context
    assert "Controller budget exhausted" not in context


def test_level32_completed_progress_while_running_is_presented_as_finalizing() -> None:
    view = SessionViewState(
        task_id="audreyr__cookiecutter-967",
        source_kind=SourceKind.LEVEL32_OPERATOR,
        status=SessionStatus.RUNNING,
        operator_stage=OperatorStage.COMPLETED,
    )
    header = render_view_header(view, mode="LIVE", mode_style="bold")
    assert "Running  ·  Finalizing" in header.plain
    panel = LiveRunContextPanel()
    panel.update_view(view)
    # Stage/finalizing is owned exclusively by the header
    assert "Finalizing" not in panel._text.render().plain


def test_live_footer_refreshes_when_running_session_fails(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    identity = PresentationIdentity(
        task_id="pdb-required-boundary-006",
        source_kind=SourceKind.OLLAMA_CLOUD_LADDER,
        session_id="session-footer-transition",
    )
    running = replace(initial_session_view(identity), status=SessionStatus.RUNNING)
    workspace = WorkspaceScreen(
        mode=WorkspaceMode.LIVE,
        identity=identity,
        view=running,
    )
    failed = SessionEvent(
        schema_version=SESSION_EVENT_SCHEMA_VERSION,
        session_id="session-footer-transition",
        task_id="pdb-required-boundary-006",
        run_id="run-footer-transition",
        sequence=0,
        timestamp_utc="2026-08-25T12:00:00Z",
        source_kind=SourceKind.OLLAMA_CLOUD_LADDER,
        event_kind=SessionEventKind.SESSION_FAILED,
        controller_phase=None,
        payload={"status": "failed", "termination_reason": "model_error"},
    )

    async def scenario(pilot):
        pilot.app.push_screen(workspace)
        await pilot.pause()
        running_footer = str(workspace.query_one("#live-bar", LiveBar).render())
        assert "c cancel" in running_footer

        pilot.app._live_events = (failed,)
        workspace.refresh_live()
        await pilot.pause()

        assert workspace._view.status is SessionStatus.FAILED
        failed_footer = str(workspace.query_one("#live-bar", LiveBar).render())
        assert "c cancel" not in failed_footer
        assert "1-7 tabs" in failed_footer
        assert "activity filters" not in failed_footer

    run_headless(app, scenario, size=(120, 32))


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (SessionStatus.CANCELLED, SessionTerminationReason.CANCELLED),
        (SessionStatus.SUCCEEDED, SessionTerminationReason.DONE),
    ],
)
def test_terminal_footer_never_advertises_cancel(
    tmp_path: Path,
    status: SessionStatus,
    reason: SessionTerminationReason,
) -> None:
    app = make_app(tmp_path)
    identity = PresentationIdentity(
        task_id="pdb-required-boundary-006",
        source_kind=SourceKind.OLLAMA_CLOUD_LADDER,
    )
    terminal = replace(
        initial_session_view(identity),
        status=status,
        termination_reason=reason,
    )

    async def scenario(pilot):
        workspace = WorkspaceScreen(
            mode=WorkspaceMode.LIVE,
            identity=identity,
            view=terminal,
        )
        pilot.app.push_screen(workspace)
        await pilot.pause()
        footer = str(workspace.query_one("#live-bar", LiveBar).render())
        assert "c cancel" not in footer
        assert "1-7 tabs" in footer
        assert "activity filters" not in footer

    run_headless(app, scenario, size=(120, 32))


def test_live_context_panel_is_wide_only(tmp_path: Path) -> None:
    def make_workspace() -> WorkspaceScreen:
        identity = PresentationIdentity(
            task_id="audreyr__cookiecutter-967",
            source_kind=SourceKind.LEVEL32_OPERATOR,
        )
        return WorkspaceScreen(
            mode=WorkspaceMode.LIVE,
            identity=identity,
            view=initial_session_view(identity),
        )

    wide_app = make_app(tmp_path / "wide")

    async def wide_scenario(pilot):
        pilot.app.push_screen(make_workspace())
        await pilot.pause()
        assert pilot.app.screen.query_one("#live-run-context").display is True

    run_headless(wide_app, wide_scenario, size=(120, 32))

    narrow_app = make_app(tmp_path / "narrow")

    async def narrow_scenario(pilot):
        pilot.app.push_screen(make_workspace())
        await pilot.pause()
        assert pilot.app.screen.query_one("#live-run-context").display is False

    run_headless(narrow_app, narrow_scenario, size=(80, 32))


def test_level32_model_selection_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting a qualified model in the ONE unified picker routes to Level32 operator without hardcoded alias."""
    from agentic_debugger.application import provider_connections as pc

    pc.add_provider_config(
        name="Ollama",
        base_url="https://ollama.com",
        api_format=pc.PROTOCOL_CHAT_COMPLETIONS,
        provider_id="ollama_cloud",
        transport_profile=pc.TRANSPORT_OLLAMA_CLOUD,
    )
    app = make_app(tmp_path)
    start_calls = []

    def record_start(**kwargs):
        start_calls.append(kwargs)

    monkeypatch.setattr(app, "start_live_session", record_start)

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)
        start._choice_selected("target", "ladder")
        start._choice_selected("task", "audreyr__cookiecutter-967")
        profiles = level32_model_profiles()
        assert len(profiles) >= 2
        # Target model X (e.g. deepseek-v4-flash:cloud or second profile)
        target = next((p for p in profiles if "deepseek" in p.alias), profiles[1])
        start._open_model_picker()
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)
        assert picker.title == "Select model"
        # The scientific roster is a group inside the ONE model picker;
        # non-Ollama entries stay visible but disabled under the ladder.
        values = [choice.value for choice in picker.choices]
        assert f"ollama_cloud:{target.alias}" in values
        offline = next(c for c in picker.choices if c.value.startswith("offline:"))
        assert offline.disabled is True

        # Select the target model in picker
        picker._on_select(f"ollama_cloud:{target.alias}")
        pilot.app.pop_screen()
        await pilot.pause()

        assert start.profile_id == target.alias
        from agentic_debugger.application.model_providers import format_model_display_name

        assert format_model_display_name(target.alias) in start.query_one("#model-row").render().plain

        # Start session
        start.action_start()
        assert len(start_calls) == 1
        assert start_calls[0]["task_id"] == "audreyr__cookiecutter-967"
        assert start_calls[0]["profile_id"] == target.alias
        assert start_calls[0]["source_kind"] is SourceKind.LEVEL32_OPERATOR
        assert start_calls[0]["policy"] == "exact-pdb-level32-frozen"

    run_headless(app, scenario, size=(120, 32))


def test_real_model_picker_keeps_provider_groups_visible_across_target_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing Target changes compatibility, never provider discoverability."""
    from agentic_debugger.application import provider_connections as pc

    pc.add_provider_config(name="Ollama Cloud", base_url="https://ollama.com", api_format=pc.PROTOCOL_CHAT_COMPLETIONS, provider_id="ollama_cloud", transport_profile=pc.TRANSPORT_OLLAMA_CLOUD)
    pc.add_provider_config(name="OpenCode Go", base_url="https://opencode.ai/zen/go/v1", api_format=pc.PROTOCOL_CHAT_COMPLETIONS, provider_id="opencode_go", transport_profile=pc.TRANSPORT_OPENCODE_GO)
    pc.add_provider_config(name="CommandCode GOAT", base_url="https://api.commandcode.ai/provider/v1", api_format=pc.PROTOCOL_CHAT_COMPLETIONS, provider_id="commandcode_goat", transport_profile=pc.TRANSPORT_COMMANDCODE_GOAT)

    app = make_app(tmp_path)
    qualified = level32_model_profiles()[0]
    collision_id = qualified.alias
    models = (
        ProviderModel(
            "ollama_cloud",
            collision_id,
            qualified.display_name,
            "Ollama Cloud",
            True,
        ),
        ProviderModel(
            "ollama_cloud",
            "glm-5.3-flash:cloud",
            "glm-5.3-flash",
            "Ollama Cloud",
            True,
        ),
        ProviderModel(
            "opencode_go",
            "opencode-go/glm-5.3",
            "glm-5.3",
            "OpenCode Go",
            True,
        ),
        ProviderModel(
            "commandcode_goat",
            collision_id,
            "CommandCode collision",
            "CommandCode GOAT",
            True,
        ),
    )
    monkeypatch.setattr(
        "agentic_debugger.ui.screens.list_provider_models",
        lambda **_kwargs: models,
    )
    monkeypatch.setattr(app, "ollama_cloud_model_profiles", lambda: (qualified,))
    monkeypatch.setattr(
        app,
        "configured_profiles",
        lambda: (
            (
                SimpleNamespace(
                    profile_id="custom-model",
                    display_name="Custom model",
                    executable="custom-adapter",
                ),
            ),
            None,
        ),
    )
    start_calls: list[dict] = []
    monkeypatch.setattr(app, "start_live_session", lambda **kw: start_calls.append(kw))

    expected_groups = [
        "OFFLINE",
        "OLLAMA CLOUD",
        "OPENCODE GO",
        "COMMANDCODE GOAT",
        "CUSTOM COMMAND PROFILES",
    ]

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)

        start._open_model_picker()
        await pilot.pause()
        curated_picker = pilot.app.screen
        assert isinstance(curated_picker, ChoicePickerScreen)
        curated_groups = [choice.group for choice in curated_picker.choices if choice.group]
        assert curated_groups == expected_groups
        pilot.app.pop_screen()
        await pilot.pause()

        start._choice_selected("target", "ladder")
        start._choice_selected("task", "audreyr__cookiecutter-967")
        start._open_model_picker()
        await pilot.pause()
        ladder_picker = pilot.app.screen
        assert isinstance(ladder_picker, ChoicePickerScreen)
        ladder_groups = [choice.group for choice in ladder_picker.choices if choice.group]
        assert ladder_groups == expected_groups

        by_value = {choice.value: choice for choice in ladder_picker.choices}
        qualified_key = f"ollama_cloud:{collision_id}"
        assert by_value[qualified_key].disabled is False
        assert sum(choice.value == qualified_key for choice in ladder_picker.choices) == 1
        assert by_value["ollama_cloud:glm-5.3-flash:cloud"].disabled is True
        assert "Scientific ladder contract" in by_value[
            "ollama_cloud:glm-5.3-flash:cloud"
        ].disabled_reason
        for key in (
            "opencode_go:opencode-go/glm-5.3",
            f"commandcode_goat:{collision_id}",
            "configured:custom-model",
        ):
            assert by_value[key].disabled is True
            assert "Scientific ladder contract" in by_value[key].disabled_reason
        offline = by_value["offline:"]
        assert offline.disabled is True
        assert "live model" in offline.disabled_reason.lower()

        # A colliding CommandCode model id remains CommandCode and blocked
        # for the frozen Level-32 treatment; it is not reinterpreted as the
        # qualified Ollama alias.
        pilot.app.pop_screen()
        await pilot.pause()
        start._choice_selected("model", f"commandcode_goat:{collision_id}")
        assert start._config.model.provider == "commandcode_goat"
        assert start.start_available is False
        start.action_start()
        assert start_calls == []

    run_headless(app, scenario, size=(120, 36))


def test_ladder_empty_roster_blocks_start_with_domain_reason(tmp_path: Path, monkeypatch) -> None:
    app = make_app(tmp_path)
    monkeypatch.setattr(app, "level32_model_profiles", lambda: ())

    async def scenario(pilot):
        await pilot.press("s")
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)
        start._choice_selected("target", "ladder")
        start._choice_selected("task", "audreyr__cookiecutter-967")
        assert start.start_available is False
        status = start.query_one("#start-status").render().plain
        assert "No qualified Ollama models available" in status
        assert "custom command profile" not in status.casefold()

    run_headless(app, scenario, size=(120, 32))
