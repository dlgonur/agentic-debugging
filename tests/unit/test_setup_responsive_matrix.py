"""Responsive setup matrix, A3 (local-project-setup-responsive-a3).

Pins the documented responsive contract for every Local Project setup
surface — grouped form, ProjEnv builder (incl. error line + DSL toggle),
and the Repro/Verify single-line editors with captions — at 80x24,
120x40, and 160x50 (the representative sizes from
docs/architecture/local-application-v1.md:989-1000):

- rail/summary visibility per width (rail at >= 100, in-column summary below)
- Run reachable + clickable at 80x24 on all three surfaces
- no region out-of-bounds at any size
- footer variant per width, fully painted (compact fits the 80-col minimum)

Plus the two genuine gaps found while probing (covered here):
- long single-line titles (Verify, DSL toggle) wrap instead of clipping
- the compact footer fits 80 cols (tighter separators, same vocabulary)

Presentation + tests only. No contract/worker/verifier/journal change.
Offline only: no providers, network, credentials, or downloads.
"""

from __future__ import annotations

import asyncio
import html
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("textual")

SIZES = [(80, 24), (120, 40), (160, 50)]
SIZE_IDS = ["80x24", "120x40", "160x50"]

PROFILE_ID = "a3-matrix-model"


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


def _make_repo(tmp: Path, name: str) -> Path:
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
                        "argv": ["-c", "print('matrix stub')"],
                        "request_timeout_seconds": 60.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _plain(widget) -> str:
    rendered = widget.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


def _painted_svg(app) -> str:
    """Exported SVG with entities resolved (Rich emits &#160; for spaces)."""
    return html.unescape(app.export_screenshot())


def _painted_line(app, line_index: int) -> str:
    """Visible text painted on one terminal row (all style runs joined).

    Rich splits a row into several <text> runs; only their concatenation
    is the painted line. Trailing blank padding is stripped.
    """
    import re

    svg = _painted_svg(app)
    parts: list[str] = []
    for match in re.finditer(r"<text ([^>]*)>(.*?)</text>", svg, re.S):
        attrs, inner = match.group(1), match.group(2)
        if f"line-{line_index}" not in attrs:
            continue
        parts.append(re.sub(r"<[^>]+>", "", inner))
    return "".join(parts).rstrip(" \xa0\n")


def _run_async(coro):
    return asyncio.run(coro)


def _is_shown(widget) -> bool:
    """True when no ancestor (up to the screen) hides the widget."""
    from textual.screen import Screen

    node = widget
    while node is not None and not isinstance(node, Screen):
        try:
            if node.display is False:
                return False
        except Exception:
            pass
        node = getattr(node, "parent", None)
    return True


