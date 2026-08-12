#!/usr/bin/env python3
"""Run one bounded R6 evaluation stage with coupled crash-safe telemetry."""

from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
EVALUATOR = THIS_FILE.with_name("evaluate_debugger.py")
TELEMETRY = THIS_FILE.with_name("gpu_telemetry.py")
ALLOCATOR_POLICY = "backend:cudaMallocAsync"
WINDOWS_CHILD_CWD_MAX_CHARS = 248
WORKSPACE_NAME_TEMPLATE = "task_workspace_" + ("0" * 32)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _versions() -> dict[str, str]:
    result = {"python": sys.version.split()[0]}
    for package in (
        "torch", "transformers", "peft", "bitsandbytes", "accelerate", "pytest"
    ):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "NOT_INSTALLED"
    return result


def _evaluator_environment() -> tuple[dict[str, str], dict[str, str]]:
    environment = os.environ.copy()
    environment["PYTORCH_CUDA_ALLOC_CONF"] = ALLOCATOR_POLICY
    python_directory = str(Path(sys.executable).resolve().parent)
    existing_path = environment.get("PATH", "")
    entries = [item for item in existing_path.split(os.pathsep) if item]
    normalized_python_directory = os.path.normcase(os.path.abspath(python_directory))
    entries = [
        item
        for item in entries
        if os.path.normcase(os.path.abspath(item)) != normalized_python_directory
    ]
    environment["PATH"] = os.pathsep.join([python_directory, *entries])
    resolved_python = shutil.which("python", path=environment["PATH"])
    if resolved_python is None:
        raise RuntimeError("the evaluator environment cannot resolve 'python'")
    try:
        same_python = os.path.samefile(resolved_python, sys.executable)
    except OSError:
        same_python = (
            os.path.normcase(os.path.abspath(resolved_python))
            == os.path.normcase(os.path.abspath(sys.executable))
        )
    if not same_python:
        raise RuntimeError(
            "the evaluator child-python policy did not resolve sys.executable: "
            f"{resolved_python!r} != {sys.executable!r}"
        )
    policy = {
        "policy": "prepend evaluator interpreter directory to PATH",
        "python_directory": python_directory,
        "resolved_python_command": str(Path(resolved_python).resolve()),
    }
    return environment, policy


def _evaluation_label(adapter_path: str | None) -> str:
    if adapter_path is None:
        return "raw-base"
    safe_name = re.sub(
        r"[^A-Za-z0-9._-]", "_", Path(adapter_path).resolve().name
    )
    return f"adapter-{safe_name}"


def _child_cwd_policy(
    output_root: Path,
    *,
    tag: str,
    label: str,
    task_ids: list[str],
    platform_name: str = os.name,
) -> dict[str, Any]:
    predicted = []
    for task_id in task_ids:
        path = (
            output_root
            / tag
            / label
            / task_id
            / f"case-{task_id}"
            / WORKSPACE_NAME_TEMPLATE
        )
        predicted.append({
            "task_id": task_id,
            "path": str(path),
            "characters": len(str(path)),
        })
    limit = WINDOWS_CHILD_CWD_MAX_CHARS if platform_name == "nt" else None
    violations = (
        [item for item in predicted if item["characters"] > limit]
        if limit is not None
        else []
    )
    if violations:
        longest = max(violations, key=lambda item: item["characters"])
        raise RuntimeError(
            "predicted Windows task/PDB child cwd is too long for reliable "
            f"CreateProcess: {longest['characters']} > {limit} characters; "
            "choose a shorter --output-dir and/or --tag"
        )
    return {
        "platform": platform_name,
        "windows_max_characters": limit,
        "workspace_name_template": WORKSPACE_NAME_TEMPLATE,
        "predicted_paths": predicted,
        "passed": True,
    }


