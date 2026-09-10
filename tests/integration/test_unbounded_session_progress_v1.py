"""Task 44 — Unbounded Session Progress v1 regressions.

Proves the repository-wide runtime rule against the REAL configured
Level-32 runtime (no monkeypatched controller/model boundary):

> Interactive/configured sessions do not have Agentic-Debugger-owned
> total model-request, directive, or controller-step execution ceilings.
> Progress counters are telemetry only.

Coverage:

* ``test_configured_level32_continues_past_old_25_request_ceiling`` —
  mandatory real regression: drives the real ``run_configured_session``
  for ``LEVEL32_TASK_ID`` with a fake provider subprocess through more
  than the old 25-request ceiling (>= 30 successful model requests).
  Fails against Candidate 51 (dies at request #25 via
  ``controller budget exhausted; directive exhausted``).
* ``test_configured_level32_continues_past_historical_40_step_ceiling`` —
  crosses the historical 40 controller-step ceiling (>= 41 valid
  progression steps, here 45) with the same real runtime.
* ``test_local_project_shared_authority_unbounded`` — representative
  Local Project regression through the SHARED controller/adapter
  authority (no duplicated implementation): unbounded limits (None)
  with Local Project task constraints continue past the old 32-call
  default.

All tests mock the EXTERNAL provider response only (fake subprocess /
fake transport); the controller/model execution boundary
(``run_local_session``, ``DeterministicController.run``,
``LiveModelAdapter`` progression authority) is never monkeypatched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agentic_debugger.application.configured_source import run_configured_session
from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.events import SessionEventKind, SourceKind
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.application.level32 import LEVEL32_TASK_ID
from agentic_debugger.application.level32_materialization import LEVEL32_INTERNAL_TASK_ID
from agentic_debugger.application.model_gateway import provider_runtime_identity
from agentic_debugger.application import provider_connections as pc
from agentic_debugger.application.sources import ModelExecutionError
from agentic_debugger.application.worker_scenarios import ScenarioContext
from agentic_debugger.cancellation import CancellationToken
from agentic_debugger.evaluation.live import LiveModelConfig

from test_level32_configured_runtime import (
    _seed_hermetic_level32_source,
    _setup_test_providers,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Exact Candidate-51 historical ceilings (Task-42 configured Level-32).
OLD_LEVEL32_MODEL_REQUEST_CEILING = 25
OLD_LEVEL32_CONTROLLER_STEP_CEILING = 25
# Historical treatment/UI ceiling (LiveTreatmentBudget 40/STEP /40).
HISTORICAL_TREATMENT_STEP_CEILING = 40


def _hypothesis_churn(n_pairs: int, start: int = 1) -> list[dict[str, Any]]:
    """Build ``n_pairs`` add/support pairs staying within tool budgets.

    Every directive is legal in UNDERSTAND, consumes NO patch/test/pdb/
    source budget, and keeps at most one active hypothesis — so the run
    can continue indefinitely without tripping the retained per-action
    tool bounds.  Each pair is two model requests / two controller steps.
    """
    seq: list[dict[str, Any]] = []
    counter = start
    for _ in range(n_pairs):
        hid = f"h{counter}"
        seq.append(
            {
                "kind": "add_hypothesis",
                "hypothesis_id": hid,
                "statement": f"hypothesis {counter} observes shallow merge in get_config",
                "confidence": "low",
                "evidence_refs": [],
                "requires_runtime_evidence": False,
            }
        )
        seq.append(
            {
                "kind": "set_hypothesis_status",
                "hypothesis_id": hid,
                "status": "supported",
            }
        )
        counter += 1
    return seq


def _level32_directives(n_successful: int) -> list[dict[str, Any]]:
    """Scripted legal directives: 1 repro + 1 transition + hypothesis churn.

    ``n_successful`` counts the well-formed directives; one deliberately
    malformed terminal is appended so the run ends with the honest
    scripted controller failure AFTER crossing old ceilings (never via
    a count budget).
    """
    assert n_successful >= 3
    seq: list[dict[str, Any]] = [
        {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
        {"kind": "transition", "target_state": "Understand", "reason": "baseline reproduced failure"},
    ]
    remaining = n_successful - len(seq)
    n_pairs = remaining // 2
    seq.extend(_hypothesis_churn(n_pairs))
    if len(seq) < n_successful:
        hid = f"h-tail-{len(seq)}"
        seq.append(
            {
                "kind": "add_hypothesis",
                "hypothesis_id": hid,
                "statement": "tail hypothesis observes shallow merge in get_config",
                "confidence": "low",
                "evidence_refs": [],
                "requires_runtime_evidence": False,
            }
        )
    assert len(seq) == n_successful
    seq.append({"kind": "transition", "state": "FAILED"})
    return seq


def _run_level32_with_fake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directives: list[dict[str, Any]],
) -> dict[str, Any]:
    """Drive the REAL configured Level-32 runtime with a fake provider.

    Returns evidence: sizes, served count, journal kinds, terminal text,
    adapter metrics, provider provenance.
    """
    _setup_test_providers(tmp_path, monkeypatch)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nonexistent_localappdata"))
    hermetic_cache = tmp_path / "hermetic_cache" / "cookiecutter-967-base-source"
    _seed_hermetic_level32_source(hermetic_cache)
    monkeypatch.setattr(
        "agentic_debugger.application.level32_materialization.default_base_source_cache_dir",
        lambda: hermetic_cache,
    )

    sizes_path = tmp_path / "request_sizes.jsonl"
    counter_path = tmp_path / "model_calls.txt"
    counter_path.write_text("0", encoding="utf-8")

    fake_model_path = tmp_path / "fake_unbounded_model.py"
    fake_model_path.write_text(
        "from __future__ import annotations\n"
        "import json\n"
        "import sys\n"
        f"REPO_ROOT = {str(REPO_ROOT)!r}\n"
        f"SIZES = {str(sizes_path)!r}\n"
        f"COUNTER = {str(counter_path)!r}\n"
        f"DIRECTIVES = {json.dumps(directives)!r}\n"
        "raw = sys.stdin.buffer.readline()\n"
        "with open(SIZES, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(str(len(raw)) + chr(10))\n"
        "count = int(open(COUNTER, encoding='utf-8').read().strip() or '0')\n"
        "open(COUNTER, 'w', encoding='utf-8').write(str(count + 1))\n"
        "directives = json.loads(DIRECTIVES)\n"
        "content = json.dumps(directives[count] if count < len(directives) else directives[-1])\n"
        "response = {'provider_completion_schema_version': 'provider-completion-v1',\n"
        "    'directive_content': content,\n"
        "    'usage': {'prompt_tokens': 20, 'completion_tokens': 15, 'total_tokens': 35}}\n"
        "print(json.dumps(response))\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )

    import agentic_debugger.application.configured_source as cs

    c = pc.get_provider_config("commandcode_goat")
    authority = provider_runtime_identity(c)

    def wrapped_resolve(provider: str, model_id: str, **kwargs: Any):
        cfg = LiveModelConfig(
            model_name="unbounded-model",
            command=(sys.executable, str(fake_model_path)),
            request_timeout_seconds=60,
            tool_version="test-v1",
        )
        return (
            cfg,
            {
                "display_name": "Unbounded Model",
                "route": "direct_api",
                "api_protocol": "chat_completions",
                "provider_model_id": model_id,
                "endpoint": "https://api.commandcode.ai/provider/v1",
                "provider_runtime_identity": authority,
                "protocol_version": "1.3",
            },
            "f" * 64,
        )

    monkeypatch.setattr(cs, "_resolve_registry_model", wrapped_resolve)

    journal_path = tmp_path / "journal.events.jsonl"
    journal = SessionEventJournal(
        journal_path,
        session_id="sess-l32-unbounded",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    emitter = SessionEventEmitter(
        sink=journal,
        session_id="sess-l32-unbounded",
        task_id=LEVEL32_TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    ctx = ScenarioContext(
        work_dir=work_dir,
        emitter=emitter,
        token=CancellationToken(),
    )

    terminal = ""
    try:
        run_configured_session(
            ctx,
            {
                "provider": "commandcode_goat",
                "model_id": "muse/muse-spark-1.3",
                "policy": "pdb-on-uncertainty",
            },
        )
    except ModelExecutionError as exc:
        terminal = str(exc)
        # Re-raise shape is asserted by callers; capture metrics first.
        pass
    else:
        pytest.fail("scripted run should end with the honest malformed-directive failure")

    sizes = [
        int(line.strip())
        for line in sizes_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    served = int(counter_path.read_text(encoding="utf-8").strip())
    events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    kinds = [e["event_kind"] for e in events]
    provenance = next(
        (e["payload"] for e in events if e["event_kind"] == SessionEventKind.MODEL_CONFIGURED.value),
        {},
    )
    return {
        "terminal": terminal,
        "sizes": sizes,
        "served": served,
        "kinds": kinds,
        "provenance": provenance,
        "journal_path": journal_path,
        "work_dir": work_dir,
    }


def test_configured_level32_continues_past_old_25_request_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mandatory Task-44 regression: real Level-32 runtime past request #25.

    Drives the REAL ``run_configured_session`` for ``LEVEL32_TASK_ID``
    with fake provider responses through 32 successful model requests
    (> old 25 ceiling).  Fails against Candidate 51 at request #25.
    """
    n_successful = 32
    directives = _level32_directives(n_successful)
    evidence = _run_level32_with_fake(tmp_path, monkeypatch, directives)

    # 1-3: execution continued beyond the old ceiling.
    assert len(evidence["sizes"]) > OLD_LEVEL32_MODEL_REQUEST_CEILING, (
        f"stopped at old ceiling: {evidence['sizes']}"
    )
    # Request #25 (index 24) and #26 (index 25) were actually sent.
    assert len(evidence["sizes"]) >= 26
    # 4-6: no count-derived terminal taxonomy in the new run.
    assert "directive exhausted" not in evidence["terminal"]
    assert "directive_exhausted" not in evidence["terminal"]
    assert "controller budget exhausted" not in evidence["terminal"]
    assert "model request limit" not in evidence["terminal"]
    assert "model_request_limit" not in evidence["terminal"]
    assert "model-request-budget" not in evidence["terminal"]
    # The scripted ending is the honest directive rejection, never a
    # provider/transport error (the Candidate-51 death shape).
    assert "directive_rejected" in evidence["terminal"]
    assert "provider_or_transport_error" not in evidence["terminal"]
    # 7: provider/model identity remains correct.
    assert evidence["provenance"].get("provider") == "commandcode_goat"
    assert evidence["provenance"].get("provider_model_id") == "muse/muse-spark-1.3"
    # 8: counters kept recording accurate totals (one transport per
    # logical request until the malformed tail; tail adds repair retries).
    assert evidence["served"] >= n_successful
    started = evidence["kinds"].count(SessionEventKind.MODEL_REQUEST_STARTED.value)
    completed = evidence["kinds"].count(SessionEventKind.MODEL_REQUEST_COMPLETED.value)
    assert started == completed
    assert started >= n_successful
    assert len(evidence["sizes"]) >= started
    assert SessionEventKind.MODEL_CONFIGURED.value in evidence["kinds"]
    staging_fixture = (
        evidence["work_dir"]
        / "level32_staging"
        / "agentic_debugger"
        / "datasets"
        / "curated"
        / LEVEL32_INTERNAL_TASK_ID
    )
    assert (staging_fixture / "cookiecutter" / "config.py").is_file()


