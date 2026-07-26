"""Deterministic offline end-to-end demonstration of the agentic-debugging MVP.

This package wires the already-accepted controller, tool-policy, runtime,
PDB, patch, source-retrieval, event-replay and verifier components into one
repeatable local demonstration over the curated benchmark set.

It adds no new debugging capability.  Its only new responsibility is
orchestration, measurement and reporting.  Every reported outcome is produced
by executing the real components; nothing is asserted from a stored table.

The demonstration never contacts a model provider or the network.  The model
is replaced by :class:`agentic_debugger.demo.model.DemoPolicyModel`, a
deterministic offline stand-in whose fixed outputs come from
:mod:`agentic_debugger.demo.catalog`.
"""

from agentic_debugger.demo.catalog import (
    DemoScenario,
    LocalizationClaim,
    ReferenceRepair,
    RuntimeProbe,
    build_reference_patch,
    reference_repair_snippets,
    scenario_for,
    scenario_ids,
)
from agentic_debugger.demo.policies import (
    DEMO_POLICIES,
    DemoPolicy,
    pdb_policy_for,
)
from agentic_debugger.demo.runner import (
    DemoCaseResult,
    DemoError,
    DemoInputError,
    DemoReport,
    LocalizationOutcome,
    RuntimeEvidenceOutcome,
    curated_task_ids,
    run_demo,
    run_demo_case,
)

__all__ = [
    "DEMO_POLICIES",
    "DemoCaseResult",
    "DemoError",
    "DemoInputError",
    "DemoPolicy",
    "DemoReport",
    "DemoScenario",
    "LocalizationClaim",
    "LocalizationOutcome",
    "ReferenceRepair",
    "RuntimeEvidenceOutcome",
    "RuntimeProbe",
    "build_reference_patch",
    "curated_task_ids",
    "pdb_policy_for",
    "reference_repair_snippets",
    "run_demo",
    "run_demo_case",
    "scenario_for",
    "scenario_ids",
]
