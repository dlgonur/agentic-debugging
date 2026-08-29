"""LOCAL-PROJECT-DEBUG-01 start-action repair — keyboard and button.

Covers the real smoke release blocker: valid clean form pressing s did nothing.
Tests per task sections 6-10. No provider, no Docker, no Level.

Uses Textual run_test deterministic fixtures; no real provider launch.

Adapted to the unified StartSessionScreen (initial_target="local_project"):
a live model is provided by installing a configured command-model profile in
the app-owned config store and selecting it explicitly (the screen never
auto-selects a model anymore); start kwargs therefore carry the explicitly
selected profile id, model_provider=None for configured profiles, and
auto_retries from the session config.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("textual")

PROFILE_ID = "qwen3.5-cloud"


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


def _write_configured_profile(root: Path, profile_id: str = PROFILE_ID) -> None:
    """Install one accepted-shaped configured command-model profile.

    Same JSON shape as tests/integration/test_configured_source.py
    ::write_profile; the command is never executed by these tests.
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
                        "display_name": profile_id,
                        "executable": sys.executable,
                        "argv": ["-c", "print('configured profile stub')"],
                        "request_timeout_seconds": 60.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _start_screen(app, project):
    """The unified start screen pinned to the Local Project target."""
    from agentic_debugger.ui.screens import StartSessionScreen

    return StartSessionScreen(
        task_options=list(app.curated_task_options()),
        initial_target="local_project",
        initial_project=str(project),
    )


def _select_configured_model(screen) -> None:
    screen._choice_selected("model", f"configured:{PROFILE_ID}")


def _plain(widget) -> str:
    rendered = widget.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


