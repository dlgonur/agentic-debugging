"""Focused tests for the R6 professor-facing trace exporter.

These tests run against SMALL synthetic evidence fixtures (built in the
tmp_path) — they never depend on the large ignored historical run trees.
The production exporter pins the accepted R6 evidence identity in its
frozen registry; here an injected ``EvidenceRegistry`` pins the synthetic
evidence instead, exercising the exact same fail-closed, deterministic,
schema-validated, leakage-audited export path.

The accepted anti-leakage authority lives under ``experiments/`` (a
namespace package), so the repository root is added to ``sys.path`` the
same way ``tests/unit/test_r5_anti_leakage.py`` does.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.evaluation.professor_trace import validate_trace
from agentic_debugger.evaluation.professor_trace_r6 import (
    ADAPTER_MODEL_SHA256,
    BASE_REPOSITORY,
    BASE_REVISION,
    EVIDENCE_CONTRACT_SHA256,
    EvidenceRegistry,
    EvidenceResolver,
    _audit_trace,
    _stable_json,
    audit_exported_text,
    build_index_r6,
    build_trace_r6,
    export_professor_traces_r6,
    verify_evidence,
)

CONTRACT = EVIDENCE_CONTRACT_SHA256


# ---------------------------------------------------------------------------
# Synthetic fixture and evidence builders
# ---------------------------------------------------------------------------


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, obj) -> str:
    text = json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" keeps exact "\n" bytes (evidence identity is byte-based)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_curated_fixture(
    root: Path,
    task_id: str,
    *,
    module_name: str = "synthetic_mod.py",
    function_name: str = "synthetic_fn",
    hidden_literal: str = "golden-answer-xyz",
    oracle_summary: str = "The synthetic function returns a wrong value.",
) -> Path:
    """A minimal tracked-style curated fixture with hidden tests."""
    fixture = root / task_id
    (fixture / "tests").mkdir(parents=True, exist_ok=True)
    (fixture / module_name).write_text(
        f"def {function_name}(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    (fixture / "tests" / f"test_{function_name}.py").write_text(
        f'def test_wrong():\n    assert {function_name}(1) == 2  # {hidden_literal}\n'
        f"def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    _write_json(
        fixture / "task.json",
        {
            "schema_version": "1.0",
            "task_id": task_id,
            "title": "Synthetic task",
            "description": "A deterministic helper produces incorrect results.",
            "tests": {
                "fail_to_pass": [f"tests/test_{function_name}.py::test_wrong"],
                "pass_to_pass": [f"tests/test_{function_name}.py::test_ok"],
            },
            "constraints": {"allowed_write_paths": [module_name]},
            "oracle": {
                "bug_category": f"{task_id}-category",
                "target_files": [module_name],
                "target_symbols": [function_name],
                "root_cause_summary": oracle_summary,
            },
        },
    )
    return fixture


def build_evidence(
    *,
    task_id: str,
    module_path: str = "synthetic_mod.py",
    function: str = "synthetic_fn",
    outcome: str = "RESOLVED",
    f2p_passed: int = 1,
    f2p_total: int = 1,
    p2p_passed: int = 2,
    p2p_total: int = 2,
    full_suite: bool = True,
    breakpoint_line: int = 2,
    production_exception: bool = False,
    with_frames: bool = True,
    failure_output: str = "baseline behavioral check failed after executing the target behavior",
    candidate_sha256: str = "1111111111111111111111111111111111111111111111111111111111111111",
    diagnosis_text: str = "diagnosis synthetic_fn adds instead of correcting the offset.",
    r5_model: bool = False,  # kept for caller compatibility; no-op
) -> dict:
    """Minimal realistic ``debugger-interaction-v2-r5-evidence`` record."""
    module_frame = {
        "frame_id": 0,
        "function": function,
        "is_current": True,
        "line": breakpoint_line,
        "script": module_path,
    }
    test_frame = {
        "frame_id": 1,
        "function": "test_wrong",
        "is_current": False,
        "line": 2,
        "script": f"tests/test_{function}.py",
    }
    frames = [module_frame, test_frame] if with_frames else [module_frame]

    def obs(oid: int, name: str, payload: dict) -> str:
        return json.dumps(
            {
                "event_id": f"event-{oid}",
                "event_type": "observation",
                "name": name,
                "payload": {
                    "observation": {
                        "observation_id": f"observation-{oid:09d}",
                        "name": name,
                        "status": "ok",
                        "payload": payload,
                    }
                },
            },
            ensure_ascii=False,
        )

    lines = [
        obs(
            0,
            "run_reproduction",
            {
                "phase": "baseline",
                "exit_code": 1,
                "expected_exit_code": 1,
                "failure_output": failure_output,
                "failure_reproduced": True,
                "node_id": f"tests/test_{function}.py::test_wrong",
            },
        ),
        obs(1, "start_pdb_session", {"script": module_path, "function": function,
                                     "line": breakpoint_line, "state": "paused"}),
        obs(
            2,
            "get_stack_summary",
            {
                "script": module_path,
                "state": "paused",
                "pause_generation": 1,
                "frames": frames,
            },
        ),
        obs(
            3,
            "get_frame_locals",
            {"frame_id": 0, "state": "paused", "pause_generation": 1,
             "locals": []},
        ),
        obs(
            4,
            "next_pdb_session",
            {"script": module_path, "function": function,
             "line": breakpoint_line + 1, "state": "paused"},
        ),
    ]
    if production_exception:
        lines.append(
            obs(
                5,
                "get_stack_summary",
                {
                    "script": module_path,
                    "state": "paused",
                    "pause_generation": 1,
                    "frames": [module_frame],
                    "production_exception": (
                        f"{module_path}:{breakpoint_line}: ValueError: boom"
                    ),
                },
            )
        )
    else:
        lines.append(
            obs(
                5,
                "get_stack_summary",
                {
                    "script": module_path,
                    "state": "paused",
                    "pause_generation": 2,
                    "frames": frames,
                },
            )
        )
    lines.append(
        obs(
            6,
            "apply_patch",
            {
                "applied": True,
                "patch_sha256": candidate_sha256,
                "changed_files": [module_path],
            },
        )
    )

    def telemetry_entry(
        index: int,
        state: str,
        command: str,
        action_name: str,
        prior_obs: str,
        is_diagnosis: bool = False,
    ) -> dict:
        directive = {
            "is_diagnosis": is_diagnosis,
            "action_name": action_name,
        }
        if is_diagnosis:
            directive["diagnosis_text"] = diagnosis_text
        return {
            "model_call_index": index,
            "controller_state": state,
            "raw_response_text": command,
            "parse_result": {"status": "accepted"},
            "translated_directive": directive,
            "provenance": {"prior_observation_id": prior_obs},
        }

    telemetry = [
        telemetry_entry(0, "Reproduce", "reproduce", "run_reproduction",
                        "observation-000000000"),
        telemetry_entry(1, "RuntimeEvidence", f"break {breakpoint_line}",
                        "start_pdb_session", "observation-000000001"),
        telemetry_entry(2, "RuntimeEvidence", "stack", "get_stack_summary",
                        "observation-000000002"),
        telemetry_entry(3, "RuntimeEvidence", "locals", "get_frame_locals",
                        "observation-000000003"),
        telemetry_entry(4, "RuntimeEvidence", "next", "next_pdb_session",
                        "observation-000000004"),
        telemetry_entry(5, "RuntimeEvidence", "stack", "get_stack_summary",
                        "observation-000000005"),
        telemetry_entry(6, "RuntimeEvidence", f"diagnosis {diagnosis_text}",
                        "diagnosis", "observation-000000005",
                        is_diagnosis=True),
        telemetry_entry(7, "Patch", f"file {module_path}", "apply_patch",
                        "observation-000000005"),
    ]

    gate_chain = {
        "passed": True,
        "reason": "model authored break->stack G1->locals->step->stack G2->diagnosis",
        "terminal_path": production_exception,
        "production_exception_path": production_exception,
        "step_outside_region": False,
        "observation_ids": {
            "break": "observation-000000001",
            "stack_G1": "observation-000000002",
            "inspection": "observation-000000003",
            "step": "observation-000000004",
            "stack_G2": "observation-000000005",
        },
        "G1": 1,
        "G2": None if production_exception else 2,
        "diagnosis_text": diagnosis_text,
    }

    f2p_records = [{"node_id": f"tests/test_{function}.py::test_wrong",
                    "status": "PASS"}]
    p2p_records = [
        {"node_id": f"tests/test_{function}.py::test_ok", "status": "PASS"}
        for _ in range(p2p_total)
    ]

    return {
        "schema_version": "debugger-interaction-v2-r5-evidence",
        "run_identity": {
            "schema_version": "debugger-interaction-v2-r5-identity",
            "experiment_id": "synthetic",
            "source_commit_sha": "1111111111111111111111111111111111111111",
            "experiment_contract_sha256": (
                CONTRACT
            ),
            "base_repository": BASE_REPOSITORY,
            "base_revision": BASE_REVISION,
            "adapter_applied": True,
            "adapter_label": "r6-tuned",
            "system_prompt_template_sha256": "2222222222222222222222222222222222222222",
            "interface_revision": "r6.8",
        },
        "task": {
            "task_id": task_id,
            "bug_category": f"{task_id}-category",
            "module_path": module_path,
            "runtime_appended_driver_start_line": 3,
        },
        "trajectory_jsonl": "\n".join(lines),
        "telemetry": telemetry,
        "gate_results": {"gate_chain": gate_chain, "gate_patch": {}},
        "diagnosis_provenance": {
            "model_call_index": 6,
            "prior_observation_id": "observation-000000005",
            "G1": 1,
            "G2": None if production_exception else 2,
        },
        "verifier": {
            "executed": True,
            "status": "COMPLETED",
            "outcome": outcome,
            "stop_reason": "completed",
            "candidate_sha256": candidate_sha256,
            "f2p_total": f2p_total,
            "f2p_passed": f2p_passed,
            "p2p_total": p2p_total,
            "p2p_passed": p2p_passed,
            "full_suite_consistent": full_suite,
            "syntax_passed": True,
            "canonical_fixture_unchanged": True,
            "workspace_lifecycle": "CLEANED",
            "f2p_records": f2p_records,
            "p2p_records": p2p_records,
        },
        "verifier_feedback_history": [
            {
                "status": "COMPLETED",
                "outcome": outcome,
                "candidate_sha256": candidate_sha256,
            }
        ],
        "patch_identity": {
            "attempts": [
                {
                    "model_patch_raw_sha256": candidate_sha256,
                    "model_patch_serialization_normalized_sha256": candidate_sha256,
                }
            ],
            "final_candidate_sha256": candidate_sha256,
        },
        "serialization_normalization": {
            "note": "normalization output SHA == verifier input SHA",
            "verifier_input_sha256": candidate_sha256,
            "patchmanager_input_sha256": candidate_sha256,
        },
        "cleanup": {"release_pdb": [], "workspace_cleanup": "cleaned"},
        "controller_result": {
            "final_state": "Done",
            "stop_reason": "done",
            "model_calls": 8,
        },
        "budget_limits": {},
        "runtime": {"controller_duration_ms": 1, "total_duration_ms": 2},
        "tool_errors": [],
    }


def build_evidence_root(tmp_path: Path) -> tuple[Path, EvidenceRegistry]:
    """A frozen-fixture-shaped evidence root for one synthetic task per scope."""
    task_id = "synthetic-bug-001"
    curated_root = tmp_path / "curated"
    build_curated_fixture(curated_root, task_id)

    root = tmp_path / "evidence"
    scopes = {
        "validation": (root / "validation" / task_id / "evidence.json", build_evidence(task_id=task_id)),
        "final_holdout_partial": (
            root / "final_holdout_partial" / task_id / "evidence.json",
            build_evidence(
                task_id=task_id,
                outcome="BREAKING_RESOLVED",
                f2p_passed=1,
                f2p_total=1,
                p2p_passed=1,
                p2p_total=2,
                full_suite=False,
            ),
        ),
    }
    hashes: dict[str, str] = {}
    outcomes: dict[str, str] = {}
    for scope, (path, evidence) in scopes.items():
        text = json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" keeps exact "\n" bytes (evidence identity is byte-based)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        hashes[scope] = hashlib.sha256(path.read_bytes()).hexdigest()
        outcomes[scope] = evidence["verifier"]["outcome"]

    ancillary = {}
    for key in ("checkpoint_selection", "stage_a_report", "stage_b_report",
                "stage_c_report"):
        sha = _write_json(root / "ancillary" / f"{key}.json", {"key": key})
        ancillary[key] = sha

    registry = EvidenceRegistry(
        validation={task_id: hashes["validation"]},
        final_holdout_partial={task_id: hashes["final_holdout_partial"]},
        outcomes={
            f"validation:{task_id}": outcomes["validation"],
            f"final_holdout_partial:{task_id}": outcomes["final_holdout_partial"],
        },
        ancillary=ancillary,
        contract_sha256=CONTRACT,
        base_repository=BASE_REPOSITORY,
        base_revision=BASE_REVISION,
    )
    return root, registry


def _resolver(root: Path) -> EvidenceResolver:
    """Hermetic resolver for synthetic fixtures: no repo-package or live
    fallbacks."""
    return EvidenceResolver(
        root,
        pkg_root=root / "_nonexistent_pkg",
        live_root=root / "_nonexistent_live",
    )


def _validation_trace(root: Path, registry: EvidenceRegistry) -> dict:
    resolver = _resolver(root)
    paths = verify_evidence(
        resolver, registry=registry, include_holdout=True,
    )
    evidence = json.loads(
        paths["validation:synthetic-bug-001"].read_text(encoding="utf-8")
    )
    return build_trace_r6(
        evidence,
        scope="validation",
        model_identity={
            "fine_tuned_checkpoint": "checkpoint-30",
            "adapter_identity_sha256": ADAPTER_MODEL_SHA256,
            "training_provenance": "synthetic",
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_export_validation_trace_success(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    curated = tmp_path / "curated"
    output = tmp_path / "out"

    artifacts = export_professor_traces_r6(
        root,
        output,
        registry=registry,
        curated_root=curated,
        source_commit_sha="1111111111111111111111111111111111111111",
    )

    trace_path = output / "r6_validation" / "professor_debug_trace_synthetic-bug-001.json"
    assert trace_path.is_file()
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    validate_trace(trace)
    assert trace["schema_version"] == "professor_debug_trace_v1"
    assert trace["task_id"] == "synthetic-bug-001"
    assert trace["evidence_scope"] == "validation"
    assert trace["final_verification"]["outcome"] == "RESOLVED"
    assert trace["model"]["fine_tuned_checkpoint"] == "checkpoint-30"

    index = json.loads(
        (output / "r6_validation_index.json").read_text(encoding="utf-8")
    )
    assert index["validation_result"] == "1/1 RESOLVED"
    assert index["holdout_used_for_checkpoint_selection"] is False
    assert index["final_holdout_status"] == "INCOMPLETE_HARDWARE_STOP"
    assert index["trace_count"] == 1
    assert artifacts["traces"]["validation:synthetic-bug-001"] == str(trace_path)


def test_resolved_verifier_mapping_and_hidden_node_ids_absent(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    trace = _validation_trace(root, registry)

    final = trace["final_verification"]
    assert final["outcome"] == "RESOLVED"
    assert final["f2p"] == "1/1"
    assert final["p2p"] == "2/2"
    assert final["full_suite"] == "PASS"
    assert final["syntax_passed"] is True
    assert final["candidate_sha256"] == "1" * 64
    assert "f2p_records" not in final and "p2p_records" not in final
    # Hidden test node ids / function names must never appear.
    text = _stable_json(trace)
    assert "test_wrong" not in text
    assert "tests/test_" not in text


def test_breaking_resolved_preserved_and_kept_out_of_validation(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    curated = tmp_path / "curated"
    output = tmp_path / "out"

    export_professor_traces_r6(
        root, output, registry=registry, curated_root=curated,
        source_commit_sha="1111111111111111111111111111111111111111",
    )

    holdout_path = (
        output / "r6_holdout_partial"
        / "professor_debug_trace_synthetic-bug-001.json"
    )
    trace = json.loads(holdout_path.read_text(encoding="utf-8"))
    assert trace["evidence_scope"] == "final_holdout_partial"
    assert trace["final_verification"]["outcome"] == "BREAKING_RESOLVED"
    assert trace["final_verification"]["f2p"] == "1/1"
    assert trace["final_verification"]["p2p"] == "1/2"
    assert trace["final_verification"]["full_suite"] is False

    holdout_index = json.loads(
        (output / "r6_holdout_partial_index.json").read_text(encoding="utf-8")
    )
    assert holdout_index["evidence_scope"] == "final_holdout_partial"
    assert "breaking_resolved_row" in holdout_index
    assert holdout_index["final_holdout_status"] == "INCOMPLETE_HARDWARE_STOP"

    validation_index = json.loads(
        (output / "r6_validation_index.json").read_text(encoding="utf-8")
    )
    assert all(
        entry["evidence_scope"] == "validation"
        for entry in validation_index["traces"]
    )
    assert all(
        entry["verifier_outcome"] == "RESOLVED"
        for entry in validation_index["traces"]
    )


def test_localization_exact_line_semantics(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    trace = _validation_trace(root, registry)

    loc = trace["error_localization"]
    assert loc["production_file"] == "synthetic_mod.py"
    assert loc["function"] == "synthetic_fn"
    assert loc["line_or_region"] == 2  # exact production pause line
    assert loc["pause_generation"] == 1
    assert loc["evidence_basis"]


def test_localization_region_only_when_no_exact_line(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    resolver = _resolver(root)
    paths = verify_evidence(
        resolver, registry=registry, include_holdout=False,
    )
    evidence = json.loads(
        paths["validation:synthetic-bug-001"].read_text(encoding="utf-8")
    )
    # Remove all production-module pause observations with exact lines.
    lines = []
    for line in evidence["trajectory_jsonl"].splitlines():
        event = json.loads(line)
        if event.get("event_type") == "observation":
            payload = event["payload"]["observation"]["payload"]
            payload["script"] = "elsewhere.py"
        lines.append(json.dumps(event, ensure_ascii=False))
    evidence["trajectory_jsonl"] = "\n".join(lines)

    trace = build_trace_r6(
        evidence,
        scope="validation",
        model_identity={
            "fine_tuned_checkpoint": "checkpoint-30",
            "adapter_identity_sha256": ADAPTER_MODEL_SHA256,
            "training_provenance": "synthetic",
        },
    )
    loc = trace["error_localization"]
    assert loc["production_file"] is None
    assert loc["function"] is None
    assert loc["line_or_region"] is None
    assert loc["evidence_basis"] == []
    # The trace must stay schema-valid with NOT_RECORDED localization.
    validate_trace(trace)


def test_production_exception_path_represented(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    resolver = _resolver(root)
    paths = verify_evidence(
        resolver, registry=registry, include_holdout=False,
    )
    evidence = json.loads(
        paths["validation:synthetic-bug-001"].read_text(encoding="utf-8")
    )
    evidence["gate_results"]["gate_chain"]["production_exception_path"] = True
    evidence["gate_results"]["gate_chain"]["terminal_path"] = True
    evidence["gate_results"]["gate_chain"]["G2"] = None

    trace = build_trace_r6(
        evidence,
        scope="validation",
        model_identity={
            "fine_tuned_checkpoint": "checkpoint-30",
            "adapter_identity_sha256": ADAPTER_MODEL_SHA256,
            "training_provenance": "synthetic",
        },
    )
    assert trace["debugger_path"] == "production-exception"
    assert trace["debugger_lifecycle"]["production_exception_path"] is True
    assert trace["debugger_lifecycle"]["terminal_path"] is True
    assert trace["debugger_lifecycle"]["pause_generations"]["G2"] is None
    validate_trace(trace)


def test_frames_filtered_to_production_region(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    trace = _validation_trace(root, registry)

    for entry in trace["debugger_trace"]:
        frames = entry.get("frames")
        if frames is not None:
            for frame in frames:
                assert frame["file"] == "synthetic_mod.py"
                assert frame["line"] < 3  # runtime_appended_driver_start_line
    # The hidden test frame is absent from every entry.
    text = _stable_json(trace)
    assert "test_wrong" not in text


def test_missing_evidence_fails_closed(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    (root / "validation" / "synthetic-bug-001" / "evidence.json").unlink()

    with pytest.raises(RuntimeError, match="evidence missing"):
        verify_evidence(
            _resolver(root),
            registry=registry,
            include_holdout=False,
        )


def test_evidence_hash_mismatch_fails_closed(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    path = root / "validation" / "synthetic-bug-001" / "evidence.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="evidence identity mismatch"):
        verify_evidence(
            _resolver(root),
            registry=registry,
            include_holdout=False,
        )


def test_verifier_outcome_mismatch_fails_closed(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    path = root / "validation" / "synthetic-bug-001" / "evidence.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    evidence["verifier"]["outcome"] = "NOT_RESOLVED"
    text = json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False)
    # Exact bytes (evidence identity is byte-based).
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    # Recompute the registry hash for the modified evidence so the outcome
    # check (not the hash check) is exercised.
    registry.validation["synthetic-bug-001"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()

    with pytest.raises(RuntimeError, match="verifier outcome mismatch"):
        verify_evidence(
            _resolver(root),
            registry=registry,
            include_holdout=False,
        )


def test_missing_ancillary_fails_closed(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    (root / "ancillary" / "checkpoint_selection.json").unlink()

    with pytest.raises(RuntimeError, match="ancillary record missing"):
        verify_evidence(
            _resolver(root),
            registry=registry,
            include_holdout=False,
        )


def test_clean_trace_passes_leakage_audit(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    curated = tmp_path / "curated"
    resolver = _resolver(root)
    paths = verify_evidence(
        resolver, registry=registry, include_holdout=False,
    )
    evidence = json.loads(
        paths["validation:synthetic-bug-001"].read_text(encoding="utf-8")
    )
    trace = _validation_trace(root, registry)

    # The exporter audits with the accepted legitimate subtractions
    # (model-authored diagnosis + debugger-observed production function
    # names), exactly like the accepted prompt audit.
    audit = _audit_trace(trace, "synthetic-bug-001", evidence,
                         curated_root=curated)
    assert audit["passed"] is True
    assert audit["leakage_findings"] == []


def test_hidden_test_source_leak_fails_audit(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    curated = tmp_path / "curated"
    resolver = _resolver(root)
    paths = verify_evidence(
        resolver, registry=registry, include_holdout=False,
    )
    evidence = json.loads(
        paths["validation:synthetic-bug-001"].read_text(encoding="utf-8")
    )
    trace = _validation_trace(root, registry)
    # Inject a hidden-test assertion line into a non-subtracted export
    # field — this is exactly the leak form the audit must catch.  The
    # exporter's own audit path (with accepted legitimate subtractions)
    # must still fail closed on it.
    trace["failure_reproduction"]["sanitized_summary"] = (
        "assert synthetic_fn(1) == 2"
    )

    audit = _audit_trace(trace, "synthetic-bug-001", evidence,
                         curated_root=curated)
    assert audit["passed"] is False
    kinds = {f["kind"] for f in audit["leakage_findings"]}
    assert "assertion_source_expression" in kinds


def test_oracle_root_cause_leak_fails_audit(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    curated = tmp_path / "curated"
    trace = _validation_trace(root, registry)
    # Model diagnosis must not restate the hidden oracle root cause.
    trace["diagnosis"]["text"] = (
        "diagnosis The synthetic function returns a wrong value."
    )

    audit = audit_exported_text(
        _stable_json(trace), "synthetic-bug-001", curated_root=curated
    )
    assert audit["passed"] is False
    kinds = {f["kind"] for f in audit["leakage_findings"]}
    assert "oracle_root_cause_summary" in kinds


def test_deterministic_regeneration(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    curated = tmp_path / "curated"
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"

    export_professor_traces_r6(
        root, out_a, registry=registry, curated_root=curated,
        source_commit_sha="1111111111111111111111111111111111111111",
    )
    export_professor_traces_r6(
        root, out_b, registry=registry, curated_root=curated,
        source_commit_sha="1111111111111111111111111111111111111111",
    )

    files_a = sorted(p.relative_to(out_a) for p in out_a.rglob("*.json"))
    files_b = sorted(p.relative_to(out_b) for p in out_b.rglob("*.json"))
    assert [str(p) for p in files_a] == [str(p) for p in files_b]
    for rel in files_a:
        assert (out_a / rel).read_bytes() == (out_b / rel).read_bytes()


def test_index_trace_hashes_correspond_to_trace_files(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    curated = tmp_path / "curated"
    output = tmp_path / "out"
    export_professor_traces_r6(
        root, output, registry=registry, curated_root=curated,
        source_commit_sha="1111111111111111111111111111111111111111",
    )

    index = json.loads(
        (output / "r6_validation_index.json").read_text(encoding="utf-8")
    )
    for entry in index["traces"]:
        trace = json.loads(
            (output / entry["trace_path"]).read_text(encoding="utf-8")
        )
        content_hash = hashlib.sha256(
            _stable_json(trace).encode("utf-8")
        ).hexdigest()
        assert entry["trace_sha256"] == content_hash
        assert (output / entry["trace_path"]).is_file()

    sha_manifest = json.loads(
        (output / "trace_sha_manifest.json").read_text(encoding="utf-8")
    )
    assert len(sha_manifest["traces"]) == 2  # validation + holdout


def test_schema_validation_of_every_exported_trace(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    curated = tmp_path / "curated"
    output = tmp_path / "out"
    export_professor_traces_r6(
        root, output, registry=registry, curated_root=curated,
        source_commit_sha="1111111111111111111111111111111111111111",
    )
    for path in output.rglob("professor_debug_trace_*.json"):
        validate_trace(json.loads(path.read_text(encoding="utf-8")))


def test_validation_and_holdout_scopes_stay_distinct(tmp_path) -> None:
    root, registry = build_evidence_root(tmp_path)
    curated = tmp_path / "curated"
    output = tmp_path / "out"
    export_professor_traces_r6(
        root, output, registry=registry, curated_root=curated,
        source_commit_sha="1111111111111111111111111111111111111111",
    )

    v_index = json.loads(
        (output / "r6_validation_index.json").read_text(encoding="utf-8")
    )
    h_index = json.loads(
        (output / "r6_holdout_partial_index.json").read_text(encoding="utf-8")
    )
    assert v_index["evidence_scope"] == "validation"
    assert h_index["evidence_scope"] == "final_holdout_partial"
    assert v_index["final_holdout_status"] == "INCOMPLETE_HARDWARE_STOP"
    assert "distinct_scopes" in v_index
    assert "incomplete_tasks" in h_index


def test_build_index_validation_result_derived_not_hardcoded() -> None:
    traces = []
    for outcome in ("RESOLVED", "RESOLVED", "BREAKING_RESOLVED"):
        evidence_shape = {
            "schema_version": "professor_debug_trace_v1",
            "task_id": f"t-{outcome}",
            "debugger_path": "normal-G2",
            "error_localization": {"production_file": None, "function": None,
                                   "line_or_region": None, "evidence_basis": []},
            "final_verification": {"outcome": outcome},
            "debugger_trace": [],
            "repair_attempts": [],
        }
        traces.append(evidence_shape)
    index = build_index_r6(
        traces,
        {"t-RESOLVED": "x", "t-BREAKING_RESOLVED": "y"},
        scope="validation",
        source_commit_sha="1111111111111111111111111111111111111111",
    )
    assert index["validation_result"] == "2/3 RESOLVED"


# ---------------------------------------------------------------------------
# Checked-in deliverable integrity (docs/professor_traces)
# ---------------------------------------------------------------------------

DOCS_TRACES = REPO_ROOT / "docs" / "professor_traces"
CURATED = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"


def _docs_trace_paths() -> list[Path]:
    assert DOCS_TRACES.is_dir(), "docs/professor_traces must exist"
    paths = sorted(
        p for p in DOCS_TRACES.rglob("professor_debug_trace_*.json")
        if "index" not in p.name and "schema" not in p.name
    )
    assert paths, "no checked-in professor traces found"
    return paths


def test_deliverable_schema_is_synced_with_module_schema() -> None:
    module_schema = (
        REPO_ROOT / "agentic_debugger" / "evaluation"
        / "professor_debug_trace_schema_v1.json"
    )
    docs_schema = DOCS_TRACES / "professor_debug_trace_schema_v1.json"
    assert docs_schema.is_file()
    assert docs_schema.read_text(encoding="utf-8") == (
        module_schema.read_text(encoding="utf-8")
    )


def test_every_checked_in_trace_is_schema_valid_and_audit_clean() -> None:
    resolver = EvidenceResolver(CAPSULE_DIR)
    for path in _docs_trace_paths():
        trace = json.loads(path.read_text(encoding="utf-8"))
        validate_trace(trace)
        task_id = trace["task_id"]
        # The checked-in trace must carry no hidden-test/oracle content
        # beyond the legitimately observed debugger evidence (production
        # function names the debugger really paused in) and the
        # model-authored diagnosis — the same accepted subtractions the
        # exporter applies.
        production_file = trace["error_localization"]["production_file"]
        observed = {
            trace["error_localization"]["function"],
            trace["diagnosis"]["text"],
        }
        for entry in trace["debugger_trace"]:
            if entry.get("production_file") != production_file:
                continue
            if entry.get("function"):
                observed.add(entry["function"])
            for frame in entry.get("frames") or []:
                if frame.get("file") == production_file and frame.get("function"):
                    observed.add(frame["function"])
        legitimate = tuple(sorted(
            (t for t in observed if isinstance(t, str) and t),
            key=len,
            reverse=True,
        ))
        audit = audit_exported_text(
            _stable_json(trace),
            task_id,
            legitimate_texts=legitimate,
            curated_root=CURATED,
            resolver=resolver,
        )
        assert audit["passed"], (
            f"{path.name} fails the professor-safe audit: "
            f"{audit['leakage_findings']}"
        )


def test_checked_in_primary_index_matches_8_validation_traces() -> None:
    index = json.loads(
        (DOCS_TRACES / "r6_validation_index.json").read_text(encoding="utf-8")
    )
    assert index["validation_result"] == "8/8 RESOLVED"
    assert index["holdout_used_for_checkpoint_selection"] is False
    assert index["final_holdout_status"] == "INCOMPLETE_HARDWARE_STOP"
    assert index["trace_count"] == 8
    assert all(
        entry["verifier_outcome"] == "RESOLVED" for entry in index["traces"]
    )
    # Every indexed trace path exists under the deliverable root.
    for entry in index["traces"]:
        trace_path = entry["trace_path"]
        assert trace_path.startswith("r6_validation/")
        file_path = DOCS_TRACES / trace_path
        assert file_path.is_file()
        trace = json.loads(file_path.read_text(encoding="utf-8"))
        content_hash = hashlib.sha256(
            _stable_json(trace).encode("utf-8")
        ).hexdigest()
        assert entry["trace_sha256"] == content_hash


def test_checked_in_holdout_index_keeps_breaking_resolved_honest() -> None:
    index = json.loads(
        (DOCS_TRACES / "r6_holdout_partial_index.json").read_text(
            encoding="utf-8"
        )
    )
    assert index["evidence_scope"] == "final_holdout_partial"
    assert index["final_holdout_status"] == "INCOMPLETE_HARDWARE_STOP"
    outcomes = {
        entry["task_id"]: entry["verifier_outcome"] for entry in index["traces"]
    }
    assert outcomes["curated-none-handling-001"] == "RESOLVED"
    assert outcomes["curated-off-by-one-002"] == "BREAKING_RESOLVED"
    breaking = DOCS_TRACES / index["traces"][
        [e["task_id"] for e in index["traces"]].index("curated-off-by-one-002")
    ]["trace_path"]
    trace = json.loads(breaking.read_text(encoding="utf-8"))
    final = trace["final_verification"]
    assert final["f2p"] == "1/1"
    assert final["p2p"] == "1/2"
    assert final["full_suite"] is False
    assert final["outcome"] != "RESOLVED"


def test_checked_in_audit_report_passes() -> None:
    audit = json.loads(
        (DOCS_TRACES / "professor_safe_audit.json").read_text(encoding="utf-8")
    )
    assert audit["passed"] is True
    assert audit["total_findings"] == 0
    assert audit["scanned_documents"] == 10  # 8 validation + 2 holdout


# ---------------------------------------------------------------------------
# Tracked frozen evidence capsule (repair 1/2/3 regression coverage)
# ---------------------------------------------------------------------------

CAPSULE_DIR = (
    REPO_ROOT / "experiments" / "r6_debugger_training" / "runs" / "frozen"
)


def _require_capsule() -> None:
    if not (CAPSULE_DIR / "capsule_manifest.json").is_file():
        pytest.skip("tracked frozen evidence capsule not present")


def test_capsule_manifest_schema_and_identity() -> None:
    _require_capsule()
    manifest = json.loads(
        (CAPSULE_DIR / "capsule_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == "r6-frozen-evidence-capsule-v1"
    assert len(manifest["evidence"]) == 10  # 8 validation + 2 holdout
    for key, entry in manifest["evidence"].items():
        scope = key.split(":", 1)[0]
        assert entry["logical_identity"].startswith(
            "validation/" if scope == "validation" else "final_holdout_partial/"
        )
        assert entry["raw_sha256"] and entry["capsule_sha256"]
        # Chain of custody: every capsule file exists and matches.
        path = CAPSULE_DIR / entry["capsule_path"]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry[
            "capsule_sha256"
        ]
    for key in ("checkpoint_selection", "stage_a_report", "stage_b_report",
                "stage_c_report", "holdout_report"):
        assert key in manifest["ancillary"]
        path = CAPSULE_DIR / "ancillary" / f"{key}.json"
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[
            "ancillary"
        ][key]["sha256"]


def test_capsule_manifest_has_no_machine_paths() -> None:
    """The public chain manifest contains logical identities, never the
    builder machine's source locations."""
    _require_capsule()
    manifest = json.loads(
        (CAPSULE_DIR / "capsule_manifest.json").read_text(encoding="utf-8")
    )
    text = json.dumps(manifest)
    assert "C:\\" not in text
    assert "C:/tmp" not in text
    assert "wsl.localhost" not in text.lower()
    assert "/home/" not in text
    assert "source_identity" not in manifest
    for entry in manifest["evidence"].values():
        assert "capture_source" not in entry
    for entry in manifest["ancillary"].values():
        assert "source_path" not in entry


