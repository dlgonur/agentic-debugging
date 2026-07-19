import copy
import json
import tempfile
from pathlib import Path

import pytest

from agentic_debugger import SchemaValidationError
from agentic_debugger.evaluation.task_schema import (
    TASK_SCHEMA_VERSION,
    DebugTask,
    Reproduction,
    Tests,
    Constraints,
    Oracle,
)


class TestDebugTaskValid:
    def test_valid_mapping_loads(self, valid_task_mapping):
        task = DebugTask.from_mapping(valid_task_mapping)
        assert task.schema_version == TASK_SCHEMA_VERSION
        assert task.task_id == "curated-none-handling-001"
        assert task.title == "Normalize missing display names"
        assert task.language == "python"
        assert isinstance(task.reproduction, Reproduction)
        assert isinstance(task.tests, Tests)
        assert isinstance(task.constraints, Constraints)
        assert isinstance(task.oracle, Oracle)
        assert task.tags == ["curated", "runtime-state", "none-handling"]

    def test_valid_file_loads(self, valid_task_mapping):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        ) as f:
            json.dump(valid_task_mapping, f)
            tmp_path = f.name
        try:
            task = DebugTask.from_file(tmp_path)
            assert task.task_id == "curated-none-handling-001"
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_round_trip(self, valid_task_mapping):
        task = DebugTask.from_mapping(valid_task_mapping)
        result = task.to_mapping()
        assert result["schema_version"] == TASK_SCHEMA_VERSION
        assert result["task_id"] == "curated-none-handling-001"
        assert result["reproduction"]["argv"] == valid_task_mapping["reproduction"]["argv"]
        assert result["tests"]["fail_to_pass"] == ["tests/test_profile.py::test_missing_display_name"]
        assert result["oracle"]["bug_category"] == "none-handling"
        assert result["tags"] == ["curated", "runtime-state", "none-handling"]

    def test_oracle_retained_internally(self, valid_task_mapping):
        task = DebugTask.from_mapping(valid_task_mapping)
        assert task.oracle.bug_category == "none-handling"
        assert task.oracle.target_files == ["profile.py"]
        assert task.oracle.root_cause_summary != ""

    def test_agent_visible_excludes_oracle(self, valid_task_mapping):
        task = DebugTask.from_mapping(valid_task_mapping)
        visible = task.agent_visible_mapping()
        assert "oracle" not in visible
        assert visible["task_id"] == "curated-none-handling-001"
        assert visible["title"] == "Normalize missing display names"
        assert "reproduction" in visible
        assert "tests" in visible
        assert "constraints" in visible

    def test_agent_visible_does_not_mutate_original(self, valid_task_mapping):
        task = DebugTask.from_mapping(valid_task_mapping)
        visible = task.agent_visible_mapping()
        assert "oracle" not in visible
        assert task.oracle.bug_category == "none-handling"


