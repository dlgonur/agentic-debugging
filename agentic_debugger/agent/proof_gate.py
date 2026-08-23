"""Fail-closed exact-runtime evidence gate for the opt-in proof path."""

from __future__ import annotations

import re
from typing import Any, Iterable

from agentic_debugger.events.schema import Observation, ObservationStatus


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTRACT_KEYS = (
    "exact_reproduction", "task_id", "reproduction_argv", "pytest_node",
    "workspace_id", "production_file", "production_file_sha256",
    "breakpoint_line", "production_frame",
)

PROOF_ROLE_SELECTION_SCHEMA_VERSION = "pdb-proof-role-selection-v2"
PROOF_ROLE_SELECTION_POLICY_ID = "admissible-runtime-cycle-recovery-v2"
PROOF_ROLE_SELECTION_POLICY = {
    "schema_version": PROOF_ROLE_SELECTION_SCHEMA_VERSION,
    "policy_id": PROOF_ROLE_SELECTION_POLICY_ID,
    "history_retained": True,
    "successful_candidates_only": True,
    "exactly_one_successful_candidate_per_role": True,
    "successful_duplicates_fail_closed": "within_selected_runtime_cycle",
    "control_role_requires_paused_production_frame": True,
    "exited_control_resets_selected_runtime_cycle": True,
    "invalid_control_retained_in_event_history": True,
    "exited_control_releases_tool_session": True,
    "fresh_exact_session_bounded_by_controller_budget": True,
    "superseded_cycle_roles_ignored_after_exited_boundary": True,
    "convenience_selection": False,
    "chronology_source": "original_ordered_history",
    "identity_checks_unchanged": True,
}


def _ok(item: Any) -> bool:
    return (
        type(item) is Observation
        and item.status is ObservationStatus.OK
        and type(item.payload) is dict
    )


def _proof(item: Observation) -> dict[str, Any] | None:
    value = item.payload.get("proof")
    return value if type(value) is dict else None


def _contract_valid(contract: Any, *, task_id: str) -> bool:
    if type(contract) is not dict or set(contract) != set(_CONTRACT_KEYS):
        return False
    if contract.get("exact_reproduction") is not True:
        return False
    if contract.get("task_id") != task_id or not isinstance(task_id, str) or not task_id:
        return False
    argv = contract.get("reproduction_argv")
    if (
        type(argv) is not list or not argv
        or any(type(item) is not str or not item for item in argv)
    ):
        return False
    if type(contract.get("pytest_node")) is not str or not contract["pytest_node"]:
        return False
    if type(contract.get("workspace_id")) is not str or not contract["workspace_id"]:
        return False
    if type(contract.get("production_file")) is not str or not contract["production_file"]:
        return False
    if not isinstance(contract.get("production_file_sha256"), str) or not _SHA256.fullmatch(contract["production_file_sha256"]):
        return False
    if type(contract.get("breakpoint_line")) is not int or contract["breakpoint_line"] <= 0:
        return False
    return type(contract.get("production_frame")) is str and bool(contract["production_frame"])


def _same_contract(left: Any, right: dict[str, Any]) -> bool:
    return type(left) is dict and all(left.get(key) == right.get(key) for key in _CONTRACT_KEYS)


def _local_values(payload: dict[str, Any]) -> dict[str, Any] | None:
    entries = payload.get("locals")
    if type(entries) is not list:
        return None
    result: dict[str, Any] = {}
    for entry in entries:
        if type(entry) is not dict or type(entry.get("name")) is not str:
            return None
        if entry["name"] in result and result[entry["name"]] != entry.get("value"):
            return None
        result[entry["name"]] = entry.get("value")
    return result


def _runtime_cycle_start_index(ordered: tuple[Observation, ...]) -> int:
    recovery_boundaries = [
        index
        for index, item in enumerate(ordered)
        if _ok(item)
        and item.name in {"step_pdb_session", "next_pdb_session"}
        and item.payload.get("state") != "paused"
        and any(
            later.name == "start_pdb_session" and _ok(later)
            for later in ordered[index + 1:]
        )
    ]
    return recovery_boundaries[-1] + 1 if recovery_boundaries else 0


