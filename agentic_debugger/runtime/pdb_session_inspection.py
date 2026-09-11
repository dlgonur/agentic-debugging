"""Inspection result validation authority for the PDB session.

Owns strict schemas for stack summaries, frame details, locals lists,
safe-eval results and the recursive value-summary grammar, plus the
protocol line-limit check for successful inspection responses.

All functions are pure: workspace-dependent script checks take
``workspace_root`` explicitly and request-correlation checks take the
requested identifiers explicitly.  The session (via control helpers)
remains the sole owner of mutable lifecycle state.

Dependency direction: depends on ``pdb_session_limits``,
``pdb_session_validation`` (shared primitives), exceptions and protocol.
Outcome/transport/control modules must not be imported here.
"""

from __future__ import annotations

import json
import math
from typing import Any, List, Optional

from agentic_debugger.runtime.exceptions import (
    PdbProtocolError,
    PdbSessionError,
)
from agentic_debugger.runtime.pdb_protocol import (
    PdbResponse,
    serialize_response,
)
from agentic_debugger.runtime.pdb_session_limits import (
    _CANONICAL_VALUE_TYPES,
    _DICT_ENTRY_FIELDS,
    _FRAME_DETAIL_FIELDS,
    _FRAME_RESULT_FIELDS,
    _FRAME_SUMMARY_FIELDS,
    _LOCAL_ENTRY_FIELDS,
    _LOCALS_RESULT_FIELDS,
    _MAX_BYTES_PREVIEW,
    _MAX_CONTAINER_DEPTH,
    _MAX_CONTAINER_ITEMS,
    _MAX_INSPECTION_NAME_UTF8,
    _MAX_LOCAL_NAMES,
    _MAX_LOCALS_RESULT_BYTES,
    _MAX_RESULT_FUNCTION_UTF8,
    _MAX_SAFE_EVAL_RESULT_BYTES,
    _MAX_SERIALIZED_INT_BITS,
    _MAX_STACK_FRAMES,
    _MAX_STRING_PREVIEW_UTF8,
    _MAX_TYPE_NAME_UTF8,
    _SAFE_EVAL_RESULT_FIELDS,
    _STACK_RESULT_FIELDS,
    _VALUE_KINDS,
    _VALUE_SUMMARY_FIELDS,
)
from agentic_debugger.runtime.pdb_session_validation import (
    check_exact_fields,
    validate_bool_field,
    validate_bounded_protocol_string,
    validate_inspection_script,
    validate_int_strict_field,
    validate_name_list,
    validate_nonnegative_int,
    validate_safe_eval_expression_input,
)


def validate_frame_summary_mapping(
    workspace_root: str,
    frame: Any,
    expected_id: int,
    label: str,
) -> None:
    if not isinstance(frame, dict):
        raise PdbProtocolError(f"{label} must be a mapping")
    check_exact_fields(frame, _FRAME_SUMMARY_FIELDS, label)
    frame_id = validate_int_strict_field(
        frame['frame_id'], f"{label} frame_id"
    )
    if frame_id != expected_id:
        raise PdbProtocolError(
            f"{label} frame_id must be {expected_id}, got {frame_id}"
        )
    validate_inspection_script(
        workspace_root, frame['script'], f"{label} script"
    )
    line = validate_int_strict_field(
        frame['line'], f"{label} line"
    )
    if line <= 0:
        raise PdbProtocolError(f"{label} line must be positive")
    validate_bounded_protocol_string(
        frame['function'], f"{label} function", _MAX_RESULT_FUNCTION_UTF8
    )
    is_current = validate_bool_field(
        frame['is_current'], f"{label} is_current"
    )
    if is_current != (frame_id == 0):
        raise PdbProtocolError(
            f"{label} is_current does not match frame_id"
        )


