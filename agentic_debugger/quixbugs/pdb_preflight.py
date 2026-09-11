"""Contained-PDB preflight gates.

This module owns the fail-closed preflight gate family for contained
PDB reachability cases: the gate vocabulary/report and
``contained_pdb_preflight``.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerRunResult,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisConfidence,
    HypothesisLedger,
    PdbGateContext,
    PdbGateDecision,
    PdbPolicy,
    decide_pdb_access,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    AddHypothesisDirective,
    ControllerSnapshot,
    ModelAdapterError,
    ModelDirective,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.bugsinpy.adapter import ExternalWorkspace
from agentic_debugger.bugsinpy.wsl import (
    DISTRO,
    ResourceLimits,
    WslBubblewrapRunner,
    build_bwrap_command,
    build_env_wrapped_command,
    build_linux_timeout_argv,
    build_prlimit_argv,
    build_wsl_command,
    to_wsl_path,
)
from agentic_debugger.demo.catalog import DemoCatalogError, RuntimeProbe, probe_driver_source, resolve_probe_breakpoint
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.tools import DemoToolContext, PdbProbe, build_registry
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.events.schema import RunEvent
from agentic_debugger.evaluation.runner import bounded_error
from agentic_debugger.evaluation.task_schema import DebugTask, TaskSource
from agentic_debugger.quixbugs.adapter import (
    QuixBugsAdapter,
    QuixBugsPreflightFacts,
    QuixBugsSourceAcquirer,
    QuixPreflightReport,
)
from agentic_debugger.runtime.exceptions import PdbSessionError
from agentic_debugger.runtime.execution import PdbLaunchPlan, VerifiedExecutionContext
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.quixbugs.pdb_bundle import _is_within, materialize_pdb_runtime_bundle

class ContainedPdbGateName(str, Enum):
    EXECUTION_CONTEXT_READY = "execution_context_ready"
    RESOURCE_ISOLATION_READY = "resource_isolation_ready"
    CONTAINMENT_READY = "containment_ready"
    LAUNCH_PLAN_IDENTITY = "launch_plan_identity"
    OBSERVATION_BUDGET_POSITIVE = "observation_budget_positive"


class ContainedPdbGateStatus(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ContainedPdbGateResult:
    name: ContainedPdbGateName
    status: ContainedPdbGateStatus
    reason: str

    def to_mapping(self) -> dict[str, str]:
        return {"name": self.name.value, "status": self.status.value, "reason": self.reason}


@dataclass(frozen=True)
class ContainedPdbPreflightReport:
    task_id: str
    gates: tuple[ContainedPdbGateResult, ...]

    @property
    def authorized(self) -> bool:
        return all(gate.status is ContainedPdbGateStatus.PASS for gate in self.gates)

    @property
    def blocked_gates(self) -> tuple[str, ...]:
        return tuple(gate.name.value for gate in self.gates if gate.status is not ContainedPdbGateStatus.PASS)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "authorized": self.authorized,
            "blocked_gates": list(self.blocked_gates),
            "gates": [gate.to_mapping() for gate in self.gates],
        }


def _boolean_gate(name: ContainedPdbGateName, ok: bool, reason: str) -> ContainedPdbGateResult:
    return ContainedPdbGateResult(name, ContainedPdbGateStatus.PASS if ok else ContainedPdbGateStatus.BLOCKED, reason if not ok else "explicitly verified")


def contained_pdb_preflight(
    *,
    task_id: str,
    execution_context: Optional[VerifiedExecutionContext],
    external_parent: Optional[str],
    repository_root: str,
    launch_plan: Optional[PdbLaunchPlan],
    expected_python_executable: Optional[str],
    expected_cwd: Optional[str],
    expected_target: Optional[str],
    expected_breakpoints: Optional[tuple[int, ...]],
    pdb_observation_budget: int,
) -> ContainedPdbPreflightReport:
    """Fail-closed gate for everything the accepted QuixBugs preflight does not
    already cover: a verified, resource-isolation-ready execution context, an
    owned containment relationship, an exact-identity-matched launch plan, and
    a positive observation budget. Every condition is derived from real
    objects (the runner's own non-forgeable ``resource_isolation_ready``
    attribute and ``boundary_guarantee``, the actual ``PdbLaunchPlan`` fields);
    none of it is a caller-supplied boolean.
    """

    context = execution_context
    execution_ready = context is not None
    gates = [_boolean_gate(ContainedPdbGateName.EXECUTION_CONTEXT_READY, execution_ready, "a verified execution context is required")]

    resource_ready = (
        execution_ready
        and getattr(context.runner, "resource_isolation_ready", False) is True
        and dict(getattr(context.runner, "boundary_guarantee", {})) == context.containment.to_mapping()
    )
    gates.append(_boolean_gate(ContainedPdbGateName.RESOURCE_ISOLATION_READY, resource_ready, "resource-isolation gate is not open on the supplied runner"))

    parent = Path(external_parent).resolve() if external_parent else None
    root = Path(context.containment.root).resolve() if execution_ready else None
    repository = Path(repository_root).resolve()
    containment_ready = (
        execution_ready
        and parent is not None
        and root is not None
        and root != root.parent
        and _is_within(parent, root)
        and not _is_within(repository, root)
    )
    gates.append(_boolean_gate(ContainedPdbGateName.CONTAINMENT_READY, containment_ready, "external parent/containment-root relationship is not verified"))

    plan_ok = (
        launch_plan is not None
        and expected_python_executable is not None
        and expected_cwd is not None
        and expected_target is not None
        and expected_breakpoints is not None
        and launch_plan.python_executable == expected_python_executable
        and launch_plan.cwd == expected_cwd
        and launch_plan.target == expected_target
        and launch_plan.driver == expected_target
        and launch_plan.breakpoints == expected_breakpoints
        and execution_ready
        and dict(launch_plan.environment) == dict(context.environment.environment)
    )
    gates.append(_boolean_gate(ContainedPdbGateName.LAUNCH_PLAN_IDENTITY, plan_ok, "PDB launch plan does not match the reviewed task/target/breakpoint/environment identity"))

    budget_ok = type(pdb_observation_budget) is int and pdb_observation_budget > 0
    gates.append(_boolean_gate(ContainedPdbGateName.OBSERVATION_BUDGET_POSITIVE, budget_ok, "PDB observation budget must be a positive int"))

    return ContainedPdbPreflightReport(task_id, tuple(gates))
