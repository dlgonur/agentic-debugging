from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_debugger.bugsinpy import (
    BugsInPyAdapter,
    BugsInPyMetadataPreflight,
    BugsInPyOperation,
    NoModelSmokeRunner,
    PreflightAuthorizationError,
)
from agentic_debugger.bugsinpy import adapter as adapter_module
from agentic_debugger.bugsinpy import metadata_preflight as metadata_module
from agentic_debugger.evaluation.task_schema import TaskSource


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "research" / "bugsinpy" / "PILOT_ELIGIBILITY_MANIFEST_V1.json"
GATE = ROOT / "research" / "bugsinpy" / "BUGSINPY_LICENSE_GATE_V1.json"
VALIDATOR = ROOT / "scripts" / "validate_bugsinpy_license_gate.py"
TASKS = [
    "bugsinpy-fastapi-001",
    "bugsinpy-fastapi-009",
    "bugsinpy-httpie-001",
    "bugsinpy-httpie-002",
    "bugsinpy-tqdm-002",
    "bugsinpy-tqdm-003",
    "bugsinpy-thefuck-001",
    "bugsinpy-thefuck-002",
]
NON_METADATA = [
    operation.value
    for operation in BugsInPyOperation
    if operation not in {BugsInPyOperation.INSPECT_METADATA, BugsInPyOperation.PACKAGE_METADATA_EVIDENCE}
]


def preflight(manifest: Path = MANIFEST, gate: Path = GATE, **kwargs):
    return BugsInPyMetadataPreflight(
        manifest_path=manifest,
        gate_path=gate,
    ).decide(**kwargs)


def copy_authorities(tmp_path: Path) -> tuple[Path, Path]:
    manifest = tmp_path / "manifest.json"
    gate = tmp_path / "gate.json"
    manifest.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    gate.write_text(GATE.read_text(encoding="utf-8"), encoding="utf-8")
    return manifest, gate