def test_capsule_protected_fields_absent() -> None:
    """The capsule must not carry model prompts or answer-bearing patch
    bodies (protected by the accepted clean-holdout policy)."""
    _require_capsule()
    for path in (CAPSULE_DIR / "validation").rglob("evidence.json"):
        capsule = json.loads(path.read_text(encoding="utf-8"))
        for record in capsule["telemetry"]:
            assert "request" not in record
            assert "arguments" not in (record.get("translated_directive") or {})
        for line in capsule["trajectory_jsonl"].splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("event_type") != "observation":
                continue
            inner = (
                (event.get("payload") or {}).get("observation") or {}
            ).get("payload") or {}
            assert "failure_output_raw" not in inner
            assert "node_id" not in inner


def test_capsule_raw_identity_matches_exporter_registry() -> None:
    """Every capsule record's raw evidence identity must equal the frozen
    exporter registry identity (chain of custody)."""
    _require_capsule()
    from agentic_debugger.evaluation.professor_trace_r6 import (
        FROZEN_REGISTRY,
    )

    manifest = json.loads(
        (CAPSULE_DIR / "capsule_manifest.json").read_text(encoding="utf-8")
    )
    for key, entry in manifest["evidence"].items():
        group, task_id = key.split(":", 1)
        expected = (
            FROZEN_REGISTRY.validation
            if group == "validation"
            else FROZEN_REGISTRY.final_holdout_partial
        )[task_id]
        assert entry["raw_sha256"] == expected


