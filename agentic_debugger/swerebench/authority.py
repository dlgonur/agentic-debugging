"""Pinned SWE-rebench V2 authorities for the GPT-OSS evaluation path."""

from __future__ import annotations

from pathlib import Path

EXPERIMENT_ID = "gpt-oss-swerebench-v2-pilot10"
EXPERIMENT_SEED = "gpt-oss-swerebench-v2-eval-20260818"
SELECTION_ALGORITHM_ID = "sha256-seed-instance-repo-then-repo-diverse-first-seen-v1"
PARENT_BASELINE = "9a470019182760f7bb7a462c981b2f71052baf91"

CANONICAL_DATASET_ID = "nebius/SWE-rebench-V2"
CANONICAL_DATASET_REVISION = "475dd5e8703bb5fb22dd3c60b5d038b019eba1e0"
CANONICAL_PARQUET_NAME = "data/train-00000-of-00001.parquet"
CANONICAL_PARQUET_SHA256 = (
    "0e0bf9355f892ad74ae98d4e1c404f39fd6654a8e351ee3e6ab162e4a64cd3ad"
)

B13_SPLIT_DIRNAME = "run_2026-08-08_rev-475dd5e8_b13_v1"
B15_CONTRACT_DIRNAME = "run_2026-08-08_rev-475dd5e8_b15_contract_v1"
CLEAN_LE32K_MASK_NAME = "validation_cd_clean_le32k_mask.json"
CLEAN_MASK_NAME = "validation_cd_clean_mask.json"
LE32K_MASK_NAME = "validation_le32k_eligible_mask.json"
VALIDATION_CANONICAL_NAME = "validation_canonical_150.jsonl"
VALIDATION_MANIFEST_NAME = "validation_manifest.csv"
SPLIT_SUMMARY_NAME = "split_summary.json"
B15_PROVENANCE_NAME = "B15_PROVENANCE.json"
B15_HASHES_NAME = "B15_ARTIFACT_HASHES.json"
B14_REPO_MATERIALIZATION_NAME = "repository_materialization.csv"

EXPECTED_CANONICAL_VALIDATION = 150
EXPECTED_VALIDATION_REPOS = 40
EXPECTED_CD_CLEAN = 142
EXPECTED_LE32K = 135
EXPECTED_CD_CLEAN_LE32K = 128

# Published B15 hashes from B15_ARTIFACT_HASHES.json.
EXPECTED_MASK_SHA256 = {
    CLEAN_LE32K_MASK_NAME: (
        "60f6ae01482abca949bc35b4f81ef6b218ca5aa60cd086e71286d17603c04bb9"
    ),
    CLEAN_MASK_NAME: (
        "f7d6cf97076e3b06045d946bd4083362d29389322d4612b4cc251ff211932d89"
    ),
    LE32K_MASK_NAME: (
        "47221edfca21e3eabd143938294c0168176fe3fcf6535d1cb73622b8d160c7c6"
    ),
    VALIDATION_CANONICAL_NAME: (
        "44da96033177bfba0bcce69d8f7a62f4290458f18ba48c92abdc7325b8d7c298"
    ),
    B15_PROVENANCE_NAME: (
        "b423f45150e9bc62ce1c9782111ecd0713484442f3fd7c7fd23e502426683ffa"
    ),
}

# B13 split hashes recorded in B14 run_provenance.json.
EXPECTED_B13_VALIDATION_MANIFEST_SHA256 = (
    "9571a90697b578dd800ca5a2761af1b50266926e9088cf5d6c0b124553de1b86"
)
EXPECTED_B13_SPLIT_SUMMARY_SHA256 = (
    "10f759c50f3d87e47814de833f18a67aa9de8ceb433670b82572d19154ff57bb"
)

B13_SPLIT_SEED = 20260808
B13_ALGORITHM = "b13_repo_disjoint_balanced_greedy_v1"

DEFAULT_CENSUS_CACHE = Path.home() / (
    "AppData/Local/agentic-debugging/swe_rebench_v2_census_cache"
    "/datasets--nebius--SWE-rebench-V2"
)
DEFAULT_B14_REPO_CACHE = Path.home() / (
    "AppData/Local/agentic-debugging/swe_rebench_v2_b14_repo_cache"
)
DEFAULT_EXTERNAL_ROOT = Path.home() / (
    "AppData/Local/agentic-debugging/gpt-oss-swerebench-v2-pilot10"
)

HISTORICAL_GPT_OSS_PRODUCT_SESSION = "sess-20260817-103258-3d1193"
HISTORICAL_GPT_OSS_PRODUCT_TASK = "curated-none-handling-001"
HISTORICAL_GPT_OSS_PRODUCT_POLICY = "pdb-on-uncertainty"

FORBIDDEN_POPULATIONS = (
    "training",
    "unused",
    "protected-overlap",
    "cd-flagged",
    "overlength",
    "oracle-localized-sft-context",
    "curated-product-session",
)

MODEL_FACING_PUBLIC_FIELDS = (
    "instance_id",
    "repo",
    "repo_canonical",
    "base_commit",
    "license",
    "problem_statement",
    "language",
)

VERIFIER_PRIVATE_FIELDS = (
    "patch",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "interface",
    "pr_description",
    "image_name",
    "install_config",
)

NEVER_MODEL_FIELDS = VERIFIER_PRIVATE_FIELDS + (
    "meta",
    "gold",
    "reference_patch",
    "oracle",
)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def artifacts_root() -> Path:
    return repository_root() / "artifacts"


def b13_split_dir() -> Path:
    return (
        artifacts_root()
        / "swe_rebench_v2_split"
        / "runs"
        / B13_SPLIT_DIRNAME
    )


def b15_contract_dir() -> Path:
    return (
        artifacts_root()
        / "swe_rebench_v2_corpus"
        / "runs"
        / B15_CONTRACT_DIRNAME
    )


def b14_v3_dir() -> Path:
    return (
        artifacts_root()
        / "swe_rebench_v2_corpus"
        / "runs"
        / "run_2026-08-08_rev-475dd5e8_b14_v3"
    )


B15_CONTRACT_DIR = b15_contract_dir()


def experiment_dir() -> Path:
    return repository_root() / "experiments" / "gpt_oss_swerebench_v2_pilot10"


def frozen_dir() -> Path:
    return experiment_dir() / "frozen"


def default_parquet_path() -> Path:
    return (
        DEFAULT_CENSUS_CACHE
        / "snapshots"
        / CANONICAL_DATASET_REVISION
        / CANONICAL_PARQUET_NAME
    )


def default_parquet_blob() -> Path:
    return DEFAULT_CENSUS_CACHE / "blobs" / CANONICAL_PARQUET_SHA256
