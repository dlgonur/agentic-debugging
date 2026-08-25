from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_debugger.evaluation import load_task
from agentic_debugger.evaluation.runner import EvaluationStatus, LifecycleStatus
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
from agentic_debugger.evaluation.verifier import EvaluationVerifier

ROOT = Path(__file__).resolve().parents[2]
CURATED = ROOT / "agentic_debugger" / "datasets" / "curated"


def patch_for(path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="\n",
    ))


def task_for(task_id: str):
    return load_task(str(CURATED / task_id / "task.json"))


CORRECT = {
    "curated-none-handling-001": ("display_name.py", "normalized_name = name.strip()", "normalized_name = name.strip() if name is not None else \"\""),
    "curated-off-by-one-002": ("recent_window.py", "end_index - (1 if requested_size == sequence_length else 0)", "end_index"),
    "curated-wrong-branch-003": ("access_branch.py", "    if employee_flag:\n        selected_branch = \"employee\"\n    elif employee_flag and pass_flag:\n        selected_branch = \"priority\"", "    if employee_flag and pass_flag:\n        selected_branch = \"priority\"\n    elif employee_flag:\n        selected_branch = \"employee\""),
    "curated-mutation-alias-004": ("labels.py", "    caller_labels = labels\n    working_labels = caller_labels\n    shared_identity = id(caller_labels) == id(working_labels)\n    if not shared_identity:\n        raise RuntimeError(\"unexpected collection identity\")", "    caller_labels = labels\n    working_labels = list(caller_labels)"),
    "curated-caller-callee-005": ("price.py", "    callee_input = caller_amount\n    return _format_price(callee_input)", "    callee_input = caller_amount * 100 if caller_representation == \"dollars\" else caller_amount\n    return _format_price(callee_input)"),
}

LOWER_RUNG_CORRECT = {
    "pdb-required-boundary-006": (
        "window_tail.py",
        "selected = values[start_index:end_index - (1 if requested_size == item_count else 0)]",
        "selected = values[start_index:end_index]",
    ),
    "pdb-required-caller-callee-007": (
        "price_pipeline.py",
        "    callee_input = caller_amount\n    return _render_cents(callee_input)",
        "    callee_input = caller_amount * 100 if caller_representation == \"dollars\" else caller_amount\n"
        "    return _render_cents(callee_input)",
    ),
    "pdb-required-multistage-units-008": (
        "deadline_pipeline.py",
        "    retry_window_ms = _expand_retry_window(value, retry_count)\n    return retry_window_ms + grace_ms",
        "    retry_window_ms = _expand_retry_window(base_delay_ms, retry_count)\n"
        "    return retry_window_ms + grace_ms",
    ),
}


@pytest.mark.parametrize("task_id", sorted(CORRECT))
def test_five_correct_curated_patches_resolve(task_id, tmp_path):
    task = task_for(task_id)
    rel, old, new = CORRECT[task_id]
    source = (CURATED / task_id / rel).read_text(encoding="utf-8")
    assert old in source
    before_hash = hashlib.sha256((CURATED / task_id / rel).read_bytes()).hexdigest()
    patch = patch_for(rel, source, source.replace(old, new))
    result = EvaluationVerifier(str(ROOT), workspace_parent=str(tmp_path)).evaluate(task, patch)
    assert result.status is EvaluationStatus.COMPLETED
    assert result.outcome is SemanticOutcome.RESOLVED
    assert result.f2p_passed == result.f2p_total
    assert result.p2p_passed == result.p2p_total
    assert result.full_suite is not None
    assert result.workspace.lifecycle is LifecycleStatus.CLEANED
    assert result.workspace.canonical_fixture_unchanged
    assert result.task_max_test_runs == 5
    assert result.verification_command_count == 4 + 2 * (result.f2p_total + result.p2p_total)
    assert result.verification_selected_test_count == 2 + 3 * (result.f2p_total + result.p2p_total)
    assert hashlib.sha256((CURATED / task_id / rel).read_bytes()).hexdigest() == before_hash


@pytest.mark.parametrize("task_id", sorted(LOWER_RUNG_CORRECT))
def test_lower_rung_verifier_replays_level6_12_18_provider_free(task_id):
    task = task_for(task_id)
    rel, old, new = LOWER_RUNG_CORRECT[task_id]
    source = (CURATED / task_id / rel).read_text(encoding="utf-8")
    assert old in source
    result = EvaluationVerifier(str(ROOT)).evaluate(
        task, patch_for(rel, source, source.replace(old, new))
    )
    assert result.status is EvaluationStatus.COMPLETED
    assert result.outcome is SemanticOutcome.RESOLVED
    assert result.baseline.valid is True
    assert result.baseline.collection is not None
    assert result.baseline.collection.passed is True
    assert result.workspace.canonical_fixture_unchanged is True


