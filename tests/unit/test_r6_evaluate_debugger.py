from __future__ import annotations

import json
import sys
from pathlib import Path

from experiments.r6_debugger_training import evaluate_debugger as evaluator


def test_lifecycle_log_is_crash_durable_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.jsonl"
    recorder = evaluator.CrashDurableLifecycleLog(path)
    recorder("first", {"value": 1})
    recorder("second", {"value": 2})
    recorder.close()

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["sequence"] for record in records] == [0, 1]
    assert [record["event"] for record in records] == ["first", "second"]
    assert all(record["schema_version"] == evaluator.LIFECYCLE_SCHEMA for record in records)
    assert all(record["wall_time_local"] for record in records)
    assert all(record["wall_time_utc"] for record in records)


def test_child_python_identity_fails_closed_on_path_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    other_python = tmp_path / "python.exe"
    other_python.write_bytes(b"not the evaluator")
    monkeypatch.setattr(evaluator.shutil, "which", lambda _name: str(other_python))

    try:
        evaluator._require_child_python_matches_runtime()
    except RuntimeError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("mismatched child Python was accepted")


def test_curated_holdout_suite_is_fixed_and_rejects_validation_tasks() -> None:
    split = {"validation_tasks": [{"task_id": "quixbugs-depth-first-search"}]}
    entries = evaluator._select_task_entries(
        split, suite="curated-holdout", task_ids=None
    )
    assert [entry["task_id"] for entry in entries] == list(
        evaluator.CURATED_HOLDOUT_IDS
    )
    try:
        evaluator._select_task_entries(
            split,
            suite="curated-holdout",
            task_ids=["quixbugs-depth-first-search"],
        )
    except ValueError as exc:
        assert "curated-holdout" in str(exc)
    else:
        raise AssertionError("validation task was accepted into curated holdout")


def test_holdout_audit_fails_closed_when_selected_evidence_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        evaluator,
        "audit_matrix_dir",
        lambda _run_dir, _curated_root: {
            "scanned_prompt_count": 4,
            "leakage_findings_total": 0,
            "passed": True,
            "per_task": {"curated-none-handling-001": {"passed": True}},
        },
    )

    audit = evaluator._holdout_anti_leakage(
        tmp_path,
        suite="curated-holdout",
        selected_tasks=[
            "curated-none-handling-001",
            "curated-off-by-one-002",
        ],
    )

    assert audit["status"] == "incomplete"
    assert audit["passed"] is False
    assert audit["missing_evidence_tasks"] == ["curated-off-by-one-002"]


def test_clean_holdout_aggregate_requires_five_strict_rows_and_zero_leakage(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "task_id": task_id,
            "per_task_pass": True,
            "verifier_status": "COMPLETED",
            "verifier_outcome": "RESOLVED",
        }
        for task_id in evaluator.CURATED_HOLDOUT_IDS
    ]
    report = evaluator._build_report(
        status="complete",
        label="adapter-checkpoint-30",
        tag="final",
        stage="C",
        adapter_path="checkpoint-30",
        adapter_identity={},
        base_only=False,
        contract_sha="c" * 64,
        git_commit="g" * 40,
        selected_tasks=list(evaluator.CURATED_HOLDOUT_IDS),
        suite="curated-holdout",
        anti_leakage={
            "status": "complete",
            "passed": True,
            "leakage_findings_total": 0,
        },
        rows=rows,
        placement_audit={"passed": True},
        lifecycle_path=tmp_path / "lifecycle.jsonl",
        started_at="2026-08-12T00:00:00+00:00",
    )

    assert report["aggregate"]["primary_target_5_of_5"] is True
    assert report["aggregate"]["leakage_findings"] == 0
    assert report["aggregate"]["clean_holdout_5_of_5"] is True


def test_validation_status_fails_closed_without_full_debugger_verifier_success() -> None:
    selected_tasks = ["quixbugs-depth-first-search"]
    row = {
        "task_id": selected_tasks[0],
        "per_task_pass": False,
        "verifier_status": None,
        "verifier_outcome": None,
        "tool_errors": ["reproduction command could not be launched"],
    }

    assert evaluator._strict_scientific_pass([row], selected_tasks) is False
    assert evaluator._final_run_status(
        [row],
        selected_tasks,
        suite="validation",
        anti_leakage={"status": "not_applicable"},
    ) == "complete_target_not_met"


def test_validation_status_accepts_only_ordered_resolved_rows() -> None:
    selected_tasks = [
        "quixbugs-depth-first-search",
        "quixbugs-flatten",
    ]
    rows = [
        {
            "task_id": task_id,
            "per_task_pass": True,
            "verifier_status": "COMPLETED",
            "verifier_outcome": "RESOLVED",
        }
        for task_id in selected_tasks
    ]

    assert evaluator._strict_scientific_pass(rows, selected_tasks) is True
    assert evaluator._final_run_status(
        rows,
        selected_tasks,
        suite="validation",
        anti_leakage={"status": "not_applicable"},
    ) == "complete"
    assert evaluator._strict_scientific_pass(list(reversed(rows)), selected_tasks) is False


