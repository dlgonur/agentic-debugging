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
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
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
    "pid_is_alive",
    "spawn_suspended_on_windows",
    "terminate_process_group",
    "terminate_process_tree",
]