# ---------------------------------------------------------------------------
# A. Grouped form: rail / summary / footer per width
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES, ids=SIZE_IDS)
def test_a1_rail_summary_footer_per_width(tmp_path, size):
    async def _inner():
        from textual.containers import VerticalScroll
        from textual.widgets import Static

        from agentic_debugger.application.local_project import (
            reset_launch_cwd,
            set_launch_cwd_for_tests,
        )
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen
        from agentic_debugger.ui.screens_shared import START_FOOTER, START_FOOTER_COMPACT

        width, height = size
        reset_launch_cwd()
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / f"hist-a1-{width}x{height}")
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp = StartSessionScreen(
                task_options=list(app.curated_task_options()),
                initial_target="local_project",
                initial_project=str(tmp_path),
            )
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            rail = lp.query_one("#start-context", VerticalScroll)
            summary = lp.query_one("#start-context-summary", Static)
            assert rail.display == (width >= 100), f"rail at {width}"
            assert summary.display == (width < 100), f"summary at {width}"
            footer = lp.query_one("#start-footer", Static)
            # Variant follows what fits: the 36-cell rail steals width at
            # >= 100 cols and the footer keeps 2 cells of padding per side.
            # 80x24 and 120x40 both select compact (76/80 < 107 cells);
            # 160x50 selects full (120 >= 107).
            rail = 36 if width >= 100 else 0
            expected = START_FOOTER if width - rail - 4 >= len(START_FOOTER) else START_FOOTER_COMPACT
            assert _plain(footer) == expected, f"footer variant at {width}"
            # The painted footer line carries the full variant text: nothing
            # may clip at the right edge (compact fits the 80-col minimum).
            painted = expected.replace(" ", "\xa0")
            assert _painted_line(app, height - 1) == painted, (
                f"footer not fully painted at {width}x{height}: "
                f"{_painted_line(app, height - 1)!r}"
            )
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# A. Grouped form: Run reachable + clickable at every size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES, ids=SIZE_IDS)
def test_a2_run_reachable_and_clickable(tmp_path, size):
    async def _inner():
        from textual.widgets import Button

        from agentic_debugger.application.local_project import (
            reset_launch_cwd,
            set_launch_cwd_for_tests,
        )
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen

        width, height = size
        reset_launch_cwd()
        repo = _make_repo(tmp_path, f"proja2-{width}x{height}")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / f"hist-a2-{width}x{height}"
        _write_configured_profile(hist)
        app = LocalApplicationV1(history_root=hist)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp = StartSessionScreen(
                task_options=list(app.curated_task_options()),
                initial_target="local_project",
                initial_project=str(repo),
            )
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._config.bug_description = "matrix run reachability"
            lp._config.reproduction_command = "python repro.py"
            lp._config.verification_command = "python repro.py"
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            button = lp.query_one("#start-session-button", Button)
            assert button.disabled is False, f"Run should be enabled at {width}x{height}"
            # Timeout-UX: the new Bounds row keeps Run within 80x24 but at
            # the bottom edge the click needs an explicit scroll into view
            # (D2-established scroll-reachable pattern).
            try:
                button.scroll_visible(animate=False)
            except Exception:
                pass
            await pilot.pause()
            region = button.region
            assert region.x >= 0 and region.y >= 0, f"Run region {region}"
            assert region.x + region.width <= width, f"Run clips at {width}"
            assert region.y + region.height <= height, f"Run off-screen at {height}"
            calls: list[dict] = []
            app.start_local_project_session = lambda **kw: calls.append(kw)  # type: ignore
            await pilot.click("#start-session-button")
            await pilot.pause()
            await asyncio.sleep(0.15)
            assert len(calls) == 1, f"Run click must start once at {width}x{height}"
            assert calls[0].get("bug_description") == "matrix run reachability"
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# A. Grouped form: no region out-of-bounds at any size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES, ids=SIZE_IDS)
def test_a3_no_region_out_of_bounds(tmp_path, size):
    async def _inner():
        from agentic_debugger.application.local_project import (
            reset_launch_cwd,
            set_launch_cwd_for_tests,
        )
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen

        width, height = size
        reset_launch_cwd()
        repo = _make_repo(tmp_path, f"proja3-{width}x{height}")
        set_launch_cwd_for_tests(tmp_path)
        hist = tmp_path / f"hist-a3-{width}x{height}"
        _write_configured_profile(hist)
        app = LocalApplicationV1(history_root=hist)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp = StartSessionScreen(
                task_options=list(app.curated_task_options()),
                initial_target="local_project",
                initial_project=str(repo),
            )
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._config.bug_description = "matrix bounds probe"
            lp._choice_selected("model", f"configured:{PROFILE_ID}")
            await pilot.pause()
            bad: list[str] = []
            for widget in lp.query("*"):
                if widget.screen is not lp or not _is_shown(widget):
                    continue
                try:
                    region = widget.region
                except Exception:
                    continue
                if region.width == 0 or region.height == 0:
                    continue
                if (
                    region.x < 0
                    or region.y < 0
                    or region.x + region.width > width
                    or region.y + region.height > height
                ):
                    bad.append(
                        f"{type(widget).__name__}#{getattr(widget, 'id', None)} {region}"
                    )
            assert bad == [], f"out-of-bounds at {width}x{height}: {bad}"
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# A. Grouped form: long paths compact (ellipsize, never hard-clip)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES, ids=SIZE_IDS)
def test_a4_long_path_compacts(tmp_path, size):
    async def _inner():
        from agentic_debugger.application.local_project import (
            reset_launch_cwd,
            set_launch_cwd_for_tests,
        )
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen

        width, height = size
        reset_launch_cwd()
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / f"hist-a4-{width}x{height}")
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.2)
            lp = StartSessionScreen(
                task_options=list(app.curated_task_options()),
                initial_target="local_project",
                initial_project=str(tmp_path),
            )
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            # A path longer than even the widest row budget must ellipsize
            # everywhere (at 160 the budget is 101 cells); shorter paths
            # legitimately render in full where room allows.
            long_path = (
                "C:/very/long/path/to/some/deeply/nested/repository/that/exceeds/"
                "any/terminal/width/budget/by/a/wide/margin/indeed/far/beyond/it"
            )
            assert len(long_path) > 101
            lp._config.project_path = long_path
            lp.render_state()
            await pilot.pause()
            row = lp._row("project")
            text = _plain(row)
            assert "\u2026" in text, f"long path must ellipsize at {width}: {text!r}"
            assert long_path not in text, f"long path must not overflow at {width}"
            region = row.region
            assert region.x + region.width <= width
        reset_launch_cwd()

    _run_async(_inner())