def _run_async(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 6. SUCCESSFUL START TEST — uses fake app boundary, no provider
# ---------------------------------------------------------------------------
def test_6_successful_start_via_s(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj6")
        set_launch_cwd_for_tests(tmp_path)
        # Eligible model via the app-owned configured-profile store
        _write_configured_profile(tmp_path / "hist6")
        app = LocalApplicationV1(history_root=tmp_path / "hist6")

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            # Valid form state per owner smoke
            bug = "discounted_price bug\nmultiline second line"
            lp._config.bug_description = bug
            lp._config.reproduction_command = "python repro.py"
            lp._config.verification_command = "python repro.py"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            lp._config.time_limit_seconds = None
            _select_configured_model(lp)
            await pilot.pause()
            # Focus Bug row as in owner's screenshot
            lp._focus_row("bug")
            await pilot.pause()
            await asyncio.sleep(0.05)
            calls: list[dict] = []

            def fake(**kwargs):
                calls.append(kwargs)

            app.start_local_project_session = fake  # type: ignore
            # press s
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert calls, "start_local_project_session not called via s"
            assert len(calls) == 1, f"expected exactly one start call, got {len(calls)}"
            captured = calls[0]
            assert captured.get("project_path") == str(repo.resolve())
            assert captured.get("bug_description") == bug.strip()
            assert captured.get("reproduction_command") == "python repro.py"
            assert captured.get("verification_command") == "python repro.py"
            assert captured.get("profile_id") == PROFILE_ID
            assert captured.get("model_provider") is None
            assert captured.get("max_elapsed_seconds") is None
        reset_launch_cwd()

    _run_async(_inner())


def test_general_ollama_ui_start_uses_registry_without_level32_or_profile_store(
    tmp_path, monkeypatch
):
    """Real unified UI -> production start -> worker provider params.

    Provider execution, worktree creation, and the worker are replaced at
    their boundaries.  The production start method itself remains real.
    """

    async def _inner():
        from types import SimpleNamespace

        from agentic_debugger.application import level32, local_project, model_providers
        from agentic_debugger.application.events import SourceKind
        from agentic_debugger.application.local_project import (
            reset_launch_cwd,
            set_launch_cwd_for_tests,
        )
        from agentic_debugger.application.model_providers import ProviderModel
        from agentic_debugger.evaluation.live import LiveModelConfig
        from agentic_debugger.ui import app as app_module
        from agentic_debugger.ui import screens as screens_module
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import ChoicePickerScreen

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "general-ollama-project")
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "general-ollama-history")

        general_model = ProviderModel(
            "ollama_cloud",
            "glm-5.3-flash:cloud",
            "glm-5.3-flash",
            "Ollama Cloud",
            True,
        )
        monkeypatch.setattr(
            screens_module,
            "list_provider_models",
            lambda **_kwargs: [general_model],
        )
        monkeypatch.setattr(app, "ollama_cloud_model_profiles", lambda: ())

        registry_calls: list[tuple[str, str]] = []
        live_config = LiveModelConfig(
            model_name="glm-5.3-flash:cloud",
            command=(sys.executable, "-c", "raise SystemExit(0)"),
            request_timeout_seconds=60.0,
            tool_version="1.3",
        )

        def resolve_registry(provider, model_id, **_kwargs):
            registry_calls.append((provider, model_id))
            return live_config, {
                "provider": provider,
                "profile_id": model_id,
                "display_name": "glm-5.3-flash",
                "protocol_version": "1.3",
                "tool_version": "1.3",
            }

        monkeypatch.setattr(
            model_providers, "resolve_provider_live_config", resolve_registry
        )

        def forbidden_level32():
            raise AssertionError("general Ollama must not query Level-32 qualification")

        def forbidden_store(_profile_id):
            raise AssertionError("registry Ollama must not query custom profiles")

        monkeypatch.setattr(level32, "level32_model_profiles", forbidden_level32)
        monkeypatch.setattr(app._config_store, "get", forbidden_store)

        isolated = tmp_path / "mock-isolated"
        parent = tmp_path / "mock-parent"
        isolated.mkdir()
        parent.mkdir()
        monkeypatch.setattr(
            local_project,
            "create_isolated_worktree",
            lambda *_args, **_kwargs: SimpleNamespace(
                isolated_path=isolated,
                parent_tmpdir=parent,
            ),
        )

        captured: dict = {}

        class FakeWorker:
            def __init__(self, **kwargs):
                captured["worker"] = kwargs
                self.session_dir = kwargs["session_dir"]

        class FakeRunner:
            def __init__(self, worker, **_kwargs):
                self.worker = worker

            def start(self):
                captured["started"] = True

            def close(self):
                pass

        monkeypatch.setattr(app_module, "SessionWorkerProcess", FakeWorker)
        monkeypatch.setattr(app_module, "LiveSessionRunner", FakeRunner)

        async with app.run_test() as pilot:
            await pilot.pause()
            start = _start_screen(app, repo)
            app.push_screen(start)
            await pilot.pause()
            start._config.bug_description = "general Ollama routing regression"
            start.render_state()

            start._open_model_picker()
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, ChoicePickerScreen)
            selected = next(
                choice
                for choice in picker.choices
                if choice.value == "ollama_cloud:glm-5.3-flash:cloud"
            )
            assert selected.disabled is False
            picker._on_select(selected.value)
            app.pop_screen()
            await pilot.pause()
            assert start._config.model.provider == "ollama_cloud"
            assert start._config.model.model_id == "glm-5.3-flash:cloud"

            await pilot.press("s")
            await pilot.pause()

            params = captured["worker"]["scenario_params"]
            spec = captured["worker"]["spec"]
            assert registry_calls == [
                ("ollama_cloud", "glm-5.3-flash:cloud")
            ]
            assert params["provider"] == "ollama_cloud"
            assert params["model_id"] == "glm-5.3-flash:cloud"
            assert "is_ollama" not in params
            assert "ollama_alias" not in params
            assert spec.source.kind is SourceKind.LOCAL_PROJECT
            assert (
                spec.source.model_config_ref
                == "ollama_cloud:glm-5.3-flash:cloud"
            )
            assert captured["started"] is True

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

        reset_launch_cwd()
        repo = _make_repo(tmp_path, f"proj7_{focus}")
        set_launch_cwd_for_tests(tmp_path)
        _write_configured_profile(tmp_path / f"hist7_{focus}")
        app = LocalApplicationV1(history_root=tmp_path / f"hist7_{focus}")

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp._config.bug_description = "bug"
            lp._config.reproduction_command = "python repro.py"
            lp._config.verification_command = "python repro.py"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            _select_configured_model(lp)
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

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj8")
        set_launch_cwd_for_tests(tmp_path)
        _write_configured_profile(tmp_path / "hist8")
        app = LocalApplicationV1(history_root=tmp_path / "hist8")

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            bug = "multiline\nbug here"
            lp._config.bug_description = bug
            lp._config.reproduction_command = "python repro.py"
            lp._config.verification_command = "python repro.py"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            lp._config.time_limit_seconds = None
            _select_configured_model(lp)
            await pilot.pause()
            # Ensure button exists
            btn = lp.query_one("#start-session-button")
            assert btn is not None
            # The primary action uses concise sentence case.
            label = getattr(btn, "label", None)
            plain = label.plain if hasattr(label, "plain") else str(label)
            assert plain == "Start debugging"
            assert btn.disabled is False
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
            await pilot.click("#start-session-button")
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
        from textual.widgets import Static

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj9a")
        set_launch_cwd_for_tests(tmp_path)
        _write_configured_profile(tmp_path / "hist9a")
        app = LocalApplicationV1(history_root=tmp_path / "hist9a")

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp._config.bug_description = "   \n  "  # blank
            lp._config.reproduction_command = "python repro.py"
            lp._config.verification_command = "python repro.py"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            _select_configured_model(lp)
            await pilot.pause()
            calls: list = []
            app.start_local_project_session = lambda **kw: calls.append(kw)  # type: ignore
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert len(calls) == 0, "blank bug must not call start"
            status = lp.query_one("#start-status", Static)
            txt = _plain(status)
            assert "start unavailable" in txt.lower(), f"expected visible blocker, got {txt!r}"
            assert "describe the bug" in txt.lower(), f"expected bug required error, got {txt!r}"

        reset_launch_cwd()

    _run_async(_inner())


