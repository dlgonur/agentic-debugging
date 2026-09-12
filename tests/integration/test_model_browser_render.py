"""Real Textual render regressions for the quiet model browser.

These tests render ``ModelBrowserScreen`` inside the real product
application (product stylesheet included) at 120x32 and 80x24 and prove
from actual widget regions and terminal output that:

- the header is quiet (``Select model`` only, no Current/ready counts);
- the provider filter is one compact control (``All providers``) with
  no per-filter counts, and the result count appears only when
  filtering;
- every row shows model name + provider only (no protocol, id,
  readiness dots, route status, counts, or qualification notes);
- there is no persistent details pane for selected models;
- every browser control sits inside the screen bounds;
- keyboard search / provider cycling / Enter selection work;
- the manage-providers entry remains reachable;
- long-list highlight/scroll behavior is preserved.

Pure-helper coverage (search, quiet rows, empty details) lives in
``tests/unit/test_model_browser_v2.py``; this file owns rendered proof.
"""

from __future__ import annotations

import re
import sys
from html import unescape
from pathlib import Path
from typing import Any, List, Optional

import pytest

textual = pytest.importorskip("textual")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui_support import run_headless  # noqa: E402

from agentic_debugger.application.history import HistoryStore  # noqa: E402
from agentic_debugger.ui.app import LocalApplicationV1  # noqa: E402
from agentic_debugger.ui.model_browser import ModelBrowserScreen  # noqa: E402
from agentic_debugger.ui.session_config import ModelOption  # noqa: E402


def make_app(tmp_path: Path) -> LocalApplicationV1:
    return LocalApplicationV1(history_store=HistoryStore(tmp_path))


def _options() -> List[ModelOption]:
    return [
        ModelOption("opencode_go", "muse-spark-1.3-contributor", "Muse Spark 1.3 Contributor",
                    detail="direct API · responses", available=True, unavailable_reason=None,
                    protocol="responses", provider_label="OpenCode Go"),
        ModelOption("opencode_go", "gpt-5.6-luna", "GPT-5.6 Luna",
                    detail="direct API · responses", available=True, unavailable_reason=None,
                    protocol="responses", provider_label="OpenCode Go"),
        ModelOption("opencode_go", "deepseek-v4.1-flash", "DeepSeek V4.1 Flash",
                    detail="direct API · chat_completions", available=True, unavailable_reason=None,
                    protocol="chat_completions", provider_label="OpenCode Go"),
        ModelOption("opencode_go", "qwen3.8-max", "Qwen 3.8 Max",
                    detail="direct API · messages", available=True, unavailable_reason=None,
                    protocol="messages", provider_label="OpenCode Go"),
        ModelOption("opencode_go", "future-unknown-zzz", "Future Unknown Zzz",
                    detail="", available=False,
                    unavailable_reason="Protocol not yet resolved for direct API",
                    protocol=None, provider_label="OpenCode Go"),
        ModelOption("my_gateway", "custom-model-x", "Custom Model X",
                    detail="", available=False,
                    unavailable_reason="no direct API credential",
                    protocol="chat_completions", provider_label="My Gateway"),
        ModelOption("offline", "", "Offline", detail="", available=True,
                    unavailable_reason=None, protocol=None, provider_label="Offline"),
    ]


def _svg_text(app: Any) -> str:
    svg = unescape(app.export_screenshot()).replace("\xa0", " ")
    texts = re.findall(r">([^<>]{1,160})<", svg)
    kept: List[str] = []
    for item in texts:
        item = item.strip()
        if not item or "font" in item.lower() or "terminal-" in item:
            continue
        if item not in kept:
            kept.append(item)
    return "\n".join(kept)


def _assert_within_bounds(screen: Any, width: int, height: int) -> None:
    for qid in ("#model-browser-dialog", "#model-browser-title",
                "#model-browser-search",
                "#model-browser-filters", "#model-browser-provider-button",
                "#model-browser-count", "#model-browser-list",
                "#model-browser-hint"):
        widget = screen.query_one(qid)
        region = widget.region
        assert region.x >= 0 and region.y >= 0, (qid, region)
        assert region.x + region.width <= width, (qid, region, width)
        assert region.y + region.height <= height, (qid, region, height)
        assert region.width > 0 and region.height > 0, (qid, region)
    # No persistent details pane: the selector surface never mounts it,
    # so the model list keeps the recovered space.
    from textual.css.query import NoMatches

    with pytest.raises(NoMatches):
        screen.query_one("#model-browser-details")


