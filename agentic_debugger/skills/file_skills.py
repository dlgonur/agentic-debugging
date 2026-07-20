from __future__ import annotations

import os
import tokenize
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agentic_debugger.runtime.exceptions import (
    SourceDecodeError,
    SourceInspectionError,
)
from agentic_debugger.runtime.workspace import TaskWorkspace

MAX_FILE_SIZE_BYTES = 1 * 1024 * 1024
MAX_OPEN_FILE_LINES = 500
DEFAULT_OPEN_FILE_LINES = 200
DEFAULT_SOURCE_WINDOW_RADIUS = 50
MAX_SOURCE_WINDOW_RADIUS = 100


@dataclass(frozen=True)
class SourceLine:
    path: str
    line_number: int
    text: str
    is_focal: bool

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "line_number": self.line_number,
            "text": self.text,
            "is_focal": self.is_focal,
        }


@dataclass(frozen=True)
class SourceWindow:
    path: str
    start_line: int
    end_line: int
    focal_line: Optional[int]
    lines: List[SourceLine]
    total_lines: int
    clipped_before: bool
    clipped_after: bool

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "focal_line": self.focal_line,
            "lines": [l.to_mapping() for l in self.lines],
            "total_lines": self.total_lines,
            "clipped_before": self.clipped_before,
            "clipped_after": self.clipped_after,
        }


def _normalize_source_path(
    workspace: TaskWorkspace, path: str
) -> str:
    root_norm = os.path.normpath(workspace.root)
    resolved = workspace.resolve_path(path, must_exist=True)
    resolved_norm = os.path.normpath(resolved)
    rel = os.path.relpath(resolved_norm, root_norm)
    return rel.replace("\\", "/")


def _read_file_lines(workspace: TaskWorkspace, path: str) -> List[str]:
    resolved = workspace.resolve_path(path, must_exist=True)
    if not os.path.isfile(resolved):
        raise SourceInspectionError(
            f"Path is not a regular file: {path!r}"
        )
    if os.path.islink(resolved):
        raise SourceInspectionError(
            f"Path is a symlink, rejecting: {path!r}"
        )
    size = os.path.getsize(resolved)
    if size > MAX_FILE_SIZE_BYTES:
        raise SourceInspectionError(
            f"File exceeds maximum size of {MAX_FILE_SIZE_BYTES} bytes: {path!r}"
        )
    if size == 0:
        return []
    try:
        with tokenize.open(resolved) as f:
            text = f.read()
    except Exception as e:
        raise SourceDecodeError(
            f"Failed to decode file: {path!r}: {e}"
        ) from e
    if "\0" in text:
        raise SourceInspectionError(
            f"File contains NUL bytes: {path!r}"
        )
    return text.splitlines(True)


def open_file(
    workspace: TaskWorkspace,
    path: str,
    start_line: int = 1,
    max_lines: int = DEFAULT_OPEN_FILE_LINES,
) -> SourceWindow:
    if start_line < 1:
        raise SourceInspectionError(
            f"start_line must be >= 1, got {start_line}"
        )
    if max_lines < 1:
        raise SourceInspectionError(
            f"max_lines must be positive, got {max_lines}"
        )
    if max_lines > MAX_OPEN_FILE_LINES:
        max_lines = MAX_OPEN_FILE_LINES

    npath = _normalize_source_path(workspace, path)
    lines = _read_file_lines(workspace, path)
    total = len(lines)

    if total > 0 and start_line > total:
        raise SourceInspectionError(
            f"start_line {start_line} exceeds file length {total}"
        )

    zero_based_start = start_line - 1
    end_line_exclusive = zero_based_start + max_lines

    clipped_before = start_line > 1
    clipped_after = end_line_exclusive < total and total > 0
    end_line_exclusive = min(end_line_exclusive, total)

    selected = lines[zero_based_start:end_line_exclusive]

    source_lines = [
        SourceLine(
            path=npath,
            line_number=zero_based_start + i + 1,
            text=sel.rstrip("\n\r"),
            is_focal=False,
        )
        for i, sel in enumerate(selected)
    ]

    return SourceWindow(
        path=npath,
        start_line=start_line,
        end_line=zero_based_start + len(source_lines),
        focal_line=None,
        lines=source_lines,
        total_lines=total,
        clipped_before=clipped_before,
        clipped_after=clipped_after,
    )


def get_source_window(
    workspace: TaskWorkspace,
    path: str,
    line: int,
    radius: int = DEFAULT_SOURCE_WINDOW_RADIUS,
) -> SourceWindow:
    if line < 1:
        raise SourceInspectionError(
            f"line must be >= 1, got {line}"
        )
    if radius < 0:
        raise SourceInspectionError(
            f"radius must be >= 0, got {radius}"
        )
    if radius > MAX_SOURCE_WINDOW_RADIUS:
        radius = MAX_SOURCE_WINDOW_RADIUS

    npath = _normalize_source_path(workspace, path)
    lines = _read_file_lines(workspace, path)
    total = len(lines)

    if line > total:
        raise SourceInspectionError(
            f"line {line} exceeds file length {total}"
        )

    win_start = max(1, line - radius)
    win_end = min(total, line + radius)

    clipped_before = win_start > 1
    clipped_after = win_end < total

    selected = lines[win_start - 1 : win_end]

    source_lines = [
        SourceLine(
            path=npath,
            line_number=win_start + i,
            text=sel.rstrip("\n\r"),
            is_focal=(win_start + i == line),
        )
        for i, sel in enumerate(selected)
    ]

    return SourceWindow(
        path=npath,
        start_line=win_start,
        end_line=win_end,
        focal_line=line,
        lines=source_lines,
        total_lines=total,
        clipped_before=clipped_before,
        clipped_after=clipped_after,
    )
