"""Leakage guard tests: held-out exclusion and oracle contamination."""

from __future__ import annotations

from pathlib import Path

from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.preference.leakage import (
    contamination_spans,
    is_contaminated,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-off-by-one-002"


def _oracle():
    task = DebugTask.from_file(str(FIXTURES / TASK_ID / "task.json"))
    return task.oracle


def test_verbatim_root_cause_summary_is_detected():
    oracle = _oracle()
    assert is_contaminated(oracle.root_cause_summary, oracle)
    spans = contamination_spans(oracle.root_cause_summary, oracle)
    assert spans[0]["field"] == "root_cause_summary"
    assert not is_contaminated("a completely unrelated response", oracle)


def test_target_symbols_and_files_are_reported_but_not_rejecting():
    oracle = _oracle()
    for symbol in oracle.target_symbols:
        spans = contamination_spans(f"the bug is in {symbol}", oracle)
        assert any(s["field"] == "target_symbols" and not s["rejecting"] for s in spans)
        assert not is_contaminated(f"the bug is in {symbol}", oracle)
    for path in oracle.target_files:
        spans = contamination_spans(f"see {path} for details", oracle)
        assert any(s["field"] == "target_files" and not s["rejecting"] for s in spans)
        assert not is_contaminated(f"see {path} for details", oracle)


def test_runtime_evidence_hint_is_detected():
    oracle = _oracle()
    assert is_contaminated(oracle.runtime_evidence_hint, oracle)
    spans = contamination_spans(oracle.runtime_evidence_hint, oracle)
    assert spans[0]["rejecting"] is True


def test_contamination_is_verbatim_no_normalization():
    oracle = _oracle()
    summary = oracle.root_cause_summary
    assert not is_contaminated(summary.upper(), oracle)
    assert not is_contaminated(summary.replace(" ", "-"), oracle)


def test_empty_oracle_values_are_safe():
    oracle = _oracle()
    assert not is_contaminated("", oracle)
