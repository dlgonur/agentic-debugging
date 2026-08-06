"""Recursive bounded JSON-value validation.

A small, strict, recursive validator applied to every free-form JSON payload
the comparison and preference schemas accept (generation configuration,
provenance records, verifier evidence, environment/timing/notes).  It
enforces:

* only JSON scalar/list/object types (exact built-ins);
* finite numbers only (nested NaN/Infinity fail here, at schema load — never
  later during identity generation);
* bounded nesting depth;
* bounded mapping/list entry counts;
* bounded string byte lengths;
* bounded total canonical serialized bytes.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agentic_debugger.comparison.schema import canonical_json, ComparisonInvariantError

#: Maximum nesting depth of accepted JSON values.
MAX_JSON_DEPTH = 32
#: Maximum total mapping keys + list items across the value.
MAX_JSON_ENTRIES = 8192
#: Maximum UTF-8 bytes of one string value.
MAX_JSON_STRING_BYTES = 64 * 1024
#: Maximum canonical serialized bytes of the complete value.
MAX_JSON_TOTAL_BYTES = 1 * 1024 * 1024


class JsonBoundsError(ComparisonInvariantError):
    """Raised when a JSON value violates the declared bounds."""


def validate_json_bounds(
    value: Any,
    *,
    max_depth: int = MAX_JSON_DEPTH,
    max_entries: int = MAX_JSON_ENTRIES,
    max_string_bytes: int = MAX_JSON_STRING_BYTES,
    max_total_bytes: int = MAX_JSON_TOTAL_BYTES,
) -> None:
    """Recursively validate one JSON-compatible value (fail-closed)."""

    if type(max_depth) is not int or max_depth < 1:
        raise JsonBoundsError("max_depth must be a positive integer")
    entries = 0
    strings = 0
    nodes = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal entries, strings, nodes
        nodes += 1
        if depth > max_depth:
            raise JsonBoundsError(f"JSON nesting exceeds depth {max_depth}")
        node_type = type(node)
        if node is None or node_type is bool or node_type is int:
            return
        if node_type is float:
            if not math.isfinite(node):
                raise JsonBoundsError("JSON value contains a non-finite number")
            return
        if node_type is str:
            strings += 1
            if len(node.encode("utf-8")) > max_string_bytes:
                raise JsonBoundsError(
                    f"JSON string exceeds {max_string_bytes} bytes"
                )
            return
        if node_type is list:
            entries += len(node)
            if entries > max_entries:
                raise JsonBoundsError(f"JSON entries exceed {max_entries}")
            for item in node:
                visit(item, depth + 1)
            return
        if node_type is dict:
            entries += len(node)
            if entries > max_entries:
                raise JsonBoundsError(f"JSON entries exceed {max_entries}")
            for key, item in node.items():
                if type(key) is not str:
                    raise JsonBoundsError("JSON object keys must be strings")
                visit(item, depth + 1)
            return
        raise JsonBoundsError(
            f"unsupported JSON value type: {node_type.__name__}"
        )

    visit(value, 0)
    try:
        total = len(canonical_json(value).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise JsonBoundsError(f"JSON value cannot be serialized canonically: {exc}") from None
    if total > max_total_bytes:
        raise JsonBoundsError(f"JSON value exceeds {max_total_bytes} serialized bytes")


__all__ = [
    "MAX_JSON_DEPTH",
    "MAX_JSON_ENTRIES",
    "MAX_JSON_STRING_BYTES",
    "MAX_JSON_TOTAL_BYTES",
    "JsonBoundsError",
    "validate_json_bounds",
]
