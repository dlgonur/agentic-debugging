"""Demonstration policy variants layered on the accepted PDB gate policy.

Task 9 demonstrates the two policy variants the MVP plan calls mandatory for a
first comparison: the static/test-feedback baseline with the debugger disabled,
and one controller-gated PDB variant.  Both variants run the identical
controller, tool registry, runtime and verifier; the only difference is the
:class:`~agentic_debugger.agent.controller_policy.PdbPolicy` handed to the real
:func:`~agentic_debugger.agent.controller_policy.decide_pdb_access` gate.

``PdbPolicy.ALWAYS_ON`` and ``PdbPolicy.AFTER_FAILED_PATCH`` remain implemented
in the policy module but are deliberately outside the Task 9 demonstration set;
see ``docs/DEMO_TASK9.md`` for the deferral rationale.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Mapping

from agentic_debugger.agent.controller_policy import PdbPolicy


class DemoPolicy(str, Enum):
    """Policy variants exercised by the Task 9 demonstration."""

    STATIC_BASELINE = "static-baseline"
    PDB_ON_UNCERTAINTY = "pdb-on-uncertainty"


_PDB_MODE: Mapping[DemoPolicy, PdbPolicy] = MappingProxyType(
    {
        DemoPolicy.STATIC_BASELINE: PdbPolicy.DISABLED,
        DemoPolicy.PDB_ON_UNCERTAINTY: PdbPolicy.ON_UNCERTAINTY,
    }
)

#: Demonstration policies in stable execution order.
DEMO_POLICIES: tuple[DemoPolicy, ...] = (
    DemoPolicy.STATIC_BASELINE,
    DemoPolicy.PDB_ON_UNCERTAINTY,
)


def pdb_policy_for(policy: DemoPolicy) -> PdbPolicy:
    """Return the accepted PDB gate policy backing a demonstration variant."""

    if type(policy) is not DemoPolicy:
        raise ValueError("policy must be a DemoPolicy")
    return _PDB_MODE[policy]


def policy_from_value(value: str) -> DemoPolicy:
    """Resolve a CLI policy string without accepting partial matches."""

    for candidate in DemoPolicy:
        if candidate.value == value:
            return candidate
    raise ValueError(f"unknown demonstration policy: {value!r}")


__all__ = [
    "DEMO_POLICIES",
    "DemoPolicy",
    "pdb_policy_for",
    "policy_from_value",
]
