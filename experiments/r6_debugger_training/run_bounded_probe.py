#!/usr/bin/env python3
"""Run one bounded R6 evaluation stage with coupled crash-safe telemetry."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
EVALUATOR = THIS_FILE.with_name("evaluate_debugger.py")
TELEMETRY = THIS_FILE.with_name("gpu_telemetry.py")
ALLOCATOR_POLICY = "backend:cudaMallocAsync"
WINDOWS_CHILD_CWD_MAX_CHARS = 248
WORKSPACE_NAME_TEMPLATE = "task_workspace_" + ("0" * 32)
EXPECTED_R6_BRANCH = "goal/r6-finetuned-debugger-codex-v2"
EXECUTION_IDENTITY_SCHEMA = "r6-working-tree-execution-identity-v1"
EXECUTION_CRITICAL_ROOTS = (
    "agentic_debugger/",
    "experiments/debugger_interaction_v2_r5/",
    "experiments/r6_debugger_training/",
)
EXECUTION_CRITICAL_SUFFIXES = (".py", ".json", ".toml")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _run_git(*args: str) -> str:
    return _run_git_bytes(*args).decode("utf-8", errors="strict").strip()


def _run_git_bytes(*args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _nul_paths(payload: bytes) -> list[str]:
    return [
        item.decode("utf-8", errors="strict")
        for item in payload.split(b"\0")
        if item
    ]


def _normalize_repo_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise RuntimeError(f"snapshot allowlist path must be repository-relative: {value!r}")
    return path.as_posix()


def _is_execution_critical(path: str) -> bool:
    return path.endswith(EXECUTION_CRITICAL_SUFFIXES) and path.startswith(
        EXECUTION_CRITICAL_ROOTS
    )


def _repo_file_identity(relative: str) -> dict[str, Any]:
    path = REPO_ROOT / Path(relative)
    if not path.is_file():
        raise RuntimeError(
            "working-tree snapshots do not permit missing or deleted files: "
            f"{relative}"
        )
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _source_manifest(extra_files: list[str] | None = None) -> dict[str, Any]:
    tracked = _nul_paths(
        _run_git_bytes(
            "ls-files",
            "-z",
            "--",
            *(root.rstrip("/") for root in EXECUTION_CRITICAL_ROOTS),
        )
    )
    paths = sorted(
        set(tracked)
        | {
            path
            for path in (extra_files or [])
            if _is_execution_critical(path)
        }
    )
    files = [
        _repo_file_identity(path)
        for path in paths
        if _is_execution_critical(path)
    ]
    canonical = json.dumps(
        files, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {
        "scope": {
            "tracked_roots": list(EXECUTION_CRITICAL_ROOTS),
            "suffixes": list(EXECUTION_CRITICAL_SUFFIXES),
        },
        "file_count": len(files),
        "files": files,
        "sha256": _sha256_bytes(canonical),
    }


def _verify_patch_reconstruction(
    base_head: str,
    patch: bytes,
    changed_files: list[dict[str, Any]],
    tracked_changed_paths: list[str],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="r6-snapshot-reconstruct-") as raw_root:
        root = Path(raw_root)
        for relative in tracked_changed_paths:
            destination = root / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_run_git_bytes("show", f"{base_head}:{relative}"))
        patch_path = root / "candidate.patch"
        patch_path.write_bytes(patch)
        for arguments in (
            (
                "git", "-c", "core.autocrlf=false", "apply",
                "--check", "--binary", str(patch_path),
            ),
            (
                "git", "-c", "core.autocrlf=false", "apply",
                "--binary", str(patch_path),
            ),
        ):
            result = subprocess.run(
                arguments,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "candidate.patch cannot reconstruct the working tree from "
                    f"BASE_HEAD: {result.stderr.strip()}"
                )
        for identity in changed_files:
            reconstructed = root / Path(identity["path"])
            if not reconstructed.is_file():
                raise RuntimeError(
                    f"reconstructed file is missing: {identity['path']}"
                )
            actual = _sha256_file(reconstructed)
            if actual != identity["sha256"]:
                raise RuntimeError(
                    "reconstructed source hash mismatch for "
                    f"{identity['path']}: {actual} != {identity['sha256']}"
                )
    return {
        "passed": True,
        "method": "materialize changed files from BASE_HEAD; "
        "git -c core.autocrlf=false apply --check; git apply --binary; "
        "verify every changed-file SHA256",
        "verified_file_count": len(changed_files),
    }


def _complete_binary_patch(
    base_head: str,
    tracked_changed_paths: list[str],
    captured_untracked_paths: list[str],
) -> bytes:
    all_paths = sorted([*tracked_changed_paths, *captured_untracked_paths])
    with tempfile.TemporaryDirectory(prefix="r6-snapshot-patch-") as raw_root:
        root = Path(raw_root)

        def run(*arguments: str, accepted_codes: tuple[int, ...] = (0,)) -> bytes:
            result = subprocess.run(
                ["git", "-c", "core.autocrlf=false", *arguments],
                cwd=root,
                capture_output=True,
                timeout=20,
                check=False,
            )
            if result.returncode not in accepted_codes:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"snapshot patch git {' '.join(arguments)} failed: {stderr}"
                )
            return result.stdout

        run("init", "--quiet")
        attributes = root / ".gitattributes"
        attributes.write_text("* binary\n", encoding="utf-8", newline="\n")
        for relative in tracked_changed_paths:
            destination = root / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_run_git_bytes("show", f"{base_head}:{relative}"))
        run("add", "--", ".gitattributes", *tracked_changed_paths)
        run(
            "-c", "user.name=R6 Snapshot",
            "-c", "user.email=r6-snapshot@example.invalid",
            "commit", "--quiet", "-m", "snapshot base",
        )
        for relative in all_paths:
            destination = root / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((REPO_ROOT / Path(relative)).read_bytes())
        run("add", "--", *all_paths)
        result = subprocess.run(
            [
                "git",
                "-c", "core.autocrlf=false",
                "diff",
                "--cached",
                "--binary",
                "HEAD",
                "--",
                *all_paths,
            ],
            cwd=root,
            capture_output=True,
            timeout=20,
            check=False,
        )
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"cannot create deterministic complete binary patch: {stderr}"
        )
    if result.stdout.count(b"GIT binary patch") != len(all_paths):
        raise RuntimeError(
            "complete candidate patch does not contain one byte-exact binary "
            "delta per captured file"
        )
    return result.stdout


def _build_execution_identity(
    allowlisted_files: list[str],
    allowlisted_untracked_files: list[str] | None = None,
) -> tuple[dict[str, Any], bytes]:
    branch = _run_git("branch", "--show-current")
    if branch != EXPECTED_R6_BRANCH:
        raise RuntimeError(
            f"bounded R6 execution requires branch {EXPECTED_R6_BRANCH!r}, "
            f"found {branch!r}"
        )
    base_head = _run_git("rev-parse", "HEAD")
    diff_check = subprocess.run(
        ["git", "diff", "--check", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if diff_check.returncode != 0:
        raise RuntimeError(f"git diff --check HEAD failed: {diff_check.stdout.strip()}")

    changed_paths = sorted(
        _nul_paths(
            _run_git_bytes(
                "diff", "--name-only", "--diff-filter=ACDMRTUXB", "-z", "HEAD"
            )
        )
    )
    allowlist = sorted({_normalize_repo_path(path) for path in allowlisted_files})
    if changed_paths != allowlist:
        unexpected = sorted(set(changed_paths) - set(allowlist))
        missing = sorted(set(allowlist) - set(changed_paths))
        raise RuntimeError(
            "tracked changes must exactly equal the validated snapshot allowlist; "
            f"unexpected={unexpected}, missing={missing}"
        )

    untracked = sorted(
        _nul_paths(
            _run_git_bytes(
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                *(root.rstrip("/") for root in EXECUTION_CRITICAL_ROOTS),
            )
        )
    )
    captured_untracked = sorted({
        _normalize_repo_path(path) for path in (allowlisted_untracked_files or [])
    })
    missing_untracked = sorted(set(captured_untracked) - set(untracked))
    if missing_untracked:
        raise RuntimeError(
            "snapshot untracked allowlist contains files that are not untracked: "
            f"{missing_untracked}"
        )
    untracked_critical = sorted(
        path
        for path in untracked
        if _is_execution_critical(path) and path not in captured_untracked
    )
    if untracked_critical:
        raise RuntimeError(
            "untracked execution-critical source/config is forbidden: "
            f"{untracked_critical}"
        )
    uncaptured_untracked_noncritical = sorted(set(untracked) - set(captured_untracked))
    uncaptured_payload = "\0".join(uncaptured_untracked_noncritical).encode("utf-8")
    uncaptured_summary = {
        "audited_roots": list(EXECUTION_CRITICAL_ROOTS),
        "count": len(uncaptured_untracked_noncritical),
        "paths_sha256": _sha256_bytes(uncaptured_payload),
    }

    source_manifest = _source_manifest(captured_untracked)
    if not changed_paths and not captured_untracked:
        identity = {
            "schema_version": EXECUTION_IDENTITY_SCHEMA,
            "identity_kind": "clean_commit",
            "execution_id": f"commit-{base_head}",
            "branch": branch,
            "base_head": base_head,
            "candidate_patch_sha256": None,
            "changed_files": [],
            "tracked_changed_files": [],
            "captured_untracked_files": [],
            "uncaptured_untracked_noncritical_files": (
                uncaptured_summary
            ),
            "changed_file_identities": [],
            "source_manifest": source_manifest,
            "confirmations": {
                "tracked_worktree_clean": True,
                "tracked_changes_exactly_allowlisted": True,
                "unrelated_tracked_changes_present": False,
                "untracked_execution_critical_source_consumed": False,
                "git_diff_check_passed": True,
            },
            "reconstruction": {
                "passed": True,
                "method": "checkout exact commit",
            },
        }
        return identity, b""

    tracked_patch = _run_git_bytes("diff", "--binary", "HEAD")
    if changed_paths and not tracked_patch:
        raise RuntimeError("tracked snapshot produced an empty git diff --binary HEAD")
    patch = _complete_binary_patch(base_head, changed_paths, captured_untracked)
    if not patch:
        raise RuntimeError("working-tree snapshot produced an empty candidate.patch")
    all_changed_paths = sorted([*changed_paths, *captured_untracked])
    changed_file_identities = [
        _repo_file_identity(path) for path in all_changed_paths
    ]
    patch_sha = _sha256_bytes(patch)
    reconstruction = _verify_patch_reconstruction(
        base_head, patch, changed_file_identities, changed_paths
    )
    identity = {
        "schema_version": EXECUTION_IDENTITY_SCHEMA,
        "identity_kind": "working_tree_snapshot",
        "execution_id": f"wt-{base_head[:12]}-{patch_sha[:20]}",
        "branch": branch,
        "base_head": base_head,
        "candidate_patch_sha256": patch_sha,
        "git_diff_binary_head_sha256": _sha256_bytes(tracked_patch),
        "git_diff_binary_head_bytes": len(tracked_patch),
        "changed_files": all_changed_paths,
        "tracked_changed_files": changed_paths,
        "captured_untracked_files": captured_untracked,
        "uncaptured_untracked_noncritical_files": uncaptured_summary,
        "changed_file_identities": changed_file_identities,
        "source_manifest": source_manifest,
        "confirmations": {
            "tracked_worktree_clean": False,
            "tracked_changes_exactly_allowlisted": True,
            "unrelated_tracked_changes_present": False,
            "untracked_execution_critical_source_consumed": False,
            "git_diff_check_passed": True,
        },
        "reconstruction": reconstruction,
    }
    return identity, patch


def _write_or_verify(path: Path, payload: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise RuntimeError(f"snapshot artifact collision: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _persist_execution_identity(
    identity: dict[str, Any],
    patch: bytes,
) -> dict[str, str]:
    snapshot_root = (
        REPO_ROOT
        / "operator"
        / "r6-execution-snapshots"
        / identity["execution_id"]
    )
    identity_path = snapshot_root / "execution_identity.json"
    identity_payload = (
        json.dumps(identity, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    _write_or_verify(identity_path, identity_payload)
    artifacts = {"execution_identity": str(identity_path)}
    if identity["identity_kind"] == "working_tree_snapshot":
        patch_path = snapshot_root / "candidate.patch"
        _write_or_verify(patch_path, patch)
        if _sha256_file(patch_path) != identity["candidate_patch_sha256"]:
            raise RuntimeError("persisted candidate.patch SHA256 mismatch")
        artifacts["candidate_patch"] = str(patch_path)
        tracked_patch_path = snapshot_root / "git-diff-binary-head.patch"
        tracked_patch = _run_git_bytes("diff", "--binary", "HEAD")
        _write_or_verify(tracked_patch_path, tracked_patch)
        if _sha256_file(tracked_patch_path) != identity[
            "git_diff_binary_head_sha256"
        ]:
            raise RuntimeError("persisted git diff --binary HEAD SHA256 mismatch")
        artifacts["git_diff_binary_head"] = str(tracked_patch_path)
    return artifacts


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
    parser.add_argument(
        "--snapshot-allow-file",
        action="append",
        default=[],
        help=(
            "repeat for every validated tracked change; must exactly match "
            "git diff HEAD"
        ),
    )
    parser.add_argument(
        "--snapshot-allow-untracked-file",
        action="append",
        default=[],
        help=(
            "repeat for each validated untracked source/config addition that "
            "must be embedded in candidate.patch"
        ),
    )
    parser.add_argument(
        "--expected-execution-id",
        help="fail closed unless the reconstructed execution identity matches",
    )
    parser.add_argument(
        "--expected-source-manifest-sha256",
        help="fail closed unless every execution-critical source hash matches",
    )
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

    try:
        execution_identity, candidate_patch = _build_execution_identity(
            args.snapshot_allow_file,
            args.snapshot_allow_untracked_file,
        )
        if (
            args.expected_execution_id
            and execution_identity["execution_id"] != args.expected_execution_id
        ):
            raise RuntimeError(
                "execution identity mismatch: "
                f"{execution_identity['execution_id']} != "
                f"{args.expected_execution_id}"
            )
        if (
            args.expected_source_manifest_sha256
            and execution_identity["source_manifest"]["sha256"]
            != args.expected_source_manifest_sha256
        ):
            raise RuntimeError(
                "execution-critical source manifest mismatch: "
                f"{execution_identity['source_manifest']['sha256']} != "
                f"{args.expected_source_manifest_sha256}"
            )
        execution_artifacts = _persist_execution_identity(
            execution_identity, candidate_patch
        )
    except RuntimeError as exc:
        parser.error(str(exc))
    git_commit = execution_identity["base_head"]
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

    execution_identity_path = tag_root / "execution_identity.json"
    _atomic_json(execution_identity_path, execution_identity)
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
        "--execution-identity", str(execution_identity_path),
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
        "schema_version": "r6-bounded-probe-v2",
        "status": "starting",
        "created_at": _utc_now(),
        "git_commit": git_commit,
        "execution_identity": execution_identity,
        "execution_identity_artifacts": execution_artifacts,
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
