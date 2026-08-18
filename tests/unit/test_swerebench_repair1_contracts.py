from pathlib import Path

import pytest

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.external_runtime import (
    is_external_isolated_task,
    validate_model_selected_pdb_target,
    validate_public_runtime_target,
)
from agentic_debugger.demo.sanitize import (
    GENERIC_BEHAVIORAL_FAILURE,
    extract_production_exception,
    sanitize_failure_output,
)
from agentic_debugger.demo.tools import build_registry
from agentic_debugger.evaluation.outcome_taxonomy import (
    OutcomeInputError,
    SemanticOutcome,
    classify_outcome,
)
from agentic_debugger.evaluation.task_schema import (
    NO_PUBLIC_REPRODUCTION,
    DebugTask,
)
from agentic_debugger.events.schema import Action, ObservationStatus
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.swerebench.mapping import build_model_task
from agentic_debugger.swerebench.pdb_readiness import pdb_was_exercised
from agentic_debugger.swerebench.provenance import (
    harness_content_sha256,
    require_harness_match,
)
from agentic_debugger.swerebench.records import (
    OfficialInstanceBundle,
    PublicInstanceRecord,
    VerifierPrivateRecord,
)
from agentic_debugger.swerebench.selection import OrderedTask


def _ordered() -> OrderedTask:
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


def _bundle() -> OfficialInstanceBundle:
    gold = "diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-old_unique_gold_line_xyz\n+new_unique_gold_line_xyz\n"
    tests = "diff --git a/tests/test_hidden.py b/tests/test_hidden.py\n+def test_hidden_oracle():\n+    assert False\n"
    public = PublicInstanceRecord(
        instance_id="example__repo-12",
        repo="example/repo",
        base_commit="a" * 40,
        problem_statement="The formatter mishandles empty input.",
        language="python",
        license="MIT",
        created_at="2024-01-01",
        problem_statement_sha256="c" * 64,
    )
    private = VerifierPrivateRecord(
        instance_id="example__repo-12",
        fail_to_pass=("tests/test_hidden.py::test_hidden_oracle",),
        pass_to_pass=("tests/test_hidden.py::test_ok",),
        test_cmd="pytest",
        image_name="docker.io/swerebenchv2/example:1",
        python_version="3.11",
        has_gold_patch=True,
        has_test_patch=True,
        gold_patch_sha256="d" * 64,
        test_patch_sha256="e" * 64,
    )
    return OfficialInstanceBundle(
        public=public,
        private=private,
        _gold_patch=gold,
        _test_patch=tests,
        _fail_to_pass=private.fail_to_pass,
        _pass_to_pass=private.pass_to_pass,
        _test_cmd="pytest",
        _install_config={"test_cmd": "pytest", "log_parser": "parse_log_pytest"},
        _image_name="docker.io/swerebenchv2/example:1",
    )


def _action(name: str, state: ControllerState, arguments: dict) -> Action:
    return Action(
        action_id="action-000000000",
        run_id="demo-run",
        task_id="swr-example-repo-12",
        state=state,
        name=name,
        arguments=arguments,
    )


def test_empty_p2p_is_vacuous_resolved_not_a_dead_end():
    assert classify_outcome([True], []) is SemanticOutcome.RESOLVED
    assert classify_outcome([False], []) is SemanticOutcome.NO_OP
    with pytest.raises(OutcomeInputError):
        classify_outcome([], [True])


def test_repo_scale_sanitizer_does_not_need_one_top_level_py():
    raw = (
        "pkg/mod.py:12: ValueError\n"
        "E   ValueError: boom from production\n"
    )
    diagnostic = sanitize_failure_output(
        raw,
        None,
        "",
        production_paths=["pkg", "setup.py", "__main__.py"],
    )
    assert "ValueError" in diagnostic.text
    assert diagnostic.text != GENERIC_BEHAVIORAL_FAILURE
    extracted = extract_production_exception(
        raw, "", production_paths=["pkg"]
    )
    assert extracted is not None
    assert extracted.cls == "ValueError"


def test_sanitizer_still_fails_closed_on_test_frames():
    raw = (
        "tests/test_hidden.py:3: AssertionError\n"
        "E   AssertionError: expected 1\n"
    )
    diagnostic = sanitize_failure_output(
        raw, None, "", production_paths=["pkg"]
    )
    assert diagnostic.text == GENERIC_BEHAVIORAL_FAILURE


def test_model_task_has_no_public_reproduction_and_empty_p2p():
    task = build_model_task(
        _ordered(),
        _bundle(),
        fixture_path="sources/example",
        allowed_write_paths=["pkg", "setup.py"],
    )
    assert is_external_isolated_task(task)
    assert task.tests.pass_to_pass == []
    visible = task.agent_visible_mapping()
    assert visible["reproduction"]["argv"] == [NO_PUBLIC_REPRODUCTION]
    assert visible["public_reproduction"] == "not_declared"
    assert "test_hidden_oracle" not in repr(visible)
    assert "new_unique_gold_line_xyz" not in repr(visible)


