"""Local Project verifier plan/result contracts and input validation.

This module owns the immutable verifier input/output contracts of the
independent Local Project verifier:

- :class:`LocalProjectEvaluationPlan` — the verifier input bound to one
  source commit and one candidate patch (argv tuples, policy paths,
  timeout, workspace parent), validated fail-closed;
- :class:`LocalProjectEvaluationResult` — the typed verifier verdict and
  its retained command/integrity evidence, with completed-result
  classification proof;
- the shared input validators (optional argv, policy paths, workspace
  parent placement).

The verifier ORCHESTRATION (the correctness authority itself) stays in
:mod:`agentic_debugger.evaluation.local_project_verifier`.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
from agentic_debugger.evaluation.runner import (
    EvaluationInputError,
    EvaluationStatus,
    PatchRecord,
    SyntaxRecord,
    TestRecord,
    TestRecordStatus,
    WorkspaceRecord,
)

_HEAD_PATTERN = re.compile(r"[0-9a-f]{40}")
_MAX_PATCH_CHARS = 100_000
_MAX_TIMEOUT_SECONDS = 600.0

@dataclass(frozen=True)
class LocalProjectEvaluationPlan:
    """Immutable verifier input bound to one source commit and candidate.

    ``None`` is the only representation of a missing command.  It is retained
    so callers can obtain an honest unresolved result instead of fabricating a
    regression pass.  Present commands are argv tuples and are always executed
    with ``shell=False`` by the established :class:`CommandRunner`.
    """

    source_repo_path: str
    source_head_commit: str
    candidate_patch: str
    reproduction_argv: Optional[Tuple[str, ...]]
    regression_argv: Optional[Tuple[str, ...]]
    allowed_paths: Tuple[str, ...]
    denied_paths: Tuple[str, ...]
    timeout_seconds: float = 30.0
    workspace_parent: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.source_repo_path) is not str or not self.source_repo_path:
            raise EvaluationInputError("source_repo_path must be a non-empty string")
        if type(self.source_head_commit) is not str or _HEAD_PATTERN.fullmatch(self.source_head_commit) is None:
            raise EvaluationInputError("source_head_commit must be an exact lowercase 40-hex SHA")
        if type(self.candidate_patch) is not str or not self.candidate_patch.strip():
            raise EvaluationInputError("candidate_patch must be a non-empty exact string")
        if len(self.candidate_patch) > _MAX_PATCH_CHARS:
            raise EvaluationInputError("candidate_patch exceeds maximum length")
        if "\x00" in self.candidate_patch:
            raise EvaluationInputError("candidate_patch contains a NUL byte")
        _validate_optional_argv(self.reproduction_argv, "reproduction_argv")
        _validate_optional_argv(self.regression_argv, "regression_argv")
        _validate_policy_paths(self.allowed_paths, "allowed_paths", required=True)
        _validate_policy_paths(self.denied_paths, "denied_paths", required=False)
        if type(self.timeout_seconds) not in (int, float) or isinstance(self.timeout_seconds, bool):
            raise EvaluationInputError("timeout_seconds must be a number")
        if not math.isfinite(self.timeout_seconds) or not (0 < self.timeout_seconds <= _MAX_TIMEOUT_SECONDS):
            raise EvaluationInputError("timeout_seconds must be positive and at most 600 seconds")
        if self.workspace_parent is not None and (
            type(self.workspace_parent) is not str or not self.workspace_parent
        ):
            raise EvaluationInputError("workspace_parent must be a non-empty string or None")

    @property
    def candidate_sha256(self) -> str:
        return hashlib.sha256(self.candidate_patch.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LocalProjectEvaluationResult:
    """Typed verifier verdict and its retained command/integrity evidence."""

    status: EvaluationStatus
    stop_reason: str
    outcome: Optional[SemanticOutcome]
    source_head_commit: str
    candidate_sha256: str
    workspace: WorkspaceRecord
    baseline_reproduction: Optional[TestRecord]
    baseline_regression: Optional[TestRecord]
    patch_application: PatchRecord
    syntax: SyntaxRecord
    post_patch_reproduction: Optional[TestRecord]
    regression: Optional[TestRecord]
    f2p_total: int
    f2p_passed: int
    p2p_total: int
    p2p_passed: int
    verification_command_count: int
    timeout: bool
    diagnostic: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, EvaluationStatus):
            raise EvaluationInputError("status must be EvaluationStatus")
        if self.outcome is not None and not isinstance(self.outcome, SemanticOutcome):
            raise EvaluationInputError("outcome must be SemanticOutcome or None")
        if self.status is not EvaluationStatus.COMPLETED and self.outcome is not None:
            raise EvaluationInputError("non-completed result cannot have a semantic outcome")
        if type(self.stop_reason) is not str or not self.stop_reason:
            raise EvaluationInputError("stop_reason must be a non-empty string")
        if _HEAD_PATTERN.fullmatch(self.source_head_commit) is None:
            raise EvaluationInputError("source_head_commit must be a lowercase 40-hex SHA")
        if re.fullmatch(r"[0-9a-f]{64}", self.candidate_sha256) is None:
            raise EvaluationInputError("candidate_sha256 must be a lowercase 64-hex digest")
        if not isinstance(self.workspace, WorkspaceRecord):
            raise EvaluationInputError("workspace must be WorkspaceRecord")
        if not isinstance(self.patch_application, PatchRecord) or not isinstance(self.syntax, SyntaxRecord):
            raise EvaluationInputError("patch and syntax evidence is malformed")
        for record in (
            self.baseline_reproduction,
            self.baseline_regression,
            self.post_patch_reproduction,
            self.regression,
        ):
            if record is not None and not isinstance(record, TestRecord):
                raise EvaluationInputError("command evidence must contain TestRecord values")
        expected_f2p_total = int(self.post_patch_reproduction is not None)
        expected_p2p_total = int(self.regression is not None)
        expected_f2p_passed = int(
            self.post_patch_reproduction is not None and self.post_patch_reproduction.passed
        )
        expected_p2p_passed = int(self.regression is not None and self.regression.passed)
        if (self.f2p_total, self.f2p_passed, self.p2p_total, self.p2p_passed) != (
            expected_f2p_total,
            expected_f2p_passed,
            expected_p2p_total,
            expected_p2p_passed,
        ):
            raise EvaluationInputError("result counts disagree with retained command evidence")
        if type(self.verification_command_count) is not int or self.verification_command_count < 0:
            raise EvaluationInputError("verification_command_count must be non-negative")
        if type(self.timeout) is not bool:
            raise EvaluationInputError("timeout must be boolean")
        if self.status is EvaluationStatus.COMPLETED:
            if (
                self.outcome is None
                or self.baseline_reproduction is None
                or self.baseline_reproduction.status is not TestRecordStatus.FAIL
                or self.baseline_regression is None
                or not self.baseline_regression.passed
                or self.post_patch_reproduction is None
                or self.regression is None
            ):
                raise EvaluationInputError("completed result lacks classification evidence")
            if not self.workspace.canonical_fixture_unchanged or not self.workspace.cleaned:
                raise EvaluationInputError("completed result lacks workspace/source integrity proof")

    @property
    def resolved(self) -> bool:
        return self.status is EvaluationStatus.COMPLETED and self.outcome is SemanticOutcome.RESOLVED

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "stop_reason": self.stop_reason,
            "outcome": self.outcome.value if self.outcome is not None else None,
            "source_head_commit": self.source_head_commit,
            "candidate_sha256": self.candidate_sha256,
            "workspace": self.workspace.to_mapping(),
            "baseline_reproduction": (
                self.baseline_reproduction.to_mapping() if self.baseline_reproduction is not None else None
            ),
            "baseline_regression": (
                self.baseline_regression.to_mapping()
                if self.baseline_regression is not None
                else None
            ),
            "patch_application": self.patch_application.to_mapping(),
            "syntax": self.syntax.to_mapping(),
            "post_patch_reproduction": (
                self.post_patch_reproduction.to_mapping() if self.post_patch_reproduction is not None else None
            ),
            "regression": self.regression.to_mapping() if self.regression is not None else None,
            "f2p_total": self.f2p_total,
            "f2p_passed": self.f2p_passed,
            "p2p_total": self.p2p_total,
            "p2p_passed": self.p2p_passed,
            "verification_command_count": self.verification_command_count,
            "timeout": self.timeout,
            "diagnostic": self.diagnostic,
        }


@dataclass
class _State:
    status: EvaluationStatus = EvaluationStatus.INTERNAL_ERROR
    stop_reason: str = "not_started"
    outcome: Optional[SemanticOutcome] = None
    baseline: Optional[TestRecord] = None
    baseline_regression: Optional[TestRecord] = None
    patch: PatchRecord = field(default_factory=lambda: PatchRecord(False, False, False, (), 0, None))
    syntax: SyntaxRecord = field(default_factory=lambda: SyntaxRecord((), False, (), None))
    post_reproduction: Optional[TestRecord] = None
    regression: Optional[TestRecord] = None
    command_count: int = 0
    timeout: bool = False
    diagnostic: Optional[str] = None


@dataclass(frozen=True)
class _SourceState:
    root: str
    head: str
    tree: str
    clean: bool

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.head}\x00{self.tree}".encode("ascii")).hexdigest()


@dataclass
class _WorkspaceLedger:
    """All disposable workspaces used by one independent evaluation."""

    workspaces: list[TaskWorkspace] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    cleanup_attempted: bool = False
    cleanup_error: Optional[str] = None


def _validate_optional_argv(value: Optional[Tuple[str, ...]], label: str) -> None:
    if value is None:
        return
    if type(value) is not tuple or not value:
        raise EvaluationInputError(f"{label} must be a non-empty tuple of strings or None")
    for index, item in enumerate(value):
        if type(item) is not str or not item or "\x00" in item:
            raise EvaluationInputError(f"{label}[{index}] must be a non-empty NUL-free string")


def _validate_policy_paths(value: Tuple[str, ...], label: str, *, required: bool) -> None:
    if type(value) is not tuple or (required and not value):
        suffix = "non-empty " if required else ""
        raise EvaluationInputError(f"{label} must be a {suffix}tuple of strings")
    if any(type(item) is not str or not item.strip() or "\x00" in item for item in value):
        raise EvaluationInputError(f"{label} must contain non-empty NUL-free strings")
    if len(set(value)) != len(value):
        raise EvaluationInputError(f"{label} must not contain duplicates")


def _validate_workspace_parent(value: Optional[str], source_root: str) -> Optional[str]:
    if value is None:
        return None
    parent = os.path.realpath(value)
    if not os.path.isdir(parent):
        raise EvaluationInputError("workspace_parent must be an existing directory")
    try:
        inside_source = os.path.normcase(os.path.commonpath([parent, source_root])) == os.path.normcase(source_root)
    except ValueError:
        inside_source = False
    if inside_source:
        raise EvaluationInputError("workspace_parent must be outside the source repository")
    return parent
