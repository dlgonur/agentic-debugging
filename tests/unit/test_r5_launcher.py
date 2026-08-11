"""R5 launcher unit tests: mechanical pytest-driver generation, cwd-safe
reproduction, original-source boundary, fail-closed shapes."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.evaluation.runner import load_task

from experiments.debugger_interaction_v2_r5.launcher import (
    R5LauncherError,
    build_r5_launcher_source,
    fixture_tree_sha256,
    prepare_r5_probe,
    task_target_module_path,
)

CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"

R5_TASKS = (
    "curated-none-handling-001",
    "curated-off-by-one-002",
    "curated-wrong-branch-003",
    "curated-mutation-alias-004",
    "curated-caller-callee-005",
)


def _task(task_id: str):
    return load_task(str(CURATED_ROOT / task_id / "task.json"))


class TestLauncherGeneration:
    @pytest.mark.parametrize("task_id", R5_TASKS)
    def test_all_five_argv_shapes_generate(self, task_id):
        task = _task(task_id)
        source = build_r5_launcher_source(task.reproduction.argv)
        assert "pytest.main" in source
        assert "if __name__ == \"__main__\":" in source
        assert "_r5_failing_execution" in source
        # No bug semantics / oracle content in the launcher body (the F2P
        # node id legitimately names the test file, but no production call,
        # anchor, or expected behavior may appear).
        assert "recent_window(" not in source
        assert "format_display_name(" not in source
        assert "def _r5_failing_execution" in source
        # The F2P node id is harness data and appears only as pytest args.
        assert task.tests.fail_to_pass[0] in source

    def test_unsupported_shape_fails_closed(self):
        for bad in (
            ["python", "tests/test_display_name.py"],
            ["python", "-m", "unittest", "x"],
            ["pytest", "-q"],
            ["python", "-m", "pytest"],
            [],
        ):
            with pytest.raises(R5LauncherError):
                build_r5_launcher_source(bad)

    def test_launcher_has_no_operator_cwd_dependence(self):
        source = build_r5_launcher_source(
            ["python", "-m", "pytest", "tests/test_display_name.py::x", "-q", "-p", "no:cacheprovider"]
        )
        assert "os.path.dirname(_os.path.abspath(__file__))" in source
        assert "_os.chdir(_fixture_root)" in source


class TestCwdIndependentReproduction:
    @pytest.mark.parametrize("task_id", R5_TASKS)
    def test_f2p_failure_reproduces_from_unrelated_cwd(self, task_id, tmp_path):
        """Run the appended launcher as __main__ from an unrelated cwd; the
        frozen failing reproduction must still fail (exit code 1)."""
        fixture_dir = CURATED_ROOT / task_id
        task = _task(task_id)
        module_path = task_target_module_path(task)
        original = (fixture_dir / module_path).read_text(encoding="utf-8")
        original_sha = hashlib.sha256(original.encode("utf-8")).hexdigest()
        line_count = len(original.splitlines())

        probe = prepare_r5_probe(
            fixture_dir, module_path, task.reproduction.argv, tmp_path,
            original_source_sha256=original_sha,
            original_source_line_count=line_count,
            eligible_lines=(),
            task_id=task_id,
        )
        assert probe.driver_start_line == line_count + 1
        module_copy = probe.source_dir / module_path
        # Unrelated cwd: the pytest tmp dir's parent (NOT the fixture copy).
        unrelated_cwd = tmp_path
        completed = subprocess.run(
            [sys.executable, str(module_copy)],
            cwd=str(unrelated_cwd),
            capture_output=True, text=True, timeout=120,
        )
        assert completed.returncode == 1, (
            f"task {task_id}: expected F2P failure exit code 1, got "
            f"{completed.returncode}\nstdout={completed.stdout[:500]}\n"
            f"stderr={completed.stderr[:500]}"
        )
        # The appended module must never have been executed as a normal import.
        appended = module_copy.read_text(encoding="utf-8")
        assert appended.count("if __name__ == \"__main__\":") == 1

    def test_canonical_fixture_unchanged(self, tmp_path):
        for task_id in R5_TASKS:
            before = fixture_tree_sha256(CURATED_ROOT / task_id)
            _task(task_id)
            task = _task(task_id)
            module_path = task_target_module_path(task)
            original = (CURATED_ROOT / task_id / module_path).read_text(encoding="utf-8")
            prepare_r5_probe(
                CURATED_ROOT / task_id, module_path, task.reproduction.argv, tmp_path / task_id,
                original_source_sha256=hashlib.sha256(original.encode("utf-8")).hexdigest(),
                original_source_line_count=len(original.splitlines()),
                eligible_lines=(),
                task_id=task_id,
            )
            after = fixture_tree_sha256(CURATED_ROOT / task_id)
            assert before == after, f"canonical fixture changed for {task_id}"


class TestTargetSelection:
    def test_single_writable_production_path_selected(self):
        for task_id in R5_TASKS:
            task = _task(task_id)
            module_path = task_target_module_path(task)
            assert module_path.endswith(".py")
            assert (CURATED_ROOT / task_id / module_path).is_file()
            assert module_path in task.constraints.allowed_write_paths
