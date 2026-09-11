"""Single authority for PDB session bounds, timeouts and protocol field sets.

This module owns every numeric bound, timeout default and strict field-set
constant used by the client-side :class:`PdbSession` implementation and its
helpers.  Keeping them here (instead of duplicating them across transport,
validation and control helpers) guarantees the hostile-input protections
cannot drift between modules.

Dependency direction: this module depends only on the standard library.
All other ``pdb_session_*`` helpers depend on it; it depends on nothing
inside the runtime.
"""

from __future__ import annotations

import os
from types import MappingProxyType

_DEFAULT_STARTUP_TIMEOUT = 5.0
_DEFAULT_REQUEST_TIMEOUT = 5.0
_DEFAULT_SHUTDOWN_TIMEOUT = 2.0
_DEFAULT_MAX_DIAGNOSTICS = 20000
_DEFAULT_MAX_LINE = 65536

_THREAD_JOIN_TIMEOUT = 3.0
_CLEANUP_WAIT_TIMEOUT = 5.0
_PROCESS_TERMINATE_WAIT = 1.0
_PROCESS_KILL_WAIT = 1.0
_STOP_REQUEST_LOCK_TIMEOUT = 0.5

_QUEUE_CAPACITY = 2

_MAX_SCRIPT_PATH_UTF8 = 4096
_MAX_ARGV_ENTRY_UTF8 = 1024
_BINARY_OPEN_FLAG = getattr(os, "O_BINARY", 0)
_MAX_TARGET_SOURCE_BYTES = 16 * 1024 * 1024

_TRUNCATION_MARKER = "\n... [diagnostics truncated] ...\n"

_PING_REQUIRED_FIELDS = frozenset({"status", "pdb_created"})
_PING_KNOWN_FIELDS = frozenset({"status", "pdb_created"})
_SHUTDOWN_REQUIRED_FIELDS = frozenset({"shutdown"})
_SHUTDOWN_KNOWN_FIELDS = frozenset({"shutdown"})

_MAX_RESULT_FUNCTION_UTF8 = 4096
_MAX_RESULT_ERROR_UTF8 = 4096
_MAX_INSPECTION_NAME_UTF8 = 512
_MAX_TYPE_NAME_UTF8 = 512
_MAX_STACK_FRAMES = 64
_MAX_LOCAL_NAMES = 128
_MAX_CONTAINER_ITEMS = 16
_MAX_CONTAINER_DEPTH = 2
_MAX_STRING_PREVIEW_UTF8 = 2048
_MAX_BYTES_PREVIEW = 1024
_MAX_SERIALIZED_INT_BITS = 4096
_MAX_LOCALS_RESULT_BYTES = 32768
_MAX_SAFE_EVAL_RESULT_BYTES = 32768
_MAX_EXPRESSION_UTF8 = 1024

_PAUSED_RESULT_FIELDS = frozenset({"state", "script", "line", "function"})
_EXITED_RESULT_FIELDS = frozenset({"state", "script", "exit_code"})
_TERMINATED_RESULT_FIELDS = frozenset({"state", "script"})
_STATUS_IDLE_FIELDS = frozenset({"state"})
_STATUS_PAUSED_FIELDS = frozenset({"state", "script", "line", "function"})
_STATUS_EXITED_FIELDS = frozenset({"state", "script", "exit_code"})
_STATUS_FAILED_FIELDS = frozenset({"state", "script", "error"})
_STATUS_TERMINATED_FIELDS = frozenset({"state", "script"})
_STACK_RESULT_FIELDS = frozenset({
    "state", "script", "pause_generation", "frames", "total_frames",
    "truncated",
})
_FRAME_SUMMARY_FIELDS = frozenset({
    "frame_id", "script", "line", "function", "is_current",
})
_FRAME_RESULT_FIELDS = frozenset({"state", "pause_generation", "frame"})
_FRAME_DETAIL_FIELDS = frozenset({
    "frame_id", "script", "line", "function", "is_current",
    "argument_names", "local_names", "locals_count", "locals_truncated",
})
_LOCALS_RESULT_FIELDS = frozenset({
    "state", "pause_generation", "frame_id", "locals", "total_count",
    "truncated",
})
_SAFE_EVAL_RESULT_FIELDS = frozenset({
    "state", "pause_generation", "frame", "expression", "value",
})
_LOCAL_ENTRY_FIELDS = frozenset({"name", "value"})
_VALUE_SUMMARY_FIELDS = frozenset({
    "kind", "type", "value", "special", "size", "items", "entries",
    "truncated",
})
_DICT_ENTRY_FIELDS = frozenset({"key", "value"})
_VALUE_KINDS = frozenset({
    "none", "bool", "int", "float", "str", "bytes", "list", "tuple",
    "dict", "set", "frozenset", "object",
})
_CANONICAL_VALUE_TYPES = MappingProxyType({
    "none": "builtins.NoneType",
    "bool": "builtins.bool",
    "int": "builtins.int",
    "float": "builtins.float",
    "str": "builtins.str",
    "bytes": "builtins.bytes",
    "list": "builtins.list",
    "tuple": "builtins.tuple",
    "dict": "builtins.dict",
    "set": "builtins.set",
    "frozenset": "builtins.frozenset",
})

# Public target states the worker may legitimately return.
_PUBLIC_TARGET_STATES = frozenset({
    "idle", "paused", "exited", "failed", "terminated",
})

# Private transient values "starting" and "continuing" are used only while
# their execution-control call owns _request_lock.  They are never valid
# worker results or stable local lifecycle states.
# Local "unknown" is set only when a correlated worker response
# is operationally failed and the true lifecycle is not known.


def _has_raw_dotdot(script: str) -> bool:
    parts = script.replace('\\', '/').split('/')
    return '..' in parts
