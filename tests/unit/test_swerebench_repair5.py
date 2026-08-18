from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agentic_debugger.swerebench.execution import DockerPublicPytestRunner, OfficialSWERebenchVerifier
from agentic_debugger.swerebench.official_eval import _summarize_item
from agentic_debugger.swerebench.records import (
    OfficialInstanceBundle,
    PublicInstanceRecord,
    VerifierPrivateRecord,
)
from agentic_debugger.swerebench.schema import PilotResultSchemaError, validate_pilot_result
from scripts.gpt_oss_swerebench_v2_pilot10 import _pilot_row


def _bundle(f2p_count: int = 5, p2p_count: int = 0) -> OfficialInstanceBundle:
    instance_id = "example__repo-12"
    f2p = tuple(f"tests/test_hidden.py::test_{index}" for index in range(f2p_count))
    p2p = tuple(f"tests/test_public.py::test_{index}" for index in range(p2p_count))
    public = PublicInstanceRecord(
        instance_id=instance_id,
        repo="example/repo",
        base_commit="a" * 40,
        problem_statement="A public issue.",
        language="python",
        license="MIT",
        created_at="2024-01-01",
        problem_statement_sha256="c" * 64,
    )
    private = VerifierPrivateRecord(
        instance_id=instance_id,
        fail_to_pass=f2p,
        pass_to_pass=p2p,
        test_cmd="pytest",
        image_name="example/image:1",
        python_version="3.11",
        has_gold_patch=True,
        has_test_patch=True,
        gold_patch_sha256="d" * 64,
        test_patch_sha256="e" * 64,
    )
    return OfficialInstanceBundle(
        public=public,
        private=private,
        _gold_patch="GOLD-ONLY",
        _test_patch="TEST-PATCH-ONLY",
        _fail_to_pass=f2p,
        _pass_to_pass=p2p,
        _test_cmd="pytest",
        _install_config={},
        _image_name=private.image_name,
    )


def _item(bundle: OfficialInstanceBundle, passing: int, *, p2p_failed: int = 0) -> dict:
    return {
        "instance_id": bundle.public.instance_id,
        "from_fail_to_pass": list(bundle.hidden_tests()[0][:passing]),
        "failed_from_pass_to_pass": list(bundle.hidden_tests()[1][:p2p_failed]),
        "passed_match": passing == len(bundle.hidden_tests()[0]) and p2p_failed == 0,
        "error": "",
    }


@pytest.mark.parametrize("passing", [0, 1, 4])
def test_partial_f2p_never_resolves(passing: int):
    bundle = _bundle()
    summary = _summarize_item(
        _item(bundle, passing),
        empty_p2p=True,
        requested_instance_id=bundle.public.instance_id,
        expected_f2p_count=5,
        expected_p2p_count=0,
    )
    assert summary["valid_result"] is True
    assert summary["passed_match"] is False


def test_complete_f2p_and_clean_empty_p2p_resolves():
    bundle = _bundle()
    summary = _summarize_item(
        _item(bundle, 5),
        empty_p2p=True,
        requested_instance_id=bundle.public.instance_id,
        expected_f2p_count=5,
        expected_p2p_count=0,
    )
    assert summary["passed_match"] is True


def test_f2p_complete_but_p2p_regression_is_unresolved():
    bundle = _bundle(p2p_count=1)
    summary = _summarize_item(
        _item(bundle, 5, p2p_failed=1),
        empty_p2p=False,
        requested_instance_id=bundle.public.instance_id,
        expected_f2p_count=5,
        expected_p2p_count=1,
    )
    assert summary["passed_match"] is False


def test_exit_one_is_valid_for_an_ordinary_unresolved_result(tmp_path: Path):
    bundle = _bundle()

    def evaluate(_spec, _report_path, _workdir):
        return {"exit_code": 1, "report": {"items": [_item(bundle, 1)]}}

    result = OfficialSWERebenchVerifier(
        bundle, work_root=tmp_path, baseline_valid=True, evaluate_fn=evaluate
    ).evaluate("candidate")
    assert result["verifier_infrastructure_valid"] is True
    assert result["verifier_outcome"] == "UNRESOLVED"


@pytest.mark.parametrize(
    "report",
    [None, {"items": []}, {"items": [{"instance_id": "wrong"}]}],
)
def test_missing_malformed_or_wrong_task_is_infrastructure_invalid(tmp_path: Path, report):
    bundle = _bundle()

    def evaluate(_spec, _report_path, _workdir):
        return {"exit_code": 1, "report": report}

    result = OfficialSWERebenchVerifier(
        bundle, work_root=tmp_path, baseline_valid=True, evaluate_fn=evaluate
    ).evaluate("candidate")
    assert result["verifier_infrastructure_valid"] is False
    assert result["verifier_outcome"] == "UNRESOLVED"


def test_evaluator_launch_failure_is_infrastructure_invalid(tmp_path: Path):
    bundle = _bundle()

    def evaluate(_spec, _report_path, _workdir):
        raise OSError("synthetic evaluator launch failure")

    result = OfficialSWERebenchVerifier(
        bundle, work_root=tmp_path, baseline_valid=True, evaluate_fn=evaluate
    ).evaluate("candidate")
    assert result["verifier_infrastructure_valid"] is False
    assert "launch failure" in result["official_error"]


