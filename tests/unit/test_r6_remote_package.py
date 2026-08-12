from __future__ import annotations

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
    )

    assert command[:2] == [
        "python",
        "experiments/r6_debugger_training/run_bounded_probe.py",
    ]
    assert command[command.index("--suite") + 1] == "curated-holdout"
    assert command[command.index("--stage") + 1] == "C"
    assert command[-2:] == ["--task", "curated-none-handling-001"]


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
