#!/usr/bin/env python3
"""Run one hash-pinned R6 training process with crash-durable GPU telemetry."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
TRAINER = THIS_FILE.with_name("train_qlora.py")
TELEMETRY = THIS_FILE.with_name("gpu_telemetry.py")
SFT_FILES = ("sft_train.jsonl", "sft_validation.jsonl", "sft_manifest.json")

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.r6_debugger_training.run_bounded_probe import (
    _build_execution_identity,
    _evaluator_environment,
    _persist_execution_identity,
    _telemetry_summary,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _append_event(path: Path, event: str, **fields: Any) -> None:
    record = {"timestamp_utc": _utc_now(), "event": event, **fields}
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sft_identity(sft_dir: Path) -> dict[str, Any]:
    files = []
    for name in SFT_FILES:
        path = sft_dir / name
        if not path.is_file():
            raise RuntimeError(f"missing SFT input: {path}")
        files.append({
            "name": name,
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        })
    canonical = json.dumps(
        files, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {
        "directory": str(sft_dir),
        "files": files,
        "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sft-dir", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--max-length", type=int, default=1792)
    parser.add_argument("--save-steps", type=int, default=10)
    parser.add_argument("--save-total-limit", type=int, default=4)
    parser.add_argument("--preflight-steps", type=int, default=0)
    parser.add_argument("--gpu-mode", choices=("ultimate", "standard-mshybrid", "unknown"), required=True)
    parser.add_argument("--telemetry-interval", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--snapshot-allow-file", action="append", default=[])
    parser.add_argument("--snapshot-allow-untracked-file", action="append", default=[])
    parser.add_argument("--expected-execution-id", required=True)
    parser.add_argument("--expected-source-manifest-sha256", required=True)
    parser.add_argument("--expected-sft-manifest-sha256", required=True)
    args = parser.parse_args()

    if args.timeout_seconds < 60:
        parser.error("timeout-seconds must be at least 60")
    try:
        source_identity, candidate_patch = _build_execution_identity(
            args.snapshot_allow_file,
            args.snapshot_allow_untracked_file,
        )
        if source_identity["execution_id"] != args.expected_execution_id:
            raise RuntimeError(
                f"execution identity mismatch: {source_identity['execution_id']} != "
                f"{args.expected_execution_id}"
            )
        if source_identity["source_manifest"]["sha256"] != args.expected_source_manifest_sha256:
            raise RuntimeError("execution-critical source manifest mismatch")
        source_artifacts = _persist_execution_identity(source_identity, candidate_patch)
        sft_identity = _sft_identity(args.sft_dir.resolve())
        if sft_identity["manifest_sha256"] != args.expected_sft_manifest_sha256:
            raise RuntimeError("SFT input manifest mismatch")
    except RuntimeError as exc:
        parser.error(str(exc))

    tag_root = Path(args.output_dir).resolve() / args.tag
    tag_root.mkdir(parents=True, exist_ok=False)
    manifest_path = tag_root / "training_probe_manifest.json"
    lifecycle_path = tag_root / "lifecycle.jsonl"
    telemetry_path = tag_root / "gpu_telemetry.csv"
    stop_path = tag_root / "telemetry.stop"
    stdout_path = tag_root / "trainer.stdout.log"
    stderr_path = tag_root / "trainer.stderr.log"
    child_environment, runtime_policy = _evaluator_environment()
    child_environment["PYTHONUNBUFFERED"] = "1"

    command = [
        sys.executable,
        str(TRAINER),
        "--run-id", args.run_id,
        "--sft-dir", str(args.sft_dir.resolve()),
        "--epochs", str(args.epochs),
        "--max-length", str(args.max_length),
        "--save-steps", str(args.save_steps),
        "--save-total-limit", str(args.save_total_limit),
    ]
    if args.preflight_steps:
        command.extend(("--preflight-steps", str(args.preflight_steps)))

    manifest: dict[str, Any] = {
        "schema_version": "r6-bounded-training-probe-v1",
        "status": "prepared",
        "created_at": _utc_now(),
        "tag": args.tag,
        "run_id": args.run_id,
        "gpu_mode": args.gpu_mode,
        "source_identity": source_identity,
        "source_identity_artifacts": source_artifacts,
        "sft_identity": sft_identity,
        "runtime_policy": runtime_policy,
        "command": command,
        "timeout_seconds": args.timeout_seconds,
        "preflight_steps": args.preflight_steps,
        "telemetry_path": str(telemetry_path),
    }
    _atomic_json(manifest_path, manifest)
    _append_event(lifecycle_path, "prepared")

    telemetry_command = [
        sys.executable,
        str(TELEMETRY),
        "--output", str(telemetry_path),
        "--interval", str(args.telemetry_interval),
        "--stop-file", str(stop_path),
    ]
    telemetry_process: subprocess.Popen[bytes] | None = None
    trainer_process: subprocess.Popen[bytes] | None = None
    trainer_exit_code: int | None = None
    try:
        telemetry_process = subprocess.Popen(
            telemetry_command,
            cwd=REPO_ROOT,
            env=child_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _append_event(lifecycle_path, "telemetry_started", pid=telemetry_process.pid)
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            trainer_process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=child_environment,
                stdout=stdout,
                stderr=stderr,
            )
            manifest.update({
                "status": "running",
                "started_at": _utc_now(),
                "trainer_pid": trainer_process.pid,
                "telemetry_pid": telemetry_process.pid,
            })
            _atomic_json(manifest_path, manifest)
            _append_event(lifecycle_path, "trainer_started", pid=trainer_process.pid)
            try:
                trainer_exit_code = trainer_process.wait(timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                trainer_process.kill()
                trainer_exit_code = trainer_process.wait(timeout=15)
                manifest["timeout"] = True
                _append_event(lifecycle_path, "trainer_timeout")
    except BaseException as exc:
        manifest["orchestration_error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        if trainer_process is not None and trainer_process.poll() is None:
            trainer_process.kill()
            trainer_process.wait(timeout=15)
        trainer_exit_code = trainer_process.returncode if trainer_process else None
    finally:
        stop_path.write_text("stop\n", encoding="utf-8")
        if telemetry_process is not None:
            try:
                telemetry_process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                telemetry_process.kill()
                telemetry_process.wait(timeout=10)
        _append_event(
            lifecycle_path,
            "trainer_completed",
            exit_code=trainer_exit_code,
        )

    manifest.update({
        "status": "complete" if trainer_exit_code == 0 else "failed",
        "completed_at": _utc_now(),
        "trainer_exit_code": trainer_exit_code,
        "telemetry_summary": _telemetry_summary(telemetry_path),
        "training_run_directory": str(
            THIS_FILE.parent / "runs" / args.run_id
        ),
    })
    _atomic_json(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, ensure_ascii=False), flush=True)
    return 0 if trainer_exit_code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
