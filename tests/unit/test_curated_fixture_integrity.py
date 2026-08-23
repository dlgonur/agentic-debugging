from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_debugger.evaluation.task_schema import DebugTask


REPO_ROOT = Path(__file__).resolve().parents[2]
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"
R6_SPLIT_MANIFEST = (
    REPO_ROOT / "experiments" / "r6_debugger_training" / "split_manifest.json"
)
R6_GENERATED_PREFIX = "quixbugs-"
EXPECTED_FIXTURES = {
    "curated-none-handling-001": ("display_name.py", "tests/test_display_name.py"),
    "curated-off-by-one-002": ("recent_window.py", "tests/test_recent_window.py"),
    "curated-wrong-branch-003": ("access_branch.py", "tests/test_access_branch.py"),
    "curated-mutation-alias-004": ("labels.py", "tests/test_labels.py"),
    "curated-caller-callee-005": ("price.py", "tests/test_price.py"),
    "pdb-required-boundary-006": ("window_tail.py", "tests/test_window_tail.py"),
    "pdb-required-caller-callee-007": ("price_pipeline.py", "tests/test_price_pipeline.py"),
    "pdb-required-multistage-units-008": ("deadline_pipeline.py", "tests/test_deadline_pipeline.py"),
}
SUMMARY_TOKEN = re.compile(
    r"(?P<count>\d+)\s+(?P<label>failed|passed|skipped|error|errors|xfailed|xpassed)\b"
)


def _fixture_dir(task_id: str) -> Path:
    return CURATED_ROOT / task_id