def test_frozen_needle_capsules_load_as_forbidden_content() -> None:
    """Frozen audit needles must reconstruct the same ForbiddenContent as
    the live derivation (audit authority parity)."""
    _require_capsule()
    from agentic_debugger.evaluation.professor_trace_r6 import (
        _frozen_forbidden_content,
        derive_forbidden_content_scoped,
    )

    curated = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"
    resolver = EvidenceResolver(CAPSULE_DIR)
    for task_id in ("quixbugs-depth-first-search", "quixbugs-kth"):
        frozen = _frozen_forbidden_content(task_id, resolver)
        assert frozen is not None
        fixture = curated / task_id
        if fixture.is_dir():
            live = derive_forbidden_content_scoped(task_id, fixture)
            assert frozen.needles() == live.needles()


def test_export_from_capsule_matches_checked_in_docs() -> None:
    """The capsule-root export must reproduce docs/professor_traces
    byte-identically (all generated files)."""
    _require_capsule()
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        output = Path(td) / "out"
        export_professor_traces_r6(
            CAPSULE_DIR,
            output,
            source_commit_sha="4610785713832daaba6aa133374506a2d200391a",
        )
        files_out = sorted(p.relative_to(output) for p in output.rglob("*.json"))
        files_docs = sorted(
            p.relative_to(DOCS_TRACES)
            for p in DOCS_TRACES.rglob("*.json")
            if p.name != "professor_debug_trace_schema_v1.json"
        )
        assert [str(p) for p in files_out] == [str(p) for p in files_docs]
        for rel in files_out:
            assert (output / rel).read_bytes() == (DOCS_TRACES / rel).read_bytes()