def _prompt_text(option_list: Any, index: int) -> str:
    prompt = option_list.get_option_at_index(index).prompt
    return prompt.plain if hasattr(prompt, "plain") else str(prompt)


@pytest.mark.parametrize("size", [(120, 32), (80, 24)])
def test_quiet_header_rows_and_footer(tmp_path: Path, size: tuple) -> None:
    """Quiet hierarchy renders: title, compact filter, name+provider rows."""
    app = make_app(tmp_path)
    width, height = size

    async def actions(pilot):
        await app.push_screen(ModelBrowserScreen(
            _options(), current_key="opencode_go:gpt-5.6-luna",
            on_select=lambda value: None, title="Select model",
        ))
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelBrowserScreen)
        _assert_within_bounds(screen, width, height)
        text = _svg_text(app)
        # Quiet header: title only.
        assert "Select model" in text
        assert "Current:" not in text
        assert "ready" not in text.lower()
        # Compact provider control, no counts.
        assert "All providers" in text
        assert "Provider:" not in text
        assert "shown" not in text.lower()
        assert "5/7" not in text
        # Search + footer are minimal.
        assert "Search models" in text
        assert "Enter select" in text
        assert "Esc cancel" in text
        assert "[ ] provider" not in text
        assert "up/down" not in text.lower()
        assert "/ search" not in text
        # Normal rows repeat no routing/configuration detail.
        for banned in ("gpt-5.6-luna", "muse-spark-1.3-contributor",
                        "deepseek-v4.1-flash", "qwen3.8-max",
                        "future-unknown-zzz", "custom-model-x",
                        "Status:", "Responses", "responses",
                        "Chat Completions", "chat_completions",
                        "Messages", "messages",
                        "Unresolved", "Route needed", "Route unresolved",
                        "direct API", "not qualified",
                        "frozen Level-32", "●", "○"):
            assert banned not in text
        # Model rows render as name + quiet provider (live options prove
        # full text: SVG glyph runs fragment rows, so rows are proven
        # here and SVG-proven below for the visible window).
        from textual.widgets import OptionList, Static

        option_list = screen.query_one("#model-browser-list", OptionList)
        assert option_list.option_count == len(screen._filtered) + 1
        prompts = [
            _prompt_text(option_list, index)
            for index in range(option_list.option_count - 1)
        ]
        assert any(p == "Muse Spark 1.3 Contributor  OpenCode Go" for p in prompts)
        assert any("GPT-5.6 Luna" in p and "OpenCode Go" in p for p in prompts)
        assert any("DeepSeek V4.1 Flash" in p and "OpenCode Go" in p for p in prompts)
        assert any("Qwen 3.8 Max" in p and "OpenCode Go" in p for p in prompts)
        for prompt in prompts[:4]:
            assert "Responses" not in prompt
            assert "Chat Completions" not in prompt
            assert "Messages" not in prompt
            assert "responses" not in prompt
            assert "chat_completions" not in prompt
            assert "messages" not in prompt
            assert "Route needed" not in prompt
            assert "Route unresolved" not in prompt
            assert "direct API" not in prompt
            assert "!" not in prompt
            assert "●" not in prompt and "○" not in prompt
        # Current selection is marked in the list, not in a header line.
        assert any("GPT-5.6 Luna" in p and "✓" in p for p in prompts)
        # Exceptional rows carry no routing status: name + provider only,
        # still disabled (proven here via the prompt text and in the
        # unit gates via is_selectable).
        assert any(p == "Future Unknown Zzz  OpenCode Go" for p in prompts)
        assert any(p == "Custom Model X  My Gateway" for p in prompts)
        for prompt in prompts[4:6]:
            assert "!" not in prompt
            assert "Route needed" not in prompt
            assert "Unavailable" not in prompt
        # Offline renders as plain "Offline" (provider deduped, no status).
        assert any(p == "Offline" or p == "Offline  ✓" for p in prompts)
        # Unfiltered count stays empty (secondary, only when filtering).
        count = screen.query_one("#model-browser-count", Static)
        assert str(count.render().plain or "").strip() == ""
        # No persistent details pane on the selector surface.
        from textual.css.query import NoMatches

        with pytest.raises(NoMatches):
            screen.query_one("#model-browser-details", Static)
        # The trailing manage-providers entry is rendered.
        assert "Manage model providers" in _prompt_text(option_list, option_list.option_count - 1)
        # ... and the visible window shows the current selection.
        assert "GPT-5.6 Luna" in text

    run_headless(app, actions, size=size)


