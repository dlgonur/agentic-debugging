import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agentic_debugger.cancellation import CancellationError, CancellationReason
from agentic_debugger.runtime.command_runner import CommandRunner
from agentic_debugger.runtime.exceptions import CommandRequestError
from agentic_debugger.runtime.workspace import TaskWorkspace

SLEEP_CODE = (
    "import os, sys, time; "
    "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid())); "
    "time.sleep(int(sys.argv[2]))"
)


def pid_alive(pid):
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, wintypes.BOOL, ctypes.c_uint32]
        open_process.restype = wintypes.HANDLE
        get_exit = kernel32.GetExitCodeProcess
        get_exit.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint32)]
        get_exit.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            if not get_exit(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@pytest.fixture
def workspace():
    src = Path(tempfile.mkdtemp())
    try:
        (src / "placeholder.txt").write_text("x\n")
        with TaskWorkspace(str(src)) as ws:
            yield ws
    finally:
        shutil.rmtree(str(src), ignore_errors=True)


class TestCommandRunnerCancellation:
    def test_no_cancel_check_behavior_is_unchanged(self, workspace):
        runner = CommandRunner(workspace)
        result = runner.run([sys.executable, "-c", "print('ok')"], ".", 30.0)
        assert result.exit_code == 0
        assert "ok" in result.stdout

    def test_polling_without_cancel_is_equivalent(self, workspace):
        runner = CommandRunner(workspace)
        plain = runner.run([sys.executable, "-c", "print('ok')"], ".", 30.0)
        polled = runner.run(
            [sys.executable, "-c", "print('ok')"], ".", 30.0,
            cancel_check=lambda: None,
        )
        assert plain.exit_code == polled.exit_code == 0
        assert plain.stdout == polled.stdout
        assert plain.timed_out == polled.timed_out

    def test_normal_timeout_unchanged_with_cancel_check(self, workspace, tmp_path):
        pid_file = tmp_path / "pid.txt"
        runner = CommandRunner(workspace)
        started = time.monotonic()
        result = runner.run(
            [sys.executable, "-c", SLEEP_CODE, str(pid_file), "30"],
            ".",
            1.5,
            cancel_check=lambda: None,
        )
        assert result.timed_out is True
        assert time.monotonic() - started < 10.0
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert pid_alive(pid) is False

    def test_cancellation_exits_promptly_and_kills_child(self, workspace, tmp_path):
        pid_file = tmp_path / "pid.txt"
        runner = CommandRunner(workspace)
        polls = {"n": 0}
        # Cancellation is gated on observed child startup (the pid file),
        # never on a fixed number of poll iterations; the bounded fallback
        # only protects against a hang if the child never starts.
        def check():
            polls["n"] += 1
            if pid_file.exists() or polls["n"] > 600:
                raise CancellationError(CancellationReason.CANCELLED)

        started = time.monotonic()
        with pytest.raises(CancellationError):
            runner.run(
                [sys.executable, "-c", SLEEP_CODE, str(pid_file), "300"],
                ".",
                300.0,
                cancel_check=check,
            )
        assert time.monotonic() - started < 10.0
        # the child must have started before cancellation fired
        assert pid_file.exists() is True
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert pid_alive(pid) is False

    def test_cancellation_is_re_raised_not_returned(self, workspace):
        runner = CommandRunner(workspace)

        def check():
            raise CancellationError(CancellationReason.TIMED_OUT)

        with pytest.raises(CancellationError) as raised:
            runner.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                ".",
                30.0,
                cancel_check=check,
            )
        assert raised.value.reason is CancellationReason.TIMED_OUT

    def test_cancel_check_must_be_callable(self, workspace):
        runner = CommandRunner(workspace)
        with pytest.raises(CommandRequestError):
            runner.run([sys.executable, "-c", "pass"], ".", 30.0, cancel_check="nope")

    def test_deadline_check_can_cancel_through_runner(self, workspace):
        """A token deadline is honored through the polling path."""
        from agentic_debugger.cancellation import CancellationToken

        token = CancellationToken(deadline_monotonic=time.monotonic() + 0.3)
        runner = CommandRunner(workspace)
        started = time.monotonic()
        with pytest.raises(CancellationError) as raised:
            runner.run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                ".",
                60.0,
                cancel_check=token.check,
            )
        assert raised.value.reason is CancellationReason.TIMED_OUT
        assert time.monotonic() - started < 10.0
