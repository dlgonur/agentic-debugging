"""Directive schemas and action contracts by controller state.

This module owns the advertised contract layer: the rejection
constructor, per-state directive-kind schemas, the effective action
contracts derived from the tool registry (the single source of truth
for enum-constrained arguments), and legal transition targets.  The
parser and adapter consume these; they never duplicate them."""

from __future__ import annotations

from typing import Any, Mapping
from agentic_debugger.agent.controller_policy import (
    ActionName, BudgetKind, allowed_actions_for_state, budget_kind_for_action,
)
from agentic_debugger.agent.state_machine import ControllerState, TRANSITION_GRAPH
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.agent.tool_registry import MAX_TOOL_ARGUMENT_BYTES, ToolRegistry, _detach_json_dict
from agentic_debugger.demo.tools import legal_reproduction_phases

from agentic_debugger.evaluation.live_contracts import DirectiveRejectionCategory, LIVE_DIRECTIVE_SCHEMA, LiveConfigurationError, LiveModelAdapterError


def _rejected(category: "DirectiveRejectionCategory", detail: str = "", *, stage: str | None = None, reason_code: str | None = None, content: str | None = None) -> LiveModelAdapterError:
    return LiveModelAdapterError("invalid model directive", category=category, detail=detail, stage=stage, reason_code=reason_code, content=content, directive_rejection=True)

def _require_field(value: Mapping[str, Any], key: str) -> Any:
    try:
        return value[key]
    except (KeyError, TypeError):
        raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, f"missing '{key}'") from None

_PDB_ACTIONS = frozenset({
    ActionName.GET_FAILURE_TRACE,
    ActionName.START_PDB_SESSION,
    ActionName.GET_STACK_SUMMARY,
    ActionName.GET_FRAME,
    ActionName.GET_FRAME_LOCALS,
    ActionName.SAFE_EVAL_EXPRESSION,
    ActionName.INSPECT_CALLER_FRAME,
    ActionName.CONTINUE_PDB_SESSION,
    ActionName.STEP_PDB_SESSION,
    ActionName.NEXT_PDB_SESSION,
    ActionName.STOP_PDB_SESSION,
})
_SESSION_ACTIONS = frozenset({
    ActionName.GET_STACK_SUMMARY,
    ActionName.GET_FRAME,
    ActionName.GET_FRAME_LOCALS,
    ActionName.SAFE_EVAL_EXPRESSION,
    ActionName.INSPECT_CALLER_FRAME,
    ActionName.CONTINUE_PDB_SESSION,
    ActionName.STEP_PDB_SESSION,
    ActionName.NEXT_PDB_SESSION,
    ActionName.STOP_PDB_SESSION,
})


def _directive_schema_for_state(state: ControllerState) -> dict[str, dict[str, Any]]:
    """Expose only directive kinds the deterministic controller can apply."""

    kinds = {"action", "transition"}
    if state is ControllerState.UNDERSTAND:
        kinds.update({"add_hypothesis", "revise_hypothesis", "set_hypothesis_status"})
    elif state is ControllerState.RUNTIME_EVIDENCE:
        kinds.update({"revise_hypothesis", "set_hypothesis_status"})
    order = (
        "action",
        "transition",
        "add_hypothesis",
        "revise_hypothesis",
        "set_hypothesis_status",
    )
    return {
        kind: _detach_json_dict(
            LIVE_DIRECTIVE_SCHEMA[kind],
            max_bytes=MAX_TOOL_ARGUMENT_BYTES,
        )
        for kind in order
        if kind in kinds
    }


def _action_contracts_for_state(
    state: ControllerState,
    *,
    registry: ToolRegistry,
    policy: DemoPolicy | None = None,
    session_active: bool = False,
    pdb_available: bool = True,
    pdb_observations_remaining: int | None = None,
    post_patch_f2p_collected: bool = False,
    regression_collected: bool = False,
    patch_allowed: bool = True,
    diagnosis_allowed: bool = True,
    failure_trace_allowed: bool = True,
) -> dict[str, dict[str, Any]]:
    """Return the effective contract derived from the supplied registry."""

    if type(registry) is not ToolRegistry:
        raise LiveConfigurationError("live tool registry is required")
    contracts = registry.argument_contracts()
    registered = set(registry.names())
    effective = set(allowed_actions_for_state(state)) & registered
    if policy is DemoPolicy.STATIC_BASELINE or not pdb_available:
        effective -= _PDB_ACTIONS
    if state is ControllerState.RUNTIME_EVIDENCE:
        if session_active:
            effective.discard(ActionName.START_PDB_SESSION)
        else:
            effective -= _SESSION_ACTIONS
    if pdb_observations_remaining is not None and pdb_observations_remaining <= 0:
        effective = {
            action
            for action in effective
            if budget_kind_for_action(action) is not BudgetKind.PDB_OBSERVATIONS
        }
        if not session_active:
            effective.discard(ActionName.START_PDB_SESSION)
    if state is ControllerState.VALIDATE and not (
        post_patch_f2p_collected and regression_collected
    ):
        # classify_outcome is legal in Validate but meaningless until both
        # required evidence values exist.  Hide it rather than leaving the
        # model to rediscover a deterministic tool error.
        effective.discard(ActionName.CLASSIFY_OUTCOME)
    if not patch_allowed:
        effective.discard(ActionName.APPLY_PATCH)
    if not diagnosis_allowed:
        effective.discard(ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS)
    if not failure_trace_allowed:
        effective.discard(ActionName.GET_FAILURE_TRACE)
    result: dict[str, dict[str, Any]] = {}
    for action in ActionName:
        if action not in effective or action.value not in contracts:
            continue
        result[action.value] = dict(contracts[action.value])
    if ActionName.RUN_REPRODUCTION.value in result:
        properties = result[ActionName.RUN_REPRODUCTION.value].get("properties")
        if not isinstance(properties, Mapping) or "phase" not in properties:
            raise LiveConfigurationError("registry run_reproduction contract is not validator-derived")
        phase = dict(properties["phase"])
        phase["enum"] = list(legal_reproduction_phases(state))
        properties = dict(properties)
        properties["phase"] = phase
        result[ActionName.RUN_REPRODUCTION.value]["properties"] = properties
    return _detach_json_dict(result, max_bytes=MAX_TOOL_ARGUMENT_BYTES)
def _validate_enum_constrained_arguments(
    name: ActionName,
    arguments: Mapping[str, Any],
    contracts: Mapping[str, Mapping[str, Any]],
) -> None:
    """Reject an argument value outside its advertised, state-specific enum.

    Only arguments whose contract already declares an ``enum`` (currently
    ``run_reproduction.phase``) are checked here; this mirrors exactly what
    ``_action_contracts_for_state`` already advertises to the model, so the
    contract stays the single source of truth.
    """
    contract = contracts.get(name.value, {})
    properties = contract.get("properties", contract)
    for argument_name, spec in properties.items():
        if isinstance(spec, Mapping) and "enum" in spec and arguments.get(argument_name) not in spec["enum"]:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, f"'{argument_name}' must be one of {list(spec['enum'])}")

def _legal_transition_targets(
    state: ControllerState,
    *,
    pdb_transition_allowed: bool = True,
    patch_allowed: bool = True,
) -> list[str]:
    return [
        candidate.value
        for candidate in ControllerState
        if candidate in TRANSITION_GRAPH[state]
        and (candidate is not ControllerState.RUNTIME_EVIDENCE or pdb_transition_allowed)
        and (candidate is not ControllerState.PATCH or patch_allowed)
    ]
