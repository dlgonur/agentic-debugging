from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "golden_trajectories"))
from support import FixedTaskWorkspace, ScriptedOutputAccountingError, pdb_steps, run_trajectory, static_steps
import support as golden_support

from agentic_debugger.agent.model_adapter import ModelScriptExhaustedError, ScriptedModelStep, TransitionDirective
from agentic_debugger.agent.state_machine import ControllerState


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "__pycache__").mkdir(parents=True, exist_ok=True)
    (source / "__pycache__" / "generated.pyc").write_bytes(b"generated")
    (source / "module.py").write_text("value = 1", encoding="utf-8")
    (source / "standalone.pyc").write_bytes(b"keep")
    return source


def test_workspace_honors_portable_parent_and_filters_only_generated_cache(tmp_path: Path) -> None:
    parent = tmp_path / "portable-parent"
    parent.mkdir()
    workspace = FixedTaskWorkspace(str(_source(tmp_path)), parent_dir=str(parent))
    root = Path(workspace.root)
    assert workspace.workspace_parent == str(parent.resolve())
    assert root.parent == parent
    assert (root / "module.py").exists()
    assert (root / "standalone.pyc").exists()
    assert not (root / "__pycache__").exists()
    workspace.cleanup()
    assert not root.exists()


def test_two_serial_workspaces_are_distinct_and_cleanup_is_exact(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    first = FixedTaskWorkspace(str(_source(tmp_path)), parent_dir=str(parent))
    second = FixedTaskWorkspace(str(_source(tmp_path)), parent_dir=str(parent))
    first_root = Path(first.root)
    second_root = Path(second.root)
    assert first_root != second_root
    assert first_root.parent == second_root.parent == parent
    first.cleanup()
    assert not first_root.exists()
    assert second_root.exists()
    second.cleanup()
    assert not second_root.exists()


def test_missing_parent_is_rejected_and_cleanup_failure_is_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="parent"):
        FixedTaskWorkspace(str(_source(tmp_path)), parent_dir=str(tmp_path / "missing"))

    parent = tmp_path / "parent"
    parent.mkdir()
    workspace = FixedTaskWorkspace(str(_source(tmp_path)), parent_dir=str(parent))
    root = Path(workspace.root)

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("locked")

    monkeypatch.setattr("support.shutil.rmtree", fail)
    with pytest.raises(OSError, match="locked"):
        workspace.cleanup()
    assert root.exists()
    monkeypatch.undo()
    workspace.cleanup()
    assert not root.exists()


def test_golden_harness_rejects_unused_and_exhausted_scripted_outputs(tmp_path: Path) -> None:
    def extra(task: object, patch: str) -> tuple[ScriptedModelStep, ...]:
        return static_steps(task, patch) + (
            ScriptedModelStep(ControllerState.DONE, TransitionDirective(ControllerState.DONE, "unused")),
        )

    with pytest.raises(ScriptedOutputAccountingError, match="unused scripted outputs"):
        run_trajectory("curated-none-handling-001", "static", extra, tmp_path / "extra")
    assert not (tmp_path / "extra" / "controller-workspace").exists()

    def short(task: object, patch: str) -> tuple[ScriptedModelStep, ...]:
        return static_steps(task, patch)[:-1]

    with pytest.raises(ModelScriptExhaustedError):
        run_trajectory("curated-none-handling-001", "static", short, tmp_path / "short")
    assert not (tmp_path / "short" / "controller-workspace").exists()
    assert not (tmp_path / "short" / ".pytest_cache").exists()
    assert not list((tmp_path / "short").rglob("__pycache__"))


def test_pdb_exhaustion_after_session_start_cleans_all_roots(tmp_path: Path) -> None:
    def short_pdb(task: object, patch: str) -> tuple[ScriptedModelStep, ...]:
        return pdb_steps(task, patch)[:4]

    with pytest.raises(ModelScriptExhaustedError):
        run_trajectory("curated-none-handling-001", "pdb", short_pdb, tmp_path / "pdb-short")
    assert not (tmp_path / "pdb-short" / "controller-workspace").exists()
    assert not (tmp_path / "pdb-short" / "pdb-source").exists()


def test_tool_exception_cleans_disposable_controller_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise OSError("tool failed")

    monkeypatch.setattr(golden_support.subprocess, "run", fail)
    run = run_trajectory("curated-none-handling-001", "static", None, tmp_path / "tool-error")
    assert run.controller.final_state.value in {"Done", "Failed"}
    assert not (tmp_path / "tool-error" / "controller-workspace").exists()
