"""Deterministic controller step/run result records.

This module owns the immutable controller result contracts:
:class:`ControllerStepResult` (one model-call step with action/
observation correlation, budget/ledger states, and stop semantics) and
:class:`ControllerRunResult` (the terminal run record), together with
the authoritative action/observation identity helpers they enforce.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentic_debugger.agent.controller_contracts import (
    ControllerError,
    ControllerInputError,
    ControllerInvariantError,
    ControllerRunConfig,
    ControllerStopReason,
    _copy_json,
    _copy_observation,
    _validate_action,
    _validate_budget_records,
    _validate_ledger_shape,
    _validate_transition_reason,
)
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ModelDirectiveKind
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ActionName, ToolDispatchReason
from agentic_debugger.events.schema import Action, Observation, ObservationStatus

def _action_id(index: int) -> str:
    return f"action-{index:09d}"


def _observation_id(index: int) -> str:
    return f"observation-{index:09d}"


@dataclass(frozen=True)
class ControllerStepResult:
    model_call_index: int
    state_before: ControllerState
    state_after: ControllerState
    directive_kind: ModelDirectiveKind | None
    action: Action | None
    observation: Observation | None
    transition_reason: str | None
    budget_before: ControllerBudgetState
    budget_after: ControllerBudgetState
    hypotheses_before: HypothesisLedger
    hypotheses_after: HypothesisLedger
    stop_reason: ControllerStopReason | None = None

    def __post_init__(self) -> None:
        if type(self.model_call_index) is not int or self.model_call_index < 0:
            raise ControllerInvariantError("invalid model_call_index")
        if type(self.state_before) is not ControllerState or type(self.state_after) is not ControllerState:
            raise ControllerInvariantError("invalid state")
        if self.directive_kind is not None and type(self.directive_kind) is not ModelDirectiveKind:
            raise ControllerInvariantError("invalid directive_kind")
        if self.stop_reason is not None and type(self.stop_reason) is not ControllerStopReason:
            raise ControllerInvariantError("invalid stop_reason")
        _validate_budget_records(self._limits_for_validation(), self.budget_before)
        _validate_budget_records(self._limits_for_validation(), self.budget_after)
        _validate_ledger_shape(self.hypotheses_before)
        _validate_ledger_shape(self.hypotheses_after)
        if self.transition_reason is not None:
            _validate_transition_reason(self.transition_reason)
        if self.action is not None:
            _validate_action(self.action)
            if self.action.run_id == "" or self.action.task_id == "":
                raise ControllerInvariantError("invalid action")
            if self.action.state is not self.state_before:
                raise ControllerInvariantError("invalid action state")
            if self.action.action_id != _action_id(self.model_call_index):
                raise ControllerInvariantError("invalid action id")
            if self.action.name not in {candidate.value for candidate in ActionName}:
                raise ControllerInvariantError("invalid action name")
        if self.observation is not None:
            if self.action is None or type(self.observation) is not Observation:
                raise ControllerInvariantError("invalid observation")
            try:
                observation_id = self.observation.observation_id
                action_id = self.observation.action_id
                observation_run_id = self.observation.run_id
                observation_task_id = self.observation.task_id
                observation_name = self.observation.name
                observation_status = self.observation.status
                observation_payload = self.observation.payload
                observation_summary = self.observation.summary
                observation_truncated = self.observation.truncated
            except Exception:
                raise ControllerInvariantError("invalid observation")
            if (
                type(observation_id) is not str
                or type(action_id) is not str
                or type(observation_run_id) is not str
                or type(observation_task_id) is not str
                or type(observation_name) is not str
                or type(observation_status) is not ObservationStatus
                or type(observation_payload) is not dict
                or type(observation_summary) is not str
                or type(observation_truncated) is not bool
            ):
                raise ControllerInvariantError("invalid observation")
            try:
                trusted_payload = _copy_json(observation_payload)
            except ControllerInputError:
                raise ControllerInvariantError("invalid observation")
            if type(trusted_payload) is not dict:
                raise ControllerInvariantError("invalid observation")
            if "dispatch_reason" not in trusted_payload or type(trusted_payload["dispatch_reason"]) is not str:
                raise ControllerInvariantError("invalid dispatch_reason")
            if not any(candidate.value == trusted_payload["dispatch_reason"] for candidate in ToolDispatchReason):
                raise ControllerInvariantError("invalid dispatch_reason")
            if not observation_id or not action_id or not observation_run_id or not observation_task_id or not observation_name:
                raise ControllerInvariantError("invalid observation")
            if observation_id != _observation_id(self.model_call_index):
                raise ControllerInvariantError("invalid observation id")
            if (
                action_id != self.action.action_id
                or observation_run_id != self.action.run_id
                or observation_task_id != self.action.task_id
                or observation_name != self.action.name
            ):
                raise ControllerInvariantError("invalid observation correlation")
            try:
                canonical_observation = Observation(
                    observation_id=observation_id,
                    action_id=action_id,
                    run_id=observation_run_id,
                    task_id=observation_task_id,
                    name=observation_name,
                    status=observation_status,
                    payload=trusted_payload,
                    summary=observation_summary,
                    truncated=observation_truncated,
                )
            except Exception:
                raise ControllerInvariantError("invalid observation")
            object.__setattr__(self, "observation", canonical_observation)
        if self.directive_kind is ModelDirectiveKind.ACTION:
            if self.transition_reason is not None:
                raise ControllerInvariantError("invalid action step")
            if self.action is None:
                if self.stop_reason not in {
                    ControllerStopReason.DIRECTIVE_REJECTED,
                    ControllerStopReason.BUDGET_EXHAUSTED,
                }:
                    raise ControllerInvariantError("invalid action step")
            elif self.observation is None and self.stop_reason is not ControllerStopReason.CONTROLLER_ERROR:
                raise ControllerInvariantError("invalid action observation")
        elif self.directive_kind is ModelDirectiveKind.TRANSITION:
            if self.action is not None or self.observation is not None or self.transition_reason is None:
                raise ControllerInvariantError("invalid transition step")
        elif self.directive_kind in {
            ModelDirectiveKind.ADD_HYPOTHESIS,
            ModelDirectiveKind.REVISE_HYPOTHESIS,
            ModelDirectiveKind.SET_HYPOTHESIS_STATUS,
        }:
            if self.action is not None or self.observation is not None or self.transition_reason is not None:
                raise ControllerInvariantError("invalid hypothesis step")
        elif self.directive_kind is None:
            if self.action is not None or self.observation is not None or self.transition_reason is not None or self.stop_reason is None:
                raise ControllerInvariantError("invalid failure step")
        if self.stop_reason is not None:
            if self.stop_reason is ControllerStopReason.DONE:
                if self.state_after is not ControllerState.DONE:
                    raise ControllerInvariantError("invalid stop_state")
            elif self.state_after is not ControllerState.FAILED:
                raise ControllerInvariantError("invalid stop_state")

    def _limits_for_validation(self) -> ControllerBudgetLimits:
        """Use the exact limits carried by the snapshot-independent state."""

        # Step records intentionally carry budget states only.  The policy
        # limits are checked by the controller before constructing a step.
        # These positive, unbounded checks are enough for the immutable record.
        return ControllerBudgetLimits(
            max_patch_attempts=max(self.budget_before.patch_attempts, self.budget_after.patch_attempts, 1),
            max_test_runs=max(self.budget_before.test_runs, self.budget_after.test_runs, 1),
            max_pdb_observations=max(self.budget_before.pdb_observations, self.budget_after.pdb_observations),
            max_source_observations=max(self.budget_before.source_observations, self.budget_after.source_observations, 1),
        )


@dataclass(frozen=True)
class ControllerRunResult:
    run_id: str
    task_id: str
    initial_state: ControllerState
    final_state: ControllerState
    stop_reason: ControllerStopReason
    model_calls: int
    steps: tuple[ControllerStepResult, ...]
    budget_state: ControllerBudgetState
    hypotheses: HypothesisLedger
    last_observation: Observation | None

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not self.run_id or type(self.task_id) is not str or not self.task_id:
            raise ControllerError("invalid identifiers")
        if type(self.initial_state) is not ControllerState or type(self.final_state) is not ControllerState:
            raise ControllerError("invalid state")
        if type(self.stop_reason) is not ControllerStopReason:
            raise ControllerError("invalid stop_reason")
        if type(self.model_calls) is not int or self.model_calls < 0:
            raise ControllerError("invalid model_calls")
        if type(self.steps) is not tuple or any(type(step) is not ControllerStepResult for step in self.steps):
            raise ControllerError("invalid steps")
        if self.model_calls < len(self.steps):
            raise ControllerError("invalid model_calls")
        if type(self.budget_state) is not ControllerBudgetState:
            raise ControllerError("invalid budget_state")
        if any(type(getattr(self.budget_state, name)) is not int or getattr(self.budget_state, name) < 0
               for name in ("patch_attempts", "test_runs", "pdb_observations", "source_observations")):
            raise ControllerError("invalid budget_state")
        try:
            _validate_ledger_shape(self.hypotheses)
        except ControllerInvariantError:
            raise ControllerError("invalid hypotheses")
        if self.last_observation is not None:
            try:
                _copy_observation(self.last_observation,
                                  run_id=self.run_id,
                                  task_id=self.task_id)
            except ControllerInputError:
                raise ControllerError("invalid last_observation")
        if self.final_state not in (ControllerState.DONE, ControllerState.FAILED):
            raise ControllerError("invalid final_state")
        if self.stop_reason is ControllerStopReason.DONE:
            if self.final_state is not ControllerState.DONE:
                raise ControllerError("invalid stop_state")
        elif self.final_state is not ControllerState.FAILED:
            raise ControllerError("invalid stop_state")
        for step in self.steps:
            if step.action is not None:
                if step.action.run_id != self.run_id or step.action.task_id != self.task_id:
                    raise ControllerError("invalid action correlation")
            if step.observation is not None:
                if step.observation.run_id != self.run_id or step.observation.task_id != self.task_id:
                    raise ControllerError("invalid observation correlation")
        if self.last_observation is not None and (
            self.last_observation.run_id != self.run_id
            or self.last_observation.task_id != self.task_id
        ):
            raise ControllerError("invalid last_observation correlation")
