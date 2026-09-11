"""Deterministic demo case facts: digests, environment, classification.

This module owns the deterministic fact derivation beneath the demo
runner: repository/fixture digests (through explicit Git), the
environment record, localization and runtime-evidence classification,
and the controller/verifier/trajectory record projections.  Facts are
derived only from completed runs; nothing here invents or upgrades
evidence.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from agentic_debugger.agent.controller import ControllerRunResult
from agentic_debugger.agent.controller_policy import ControllerState
from agentic_debugger.demo.isolation import guard_scope_note
from agentic_debugger.demo.model import DEMO_MODEL_NAME
from agentic_debugger.demo.report_records import (
    LocalizationOutcome,
    RuntimeEvidenceOutcome,
    _bounded,
    _utc_now,
)
from agentic_debugger.evaluation.runner import normalize_output
from agentic_debugger.events.schema import EventType, ObservationStatus

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
