#!/usr/bin/env python3
"""R6 — build the SFT dataset from successful scripted trajectories.

For every SUCCESSFUL trajectory (gate_patch passed, verifier RESOLVED),
extract the per-turn supervision pairs exactly as the live model saw them:

  system_prompt  = bridge.build_system_prompt(module_path) — hash-verified
                   against telemetry.system_prompt_sha256
  user_prompt    = telemetry[*].request.user_prompt_full (exact)
  completion     = telemetry[*].raw_response_text (exact accepted directive)

Only accepted directives are used (the scripted transport is never
rejected).  Train/validation are task-disjoint by the frozen split manifest.
The five R6 curated holdouts are structurally absent.

Outputs (experiments/r6_debugger_training/sft/):
  sft_train.jsonl / sft_validation.jsonl   raw (prompt, completion) pairs
  sft_manifest.json                        provenance + token statistics
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.debugger_interaction_v2_r5 import bridge as r5_bridge  # noqa: E402
from experiments.debugger_interaction_v2_r5.launcher import (  # noqa: E402
    task_target_module_path,
)
from experiments.r6_debugger_training.quixbugs_tasks import (  # noqa: E402
    CURATED_HOLDOUT_IDS,
    SPLIT_SEED,
)

EXPERIMENT_DIR = THIS_FILE.parent
SPLIT_MANIFEST = EXPERIMENT_DIR / "split_manifest.json"
TRAJECTORY_ROOT = EXPERIMENT_DIR / "runs" / "trajectories-v1"
SFT_OUTPUT_DIR = EXPERIMENT_DIR / "sft"

SCHEMA_VERSION = "r6-debugger-sft-v1"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_turns(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-turn (system, user, completion) from one accepted trajectory."""
    task_meta = evidence.get("task") or {}
    module_path = task_meta.get("module_path")
    if not module_path:
        raise ValueError("evidence has no module_path")
    system_prompt = r5_bridge.build_system_prompt(module_path)
    system_sha = sha256(system_prompt)
    turns: list[dict[str, Any]] = []
    for record in evidence.get("telemetry") or []:
        parse_result = record.get("parse_result") or {}
        if parse_result.get("status") != "accepted":
            continue
        request = record.get("request") or {}
        user_prompt = request.get("user_prompt_full")
        raw = record.get("raw_response_text")
        if type(user_prompt) is not str or not user_prompt:
            continue
        if type(raw) is not str or not raw.strip():
            continue
        if request.get("system_prompt_sha256") != system_sha:
            raise ValueError(
                "telemetry system_prompt_sha256 does not match reconstructed "
                f"build_system_prompt({module_path!r})"
            )
        turns.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "completion": raw,
            "controller_state": record.get("controller_state"),
        })
    return turns


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build R6 debugger SFT data")
    parser.add_argument("--trajectory-root", type=Path, default=TRAJECTORY_ROOT)
    parser.add_argument("--feedback-trajectory-root", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=SFT_OUTPUT_DIR)
    parser.add_argument("--breakpoint-repeat", type=int, default=1)
    parser.add_argument("--patch-repeat", type=int, default=1)
    args = parser.parse_args()
    if args.breakpoint_repeat < 1 or args.patch_repeat < 1:
        parser.error("phase repeat counts must be >= 1")

    if not SPLIT_MANIFEST.is_file():
        print(f"split manifest missing: {SPLIT_MANIFEST}")
        return 1
    split = json.loads(SPLIT_MANIFEST.read_text(encoding="utf-8"))
    train_ids = {e["task_id"] for e in split["train_tasks"]}
    validation_ids = {e["task_id"] for e in split["validation_tasks"]}
    if train_ids & validation_ids:
        print("split overlap — aborting")
        return 1

    trajectory_root = args.trajectory_root.resolve()
    output_dir = args.output_dir.resolve()
    gen_summary_path = trajectory_root / "generation_summary.json"
    if not gen_summary_path.is_file():
        print(f"generation summary missing: {gen_summary_path}")
        return 1
    gen_summary = json.loads(gen_summary_path.read_text(encoding="utf-8"))
    results = gen_summary.get("results") or {}
    successful = {
        task_id: r for task_id, r in results.items() if r.get("success")
    }
    print(f"successful trajectories: {len(successful)}/{len(results)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    train_rows: list[dict[str, Any]] = []
    validation_rows: list[dict[str, Any]] = []
    per_task_counts: dict[str, dict[str, int]] = {}

    for task_id, result in sorted(successful.items()):
        if task_id in CURATED_HOLDOUT_IDS:
            raise RuntimeError(f"holdout task present in training data: {task_id}")
        evidence = json.loads(
            (trajectory_root / task_id / "evidence.json").read_text(encoding="utf-8")
        )
        turns = extract_turns(evidence)
        if task_id in train_ids:
            rows = train_rows
        elif task_id in validation_ids:
            rows = validation_rows
        else:
            raise RuntimeError(f"task {task_id} is in neither split")
        for turn in turns:
            repeat = 1
            if task_id in train_ids and turn["completion"].startswith("break "):
                repeat = args.breakpoint_repeat
            elif task_id in train_ids and turn["controller_state"] == "Patch":
                repeat = args.patch_repeat
            for _ in range(repeat):
                rows.append({
                    "task_id": task_id,
                    "algo": result["algo"],
                    "controller_state": turn["controller_state"],
                    "system_prompt": turn["system_prompt"],
                    "user_prompt": turn["user_prompt"],
                    "completion": turn["completion"],
                })
        per_task_counts[task_id] = {
            "turns": len(turns),
            "algo": result["algo"],
        }

    def _write(path: Path, rows: list[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")

    recovery_examples = 0
    feedback_root = args.feedback_trajectory_root
    if feedback_root is not None:
        feedback_root = feedback_root.resolve()
        feedback_summary = json.loads(
            (feedback_root / "generation_summary.json").read_text(encoding="utf-8")
        )
        feedback_results = feedback_summary.get("results") or {}
        if set(feedback_results) != train_ids:
            raise RuntimeError(
                "feedback recovery trajectories must exactly cover the train split"
            )
        if feedback_summary.get("feedback_recovery") is not True:
            raise RuntimeError("feedback trajectories are not recovery-mode evidence")
        for task_id in sorted(train_ids):
            if (feedback_results[task_id] or {}).get("success") is not True:
                raise RuntimeError(f"{task_id}: feedback recovery trajectory failed")
            evidence = json.loads(
                (feedback_root / task_id / "evidence.json").read_text(encoding="utf-8")
            )
            patch_turns = [
                turn
                for turn in extract_turns(evidence)
                if turn["controller_state"] == "Patch"
            ]
            if len(patch_turns) < 2:
                raise RuntimeError(f"{task_id}: no verifier-feedback recovery turn")
            recovery = patch_turns[-1]
            train_rows.append({
                "task_id": task_id,
                "algo": feedback_results[task_id]["algo"],
                "controller_state": recovery["controller_state"],
                "system_prompt": recovery["system_prompt"],
                "user_prompt": recovery["user_prompt"],
                "completion": recovery["completion"],
            })
            recovery_examples += 1

    train_path = output_dir / "sft_train.jsonl"
    validation_path = output_dir / "sft_validation.jsonl"
    _write(train_path, train_rows)
    _write(validation_path, validation_rows)

    # Token statistics with the pinned Qwen2.5 tokenizer (completion-only).
    token_stats: dict[str, Any] = {"train": {}, "validation": {}}
    try:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(
            "Qwen/Qwen2.5-Coder-7B-Instruct",
            revision="c03e6d358207e414f1eca0bb1891e29f1db0e242",
            trust_remote_code=False,
        )

        def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
            prompt_tokens: list[int] = []
            completion_tokens: list[int] = []
            total_tokens: list[int] = []
            for row in rows:
                rendered = tokenizer.apply_chat_template(
                    [{"role": "system", "content": row["system_prompt"]},
                     {"role": "user", "content": row["user_prompt"]}],
                    tokenize=True, add_generation_prompt=True,
                )
                completion = tokenizer(row["completion"], add_special_tokens=False)["input_ids"]
                prompt_tokens.append(len(rendered))
                completion_tokens.append(len(completion))
                total_tokens.append(len(rendered) + len(completion))
            return {
                "examples": len(rows),
                "prompt_tokens_max": max(prompt_tokens) if prompt_tokens else 0,
                "completion_tokens_max": max(completion_tokens) if completion_tokens else 0,
                "total_tokens_max": max(total_tokens) if total_tokens else 0,
                "total_tokens_sum": sum(total_tokens),
            }

        token_stats["train"] = _stats(train_rows)
        token_stats["validation"] = _stats(validation_rows)
    except Exception as exc:
        token_stats["error"] = f"{type(exc).__name__}: {exc}"

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "split_seed": SPLIT_SEED,
        "split_manifest": str(SPLIT_MANIFEST),
        "trajectory_root": str(trajectory_root),
        "feedback_trajectory_root": str(feedback_root) if feedback_root else None,
        "train_examples": len(train_rows),
        "validation_examples": len(validation_rows),
        "train_tasks": sorted(train_ids & set(per_task_counts)),
        "validation_tasks": sorted(validation_ids & set(per_task_counts)),
        "per_task_turn_counts": {k: v for k, v in sorted(per_task_counts.items())},
        "token_statistics": token_stats,
        "holdout_excluded": sorted(CURATED_HOLDOUT_IDS),
        "phase_balancing": {
            "breakpoint_repeat": args.breakpoint_repeat,
            "patch_repeat": args.patch_repeat,
            "feedback_recovery_examples": recovery_examples,
        },
        "model_visible_fields": [
            "task title/description (agent_visible_mapping)",
            "original production source",
            "eligible breakpoint lines",
            "real debugger observations (production-region filtered)",
            "sanitized reproduction diagnostic",
            "sanitized verifier feedback",
        ],
        "excluded_oracle_fields": [
            "oracle.bug_category", "oracle.target_files", "oracle.target_symbols",
            "oracle.root_cause_summary", "oracle.runtime_evidence_hint",
            "tests.fail_to_pass/pass_to_pass node ids", "reproduction.argv",
            "gold repair", "corrected source (never in prompts)",
            "hidden test content",
        ],
    }
    (output_dir / "sft_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps({
        "train_examples": len(train_rows),
        "validation_examples": len(validation_rows),
        "train_tasks": manifest["train_tasks"],
        "validation_tasks": manifest["validation_tasks"],
        "token_statistics": token_stats,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
