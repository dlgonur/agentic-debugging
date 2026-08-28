"""S4 — definitive cp118 + frozen RAG treatment runner.

Subcommands
-----------

* ``validate`` — OFFLINE qualification.  No model is loaded and no
  inference is executed.  Verifies: git state (branch/HEAD/clean tracked
  tree), frozen quix40 manifest + payload identities (40 tasks), cp118
  adapter identity (fail-closed, accepted S2 convention), QuixBugs frozen
  revision checkout, anti-oracle scoped corpus materialization, one shared
  repo-mode index, per-task frozen retrieval determinism (two runs →
  identical retrieval identities), RagContext budgets, and the assembled
  v1.2.1+RAG prompt budgets for all 40 tasks (tokenizer-only token counts,
  no model).  Writes ``validation-evidence.json``.
* ``generate`` — full preflight (validate) then the live one-shot cp118+RAG
  generation: 40 tasks × 1 greedy generation (``max_new_tokens=4096``),
  C9-compatible ``raw/`` + ``meta/`` outputs plus per-task retrieval
  evidence, then ``S4_GENERATION_COMPLETE.json`` (40 valid pairs).  The
  generation is bound to the source-freeze identity via an immutable
  ``run-identity.json`` (source_commit_sha, branch, contract SHA256,
  adapter tree identity, cohort/manifest identity, protocol identity).
  Resume: valid completed pairs are never regenerated; incomplete/corrupt
  pairs fail closed; infrastructure-only failures retry the current task up
  to the frozen limit with identical settings.
* ``eval`` — frozen v1.2.1 CPU evaluation semantics over the generated
  pairs.  Fails closed unless the generation's run identity matches the
  current authorized source/contract identities (generation HEAD == current
  HEAD, generation contract SHA256 == current contract SHA256, and the
  cp118/cohort/protocol identities equal the frozen expected values).
* ``smoke-eval`` — bounded OFFLINE evaluator smoke (engineering validation
  only, no model): one real frozen QuixBugs task with a canned non-model
  candidate through the real s4_eval orchestration and the real
  ``run_cmd`` interface.

The runner never commits, never merges, and refuses to run ``generate``
unless the tracked working tree is clean and the contract identities hold
(owner source-freeze commit is a prerequisite for the live run).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agentic_debugger.rag.schema import canonical_json

from experiments.cp118_rag_definitive.s4_corpus import (
    BUNDLE_PATH,
    BUNDLE_SHA256,
    MANIFEST_PATH,
    MANIFEST_SHA256,
    MAX_RAG_CONTEXT_BYTES,
    MAX_RETRIEVAL_CONTEXT_BYTES,
    MAX_RETRIEVAL_RESULTS,
    QUIXBUGS_REVISION,
    build_rag_context,
    build_shared_index,
    build_v121_payload_hashes,
    retrieve_for_task,
    serialize_index,
    validate_manifest,
    validate_payloads,
    verify_failing_output_identity,
)
from experiments.cp118_rag_definitive.s4_payload import (
    MAX_NEW_TOKENS,
    MAX_PROMPT_TOKENS,
    OUTPUT_REQUIREMENTS_V121,
    PROTOCOL_VERSION,
    assemble_rag_payload,
    atomic_write_text,
    build_v12_payload,
    canonical_rag_block,
    record_prompt_budget,
    sha256_text,
)
from experiments.cp118_rag_definitive.s4_quixbugs import (
    ensure_quixbugs_repo,
    materialize_scoped_corpus,
)
from experiments.cp118_rag_definitive.s4_transport import (
    LocalCp118QwenTransport,
    TransportError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = Path(__file__).resolve().parent
CONTRACT_PATH = PACKAGE_DIR / "s4_contract.json"
MODEL_ID = "Qwen/Qwen2.5-Coder-7B-Instruct+CP118-RAG"
RUNNER_VERSION = "s4-cp118-rag-definitive-1"
BASE_REVISION = "c03e6d358207e414f1eca0bb1891e29f1db0e242"
INFRA_RETRIES_PER_TASK = 2

#: gitignored run output root (matches the repository ``runs/`` ignore rule).
DEFAULT_OUTPUT_DIR = PACKAGE_DIR / "runs"


class RunnerError(RuntimeError):
    """Raised when an S4 stage cannot proceed (fail closed)."""


class RunLogger:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")

    def log(self, msg: str = "") -> None:
        line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}"
        print(line, flush=True)
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def _run_cmd(cmd: List[str], *, cwd: Optional[str] = None, timeout: int = 1200):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)


def git_state() -> Dict[str, Any]:
    branch = _run_cmd(["git", "branch", "--show-current"], cwd=str(REPO_ROOT))
    head = _run_cmd(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT))
    tracked = _run_cmd(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=str(REPO_ROOT),
    )
    full = _run_cmd(["git", "status", "--porcelain"], cwd=str(REPO_ROOT))
    untracked = [
        line[3:] for line in full.stdout.splitlines() if line.startswith("??")
    ]
    return {
        "branch": branch.stdout.strip(),
        "head_sha": head.stdout.strip(),
        "tracked_dirty": bool(tracked.stdout.strip()),
        "tracked_porcelain": tracked.stdout.strip()[:4000],
        "untracked_paths": untracked[:200],
    }


def load_contract() -> Dict[str, Any]:
    if not CONTRACT_PATH.is_file():
        raise RunnerError(f"contract missing: {CONTRACT_PATH}")
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    contract["contract_sha256"] = sha256_text(
        CONTRACT_PATH.read_text(encoding="utf-8")
    )
    return contract


def protocol_identity() -> str:
    """Deterministic protocol identity: version + generation settings +
    the verbatim output-requirements block."""

    return sha256_text(canonical_json({
        "protocol_version": PROTOCOL_VERSION,
        "max_new_tokens": MAX_NEW_TOKENS,
        "max_prompt_tokens": MAX_PROMPT_TOKENS,
        "output_requirements_v121_sha256": sha256_text(OUTPUT_REQUIREMENTS_V121),
    }))


def build_run_identity(contract: Dict[str, Any], g: Dict[str, Any]) -> Dict[str, Any]:
    identity = {
        "schema_version": "s4-cp118-rag-definitive-run-identity",
        "runner_version": RUNNER_VERSION,
        "source_commit_sha": g["head_sha"],
        "branch": g["branch"],
        "contract_sha256": contract["contract_sha256"],
        "adapter_tree_identity_sha256": contract["model"]["adapter_identity"][
            "tree_identity_sha256"],
        "cohort_manifest_sha256": MANIFEST_SHA256,
        "quixbugs_revision": QUIXBUGS_REVISION,
        "protocol_identity": protocol_identity(),
        "model_condition": MODEL_ID,
        "rag_enabled": True,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    identity["run_identity_sha256"] = sha256_text(
        canonical_json({k: v for k, v in identity.items()
                        if k != "run_identity_sha256"})
    )
    return identity


def write_or_verify_run_identity(out_dir: Path, identity: Dict[str, Any]) -> Dict[str, Any]:
    """Write the immutable run identity exactly once, or REUSE the stored
    identity on resume (Repair Pass 2, Blocker 1).

    The first invocation creates exactly one identity (including
    ``created_at`` and ``run_identity_sha256``) and persists it.  Subsequent
    invocations validate the stored identity against the current source/
    contract state and return the STORED identity verbatim — a resumed run
    never produces a second identity hash.  Fails closed on any drift.
    """

    path = out_dir / "run-identity.json"
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        for key in ("schema_version", "runner_version", "source_commit_sha",
                    "branch", "contract_sha256",
                    "adapter_tree_identity_sha256", "cohort_manifest_sha256",
                    "quixbugs_revision", "protocol_identity",
                    "model_condition", "rag_enabled"):
            if existing.get(key) != identity[key]:
                raise RunnerError(
                    f"run dir identity conflict: {key} "
                    f"existing={existing.get(key)} current={identity[key]}"
                )
        # The stored run_identity_sha256 cannot be compared against the
        # freshly constructed one (created_at differs by construction); it
        # is verified by self-consistency over the stored identity fields.
        stored_sha = existing.get("run_identity_sha256")
        recomputed = sha256_text(canonical_json(
            {k: v for k, v in existing.items()
             if k != "run_identity_sha256"}
        ))
        if not stored_sha or stored_sha != recomputed:
            raise RunnerError(
                "run dir identity conflict: stored run_identity_sha256 is "
                "not self-consistent (tampered or corrupt)"
            )
        return existing
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_dir / "run-identity.json",
        json.dumps(identity, indent=2, ensure_ascii=False))
    return identity


def _nvidia_gpu_info() -> str:
    try:
        out = _run_cmd(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader"],
            timeout=30,
        )
        if out.returncode == 0:
            return out.stdout.strip().splitlines()[0]
    except Exception:  # noqa: BLE001
        pass
    return "NOT_RECORDED"


def _count_tokens_with_tokenizer(text: str) -> int:
    """Exact token count via the frozen base tokenizer (CPU, no model)."""

    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(
        "Qwen/Qwen2.5-Coder-7B-Instruct", revision=BASE_REVISION
    )
    return len(tok(text)["input_ids"])


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def validate_stage(args: argparse.Namespace, logger: RunLogger) -> Dict[str, Any]:
    logger.log("VALIDATE stage (no model load, no inference)")
    evidence: Dict[str, Any] = {
        "stage": "validate",
        "runner_version": RUNNER_VERSION,
        "git": git_state(),
        "contract": load_contract(),
    }

    # Cohort identity.
    tasks = validate_manifest(REPO_ROOT / MANIFEST_PATH)
    payloads = validate_payloads(
        tasks, REPO_ROOT / "experiments/raw-pilot-v1.1/state/quix40-v1/payloads")
    bundle = REPO_ROOT / BUNDLE_PATH
    if not bundle.is_file():
        raise RunnerError(f"frozen bundle missing: {bundle}")
    bundle_digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    if bundle_digest != BUNDLE_SHA256:
        raise RunnerError(
            f"bundle sha256 {bundle_digest} != frozen {BUNDLE_SHA256}"
        )
    failing_match = verify_failing_output_identity(tasks, payloads)
    mismatches = sorted(tid for tid, ok in failing_match.items() if not ok)
    evidence["cohort"] = {
        "manifest_path": MANIFEST_PATH,
        "manifest_sha256": MANIFEST_SHA256,
        "bundle_sha256": BUNDLE_SHA256,
        "task_count": len(tasks),
        "payloads_verified": len(payloads),
        "failing_output_identity_match": failing_match,
        "failing_output_identity_match_count": sum(failing_match.values()),
        "failing_output_identity_mismatch_tasks": mismatches,
        "failing_output_identity_note": (
            "38/40 FAILING_TEST_OUTPUT sections hash-identical to the frozen "
            "manifest selection. For quixbugs-mergesort and "
            "quixbugs-possible_change the payload embeds a rendering that "
            "differs from the recorded selection hash (same visible_chars); "
            "the frozen payload remains the prompt authority (payload_sha256 "
            "verified for all 40) and the retrieval query uses the payload "
            "text. Recorded as a provenance observation; not a safety failure."
        ),
    }
    logger.log(
        f"COHORT OK: {len(tasks)} tasks; failing-output identity match "
        f"{sum(failing_match.values())}/40 (mismatch: {mismatches or 'none'})"
    )

    # Adapter identity (fail closed).
    if not args.adapter_path:
        raise RunnerError(
            "--adapter-path is required; pass it explicitly or set "
            "AGENTIC_DEBUGGER_CP118_ADAPTER"
        )
    adapter_path = Path(args.adapter_path)
    expected = evidence["contract"]["model"]["adapter_identity"]
    from experiments.cp118_rag_definitive.s4_identity import (
        verify_adapter_identity,
    )
    adapter_identity = verify_adapter_identity(adapter_path, expected)
    evidence["adapter"] = adapter_identity
    logger.log(
        "ADAPTER OK: tree_identity_sha256 "
        f"{adapter_identity['tree_identity_sha256']}"
    )

    # QuixBugs frozen revision + scoped anti-oracle corpus.
    quix_root = Path(args.quixbugs_root) if args.quixbugs_root else (
        REPO_ROOT / "tmp" / "s4"
    )
    repo = ensure_quixbugs_repo(quix_root, logger=logger)
    scoped_root = quix_root / "scoped-corpus-v1"
    scoped = materialize_scoped_corpus(repo, scoped_root, logger=logger)
    evidence["quixbugs"] = {
        "revision": QUIXBUGS_REVISION,
        "repo_path": str(repo),
        "scoped_corpus_root": str(scoped_root),
        "scoped_tree_identity_sha256": scoped["tree_identity_sha256"],
        "scoped_file_count": scoped["copied_files"],
        "scope_dirs": ["python_programs", "python_testcases"],
    }

    # Shared repo-mode index.
    revision = evidence["git"]["head_sha"]
    repository_identity = (
        f"QuixBugs@{QUIXBUGS_REVISION} "
        f"scoped:{scoped['tree_identity_sha256']}"
    )
    index = build_shared_index(
        scoped_root, revision=revision, repository_identity=repository_identity
    )
    index_text = serialize_index(index)
    evidence["index"] = {
        "index_id": index.index_id,
        "revision": revision,
        "repository_identity": repository_identity,
        "index_bytes": len(index_text.encode("utf-8")),
    }
    logger.log(f"INDEX OK: {index.index_id}")

    # Per-task retrieval determinism + RagContext + payload budgets.
    v121_hashes = build_v121_payload_hashes(tasks, payloads)
    per_task: Dict[str, Any] = {}
    for task in tasks:
        tid = task["task_id"]
        r1 = retrieve_for_task(
            index, task, payloads[tid], expected_revision=revision
        )
        r2 = retrieve_for_task(
            index, task, payloads[tid], expected_revision=revision
        )
        ctx = build_rag_context(r1)
        rag_mapping = ctx.to_request_mapping()
        v12 = build_v12_payload(payloads[tid])
        assembled = assemble_rag_payload(v12, rag_mapping)
        manifest_tokens = (
            task.get("prompt_tokens_by_model", {})
            .get("Qwen/Qwen2.5-Coder-7B-Instruct")
        )
        assembled_tokens = None
        if args.count_tokens:
            assembled_tokens = _count_tokens_with_tokenizer(assembled)
        budget = record_prompt_budget(
            base_prompt=v12,
            retrieved_context_bytes=len(
                canonical_rag_block(rag_mapping).encode("utf-8")
            ),
            assembled_prompt=assembled,
            assembled_prompt_tokens=assembled_tokens,
            base_prompt_tokens_manifest=manifest_tokens,
            rag_context_truncated=bool(ctx.truncated),
        )
        if r1.retrieval_id != r2.retrieval_id:
            raise RunnerError(
                f"{tid}: retrieval determinism broken "
                f"({r1.retrieval_id} != {r2.retrieval_id})"
            )
        per_task[tid] = {
            "retrieval_id": r1.retrieval_id,
            "query_identity": r1.query_identity,
            "context_identity": ctx.context_identity(),
            "selected_chunk_count": len(rag_mapping.get("chunks", [])),
            "selected_bytes": ctx.selected_bytes,
            "retrieval_truncated": r1.result_count_truncated,
            "retrieval_context_bytes_truncated": r1.context_bytes_truncated,
            "context_truncated": ctx.truncated,
            "v121_payload_sha256": v121_hashes[tid],
            "assembled_prompt_sha256": sha256_text(assembled),
            "budget": budget,
        }
    evidence["per_task"] = per_task
    evidence["summary"] = {
        "task_count": len(per_task),
        "retrieval_determinism": "PASS (two runs identical)",
        "rag_context_bytes_cap": MAX_RAG_CONTEXT_BYTES,
        "retrieval_results_cap": MAX_RETRIEVAL_RESULTS,
        "retrieval_context_bytes_cap": MAX_RETRIEVAL_CONTEXT_BYTES,
        "retrieval_bound_used": (
            "frozen agent-context cap 4000 (frozen 4096 bound exceeds the "
            "4000 agent cap; RagContext fails closed in the slack)"
        ),
        "protocol_max_prompt_tokens": MAX_PROMPT_TOKENS,
        "protocol_max_new_tokens": MAX_NEW_TOKENS,
        "protocol_version": PROTOCOL_VERSION,
        "protocol_identity": protocol_identity(),
        "gpu": _nvidia_gpu_info(),
    }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_dir / "validation-evidence.json",
        json.dumps(evidence, indent=2, ensure_ascii=False))
    atomic_write_text(out_dir / "index-v1.json", index_text)
    logger.log("VALIDATE PASS")
    return evidence


# ---------------------------------------------------------------------------
# pair validity + resume (Blocker 4)
# ---------------------------------------------------------------------------


def _slug(model_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", model_id)


def pair_files(out_dir: Path, task_id: str) -> Tuple[Path, Path, Path]:
    stem = f"{_slug(MODEL_ID)}__{task_id}"
    return (
        out_dir / "raw" / f"{stem}.txt",
        out_dir / "meta" / f"{stem}.json",
        out_dir / "retrieval" / f"{stem}.json",
    )


def pair_is_valid(
    out_dir: Path, task: Dict[str, Any], *,
    expected_run_identity_sha256: str,
) -> Tuple[bool, str]:
    """Deterministic pair validity (Repair Pass 2, Blocker 3).

    Proves the pair BELONGS TO THIS RUN and that RAG was actually used:

    * raw + meta + retrieval evidence exist;
    * meta binds the frozen payload/protocol identities and its own raw
      hash (as before);
    * ``meta.run_identity_sha256`` equals the immutable run identity SHA
      (a copied/stale pair from another run fails closed);
    * retrieval evidence is parseable JSON with ``task_id`` matching the
      task and the RAG provenance fields (``query_identity``,
      ``retrieval_id``, ``context_identity``, ``selected_chunk_count``)
      present and consistent with the meta's ``rag`` block
      (``meta.rag.retrieval_id == retrieval.retrieval_id``).
    """

    raw, meta_p, retrieval_p = pair_files(out_dir, task["task_id"])
    if not (raw.is_file() and meta_p.is_file() and retrieval_p.is_file()):
        return False, "missing_pair_files"
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False, "meta_unparseable"
    checks = [
        meta.get("protocol_version") == PROTOCOL_VERSION,
        meta.get("model") == MODEL_ID,
        meta.get("task_id") == task["task_id"],
        int(meta.get("rank", -1)) == int(task["slot"]),
        meta.get("source_v1_1_payload_sha256") == task["payload_sha256"],
        meta.get("max_new_tokens") == MAX_NEW_TOKENS,
        meta.get("raw_sha256") == sha256_text(
            raw.read_text(encoding="utf-8")),
        meta.get("rag", {}).get("enabled") is True,
        meta.get("run_identity_sha256") == expected_run_identity_sha256,
    ]
    if not all(checks):
        return False, "meta_mismatch"
    try:
        retrieval = json.loads(retrieval_p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False, "retrieval_unparseable"
    if not isinstance(retrieval, dict):
        return False, "retrieval_unparseable"
    rag = meta.get("rag", {})
    retrieval_checks = [
        retrieval.get("task_id") == task["task_id"],
        isinstance(retrieval.get("query_identity"), str)
        and len(retrieval["query_identity"]) > 0,
        isinstance(retrieval.get("retrieval_id"), str)
        and len(retrieval["retrieval_id"]) > 0,
        isinstance(retrieval.get("context_identity"), str)
        and len(retrieval["context_identity"]) > 0,
        isinstance(retrieval.get("selected_chunk_count"), int)
        and retrieval["selected_chunk_count"] >= 0,
        retrieval.get("retrieval_id") == rag.get("retrieval_id"),
        retrieval.get("query_identity") == rag.get("query_identity"),
    ]
    if not all(retrieval_checks):
        return False, "retrieval_mismatch"
    return True, ""


def completion_marker(out_dir: Path) -> Dict[str, Any]:
    path = out_dir / "S4_GENERATION_COMPLETE.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def plan_resume(
    out_dir: Path, tasks: List[Dict[str, Any]], *,
    expected_run_identity_sha256: str,
) -> Tuple[List[str], List[Dict[str, Any]], List[str]]:
    """Resume planning: returns ``(skipped_valid, pending, corrupt)``.
    A valid already-completed pair (proven to belong to this run via the
    immutable run identity) is skipped and never regenerated; partial,
    corrupt, or foreign-run evidence is never treated as valid."""

    skipped: List[str] = []
    pending: List[Dict[str, Any]] = []
    corrupt: List[str] = []
    for task in tasks:
        valid, reason = pair_is_valid(
            out_dir, task,
            expected_run_identity_sha256=expected_run_identity_sha256,
        )
        raw, _, _ = pair_files(out_dir, task["task_id"])
        if valid:
            skipped.append(task["task_id"])
        elif raw.exists():
            corrupt.append(f"{task['task_id']}({reason})")
        else:
            pending.append(task)
    return skipped, pending, corrupt


def with_infra_retries(tid: str, attempt_fn, *, max_retries: int,
                       logger=None):
    """Bounded infrastructure-only retry loop (Blocker 4).

    Retries ONLY ``TransportError``/``OSError``/``TimeoutExpired``
    (infrastructure); any other exception (including ``RunnerError`` from
    pair validation after a generation) aborts immediately — a valid
    completion is never retried for scientific reasons, and prompt/
    retrieval/model settings are never changed on retry (the attempt
    function is deterministic)."""

    attempts = 0
    while True:
        try:
            return attempt_fn(), attempts + 1
        except (TransportError, OSError, subprocess.TimeoutExpired) as exc:
            attempts += 1
            if attempts > max_retries:
                raise RunnerError(
                    f"{tid}: infrastructure failure after {max_retries} "
                    f"retries: {exc}"
                ) from exc
            if logger:
                logger.log(
                    f"TASK {tid} infra retry {attempts}/{max_retries}: {exc}"
                )


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def _assemble_task_prompt(
    index: Any, task: Dict[str, Any], payload_text: str, revision: str
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Deterministic per-task RAG assembly (frozen; identical on retry)."""

    result = retrieve_for_task(
        index, task, payload_text, expected_revision=revision
    )
    ctx = build_rag_context(result)
    rag_mapping = ctx.to_request_mapping()
    v12 = build_v12_payload(payload_text)
    assembled = assemble_rag_payload(v12, rag_mapping)
    budget = record_prompt_budget(
        base_prompt=v12,
        retrieved_context_bytes=len(
            canonical_rag_block(rag_mapping).encode("utf-8")
        ),
        assembled_prompt=assembled,
        assembled_prompt_tokens=None,
        base_prompt_tokens_manifest=(
            task.get("prompt_tokens_by_model", {})
            .get("Qwen/Qwen2.5-Coder-7B-Instruct")
        ),
        rag_context_truncated=bool(ctx.truncated),
    )
    rag_evidence = {
        "task_id": task["task_id"],
        "index_id": result.index_id,
        "query_identity": result.query_identity,
        "retrieval_id": result.retrieval_id,
        "context_identity": ctx.context_identity(),
        "selected_chunk_count": len(rag_mapping.get("chunks", [])),
        "selected_bytes": ctx.selected_bytes,
        "retrieval_truncated": result.result_count_truncated,
        "retrieval_context_bytes_truncated": result.context_bytes_truncated,
        "context_truncated": ctx.truncated,
        "retrieval_latency_ms": (
            result.latency_ms if hasattr(result, "latency_ms") else None
        ),
        "budget": budget,
    }
    return assembled, rag_evidence, budget


