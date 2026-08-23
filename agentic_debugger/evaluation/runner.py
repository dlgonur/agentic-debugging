"""Typed, portable records for trusted-local Task 7 evaluation.

Task 7 v1 is a trusted local benchmark evaluator.  Its disposable workspace,
patch-path checks, cwd handling, and bounded commands protect repository
integrity; they are not an OS-level hostile-code security sandbox.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

from agentic_debugger import AgenticDebuggerError
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome, classify_outcome
from agentic_debugger.evaluation.task_schema import DebugTask

MAX_PATCH_CHARS = 100_000
MAX_RETAINED_OUTPUT_CHARS = 20_000
MAX_ERROR_CHARS = 4_000
WORKSPACE_MARKER = "<WORKSPACE>"
OUTSIDE_WORKSPACE_MARKER = "<OUTSIDE_WORKSPACE>"


class EvaluationError(AgenticDebuggerError):
    """Base class for evaluator API errors."""


class EvaluationInputError(EvaluationError):
    """Raised for direct API misuse or invalid task input."""


class EvaluationInvariantError(EvaluationError):
    """Raised for evaluator construction/invariant misuse."""


class ExecutionBoundary(str, Enum):
    TRUSTED_LOCAL_WORKSPACE = "TRUSTED_LOCAL_WORKSPACE"


class EvaluationStatus(str, Enum):
    COMPLETED = "COMPLETED"
    BASELINE_INVALID = "BASELINE_INVALID"
    PATCH_APPLY_FAILED = "PATCH_APPLY_FAILED"
    SYNTAX_FAILED = "SYNTAX_FAILED"
    TEST_EXECUTION_FAILED = "TEST_EXECUTION_FAILED"
    TEST_TIMEOUT = "TEST_TIMEOUT"
    FULL_SUITE_CONTRADICTION = "FULL_SUITE_CONTRADICTION"
    WORKSPACE_PREPARATION_FAILED = "WORKSPACE_PREPARATION_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    CANONICAL_FIXTURE_CHANGED = "CANONICAL_FIXTURE_CHANGED"
    EVALUATOR_INVARIANT_FAILED = "EVALUATOR_INVARIANT_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class LifecycleStatus(str, Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    PREPARED = "PREPARED"
    CLEANED = "CLEANED"
    CLEANUP_FAILED = "CLEANUP_FAILED"


class TestRecordStatus(str, Enum):
    __test__ = False
    PASS = "PASS"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


@dataclass(frozen=True)
class PytestCounts:
    collected: Optional[int] = None
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0

    def __post_init__(self) -> None:
        for name in ("collected", "passed", "failed", "errors", "skipped", "xfailed", "xpassed"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise EvaluationInputError(f"pytest count {name} must be a non-negative integer")

    @property
    def executed(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped + self.xfailed + self.xpassed

    def to_mapping(self) -> dict[str, Any]:
        return {"collected": self.collected, "passed": self.passed, "failed": self.failed, "errors": self.errors, "skipped": self.skipped, "xfailed": self.xfailed, "xpassed": self.xpassed}


@dataclass(frozen=True)
class TestRecord:
    __test__ = False
    node_id: str
    kind: str
    argv: Tuple[str, ...]
    resolved_cwd: str
    timeout_seconds: float
    exit_code: Optional[int]
    status: TestRecordStatus
    timed_out: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    counts: PytestCounts = PytestCounts()
    parse_error: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", _tuple_strings(self.argv, "argv"))
        if not isinstance(self.status, TestRecordStatus):
            raise EvaluationInputError("status must be TestRecordStatus")
        if not isinstance(self.counts, PytestCounts):
            raise EvaluationInputError("counts must be PytestCounts")
        object.__setattr__(self, "stdout", _bounded_text(self.stdout))
        object.__setattr__(self, "stderr", _bounded_text(self.stderr))
        object.__setattr__(self, "resolved_cwd", _portable_path(self.resolved_cwd))
        if not isinstance(self.node_id, str) or not self.node_id:
            raise EvaluationInputError("node_id must be non-empty")
        if not isinstance(self.kind, str) or not self.kind:
            raise EvaluationInputError("kind must be non-empty")
        if type(self.timeout_seconds) not in (int, float) or isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise EvaluationInputError("timeout_seconds must be positive number")
        if self.exit_code is not None and (type(self.exit_code) is not int):
            raise EvaluationInputError("exit_code must be integer or None")
        if type(self.timed_out) is not bool:
            raise EvaluationInputError("timed_out must be boolean")
        if type(self.stdout_truncated) is not bool or type(self.stderr_truncated) is not bool:
            raise EvaluationInputError("output truncation flags must be boolean")
        if self.parse_error is not None and type(self.parse_error) is not str:
            raise EvaluationInputError("parse_error must be string or None")
        object.__setattr__(self, "parse_error", _optional_bounded(self.parse_error))

    @property
    def passed(self) -> bool:
        return self.status is TestRecordStatus.PASS

    def to_mapping(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "kind": self.kind, "argv": list(self.argv), "resolved_cwd": self.resolved_cwd, "timeout_seconds": self.timeout_seconds, "exit_code": self.exit_code, "status": self.status.value, "timed_out": self.timed_out, "stdout": self.stdout, "stderr": self.stderr, "stdout_truncated": self.stdout_truncated, "stderr_truncated": self.stderr_truncated, "counts": self.counts.to_mapping(), "parse_error": self.parse_error}


@dataclass(frozen=True)
class ReproductionRecord:
    argv: Tuple[str, ...]
    resolved_cwd: str
    timeout_seconds: float
    exit_code: Optional[int]
    status: TestRecordStatus
    expected_exit_code: int
    expected_failure: bool
    reproduction_match: Optional[bool]
    timed_out: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    counts: PytestCounts = PytestCounts()
    parse_error: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", _tuple_strings(self.argv, "argv"))
        if not isinstance(self.status, TestRecordStatus) or not isinstance(self.counts, PytestCounts):
            raise EvaluationInputError("reproduction typed fields are malformed")
        if type(self.timeout_seconds) not in (int, float) or isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise EvaluationInputError("timeout_seconds must be positive number")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise EvaluationInputError("exit_code must be integer or None")
        if type(self.expected_exit_code) is not int or type(self.expected_failure) is not bool or type(self.timed_out) is not bool:
            raise EvaluationInputError("reproduction scalar fields are malformed")
        if self.reproduction_match is not None and type(self.reproduction_match) is not bool:
            raise EvaluationInputError("reproduction_match must be bool or None")
        if not isinstance(self.status, TestRecordStatus):
            raise EvaluationInputError("status must be TestRecordStatus")
        if not isinstance(self.counts, PytestCounts):
            raise EvaluationInputError("counts must be PytestCounts")
        object.__setattr__(self, "resolved_cwd", _portable_path(self.resolved_cwd))
        object.__setattr__(self, "stdout", _bounded_text(self.stdout))
        object.__setattr__(self, "stderr", _bounded_text(self.stderr))
        if type(self.stdout_truncated) is not bool or type(self.stderr_truncated) is not bool:
            raise EvaluationInputError("output truncation flags must be boolean")
        if self.parse_error is not None and type(self.parse_error) is not str:
            raise EvaluationInputError("parse_error must be string or None")
        object.__setattr__(self, "parse_error", _optional_bounded(self.parse_error))

    def to_mapping(self) -> dict[str, Any]:
        return {"argv": list(self.argv), "resolved_cwd": self.resolved_cwd, "timeout_seconds": self.timeout_seconds, "exit_code": self.exit_code, "status": self.status.value, "expected_exit_code": self.expected_exit_code, "expected_failure": self.expected_failure, "reproduction_match": self.reproduction_match, "timed_out": self.timed_out, "stdout": self.stdout, "stderr": self.stderr, "stdout_truncated": self.stdout_truncated, "stderr_truncated": self.stderr_truncated, "counts": self.counts.to_mapping(), "parse_error": self.parse_error}


@dataclass(frozen=True)
class PatchRecord:
    attempted: bool
    success: bool
    no_op: bool
    changed_files: Tuple[str, ...]
    hunk_count: int
    error: Optional[str]

    def __post_init__(self) -> None:
        if type(self.attempted) is not bool or type(self.success) is not bool or type(self.no_op) is not bool:
            raise EvaluationInputError("patch flags must be boolean")
        if type(self.hunk_count) is not int or self.hunk_count < 0:
            raise EvaluationInputError("hunk_count must be non-negative integer")
        object.__setattr__(self, "changed_files", _tuple_strings(self.changed_files, "changed_files"))
        object.__setattr__(self, "error", _optional_bounded(self.error))

    def to_mapping(self) -> dict[str, Any]:
        return {"attempted": self.attempted, "success": self.success, "no_op": self.no_op, "changed_files": list(self.changed_files), "hunk_count": self.hunk_count, "error": self.error}


@dataclass(frozen=True)
class SyntaxFileRecord:
    path: str
    success: bool
    error_type: Optional[str]
    message: Optional[str]
    line: Optional[int]
    column: Optional[int]

    def __post_init__(self) -> None:
        if type(self.path) is not str or not self.path or type(self.success) is not bool:
            raise EvaluationInputError("syntax file record scalars are malformed")
        if self.error_type is not None and type(self.error_type) is not str:
            raise EvaluationInputError("syntax error_type must be string or None")
        if self.message is not None and type(self.message) is not str:
            raise EvaluationInputError("syntax message must be string or None")
        if self.line is not None and type(self.line) is not int:
            raise EvaluationInputError("syntax line must be integer or None")
        if self.column is not None and type(self.column) is not int:
            raise EvaluationInputError("syntax column must be integer or None")
        object.__setattr__(self, "message", _optional_bounded(self.message))

    def to_mapping(self) -> dict[str, Any]:
        return {"path": self.path, "success": self.success, "error_type": self.error_type, "message": self.message, "line": self.line, "column": self.column}


@dataclass(frozen=True)
class SyntaxRecord:
    checked_files: Tuple[str, ...]
    passed: bool
    results: Tuple[SyntaxFileRecord, ...]
    error: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "checked_files", _tuple_strings(self.checked_files, "checked_files"))
        if not isinstance(self.results, (list, tuple)):
            raise EvaluationInputError("results must be a list or tuple")
        object.__setattr__(self, "results", tuple(self.results))
        if type(self.passed) is not bool:
            raise EvaluationInputError("syntax passed must be boolean")
        if not all(isinstance(item, SyntaxFileRecord) for item in self.results):
            raise EvaluationInputError("results must contain SyntaxFileRecord values")
        object.__setattr__(self, "error", _optional_bounded(self.error))

    def to_mapping(self) -> dict[str, Any]:
        return {"checked_files": list(self.checked_files), "passed": self.passed, "results": [item.to_mapping() for item in self.results], "error": self.error}


@dataclass(frozen=True)
class CollectionRecord:
    declared_nodes: Tuple[str, ...]
    collected_nodes: Tuple[str, ...]
    passed: bool
    error: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "declared_nodes", _tuple_strings(self.declared_nodes, "declared_nodes"))
        object.__setattr__(self, "collected_nodes", _tuple_strings(self.collected_nodes, "collected_nodes"))
        if type(self.passed) is not bool:
            raise EvaluationInputError("collection passed must be boolean")
        object.__setattr__(self, "error", _optional_bounded(self.error))

    def to_mapping(self) -> dict[str, Any]:
        return {"declared_nodes": list(self.declared_nodes), "collected_nodes": list(self.collected_nodes), "passed": self.passed, "error": self.error}


@dataclass(frozen=True)
class WorkspaceRecord:
    lifecycle: LifecycleStatus
    prepared: bool
    cleanup_attempted: bool
    cleaned: bool
    canonical_fixture_unchanged: bool
    canonical_hash_before: Optional[str]
    canonical_hash_after: Optional[str]
    error: Optional[str]

    def __post_init__(self) -> None:
        if not isinstance(self.lifecycle, LifecycleStatus):
            raise EvaluationInputError("lifecycle must be LifecycleStatus")
        if not all(type(value) is bool for value in (self.prepared, self.cleanup_attempted, self.cleaned, self.canonical_fixture_unchanged)):
            raise EvaluationInputError("workspace lifecycle flags must be boolean")
        for name in ("canonical_hash_before", "canonical_hash_after", "error"):
            value = getattr(self, name)
            if value is not None and type(value) is not str:
                raise EvaluationInputError(f"{name} must be string or None")
            object.__setattr__(self, name, _optional_bounded(value))

    def to_mapping(self) -> dict[str, Any]:
        return {"lifecycle": self.lifecycle.value, "prepared": self.prepared, "cleanup_attempted": self.cleanup_attempted, "cleaned": self.cleaned, "canonical_fixture_unchanged": self.canonical_fixture_unchanged, "canonical_hash_before": self.canonical_hash_before, "canonical_hash_after": self.canonical_hash_after, "error": self.error}


@dataclass(frozen=True)
class BaselineRecord:
    valid: bool
    reason: Optional[str]
    collection: Optional[CollectionRecord]
    reproduction: Optional[ReproductionRecord]
    f2p: Tuple[TestRecord, ...]
    p2p: Tuple[TestRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.f2p, (list, tuple)) or not isinstance(self.p2p, (list, tuple)):
            raise EvaluationInputError("baseline test records must be lists or tuples")
        object.__setattr__(self, "f2p", tuple(self.f2p))
        object.__setattr__(self, "p2p", tuple(self.p2p))
        if type(self.valid) is not bool:
            raise EvaluationInputError("baseline valid must be boolean")
        if self.collection is not None and not isinstance(self.collection, CollectionRecord):
            raise EvaluationInputError("collection must be CollectionRecord")
        if self.reproduction is not None and not isinstance(self.reproduction, ReproductionRecord):
            raise EvaluationInputError("reproduction must be ReproductionRecord")
        if not all(isinstance(item, TestRecord) for item in self.f2p + self.p2p):
            raise EvaluationInputError("baseline records must be TestRecord values")
        object.__setattr__(self, "reason", _optional_bounded(self.reason))

    def to_mapping(self) -> dict[str, Any]:
        return {"valid": self.valid, "reason": self.reason, "collection": self.collection.to_mapping() if self.collection else None, "reproduction": self.reproduction.to_mapping() if self.reproduction else None, "f2p": [record.to_mapping() for record in self.f2p], "p2p": [record.to_mapping() for record in self.p2p]}


@dataclass(frozen=True)
class EvaluationResult:
    task_id: str
    boundary: ExecutionBoundary
    status: EvaluationStatus
    stop_reason: str
    outcome: Optional[SemanticOutcome]
    workspace: WorkspaceRecord
    baseline: BaselineRecord
    patch_application: PatchRecord
    syntax: SyntaxRecord
    post_patch_reproduction: Optional[ReproductionRecord]
    post_patch_f2p: Tuple[TestRecord, ...]
    post_patch_p2p: Tuple[TestRecord, ...]
    full_suite: Optional[TestRecord]
    f2p_total: int
    f2p_passed: int
    p2p_total: int
    p2p_passed: int
    candidate_patch_attempt_count: int
    task_max_test_runs: int
    verification_command_count: int
    verification_selected_test_count: int
    timeout: bool
    diagnostic: Optional[str] = None
    private_checks_passed: Optional[bool] = None

    def __post_init__(self) -> None:
        if not isinstance(self.post_patch_f2p, (list, tuple)) or not isinstance(self.post_patch_p2p, (list, tuple)):
            raise EvaluationInputError("post-patch test records must be lists or tuples")
        object.__setattr__(self, "post_patch_f2p", tuple(self.post_patch_f2p))
        object.__setattr__(self, "post_patch_p2p", tuple(self.post_patch_p2p))
        if type(self.task_id) is not str or not self.task_id or type(self.stop_reason) is not str:
            raise EvaluationInputError("task_id and stop_reason must be strings")
        if type(self.task_max_test_runs) is not int or self.task_max_test_runs < 0:
            raise EvaluationInputError("task_max_test_runs must be non-negative integer")
        if type(self.verification_command_count) is not int or self.verification_command_count < 0:
            raise EvaluationInputError("verification_command_count must be non-negative integer")
        if type(self.verification_selected_test_count) is not int or self.verification_selected_test_count < 0:
            raise EvaluationInputError("verification_selected_test_count must be non-negative integer")
        if type(self.candidate_patch_attempt_count) is not int or self.candidate_patch_attempt_count not in (0, 1):
            raise EvaluationInputError("candidate_patch_attempt_count must be 0 or 1")
        if type(self.timeout) is not bool:
            raise EvaluationInputError("timeout must be boolean")
        if self.private_checks_passed is not None and type(self.private_checks_passed) is not bool:
            raise EvaluationInputError("private_checks_passed must be boolean or null")
        if not isinstance(self.boundary, ExecutionBoundary) or not isinstance(self.status, EvaluationStatus):
            raise EvaluationInputError("boundary and status must be typed enums")
        if self.outcome is not None and not isinstance(self.outcome, SemanticOutcome):
            raise EvaluationInputError("outcome must be SemanticOutcome or None")
        if not isinstance(self.workspace, WorkspaceRecord) or not isinstance(self.baseline, BaselineRecord):
            raise EvaluationInputError("workspace and baseline records are malformed")
        if not isinstance(self.patch_application, PatchRecord) or not isinstance(self.syntax, SyntaxRecord):
            raise EvaluationInputError("patch and syntax records are malformed")
        if self.post_patch_reproduction is not None and not isinstance(self.post_patch_reproduction, ReproductionRecord):
            raise EvaluationInputError("post reproduction is malformed")
        if not all(isinstance(item, TestRecord) for item in self.post_patch_f2p + self.post_patch_p2p):
            raise EvaluationInputError("post records must be TestRecord values")
        if self.full_suite is not None and not isinstance(self.full_suite, TestRecord):
            raise EvaluationInputError("full suite must be TestRecord")
        for name in ("f2p_total", "f2p_passed", "p2p_total", "p2p_passed"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise EvaluationInputError(f"{name} must be a non-negative integer")
        if self.f2p_total != len(self.post_patch_f2p) or self.p2p_total != len(self.post_patch_p2p):
            raise EvaluationInputError("result totals do not match retained records")
        if self.f2p_passed != sum(item.passed for item in self.post_patch_f2p) or self.p2p_passed != sum(item.passed for item in self.post_patch_p2p):
            raise EvaluationInputError("result pass counts do not match retained records")
        if self.status is EvaluationStatus.COMPLETED and (self.outcome is None or self.full_suite is None or self.post_patch_reproduction is None):
            raise EvaluationInputError("completed result lacks complete post-patch evidence")
        if self.status is EvaluationStatus.COMPLETED and any(item.status in (TestRecordStatus.ERROR, TestRecordStatus.TIMEOUT) for item in self.post_patch_f2p + self.post_patch_p2p):
            raise EvaluationInputError("completed result contains an invalid individual test status")
        if self.status is not EvaluationStatus.COMPLETED and self.outcome is not None:
            raise EvaluationInputError("non-completed result cannot have semantic outcome")
        if self.status is EvaluationStatus.COMPLETED:
            _validate_completed_result(self)
        object.__setattr__(self, "diagnostic", _optional_bounded(self.diagnostic))

    def to_mapping(self) -> dict[str, Any]:
        result = {"task_id": self.task_id, "boundary": self.boundary.value, "status": self.status.value, "stop_reason": self.stop_reason, "outcome": self.outcome.value if self.outcome else None, "workspace": self.workspace.to_mapping(), "baseline": self.baseline.to_mapping(), "patch_application": self.patch_application.to_mapping(), "syntax": self.syntax.to_mapping(), "post_patch_reproduction": self.post_patch_reproduction.to_mapping() if self.post_patch_reproduction else None, "post_patch_f2p": [record.to_mapping() for record in self.post_patch_f2p], "post_patch_p2p": [record.to_mapping() for record in self.post_patch_p2p], "full_suite": self.full_suite.to_mapping() if self.full_suite else None, "f2p_total": self.f2p_total, "f2p_passed": self.f2p_passed, "p2p_total": self.p2p_total, "p2p_passed": self.p2p_passed, "candidate_patch_attempt_count": self.candidate_patch_attempt_count, "task_max_test_runs": self.task_max_test_runs, "verification_command_count": self.verification_command_count, "verification_selected_test_count": self.verification_selected_test_count, "timeout": self.timeout, "diagnostic": self.diagnostic}
        if self.private_checks_passed is not None:
            result["private_checks_passed"] = self.private_checks_passed
        return result

    def semantic_mapping(self) -> dict[str, Any]:
        mapping = self.to_mapping()
        mapping.pop("diagnostic", None)
        return mapping

    def to_json(self) -> str:
        return json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"))


def _validate_completed_result(result: EvaluationResult) -> None:
    """Reject semantically impossible completed result records."""
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise EvaluationInputError(message)

    require(result.stop_reason == "completed", "completed result must have stop_reason completed")
    require(result.outcome is not None, "completed result must have an outcome")
    require(result.timeout is False, "completed result cannot be timed out")
    require(result.candidate_patch_attempt_count == 1, "completed result must attempt the candidate patch")
    if result.private_checks_passed is not None:
        require(result.private_checks_passed is True, "completed proof result must pass private checks")

    workspace = result.workspace
    require(workspace.lifecycle is LifecycleStatus.CLEANED, "completed workspace must be CLEANED")
    require(workspace.prepared is True, "completed workspace must be prepared")
    require(workspace.cleanup_attempted is True, "completed workspace cleanup must be attempted")
    require(workspace.cleaned is True, "completed workspace must be cleaned")
    require(workspace.canonical_fixture_unchanged is True, "completed result must preserve the canonical fixture")
    require(workspace.error is None, "completed workspace cannot contain an error")

    baseline = result.baseline
    require(baseline.valid is True, "completed baseline must be valid")
    require(baseline.reason is None, "completed baseline cannot have a reason")
    require(baseline.collection is not None, "completed baseline lacks collection evidence")
    require(baseline.collection.passed is True, "completed baseline collection must pass")
    require(baseline.collection.error is None, "completed baseline collection cannot have an error")
    require(baseline.reproduction is not None, "completed baseline lacks reproduction evidence")
    reproduction = baseline.reproduction
    require(reproduction.status is TestRecordStatus.FAIL, "baseline reproduction must be a genuine failure")
    require(reproduction.timed_out is False, "baseline reproduction cannot time out")
    require(reproduction.reproduction_match is True, "baseline reproduction must match the expected failure")
    require(reproduction.expected_failure is True, "baseline reproduction must expect failure")
    require(reproduction.parse_error is None, "baseline reproduction cannot have a parse error")
    require(bool(baseline.f2p) and bool(baseline.p2p), "completed baseline lacks individual test evidence")
    for record in baseline.f2p:
        require(record.status is TestRecordStatus.FAIL, "every baseline F2P test must fail")
        require(record.timed_out is False and record.parse_error is None, "baseline F2P evidence is invalid")
    for record in baseline.p2p:
        require(record.status is TestRecordStatus.PASS, "every baseline P2P test must pass")
        require(record.timed_out is False and record.parse_error is None, "baseline P2P evidence is invalid")

    patch = result.patch_application
    require(patch.attempted is True, "completed result must record patch application")
    require(patch.success is True, "completed patch application must succeed")
    require(patch.error is None, "completed patch application cannot have an error")
    require(result.syntax.passed is True, "completed syntax validation must pass")
    require(result.syntax.error is None, "completed syntax validation cannot have an error")
    require(all(item.success for item in result.syntax.results), "every completed syntax file must pass")

    post_reproduction = result.post_patch_reproduction
    require(post_reproduction is not None, "completed result lacks post-patch reproduction")
    require(post_reproduction.status in (TestRecordStatus.PASS, TestRecordStatus.FAIL), "post-patch reproduction has an invalid status")
    require(post_reproduction.timed_out is False and post_reproduction.parse_error is None, "post-patch reproduction evidence is invalid")
    for record in result.post_patch_f2p + result.post_patch_p2p:
        require(record.status in (TestRecordStatus.PASS, TestRecordStatus.FAIL), "completed individual evidence has an invalid status")
        require(record.timed_out is False and record.parse_error is None, "completed individual evidence is invalid")

    full_suite = result.full_suite
    require(full_suite is not None, "completed result lacks full-suite evidence")
    require(full_suite.status in (TestRecordStatus.PASS, TestRecordStatus.FAIL), "full-suite evidence has an invalid status")
    require(full_suite.timed_out is False and full_suite.parse_error is None, "full-suite evidence is invalid")
    counts = full_suite.counts
    require(counts.errors == 0 and counts.skipped == 0 and counts.xfailed == 0 and counts.xpassed == 0, "full-suite evidence contains infrastructure outcomes")
    all_post = result.post_patch_f2p + result.post_patch_p2p
    require(counts.executed == len(all_post), "full-suite executed count disagrees with individual evidence")
    require(counts.passed == sum(item.status is TestRecordStatus.PASS for item in all_post), "full-suite passed count disagrees with individual evidence")
    require(counts.failed == sum(item.status is TestRecordStatus.FAIL for item in all_post), "full-suite failed count disagrees with individual evidence")
    if counts.failed:
        require(full_suite.status is TestRecordStatus.FAIL, "full-suite failure counts require FAIL status")
        require(full_suite.exit_code is not None and full_suite.exit_code != 0, "full-suite failure counts require a non-zero exit code")
    else:
        require(full_suite.status is TestRecordStatus.PASS, "a passing full-suite count requires PASS status")
        require(full_suite.exit_code == 0, "a passing full-suite count requires exit code zero")
    expected = classify_outcome(
        [item.status is TestRecordStatus.PASS for item in result.post_patch_f2p],
        [item.status is TestRecordStatus.PASS for item in result.post_patch_p2p],
    )
    require(result.outcome is expected, "completed outcome disagrees with individual evidence")


def load_task(task: Union[DebugTask, Mapping[str, Any], str]) -> DebugTask:
    """Load every task input through the complete authoritative schema path."""
    try:
        if isinstance(task, DebugTask):
            # Round-trip direct objects so dataclass mutations cannot bypass
            # DebugTask schema validation or retain caller-owned list aliases.
            return DebugTask.from_mapping(task.to_mapping())
        if isinstance(task, str):
            return DebugTask.from_file(task)
        if isinstance(task, Mapping):
            return DebugTask.from_mapping(dict(task))
        raise EvaluationInputError("task must be a DebugTask, mapping, or manifest path")
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except EvaluationInputError:
        raise
    except Exception as exc:
        raise EvaluationInputError(bounded_error(exc)) from exc


def command_result_mapping(result: Any) -> dict[str, Any]:
    return {"argv": list(result.argv), "cwd": _portable_path(result.cwd), "exit_code": result.exit_code, "timed_out": result.timed_out, "stdout": _bounded_text(result.stdout), "stderr": _bounded_text(result.stderr), "stdout_truncated": result.stdout_truncated, "stderr_truncated": result.stderr_truncated}


def _tuple_strings(value: Sequence[str], label: str) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise EvaluationInputError(f"{label} must contain strings")
    return tuple(value)


def _bounded_text(value: str) -> str:
    if not isinstance(value, str):
        raise EvaluationInputError("output must be a string")
    if len(value) <= MAX_RETAINED_OUTPUT_CHARS:
        return value
    marker = "\n... [output truncated] ...\n"
    remaining = MAX_RETAINED_OUTPUT_CHARS - len(marker)
    head = remaining // 2
    return value[:head] + marker + value[-(remaining - head):]


def normalize_output(value: str, workspace_root: Optional[str]) -> str:
    text = _bounded_text(value)
    if workspace_root:
        variants = {workspace_root, workspace_root.replace("\\", "/"), workspace_root.replace("/", "\\")}
        for variant in sorted(variants, key=len, reverse=True):
            text = text.replace(variant, WORKSPACE_MARKER)
    text = re.sub(r"task_workspace_[A-Za-z0-9_-]+", WORKSPACE_MARKER, text)
    text = re.sub(r"\bin[ \t]+\d+(?:\.\d+)?s\b", "in <DURATION>s", text, flags=re.IGNORECASE)
    return text


def _portable_path(value: str) -> str:
    if not isinstance(value, str):
        raise EvaluationInputError("path must be a string")
    normalized = value.replace("\\", "/")
    if normalized in ("", "."):
        return "."
    if normalized == WORKSPACE_MARKER or normalized.startswith(WORKSPACE_MARKER + "/"):
        return normalized
    if normalized == OUTSIDE_WORKSPACE_MARKER or normalized.startswith(OUTSIDE_WORKSPACE_MARKER + "/"):
        return normalized
    if ":/" in normalized or normalized.startswith("/"):
        return OUTSIDE_WORKSPACE_MARKER
    return normalized.strip("/")


def _optional_bounded(value: Optional[str]) -> Optional[str]:
    return None if value is None else _bounded_text(value)[:MAX_ERROR_CHARS]


def bounded_error(exc: BaseException) -> str:
    return _optional_bounded(f"{type(exc).__name__}: {exc}") or "internal evaluator error"
