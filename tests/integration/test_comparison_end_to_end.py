"""End-to-end deterministic comparison demo over the curated tasks.

Runs the full provider-free pipeline over the two default curated tasks:

1. fixture index construction (source/test/issue/failure documents);
2. retrieval from task description plus captured failure excerpt;
3. path/line/chunk provenance;
4. imported base and tuned demo artifacts (correct + one non-repair
   preference fixture);
5. native agentic mode;
6. native RAG-agentic mode;
7. same-patch parity for the native conditions;
8. the normalized four-condition report with exactly two primary
   evaluation attempts per condition, auxiliary attempts excluded from
   aggregates/deltas, and no synthetic tuned superiority;
9. verifier-backed preference pairs and audit;
10. replay validity, cleanup, canonical immutability;
11. zero local provider and zero local network attempts; external
    generation telemetry separated.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from agentic_debugger.comparison.cli import main as comparison_main
from agentic_debugger.comparison.metrics import aggregate_all, delta_against_baseline
from agentic_debugger.comparison.runner import (
    BASELINE_CONDITION,
    CONDITIONS,
    DEMO_TASKS,
    run_comparison_demo,
)
from agentic_debugger.comparison.schema import ComparisonExperiment
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.preference.exporter import export_preferences_from_experiment

ROOT = Path(__file__).resolve().parents[2]


def _run_demo(tmp_path: Path, tasks=DEMO_TASKS):
    output = tmp_path / "demo-out"
    written = run_comparison_demo(
        repository_root=str(ROOT),
        tasks=tasks,
        output_root=str(output),
        experiment_id="integration-demo",
    )
    return output, written


def test_demo_end_to_end_over_two_curated_tasks(tmp_path: Path):
    output, written = _run_demo(tmp_path)

    # 1-2. index and retrieval artifacts exist with revision binding.
    for task_id in DEMO_TASKS:
        index_path = output / "index" / task_id / "repository-index-v1.json"
        assert index_path.is_file()
        index_doc = json.loads(index_path.read_text(encoding="utf-8"))
        assert index_doc["schema_version"] == "repository-index-v1"
        assert index_doc["task_id"] == task_id
        assert index_doc["revision"]
        assert index_doc["index_id"]
        kinds = {doc["kind"] for doc in index_doc["documents"]}
        assert kinds == {"source", "test", "issue", "failure"}
        assert len(index_doc["chunks"]) >= 1

        retrieval_path = output / "retrieval" / task_id / "retrieval-result-v1.json"
        assert retrieval_path.is_file()
        retrieval_doc = json.loads(retrieval_path.read_text(encoding="utf-8"))
        assert retrieval_doc["schema_version"] == "retrieval-result-v1"
        assert retrieval_doc["index_id"] == index_doc["index_id"]
        assert retrieval_doc["retrieval_id"]
        assert retrieval_doc["selected"]
        assert "max_results" in retrieval_doc
        for item in retrieval_doc["selected"]:
            assert item["path"]
            assert 1 <= item["start_line"] <= item["end_line"]
            assert item["chunk_id"]

    # 4. imported artifacts are clearly labeled synthetic and carry the
    # strict patch-extraction contract.
    for task_id in DEMO_TASKS:
        for attempt_id in (f"{task_id}:base", f"{task_id}:fine-tuned", f"{task_id}:base:wrong"):
            artifact_path = output / "attempts" / task_id / (
                f"{attempt_id.replace(':', '__')}.generation-artifact-v1.json"
            )
            assert artifact_path.is_file()
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            assert artifact["provenance"]["generator"] == "offline-deterministic-demo"
            assert artifact["schema_version"] == "generation-artifact-v1"
            if artifact["patch"] is not None:
                assert artifact["patch_extraction"]["mode"] == "substring"
                assert artifact["patch"] in artifact["raw_output"]

    # 5-8. normalized four-condition report.
    experiment_doc = json.loads((output / "experiment.json").read_text(encoding="utf-8"))
    assert experiment_doc["schema_version"] == "comparison-v1"
    assert experiment_doc["baseline_condition"] == BASELINE_CONDITION
    assert set(experiment_doc["conditions"]) == set(CONDITIONS)
    experiment = ComparisonExperiment.from_mapping(experiment_doc)
    attempts = {a.attempt_id: a for a in experiment.attempts}
    assert len(attempts) == 5 * len(DEMO_TASKS)

    # Exactly two primary evaluation attempts per condition (one per task).
    primary_counts = {}
    for condition in CONDITIONS:
        primary_counts[condition] = sum(
            1
            for a in experiment.attempts
            if a.condition_id == condition and a.role == "evaluation"
        )
    assert primary_counts == {
        "base": 2, "fine-tuned": 2, "rag-assisted": 2, "agentic": 2,
    }
    # The negative fixture exists as a labeled preference-fixture attempt.
    auxiliaries = [a for a in experiment.attempts if a.role == "preference-fixture"]
    assert len(auxiliaries) == len(DEMO_TASKS)
    for auxiliary in auxiliaries:
        assert auxiliary.attempt_id.endswith(":base:wrong")

    for task_id in DEMO_TASKS:
        # Imported correct base/tuned attempts resolve; the wrong one does not.
        assert attempts[f"{task_id}:base"].verifier_outcome == "RESOLVED"
        assert attempts[f"{task_id}:fine-tuned"].verifier_outcome == "RESOLVED"
        assert attempts[f"{task_id}:base:wrong"].verifier_outcome in (
            "NO_OP", "REGRESSION", "WORK_IN_PROGRESS", "PARTIALLY_RESOLVED",
        )
        assert attempts[f"{task_id}:base:wrong"].valid_patch is True  # applied, non-repair
        assert attempts[f"{task_id}:base:wrong"].role == "preference-fixture"
        # Native conditions resolve with identical patches (parity).
        agentic = attempts[f"agentic:{task_id}"]
        rag = attempts[f"rag-assisted:{task_id}"]
        assert agentic.verifier_outcome == "RESOLVED"
        assert agentic.patch_sha256 == rag.patch_sha256
        assert agentic.retrieval_count is None
        assert rag.retrieval_count >= 1
        assert rag.retrieval_bytes >= 1
        # Replay validity, cleanup, immutability, local offline counters.
        for native in (agentic, rag):
            assert native.replay_valid is True
            assert native.cleanup_status == "cleaned"
            assert native.canonical_fixture_unchanged is True
            assert native.provider_attempts == 0
            assert native.network_attempts == 0
        # External generation telemetry is separated (verified zero for
        # synthetic offline generations) and never fabricated for native.
        assert attempts[f"{task_id}:base"].external_provider_attempts == 0
        assert attempts[f"{task_id}:base"].external_network_attempts == 0
        assert agentic.external_provider_attempts is None

    # Primary aggregates exclude auxiliary attempts: no tuned-over-base gain.
    aggregates = aggregate_all(experiment)
    by_condition = {b["condition_id"]: b for b in aggregates["conditions"]}
    for condition in CONDITIONS:
        assert by_condition[condition]["attempts"] == 2
        assert by_condition[condition]["resolved"] == 2
        assert by_condition[condition]["resolved_rate"] == 1.0
    assert by_condition["base"]["auxiliary_attempts"] == 2
    delta = delta_against_baseline(experiment, aggregates)
    aggregate_deltas = {
        (e["condition_id"], e["metric"]): e["delta"]
        for e in delta["delta_entries"]
        if e["task_id"] is None
    }
    assert aggregate_deltas[("fine-tuned", "resolved")] == 0
    assert aggregate_deltas[("fine-tuned", "resolved_rate")] == 0.0

    # 11. CSV and Markdown exist, are consistent, and mark roles.
    csv_path = output / "comparison.csv"
    assert csv_path.is_file()
    rows = list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0][0] == "attempt_id"
    assert "role" in rows[0]
    assert len(rows) == 1 + 5 * len(DEMO_TASKS)
    md = (output / "comparison.md").read_text(encoding="utf-8")
    assert "This deterministic pilot is not statistically representative" in md
    assert "same-patch parity" in md or "identical repair behavior" in md
    assert "offline-deterministic-demo" in md
    assert "preference-fixture" in md

    # 9. verifier-backed preference pairs.
    oracles = {}
    for task_id in DEMO_TASKS:
        task = DebugTask.from_file(
            str(ROOT / "agentic_debugger" / "datasets" / "curated" / task_id / "task.json")
        )
        oracles[task_id] = task.oracle
    pairs, audit = export_preferences_from_experiment(
        experiment,
        task_oracles=oracles,
        source_comparison_identity="integration-demo:experiment.json",
    )
    assert len(pairs) >= len(DEMO_TASKS)
    pair_ids = [p.pair_id for p in pairs]
    assert len(pair_ids) == len(set(pair_ids))
    for pair in pairs:
        assert pair.verifier_evidence["chosen"] or pair.verifier_evidence["rejected"]
        assert pair.chosen.response != pair.rejected.response
        assert pair.task_id in DEMO_TASKS
        assert pair.chosen.response_sha256 is not None
    assert audit["pairs_produced"] == len(pairs)
    assert audit["rule_counts"]["rule-1"] >= len(DEMO_TASKS)

    # Local offline totals in the demo are zero across everything.
    assert written["offline_totals"] == {"provider_attempts": 0, "network_attempts": 0}


def test_demo_is_deterministic_across_runs(tmp_path: Path):
    single_task = ("curated-off-by-one-002",)
    first_out, _first = _run_demo(tmp_path / "a", tasks=single_task)
    second_out, _second = _run_demo(tmp_path / "b", tasks=single_task)

    def deterministic_view(path: Path):
        doc = json.loads(path.read_text(encoding="utf-8"))
        stripped = {k: v for k, v in doc.items() if k not in ("environment", "timing")}
        attempts = []
        for attempt in stripped["attempts"]:
            attempt = dict(attempt)
            attempt.pop("runtime_ms", None)
            attempt.pop("retrieval_latency_ms", None)
            attempts.append(attempt)
        stripped["attempts"] = attempts
        return stripped

    first = deterministic_view(first_out / "experiment.json")
    second = deterministic_view(second_out / "experiment.json")
    assert first == second

    def csv_without_timing(path: Path) -> str:
        rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
        header = rows[0]
        timing_columns = {header.index(c) for c in ("runtime_ms", "retrieval_latency_ms")}
        return "\n".join(
            ",".join(cell for i, cell in enumerate(row) if i not in timing_columns)
            for row in rows
        )

    assert csv_without_timing(first_out / "comparison.csv") == csv_without_timing(
        second_out / "comparison.csv"
    )


def test_cli_build_index_retrieve_import_export(tmp_path: Path):
    """The six CLI subcommands work with explicit unique output roots."""
    repo = str(ROOT)
    task_id = "curated-off-by-one-002"
    fixture = str(ROOT / "agentic_debugger" / "datasets" / "curated" / task_id)

    idx_root = tmp_path / "idx"
    assert comparison_main([
        "build-index",
        "--repo-root", repo,
        "--output-root", str(idx_root),
        "--corpus-root", fixture,
        "--mode", "fixture",
        "--task-id", task_id,
    ]) == 0
    index_path = idx_root / "repository-index-v1.json"
    assert index_path.is_file()

    ret_root = tmp_path / "ret"
    assert comparison_main([
        "retrieve",
        "--output-root", str(ret_root),
        "--index", str(index_path),
        "--query", "recent_window sequence length",
    ]) == 0
    assert (ret_root / "retrieval-result-v1.json").is_file()

    # Import one correct demo artifact through the strict path.
    patch = _reference_patch_text(task_id)
    raw_output = f"Synthetic cli output.\n{patch}"
    start = raw_output.encode("utf-8").find(patch.encode("utf-8"))
    artifact_path = tmp_path / "import-artifact.json"
    artifact_path.write_text(json.dumps({
        "schema_version": "generation-artifact-v1",
        "experiment_id": "cli-test",
        "attempt_id": f"{task_id}:cli-base",
        "condition_id": "base",
        "task_id": task_id,
        "model_repository": "offline-deterministic-demo",
        "model_revision": "rev",
        "adapter_identity": None,
        "prompt_contract": "cli-test",
        "generation_config": {"synthetic": True},
        "raw_output": raw_output,
        "patch_extraction": {"mode": "substring", "start": start,
                             "end": start + len(patch.encode("utf-8"))},
        "patch": patch,
        "runtime_ms": None,
        "memory_bytes": None,
        "cost": None,
        "tokens": None,
        "external_provider_attempts": 0,
        "external_network_attempts": 0,
        "provenance": {"generator": "offline-deterministic-demo", "note": "cli test"},
    }), encoding="utf-8")
    imp_root = tmp_path / "imp"
    assert comparison_main([
        "import-attempt",
        "--repo-root", repo,
        "--output-root", str(imp_root),
        "--artifact", str(artifact_path),
        "--task-manifest", str(ROOT / "agentic_debugger" / "datasets" / "curated" / task_id / "task.json"),
    ]) == 0
    attempt_path = imp_root / f"{task_id}:cli-base.attempt-v1.json".replace(":", "__")
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    assert attempt["valid_patch"] is True
    assert attempt["verifier_outcome"] == "RESOLVED"
    assert attempt["role"] == "evaluation"

    # Export preferences from the full demo run.
    demo_out, _written = _run_demo(tmp_path / "demo", tasks=("curated-off-by-one-002",))
    pref_root = tmp_path / "pref"
    assert comparison_main([
        "export-preferences",
        "--output-root", str(pref_root),
        "--results", str(demo_out / "experiment.json"),
        "--task-manifest",
        str(ROOT / "agentic_debugger" / "datasets" / "curated" / "curated-off-by-one-002" / "task.json"),
    ]) == 0
    lines = (pref_root / "preferences.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines
    pair = json.loads(lines[0])
    assert pair["schema_version"] == "preference-pair-v1"
    assert (pref_root / "preference-audit.json").is_file()


def test_output_root_must_be_unique(tmp_path: Path):
    output = tmp_path / "claimed"
    output.mkdir()
    (output / "existing.txt").write_text("x", encoding="utf-8")
    with pytest.raises(Exception):
        run_comparison_demo(
            repository_root=str(ROOT),
            tasks=("curated-off-by-one-002",),
            output_root=str(output),
            experiment_id="dup-test",
        )


def _reference_patch_text(task_id: str) -> str:
    from agentic_debugger.demo.catalog import build_reference_patch, scenario_for

    scenario = scenario_for(task_id)
    fixture = ROOT / "agentic_debugger" / "datasets" / "curated" / task_id
    source = (fixture / scenario.reference_repair.target_path).read_text(encoding="utf-8")
    return build_reference_patch(source, scenario.reference_repair)
