"""Shared OptionList highlight/scroll-follow helper gates (Textual-free).

Proves :func:`agentic_debugger.ui.screens_shared.set_option_list_highlight`
keeps a valid, visible selection from stub lists: clamping, clearing,
explicit reveal, and never raising on hostile widget state.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.ui.screens_shared import (  # noqa: E402
    set_option_list_highlight,
)


class _StubOptionList:
    """Minimal OptionList double (highlighted + reveal protocol only)."""

    def __init__(self, count: int) -> None:
        self._count = count
        self.highlighted = None
        self.reveals = 0
        self.top_scrolls = 0

    @property
    def option_count(self) -> int:
        return self._count

    def scroll_to_highlight(self) -> None:
        self.reveals += 1

    def scroll_to(self, **kwargs) -> None:
        self.top_scrolls += 1


class TestSetOptionListHighlight:
    def test_valid_index_is_revealed(self):
        opt_list = _StubOptionList(8)
        set_option_list_highlight(opt_list, 3)
        assert opt_list.highlighted == 3
        assert opt_list.reveals == 1

    def test_index_clamps_to_live_range(self):
        opt_list = _StubOptionList(8)
        set_option_list_highlight(opt_list, 99)
        assert opt_list.highlighted == 7
        set_option_list_highlight(opt_list, -4)
        assert opt_list.highlighted == 0

    def test_non_integer_index_falls_back_to_zero(self):
        opt_list = _StubOptionList(8)
        set_option_list_highlight(opt_list, "nope")
        assert opt_list.highlighted == 0

    def test_none_clears_highlight(self):
        opt_list = _StubOptionList(8)
        opt_list.highlighted = 5
        set_option_list_highlight(opt_list, None)
        assert opt_list.highlighted is None
        assert opt_list.top_scrolls == 1

    def test_empty_list_is_noop(self):
        opt_list = _StubOptionList(0)
        set_option_list_highlight(opt_list, 0)
        assert opt_list.highlighted is None
        assert opt_list.reveals == 0

    def test_hostile_widget_never_raises(self):
        class _Broken:
            option_count = 4

            def __setattr__(self, name, value):
                if name == "highlighted":
                    raise RuntimeError("frozen")
                super().__setattr__(name, value)

            def scroll_to_highlight(self):
                raise RuntimeError("no viewport")

        set_option_list_highlight(_Broken(), 2)

        class _NoCount:
            pass

        set_option_list_highlight(_NoCount(), 2)
        set_option_list_highlight(None, 2)
