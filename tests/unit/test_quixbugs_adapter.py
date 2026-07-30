from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from agentic_debugger.bugsinpy.adapter import ExternalWorkspace
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.task_schema import DebugTask, TaskSource
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.quixbugs.adapter import (
    DiscoveryError,
    QuixBugsAdapter,
    QuixBugsManifest,
    QuixBugsManifestValidationError,
    QuixBugsPreflightFacts,
    QuixBugsSmokeRunner,
    QuixBugsSourceAcquirer,
    QuixBugsTaskMappingError,
    QuixGateName,
    QuixGateStatus,
    build_gold_patch,
    verifier_command_count,
)
from agentic_debugger.runtime.command_runner import CommandResult
from agentic_debugger.runtime.execution import (
    ContainmentGuarantee,
    DependencyPreparation,
    PreparedEnvironment,
    VerifiedExecutionContext,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "research" / "quixbugs" / "GCD_SMOKE_MANIFEST_V1.json"

BUGGY_GCD = "def gcd(a, b):\n    if b == 0:\n        return a\n    else:\n        return gcd(a % b, b)\n"
CORRECT_GCD = "def gcd(a, b):\n    if b == 0:\n        return a\n    else:\n        return gcd(b, a % b)\n"


def adapter() -> QuixBugsAdapter:
    return QuixBugsAdapter.from_manifest(MANIFEST)


# ---- Manifest validation ----------------------------------------------------


def test_valid_manifest_loads_and_fingerprints_deterministically() -> None:
    first = QuixBugsManifest.load(MANIFEST)
    second = QuixBugsManifest.load(MANIFEST)

    assert first.manifest_id == "quixbugs-gcd-smoke-v1"
    assert first.task_id == "quixbugs-gcd-smoke-v1"
    assert first.authority_revision == "4257f44b0ff1181dedaedee6a447e133219fcebf"
    assert first.official_repository == "https://github.com/jkoppel/QuixBugs"
    assert first.buggy_path == "python_programs/gcd.py"
    assert first.fingerprint == second.fingerprint


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update({"manifest_schema_version": "9.0"}),
        lambda data: data.update({"dataset": "BugsInPy"}),
        lambda data: data["authority"].update({"official_repository_revision": "not-a-sha"}),
        lambda data: data["authority"].update({"official_repository": "https://github.com/evil/QuixBugs"}),
        lambda data: data["target"].update({"algorithm": "bitcount"}),
        lambda data: data["target"].update({"buggy_path": "../outside.py"}),
        lambda data: data["resource_profile"].update({"cpu_seconds": -1}),
        lambda data: data["resource_profile"].update({"network_denied": False}),
        lambda data: data["licensing"].update({"license": "GPL-3.0"}),
        lambda data: data.update({"unresolved_assumptions": []}) or data.__setitem__("unresolved_assumptions", "not-a-list"),
    ],
)
def test_malformed_or_unsupported_manifest_is_rejected(tmp_path: Path, mutation) -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    mutation(data)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(QuixBugsManifestValidationError):
        QuixBugsManifest.load(path)


def test_unknown_task_id_is_rejected() -> None:
    with pytest.raises(QuixBugsTaskMappingError, match="unknown QuixBugs task ID"):
        adapter().manifest.select("quixbugs-does-not-exist")


# ---- Generalization beyond the single gcd task ------------------------------


SECOND_MANIFEST = ROOT / "research" / "quixbugs" / "BUCKETSORT_SMOKE_MANIFEST_V1.json"


def test_second_real_manifest_loads_with_its_own_algorithm_and_oracle() -> None:
    manifest = QuixBugsManifest.load(SECOND_MANIFEST)

    assert manifest.algorithm == "bucketsort"
    assert manifest.manifest_id == "quixbugs-bucketsort-smoke-v1"
    assert manifest.task_id == "quixbugs-bucketsort-smoke-v1"
    assert manifest.buggy_path == "python_programs/bucketsort.py"
    assert manifest.corrected_path == "correct_python_programs/bucketsort.py"
    assert manifest.pytest_path == "python_testcases/test_bucketsort.py"
    assert manifest.oracle["target_symbols"] == ["bucketsort"]
    assert manifest.oracle["bug_category"]
    assert "python_testcases/load_testdata.py" in manifest.support_paths
    # every eight-task-baseline manifest is pinned to the same reused venv/environment
    assert manifest.fingerprint != QuixBugsManifest.load(MANIFEST).fingerprint


