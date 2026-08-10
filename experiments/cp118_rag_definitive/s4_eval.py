"""S4 — primary evaluation with the exact frozen C9 v1.2.1 semantics.

Amendment 3: the primary cp118+RAG result is computed by the exact accepted
C9/D7 CPU evaluation functions (``strict_compliance``,
``extract_patch_semantic_v121``, ``parse_files_independent``,
``parse_symbols_independent``, ``symbol_ground_truth``,
``canonicalize_transcript``) imported at runtime from the frozen
``RAW_C9_5Model_40Task_Protocol_v1_2_1_CPU_EVAL_v1.py`` (stdlib-only
module; the frozen source file is never modified).  The row construction,
patch-apply flow (``git reset --hard``/``git clean`` + ``git apply
--check``/``git apply`` + pytest on the visible test), truncation rule
(``output_tokens >= 4096``) and the ``semantic_failure_stage`` taxonomy
mirror ``evaluate()`` for the single cp118+RAG model condition.

The frozen evaluator's 5-model orchestration (200 rows, Colab-only
``/content`` clone root) is not reproducible for a one-condition run; the
D7 accepted precedent evaluated the cp118 condition with the same frozen
functions under its own orchestration.  The S4 orchestration keeps every
function-level computation identical and adapts only: (a) the model loop is
the single cp118 condition; (b) the QuixBugs worktree root is a local
disposable checkout instead of ``/content``; (c) the oracle sanity gate
(``pytest`` buggy-fails / ``pytest --correct`` passes) is preserved.

Subprocess contract: this module consumes ``s4_quixbugs.run_cmd`` which
returns ``subprocess.CompletedProcess`` (the frozen script's own helper
returns ``(returncode, merged_output)``; here ``returncode`` is read from
the process object and ``stdout + stderr`` is used where the frozen path
used merged output — the evaluation semantics are identical).

Frozen-source binding: before importing the frozen evaluator, its file
SHA-256 is verified against the S4 contract
(``protocol.frozen_sources.cpu_eval_script_sha256``) and the import fails
closed on any drift.

F2P/P2P: the frozen evaluator records ``test_pass`` (the supplied-oracle
designated visible test) which is the fail-to-pass basis of the accepted
RESOLVED metric.  Pass-to-pass was never part of the frozen v1.2.1
evaluator → reported as NOT_RECORDED for the primary S4 condition.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from experiments.cp118_rag_definitive.s4_quixbugs import (
    QUIXBUGS_REVISION,
    ensure_quixbugs_repo,
    run_cmd,
)

#: The frozen CPU evaluator script (never modified; hash-pinned via the
#: contract before import).
FROZEN_EVAL_SCRIPT = (
    "experiments/raw-pilot-v1.1/scripts/"
    "RAW_C9_5Model_40Task_Protocol_v1_2_1_CPU_EVAL_v1.py"
)

CONTRACT_PATH = Path(__file__).resolve().parent / "s4_contract.json"

TEST_TIMEOUT_SECONDS = 10
MAX_NEW_TOKENS = 4096


class EvalError(RuntimeError):
    """Raised when the frozen evaluation semantics cannot be applied."""


def _contract_frozen_source_sha256(key: str) -> str:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    try:
        return contract["protocol"]["frozen_sources"][key]
    except KeyError as exc:
        raise EvalError(
            f"contract lacks protocol.frozen_sources.{key}"
        ) from exc


def _verify_frozen_script(path: Path, key: str) -> None:
    """Fail closed unless the on-disk frozen source matches the pinned
    contract identity."""

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = _contract_frozen_source_sha256(key)
    if digest != expected:
        raise EvalError(
            f"frozen source drift: {path} sha256 {digest} != pinned "
            f"{expected} (contract protocol.frozen_sources.{key})"
        )


def _load_frozen_eval() -> Any:
    path = Path(FROZEN_EVAL_SCRIPT)
    if not path.is_file():
        raise EvalError(f"frozen CPU evaluator missing: {path}")
    _verify_frozen_script(path, "cpu_eval_script_sha256")
    spec = importlib.util.spec_from_file_location("raw_c9_cpu_eval_v121", path)
    if spec is None or spec.loader is None:
        raise EvalError(f"cannot load frozen CPU evaluator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _oracle_sanity(repo: Path, manifest: List[Dict[str, Any]], logger=None):
    """Mirror of the frozen ``ensure_quixbugs_repo`` oracle sanity gate:
    for every task the buggy visible test must fail and the ``--correct``
    run must pass at the frozen revision."""

    for i, task in enumerate(manifest, 1):
        run_cmd(["git", "reset", "--hard", QUIXBUGS_REVISION], cwd=str(repo),
                check=True)
        run_cmd(["git", "clean", "-fdx"], cwd=str(repo), check=True)
        test_rel = task["visible_test_path"]
        proc_bug = run_cmd(
            [sys.executable, "-m", "pytest", "-q",
             f"--timeout={TEST_TIMEOUT_SECONDS}", test_rel],
            cwd=str(repo), timeout=180,
        )
        proc_fix = run_cmd(
            [sys.executable, "-m", "pytest", "-q", "--correct",
             f"--timeout={TEST_TIMEOUT_SECONDS}", test_rel],
            cwd=str(repo), timeout=180,
        )
        if proc_bug.returncode == 0 or proc_fix.returncode != 0:
            raise EvalError(
                f"Oracle sanity failed {task['task_id']}: "
                f"buggy_rc={proc_bug.returncode}, correct_rc={proc_fix.returncode}"
            )
        if logger and (i % 10 == 0 or i == 40):
            logger.log(f"VERIFY oracle sanity: {i}/{len(manifest)} PASS")


def run_s4_eval(
    *,
    run_dir: Path,
    raw_dir: Path,
    meta_dir: Path,
    manifest: List[Dict[str, Any]],
    model_id: str,
    logger=None,
    quixbugs_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Primary S4 evaluation: frozen v1.2.1 semantics for the single
    cp118+RAG condition.  Writes details.csv / summary.csv /
    failure_taxonomy_counts.csv and returns the rows + summary."""

    ev = _load_frozen_eval()
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", model_id)

    repo = ensure_quixbugs_repo(
        quixbugs_root or (run_dir.parent / "work"), logger=logger
    )
    if logger:
        logger.log("EVAL oracle sanity: full checkout at frozen revision")
    _oracle_sanity(repo, manifest, logger=logger)

    rows: List[Dict[str, Any]] = []
    for task in manifest:
        stem = f"{slug}__{task['task_id']}"
        text = (raw_dir / f"{stem}.txt").read_text(encoding="utf-8")
        meta = json.loads((meta_dir / f"{stem}.json").read_text(encoding="utf-8"))
        comp = ev.strict_compliance(text)
        sem = ev.extract_patch_semantic_v121(text)
        patch = sem["normalized_patch"]
        predicted_files, files_parseable, files_reason = ev.parse_files_independent(text)
        predicted_symbols, symbols_parseable, symbols_reason = ev.parse_symbols_independent(text)
        expected_symbol = ev.symbol_ground_truth(repo, task)
        symbol_measurable = expected_symbol is not None
        trunc = int(meta.get("output_tokens", 0)) >= MAX_NEW_TOKENS
        row: Dict[str, Any] = {
            "model": model_id,
            "task_id": task["task_id"],
            "rank": int(task["slot"]),
            "program": task.get("program"),
            "target_file": task["target_file"],
            "strict_compliant": comp["strict_compliant"],
            "compliance_violations": json.dumps(comp["compliance_violations"]),
            "fence_present": comp["fence_present"],
            "root_cause_words": comp["root_cause_words"],
            "patch_marker_count": sem["patch_marker_count"],
            "patch_section_nonempty": sem["patch_section_nonempty"],
            "recognizable_diff": sem["recognizable_diff"],
            "semantic_patch_extracted": patch is not None,
            "evaluator_loss": bool(sem["recognizable_diff"] and patch is None),
            "patch_kind": sem["patch_kind"],
            "extraction_route": sem["extraction_route"],
            "normalizations_applied": json.dumps(sem["normalizations"]),
            "semantic_extraction_reason": sem["reason"],
            "patch_files": json.dumps(sem["patch_files"]),
            "predicted_files": json.dumps(predicted_files),
            "files_section_parseable": files_parseable,
            "files_parse_reason": files_reason,
            "target_file_localized": task["target_file"] in predicted_files,
            "predicted_symbols": json.dumps(predicted_symbols),
            "symbols_section_parseable": symbols_parseable,
            "symbols_parse_reason": symbols_reason,
            "target_symbol": expected_symbol or "",
            "symbol_ground_truth_measurable": symbol_measurable,
            "target_symbol_localized": bool(symbol_measurable and expected_symbol in predicted_symbols),
            "truncated_at_budget": trunc,
            "patch_apply": False,
            "test_pass": False,
            "semantic_failure_stage": "no_semantic_patch",
            "verifier_output": "",
            "prompt_tokens": int(meta.get("prompt_tokens", 0)),
            "output_tokens": int(meta.get("output_tokens", 0)),
            "generation_latency_s": float(meta.get("generation_latency_s", 0.0)),
            "peak_allocated_gib": float(meta.get("peak_allocated_gib", 0.0)),
        }
        if patch is not None:
            touched = set(sem["patch_files"])
            allowed = set(task["candidate_files"])
            if not touched or not touched.issubset(allowed):
                row["semantic_failure_stage"] = "patch_touches_unexposed_or_no_file"
            else:
                run_cmd(["git", "reset", "--hard", QUIXBUGS_REVISION], cwd=str(repo),
                        check=True)
                run_cmd(["git", "clean", "-fdx"], cwd=str(repo), check=True)
                patch_file = repo.parent / "model.patch"
                # LF-explicit: Path.write_text on Windows would translate
                # \n -> \r\n and break git apply (the frozen flow ran on
                # Linux/Colab).
                from experiments.cp118_rag_definitive.s4_payload import (
                    atomic_write_text,
                )

                atomic_write_text(patch_file, patch)
                proc_check = run_cmd(["git", "apply", "--check", str(patch_file)],
                                     cwd=str(repo))
                if proc_check.returncode != 0:
                    row["semantic_failure_stage"] = "patch_apply_failed"
                    row["verifier_output"] = ev.canonicalize_transcript(
                        proc_check.stdout + proc_check.stderr, repo)[-4000:]
                else:
                    proc_apply = run_cmd(["git", "apply", str(patch_file)],
                                         cwd=str(repo))
                    if proc_apply.returncode != 0:
                        row["semantic_failure_stage"] = "patch_apply_failed"
                        row["verifier_output"] = ev.canonicalize_transcript(
                            proc_apply.stdout + proc_apply.stderr, repo)[-4000:]
                    else:
                        row["patch_apply"] = True
                        proc_test = run_cmd(
                            [sys.executable, "-m", "pytest", "-q",
                             f"--timeout={TEST_TIMEOUT_SECONDS}",
                             task["visible_test_path"]],
                            cwd=str(repo), timeout=180,
                        )
                        row["test_pass"] = proc_test.returncode == 0
                        row["semantic_failure_stage"] = (
                            "resolved_supplied_oracle"
                            if proc_test.returncode == 0
                            else "designated_test_failed"
                        )
                        row["verifier_output"] = ev.canonicalize_transcript(
                            proc_test.stdout + proc_test.stderr, repo)[-4000:]
        rows.append(row)

    if len(rows) != len(manifest):
        raise EvalError(f"expected {len(manifest)} rows, got {len(rows)}")

    summary = _summarize_one(rows, model_id, ev)
    semantic, compliance = _taxonomy_one(rows, ev)
    _write_csv(run_dir / "details.csv", rows)
    _write_csv(run_dir / "summary.csv", [summary])
    _write_csv(run_dir / "failure_taxonomy_counts.csv", semantic + compliance)
    return {
        "rows": rows,
        "summary": summary,
        "taxonomy": {"semantic": semantic, "compliance": compliance},
        "p2p": "NOT_RECORDED",
        "p2p_reason": (
            "the frozen v1.2.1 CPU evaluator never ran pass-to-pass; "
            "test_pass is the supplied-oracle fail-to-pass basis"
        ),
    }


