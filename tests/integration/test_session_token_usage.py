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
from agentic_debugger.ui.widgets import session_tokens_breakdown, session_tokens_summary

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


class _PartialUsageTransport:
    """Deterministic offline transport: attempt 1 reports usage but rejected directive,
    attempt 2 has valid directive but lacks usage block."""

    def __init__(self) -> None:
        self.calls = 0

    def request(self, payload, timeout_seconds):
        self.calls += 1
        if self.calls == 1:
            return {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "cached_input_tokens": 40,
                },
                "directive": {
                    "kind": "transition",
                    "target_state": "InvalidNonExistentState",
                    "reason": "bad state",
                },
            }
        return {
            "directive": {
                "kind": "transition",
                "target_state": "Failed",
                "reason": "bounded synthetic stop",
            },
        }


class _ExactTotalPartialComponentsTransport:
    """Deterministic offline transport: attempt 1 reports 100/20/120 with rejected
    directive; attempt 2 reports valid directive but only total_tokens=150."""

    def __init__(self) -> None:
        self.calls = 0

    def request(self, payload, timeout_seconds):
        self.calls += 1
        if self.calls == 1:
            return {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
                "directive": {
                    "kind": "transition",
                    "target_state": "InvalidNonExistentState",
                    "reason": "bad state",
                },
            }
        return {
            "usage": {
                "total_tokens": 150,
            },
            "directive": {
                "kind": "transition",
                "target_state": "Failed",
                "reason": "bounded synthetic stop",
            },
        }