def test_exceptional_highlight_shows_no_details_strip(tmp_path: Path) -> None:
    """Highlighting an exceptional model shows no routing strip.

    The picker is a selector, not an inspection screen: moving the
    highlight onto an unresolved/unavailable row must not mount any
    details pane or routing prose.  The row stays name + provider only
    (disabled), and the highlight stays visible.
    """
    app = make_app(tmp_path)

    async def actions(pilot):
        await app.push_screen(ModelBrowserScreen(
            _options(), current_key="opencode_go:gpt-5.6-luna",
            on_select=lambda value: None, title="Select model",
        ))
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        from textual.css.query import NoMatches
        from textual.widgets import OptionList, Static

        option_list = screen.query_one("#model-browser-list", OptionList)
        # No details pane on mount.
        with pytest.raises(NoMatches):
            screen.query_one("#model-browser-details", Static)
        # Move to the unresolved row: still no details pane, no routing
        # prose anywhere in the rendered output.
        for index in range(len(screen._filtered)):
            if screen._filtered[index].model_id == "future-unknown-zzz":
                option_list.highlighted = index
                break
        await pilot.pause()
        await pilot.pause()
        with pytest.raises(NoMatches):
            screen.query_one("#model-browser-details", Static)
        prompt = _prompt_text(option_list, option_list.highlighted)
        assert prompt == "Future Unknown Zzz  OpenCode Go"
        for banned in ("Route needed", "Route unresolved", "Unresolved",
                        "direct API", "responses", "chat_completions",
                        "messages", "Status:", "!"):
            assert banned not in prompt
        assert option_list.get_option_at_index(option_list.highlighted).disabled is True
        _assert_highlight_visible(option_list)
        text = _svg_text(app)
        for banned in ("Route needed", "Route unresolved", "direct API",
                        "responses", "chat_completions", "messages",
                        "not qualified", "Status:"):
            assert banned not in text

    run_headless(app, actions, size=(120, 32))


def test_constrained_list_and_hint_visible(tmp_path: Path) -> None:
    """At 80x24 the list and the short hint fit on screen."""
    app = make_app(tmp_path)

    async def actions(pilot):
        await app.push_screen(ModelBrowserScreen(
            _options(), current_key="opencode_go:gpt-5.6-luna",
            on_select=lambda value: None, title="Select model",
        ))
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        from textual.widgets import OptionList

        option_list = screen.query_one("#model-browser-list", OptionList)
        assert option_list.region.height >= 3
        text = _svg_text(app)
        # Quiet footer only.
        assert "Enter select" in text
        assert "Esc cancel" in text
        assert "[ ] provider" not in text
        assert "Status:" not in text
        assert "Route needed" not in text
        assert "Route unresolved" not in text
        assert "direct API" not in text
        # At least one model row is visible in the window (rows carry
        # quiet text, so visibility is proven via the visible window
        # plus the live option prompts).
        prompts = [
            _prompt_text(option_list, index)
            for index in range(option_list.option_count - 1)
        ]
        assert any("Qwen 3.8 Max" in prompt for prompt in prompts)
        assert any("DeepSeek V4.1 Flash" in prompt for prompt in prompts)
        for prompt in prompts:
            for banned in ("Route needed", "Route unresolved", "direct API",
                            "responses", "chat_completions", "messages",
                            "Status:", "!"):
                assert banned not in prompt

    run_headless(app, actions, size=(80, 24))


