"""POSIX-only deterministic process-tree gates for the configured transport.

Repair Pass 3 Blocker 1: a configured command request is spawned into its own
request-owned POSIX process group (``start_new_session``) so per-request
cancellation/timeout kills the command AND every descendant, and a worker
SIGTERM handler (worker-lifecycle cleanup ownership) terminates every
in-flight group so a forced/cooperative worker shutdown cannot orphan a
detached command tree.

Repair Pass 4 Blocker: the request-owned group belongs to the application
for the ENTIRE request lifetime.  A normally completed command (successful
response, non-zero exit, or invalid response) is reaped before ``request()``
returns, so ``os.getpgid(proc.pid)`` can no longer resolve the group even
while descendants with the original group id are alive; the final cleanup
therefore uses the authoritative group id known at spawn time and runs on
EVERY request exit path before the group is unregistered.

These tests use real local dummy processes with explicit readiness/PID markers
and bounded waits — never a fixed arbitrary sleep before cancellation.  They
are platform-gated to POSIX: process-group semantics (``os.getpgid`` /
``os.killpg`` / ``start_new_session``) do not exist on Windows, where the
accepted Job Object / ``taskkill /T`` containment covers the same topology and
is exercised by the Windows-specific tests that must not regress.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_debugger.application.command_transport import (
    CancellableJsonlCommandTransport,
)
from agentic_debugger.cancellation import CancellationError, CancellationToken
from agentic_debugger.evaluation.live import LiveModelConfig, LiveTransportError

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process-group semantics (getpgid/killpg/start_new_session) "
    "are unavailable on Windows; the accepted Job Object / taskkill "
    "containment covers this topology there",
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "command_models"
    / "dummy_command_model.py"
)


def make_config(command) -> LiveModelConfig:
    return LiveModelConfig("dummy-command", tuple(command), request_timeout_seconds=30.0)


def transport_for(command, **kwargs) -> CancellableJsonlCommandTransport:
    return CancellableJsonlCommandTransport(make_config(command), **kwargs)


def wait_for_file(path: Path, timeout_seconds: float = 15.0) -> bool:
    """Bounded readiness wait on an explicit marker (never a fixed sleep)."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def wait_until_dead(pid: int, timeout_seconds: float = 10.0) -> bool:
    """Bounded wait until ``pid`` is no longer alive."""
    from agentic_debugger.application.process_tree import pid_is_alive

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_is_alive(pid)


def spawn_tree_command(tmp_path: Path, delay: str = "60", with_pgid: bool = False):
    """Build the fixture argv that spawns command -> child -> grandchild.

    With ``with_pgid`` the fixture additionally records each process's POSIX
    process-group id so the request-owned group topology can be asserted from
    real evidence captured while the tree is alive.
    """
    argv = [
        sys.executable,
        str(FIXTURE),
        "spawn_child",
        "--state-dir",
        str(tmp_path / "state"),
        "--self-pid-file",
        str(tmp_path / "self.pid"),
        "--child-pid-file",
        str(tmp_path / "child.pid"),
        "--grandchild-pid-file",
        str(tmp_path / "grandchild.pid"),
        "--delay",
        delay,
    ]
    if with_pgid:
        argv += [
            "--self-pgid-file",
            str(tmp_path / "self.pgid"),
            "--child-pgid-file",
            str(tmp_path / "child.pgid"),
            "--grandchild-pgid-file",
            str(tmp_path / "grandchild.pgid"),
        ]
    return argv


def read_pids(tmp_path: Path) -> tuple[int, int, int]:
    self_pid = int((tmp_path / "self.pid").read_text())
    child_pid = int((tmp_path / "child.pid").read_text())
    grandchild_pid = int((tmp_path / "grandchild.pid").read_text())
    return self_pid, child_pid, grandchild_pid


@POSIX_ONLY
class TestPosixRequestOwnedGroupTopology:
    """The configured command runs in its own request-owned process group,
    distinct from the worker/test group, and its descendants share it."""

    def test_command_is_its_own_group_leader_and_descendants_share_it(self, tmp_path):
        # Real topology evidence captured while the tree is alive: the fixture
        # records each process's pgid.  The command must be its own group
        # leader (start_new_session), distinct from this process's group, and
        # the child/grandchild must share the command's group.
        command = spawn_tree_command(tmp_path, with_pgid=True)
        token = CancellationToken()
        transport = transport_for(command, cancel_check=token.check)

        def _cancel() -> None:
            # The grandchild pgid file is written by the grandchild itself, so
            # it is the strongest readiness marker for the whole tree.
            assert wait_for_file(tmp_path / "grandchild.pgid"), "tree never ready"
            token.request()

        threading.Thread(target=_cancel, daemon=True).start()
        with pytest.raises(CancellationError):
            transport.request({}, 60.0)

        self_pid, child_pid, grandchild_pid = read_pids(tmp_path)
        self_pgid = int((tmp_path / "self.pgid").read_text())
        child_pgid = int((tmp_path / "child.pgid").read_text())
        grandchild_pgid = int((tmp_path / "grandchild.pgid").read_text())
        own_pgid = os.getpgid(os.getpid())

        assert self_pgid == self_pid, "command must be its own group leader"
        assert self_pgid != own_pgid, "command group must differ from ours"
        assert child_pgid == self_pgid, "child must share the command group"
        assert grandchild_pgid == self_pgid, "grandchild must share the command group"

        # And after cancellation the whole tree is dead.
        assert wait_until_dead(self_pid), "direct command survived cancellation"
        assert wait_until_dead(child_pid), "child survived cancellation"
        assert wait_until_dead(grandchild_pid), "grandchild survived cancellation"