def _run_synthetic_live_session(
    tmp_path: Path,
    transport=None,
    limits=None,
    session_id: str = SESSION_ID,
    journal_name: str = "session-token-usage.jsonl",
):
    if transport is None:
        transport = _UsageTransport()
    if limits is None:
        limits = LiveRunLimits(max_model_requests=2, max_retries=0, max_directive_repairs=0)
    task = DebugTask.from_mapping(
        json.loads(
            (ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()
        )
    )
    journal = SessionEventJournal(
        tmp_path / journal_name,
        session_id=session_id,
        task_id=task.task_id,
        source_kind=SourceKind.CONFIGURED_MODEL,
    )
    emitter = SessionEventEmitter(
        session_id=session_id,
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
        transport=transport,
        limits=limits,
        registry=ToolRegistry(()),
    )
    observer = ControllerSessionEventAdapter(
        ControllerObservationContext(
            session_id=session_id,
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


def test_partial_logical_call_preserves_lower_bound_and_replay_parity(tmp_path):
    adapter, observer, result = _run_synthetic_live_session(
        tmp_path,
        transport=_PartialUsageTransport(),
        limits=LiveRunLimits(max_model_requests=2, max_retries=0, max_directive_repairs=1),
        session_id="session-token-partial-001",
        journal_name="session-token-partial.jsonl",
    )
    assert result.stop_reason is not None

    # The adapter preserved attempt 1's usage as a lower bound and marked coverage partial.
    from agentic_debugger.agent.token_usage import TokenUsage
    assert adapter.last_request_token_usage() == TokenUsage(
        input_tokens=100, output_tokens=20, cached_input_tokens=40, total_tokens=120
    )
    coverage = adapter.last_request_token_coverage()
    assert coverage is not None
    assert not coverage.input_tokens
    assert not coverage.cached_input_tokens
    assert not coverage.output_tokens
    assert not coverage.total_tokens

    # Controller emitted MODEL_REQUEST_COMPLETED carrying usage and coverage.
    live_events = observer.events()
    completions = [
        event
        for event in live_events
        if event.event_kind is SessionEventKind.MODEL_REQUEST_COMPLETED
    ]
    assert len(completions) == 1
    assert dict(completions[0].payload["token_usage"]) == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_input_tokens": 40,
        "total_tokens": 120,
    }
    assert dict(completions[0].payload["token_usage_coverage"]) == {
        "input_tokens": False,
        "output_tokens": False,
        "cached_input_tokens": False,
        "total_tokens": False,
    }

    # Journal roundtrip preserves the coverage block.
    read = read_session_journal(tmp_path / "session-token-partial.jsonl")
    journal_completions = [
        event
        for event in read.events
        if event.event_kind is SessionEventKind.MODEL_REQUEST_COMPLETED
    ]
    assert len(journal_completions) == 1
    assert journal_completions[0].to_mapping()["payload"] == completions[0].to_mapping()["payload"]

    # Live and replayed views match identically.
    identity = PresentationIdentity(
        task_id=TASK_ID, source_kind=SourceKind.CONFIGURED_MODEL, session_id="session-token-partial-001"
    )
    view = initial_session_view(identity)
    for event in read.events:
        view = reduce_event(view, event)

    usage = view.token_usage
    assert usage.requests_completed == 1
    assert usage.requests_with_usage == 1
    assert usage.input_tokens == 100
    assert usage.cached_input_tokens == 40
    assert usage.output_tokens == 20
    assert usage.total_tokens == 120
    assert usage.requests_complete_input == 0
    assert usage.requests_complete_cached == 0
    assert usage.requests_complete_output == 0
    assert usage.requests_complete_total == 0
    assert usage.is_partial("input_tokens")
    assert usage.is_partial("cached_input_tokens")
    assert usage.is_partial("output_tokens")
    assert usage.is_partial("total_tokens")
    assert not usage.total_complete

    assert session_tokens_summary(usage) == "Tokens 120 (partial)"
    assert session_tokens_breakdown(usage) == "In 100+ · Cache 40+ · Out 20+"


def test_session_token_usage_exact_total_with_partial_components(tmp_path):
    """F5 / Regression H: Live session with exact total and partial components
    flows through journal, reduces identically on replay, and verifies both
    UI header summary ('Tokens 270') and workstream detail ('Input 100+ · Output 20+ · Total 270')."""
    from agentic_debugger.agent.token_usage import TokenUsage
    from agentic_debugger.application.workstream import _token_usage_detail

    adapter, observer, result = _run_synthetic_live_session(
        tmp_path,
        transport=_ExactTotalPartialComponentsTransport(),
        limits=LiveRunLimits(max_model_requests=2, max_retries=0, max_directive_repairs=1),
        session_id="session-token-exact-total-001",
        journal_name="session-token-exact-total.jsonl",
    )
    assert result.stop_reason is not None

    # 1. Adapter produces exact total 270 with complete total coverage
    assert adapter.last_request_token_usage() == TokenUsage(
        input_tokens=100, output_tokens=20, total_tokens=270
    )
    coverage = adapter.last_request_token_coverage()
    assert coverage is not None
    assert not coverage.input_tokens
    assert not coverage.output_tokens
    assert coverage.total_tokens

    # 2. Controller emitted MODEL_REQUEST_COMPLETED with exact total 270
    live_events = observer.events()
    completions = [
        event
        for event in live_events
        if event.event_kind is SessionEventKind.MODEL_REQUEST_COMPLETED
    ]
    assert len(completions) == 1
    assert dict(completions[0].payload["token_usage"]) == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 270,
    }
    assert dict(completions[0].payload["token_usage_coverage"]) == {
        "input_tokens": False,
        "output_tokens": False,
        "total_tokens": True,
    }

    # Workstream detail from the completed event payload
    detail = _token_usage_detail(completions[0].payload)
    assert detail == "Input 100+ · Output 20+ · Total 270"

    # 3. Journal serialization/deserialization preserves the event payload
    read = read_session_journal(tmp_path / "session-token-exact-total.jsonl")
    journal_completions = [
        event
        for event in read.events
        if event.event_kind is SessionEventKind.MODEL_REQUEST_COMPLETED
    ]
    assert len(journal_completions) == 1
    assert journal_completions[0].to_mapping()["payload"] == completions[0].to_mapping()["payload"]

    # 4. Live and replayed views match identically
    identity = PresentationIdentity(
        task_id=TASK_ID,
        source_kind=SourceKind.CONFIGURED_MODEL,
        session_id="session-token-exact-total-001",
    )
    view = initial_session_view(identity)
    for event in read.events:
        view = reduce_event(view, event)

    usage = view.token_usage
    assert usage.requests_completed == 1
    assert usage.requests_with_usage == 1
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20
    assert usage.total_tokens == 270
    assert usage.requests_complete_input == 0
    assert usage.requests_complete_output == 0
    assert usage.requests_complete_total == 1
    assert usage.is_partial("input_tokens")
    assert usage.is_partial("output_tokens")
    assert not usage.is_partial("total_tokens")
    assert usage.total_complete

    # 5. UI widgets: header summary has no (partial); breakdown has + on partials
    assert session_tokens_summary(usage) == "Tokens 270"
    assert session_tokens_breakdown(usage) == "In 100+ · Out 20+"