def _model_call(transport, assembled: str) -> Dict[str, Any]:
    """One model generation call.

    Normalizes CUDA out-of-memory ``RuntimeError`` into the retryable
    transport-error policy so the contract's retry scope
    (process/timeout/OOM) matches what the code actually supports.
    """

    try:
        return transport.generate_one(assembled)
    except RuntimeError as exc:
        if "out of memory" in str(exc).lower():
            raise TransportError(f"cuda out of memory: {exc}") from exc
        raise


def _persist_pair(
    *, out_dir: Path, task: Dict[str, Any], gen: Dict[str, Any],
    rag_evidence: Dict[str, Any], budget: Dict[str, Any],
    run_identity_sha256: str, load_s: Any, v121_payload_sha256: str,
) -> None:
    """Post-completion persistence, EXACTLY ONCE (Repair Pass 2, Blocker 2).

    Called only after the model call returned successfully; any failure
    here aborts fail-closed and MUST NOT invoke the model again."""

    tid = task["task_id"]
    if int(gen["prompt_tokens"]) > MAX_PROMPT_TOKENS:
        raise RunnerError(
            f"{tid}: assembled prompt {gen['prompt_tokens']} tokens > "
            f"max_prompt_tokens {MAX_PROMPT_TOKENS}"
        )
    raw_path, meta_path, retrieval_path = pair_files(out_dir, tid)
    atomic_write_text(raw_path, gen["text"])
    meta = {
        "runner_version": RUNNER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "source_state_protocol": "v1.1",
        "model": MODEL_ID,
        "model_revision": BASE_REVISION,
        "task_id": tid,
        "rank": int(task["slot"]),
        "source_v1_1_payload_sha256": task["payload_sha256"],
        "v1_2_1_payload_sha256": v121_payload_sha256,
        "raw_sha256": sha256_text(gen["text"]),
        "gpu": _nvidia_gpu_info(),
        "load_s": load_s,
        "max_new_tokens": MAX_NEW_TOKENS,
        "completed_at": datetime.now().astimezone().isoformat(),
        "prompt_tokens": int(gen["prompt_tokens"]),
        "output_tokens": int(gen["output_tokens"]),
        "generation_latency_s": float(gen["generation_latency_s"]),
        "peak_allocated_gib": float(gen["peak_allocated_gib"]),
        "run_identity_sha256": run_identity_sha256,
        "rag": {
            "enabled": True,
            "index_id": rag_evidence["index_id"],
            "query_identity": rag_evidence["query_identity"],
            "retrieval_id": rag_evidence["retrieval_id"],
            "context_identity": rag_evidence["context_identity"],
            "retrieved_context_bytes": budget["retrieved_context_bytes"],
            "selected_chunk_count": rag_evidence["selected_chunk_count"],
            "selected_bytes": rag_evidence["selected_bytes"],
            "retrieval_truncated": rag_evidence["retrieval_truncated"],
            "retrieval_context_bytes_truncated": rag_evidence[
                "retrieval_context_bytes_truncated"],
            "context_truncated": rag_evidence["context_truncated"],
            "budget": budget,
            "temperature_top_p": "NOT_RECORDED",
        },
    }
    atomic_write_text(meta_path, json.dumps(meta, indent=2, ensure_ascii=False))
    atomic_write_text(retrieval_path,
                      json.dumps(rag_evidence, indent=2, ensure_ascii=False))
    valid, reason = pair_is_valid(
        out_dir, task, expected_run_identity_sha256=run_identity_sha256
    )
    if not valid:
        raise RunnerError(
            f"{tid}: generated pair failed internal validation ({reason}); "
            f"run aborted"
        )


