"""Unit tests for S1 evidence/telemetry retention.

These tests prove:
- raw_response_text is present in telemetry even when parsing fails;
- raw_response_text is NOT_AVAILABLE when transport fails (not fabricated);
- missing usage data is NOT_RECORDED, never 0 or null;
- post_debug_diagnosis records model-authored text bound to model_call_index;
- observation provenance binding (prior_observation_id, rendered_observation_sha256).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import (
    ControllerSnapshot,
    ModelAdapterError,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.events.schema import Observation, ObservationStatus

from experiments.debugger_interaction_v2.adapter import (
    DebuggerBridgeAdapter,
    NOT_AVAILABLE,
    NOT_RECORDED,
)
from experiments.debugger_interaction_v2.transport import (
    FakeTransport,
    FailingTransport,
)
from experiments.debugger_interaction_v2.bridge import BridgeParseError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    state: ControllerState = ControllerState.RUNTIME_EVIDENCE,
    model_call_index: int = 0,
    last_observation: Observation | None = None,
) -> ControllerSnapshot:
    return ControllerSnapshot(
        run_id="s1-test",
        task_id="curated-off-by-one-002",
        state=state,
        model_call_index=model_call_index,
        budget_limits=ControllerBudgetLimits(
            max_patch_attempts=2,
            max_test_runs=5,
            max_pdb_observations=8,
        ),
        budget_state=ControllerBudgetState(),
        hypotheses=HypothesisLedger(),
        last_observation=last_observation,
    )


def _make_stack_observation(
    observation_id: str = "obs-001",
    action_id: str = "act-001",
    pause_generation: int = 2,
) -> Observation:
    return Observation(
        observation_id=observation_id,
        action_id=action_id,
        run_id="s1-test",
        task_id="curated-off-by-one-002",
        name="get_stack_summary",
        status=ObservationStatus.OK,
        payload={
            "state": "paused",
            "script": "recent_window.py",
            "pause_generation": pause_generation,
            "frames": [
                {
                    "frame_id": 0,
                    "script": "recent_window.py",
                    "line": 9,
                    "function": "recent_window",
                    "is_current": True,
                },
            ],
            "total_frames": 1,
            "truncated": False,
        },
        summary="bounded stack summary collected",
        truncated=False,
    )


# ---------------------------------------------------------------------------
# Raw text retention on parse failure
# ---------------------------------------------------------------------------


class TestRawTextRetention:
    """Raw response text is ALWAYS retained when the transport produced text,
    even when parsing fails."""

    def test_raw_text_retained_on_parse_failure(self):
        """When the model produces unparseable text, the raw text is in telemetry."""
        transport = FakeTransport(("this is not a valid command",))
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=0,  # don't retry — we want to see the rejection
        )
        snapshot = _make_snapshot()
        with pytest.raises(ModelAdapterError):
            adapter.next_directive(snapshot)

        telemetry = adapter.telemetry
        assert len(telemetry) == 1
        record = telemetry[0]
        # The raw text must be present, even though parsing failed.
        assert record["raw_response_text"] == "this is not a valid command"
        assert record["raw_response_status"] == "decoded"
        assert record["parse_result"]["status"] == "rejected"
        # The rejection category must be recorded.
        assert record["parse_result"]["rejection_category"] is not None

    def test_raw_text_retained_on_second_parse_failure_with_retry(self):
        """With retries, both attempts' raw text is retained."""
        transport = FakeTransport(("bad1", "bad2"))
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=1,
        )
        snapshot = _make_snapshot()
        with pytest.raises(ModelAdapterError):
            adapter.next_directive(snapshot)

        telemetry = adapter.telemetry
        assert len(telemetry) == 2
        assert telemetry[0]["raw_response_text"] == "bad1"
        assert telemetry[1]["raw_response_text"] == "bad2"
        assert telemetry[0]["parse_result"]["status"] == "rejected"
        assert telemetry[1]["parse_result"]["status"] == "rejected"

    def test_raw_text_not_available_on_transport_failure(self):
        """When the transport fails before producing text, raw_response_text
        is NOT_AVAILABLE (not fabricated)."""
        transport = FailingTransport("generation_error")
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=0,
        )
        snapshot = _make_snapshot()
        with pytest.raises(ModelAdapterError):
            adapter.next_directive(snapshot)

        telemetry = adapter.telemetry
        assert len(telemetry) == 1
        record = telemetry[0]
        assert record["raw_response_text"] == NOT_AVAILABLE
        assert record["raw_response_status"] == "transport_failure"
        assert record["transport_error_category"] is not None
        assert record["parse_result"]["status"] == "not_attempted"

    def test_raw_text_retained_on_success(self):
        """When parsing succeeds, the raw text is still retained."""
        transport = FakeTransport(("break 9",))
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=0,
        )
        snapshot = _make_snapshot()
        adapter.next_directive(snapshot)

        telemetry = adapter.telemetry
        assert len(telemetry) == 1
        assert telemetry[0]["raw_response_text"] == "break 9"
        assert telemetry[0]["parse_result"]["status"] == "accepted"


# ---------------------------------------------------------------------------
# NOT_RECORDED for missing usage
# ---------------------------------------------------------------------------


