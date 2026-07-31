"""Validate the tracked BugsInPy pilot licensing-gate manifest and record.

This check is offline and read-only. It validates exact identity, artifact
contract, references, and verdict propagation; it does not acquire or execute
benchmark material.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


VERDICTS = {"CLEAR", "CLEAR_WITH_CONDITIONS", "BLOCKED", "UNKNOWN"}
VERDICT_RANK = {
    "BLOCKED": 0,
    "UNKNOWN": 1,
    "CLEAR_WITH_CONDITIONS": 2,
    "CLEAR": 3,
}
EXPECTED_PROJECTS = {"fastapi", "httpie", "tqdm", "thefuck"}

PROJECT_ARTIFACT_CONTRACTS: dict[str, dict[str, Any]] = {
    "fastapi": {
        "repository": "tiangolo/fastapi",
        "project_spdx_identifiers": ["MIT"],
        "artifacts": {
            "LICENSE": {"record_kind": "license", "spdx_identifier": "MIT"},
        },
    },
    "httpie": {
        "repository": "httpie/httpie",
        "project_spdx_identifiers": ["BSD-3-Clause"],
        "artifacts": {
            "LICENSE": {"record_kind": "license", "spdx_identifier": "BSD-3-Clause"},
            "AUTHORS.rst": {"record_kind": "attribution_notice", "spdx_identifier": None},
        },
    },
    "tqdm": {
        "repository": "tqdm/tqdm",
        "project_spdx_identifiers": ["MIT", "MPL-2.0"],
        "artifacts": {
            "LICENCE": {"record_kind": "license", "spdx_identifiers": ["MIT", "MPL-2.0"]},
        },
        "require_project_scope_note": True,
    },
    "thefuck": {
        "repository": "nvbn/thefuck",
        "project_spdx_identifiers": ["MIT"],
        "artifacts": {
            "LICENSE.md": {"record_kind": "license", "spdx_identifier": "MIT"},
        },
    },
}
EXPECTED_REPOSITORIES = {
    project: contract["repository"]
    for project, contract in PROJECT_ARTIFACT_CONTRACTS.items()
}
EXPECTED_TASKS = {
    "bugsinpy-fastapi-001",
    "bugsinpy-fastapi-009",
    "bugsinpy-httpie-001",
    "bugsinpy-httpie-002",
    "bugsinpy-tqdm-002",
    "bugsinpy-tqdm-003",
    "bugsinpy-thefuck-001",
    "bugsinpy-thefuck-002",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BUGSINPY_REPOSITORY = "soarsmu/BugsInPy"
LOCKED_BUGSINPY_REVISION = "11c5f1eea954a42132cfd06bf257766a7963e0fd"
RECORD_KINDS = {"license", "attribution_notice"}
CANONICAL_GATE_PATH = Path("research/bugsinpy/BUGSINPY_LICENSE_GATE_V1.json")


class ValidationError(Exception):
    """Raised when a gate invariant is not satisfied."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot load JSON {path}: {exc}") from exc
    require(isinstance(value, dict), f"JSON root must be an object: {path}")
    return value


def require_verdict(value: Any, label: str) -> str:
    require(isinstance(value, str) and value in VERDICTS,
            f"{label} has invalid verdict vocabulary")
    return value


def no_stronger(candidate: str, controlling: str) -> bool:
    """Return whether candidate clearance is no stronger than its controller."""
    require_verdict(candidate, "candidate")
    require_verdict(controlling, "controlling")
    return VERDICT_RANK[candidate] <= VERDICT_RANK[controlling]