def generate_and_persist_task(
    *, transport, index, task: Dict[str, Any], payload_text: str,
    revision: str, out_dir: Path, run_identity_sha256: str, load_s: Any,
    logger=None,
) -> Tuple[Dict[str, Any], int]:
    """Per-task orchestration (Repair Pass 2, Blocker 2).

    Phase 1 — deterministic prompt/retrieval assembly (no model; a failure
    here aborts — assembly is pure and deterministic, not retryable).
    Phase 2 — the model-generation operation ONLY, with bounded retries for
    explicitly authorized pre-completion transport/infrastructure failures.
    Phase 3 — post-completion persistence + pair validation EXACTLY ONCE;
    once ``generate_one`` returns, the completion is final and any
    persistence/validation failure aborts without invoking the model again.
    """

    tid = task["task_id"]
    # Phase 1 — deterministic prompt/retrieval assembly (no model; a
    # failure here aborts — assembly is pure and deterministic, not
    # retryable).
    assembled, rag_evidence, budget = _assemble_task_prompt(
        index, task, payload_text, revision
    )
    v121_payload_sha256 = sha256_text(build_v12_payload(payload_text))
    # Phase 2 — the model-generation operation ONLY, with bounded retries
    # for explicitly authorized pre-completion infrastructure failures.
    gen, attempts_used = with_infra_retries(
        tid, lambda: _model_call(transport, assembled),
        max_retries=INFRA_RETRIES_PER_TASK, logger=logger,
    )
    # Phase 3 — post-completion persistence + pair validation EXACTLY ONCE.
    try:
        _persist_pair(
            out_dir=out_dir, task=task, gen=gen,
            rag_evidence=rag_evidence, budget=budget,
            run_identity_sha256=run_identity_sha256, load_s=load_s,
            v121_payload_sha256=v121_payload_sha256,
        )
    except RunnerError:
        raise
    except Exception as exc:  # noqa: BLE001 - post-completion abort
        raise RunnerError(
            f"{tid}: post-completion persistence failed ({exc}); the "
            f"completed generation is NOT regenerated — run aborted"
        ) from exc
    return gen, attempts_used


