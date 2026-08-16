"""Process-tree termination support for the session worker boundary.

The final forced-escalation boundary must terminate the worker AND every
descendant session process (PDB worker, test subprocesses) without relying
on cooperative shutdown.

Windows strategy (the supported development platform): one job object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``.  The worker is spawned suspended,
assigned to the job, and resumed; every descendant the worker later creates
inherits job membership, so ``TerminateJobObject`` deterministically kills
the whole tree and closing the job handle (supervisor death/close) kills any
remaining members.  This uses only ``ctypes`` (standard library).

POSIX strategy: the worker runs in its own session/process group; escalation
uses the accepted SIGTERM/SIGKILL process-group ladder.  Children that detach
into their own groups (the PDB worker does) are not reachable by that ladder;
this is a documented POSIX limitation — the Task-3 acceptance gate is
Windows, where the job object covers the same topology.

Configured command-model requests (Task 8) close the POSIX gap with a
request-owned process group plus explicit worker-lifecycle cleanup ownership:
each configured command is spawned with ``start_new_session`` (its own
group), so the per-request cancellation/timeout ladder kills the command and
every descendant in that group; and every in-flight group is registered here
with a worker SIGTERM handler that terminates all registered groups before
the worker's default termination, so a forced/cooperative worker shutdown
cannot leave a detached command tree behind.  The same authoritative
group id (known at spawn time: the direct command's pid) is also used for
the final per-request cleanup on EVERY request exit — including a normal
successful or naturally failed command exit, when the direct process is
already reaped and ``os.getpgid(proc.pid)`` can no longer resolve the group
— so no request-owned group is ever unregistered with live descendants
still in it.  On Windows the accepted Job Object already covers the
worker-escalation topology, so the registry is a no-op there.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from typing import Optional

from agentic_debugger.application import ApplicationError

# Windows constants -----------------------------------------------------------

_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_THREAD_SUSPEND_RESUME = 0x0002
_STILL_ACTIVE = 259


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32() -> ctypes.WinDLL:
    return ctypes.WinDLL("kernel32", use_last_error=True)


class ProcessTreeError(ApplicationError):
    """Raised when the native process-tree boundary cannot be established."""


class WindowsProcessTreeJob:
    """One Windows job object that terminates its whole process tree.

    Lifecycle: create (with ``KILL_ON_JOB_CLOSE``), assign one or more
    process ids, ``terminate`` for forced escalation, ``close`` to release.
    Closing the last handle while members remain kills them (the supervisor
    lifetime boundary).
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ProcessTreeError("Windows job objects require win32")
        kernel32 = _kernel32()
        self._create = kernel32.CreateJobObjectW
        self._create.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._create.restype = wintypes.HANDLE
        self._set_info = kernel32.SetInformationJobObject
        self._set_info.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            ctypes.c_uint32,
        ]
        self._set_info.restype = wintypes.BOOL
        self._assign = kernel32.AssignProcessToJobObject
        self._assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._assign.restype = wintypes.BOOL
        self._terminate = kernel32.TerminateJobObject
        self._terminate.argtypes = [wintypes.HANDLE, ctypes.c_uint32]
        self._terminate.restype = wintypes.BOOL
        self._open_process = kernel32.OpenProcess
        self._open_process.argtypes = [ctypes.c_uint32, wintypes.BOOL, ctypes.c_uint32]
        self._open_process.restype = wintypes.HANDLE
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL

        handle = self._create(None, None)
        if not handle:
            raise ProcessTreeError(
                f"CreateJobObject failed: win32 error {ctypes.get_last_error()}"
            )
        self._handle = handle
        self._assigned = False
        try:
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = self._set_info(
                self._handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                raise ProcessTreeError(
                    "SetInformationJobObject failed: "
                    f"win32 error {ctypes.get_last_error()}"
                )
        except Exception:
            self.close()
            raise

    @property
    def assigned(self) -> bool:
        return self._assigned

    def assign(self, pid: int) -> bool:
        """Assign one process (and its future descendants) to the job."""
        process_handle = self._open_process(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
        )
        if not process_handle:
            return False
        try:
            ok = self._assign(self._handle, process_handle)
        finally:
            self._close_handle(process_handle)
        if ok:
            self._assigned = True
        return bool(ok)

    def resume(self, pid: int) -> bool:
        """Resume every thread of a suspended process (spawned suspended)."""
        return resume_suspended_process(pid)

    def terminate(self, exit_code: int = 1) -> bool:
        """Force-terminate every process in the job (the whole tree)."""
        if not self._handle:
            return False
        return bool(self._terminate(self._handle, exit_code))

    def close(self) -> None:
        """Release the job handle; remaining members are killed on close."""
        if self._handle:
            self._close_handle(self._handle)
            self._handle = None


def resume_suspended_process(pid: int) -> bool:
    """Resume every thread of a suspended process.

    ``CREATE_SUSPENDED`` suspends the primary thread.  The primary thread id
    is not guaranteed to equal the process id (it differs on current Windows
    builds), so threads are enumerated with ``Toolhelp32`` and each one is
    resumed; at spawn time the primary thread is the only one.
    """
    if sys.platform != "win32":
        return False
    kernel32 = _kernel32()

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ThreadID", ctypes.c_uint32),
            ("th32OwnerProcessID", ctypes.c_uint32),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
        ]

    snapshot = kernel32.CreateToolhelp32Snapshot
    snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    snapshot.restype = wintypes.HANDLE
    thread_first = kernel32.Thread32First
    thread_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    thread_first.restype = wintypes.BOOL
    thread_next = kernel32.Thread32Next
    thread_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    thread_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    open_thread = kernel32.OpenThread
    open_thread.argtypes = [ctypes.c_uint32, wintypes.BOOL, ctypes.c_uint32]
    open_thread.restype = wintypes.HANDLE
    resume_thread = kernel32.ResumeThread
    resume_thread.argtypes = [wintypes.HANDLE]
    resume_thread.restype = ctypes.c_uint32

    snap = snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if not snap:
        return False
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        resumed_any = False
        found = bool(thread_first(snap, ctypes.byref(entry)))
        while found:
            if entry.th32OwnerProcessID == pid:
                handle = open_thread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if handle:
                    try:
                        if resume_thread(handle) != 0xFFFFFFFF:
                            resumed_any = True
                    finally:
                        close_handle(handle)
            found = bool(thread_next(snap, ctypes.byref(entry)))
        return resumed_any
    finally:
        close_handle(snap)


