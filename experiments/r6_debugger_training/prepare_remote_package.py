#!/usr/bin/env python3
"""Create a weight-free, hash-pinned remote execution handoff for R6."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.r6_debugger_training.run_bounded_probe import (
    _build_execution_identity,
    _persist_execution_identity,
)
from experiments.r6_debugger_training.run_bounded_training import (
    SFT_FILES,
    _sft_identity,
)

BASE_REPOSITORY = "Qwen/Qwen2.5-Coder-7B-Instruct"
BASE_REVISION = "c03e6d358207e414f1eca0bb1891e29f1db0e242"
ALLOCATOR_POLICY = "backend:cudaMallocAsync"
INFERENCE_FILES = ("adapter_config.json", "adapter_model.safetensors")
CONTRACT_FILES = (
    "experiments/r6_debugger_training/r6_eval_contract.json",
    "experiments/r6_debugger_training/split_manifest.json",
    "agentic_debugger/evaluation/professor_debug_trace_schema_v1.json",
)
PACKAGES = (
    "torch", "transformers", "peft", "bitsandbytes", "accelerate", "pytest"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _file_identity(path: Path, *, portable_path: str) -> dict[str, Any]:
    return {
        "path": portable_path,
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _checkpoint(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("checkpoint must be LABEL=PATH")
    label, raw_path = value.split("=", 1)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", label):
        raise argparse.ArgumentTypeError(f"unsafe checkpoint label: {label!r}")
    path = Path(raw_path).resolve()
    for name in INFERENCE_FILES:
        if not (path / name).is_file():
            raise argparse.ArgumentTypeError(f"missing {name}: {path}")
    return label, path


def _versions() -> dict[str, str]:
    result = {"python": platform.python_version()}
    for package in PACKAGES:
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "NOT_INSTALLED"
    return result


def _command(
    checkpoint_label: str,
    *,
    tag: str,
    stage: str,
    suite: str,
    tasks: tuple[str, ...],
    execution_id: str,
    source_manifest_sha256: str,
    snapshot_files: tuple[str, ...],
    snapshot_untracked_files: tuple[str, ...],
) -> list[str]:
    result = [
        "python",
        "experiments/r6_debugger_training/run_bounded_probe.py",
        "--adapter-path",
        f"${{ADAPTER_ROOT}}/{checkpoint_label}",
        "--output-dir",
        "${OUTPUT_ROOT}",
        "--tag",
        tag,
        "--stage",
        stage,
        "--suite",
        suite,
        "--gpu-mode",
        "unknown",
        "--expected-execution-id",
        execution_id,
        "--expected-source-manifest-sha256",
        source_manifest_sha256,
    ]
    for path in snapshot_files:
        result.extend(("--snapshot-allow-file", path))
    for path in snapshot_untracked_files:
        result.extend(("--snapshot-allow-untracked-file", path))
    for task_id in tasks:
        result.extend(("--task", task_id))
    return result


def _training_command(
    *,
    execution_id: str,
    source_manifest_sha256: str,
    sft_manifest_sha256: str,
    snapshot_files: tuple[str, ...],
    snapshot_untracked_files: tuple[str, ...],
) -> list[str]:
    result = [
        "python",
        "experiments/r6_debugger_training/run_bounded_training.py",
        "--output-dir", "${OUTPUT_ROOT}",
        "--tag", "remote-training-v3",
        "--run-id", "r6-sft-debugger-v3",
        "--sft-dir", "${PACKAGE_ROOT}/training-data",
        "--gpu-mode", "unknown",
        "--expected-execution-id", execution_id,
        "--expected-source-manifest-sha256", source_manifest_sha256,
        "--expected-sft-manifest-sha256", sft_manifest_sha256,
    ]
    for path in snapshot_files:
        result.extend(("--snapshot-allow-file", path))
    for path in snapshot_untracked_files:
        result.extend(("--snapshot-allow-untracked-file", path))
    return result


def _write_text(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _write_bytes(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--checkpoint",
        action="append",
        type=_checkpoint,
        required=True,
        help="repeat LABEL=PATH; adapter weights are hashed but never copied",
    )
    parser.add_argument(
        "--sft-dir",
        type=Path,
        default=None,
        help="optional exact SFT input directory to embed for remote training",
    )
    parser.add_argument(
        "--snapshot-allow-file",
        action="append",
        default=[],
        help="repeat for every validated tracked change in snapshot mode",
    )
    parser.add_argument(
        "--snapshot-allow-untracked-file",
        action="append",
        default=[],
        help="repeat for every validated untracked addition embedded in the patch",
    )
    args = parser.parse_args()

    try:
        execution_identity, candidate_patch = _build_execution_identity(
            args.snapshot_allow_file,
            args.snapshot_allow_untracked_file,
        )
        execution_artifacts = _persist_execution_identity(
            execution_identity, candidate_patch
        )
    except RuntimeError as exc:
        parser.error(str(exc))
    labels = [label for label, _path in args.checkpoint]
    if len(set(labels)) != len(labels):
        parser.error("checkpoint labels must be unique")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    inputs_dir = output_dir / "contracts"
    inputs_dir.mkdir()
    source_dir = output_dir / "source-identity"
    source_dir.mkdir()
    _write_text(
        source_dir / "execution_identity.json",
        json.dumps(
            execution_identity,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n",
    )
    if execution_identity["identity_kind"] == "working_tree_snapshot":
        _write_bytes(source_dir / "candidate.patch", candidate_patch)

    contract_identities = []
    for relative in CONTRACT_FILES:
        source = REPO_ROOT / relative
        if not source.is_file():
            raise RuntimeError(f"required contract is missing: {source}")
        destination = inputs_dir / Path(relative).name
        shutil.copy2(source, destination)
        contract_identities.append(
            _file_identity(destination, portable_path=f"contracts/{destination.name}")
        )

    training_input = None
    if args.sft_dir is not None:
        sft_dir = args.sft_dir.resolve()
        try:
            training_input = _sft_identity(sft_dir)
        except RuntimeError as exc:
            parser.error(str(exc))
        training_dir = output_dir / "training-data"
        training_dir.mkdir()
        for name in SFT_FILES:
            shutil.copy2(sft_dir / name, training_dir / name)
        training_input = {
            **training_input,
            "directory": "${PACKAGE_ROOT}/training-data",
            "embedded": True,
            "package_files": [f"training-data/{name}" for name in SFT_FILES],
        }

    checkpoint_identities = []
    for label, checkpoint_dir in args.checkpoint:
        checkpoint_identities.append(
            {
                "label": label,
                "remote_expected_directory": f"${{ADAPTER_ROOT}}/{label}",
                "local_source_directory": str(checkpoint_dir),
                "files": [
                    _file_identity(
                        checkpoint_dir / name,
                        portable_path=f"${{ADAPTER_ROOT}}/{label}/{name}",
                    )
                    for name in INFERENCE_FILES
                ],
                "weights_in_package": False,
            }
        )

    versions = _versions()
    requirements = "\n".join(
        f"{name}=={versions[name]}" for name in PACKAGES if name != "torch"
    ) + "\n"
    _write_text(output_dir / "requirements-evaluation.txt", requirements)

    selected_label = "checkpoint-30" if "checkpoint-30" in labels else labels[0]
    validation_a = ("quixbugs-depth-first-search",)
    validation_b = ("quixbugs-quicksort", "quixbugs-flatten")
    validation_c = (
        "quixbugs-find-in-sorted",
        "quixbugs-rpn-eval",
        "quixbugs-shortest-path-length",
        "quixbugs-reverse-linked-list",
        "quixbugs-kth",
    )
    holdout = (
        "curated-none-handling-001",
        "curated-off-by-one-002",
        "curated-wrong-branch-003",
        "curated-mutation-alias-004",
        "curated-caller-callee-005",
    )
    command_identity = {
        "execution_id": execution_identity["execution_id"],
        "source_manifest_sha256": execution_identity["source_manifest"]["sha256"],
        "snapshot_files": tuple(execution_identity["tracked_changed_files"]),
        "snapshot_untracked_files": tuple(
            execution_identity["captured_untracked_files"]
        ),
    }
    commands = [
        _command(
            selected_label, tag="remote-stage-a", stage="A",
            suite="validation", tasks=validation_a, **command_identity,
        ),
        _command(
            selected_label, tag="remote-stage-b", stage="B",
            suite="validation", tasks=validation_b, **command_identity,
        ),
        _command(
            selected_label, tag="remote-stage-c", stage="C",
            suite="validation", tasks=validation_c, **command_identity,
        ),
        _command(
            selected_label, tag="remote-final-holdout", stage="C",
            suite="curated-holdout", tasks=holdout, **command_identity,
        ),
    ]
    training_command = None
    if training_input is not None:
        training_command = _training_command(
            sft_manifest_sha256=training_input["manifest_sha256"],
            **command_identity,
        )

    if execution_identity["identity_kind"] == "working_tree_snapshot":
        reconstruction_commands = [
            "git checkout -B "
            f"{execution_identity['branch']} {execution_identity['base_head']}",
            "git -c core.autocrlf=false apply --check --binary "
            "source-identity/candidate.patch",
            "git -c core.autocrlf=false apply --binary "
            "source-identity/candidate.patch",
        ]
    else:
        reconstruction_commands = [
            "git checkout -B "
            f"{execution_identity['branch']} {execution_identity['base_head']}"
        ]

    manifest: dict[str, Any] = {
        "schema_version": "r6-remote-execution-package-v2",
        "created_at": _utc_now(),
        "source_identity": execution_identity,
        "source_identity_artifacts": execution_artifacts,
        "remote_reconstruction_commands": reconstruction_commands,
        "source_runtime": {
            "platform": platform.platform(),
            "python_executable": sys.executable,
            "versions": versions,
        },
        "remote_runtime_contract": {
            "python": versions["python"],
            "packages": {name: versions[name] for name in PACKAGES},
            "torch_install": (
                "python -m pip install --pre torch=="
                f"{versions['torch']} --index-url "
                "https://download.pytorch.org/whl/nightly/cu128"
            ),
            "remaining_install": (
                "python -m pip install -r requirements-evaluation.txt"
            ),
            "project_install": (
                "not required; evaluator inserts the exact checkout into sys.path "
                "(pyproject currently requires Python >=3.11 while the proven R6 "
                "model environment is Python 3.10.1)"
            ),
        },
        "model": {
            "base_repository": BASE_REPOSITORY,
            "base_revision": BASE_REVISION,
            "quantization": "NF4 4-bit double-quant",
            "attention_implementation": "efficient_sdpa",
            "placement": "explicit complete model map to cuda:0; no CPU/disk/meta",
            "residency": "one load per evaluator process; no per-task empty_cache",
        },
        "environment": {"PYTORCH_CUDA_ALLOC_CONF": ALLOCATOR_POLICY},
        "checkpoints": checkpoint_identities,
        "training_input": training_input,
        "training_command": training_command,
        "contracts": contract_identities,
        "commands_in_order": commands,
        "expected_artifacts_per_tag": [
            "${OUTPUT_ROOT}/<tag>/probe_manifest.json",
            "${OUTPUT_ROOT}/<tag>/gpu_telemetry.csv",
            "${OUTPUT_ROOT}/<tag>/evaluator.stdout.log",
            "${OUTPUT_ROOT}/<tag>/evaluator.stderr.log",
            "${OUTPUT_ROOT}/<tag>/adapter-<checkpoint>/eval_report.json",
            "${OUTPUT_ROOT}/<tag>/adapter-<checkpoint>/placement_audit.json",
            "${OUTPUT_ROOT}/<tag>/adapter-<checkpoint>/lifecycle.jsonl",
            "${OUTPUT_ROOT}/<tag>/adapter-<checkpoint>/<task-id>/evidence.json",
            "${OUTPUT_ROOT}/remote-final-holdout/adapter-<checkpoint>/professor-traces/professor_debug_trace_index.json",
            "${OUTPUT_ROOT}/remote-final-holdout/adapter-<checkpoint>/professor-traces/professor_debug_trace_<task-id>.json",
        ],
        "weight_policy": {
            "contains_model_or_adapter_weights": False,
            "transfer_separately": list(INFERENCE_FILES),
            "verification": "hash each transferred file and compare with checkpoints",
        },
    }
    manifest_path = output_dir / "remote_manifest.json"
    _write_text(
        manifest_path,
        json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
    )
    readme = """# R6 remote execution handoff

