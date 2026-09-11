"""Post-mortem traceback/exception/local evidence for the PDB worker.

Owns deterministic, bounded, JSON-compatible evidence construction from a
captured exception. Uses only exact built-in operations and descriptor reads
so no target-defined presentation code ever runs.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from agentic_debugger.runtime.pdb_protocol import PROTOCOL_VERSION, PdbResponse
from agentic_debugger.runtime.pdb_worker_frames import (
    _collect_bounded_locals,
)
from agentic_debugger.runtime.pdb_worker_limits import (
    _MAX_BYTES_PREVIEW,
    _MAX_SERIALIZED_INT_BITS,
    _POST_MORTEM_EXC_ARGS_MAX_SCAN,
    _POST_MORTEM_MAX_EXC_MESSAGE_UTF8,
    _POST_MORTEM_MAX_FILE_UTF8,
    _POST_MORTEM_MAX_FRAMES,
    _POST_MORTEM_MAX_FUNCTION_UTF8,
    _POST_MORTEM_MAX_LOCALS,
    _POST_MORTEM_MAX_SCRIPT_UTF8,
    _POST_MORTEM_MAX_TB_SCAN,
    _POST_MORTEM_MAX_TEXT_UTF8,
    _POST_MORTEM_MAX_TYPE_NAME_UTF8,
    _POST_MORTEM_TRUNCATION_MARKER,
    _POST_MORTEM_TRUNCATION_MARKER_UTF8,
)
from agentic_debugger.runtime.pdb_worker_values import (
    _safe_local_summary,
    _safe_type_name,
    _utf8_preview,
)


def _post_mortem_bounded_text(value: Any, maximum_utf8: int) -> str:
    """Return a UTF-8-byte-bounded, sanitized copy of a text field.

    The complete returned byte sequence â€” including any truncation marker â€”
    never exceeds ``maximum_utf8`` UTF-8 bytes.  Values that already fit are
    returned unchanged.  Only exact built-in types (``str``, ``int``,
    ``float``, ``bool``) are converted with their exact built-in ``str()``
    representation; any other type is opaque and yields ``""`` â€” no user
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
    omission/truncation marker ``'â€¦'`` are included inside the same
    ``_POST_MORTEM_MAX_EXC_MESSAGE_UTF8`` budget), no list of every rendered
    argument and no full-length joined message is ever built, huge exact
    ``str`` values are only previewed, huge exact ``bytes`` values are only
    decoded from a bounded prefix, and huge exact ``int`` values are never
    decimalized.  The result is deterministic and JSON-serializable.

    Marker reservation: whenever any argument or argument tail is not
    represented and the budget can hold the marker, the final message ends
    with exactly one marker, reserved inside the budget.  Marker decisions
    use the explicit ``truncated`` metadata returned by
    :func:`_render_single_exception_arg` â€” never the rendered text suffix â€”
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
    accepted :func:`_summarize_value` / :func:`_safe_type_name` machinery â€”
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


def _bounded_local_repr_pure(value: Any) -> str:
    """Return a bounded, side-effect-safe textual summary of a local value.

    Deprecated in favor of :func:`_safe_local_summary`; retained only for
    backward compatibility with earlier tests.  Does NOT invoke
    ``repr(value)`` â€” uses :func:`_safe_local_summary` and renders the
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