def test_docker_runner_translates_pythonpath_and_preserves_candidate_precedence(
    tmp_path: Path, monkeypatch
):
    workspace = tmp_path / "workspace"
    (workspace / "src" / "probe").mkdir(parents=True)
    (workspace / "src" / "probe" / "__init__.py").write_text(
        "VALUE = 'A'\n", encoding="utf-8"
    )
    (workspace / "test_probe.py").write_text(
        "from probe import VALUE\n\ndef test_value():\n    assert VALUE == 'A'\n",
        encoding="utf-8",
    )
    captured = []
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        captured.append((command, kwargs))
        image_index = command.index("example/image:1")
        container_env = dict(os.environ)
        for index, value in enumerate(command):
            if value == "--env":
                name, env_value = command[index + 1].split("=", 1)
                if name == "PYTHONPATH":
                    env_value = os.pathsep.join(
                        part.replace("/workspace", str(workspace))
                        for part in env_value.split(":")
                    )
                else:
                    env_value = env_value.replace("/workspace", str(workspace))
                container_env[name] = env_value
        guest_args = command[image_index + 1 :]
        result = real_run(
            ["python", *guest_args[1:]],
            cwd=workspace,
            env=container_env,
            capture_output=True,
            text=True,
            check=False,
        )
        return subprocess.CompletedProcess(command, result.returncode, result.stdout, result.stderr)

    monkeypatch.setattr("agentic_debugger.swerebench.execution.subprocess.run", fake_run)
    runner = DockerPublicPytestRunner("example/image:1", root=tmp_path)
    first = runner.run(
        ["/opt/swe-rebench/python", "-m", "pytest", "test_probe.py"],
        str(workspace),
        10,
        {"PYTHONPATH": str(workspace), "PYTHONUNBUFFERED": "1"},
    )
    (workspace / "src" / "probe" / "__init__.py").write_text(
        "VALUE = 'B'\n", encoding="utf-8"
    )
    (workspace / "test_probe.py").write_text(
        "from probe import VALUE\n\ndef test_value():\n    assert VALUE == 'B'\n",
        encoding="utf-8",
    )
    second = runner.run(
        ["/opt/swe-rebench/python", "-m", "pytest", "test_probe.py"],
        str(workspace),
        10,
        {"PYTHONPATH": str(workspace), "PYTHONUNBUFFERED": "1"},
    )
    assert first.exit_code == second.exit_code == 0
    command = captured[-1][0]
    assert any("PYTHONPATH=/workspace/src:/workspace" == item for item in command)
    assert f"PYTHONPATH={workspace}" not in " ".join(command)
    assert command[command.index("--workdir") + 1] == "/workspace"


def _ordered():
    from agentic_debugger.swerebench.selection import OrderedTask

    return OrderedTask(
        order_index=1,
        instance_id="example__repo-12",
        repo="example/repo",
        repo_canonical="example/repo",
        base_commit="a" * 40,
        assignment_key="b" * 64,
        first_repo_occurrence=True,
        license="MIT",
        difficulty="medium",
        age_bin="middle",
        patch_bin="small",
    )


def test_pilot_row_extracts_metrics_and_distinguishes_provider_failure(tmp_path: Path):
    session = tmp_path / "session"
    session.mkdir()
    (session / "candidate.patch").write_text("candidate", encoding="utf-8")
    (session / "metrics.json").write_text(
        json.dumps(
            {
                "wall_clock_seconds": 12.5,
                "model_request_count": 3,
                "transport_attempts": 5,
                "retry_count": 2,
                "fallback_count": 1,
                "token_usage": {"total_tokens": 99},
                "provider_error_count": 0,
            }
        ),
        encoding="utf-8",
    )
    evaluation = {
        "verifier_ran": True,
        "verifier_infrastructure_valid": True,
        "resolved": False,
        "verifier_outcome": "UNRESOLVED",
    }
    row = _pilot_row(
        _ordered(),
        "swr-example-repo-12",
        "session-1",
        {"status": "succeeded", "termination_reason": "done"},
        evaluation,
        session,
    )
    assert row["runtime"]["wall_clock_seconds"] == 12.5
    # Logical calls require durable controller journal boundaries.  This
    # synthetic metrics-only session has none; model_request_count is the
    # adapter's transport-attempt count, not a logical-call substitute.
    assert row["runtime"]["logical_model_calls"] == 0
    assert row["runtime"]["transport_attempts"] == 3
    assert row["runtime"]["adapter_retry_count"] == 2
    assert row["runtime"]["fallback_count"] == 1
    assert row["science"]["classification"] == "admissible_unresolved"
    assert row["science"]["execution_classification"] == "independent_verifier_unresolved"

    (session / "metrics.json").write_text(
        json.dumps({"provider_error_count": 1, "termination_reason": "provider_or_transport_error"}),
        encoding="utf-8",
    )
    failed = _pilot_row(
        _ordered(),
        "swr-example-repo-12",
        "session-2",
        {"status": "failed", "termination_reason": "model_error"},
        {},
        session,
    )
    assert failed["science"]["provider_invalid"] is True
    assert failed["science"]["classification"] == "provider_invalid"


def test_schema_rejects_contradictory_science_classification(tmp_path: Path):
    session = tmp_path / "session"
    session.mkdir()
    row = _pilot_row(
        _ordered(),
        "swr-example-repo-12",
        "session-1",
        {"status": "succeeded", "termination_reason": "done"},
        {},
        session,
    )
    row["science"]["classification"] = "infrastructure_invalid"
    with pytest.raises(PilotResultSchemaError, match="classification"):
        validate_pilot_result(row)
