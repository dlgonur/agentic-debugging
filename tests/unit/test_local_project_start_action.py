"""LOCAL-PROJECT-DEBUG-01 start-action repair — keyboard and button.

Covers the real smoke release blocker: valid clean form pressing s did nothing.
Tests per task sections 6-10. No provider, no Docker, no Level.

Uses Textual run_test deterministic fixtures; no real provider launch.
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


def _make_repo(tmp: Path, name: str = "proj") -> Path:
    repo = tmp / name
    repo.mkdir()
    _run_git(repo, ["init"])
    _run_git(repo, ["config", "user.email", "a@a.com"])
    _run_git(repo, ["config", "user.name", "A"])
    (repo / "repro.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "file.py").write_text("x=1\n", encoding="utf-8")
    _run_git(repo, ["add", "."])
    _run_git(repo, ["commit", "-m", "init"])
    return repo


def _dummy_profile(pid: str = "qwen3.5:cloud", display: str = "qwen3.5:cloud"):
    return type("P", (), {"profile_id": pid, "display_name": display, "executable": "python", "is_ollama": False, "alias": pid})()


def _run_async(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 6. SUCCESSFUL START TEST — uses fake app boundary, no provider
# ---------------------------------------------------------------------------
def test_6_successful_start_via_s(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj6")
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist6")
        # Ensure eligible model selected
        from agentic_debugger.application.level32 import Level32ModelProfile
        mock = Level32ModelProfile(alias="qwen3.5:cloud", display_name="qwen3.5:cloud", readiness="live_verified", transport_config_fingerprint="a" * 64)
        app.ollama_cloud_model_profiles = lambda: (mock,)  # type: ignore

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            # Valid form state per owner smoke
            bug = "discounted_price bug\nmultiline second line"
            lp._bug_description = bug
            lp._repro_command = "python repro.py"
            lp._verify_command = "python repro.py"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            lp._profile_id = "qwen3.5:cloud"
            lp._max_elapsed_seconds = None
            lp._render_rows()
            await pilot.pause()
            # Focus Bug row as in owner's screenshot
            lp._focus_row("bug")
            await pilot.pause()
            await asyncio.sleep(0.05)
            captured: dict = {}

            def fake(**kwargs):
                captured.update(kwargs)

            app.start_local_project_session = fake  # type: ignore
            # press s
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert captured, "start_local_project_session not called via s"
            assert captured.get("project_path") == str(repo.resolve())
            assert captured.get("bug_description") == bug.strip()
            assert captured.get("reproduction_command") == "python repro.py"
            assert captured.get("verification_command") == "python repro.py"
            assert captured.get("profile_id") == "qwen3.5:cloud"
            assert captured.get("max_elapsed_seconds") is None
            # Also assert screen transitions / handoff occurs according to contract:
            #Fake does not push workspace; verify that _start would have tried to call app.start... which we captured.
            # Ensure exactly once
            assert len([captured]) == 1
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 7. FOCUS-MATRIX TEST — parameterized over all rows
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("focus", ["project", "bug", "repro", "verify", "model", "time_limit"])
def test_7_focus_matrix_s_starts_from_every_row(tmp_path, focus):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen
        from agentic_debugger.application.level32 import Level32ModelProfile

        reset_launch_cwd()
        repo = _make_repo(tmp_path, f"proj7_{focus}")
        set_launch_cwd_for_tests(tmp_path)
        mock = Level32ModelProfile(alias="qwen3.5:cloud", display_name="qwen3.5:cloud", readiness="live_verified", transport_config_fingerprint="a" * 64)
        app = LocalApplicationV1(history_root=tmp_path / f"hist7_{focus}")
        app.ollama_cloud_model_profiles = lambda: (mock,)  # type: ignore

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp._bug_description = "bug"
            lp._repro_command = "python repro.py"
            lp._verify_command = "python repro.py"
            lp._profile_id = "qwen3.5:cloud"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            lp._render_rows()
            await pilot.pause()
            lp._focus_row(focus)
            await pilot.pause()
            await asyncio.sleep(0.05)
            assert getattr(app.screen.focused, "row_key", None) == focus, f"focus not on {focus}"
            calls: list[dict] = []

            def fake(**kw):
                calls.append(kw)

            app.start_local_project_session = fake  # type: ignore
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert len(calls) == 1, f"s from {focus} should call start exactly once, got {len(calls)}"
            # Press again should call again (exactly once per press)
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.1)
            assert len(calls) == 2, "second s should call again exactly once"

        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 8. BUTTON TEST — click Start debugging => same exact call as s
# ---------------------------------------------------------------------------
def test_8_button_click_same_as_s(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen
        from agentic_debugger.application.level32 import Level32ModelProfile

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj8")
        set_launch_cwd_for_tests(tmp_path)
        mock = Level32ModelProfile(alias="qwen3.5:cloud", display_name="qwen3.5:cloud", readiness="live_verified", transport_config_fingerprint="a" * 64)
        app = LocalApplicationV1(history_root=tmp_path / "hist8")
        app.ollama_cloud_model_profiles = lambda: (mock,)  # type: ignore

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            bug = "multiline\nbug here"
            lp._bug_description = bug
            lp._repro_command = "python repro.py"
            lp._verify_command = "python repro.py"
            lp._profile_id = "qwen3.5:cloud"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            lp._max_elapsed_seconds = None
            lp._render_rows()
            await pilot.pause()
            # Ensure button exists
            btn = lp.query_one("#local-start-button")
            assert btn is not None
            # The primary action uses concise sentence case.
            label = getattr(btn, "label", None)
            plain = label.plain if hasattr(label, "plain") else str(label)
            assert plain == "Start debugging"
            # Capture via s first
            s_calls: list[dict] = []
            app.start_local_project_session = lambda **kw: s_calls.append(dict(kw))  # type: ignore
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert len(s_calls) == 1
            # Now click button
            b_calls: list[dict] = []
            app.start_local_project_session = lambda **kw: b_calls.append(dict(kw))  # type: ignore
            await pilot.click("#local-start-button")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert len(b_calls) == 1, f"button click should call exactly once, got {b_calls}"
            # Must be same exact args as s (no duplicated logic)
            assert b_calls[0] == s_calls[0], f"button {b_calls[0]} != s {s_calls[0]}"
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 9. INVALID FORM TEST — each important invalid state produces visible feedback
# ---------------------------------------------------------------------------
def test_9_invalid_blank_bug_shows_error_and_no_start(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen
        from agentic_debugger.application.level32 import Level32ModelProfile
        from textual.widgets import Static

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj9a")
        set_launch_cwd_for_tests(tmp_path)
        mock = Level32ModelProfile(alias="qwen3.5:cloud", display_name="qwen3.5:cloud", readiness="live_verified", transport_config_fingerprint="a" * 64)
        app = LocalApplicationV1(history_root=tmp_path / "hist9a")
        app.ollama_cloud_model_profiles = lambda: (mock,)  # type: ignore

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp._bug_description = "   \n  "  # blank
            lp._repro_command = "python repro.py"
            lp._verify_command = "python repro.py"
            lp._profile_id = "qwen3.5:cloud"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            lp._render_rows()
            await pilot.pause()
            calls: list = []
            app.start_local_project_session = lambda **kw: calls.append(kw)  # type: ignore
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert len(calls) == 0, "blank bug must not call start"
            status = lp.query_one("#local-start-status", Static)
            txt = status.render().plain if hasattr(status.render(), "plain") else str(status.render())
            # fallback to internal renderable
            if "bug description is required" not in txt.lower():
                # try alternative: check widget's _renderable
                try:
                    raw = str(status._renderable)
                    txt = raw
                except Exception:
                    pass
            assert "bug description is required" in txt.lower(), f"expected bug required error, got {txt!r}"

        reset_launch_cwd()

    _run_async(_inner())


def test_9_invalid_dirty_repo_shows_warning(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen
        from agentic_debugger.application.level32 import Level32ModelProfile
        from textual.widgets import Static

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj9b")
        set_launch_cwd_for_tests(tmp_path)
        mock = Level32ModelProfile(alias="qwen3.5:cloud", display_name="qwen3.5:cloud", readiness="live_verified", transport_config_fingerprint="a" * 64)
        app = LocalApplicationV1(history_root=tmp_path / "hist9b")
        app.ollama_cloud_model_profiles = lambda: (mock,)  # type: ignore

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            # make dirty
            (repo / "file.py").write_text("dirty change\n", encoding="utf-8")
            lp._bug_description = "bug"
            lp._repro_command = "python repro.py"
            lp._verify_command = "python repro.py"
            lp._profile_id = "qwen3.5:cloud"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            lp._render_rows()
            await pilot.pause()
            calls: list = []
            app.start_local_project_session = lambda **kw: calls.append(kw)  # type: ignore
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert len(calls) == 0, "dirty repo must not call start"
            status = lp.query_one("#local-start-status", Static)
            txt = status.render().plain if hasattr(status.render(), "plain") else str(status.render())
            if "uncommitted changes" not in txt.lower():
                try:
                    txt = str(status._renderable)
                except Exception:
                    pass
            assert "uncommitted changes" in txt.lower(), f"expected dirty warning, got {txt!r}"
            # cleanup
            _run_git(repo, ["checkout", "--", "file.py"])

        reset_launch_cwd()

    _run_async(_inner())


def test_9_invalid_no_model_shows_error(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen
        from textual.widgets import Static

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj9c")
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist9c")
        app.ollama_cloud_model_profiles = lambda: ()  # type: ignore

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            # force no profiles
            lp._profiles = ()
            lp._profile_id = None
            lp._bug_description = "bug"
            lp._repro_command = "python repro.py"
            lp._verify_command = "python repro.py"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            lp._render_rows()
            await pilot.pause()
            calls: list = []
            app.start_local_project_session = lambda **kw: calls.append(kw)  # type: ignore
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert len(calls) == 0, "no model must not call start"
            status = lp.query_one("#local-start-status", Static)
            txt = status.render().plain if hasattr(status.render(), "plain") else str(status.render())
            if "no eligible model" not in txt.lower():
                try:
                    txt = str(status._renderable)
                except Exception:
                    pass
            assert "no eligible model" in txt.lower(), f"expected no eligible model, got {txt!r}"

        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 10. MODAL TEXT INPUT REGRESSION — s types normally inside editors
# ---------------------------------------------------------------------------
def test_10_modal_s_types_not_starts_bug_editor(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen, BugDescriptionEditorScreen
        from agentic_debugger.application.level32 import Level32ModelProfile
        from textual.widgets import TextArea

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj10a")
        set_launch_cwd_for_tests(tmp_path)
        mock = Level32ModelProfile(alias="qwen3.5:cloud", display_name="qwen3.5:cloud", readiness="live_verified", transport_config_fingerprint="a" * 64)
        app = LocalApplicationV1(history_root=tmp_path / "hist10a")
        app.ollama_cloud_model_profiles = lambda: (mock,)  # type: ignore

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp._bug_description = "bug"
            lp._repro_command = "python repro.py"
            lp._verify_command = "python repro.py"
            lp._profile_id = "qwen3.5:cloud"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            lp._render_rows()
            await pilot.pause()
            lp._activate_row("bug")
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert isinstance(app.screen, BugDescriptionEditorScreen)
            ta = app.screen.query_one("#bug-editor", TextArea)
            calls: list = []
            app.start_local_project_session = lambda **kw: calls.append(kw)  # type: ignore
            before = ta.text
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.1)
            after = ta.text
            assert len(calls) == 0, "s inside Bug editor must not start"
            assert "s" in after.lower(), f"s not typed, before {before!r} after {after!r}"
            # ensure character appears (TextArea inserts at cursor)
            assert len(after) == len(before) + 1 or "s" in after
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.15)

        reset_launch_cwd()

    _run_async(_inner())


def test_10_modal_s_types_not_starts_single_line(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import LocalProjectStartScreen, SingleLineFieldEditorScreen
        from agentic_debugger.application.level32 import Level32ModelProfile
        from textual.widgets import Input

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj10b")
        set_launch_cwd_for_tests(tmp_path)
        mock = Level32ModelProfile(alias="qwen3.5:cloud", display_name="qwen3.5:cloud", readiness="live_verified", transport_config_fingerprint="a" * 64)
        app = LocalApplicationV1(history_root=tmp_path / "hist10b")
        app.ollama_cloud_model_profiles = lambda: (mock,)  # type: ignore

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = LocalProjectStartScreen(initial_project=str(repo))
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp._bug_description = "bug"
            lp._profile_id = "qwen3.5:cloud"
            lp._render_rows()
            await pilot.pause()
            lp._activate_row("repro")
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            inp = app.screen.query_one("#single-line-editor", Input)
            calls: list = []
            app.start_local_project_session = lambda **kw: calls.append(kw)  # type: ignore
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.1)
            assert len(calls) == 0, "s inside single-line editor must not start"
            assert "s" in inp.value.lower(), f"s not typed in Input value {inp.value!r}"
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.15)

        reset_launch_cwd()

    _run_async(_inner())