def test_9_invalid_dirty_repo_shows_warning(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from textual.widgets import Static

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj9b")
        set_launch_cwd_for_tests(tmp_path)
        _write_configured_profile(tmp_path / "hist9b")
        app = LocalApplicationV1(history_root=tmp_path / "hist9b")

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            # make dirty
            (repo / "file.py").write_text("dirty change\n", encoding="utf-8")
            lp._config.bug_description = "bug"
            lp._config.reproduction_command = "python repro.py"
            lp._config.verification_command = "python repro.py"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            _select_configured_model(lp)
            await pilot.pause()
            calls: list = []
            app.start_local_project_session = lambda **kw: calls.append(kw)  # type: ignore
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert len(calls) == 0, "dirty repo must not call start"
            status = lp.query_one("#start-status", Static)
            txt = _plain(status)
            assert "start unavailable" in txt.lower(), f"expected visible blocker, got {txt!r}"
            assert "uncommitted changes" in txt.lower(), f"expected dirty warning, got {txt!r}"
            assert lp.query_one("#start-session-button").disabled is True
            # cleanup
            _run_git(repo, ["checkout", "--", "file.py"])

        reset_launch_cwd()

    _run_async(_inner())


def test_9_invalid_no_model_shows_error(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from textual.widgets import Static

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj9c")
        set_launch_cwd_for_tests(tmp_path)
        # No configured profile installed and no model selected: the unified
        # screen stays on the Offline default, which Local Project rejects.
        app = LocalApplicationV1(history_root=tmp_path / "hist9c")

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp._config.bug_description = "bug"
            lp._config.reproduction_command = "python repro.py"
            lp._config.verification_command = "python repro.py"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            lp.render_state()
            await pilot.pause()
            assert lp.profile_id is None  # Offline default: no live model selected
            calls: list = []
            app.start_local_project_session = lambda **kw: calls.append(kw)  # type: ignore
            await pilot.press("s")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert len(calls) == 0, "no model must not call start"
            status = lp.query_one("#start-status", Static)
            txt = _plain(status)
            assert "start unavailable" in txt.lower(), f"expected visible blocker, got {txt!r}"
            assert "select a live model" in txt.lower(), f"expected no live model error, got {txt!r}"

        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# 10. MODAL TEXT INPUT REGRESSION — s types normally inside editors
# ---------------------------------------------------------------------------
def test_10_modal_s_types_not_starts_bug_editor(tmp_path):
    async def _inner():
        from agentic_debugger.application.local_project import reset_launch_cwd, set_launch_cwd_for_tests
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import BugDescriptionEditorScreen
        from textual.widgets import TextArea

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj10a")
        set_launch_cwd_for_tests(tmp_path)
        _write_configured_profile(tmp_path / "hist10a")
        app = LocalApplicationV1(history_root=tmp_path / "hist10a")

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp._config.bug_description = "bug"
            lp._config.reproduction_command = "python repro.py"
            lp._config.verification_command = "python repro.py"
            lp._repro_user_edited = True
            lp._verify_user_edited = True
            _select_configured_model(lp)
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
        from agentic_debugger.ui.screens import SingleLineFieldEditorScreen
        from textual.widgets import Input

        reset_launch_cwd()
        repo = _make_repo(tmp_path, "proj10b")
        set_launch_cwd_for_tests(tmp_path)
        _write_configured_profile(tmp_path / "hist10b")
        app = LocalApplicationV1(history_root=tmp_path / "hist10b")

        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.sleep(0.15)
            lp = _start_screen(app, repo)
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp._config.bug_description = "bug"
            _select_configured_model(lp)
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