def test_configured_level32_continues_past_historical_40_step_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task-44 second regression: real Level-32 runtime past step #40.

    The historical 40 controller-step ceiling (LiveTreatmentBudget 40 /
    STEP /40 UI) must not terminate generic execution: at least 41 valid
    progression steps remain live/progressing (here 45 successful model
    requests through the same real runtime).
    """
    n_successful = 45
    directives = _level32_directives(n_successful)
    evidence = _run_level32_with_fake(tmp_path, monkeypatch, directives)

    assert len(evidence["sizes"]) > HISTORICAL_TREATMENT_STEP_CEILING, (
        f"stopped at historical 40 ceiling: {len(evidence['sizes'])}"
    )
    assert len(evidence["sizes"]) >= 41
    assert "directive exhausted" not in evidence["terminal"]
    assert "controller budget exhausted" not in evidence["terminal"]
    assert "model request limit" not in evidence["terminal"]
    assert "model_request_limit" not in evidence["terminal"]
    assert "directive_rejected" in evidence["terminal"]
    started = evidence["kinds"].count(SessionEventKind.MODEL_REQUEST_STARTED.value)
    assert started >= 41
    assert evidence["served"] >= n_successful


def test_local_project_shared_authority_unbounded() -> None:
    """Local Project representative regression via the SHARED authority.

    The limit authority is shared (DeterministicController +
    LiveModelAdapter); Local Project duplicates no implementation.  This
    drives that shared authority with unbounded limits (None) through 35
    valid UNDERSTAND self-loop transitions — past the old Local Project
    default (32) — ending with the honest scripted FAILED transition
    (never via a count budget).
    """
    import json as _json
    from pathlib import Path as _Path

    from agentic_debugger.agent.controller import ControllerRunConfig, DeterministicController
    from agentic_debugger.agent.controller_policy import (
        ControllerBudgetLimits,
        ControllerBudgetState,
        HypothesisLedger,
    )
    from agentic_debugger.agent.model_adapter import ControllerSnapshot
    from agentic_debugger.agent.state_machine import ControllerState
    from agentic_debugger.agent.tool_registry import ToolRegistry
    from agentic_debugger.demo.policies import DemoPolicy
    from agentic_debugger.evaluation.live import LiveModelAdapter, LiveModelConfig, LiveRunLimits
    from agentic_debugger.evaluation.runner import load_task

    old_local_default = 32
    n_churn = 35

    repo_root = _Path(__file__).resolve().parents[2]
    task = load_task(
        str(repo_root / "agentic_debugger" / "datasets" / "curated" / "curated-off-by-one-002" / "task.json")
    )

    calls: list[dict] = []

    class FakeTransport:
        def request(self, payload, timeout_seconds):
            idx = len(calls)
            calls.append(payload)
            assert payload["protocol"]["logical_model_call_index"] == idx
            if idx < n_churn:
                directive = {
                    "kind": "transition",
                    "target_state": "Understand",
                    "reason": f"churn {idx} continue reasoning past old ceiling",
                }
            else:
                directive = {
                    "kind": "transition",
                    "target_state": "Failed",
                    "reason": "scripted end after unbounded proof",
                }
            return {
                "directive": directive,
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            }

    registry = ToolRegistry(())
    config = LiveModelConfig(
        model_name="local-fake",
        command=("echo", "hi"),
        request_timeout_seconds=30,
        tool_version="test-v1",
    )
    limits = LiveRunLimits(
        max_model_requests=None,
        max_controller_steps=None,
        max_retries=2,
        max_directive_repairs=0,
    )
    assert limits.max_model_requests is None
    assert limits.max_controller_steps is None
    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=config,
        transport=FakeTransport(),
        limits=limits,
        registry=registry,
        evaluation_id="eval-local",
        case_id="case-local",
        run_id="run-local",
        trajectory_id="run-local",
    )
    controller = DeterministicController(
        registry, adapter, ControllerRunConfig(max_model_calls=None)
    )
    assert controller.config.max_model_calls is None
    snapshot = ControllerSnapshot(
        "run-local",
        task.task_id,
        ControllerState.UNDERSTAND,
        0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(),
        HypothesisLedger(),
    )
    result = controller.run(snapshot)
    # Honest scripted FAILED ending after 35 valid churn steps — never
    # via a total-session count ceiling.
    from agentic_debugger.agent.controller import ControllerStopReason

    assert result.stop_reason is ControllerStopReason.FAILED
    assert result.model_calls == n_churn + 1
    assert result.model_calls > old_local_default
    assert len(result.steps) == n_churn + 1
    assert adapter.metrics.model_requests == n_churn + 1
    assert adapter.metrics.termination_reason is None
    assert len(calls) == n_churn + 1
    # No count-derived taxonomy was emitted.
    assert adapter.metrics.termination_reason not in {
        "model_request_limit",
        "controller_step_limit",
        "directive_rejected",
    }