def generate_stage(args: argparse.Namespace, logger: RunLogger) -> None:
    logger.log("GENERATE stage (live one-shot cp118+RAG, 40 tasks)")
    t_start = time.monotonic()
    evidence = validate_stage(args, logger)
    g = evidence["git"]
    if g["tracked_dirty"]:
        raise RunnerError(
            "tracked working tree is dirty; the owner source-freeze commit "
            "is required before the live run"
        )

    contract = load_contract()
    out_dir = Path(args.output_dir)
    # The immutable run identity is created exactly once and REUSED verbatim
    # on resume (Repair Pass 2, Blocker 1).
    run_identity = write_or_verify_run_identity(
        out_dir, build_run_identity(contract, g))
    logger.log(
        f"RUN IDENTITY: source_commit_sha={run_identity['source_commit_sha']} "
        f"run_identity_sha256={run_identity['run_identity_sha256']}"
    )

    # An already-complete run is never regenerated.
    marker = completion_marker(out_dir)
    if marker.get("status") == "complete" and marker.get("valid_pairs") == 40:
        raise RunnerError(
            "run already complete with 40 valid pairs; refusing to "
            "regenerate (frozen resume rule)"
        )

    tasks = validate_manifest(REPO_ROOT / MANIFEST_PATH)
    payloads = validate_payloads(
        tasks, REPO_ROOT / "experiments/raw-pilot-v1.1/state/quix40-v1/payloads"
    )
    revision = g["head_sha"]
    scoped_root = Path(evidence["quixbugs"]["scoped_corpus_root"])
    index = build_shared_index(
        scoped_root,
        revision=revision,
        repository_identity=evidence["index"]["repository_identity"],
    )

    # Resume: valid pairs (proven to belong to THIS run) are skipped, never
    # regenerated; corrupt/foreign evidence fails closed.
    skipped, pending, corrupt = plan_resume(
        out_dir, tasks,
        expected_run_identity_sha256=run_identity["run_identity_sha256"],
    )
    if corrupt:
        raise RunnerError(
            "partial/corrupt/foreign-run pair evidence must not be silently "
            "treated as valid or overwritten; clean the run dir or rerun in "
            "a fresh output dir. Corrupt: " + ", ".join(corrupt)
        )
    logger.log(f"RESUME: valid_skipped={len(skipped)} pending={len(pending)}")

    transport = LocalCp118QwenTransport(
        adapter_path=args.adapter_path,
        expected_adapter_identity=contract["model"]["adapter_identity"],
        max_new_tokens=MAX_NEW_TOKENS,
        max_input_tokens=MAX_PROMPT_TOKENS,
    )
    transport.load()
    logger.log(
        f"MODEL loaded: {MODEL_ID}; load_s={transport._load_seconds:.2f}"
    )

    raw_dir = out_dir / "raw"
    meta_dir = out_dir / "meta"
    retrieval_dir = out_dir / "retrieval"
    for d in (raw_dir, meta_dir, retrieval_dir):
        d.mkdir(parents=True, exist_ok=True)

    generated: List[str] = []
    try:
        for task in pending:
            gen, attempts_used = generate_and_persist_task(
                transport=transport, index=index, task=task,
                payload_text=payloads[task["task_id"]], revision=revision,
                out_dir=out_dir,
                run_identity_sha256=run_identity["run_identity_sha256"],
                load_s=transport._load_seconds, logger=logger,
            )
            generated.append(task["task_id"])
            logger.log(
                f"TASK {task['task_id']} OK (attempt {attempts_used}); "
                f"tokens={gen['prompt_tokens']}+{gen['output_tokens']}")
    finally:
        transport.close()

    # The completion marker is written only after ALL 40 valid pairs exist.
    all_valid, invalid = all_pairs_valid(
        out_dir, tasks,
        expected_run_identity_sha256=run_identity["run_identity_sha256"],
    )
    if not all_valid:
        raise RunnerError(
            f"generation finished with invalid pairs; marker NOT written: "
            f"{invalid[:10]}"
        )
    elapsed = time.monotonic() - t_start
    marker = {
        "status": "complete",
        "protocol_version": PROTOCOL_VERSION,
        "condition": MODEL_ID,
        "valid_pairs": len(skipped) + len(generated),
        "expected_pairs": len(tasks),
        "skipped_valid": len(skipped),
        "generated": len(generated),
        "source_commit_sha": run_identity["source_commit_sha"],
        "branch": run_identity["branch"],
        "contract_sha256": run_identity["contract_sha256"],
        "adapter_tree_identity_sha256": run_identity[
            "adapter_tree_identity_sha256"],
        "cohort_manifest_sha256": run_identity["cohort_manifest_sha256"],
        "quixbugs_revision": run_identity["quixbugs_revision"],
        "protocol_identity": run_identity["protocol_identity"],
        "run_identity_sha256": run_identity["run_identity_sha256"],
        "campaign_elapsed_seconds": round(elapsed, 3),
        "campaign_timing": (
            "observational stop-policy limit; no hard enforcement mechanism"
        ),
        "completed_at": datetime.now().astimezone().isoformat(),
    }
    atomic_write_text(out_dir / "S4_GENERATION_COMPLETE.json",
        json.dumps(marker, indent=2, ensure_ascii=False))
    logger.log(
        f"GENERATE complete: valid={len(skipped) + len(generated)}/40 "
        f"(skipped={len(skipped)}, generated={len(generated)})"
    )



