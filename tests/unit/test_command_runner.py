import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agentic_debugger.runtime.command_runner import CommandResult, CommandRunner
from agentic_debugger.runtime.exceptions import (
    CommandExecutionError,
    CommandRequestError,
    WorkspaceError,
)
from agentic_debugger.runtime.workspace import TaskWorkspace


@pytest.fixture
def workspace():
    src = Path(tempfile.mkdtemp())
    try:
        (src / "hello.py").write_text("print('hello')\n")
        (src / "subdir").mkdir()
        (src / "subdir" / "util.py").write_text("def util(): return 42\n")
        with TaskWorkspace(str(src)) as ws:
            yield ws
    finally:
        shutil.rmtree(str(src), ignore_errors=True)


class TestCommandResult:
    def test_to_mapping(self):
        result = CommandResult(
            argv=["python", "-c", "pass"],
            cwd=".",
            exit_code=0,
            timed_out=False,
            duration_ms=42,
            stdout="out",
            stderr="err",
            stdout_truncated=False,
            stderr_truncated=False,
        )
        m = result.to_mapping()
        assert m["argv"] == ["python", "-c", "pass"]
        assert m["exit_code"] == 0
        assert m["timed_out"] is False
        assert m["stdout"] == "out"
        assert m["stderr"] == "err"

    def test_frozen(self):
        result = CommandResult(
            argv=["python"],
            cwd=".",
            exit_code=0,
            timed_out=False,
            duration_ms=0,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        )
        with pytest.raises(AttributeError):
            result.stdout = "changed"


