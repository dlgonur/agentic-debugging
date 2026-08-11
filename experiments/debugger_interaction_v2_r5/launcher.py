"""R5 — neutral cwd-safe pytest launcher and probe preparation.

Replaces the R3 hand-authored semantic runtime probe with a mechanically
generated debugger-only launcher derived from the task's frozen
``reproduction.argv`` (``python -m pytest <node> ...``).  The launcher:

- encodes NO bug semantics: no function name, call, anchor, expression, or
  expected behavior;
- resolves its own disposable fixture root via ``__file__`` and chdirs there
  before ``pytest.main``, so reproduction is independent of the operator or
  repository cwd;
- is guarded by ``if __name__ == "__main__"`` so the pytest-imported module
  copy never re-runs it recursively;
- is appended ONLY to the disposable runtime copy, strictly after the
  original-source metadata (source sha, line count, eligible breakpoint
  lines) has been frozen; the appended region always starts beyond
  ``original_source_line_count`` and is therefore never a model breakpoint
  candidate and never part of model-facing source.

``_ARGS`` is harness-only; the launcher text never enters model prompts.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from agentic_debugger.demo.tools import PdbProbe
from agentic_debugger.evaluation.task_schema import DebugTask


class R5LauncherError(ValueError):
    """Raised when the launcher cannot be derived mechanically (fail closed)."""


def task_target_module_path(task: DebugTask) -> str:
    """Mechanically select the debugged production module.

    Uses the public task constraint ``constraints.allowed_write_paths``:
    exactly one writable path, a relative ``.py`` file outside ``tests``.
    This design choice is reported explicitly in the R5 contract and
    evidence (allowed per the R5 non-oracle rule).
    """
    allowed = list(task.constraints.allowed_write_paths)
    candidates = [p for p in allowed if p.endswith(".py") and not p.startswith("tests/")]
    if len(candidates) != 1:
        raise R5LauncherError(
            "task must declare exactly one writable production .py path, "
            f"got {sorted(allowed)!r}"
        )
    return candidates[0]


def build_r5_launcher_source(reproduction_argv: Sequence[str]) -> str:
    """Generate the neutral guarded pytest launcher from the frozen argv.

    Supported pre-registered shape: ``["python", "-m", "pytest", <node>, ...]``.
    Anything else fails closed — no inference, no task semantics.
    """
    argv = list(reproduction_argv)
    if len(argv) < 4:
        raise R5LauncherError(
            f"reproduction argv too short for the supported shape: {argv!r}"
        )
    if argv[0] != "python" or argv[1] != "-m" or argv[2] != "pytest":
        raise R5LauncherError(
            "reproduction argv must match the pre-registered shape "
            f"'python -m pytest <node> ...', got {argv!r}"
        )
    pytest_args = argv[3:]
    for item in pytest_args:
        if not isinstance(item, str) or not item:
            raise R5LauncherError("pytest args must be non-empty strings")
    # Mechanical hermetic flags appended AFTER the frozen argv:
    #   -s  disable pytest's fd-based output capture, which would otherwise
    #       close the debugger worker's stdio fds (stdin EOF terminates the
    #       worker main loop); capture is test-output-only, never semantics.
    pytest_args = list(pytest_args) + ["-s"]
    args_literal = "[" + ", ".join(repr(a) for a in pytest_args) + "]"
    return (
        "\n\n"
        "# --- R5 neutral failing-execution launcher ---\n"
        "# Generated mechanically from task.reproduction.argv. Debugger-only\n"
        "# harness code; never part of model-facing source or any candidate.\n"
        "import os as _os\n"
        "import sys as _sys\n"
        "import pytest as _pytest\n\n"
        "# Hermetic execution: disable setuptools-entrypoint plugin autoload\n"
        "# (ambient plugins such as telemetry hooks can be pathologically slow\n"
        "# under the debugger trace; the failing test needs no plugins).\n"
        "_os.environ[\"PYTEST_DISABLE_PLUGIN_AUTOLOAD\"] = \"1\"\n\n"
        f"_ARGS = {args_literal}\n\n"
        "def _r5_failing_execution() -> None:\n"
        "    _fixture_root = _os.path.dirname(_os.path.abspath(__file__))\n"
        "    _previous_cwd = _os.getcwd()\n"
        "    _os.chdir(_fixture_root)\n"
        "    try:\n"
        "        _sys.exit(_pytest.main(_ARGS))\n"
        "    finally:\n"
        "        _os.chdir(_previous_cwd)\n\n"
        "if __name__ == \"__main__\":\n"
        "    _r5_failing_execution()\n"
    )


@dataclass(frozen=True)
class R5Probe:
    """Disposable debugger target with original-source boundary metadata."""

    probe: PdbProbe
    driver_start_line: int
    original_source_sha256: str
    original_source_line_count: int
    eligible_lines: tuple[int, ...]
    module_path: str

    @property
    def source_dir(self) -> Path:
        return self.probe.source_dir


def fixture_tree_sha256(task_dir: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        p for p in task_dir.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    for path in files:
        relative = path.relative_to(task_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def prepare_r5_probe(
    fixture_dir: Path,
    module_path: str,
    reproduction_argv: Sequence[str],
    parent_dir: Path,
    *,
    original_source_sha256: str,
    original_source_line_count: int,
    eligible_lines: tuple[int, ...],
    task_id: str,
) -> R5Probe:
    """Copy the canonical fixture and append the neutral guarded launcher.

    The canonical fixture is never written to.  The original-source metadata
    must already be frozen BEFORE this call so the appended harness can never
    influence breakpoint candidate derivation.
    """
    source_dir = parent_dir / f"probe-{task_id}"
    if source_dir.exists():
        raise R5LauncherError(f"probe source directory already exists: {source_dir}")
    shutil.copytree(
        fixture_dir, source_dir,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    module = source_dir / module_path
    if not module.is_file():
        raise R5LauncherError(f"probe module is missing from the fixture: {module_path}")
    original = module.read_text(encoding="utf-8")
    actual_sha = hashlib.sha256(original.encode("utf-8")).hexdigest()
    if actual_sha != original_source_sha256:
        raise R5LauncherError(
            "probe module bytes differ from the frozen original source "
            "(fixture drift)"
        )
    actual_lines = len(original.splitlines())
    if actual_lines != original_source_line_count:
        raise R5LauncherError("probe module line count differs from frozen original")
    launcher = build_r5_launcher_source(reproduction_argv)
    module.write_text(original + launcher, encoding="utf-8", newline="\n")
    driver_start_line = original_source_line_count + 1
    if driver_start_line <= original_source_line_count:
        raise R5LauncherError("driver start line must be beyond the original source")
    return R5Probe(
        probe=PdbProbe(
            source_dir=source_dir,
            parent_dir=parent_dir,
            script=module_path,
            breakpoint_line=0,  # model authors the breakpoint
            focus_function="",  # interactive mode never uses it
        ),
        driver_start_line=driver_start_line,
        original_source_sha256=original_source_sha256,
        original_source_line_count=original_source_line_count,
        eligible_lines=eligible_lines,
        module_path=module_path,
    )


__all__ = [
    "R5LauncherError",
    "R5Probe",
    "build_r5_launcher_source",
    "fixture_tree_sha256",
    "prepare_r5_probe",
    "task_target_module_path",
]