# ---------------------------------------------------------------------------
# B. ProjEnv builder: error fidelity + in-bounds at every size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES, ids=SIZE_IDS)
def test_b1_builder_error_names_row_and_rule(tmp_path, size):
    async def _inner():
        from textual.widgets import Static

        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import ProjectEnvBuilderScreen

        width, height = size
        app = LocalApplicationV1(history_root=tmp_path / f"hist-b1-{width}x{height}")
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            screen = ProjectEnvBuilderScreen(
                initial_text="FOO, BAD NAME, secret:DB_URL",
                on_save=lambda value: None,
            )
            app.push_screen(screen)
            await pilot.pause()
            await asyncio.sleep(0.3)
            err = screen.query_one("#projenv-error", Static)
            text = _plain(err)
            assert "row 2" in text and "BAD NAME" in text, text
            region = err.region
            assert region.x + region.width <= width
            assert region.y + region.height <= height
            await pilot.press("escape")
            await pilot.pause()

    _run_async(_inner())


@pytest.mark.parametrize("size", SIZES, ids=SIZE_IDS)
def test_b2_builder_controls_in_bounds(tmp_path, size):
    async def _inner():
        from textual.widgets import Button, Input, Static

        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import ProjectEnvBuilderScreen

        width, height = size
        app = LocalApplicationV1(history_root=tmp_path / f"hist-b2-{width}x{height}")
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            screen = ProjectEnvBuilderScreen(
                initial_text="FOO, BAD NAME, secret:DB_URL",
                on_save=lambda value: None,
            )
            app.push_screen(screen)
            await pilot.pause()
            await asyncio.sleep(0.3)
            dialog = screen.query_one("#projenv-dialog")
            region = dialog.region
            assert region.x + region.width <= width
            assert region.y + region.height <= height
            assert len(list(screen.query(Input))) == 3
            for widget in list(screen.query(Input)) + list(screen.query(Button)):
                region = widget.region
                assert region.x + region.width <= width, f"{widget.id} clips at {width}"
                assert region.y + region.height <= height, f"{widget.id} off-screen"
            for widget in screen.query(Static):
                region = widget.region
                if region.width == 0 or region.height == 0:
                    continue
                assert region.x + region.width <= width, f"{widget.id} clips"
            count = _plain(screen.query_one("#projenv-count", Static))
            assert "inherit" in count and "secret" in count, count
            await pilot.press("escape")
            await pilot.pause()

    _run_async(_inner())


