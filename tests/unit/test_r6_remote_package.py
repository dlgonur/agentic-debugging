from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from experiments.r6_debugger_training import prepare_remote_package as package
from experiments.r6_debugger_training import run_bounded_probe as bounded_probe


def test_checkpoint_identity_input_requires_only_inference_files(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint-30"
    checkpoint.mkdir()
    (checkpoint / "adapter_config.json").write_text("{}", encoding="utf-8")
    (checkpoint / "adapter_model.safetensors").write_bytes(b"weights")
    (checkpoint / "optimizer.pt").write_bytes(b"must-not-be-packaged")

    label, resolved = package._checkpoint(f"checkpoint-30={checkpoint}")

    assert label == "checkpoint-30"
    assert resolved == checkpoint.resolve()
    assert package.INFERENCE_FILES == (
        "adapter_config.json",
        "adapter_model.safetensors",
    )


def test_remote_command_pins_suite_stage_and_persistent_wrapper() -> None:
    command = package._command(
        "checkpoint-30",
        tag="final",
        stage="C",
        suite="curated-holdout",
        tasks=("curated-none-handling-001",),
        execution_id="wt-base-patch",
        source_manifest_sha256="f" * 64,
        snapshot_files=("experiments/r6_debugger_training/run_bounded_probe.py",),
        snapshot_untracked_files=(
            "experiments/r6_debugger_training/prepare_remote_package.py",
        ),
    )

    assert command[:2] == [
        "python",
        "experiments/r6_debugger_training/run_bounded_probe.py",
    ]
    assert command[command.index("--suite") + 1] == "curated-holdout"
    assert command[command.index("--stage") + 1] == "C"
    assert command[command.index("--expected-execution-id") + 1] == "wt-base-patch"
    assert command[command.index("--expected-source-manifest-sha256") + 1] == (
        "f" * 64
    )
    assert command[command.index("--snapshot-allow-file") + 1] == (
        "experiments/r6_debugger_training/run_bounded_probe.py"
    )
    assert command[command.index("--snapshot-allow-untracked-file") + 1] == (
        "experiments/r6_debugger_training/prepare_remote_package.py"
    )
    assert command[-2:] == ["--task", "curated-none-handling-001"]


def test_remote_training_command_pins_source_and_sft_identities() -> None:
    command = package._training_command(
        execution_id="wt-base-patch",
        source_manifest_sha256="a" * 64,
        sft_manifest_sha256="b" * 64,
        snapshot_files=("experiments/r6_debugger_training/train_qlora.py",),
        snapshot_untracked_files=(
            "experiments/r6_debugger_training/run_bounded_training.py",
        ),
    )

    assert command[:2] == [
        "python",
        "experiments/r6_debugger_training/run_bounded_training.py",
    ]
    assert command[command.index("--sft-dir") + 1] == "${PACKAGE_ROOT}/training-data"
    assert command[command.index("--expected-execution-id") + 1] == "wt-base-patch"
    assert command[command.index("--expected-sft-manifest-sha256") + 1] == "b" * 64
    assert "experiments/r6_debugger_training/run_bounded_training.py" in command


def test_bounded_environment_resolves_child_python_to_evaluator() -> None:
    environment, policy = bounded_probe._evaluator_environment()

    assert environment["PYTORCH_CUDA_ALLOC_CONF"] == "backend:cudaMallocAsync"
    assert Path(policy["resolved_python_command"]).samefile(Path(sys.executable))
    assert environment["PATH"].split(bounded_probe.os.pathsep)[0] == str(
        Path(sys.executable).resolve().parent
    )


def test_windows_child_cwd_policy_rejects_deep_evidence_layout() -> None:
    deep_root = Path("C:/") / ("deep-segment" * 20)

    try:
        bounded_probe._child_cwd_policy(
            deep_root,
            tag="stage-a",
            label="adapter-checkpoint-30",
            task_ids=["quixbugs-depth-first-search"],
            platform_name="nt",
        )
    except RuntimeError as exc:
        assert "child cwd is too long" in str(exc)
    else:
        raise AssertionError("unsafe Windows child cwd was accepted")


def test_windows_child_cwd_policy_records_short_layout() -> None:
    policy = bounded_probe._child_cwd_policy(
        Path("C:/r6"),
        tag="a",
        label="adapter-checkpoint-30",
        task_ids=["quixbugs-depth-first-search"],
        platform_name="nt",
    )

    assert policy["passed"] is True
    assert policy["predicted_paths"][0]["characters"] <= 248


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _snapshot_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", bounded_probe.EXPECTED_R6_BRANCH)
    _git(repo, "config", "user.email", "snapshot-test@example.invalid")
    _git(repo, "config", "user.name", "Snapshot Test")
    source = repo / "experiments/r6_debugger_training/run_bounded_probe.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 'base'\n", encoding="utf-8")
    _git(repo, "add", "--", source.relative_to(repo).as_posix())
    _git(repo, "commit", "-m", "base")
    return repo, source