def test_public_runtime_target_and_pdb_target_are_fail_closed(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "pkg").mkdir()
    (src / "pkg" / "mod.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (src / "tests").mkdir()
    (src / "tests" / "test_hidden.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
    workspace = TaskWorkspace(str(src), parent_dir=str(tmp_path))
    try:
        assert validate_public_runtime_target(workspace, "pkg/mod.py") == "pkg/mod.py"
        with pytest.raises(ValueError, match="hidden"):
            validate_public_runtime_target(
                workspace,
                "tests/test_hidden.py::test_hidden_oracle",
                hidden_identities=["tests/test_hidden.py::test_hidden_oracle"],
            )
        path, line, symbol = validate_model_selected_pdb_target(
            workspace,
            "pkg/mod.py",
            1,
            prefixes=["pkg"],
            symbol="foo",
        )
        assert path == "pkg/mod.py"
        assert line == 1
        assert symbol == "foo"
        with pytest.raises(ValueError, match="not a production"):
            validate_model_selected_pdb_target(
                workspace, "tests/test_hidden.py", 1, prefixes=["pkg"]
            )
        with pytest.raises(ValueError, match="outside"):
            validate_model_selected_pdb_target(
                workspace, "pkg/mod.py", 99, prefixes=["pkg"]
            )
        with pytest.raises(ValueError, match="not defined"):
            validate_model_selected_pdb_target(
                workspace, "pkg/mod.py", 1, prefixes=["pkg"], symbol="missing"
            )
    finally:
        workspace.cleanup()


def test_empty_external_p2p_regression_is_not_a_dead_end(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "pkg.py").write_text("x = 1\n", encoding="utf-8")
    (src / ".git").mkdir()
    (src / "task.json").write_text("{}", encoding="utf-8")
    (src / "tests").mkdir()
    workspace = TaskWorkspace(str(src), parent_dir=str(tmp_path))
    task = build_model_task(
        _ordered(),
        _bundle(),
        fixture_path="sources/example",
        allowed_write_paths=["pkg.py"],
    )
    from agentic_debugger.demo.tools import DemoToolContext

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    try:
        registry = build_registry(context)
        observation = registry.dispatch(
            _action("run_regression_tests", ControllerState.VALIDATE, {}),
            observation_id="observation-000000000",
        )
        assert observation.status is ObservationStatus.OK
        assert observation.payload["empty_official_p2p"] is True
        assert observation.payload["all_passed"] is True
        assert context.regression_passed is True
    finally:
        context.release_pdb()
        workspace.cleanup()


def test_isolated_reproduction_without_public_target_is_rejected(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "pkg.py").write_text("x = 1\n", encoding="utf-8")
    (src / ".git").mkdir()
    (src / "task.json").write_text("{}", encoding="utf-8")
    (src / "tests").mkdir()
    workspace = TaskWorkspace(str(src), parent_dir=str(tmp_path))
    task = build_model_task(
        _ordered(),
        _bundle(),
        fixture_path="sources/example",
        allowed_write_paths=["pkg.py"],
    )
    from agentic_debugger.demo.tools import DemoToolContext

    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    try:
        registry = build_registry(context)
        observation = registry.dispatch(
            _action("run_reproduction", ControllerState.REPRODUCE, {"phase": "baseline"}),
            observation_id="observation-000000000",
        )
        assert observation.status is ObservationStatus.REJECTED
    finally:
        context.release_pdb()
        workspace.cleanup()


def test_pdb_exercised_requires_real_session_and_action():
    assert pdb_was_exercised(pdb_entered=True, debugger_actions=1, paused=True)
    assert not pdb_was_exercised(pdb_entered=False, debugger_actions=0, paused=False)
    assert not pdb_was_exercised(pdb_entered=True, debugger_actions=0, paused=True)


def test_external_pdb_readiness_is_conservative_for_pilot10():
    from agentic_debugger.swerebench.pdb_readiness import classify_pdb_readiness

    readiness = classify_pdb_readiness("example__repo-12", has_official_fail_to_pass=True)
    assert readiness.classification == "PDB_DEFERRED_TO_SEPARATE_TREATMENT"
    assert readiness.gate_can_open_under_policy is False


def test_harness_provenance_fails_closed_on_mismatch():
    actual = harness_content_sha256()
    with pytest.raises(ValueError, match="does not match"):
        require_harness_match("0" * 64)
    require_harness_match(actual)
