"""``python -m agentic_debugger.ui`` — launch Agentic Debugger.

Usage::

    agentic-debugger [--root DIR] [--project DIR]
    agentic-debugger --doctor
    agentic-debugger --list-sessions [--root DIR]
    agentic-debugger --export-session SESSION_ID [--output REPORT.md] [--root DIR]

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
import platform
import sys
from importlib.metadata import (
    PackageNotFoundError,
    version as distribution_version,
)
from importlib.resources import files
from typing import Optional, Sequence

from agentic_debugger import __version__


def collect_diagnostics() -> dict[str, object]:
    """Inspect the local install without launching the UI or touching providers."""

    try:
        textual_version: Optional[str] = distribution_version("textual")
    except PackageNotFoundError:
        textual_version = None

    try:
        curated_root = (
            files("agentic_debugger").joinpath("datasets").joinpath("curated")
        )
        curated_tasks = sum(
            1
            for entry in curated_root.iterdir()
            if entry.is_dir() and entry.joinpath("task.json").is_file()
        )
    except (FileNotFoundError, ModuleNotFoundError):
        curated_tasks = 0

    python_supported = sys.version_info >= (3, 11)
    providers: list[tuple[str, bool, Optional[str]]] = []
    try:
        from agentic_debugger.application.model_providers import (
            provider_availability,
        )

        providers = provider_availability()
    except Exception:
        providers = []
    ready = python_supported and textual_version is not None and curated_tasks >= 5
    return {
        "version": __version__,
        "python_version": platform.python_version(),
        "python_supported": python_supported,
        "textual_version": textual_version,
        "curated_tasks": curated_tasks,
        "providers": providers,
        "ready": ready,
    }


def render_diagnostics(diagnostics: dict[str, object]) -> int:
    """Print concise installation diagnostics and return a process status."""

    python_status = "ok" if diagnostics["python_supported"] else "unsupported"
    textual_version = diagnostics["textual_version"]
    textual_status = str(textual_version) if textual_version is not None else "missing"
    curated_tasks = int(diagnostics["curated_tasks"])
    task_status = "ok" if curated_tasks >= 5 else "missing package data"

    print(f"Agentic Debugger {diagnostics['version']}")
    print(f"Python: {diagnostics['python_version']} ({python_status})")
    print(f"Textual: {textual_status}")
    print(f"Curated task manifests: {curated_tasks} ({task_status})")
    for kind, available, reason in diagnostics["providers"]:  # type: ignore[union-attr]
        if available:
            print(f"Model provider {kind}: ready")
        else:
            print(f"Model provider {kind}: unavailable ({reason})")
    print(f"Status: {'READY' if diagnostics['ready'] else 'NOT READY'}")
    if textual_version is None:
        print('Install the UI dependency with: python -m pip install -e ".[app]"')
    return 0 if diagnostics["ready"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic-debugger",
        description=(
            "Launch the Agentic Debugger terminal application over local "
            "session history, deterministic "
            "offline sessions, configured command-model sessions, and "
            "Local Project Debug sessions."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--doctor",
        action="store_true",
        help=(
            "Check Python, Textual, and packaged task resources without "
            "launching the UI or contacting a provider."
        ),
    )
    modes.add_argument(
        "--list-sessions",
        action="store_true",
        help="List app-owned sessions without launching the UI.",
    )
    modes.add_argument(
        "--export-session",
        metavar="SESSION_ID",
        help=(
            "Render one validated session as a safe Markdown report without "
            "executing any recorded work."
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
    parser.add_argument(
        "--output",
        metavar="REPORT.md",
        help=(
            "Write --export-session to a new file instead of stdout; existing "
            "files are never overwritten."
        ),
    )
    return parser


def _require_textual() -> None:
    """Fail with a concise installation instruction, never a traceback."""
    try:
        import textual  # noqa: F401
    except ImportError as exc:
        print(
            "Agentic Debugger requires the optional 'app' extra "
            "(Textual).\n"
            "Install it with:  python -m pip install -e '.[app]'\n"
            f"(missing import: {exc.name})",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.output is not None and args.export_session is None:
        parser.error("--output requires --export-session")
    if args.doctor:
        return render_diagnostics(collect_diagnostics())
    if args.list_sessions or args.export_session is not None:
        from pathlib import Path

        from agentic_debugger import AgenticDebuggerError
        from agentic_debugger.application.history import (
            HistoryStore,
            default_history_root,
        )
        from agentic_debugger.application.reporting import (
            render_session_listing,
            render_session_report,
            write_session_report,
        )

        store = HistoryStore(Path(args.root) if args.root else default_history_root())
        if args.list_sessions:
            print(render_session_listing(store.list_sessions()), end="")
            return 0
        try:
            reopened = store.reopen(args.export_session)
            if args.output is None:
                print(render_session_report(reopened), end="")
            else:
                written = write_session_report(reopened, args.output)
                print(f"Session report written: {written}")
        except AgenticDebuggerError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
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
