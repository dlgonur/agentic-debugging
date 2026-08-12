#!/usr/bin/env python3
"""R6 — generate authentic debugger trajectories over QuixBugs training tasks.

For every accepted QuixBugs training/validation task (built by
``quixbugs_tasks.py``), run the frozen r5.9 treatment with the SCRIPTED
perfect-protocol transport:

  reproduce -> break <gold-region line> -> stack -> locals -> next ->
  stack (G2) or production-exception path -> diagnosis (single line) ->
  file <path> + corrected whole-file content -> real PatchManager ->
  independent EvaluationVerifier

All debugger observations, sanitized diagnostics, prompts, and verifier
feedback are REAL (the exact r5.9 bridge/controller/PDB/verifier
machinery); only the model output is scripted.  Successful trajectories
(gate_patch passed, verifier RESOLVED) are the SFT corpus inputs; every
trajectory is recorded, successful or not.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.evaluation.runner import load_task  # noqa: E402
from agentic_debugger.runtime.pdb_session import PdbSession  # noqa: E402
from agentic_debugger.runtime.workspace import TaskWorkspace  # noqa: E402

from experiments.debugger_interaction_v2_r5.bridge import (  # noqa: E402
    breakpoint_eligible_lines,
)
from experiments.debugger_interaction_v2_r5.launcher import (  # noqa: E402
    prepare_r5_probe,
)
from experiments.debugger_interaction_v2_r5.r5_runner import (  # noqa: E402
    R5_BUDGETS,
    run_experiment,
)
from experiments.r6_debugger_training.diagnoses import diagnosis_for  # noqa: E402
from experiments.r6_debugger_training.quixbugs_tasks import (  # noqa: E402
    QUIXBUGS_REVISION,
    ensure_quixbugs_repo,
)
from experiments.r6_debugger_training.scripted_transport import (  # noqa: E402
    ScriptedTrajectoryTransport,
)

CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"
GOLD_DIR = REPO_ROOT / "experiments" / "r6_debugger_training" / "gold"
SPLIT_MANIFEST = (
    REPO_ROOT / "experiments" / "r6_debugger_training" / "split_manifest.json"
)


def changed_original_lines(original: str, corrected: str) -> list[int]:
    """0-based line indices of the original changed by the gold repair."""
    matcher = difflib.SequenceMatcher(
        None, original.splitlines(), corrected.splitlines()
    )
    changed: list[int] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            changed.extend(range(i1, i2))
    return sorted(set(changed))


def breakpoint_candidates(eligible: tuple[int, ...], changed: list[int]) -> list[int]:
    """Eligible lines in the changed region first, then nearest neighbors."""
    if not changed:
        return list(eligible)
    in_region = [line for line in eligible if (line - 1) in changed]
    if in_region:
        return in_region
    nearest = sorted(
        eligible, key=lambda line: min(abs(line - 1 - c) for c in changed)
    )
    return nearest


def find_pausing_breakpoint(
    fixture_dir: Path,
    module_path: str,
    reproduction_argv: list[str],
    task_id: str,
    original_source: str,
    eligible_lines: tuple[int, ...],
    changed_lines: list[int],
    *,
    pdb_timeout: float = 60.0,
    max_candidates: int = 4,
) -> tuple[Optional[int], Optional[dict[str, Any]]]:
    """Try candidate lines with a REAL PDB session; first pause wins."""
    original_sha = hashlib.sha256(original_source.encode("utf-8")).hexdigest()
    original_count = len(original_source.splitlines())
    candidates = breakpoint_candidates(eligible_lines, changed_lines)[:max_candidates]
    with tempfile.TemporaryDirectory(prefix="r6-bpcheck-") as tmp:
        parent = Path(tmp)
        r5_probe = prepare_r5_probe(
            fixture_dir, module_path, reproduction_argv, parent,
            original_source_sha256=original_sha,
            original_source_line_count=original_count,
            eligible_lines=eligible_lines,
            task_id=task_id,
        )
        for line in candidates:
            workspace = TaskWorkspace(str(r5_probe.source_dir), parent_dir=str(parent))
            session = PdbSession(
                workspace, startup_timeout=pdb_timeout,
                request_timeout=pdb_timeout, shutdown_timeout=10.0,
            )
            try:
                session.start()
                result = session.start_paused_target(module_path, [line])
                if (
                    result.get("state") == "paused"
                    and type(result.get("function")) is str
                    and result.get("function") not in ("", "<module>")
                ):
                    return line, result
            except Exception:
                pass
            finally:
                try:
                    session.stop()
                except Exception:
                    pass
                try:
                    workspace.cleanup()
                except Exception:
                    pass
    return None, None


def build_transport(task_id: str, algo: str, module_path: str) -> ScriptedTrajectoryTransport:
    task = load_task(str(CURATED_ROOT / task_id / "task.json"))
    fixture_dir = CURATED_ROOT / task_id
    original_source = (fixture_dir / module_path).read_text(encoding="utf-8")
    corrected_source = (REPO_ROOT / "operator" / "r6-training" / "QuixBugs"
                        / "correct_python_programs" / f"{algo}.py").read_text(
        encoding="utf-8"
    )
    eligible = breakpoint_eligible_lines(original_source)
    changed = changed_original_lines(original_source, corrected_source)
    line, pause = find_pausing_breakpoint(
        fixture_dir, module_path, task.reproduction.argv, task_id,
        original_source, tuple(eligible), changed,
    )
    if line is None:
        raise RuntimeError(f"{task_id}: no pausing breakpoint candidate")
    return (
        task,
        ScriptedTrajectoryTransport(
            module_path=module_path,
            breakpoint_line=line,
            diagnosis_text=diagnosis_for(algo),
            corrected_source=corrected_source,
        ),
        {"breakpoint_line": line, "changed_lines": changed,
         "pause_function": (pause or {}).get("function"),
         "pause_line": (pause or {}).get("line")},
    )


def run_trajectory(
    contract: dict[str, Any],
    task_id: str,
    algo: str,
    module_path: str,
    output_dir: Path,
) -> dict[str, Any]:
    task, transport, bp_meta = build_transport(task_id, algo, module_path)
    pdb_timeout = contract["budgets"]["pdb_request_timeout_seconds"]

    def session_factory(workspace: TaskWorkspace) -> PdbSession:
        return PdbSession(
            workspace, startup_timeout=float(pdb_timeout),
            request_timeout=float(pdb_timeout), shutdown_timeout=2.0,
        )

    evidence = run_experiment(
        contract, transport, output_dir, task_id=task_id,
        pdb_session_factory=session_factory,
    )
    evidence["breakpoint_selection"] = bp_meta
    (output_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    return evidence


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate R6 scripted debugger trajectories over QuixBugs tasks"
    )
    parser.add_argument("--output-root", type=str, default=None,
                        help="trajectory output root (default experiments/r6_debugger_training/runs/trajectories-v1)")
    parser.add_argument("--algo", type=str, default=None,
                        help="single algorithm (debugging aid)")
    args = parser.parse_args()

    if not SPLIT_MANIFEST.is_file():
        print(f"split manifest missing: {SPLIT_MANIFEST} — run quixbugs_tasks.py first")
        return 1
    split = json.loads(SPLIT_MANIFEST.read_text(encoding="utf-8"))
    entries = [
        (e["task_id"], e["algo"]) for e in split["train_tasks"] + split["validation_tasks"]
    ]
    if args.algo:
        entries = [e for e in entries if e[1] == args.algo]
        if not entries:
            print(f"algorithm {args.algo!r} not in split")
            return 1

    output_root = Path(args.output_root) if args.output_root else (
        THIS_FILE.parent / "runs" / "trajectories-v1"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    contract = json.loads(
        (THIS_FILE.parent / "r6_train_contract.json").read_text(encoding="utf-8")
    )

    results: dict[str, Any] = {}
    for task_id, algo in entries:
        case_output = output_root / task_id
        case_output.mkdir(parents=True, exist_ok=True)
        try:
            evidence = run_trajectory(
                contract, task_id, algo, f"{algo}.py", case_output
            )
            gate = evidence.get("gate_results", {}).get("gate_patch") or {}
            verifier = evidence.get("verifier") or {}
            ok = bool(gate.get("passed")) and verifier.get("outcome") == "RESOLVED"
            results[task_id] = {
                "algo": algo,
                "success": ok,
                "verifier_outcome": verifier.get("outcome"),
                "verifier_status": verifier.get("status"),
                "gate_reason": gate.get("reason") or (
                    evidence.get("gate_results", {}).get("gate_chain") or {}
                ).get("reason"),
                "breakpoint_line": evidence.get("breakpoint_selection", {}).get("breakpoint_line"),
                "model_calls": (evidence.get("controller_result") or {}).get("model_calls"),
            }
            print(f"{task_id}: {'OK ' if ok else 'FAIL'} "
                  f"bp={results[task_id]['breakpoint_line']} "
                  f"verifier={verifier.get('status')}/{verifier.get('outcome')}")
        except Exception as exc:
            results[task_id] = {"algo": algo, "success": False,
                                "error": f"{type(exc).__name__}: {exc}"}
            print(f"{task_id}: ERROR {type(exc).__name__}: {exc}")

    summary = {
        "schema_version": "r6-trajectory-generation-v1",
        "quixbugs_revision": QUIXBUGS_REVISION,
        "split_manifest": str(SPLIT_MANIFEST),
        "contract_sha256": hashlib.sha256(
            json.dumps(contract, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "results": results,
        "success_count": sum(1 for r in results.values() if r.get("success")),
        "total_count": len(results),
    }
    (output_root / "generation_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps({"success": summary["success_count"],
                      "total": summary["total_count"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
