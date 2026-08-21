from __future__ import annotations

import ast
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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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
_DISCARD_FD = os.open(os.devnull, os.O_WRONLY)
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
_MAX_SAFE_EVAL_RESULT_BYTES = 32768
_MAX_EXPRESSION_UTF8 = 1024
_MAX_AST_NODES = 64
_MAX_AST_DEPTH = 12
_MAX_EVALUATOR_STEPS = 128
_MAX_IDENTIFIER_UTF8 = 512
_MAX_CONSTANT_STRING_UTF8 = 2048
_MAX_CONSTANT_BYTES = 1024
_MAX_COMPARISON_TEXT_BYTES = 4096
_MAX_DICT_SCAN_ENTRIES = 256
_MAX_FRAME_LOCAL_ENTRIES = 4096
# Post-mortem evidence bounds: a bounded tail of traceback frames, a bounded
# set of innermost-frame locals, and byte-bounded text fields.  These keep
# post-mortem evidence deterministic and replay-safe without dumping
# unbounded source or object graphs, and without invoking any user-defined
# ``__repr__``, ``__str__``, properties, or iteration on target objects.
_POST_MORTEM_MAX_FRAMES = 16
_POST_MORTEM_MAX_LOCALS = 32
_POST_MORTEM_MAX_TEXT_UTF8 = 256
_POST_MORTEM_MAX_EXC_MESSAGE_UTF8 = 1024
_POST_MORTEM_MAX_TYPE_NAME_UTF8 = 256
_POST_MORTEM_MAX_FILE_UTF8 = 512
_POST_MORTEM_MAX_FUNCTION_UTF8 = 512
_POST_MORTEM_MAX_SCRIPT_UTF8 = 512
# Hard argument ceiling for exception summarization: at most this many
# exception arguments are ever inspected, independent of the real argument
# tuple length.  Together with _POST_MORTEM_MAX_EXC_MESSAGE_UTF8 (the total
# message byte budget) this makes exception-argument summarization
# work-bounded as well as byte-bounded: no full argument list is ever joined
# and no complete huge str/bytes value is ever copied or decoded before
# truncation.
_POST_MORTEM_EXC_ARGS_MAX_SCAN = 64
# Hard scan ceiling for the manual traceback walk: real traceback chains are
# bounded by the recursion limit (~1000 nodes), so a generous fixed ceiling
# never truncates legitimate evidence while guaranteeing termination on any
# injected cyclic or malformed chain.
_POST_MORTEM_MAX_TB_SCAN = 4096
# Hard inspection ceiling for the bounded local scan: at most this many
# mapping entries are ever inspected, independent of mapping size, and the
# scan stops as soon as _POST_MORTEM_MAX_LOCALS non-dunder names are accepted.
_POST_MORTEM_LOCALS_SCAN_CEILING = _POST_MORTEM_MAX_LOCALS * 4
# UTF-8 truncation marker; always emitted inside the declared byte budget.
_POST_MORTEM_TRUNCATION_MARKER = '\u2026'
_POST_MORTEM_TRUNCATION_MARKER_UTF8 = len(
    _POST_MORTEM_TRUNCATION_MARKER.encode('utf-8')
)
# Keeps JSON integer conversion comfortably below Python's default decimal
# conversion limit while preserving ordinary large integers losslessly.
_MAX_SERIALIZED_INT_BITS = 4096


class _BreakpointSentinel(BaseException):
    pass


class _TerminationSentinel(BaseException):
    pass


class _SafeEvaluationError(Exception):
    """Bounded ordinary failure from the read-only expression language."""


_SAFE_CONSTANT_TYPES = (type(None), bool, int, float, str, bytes)
_SAFE_DICT_KEY_TYPES = _SAFE_CONSTANT_TYPES
_SAFE_LEN_TYPES = (str, bytes, list, tuple, dict, set, frozenset)
_SAFE_SEQUENCE_TYPES = (list, tuple, str, bytes)
_SAFE_AST_TYPES = (
    ast.Expression, ast.Name, ast.Constant, ast.UnaryOp, ast.BinOp,
    ast.BoolOp, ast.Compare, ast.IfExp, ast.Subscript, ast.Call, ast.Load,
    ast.UAdd, ast.USub, ast.Invert, ast.Not, ast.Add, ast.Sub, ast.Mult,
    ast.Div, ast.FloorDiv, ast.Mod, ast.And, ast.Or, ast.Eq, ast.NotEq,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
)


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


def _frame_locals_operations(mapping: Any) -> Optional[Tuple[Any, Any]]:
    mapping_type = type(mapping)
    if mapping_type is dict:
        return dict.__len__, dict.items
    if mapping_type is _FRAME_LOCALS_PROXY_TYPE:
        return (
            _FRAME_LOCALS_PROXY_TYPE.__len__,
            _FRAME_LOCALS_PROXY_TYPE.items,
        )
    return None


def _frame_locals_lookup(
    mapping: Any, requested_name: str
) -> Tuple[bool, Any, Optional[str]]:
    """Find one exact-string local without hashing into the mapping."""
    operations = _frame_locals_operations(mapping)
    if operations is None:
        return False, None, "Frame locals are unavailable for this pause"
    if (type(requested_name) is not str or
            _safe_utf8_string(requested_name, _MAX_NAME_UTF8) is None):
        return False, None, "Requested local name is invalid"
    length_operation, items_operation = operations
    iterator: Any = None
    stored_name: Any = None
    stored_value: Any = None
    try:
        original_size = length_operation(mapping)
        iterator = iter(items_operation(mapping))
        for index in range(_MAX_FRAME_LOCAL_ENTRIES + 1):
            try:
                stored_name, stored_value = next(iterator)
            except StopIteration:
                if length_operation(mapping) != original_size:
                    return (
                        False, None,
                        "Frame locals mutated during bounded scan",
                    )
                return False, None, None
            except RuntimeError:
                return (
                    False, None,
                    "Frame locals mutated during bounded scan",
                )
            if index == _MAX_FRAME_LOCAL_ENTRIES:
                return (
                    False, None,
                    "Frame locals exceed 4096-entry scan limit",
                )
            if (type(stored_name) is str and
                    _safe_utf8_string(
                        stored_name, _MAX_NAME_UTF8
                    ) is not None and
                    str.__eq__(stored_name, requested_name) is True):
                if length_operation(mapping) != original_size:
                    return (
                        False, None,
                        "Frame locals mutated during bounded scan",
                    )
                return True, stored_value, None
    except (MemoryError, RuntimeError, TypeError, ValueError):
        return False, None, "Frame locals scan failed safely"
    finally:
        stored_value = None
        stored_name = None
        iterator = None
    return False, None, None


def _frame_locals_entries(
    mapping: Any,
) -> Tuple[Optional[List[Tuple[str, Any]]], Optional[str]]:
    """Collect bounded safe local pairs without keyed re-fetches."""
    operations = _frame_locals_operations(mapping)
    if operations is None:
        return None, "Frame locals are unavailable for this pause"
    length_operation, items_operation = operations
    entries: List[Tuple[str, Any]] = []
    iterator: Any = None
    stored_name: Any = None
    stored_value: Any = None
    try:
        original_size = length_operation(mapping)
        iterator = iter(items_operation(mapping))
        for index in range(_MAX_FRAME_LOCAL_ENTRIES + 1):
            try:
                stored_name, stored_value = next(iterator)
            except StopIteration:
                if length_operation(mapping) != original_size:
                    return None, "Frame locals mutated during bounded scan"
                entries.sort(key=lambda entry: entry[0])
                return entries, None
            except RuntimeError:
                return None, "Frame locals mutated during bounded scan"
            if index == _MAX_FRAME_LOCAL_ENTRIES:
                return None, "Frame locals exceed 4096-entry scan limit"
            if (type(stored_name) is str and
                    _safe_utf8_string(
                        stored_name, _MAX_NAME_UTF8
                    ) is not None):
                entries.append((stored_name, stored_value))
    except (MemoryError, RuntimeError, TypeError, ValueError):
        return None, "Frame locals scan failed safely"
    finally:
        stored_value = None
        stored_name = None
        iterator = None
    return None, "Frame locals scan failed safely"


