"""Safe-expression parsing and evaluation for the PDB worker.

Owns the read-only expression grammar, AST bounds, and the step-bounded
interpreter. Evaluation uses only exact built-in operations and the bounded
frame-local lookup; hostile target hooks are never invoked.
"""
from __future__ import annotations

import ast
import math
from typing import Any, Dict, List, Optional, Tuple

from agentic_debugger.runtime.pdb_worker_frames import _frame_locals_lookup
from agentic_debugger.runtime.pdb_worker_limits import (
    _MAX_AST_DEPTH,
    _MAX_AST_NODES,
    _MAX_COMPARISON_TEXT_BYTES,
    _MAX_CONSTANT_BYTES,
    _MAX_CONSTANT_STRING_UTF8,
    _MAX_DICT_SCAN_ENTRIES,
    _MAX_EVALUATOR_STEPS,
    _MAX_EXPRESSION_UTF8,
    _MAX_IDENTIFIER_UTF8,
    _MAX_SERIALIZED_INT_BITS,
    _SAFE_AST_TYPES,
    _SAFE_CONSTANT_TYPES,
    _SAFE_DICT_KEY_TYPES,
    _SAFE_LEN_TYPES,
    _SAFE_SEQUENCE_TYPES,
)

class _SafeEvaluationError(Exception):
    """Bounded ordinary failure from the read-only expression language."""


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
