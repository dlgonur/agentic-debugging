"""Repair-13 V5 direct-execution regressions; all tests are zero-provider."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_debugger.swerebench.execution import OfficialSWERebenchVerifier
from agentic_debugger.swerebench.schema import classify_execution_result
from scripts import gpt_oss_swerebench_v2_devqual10_v5 as v5
from scripts import gpt_oss_swerebench_v2_pilot10 as pilot
from scripts import ollama_cloud_command_adapter as adapter


ROOT = Path(__file__).resolve().parents[2]


def test_v5_actual_script_entrypoint_resolves_adapter_before_live_guard(tmp_path):
    campaign = tmp_path / "campaign"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/gpt_oss_swerebench_v2_devqual10_v5.py",
            "execute",
            "--config-root", str(tmp_path / "config"),
            "--external-root", str(campaign),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "--live" in output
    assert "No module named 'scripts.ollama_cloud_command_adapter'" not in output
    assert not campaign.exists()


def test_v5_validate_identity_and_frozen_first_ten_are_zero_provider():
    identity = v5.validate_devqual_identity()
    contract = v5.load_devqual_contract()
    assert len(identity["first_ten_instance_ids"]) == 10
    assert contract["provider_generation_calls"] == 0
    assert contract["direct_execution_contract"]["preflight_command"] is False
    assert contract["direct_execution_contract"]["authorize_command"] is False


def test_v5_execute_has_only_explicit_live_and_no_readiness_arguments():
    parser = v5.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["execute", "--config-root", "x", "--preflight-summary", "x"])
    with pytest.raises(SystemExit, match="--live"):
        v5.main(["execute", "--config-root", "x", "--external-root", "y"])


def test_v5_live_execute_calls_shared_runner_in_direct_mode_without_provider(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(v5, "_cheap_guards", lambda _args: ({"harness_sha256": "h" * 64, "runtime_git_head": "g" * 40}, "f" * 64))
    def fake_runner(args, frozen, **kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(v5, "_run_authorized_pilot10", fake_runner)
    assert v5.main([
        "execute", "--live", "--config-root", str(tmp_path / "config"),
        "--external-root", str(tmp_path / "campaign"),
    ]) == 0
    assert calls["readiness_mode"] == "direct"
    assert "preflight_record_dir" not in calls
    assert calls["campaign_metadata"]["readiness_required"] is False


def test_v5_source_contains_no_prerequisite_readiness_gate():
    source = Path(v5.__file__).read_text(encoding="utf-8")
    assert "run_task_preflight" not in source
    assert "run_official_infrastructure_gate" not in source
    assert "preflight_summary" not in source
    assert "preflight_record_dir" not in source
    assert "from scripts.ollama_cloud_command_adapter import resolve_cloud_model" not in source
    assert source.count("from scripts import ollama_cloud_command_adapter as ollama_adapter") == 1


def test_direct_shared_runner_is_explicit_and_historical_default_remains_preflight():
    assert "readiness_mode" in pilot._run_authorized_pilot10.__code__.co_varnames
    assert pilot._run_authorized_pilot10.__code__.co_consts.count("preflight") >= 1


def test_v5_identity_profile_reasoning_and_request_envelope_match_v4():
    contract = v5.load_devqual_contract()
    provider = contract["provider"]
    assert v5.ollama_adapter is adapter
    assert (provider["profile_id"], provider["alias"], provider["upstream"], provider["protocol"]) == (
        "ollama-cloud-gpt-oss-20b", "gpt-oss:20b-cloud", "gpt-oss:20b", "1.3"
    )
    assert provider["reasoning_effort"] == "high"
    assert contract["request_envelope"] == {
        "canonical_public_request_bytes": 128 * 1024,
        "stdin_request_bytes": 192 * 1024,
        "http_request_body_bytes": 256 * 1024,
        "raw_response_bytes": 64 * 1024,
    }
    assert adapter.MAX_PUBLIC_REQUEST_BYTES == contract["request_envelope"]["canonical_public_request_bytes"]
    assert adapter.MAX_STDIN_REQUEST_BYTES == contract["request_envelope"]["stdin_request_bytes"]
    assert adapter.MAX_HTTP_REQUEST_BODY_BYTES == contract["request_envelope"]["http_request_body_bytes"]
    assert adapter.MAX_RAW_RESPONSE_BYTES == contract["request_envelope"]["raw_response_bytes"]
    assert provider["adapter_retry_count"] == 0
    assert provider["fallback_count"] == 0
    assert provider["request_timeout_seconds"] == 60
    assert contract["controller"]["external_patch_gate"] == "successful get_source_window observation required"


def test_campaign_root_guards_reject_repository_and_existing_targets(monkeypatch, tmp_path):
    outside = tmp_path / "campaign"
    args = v5.build_parser().parse_args([
        "execute", "--live", "--config-root", str(tmp_path / "config"),
        "--external-root", str(outside),
    ])
    monkeypatch.setattr(v5, "validate_devqual_identity", lambda project=None: {"experiment_id": v5.DEVQUAL_EXPERIMENT_ID})
    monkeypatch.setattr(v5, "current_git_head", lambda _root: "a" * 40)
    monkeypatch.setattr(v5, "working_tree_dirty", lambda _root: False)
    monkeypatch.setattr(v5, "_validate_profile", lambda _args, _contract: "b" * 64)
    outside.mkdir()
    with pytest.raises(SystemExit, match="already exists"):
        v5._cheap_guards(args)
    inside = ROOT / "_ai-review" / "repair13-test-root"
    args.external_root = str(inside)
    with pytest.raises(SystemExit, match="outside the repository"):
        v5._cheap_guards(args)


def test_direct_setup_failure_is_infrastructure_invalid_without_model_calls(monkeypatch):
    ordered = type("Task", (), {
        "instance_id": "example__repo-1", "repo": "example/repo",
        "base_commit": "a" * 40, "order_index": 1,
    })()
    row = pilot._direct_setup_failure_row(ordered, "swr-example-repo-1", RuntimeError("setup"))
    assert row["science"]["classification"] == "infrastructure_invalid"
    assert row["runtime"]["logical_model_calls"] == 0
    assert row["runtime"]["transport_attempts"] == 0


def _direct_runner_fixture(monkeypatch, tmp_path, *, worker_mode):
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    task = {
        "order_index": 1,
        "instance_id": "example__repo-1",
        "repo": "example/repo",
        "repo_canonical": "example/repo",
        "base_commit": "a" * 40,
        "assignment_key": "b" * 64,
        "first_repo_occurrence": True,
        "license": "MIT",
        "difficulty": "easy",
        "age_bin": "middle",
        "patch_bin": "small",
    }
    (frozen / "pilot10_manifest.json").write_text(
        json.dumps({"tasks": [task]}), encoding="utf-8"
    )
    bundle = type("Bundle", (), {
        "gold_patch": lambda self: "gold secret line long enough",
        "test_patch": lambda self: "test secret line long enough",
        "hidden_tests": lambda self: (("hidden/test.py::test_bug",), ()),
    })()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(pilot, "load_official_bundles", lambda _ids: {task["instance_id"]: bundle})
    monkeypatch.setattr(pilot, "materialize_base_commit", lambda **_kwargs: checkout)
    monkeypatch.setattr(pilot, "production_write_paths", lambda _checkout: ["pkg"])
    monkeypatch.setattr(pilot, "build_model_task", lambda *_args, **_kwargs: type(
        "ModelTask", (), {
            "agent_visible_mapping": lambda self: {"description": "public issue"},
            "to_mapping": lambda self: {"description": "public issue"},
        }
    )())
    monkeypatch.setattr(pilot, "write_private_bundle", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pilot, "create_external_execution_root", lambda path: (Path(path).mkdir(), Path(path))[1])
    monkeypatch.setattr(pilot, "write_json", lambda *_args, **_kwargs: None)
    calls = {"setup_row": 0}
    original_setup_row = pilot._direct_setup_failure_row
    def setup_row(*row_args):
        calls["setup_row"] += 1
        calls["error"] = str(row_args[-1])
        return original_setup_row(*row_args)
    monkeypatch.setattr(pilot, "_direct_setup_failure_row", setup_row)

    class Result:
        def to_mapping(self):
            return {"status": "succeeded"}

    class Worker:
        def __init__(self, *, session_dir, **_kwargs):
            self.session_dir = Path(session_dir)
            self.session_dir.mkdir(parents=True, exist_ok=True)

        def start(self):
            (self.session_dir / "provider.metrics.json").write_text(
                json.dumps({"model_request_count": 3}), encoding="utf-8"
            )
            if worker_mode == "raise":
                raise RuntimeError("post-model worker failure")
            return Result()

        def wait(self):
            return Result()

        def close(self):
            return None

    monkeypatch.setattr(pilot, "SessionWorkerProcess", Worker)
    args = SimpleNamespace(
        external_root=str(tmp_path / "campaign"),
        config_root=str(tmp_path / "config"),
        profile_id=pilot.PROFILE_ID,
    )
    return frozen, args, calls


def test_pre_worker_setup_exception_is_the_only_direct_zero_call_setup_row(monkeypatch, tmp_path):
    frozen, args, calls = _direct_runner_fixture(monkeypatch, tmp_path, worker_mode="raise")
    monkeypatch.setattr(pilot, "materialize_base_commit", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("before worker")))
    rows = {}
    monkeypatch.setattr(pilot, "write_json", lambda _path, payload: rows.update(payload))
    assert pilot._run_authorized_pilot10(args, frozen, profile_fingerprint="c" * 64, readiness_mode="direct") == 0
    assert calls["setup_row"] == 1
    assert rows["rows"][0]["runtime"]["transport_attempts"] == 0
    assert rows["rows"][0]["science"]["classification"] == "infrastructure_invalid"


def test_post_worker_exception_never_uses_setup_row_and_preserves_durable_counts(monkeypatch, tmp_path):
    frozen, args, calls = _direct_runner_fixture(monkeypatch, tmp_path, worker_mode="raise")
    raised = None
    try:
        pilot._run_authorized_pilot10(args, frozen, profile_fingerprint="c" * 64, readiness_mode="direct")
    except RuntimeError as exc:
        raised = exc
    assert raised is not None, calls
    assert str(raised) == "post-model worker failure"
    assert calls["setup_row"] == 0, calls
    metrics = next((tmp_path / "campaign" / "sessions").glob("*/provider.metrics.json"))
    assert json.loads(metrics.read_text(encoding="utf-8"))["model_request_count"] == 3


def test_post_model_row_projection_exception_fails_closed_without_erasing_counts(monkeypatch, tmp_path):
    frozen, args, calls = _direct_runner_fixture(monkeypatch, tmp_path, worker_mode="success")
    monkeypatch.setattr(pilot, "_pilot_row", lambda *args: (_ for _ in ()).throw(RuntimeError("projection failure")))
    raised = None
    try:
        pilot._run_authorized_pilot10(args, frozen, profile_fingerprint="c" * 64, readiness_mode="direct")
    except RuntimeError as exc:
        raised = exc
    assert raised is not None, calls
    assert str(raised) == "projection failure"
    assert calls["setup_row"] == 0, calls
    metrics = next((tmp_path / "campaign" / "sessions").glob("*/provider.metrics.json"))
    assert json.loads(metrics.read_text(encoding="utf-8"))["model_request_count"] == 3


def test_repair13b_restores_parent_v3_v4_validator_source():
    for relative in (
        "agentic_debugger/swerebench/devqual_v3.py",
        "agentic_debugger/swerebench/devqual_v4.py",
    ):
        current = (ROOT / relative).read_text(encoding="utf-8")
        completed = __import__("subprocess").run(
            ["git", "show", f"99abbcdb597d51cc03b6a1230b95b18dc16c6f68:{relative}"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        assert current == completed.stdout


@pytest.mark.parametrize("kwargs, expected", [
    ({"controller_completed": False, "candidate_produced": False, "verifier_ran": False, "verifier_resolved": False, "verifier_infrastructure_valid": True, "runtime_infrastructure_invalid": True}, "infrastructure_invalid"),
    ({"controller_completed": False, "candidate_produced": False, "verifier_ran": False, "verifier_resolved": False, "verifier_infrastructure_valid": True, "provider_invalid": True}, "provider_invalid"),
    ({"controller_completed": False, "candidate_produced": False, "verifier_ran": False, "verifier_resolved": False, "verifier_infrastructure_valid": True}, "model_controller_failure_before_candidate"),
    ({"controller_completed": True, "candidate_produced": True, "verifier_ran": True, "verifier_resolved": False, "verifier_infrastructure_valid": False}, "infrastructure_invalid"),
    ({"controller_completed": True, "candidate_produced": True, "verifier_ran": True, "verifier_resolved": True, "verifier_infrastructure_valid": True}, "independent_verifier_resolved"),
])
def test_direct_classification_matrix(kwargs, expected):
    assert classify_execution_result(**kwargs) == expected


def test_lazy_verifier_environment_failure_is_infrastructure_invalid(monkeypatch, tmp_path):
    class Bundle:
        def hidden_tests(self): return (("tests/test_f2p.py::test_bug",), ())
        def public(self): return None

    bundle = Bundle()
    monkeypatch.setattr("agentic_debugger.swerebench.execution.run_official_baseline_check", lambda *_args, **_kwargs: {
        "ran": False, "verifier_baseline_valid": False, "reason": "docker_unavailable",
    })
    verifier = OfficialSWERebenchVerifier(bundle, work_root=tmp_path, baseline_valid=None)
    result = verifier.evaluate("diff --git a/x b/x\n")
    assert result["verifier_infrastructure_valid"] is False
    assert result["verifier_ran"] is True
    assert result["resolved"] is False
    assert not list(tmp_path.glob("candidate-verification-private-*"))


def test_candidate_verifier_result_is_sole_success_authority(monkeypatch, tmp_path):
    class Public:
        instance_id = "example__repo-1"
        repo = "example/repo"
        base_commit = "a" * 40
        problem_statement = "public issue"
        language = "python"
        license = "MIT"

    class Bundle:
        public = Public()
        def gold_patch(self): return "gold"
        def test_patch(self): return "test"
        def hidden_tests(self): return (("tests/test_f2p.py::test_bug",), ())
        def image_name(self): return "image"
        def install_config(self): return {}

    monkeypatch.setattr("agentic_debugger.swerebench.execution.run_official_baseline_check", lambda *_args, **_kwargs: {
        "ran": True, "verifier_baseline_valid": True,
    })
    def evaluator(spec, report_path, workdir):
        report = {"items": [{"instance_id": "example__repo-1", "from_fail_to_pass": ["tests/test_f2p.py::test_bug"], "failed_from_pass_to_pass": [], "passed_match": True, "error": None}]}
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return {"report": report, "exit_code": 0}
    verifier = OfficialSWERebenchVerifier(Bundle(), work_root=tmp_path, baseline_valid=None, evaluate_fn=evaluator)
    result = verifier.evaluate("diff --git a/x b/x\n")
    assert result["verifier_infrastructure_valid"] is True
    assert result["verifier_outcome"] == "RESOLVED"
    assert result["resolved"] is True


def test_historical_v1_v2_v3_v4_frozen_files_are_not_mutated_by_v5_validate():
    roots = [ROOT / "experiments" / name / "frozen" for name in [
        "gpt_oss_swerebench_v2_pilot10", "gpt_oss_swerebench_v2_devqual10_v2",
        "gpt_oss_swerebench_v2_devqual10_v3", "gpt_oss_swerebench_v2_devqual10_v4",
    ]]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for root in roots for path in root.rglob("*") if path.is_file()}
    assert v5.main(["validate"]) == 0
    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for root in roots for path in root.rglob("*") if path.is_file()}
    assert before == after


def test_v5_no_real_provider_generations_are_recorded():
    assert pilot.provider_execution_truth([]) == {
        "provider_execution_authorized": True,
        "provider_inference_started": False,
        "tasks_with_transport_attempts": 0,
        "transport_attempts": 0,
        "provider_generation_calls": 0,
    }
