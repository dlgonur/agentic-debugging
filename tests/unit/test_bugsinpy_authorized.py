from __future__ import annotations

import json
import importlib
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_debugger.bugsinpy import (
    BugsInPyAdapter,
    ContainmentGuarantee,
    DependencyPreparation,
    ExternalWorkspace,
    GateName,
    GitSourceAcquirer,
    NoModelSmokeRunner,
    PdbLaunchPlan,
    PreflightFacts,
    PreparedEnvironment,
    TaskSource,
    VerifiedExecutionContext,
)
from agentic_debugger.evaluation.runner import EvaluationInputError
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.runtime.command_runner import CommandResult, CommandRunner
from agentic_debugger.runtime.workspace import TaskWorkspace

adapter_module = importlib.import_module("agentic_debugger.bugsinpy.adapter")

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "research" / "bugsinpy" / "PILOT_ELIGIBILITY_MANIFEST_V1.json"


def authorized_adapter(tmp_path: Path) -> BugsInPyAdapter:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for entry in data["tasks"]:
        entry["licensing"]["status"] = "cleared"
        entry["licensing"]["underlying_project_license"] = "MIT"
        entry["environment"]["official_dependency_recipe_sha256"] = "a" * 64
        entry["environment"]["pythonpath"] = "src"
        entry["environment"]["reviewed_environment"] = {"LANG": "C"}
        entry["reproduction"]["cwd"] = "."
        entry["debugger_relevance"]["candidate_changed_files"] = ["src/target.py"]
        entry["debugger_relevance"]["pdb_driver"] = "driver.py"
        entry["debugger_relevance"]["reviewed_breakpoints"] = [10]
        entry["debugger_relevance"]["target_symbols"] = "reviewed.target"
    path = tmp_path / "authorized-manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return BugsInPyAdapter.from_manifest(path)


class FakeContainmentRunner:
    runner_id = "fake-contained"

    def __init__(self) -> None:
        self.calls = []
        self.boundary_guarantee = {}

    def run(self, argv, cwd, timeout_seconds, env):
        self.calls.append((argv, cwd, timeout_seconds, env))
        return CommandResult(list(argv), cwd, 0, False, 1, "", "", False, False)


def context(tmp_path: Path, adapter: BugsInPyAdapter) -> tuple[VerifiedExecutionContext, FakeContainmentRunner]:
    runner = FakeContainmentRunner()
    entry = adapter.select("bugsinpy-tqdm-003")
    project = entry["bugsinpy"]
    python_executable = tmp_path / "venv" / "bin" / "python"
    python_executable.parent.mkdir(parents=True, exist_ok=True)
    python_executable.write_text("fake interpreter", encoding="utf-8")
    deps = DependencyPreparation(
        "bugsinpy-tqdm-003",
        adapter.manifest.fingerprint,
        adapter.manifest.authority_revision,
        project["project"],
        str(project["bug_id"]),
        project["buggy_revision"],
        entry["environment"]["official_dependency_recipe"],
        "a" * 64,
        "b" * 64,
    )
    environment = PreparedEnvironment(
        str(python_executable), "3.6.9", ".", ("src",), {"LANG": "C"}, deps
    )
    containment = ContainmentGuarantee(
        str(tmp_path.resolve()), runner.runner_id, resource_limits={"cpu": "1", "memory": "256m"}
    )
    runner.boundary_guarantee = containment.to_mapping()
    return VerifiedExecutionContext(environment, containment, runner), runner


def facts(adapter: BugsInPyAdapter, tmp_path: Path) -> PreflightFacts:
    verified, _ = context(tmp_path, adapter)
    entry = adapter.select("bugsinpy-tqdm-003")
    f2p = adapter.normalize(entry).fail_to_pass[0]
    return PreflightFacts(
        platform="linux",
        pinned_source_verified=True,
        license_reviewed=True,
        test_command_available=True,
        workspace_cleanup_ready=True,
        target_annotation_reviewed=True,
        external_parent=str(tmp_path),
        execution_context=verified,
        pdb_launch_plan=PdbLaunchPlan(
            str(verified.environment.python_executable),
            "driver.py", "src/target.py", (10,), ".", ("pytest", f2p), {"LANG": "C"}
        ),
    )


