"""Deterministic demo report contracts: statuses, outcomes, records.

This module owns the versioned demo report vocabulary and the immutable
result/report records: ``RESULTS_SCHEMA_VERSION``, the demo error
vocabulary, ``DemoCaseStatus``/``LocalizationOutcome``/
``RuntimeEvidenceOutcome``, the bounded helpers, ``DemoCaseResult``,
``DemoReport``, and the deterministic (nondeterministic-key-stripping)
view of serialized results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.isolation import guard_scope_note
from agentic_debugger.evaluation.runner import bounded_error, normalize_output

RESULTS_SCHEMA_VERSION = "1.0"

#: Relative path of the curated dataset root inside the repository.
CURATED_RELATIVE_ROOT = "agentic_debugger/datasets/curated"

#: Tool-version string recorded in every demonstration event.
DEMO_TOOL_VERSION = "demo-task9-v1"

#: Model-call ceiling for one demonstration case.  Generous but bounded.
DEMO_MAX_MODEL_CALLS = 64

#: Top-level result keys whose contents are environment- or timing-dependent
#: and therefore excluded from the deterministic comparison.
NONDETERMINISTIC_RESULT_KEYS: tuple[str, ...] = ("environment", "timing")

_PDB_ACTION_NAMES = frozenset(
    {
        "start_pdb_session",
        "get_failure_trace",
        "get_stack_summary",
        "get_frame",
        "get_frame_locals",
        "safe_eval_expression",
        "inspect_caller_frame",
        "stop_pdb_session",
    }
)
_PDB_OBSERVATION_ACTION_NAMES = frozenset(
    {
        "get_failure_trace",
        "get_stack_summary",
        "get_frame",
        "get_frame_locals",
        "safe_eval_expression",
        "inspect_caller_frame",
    }
)


class DemoError(RuntimeError):
    """Base class for demonstration orchestration failures."""


class DemoInputError(DemoError):
    """Raised for an invalid demonstration request."""


class DemoCaseStatus(str, Enum):
    """How far one demonstration case actually got."""

    COMPLETED = "COMPLETED"
    CONTROLLER_STOPPED = "CONTROLLER_STOPPED"
    HARNESS_ERROR = "HARNESS_ERROR"


class LocalizationOutcome(str, Enum):
    """Localization categories from the accepted MVP evaluation design."""

    CORRECT_TARGET_SYMBOL = "CORRECT_TARGET_SYMBOL"
    WRONG_LOCATION_IN_CORRECT_FILE = "WRONG_LOCATION_IN_CORRECT_FILE"
    WRONG_FILE = "WRONG_FILE"
    NO_LOCALIZATION = "NO_LOCALIZATION"
    NO_PATCH = "NO_PATCH"


class RuntimeEvidenceOutcome(str, Enum):
    """Runtime-evidence categories this demonstration can honestly support."""

    PDB_NOT_USED = "PDB_NOT_USED"
    #: The debugger ran and returned bounded evidence.  Deliberately *not*
    #: named "confirmed": nothing in this demonstration compares the collected
    #: values against the diagnosis, so no confirmation is measured.
    PDB_EVIDENCE_COLLECTED = "PDB_EVIDENCE_COLLECTED"
    PDB_TOOL_FAILURE = "PDB_TOOL_FAILURE"
    PDB_NOT_REACHED = "PDB_NOT_REACHED"


#: Why the demonstration never claims that PDB changed a diagnosis.
RUNTIME_ATTRIBUTION_NOTE = (
    "The offline model's diagnosis and candidate repair are fixed before the "
    "run, so collected runtime evidence cannot change them. This demonstration "
    "measures debugger-path execution and cost, not debugger usefulness."
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded(text: str, workspace_root: Optional[str] = None, limit: int = 400) -> str:
    """Bound a message and redact disposable workspace paths from it.

    Diagnostics are part of the deterministic result section, so a raw OS
    error naming a ``mkdtemp`` directory would break byte stability.
    Redaction reuses the accepted verifier helper.
    """

    normalized = normalize_output(text, workspace_root)
    cleaned = "".join(
        char if 0x20 <= ord(char) != 0x7F else " " for char in normalized
    ).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3] + "..."
    return cleaned or "unspecified"


def _diagnostic(exc: BaseException, workspace_root: Optional[str] = None) -> str:
    return _bounded(bounded_error(exc), workspace_root)


def curated_task_ids(repository_root: str | Path) -> tuple[str, ...]:
    """Discover the complete curated set from the live dataset directory."""

    root = Path(repository_root) / CURATED_RELATIVE_ROOT
    if not root.is_dir():
        raise DemoInputError(f"curated dataset root is missing: {root}")
    found = sorted(
        item.name for item in root.iterdir() if item.is_dir() and (item / "task.json").is_file()
    )
    if not found:
        raise DemoInputError(f"curated dataset root contains no tasks: {root}")
    return tuple(found)

# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoCaseResult:
    """The complete measured record of one task/policy demonstration case."""

    task_id: str
    policy: str
    pdb_policy: str
    status: DemoCaseStatus
    controller: dict[str, Any]
    gate_decisions: tuple[dict[str, Any], ...]
    localization: dict[str, Any]
    patch: dict[str, Any]
    runtime_evidence: dict[str, Any]
    controller_validation: dict[str, Any]
    verifier: dict[str, Any]
    trajectory: dict[str, Any]
    tool_errors: tuple[dict[str, str], ...]
    offline: dict[str, Any]
    diagnostics: tuple[str, ...]
    wall_clock_ms: int
    semantic_events: tuple[dict[str, Any], ...] = field(default=(), repr=False, compare=False)
    events_jsonl: str = field(default="", repr=False, compare=False)
    retrieval: Optional[dict[str, Any]] = field(default=None, repr=False, compare=False)

    @property
    def case_id(self) -> str:
        return f"{self.task_id}__{self.policy}"

    def to_mapping(self) -> dict[str, Any]:
        """Return the deterministic, machine-readable case record."""

        mapping = {
            "case_id": self.case_id,
            "task_id": self.task_id,
            "policy": self.policy,
            "pdb_policy": self.pdb_policy,
            "status": self.status.value,
            "controller": self.controller,
            "gate_decisions": [dict(item) for item in self.gate_decisions],
            "localization": self.localization,
            "patch": self.patch,
            "runtime_evidence": self.runtime_evidence,
            "controller_validation": self.controller_validation,
            "verifier": self.verifier,
            "trajectory": self.trajectory,
            "tool_errors": [dict(item) for item in self.tool_errors],
            "offline": self.offline,
            "diagnostics": list(self.diagnostics),
        }
        # Additive retrieval block: present only when RAG context was
        # explicitly supplied, so the default demo output stays byte-identical.
        if self.retrieval is not None:
            mapping["retrieval"] = self.retrieval
        return mapping

    def timing_mapping(self) -> dict[str, Any]:
        """Return the wall-clock measurement, which is never deterministic."""

        return {"case_id": self.case_id, "wall_clock_ms": self.wall_clock_ms}


@dataclass(frozen=True)
class DemoReport:
    """The complete demonstration result document."""

    environment: dict[str, Any]
    cases: tuple[DemoCaseResult, ...]

    def to_mapping(self) -> dict[str, Any]:
        cases = [case.to_mapping() for case in self.cases]
        return {
            "schema_version": RESULTS_SCHEMA_VERSION,
            "environment": self.environment,
            "determinism": {
                "nondeterministic_top_level_keys": list(NONDETERMINISTIC_RESULT_KEYS),
                "note": (
                    "Every key outside "
                    f"{list(NONDETERMINISTIC_RESULT_KEYS)} is byte-stable across "
                    "repeated local runs of the same working tree. 'environment' "
                    "identifies the tested tree and interpreter; 'timing' holds "
                    "wall-clock measurements."
                ),
            },
            "aggregates": self.aggregates(),
            "cases": cases,
            "timing": [case.timing_mapping() for case in self.cases],
        }

    def aggregates(self) -> dict[str, Any]:
        """Derive per-policy aggregates from the measured case records only."""

        by_policy: dict[str, dict[str, Any]] = {}
        for case in self.cases:
            bucket = by_policy.setdefault(
                case.policy,
                {
                    "policy": case.policy,
                    "pdb_policy": case.pdb_policy,
                    "cases": 0,
                    "controller_done": 0,
                    "verifier_completed": 0,
                    "verifier_outcomes": {},
                    "localization_outcomes": {},
                    "runtime_evidence_outcomes": {},
                    "patches_applied": 0,
                    "f2p_passed": 0,
                    "f2p_total": 0,
                    "p2p_passed": 0,
                    "p2p_total": 0,
                    "model_calls": 0,
                    "tool_calls": 0,
                    "pdb_actions": 0,
                    "pdb_observation_attempts": 0,
                    "pdb_observations_succeeded": 0,
                    "provider_attempts": 0,
                    "network_attempts": 0,
                    "patch_sha256_by_task": {},
                },
            )
            bucket["cases"] += 1
            if case.controller.get("final_state") == ControllerState.DONE.value:
                bucket["controller_done"] += 1
            if case.verifier.get("status") == "COMPLETED":
                bucket["verifier_completed"] += 1
            outcome = case.verifier.get("outcome") or "not_produced"
            bucket["verifier_outcomes"][outcome] = bucket["verifier_outcomes"].get(outcome, 0) + 1
            localization = case.localization.get("outcome", LocalizationOutcome.NO_LOCALIZATION.value)
            bucket["localization_outcomes"][localization] = (
                bucket["localization_outcomes"].get(localization, 0) + 1
            )
            evidence = case.runtime_evidence.get("outcome", RuntimeEvidenceOutcome.PDB_NOT_REACHED.value)
            bucket["runtime_evidence_outcomes"][evidence] = (
                bucket["runtime_evidence_outcomes"].get(evidence, 0) + 1
            )
            bucket["patches_applied"] += 1 if case.patch.get("applied") else 0
            bucket["f2p_passed"] += int(case.verifier.get("f2p_passed") or 0)
            bucket["f2p_total"] += int(case.verifier.get("f2p_total") or 0)
            bucket["p2p_passed"] += int(case.verifier.get("p2p_passed") or 0)
            bucket["p2p_total"] += int(case.verifier.get("p2p_total") or 0)
            bucket["model_calls"] += int(case.controller.get("model_calls") or 0)
            bucket["tool_calls"] += int(case.controller.get("tool_call_count") or 0)
            bucket["pdb_actions"] += int(case.runtime_evidence.get("pdb_action_count") or 0)
            bucket["pdb_observation_attempts"] += int(
                case.runtime_evidence.get("pdb_observation_attempts") or 0
            )
            bucket["pdb_observations_succeeded"] += int(
                case.runtime_evidence.get("pdb_observations_succeeded") or 0
            )
            bucket["provider_attempts"] += int(case.offline.get("provider_attempts") or 0)
            bucket["network_attempts"] += int(case.offline.get("network_attempts") or 0)
            bucket["patch_sha256_by_task"][case.task_id] = case.patch.get("sha256")
        for bucket in by_policy.values():
            for key in ("verifier_outcomes", "localization_outcomes", "runtime_evidence_outcomes"):
                bucket[key] = {name: bucket[key][name] for name in sorted(bucket[key])}
        return {
            "by_policy": [by_policy[name] for name in sorted(by_policy)],
            "case_count": len(self.cases),
            "task_count": len({case.task_id for case in self.cases}),
            "patch_parity": self.patch_parity(),
            "offline": {
                "provider_attempts": sum(
                    int(case.offline.get("provider_attempts") or 0) for case in self.cases
                ),
                "network_attempts": sum(
                    int(case.offline.get("network_attempts") or 0) for case in self.cases
                ),
                "guard_scope": guard_scope_note(),
            },
        }

    def patch_parity(self) -> dict[str, Any]:
        """Derive, per task, whether every policy received the same diff."""

        by_task: dict[str, dict[str, Optional[str]]] = {}
        for case in self.cases:
            by_task.setdefault(case.task_id, {})[case.policy] = case.patch.get("sha256")
        tasks = []
        for task_id in sorted(by_task):
            digests = by_task[task_id]
            distinct = {value for value in digests.values() if value is not None}
            tasks.append(
                {
                    "task_id": task_id,
                    "policies_compared": sorted(digests),
                    "distinct_patch_digests": len(distinct),
                    "identical_across_policies": None
                    if len(digests) < 2 or not distinct
                    else len(distinct) == 1,
                }
            )
        comparable = [
            item for item in tasks if item["identical_across_policies"] is not None
        ]
        return {
            "tasks": tasks,
            "comparable_task_count": len(comparable),
            "all_comparable_tasks_identical": None
            if not comparable
            else all(item["identical_across_policies"] for item in comparable),
        }


def deterministic_view(results: dict[str, Any]) -> dict[str, Any]:
    """Strip the explicitly nondeterministic sections from a result document."""

    return {key: value for key, value in results.items() if key not in NONDETERMINISTIC_RESULT_KEYS}


# ---------------------------------------------------------------------------
# Environment identification
# ---------------------------------------------------------------------------