def affirmative_boundary(tmp_path: Path) -> BugsInPyMetadataPreflight:
    manifest_path, gate_path = copy_authorities(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    review = manifest["licensing_review"]
    review.update({
        "dataset_verdict": "CLEAR",
        "formal_license_status": "CLEAR",
        "redistribution_verdict": "CLEAR",
        "private_local_research_use_verdict": "CLEAR",
        "operational_execution_gate": "CLEAR",
        "overall_pilot_verdict": "CLEAR",
        "project_verdicts": {project: "CLEAR" for project in review["project_verdicts"]},
    })
    for task in manifest["tasks"]:
        licensing = task["licensing"]
        licensing.update({
            "dataset_verdict": "CLEAR",
            "formal_license_status": "CLEAR",
            "project_verdict": "CLEAR",
            "task_verdict": "CLEAR",
            "gate_status": "CLEAR",
            "redistribution_verdict": "CLEAR",
            "private_local_research_use_verdict": "CLEAR",
            "operational_execution_gate": "CLEAR",
        })
    dataset = gate["dataset_records"][0]
    dataset.update({
        "verdict": "CLEAR",
        "formal_license_status": "CLEAR",
        "redistribution_verdict": "CLEAR",
        "private_local_research_use_verdict": "CLEAR",
        "operational_execution_gate": "CLEAR",
    })
    for record in gate["project_records"]:
        record["verdict"] = "CLEAR"
    gate["verdict"] = "CLEAR"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    gate_path.write_text(json.dumps(gate), encoding="utf-8")
    return BugsInPyMetadataPreflight(
        manifest_path=manifest_path,
        gate_path=gate_path,
    )


def cli_result(*args: str) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, "-m", "agentic_debugger.bugsinpy.preflight_cli", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, result.stdout + result.stderr
    return result.returncode, json.loads(lines[0])


def test_current_authorities_validate_and_metadata_inspection_is_source_free() -> None:
    decision = preflight(task_id=TASKS[0], operation="inspect_metadata")
    assert decision.allowed
    assert decision.reason_code == "ALLOWED_METADATA_INSPECTION"
    assert decision.validation == {
        "status": "PASS",
        "manifest": "PASS",
        "canonical_gate": "PASS",
        "license_validator": "PASS",
    }
    assert decision.authority_revisions["manifest"] == decision.authority_revisions["gate_dataset"]


@pytest.mark.parametrize("operation", NON_METADATA)
def test_every_source_or_execution_operation_is_blocked_under_current_gate(operation: str) -> None:
    decision = preflight(task_id=TASKS[0], operation=operation, operator_authorization_state="approved", containment_readiness=True, dependency_readiness=True)
    assert decision.decision == "BLOCK"
    assert decision.reason_code == (
        "SOURCE_BEARING_EVIDENCE_PROHIBITED" if operation == "package_evidence" else "DATASET_VERDICT_BLOCKED"
    )


def test_all_eight_tasks_have_expected_current_decisions() -> None:
    for task_id in TASKS:
        metadata = preflight(task_id=task_id, operation="inspect_metadata")
        execution = preflight(task_id=task_id, operation="reproduce_bug", operator_authorization_state="approved", containment_readiness=True, dependency_readiness=True)
        assert metadata.decision == "ALLOW"
        assert execution.decision == "BLOCK"
        assert execution.reason_code == "DATASET_VERDICT_BLOCKED"
        assert execution.task_verdict == "BLOCKED"


def test_unknown_task_and_operation_are_blocked() -> None:
    assert preflight(task_id="bugsinpy-unknown-999", operation="inspect_metadata").reason_code == "UNKNOWN_TASK"
    assert preflight(task_id=TASKS[0], operation="run_everything").reason_code == "UNKNOWN_OPERATION"


def test_missing_authority_files_are_blocked(tmp_path: Path) -> None:
    assert preflight(manifest=tmp_path / "missing-manifest.json", task_id=TASKS[0], operation="inspect_metadata").reason_code == "AUTHORITY_MISSING"
    assert preflight(gate=tmp_path / "missing-gate.json", task_id=TASKS[0], operation="inspect_metadata").reason_code == "AUTHORITY_MISSING"


def test_malformed_json_is_blocked(tmp_path: Path) -> None:
    manifest, gate = copy_authorities(tmp_path)
    manifest.write_text("{", encoding="utf-8")
    assert preflight(manifest=manifest, gate=gate, task_id=TASKS[0], operation="inspect_metadata").reason_code == "AUTHORITY_JSON_INVALID"
    gate.write_text("[]", encoding="utf-8")
    assert preflight(manifest=manifest, gate=gate, task_id=TASKS[0], operation="inspect_metadata").reason_code == "AUTHORITY_JSON_INVALID"


def test_validator_failure_and_revision_mismatch_are_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, gate = copy_authorities(tmp_path)
    monkeypatch.setattr(metadata_module, "_validator_function", lambda: (_ for _ in ()).throw(ValueError("synthetic")))
    failing = BugsInPyMetadataPreflight(manifest_path=manifest, gate_path=gate)
    assert failing.decide(TASKS[0], "inspect_metadata").reason_code == "LICENSE_VALIDATOR_FAILED"
    data = json.loads(gate.read_text(encoding="utf-8"))
    data["dataset_records"][0]["revision"] = "0" * 40
    gate.write_text(json.dumps(data), encoding="utf-8")
    assert preflight(manifest=manifest, gate=gate, task_id=TASKS[0], operation="inspect_metadata").reason_code == "AUTHORITY_REVISION_MISMATCH"


def test_stronger_task_verdict_is_rejected(tmp_path: Path) -> None:
    manifest, gate = copy_authorities(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["tasks"][0]["licensing"]["task_verdict"] = "CLEAR"
    data["tasks"][0]["licensing"]["gate_status"] = "CLEAR"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    decision = preflight(manifest=manifest, gate=gate, task_id=TASKS[0], operation="inspect_metadata")
    assert decision.reason_code == "TASK_VERDICT_EXCEEDS_AUTHORITY"


@pytest.mark.parametrize("state", ["absent", "approved"])
def test_operator_approval_cannot_override_blocked_authority(state: str) -> None:
    decision = preflight(task_id=TASKS[0], operation="reproduce_bug", operator_authorization_state=state, containment_readiness=True, dependency_readiness=True)
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "DATASET_VERDICT_BLOCKED"


def test_readiness_metadata_cannot_override_blocked_authority() -> None:
    for operation in ("start_containment", "prepare_dependencies"):
        decision = preflight(task_id=TASKS[0], operation=operation, operator_authorization_state="approved", containment_readiness=True, dependency_readiness=True)
        assert decision.decision == "BLOCK"
        assert decision.reason_code == "DATASET_VERDICT_BLOCKED"


def test_unsupported_override_is_blocked() -> None:
    decision = preflight(task_id=TASKS[0], operation="inspect_metadata", override_flags={"force": True})
    assert decision.reason_code == "UNSUPPORTED_OVERRIDE"


def test_blocked_runner_starts_no_subprocess_network_workspace_or_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    class NoSideEffects:
        def acquire(self, *args, **kwargs):
            calls.append("network")
            raise AssertionError("acquisition was reached")

        def read_gold_patch(self, *args, **kwargs):
            calls.append("source")
            raise AssertionError("patch read was reached")

    monkeypatch.setattr(adapter_module.subprocess, "run", lambda *args, **kwargs: calls.append("subprocess"))
    monkeypatch.setattr(adapter_module.ExternalWorkspace, "create", lambda *args, **kwargs: calls.append("workspace"))
    adapter = BugsInPyAdapter.from_manifest(MANIFEST)
    evidence = NoModelSmokeRunner(adapter, NoSideEffects()).run(
        TASKS[0], facts=adapter_module.PreflightFacts(), external_parent=str(tmp_path), repository_root=str(ROOT)
    )
    assert evidence.verdict == "REAL_SMOKE_BLOCKED"
    assert evidence.preflight.reason_code == "DATASET_VERDICT_BLOCKED"
    assert calls == []


def test_cli_emits_one_json_object_and_nonzero_for_blocked_execution() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agentic_debugger.bugsinpy.preflight_cli", "--task", TASKS[0], "--operation", "reproduce_bug"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert result.returncode != 0
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["decision"] == "BLOCK"
    assert payload["reason_code"] == "DATASET_VERDICT_BLOCKED"


def test_sanitized_metadata_evidence_has_distinct_allowed_operation() -> None:
    allowed = preflight(task_id=TASKS[0], operation="package_metadata_evidence", evidence_handling="sanitized_metadata_only")
    prohibited = preflight(task_id=TASKS[0], operation="package_evidence", evidence_handling="sanitized_metadata_only")
    assert allowed.decision == "ALLOW"
    assert allowed.reason_code == "ALLOWED_SANITIZED_METADATA_EVIDENCE"
    assert prohibited.decision == "BLOCK"
    assert prohibited.reason_code == "SOURCE_BEARING_EVIDENCE_PROHIBITED"


def test_synthetic_affirmative_authority_issues_bounded_permit(tmp_path: Path) -> None:
    boundary = affirmative_boundary(tmp_path)
    decision = boundary.decide(
        TASKS[0],
        "acquire_source",
        operator_authorization_state="approved",
        containment_readiness=True,
        dependency_readiness=True,
    )
    assert decision.decision == "ALLOW"
    assert decision.reason_code == "OPERATION_ALLOWED"
    assert decision.permit is not None
    assert decision.permit.task_id == TASKS[0]


def _permission_boundary(tmp_path: Path, *, formal: str = "CLEAR", private_use: str = "CLEAR", redistribution: str = "CLEAR") -> BugsInPyMetadataPreflight:
    boundary = affirmative_boundary(tmp_path)
    manifest = json.loads(boundary.manifest_path.read_text(encoding="utf-8"))
    gate = json.loads(boundary.gate_path.read_text(encoding="utf-8"))
    manifest["licensing_review"].update({"formal_license_status": formal, "private_local_research_use_verdict": private_use, "redistribution_verdict": redistribution})
    for task in manifest["tasks"]:
        task["licensing"].update({"formal_license_status": formal, "private_local_research_use_verdict": private_use, "redistribution_verdict": redistribution})
    gate["dataset_records"][0].update({"formal_license_status": formal, "private_local_research_use_verdict": private_use, "redistribution_verdict": redistribution})
    boundary.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    boundary.gate_path.write_text(json.dumps(gate), encoding="utf-8")
    return boundary


def test_unknown_private_use_permission_blocks_source_even_with_clear_composites(tmp_path: Path) -> None:
    boundary = _permission_boundary(tmp_path, formal="CLEAR", private_use="UNKNOWN")
    decision = boundary.decide(TASKS[0], "acquire_source", operator_authorization_state="approved", containment_readiness=True, dependency_readiness=True)
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "PRIVATE_USE_PERMISSION_REQUIRED"
    assert decision.permit is None


def test_unknown_formal_license_blocks_source_without_local_use_basis(tmp_path: Path) -> None:
    boundary = _permission_boundary(tmp_path, formal="UNKNOWN", private_use="CLEAR")
    decision = boundary.decide(TASKS[0], "acquire_source", operator_authorization_state="approved", containment_readiness=True, dependency_readiness=True)
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "FORMAL_LICENSE_PERMISSION_REQUIRED"
    assert decision.permit is None


def test_blocked_redistribution_does_not_allow_source_bearing_evidence(tmp_path: Path) -> None:
    boundary = _permission_boundary(tmp_path, redistribution="BLOCKED")
    decision = boundary.decide(TASKS[0], "package_evidence", evidence_handling="sanitized_metadata_only")
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "SOURCE_BEARING_EVIDENCE_PROHIBITED"


@pytest.mark.parametrize(
    ("operation", "handling", "reason"),
    [
        ("acquire_source", "sanitized_metadata_only", "EVIDENCE_HANDLING_REQUIRED"),
        ("reproduce_bug", "sanitized_metadata_only", "EVIDENCE_HANDLING_REQUIRED"),
        ("inspect_metadata", "raw_upstream", "SOURCE_BEARING_EVIDENCE_PROHIBITED"),
        ("inspect_metadata", "mystery", "EVIDENCE_HANDLING_REQUIRED"),
    ],
)
def test_unrelated_operations_require_unspecified_evidence_handling(operation: str, handling: str, reason: str, tmp_path: Path) -> None:
    boundary = affirmative_boundary(tmp_path)
    decision = boundary.decide(TASKS[0], operation, evidence_handling=handling, operator_authorization_state="approved", containment_readiness=True, dependency_readiness=True)
    assert decision.decision == "BLOCK"
    assert decision.reason_code == reason


def test_current_authority_issues_no_acquisition_or_execution_permit() -> None:
    for operation in ("acquire_source", "verify_patch", "reproduce_bug"):
        decision = preflight(
            task_id=TASKS[0],
            operation=operation,
            operator_authorization_state="approved",
            containment_readiness=True,
            dependency_readiness=True,
        )
        assert decision.decision == "BLOCK"
        assert decision.permit is None


def test_direct_acquisition_without_permit_fails_before_git_or_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "external"
    parent.mkdir()
    workspace = adapter_module.ExternalWorkspace.create(parent, repository_root=str(ROOT))
    destination = workspace.source_dir / "tqdm"
    calls: list[object] = []
    monkeypatch.setattr(adapter_module, "_run_git", lambda *args, **kwargs: calls.append(args))
    try:
        with pytest.raises(PreflightAuthorizationError):
            adapter_module.GitSourceAcquirer().acquire(
                "https://github.com/tqdm/tqdm", "a" * 40, destination
            )
        assert calls == []
        assert not destination.exists()
    finally:
        workspace.cleanup()


def test_direct_acquisition_rejects_wrong_task_operation_and_revision_permits(tmp_path: Path) -> None:
    boundary = affirmative_boundary(tmp_path)
    acquire = boundary.decide(TASKS[0], "acquire_source", operator_authorization_state="approved", containment_readiness=True)
    verify = boundary.decide(TASKS[0], "verify_patch", operator_authorization_state="approved", containment_readiness=True, dependency_readiness=True)
    acquirer = adapter_module.GitSourceAcquirer(preflight_boundary=boundary)
    destination = tmp_path / "not-created"
    assert acquire.permit is not None and verify.permit is not None
    with pytest.raises(PreflightAuthorizationError):
        acquirer.acquire("https://github.com/tqdm/tqdm", "a" * 40, destination, task_id=TASKS[1], preflight_decision=acquire, permit=acquire.permit)
    with pytest.raises(PreflightAuthorizationError):
        acquirer.acquire("https://github.com/tqdm/tqdm", "a" * 40, destination, task_id=TASKS[0], preflight_decision=verify, permit=verify.permit)
    mismatched = replace(acquire, authority_revisions={"manifest": "0" * 40, "gate_dataset": "0" * 40})
    with pytest.raises(PreflightAuthorizationError):
        acquirer.acquire("https://github.com/tqdm/tqdm", "a" * 40, destination, task_id=TASKS[0], preflight_decision=mismatched, permit=acquire.permit)
    assert not destination.exists()


def test_direct_acquisition_rejects_stale_authority_permit(tmp_path: Path) -> None:
    boundary = affirmative_boundary(tmp_path)
    decision = boundary.decide(TASKS[0], "acquire_source", operator_authorization_state="approved", containment_readiness=True)
    assert decision.permit is not None
    gate = json.loads(boundary.gate_path.read_text(encoding="utf-8"))
    gate["dataset_records"][0]["revision"] = "0" * 40
    boundary.gate_path.write_text(json.dumps(gate), encoding="utf-8")
    with pytest.raises(PreflightAuthorizationError):
        adapter_module.GitSourceAcquirer(preflight_boundary=boundary).acquire(
            "https://github.com/tqdm/tqdm", "a" * 40, tmp_path / "not-created",
            task_id=TASKS[0], preflight_decision=decision, permit=decision.permit,
        )


def test_read_gold_patch_requires_permit_before_path_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    acquirer = adapter_module.GitSourceAcquirer()
    def forbidden(*args, **kwargs):
        raise AssertionError("path access occurred before authorization")
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    with pytest.raises(PreflightAuthorizationError):
        acquirer.read_gold_patch(tmp_path, "projects/tqdm/bugs/1/bug_patch.txt")


def test_read_gold_patch_rejects_mismatched_permit_before_path_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    boundary = affirmative_boundary(tmp_path)
    acquire = boundary.decide(TASKS[0], "acquire_source", operator_authorization_state="approved", containment_readiness=True)
    assert acquire.permit is not None
    monkeypatch.setattr(Path, "resolve", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("resolve reached")))
    with pytest.raises(PreflightAuthorizationError):
        adapter_module.GitSourceAcquirer(preflight_boundary=boundary).read_gold_patch(
            tmp_path, "projects/tqdm/bugs/1/bug_patch.txt", task_id=TASKS[0], preflight_decision=acquire, permit=acquire.permit
        )


@pytest.mark.parametrize("handling", ["unspecified", "source_bearing", "raw_upstream", "mystery"])
def test_package_metadata_evidence_rejects_non_sanitized_handling(handling: str) -> None:
    decision = preflight(task_id=TASKS[0], operation="package_metadata_evidence", evidence_handling=handling)
    assert decision.decision == "BLOCK"
    assert decision.reason_code in {"EVIDENCE_HANDLING_REQUIRED", "SOURCE_BEARING_EVIDENCE_PROHIBITED"}


@pytest.mark.parametrize("handling", ["unspecified", "sanitized_metadata_only", "source_bearing", "raw_upstream", "mystery"])
def test_package_evidence_is_always_blocked(handling: str) -> None:
    decision = preflight(task_id=TASKS[0], operation="package_evidence", evidence_handling=handling)
    assert decision.decision == "BLOCK"
    assert decision.reason_code == "SOURCE_BEARING_EVIDENCE_PROHIBITED"


@pytest.mark.parametrize(
    ("args", "reason_code"),
    [
        (("--task", TASKS[0], "--operation", "not-an-operation"), "UNKNOWN_OPERATION"),
        (("--operation", "inspect_metadata"), "TASK_ID_REQUIRED"),
        (("--task", "bugsinpy-unknown-999", "--operation", "inspect_metadata"), "UNKNOWN_TASK"),
        (("--task", TASKS[0], "--operation", "inspect_metadata", "--operator-authorization", "maybe"), "UNSUPPORTED_OVERRIDE"),
        (("--task", TASKS[0], "--operation", "package_metadata_evidence", "--evidence-handling", "mystery"), "EVIDENCE_HANDLING_REQUIRED"),
    ],
)
def test_cli_semantic_invalid_inputs_are_one_json_decision(args: tuple[str, ...], reason_code: str) -> None:
    code, payload = cli_result(*args)
    assert code != 0
    assert payload["decision"] == "BLOCK"
    assert payload["reason_code"] == reason_code


def test_cli_missing_malformed_and_validator_failure_are_json_decisions(tmp_path: Path) -> None:
    code, payload = cli_result("--task", TASKS[0], "--operation", "inspect_metadata", "--manifest", str(tmp_path / "missing.json"))
    assert code != 0 and payload["reason_code"] == "AUTHORITY_MISSING"
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    code, payload = cli_result("--task", TASKS[0], "--operation", "inspect_metadata", "--manifest", str(malformed))
    assert code != 0 and payload["reason_code"] == "AUTHORITY_JSON_INVALID"
    with pytest.raises(TypeError):
        BugsInPyMetadataPreflight(manifest_path=MANIFEST, gate_path=GATE, validator_path=tmp_path / "validator.py")  # type: ignore[call-arg]


def test_smoke_evidence_serializes_both_preflight_result_types() -> None:
    metadata = preflight(task_id=TASKS[0], operation="inspect_metadata")
    legacy = adapter_module.PreflightReport(TASKS[0], "a" * 64, tuple())
    for result in (metadata, legacy):
        evidence = adapter_module.SmokeEvidence(TASKS[0], "REAL_SMOKE_BLOCKED", result, None, None, False, True, None)
        assert evidence.to_mapping()["preflight"] == result.to_mapping()


def test_synthetic_permitted_runner_reaches_only_mocked_collaborators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    boundary = affirmative_boundary(tmp_path)
    adapter = BugsInPyAdapter.from_manifest(MANIFEST)
    adapter.metadata_boundary = boundary
    adapter.preflight = lambda *args, **kwargs: adapter_module.PreflightReport(
        TASKS[0], adapter.manifest.fingerprint,
        tuple(adapter_module.GateResult(name, adapter_module.GateStatus.PASS, "synthetic") for name in adapter_module.GateName),
    )
    calls: list[tuple[str, str]] = []

    class FakeWorkspace:
        def __init__(self) -> None:
            self.root = tmp_path / "case"
            self.source_dir = self.root / "sources"
            self.verifier_workspace_parent = self.root / "verifier-workspaces"
            self.source_dir.mkdir(parents=True, exist_ok=True)
        def assert_contained(self, path: Path) -> None:
            return None
        def materialize_project(self, source: Path, project_name: str, provenance) -> TaskSource:
            self.verifier_workspace_parent.mkdir(parents=True, exist_ok=True)
            return TaskSource("external", "sources/" + project_name, dict(provenance))
        def cleanup(self) -> None:
            return None

    class FakeAcquirer:
        def acquire(self, url, revision, destination, *, task_id, preflight_decision, permit):
            assert preflight_decision.permit is permit
            calls.append(("acquire", preflight_decision.requested_operation))
            destination.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(root=destination)
        def read_gold_patch(self, framework_root, metadata_path, *, task_id, preflight_decision, permit):
            assert preflight_decision.permit is permit
            calls.append(("patch", preflight_decision.requested_operation))
            return ""

    class FakeVerifier:
        def __init__(self, *args, **kwargs):
            calls.append(("verifier", "verify_patch"))
        def evaluate(self, task, patch):
            return SimpleNamespace(status=SimpleNamespace(value="COMPLETED"), semantic_mapping=lambda: {"status": "COMPLETED"})

    monkeypatch.setattr(adapter_module.ExternalWorkspace, "create", lambda *args, **kwargs: FakeWorkspace())
    facts = adapter_module.PreflightFacts(
        operator_authorization_state="approved",
        containment_ready=True,
        dependency_install_boundary_ready=True,
        target_annotation_reviewed=True,
        execution_context=SimpleNamespace(containment=SimpleNamespace(root=str(tmp_path))),
    )
    evidence = NoModelSmokeRunner(adapter, FakeAcquirer(), verifier_factory=FakeVerifier).run(
        TASKS[0], facts=facts, external_parent=str(tmp_path), repository_root=str(ROOT), target_symbols=["target.symbol"]
    )
    assert evidence.verdict == "REAL_SMOKE_PASSED"
    assert calls == [("acquire", "acquire_source"), ("acquire", "acquire_source"), ("patch", "verify_patch"), ("verifier", "verify_patch")]


def _canonicalize_synthetic_authority(boundary: BugsInPyMetadataPreflight, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata_module, "CANONICAL_MANIFEST_PATH", boundary.manifest_path.resolve())
    monkeypatch.setattr(metadata_module, "CANONICAL_GATE_PATH", boundary.gate_path.resolve())


def _mocked_scoped_acquirer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_id: str, operation: str = "acquire_source"):
    boundary = affirmative_boundary(tmp_path)
    _canonicalize_synthetic_authority(boundary, monkeypatch)
    decision = boundary.decide(task_id, operation, operator_authorization_state="approved", containment_readiness=True, dependency_readiness=True)
    assert decision.allowed and decision.permit is not None
    monkeypatch.setattr(adapter_module, "_owned_external_root", lambda _path: True)
    selected_revision = {"value": ""}
    def mocked_git(args, cwd):
        if args[0] == "-c":
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        if args[0] == "checkout":
            selected_revision["value"] = args[-1]
        return SimpleNamespace(stdout=(selected_revision["value"] if args[0] == "rev-parse" else ""))
    monkeypatch.setattr(adapter_module, "_run_git", mocked_git)
    return boundary, decision, adapter_module.GitSourceAcquirer(preflight_boundary=boundary)