def all_pairs_valid(
    out_dir: Path, tasks: List[Dict[str, Any]], *,
    expected_run_identity_sha256: str,
) -> Tuple[bool, List[str]]:
    invalid: List[str] = []
    for task in tasks:
        valid, reason = pair_is_valid(
            out_dir, task,
            expected_run_identity_sha256=expected_run_identity_sha256,
        )
        if not valid:
            invalid.append(f"{task['task_id']}({reason})")
    return not invalid, invalid


def verify_eval_source_state(
    g: Dict[str, Any], run_identity: Dict[str, Any],
    contract: Dict[str, Any],
) -> None:
    """Fail closed unless the generation's run identity matches the CURRENT
    authorized source/contract state AND the tracked source tree is clean
    (Repair Pass 2, Blocker 4)."""

    if g["tracked_dirty"]:
        raise RunnerError(
            "tracked working tree is dirty; eval requires the clean "
            "committed source state (owner source-freeze commit)"
        )
    if run_identity.get("source_commit_sha") != g["head_sha"]:
        raise RunnerError(
            "generation source_commit_sha "
            f"{run_identity.get('source_commit_sha')} != current HEAD "
            f"{g['head_sha']}; eval is bound to the generation source state"
        )
    if run_identity.get("contract_sha256") != contract["contract_sha256"]:
        raise RunnerError(
            "generation contract_sha256 "
            f"{run_identity.get('contract_sha256')} != current contract "
            f"{contract['contract_sha256']}"
        )
    expected_tree = contract["model"]["adapter_identity"][
        "tree_identity_sha256"]
    for key, expected in (
        ("adapter_tree_identity_sha256", expected_tree),
        ("cohort_manifest_sha256", MANIFEST_SHA256),
        ("quixbugs_revision", QUIXBUGS_REVISION),
        ("protocol_identity", protocol_identity()),
    ):
        if run_identity.get(key) != expected:
            raise RunnerError(
                f"run identity {key} {run_identity.get(key)} != frozen "
                f"expected {expected}"
            )