@POSIX_ONLY
class TestPosixCancellationKillsWholeTree:
    def test_cancellation_kills_command_child_grandchild(self, tmp_path):
        command = spawn_tree_command(tmp_path)
        token = CancellationToken()
        transport = transport_for(command, cancel_check=token.check)

        def _cancel() -> None:
            # Deterministic: gate on the explicit readiness markers, never a
            # fixed sleep, so the whole tree is confirmed spawned first.
            assert wait_for_file(tmp_path / "child.pid"), "child never spawned"
            assert wait_for_file(tmp_path / "grandchild.pid"), "grandchild never spawned"
            token.request()

        threading.Thread(target=_cancel, daemon=True).start()
        # 1. cancellation returns CancellationError (not a transport error).
        with pytest.raises(CancellationError):
            transport.request({}, 60.0)

        self_pid, child_pid, grandchild_pid = read_pids(tmp_path)
        # 2/3/4. direct command, child, and grandchild are all dead.
        assert wait_until_dead(self_pid), "direct command survived cancellation"
        assert wait_until_dead(child_pid), "child survived cancellation"
        assert wait_until_dead(grandchild_pid), "grandchild survived cancellation"


@POSIX_ONLY
class TestPosixTimeoutKillsWholeTree:
    def test_timeout_kills_command_child_grandchild(self, tmp_path):
        command = spawn_tree_command(tmp_path)
        transport = transport_for(command)
        # 5. timeout kills the same tree and stays a request_timeout.
        with pytest.raises(LiveTransportError) as exc:
            transport.request({}, 6.0)
        assert exc.value.kind == "request_timeout"
        assert exc.value.timed_out is True

        assert wait_for_file(tmp_path / "child.pid"), "child never spawned"
        assert wait_for_file(tmp_path / "grandchild.pid"), "grandchild never spawned"
        self_pid, child_pid, grandchild_pid = read_pids(tmp_path)
        assert wait_until_dead(self_pid), "direct command survived the timeout"
        assert wait_until_dead(child_pid), "child survived the timeout"
        assert wait_until_dead(grandchild_pid), "grandchild survived the timeout"


