"""``python -m agentic_debugger.ui`` — the Local Application V1 launch command.

Usage::

    python -m agentic_debugger.ui [--root DIR]

``--root`` selects the application-owned history root (default:
``%LOCALAPPDATA%\\AgenticDebugger`` on Windows, ``~/AgenticDebugger``
elsewhere).  The application is full-screen, offline, and requires no GPU,
model provider, network, WSL, or campaign infrastructure.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentic_debugger.ui",
        description=(
            "Launch the Local Application V1 replay-first Textual "
            "application over app-owned session history and deterministic "
            "offline sessions."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        help=(
            "Application-owned history root (default: "
            "%%LOCALAPPDATA%%/AgenticDebugger or ~/AgenticDebugger)."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    from agentic_debugger.ui.app import LocalApplicationV1

    args = build_parser().parse_args(argv)
    app = LocalApplicationV1(history_root=args.root)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
