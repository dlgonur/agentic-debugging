"""Regression tests for the tracked BugsInPy licensing gate."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "research/bugsinpy/PILOT_ELIGIBILITY_MANIFEST_V1.json"
GATE_PATH = ROOT / "research/bugsinpy/BUGSINPY_LICENSE_GATE_V1.json"
PACKAGE_MATRIX_PATH = ROOT / "_ai-review/bugsinpy-license-gate-v1/license-verification-matrix.json"

_spec = importlib.util.spec_from_file_location(
    "validate_bugsinpy_license_gate",
    ROOT / "scripts/validate_bugsinpy_license_gate.py",
)
_validator = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_validator)


@pytest.fixture
def documents():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    gate = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    return manifest, gate


def rejects(manifest, gate):
    with pytest.raises(_validator.ValidationError):
        _validator.validate(manifest, gate)


def test_final_records_validate(documents):
    manifest, gate = documents
    assert _validator.validate(manifest, gate) == {
        "dataset_records": 1,
        "project_records": 4,
        "file_records": 20,
        "tasks": 8,
    }


def test_exact_eight_task_ids(documents):
    manifest, _ = documents
    task_ids = [task["pilot_task_id"] for task in manifest["tasks"]]
    assert len(task_ids) == 8
    assert len(set(task_ids)) == 8
    assert set(task_ids) == _validator.EXPECTED_TASKS


def test_missing_dataset_record_rejected(documents):
    manifest, gate = documents
    manifest["licensing_review"]["dataset_record_id"] = "missing-dataset"
    rejects(manifest, gate)


def test_missing_project_record_rejected(documents):
    manifest, gate = documents
    manifest["licensing_review"]["project_verdict_record_ids"]["fastapi"] = "missing-project"
    rejects(manifest, gate)


def test_missing_file_record_rejected(documents):
    manifest, gate = documents
    manifest["tasks"][0]["licensing"]["reviewed_license_record_ids"].append("missing-file")
    rejects(manifest, gate)


def test_duplicate_id_within_record_class_rejected(documents):
    manifest, gate = documents
    gate["file_records"].append(copy.deepcopy(gate["file_records"][0]))
    rejects(manifest, gate)


def test_duplicate_id_across_record_classes_rejected(documents):
    manifest, gate = documents
    duplicate = copy.deepcopy(gate["file_records"][0])
    gate["dataset_records"].append(duplicate)
    rejects(manifest, gate)


def test_invalid_verdict_vocabulary_rejected(documents):
    manifest, gate = documents
    gate["dataset_records"][0]["verdict"] = "MAYBE"
    rejects(manifest, gate)


def test_task_clear_while_dataset_blocked_rejected(documents):
    manifest, gate = documents
    task = manifest["tasks"][0]["licensing"]
    task["task_verdict"] = "CLEAR"
    task["gate_status"] = "CLEAR"
    rejects(manifest, gate)


def test_task_clear_while_operational_gate_blocked_rejected(documents):
    manifest, gate = documents
    gate["dataset_records"][0]["verdict"] = "CLEAR"
    manifest["licensing_review"]["dataset_verdict"] = "CLEAR"
    manifest["licensing_review"]["overall_pilot_verdict"] = "CLEAR"
    gate["verdict"] = "CLEAR"
    manifest["licensing_review"]["project_verdicts"]["fastapi"] = "CLEAR"
    fastapi_id = manifest["licensing_review"]["project_verdict_record_ids"]["fastapi"]
    gate["project_records"][0]["verdict"] = "CLEAR"
    task = manifest["tasks"][0]["licensing"]
    task["dataset_verdict"] = "CLEAR"
    task["project_verdict"] = "CLEAR"
    task["task_verdict"] = "CLEAR"
    task["gate_status"] = "CLEAR"
    task["project_record_id"] = fastapi_id
    rejects(manifest, gate)


def test_task_private_use_stronger_than_dataset_rejected(documents):
    manifest, gate = documents
    manifest["tasks"][0]["licensing"]["private_local_research_use_verdict"] = "CLEAR"
    rejects(manifest, gate)


def test_task_redistribution_stronger_than_dataset_rejected(documents):
    manifest, gate = documents
    manifest["tasks"][0]["licensing"]["redistribution_verdict"] = "CLEAR"
    rejects(manifest, gate)


def test_manifest_dataset_private_use_mismatch_rejected(documents):
    manifest, gate = documents
    manifest["licensing_review"]["private_local_research_use_verdict"] = "CLEAR"
    rejects(manifest, gate)


def test_manifest_dataset_verdict_mismatch_rejected(documents):
    manifest, gate = documents
    manifest["licensing_review"]["dataset_verdict"] = "CLEAR"
    rejects(manifest, gate)


def test_task_project_verdict_mismatch_rejected(documents):
    manifest, gate = documents
    manifest["tasks"][0]["licensing"]["project_verdict"] = "CLEAR"
    rejects(manifest, gate)


def test_task_project_url_mismatch_rejected(documents):
    manifest, gate = documents
    manifest["tasks"][0]["bugsinpy"]["project_url"] = "https://github.com/other/fastapi"
    rejects(manifest, gate)


def test_license_records_swapped_with_other_bug_rejected(documents):
    manifest, gate = documents
    task = next(t for t in manifest["tasks"] if t["pilot_task_id"] == "bugsinpy-fastapi-001")
    task["licensing"]["reviewed_license_record_ids"] = [
        "fastapi-a7a92b-license",
        "fastapi-c58179-license",
    ]
    rejects(manifest, gate)


def test_missing_buggy_revision_license_coverage_rejected(documents):
    manifest, gate = documents
    task = next(t for t in manifest["tasks"] if t["pilot_task_id"] == "bugsinpy-fastapi-001")
    task["licensing"]["reviewed_license_record_ids"].remove("fastapi-766157-license")
    rejects(manifest, gate)


def test_missing_fixed_revision_license_coverage_rejected(documents):
    manifest, gate = documents
    task = next(t for t in manifest["tasks"] if t["pilot_task_id"] == "bugsinpy-fastapi-001")
    task["licensing"]["reviewed_license_record_ids"].remove("fastapi-3397d4-license")
    rejects(manifest, gate)


def test_httpie_missing_authors_record_for_revision_rejected(documents):
    manifest, gate = documents
    task = next(t for t in manifest["tasks"] if t["pilot_task_id"] == "bugsinpy-httpie-001")
    task["licensing"]["reviewed_license_record_ids"].remove("httpie-001bda-authors")
    rejects(manifest, gate)


def test_extra_unrelated_revision_record_rejected(documents):
    manifest, gate = documents
    task = next(t for t in manifest["tasks"] if t["pilot_task_id"] == "bugsinpy-fastapi-001")
    task["licensing"]["reviewed_license_record_ids"].append("fastapi-a7a92b-license")
    rejects(manifest, gate)


def test_wrong_project_file_record_rejected(documents):
    manifest, gate = documents
    task = next(t for t in manifest["tasks"] if t["pilot_task_id"] == "bugsinpy-fastapi-001")
    task["licensing"]["reviewed_license_record_ids"] = [
        "fastapi-766157-license",
        "httpie-001bda-license",
    ]
    rejects(manifest, gate)


def test_file_record_repository_mismatch_rejected(documents):
    manifest, gate = documents
    gate["file_records"][0]["repository"] = "wrong-owner/fastapi"
    rejects(manifest, gate)


def test_project_record_repository_mismatch_rejected(documents):
    manifest, gate = documents
    gate["project_records"][0]["repository"] = "wrong-owner/fastapi"
    rejects(manifest, gate)


def test_manifest_authority_revision_mismatch_rejected(documents):
    manifest, gate = documents
    manifest["authority"]["official_repository_revision"] = "0" * 40
    rejects(manifest, gate)


def test_dataset_record_revision_mismatch_rejected(documents):
    manifest, gate = documents
    gate["dataset_records"][0]["revision"] = "0" * 40
    rejects(manifest, gate)


def test_dataset_repository_identity_mismatch_rejected(documents):
    manifest, gate = documents
    gate["dataset_records"][0]["repository"] = "someone/BugsInPy"
    rejects(manifest, gate)


def test_tree_resolved_sha_mismatch_rejected(documents):
    manifest, gate = documents
    gate["dataset_records"][0]["tree_evidence"]["resolved_tree_sha"] = "0" * 40
    rejects(manifest, gate)


def test_tree_http_status_mismatch_rejected(documents):
    manifest, gate = documents
    gate["dataset_records"][0]["tree_evidence"]["http_status"] = 403
    rejects(manifest, gate)


def test_truncated_tree_evidence_rejected(documents):
    manifest, gate = documents
    gate["dataset_records"][0]["tree_evidence"]["recursive_result_truncated"] = True
    rejects(manifest, gate)


def test_tree_matching_paths_not_array_rejected(documents):
    manifest, gate = documents
    gate["dataset_records"][0]["tree_evidence"]["matching_paths"] = None
    rejects(manifest, gate)


@pytest.mark.parametrize("field", ["response_sha256", "sanitized_result_sha256", "readme_sha256"])
def test_invalid_dataset_evidence_sha256_rejected(documents, field):
    manifest, gate = documents
    if field == "readme_sha256":
        gate["dataset_records"][0][field] = "zz" * 32
    else:
        gate["dataset_records"][0]["tree_evidence"][field] = "zz" * 32
    rejects(manifest, gate)


def test_manifest_dataset_operational_gate_mismatch_rejected(documents):
    manifest, gate = documents
    manifest["licensing_review"]["operational_execution_gate"] = "CLEAR"
    rejects(manifest, gate)


def test_overall_verdict_stronger_than_dependency_rejected(documents):
    manifest, gate = documents
    manifest["licensing_review"]["overall_pilot_verdict"] = "CLEAR"
    gate["verdict"] = "CLEAR"
    rejects(manifest, gate)


def test_httpie_authors_revision_changed_and_removed_rejected(documents):
    manifest, gate = documents
    authors = next(r for r in gate["file_records"] if r["id"] == "httpie-001bda-authors")
    new_revision = "ffffffffffffffffffffffffffffffffffffffff"
    authors["revision"] = new_revision
    authors["source_url"] = (
        f"https://raw.githubusercontent.com/httpie/httpie/{new_revision}/AUTHORS.rst"
    )
    authors["revision_url"] = f"https://github.com/httpie/httpie/tree/{new_revision}"
    task = next(t for t in manifest["tasks"] if t["pilot_task_id"] == "bugsinpy-httpie-001")
    task["licensing"]["reviewed_license_record_ids"].remove("httpie-001bda-authors")
    rejects(manifest, gate)


def test_required_artifact_path_changed_rejected(documents):
    manifest, gate = documents
    gate["file_records"][0]["path"] = "COPYING"
    rejects(manifest, gate)


def test_required_artifact_record_kind_changed_rejected(documents):
    manifest, gate = documents
    gate["file_records"][0]["record_kind"] = "attribution_notice"
    rejects(manifest, gate)


def test_source_url_repository_mismatch_rejected(documents):
    manifest, gate = documents
    record = gate["file_records"][0]
    record["source_url"] = record["source_url"].replace("tiangolo/fastapi", "evil-user/fastapi")
    rejects(manifest, gate)


def test_source_url_revision_mismatch_rejected(documents):
    manifest, gate = documents
    record = gate["file_records"][0]
    record["source_url"] = record["source_url"].replace(record["revision"], "0" * 40)
    rejects(manifest, gate)


def test_source_url_path_mismatch_rejected(documents):
    manifest, gate = documents
    record = gate["file_records"][0]
    record["source_url"] = record["source_url"].replace("/LICENSE", "/COPYING")
    rejects(manifest, gate)


def test_revision_url_mismatch_rejected(documents):
    manifest, gate = documents
    record = gate["file_records"][0]
    record["revision_url"] = record["revision_url"].replace(record["revision"], "0" * 40)
    rejects(manifest, gate)


@pytest.mark.parametrize(
    "revision",
    [
        "766157BFB4E7DFCCBA09AB398E8EC444D14E947C",
        "766157bfb4e7dfccba09ab398e8ec444d14e94",
        "766157bfb4e7dfccba09ab398e8ec444d14e947g",
        "766157bfb4e7dfccba09ab398e8ec444d14e947c0",
    ],
)
def test_noncanonical_revision_sha_rejected(documents, revision):
    manifest, gate = documents
    gate["file_records"][0]["revision"] = revision
    rejects(manifest, gate)


def test_duplicate_repository_revision_path_rejected(documents):
    manifest, gate = documents
    duplicate = copy.deepcopy(gate["file_records"][0])
    duplicate["id"] = "fastapi-766157-license-duplicate"
    gate["file_records"].append(duplicate)
    project = next(p for p in gate["project_records"] if p["project"] == "fastapi")
    project["file_record_ids"].append(duplicate["id"])
    rejects(manifest, gate)


def test_extra_unselected_revision_record_rejected(documents):
    manifest, gate = documents
    new_revision = "1111111111111111111111111111111111111111"
    extra = {
        "id": "fastapi-extra-license",
        "repository": "tiangolo/fastapi",
        "revision": new_revision,
        "path": "LICENSE",
        "source_url": f"https://raw.githubusercontent.com/tiangolo/fastapi/{new_revision}/LICENSE",
        "revision_url": f"https://github.com/tiangolo/fastapi/tree/{new_revision}",
        "sha256": "4ec89ffc81485b97fec584b2d4a961032eeffe834453894fd9c1274906cc744e",
        "record_kind": "license",
        "spdx_identifier": "MIT",
        "attribution": "unrelated",
        "terms_same_at_buggy_and_fixed": True,
    }
    gate["file_records"].append(extra)
    project = next(p for p in gate["project_records"] if p["project"] == "fastapi")
    project["file_record_ids"].append(extra["id"])
    rejects(manifest, gate)


def test_project_file_record_not_covered_by_any_task_rejected(documents):
    manifest, gate = documents
    task = next(t for t in manifest["tasks"] if t["pilot_task_id"] == "bugsinpy-httpie-001")
    task["licensing"]["reviewed_license_record_ids"].remove("httpie-001bda-authors")
    rejects(manifest, gate)


def test_duplicate_task_reviewed_record_id_rejected(documents):
    manifest, gate = documents
    task = next(t for t in manifest["tasks"] if t["pilot_task_id"] == "bugsinpy-httpie-001")
    task["licensing"]["reviewed_license_record_ids"].append("httpie-001bda-license")
    rejects(manifest, gate)


@pytest.mark.parametrize(
    "record_id",
    ["fastapi-766157-license", "thefuck-2ced7a-license"],
)
def test_singular_project_spdx_mismatch_rejected(documents, record_id):
    manifest, gate = documents
    record = next(r for r in gate["file_records"] if r["id"] == record_id)
    record["spdx_identifier"] = "Apache-2.0"
    rejects(manifest, gate)


def test_httpie_license_spdx_mismatch_rejected(documents):
    manifest, gate = documents
    record = next(r for r in gate["file_records"] if r["id"] == "httpie-001bda-license")
    record["spdx_identifier"] = "MIT"
    rejects(manifest, gate)


def test_httpie_authors_spdx_incorrectly_populated_rejected(documents):
    manifest, gate = documents
    record = next(r for r in gate["file_records"] if r["id"] == "httpie-001bda-authors")
    record["spdx_identifier"] = "BSD-3-Clause"
    rejects(manifest, gate)


def test_tqdm_project_spdx_set_mismatch_rejected(documents):
    manifest, gate = documents
    project = next(p for p in gate["project_records"] if p["project"] == "tqdm")
    project["spdx_identifiers"] = ["MIT"]
    rejects(manifest, gate)


def test_missing_tqdm_scope_note_rejected(documents):
    manifest, gate = documents
    project = next(p for p in gate["project_records"] if p["project"] == "tqdm")
    project["scope_note"] = ""
    rejects(manifest, gate)


def test_review_package_matrix_matches_tracked_record_when_present(documents):
    if not PACKAGE_MATRIX_PATH.exists():
        pytest.skip("review package is optional in a clean checkout")
    _, gate = documents
    package = json.loads(PACKAGE_MATRIX_PATH.read_text(encoding="utf-8"))
    assert package == gate
