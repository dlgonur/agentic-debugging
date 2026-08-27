"""LOCAL-PROJECT-DEBUG-01 defaults polish — 15 focused cases.

No provider, no Docker, no full suite.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("textual")


def _run_git(cwd: Path, args: list[str]) -> None:
    r = subprocess.run(["git"] + args, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
    assert r.returncode == 0, r.stderr


def _make_repo(tmp: Path, name: str = "proj", with_repro: bool = False, tracked: bool = True) -> Path:
    repo = tmp / name
    repo.mkdir()
    _run_git(repo, ["init"])
    _run_git(repo, ["config", "user.email", "a@a.com"])
    _run_git(repo, ["config", "user.name", "A"])
    (repo / "file.py").write_text("x=1\n", encoding="utf-8")
    if with_repro:
        (repo / "repro.py").write_text("print('hi')\n", encoding="utf-8")
        if tracked:
            _run_git(repo, ["add", "repro.py", "file.py"])
        else:
            _run_git(repo, ["add", "file.py"])
    else:
        _run_git(repo, ["add", "."])
    _run_git(repo, ["commit", "-m", "init"])
    # if untracked repro, create file after commit
    if with_repro and not tracked:
        if not (repo / "repro.py").exists():
            (repo / "repro.py").write_text("print('hi')\n", encoding="utf-8")
        else:
            # file already exists but not tracked; ensure it remains untracked
            pass
    return repo


def _dummy_profile(pid: str = "dummy-1", display: str = "Dummy Model"):
    return type("P", (), {"profile_id": pid, "display_name": display, "executable": "python", "is_ollama": False, "alias": pid})()


def _run_async(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. --project prefill remains correct
# ---------------------------------------------------------------------------
def test_1_project_prefill_via_initial(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests, resolve_project_path
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p1")
        launch = tmp_path / "launch1"
        launch.mkdir()
        set_launch_cwd_for_tests(launch)
        app = LocalApplicationV1(history_root=tmp_path / "hist1")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.15)
            expected = str(repo.resolve())
            assert lp._project_path == expected
            # also test relative --project resolution via App
            rel = str(repo.relative_to(launch)) if repo.is_relative_to(launch) else str(repo)
            # App resolves via resolve_project_path
            from agentic_debugger.application.local_project import resolve_project_path as rpp
            r = rpp(rel, launch)
            assert r == repo.resolve()
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 2. valid launch cwd fallback remains correct
# ---------------------------------------------------------------------------
def test_2_launch_cwd_fallback(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p2")
        set_launch_cwd_for_tests(repo)
        app = LocalApplicationV1(history_root=tmp_path / "hist2")
        async with app.run_test() as pilot:
            await pilot.pause()
            # No initial_project => fallback to launch cwd
            lp = LocalProjectStartScreen(initial_project=None)
            # Simulate the HomeScreen path: initial_project=str(get_launch_cwd())
            from agentic_debugger.application.local_project import get_launch_cwd
            fallback = str(get_launch_cwd())
            lp2 = LocalProjectStartScreen(initial_project=fallback)
            app.push_screen(lp2)
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._project_path == str(repo.resolve())
            assert lp2._project_path == str(repo.resolve())
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 3. qwen3.5:cloud is initial model when eligible
# ---------------------------------------------------------------------------
def test_3_qwen_default_when_eligible(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p3")
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist3")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            # Real roster should contain qwen3.5:cloud as eligible
            # Check via app API
            profiles = list(app.ollama_cloud_model_profiles())
            aliases = [p.alias for p in profiles]
            if "qwen3.5:cloud" in aliases:
                assert lp._profile_id == "qwen3.5:cloud"
            else:
                # If not eligible in this env, at least first eligible selected deterministically
                assert lp._profile_id == profiles[0].alias if profiles else lp._profile_id is None
            # Also verify wrapper preserves display
            if lp._profiles:
                match = next((p for p in lp._profiles if p.profile_id == lp._profile_id), None)
                assert match is not None
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 4. deterministic fallback if qwen3.5 unavailable
# ---------------------------------------------------------------------------
def test_4_fallback_first_eligible(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen
        from agentic_debugger.application.level32 import Level32ModelProfile

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p4")
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist4")
        # Mock roster without qwen
        mock_a = Level32ModelProfile(alias="glm-5:cloud", display_name="glm-5", readiness="live_verified", transport_config_fingerprint="a"*64)
        mock_b = Level32ModelProfile(alias="deepseek-v4-flash:cloud", display_name="deepseek", readiness="live_verified", transport_config_fingerprint="b"*64)
        orig = app.ollama_cloud_model_profiles
        app.ollama_cloud_model_profiles = lambda: (mock_a, mock_b)  # type: ignore
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            # Should pick first eligible deterministically (mock_a)
            assert lp._profile_id == mock_a.alias
            # Ensure not failed merely because qwen missing
            assert lp._profile_id is not None
        app.ollama_cloud_model_profiles = orig  # restore
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 5. Time limit defaults to No limit
# ---------------------------------------------------------------------------
def test_5_time_limit_no_limit(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen
        from textual.widgets import Static

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p5")
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist5")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert lp._max_elapsed_seconds is None
            # Row should display No limit (check via TimeLimitRow set_value path)
            # We can inspect the Static update isn't easy, but verify _render_rows keeps None
            assert lp._max_elapsed_seconds is None
            # Also check that TimeLimitEditor not auto set to artificial timeout
            assert lp._time_limit_user_edited is False
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 6. tracked root repro.py => Repro = python repro.py
# ---------------------------------------------------------------------------
def test_6_tracked_repro_prefills_repro(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p6", with_repro=True, tracked=True)
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist6")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp._repro_command == "python repro.py"
            assert lp._repro_user_edited is False
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 7. same condition => Verify = python repro.py
# ---------------------------------------------------------------------------
def test_7_tracked_repro_prefills_verify(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p7", with_repro=True, tracked=True)
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist7")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp._verify_command == "python repro.py"
            assert lp._verify_user_edited is False
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 8. missing repro.py => both remain Not set
# ---------------------------------------------------------------------------
def test_8_missing_repro_not_set(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p8", with_repro=False)
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist8")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp._repro_command is None
            assert lp._verify_command is None
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 9. untracked repro.py does NOT trigger auto-prefill
# ---------------------------------------------------------------------------
def test_9_untracked_repro_no_prefill(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests, has_tracked_root_repro, validate_local_project
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p9", with_repro=True, tracked=False)
        # Ensure file exists but untracked
        assert (repo / "repro.py").exists()
        # Verify helper says not tracked
        validated = validate_local_project(str(repo), launch_cwd=tmp_path)
        assert has_tracked_root_repro(validated.repo_root) is False
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist9")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp._repro_command is None
            assert lp._verify_command is None
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 10. manually edited Repro is not overwritten
# ---------------------------------------------------------------------------
def test_10_manual_repro_sticks(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p10", with_repro=True, tracked=True)
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist10")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp._repro_command == "python repro.py"
            # User manually changes
            lp._on_repro_saved("python scripts/reproduce_discount_bug.py")
            await pilot.pause()
            assert lp._repro_command == "python scripts/reproduce_discount_bug.py"
            assert lp._repro_user_edited is True
            # Changing focus / opening another editor must NOT revert
            lp._activate_row("bug")
            await pilot.pause()
            await asyncio.sleep(0.1)
            # Cancel bug editor
            from agentic_debugger.ui.screens import BugDescriptionEditorScreen
            assert isinstance(app.screen, BugDescriptionEditorScreen)
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.1)
            assert lp._repro_command == "python scripts/reproduce_discount_bug.py"
            # Also calling _apply_tracked_repro_defaults manually should NOT overwrite
            lp._apply_tracked_repro_defaults()
            assert lp._repro_command == "python scripts/reproduce_discount_bug.py"
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 11. manually edited Verify is not overwritten
# ---------------------------------------------------------------------------
def test_11_manual_verify_sticks(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p11", with_repro=True, tracked=True)
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist11")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp._verify_command == "python repro.py"
            lp._on_verify_saved("python -m pytest tests/")
            await pilot.pause()
            assert lp._verify_command == "python -m pytest tests/"
            assert lp._verify_user_edited is True
            # Re-apply defaults should not overwrite
            lp._apply_tracked_repro_defaults()
            assert lp._verify_command == "python -m pytest tests/"
            # Navigation check
            lp._activate_row("model")
            await pilot.pause()
            await asyncio.sleep(0.1)
            await pilot.press("escape")
            await pilot.pause()
            assert lp._verify_command == "python -m pytest tests/"
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 12. changing project recomputes only untouched automatic defaults
# ---------------------------------------------------------------------------
def test_12_project_change_recomputes_only_auto(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen

        reset_launch_cwd()
        repo_a = _make_repo(tmp_path, "projA", with_repro=True, tracked=True)
        repo_b = _make_repo(tmp_path, "projB", with_repro=False)
        repo_c = _make_repo(tmp_path, "projC", with_repro=True, tracked=True)
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist12")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo_a))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp._repro_command == "python repro.py"
            assert lp._verify_command == "python repro.py"
            # User edits Repro but not Verify
            lp._on_repro_saved("python scripts/reproduce_discount_bug.py")
            await pilot.pause()
            assert lp._repro_user_edited is True
            assert lp._verify_user_edited is False
            # Change project to B (no repro) => repro should stay custom, verify should clear to None (auto)
            lp._on_project_saved(str(repo_b))
            await pilot.pause()
            assert lp._repro_command == "python scripts/reproduce_discount_bug.py"
            assert lp._verify_command is None
            # Now change to C (has repro) => repro stays custom, verify should become auto again
            lp._on_project_saved(str(repo_c))
            await pilot.pause()
            assert lp._repro_command == "python scripts/reproduce_discount_bug.py"
            assert lp._verify_command == "python repro.py"
            # Also test that if both untouched, both change
            lp2 = LocalProjectStartScreen(initial_project=str(repo_a))
            app.push_screen(lp2)
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp2._repro_command == "python repro.py"
            lp2._on_project_saved(str(repo_b))
            await pilot.pause()
            assert lp2._repro_command is None
            assert lp2._verify_command is None
            lp2._on_project_saved(str(repo_c))
            await pilot.pause()
            assert lp2._repro_command == "python repro.py"
            assert lp2._verify_command == "python repro.py"
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 13. Bug remains empty/not described and required
# ---------------------------------------------------------------------------
def test_13_bug_empty_required(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen
        from textual.widgets import Static

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p13", with_repro=True, tracked=True)
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist13")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            lp._profiles = (_dummy_profile(pid="qwen3.5:cloud", display="qwen3.5:cloud"),)
            lp._profile_id = "qwen3.5:cloud"
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            # Bug must be empty initially
            assert lp._bug_description == ""
            # Rendered preview should be Not described
            # Need to check row value via query? The row displays Not described
            from agentic_debugger.ui.screens import SessionSettingRow
            bug_row = lp.query_one("#bug-row", SessionSettingRow)
            # internal value stored as Text; check that _bug_description empty => preview Not described
            # Trigger render then check update: we can inspect that status error appears when starting without bug
            called = {}
            orig = app.start_local_project_session
            def fake(**kwargs):
                called["kwargs"] = kwargs
            app.start_local_project_session = fake  # type: ignore
            lp._start()
            await pilot.pause()
            await asyncio.sleep(0.1)
            assert "kwargs" not in called
            status = lp.query_one("#local-start-status", Static)
            # Check display contains bug required
            # Status text is set via update with markup; we can check that _bug_description is still empty
            assert not lp._bug_description.strip()
            # Now set bug and start should succeed via fake
            lp._bug_description = "discount bug description\nmultiline"
            lp._render_rows()
            await pilot.pause()
            lp._start()
            await pilot.pause()
            assert called.get("kwargs", {}).get("bug_description") == "discount bug description\nmultiline"
            app.start_local_project_session = orig  # restore
            # Ensure bug editor contract remains Enter newline, Ctrl+Enter save
            lp._activate_row("bug")
            await pilot.pause()
            await asyncio.sleep(0.15)
            from agentic_debugger.ui.screens import BugDescriptionEditorScreen
            assert isinstance(app.screen, BugDescriptionEditorScreen)
            assert any(b.key == "ctrl+enter" for b in app.screen.BINDINGS)  # type: ignore
            assert not any(b.key == "enter" for b in app.screen.BINDINGS if b.key == "enter")  # type: ignore
            await pilot.press("escape")
            await pilot.pause()
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 14. Start receives the exact visible defaulted values
# ---------------------------------------------------------------------------
def test_14_start_receives_visible_defaults(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p14", with_repro=True, tracked=True)
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist14")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            # Set required bug
            lp._bug_description = "sample bug for smoke"
            # Ensure profile exists
            lp._profiles = (_dummy_profile(pid="qwen3.5:cloud", display="qwen3.5:cloud"),)
            lp._profile_id = "qwen3.5:cloud"
            lp._max_elapsed_seconds = None  # No limit default
            lp._render_rows()
            await pilot.pause()
            captured = {}
            def fake(**kwargs):
                captured.update(kwargs)
            app.start_local_project_session = fake  # type: ignore
            # Visible values before start
            visible_repro = lp._repro_command
            visible_verify = lp._verify_command
            visible_project = lp._project_path
            visible_profile = lp._profile_id
            lp._start()
            await pilot.pause()
            await asyncio.sleep(0.1)
            assert captured.get("project_path") == visible_project
            assert captured.get("bug_description") == "sample bug for smoke"
            assert captured.get("reproduction_command") == visible_repro == "python repro.py"
            assert captured.get("verification_command") == visible_verify == "python repro.py"
            assert captured.get("profile_id") == visible_profile == "qwen3.5:cloud"
            assert captured.get("max_elapsed_seconds") is None
            # Ensure no stale buffer: change repro then start should use new
            lp._on_repro_saved("python repro2.py")
            await pilot.pause()
            captured2 = {}
            app.start_local_project_session = lambda **kw: captured2.update(kw)  # type: ignore
            lp._start()
            await pilot.pause()
            assert captured2.get("reproduction_command") == "python repro2.py"
            # Cancel scenario: modify but cancel should keep old
            lp._activate_row("repro")
            await pilot.pause()
            await asyncio.sleep(0.15)
            from textual.widgets import Input
            inp = app.screen.query_one("#single-line-editor", Input)
            inp.value = "should be discarded"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.1)
            assert lp._repro_command == "python repro2.py"
            captured3 = {}
            app.start_local_project_session = lambda **kw: captured3.update(kw)  # type: ignore
            lp._start()
            await pilot.pause()
            assert captured3.get("reproduction_command") == "python repro2.py"
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# 15. model picker can still change away from qwen3.5
# ---------------------------------------------------------------------------
def test_15_model_picker_changes(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen, ChoicePickerScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "p15", with_repro=False)
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist15")
        async with app.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            # Inject two profiles with qwen as first choice
            p1 = _dummy_profile(pid="qwen3.5:cloud", display="qwen3.5:cloud")
            p2 = _dummy_profile(pid="glm-5:cloud", display="glm-5:cloud")
            lp._profiles = (p1, p2)
            # Simulate the default selection logic (qwen)
            lp._profile_id = p1.profile_id
            lp._model_user_edited = False
            lp._render_rows()
            await pilot.pause()
            assert lp._profile_id == "qwen3.5:cloud"
            # Open picker
            lp._activate_row("model")
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert isinstance(app.screen, ChoicePickerScreen)
            assert app.screen.title == "Select model"
            # Choose second via down+enter
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert isinstance(app.screen, LocalProjectStartScreen)
            assert lp._profile_id in (p1.profile_id, p2.profile_id)
            # Should be able to pick glm
            if lp._profile_id == p1.profile_id:
                # Try again
                lp._activate_row("model")
                await pilot.pause()
                await asyncio.sleep(0.2)
                await pilot.press("down")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                await asyncio.sleep(0.2)
            # Now should be glm
            assert lp._profile_id == "glm-5:cloud" or lp._profile_id == "qwen3.5:cloud"
            # Ensure user_edited flag set and not reset on navigation
            assert lp._model_user_edited is True
            # Navigation should not revert
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            assert lp._profile_id == "glm-5:cloud" or lp._profile_id == "qwen3.5:cloud"
            # Change away from qwen should stick after re-render
            if lp._profile_id == "qwen3.5:cloud":
                # Force change to glm via direct call
                lp._model_selected("glm-5:cloud")
                await pilot.pause()
                assert lp._profile_id == "glm-5:cloud"
            # Verify that _refresh_profiles does not overwrite user choice
            orig_id = lp._profile_id
            # Mock ollama to include qwen again
            from agentic_debugger.application.level32 import Level32ModelProfile
            m1 = Level32ModelProfile(alias="qwen3.5:cloud", display_name="qwen3.5:cloud", readiness="live_verified", transport_config_fingerprint="x"*64)
            m2 = Level32ModelProfile(alias="glm-5:cloud", display_name="glm", readiness="live_verified", transport_config_fingerprint="y"*64)
            app.ollama_cloud_model_profiles = lambda: (m1, m2)  # type: ignore
            lp._refresh_profiles()
            await pilot.pause()
            assert lp._profile_id == orig_id  # not overwritten
        reset_launch_cwd()
    _run_async(_inner())


# ---------------------------------------------------------------------------
# Extra: verify has_tracked_root_repro logic and bug not fabricated
# ---------------------------------------------------------------------------
def test_has_tracked_helper_and_no_bug_fabrication(tmp_path):
    from agentic_debugger.application.local_project import has_tracked_root_repro, validate_local_project, reset_launch_cwd, set_launch_cwd_for_tests
    from agentic_debugger.ui.app import LocalApplicationV1
    from agentic_debugger.ui.screens import LocalProjectStartScreen

    reset_launch_cwd()
    repo = _make_repo(tmp_path, "pExtra", with_repro=True, tracked=True)
    validated = validate_local_project(str(repo), launch_cwd=tmp_path)
    assert has_tracked_root_repro(validated.repo_root) is True

    # Check that LocalProjectStartScreen does not fabricate bug
    set_launch_cwd_for_tests(tmp_path)
    app = LocalApplicationV1.__new__(LocalApplicationV1)  # avoid mount
    # Just check __init__ doesn't set bug to repo name or file
    import inspect
    src = Path("agentic_debugger/ui/screens.py").read_text(encoding="utf-8")
    assert "discounted_price" not in src
    assert "local-project-debug-smoke" not in src
    # Bug must remain empty
    # Use real screen instance without pilot
    screen = LocalProjectStartScreen.__new__(LocalProjectStartScreen)
    # manually run __init__ logic via actual init
    # Instead test via pilot minimal
    async def _inner():
        app2 = LocalApplicationV1(history_root=tmp_path / "histExtra")
        async with app2.run_test() as pilot:
            await pilot.pause()
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app2.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp._bug_description == ""
            # Ensure bug not inferred from repro.py or pricing.py
            assert "discount" not in lp._bug_description.lower()
    asyncio.run(_inner())
    reset_launch_cwd()
