"""Visual design tokens for the Agentic Debugger terminal application.

The Textual theme and Rich render helpers share this palette so presentation
code describes semantic roles instead of inventing local colours.  The scene
is a low-light engineering workspace: cool cyan marks interaction and live
signal, while warm amber is reserved for evidence and verifier authority.
"""

from __future__ import annotations

from textual.theme import Theme


THEME_NAME = "agentic-debugger"

# Base palette.  These values are also used by Rich ``Text`` renderers, where
# Textual CSS variables are not available.
CANVAS = "#07131C"
SURFACE = "#0C1A24"
PANEL = "#102430"
PANEL_RAISED = "#15303E"
PRIMARY = "#49D8FF"
SECONDARY = "#A98BFF"
EVIDENCE = "#FFB454"
EVIDENCE_SURFACE = "#3A2B12"
FOREGROUND = "#E7F2F7"
MUTED = "#91A8B5"
FAINT = "#6D8794"
LINE = "#294655"
LINE_STRONG = "#3D687C"
SUCCESS = "#45E0A8"
WARNING = "#FFCA72"
ERROR = "#FF7185"
DEBUGGER = "#E88CFF"
TOOL = "#5CC8C8"
CODE_FUNCTION = "#C5A5FF"
CODE_STRING = "#A8D8F0"


APP_THEME_VARIABLES: dict[str, str] = {
    "canvas-deep": CANVAS,
    "surface-raised": PANEL_RAISED,
    "line": LINE,
    "line-strong": LINE_STRONG,
    "text-faint": FAINT,
    "evidence": EVIDENCE,
    "focus-ring": PRIMARY,
    "selection": "#19485B",
    "live": PRIMARY,
    "replay": SUCCESS,
    "recorded": SECONDARY,
    "debugger": DEBUGGER,
    "tool": TOOL,
}


AGENTIC_DEBUGGER_THEME = Theme(
    name=THEME_NAME,
    primary=PRIMARY,
    secondary=SECONDARY,
    accent=EVIDENCE,
    foreground=FOREGROUND,
    background=CANVAS,
    surface=SURFACE,
    panel=PANEL,
    success=SUCCESS,
    warning=WARNING,
    error=ERROR,
    dark=True,
    variables={
        **APP_THEME_VARIABLES,
        "border": LINE,
        "border-blurred": "#1A313E",
        "block-cursor-background": "#19485B",
        "block-cursor-foreground": FOREGROUND,
        "footer-background": SURFACE,
        "footer-foreground": MUTED,
        "footer-key-foreground": PRIMARY,
        "input-selection-background": "#19485B",
        "scrollbar": LINE_STRONG,
        "scrollbar-background": SURFACE,
        "scrollbar-hover": PRIMARY,
    },
)


__all__ = [
    "AGENTIC_DEBUGGER_THEME",
    "APP_THEME_VARIABLES",
    "CANVAS",
    "CODE_FUNCTION",
    "CODE_STRING",
    "DEBUGGER",
    "ERROR",
    "EVIDENCE",
    "EVIDENCE_SURFACE",
    "FAINT",
    "FOREGROUND",
    "LINE",
    "LINE_STRONG",
    "MUTED",
    "PANEL",
    "PANEL_RAISED",
    "PRIMARY",
    "SECONDARY",
    "SUCCESS",
    "SURFACE",
    "THEME_NAME",
    "TOOL",
    "WARNING",
]