def _validate_expression_envelope(expression: Any) -> Optional[str]:
    if type(expression) is not str:
        return "expression must be a string"
    if not expression:
        return "expression must be non-empty"
    if expression != expression.strip():
        return "expression must not have surrounding whitespace"
    if any(ord(character) <= 0x1f or ord(character) == 0x7f
           for character in expression):
        return "expression contains a prohibited control character"
    try:
        encoded = expression.encode('utf-8')
    except UnicodeEncodeError:
        return "expression must be valid UTF-8"
    if len(encoded) > _MAX_EXPRESSION_UTF8:
        return "expression exceeds 1024 UTF-8 bytes"
    return None


def _validate_constant(value: Any) -> None:
    value_type = type(value)
    if value_type not in _SAFE_CONSTANT_TYPES:
        raise _SafeEvaluationError("Unsupported constant type")
    if value_type is int and int.bit_length(value) > _MAX_SERIALIZED_INT_BITS:
        raise _SafeEvaluationError("Integer constant exceeds 4096-bit limit")
    if value_type is str:
        try:
            size = len(value.encode('utf-8'))
        except UnicodeEncodeError:
            raise _SafeEvaluationError("String constant is not valid UTF-8")
        if size > _MAX_CONSTANT_STRING_UTF8:
            raise _SafeEvaluationError(
                "String constant exceeds 2048-byte limit"
            )
    if value_type is bytes and bytes.__len__(value) > _MAX_CONSTANT_BYTES:
        raise _SafeEvaluationError("Bytes constant exceeds 1024-byte limit")


def _parse_safe_expression(expression: str) -> ast.Expression:
    try:
        parsed = ast.parse(expression, mode='eval')
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        raise _SafeEvaluationError("Expression syntax is invalid")

    count = 0
    pending: List[Tuple[ast.AST, int]] = [(parsed, 1)]
    try:
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > _MAX_AST_NODES:
                raise _SafeEvaluationError("Expression exceeds AST node limit")
            if depth > _MAX_AST_DEPTH:
                raise _SafeEvaluationError("Expression exceeds AST depth limit")
            if type(node) not in _SAFE_AST_TYPES:
                raise _SafeEvaluationError("Expression uses unsupported syntax")
            if type(node) is ast.Name:
                try:
                    identifier = node.id.encode('utf-8')
                except UnicodeEncodeError:
                    raise _SafeEvaluationError("Identifier is not valid UTF-8")
                if len(identifier) > _MAX_IDENTIFIER_UTF8:
                    raise _SafeEvaluationError(
                        "Identifier exceeds 512-byte limit"
                    )
            elif type(node) is ast.Constant:
                _validate_constant(node.value)
            elif type(node) is ast.Call:
                if (type(node.func) is not ast.Name or
                        node.func.id != 'len' or len(node.args) != 1 or
                        node.keywords or
                        type(node.args[0]) is ast.Starred):
                    raise _SafeEvaluationError("Unsupported function call")
            children = list(ast.iter_child_nodes(node))
            for child in reversed(children):
                pending.append((child, depth + 1))
    except _SafeEvaluationError:
        raise
    except (MemoryError, RecursionError, TypeError, ValueError):
        raise _SafeEvaluationError("Expression structure is invalid")
    return parsed


def _check_bounded_integer(value: Any, label: str) -> None:
    if type(value) is int and int.bit_length(value) > _MAX_SERIALIZED_INT_BITS:
        raise _SafeEvaluationError(f"{label} exceeds 4096-bit limit")


def _check_comparison_bound(value: Any) -> None:
    value_type = type(value)
    if value_type is int:
        _check_bounded_integer(value, "Comparison operand")
    elif value_type is str:
        if len(value.encode('utf-8')) > _MAX_COMPARISON_TEXT_BYTES:
            raise _SafeEvaluationError(
                "Comparison string exceeds 4096-byte limit"
            )
    elif value_type is bytes:
        if bytes.__len__(value) > _MAX_COMPARISON_TEXT_BYTES:
            raise _SafeEvaluationError(
                "Comparison bytes exceed 4096-byte limit"
            )


def _safe_scalar_keys_equal(left: Any, right: Any) -> bool:
    if left is right:
        return True
    left_type = type(left)
    right_type = type(right)
    if (left_type not in _SAFE_DICT_KEY_TYPES or
            right_type not in _SAFE_DICT_KEY_TYPES):
        return False
    if left_type is type(None) or right_type is type(None):
        return False
    if left_type in (bool, int, float) and right_type in (bool, int, float):
        return bool(left == right)
    if left_type is str and right_type is str:
        return bool(str.__eq__(left, right))
    if left_type is bytes and right_type is bytes:
        return bool(bytes.__eq__(left, right))
    return False


def _safe_dict_key_is_bounded(key: Any) -> bool:
    key_type = type(key)
    if key_type not in _SAFE_DICT_KEY_TYPES:
        return False
    if key_type is int:
        return int.bit_length(key) <= _MAX_SERIALIZED_INT_BITS
    if key_type is str:
        if str.__len__(key) > _MAX_COMPARISON_TEXT_BYTES:
            return False
        try:
            return len(key.encode('utf-8')) <= _MAX_COMPARISON_TEXT_BYTES
        except UnicodeEncodeError:
            return False
    if key_type is bytes:
        return bytes.__len__(key) <= _MAX_COMPARISON_TEXT_BYTES
    return True


def _safe_dict_lookup(mapping: dict, requested_key: Any) -> Any:
    if type(requested_key) not in _SAFE_DICT_KEY_TYPES:
        raise _SafeEvaluationError("Dictionary lookup key type is unsafe")
    if not _safe_dict_key_is_bounded(requested_key):
        raise _SafeEvaluationError("Dictionary lookup key exceeds safe bounds")
    original_size = dict.__len__(mapping)
    iterator = iter(dict.items(mapping))
    try:
        for index in range(_MAX_DICT_SCAN_ENTRIES + 1):
            try:
                stored_key, stored_value = next(iterator)
            except StopIteration:
                if dict.__len__(mapping) != original_size:
                    raise _SafeEvaluationError(
                        "Dictionary mutated during safe lookup"
                    )
                raise _SafeEvaluationError("Dictionary key was not found")
            except RuntimeError:
                raise _SafeEvaluationError(
                    "Dictionary mutated during safe lookup"
                )
            if index == _MAX_DICT_SCAN_ENTRIES:
                raise _SafeEvaluationError(
                    "Dictionary lookup exceeds 256-entry scan limit"
                )
            if (_safe_dict_key_is_bounded(stored_key) and
                    _safe_scalar_keys_equal(stored_key, requested_key)):
                if dict.__len__(mapping) != original_size:
                    raise _SafeEvaluationError(
                        "Dictionary mutated during safe lookup"
                    )
                return stored_value
    finally:
        iterator = None
    raise _SafeEvaluationError("Dictionary key was not found")


