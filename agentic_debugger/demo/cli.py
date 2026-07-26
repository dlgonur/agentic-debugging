"""Command-line entry point for the Task 9 end-to-end demonstration.

Usage::

    python -m agentic_debugger.demo --output-dir demo-out

The command is offline by default: it never constructs a model-provider client
and never opens a socket.  It writes a machine-readable ``results.json``, a
generated technical summary, reproduction instructions and one raw plus one
semantic trajectory per case.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from agentic_debugger.demo.policies import DEMO_POLICIES, DemoPolicy, policy_from_value
from agentic_debugger.demo.report import render_summary
from agentic_debugger.demo.runner import (
    DemoCaseStatus,
    DemoError,
    DemoInputError,
    DemoReport,
    curated_task_ids,
    results_json,
    run_demo,
)

#: The exact command a reader should run to reproduce a full demonstration.
DEFAULT_ENTRY_POINT = "python -m agentic_debugger.demo --output-dir demo-out"

RESULTS_FILENAME = "results.json"
SUMMARY_FILENAME = "technical-evaluation-summary.md"
REPRODUCE_FILENAME = "REPRODUCE.md"
TRAJECTORY_DIRNAME = "trajectories"


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentic_debugger.demo",
        description=(
            "Run the deterministic offline end-to-end demonstration of the "
            "agentic-debugging MVP over the curated benchmark set."
        ),
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory that receives results.json, the summary and trajectories.",
    )
    parser.add_argument(
        "--repository-root",
        default=None,
        help="Repository root. Defaults to the checkout containing this package.",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        default=None,
        metavar="TASK_ID",
        help="Restrict the run to a curated task id. Repeatable. Default: all.",
    )
    parser.add_argument(
        "--policy",
        action="append",
        default=None,
        metavar="POLICY",
        choices=[item.value for item in DemoPolicy],
        help="Restrict the run to a demonstration policy. Repeatable. Default: all.",
    )
    parser.add_argument(
        "--workspace-parent",
        default=None,
        help="Existing directory for disposable workspaces. Default: a temporary directory.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Exit non-zero unless every case completed, every verifier result "
            "is COMPLETED and every verifier outcome is RESOLVED. Failures are "
            "always reported either way."
        ),
    )
    parser.add_argument(
        "--list-tasks",
        action="store_true",
        help="Print the discovered curated task ids and exit.",
    )
    return parser


def write_outputs(report: DemoReport, output_dir: Path, entry_point: str) -> dict[str, Path]:
    """Write every demonstration artifact and return the written paths."""

    output_dir.mkdir(parents=True, exist_ok=True)
    trajectories = output_dir / TRAJECTORY_DIRNAME
    trajectories.mkdir(parents=True, exist_ok=True)

    mapping = report.to_mapping()
    results_path = output_dir / RESULTS_FILENAME
    results_path.write_text(results_json(report), encoding="utf-8", newline="\n")

    summary_path = output_dir / SUMMARY_FILENAME
    summary_path.write_text(
        render_summary(mapping, entry_point=entry_point), encoding="utf-8", newline="\n"
    )

    reproduce_path = output_dir / REPRODUCE_FILENAME
    reproduce_path.write_text(
        render_reproduction(report, entry_point), encoding="utf-8", newline="\n"
    )

    written = {
        "results": results_path,
        "summary": summary_path,
        "reproduce": reproduce_path,
    }
    for case in report.cases:
        if case.events_jsonl:
            path = trajectories / f"{case.case_id}.events.jsonl"
            path.write_text(case.events_jsonl, encoding="utf-8", newline="\n")
        if case.semantic_events:
            path = trajectories / f"{case.case_id}.semantic.json"
            path.write_text(
                json.dumps(
                    list(case.semantic_events),
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
    return written


def render_reproduction(report: DemoReport, entry_point: str) -> str:
    """Render clean-checkout reproduction instructions for this exact run."""

    environment = report.environment
    return "\n".join(
        [
            "# Reproducing the Task 9 demonstration",
            "",
            "## Requirements",
            "",
            "* Python >= 3.11 (this run used "
            f"{environment.get('python_version')} on {environment.get('platform')})",
            "* `pytest` available to the same interpreter",
            "* `python` resolvable on PATH — curated task manifests invoke "
            "`python -m pytest`",
            "* No network access and no model-provider credentials are needed "
            "or used",
            "",
            "## Steps from a clean checkout",
            "",
            "```powershell",
            "git clone <repository-url> agentic-debugging-internship",
            "cd agentic-debugging-internship",
            f"git checkout {environment.get('git_head') or '<commit>'}",
            "python -m pip install -e .[test]",
            entry_point,
            "```",
            "",
            "## What is produced",
            "",
            f"* `{RESULTS_FILENAME}` — machine-readable results for every case",
            f"* `{SUMMARY_FILENAME}` — technical summary generated from those results",
            f"* `{TRAJECTORY_DIRNAME}/<case>.events.jsonl` — the raw controller "
            "trajectory (contains real UTC timestamps; not byte-stable)",
            f"* `{TRAJECTORY_DIRNAME}/<case>.semantic.json` — the stable semantic "
            "projection used for trajectory comparison",
            "",
            "## Verifying determinism",
            "",
            "Run the command twice into two directories and compare the "
            "deterministic view:",
            "",
            "```powershell",
            "python -m agentic_debugger.demo --output-dir demo-out-a",
            "python -m agentic_debugger.demo --output-dir demo-out-b",
            "python -c \"import json;from agentic_debugger.demo.runner import "
            "deterministic_view as d;a=d(json.load(open('demo-out-a/results.json')));"
            "b=d(json.load(open('demo-out-b/results.json')));print('identical' if a==b "
            "else 'DIFFERENT')\"",
            "```",
            "",
            "## Tested state",
            "",
            f"* branch: `{environment.get('git_branch')}`",
            f"* HEAD: `{environment.get('git_head')}`",
            f"* working tree dirty: `{environment.get('git_working_tree_dirty')}`",
            f"* `agentic_debugger/` source digest: "
            f"`{environment.get('source_tree_sha256')}`",
            "",
            "A dirty working tree means the run included uncommitted changes; "
            "the source digest pins exactly which bytes were executed.",
            "",
        ]
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = Path(args.repository_root).resolve() if args.repository_root else _repository_root()

    try:
        discovered = curated_task_ids(repo)
    except DemoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list_tasks:
        for task_id in discovered:
            print(task_id)
        return 0

    policies = (
        tuple(policy_from_value(value) for value in args.policy)
        if args.policy
        else DEMO_POLICIES
    )
    task_ids = tuple(args.task_id) if args.task_id else None

    owns_parent = args.workspace_parent is None
    parent = Path(tempfile.mkdtemp(prefix="agentic-demo-")) if owns_parent else Path(args.workspace_parent)
    try:
        if not parent.is_dir():
            print(f"error: workspace parent is not a directory: {parent}", file=sys.stderr)
            return 2
        try:
            report = run_demo(
                repository_root=repo,
                workspace_parent=parent,
                task_ids=task_ids,
                policies=policies,
            )
        except DemoError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        written = write_outputs(report, Path(args.output_dir), DEFAULT_ENTRY_POINT)
    finally:
        if owns_parent:
            shutil.rmtree(parent, ignore_errors=True)

    harness_errors = [
        case.case_id for case in report.cases if case.status is DemoCaseStatus.HARNESS_ERROR
    ]
    incomplete = [
        case.case_id
        for case in report.cases
        if case.status is not DemoCaseStatus.COMPLETED
        or case.verifier.get("status") != "COMPLETED"
        or case.verifier.get("outcome") != "RESOLVED"
    ]
    print(f"cases: {len(report.cases)}")
    print(f"results: {written['results']}")
    print(f"summary: {written['summary']}")
    if harness_errors:
        print(f"harness errors: {', '.join(harness_errors)}", file=sys.stderr)
    if incomplete:
        print(f"incomplete cases: {', '.join(incomplete)}", file=sys.stderr)
    if harness_errors:
        return 1
    if args.strict and incomplete:
        return 1
    return 0


__all__ = [
    "DEFAULT_ENTRY_POINT",
    "REPRODUCE_FILENAME",
    "RESULTS_FILENAME",
    "SUMMARY_FILENAME",
    "TRAJECTORY_DIRNAME",
    "build_parser",
    "main",
    "render_reproduction",
    "write_outputs",
]
