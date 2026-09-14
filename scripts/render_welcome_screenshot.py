#!/usr/bin/env python3
"""Deterministic offline regeneration of the README welcome screenshot.

Regenerates ``docs/assets/agentic-debugger-welcome.png`` from the REAL
welcome surface code (not a hand-drawn mock):

- banner text is imported from ``agentic_debugger.ui.screens_home``
  (``BANNER_WIDE_SLANT`` / ``BANNER_3LINE``) and selected with the same
  ``width >= 102`` rule as ``HomeScreen.update_content``;
- the five action rows are constructed as the real ``HomeActionRow``
  widgets with the same ``(key, title, description, action_id)`` literals
  as ``HomeScreen.compose`` and rendered through the real
  ``HomeActionRow.render()`` method (both the focused and unfocused
  branches; focus is faked deterministically because there is no running
  app headless — see ``build_row_texts``);
- the footer vocabulary (``↑/↓ Select   Enter Open   Ctrl+C Quit`` with
  cyan keys) matches the ``HomeScreen`` footer markup;
- semantic colors come from ``agentic_debugger.ui.theme``
  (cyan = live/focus, faint/muted text hierarchy, surface/canvas
  tonal layers, selection fill) per ``DESIGN.md`` signal discipline.

Authoritative geometry is one representative size: a 120x30 terminal
(the README geometry). At 120 columns the wide rule applies
(container 96, wide slant banner). History state is pinned to a fresh
install (0 recorded sessions, ``"No recorded sessions"``) so the output
has no clock, seed, or filesystem dependency.

Font handling: a monospace TrueType font resolved in a pinned order
(``--font-path`` > ``$AGENTIC_DEBUGGER_SCREENSHOT_FONT`` > well-known
system monospace paths > Pillow-bundled fallback), drawn cell by cell on
a fixed cell grid. Bold is a synthetic double-strike so only one font
file is needed. The chosen font, geometry, Pillow version, and output
SHA-256 are printed on every run.

Usage::

    python scripts/render_welcome_screenshot.py --help
    python scripts/render_welcome_screenshot.py
    python scripts/render_welcome_screenshot.py --check

Offline only: no provider, network, credentials, worker, or display.
Requires Pillow (``pip install pillow``) for PNG rasterization.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rich.text import Text  # noqa: E402

from agentic_debugger.ui.screens_home import (  # noqa: E402
    BANNER_3LINE,
    BANNER_WIDE_SLANT,
    HomeActionRow,
)
from agentic_debugger.ui.theme import (  # noqa: E402
    CANVAS,
    FAINT,
    FOREGROUND,
    LINE,
    MUTED,
    PANEL,
    PRIMARY,
    SURFACE,
)

# The focused-row fill. ``HomeScreen`` CSS uses ``$selection`` for
# ``HomeActionRow:focus``; the token lives in the app theme variables.
SELECTION = "#19485B"
# Unfocused key-chip fill, from ``HomeActionRow.render`` (``on #102430``).
CHIP_REST = "#102430"

# Authoritative terminal geometry (one representative size, README geometry).
TERMINAL_WIDTH = 120
TERMINAL_HEIGHT = 30
# Mirrors ``HomeScreen.update_content``: ``is_wide = self.size.width >= 102``.
WIDE_THRESHOLD = 102
CONTAINER_WIDE = 96
CONTAINER_NARROW = 76

# Fixed raster cells (terminal cell grid -> pixels). When --cell-width /
# --cell-height are omitted, the cell is measured from the resolved font
# (advance x ascent+descent, like a real terminal) so box/block glyphs
# tile seamlessly; explicit flags override the measurement.
CELL_WIDTH: Optional[int] = None
CELL_HEIGHT: Optional[int] = None
CELL_WIDTH_FALLBACK = 12
CELL_HEIGHT_FALLBACK = 23
FONT_SIZE = 22

# Pinned history state: a fresh install has no recorded sessions, so the
# history row is clock- and filesystem-independent.
DEFAULT_HISTORY_SESSIONS = 0

FOOTER_MARKUP = (
    f"[bold {PRIMARY}]\u2191/\u2193[/] Select   "
    f"[bold {PRIMARY}]Enter[/] Open   "
    f"[bold {PRIMARY}]Ctrl+C[/] Quit"
)

# Well-known monospace fonts, first existing file wins. DejaVu first
# (common on Linux), then Windows monospace faces. The Pillow-bundled
# fallback needs no OS font at all.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/fonts-dejavu/DejaVuSansMono.ttf",
    r"C:\Windows\Fonts\consola.ttf",
    r"C:\Windows\Fonts\CascadiaMono.ttf",
    r"C:\Windows\Fonts\cour.ttf",
)


def history_description(count: int) -> str:
    """Mirror ``HomeScreen.refresh_history`` for a pinned session count."""
    if count < 0:
        raise ValueError("history session count must be >= 0")
    if count == 0:
        return "No recorded sessions"
    if count == 1:
        return "1 recorded session"
    return f"{count} recorded sessions"


def welcome_rows(history_sessions: int = DEFAULT_HISTORY_SESSIONS) -> list[tuple[str, str, str, str]]:
    """The ``(key, title, description, action_id)`` rows of ``HomeScreen``."""
    return [
        ("S", "Start Debugging", "Curated task or Capability Ladder", "action-start"),
        ("P", "Debug Local Project", "Debug a local Git repository", "action-local"),
        ("M", "Model Providers", "Manage external endpoints & API keys", "action-providers"),
        ("H", "Session History", history_description(history_sessions), "action-history"),
        ("?", "Help & Architecture", "System reference", "action-help"),
    ]


def select_banner(width: int) -> tuple[str, int]:
    """Mirror ``HomeScreen.update_content`` banner/container selection."""
    if width >= WIDE_THRESHOLD:
        return BANNER_WIDE_SLANT, CONTAINER_WIDE
    return BANNER_3LINE, CONTAINER_NARROW


def build_row_texts(
    rows: list[tuple[str, str, str, str]],
) -> list[Text]:
    """Render rows through the real ``HomeActionRow.render()``.

    The first row is rendered through the focused branch (``on_mount``
    focuses ``#action-start``); the rest through the unfocused branch.
    ``has_focus`` is a Textual reactive that needs a running app, so it
    is faked deterministically per row and always restored.
    """
    from unittest.mock import patch

    texts: list[Text] = []
    for index, (key, title, desc, action_id) in enumerate(rows):
        row = HomeActionRow(key, title, desc, action_id)
        focused = index == 0
        patcher = patch.object(
            HomeActionRow,
            "has_focus",
            new=property(lambda self, _f=focused: _f),
        )
        patcher.start()
        try:
            rendered = row.render()
        finally:
            patcher.stop()
        if not isinstance(rendered, Text):
            rendered = Text(str(rendered))
        texts.append(rendered)
    return texts


def build_banner_text(banner: str) -> Text:
    return Text(banner, style=f"bold {PRIMARY}")


def build_footer_text() -> Text:
    return Text.from_markup(FOOTER_MARKUP)


def _parse_style(style: str) -> tuple[Optional[str], Optional[str], bool]:
    """Parse a Rich style string into ``(fg_hex, bg_hex, bold)``."""
    fg: Optional[str] = None
    bg: Optional[str] = None
    bold = False
    tokens = str(style or "").split()
    for position, token in enumerate(tokens):
        low = token.lower()
        if low == "bold":
            bold = True
        elif token.startswith("#") and len(token) == 7:
            if position > 0 and tokens[position - 1].lower() == "on":
                bg = token.upper()
            elif fg is None:
                fg = token.upper()
    return fg, bg, bold


def text_to_cells(
    text: Text,
    width: int,
    default_fg: str,
    default_bg: Optional[str] = None,
) -> list[list[tuple[str, str, Optional[str], bool]]]:
    """Expand one ``Text`` (possibly multi-line) to styled cell rows.

    Fail-closed: a line wider than ``width`` raises instead of clipping.
    """
    base_fg, base_bg, base_bold = _parse_style(text.style or "")
    lines = text.plain.split("\n")
    out: list[list[tuple[str, str, Optional[str], bool]]] = []
    for line in lines:
        if len(line) > width:
            raise ValueError(
                f"screenshot content overflows allotted width "
                f"({len(line)} > {width}): {line!r}"
            )
        fg_row = [base_fg or default_fg] * len(line)
        bg_row: list[Optional[str]] = [base_bg if base_bg is not None else default_bg] * len(line)
        bold_row = [base_bold] * len(line)
        for span in text.spans:
            span_fg, span_bg, span_bold = _parse_style(span.style or "")
            for offset in range(span.start, min(span.end, len(line))):
                if span_fg is not None:
                    fg_row[offset] = span_fg
                if span_bg is not None:
                    bg_row[offset] = span_bg
                if span_bold:
                    bold_row[offset] = True
        cells = [
            (ch, fg_row[i], bg_row[i], bold_row[i]) for i, ch in enumerate(line)
        ]
        cells += [(" ", default_fg, default_bg, False)] * (width - len(line))
        out.append(cells)
    return out


def resolve_font(font_path: Optional[str] = None, font_size: int = FONT_SIZE) -> tuple[object, dict[str, str]]:
    """Resolve the monospace font in the pinned order (no OS guessing)."""
    try:
        from PIL import ImageFont
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required to render the welcome screenshot: "
            "pip install pillow"
        ) from exc
    candidates: list[tuple[str, str]] = []
    if font_path:
        candidates.append((font_path, "explicit --font-path"))
    env_path = os.environ.get("AGENTIC_DEBUGGER_SCREENSHOT_FONT")
    if env_path:
        candidates.append((env_path, "env AGENTIC_DEBUGGER_SCREENSHOT_FONT"))
    candidates += [(path, "well-known system monospace") for path in FONT_CANDIDATES]
    for path, source in candidates:
        if path and Path(path).is_file():
            font = ImageFont.truetype(path, font_size)
            return font, {"font": str(path), "font_source": source}
    font = ImageFont.load_default(size=font_size)
    return font, {"font": "pillow-bundled ImageFont.load_default", "font_source": "fallback"}


def measure_cell(font: object) -> tuple[int, int]:
    """Measure one terminal cell from the font, like a real terminal.

    Width is the ceiling of the widest advance over a representative
    sample (monospace faces are uniform); height is ascent + descent.
    Falls back to fixed constants for fonts without metrics.
    """
    import math

    try:
        sample = "MWmw01|-\u2502\u2500\u2588\u2584\u2580\u203a\u2191\u2193?# "
        advances = [float(font.getlength(ch)) for ch in sample]  # type: ignore[attr-defined]
        cell_w = max(1, int(math.ceil(max(advances))))
        ascent, descent = font.getmetrics()  # type: ignore[attr-defined]
        cell_h = max(1, int(ascent + descent))
        return cell_w, cell_h
    except Exception:
        return CELL_WIDTH_FALLBACK, CELL_HEIGHT_FALLBACK


def _repo_head() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return "unknown"
    head = (completed.stdout or "").strip()
    return head or "unknown"


def render_png_bytes(
    *,
    width: int = TERMINAL_WIDTH,
    height: int = TERMINAL_HEIGHT,
    cell_width: Optional[int] = None,
    cell_height: Optional[int] = None,
    font_size: int = FONT_SIZE,
    font_path: Optional[str] = None,
    history_sessions: int = DEFAULT_HISTORY_SESSIONS,
) -> tuple[bytes, dict[str, object]]:
    """Render the welcome surface to deterministic PNG bytes plus a record."""
    from PIL import Image, ImageDraw

    if width <= 0 or height <= 0:
        raise ValueError("terminal width/height must be positive")
    if cell_width is not None and cell_width <= 0:
        raise ValueError("cell width must be positive")
    if cell_height is not None and cell_height <= 0:
        raise ValueError("cell height must be positive")

    banner_text, container_width = select_banner(width)
    if container_width > width:
        raise ValueError("container wider than terminal")
    rows = welcome_rows(history_sessions)
    row_texts = build_row_texts(rows)
    banner = build_banner_text(banner_text)
    footer = build_footer_text()

    # -- cell grid ------------------------------------------------------
    # Grid cells are (char, fg_hex, bg_hex|None, bold); None bg inherits
    # the region background decided at placement time.
    grid_fg: list[list[str]] = [[FOREGROUND for _ in range(width)] for _ in range(height)]
    grid_bg: list[list[str]] = [[CANVAS for _ in range(width)] for _ in range(height)]
    grid_bold: list[list[bool]] = [[False for _ in range(width)] for _ in range(height)]
    grid_ch: list[list[str]] = [[" " for _ in range(width)] for _ in range(height)]

    def place(
        cells: list[list[tuple[str, str, Optional[str], bool]]],
        ox: int,
        oy: int,
        region_bg: str,
    ) -> None:
        for dy, line in enumerate(cells):
            y = oy + dy
            if not 0 <= y < height:
                raise ValueError("screenshot content overflows terminal height")
            for dx, (ch, fg, bg, bold) in enumerate(line):
                x = ox + dx
                if not 0 <= x < width:
                    raise ValueError("screenshot content overflows terminal width")
                grid_ch[y][x] = ch
                grid_fg[y][x] = fg
                grid_bg[y][x] = bg if bg is not None else region_bg
                grid_bold[y][x] = bold

    footer_cells = text_to_cells(footer, width - 4, FAINT)
    footer_line = footer_cells[0]
    if len(footer_cells) != 1:
        raise ValueError("footer must render as a single line")

    panel_x = (width - container_width) // 2
    banner_lines = banner.plain.split("\n")
    banner_w = max(len(line) for line in banner_lines)
    if banner_w > container_width:
        raise ValueError("banner wider than container")
    banner_x = panel_x + (container_width - banner_w) // 2

    row_count = len(row_texts)
    panel_h = 1 + 1 + (row_count + (row_count - 1)) + 1 + 1
    content_h = len(banner_lines) + 1 + panel_h
    avail_h = height - 1  # last row is the docked footer bar
    if content_h > avail_h:
        raise ValueError("welcome content overflows terminal height")
    banner_y = (avail_h - content_h) // 2
    panel_y = banner_y + len(banner_lines) + 1

    # Banner on the deep canvas.
    place(text_to_cells(banner, banner_w, PRIMARY), banner_x, banner_y, CANVAS)

    # Actions panel: rounded Textual border glyphs in LINE on canvas,
    # SURFACE interior.
    panel_cells_x1 = panel_x + container_width - 1
    panel_y1 = panel_y + panel_h - 1
    for x in range(panel_x, panel_x + container_width):
        grid_bg[panel_y][x] = CANVAS
        grid_bg[panel_y1][x] = CANVAS
    for y in range(panel_y, panel_y1 + 1):
        grid_bg[y][panel_x] = CANVAS
        grid_bg[y][panel_cells_x1] = CANVAS
    grid_ch[panel_y][panel_x] = "\u256d"
    grid_ch[panel_y][panel_cells_x1] = "\u256e"
    grid_ch[panel_y1][panel_x] = "\u2570"
    grid_ch[panel_y1][panel_cells_x1] = "\u256f"
    for x in range(panel_x + 1, panel_cells_x1):
        grid_ch[panel_y][x] = "\u2500"
        grid_ch[panel_y1][x] = "\u2500"
        grid_fg[panel_y][x] = LINE
        grid_fg[panel_y1][x] = LINE
    for y in range(panel_y + 1, panel_y1):
        grid_ch[y][panel_x] = "\u2502"
        grid_ch[y][panel_cells_x1] = "\u2502"
        grid_fg[y][panel_x] = LINE
        grid_fg[y][panel_cells_x1] = LINE
    grid_fg[panel_y][panel_x] = LINE
    grid_fg[panel_y][panel_cells_x1] = LINE
    grid_fg[panel_y1][panel_x] = LINE
    grid_fg[panel_y1][panel_cells_x1] = LINE
    for y in range(panel_y + 1, panel_y1):
        for x in range(panel_x + 1, panel_cells_x1):
            grid_bg[y][x] = SURFACE
            grid_ch[y][x] = " "

    inner_x = panel_x + 1 + 2
    inner_w = container_width - 2 - 4
    for index, row_text in enumerate(row_texts):
        y = panel_y + 1 + 1 + index * 2
        region_bg = SELECTION if index == 0 else SURFACE
        for x in range(panel_x + 1, panel_cells_x1):
            grid_bg[y][x] = region_bg
        place(text_to_cells(row_text, inner_w, FOREGROUND), inner_x, y, region_bg)

    # Docked footer bar: full-width SURFACE strip, faint labels, cyan keys.
    for x in range(width):
        grid_bg[height - 1][x] = SURFACE
        grid_ch[height - 1][x] = " "
        grid_fg[height - 1][x] = FAINT
    for dx, (ch, fg, bg, bold) in enumerate(footer_line[: width - 4]):
        x = 2 + dx
        grid_ch[height - 1][x] = ch
        grid_fg[height - 1][x] = fg
        grid_bold[height - 1][x] = bold
        if bg is not None:
            grid_bg[height - 1][x] = bg

    # -- rasterize ------------------------------------------------------
    font, font_record = resolve_font(font_path, font_size)
    measured_w, measured_h = measure_cell(font)
    cell_width = measured_w if cell_width is None else cell_width
    cell_height = measured_h if cell_height is None else cell_height
    if cell_width <= 0 or cell_height <= 0:
        raise ValueError("cell width/height must be positive")
    try:
        import PIL

        pillow_version = PIL.__version__
    except Exception:
        pillow_version = "unknown"

    img = Image.new("RGB", (width * cell_width, height * cell_height), CANVAS)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        for x in range(width):
            px, py = x * cell_width, y * cell_height
            bg = grid_bg[y][x]
            if bg.upper() != CANVAS.upper():
                draw.rectangle([px, py, px + cell_width - 1, py + cell_height - 1], fill=bg)
            ch = grid_ch[y][x]
            if ch == " ":
                continue
            fg = grid_fg[y][x]
            try:
                bbox = font.getbbox(ch)
            except Exception:
                bbox = None
            if bbox:
                x0, y0, x1, y1 = bbox
                glyph_w = x1 - x0
                glyph_h = y1 - y0
                dx = (cell_width - glyph_w) // 2 - x0 if glyph_w else 0
                dy = (cell_height - glyph_h) // 2 - y0 if glyph_h else 0
            else:
                dx = 2
                dy = 2
            draw.text((px + dx, py + dy), ch, font=font, fill=fg)
            if grid_bold[y][x]:
                # Synthetic bold: deterministic double-strike, one pixel.
                draw.text((px + dx + 1, py + dy), ch, font=font, fill=fg)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", compress_level=6)
    png_bytes = buffer.getvalue()
    digest = hashlib.sha256(png_bytes).hexdigest()

    record: dict[str, object] = {
        "output": "docs/assets/agentic-debugger-welcome.png",
        "sha256": digest,
        "bytes": len(png_bytes),
        "terminal": {"width": width, "height": height},
        "container_width": container_width,
        "banner": "wide-slant" if banner_text == BANNER_WIDE_SLANT else "compact-3line",
        "cell": {"width": cell_width, "height": cell_height},
        "font_size": font_size,
        **font_record,
        "pillow": pillow_version,
        "history_sessions": history_sessions,
        "history_description": history_description(history_sessions),
        "rows": [action_id for _, _, _, action_id in rows],
        "repo_head": _repo_head(),
    }
    return png_bytes, record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministically regenerate docs/assets/agentic-debugger-"
            "welcome.png offline from the real Home/welcome surface "
            "(120x30 authoritative geometry, pinned fresh-install history, "
            "DESIGN.md forensic-console colors)."
        )
    )
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "docs" / "assets" / "agentic-debugger-welcome.png"),
        help="PNG output path (default: docs/assets/agentic-debugger-welcome.png)",
    )
    parser.add_argument("--width", type=int, default=TERMINAL_WIDTH)
    parser.add_argument("--height", type=int, default=TERMINAL_HEIGHT)
    parser.add_argument(
        "--cell-width",
        type=int,
        default=None,
        help="Cell width in pixels (default: measured from the font advance)",
    )
    parser.add_argument(
        "--cell-height",
        type=int,
        default=None,
        help="Cell height in pixels (default: measured font ascent+descent)",
    )
    parser.add_argument("--font-size", type=int, default=FONT_SIZE)
    parser.add_argument(
        "--font-path",
        default=None,
        help="Explicit monospace .ttf (overrides the pinned resolution order)",
    )
    parser.add_argument(
        "--history-sessions",
        type=int,
        default=DEFAULT_HISTORY_SESSIONS,
        help="Pinned recorded-session count (default 0: fresh install)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Regenerate in memory and compare with --output (exit 2 if stale)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        png_bytes, record = render_png_bytes(
            width=args.width,
            height=args.height,
            cell_width=args.cell_width,
            cell_height=args.cell_height,
            font_size=int(args.font_size),
            font_path=args.font_path,
            history_sessions=args.history_sessions,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: refusing to render a clipped screenshot: {exc}", file=sys.stderr)
        return 1

    record["output"] = os.path.relpath(args.output, str(REPO_ROOT)).replace(os.sep, "/")
    digest = str(record["sha256"])
    print(json.dumps(record, indent=2, sort_keys=True))
    print(f"sha256: {digest}  {record['output']}")

    output_path = Path(args.output)
    if args.check:
        try:
            current = output_path.read_bytes()
        except FileNotFoundError:
            print(f"stale: {record['output']} does not exist", file=sys.stderr)
            return 2
        current_digest = hashlib.sha256(current).hexdigest()
        if current_digest != digest:
            print(
                f"stale: {record['output']} sha256 {current_digest} != "
                f"regenerated {digest}",
                file=sys.stderr,
            )
            return 2
        print(f"fresh: {record['output']} matches regenerated sha256 {digest}")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(png_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
