"""The deterministic demonstration tool registry.

This module owns :func:`build_registry` — the deterministic construction
of the controller-visible tool registry (every tool name, action
schema, argument validation, bounded diagnostics, PDB proof semantics,
and validation-evidence readiness) — plus the public demo-tool import
surface.

Architecture (one-way dependency graph):

* :mod:`agentic_debugger.demo.diagnostics` — bounded-safe helpers and
  validation readiness;
* :mod:`agentic_debugger.demo.pdb_probe` — the disposable debugger
  target preparation (:class:`PdbProbe`);
* :mod:`agentic_debugger.demo.context` — the mutable
  :class:`DemoToolContext`;
* this module — the tool registry and the public surface.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Optional

from agentic_debugger.agent.controller_policy import (
    ActionName,
    HypothesisConfidence,
    PdbPolicy,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import (
    ToolExecutionError,
    ToolRegistry,
    ToolRejectedError,
    ToolResult,
    ToolSpec,
    ToolTimeoutError,
)
from agentic_debugger.application.source_snapshots import (
    SourceSnapshotStage,
    capture_source_snapshot,
)
from agentic_debugger.demo.catalog import PROBE_DRIVER_FUNCTION
from agentic_debugger.demo.context import DemoToolContext
from agentic_debugger.demo.diagnostics import (
    EXACT_PROOF_SOURCE_WINDOW_RADIUS,
    MAX_DIAGNOSTIC_CHARS,
    MAX_RAW_FAILURE_OUTPUT_CHARS,
    MAX_VERIFIER_FAILURE_DETAIL_CHARS,
    SOURCE_WINDOW_RADIUS,
    DemoToolError,
    _json_safe,
    _safe_rejection,
    _validator,
    bounded_diagnostic,
    bounded_diagnostic_text,
    legal_reproduction_phases,
    pytest_argv,
    reproduction_failure_output,
    reproduction_failure_output_raw,
    task_target_module_path,
    validation_classification_ready,
)
from agentic_debugger.demo.pdb_probe import (
    PdbProbe,
    opaque_workspace_id,
    prepare_pdb_probe,
)
from agentic_debugger.evaluation.outcome_taxonomy import classify_outcome
from agentic_debugger.evaluation.runner import bounded_error, normalize_output
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.events.schema import Action, ObservationStatus
from agentic_debugger.runtime.exceptions import (
    PatchApplyError,
    PatchAuthorizationError,
    PatchRevertError,
    PatchStateError,
    PatchValidationError,
    PdbSessionError,
    PdbSessionTimeoutError,
    SourceInspectionError,
    SourceParseError,
    WorkspaceError,
)
from agentic_debugger.runtime.patcher import (
    PatchManager,
    build_bounded_patch_failure_payload,
)
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.test_runner import TestRunKind, TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.skills.file_skills import get_source_window
from agentic_debugger.skills.search_skills import find_function

def _ok(payload: dict[str, Any], summary: str) -> ToolResult:
    return ToolResult(ObservationStatus.OK, payload, summary)


def build_registry(
    context: DemoToolContext,
    *,
    pdb_policy: Any = None,
    interactive_debugger_controls: bool = False,
) -> ToolRegistry:
    """Register every demonstration tool against the accepted registry."""

    task = context.task
    reproduction_argv = tuple(task.reproduction.argv)
    reproduction_cwd = task.reproduction.cwd

    def spec(
        name: ActionName,
        validator: Callable[[dict[str, object]], dict[str, object]],
        handler: Callable[[Action, dict[str, object]], ToolResult],
    ) -> ToolSpec:
        def guarded(action: Action, arguments: dict[str, object]) -> ToolResult:
            context.tool_calls.append(action.name)
            try:
                return handler(action, arguments)
            except BaseException as exc:  # noqa: BLE001 - record, then surface
                context.record_error(action.name, exc)
                raise

        return ToolSpec(
            name,
            validator,
            guarded,
            version="demo-1",
            argument_contract=getattr(validator, "argument_contract", {}),
        )

    # -- reproduction and tests -------------------------------------------

    def handle_run_reproduction(action: Action, arguments: dict[str, object]) -> ToolResult:
        phase = arguments["phase"]
        if phase not in legal_reproduction_phases(action.state):
            raise _safe_rejection("phase must be baseline or post_patch")
        result = context.test_runner.run_reproduction(task)
        if result.timed_out:
            raise ToolTimeoutError("reproduction command timed out")
        if result.launch_error or result.command_result.exit_code is None:
            raise ToolExecutionError("reproduction command could not be launched")
        node_id = task.tests.fail_to_pass[0]
        reproduced = bool(result.reproduction_match) and not result.passed
        # Sanitized production diagnostic for the model (never hidden-test
        # content) plus the bounded RAW output retained as evidence only.
        module_path = task_target_module_path(task)
        source_path = Path(context.workspace.root) / module_path
        try:
            original_line_count = len(
                source_path.read_text(encoding="utf-8").splitlines()
            )
        except OSError:
            raise ToolExecutionError(
                "production module is missing from the disposable workspace"
            ) from None
        payload: dict[str, Any] = {
            "phase": phase,
            "node_id": node_id,
            "exit_code": result.command_result.exit_code,
            "expected_exit_code": task.reproduction.expected_exit_code,
            "passed": bool(result.passed),
            "failure_reproduced": reproduced,
            # Sanitized production diagnostic (common deterministic
            # sanitizer): structured production exception or generic
            # behavioral-failure statement.  Never hidden test source,
            # assertions, node ids, or expected literals.
            "failure_output": reproduction_failure_output(
                result, context.workspace.root, module_path,
                original_line_count,
            ) if not result.passed else "",
            # Bounded RAW reproduction output — audit-only evidence, never
            # rendered into any model prompt.
            "failure_output_raw": reproduction_failure_output_raw(
                result, context.workspace.root
            ) if not result.passed else "",
        }
        if context.probe is not None and context.probe.exact_public_reproduction:
            payload["reproduction_argv"] = list(task.reproduction.argv)
        if phase == "baseline":
            context.baseline_failure_reproduced = reproduced
            summary = "baseline reproduction executed"
        else:
            context.post_patch_f2p_passed = bool(result.passed)
            summary = "post-patch reproduction executed"
        return _ok(payload, summary)

    def handle_run_regression_tests(action: Action, arguments: dict[str, object]) -> ToolResult:
        nodes = list(task.tests.pass_to_pass)
        if not nodes:
            raise ToolExecutionError("task declares no pass-to-pass tests")
        argv = pytest_argv(reproduction_argv, nodes)
        result = context.test_runner.run_tests(
            argv,
            reproduction_cwd,
            task.tests.timeout_seconds,
            kind=TestRunKind.REGRESSION,
        )
        if result.timed_out:
            raise ToolTimeoutError("regression command timed out")
        if result.launch_error or result.command_result.exit_code is None:
            raise ToolExecutionError("regression command could not be launched")
        all_passed = bool(result.passed)
        context.regression_passed = all_passed
        return _ok(
            {
                "node_ids": nodes,
                "node_count": len(nodes),
                "exit_code": result.command_result.exit_code,
                "all_passed": all_passed,
            },
            "designated regression tests executed",
        )

    def handle_classify_outcome(action: Action, arguments: dict[str, object]) -> ToolResult:
        if not context.validation_evidence_ready():
            raise ToolExecutionError("validation evidence is incomplete")
        f2p = [context.post_patch_f2p_passed]
        p2p = [context.regression_passed]
        outcome = classify_outcome(f2p, p2p)
        context.controller_outcome = outcome.value
        return _ok(
            {
                "outcome": outcome.value,
                "f2p_passed": f2p,
                "p2p_passed": p2p,
                "evidence_scope": "controller_validation",
            },
            "controller validation outcome classified",
        )

    def handle_get_failure_trace(
        action: Action, arguments: dict[str, object]
    ) -> ToolResult:
        """Capture one bounded post-mortem PDB observation and clean up.

        The action is legal only after the baseline failure has been
        reproduced, uses the already-prepared disposable probe copy, and is
        charged to the controller's PDB-observation budget.  The full strict
        ``PdbResponse`` mapping is retained in the ToolResult, so canonical
        trajectory projection/replay records the actual protocol evidence.
        """

        if pdb_policy is PdbPolicy.DISABLED:
            raise _safe_rejection("PDB access is disabled by evaluation policy")
        if context.baseline_failure_reproduced is not True:
            raise _safe_rejection(
                "post-mortem failure trace requires a reproduced baseline failure"
            )
        probe = context.probe
        if probe is None:
            raise _safe_rejection("no post-mortem probe is configured for this task")
        if context.pdb_session is not None or context.pdb_workspace is not None:
            raise _safe_rejection("a PDB session is already active")

        try:
            workspace = TaskWorkspace(
                str(probe.source_dir), parent_dir=str(probe.parent_dir)
            )
        except WorkspaceError as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        context.pdb_workspace = workspace
        try:
            session = create_pdb_session(workspace)
        except Exception as exc:
            cleanup_errors = context.release_pdb()
            if cleanup_errors:
                raise ToolExecutionError(
                    bounded_diagnostic(cleanup_errors[0], context.workspace.root)
                ) from exc
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        context.pdb_session = session
        try:
            session.start()
            context.pdb_session_started = True
            response = session.run_post_mortem(probe.script)
        except Exception as exc:
            # Catch every exception (not just PdbSessionError/TimeoutError) so
            # an unexpected OSError, BrokenPipeError, RuntimeError, or worker
            # crash still releases the PDB session and removes the disposable
            # workspace.  Without this, a non-PDB exception leaks both.
            cleanup_errors = context.release_pdb()
            if cleanup_errors:
                raise ToolExecutionError(
                    bounded_diagnostic(cleanup_errors[0], context.workspace.root)
                ) from exc
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc

        serialization_error: Exception | None = None
        response_mapping: dict[str, Any] | None = None
        try:
            response_mapping = _json_safe(
                response.to_mapping(), "post-mortem PDB response"
            )
        except Exception as exc:
            serialization_error = exc
        cleanup_errors = context.release_pdb()
        if cleanup_errors:
            raise ToolExecutionError(
                bounded_diagnostic(cleanup_errors[0], context.workspace.root)
            )
        if serialization_error is not None:
            raise ToolExecutionError(
                bounded_diagnostic(serialization_error, context.workspace.root)
            ) from serialization_error
        if response_mapping is None:
            raise ToolExecutionError("post-mortem PDB response was not retained")
        if response.success is not True:
            raise ToolExecutionError("post-mortem PDB request failed closed")

        status = response.result.get("status")
        post_mortem = response.result.get("post_mortem") is True
        if status not in {"post_mortem", "exited"}:
            raise ToolExecutionError("post-mortem PDB response has invalid status")
        if exact_pytest_bootstrap_failure(response.result):
            raise ToolExecutionError(
                "exact public pytest probe failed before reaching the target"
            )
        context.pdb_observation_names.append("get_failure_trace")
        return _ok(
            {
                "evidence_kind": "pdb-post-mortem-v1",
                "pdb_response": response_mapping,
                "post_mortem": post_mortem,
                "session_stopped": context.pdb_session is None,
                "workspace_removed": context.pdb_workspace is None,
            },
            (
                "bounded post-mortem traceback evidence captured"
                if post_mortem
                else "post-mortem target exited without traceback evidence"
            ),
        )

    # -- static source retrieval ------------------------------------------

    def handle_find_function(action: Action, arguments: dict[str, object]) -> ToolResult:
        try:
            match = find_function(context.workspace, arguments["name"], arguments["path"])
        except (SourceInspectionError, SourceParseError, WorkspaceError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        if match is None:
            raise ToolExecutionError("declared symbol was not found in the declared file")
        return _ok(_json_safe(match.to_mapping(), "find_function"), "declared symbol located")

    def handle_get_source_window(action: Action, arguments: dict[str, object]) -> ToolResult:
        line = arguments["line"]
        if line < 1:
            raise _safe_rejection("line must be positive")
        try:
            window = get_source_window(
                context.workspace,
                arguments["path"],
                line,
                EXACT_PROOF_SOURCE_WINDOW_RADIUS
                if context.probe is not None
                and context.probe.exact_public_reproduction
                else SOURCE_WINDOW_RADIUS,
            )
        except (SourceInspectionError, WorkspaceError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        return _ok(_json_safe(window.to_mapping(), "get_source_window"), "source window retrieved")

    def handle_express_hypothesis(action: Action, arguments: dict[str, object]) -> ToolResult:
        declared = {
            "hypothesis_id": str(arguments["hypothesis_id"]),
            "statement": str(arguments["statement"]),
            "target_file": str(arguments["target_file"]),
            "target_symbol": str(arguments["target_symbol"]),
            "confidence": str(arguments["confidence"]),
        }
        bound = context.validate_bound_diagnosis(
            action,
            arguments.get("evidence_refs"),
            arguments.get("observed_values"),
        )
        context.declared_localization = {
            "file_path": declared["target_file"],
            "symbol": declared["target_symbol"],
        }
        # The hypothesis is an explicit model-authored diagnosis artifact
        # (already recorded verbatim in the canonical tool observation); the
        # app event carries the bounded structured claim, never hidden
        # reasoning or evaluator information.
        context.observe(
            lambda: context.observability.diagnosis_recorded(
                text=declared["statement"],
                file_path=declared["target_file"],
                symbol=declared["target_symbol"],
                confidence=declared["confidence"],
                evidence_refs=bound.get("evidence_refs"),
                observed_values=bound.get("observed_values"),
                proof_contract=bound.get("proof_contract"),
            )
        )
        return _ok({**declared, **bound}, "root-cause hypothesis recorded")

    # -- patch lifecycle ---------------------------------------------------

    def handle_apply_patch(action: Action, arguments: dict[str, object]) -> ToolResult:
        # A new apply attempt replaces or abandons the previous candidate, so
        # any earlier Validate evidence is no longer about the workspace.
        context.clear_validation_evidence()
        diff = arguments["patch"]
        if context.patch_manager.has_active_patch and context.candidate_patch == diff:
            raise _safe_rejection("the candidate patch is already active")
        attempt_index = context.patch_attempt_index
        context.patch_attempt_index += 1
        patch_sha256 = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        reverted_previous = False
        # A repair retry replaces the previous accepted candidate: if the
        # PatchManager still holds an active patch, revert it first so the
        # new diff is applied to the pristine baseline (deterministic
        # revise-patch semantics for the verifier-feedback loop).
        if context.patch_manager.has_active_patch:
            try:
                context.patch_manager.revert_patch()
            except (
                PatchStateError,
                PatchApplyError,
                PatchRevertError,
                Exception,
            ) as exc:
                bounded_diag = bounded_diagnostic(exc, context.workspace.root)
                payload_data, recoverable, error_kind = build_bounded_patch_failure_payload(
                    exc, error_kind="revert_failure", recoverable=False
                )
                context.observe(
                    lambda: context.observability.patch_apply_failed(
                        attempt_index, bounded_diag
                    )
                )
                raise ToolExecutionError(
                    bounded_diag,
                    safe_diagnostic=bounded_diag,
                    recoverable=False,
                    payload_data=payload_data,
                ) from exc
            reverted_previous = True
            context.observe(
                lambda: context.observability.patch_reverted(attempt_index - 1)
            )
            context._capture_changed_source(SourceSnapshotStage.REVERTED)
        context.observe(
            lambda: context.observability.patch_proposed(
                attempt_index, patch_sha256, patch_text=diff
            )
        )
        try:
            result = context.patch_manager.apply_patch(diff)
        except (
            PatchValidationError,
            PatchAuthorizationError,
            PatchStateError,
            PatchApplyError,
            PatchRevertError,
            Exception,
        ) as exc:
            bounded_diag = bounded_diagnostic(exc, context.workspace.root)
            payload_data, recoverable, error_kind = build_bounded_patch_failure_payload(exc)

            if isinstance(
                exc,
                (
                    PatchValidationError,
                    PatchAuthorizationError,
                    PatchStateError,
                ),
            ):
                context.observe(
                    lambda: context.observability.patch_rejected(
                        attempt_index, bounded_diag
                    )
                )
                raise ToolRejectedError(
                    bounded_diag,
                    safe_diagnostic=bounded_diag,
                    recoverable=recoverable,
                    payload_data=payload_data,
                ) from exc
            else:
                context.observe(
                    lambda: context.observability.patch_apply_failed(
                        attempt_index, bounded_diag
                    )
                )
                raise ToolExecutionError(
                    bounded_diag,
                    safe_diagnostic=bounded_diag,
                    recoverable=recoverable,
                    payload_data=payload_data,
                ) from exc
        # Only a patch that passed the real PatchManager lifecycle becomes
        # authoritative evidence for the evaluator.  Rejected or failed
        # attempts never overwrite the accepted candidate.
        context.candidate_patch = diff
        context.patch_applied = bool(result.success)
        context.patch_changed_files = tuple(sorted(item.path for item in result.changed_files))
        context.observe(
            lambda: context.observability.patch_applied(
                attempt_index, context.patch_changed_files, None
            )
        )
        context._capture_changed_source(SourceSnapshotStage.APPLIED)
        # Real independent-verifier feedback on the exact accepted candidate
        # (optional; bound failures, never crash the tool).
        verifier_feedback: Optional[dict[str, Any]] = None
        if context.verifier_feedback_fn is not None:
            try:
                verifier_feedback = context.verifier_feedback_fn(task, diff)
            except BaseException as exc:  # noqa: BLE001 - bounded, recorded
                verifier_feedback = {
                    "error": bounded_diagnostic(exc, context.workspace.root),
                }
            if isinstance(verifier_feedback, dict):
                context.verifier_feedback_history.append(dict(verifier_feedback))
        payload: dict[str, Any] = {
            "applied": bool(result.success),
            "changed_files": list(context.patch_changed_files),
            "hunk_count": result.hunk_count,
            "patch_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            "after_sha256": {key: result.after_sha256[key] for key in sorted(result.after_sha256)},
            "hunk_adjustments": [list(item) for item in result.hunk_adjustments],
            "reverted_previous": reverted_previous,
        }
        if verifier_feedback is not None:
            payload["verifier_feedback"] = verifier_feedback
        return _ok(
            _json_safe(payload, "apply_patch"),
            "candidate patch applied to the disposable workspace",
        )

    def handle_revert_patch(action: Action, arguments: dict[str, object]) -> ToolResult:
        try:
            result = context.patch_manager.revert_patch()
        except (
            PatchStateError,
            PatchApplyError,
            PatchRevertError,
            Exception,
        ) as exc:
            bounded_diag = bounded_diagnostic(exc, context.workspace.root)
            payload_data, recoverable, error_kind = build_bounded_patch_failure_payload(
                exc, error_kind="revert_failure", recoverable=False
            )
            if isinstance(exc, PatchStateError):
                raise ToolRejectedError(
                    bounded_diag,
                    safe_diagnostic=bounded_diag,
                    recoverable=False,
                    payload_data=payload_data,
                ) from exc
            raise ToolExecutionError(
                bounded_diag,
                safe_diagnostic=bounded_diag,
                recoverable=False,
                payload_data=payload_data,
            ) from exc
        changed_files = tuple(sorted(item.path for item in result.changed_files))
        reverted_index = max(0, context.patch_attempt_index - 1)
        context.observe(
            lambda: context.observability.patch_reverted(reverted_index)
        )
        # Capture the reverted (baseline) source while the changed-file paths
        # are still known, before the accepted candidate state is cleared.
        for path in changed_files:
            try:
                snapshot = capture_source_snapshot(
                    context.workspace.root, path, SourceSnapshotStage.REVERTED
                )
            except Exception:
                continue
            context.observe(
                lambda captured=snapshot: context.observability.source_snapshot(captured)
            )
        context.candidate_patch = ""
        context.patch_applied = False
        context.patch_changed_files = ()
        context.syntax_passed = None
        context.clear_validation_evidence()
        return _ok(
            {
                "reverted": True,
                "changed_files": list(changed_files),
            },
            "accepted candidate patch reverted from the disposable workspace",
        )

    def handle_syntax_check(action: Action, arguments: dict[str, object]) -> ToolResult:
        try:
            result = context.patch_manager.syntax_check()
        except (
            PatchStateError,
            PatchApplyError,
            PatchRevertError,
            Exception,
        ) as exc:
            bounded_diag = bounded_diagnostic(exc, context.workspace.root)
            payload_data, recoverable, error_kind = build_bounded_patch_failure_payload(
                exc, error_kind="syntax_check_failure", recoverable=False
            )
            raise ToolExecutionError(
                bounded_diag,
                safe_diagnostic=bounded_diag,
                recoverable=False,
                payload_data=payload_data,
            ) from exc
        context.syntax_passed = bool(result.all_passed)
        return _ok(
            {
                "all_passed": bool(result.all_passed),
                "results": [item.to_mapping() for item in result.results],
            },
            "patched source syntax validated",
        )

    # -- bounded runtime evidence -----------------------------------------

    def create_pdb_session(workspace: TaskWorkspace) -> PdbSession:
        """Construct the probe session without bypassing injected containment."""

        probe = context.probe
        if (
            probe is not None
            and probe.exact_public_reproduction
            and context.pdb_session_factory is PdbSession
        ):
            return context.pdb_session_factory(
                workspace,
                startup_timeout=15.0,
                request_timeout=30.0,
                proof_pytest_dependencies=True,
            )
        return context.pdb_session_factory(workspace)

    def exact_pytest_bootstrap_failure(result: object) -> bool:
        """Recognize only the structured exact-probe bootstrap failure."""

        if context.probe is None or not context.probe.exact_public_reproduction:
            return False
        if not isinstance(result, Mapping):
            return False
        exception = result.get("exception")
        innermost = result.get("innermost_frame")
        return (
            isinstance(exception, Mapping)
            and exception.get("type") == "ModuleNotFoundError"
            and exception.get("message") == "Target raised ModuleNotFoundError: No module named 'pytest'"
            and isinstance(innermost, Mapping)
            and innermost.get("function") == PROBE_DRIVER_FUNCTION
        )

    def handle_start_pdb(action: Action, arguments: dict[str, object]) -> ToolResult:
        if pdb_policy is PdbPolicy.DISABLED:
            raise _safe_rejection("PDB access is disabled by evaluation policy")
        probe = context.probe
        if probe is None:
            raise _safe_rejection("no runtime probe is configured for this task")
        if context.pdb_session is not None:
            raise _safe_rejection("a PDB session is already active")
        if (
            interactive_debugger_controls
            and context.interactive_pdb_session_started
            and not probe.exact_public_reproduction
        ):
            raise _safe_rejection(
                "interactive debugger pilot permits one PDB session per case"
            )
        try:
            workspace = TaskWorkspace(str(probe.source_dir), parent_dir=str(probe.parent_dir))
        except WorkspaceError as exc:
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        context.pdb_workspace = workspace
        # Register the session before starting it so a failed start is still
        # stopped and its workspace removed by release_pdb().
        session = create_pdb_session(workspace)
        context.pdb_session = session
        breakpoint_line = (
            int(arguments["breakpoint_line"])
            if interactive_debugger_controls
            else probe.breakpoint_line
        )
        if breakpoint_line <= 0:
            context.release_pdb()
            raise _safe_rejection("breakpoint_line must be positive")
        try:
            session.start()
            context.pdb_session_started = True
            started = session.start_paused_target(probe.script, [breakpoint_line])
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            context.release_pdb()
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        if started.get("state") != "paused":
            context.release_pdb()
            raise ToolExecutionError(
                "runtime probe did not reach the declared breakpoint",
                safe_diagnostic="runtime probe did not reach the declared breakpoint",
            )
        if probe.exact_public_reproduction and (
            started.get("script") != probe.script
            or started.get("function") != probe.focus_function
            or started.get("line") != breakpoint_line
        ):
            context.release_pdb()
            raise _safe_rejection(
                "breakpoint_line must pause on an executable statement inside the target function"
            )
        if interactive_debugger_controls:
            # A semantically invalid breakpoint is rejected above and may be
            # corrected.  Record a valid production-frame pause. Non-proof
            # interactive pilots remain one-shot; exact-public proof may start
            # a fresh controller-budgeted cycle only after the prior target
            # exits and its session/workspace are released.
            context.interactive_pdb_session_started = True
        context.pdb_pause_generation = 1
        context.observe(
            lambda: context.observability.debugger_started(
                probe.script, [f"{probe.script}:{breakpoint_line}"]
            )
        )
        context.observe(
            lambda: context.observability.location_changed(
                started["script"], started["line"], started["function"], 1
            )
        )
        payload = {
            "state": "paused",
            "script": started["script"],
            "line": started["line"],
            "function": started["function"],
            "breakpoint_line": breakpoint_line,
        }
        if probe.exact_public_reproduction:
            production_path = Path(workspace.resolve_path(probe.script, must_exist=True))
            context.pdb_proof_contract = {
                "exact_reproduction": True,
                "task_id": context.task.task_id,
                "reproduction_argv": list(probe.reproduction_argv),
                "pytest_node": probe.reproduction_node,
                "workspace_id": opaque_workspace_id(workspace),
                "production_file": probe.script,
                "production_file_sha256": hashlib.sha256(
                    production_path.read_bytes()
                ).hexdigest(),
                "breakpoint_line": breakpoint_line,
                "production_frame": probe.focus_function,
            }
            payload = context.record_pdb_proof_observation(
                action, payload, proof=context.pdb_proof_contract
            )
        if not interactive_debugger_controls:
            payload["focus_function"] = probe.focus_function
        return _ok(
            payload,
            "runtime probe paused at the declared breakpoint",
        )

    def handle_stack_summary(action: Action, arguments: dict[str, object]) -> ToolResult:
        session = context.require_session("get_stack_summary")
        try:
            stack = session.get_stack_summary()
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        generation = stack.get("pause_generation")
        if type(generation) is not int:
            raise ToolExecutionError(
                "stack summary did not report a pause generation",
                safe_diagnostic="stack summary did not report a pause generation",
            )
        context.pdb_pause_generation = generation
        context.pdb_observation_names.append("get_stack_summary")
        stack_payload = _json_safe(dict(stack), "get_stack_summary")
        if context.pdb_proof_contract is not None:
            stack_payload = context.record_pdb_proof_observation(
                action, stack_payload, proof=context.pdb_proof_contract
            )
        context.observe(
            lambda: context.observability.stack_observed(dict(stack))
        )
        return _ok(stack_payload, "bounded stack summary collected")

    def handle_frame_locals(action: Action, arguments: dict[str, object]) -> ToolResult:
        session = context.require_session("get_frame_locals")
        try:
            result = session.get_frame_locals(
                int(arguments["frame_id"]), int(arguments["pause_generation"])
            )
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        context.pdb_observation_names.append("get_frame_locals")
        locals_payload = _json_safe(dict(result), "get_frame_locals")
        if context.pdb_proof_contract is not None:
            locals_payload = context.record_pdb_proof_observation(
                action, locals_payload, proof=context.pdb_proof_contract
            )
        context.observe(
            lambda: context.observability.locals_observed(dict(result))
        )
        return _ok(locals_payload, "bounded frame locals collected")

    def handle_safe_eval(action: Action, arguments: dict[str, object]) -> ToolResult:
        session = context.require_session("safe_eval_expression")
        try:
            result = session.safe_eval_expression(
                int(arguments["frame_id"]),
                int(arguments["pause_generation"]),
                str(arguments["expression"]),
            )
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        context.pdb_observation_names.append("safe_eval_expression")
        return _ok(
            _json_safe(dict(result), "safe_eval_expression"),
            "restricted runtime expression evaluated",
        )

    def handle_execution_control(
        action: Action,
        arguments: dict[str, object],
    ) -> ToolResult:
        action_name = ActionName(action.name)
        session = context.require_session(action.name)
        operation = {
            ActionName.CONTINUE_PDB_SESSION: session.continue_paused_target,
            ActionName.STEP_PDB_SESSION: session.step_paused_target,
            ActionName.NEXT_PDB_SESSION: session.next_paused_target,
        }[action_name]
        try:
            result = operation()
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        if result.get("state") == "paused":
            # Every step/next/continue pause increments the PDB worker's
            # pause generation; the demo context mirrors that authoritative
            # counter so location events stay truthful and monotonic.
            context.pdb_pause_generation = (context.pdb_pause_generation or 0) + 1
            context.observe(
                lambda: context.observability.location_changed(
                    result["script"],
                    result["line"],
                    result["function"],
                    context.pdb_pause_generation,
                )
            )
        context.pdb_observation_names.append(action.name)
        control_payload = _json_safe(dict(result), action.name)
        if context.pdb_proof_contract is not None:
            control_payload["operation"] = action.name
            control_payload = context.record_pdb_proof_observation(
                action, control_payload, proof=context.pdb_proof_contract
            )
        if result.get("state") != "paused":
            errors = context.release_pdb()
            if errors:
                diag = bounded_diagnostic(errors[0], context.workspace.root)
                raise ToolExecutionError(diag, safe_diagnostic=diag)
            control_payload["session_released"] = True
        return _ok(
            control_payload,
            f"debugger execution control completed: {action.name}",
        )

    def handle_stop_pdb(action: Action, arguments: dict[str, object]) -> ToolResult:
        if interactive_debugger_controls and context.pdb_session is None:
            raise _safe_rejection(
                "interactive debugger pilot stop requires an active PDB session"
            )
        started = context.pdb_session_started
        had_workspace = context.pdb_workspace is not None
        errors = context.release_pdb()
        if errors:
            diag = bounded_diagnostic(errors[0], context.workspace.root)
            raise ToolExecutionError(diag, safe_diagnostic=diag)
        return _ok(
            {
                "stopped": context.pdb_session is None,
                "session_started": started,
                "workspace_removed": had_workspace and context.pdb_workspace is None,
            },
            "PDB session stopped and its workspace released",
        )

    diagnosis_required = {
        "hypothesis_id": str,
        "statement": str,
        "target_file": str,
        "target_symbol": str,
        "confidence": str,
    }
    if context.probe is not None and context.probe.exact_public_reproduction:
        diagnosis_required.update({"evidence_refs": list, "observed_values": dict})

    tool_specs = [
        spec(ActionName.RUN_REPRODUCTION, _validator({"phase": str}), handle_run_reproduction),
        spec(ActionName.GET_FAILURE_TRACE, _validator({}), handle_get_failure_trace),
        spec(ActionName.RUN_REGRESSION_TESTS, _validator({}), handle_run_regression_tests),
        spec(ActionName.CLASSIFY_OUTCOME, _validator({}), handle_classify_outcome),
        spec(
            ActionName.FIND_FUNCTION,
            _validator({"name": str, "path": str}),
            handle_find_function,
        ),
        spec(
            ActionName.GET_SOURCE_WINDOW,
            _validator({"path": str, "line": int}, minimums={"line": 1}),
            handle_get_source_window,
        ),
        spec(
            ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS,
            _validator(
                diagnosis_required,
                enums={
                    "confidence": tuple(item.value for item in HypothesisConfidence)
                },
            ),
            handle_express_hypothesis,
        ),
        spec(ActionName.APPLY_PATCH, _validator({"patch": str}), handle_apply_patch),
        spec(ActionName.REVERT_PATCH, _validator({}), handle_revert_patch),
        spec(ActionName.SYNTAX_CHECK, _validator({}), handle_syntax_check),
        spec(
            ActionName.START_PDB_SESSION,
            _validator(
                {"breakpoint_line": int}
                if interactive_debugger_controls
                else {},
                minimums={"breakpoint_line": 1}
                if interactive_debugger_controls
                else None,
            ),
            handle_start_pdb,
        ),
        spec(ActionName.GET_STACK_SUMMARY, _validator({}), handle_stack_summary),
        spec(
            ActionName.GET_FRAME_LOCALS,
            _validator({"frame_id": int, "pause_generation": int}),
            handle_frame_locals,
        ),
        spec(
            ActionName.SAFE_EVAL_EXPRESSION,
            _validator({"frame_id": int, "pause_generation": int, "expression": str}),
            handle_safe_eval,
        ),
    ]
    if interactive_debugger_controls or (
        context.probe is not None and context.probe.exact_public_reproduction
    ):
        control_validator = _validator({})
        tool_specs.extend(
            [
                spec(
                    ActionName.CONTINUE_PDB_SESSION,
                    control_validator,
                    handle_execution_control,
                ),
                spec(ActionName.STEP_PDB_SESSION, control_validator, handle_execution_control),
                spec(ActionName.NEXT_PDB_SESSION, control_validator, handle_execution_control),
            ]
        )
    tool_specs.append(spec(ActionName.STOP_PDB_SESSION, _validator({}), handle_stop_pdb))
    return ToolRegistry(tuple(tool_specs))


__all__ = [
    "MAX_DIAGNOSTIC_CHARS",
    "EXACT_PROOF_SOURCE_WINDOW_RADIUS",
    "SOURCE_WINDOW_RADIUS",
    "DemoToolContext",
    "DemoToolError",
    "PdbProbe",
    "build_registry",
    "prepare_pdb_probe",
    "legal_reproduction_phases",
    "pytest_argv",
    "validation_classification_ready",
]
