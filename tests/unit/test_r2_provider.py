"""Unit tests for R2 session state provider — public get_target_status() based."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.debugger_interaction_v2_r2.adapter import (
    R2StageTracker,
    make_r2_session_state_provider,
)
from experiments.debugger_interaction_v2_r2.bridge import R2Stage, DebuggerLifecycle


def _context(pdb_session=None, started=False):
    ctx = Mock()
    ctx.pdb_session = pdb_session
    ctx.pdb_session_started = started
    return ctx


def _mock_session(state: str):
    sess = Mock()
    sess.get_target_status.return_value = {"state": state, "script": "recent_window.py"}
    return sess


class TestR2ProviderNotStarted:
    def test_no_session_no_started_is_not_started(self):
        tracker = R2StageTracker()
        ctx = _context(pdb_session=None, started=False)
        provider = make_r2_session_state_provider(ctx, lambda: tracker.stage)
        s = provider()
        assert s.lifecycle is DebuggerLifecycle.NOT_STARTED
        assert s.r2_stage is R2Stage.NOT_STARTED


class TestR2ProviderPausedDelegatesToStage:
    def test_paused_delegates_to_stage_needs_stack(self):
        tracker = R2StageTracker()
        # advance to PAUSED_NEEDS_STACK via a synthetic break observation
        from agentic_debugger.events.schema import Observation, ObservationStatus
        obs = Observation(
            observation_id="o1", action_id="a1", run_id="r", task_id="t",
            name="start_pdb_session", status=ObservationStatus.OK,
            payload={"state": "paused", "script": "recent_window.py", "line": 2, "function": "recent_window"},
            summary="x", truncated=False,
        )
        tracker.update_from_observation(obs)
        assert tracker.stage is R2Stage.PAUSED_NEEDS_STACK
        ctx = _context(pdb_session=_mock_session("paused"), started=True)
        provider = make_r2_session_state_provider(ctx, lambda: tracker.stage)
        s = provider()
        assert s.lifecycle is DebuggerLifecycle.PAUSED
        assert s.r2_stage is R2Stage.PAUSED_NEEDS_STACK


class TestR2ProviderExitedIsConsumed:
    def test_exited_session_is_consumed_not_paused(self):
        tracker = R2StageTracker()
        ctx = _context(pdb_session=_mock_session("exited"), started=True)
        provider = make_r2_session_state_provider(ctx, lambda: tracker.stage)
        s = provider()
        assert s.lifecycle is DebuggerLifecycle.CONSUMED_OR_ENDED
        assert s.r2_stage is R2Stage.CONSUMED_OR_ENDED

    def test_failed_session_is_consumed(self):
        tracker = R2StageTracker()
        ctx = _context(pdb_session=_mock_session("failed"), started=True)
        provider = make_r2_session_state_provider(ctx, lambda: tracker.stage)
        s = provider()
        assert s.lifecycle is DebuggerLifecycle.CONSUMED_OR_ENDED

    def test_terminated_session_is_consumed(self):
        tracker = R2StageTracker()
        ctx = _context(pdb_session=_mock_session("terminated"), started=True)
        provider = make_r2_session_state_provider(ctx, lambda: tracker.stage)
        s = provider()
        assert s.lifecycle is DebuggerLifecycle.CONSUMED_OR_ENDED


class TestR2ProviderFailClosed:
    def test_status_failure_is_consumed_fail_closed(self):
        tracker = R2StageTracker()
        sess = Mock()
        sess.get_target_status.side_effect = RuntimeError("status failed")
        ctx = _context(pdb_session=sess, started=True)
        provider = make_r2_session_state_provider(ctx, lambda: tracker.stage)
        s = provider()
        assert s.lifecycle is DebuggerLifecycle.CONSUMED_OR_ENDED
        assert s.status_diagnostic is not None

    def test_no_session_but_started_is_consumed(self):
        tracker = R2StageTracker()
        ctx = _context(pdb_session=None, started=True)
        provider = make_r2_session_state_provider(ctx, lambda: tracker.stage)
        s = provider()
        assert s.lifecycle is DebuggerLifecycle.CONSUMED_OR_ENDED