def validate_stack_summary_result(
    result: Any, active_script: str, workspace_root: str
) -> None:
    if not isinstance(result, dict):
        raise PdbProtocolError(
            "get_stack_summary result must be a mapping"
        )
    check_exact_fields(
        result, _STACK_RESULT_FIELDS, "get_stack_summary result"
    )
    if type(result['state']) is not str or result['state'] != 'paused':
        raise PdbProtocolError(
            "get_stack_summary result state must be 'paused'"
        )
    script = validate_inspection_script(
        workspace_root,
        result['script'], "get_stack_summary result script"
    )
    if script != active_script:
        raise PdbProtocolError(
            f"get_stack_summary script {script!r} does not match "
            f"active {active_script!r}"
        )
    generation = validate_int_strict_field(
        result['pause_generation'],
        "get_stack_summary pause_generation",
    )
    if generation <= 0:
        raise PdbProtocolError(
            "get_stack_summary pause_generation must be positive"
        )
    frames = result['frames']
    if not isinstance(frames, list):
        raise PdbProtocolError("get_stack_summary frames must be a list")
    if not frames:
        raise PdbProtocolError(
            "get_stack_summary must contain current frame zero"
        )
    if len(frames) > _MAX_STACK_FRAMES:
        raise PdbProtocolError(
            "get_stack_summary returned too many frames"
        )
    for expected_id, frame in enumerate(frames):
        validate_frame_summary_mapping(
            workspace_root,
            frame, expected_id,
            f"get_stack_summary frame {expected_id}",
        )
    if frames[0]['script'] != script:
        raise PdbProtocolError(
            "get_stack_summary current frame script does not match target"
        )
    current_count = sum(
        1 for frame in frames if frame['is_current'] is True
    )
    if current_count != 1:
        raise PdbProtocolError(
            "get_stack_summary must contain exactly one current frame"
        )
    total = validate_int_strict_field(
        result['total_frames'], "get_stack_summary total_frames"
    )
    if total < 0 or total < len(frames):
        raise PdbProtocolError(
            "get_stack_summary total_frames is inconsistent"
        )
    truncated = validate_bool_field(
        result['truncated'], "get_stack_summary truncated"
    )
    if truncated != (total > len(frames)):
        raise PdbProtocolError(
            "get_stack_summary count/truncated fields are inconsistent"
        )


def validate_frame_result(
    result: Any,
    requested_frame_id: Optional[int],
    requested_generation: Optional[int],
    active_script: str,
    workspace_root: str,
) -> None:
    if not isinstance(result, dict):
        raise PdbProtocolError("get_frame result must be a mapping")
    check_exact_fields(result, _FRAME_RESULT_FIELDS, "get_frame result")
    if type(result['state']) is not str or result['state'] != 'paused':
        raise PdbProtocolError("get_frame result state must be 'paused'")
    generation = validate_int_strict_field(
        result['pause_generation'], "get_frame pause_generation"
    )
    if generation <= 0 or generation != requested_generation:
        raise PdbProtocolError(
            "get_frame result pause_generation does not match request"
        )
    frame = result['frame']
    if not isinstance(frame, dict):
        raise PdbProtocolError("get_frame frame must be a mapping")
    check_exact_fields(frame, _FRAME_DETAIL_FIELDS, "get_frame frame")
    if requested_frame_id is None:
        raise PdbProtocolError("get_frame request frame_id is unavailable")
    summary = {
        key: frame[key] for key in _FRAME_SUMMARY_FIELDS
    }
    validate_frame_summary_mapping(
        workspace_root, summary, requested_frame_id, "get_frame frame"
    )
    if frame['function'] == '<module>':
        raise PdbProtocolError(
            "get_frame successful result must not expose a module frame"
        )
    if requested_frame_id == 0 and frame['script'] != active_script:
        raise PdbProtocolError(
            "get_frame current frame script does not match active target"
        )
    validate_name_list(
        frame['argument_names'], "get_frame argument_names",
        sorted_required=False,
    )
    local_names = validate_name_list(
        frame['local_names'], "get_frame local_names",
        sorted_required=True, maximum_count=_MAX_LOCAL_NAMES,
    )
    count = validate_int_strict_field(
        frame['locals_count'], "get_frame locals_count"
    )
    if count < 0 or count < len(local_names):
        raise PdbProtocolError("get_frame locals_count is inconsistent")
    truncated = validate_bool_field(
        frame['locals_truncated'], "get_frame locals_truncated"
    )
    if truncated != (count > len(local_names)):
        raise PdbProtocolError(
            "get_frame locals count/truncation is inconsistent"
        )


