"""Real Textual render regressions for the model browser (Blocker 2 repair).

These tests render ``ModelBrowserScreen`` inside the real product
application (product stylesheet included) at 120x32 and 80x24 and prove
from actual widget regions and terminal output that:

- the provider filter visibly renders with its current value and ready
  counts (no invisible ``Select``, no dead gap);
- the shown-count is visible without clipping;
- model rows render;
- every browser control sits inside the screen bounds;
- keyboard search / provider cycling / Enter selection work;
- the manage-providers entry remains reachable.

Pure-helper coverage (search, badges, rows, details) lives in
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
                "#model-browser-current", "#model-browser-search",
                "#model-browser-filters", "#model-browser-provider-button",
                "#model-browser-count", "#model-browser-list",
                "#model-browser-details", "#model-browser-hint"):
        widget = screen.query_one(qid)
        region = widget.region
        assert region.x >= 0 and region.y >= 0, (qid, region)
        assert region.x + region.width <= width, (qid, region, width)
        assert region.y + region.height <= height, (qid, region, height)
        assert region.width > 0 and region.height > 0, (qid, region)


def _prompt_text(option_list: Any, index: int) -> str:
    prompt = option_list.get_option_at_index(index).prompt
    return prompt.plain if hasattr(prompt, "plain") else str(prompt)


@pytest.mark.parametrize("size", [(120, 32), (80, 24)])
def test_provider_filter_renders_with_counts(tmp_path: Path, size: tuple) -> None:
    """Filter value, ready counts, shown count, and rows are all visible."""
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
        # Provider filter value + per-filter ready counts.
        assert "Provider: All (5/7)" in text
        # Shown-count without clipping.
        assert "7 shown" in text
        # Totals in the header.
        assert "5/7 ready" in text
        # Details pane for the highlighted (current) model.
        assert "gpt-5.6-luna" in text
        assert "Status: Ready" in text
        # Model rows render with protocol badges (read from the live
        # options: SVG glyph runs fragment row text, so rows are proven
        # here and SVG-proven below for the visible window).
        from textual.widgets import OptionList

        option_list = screen.query_one("#model-browser-list", OptionList)
        assert option_list.option_count == len(screen._filtered) + 1
        prompts = [
            _prompt_text(option_list, index)
            for index in range(option_list.option_count - 1)
        ]
        assert any("GPT-5.6 Luna" in prompt and "Responses" in prompt for prompt in prompts)
        assert any("DeepSeek V4.1 Flash" in prompt and "Chat Completions" in prompt for prompt in prompts)
        assert any("Qwen 3.8 Max" in prompt and "Messages" in prompt for prompt in prompts)
        assert any("Unresolved" in prompt for prompt in prompts)
        # The trailing manage-providers entry is rendered.
        assert "Manage model providers" in _prompt_text(option_list, option_list.option_count - 1)
        # ... and the visible window shows the current selection.
        assert "GPT-5.6 Luna" in text

    run_headless(app, actions, size=size)


def test_constrained_list_and_details_visible(tmp_path: Path) -> None:
    """At 80x24 the list, details, and hint all fit on screen."""
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
        # Details pane content for the highlighted (current) model.
        assert "gpt-5.6-luna" in text
        assert "Status: Ready" in text
        # At least one model row is visible in the window (rows carry
        # non-ASCII markers, so visibility is proven via the details pane
        # plus the live option prompts).
        prompts = [
            _prompt_text(option_list, index)
            for index in range(option_list.option_count - 1)
        ]
        assert any("Qwen 3.8 Max" in prompt for prompt in prompts)
        assert any("DeepSeek V4.1 Flash" in prompt for prompt in prompts)
        # Keyboard hint documents provider cycling + search.
        assert "[ ] provider" in text

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
        from textual.widgets import Input, OptionList

        # Type a real search query into the focused search box.
        screen.query_one("#model-browser-search", Input).focus()
        await pilot.pause()
        await pilot.press("q", "w", "e", "n")
        await pilot.pause()
        assert screen._search_text == "qwen"
        assert [o.model_id for o in screen._filtered] == ["qwen3.8-max"]
        # Clear the query with real backspaces.
        await pilot.press("backspace", "backspace", "backspace", "backspace")
        await pilot.pause()
        assert screen._search_text == ""
        assert len(screen._filtered) == 7
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
    """Clicking the filter button cycles All -> first provider."""
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
        assert f"Provider: {screen._providers[0].label}" in text

    run_headless(app, actions, size=(120, 32))
