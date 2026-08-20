"""Immutable identity and selection checks for the GPT-OSS DEVQUAL V8 stream treatment."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agentic_debugger.swerebench.authority import repository_root
from agentic_debugger.swerebench.hashing import require_sha256, sha256_file
from agentic_debugger.swerebench.provenance import require_harness_match


DEVQUAL_EXPERIMENT_ID = "gpt_oss_swerebench_v2_devqual10_v8"
PARENT_EXPERIMENT_ID = "gpt_oss_swerebench_v2_devqual10_v7"
_LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
DEVQUAL_EXTERNAL_ROOT = _LOCAL_APP_DATA / "agentic-debugging/gpt-oss-swerebench-v2-devqual10-v8"
DEVQUAL_DIR = repository_root() / "experiments" / DEVQUAL_EXPERIMENT_ID
DEVQUAL_FROZEN_DIR = DEVQUAL_DIR / "frozen"
HISTORICAL_FULL_ORDERING_SHA256 = "599a07b6a527b4f8dffda4120be8e3c524ad608929bb048ea98286f80e0f5061"
HISTORICAL_POPULATION_SHA256 = "36bd31d1470b86db982235153372793455a850ae1fe9c1669bdf8c0e7e68ab8f"
HISTORICAL_PILOT_MANIFEST_SHA256 = "4b9b17f8f897e56263f0394e35c06261bc613097f38a1b2e157d4d9a215a963f"


def devqual_manifest_path(project: Path | None = None) -> Path:
    return ((project or repository_root()).resolve() / "experiments" / DEVQUAL_EXPERIMENT_ID / "frozen" / "pilot10_manifest.json")


def devqual_contract_path(project: Path | None = None) -> Path:
    return devqual_manifest_path(project).with_name("execution_contract.json")


def load_devqual_contract(project: Path | None = None) -> dict[str, Any]:
    return json.loads(devqual_contract_path(project).read_text(encoding="utf-8"))


def load_devqual_manifest(project: Path | None = None) -> dict[str, Any]:
    return json.loads(devqual_manifest_path(project).read_text(encoding="utf-8"))


def validate_devqual_identity(*, project: Path | None = None) -> dict[str, Any]:
    root = (project or repository_root()).resolve()
    contract = load_devqual_contract(root)
    manifest = load_devqual_manifest(root)
    historical = root / "experiments" / "gpt_oss_swerebench_v2_pilot10" / "frozen"
    expected = json.loads((historical / "full_ordering.json").read_text(encoding="utf-8"))["entries"][:10]
    if manifest.get("tasks") != expected or manifest.get("selected_instance_ids") != [item["instance_id"] for item in expected]:
        raise ValueError("DEVQUAL V8 first-ten tasks differ from the historical full-ordering prefix")
    if manifest.get("n") != 10 or manifest.get("distinct_repos") != 10:
        raise ValueError("DEVQUAL V8 manifest must contain exactly ten repository-diverse tasks")
    require_sha256(historical / "full_ordering.json", contract["source_ordering_sha256"], label="source full ordering")
    require_sha256(historical / "population.json", contract["source_population_sha256"], label="source population")
    require_sha256(historical / "pilot10_manifest.json", contract["historical_pilot10_manifest_sha256"], label="historical Pilot-10 manifest")
    if sha256_file(devqual_manifest_path(root)) != contract["devqual_manifest_sha256"]:
        raise ValueError("DEVQUAL V8 manifest content does not match its frozen hash")
    expected_harness = contract["harness"]["harness_content_sha256"]
    require_harness_match(expected_harness, root)
    return {
        "experiment_id": contract["experiment_id"],
        "parent_experiment_id": contract["parent_experiment_id"],
        "first_ten_instance_ids": list(manifest["selected_instance_ids"]),
        "source_full_ordering_sha256": contract["source_ordering_sha256"],
        "source_population_sha256": contract["source_population_sha256"],
        "historical_pilot10_manifest_sha256": contract["historical_pilot10_manifest_sha256"],
        "devqual_manifest_sha256": contract["devqual_manifest_sha256"],
        "harness_content_sha256": expected_harness,
    }


__all__ = [
    "DEVQUAL_DIR", "DEVQUAL_EXPERIMENT_ID", "DEVQUAL_EXTERNAL_ROOT",
    "DEVQUAL_FROZEN_DIR", "PARENT_EXPERIMENT_ID",
    "HISTORICAL_FULL_ORDERING_SHA256", "HISTORICAL_POPULATION_SHA256",
    "HISTORICAL_PILOT_MANIFEST_SHA256", "devqual_contract_path",
    "devqual_manifest_path", "load_devqual_contract", "load_devqual_manifest",
    "validate_devqual_identity",
]
