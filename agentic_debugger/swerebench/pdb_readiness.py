"""Honest PDB-readiness classification for SWE-rebench tasks.

Primary Pilot-10 treatment remains ``pdb-on-uncertainty``.

The current Pilot-10 uses Option B.  Its existing direct-file PDB launcher is
not coupled to the model-selected failing pytest process, so a pause is not
claimed as bug-relevant debugger evidence.  A separate treatment owns that
question.

A separate ``pdb-required-model-selected-target-v1`` contract exists for a
future treatment that would attach the debugger to hidden-test execution
without exposing those identities. It is not this Pilot-10.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentic_debugger.agent.controller_policy import PdbPolicy
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for

PDB_REQUIRED_TREATMENT_ID = "pdb-required-model-selected-target-v1"


@dataclass(frozen=True)
class PdbReadiness:
    instance_id: str
    policy: str
    gate_can_open_under_policy: bool
    catalog_probe_present: bool
    oracle_probe_used: bool
    model_selected_entry_available: bool
    public_failure_required_for_gate: bool
    hidden_tests_required_for_official_reproduction: bool
    classification: str
    reason: str
    pdb_exercised_requires: tuple[str, ...]
    future_pdb_required_treatment: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "policy": self.policy,
            "gate_can_open_under_policy": self.gate_can_open_under_policy,
            "catalog_probe_present": self.catalog_probe_present,
            "oracle_probe_used": self.oracle_probe_used,
            "model_selected_entry_available": self.model_selected_entry_available,
            "public_failure_required_for_gate": self.public_failure_required_for_gate,
            "hidden_tests_required_for_official_reproduction": (
                self.hidden_tests_required_for_official_reproduction
            ),
            "classification": self.classification,
            "reason": self.reason,
            "pdb_exercised_requires": list(self.pdb_exercised_requires),
            "future_pdb_required_treatment": self.future_pdb_required_treatment,
        }


def classify_pdb_readiness(
    instance_id: str,
    *,
    has_official_fail_to_pass: bool,
    policy: DemoPolicy = DemoPolicy.PDB_ON_UNCERTAINTY,
) -> PdbReadiness:
    if pdb_policy_for(policy) is not PdbPolicy.ON_UNCERTAINTY:
        raise ValueError("Pilot-10 PDB readiness is defined only for pdb-on-uncertainty")
    return PdbReadiness(
        instance_id=instance_id,
        policy=policy.value,
        gate_can_open_under_policy=False,
        catalog_probe_present=False,
        oracle_probe_used=False,
        model_selected_entry_available=False,
        public_failure_required_for_gate=False,
        hidden_tests_required_for_official_reproduction=has_official_fail_to_pass,
        classification="PDB_DEFERRED_TO_SEPARATE_TREATMENT",
        reason=(
            "Pilot-10 is overall-repair treatment only: the current PDB "
            "launcher does not run the model-selected public pytest target, "
            "so PDB NOT EXERCISED and any direct-file pause are not "
            "bug-relevant debugger evidence. A separate authorized treatment "
            "must implement failing-runtime-coupled PDB before making that "
            "claim. This is not ALWAYS_ON."
        ),
        pdb_exercised_requires=(
            "genuine public target failure recorded",
            "same verified pytest runtime attached to PDB",
            "breakpoint reached in that failing process",
            "at least one subsequent debugger action",
        ),
        future_pdb_required_treatment=PDB_REQUIRED_TREATMENT_ID,
    )


def pdb_was_exercised(
    *,
    pdb_entered: bool,
    debugger_actions: int,
    paused: bool,
) -> bool:
    return bool(pdb_entered and paused and debugger_actions > 0)