def test_noop_breaking_and_regression(tmp_path):
    task = task_for("curated-none-handling-001")
    source = (CURATED / task.task_id / "display_name.py").read_text(encoding="utf-8")
    (tmp_path / "noop").mkdir()
    noop = EvaluationVerifier(str(ROOT), workspace_parent=str(tmp_path / "noop")).evaluate(task, "")
    assert noop.status is EvaluationStatus.COMPLETED
    assert noop.outcome is SemanticOutcome.NO_OP

    breaking_source = source.replace("    normalized_name = name.strip()", "    normalized_name = name.strip() if name is not None else \"\"").replace("    return normalized_name.title()", "    return \"Anonymous\"")
    breaking_patch = patch_for("display_name.py", source, breaking_source)
    (tmp_path / "breaking").mkdir()
    breaking = EvaluationVerifier(str(ROOT), workspace_parent=str(tmp_path / "breaking")).evaluate(task, breaking_patch)
    assert breaking.status is EvaluationStatus.COMPLETED
    assert breaking.outcome is SemanticOutcome.BREAKING_RESOLVED

    regression_source = source.replace("    normalized_name = name.strip()", "    normalized_name = name.strip()\n    return \"Broken\"")
    regression_patch = patch_for("display_name.py", source, regression_source)
    (tmp_path / "regression").mkdir()
    regression = EvaluationVerifier(str(ROOT), workspace_parent=str(tmp_path / "regression")).evaluate(task, regression_patch)
    assert regression.status is EvaluationStatus.COMPLETED
    assert regression.outcome is SemanticOutcome.REGRESSION


def test_real_patch_validation_and_syntax_failure(tmp_path):
    task = task_for("curated-none-handling-001")
    evaluator = EvaluationVerifier(str(ROOT), workspace_parent=str(tmp_path))
    invalid = evaluator.evaluate(task, "not a unified diff")
    assert invalid.status is EvaluationStatus.PATCH_APPLY_FAILED
    assert invalid.outcome is None
    denied = evaluator.evaluate(task, patch_for("task.json", "{}\n", "{ }\n"))
    assert denied.status is EvaluationStatus.PATCH_APPLY_FAILED
    source = (CURATED / task.task_id / "display_name.py").read_text(encoding="utf-8")
    syntax = evaluator.evaluate(task, patch_for("display_name.py", source, source.replace("return normalized_name.title()", "return normalized_name.title(")))
    assert syntax.status is EvaluationStatus.SYNTAX_FAILED
    assert syntax.outcome is None


def test_repeated_evaluation_is_semantically_deterministic(tmp_path):
    task = task_for("curated-none-handling-001")
    source = (CURATED / task.task_id / "display_name.py").read_text(encoding="utf-8")
    rel, old, new = CORRECT[task.task_id]
    patch = patch_for(rel, source, source.replace(old, new))
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first = EvaluationVerifier(str(ROOT), workspace_parent=str(tmp_path / "first")).evaluate(task, patch)
    second = EvaluationVerifier(str(ROOT), workspace_parent=str(tmp_path / "second")).evaluate(task, patch)
    assert first.semantic_mapping() == second.semantic_mapping()
    assert "task_workspace_" not in json.dumps(first.semantic_mapping())
    assert first.workspace.canonical_fixture_unchanged and second.workspace.canonical_fixture_unchanged




