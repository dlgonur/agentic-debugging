from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agentic_debugger.bugsinpy import (
    BugsInPyAdapter,
    BugsInPyManifest,
    ExternalWorkspace,
    GateName,
    GateStatus,
    NoModelSmokeRunner,
    PreflightFacts,
    TaskMappingError,
)
from agentic_debugger.evaluation.task_schema import DebugTask, TaskSource
from agentic_debugger.evaluation.runner import load_task


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "research" / "bugsinpy" / "PILOT_ELIGIBILITY_MANIFEST_V1.json"


def adapter() -> BugsInPyAdapter:
    return BugsInPyAdapter.from_manifest(MANIFEST)


def test_valid_manifest_loads_and_fingerprints_deterministically() -> None:
    first = BugsInPyManifest.load(MANIFEST)
    second = BugsInPyManifest.load(MANIFEST)

    assert first.manifest_id == "bugsinpy-pilot-eligibility-v1"
    assert first.authority_revision == "11c5f1eea954a42132cfd06bf257766a7963e0fd"
    assert len(first.tasks) == 8
    assert first.fingerprint == second.fingerprint


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update({"manifest_schema_version": "9.0"}),
        lambda data: data["tasks"][0]["bugsinpy"].update({"buggy_revision": "abc"}),
        lambda data: data["tasks"].pop(),
    ],
)
def test_malformed_or_unsupported_manifest_is_rejected(tmp_path: Path, mutation) -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    mutation(data)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError):
        BugsInPyManifest.load(path)


def test_unknown_pilot_task_id_is_rejected() -> None:
    with pytest.raises(TaskMappingError, match="unknown BugsInPy pilot task ID"):
        adapter().select("bugsinpy-does-not-exist")


def test_mapping_preserves_current_debugtask_contract() -> None:
    current = adapter()
    entry = current.select("bugsinpy-tqdm-003")
    mapping = current.to_debug_task(
        entry,
        TaskSource("external", "sources/tqdm", current.source_provenance(entry)),
        target_symbols=["tqdm.__bool__"],
    )

    assert isinstance(mapping, DebugTask)
    assert load_task(mapping).to_mapping() == mapping.to_mapping()
    assert mapping.source is not None
    assert mapping.source.kind == "external"
    assert mapping.fixture_path == "sources/tqdm"
    assert not mapping.fixture_path.startswith("agentic_debugger/datasets/curated/")
    assert mapping.constraints.network_allowed is False
    assert mapping.constraints.external_services_allowed is False
    assert mapping.tests.fail_to_pass == ["tqdm/tests/tests_tqdm.py::test_bool"]
    assert mapping.tests.pass_to_pass == [
        "tqdm/tests/tests_tqdm.py::test_format_meter",
        "tqdm/tests/tests_contrib.py::test_enumerate",
    ]


def test_unreviewed_target_annotation_cannot_map() -> None:
    current = adapter()
    with pytest.raises(TaskMappingError, match="target_symbols"):
       current.to_debug_task(
           current.select("bugsinpy-tqdm-003"),
            TaskSource("external", "sources/tqdm", current.source_provenance(current.select("bugsinpy-tqdm-003"))),
       )


def test_preflight_reports_each_execution_gate_and_blocks_by_default() -> None:
    report = adapter().preflight("bugsinpy-tqdm-003")
    names = {gate.name for gate in report.gates}

    assert names == {
        GateName.MANIFEST_VALID,
        GateName.SUPPORTED_PLATFORM,
        GateName.PINNED_UPSTREAM_SOURCE,
        GateName.PROJECT_LICENSE_REVIEW,
        GateName.PYTHON_RUNTIME_AVAILABLE,
        GateName.DEPENDENCY_INSTALL_BOUNDARY,
        GateName.TEST_COMMAND_AVAILABILITY,
        GateName.CONTAINMENT_READY,
        GateName.WORKSPACE_CLEANUP_READY,
       GateName.TARGET_ANNOTATION_REVIEW,
        GateName.PDB_PLANNING,
    }
    assert report.authorized is False
    assert all(gate.status is GateStatus.PASS for gate in report.gates if gate.name is GateName.MANIFEST_VALID)
    assert GateName.PROJECT_LICENSE_REVIEW.value in report.blocked_gates
    assert GateName.CONTAINMENT_READY.value in report.blocked_gates


def test_command_normalization_is_argv_only_and_selected_suite_is_explicit() -> None:
    current = adapter()
    entry = current.select("bugsinpy-thefuck-001")
    commands = current.normalize(entry)

    assert commands.baseline_argv[0] in {"pytest", "python3"}
    assert "&&" not in commands.baseline_argv
    assert commands.selected_suite_argv.count("tests/rules/test_pip_unknown_command.py::test_get_new_command") == 1
    assert commands.official_full_suite_argv is None
    assert commands.environment["network"] == "denied-during-execution"

    malformed = copy.deepcopy(entry)
    malformed["reproduction"]["argv"] = ["pytest", "tests/test.py::test_x", "&&", "echo", "bad"]
    with pytest.raises(TaskMappingError, match="shell"):
        current.normalize(malformed)


def test_external_workspace_is_owned_external_and_cleanup_is_scoped(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    parent = tmp_path / "external"
    parent.mkdir()
    sibling = parent / "user-file.txt"
    sibling.write_text("keep", encoding="utf-8")

    workspace = ExternalWorkspace.create(parent, repository_root=str(repository))
    root = workspace.root
    assert root.parent == parent
    assert not str(root).startswith(str(repository))
    (root / "owned.log").write_text("owned", encoding="utf-8")
    workspace.cleanup()

    assert not root.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"

    with pytest.raises(OSError, match="tracked repository"):
        ExternalWorkspace.create(repository, repository_root=str(repository))


def test_blocked_preflight_never_acquires_or_executes_benchmark(tmp_path: Path) -> None:
    class NoExecutionAcquirer:
        def acquire(self, url, revision, destination):
            raise AssertionError("benchmark acquisition must not happen")

        def read_gold_patch(self, framework_root, metadata_path):
            raise AssertionError("gold patch must not be read")

    runner = NoModelSmokeRunner(adapter(), NoExecutionAcquirer())
    evidence = runner.run(
        "bugsinpy-tqdm-003",
        facts=PreflightFacts(),
        external_parent=str(tmp_path),
        repository_root=str(ROOT),
        target_symbols=["tqdm.__bool__"],
    )

    assert evidence.verdict == "REAL_SMOKE_BLOCKED"
    assert evidence.cleanup_attempted is False
    assert evidence.cleanup_succeeded is True