class TestCommandRunner:
    def test_successful_command(self, workspace):
        runner = CommandRunner(workspace)
        result = runner.run(
            [sys.executable, "-c", "print('ok')"],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.exit_code == 0
        assert result.timed_out is False
        assert "ok" in result.stdout
        assert result.stderr == ""
        assert result.stdout_truncated is False
        assert result.stderr_truncated is False
        assert result.duration_ms >= 0
        assert result.argv == [sys.executable, "-c", "print('ok')"]

    def test_non_zero_exit_code(self, workspace):
        runner = CommandRunner(workspace)
        result = runner.run(
            [sys.executable, "-c", "exit(42)"],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.exit_code == 42
        assert result.timed_out is False

    def test_separate_stdout_and_stderr(self, workspace):
        runner = CommandRunner(workspace)
        code = (
            "import sys; sys.stdout.write('out'); "
            "sys.stderr.write('err')"
        )
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.stdout == "out"
        assert result.stderr == "err"

    def test_unicode_output(self, workspace):
        runner = CommandRunner(workspace)
        code = "print('héllo wörld 日本語')"
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert "héllo wörld 日本語" in result.stdout

    def test_invalid_byte_decoding(self, workspace):
        runner = CommandRunner(workspace)
        code = "import sys; sys.stdout.buffer.write(b'\\xff\\xfe')"
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.exit_code == 0
        assert isinstance(result.stdout, str)
        assert len(result.stdout) > 0

    def test_command_runs_in_requested_cwd(self, workspace):
        runner = CommandRunner(workspace)
        result = runner.run(
            [sys.executable, "-c",
             "import os; print(os.path.basename(os.getcwd()))"],
            cwd="subdir",
            timeout_seconds=10,
        )
        ws_root_name = os.path.basename(workspace.root)
        out = result.stdout.strip()
        assert out != ws_root_name

    def test_string_argv_rejected(self, workspace):
        runner = CommandRunner(workspace)
        with pytest.raises(CommandRequestError, match="list"):
            runner.run("python -c pass", cwd=".", timeout_seconds=10)

    def test_empty_argv_rejected(self, workspace):
        runner = CommandRunner(workspace)
        with pytest.raises(CommandRequestError, match="non-empty"):
            runner.run([], cwd=".", timeout_seconds=10)

    def test_empty_argv_element_rejected(self, workspace):
        runner = CommandRunner(workspace)
        with pytest.raises(CommandRequestError, match="non-empty"):
            runner.run([sys.executable, ""], cwd=".", timeout_seconds=10)

    def test_nul_in_argv_rejected(self, workspace):
        runner = CommandRunner(workspace)
        with pytest.raises(CommandRequestError, match="NUL"):
            runner.run(
                [sys.executable, "-\0bad"],
                cwd=".",
                timeout_seconds=10,
            )

    def test_invalid_timeout_rejected(self, workspace):
        runner = CommandRunner(workspace)
        with pytest.raises(CommandRequestError, match="timeout_seconds"):
            runner.run(
                [sys.executable, "-c", "pass"],
                cwd=".",
                timeout_seconds=0,
            )

    def test_negative_timeout_rejected(self, workspace):
        runner = CommandRunner(workspace)
        with pytest.raises(CommandRequestError, match="timeout_seconds"):
            runner.run(
                [sys.executable, "-c", "pass"],
                cwd=".",
                timeout_seconds=-1,
            )

    def test_infinite_timeout_rejected(self, workspace):
        runner = CommandRunner(workspace)
        with pytest.raises(CommandRequestError, match="timeout_seconds"):
            runner.run(
                [sys.executable, "-c", "pass"],
                cwd=".",
                timeout_seconds=float("inf"),
            )

    def test_absolute_cwd_rejected(self, workspace):
        runner = CommandRunner(workspace)
        with pytest.raises((CommandRequestError, WorkspaceError)):
            runner.run(
                [sys.executable, "-c", "pass"],
                cwd="/etc",
                timeout_seconds=10,
            )

    def test_traversing_cwd_rejected(self, workspace):
        runner = CommandRunner(workspace)
        with pytest.raises(WorkspaceError, match="traversal"):
            runner.run(
                [sys.executable, "-c", "pass"],
                cwd="../outside",
                timeout_seconds=10,
            )

    def test_missing_executable_raises_execution_error(self, workspace):
        runner = CommandRunner(workspace)
        with pytest.raises(CommandExecutionError):
            runner.run(
                ["/nonexistent/binary"],
                cwd=".",
                timeout_seconds=5,
            )

    def test_timeout_returns_timed_out_result(self, workspace):
        runner = CommandRunner(workspace)
        code = "import time; time.sleep(30)"
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=2,
        )
        assert result.timed_out is True
        assert result.exit_code is None

    def test_stdout_truncation(self, workspace):
        runner = CommandRunner(workspace)
        code = "print('x' * 30000)"
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.stdout_truncated is True
        assert len(result.stdout) < 21000
        assert "... [output truncated] ..." in result.stdout

    def test_stderr_truncation(self, workspace):
        runner = CommandRunner(workspace)
        code = (
            "import sys; "
            "sys.stderr.write('x' * 30000)"
        )
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.stderr_truncated is True
        assert len(result.stderr) < 21000
        assert "... [output truncated] ..." in result.stderr

    def test_stdout_head_retained(self, workspace):
        runner = CommandRunner(workspace)
        prefix = "BEGIN_TOKEN_xyz"
        code = "print('" + prefix + "' + 'x' * 30000)"
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.stdout_truncated is True
        assert prefix in result.stdout

    def test_stdout_tail_retained(self, workspace):
        runner = CommandRunner(workspace)
        suffix = "END_TOKEN_abc"
        code = "print('x' * 30000 + '" + suffix + "')"
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.stdout_truncated is True
        assert suffix in result.stdout

    def test_stdout_middle_removed(self, workspace):
        runner = CommandRunner(workspace)
        middle = "UNIQUE_MIDDLE_MARKER_12345"
        code = "print('x' * 10000 + '" + middle + "' + 'x' * 10000)"
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.stdout_truncated is True
        assert middle not in result.stdout

    def test_large_both_streams_no_deadlock(self, workspace):
        runner = CommandRunner(workspace)
        code = (
            "import sys, threading\n"
            "def w(s):\n"
            "    for _ in range(500):\n"
            "        s.write('x' * 100)\n"
            "        s.flush()\n"
            "t1 = threading.Thread(target=w, args=(sys.stdout,))\n"
            "t2 = threading.Thread(target=w, args=(sys.stderr,))\n"
            "t1.start(); t2.start()\n"
            "t1.join(); t2.join()\n"
        )
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.exit_code == 0
        assert result.stdout_truncated or len(result.stdout) > 0
        assert result.stderr_truncated or len(result.stderr) > 0

    def test_utf8_split_across_chunks(self, workspace):
        runner = CommandRunner(workspace)
        code = (
            "import sys\n"
            "sys.stdout.buffer.write(b'\\xe2\\x82')\n"
            "sys.stdout.buffer.write(b'\\xac')\n"
            "sys.stdout.buffer.write(b' hello')\n"
        )
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.exit_code == 0
        assert result.stdout.strip() == "\u20ac hello"

    def test_posix_child_has_different_process_group(self, workspace):
        if sys.platform == "win32":
            pytest.skip("POSIX-specific process group test")
        runner = CommandRunner(workspace)
        code = (
            "import os; print(os.getpgid(os.getpid()))"
        )
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        child_pgid = int(result.stdout.strip())
        own_pgid = os.getpgid(os.getpid())
        assert child_pgid != own_pgid

    def test_detached_inherited_pipe_returns_bounded(self, workspace):
        if sys.platform == "win32":
            pytest.skip(
                "Windows does not support POSIX process-group detachment for "
                "inherited-pipe tests without Job Object"
            )
        pid_file = os.path.join(workspace.root, "detached.pid")
        grandchild_script = (
            "import os, sys, time\n"
            "with open(" + repr(pid_file) + ", 'w') as f:\n"
            "    f.write(str(os.getpid()) + '\\n')\n"
            "sys.stdout.write('grandchild alive\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(15)\n"
        )
        parent_script = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', "
            + repr(grandchild_script) + "],\n"
            "    start_new_session=True)\n"
            "sys.stdout.write(str(p.pid) + '\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(15)\n"
        )
        runner = CommandRunner(workspace)
        start = time.monotonic()
        try:
            result = runner.run(
                [sys.executable, "-c", parent_script],
                cwd=".",
                timeout_seconds=2,
            )
            elapsed = time.monotonic() - start
            assert result.timed_out is True
            assert elapsed < 8.0, (
                f"Runner took {elapsed:.2f}s, expected bounded return"
            )
            assert os.path.isfile(pid_file), "Grandchild PID file not created"
        finally:
            if os.path.isfile(pid_file):
                with open(pid_file) as f:
                    gpid = int(f.read().strip())
                try:
                    os.kill(gpid, 9)
                except Exception:
                    pass

    def test_20001_head_and_tail_preserved(self, workspace):
        runner = CommandRunner(workspace)
        prefix = "BEGIN_MARKER_999"
        suffix = "END_MARKER_888"
        code = (
            "import sys\n"
            "sys.stdout.write('" + prefix + "')\n"
            "sys.stdout.write('x' * 19991)\n"
            "sys.stdout.write('" + suffix + "')\n"
        )
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.stdout_truncated is True
        assert prefix in result.stdout, "Beginning token missing"
        assert suffix in result.stdout, "Ending token missing"
        assert len(result.stdout) < 21000

    @pytest.mark.parametrize("n_chars,expected_truncated", [
        (9_986, False),
        (9_987, False),
        (19_999, False),
        (20_000, False),
        (20_001, True),
    ])
    def test_output_boundary(self, workspace, n_chars, expected_truncated):
        runner = CommandRunner(workspace)
        code = "import sys; sys.stdout.write('x' * " + str(n_chars) + ")"
        result = runner.run(
            [sys.executable, "-c", code],
            cwd=".",
            timeout_seconds=10,
        )
        assert result.stdout_truncated is expected_truncated, (
            f"Expected truncated={expected_truncated} for n_chars={n_chars}, "
            f"got truncated={result.stdout_truncated}"
        )
        assert len(result.stdout) == n_chars if not expected_truncated else True
