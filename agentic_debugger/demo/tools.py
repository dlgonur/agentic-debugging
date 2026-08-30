"""Real tool handlers for the Task 9 demonstration.

Every handler delegates to an already-accepted component:

============================  =======================================
Controller action             Backing implementation
============================  =======================================
``run_reproduction``          ``runtime.test_runner.TestRunner``
``get_failure_trace``         ``runtime.pdb_session.PdbSession.run_post_mortem``
``run_regression_tests``      ``runtime.test_runner.TestRunner``
``find_function``             ``skills.search_skills.find_function``
``get_source_window``         ``skills.file_skills.get_source_window``
``apply_patch``               ``runtime.patcher.PatchManager``
``syntax_check``              ``runtime.patcher.PatchManager``
``start_pdb_session``         ``runtime.pdb_session.PdbSession``
``get_stack_summary``         ``runtime.pdb_session.PdbSession``
``get_frame_locals``          ``runtime.pdb_session.PdbSession``
``safe_eval_expression``      ``runtime.pdb_session.PdbSession``
``continue_pdb_session``      ``runtime.pdb_session.PdbSession``
``step_pdb_session``          ``runtime.pdb_session.PdbSession``
``next_pdb_session``          ``runtime.pdb_session.PdbSession``
``stop_pdb_session``          ``runtime.pdb_session.PdbSession``
``classify_outcome``          ``evaluation.outcome_taxonomy``
============================  =======================================

``express_root_cause_hypothesis`` is the one handler with no backing runtime
component: it records the offline model's own localization and root-cause
claim so the evaluator can score it against the task oracle.

Handlers never fabricate a success.  A component that raises is surfaced as a
``rejected``/``error``/``timeout`` observation through the accepted tool
registry, and the controller reacts to the observation it actually received.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

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
from agentic_debugger.demo.catalog import (
    PROBE_DRIVER_FUNCTION,
    DemoScenario,
    exact_pytest_driver_source,
    probe_driver_source,
    resolve_probe_breakpoint,
)
from agentic_debugger.demo.sanitize import (
    MAX_RAW_FAILURE_OUTPUT_CHARS,
    sanitize_failure_output,
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
from agentic_debugger.runtime.execution import VerifiedExecutionContext
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.test_runner import TestRunKind, TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.skills.file_skills import get_source_window
from agentic_debugger.skills.search_skills import find_function

#: Source-window radius used by the demonstration.  Small enough to keep the
#: observation payload bounded and stable, large enough to show the defect.
SOURCE_WINDOW_RADIUS = 6
#: The exact lowest-rung proof exposes one complete small target function so
#: breakpoint selection and diagnosis are based on public source, not guesses.
EXACT_PROOF_SOURCE_WINDOW_RADIUS = 12


def legal_reproduction_phases(state: ControllerState) -> tuple[str, ...]:
    """Return the phase values accepted by run_reproduction in a state."""

    if state is ControllerState.REPRODUCE:
        return ("baseline",)
    if state is ControllerState.VALIDATE:
        return ("post_patch",)
    return ()


def validation_classification_ready(
    post_patch_f2p_passed: object,
    regression_passed: object,
) -> bool:
    """Return whether both required Validate evidence values have been collected.

    ``False`` is collected evidence (the check failed).  Only ``None`` means
    the corresponding evidence has not been gathered yet.  This helper never
    invents a pass/fail value.
    """

    return post_patch_f2p_passed is not None and regression_passed is not None

#: Maximum characters of a bounded diagnostic retained for reporting.
MAX_DIAGNOSTIC_CHARS = 400

#: Maximum characters of the RAW reproduction failure output retained in
#: the evidence payload (audit-only; never rendered into a model prompt).
MAX_RAW_FAILURE_OUTPUT_CHARS = 4000

#: Tail window of a failing-test record output fed back to the model after a
#: real verifier run (the exception/assertion summary is at the end).
MAX_VERIFIER_FAILURE_DETAIL_CHARS = 900


def _safe_rejection(message: str) -> ToolRejectedError:
    return ToolRejectedError(message, safe_diagnostic=message)


class DemoToolError(RuntimeError):
    """Raised for demonstration harness misuse rather than tool failure."""


def _observation_id_for_action(action: Action) -> str:
    """Derive the controller's detached observation id for this action."""

    prefix = "action-"
    if not action.action_id.startswith(prefix):
        raise DemoToolError("controller action id is not canonical")
    suffix = action.action_id[len(prefix):]
    if not suffix.isdigit():
        raise DemoToolError("controller action id has no numeric observation index")
    return "observation-" + suffix


