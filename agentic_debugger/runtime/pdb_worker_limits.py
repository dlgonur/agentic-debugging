"""Shared safety/bounding constants for the PDB worker.

Single authority for every numeric bound, truncation marker, safe-type
tuple, and worker timeout used by the decomposed worker modules.
No other worker module may define a competing bound.
"""
from __future__ import annotations

import ast
import os

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
