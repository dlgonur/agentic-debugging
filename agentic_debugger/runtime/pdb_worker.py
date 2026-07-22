from __future__ import annotations

import builtins
import io
import json
import math
import os
import pdb
import posixpath
import stat
import sys
import threading
import traceback
import types
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agentic_debugger.runtime.pdb_protocol import (
    PROTOCOL_VERSION,
    MAX_LINE_LENGTH,
    PdbRequest,
    PdbResponse,
    serialize_response,
    deserialize_request,
)
from agentic_debugger.runtime.exceptions import PdbProtocolError


_MAX_SCRIPT_PATH_UTF8 = 4096
_MAX_ARGV_ENTRY_UTF8 = 1024
_BINARY_OPEN_FLAG = getattr(os, "O_BINARY", 0)
_MAX_TARGET_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_STACK_FRAMES = 64
_MAX_LOCAL_NAMES = 128
_MAX_NAME_UTF8 = 512
_MAX_TYPE_NAME_UTF8 = 512
_MAX_FUNCTION_UTF8 = 4096
_MAX_STRING_PREVIEW_UTF8 = 2048
_MAX_BYTES_PREVIEW = 1024
_MAX_CONTAINER_ITEMS = 16
_MAX_CONTAINER_DEPTH = 2
_MAX_LOCALS_RESULT_BYTES = 32768
# Keeps JSON integer conversion comfortably below Python's default decimal
# conversion limit while preserving ordinary large integers losslessly.
_MAX_SERIALIZED_INT_BITS = 4096


class _BreakpointSentinel(BaseException):
    pass


class _TerminationSentinel(BaseException):
    pass


_WORKER_TERMINATION_TIMEOUT = 3.0


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
        if (frame.f_lineno in self._breakpoints and
            os.path.normcase(os.path.abspath(frame.f_code.co_filename)) == self._script_canonic):
            with self._condition:
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
    def write(self, s: str) -> int:
        return len(s) if s else 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")


class _DiscardStderr:
    def write(self, s: str) -> int:
        return len(s) if s else 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")


class _NullReader:
    def read(self, size: int = -1) -> str:
        return ''

    def readline(self, size: int = -1) -> str:
        return ''

    def readable(self) -> bool:
        return True


def _has_raw_dotdot(script: str) -> bool:
    parts = script.replace('\\', '/').split('/')
    return '..' in parts


