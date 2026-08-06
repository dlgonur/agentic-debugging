"""Deterministic end-to-end comparison orchestration (provider-free).

:func:`run_comparison_demo` executes, for each curated task:

1. baseline failure-output capture (disposable workspace);
2. fixture-scoped index construction bound to the repository revision;
3. deterministic retrieval (task/issue projection + failure excerpt);
4. imported base and fine-tuned demo artifacts plus one deterministic
   non-repair artifact — all run through the real strict parser, disposable
   workspace and :class:`EvaluationVerifier`;
5. native agentic and native RAG-agentic conditions through the accepted
   demo runner path, with a fail-closed same-patch parity check;
6. the normalized four-condition experiment document (JSON/CSV/Markdown).

Every artifact is written under an explicit, uniquely claimed output root.
Nothing here contacts a provider or the network.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agentic_debugger.comparison.import_schema import run_imported_attempt
from agentic_debugger.comparison.metrics import aggregate_all, delta_against_baseline
from agentic_debugger.comparison.native import (
    build_comment_append_patch,
    build_task_query,
    capture_failure_output,
    check_native_parity,
    reference_patch_for,
    run_native_attempt,
    synthetic_demo_artifact,
)
from agentic_debugger.comparison.report import (
    environment_record,
    experiment_csv,
    experiment_document,
    git_head,
    render_markdown,
    results_json,
)
from agentic_debugger.comparison.schema import (
    AttemptRecord,
    ComparisonExperiment,
    ComparisonInputError,
    canonical_json,
)
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.rag.context import RagContext
from agentic_debugger.rag.corpus import build_corpus
from agentic_debugger.rag.indexer import build_index, index_to_text
from agentic_debugger.rag.retrieval import retrieve
from agentic_debugger.rag.schema import RetrievalResult

#: Default demo tasks (two existing curated tasks).
DEMO_TASKS: Tuple[str, ...] = ("curated-off-by-one-002", "curated-none-handling-001")

#: Condition identities of the four-condition comparison.
CONDITIONS: Tuple[str, ...] = ("base", "fine-tuned", "rag-assisted", "agentic")

#: Baseline condition for the demo experiment.
BASELINE_CONDITION = "base"


class ComparisonRunError(ComparisonInputError):
    """Raised when a comparison run fails."""


def claim_output_root(output_root: str) -> Path:
    """Claim an explicit, unique output root (create-once semantics)."""

    if type(output_root) is not str or not output_root:
        raise ComparisonRunError("output_root must be a non-empty string")
    root = Path(output_root)
    if root.exists() and any(root.iterdir()):
        raise ComparisonRunError(
            f"output root exists and is not empty; refusing to overwrite: {output_root}"
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_canonical(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8", newline="\n")


def run_comparison_demo(
    *,
    repository_root: str,
    tasks: Sequence[str],
    output_root: str,
    experiment_id: str,
    workspace_parent: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the deterministic four-condition comparison for the given tasks.

    Returns a mapping of written artifact paths plus measured timing and the
    offline ledger totals.
    """

    repo = Path(repository_root).resolve()
    if not repo.is_dir():
        raise ComparisonRunError(f"repository_root is not a directory: {repository_root!r}")
    selected = tuple(tasks)
    if not selected:
        raise ComparisonRunError("at least one task is required")
    unknown = [t for t in selected if not (
        Path(repo) / "agentic_debugger" / "datasets" / "curated" / t / "task.json"
    ).is_file()]
    if unknown:
        raise ComparisonRunError(f"unknown curated task ids: {sorted(unknown)}")

    root = claim_output_root(output_root)
    owns_parent = workspace_parent is None
    parent = (
        Path(tempfile.mkdtemp(prefix="comparison-workspaces-"))
        if owns_parent
        else Path(workspace_parent)
    )
    if not parent.is_dir():
        if owns_parent:
            shutil.rmtree(parent, ignore_errors=True)
        raise ComparisonRunError(
            f"workspace_parent is not a directory: {workspace_parent!r}"
        )

    revision = git_head(str(repo))
    environment = environment_record(str(repo))
    timing: Dict[str, Any] = {}
    attempts: List[AttemptRecord] = []
    written: Dict[str, Any] = {"output_root": str(root)}
    offline_totals = {"provider_attempts": 0, "network_attempts": 0}

    try:
        for task_id in selected:
            fixture_dir = repo / "agentic_debugger" / "datasets" / "curated" / task_id
            task = DebugTask.from_file(str(fixture_dir / "task.json"))

            # 1. baseline failure output (disposable workspace).
            t0 = time.monotonic()
            failure_text = capture_failure_output(
                task, fixture_dir=str(fixture_dir), workspace_parent=str(parent)
            )
            timing.setdefault(task_id, {})["failure_capture_ms"] = int(
                (time.monotonic() - t0) * 1000
            )

            # 2. fixture-scoped index, revision-bound.
            corpus = build_corpus(
                str(fixture_dir),
                mode="fixture",
                task_id=task_id,
                failure_text=failure_text,
            )
            index = build_index(
                corpus,
                revision=revision,
                repository_identity=f"agentic-debugging-internship@{revision}",
                task_id=task_id,
            )
            index_path = root / "index" / task_id / "repository-index-v1.json"
            _write_canonical(index_path, json.loads(index_to_text(index)))
            written[f"index:{task_id}"] = str(index_path)

            # 3. deterministic retrieval.
            query = build_task_query(task, failure_text)
            t0 = time.monotonic()
            result = retrieve(index, query, expected_revision=revision)
            latency_ms = int((time.monotonic() - t0) * 1000)
            retrieval_result = RetrievalResult(
                schema_version=result.schema_version,
                retrieval_id=result.retrieval_id,
                index_id=result.index_id,
                query_identity=result.query_identity,
                query=result.query,
                selected=result.selected,
                result_count_truncated=result.result_count_truncated,
                context_bytes_truncated=result.context_bytes_truncated,
                selected_bytes=result.selected_bytes,
                max_results=result.max_results,
                max_context_bytes=result.max_context_bytes,
                latency_ms=latency_ms,
            )
            retrieval_path = root / "retrieval" / task_id / "retrieval-result-v1.json"
            _write_canonical(retrieval_path, json.loads(retrieval_result.to_text()))
            written[f"retrieval:{task_id}"] = str(retrieval_path)
            rag_context = RagContext.from_retrieval(
                retrieval_result, retrieval_latency_ms=latency_ms
            )
            timing.setdefault(task_id, {})["retrieval_latency_ms"] = latency_ms

            # 4. imported artifacts (synthetic, clearly labeled).
            patch_text = reference_patch_for(str(repo), task_id)
            wrong_patch = build_comment_append_patch(
                (fixture_dir / _scenario_target(task_id)).read_text(encoding="utf-8"),
                _scenario_target(task_id),
            )
            artifacts = [
                synthetic_demo_artifact(
                    experiment_id=experiment_id,
                    attempt_id=f"{task_id}:base",
                    condition_id="base",
                    task_id=task_id,
                    model_revision=revision,
                    adapter_identity=None,
                    patch=patch_text,
                    generation_note=(
                        "verified correct repair rendered from the canonical "
                        "fixture bytes; offline-deterministic-demo identity only"
                    ),
                ),
                synthetic_demo_artifact(
                    experiment_id=experiment_id,
                    attempt_id=f"{task_id}:fine-tuned",
                    condition_id="fine-tuned",
                    task_id=task_id,
                    model_revision=revision,
                    adapter_identity="demo-lora-adapter",
                    patch=patch_text,
                    generation_note=(
                        "synthetic tuned stand-in; not a real QLoRA adapter or "
                        "fine-tuned model"
                    ),
                ),
                synthetic_demo_artifact(
                    experiment_id=experiment_id,
                    attempt_id=f"{task_id}:base:wrong",
                    condition_id="base",
                    task_id=task_id,
                    model_revision=revision,
                    adapter_identity=None,
                    patch=wrong_patch,
                    generation_note=(
                        "deterministic non-repair preference fixture; role "
                        "preference-fixture (excluded from primary aggregates); "
                        "verdict produced by the real parser, workspace and "
                        "verifier"
                    ),
                ),
            ]
            attempt_dir = root / "attempts" / task_id
            for artifact in artifacts:
                # Attempt ids may contain ":" (protocol identity); Windows
                # filenames may not, so the artifact filename is sanitized
                # while the attempt identity inside the file is unchanged.
                safe_name = artifact.attempt_id.replace(":", "__")
                path = attempt_dir / f"{safe_name}.generation-artifact-v1.json"
                _write_canonical(path, json.loads(artifact.to_text()))
                written[f"artifact:{artifact.attempt_id}"] = str(path)

            t0 = time.monotonic()
            base_attempt = run_imported_attempt(
                artifacts[0], task=task, repository_root=str(repo),
                workspace_parent=str(parent), role="evaluation",
            )
            tuned_attempt = run_imported_attempt(
                artifacts[1], task=task, repository_root=str(repo),
                workspace_parent=str(parent), role="evaluation",
            )
            wrong_attempt = run_imported_attempt(
                artifacts[2], task=task, repository_root=str(repo),
                workspace_parent=str(parent), role="preference-fixture",
            )
            timing.setdefault(task_id, {})["imported_verify_ms"] = int(
                (time.monotonic() - t0) * 1000
            )

            # 5. native conditions through the accepted demo runner path.
            agentic_attempt = run_native_attempt(
                repository_root=str(repo),
                task_id=task_id,
                condition_id="agentic",
                workspace_parent=str(parent),
                response_text=patch_text,
            )
            rag_attempt = run_native_attempt(
                repository_root=str(repo),
                task_id=task_id,
                condition_id="rag-assisted",
                workspace_parent=str(parent),
                response_text=patch_text,
                rag_context=rag_context,
            )
            check_native_parity(agentic_attempt, rag_attempt)

            for attempt in (base_attempt, tuned_attempt, wrong_attempt,
                            agentic_attempt, rag_attempt):
                attempts.append(AttemptRecord.from_mapping(attempt))
                offline_totals["provider_attempts"] += int(
                    attempt.get("provider_attempts") or 0
                )
                offline_totals["network_attempts"] += int(
                    attempt.get("network_attempts") or 0
                )
    finally:
        if owns_parent:
            shutil.rmtree(parent, ignore_errors=True)

    experiment = ComparisonExperiment(
        schema_version="comparison-v1",
        experiment_id=experiment_id,
        baseline_condition=BASELINE_CONDITION,
        conditions=CONDITIONS,
        task_ids=tuple(selected),
        attempts=tuple(attempts),
    )
    aggregates = aggregate_all(experiment)
    delta = delta_against_baseline(experiment, aggregates)
    environment["generated_utc"] = (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    timing["offline_totals"] = dict(offline_totals)
    document = experiment_document(
        experiment,
        aggregates=aggregates,
        delta=delta,
        environment=environment,
        timing=timing,
    )
    results_path = root / "experiment.json"
    results_path.write_text(results_json(document), encoding="utf-8", newline="\n")
    written["experiment"] = str(results_path)
    csv_path = root / "comparison.csv"
    csv_path.write_text(experiment_csv(experiment), encoding="utf-8", newline="\n")
    written["csv"] = str(csv_path)
    md_path = root / "comparison.md"
    md_path.write_text(render_markdown(document), encoding="utf-8", newline="\n")
    written["markdown"] = str(md_path)
    written["offline_totals"] = dict(offline_totals)
    written["timing"] = timing
    return written


def _scenario_target(task_id: str) -> str:
    from agentic_debugger.demo.catalog import scenario_for

    return scenario_for(task_id).reference_repair.target_path


__all__ = [
    "DEMO_TASKS",
    "CONDITIONS",
    "BASELINE_CONDITION",
    "ComparisonRunError",
    "claim_output_root",
    "run_comparison_demo",
]