def validate_pdb_runtime_evidence(observations: Iterable[Observation]) -> tuple[bool, str]:
    """Validate the chronological baseline-to-control proof prefix.

    The input order is the controller's authoritative observation order.  No
    inference from names, latest values, or duplicate records is permitted.
    """

    ordered = tuple(observations)
    if not ordered:
        return False, "pdb evidence is absent"
    if any(type(item) is not Observation for item in ordered):
        return False, "pdb evidence contains an invalid observation"
    observation_ids = [item.observation_id for item in ordered]
    action_ids = [item.action_id for item in ordered]
    if len(set(observation_ids)) != len(observation_ids) or len(set(action_ids)) != len(action_ids):
        return False, "pdb evidence contains duplicate observation identity"

    # Rejected/error observations are authoritative history, but only a
    # successful observation may satisfy a proof role.  Keep ``ordered``
    # intact so the selected successful nodes are still checked against the
    # original chronology below.
    baseline_items = [
        item
        for item in ordered
        if _ok(item)
        and item.name == "run_reproduction"
        and item.payload.get("phase") == "baseline"
    ]
    cycle_start = _runtime_cycle_start_index(ordered)
    cycle = ordered[cycle_start:]
    start_items = [item for item in cycle if _ok(item) and item.name == "start_pdb_session"]
    stack_items = [item for item in cycle if _ok(item) and item.name == "get_stack_summary"]
    locals_items = [item for item in cycle if _ok(item) and item.name == "get_frame_locals"]
    raw_step_items = [
        item
        for item in cycle
        if _ok(item) and item.name in {"step_pdb_session", "next_pdb_session"}
    ]
    if any(len(items) != 1 for items in (baseline_items, start_items, stack_items, locals_items)):
        return False, "proof chain has missing or duplicate required observations"
    baseline, start, stack, locals_observation = (
        baseline_items[0], start_items[0], stack_items[0], locals_items[0]
    )
    if baseline.payload.get("failure_reproduced") is not True:
        return False, "baseline failure reproduction is missing"

    contract = _proof(start)
    if not _contract_valid(contract, task_id=start.task_id):
        return False, "PDB proof contract is malformed"
    step_items = [
        item
        for item in raw_step_items
        if item.payload.get("state") == "paused"
        and item.payload.get("script") == contract["production_file"]
        and item.payload.get("function") == contract["production_frame"]
    ]
    if len(step_items) != 1:
        if len(raw_step_items) == 1:
            return False, "step/next did not produce a paused production frame"
        return False, "proof chain has missing or duplicate required observations"
    step = step_items[0]
    chain = (baseline, start, stack, locals_observation, step)
    positions = [ordered.index(item) for item in chain]
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        return False, "proof chain is not chronological"
    if any(item.run_id != baseline.run_id or item.task_id != baseline.task_id for item in chain):
        return False, "proof observations cross run or task identity"
    if any(not _same_contract(_proof(item), contract) for item in chain[1:5]):
        return False, "proof observations cross runtime identity"
    if baseline.task_id != contract["task_id"] or baseline.payload.get("node_id") != contract["pytest_node"]:
        return False, "baseline pytest node does not match the proof contract"
    baseline_argv = baseline.payload.get("reproduction_argv")
    if type(baseline_argv) is not list or baseline_argv != contract["reproduction_argv"]:
        return False, "baseline reproduction argv is absent or does not match the proof contract"

    start_payload = start.payload
    if (
        start_payload.get("state") != "paused"
        or start_payload.get("script") != contract["production_file"]
        or start_payload.get("function") != contract["production_frame"]
        or start_payload.get("line") != contract["breakpoint_line"]
    ):
        return False, "PDB start is not the declared production breakpoint"
    frames = stack.payload.get("frames")
    if type(frames) is not list or not frames:
        return False, "stack evidence has no frames"
    current_frames = [frame for frame in frames if type(frame) is dict and frame.get("is_current") is True]
    if len(current_frames) != 1:
        return False, "stack evidence has no unique current frame"
    frame = current_frames[0]
    if frame.get("script") != contract["production_file"] or frame.get("function") != contract["production_frame"]:
        return False, "stack does not identify the declared production frame"
    if locals_observation.payload.get("state") != "paused" or locals_observation.payload.get("frame_id") != frame.get("frame_id"):
        return False, "locals are not bound to the production stack frame"
    if _local_values(locals_observation.payload) is None:
        return False, "locals evidence is malformed"
    return True, "exact-runtime PDB evidence is ready for diagnosis"