# ---------------------------------------------------------------------------
# eval (Blocker 2: bound to the same source-freeze identity)
# ---------------------------------------------------------------------------


def eval_stage(args: argparse.Namespace, logger: RunLogger) -> None:
    logger.log("EVAL stage (frozen v1.2.1 semantics)")
    out_dir = Path(args.output_dir)
    contract = load_contract()
    g = git_state()
    tasks = validate_manifest(REPO_ROOT / MANIFEST_PATH)

    # Fail closed unless the generation is bound to the CURRENT authorized
    # source/contract state AND the tracked source tree is clean.
    identity_path = out_dir / "run-identity.json"
    if not identity_path.is_file():
        raise RunnerError("run-identity.json missing; cannot bind eval to "
                          "the generation source state")
    run_identity = json.loads(identity_path.read_text(encoding="utf-8"))
    verify_eval_source_state(g, run_identity, contract)
    logger.log(
        f"EVAL IDENTITY OK: source_commit_sha={g['head_sha']} "
        f"contract_sha256={contract['contract_sha256'][:12]}..."
    )

    marker = completion_marker(out_dir)
    if marker.get("valid_pairs") != len(tasks):
        raise RunnerError(
            f"generation marker valid_pairs={marker.get('valid_pairs')} "
            f"!= {len(tasks)}"
        )
    all_ok, invalid = all_pairs_valid(
        out_dir, tasks,
        expected_run_identity_sha256=run_identity.get("run_identity_sha256"),
    )
    if not all_ok:
        raise RunnerError(f"pair validation failed: {invalid[:10]}")

    from experiments.cp118_rag_definitive.s4_eval import run_s4_eval

    result = run_s4_eval(
        run_dir=out_dir,
        raw_dir=out_dir / "raw",
        meta_dir=out_dir / "meta",
        manifest=tasks,
        model_id=MODEL_ID,
        logger=logger,
        quixbugs_root=Path(args.quixbugs_root) if args.quixbugs_root else (
            REPO_ROOT / "tmp" / "s4"
        ),
    )
    evidence = {
        "schema_version": "s4-cp118-rag-definitive-evidence",
        "runner_version": RUNNER_VERSION,
        "git": g,
        "contract": contract,
        "run_identity": run_identity,
        "summary": result["summary"],
        "p2p": result["p2p"],
        "p2p_reason": result["p2p_reason"],
        "generation_marker": marker,
        "evaluated_pairs": len(result["rows"]),
        "completed_at": datetime.now().astimezone().isoformat(),
    }
    atomic_write_text(out_dir / "evidence.json",
        json.dumps(evidence, indent=2, ensure_ascii=False))
    _write_run_summary(out_dir, evidence)
    _write_sha256sums(out_dir)
    logger.log("EVAL complete")


