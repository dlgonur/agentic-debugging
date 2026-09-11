"""Request/input validation authority for the PDB session.

Owns target-script, argv, breakpoint and inspection-identifier validation,
workspace script reading (bounded, TOCTOU-safe) and the shared low-level
protocol primitives (bounded strings, strict ints, exact-field sets,
booleans, non-negative ints, name lists).  Result-schema modules import
these primitives; they must not duplicate them.

All functions are pure with respect to session state: callers pass the
workspace root (or other expected values) explicitly.  The session remains
the sole owner of mutable lifecycle/request state and calls these helpers.

Dependency direction: depends on ``pdb_session_limits`` and exceptions.
Inspection/outcome validators depend on this module; it depends on neither.
"""

from __future__ import annotations

import os
import stat
from typing import Any, List, Optional, Sequence, Tuple

from agentic_debugger.runtime.exceptions import (
    PdbProtocolError,
    PdbSessionError,
)
from agentic_debugger.runtime.pdb_session_limits import (
    _BINARY_OPEN_FLAG,
    _MAX_EXPRESSION_UTF8,
    _MAX_INSPECTION_NAME_UTF8,
    _MAX_SCRIPT_PATH_UTF8,
    _MAX_TARGET_SOURCE_BYTES,
    _has_raw_dotdot,
)


def check_utf8_strict(value: str, label: str) -> bytes:
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError as e:
        raise PdbProtocolError(
            f"{label} contains non-UTF-8-representable characters: {e}"
        ) from e
    return encoded


def read_bounded_fd(fd: int) -> bytes:
    buffer = bytearray()
    while True:
        remaining = _MAX_TARGET_SOURCE_BYTES + 1 - len(buffer)
        if remaining <= 0:
            break
        try:
            chunk = os.read(fd, min(64 * 1024, remaining))
        except OSError as e:
            raise PdbProtocolError(
                f"cannot read script: {e}"
            ) from e
        if not chunk:
            break
        buffer.extend(chunk)
    if len(buffer) > _MAX_TARGET_SOURCE_BYTES:
        raise PdbProtocolError(
            "script exceeds maximum source size"
        )
    return bytes(buffer)