def test_task_permit_scope_rejects_wrong_project_and_revision_before_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, decision, acquirer = _mocked_scoped_acquirer(tmp_path, monkeypatch, TASKS[0])
    destination = tmp_path / "not-created"
    with pytest.raises(PreflightAuthorizationError):
        acquirer.acquire("https://github.com/tqdm/tqdm", "a" * 40, destination, task_id=TASKS[0], preflight_decision=decision, permit=decision.permit)
    assert not destination.exists()
    entry = json.loads(MANIFEST.read_text(encoding="utf-8"))["tasks"][0]["bugsinpy"]
    with pytest.raises(PreflightAuthorizationError):
        acquirer.acquire(entry["project_url"], "a" * 40, destination, task_id=TASKS[0], preflight_decision=decision, permit=decision.permit)
    assert not destination.exists()


def test_framework_acquisition_requires_exact_pinned_authority_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, decision, acquirer = _mocked_scoped_acquirer(tmp_path, monkeypatch, TASKS[0])
    destination = tmp_path / "framework"
    with pytest.raises(PreflightAuthorizationError):
        acquirer.acquire("https://github.com/soarsmu/BugsInPy", "a" * 40, destination, task_id=TASKS[0], preflight_decision=decision, permit=decision.permit)
    assert not destination.exists()


