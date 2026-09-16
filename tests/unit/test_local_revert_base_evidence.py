"""Revert OK payloads carry restored-base evidence (Fix B).

After a revert (explicit or automatic), the model-facing success payload
must include the restored base (per-file sha256 + bounded source window)
so the next diff is rebuilt against the restored base instead of
remembered patched text.  No gate, bound, or failure-class change:
additive payload fields only.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ORIGINAL = "def add(a,b):\n    return a - b\n"
PATCHED = "def add(a,b):\n    return a + b\n"

DIFF_TO_PATCHED = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a,b):
-    return a - b
+    return a + b
"""

DIFF_SECOND_CHANGE = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a,b):
-    return a - b
+    return a - b  # annotated
"""


def _context(isolated: Path):
    from agentic_debugger.agent.controller_policy import PdbPolicy
    from agentic_debugger.application.events import SourceKind
    from agentic_debugger.application.local_project_helpers import LocalProjectTask
    from agentic_debugger.application.local_project_tools import (
        _LocalToolContext,
        _build_local_registry,
    )
    from agentic_debugger.application.observability import (
        ObservabilityContext,
        SessionObservability,
    )
    from agentic_debugger.evaluation.task_schema import Constraints

    task = LocalProjectTask(
        task_id="local-project-debug",
        title="t",
        description="b",
        language="python",
        fixture_path="isolated",
        constraints=Constraints(
            allowed_write_paths=["calc.py"],
            denied_write_paths=["tests", "task.json"],
            network_allowed=False,
            external_services_allowed=False,
            max_patch_attempts=2,
            max_test_runs=10,
            max_pdb_observations=5,
        ),
        tracked_files=("calc.py",),
        bug_description="b",
        reproduction_command="python -c \"print(1)\"",
        verification_command=None,
    )
    ctx = _LocalToolContext(
        isolated=isolated,
        tracked=["calc.py"],
        task=task,
        probe=None,
        observability=SessionObservability(
            ObservabilityContext(
                session_id="sess-revert-base-01",
                task_id="local-project-debug",
                source_kind=SourceKind.LOCAL_PROJECT,
                run_id="run-1",
            )
        ),
        command_environment={},
        pdb_worker_environment=None,
        executor=None,
        capabilities=None,
    )
    registry = _build_local_registry(ctx, pdb_policy=PdbPolicy.DISABLED)
    return ctx, registry


def _dispatch(registry, name: str, state: str, arguments: dict, seq: int):
    from agentic_debugger.agent.state_machine import ControllerState
    from agentic_debugger.events.schema import Action

    action = Action(
        action_id=f"action-{seq:09d}",
        run_id="run-1",
        task_id="local-project-debug",
        state=ControllerState[state],
        name=name,
        arguments=arguments,
    )
    return registry.dispatch(action, observation_id=f"observation-{seq:09d}")


def test_explicit_revert_carries_restored_base(tmp_path: Path) -> None:
    from agentic_debugger.agent.controller_policy import ActionName
    from agentic_debugger.events.schema import ObservationStatus

    isolated = tmp_path / "iso"
    isolated.mkdir()
    (isolated / "calc.py").write_text(ORIGINAL, encoding="utf-8")
    baseline_sha = hashlib.sha256((isolated / "calc.py").read_bytes()).hexdigest()
    _, registry = _context(isolated)

    applied = _dispatch(
        registry, ActionName.APPLY_PATCH.value, "PATCH", {"patch": DIFF_TO_PATCHED}, 1
    )
    assert applied.status is ObservationStatus.OK
    assert applied.payload["applied"] is True
    assert "return a + b" in (isolated / "calc.py").read_text(encoding="utf-8")

    reverted = _dispatch(registry, ActionName.REVERT_PATCH.value, "PATCH", {}, 2)
    assert reverted.status is ObservationStatus.OK
    assert reverted.payload["reverted"] is True
    assert reverted.payload["base_restored"] is True
    assert reverted.payload["base_sha256"] == {"calc.py": baseline_sha}
    window = reverted.payload["base_source_window"]["calc.py"]
    assert "return a - b" in window
    assert "return a + b" not in window
    assert "rebuild the next diff against this base" in reverted.summary
    assert hashlib.sha256((isolated / "calc.py").read_bytes()).hexdigest() == baseline_sha


def test_auto_revert_on_reapply_carries_restored_base(tmp_path: Path) -> None:
    from agentic_debugger.agent.controller_policy import ActionName
    from agentic_debugger.events.schema import ObservationStatus

    isolated = tmp_path / "iso"
    isolated.mkdir()
    (isolated / "calc.py").write_text(ORIGINAL, encoding="utf-8")
    _, registry = _context(isolated)

    first = _dispatch(
        registry, ActionName.APPLY_PATCH.value, "PATCH", {"patch": DIFF_TO_PATCHED}, 1
    )
    assert first.status is ObservationStatus.OK
    assert first.payload["reverted_previous"] is False
    assert "base_restored" not in first.payload

    # Revert explicitly, then apply a follow-up diff built against the
    # ORIGINAL base (the stale-base shape): the revert payload must have
    # re-anchored the model to the original text.
    reverted = _dispatch(registry, ActionName.REVERT_PATCH.value, "PATCH", {}, 2)
    assert reverted.payload["base_restored"] is True
    second = _dispatch(
        registry, ActionName.APPLY_PATCH.value, "PATCH", {"patch": DIFF_SECOND_CHANGE}, 3
    )
    assert second.status is ObservationStatus.OK
    assert second.payload["applied"] is True
    assert "annotated" in (isolated / "calc.py").read_text(encoding="utf-8")


def test_apply_over_active_patch_auto_revert_reports_base(tmp_path: Path) -> None:
    from agentic_debugger.agent.controller_policy import ActionName
    from agentic_debugger.events.schema import ObservationStatus

    isolated = tmp_path / "iso"
    isolated.mkdir()
    (isolated / "calc.py").write_text(ORIGINAL, encoding="utf-8")
    baseline_sha = hashlib.sha256((isolated / "calc.py").read_bytes()).hexdigest()
    _, registry = _context(isolated)

    _dispatch(
        registry, ActionName.APPLY_PATCH.value, "PATCH", {"patch": DIFF_TO_PATCHED}, 1
    )
    # A different diff while one is active triggers the auto-revert path.
    other = DIFF_TO_PATCHED.replace("return a + b", "return a + b  # v2")
    applied = _dispatch(
        registry, ActionName.APPLY_PATCH.value, "PATCH", {"patch": other}, 2
    )
    assert applied.status is ObservationStatus.OK
    assert applied.payload["reverted_previous"] is True
    assert applied.payload["base_restored"] is True
    assert applied.payload["base_sha256"] == {"calc.py": baseline_sha}
    assert "return a - b" in applied.payload["base_source_window"]["calc.py"]