# ---------------------------------------------------------------------------
# offline evaluator smoke (engineering validation only)
# ---------------------------------------------------------------------------


def smoke_eval_stage(args: argparse.Namespace, logger: RunLogger) -> None:
    """Bounded OFFLINE evaluator smoke: one real frozen QuixBugs task with a
    canned non-model candidate through the real s4_eval orchestration and
    the real run_cmd interface.  No model inference; engineering validation
    only, not experimental evidence."""

    logger.log("SMOKE-EVAL stage (offline, canned candidate, no model)")
    tasks = validate_manifest(REPO_ROOT / MANIFEST_PATH)
    task = next(t for t in tasks if t["task_id"] == "quixbugs-gcd")
    out_dir = Path(args.output_dir)
    raw_dir = out_dir / "raw"
    meta_dir = out_dir / "meta"
    raw_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    from experiments.cp118_rag_definitive.s4_eval import (
        _load_frozen_eval,
        run_s4_eval,
    )

    # Frozen evaluator must load (hash-pinned).
    ev = _load_frozen_eval()

    # Canned candidate: a real unified diff repairing the frozen gcd bug,
    # wrapped in the frozen v1.2.1 output contract.  The checkout is reset
    # to the pristine frozen revision before and after, so the shared
    # worktree is never left modified by the smoke.
    repo = ensure_quixbugs_repo(
        Path(args.quixbugs_root) if args.quixbugs_root else (
            REPO_ROOT / "tmp" / "s4"),
        logger=logger,
    )
    from experiments.cp118_rag_definitive.s4_quixbugs import (
        QUIXBUGS_REVISION as _REV,
        run_cmd as _rc,
    )
    _rc(["git", "reset", "--hard", _REV], cwd=str(repo), check=True)
    _rc(["git", "clean", "-fdx"], cwd=str(repo), check=True)
    target = repo / task["target_file"]
    buggy = target.read_text(encoding="utf-8")
    fixed = buggy.replace(
        "        return gcd(a % b, b)\n",
        "        return gcd(b, a % b)\n",
    )
    if fixed == buggy:
        raise RunnerError("smoke: canned fix did not match the frozen gcd "
                          "source; refusing to invent one")
    import difflib

    diff = "".join(difflib.unified_diff(
        buggy.splitlines(keepends=True),
        fixed.splitlines(keepends=True),
        fromfile=f"a/{task['target_file']}",
        tofile=f"b/{task['target_file']}",
        lineterm="\n",
    ))
    raw_text = (
        "PATCH\n" + diff + "\n"
        "FILES\n" + task["target_file"] + "\n"
        "SYMBOLS\n" + task["target_file"] + "::gcd\n"
        "ROOT_CAUSE\nOff-by-one swap in the Euclidean loop condition; "
        "the loop must continue while b != 0.\n"
    )
    stem = f"{_slug(MODEL_ID)}__{task['task_id']}"
    atomic_write_text(raw_dir / f"{stem}.txt", raw_text)
    atomic_write_text(meta_dir / f"{stem}.json", json.dumps({
        "protocol_version": PROTOCOL_VERSION,
        "model": MODEL_ID,
        "model_revision": BASE_REVISION,
        "task_id": task["task_id"],
        "rank": int(task["slot"]),
        "source_v1_1_payload_sha256": task["payload_sha256"],
        "max_new_tokens": MAX_NEW_TOKENS,
        "raw_sha256": sha256_text(raw_text),
        "prompt_tokens": 0,
        "output_tokens": len(raw_text.split()),
        "generation_latency_s": 0.0,
        "peak_allocated_gib": 0.0,
    }, indent=2))

    result = run_s4_eval(
        run_dir=out_dir,
        raw_dir=raw_dir,
        meta_dir=meta_dir,
        manifest=[task],
        model_id=MODEL_ID,
        logger=logger,
        quixbugs_root=Path(args.quixbugs_root) if args.quixbugs_root else (
            REPO_ROOT / "tmp" / "s4"),
    )
    row = result["rows"][0]
    summary = result["summary"]
    logger.log(
        f"SMOKE row: patch_apply={row['patch_apply']} "
        f"test_pass={row['test_pass']} stage={row['semantic_failure_stage']} "
        f"summary_resolved={summary['supplied_oracle_resolved_count']}"
    )
    # The canned repair must demonstrate the full pipeline: apply + visible
    # pytest + resolved row.
    if not (row["patch_apply"] and row["test_pass"]):
        raise RunnerError(
            f"smoke did not exercise the resolved path: {row}"
        )
    # Restore the shared checkout to the pristine frozen revision (the
    # frozen eval flow leaves the last applied patch in the tree).
    _rc(["git", "reset", "--hard", _REV], cwd=str(repo), check=True)
    _rc(["git", "clean", "-fdx"], cwd=str(repo), check=True)
    logger.log("SMOKE-EVAL PASS (canned candidate resolved through the "
               "frozen evaluator)")


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------