def test_selected_project_url_and_canonical_httpie_identity_are_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, decision, acquirer = _mocked_scoped_acquirer(tmp_path, monkeypatch, TASKS[2])
    entry = json.loads(MANIFEST.read_text(encoding="utf-8"))["tasks"][2]["bugsinpy"]
    destination = tmp_path / "httpie"
    acquirer.acquire(entry["project_url"], entry["buggy_revision"], destination, task_id=TASKS[2], preflight_decision=decision, permit=decision.permit)
    assert destination.exists()
    with pytest.raises(PreflightAuthorizationError):
        acquirer.acquire("https://github.com/jakubroztocil/httpie/", entry["buggy_revision"], tmp_path / "obsolete", task_id=TASKS[2], preflight_decision=decision, permit=decision.permit)


def test_patch_scope_rejects_wrong_task_project_and_bug_before_path_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, decision, acquirer = _mocked_scoped_acquirer(tmp_path, monkeypatch, TASKS[0], "verify_patch")
    framework = tmp_path / "framework"
    monkeypatch.setattr(Path, "resolve", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("path resolved before scope rejection")))
    with pytest.raises(PreflightAuthorizationError):
        acquirer.read_gold_patch(framework, "projects/httpie/bugs/1/bug_patch.txt", task_id=TASKS[0], preflight_decision=decision, permit=decision.permit)
    with pytest.raises(PreflightAuthorizationError):
        acquirer.read_gold_patch(framework, "projects/fastapi/bugs/9/bug_patch.txt", task_id=TASKS[0], preflight_decision=decision, permit=decision.permit)


