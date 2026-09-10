"""Live adapter RAG seam: default byte identity and bounded injection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import (
    ActionName,
    ObservationStatus,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import (
    LiveModelAdapter,
    LiveModelAdapterError,
    LiveModelConfig,
    LiveRunLimits,
)
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.rag.context import PUBLIC_REQUEST_BYTE_BUDGET
from agentic_debugger.rag.corpus import build_corpus
from agentic_debugger.rag.indexer import build_index
from agentic_debugger.rag.retrieval import retrieve
from agentic_debugger.rag.schema import canonical_json

ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "curated-none-handling-001"


def _config() -> LiveModelConfig:
    return LiveModelConfig("test-model", ("test-model-command",))


def _registry() -> ToolRegistry:
    return ToolRegistry(
        (
            ToolSpec(
                ActionName.RUN_REPRODUCTION,
                lambda arguments: dict(arguments),
                lambda _action, _arguments: ToolResult(
                    ObservationStatus.OK, {}, "ok"
                ),
                argument_contract={
                    "required": ["phase"],
                    "properties": {
                        "phase": {"type": "string", "min_length": 1}
                    },
                    "additional_properties": False,
                },
            ),
        )
    )


def _task() -> DebugTask:
    return DebugTask.from_file(
        str(ROOT / "agentic_debugger" / "datasets" / "curated" / TASK_ID / "task.json")
    )


def _snapshot():
    task = _task()
    return ControllerSnapshot(
        "run-x",
        task.task_id,
        ControllerState.REPRODUCE,
        0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(),
        HypothesisLedger(),
    )


def _adapter(**kwargs):
    return LiveModelAdapter(
        task=_task(),
        policy=DemoPolicy.STATIC_BASELINE,
        config=_config(),
        transport=None,
        limits=LiveRunLimits(max_model_requests=1),
        registry=_registry(),
        evaluation_id="eval-x",
        case_id="case-x",
        run_id="run-x",
        trajectory_id="trajectory-x",
        **kwargs,
    )


def _rag_context():
    fixture = ROOT / "agentic_debugger" / "datasets" / "curated" / TASK_ID
    corpus = build_corpus(str(fixture), mode="fixture", task_id=TASK_ID)
    index = build_index(corpus, revision="rev1", repository_identity="r", task_id=TASK_ID)
    result = retrieve(index, "display name missing value", expected_revision="rev1")
    from agentic_debugger.rag.context import RagContext

    return RagContext.from_retrieval(result, retrieval_latency_ms=3)


def _normalized(request: dict) -> str:
    """Canonical request bytes with the per-call uuid request id pinned."""
    request = dict(request)
    protocol = dict(request["protocol"])
    protocol["request_id"] = "fixed-for-identity-test"
    request["protocol"] = protocol
    return canonical_json(request)


def test_default_request_has_no_retrieved_context_key():
    adapter = _adapter()
    request = adapter._request_context(_snapshot(), logical_request_index=0, transport_attempt_index=1)
    assert "retrieved_context" not in request


def test_default_canonical_request_is_byte_identical_across_builds():
    adapter = _adapter()
    first = _normalized(adapter._request_context(_snapshot(), logical_request_index=0, transport_attempt_index=1))
    second = _normalized(adapter._request_context(_snapshot(), logical_request_index=0, transport_attempt_index=1))
    assert first == second


def test_rag_enabled_request_adds_only_the_bounded_block():
    base_adapter = _adapter()
    base_request = _normalized(base_adapter._request_context(_snapshot(), logical_request_index=0, transport_attempt_index=1))
    rag_adapter = _adapter(rag_context=_rag_context())
    rag_request = rag_adapter._request_context(_snapshot(), logical_request_index=0, transport_attempt_index=1)
    assert "retrieved_context" in rag_request
    stripped = {k: v for k, v in rag_request.items() if k != "retrieved_context"}
    assert _normalized(stripped) == base_request


def test_rag_request_stays_inside_public_budget():
    rag_adapter = _adapter(rag_context=_rag_context())
    request = rag_adapter._request_context(_snapshot(), logical_request_index=0, transport_attempt_index=1)
    serialized = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert len(serialized) <= PUBLIC_REQUEST_BYTE_BUDGET


def test_live_boundary_rejects_lookalike_rag_context_objects():
    """Only a validated RagContext may cross the live boundary."""
    from agentic_debugger.evaluation.live import LiveConfigurationError

    class Lookalike:
        def to_request_mapping(self):
            return {"chunks": []}

        def to_record_mapping(self):
            return {"chunk_count": 0}

    with pytest.raises(LiveConfigurationError):
        _adapter(rag_context=Lookalike())
    with pytest.raises(LiveConfigurationError):
        _adapter(rag_context={"index_id": "x"})


def test_next_directive_forwards_large_rag_request_to_transport(monkeypatch):
    """Request plus RAG context above the historical budget reaches transport.

    Task 43 (provider-owned request size): the removed internal
    public-request ceiling must no longer terminate execution before the
    provider.  A RAG-enabled request larger than the historical 20,000-byte
    budget is constructed, handed to the transport in full, and answered.
    """

    class CapturingTransport:
        def __init__(self):
            self.calls = 0
            self.payloads = []

        def request(self, payload, timeout_seconds):
            self.calls += 1
            self.payloads.append(payload)
            return {"directive": {"kind": "transition", "target_state": "Understand", "reason": "large context accepted"}}

    transport = CapturingTransport()
    adapter = LiveModelAdapter(
        task=_task(),
        policy=DemoPolicy.STATIC_BASELINE,
        config=_config(),
        transport=transport,
        limits=LiveRunLimits(max_model_requests=1),
        registry=_registry(),
        rag_context=_rag_context(),
    )
    # A real RagContext is at most ~4 KiB; pad the task-visible history so
    # the serialized request deterministically exceeds the historical
    # 20,000-byte ceiling.
    adapter.history.append(
        {
            "request_index": 0,
            "state": ControllerState.REPRODUCE.value,
            "allowed_actions": [],
            "last_observation": {"padding": "x" * 24_000},
        }
    )
    directive = adapter.next_directive(_snapshot())
    assert transport.calls == 1  # handed to transport, never gated
    sent_bytes = json.dumps(
        transport.payloads[0], ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    from agentic_debugger.rag.schema import PUBLIC_REQUEST_BYTE_BUDGET as HISTORICAL_BUDGET

    assert HISTORICAL_BUDGET == 20_000
    assert len(sent_bytes) > HISTORICAL_BUDGET
    assert transport.payloads[0]["history"][0]["last_observation"]["padding"] == "x" * 24_000
    assert directive.target_state is ControllerState.UNDERSTAND
    assert adapter.metrics.termination_reason is None


def test_frozen_quixbugs_path_never_passes_rag_context():
    """The additive kwarg defaults to None; no caller change is required."""
    adapter = _adapter()
    assert adapter._rag_context is None