def test_keyboard_search_filter_select(tmp_path: Path) -> None:
    """Real keypresses: search narrows, [ ] cycles provider, Enter selects."""
    app = make_app(tmp_path)
    selected: List[str] = []

    async def actions(pilot):
        await app.push_screen(ModelBrowserScreen(
            _options(), current_key="opencode_go:gpt-5.6-luna",
            on_select=selected.append, title="Select model",
        ))
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        from textual.widgets import Input, OptionList, Static

        # Type a real search query into the focused search box.
        screen.query_one("#model-browser-search", Input).focus()
        await pilot.pause()
        await pilot.press("q", "w", "e", "n")
        await pilot.pause()
        assert screen._search_text == "qwen"
        assert [o.model_id for o in screen._filtered] == ["qwen3.8-max"]
        # Filtered count appears only while filtering (secondary).
        assert str(screen.query_one("#model-browser-count", Static).render().plain or "").strip() == "1 of 7"
        # Clear the query with real backspaces.
        await pilot.press("backspace", "backspace", "backspace", "backspace")
        await pilot.pause()
        assert screen._search_text == ""
        assert len(screen._filtered) == 7
        assert str(screen.query_one("#model-browser-count", Static).render().plain or "").strip() == ""
        # Cycle the provider filter with [ / ] from the list.
        screen.query_one("#model-browser-list", OptionList).focus()
        await pilot.pause()
        await pilot.press("]")
        await pilot.pause()
        assert screen._provider_filter == "my_gateway"
        assert [o.model_id for o in screen._filtered] == ["custom-model-x"]
        await pilot.press("[")
        await pilot.pause()
        assert screen._provider_filter == "all"
        assert len(screen._filtered) == 7
        # Enter on the highlighted (current) model selects it.
        screen.query_one("#model-browser-list", OptionList).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert selected == ["opencode_go:gpt-5.6-luna"]

    run_headless(app, actions, size=(120, 32))


def test_keyboard_manage_entry_reachable(tmp_path: Path) -> None:
    """The trailing manage-providers row is keyboard-reachable."""
    app = make_app(tmp_path)
    selected: List[str] = []

    async def actions(pilot):
        await app.push_screen(ModelBrowserScreen(
            _options(), current_key="opencode_go:gpt-5.6-luna",
            on_select=selected.append, title="Select model",
        ))
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        from textual.widgets import OptionList

        option_list = screen.query_one("#model-browser-list", OptionList)
        assert option_list.option_count == len(screen._filtered) + 1
        option_list.focus()
        await pilot.pause()
        option_list.highlighted = len(screen._filtered)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert selected == ["providers:manage"]

    run_headless(app, actions, size=(80, 24))


def test_provider_cycle_button_click(tmp_path: Path) -> None:
    """Clicking the filter button cycles All -> first provider quietly."""
    app = make_app(tmp_path)

    async def actions(pilot):
        await app.push_screen(ModelBrowserScreen(
            _options(), current_key="opencode_go:gpt-5.6-luna",
            on_select=lambda value: None, title="Select model",
        ))
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert screen._provider_filter == "all"
        await pilot.click("#model-browser-provider-button")
        await pilot.pause()
        first_provider = screen._providers[0].provider_id
        assert screen._provider_filter == first_provider
        assert all(o.provider == first_provider for o in screen._filtered)
        text = _svg_text(app)
        assert screen._providers[0].label in text
        assert "Provider:" not in text
        assert "ready" not in text.lower()

    run_headless(app, actions, size=(120, 32))


def _many_options(count: int) -> List[ModelOption]:
    return [
        ModelOption(
            "demo_prov", f"test-model-{index:02d}", f"Test Model {index:02d}",
            detail="", available=True, unavailable_reason=None,
            protocol="chat_completions", provider_label="Demo Provider",
        )
        for index in range(count)
    ]


def _assert_highlight_visible(option_list: Any) -> None:
    """The highlighted row is valid and inside the visible window, so the
    scrollbar thumb and the selection always agree."""
    highlighted = option_list.highlighted
    assert highlighted is not None
    assert 0 <= highlighted < option_list.option_count
    line = option_list._index_to_line.get(highlighted)
    assert line is not None
    top = int(option_list.scroll_y)
    height = int(option_list.scrollable_content_region.height)
    assert height > 0
    assert top <= line < top + height, (highlighted, line, top, height)