class TestDebugTaskRejections:
    def test_unsupported_schema_version_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["schema_version"] = "2.0"
        with pytest.raises(SchemaValidationError, match="Unsupported schema version"):
            DebugTask.from_mapping(mapping)

    def test_missing_field_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        del mapping["title"]
        with pytest.raises(SchemaValidationError, match="Missing required fields"):
            DebugTask.from_mapping(mapping)

    def test_unknown_field_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["extra_field"] = "unexpected"
        with pytest.raises(SchemaValidationError, match="Unknown fields"):
            DebugTask.from_mapping(mapping)

    def test_invalid_task_id_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["task_id"] = "UPPERCASE-ID"
        with pytest.raises(SchemaValidationError, match="Invalid task_id"):
            DebugTask.from_mapping(mapping)

    def test_empty_task_id_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["task_id"] = ""
        with pytest.raises(SchemaValidationError, match="non-empty string"):
            DebugTask.from_mapping(mapping)

    def test_short_task_id_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["task_id"] = "ab"
        with pytest.raises(SchemaValidationError, match="Invalid task_id"):
            DebugTask.from_mapping(mapping)

    def test_absolute_path_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["fixture_path"] = "/absolute/path"
        with pytest.raises(SchemaValidationError, match="relative path"):
            DebugTask.from_mapping(mapping)

    def test_windows_absolute_path_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["fixture_path"] = "C:\\Users\\foo"
        with pytest.raises(SchemaValidationError, match="relative path"):
            DebugTask.from_mapping(mapping)

    def test_traversal_path_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["fixture_path"] = "../evil/task"
        with pytest.raises(SchemaValidationError, match="path traversal"):
            DebugTask.from_mapping(mapping)

    def test_traversal_path_deep_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["fixture_path"] = "foo/../../bar"
        with pytest.raises(SchemaValidationError, match="path traversal"):
            DebugTask.from_mapping(mapping)

    def test_empty_argv_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["reproduction"]["argv"] = []
        with pytest.raises(SchemaValidationError, match="non-empty"):
            DebugTask.from_mapping(mapping)

    def test_string_command_instead_of_argv_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["reproduction"]["argv"] = "python -m pytest test.py"
        with pytest.raises(SchemaValidationError, match="must be a list"):
            DebugTask.from_mapping(mapping)

    def test_duplicate_fail_to_pass_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        dup = mapping["tests"]["fail_to_pass"][0]
        mapping["tests"]["fail_to_pass"] = [dup, dup]
        with pytest.raises(SchemaValidationError, match="exactly 1 node ID"):
            DebugTask.from_mapping(mapping)

    def test_duplicate_pass_to_pass_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        dup = mapping["tests"]["pass_to_pass"][0]
        mapping["tests"]["pass_to_pass"] = [dup, dup, "other::test"]
        with pytest.raises(SchemaValidationError, match="duplicate"):
            DebugTask.from_mapping(mapping)

    def test_f2p_p2p_overlap_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        overlapping = mapping["tests"]["fail_to_pass"][0]
        mapping["tests"]["pass_to_pass"] = [overlapping, "other::test_a", "other::test_b"]
        with pytest.raises(SchemaValidationError, match="overlap"):
            DebugTask.from_mapping(mapping)

    def test_timeout_below_min_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["reproduction"]["timeout_seconds"] = 0
        with pytest.raises(SchemaValidationError, match="range"):
            DebugTask.from_mapping(mapping)

    def test_timeout_above_max_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["reproduction"]["timeout_seconds"] = 61
        with pytest.raises(SchemaValidationError, match="range"):
            DebugTask.from_mapping(mapping)

    def test_budget_out_of_range_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["max_patch_attempts"] = 0
        with pytest.raises(SchemaValidationError, match="range"):
            DebugTask.from_mapping(mapping)

    def test_network_access_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["network_allowed"] = True
        with pytest.raises(SchemaValidationError, match="network_allowed.*false"):
            DebugTask.from_mapping(mapping)

    def test_empty_allowed_write_paths_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["allowed_write_paths"] = []
        with pytest.raises(SchemaValidationError, match="non-empty"):
            DebugTask.from_mapping(mapping)

    def test_empty_title_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["title"] = ""
        with pytest.raises(SchemaValidationError, match="non-empty"):
            DebugTask.from_mapping(mapping)

    def test_unsupported_language_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["language"] = "javascript"
        with pytest.raises(SchemaValidationError, match="Unsupported language"):
            DebugTask.from_mapping(mapping)

    def test_patch_attempts_exceeds_max_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["max_patch_attempts"] = 5
        with pytest.raises(SchemaValidationError, match="range"):
            DebugTask.from_mapping(mapping)

    def test_pdb_observations_exceeds_max_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["max_pdb_observations"] = 25
        with pytest.raises(SchemaValidationError, match="range"):
            DebugTask.from_mapping(mapping)

    def test_external_services_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["external_services_allowed"] = True
        with pytest.raises(SchemaValidationError, match="external_services_allowed.*false"):
            DebugTask.from_mapping(mapping)

    def test_empty_description_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["description"] = ""
        with pytest.raises(SchemaValidationError, match="non-empty"):
            DebugTask.from_mapping(mapping)

    def test_non_dict_task_rejected(self):
        with pytest.raises(SchemaValidationError, match="mapping"):
            DebugTask.from_mapping("not a dict")

    def test_invalid_argv_type_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["reproduction"]["argv"] = [1, 2, 3]
        with pytest.raises(SchemaValidationError, match="string"):
            DebugTask.from_mapping(mapping)

    def test_p2p_too_few_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["tests"]["pass_to_pass"] = ["only_one::test"]
        with pytest.raises(SchemaValidationError, match="at least 2"):
            DebugTask.from_mapping(mapping)

    def test_unknown_reproduction_field_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["reproduction"]["extra"] = True
        with pytest.raises(SchemaValidationError, match="Unknown fields"):
            DebugTask.from_mapping(mapping)

    def test_unknown_constraints_field_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["extra"] = True
        with pytest.raises(SchemaValidationError, match="Unknown fields"):
            DebugTask.from_mapping(mapping)

    def test_tags_not_a_list_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["tags"] = "not-a-list"
        with pytest.raises(SchemaValidationError, match="must be a list"):
            DebugTask.from_mapping(mapping)

    def test_empty_fixture_path_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["fixture_path"] = ""
        with pytest.raises(SchemaValidationError, match="non-empty"):
            DebugTask.from_mapping(mapping)

    # --- Issue 1: path-safety regression tests ---

    def test_fixture_path_outside_curated_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["fixture_path"] = "agentic_debugger/datasets/other/task"
        with pytest.raises(SchemaValidationError, match="must be inside"):
            DebugTask.from_mapping(mapping)

    def test_fixture_path_leading_backslash_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["fixture_path"] = "\\etc\\passwd"
        with pytest.raises(SchemaValidationError, match="relative path"):
            DebugTask.from_mapping(mapping)

    def test_allowed_write_path_absolute_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["allowed_write_paths"] = ["/etc/passwd"]
        with pytest.raises(SchemaValidationError, match="relative path"):
            DebugTask.from_mapping(mapping)

    def test_allowed_write_path_traversal_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["allowed_write_paths"] = ["sub/../evil"]
        with pytest.raises(SchemaValidationError, match="path traversal"):
            DebugTask.from_mapping(mapping)

    def test_allowed_write_path_windows_absolute_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["allowed_write_paths"] = ["D:\\windows\\system32"]
        with pytest.raises(SchemaValidationError, match="relative path"):
            DebugTask.from_mapping(mapping)

    def test_denied_write_path_absolute_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["denied_write_paths"] = ["/bin/sh"]
        with pytest.raises(SchemaValidationError, match="relative path"):
            DebugTask.from_mapping(mapping)

    def test_denied_write_path_traversal_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["denied_write_paths"] = ["config/../../secret"]
        with pytest.raises(SchemaValidationError, match="path traversal"):
            DebugTask.from_mapping(mapping)

    def test_oracle_target_file_absolute_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["oracle"]["target_files"] = ["/usr/bin/malicious"]
        with pytest.raises(SchemaValidationError, match="relative path"):
            DebugTask.from_mapping(mapping)

    def test_oracle_target_file_traversal_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["oracle"]["target_files"] = ["src/../secret"]
        with pytest.raises(SchemaValidationError, match="path traversal"):
            DebugTask.from_mapping(mapping)

    def test_oracle_target_file_backslash_abs_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["oracle"]["target_files"] = ["\\global\\etc"]
        with pytest.raises(SchemaValidationError, match="relative path"):
            DebugTask.from_mapping(mapping)

    def test_fixture_path_backslash_normalization_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["fixture_path"] = "agentic_debugger\\datasets\\other\\task"
        with pytest.raises(SchemaValidationError, match="must be inside"):
            DebugTask.from_mapping(mapping)

    # --- Issue 2: curated test and argv contract tests ---

    def test_fail_to_pass_exactly_one_rejected_empty(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["tests"]["fail_to_pass"] = []
        with pytest.raises(SchemaValidationError, match="exactly 1"):
            DebugTask.from_mapping(mapping)

    def test_fail_to_pass_exactly_one_rejected_two(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["tests"]["fail_to_pass"] = [
            "tests/a::test_x",
            "tests/b::test_y",
        ]
        with pytest.raises(SchemaValidationError, match="exactly 1"):
            DebugTask.from_mapping(mapping)

    def test_full_suite_argv_empty_element_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["tests"]["full_suite_argv"] = ["python", ""]
        with pytest.raises(SchemaValidationError, match="non-empty"):
            DebugTask.from_mapping(mapping)

    # --- Issue 1 follow-up: mandatory denied paths ---

    def test_denied_empty_list_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["denied_write_paths"] = []
        with pytest.raises(SchemaValidationError, match="must include"):
            DebugTask.from_mapping(mapping)

    def test_denied_missing_tests_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["denied_write_paths"] = ["task.json"]
        with pytest.raises(SchemaValidationError, match="must include"):
            DebugTask.from_mapping(mapping)

    def test_denied_missing_task_json_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["denied_write_paths"] = ["tests"]
        with pytest.raises(SchemaValidationError, match="must include"):
            DebugTask.from_mapping(mapping)

    def test_denied_substring_lookalike_rejected(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["denied_write_paths"] = [
            "mytests", "task.json.backup"
        ]
        with pytest.raises(SchemaValidationError, match="must include"):
            DebugTask.from_mapping(mapping)

    def test_denied_additional_path_accepted(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["denied_write_paths"] = [
            "tests", "task.json", "secret.cfg"
        ]
        task = DebugTask.from_mapping(mapping)
        assert "secret.cfg" in task.constraints.denied_write_paths

    def test_denied_backslash_normalized_matches(self, valid_task_mapping):
        mapping = valid_task_mapping
        mapping["constraints"]["denied_write_paths"] = ["tests", "task.json"]
        task = DebugTask.from_mapping(mapping)
        assert "tests" in task.constraints.denied_write_paths
        assert "task.json" in task.constraints.denied_write_paths