def test_duplicate_patch_declaration_is_rejected(tmp_path: Path) -> None:
    manifest, gate = copy_authorities(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["tasks"][0]["bugsinpy"]["metadata_paths"].append(data["tasks"][0]["bugsinpy"]["metadata_paths"][-1])
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert preflight(manifest=manifest, gate=gate, task_id=TASKS[0], operation="inspect_metadata").reason_code == "RESOURCE_SCOPE_INVALID"


@pytest.mark.parametrize("mutation", ["verdict", "manifest", "gate", "validator"])
def test_authority_snapshot_revokes_permit_on_verdict_or_content_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    boundary, decision, acquirer = _mocked_scoped_acquirer(tmp_path, monkeypatch, TASKS[0])
    if mutation == "verdict":
        gate = json.loads(boundary.gate_path.read_text(encoding="utf-8"))
        gate["dataset_records"][0]["verdict"] = "BLOCKED"
        boundary.gate_path.write_text(json.dumps(gate), encoding="utf-8")
    elif mutation == "manifest":
        manifest = json.loads(boundary.manifest_path.read_text(encoding="utf-8"))
        manifest["selection_status"] = "changed-after-permit"
        boundary.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif mutation == "gate":
        gate = json.loads(boundary.gate_path.read_text(encoding="utf-8"))
        gate["review_note_for_test"] = "changed-after-permit"
        boundary.gate_path.write_text(json.dumps(gate), encoding="utf-8")
    else:
        original_hash = metadata_module._sha256_file
        monkeypatch.setattr(metadata_module, "_sha256_file", lambda path: "0" * 64 if path == metadata_module.CANONICAL_VALIDATOR_PATH else original_hash(path))
    with pytest.raises(PreflightAuthorizationError):
        acquirer.acquire("https://github.com/tiangolo/fastapi", "766157bfb4e7dfccba09ab398e8ec444d14e947c", tmp_path / "revoked", task_id=TASKS[0], preflight_decision=decision, permit=decision.permit)


def test_noncanonical_synthetic_authority_permit_is_rejected_by_real_acquirer(tmp_path: Path) -> None:
    boundary = affirmative_boundary(tmp_path)
    decision = boundary.decide(TASKS[0], "acquire_source", operator_authorization_state="approved", containment_readiness=True, dependency_readiness=True)
    assert decision.allowed and decision.permit is not None
    with pytest.raises(PreflightAuthorizationError):
        adapter_module.GitSourceAcquirer(preflight_boundary=boundary).acquire("https://github.com/tiangolo/fastapi", "766157bfb4e7dfccba09ab398e8ec444d14e947c", tmp_path / "rejected", task_id=TASKS[0], preflight_decision=decision, permit=decision.permit)


def test_production_boundary_rejects_validator_injection_and_does_not_import_module(tmp_path: Path) -> None:
    marker = tmp_path / "imported"
    module = tmp_path / "validator.py"
    module.write_text(f"{marker!s}.write_text('executed')\n", encoding="utf-8")
    with pytest.raises(TypeError):
        BugsInPyMetadataPreflight(validator=module)  # type: ignore[call-arg]
    assert not marker.exists()


def _official_receipt_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    boundary, acquire_decision, acquirer = _mocked_scoped_acquirer(tmp_path, monkeypatch, TASKS[0])
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["tasks"] if item["pilot_task_id"] == TASKS[0])["bugsinpy"]
    framework = acquirer.acquire(
        "https://github.com/soarsmu/BugsInPy",
        manifest["authority"]["official_repository_revision"],
        tmp_path / "framework",
        task_id=TASKS[0],
        preflight_decision=acquire_decision,
        permit=acquire_decision.permit,
    )
    verify = boundary.decide(TASKS[0], "verify_patch", operator_authorization_state="approved", containment_readiness=True, dependency_readiness=True)
    assert verify.allowed and verify.permit is not None
    patch_path = framework.root / entry["metadata_paths"][-1]
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text("sanitized synthetic patch", encoding="utf-8")
    return boundary, acquirer, framework, verify, entry["metadata_paths"][-1]


