"""Import-isolation and existing-surface compatibility tests.

The application contract package must import without Textual and without
loading or executing the heavy controller/verifier/demo paths.  Existing
canonical entry points must remain importable unchanged.
"""

from __future__ import annotations

import subprocess
import sys

ISOLATION_SCRIPT = """
import sys

import agentic_debugger.application  # noqa: F401
import agentic_debugger.application.events  # noqa: F401
import agentic_debugger.application.session  # noqa: F401
import agentic_debugger.application.sources  # noqa: F401
import agentic_debugger.application.presentation  # noqa: F401

loaded = set(sys.modules)
# Heavy execution paths and Textual must never be pulled in by the contract
# layer.  Lightweight data modules (events.schema, agent.state_machine,
# evaluation taxonomies) may legitimately load.
forbidden = {
    "textual",
    "agentic_debugger.agent.controller",
    "agentic_debugger.agent.tool_registry",
    "agentic_debugger.agent.model_adapter",
    "agentic_debugger.evaluation.verifier",
    "agentic_debugger.demo",
}
violations = sorted(loaded & forbidden)
if violations:
    raise SystemExit("forbidden modules loaded: " + ", ".join(violations))
print("isolation-ok")
"""

ENTRY_POINT_SCRIPT = """
from agentic_debugger.events.schema import RunEvent
from agentic_debugger.agent.controller import DeterministicController
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.demo.cli import build_parser, main
from agentic_debugger.events.replay import replay_events
print("entry-points-ok")
"""


def _run_python(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


class TestApplicationImportIsolation:
    def test_application_imports_are_isolated(self):
        """Importing the contracts must not load Textual or execution paths."""
        completed = _run_python(ISOLATION_SCRIPT)
        assert completed.returncode == 0, completed.stderr
        assert "isolation-ok" in completed.stdout

    def test_textual_is_not_required(self):
        """The contract layer must never import Textual."""
        completed = _run_python(
            "import agentic_debugger.application; import sys; "
            "assert 'textual' not in sys.modules; print('no-textual-ok')"
        )
        assert completed.returncode == 0, completed.stderr
        assert "no-textual-ok" in completed.stdout


class TestExistingSurfaceCompatibility:
    def test_existing_entry_points_import_unchanged(self):
        completed = _run_python(ENTRY_POINT_SCRIPT)
        assert completed.returncode == 0, completed.stderr
        assert "entry-points-ok" in completed.stdout

    def test_demo_cli_list_tasks_still_works(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentic_debugger.demo",
                "--list-tasks",
                "--output-dir",
                "demo-out-list-tasks-smoke",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=_repository_root(),
        )
        assert completed.returncode == 0, completed.stderr
        assert "curated-off-by-one-002" in completed.stdout


def _repository_root():
    from pathlib import Path

    return str(Path(__file__).resolve().parents[2])