class _SafeExpressionInterpreter:
    def __init__(self, local_mapping: Any) -> None:
        self._locals = local_mapping
        self._steps = 0

    def evaluate(self, parsed: ast.Expression) -> Any:
        return self._evaluate_node(parsed.body)

    def _step(self) -> None:
        self._steps += 1
        if self._steps > _MAX_EVALUATOR_STEPS:
            raise _SafeEvaluationError("Expression exceeds evaluator step limit")

    def _evaluate_node(self, node: ast.AST) -> Any:
        self._step()
        node_type = type(node)
        if node_type is ast.Name:
            found, value, failure = _frame_locals_lookup(
                self._locals, node.id
            )
            if failure is not None:
                raise _SafeEvaluationError(failure)
            if not found:
                raise _SafeEvaluationError("Unknown local name")
            return value
        if node_type is ast.Constant:
            return node.value
        if node_type is ast.UnaryOp:
            return self._unary(node)
        if node_type is ast.BinOp:
            return self._binary(node)
        if node_type is ast.BoolOp:
            return self._boolean(node)
        if node_type is ast.Compare:
            return self._compare(node)
        if node_type is ast.IfExp:
            condition = self._evaluate_node(node.test)
            if type(condition) is not bool:
                raise _SafeEvaluationError(
                    "Conditional expression requires a boolean condition"
                )
            return self._evaluate_node(
                node.body if condition else node.orelse
            )
        if node_type is ast.Subscript:
            return self._subscript(node)
        if node_type is ast.Call:
            return self._intrinsic_len(node)
        raise _SafeEvaluationError("Expression uses unsupported syntax")

    def _unary(self, node: ast.UnaryOp) -> Any:
        value = self._evaluate_node(node.operand)
        operator_type = type(node.op)
        if operator_type is ast.Not:
            if type(value) is not bool:
                raise _SafeEvaluationError("not requires an exact boolean")
            return not value
        if type(value) not in (int, float) or type(value) is bool:
            raise _SafeEvaluationError("Unary numeric operand type is unsafe")
        _check_bounded_integer(value, "Unary operand")
        try:
            if operator_type is ast.UAdd:
                result = +value
            elif operator_type is ast.USub:
                result = -value
            elif operator_type is ast.Invert and type(value) is int:
                result = ~value
            else:
                raise _SafeEvaluationError("Unsupported unary operator")
        except (ArithmeticError, MemoryError, ValueError):
            raise _SafeEvaluationError("Unary operation failed safely")
        _check_bounded_integer(result, "Unary result")
        return result

    def _binary(self, node: ast.BinOp) -> Any:
        left = self._evaluate_node(node.left)
        right = self._evaluate_node(node.right)
        if (type(left) not in (int, float) or type(left) is bool or
                type(right) not in (int, float) or type(right) is bool):
            raise _SafeEvaluationError("Arithmetic operand type is unsafe")
        _check_bounded_integer(left, "Arithmetic operand")
        _check_bounded_integer(right, "Arithmetic operand")
        operands_are_finite = (
            (type(left) is int or math.isfinite(left)) and
            (type(right) is int or math.isfinite(right))
        )
        operator_type = type(node.op)
        if operator_type is ast.Mult and type(left) is int and type(right) is int:
            if (left != 0 and right != 0 and
                    int.bit_length(left) + int.bit_length(right) - 1 >
                    _MAX_SERIALIZED_INT_BITS):
                raise _SafeEvaluationError(
                    "Integer multiplication exceeds 4096-bit limit"
                )
        try:
            if operator_type is ast.Add:
                result = left + right
            elif operator_type is ast.Sub:
                result = left - right
            elif operator_type is ast.Mult:
                result = left * right
            elif operator_type is ast.Div:
                result = left / right
            elif operator_type is ast.FloorDiv:
                result = left // right
            elif operator_type is ast.Mod:
                result = left % right
            else:
                raise _SafeEvaluationError("Unsupported arithmetic operator")
        except (ArithmeticError, MemoryError, ValueError):
            raise _SafeEvaluationError("Arithmetic operation failed safely")
        if type(result) not in (int, float) or type(result) is bool:
            raise _SafeEvaluationError("Arithmetic result type is unsafe")
        if (type(result) is float and operands_are_finite and
                not math.isfinite(result)):
            raise _SafeEvaluationError(
                "Finite arithmetic overflow produced a non-finite result"
            )
        _check_bounded_integer(result, "Arithmetic result")
        return result

    def _boolean(self, node: ast.BoolOp) -> bool:
        is_and = type(node.op) is ast.And
        for value_node in node.values:
            value = self._evaluate_node(value_node)
            if type(value) is not bool:
                raise _SafeEvaluationError(
                    "Boolean operation requires exact booleans"
                )
            if is_and and not value:
                return False
            if not is_and and value:
                return True
        return is_and

    def _compare(self, node: ast.Compare) -> bool:
        left = self._evaluate_node(node.left)
        for operator_node, comparator_node in zip(node.ops, node.comparators):
            right = self._evaluate_node(comparator_node)
            operator_type = type(operator_node)
            if operator_type is ast.Is:
                matched = left is right
            elif operator_type is ast.IsNot:
                matched = left is not right
            elif operator_type in (ast.Eq, ast.NotEq):
                if (type(left) not in _SAFE_CONSTANT_TYPES or
                        type(right) not in _SAFE_CONSTANT_TYPES):
                    raise _SafeEvaluationError(
                        "Equality operand type is unsafe"
                    )
                _check_comparison_bound(left)
                _check_comparison_bound(right)
                matched = left == right
                if operator_type is ast.NotEq:
                    matched = not matched
            else:
                left_type = type(left)
                right_type = type(right)
                numeric = (
                    left_type in (int, float) and left_type is not bool and
                    right_type in (int, float) and right_type is not bool
                )
                same_text = (
                    (left_type is str and right_type is str) or
                    (left_type is bytes and right_type is bytes)
                )
                if not numeric and not same_text:
                    raise _SafeEvaluationError(
                        "Ordering operand types are unsafe"
                    )
                _check_comparison_bound(left)
                _check_comparison_bound(right)
                if operator_type is ast.Lt:
                    matched = left < right
                elif operator_type is ast.LtE:
                    matched = left <= right
                elif operator_type is ast.Gt:
                    matched = left > right
                elif operator_type is ast.GtE:
                    matched = left >= right
                else:
                    raise _SafeEvaluationError(
                        "Unsupported comparison operator"
                    )
            if type(matched) is not bool:
                raise _SafeEvaluationError("Comparison result is unsafe")
            if not matched:
                return False
            left = right
        return True

    def _intrinsic_len(self, node: ast.Call) -> int:
        value = self._evaluate_node(node.args[0])
        value_type = type(value)
        if value_type not in _SAFE_LEN_TYPES:
            raise _SafeEvaluationError("Intrinsic len operand type is unsafe")
        operations = {
            str: str.__len__, bytes: bytes.__len__, list: list.__len__,
            tuple: tuple.__len__, dict: dict.__len__, set: set.__len__,
            frozenset: frozenset.__len__,
        }
        try:
            return operations[value_type](value)
        except (MemoryError, RuntimeError):
            raise _SafeEvaluationError("Intrinsic len failed safely")

    def _subscript(self, node: ast.Subscript) -> Any:
        value = self._evaluate_node(node.value)
        key = self._evaluate_node(node.slice)
        value_type = type(value)
        if value_type in _SAFE_SEQUENCE_TYPES:
            if type(key) is not int:
                raise _SafeEvaluationError(
                    "Sequence index must be an exact integer"
                )
            _check_bounded_integer(key, "Sequence index")
            operations = {
                list: list.__getitem__, tuple: tuple.__getitem__,
                str: str.__getitem__, bytes: bytes.__getitem__,
            }
            try:
                return operations[value_type](value, key)
            except (IndexError, OverflowError):
                raise _SafeEvaluationError("Sequence index is out of range")
        if value_type is dict:
            return _safe_dict_lookup(value, key)
        raise _SafeEvaluationError("Subscript operand type is unsafe")


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


def _post_mortem_bounded_text(value: Any, maximum_utf8: int) -> str:
    """Return a UTF-8-byte-bounded, sanitized copy of a text field.

    The complete returned byte sequence — including any truncation marker —
    never exceeds ``maximum_utf8`` UTF-8 bytes.  Values that already fit are
    returned unchanged.  Only exact built-in types (``str``, ``int``,
    ``float``, ``bool``) are converted with their exact built-in ``str()``
    representation; any other type is opaque and yields ``""`` — no user
    ``__str__``/``__repr__`` is ever invoked.  Every character is encoded
    with ``errors='replace'``, so lone surrogates and malformed Unicode
    become U+FFFD and the result is always JSON-serializable.

    ``maximum_utf8`` must be a non-negative exact ``int``."""
    if type(maximum_utf8) is not int or maximum_utf8 < 0:
        return ""
    if type(value) is str:
        text = value
    elif type(value) is int:
        # Exact integers use the same safe bit ceiling as the rest of the
        # evidence machinery: values beyond _MAX_SERIALIZED_INT_BITS are never
        # decimalized (Python's integer-to-string conversion limit can raise
        # ValueError on such values) and instead render as stable bounded
        # metadata.  Any exact-built-in conversion failure fails closed.
        bits = _safe_exact_int_bits(value)
        if bits is None:
            return ""
        if bits > _MAX_SERIALIZED_INT_BITS:
            text = f"<int bits={bits}>"
        else:
            try:
                text = str(value)
            except BaseException:
                return ""
    elif type(value) is float:
        try:
            text = str(value)
        except BaseException:
            return ""
    elif type(value) is bool:
        text = str(value)
    else:
        return ""
    if maximum_utf8 == 0:
        return ""
    # A value that already fits within the limit is returned unchanged; the
    # truncation marker is only used when the value genuinely exceeds the
    # limit, and it is then included inside the declared byte budget.
    preview, truncated = _utf8_preview(text, maximum_utf8)
    if not truncated:
        return preview
    marker_utf8 = _POST_MORTEM_TRUNCATION_MARKER_UTF8
    if maximum_utf8 >= marker_utf8:
        content_preview, _ = _utf8_preview(
            text, maximum_utf8 - marker_utf8
        )
        return content_preview + _POST_MORTEM_TRUNCATION_MARKER
    return preview


