"""Bounded frame-local access for the PDB worker.

Owns the frame-locals proxy type, exact-operation mapping access, single
name lookup, full bounded enumeration, and the post-mortem bounded local
scan. All scans avoid hashing into attacker-controlled mappings and detect
mutation fail-closed.
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from agentic_debugger.runtime.pdb_worker_limits import (
    _MAX_FRAME_LOCAL_ENTRIES,
    _MAX_NAME_UTF8,
    _POST_MORTEM_LOCALS_SCAN_CEILING,
)
from agentic_debugger.runtime.pdb_worker_values import _safe_utf8_string

def _get_frame_locals_proxy_type() -> type:
    def _capture() -> type:
        return type(sys._getframe().f_locals)
    return _capture()


_FRAME_LOCALS_PROXY_TYPE = _get_frame_locals_proxy_type()


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
    operations as a narrow seam.  The mapping is iterated lazily â€” never
    materialized via ``list(mapping.items())`` â€” the scan budget is checked
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