def validate_pdb_patch_evidence(observations: Iterable[Observation]) -> tuple[bool, str]:
    """Validate the chronological baseline-to-diagnosis proof chain."""

    ordered = tuple(observations)
    runtime_allowed, runtime_reason = validate_pdb_runtime_evidence(ordered)
    if not runtime_allowed:
        return False, runtime_reason

    baseline = next(
        item for item in ordered
        if _ok(item)
        and item.name == "run_reproduction"
        and item.payload.get("phase") == "baseline"
    )
    cycle = ordered[_runtime_cycle_start_index(ordered):]
    start = next(item for item in cycle if _ok(item) and item.name == "start_pdb_session")
    stack = next(item for item in cycle if _ok(item) and item.name == "get_stack_summary")
    locals_observation = next(item for item in cycle if _ok(item) and item.name == "get_frame_locals")
    contract = _proof(start)
    assert contract is not None
    step = next(
        item for item in cycle
        if _ok(item)
        and item.name in {"step_pdb_session", "next_pdb_session"}
        and item.payload.get("state") == "paused"
        and item.payload.get("script") == contract["production_file"]
        and item.payload.get("function") == contract["production_frame"]
    )
    diagnosis_items = [
        item
        for item in ordered
        if _ok(item) and item.name == "express_root_cause_hypothesis"
    ]
    if len(diagnosis_items) != 1:
        return False, "proof chain has missing or duplicate required observations"
    diagnosis = diagnosis_items[0]
    if ordered.index(diagnosis) <= ordered.index(step):
        return False, "proof chain is not chronological"
    if diagnosis.run_id != baseline.run_id or diagnosis.task_id != baseline.task_id:
        return False, "proof observations cross run or task identity"

    refs = diagnosis.payload.get("evidence_refs")
    values = diagnosis.payload.get("observed_values")
    if type(refs) is not list or any(type(ref) is not str for ref in refs) or type(values) is not dict or not values:
        return False, "diagnosis lacks structured evidence references"
    by_id = {item.observation_id: item for item in ordered}
    if len(set(refs)) != len(refs) or any(ref not in by_id for ref in refs):
        return False, "diagnosis references stale or nonexistent observations"
    required_refs = {stack.observation_id, locals_observation.observation_id, step.observation_id}
    if not required_refs.issubset(refs):
        return False, "diagnosis must reference stack, locals, and step/next observations"
    if any(ordered.index(by_id[ref]) >= ordered.index(diagnosis) for ref in refs):
        return False, "diagnosis references evidence created afterward"
    local_values = _local_values(locals_observation.payload)
    if local_values is None or any(name not in local_values or local_values[name] != value for name, value in values.items()):
        return False, "diagnosis runtime value is absent from referenced locals"
    if diagnosis.payload.get("proof_contract") != contract:
        return False, "diagnosis proof contract does not match the PDB runtime"
    return True, "exact-runtime PDB evidence and diagnosis are complete"


__all__ = [
    "PROOF_ROLE_SELECTION_POLICY",
    "PROOF_ROLE_SELECTION_POLICY_ID",
    "PROOF_ROLE_SELECTION_SCHEMA_VERSION",
    "validate_pdb_runtime_evidence",
    "validate_pdb_patch_evidence",
]
