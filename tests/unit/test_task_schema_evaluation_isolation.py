from agentic_debugger.evaluation.task_schema import (
    EXTERNAL_TIMEOUT_SECONDS_MAX,
    HIDDEN_TEST_PLACEHOLDER,
    NO_PUBLIC_REPRODUCTION,
    DebugTask,
)


def _external_mapping():
    return {
        "schema_version": "1.0",
        "task_id": "swr-example-repo-12",
        "title": "Example",
        "description": "Public issue text",
        "language": "python",
        "fixture_path": "external/example",
        "source": {
            "kind": "external",
            "path": "external/example",
            "provenance": {
                "dataset": "nebius/SWE-rebench-V2",
                "manifest_id": "example__repo-12",
                "manifest_fingerprint": "a" * 40,
                "upstream_repository": "https://github.com/example/repo",
                "upstream_revision": "a" * 40,
                "project": "example/repo",
                "bug_id": "example__repo-12",
                "buggy_revision": "a" * 40,
                "fixed_revision": "withheld-from-model",
            },
        },
        "reproduction": {
            "argv": ["python", "-m", "pytest", "tests/test_hidden.py::test_hidden", "-q"],
            "cwd": ".",
            "timeout_seconds": 120,
            "expected_exit_code": 1,
        },
        "tests": {
            "fail_to_pass": ["tests/test_hidden.py::test_hidden"],
            "pass_to_pass": ["tests/test_hidden.py::test_ok"],
            "full_suite_argv": ["python", "-m", "pytest", "-q"],
            "timeout_seconds": 180,
        },
        "constraints": {
            "allowed_write_paths": ["pkg"],
            "denied_write_paths": ["tests", "task.json"],
            "network_allowed": False,
            "external_services_allowed": False,
            "max_patch_attempts": 3,
            "max_test_runs": 12,
            "max_pdb_observations": 8,
        },
        "oracle": {
            "bug_category": "swe-rebench-v2",
            "target_files": ["task.json"],
            "target_symbols": ["withheld"],
            "root_cause_summary": "withheld",
            "runtime_evidence_hint": "withheld",
        },
        "tags": ["swe-rebench-v2"],
        "evaluation_isolation": {"hide_test_identities_from_model": True},
    }


def test_external_timeouts_above_curated_cap_are_accepted():
    task = DebugTask.from_mapping(_external_mapping())
    assert task.tests.timeout_seconds == 180
    assert task.reproduction.timeout_seconds == 120
    assert task.tests.timeout_seconds <= EXTERNAL_TIMEOUT_SECONDS_MAX


def test_external_tasks_may_have_empty_pass_to_pass():
    mapping = _external_mapping()
    mapping["tests"]["pass_to_pass"] = []
    task = DebugTask.from_mapping(mapping)
    assert task.tests.pass_to_pass == []


def test_evaluation_isolation_redacts_hidden_tests_from_agent_view():
    task = DebugTask.from_mapping(_external_mapping())
    visible = task.agent_visible_mapping()
    assert "oracle" not in visible
    assert visible["tests"]["fail_to_pass"] == [HIDDEN_TEST_PLACEHOLDER]
    assert "test_hidden" not in repr(visible["tests"])
    assert visible["reproduction"]["argv"] == [NO_PUBLIC_REPRODUCTION]
    assert task.tests.fail_to_pass == ["tests/test_hidden.py::test_hidden"]
    assert "fixed_revision" not in visible["source"]["provenance"]
