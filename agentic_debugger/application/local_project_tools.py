"""The Local Project isolated tool context and controller tool registry.

This module owns the honest Local Project tool surface:

- ``_LocalToolContext`` — the one mutable session-owned execution context
  (isolated workspace view, capability gates, project-secret egress seal
  and redaction authority, bounded project-command execution, product
  PDB session open/release, patch candidate state, validation-evidence
  readiness, error recording);
- ``_build_local_registry`` — the deterministic controller tool registry
  construction (tool names, schemas, validation, bounded diagnostics)
  dispatching over that context.

Dependency rule: imports the input/task helpers from
:mod:`agentic_debugger.application.local_project_helpers`; imported by
:mod:`agentic_debugger.application.local_project_source`; never imports
the source (no cycles).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, List, Mapping, Optional

from agentic_debugger.agent.controller_policy import PdbPolicy
from agentic_debugger.agent.tool_registry import (
    ToolExecutionError,
    ToolRejectedError,
    ToolRegistry,
)
from agentic_debugger.application.local_project_helpers import (
    LocalProjectTask,
    _IsolatedWorkspace,
    _bounded,
    _run_command_bounded,
    _split_command,
)
from agentic_debugger.application.local_project import assert_path_inside_workspace
from agentic_debugger.application.source_snapshots import (
    SourceSnapshotStage,
    capture_source_snapshot,
)
from agentic_debugger.cancellation import CancellationError
from agentic_debugger.runtime.exceptions import PatchRevertError

# ---------------------------------------------------------------------------
# Honest local tool context (no DebugTask fabrications)
# ---------------------------------------------------------------------------

class _LocalToolContext:
    def __init__(self, *, isolated: Path, tracked: List[str], task: LocalProjectTask, probe: Optional[Any], observability: Any, command_environment: Mapping[str, str], pdb_worker_environment: Optional[Mapping[str, str]], executor: Optional[Any] = None, capabilities: Optional[Any] = None):
        from agentic_debugger.runtime.patcher import PatchManager
        self.isolated = isolated
        self.tracked = tracked
        self.task = task
        self.probe = probe
        self.observability = observability
        # V2-02 session execution authority: the fixed role environments
        # derived by the SessionLaunch (declarative project runtime), the
        # logical Executor seam over them, and the computed effective
        # capabilities.  Direct unit/harness construction may leave the
        # seam empty (legacy behavior through the explicit mappings);
        # the real worker/source path always sets it.
        self.command_environment = command_environment
        self.pdb_worker_environment = pdb_worker_environment
        self.executor = executor
        self.capabilities = capabilities
        # V2-02 project-secret egress seal: the session's ONE redaction
        # authority (derived by the SessionLaunch ExecutionEnvironment from
        # the same materialization as the role child environments).  Raw
        # materialized project-secret values are redacted here before any
        # product PDB response/observability payload crosses into the
        # controller/model/evidence domain.  Absent on legacy direct
        # harness construction (historical behavior preserved).
        self.secret_redaction = (
            executor.project_secret_redactor()
            if executor is not None
            and hasattr(executor, "project_secret_redactor")
            else None
        )
        self.workspace = _IsolatedWorkspace(isolated)
        self.patch_manager = PatchManager(self.workspace, list(tracked), ["tests", "task.json"])
        self.candidate_patch = ""
        self.patch_applied = False
        self.patch_changed_files: tuple[str, ...] = ()
        self.patch_attempt_index = 0
        self.tool_calls: list[str] = []
        self.tool_errors: list[dict[str, str]] = []
        self.baseline_failure_reproduced: Optional[bool] = None
        self.post_patch_f2p_passed: Optional[bool] = None
        self.regression_passed: Optional[bool] = None
        self.syntax_passed: Optional[bool] = None
        self.declared_localization: Optional[dict[str, str]] = None
        self.controller_outcome: Optional[str] = None
        self.pdb_session: Optional[Any] = None
        self.pdb_workspace: Optional[Any] = None
        self.pdb_pause_generation: Optional[int] = None
        self.pdb_observation_names: list[str] = []
        self.pdb_session_started = False
        self.interactive_pdb_session_started = False
        self.pdb_proof_contract: Optional[dict[str, Any]] = None
        self.pdb_proof_observations: dict[str, dict[str, Any]] = {}
        # initial snapshots
        if observability is not None:
            for rel in tracked[:3]:
                try:
                    assert_path_inside_workspace(isolated, rel)
                    snap=capture_source_snapshot(isolated, rel, SourceSnapshotStage.INITIAL)
                    observability.source_snapshot(snap)
                except Exception:
                    continue

    def observe(self, fn):  # type: ignore[no-untyped-def]
        if self.observability is None:
            return
        try:
            fn()
        except Exception:
            pass

    def redact_project_output(self, value):  # type: ignore[no-untyped-def]
        """Redact raw materialized project-secret values from one
        product-output structure (identity when the session carries no
        redaction authority).  One authority, never per-handler rules."""
        if self.secret_redaction is None:
            return value
        return self.secret_redaction.redact_structure(value)

    def safe_project_diagnostic(self, exc, workspace_root=None):  # type: ignore[no-untyped-def]
        """Product exception-diagnostic egress (redact BEFORE bounding).

        The FULL exception text crosses the one session redaction
        authority first — including application-created bounded-tail
        fragments (repair 11: the PDB worker marks its own bounded
        diagnostics, and Agentic Debugger-created cuts must never expose a
        raw secret fragment) — and only then takes the established
        ``MAX_DIAGNOSTIC_CHARS`` diagnostic bound.  Without a session
        redaction authority this is the historical ``bounded_diagnostic``
        exactly (legacy direct-harness behavior preserved).
        """
        if self.secret_redaction is None:
            from agentic_debugger.demo.tools import bounded_diagnostic
            return bounded_diagnostic(exc, workspace_root)
        from agentic_debugger.demo.tools import bounded_diagnostic_text
        try:
            text = f"{type(exc).__name__}: {exc}"
        except Exception:
            text = f"{type(exc).__name__}: <unprintable exception>"
        return bounded_diagnostic_text(
            self.secret_redaction.redact_bounded_text(text), workspace_root
        )

    def require_capability(self, capability):  # type: ignore[no-untyped-def]
        """Fail closed (tool-unavailable) when the session denies a capability.

        Direct harness construction without a computed authority keeps the
        historical ungated behavior; the real session path always carries
        the computed EffectiveSessionCapabilities and enforces them.
        """
        if self.capabilities is None:
            return
        try:
            self.capabilities.require(capability)
        except Exception as exc:
            raise ToolRejectedError(str(exc)) from exc

    def run_project_command(self, cmd: str, cwd: Path, timeout: float = 30.0, cancel_check=None):  # type: ignore[no-untyped-def]
        """Run one project command through the Executor seam when present."""
        from agentic_debugger.application.session_runtime import SessionCapability

        self.require_capability(SessionCapability.PROJECT_COMMAND)
        if self.executor is not None:
            from agentic_debugger.runtime.exceptions import CommandExecutionError

            start = time.monotonic()
            try:
                argv = _split_command(cmd)
            except ValueError as exc:
                return 127, "", f"parse failed: {exc}", time.monotonic() - start
            if not argv:
                return 127, "", "empty", time.monotonic() - start
            try:
                result = self.executor.run_project_command(
                    argv, _IsolatedWorkspace(cwd), timeout,
                    cancel_check=cancel_check,
                )
            except CommandExecutionError as exc:
                return 127, "", f"launch failed: {exc}", time.monotonic() - start
            except CancellationError:
                raise
            if result.exit_code is not None:
                exit_code = result.exit_code
            elif result.timed_out:
                exit_code = 124
            else:
                exit_code = 127
            err = result.stderr or ""
            if result.timed_out:
                err = (err + f" timed out {timeout}s").strip()
            return (
                exit_code,
                _bounded(result.stdout or ""),
                _bounded(err),
                time.monotonic() - start,
            )
        return _run_command_bounded(
            cmd, cwd, timeout, cancel_check=cancel_check,
            environment=self.command_environment,
        )

    def open_product_pdb(self, workspace):  # type: ignore[no-untyped-def]
        """Create one product PDB session through the Executor seam when present."""
        from agentic_debugger.application.session_runtime import SessionCapability
        from agentic_debugger.runtime.pdb_session import PdbSession

        self.require_capability(SessionCapability.PDB)
        if self.executor is not None:
            probe = self.probe
            if probe is not None and getattr(probe, "exact_public_reproduction", False):
                return self.executor.open_product_pdb(
                    workspace,
                    startup_timeout=15.0,
                    request_timeout=30.0,
                    proof_pytest_dependencies=True,
                )
            return self.executor.open_product_pdb(workspace)
        if self.probe is not None and getattr(self.probe, "exact_public_reproduction", False) and self.__dict__.get("pdb_session_factory", PdbSession) is PdbSession:
            return PdbSession(workspace, startup_timeout=15.0, request_timeout=30.0, proof_pytest_dependencies=True, worker_environment=self.pdb_worker_environment)
        return PdbSession(workspace, startup_timeout=15.0, request_timeout=60.0, worker_environment=self.pdb_worker_environment)

    def record_error(self, action: str, exc: BaseException) -> None:
        # Egress seal: tool_errors is a product surface; project-domain
        # text carried by an exception (e.g. a PDB worker diagnostic) is
        # redacted through the same session authority BEFORE the
        # diagnostic bound, never after it.
        try:
            diag = self.safe_project_diagnostic(exc, self.workspace.root)
        except Exception:
            diag = str(exc)[:400]
        self.tool_errors.append({"action": action, "diagnostic": diag})

    def require_session(self, action: str):  # type: ignore[no-untyped-def]
        if self.pdb_session is None:
            raise ToolRejectedError(f"{action} requires an active PDB session")
        return self.pdb_session

    def _capture_changed_source(self, stage):  # type: ignore[no-untyped-def]
        for path in self.patch_changed_files:
            try:
                snapshot = capture_source_snapshot(self.workspace.root, path, stage)
            except Exception:
                continue
            self.observe(lambda captured=snapshot: self.observability.source_snapshot(captured))

    def validation_evidence_ready(self) -> bool:
        return self.post_patch_f2p_passed is not None and self.regression_passed is not None

    def clear_validation_evidence(self) -> None:
        self.post_patch_f2p_passed = None
        self.regression_passed = None
        self.controller_outcome = None

    def release_pdb(self):  # type: ignore[no-untyped-def]
        errors: list[BaseException] = []
        session = self.pdb_session
        if session is not None:
            try:
                session.stop()
            except BaseException as exc:
                errors.append(exc)
            else:
                self.pdb_session = None
        workspace = self.pdb_workspace
        if workspace is not None:
            try:
                workspace.cleanup()
                if os.path.exists(workspace.root):
                    raise RuntimeError("PDB workspace root remains after cleanup")
            except BaseException as exc:
                errors.append(exc)
            else:
                self.pdb_workspace = None
        return errors

def _build_local_registry(context: _LocalToolContext, *, pdb_policy: Any = None, interactive_debugger_controls: bool = False) -> ToolRegistry:
    from agentic_debugger.agent.controller_policy import ActionName, HypothesisConfidence
    from agentic_debugger.runtime.pdb_session import PdbSession
    from agentic_debugger.runtime.workspace import TaskWorkspace
    from agentic_debugger.skills.file_skills import get_source_window
    from agentic_debugger.skills.search_skills import find_function
    from agentic_debugger.demo.tools import bounded_diagnostic, DemoToolError, _json_safe, _safe_rejection, MAX_DIAGNOSTIC_CHARS
    from agentic_debugger.evaluation.outcome_taxonomy import classify_outcome
    from agentic_debugger.runtime.exceptions import PatchApplyError, PatchAuthorizationError, PatchStateError, PatchValidationError, PdbSessionError, PdbSessionTimeoutError, SourceInspectionError, SourceParseError, WorkspaceError

    def spec(name, validator, handler):  # type: ignore[no-untyped-def]
        def guarded(action, arguments):  # type: ignore[no-untyped-def]
            context.tool_calls.append(action.name)
            try:
                return handler(action, arguments)
            except BaseException as exc:
                context.record_error(action.name, exc)
                raise
        from agentic_debugger.agent.tool_registry import ToolSpec
        return ToolSpec(name, validator, guarded, version="demo-1", argument_contract=getattr(validator, "argument_contract", {}))

    def _validator(required, optional=None, *, enums=None, minimums=None):  # type: ignore[no-untyped-def]
        optional = optional or {}
        enums = enums or {}
        minimums = minimums or {}
        known = set(required) | set(optional)
        def validate(arguments):  # type: ignore[no-untyped-def]
            if type(arguments) is not dict:
                raise ToolRejectedError("arguments must be a mapping")
            unknown = sorted(set(arguments) - known)
            if unknown:
                raise ToolRejectedError(f"unknown argument: {unknown[0]}")
            missing = sorted(set(required) - set(arguments))
            if missing:
                raise ToolRejectedError(f"missing argument: {missing[0]}")
            for name2, expected in {**required, **optional}.items():
                if name2 not in arguments:
                    continue
                value = arguments[name2]
                if type(value) is not expected:
                    raise ToolRejectedError(f"argument {name2} has the wrong type")
                if expected is str and not value:
                    raise ToolRejectedError(f"argument {name2} must be non-empty")
                if expected is int and value < 0:
                    raise ToolRejectedError(f"argument {name2} must be non-negative")
                if expected is int and name2 in minimums and value < minimums[name2]:
                    raise ToolRejectedError(f"argument {name2} must be at least {minimums[name2]}")
                if name2 in enums and value not in enums[name2]:
                    raise ToolRejectedError(f"argument {name2} has an unsupported value")
            return dict(arguments)
        def type_name(expected):  # type: ignore[no-untyped-def]
            return {str: "string", int: "integer", bool: "boolean"}.get(expected, expected.__name__)
        properties = {}
        for name2, expected in {**required, **optional}.items():
            constraint = {"type": type_name(expected)}
            if expected is str:
                constraint["min_length"] = 1
            if expected is int:
                constraint["minimum"] = minimums.get(name2, 0)
            if name2 in enums:
                constraint["enum"] = list(enums[name2])
            properties[name2] = constraint
        validate.argument_contract = {"required": list(required), "properties": properties, "additional_properties": False}  # type: ignore[attr-defined]
        return validate

    def _ok(payload, summary):  # type: ignore[no-untyped-def]
        from agentic_debugger.events.schema import ObservationStatus
        from agentic_debugger.agent.tool_registry import ToolResult
        return ToolResult(ObservationStatus.OK, payload, summary)

    # -- reproduction -------------------------------------------------------
    def handle_run_reproduction(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.demo.tools import legal_reproduction_phases
        phase = arguments["phase"]
        if phase not in legal_reproduction_phases(action.state):
            raise ToolRejectedError("phase must be baseline or post_patch")
        if context.task.reproduction_command is None:
            raise ToolExecutionError("no reproduction command configured for this project")
        # Execute the honest reproduction command in the isolated workspace
        # through the session Executor seam (fixed role environment,
        # capability-gated).
        exit_code, out, err, elapsed = context.run_project_command(context.task.reproduction_command, Path(context.workspace.root), timeout=30.0)
        passed = (exit_code == 0)
        # Baseline truth comes from the command itself: a non-zero exit is
        # the observed failure; a zero exit means the reported bug did NOT
        # reproduce and must not satisfy any downstream gate (the user's
        # bug report is not reproduction proof).  Post-patch records
        # ``passed`` (exit==0) and reports no failure reproduction.
        baseline_reproduced = not passed
        failure_output = _bounded((out or "") + (err or ""), 4000)
        # Keep payload honest: failure_reproduced reflects the observed
        # command result, passed reflects the exit code.
        payload = {
            "phase": phase,
            "exit_code": exit_code,
            "passed": bool(passed),
            "failure_reproduced": bool(baseline_reproduced) if phase == "baseline" else False,
            "failure_output": failure_output,
        }
        if phase == "baseline":
            context.baseline_failure_reproduced = bool(baseline_reproduced)
            summary = "baseline reproduction executed"
        else:
            context.post_patch_f2p_passed = bool(passed)
            summary = "post-patch reproduction executed"
        return _ok(payload, summary)

    def handle_run_regression_tests(action, arguments):  # type: ignore[no-untyped-def]
        # Honest verification: run verification_command if present, else no regression
        if context.task.verification_command is None:
            # No verification configured -> conservatively mark regression as passed for controller flow;
            # external verifier will still mark UNRESOLVED.
            context.regression_passed = True
            return _ok({"exit_code": 0, "all_passed": True, "note": "no verification command"}, "no verification command; regression considered passed for controller")
        exit_code, out, err, elapsed = context.run_project_command(context.task.verification_command, Path(context.workspace.root), timeout=30.0)
        all_passed = (exit_code == 0)
        context.regression_passed = all_passed
        return _ok({"exit_code": exit_code, "all_passed": all_passed}, "verification command executed")

    def handle_classify_outcome(action, arguments):  # type: ignore[no-untyped-def]
        if not context.validation_evidence_ready():
            raise ToolExecutionError("validation evidence is incomplete")
        f2p = [context.post_patch_f2p_passed]
        p2p = [context.regression_passed]
        outcome = classify_outcome(f2p, p2p)
        context.controller_outcome = outcome.value
        return _ok({"outcome": outcome.value, "f2p_passed": f2p, "p2p_passed": p2p, "evidence_scope": "controller_validation"}, "controller validation outcome classified")

    def handle_find_function(action, arguments):  # type: ignore[no-untyped-def]
        try:
            match = find_function(context.workspace, arguments["name"], arguments["path"])
        except (SourceInspectionError, SourceParseError, WorkspaceError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        if match is None:
            raise ToolExecutionError("declared symbol was not found in the declared file")
        return _ok(_json_safe(match.to_mapping(), "find_function"), "declared symbol located")

    def handle_get_source_window(action, arguments):  # type: ignore[no-untyped-def]
        line = arguments["line"]
        if line < 1:
            raise ToolRejectedError("line must be positive")
        try:
            window = get_source_window(context.workspace, arguments["path"], line, 6)
        except (SourceInspectionError, WorkspaceError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        return _ok(_json_safe(window.to_mapping(), "get_source_window"), "source window retrieved")

    def handle_express_hypothesis(action, arguments):  # type: ignore[no-untyped-def]
        declared = {
            "hypothesis_id": str(arguments["hypothesis_id"]),
            "statement": str(arguments["statement"]),
            "target_file": str(arguments["target_file"]),
            "target_symbol": str(arguments["target_symbol"]),
            "confidence": str(arguments["confidence"]),
        }
        context.declared_localization = {"file_path": declared["target_file"], "symbol": declared["target_symbol"]}
        context.observe(lambda: context.observability.diagnosis_recorded(text=declared["statement"], file_path=declared["target_file"], symbol=declared["target_symbol"], confidence=declared["confidence"]))
        return _ok(declared, "root-cause hypothesis recorded")

    def handle_apply_patch(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.application.session_runtime import SessionCapability
        context.require_capability(SessionCapability.PATCH)
        context.clear_validation_evidence()
        diff = arguments["patch"]
        if context.patch_manager.has_active_patch and context.candidate_patch == diff:
            raise ToolRejectedError("the candidate patch is already active")
        attempt_index = context.patch_attempt_index
        context.patch_attempt_index += 1
        import hashlib as _hashlib
        patch_sha256 = _hashlib.sha256(diff.encode("utf-8")).hexdigest()
        reverted_previous = False
        if context.patch_manager.has_active_patch:
            try:
                context.patch_manager.revert_patch()
            except (
                PatchStateError,
                PatchApplyError,
                PatchRevertError,
                Exception,
            ) as exc:
                from agentic_debugger.runtime.patcher import build_bounded_patch_failure_payload
                bounded_diag = bounded_diagnostic(exc, context.workspace.root)
                payload_data, recoverable, error_kind = build_bounded_patch_failure_payload(
                    exc, error_kind="revert_failure", recoverable=False
                )
                context.observe(lambda: context.observability.patch_apply_failed(attempt_index, bounded_diag))
                raise ToolExecutionError(
                    bounded_diag,
                    safe_diagnostic=bounded_diag,
                    recoverable=False,
                    payload_data=payload_data,
                ) from exc
            reverted_previous = True
            context.observe(lambda: context.observability.patch_reverted(attempt_index - 1))
            context._capture_changed_source(SourceSnapshotStage.REVERTED)
        context.observe(lambda: context.observability.patch_proposed(attempt_index, patch_sha256, patch_text=diff))
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
            from agentic_debugger.runtime.patcher import build_bounded_patch_failure_payload
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
        context.candidate_patch = diff
        context.patch_applied = bool(result.success)
        context.patch_changed_files = tuple(sorted(item.path for item in result.changed_files))
        context.observe(lambda: context.observability.patch_applied(attempt_index, context.patch_changed_files, None))
        context._capture_changed_source(SourceSnapshotStage.APPLIED)
        payload = {
            "applied": bool(result.success),
            "changed_files": list(context.patch_changed_files),
            "hunk_count": result.hunk_count,
            "patch_sha256": _hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            "after_sha256": {key: result.after_sha256[key] for key in sorted(result.after_sha256)},
            "hunk_adjustments": [list(item) for item in result.hunk_adjustments],
            "reverted_previous": reverted_previous,
        }
        return _ok(_json_safe(payload, "apply_patch"), "candidate patch applied to the disposable workspace")

    def handle_revert_patch(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.application.session_runtime import SessionCapability
        context.require_capability(SessionCapability.PATCH)
        try:
            result = context.patch_manager.revert_patch()
        except (
            PatchStateError,
            PatchApplyError,
            PatchRevertError,
            Exception,
        ) as exc:
            from agentic_debugger.runtime.patcher import build_bounded_patch_failure_payload
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
        context.observe(lambda: context.observability.patch_reverted(reverted_index))
        for path in changed_files:
            try:
                snapshot = capture_source_snapshot(context.workspace.root, path, SourceSnapshotStage.REVERTED)
            except Exception:
                continue
            context.observe(lambda captured=snapshot: context.observability.source_snapshot(captured))
        context.candidate_patch = ""
        context.patch_applied = False
        context.patch_changed_files = ()
        context.syntax_passed = None
        context.clear_validation_evidence()
        return _ok({"reverted": True, "changed_files": list(changed_files)}, "accepted candidate patch reverted from the disposable workspace")

    def handle_syntax_check(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.application.session_runtime import SessionCapability
        context.require_capability(SessionCapability.PATCH)
        try:
            result = context.patch_manager.syntax_check()
        except (
            PatchStateError,
            PatchApplyError,
            PatchRevertError,
            Exception,
        ) as exc:
            from agentic_debugger.runtime.patcher import build_bounded_patch_failure_payload
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
        return _ok({"all_passed": bool(result.all_passed), "results": [item.to_mapping() for item in result.results]}, "patched source syntax validated")

    # -- PDB (honest, targets resolved repro script) -----------------------
    def create_pdb_session(workspace):  # type: ignore[no-untyped-def]
        # V2-02: the ordinary product PDB worker is created through the
        # session Executor seam (fixed PRODUCT_PDB role environment,
        # PDB-capability-gated); Windows venv identity still travels
        # through build_worker_env inside PdbSession.
        return context.open_product_pdb(workspace)

    def handle_start_pdb(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.application.session_runtime import SessionCapability
        # Session capability first (the computed session authority), then
        # the existing task PDB policy: both fail closed as tool-unavailable.
        context.require_capability(SessionCapability.PDB)
        if pdb_policy is PdbPolicy.DISABLED:
            raise ToolRejectedError("PDB access is disabled by evaluation policy")
        probe = context.probe
        if probe is None:
            raise ToolRejectedError("no runtime probe is configured for this task")
        if context.pdb_session is not None:
            raise ToolRejectedError("a PDB session is already active")
        if interactive_debugger_controls and context.interactive_pdb_session_started and not getattr(probe, "exact_public_reproduction", False):
            raise ToolRejectedError("interactive debugger pilot permits one PDB session per case")
        try:
            workspace = TaskWorkspace(str(probe.source_dir), parent_dir=str(probe.parent_dir))
        except WorkspaceError as exc:
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        context.pdb_workspace = workspace
        session = create_pdb_session(workspace)
        context.pdb_session = session
        breakpoint_line = int(arguments["breakpoint_line"]) if interactive_debugger_controls else probe.breakpoint_line
        if breakpoint_line <= 0:
            context.release_pdb()
            raise ToolRejectedError("breakpoint_line must be positive")
        try:
            session.start()
            context.pdb_session_started = True
            started = session.start_paused_target(probe.script, [breakpoint_line])
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = context.safe_project_diagnostic(exc)
            context.release_pdb()
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        if started.get("state") != "paused":
            context.release_pdb()
            raise ToolExecutionError("runtime probe did not reach the declared breakpoint", safe_diagnostic="runtime probe did not reach the declared breakpoint")
        # Egress seal: one sanitized object feeds BOTH the observability
        # event and the model payload (never raw-then-sanitized).
        started = context.redact_project_output(started)
        if interactive_debugger_controls:
            context.interactive_pdb_session_started = True
        context.pdb_pause_generation = 1
        context.observe(lambda: context.observability.debugger_started(probe.script, [f"{probe.script}:{breakpoint_line}"]))
        context.observe(lambda: context.observability.location_changed(started["script"], started["line"], started["function"], 1))
        payload = {"state": "paused", "script": started["script"], "line": started["line"], "function": started["function"], "breakpoint_line": breakpoint_line}
        if not interactive_debugger_controls:
            payload["focus_function"] = probe.focus_function
        return _ok(payload, "runtime probe paused at the declared breakpoint")

    def handle_stack_summary(action, arguments):  # type: ignore[no-untyped-def]
        session = context.require_session("get_stack_summary")
        try:
            stack = session.get_stack_summary()
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = context.safe_project_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        # Egress seal: sanitize once; the SAME object is observed and returned.
        stack = context.redact_project_output(stack)
        generation = stack.get("pause_generation")
        if type(generation) is not int:
            raise ToolExecutionError("stack summary did not report a pause generation", safe_diagnostic="stack summary did not report a pause generation")
        context.pdb_pause_generation = generation
        context.pdb_observation_names.append("get_stack_summary")
        context.observe(lambda: context.observability.stack_observed(dict(stack)))
        return _ok(_json_safe(dict(stack), "get_stack_summary"), "bounded stack summary collected")

    def handle_frame_locals(action, arguments):  # type: ignore[no-untyped-def]
        session = context.require_session("get_frame_locals")
        try:
            result = session.get_frame_locals(int(arguments["frame_id"]), int(arguments["pause_generation"]))
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = context.safe_project_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        # Egress seal: sanitize once; the SAME object is observed and returned.
        result = context.redact_project_output(result)
        context.pdb_observation_names.append("get_frame_locals")
        context.observe(lambda: context.observability.locals_observed(dict(result)))
        return _ok(_json_safe(dict(result), "get_frame_locals"), "bounded frame locals collected")

    def handle_safe_eval(action, arguments):  # type: ignore[no-untyped-def]
        session = context.require_session("safe_eval_expression")
        try:
            result = session.safe_eval_expression(int(arguments["frame_id"]), int(arguments["pause_generation"]), str(arguments["expression"]))
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = context.safe_project_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        # Egress seal: evaluated runtime values are project-domain output.
        result = context.redact_project_output(result)
        context.pdb_observation_names.append("safe_eval_expression")
        return _ok(_json_safe(dict(result), "safe_eval_expression"), "restricted runtime expression evaluated")

    def handle_execution_control(action, arguments):  # type: ignore[no-untyped-def]
        from agentic_debugger.agent.controller_policy import ActionName
        session = context.require_session(action.name)
        operation = {ActionName.CONTINUE_PDB_SESSION: session.continue_paused_target, ActionName.STEP_PDB_SESSION: session.step_paused_target, ActionName.NEXT_PDB_SESSION: session.next_paused_target}[ActionName(action.name)]
        try:
            result = operation()
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            diag = context.safe_project_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        # Egress seal: one sanitized object for observation and payload.
        result = context.redact_project_output(result)
        if result.get("state") == "paused":
            context.pdb_pause_generation = (context.pdb_pause_generation or 0) + 1
            context.observe(lambda: context.observability.location_changed(result["script"], result["line"], result["function"], context.pdb_pause_generation))
        context.pdb_observation_names.append(action.name)
        control_payload = _json_safe(dict(result), action.name)
        if result.get("state") != "paused":
            errors = context.release_pdb()
            if errors:
                diag = context.safe_project_diagnostic(
                    errors[0], context.workspace.root
                )
                raise ToolExecutionError(diag, safe_diagnostic=diag)
            control_payload["session_released"] = True
        return _ok(control_payload, f"debugger execution control completed: {action.name}")

    def handle_stop_pdb(action, arguments):  # type: ignore[no-untyped-def]
        started = context.pdb_session_started
        had_workspace = context.pdb_workspace is not None
        errors = context.release_pdb()
        if errors:
            diag = context.safe_project_diagnostic(
                errors[0], context.workspace.root
            )
            raise ToolExecutionError(diag, safe_diagnostic=diag)
        return _ok({"stopped": context.pdb_session is None, "session_started": started, "workspace_removed": had_workspace and context.pdb_workspace is None}, "PDB session stopped and its workspace released")

    diagnosis_required = {"hypothesis_id": str, "statement": str, "target_file": str, "target_symbol": str, "confidence": str}
    tool_specs = [
        spec(ActionName.RUN_REPRODUCTION, _validator({"phase": str}), handle_run_reproduction),
        spec(ActionName.RUN_REGRESSION_TESTS, _validator({}), handle_run_regression_tests),
        spec(ActionName.CLASSIFY_OUTCOME, _validator({}), handle_classify_outcome),
        spec(ActionName.FIND_FUNCTION, _validator({"name": str, "path": str}), handle_find_function),
        spec(ActionName.GET_SOURCE_WINDOW, _validator({"path": str, "line": int}, minimums={"line": 1}), handle_get_source_window),
        spec(ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS, _validator(diagnosis_required, enums={"confidence": tuple(item.value for item in HypothesisConfidence)}), handle_express_hypothesis),
        spec(ActionName.APPLY_PATCH, _validator({"patch": str}), handle_apply_patch),
        spec(ActionName.REVERT_PATCH, _validator({}), handle_revert_patch),
        spec(ActionName.SYNTAX_CHECK, _validator({}), handle_syntax_check),
        spec(ActionName.START_PDB_SESSION, _validator({"breakpoint_line": int} if interactive_debugger_controls else {}, minimums={"breakpoint_line": 1} if interactive_debugger_controls else None), handle_start_pdb),
        spec(ActionName.GET_STACK_SUMMARY, _validator({}), handle_stack_summary),
        spec(ActionName.GET_FRAME_LOCALS, _validator({"frame_id": int, "pause_generation": int}), handle_frame_locals),
        spec(ActionName.SAFE_EVAL_EXPRESSION, _validator({"frame_id": int, "pause_generation": int, "expression": str}), handle_safe_eval),
    ]
    if interactive_debugger_controls:
        control_validator = _validator({})
        tool_specs.extend([
            spec(ActionName.CONTINUE_PDB_SESSION, control_validator, handle_execution_control),
            spec(ActionName.STEP_PDB_SESSION, control_validator, handle_execution_control),
            spec(ActionName.NEXT_PDB_SESSION, control_validator, handle_execution_control),
        ])
    tool_specs.append(spec(ActionName.STOP_PDB_SESSION, _validator({}), handle_stop_pdb))
    return ToolRegistry(tuple(tool_specs))

