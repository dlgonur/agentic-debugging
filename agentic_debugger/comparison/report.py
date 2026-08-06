"""Comparison report outputs: canonical JSON, CSV, Markdown, environment.

The canonical experiment document keeps deterministic and nondeterministic
sections apart (``environment`` / ``timing``), mirroring the accepted demo
report conventions.  The pilot disclaimer is emitted in every human- and
machine-readable artifact that summarizes results.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from agentic_debugger.comparison.schema import (
    COMPARISON_SCHEMA_VERSION,
    ComparisonExperiment,
    canonical_json,
)
from agentic_debugger.comparison.metrics import (
    AGGREGATE_METRICS,
    csv_rows,
    to_csv_text,
)

#: Top-level keys that are environment- or timing-dependent and therefore
#: excluded from the deterministic comparison view.
NONDETERMINISTIC_RESULT_KEYS: Tuple[str, ...] = ("environment", "timing")

#: Mandatory disclaimer for every small-pilot report.
PILOT_DISCLAIMER = (
    "This deterministic pilot is not statistically representative."
)

PARITY_NOTE = (
    "The two native conditions use identical repair behavior (same candidate "
    "patch, same verifier outcome); RAG may change only retrieval/citation "
    "metrics. This parity demo does not establish a causal RAG performance "
    "improvement."
)

SYNTHETIC_IDENTITY_NOTE = (
    "Imported base/tuned artifacts are labeled offline-deterministic-demo; "
    "they are infrastructure evidence and do not imply actual QLoRA "
    "evaluation or model performance."
)


def _git(repository_root: str, *args: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repository_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def git_head(repository_root: str) -> str:
    """The repository revision used for index and artifact binding.

    Fail-closed: a comparison run without a resolvable revision is refused.
    """

    head = _git(repository_root, "rev-parse", "HEAD")
    if head is None:
        raise ValueError(
            f"cannot resolve a Git revision for repository root {repository_root!r}"
        )
    return head


def environment_record(repository_root: str) -> Dict[str, Any]:
    """Identify the exact tree and interpreter under test."""

    status = _git(repository_root, "status", "--porcelain")
    return {
        "generated_utc": None,  # filled by the caller (nondeterministic)
        "repository_root_name": os.path.basename(os.path.realpath(repository_root)),
        "git_head": _git(repository_root, "rev-parse", "HEAD"),
        "git_branch": _git(repository_root, "rev-parse", "--abbrev-ref", "HEAD"),
        "git_working_tree_dirty": None if status is None else bool(status),
        "git_status_digest": None
        if status is None
        else hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "python_version": sys.version.split()[0],
        "platform": platform.system(),
        "comparison_schema_version": COMPARISON_SCHEMA_VERSION,
        "network_access_policy": "blocked-in-process",
        "pilot_disclaimer": PILOT_DISCLAIMER,
        "parity_note": PARITY_NOTE,
        "synthetic_identity_note": SYNTHETIC_IDENTITY_NOTE,
    }


def experiment_document(
    experiment: ComparisonExperiment,
    *,
    aggregates: Dict[str, Any],
    delta: Dict[str, Any],
    environment: Optional[Dict[str, Any]] = None,
    timing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The complete machine-readable experiment document."""

    document = experiment.to_mapping(environment=environment, timing=timing)
    document["aggregates"] = aggregates
    document["delta"] = delta
    document["notes"] = {
        "pilot_disclaimer": PILOT_DISCLAIMER,
        "parity_note": PARITY_NOTE,
        "synthetic_identity_note": SYNTHETIC_IDENTITY_NOTE,
        "role_note": (
            "Primary aggregates and baseline deltas use evaluation-role "
            "attempts only; auxiliary (preference-fixture) attempts are "
            "identified in every report and excluded from performance metrics."
        ),
        "telemetry_note": (
            "Provider/network attempts are separated into local verification "
            "evidence (provider_attempts/network_attempts) and external "
            "generation telemetry (external_provider_attempts/"
            "external_network_attempts) when supplied by the imported artifact."
        ),
        "nondeterministic_top_level_keys": list(NONDETERMINISTIC_RESULT_KEYS),
    }
    return document


def results_json(document: Mapping[str, Any]) -> str:
    return canonical_json(document) + "\n"


def deterministic_view(document: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in document.items()
        if key not in NONDETERMINISTIC_RESULT_KEYS
    }


def experiment_csv(experiment: ComparisonExperiment) -> str:
    """Deterministic one-row-per-attempt CSV."""

    return to_csv_text(csv_rows(experiment))


