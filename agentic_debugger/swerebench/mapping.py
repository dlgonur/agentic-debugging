"""Map official SWE-rebench instances onto DebugTask without oracle localization."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from agentic_debugger.evaluation.task_schema import DebugTask, TaskSource
from agentic_debugger.swerebench.authority import CANONICAL_DATASET_ID
from agentic_debugger.swerebench.records import OfficialInstanceBundle
from agentic_debugger.swerebench.selection import OrderedTask

_INSTANCE_SANITIZE = re.compile(r"[^a-z0-9]+")
MODEL_F2P_PLACEHOLDER = "hidden-tests-withheld/test_issue.py::test_not_provided_to_model"
MODEL_P2P_PLACEHOLDER = "hidden-tests-withheld/test_issue.py::test_regression_not_provided_to_model"
# Schema requires argv, but the product tool refuses to execute this as a
# reproduction. The model must supply a public_target that already exists.
UNAVAILABLE_PUBLIC_REPRODUCTION = [
    "python",
    "-c",
    "raise SystemExit('no-public-reproduction-declared')",
]


def product_task_id(instance_id: str) -> str:
    slug = _INSTANCE_SANITIZE.sub("-", instance_id.lower()).strip("-")
    task_id = f"swr-{slug}"
    if len(task_id) > 64:
        task_id = task_id[:64].rstrip("-")
    return task_id


def default_denied_paths(extra: Sequence[str] = ()) -> list[str]:
    denied = {"tests", "task.json", ".git"}
    denied.update(path.replace("\\", "/") for path in extra if path)
    return sorted(denied)


def production_write_paths(root: Path) -> list[str]:
    """Runtime-derived write allow-list. Never uses gold patch hunks."""

    blocked = {
        ".git",
        "tests",
        "test",
        "task.json",
        ".github",
        ".circleci",
        "docs",
        "documentation",
        "changelog",
        "history",
    }
    allowed: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        name = entry.name
        if name in blocked or name.startswith("."):
            continue
        if entry.is_dir() or entry.suffix == ".py":
            allowed.append(name)
    if not allowed:
        raise ValueError(f"no production write paths derived from {root}")
    return allowed


def _source(
    instance_id: str,
    repo: str,
    base_commit: str,
    fixture_path: str,
) -> dict[str, object]:
    return {
        "kind": "external",
        "path": fixture_path,
        "provenance": {
            "dataset": CANONICAL_DATASET_ID,
            "manifest_id": instance_id,
            "manifest_fingerprint": base_commit,
            "upstream_repository": f"https://github.com/{repo}",
            "upstream_revision": base_commit,
            "project": repo,
            "bug_id": instance_id,
            "buggy_revision": base_commit,
            "fixed_revision": "withheld-from-model",
        },
    }


def build_model_task(
    ordered: OrderedTask,
    bundle: OfficialInstanceBundle,
    *,
    fixture_path: str,
    allowed_write_paths: Sequence[str],
) -> DebugTask:
    """Model-facing DebugTask: issue text + repo, no hidden tests or gold."""

    mapping = {
        "schema_version": "1.0",
        "task_id": product_task_id(ordered.instance_id),
        "title": f"SWE-rebench V2 {ordered.instance_id}",
        "description": bundle.public.problem_statement,
        "language": "python",
        "fixture_path": fixture_path,
        "source": _source(
            ordered.instance_id, ordered.repo, ordered.base_commit, fixture_path
        ),
        "reproduction": {
            "argv": list(UNAVAILABLE_PUBLIC_REPRODUCTION),
            "cwd": ".",
            "timeout_seconds": 60,
            "expected_exit_code": 1,
        },
        "tests": {
            "fail_to_pass": [MODEL_F2P_PLACEHOLDER],
            "pass_to_pass": [],
            "full_suite_argv": list(UNAVAILABLE_PUBLIC_REPRODUCTION),
            "timeout_seconds": 60,
        },
        "constraints": {
            "allowed_write_paths": list(allowed_write_paths),
            "denied_write_paths": default_denied_paths(),
            "network_allowed": False,
            "external_services_allowed": False,
            "max_patch_attempts": 3,
            "max_test_runs": 12,
            "max_pdb_observations": 8,
        },
        "oracle": {
            "bug_category": "swe-rebench-v2",
            "target_files": ["task.json"],
            "target_symbols": ["withheld"],
            "root_cause_summary": "withheld",
            "runtime_evidence_hint": "withheld",
        },
        "tags": [
            "swe-rebench-v2",
            "validation",
            "clean-le32k",
            "no-public-reproduction",
            "hidden-tests",
            "model-selected-runtime",
        ],
        "evaluation_isolation": {"hide_test_identities_from_model": True},
    }
    return DebugTask.from_mapping(mapping)


def build_verifier_task(
    ordered: OrderedTask,
    bundle: OfficialInstanceBundle,
    *,
    fixture_path: str,
    allowed_write_paths: Sequence[str],
    timeout_seconds: int = 300,
) -> DebugTask:
    """Verifier-only DebugTask. Hidden tests never go to agent_visible_mapping."""

    f2p, p2p = bundle.hidden_tests()
    if not f2p:
        raise ValueError(f"{ordered.instance_id} has no FAIL_TO_PASS tests")
    if not p2p:
        p2p = ()
    if timeout_seconds < 1 or timeout_seconds > 1800:
        raise ValueError("verifier timeout_seconds out of external range")
    pytest_f2p = [
        "python",
        "-m",
        "pytest",
        *list(f2p),
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    mapping = {
        "schema_version": "1.0",
        "task_id": product_task_id(ordered.instance_id),
        "title": f"SWE-rebench V2 {ordered.instance_id}",
        "description": bundle.public.problem_statement,
        "language": "python",
        "fixture_path": fixture_path,
        "source": _source(
            ordered.instance_id, ordered.repo, ordered.base_commit, fixture_path
        ),
        "reproduction": {
            "argv": pytest_f2p,
            "cwd": ".",
            "timeout_seconds": timeout_seconds,
            "expected_exit_code": 1,
        },
        "tests": {
            "fail_to_pass": list(f2p),
            "pass_to_pass": list(p2p),
            "full_suite_argv": [
                "python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            "timeout_seconds": timeout_seconds,
        },
        "constraints": {
            "allowed_write_paths": list(allowed_write_paths),
            "denied_write_paths": default_denied_paths(),
            "network_allowed": False,
            "external_services_allowed": False,
            "max_patch_attempts": 3,
            "max_test_runs": 12,
            "max_pdb_observations": 8,
        },
        "oracle": {
            "bug_category": "swe-rebench-v2",
            "target_files": ["task.json"],
            "target_symbols": ["withheld"],
            "root_cause_summary": "withheld",
            "runtime_evidence_hint": "withheld",
        },
        "tags": [
            "swe-rebench-v2",
            "validation",
            "clean-le32k",
            "verifier-private-tests",
        ],
        "evaluation_isolation": {"hide_test_identities_from_model": True},
    }
    return DebugTask.from_mapping(mapping)