def test_second_manifest_bug_id_and_title_are_derived_not_hardcoded() -> None:
    second = QuixBugsAdapter.from_manifest(SECOND_MANIFEST)
    assert second.source_provenance()["bug_id"] == "bucketsort"

    commands = second.build_commands(fail_to_pass=["t::a"], pass_to_pass=["t::b"])
    source = TaskSource("external", "sources/quixbugs", second.source_provenance())
    task = second.to_debug_task(source, commands)
    assert "bucketsort" in task.title
    assert "gcd" not in task.title
    assert task.oracle.target_symbols == ["bucketsort"]
    assert task.constraints.allowed_write_paths == ["python_programs/bucketsort.py"]


@pytest.mark.parametrize(
    "mutation",
    [
        # target.algorithm must match the buggy/corrected/pytest path naming convention
        lambda data: data["target"].update({"buggy_path": "python_programs/gcd_other.py"}),
        lambda data: data["target"].update({"algorithm": "Gcd"}),
        lambda data: data["target"].update({"algorithm": "gcd", "language": "javascript"}),
        # manifest_id/task_id must be the derived quixbugs-<algorithm>-smoke-v1 identity
        lambda data: data.update({"manifest_id": "quixbugs-not-derived-smoke-v1"}),
        lambda data: data.update({"task_id": "quixbugs-not-derived-smoke-v1"}),
        # oracle section is now required and structurally validated
        lambda data: data.pop("oracle"),
        lambda data: data["oracle"].update({"bug_category": ""}),
        lambda data: data["oracle"].update({"target_symbols": []}),
        lambda data: data["oracle"].update({"root_cause_summary": ""}),
        lambda data: data["oracle"].update({"runtime_evidence_hint": ""}),
    ],
)
def test_generalized_manifest_validation_rejects_mismatches(tmp_path: Path, mutation) -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    mutation(data)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(QuixBugsManifestValidationError):
        QuixBugsManifest.load(path)


def test_manifest_rejects_algorithm_too_long_for_the_shared_task_id_pattern(tmp_path: Path) -> None:
    # max-length target.algorithm (64 chars) makes the derived
    # "quixbugs-<algorithm>-smoke-v1" identity exceed DebugTask's shared
    # TASK_ID_PATTERN (max 64 chars total) -- must fail closed at manifest
    # load time, not only later when to_debug_task() builds a DebugTask.
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    long_algorithm = "a" * 64
    data["target"].update({
        "algorithm": long_algorithm,
        "buggy_path": f"python_programs/{long_algorithm}.py",
        "corrected_path": f"correct_python_programs/{long_algorithm}.py",
        "pytest_path": f"python_testcases/test_{long_algorithm}.py",
    })
    data["manifest_id"] = data["task_id"] = f"quixbugs-{long_algorithm}-smoke-v1"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(QuixBugsManifestValidationError, match="not a valid task_id"):
        QuixBugsManifest.load(path)


# ---- TaskSource / DebugTask mapping -----------------------------------------


REAL_F2P = (
    "python_testcases/test_gcd.py::test_gcd[input_data1-13]",
    "python_testcases/test_gcd.py::test_gcd[input_data2-1]",
    "python_testcases/test_gcd.py::test_gcd[input_data3-20]",
    "python_testcases/test_gcd.py::test_gcd[input_data4-18913]",
    "python_testcases/test_gcd.py::test_gcd[input_data5-3]",
)
REAL_P2P = ("python_testcases/test_gcd.py::test_gcd[input_data0-17]",)


def test_external_task_source_mapping_and_gcd_path_normalization() -> None:
    current = adapter()
    source = TaskSource("external", "sources/quixbugs", current.source_provenance())
    commands = current.build_commands(fail_to_pass=REAL_F2P, pass_to_pass=REAL_P2P)
    task = current.to_debug_task(source, commands)

    assert isinstance(task, DebugTask)
    assert load_task(task).to_mapping() == task.to_mapping()
    assert task.source is not None and task.source.kind == "external"
    assert task.fixture_path == "sources/quixbugs"
    assert not task.fixture_path.startswith("agentic_debugger/datasets/curated/")
    assert task.constraints.network_allowed is False
    assert task.constraints.allowed_write_paths == ["python_programs/gcd.py"]
    assert "python_testcases/test_gcd.py" in task.constraints.denied_write_paths
    assert "correct_python_programs/gcd.py" in task.constraints.denied_write_paths
    assert task.tests.fail_to_pass == list(REAL_F2P)
    assert task.tests.pass_to_pass == list(REAL_P2P)
    assert "python_testcases/test_gcd.py" in task.tests.full_suite_argv
    assert "input_data" not in " ".join(task.tests.full_suite_argv)