def bounded_diagnostic(exc: BaseException, workspace_root: Optional[str] = None) -> str:
    """Bound a diagnostic and strip disposable workspace paths out of it.

    Diagnostics land in the deterministic section of the demonstration result
    document, so a raw ``PermissionError`` naming a ``mkdtemp`` directory would
    make that section unstable.  Normalisation reuses the accepted verifier
    helper so the demo and the verifier redact identically.
    """

    text = normalize_output(bounded_error(exc), workspace_root)
    text = "".join(char if 0x20 <= ord(char) != 0x7F else " " for char in text).strip()
    if len(text) > MAX_DIAGNOSTIC_CHARS:
        text = text[: MAX_DIAGNOSTIC_CHARS - 3] + "..."
    return text or "tool failure"


def _json_safe(value: Any, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ToolExecutionError(f"{label} is not JSON-compatible") from exc


def _bounded_tail(text: str, maximum: int) -> str:
    """Keep the tail of ``text`` at most ``maximum`` characters."""
    if len(text) <= maximum:
        return text
    marker = "... [output truncated] ...\n"
    return marker + text[-(maximum - len(marker)):]


def task_target_module_path(task: DebugTask) -> str:
    """Mechanically select the single writable production module.

    Uses the public task constraint ``constraints.allowed_write_paths``:
    exactly one writable ``.py`` path outside ``tests``.  Identical rule to
    the R5 launcher (reported design choice; no oracle data).
    """
    allowed = list(task.constraints.allowed_write_paths)
    candidates = [p for p in allowed if p.endswith(".py") and not p.startswith("tests/")]
    if len(candidates) != 1:
        raise DemoToolError(
            "task must declare exactly one writable production .py path, "
            f"got {sorted(allowed)!r}"
        )
    return candidates[0]


def reproduction_failure_output(
    result: Any,
    workspace_root: Optional[str],
    script_path: str,
    original_line_count: int,
) -> str:
    """SANITIZED reproduction diagnostic of the executed test command.

    ``result`` is a ``TestRunResult``.  The raw stdout/stderr of the
    executed command is consumed by the common deterministic sanitizer
    (``sanitize.sanitize_failure_output``), which derives the bounded
    structured production diagnostic and never forwards hidden-test
    content (test source, assertions, node ids, literals).  Empty when the
    command produced nothing.
    """
    command = result.command_result
    raw = (command.stdout or "") + "\n" + (command.stderr or "")
    if not raw.strip():
        return ""
    diagnostic = sanitize_failure_output(
        raw, workspace_root, script_path, original_line_count
    )
    return diagnostic.text


def reproduction_failure_output_raw(
    result: Any, workspace_root: Optional[str]
) -> str:
    """Bounded, normalized RAW failure output — evidence only.

    Retained for auditability of the sanitizer's mechanical derivation;
    never rendered into a model prompt.
    """
    command = result.command_result
    raw = (command.stdout or "") + "\n" + (command.stderr or "")
    if not raw.strip():
        return ""
    normalized = normalize_output(raw, workspace_root)
    return _bounded_tail(normalized, MAX_RAW_FAILURE_OUTPUT_CHARS)


def _validator(
    required: dict[str, type],
    optional: Optional[dict[str, type]] = None,
    *,
    enums: Optional[dict[str, tuple[object, ...]]] = None,
    minimums: Optional[dict[str, int]] = None,
) -> Callable[[dict[str, object]], dict[str, object]]:
    """Build a strict argument validator that rejects unknown keys."""

    optional = optional or {}
    enums = enums or {}
    minimums = minimums or {}
    known = set(required) | set(optional)

    def validate(arguments: dict[str, object]) -> dict[str, object]:
        if type(arguments) is not dict:
            raise _safe_rejection("arguments must be a mapping")
        unknown = sorted(set(arguments) - known)
        if unknown:
            raise _safe_rejection(f"unknown argument: {unknown[0]}")
        missing = sorted(set(required) - set(arguments))
        if missing:
            raise _safe_rejection(f"missing argument: {missing[0]}")
        for name, expected in {**required, **optional}.items():
            if name not in arguments:
                continue
            value = arguments[name]
            if type(value) is not expected:
                raise _safe_rejection(f"argument {name} has the wrong type")
            if expected is str and not value:
                raise _safe_rejection(f"argument {name} must be non-empty")
            if expected is int and value < 0:
                raise _safe_rejection(f"argument {name} must be non-negative")
            if expected is int and name in minimums and value < minimums[name]:
                raise _safe_rejection(
                    f"argument {name} must be at least {minimums[name]}"
                )
            if name in enums and value not in enums[name]:
                raise _safe_rejection(f"argument {name} has an unsupported value")
        return dict(arguments)

    def type_name(expected: type) -> str:
        return {str: "string", int: "integer", bool: "boolean"}.get(
            expected, expected.__name__
        )

    properties = {}
    for name, expected in {**required, **optional}.items():
        constraint = {"type": type_name(expected)}
        if expected is str:
            constraint["min_length"] = 1
        if expected is int:
            constraint["minimum"] = minimums.get(name, 0)
        if name in enums:
            constraint["enum"] = list(enums[name])
        properties[name] = constraint
    validate.argument_contract = {  # type: ignore[attr-defined]
        "required": list(required),
        "properties": properties,
        "additional_properties": False,
    }
    return validate


def pytest_argv(base: Sequence[str], node_ids: Sequence[str]) -> list[str]:
    """Rebuild a manifest pytest argv for an explicit set of node ids."""

    argv: list[str] = []
    replaced = False
    for item in base:
        if "::" in item:
            if not replaced:
                argv.extend(node_ids)
                replaced = True
            continue
        argv.append(item)
    if not replaced:
        argv.extend(node_ids)
    return argv


@dataclass(frozen=True)
class PdbProbe:
    """A prepared, disposable debugger target derived from the fixture."""

    source_dir: Path
    parent_dir: Path
    script: str
    breakpoint_line: int
    focus_function: str
    exact_public_reproduction: bool = False
    reproduction_argv: tuple[str, ...] = ()
    reproduction_node: str = ""
    workspace_id: str = ""
    production_file_sha256: str = ""


def opaque_workspace_id(workspace: TaskWorkspace) -> str:
    """Return a provider-safe identity derived from the actual workspace root."""

    return hashlib.sha256(
        str(Path(workspace.root).resolve()).encode("utf-8")
    ).hexdigest()[:24]


def prepare_pdb_probe(
    fixture_dir: Path,
    scenario: DemoScenario,
    parent_dir: Path,
    *,
    model_selects_breakpoint: bool = False,
    task: Optional[DebugTask] = None,
    model_visible_task_mapping: Optional[Mapping[str, Any]] = None,
) -> PdbProbe:
    """Copy the canonical fixture and append one module-level probe driver.

    The canonical fixture is never written to.  The copy receives a single
    appended driver function plus its call so the focus function actually runs
    under the debugger.  The accepted demo resolves its fixed breakpoint from
    the fixture AST.  The tuned-debugger pilot can instead leave the stored
    breakpoint unset (0) so the live model must supply ``breakpoint_line``.
    """

    probe = scenario.runtime_probe
    source_dir = parent_dir / f"probe-{scenario.task_id}"
    if source_dir.exists():
        raise DemoToolError(f"probe source directory already exists: {source_dir}")
    shutil.copytree(fixture_dir, source_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    module = source_dir / probe.module_path
    if not module.is_file():
        raise DemoToolError(f"probe module is missing from the fixture: {probe.module_path}")
    original = module.read_text(encoding="utf-8")
    breakpoint_line = (
        0 if model_selects_breakpoint else resolve_probe_breakpoint(original, probe)
    )
    exact = probe.exact_public_reproduction
    if exact and task is not None:
        # The probe workspace is provider-visible execution state.  Keep the
        # evaluator oracle and fixed revision out of it while retaining the
        # public task contract needed by tooling.
        visible_task = (
            model_visible_task_mapping
            if model_visible_task_mapping is not None
            else task.agent_visible_mapping()
        )
        (source_dir / "task.json").write_text(
            json.dumps(visible_task, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    reproduction_argv: tuple[str, ...] = ()
    reproduction_node = ""
    if exact:
        if task is None:
            raise DemoToolError("exact PDB probe requires the loaded task")
        reproduction_argv = tuple(task.reproduction.argv)
        nodes = tuple(task.tests.fail_to_pass)
        if len(nodes) != 1:
            raise DemoToolError("exact PDB proof requires one public failing pytest node")
        reproduction_node = nodes[0]
        marker = ("-m", "pytest")
        try:
            marker_index = next(
                index for index in range(len(reproduction_argv) - 1)
                if reproduction_argv[index:index + 2] == marker
            )
        except StopIteration as exc:
            raise DemoToolError("exact PDB reproduction must use python -m pytest") from exc
        pytest_args = reproduction_argv[marker_index + 2:]
        if reproduction_node not in pytest_args:
            raise DemoToolError("exact PDB reproduction argv does not name the public failing node")
        driver = exact_pytest_driver_source(probe, tuple(pytest_args))
    else:
        driver = probe_driver_source(probe)
    module.write_text(original + driver, encoding="utf-8", newline="\n")
    production_file_sha256 = hashlib.sha256(module.read_bytes()).hexdigest()
    workspace_id = hashlib.sha256(str(source_dir.resolve()).encode("utf-8")).hexdigest()[:24]
    return PdbProbe(
        source_dir=source_dir,
        parent_dir=parent_dir,
        script=probe.module_path,
        breakpoint_line=breakpoint_line,
        focus_function=probe.focus_function,
        exact_public_reproduction=exact,
        reproduction_argv=reproduction_argv,
        reproduction_node=reproduction_node,
        workspace_id=workspace_id,
        production_file_sha256=production_file_sha256,
    )


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