# Exact descriptor-based exception identity/argument access: these CPython
# getset descriptors are read directly, bypassing any subclass property,
# custom metaclass ``__getattribute__``, or metaclass attribute hook.  A
# custom exception can never inject presentation code into this path.
_TYPE_NAME_DESCRIPTOR = type.__dict__['__name__']
_BASE_EXCEPTION_ARGS_DESCRIPTOR = BaseException.__dict__['args']


def _safe_exception_type_name(exc_type: Any) -> str:
    """Return the exact short type name of an exception type without
    consulting the instance or any metaclass presentation hook."""
    try:
        name = _TYPE_NAME_DESCRIPTOR.__get__(exc_type, type(exc_type))
    except BaseException:
        return 'unknown'
    if type(name) is not str:
        return 'unknown'
    bounded = _post_mortem_bounded_text(name, _POST_MORTEM_MAX_TYPE_NAME_UTF8)
    return bounded or 'unknown'


def _safe_exact_int_bits(value: Any) -> Optional[int]:
    """Return the exact bit length of an exact ``int``, or ``None`` on any
    failure.  ``int.bit_length`` is an exact built-in operation that never
    decimalizes the value, so it is always safe and cheap even for integers
    far beyond Python's integer-to-string conversion digit limit."""
    try:
        bits = int.bit_length(value)
    except BaseException:
        return None
    if type(bits) is not int or bits < 0:
        return None
    return bits


def _render_single_exception_arg(arg: Any, limit: int) -> Tuple[str, bool]:
    """Render one exact exception argument to at most ``limit`` UTF-8 bytes.

    Returns ``(rendered, truncated)`` where ``truncated`` is True when the
    rendered text is a bounded preview (an argument-truncation marker is then
    already included inside ``limit`` when it fits).  Only exact built-in
    operations are used: exact ``str`` values are previewed character by
    character (never copied in full), exact ``bytes`` values are sliced to a
    bounded prefix *before* decoding (the complete object is never decoded),
    exact ``int`` values are decimalized only below the safe bit ceiling and
    otherwise rendered as stable ``<int bits=N>`` metadata, and unknown/custom
    objects become opaque type metadata via :func:`_safe_type_name`.  No
    user-defined presentation or iteration hook is ever invoked."""
    if limit < 0:
        return '', True
    if type(arg) is str:
        preview, truncated = _utf8_preview(arg, limit)
        if not truncated:
            return preview, False
        if limit >= _POST_MORTEM_TRUNCATION_MARKER_UTF8:
            content, _ = _utf8_preview(
                arg, limit - _POST_MORTEM_TRUNCATION_MARKER_UTF8
            )
            return content + _POST_MORTEM_TRUNCATION_MARKER, True
        return preview, True
    if type(arg) is bytes:
        size = bytes.__len__(arg)
        if size == 0:
            return '', False
        prefix = arg[:_MAX_BYTES_PREVIEW]
        try:
            decoded = bytes.decode(prefix, 'utf-8', errors='replace')
        except BaseException:
            return _safe_type_name(arg), True
        preview, truncated = _utf8_preview(decoded, limit)
        if not truncated and size <= _MAX_BYTES_PREVIEW:
            return preview, False
        if limit >= _POST_MORTEM_TRUNCATION_MARKER_UTF8:
            content, _ = _utf8_preview(
                decoded, limit - _POST_MORTEM_TRUNCATION_MARKER_UTF8
            )
            return content + _POST_MORTEM_TRUNCATION_MARKER, True
        return preview, True
    if arg is None:
        return 'None', False
    if type(arg) is bool:
        try:
            return str(arg), False
        except BaseException:
            return _safe_type_name(arg), True
    if type(arg) is int:
        bits = _safe_exact_int_bits(arg)
        if bits is None:
            return '<int bits=unknown>', False
        if bits > _MAX_SERIALIZED_INT_BITS:
            return f"<int bits={bits}>", False
        try:
            return str(arg), False
        except BaseException:
            return f"<int bits={bits}>", False
    if type(arg) is float:
        try:
            return str(arg), False
        except BaseException:
            return _safe_type_name(arg), True
    return _safe_type_name(arg), False


def _safe_exception_message(exc: BaseException) -> str:
    """Return a bounded, side-effect-safe textual summary of an exception.

    Uses only exact descriptor operations: the type name comes from the
    ``type.__dict__['__name__']`` getset descriptor and the arguments from
    the ``BaseException.__dict__['args']`` getset descriptor, so no custom
    ``__str__``, ``__repr__``, property, or metaclass hook on the target
    exception is ever invoked.  Exact built-in scalar arguments (str, bytes,
    int, float, bool, None) are rendered by exact built-in operations;
    unknown argument objects become opaque type metadata via
    :func:`_safe_type_name`.

    The summarization is both work-bounded and byte-bounded: at most
    ``_POST_MORTEM_EXC_ARGS_MAX_SCAN`` arguments are inspected, a remaining
    UTF-8 byte budget is reduced while processing (separators and the
    omission/truncation marker ``'…'`` are included inside the same
    ``_POST_MORTEM_MAX_EXC_MESSAGE_UTF8`` budget), no list of every rendered
    argument and no full-length joined message is ever built, huge exact
    ``str`` values are only previewed, huge exact ``bytes`` values are only
    decoded from a bounded prefix, and huge exact ``int`` values are never
    decimalized.  The result is deterministic and JSON-serializable.

    Marker reservation: whenever any argument or argument tail is not
    represented and the budget can hold the marker, the final message ends
    with exactly one marker, reserved inside the budget.  Marker decisions
    use the explicit ``truncated`` metadata returned by
    :func:`_render_single_exception_arg` — never the rendered text suffix —
    so a real argument value that legitimately ends with the marker
    character is never mistaken for a synthetic marker.  When the budget is
    already full without the marker, the final represented argument is
    re-rendered at most once with the marker slot carved from its own
    limit; when even that cannot carry the marker, the final marker-less
    tail is dropped so the marker fits.  An exact empty ``str`` or
    ``bytes`` argument is still represented when only the separator fits
    (zero available argument bytes); a non-empty argument with no available
    argument bytes is omitted like any other unrepresentable argument.
    Arguments beyond the scan ceiling are never inspected, and all work
    stays bounded by argument count and byte count."""
    try:
        args = _BASE_EXCEPTION_ARGS_DESCRIPTOR.__get__(exc, type(exc))
    except BaseException:
        return '<unprintable exception>'
    if type(args) is not tuple:
        return _safe_exception_type_name(type(exc))
    total_args = tuple.__len__(args)
    if total_args == 0:
        return _safe_exception_type_name(type(exc))
    budget = _POST_MORTEM_MAX_EXC_MESSAGE_UTF8
    separator = '; '
    separator_utf8 = len(separator.encode('utf-8'))
    marker = _POST_MORTEM_TRUNCATION_MARKER
    marker_utf8 = _POST_MORTEM_TRUNCATION_MARKER_UTF8
    parts: List[str] = []
    used = 0
    rendered_count = 0
    last_arg: Any = None
    last_part_len = 0
    last_sep_cost = 0
    last_limit = 0
    last_truncated = False
    last_render_skipped = False
    for index in range(
        min(total_args, _POST_MORTEM_EXC_ARGS_MAX_SCAN)
    ):
        remaining = budget - used
        separator_cost = separator_utf8 if parts else 0
        # A negative available argument budget cannot render anything; a
        # zero budget may still represent an exact zero-byte argument (an
        # empty exact str/bytes value) when the separator itself fits.
        if remaining - separator_cost < 0:
            break
        part, arg_truncated = _render_single_exception_arg(
            arg=args[index], limit=remaining - separator_cost
        )
        part_utf8 = part.encode('utf-8', errors='replace')
        if used + separator_cost + len(part_utf8) > budget:
            last_render_skipped = True
            break
        if arg_truncated and not part_utf8:
            # A truncated argument whose preview is empty (for example a
            # non-empty argument at a zero-byte limit) cannot be represented
            # at all: it is omitted, never silently rendered as an empty
            # part.
            last_render_skipped = True
            break
        if parts:
            parts.append(separator)
        parts.append(part)
        used += separator_cost + len(part_utf8)
        rendered_count += 1
        last_arg = args[index]
        last_part_len = len(part_utf8)
        last_sep_cost = separator_cost
        last_limit = remaining - separator_cost
        last_truncated = arg_truncated
        last_render_skipped = False
        if arg_truncated:
            break
    omission = (
        rendered_count < total_args or last_truncated or last_render_skipped
    )
    if omission and marker_utf8 <= budget:
        # The marker can only be reserved when the budget can hold it;
        # otherwise the rendered prefix stays exactly as the loop produced
        # it (the final bound still trims it to the budget).
        if last_truncated and last_limit >= marker_utf8:
            # The final represented argument already ends with its own
            # synthetic marker (exact str/bytes preview); appending another
            # would create a duplicate.  Exactly one final marker holds.
            pass
        else:
            if last_truncated and parts:
                # Marker-less truncated tail (limit smaller than the marker):
                # drop it so the marker fits in the freed space.
                parts.pop()
                used -= last_part_len
                if last_sep_cost:
                    parts.pop()
                    used -= last_sep_cost
            deficit = used + marker_utf8 - budget
            if deficit <= 0:
                parts.append(marker)
                used += marker_utf8
            elif parts and not last_truncated:
                # Budget already full without a marker: carve the marker
                # slot out of the final represented argument by re-rendering
                # it once at a reduced limit.
                squeeze_limit = last_part_len - deficit
                if squeeze_limit >= 1:
                    squeezed, squeezed_truncated = _render_single_exception_arg(
                        arg=last_arg, limit=squeeze_limit
                    )
                    if squeezed_truncated and squeeze_limit >= marker_utf8:
                        squeezed_utf8 = squeezed.encode('utf-8', errors='replace')
                        parts[-1] = squeezed
                        used = used - last_part_len + len(squeezed_utf8)
                    else:
                        parts.pop()
                        used -= last_part_len
                        if last_sep_cost:
                            parts.pop()
                            used -= last_sep_cost
                        parts.append(marker)
                        used += marker_utf8
                else:
                    parts.pop()
                    used -= last_part_len
                    if last_sep_cost:
                        parts.pop()
                        used -= last_sep_cost
                    parts.append(marker)
                    used += marker_utf8
            elif used + marker_utf8 <= budget:
                parts.append(marker)
                used += marker_utf8
    message = ''.join(parts)
    if not message:
        message = _safe_exception_type_name(type(exc))
    return _post_mortem_bounded_text(message, budget)


