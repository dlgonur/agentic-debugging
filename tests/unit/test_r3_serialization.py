"""R3.2 — fail-closed metadata-only hunk-count normalizer tests.

Covers: count-only corrections, already-correct byte-identity, multiple hunks,
section suffix preservation, `\\ No newline at end of file` handling,
semantic/body fingerprint invariance, path/start invariance, mutation
detection, malformed fail-closed, and the frozen R3.1 live B regression.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.debugger_interaction_v2_r3.serialization import (
    SerializationNormalizationError,
    normalize_hunk_counts,
    semantic_body_fingerprint,
)

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "r31_model_patch_raw.patch"

# Frozen R3.1 live B (exact bytes from run-r3-1-live-2026-08-11 trajectory)
R31_B_SHA256 = "831b1c2bc347c9812296de5ddb7ebac5f6f414bbd6512561b4cb29066e6e2c76"
# Review-verified metadata-only correction: only -7,7 +7,7 -> -7,6 +7,6
R31_C_HEADER = "@@ -7,6 +7,6 @@ def recent_window(values: list[int], size: int) -> list[int]:"


def _diff(old_count=7, new_count=7, header_suffix=""):
    """Build a diff with explicit count fields and a fixed 6/6 body."""
    body = [
        "     start_index = max(sequence_length - requested_size, 0)",
        "     end_index = sequence_length",
        "     calculated_indexes = list(",
        "-        range(start_index, end_index - (1 if requested_size == sequence_length else 0))",
        "+        range(start_index, end_index)",
        "     )",
        "     return [values[index] for index in calculated_indexes]",
    ]
    return (
        "--- a/recent_window.py\n"
        "+++ b/recent_window.py\n"
        f"@@ -7,{old_count} +7,{new_count} @@{header_suffix}\n"
        + "\n".join(body)
        + "\n"
    )


def _header_of(diff: str) -> str:
    return diff.splitlines()[2]


class TestHunkCountCorrections:
    def test_incorrect_old_count_only(self):
        c, rec = normalize_hunk_counts(_diff(old_count=9, new_count=6))
        assert _header_of(c) == "@@ -7,6 +7,6 @@"
        assert rec.header_fields_changed == 1
        assert rec.fingerprint_equal is True

    def test_incorrect_new_count_only(self):
        c, rec = normalize_hunk_counts(_diff(old_count=6, new_count=8))
        assert _header_of(c) == "@@ -7,6 +7,6 @@"
        assert rec.header_fields_changed == 1

    def test_both_incorrect(self):
        c, rec = normalize_hunk_counts(_diff(old_count=7, new_count=7))
        assert _header_of(c) == "@@ -7,6 +7,6 @@"
        assert rec.header_fields_changed == 2

    def test_already_correct_byte_identical(self):
        b = _diff(old_count=6, new_count=6)
        c, rec = normalize_hunk_counts(b)
        assert c == b
        assert rec.header_fields_changed == 0
        assert rec.changed_headers == ()

    def test_multiple_hunks(self):
        diff = (
            "--- a/x.py\n+++ b/x.py\n"
            "@@ -1,3 +1,4 @@\n a\n-b\n+c\n d\n"
            "@@ -10,2 +10,2 @@\n e\n-f\n+g\n"
        )
        c, rec = normalize_hunk_counts(diff)
        lines = c.splitlines()
        assert lines[2] == "@@ -1,3 +1,3 @@"
        assert lines[7] == "@@ -10,2 +10,2 @@"
        assert rec.header_fields_changed == 1

    def test_hunk_section_suffix_preserved(self):
        suffix = " def foo(a, b):  # custom suffix"
        c, rec = normalize_hunk_counts(_diff(old_count=9, new_count=9, header_suffix=suffix))
        assert _header_of(c) == f"@@ -7,6 +7,6 @@{suffix}"
        assert rec.fingerprint_equal is True

    def test_no_newline_marker_not_counted(self):
        diff = (
            "--- a/x.py\n+++ b/x.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-a\n+b\n"
            "\\ No newline at end of file\n"
        )
        c, rec = normalize_hunk_counts(diff)
        assert rec.header_fields_changed == 0  # counts 1/1 correct
        assert "\\ No newline at end of file" in c

    def test_no_newline_marker_kept_when_counts_fixed(self):
        diff = (
            "--- a/x.py\n+++ b/x.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-a\n+b\n"
            "\\ No newline at end of file\n"
        )
        # body: 1 removed (old) + marker = old_actual 1 -> header corrected to -1,1 +1,1
        c, rec = normalize_hunk_counts(diff)
        assert _header_of(c) == "@@ -1,1 +1,1 @@"
        assert "\\ No newline at end of file" in c


class TestFingerprintInvariance:
    def test_fingerprint_unchanged_by_normalization(self):
        b = _diff(old_count=7, new_count=7)
        c, rec = normalize_hunk_counts(b)
        assert rec.semantic_body_fingerprint_raw == rec.semantic_body_fingerprint_normalized
        assert semantic_body_fingerprint(b) == semantic_body_fingerprint(c)

    def test_start_positions_unchanged(self):
        b = _diff(old_count=7, new_count=7)
        c, rec = normalize_hunk_counts(b)
        assert _header_of(c).startswith("@@ -7,6 +7,6 @@")

    def test_paths_unchanged(self):
        b = _diff(old_count=7, new_count=7)
        c, rec = normalize_hunk_counts(b)
        assert c.splitlines()[0] == "--- a/recent_window.py"
        assert c.splitlines()[1] == "+++ b/recent_window.py"

    def test_body_mutation_changes_fingerprint(self):
        b = _diff(old_count=6, new_count=6)
        mutated = b.replace("end_index - (1 if requested_size == sequence_length else 0)", "end_index - 99")
        assert semantic_body_fingerprint(b) != semantic_body_fingerprint(mutated)

    def test_path_mutation_changes_fingerprint(self):
        b = _diff(old_count=6, new_count=6)
        mutated = b.replace("a/recent_window.py", "a/other.py")
        assert semantic_body_fingerprint(b) != semantic_body_fingerprint(mutated)

    def test_start_mutation_changes_fingerprint(self):
        b = _diff(old_count=6, new_count=6)
        mutated = b.replace("@@ -7,6 +7,6 @@", "@@ -8,6 +8,6 @@")
        assert semantic_body_fingerprint(b) != semantic_body_fingerprint(mutated)


class TestFailClosed:
    def test_malformed_body_line_fails_closed(self):
        with pytest.raises(SerializationNormalizationError):
            normalize_hunk_counts("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\njunk\n")

    def test_empty_hunk_fails_closed(self):
        with pytest.raises(SerializationNormalizationError):
            normalize_hunk_counts("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n")

    def test_no_file_header_fails_closed(self):
        with pytest.raises(SerializationNormalizationError):
            normalize_hunk_counts("@@ -1,2 +1,2 @@\n a\n")

    def test_no_hunks_fails_closed(self):
        with pytest.raises(SerializationNormalizationError):
            normalize_hunk_counts("--- a/x.py\n+++ b/x.py\n")

    def test_garbage_fails_closed(self):
        with pytest.raises(SerializationNormalizationError):
            normalize_hunk_counts("not a diff at all")


class TestFrozenR31Regression:
    def test_frozen_r31_b_normalizes_to_c(self):
        b = FIXTURE.read_text(encoding="utf-8")
        assert hashlib.sha256(b.encode("utf-8")).hexdigest() == R31_B_SHA256
        c, rec = normalize_hunk_counts(b)
        assert _header_of(c) == R31_C_HEADER
        assert rec.header_fields_changed == 2
        assert rec.fingerprint_equal is True
        # Body line count unchanged: exactly one removed + one added line (splitlines-based)
        body = [ln for ln in c.splitlines()[3:] if ln.startswith(("-", "+", " "))]
        assert sum(1 for ln in body if ln.startswith("-")) == 1
        assert sum(1 for ln in body if ln.startswith("+")) == 1
        assert sum(1 for ln in body if not ln.startswith(("+", "-"))) == 5
