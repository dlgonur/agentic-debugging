from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.external_runtime import (
    PublicRuntimeClassification,
    classify_public_runtime_result,
)
from agentic_debugger.demo.tools import DemoToolContext, build_registry
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.events.schema import Action, ObservationStatus
from agentic_debugger.runtime.command_runner import CommandResult
from agentic_debugger.runtime.execution import (
    ContainmentGuarantee,
    DependencyPreparation,
    PreparedEnvironment,
    VerifiedExecutionContext,
)
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.swerebench.execution import OfficialSWERebenchVerifier
from agentic_debugger.swerebench.mapping import build_model_task
from agentic_debugger.swerebench.records import (
    OfficialInstanceBundle,
    PublicInstanceRecord,
    VerifierPrivateRecord,
)
from agentic_debugger.swerebench.schema import classify_execution_result
from agentic_debugger.swerebench.selection import OrderedTask


class FakeContainmentRunner:
    runner_id = "fake-swerebench-runner"

    def __init__(self, output: str, exit_code: int = 1) -> None:
        self.output = output
        self.exit_code = exit_code
        self.calls: list[tuple[list[str], str, float, dict[str, str]]] = []
        self.boundary_guarantee = {}

    def run(self, argv, cwd, timeout_seconds, env):
        self.calls.append((list(argv), cwd, timeout_seconds, dict(env)))
        return CommandResult(
            argv=list(argv),
            cwd=cwd,
            exit_code=self.exit_code,
            timed_out=False,
            duration_ms=1,
            stdout=self.output,
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        )


def _context(tmp_path: Path, runner: FakeContainmentRunner) -> VerifiedExecutionContext:
    root = tmp_path.resolve()
    containment = ContainmentGuarantee(
        str(root), runner.runner_id, resource_limits={"timeout": "fake"}
    )
    runner.boundary_guarantee = containment.to_mapping()
    dependencies = DependencyPreparation(
        pilot_task_id="swr-example",
        manifest_fingerprint="m" * 64,
        authority_revision="a" * 40,
        project="example/repo",
        bug_id="example-1",
        buggy_revision="b" * 40,
        recipe_path="fake-image",
        recipe_sha256="r" * 64,
        installed_fingerprint="i" * 64,
    )
    environment = PreparedEnvironment(
        python_executable="C:/opt/python/bin/python",
        python_version="3.11",
        project_cwd=".",
        pythonpath=(".",),
        environment={},
        dependencies=dependencies,
    )
    return VerifiedExecutionContext(environment, containment, runner)


