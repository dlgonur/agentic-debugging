"""PDB trace machinery and target stdio isolation for the worker.

Owns the one-shot breakpoint runner, the persistent paused-target runner
(single paused-target pause/synchronization protocol, operating on the
worker-owned lifecycle dict passed in by the caller), and the
discard/null stdio shims that isolate target output from the protocol
channel. No protocol dispatch, no validation, no evidence building.
"""
from __future__ import annotations

import io
import os
import pdb
import threading
from typing import Any, Dict, Optional

from agentic_debugger.runtime.pdb_worker_limits import _DISCARD_FD


class _BreakpointSentinel(BaseException):
    pass


class _TerminationSentinel(BaseException):
    pass


class _PdbRunner(pdb.Pdb):
    def __init__(self, script_canonic: str, breakpoints_set: frozenset[int]) -> None:
        stdin = io.StringIO()
        stdout = io.StringIO()
        super().__init__(readrc=False, stdin=stdin, stdout=stdout)
        self._script_canonic: str = script_canonic
        self._breakpoints: frozenset[int] = breakpoints_set
        self.hit_info: Optional[Dict[str, Any]] = None

    def user_line(self, frame: Any, return_to_frame: Any = None) -> None:
        if (frame.f_lineno in self._breakpoints and
            os.path.normcase(os.path.abspath(frame.f_code.co_filename)) == self._script_canonic):
            self.hit_info = {
                'line': frame.f_lineno,
                'function': frame.f_code.co_name,
            }
            raise _BreakpointSentinel()

    def user_call(self, frame: Any, argument: Any) -> None:
        pass

    def user_return(self, frame: Any, return_value: Any) -> None:
        pass

    def user_exception(self, frame: Any, exc_info: Any) -> None:
        pass

    def preloop(self) -> None:
        pass

    def postloop(self) -> None:
        pass


class _PdbPersistentRunner(pdb.Pdb):
    def __init__(
        self,
        script_canonic: str,
        breakpoints_set: frozenset[int],
        condition: threading.Condition,
        lifecycle: Dict[str, Any],
    ) -> None:
        stdin = io.StringIO()
        stdout = io.StringIO()
        super().__init__(readrc=False, stdin=stdin, stdout=stdout)
        self._script_canonic: str = script_canonic
        self._breakpoints: frozenset[int] = breakpoints_set
        self._condition: threading.Condition = condition
        self._lifecycle: Dict[str, Any] = lifecycle

    def user_line(self, frame: Any, return_to_frame: Any = None) -> None:
        is_target_script = (
            os.path.normcase(os.path.abspath(frame.f_code.co_filename))
            == self._script_canonic
        )
        if not is_target_script:
            return

        with self._condition:
            resume_mode = self._lifecycle.get('_resume_mode')
            resume_frame = self._lifecycle.get('_resume_frame')
            forced_pause = (
                resume_mode == 'step'
                or (resume_mode == 'next' and frame is resume_frame)
            )
            if frame.f_lineno not in self._breakpoints and not forced_pause:
                return

            # A forced step/next is one-shot.  Breakpoint hits also consume a
            # pending execution-control request so a later line cannot create
            # a second, hidden pause from the same model action.
            self._lifecycle['_resume_mode'] = None
            self._lifecycle['_resume_frame'] = None
            self._lifecycle['pause_generation'] += 1
            self._lifecycle['state'] = 'paused'
            self._lifecycle['line'] = frame.f_lineno
            self._lifecycle['function'] = frame.f_code.co_name
            self._lifecycle['_paused_frame'] = frame
            self._condition.notify_all()
            try:
                while self._lifecycle['state'] == 'paused':
                    self._condition.wait()
                if self._lifecycle['state'] == 'terminating':
                    raise _TerminationSentinel()
            finally:
                self._lifecycle['_paused_frame'] = None

    def user_call(self, frame: Any, argument: Any) -> None:
        pass

    def user_return(self, frame: Any, return_value: Any) -> None:
        pass

    def user_exception(self, frame: Any, exc_info: Any) -> None:
        pass

    def preloop(self) -> None:
        pass

    def postloop(self) -> None:
        pass


class _DiscardStdout:
    encoding = "utf-8"
    errors = "replace"

    def write(self, s: str) -> int:
        return len(s) if s else 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return _DISCARD_FD


class _DiscardStderr:
    encoding = "utf-8"
    errors = "replace"

    def write(self, s: str) -> int:
        return len(s) if s else 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return _DISCARD_FD


class _NullReader:
    def read(self, size: int = -1) -> str:
        return ''

    def readline(self, size: int = -1) -> str:
        return ''

    def readable(self) -> bool:
        return True