def test_long_list_keyboard_keeps_highlight_visible(tmp_path: Path) -> None:
    """End/Home/arrows on a long picker keep the selection scrolled into view."""
    app = make_app(tmp_path)

    async def actions(pilot):
        await app.push_screen(ModelBrowserScreen(
            _many_options(30), current_key=None,
            on_select=lambda value: None, title="Select model",
        ))
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        from textual.widgets import Input, OptionList

        option_list = screen.query_one("#model-browser-list", OptionList)
        assert option_list.option_count == 31  # 30 models + manage entry
        option_list.focus()
        await pilot.pause()
        _assert_highlight_visible(option_list)
        # Jump to the end with a real keypress: the last row (manage
        # entry) must scroll into view, moving the scrollbar with it.
        await pilot.press("end")
        await pilot.pause()
        assert option_list.highlighted == 30
        assert int(option_list.scroll_y) > 0
        _assert_highlight_visible(option_list)
        # Home returns to the top.
        await pilot.press("home")
        await pilot.pause()
        assert option_list.highlighted == 0
        _assert_highlight_visible(option_list)
        # Arrow keys walk the selection while staying visible.
        await pilot.press("down", "down", "down", "down", "down")
        await pilot.pause()
        assert option_list.highlighted == 5
        _assert_highlight_visible(option_list)
        # Page keys move the selection while staying visible.
        await pilot.press("pagedown")
        await pilot.pause()
        assert option_list.highlighted is not None and option_list.highlighted > 5
        _assert_highlight_visible(option_list)
        # Narrowing the list via search resets to a valid visible row.
        screen.query_one("#model-browser-search", Input).focus()
        await pilot.pause()
        await pilot.press("m", "o", "d", "e", "l", "-", "2")
        await pilot.pause()
        assert [o.model_id for o in screen._filtered] == [f"test-model-{i:02d}" for i in range(20, 30)]
        _assert_highlight_visible(option_list)
        # Clearing the search restores the full list with a valid row.
        await pilot.press("backspace", "backspace", "backspace", "backspace",
                          "backspace", "backspace", "backspace")
        await pilot.pause()
        assert len(screen._filtered) == 30
        _assert_highlight_visible(option_list)
        # Escape cancels back to the previous screen.
        screen.query_one("#model-browser-list", OptionList).focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ModelBrowserScreen)

    run_headless(app, actions, size=(120, 32))


def test_catalog_browser_filter_keeps_valid_highlight(tmp_path: Path) -> None:
    """Filtering the 62-model catalog browser never strands the highlight
    past the end of the list and always leaves a visible selection."""
    from agentic_debugger.application.provider_connections import (
        DiscoveredProviderModel,
    )
    from agentic_debugger.ui.provider_dialogs import ModelCatalogBrowserScreen
    from textual.widgets import Input, OptionList

    models = tuple(
        DiscoveredProviderModel(
            kind="demo",
            model_id=f"demo-model-{index:02d}",
            display_name=f"Demo Model {index:02d}",
            protocol="chat_completions",
            runnable=True,
        )
        for index in range(62)
    )
    app = make_app(tmp_path)

    async def actions(pilot):
        await app.push_screen(ModelCatalogBrowserScreen(
            provider_id="demo", provider_name="Demo", models=models,
        ))
        await pilot.pause()
        await pilot.pause()
        browser = app.screen
        assert isinstance(browser, ModelCatalogBrowserScreen)
        option_list = browser.query_one("#catalog-models-list", OptionList)
        assert option_list.option_count == 62
        assert option_list.highlighted == 0
        # Walk deep into the list: the highlight must stay visible.
        option_list.focus()
        await pilot.pause()
        await pilot.press("end")
        await pilot.pause()
        assert option_list.highlighted == 61
        assert int(option_list.scroll_y) > 0
        _assert_highlight_visible(option_list)
        # Filtering to one row resets the highlight instead of leaving
        # it stranded at index 61 (no visible selection, stale scroll).
        filter_input = browser.query_one("#catalog-filter-input", Input)
        filter_input.value = "model-05"
        await pilot.pause()
        assert option_list.option_count == 1
        assert option_list.highlighted == 0
        _assert_highlight_visible(option_list)
        # Clearing restores the full list and the previously visible row.
        filter_input.value = ""
        await pilot.pause()
        assert option_list.option_count == 62
        assert option_list.highlighted == 5
        _assert_highlight_visible(option_list)
        # Escape closes back to the previous screen.
        option_list.focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ModelCatalogBrowserScreen)

    run_headless(app, actions, size=(110, 30))