def test_working_tree_snapshot_is_exact_hashed_and_reconstructable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, source = _snapshot_repo(tmp_path)
    relative = source.relative_to(repo).as_posix()
    source.write_text("VALUE = 'bounded repair'\n", encoding="utf-8")
    monkeypatch.setattr(bounded_probe, "REPO_ROOT", repo)

    identity, patch = bounded_probe._build_execution_identity([relative])

    assert identity["identity_kind"] == "working_tree_snapshot"
    assert identity["branch"] == bounded_probe.EXPECTED_R6_BRANCH
    assert identity["base_head"] == _git(repo, "rev-parse", "HEAD")
    assert identity["candidate_patch_sha256"] == hashlib.sha256(patch).hexdigest()
    assert identity["changed_files"] == [relative]
    assert identity["changed_file_identities"] == [
        {
            "path": relative,
            "bytes": source.stat().st_size,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    ]
    assert identity["reconstruction"]["passed"] is True
    assert identity["confirmations"] == {
        "tracked_worktree_clean": False,
        "tracked_changes_exactly_allowlisted": True,
        "unrelated_tracked_changes_present": False,
        "untracked_execution_critical_source_consumed": False,
        "git_diff_check_passed": True,
    }

    artifacts = bounded_probe._persist_execution_identity(identity, patch)
    patch_path = Path(artifacts["candidate_patch"])
    assert patch_path.read_bytes() == patch
    assert hashlib.sha256(patch_path.read_bytes()).hexdigest() == identity[
        "candidate_patch_sha256"
    ]


def test_snapshot_rejects_unrelated_or_untracked_execution_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, source = _snapshot_repo(tmp_path)
    relative = source.relative_to(repo).as_posix()
    source.write_text("VALUE = 'bounded repair'\n", encoding="utf-8")
    monkeypatch.setattr(bounded_probe, "REPO_ROOT", repo)

    try:
        bounded_probe._build_execution_identity([])
    except RuntimeError as exc:
        assert "exactly equal" in str(exc)
    else:
        raise AssertionError("unallowlisted tracked change was accepted")

    untracked = repo / "experiments/r6_debugger_training/untracked_config.py"
    untracked.write_text("UNSAFE = True\n", encoding="utf-8")
    try:
        bounded_probe._build_execution_identity([relative])
    except RuntimeError as exc:
        assert "untracked execution-critical" in str(exc)
    else:
        raise AssertionError("untracked execution source was accepted")

    identity, patch = bounded_probe._build_execution_identity(
        [relative],
        [untracked.relative_to(repo).as_posix()],
    )
    assert identity["captured_untracked_files"] == [
        untracked.relative_to(repo).as_posix()
    ]
    assert identity["candidate_patch_sha256"] == hashlib.sha256(patch).hexdigest()
    assert identity["reconstruction"]["verified_file_count"] == 2
