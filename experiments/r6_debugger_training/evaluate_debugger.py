#!/usr/bin/env python3
"""R6 checkpoint evaluation on the frozen disjoint QuixBugs validation set.

The process loads one RAW or PEFT model exactly once, proves that every model
module/tensor is resident on one explicit CUDA device, and keeps the model
resident across all selected tasks.  Only task, PDB, verifier, and workspace
state is cleaned between rows.  Crash-durable lifecycle JSONL is fsynced at
every important boundary so a platform failure can be correlated without
guessing or rerunning a dangerous workload.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.runtime.pdb_session import PdbSession  # noqa: E402
from agentic_debugger.runtime.workspace import TaskWorkspace  # noqa: E402
from agentic_debugger.evaluation.professor_trace import export_traces  # noqa: E402

from experiments.debugger_interaction_v2_r5.r5_runner import (  # noqa: E402
    CURATED_ROOT,
    _clean_holdout_5_of_5,
    _contract_sha256,
    _matrix_row,
    run_experiment,
)
from experiments.debugger_interaction_v2_r5.anti_leakage import (  # noqa: E402
    audit_matrix_dir,
)
from experiments.debugger_interaction_v2_r5.transport import (  # noqa: E402
    BASE_REPOSITORY,
    BASE_REVISION,
    LocalRawQwenTransport,
)
from experiments.debugger_interaction_v2_r5.transport_cp118 import (  # noqa: E402
    LocalQwenPeftTransport,
)

EXPERIMENT_DIR = THIS_FILE.parent
SPLIT_MANIFEST = EXPERIMENT_DIR / "split_manifest.json"
EVAL_CONTRACT = EXPERIMENT_DIR / "r6_eval_contract.json"
REPORT_SCHEMA = "r6-debugger-eval-v2"
LIFECYCLE_SCHEMA = "r6-evaluator-lifecycle-v1"
PLACEMENT_FILENAME = "placement_audit.json"
LIFECYCLE_FILENAME = "lifecycle.jsonl"
CURATED_HOLDOUT_IDS = (
    "curated-none-handling-001",
    "curated-off-by-one-002",
    "curated-wrong-branch-003",
    "curated-mutation-alias-004",
    "curated-caller-callee-005",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _local_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _bounded_error(exc: BaseException) -> dict[str, str]:
    message = str(exc).replace("\r", " ").replace("\n", " ")
    return {
        "type": type(exc).__name__,
        "message": message[:1000],
    }


class CrashDurableLifecycleLog:
    """Append-only JSONL whose every event is flushed and fsynced."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._stream = path.open("x", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self._sequence = 0

    def __call__(self, event: str, details: dict[str, Any]) -> None:
        if type(event) is not str or not event:
            raise RuntimeError("lifecycle event name must be a non-empty string")
        if not isinstance(details, dict):
            raise RuntimeError("lifecycle event details must be a mapping")
        with self._lock:
            record = {
                "schema_version": LIFECYCLE_SCHEMA,
                "sequence": self._sequence,
                "wall_time_local": _local_now(),
                "wall_time_utc": _utc_now(),
                "monotonic_ns": time.monotonic_ns(),
                "pid": os.getpid(),
                "event": event,
                "details": details,
            }
            encoded = json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                default=str,
            )
            self._stream.write(encoded + "\n")
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._sequence += 1

    def close(self) -> None:
        with self._lock:
            if not self._stream.closed:
                self._stream.flush()
                os.fsync(self._stream.fileno())
                self._stream.close()


