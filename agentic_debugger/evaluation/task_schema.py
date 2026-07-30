from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agentic_debugger import SchemaValidationError

TASK_SCHEMA_VERSION = "1.0"
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
FIXTURE_PATH_PREFIX = "agentic_debugger/datasets/curated/"


def _ensure_non_empty_str(v: Any, label: str) -> str:
    if not isinstance(v, str) or not v:
        raise SchemaValidationError(f"{label} must be a non-empty string")
    return v


def _ensure_list_of_non_empty_strs(v: Any, label: str) -> List[str]:
    if not isinstance(v, list):
        raise SchemaValidationError(f"{label} must be a list")
    result: List[str] = []
    for i, item in enumerate(v):
        if not isinstance(item, str) or not item:
            raise SchemaValidationError(
                f"{label}[{i}] must be a non-empty string"
            )
        result.append(item)
    return result


def _validate_int_range(v: Any, label: str, lo: int, hi: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise SchemaValidationError(f"{label} must be an integer")
    if v < lo or v > hi:
        raise SchemaValidationError(
            f"{label} must be in range [{lo}, {hi}], got {v}"
        )
    return v


def _validate_path_relative(path: str, label: str) -> str:
    if not isinstance(path, str) or not path:
        raise SchemaValidationError(f"{label} must be a non-empty string")
    if path.startswith("/") or path.startswith("\\"):
        raise SchemaValidationError(
            f"{label} must be a relative path, got absolute: {path!r}"
        )
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        raise SchemaValidationError(
            f"{label} must be a relative path, got absolute: {path!r}"
        )
    if os.path.isabs(path):
        raise SchemaValidationError(
            f"{label} must be a relative path, got absolute: {path!r}"
        )
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    if ".." in parts:
        raise SchemaValidationError(
            f"{label} must not contain path traversal: {path!r}"
        )
    return path


def _check_no_unknown_fields(m: Dict[str, Any], known: set, label: str) -> None:
    extra = set(m.keys()) - known
    if extra:
        raise SchemaValidationError(
            f"Unknown fields in {label}: {sorted(extra)}"
        )


def _check_required_fields(m: Dict[str, Any], required: set, label: str) -> None:
    missing = required - set(m.keys())
    if missing:
        raise SchemaValidationError(
            f"Missing required fields in {label}: {sorted(missing)}"
        )


@dataclass(frozen=True)
class Reproduction:
    argv: List[str]
    cwd: str
    timeout_seconds: int
    expected_exit_code: int

    @staticmethod
    def from_mapping(m: Any) -> Reproduction:
        if not isinstance(m, dict):
            raise SchemaValidationError("reproduction must be a mapping")
        _check_required_fields(
            m, {"argv", "cwd", "timeout_seconds", "expected_exit_code"}, "reproduction"
        )
        _check_no_unknown_fields(
            m, {"argv", "cwd", "timeout_seconds", "expected_exit_code"}, "reproduction"
        )

        argv = m["argv"]
        if not isinstance(argv, list):
            raise SchemaValidationError("reproduction.argv must be a list")
        if len(argv) == 0:
            raise SchemaValidationError("reproduction.argv must be non-empty")
        for i, arg in enumerate(argv):
            if not isinstance(arg, str):
                raise SchemaValidationError(
                    f"reproduction.argv[{i}] must be a string"
                )
            if not arg:
                raise SchemaValidationError(
                    f"reproduction.argv[{i}] must be non-empty"
                )

        cwd = _validate_path_relative(m["cwd"], "reproduction.cwd")
        timeout_seconds = _validate_int_range(
            m["timeout_seconds"], "reproduction.timeout_seconds", 1, 60
        )
        ec = m["expected_exit_code"]
        if not isinstance(ec, int) or isinstance(ec, bool):
            raise SchemaValidationError(
                "reproduction.expected_exit_code must be an integer"
            )

        return Reproduction(
            argv=argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            expected_exit_code=ec,
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "expected_exit_code": self.expected_exit_code,
        }


@dataclass(frozen=True)
class Tests:
    __test__ = False  # pytest: not a test class
    fail_to_pass: List[str]
    pass_to_pass: List[str]
    full_suite_argv: List[str]
    timeout_seconds: int

    @staticmethod
    def from_mapping(m: Any) -> Tests:
        if not isinstance(m, dict):
            raise SchemaValidationError("tests must be a mapping")
        _check_required_fields(
            m,
            {"fail_to_pass", "pass_to_pass", "full_suite_argv", "timeout_seconds"},
            "tests",
        )
        _check_no_unknown_fields(
            m,
            {"fail_to_pass", "pass_to_pass", "full_suite_argv", "timeout_seconds"},
            "tests",
        )

        fail_to_pass = _ensure_list_of_non_empty_strs(
            m["fail_to_pass"], "tests.fail_to_pass"
        )
        if len(fail_to_pass) != 1:
            raise SchemaValidationError(
                "tests.fail_to_pass must contain exactly 1 node ID "
                f"for schema v1, got {len(fail_to_pass)}"
            )
        if len(fail_to_pass) != len(set(fail_to_pass)):
            raise SchemaValidationError(
                "tests.fail_to_pass contains duplicate entries"
            )

        pass_to_pass = _ensure_list_of_non_empty_strs(
            m["pass_to_pass"], "tests.pass_to_pass"
        )
        if len(pass_to_pass) < 2:
            raise SchemaValidationError(
                "tests.pass_to_pass must have at least 2 entries"
            )
        if len(pass_to_pass) != len(set(pass_to_pass)):
            raise SchemaValidationError(
                "tests.pass_to_pass contains duplicate entries"
            )

        overlap = set(fail_to_pass) & set(pass_to_pass)
        if overlap:
            raise SchemaValidationError(
                f"tests.fail_to_pass and tests.pass_to_pass overlap: "
                f"{sorted(overlap)}"
            )

        full_suite_argv = _ensure_list_of_non_empty_strs(
            m["full_suite_argv"], "tests.full_suite_argv"
        )
        if len(full_suite_argv) == 0:
            raise SchemaValidationError(
                "tests.full_suite_argv must be a non-empty list"
            )

        timeout_seconds = _validate_int_range(
            m["timeout_seconds"], "tests.timeout_seconds", 1, 60
        )

        return Tests(
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
            full_suite_argv=full_suite_argv,
            timeout_seconds=timeout_seconds,
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "fail_to_pass": list(self.fail_to_pass),
            "pass_to_pass": list(self.pass_to_pass),
            "full_suite_argv": list(self.full_suite_argv),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class Constraints:
    allowed_write_paths: List[str]
    denied_write_paths: List[str]
    network_allowed: bool
    external_services_allowed: bool
    max_patch_attempts: int
    max_test_runs: int
    max_pdb_observations: int

    @staticmethod
    def from_mapping(m: Any) -> Constraints:
        if not isinstance(m, dict):
            raise SchemaValidationError("constraints must be a mapping")
        _check_required_fields(
            m,
            {
                "allowed_write_paths",
                "denied_write_paths",
                "network_allowed",
                "external_services_allowed",
                "max_patch_attempts",
                "max_test_runs",
                "max_pdb_observations",
            },
            "constraints",
        )
        _check_no_unknown_fields(
            m,
            {
                "allowed_write_paths",
                "denied_write_paths",
                "network_allowed",
                "external_services_allowed",
                "max_patch_attempts",
                "max_test_runs",
                "max_pdb_observations",
            },
            "constraints",
        )

        allowed_write_paths = _ensure_list_of_non_empty_strs(
            m["allowed_write_paths"], "constraints.allowed_write_paths"
        )
        if len(allowed_write_paths) == 0:
            raise SchemaValidationError(
                "constraints.allowed_write_paths must be non-empty"
            )
        allowed_write_paths = [
            _validate_path_relative(p, "constraints.allowed_write_paths")
            for p in allowed_write_paths
        ]

        denied_write_paths = _ensure_list_of_non_empty_strs(
            m["denied_write_paths"], "constraints.denied_write_paths"
        )
        denied_write_paths = [
            _validate_path_relative(p, "constraints.denied_write_paths")
            for p in denied_write_paths
        ]
        normalized_denied = {p.replace("\\", "/") for p in denied_write_paths}
        _MANDATORY_DENIED = {"tests", "task.json"}
        missing_mandatory = _MANDATORY_DENIED - normalized_denied
        if missing_mandatory:
            raise SchemaValidationError(
                "constraints.denied_write_paths must include "
                f"{sorted(_MANDATORY_DENIED)}, missing: {sorted(missing_mandatory)}"
            )

        network_allowed = m["network_allowed"]
        if not isinstance(network_allowed, bool):
            raise SchemaValidationError(
                "constraints.network_allowed must be a boolean"
            )
        if network_allowed:
            raise SchemaValidationError(
                "constraints.network_allowed must be false for curated v1 tasks"
            )

        external_services_allowed = m["external_services_allowed"]
        if not isinstance(external_services_allowed, bool):
            raise SchemaValidationError(
                "constraints.external_services_allowed must be a boolean"
            )
        if external_services_allowed:
            raise SchemaValidationError(
                "constraints.external_services_allowed must be false"
            )

        max_patch_attempts = _validate_int_range(
            m["max_patch_attempts"], "constraints.max_patch_attempts", 1, 3
        )
        max_test_runs = _validate_int_range(
            m["max_test_runs"], "constraints.max_test_runs", 1, 10
        )
        max_pdb_observations = _validate_int_range(
            m["max_pdb_observations"], "constraints.max_pdb_observations", 0, 20
        )

        return Constraints(
            allowed_write_paths=allowed_write_paths,
            denied_write_paths=denied_write_paths,
            network_allowed=network_allowed,
            external_services_allowed=external_services_allowed,
            max_patch_attempts=max_patch_attempts,
            max_test_runs=max_test_runs,
            max_pdb_observations=max_pdb_observations,
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "allowed_write_paths": list(self.allowed_write_paths),
            "denied_write_paths": list(self.denied_write_paths),
            "network_allowed": self.network_allowed,
            "external_services_allowed": self.external_services_allowed,
            "max_patch_attempts": self.max_patch_attempts,
            "max_test_runs": self.max_test_runs,
            "max_pdb_observations": self.max_pdb_observations,
        }


@dataclass(frozen=True)
class Oracle:
    bug_category: str
    target_files: List[str]
    target_symbols: List[str]
    root_cause_summary: str
    runtime_evidence_hint: str

    @staticmethod
    def from_mapping(m: Any) -> Oracle:
        if not isinstance(m, dict):
            raise SchemaValidationError("oracle must be a mapping")
        _check_required_fields(
            m,
            {
                "bug_category",
                "target_files",
                "target_symbols",
                "root_cause_summary",
                "runtime_evidence_hint",
            },
            "oracle",
        )
        _check_no_unknown_fields(
            m,
            {
                "bug_category",
                "target_files",
                "target_symbols",
                "root_cause_summary",
                "runtime_evidence_hint",
            },
            "oracle",
        )

        bug_category = _ensure_non_empty_str(
            m["bug_category"], "oracle.bug_category"
        )
        target_files = _ensure_list_of_non_empty_strs(
            m["target_files"], "oracle.target_files"
        )
        target_files = [
            _validate_path_relative(p, "oracle.target_files")
            for p in target_files
        ]
        target_symbols = _ensure_list_of_non_empty_strs(
            m["target_symbols"], "oracle.target_symbols"
        )
        root_cause_summary = _ensure_non_empty_str(
            m["root_cause_summary"], "oracle.root_cause_summary"
        )
        runtime_evidence_hint = _ensure_non_empty_str(
            m["runtime_evidence_hint"], "oracle.runtime_evidence_hint"
        )

        return Oracle(
            bug_category=bug_category,
            target_files=target_files,
            target_symbols=target_symbols,
            root_cause_summary=root_cause_summary,
            runtime_evidence_hint=runtime_evidence_hint,
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "bug_category": self.bug_category,
            "target_files": list(self.target_files),
            "target_symbols": list(self.target_symbols),
            "root_cause_summary": self.root_cause_summary,
            "runtime_evidence_hint": self.runtime_evidence_hint,
        }


@dataclass(frozen=True)
class TaskSource:
    """Typed source binding for curated and external task roots."""

    kind: str
    path: str
    provenance: Dict[str, str]

    @staticmethod
    def from_mapping(m: Any) -> "TaskSource":
        if not isinstance(m, dict):
            raise SchemaValidationError("source must be a mapping")
        _check_required_fields(m, {"kind", "path", "provenance"}, "source")
        _check_no_unknown_fields(m, {"kind", "path", "provenance"}, "source")
        kind = m["kind"]
        if kind not in {"curated", "external"}:
            raise SchemaValidationError("source.kind must be curated or external")
        path = _validate_path_relative(m["path"], "source.path")
        provenance = m["provenance"]
        required = {
            "dataset", "manifest_id", "manifest_fingerprint", "upstream_repository",
            "upstream_revision", "project", "bug_id", "buggy_revision", "fixed_revision",
        }
        if not isinstance(provenance, dict):
            raise SchemaValidationError("source.provenance must be a mapping")
        _check_required_fields(provenance, required, "source.provenance")
        _check_no_unknown_fields(provenance, required, "source.provenance")
        if not all(isinstance(value, str) and value for value in provenance.values()):
            raise SchemaValidationError("source.provenance values must be non-empty strings")
        normalized = path.replace("\\", "/")
        if kind == "curated" and not normalized.startswith(FIXTURE_PATH_PREFIX):
            raise SchemaValidationError("curated source.path must be inside the curated fixture root")
        if kind == "external" and normalized.startswith(FIXTURE_PATH_PREFIX):
            raise SchemaValidationError("external source.path must not use the curated fixture root")
        return TaskSource(kind, path, dict(provenance))

    def to_mapping(self) -> Dict[str, Any]:
        return {"kind": self.kind, "path": self.path, "provenance": dict(self.provenance)}

    def agent_visible_mapping(self) -> Dict[str, Any]:
        provenance = dict(self.provenance)
        provenance.pop("fixed_revision", None)
        return {"kind": self.kind, "path": self.path, "provenance": provenance}


@dataclass(frozen=True)
class DebugTask:
    schema_version: str
    task_id: str
    title: str
    description: str
    language: str
    fixture_path: str
    reproduction: Reproduction
    tests: Tests
    constraints: Constraints
    oracle: Oracle
    tags: List[str]
    source: Optional[TaskSource] = None

    @staticmethod
    def from_mapping(m: Any) -> DebugTask:
        if not isinstance(m, dict):
            raise SchemaValidationError("task must be a mapping")
        _check_required_fields(
            m,
            {
                "schema_version",
                "task_id",
                "title",
                "description",
                "language",
                "fixture_path",
                "reproduction",
                "tests",
                "constraints",
                "oracle",
                "tags",
            },
            "task",
        )
        _check_no_unknown_fields(
            m,
            {
                "schema_version",
                "task_id",
                "title",
                "description",
                "language",
                "fixture_path",
                "reproduction",
                "tests",
                "constraints",
                "oracle",
                "tags",
                "source",
            },
            "task",
        )

        schema_version = m["schema_version"]
        if not isinstance(schema_version, str) or schema_version != TASK_SCHEMA_VERSION:
            raise SchemaValidationError(
                f"Unsupported schema version: {schema_version!r}"
            )

        task_id = _ensure_non_empty_str(m["task_id"], "task.task_id")
        if not TASK_ID_PATTERN.match(task_id):
            raise SchemaValidationError(f"Invalid task_id: {task_id!r}")

        title = _ensure_non_empty_str(m["title"], "task.title")
        description = _ensure_non_empty_str(m["description"], "task.description")
        language = m["language"]
        if language != "python":
            raise SchemaValidationError(f"Unsupported language: {language!r}")
        fixture_path = _validate_path_relative(
            m["fixture_path"], "task.fixture_path"
        )
        normalized_fixture = fixture_path.replace("\\", "/")
        source = TaskSource.from_mapping(m["source"]) if "source" in m else None
        if source is None and not normalized_fixture.startswith(FIXTURE_PATH_PREFIX):
            raise SchemaValidationError(
                f"task.fixture_path must be inside "
                f"{FIXTURE_PATH_PREFIX!r}, got {fixture_path!r}"
            )
        if source is not None and source.path != fixture_path:
            raise SchemaValidationError("task.fixture_path must equal source.path")

        reproduction = Reproduction.from_mapping(m["reproduction"])
        tests = Tests.from_mapping(m["tests"])
        constraints = Constraints.from_mapping(m["constraints"])
        oracle = Oracle.from_mapping(m["oracle"])

        tags = m["tags"]
        if not isinstance(tags, list):
            raise SchemaValidationError("task.tags must be a list")
        clean_tags: List[str] = []
        for i, tag in enumerate(tags):
            if not isinstance(tag, str):
                raise SchemaValidationError(f"task.tags[{i}] must be a string")
            clean_tags.append(tag)

        return DebugTask(
            schema_version=schema_version,
            task_id=task_id,
            title=title,
            description=description,
            language=language,
            fixture_path=fixture_path,
            reproduction=reproduction,
            tests=tests,
            constraints=constraints,
            oracle=oracle,
            tags=clean_tags,
            source=source,
        )

    @staticmethod
    def from_file(path: str) -> DebugTask:
        if not isinstance(path, str) or not path:
            raise SchemaValidationError("path must be a non-empty string")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return DebugTask.from_mapping(data)

    def to_mapping(self) -> Dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "language": self.language,
            "fixture_path": self.fixture_path,
            "reproduction": self.reproduction.to_mapping(),
            "tests": self.tests.to_mapping(),
            "constraints": self.constraints.to_mapping(),
            "oracle": self.oracle.to_mapping(),
            "tags": list(self.tags),
        }
        if self.source is not None:
            result["source"] = self.source.to_mapping()
        return result

    def agent_visible_mapping(self) -> Dict[str, Any]:
        result = self.to_mapping()
        del result["oracle"]
        if self.source is not None:
            result["source"] = self.source.agent_visible_mapping()
        return result
