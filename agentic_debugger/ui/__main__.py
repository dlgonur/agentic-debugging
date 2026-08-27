"""``python -m agentic_debugger.ui`` — the Local Application V1 launch command.

Usage::

    python -m agentic_debugger.ui [--root DIR]

``--root`` selects the application-owned history root (default:
``%LOCALAPPDATA%\\AgenticDebugger`` on Windows, ``~/AgenticDebugger``
elsewhere).  The application is full-screen and requires no GPU, model
provider, WSL, or campaign infrastructure.  Deterministic sessions are
application-controlled offline execution; configured command-model sessions
launch a user-configured local command (trusted user configuration) whose
capabilities are those of that executable under the host OS -- V1 does not
enforce child-process network isolation.  It requires the optional ``app``
extra (Textual); launching without it prints a concise installation
instruction instead of an import traceback.
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
            "application over app-owned session history, deterministic "
            "offline sessions, configured command-model sessions, and "
            "Local Project Debug sessions."
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
    parser.add_argument(
        "--project",
        default=None,
        help=(
            "Prefill Local Project Debug with a project path "
            "(absolute or relative to the shell launch cwd; '.' means launch cwd)."
        ),
    )
    return parser


def _require_textual() -> None:
    """Fail with a concise installation instruction, never a traceback."""
    try:
        import textual  # noqa: F401
    except ImportError as exc:
        print(
            "The Local Application V1 TUI requires the optional 'app' extra "
            "(Textual).\n"
            "Install it with:  python -m pip install -e '.[app]'\n"
            f"(missing import: {exc.name})",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _require_textual()
    from agentic_debugger.ui.app import LocalApplicationV1
    from agentic_debugger.application.local_project import capture_launch_cwd

    # Preserve shell cwd before any root handling
    capture_launch_cwd()
    app = LocalApplicationV1(history_root=args.root, initial_project=args.project)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
