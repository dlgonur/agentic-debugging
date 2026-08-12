"""R5.6 phase-navigation unit tests: administrative closeout on a real
verifier-RESOLVED candidate.

The independent EvaluationVerifier is the correctness authority; once it
confirms RESOLVED for the accepted candidate the harness performs the
administrative PATCH->VALIDATE->DONE closeout instead of letting a further
greedy retry replace the resolved candidate (the r5.5 005 failure mode)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    ControllerSnapshot,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.events.schema import Observation

from experiments.debugger_interaction_v2_r5.adapter import ScriptedBridgeAdapter
from experiments.debugger_interaction_v2_r5.phase_navigation import (
    R5PhaseNavigationAdapter,
)


def _observation(name: str, payload: dict, status: str = "ok") -> Observation:
    return Observation.from_mapping({
        "observation_id": f"obs-{name}",
        "action_id": f"act-{name}",
        "run_id": "r5-test",
        "task_id": "r5-test",
        "name": name,
        "status": status,
        "payload": payload,
        "summary": "",
        "truncated": False,
    })


def _snapshot(state: ControllerState, last_obs=None) -> ControllerSnapshot:
    return ControllerSnapshot(
        "r5-test", "r5-test", state, 0,
        ControllerBudgetLimits(max_patch_attempts=4, max_test_runs=5, max_pdb_observations=8),
        ControllerBudgetState(), HypothesisLedger(),
        last_observation=last_obs,
    )


def _resolved_feedback_observation() -> Observation:
    return _observation(
        "apply_patch",
        {
            "applied": True,
            "changed_files": ["price.py"],
            "hunk_count": 1,
            "verifier_feedback": {
                "status": "COMPLETED",
                "outcome": "RESOLVED",
                "f2p_total": 1,
                "f2p_passed": 1,
                "p2p_total": 2,
                "p2p_passed": 2,
                "full_suite": "PASS",
                "syntax": True,
                "failures": [],
            },
        },
    )


class _Inner:
    """Records that the inner adapter was consulted."""

    def __init__(self) -> None:
        self.calls: list[ControllerState] = []

    def next_directive(self, snapshot: ControllerSnapshot):
        self.calls.append(snapshot.state)
        return ActionDirective(ActionName.APPLY_PATCH, {})


class TestVerifierResolvedCloseout:
    def test_patch_resolved_auto_closeout_patch_validate_done(self):
        inner = _Inner()
        adapter = R5PhaseNavigationAdapter(inner)
        # Reproduce -> Understand (admin nav)
        directive = adapter.next_directive(_snapshot(ControllerState.REPRODUCE, _observation("run_reproduction", {"failure_reproduced": True})))
        assert isinstance(directive, TransitionDirective)
        assert directive.target_state is ControllerState.UNDERSTAND
        # Understand -> RuntimeEvidence (admin nav)
        directive = adapter.next_directive(_snapshot(ControllerState.UNDERSTAND))
        assert isinstance(directive, TransitionDirective)
        assert directive.target_state is ControllerState.RUNTIME_EVIDENCE
        # PATCH with a verifier-RESOLVED apply_patch observation -> closeout
        directive = adapter.next_directive(_snapshot(ControllerState.PATCH, _resolved_feedback_observation()))
        assert isinstance(directive, TransitionDirective)
        assert directive.target_state is ControllerState.VALIDATE
        assert "verifier" in directive.reason
        # VALIDATE -> DONE (closeout complete)
        directive = adapter.next_directive(_snapshot(ControllerState.VALIDATE))
        assert isinstance(directive, TransitionDirective)
        assert directive.target_state is ControllerState.DONE
        # The model is never consulted again after closeout.
        assert inner.calls == []

    def test_unresolved_feedback_delegates_to_inner(self):
        inner = _Inner()
        adapter = R5PhaseNavigationAdapter(inner)
        adapter._admin_nav_done = True
        obs = _observation(
            "apply_patch",
            {"applied": True, "verifier_feedback": {"outcome": "REGRESSION"}},
        )
        adapter.next_directive(_snapshot(ControllerState.PATCH, obs))
        assert inner.calls == [ControllerState.PATCH]

    def test_no_feedback_delegates_to_inner(self):
        inner = _Inner()
        adapter = R5PhaseNavigationAdapter(inner)
        adapter._admin_nav_done = True
        adapter.next_directive(_snapshot(ControllerState.PATCH, _observation("apply_patch", {"applied": True})))
        assert inner.calls == [ControllerState.PATCH]