def _safe_exception_error_message(exc: BaseException) -> str:
    """Post-mortem exception message with the established evidence shape.

    Returns ``"Target raised <type>: <message>"`` built exclusively from
    :func:`_safe_exception_type_name` and :func:`_safe_exception_message`, so
    no target-defined presentation code is executed.  UTF-8-byte-bounded."""
    type_name = _safe_exception_type_name(type(exc))
    message = _safe_exception_message(exc)
    return _post_mortem_bounded_text(
        f"Target raised {type_name}: {message}",
        _POST_MORTEM_MAX_EXC_MESSAGE_UTF8,
    )


def _safe_local_summary(value: Any) -> Dict[str, Any]:
    """Return a bounded, side-effect-safe summary of a local variable value.

    Reuses the accepted :func:`_summarize_value` machinery, which uses only
    exact built-in operations (``type()``, ``len()``, ``str.__len__``,
    ``bytes.hex``, ``int.bit_length``, descriptor ``__get__`` on the *type*
    not the instance).  Never calls ``repr()``, ``str()``, ``__repr__``,
    ``__str__``, properties, or iteration on the *value instance*.  For
    unknown/custom types, reports ``kind: 'object'`` with bounded type
    metadata and no value preview."""
    try:
        return _summarize_value(value)
    except BaseException:
        return _empty_value_summary('object', value)


def _has_traceback(captured_exc: Optional[Tuple[type, BaseException, Any]]) -> bool:
    """Decide whether a captured exception carries a real traceback.

    Factored as a pure helper so the worker's fail-closed decision
    (``captured_exc is None or captured_exc[2] is None``) is testable in
    isolation without relying on ``raise exc.with_traceback(None)``."""
    if captured_exc is None:
        return False
    return captured_exc[2] is not None


def _bounded_traceback_frames(
    tb: Any,
) -> Tuple[List[Dict[str, Any]], Any, bool, bool, Optional[str]]:
    """Walk a traceback chain with a hard scan ceiling, retaining only the
    innermost ``_POST_MORTEM_MAX_FRAMES`` frames.

    Never materializes the complete chain and never loads source lines: each
    visited node contributes only its frame's ``co_filename``, ``co_name``,
    and ``f_lineno`` (exact frame/code metadata reads).  The walk stops after
    at most ``_POST_MORTEM_MAX_TB_SCAN`` visited nodes, which guarantees
    termination on injected cyclic chains; a malformed node (missing or
    inaccessible expected fields) fails closed.  The innermost frame object
    is captured during the single walk so the caller never rewalks the
    chain.

    Returns ``(frames, innermost_frame, truncation_marker, walk_terminated,
    error)`` where ``frames`` is the deterministic innermost tail in
    outermost-to-innermost order, ``innermost_frame`` is the last visited
    frame object (or None on malformed structure), ``truncation_marker`` is
    True when more frames existed than the reported tail,
    ``walk_terminated`` is True when the hard scan ceiling was hit, and
    ``error`` is a bounded fail-closed reason or None."""
    frames: List[Dict[str, Any]] = []
    tail: List[Dict[str, Any]] = []
    innermost_frame: Any = None
    visited = 0
    node = tb
    while node is not None:
        if visited >= _POST_MORTEM_MAX_TB_SCAN:
            frames = list(tail)
            return frames, innermost_frame, True, True, (
                "traceback scan ceiling reached"
            )
        visited += 1
        try:
            frame = node.tb_frame
            code = frame.f_code
            filename = code.co_filename
            func_name = code.co_name
            lineno = frame.f_lineno
        except BaseException:
            frames = list(tail)
            return frames, None, True, False, "traceback node is malformed"
        if type(filename) is not str:
            filename = ''
        if type(func_name) is not str:
            func_name = ''
        if type(lineno) is not int:
            lineno = 0
        entry = {
            'file': _post_mortem_bounded_text(
                os.path.basename(filename) if filename else '',
                _POST_MORTEM_MAX_FILE_UTF8,
            ),
            'line': lineno,
            'function': _post_mortem_bounded_text(
                func_name, _POST_MORTEM_MAX_FUNCTION_UTF8,
            ),
        }
        if len(tail) >= _POST_MORTEM_MAX_FRAMES:
            tail.pop(0)
        tail.append(entry)
        innermost_frame = frame
        try:
            node = node.tb_next
        except BaseException:
            frames = list(tail)
            return frames, innermost_frame, True, False, (
                "traceback chain is malformed"
            )
    frames = list(tail)
    truncated = visited > _POST_MORTEM_MAX_FRAMES
    return frames, innermost_frame, truncated, False, None


