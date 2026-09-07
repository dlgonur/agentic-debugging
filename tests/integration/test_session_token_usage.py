"""Task-34 integration: session token usage through the durable live path.

A synthetic live session (offline: fake in-process transport, no provider,
no network) runs the production chain ``LiveModelAdapter`` ->
``DeterministicController`` -> ``ControllerSessionEventAdapter`` ->
``SessionEventEmitter`` -> ``SessionEventJournal``.  The durable
``model.request_completed`` events carry provider-reported usage, journal
serialization/deserialization preserves it, and prefix reductions expose
the same cumulative values after each completed request that the live UI
reduces — proving live/replay parity for the token summary.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agentic_debugger.agent.controller import ControllerRunConfig, DeterministicController
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry
from agentic_debugger.application.controller_adapter import (
    ControllerObservationContext,
    ControllerSessionEventAdapter,
)
from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.events import SessionEventKind, SourceKind
from agentic_debugger.application.journal import (
    JournalReadState,
    SessionEventJournal,
    read_session_journal,
)
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import (
    LiveModelAdapter,
    LiveModelConfig,
    LiveRunLimits,
)
from agentic_debugger.evaluation.task_schema import DebugTask

ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "curated-none-handling-001"
SESSION_ID = "session-token-usage-001"
RUN_ID = "run-token-usage-001"
SPEC_FINGERPRINT = "a" * 64


class _UsageTransport:
    """Deterministic offline transport: two provider-completed responses."""

    def __init__(self) -> None:
        self.calls = 0

    def request(self, payload, timeout_seconds):
        self.calls += 1
        if self.calls == 1:
            return {
                "usage": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 200,
                    "cached_input_tokens": 600,
                },
                "directive": {
                    "kind": "transition",
                    "target_state": "Understand",
                    "reason": "first",
                },
            }
        return {
            "usage": {
                "prompt_tokens": 1_500,
                "completion_tokens": 300,
                "cached_input_tokens": 1_000,
            },
            "directive": {
                "kind": "transition",
                "target_state": "Failed",
                "reason": "bounded synthetic stop",
            },
        }


def _run_synthetic_live_session(tmp_path: Path):
    task = DebugTask.from_mapping(
        json.loads(
            (ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()
        )
    )
    journal = SessionEventJournal(
        tmp_path / "session-token-usage.jsonl",
        session_id=SESSION_ID,
        task_id=task.task_id,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    emitter = SessionEventEmitter(
        session_id=SESSION_ID,
        task_id=task.task_id,
        source_kind=SourceKind.CONFIGURED_MODEL,
        run_id=RUN_ID,
        sink=journal,
    )
    emitter.emit(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": SPEC_FINGERPRINT})
    emitter.emit(SessionEventKind.SESSION_STARTED, {})

    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig("usage-synthetic", (sys.executable, "-c", "pass")),
        transport=_UsageTransport(),
        limits=LiveRunLimits(max_model_requests=2, max_retries=0, max_directive_repairs=0),
        registry=ToolRegistry(()),
    )
    observer = ControllerSessionEventAdapter(
        ControllerObservationContext(
            session_id=SESSION_ID,
            task_id=task.task_id,
            source_kind=SourceKind.CONFIGURED_MODEL,
            run_id=RUN_ID,
        ),
        emitter=emitter,
    )
    controller = DeterministicController(
        ToolRegistry(()),
        adapter,
        ControllerRunConfig(max_model_calls=2),
        observer=observer,
    )
    snapshot = ControllerSnapshot(
        RUN_ID,
        task.task_id,
        ControllerState.REPRODUCE,
        0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(),
        HypothesisLedger(),
    )
    result = controller.run(snapshot)
    journal.close()
    return adapter, observer, result


def test_live_session_usage_flows_to_journal_and_replay_reduces_identically(tmp_path):
    adapter, observer, result = _run_synthetic_live_session(tmp_path)
    assert result.stop_reason is not None

    # The adapter aggregated each logical call's provider-reported usage.
    assert observer is not None
    live_events = observer.events()
    completions = [
        event
        for event in live_events
        if event.event_kind is SessionEventKind.MODEL_REQUEST_COMPLETED
    ]
    assert len(completions) == 2
    assert dict(completions[0].payload)["token_usage"] is not None
    assert dict(dict(completions[0].payload)["token_usage"]) == {
        "input_tokens": 1_000,
        "output_tokens": 200,
        "cached_input_tokens": 600,
        "total_tokens": 1_200,
    }
    assert dict(dict(completions[1].payload)["token_usage"]) == {
        "input_tokens": 1_500,
        "output_tokens": 300,
        "cached_input_tokens": 1_000,
        "total_tokens": 1_800,
    }

    # Journal serialization/deserialization preserves the usage blocks.
    read = read_session_journal(
        tmp_path / "session-token-usage.jsonl"
    )
    assert read.state is JournalReadState.INTERRUPTED  # no terminal session event
    journal_completions = [
        event
        for event in read.events
        if event.event_kind is SessionEventKind.MODEL_REQUEST_COMPLETED
    ]
    assert len(journal_completions) == 2
    for live, replayed in zip(completions, journal_completions):
        assert replayed.to_mapping()["payload"] == live.to_mapping()["payload"]

    # Incremental reduction: cumulative usage updates after each completed
    # request, and Cached stays a subset (never added into Total).
    identity = PresentationIdentity(
        task_id=TASK_ID, source_kind=SourceKind.CONFIGURED_MODEL, session_id=SESSION_ID
    )
    view = initial_session_view(identity)
    snapshots = []
    for event in read.events:
        view = reduce_event(view, event)
        if event.event_kind is SessionEventKind.MODEL_REQUEST_COMPLETED:
            snapshots.append(view.token_usage)

    assert len(snapshots) == 2
    after_first = snapshots[0]
    assert after_first.requests_completed == 1
    assert after_first.requests_with_usage == 1
    assert after_first.input_tokens == 1_000
    assert after_first.cached_input_tokens == 600
    assert after_first.output_tokens == 200
    assert after_first.total_tokens == 1_200
    assert after_first.complete

    after_second = snapshots[1]
    assert after_second.requests_completed == 2
    assert after_second.requests_with_usage == 2
    assert after_second.input_tokens == 2_500
    assert after_second.cached_input_tokens == 1_600
    assert after_second.output_tokens == 500
    # Total is Input + Output: 2,500 + 500 = 3,000 (Cached NOT added again).
    assert after_second.total_tokens == 3_000
    assert after_second.effective_total_tokens == 3_000
    assert after_second.complete
    assert view.token_usage == after_second
