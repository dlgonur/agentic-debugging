"""S4 — quix40 manifest validation, per-task DebugTask projection, and the
frozen RAG index/retrieval/context assembly.

Cohort identity
---------------

The frozen cohort is ``experiments/raw-pilot-v1.1/state/quix40-v1/
pilot_manifest_frozen_v1.jsonl`` (40 tasks; manifest SHA-256
``57208248...``; frozen bundle ``quix40-v1-state.zip`` SHA-256
``41b9b633...``; QuixBugs revision ``4257f44b``).  Every payload is
verified against its frozen ``payload_sha256`` before use.

Task projection
---------------

The frozen query construction (``build_task_query``) requires a validated
``DebugTask``; the issue projection whitelists exactly
``task_id/title/description/tags``.  The quixbugs task projection is built
from public manifest/payload fields only; the ``oracle`` schema slot is
filled with fixed non-oracle sentinels because QuixBugs payloads carry no
oracle data — the frozen projection never reads it and it never enters the
index (pinned by the anti-oracle unit test).

Index / retrieval
-----------------

One shared repo-mode index over the scoped corpus view (frozen revision,
``python_programs/`` + ``python_testcases/`` only); retrieval per task with
the frozen defaults ``max_results=8``, ``max_context_bytes=4096``; query
bounded by the frozen ``MAX_QUERY_BYTES=2000`` (explicit truncation
marker); ``RagContext`` capped at the frozen 4000 bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentic_debugger.comparison.native import build_task_query
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.rag.context import RagContext
from agentic_debugger.rag.corpus import build_corpus
from agentic_debugger.rag.indexer import build_index, index_to_text
from agentic_debugger.rag.retrieval import retrieve

from experiments.cp118_rag_definitive.s4_payload import (
    MAX_PROMPT_TOKENS,
    build_v12_payload,
    parse_payload_sections,
    sha256_text,
)

MANIFEST_PATH = "experiments/raw-pilot-v1.1/state/quix40-v1/pilot_manifest_frozen_v1.jsonl"
MANIFEST_SHA256 = "572082482a64adabc8c790293580a9869cdda485a3813505f14adec850577afd"
BUNDLE_PATH = "experiments/raw-pilot-v1.1/artifacts/quix40-v1-state.zip"
BUNDLE_SHA256 = "41b9b633c70baae09be4e8f0570932e52f59fc7f25de36e4dec2c4ca66f6bbeb"
QUIXBUGS_REVISION = "4257f44b0ff1181dedaedee6a447e133219fcebf"
EXPECTED_TASK_COUNT = 40

#: Fixed non-oracle sentinels for the DebugTask oracle schema slot.
ORACLE_ABSENT = "S4_QUIXBUGS_ORACLE_ABSENT"

#: Frozen retrieval defaults (schema.py) — passed explicitly and recorded.
MAX_RETRIEVAL_RESULTS = 8
MAX_RETRIEVAL_CONTEXT_BYTES = 4096
MAX_RAG_CONTEXT_BYTES = 4000


class CohortError(RuntimeError):
    """Raised when the frozen cohort cannot be validated."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_manifest(manifest_path: Path) -> List[Dict[str, Any]]:
    """Load and validate the frozen quix40 manifest (identity + shape)."""

    manifest_path = Path(manifest_path)
    if sha256_file(manifest_path) != MANIFEST_SHA256:
        raise CohortError(
            f"manifest sha256 mismatch: {manifest_path} "
            f"got {sha256_file(manifest_path)}, expected {MANIFEST_SHA256}"
        )
    tasks: List[Dict[str, Any]] = []
    with open(manifest_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            tasks.append(json.loads(line))
    if len(tasks) != EXPECTED_TASK_COUNT:
        raise CohortError(
            f"manifest task count {len(tasks)} != {EXPECTED_TASK_COUNT}"
        )
    if [t["slot"] for t in tasks] != list(range(1, EXPECTED_TASK_COUNT + 1)):
        raise CohortError("manifest slots are not 1..40 in order")
    for task in tasks:
        if task.get("quixbugs_revision") != QUIXBUGS_REVISION:
            raise CohortError(
                f"{task['task_id']}: quixbugs_revision "
                f"{task.get('quixbugs_revision')!r} != {QUIXBUGS_REVISION}"
            )
    return tasks


def validate_payloads(tasks: List[Dict[str, Any]], payload_dir: Path) -> Dict[str, str]:
    """Verify every frozen payload file against its manifest sha256 and
    return ``{task_id: payload_text}``."""

    payloads: Dict[str, str] = {}
    for task in tasks:
        payload_file = Path(payload_dir) / task["payload_file"]
        if not payload_file.is_file():
            raise CohortError(f"{task['task_id']}: missing payload {payload_file}")
        text = payload_file.read_text(encoding="utf-8")
        digest = sha256_text(text)
        if digest != task["payload_sha256"]:
            raise CohortError(
                f"{task['task_id']}: payload sha256 {digest} != frozen "
                f"{task['payload_sha256']}"
            )
        payloads[task["task_id"]] = text
    return payloads


def build_task_mapping(task: Dict[str, Any], payload_text: str) -> Dict[str, Any]:
    """Deterministic quixbugs → DebugTask projection mapping.

    Only public manifest/payload fields are used: task_id, program (title),
    the PROBLEM section (description), the visible test path and frozen
    timeouts.  The ``oracle`` slot uses fixed non-oracle sentinels.
    """

    sections = parse_payload_sections(payload_text)
    problem = sections.get("PROBLEM", "")
    visible_test = task["visible_test_path"]
    return {
        "schema_version": "1.0",
        "task_id": task["task_id"],
        "title": task.get("program") or task["task_id"],
        "description": problem,
        "language": "python",
        # The quixbugs cohort has no curated fixture tree; the fixture_path
        # points at the frozen cohort state directory (honest location).
        "fixture_path": "experiments/raw-pilot-v1.1/state/quix40-v1",
        "reproduction": {
            "argv": ["python", "-m", "pytest", "-q", visible_test],
            "cwd": ".",
            "timeout_seconds": 10,
            "expected_exit_code": 1,
        },
        "tests": {
            "fail_to_pass": [visible_test],
            # Schema-required >=1 entry; the frozen quix40 protocol has no
            # pass-to-pass selection (P2P = NOT_RECORDED).  Self-describing
            # non-oracle placeholder; never enters the index (issue
            # projection only).
            "pass_to_pass": ["<quix40-p2p-not-recorded>"],
            "full_suite_argv": ["python", "-m", "pytest", "-q"],
            "timeout_seconds": 10,
        },
        "constraints": {
            "allowed_write_paths": [task["target_file"]],
            # Schema-required mandatory denied set (curated-schema plumbing;
            # inert for retrieval — only the issue projection is indexed).
            "denied_write_paths": ["tests", "task.json"],
            "network_allowed": False,
            "external_services_allowed": False,
            "max_patch_attempts": 1,
            "max_test_runs": 5,
            "max_pdb_observations": 0,
        },
        "oracle": {
            "bug_category": ORACLE_ABSENT,
            "target_files": [],
            "target_symbols": [],
            "root_cause_summary": ORACLE_ABSENT,
            "runtime_evidence_hint": ORACLE_ABSENT,
        },
        "tags": [],
    }


def build_debug_task(task: Dict[str, Any], payload_text: str) -> DebugTask:
    """Validated DebugTask for a quixbugs task.

    ``DebugTask.from_mapping`` hard-requires ``fixture_path`` inside the
    curated fixtures tree, which cannot be satisfied truthfully for
    QuixBugs; every nested component is therefore validated through its own
    ``from_mapping`` and the dataclass is constructed directly.  The issue
    projection (task_id/title/description/tags) is identical to the frozen
    construction path.
    """

    mapping = build_task_mapping(task, payload_text)
    from agentic_debugger.evaluation.task_schema import (
        Constraints,
        Oracle,
        Reproduction,
        Tests,
    )

    return DebugTask(
        schema_version="1.0",
        task_id=mapping["task_id"],
        title=mapping["title"],
        description=mapping["description"],
        language=mapping["language"],
        fixture_path=mapping["fixture_path"],
        reproduction=Reproduction.from_mapping(mapping["reproduction"]),
        tests=Tests.from_mapping(mapping["tests"]),
        constraints=Constraints.from_mapping(mapping["constraints"]),
        oracle=Oracle.from_mapping(mapping["oracle"]),
        tags=mapping["tags"],
    )


def build_v121_payload_hashes(
    tasks: List[Dict[str, Any]], payloads: Dict[str, str]
) -> Dict[str, str]:
    """Deterministic v1.2.1 payload revision hashes (frozen builder)."""

    return {
        task["task_id"]: sha256_text(build_v12_payload(payloads[task["task_id"]]))
        for task in tasks
    }


def failing_output_of(payload_text: str) -> str:
    """The frozen FAILING_TEST_OUTPUT section text."""

    return parse_payload_sections(payload_text).get("FAILING_TEST_OUTPUT", "")


def build_shared_index(
    scoped_root: Path,
    *,
    revision: str,
    repository_identity: str,
) -> Any:
    """One repo-mode index over the scoped frozen-revision corpus view."""

    corpus = build_corpus(str(scoped_root), mode="repo")
    return build_index(
        corpus,
        revision=revision,
        repository_identity=repository_identity,
    )


def retrieve_for_task(
    index: Any,
    task: Dict[str, Any],
    payload_text: str,
    *,
    expected_revision: str,
) -> Any:
    """Frozen retrieval for one task.

    The frozen retrieval bound (``MAX_RETRIEVAL_CONTEXT_BYTES = 4096``)
    exceeds the frozen agent-context cap (``MAX_RAG_CONTEXT_BYTES = 4000``);
    ``RagContext.from_retrieval`` fails closed on results in the slack
    (4000, 4096].  To make the frozen constraints coexist (Amendment 2)
    without inventing a truncation policy, the retrieval bound is the frozen
    agent-context cap itself: results always fit the agent cap, and any
    reduction is reported explicitly via ``context_bytes_truncated`` (never
    silent).  No retrieval parameter is tuned; both constants are frozen.
    """

    debug_task = build_debug_task(task, payload_text)
    query = build_task_query(debug_task, failing_output_of(payload_text))
    return retrieve(
        index,
        query,
        expected_revision=expected_revision,
        max_results=MAX_RETRIEVAL_RESULTS,
        max_context_bytes=MAX_RAG_CONTEXT_BYTES,
    )


def build_rag_context(result: Any) -> RagContext:
    return RagContext.from_retrieval(
        result, max_context_bytes=MAX_RAG_CONTEXT_BYTES
    )


def verify_failing_output_identity(
    tasks: List[Dict[str, Any]], payloads: Dict[str, str]
) -> Dict[str, bool]:
    """Provenance check: FAILING_TEST_OUTPUT section hash vs the frozen
    manifest selection (mode ``full``).  Returns per-task match flags."""

    results: Dict[str, bool] = {}
    for task in tasks:
        sel = task.get("failing_output_selection", {})
        if sel.get("mode") != "full":
            results[task["task_id"]] = False
            continue
        text = failing_output_of(payloads[task["task_id"]])
        results[task["task_id"]] = (
            sha256_text(text) == sel.get("sha256_full")
        )
    return results


def serialize_index(index: Any) -> str:
    return index_to_text(index)
