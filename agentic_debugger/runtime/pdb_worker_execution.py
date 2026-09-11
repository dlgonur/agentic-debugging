"""Validated target loading and supervised target runs for the PDB worker.

Owns workspace-script/argv/breakpoint validation (pure: returns an error
string instead of sending a wire response), the generic target-error
sanitizer, and the three supervised run bodies:

* :func:`run_one_shot` — one-shot ``run_to_breakpoint`` execution,
* :func:`run_post_mortem` — completion run with post-mortem capture,
* :func:`run_persistent_target` — persistent paused-target thread body.

The run bodies preserve the established stdio/cwd/trace isolation contract
exactly. They never send wire responses; the worker facade translates
outcomes into responses and owns all lifecycle-dict transition decisions,
except :func:`run_persistent_target` which publishes its terminal state
through the worker-owned lifecycle/condition pair passed in by the caller
(the same single lifecycle dict the persistent runner pauses on).
"""
from __future__ import annotations

import builtins
import os
import stat
import sys
from typing import Any, Dict, List, Optional, Tuple

from agentic_debugger.runtime.pdb_worker_limits import (
    _BINARY_OPEN_FLAG,
    _MAX_ARGV_ENTRY_UTF8,
    _MAX_SCRIPT_PATH_UTF8,
    _MAX_TARGET_SOURCE_BYTES,
)
from agentic_debugger.runtime.pdb_worker_paths import (
    _canonic,
    _has_raw_dotdot,
    _package_context_for_script,
)
from agentic_debugger.runtime.pdb_worker_postmortem import (
    _capture_post_mortem_evidence_pure,
    _has_traceback,
    _safe_exception_error_message,
)
from agentic_debugger.runtime import pdb_worker_runners as _runners
from agentic_debugger.runtime.pdb_worker_runners import (
    _BreakpointSentinel,
    _DiscardStderr,
    _DiscardStdout,
    _NullReader,
    _PdbRunner,
    _TerminationSentinel,
)


def validate_breakpoints(
    breakpoints_raw: Any,
    source_bytes: bytes,
) -> Tuple[Optional[List[int]], Optional[str]]:
    if not isinstance(breakpoints_raw, list):
        return None, "breakpoints must be a list"

    if len(breakpoints_raw) < 1 or len(breakpoints_raw) > 16:
        return None, "breakpoints must have 1-16 entries"

    bps: List[int] = []
    for bp in breakpoints_raw:
        if isinstance(bp, bool) or not isinstance(bp, int):
            return None, "breakpoints must contain only integers"
        if bp <= 0:
            return None, "breakpoints must be positive integers"
        bps.append(bp)

    if len(set(bps)) != len(bps):
        return None, "breakpoints must not contain duplicates"

    bps.sort()

    line_count = len(source_bytes.splitlines())

    for bp_line in bps:
        if bp_line > line_count:
            return None, (
                f"breakpoint line {bp_line} exceeds source length ({line_count})"
            )

    return bps, None


