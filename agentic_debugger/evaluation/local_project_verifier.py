"""Independent, fail-closed verification for Local Project candidates.

The controller's worktree is evidence, not a correctness authority.  This
module binds a candidate to one clean source repository commit, exports that
commit without Git metadata, and evaluates the exact candidate in a second
disposable :class:`~agentic_debugger.runtime.workspace.TaskWorkspace`.

The boundary is trusted-local execution.  It protects the owner's repository
from evaluator writes, but it is not an operating-system sandbox for hostile
project code.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional, Sequence, Tuple

from agentic_debugger.cancellation import CancellationError
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome, classify_outcome
from agentic_debugger.evaluation.runner import (
    EvaluationInputError,
    EvaluationStatus,
    LifecycleStatus,
    PatchRecord,
    PytestCounts,
    SyntaxFileRecord,
    SyntaxRecord,
    TestRecord,
    TestRecordStatus,
    WorkspaceRecord,
    bounded_error,
    normalize_output,
)
from agentic_debugger.evaluation.verifier import (
    STAGE_APPLY_CANDIDATE,
    STAGE_BASELINE_REPRODUCTION,
    STAGE_CLASSIFICATION,
    STAGE_CLEANUP_INTEGRITY,
    STAGE_F2P_P2P_CHECKS,
    STAGE_POST_PATCH_REPRODUCTION,
    STAGE_PREPARE_WORKSPACE,
    STAGE_SYNTAX_VALIDATION,
    VerifierProgressObserver,
)
from agentic_debugger.runtime.command_runner import CommandResult, CommandRunner
from agentic_debugger.runtime.exceptions import (
    CommandExecutionError,
    PatchApplyError,
    PatchAuthorizationError,
    PatchStateError,
    PatchValidationError,
    WorkspaceError,
)
from agentic_debugger.runtime.patcher import PatchManager
from agentic_debugger.runtime.workspace import TaskWorkspace


_HEAD_PATTERN = re.compile(r"[0-9a-f]{40}")
_MAX_PATCH_CHARS = 100_000
_MAX_TIMEOUT_SECONDS = 600.0
_MAX_ARCHIVE_MEMBERS = 20_000
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_EXPORT_PREFIX = "agentic-debugger-local-verifier-"


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


class LocalProjectVerifier:
    """Evaluate a Local Project candidate independently from its controller."""

    def __init__(
        self,
        *,
        progress_observer: Optional[VerifierProgressObserver] = None,
        workspace_factory: Callable[..., TaskWorkspace] = TaskWorkspace,
        command_runner_factory: Callable[[TaskWorkspace], CommandRunner] = CommandRunner,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> None:
        if progress_observer is not None and not callable(getattr(progress_observer, "stage_started", None)):
            raise EvaluationInputError("progress_observer must implement stage_started")
        if progress_observer is not None and not callable(getattr(progress_observer, "stage_completed", None)):
            raise EvaluationInputError("progress_observer must implement stage_completed")
        if cancel_check is not None and not callable(cancel_check):
            raise EvaluationInputError("cancel_check must be callable or None")
        self._observer = progress_observer
        self._workspace_factory = workspace_factory
        self._runner_factory = command_runner_factory
        self._cancel_check = cancel_check

    def _checkpoint(self) -> None:
        if self._cancel_check is not None:
            self._cancel_check()

    def evaluate(self, plan: LocalProjectEvaluationPlan) -> LocalProjectEvaluationResult:
        if not isinstance(plan, LocalProjectEvaluationPlan):
            raise EvaluationInputError("plan must be LocalProjectEvaluationPlan")

        state = _State()
        before: Optional[_SourceState] = None
        ledger = _WorkspaceLedger()
        export_root: Optional[str] = None
        export_source: Optional[str] = None
        prepared = False

        try:
            self._checkpoint()
            before = _inspect_source(plan.source_repo_path)
            if before.head != plan.source_head_commit:
                state.status = EvaluationStatus.EVALUATOR_INVARIANT_FAILED
                state.stop_reason = "source_head_mismatch"
                state.diagnostic = "source HEAD does not match the candidate-bound commit"
                return self._finish(
                    plan, state, before, ledger, export_root, prepared,
                )
            if not before.clean:
                state.status = EvaluationStatus.EVALUATOR_INVARIANT_FAILED
                state.stop_reason = "source_repository_dirty"
                state.diagnostic = "source repository must be clean before verification"
                return self._finish(
                    plan, state, before, ledger, export_root, prepared,
                )
            workspace_parent = _validate_workspace_parent(plan.workspace_parent, before.root)

            self._checkpoint()
            self._stage_started(STAGE_PREPARE_WORKSPACE)
            try:
                export_root = tempfile.mkdtemp(prefix=_EXPORT_PREFIX, dir=workspace_parent)
                export_source = os.path.join(export_root, "source")
                os.mkdir(export_source)
                _export_commit(before.root, before.head, export_source, export_root)
                prepared = True
            except Exception as exc:
                state.status = EvaluationStatus.WORKSPACE_PREPARATION_FAILED
                state.stop_reason = "workspace_preparation_failed"
                state.diagnostic = bounded_error(exc)
                self._stage_completed(STAGE_PREPARE_WORKSPACE, "failed")
            else:
                self._stage_completed(STAGE_PREPARE_WORKSPACE, "completed")
                self._checkpoint()
                self._evaluate_candidate(
                    plan,
                    state,
                    export_source,
                    export_root,
                    ledger,
                )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except CancellationError:
            self._finish(
                plan,
                state,
                before,
                ledger,
                export_root,
                prepared,
            )
            raise
        except Exception as exc:
            state.status = EvaluationStatus.EVALUATOR_INVARIANT_FAILED
            state.stop_reason = "source_validation_failed"
            state.outcome = None
            state.diagnostic = bounded_error(exc)

        return self._finish(
            plan, state, before, ledger, export_root, prepared,
        )

    run = evaluate

    def _evaluate_candidate(
        self,
        plan: LocalProjectEvaluationPlan,
        state: _State,
        export_source: str,
        export_root: str,
        ledger: _WorkspaceLedger,
    ) -> None:
        """Evaluate four independent clean states.

        Baseline reproduction, baseline regression, patched reproduction, and
        patched regression never share a filesystem.  This prevents a setup
        script, test cache, generated file, database, or other command side
        effect from manufacturing a later pass.
        """
        self._checkpoint()
        self._stage_started(STAGE_BASELINE_REPRODUCTION)
        if plan.reproduction_argv is None:
            state.status = EvaluationStatus.BASELINE_INVALID
            state.stop_reason = "reproduction_command_missing"
            state.diagnostic = "a reproduction command is required for independent verification"
            self._stage_completed(STAGE_BASELINE_REPRODUCTION, "failed")
            return
        baseline = self._run_in_clean_workspace(
            plan,
            state,
            ledger,
            export_source,
            export_root,
            plan.reproduction_argv,
            "<REPRODUCTION>",
            "F2P_BASELINE",
        )
        state.baseline = baseline
        if ledger.cleanup_error is not None:
            self._stage_completed(STAGE_BASELINE_REPRODUCTION, "failed")
            return
        if baseline.status is TestRecordStatus.TIMEOUT:
            state.status = EvaluationStatus.TEST_TIMEOUT
            state.stop_reason = "baseline_reproduction_timeout"
            self._stage_completed(STAGE_BASELINE_REPRODUCTION, "failed")
            return
        if baseline.status is TestRecordStatus.ERROR:
            state.status = EvaluationStatus.TEST_EXECUTION_FAILED
            state.stop_reason = "baseline_reproduction_execution_failed"
            self._stage_completed(STAGE_BASELINE_REPRODUCTION, "failed")
            return
        if baseline.status is not TestRecordStatus.FAIL:
            state.status = EvaluationStatus.BASELINE_INVALID
            state.stop_reason = "baseline_reproduction_not_genuine_failure"
            state.diagnostic = "baseline reproduction exited zero; the reported failure was not reproduced"
            self._stage_completed(STAGE_BASELINE_REPRODUCTION, "failed")
            return
        if plan.regression_argv is None:
            state.status = EvaluationStatus.TEST_EXECUTION_FAILED
            state.stop_reason = "regression_command_missing"
            state.diagnostic = (
                "a separate regression command is required for independent verification"
            )
            self._stage_completed(STAGE_BASELINE_REPRODUCTION, "failed")
            return
        baseline_regression = self._run_in_clean_workspace(
            plan,
            state,
            ledger,
            export_source,
            export_root,
            plan.regression_argv,
            "<REGRESSION>",
            "P2P_BASELINE",
        )
        state.baseline_regression = baseline_regression
        if ledger.cleanup_error is not None:
            self._stage_completed(STAGE_BASELINE_REPRODUCTION, "failed")
            return
        if baseline_regression.status is TestRecordStatus.TIMEOUT:
            state.status = EvaluationStatus.TEST_TIMEOUT
            state.stop_reason = "baseline_regression_timeout"
            self._stage_completed(STAGE_BASELINE_REPRODUCTION, "failed")
            return
        if baseline_regression.status is TestRecordStatus.ERROR:
            state.status = EvaluationStatus.TEST_EXECUTION_FAILED
            state.stop_reason = "baseline_regression_execution_failed"
            self._stage_completed(STAGE_BASELINE_REPRODUCTION, "failed")
            return
        if baseline_regression.status is not TestRecordStatus.PASS:
            state.status = EvaluationStatus.BASELINE_INVALID
            state.stop_reason = "baseline_regression_not_passing"
            state.diagnostic = (
                "the designated pass-to-pass command did not pass on the clean baseline"
            )
            self._stage_completed(STAGE_BASELINE_REPRODUCTION, "failed")
            return
        self._stage_completed(STAGE_BASELINE_REPRODUCTION, "completed")

        self._checkpoint()
        self._stage_started(STAGE_APPLY_CANDIDATE)
        candidate_workspace = self._fresh_workspace(
            export_source, export_root, ledger
        )
        manager = PatchManager(candidate_workspace, list(plan.allowed_paths), list(plan.denied_paths))
        try:
            applied = manager.apply_patch(plan.candidate_patch)
        except (PatchValidationError, PatchAuthorizationError, PatchApplyError, PatchStateError) as exc:
            state.patch = PatchRecord(True, False, False, (), 0, bounded_error(exc))
            state.status = EvaluationStatus.PATCH_APPLY_FAILED
            state.stop_reason = "patch_apply_failed"
            state.diagnostic = bounded_error(exc)
            self._stage_completed(STAGE_APPLY_CANDIDATE, "failed")
            self._release_workspace(candidate_workspace, ledger)
            return
        changed = tuple(sorted(item.path for item in applied.changed_files))
        state.patch = PatchRecord(True, bool(applied.success), not changed, changed, applied.hunk_count, applied.error)
        if not applied.success or not changed:
            state.status = EvaluationStatus.PATCH_APPLY_FAILED
            state.stop_reason = "patch_apply_failed"
            state.diagnostic = applied.error or "candidate patch produced no changed files"
            self._stage_completed(STAGE_APPLY_CANDIDATE, "failed")
            self._release_workspace(candidate_workspace, ledger)
            return
        self._stage_completed(STAGE_APPLY_CANDIDATE, "completed")

        self._checkpoint()
        self._stage_started(STAGE_SYNTAX_VALIDATION)
        python_paths = [path for path in changed if path.endswith(".py")]
        try:
            syntax = manager.syntax_check(python_paths)
        except (PatchApplyError, PatchStateError, WorkspaceError) as exc:
            state.syntax = SyntaxRecord(tuple(python_paths), False, (), bounded_error(exc))
            state.status = EvaluationStatus.SYNTAX_FAILED
            state.stop_reason = "syntax_validation_failed"
            state.diagnostic = bounded_error(exc)
            self._stage_completed(STAGE_SYNTAX_VALIDATION, "failed")
            self._release_workspace(candidate_workspace, ledger)
            return
        syntax_records = tuple(
            SyntaxFileRecord(
                item.path,
                item.success,
                item.error_type,
                item.message,
                item.line,
                item.column,
            )
            for item in syntax.results
        )
        state.syntax = SyntaxRecord(tuple(python_paths), syntax.all_passed, syntax_records, None)
        if not syntax.all_passed:
            state.status = EvaluationStatus.SYNTAX_FAILED
            state.stop_reason = "syntax_validation_failed"
            state.diagnostic = "candidate contains invalid Python syntax"
            self._stage_completed(STAGE_SYNTAX_VALIDATION, "failed")
            self._release_workspace(candidate_workspace, ledger)
            return
        self._stage_completed(STAGE_SYNTAX_VALIDATION, "completed")

        self._checkpoint()
        self._stage_started(STAGE_POST_PATCH_REPRODUCTION)
        try:
            post = self._run_command(
                plan.reproduction_argv,
                "<REPRODUCTION>",
                "F2P",
                plan,
                state,
                candidate_workspace,
                self._runner_factory(candidate_workspace),
            )
            state.post_reproduction = post
        finally:
            self._release_workspace(candidate_workspace, ledger)
        if ledger.cleanup_error is not None:
            self._stage_completed(STAGE_POST_PATCH_REPRODUCTION, "failed")
            return
        if post.status is TestRecordStatus.TIMEOUT:
            state.status = EvaluationStatus.TEST_TIMEOUT
            state.stop_reason = "post_patch_reproduction_timeout"
            self._stage_completed(STAGE_POST_PATCH_REPRODUCTION, "failed")
            return
        if post.status is TestRecordStatus.ERROR:
            state.status = EvaluationStatus.TEST_EXECUTION_FAILED
            state.stop_reason = "post_patch_reproduction_execution_failed"
            self._stage_completed(STAGE_POST_PATCH_REPRODUCTION, "failed")
            return
        self._stage_completed(STAGE_POST_PATCH_REPRODUCTION, "completed")

        self._checkpoint()
        self._stage_started(STAGE_F2P_P2P_CHECKS)
        regression_workspace = self._fresh_workspace(
            export_source, export_root, ledger
        )
        try:
            second_manager = PatchManager(
                regression_workspace,
                list(plan.allowed_paths),
                list(plan.denied_paths),
            )
            second_applied = second_manager.apply_patch(plan.candidate_patch)
            second_changed = tuple(
                sorted(item.path for item in second_applied.changed_files)
            )
            if (
                not second_applied.success
                or second_changed != changed
                or second_applied.hunk_count != applied.hunk_count
            ):
                state.status = EvaluationStatus.PATCH_APPLY_FAILED
                state.stop_reason = "candidate_reapplication_failed"
                state.diagnostic = (
                    second_applied.error
                    or "candidate did not reproduce the same changed-file set"
                )
                self._stage_completed(STAGE_F2P_P2P_CHECKS, "failed")
                return
            regression = self._run_command(
                plan.regression_argv,
                "<REGRESSION>",
                "P2P",
                plan,
                state,
                regression_workspace,
                self._runner_factory(regression_workspace),
            )
            state.regression = regression
        except (
            PatchValidationError,
            PatchAuthorizationError,
            PatchApplyError,
            PatchStateError,
        ) as exc:
            state.status = EvaluationStatus.PATCH_APPLY_FAILED
            state.stop_reason = "candidate_reapplication_failed"
            state.diagnostic = bounded_error(exc)
            self._stage_completed(STAGE_F2P_P2P_CHECKS, "failed")
            return
        finally:
            self._release_workspace(regression_workspace, ledger)
        if ledger.cleanup_error is not None:
            self._stage_completed(STAGE_F2P_P2P_CHECKS, "failed")
            return
        if regression.status is TestRecordStatus.TIMEOUT:
            state.status = EvaluationStatus.TEST_TIMEOUT
            state.stop_reason = "regression_timeout"
            self._stage_completed(STAGE_F2P_P2P_CHECKS, "failed")
            return
        if regression.status is TestRecordStatus.ERROR:
            state.status = EvaluationStatus.TEST_EXECUTION_FAILED
            state.stop_reason = "regression_execution_failed"
            self._stage_completed(STAGE_F2P_P2P_CHECKS, "failed")
            return
        self._stage_completed(STAGE_F2P_P2P_CHECKS, "completed")

        self._checkpoint()
        self._stage_started(STAGE_CLASSIFICATION)
        state.outcome = classify_outcome([post.passed], [regression.passed])
        state.status = EvaluationStatus.COMPLETED
        state.stop_reason = "completed"
        self._stage_completed(STAGE_CLASSIFICATION, "completed")

    def _fresh_workspace(
        self,
        export_source: str,
        export_root: str,
        ledger: _WorkspaceLedger,
    ) -> TaskWorkspace:
        self._checkpoint()
        workspace = self._workspace_factory(export_source, parent_dir=export_root)
        ledger.workspaces.append(workspace)
        ledger.roots.append(workspace.root)
        if os.path.exists(os.path.join(workspace.root, ".git")):
            raise WorkspaceError("independent verifier workspace contains Git metadata")
        return workspace

    def _release_workspace(
        self,
        workspace: TaskWorkspace,
        ledger: _WorkspaceLedger,
    ) -> None:
        ledger.cleanup_attempted = True
        try:
            workspace.cleanup()
        except Exception as exc:
            ledger.cleanup_error = ledger.cleanup_error or bounded_error(exc)
        if os.path.exists(workspace.root) and ledger.cleanup_error is None:
            ledger.cleanup_error = "disposable verifier workspace remains after cleanup"

    def _run_in_clean_workspace(
        self,
        plan: LocalProjectEvaluationPlan,
        state: _State,
        ledger: _WorkspaceLedger,
        export_source: str,
        export_root: str,
        argv: Sequence[str],
        node_id: str,
        kind: str,
    ) -> TestRecord:
        workspace = self._fresh_workspace(export_source, export_root, ledger)
        try:
            return self._run_command(
                argv,
                node_id,
                kind,
                plan,
                state,
                workspace,
                self._runner_factory(workspace),
            )
        finally:
            self._release_workspace(workspace, ledger)

    def _run_command(
        self,
        argv: Sequence[str],
        node_id: str,
        kind: str,
        plan: LocalProjectEvaluationPlan,
        state: _State,
        workspace: TaskWorkspace,
        runner: CommandRunner,
    ) -> TestRecord:
        state.command_count += 1
        try:
            result = runner.run(
                list(argv),
                ".",
                plan.timeout_seconds,
                cancel_check=self._cancel_check,
            )
        except CommandExecutionError as exc:
            state.diagnostic = bounded_error(exc)
            return TestRecord(
                node_id, kind, tuple(argv), "<WORKSPACE>", plan.timeout_seconds,
                None, TestRecordStatus.ERROR, False, "", state.diagnostic,
                False, False, PytestCounts(), "command launch failed",
            )
        if not isinstance(result, CommandResult):
            raise EvaluationInputError("command runner returned malformed evidence")
        if result.timed_out:
            status = TestRecordStatus.TIMEOUT
            parse_error = "command timed out"
            state.timeout = True
        elif result.exit_code is None:
            status = TestRecordStatus.ERROR
            parse_error = "command produced no exit code"
        elif result.exit_code == 0:
            status = TestRecordStatus.PASS
            parse_error = None
        else:
            status = TestRecordStatus.FAIL
            parse_error = None
        return TestRecord(
            node_id=node_id,
            kind=kind,
            argv=tuple(result.argv),
            resolved_cwd="<WORKSPACE>",
            timeout_seconds=plan.timeout_seconds,
            exit_code=result.exit_code,
            status=status,
            timed_out=result.timed_out,
            stdout=normalize_output(result.stdout, workspace.root),
            stderr=normalize_output(result.stderr, workspace.root),
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            counts=PytestCounts(),
            parse_error=parse_error,
        )

    def _finish(
        self,
        plan: LocalProjectEvaluationPlan,
        state: _State,
        before: Optional[_SourceState],
        ledger: _WorkspaceLedger,
        export_root: Optional[str],
        prepared: bool,
    ) -> LocalProjectEvaluationResult:
        self._stage_started(STAGE_CLEANUP_INTEGRITY)
        for workspace in ledger.workspaces:
            if os.path.exists(workspace.root):
                self._release_workspace(workspace, ledger)
        if export_root is not None:
            ledger.cleanup_attempted = True
            try:
                _remove_tree(export_root)
            except Exception as exc:
                ledger.cleanup_error = ledger.cleanup_error or bounded_error(exc)
        cleaned = (
            all(not os.path.exists(root) for root in ledger.roots)
            and (export_root is None or not os.path.exists(export_root))
        )
        if ledger.cleanup_attempted and not cleaned and ledger.cleanup_error is None:
            ledger.cleanup_error = "disposable verifier workspace remains after cleanup"

        after: Optional[_SourceState] = None
        source_error: Optional[str] = None
        try:
            after = _inspect_source(plan.source_repo_path)
        except Exception as exc:
            source_error = bounded_error(exc)
        unchanged = bool(
            before is not None
            and after is not None
            and before.head == after.head == plan.source_head_commit
            and before.tree == after.tree
            and before.clean
            and after.clean
        )
        before_hash = before.fingerprint if before is not None else None
        after_hash = after.fingerprint if after is not None else None
        workspace_error = ledger.cleanup_error or source_error
        lifecycle = (
            LifecycleStatus.NOT_ATTEMPTED
            if not prepared
            else LifecycleStatus.CLEANED
            if cleaned and ledger.cleanup_error is None
            else LifecycleStatus.CLEANUP_FAILED
        )
        record = WorkspaceRecord(
            lifecycle=lifecycle,
            prepared=prepared,
            cleanup_attempted=ledger.cleanup_attempted,
            cleaned=cleaned,
            canonical_fixture_unchanged=unchanged,
            canonical_hash_before=before_hash,
            canonical_hash_after=after_hash,
            error=workspace_error,
        )
        if before is not None and not unchanged:
            state.status = EvaluationStatus.CANONICAL_FIXTURE_CHANGED
            state.stop_reason = "source_repository_changed"
            state.outcome = None
            state.diagnostic = source_error or "source repository HEAD or clean tracked content changed"
        elif ledger.cleanup_error is not None or (prepared and not cleaned):
            state.status = EvaluationStatus.CLEANUP_FAILED
            state.stop_reason = "cleanup_failed"
            state.outcome = None
            state.diagnostic = ledger.cleanup_error or "workspace cleanup failed"
        self._stage_completed(
            STAGE_CLEANUP_INTEGRITY,
            "completed"
            if unchanged and cleaned and ledger.cleanup_error is None
            else "failed",
        )
        return LocalProjectEvaluationResult(
            status=state.status,
            stop_reason=state.stop_reason,
            outcome=state.outcome,
            source_head_commit=plan.source_head_commit,
            candidate_sha256=plan.candidate_sha256,
            workspace=record,
            baseline_reproduction=state.baseline,
            baseline_regression=state.baseline_regression,
            patch_application=state.patch,
            syntax=state.syntax,
            post_patch_reproduction=state.post_reproduction,
            regression=state.regression,
            f2p_total=int(state.post_reproduction is not None),
            f2p_passed=int(state.post_reproduction is not None and state.post_reproduction.passed),
            p2p_total=int(state.regression is not None),
            p2p_passed=int(state.regression is not None and state.regression.passed),
            verification_command_count=state.command_count,
            timeout=state.timeout,
            diagnostic=state.diagnostic,
        )

    def _stage_started(self, stage: str) -> None:
        if self._observer is None:
            return
        try:
            self._observer.stage_started(stage)
        except Exception:
            pass

    def _stage_completed(self, stage: str, status: str) -> None:
        if self._observer is None:
            return
        try:
            self._observer.stage_completed(stage, status)
        except Exception:
            pass


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


def _run_git(repo: str, arguments: Sequence[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvaluationInputError(f"Git source operation could not run: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()[:1000]
        raise EvaluationInputError(f"Git {' '.join(arguments)} failed" + (f": {detail}" if detail else ""))
    return result


def _inspect_source(path: str) -> _SourceState:
    root = os.path.realpath(path)
    if not os.path.isdir(root):
        raise EvaluationInputError("source_repo_path must identify an existing directory")
    inside = _run_git(root, ["rev-parse", "--is-inside-work-tree"]).stdout.decode("ascii", "strict").strip()
    if inside != "true":
        raise EvaluationInputError("source_repo_path must identify a Git working tree")
    reported_root = _run_git(root, ["rev-parse", "--show-toplevel"]).stdout.decode("utf-8", "strict").strip()
    if os.path.normcase(os.path.realpath(reported_root)) != os.path.normcase(root):
        raise EvaluationInputError("source_repo_path must be the repository root")
    head = _run_git(root, ["rev-parse", "--verify", "HEAD"]).stdout.decode("ascii", "strict").strip()
    tree = _run_git(root, ["rev-parse", "--verify", "HEAD^{tree}"]).stdout.decode("ascii", "strict").strip()
    if _HEAD_PATTERN.fullmatch(head) is None or _HEAD_PATTERN.fullmatch(tree) is None:
        raise EvaluationInputError("source repository returned malformed object identity")
    status = _run_git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"],
    ).stdout
    return _SourceState(root=root, head=head, tree=tree, clean=status == b"")


def _export_commit(repo: str, head: str, destination: str, export_root: str) -> None:
    archive_path = os.path.join(export_root, "source.tar")
    _run_git(repo, ["archive", "--format=tar", "--output", archive_path, head])
    total_members = 0
    total_bytes = 0
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            for member in archive:
                total_members += 1
                total_bytes += max(0, member.size)
                if total_members > _MAX_ARCHIVE_MEMBERS or total_bytes > _MAX_ARCHIVE_BYTES:
                    raise WorkspaceError("source archive exceeds verifier extraction bounds")
                relative = _validated_archive_path(member.name)
                target = os.path.join(destination, *relative.parts)
                if member.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                if not member.isfile():
                    raise WorkspaceError(f"source archive contains unsupported entry: {member.name!r}")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise WorkspaceError(f"source archive member could not be read: {member.name!r}")
                with source, open(target, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                if member.mode & stat.S_IXUSR:
                    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR)
    finally:
        try:
            os.unlink(archive_path)
        except OSError:
            pass


def _validated_archive_path(value: str) -> PurePosixPath:
    if type(value) is not str or not value or "\\" in value or "\x00" in value:
        raise WorkspaceError("source archive contains an invalid path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkspaceError(f"source archive path escapes the export root: {value!r}")
    if path.parts[0].casefold() == ".git":
        raise WorkspaceError("source archive contains reserved Git metadata")
    return path


def _remove_tree(path: str) -> None:
    def remove_readonly(function: Callable[..., Any], target: str, _exc_info: object) -> None:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        function(target)

    if os.path.isdir(path):
        shutil.rmtree(path, onerror=remove_readonly)


def _purge_python_bytecode(root: str) -> None:
    for directory, directories, files in os.walk(root, topdown=False):
        for name in files:
            if name.endswith(".pyc"):
                try:
                    os.unlink(os.path.join(directory, name))
                except OSError as exc:
                    raise WorkspaceError(
                        f"could not remove disposable bytecode: {name}"
                    ) from exc
        for name in directories:
            if name == "__pycache__":
                cache = os.path.join(directory, name)
                try:
                    shutil.rmtree(cache)
                except OSError as exc:
                    raise WorkspaceError(
                        "could not remove disposable __pycache__ directory"
                    ) from exc


__all__ = [
    "LocalProjectEvaluationPlan",
    "LocalProjectEvaluationResult",
    "LocalProjectVerifier",
]
