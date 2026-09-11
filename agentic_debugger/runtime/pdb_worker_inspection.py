"""Paused-target inspection for the PDB worker.

Owns workspace stack derivation, frame/locals detail building, and
safe-expression evaluation responses as pure helpers. All helpers assume
the caller holds the worker's lifecycle condition while live target frames
are touched; they never acquire locks, never send wire responses, and never
mutate the lifecycle dict — invariant failures are returned as strings so
the worker facade (the single lifecycle authority) can fail the target
closed exactly once.
"""
from __future__ import annotations

import ast
import os
import posixpath
import types
from typing import Any, Dict, List, Optional, Tuple

from agentic_debugger.runtime.exceptions import PdbProtocolError
from agentic_debugger.runtime.pdb_protocol import PROTOCOL_VERSION, PdbResponse
from agentic_debugger.runtime.pdb_worker_frames import (
    _FRAME_LOCALS_PROXY_TYPE,
    _frame_locals_entries,
)
from agentic_debugger.runtime.pdb_worker_limits import (
    _MAX_FUNCTION_UTF8,
    _MAX_LOCAL_NAMES,
    _MAX_LOCALS_RESULT_BYTES,
    _MAX_NAME_UTF8,
    _MAX_SAFE_EVAL_RESULT_BYTES,
    _MAX_SCRIPT_PATH_UTF8,
    _MAX_STACK_FRAMES,
)
from agentic_debugger.runtime.pdb_worker_paths import _has_raw_dotdot
from agentic_debugger.runtime.pdb_worker_safeeval import (
    _SafeEvaluationError,
    _SafeExpressionInterpreter,
    _parse_safe_expression,
    _validate_expression_envelope,
)
from agentic_debugger.runtime.pdb_worker_values import (
    _compact_json_size,
    _safe_utf8_string,
    _successful_response_fits,
    _summarize_value,
)


def canonical_workspace_frame_script(
    frame: types.FrameType, workspace_root_real: str
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
        candidate = os.path.join(workspace_root_real, filename)
    try:
        resolved = os.path.realpath(os.path.abspath(candidate))
        common = os.path.commonpath(
            (workspace_root_real, resolved)
        )
    except (OSError, ValueError):
        return None
    if os.path.normcase(common) != os.path.normcase(
            workspace_root_real):
        return None
    try:
        relative = os.path.relpath(resolved, workspace_root_real)
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


def frame_metadata(
    frame: types.FrameType,
    frame_id: int,
    workspace_root_real: str,
) -> Optional[Dict[str, Any]]:
    script = canonical_workspace_frame_script(frame, workspace_root_real)
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


def derive_workspace_stack(
    current: types.FrameType, workspace_root_real: str
) -> Tuple[Optional[List[Tuple[types.FrameType, Dict[str, Any]]]], int]:
    frames: List[Tuple[types.FrameType, Dict[str, Any]]] = []
    total = 0
    cursor: Optional[types.FrameType] = current
    first = True
    while cursor is not None:
        metadata = frame_metadata(cursor, total, workspace_root_real)
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


def validate_frame_inspection_payload(
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


def inspection_snapshot(
    lifecycle: Dict[str, Any],
    target_thread: Any,
    workspace_root_real: str,
) -> Tuple[
    Optional[List[Tuple[types.FrameType, Dict[str, Any]]]],
    int,
    int,
    str,
    Optional[str],
    Optional[str],
]:
    state = lifecycle.get('state')
    if state != 'paused':
        return None, 0, 0, '', (
            f"Cannot inspect target in state: {state}"
        ), None
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
    current = lifecycle.get('_paused_frame')
    if not isinstance(current, types.FrameType):
        return None, 0, 0, '', None, (
            "Paused target invariant failure during inspection: "
            "paused frame is missing"
        )
    generation = lifecycle.get('pause_generation')
    if (isinstance(generation, bool) or
            not isinstance(generation, int) or generation <= 0):
        return None, 0, 0, '', None, (
            "Paused target invariant failure during inspection: "
            "pause generation is invalid"
        )
    script = lifecycle.get('script')
    if not isinstance(script, str):
        return None, 0, 0, '', None, (
            "Paused target invariant failure during inspection: "
            "active script is invalid"
        )
    frames, total = derive_workspace_stack(current, workspace_root_real)
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


def stack_summary_response(
    request_id: int,
    frames: List[Tuple[types.FrameType, Dict[str, Any]]],
    total: int,
    generation: int,
    script: str,
) -> Tuple[Optional[PdbResponse], Optional[str]]:
    result: Optional[Dict[str, Any]] = None
    failure: Optional[str] = None
    response: Optional[PdbResponse] = None
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
            request_id=request_id,
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
    if response is not None:
        return response, None
    return None, failure or "Inspection failed closed"


def namespace_local_mapping(
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


def frame_detail_result(
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


def frame_response(
    request_id: int,
    frames: List[Tuple[types.FrameType, Dict[str, Any]]],
    generation: int,
    frame_id: int,
) -> Tuple[Optional[PdbResponse], Optional[str]]:
    result: Optional[Dict[str, Any]] = None
    failure: Optional[str] = None
    response: Optional[PdbResponse] = None
    if frame_id >= len(frames):
        return None, "Unknown frame_id for current pause"
    frame, metadata = frames[frame_id]
    local_mapping, failure = namespace_local_mapping(frame)
    if local_mapping is not None:
        result, failure = frame_detail_result(
            frame, metadata, generation, local_mapping
        )
    if result is not None:
        while True:
            candidate = PdbResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request_id,
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
    frame = None  # type: ignore[assignment]
    local_mapping = None
    if response is not None:
        return response, None
    return None, failure or "Inspection failed closed"


def frame_locals_result(
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


def frame_locals_response(
    request_id: int,
    frames: List[Tuple[types.FrameType, Dict[str, Any]]],
    generation: int,
    frame_id: int,
) -> Tuple[Optional[PdbResponse], Optional[str]]:
    if frame_id >= len(frames):
        return None, "Unknown frame_id for current pause"
    frame = frames[frame_id][0]
    local_mapping, failure = namespace_local_mapping(frame)
    result: Optional[Dict[str, Any]] = None
    response: Optional[PdbResponse] = None
    if local_mapping is not None:
        result, failure = frame_locals_result(
            frame_id, generation, local_mapping
        )
    if result is not None:
        candidate = PdbResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id=request_id,
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
    frame = None  # type: ignore[assignment]
    local_mapping = None
    if response is not None:
        return response, None
    return None, failure or "Inspection failed closed"


def validate_safe_eval_payload(
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


def safe_eval_response(
    request_id: int,
    frames: List[Tuple[types.FrameType, Dict[str, Any]]],
    generation: int,
    frame_id: int,
    expression: str,
) -> Tuple[Optional[PdbResponse], Optional[str]]:
    failure: Optional[str] = None
    response: Optional[PdbResponse] = None
    parsed: Optional[ast.Expression] = None
    interpreter: Optional[_SafeExpressionInterpreter] = None
    evaluated_value: Any = None
    local_mapping: Any = None
    frame: Optional[types.FrameType] = None
    try:
        if frame_id >= len(frames):
            failure = "Unknown frame_id for current pause"
        else:
            try:
                frame, metadata = frames[frame_id]
                local_mapping, failure = (
                    namespace_local_mapping(frame)
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
                        request_id=request_id,
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
    if response is not None:
        return response, None
    return None, failure or "Safe evaluation failed closed"
