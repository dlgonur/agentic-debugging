"""Command-line interface for the unified comparison harness.

Subcommands:

* ``build-index`` — build a deterministic ``repository-index-v1`` artifact;
* ``retrieve`` — deterministic retrieval over an index artifact;
* ``import-attempt`` — verify one ``generation-artifact-v1`` through the
  strict parser and the independent verifier;
* ``compare`` — run the deterministic four-condition comparison;
* ``export-preferences`` — build preference pairs from an experiment;
* ``demo`` — the full provider-free end-to-end demonstration (compare plus
  preference export plus summary).

Every command requires an explicit, uniquely claimed ``--output-root`` and
never writes into tracked source directories.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from agentic_debugger.comparison.runner import (
    DEMO_TASKS,
    claim_output_root,
    run_comparison_demo,
)
from agentic_debugger.rag.corpus import build_corpus
from agentic_debugger.rag.indexer import build_index, index_to_text, load_index_file
from agentic_debugger.rag.retrieval import retrieve
from agentic_debugger.rag.schema import canonical_json

DEFAULT_EXPERIMENT_ID = "rag-comparison-demo"


def _repository_root(value: Optional[str]) -> str:
    if value:
        root = Path(value).resolve()
        if not root.is_dir():
            raise SystemExit(f"error: repository root is not a directory: {value}")
        return str(root)
    return str(Path(__file__).resolve().parents[2])


def _git_head(repository_root: str) -> str:
    from agentic_debugger.comparison.report import git_head

    try:
        return git_head(repository_root)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc


def _build_index(args: object) -> int:
    from agentic_debugger.comparison.report import git_head

    root = claim_output_root(args.output_root)
    revision = args.revision
    if not revision:
        try:
            revision = git_head(args.repo_root)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from exc
    failure_text = None
    if args.failure_text_file:
        failure_text = Path(args.failure_text_file).read_text(encoding="utf-8")
    corpus = build_corpus(
        args.corpus_root,
        mode=args.mode,
        task_id=args.task_id,
        failure_text=failure_text,
        include_docs=args.include_docs,
    )
    index = build_index(
        corpus,
        revision=revision,
        repository_identity=args.repository_identity or "declared-corpus-root",
        task_id=args.task_id,
    )
    path = root / "repository-index-v1.json"
    path.write_text(index_to_text(index), encoding="utf-8", newline="\n")
    print(f"index: {path}")
    print(f"index_id: {index.index_id}")
    print(f"chunks: {len(index.chunks)}")
    print(f"documents: {len(index.documents)}")
    print(f"bytes: {len(index_to_text(index).encode('utf-8'))}")
    return 0


def _retrieve(args: object) -> int:
    root = claim_output_root(args.output_root)
    query = args.query
    if args.query_file:
        query = Path(args.query_file).read_text(encoding="utf-8")
    if not query:
        raise SystemExit("error: a query is required (--query or --query-file)")
    index = load_index_file(args.index, expected_revision=args.expected_revision)
    result = retrieve(
        index,
        query,
        expected_revision=args.expected_revision,
        max_results=args.max_results,
        max_context_bytes=args.max_context_bytes,
    )
    path = root / "retrieval-result-v1.json"
    path.write_text(result.to_text(), encoding="utf-8", newline="\n")
    print(f"retrieval: {path}")
    print(f"retrieval_id: {result.retrieval_id}")
    print(f"selected: {len(result.selected)} chunks, {result.selected_bytes} bytes")
    return 0


def _import_attempt(args: object) -> int:
    from agentic_debugger.comparison.import_schema import GenerationArtifact
    from agentic_debugger.evaluation.task_schema import DebugTask

    root = claim_output_root(args.output_root)
    artifact = GenerationArtifact.from_file(args.artifact)
    task = DebugTask.from_file(args.task_manifest)
    owns_parent = args.workspace_parent is None
    parent = Path(tempfile.mkdtemp(prefix="import-attempt-workspaces-")) if owns_parent else Path(args.workspace_parent)
    try:
        from agentic_debugger.comparison.import_schema import run_imported_attempt

        attempt = run_imported_attempt(
            artifact,
            task=task,
            repository_root=args.repo_root,
            workspace_parent=str(parent),
        )
    finally:
        if owns_parent:
            shutil.rmtree(parent, ignore_errors=True)
    path = root / f"{artifact.attempt_id.replace(':', '__')}.attempt-v1.json"
    path.write_text(canonical_json(attempt) + "\n", encoding="utf-8", newline="\n")
    print(f"attempt: {path}")
    print(f"valid_patch: {attempt['valid_patch']}")
    print(f"verifier_outcome: {attempt['verifier_outcome']}")
    print(f"failure_category: {attempt['failure_category']}")
    return 0


def _compare(args: object) -> int:
    tasks = tuple(args.tasks) if args.tasks else DEMO_TASKS
    written = run_comparison_demo(
        repository_root=args.repo_root,
        tasks=tasks,
        output_root=args.output_root,
        experiment_id=args.experiment_id,
        workspace_parent=args.workspace_parent,
    )
    for key, path in sorted(written.items()):
        if key in {"output_root", "offline_totals", "timing"}:
            continue
        print(f"{key}: {path}")
    print(f"offline totals: {written['offline_totals']}")
    return 0


def _export_preferences(args: object) -> int:
    from agentic_debugger.evaluation.task_schema import DebugTask
    from agentic_debugger.preference.exporter import export_preferences_from_experiment
    from agentic_debugger.comparison.schema import ComparisonExperiment

    root = claim_output_root(args.output_root)
    experiment = ComparisonExperiment.from_file(args.results)
    oracles = {}
    for manifest in args.task_manifest:
        task = DebugTask.from_file(manifest)
        if task.task_id in oracles:
            raise SystemExit(f"error: duplicate task manifest for {task.task_id}")
        oracles[task.task_id] = task.oracle
    pairs, audit = export_preferences_from_experiment(
        experiment,
        task_oracles=oracles,
        held_out_task_ids=tuple(args.held_out) if args.held_out else (),
        source_comparison_identity=(
            f"{experiment.experiment_id}:{Path(args.results).name}"
        ),
    )
    pairs_path = root / "preferences.jsonl"
    with open(pairs_path, "w", encoding="utf-8", newline="\n") as handle:
        for pair in pairs:
            handle.write(canonical_json(pair.to_mapping()) + "\n")
    audit_path = root / "preference-audit.json"
    audit_path.write_text(canonical_json(audit) + "\n", encoding="utf-8", newline="\n")
    print(f"preferences: {pairs_path}")
    print(f"audit: {audit_path}")
    print(f"pairs: {len(pairs)}")
    return 0


def _demo(args: object) -> int:
    tasks = tuple(args.tasks) if args.tasks else DEMO_TASKS
    written = run_comparison_demo(
        repository_root=args.repo_root,
        tasks=tasks,
        output_root=args.output_root,
        experiment_id=args.experiment_id,
        workspace_parent=args.workspace_parent,
    )
    root = Path(written["output_root"])
    results_path = Path(written["experiment"])
    experiment_doc = json.loads(results_path.read_text(encoding="utf-8"))

    # Preference export from the real verifier-backed attempts.
    from agentic_debugger.evaluation.task_schema import DebugTask
    from agentic_debugger.preference.exporter import export_preferences_from_experiment
    from agentic_debugger.comparison.schema import ComparisonExperiment

    experiment = ComparisonExperiment.from_mapping(experiment_doc)
    oracles = {}
    repo = Path(args.repo_root).resolve()
    for task_id in tasks:
        task = DebugTask.from_file(
            str(repo / "agentic_debugger" / "datasets" / "curated" / task_id / "task.json")
        )
        oracles[task_id] = task.oracle
    pairs, audit = export_preferences_from_experiment(
        experiment,
        task_oracles=oracles,
        held_out_task_ids=tuple(args.held_out) if args.held_out else (),
        source_comparison_identity=f"{args.experiment_id}:experiment.json",
    )
    pairs_path = root / "preferences.jsonl"
    with open(pairs_path, "w", encoding="utf-8", newline="\n") as handle:
        for pair in pairs:
            handle.write(canonical_json(pair.to_mapping()) + "\n")
    audit_path = root / "preference-audit.json"
    audit_path.write_text(canonical_json(audit) + "\n", encoding="utf-8", newline="\n")

    summary_lines = [
        f"# Deterministic RAG-comparison demonstration ({args.experiment_id})",
        "",
        "> This deterministic pilot is not statistically representative.",
        "",
        f"* tasks: {', '.join(tasks)}",
        f"* conditions: {', '.join(experiment.conditions)}",
        f"* baseline condition: `{experiment.baseline_condition}`",
        f"* attempts: {len(experiment.attempts)}",
        f"* preference pairs: {len(pairs)}",
        f"* provider attempts: {written['offline_totals']['provider_attempts']}",
        f"* network attempts: {written['offline_totals']['network_attempts']}",
        "",
        "## Artifacts",
        "",
    ]
    for key, path in sorted(written.items()):
        if key in {"output_root", "offline_totals", "timing"}:
            continue
        summary_lines.append(f"* `{key}`: `{path}`")
    summary_lines.append(f"* `preferences`: `{pairs_path}`")
    summary_lines.append(f"* `preference-audit`: `{audit_path}`")
    summary_lines.append("")
    summary_lines.append("## Offline evidence")
    summary_lines.append("")
    summary_lines.append(
        f"* provider attempts: {written['offline_totals']['provider_attempts']}"
    )
    summary_lines.append(
        f"* network attempts: {written['offline_totals']['network_attempts']}"
    )
    summary_lines.append("")
    summary_lines.append(
        "Imported base/tuned identities are `offline-deterministic-demo`; they "
        "do not imply actual QLoRA evaluation."
    )
    summary_path = root / "demo-summary.md"
    summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8", newline="\n")
    print(f"demo: {root}")
    print(f"offline totals: {written['offline_totals']}")
    print(f"preference pairs: {len(pairs)}")
    return 0


def build_parser() -> object:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m agentic_debugger.comparison",
        description=(
            "Deterministic offline RAG/comparison/preference harness over the "
            "accepted debugging infrastructure. Never contacts a provider."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: object) -> None:
        subparser.add_argument(
            "--repo-root",
            default=None,
            help="Repository root. Defaults to the checkout containing this package.",
        )
        subparser.add_argument(
            "--output-root",
            required=True,
            help="Explicit, uniquely claimed output root (must be empty or absent).",
        )

    p = sub.add_parser("build-index", help="Build a repository-index-v1 artifact.")
    add_common(p)
    p.add_argument("--corpus-root", required=True)
    p.add_argument("--mode", choices=("fixture", "repo"), default="fixture")
    p.add_argument("--task-id", default=None)
    p.add_argument("--revision", default=None)
    p.add_argument("--repository-identity", default=None)
    p.add_argument("--failure-text-file", default=None)
    p.add_argument("--include-docs", action="store_true")
    p.set_defaults(handler=_build_index)

    p = sub.add_parser("retrieve", help="Deterministic retrieval over an index.")
    add_common(p)
    p.add_argument("--index", required=True)
    p.add_argument("--query", default=None)
    p.add_argument("--query-file", default=None)
    p.add_argument("--expected-revision", default=None)
    p.add_argument("--max-results", type=int, default=8)
    p.add_argument("--max-context-bytes", type=int, default=4096)
    p.set_defaults(handler=_retrieve)

    p = sub.add_parser("import-attempt", help="Verify one imported generation artifact.")
    add_common(p)
    p.add_argument("--artifact", required=True)
    p.add_argument("--task-manifest", required=True)
    p.add_argument("--workspace-parent", default=None)
    p.set_defaults(handler=_import_attempt)

    p = sub.add_parser("compare", help="Run the deterministic four-condition comparison.")
    add_common(p)
    p.add_argument("--tasks", action="append", default=None, metavar="TASK_ID")
    p.add_argument("--experiment-id", default=DEFAULT_EXPERIMENT_ID)
    p.add_argument("--workspace-parent", default=None)
    p.set_defaults(handler=_compare)

    p = sub.add_parser("export-preferences", help="Build preference pairs from an experiment.")
    add_common(p)
    p.add_argument("--results", required=True)
    p.add_argument("--task-manifest", action="append", required=True, metavar="TASK_JSON")
    p.add_argument("--held-out", action="append", default=None, metavar="TASK_ID")
    p.set_defaults(handler=_export_preferences)

    p = sub.add_parser("demo", help="Provider-free end-to-end demonstration.")
    add_common(p)
    p.add_argument("--tasks", action="append", default=None, metavar="TASK_ID")
    p.add_argument("--experiment-id", default=DEFAULT_EXPERIMENT_ID)
    p.add_argument("--workspace-parent", default=None)
    p.add_argument("--held-out", action="append", default=None, metavar="TASK_ID")
    p.set_defaults(handler=_demo)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.repo_root = _repository_root(getattr(args, "repo_root", None))
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))


__all__ = ["DEFAULT_EXPERIMENT_ID", "build_parser", "main"]
