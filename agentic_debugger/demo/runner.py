"""Orchestration and measurement for the Task 9 end-to-end demonstration.

One *case* is one curated task executed under one demonstration policy.  A
case runs the real integrated MVP twice over the same candidate repair:

1. the accepted controller drives the accepted tool registry inside a
   disposable workspace, producing a trajectory, a candidate diff and its own
   validation classification;
2. the accepted Task 7 verifier independently re-evaluates the same candidate
   diff from a fresh baseline workspace and produces the authoritative outcome.

Every reported measurement is derived from those executions.  Nothing in this
module encodes an expected outcome for any curated task.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerRunResult,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
    PdbPolicy,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.demo.catalog import (
    DemoCatalogError,
    build_reference_patch,
    scenario_for,
    scenario_ids,
)
from agentic_debugger.demo.isolation import OfflineGuard, guard_scope_note
from agentic_debugger.demo.model import DEMO_MODEL_NAME, DemoPolicyModel
from agentic_debugger.demo.policies import DEMO_POLICIES, DemoPolicy, pdb_policy_for
from agentic_debugger.demo.tools import (
    DemoToolContext,
    build_registry,
    prepare_pdb_probe,
)
from agentic_debugger.evaluation.runner import bounded_error, load_task, normalize_output
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.events.replay import (
    ReplayError,
    replay_events,
    semantic_projection,
)
from agentic_debugger.events.schema import EventType, ObservationStatus
from agentic_debugger.runtime.workspace import TaskWorkspace

#: Version of the machine-readable demonstration result document.
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


def _git(repository_root: Path, *args: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repository_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def source_tree_digest(repository_root: Path) -> str:
    """Digest every implementation and dataset byte the demonstration ran on."""

    digest = hashlib.sha256()
    package_root = repository_root / "agentic_debugger"
    for path in sorted(package_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(repository_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def fixture_digest(fixture_dir: Path) -> str:
    """Digest one canonical curated fixture, ignoring bytecode caches."""

    digest = hashlib.sha256()
    for path in sorted(item for item in fixture_dir.rglob("*") if item.is_file()):
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(fixture_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def environment_record(repository_root: Path) -> dict[str, Any]:
    """Identify the exact working-tree state and interpreter under test."""

    status = _git(repository_root, "status", "--porcelain")
    return {
        "generated_utc": _utc_now(),
        "repository_root_name": repository_root.name,
        "git_head": _git(repository_root, "rev-parse", "HEAD"),
        "git_branch": _git(repository_root, "rev-parse", "--abbrev-ref", "HEAD"),
        "git_working_tree_dirty": None if status is None else bool(status),
        "git_status_digest": None
        if status is None
        else hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "source_tree_sha256": source_tree_digest(repository_root),
        "python_version": sys.version.split()[0],
        "platform": platform.system(),
        "model_backend": DEMO_MODEL_NAME,
        # Stated policy, not a measurement.  The measured counters produced
        # by the in-process offline guard live in each case's "offline"
        # block and in aggregates.offline.
        "model_provider_policy": "none-configured",
        "network_access_policy": "blocked-in-process",
        "offline_guard_scope": guard_scope_note(),
    }


# ---------------------------------------------------------------------------
# Metric derivation
# ---------------------------------------------------------------------------


def classify_localization(
    declared: Optional[dict[str, str]],
    changed_files: Sequence[str],
    patch_applied: bool,
    oracle_files: Sequence[str],
    oracle_symbols: Sequence[str],
) -> LocalizationOutcome:
    """Score the declared localization against the evaluator-only oracle.

    The accepted evaluation design uses one flat category, so precedence has
    to be fixed and stated.  It is, in order:

    1. no claim declared -> ``NO_LOCALIZATION``;
    2. claimed file is not an oracle target, or an applied patch touched a
       file outside the oracle targets -> ``WRONG_FILE``;
    3. claimed symbol is not an oracle target -> ``WRONG_LOCATION_IN_CORRECT_FILE``;
    4. claim is right but no patch was applied -> ``NO_PATCH``;
    5. otherwise ``CORRECT_TARGET_SYMBOL``.

    :func:`localization_record` additionally reports each individual fact.
    """

    if not declared:
        return LocalizationOutcome.NO_LOCALIZATION
    if declared.get("file_path") not in set(oracle_files):
        return LocalizationOutcome.WRONG_FILE
    if patch_applied and not set(changed_files).issubset(set(oracle_files)):
        return LocalizationOutcome.WRONG_FILE
    if declared.get("symbol") not in set(oracle_symbols):
        return LocalizationOutcome.WRONG_LOCATION_IN_CORRECT_FILE
    if not patch_applied:
        return LocalizationOutcome.NO_PATCH
    return LocalizationOutcome.CORRECT_TARGET_SYMBOL


def localization_record(
    declared: Optional[dict[str, str]],
    changed_files: Sequence[str],
    patch_applied: bool,
    oracle_files: Sequence[str],
    oracle_symbols: Sequence[str],
) -> dict[str, Any]:
    """Return the flat category plus every individual localization fact.

    One flat category cannot express both *where the claim pointed* and
    *whether a patch was produced*, so the individual booleans are reported
    alongside it and nothing is lost to precedence.
    """

    files = set(oracle_files)
    symbols = set(oracle_symbols)
    return {
        "declared": declared,
        "oracle_target_files": list(oracle_files),
        "oracle_target_symbols": list(oracle_symbols),
        "patch_changed_files": list(changed_files),
        "patch_applied": patch_applied,
        "claim_declared": declared is not None,
        "claim_file_matches_oracle": None
        if not declared
        else declared.get("file_path") in files,
        "claim_symbol_matches_oracle": None
        if not declared
        else declared.get("symbol") in symbols,
        "patch_within_oracle_files": None
        if not patch_applied
        else set(changed_files).issubset(files),
        "outcome": classify_localization(
            declared, changed_files, patch_applied, oracle_files, oracle_symbols
        ).value,
    }


def classify_runtime_evidence(
    *,
    gate_reached: bool,
    gate_allowed: bool,
    session_started: bool,
    evidence_collected: bool,
    proof_required: bool = False,
) -> RuntimeEvidenceOutcome:
    """Classify debugger use with only the categories this demo can support."""

    if proof_required:
        if not session_started or not evidence_collected:
            return RuntimeEvidenceOutcome.PDB_TOOL_FAILURE
        return RuntimeEvidenceOutcome.PDB_EVIDENCE_COLLECTED
    if not gate_reached:
        return RuntimeEvidenceOutcome.PDB_NOT_REACHED
    if not gate_allowed:
        return RuntimeEvidenceOutcome.PDB_NOT_USED
    if not session_started or not evidence_collected:
        return RuntimeEvidenceOutcome.PDB_TOOL_FAILURE
    return RuntimeEvidenceOutcome.PDB_EVIDENCE_COLLECTED


def _controller_record(result: ControllerRunResult, tool_calls: Sequence[str]) -> dict[str, Any]:
    return {
        "final_state": result.final_state.value,
        "stop_reason": result.stop_reason.value,
        "model_calls": result.model_calls,
        "step_count": len(result.steps),
        # Actions the controller issued, including any the registry rejected
        # before a handler ran.
        "action_count": sum(1 for step in result.steps if step.action is not None),
        # Actions that actually reached a demonstration tool handler.  Lower
        # than action_count whenever the registry rejected an action.
        "tool_call_count": len(tool_calls),
        "tool_calls": list(tool_calls),
        "budget_state": {
            "patch_attempts": result.budget_state.patch_attempts,
            "test_runs": result.budget_state.test_runs,
            "pdb_observations": result.budget_state.pdb_observations,
            "source_observations": result.budget_state.source_observations,
        },
        "states_visited": sorted({step.state_before.value for step in result.steps}
                                 | {step.state_after.value for step in result.steps}),
    }


def _verifier_record(evaluation: Any, note: Optional[str] = None) -> dict[str, Any]:
    if evaluation is None:
        # Keep the key set stable so downstream consumers never have to branch
        # on whether the verifier ran.
        return {
            "executed": False,
            "status": None,
            "stop_reason": None,
            "outcome": None,
            "baseline_valid": None,
            "baseline_reason": None,
            "patch_applied": None,
            "patch_changed_files": [],
            "syntax_passed": None,
            "f2p_total": None,
            "f2p_passed": None,
            "p2p_total": None,
            "p2p_passed": None,
            "full_suite_status": None,
            "verification_command_count": None,
            "verification_selected_test_count": None,
            "timeout": None,
            "workspace_lifecycle": None,
            "workspace_cleaned": None,
            "canonical_fixture_unchanged": None,
            "diagnostic": None,
            "note": note or "verifier was not executed",
        }
    full_suite = evaluation.full_suite
    return {
        "executed": True,
        "status": evaluation.status.value,
        "stop_reason": evaluation.stop_reason,
        "outcome": evaluation.outcome.value if evaluation.outcome else None,
        "baseline_valid": evaluation.baseline.valid,
        "baseline_reason": evaluation.baseline.reason,
        "patch_applied": evaluation.patch_application.success,
        "patch_changed_files": list(evaluation.patch_application.changed_files),
        "syntax_passed": evaluation.syntax.passed,
        "f2p_total": evaluation.f2p_total,
        "f2p_passed": evaluation.f2p_passed,
        "p2p_total": evaluation.p2p_total,
        "p2p_passed": evaluation.p2p_passed,
        "full_suite_status": full_suite.status.value if full_suite else None,
        "verification_command_count": evaluation.verification_command_count,
        "verification_selected_test_count": evaluation.verification_selected_test_count,
        "timeout": evaluation.timeout,
        "workspace_lifecycle": evaluation.workspace.lifecycle.value,
        "workspace_cleaned": evaluation.workspace.cleaned,
        "canonical_fixture_unchanged": evaluation.workspace.canonical_fixture_unchanged,
        "diagnostic": evaluation.diagnostic,
        "note": note,
    }


def _trajectory_record(
    semantic_events: Sequence[dict[str, Any]],
    replay_error: Optional[str],
) -> dict[str, Any]:
    actions = [item for item in semantic_events if item.get("event_type") == EventType.ACTION.value]
    observations = [
        item for item in semantic_events if item.get("event_type") == EventType.OBSERVATION.value
    ]

    def _status(item: dict[str, Any]) -> Optional[str]:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            return None
        observation = payload.get("observation")
        if not isinstance(observation, dict):
            return None
        value = observation.get("status")
        return value if isinstance(value, str) else None

    statuses = [_status(item) for item in observations]
    return {
        "replay_attempted": bool(semantic_events) or replay_error is not None,
        "replay_valid": replay_error is None and bool(semantic_events),
        "replay_error": replay_error,
        "event_count": len(semantic_events),
        "action_count": len(actions),
        "observation_count": len(observations),
        "observation_status_counts": {
            candidate.value: statuses.count(candidate.value)
            for candidate in ObservationStatus
        },
    }


# ---------------------------------------------------------------------------
# Case execution
# ---------------------------------------------------------------------------


def run_demo_case(
    *,
    repository_root: str | Path,
    task_id: str,
    policy: DemoPolicy,
    workspace_parent: str | Path,
    rag_context: Any = None,
) -> DemoCaseResult:
    """Execute one curated task under one demonstration policy end to end.

    ``rag_context`` is an optional additive seam: when supplied (and only
    then) it is handed to the offline model, which records its retrieval
    evidence on the case result without changing any directive.  Default
    no-RAG behavior is unchanged and the default case record is
    byte-identical.
    """

    if type(policy) is not DemoPolicy:
        raise DemoInputError("policy must be a DemoPolicy")
    repo = Path(repository_root).resolve()
    fixture_dir = repo / CURATED_RELATIVE_ROOT / task_id
    if not (fixture_dir / "task.json").is_file():
        raise DemoInputError(f"curated task manifest is missing: {task_id}")
    try:
        scenario = scenario_for(task_id)
    except DemoCatalogError as exc:
        raise DemoInputError(str(exc)) from exc

    parent = Path(workspace_parent)
    if not parent.is_dir():
        raise DemoInputError(f"workspace_parent must be an existing directory: {parent}")

    pdb_mode = pdb_policy_for(policy)
    # Load the manifest before creating any directory so an invalid task cannot
    # leave an orphaned case workspace behind, and so a malformed canonical
    # manifest surfaces as an input error rather than an uncaught traceback.
    try:
        task = load_task(str(fixture_dir / "task.json"))
    except DemoInputError:
        raise
    except Exception as exc:
        raise DemoInputError(
            f"curated task manifest {task_id!r} is invalid: {_diagnostic(exc)}"
        ) from exc

    case_parent = parent / f"case-{task_id}-{policy.value}"
    case_parent.mkdir(parents=True, exist_ok=False)
    diagnostics: list[str] = []
    started = time.monotonic()

    workspace: Optional[TaskWorkspace] = None
    context: Optional[DemoToolContext] = None
    model: Optional[DemoPolicyModel] = None
    probe = None
    controller_result: Optional[ControllerRunResult] = None
    evaluation = None
    verifier_note: Optional[str] = None
    semantic_events: tuple[dict[str, Any], ...] = ()
    events_jsonl = ""
    replay_error: Optional[str] = None
    patch_text = ""
    status = DemoCaseStatus.HARNESS_ERROR
    guard = OfflineGuard()
    # Hash the canonical fixture around the controller phase so the
    # demonstration reports fixture immutability itself rather than relying
    # only on the verifier, whose own hash is taken after the controller ran.
    fixture_before = fixture_digest(fixture_dir)
    fixture_after: Optional[str] = None

    try:
        with guard:
            workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_parent))
            if scenario.runtime_probe.exact_public_reproduction:
                (Path(workspace.root) / "task.json").write_text(
                    json.dumps(task.agent_visible_mapping(), sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
            source_path = Path(workspace.root) / scenario.reference_repair.target_path
            patch_text = build_reference_patch(
                source_path.read_text(encoding="utf-8"), scenario.reference_repair
            )
            if pdb_mode is not PdbPolicy.DISABLED:
                probe = prepare_pdb_probe(
                    fixture_dir, scenario, case_parent, task=task
                )

            context = DemoToolContext(
                task=task,
                workspace=workspace,
                patch=patch_text,
                probe=probe,
            )
            model = DemoPolicyModel(
                scenario=scenario,
                patch=patch_text,
                pdb_policy=pdb_mode,
                rag_context=rag_context,
            )
            controller = DeterministicController(
                build_registry(
                    context,
                    interactive_debugger_controls=(
                        scenario.runtime_probe.exact_public_reproduction
                    ),
                ),
                model,
                ControllerRunConfig(
                    max_model_calls=DEMO_MAX_MODEL_CALLS,
                    require_pdb_evidence_before_patch=(
                        scenario.runtime_probe.exact_public_reproduction
                    ),
                ),
            )
            snapshot = ControllerSnapshot(
                f"{task_id}--{policy.value}",
                task_id,
                ControllerState.REPRODUCE,
                0,
                ControllerBudgetLimits.from_task_constraints(task.constraints),
                ControllerBudgetState(),
                HypothesisLedger(),
            )
            controller_result = controller.run(snapshot)
            fixture_after = fixture_digest(fixture_dir)

            stream = io.StringIO()
            logger = JsonlEventLogger(controller_result.run_id, controller_result.task_id, stream=stream)
            try:
                for event in project_controller_run(
                    controller_result,
                    tool_version=DEMO_TOOL_VERSION,
                    model=DEMO_MODEL_NAME,
                    timestamp=_utc_now(),
                    duration_ms=None,
                ):
                    logger.append(event)
                logger.flush()
            finally:
                logger.close()
            events_jsonl = stream.getvalue()
            try:
                trajectory = replay_events(events_jsonl)
                semantic_events = semantic_projection(
                    trajectory, workspace_roots=[str(case_parent), str(workspace.root)]
                )
            except ReplayError as exc:
                replay_error = _diagnostic(exc, str(case_parent))
                diagnostics.append(f"replay validation failed: {replay_error}")

            # The verifier independently re-evaluates the candidate diff from a
            # clean baseline; it never sees the controller's workspace.
            evaluation = EvaluationVerifier(str(repo), workspace_parent=str(case_parent)).evaluate(
                task, patch_text
            )
            status = (
                DemoCaseStatus.COMPLETED
                if controller_result.final_state is ControllerState.DONE
                else DemoCaseStatus.CONTROLLER_STOPPED
            )
    except BaseException as exc:  # noqa: BLE001 - recorded, not swallowed silently
        diagnostics.append(f"case execution failed: {_diagnostic(exc, str(case_parent))}")
        if evaluation is None:
            verifier_note = "verifier skipped because the demonstration case failed"
        status = DemoCaseStatus.HARNESS_ERROR
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        cleanup_ok = _release(context, workspace, case_parent, diagnostics)
        if not cleanup_ok:
            status = DemoCaseStatus.HARNESS_ERROR

    wall_clock_ms = int((time.monotonic() - started) * 1000)
    return _assemble_case(
        task_id=task_id,
        policy=policy,
        pdb_mode=pdb_mode,
        task=task,
        status=status,
        controller_result=controller_result,
        context=context,
        model_used=model,
        evaluation=evaluation,
        verifier_note=verifier_note,
        semantic_events=semantic_events,
        events_jsonl=events_jsonl,
        replay_error=replay_error,
        patch_text=patch_text,
        diagnostics=diagnostics,
        wall_clock_ms=wall_clock_ms,
        offline_ledger=guard.ledger,
        fixture_before=fixture_before,
        fixture_after=fixture_after,
    )

def _release(
    context: Optional[DemoToolContext],
    workspace: Any,
    case_parent: Path,
    diagnostics: list[str],
) -> bool:
    """Release every workspace and subprocess resource; never raise."""

    ok = True
    root = str(case_parent)
    if context is not None:
        # release_pdb keeps a handle whose stop() failed, so a second pass
        # here is a genuine retry rather than a no-op.
        for error in context.release_pdb():
            ok = False
            diagnostics.append(f"pdb cleanup failed: {_diagnostic(error, root)}")
    if workspace is not None:
        try:
            workspace.cleanup()
            # TaskWorkspace.cleanup swallows its own rmtree failure, so the
            # only reliable check is whether the root is actually gone.
            if os.path.exists(workspace.root):
                raise DemoError("controller workspace root remains after cleanup")
        except BaseException as exc:  # noqa: BLE001 - cleanup must continue
            ok = False
            diagnostics.append(
                f"controller workspace cleanup failed: {_diagnostic(exc, root)}"
            )
    try:
        if case_parent.exists():
            shutil.rmtree(case_parent)
        if case_parent.exists():
            ok = False
            diagnostics.append("case workspace parent still exists after cleanup")
    except BaseException as exc:  # noqa: BLE001 - cleanup must continue
        ok = False
        diagnostics.append(f"case workspace cleanup failed: {_diagnostic(exc, root)}")
    return ok


def _assemble_case(
    *,
    task_id: str,
    policy: DemoPolicy,
    pdb_mode: Any,
    task: Any,
    status: DemoCaseStatus,
    controller_result: Optional[ControllerRunResult],
    context: Optional[DemoToolContext],
    model_used: Any,
    evaluation: Any,
    verifier_note: Optional[str],
    semantic_events: tuple[dict[str, Any], ...],
    events_jsonl: str,
    replay_error: Optional[str],
    patch_text: str,
    diagnostics: list[str],
    wall_clock_ms: int,
    offline_ledger: Any,
    fixture_before: str,
    fixture_after: Optional[str],
) -> DemoCaseResult:
    tool_calls = tuple(context.tool_calls) if context else ()
    gate_records = tuple(item.to_mapping() for item in model_used.gate_records) if model_used else ()
    gate_reached = bool(gate_records)
    gate_allowed = bool(gate_records and gate_records[-1]["allowed"])

    declared = dict(context.declared_localization) if context and context.declared_localization else None
    changed_files = list(context.patch_changed_files) if context else []
    patch_applied = bool(context.patch_applied) if context else False

    localization = localization_record(
        declared,
        changed_files,
        patch_applied,
        task.oracle.target_files,
        task.oracle.target_symbols,
    )
    pdb_actions = sum(1 for name in tool_calls if name in _PDB_ACTION_NAMES)
    pdb_observations = sum(1 for name in tool_calls if name in _PDB_OBSERVATION_ACTION_NAMES)
    evidence_collected = bool(model_used.runtime_evidence_collected) if model_used else False
    session_started = bool(context.pdb_session_started) if context else False
    runtime_outcome = classify_runtime_evidence(
        gate_reached=gate_reached,
        gate_allowed=gate_allowed,
        session_started=session_started,
        evidence_collected=evidence_collected,
        proof_required=bool(
            context
            and context.probe is not None
            and context.probe.exact_public_reproduction
        ),
    )

    controller_record = (
        _controller_record(controller_result, tool_calls)
        if controller_result is not None
        else {
            "final_state": None,
            "stop_reason": None,
            "model_calls": 0,
            "step_count": 0,
            "tool_call_count": len(tool_calls),
            "tool_calls": list(tool_calls),
            "budget_state": None,
            "states_visited": [],
        }
    )

    return DemoCaseResult(
        task_id=task_id,
        policy=policy.value,
        pdb_policy=pdb_mode.value,
        status=status,
        controller=controller_record,
        gate_decisions=gate_records,
        localization=localization,
        patch={
            "source": "offline_catalog_reference_repair",
            "attempted": "apply_patch" in tool_calls,
            "applied": patch_applied,
            "changed_files": changed_files,
            "sha256": hashlib.sha256(patch_text.encode("utf-8")).hexdigest() if patch_text else None,
            "line_count": patch_text.count("\n") if patch_text else 0,
            "syntax_passed": context.syntax_passed if context else None,
        },
        runtime_evidence={
            "policy": pdb_mode.value,
            "gate_reached": gate_reached,
            "gate_allowed": gate_allowed,
            "gate_reason": gate_records[-1]["reason"] if gate_records else None,
            "session_started": session_started,
            "pdb_action_count": pdb_actions,
            # Attempted inspection calls, counted before dispatch.
            "pdb_observation_attempts": pdb_observations,
            # Inspection calls whose handler actually returned evidence.
            "pdb_observations_succeeded": len(context.pdb_observation_names) if context else 0,
            "pdb_observation_names": list(context.pdb_observation_names) if context else [],
            "evidence_collected": evidence_collected,
            "outcome": runtime_outcome.value,
            "diagnosis_change_attributable_to_runtime_evidence": False,
            "attribution_note": RUNTIME_ATTRIBUTION_NOTE,
        },
        controller_validation={
            "baseline_failure_reproduced": context.baseline_failure_reproduced if context else None,
            "post_patch_f2p_passed": context.post_patch_f2p_passed if context else None,
            "designated_regression_passed": context.regression_passed if context else None,
            "outcome": context.controller_outcome if context else None,
            "abort_reason": model_used.abort_reason if model_used else None,
        },
        verifier=_verifier_record(evaluation, verifier_note),
        trajectory=_trajectory_record(semantic_events, replay_error),
        tool_errors=tuple(dict(item) for item in (context.tool_errors if context else [])),
        offline={
            **offline_ledger.to_mapping(),
            "guard_installed": True,
            "guard_scope": guard_scope_note(),
            "canonical_fixture_sha256_before_controller": fixture_before,
            "canonical_fixture_sha256_after_controller": fixture_after,
            "canonical_fixture_unchanged_by_controller": None
            if fixture_after is None
            else fixture_after == fixture_before,
        },
        diagnostics=tuple(diagnostics),
        wall_clock_ms=wall_clock_ms,
        semantic_events=semantic_events,
        events_jsonl=events_jsonl,
        retrieval=model_used.retrieval_record if model_used else None,
    )


def run_demo(
    *,
    repository_root: str | Path,
    workspace_parent: str | Path,
    task_ids: Optional[Sequence[str]] = None,
    policies: Optional[Sequence[DemoPolicy]] = None,
) -> DemoReport:
    """Run every requested task/policy case and assemble the demo report."""

    repo = Path(repository_root).resolve()
    discovered = curated_task_ids(repo)
    selected = tuple(task_ids) if task_ids is not None else discovered
    unknown = sorted(set(selected) - set(discovered))
    if unknown:
        raise DemoInputError(f"unknown curated task ids: {unknown}")
    missing_scenarios = sorted(set(selected) - set(scenario_ids()))
    if missing_scenarios:
        raise DemoInputError(
            f"no demonstration scenario for curated tasks: {missing_scenarios}"
        )
    chosen_policies = tuple(policies) if policies is not None else DEMO_POLICIES
    if not chosen_policies:
        raise DemoInputError("at least one demonstration policy is required")

    cases: list[DemoCaseResult] = []
    for task_id in selected:
        for policy in chosen_policies:
            cases.append(
                run_demo_case(
                    repository_root=repo,
                    task_id=task_id,
                    policy=policy,
                    workspace_parent=workspace_parent,
                )
            )
    return DemoReport(environment=environment_record(repo), cases=tuple(cases))


def results_json(report: DemoReport) -> str:
    """Serialize the report deterministically apart from its marked sections."""

    return json.dumps(
        report.to_mapping(), ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True
    ) + "\n"


__all__ = [
    "CURATED_RELATIVE_ROOT",
    "DEMO_MAX_MODEL_CALLS",
    "DEMO_TOOL_VERSION",
    "NONDETERMINISTIC_RESULT_KEYS",
    "RESULTS_SCHEMA_VERSION",
    "RUNTIME_ATTRIBUTION_NOTE",
    "DemoCaseResult",
    "DemoCaseStatus",
    "DemoError",
    "DemoInputError",
    "DemoReport",
    "LocalizationOutcome",
    "RuntimeEvidenceOutcome",
    "classify_localization",
    "classify_runtime_evidence",
    "curated_task_ids",
    "deterministic_view",
    "environment_record",
    "fixture_digest",
    "localization_record",
    "results_json",
    "run_demo",
    "run_demo_case",
    "source_tree_digest",
]
