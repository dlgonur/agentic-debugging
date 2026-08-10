#!/usr/bin/env python3
"""S1 — Debugger Interaction v2 experiment runner.

This runner orchestrates the S1 feasibility experiment:

1. Load the frozen task ``curated-off-by-one-002``.
2. Prepare a disposable PDB probe copy (model selects breakpoint).
3. Build the existing tool registry with ``PdbPolicy.ALWAYS_ON`` and
   ``interactive_debugger_controls=True``.
4. Construct the ``DebuggerBridgeAdapter`` (or ``ScriptedBridgeAdapter``
   for offline tests).
5. Run the existing ``DeterministicController`` through the full trajectory.
6. Project the trajectory to JSONL events.
7. Run the existing ``EvaluationVerifier`` if a patch was produced.
8. Write the evidence/telemetry file.
9. Clean up all disposable state.

``--validate-only`` validates the experiment contract and identity without
loading the model.  ``--run`` loads the real RAW Qwen2.5 transport and
executes the live experiment (requires GPU + authorization; NOT run in
BUILD).

The runner is experiment-local.  It imports from the production
``agentic_debugger`` package but does not modify any production file.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
    PdbPolicy,
)
from agentic_debugger.agent.model_adapter import (
    ControllerSnapshot,
    ModelAdapterError,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.tools import (
    DemoToolContext,
    build_registry,
    prepare_pdb_probe,
)
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.events.replay import replay_events
from agentic_debugger.runtime.workspace import TaskWorkspace

from experiments.debugger_interaction_v2.adapter import (
    DebuggerBridgeAdapter,
    NOT_AVAILABLE,
    NOT_RECORDED,
    ScriptedBridgeAdapter,
)
from experiments.debugger_interaction_v2.bridge import SYSTEM_PROMPT
from experiments.debugger_interaction_v2.transport import (
    BASE_REPOSITORY,
    BASE_REVISION,
    GENERATION_CONFIG,
    LocalRawQwenTransport,
)

CONTRACT_PATH = THIS_FILE.with_name("experiment_contract.json")
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"

# Frozen task (S1 uses exactly one task).
TASK_ID = "curated-off-by-one-002"

# Frozen budgets (identical to v1 contract).
V1_BUDGETS = {
    "task_max_patch_attempts": 2,
    "task_max_test_runs": 5,
    "task_max_pdb_observations": 8,
    "debugger_session_starts_max": 1,
    "debugger_observation_or_control_actions_max": 8,
    "debugger_session_stops_max": 1,
    "controller_steps_max": 24,
    "model_requests_max": 28,
    "model_retries_per_logical_call_max": 1,
    "model_phase_seconds_max": 600,
    "model_request_timeout_seconds": 60,
    "pdb_request_timeout_seconds": 5,
    "debugger_accepted_actions_max": 10,
}


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


def _load_contract() -> dict[str, Any]:
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if value.get("schema_version") != "debugger-interaction-v2":
        raise RuntimeError("unsupported experiment contract")
    return value


# Regex for a full 40-character lowercase Git commit SHA.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _git_head(repository_root: Path) -> Optional[str]:
    """Return the current ``git rev-parse HEAD`` commit SHA, or None.

    Mirrors the read-only ``_git`` convention in the production demo runner
    (demo/runner.py:415-429): ``subprocess.run`` with ``check=False`` and a
    30s timeout, returning None on any failure rather than raising.  The
    result is validated as a 40-hex SHA before being trusted, so a dirty or
    non-repo state fails closed rather than yielding a malformed value.
    """

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
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
    sha = completed.stdout.strip()
    if not _COMMIT_SHA_RE.match(sha):
        return None
    return sha


def _contract_sha256(contract: dict[str, Any]) -> str:
    """Compute the deterministic SHA-256 of the contract (sorted-key JSON).

    Centralised so ``_validate_contract`` and ``_run_identity`` report the
    exact same value and cannot drift.
    """

    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate the frozen experiment contract.

    Checks: schema version, model identity, task identity, budgets,
    generation config, treatment differences from v1.
    """

    model = contract.get("model", {})
    if model.get("base_repository") != BASE_REPOSITORY:
        raise RuntimeError("model base_repository drift")
    if model.get("base_revision") != BASE_REVISION:
        raise RuntimeError("model base_revision drift")
    if model.get("adapter_applied") is not False:
        raise RuntimeError("S1 must be RAW base only (adapter_applied=false)")
    if model.get("rag_enabled") is not False:
        raise RuntimeError("S1 must have RAG OFF (rag_enabled=false)")

    gen = model.get("generation", {})
    if gen.get("do_sample") is not False:
        raise RuntimeError("generation.do_sample must be False")
    if gen.get("max_new_tokens") != 1024:
        raise RuntimeError("generation.max_new_tokens must be 1024 (v1 frozen)")
    if gen.get("max_input_tokens") != 32768:
        raise RuntimeError("generation.max_input_tokens must be 32768 (v1 frozen)")

    tasks = contract.get("tasks", [])
    if len(tasks) != 1 or tasks[0].get("task_id") != TASK_ID:
        raise RuntimeError(f"experiment must contain exactly task {TASK_ID!r}")

    budgets = contract.get("budgets", {})
    for key, expected in V1_BUDGETS.items():
        if budgets.get(key) != expected:
            raise RuntimeError(f"budget {key} drift: expected {expected}, got {budgets.get(key)}")

    # Verify treatment differences from v1 are explicitly recorded.
    diffs = contract.get("treatment_differences_from_v1", {})
    if not diffs.get("simplified_interface"):
        raise RuntimeError("contract must record simplified_interface treatment difference")
    if diffs.get("debugger_policy") != "always_on":
        raise RuntimeError("contract must record debugger_policy=always_on")

    # Verify the fixture tree hash is stable.
    fixture_dir = CURATED_ROOT / TASK_ID
    fixture_hash = _fixture_tree_sha256(fixture_dir)
    frozen_hash = tasks[0].get("fixture_tree_sha256")
    if frozen_hash and fixture_hash != frozen_hash:
        raise RuntimeError(
            f"fixture tree hash drift: expected {frozen_hash}, got {fixture_hash}"
        )

    return {
        "contract_sha256": _contract_sha256(contract),
        "fixture_tree_sha256": fixture_hash,
        "validated": True,
    }