def test_b3_builder_dsl_toggle_round_trip_at_80x24(tmp_path):
    async def _inner():
        from textual.widgets import Button, Input, Static

        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import (
            ProjectEnvBuilderScreen,
            SingleLineFieldEditorScreen,
        )

        app = LocalApplicationV1(history_root=tmp_path / "hist-b3")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen = ProjectEnvBuilderScreen(
                initial_text="FOO, secret:DB_URL",
                on_save=lambda value: None,
            )
            app.push_screen(screen)
            await pilot.pause()
            await asyncio.sleep(0.3)
            await pilot.click("#projenv-dsl")
            await pilot.pause()
            await asyncio.sleep(0.3)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            # Long DSL title wraps instead of clipping (same dialog geometry
            # as the Repro/Verify editors).
            title = app.screen.query_one("#single-line-title", Static)
            assert title.region.height >= 2, "DSL title must wrap, not clip"
            for selector in (
                "#single-line-title",
                "#single-line-note",
                "#single-line-editor",
                "#single-line-save-button",
                "#single-line-hint",
            ):
                region = app.screen.query_one(selector).region
                assert region.x + region.width <= 80, f"{selector} clips"
                assert region.y + region.height <= 24, f"{selector} off-screen"
            assert "(DSL;" in _plain(title), "DSL title must stay legible"
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert isinstance(app.screen, ProjectEnvBuilderScreen)
            assert [row["name"] for row in screen._rows] == ["FOO", "DB_URL"]
            await pilot.press("escape")
            await pilot.pause()

    _run_async(_inner())


def test_b4_builder_add_and_save_at_80x24(tmp_path):
    async def _inner():
        from textual.widgets import Input

        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import ProjectEnvBuilderScreen

        app = LocalApplicationV1(history_root=tmp_path / "hist-b4")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            saved: list[str] = []
            screen = ProjectEnvBuilderScreen(
                initial_text="FOO",
                on_save=lambda value: saved.append(value),
            )
            app.push_screen(screen)
            await pilot.pause()
            await asyncio.sleep(0.3)
            await pilot.click("#projenv-add")
            await pilot.pause()
            await asyncio.sleep(0.15)
            inputs = list(screen.query(Input))
            assert len(inputs) == 2
            # The new row is keyboard-reachable: Tab from the Add button
            # lands on it within a bounded number of stops (mount is async,
            # so focus is not forced synchronously by the click handler).
            new_id = inputs[-1].id
            assert new_id
            for _ in range(10):
                if inputs[-1].has_focus:
                    break
                await pilot.press("tab")
                await pilot.pause()
            assert inputs[-1].has_focus, "new row must be Tab-reachable"
            inputs[-1].value = "NEWVAR"
            await pilot.pause()
            # All three builder actions were already exercised by click
            # (Add above, Save below); DSL toggle is covered by test_b3.
            await pilot.click("#projenv-save")
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert saved == ["FOO, NEWVAR"], saved

    _run_async(_inner())


# ---------------------------------------------------------------------------
# C. Repro/Verify single-line editors: caption + title + save at every size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row_key", ["repro", "verify"])
@pytest.mark.parametrize("size", SIZES, ids=SIZE_IDS)
def test_c1_editor_caption_and_bounds(tmp_path, row_key, size):
    async def _inner():
        from textual.widgets import Static

        from agentic_debugger.application.local_project import (
            reset_launch_cwd,
            set_launch_cwd_for_tests,
        )
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import (
            SingleLineFieldEditorScreen,
            StartSessionScreen,
        )
        from agentic_debugger.ui.setup_display import APPLY_ELIGIBILITY_CAPTION

        width, height = size
        reset_launch_cwd()
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / f"hist-c1-{row_key}-{width}x{height}")
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            lp = StartSessionScreen(
                task_options=list(app.curated_task_options()),
                initial_target="local_project",
                initial_project=str(tmp_path),
            )
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._activate_row(row_key)
            await pilot.pause()
            await asyncio.sleep(0.3)
            assert isinstance(app.screen, SingleLineFieldEditorScreen)
            assert _plain(app.screen.query_one("#single-line-note", Static)) == (
                APPLY_ELIGIBILITY_CAPTION
            )
            for selector in (
                "#single-line-title",
                "#single-line-note",
                "#single-line-editor",
                "#single-line-save-button",
                "#single-line-hint",
            ):
                region = app.screen.query_one(selector).region
                assert region.x + region.width <= width, f"{selector} clips"
                assert region.y + region.height <= height, f"{selector} off-screen"
                assert region.width > 0 and region.height > 0, selector
            await pilot.press("escape")
            await pilot.pause()
        reset_launch_cwd()

    _run_async(_inner())


