"""Bounded-value validation primitives and freezing for session events.

This module owns the fail-closed, credential-safe scalar/structure
validators and the deep-freeze/thaw machinery used by the application
event schema.  Every bounded payload field in
:mod:`agentic_debugger.application.event_payloads` and every
:class:`~agentic_debugger.application.events.SessionEvent` identity field
is validated through exactly one helper defined here.

Dependency rule: imports only the vocabulary/bound constants from
:mod:`agentic_debugger.application.event_contracts` and the shared JSON
schema primitive from :mod:`agentic_debugger.events.schema`.  It never
imports controller, verifier, PDB, patch, demo, live-model, or experiment
code.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Optional

from agentic_debugger import SchemaValidationError
from agentic_debugger.application.event_contracts import (
    MAX_IDENTIFIER_CHARS,
    MAX_SHORT_TEXT_CHARS,
    MAX_TEXT_CHARS,
    MAX_TUPLE_TEXT_CHARS,
    contains_credential_shape,
)
from agentic_debugger.events.schema import validate_json_compatible


def _check_required(mapping: Mapping[str, Any], required: set, label: str) -> None:
    missing = required - set(mapping.keys())
    if missing:
        raise SchemaValidationError(
            f"Missing required fields in {label}: {sorted(missing)}"
        )


def _check_no_unknown(mapping: Mapping[str, Any], known: set, label: str) -> None:
    extra = set(mapping.keys()) - known
    if extra:
        raise SchemaValidationError(
            f"Unknown fields in {label}: {sorted(extra)}"
        )


def _looks_like_secret(value: str) -> bool:
    return contains_credential_shape(value)


def _bounded_text(
    value: Any,
    label: str,
    max_chars: int,
    *,
    allow_empty: bool = False,
    nullable: bool = False,
) -> Optional[str]:
    if value is None and nullable:
        return None
    if type(value) is not str:
        raise SchemaValidationError(f"{label} must be a string or null")
    if not value.strip() and not allow_empty:
        raise SchemaValidationError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise SchemaValidationError(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise SchemaValidationError(
            f"{label} exceeds the {max_chars}-byte bound"
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise SchemaValidationError(f"{label} contains control characters")
    if _looks_like_secret(value):
        raise SchemaValidationError(
            f"{label} contains a credential-shaped value"
        )
    return value


def _bounded_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, label, max_chars)


def _multiline_text(value: Any, label: str, max_chars: int) -> str:
    """Bound UTF-8 text that may contain normal source whitespace.

    Source/patch content legitimately contains newlines and tabs, so this
    validator allows ``\\n``, ``\\t`` and ``\\r`` but rejects every other
    control character (NUL, ``0x7F``, and the rest of ``0x00-0x1F``) and any
    oversized value.  It intentionally does not apply the credential-shape
    regex here: source code may legitimately mention token-like identifiers,
    so safe-data enforcement for source/patch content happens at the producer
    boundary through the shared :func:`contains_credential_shape` policy
    (captured source snapshots and patch bodies that match it are withheld
    before they can enter an event).
    """
    if type(value) is not str:
        raise SchemaValidationError(f"{label} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise SchemaValidationError(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise SchemaValidationError(
            f"{label} exceeds the {max_chars}-byte bound"
        )
    for char in value:
        code = ord(char)
        if code == 0x00 or code == 0x7F or (
            code < 0x20 and char not in "\n\t\r"
        ):
            raise SchemaValidationError(
                f"{label} contains a prohibited control character"
            )
    return value


def _multiline_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    if value is None:
        return None
    return _multiline_text(value, label, max_chars)


def _identifier(value: Any, label: str) -> str:
    if value is None:
        raise SchemaValidationError(f"{label} must be a non-empty string")
    return _bounded_text(value, label, MAX_IDENTIFIER_CHARS)


def _sha256_hex(value: Any, label: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SchemaValidationError(f"{label} must be a 64-character hex digest")
    return value


def _nonneg_int(value: Any, label: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise SchemaValidationError(f"{label} must be a non-negative integer")
    return value


def _int_or_none(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    return _nonneg_int(value, label)


def _bool_or_none(value: Any, label: str) -> Optional[bool]:
    if value is None:
        return None
    if type(value) is not bool:
        raise SchemaValidationError(f"{label} must be a boolean or null")
    return value


def _enum_or_none(value: Any, label: str, enum_type: type) -> Optional[Any]:
    if value is None:
        return None
    return _enum(value, label, enum_type)


def _enum(value: Any, label: str, enum_type: type) -> Any:
    if type(value) is not str or not value:
        raise SchemaValidationError(f"{label} must be a non-empty string")
    try:
        return enum_type(value)
    except ValueError:
        raise SchemaValidationError(
            f"{label} is not a valid {enum_type.__name__}: {value!r}"
        )


def _string_tuple(
    value: Any,
    label: str,
    *,
    max_items: int,
    max_chars: int = MAX_TUPLE_TEXT_CHARS,
) -> tuple[str, ...]:
    if type(value) is not tuple and type(value) is not list:
        raise SchemaValidationError(f"{label} must be a list of strings")
    if len(value) > max_items:
        raise SchemaValidationError(
            f"{label} exceeds the {max_items}-item bound"
        )
    return tuple(
        _bounded_text(item, f"{label}[{index}]", max_chars)  # type: ignore[arg-type]
        for index, item in enumerate(value)
    )


def _frame_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaValidationError(f"{label} must be a mapping")
    required = {"index", "function", "file", "line", "is_current"}
    _check_required(value, required, label)
    _check_no_unknown(value, required, label)
    return {
        "index": _nonneg_int(value["index"], f"{label}.index"),
        "function": _bounded_text(
            value["function"], f"{label}.function", MAX_IDENTIFIER_CHARS
        ),
        "file": _bounded_text(value["file"], f"{label}.file", MAX_SHORT_TEXT_CHARS),
        "line": _nonneg_int(value["line"], f"{label}.line"),
        "is_current": _bool(value["is_current"], f"{label}.is_current"),
    }


def _bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise SchemaValidationError(f"{label} must be a boolean")
    return value


def _local_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaValidationError(f"{label} must be a mapping")
    required = {"name", "summary"}
    _check_required(value, required, label)
    _check_no_unknown(value, required, label)
    return {
        "name": _bounded_text(value["name"], f"{label}.name", MAX_IDENTIFIER_CHARS),
        "summary": _bounded_text(value["summary"], f"{label}.summary", MAX_TEXT_CHARS),
    }


def _detached(payload: Mapping[str, Any]) -> dict[str, Any]:
    validate_json_compatible(payload, "payload")
    try:
        return json.loads(
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError, OverflowError):
        raise SchemaValidationError("payload is not strictly JSON-compatible")


class _FrozenList(tuple):
    """Tuple-backed JSON sequence with no mutating list protocol."""

    def __new__(cls, values: Any = ()) -> "_FrozenList":
        return tuple.__new__(cls, values)


class _FrozenDict(tuple, Mapping[str, Any]):
    """Tuple-backed JSON mapping whose canonical pairs are the tuple itself."""

    def __new__(
        cls,
        values: Any,
    ) -> "_FrozenDict":
        items = (
            tuple(values.items())
            if isinstance(values, Mapping)
            else tuple(values)
        )
        return tuple.__new__(cls, tuple((str(key), value) for key, value in items))

    def __iter__(self):
        return (key for key, _ in tuple.__iter__(self))

    def __len__(self) -> int:
        return tuple.__len__(self)

    def __getitem__(self, key: str) -> Any:
        for item_key, item_value in tuple.__iter__(self):
            if item_key == key:
                return item_value
        raise KeyError(key)


def _freeze(value: Any) -> Any:
    """Deep-freeze JSON-compatible values into tuple-backed structures."""
    if isinstance(value, Mapping):
        return _FrozenDict((str(key), _freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return _FrozenList(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """Deep-convert frozen structures back into plain JSON data."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value