def _summarize_one(rows: List[Dict[str, Any]], model_id: str, ev: Any) -> Dict[str, Any]:
    n = len(rows)
    sym = [r for r in rows if r["symbol_ground_truth_measurable"]]
    return {
        "model": model_id,
        "n": n,
        "strict_compliance_count": sum(r["strict_compliant"] for r in rows),
        "strict_compliance_rate": sum(r["strict_compliant"] for r in rows) / n,
        "recognizable_diff_count": sum(r["recognizable_diff"] for r in rows),
        "recognizable_diff_rate": sum(r["recognizable_diff"] for r in rows) / n,
        "semantic_extraction_count": sum(r["semantic_patch_extracted"] for r in rows),
        "semantic_extraction_rate": sum(r["semantic_patch_extracted"] for r in rows) / n,
        "evaluator_loss_count": sum(r["evaluator_loss"] for r in rows),
        "patch_apply_count": sum(r["patch_apply"] for r in rows),
        "patch_apply_rate": sum(r["patch_apply"] for r in rows) / n,
        "supplied_oracle_resolved_count": sum(r["test_pass"] for r in rows),
        "supplied_oracle_resolved_rate": sum(r["test_pass"] for r in rows) / n,
        "file_localization_count": sum(r["target_file_localized"] for r in rows),
        "file_localization_rate": sum(r["target_file_localized"] for r in rows) / n,
        "symbol_localization_measurable_n": len(sym),
        "symbol_localization_count": sum(r["target_symbol_localized"] for r in sym),
        "symbol_localization_rate": (
            sum(r["target_symbol_localized"] for r in sym) / len(sym)
            if sym else None
        ),
        "truncation_count": sum(r["truncated_at_budget"] for r in rows),
        "median_generation_latency_s": ev.median(
            [r["generation_latency_s"] for r in rows]
        ),
        "median_prompt_tokens": ev.median([r["prompt_tokens"] for r in rows]),
        "median_output_tokens": ev.median([r["output_tokens"] for r in rows]),
        "median_peak_allocated_gib": ev.median(
            [r["peak_allocated_gib"] for r in rows]
        ),
        "p2p": "NOT_RECORDED",
    }


def _taxonomy_one(rows: List[Dict[str, Any]], ev: Any):
    from collections import Counter

    sem = Counter(r["semantic_failure_stage"] for r in rows)
    semantic = [
        {"model": rows[0]["model"], "failure_stage": s, "count": c}
        for s, c in sorted(sem.items())
    ]
    comp = Counter()
    for r in rows:
        for v in json.loads(r["compliance_violations"]):
            comp[v] += 1
    compliance = [
        {"model": rows[0]["model"], "violation": v, "count": c}
        for v, c in sorted(comp.items())
    ]
    return semantic, compliance


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
