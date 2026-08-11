"""R4 unit tests — deterministic framing, first-response-only, SHA identity.

Amendment 11: fence extraction/framing; first-response-only contract; test
SHA identity (T_raw / T_parsed / T_written).
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.debugger_interaction_v2_r3.transport import FakeTransport
from experiments.model_generated_test_probe_r4 import test_generation as tg


def _task():
    from agentic_debugger.evaluation.runner import load_task
    fixture = (
        REPO_ROOT
        / "agentic_debugger" / "datasets" / "curated" / "curated-off-by-one-002"
    )
    return load_task(str(fixture / "task.json"))


# ---------------------------------------------------------------------------
# Fence extraction / framing
# ---------------------------------------------------------------------------


class TestExtractCodeBlock:
    def test_python_fenced_block(self):
        raw = "```python\nfrom recent_window import recent_window\n\n\ndef test_x():\n    pass\n```"
        body = tg.extract_code_block(raw)
        assert body == "from recent_window import recent_window\n\n\ndef test_x():\n    pass"

    def test_py_fenced_block(self):
        raw = "```py\nprint(1)\n```"
        assert tg.extract_code_block(raw) == "print(1)"

    def test_plain_fenced_block(self):
        raw = "```\ndef test_x():\n    assert 1 == 1\n```"
        assert tg.extract_code_block(raw) == "def test_x():\n    assert 1 == 1"

    def test_terminal_newline_normalized(self):
        raw = "```python\ndef test_x():\n    assert 1 == 1\n\n```"
        body = tg.extract_code_block(raw)
        assert body == "def test_x():\n    assert 1 == 1"
        assert not body.endswith("\n")

    def test_bare_module_fallback(self):
        raw = "from recent_window import recent_window\n\ndef test_x():\n    assert 1 == 1"
        assert tg.extract_code_block(raw) == raw

    def test_prose_before_fence_still_extracts_block(self):
        # Deterministic framing removes ONE surrounding fence; surrounding
        # prose outside the fence does not change the parsed body.
        raw = "Here is my test:\n```python\npass\n```\nand more"
        assert tg.extract_code_block(raw) == "pass"

    def test_empty_response_rejected(self):
        with pytest.raises(tg.ExtractionError) as exc:
            tg.extract_code_block("   ")
        assert exc.value.category == "empty_response"

    def test_empty_fenced_block_rejected(self):
        with pytest.raises(tg.ExtractionError) as exc:
            tg.extract_code_block("```python\n```")
        assert exc.value.category == "empty_code_block"

    def test_non_module_prose_without_fence_rejected(self):
        with pytest.raises(tg.ExtractionError):
            tg.extract_code_block("Here is a test that asserts recent_window works.")


# ---------------------------------------------------------------------------
# T identity: T_parsed -> T_written is byte-identical via binary write
# ---------------------------------------------------------------------------


class TestWrittenIdentity:
    def test_written_bytes_equal_parsed_bytes(self, tmp_path):
        parsed = "from recent_window import recent_window\n\n\ndef test_x() -> None:\n    assert recent_window([1], 1) == [1]\n"
        target = tmp_path / "test_generated_regression.py"
        target.write_bytes(parsed.encode("utf-8"))
        written = target.read_bytes().decode("utf-8")
        assert tg._sha256(written) == tg._sha256(parsed)

    def test_framing_relation_is_recorded(self):
        # The probe records the deterministic framing relation explicitly;
        # here we pin the identity functions used for it.
        source = "def test_x():\n    pass"
        assert tg._sha256(source) == __import__("hashlib").sha256(
            source.encode("utf-8")
        ).hexdigest()


# ---------------------------------------------------------------------------
# First-response-only contract (amendment 2): EXACTLY ONE model call
# ---------------------------------------------------------------------------


class TestSingleAttempt:
    def test_successful_first_attempt_single_call(self, tmp_path):
        task = _task()
        fixture_dir = (
            REPO_ROOT
            / "agentic_debugger" / "datasets" / "curated" / "curated-off-by-one-002"
        )
        response = (
            "```python\n"
            "from recent_window import recent_window\n\n\n"
            "def test_full_window() -> None:\n"
            "    values = [10, 20, 30, 40]\n"
            "    assert recent_window(values, len(values)) == values\n"
            "```\n"
        )
        transport = FakeTransport((response,))
        outcome = tg.generate_frozen_test(
            transport, task, fixture_dir, tmp_path,
            model_name="test",
            request_timeout_seconds=60.0,
            test_timeout_seconds=20,
        )
        assert outcome.stop_reason == "frozen"
        assert outcome.frozen_test is not None
        assert outcome.frozen_test.attempt_index == 0
        assert len(outcome.attempts) == 1

    def test_failed_first_attempt_never_retries(self, tmp_path):
        task = _task()
        fixture_dir = (
            REPO_ROOT
            / "agentic_debugger" / "datasets" / "curated" / "curated-off-by-one-002"
        )
        # A second request would raise FakeTransport "exhausted": proving the
        # single-attempt contract by absence of a second call.
        transport = FakeTransport(("just some prose, no code block",))
        outcome = tg.generate_frozen_test(
            transport, task, fixture_dir, tmp_path,
            model_name="test",
            request_timeout_seconds=60.0,
            test_timeout_seconds=20,
        )
        assert outcome.frozen_test is None
        assert outcome.stop_reason == "no_executable_test"
        assert len(outcome.attempts) == 1
        assert outcome.attempts[0]["extraction"]["category"] == "no_code_block"

    def test_spec_section_hash_frozen(self):
        spec = tg.render_task_spec_section(_task())
        assert tg._sha256(spec) == (
            "18aea9f1f430465dac938b24385b079f6b0016e95fd617ae5be1aefdf7056604"
        )