@pytest.mark.parametrize("size", SIZES, ids=SIZE_IDS)
def test_c2_long_title_wraps_instead_of_clipping(tmp_path, size):
    """The Verify title (71 cells) exceeds the 64-cell dialog inner width.

    Before A3 the title Static was height:1, so "the fix)" never painted.
    Now it wraps and the full title paints at every size.
    """

    async def _inner():
        from textual.widgets import Static

        from agentic_debugger.application.local_project import (
            reset_launch_cwd,
            set_launch_cwd_for_tests,
        )
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen

        width, height = size
        reset_launch_cwd()
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / f"hist-c2-{width}x{height}")
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            lp = StartSessionScreen(
                task_options=list(app.curated_task_options()),
                initial_target="local_project",
                initial_project=str(tmp_path),
            )
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._activate_row("verify")
            await pilot.pause()
            await asyncio.sleep(0.3)
            title = app.screen.query_one("#single-line-title", Static)
            assert title.region.height >= 2, f"Verify title must wrap at {width}"
            svg = _painted_svg(app)
            assert "BEFORE" in svg and "fix)" in svg, (
                f"Verify title must fully paint at {width}x{height}"
            )
            await pilot.press("escape")
            await pilot.pause()
            # Short titles still occupy one line (no churn for the common case).
            lp._activate_row("repro")
            await pilot.pause()
            await asyncio.sleep(0.3)
            short = app.screen.query_one("#single-line-title", Static)
            assert short.region.height == 1
            assert "Reproduction\xa0command\xa0(optional)" in _painted_svg(app)
            await pilot.press("escape")
            await pilot.pause()
        reset_launch_cwd()

    _run_async(_inner())


def test_c3_editor_save_and_cancel_at_80x24(tmp_path):
    async def _inner():
        from textual.widgets import Input

        from agentic_debugger.application.local_project import (
            reset_launch_cwd,
            set_launch_cwd_for_tests,
        )
        from agentic_debugger.ui.app import LocalApplicationV1
        from agentic_debugger.ui.screens import StartSessionScreen

        reset_launch_cwd()
        set_launch_cwd_for_tests(tmp_path)
        app = LocalApplicationV1(history_root=tmp_path / "hist-c3")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            lp = StartSessionScreen(
                task_options=list(app.curated_task_options()),
                initial_target="local_project",
                initial_project=str(tmp_path),
            )
            app.push_screen(lp)
            await pilot.pause()
            await asyncio.sleep(0.3)
            lp._activate_row("repro")
            await pilot.pause()
            await asyncio.sleep(0.3)
            editor = app.screen.query_one("#single-line-editor", Input)
            editor.value = "python repro.py -k smoke"
            await pilot.pause()
            await pilot.click("#single-line-save-button")
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp._config.reproduction_command == "python repro.py -k smoke"
            lp._activate_row("verify")
            await pilot.pause()
            await asyncio.sleep(0.3)
            app.screen.query_one("#single-line-editor", Input).value = "discard me"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert lp._config.verification_command != "discard me"
        reset_launch_cwd()

    _run_async(_inner())
