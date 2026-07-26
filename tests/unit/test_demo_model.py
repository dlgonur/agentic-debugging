"""Branch coverage for the deterministic offline model's phase machine.

The model is driven here by a miniature controller loop that enforces the same
invariants the real controller enforces (state/action allowlist, transition
graph, budget consumption, hypothesis ledger) while allowing every tool
observation to be scripted.  That makes each failure branch reachable without
paying for a real subprocess run.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import pytest

from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
    PdbPolicy,
    budget_kind_for_action,
    is_action_allowed,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    AddHypothesisDirective,
    ControllerSnapshot,
    ModelAdapterError,
    ReviseHypothesisDirective,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState, is_transition_allowed
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.model import DEMO_MODEL_NAME, DemoPolicyModel
from agentic_debugger.events.schema import Observation, ObservationStatus

TASK_ID = "curated-none-handling-001"
RUN_ID = "demo-run"
LIMITS = ControllerBudgetLimits(
    max_patch_attempts=2, max_test_runs=5, max_pdb_observations=8
)

#: Observations a fully successful integrated run would produce.
HAPPY_PATH: dict[str, tuple[ObservationStatus, dict[str, Any]]] = {
    "run_reproduction": (ObservationStatus.OK, {"failure_reproduced": True, "passed": False}),
    "find_function": (ObservationStatus.OK, {"start_line": 1}),
    "get_source_window": (ObservationStatus.OK, {"start_line": 1}),
    "express_root_cause_hypothesis": (ObservationStatus.OK, {"recorded": True}),
    "start_pdb_session": (ObservationStatus.OK, {"state": "paused"}),
    "get_stack_summary": (ObservationStatus.OK, {"pause_generation": 4}),
    "get_frame_locals": (ObservationStatus.OK, {"locals": []}),
    "safe_eval_expression": (ObservationStatus.OK, {"value": None}),
    "stop_pdb_session": (ObservationStatus.OK, {"stopped": True}),
    "apply_patch": (ObservationStatus.OK, {"applied": True}),
    "syntax_check": (ObservationStatus.OK, {"all_passed": True}),
    "run_regression_tests": (ObservationStatus.OK, {"all_passed": True}),
    "classify_outcome": (ObservationStatus.OK, {"outcome": "RESOLVED"}),
}

PATCH = "--- a/display_name.py\n+++ b/display_name.py\n@@ -1 +1 @@\n-a\n+b\n"


def _observation(name: str, status: ObservationStatus, payload: dict[str, Any]) -> Observation:
    return Observation(
        observation_id="observation-000000000",
        action_id="action-000000000",
        run_id=RUN_ID,
        task_id=TASK_ID,
        name=name,
        status=status,
        payload=dict(payload),
        summary="scripted observation",
        truncated=False,
    )


class MiniController:
    """A minimal controller that enforces the real policy invariants."""

    def __init__(
        self,
        model: DemoPolicyModel,
        responses: Optional[dict[str, tuple[ObservationStatus, dict[str, Any]]]] = None,
        *,
        post_patch: Optional[tuple[ObservationStatus, dict[str, Any]]] = None,
        dynamic: Optional[Callable[[str, int], tuple[ObservationStatus, dict[str, Any]]]] = None,
    ) -> None:
        self._model = model
        self._responses = {**HAPPY_PATH, **(responses or {})}
        self._post_patch = post_patch or (
            ObservationStatus.OK,
            {"passed": True, "failure_reproduced": False},
        )
        self._dynamic = dynamic
        self.state = ControllerState.REPRODUCE
        self.budget = ControllerBudgetState()
        self.hypotheses = HypothesisLedger()
        self.actions: list[str] = []
        self.transitions: list[tuple[str, str]] = []
        self.directives: list[Any] = []
        self._patched = False
        self._seen: dict[str, int] = {}

    def _snapshot(self, observation: Optional[Observation]) -> ControllerSnapshot:
        return ControllerSnapshot(
            RUN_ID,
            TASK_ID,
            self.state,
            len(self.directives),
            LIMITS,
            self.budget,
            self.hypotheses,
            observation,
        )

    def _respond(self, name: str) -> Observation:
        index = self._seen.get(name, 0)
        self._seen[name] = index + 1
        if self._dynamic is not None:
            override = self._dynamic(name, index)
            if override is not None:
                return _observation(name, *override)
        if name == "run_reproduction" and self._patched:
            return _observation(name, *self._post_patch)
        return _observation(name, *self._responses[name])

    def run(self, max_steps: int = 40) -> ControllerState:
        observation: Optional[Observation] = None
        while self.state not in (ControllerState.DONE, ControllerState.FAILED):
            if len(self.directives) >= max_steps:
                raise AssertionError("offline model did not terminate")
            directive = self._model.next_directive(self._snapshot(observation))
            self.directives.append(directive)
            if isinstance(directive, ActionDirective):
                assert is_action_allowed(self.state, directive.name), (
                    f"{directive.name.value} is not allowed in {self.state.value}"
                )
                kind = budget_kind_for_action(directive.name)
                if kind is not None:
                    assert self.budget.can_consume(LIMITS, kind), f"budget exhausted for {kind}"
                    self.budget = self.budget.consume(LIMITS, kind)
                self.actions.append(directive.name.value)
                if directive.name is ActionName.APPLY_PATCH:
                    self._patched = True
                observation = self._respond(directive.name.value)
            elif isinstance(directive, TransitionDirective):
                assert is_transition_allowed(self.state, directive.target_state), (
                    f"illegal transition {self.state.value} -> {directive.target_state.value}"
                )
                self.transitions.append((self.state.value, directive.target_state.value))
                self.state = directive.target_state
            elif isinstance(directive, AddHypothesisDirective):
                self.hypotheses = self.hypotheses.add(
                    LIMITS,
                    hypothesis_id=directive.hypothesis_id,
                    statement=directive.statement,
                    confidence=directive.confidence,
                    evidence_refs=directive.evidence_refs,
                    requires_runtime_evidence=directive.requires_runtime_evidence,
                )
            elif isinstance(directive, ReviseHypothesisDirective):
                self.hypotheses = self.hypotheses.revise(
                    directive.hypothesis_id,
                    statement=directive.statement,
                    confidence=directive.confidence,
                    evidence_refs=directive.evidence_refs,
                    requires_runtime_evidence=directive.requires_runtime_evidence,
                )
            else:  # pragma: no cover - the model emits no other directive kind
                raise AssertionError(f"unexpected directive {directive!r}")
        return self.state


def _model(policy: PdbPolicy = PdbPolicy.DISABLED) -> DemoPolicyModel:
    return DemoPolicyModel(scenario=scenario_for(TASK_ID), patch=PATCH, pdb_policy=policy)


def _fail(name: str, status: ObservationStatus = ObservationStatus.ERROR) -> dict[str, Any]:
    return {name: (status, {})}


class TestConstruction:
    def test_model_name_is_not_a_provider_identifier(self) -> None:
        assert DemoPolicyModel.model_name == DEMO_MODEL_NAME
        assert "claude" not in DEMO_MODEL_NAME and "gpt" not in DEMO_MODEL_NAME

    def test_invalid_construction_is_rejected(self) -> None:
        scenario = scenario_for(TASK_ID)
        with pytest.raises(ModelAdapterError):
            DemoPolicyModel(scenario=scenario, patch=PATCH, pdb_policy="disabled")  # type: ignore[arg-type]
        with pytest.raises(ModelAdapterError):
            DemoPolicyModel(scenario=scenario, patch="", pdb_policy=PdbPolicy.DISABLED)


class TestStaticHappyPath:
    def test_static_run_reaches_done_without_touching_the_debugger(self) -> None:
        model = _model()
        controller = MiniController(model)
        assert controller.run() is ControllerState.DONE
        assert controller.actions == [
            "run_reproduction",
            "find_function",
            "get_source_window",
            "express_root_cause_hypothesis",
            "apply_patch",
            "syntax_check",
            "run_reproduction",
            "run_regression_tests",
            "classify_outcome",
        ]
        assert model.abort_reason is None
        assert model.failure_reproduced is True
        assert model.runtime_evidence_collected is False
        assert [item["reason"] for item in
                [record.to_mapping() for record in model.gate_records]] == ["policy_disabled"]

    def test_static_run_stays_inside_every_task_budget(self) -> None:
        controller = MiniController(_model())
        controller.run()
        assert controller.budget.test_runs == 3
        assert controller.budget.patch_attempts == 1
        assert controller.budget.source_observations == 2
        assert controller.budget.pdb_observations == 0


class TestPdbHappyPath:
    def test_gated_run_collects_evidence_and_reaches_done(self) -> None:
        model = _model(PdbPolicy.ON_UNCERTAINTY)
        controller = MiniController(model)
        assert controller.run() is ControllerState.DONE
        assert controller.actions.count("safe_eval_expression") == len(
            scenario_for(TASK_ID).runtime_probe.inspect_expressions
        )
        assert controller.actions.count("stop_pdb_session") == 1
        assert model.runtime_evidence_collected is True
        assert controller.budget.pdb_observations == 3
        gate = model.gate_records[0].to_mapping()
        assert gate["allowed"] is True and gate["reason"] == "allowed"
        assert gate["active_hypothesis_confidence"] == "low"
        assert gate["active_hypothesis_requires_runtime_evidence"] is True

    def test_revision_does_not_raise_confidence_or_claim_confirmation(self) -> None:
        model = _model(PdbPolicy.ON_UNCERTAINTY)
        controller = MiniController(model)
        controller.run()
        revisions = [
            item for item in controller.directives if isinstance(item, ReviseHypothesisDirective)
        ]
        assert len(revisions) == 1
        revision = revisions[0]
        assert revision.confidence.value == "low"
        assert revision.requires_runtime_evidence is False
        assert revision.statement == scenario_for(TASK_ID).root_cause_statement
        assert set(revision.evidence_refs) >= {
            "observation:get_stack_summary",
            "observation:get_frame_locals",
            "observation:safe_eval_expression",
        }
        reasons = " ".join(
            item.reason for item in controller.directives if isinstance(item, TransitionDirective)
        ).lower()
        for forbidden in ("resolved the remaining uncertainty", "confirmed", "proved"):
            assert forbidden not in reasons

    def test_dynamic_pause_generation_is_read_from_the_stack_observation(self) -> None:
        model = _model(PdbPolicy.ON_UNCERTAINTY)
        controller = MiniController(model)
        controller.run()
        inspections = [
            item
            for item in controller.directives
            if isinstance(item, ActionDirective)
            and item.name
            in (ActionName.GET_FRAME_LOCALS, ActionName.SAFE_EVAL_EXPRESSION)
        ]
        assert inspections
        assert all(item.arguments["pause_generation"] == 4 for item in inspections)


class TestAbortBranches:
    def test_unreproduced_failure_stops_before_any_patch(self) -> None:
        model = _model()
        controller = MiniController(
            model,
            {"run_reproduction": (ObservationStatus.OK, {"failure_reproduced": False, "passed": True})},
        )
        assert controller.run() is ControllerState.FAILED
        assert model.abort_reason == "failure_not_reproduced"
        assert "apply_patch" not in controller.actions
        assert model.gate_records == []

    def test_reproduction_tool_failure_stops_the_run(self) -> None:
        model = _model()
        controller = MiniController(model, _fail("run_reproduction", ObservationStatus.TIMEOUT))
        assert controller.run() is ControllerState.FAILED
        assert model.abort_reason == "failure_not_reproduced"

    def test_missing_symbol_stops_the_run(self) -> None:
        model = _model()
        controller = MiniController(model, _fail("find_function"))
        assert controller.run() is ControllerState.FAILED
        assert model.abort_reason == "symbol_not_located"
        assert "apply_patch" not in controller.actions

    def test_failed_source_window_is_not_cited_as_evidence(self) -> None:
        model = _model()
        controller = MiniController(model, _fail("get_source_window"))
        controller.run()
        added = [
            item for item in controller.directives if isinstance(item, AddHypothesisDirective)
        ]
        assert added[0].evidence_refs == ("observation:find_function",)

    def test_rejected_patch_stops_before_validation(self) -> None:
        model = _model()
        controller = MiniController(
            model, {"apply_patch": (ObservationStatus.REJECTED, {})}
        )
        assert controller.run() is ControllerState.FAILED
        assert model.abort_reason == "patch_not_applied"
        assert "syntax_check" not in controller.actions

    def test_patch_reported_as_not_applied_stops_the_run(self) -> None:
        model = _model()
        controller = MiniController(model, {"apply_patch": (ObservationStatus.OK, {"applied": False})})
        assert controller.run() is ControllerState.FAILED
        assert model.abort_reason == "patch_not_applied"

    def test_failed_syntax_check_stops_the_run(self) -> None:
        model = _model()
        controller = MiniController(
            model, {"syntax_check": (ObservationStatus.OK, {"all_passed": False})}
        )
        assert controller.run() is ControllerState.FAILED
        assert model.abort_reason == "syntax_check_failed"
        assert "run_regression_tests" not in controller.actions

    def test_post_patch_reproduction_failure_stops_the_run(self) -> None:
        model = _model()
        controller = MiniController(
            model, post_patch=(ObservationStatus.ERROR, {})
        )
        assert controller.run() is ControllerState.FAILED
        assert model.abort_reason == "post_patch_reproduction_failed"

    def test_regression_execution_failure_stops_the_run(self) -> None:
        model = _model()
        controller = MiniController(model, _fail("run_regression_tests"))
        assert controller.run() is ControllerState.FAILED
        assert model.abort_reason == "regression_execution_failed"

    def test_non_resolved_outcome_is_reported_as_a_failure(self) -> None:
        model = _model()
        controller = MiniController(
            model, {"classify_outcome": (ObservationStatus.OK, {"outcome": "BREAKING_RESOLVED"})}
        )
        assert controller.run() is ControllerState.FAILED
        assert model.abort_reason == "controller_outcome_BREAKING_RESOLVED"
        reason = controller.directives[-1].reason
        assert "BREAKING_RESOLVED" in reason

    def test_unclassifiable_outcome_is_reported_as_a_failure(self) -> None:
        model = _model()
        controller = MiniController(model, _fail("classify_outcome"))
        assert controller.run() is ControllerState.FAILED
        assert model.abort_reason == "controller_outcome_unclassified"


class TestRuntimeAbandonment:
    @pytest.mark.parametrize(
        "failing,expected_actions",
        [
            ("start_pdb_session", ["start_pdb_session", "stop_pdb_session"]),
            (
                "get_stack_summary",
                ["start_pdb_session", "get_stack_summary", "stop_pdb_session"],
            ),
            (
                "get_frame_locals",
                [
                    "start_pdb_session",
                    "get_stack_summary",
                    "get_frame_locals",
                    "stop_pdb_session",
                ],
            ),
        ],
    )
    def test_a_failed_debugger_step_always_stops_the_session_and_continues(
        self, failing: str, expected_actions: list[str]
    ) -> None:
        model = _model(PdbPolicy.ON_UNCERTAINTY)
        controller = MiniController(model, _fail(failing))
        assert controller.run() is ControllerState.DONE
        pdb_actions = [
            name
            for name in controller.actions
            if name.startswith(("start_pdb", "get_stack", "get_frame", "safe_eval", "stop_pdb"))
        ]
        assert pdb_actions == expected_actions
        assert model.runtime_evidence_collected is False
        assert "apply_patch" in controller.actions
        assert ("RuntimeEvidence", "Understand") in controller.transitions

    def test_a_failed_expression_abandons_the_remaining_expressions(self) -> None:
        model = _model(PdbPolicy.ON_UNCERTAINTY)
        expressions = len(scenario_for(TASK_ID).runtime_probe.inspect_expressions)
        controller = MiniController(model, _fail("safe_eval_expression"))
        assert controller.run() is ControllerState.DONE
        assert controller.actions.count("safe_eval_expression") == 1
        assert expressions >= 1
        assert model.runtime_evidence_collected is False
        assert controller.actions.count("stop_pdb_session") == 1

    def test_abandoned_runtime_evidence_is_not_cited_by_the_hypothesis(self) -> None:
        model = _model(PdbPolicy.ON_UNCERTAINTY)
        controller = MiniController(model, _fail("start_pdb_session"))
        controller.run()
        assert not any(
            isinstance(item, ReviseHypothesisDirective) for item in controller.directives
        )


class TestGateBehaviour:
    def test_exhausted_pdb_budget_denies_access_even_under_the_gated_policy(self) -> None:
        model = _model(PdbPolicy.ON_UNCERTAINTY)
        controller = MiniController(model)
        controller.budget = ControllerBudgetState(pdb_observations=LIMITS.max_pdb_observations)
        assert controller.run() is ControllerState.DONE
        assert model.gate_records[0].reason == "budget_exhausted"
        assert model.gate_records[0].allowed is False
        assert not any(name.startswith("start_pdb") for name in controller.actions)

    def test_the_gate_receives_the_live_controller_state(self) -> None:
        model = _model(PdbPolicy.ON_UNCERTAINTY)
        controller = MiniController(model)
        controller.run()
        # A wrong source_state would surface as INVALID_SOURCE_STATE.
        assert model.gate_records[0].reason == "allowed"
