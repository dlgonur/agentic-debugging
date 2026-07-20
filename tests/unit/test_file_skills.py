from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from agentic_debugger.runtime.exceptions import (
    SourceDecodeError,
    SourceInspectionError,
    WorkspaceError,
)
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.skills.file_skills import (
    MAX_FILE_SIZE_BYTES,
    MAX_OPEN_FILE_LINES,
    MAX_SOURCE_WINDOW_RADIUS,
    SourceWindow,
    get_source_window,
    open_file,
)


@pytest.fixture
def sample_workspace():
    tmp = Path(tempfile.mkdtemp())
    try:
        src = tmp / "source"
        src.mkdir()
        lines = [f"line {i}\n" for i in range(1, 101)]
        (src / "sample.py").write_text("".join(lines))
        (src / "ten_lines.py").write_text(
            "".join(f"line {i}\n" for i in range(1, 11))
        )
        (src / "empty.py").write_text("")
        (src / "onedir").mkdir()
        (src / "onedir" / "nested.py").write_text("x = 1\n")
        yield TaskWorkspace(str(src))
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


class TestOpenFile:
    def test_read_full_within_bounds(self, sample_workspace):
        win = open_file(sample_workspace, "ten_lines.py")
        assert win.start_line == 1
        assert win.end_line == 10
        assert win.total_lines == 10
        assert len(win.lines) == 10
        assert win.clipped_before is False
        assert win.clipped_after is False
        assert win.focal_line is None

    def test_one_based_line_numbers(self, sample_workspace):
        win = open_file(sample_workspace, "sample.py", start_line=5, max_lines=3)
        assert win.start_line == 5
        assert win.end_line == 7
        assert len(win.lines) == 3
        assert win.lines[0].line_number == 5
        assert win.lines[0].text == "line 5"
        assert win.lines[2].line_number == 7
        assert win.lines[2].text == "line 7"

    def test_clipped_after(self, sample_workspace):
        win = open_file(
            sample_workspace, "sample.py", start_line=1, max_lines=10
        )
        assert win.total_lines == 100
        assert win.clipped_after is True
        assert len(win.lines) == 10

    def test_clipped_before_default_false(self, sample_workspace):
        win = open_file(sample_workspace, "sample.py", start_line=1, max_lines=5)
        assert win.clipped_before is False

    def test_clipped_before_with_offset(self, sample_workspace):
        win = open_file(sample_workspace, "sample.py", start_line=3, max_lines=5)
        assert win.clipped_before is True

    def test_start_line_beyond_file(self, sample_workspace):
        with pytest.raises(SourceInspectionError, match="exceeds file length"):
            open_file(sample_workspace, "ten_lines.py", start_line=20)

    def test_invalid_start_line_zero(self, sample_workspace):
        with pytest.raises(SourceInspectionError, match="start_line.*>= 1"):
            open_file(sample_workspace, "ten_lines.py", start_line=0)

    def test_invalid_start_line_negative(self, sample_workspace):
        with pytest.raises(SourceInspectionError, match="start_line.*>= 1"):
            open_file(sample_workspace, "ten_lines.py", start_line=-5)

    def test_max_lines_capped(self, sample_workspace):
        win = open_file(
            sample_workspace,
            "sample.py",
            start_line=1,
            max_lines=MAX_OPEN_FILE_LINES + 100,
        )
        assert len(win.lines) <= MAX_OPEN_FILE_LINES

    def test_missing_file(self, sample_workspace):
        with pytest.raises(WorkspaceError, match="does not exist"):
            open_file(sample_workspace, "nonexistent.py")

    def test_directory_path_rejected(self, sample_workspace):
        with pytest.raises(SourceInspectionError, match="not a regular file"):
            open_file(sample_workspace, "onedir")

    def test_absolute_path_rejected(self, sample_workspace):
        with pytest.raises(WorkspaceError, match="Absolute paths"):
            open_file(sample_workspace, "/etc/passwd")

    def test_traversal_path_rejected(self, sample_workspace):
        with pytest.raises(WorkspaceError, match="Path traversal"):
            open_file(sample_workspace, "../outside")

    def test_empty_file(self, sample_workspace):
        win = open_file(sample_workspace, "empty.py")
        assert win.total_lines == 0
        assert len(win.lines) == 0
        assert win.start_line == 1
        assert win.end_line == 0
        assert win.clipped_before is False
        assert win.clipped_after is False

    def test_nested_file(self, sample_workspace):
        win = open_file(sample_workspace, "onedir/nested.py")
        assert win.total_lines == 1
        assert win.lines[0].text == "x = 1"

    def test_no_absolute_path_leak(self, sample_workspace):
        win = open_file(sample_workspace, "ten_lines.py")
        mapping = win.to_mapping()
        assert mapping["path"] == "ten_lines.py"
        for sl in mapping["lines"]:
            assert sl["path"] == "ten_lines.py"

    def test_normalized_path(self, sample_workspace):
        win = open_file(sample_workspace, "onedir/nested.py")
        assert win.path == "onedir/nested.py"
        for sl in win.lines:
            assert sl.path == "onedir/nested.py"

    def test_path_consistency_across_records(self, sample_workspace):
        win = open_file(sample_workspace, "onedir/nested.py")
        assert win.path == "onedir/nested.py"
        for sl in win.lines:
            assert sl.path == win.path