def _task() -> DebugTask:
    ordered = OrderedTask(
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
    public = PublicInstanceRecord(
        instance_id=ordered.instance_id,
        repo=ordered.repo,
        base_commit=ordered.base_commit,
        problem_statement="A public issue.",
        language="python",
        license="MIT",
        created_at="2024-01-01",
        problem_statement_sha256="c" * 64,
    )
    private = VerifierPrivateRecord(
        instance_id=ordered.instance_id,
        fail_to_pass=("tests/test_hidden.py::test_bug",),
        pass_to_pass=(),
        test_cmd="pytest",
        image_name="example/image:1",
        python_version="3.11",
        has_gold_patch=True,
        has_test_patch=True,
        gold_patch_sha256="d" * 64,
        test_patch_sha256="e" * 64,
    )
    bundle = OfficialInstanceBundle(
        public=public,
        private=private,
        _gold_patch="GOLD-ONLY",
        _test_patch="TEST-PATCH-ONLY",
        _fail_to_pass=private.fail_to_pass,
        _pass_to_pass=(),
        _test_cmd="pytest",
        _install_config={},
        _image_name=private.image_name,
    )
    return build_model_task(
        ordered, bundle, fixture_path=".", allowed_write_paths=["pkg"]
    )


def _action(task_id: str, arguments: dict) -> Action:
    return Action(
        action_id="action-000000000",
        run_id="run-1",
        task_id=task_id,
        state=ControllerState.REPRODUCE,
        name="run_reproduction",
        arguments=arguments,
    )


def test_public_runtime_classification_never_turns_import_failure_into_reproduction():
    dependency = SimpleNamespace(
        timed_out=False,
        launch_error=False,
        passed=False,
        command_result=SimpleNamespace(
            exit_code=2,
            stdout="ERROR collecting tests/test_public.py",
            stderr="ModuleNotFoundError: No module named 'dependency'",
        ),
    )
    assert classify_public_runtime_result(dependency) is PublicRuntimeClassification.DEPENDENCY_FAILURE
    assert classify_execution_result(
        controller_completed=True,
        candidate_produced=False,
        verifier_ran=False,
        verifier_resolved=False,
        verifier_infrastructure_valid=True,
    ) == "admissible_model_failure"


def test_external_reproduction_uses_verified_runner_and_sets_only_genuine_failure(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "pkg" / "test_public.py").write_text(
        "def test_public():\n    assert False\n", encoding="utf-8"
    )
    workspace = TaskWorkspace(str(root), parent_dir=str(tmp_path))
    runner = FakeContainmentRunner("pkg/test_public.py::test_public FAILED\n1 failed in 0.01s")
    context = DemoToolContext(
        task=_task(),
        workspace=workspace,
        patch="",
        probe=None,
        execution_context=_context(tmp_path, runner),
    )
    try:
        observation = build_registry(context).dispatch(
            _action(context.task.task_id, {"phase": "baseline", "public_target": "pkg/test_public.py"}),
            observation_id="observation-000000000",
        )
        assert observation.status is ObservationStatus.OK
        assert observation.payload["failure_reproduced"] is True
        assert observation.payload["runtime_classification"] == "target_test_failed"
        assert runner.calls
        assert runner.calls[0][0][1:3] == ["-m", "pytest"]
    finally:
        workspace.cleanup()


def test_official_candidate_adapter_receives_candidate_and_cleans_private_files(tmp_path: Path):
    task = _task()
    # Reconstruct the small private bundle used by the model task helper.
    public = PublicInstanceRecord(
        instance_id="example__repo-12",
        repo="example/repo",
        base_commit="a" * 40,
        problem_statement="A public issue.",
        language="python",
        license="MIT",
        created_at="2024-01-01",
        problem_statement_sha256="c" * 64,
    )
    private = VerifierPrivateRecord(
        instance_id=public.instance_id,
        fail_to_pass=("tests/test_hidden.py::test_bug",),
        pass_to_pass=(),
        test_cmd="pytest",
        image_name="example/image:1",
        python_version="3.11",
        has_gold_patch=True,
        has_test_patch=True,
        gold_patch_sha256="d" * 64,
        test_patch_sha256="e" * 64,
    )
    bundle = OfficialInstanceBundle(
        public=public,
        private=private,
        _gold_patch="GOLD-ONLY",
        _test_patch="TEST-PATCH-ONLY",
        _fail_to_pass=private.fail_to_pass,
        _pass_to_pass=(),
        _test_cmd="pytest",
        _install_config={},
        _image_name=private.image_name,
    )
    candidate = "diff --git a/pkg/mod.py b/pkg/mod.py\n+candidate\n"

    def fake_eval(spec_path: Path, report_path: Path, _workdir: Path):
        spec = json.loads(spec_path.read_text(encoding="utf-8"))[0]
        resolved = spec["patch"] == candidate
        assert spec["test_patch"] == "TEST-PATCH-ONLY"
        assert "tests/test_hidden.py::test_bug" in spec["FAIL_TO_PASS"]
        report_path.write_text(
            json.dumps({
                "items": [{
                    "instance_id": "example__repo-12",
                    "from_fail_to_pass": ["tests/test_hidden.py::test_bug"] if resolved else [],
                    "failed_from_pass_to_pass": [],
                    "passed_match": resolved,
                    "error": None,
                }]
            }),
            encoding="utf-8",
        )
        return {"exit_code": 0, "report": json.loads(report_path.read_text(encoding="utf-8"))}

    verifier = OfficialSWERebenchVerifier(
        bundle,
        work_root=tmp_path,
        baseline_valid=True,
        evaluate_fn=fake_eval,
    )
    result = verifier.evaluate(candidate)
    assert result["verifier_outcome"] == "RESOLVED"
    assert result["pass_to_pass"]["empty"] is True
    assert result["cleanup"] is True
    assert not (tmp_path / "candidate-verification-private" / "candidate.json").exists()
    unresolved = verifier.evaluate("NOOP-CANDIDATE")
    assert unresolved["verifier_outcome"] == "UNRESOLVED"
    assert task.source is not None