def validate_value_summary(
    summary: Any,
    label: str,
    depth: int = 0,
) -> None:
    if not isinstance(summary, dict):
        raise PdbProtocolError(f"{label} must be a mapping")
    check_exact_fields(summary, _VALUE_SUMMARY_FIELDS, label)
    kind = summary['kind']
    if type(kind) is not str or kind not in _VALUE_KINDS:
        raise PdbProtocolError(f"{label} has invalid kind")
    type_name = validate_bounded_protocol_string(
        summary['type'], f"{label} type", _MAX_TYPE_NAME_UTF8
    )
    if kind == 'object':
        if type_name != 'unknown':
            module_name, separator, qualname = type_name.partition('.')
            if not separator or not module_name or not qualname:
                raise PdbProtocolError(
                    f"{label} object type must be module.qualname or unknown"
                )
    elif type_name != _CANONICAL_VALUE_TYPES[kind]:
        raise PdbProtocolError(
            f"{label} type does not match kind {kind!r}"
        )
    truncated = validate_bool_field(
        summary['truncated'], f"{label} truncated"
    )
    items = summary['items']
    entries = summary['entries']
    if not isinstance(items, list) or not isinstance(entries, list):
        raise PdbProtocolError(f"{label} items and entries must be lists")

    value = summary['value']
    special = summary['special']
    size = summary['size']
    if kind == 'none':
        if (value is not None or special is not None or size is not None or
                items or entries or truncated):
            raise PdbProtocolError(f"{label} none summary is inconsistent")
        return
    if kind == 'bool':
        if (type(value) is not bool or special is not None or
                size is not None or items or entries or truncated):
            raise PdbProtocolError(f"{label} bool summary is inconsistent")
        return
    if kind == 'int':
        bits = validate_nonnegative_int(size, f"{label} size")
        if special is not None or items or entries:
            raise PdbProtocolError(f"{label} int summary is inconsistent")
        if truncated:
            if value is not None or bits <= _MAX_SERIALIZED_INT_BITS:
                raise PdbProtocolError(
                    f"{label} truncated int is inconsistent"
                )
        elif (type(value) is not int or int.bit_length(value) != bits or
              bits > _MAX_SERIALIZED_INT_BITS):
            raise PdbProtocolError(f"{label} int summary is inconsistent")
        return
    if kind == 'float':
        if size is not None or items or entries or truncated:
            raise PdbProtocolError(f"{label} float summary is inconsistent")
        if special is None:
            if type(value) is not float or not math.isfinite(value):
                raise PdbProtocolError(
                    f"{label} finite float summary is inconsistent"
                )
        elif (special not in ('nan', 'inf', '-inf') or
              value is not None):
            raise PdbProtocolError(
                f"{label} special float summary is inconsistent"
            )
        return
    if kind == 'str':
        count = validate_nonnegative_int(size, f"{label} size")
        if type(value) is not str or special is not None or items or entries:
            raise PdbProtocolError(f"{label} str summary is inconsistent")
        try:
            encoded = value.encode('utf-8')
        except UnicodeEncodeError as e:
            raise PdbProtocolError(f"{label} str value is not UTF-8") from e
        if len(encoded) > _MAX_STRING_PREVIEW_UTF8 or count < len(value):
            raise PdbProtocolError(f"{label} str summary exceeds bounds")
        if truncated != (count > len(value)):
            raise PdbProtocolError(
                f"{label} str truncation is inconsistent"
            )
        return
    if kind == 'bytes':
        count = validate_nonnegative_int(size, f"{label} size")
        if (type(value) is not str or special is not None or items or entries or
                len(value) % 2 or len(value) > _MAX_BYTES_PREVIEW * 2 or
                any(c not in '0123456789abcdef' for c in value)):
            raise PdbProtocolError(f"{label} bytes summary is inconsistent")
        preview_size = len(value) // 2
        if count < preview_size or truncated != (count > preview_size):
            raise PdbProtocolError(
                f"{label} bytes truncation is inconsistent"
            )
        return

    if kind == 'object':
        if (value is not None or special is not None or size is not None or
                items or entries or truncated):
            raise PdbProtocolError(f"{label} object summary is inconsistent")
        return

    count = validate_nonnegative_int(size, f"{label} size")
    if value is not None or special is not None:
        raise PdbProtocolError(f"{label} container scalar fields are invalid")
    if kind in ('set', 'frozenset'):
        if items or entries or truncated != (count > 0):
            raise PdbProtocolError(f"{label} set summary is inconsistent")
        return
    if depth >= _MAX_CONTAINER_DEPTH and (items or entries):
        raise PdbProtocolError(f"{label} exceeds maximum recursion depth")
    if kind in ('list', 'tuple'):
        if entries or len(items) > _MAX_CONTAINER_ITEMS or count < len(items):
            raise PdbProtocolError(f"{label} sequence summary is inconsistent")
        if not truncated and count != len(items):
            raise PdbProtocolError(f"{label} sequence omission is unmarked")
        for index, item in enumerate(items):
            validate_value_summary(
                item, f"{label} items[{index}]", depth + 1
            )
        return
    if items or len(entries) > _MAX_CONTAINER_ITEMS or count < len(entries):
        raise PdbProtocolError(f"{label} dict summary is inconsistent")
    if not truncated and count != len(entries):
        raise PdbProtocolError(f"{label} dict omission is unmarked")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise PdbProtocolError(
                f"{label} entries[{index}] must be a mapping"
            )
        check_exact_fields(
            entry, _DICT_ENTRY_FIELDS, f"{label} entries[{index}]"
        )
        validate_value_summary(
            entry['key'], f"{label} entries[{index}] key", depth + 1
        )
        validate_value_summary(
            entry['value'], f"{label} entries[{index}] value", depth + 1
        )