class TestGetSourceWindow:
    def test_valid_window_with_focal_line(self, sample_workspace):
        win = get_source_window(sample_workspace, "sample.py", line=50, radius=5)
        assert win.focal_line == 50
        assert win.start_line == 45
        assert win.end_line == 55
        assert len(win.lines) == 11
        assert win.clipped_before is True
        assert win.clipped_after is True

        focal = [l for l in win.lines if l.is_focal]
        assert len(focal) == 1
        assert focal[0].line_number == 50

    def test_window_first_line(self, sample_workspace):
        win = get_source_window(sample_workspace, "sample.py", line=1, radius=5)
        assert win.start_line == 1
        assert win.end_line == 6
        assert len(win.lines) == 6
        assert win.clipped_before is False
        assert win.clipped_after is True

    def test_window_last_line(self, sample_workspace):
        win = get_source_window(sample_workspace, "sample.py", line=100, radius=5)
        assert win.start_line == 95
        assert win.end_line == 100
        assert len(win.lines) == 6
        assert win.clipped_before is True
        assert win.clipped_after is False

    def test_window_middle_no_extra_clip(self, sample_workspace):
        win = get_source_window(sample_workspace, "sample.py", line=50, radius=5)
        assert win.start_line == 45
        assert win.end_line == 55

    def test_window_radius_clipping_before(self, sample_workspace):
        win = get_source_window(sample_workspace, "sample.py", line=2, radius=10)
        assert win.start_line == 1
        assert win.end_line == 12
        assert win.clipped_before is False

    def test_window_large_radius_capped(self, sample_workspace):
        win = get_source_window(
            sample_workspace,
            "sample.py",
            line=50,
            radius=MAX_SOURCE_WINDOW_RADIUS + 50,
        )
        assert win.end_line - win.start_line + 1 <= 2 * MAX_SOURCE_WINDOW_RADIUS + 1

    def test_invalid_line_zero(self, sample_workspace):
        with pytest.raises(SourceInspectionError, match="line.*>= 1"):
            get_source_window(sample_workspace, "sample.py", line=0)

    def test_invalid_line_negative(self, sample_workspace):
        with pytest.raises(SourceInspectionError, match="line.*>= 1"):
            get_source_window(sample_workspace, "sample.py", line=-1)

    def test_invalid_radius_negative(self, sample_workspace):
        with pytest.raises(SourceInspectionError, match="radius.*>= 0"):
            get_source_window(sample_workspace, "sample.py", line=10, radius=-1)

    def test_line_beyond_file(self, sample_workspace):
        with pytest.raises(SourceInspectionError, match="exceeds file length"):
            get_source_window(sample_workspace, "ten_lines.py", line=20)

    def test_zero_radius(self, sample_workspace):
        win = get_source_window(sample_workspace, "sample.py", line=50, radius=0)
        assert len(win.lines) == 1
        assert win.lines[0].line_number == 50
        assert win.lines[0].is_focal


class TestBinaryAndEncoding:
    def test_nul_byte_rejected(self, sample_workspace):
        path = os.path.join(sample_workspace.root, "binary.bin")
        with open(path, "wb") as f:
            f.write(b"hello\x00world\n")
        with pytest.raises((SourceInspectionError, SourceDecodeError)):
            open_file(sample_workspace, "binary.bin")

    def test_pep263_encoding(self, sample_workspace):
        path = os.path.join(sample_workspace.root, "encoded.py")
        with open(path, "wb") as f:
            f.write("# -*- coding: latin-1 -*-\n".encode("ascii"))
            f.write("x = 'caf\xe9'\n".encode("latin-1"))
        win = open_file(sample_workspace, "encoded.py")
        assert win.total_lines == 2
        assert win.lines[1].text == "x = 'caf\xe9'"

    def test_utf8_bom(self, sample_workspace):
        path = os.path.join(sample_workspace.root, "bom.py")
        with open(path, "wb") as f:
            f.write("\ufeff".encode("utf-8"))
            f.write("x = 1\n".encode("utf-8"))
        win = open_file(sample_workspace, "bom.py")
        assert win.total_lines == 1

    def test_to_mapping_no_absolute_path(self, sample_workspace):
        win = open_file(sample_workspace, "ten_lines.py")
        m = win.to_mapping()
        assert m["path"] == "ten_lines.py"
        for sl in m["lines"]:
            assert sl["path"] == "ten_lines.py"