def _write_run_summary(out_dir: Path, evidence: Dict[str, Any]) -> None:
    g = evidence["git"]
    ri = evidence["run_identity"]
    lines = [
        "# S4 — Definitive cp118 + Frozen RAG Treatment — Run Summary",
        "",
        f"- Experiment: `{evidence['schema_version']}`",
        f"- Runner: `{evidence['runner_version']}`",
        f"- Branch: `{g['branch']}`  HEAD: `{g['head_sha']}`",
        f"- Run identity sha256: `{ri['run_identity_sha256']}`",
        f"- Contract sha256: `{ri['contract_sha256']}`",
        f"- Model condition: `{ri['model_condition']}`",
        f"- Adapter tree identity: `{ri['adapter_tree_identity_sha256']}`",
        f"- Cohort manifest: `{ri['cohort_manifest_sha256']}`",
        f"- QuixBugs revision: `{ri['quixbugs_revision']}`",
        f"- Protocol identity: `{ri['protocol_identity']}`",
        f"- RAG: ON (repo mode, frozen)",
        "",
        "## Primary metrics (frozen v1.2.1 CPU eval semantics)",
        "",
        f"- Strict compliance: {evidence['summary']['strict_compliance_count']}/40",
        f"- Semantic extraction: {evidence['summary']['semantic_extraction_count']}/40",
        f"- Patch apply: {evidence['summary']['patch_apply_count']}/40",
        f"- Supplied-oracle resolved (test_pass): {evidence['summary']['supplied_oracle_resolved_count']}/40",
        f"- Truncation: {evidence['summary']['truncation_count']}/40",
        f"- P2P: {evidence['p2p']} ({evidence['p2p_reason']})",
        "",
        "## RAG provenance",
        "",
        "Per-task retrieval/context identities, budgets and provenance are in "
        "`retrieval/` and the generation `meta/` records.",
        "",
    ]
    atomic_write_text(out_dir / "RUN_SUMMARY.md", "\n".join(lines))


def _write_sha256sums(out_dir: Path) -> None:
    sums = []
    for path in sorted(out_dir.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rel = path.relative_to(out_dir).as_posix()
            sums.append(f"{digest}  {rel}")
    atomic_write_text(out_dir / "SHA256SUMS.txt", "\n".join(sums) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="S4 cp118 + frozen RAG treatment")
    sub = ap.add_subparsers(dest="stage", required=True)

    for name, help_text in (
        ("validate", "offline qualification (no model, no inference)"),
        ("generate", "live one-shot cp118+RAG generation (40 tasks)"),
        ("eval", "frozen v1.2.1 CPU evaluation of generated pairs"),
        ("smoke-eval", "offline evaluator smoke (canned candidate, no model)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "--adapter-path",
            default=os.environ.get("AGENTIC_DEBUGGER_CP118_ADAPTER"),
            help=(
                "path to the accepted cp118 adapter; defaults to the "
                "AGENTIC_DEBUGGER_CP118_ADAPTER environment variable"
            ),
        )
        p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR / "run-1"))
        p.add_argument("--quixbugs-root", default=None,
                       help="existing QuixBugs checkout parent dir "
                            "(cloned into tmp/s4 by default)")
        p.add_argument("--count-tokens", action="store_true",
                       help="validate: exact tokenizer token counts "
                            "(CPU tokenizer only, no model)")

    args = ap.parse_args(argv)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = RunLogger(out_dir / "runner_stdout.log")
    if args.stage == "validate":
        validate_stage(args, logger)
    elif args.stage == "generate":
        generate_stage(args, logger)
    elif args.stage == "eval":
        eval_stage(args, logger)
    elif args.stage == "smoke-eval":
        smoke_eval_stage(args, logger)
    else:
        raise RunnerError(f"unknown stage {args.stage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