def read_validated_workspace_script(
    workspace_root: str, script_normalized: str
) -> bytes:
    abs_path = os.path.normpath(
        os.path.join(workspace_root, script_normalized)
    )

    try:
        fd = os.open(abs_path, os.O_RDONLY | _BINARY_OPEN_FLAG)
    except (FileNotFoundError, IsADirectoryError) as e:
        if os.path.isdir(abs_path):
            raise PdbProtocolError(
                f"script is a directory: {script_normalized}"
            ) from e
        raise PdbProtocolError(
            f"script not found: {script_normalized}"
        ) from e
    except OSError as e:
        raise PdbProtocolError(
            f"cannot open script: {e}"
        ) from e

    try:
        try:
            opened_stat = os.fstat(fd)
        except OSError as e:
            raise PdbProtocolError(
                f"cannot stat opened script: {e}"
            ) from e

        if not stat.S_ISREG(opened_stat.st_mode):
            raise PdbProtocolError(
                f"script is not a regular file: {script_normalized}"
            )

        try:
            real_root = os.path.realpath(workspace_root)
            real_path = os.path.realpath(abs_path)
        except (ValueError, OSError) as e:
            raise PdbProtocolError(
                f"cannot resolve script path: {e}"
            ) from e

        try:
            common = os.path.commonpath([real_root, real_path])
        except (ValueError, OSError) as e:
            raise PdbProtocolError(
                f"script path containment check failed: {e}"
            ) from e

        if os.path.normcase(common) != os.path.normcase(real_root):
            raise PdbProtocolError(
                "script escapes workspace via symlink or junction"
            )

        try:
            current_path_stat = os.stat(real_path)
        except OSError as e:
            raise PdbProtocolError(
                f"cannot stat resolved script: {e}"
            ) from e

        if not os.path.samestat(opened_stat, current_path_stat):
            raise PdbProtocolError(
                "script file changed between validation and open"
            )

        source_bytes = read_bounded_fd(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    return source_bytes


def validate_script_and_read(
    workspace_root: str, script: str
) -> Tuple[str, bytes]:
    if not isinstance(script, str) or not script:
        raise PdbProtocolError("script must be a non-empty string")

    if '\0' in script:
        raise PdbProtocolError("script contains NUL byte")

    if not script.endswith('.py'):
        raise PdbProtocolError(
            "script must end with .py"
        )

    check_utf8_strict(script, "script")

    if len(script.encode('utf-8')) > _MAX_SCRIPT_PATH_UTF8:
        raise PdbProtocolError(
            f"script path exceeds {_MAX_SCRIPT_PATH_UTF8} UTF-8 bytes"
        )

    if len(script) >= 2 and script[1] == ':':
        raise PdbProtocolError("script must be a relative path")

    if script.startswith('/') or script.startswith('\\'):
        raise PdbProtocolError("script must be a relative path")

    if _has_raw_dotdot(script):
        raise PdbProtocolError(
            "script must not contain .. traversal"
        )

    normalized = os.path.normpath(script)

    if os.path.isabs(normalized):
        raise PdbProtocolError("script must be a relative path")

    normalized = normalized.replace('\\', '/')

    source_bytes = read_validated_workspace_script(
        workspace_root, normalized
    )

    return (normalized, source_bytes)


def validate_breakpoints(
    breakpoints: Sequence[int], source_bytes: bytes = b""
) -> List[int]:
    if not isinstance(breakpoints, (list, tuple)):
        raise PdbProtocolError("breakpoints must be a list")

    if len(breakpoints) < 1 or len(breakpoints) > 16:
        raise PdbProtocolError(
            "breakpoints must have 1-16 entries"
        )

    bps: List[int] = []
    for bp in breakpoints:
        if isinstance(bp, bool) or not isinstance(bp, int):
            raise PdbProtocolError(
                "breakpoints must contain only integers"
            )
        if bp <= 0:
            raise PdbProtocolError(
                "breakpoints must be positive integers"
            )
        bps.append(bp)

    if len(set(bps)) != len(bps):
        raise PdbProtocolError(
            "breakpoints must not contain duplicates"
        )

    bps.sort()

    if source_bytes:
        line_count = len(source_bytes.splitlines())
        for bp_line in bps:
            if bp_line > line_count:
                raise PdbProtocolError(
                    f"breakpoint line {bp_line} exceeds "
                    f"source length ({line_count})"
                )

    return bps


def validate_argv(argv: Sequence[str]) -> List[str]:
    if not isinstance(argv, (list, tuple)):
        raise PdbProtocolError("argv must be a list")

    if len(argv) > 32:
        raise PdbProtocolError(
            "argv must have at most 32 entries"
        )

    av: List[str] = []
    for a in argv:
        if isinstance(a, bool) or not isinstance(a, str):
            raise PdbProtocolError(
                "argv entries must be strings"
            )
        if '\0' in a:
            raise PdbProtocolError(
                "argv entry contains NUL byte"
            )
        check_utf8_strict(a, "argv entry")
        if len(a.encode('utf-8')) > 1024:
            raise PdbProtocolError(
                f"argv entry exceeds 1024 UTF-8 bytes"
            )
        av.append(a)

    return av


def validate_safe_eval_identifiers(
    frame_id: Any, pause_generation: Any
) -> Tuple[int, int]:
    if type(frame_id) is not int:
        raise PdbSessionError("frame_id must be an integer")
    if frame_id < 0:
        raise PdbSessionError("frame_id must be non-negative")
    if type(pause_generation) is not int:
        raise PdbSessionError("pause_generation must be an integer")
    if pause_generation <= 0:
        raise PdbSessionError("pause_generation must be positive")
    return frame_id, pause_generation


def validate_safe_eval_expression_input(expression: Any) -> str:
    if type(expression) is not str:
        raise PdbSessionError("expression must be a string")
    if not expression:
        raise PdbSessionError("expression must be non-empty")
    if expression != expression.strip():
        raise PdbSessionError(
            "expression must not have surrounding whitespace"
        )
    if any(ord(character) <= 0x1f or ord(character) == 0x7f
           for character in expression):
        raise PdbSessionError(
            "expression contains a prohibited control character"
        )
    try:
        encoded = expression.encode('utf-8')
    except UnicodeEncodeError as e:
        raise PdbSessionError("expression must be valid UTF-8") from e
    if len(encoded) > _MAX_EXPRESSION_UTF8:
        raise PdbSessionError("expression exceeds 1024 UTF-8 bytes")
    return expression


def validate_inspection_identifiers(
    frame_id: Any, pause_generation: Any
) -> Tuple[int, int]:
    if isinstance(frame_id, bool) or not isinstance(frame_id, int):
        raise PdbSessionError("frame_id must be an integer")
    if frame_id < 0:
        raise PdbSessionError("frame_id must be non-negative")
    if (isinstance(pause_generation, bool) or
            not isinstance(pause_generation, int)):
        raise PdbSessionError("pause_generation must be an integer")
    if pause_generation <= 0:
        raise PdbSessionError("pause_generation must be positive")
    return frame_id, pause_generation


def validate_bounded_protocol_string(
    v: Any, label: str, max_utf8: int = _MAX_SCRIPT_PATH_UTF8,
) -> str:
    if not isinstance(v, str):
        raise PdbProtocolError(f"{label} must be a string")
    if not v:
        raise PdbProtocolError(f"{label} must be non-empty")
    if '\0' in v:
        raise PdbProtocolError(f"{label} contains NUL byte")
    try:
        encoded = v.encode('utf-8')
    except UnicodeEncodeError as e:
        raise PdbProtocolError(
            f"{label} contains non-UTF-8-representable characters: {e}"
        ) from e
    if len(encoded) > max_utf8:
        raise PdbProtocolError(
            f"{label} exceeds {max_utf8} UTF-8 bytes"
        )
    return v


def validate_result_script(v: Any, label: str) -> str:
    import posixpath
    script = validate_bounded_protocol_string(
        v, label, _MAX_SCRIPT_PATH_UTF8
    )
    if '\\' in script:
        raise PdbProtocolError(
            f"{label} must use forward slashes, got {script!r}"
        )
    if not script.endswith('.py'):
        raise PdbProtocolError(f"{label} must end with .py")
    if len(script) >= 2 and script[1] == ':':
        raise PdbProtocolError(f"{label} must be a relative path")
    if script.startswith('/'):
        raise PdbProtocolError(f"{label} must be a relative path")
    if _has_raw_dotdot(script):
        raise PdbProtocolError(
            f"{label} must not contain .. traversal"
        )
    normalized = posixpath.normpath(script)
    if normalized != script:
        raise PdbProtocolError(
            f"{label} must already be a normalized forward-slash "
            f"path, got {script!r}"
        )
    if normalized in ("", ".", "..") or normalized.startswith("/"):
        raise PdbProtocolError(
            f"{label} is not a valid relative path, got {script!r}"
        )
    return script


def validate_int_strict_field(v: Any, label: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise PdbProtocolError(
            f"{label} must be an integer, got {type(v).__name__}"
        )
    return v


def check_exact_fields(
    result: dict, required: frozenset, label: str
) -> None:
    extra = set(result.keys()) - required
    if extra:
        raise PdbProtocolError(
            f"Unknown fields in {label}: {sorted(extra)}"
        )
    missing = required - set(result.keys())
    if missing:
        raise PdbProtocolError(
            f"Missing fields in {label}: {sorted(missing)}"
        )


def validate_bool_field(v: Any, label: str) -> bool:
    if not isinstance(v, bool):
        raise PdbProtocolError(f"{label} must be a boolean")
    return v


def validate_inspection_script(
    workspace_root: str, v: Any, label: str
) -> str:
    script = validate_result_script(v, label)
    root = os.path.realpath(os.path.abspath(workspace_root))
    candidate = os.path.realpath(os.path.abspath(os.path.join(
        root, script.replace('/', os.sep)
    )))
    try:
        common = os.path.commonpath((root, candidate))
    except ValueError as e:
        raise PdbProtocolError(
            f"{label} does not resolve inside the active workspace"
        ) from e
    if os.path.normcase(common) != os.path.normcase(root):
        raise PdbProtocolError(
            f"{label} resolves outside the active workspace"
        )
    return script


def validate_nonnegative_int(v: Any, label: str) -> int:
    value = validate_int_strict_field(v, label)
    if value < 0:
        raise PdbProtocolError(f"{label} must be non-negative")
    return value


def validate_name_list(
    value: Any,
    label: str,
    *,
    sorted_required: bool,
    maximum_count: Optional[int] = None,
) -> List[str]:
    if not isinstance(value, list):
        raise PdbProtocolError(f"{label} must be a list")
    if maximum_count is not None and len(value) > maximum_count:
        raise PdbProtocolError(f"{label} contains too many names")
    names: List[str] = []
    for index, name in enumerate(value):
        names.append(validate_bounded_protocol_string(
            name, f"{label}[{index}]", _MAX_INSPECTION_NAME_UTF8
        ))
    if len(set(names)) != len(names):
        raise PdbProtocolError(f"{label} contains duplicate names")
    if sorted_required and names != sorted(names):
        raise PdbProtocolError(f"{label} must be sorted")
    return names
