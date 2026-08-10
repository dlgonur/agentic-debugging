import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def _import_runner():
    repo = Path(__file__).resolve().parents[2]
    script = repo / "experiments" / "tuned_debugger_pilot_v1" / "run_pilot.py"
    spec = importlib.util.spec_from_file_location("tuned_pilot_runner", script)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(repo))
    spec.loader.exec_module(module)
    return module


def test_frozen_tuned_debugger_pilot_validate_only() -> None:
    repo = Path(__file__).resolve().parents[2]
    script = repo / "experiments" / "tuned_debugger_pilot_v1" / "run_pilot.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--validate-only"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    payload = json.loads(completed.stdout)
    assert payload["status"] == "PASS"
    assert payload["validated_case_count"] == 10
    assert len(payload["task_evidence"]) == 5
    assert all(
        item["agent_visible_mapping_identical_A_B"] is True
        for item in payload["task_evidence"].values()
    )


def test_real_pilot_fails_closed_without_chat_b_adapter() -> None:
    repo = Path(__file__).resolve().parents[2]
    script = repo / "experiments" / "tuned_debugger_pilot_v1" / "run_pilot.py"
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode != 0
    assert "waiting for frozen tuned adapter from Chat B" in (
        completed.stdout + completed.stderr
    )


def test_base_only_and_adapter_path_are_mutually_exclusive() -> None:
    repo = Path(__file__).resolve().parents[2]
    script = repo / "experiments" / "tuned_debugger_pilot_v1" / "run_pilot.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--base-only",
            "--adapter-path",
            "some-dir",
            "--output-dir",
            "some-out",
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode != 0
    assert "--base-only and --adapter-path are mutually exclusive" in (
        completed.stdout + completed.stderr
    )


def test_base_only_does_not_require_adapter_files() -> None:
    repo = Path(__file__).resolve().parents[2]
    script = repo / "experiments" / "tuned_debugger_pilot_v1" / "run_pilot.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--base-only"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "--output-dir is required for a real pilot run" in output
    assert "waiting for frozen tuned adapter from Chat B" not in output


def test_tuned_mode_still_validates_adapter_identity_fail_closed(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    script = repo / "experiments" / "tuned_debugger_pilot_v1" / "run_pilot.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--adapter-path",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "out"),
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "adapter_config.json is missing from tuned adapter" in output


def test_frozen_model_contract_unchanged() -> None:
    runner = _import_runner()
    contract = runner._load_contract()
    model = contract["model"]
    assert model["base_repository"] == "Qwen/Qwen2.5-Coder-7B-Instruct"
    assert model["base_revision"] == "c03e6d358207e414f1eca0bb1891e29f1db0e242"
    assert model["generation"] == {
        "do_sample": False,
        "max_new_tokens": 1024,
        "max_input_tokens": 32768,
    }
    assert [item["condition_id"] for item in contract["conditions"]] == [
        "A-static-repair",
        "B-debugger-assisted",
    ]
    assert [item["policy"] for item in contract["conditions"]] == [
        "static-baseline",
        "pdb-on-uncertainty",
    ]


def test_transport_system_prompt_unchanged() -> None:
    runner = _import_runner()
    assert runner.LocalQwenPeftTransport.SYSTEM_PROMPT == (
        "You are the model component of a typed debugging controller. "
        "Return exactly one JSON directive object and no prose, markdown, or "
        "analysis. Obey only the directive_schema, allowed_actions, "
        "legal_transition_targets, and action_contracts in the user payload."
    )


def test_run_identity_distinguishes_raw_from_tuned() -> None:
    runner = _import_runner()
    validation = {
        "contract_sha256": "a" * 64,
        "task_evidence": {},
        "validated_case_count": 10,
    }
    model_contract = {
        "base_repository": "Qwen/Qwen2.5-Coder-7B-Instruct",
        "base_revision": "c03e6d358207e414f1eca0bb1891e29f1db0e242",
    }
    adapter = {"path": "C:\\adapter", "tree_identity_sha256": "b" * 64, "files": []}
    task_ids = ("curated-none-handling-001",)
    conditions = ["A-static-repair", "B-debugger-assisted"]

    tuned = runner._run_identity(
        validation,
        model_contract,
        adapter,
        task_ids,
        conditions,
        base_only=False,
        chat_template="<chat-template>",
    )
    raw = runner._run_identity(
        validation,
        model_contract,
        None,
        task_ids,
        conditions,
        base_only=True,
        chat_template="<chat-template>",
    )

    assert tuned["adapter_identity"] == adapter
    assert "model_condition" not in tuned
    assert raw["model_condition"] == "RAW_BASE"
    assert raw["adapter_applied"] is False
    assert raw["adapter_path"] is None
    assert raw["adapter_identity"] is None
    assert raw["base_repository"] == "Qwen/Qwen2.5-Coder-7B-Instruct"
    assert raw["base_revision"] == "c03e6d358207e414f1eca0bb1891e29f1db0e242"
    expected_chat = hashlib.sha256(b"<chat-template>").hexdigest()
    assert raw["tokenizer_identity"]["chat_template_sha256"] == expected_chat
    assert raw["task_ids"] == list(task_ids)
    assert raw["conditions"] == conditions
