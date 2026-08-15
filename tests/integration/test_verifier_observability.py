"""Task-4 verifier observability integration tests.

Runs the real independent EvaluationVerifier over curated fixtures and
proves the optional progress observer and between-stage cancellation
checkpoints are non-invasive: results are unchanged with/without observation,
stage ordering is truthful, cancellation stays operational (never a
scientific verdict), and observer failures never alter verifier behavior.
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from agentic_debugger.application.events import (
    SourceKind,
    VerifierStage,
    VerifierStageStatus,
)
from agentic_debugger.application.observability import ObservabilityContext
from agentic_debugger.application.verifier_observer import VerifierSessionEventAdapter
from agentic_debugger.cancellation import CancellationError, CancellationReason
from agentic_debugger.evaluation import load_task
from agentic_debugger.evaluation.runner import EvaluationStatus
from agentic_debugger.evaluation.verifier import (
    EvaluationVerifier,
    VerifierProgressObserver,
)

ROOT = Path(__file__).resolve().parents[2]
CURATED = ROOT / "agentic_debugger" / "datasets" / "curated"
TASK_ID = "curated-none-handling-001"
REL = "display_name.py"
OLD = "normalized_name = name.strip()"
NEW = 'normalized_name = name.strip() if name is not None else ""'


def _patch() -> str:
    source = (CURATED / TASK_ID / REL).read_text(encoding="utf-8")
    return "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            source.replace(OLD, NEW).splitlines(keepends=True),
            fromfile=f"a/{REL}",
            tofile=f"b/{REL}",
            lineterm="\n",
        )
    )


def _task():
    return load_task(str(CURATED / TASK_ID / "task.json"))


def _adapter() -> VerifierSessionEventAdapter:
    context = ObservabilityContext(
        session_id="session-verifier-int-001",
        task_id=TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
        run_id="run-verifier-int-001",
    )
    return VerifierSessionEventAdapter(context)


def _verifier(tmp_path, observer=None, cancel_check=None) -> EvaluationVerifier:
    return EvaluationVerifier(
        str(ROOT),
        workspace_parent=str(tmp_path),
        progress_observer=observer,
        cancel_check=cancel_check,
    )


class RecordingObserver:
    """Simple observer capturing ordered stage boundaries."""

    def __init__(self):
        self.stages: list[tuple[str, str]] = []

    def stage_started(self, stage: str) -> None:
        self.stages.append((stage, "started"))

    def stage_completed(self, stage: str, status: str) -> None:
        self.stages.append((stage, status))


EXPECTED_ORDER = (
    "prepare_workspace",
    "baseline_reproduction",
    "pre_patch_targeted",
    "apply_candidate",
    "syntax_validation",
    "post_patch_reproduction",
    "f2p_p2p_checks",
    "broader_suite",
    "classification",
    "cleanup_integrity",
)


def test_verifier_result_unchanged_with_observer(tmp_path):
    task = _task()
    patch = _patch()
    (tmp_path / "plain").mkdir()
    (tmp_path / "observed").mkdir()
    plain = EvaluationVerifier(str(ROOT), workspace_parent=str(tmp_path / "plain")).evaluate(task, patch)
    observer = RecordingObserver()
    observed = EvaluationVerifier(
        str(ROOT), workspace_parent=str(tmp_path / "observed"), progress_observer=observer
    ).evaluate(task, patch)
    assert observed.status is plain.status
    assert observed.outcome is plain.outcome
    assert observed.f2p_passed == plain.f2p_passed
    assert observed.f2p_total == plain.f2p_total
    assert observed.p2p_passed == plain.p2p_passed
    assert observed.p2p_total == plain.p2p_total
    assert observed.workspace.cleaned == plain.workspace.cleaned
    assert observed.to_mapping() == plain.to_mapping()
    # The full pipeline ran in the canonical stage order.
    started = [name for name, status in observer.stages if status == "started"]
    assert started == list(EXPECTED_ORDER)


def test_verifier_adapter_stage_events_ordered(tmp_path):
    task = _task()
    patch = _patch()
    adapter = _adapter()
    adapter.started()
    result = _verifier(tmp_path, observer=adapter).evaluate(task, patch)
    adapter.completed(result)
    kinds = [e.event_kind.value for e in adapter.events()]
    assert kinds[0] == "verifier.started"
    assert kinds[-1] == "verifier.completed"
    stage_names = [
        e.payload["stage"]
        for e in adapter.events()
        if e.event_kind.value == "verifier.stage_started"
    ]
    assert stage_names == list(EXPECTED_ORDER)
    final = adapter.events()[-1].payload
    assert final["status"] == "COMPLETED"
    assert final["f2p_passed"] == final["f2p_total"]


@pytest.mark.parametrize("cancel_after", [0, 3])
def test_verifier_cancellation_is_operational_never_scientific(tmp_path, cancel_after):
    task = _task()
    patch = _patch()
    calls = {"count": 0}

    def cancel_check():
        calls["count"] += 1
        if calls["count"] > cancel_after:
            raise CancellationError(CancellationReason.CANCELLED)

    observer = RecordingObserver()
    verifier = _verifier(tmp_path, observer=observer, cancel_check=cancel_check)
    with pytest.raises(CancellationError):
        verifier.evaluate(task, patch)
    # The cancellation propagated out of evaluate: no scientific result was
    # fabricated and no verifier stage ran beyond the terminal cleanup cycle
    # (cleanup_integrity always runs in the finally block).
    assert calls["count"] == cancel_after + 1
    if cancel_after == 0:
        assert [name for name, _ in observer.stages] == [
            "cleanup_integrity",
            "cleanup_integrity",
        ]


def test_verifier_observer_failure_swallowed(tmp_path):
    task = _task()
    patch = _patch()
    (tmp_path / "plain").mkdir()
    (tmp_path / "observed").mkdir()

    class RaisingObserver:
        def stage_started(self, stage: str) -> None:
            raise RuntimeError("observer bug")

        def stage_completed(self, stage: str, status: str) -> None:
            raise RuntimeError("observer bug")

    plain = EvaluationVerifier(str(ROOT), workspace_parent=str(tmp_path / "plain")).evaluate(task, patch)
    observed = EvaluationVerifier(
        str(ROOT),
        workspace_parent=str(tmp_path / "observed"),
        progress_observer=RaisingObserver(),
    ).evaluate(task, patch)
    assert observed.to_mapping() == plain.to_mapping()