class TestMissingUsage:
    """Missing usage data is NOT_RECORDED, never 0 or null."""

    def test_usage_present_from_fake_transport(self):
        """When the transport provides usage, it is recorded."""
        transport = FakeTransport(("break 9",))
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=0,
        )
        snapshot = _make_snapshot()
        adapter.next_directive(snapshot)

        record = adapter.telemetry[0]
        assert record["usage"]["prompt_tokens"] == 100  # from FakeTransport
        assert record["usage"]["completion_tokens"] is not None
        assert record["usage"]["provider_reported"] is True

    def test_usage_not_recorded_on_transport_failure(self):
        """When the transport fails (no usage), usage is NOT_RECORDED."""
        transport = FailingTransport()
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=0,
        )
        snapshot = _make_snapshot()
        with pytest.raises(ModelAdapterError):
            adapter.next_directive(snapshot)

        record = adapter.telemetry[0]
        assert record["usage"]["prompt_tokens"] == NOT_RECORDED
        assert record["usage"]["completion_tokens"] == NOT_RECORDED
        assert record["usage"]["total_tokens"] == NOT_RECORDED
        assert record["usage"]["provider_reported"] is False

    def test_not_recorded_is_string_not_zero(self):
        """NOT_RECORDED is a string, not 0 or None."""
        assert NOT_RECORDED == "NOT_RECORDED"
        assert NOT_RECORDED != 0
        assert NOT_RECORDED is not None

    def test_not_available_is_distinct_from_not_recorded(self):
        """NOT_AVAILABLE (transport failure) is distinct from NOT_RECORDED
        (usage missing)."""
        assert NOT_AVAILABLE == "NOT_AVAILABLE"
        assert NOT_AVAILABLE != NOT_RECORDED


# ---------------------------------------------------------------------------
# Post-debug diagnosis evidence
# ---------------------------------------------------------------------------


class TestPostDebugDiagnosis:
    """post_debug_diagnosis records model-authored text bound to model_call_index."""

    def test_diagnosis_recorded(self):
        """When the model emits 'diagnosis <text>', it is recorded."""
        transport = FakeTransport(("diagnosis the bug is an off-by-one",))
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=0,
        )
        snapshot = _make_snapshot(state=ControllerState.RUNTIME_EVIDENCE)
        adapter.next_directive(snapshot)

        diagnoses = adapter.post_debug_diagnoses
        assert len(diagnoses) == 1
        assert diagnoses[0]["text"] == "the bug is an off-by-one"
        assert diagnoses[0]["model_call_index"] == 0
        assert diagnoses[0]["controller_state"] == "RuntimeEvidence"
        assert "provenance" in diagnoses[0]
        assert "model-authored" in diagnoses[0]["provenance"]

    def test_no_diagnosis_when_not_emitted(self):
        """No diagnosis is recorded when the model doesn't emit one."""
        transport = FakeTransport(("break 9",))
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=0,
        )
        snapshot = _make_snapshot(state=ControllerState.RUNTIME_EVIDENCE)
        adapter.next_directive(snapshot)

        assert len(adapter.post_debug_diagnoses) == 0

    def test_diagnosis_text_is_model_authored(self):
        """The diagnosis text comes verbatim from the model, not fabricated."""
        transport = FakeTransport(("diagnosis my exact diagnosis text here",))
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=0,
        )
        snapshot = _make_snapshot(state=ControllerState.RUNTIME_EVIDENCE)
        adapter.next_directive(snapshot)

        assert adapter.post_debug_diagnoses[0]["text"] == "my exact diagnosis text here"

    def test_diagnosis_bound_to_model_call_index(self):
        """The diagnosis is bound to the exact model_call_index that produced it."""
        transport = FakeTransport(("break 9", "diagnosis after debugging"))
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=0,
        )
        # First call (index 0): break 9
        adapter.next_directive(_make_snapshot(model_call_index=0))
        # Second call (index 1): diagnosis
        adapter.next_directive(_make_snapshot(model_call_index=1))

        diagnoses = adapter.post_debug_diagnoses
        assert len(diagnoses) == 1
        assert diagnoses[0]["model_call_index"] == 1


# ---------------------------------------------------------------------------
# Observation provenance binding
# ---------------------------------------------------------------------------


class TestProvenanceBinding:
    """The adapter binds the exact real PDB observation to the next model request."""

    def test_provenance_fields_populated(self):
        """When last_observation exists, provenance fields are populated."""
        obs = _make_stack_observation(observation_id="obs-abc")
        transport = FakeTransport(("locals",))
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=0,
        )
        snapshot = _make_snapshot(
            state=ControllerState.RUNTIME_EVIDENCE,
            last_observation=obs,
        )
        adapter.next_directive(snapshot)

        record = adapter.telemetry[0]
        assert record["provenance"]["prior_observation_id"] == "obs-abc"
        assert record["provenance"]["prior_observation_sha256"] is not None
        assert record["provenance"]["rendered_observation_sha256"] is not None

    def test_provenance_null_when_no_observation(self):
        """When there is no last_observation, provenance fields are null."""
        transport = FakeTransport(("break 9",))
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=0,
        )
        snapshot = _make_snapshot(last_observation=None)
        adapter.next_directive(snapshot)

        record = adapter.telemetry[0]
        assert record["provenance"]["prior_observation_id"] is None
        assert record["provenance"]["prior_observation_sha256"] is None
        assert record["provenance"]["rendered_observation_sha256"] is None

    def test_rendered_observation_hash_changes_with_observation(self):
        """Different observations produce different rendered_observation_sha256."""
        obs1 = _make_stack_observation(observation_id="obs-1", pause_generation=1)
        obs2 = _make_stack_observation(observation_id="obs-2", pause_generation=2)

        transport = FakeTransport(("locals", "locals"))
        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name="test",
            task_description="test task",
            max_retries=0,
        )
        adapter.next_directive(_make_snapshot(last_observation=obs1))
        adapter.next_directive(_make_snapshot(last_observation=obs2, model_call_index=1))

        hash1 = adapter.telemetry[0]["provenance"]["rendered_observation_sha256"]
        hash2 = adapter.telemetry[1]["provenance"]["rendered_observation_sha256"]
        assert hash1 != hash2  # different observations -> different renderings