def test_authorized_preflight_requires_context_and_pdb_plan(tmp_path: Path) -> None:
    current = authorized_adapter(tmp_path)
    report = current.preflight(
        "bugsinpy-tqdm-003", facts(current, tmp_path), target_symbols=["tqdm.__bool__"], repository_root=str(ROOT)
    )
    assert report.authorized is True
    assert not report.blocked_gates


def test_verified_context_rewrites_pytest_and_never_uses_host_runner(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    workspace = TaskWorkspace(str(source), parent_dir=str(tmp_path))
    verified, runner = context(tmp_path, BugsInPyAdapter.from_manifest(MANIFEST))
    try:
        result = CommandRunner(workspace, verified).run(["pytest", "tests/test_x.py"], ".", 3)
        assert result.exit_code == 0
        argv, cwd, _, env = runner.calls[0]
        assert argv[:3] == [verified.environment.python_executable, "-m", "pytest"]
        assert cwd == workspace.root
        assert env["PYTHONPATH"] == str(Path(workspace.root) / "src")
    finally:
        workspace.cleanup()


def test_external_verifier_rejects_missing_context_before_execution(tmp_path: Path) -> None:
    current = authorized_adapter(tmp_path)
    entry = current.select("bugsinpy-tqdm-003")
    (tmp_path / "sources" / "tqdm").mkdir(parents=True)
    task = current.to_debug_task(
        entry, TaskSource("external", "sources/tqdm", current.source_provenance(entry)), target_symbols=["tqdm.__bool__"]
    )
    with pytest.raises(EvaluationInputError, match="verified execution context"):
        EvaluationVerifier(str(tmp_path)).evaluate(task, "")


class FakeAcquirer:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    def acquire(self, url, revision, destination):
        self.calls.append(("acquire", url, revision, destination))
        if self.fail:
            raise RuntimeError("synthetic acquisition failure")
        destination.mkdir(parents=True)
        if destination.name != "bugsinpy-framework":
            (destination / "README.md").write_text("external", encoding="utf-8")
        return destination

    def read_gold_patch(self, framework_root, metadata_path):
        self.calls.append(("gold", metadata_path))
        assert metadata_path.replace("\\", "/").endswith("bug_patch.txt")
        return ""


def test_authorized_smoke_orders_sources_and_verifier(tmp_path: Path) -> None:
    current = authorized_adapter(tmp_path)
    acquirer = FakeAcquirer()
    seen = {}

    class FakeVerifier:
        def __init__(self, root, **kwargs):
            seen["root"] = Path(root)
            seen["context"] = kwargs["execution_context"]

        def evaluate(self, task, patch):
            seen["task"] = task
            seen["patch"] = patch
            return SimpleNamespace(
                status=SimpleNamespace(value="COMPLETED"),
                semantic_mapping=lambda: {"status": "COMPLETED"},
            )

    preflight_facts = facts(current, tmp_path)
    evidence = NoModelSmokeRunner(current, acquirer, verifier_factory=FakeVerifier).run(
        "bugsinpy-tqdm-003",
        facts=preflight_facts,
        external_parent=str(tmp_path),
        repository_root=str(ROOT),
        target_symbols=["tqdm.__bool__"],
    )
    assert evidence.verdict == "REAL_SMOKE_PASSED"
    assert evidence.cleanup_succeeded is True
    assert evidence.execution_error is None
    assert [call[0] for call in acquirer.calls] == ["acquire", "acquire", "gold"]
    assert seen["context"] is preflight_facts.execution_context
    assert seen["task"].source.kind == "external"
    assert not seen["task"].fixture_path.startswith("agentic_debugger/datasets/curated/")
    assert seen["task"].source.provenance["fixed_revision"] == current.select("bugsinpy-tqdm-003")["bugsinpy"]["fixed_revision"]


def test_acquisition_and_verifier_failures_are_separate_from_cleanup(tmp_path: Path) -> None:
    current = authorized_adapter(tmp_path)
    preflight_facts = facts(current, tmp_path)
    acquired = NoModelSmokeRunner(current, FakeAcquirer(fail=True)).run(
        "bugsinpy-tqdm-003", facts=preflight_facts, external_parent=str(tmp_path), repository_root=str(ROOT), target_symbols=["tqdm.__bool__"]
    )
    assert acquired.failure_kind == "acquisition_failure"
    assert acquired.execution_error is not None
    assert acquired.cleanup_error is None
    assert acquired.cleanup_succeeded is True

    def failing_verifier(*args, **kwargs):
        raise RuntimeError("synthetic verifier failure")

    verified = NoModelSmokeRunner(current, FakeAcquirer(), verifier_factory=failing_verifier).run(
        "bugsinpy-tqdm-003", facts=preflight_facts, external_parent=str(tmp_path), repository_root=str(ROOT), target_symbols=["tqdm.__bool__"]
    )
    assert verified.failure_kind == "verifier_failure"
    assert verified.execution_error is not None
    assert verified.cleanup_error is None
    assert verified.cleanup_succeeded is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pilot_task_id", "bugsinpy-other-001"),
        ("manifest_fingerprint", "f" * 64),
        ("authority_revision", "e" * 40),
        ("project", "other-project"),
        ("bug_id", "99"),
        ("buggy_revision", "d" * 40),
        ("recipe_path", "projects/other/requirements.txt"),
        ("recipe_sha256", "c" * 64),
    ],
)
def test_dependency_preflight_rejects_unrelated_identity(
    tmp_path: Path, field: str, value: str
) -> None:
    current = authorized_adapter(tmp_path)
    current_facts = facts(current, tmp_path)
    dependencies = replace(current_facts.execution_context.environment.dependencies, **{field: value})
    environment = replace(current_facts.execution_context.environment, dependencies=dependencies)
    context_value = replace(current_facts.execution_context, environment=environment)
    report = current.preflight(
        "bugsinpy-tqdm-003",
        replace(current_facts, execution_context=context_value),
        target_symbols=["tqdm.__bool__"],
        repository_root=str(ROOT),
    )
    assert report.authorized is False
    assert GateName.DEPENDENCY_INSTALL_BOUNDARY.value in report.blocked_gates


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_cwd", "unrelated"),
        ("pythonpath", ("unrelated",)),
        ("environment", {"LANG": "C", "UNRELATED": "1"}),
    ],
)
def test_environment_preflight_rejects_unreviewed_task_context(
    tmp_path: Path, field: str, value: object
) -> None:
    current = authorized_adapter(tmp_path)
    current_facts = facts(current, tmp_path)
    environment = replace(current_facts.execution_context.environment, **{field: value})
    context_value = replace(current_facts.execution_context, environment=environment)
    report = current.preflight(
        "bugsinpy-tqdm-003",
        replace(current_facts, execution_context=context_value),
        target_symbols=["tqdm.__bool__"],
        repository_root=str(ROOT),
    )
    assert GateName.PYTHON_RUNTIME_AVAILABLE.value in report.blocked_gates


