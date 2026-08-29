"""Focused product-surface coverage for the capability-ladder workflow."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

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


def test_ladder_start_has_provider_free_default_and_no_configured_warning(tmp_path: Path, monkeypatch) -> None:
    app = make_app(tmp_path)
    start_calls = []

    def record_start(**kwargs):
        start_calls.append(kwargs)

    monkeypatch.setattr(app, "start_live_session", record_start)

    def fail_configured_lookup():
        raise AssertionError("ladder setup must not inspect configured profiles")

    monkeypatch.setattr(app, "configured_profiles", fail_configured_lookup)

    async def scenario(pilot):
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)
        start._choice_selected("task", "pdb-required-boundary-006")
        profiles = level32_model_profiles()
        assert start.profile_id == profiles[0].alias
        assert start.start_available is True
        context = start.query_one("#context-summary").render().plain
        assert "Task\nLevel 6/100" in context
        assert f"Alias\n{profiles[0].alias}" in context
        assert "Ready\nYes" in context
        assert "configured model profiles" not in start.query_one("#start-status").render().plain
        assert "Accepted Level-6 contract" in context
        assert "Independent verifier" in context
        assert "Frozen Level-32" not in context
        assert "Official SWE-rebench" not in context

        start.action_start()
        assert start_calls[0]["task_id"] == "pdb-required-boundary-006"
        assert start_calls[0]["profile_id"] == profiles[0].alias
        assert start_calls[0]["source_kind"] is SourceKind.OLLAMA_CLOUD_LADDER

        await pilot.press("enter")
        assert isinstance(pilot.app.screen, ChoicePickerScreen)
        assert pilot.app.screen.title == "Select task"
        assert [choice.value for choice in pilot.app.screen.choices] == [
            task_id for _, task_id in app.curated_task_options()
        ]
        assert [choice.value for choice in pilot.app.screen.choices[-4:]] == [
            item.task_id for item in LADDER_TASKS
        ]
        assert any(choice.value.startswith("curated-") for choice in pilot.app.screen.choices[:-4])
        await pilot.press("escape")

        start._choice_selected("task", "audreyr__cookiecutter-967")
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
    panel.update_view(view, elapsed="01:42")
    context = panel._text.render().plain
    assert "DeepSeek V4 Flash" in context
    assert "deepseek-v4-flash:cloud" in context
    assert "Model running" in context
    assert "01:42" in context
    assert "PDB\nPending" in context
    assert "Verifier\nPending" in context
    assert "Official SWE-rebench" in context
    assert "Official tests\nNot started" in context

    # Official milestone truth: preparing/launched/completed stay distinct
    # preparatory states; only the typed execution-proven fact renders
    # "Executed".
    def _official_for(stage: OperatorStage, proven=None) -> str:
        milestone_view = SessionViewState(
            task_id="audreyr__cookiecutter-967",
            source_kind=SourceKind.LEVEL32_OPERATOR,
            status=SessionStatus.RUNNING,
            operator_stage=stage,
            official_execution_proven=proven,
        )
        milestone_panel = LiveRunContextPanel()
        milestone_panel.update_view(milestone_view, elapsed="00:00")
        return milestone_panel._text.render().plain

    assert "Official tests\nPreparing" in _official_for(
        OperatorStage.OFFICIAL_VERIFICATION_PREPARING
    )
    assert "Official tests\nEvaluator launched" in _official_for(
        OperatorStage.OFFICIAL_EVALUATOR_STARTED
    )
    unproven_completed = _official_for(OperatorStage.OFFICIAL_EVALUATOR_COMPLETED)
    assert "Official tests\nCompleted (unproven)" in unproven_completed
    assert "Executed" not in unproven_completed.split("Official tests", 1)[1].splitlines()[1]
    assert "Official tests\nExecuted" in _official_for(
        OperatorStage.OFFICIAL_EVALUATOR_COMPLETED, proven=True
    )

    lower_view = SessionViewState(
        task_id="pdb-required-multistage-units-008",
        source_kind=SourceKind.OLLAMA_CLOUD_LADDER,
        status=SessionStatus.RUNNING,
        operator_stage=OperatorStage.VERIFICATION,
        model_provenance=ModelProvenanceView(
            profile_id="deepseek-v4-flash:cloud",
            display_name="DeepSeek V4 Flash",
        ),
    )
    lower_panel = LiveRunContextPanel()
    lower_panel.update_view(lower_view, elapsed="00:18")
    lower_context = lower_panel._text.render().plain
    assert "Level 18/100" in lower_context
    assert "Independent verifier" in lower_context
    assert "Stage\nVerification" in lower_context
    assert "Official verification" not in lower_context
    assert "Official tests" not in lower_context

    official_panel = LiveRunContextPanel()
    official_panel.update_view(replace(view, operator_stage=OperatorStage.OFFICIAL_VERIFICATION))
    official_context = official_panel._text.render().plain
    assert "Stage\nOfficial verification" in official_context


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
    panel.update_view(view, elapsed="00:02")
    context = panel._text.render().plain
    assert "Stage\nModel error" in context
    assert "PDB\nNot reached" in context
    assert "Verifier\nNot run" in context
    assert "Stage\nCompleted" not in context
    assert "PDB\nPending" not in context
    assert "Verifier\nPending" not in context

    cancelled = replace(
        view,
        status=SessionStatus.CANCELLED,
        termination_reason=SessionTerminationReason.CANCELLED,
    )
    panel.update_view(cancelled)
    cancelled_context = panel._text.render().plain
    assert "Stage\nCancelled" in cancelled_context
    assert "PDB\nNot reached" in cancelled_context
    assert "Verifier\nNot run" in cancelled_context


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
    assert "Stage\nController budget exhausted" in context
    assert "PDB\nNot reached" in context
    assert "Verifier\nNot run" in context


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
    assert "Stage\nFinalizing" in panel._text.render().plain


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
        assert "1-8 activity filters" in failed_footer

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
        assert "1-8 activity filters" in footer

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


def test_level32_model_selection_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic, provider-free proof that selecting model X in Level-32 picker
    passes model X strictly to start_live_session."""
    app = make_app(tmp_path)
    start_calls = []

    def record_start(**kwargs):
        start_calls.append(kwargs)

    monkeypatch.setattr(app, "start_live_session", record_start)

    async def scenario(pilot):
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)
        # Select Level-32 task
        start._choice_selected("task", "audreyr__cookiecutter-967")
        profiles = level32_model_profiles()
        assert len(profiles) >= 2
        # Target model X (e.g. deepseek-v4-flash:cloud or second profile)
        target = next((p for p in profiles if "deepseek" in p.alias), profiles[1])
        start._open_choice_picker("model")
        await pilot.pause()
        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)
        assert picker.title == "Select qualified Ollama model"
        assert "Scientific Level-32 roster" in (picker.subtitle or "")

        # Select the target model in picker
        picker._on_select(target.alias)
        pilot.app.pop_screen()
        await pilot.pause()

        assert start.profile_id == target.alias
        assert target.display_name in start.query_one("#model-row").render().plain

        # Start session
        start.action_start()
        assert len(start_calls) == 1
        assert start_calls[0]["task_id"] == "audreyr__cookiecutter-967"
        assert start_calls[0]["profile_id"] == target.alias
        assert start_calls[0]["source_kind"] is SourceKind.LEVEL32_OPERATOR

    run_headless(app, scenario, size=(120, 32))


def test_ladder_model_picker_empty_state_is_truthful_and_domain_specific(tmp_path: Path, monkeypatch) -> None:
    app = make_app(tmp_path)
    monkeypatch.setattr(app, "level32_model_profiles", lambda: ())

    async def scenario(pilot):
        start = pilot.app.screen
        assert isinstance(start, StartSessionScreen)
        # Select Level-32 task
        start._choice_selected("task", "audreyr__cookiecutter-967")
        start._open_choice_picker("model")
        await pilot.pause()

        picker = pilot.app.screen
        assert isinstance(picker, ChoicePickerScreen)
        assert picker.title == "Select qualified Ollama model"
        assert "Scientific Level-32 roster" in (picker.subtitle or "")

        empty_widget = picker.query_one("#choice-picker-empty")
        empty_text = str(empty_widget.render())
        assert "No qualified Ollama models available." in empty_text
        assert "custom command profile" not in empty_text.casefold()
        assert "custom command profiles" not in empty_text.casefold()

    run_headless(app, scenario, size=(120, 32))
