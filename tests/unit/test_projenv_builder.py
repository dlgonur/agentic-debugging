"""ProjEnv inline builder + error fidelity, A2 (local-project-projenv-builder-a2).

Builder is primary, single-line DSL is the advanced toggle.  Same
ProjectRuntimeEnvironmentSpec, same fail-closed gates.  Offline only.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("textual")

from agentic_debugger.ui.session_config import (
    ProjEnvEntry,
    dsl_to_projenv_entries,
    explain_project_env_error,
    parse_project_env_declarations,
    projenv_entries_error,
    projenv_entries_to_dsl,
    summarize_project_env_declarations,
)


def _run_git(cwd: Path, args: list[str]) -> None:
    r = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, r.stderr


def _run_async(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure parity: builder <-> DSL never loses or reorders (semantic parity)
# ---------------------------------------------------------------------------


def test_builder_to_dsl_to_builder_identical():
    entries = (
        ProjEnvEntry("FOO", True, False),
        ProjEnvEntry("BAR", False, False),
        ProjEnvEntry("DB_URL", True, True),
        ProjEnvEntry("TOKEN", False, True),
    )
    dsl = projenv_entries_to_dsl(entries)
    assert dsl == "FOO, BAR?, secret:DB_URL, secret:TOKEN?"
    assert dsl_to_projenv_entries(dsl) == entries


def test_dsl_to_builder_to_dsl_stable():
    # DSL -> builder -> DSL is normalized (spacing/case) but stable: the
    # second serialization is byte-identical and entries never reorder.
    cases = [
        "FOO",
        "FOO, BAR?, secret:DB_URL, secret:TOKEN?",
        "FOO, secret:BAR, BAZ?",
    ]
    for text in cases:
        entries = dsl_to_projenv_entries(text)
        dsl2 = projenv_entries_to_dsl(entries)
        assert dsl_to_projenv_entries(dsl2) == entries
        assert projenv_entries_to_dsl(dsl_to_projenv_entries(dsl2)) == dsl2


def test_parity_prefixes_and_empty_text():
    assert dsl_to_projenv_entries("") == ()
    assert dsl_to_projenv_entries("   ") == ()
    assert projenv_entries_to_dsl(()) == ""
    # ? / secret: prefixes survive both directions.
    assert dsl_to_projenv_entries("FOO?") == (ProjEnvEntry("FOO", False, False),)
    assert projenv_entries_to_dsl([ProjEnvEntry("FOO", False, False)]) == "FOO?"
    assert dsl_to_projenv_entries("secret:DB_URL?") == (
        ProjEnvEntry("DB_URL", False, True),
    )
    assert projenv_entries_to_dsl([ProjEnvEntry("DB_URL", False, True)]) == "secret:DB_URL?"
    # Order is preserved (inherit/secrets interleave, unlike the grouped parse).
    ordered = dsl_to_projenv_entries("B, secret:S1, A?, secret:S2?")
    assert [entry.name for entry in ordered] == ["B", "S1", "A", "S2"]
    assert projenv_entries_to_dsl(ordered) == "B, secret:S1, A?, secret:S2?"


def test_parity_invalid_declarations_survive_switch():
    # Invalid declarations are not dropped by the syntactic split: they
    # survive builder <-> DSL so the builder can surface them inline.
    entries = dsl_to_projenv_entries("FOO=bar, HAS SPACE")
    assert [entry.name for entry in entries] == ["FOO=bar", "HAS SPACE"]
    dsl2 = projenv_entries_to_dsl(entries)
    assert dsl_to_projenv_entries(dsl2) == entries
    with pytest.raises(ValueError):
        parse_project_env_declarations(dsl2)


# ---------------------------------------------------------------------------
# Same validator: builder serializes through the existing path
# ---------------------------------------------------------------------------


def test_builder_valid_state_passes_existing_validator():
    entries = [
        ProjEnvEntry("FOO", True, False),
        ProjEnvEntry("BAR", False, False),
        ProjEnvEntry("DB_URL", True, True),
    ]
    dsl = projenv_entries_to_dsl(entries)
    inherit, secrets = parse_project_env_declarations(dsl)
    assert inherit == (("FOO", True), ("BAR", False))
    assert secrets == (("DB_URL", True),)
    assert projenv_entries_error(entries) is None
    assert explain_project_env_error(dsl) is None


def test_builder_invalid_state_fails_existing_validator():
    entries = [ProjEnvEntry("FOO=bar", True, False)]
    dsl = projenv_entries_to_dsl(entries)
    with pytest.raises(ValueError):
        parse_project_env_declarations(dsl)
    assert projenv_entries_error(entries) is not None


def test_secret_values_never_entered_displayed_or_persisted():
    entries = [ProjEnvEntry("DB_URL", True, True), ProjEnvEntry("FOO", False, False)]
    dsl = projenv_entries_to_dsl(entries)
    assert "=" not in dsl
    assert "DB_URL" in dsl and "FOO" in dsl
    # No value field exists on the row model; serialization is names-only.
    assert not any(hasattr(entry, "value") for entry in entries)
    assert summarize_project_env_declarations(dsl) == "1 inherit · 1 secret"


# ---------------------------------------------------------------------------
# Inline error fidelity: exact declaration + rule, not generic Invalid
# ---------------------------------------------------------------------------


def test_invalid_names_exact_declaration_and_rule_inline():
    err_eq = projenv_entries_error([ProjEnvEntry("FOO=bar", True, False)])
    assert err_eq is not None and "row 1" in err_eq and "FOO=bar" in err_eq
    assert "=" in err_eq

    err_space = projenv_entries_error([ProjEnvEntry("HAS SPACE", True, False)])
    assert err_space is not None and "row 1" in err_space and "HAS SPACE" in err_space
    assert "space" in err_space.lower()

    err_empty = projenv_entries_error([ProjEnvEntry("   ", True, False)])
    assert err_empty is not None and "row 1" in err_empty and "empty" in err_empty.lower()

    # Second-row attribution (which row has the bad declaration).
    err_second = projenv_entries_error(
        [ProjEnvEntry("GOOD", True, False), ProjEnvEntry("BAD NAME", True, False)]
    )
    assert err_second is not None and "row 2" in err_second and "BAD NAME" in err_second


def test_dsl_error_explains_row_and_rule():
    err = explain_project_env_error("FOO, BAD NAME")
    assert err is not None and "row 2" in err and "BAD NAME" in err
    assert explain_project_env_error("FOO, secret:DB_URL") is None
    assert explain_project_env_error("") is None
    # Row summary still degrades gracefully when invalid.
    assert summarize_project_env_declarations("FOO=bar") == "Invalid — edit to fix"


def test_readiness_blocks_invalid_projenv(tmp_path=None):
    from agentic_debugger.ui.session_config import (
        PROVIDER_OLLAMA,
        ModelChoice,
        ModelOption,
        ProjectStatus,
        SessionCatalog,
        SessionConfig,
        TARGET_LOCAL_PROJECT,
        ROW_PROJECT_ENV,
        SEVERITY_ERROR,
        derive_readiness,
    )

    catalog = SessionCatalog(
        models=(ModelOption(PROVIDER_OLLAMA, "m", "m"),),
    )
    config = SessionConfig(
        target=TARGET_LOCAL_PROJECT,
        project_path="C:/repo",
        bug_description="bug",
        model=ModelChoice(PROVIDER_OLLAMA, "m", "m"),
        project_env_text="FOO=bar",
    )
    readiness = derive_readiness(
        config, catalog, ProjectStatus("C:/repo", True, "clean", "Git: repo @ abc1234")
    )
    assert readiness.ready is False
    issues = [i for i in readiness.issues if i.field == ROW_PROJECT_ENV]
    assert issues and issues[0].severity == SEVERITY_ERROR
    # The blocker names the declaration + rule (not a generic Invalid).
    assert "FOO=bar" in issues[0].message


def test_per_category_max_enforced_with_per_row_message():
    entries = tuple(ProjEnvEntry(f"V{i:02d}", True, False) for i in range(33))
    err = projenv_entries_error(entries)
    assert err is not None and "row 33" in err and "32" in err
    with pytest.raises(ValueError):
        parse_project_env_declarations(projenv_entries_to_dsl(entries))
    # 32 valid declarations pass.
    ok = tuple(ProjEnvEntry(f"V{i:02d}", True, False) for i in range(32))
    assert projenv_entries_error(ok) is None


# ---------------------------------------------------------------------------
# UI: ProjEnv row opens the builder by default; toggles + DSL switch
# ---------------------------------------------------------------------------


def test_projenv_row_opens_builder_by_default(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import (
            reset_launch_cwd,
            set_launch_cwd_for_tests,
        )
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import ProjectEnvBuilderScreen, StartSessionScreen

        reset_launch_cwd()
        repo = tmp_path / "proj-builder-default"
        repo.mkdir()
        _run_git(repo, ["init"])
        _run_git(repo, ["config", "user.email", "a@a.com"])
        _run_git(repo, ["config", "user.name", "A"])
        (repo / "file.py").write_text("x=1\n", encoding="utf-8")
        _run_git(repo, ["add", "."])
        _run_git(repo, ["commit", "-m", "init"])
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist-builder-default")
        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = StartSessionScreen(
                task_options=list(app.curated_task_options()),
                initial_target="local_project",
                initial_project=str(repo),
            )
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp._activate_row("project_env")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, ProjectEnvBuilderScreen)
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.1)
        reset_launch_cwd()

    _run_async(_inner())


def test_builder_toggles_and_save_round_trip(tmp_path):
    async def _inner():
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import ProjectEnvBuilderScreen
        from textual.widgets import Button, Input, Static

        app = LocalApplicationV1(history_root=tmp_path / "hist-builder-toggle")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            saved: list = []
            screen = ProjectEnvBuilderScreen(
                initial_text="FOO", on_save=lambda value: saved.append(value)
            )
            app.push_screen(screen)
            await pilot.pause()
            await asyncio.sleep(0.15)
            uid = screen._rows[0]["uid"]
            # Required toggle: required -> optional (FOO -> FOO?).
            await pilot.click(f"#projenv-req-{uid}")
            await pilot.pause()
            assert "optional" in str(
                screen.query_one(f"#projenv-req-{uid}", Button).label
            )
            # Secret toggle: inherit -> secret (FOO? -> secret:FOO?).
            await pilot.click(f"#projenv-sec-{uid}")
            await pilot.pause()
            assert "secret" in str(
                screen.query_one(f"#projenv-sec-{uid}", Button).label
            )
            await pilot.click("#projenv-save")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert saved == ["secret:FOO?"]
            # Each declaration row exposes name + required + secret affordances.
            screen2 = ProjectEnvBuilderScreen(
                initial_text="FOO, secret:DB_URL", on_save=lambda value: None
            )
            app.push_screen(screen2)
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert len(list(screen2.query(Input))) == 2
            assert len([b for b in screen2.query(Button) if "projenv-req-" in (b.id or "")]) == 2
            assert len([b for b in screen2.query(Button) if "projenv-sec-" in (b.id or "")]) == 2
            count = screen2.query_one("#projenv-count", Static)
            rendered = count.render()
            plain = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "inherit" in plain and "secret" in plain
            await pilot.press("escape")
            await pilot.pause()

    _run_async(_inner())


def test_builder_dsl_toggle_preserves_order(tmp_path):
    async def _inner():
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import ProjectEnvBuilderScreen
        from textual.widgets import Input

        app = LocalApplicationV1(history_root=tmp_path / "hist-builder-dsl")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen = ProjectEnvBuilderScreen(
                initial_text="B, secret:S1, A?, secret:S2?", on_save=lambda value: None
            )
            app.push_screen(screen)
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert [row["name"] for row in screen._rows] == ["B", "S1", "A", "S2"]
            # Builder -> DSL text preserves order and flags.
            screen._open_dsl_editor()
            await pilot.pause()
            await asyncio.sleep(0.15)
            from agentic_debugger.ui.screens import SingleLineFieldEditorScreen

            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            dsl_input = app.screen.query_one("#single-line-editor", Input)
            assert dsl_input.value == "B, secret:S1, A?, secret:S2?"
            # DSL -> builder preserves order (edit then save).
            dsl_input.value = "Z?, secret:Q, M"
            await pilot.pause()
            await pilot.click("#single-line-save-button")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, ProjectEnvBuilderScreen)
            assert [(r["name"], r["required"], r["secret"]) for r in screen._rows] == [
                ("Z", False, False),
                ("Q", True, True),
                ("M", True, False),
            ]
            await pilot.press("escape")
            await pilot.pause()

    _run_async(_inner())


def test_builder_headless_80x24_reachable():
    async def _inner():
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import ProjectEnvBuilderScreen
        from textual.widgets import Button, Input, Static

        import tempfile

        tmp = Path(tempfile.mkdtemp())
        app = LocalApplicationV1(history_root=tmp / "hist-builder-80")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen = ProjectEnvBuilderScreen(
                initial_text="FOO, BAD NAME, secret:DB_URL",
                on_save=lambda value: None,
            )
            app.push_screen(screen)
            await pilot.pause()
            await asyncio.sleep(0.2)
            # Rows/toggles/error/count all present and within width.
            assert len(list(screen.query(Input))) == 3
            err = screen.query_one("#projenv-error", Static)
            rendered = err.render()
            plain = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "row 2" in plain and "BAD NAME" in plain
            for bid in ("#projenv-add", "#projenv-dsl", "#projenv-save", "#projenv-cancel"):
                button = screen.query_one(bid, Button)
                region = button.region
                assert region.x + region.width <= 80, f"{bid} clips at 80 cols"
                assert region.y < 24, f"{bid} off-screen at 80x24"
            await pilot.press("escape")
            await pilot.pause()

    _run_async(_inner())