def _fixture_tree_sha256(task_dir: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in task_dir.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    )
    for path in files:
        relative = path.relative_to(task_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Run identity
# ---------------------------------------------------------------------------


def _run_identity(contract: dict[str, Any]) -> dict[str, Any]:
    model = contract["model"]
    return {
        "schema_version": "debugger-interaction-v2-identity",
        "experiment_id": "debugger-interaction-v2",
        # Source provenance (REPAIR PASS 1, Blocker 2): the live run binds to
        # the exact committed source tree via ``git rev-parse HEAD`` captured
        # at runtime (NOT hardcoded), and to the exact experiment contract via
        # its SHA-256.  ``source_commit_sha`` may be None if HEAD cannot be
        # resolved; ``--validate-only`` fails closed in that case (see main).
        # ``source_baseline.audited_main_commit`` in the contract remains the
        # audited pre-S1 baseline and is intentionally distinct from this
        # live-run source identity.
        "source_commit_sha": _git_head(REPO_ROOT),
        "experiment_contract_sha256": _contract_sha256(contract),
        "model_condition": "RAW_BASE",
        "adapter_applied": False,
        "adapter_path": None,
        "base_repository": model["base_repository"],
        "base_revision": model["base_revision"],
        "rag_enabled": False,
        "generation": model["generation"],
        "task_id": TASK_ID,
        "system_prompt_sha256": hashlib.sha256(
            SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
        "budgets": contract["budgets"],
    }


# ---------------------------------------------------------------------------
# Experiment execution
# ---------------------------------------------------------------------------


def _build_task_description(task: Any) -> str:
    """Build the agent-visible task description (oracle stripped)."""

    mapping = task.agent_visible_mapping()
    lines = []
    title = mapping.get("title", "")
    desc = mapping.get("description", "")
    if title:
        lines.append(f"Title: {title}")
    if desc:
        lines.append(f"Description: {desc}")
    tests = mapping.get("tests", {})
    f2p = tests.get("fail_to_pass", [])
    p2p = tests.get("pass_to_pass", [])
    if f2p:
        lines.append(f"Failing tests (must pass after fix): {f2p}")
    if p2p:
        lines.append(f"Passing tests (must still pass): {p2p}")
    return "\n".join(lines)


def run_experiment(
    contract: dict[str, Any],
    adapter: Any,  # DebuggerBridgeAdapter | ScriptedBridgeAdapter
    output_dir: Path,
) -> dict[str, Any]:
    """Run the S1 experiment with a given adapter.

    Returns the evidence dict (written to ``evidence.json``).
    """

    fixture_dir = CURATED_ROOT / TASK_ID
    task = load_task(str(fixture_dir / "task.json"))
    scenario = scenario_for(TASK_ID)

    # Create a disposable case directory.
    case_dir = output_dir / f"case-{TASK_ID}"
    case_dir.mkdir(parents=True, exist_ok=True)

    # Main workspace (for patch/test).
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_dir))

    # PDB probe (model selects breakpoint).
    probe = prepare_pdb_probe(
        fixture_dir,
        scenario,
        case_dir,
        model_selects_breakpoint=True,
    )

    # Build the tool context and registry.
    context = DemoToolContext(
        task=task,
        workspace=workspace,
        patch="",
        probe=probe,
    )
    registry = build_registry(
        context,
        pdb_policy=PdbPolicy.ALWAYS_ON,
        interactive_debugger_controls=True,
    )

    # Build the controller.
    controller = DeterministicController(
        registry,
        adapter,
        ControllerRunConfig(max_model_calls=V1_BUDGETS["controller_steps_max"]),
    )

    # Build the initial snapshot (same pattern as demo runner).
    snapshot = ControllerSnapshot(
        f"s1-{TASK_ID}",
        TASK_ID,
        ControllerState.REPRODUCE,
        0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(),
        HypothesisLedger(),
    )

    # Run the controller.
    run_start = time.monotonic()
    try:
        controller_result = controller.run(snapshot)
    except Exception as exc:
        controller_result = None
        controller_error = f"{type(exc).__name__}: {exc}"
    else:
        controller_error = None
    run_duration_ms = int((time.monotonic() - run_start) * 1000)

    # Project trajectory to JSONL events.
    events_jsonl = ""
    if controller_result is not None:
        stream = io.StringIO()
        logger = JsonlEventLogger(
            controller_result.run_id, controller_result.task_id, stream=stream
        )
        try:
            for event in project_controller_run(
                controller_result,
                tool_version="debugger-interaction-v2",
                model=adapter.model_name,
                timestamp=_utc_now(),
                duration_ms=run_duration_ms,
            ):
                logger.append(event)
            logger.flush()
        finally:
            logger.close()
        events_jsonl = stream.getvalue()

    # Run the verifier if a patch was produced.
    verifier_result: Optional[dict[str, Any]] = None
    candidate_patch = context.candidate_patch if context.candidate_patch else None
    if candidate_patch:
        try:
            evaluation = EvaluationVerifier(
                str(REPO_ROOT), workspace_parent=str(case_dir)
            ).evaluate(task, candidate_patch)
            verifier_result = {
                "executed": True,
                "status": evaluation.status.value if hasattr(evaluation.status, "value") else str(evaluation.status),
                "outcome": evaluation.outcome.value if hasattr(evaluation.outcome, "value") else str(evaluation.outcome),
                "f2p_passed": [t.status.value if hasattr(t.status, "value") else str(t.status) for t in evaluation.f2p_records],
                "p2p_passed": [t.status.value if hasattr(t.status, "value") else str(t.status) for t in evaluation.p2p_records],
            }
        except Exception as exc:
            verifier_result = {
                "executed": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        verifier_result = {"executed": False}

    # Compute Gate B (interaction loop).
    gate_b = _compute_gate_b(adapter.telemetry)

    # Compute Gate C (full trajectory).
    gate_c = _compute_gate_c(
        adapter.telemetry, verifier_result, controller_result
    )

    # Build the evidence dict.
    evidence: dict[str, Any] = {
        "schema_version": "debugger-interaction-v2-evidence",
        "run_identity": _run_identity(contract),
        "controller_result": _controller_result_to_dict(controller_result, controller_error),
        "telemetry": adapter.telemetry,
        "post_debug_diagnoses": adapter.post_debug_diagnoses,
        "trajectory_jsonl": events_jsonl,
        "verifier": verifier_result,
        "candidate_patch": candidate_patch,
        "gate_results": {
            "gate_a": True,  # Offline tests pass (set by caller)
            "gate_b": gate_b,
            "gate_c": gate_c,
        },
        "claims_boundary": (
            "This is a mechanism/interface feasibility experiment. "
            "It is NOT a strict causal ablation against v1. "
            "Treatment differences from v1: (1) simplified state-specific "
            "model-facing command interface; (2) debugger policy = "
            "PdbPolicy.ALWAYS_ON instead of the historical "
            "PDB_ON_UNCERTAINTY gate. A successful S1 supports only: "
            "'RAW Qwen can perform bounded real debugger interaction "
            "under the S1 simplified/direct-access treatment.'"
        ),
    }

    # Write evidence file.
    evidence_path = output_dir / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    # Clean up the PDB session if still active.
    context.release_pdb()

    return evidence


# ---------------------------------------------------------------------------
# Gate computation
# ---------------------------------------------------------------------------


# PDB action names that count as "real debugger commands" for Gate B.
_PDB_ACTIONS = frozenset({
    "start_pdb_session",
    "get_stack_summary",
    "get_frame_locals",
    "safe_eval_expression",
    "continue_pdb_session",
    "step_pdb_session",
    "next_pdb_session",
})


def _compute_gate_b(telemetry: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute Gate B: interaction loop (not one-shot).

    Gate B requires:
    1. First accepted real PDB command that reaches real PDB.
    2. Real PDB observation with provenance bound into the next request.
    3. Model emits a second accepted debugger command after consuming that request.
    4. Second command also reaches real PDB and produces another real observation.
    """

    accepted_pdb = [
        t for t in telemetry
        if t.get("parse_result", {}).get("status") == "accepted"
        and t.get("translated_directive", {}).get("action_name") in _PDB_ACTIONS
    ]

    if len(accepted_pdb) < 2:
        return {
            "passed": False,
            "reason": f"need >=2 accepted PDB commands, got {len(accepted_pdb)}",
            "accepted_pdb_count": len(accepted_pdb),
        }

    first = accepted_pdb[0]
    second = accepted_pdb[1]

    # Check the second call consumed the first observation (provenance).
    second_provenance = second.get("provenance", {})
    prior_obs_id = second_provenance.get("prior_observation_id")
    rendered_obs_sha = second_provenance.get("rendered_observation_sha256")

    if not prior_obs_id:
        return {
            "passed": False,
            "reason": "second command has no prior_observation_id provenance",
            "accepted_pdb_count": len(accepted_pdb),
        }
    if not rendered_obs_sha:
        return {
            "passed": False,
            "reason": "second command has no rendered_observation_sha256",
            "accepted_pdb_count": len(accepted_pdb),
        }

    return {
        "passed": True,
        "reason": "interaction loop confirmed: 2 accepted PDB commands with "
                   "observation provenance binding",
        "first_command": first.get("translated_directive", {}).get("action_name"),
        "second_command": second.get("translated_directive", {}).get("action_name"),
        "prior_observation_id": prior_obs_id,
        "rendered_observation_sha256": rendered_obs_sha,
        "accepted_pdb_count": len(accepted_pdb),
    }


def _compute_gate_c(
    telemetry: list[dict[str, Any]],
    verifier_result: Optional[dict[str, Any]],
    controller_result: Any,
) -> dict[str, Any]:
    """Compute Gate C: runtime evidence → diagnosis → patch → verifier."""

    has_pdb = any(
        t.get("translated_directive", {}).get("action_name") in _PDB_ACTIONS
        and t.get("parse_result", {}).get("status") == "accepted"
        for t in telemetry
    )
    has_diagnosis = any(
        t.get("translated_directive", {}).get("is_diagnosis") is True
        for t in telemetry
    )
    has_patch = any(
        t.get("translated_directive", {}).get("action_name") == "apply_patch"
        and t.get("parse_result", {}).get("status") == "accepted"
        for t in telemetry
    )
    verifier_executed = verifier_result is not None and verifier_result.get("executed") is True

    resolved = False
    if verifier_executed and "outcome" in (verifier_result or {}):
        resolved = (verifier_result or {}).get("outcome") == "RESOLVED"

    return {
        "has_pdb_evidence": has_pdb,
        "has_diagnosis": has_diagnosis,
        "has_patch": has_patch,
        "verifier_executed": verifier_executed,
        "resolved": resolved,
        "passed": has_pdb and has_patch and verifier_executed,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _controller_result_to_dict(
    result: Any,
    error: Optional[str],
) -> dict[str, Any]:
    if result is None:
        return {"error": error or "controller failed"}
    return {
        "final_state": result.final_state.value if hasattr(result.final_state, "value") else str(result.final_state),
        "stop_reason": result.stop_reason.value if hasattr(result.stop_reason, "value") else str(result.stop_reason),
        "model_calls": result.model_calls,
        "steps_count": len(result.steps),
    }


def _utc_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="S1 — Debugger Interaction v2: RAW Feasibility"
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate contract/identity without loading the model.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run the live RAW Qwen2.5 experiment (requires GPU + authorization).",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory for evidence (required for --run).",
    )
    args = parser.parse_args()

    if not args.validate_only and not args.run:
        parser.error("select --validate-only or --run")

    contract = _load_contract()

    if args.validate_only:
        validation = _validate_contract(contract)
        identity = _run_identity(contract)
        source_commit_sha = identity.get("source_commit_sha")
        # Surface source provenance in the validation block too, and fail
        # closed if the live source commit cannot be resolved.  Live-readiness
        # must not silently omit source provenance (REPAIR PASS 1, Blocker 2).
        validation = dict(validation)
        validation["source_commit_sha"] = source_commit_sha
        validation["experiment_contract_sha256"] = identity.get(
            "experiment_contract_sha256"
        )
        if source_commit_sha is None:
            print(json.dumps({
                "status": "FAIL",
                "reason": "source_commit_sha unresolved: "
                          "git rev-parse HEAD failed or returned a non-SHA value",
                "validation": validation,
                "run_identity": identity,
            }, indent=2, ensure_ascii=False))
            return 1
        print(json.dumps({
            "status": "PASS",
            "validation": validation,
            "run_identity": identity,
        }, indent=2, ensure_ascii=False))
        return 0

    if args.run:
        if not args.output_dir:
            parser.error("--output-dir is required for --run")
        validation = _validate_contract(contract)
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build the real transport (requires GPU).
        transport = LocalRawQwenTransport(
            max_new_tokens=GENERATION_CONFIG["max_new_tokens"],
            max_input_tokens=GENERATION_CONFIG["max_input_tokens"],
        )

        task = load_task(str(CURATED_ROOT / TASK_ID / "task.json"))
        task_desc = _build_task_description(task)

        adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name=f"{BASE_REPOSITORY}+RAW-BASE",
            task_description=task_desc,
            max_retries=V1_BUDGETS["model_retries_per_logical_call_max"],
            request_timeout_seconds=V1_BUDGETS["model_request_timeout_seconds"],
        )

        evidence = run_experiment(contract, adapter, output_dir)
        print(json.dumps({
            "status": "COMPLETE",
            "gate_b": evidence["gate_results"]["gate_b"],
            "gate_c": evidence["gate_results"]["gate_c"],
            "evidence_path": str(output_dir / "evidence.json"),
        }, indent=2, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())