def records_by_id(record_source: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    records = record_source.get(key)
    require(isinstance(records, list), f"{key} must be an array")
    result: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        require(isinstance(record, dict), f"{key}[{index}] must be an object")
        record_id = record.get("id")
        require(isinstance(record_id, str) and record_id,
                f"{key}[{index}] has no record ID")
        require(record_id not in result, f"duplicate record ID: {record_id}")
        result[record_id] = record
    return result


def validate(manifest: dict[str, Any], gate: dict[str, Any]) -> dict[str, int]:
    require(gate.get("schema_version") == "1.0", "invalid gate schema version")
    require(gate.get("task_id") == "bugsinpy-license-gate-v1", "gate task identity mismatch")
    require(gate.get("canonical_record_path") == str(CANONICAL_GATE_PATH).replace("\\", "/"),
            "gate canonical path mismatch")
    require(gate.get("accepted_baseline") == "a143e62d54a7cf25f56ba743a020cc19b472c762",
            "gate accepted baseline mismatch")

    manifest_review = manifest.get("licensing_review")
    require(isinstance(manifest_review, dict), "manifest licensing_review is missing")
    require(manifest_review.get("canonical_record_path") == str(CANONICAL_GATE_PATH).replace("\\", "/"),
            "manifest canonical record path mismatch")

    dataset_records = records_by_id(gate, "dataset_records")
    project_records = records_by_id(gate, "project_records")
    file_records = records_by_id(gate, "file_records")
    all_ids = list(dataset_records) + list(project_records) + list(file_records)
    require(len(all_ids) == len(set(all_ids)), "duplicate record ID across record classes")

    dataset_id = manifest_review.get("dataset_record_id")
    require(dataset_id in dataset_records, f"missing dataset record: {dataset_id}")
    dataset = dataset_records[dataset_id]
    dataset_verdict = require_verdict(dataset.get("verdict"), "dataset record")

    authority = manifest.get("authority")
    require(isinstance(authority, dict), "manifest authority block is missing")
    require(authority.get("official_repository_revision") == LOCKED_BUGSINPY_REVISION,
            "manifest authority revision mismatch")
    require(dataset.get("revision") == LOCKED_BUGSINPY_REVISION,
            "dataset record revision mismatch")
    require(dataset.get("repository") == BUGSINPY_REPOSITORY,
            "dataset repository identity mismatch")
    tree_evidence = dataset.get("tree_evidence")
    require(isinstance(tree_evidence, dict), "dataset tree_evidence is missing")
    require(tree_evidence.get("resolved_tree_sha") == LOCKED_BUGSINPY_REVISION,
            "tree resolved SHA mismatch")
    require(tree_evidence.get("http_status") == 200,
            "tree HTTP status is not 200")
    require(tree_evidence.get("recursive_result_truncated") is False,
            "tree recursive result is truncated")
    require(isinstance(tree_evidence.get("matching_paths"), list),
            "tree matching_paths must be an array")
    for field in ("response_sha256", "sanitized_result_sha256"):
        value = tree_evidence.get(field)
        require(isinstance(value, str) and SHA256_RE.fullmatch(value) is not None,
                f"invalid tree evidence {field}")
    readme_sha256 = dataset.get("readme_sha256")
    require(isinstance(readme_sha256, str) and SHA256_RE.fullmatch(readme_sha256) is not None,
            "invalid dataset readme_sha256")

    dataset_fields = (
        "dataset_verdict",
        "formal_license_status",
        "redistribution_verdict",
        "private_local_research_use_verdict",
        "operational_execution_gate",
    )
    for field in dataset_fields:
        dataset_value = dataset["verdict"] if field == "dataset_verdict" else dataset.get(field)
        require_verdict(dataset_value, f"dataset record {field}")
        require(dataset_value == manifest_review.get(field),
                f"manifest/dataset {field} mismatch")

    overall = require_verdict(gate.get("verdict"), "gate overall verdict")
    require(overall == manifest_review.get("overall_pilot_verdict"),
            "overall verdict mismatch")

    manifest_projects = manifest_review.get("project_verdicts")
    manifest_project_ids = manifest_review.get("project_verdict_record_ids")
    require(isinstance(manifest_projects, dict), "manifest project_verdicts is missing")
    require(isinstance(manifest_project_ids, dict), "manifest project_verdict_record_ids is missing")
    require(set(manifest_projects) == EXPECTED_PROJECTS,
            "manifest project verdict project set mismatch")
    require(set(manifest_project_ids) == EXPECTED_PROJECTS,
            "manifest project record project set mismatch")
    require({record.get("project") for record in project_records.values()} == EXPECTED_PROJECTS,
            "project record project set mismatch")

    tasks = manifest.get("tasks")
    require(isinstance(tasks, list), "manifest tasks is missing")
    task_ids = [task.get("pilot_task_id") for task in tasks if isinstance(task, dict)]
    require(len(task_ids) == len(set(task_ids)), "duplicate pilot task ID")
    require(set(task_ids) == EXPECTED_TASKS and len(task_ids) == 8,
            "selected task IDs are not exactly the eight pilot tasks")

    project_revisions: dict[str, set[str]] = {project: set() for project in EXPECTED_PROJECTS}
    for task in tasks:
        task_id = task["pilot_task_id"]
        bugsinpy = task.get("bugsinpy", {})
        project = bugsinpy.get("project")
        require(project in EXPECTED_PROJECTS, f"{task_id} has invalid project")
        buggy_revision = bugsinpy.get("buggy_revision")
        fixed_revision = bugsinpy.get("fixed_revision")
        require(isinstance(buggy_revision, str) and COMMIT_SHA_RE.fullmatch(buggy_revision),
                f"{task_id} has invalid buggy_revision")
        require(isinstance(fixed_revision, str) and COMMIT_SHA_RE.fullmatch(fixed_revision),
                f"{task_id} has invalid fixed_revision")
        project_revisions[project].update((buggy_revision, fixed_revision))

    project_file_ids: list[str] = []
    file_project: dict[str, str] = {}
    for project, project_id in manifest_project_ids.items():
        require(project_id in project_records, f"missing project record: {project_id}")
        record = project_records[project_id]
        require(record.get("project") == project, f"project record mismatch for {project}")
        expected_repository = EXPECTED_REPOSITORIES[project]
        require(record.get("repository") == expected_repository,
                f"project record repository mismatch for {project}")
        contract = PROJECT_ARTIFACT_CONTRACTS[project]
        require(record.get("spdx_identifiers") == contract["project_spdx_identifiers"],
                f"project {project} spdx_identifiers mismatch")
        if contract.get("require_project_scope_note"):
            scope_note = record.get("scope_note")
            require(isinstance(scope_note, str) and scope_note.strip(),
                    f"project {project} mixed-license scope note is missing")
        project_verdict = require_verdict(record.get("verdict"), f"project {project}")
        require(project_verdict == manifest_projects[project],
                f"project verdict propagation mismatch for {project}")
        file_ids = record.get("file_record_ids")
        require(isinstance(file_ids, list), f"project {project} file_record_ids is missing")
        for file_id in file_ids:
            require(file_id in file_records, f"missing project file record: {file_id}")
            require(file_records[file_id].get("repository") == expected_repository,
                    f"file record {file_id} repository does not match project {project}")
            file_project[file_id] = project
            project_file_ids.append(file_id)
    require(len(project_file_ids) == len(set(project_file_ids)),
            "file record assigned to multiple project records")
    require(set(project_file_ids) == set(file_records),
            "project records do not cover exactly all file records")

    required_file_fields = ("source_url", "repository", "revision", "path", "sha256")
    file_identities: list[tuple[str, str, str]] = []
    for record_id, record in file_records.items():
        for field in required_file_fields:
            value = record.get(field)
            require(isinstance(value, str) and value,
                    f"file record {record_id} missing {field}")
        require(SHA256_RE.fullmatch(record["sha256"]) is not None,
                f"file record {record_id} has invalid SHA-256")
        require(record.get("record_kind") in RECORD_KINDS,
                f"file record {record_id} has invalid record_kind")
        revision = record["revision"]
        require(COMMIT_SHA_RE.fullmatch(revision) is not None,
                f"file record {record_id} has invalid revision")
        project = file_project[record_id]
        contract = PROJECT_ARTIFACT_CONTRACTS[project]
        path = record["path"]
        artifact_spec = contract["artifacts"].get(path)
        require(artifact_spec is not None,
                f"file record {record_id} path {path} is not a required artifact path for {project}")
        require(record["record_kind"] == artifact_spec["record_kind"],
                f"file record {record_id} record_kind does not match required "
                f"artifact {path} for {project}")
        for spdx_field, spdx_value in artifact_spec.items():
            if spdx_field == "record_kind":
                continue
            require(spdx_field in record and record.get(spdx_field) == spdx_value,
                    f"file record {record_id} SPDX metadata does not match "
                    f"required artifact {path} for {project}")
        require(record["revision_url"] ==
                f"https://github.com/{record['repository']}/tree/{revision}",
                f"file record {record_id} revision_url mismatch")
        require(record["source_url"] ==
                f"https://raw.githubusercontent.com/{record['repository']}/{revision}/{path}",
                f"file record {record_id} source_url mismatch")
        file_identities.append((record["repository"], revision, path))
    require(len(file_identities) == len(set(file_identities)),
            "duplicate repository/revision/path file record")

    expected_project_ids: dict[str, set[str]] = {}
    for project, project_id in manifest_project_ids.items():
        record = project_records[project_id]
        contract = PROJECT_ARTIFACT_CONTRACTS[project]
        selected = project_revisions[project]
        require(len(selected) == 4,
                f"project {project} must select exactly four distinct revisions")
        expected: set[str] = set()
        for revision in selected:
            for path in contract["artifacts"]:
                matches = [
                    file_id for file_id in record["file_record_ids"]
                    if file_records[file_id]["revision"] == revision
                    and file_records[file_id]["path"] == path
                ]
                require(len(matches) == 1,
                        f"project {project} missing required artifact record for "
                        f"revision {revision} path {path}")
                expected.add(matches[0])
        for file_id in record["file_record_ids"]:
            require(file_records[file_id]["revision"] in selected,
                    f"file record {file_id} revision is not a selected revision for {project}")
        expected_project_ids[project] = expected

    for task in tasks:
        task_id = task["pilot_task_id"]
        licensing = task.get("licensing")
        require(isinstance(licensing, dict), f"{task_id} licensing block is missing")
        require(licensing.get("dataset_record_id") == dataset_id,
                f"{task_id} dataset reference mismatch")
        task_dataset_fields = {
            "dataset_verdict": "verdict",
            "formal_license_status": "formal_license_status",
            "private_local_research_use_verdict": "private_local_research_use_verdict",
            "operational_execution_gate": "operational_execution_gate",
            "redistribution_verdict": "redistribution_verdict",
        }
        for field, dataset_field in task_dataset_fields.items():
            require_verdict(licensing.get(field), f"{task_id} {field}")
            require(licensing[field] == dataset[dataset_field],
                    f"{task_id} dataset field mismatch: {field}")

        bugsinpy = task.get("bugsinpy", {})
        project = bugsinpy.get("project")
        project_id = licensing.get("project_record_id")
        require(project in EXPECTED_PROJECTS, f"{task_id} has invalid project")
        require(project_id == manifest_project_ids[project],
                f"{task_id} project reference mismatch")
        project_record = project_records[project_id]
        require(project_record.get("project") == project,
                f"{task_id} project name does not agree with project record")
        require(str(bugsinpy.get("project_url") or "").rstrip("/") ==
                "https://github.com/" + EXPECTED_REPOSITORIES[project],
                f"{task_id} project URL does not agree with project record repository")
        buggy_revision = bugsinpy.get("buggy_revision")
        fixed_revision = bugsinpy.get("fixed_revision")
        require(isinstance(buggy_revision, str) and COMMIT_SHA_RE.fullmatch(buggy_revision),
                f"{task_id} has invalid buggy_revision")
        require(isinstance(fixed_revision, str) and COMMIT_SHA_RE.fullmatch(fixed_revision),
                f"{task_id} has invalid fixed_revision")
        project_verdict = require_verdict(project_record.get("verdict"),
                                          f"project {project} verdict")
        require(licensing.get("project_verdict") == project_verdict,
                f"{task_id} project verdict mismatch")
        task_verdict = require_verdict(licensing.get("task_verdict"),
                                        f"{task_id} task verdict")
        require(task_verdict == licensing.get("gate_status"),
                f"{task_id} task gate mismatch")
        require(no_stronger(task_verdict, dataset_verdict),
                f"{task_id} task verdict stronger than dataset verdict")
        require(no_stronger(task_verdict, project_verdict),
                f"{task_id} task verdict stronger than project verdict")
        require(no_stronger(task_verdict, licensing["operational_execution_gate"]),
                f"{task_id} task verdict stronger than operational gate")
        require(no_stronger(licensing["private_local_research_use_verdict"],
                            dataset["private_local_research_use_verdict"]),
                f"{task_id} private-use verdict stronger than dataset")
        require(no_stronger(licensing["redistribution_verdict"],
                            dataset["redistribution_verdict"]),
                f"{task_id} redistribution verdict stronger than dataset")
        if licensing["operational_execution_gate"] == "BLOCKED":
            require(task_verdict == "BLOCKED",
                    f"{task_id} cannot clear while operational gate is BLOCKED")

        reviewed = licensing.get("reviewed_license_record_ids")
        require(isinstance(reviewed, list) and reviewed,
                f"{task_id} has no reviewed file records")
        require(len(reviewed) == len(set(reviewed)),
                f"{task_id} duplicate reviewed record ID")
        project_file_set = set(project_record["file_record_ids"])
        for file_id in reviewed:
            require(file_id in file_records, f"{task_id} missing file record: {file_id}")
            require(file_id in project_file_set,
                    f"{task_id} file record is outside its project record")
        contract = PROJECT_ARTIFACT_CONTRACTS[project]
        expected_reviewed = {
            file_id for file_id in project_file_set
            if file_records[file_id]["revision"] in (buggy_revision, fixed_revision)
            and file_records[file_id]["path"] in contract["artifacts"]
        }
        require(set(reviewed) == expected_reviewed,
                f"{task_id} reviewed license records must cover exactly the "
                "required artifact records for its buggy/fixed revisions")
        for revision, label in ((buggy_revision, "buggy"), (fixed_revision, "fixed")):
            require(any(file_records[file_id]["revision"] == revision
                        for file_id in expected_reviewed),
                    f"{task_id} missing {label}-revision license coverage")

    task_reviewed = {
        task["pilot_task_id"]: task["licensing"]["reviewed_license_record_ids"]
        for task in tasks
    }
    project_tasks: dict[str, list[str]] = {project: [] for project in EXPECTED_PROJECTS}
    for task in tasks:
        project_tasks[task["bugsinpy"]["project"]].append(task["pilot_task_id"])
    for project, project_id in manifest_project_ids.items():
        record = project_records[project_id]
        covered: set[str] = set()
        for task_id in project_tasks[project]:
            covered.update(task_reviewed[task_id])
        require(covered == expected_project_ids[project],
                f"project {project} task-reviewed records do not cover exactly "
                "the required selected-revision artifact set")
        for file_id in record["file_record_ids"]:
            require(file_id in covered,
                    f"project file record {file_id} is not covered by any selected task")

    dependencies = [dataset_verdict]
    dependencies.extend(project_records[project_id]["verdict"]
                        for project_id in manifest_project_ids.values())
    dependencies.extend(task["licensing"]["task_verdict"] for task in tasks)
    require(all(no_stronger(overall, dependency) for dependency in dependencies),
            "overall pilot verdict is stronger than a material dependency")

    return {
        "dataset_records": len(dataset_records),
        "project_records": len(project_records),
        "file_records": len(file_records),
        "tasks": len(tasks),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("research/bugsinpy/PILOT_ELIGIBILITY_MANIFEST_V1.json"),
    )
    parser.add_argument(
        "--gate-record",
        type=Path,
        default=CANONICAL_GATE_PATH,
    )
    parser.add_argument(
        "--package-matrix",
        type=Path,
        default=None,
        help="Optional review-package matrix to compare with the tracked gate record.",
    )
    args = parser.parse_args()
    try:
        manifest = load_json(args.manifest)
        gate = load_json(args.gate_record)
        if args.package_matrix is not None:
            package = load_json(args.package_matrix)
            require(package == gate,
                    "review-package matrix differs from tracked canonical record")
        counts = validate(manifest, gate)
    except ValidationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("PASS: BugsInPy license-gate artifact contract, referential integrity, and verdict propagation")
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
