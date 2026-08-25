"""Focused product-surface coverage for the capability-ladder workflow."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

textual = pytest.importorskip("textual")

from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.events import OperatorStage, SessionStatus, SourceKind
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
from agentic_debugger.ui.widgets import LiveRunContextPanel
from ui_support import run_headless


def make_app(tmp_path: Path) -> LocalApplicationV1:
    return LocalApplicationV1(history_store=HistoryStore(tmp_path))


def test_new_session_catalog_is_exactly_the_four_accepted_rungs(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    assert app.curated_task_options() == tuple(
        (f"{item.title} · {item.task_id}", item.task_id)
        for item in LADDER_TASKS
    )
    assert [task_id for _, task_id in app.curated_task_options()] == [
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
        assert all("curated-" not in choice.value for choice in pilot.app.screen.choices)
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
    assert "Official tests\nNot executed" in context

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
