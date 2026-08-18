"""Load the canonical clean + <=32K repository-disjoint validation population."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agentic_debugger.swerebench.authority import (
    B13_ALGORITHM,
    B13_SPLIT_SEED,
    CLEAN_LE32K_MASK_NAME,
    CLEAN_MASK_NAME,
    EXPECTED_B13_SPLIT_SUMMARY_SHA256,
    EXPECTED_B13_VALIDATION_MANIFEST_SHA256,
    EXPECTED_CANONICAL_VALIDATION,
    EXPECTED_CD_CLEAN,
    EXPECTED_CD_CLEAN_LE32K,
    EXPECTED_LE32K,
    EXPECTED_MASK_SHA256,
    EXPECTED_VALIDATION_REPOS,
    LE32K_MASK_NAME,
    SPLIT_SUMMARY_NAME,
    VALIDATION_MANIFEST_NAME,
    b13_split_dir,
    b15_contract_dir,
)
from agentic_debugger.swerebench.hashing import require_sha256, sha256_file


@dataclass(frozen=True)
class ValidationTaskMeta:
    instance_id: str
    repo: str
    repo_canonical: str
    base_commit: str
    license: str
    created_at: str
    difficulty: str
    age_bin: str
    patch_bin: str
    split: str

    def to_public_mapping(self) -> dict[str, str]:
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "repo_canonical": self.repo_canonical,
            "base_commit": self.base_commit,
            "license": self.license,
            "created_at": self.created_at,
            "difficulty": self.difficulty,
            "age_bin": self.age_bin,
            "patch_bin": self.patch_bin,
            "split": self.split,
        }


@dataclass(frozen=True)
class CleanValidationPopulation:
    instance_ids: tuple[str, ...]
    tasks: tuple[ValidationTaskMeta, ...]
    source_hashes: Mapping[str, str]
    counts: Mapping[str, int]
    eligibility_filters: tuple[str, ...]
    population_sha256: str

    def task_by_id(self) -> dict[str, ValidationTaskMeta]:
        return {task.instance_id: task for task in self.tasks}

    def repos(self) -> tuple[str, ...]:
        return tuple(sorted({task.repo_canonical for task in self.tasks}))


def _load_id_list(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    ids = data.get("example_ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError(f"{path.name} is missing example_ids")
    if any(not isinstance(item, str) or not item for item in ids):
        raise ValueError(f"{path.name} example_ids must be non-empty strings")
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path.name} example_ids contain duplicates")
    declared = data.get("count")
    if declared is not None and declared != len(ids):
        raise ValueError(
            f"{path.name} count {declared} does not match {len(ids)} ids"
        )
    return list(ids)


def _load_validation_manifest(path: Path) -> dict[str, ValidationTaskMeta]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    tasks: dict[str, ValidationTaskMeta] = {}
    for row in rows:
        instance_id = row["instance_id"]
        if instance_id in tasks:
            raise ValueError(f"duplicate validation instance_id: {instance_id}")
        if row.get("split") != "validation":
            raise ValueError(
                f"{instance_id} has split {row.get('split')!r}, not validation"
            )
        tasks[instance_id] = ValidationTaskMeta(
            instance_id=instance_id,
            repo=row["repo"],
            repo_canonical=row["repo_canonical"],
            base_commit=row["base_commit"],
            license=row["license"],
            created_at=row["created_at"],
            difficulty=row["difficulty"],
            age_bin=row["age_bin"],
            patch_bin=row["patch_bin"],
            split=row["split"],
        )
    if len(tasks) != EXPECTED_CANONICAL_VALIDATION:
        raise ValueError(
            "validation manifest size "
            f"{len(tasks)} != {EXPECTED_CANONICAL_VALIDATION}"
        )
    repos = {task.repo_canonical for task in tasks.values()}
    if len(repos) != EXPECTED_VALIDATION_REPOS:
        raise ValueError(
            "validation repo count "
            f"{len(repos)} != {EXPECTED_VALIDATION_REPOS}"
        )
    return tasks


def load_clean_validation_population(
    *,
    split_dir: Path | None = None,
    contract_dir: Path | None = None,
) -> CleanValidationPopulation:
    """Return the frozen C/D-clean AND <=32K repository-disjoint validation set.

    Authority is the B15 mask ``validation_cd_clean_le32k_mask.json``, joined
    to the B13 validation manifest. This function never reconstructs the
    population from memory or from the oracle-localized SFT JSONL.
    """

    split_dir = split_dir or b13_split_dir()
    contract_dir = contract_dir or b15_contract_dir()
    mask_path = contract_dir / CLEAN_LE32K_MASK_NAME
    clean_path = contract_dir / CLEAN_MASK_NAME
    le32k_path = contract_dir / LE32K_MASK_NAME
    manifest_path = split_dir / VALIDATION_MANIFEST_NAME
    summary_path = split_dir / SPLIT_SUMMARY_NAME

    hashes = {
        CLEAN_LE32K_MASK_NAME: require_sha256(
            mask_path, EXPECTED_MASK_SHA256[CLEAN_LE32K_MASK_NAME],
            label="B15 clean<=32K mask",
        ),
        CLEAN_MASK_NAME: require_sha256(
            clean_path, EXPECTED_MASK_SHA256[CLEAN_MASK_NAME],
            label="B15 C/D-clean mask",
        ),
        LE32K_MASK_NAME: require_sha256(
            le32k_path, EXPECTED_MASK_SHA256[LE32K_MASK_NAME],
            label="B15 <=32K mask",
        ),
        VALIDATION_MANIFEST_NAME: require_sha256(
            manifest_path,
            EXPECTED_B13_VALIDATION_MANIFEST_SHA256,
            label="B13 validation manifest",
        ),
        SPLIT_SUMMARY_NAME: require_sha256(
            summary_path,
            EXPECTED_B13_SPLIT_SUMMARY_SHA256,
            label="B13 split summary",
        ),
    }

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("algorithm") != B13_ALGORITHM:
        raise ValueError(f"unexpected B13 algorithm: {summary.get('algorithm')}")
    if summary.get("seed") != B13_SPLIT_SEED:
        raise ValueError(f"unexpected B13 seed: {summary.get('seed')}")
    if summary.get("validation", {}).get("tasks") != EXPECTED_CANONICAL_VALIDATION:
        raise ValueError("B13 validation task count drifted")
    if summary.get("validation", {}).get("repos") != EXPECTED_VALIDATION_REPOS:
        raise ValueError("B13 validation repo count drifted")
    if summary.get("hard_gates", {}).get("train_validation_repo_overlap") != 0:
        raise ValueError("B13 train/validation repo overlap is not zero")

    clean_le32k = _load_id_list(mask_path)
    cd_clean = set(_load_id_list(clean_path))
    le32k = set(_load_id_list(le32k_path))
    if len(cd_clean) != EXPECTED_CD_CLEAN:
        raise ValueError(f"C/D-clean count {len(cd_clean)} != {EXPECTED_CD_CLEAN}")
    if len(le32k) != EXPECTED_LE32K:
        raise ValueError(f"<=32K count {len(le32k)} != {EXPECTED_LE32K}")
    if len(clean_le32k) != EXPECTED_CD_CLEAN_LE32K:
        raise ValueError(
            f"clean+<=32K count {len(clean_le32k)} != {EXPECTED_CD_CLEAN_LE32K}"
        )
    if set(clean_le32k) != (cd_clean & le32k):
        raise ValueError(
            "B15 clean+<=32K mask is not the intersection of the C/D-clean "
            "and <=32K masks"
        )

    manifest = _load_validation_manifest(manifest_path)
    missing = [item for item in clean_le32k if item not in manifest]
    if missing:
        raise ValueError(
            "clean+<=32K ids missing from the B13 validation manifest: "
            + ", ".join(missing[:8])
        )
    train_overlap = [
        item for item in clean_le32k if manifest[item].split != "validation"
    ]
    if train_overlap:
        raise ValueError("clean validation ids are not confined to validation")

    tasks = tuple(manifest[item] for item in clean_le32k)
    repos = {task.repo_canonical for task in tasks}
    population = {
        "authority": CLEAN_LE32K_MASK_NAME,
        "instance_ids": list(clean_le32k),
        "source_hashes": dict(hashes),
    }
    from agentic_debugger.swerebench.hashing import sha256_canonical_json

    return CleanValidationPopulation(
        instance_ids=tuple(clean_le32k),
        tasks=tasks,
        source_hashes=hashes,
        counts={
            "canonical_validation": EXPECTED_CANONICAL_VALIDATION,
            "canonical_validation_repos": EXPECTED_VALIDATION_REPOS,
            "cd_clean": EXPECTED_CD_CLEAN,
            "le32k": EXPECTED_LE32K,
            "cd_clean_and_le32k": EXPECTED_CD_CLEAN_LE32K,
            "cd_clean_and_le32k_repos": len(repos),
        },
        eligibility_filters=(
            "dataset=nebius/SWE-rebench-V2@475dd5e8703bb5fb22dd3c60b5d038b019eba1e0",
            "split=B13 repository-disjoint validation (seed 20260808)",
            "not training",
            "not unused",
            "protected-repo overlap = 0 (B13 hard gate)",
            "B15 C/D leakage mask excluded (8 validation ids)",
            "B15 exact-tokenizer <=32K eligibility (no truncation)",
            "oracle-localized SFT JSONL is not the evaluation population",
        ),
        population_sha256=sha256_canonical_json(population),
    )