def validate_argv(argv_raw: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    if not isinstance(argv_raw, list):
        return None, "argv must be a list"

    if len(argv_raw) > 32:
        return None, "argv must have at most 32 entries"

    av: List[str] = []
    for a in argv_raw:
        if isinstance(a, bool) or not isinstance(a, str):
            return None, "argv entries must be strings"
        if '\0' in a:
            return None, "argv entry contains NUL byte"
        try:
            encoded = a.encode('utf-8')
        except UnicodeEncodeError as e:
            return None, (
                f"argv entry contains non-UTF-8-representable characters: {e}"
            )
        if len(encoded) > _MAX_ARGV_ENTRY_UTF8:
            return None, (
                f"argv entry exceeds {_MAX_ARGV_ENTRY_UTF8} UTF-8 bytes"
            )
        av.append(a)

    return av, None


def read_bounded_fd(fd: int) -> Tuple[Optional[bytes], Optional[str]]:
    buffer = bytearray()
    while True:
        remaining = _MAX_TARGET_SOURCE_BYTES + 1 - len(buffer)
        if remaining <= 0:
            break
        try:
            chunk = os.read(fd, min(64 * 1024, remaining))
        except OSError as e:
            return None, f"cannot read script: {e}"
        if not chunk:
            break
        buffer.extend(chunk)
    if len(buffer) > _MAX_TARGET_SOURCE_BYTES:
        return None, "script exceeds maximum source size"
    return bytes(buffer), None


def validate_workspace_script(
    script: Any,
    workspace_root: str,
) -> Tuple[Optional[Tuple[str, str, bytes]], Optional[str]]:
    if not isinstance(script, str) or not script:
        return None, "script must be a non-empty string"

    if '\0' in script:
        return None, "script contains NUL byte"

    if not script.endswith('.py'):
        return None, "script must end with .py"

    try:
        encoded = script.encode('utf-8')
    except UnicodeEncodeError as e:
        return None, (
            f"script contains non-UTF-8-representable characters: {e}"
        )

    if len(encoded) > _MAX_SCRIPT_PATH_UTF8:
        return None, (
            f"script path exceeds {_MAX_SCRIPT_PATH_UTF8} UTF-8 bytes"
        )

    if len(script) >= 2 and script[1] == ':':
        return None, "script must be a relative path"

    if script.startswith('/') or script.startswith('\\'):
        return None, "script must be a relative path"

    normalized = os.path.normpath(script)

    if os.path.isabs(normalized):
        return None, "script must be a relative path"

    if _has_raw_dotdot(script):
        return None, "script must not contain .. traversal"

    normalized = normalized.replace('\\', '/')

    abs_path = os.path.normpath(os.path.join(workspace_root, normalized))

    try:
        fd = os.open(abs_path, os.O_RDONLY | _BINARY_OPEN_FLAG)
    except (FileNotFoundError, IsADirectoryError) as e:
        if os.path.isdir(abs_path):
            return None, f"script is a directory: {script}"
        return None, f"script not found: {script}"
    except OSError as e:
        return None, f"cannot open script: {e}"

    try:
        try:
            opened_stat = os.fstat(fd)
        except OSError as e:
            return None, f"cannot stat opened script: {e}"

        if not stat.S_ISREG(opened_stat.st_mode):
            return None, f"script is not a regular file: {script}"

        try:
            real_root = os.path.realpath(workspace_root)
            real_path = os.path.realpath(abs_path)
        except (ValueError, OSError) as e:
            return None, f"cannot resolve script path: {e}"

        try:
            common = os.path.commonpath([real_root, real_path])
        except (ValueError, OSError) as e:
            return None, f"script path containment check failed: {e}"

        if os.path.normcase(common) != os.path.normcase(real_root):
            return None, "script escapes workspace via symlink or junction"

        try:
            current_path_stat = os.stat(real_path)
        except OSError as e:
            return None, f"cannot stat resolved script: {e}"

        if not os.path.samestat(opened_stat, current_path_stat):
            return None, (
                "script file changed between validation and open"
            )

        source_bytes, read_error = read_bounded_fd(fd)
        if source_bytes is None:
            return None, read_error
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    return (normalized, real_path, source_bytes), None


def safe_error_message(exc: BaseException) -> str:
    try:
        rendered = str(exc)
    except BaseException:
        rendered = "<unprintable exception>"
    msg = f"Target raised {type(exc).__name__}: {rendered}"
    control_chars = set('\r\n\t')
    safe = ''.join(c if c.isprintable() or c in (' ', '\t') else '?' for c in msg)
    safe_enc = safe.encode('utf-8', errors='replace')[:4096].decode('utf-8', errors='replace')
    return safe_enc


def _map_system_exit_code(code: Any) -> int:
    if code is None:
        return 0
    if isinstance(code, bool):
        return 1 if code else 0
    if isinstance(code, int):
        return code
    return 1


def run_one_shot(
    script_normalized: str,
    script_abs: str,
    breakpoints: List[int],
    argv: List[str],
    source_bytes: bytes,
) -> Dict[str, Any]:
    """Execute a one-shot breakpoint run; return the outcome (no wire I/O)."""
    saved_argv = list(sys.argv)
    saved_path = list(sys.path)
    saved_stdin = sys.stdin
    saved_stdout = sys.stdout
    saved_stderr = sys.stderr
    saved_dunder_stdout = sys.__stdout__
    saved_dunder_stderr = sys.__stderr__
    saved_cwd = os.getcwd()
    saved_trace = sys.gettrace()

    try:
        script_dir = os.path.dirname(script_abs)
        sys.argv = [script_normalized] + argv
        sys.path = [script_dir] + saved_path
        sys.stdin = _NullReader()
        sys.stdout = _DiscardStdout()
        sys.stderr = _DiscardStderr()
        sys.__stdout__ = sys.stdout
        sys.__stderr__ = sys.stderr

        try:
            code = compile(source_bytes, script_abs, 'exec')
        except SyntaxError as e:
            return {'kind': 'compile_error', 'error': safe_error_message(e)}

        canonic = _canonic(script_abs)
        runner = _PdbRunner(canonic, frozenset(breakpoints))

        try:
            globs: Dict[str, Any] = {
                '__name__': '__main__',
                '__doc__': None,
                '__package__': None,
                '__loader__': None,
                '__spec__': None,
                '__file__': script_abs,
                '__builtins__': builtins.__dict__,
            }
            runner.run(code, globs, globs)
        except _BreakpointSentinel:
            return {
                'kind': 'breakpoint',
                'line': runner.hit_info['line'],
                'function': runner.hit_info['function'],
            }
        except SystemExit as e:
            return {'kind': 'exited', 'exit_code': _map_system_exit_code(e.code)}
        except BaseException as e:
            return {'kind': 'failed', 'error': safe_error_message(e)}
        else:
            return {'kind': 'exited', 'exit_code': 0}
    finally:
        sys.argv = saved_argv
        sys.path = saved_path
        sys.stdin = saved_stdin
        sys.stdout = saved_stdout
        sys.stderr = saved_stderr
        sys.__stdout__ = saved_dunder_stdout
        sys.__stderr__ = saved_dunder_stderr
        os.chdir(saved_cwd)
        sys.settrace(None)
        sys.settrace(saved_trace)


def run_post_mortem(
    script_normalized: str,
    script_abs: str,
    argv: List[str],
    source_bytes: bytes,
) -> Dict[str, Any]:
    """Run to completion; capture post-mortem evidence on failure (no wire I/O)."""
    saved_argv = list(sys.argv)
    saved_path = list(sys.path)
    saved_stdin = sys.stdin
    saved_stdout = sys.stdout
    saved_stderr = sys.stderr
    saved_dunder_stdout = sys.__stdout__
    saved_dunder_stderr = sys.__stderr__
    saved_cwd = os.getcwd()

    try:
        script_dir = os.path.dirname(script_abs)
        sys.argv = [script_normalized] + argv
        sys.path = [script_dir] + saved_path
        sys.stdin = _NullReader()
        sys.stdout = _DiscardStdout()
        sys.stderr = _DiscardStderr()
        sys.__stdout__ = sys.stdout
        sys.__stderr__ = sys.stderr

        try:
            code = compile(source_bytes, script_abs, 'exec')
        except SyntaxError as e:
            return {'kind': 'compile_error', 'error': safe_error_message(e)}

        captured_exc: Optional[Tuple[type, BaseException, Any]] = None
        try:
            globs: Dict[str, Any] = {
                '__name__': '__main__',
                '__doc__': None,
                '__package__': None,
                '__loader__': None,
                '__spec__': None,
                '__file__': script_abs,
                '__builtins__': builtins.__dict__,
            }
            exec(code, globs, globs)
        except SystemExit as e:
            return {'kind': 'exited', 'exit_code': _map_system_exit_code(e.code)}
        except BaseException as e:
            captured_exc = (type(e), e, e.__traceback__)
        else:
            return {'kind': 'exited', 'exit_code': 0}

        if not _has_traceback(captured_exc):
            return {'kind': 'missing_traceback'}

        assert captured_exc is not None
        evidence = _capture_post_mortem_evidence_pure(
            script_normalized, captured_exc[0], captured_exc[1], captured_exc[2],
            _safe_exception_error_message,
        )
        return {'kind': 'post_mortem', 'evidence': evidence}
    finally:
        sys.argv = saved_argv
        sys.path = saved_path
        sys.stdin = saved_stdin
        sys.stdout = saved_stdout
        sys.stderr = saved_stderr
        sys.__stdout__ = saved_dunder_stdout
        sys.__stderr__ = saved_dunder_stderr
        os.chdir(saved_cwd)


def run_persistent_target(
    lifecycle: Dict[str, Any],
    condition: Any,
    script_normalized: str,
    script_abs: str,
    breakpoints: List[int],
    argv: List[str],
    source_bytes: bytes,
) -> None:
    """Persistent paused-target thread body (publishes terminal state).

    Operates on the worker-owned ``lifecycle``/``condition`` pair passed in
    by the caller — the same single lifecycle dict the persistent runner
    pauses on.  Terminal states (``exited``/``failed``/``terminated``) are
    published exactly as the accepted worker did.
    """
    saved_argv = list(sys.argv)
    saved_path = list(sys.path)
    saved_stdin = sys.stdin
    saved_stdout = sys.stdout
    saved_stderr = sys.stderr
    saved_dunder_stdout = sys.__stdout__
    saved_dunder_stderr = sys.__stderr__
    saved_cwd = os.getcwd()
    saved_trace = sys.gettrace()

    pending_state: Optional[str] = None
    pending_exit_code: Optional[int] = None
    pending_error: str = ""

    try:
        script_dir = os.path.dirname(script_abs)
        package_context = _package_context_for_script(
            script_normalized, saved_cwd
        )
        sys.argv = [script_normalized] + argv
        sys.path = [saved_cwd, script_dir] + saved_path
        sys.stdin = _NullReader()
        sys.stdout = _DiscardStdout()
        sys.stderr = _DiscardStderr()
        sys.__stdout__ = sys.stdout
        sys.__stderr__ = sys.stderr

        try:
            code = compile(source_bytes, script_abs, 'exec')
        except SyntaxError as e:
            pending_state = 'failed'
            pending_error = safe_error_message(e)
            return

        canonic = _canonic(script_abs)
        # Resolved via the runners module (not a local binding) so the
        # persistent-runner substitution seam used by lifecycle tests keeps
        # working against a single patch point.
        runner: Optional[_runners._PdbPersistentRunner] = _runners._PdbPersistentRunner(
            canonic, frozenset(breakpoints),
            condition, lifecycle,
        )

        globs: Dict[str, Any] = {
            '__name__': '__main__',
            '__doc__': None,
            '__package__': package_context,
            '__loader__': None,
            '__spec__': None,
            '__file__': script_abs,
            '__builtins__': builtins.__dict__,
        }

        try:
            runner.run(code, globs, globs)
        except _TerminationSentinel:
            pending_state = 'terminated'
            return
        except SystemExit as e:
            pending_exit_code = _map_system_exit_code(e.code)
            pending_state = 'exited'
            return
        except BaseException as e:
            pending_state = 'failed'
            pending_error = safe_error_message(e)
            return
        else:
            pending_state = 'exited'
            pending_exit_code = 0
            return
    finally:
        runner = None
        sys.argv = saved_argv
        sys.path = saved_path
        sys.stdin = saved_stdin
        sys.stdout = saved_stdout
        sys.stderr = saved_stderr
        sys.__stdout__ = saved_dunder_stdout
        sys.__stderr__ = saved_dunder_stderr
        os.chdir(saved_cwd)
        sys.settrace(None)
        sys.settrace(saved_trace)
        if pending_state is not None:
            with condition:
                lifecycle['state'] = pending_state
                lifecycle['_paused_frame'] = None
                lifecycle['_resume_mode'] = None
                lifecycle['_resume_frame'] = None
                if pending_state == 'exited':
                    lifecycle['exit_code'] = pending_exit_code
                elif pending_state == 'failed':
                    lifecycle['error'] = pending_error
                elif pending_state == 'terminated':
                    pass
                condition.notify_all()