def _minimal_patch(path: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-old\n+new\n"


@pytest.mark.parametrize("path", ["/absolute.py", "../traversal.py", "task.json", "tests/test_display_name.py", "other.py"])
def test_real_patch_path_rejections_are_bounded(tmp_path, path):
    task = task_for("curated-none-handling-001")
    parent = tmp_path / path.replace("/", "_").replace("\\", "_").replace("..", "up")
    parent.mkdir()
    result = EvaluationVerifier(str(ROOT), workspace_parent=str(parent)).evaluate(task, _minimal_patch(path))
    assert result.status is EvaluationStatus.PATCH_APPLY_FAILED
    assert result.outcome is None
    assert result.workspace.lifecycle is LifecycleStatus.CLEANED


def test_real_baseline_already_passing_is_rejected(tmp_path):
    base = task_for("curated-none-handling-001")
    task = replace(base, tests=replace(base.tests, fail_to_pass=[base.tests.pass_to_pass[0]], pass_to_pass=[base.tests.pass_to_pass[1], base.tests.fail_to_pass[0]]))
    parent = tmp_path / "baseline-pass"
    parent.mkdir()
    result = EvaluationVerifier(str(ROOT), workspace_parent=str(parent)).evaluate(task, "")
    assert result.status is EvaluationStatus.BASELINE_INVALID
    assert result.outcome is None
    assert result.baseline.f2p[0].status.value == "PASS"
    assert result.patch_application.attempted is False


def test_real_collection_import_error_is_infrastructure_failure(tmp_path):
    base = task_for("curated-none-handling-001")
    task = replace(base, tests=replace(base.tests, full_suite_argv=["python", "-m", "pytest", "tests/missing_node.py", "-q", "-p", "no:cacheprovider"]))
    parent = tmp_path / "collection-error"
    parent.mkdir()
    result = EvaluationVerifier(str(ROOT), workspace_parent=str(parent)).evaluate(task, "")
    assert result.status is EvaluationStatus.TEST_EXECUTION_FAILED
    assert result.outcome is None
    assert result.baseline.collection is not None
    assert not result.baseline.collection.passed


def test_real_failed_run_followed_by_valid_run_isolated(tmp_path):
    task = task_for("curated-none-handling-001")
    source = (CURATED / task.task_id / "display_name.py").read_text(encoding="utf-8")
    rel, old, new = CORRECT[task.task_id]
    patch = patch_for(rel, source, source.replace(old, new))
    bad_parent = tmp_path / "bad"
    good_parent = tmp_path / "good"
    bad_parent.mkdir(); good_parent.mkdir()
    bad = EvaluationVerifier(str(ROOT), workspace_parent=str(bad_parent)).evaluate(task, "not unified diff")
    good = EvaluationVerifier(str(ROOT), workspace_parent=str(good_parent)).evaluate(task, patch)
    assert bad.status is EvaluationStatus.PATCH_APPLY_FAILED
    assert good.status is EvaluationStatus.COMPLETED
    assert good.outcome is SemanticOutcome.RESOLVED



def test_real_non_root_manifest_cwd_is_portable_and_exact(tmp_path):
    fixture_rel = "agentic_debugger/datasets/curated/task7-non-root-cwd"
    fixture = tmp_path / fixture_rel
    project = fixture / "project"
    tests_dir = project / "tests"
    tests_dir.mkdir(parents=True)
    (project / "module.py").write_text("def value(x):\n    return x\n", encoding="utf-8")
    (tests_dir / "test_values.py").write_text(
        "from module import value\n\ndef test_first():\n    assert value(1) == 2\n\ndef test_preserved_one():\n    assert value(2) == 2\n\ndef test_preserved_two():\n    assert value(3) == 3\n",
        encoding="utf-8",
    )
    base = task_for("curated-none-handling-001")
    task = replace(
        base,
        task_id="task7-non-root-cwd",
        fixture_path=fixture_rel,
        reproduction=replace(base.reproduction, argv=["python", "-m", "pytest", "tests/test_values.py::test_first", "-q", "-p", "no:cacheprovider"], cwd="project"),
        tests=replace(base.tests, fail_to_pass=["tests/test_values.py::test_first"], pass_to_pass=["tests/test_values.py::test_preserved_one", "tests/test_values.py::test_preserved_two"], full_suite_argv=["python", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]),
        constraints=replace(base.constraints, allowed_write_paths=["project/module.py"]),
    )
    parent = tmp_path / "workspaces"
    parent.mkdir()
    result = EvaluationVerifier(str(tmp_path), workspace_parent=str(parent)).evaluate(task, "")
    assert result.status is EvaluationStatus.COMPLETED
    assert result.baseline.reproduction is not None
    assert result.baseline.reproduction.resolved_cwd == "<WORKSPACE>/project"
    assert result.post_patch_reproduction is not None
    assert result.post_patch_reproduction.resolved_cwd == "<WORKSPACE>/project"
    assert all(record.resolved_cwd == "<WORKSPACE>/project" for record in result.post_patch_f2p + result.post_patch_p2p)
    assert "task_workspace_" not in json.dumps(result.semantic_mapping())



def test_real_post_patch_timeout_is_bounded_and_cleaned(tmp_path):
    fixture_rel = "agentic_debugger/datasets/curated/task7-post-timeout"
    fixture = tmp_path / fixture_rel
    (fixture / "tests").mkdir(parents=True)
    (fixture / "module.py").write_text("def value(x):\n    return x\n", encoding="utf-8")
    (fixture / "tests" / "test_timeout.py").write_text(
        "from module import value\n\ndef test_first():\n    assert value(1) == 2\n\ndef test_preserved_one():\n    assert value(2) == 2\n\ndef test_preserved_two():\n    assert value(3) == 3\n",
        encoding="utf-8",
    )
    base = task_for("curated-none-handling-001")
    task = replace(
        base,
        task_id="task7-post-timeout",
        fixture_path=fixture_rel,
        reproduction=replace(base.reproduction, argv=["python", "-m", "pytest", "tests/test_timeout.py::test_first", "-q", "-p", "no:cacheprovider"], timeout_seconds=60),
        tests=replace(base.tests, fail_to_pass=["tests/test_timeout.py::test_first"], pass_to_pass=["tests/test_timeout.py::test_preserved_one", "tests/test_timeout.py::test_preserved_two"], full_suite_argv=["python", "-m", "pytest", "tests/test_timeout.py::test_first", "tests/test_timeout.py::test_preserved_one", "tests/test_timeout.py::test_preserved_two", "-q", "-p", "no:cacheprovider"], timeout_seconds=5),
        constraints=replace(base.constraints, allowed_write_paths=["module.py"]),
    )
    source = (fixture / "module.py").read_text(encoding="utf-8")
    patched = "import sys, time\n\ndef value(x):\n    if x == 1 and '-vv' in sys.argv:\n        time.sleep(8)\n    return 2 if x == 1 else x\n"
    patch = patch_for("module.py", source, patched)
    parent = tmp_path / "workspaces"
    parent.mkdir()
    result = EvaluationVerifier(str(tmp_path), workspace_parent=str(parent)).evaluate(task, patch)
    assert result.status is EvaluationStatus.TEST_TIMEOUT
    assert result.outcome is None
    assert result.workspace.lifecycle is LifecycleStatus.CLEANED
    assert result.baseline.valid
    assert result.post_patch_reproduction is not None
    assert result.post_patch_f2p or result.post_patch_reproduction.timed_out
    if result.post_patch_f2p:
        assert result.post_patch_f2p[0].status.value == "TIMEOUT"



def test_real_baseline_p2p_failure_is_rejected_before_patch(tmp_path):
    fixture_rel = "agentic_debugger/datasets/curated/task7-baseline-p2p"
    fixture = tmp_path / fixture_rel
    (fixture / "tests").mkdir(parents=True)
    (fixture / "module.py").write_text("def value(x):\n    return x\n", encoding="utf-8")
    (fixture / "tests" / "test_baseline.py").write_text(
        "from module import value\n\ndef test_f2p():\n    assert value(1) == 2\n\ndef test_broken_p2p():\n    assert value(2) == 3\n\ndef test_preserved_p2p():\n    assert value(3) == 3\n",
        encoding="utf-8",
    )
    base = task_for("curated-none-handling-001")
    task = replace(
        base,
        task_id="task7-baseline-p2p",
        fixture_path=fixture_rel,
        reproduction=replace(base.reproduction, argv=["python", "-m", "pytest", "tests/test_baseline.py::test_f2p", "-q", "-p", "no:cacheprovider"]),
        tests=replace(base.tests, fail_to_pass=["tests/test_baseline.py::test_f2p"], pass_to_pass=["tests/test_baseline.py::test_broken_p2p", "tests/test_baseline.py::test_preserved_p2p"], full_suite_argv=["python", "-m", "pytest", "tests/test_baseline.py::test_f2p", "tests/test_baseline.py::test_broken_p2p", "tests/test_baseline.py::test_preserved_p2p", "-q", "-p", "no:cacheprovider"]),
        constraints=replace(base.constraints, allowed_write_paths=["module.py"]),
    )
    parent = tmp_path / "workspaces"
    parent.mkdir()
    result = EvaluationVerifier(str(tmp_path), workspace_parent=str(parent)).evaluate(task, "")
    assert result.status is EvaluationStatus.BASELINE_INVALID
    assert result.outcome is None
    assert result.baseline.reason == "baseline_p2p_not_genuine_pass"
    assert result.baseline.p2p[0].status.value == "FAIL"
    assert result.workspace.lifecycle is LifecycleStatus.CLEANED



def test_real_full_suite_contradiction_is_exact_and_cleaned(tmp_path):
    fixture_rel = "agentic_debugger/datasets/curated/task7-full-contradiction"
    fixture = tmp_path / fixture_rel
    (fixture / "tests").mkdir(parents=True)
    (fixture / "module.py").write_text("def value(x):\n    return x\n", encoding="utf-8")
    (fixture / "tests" / "test_contradiction.py").write_text(
        "import sys\nfrom module import value\n\ndef test_f2p():\n    assert value(1) == 2\n\ndef test_p2p_one():\n    assert value(2) == 2\n\ndef test_p2p_two():\n    node_args = [arg for arg in sys.argv if '::' in arg]\n    assert len(node_args) < 3\n    assert value(3) == 3\n",
        encoding="utf-8",
    )
    base = task_for("curated-none-handling-001")
    nodes = ["tests/test_contradiction.py::test_f2p", "tests/test_contradiction.py::test_p2p_one", "tests/test_contradiction.py::test_p2p_two"]
    task = replace(
        base,
        task_id="task7-full-contradiction",
        fixture_path=fixture_rel,
        reproduction=replace(base.reproduction, argv=["python", "-m", "pytest", nodes[0], "-q", "-p", "no:cacheprovider"]),
        tests=replace(base.tests, fail_to_pass=[nodes[0]], pass_to_pass=nodes[1:], full_suite_argv=["python", "-m", "pytest", *nodes, "-q", "-p", "no:cacheprovider"]),
        constraints=replace(base.constraints, allowed_write_paths=["module.py"]),
    )
    source = (fixture / "module.py").read_text(encoding="utf-8")
    patch = patch_for("module.py", source, "def value(x):\n    return 2 if x == 1 else x\n")
    parent = tmp_path / "workspaces"
    parent.mkdir()
    result = EvaluationVerifier(str(tmp_path), workspace_parent=str(parent)).evaluate(task, patch)
    assert result.status is EvaluationStatus.FULL_SUITE_CONTRADICTION
    assert result.outcome is None
    assert result.full_suite is not None
    assert result.workspace.canonical_fixture_unchanged
    assert result.workspace.lifecycle is LifecycleStatus.CLEANED


def test_real_parameterized_node_resolves_with_exact_id_and_consistency(tmp_path):
    fixture_rel = "agentic_debugger/datasets/curated/task7-parameterized-node"
    fixture = tmp_path / fixture_rel
    (fixture / "tests").mkdir(parents=True)
    (fixture / "module.py").write_text("def lookup(value):\n    return None\n", encoding="utf-8")
    (fixture / "tests" / "test_param.py").write_text(
        "import pytest\nfrom module import lookup\n\n@pytest.mark.parametrize('case', [1])\ndef test_missing_case(case):\n    assert lookup(case) == 'present'\n\ndef test_preserved_one():\n    assert lookup(2) is None\n\ndef test_preserved_two():\n    assert lookup(3) is None\n",
        encoding="utf-8",
    )
    base = task_for("curated-none-handling-001")
    nodes = ["tests/test_param.py::test_missing_case[1]", "tests/test_param.py::test_preserved_one", "tests/test_param.py::test_preserved_two"]
    task = replace(
        base,
        task_id="task7-parameterized-node",
        fixture_path=fixture_rel,
        reproduction=replace(base.reproduction, argv=["python", "-m", "pytest", nodes[0], "-q", "-p", "no:cacheprovider"]),
        tests=replace(base.tests, fail_to_pass=[nodes[0]], pass_to_pass=nodes[1:], full_suite_argv=["python", "-m", "pytest", *nodes, "-q", "-p", "no:cacheprovider"]),
        constraints=replace(base.constraints, allowed_write_paths=["module.py"]),
    )
    source = (fixture / "module.py").read_text(encoding="utf-8")
    patch = patch_for("module.py", source, "def lookup(value):\n    return 'present' if value == 1 else None\n")
    parent = tmp_path / "workspaces"
    parent.mkdir()
    result = EvaluationVerifier(str(tmp_path), workspace_parent=str(parent)).evaluate(task, patch)
    assert result.status is EvaluationStatus.COMPLETED
    assert result.outcome is SemanticOutcome.RESOLVED
    assert result.baseline.collection is not None
    assert nodes[0] in result.baseline.collection.collected_nodes
    assert result.post_patch_f2p[0].node_id == nodes[0]
    assert result.full_suite is not None
    assert result.workspace.canonical_fixture_unchanged
    assert result.workspace.lifecycle is LifecycleStatus.CLEANED
