"""Contract for the deterministic README welcome-screenshot regeneration.

``scripts/render_welcome_screenshot.py`` must track the real Home surface:
its row literals, banner selection rule, footer vocabulary, and history
description rule come from ``agentic_debugger.ui.screens_home``. These
tests pin that drift guard and the script's determinism without running
a terminal.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import render_welcome_screenshot as renderer
from agentic_debugger.ui.screens_home import BANNER_3LINE, BANNER_WIDE_SLANT


def _compose_rows_from_source() -> list[tuple[str, str, str, str]]:
    """Extract the ``HomeActionRow(...)`` literals from ``HomeScreen.compose``."""
    source = (REPO_ROOT / "agentic_debugger" / "ui" / "screens_home.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    rows: list[tuple[str, str, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "HomeActionRow":
            continue
        if len(node.args) < 4:
            continue
        try:
            values = [arg.value for arg in node.args[:4]]  # type: ignore[attr-defined]
        except AttributeError:
            continue
        if all(isinstance(value, str) for value in values):
            rows.append((values[0], values[1], values[2], ""))
    # ``compose`` builds exactly the five welcome rows in focus order.
    assert len(rows) == 5, f"expected 5 HomeActionRow literals, found {len(rows)}"
    return rows


def test_rows_track_home_compose() -> None:
    source_rows = _compose_rows_from_source()
    script_rows = renderer.welcome_rows(0)
    for (source_key, source_title, source_desc, _), (key, title, desc, _action) in zip(
        source_rows, script_rows
    ):
        assert (key, title, desc) == (source_key, source_title, source_desc)
    action_ids = [action for _, _, _, action in script_rows]
    assert action_ids == [
        "action-start",
        "action-local",
        "action-providers",
        "action-history",
        "action-help",
    ]
    # The regenerated screenshot must show the Model Providers affordance.
    assert script_rows[2][0] == "M"
    assert script_rows[2][1] == "Model Providers"


def test_banner_rule_tracks_home_update_content() -> None:
    source = (REPO_ROOT / "agentic_debugger" / "ui" / "screens_home.py").read_text(
        encoding="utf-8"
    )
    assert "self.size.width >= 102" in source
    assert renderer.WIDE_THRESHOLD == 102
    banner_wide, container_wide = renderer.select_banner(120)
    assert banner_wide == BANNER_WIDE_SLANT
    assert container_wide == 96
    banner_compact, container_compact = renderer.select_banner(80)
    assert banner_compact == BANNER_3LINE
    assert container_compact == 76


def test_history_description_rule() -> None:
    assert renderer.history_description(0) == "No recorded sessions"
    assert renderer.history_description(1) == "1 recorded session"
    assert renderer.history_description(24) == "24 recorded sessions"


def test_footer_vocabulary() -> None:
    footer = renderer.build_footer_text()
    plain = footer.plain
    for token in ("\u2191/\u2193", "Select", "Enter", "Open", "Ctrl+C", "Quit"):
        assert token in plain, f"footer missing {token!r}"
    assert plain == "\u2191/\u2193 Select   Enter Open   Ctrl+C Quit"


def test_render_is_deterministic() -> None:
    first, first_record = renderer.render_png_bytes()
    second, second_record = renderer.render_png_bytes()
    assert first == second
    assert first[:8] == b"\x89PNG\r\n\x1a\n"
    digest = hashlib.sha256(first).hexdigest()
    assert first_record["sha256"] == digest
    assert second_record["sha256"] == digest
    cell = first_record["cell"]
    assert isinstance(cell, dict)
    import io

    from PIL import Image

    probe = Image.open(io.BytesIO(first))
    assert probe.size == (
        renderer.TERMINAL_WIDTH * int(cell["width"]),
        renderer.TERMINAL_HEIGHT * int(cell["height"]),
    )