def test_no_discovered_f2p_node_is_silently_dropped() -> None:
    current = adapter()
    commands = current.build_commands(fail_to_pass=REAL_F2P, pass_to_pass=REAL_P2P)
    assert set(commands.fail_to_pass) == set(REAL_F2P)
    assert len(commands.fail_to_pass) == 5
    source = TaskSource("external", "sources/quixbugs", current.source_provenance())
    task = current.to_debug_task(source, commands)
    assert set(task.tests.fail_to_pass) == set(REAL_F2P)
    assert len(task.tests.fail_to_pass) == 5


def test_max_test_runs_reflects_the_real_verifier_lifecycle() -> None:
    current = adapter()
    commands = current.build_commands(fail_to_pass=REAL_F2P, pass_to_pass=REAL_P2P)
    # 1 collect + 1 baseline repro + 6 baseline nodes + 1 post repro + 6 post nodes + 1 full suite = 16.
    # The shared DebugTask schema's max_test_runs range was widened to [1, 20]
    # specifically so this real value is representable exactly, not clamped.
    assert verifier_command_count(5, 1) == 16
    assert commands.max_test_runs == 16

    source = TaskSource("external", "sources/quixbugs", current.source_provenance())
    task = current.to_debug_task(source, commands)
    assert task.constraints.max_test_runs == 16


def test_internal_source_binding_is_rejected() -> None:
    current = adapter()
    commands = current.build_commands(fail_to_pass=["t::a"], pass_to_pass=["t::b"])
    with pytest.raises(QuixBugsTaskMappingError, match="external source binding"):
        current.to_debug_task(TaskSource("curated", "agentic_debugger/datasets/curated/x", current.source_provenance()), commands)


def test_build_commands_requires_nonoverlapping_f2p_p2p() -> None:
    current = adapter()
    with pytest.raises(QuixBugsTaskMappingError, match="at least one non-empty pass_to_pass"):
        current.build_commands(fail_to_pass=["t::a"], pass_to_pass=[])
    with pytest.raises(QuixBugsTaskMappingError, match="overlap"):
        current.build_commands(fail_to_pass=["t::a"], pass_to_pass=["t::a"])
    with pytest.raises(QuixBugsTaskMappingError, match="at least one non-empty fail_to_pass"):
        current.build_commands(fail_to_pass=[], pass_to_pass=["t::b"])
    with pytest.raises(QuixBugsTaskMappingError, match="duplicate"):
        current.build_commands(fail_to_pass=["t::a", "t::a"], pass_to_pass=["t::b"])


def test_preflight_reports_each_gate_and_blocks_by_default() -> None:
    report = adapter().preflight()
    names = {gate.name for gate in report.gates}

    assert names == {
        QuixGateName.MANIFEST_VALID,
        QuixGateName.SUPPORTED_PLATFORM,
        QuixGateName.PINNED_UPSTREAM_SOURCE,
        QuixGateName.PROJECT_LICENSE_REVIEW,
        QuixGateName.PYTHON_RUNTIME_AVAILABLE,
        QuixGateName.DEPENDENCY_INSTALL_BOUNDARY,
        QuixGateName.TEST_COMMAND_AVAILABILITY,
        QuixGateName.CONTAINMENT_READY,
        QuixGateName.WORKSPACE_CLEANUP_READY,
        QuixGateName.TARGET_ANNOTATION_REVIEW,
    }
    assert report.authorized is False
    assert all(gate.status is QuixGateStatus.PASS for gate in report.gates if gate.name is QuixGateName.MANIFEST_VALID)
    assert QuixGateName.PROJECT_LICENSE_REVIEW.value in report.blocked_gates
    assert QuixGateName.CONTAINMENT_READY.value in report.blocked_gates


# ---- Gold patch --------------------------------------------------------------


def test_gold_patch_matches_patch_manager_format_and_scope() -> None:
    patch = build_gold_patch(BUGGY_GCD, CORRECT_GCD, "python_programs/gcd.py")
    assert patch.startswith("--- a/python_programs/gcd.py\n")
    assert "+++ b/python_programs/gcd.py\n" in patch
    assert "diff --git" not in patch
    assert patch.count("--- ") == 1 and patch.count("+++ ") == 1


