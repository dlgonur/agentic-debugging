"""Generate and validate the frozen Pilot-10 / full-order artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_debugger.swerebench.authority import (
    B13_ALGORITHM,
    B13_SPLIT_SEED,
    CANONICAL_DATASET_ID,
    CANONICAL_DATASET_REVISION,
    CANONICAL_PARQUET_SHA256,
    CLEAN_LE32K_MASK_NAME,
    EXPERIMENT_ID,
    EXPERIMENT_SEED,
    EXPECTED_CD_CLEAN_LE32K,
    PARENT_BASELINE,
    HISTORICAL_GPT_OSS_PRODUCT_POLICY,
    HISTORICAL_GPT_OSS_PRODUCT_SESSION,
    HISTORICAL_GPT_OSS_PRODUCT_TASK,
    SELECTION_ALGORITHM_ID,
    frozen_dir,
)
from agentic_debugger.swerebench.hashing import (
    canonical_json_bytes,
    sha256_canonical_json,
    sha256_file,
)
from agentic_debugger.swerebench.population import load_clean_validation_population
from agentic_debugger.swerebench.provenance import frozen_harness_identity
from agentic_debugger.swerebench.schema import (
    PILOT_RESULT_SCHEMA_VERSION,
    POST_PILOT_FORMULAS,
)
from agentic_debugger.swerebench.selection import select_repo_diverse_ordering

EXECUTION_CONTRACT = {
    "schema_version": "gpt-oss-swerebench-v2-pilot10-execution-contract-v2",
    "experiment_id": EXPERIMENT_ID,
    "status": "REPAIRED_BEFORE_FIRST_PROVIDER_INFERENCE",
    "parent_baseline": PARENT_BASELINE,
    "scientific_questions": {
        "overall_repair": "independent verifier RESOLVED",
        "debugger_assisted_repair": (
            "actual PDB/runtime-debugger evidence exercised AND independently "
            "verified RESOLVED; PDB NOT EXERCISED is not debugger success"
        ),
    },
    "provider": {
        "route": "Ollama Cloud",
        "profile_id": "ollama-cloud-gpt-oss-20b",
        "display_name": "Ollama Cloud GPT-OSS 20B",
        "alias": "gpt-oss:20b-cloud",
        "upstream": "gpt-oss:20b",
        "adapter": "scripts/ollama_cloud_command_adapter.py",
        "protocol": "1.3",
        "adapter_retry_count": 0,
        "fallback_count": 0,
        "public_request_byte_budget": 25000,
        "adapter_logical_call_guidance": 25,
    },
    "controller": {
        "architecture": "single DeterministicController + typed tools + PatchManager",
        "policy": "pdb-on-uncertainty",
        "attempts_per_task": 1,
        "max_controller_steps": 64,
        "max_model_requests": 64,
        "live_run_limit_retries": 2,
        "max_patch_attempts": 3,
        "max_test_runs": 12,
        "max_pdb_observations": 8,
        "max_active_hypotheses": 3,
        "max_source_observations": 12,
        "justification": {
            "max_controller_steps": (
                "accepted Local Application configured_source default"
            ),
            "adapter_retry_count": "accepted Ollama Cloud adapter contract is zero retry",
            "live_run_limit_retries": (
                "accepted configured_source LiveRunLimits default; adapter itself "
                "still has zero provider retry/fallback"
            ),
            "max_patch_attempts": (
                "schema maximum 3; repository-scale diffs, not curated 2"
            ),
            "max_test_runs": (
                "raised from curated 5 to 12 within the schema cap of 20"
            ),
        },
    },
    "evaluation": {
        "correctness_authority": "official SWE-rebench Docker evaluator via OfficialSWERebenchVerifier",
        "fresh_external_application_root_per_task": True,
        "external_runtime_root": (
            "%LOCALAPPDATA%/agentic-debugging/gpt-oss-swerebench-v2-pilot10"
        ),
        "no_task_specific_prompt_customization": True,
        "no_outcome_dependent_retries": True,
        "no_campaign_modification_after_first_provider_result": True,
        "model_workspace": "base_commit only; no test_patch; no gold patch",
        "verifier_workspace": "base_commit + hidden test_patch; gold never applied as model input",
        "candidate_verifier_adapter": "agentic_debugger.swerebench.execution.OfficialSWERebenchVerifier",
        "candidate_verifier_input": "candidate unified diff only; private task identity/spec stays outside model context",
        "model_reproduction": (
            "not officially declared; model-selected public_target only; "
            "hidden FAIL_TO_PASS withheld"
        ),
        "pdb_treatment": (
            "Option B: Pilot-10 overall-repair treatment; PDB unavailable by "
            "treatment contract because the current launcher is not coupled to "
            "the failing public pytest runtime"
        ),
        "pdb_required_treatment": (
            "distinct frozen contract pdb-required-model-selected-target-v1; "
            "failing-runtime-coupled PDB required before debugger claims; not "
            "this Pilot-10"
        ),
        "contract_repaired_before_first_provider_inference": True,
    },
    "historical_product_evidence_excluded": {
        "session_id": HISTORICAL_GPT_OSS_PRODUCT_SESSION,
        "task_id": HISTORICAL_GPT_OSS_PRODUCT_TASK,
        "policy": HISTORICAL_GPT_OSS_PRODUCT_POLICY,
        "role": "historical product evidence only; not in Pilot-10 denominator",
        "pdb": "NOT EXERCISED",
    },
    "result_schema_version": PILOT_RESULT_SCHEMA_VERSION,
    "post_pilot_formulas": POST_PILOT_FORMULAS,
}


def freeze_population_and_order(output_dir: Path | None = None) -> dict[str, Any]:
    output = output_dir or frozen_dir()
    output.mkdir(parents=True, exist_ok=True)
    population = load_clean_validation_population()
    ordering = select_repo_diverse_ordering(population)
    population_doc = {
        "schema_version": "gpt-oss-swerebench-v2-clean-validation-population-v1",
        "experiment_id": EXPERIMENT_ID,
        "dataset": {
            "id": CANONICAL_DATASET_ID,
            "revision": CANONICAL_DATASET_REVISION,
            "parquet_sha256": CANONICAL_PARQUET_SHA256,
        },
        "split": {
            "algorithm": B13_ALGORITHM,
            "seed": B13_SPLIT_SEED,
            "canonical_validation_tasks": 150,
            "canonical_validation_repos": 40,
            "train_validation_repo_overlap": 0,
        },
        "authority_artifact": (
            "artifacts/swe_rebench_v2_corpus/runs/"
            "run_2026-08-08_rev-475dd5e8_b15_contract_v1/"
            + CLEAN_LE32K_MASK_NAME
        ),
        "eligibility_filters": list(population.eligibility_filters),
        "counts": dict(population.counts),
        "source_hashes": dict(population.source_hashes),
        "instance_ids": list(population.instance_ids),
        "repos": list(population.repos()),
        "excluded_from_this_population": [
            "training split",
            "unused remainder",
            "C/D-flagged validation ids",
            "overlength >32K validation ids",
            "oracle-localized B14/B15 SFT JSONL context",
            "historical curated GPT-OSS product session",
        ],
        "population_sha256": population.population_sha256,
    }
    full_order_doc = {
        "schema_version": "gpt-oss-swerebench-v2-full-ordering-v1",
        "experiment_id": EXPERIMENT_ID,
        "seed": ordering.seed,
        "algorithm": ordering.algorithm,
        "population_sha256": ordering.population_sha256,
        "n": len(ordering.entries),
        "entries": [entry.to_mapping() for entry in ordering.entries],
    }
    pilot = list(ordering.pilot10)
    pilot_doc = {
        "schema_version": "gpt-oss-swerebench-v2-pilot10-manifest-v1",
        "experiment_id": EXPERIMENT_ID,
        "frozen": True,
        "seed": EXPERIMENT_SEED,
        "algorithm": SELECTION_ALGORITHM_ID,
        "population_authority": CLEAN_LE32K_MASK_NAME,
        "population_sha256": population.population_sha256,
        "expected_population_n": EXPECTED_CD_CLEAN_LE32K,
        "n": 10,
        "distinct_repos": 10,
        "tasks": [entry.to_mapping() for entry in pilot],
        "selected_instance_ids": [entry.instance_id for entry in pilot],
        "selected_repos": [entry.repo for entry in pilot],
        "notes": [
            "Pilot-10 is exactly the first 10 entries of the frozen full ordering.",
            "Later 30-task or full-clean-validation expansions must continue this order.",
            "Do not re-sample after outcomes are observed.",
        ],
    }
    contract = dict(EXECUTION_CONTRACT)
    contract["harness"] = frozen_harness_identity()
    _write(output / "population.json", population_doc)
    _write(output / "full_ordering.json", full_order_doc)
    _write(output / "pilot10_manifest.json", pilot_doc)
    _write(output / "execution_contract.json", contract)
    hashes = {
        name: sha256_file(output / name)
        for name in (
            "population.json",
            "full_ordering.json",
            "pilot10_manifest.json",
            "execution_contract.json",
        )
    }
    hashes["pilot10_manifest_body"] = sha256_canonical_json(pilot_doc)
    _write(output / "artifact_hashes.json", hashes)
    return {
        "population": population_doc,
        "ordering": full_order_doc,
        "pilot10": pilot_doc,
        "hashes": hashes,
        "output_dir": str(output),
    }


def verify_frozen(output_dir: Path | None = None) -> dict[str, Any]:
    output = output_dir or frozen_dir()
    live = freeze_population_and_order(output_dir=output)
    expected = json.loads((output / "artifact_hashes.json").read_text(encoding="utf-8"))
    if live["hashes"] != expected:
        raise ValueError("frozen artifact hashes are not reproducible")
    return live


def _write(path: Path, payload: Any) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    path.write_bytes(text.encode("utf-8"))