def _collect_bounded_locals(
    mapping: Any,
    max_entries: int,
    length_op: Optional[Callable[[Any], int]] = None,
    items_op: Optional[Callable[[Any], Any]] = None,
) -> Tuple[Optional[List[Tuple[str, Any]]], int, bool, Optional[str]]:
    """Collect a deterministic bounded set of local name/value pairs.

    Uses only the exact mapping length and items operations: for a plain
    ``dict`` or the frame-locals proxy these are resolved from the accepted
    :func:`_frame_locals_operations`; tests may inject equivalent exact
    operations as a narrow seam.  The mapping is iterated lazily — never
    materialized via ``list(mapping.items())`` — the scan budget is checked
    *before* every iterator advance, and at most
    ``_POST_MORTEM_LOCALS_SCAN_CEILING`` entries are ever inspected (actual
    successful iterator advances never exceed the declared ceiling and
    ``inspected`` equals the number of successful advances; no ``next()``
    probe is issued after the budget is exhausted).  The accepted non-dunder
    entries are sorted deterministically by name.  A size change during the
    scan (mutation) or any iteration failure fails closed.

    When the exact mapping length is available, unseen entries are decided
    from it: ``original_size > inspected`` reports truncation, ``original_size
    == inspected`` does not, and no extra advance is required to discover
    whether a further accepted entry exists.  When the exact length is
    unavailable and the scan ceiling is exhausted, truncation is reported
    without advancing once more; when the exact length is unavailable and the
    acceptance bound (``max_entries``) was reached, one additional advance is
    required to discover whether more entries remain (a further successful
    advance reports truncation; ``StopIteration`` means none remain).

    Returns ``(entries, inspected, truncated, error)``: ``entries`` is
    ``None`` on failure, ``inspected`` is the exact number of mapping entries
    examined, ``truncated`` is True when accepted entries were dropped, and
    ``error`` is a bounded reason or None."""
    if type(max_entries) is not int or max_entries < 0:
        return None, 0, False, "local limit is invalid"
    operations = (
        (length_op, items_op)
        if length_op is not None and items_op is not None
        else _frame_locals_operations(mapping)
    )
    if operations is None:
        return None, 0, False, "frame locals are unavailable for this pause"
    length_operation, items_operation = operations
    entries: List[Tuple[str, Any]] = []
    inspected = 0
    collected = 0
    truncated = False
    iterator: Any = None
    stored_name: Any = None
    stored_value: Any = None
    try:
        raw_size = length_operation(mapping)
        if type(raw_size) is int and raw_size >= 0:
            original_size = raw_size
            exact_length = True
        else:
            original_size = None
            exact_length = False
        iterator = iter(items_operation(mapping))
        while True:
            if exact_length and inspected >= original_size:
                # The exact mapping length proves the scan is complete; no
                # further advance (and no StopIteration probe) is needed.
                break
            if inspected >= _POST_MORTEM_LOCALS_SCAN_CEILING:
                # Budget exhausted before any further advance.  With an exact
                # mapping length this implies unseen entries remain
                # (otherwise the length-exit above would have fired); with no
                # usable length, report truncation honestly without advancing.
                truncated = True
                break
            if collected >= max_entries:
                if exact_length:
                    truncated = True
                    break
                # No usable length: one additional advance is required to
                # discover whether more entries remain.
            try:
                stored_name, stored_value = next(iterator)
            except StopIteration:
                if exact_length and length_operation(mapping) != original_size:
                    return None, inspected, False, (
                        "frame locals mutated during bounded scan"
                    )
                break
            except RuntimeError:
                return None, inspected, False, (
                    "frame locals mutated during bounded scan"
                )
            inspected += 1
            if type(stored_name) is not str:
                continue
            if stored_name.startswith('__'):
                continue
            if collected >= max_entries:
                truncated = True
                break
            collected += 1
            entries.append((stored_name, stored_value))
        entries.sort(key=lambda entry: entry[0])
        return entries, inspected, truncated, None
    except (MemoryError, RuntimeError, TypeError, ValueError):
        return None, inspected, False, "frame locals scan failed safely"
    finally:
        stored_value = None
        stored_name = None
        iterator = None


def _bounded_local_repr_pure(value: Any) -> str:
    """Return a bounded, side-effect-safe textual summary of a local value.

    Deprecated in favor of :func:`_safe_local_summary`; retained only for
    backward compatibility with earlier tests.  Does NOT invoke
    ``repr(value)`` — uses :func:`_safe_local_summary` and renders the
    ``kind``/``type``/``value`` fields into a bounded string."""
    summary = _safe_local_summary(value)
    parts = [summary.get('kind', 'object')]
    type_name = summary.get('type')
    if type_name and type_name != 'object':
        parts.append(type_name)
    val = summary.get('value')
    if val is not None and type(val) is str:
        parts.append(_post_mortem_bounded_text(val, _POST_MORTEM_MAX_TEXT_UTF8))
    return '<' + ' '.join(parts) + '>'


def _capture_post_mortem_evidence_pure(
    script_normalized: str,
    exc_type: type,
    exc_value: BaseException,
    tb: Any,
    safe_error_message: Callable[[BaseException], str],
) -> Dict[str, Any]:
    """Build bounded, side-effect-safe post-mortem evidence from a captured
    exception.

    Pure module-level helper (no ``self``) so it can be unit-tested with
    controlled injection (e.g. a ``None`` traceback, a deep frame chain, many
    locals, adversarial objects).  Uses only exact built-in operations and the
    accepted :func:`_summarize_value` / :func:`_safe_type_name` machinery —
    never calls ``repr()``, ``str()``, ``__repr__``, ``__str__``, properties,
    or iteration on target *value instances*.  All text fields are
    UTF-8-byte-bounded (truncation marker included in the budget).  The
    exception type identity comes from the ``type.__dict__['__name__']``
    descriptor (no metaclass hooks); the message comes from
    ``safe_error_message``, which the worker supplies as the side-effect-safe
    :func:`_safe_exception_error_message`.  Traceback frames are produced by
    the single bounded walk :func:`_bounded_traceback_frames` (hard scan
    ceiling, innermost tail, no source loading); the innermost frame's
    locals are collected by :func:`_collect_bounded_locals` with a hard
    inspection ceiling and fail-closed mutation handling.  If ``tb`` is
    ``None``, returns empty frame/local evidence (the caller is responsible
    for the fail-closed decision via :func:`_has_traceback`)."""
    exc_repr = safe_error_message(exc_value)
    type_name = _safe_exception_type_name(exc_type)
    exc_message = _post_mortem_bounded_text(exc_repr, _POST_MORTEM_MAX_EXC_MESSAGE_UTF8)
    script_bounded = _post_mortem_bounded_text(script_normalized, _POST_MORTEM_MAX_SCRIPT_UTF8)
    frames: List[Dict[str, Any]] = []
    innermost_frame: Any = None
    frames_truncated = False
    traceback_error: Optional[str] = None
    if tb is not None:
        frames, innermost_frame, frames_truncated, _terminated, tb_error = (
            _bounded_traceback_frames(tb)
        )
        if tb_error is not None:
            frames_truncated = True
            traceback_error = _post_mortem_bounded_text(
                tb_error, _POST_MORTEM_MAX_TEXT_UTF8
            )
    innermost: Dict[str, Any] = {}
    if innermost_frame is not None:
        local_names: List[str] = []
        local_values: List[Dict[str, Any]] = []
        truncated_locals = False
        try:
            local_mapping = innermost_frame.f_locals
        except BaseException:
            local_mapping = None
        if local_mapping is None:
            truncated_locals = True
        else:
            entries, _inspected, truncated_locals, _local_error = (
                _collect_bounded_locals(
                    local_mapping, _POST_MORTEM_MAX_LOCALS
                )
            )
            if entries is None:
                entries = []
                truncated_locals = True
            for name, value in entries:
                bounded_name = _post_mortem_bounded_text(
                    name, _POST_MORTEM_MAX_TEXT_UTF8
                )
                local_names.append(bounded_name)
                summary = _safe_local_summary(value)
                local_values.append({
                    'name': bounded_name,
                    'summary': summary,
                    'type': _post_mortem_bounded_text(
                        summary.get('type') or 'unknown',
                        _POST_MORTEM_MAX_TYPE_NAME_UTF8,
                    ),
                })
        try:
            code = innermost_frame.f_code
            co_filename = code.co_filename
            co_name = code.co_name
            lineno = innermost_frame.f_lineno
        except BaseException:
            co_filename = ''
            co_name = ''
            lineno = 0
        if type(co_filename) is not str:
            co_filename = ''
        if type(co_name) is not str:
            co_name = ''
        if type(lineno) is not int:
            lineno = 0
        innermost = {
            'file': _post_mortem_bounded_text(
                os.path.basename(co_filename) if co_filename else '',
                _POST_MORTEM_MAX_FILE_UTF8,
            ),
            'line': lineno,
            'function': _post_mortem_bounded_text(
                co_name, _POST_MORTEM_MAX_FUNCTION_UTF8,
            ),
            'local_names': local_names,
            'local_values': local_values,
            'locals_truncated': truncated_locals,
        }
    evidence: Dict[str, Any] = {
        'exception': {
            'type': type_name,
            'message': exc_message,
            'repr': _post_mortem_bounded_text(
                f"{type_name}: {exc_message}",
                _POST_MORTEM_MAX_EXC_MESSAGE_UTF8,
            ),
        },
        'traceback_frames': frames,
        'innermost_frame': innermost,
        'script': script_bounded,
        'frames_truncated': frames_truncated,
    }
    if traceback_error is not None:
        evidence['traceback_error'] = traceback_error
    return evidence