def validate_locals_result(
    result: Any,
    requested_frame_id: Optional[int],
    requested_generation: Optional[int],
) -> None:
    if not isinstance(result, dict):
        raise PdbProtocolError("get_frame_locals result must be a mapping")
    check_exact_fields(
        result, _LOCALS_RESULT_FIELDS, "get_frame_locals result"
    )
    if type(result['state']) is not str or result['state'] != 'paused':
        raise PdbProtocolError(
            "get_frame_locals result state must be 'paused'"
        )
    generation = validate_int_strict_field(
        result['pause_generation'],
        "get_frame_locals pause_generation",
    )
    if generation <= 0 or generation != requested_generation:
        raise PdbProtocolError(
            "get_frame_locals pause_generation does not match request"
        )
    frame_id = validate_int_strict_field(
        result['frame_id'], "get_frame_locals frame_id"
    )
    if frame_id < 0 or frame_id != requested_frame_id:
        raise PdbProtocolError(
            "get_frame_locals frame_id does not match request"
        )
    locals_value = result['locals']
    if not isinstance(locals_value, list):
        raise PdbProtocolError("get_frame_locals locals must be a list")
    if len(locals_value) > _MAX_LOCAL_NAMES:
        raise PdbProtocolError("get_frame_locals returned too many locals")
    names: List[str] = []
    for index, entry in enumerate(locals_value):
        if not isinstance(entry, dict):
            raise PdbProtocolError(
                f"get_frame_locals locals[{index}] must be a mapping"
            )
        check_exact_fields(
            entry, _LOCAL_ENTRY_FIELDS,
            f"get_frame_locals locals[{index}]",
        )
        names.append(validate_bounded_protocol_string(
            entry['name'], f"get_frame_locals locals[{index}] name",
            _MAX_INSPECTION_NAME_UTF8,
        ))
        validate_value_summary(
            entry['value'], f"get_frame_locals locals[{index}] value"
        )
    if names != sorted(names) or len(set(names)) != len(names):
        raise PdbProtocolError(
            "get_frame_locals names must be unique and sorted"
        )
    total = validate_nonnegative_int(
        result['total_count'], "get_frame_locals total_count"
    )
    if total < len(locals_value):
        raise PdbProtocolError(
            "get_frame_locals total_count is inconsistent"
        )
    truncated = validate_bool_field(
        result['truncated'], "get_frame_locals truncated"
    )
    if truncated != (total > len(locals_value)):
        raise PdbProtocolError(
            "get_frame_locals count/truncation is inconsistent"
        )
    try:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError, UnicodeEncodeError) as e:
        raise PdbProtocolError(
            "get_frame_locals result is not compact valid JSON"
        ) from e
    if len(encoded) > _MAX_LOCALS_RESULT_BYTES:
        raise PdbProtocolError(
            "get_frame_locals result exceeds 32768-byte budget"
        )