def test_professor_manifest_has_no_machine_paths() -> None:
    """The checked-in professor-facing manifest must use portable logical
    identities only (no machine-local capture paths)."""
    manifest = json.loads(
        (DOCS_TRACES / "source_evidence_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    text = json.dumps(manifest)
    assert "C:\\" not in text
    assert "C:/tmp" not in text
    assert "wsl.localhost" not in text.lower()
    assert "/home/" not in text
    entry = manifest["evidence"]["validation:quixbugs-depth-first-search"]
    assert entry["logical_identity"] == (
        "validation/stage-a/quixbugs-depth-first-search"
    )
    assert "source_path" not in entry


def test_traces_have_no_machine_paths() -> None:
    for path in DOCS_TRACES.rglob("professor_debug_trace_*.json"):
        text = path.read_text(encoding="utf-8")
        assert "C:\\" not in text
        assert "C:/tmp" not in text
        assert "wsl.localhost" not in text.lower()
        assert "/home/" not in text


def test_exporter_verify_evidence_accepts_capsule_root() -> None:
    _require_capsule()
    resolver = EvidenceResolver(CAPSULE_DIR)
    paths = verify_evidence(resolver, registry=None, include_holdout=True)
    assert len(paths) == 10


def test_exporter_verify_evidence_capsule_tamper_fails_closed(tmp_path) -> None:
    _require_capsule()
    import shutil

    copy_root = tmp_path / "frozen"
    shutil.copytree(CAPSULE_DIR, copy_root)
    target = copy_root / "validation" / "quixbugs-kth" / "evidence.json"
    target.write_text(target.read_text(encoding="utf-8") + "\n",
                      encoding="utf-8")
    with pytest.raises(RuntimeError, match="capsule sha256 mismatch"):
        verify_evidence(
            EvidenceResolver(copy_root, pkg_root=tmp_path / "_nopkg",
                             live_root=tmp_path / "_nolive"),
            registry=None,
            include_holdout=False,
        )


def test_pristine_tracked_only_checkout_regeneration(tmp_path) -> None:
    """ACCEPTANCE AUTHORITY: from a pristine checkout containing ONLY
    tracked files (no _ai-review, no C:/tmp/r6-bounded, no operator data,
    no ignored local fixtures), the default exporter command must reproduce
    the checked-in professor traces byte-identically.

    The test materializes the pristine tree via ``git ls-files``: it copies
    ONLY tracked files (plus the untracked deliverable files that this
    candidate adds, which will be tracked by the owner commit) into a
    scratch tree, and runs the exporter inside it.  The frozen evidence
    capsule is tracked, so regeneration must succeed fail-closed.
    """
    import shutil
    import subprocess

    pristine = tmp_path / "pristine"
    pristine.mkdir()

    # 1. All tracked files.
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True,
        check=True,
    )
    tracked = [line for line in listed.stdout.decode("utf-8").split("\0") if line]
    assert tracked, "git ls-files returned nothing"

    # 2. The candidate's new files (tracked by the owner commit).
    candidate_new = [
        "agentic_debugger/evaluation/professor_trace_r6.py",
        "tests/unit/test_professor_trace_r6.py",
        "scripts/build_r6_frozen_evidence_capsule.py",
        "docs/professor_traces/README.md",
        "docs/professor_traces/professor_debug_trace_schema_v1.json",
    ]
    candidate_new += [
        str(p.relative_to(REPO_ROOT)).replace("\\", "/")
        for p in REPO_ROOT.joinpath("docs/professor_traces").rglob("*.json")
    ]
    candidate_new += [
        str(p.relative_to(REPO_ROOT)).replace("\\", "/")
        for p in REPO_ROOT.joinpath(
            "experiments/r6_debugger_training/runs/frozen"
        ).rglob("*")
        if p.is_file()
    ]

    all_files = sorted(set(tracked) | set(candidate_new))
    for rel in all_files:
        src = REPO_ROOT / rel
        if not src.is_file():
            continue
        dst = pristine / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # 3. Sanity: the pristine tree contains NO review package, NO live
    #    run trees, NO operator data, NO ignored fixtures.
    assert not (pristine / "_ai-review").exists()
    assert not (pristine / "operator").exists()
    assert not (pristine / "agentic_debugger" / "datasets" / "curated"
                / "quixbugs-depth-first-search").exists()
    assert not (pristine / "experiments" / "debugger_interaction_v2_r5"
                / "runs" / "R5.9-MATRIX-14B-CLEAN-FINAL-2026-08-12").exists()

    # 4. Run the exporter inside the pristine tree with its default
    #    evidence-root resolution (must find the tracked capsule).
    out = tmp_path / "out"
    env = dict(__import__("os").environ)
    result = subprocess.run(
        [
            sys.executable, "-m",
            "agentic_debugger.evaluation.professor_trace_r6",
            "--output-dir", str(out),
        ],
        cwd=pristine,
        capture_output=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()

    # 5. Byte-identical comparison against the checked-in deliverable.
    for rel in sorted(p.relative_to(DOCS_TRACES) for p in DOCS_TRACES.rglob("*.json")):
        if rel.name == "professor_debug_trace_schema_v1.json":
            continue
        assert (out / rel).read_bytes() == (DOCS_TRACES / rel).read_bytes(), (
            f"regeneration mismatch: {rel}"
        )