def test_arbitrary_patch_root_is_rejected_before_path_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, acquirer, _, verify, metadata_path = _official_receipt_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, "resolve", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("arbitrary patch root was resolved")))
    with pytest.raises(PreflightAuthorizationError):
        acquirer.read_gold_patch(tmp_path / "tampered", metadata_path, task_id=TASKS[0], preflight_decision=verify, permit=verify.permit)


def test_project_receipt_cannot_authorize_framework_patch_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    boundary, acquire_decision, acquirer = _mocked_scoped_acquirer(tmp_path, monkeypatch, TASKS[0])
    entry = json.loads(MANIFEST.read_text(encoding="utf-8"))["tasks"][0]["bugsinpy"]
    project = acquirer.acquire(entry["project_url"], entry["buggy_revision"], tmp_path / "project", task_id=TASKS[0], preflight_decision=acquire_decision, permit=acquire_decision.permit)
    verify = boundary.decide(TASKS[0], "verify_patch", operator_authorization_state="approved", containment_readiness=True, dependency_readiness=True)
    with pytest.raises(PreflightAuthorizationError):
        acquirer.read_gold_patch(project, entry["metadata_paths"][-1], task_id=TASKS[0], preflight_decision=verify, permit=verify.permit)


def test_receipt_for_another_task_is_rejected_before_patch_path_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, task_zero_receipt, _, metadata_path = _official_receipt_fixture(tmp_path, monkeypatch)
    boundary = task_zero_receipt  # retain the issuer-bound receipt as the adversarial input
    other_root = tmp_path / "other"
    other_root.mkdir()
    other_boundary, other_decision, acquirer = _mocked_scoped_acquirer(other_root, monkeypatch, TASKS[1], "verify_patch")
    with pytest.raises(PreflightAuthorizationError):
        acquirer.read_gold_patch(boundary, metadata_path, task_id=TASKS[1], preflight_decision=other_decision, permit=other_decision.permit)