def test_gold_patch_rejects_identical_source() -> None:
    with pytest.raises(QuixBugsTaskMappingError, match="empty"):
        build_gold_patch(BUGGY_GCD, BUGGY_GCD, "python_programs/gcd.py")


# ---- Cleanup ownership (reuses the existing ExternalWorkspace contract) -----


def test_external_workspace_ownership_is_reused_unmodified(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    parent = tmp_path / "external"
    parent.mkdir()

    workspace = ExternalWorkspace.create(parent, repository_root=str(repository))
    root = workspace.root
    assert root.parent == parent
    workspace.cleanup()
    assert not root.exists()


# ---- Source acquisition: URL/revision/ownership gates, no network reached --


def test_source_acquirer_rejects_non_sha1_revision(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SHA-1"):
        QuixBugsSourceAcquirer().acquire("https://github.com/jkoppel/QuixBugs", "not-a-sha", tmp_path / "dest")


def test_source_acquirer_rejects_unapproved_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="approved"):
        QuixBugsSourceAcquirer().acquire(
            "https://github.com/evil/QuixBugs", "4257f44b0ff1181dedaedee6a447e133219fcebf", tmp_path / "dest"
        )


def test_source_acquirer_requires_an_already_existing_owned_parent(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="already-existing owned parent"):
        QuixBugsSourceAcquirer().acquire(
            "https://github.com/jkoppel/QuixBugs", "4257f44b0ff1181dedaedee6a447e133219fcebf", tmp_path / "nonexistent" / "dest"
        )


def test_source_acquirer_verify_pinned_rejects_bad_revision(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SHA-1"):
        QuixBugsSourceAcquirer().verify_pinned(tmp_path, "not-a-sha")


# ---- Immutable source cleanliness: real local git repo, no network --------


def _run(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=str(cwd), check=True, capture_output=True, text=True)


def _local_pinned_repo(tmp_path: Path) -> tuple[Path, str]:
    """A real, local, network-free git repo standing in for a pinned checkout."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "--quiet"], repo)
    _run(["git", "config", "core.autocrlf", "false"], repo)
    (repo / "python_programs").mkdir()
    (repo / "python_programs" / "gcd.py").write_text(BUGGY_GCD, encoding="utf-8")
    _run(["git", "add", "."], repo)
    _run(["git", "-c", "user.email=test@test", "-c", "user.name=test", "commit", "--quiet", "-m", "initial"], repo)
    _run(["git", "remote", "add", "origin", "https://github.com/jkoppel/QuixBugs"], repo)
    _run(["git", "checkout", "--quiet", "--detach", "HEAD"], repo)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()
    return repo, sha


def test_verify_pinned_accepts_a_genuinely_clean_checkout(tmp_path: Path) -> None:
    repo, sha = _local_pinned_repo(tmp_path)
    QuixBugsSourceAcquirer().verify_pinned(repo, sha)


def test_verify_pinned_rejects_a_modified_tracked_file(tmp_path: Path) -> None:
    repo, sha = _local_pinned_repo(tmp_path)
    (repo / "python_programs" / "gcd.py").write_text(CORRECT_GCD, encoding="utf-8")
    with pytest.raises(RuntimeError, match="not clean"):
        QuixBugsSourceAcquirer().verify_pinned(repo, sha)


def test_verify_pinned_rejects_a_staged_file(tmp_path: Path) -> None:
    repo, sha = _local_pinned_repo(tmp_path)
    (repo / "python_programs" / "gcd.py").write_text(CORRECT_GCD, encoding="utf-8")
    _run(["git", "add", "."], repo)
    with pytest.raises(RuntimeError, match="not clean"):
        QuixBugsSourceAcquirer().verify_pinned(repo, sha)


def test_verify_pinned_rejects_an_untracked_file(tmp_path: Path) -> None:
    repo, sha = _local_pinned_repo(tmp_path)
    (repo / "unexpected.txt").write_text("contamination", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not clean"):
        QuixBugsSourceAcquirer().verify_pinned(repo, sha)


def test_verify_pinned_rejects_wrong_origin(tmp_path: Path) -> None:
    repo, sha = _local_pinned_repo(tmp_path)
    _run(["git", "remote", "set-url", "origin", "https://github.com/evil/QuixBugs"], repo)
    with pytest.raises(RuntimeError, match="approved URL"):
        QuixBugsSourceAcquirer().verify_pinned(repo, sha)


def test_verify_pinned_never_resets_or_deletes_a_dirty_checkout(tmp_path: Path) -> None:
    repo, sha = _local_pinned_repo(tmp_path)
    (repo / "python_programs" / "gcd.py").write_text(CORRECT_GCD, encoding="utf-8")
    with pytest.raises(RuntimeError):
        QuixBugsSourceAcquirer().verify_pinned(repo, sha)
    # the modification must still be present -- no silent reset/clean/delete
    assert (repo / "python_programs" / "gcd.py").read_text(encoding="utf-8") == CORRECT_GCD
    assert repo.is_dir()


# ---- Discovery / node classification (fakes only, no WSL/network) ----------


class FakeContainmentRunner:
    runner_id = "fake-quixbugs-contained"

    def __init__(self, *, collect_stdout: str, node_exit_codes: dict[str, int], oracle_exit_code: int, resource_isolation_ready: bool = False) -> None:
        self.calls: list[tuple[list[str], str, float, dict]] = []
        self.boundary_guarantee: dict = {}
        self.resource_isolation_ready = resource_isolation_ready
        self._collect_stdout = collect_stdout
        self._node_exit_codes = node_exit_codes
        self._oracle_exit_code = oracle_exit_code

    def run(self, argv, cwd, timeout_seconds, env):
        self.calls.append((argv, cwd, timeout_seconds, env))
        if "--collect-only" in argv:
            return CommandResult(list(argv), cwd, 0, False, 1, self._collect_stdout, "", False, False)
        if "--correct" in argv:
            return CommandResult(list(argv), cwd, self._oracle_exit_code, False, 1, "1 passed\n", "", False, False)
        for node, code in self._node_exit_codes.items():
            if node in argv:
                return CommandResult(list(argv), cwd, code, False, 1, f"{node} {'PASSED' if code == 0 else 'FAILED'}\n", "", False, False)
        raise AssertionError(f"unexpected argv: {argv}")


COLLECT_STDOUT = "\n".join(
    f"python_testcases/test_gcd.py::test_gcd[input_data{i}-expected{i}]" for i in range(6)
)


def _dependencies() -> DependencyPreparation:
    current = adapter()
    recipe_path = f"pytest=={current.manifest.environment['pinned_packages']['pytest']}"
    recipe_sha256 = hashlib.sha256(recipe_path.encode("utf-8")).hexdigest()
    return DependencyPreparation(
        current.manifest.task_id, current.manifest.fingerprint, current.manifest.authority_revision,
        "quixbugs", "gcd", current.manifest.authority_revision,
        recipe_path, recipe_sha256, current.manifest.environment["expected_fingerprint"],
    )


def _fake_context(tmp_path: Path, runner: FakeContainmentRunner) -> VerifiedExecutionContext:
    python_executable = tmp_path / "venv" / "bin" / "python"
    python_executable.parent.mkdir(parents=True, exist_ok=True)
    python_executable.write_text("fake interpreter", encoding="utf-8")
    deps = _dependencies()
    environment = PreparedEnvironment(str(python_executable), "3.10.12", ".", (), {}, deps)
    containment = ContainmentGuarantee(str(tmp_path.resolve()), runner.runner_id, resource_limits={"cpu_seconds": "prlimit-enforced:5"})
    runner.boundary_guarantee = containment.to_mapping()
    return VerifiedExecutionContext(environment, containment, runner)


def test_discovery_classifies_failing_and_passing_nodes(tmp_path: Path) -> None:
    node_exit_codes = {f"python_testcases/test_gcd.py::test_gcd[input_data{i}-expected{i}]": (0 if i == 0 else 1) for i in range(6)}
    runner = FakeContainmentRunner(collect_stdout=COLLECT_STDOUT, node_exit_codes=node_exit_codes, oracle_exit_code=0)
    context = _fake_context(tmp_path, runner)

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    from agentic_debugger.runtime.workspace import TaskWorkspace

    workspace = TaskWorkspace(str(source_dir), parent_dir=str(tmp_path))
    try:
        smoke = QuixBugsSmokeRunner(adapter(), acquirer=None)  # type: ignore[arg-type]
        record = smoke.discover(context, workspace)
    finally:
        workspace.cleanup()

    assert len(record.collected_nodes) == 6
    assert len(record.f2p_candidates) == 5
    assert record.p2p_candidates == ("python_testcases/test_gcd.py::test_gcd[input_data0-expected0]",)
    assert record.oracle_correct_exit_code == 0


def test_discovery_requires_at_least_one_failing_node(tmp_path: Path) -> None:
    node_exit_codes = {f"python_testcases/test_gcd.py::test_gcd[input_data{i}-expected{i}]": 0 for i in range(6)}
    runner = FakeContainmentRunner(collect_stdout=COLLECT_STDOUT, node_exit_codes=node_exit_codes, oracle_exit_code=0)
    context = _fake_context(tmp_path, runner)

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    from agentic_debugger.runtime.workspace import TaskWorkspace

    workspace = TaskWorkspace(str(source_dir), parent_dir=str(tmp_path))
    try:
        smoke = QuixBugsSmokeRunner(adapter(), acquirer=None)  # type: ignore[arg-type]
        with pytest.raises(DiscoveryError, match="reproducible failing"):
            smoke.discover(context, workspace)
    finally:
        workspace.cleanup()


# ---- Blocked preflight never acquires or executes ---------------------------


def test_blocked_preflight_never_acquires_or_executes_benchmark(tmp_path: Path) -> None:
    class NoExecutionAcquirer:
        def acquire(self, url, revision, destination):
            raise AssertionError("benchmark acquisition must not happen")

    smoke = QuixBugsSmokeRunner(adapter(), NoExecutionAcquirer())  # type: ignore[arg-type]
    evidence = smoke.run(
        facts=QuixBugsPreflightFacts(), sources_parent=str(tmp_path / "sources"), external_parent=str(tmp_path), repository_root=str(ROOT)
    )

    assert evidence.verdict == "REAL_SMOKE_BLOCKED"
    assert evidence.cleanup_attempted is False
    assert evidence.cleanup_succeeded is True
    assert evidence.discovery is None


# ---- Verifier invocation with fakes (full offline pipeline) ----------------


def _fake_acquirer() -> object:
    class FakeAcquirer:
        def acquire(self, url: str, revision: str, destination: Path) -> Path:
            assert url == "https://github.com/jkoppel/QuixBugs"
            (destination / "python_programs").mkdir(parents=True)
            (destination / "python_programs" / "gcd.py").write_text(BUGGY_GCD, encoding="utf-8")
            (destination / "correct_python_programs").mkdir(parents=True)
            (destination / "correct_python_programs" / "gcd.py").write_text(CORRECT_GCD, encoding="utf-8")
            return destination

    return FakeAcquirer()


def _fake_verifier_factory(*, status: str, outcome):
    from agentic_debugger.evaluation.runner import EvaluationStatus

    class FakeVerifier:
        def __init__(self, *args, **kwargs):
            pass

        def evaluate(self, task, patch):
            class _Result:
                pass

            result = _Result()
            result.status = getattr(EvaluationStatus, status)
            result.outcome = outcome
            result.semantic_mapping = lambda: {"status": status}
            assert task.tests.fail_to_pass[0].endswith("]")
            assert "gcd(b, a % b)" in patch
            return result

    return FakeVerifier


def _fake_acquirer_and_verifier():
    from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome

    return _fake_acquirer(), _fake_verifier_factory(status="COMPLETED", outcome=SemanticOutcome.RESOLVED)


# ---- Full dependency/task binding: every field must match exactly ----------


def _facts_with_dependencies(tmp_path: Path, dependencies: DependencyPreparation) -> QuixBugsPreflightFacts:
    runner = FakeContainmentRunner(collect_stdout=COLLECT_STDOUT, node_exit_codes={}, oracle_exit_code=0, resource_isolation_ready=True)
    python_executable = tmp_path / "venv" / "bin" / "python"
    python_executable.parent.mkdir(parents=True, exist_ok=True)
    python_executable.write_text("fake interpreter", encoding="utf-8")
    environment = PreparedEnvironment(str(python_executable), "3.10.12", ".", (), {}, dependencies)
    containment = ContainmentGuarantee(str(tmp_path.resolve()), runner.runner_id, resource_limits={"cpu_seconds": "prlimit-enforced:5"})
    runner.boundary_guarantee = containment.to_mapping()
    context = VerifiedExecutionContext(environment, containment, runner)
    external_parent = tmp_path / "external"
    external_parent.mkdir(exist_ok=True)
    return QuixBugsPreflightFacts(
        platform="linux", pinned_source_verified=True, license_reviewed=True, test_command_available=True,
        workspace_cleanup_ready=True, target_annotation_reviewed=True, external_parent=str(external_parent), execution_context=context,
    )


def test_dependency_gate_passes_with_exact_binding(tmp_path: Path) -> None:
    report = adapter().preflight(_facts_with_dependencies(tmp_path, _dependencies()), repository_root=str(ROOT))
    assert QuixGateName.DEPENDENCY_INSTALL_BOUNDARY.value not in report.blocked_gates


@pytest.mark.parametrize(
    "field,value",
    [
        ("pilot_task_id", "some-other-task"),
        ("manifest_fingerprint", "f" * 64),
        ("authority_revision", "0" * 40),
        ("project", "not-quixbugs"),
        ("bug_id", "bitcount"),
        ("buggy_revision", "0" * 40),
        ("recipe_path", "pytest==0.0.0"),
        ("recipe_sha256", "a" * 64),
        ("installed_fingerprint", "1" * 64),
    ],
)
def test_dependency_gate_rejects_every_field_mismatch(tmp_path: Path, field: str, value: str) -> None:
    from dataclasses import replace

    mismatched = replace(_dependencies(), **{field: value})
    report = adapter().preflight(_facts_with_dependencies(tmp_path, mismatched), repository_root=str(ROOT))
    assert QuixGateName.DEPENDENCY_INSTALL_BOUNDARY.value in report.blocked_gates


def test_dependency_gate_rejects_a_context_belonging_to_another_task(tmp_path: Path) -> None:
    foreign = DependencyPreparation(
        "bugsinpy-tqdm-003", "e" * 64, "1" * 40, "tqdm", "3", "1" * 40, "requirements.txt", "c" * 64, "d" * 64,
    )
    report = adapter().preflight(_facts_with_dependencies(tmp_path, foreign), repository_root=str(ROOT))
    assert QuixGateName.DEPENDENCY_INSTALL_BOUNDARY.value in report.blocked_gates
    assert not report.authorized


def test_containment_gate_blocks_without_resource_isolation_ready(tmp_path: Path) -> None:
    node_exit_codes = {f"python_testcases/test_gcd.py::test_gcd[input_data{i}-expected{i}]": (0 if i == 0 else 1) for i in range(6)}
    runner = FakeContainmentRunner(collect_stdout=COLLECT_STDOUT, node_exit_codes=node_exit_codes, oracle_exit_code=0, resource_isolation_ready=False)
    context = _fake_context(tmp_path, runner)
    external_parent = tmp_path / "external"
    external_parent.mkdir()
    acquirer, verifier_factory = _fake_acquirer_and_verifier()

    smoke = QuixBugsSmokeRunner(adapter(), acquirer, verifier_factory=verifier_factory)
    facts = QuixBugsPreflightFacts(
        platform="linux", pinned_source_verified=True, license_reviewed=True, test_command_available=True,
        workspace_cleanup_ready=True, target_annotation_reviewed=True, external_parent=str(external_parent), execution_context=context,
    )
    evidence = smoke.run(
        facts=facts, sources_parent=str(tmp_path / "sources"), external_parent=str(external_parent), repository_root=str(ROOT)
    )
    assert evidence.verdict == "REAL_SMOKE_BLOCKED"
    assert QuixGateName.CONTAINMENT_READY.value in evidence.preflight.blocked_gates


def test_full_pipeline_runs_with_fakes_and_no_network(tmp_path: Path) -> None:
    node_exit_codes = {f"python_testcases/test_gcd.py::test_gcd[input_data{i}-expected{i}]": (0 if i == 0 else 1) for i in range(6)}
    runner = FakeContainmentRunner(collect_stdout=COLLECT_STDOUT, node_exit_codes=node_exit_codes, oracle_exit_code=0, resource_isolation_ready=True)
    context = _fake_context(tmp_path, runner)

    external_parent = tmp_path / "external"
    external_parent.mkdir()
    acquirer, verifier_factory = _fake_acquirer_and_verifier()

    smoke = QuixBugsSmokeRunner(adapter(), acquirer, verifier_factory=verifier_factory)
    facts = QuixBugsPreflightFacts(
        platform="linux",
        pinned_source_verified=True,
        license_reviewed=True,
        test_command_available=True,
        workspace_cleanup_ready=True,
        target_annotation_reviewed=True,
        external_parent=str(external_parent),
        execution_context=context,
    )
    evidence = smoke.run(
        facts=facts, sources_parent=str(tmp_path / "sources"), external_parent=str(external_parent), repository_root=str(ROOT)
    )

    assert evidence.verdict == "REAL_SMOKE_PASSED", evidence.execution_error
    assert evidence.discovery is not None
    assert len(evidence.discovery.f2p_candidates) == 5
    assert evidence.discovery.p2p_candidates == ("python_testcases/test_gcd.py::test_gcd[input_data0-expected0]",)
    assert evidence.gold_patch_hashes is not None
    assert evidence.cleanup_succeeded is True
    assert list(external_parent.iterdir()) == []
    # the pinned source is immutable/persistent: cleanup only removes the
    # disposable run workspace under external_parent, never sources_parent.
    assert (tmp_path / "sources" / "quixbugs" / "python_programs" / "gcd.py").is_file()


def _run_full_pipeline_with_verifier_factory(tmp_path: Path, verifier_factory) -> "object":
    node_exit_codes = {f"python_testcases/test_gcd.py::test_gcd[input_data{i}-expected{i}]": (0 if i == 0 else 1) for i in range(6)}
    runner = FakeContainmentRunner(collect_stdout=COLLECT_STDOUT, node_exit_codes=node_exit_codes, oracle_exit_code=0, resource_isolation_ready=True)
    context = _fake_context(tmp_path, runner)
    external_parent = tmp_path / "external"
    external_parent.mkdir()
    smoke = QuixBugsSmokeRunner(adapter(), _fake_acquirer(), verifier_factory=verifier_factory)
    facts = QuixBugsPreflightFacts(
        platform="linux", pinned_source_verified=True, license_reviewed=True, test_command_available=True,
        workspace_cleanup_ready=True, target_annotation_reviewed=True, external_parent=str(external_parent), execution_context=context,
    )
    return smoke.run(
        facts=facts, sources_parent=str(tmp_path / "sources"), external_parent=str(external_parent), repository_root=str(ROOT)
    )


def test_completed_but_unresolved_outcome_is_not_real_smoke_passed(tmp_path: Path) -> None:
    from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome

    verifier_factory = _fake_verifier_factory(status="COMPLETED", outcome=SemanticOutcome.NO_OP)
    evidence = _run_full_pipeline_with_verifier_factory(tmp_path, verifier_factory)

    assert evidence.evaluation.status.value == "COMPLETED"
    assert evidence.evaluation.outcome.value == "NO_OP"
    assert evidence.verdict == "REAL_SMOKE_FAILED"
    # cleanup still runs and succeeds regardless of the unresolved outcome
    assert evidence.cleanup_succeeded is True


def test_completed_and_resolved_but_failed_cleanup_is_not_real_smoke_passed(tmp_path: Path, monkeypatch) -> None:
    import agentic_debugger.quixbugs.adapter as quixbugs_adapter_module
    from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome

    class _CleanupFailsWorkspace:
        def __init__(self, root: Path) -> None:
            self.root = root
            self.verifier_workspace_parent = root / "verifier-workspaces"

        def assert_contained(self, path: Path) -> None:
            pass

        def cleanup(self) -> None:
            pass  # deliberately does not remove root -- simulates cleanup failure

        @classmethod
        def create(cls, parent_dir, *, repository_root=None, containment_root=None):
            root = Path(parent_dir) / "owned-case"
            root.mkdir()
            return cls(root)

    monkeypatch.setattr(quixbugs_adapter_module, "ExternalWorkspace", _CleanupFailsWorkspace)
    verifier_factory = _fake_verifier_factory(status="COMPLETED", outcome=SemanticOutcome.RESOLVED)

    evidence = _run_full_pipeline_with_verifier_factory(tmp_path, verifier_factory)

    assert evidence.evaluation.status.value == "COMPLETED"
    assert evidence.evaluation.outcome.value == "RESOLVED"
    assert evidence.cleanup_succeeded is False
    assert evidence.verdict == "REAL_SMOKE_FAILED"


def test_ensure_source_is_idempotent_and_never_reclones(tmp_path: Path) -> None:
    calls = {"acquire": 0, "verify_pinned": 0}

    class CountingAcquirer:
        def acquire(self, url: str, revision: str, destination: Path) -> Path:
            calls["acquire"] += 1
            (destination / "python_programs").mkdir(parents=True)
            (destination / "python_programs" / "gcd.py").write_text(BUGGY_GCD, encoding="utf-8")
            return destination

        def verify_pinned(self, destination: Path, revision: str) -> None:
            calls["verify_pinned"] += 1

    smoke = QuixBugsSmokeRunner(adapter(), CountingAcquirer())
    sources_parent = tmp_path / "sources"

    first = smoke.ensure_source(sources_parent)
    second = smoke.ensure_source(sources_parent)

    assert first == second == sources_parent / "quixbugs"
    assert calls == {"acquire": 1, "verify_pinned": 1}