def test_same_transport_is_resident_across_stage_b_without_per_task_cuda_release(
    tmp_path: Path,
    monkeypatch,
) -> None:
    split_path = tmp_path / "split.json"
    split_path.write_text(
        json.dumps(
            {
                "validation_tasks": [
                    {"task_id": "quixbugs-depth-first-search"},
                    {"task_id": "quixbugs-flatten"},
                ]
            }
        ),
        encoding="utf-8",
    )
    adapter_path = tmp_path / "checkpoint-30"
    adapter_path.mkdir()
    (adapter_path / "adapter_config.json").write_text("{}", encoding="utf-8")
    (adapter_path / "adapter_model.safetensors").write_bytes(b"adapter")

    transports = []

    class FakeTransport:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.placement_audit = {
                "passed": True,
                "requested_device_map": {"": 0},
            }
            transports.append(self)

    calls = []

    def fake_run_experiment(
        contract,
        transport,
        output_dir,
        *,
        task_id,
        pdb_session_factory,
        lifecycle_event,
    ):
        calls.append(
            {
                "contract": contract,
                "transport": transport,
                "task_id": task_id,
                "lifecycle_event": lifecycle_event,
            }
        )
        lifecycle_event("fake_task_body", {"task_id": task_id})
        return {
            "telemetry": [
                {
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    },
                    "timing": {"request_duration_ms": 5},
                }
            ],
            "runtime": {"total_duration_ms": 20},
        }

    def fake_matrix_row(evidence, task_id, contract_sha, contract):
        return {
            "task_id": task_id,
            "verifier_status": "COMPLETED",
            "verifier_outcome": "RESOLVED",
            "per_task_pass": True,
            "first_causal_failure": "none",
            "model_calls": 1,
            "breakpoint_line": 1,
            "inspection_command": "get_frame_locals",
            "step_next_command": "next_pdb_session",
            "diagnosis_present": True,
            "B_sha": "b",
            "patch_applied": True,
        }

    monkeypatch.setattr(evaluator, "SPLIT_MANIFEST", split_path)
    monkeypatch.setattr(evaluator, "LocalQwenPeftTransport", FakeTransport)
    monkeypatch.setattr(evaluator, "run_experiment", fake_run_experiment)
    monkeypatch.setattr(evaluator, "_matrix_row", fake_matrix_row)
    monkeypatch.setattr(evaluator, "_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(
        evaluator,
        "_require_child_python_matches_runtime",
        lambda: sys.executable,
    )
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_debugger.py",
            "--adapter-path",
            str(adapter_path),
            "--output-dir",
            str(output),
            "--task",
            "quixbugs-depth-first-search",
            "--task",
            "quixbugs-flatten",
            "--stage",
            "B",
            "--tag",
            "stage-b-test",
        ],
    )

    assert evaluator.main() == 0
    assert len(transports) == 1
    assert [call["task_id"] for call in calls] == [
        "quixbugs-depth-first-search",
        "quixbugs-flatten",
    ]
    assert all(call["transport"] is transports[0] for call in calls)
    assert all(
        call["contract"]["experiment_id"]
        == "debugger-interaction-v2-r6-model-evaluation"
        for call in calls
    )
    assert all("model_role" not in call["contract"]["model"] for call in calls)

    report_path = (
        output
        / "stage-b-test"
        / "adapter-checkpoint-30"
        / "eval_report.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["run_status"] == "complete"
    assert report["suite"] == "validation"
    assert report["execution_policy"] == {
        "model_loads": 1,
        "model_residency": "one process; resident across every selected task",
        "model_release": "process exit only",
        "per_task_gc_collect": False,
        "per_task_torch_cuda_empty_cache": False,
        "implicit_device_map_auto": False,
        "implicit_cpu_or_disk_dispatch": False,
        "attention_implementation": "efficient_sdpa",
    }
    assert report["aggregate"]["verifier_resolved"] == 2
    assert report["aggregate"]["tokens_total"] == 24
    assert report["placement_audit"]["requested_device_map"] == {"": 0}

    lifecycle = [
        json.loads(line)
        for line in (report_path.parent / evaluator.LIFECYCLE_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    events = [record["event"] for record in lifecycle]
    assert events.count("model_load_start") == 1
    assert events.count("model_load_complete") == 1
    assert events.count("task_start") == 2
    assert events.count("task_complete") == 2
    assert "evaluator_complete" in events