This package contains no model or adapter weights. Reconstruct the exact source
with `remote_reconstruction_commands`: either check out the clean commit, or
check out `base_head`, check/apply `source-identity/candidate.patch`, and let the
bounded wrapper re-verify the execution ID, patch allowlist, reconstruction, and
source-manifest hash before model loading. Create the exact Python environment,
transfer each checkpoint's two inference files separately, and verify every
SHA-256. Set `PACKAGE_ROOT`, `ADAPTER_ROOT`, and `OUTPUT_ROOT`. When the package
contains `training_input`, its `training_command` reproduces the bounded v3
training intervention; its resulting checkpoint can then be packaged or bound
as an adapter for evaluation. Run evaluation commands in manifest order. Do not
proceed from a stage whose process, placement audit, telemetry, or
evaluator report is incomplete. The final command exports professor traces only
if the selected checkpoint has a strict five-row `RESOLVED`, zero-leakage
curated-holdout result.
"""
    _write_text(output_dir / "README.md", readme)

    package_files = sorted(path for path in output_dir.rglob("*") if path.is_file())
    package_hashes = {
        path.relative_to(output_dir).as_posix(): _sha256_file(path)
        for path in package_files
    }
    _write_text(
        output_dir / "package_hashes.json",
        json.dumps(package_hashes, indent=2, sort_keys=True) + "\n",
    )
    zip_path = output_dir.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in output_dir.rglob("*") if p.is_file()):
            archive.write(path, path.relative_to(output_dir.parent))
    print(json.dumps({"output_dir": str(output_dir), "zip": str(zip_path)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
