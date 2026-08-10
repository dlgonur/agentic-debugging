#!/usr/bin/env python3
"""D1 — Forced-Runtime-Entry Sanity Diagnostic runner.

This runner orchestrates the S1-D1 diagnostic (the ONE post-S1 STOP-gate
interface sanity diagnostic):

1. Load the frozen D1 contract (based on the accepted S1 source commit).
2. Validate contract identity, source provenance, and runtime Python.
3. Build the unchanged S1 pieces: task, workspace, PDB probe, tool registry
   (``PdbPolicy.ALWAYS_ON`` + interactive controls), ``LocalRawQwenTransport``,
   ``DebuggerBridgeAdapter``.
4. Wrap the adapter in the experiment-local ``D1PhaseNavigationAdapter``,
   which deterministically performs ONLY the administrative phase
   transitions ``REPRODUCE -> UNDERSTAND -> RUNTIME_EVIDENCE`` after a
   verified reproduction, then delegates all debugger/action choices to the
   model.
5. Run the unchanged ``DeterministicController``, project the trajectory,
   run the unchanged verifier if a patch is produced, write evidence, and
   clean up disposable state.

``--validate-only`` validates the D1 contract and identity WITHOUT loading
the model.  ``--run`` loads the real RAW Qwen2.5 transport and executes the
live diagnostic (requires GPU + authorization; NOT run in BUILD).

The runner is experiment-local.  It imports from the production
``agentic_debugger`` package and the frozen S1 experiment but modifies
neither.  The S1 Live Run 1 evidence is never overwritten or reinterpreted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path
from typing import Any, Optional

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.debugger_interaction_v2.adapter import DebuggerBridgeAdapter
from experiments.debugger_interaction_v2.bridge import SYSTEM_PROMPT
from experiments.debugger_interaction_v2.runner import (
    V1_BUDGETS,
    _build_task_description,
    _contract_sha256,
    _fixture_tree_sha256,
    _git_head,
    run_experiment,
)
from experiments.debugger_interaction_v2.transport import (
    BASE_REPOSITORY,
    BASE_REVISION,
    GENERATION_CONFIG,
    LocalRawQwenTransport,
)
from agentic_debugger.evaluation.runner import load_task

from experiments.debugger_interaction_v2_d1.d1_adapter import (
    D1PhaseNavigationAdapter,
)

CONTRACT_PATH = THIS_FILE.with_name("d1_contract.json")
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"

# Frozen D1 identity (must not drift).
D1_SCHEMA_VERSION = "debugger-interaction-v2-d1"
TASK_ID = "curated-off-by-one-002"

# Accepted S1 source commit that D1 descends from (Amendment 1).
S1_ACCEPTED_PARENT_COMMIT = "2d4bc14c16d1a7eb3e7fa72c8fbd23259cb5cc4f"

# Same frozen budgets as S1 / v1 (reused unchanged).
D1_BUDGETS = dict(V1_BUDGETS)


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


def _load_contract() -> dict[str, Any]:
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if value.get("schema_version") != D1_SCHEMA_VERSION:
        raise RuntimeError("unsupported D1 contract")
    return value


def _validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate the frozen D1 contract.

    Checks: schema version, model identity (identical to S1), task identity,
    budgets (identical to S1), D1 source baseline (accepted S1 parent
    commit), D1 treatment (administrative phase navigation only), fixture
    tree hash stability.
    """

    model = contract.get("model", {})
    if model.get("base_repository") != BASE_REPOSITORY:
        raise RuntimeError("model base_repository drift")
    if model.get("base_revision") != BASE_REVISION:
        raise RuntimeError("model base_revision drift")
    if model.get("adapter_applied") is not False:
        raise RuntimeError("D1 must be RAW base only (adapter_applied=false)")
    if model.get("rag_enabled") is not False:
        raise RuntimeError("D1 must have RAG OFF (rag_enabled=false)")

    gen = model.get("generation", {})
    if gen.get("do_sample") is not False:
        raise RuntimeError("generation.do_sample must be False")
    if gen.get("max_new_tokens") != 1024:
        raise RuntimeError("generation.max_new_tokens must be 1024 (frozen)")
    if gen.get("max_input_tokens") != 32768:
        raise RuntimeError("generation.max_input_tokens must be 32768 (frozen)")

    tasks = contract.get("tasks", [])
    if len(tasks) != 1 or tasks[0].get("task_id") != TASK_ID:
        raise RuntimeError(f"D1 must contain exactly task {TASK_ID!r}")

    budgets = contract.get("budgets", {})
    for key, expected in D1_BUDGETS.items():
        if budgets.get(key) != expected:
            raise RuntimeError(
                f"budget {key} drift: expected {expected}, got {budgets.get(key)}"
            )

    baseline = contract.get("source_baseline", {})
    if baseline.get("s1_accepted_parent_commit") != S1_ACCEPTED_PARENT_COMMIT:
        raise RuntimeError("D1 source_baseline.s1_accepted_parent_commit drift")

    treatment = contract.get("d1_treatment", {})
    if treatment.get("administrative_transitions_do_not_count_as_debugger_commands") is not True:
        raise RuntimeError("D1 contract must record the admin-transition exclusion rule")
    if treatment.get("automated_only") != [
        "REPRODUCE -> UNDERSTAND",
        "UNDERSTAND -> RUNTIME_EVIDENCE",
    ]:
        raise RuntimeError("D1 contract must record exactly the two administrative transitions")

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