@pytest.mark.parametrize(
    "plan_change",
    [
        {"target": "unrelated.py"},
        {"driver": "unrelated-driver.py"},
        {"breakpoints": (99,)},
        {"argv": ("pytest", "unrelated/tests.py::test_unrelated")},
        {"environment": {"LANG": "C", "UNRELATED": "1"}},
    ],
)
def test_pdb_preflight_rejects_unrelated_launch_plan(tmp_path: Path, plan_change: dict[str, object]) -> None:
    current = authorized_adapter(tmp_path)
    current_facts = facts(current, tmp_path)
    plan = replace(current_facts.pdb_launch_plan, **plan_change)
    report = current.preflight(
        "bugsinpy-tqdm-003",
        replace(current_facts, pdb_launch_plan=plan),
        target_symbols=["tqdm.__bool__"],
        repository_root=str(ROOT),
    )
    assert GateName.PDB_PLANNING.value in report.blocked_gates


def test_containment_preflight_rejects_root_outside_external_parent(tmp_path: Path) -> None:
    current = authorized_adapter(tmp_path)
    current_facts = facts(current, tmp_path)
    other_root = tmp_path / "other-root"
    other_root.mkdir()
    containment = replace(current_facts.execution_context.containment, root=str(other_root))
    current_facts.execution_context.runner.boundary_guarantee = containment.to_mapping()
    context_value = replace(current_facts.execution_context, containment=containment)
    report = current.preflight(
        "bugsinpy-tqdm-003",
        replace(current_facts, execution_context=context_value),
        target_symbols=["tqdm.__bool__"],
        repository_root=str(ROOT),
    )
    assert GateName.CONTAINMENT_READY.value in report.blocked_gates