def validate_safe_eval_result(
    result: Any,
    requested_frame_id: Optional[int],
    requested_generation: Optional[int],
    requested_expression: Any,
    active_script: str,
    workspace_root: str,
) -> None:
    if not isinstance(result, dict):
        raise PdbProtocolError(
            "safe_eval_expression result must be a mapping"
        )
    check_exact_fields(
        result, _SAFE_EVAL_RESULT_FIELDS,
        "safe_eval_expression result",
    )
    if type(result['state']) is not str or result['state'] != 'paused':
        raise PdbProtocolError(
            "safe_eval_expression result state must be 'paused'"
        )
    if type(result['pause_generation']) is not int:
        raise PdbProtocolError(
            "safe_eval_expression pause_generation must be an integer"
        )
    generation = result['pause_generation']
    if generation <= 0 or generation != requested_generation:
        raise PdbProtocolError(
            "safe_eval_expression pause_generation does not match request"
        )
    if requested_frame_id is None:
        raise PdbProtocolError(
            "safe_eval_expression request frame_id is unavailable"
        )
    frame = result['frame']
    if not isinstance(frame, dict):
        raise PdbProtocolError(
            "safe_eval_expression frame must be a mapping"
        )
    if (type(frame.get('script')) is not str or
            type(frame.get('function')) is not str or
            type(frame.get('frame_id')) is not int or
            type(frame.get('line')) is not int):
        raise PdbProtocolError(
            "safe_eval_expression frame strings must be exact strings"
        )
    validate_frame_summary_mapping(
        workspace_root,
        frame, requested_frame_id, "safe_eval_expression frame"
    )
    if frame['function'] == '<module>':
        raise PdbProtocolError(
            "safe_eval_expression successful result must not expose "
            "a module frame"
        )
    if requested_frame_id == 0 and frame['script'] != active_script:
        raise PdbProtocolError(
            "safe_eval_expression current frame does not match target"
        )
    expression = result['expression']
    if type(expression) is not str or expression != requested_expression:
        raise PdbProtocolError(
            "safe_eval_expression expression does not match request"
        )
    try:
        validate_safe_eval_expression_input(expression)
    except PdbSessionError as e:
        raise PdbProtocolError(
            "safe_eval_expression result expression is invalid"
        ) from e
    validate_value_summary(
        result['value'], "safe_eval_expression value"
    )
    try:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
            allow_nan=False,
        ).encode('utf-8')
    except (
        TypeError, ValueError, UnicodeEncodeError, MemoryError,
        RecursionError,
    ) as e:
        raise PdbProtocolError(
            "safe_eval_expression result is not compact valid JSON"
        ) from e
    if len(encoded) > _MAX_SAFE_EVAL_RESULT_BYTES:
        raise PdbProtocolError(
            "safe_eval_expression result exceeds 32768-byte budget"
        )


def validate_successful_inspection_response_size(
    response: PdbResponse,
    operation: str,
) -> None:
    try:
        serialize_response(response)
    except PdbProtocolError as e:
        raise PdbProtocolError(
            f"{operation} successful response exceeds the protocol "
            "line limit or is not serializable"
        ) from e