# ---------------------------------------------------------------------------
# Run identity (D1-specific: source ancestry + runtime Python)
# ---------------------------------------------------------------------------


def _d1_run_identity(contract: dict[str, Any]) -> dict[str, Any]:
    model = contract["model"]
    return {
        "schema_version": "debugger-interaction-v2-d1-identity",
        "experiment_id": "debugger-interaction-v2-d1",
        "source_commit_sha": _git_head(REPO_ROOT),
        "source_ancestry": {
            "s1_accepted_parent_commit": contract["source_baseline"][
                "s1_accepted_parent_commit"
            ],
            "note": "D1 is based on the accepted S1 source commit; "
                    "source_commit_sha is the committed clean HEAD used for the run.",
        },
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
        "runtime_python": {
            "executable": sys.executable,
            "version": platform.python_version(),
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="D1 — Forced-Runtime-Entry Sanity Diagnostic"
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the D1 contract/identity without loading the model.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run the live D1 diagnostic (requires GPU + authorization; NOT run in BUILD).",
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
        identity = _d1_run_identity(contract)
        source_commit_sha = identity.get("source_commit_sha")
        validation = dict(validation)
        validation["source_commit_sha"] = source_commit_sha
        validation["source_ancestry"] = identity.get("source_ancestry")
        validation["experiment_contract_sha256"] = identity.get(
            "experiment_contract_sha256"
        )
        validation["runtime_python"] = identity.get("runtime_python")
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
        _validate_contract(contract)
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build the real transport (requires GPU).
        transport = LocalRawQwenTransport(
            max_new_tokens=GENERATION_CONFIG["max_new_tokens"],
            max_input_tokens=GENERATION_CONFIG["max_input_tokens"],
        )

        task = load_task(str(CURATED_ROOT / TASK_ID / "task.json"))
        task_desc = _build_task_description(task)

        inner_adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name=f"{BASE_REPOSITORY}+RAW-BASE",
            task_description=task_desc,
            max_retries=D1_BUDGETS["model_retries_per_logical_call_max"],
            request_timeout_seconds=D1_BUDGETS["model_request_timeout_seconds"],
        )
        adapter = D1PhaseNavigationAdapter(inner_adapter)

        # Reuse the S1 experiment execution path unchanged (adapter is the
        # only injection point).  The returned evidence dict is then
        # augmented with D1-specific identity/treatment/admin records.
        evidence = run_experiment(contract, adapter, output_dir)

        evidence["run_identity"] = _d1_run_identity(contract)
        evidence["d1_treatment"] = contract.get("d1_treatment")
        evidence["admin_transitions"] = adapter.admin_transitions
        evidence["claims_boundary"] = (
            "D1 is a single-run interface sanity diagnostic of the S1 STOP "
            "gate. It is NOT a new protocol campaign, NOT S1 v2.1, NOT a "
            "prompt-optimization campaign, and NOT a general repair pass. "
            "Only administrative REPRODUCE->UNDERSTAND->RUNTIME_EVIDENCE "
            "phase navigation after verified reproduction is automated; "
            "debugger commands remain model-authored."
        )
        evidence_path = output_dir / "evidence.json"
        evidence_path.write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )

        print(json.dumps({
            "status": "COMPLETE",
            "gate_b": evidence["gate_results"]["gate_b"],
            "gate_c": evidence["gate_results"]["gate_c"],
            "admin_transitions": adapter.admin_transitions,
            "evidence_path": str(evidence_path),
        }, indent=2, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
