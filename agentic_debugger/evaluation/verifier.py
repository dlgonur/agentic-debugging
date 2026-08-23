"""Trusted-local verifier for one candidate unified diff.

The verifier protects the canonical benchmark fixture by copying it into a
fresh disposable workspace, confines evaluator patch paths, uses manifest cwd
and timeouts, and bounds retained output.  ``TRUSTED_LOCAL_WORKSPACE`` is not
an OS-level hostile-code security sandbox; Task 7 v1 deliberately defers
hostile-code containment.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from agentic_debugger.cancellation import CancellationError
from agentic_debugger.evaluation.outcome_taxonomy import classify_outcome
from agentic_debugger.evaluation.private_checks import run_private_checks
from agentic_debugger.evaluation.runner import (
    BaselineRecord,
    CollectionRecord,
    EvaluationInputError,
    EvaluationInvariantError,
    EvaluationResult,
    EvaluationStatus,
    ExecutionBoundary,
    LifecycleStatus,
    PatchRecord,
    PytestCounts,
    ReproductionRecord,
    SyntaxFileRecord,
    SyntaxRecord,
    TestRecord,
    TestRecordStatus,
    WorkspaceRecord,
    bounded_error,
    load_task,
    normalize_output,
)
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.runtime.execution import VerifiedExecutionContext
from agentic_debugger.runtime.exceptions import (
    PatchApplyError,
    PatchAuthorizationError,
    PatchStateError,
    PatchValidationError,
    WorkspaceError,
)
from agentic_debugger.runtime.patcher import PatchManager
from agentic_debugger.runtime.test_runner import TestRunKind, TestRunResult, TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace


# ---------------------------------------------------------------------------
# Optional non-invasive progress observation (Task-4 observability seam)
# ---------------------------------------------------------------------------
#
# Stage names are the application `VerifierStage` values (see
# ``agentic_debugger.application.events``); statuses are the application
# `VerifierStageStatus` values (``completed``/``failed``/``skipped``/
# ``cancelled``).  The verifier module stays decoupled from the application
# package: these are plain strings, and the application adapter validates
# them fail-closed.  The observer is optional, non-authoritative telemetry:
# an ordinary observer ``Exception`` is swallowed and never changes verifier
# behavior, result, classification, or workspace semantics.  The final
# ``EvaluationResult`` remains the only correctness authority.

STAGE_PREPARE_WORKSPACE = "prepare_workspace"
STAGE_BASELINE_REPRODUCTION = "baseline_reproduction"
STAGE_PRE_PATCH_TARGETED = "pre_patch_targeted"
STAGE_APPLY_CANDIDATE = "apply_candidate"
STAGE_SYNTAX_VALIDATION = "syntax_validation"
STAGE_POST_PATCH_REPRODUCTION = "post_patch_reproduction"
STAGE_F2P_P2P_CHECKS = "f2p_p2p_checks"
STAGE_BROADER_SUITE = "broader_suite"
STAGE_CLASSIFICATION = "classification"
STAGE_CLEANUP_INTEGRITY = "cleanup_integrity"


class VerifierProgressObserver(Protocol):
    """Optional observer of real verifier stage boundaries.

    ``stage_started`` fires immediately before the stage's work begins;
    ``stage_completed`` fires after the stage's work produced its record
    (with ``completed`` or ``failed``; stages never entered are not
    reported).  Implementations must be side-effect safe.
    """

    def stage_started(self, stage: str) -> None: ...

    def stage_completed(self, stage: str, status: str) -> None: ...


class NoopVerifierProgressObserver:
    """Default observer: receives stage boundaries and does nothing."""

    def stage_started(self, stage: str) -> None:
        return None

    def stage_completed(self, stage: str, status: str) -> None:
        return None


@dataclass
class _EvaluationState:
    task_max_test_runs: int
    verification_command_count: int = 0
    verification_selected_test_count: int = 0
    workspace_prepared: bool = False
    workspace_record: Optional[WorkspaceRecord] = None
    collection: Optional[CollectionRecord] = None
    reproduction: Optional[ReproductionRecord] = None
    baseline_f2p: list[TestRecord] = field(default_factory=list)
    baseline_p2p: list[TestRecord] = field(default_factory=list)
    baseline_valid: bool = False
    baseline_reason: Optional[str] = None
    patch: PatchRecord = field(default_factory=lambda: PatchRecord(False, False, False, (), 0, None))
    syntax: SyntaxRecord = field(default_factory=lambda: SyntaxRecord((), False, (), None))
    post_reproduction: Optional[ReproductionRecord] = None
    post_f2p: list[TestRecord] = field(default_factory=list)
    post_p2p: list[TestRecord] = field(default_factory=list)
    full_suite: Optional[TestRecord] = None
    timeout: bool = False
    status: EvaluationStatus = EvaluationStatus.INTERNAL_ERROR
    stop_reason: str = "not_started"
    outcome: Any = None
    diagnostic: Optional[str] = None
    canonical_hash_error: Optional[str] = None
    private_checks_passed: Optional[bool] = None


class EvaluationVerifier:
    """Evaluate one candidate diff using trusted local benchmark execution."""

    def __init__(
        self,
        repository_root: str,
        *,
        workspace_parent: Optional[str] = None,
        workspace_factory: Callable[..., TaskWorkspace] = TaskWorkspace,
        test_runner_factory: Callable[[TaskWorkspace], TestRunner] = TestRunner,
        execution_context: Optional[VerifiedExecutionContext] = None,
        progress_observer: Optional[VerifierProgressObserver] = None,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> None:
        if not isinstance(repository_root, str) or not repository_root:
            raise EvaluationInvariantError("repository_root must be non-empty")
        root = os.path.realpath(repository_root)
        if not os.path.isdir(root):
            raise EvaluationInvariantError("repository_root must be a directory")
        if workspace_parent is not None:
            if not isinstance(workspace_parent, str) or not os.path.isdir(workspace_parent):
                raise EvaluationInvariantError("workspace_parent must be an existing directory")
        if progress_observer is not None and not callable(
            getattr(progress_observer, "stage_started", None)
        ):
            raise EvaluationInvariantError("progress_observer must implement stage_started")
        if cancel_check is not None and not callable(cancel_check):
            raise EvaluationInvariantError("cancel_check must be callable")
        self._repository_root = root
        self._workspace_parent = workspace_parent
        self._workspace_factory = workspace_factory
        self._test_runner_factory = test_runner_factory
        self._execution_context = execution_context
        self._progress_observer = progress_observer
        self._cancel_check = cancel_check

    def _notify_stage_started(self, stage: str) -> None:
        observer = self._progress_observer
        if observer is None:
            return
        try:
            observer.stage_started(stage)
        except Exception:
            pass

    def _notify_stage_completed(self, stage: str, status: str) -> None:
        observer = self._progress_observer
        if observer is None:
            return
        try:
            observer.stage_completed(stage, status)
        except Exception:
            pass

    def _checkpoint(self) -> None:
        """Honor operational cancellation between verifier stages.

        The caller's ``cancel_check`` raises :class:`CancellationError` for
        cooperative cancellation; it propagates out of ``evaluate`` after
        cleanup runs (the ``finally`` block), so cancellation is never
        converted into a scientific verdict.
        """
        if self._cancel_check is not None:
            self._cancel_check()

    def evaluate(self, task: Any, candidate_patch: str) -> EvaluationResult:
        loaded = load_task(task)
        if loaded.source is not None and loaded.source.kind == "external":
            if self._execution_context is None:
                raise EvaluationInputError("external task requires a verified execution context")
            if self._test_runner_factory is not TestRunner:
                raise EvaluationInvariantError("external task cannot use an unbound test runner factory")
        _validate_candidate_patch(candidate_patch)
        fixture = self._fixture_path(loaded)
        state = _EvaluationState(loaded.constraints.max_test_runs)
        before_hash: Optional[str] = None
        try:
            before_hash = _tree_hash(fixture)
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as exc:
            state.canonical_hash_error = bounded_error(exc)
            state.status = EvaluationStatus.EVALUATOR_INVARIANT_FAILED
            state.stop_reason = "canonical_hash_failed"
            state.diagnostic = state.canonical_hash_error
        workspace: Optional[TaskWorkspace] = None
        try:
            if state.canonical_hash_error is None:
                self._checkpoint()
                self._notify_stage_started(STAGE_PREPARE_WORKSPACE)
                try:
                    workspace = self._make_workspace(fixture)
                except WorkspaceError as exc:
                    state.status = EvaluationStatus.WORKSPACE_PREPARATION_FAILED
                    state.stop_reason = "workspace_preparation_failed"
                    state.diagnostic = bounded_error(exc)
                    self._notify_stage_completed(STAGE_PREPARE_WORKSPACE, "failed")
                else:
                    state.workspace_prepared = True
                    state.workspace_record = WorkspaceRecord(LifecycleStatus.PREPARED, True, False, False, False, before_hash, None, None)
                    self._notify_stage_completed(STAGE_PREPARE_WORKSPACE, "completed")
                    if self._execution_context is not None:
                        runner = TestRunner(workspace, execution_context=self._execution_context)
                    else:
                        runner = self._test_runner_factory(workspace)
                    self._run_baseline(loaded, runner, state, workspace)
                    if not state.baseline_valid:
                        if state.timeout:
                            state.status = EvaluationStatus.TEST_TIMEOUT
                            state.stop_reason = state.baseline_reason or "baseline_timeout"
                        elif state.baseline_reason in {"baseline_collection_process_error", "baseline_collection_output_error", "baseline_test_execution_error"}:
                            state.status = EvaluationStatus.TEST_EXECUTION_FAILED
                            state.stop_reason = state.baseline_reason
                        else:
                            state.status = EvaluationStatus.BASELINE_INVALID
                            state.stop_reason = state.baseline_reason or "baseline_invalid"
                    else:
                        self._checkpoint()
                        self._run_candidate(loaded, candidate_patch, runner, workspace, state)
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except CancellationError:
            raise
        except EvaluationInvariantError as exc:
            state.status = EvaluationStatus.EVALUATOR_INVARIANT_FAILED
            state.stop_reason = "evaluator_invariant_failed"
            state.diagnostic = bounded_error(exc)
            state.outcome = None
        except (PatchValidationError, PatchAuthorizationError, PatchApplyError, PatchStateError) as exc:
            if not state.patch.attempted:
                state.patch = PatchRecord(True, False, False, (), 0, bounded_error(exc))
            state.status = EvaluationStatus.PATCH_APPLY_FAILED
            state.stop_reason = "patch_apply_failed"
            state.diagnostic = bounded_error(exc)
            state.outcome = None
            self._notify_stage_completed(STAGE_APPLY_CANDIDATE, "failed")
        except Exception as exc:
            state.status = EvaluationStatus.INTERNAL_ERROR
            state.stop_reason = "internal_error"
            state.diagnostic = bounded_error(exc)
            state.outcome = None
        finally:
            self._notify_stage_started(STAGE_CLEANUP_INTEGRITY)
            result = self._finish(loaded, state, before_hash, workspace, fixture)
            cleaned = result.workspace.cleaned and result.workspace.lifecycle is LifecycleStatus.CLEANED
            self._notify_stage_completed(
                STAGE_CLEANUP_INTEGRITY,
                "completed" if cleaned else "failed",
            )
        return result

    run = evaluate

    def _finish(self, task: DebugTask, state: _EvaluationState, before_hash: Optional[str], workspace: Optional[TaskWorkspace], fixture: str) -> EvaluationResult:
        cleanup_error: Optional[str] = None
        cleaned = False
        attempted = False
        prepared = state.workspace_prepared
        if workspace is not None:
            attempted = True
            try:
                workspace.cleanup()
                cleaned = not os.path.exists(workspace.root)
                if not cleaned:
                    cleanup_error = "workspace root remains after cleanup"
            except Exception as exc:
                cleanup_error = normalize_output(bounded_error(exc), self._workspace_root(workspace))
        after_hash: Optional[str] = None
        if state.canonical_hash_error is None:
            try:
                after_hash = _tree_hash(fixture)
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except Exception as exc:
                state.canonical_hash_error = bounded_error(exc)
                state.status = EvaluationStatus.EVALUATOR_INVARIANT_FAILED
                state.stop_reason = "canonical_hash_failed"
                state.outcome = None
                state.diagnostic = state.canonical_hash_error
        unchanged = before_hash is not None and after_hash is not None and before_hash == after_hash
        if not prepared:
            lifecycle = LifecycleStatus.NOT_ATTEMPTED
        elif cleanup_error is not None or not cleaned:
            lifecycle = LifecycleStatus.CLEANUP_FAILED
        else:
            lifecycle = LifecycleStatus.CLEANED
        workspace_error = cleanup_error or state.canonical_hash_error
        state.workspace_record = WorkspaceRecord(lifecycle, prepared, attempted, cleaned, unchanged, before_hash, after_hash, workspace_error)
        if state.canonical_hash_error is not None:
            state.status = EvaluationStatus.EVALUATOR_INVARIANT_FAILED
            state.stop_reason = "canonical_hash_failed"
            state.outcome = None
            state.diagnostic = state.canonical_hash_error
        elif not unchanged:
            state.status = EvaluationStatus.CANONICAL_FIXTURE_CHANGED
            state.stop_reason = "canonical_fixture_changed"
            state.outcome = None
            state.diagnostic = "canonical fixture bytes changed"
        elif cleanup_error is not None or (prepared and not cleaned):
            if state.status is not EvaluationStatus.CANONICAL_FIXTURE_CHANGED:
                state.status = EvaluationStatus.CLEANUP_FAILED
                state.stop_reason = "cleanup_failed"
                state.outcome = None
                state.diagnostic = cleanup_error or "workspace cleanup failed"
        return _state_result(task, state)

    def _fixture_path(self, task: DebugTask) -> str:
        source_path = task.source.path if task.source is not None else task.fixture_path
        if task.source is not None and task.source.kind == "external" and source_path.replace("\\", "/").startswith("agentic_debugger/datasets/curated/"):
            raise EvaluationInputError("external task source cannot use curated fixture namespace")
        fixture = os.path.realpath(os.path.join(self._repository_root, source_path))
        root = os.path.realpath(self._repository_root)
        if os.path.commonpath([fixture, root]) != root:
            raise EvaluationInputError("task source escapes repository root")
        if not os.path.isdir(fixture):
            raise EvaluationInputError("task source does not exist")
        return fixture

    def _make_workspace(self, fixture: str) -> TaskWorkspace:
        try:
            if self._workspace_parent is None:
                return self._workspace_factory(fixture)
            return self._workspace_factory(fixture, parent_dir=self._workspace_parent)
        except Exception as exc:
            raise WorkspaceError(bounded_error(exc)) from exc

    @staticmethod
    def _record_command(state: _EvaluationState, selected_test_count: int = 0) -> None:
        if type(selected_test_count) is not int or selected_test_count < 0:
            raise EvaluationInvariantError("selected_test_count must be non-negative")
        state.verification_command_count += 1
        state.verification_selected_test_count += selected_test_count

    def _run_baseline(self, task: DebugTask, runner: TestRunner, state: _EvaluationState, workspace: TaskWorkspace) -> None:
        all_nodes = tuple(task.tests.fail_to_pass) + tuple(task.tests.pass_to_pass)
        self._checkpoint()
        self._notify_stage_started(STAGE_BASELINE_REPRODUCTION)
        state.collection = self._run_collection(task, runner, state, workspace, all_nodes)
        if not state.collection.passed:
            state.baseline_reason = "baseline_collection_process_error" if state.collection.error in {"collection timeout", "collection process error"} else ("baseline_collection_output_error" if state.collection.error == "malformed collection output" else "baseline_collection_invalid")
            self._notify_stage_completed(STAGE_BASELINE_REPRODUCTION, "failed")
            return
        state.reproduction = self._run_reproduction(task, runner, state, workspace, "baseline")
        self._notify_stage_completed(STAGE_BASELINE_REPRODUCTION, "completed")
        self._checkpoint()
        self._notify_stage_started(STAGE_PRE_PATCH_TARGETED)
        for node in task.tests.fail_to_pass:
            state.baseline_f2p.append(self._run_node(task, runner, state, workspace, node, "F2P", "baseline"))
        for node in task.tests.pass_to_pass:
            state.baseline_p2p.append(self._run_node(task, runner, state, workspace, node, "P2P", "baseline"))
        self._notify_stage_completed(STAGE_PRE_PATCH_TARGETED, "completed")
        records = state.baseline_f2p + state.baseline_p2p
        if state.reproduction.timed_out or any(record.timed_out for record in records):
            state.timeout = True
            state.baseline_reason = "baseline_timeout"
        elif state.reproduction.status is TestRecordStatus.ERROR or any(record.status is TestRecordStatus.ERROR for record in records):
            state.baseline_reason = "baseline_test_execution_error"
        elif not (state.reproduction.status is TestRecordStatus.FAIL and state.reproduction.reproduction_match is True and state.reproduction.expected_failure):
            state.baseline_reason = "baseline_reproduction_not_genuine_failure"
        elif not all(record.status is TestRecordStatus.FAIL for record in state.baseline_f2p):
            state.baseline_reason = "baseline_f2p_not_genuine_failure"
        elif not all(record.status is TestRecordStatus.PASS for record in state.baseline_p2p):
            state.baseline_reason = "baseline_p2p_not_genuine_pass"
        else:
            state.baseline_valid = True

    def _run_candidate(self, task: DebugTask, candidate_patch: str, runner: TestRunner, workspace: TaskWorkspace, state: _EvaluationState) -> None:
        if not candidate_patch.strip():
            state.patch = PatchRecord(True, True, True, (), 0, None)
            state.syntax = SyntaxRecord((), True, (), None)
        else:
            self._checkpoint()
            self._notify_stage_started(STAGE_APPLY_CANDIDATE)
            manager = PatchManager(workspace, list(task.constraints.allowed_write_paths), list(task.constraints.denied_write_paths))
            applied = manager.apply_patch(candidate_patch)
            state.patch = PatchRecord(True, True, not applied.changed_files, tuple(sorted(item.path for item in applied.changed_files)), applied.hunk_count, None)
            self._notify_stage_completed(STAGE_APPLY_CANDIDATE, "completed")
            self._checkpoint()
            self._notify_stage_started(STAGE_SYNTAX_VALIDATION)
            checked = manager.syntax_check()
            state.syntax = SyntaxRecord(tuple(sorted(item.path for item in checked.results)), checked.all_passed, tuple(SyntaxFileRecord(item.path, item.success, item.error_type, item.message, item.line, item.column) for item in checked.results), None)
            self._notify_stage_completed(
                STAGE_SYNTAX_VALIDATION,
                "completed" if state.syntax.passed else "failed",
            )
        if not state.syntax.passed:
            state.status = EvaluationStatus.SYNTAX_FAILED
            state.stop_reason = "syntax_failed"
            return
        self._checkpoint()
        self._notify_stage_started(STAGE_POST_PATCH_REPRODUCTION)
        state.post_reproduction = self._run_reproduction(task, runner, state, workspace, "post")
        self._notify_stage_completed(STAGE_POST_PATCH_REPRODUCTION, "completed")
        self._checkpoint()
        self._notify_stage_started(STAGE_F2P_P2P_CHECKS)
        for node in task.tests.fail_to_pass:
            state.post_f2p.append(self._run_node(task, runner, state, workspace, node, "F2P", "post"))
        for node in task.tests.pass_to_pass:
            state.post_p2p.append(self._run_node(task, runner, state, workspace, node, "P2P", "post"))
        self._notify_stage_completed(STAGE_F2P_P2P_CHECKS, "completed")
        records = state.post_f2p + state.post_p2p
        if state.post_reproduction.timed_out or any(record.timed_out for record in records):
            state.timeout = True
            state.status = EvaluationStatus.TEST_TIMEOUT
            state.stop_reason = "post_patch_test_timeout"
            return
        if state.post_reproduction.status is TestRecordStatus.ERROR or any(record.status is TestRecordStatus.ERROR for record in records):
            state.status = EvaluationStatus.TEST_EXECUTION_FAILED
            state.stop_reason = "post_patch_test_execution_failed"
            return
        self._checkpoint()
        self._notify_stage_started(STAGE_BROADER_SUITE)
        state.full_suite = self._run_full_suite(task, runner, state, workspace)
        self._notify_stage_completed(STAGE_BROADER_SUITE, "completed")
        if state.full_suite.status is TestRecordStatus.TIMEOUT:
            state.timeout = True
            state.status = EvaluationStatus.TEST_TIMEOUT
            state.stop_reason = "full_suite_timeout"
            return
        if state.full_suite.status is TestRecordStatus.ERROR:
            state.status = EvaluationStatus.TEST_EXECUTION_FAILED
            state.stop_reason = "full_suite_execution_failed"
            return
        if not _full_suite_consistent(state.full_suite, state.post_f2p, state.post_p2p):
            state.status = EvaluationStatus.FULL_SUITE_CONTRADICTION
            state.stop_reason = "full_suite_contradicts_declared_nodes"
            return
        self._checkpoint()
        private = run_private_checks(task, runner, workspace)
        state.private_checks_passed = private.passed
        if private.applicable and private.passed is not True:
            state.status = EvaluationStatus.TEST_EXECUTION_FAILED
            state.stop_reason = "private_verifier_checks_failed"
            state.diagnostic = private.diagnostic or "private verifier checks failed"
            return
        self._checkpoint()
        self._notify_stage_started(STAGE_CLASSIFICATION)
        state.outcome = classify_outcome([record.passed for record in state.post_f2p], [record.passed for record in state.post_p2p])
        state.status = EvaluationStatus.COMPLETED
        state.stop_reason = "completed"
        self._notify_stage_completed(STAGE_CLASSIFICATION, "completed")

    def _run_collection(self, task: DebugTask, runner: TestRunner, state: _EvaluationState, workspace: TaskWorkspace, nodes: Sequence[str]) -> CollectionRecord:
        argv = list(task.tests.full_suite_argv) + ["--collect-only"]
        self._record_command(state)
        raw = runner.run_tests(argv, task.reproduction.cwd, task.tests.timeout_seconds, kind=TestRunKind.SELECTED)
        if raw.timed_out:
            state.timeout = True
            return CollectionRecord(tuple(nodes), (), False, "collection timeout")
        if raw.launch_error or raw.command_result.exit_code is None or raw.command_result.exit_code != 0:
            return CollectionRecord(tuple(nodes), (), False, "collection process error")
        output = normalize_output(raw.command_result.stdout + "\n" + raw.command_result.stderr, self._workspace_root(workspace))
        collected = _parse_collected_nodes(output)
        if not collected:
            return CollectionRecord(tuple(nodes), (), False, "malformed collection output")
        counts = Counter(collected)
        valid = not (set(collected) - set(nodes)) and not (set(nodes) - set(collected)) and all(count == 1 for count in counts.values()) and len(collected) == len(nodes)
        return CollectionRecord(tuple(nodes), tuple(collected), valid, None if valid else "declared node set mismatch")

    def _run_reproduction(self, task: DebugTask, runner: TestRunner, state: _EvaluationState, workspace: TaskWorkspace, phase: str) -> ReproductionRecord:
        self._record_command(state, 1)
        raw = runner.run_reproduction(task)
        return _reproduction_record(raw, task.reproduction.timeout_seconds, task.reproduction.expected_exit_code, task.tests.fail_to_pass[0], self._workspace_root(workspace), phase, self._portable_cwd(workspace, task.reproduction.cwd))

    def _run_node(self, task: DebugTask, runner: TestRunner, state: _EvaluationState, workspace: TaskWorkspace, node_id: str, kind: str, phase: str) -> TestRecord:
        self._record_command(state, 1)
        argv = _node_argv(task.reproduction.argv, node_id)
        raw = runner.run_tests(argv, task.reproduction.cwd, task.tests.timeout_seconds, kind=TestRunKind.SELECTED)
        return _test_record(raw, node_id, kind, task.tests.timeout_seconds, self._workspace_root(workspace), phase, self._portable_cwd(workspace, task.reproduction.cwd))

    def _run_full_suite(self, task: DebugTask, runner: TestRunner, state: _EvaluationState, workspace: TaskWorkspace) -> TestRecord:
        self._record_command(state, len(task.tests.fail_to_pass) + len(task.tests.pass_to_pass))
        raw = runner.run_full_suite(task)
        return _test_record(raw, "<FULL_SUITE>", "FULL_SUITE", task.tests.timeout_seconds, self._workspace_root(workspace), "post", self._portable_cwd(workspace, task.reproduction.cwd))

    @staticmethod
    def _workspace_root(value: Any) -> Optional[str]:
        return getattr(value, "root", None)

    @staticmethod
    def _portable_cwd(workspace: Any, cwd: str) -> str:
        root = getattr(workspace, "root", None)
        resolver = getattr(workspace, "resolve_path", None)
        if root and callable(resolver):
            try:
                resolved = os.path.realpath(resolver(cwd, must_exist=True))
                relative = os.path.relpath(resolved, os.path.realpath(root)).replace(os.sep, "/")
                return "<WORKSPACE>" if relative == "." else "<WORKSPACE>/" + relative
            except Exception:
                return "<OUTSIDE_WORKSPACE>"
        return cwd


def _validate_candidate_patch(candidate_patch: str) -> None:
    if not isinstance(candidate_patch, str):
        raise EvaluationInputError("candidate_patch must be an exact string")
    if len(candidate_patch) > 100_000:
        raise EvaluationInputError("candidate_patch exceeds maximum length")
    if "\x00" in candidate_patch:
        raise EvaluationInputError("candidate_patch contains a NUL byte")


def _node_argv(base: Sequence[str], node_id: str) -> list[str]:
    argv = [item for item in base if item not in {"-q", "--quiet"}]
    for index, argument in enumerate(argv):
        if "::" in argument or argument == node_id:
            argv[index] = node_id
            break
    else:
        argv.append(node_id)
    if "-vv" not in argv:
        argv.append("-vv")
    return argv


def _parse_collected_nodes(output: str) -> list[str]:
    found: list[str] = []
    for line in output.splitlines():
        value = line.strip()
        if "::" not in value or value.startswith("="):
            continue
        found.append(value)
    return found


def _parse_counts(output: str) -> Optional[PytestCounts]:
    text = output.lower()
    if "no tests ran" in text or "internalerror" in text:
        return None
    values = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0, "xpassed": 0}
    labels = {"passed": "passed", "failed": "failed", "errors": "errors?", "skipped": "skipped", "xfailed": "xfailed", "xpassed": "xpassed"}
    summary_pattern = re.compile(r"^\s*(?P<body>.+?)\s+in\s+(?:\d+(?:\.\d+)?|<DURATION>)s\s*$", re.IGNORECASE)
    for line in output.splitlines():
        candidate = line.strip("= \t")
        match = summary_pattern.match(candidate)
        if not match:
            continue
        found = 0
        for part in match.group("body").split(","):
            item = re.match(r"^\s*(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)\b", part, re.IGNORECASE)
            if item is None:
                continue
            number, label = int(item.group(1)), item.group(2).lower()
            key = "errors" if label in {"error", "errors"} else label
            values[key] += number
            found += number
        if found:
            return PytestCounts(passed=values["passed"], failed=values["failed"], errors=values["errors"], skipped=values["skipped"], xfailed=values["xfailed"], xpassed=values["xpassed"])
    return None


def _record_status(raw: TestRunResult, counts: Optional[PytestCounts], *, full: bool, selected_node_present: bool = True) -> tuple[TestRecordStatus, Optional[str]]:
    if raw.timed_out:
        return TestRecordStatus.TIMEOUT, "command timed out"
    if raw.launch_error or raw.command_result.exit_code is None:
        return TestRecordStatus.ERROR, "command launch failed"
    if counts is None:
        return TestRecordStatus.ERROR, "pytest summary or collection output was malformed"
    if full:
        if counts.errors or counts.skipped or counts.xfailed or counts.xpassed:
            return TestRecordStatus.ERROR, "full suite contains errors/skips/xfail/xpass"
        if counts.failed == 0 and raw.command_result.exit_code == 0:
            return TestRecordStatus.PASS, None
        if counts.failed > 0 and raw.command_result.exit_code != 0:
            return TestRecordStatus.FAIL, None
        return TestRecordStatus.ERROR, "full suite summary and exit code disagree"
    if not selected_node_present:
        return TestRecordStatus.ERROR, "selected pytest output did not identify the declared node"
    if counts.executed != 1 or counts.errors or counts.skipped or counts.xfailed or counts.xpassed:
        return TestRecordStatus.ERROR, "selected pytest command did not execute exactly one genuine test"
    if counts.passed == 1 and raw.command_result.exit_code == 0:
        return TestRecordStatus.PASS, None
    if counts.failed == 1 and raw.command_result.exit_code == 1:
        return TestRecordStatus.FAIL, None
    return TestRecordStatus.ERROR, "selected pytest result did not match pass/fail contract"


def _test_record(raw: TestRunResult, node_id: str, kind: str, timeout: float, workspace_root: Optional[str], phase: str, resolved_cwd: Optional[str] = None) -> TestRecord:
    if not isinstance(raw, TestRunResult) or not hasattr(raw, "command_result"):
        raise EvaluationInvariantError("test runner returned malformed result")
    command = raw.command_result
    if not hasattr(command, "argv") or not isinstance(command.argv, list):
        raise EvaluationInvariantError("test runner returned malformed command result")
    for name in ("timed_out", "stdout_truncated", "stderr_truncated"):
        if type(getattr(command, name, None)) is not bool:
            raise EvaluationInvariantError(f"test runner returned malformed {name}")
    if type(raw.timed_out) is not bool:
        raise EvaluationInvariantError("test runner returned malformed timeout flag")
    output = normalize_output(command.stdout + "\n" + command.stderr, workspace_root)
    counts = _parse_counts(output)
    node_status = _pytest_node_status(output, node_id) if kind != "FULL_SUITE" else "FULL_SUITE"
    status, parse_error = _record_status(raw, counts, full=kind == "FULL_SUITE", selected_node_present=node_status is not None)
    if kind != "FULL_SUITE" and status is TestRecordStatus.PASS and node_status != "PASSED":
        status, parse_error = TestRecordStatus.ERROR, "declared node status disagrees with pytest summary"
    elif kind != "FULL_SUITE" and status is TestRecordStatus.FAIL and node_status != "FAILED":
        status, parse_error = TestRecordStatus.ERROR, "declared node status disagrees with pytest summary"
    return TestRecord(node_id, kind, tuple(command.argv), resolved_cwd if resolved_cwd is not None else command.cwd, timeout, command.exit_code, status, raw.timed_out, normalize_output(command.stdout, workspace_root), normalize_output(command.stderr, workspace_root), command.stdout_truncated, command.stderr_truncated, counts or PytestCounts(), parse_error)


def _reproduction_record(raw: TestRunResult, timeout: float, expected: int, node_id: str, workspace_root: Optional[str], phase: str, resolved_cwd: Optional[str] = None) -> ReproductionRecord:
    if not isinstance(raw, TestRunResult) or not hasattr(raw, "command_result"):
        raise EvaluationInvariantError("test runner returned malformed reproduction result")
    command = raw.command_result
    for name in ("timed_out", "stdout_truncated", "stderr_truncated"):
        if type(getattr(command, name, None)) is not bool:
            raise EvaluationInvariantError(f"test runner returned malformed {name}")
    if type(raw.timed_out) is not bool:
        raise EvaluationInvariantError("test runner returned malformed timeout flag")
    output = normalize_output(command.stdout + "\n" + command.stderr, workspace_root)
    counts = _parse_counts(output)
    status, parse_error = _record_status(raw, counts, full=False)
    return ReproductionRecord(tuple(command.argv), resolved_cwd if resolved_cwd is not None else command.cwd, timeout, command.exit_code, status, expected, command.exit_code is not None and command.exit_code != 0, raw.reproduction_match, raw.timed_out, normalize_output(command.stdout, workspace_root), normalize_output(command.stderr, workspace_root), command.stdout_truncated, command.stderr_truncated, counts or PytestCounts(), parse_error)


def _pytest_node_status(output: str, node_id: str) -> Optional[str]:
    escaped = re.escape(node_id)
    match = re.search(rf"(?m)^\s*{escaped}\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b", output, flags=re.IGNORECASE)
    return match.group(1).upper() if match else None


def _full_suite_consistent(full: TestRecord, f2p: Sequence[TestRecord], p2p: Sequence[TestRecord]) -> bool:
    declared_total = len(f2p) + len(p2p)
    passed = sum(record.status is TestRecordStatus.PASS for record in tuple(f2p) + tuple(p2p))
    failed = sum(record.status is TestRecordStatus.FAIL for record in tuple(f2p) + tuple(p2p))
    counts = full.counts
    return counts.executed == declared_total and counts.passed == passed and counts.failed == failed and counts.errors == 0 and counts.skipped == 0 and counts.xfailed == 0 and counts.xpassed == 0


def _tree_hash(path: str) -> Optional[str]:
    if not os.path.isdir(path):
        return None
    digest = hashlib.sha256()
    root = Path(path)
    for file_path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = file_path.relative_to(root).as_posix()
        if file_path.suffix == ".pyc" and file_path.parent.name == "__pycache__":
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _state_result(task: DebugTask, state: _EvaluationState) -> EvaluationResult:
    baseline = BaselineRecord(state.baseline_valid, state.baseline_reason, state.collection, state.reproduction, tuple(state.baseline_f2p), tuple(state.baseline_p2p))
    workspace = state.workspace_record or WorkspaceRecord(LifecycleStatus.NOT_ATTEMPTED, False, False, False, False, None, None, None)
    return EvaluationResult(task.task_id, ExecutionBoundary.TRUSTED_LOCAL_WORKSPACE, state.status, state.stop_reason, state.outcome, workspace, baseline, state.patch, state.syntax, state.post_reproduction, tuple(state.post_f2p), tuple(state.post_p2p), state.full_suite, len(state.post_f2p), sum(record.passed for record in state.post_f2p), len(state.post_p2p), sum(record.passed for record in state.post_p2p), 1 if state.patch.attempted else 0, state.task_max_test_runs, state.verification_command_count, state.verification_selected_test_count, state.timeout, state.diagnostic, state.private_checks_passed)
