#!/usr/bin/env python3
"""R6 — executable debugger evaluation of a tuned model on the DISJOINT
QuixBugs validation set (checkpoint selection authority).

Runs the frozen r5.9 treatment with a REAL model transport (base 7B +
PEFT adapter, or the raw base) over the validation tasks, reusing the
accepted r5 runner's per-row metrics: debugger entry, accepted breakpoint,
inspection, step/next, diagnosis, patch produced/applied, verifier
RESOLVED, model calls, tokens.

Checkpoint selection uses ONLY these executable debugger metrics and the
validation eval_loss — never the five R6 curated holdouts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.runtime.pdb_session import PdbSession  # noqa: E402
from agentic_debugger.runtime.workspace import TaskWorkspace  # noqa: E402

from experiments.debugger_interaction_v2_r5.r5_runner import (  # noqa: E402
    _contract_sha256,
    _matrix_row,
    run_experiment,
)
from experiments.debugger_interaction_v2_r5.transport import (  # noqa: E402
    LocalRawQwenTransport,
)
from experiments.debugger_interaction_v2_r5.transport_cp118 import (  # noqa: E402
    LocalQwenPeftTransport,
)

EXPERIMENT_DIR = THIS_FILE.parent
SPLIT_MANIFEST = EXPERIMENT_DIR / "split_manifest.json"
TRAIN_CONTRACT = EXPERIMENT_DIR / "r6_train_contract.json"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="R6 executable debugger evaluation on the QuixBugs validation set"
    )
    parser.add_argument("--adapter-path", type=str, default=None,
                        help="PEFT adapter directory (tuned model)")
    parser.add_argument("--base-only", action="store_true",
                        help="evaluate the RAW base (untuned control)")
    parser.add_argument("--output-dir", type=str, required=True)
    parser.add_argument("--task", type=str, default=None,
                        help="single validation task id (debugging aid)")
    parser.add_argument("--tag", type=str, default="eval",
                        help="output subdirectory tag")
    args = parser.parse_args()

    if args.base_only == (args.adapter_path is not None):
        parser.error("select exactly one of --base-only or --adapter-path")

    if not SPLIT_MANIFEST.is_file():
        print(f"split manifest missing: {SPLIT_MANIFEST}")
        return 1
    split = json.loads(SPLIT_MANIFEST.read_text(encoding="utf-8"))
    validation_entries = split["validation_tasks"]
    if args.task:
        validation_entries = [e for e in validation_entries if e["task_id"] == args.task]
        if not validation_entries:
            print(f"task {args.task!r} not in validation split")
            return 1

    contract = json.loads(TRAIN_CONTRACT.read_text(encoding="utf-8"))
    contract_sha = _contract_sha256(contract)
    pdb_timeout = contract["budgets"]["pdb_request_timeout_seconds"]

    if args.adapter_path is not None:
        adapter_path = Path(args.adapter_path)
        if not (adapter_path / "adapter_config.json").is_file() or not (
            adapter_path / "adapter_model.safetensors"
        ).is_file():
            parser.error("adapter path must contain adapter_config.json + adapter_model.safetensors")
        transport_kwargs: dict[str, Any] = {"adapter_path": str(adapter_path.resolve())}
        transport_factory = lambda: LocalQwenPeftTransport(**transport_kwargs)
        label = f"adapter-{Path(args.adapter_path).name}"
    else:
        transport_factory = LocalRawQwenTransport
        label = "raw-base"

    output_root = Path(args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / label
    run_dir.mkdir(parents=True, exist_ok=True)

    def session_factory(workspace: TaskWorkspace) -> PdbSession:
        return PdbSession(
            workspace, startup_timeout=float(pdb_timeout),
            request_timeout=float(pdb_timeout), shutdown_timeout=2.0,
        )

    rows = []
    transport = transport_factory()
    for entry in validation_entries:
        task_id = entry["task_id"]
        case_output = run_dir / task_id
        case_output.mkdir(parents=True, exist_ok=True)
        try:
            evidence = run_experiment(
                contract, transport, case_output,
                task_id=task_id, pdb_session_factory=session_factory,
            )
            row = _matrix_row(evidence, task_id, contract_sha, contract)
            rows.append(row)
            print(json.dumps({
                "task_id": task_id,
                "verifier": f"{row['verifier_status']}/{row['verifier_outcome']}",
                "first_causal_failure": row["first_causal_failure"],
                "model_calls": row["model_calls"],
            }, ensure_ascii=False))
        except Exception as exc:
            rows.append({
                "task_id": task_id,
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"{task_id}: ERROR {type(exc).__name__}: {exc}")
        finally:
            # Free per-task CUDA memory (the model is loaded once and reused;
            # the PDB sessions/workspaces are cleaned by the runner).
            import gc
            gc.collect()
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass

    aggregate = {
        "tasks_total": len(rows),
        "verifier_resolved": sum(
            1 for r in rows if r.get("verifier_outcome") == "RESOLVED"
        ),
        "debugger_entry": sum(
            1 for r in rows if r.get("first_causal_failure") not in (
                "debugger entrypoint", "reproduction")
        ),
        "accepted_breakpoint": sum(
            1 for r in rows if r.get("breakpoint_line") is not None
        ),
        "inspection": sum(1 for r in rows if r.get("inspection_command")),
        "step_next": sum(1 for r in rows if r.get("step_next_command")),
        "diagnosis": sum(1 for r in rows if r.get("diagnosis_present")),
        "patch_produced": sum(1 for r in rows if r.get("B_sha")),
        "patch_applied": sum(1 for r in rows if r.get("patch_applied")),
        "model_calls_total": sum(r.get("model_calls") or 0 for r in rows),
    }
    report = {
        "schema_version": "r6-debugger-eval-v1",
        "label": label,
        "adapter_path": args.adapter_path,
        "base_only": args.base_only,
        "contract_sha256": contract_sha,
        "rows": rows,
        "aggregate": aggregate,
    }
    (run_dir / "eval_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps({"aggregate": aggregate}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