def _canonic(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _get_frame_locals_proxy_type() -> type:
    def _capture() -> type:
        return type(sys._getframe().f_locals)
    return _capture()


_FRAME_LOCALS_PROXY_TYPE = _get_frame_locals_proxy_type()
_TYPE_MODULE_DESCRIPTOR = type.__dict__['__module__']
_TYPE_QUALNAME_DESCRIPTOR = type.__dict__['__qualname__']
_SAFE_BUILTIN_TYPE_NAMES = (
    (type(None), 'builtins.NoneType'),
    (bool, 'builtins.bool'),
    (int, 'builtins.int'),
    (float, 'builtins.float'),
    (str, 'builtins.str'),
    (bytes, 'builtins.bytes'),
    (list, 'builtins.list'),
    (tuple, 'builtins.tuple'),
    (dict, 'builtins.dict'),
    (set, 'builtins.set'),
    (frozenset, 'builtins.frozenset'),
    (object, 'builtins.object'),
    (type, 'builtins.type'),
)


def _frame_locals_keys(mapping: Any) -> Any:
    if type(mapping) is dict:
        return dict.keys(mapping)
    return _FRAME_LOCALS_PROXY_TYPE.keys(mapping)


def _frame_locals_get(mapping: Any, name: str) -> Any:
    if type(mapping) is dict:
        return dict.__getitem__(mapping, name)
    return _FRAME_LOCALS_PROXY_TYPE.__getitem__(mapping, name)


def _safe_utf8_string(value: Any, maximum: int) -> Optional[str]:
    if type(value) is not str or not value or '\0' in value:
        return None
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError:
        return None
    if len(encoded) > maximum:
        return None
    return value


def _safe_type_name(value: Any) -> str:
    """Describe a value's type without consulting the value instance."""
    value_type = type(value)
    for known_type, known_name in _SAFE_BUILTIN_TYPE_NAMES:
        if value_type is known_type:
            return known_name
    try:
        module = _TYPE_MODULE_DESCRIPTOR.__get__(
            value_type, type(value_type)
        )
        qualname = _TYPE_QUALNAME_DESCRIPTOR.__get__(
            value_type, type(value_type)
        )
    except BaseException:
        return 'unknown'
    if type(module) is not str or type(qualname) is not str:
        return 'unknown'
    candidate = f"{module}.{qualname}"
    if _safe_utf8_string(candidate, _MAX_TYPE_NAME_UTF8) is None:
        return 'unknown'
    return candidate


def _utf8_preview(value: str, maximum: int) -> Tuple[str, bool]:
    chunks: List[str] = []
    used = 0
    consumed = 0
    for character in value:
        encoded = character.encode('utf-8', errors='replace')
        if used + len(encoded) > maximum:
            break
        chunks.append(encoded.decode('utf-8'))
        used += len(encoded)
        consumed += 1
    return ''.join(chunks), consumed < str.__len__(value)


def _empty_value_summary(kind: str, value: Any) -> Dict[str, Any]:
    return {
        'kind': kind,
        'type': _safe_type_name(value),
        'value': None,
        'special': None,
        'size': None,
        'items': [],
        'entries': [],
        'truncated': False,
    }


def _summarize_value(
    value: Any,
    depth: int = 0,
    ancestors: Optional[set[int]] = None,
) -> Dict[str, Any]:
    """Return a bounded summary using only exact built-in operations."""
    value_type = type(value)
    if value is None:
        return _empty_value_summary('none', value)
    if value_type is bool:
        result = _empty_value_summary('bool', value)
        result['value'] = value
        return result
    if value_type is int:
        result = _empty_value_summary('int', value)
        bits = int.bit_length(value)
        result['size'] = bits
        if bits <= _MAX_SERIALIZED_INT_BITS:
            result['value'] = value
        else:
            result['truncated'] = True
        return result
    if value_type is float:
        result = _empty_value_summary('float', value)
        if math.isnan(value):
            result['special'] = 'nan'
        elif math.isinf(value):
            result['special'] = 'inf' if value > 0 else '-inf'
        else:
            result['value'] = value
        return result
    if value_type is str:
        result = _empty_value_summary('str', value)
        preview, truncated = _utf8_preview(value, _MAX_STRING_PREVIEW_UTF8)
        result['value'] = preview
        result['size'] = str.__len__(value)
        result['truncated'] = truncated
        return result
    if value_type is bytes:
        result = _empty_value_summary('bytes', value)
        size = bytes.__len__(value)
        result['value'] = bytes.hex(value[:_MAX_BYTES_PREVIEW])
        result['size'] = size
        result['truncated'] = size > _MAX_BYTES_PREVIEW
        return result

    if value_type is list:
        kind = 'list'
    elif value_type is tuple:
        kind = 'tuple'
    elif value_type is dict:
        kind = 'dict'
    elif value_type is set:
        kind = 'set'
    elif value_type is frozenset:
        kind = 'frozenset'
    else:
        return _empty_value_summary('object', value)

    result = _empty_value_summary(kind, value)
    try:
        size = len(value)
    except BaseException:
        result['truncated'] = True
        return result
    result['size'] = size

    if kind in ('set', 'frozenset'):
        result['truncated'] = size > 0
        return result
    if size == 0:
        return result
    if depth >= _MAX_CONTAINER_DEPTH:
        result['truncated'] = True
        return result

    if ancestors is None:
        ancestors = set()
    identity = id(value)
    if identity in ancestors:
        result['truncated'] = True
        return result
    ancestors.add(identity)
    try:
        limit = min(size, _MAX_CONTAINER_ITEMS)
        if kind in ('list', 'tuple'):
            getter = list.__getitem__ if value_type is list else tuple.__getitem__
            try:
                for index in range(limit):
                    item = getter(value, index)
                    result['items'].append(
                        _summarize_value(item, depth + 1, ancestors)
                    )
            except (IndexError, RuntimeError):
                result['truncated'] = True
            if size > len(result['items']):
                result['truncated'] = True
            try:
                if len(value) != size:
                    result['truncated'] = True
            except BaseException:
                result['truncated'] = True
            return result

        iterator = iter(dict.items(value))
        try:
            for _ in range(limit):
                key, item_value = next(iterator)
                result['entries'].append({
                    'key': _summarize_value(key, depth + 1, ancestors),
                    'value': _summarize_value(
                        item_value, depth + 1, ancestors
                    ),
                })
        except StopIteration:
            result['truncated'] = True
        except RuntimeError:
            result['truncated'] = True
        if size > len(result['entries']):
            result['truncated'] = True
        try:
            if len(value) != size:
                result['truncated'] = True
        except BaseException:
            result['truncated'] = True
        return result
    finally:
        ancestors.discard(identity)


def _compact_json_size(value: Dict[str, Any]) -> int:
    return len(json.dumps(
        value,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
        allow_nan=False,
    ).encode('utf-8'))


def _successful_response_fits(response: PdbResponse) -> bool:
    """Preflight a successful response with the canonical wire serializer."""
    if response.success is not True:
        raise PdbProtocolError(
            "Response-size preflight requires a successful response"
        )
    try:
        serialize_response(response)
    except PdbProtocolError as exc:
        if str(exc).startswith(
                "Serialized response exceeds MAX_LINE_LENGTH"):
            return False
        raise
    return True


class PdbWorker:
    def __init__(self) -> None:
        self._pdb_stdin = io.StringIO()
        self._pdb_stdout = io.StringIO()
        self._pdb = pdb.Pdb(
            readrc=False,
            stdin=self._pdb_stdin,
            stdout=self._pdb_stdout,
        )
        self._running = True
        self._target_started = False
        self._protocol_stdin = sys.stdin
        self._protocol_stdout = sys.stdout
        self._condition = threading.Condition()
        self._lifecycle: Dict[str, Any] = {
            'state': 'idle',
            'script': '',
            'line': 0,
            'function': '',
            'exit_code': None,
            'error': '',
            '_start_script': '',
            'pause_generation': 0,
            '_paused_frame': None,
        }
        self._target_thread: Optional[threading.Thread] = None
        self._unsafe = False
        self._workspace_root_real = os.path.realpath(os.path.abspath(os.getcwd()))

    def run(self) -> None:
        while self._running and not self._unsafe:
            try:
                data = self._protocol_stdin.buffer.readline(MAX_LINE_LENGTH + 1)
            except OSError:
                self._diag("stdin read error")
                break

            if not data:
                break

            if len(data) > MAX_LINE_LENGTH:
                self._send_error(
                    request_id=0,
                    error=(
                        f"Input line exceeds maximum length "
                        f"({len(data)} > {MAX_LINE_LENGTH} bytes)"
                    ),
                )
                self._running = False
                break

            try:
                request = deserialize_request(data)
            except PdbProtocolError as e:
                self._send_error(
                    request_id=0,
                    error=str(e),
                )
                continue

            try:
                self._handle(request)
            except Exception as e:
                self._diag(f"Unhandled error: {traceback.format_exc()}")
                self._send_error(
                    request_id=request.request_id,
                    error=f"Internal worker error: {e}",
                )

    def _handle(self, request: PdbRequest) -> None:
        if request.protocol_version != PROTOCOL_VERSION:
            self._send_error(
                request_id=request.request_id,
                error=(
                    f"Unsupported protocol version: "
                    f"{request.protocol_version}, "
                    f"expected {PROTOCOL_VERSION}"
                ),
            )
            return
        op = request.operation
        if op == "hello":
            self._handle_hello(request)
        elif op == "ping":
            self._handle_ping(request)
        elif op == "shutdown":
            self._handle_shutdown(request)
        elif op == "run_to_breakpoint":
            self._handle_run_to_breakpoint(request)
        elif op == "start_paused_target":
            self._handle_start_paused_target(request)
        elif op == "continue_paused_target":
            self._handle_continue_paused_target(request)
        elif op == "get_target_status":
            self._handle_get_target_status(request)
        elif op == "terminate_paused_target":
            self._handle_terminate_paused_target(request)
        elif op == "get_stack_summary":
            self._handle_get_stack_summary(request)
        elif op == "get_frame":
            self._handle_get_frame(request)
        elif op == "get_frame_locals":
            self._handle_get_frame_locals(request)
        else:
            self._send_error(
                request_id=request.request_id,
                error=f"Unsupported operation: {op!r}",
            )

    def _handle_hello(self, request: PdbRequest) -> None:
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            success=True,
            result={
                "pid": os.getpid(),
                "protocol_version": PROTOCOL_VERSION,
            },
            error="",
        )
        self._send_response(response)

    def _handle_ping(self, request: PdbRequest) -> None:
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            success=True,
            result={"status": "ok", "pdb_created": True},
            error="",
        )
        self._send_response(response)

    def _handle_shutdown(self, request: PdbRequest) -> None:
        with self._condition:
            is_paused = self._lifecycle['state'] == 'paused'
        if is_paused:
            term_result = self._request_target_termination()
            if term_result.get('error'):
                self._send_error(request.request_id, term_result['error'])
                self._running = False
                self._unsafe = True
                return
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            success=True,
            result={"shutdown": True},
            error="",
        )
        self._send_response(response)
        self._running = False

    def _handle_run_to_breakpoint(self, request: PdbRequest) -> None:
        payload = request.payload

        for field in ('script', 'breakpoints', 'argv'):
            if field not in payload:
                self._send_error(
                    request.request_id,
                    f"Missing required payload field: {field}"
                )
                return

        for field in payload:
            if field not in ('script', 'breakpoints', 'argv'):
                self._send_error(
                    request.request_id,
                    f"Unknown payload field: {field}"
                )
                return

        if self._target_started:
            self._send_error(
                request.request_id,
                "Target execution already completed on this worker"
            )
            return

        workspace_root = os.getcwd()

        script = payload['script']
        breakpoints_raw = payload['breakpoints']
        argv_raw = payload['argv']

        sv = self._read_validated_workspace_script(script, workspace_root, request.request_id)
        if sv is None:
            return
        script_normalized, script_abs, source_bytes = sv

        bps = self._validate_breakpoints(breakpoints_raw, source_bytes, request.request_id)
        if bps is None:
            return

        av = self._validate_argv(argv_raw, request.request_id)
        if av is None:
            return

        self._target_started = True
        self._execute_target(script_normalized, script_abs, bps, av, source_bytes, request.request_id)

    def _read_validated_workspace_script(
        self,
        script: Any,
        workspace_root: str,
        request_id: int,
    ) -> Optional[Tuple[str, str, bytes]]:
        if not isinstance(script, str) or not script:
            self._send_error(request_id, "script must be a non-empty string")
            return None

        if '\0' in script:
            self._send_error(request_id, "script contains NUL byte")
            return None

        if not script.endswith('.py'):
            self._send_error(request_id, "script must end with .py")
            return None

        try:
            encoded = script.encode('utf-8')
        except UnicodeEncodeError as e:
            self._send_error(
                request_id,
                f"script contains non-UTF-8-representable characters: {e}"
            )
            return None

        if len(encoded) > _MAX_SCRIPT_PATH_UTF8:
            self._send_error(
                request_id,
                f"script path exceeds {_MAX_SCRIPT_PATH_UTF8} UTF-8 bytes"
            )
            return None

        if len(script) >= 2 and script[1] == ':':
            self._send_error(request_id, "script must be a relative path")
            return None

        if script.startswith('/') or script.startswith('\\'):
            self._send_error(request_id, "script must be a relative path")
            return None

        normalized = os.path.normpath(script)

        if os.path.isabs(normalized):
            self._send_error(request_id, "script must be a relative path")
            return None

        if _has_raw_dotdot(script):
            self._send_error(request_id, "script must not contain .. traversal")
            return None

        normalized = normalized.replace('\\', '/')

        abs_path = os.path.normpath(os.path.join(workspace_root, normalized))

        try:
            fd = os.open(abs_path, os.O_RDONLY | _BINARY_OPEN_FLAG)
        except (FileNotFoundError, IsADirectoryError) as e:
            if os.path.isdir(abs_path):
                self._send_error(request_id, f"script is a directory: {script}")
                return None
            self._send_error(request_id, f"script not found: {script}")
            return None
        except OSError as e:
            self._send_error(request_id, f"cannot open script: {e}")
            return None

        try:
            try:
                opened_stat = os.fstat(fd)
            except OSError as e:
                self._send_error(request_id, f"cannot stat opened script: {e}")
                return None

            if not stat.S_ISREG(opened_stat.st_mode):
                self._send_error(request_id, f"script is not a regular file: {script}")
                return None

            try:
                real_root = os.path.realpath(workspace_root)
                real_path = os.path.realpath(abs_path)
            except (ValueError, OSError) as e:
                self._send_error(request_id, f"cannot resolve script path: {e}")
                return None

            try:
                common = os.path.commonpath([real_root, real_path])
            except (ValueError, OSError) as e:
                self._send_error(request_id, f"script path containment check failed: {e}")
                return None

            if os.path.normcase(common) != os.path.normcase(real_root):
                self._send_error(request_id, "script escapes workspace via symlink or junction")
                return None

            try:
                current_path_stat = os.stat(real_path)
            except OSError as e:
                self._send_error(request_id, f"cannot stat resolved script: {e}")
                return None

            if not os.path.samestat(opened_stat, current_path_stat):
                self._send_error(
                    request_id,
                    "script file changed between validation and open"
                )
                return None

            source_bytes = self._read_bounded_fd(fd, request_id)
            if source_bytes is None:
                return None
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        return (normalized, real_path, source_bytes)

    def _read_bounded_fd(self, fd: int, request_id: int) -> Optional[bytes]:
        buffer = bytearray()
        while True:
            remaining = _MAX_TARGET_SOURCE_BYTES + 1 - len(buffer)
            if remaining <= 0:
                break
            try:
                chunk = os.read(fd, min(64 * 1024, remaining))
            except OSError as e:
                self._send_error(request_id, f"cannot read script: {e}")
                return None
            if not chunk:
                break
            buffer.extend(chunk)
        if len(buffer) > _MAX_TARGET_SOURCE_BYTES:
            self._send_error(request_id, "script exceeds maximum source size")
            return None
        return bytes(buffer)

    def _validate_breakpoints(
        self,
        breakpoints_raw: Any,
        source_bytes: bytes,
        request_id: int,
    ) -> Optional[List[int]]:
        if not isinstance(breakpoints_raw, list):
            self._send_error(request_id, "breakpoints must be a list")
            return None

        if len(breakpoints_raw) < 1 or len(breakpoints_raw) > 16:
            self._send_error(request_id, "breakpoints must have 1-16 entries")
            return None

        bps: List[int] = []
        for bp in breakpoints_raw:
            if isinstance(bp, bool) or not isinstance(bp, int):
                self._send_error(request_id, "breakpoints must contain only integers")
                return None
            if bp <= 0:
                self._send_error(request_id, "breakpoints must be positive integers")
                return None
            bps.append(bp)

        if len(set(bps)) != len(bps):
            self._send_error(request_id, "breakpoints must not contain duplicates")
            return None

        bps.sort()

        line_count = len(source_bytes.splitlines())

        for bp_line in bps:
            if bp_line > line_count:
                self._send_error(
                    request_id,
                    f"breakpoint line {bp_line} exceeds source length ({line_count})"
                )
                return None

        return bps

    def _validate_argv(
        self,
        argv_raw: Any,
        request_id: int,
    ) -> Optional[List[str]]:
        if not isinstance(argv_raw, list):
            self._send_error(request_id, "argv must be a list")
            return None

        if len(argv_raw) > 32:
            self._send_error(request_id, "argv must have at most 32 entries")
            return None

        av: List[str] = []
        for a in argv_raw:
            if isinstance(a, bool) or not isinstance(a, str):
                self._send_error(request_id, "argv entries must be strings")
                return None
            if '\0' in a:
                self._send_error(request_id, "argv entry contains NUL byte")
                return None
            try:
                encoded = a.encode('utf-8')
            except UnicodeEncodeError as e:
                self._send_error(
                    request_id,
                    f"argv entry contains non-UTF-8-representable characters: {e}"
                )
                return None
            if len(encoded) > _MAX_ARGV_ENTRY_UTF8:
                self._send_error(
                    request_id,
                    f"argv entry exceeds {_MAX_ARGV_ENTRY_UTF8} UTF-8 bytes"
                )
                return None
            av.append(a)

        return av

    def _safe_error_message(self, exc: BaseException) -> str:
        try:
            rendered = str(exc)
        except BaseException:
            rendered = "<unprintable exception>"
        msg = f"Target raised {type(exc).__name__}: {rendered}"
        control_chars = set('\r\n\t')
        safe = ''.join(c if c.isprintable() or c in (' ', '\t') else '?' for c in msg)
        safe_enc = safe.encode('utf-8', errors='replace')[:4096].decode('utf-8', errors='replace')
        return safe_enc

    def _execute_target(
        self,
        script_normalized: str,
        script_abs: str,
        breakpoints: List[int],
        argv: List[str],
        source_bytes: bytes,
        request_id: int,
    ) -> None:
        saved_argv = list(sys.argv)
        saved_path = list(sys.path)
        saved_stdin = sys.stdin
        saved_stdout = sys.stdout
        saved_stderr = sys.stderr
        saved_cwd = os.getcwd()
        saved_trace = sys.gettrace()

        try:
            script_dir = os.path.dirname(script_abs)
            sys.argv = [script_normalized] + argv
            sys.path = [script_dir] + saved_path
            sys.stdin = _NullReader()
            sys.stdout = _DiscardStdout()
            sys.stderr = _DiscardStderr()

            try:
                code = compile(source_bytes, script_abs, 'exec')
            except SyntaxError as e:
                error_msg = self._safe_error_message(e)
                self._send_error(request_id, error_msg)
                with self._condition:
                    self._lifecycle['state'] = 'failed'
                    self._lifecycle['script'] = script_normalized
                    self._lifecycle['error'] = error_msg
                    self._condition.notify_all()
                return

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
                result = {
                    'status': 'breakpoint',
                    'script': script_normalized,
                    'line': runner.hit_info['line'],
                    'function': runner.hit_info['function'],
                }
                response = PdbResponse(
                    protocol_version=PROTOCOL_VERSION,
                    request_id=request_id,
                    success=True,
                    result=result,
                    error="",
                )
                self._send_response(response)
                with self._condition:
                    self._lifecycle['state'] = 'terminated'
                    self._lifecycle['script'] = script_normalized
                    self._condition.notify_all()
                return
            except SystemExit as e:
                ec = e.code
                if ec is None:
                    exit_code = 0
                elif isinstance(ec, bool):
                    exit_code = 1 if ec else 0
                elif isinstance(ec, int):
                    exit_code = ec
                else:
                    exit_code = 1
                result = {
                    'status': 'exited',
                    'script': script_normalized,
                    'exit_code': exit_code,
                }
                response = PdbResponse(
                    protocol_version=PROTOCOL_VERSION,
                    request_id=request_id,
                    success=True,
                    result=result,
                    error="",
                )
                self._send_response(response)
                with self._condition:
                    self._lifecycle['state'] = 'exited'
                    self._lifecycle['script'] = script_normalized
                    self._lifecycle['exit_code'] = exit_code
                    self._condition.notify_all()
                return
            except BaseException as e:
                error_msg = self._safe_error_message(e)
                response = PdbResponse(
                    protocol_version=PROTOCOL_VERSION,
                    request_id=request_id,
                    success=False,
                    result={},
                    error=error_msg,
                )
                self._send_response(response)
                with self._condition:
                    self._lifecycle['state'] = 'failed'
                    self._lifecycle['script'] = script_normalized
                    self._lifecycle['error'] = error_msg
                    self._condition.notify_all()
                return
            else:
                result = {
                    'status': 'exited',
                    'script': script_normalized,
                    'exit_code': 0,
                }
                response = PdbResponse(
                    protocol_version=PROTOCOL_VERSION,
                    request_id=request_id,
                    success=True,
                    result=result,
                    error="",
                )
                self._send_response(response)
                with self._condition:
                    self._lifecycle['state'] = 'exited'
                    self._lifecycle['script'] = script_normalized
                    self._lifecycle['exit_code'] = 0
                    self._condition.notify_all()
                return
        finally:
            sys.argv = saved_argv
            sys.path = saved_path
            sys.stdin = saved_stdin
            sys.stdout = saved_stdout
            sys.stderr = saved_stderr
            os.chdir(saved_cwd)
            sys.settrace(None)
            sys.settrace(saved_trace)

    def _request_target_termination(self) -> Dict[str, Any]:
        with self._condition:
            if self._lifecycle['state'] == 'paused':
                self._lifecycle['state'] = 'terminating'
                self._condition.notify_all()
        target_thread = self._target_thread
        if target_thread is not None and target_thread is not threading.current_thread():
            target_thread.join(timeout=_WORKER_TERMINATION_TIMEOUT)
            if target_thread.is_alive():
                self._unsafe = True
                self._running = False
                self._target_thread = None
                return {'error': "Target termination did not complete within timeout", 'timeout': True}
            self._target_thread = None
        with self._condition:
            final_state = self._lifecycle['state']
        if final_state == 'terminated':
            return {'state': 'terminated'}
        return {'error': f"Target termination produced unexpected state: {final_state}"}

    def _fail_paused_target_invariant(self, error: str) -> str:
        """Clean up a false paused state without writing a response."""
        safe_error = ''.join(
            c if c.isprintable() or c in (' ', '\t') else '?'
            for c in error
        )
        safe_error = safe_error.encode(
            'utf-8', errors='replace'
        )[:4096].decode('utf-8', errors='replace')
        if not safe_error:
            safe_error = "Internal paused-target invariant failure"

        with self._condition:
            target_thread = self._target_thread
            if target_thread is not None and target_thread.is_alive():
                self._lifecycle['state'] = 'terminating'
                self._condition.notify_all()

        cleanup_safe = True
        if target_thread is threading.current_thread():
            cleanup_safe = False
        elif target_thread is not None and target_thread.is_alive():
            target_thread.join(timeout=_WORKER_TERMINATION_TIMEOUT)
            cleanup_safe = not target_thread.is_alive()

        if not cleanup_safe:
            safe_error += "; target invariant cleanup timed out"
            safe_error = safe_error.encode(
                'utf-8', errors='replace'
            )[:4096].decode('utf-8', errors='replace')
            self._unsafe = True
            self._running = False

        with self._condition:
            self._target_thread = None
            self._lifecycle['state'] = 'failed'
            self._lifecycle['error'] = safe_error
            self._lifecycle['_paused_frame'] = None
            self._condition.notify_all()
        return safe_error

    def _execute_target_persistent(
        self,
        script_normalized: str,
        script_abs: str,
        breakpoints: List[int],
        argv: List[str],
        source_bytes: bytes,
    ) -> None:
        saved_argv = list(sys.argv)
        saved_path = list(sys.path)
        saved_stdin = sys.stdin
        saved_stdout = sys.stdout
        saved_stderr = sys.stderr
        saved_cwd = os.getcwd()
        saved_trace = sys.gettrace()

        pending_state: Optional[str] = None
        pending_exit_code: Optional[int] = None
        pending_error: str = ""

        try:
            script_dir = os.path.dirname(script_abs)
            sys.argv = [script_normalized] + argv
            sys.path = [script_dir] + saved_path
            sys.stdin = _NullReader()
            sys.stdout = _DiscardStdout()
            sys.stderr = _DiscardStderr()

            try:
                code = compile(source_bytes, script_abs, 'exec')
            except SyntaxError as e:
                pending_state = 'failed'
                pending_error = self._safe_error_message(e)
                return

            canonic = _canonic(script_abs)
            runner: Optional[_PdbPersistentRunner] = _PdbPersistentRunner(
                canonic, frozenset(breakpoints),
                self._condition, self._lifecycle,
            )

            globs: Dict[str, Any] = {
                '__name__': '__main__',
                '__doc__': None,
                '__package__': None,
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
                ec = e.code
                if ec is None:
                    pending_exit_code = 0
                elif isinstance(ec, bool):
                    pending_exit_code = 1 if ec else 0
                elif isinstance(ec, int):
                    pending_exit_code = ec
                else:
                    pending_exit_code = 1
                pending_state = 'exited'
                return
            except BaseException as e:
                pending_state = 'failed'
                pending_error = self._safe_error_message(e)
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
            os.chdir(saved_cwd)
            sys.settrace(None)
            sys.settrace(saved_trace)
            if pending_state is not None:
                with self._condition:
                    self._lifecycle['state'] = pending_state
                    self._lifecycle['_paused_frame'] = None
                    if pending_state == 'exited':
                        self._lifecycle['exit_code'] = pending_exit_code
                    elif pending_state == 'failed':
                        self._lifecycle['error'] = pending_error
                    elif pending_state == 'terminated':
                        pass
                    self._condition.notify_all()

    def _handle_start_paused_target(self, request: PdbRequest) -> None:
        payload = request.payload

        for field in ('script', 'breakpoints', 'argv'):
            if field not in payload:
                self._send_error(
                    request.request_id,
                    f"Missing required payload field: {field}"
                )
                return

        for field in payload:
            if field not in ('script', 'breakpoints', 'argv'):
                self._send_error(
                    request.request_id,
                    f"Unknown payload field: {field}"
                )
                return

        with self._condition:
            if self._target_started:
                self._send_error(
                    request.request_id,
                    "Target execution already completed on this worker"
                )
                return
            if self._lifecycle['state'] != 'idle':
                self._send_error(
                    request.request_id,
                    "Target execution already completed on this worker"
                )
                return

        workspace_root = os.getcwd()
        script = payload['script']
        breakpoints_raw = payload['breakpoints']
        argv_raw = payload['argv']

        sv = self._read_validated_workspace_script(script, workspace_root, request.request_id)
        if sv is None:
            return
        script_normalized, script_abs, source_bytes = sv

        bps = self._validate_breakpoints(breakpoints_raw, source_bytes, request.request_id)
        if bps is None:
            return

        av = self._validate_argv(argv_raw, request.request_id)
        if av is None:
            return

        self._target_started = True

        with self._condition:
            self._lifecycle['state'] = 'starting'
            self._lifecycle['_start_script'] = script_normalized
            self._lifecycle['script'] = script_normalized
            self._lifecycle['line'] = 0
            self._lifecycle['function'] = ''
            self._lifecycle['exit_code'] = None
            self._lifecycle['error'] = ''
            self._lifecycle['pause_generation'] = 0
            self._lifecycle['_paused_frame'] = None

        self._target_thread = threading.Thread(
            target=self._execute_target_persistent,
            args=(script_normalized, script_abs, bps, av, source_bytes),
            daemon=True,
        )
        self._target_thread.start()

        with self._condition:
            while self._lifecycle['state'] == 'starting':
                self._condition.wait()
            state = self._lifecycle['state']

        if state == 'paused':
            result: Dict[str, Any] = {
                'state': 'paused',
                'script': script_normalized,
                'line': self._lifecycle['line'],
                'function': self._lifecycle['function'],
            }
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            )
            self._send_response(response)
        elif state in ('exited', 'failed'):
            target_thread = self._target_thread
            if target_thread is not None and target_thread is not threading.current_thread():
                target_thread.join(timeout=_WORKER_TERMINATION_TIMEOUT)
                if target_thread.is_alive():
                    self._unsafe = True
                    self._running = False
                    self._send_error(
                        request.request_id,
                        "Target thread did not complete after outcome"
                    )
                    return
                self._target_thread = None
            if state == 'exited':
                result = {
                    'state': 'exited',
                    'script': script_normalized,
                    'exit_code': self._lifecycle['exit_code'],
                }
                response = PdbResponse(
                    protocol_version=PROTOCOL_VERSION,
                    request_id=request.request_id,
                    success=True,
                    result=result,
                    error="",
                )
                self._send_response(response)
            else:
                error_msg = self._lifecycle['error']
                response = PdbResponse(
                    protocol_version=PROTOCOL_VERSION,
                    request_id=request.request_id,
                    success=False,
                    result={},
                    error=error_msg,
                )
                self._send_response(response)
        else:
            self._send_error(
                request.request_id,
                f"Unexpected target lifecycle state: {state}"
            )

    def _handle_continue_paused_target(self, request: PdbRequest) -> None:
        payload = request.payload
        if not isinstance(payload, dict):
            self._send_error(request.request_id, "payload must be a mapping")
            return
        for field in payload:
            self._send_error(
                request.request_id,
                f"Unknown payload field: {field}"
            )
            return

        failure: Optional[str] = None
        invariant_failure: Optional[str] = None
        result: Optional[Dict[str, Any]] = None
        terminal_state: Optional[str] = None
        with self._condition:
            state = self._lifecycle['state']
            target_thread = self._target_thread
            if state != 'paused':
                failure = f"Cannot continue target in state: {state}"
            elif target_thread is None:
                invariant_failure = (
                    "Paused target invariant failure: target thread is missing"
                )
            elif not target_thread.is_alive():
                invariant_failure = (
                    "Paused target invariant failure: target thread is not alive"
                )
            else:
                pause_generation = self._lifecycle['pause_generation']
                self._lifecycle['state'] = 'running'
                self._condition.notify_all()

                while True:
                    state = self._lifecycle['state']
                    current_generation = self._lifecycle['pause_generation']
                    if state == 'paused':
                        if current_generation <= pause_generation:
                            invariant_failure = (
                                "Paused target invariant failure: stale pause "
                                f"generation {current_generation} did not "
                                f"advance beyond {pause_generation}"
                            )
                        else:
                            result = {
                                'state': 'paused',
                                'script': self._lifecycle['script'],
                                'line': self._lifecycle['line'],
                                'function': self._lifecycle['function'],
                            }
                        break
                    if state in ('exited', 'failed'):
                        terminal_state = state
                        break
                    if state != 'running':
                        invariant_failure = (
                            "Paused target invariant failure: unexpected "
                            f"lifecycle state after continue: {state}"
                        )
                        break
                    self._condition.wait()

        if invariant_failure is not None:
            error = self._fail_paused_target_invariant(invariant_failure)
            self._send_error(request.request_id, error)
            return

        if failure is not None:
            self._send_error(request.request_id, failure)
            return

        if result is not None:
            self._send_response(PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            ))
            return

        target_thread = self._target_thread
        if target_thread is not None and target_thread is not threading.current_thread():
            target_thread.join(timeout=_WORKER_TERMINATION_TIMEOUT)
            if target_thread.is_alive():
                self._unsafe = True
                self._running = False
                self._target_thread = None
                self._send_error(
                    request.request_id,
                    "Target thread did not complete after continued outcome"
                )
                return
            self._target_thread = None

        with self._condition:
            state = self._lifecycle['state']
            script = self._lifecycle['script']
            exit_code = self._lifecycle['exit_code']
            error = self._lifecycle['error']

        if state != terminal_state:
            self._send_error(
                request.request_id,
                f"Continued target terminal state changed unexpectedly: {state}"
            )
        elif state == 'exited':
            self._send_response(PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result={
                    'state': 'exited',
                    'script': script,
                    'exit_code': exit_code,
                },
                error="",
            ))
        else:
            self._send_error(request.request_id, error)

    def _handle_get_target_status(self, request: PdbRequest) -> None:
        payload = request.payload
        if not isinstance(payload, dict):
            self._send_error(request.request_id, "payload must be a mapping")
            return
        for field in payload:
            self._send_error(
                request.request_id,
                f"Unknown payload field: {field}"
            )
            return

        invariant_failure: Optional[str] = None
        with self._condition:
            state = self._lifecycle['state']
            if state == 'paused':
                target_thread = self._target_thread
                if target_thread is None:
                    invariant_failure = (
                        "Paused target invariant failure during status: "
                        "target thread is missing"
                    )
                elif not target_thread.is_alive():
                    invariant_failure = (
                        "Paused target invariant failure during status: "
                        "target thread is not alive"
                    )

        if invariant_failure is not None:
            self._fail_paused_target_invariant(invariant_failure)
            with self._condition:
                state = self._lifecycle['state']

        if state == 'idle':
            result = {'state': 'idle'}
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            )
        elif state == 'paused':
            result = {
                'state': 'paused',
                'script': self._lifecycle['script'],
                'line': self._lifecycle['line'],
                'function': self._lifecycle['function'],
            }
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            )
        elif state == 'exited':
            result = {
                'state': 'exited',
                'script': self._lifecycle['script'],
                'exit_code': self._lifecycle['exit_code'],
            }
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            )
        elif state == 'failed':
            result = {
                'state': 'failed',
                'script': self._lifecycle['script'],
                'error': self._lifecycle['error'],
            }
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            )
        elif state == 'terminated':
            result = {
                'state': 'terminated',
                'script': self._lifecycle['script'],
            }
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                success=True,
                result=result,
                error="",
            )
        else:
            self._send_error(
                request.request_id,
                f"Unexpected lifecycle state: {state}"
            )
            return
        self._send_response(response)

    def _canonical_workspace_frame_script(
        self, frame: types.FrameType
    ) -> Optional[str]:
        try:
            filename = frame.f_code.co_filename
        except BaseException:
            return None
        if not isinstance(filename, str) or not filename or '\0' in filename:
            return None
        try:
            filename.encode('utf-8')
        except UnicodeEncodeError:
            return None
        if os.path.isabs(filename):
            candidate = filename
        else:
            candidate = os.path.join(self._workspace_root_real, filename)
        try:
            resolved = os.path.realpath(os.path.abspath(candidate))
            common = os.path.commonpath(
                (self._workspace_root_real, resolved)
            )
        except (OSError, ValueError):
            return None
        if os.path.normcase(common) != os.path.normcase(
                self._workspace_root_real):
            return None
        try:
            relative = os.path.relpath(resolved, self._workspace_root_real)
        except ValueError:
            return None
        relative = relative.replace('\\', '/')
        if (not relative.endswith('.py') or relative.startswith('/') or
                '\\' in relative or _has_raw_dotdot(relative) or
                (len(relative) >= 2 and relative[1] == ':') or
                posixpath.normpath(relative) != relative):
            return None
        if _safe_utf8_string(relative, _MAX_SCRIPT_PATH_UTF8) is None:
            return None
        return relative

    def _frame_metadata(
        self,
        frame: types.FrameType,
        frame_id: int,
    ) -> Optional[Dict[str, Any]]:
        script = self._canonical_workspace_frame_script(frame)
        if script is None:
            return None
        try:
            line = frame.f_lineno
            function = frame.f_code.co_name
        except BaseException:
            return None
        if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
            return None
        function = _safe_utf8_string(function, _MAX_FUNCTION_UTF8)
        if function is None:
            return None
        return {
            'frame_id': frame_id,
            'script': script,
            'line': line,
            'function': function,
            'is_current': frame_id == 0,
        }

    def _derive_workspace_stack(
        self, current: types.FrameType
    ) -> Tuple[Optional[List[Tuple[types.FrameType, Dict[str, Any]]]], int]:
        frames: List[Tuple[types.FrameType, Dict[str, Any]]] = []
        total = 0
        cursor: Optional[types.FrameType] = current
        first = True
        while cursor is not None:
            metadata = self._frame_metadata(cursor, total)
            if metadata is None:
                if first:
                    return None, 0
            else:
                if len(frames) < _MAX_STACK_FRAMES:
                    frames.append((cursor, metadata))
                total += 1
            first = False
            cursor = cursor.f_back
        return frames, total

    @staticmethod
    def _validate_frame_inspection_payload(
        payload: Dict[str, Any]
    ) -> Tuple[Optional[int], Optional[int], Optional[str]]:
        required = {'frame_id', 'pause_generation'}
        missing = required - set(payload.keys())
        if missing:
            return None, None, (
                f"Missing required payload field: {sorted(missing)[0]}"
            )
        extra = set(payload.keys()) - required
        if extra:
            return None, None, f"Unknown payload field: {sorted(extra)[0]}"
        frame_id = payload['frame_id']
        generation = payload['pause_generation']
        if isinstance(frame_id, bool) or not isinstance(frame_id, int):
            return None, None, "frame_id must be an integer"
        if frame_id < 0:
            return None, None, "frame_id must be non-negative"
        if isinstance(generation, bool) or not isinstance(generation, int):
            return None, None, "pause_generation must be an integer"
        if generation <= 0:
            return None, None, "pause_generation must be positive"
        return frame_id, generation, None

    def _inspection_snapshot(
        self,
    ) -> Tuple[
        Optional[List[Tuple[types.FrameType, Dict[str, Any]]]],
        int,
        int,
        str,
        Optional[str],
        Optional[str],
    ]:
        state = self._lifecycle.get('state')
        if state != 'paused':
            return None, 0, 0, '', (
                f"Cannot inspect target in state: {state}"
            ), None
        target_thread = self._target_thread
        if target_thread is None:
            return None, 0, 0, '', None, (
                "Paused target invariant failure during inspection: "
                "target thread is missing"
            )
        if not target_thread.is_alive():
            return None, 0, 0, '', None, (
                "Paused target invariant failure during inspection: "
                "target thread is not alive"
            )
        current = self._lifecycle.get('_paused_frame')
        if not isinstance(current, types.FrameType):
            return None, 0, 0, '', None, (
                "Paused target invariant failure during inspection: "
                "paused frame is missing"
            )
        generation = self._lifecycle.get('pause_generation')
        if (isinstance(generation, bool) or
                not isinstance(generation, int) or generation <= 0):
            return None, 0, 0, '', None, (
                "Paused target invariant failure during inspection: "
                "pause generation is invalid"
            )
        script = self._lifecycle.get('script')
        if not isinstance(script, str):
            return None, 0, 0, '', None, (
                "Paused target invariant failure during inspection: "
                "active script is invalid"
            )
        frames, total = self._derive_workspace_stack(current)
        current = None
        if frames is None or not frames:
            return None, 0, 0, '', None, (
                "Paused target invariant failure during inspection: "
                "current frame is outside the workspace or cannot be canonicalized"
            )
        if frames[0][1]['script'] != script:
            return None, 0, 0, '', None, (
                "Paused target invariant failure during inspection: "
                "current frame does not match the active target script"
            )
        return frames, total, generation, script, None, None

    def _handle_get_stack_summary(self, request: PdbRequest) -> None:
        if request.payload:
            field = sorted(request.payload.keys())[0]
            self._send_error(
                request.request_id, f"Unknown payload field: {field}"
            )
            return
        result: Optional[Dict[str, Any]] = None
        failure: Optional[str] = None
        invariant_failure: Optional[str] = None
        response: Optional[PdbResponse] = None
        frames: Optional[List[Tuple[types.FrameType, Dict[str, Any]]]] = None
        with self._condition:
            (frames, total, generation, script,
             failure, invariant_failure) = self._inspection_snapshot()
            if frames is not None:
                summaries = [dict(metadata) for _, metadata in frames]
                while summaries:
                    result = {
                        'state': 'paused',
                        'script': script,
                        'pause_generation': generation,
                        'frames': summaries,
                        'total_frames': total,
                        'truncated': total > len(summaries),
                    }
                    candidate = PdbResponse(
                        protocol_version=PROTOCOL_VERSION,
                        request_id=request.request_id,
                        success=True,
                        result=result,
                        error='',
                    )
                    if _successful_response_fits(candidate):
                        response = candidate
                        break
                    if len(summaries) == 1:
                        failure = (
                            "Current stack frame exceeds protocol response limit"
                        )
                        result = None
                        break
                    summaries.pop()
        frames = None
        if invariant_failure is not None:
            error = self._fail_paused_target_invariant(invariant_failure)
            self._send_error(request.request_id, error)
        elif failure is not None:
            self._send_error(request.request_id, failure)
        elif response is not None:
            self._send_response(response)
        else:
            self._send_error(request.request_id, "Inspection failed closed")

    def _frame_detail_result(
        self,
        frame: types.FrameType,
        metadata: Dict[str, Any],
        generation: int,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            code = frame.f_code
            argument_count = (
                code.co_argcount + code.co_kwonlyargcount
            )
            if code.co_flags & 0x04:
                argument_count += 1
            if code.co_flags & 0x08:
                argument_count += 1
            raw_arguments = code.co_varnames[:argument_count]
            local_mapping = frame.f_locals
        except BaseException:
            return None, "Frame metadata is unavailable for this pause"
        if type(local_mapping) not in (dict, _FRAME_LOCALS_PROXY_TYPE):
            return None, "Frame locals are unavailable for this pause"

        argument_names: List[str] = []
        seen_arguments = set()
        for name in raw_arguments:
            bounded = _safe_utf8_string(name, _MAX_NAME_UTF8)
            if bounded is None or bounded in seen_arguments:
                return None, "Frame argument names cannot be represented safely"
            seen_arguments.add(bounded)
            argument_names.append(bounded)

        local_names = [
            name for name in _frame_locals_keys(local_mapping)
            if type(name) is str and
            _safe_utf8_string(name, _MAX_NAME_UTF8) is not None
        ]
        local_names.sort()
        locals_count = len(local_names)
        detail = dict(metadata)
        detail.update({
            'argument_names': argument_names,
            'local_names': local_names[:_MAX_LOCAL_NAMES],
            'locals_count': locals_count,
            'locals_truncated': locals_count > _MAX_LOCAL_NAMES,
        })
        return {
            'state': 'paused',
            'pause_generation': generation,
            'frame': detail,
        }, None

    def _handle_get_frame(self, request: PdbRequest) -> None:
        frame_id, requested_generation, payload_error = (
            self._validate_frame_inspection_payload(request.payload)
        )
        if payload_error is not None:
            self._send_error(request.request_id, payload_error)
            return
        result: Optional[Dict[str, Any]] = None
        failure: Optional[str] = None
        invariant_failure: Optional[str] = None
        response: Optional[PdbResponse] = None
        frames: Optional[List[Tuple[types.FrameType, Dict[str, Any]]]] = None
        with self._condition:
            (frames, _total, generation, _script,
             failure, invariant_failure) = self._inspection_snapshot()
            if frames is not None:
                if requested_generation != generation:
                    failure = "Stale or unknown pause generation"
                elif frame_id is None or frame_id >= len(frames):
                    failure = "Unknown frame_id for current pause"
                else:
                    frame, metadata = frames[frame_id]
                    result, failure = self._frame_detail_result(
                        frame, metadata, generation
                    )
                    if result is not None:
                        while True:
                            candidate = PdbResponse(
                                protocol_version=PROTOCOL_VERSION,
                                request_id=request.request_id,
                                success=True,
                                result=result,
                                error='',
                            )
                            if _successful_response_fits(candidate):
                                response = candidate
                                break
                            local_names = result['frame']['local_names']
                            if local_names:
                                local_names.pop()
                                result['frame']['locals_truncated'] = True
                                continue
                            failure = (
                                "Frame argument metadata exceeds protocol "
                                "response limit"
                            )
                            result = None
                            break
                    frame = None
        frames = None
        if invariant_failure is not None:
            error = self._fail_paused_target_invariant(invariant_failure)
            self._send_error(request.request_id, error)
        elif failure is not None:
            self._send_error(request.request_id, failure)
        elif response is not None:
            self._send_response(response)
        else:
            self._send_error(request.request_id, "Inspection failed closed")

    def _frame_locals_result(
        self,
        frame: types.FrameType,
        frame_id: int,
        generation: int,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            local_mapping = frame.f_locals
        except BaseException:
            return None, "Frame locals are unavailable for this pause"
        if type(local_mapping) not in (dict, _FRAME_LOCALS_PROXY_TYPE):
            return None, "Frame locals are unavailable for this pause"
        names = [
            name for name in _frame_locals_keys(local_mapping)
            if type(name) is str and
            _safe_utf8_string(name, _MAX_NAME_UTF8) is not None
        ]
        names.sort()
        total_count = len(names)
        result: Dict[str, Any] = {
            'state': 'paused',
            'pause_generation': generation,
            'frame_id': frame_id,
            'locals': [],
            'total_count': total_count,
            'truncated': total_count > _MAX_LOCAL_NAMES,
        }
        for name in names[:_MAX_LOCAL_NAMES]:
            try:
                value = _frame_locals_get(local_mapping, name)
                summary = _summarize_value(value)
            except (KeyError, RuntimeError):
                result['truncated'] = True
                break
            entry = {'name': name, 'value': summary}
            result['locals'].append(entry)
            try:
                within_budget = (
                    _compact_json_size(result) <= _MAX_LOCALS_RESULT_BYTES
                )
            except (TypeError, ValueError, UnicodeEncodeError, OverflowError):
                within_budget = False
            if not within_budget:
                result['locals'].pop()
                result['truncated'] = True
                break
        if len(result['locals']) < total_count:
            result['truncated'] = True
        if _compact_json_size(result) > _MAX_LOCALS_RESULT_BYTES:
            return None, "Locals result cannot be represented within budget"
        return result, None

    def _handle_get_frame_locals(self, request: PdbRequest) -> None:
        frame_id, requested_generation, payload_error = (
            self._validate_frame_inspection_payload(request.payload)
        )
        if payload_error is not None:
            self._send_error(request.request_id, payload_error)
            return
        result: Optional[Dict[str, Any]] = None
        failure: Optional[str] = None
        invariant_failure: Optional[str] = None
        response: Optional[PdbResponse] = None
        frames: Optional[List[Tuple[types.FrameType, Dict[str, Any]]]] = None
        with self._condition:
            (frames, _total, generation, _script,
             failure, invariant_failure) = self._inspection_snapshot()
            if frames is not None:
                if requested_generation != generation:
                    failure = "Stale or unknown pause generation"
                elif frame_id is None or frame_id >= len(frames):
                    failure = "Unknown frame_id for current pause"
                else:
                    frame = frames[frame_id][0]
                    result, failure = self._frame_locals_result(
                        frame, frame_id, generation
                    )
                    if result is not None:
                        candidate = PdbResponse(
                            protocol_version=PROTOCOL_VERSION,
                            request_id=request.request_id,
                            success=True,
                            result=result,
                            error='',
                        )
                        if _successful_response_fits(candidate):
                            response = candidate
                        else:
                            failure = (
                                "Locals result exceeds protocol response limit"
                            )
                            result = None
                    frame = None
        frames = None
        if invariant_failure is not None:
            error = self._fail_paused_target_invariant(invariant_failure)
            self._send_error(request.request_id, error)
        elif failure is not None:
            self._send_error(request.request_id, failure)
        elif response is not None:
            self._send_response(response)
        else:
            self._send_error(request.request_id, "Inspection failed closed")

    def _handle_terminate_paused_target(self, request: PdbRequest) -> None:
        payload = request.payload
        if not isinstance(payload, dict):
            self._send_error(request.request_id, "payload must be a mapping")
            return
        for field in payload:
            self._send_error(
                request.request_id,
                f"Unknown payload field: {field}"
            )
            return

        with self._condition:
            state = self._lifecycle['state']

        if state != 'paused':
            self._send_error(
                request.request_id,
                f"Cannot terminate target in state: {state}"
            )
            return

        term_result = self._request_target_termination()
        if term_result.get('error'):
            self._send_error(request.request_id, term_result['error'])
            return

        result = {
            'state': 'terminated',
            'script': self._lifecycle['script'],
        }
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            success=True,
            result=result,
            error="",
        )
        self._send_response(response)

    def _send_response(self, response: PdbResponse) -> None:
        data = serialize_response(response)
        try:
            self._protocol_stdout.buffer.write(data)
            self._protocol_stdout.buffer.flush()
        except OSError:
            self._running = False

    def _send_error(
        self, request_id: int, error: str
    ) -> None:
        response = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request_id,
            success=False,
            result={},
            error=error,
        )
        self._send_response(response)

    @staticmethod
    def _diag(message: str) -> None:
        try:
            print(f"[pdb_worker] {message}", file=sys.stderr)
        except OSError:
            pass


def main() -> None:
    worker = PdbWorker()
    worker.run()


if __name__ == "__main__":
    main()