def test_receipt_with_wrong_framework_revision_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, acquirer, framework, verify, metadata_path = _official_receipt_fixture(tmp_path, monkeypatch)
    object.__setattr__(framework, "_revision", "a" * 40)
    with pytest.raises(PreflightAuthorizationError):
        acquirer.read_gold_patch(framework, metadata_path, task_id=TASKS[0], preflight_decision=verify, permit=verify.permit)


def test_receipt_with_stale_authority_snapshot_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    boundary, acquirer, framework, _, metadata_path = _official_receipt_fixture(tmp_path, monkeypatch)
    manifest = json.loads(boundary.manifest_path.read_text(encoding="utf-8"))
    manifest["selection_status"] = "changed-after-receipt"
    boundary.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    verify = boundary.decide(TASKS[0], "verify_patch", operator_authorization_state="approved", containment_readiness=True, dependency_readiness=True)
    assert verify.allowed
    with pytest.raises(PreflightAuthorizationError):
        acquirer.read_gold_patch(framework, metadata_path, task_id=TASKS[0], preflight_decision=verify, permit=verify.permit)


def test_official_receipt_allows_only_exact_task_patch_in_mocked_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, acquirer, framework, verify, metadata_path = _official_receipt_fixture(tmp_path, monkeypatch)
    assert acquirer.read_gold_patch(framework, metadata_path, task_id=TASKS[0], preflight_decision=verify, permit=verify.permit) == "sanitized synthetic patch"
    with pytest.raises(PreflightAuthorizationError):
        acquirer.read_gold_patch(framework, "projects/fastapi/bugs/9/bug_patch.txt", task_id=TASKS[0], preflight_decision=verify, permit=verify.permit)


def test_relative_authority_paths_resolve_to_canonical_and_other_paths_do_not(tmp_path: Path) -> None:
    relative = BugsInPyMetadataPreflight(
        manifest_path="research/bugsinpy/PILOT_ELIGIBILITY_MANIFEST_V1.json",
        gate_path="research/bugsinpy/BUGSINPY_LICENSE_GATE_V1.json",
    )
    decision = relative.decide(TASKS[0], "inspect_metadata")
    assert decision.allowed
    assert decision.authority_snapshot is not None and decision.authority_snapshot.canonical_paths is True
    manifest, gate = copy_authorities(tmp_path)
    synthetic = BugsInPyMetadataPreflight(manifest_path=Path(os.path.relpath(manifest, Path.cwd())), gate_path=Path(os.path.relpath(gate, Path.cwd())))
    assert synthetic.manifest_path != relative.manifest_path
    assert synthetic.gate_path != relative.gate_path


def test_documented_relative_manifest_smoke_path_is_not_rejected() -> None:
    decision = BugsInPyMetadataPreflight().decide(TASKS[0], "inspect_metadata")
    assert decision.allowed