def _r6_manifest_entries(
    manifest_path: Path = R6_SPLIT_MANIFEST,
) -> dict[str, dict[str, str]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "r6-debugger-sft-split-v1"
    entries = [*manifest["train_tasks"], *manifest["validation_tasks"]]
    task_ids = [entry["task_id"] for entry in entries]
    assert len(task_ids) == len(set(task_ids)), "R6 manifest contains duplicate task IDs"
    return {entry["task_id"]: entry for entry in entries}


def _assert_generated_r6_corpus(
    root: Path,
    *,
    manifest_path: Path = R6_SPLIT_MANIFEST,
    require_complete: bool = True,
) -> None:
    generated = {
        path.name: path
        for path in root.iterdir()
        if path.is_dir() and path.name.startswith(R6_GENERATED_PREFIX)
    }
    if not generated:
        return

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = _r6_manifest_entries(manifest_path)
    present_ids = set(generated)
    expected_ids = set(entries)
    unknown_ids = present_ids - expected_ids
    assert not unknown_ids, (
        f"unexpected generated QuixBugs fixture directories: "
        f"{sorted(unknown_ids)}"
    )
    if require_complete:
        assert present_ids == expected_ids, (
            "generated QuixBugs corpus must be complete when materialized: "
            f"missing={sorted(expected_ids - present_ids)}, "
            f"unexpected={sorted(present_ids - expected_ids)}"
        )

    for task_id, fixture in generated.items():
        entry = entries[task_id]
        algorithm = entry["algo"]
        source = fixture / f"{algorithm}.py"
        assert source.is_file(), f"missing generated QuixBugs source: {source}"
        actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        assert actual_hash == entry["source_sha256"], (
            f"generated QuixBugs source hash mismatch for {task_id}: "
            f"expected {entry['source_sha256']}, got {actual_hash}"
        )

        task_path = fixture / "task.json"
        assert task_path.is_file(), f"missing generated QuixBugs task manifest: {task_path}"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        assert task["task_id"] == task_id
        assert task["fixture_path"] == (
            f"agentic_debugger/datasets/curated/{task_id}"
        )
        provenance = task["source"]["provenance"]
        assert provenance["dataset"] == "QuixBugs"
        assert provenance["upstream_revision"] == manifest["quixbugs_revision"]
        assert provenance["bug_id"] == algorithm


def _assert_curated_directory_set(root: Path) -> None:
    assert root.is_dir()
    directory_names = {
        path.name for path in root.iterdir() if path.is_dir()
    }
    generated_names = {
        name for name in directory_names if name.startswith(R6_GENERATED_PREFIX)
    }
    assert directory_names - generated_names == set(EXPECTED_FIXTURES)
    _assert_generated_r6_corpus(root)


def _snapshot(directory: Path) -> dict[str, bytes]:
    """Capture every file currently present, including pre-existing generated files."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _is_standard_bytecode(directory: Path, path: Path) -> bool:
    relative_parts = path.relative_to(directory).parts
    return path.suffix == ".pyc" and "__pycache__" in relative_parts


def _assert_canonical_payload_files(
    directory: Path, expected_files: set[str]
) -> None:
    actual_files = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and not _is_standard_bytecode(directory, path)
    }
    assert actual_files == expected_files, (
        f"canonical payload mismatch for {directory}: "
        f"expected {sorted(expected_files)}, got {sorted(actual_files)}"
    )


def _subprocess_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    for name in (
        "PATH", "PATHEXT", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
        "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    environment.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    return environment


def _run(
    argv: list[str], cwd: Path, timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        shell=False,
    )


def _resolve_manifest_cwd(fixture: Path, manifest_cwd: str) -> Path:
    fixture_root = fixture.resolve()
    resolved = (fixture_root / manifest_cwd).resolve()
    assert resolved.is_relative_to(fixture_root), (
        f"manifest cwd escapes fixture root: {manifest_cwd!r}"
    )
    assert resolved.is_dir(), f"manifest cwd is not a directory: {manifest_cwd!r}"
    return resolved


def _pytest_index(argv: list[str]) -> int:
    try:
        return argv.index("pytest")
    except ValueError as exc:
        raise AssertionError(f"argv is not a pytest command: {argv!r}") from exc


def _node_argv(task: DebugTask, node_id: str) -> list[str]:
    argv = list(task.reproduction.argv)
    node_index = _pytest_index(argv) + 1
    assert node_index < len(argv) and "::" in argv[node_index]
    argv[node_index] = node_id
    return argv


def _combined_argv(argv: list[str], node_ids: list[str]) -> list[str]:
    pytest_index = _pytest_index(argv)
    return argv[: pytest_index + 1] + node_ids + argv[pytest_index + 1 :]


def _collection_argv(task: DebugTask) -> list[str]:
    return [*task.tests.full_suite_argv, "--collect-only"]


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}\n{result.stderr}"


def _assert_exit(
    result: subprocess.CompletedProcess[str], expected: int, label: str
) -> None:
    assert result.returncode == expected, (
        f"{label} returned {result.returncode}, expected {expected}\n{_output(result)}"
    )


def _summary_counts(output: str, label: str) -> dict[str, int]:
    summary_lines = [
        line.strip()
        for line in output.splitlines()
        if re.search(r"\bin\s+\d+(?:\.\d+)?s\b", line)
    ]
    assert summary_lines, f"{label} has no bounded pytest summary\n{output}"
    summary = summary_lines[-1]
    counts: dict[str, int] = {}
    for match in SUMMARY_TOKEN.finditer(summary):
        name = match.group("label")
        if name == "errors":
            name = "error"
        counts[name] = int(match.group("count"))
    assert counts, f"{label} summary has no test outcome counts: {summary!r}"
    return counts


def _assert_pytest_outcome(
    result: subprocess.CompletedProcess[str],
    expected_passed: int,
    label: str,
) -> None:
    _assert_exit(result, 1, label)
    output = _output(result)
    assert "ERROR collecting" not in output, f"{label} had a collection error\n{output}"
    counts = _summary_counts(output, label)
    assert counts == {"failed": 1, "passed": expected_passed}, (
        f"{label} had unexpected pytest outcomes: {counts}\n{output}"
    )


def _function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _normalize_node_id(raw_node_id: str, fixture: Path) -> str | None:
    text = raw_node_id.strip().replace("\\", "/")
    marker = "tests/"
    marker_index = text.find(marker)
    if marker_index < 0:
        return None
    normalized = text[marker_index:]
    return normalized if "::" in normalized else None


def _collect_node_ids(output: str, fixture: Path) -> list[str]:
    node_ids = [
        normalized
        for line in output.splitlines()
        if (normalized := _normalize_node_id(line, fixture)) is not None
    ]
    return node_ids


def _assert_exact_full_suite_collection(
    task: DebugTask, fixture: Path, fixture_cwd: Path
) -> list[str]:
    before = _snapshot(fixture)
    result = _run(
        _collection_argv(task), fixture_cwd, task.tests.timeout_seconds
    )
    _assert_exit(result, 0, f"collect full suite {task.task_id}")
    output = _output(result)
    assert "ERROR collecting" not in output
    collected = _collect_node_ids(result.stdout, fixture)
    expected = [*task.tests.fail_to_pass, *task.tests.pass_to_pass]
    assert len(collected) == len(set(collected)), (
        f"duplicate collected node IDs for {task.task_id}: {collected}"
    )
    assert set(collected) == set(expected), (
        f"full-suite node mismatch for {task.task_id}: "
        f"expected {sorted(expected)}, got {sorted(collected)}"
    )
    assert _snapshot(fixture) == before
    return collected


def _assert_individual_node(
    task: DebugTask,
    fixture: Path,
    fixture_cwd: Path,
    node_id: str,
    expected_exit: int,
) -> None:
    before = _snapshot(fixture)
    result = _run(
        _node_argv(task, node_id), fixture_cwd, task.tests.timeout_seconds
    )
    _assert_exit(result, expected_exit, f"individual {task.task_id} {node_id}")
    assert _snapshot(fixture) == before


def test_curated_directory_set_is_exact() -> None:
    _assert_curated_directory_set(CURATED_ROOT)


def test_generated_r6_corpus_is_optional_on_clean_checkout(tmp_path: Path) -> None:
    for task_id in EXPECTED_FIXTURES:
        (tmp_path / task_id).mkdir()

    _assert_curated_directory_set(tmp_path)


def test_generated_r6_corpus_rejects_unknown_task_id(tmp_path: Path) -> None:
    (tmp_path / "quixbugs-not-in-manifest").mkdir()

    with pytest.raises(AssertionError, match="unexpected generated QuixBugs"):
        _assert_generated_r6_corpus(tmp_path, require_complete=False)


def test_generated_r6_corpus_rejects_corrupted_source(tmp_path: Path) -> None:
    entries = _r6_manifest_entries()
    task_id, entry = next(iter(entries.items()))
    fixture = tmp_path / task_id
    fixture.mkdir()
    (fixture / f"{entry['algo']}.py").write_text(
        "# deliberately corrupted source\n", encoding="utf-8"
    )

    with pytest.raises(AssertionError, match="source hash mismatch"):
        _assert_generated_r6_corpus(tmp_path, require_complete=False)


def test_generated_r6_corpus_rejects_partial_materialization(tmp_path: Path) -> None:
    task_id = next(iter(_r6_manifest_entries()))
    (tmp_path / task_id).mkdir()

    with pytest.raises(AssertionError, match="must be complete"):
        _assert_generated_r6_corpus(tmp_path)


def test_curated_directory_set_rejects_unrelated_directory(tmp_path: Path) -> None:
    for task_id in EXPECTED_FIXTURES:
        (tmp_path / task_id).mkdir()
    (tmp_path / "random-junk").mkdir()

    with pytest.raises(AssertionError):
        _assert_curated_directory_set(tmp_path)


def test_canonical_payload_rejects_arbitrary_extra_file(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    (fixture / "tests").mkdir(parents=True)
    (fixture / "task.json").write_bytes(b"{}")
    (fixture / "source.py").write_bytes(b"def target(): pass\n")
    (fixture / "tests" / "test_source.py").write_bytes(b"def test_target(): pass\n")
    (fixture / "unexpected.txt").write_bytes(b"not part of the payload")

    with pytest.raises(AssertionError, match="canonical payload mismatch"):
        _assert_canonical_payload_files(
            fixture, {"task.json", "source.py", "tests/test_source.py"}
        )


def test_canonical_payload_ignores_only_standard_bytecode(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    (fixture / "tests").mkdir(parents=True)
    (fixture / "__pycache__").mkdir()
    (fixture / "task.json").write_bytes(b"{}")
    (fixture / "source.py").write_bytes(b"def target(): pass\n")
    (fixture / "tests" / "test_source.py").write_bytes(b"def test_target(): pass\n")
    (fixture / "__pycache__" / "source.cpython-314.pyc").write_bytes(b"bytecode")
    expected = {"task.json", "source.py", "tests/test_source.py"}

    _assert_canonical_payload_files(fixture, expected)
    (fixture / "__pycache__" / "metadata.json").write_bytes(b"not bytecode")
    with pytest.raises(AssertionError, match="canonical payload mismatch"):
        _assert_canonical_payload_files(fixture, expected)


def test_manifest_cwd_resolution_rejects_escape(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    forged_task = SimpleNamespace(
        reproduction=SimpleNamespace(cwd="../outside")
    )
    with pytest.raises(AssertionError, match="escapes fixture root"):
        _resolve_manifest_cwd(fixture, forged_task.reproduction.cwd)

    with pytest.raises(AssertionError, match="not a directory"):
        _resolve_manifest_cwd(fixture, "missing")


def test_manifests_match_directories_and_hide_oracles() -> None:
    for task_id, (source, test_path) in EXPECTED_FIXTURES.items():
        fixture = _fixture_dir(task_id)
        task_path = fixture / "task.json"
        task = DebugTask.from_file(str(task_path))
        raw = json.loads(task_path.read_text(encoding="utf-8"))
        fixture_cwd = _resolve_manifest_cwd(fixture, task.reproduction.cwd)
        assert task.task_id == task_id
        assert task.fixture_path == f"agentic_debugger/datasets/curated/{task_id}"
        assert (REPO_ROOT / task.fixture_path).resolve() == fixture.resolve()
        assert fixture.resolve().is_relative_to(CURATED_ROOT.resolve())
        assert task.to_mapping() == raw
        assert "oracle" not in task.agent_visible_mapping()
        assert task.oracle.target_files == [source]
        assert (fixture / source).is_file() and (fixture / test_path).is_file()
        _assert_canonical_payload_files(fixture, {"task.json", source, test_path})
        assert set(task.oracle.target_symbols) <= _function_names(fixture / source)
        assert not set(task.tests.fail_to_pass) & set(task.tests.pass_to_pass)
        assert task.reproduction.argv == _node_argv(
            task, task.tests.fail_to_pass[0]
        )
        for node_id in [*task.tests.fail_to_pass, *task.tests.pass_to_pass]:
            _assert_individual_node(
                task,
                fixture,
                fixture_cwd,
                node_id,
                1 if node_id in task.tests.fail_to_pass else 0,
            )
        _assert_exact_full_suite_collection(task, fixture, fixture_cwd)


def test_fixture_modules_are_unique_and_have_no_machine_or_network_paths() -> None:
    source_names: list[str] = []
    test_names: list[str] = []
    absolute_path = str(REPO_ROOT).replace("\\", "/").encode("utf-8")
    for task_id in EXPECTED_FIXTURES:
        fixture = _fixture_dir(task_id)
        source, test_path = EXPECTED_FIXTURES[task_id]
        paths = [fixture / "task.json", fixture / source, fixture / test_path]
        for path in paths:
            contents = path.read_bytes()
            assert absolute_path not in contents
            assert b"http://" not in contents and b"https://" not in contents
        source_names.append(source)
        test_names.append(test_path.rsplit("/", maxsplit=1)[-1])
    assert len(source_names) == len(set(source_names))
    assert len(test_names) == len(set(test_names))


@pytest.mark.parametrize("task_id", sorted(EXPECTED_FIXTURES))
def test_baseline_behavior_is_deterministic_and_immutable(task_id: str) -> None:
    fixture = _fixture_dir(task_id)
    task = DebugTask.from_file(str(fixture / "task.json"))
    fixture_cwd = _resolve_manifest_cwd(fixture, task.reproduction.cwd)
    baseline = _snapshot(fixture)
    fail_to_pass = task.tests.fail_to_pass[0]

    reproduction = _run(
        list(task.reproduction.argv),
        fixture_cwd,
        task.reproduction.timeout_seconds,
    )
    _assert_exit(reproduction, task.reproduction.expected_exit_code, f"reproduction {task_id}")
    assert _snapshot(fixture) == baseline

    for node_id in [fail_to_pass, *task.tests.pass_to_pass]:
        _assert_individual_node(
            task,
            fixture,
            fixture_cwd,
            node_id,
            1 if node_id == fail_to_pass else 0,
        )

    _assert_exact_full_suite_collection(task, fixture, fixture_cwd)
    full_suite = _run(
        list(task.tests.full_suite_argv), fixture_cwd, task.tests.timeout_seconds
    )
    _assert_pytest_outcome(full_suite, len(task.tests.pass_to_pass), f"full suite {task_id}")
    assert _snapshot(fixture) == baseline

    for label, nodes in (
        ("combined declared order", [fail_to_pass, *task.tests.pass_to_pass]),
        ("combined reversed order", list(reversed([fail_to_pass, *task.tests.pass_to_pass]))),
    ):
        combined = _run(
            _combined_argv(task.tests.full_suite_argv, nodes),
            fixture_cwd,
            task.tests.timeout_seconds,
        )
        _assert_pytest_outcome(
            combined, len(task.tests.pass_to_pass), f"{label} {task_id}"
        )
        assert _snapshot(fixture) == baseline

    repeated_full_suite = _run(
        list(task.tests.full_suite_argv), fixture_cwd, task.tests.timeout_seconds
    )
    _assert_pytest_outcome(
        repeated_full_suite, len(task.tests.pass_to_pass), f"repeated full suite {task_id}"
    )
    assert _snapshot(fixture) == baseline
