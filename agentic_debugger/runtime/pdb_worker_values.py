"""Bounded value summarization for the PDB worker.

Owns hostile-object-safe type naming, UTF-8 previewing, exact-builtin
value summaries, and protocol-size preflights. Never invokes
user-defined ``__repr__``/``__str__``/iteration on target values.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional, Tuple

from agentic_debugger.runtime.exceptions import PdbProtocolError
from agentic_debugger.runtime.pdb_protocol import PdbResponse, serialize_response
from agentic_debugger.runtime.pdb_worker_limits import (
    _MAX_BYTES_PREVIEW,
    _MAX_CONTAINER_DEPTH,
    _MAX_CONTAINER_ITEMS,
    _MAX_LOCALS_RESULT_BYTES,
    _MAX_SAFE_EVAL_RESULT_BYTES,
    _MAX_SERIALIZED_INT_BITS,
    _MAX_STRING_PREVIEW_UTF8,
    _MAX_TYPE_NAME_UTF8,
    _SAFE_BUILTIN_TYPE_NAMES,
)

_TYPE_MODULE_DESCRIPTOR = type.__dict__['__module__']
_TYPE_QUALNAME_DESCRIPTOR = type.__dict__['__qualname__']


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
