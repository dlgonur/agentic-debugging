import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agentic_debugger.runtime.command_runner import CommandRunner
from agentic_debugger.runtime.exceptions import WorkspaceError
from agentic_debugger.runtime.workspace import TaskWorkspace


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _poll_pid_dead(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def workspace():
    src = Path(tempfile.mkdtemp())
    try:
        (src / "test.py").write_text("print('test')\n")
        (src / "subdir").mkdir()
        (src / "subdir" / "util.py").write_text("def util(): return 42\n")
        with TaskWorkspace(str(src)) as ws:
            yield ws
    finally:
        shutil.rmtree(str(src), ignore_errors=True)


class TestCommandRunnerIntegration:
    def test_direct_child_process_cleaned(self, workspace):
        pid_file = os.path.join(workspace.root, "child.pid")
        code = (
            "import os, time\n"
            "with open(" + repr(pid_file) + ", 'w') as f:\n"
            "    f.write(str(os.getpid()) + '\\n')\n"
            "time.sleep(30)\n"
        )
        runner = CommandRunner(workspace)
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=2,
        )
        assert result.timed_out is True
        assert os.path.isfile(pid_file), "PID file was not created"
        with open(pid_file) as f:
            child_pid = int(f.read().strip())
        try:
            assert _poll_pid_dead(child_pid, timeout=3.0), (
                f"Direct child PID {child_pid} still alive after timeout"
            )
        finally:
            try:
                os.kill(child_pid, 9)
            except Exception:
                pass

    def test_inherited_group_descendant_cleaned(self, workspace):
        pid_file = os.path.join(workspace.root, "descendant.pid")
        child_code = (
            "import os, time\n"
            "with open(" + repr(pid_file) + ", 'w') as f:\n"
            "    f.write(str(os.getpid()) + '\\n')\n"
            "time.sleep(30)\n"
        )
        parent_script = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', "
            + repr(child_code) + "])\n"
            "time.sleep(30)\n"
        )
        runner = CommandRunner(workspace)
        result = runner.run(
            [sys.executable, "-c", parent_script],
            cwd=".",
            timeout_seconds=3,
        )
        assert result.timed_out is True
        assert os.path.isfile(pid_file), "Descendant PID file was not created"
        with open(pid_file) as f:
            descendant_pid = int(f.read().strip())
        try:
            assert _poll_pid_dead(descendant_pid, timeout=5.0), (
                f"Descendant PID {descendant_pid} still alive after timeout"
            )
        finally:
            try:
                os.kill(descendant_pid, 9)
            except Exception:
                pass

    def test_cwd_must_exist(self, workspace):
        runner = CommandRunner(workspace)
        with pytest.raises(WorkspaceError, match="does not exist"):
            runner.run(
                [sys.executable, "-c", "pass"],
                cwd="nonexistent_subdir",
                timeout_seconds=5,
            )

    def test_command_executes_in_workspace_cwd(self, workspace):
        runner = CommandRunner(workspace)
        result = runner.run(
            [sys.executable, "-c", "import os; print(os.getcwd())"],
            cwd=".",
            timeout_seconds=10,
        )
        cwd_out = result.stdout.strip()
        assert cwd_out == workspace.root

    def test_result_is_json_compatible(self, workspace):
        runner = CommandRunner(workspace)
        result = runner.run(
            [sys.executable, "-c", "print('json')"],
            cwd=".",
            timeout_seconds=10,
        )
        import json
        mapping = result.to_mapping()
        serialized = json.dumps(mapping, ensure_ascii=False)
        assert '"argv"' in serialized
        assert '"exit_code"' in serialized