def spawn_suspended_on_windows() -> int:
    """Creation flags for a worker that is assigned to its job before it runs."""
    if sys.platform == "win32":
        return subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
    return 0


def pid_is_alive(pid: int) -> bool:
    """Whether a process with ``pid`` is still running (same-user check)."""
    if type(pid) is not int or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == "win32":
        kernel32 = _kernel32()
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, wintypes.BOOL, ctypes.c_uint32]
        open_process.restype = wintypes.HANDLE
        get_exit = kernel32.GetExitCodeProcess
        get_exit.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint32)]
        get_exit.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            if not get_exit(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_proc(proc: subprocess.Popen, timeout: float) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Request-owned process groups (POSIX configured-command containment)
# ---------------------------------------------------------------------------
#
# A configured command request is spawned into its own POSIX process group
# (``start_new_session``) so the per-request cancellation/timeout ladder can
# kill the command AND every descendant with a group signal.  That detachment
# means a forced supervisor kill of the worker (which signals only the
# worker's own group) can no longer reach the command tree by itself.  The
# registry below is the explicit worker-lifecycle cleanup ownership that
# closes that gap: every in-flight request registers its group id here, and a
# one-time SIGTERM handler terminates every registered group before the
# worker's default termination runs.  On Windows the accepted Job Object
# already covers the worker-escalation topology, so the registry is a no-op
# there (the job kills every member, detached or not).

_REQUEST_GROUP_LOCK = threading.RLock()
_REQUEST_GROUP_IDS: set[int] = set()
_REQUEST_GROUP_HANDLER_INSTALLED = False

#: Bounded disappearance check for a signaled request-owned group: a short
#: polling window (never a busy spin) between SIGTERM and the SIGKILL
#: escalation, and again after SIGKILL before reporting the group state.
_GROUP_VANISH_SECONDS = 2.0
_GROUP_POLL_SECONDS = 0.05


def _snapshot_request_groups() -> list[int]:
    """Return the in-flight request group ids.

    Registration, unregistration, and the SIGTERM handler all execute on the
    worker's main thread (the transport request loop), so a snapshot is safe;
    the reentrant lock additionally guards a future cross-thread caller and,
    critically, lets the SIGTERM handler re-acquire the lock if the signal
    interrupts the main thread while it already holds it (``threading.Lock``
    is not reentrant and would deadlock the handler).
    """
    with _REQUEST_GROUP_LOCK:
        return list(_REQUEST_GROUP_IDS)


def _terminate_registered_groups() -> None:
    """Best-effort SIGTERM+SIGKILL of every in-flight request-owned group.

    Used by the worker's SIGTERM handler (the forced/cooperative shutdown
    path): it must leave no descendant behind, so it escalates straight to
    SIGKILL after SIGTERM.  Every step is bounded and exception-safe; a group
    that is already gone simply raises ``ProcessLookupError`` and is skipped.
    """
    for group_id in _snapshot_request_groups():
        try:
            os.killpg(group_id, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        try:
            os.killpg(group_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            continue


def _worker_signal_handler(signum, frame):  # pragma: no cover - signal path
    # Terminate every in-flight request-owned group (lock-free), then restore
    # and re-raise the default disposition so the worker still terminates the
    # normal way.  Never returns to the interrupted frame.
    try:
        _terminate_registered_groups()
    finally:
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except Exception:
            os._exit(128 + int(signum))


def install_worker_request_group_cleanup() -> None:
    """Register the worker-lifecycle cleanup for request-owned groups.

    Idempotent.  On POSIX it installs a SIGTERM handler that terminates every
    in-flight configured-command group before the worker's default
    termination, so a forced/cooperative worker shutdown cannot orphan a
    detached command tree.  On Windows the accepted Job Object already kills
    every descendant on escalation/close, so nothing extra is required.
    """
    global _REQUEST_GROUP_HANDLER_INSTALLED
    if sys.platform == "win32":
        return
    with _REQUEST_GROUP_LOCK:
        if _REQUEST_GROUP_HANDLER_INSTALLED:
            return
        try:
            signal.signal(signal.SIGTERM, _worker_signal_handler)
        except (ValueError, OSError):
            # Not in the main thread or signal unsupported: the per-request
            # ladder still owns cancellation/timeout cleanup.
            return
        _REQUEST_GROUP_HANDLER_INSTALLED = True


def register_request_group(group_id: int) -> None:
    """Track one in-flight request-owned process group for worker cleanup."""
    if sys.platform == "win32":
        return
    if type(group_id) is not int or isinstance(group_id, bool) or group_id <= 0:
        return
    with _REQUEST_GROUP_LOCK:
        _REQUEST_GROUP_IDS.add(group_id)


def unregister_request_group(group_id: int) -> None:
    """Stop tracking a request group once its request has fully terminated."""
    if sys.platform == "win32":
        return
    with _REQUEST_GROUP_LOCK:
        _REQUEST_GROUP_IDS.discard(group_id)


def terminate_request_process_group(proc: subprocess.Popen) -> None:
    """Terminate a request-owned process group and every descendant (POSIX).

    The command was spawned with ``start_new_session``, so its process group
    id equals its pid and is distinct from the worker's group; the ladder
    signals the whole group (SIGTERM then SIGKILL), which reaches the command
    and every child/grandchild that did not itself detach.  Falls back to the
    direct-process ladder when the group cannot be resolved.  On Windows this
    delegates to the accepted group ladder (the job object owns the tree).
    """
    if sys.platform == "win32":
        terminate_process_group(proc)
        return
    group_id: Optional[int] = None
    try:
        group_id = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        group_id = None
    if group_id is None or group_id <= 0:
        # The process is already gone or its group is unreadable: reap the
        # direct process and fall back to the generic ladder.
        terminate_process_group(proc)
        return
    own_pgid = os.getpgid(os.getpid())
    if group_id == own_pgid:
        # Defensive: never signal the worker's own group.
        terminate_process_group(proc)
        return
    try:
        os.killpg(group_id, signal.SIGTERM)
        _wait_proc(proc, 2.0)
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        proc.terminate()
    _wait_proc(proc, 1.0)
    try:
        proc.kill()
    except Exception:
        pass
    _wait_proc(proc, 1.0)


def terminate_request_group_id(group_id: int) -> bool:
    """Terminate a known request-owned process group by its id (POSIX).

    The authoritative request-group id is known at spawn time (with
    ``start_new_session`` the direct command is its own group leader, so the
    group id equals its pid).  This helper works from that id alone, because
    on a NORMAL command exit the direct process is already reaped and
    ``os.getpgid(proc.pid)`` may fail even while descendants with the
    original group id are still alive.

    Bounded ladder: SIGTERM to the group, a bounded disappearance check
    (polling, never a busy spin), SIGKILL to the group if members remain.
    A group that is already gone (``ProcessLookupError``) is ordinary
    success.  The worker's own process group is never signaled: an invalid
    or self-referential id is refused.  On Windows this is a no-op (the
    accepted Job Object owns the tree).

    Returns True when the group is observed empty afterwards (including the
    already-gone case), False when the id was refused or members could not
    be confirmed gone within the bounded window.
    """
    if sys.platform == "win32":
        return True
    if type(group_id) is not int or isinstance(group_id, bool) or group_id <= 0:
        return False
    try:
        own_pgid = os.getpgid(os.getpid())
    except OSError:  # pragma: no cover - defensive
        own_pgid = None
    if own_pgid is not None and group_id == own_pgid:
        # Defensive: never signal the worker's own process group.
        return False

    def _group_empty() -> bool:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # Members exist but are not ours to signal: not empty.
            return False
        except OSError:
            return True
        return False

    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return _group_empty()
    deadline = time.monotonic() + _GROUP_VANISH_SECONDS
    while time.monotonic() < deadline:
        if _group_empty():
            return True
        time.sleep(_GROUP_POLL_SECONDS)
    if _group_empty():
        return True
    try:
        os.killpg(group_id, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    deadline = time.monotonic() + _GROUP_VANISH_SECONDS
    while time.monotonic() < deadline:
        if _group_empty():
            return True
        time.sleep(_GROUP_POLL_SECONDS)
    return _group_empty()


def terminate_process_group(proc: subprocess.Popen) -> None:
    """Best-effort group kill ladder for the worker's own process group.

    POSIX: SIGTERM then SIGKILL to the worker's process group.  Windows
    fallback (used only when no job object is available): CTRL_BREAK then
    terminate then kill, mirroring the accepted command-runner ladder.
    """
    try:
        if sys.platform == "win32":
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            _wait_proc(proc, 1.0)
            proc.terminate()
            _wait_proc(proc, 1.0)
            proc.kill()
            _wait_proc(proc, 1.0)
        else:
            try:
                child_pgid = os.getpgid(proc.pid)
                own_pgid = os.getpgid(os.getpid())
                if child_pgid == own_pgid:
                    proc.terminate()
                else:
                    os.killpg(child_pgid, signal.SIGTERM)
                    _wait_proc(proc, 2.0)
                    os.killpg(child_pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                proc.terminate()
            _wait_proc(proc, 1.0)
            proc.kill()
            _wait_proc(proc, 1.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def terminate_process_tree(
    proc: subprocess.Popen,
    job: Optional[WindowsProcessTreeJob],
) -> bool:
    """Force-terminate the worker and its whole descendant tree.

    Returns whether tree-wide termination is guaranteed by the mechanism
    used (Windows job object) rather than the single-group fallback.
    """
    if job is not None and job.assigned:
        job.terminate()
        _wait_proc(proc, 5.0)
        return True
    terminate_process_group(proc)
    return False


__all__ = [
    "ProcessTreeError",
    "WindowsProcessTreeJob",
    "install_worker_request_group_cleanup",
    "pid_is_alive",
    "register_request_group",
    "spawn_suspended_on_windows",
    "terminate_process_group",
    "terminate_process_tree",
    "terminate_request_group_id",
    "terminate_request_process_group",
    "unregister_request_group",
]