def _telemetry_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"rows": 0, "error": "telemetry file missing"}
    with path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    numeric_fields = (
        "temp_c", "power_w", "util_pct", "dedicated_vram_mib",
        "physical_vram_mib", "shared_gpu_mib",
    )
    peaks: dict[str, float] = {}
    for field in numeric_fields:
        values = []
        for row in rows:
            try:
                values.append(float(row[field]))
            except (KeyError, TypeError, ValueError):
                pass
        if values:
            peaks[field] = max(values)
    return {
        "rows": len(rows),
        "peaks": peaks,
        "first_sample": rows[0] if rows else None,
        "last_sample": rows[-1] if rows else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a bounded R6 evaluator stage with GPU telemetry"
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--adapter-path", type=str)
    selection.add_argument("--base-only", action="store_true")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--stage", choices=("A", "B", "C"), required=True)
    parser.add_argument(
        "--suite",
        choices=("validation", "curated-holdout"),
        default="validation",
    )
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--cuda-device-index", type=int, default=0)
    parser.add_argument(
        "--gpu-mode",
        choices=("ultimate", "standard-mshybrid", "unknown"),
        default="unknown",
    )
    parser.add_argument("--telemetry-interval", type=float, default=1.0)
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.tag):
        parser.error("--tag must be one safe path component")
    if args.stage == "A" and len(args.task) != 1:
        parser.error("Stage A requires exactly one --task")
    if args.stage == "B" and len(args.task) != 2:
        parser.error("Stage B requires exactly two --task values")
    if args.stage == "C" and len(args.task) < 1:
        parser.error("Stage C requires at least one --task")
    if len(set(args.task)) != len(args.task):
        parser.error("--task values must be unique")

    tracked_status = _run_git("status", "--short", "--untracked-files=no")
    if tracked_status:
        parser.error(
            "bounded GPU probes require a clean tracked worktree; commit the "
            "coherent local checkpoint first"
        )
    git_commit = _run_git("rev-parse", "HEAD")
    output_root = Path(args.output_dir).resolve()
    label = _evaluation_label(args.adapter_path)
    try:
        child_cwd_policy = _child_cwd_policy(
            output_root,
            tag=args.tag,
            label=label,
            task_ids=args.task,
        )
    except RuntimeError as exc:
        parser.error(str(exc))
    tag_root = output_root / args.tag
    if tag_root.exists() and any(tag_root.iterdir()):
        parser.error(f"tag output already exists and is not empty: {tag_root}")
    tag_root.mkdir(parents=True, exist_ok=True)

    telemetry_path = tag_root / "gpu_telemetry.csv"
    telemetry_stop_path = tag_root / "telemetry.stop"
    telemetry_stdout_path = tag_root / "telemetry.stdout.log"
    telemetry_stderr_path = tag_root / "telemetry.stderr.log"
    evaluator_stdout_path = tag_root / "evaluator.stdout.log"
    evaluator_stderr_path = tag_root / "evaluator.stderr.log"
    manifest_path = tag_root / "probe_manifest.json"

    evaluator_command = [
        sys.executable,
        str(EVALUATOR),
        "--output-dir", str(output_root),
        "--tag", args.tag,
        "--stage", args.stage,
        "--suite", args.suite,
        "--cuda-device-index", str(args.cuda_device_index),
    ]
    if args.adapter_path:
        evaluator_command.extend(["--adapter-path", str(Path(args.adapter_path).resolve())])
    else:
        evaluator_command.append("--base-only")
    for task_id in args.task:
        evaluator_command.extend(["--task", task_id])

    telemetry_command = [
        sys.executable,
        str(TELEMETRY),
        "--output", str(telemetry_path),
        "--interval", str(args.telemetry_interval),
        "--stop-file", str(telemetry_stop_path),
    ]
    environment, python_command_policy = _evaluator_environment()
    manifest: dict[str, Any] = {
        "schema_version": "r6-bounded-probe-v1",
        "status": "starting",
        "created_at": _utc_now(),
        "git_commit": git_commit,
        "python_executable": sys.executable,
        "versions": _versions(),
        "stage": args.stage,
        "suite": args.suite,
        "tasks": args.task,
        "gpu_mode": args.gpu_mode,
        "cuda_device_index": args.cuda_device_index,
        "child_cwd_policy": child_cwd_policy,
        "environment": {
            "PYTORCH_CUDA_ALLOC_CONF": ALLOCATOR_POLICY,
            "python_command": python_command_policy,
        },
        "evaluator_command": evaluator_command,
        "telemetry_command": telemetry_command,
        "expected_evidence": {
            "telemetry": str(telemetry_path),
            "evaluator_stdout": str(evaluator_stdout_path),
            "evaluator_stderr": str(evaluator_stderr_path),
        },
    }
    _atomic_json(manifest_path, manifest)

    telemetry_process = None
    evaluator_process = None
    evaluator_exit_code = 3
    telemetry_stdout = telemetry_stdout_path.open("x", encoding="utf-8")
    telemetry_stderr = telemetry_stderr_path.open("x", encoding="utf-8")
    evaluator_stdout = evaluator_stdout_path.open("x", encoding="utf-8")
    evaluator_stderr = evaluator_stderr_path.open("x", encoding="utf-8")
    try:
        telemetry_process = subprocess.Popen(
            telemetry_command,
            cwd=REPO_ROOT,
            env=environment,
            stdout=telemetry_stdout,
            stderr=telemetry_stderr,
        )
        time.sleep(min(2.0, max(0.25, args.telemetry_interval)))
        telemetry_startup_code = telemetry_process.poll()
        if telemetry_startup_code is not None:
            raise RuntimeError(
                f"telemetry exited during startup with code {telemetry_startup_code}"
            )
        evaluator_process = subprocess.Popen(
            evaluator_command,
            cwd=REPO_ROOT,
            env=environment,
            stdout=evaluator_stdout,
            stderr=evaluator_stderr,
        )
        manifest["status"] = "running"
        manifest["evaluator_pid"] = evaluator_process.pid
        manifest["telemetry_pid"] = telemetry_process.pid
        manifest["evaluation_started_at"] = _utc_now()
        _atomic_json(manifest_path, manifest)
        evaluator_exit_code = evaluator_process.wait()
    except KeyboardInterrupt:
        evaluator_exit_code = 130
        if evaluator_process is not None and evaluator_process.poll() is None:
            evaluator_process.terminate()
            try:
                evaluator_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                evaluator_process.kill()
                evaluator_process.wait(timeout=10)
    except BaseException as exc:  # noqa: BLE001 - preserve orchestration failure
        manifest["orchestration_error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        if evaluator_process is not None and evaluator_process.poll() is None:
            evaluator_process.terminate()
            try:
                evaluator_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                evaluator_process.kill()
                evaluator_process.wait(timeout=10)
    finally:
        if telemetry_process is not None and telemetry_process.poll() is None:
            telemetry_stop_path.touch(exist_ok=True)
            try:
                telemetry_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                telemetry_process.terminate()
                try:
                    telemetry_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    telemetry_process.kill()
                    telemetry_process.wait(timeout=10)
        for stream in (
            telemetry_stdout, telemetry_stderr, evaluator_stdout, evaluator_stderr
        ):
            stream.flush()
            stream.close()

    manifest["status"] = "complete" if evaluator_exit_code == 0 else "failed"
    manifest["completed_at"] = _utc_now()
    manifest["evaluator_exit_code"] = evaluator_exit_code
    manifest["telemetry_summary"] = _telemetry_summary(telemetry_path)
    _atomic_json(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, ensure_ascii=False), flush=True)
    return evaluator_exit_code


if __name__ == "__main__":
    sys.exit(main())
