#!/usr/bin/env python3
"""S2 — cp118 on the frozen D1 runtime-entry treatment: runner.

S2 is the single professor-critical model-condition diagnostic: it repeats
the frozen D1 treatment EXACTLY (deterministic administrative phase
navigation ``REPRODUCE -> UNDERSTAND -> RUNTIME_EVIDENCE`` after a real
``failure_reproduced == true`` observation, then the unchanged S1
RUNTIME_EVIDENCE model-facing surface) with the ONLY material model-condition
change being

    RAW Qwen2.5-Coder-7B-Instruct  ->  definitive cp118 tuned checkpoint

This runner orchestrates the diagnostic:

1. Load the frozen S2 contract.
2. Validate contract identity, source ancestry (S1 parent + D1 source
   commit), budgets, task, fixture tree, generation, and — fail closed —
   the on-disk cp118 adapter identity against the frozen contract identity.
3. Build the unchanged S1 pieces: task, workspace, PDB probe, tool registry
   (``PdbPolicy.ALWAYS_ON`` + interactive controls), ``DebuggerBridgeAdapter``
   with the frozen S1 RUNTIME_EVIDENCE surface.
4. Wrap the adapter in the unchanged D1 ``D1PhaseNavigationAdapter``
   (automates ONLY the two administrative transitions after verified
   reproduction; every debugger/action choice stays model-authored).
5. Drive the unchanged ``DeterministicController`` through the unchanged S1
   ``run_experiment`` path, project the trajectory, run the unchanged
   verifier if a patch is produced, write evidence, and clean up disposable
   state.
6. Report BOTH Gate B legacy (existing computation, unchanged) and Gate B
   strict (six-condition real iterative loop), plus Gate C.

``--validate-only`` validates contract + adapter identity WITHOUT loading
the model.  ``--run`` loads the real cp118 transport and executes the ONE
live diagnostic (requires GPU + authorization).

The runner is experiment-local.  It imports from the production
``agentic_debugger`` package and the frozen S1/D1 experiments but modifies
neither.  The D1 Live Run evidence is never overwritten or reinterpreted.
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
)
from agentic_debugger.evaluation.runner import load_task

from experiments.debugger_interaction_v2_d1.d1_adapter import (
    D1PhaseNavigationAdapter,
)
from experiments.debugger_interaction_v2_s2_cp118.s2_gates import (
    compute_gate_b_legacy,
    compute_gate_b_strict,
    observation_status_map,
)
from experiments.debugger_interaction_v2_s2_cp118.s2_transport import (
    LocalCp118QwenTransport,
    verify_adapter_identity,
)

CONTRACT_PATH = THIS_FILE.with_name("s2_contract.json")
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"

# Frozen S2 identity (must not drift).
S2_SCHEMA_VERSION = "debugger-interaction-v2-s2-cp118"
TASK_ID = "curated-off-by-one-002"

# Accepted ancestry: S1 accepted parent + D1 source commit (S2 parent).
S1_ACCEPTED_PARENT_COMMIT = "2d4bc14c16d1a7eb3e7fa72c8fbd23259cb5cc4f"
D1_SOURCE_COMMIT = "7bda64d04a6165eb47bfb232094976e82e1155ed"

# Same frozen budgets as S1 / D1 (reused unchanged).
S2_BUDGETS = dict(V1_BUDGETS)


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


def _load_contract() -> dict[str, Any]:
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if value.get("schema_version") != S2_SCHEMA_VERSION:
        raise RuntimeError("unsupported S2 contract")
    return value


def _validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate the frozen S2 contract.

    Checks: schema version, model identity (base identical to S1/D1,
    adapter_applied true with a frozen adapter identity block), task
    identity, budgets (identical to D1), source ancestry (S1 parent + D1
    source commit), S2 treatment (D1 administrative phase navigation only,
    model-condition change only), patch policy (no normalizer), fixture tree
    hash stability.
    """

    model = contract.get("model", {})
    if model.get("base_repository") != BASE_REPOSITORY:
        raise RuntimeError("model base_repository drift")
    if model.get("base_revision") != BASE_REVISION:
        raise RuntimeError("model base_revision drift")
    if model.get("adapter_applied") is not True:
        raise RuntimeError(
            "S2 must apply the cp118 adapter (adapter_applied=true)"
        )
    adapter_identity = model.get("adapter_identity")
    if not isinstance(adapter_identity, dict) or not adapter_identity.get("files"):
        raise RuntimeError("S2 contract must freeze the cp118 adapter identity")
    if model.get("rag_enabled") is not False:
        raise RuntimeError("S2 must have RAG OFF (rag_enabled=false)")

    gen = model.get("generation", {})
    if gen.get("do_sample") is not False:
        raise RuntimeError("generation.do_sample must be False")
    if gen.get("max_new_tokens") != 1024:
        raise RuntimeError("generation.max_new_tokens must be 1024 (frozen)")
    if gen.get("max_input_tokens") != 32768:
        raise RuntimeError("generation.max_input_tokens must be 32768 (frozen)")

    tasks = contract.get("tasks", [])
    if len(tasks) != 1 or tasks[0].get("task_id") != TASK_ID:
        raise RuntimeError(f"S2 must contain exactly task {TASK_ID!r}")

    budgets = contract.get("budgets", {})
    for key, expected in S2_BUDGETS.items():
        if budgets.get(key) != expected:
            raise RuntimeError(
                f"budget {key} drift: expected {expected}, got {budgets.get(key)}"
            )

    baseline = contract.get("source_baseline", {})
    if baseline.get("s1_accepted_parent_commit") != S1_ACCEPTED_PARENT_COMMIT:
        raise RuntimeError(
            "S2 source_baseline.s1_accepted_parent_commit drift"
        )
    if baseline.get("d1_source_commit") != D1_SOURCE_COMMIT:
        raise RuntimeError("S2 source_baseline.d1_source_commit drift")

    treatment = contract.get("s2_treatment", {})
    if treatment.get(
        "administrative_transitions_do_not_count_as_debugger_commands"
    ) is not True:
        raise RuntimeError(
            "S2 contract must record the admin-transition exclusion rule"
        )
    if treatment.get("automated_only") != [
        "REPRODUCE -> UNDERSTAND",
        "UNDERSTAND -> RUNTIME_EVIDENCE",
    ]:
        raise RuntimeError(
            "S2 contract must record exactly the two administrative transitions"
        )

    diff = contract.get("treatment_differences_from_d1", {})
    if diff.get("everything_else_unchanged") is not True:
        raise RuntimeError(
            "S2 contract must record that only the model condition changes"
        )

    patch_policy = contract.get("patch_policy", {})
    if patch_policy.get("patch_normalizer_applied") is not False:
        raise RuntimeError(
            "S2 contract must record that no patch normalizer is applied "
            "(S1-P serialization normalization is NOT part of S2)"
        )

    # Structural consistency of the frozen adapter identity block.  The
    # authoritative byte-exact verification against the on-disk checkpoint
    # is done by ``verify_adapter_identity`` (which uses the established
    # Path-based tree convention of the accepted tuned pilot, recorded
    # verbatim as ``tree_identity_sha256``); this check only guards the
    # frozen block itself (unique paths, well-formed digests/sizes).
    files = adapter_identity.get("files", [])
    if not files:
        raise RuntimeError("S2 contract adapter identity file list is empty")
    paths = [f.get("path") for f in files]
    if any(not isinstance(p, str) or not p for p in paths):
        raise RuntimeError(
            "S2 contract adapter identity contains a malformed path"
        )
    if len(set(paths)) != len(paths):
        raise RuntimeError(
            "S2 contract adapter identity file list has duplicate paths"
        )
    for item in files:
        digest = item.get("sha256")
        size = item.get("size_bytes")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
        ):
            raise RuntimeError(
                "S2 contract adapter identity contains a malformed sha256"
            )
        if not isinstance(size, int) or size <= 0:
            raise RuntimeError(
                "S2 contract adapter identity contains a malformed size_bytes"
            )

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
# Run identity (S2-specific: source ancestry + adapter identity + runtime)
# ---------------------------------------------------------------------------


