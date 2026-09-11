"""The mutable demo tool execution context.

:class:`DemoToolContext` owns the demonstration's session-scoped mutable
state: the task/workspace/patch inputs, the PDB probe and session
factory seam, the test runner and patch manager, verifier feedback
history, exact-PDB proof observations, validation evidence readiness,
observability projection (best-effort, never behavior-changing), PDB
lifecycle release, and bounded error recording.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from agentic_debugger.agent.tool_registry import ToolResult
from agentic_debugger.application.source_snapshots import (
    SourceSnapshotStage,
    capture_source_snapshot,
)
from agentic_debugger.demo.diagnostics import (
    DemoToolError,
    MAX_RAW_FAILURE_OUTPUT_CHARS,
    _json_safe,
    _observation_id_for_action,
    _safe_rejection,
    bounded_diagnostic,
    task_target_module_path,
    validation_classification_ready,
)
from agentic_debugger.demo.pdb_probe import PdbProbe
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.events.schema import Action
from agentic_debugger.runtime.execution import VerifiedExecutionContext
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.patcher import PatchManager
from agentic_debugger.runtime.test_runner import TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace

class DemoToolContext:
    """Mutable execution state shared by the demonstration tool handlers."""

    def __init__(
        self,
        *,
        task: DebugTask,
        workspace: TaskWorkspace,
        patch: str,
        probe: Optional[PdbProbe],
        execution_context: Optional[VerifiedExecutionContext] = None,
        pdb_session_factory: Callable[[TaskWorkspace], PdbSession] = PdbSession,
        verifier_feedback_fn: Optional[Callable[[DebugTask, str], dict[str, Any]]] = None,
        observability: Any = None,
        official_patch_compatibility: bool = False,
    ) -> None:
        self.task = task
        self.workspace = workspace
        self.patch = patch
        self.candidate_patch = ""
        self.probe = probe
        self.execution_context = execution_context
        # Defaults to plain host-local ``PdbSession`` construction, preserving
        # the accepted demo/live behavior exactly. A caller that must run the
        # debugger over external/untrusted code (e.g. a contained WSL/
        # Bubblewrap boundary) injects a factory that builds a session bound
        # to that boundary instead -- the host-local path is never implied.
        self.pdb_session_factory = pdb_session_factory
        # Optional real-verifier feedback callback: when set, every accepted
        # candidate patch is evaluated by the independent EvaluationVerifier
        # and a bounded feedback record is attached to the apply_patch
        # observation.  The callback returns a JSON-compatible mapping or
        # raises (the error is bounded into the observation, never a crash).
        self.verifier_feedback_fn = verifier_feedback_fn
        # Historical record of every verifier-feedback run (attempt order).
        self.verifier_feedback_history: list[dict[str, Any]] = []
        self.test_runner = TestRunner(workspace, execution_context=execution_context)
        self.patch_manager = PatchManager(
            workspace,
            list(task.constraints.allowed_write_paths),
            list(task.constraints.denied_write_paths),
            official_patch_compatibility=official_patch_compatibility,
        )
        # Optional Task-4 observability producer (``SessionObservability`` or
        # an object with the same emit methods).  When set, the tool handlers
        # project real debugger/patch/source/diagnosis facts into validated
        # application events.  Observability is strictly observational: a
        # failure is swallowed and never changes a tool result or the demo.
        self.observability = observability
        # Patch attempts are counted per apply_patch invocation; rejected,
        # apply-failed, and reverted attempts share the same attempt index.
        self.patch_attempt_index = 0
        # Best-effort initial source snapshot of the pristine task source
        # (the disposable workspace copy is pristine at construction).
        if observability is not None:
            self._capture_initial_source()

        self.tool_calls: list[str] = []
        self.tool_errors: list[dict[str, str]] = []
        self.baseline_failure_reproduced: Optional[bool] = None
        self.post_patch_f2p_passed: Optional[bool] = None
        self.regression_passed: Optional[bool] = None
        self.patch_applied = False
        self.patch_changed_files: tuple[str, ...] = ()
        self.syntax_passed: Optional[bool] = None
        self.declared_localization: Optional[dict[str, str]] = None
        self.controller_outcome: Optional[str] = None

        self.pdb_session: Optional[PdbSession] = None
        self.pdb_workspace: Optional[TaskWorkspace] = None
        self.pdb_pause_generation: Optional[int] = None
        self.pdb_observation_names: list[str] = []
        self.pdb_session_started = False
        self.interactive_pdb_session_started = False
        self.pdb_proof_contract: Optional[dict[str, Any]] = None
        self.pdb_proof_observations: dict[str, dict[str, Any]] = {}

    def record_pdb_proof_observation(
        self, action: Action, payload: dict[str, Any], *, proof: dict[str, Any]
    ) -> dict[str, Any]:
        """Attach exact-runtime identity to a model-visible tool result."""

        if self.probe is None or not self.probe.exact_public_reproduction:
            return payload
        observation_id = _observation_id_for_action(action)
        detached = _json_safe(payload, action.name)
        detached["proof"] = _json_safe(proof, "pdb proof")
        self.pdb_proof_observations[observation_id] = detached
        return detached

    def validate_bound_diagnosis(
        self, action: Action, evidence_refs: object, observed_values: object
    ) -> dict[str, Any]:
        if self.probe is None or not self.probe.exact_public_reproduction:
            return {}
        if type(evidence_refs) is not list or not evidence_refs:
            raise _safe_rejection("exact PDB diagnosis requires evidence_refs")
        if any(type(item) is not str or not item for item in evidence_refs):
            raise _safe_rejection("evidence_refs must contain observation ids")
        if type(observed_values) is not dict or not observed_values:
            raise _safe_rejection("exact PDB diagnosis requires observed_values")
        if self.pdb_proof_contract is None:
            raise _safe_rejection("exact PDB evidence is not available")
        referenced = [self.pdb_proof_observations.get(item) for item in evidence_refs]
        if any(item is None for item in referenced):
            raise _safe_rejection("diagnosis references a stale or nonexistent observation")
        locals_payload = next(
            (item for item in referenced if item and item.get("locals") is not None),
            None,
        )
        if locals_payload is None:
            raise _safe_rejection("diagnosis must reference frame locals")
        locals_by_name = {
            item.get("name"): item.get("value")
            for item in locals_payload.get("locals", [])
            if type(item) is dict and type(item.get("name")) is str
        }
        if any(name not in locals_by_name or locals_by_name[name] != value for name, value in observed_values.items()):
            raise _safe_rejection("diagnosis runtime value is absent from referenced locals")
        step_seen = any(
            item and item.get("proof") == self.pdb_proof_contract
            and item.get("state") == "paused"
            for item in referenced
        )
        if not step_seen:
            raise _safe_rejection("diagnosis must reference a paused step or next observation")
        return {
            "evidence_refs": list(evidence_refs),
            "observed_values": _json_safe(observed_values, "observed_values"),
            "proof_contract": _json_safe(self.pdb_proof_contract, "proof contract"),
        }

    def validation_evidence_ready(self) -> bool:
        """Return whether classify_outcome has both required evidence values."""

        return validation_classification_ready(
            self.post_patch_f2p_passed, self.regression_passed
        )

    def clear_validation_evidence(self) -> None:
        """Forget controller-validation evidence after the candidate changes."""

        self.post_patch_f2p_passed = None
        self.regression_passed = None
        self.controller_outcome = None

    # -- observability helpers ---------------------------------------------

    def observe(self, fn: Callable[[], None]) -> None:
        """Run one observability projection; failure never changes execution.

        Mirrors the controller's observer rule: an ordinary ``Exception`` in
        observability is swallowed and never alters a tool decision, result,
        budget, or cleanup.  ``BaseException`` propagates.
        """
        if self.observability is None:
            return
        try:
            fn()
        except Exception:
            pass

    def _capture_initial_source(self) -> None:
        """Emit one bounded initial source snapshot for the task target.

        Best-effort: any failure is swallowed (observability never changes
        the demonstration), and only the declared production module path is
        captured -- never tests, oracles, or unrelated files.
        """
        try:
            module_path = task_target_module_path(self.task)
            snapshot = capture_source_snapshot(
                self.workspace.root, module_path, SourceSnapshotStage.INITIAL
            )
        except Exception:
            return
        self.observe(lambda: self.observability.source_snapshot(snapshot))

    def _capture_changed_source(self, stage: SourceSnapshotStage) -> None:
        """Emit one bounded source snapshot per currently changed file."""
        for path in self.patch_changed_files:
            try:
                snapshot = capture_source_snapshot(
                    self.workspace.root, path, stage
                )
            except Exception:
                continue
            self.observe(
                lambda captured=snapshot: self.observability.source_snapshot(captured)
            )

    # -- lifecycle ---------------------------------------------------------

    def release_pdb(self) -> list[BaseException]:
        """Stop the session and delete its workspace; never raise."""

        errors: list[BaseException] = []
        session = self.pdb_session
        if session is not None:
            try:
                session.stop()
            except BaseException as exc:  # noqa: BLE001 - cleanup must continue
                # Keep the handle so an outer cleanup pass can retry rather
                # than losing the only reference to a live worker process.
                errors.append(exc)
            else:
                self.pdb_session = None
        workspace = self.pdb_workspace
        if workspace is not None:
            try:
                workspace.cleanup()
                if os.path.exists(workspace.root):
                    raise DemoToolError("PDB workspace root remains after cleanup")
            except BaseException as exc:  # noqa: BLE001 - cleanup must continue
                errors.append(exc)
            else:
                self.pdb_workspace = None
        return errors

    def record_error(self, action: str, exc: BaseException) -> None:
        """Retain a bounded, path-normalized diagnostic naming what failed."""

        self.tool_errors.append(
            {"action": action, "diagnostic": bounded_diagnostic(exc, self.workspace.root)}
        )

    # -- helpers -----------------------------------------------------------

    def require_session(self, action: str) -> PdbSession:
        if self.pdb_session is None:
            raise _safe_rejection(f"{action} requires an active PDB session")
        return self.pdb_session