def test_containment_preflight_rejects_root_containing_tracked_repository(tmp_path: Path) -> None:
    current = authorized_adapter(tmp_path)
    current_facts = facts(current, tmp_path)
    containment = replace(current_facts.execution_context.containment, root=str(ROOT))
    current_facts.execution_context.runner.boundary_guarantee = containment.to_mapping()
    context_value = replace(current_facts.execution_context, containment=containment)
    report = current.preflight(
        "bugsinpy-tqdm-003",
        replace(current_facts, execution_context=context_value),
        target_symbols=["tqdm.__bool__"],
        repository_root=str(ROOT),
    )
    assert GateName.CONTAINMENT_READY.value in report.blocked_gates


def test_containment_preflight_rejects_filesystem_root(tmp_path: Path) -> None:
    current = authorized_adapter(tmp_path)
    current_facts = facts(current, tmp_path)
    containment = replace(current_facts.execution_context.containment, root=str(Path(tmp_path.anchor)))
    current_facts.execution_context.runner.boundary_guarantee = containment.to_mapping()
    context_value = replace(current_facts.execution_context, containment=containment)
    report = current.preflight(
        "bugsinpy-tqdm-003",
        replace(current_facts, execution_context=context_value),
        target_symbols=["tqdm.__bool__"],
        repository_root=str(ROOT),
    )
    assert GateName.CONTAINMENT_READY.value in report.blocked_gates


def test_cli_facts_json_is_metadata_only_and_fail_closed(tmp_path: Path, monkeypatch, capsys) -> None:
    from agentic_debugger.bugsinpy import smoke

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bugsinpy-smoke",
            "--manifest",
            str(MANIFEST),
            "--task",
            "bugsinpy-tqdm-003",
            "--external-parent",
            str(tmp_path),
            "--facts-json",
            str(tmp_path / "facts.json"),
        ],
    )
    assert smoke.main() == 2
    assert "cannot construct a verified execution context" in capsys.readouterr().out


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:tqdm/tqdm.git",
        "ssh://github.com/tqdm/tqdm",
        "file:///tmp/tqdm",
        "https://example.com/tqdm/tqdm",
        "https://github.com/tqdm/tqdm.git",
    ],
)
def test_git_acquisition_rejects_nonapproved_public_urls(tmp_path: Path, url: str) -> None:
    parent = tmp_path / "external"
    parent.mkdir()
    workspace = ExternalWorkspace.create(parent, repository_root=str(ROOT))
    try:
        with pytest.raises(ValueError, match="approved public HTTPS"):
            GitSourceAcquirer().acquire(url, "a" * 40, workspace.source_dir / "tqdm")
    finally:
        workspace.cleanup()


def test_git_acquisition_is_pinned_noninteractive_and_no_submodules(tmp_path: Path, monkeypatch) -> None:
    parent = tmp_path / "external"
    parent.mkdir()
    workspace = ExternalWorkspace.create(parent, repository_root=str(ROOT))
    calls = []

    def fake_git(argv, cwd):
        calls.append((argv, cwd))
        if "clone" in argv:
            Path(argv[-1]).mkdir(parents=True)
        stdout = "a" * 40 + "\n" if argv[:2] == ["rev-parse", "HEAD"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(adapter_module, "_run_git", fake_git)
    try:
        destination = workspace.source_dir / "tqdm"
        result = GitSourceAcquirer().acquire("https://github.com/tqdm/tqdm", "a" * 40, destination)
        assert result == destination
        assert calls[0][0][:4] == ["-c", "credential.helper=", "clone", "--no-checkout"]
        assert "--no-tags" in calls[0][0]
        assert "--recurse-submodules" not in calls[0][0]
    finally:
        workspace.cleanup()


def test_git_runner_disables_prompts_and_global_credentials(monkeypatch, tmp_path: Path) -> None:
    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(adapter_module.subprocess, "run", fake_run)
    adapter_module._run_git(["status"], tmp_path)
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert seen["env"]["GIT_CONFIG_GLOBAL"]
    assert not any("TOKEN" in key or "PASSWORD" in key or "SECRET" in key for key in seen["env"])
