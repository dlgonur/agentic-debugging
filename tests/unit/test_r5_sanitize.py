"""R5.9 sanitizer unit tests: the common deterministic diagnostic sanitizer
must separate REAL DIAGNOSTIC SIGNAL from HIDDEN TEST ANSWER CONTENT.

The sanitizer is the single authority for the two dynamic model-facing
paths: reproduction failure output and verifier-feedback failing records.
Everything it emits must be mechanically derivable from production runtime
state and must never contain hidden test source, assertion expressions,
expected literals, node ids, test filenames, or test function names.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.demo.sanitize import (
    GENERIC_BEHAVIORAL_FAILURE,
    extract_production_exception,
    sanitize_failure_output,
    sanitize_verifier_failure_output,
)

# Real pytest output of the task's own failing reproduction (001).
RAW_001 = """F                                                                        [100%]
================================== FAILURES ===================================
_________________ test_missing_display_name_returns_fallback __________________

    def test_missing_display_name_returns_fallback() -> None:
>       assert format_display_name(None) == "Anonymous"

tests\\test_display_name.py:5: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

name = None

    def format_display_name(name: str | None) -> str:
>       normalized_name = name.strip()
E       AttributeError: 'NoneType' object has no attribute 'strip'

display_name.py:2: AttributeError
=========================== short test summary info ===========================
FAILED tests/test_display_name.py::test_missing_display_name_returns_fallback - AttributeError: 'NoneType' object has no attribute 'strip'
============================== 1 failed in 0.12s ==============================
"""

# Pure assertion failure (002): the failure is defined only by hidden-test
# content — nothing but the generic statement may be forwarded.
RAW_002 = """    def test_full_length_window_includes_every_value() -> None:
>       assert recent_window(values, len(values)) == values
E       assert recent_window(values, len(values)) == values
E        +  where recent_window(values, len(values)) = [10, 20, 30]
E        +    where values = [10, 20, 30, 40]

recent_window.py:11: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_recent_window.py::test_full_length_window_includes_every_value
============================== 1 failed in 0.14s ==============================
"""

# Failing verifier P2P record for a candidate that introduced a NameError in
# production code (001 retry shape): the production exception is real signal;
# the test function name, node id, and hidden input value are not.
RAW_VERIFIER_P2P = """_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

name = 'Ada Lovelace'

    def format_display_name(name: str | None) -> str:
        if name is None:
            return "Anonymous"
>       if not normalized_name:
E       NameError: name 'normalized_name' is not defined

display_name.py:4: NameError
=========================== short test summary info ===========================
FAILED tests/test_display_name.py::test_regular_display_name_is_formatted - NameError: name 'normalized_name' is not defined
============================== 1 failed in 0.15s ==============================
"""


class TestExtractProductionException:
    def test_production_attribute_error_extracted(self):
        exc = extract_production_exception(RAW_001, "display_name.py", 5)
        assert exc is not None
        assert exc.path == "display_name.py"
        assert exc.line == 2
        assert exc.cls == "AttributeError"
        assert exc.message == "'NoneType' object has no attribute 'strip'"
        assert exc.summary() == "display_name.py:2: AttributeError"
        assert exc.full() == (
            "display_name.py:2: AttributeError: "
            "'NoneType' object has no attribute 'strip'"
        )

    def test_assertion_failure_fails_closed(self):
        assert extract_production_exception(RAW_002, "recent_window.py", 20) is None

    def test_line_outside_original_region_fails_closed(self):
        # A production-path summary frame beyond the original source region
        # (the appended launcher) is never a production exception frame.
        assert extract_production_exception(RAW_001, "display_name.py", 1) is None

    def test_wrong_script_fails_closed(self):
        assert extract_production_exception(RAW_001, "other_module.py", 5) is None

    def test_empty_input_fails_closed(self):
        assert extract_production_exception("", "display_name.py", 5) is None
        assert extract_production_exception(None, "display_name.py", 5) is None

    def test_assert_in_exception_message_fails_closed(self):
        raw = """E       AssertionError: assert x == 1

display_name.py:2: AssertionError
"""
        assert extract_production_exception(raw, "display_name.py", 5) is None

    def test_production_frame_without_e_line_fails_closed(self):
        # A summary line alone is not enough: without a safe E-line the
        # structured diagnostic would have no production-originated message.
        raw = """something else

display_name.py:2: AttributeError
"""
        assert extract_production_exception(raw, "display_name.py", 5) is None


class TestSanitizeFailureOutput:
    def test_001_structured_diagnostic_shape(self):
        diagnostic = sanitize_failure_output(RAW_001, None, "display_name.py", 5)
        assert diagnostic.production_exception is not None
        assert diagnostic.text == (
            "baseline failure reproduced\n"
            "production exception:\n"
            "  display_name.py:2: AttributeError\n"
            "  AttributeError: 'NoneType' object has no attribute 'strip'\n"
        )

    def test_no_hidden_test_content_in_001_diagnostic(self):
        diagnostic = sanitize_failure_output(RAW_001, None, "display_name.py", 5)
        for needle in (
            "test_missing_display_name_returns_fallback",
            "def test_",
            "assert format_display_name(None)",
            '"Anonymous"',
            "tests\\test_display_name.py",
            "short test summary",
            "FAILED ",
        ):
            assert needle not in diagnostic.text, needle

    def test_002_pure_assertion_fails_closed_to_generic(self):
        diagnostic = sanitize_failure_output(RAW_002, None, "recent_window.py", 20)
        assert diagnostic.production_exception is None
        assert diagnostic.text == GENERIC_BEHAVIORAL_FAILURE

    def test_002_generic_statement_never_leaks(self):
        diagnostic = sanitize_failure_output(RAW_002, None, "recent_window.py", 20)
        for needle in (
            "recent_window(values, len(values))",
            "AssertionError",
            "test_full_length_window_includes_every_value",
            "[10, 20, 30]",
        ):
            assert needle not in diagnostic.text, needle

    def test_empty_output_yields_generic(self):
        diagnostic = sanitize_failure_output("", None, "display_name.py", 5)
        assert diagnostic.text == GENERIC_BEHAVIORAL_FAILURE

    def test_diagnostic_sha_recorded(self):
        diagnostic = sanitize_failure_output(RAW_001, None, "display_name.py", 5)
        assert len(diagnostic.raw_normalized_sha256) == 64


class TestSanitizeVerifierFailureOutput:
    def test_candidate_production_exception_kept(self):
        result = sanitize_verifier_failure_output(
            RAW_VERIFIER_P2P, "display_name.py", 5
        )
        assert result == (
            "display_name.py:4: NameError: "
            "name 'normalized_name' is not defined"
        )

    def test_hidden_test_content_never_kept(self):
        result = sanitize_verifier_failure_output(
            RAW_VERIFIER_P2P, "display_name.py", 5
        )
        assert result is not None
        for needle in (
            "test_regular_display_name_is_formatted",
            "tests/test_display_name.py::",
            "Ada Lovelace",
            "def format_display_name(name: str | None) -> str:",
            '"Anonymous"',
        ):
            assert needle not in result, needle

    def test_assertion_failure_record_fails_closed(self):
        assert sanitize_verifier_failure_output(RAW_002, "recent_window.py", 20) is None

    def test_empty_record_fails_closed(self):
        assert sanitize_verifier_failure_output("", "display_name.py", 5) is None