class LifecyclePdbSession(PdbSession):
    """PDB session that emits durable worker and target boundaries."""

    def __init__(
        self,
        workspace: TaskWorkspace,
        *,
        task_id: str,
        lifecycle_event: CrashDurableLifecycleLog,
        startup_timeout: float,
        request_timeout: float,
        shutdown_timeout: float,
    ) -> None:
        self._r6_task_id = task_id
        self._r6_lifecycle_event = lifecycle_event
        self._r6_workspace_name = Path(workspace.root).name
        super().__init__(
            workspace,
            startup_timeout=startup_timeout,
            request_timeout=request_timeout,
            shutdown_timeout=shutdown_timeout,
        )
        self._emit("pdb_session_constructed")

    def _emit(self, event: str, **details: Any) -> None:
        self._r6_lifecycle_event(
            event,
            {
                "task_id": self._r6_task_id,
                "pdb_workspace_name": self._r6_workspace_name,
                **details,
            },
        )

    def start(self) -> None:
        self._emit("pdb_worker_start")
        try:
            super().start()
        except BaseException as exc:  # noqa: BLE001 - preserve boundary
            self._emit("pdb_worker_error", **_bounded_error(exc))
            raise
        self._emit("pdb_worker_ready")

    def start_paused_target(
        self,
        script: str,
        breakpoints: Sequence[int],
        argv: Sequence[str] = (),
    ) -> dict[str, object]:
        self._emit(
            "pdb_target_start",
            script=script,
            breakpoints=list(breakpoints),
            argv=list(argv),
        )
        try:
            result = super().start_paused_target(script, breakpoints, argv)
        except BaseException as exc:  # noqa: BLE001 - preserve boundary
            self._emit("pdb_target_error", **_bounded_error(exc))
            raise
        self._emit(
            "pdb_target_result",
            state=result.get("state"),
            script=result.get("script"),
            line=result.get("line"),
            function=result.get("function"),
        )
        return result

    def stop(self) -> None:
        self._emit("pdb_session_stop_start")
        try:
            super().stop()
        except BaseException as exc:  # noqa: BLE001 - preserve boundary
            self._emit("pdb_session_stop_error", **_bounded_error(exc))
            raise
        self._emit("pdb_session_stop_complete")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _adapter_inference_identity(adapter_path: Path) -> dict[str, Any]:
    files = []
    for name in ("adapter_config.json", "adapter_model.safetensors"):
        path = adapter_path / name
        if not path.is_file():
            raise RuntimeError(f"adapter inference file is missing: {path}")
        files.append({
            "path": name,
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        })
    combined = hashlib.sha256()
    for item in files:
        combined.update(item["path"].encode("utf-8"))
        combined.update(b"\0")
        combined.update(item["sha256"].encode("ascii"))
        combined.update(b"\0")
    return {
        "resolved_path": str(adapter_path.resolve()),
        "inference_tree_sha256": combined.hexdigest(),
        "files": files,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(
                payload,
                stream,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
                default=str,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("cannot resolve the evaluator Git commit")
    return result.stdout.strip()


def _require_child_python_matches_runtime() -> str:
    resolved = shutil.which("python")
    if resolved is None:
        raise RuntimeError("task subprocess command 'python' is not resolvable")
    try:
        matches = os.path.samefile(resolved, sys.executable)
    except OSError:
        matches = (
            os.path.normcase(os.path.abspath(resolved))
            == os.path.normcase(os.path.abspath(sys.executable))
        )
    if not matches:
        raise RuntimeError(
            "task subprocess 'python' does not match the evaluator runtime: "
            f"{resolved!r} != {sys.executable!r}"
        )
    return str(Path(resolved).resolve())


def _row_metrics(evidence: dict[str, Any]) -> dict[str, int]:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    model_runtime_ms = 0
    for record in evidence.get("telemetry") or []:
        usage = record.get("usage") or {}
        timing = record.get("timing") or {}
        for key, accumulator in (
            ("prompt_tokens", "prompt"),
            ("completion_tokens", "completion"),
            ("total_tokens", "total"),
        ):
            value = usage.get(key)
            if type(value) is int:
                if accumulator == "prompt":
                    prompt_tokens += value
                elif accumulator == "completion":
                    completion_tokens += value
                else:
                    total_tokens += value
        duration = timing.get("request_duration_ms")
        if type(duration) is int:
            model_runtime_ms += duration
    runtime = evidence.get("runtime") or {}
    task_runtime_ms = runtime.get("total_duration_ms")
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "model_request_runtime_ms": model_runtime_ms,
        "task_runtime_ms": task_runtime_ms if type(task_runtime_ms) is int else 0,
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tasks_total": len(rows),
        "rows_with_errors": sum(1 for row in rows if row.get("error")),
        "verifier_resolved": sum(
            1 for row in rows if row.get("verifier_outcome") == "RESOLVED"
        ),
        "debugger_entry": sum(
            1
            for row in rows
            if not row.get("error")
            and row.get("first_causal_failure")
            not in ("debugger entrypoint", "reproduction")
        ),
        "accepted_breakpoint": sum(
            1 for row in rows if row.get("breakpoint_line") is not None
        ),
        "inspection": sum(1 for row in rows if row.get("inspection_command")),
        "step_next": sum(1 for row in rows if row.get("step_next_command")),
        "diagnosis": sum(1 for row in rows if row.get("diagnosis_present")),
        "patch_produced": sum(1 for row in rows if row.get("B_sha")),
        "patch_applied": sum(1 for row in rows if row.get("patch_applied")),
        "model_calls_total": sum(row.get("model_calls") or 0 for row in rows),
        "prompt_tokens_total": sum(row.get("prompt_tokens") or 0 for row in rows),
        "completion_tokens_total": sum(
            row.get("completion_tokens") or 0 for row in rows
        ),
        "tokens_total": sum(row.get("total_tokens") or 0 for row in rows),
        "model_request_runtime_ms_total": sum(
            row.get("model_request_runtime_ms") or 0 for row in rows
        ),
        "task_runtime_ms_total": sum(
            row.get("task_runtime_ms") or 0 for row in rows
        ),
    }


def _holdout_anti_leakage(
    run_dir: Path,
    *,
    suite: str,
    selected_tasks: list[str],
) -> dict[str, Any]:
    if suite != "curated-holdout":
        return {
            "status": "not_applicable",
            "reason": "checkpoint selection uses the disjoint QuixBugs validation suite",
        }
    try:
        audit = audit_matrix_dir(run_dir, CURATED_ROOT)
    except BaseException as exc:  # noqa: BLE001 - holdout audit fails closed
        return {
            "status": "error",
            "passed": False,
            "error": _bounded_error(exc),
            "missing_evidence_tasks": list(selected_tasks),
        }
    audited_tasks = set((audit.get("per_task") or {}).keys())
    missing = [task_id for task_id in selected_tasks if task_id not in audited_tasks]
    result = dict(audit)
    result["status"] = "complete" if not missing else "incomplete"
    result["missing_evidence_tasks"] = missing
    result["passed"] = bool(audit.get("passed") and not missing)
    return result


def _build_report(
    *,
    status: str,
    label: str,
    tag: str,
    stage: Optional[str],
    adapter_path: Optional[str],
    adapter_identity: Optional[dict[str, Any]],
    base_only: bool,
    contract_sha: str,
    git_commit: str,
    selected_tasks: list[str],
    suite: str,
    anti_leakage: Optional[dict[str, Any]],
    rows: list[dict[str, Any]],
    placement_audit: Optional[dict[str, Any]],
    lifecycle_path: Path,
    started_at: str,
    error: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    aggregate = _aggregate(rows)
    if suite == "curated-holdout":
        audit = anti_leakage or {
            "passed": False,
            "leakage_findings_total": None,
        }
        primary_target = bool(
            len(selected_tasks) == len(CURATED_HOLDOUT_IDS)
            and selected_tasks == list(CURATED_HOLDOUT_IDS)
            and len(rows) == len(CURATED_HOLDOUT_IDS)
            and all(
                not row.get("error")
                and row.get("per_task_pass") is True
                and row.get("verifier_status") == "COMPLETED"
                and row.get("verifier_outcome") == "RESOLVED"
                for row in rows
            )
        )
        leakage_findings = audit.get("leakage_findings_total")
        aggregate.update({
            "primary_target_5_of_5": primary_target,
            "leakage_findings": leakage_findings,
            "clean_holdout_prompt_audit_passed": audit.get("passed") is True,
            "clean_holdout_5_of_5": _clean_holdout_5_of_5(
                primary_target,
                audit.get("passed") is True,
                leakage_findings,
            ),
        })

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "run_status": status,
        "started_at": started_at,
        "updated_at": _utc_now(),
        "git_commit": git_commit,
        "tag": tag,
        "stage": stage,
        "label": label,
        "model": {
            "base_repository": BASE_REPOSITORY,
            "base_revision": BASE_REVISION,
            "adapter_path": adapter_path,
            "adapter_identity": adapter_identity,
            "base_only": base_only,
        },
        "contract_sha256": contract_sha,
        "selected_tasks": selected_tasks,
        "suite": suite,
        "anti_leakage": anti_leakage,
        "execution_policy": {
            "model_loads": 1 if placement_audit is not None else 0,
            "model_residency": "one process; resident across every selected task",
            "model_release": "process exit only",
            "per_task_gc_collect": False,
            "per_task_torch_cuda_empty_cache": False,
            "implicit_device_map_auto": False,
            "implicit_cpu_or_disk_dispatch": False,
            "attention_implementation": "efficient_sdpa",
        },
        "placement_audit": placement_audit,
        "lifecycle_log": str(lifecycle_path),
        "rows": rows,
        "aggregate": aggregate,
    }
    if error is not None:
        report["error"] = error
    return report


def _safe_tag(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
        raise ValueError(
            "tag must be one safe path component (letters, digits, '.', '_', '-')"
        )
    return value


def _select_task_entries(
    split: dict[str, Any],
    *,
    suite: str,
    task_ids: Optional[list[str]],
) -> list[dict[str, Any]]:
    if suite == "validation":
        available_entries = list(split["validation_tasks"])
    elif suite == "curated-holdout":
        available_entries = [{"task_id": task_id} for task_id in CURATED_HOLDOUT_IDS]
    else:
        raise ValueError(f"unknown task suite: {suite}")
    by_task = {entry["task_id"]: entry for entry in available_entries}
    if not task_ids:
        return available_entries
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("--task values must be unique")
    unknown = [task_id for task_id in task_ids if task_id not in by_task]
    if unknown:
        raise ValueError(f"tasks are not in the frozen {suite} suite: {unknown}")
    return [by_task[task_id] for task_id in task_ids]


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="R6 executable debugger evaluation on the disjoint validation set"
    )
    parser.add_argument(
        "--adapter-path", type=str, default=None,
        help="PEFT adapter directory (tuned model)",
    )
    parser.add_argument(
        "--base-only", action="store_true",
        help="evaluate the RAW base (untuned control)",
    )
    parser.add_argument("--output-dir", type=str, required=True)
    parser.add_argument(
        "--task", action="append", default=None,
        help="validation task id; repeat for a same-process multi-task probe",
    )
    parser.add_argument(
        "--suite",
        choices=("validation", "curated-holdout"),
        default="validation",
        help="frozen task suite from which --task values are selected",
    )
    parser.add_argument(
        "--stage", choices=("A", "B", "C"), default=None,
        help="bounded local execution stage recorded in the run evidence",
    )
    parser.add_argument(
        "--tag", type=str, default="eval",
        help="unique run tag (used as an output subdirectory)",
    )
    parser.add_argument("--cuda-device-index", type=int, default=0)
    args = parser.parse_args()

    if args.base_only == (args.adapter_path is not None):
        parser.error("select exactly one of --base-only or --adapter-path")
    try:
        tag = _safe_tag(args.tag)
    except ValueError as exc:
        parser.error(str(exc))

    if not SPLIT_MANIFEST.is_file():
        parser.error(f"split manifest missing: {SPLIT_MANIFEST}")
    if not EVAL_CONTRACT.is_file():
        parser.error(f"evaluation contract missing: {EVAL_CONTRACT}")

    split = json.loads(SPLIT_MANIFEST.read_text(encoding="utf-8"))
    try:
        selected_entries = _select_task_entries(
            split, suite=args.suite, task_ids=args.task
        )
    except (KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))

    if args.stage == "A" and len(selected_entries) != 1:
        parser.error("Stage A requires exactly one --task")
    if args.stage == "B" and len(selected_entries) != 2:
        parser.error("Stage B requires exactly two distinct --task values")
    if args.stage == "C" and len(selected_entries) < 1:
        parser.error("Stage C requires at least one validation task")

    contract = json.loads(EVAL_CONTRACT.read_text(encoding="utf-8"))
    if contract.get("model", {}).get("base_repository") != BASE_REPOSITORY:
        parser.error("evaluation contract base repository does not match transport")
    if contract.get("model", {}).get("base_revision") != BASE_REVISION:
        parser.error("evaluation contract base revision does not match transport")
    contract = json.loads(json.dumps(contract))
    contract["model"]["adapter_applied"] = not args.base_only
    if args.base_only:
        contract["model"].pop("adapter_label", None)
    contract_sha = _contract_sha256(contract)
    pdb_timeout = float(contract["budgets"]["pdb_request_timeout_seconds"])
    try:
        child_python = _require_child_python_matches_runtime()
    except RuntimeError as exc:
        parser.error(str(exc))

    adapter_identity: Optional[dict[str, Any]] = None
    if args.adapter_path is not None:
        adapter_path = Path(args.adapter_path).resolve()
        try:
            adapter_identity = _adapter_inference_identity(adapter_path)
        except RuntimeError as exc:
            parser.error(str(exc))
        safe_adapter_name = re.sub(r"[^A-Za-z0-9._-]", "_", adapter_path.name)
        label = f"adapter-{safe_adapter_name}"
    else:
        adapter_path = None
        label = "raw-base"

    output_root = Path(args.output_dir).resolve()
    run_dir = output_root / tag / label
    if run_dir.exists() and any(run_dir.iterdir()):
        parser.error(f"run directory is not empty; choose a unique --tag: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)

    lifecycle_path = run_dir / LIFECYCLE_FILENAME
    report_path = run_dir / "eval_report.json"
    recorder = CrashDurableLifecycleLog(lifecycle_path)
    started_at = _utc_now()
    git_commit = _git_commit()
    selected_tasks = [entry["task_id"] for entry in selected_entries]
    recorder(
        "evaluator_start",
        {
            "git_commit": git_commit,
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "child_python_command": child_python,
            "tag": tag,
            "stage": args.stage,
            "label": label,
            "selected_tasks": selected_tasks,
            "suite": args.suite,
            "cuda_device_index": args.cuda_device_index,
            "persistent_residency": True,
            "per_task_empty_cache": False,
        },
    )

    placement_audit: Optional[dict[str, Any]] = None
    rows: list[dict[str, Any]] = []
    try:
        recorder("model_load_start", {"label": label})
        if adapter_path is not None:
            transport = LocalQwenPeftTransport(
                adapter_path=str(adapter_path),
                cuda_device_index=args.cuda_device_index,
                lifecycle_event=recorder,
            )
        else:
            transport = LocalRawQwenTransport(
                cuda_device_index=args.cuda_device_index,
                lifecycle_event=recorder,
            )
        placement_audit = getattr(transport, "placement_audit", None)
        if not isinstance(placement_audit, dict):
            raise RuntimeError("transport did not provide a placement audit")
        _atomic_write_json(run_dir / PLACEMENT_FILENAME, placement_audit)
        recorder(
            "model_load_complete",
            {"label": label, "placement_audit": placement_audit},
        )
    except BaseException as exc:  # noqa: BLE001 - materialize load failure
        error = _bounded_error(exc)
        recorder("model_load_error", error)
        report = _build_report(
            status="model_load_failed",
            label=label,
            tag=tag,
            stage=args.stage,
            adapter_path=str(adapter_path) if adapter_path is not None else None,
            adapter_identity=adapter_identity,
            base_only=args.base_only,
            contract_sha=contract_sha,
            git_commit=git_commit,
            selected_tasks=selected_tasks,
            suite=args.suite,
            anti_leakage=None,
            rows=rows,
            placement_audit=placement_audit,
            lifecycle_path=lifecycle_path,
            started_at=started_at,
            error=error,
        )
        _atomic_write_json(report_path, report)
        recorder.close()
        print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)
        return 2

    active_task_id = ""

    def session_factory(workspace: TaskWorkspace) -> PdbSession:
        if not active_task_id:
            raise RuntimeError("PDB session requested outside an active task")
        return LifecyclePdbSession(
            workspace,
            task_id=active_task_id,
            lifecycle_event=recorder,
            startup_timeout=pdb_timeout,
            request_timeout=pdb_timeout,
            shutdown_timeout=2.0,
        )

    def compute_anti_leakage() -> dict[str, Any]:
        recorder(
            "anti_leakage_audit_start",
            {"suite": args.suite, "selected_tasks": selected_tasks},
        )
        audit = _holdout_anti_leakage(
            run_dir,
            suite=args.suite,
            selected_tasks=selected_tasks,
        )
        recorder(
            "anti_leakage_audit_complete",
            {
                "suite": args.suite,
                "status": audit.get("status"),
                "passed": audit.get("passed"),
                "leakage_findings_total": audit.get("leakage_findings_total"),
                "missing_evidence_tasks": audit.get("missing_evidence_tasks"),
            },
        )
        return audit

    for entry in selected_entries:
        task_id = entry["task_id"]
        active_task_id = task_id
        case_output = run_dir / task_id
        case_output.mkdir(parents=True, exist_ok=True)
        recorder("task_start", {"task_id": task_id})
        task_started = time.monotonic()
        try:
            evidence = run_experiment(
                contract,
                transport,
                case_output,
                task_id=task_id,
                pdb_session_factory=session_factory,
                lifecycle_event=recorder,
            )
            row = _matrix_row(evidence, task_id, contract_sha, contract)
            row.update(_row_metrics(evidence))
            rows.append(row)
            recorder(
                "task_complete",
                {
                    "task_id": task_id,
                    "duration_ms": int((time.monotonic() - task_started) * 1000),
                    "verifier_status": row.get("verifier_status"),
                    "verifier_outcome": row.get("verifier_outcome"),
                    "first_causal_failure": row.get("first_causal_failure"),
                    "model_calls": row.get("model_calls"),
                    "total_tokens": row.get("total_tokens"),
                },
            )
            print(
                json.dumps(
                    {
                        "task_id": task_id,
                        "verifier": (
                            f"{row['verifier_status']}/{row['verifier_outcome']}"
                        ),
                        "first_causal_failure": row["first_causal_failure"],
                        "model_calls": row["model_calls"],
                        "total_tokens": row["total_tokens"],
                        "task_runtime_ms": row["task_runtime_ms"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as exc:
            error = _bounded_error(exc)
            row = {
                "task_id": task_id,
                "error": f"{error['type']}: {error['message']}",
                "task_runtime_ms": int((time.monotonic() - task_started) * 1000),
            }
            rows.append(row)
            recorder("task_error", {"task_id": task_id, **error})
            print(f"{task_id}: ERROR {row['error']}", flush=True)
        finally:
            active_task_id = ""

        anti_leakage = compute_anti_leakage()
        checkpoint_report = _build_report(
            status="in_progress",
            label=label,
            tag=tag,
            stage=args.stage,
            adapter_path=str(adapter_path) if adapter_path is not None else None,
            adapter_identity=adapter_identity,
            base_only=args.base_only,
            contract_sha=contract_sha,
            git_commit=git_commit,
            selected_tasks=selected_tasks,
            suite=args.suite,
            anti_leakage=anti_leakage,
            rows=rows,
            placement_audit=placement_audit,
            lifecycle_path=lifecycle_path,
            started_at=started_at,
        )
        _atomic_write_json(report_path, checkpoint_report)
        recorder(
            "report_checkpoint_written",
            {"rows": len(rows), "path": str(report_path)},
        )

    anti_leakage = compute_anti_leakage()
    run_status = (
        "complete_with_errors"
        if any(row.get("error") for row in rows)
        or (args.suite == "curated-holdout" and anti_leakage.get("passed") is not True)
        else "complete"
    )
    report = _build_report(
        status=run_status,
        label=label,
        tag=tag,
        stage=args.stage,
        adapter_path=str(adapter_path) if adapter_path is not None else None,
        adapter_identity=adapter_identity,
        base_only=args.base_only,
        contract_sha=contract_sha,
        git_commit=git_commit,
        selected_tasks=selected_tasks,
        suite=args.suite,
        anti_leakage=anti_leakage,
        rows=rows,
        placement_audit=placement_audit,
        lifecycle_path=lifecycle_path,
        started_at=started_at,
    )
    if args.suite == "curated-holdout":
        if report["aggregate"].get("clean_holdout_5_of_5") is not True:
            run_status = "complete_target_not_met"
            report["run_status"] = run_status
        else:
            trace_dir = run_dir / "professor-traces"
            model_identity = {
                "fine_tuned_checkpoint": label,
                "adapter_identity_sha256": (
                    adapter_identity.get("inference_tree_sha256")
                    if adapter_identity is not None
                    else None
                ),
                "training_provenance": None,
            }
            if adapter_path is not None:
                provenance_path = adapter_path.parents[1] / "training_provenance.json"
                if provenance_path.is_file():
                    model_identity["training_provenance"] = str(provenance_path)
            recorder(
                "professor_trace_export_start",
                {"output_dir": str(trace_dir), "task_count": len(selected_tasks)},
            )
            try:
                trace_result = export_traces(
                    run_dir,
                    trace_dir,
                    task_ids=selected_tasks,
                    model_identity=model_identity,
                )
                if len(trace_result["traces"]) != len(CURATED_HOLDOUT_IDS):
                    raise RuntimeError(
                        "professor trace export did not produce all five holdout traces"
                    )
                report["professor_trace_export"] = {
                    "schema_version": "professor_debug_trace_v1",
                    "trace_count": len(trace_result["traces"]),
                    "index_path": trace_result["index_path"],
                    "output_dir": str(trace_dir),
                }
                recorder(
                    "professor_trace_export_complete",
                    report["professor_trace_export"],
                )
            except BaseException as exc:  # noqa: BLE001 - final export fails closed
                run_status = "complete_trace_export_failed"
                report["run_status"] = run_status
                report["professor_trace_export"] = {
                    "trace_count": 0,
                    "error": _bounded_error(exc),
                }
                recorder(
                    "professor_trace_export_error",
                    report["professor_trace_export"],
                )
    _atomic_write_json(report_path, report)
    recorder(
        "evaluator_complete",
        {
            "run_status": run_status,
            "aggregate": report["aggregate"],
            "model_release": "process_exit_only",
        },
    )
    recorder.close()
    print(json.dumps({"aggregate": report["aggregate"]}, indent=2), flush=True)
    return 0 if run_status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