def _s2_run_identity(
    contract: dict[str, Any],
    on_disk_adapter_identity: dict[str, Any],
) -> dict[str, Any]:
    model = contract["model"]
    return {
        "schema_version": "debugger-interaction-v2-s2-cp118-identity",
        "experiment_id": "debugger-interaction-v2-s2-cp118",
        "source_commit_sha": _git_head(REPO_ROOT),
        "source_ancestry": {
            "s1_accepted_parent_commit": contract["source_baseline"][
                "s1_accepted_parent_commit"
            ],
            "d1_source_commit": contract["source_baseline"]["d1_source_commit"],
            "note": "S2 is based on the accepted D1 source commit; "
                    "source_commit_sha is the committed clean HEAD used for the run.",
        },
        "experiment_contract_sha256": _contract_sha256(contract),
        "model_condition": "CP118_TUNED",
        "adapter_applied": True,
        "adapter_identity": on_disk_adapter_identity,
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
        description="S2 — cp118 on the frozen D1 runtime-entry treatment"
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the S2 contract + on-disk cp118 adapter identity "
             "without loading the model.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run the ONE live cp118 diagnostic (requires GPU + authorization).",
    )
    parser.add_argument(
        "--adapter-path",
        type=str,
        default=None,
        help="Path to the definitive cp118 adapter directory (required in "
             "both modes; verified byte-exact against the frozen contract).",
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
    if not args.adapter_path:
        parser.error("--adapter-path is required (definitive cp118 checkpoint)")

    contract = _load_contract()
    expected_adapter_identity = contract["model"]["adapter_identity"]

    try:
        on_disk_adapter_identity = verify_adapter_identity(
            Path(args.adapter_path), expected_adapter_identity
        )
    except RuntimeError as exc:
        print(json.dumps({
            "status": "FAIL",
            "reason": f"cp118 checkpoint verification failed: {exc}",
        }, indent=2, ensure_ascii=False))
        return 1

    if args.validate_only:
        try:
            validation = _validate_contract(contract)
        except RuntimeError as exc:
            print(json.dumps({
                "status": "FAIL",
                "reason": str(exc),
                "adapter_identity": on_disk_adapter_identity,
            }, indent=2, ensure_ascii=False))
            return 1
        identity = _s2_run_identity(contract, on_disk_adapter_identity)
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
        try:
            _validate_contract(contract)
        except RuntimeError as exc:
            print(json.dumps({
                "status": "FAIL",
                "reason": str(exc),
            }, indent=2, ensure_ascii=False))
            return 1
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build the real cp118 transport (requires GPU).  The adapter
        # identity is re-verified fail-closed inside the transport before
        # the GPU load.
        transport = LocalCp118QwenTransport(
            adapter_path=args.adapter_path,
            expected_adapter_identity=expected_adapter_identity,
            max_new_tokens=GENERATION_CONFIG["max_new_tokens"],
            max_input_tokens=GENERATION_CONFIG["max_input_tokens"],
        )

        task = load_task(str(CURATED_ROOT / TASK_ID / "task.json"))
        task_desc = _build_task_description(task)

        inner_adapter = DebuggerBridgeAdapter(
            transport=transport,
            model_name=f"{BASE_REPOSITORY}+CP118-TUNED",
            task_description=task_desc,
            max_retries=S2_BUDGETS["model_retries_per_logical_call_max"],
            request_timeout_seconds=S2_BUDGETS["model_request_timeout_seconds"],
        )
        adapter = D1PhaseNavigationAdapter(inner_adapter)

        # Reuse the S1 experiment execution path unchanged (adapter is the
        # only injection point).  The returned evidence dict is then
        # augmented with S2-specific identity/treatment/admin/gate records.
        evidence = run_experiment(contract, adapter, output_dir)

        evidence["run_identity"] = _s2_run_identity(
            contract, on_disk_adapter_identity
        )
        evidence["s2_treatment"] = contract.get("s2_treatment")
        evidence["treatment_differences_from_d1"] = contract.get(
            "treatment_differences_from_d1"
        )
        evidence["patch_policy"] = contract.get("patch_policy")
        evidence["admin_transitions"] = adapter.admin_transitions
        evidence["gate_b_legacy"] = compute_gate_b_legacy(
            evidence["telemetry"]
        )
        evidence["gate_b_strict"] = compute_gate_b_strict(
            evidence["telemetry"],
            observation_status_map(evidence["trajectory_jsonl"]),
        )
        evidence["claims_boundary"] = (
            "S2 is a single-run model-condition diagnostic. It is NOT a new "
            "protocol campaign, NOT an interface repair, NOT a "
            "prompt-optimization campaign, and NOT a general repair pass. "
            "Only administrative REPRODUCE->UNDERSTAND->RUNTIME_EVIDENCE "
            "phase navigation after verified reproduction is automated "
            "(unchanged D1 harness); debugger commands remain "
            "model-authored. The only material model-condition change vs "
            "D1 is RAW Qwen2.5-Coder-7B-Instruct -> the definitive cp118 "
            "tuned checkpoint. Do NOT infer broad fine-tuning conclusions "
            "from one task."
        )
        evidence_path = output_dir / "evidence.json"
        evidence_path.write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )

        print(json.dumps({
            "status": "COMPLETE",
            "gate_b_legacy": evidence["gate_b_legacy"],
            "gate_b_strict": evidence["gate_b_strict"],
            "gate_c": evidence["gate_results"]["gate_c"],
            "admin_transitions": adapter.admin_transitions,
            "evidence_path": str(evidence_path),
        }, indent=2, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