@POSIX_ONLY
class TestPosixNormalExitCleansRequestGroup:
    """Repair Pass 4: a normally completed request owns its group until
    ``request()`` returns — no descendant may survive a natural exit.

    The direct command exits naturally (successfully, non-zero, or with an
    invalid response) and is reaped BEFORE ``request()`` returns, so the
    group cleanup must work from the authoritative group id known at spawn
    time, not from ``os.getpgid(proc.pid)``.
    """

    def test_successful_request_kills_child_and_grandchild(self, tmp_path):
        # The exact regression reproduction: command -> child -> grandchild,
        # the command emits a valid response and exits 0.  After request()
        # RETURNS SUCCESSFULLY the whole tree must be dead.  delay=0: the
        # command exits naturally right after the tree is confirmed alive
        # (the fixture's readiness barrier), never via the request timeout.
        command = spawn_tree_command(tmp_path, delay="0")
        transport = transport_for(command)
        response = transport.request({"controller": {"state": "Reproduce"}}, 60.0)
        # The response stays a successful valid directive: the residual
        # descendant cleanup must not reinterpret a valid response.
        assert response["kind"] == "action"
        assert response["name"] == "run_reproduction"

        self_pid, child_pid, grandchild_pid = read_pids(tmp_path)
        assert wait_until_dead(self_pid), "direct command not reaped"
        assert wait_until_dead(child_pid), "child survived a successful request"
        assert wait_until_dead(grandchild_pid), (
            "grandchild survived a successful request"
        )

    def test_natural_nonzero_exit_kills_child_and_grandchild(self, tmp_path):
        # Natural non-zero exit with the tree alive: the existing
        # process_error taxonomy is retained and the tree is cleaned.
        command = [
            sys.executable,
            str(FIXTURE),
            "spawn_child_exit_nonzero",
            "--state-dir",
            str(tmp_path / "state"),
            "--self-pid-file",
            str(tmp_path / "self.pid"),
            "--child-pid-file",
            str(tmp_path / "child.pid"),
            "--grandchild-pid-file",
            str(tmp_path / "grandchild.pid"),
        ]
        transport = transport_for(command)
        with pytest.raises(LiveTransportError) as exc:
            transport.request({}, 60.0)
        assert exc.value.kind == "process_error"

        assert wait_for_file(tmp_path / "child.pid"), "child never spawned"
        assert wait_for_file(tmp_path / "grandchild.pid"), "grandchild never spawned"
        self_pid, child_pid, grandchild_pid = read_pids(tmp_path)
        assert wait_until_dead(self_pid), "direct command not reaped"
        assert wait_until_dead(child_pid), "child survived a natural non-zero exit"
        assert wait_until_dead(grandchild_pid), (
            "grandchild survived a natural non-zero exit"
        )

    def test_natural_invalid_response_kills_child_and_grandchild(self, tmp_path):
        # Natural zero exit with an invalid response and the tree alive: the
        # existing invalid_response taxonomy is retained and the tree is
        # cleaned.
        command = [
            sys.executable,
            str(FIXTURE),
            "spawn_child_exit_invalid",
            "--state-dir",
            str(tmp_path / "state"),
            "--self-pid-file",
            str(tmp_path / "self.pid"),
            "--child-pid-file",
            str(tmp_path / "child.pid"),
            "--grandchild-pid-file",
            str(tmp_path / "grandchild.pid"),
        ]
        transport = transport_for(command)
        with pytest.raises(LiveTransportError) as exc:
            transport.request({}, 60.0)
        assert exc.value.kind == "invalid_response"

        assert wait_for_file(tmp_path / "child.pid"), "child never spawned"
        assert wait_for_file(tmp_path / "grandchild.pid"), "grandchild never spawned"
        self_pid, child_pid, grandchild_pid = read_pids(tmp_path)
        assert wait_until_dead(self_pid), "direct command not reaped"
        assert wait_until_dead(child_pid), "child survived an invalid response"
        assert wait_until_dead(grandchild_pid), (
            "grandchild survived an invalid response"
        )

    def test_request_group_is_unregistered_after_request(self, tmp_path):
        # After any completed request the group is unregistered: the worker
        # SIGTERM registry only tracks IN-FLIGHT requests.
        from agentic_debugger.application.process_tree import _snapshot_request_groups

        command = spawn_tree_command(tmp_path, delay="0")
        transport = transport_for(command)
        transport.request({"controller": {"state": "Reproduce"}}, 60.0)
        self_pid, _, _ = read_pids(tmp_path)
        assert self_pid not in _snapshot_request_groups()


# A minimal worker stand-in: installs the worker-lifecycle cleanup handler,
# spawns a detached sleeper in its own group, registers that group, signals
# readiness, then waits to be terminated.  On SIGTERM the handler must kill
# the registered group before the worker exits.
_WORKER_STANDIN = r"""
import os, signal, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, {project_root!r})
from agentic_debugger.application.process_tree import (
    install_worker_request_group_cleanup,
    register_request_group,
)

ready_file = Path({ready_file!r})
sleeper_pid_file = Path({sleeper_pid_file!r})

install_worker_request_group_cleanup()
sleeper = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(3600)"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
    start_new_session=True,
)
register_request_group(sleeper.pid)
sleeper_pid_file.write_text(str(sleeper.pid))
ready_file.write_text("ready")
time.sleep(3600)
"""


@POSIX_ONLY
class TestPosixWorkerShutdownLeavesNoTree:
    def test_worker_sigterm_kills_registered_request_group(self, tmp_path):
        # 6. worker/app shutdown path leaves no detached tree behind: a worker
        # stand-in installs the cleanup handler, registers a detached sleeper
        # group, then receives SIGTERM; the handler must kill the sleeper.
        project_root = str(Path(__file__).resolve().parents[2])
        ready_file = tmp_path / "ready"
        sleeper_pid_file = tmp_path / "sleeper.pid"
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WORKER_STANDIN.format(
                    project_root=project_root,
                    ready_file=str(ready_file),
                    sleeper_pid_file=str(sleeper_pid_file),
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=True,
        )
        try:
            assert wait_for_file(ready_file), "worker stand-in never became ready"
            sleeper_pid = int(sleeper_pid_file.read_text())
            from agentic_debugger.application.process_tree import pid_is_alive

            assert pid_is_alive(sleeper_pid), "sleeper should be alive pre-shutdown"
            # Forced/cooperative worker shutdown: SIGTERM the worker.
            os.kill(worker.pid, signal.SIGTERM)
            # Reap the worker (it is our direct child): a zombie still answers
            # os.kill(pid, 0), so wait() is the honest exit observation.
            try:
                worker.wait(timeout=10.0)
                exited = True
            except subprocess.TimeoutExpired:
                exited = False
            assert exited, "worker stand-in ignored SIGTERM"
            # The registered request group (the detached sleeper) must be gone.
            assert wait_until_dead(sleeper_pid), (
                "detached request group survived worker shutdown"
            )
        finally:
            for pid_source in (worker.pid,):
                try:
                    os.killpg(os.getpgid(pid_source), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            try:
                worker.kill()
            except Exception:
                pass
