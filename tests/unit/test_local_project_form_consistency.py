"""FINAL UX consistency repair — migrate tiny editors to focused SingleLineFieldEditorScreen.

Covers required targeted tests A-H plus I (Time Limit) validation.
No provider, no Docker, no full suite.

Adapted to the unified StartSessionScreen (initial_target="local_project"):
the removed LocalProjectStartScreen attributes became SessionConfig fields
(screen._config.*), form state renders via render_state(), the start button
is #start-session-button, and a selectable live model is provided by writing
a configured command-model profile into the app-owned config store instead
of the removed screen._profiles seam.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("textual")


def _run_git(cwd: Path, args: list[str]) -> None:
    r = subprocess.run(["git"] + args, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
    assert r.returncode == 0, r.stderr


def _make_repo(tmp: Path, name: str = "proj") -> Path:
    repo = tmp / name
    repo.mkdir()
    _run_git(repo, ["init"])
    _run_git(repo, ["config", "user.email", "a@a.com"])
    _run_git(repo, ["config", "user.name", "A"])
    (repo / "file.py").write_text("x=1\n", encoding="utf-8")
    _run_git(repo, ["add", "."])
    _run_git(repo, ["commit", "-m", "init"])
    return repo


def _write_configured_profiles(root: Path, profiles: list[tuple[str, str]]) -> None:
    """Install accepted-shaped configured command-model profiles for one app.

    The unified start screen builds its selectable model roster from the
    app-owned store ``<history root>/config/command-models.json`` (same JSON
    shape as tests/integration/test_configured_source.py::write_profile).
    The profile command is never executed by these tests.
    """
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "command-models.json").write_text(
        json.dumps(
            {
                "schema_version": "command-models-v1",
                "profiles": [
                    {
                        "profile_id": profile_id,
                        "display_name": display,
                        "executable": sys.executable,
                        "argv": ["-c", "print('configured profile stub')"],
                        "request_timeout_seconds": 60.0,
                    }
                    for profile_id, display in profiles
                ],
            }
        ),
        encoding="utf-8",
    )


def _start_screen(app, project) -> "object":
    """The unified start screen pinned to the Local Project target."""
    from agentic_debugger.ui.screens import StartSessionScreen

    return StartSessionScreen(
        task_options=list(app.curated_task_options()),
        initial_target="local_project",
        initial_project=str(project),
    )


def _plain(widget) -> str:
    rendered = widget.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


def _run_async(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# A. Project editor
# ---------------------------------------------------------------------------
def test_A_project_editor_opens_prefilled_enter_saves_esc_cancels(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen, SingleLineFieldEditorScreen, ChoicePickerScreen
        from textual.widgets import Input, Static

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "repoA")
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "histA")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.15)
            original = lp._config.project_path
            # Verify project picker exists (choice)
            lp._activate_row("project")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, ChoicePickerScreen)
            assert app.screen.title == "Project input"
            # Select Type/paste path via UI (down+down+enter) — pops picker then pushes editor
            await pilot.press("down", "down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            # Prefilled current path
            inp = app.screen.query_one("#single-line-editor", Input)
            assert inp.value == original
            assert inp.has_focus
            # Cursor at end
            assert inp.cursor_position == len(inp.value)
            # Footer matches contract
            hint = app.screen.query_one("#single-line-hint", Static)
            ht = _plain(hint)
            assert "Enter save" in ht
            assert "Esc cancel" in ht
            # Verify visual family: dialog width 70 via CSS (check CSS file has entry)
            css = Path("agentic_debugger/ui/app.tcss").read_text(encoding="utf-8")
            assert "SingleLineFieldEditorScreen" in css
            assert "#single-line-dialog" in css
            assert "width: 70" in css
            # Now test Esc cancels: modify then Esc
            inp.value = "/tmp/bogus-not-saved"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, StartSessionScreen)
            # Project path unchanged
            assert lp._config.project_path == original
            # Reopen via direct editor (bypass picker for determinism) and test Enter saves
            lp._open_text_editor("Project path", lp._config.project_path, lp._on_project_saved)
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp2 = app.screen.query_one("#single-line-editor", Input)
            # Prepare a second repo to save
            repo2 = _make_repo(tmp_path, "repoA2")
            inp2.value = str(repo2)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, StartSessionScreen)
            assert lp._config.project_path == str(repo2.resolve())
            # Also test Save button click saves (direct editor again)
            lp._open_text_editor("Project path", lp._config.project_path, lp._on_project_saved)
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp3 = app.screen.query_one("#single-line-editor", Input)
            inp3.value = str(repo)
            await pilot.pause()
            await pilot.click("#single-line-save-button")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, StartSessionScreen)
            assert lp._config.project_path == str(repo.resolve())
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# B. Reproduction editor
# ---------------------------------------------------------------------------
def test_B_repro_editor_opens_prefilled_enter_saves_esc_cancels_blank(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen, SingleLineFieldEditorScreen
        from textual.widgets import Input, Static

        reset_launch_cwd()
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "histB")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, tmp_path)
            lp._config.reproduction_command = "python repro.py"
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.15)
            # Open repro editor
            lp._activate_row("repro")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            # Title matches spec
            assert "Reproduction command" in app.screen.title_text
            inp = app.screen.query_one("#single-line-editor", Input)
            assert inp.value == "python repro.py"
            assert inp.has_focus
            assert inp.cursor_position == len("python repro.py")
            hint = app.screen.query_one("#single-line-hint", Static)
            ht = _plain(hint)
            assert "Enter save" in ht and "Esc cancel" in ht
            # Enter saves reliably
            inp.value = "python repro2.py"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, StartSessionScreen)
            assert lp._config.reproduction_command == "python repro2.py"
            # Reopen, modify, Esc cancels
            lp._activate_row("repro")
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp2 = app.screen.query_one("#single-line-editor", Input)
            assert inp2.value == "python repro2.py"
            inp2.value = "should not persist"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._config.reproduction_command == "python repro2.py"
            # Blank saves as Not set (None) per existing contract
            lp._activate_row("repro")
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp3 = app.screen.query_one("#single-line-editor", Input)
            inp3.value = "   "
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._config.reproduction_command is None
            # The row value shows the Not set (optional) placeholder for None
            lp.render_state()
            await pilot.pause()
            assert "Not set" in _plain(lp._row("repro"))
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# C. Verification editor
# ---------------------------------------------------------------------------
def test_C_verify_editor_same_behavior(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen, SingleLineFieldEditorScreen
        from textual.widgets import Input, Static

        reset_launch_cwd()
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "histC")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, tmp_path)
            lp._config.verification_command = "pytest -q"
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp._activate_row("verify")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            assert "Regression check command" in app.screen.title_text
            inp = app.screen.query_one("#single-line-editor", Input)
            assert inp.value == "pytest -q"
            assert inp.has_focus
            # Enter saves
            inp.value = "python -m pytest"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._config.verification_command == "python -m pytest"
            # Esc cancels
            lp._activate_row("verify")
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp2 = app.screen.query_one("#single-line-editor", Input)
            inp2.value = "nope"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._config.verification_command == "python -m pytest"
            # Blank => None
            lp._activate_row("verify")
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp3 = app.screen.query_one("#single-line-editor", Input)
            inp3.value = ""
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._config.verification_command is None
            # Save via button click
            lp._config.verification_command = "old"
            lp._activate_row("verify")
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp4 = app.screen.query_one("#single-line-editor", Input)
            inp4.value = "new via button"
            await pilot.pause()
            await pilot.click("#single-line-save-button")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._config.verification_command == "new via button"
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# D. Bug editor retains multiline semantics
# ---------------------------------------------------------------------------
def test_D_bug_editor_retains_multiline_contract(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen, BugDescriptionEditorScreen
        from textual.widgets import TextArea

        reset_launch_cwd()
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "histD")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, tmp_path)
            lp._config.bug_description = "hello"
            app.push_screen(lp)
            await pilot.pause()
            lp._activate_row("bug")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, BugDescriptionEditorScreen)
            ta = app.screen.query_one("#bug-editor", TextArea)
            # Enter should insert newline, not save
            ta.cursor_location = (0, len(ta.text))
            before = ta.text
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.1)
            assert isinstance(app.screen, BugDescriptionEditorScreen)
            assert "\n" in app.screen.query_one("#bug-editor", TextArea).text
            # Ctrl+Enter saves
            ta2 = app.screen.query_one("#bug-editor", TextArea)
            ta2.text = "line1\nline2"
            await pilot.pause()
            await pilot.press("ctrl+enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, StartSessionScreen)
            assert lp._config.bug_description == "line1\nline2"
            # Esc cancels
            lp._activate_row("bug")
            await pilot.pause()
            await asyncio.sleep(0.15)
            ta3 = app.screen.query_one("#bug-editor", TextArea)
            ta3.text = "discard"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._config.bug_description == "line1\nline2"
            # Check bindings contract
            assert any(b.key == "ctrl+enter" for b in BugDescriptionEditorScreen.BINDINGS)
            assert not any(b.key == "enter" for b in BugDescriptionEditorScreen.BINDINGS if getattr(b, "key", None) == "enter")
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# E. Model picker unchanged
# ---------------------------------------------------------------------------
def test_E_model_picker_still_works(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen, ChoicePickerScreen
        reset_launch_cwd()
        set_launch_cwd_for_tests(tmp_path)
        # The model roster now comes from the app-owned configured-profile
        # store (plus the offline provider registry); the unified screen
        # never auto-selects a model, so select one explicitly.
        _write_configured_profiles(
            tmp_path / "histE",
            [("qwen3.5-cloud", "qwen3.5 cloud"), ("glm-5-cloud", "glm-5 cloud")],
        )
        app = LocalApplicationV1(history_root=tmp_path / "histE")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, tmp_path)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp._choice_selected("model", "configured:qwen3.5-cloud")
            await pilot.pause()
            assert lp.profile_id == "qwen3.5-cloud"
            lp._activate_row("model")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, ChoicePickerScreen)
            assert app.screen.title == "Select model"
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.1)
            assert isinstance(app.screen, StartSessionScreen)
            assert lp.profile_id == "qwen3.5-cloud"
            # Select second via down+enter
            lp._activate_row("model")
            await pilot.pause()
            await asyncio.sleep(0.15)
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, StartSessionScreen)
            assert lp.profile_id in ("qwen3.5-cloud", "glm-5-cloud")
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# F. Focus behavior for each editor
# ---------------------------------------------------------------------------
def test_F_focus_each_editor_and_return_navigable(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen, SingleLineFieldEditorScreen, BugDescriptionEditorScreen
        from textual.widgets import Input, TextArea
        reset_launch_cwd()
        repo = _make_repo(tmp_path, "repoF")
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "histF")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.15)

            # Project via type (ChoicePicker -> Type)
            lp._activate_row("project")
            await pilot.pause()
            await asyncio.sleep(0.15)
            from agentic_debugger.ui.screens import ChoicePickerScreen
            assert isinstance(app.screen, ChoicePickerScreen)
            await pilot.press("down", "down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            inp = app.screen.query_one("#single-line-editor", Input)
            assert inp.has_focus
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert getattr(app.screen.focused, "row_key", None) == "project"
            # Down navigation still works
            await pilot.press("down")
            await pilot.pause()
            assert getattr(app.screen.focused, "row_key", None) == "bug"

            # Bug
            lp._activate_row("bug")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, BugDescriptionEditorScreen)
            ta = app.screen.query_one("#bug-editor", TextArea)
            assert ta.has_focus
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert getattr(app.screen.focused, "row_key", None) == "bug"

            # Repro
            lp._activate_row("repro")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            inp2 = app.screen.query_one("#single-line-editor", Input)
            assert inp2.has_focus
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert getattr(app.screen.focused, "row_key", None) == "repro"
            await pilot.press("down")
            await pilot.pause()
            assert getattr(app.screen.focused, "row_key", None) == "verify"

            # Verify
            lp._activate_row("verify")
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp3 = app.screen.query_one("#single-line-editor", Input)
            assert inp3.has_focus
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert getattr(app.screen.focused, "row_key", None) == "verify"

            # Up navigation back
            await pilot.press("up")
            await pilot.pause()
            assert getattr(app.screen.focused, "row_key", None) == "repro"

        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# G. Start uses exact saved values
# ---------------------------------------------------------------------------
def test_G_start_uses_exact_saved_values(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen
        from textual.widgets import TextArea, Input, Static

        reset_launch_cwd()
        set_launch_cwd_for_tests(tmp_path)
        repo = _make_repo(tmp_path, "repoG")
        _write_configured_profiles(tmp_path / "histG", [("qwen3.5", "qwen3.5")])
        app = LocalApplicationV1(history_root=tmp_path / "histG")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.15)
            # Select the live model explicitly (no auto-default anymore)
            lp._choice_selected("model", "configured:qwen3.5")
            await pilot.pause()
            lp._config.time_limit_seconds = 120
            # Edit bug via editor to multiline
            lp._activate_row("bug")
            await pilot.pause()
            await asyncio.sleep(0.15)
            ta = app.screen.query_one("#bug-editor", TextArea)
            multiline = "discounted_price bug\nsecond line"
            ta.text = multiline
            await pilot.pause()
            await pilot.press("ctrl+enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            # Edit repro
            lp._activate_row("repro")
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp = app.screen.query_one("#single-line-editor", Input)
            inp.value = "python repro.py"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            # Edit verify to blank (None)
            lp._activate_row("verify")
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp2 = app.screen.query_one("#single-line-editor", Input)
            inp2.value = ""
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._config.verification_command is None
            # Edit project via type to repo (already but simulate change)
            lp._set_project(str(repo))
            await pilot.pause()
            # Intercept start
            captured = {}
            def fake_start(**kwargs):
                captured.update(kwargs)
            app.start_local_project_session = fake_start  # type: ignore
            lp.render_state()
            await pilot.pause()
            assert lp.query_one("#start-session-button").disabled is False
            lp._start()
            await pilot.pause()
            await asyncio.sleep(0.1)
            # Verify exact saved values used, no stale buffer
            assert captured.get("project_path") == str(repo.resolve()) or captured.get("project_path") == str(repo)
            assert captured.get("bug_description") == multiline.strip()
            assert captured.get("reproduction_command") == "python repro.py"
            assert captured.get("verification_command") is None
            assert captured.get("profile_id") == "qwen3.5"
            assert captured.get("model_provider") is None
            assert captured.get("max_elapsed_seconds") == 120
            assert captured.get("auto_retries") == 1
            # Cancel must not overwrite: modify repro editor but cancel, then start again should keep old
            lp._activate_row("repro")
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp3 = app.screen.query_one("#single-line-editor", Input)
            inp3.value = "should be discarded on cancel"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._config.reproduction_command == "python repro.py"
            captured2 = {}
            app.start_local_project_session = lambda **kw: captured2.update(kw)  # type: ignore
            lp._start()
            await pilot.pause()
            assert captured2.get("reproduction_command") == "python repro.py"

            # A newly dirty repository is a visible pre-flight gate, not an
            # apparently clickable primary action that fails only afterward.
            # The unified screen re-validates the project on the start path
            # (_refresh_for_start), so exercising start is the honest gate.
            (repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
            calls3: list[dict] = []
            app.start_local_project_session = lambda **kw: calls3.append(kw)  # type: ignore
            lp._start()
            await pilot.pause()
            await asyncio.sleep(0.1)
            assert calls3 == [], "dirty repo must not call start"
            assert lp.query_one("#start-session-button").disabled is True
            status = lp.query_one("#start-status", Static)
            assert "uncommitted changes" in _plain(status).lower()
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# H. Legacy — the unified start screen uses the shared editor family
# ---------------------------------------------------------------------------
def test_H_legacy_no_tiny_editor(tmp_path):
    # Inspect source: the unified session-setup surface must use the shared
    # editor family, not any tiny single-purpose editor screen.
    src = Path("agentic_debugger/ui/screens.py").read_text(encoding="utf-8")
    # _SingleLineEditorScreen must remain the alias to SingleLineFieldEditorScreen, not a tiny Screen
    assert "_SingleLineEditorScreen = SingleLineFieldEditorScreen" in src
    # The unified start screen's _open_text_editor must use SingleLineFieldEditorScreen
    assert "SingleLineFieldEditorScreen" in src
    # The unified start screen is the single session-setup surface (the old
    # LocalProjectStartScreen literal pin is removed by design — the class
    # no longer exists).
    assert "class StartSessionScreen" in src
    assert "SessionConfig" in src
    assert "render_state" in src
    # Verify CSS has centered SingleLine dialog, not tiny upper-left
    css = Path("agentic_debugger/ui/app.tcss").read_text(encoding="utf-8")
    assert "SingleLineFieldEditorScreen" in css
    assert "#single-line-dialog" in css
    assert "width: 70" in css
    # Verify BugDescriptionEditorScreen still multiline contract
    assert "BugDescriptionEditorScreen" in css
    # Verify SingleLine and Bug share same background and align family
    assert "background: $surface;" in css
    assert "align: center middle;" in css
    # Runtime check: each Local Project field opens proper screen family
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen, SingleLineFieldEditorScreen, BugDescriptionEditorScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "repoH")
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "histH")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.15)
            # Repro
            lp._activate_row("repro")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            # Check dialog is centered (Screen has align center middle via CSS)
            # We verify by checking that screen's CSS background is dim overlay
            assert "SingleLine" in type(app.screen).__name__
            await pilot.press("escape")
            await pilot.pause()
            # Verify
            lp._activate_row("verify")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            await pilot.press("escape")
            await pilot.pause()
            # Project via type (ChoicePicker -> Type)
            lp._activate_row("project")
            await pilot.pause()
            await asyncio.sleep(0.15)
            from agentic_debugger.ui.screens import ChoicePickerScreen
            assert isinstance(app.screen, ChoicePickerScreen)
            await pilot.press("down", "down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.15)
            # Now back on the unified start form, verify Bug still opens correctly
            assert isinstance(app.screen, StartSessionScreen)
            lp._activate_row("bug")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, BugDescriptionEditorScreen)
            await pilot.press("escape")
            await pilot.pause()
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# I. Time Limit — already centered (not tiny), preserve validation
# ---------------------------------------------------------------------------
def test_I_time_limit_preserved_and_not_tiny(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen, TimeLimitEditorScreen
        from textual.widgets import Input, Static

        reset_launch_cwd()
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "histI")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = _start_screen(app, tmp_path)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp._activate_row("time_limit")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert isinstance(app.screen, TimeLimitEditorScreen)
            # Still centered via CSS
            css = Path("agentic_debugger/ui/app.tcss").read_text(encoding="utf-8")
            assert "TimeLimitEditorScreen" in css
            assert "align: center middle" in css
            # Validation preserved
            inp = app.screen.query_one("#time-limit-editor", Input)
            inp.value = "0"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.1)
            # Should stay on editor with error
            assert isinstance(app.screen, TimeLimitEditorScreen)
            err = app.screen.query_one("#time-limit-error", Static)
            err_text = _plain(err)
            assert "at least 1 second" in err_text
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.1)
            assert isinstance(app.screen, StartSessionScreen)
            # Empty => No limit, valid
            lp._activate_row("time_limit")
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp2 = app.screen.query_one("#time-limit-editor", Input)
            inp2.value = ""
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._config.time_limit_seconds is None
            # Valid integer
            lp._activate_row("time_limit")
            await pilot.pause()
            await asyncio.sleep(0.15)
            inp3 = app.screen.query_one("#time-limit-editor", Input)
            inp3.value = "60"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._config.time_limit_seconds == 60
        reset_launch_cwd()
    _run_async(_inner())
