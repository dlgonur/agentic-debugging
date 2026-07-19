import copy
from typing import Any, Dict

import pytest


def _deep_copy(m: Any) -> Any:
    return copy.deepcopy(m)


VALID_TASK_MAPPING: Dict[str, Any] = {
    "schema_version": "1.0",
    "task_id": "curated-none-handling-001",
    "title": "Normalize missing display names",
    "description": "A formatting helper crashes when an optional display name is missing.",
    "language": "python",
    "fixture_path": "agentic_debugger/datasets/curated/curated-none-handling-001",
    "reproduction": {
        "argv": [
            "python",
            "-m",
            "pytest",
            "tests/test_profile.py::test_missing_display_name",
            "-q",
        ],
        "cwd": ".",
        "timeout_seconds": 10,
        "expected_exit_code": 1,
    },
    "tests": {
        "fail_to_pass": ["tests/test_profile.py::test_missing_display_name"],
        "pass_to_pass": [
            "tests/test_profile.py::test_regular_display_name",
            "tests/test_profile.py::test_whitespace_is_normalized",
        ],
        "full_suite_argv": ["python", "-m", "pytest", "-q"],
        "timeout_seconds": 20,
    },
    "constraints": {
        "allowed_write_paths": ["profile.py"],
        "denied_write_paths": ["tests", "task.json"],
        "network_allowed": False,
        "external_services_allowed": False,
        "max_patch_attempts": 2,
        "max_test_runs": 5,
        "max_pdb_observations": 8,
    },
    "oracle": {
        "bug_category": "none-handling",
        "target_files": ["profile.py"],
        "target_symbols": ["format_display_name"],
        "root_cause_summary": "The helper calls a string method before normalizing an optional None value.",
        "runtime_evidence_hint": "The failing frame shows that the local name value is None while the normal path contains a string.",
    },
    "tags": ["curated", "runtime-state", "none-handling"],
}


@pytest.fixture
def valid_task_mapping() -> Dict[str, Any]:
    return _deep_copy(VALID_TASK_MAPPING)