def _post_mortem_missing_traceback_response(request_id: int) -> PdbResponse:
    """Authoritative fail-closed response for a captured exception with no
    traceback: success False, empty result, bounded non-empty error, and no
    fabricated frame or local evidence.  The worker sends exactly this
    response, so tests exercise the real branch contract."""
    error_msg = (
        "post-mortem entry rejected: no traceback was captured "
        "for the failing target"
    )
    return PdbResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        success=False,
        result={},
        error=error_msg,
    )


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
        # Pytest's fd-level capture redirects descriptor 1 inside an
        # in-process target.  Keep the JSON protocol on a protected duplicate
        # so exact pytest reproductions cannot redirect or close the channel.
        self._protocol_stdout = os.fdopen(
            os.dup(sys.stdout.fileno()),
            "w",
            encoding="utf-8",
            newline="",
        )
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
            '_resume_mode': None,
            '_resume_frame': None,
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
        elif op == "step_paused_target":
            self._handle_step_paused_target(request)
        elif op == "next_paused_target":
            self._handle_next_paused_target(request)
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
        elif op == "safe_eval_expression":
            self._handle_safe_eval_expression(request)
        elif op == "run_post_mortem":
            self._handle_run_post_mortem(request)
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

    def _handle_run_post_mortem(self, request: PdbRequest) -> None:
        """Run a Python script to completion; if it terminates with an
        unhandled exception, capture the structured traceback as post-mortem
        runtime evidence.  No interactive PDB session is entered: the worker
        captures the failure's call stack, exception identity, and the
        innermost frame's locals snapshot deterministically, then reports it
        as a ``post_mortem`` result.  A successful exit produces no
        post-mortem evidence (the result reports ``status: "exited"`` with
        ``post_mortem: false``); a failure without a traceback (e.g. a bare
        ``SystemExit``) fails closed with ``status: "failed"`` and no
        fabricated traceback.  Exactly one execution is allowed per worker."""
        payload = request.payload

        for field in ('script', 'argv'):
            if field not in payload:
                self._send_error(
                    request.request_id,
                    f"Missing required payload field: {field}"
                )
                return

        for field in payload:
            if field not in ('script', 'argv'):
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
        argv_raw = payload['argv']

        sv = self._read_validated_workspace_script(script, workspace_root, request.request_id)
        if sv is None:
            return
        script_normalized, script_abs, source_bytes = sv

        av = self._validate_argv(argv_raw, request.request_id)
        if av is None:
            return

        self._target_started = True
        self._execute_post_mortem_target(script_normalized, script_abs, av, source_bytes, request.request_id)

    def _execute_post_mortem_target(
        self,
        script_normalized: str,
        script_abs: str,
        argv: List[str],
        source_bytes: bytes,
        request_id: int,
    ) -> None:
        """Run the script and capture post-mortem traceback evidence on failure.

        Reuses the same argv/path/stdin/stdout isolation and restoration
        contract as :meth:`_execute_target`, but never installs a trace
        function and never enters an interactive paused state.  The captured
        evidence is bounded and sanitized: only the exception type, message,
        and a bounded list of traceback frames (file, line, function) plus
        the innermost frame's local-variable names and bounded repr values.
        """
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
                error_msg = self._safe_error_message(e)
                self._send_error(request_id, error_msg)
                with self._condition:
                    self._lifecycle['state'] = 'failed'
                    self._lifecycle['script'] = script_normalized
                    self._lifecycle['error'] = error_msg
                    self._condition.notify_all()
                return

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
                    'post_mortem': False,
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
                captured_exc = (type(e), e, e.__traceback__)
            else:
                result = {
                    'status': 'exited',
                    'script': script_normalized,
                    'exit_code': 0,
                    'post_mortem': False,
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

            # An unhandled exception was captured: build the structured
            # post-mortem evidence.  If the traceback is missing, fail closed
            # rather than fabricate frame evidence.  The traceback-presence
            # decision is factored into _has_traceback for testability, and
            # the authoritative fail-closed response is built by
            # _post_mortem_missing_traceback_response so the real branch is
            # testable without a subprocess.
            if not _has_traceback(captured_exc):
                response = _post_mortem_missing_traceback_response(request_id)
                self._send_response(response)
                with self._condition:
                    self._lifecycle['state'] = 'failed'
                    self._lifecycle['script'] = script_normalized
                    self._lifecycle['error'] = response.error
                    self._condition.notify_all()
                return

            evidence = self._capture_post_mortem_evidence(
                script_normalized, captured_exc[0], captured_exc[1], captured_exc[2]
            )
            result: Dict[str, Any] = {
                'status': 'post_mortem',
                'script': evidence.get('script', script_normalized),
                'post_mortem': True,
                'exception': evidence['exception'],
                'traceback_frames': evidence['traceback_frames'],
                'innermost_frame': evidence['innermost_frame'],
                'frames_truncated': evidence.get('frames_truncated', False),
            }
            if evidence.get('traceback_error'):
                result['traceback_error'] = evidence['traceback_error']
            response = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request_id,
                success=True,
                result=result,
                error="",
            )
            self._send_response(response)
            with self._condition:
                self._lifecycle['state'] = 'failed'
                self._lifecycle['script'] = script_normalized
                self._lifecycle['error'] = evidence['exception']['repr']
                self._condition.notify_all()
        finally:
            sys.argv = saved_argv
            sys.path = saved_path
            sys.stdin = saved_stdin
            sys.stdout = saved_stdout
            sys.stderr = saved_stderr
            sys.__stdout__ = saved_dunder_stdout
            sys.__stderr__ = saved_dunder_stderr
            os.chdir(saved_cwd)

    def _capture_post_mortem_evidence(
        self,
        script_normalized: str,
        exc_type: type,
        exc_value: BaseException,
        tb: Any,
    ) -> Dict[str, Any]:
        """Build bounded, sanitized post-mortem evidence from a captured
        exception (delegates to the module-level pure helper with the
        side-effect-safe exception message function, so no target-defined
        ``__str__``/``__repr__``/metaclass presentation code is invoked)."""
        return _capture_post_mortem_evidence_pure(
            script_normalized, exc_type, exc_value, tb,
            _safe_exception_error_message,
        )

    def _bounded_local_repr(self, value: Any) -> str:
        """Return a bounded, sanitized repr of a local variable value."""
        return _bounded_local_repr_pure(value)

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
            sys.__stdout__ = saved_dunder_stdout
            sys.__stderr__ = saved_dunder_stderr
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
            self._lifecycle['_resume_mode'] = None
            self._lifecycle['_resume_frame'] = None
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
        saved_dunder_stdout = sys.__stdout__
        saved_dunder_stderr = sys.__stderr__
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
            sys.__stdout__ = sys.stdout
            sys.__stderr__ = sys.stderr

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
            sys.__stdout__ = saved_dunder_stdout
            sys.__stderr__ = saved_dunder_stderr
            os.chdir(saved_cwd)
            sys.settrace(None)
            sys.settrace(saved_trace)
            if pending_state is not None:
                with self._condition:
                    self._lifecycle['state'] = pending_state
                    self._lifecycle['_paused_frame'] = None
                    self._lifecycle['_resume_mode'] = None
                    self._lifecycle['_resume_frame'] = None
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
            self._lifecycle['_resume_mode'] = None
            self._lifecycle['_resume_frame'] = None

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
        self._handle_resume_paused_target(request, "continue")

    def _handle_step_paused_target(self, request: PdbRequest) -> None:
        self._handle_resume_paused_target(request, "step")

    def _handle_next_paused_target(self, request: PdbRequest) -> None:
        self._handle_resume_paused_target(request, "next")

    def _handle_resume_paused_target(
        self,
        request: PdbRequest,
        mode: str,
    ) -> None:
        if mode not in {"continue", "step", "next"}:
            self._send_error(request.request_id, "Unsupported resume mode")
            return
        verb = mode
        past_tense = {"continue": "continued", "step": "stepped", "next": "nexted"}[mode]
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
                failure = f"Cannot {verb} target in state: {state}"
            elif target_thread is None:
                invariant_failure = (
                    "Paused target invariant failure: target thread is missing"
                )
            elif not target_thread.is_alive():
                invariant_failure = (
                    "Paused target invariant failure: target thread is not alive"
                )
            else:
                paused_frame = self._lifecycle.get('_paused_frame')
                if mode == 'next' and paused_frame is None:
                    invariant_failure = (
                        "Paused target invariant failure: paused frame is missing"
                    )
                    paused_frame = None
                if invariant_failure is not None:
                    pass
                else:
                    self._lifecycle['_resume_mode'] = (
                        mode if mode in {'step', 'next'} else None
                    )
                    self._lifecycle['_resume_frame'] = (
                        paused_frame if mode == 'next' else None
                    )
                pause_generation = self._lifecycle['pause_generation']
                if invariant_failure is None:
                    self._lifecycle['state'] = 'running'
                    self._condition.notify_all()

                while invariant_failure is None:
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
                            f"lifecycle state after {verb}: {state}"
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
                    f"Target thread did not complete after {past_tense} outcome"
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
                f"{past_tense.capitalize()} target terminal state changed "
                f"unexpectedly: {state}"
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

    @staticmethod
    def _namespace_local_mapping(
        frame: types.FrameType,
    ) -> Tuple[Optional[Any], Optional[str]]:
        try:
            local_mapping = frame.f_locals
            global_mapping = frame.f_globals
        except BaseException:
            return None, "Frame locals are unavailable for this pause"
        if local_mapping is global_mapping:
            return None, "Module-scope frame values are unavailable"
        if type(local_mapping) not in (dict, _FRAME_LOCALS_PROXY_TYPE):
            return None, "Frame locals are unavailable for this pause"
        return local_mapping, None

    def _frame_detail_result(
        self,
        frame: types.FrameType,
        metadata: Dict[str, Any],
        generation: int,
        local_mapping: Any,
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
        except BaseException:
            return None, "Frame metadata is unavailable for this pause"

        argument_names: List[str] = []
        seen_arguments = set()
        for name in raw_arguments:
            bounded = _safe_utf8_string(name, _MAX_NAME_UTF8)
            if bounded is None or bounded in seen_arguments:
                return None, "Frame argument names cannot be represented safely"
            seen_arguments.add(bounded)
            argument_names.append(bounded)

        local_entries, locals_failure = _frame_locals_entries(local_mapping)
        if locals_failure is not None or local_entries is None:
            return None, locals_failure or "Frame locals scan failed safely"
        local_names = [name for name, _value in local_entries]
        local_entries = None
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
        local_mapping: Any = None
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
                    local_mapping, failure = self._namespace_local_mapping(frame)
                    if local_mapping is not None:
                        result, failure = self._frame_detail_result(
                            frame, metadata, generation, local_mapping
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
        local_mapping = None
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
        frame_id: int,
        generation: int,
        local_mapping: Any,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        entries, entries_failure = _frame_locals_entries(local_mapping)
        if entries_failure is not None or entries is None:
            return None, entries_failure or "Frame locals scan failed safely"
        total_count = len(entries)
        result: Dict[str, Any] = {
            'state': 'paused',
            'pause_generation': generation,
            'frame_id': frame_id,
            'locals': [],
            'total_count': total_count,
            'truncated': total_count > _MAX_LOCAL_NAMES,
        }
        value: Any = None
        try:
            for index, (name, value) in enumerate(entries):
                if index >= _MAX_LOCAL_NAMES:
                    break
                summary = _summarize_value(value)
                entry = {'name': name, 'value': summary}
                result['locals'].append(entry)
                try:
                    within_budget = (
                        _compact_json_size(result) <= _MAX_LOCALS_RESULT_BYTES
                    )
                except (
                    TypeError, ValueError, UnicodeEncodeError, OverflowError,
                ):
                    within_budget = False
                if not within_budget:
                    result['locals'].pop()
                    result['truncated'] = True
                    break
        finally:
            value = None
            entries = None
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
        local_mapping: Any = None
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
                    local_mapping, failure = self._namespace_local_mapping(frame)
                    if local_mapping is not None:
                        result, failure = self._frame_locals_result(
                            frame_id, generation, local_mapping
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
        local_mapping = None
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

    @staticmethod
    def _validate_safe_eval_payload(
        payload: Dict[str, Any]
    ) -> Tuple[Optional[int], Optional[int], Optional[str], Optional[str]]:
        required = {'frame_id', 'pause_generation', 'expression'}
        missing = required - set(payload.keys())
        if missing:
            return None, None, None, (
                f"Missing required payload field: {sorted(missing)[0]}"
            )
        extra = set(payload.keys()) - required
        if extra:
            return None, None, None, (
                f"Unknown payload field: {sorted(extra)[0]}"
            )
        frame_id = payload['frame_id']
        generation = payload['pause_generation']
        expression = payload['expression']
        if type(frame_id) is not int:
            return None, None, None, "frame_id must be an integer"
        if frame_id < 0:
            return None, None, None, "frame_id must be non-negative"
        if type(generation) is not int:
            return None, None, None, "pause_generation must be an integer"
        if generation <= 0:
            return None, None, None, "pause_generation must be positive"
        expression_error = _validate_expression_envelope(expression)
        if expression_error is not None:
            return None, None, None, expression_error
        return frame_id, generation, expression, None

    def _handle_safe_eval_expression(self, request: PdbRequest) -> None:
        (frame_id, requested_generation, expression,
         payload_error) = self._validate_safe_eval_payload(request.payload)
        if payload_error is not None:
            self._send_error(request.request_id, payload_error)
            return

        failure: Optional[str] = None
        invariant_failure: Optional[str] = None
        response: Optional[PdbResponse] = None
        frames: Optional[List[Tuple[types.FrameType, Dict[str, Any]]]] = None
        parsed: Optional[ast.Expression] = None
        interpreter: Optional[_SafeExpressionInterpreter] = None
        evaluated_value: Any = None
        local_mapping: Any = None
        frame: Optional[types.FrameType] = None
        try:
            with self._condition:
                (frames, _total, generation, _script,
                 failure, invariant_failure) = self._inspection_snapshot()
                if frames is not None:
                    if requested_generation != generation:
                        failure = "Stale or unknown pause generation"
                    elif frame_id is None or frame_id >= len(frames):
                        failure = "Unknown frame_id for current pause"
                    else:
                        try:
                            frame, metadata = frames[frame_id]
                            local_mapping, failure = (
                                self._namespace_local_mapping(frame)
                            )
                            if failure is not None or local_mapping is None:
                                raise _SafeEvaluationError(
                                    failure or
                                    "Frame locals are unavailable for this pause"
                                )
                            parsed = _parse_safe_expression(expression)
                            interpreter = _SafeExpressionInterpreter(local_mapping)
                            evaluated_value = interpreter.evaluate(parsed)
                            result = {
                                'state': 'paused',
                                'pause_generation': generation,
                                'frame': dict(metadata),
                                'expression': expression,
                                'value': _summarize_value(evaluated_value),
                            }
                            try:
                                result_fits = (
                                    _compact_json_size(result) <=
                                    _MAX_SAFE_EVAL_RESULT_BYTES
                                )
                            except (
                                TypeError, ValueError, UnicodeEncodeError,
                                OverflowError, MemoryError, RecursionError,
                            ):
                                result_fits = False
                            if not result_fits:
                                failure = (
                                    "Safe-evaluation result exceeds "
                                    "32768-byte budget"
                                )
                            else:
                                candidate = PdbResponse(
                                    protocol_version=PROTOCOL_VERSION,
                                    request_id=request.request_id,
                                    success=True,
                                    result=result,
                                    error='',
                                )
                                try:
                                    response_fits = _successful_response_fits(
                                        candidate
                                    )
                                except PdbProtocolError:
                                    response_fits = False
                                if response_fits:
                                    response = candidate
                                else:
                                    failure = (
                                        "Safe-evaluation response exceeds "
                                        "65536-byte protocol limit"
                                    )
                        except _SafeEvaluationError as exc:
                            failure = str(exc)
                        except BaseException:
                            failure = "Safe evaluation failed closed"
        finally:
            evaluated_value = None
            interpreter = None
            local_mapping = None
            parsed = None
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
            self._send_error(
                request.request_id, "Safe evaluation failed closed"
            )

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