def render_markdown(document: Mapping[str, Any]) -> str:
    """Human-readable Markdown comparison report."""

    experiment_id = document.get("experiment_id", "?")
    baseline = document.get("baseline_condition", "?")
    conditions = list(document.get("conditions", []))
    task_ids = list(document.get("task_ids", []))
    attempts = list(document.get("attempts", []))
    aggregates = document.get("aggregates", {}).get("conditions", [])
    delta_entries = document.get("delta", {}).get("delta_entries", [])

    lines: List[str] = [
        f"# Comparison experiment: {experiment_id}",
        "",
        f"* schema: `{document.get('schema_version')}`",
        f"* baseline condition: `{baseline}`",
        f"* conditions: {', '.join(f'`{c}`' for c in conditions)}",
        f"* tasks: {', '.join(f'`{t}`' for t in task_ids)}",
        f"* attempts: {len(attempts)}",
        "",
        f"> {PILOT_DISCLAIMER}",
        "",
        "## Notes",
        "",
        f"* {PARITY_NOTE}",
        f"* {SYNTHETIC_IDENTITY_NOTE}",
        "",
        "## Per-task results",
        "",
    ]
    key_metrics = (
        "role",
        "valid_patch",
        "f2p_passed",
        "f2p_total",
        "p2p_passed",
        "p2p_total",
        "verifier_outcome",
        "failure_category",
        "retrieval_count",
        "replay_valid",
        "cleanup_status",
        "canonical_fixture_unchanged",
        "provider_attempts",
        "network_attempts",
        "external_provider_attempts",
        "external_network_attempts",
    )
    for task_id in task_ids:
        lines.append(f"### {task_id}")
        lines.append("")
        header = "| attempt_id | condition_id | mode | " + " | ".join(key_metrics) + " |"
        lines.append(header)
        lines.append("|" + "---|" * (3 + len(key_metrics)))
        for attempt in sorted(
            attempts,
            key=lambda a: (a["condition_id"], a["attempt_id"]),
        ):
            if attempt["task_id"] != task_id:
                continue
            row = [
                attempt["attempt_id"],
                attempt["condition_id"],
                attempt["mode"],
            ]
            for metric in key_metrics:
                value = attempt.get(metric)
                row.append("" if value is None else str(value))
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        auxiliary = [
            a["attempt_id"]
            for a in attempts
            if a["task_id"] == task_id and a.get("role") != "evaluation"
        ]
        if auxiliary:
            lines.append(
                f"Auxiliary (preference-fixture) attempts for this task "
                f"(excluded from primary aggregates and deltas): "
                f"{', '.join(auxiliary)}."
            )
            lines.append("")

    lines.append("## Per-condition aggregates")
    lines.append("")
    lines.append(
        "Primary aggregates and baseline deltas use evaluation-role attempts "
        "only; auxiliary (preference-fixture) attempts are counted separately "
        "and can never affect performance metrics."
    )
    lines.append("")
    header = "| condition_id | " + " | ".join(AGGREGATE_METRICS) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (1 + len(AGGREGATE_METRICS)))
    for bucket in sorted(aggregates, key=lambda b: b["condition_id"]):
        row = [bucket["condition_id"]]
        for metric in AGGREGATE_METRICS:
            value = bucket.get(metric)
            row.append("" if value is None else str(value))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lines.append("## Delta against baseline")
    lines.append("")
    lines.append(f"Baseline condition: `{baseline}`")
    lines.append("")
    header = "| condition_id | task_id | metric | baseline_value | value | delta |"
    lines.append(header)
    lines.append("|---|--|--|--|--|--|")
    for entry in delta_entries:
        lines.append(
            "| " + " | ".join(
                [
                    entry["condition_id"],
                    entry["task_id"] or "(aggregate)",
                    entry["metric"],
                    "" if entry["baseline_value"] is None else str(entry["baseline_value"]),
                    "" if entry["value"] is None else str(entry["value"]),
                    "" if entry["delta"] is None else str(entry["delta"]),
                ]
            ) + " |"
        )
    lines.append("")
    lines.append(
        f"> {PILOT_DISCLAIMER} Delta values describe this deterministic "
        "fixture set only."
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "NONDETERMINISTIC_RESULT_KEYS",
    "PILOT_DISCLAIMER",
    "PARITY_NOTE",
    "SYNTHETIC_IDENTITY_NOTE",
    "git_head",
    "environment_record",
    "experiment_document",
    "results_json",
    "deterministic_view",
    "experiment_csv",
    "render_markdown",
]
