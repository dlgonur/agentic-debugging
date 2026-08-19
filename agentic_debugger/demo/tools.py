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
    DemoScenario,
    probe_driver_source,
    resolve_probe_breakpoint,
)
from agentic_debugger.demo.external_runtime import (
    PublicRuntimeClassification,
    classify_public_runtime_result,
    is_external_isolated_task,
    production_path_prefixes,
    validate_model_selected_pdb_target,
    validate_public_runtime_target,
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
    PatchStateError,
    PatchValidationError,
    PdbSessionError,
    PdbSessionTimeoutError,
    SourceInspectionError,
    SourceParseError,
    WorkspaceError,
)
from agentic_debugger.runtime.patcher import PatchManager
from agentic_debugger.runtime.execution import VerifiedExecutionContext
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.test_runner import TestRunKind, TestRunner
from agentic_debugger.runtime.workspace import TaskWorkspace
from agentic_debugger.skills.file_skills import get_source_window
from agentic_debugger.skills.search_skills import (
    find_class,
    find_function,
    search_code,
)

#: Source-window radius used by the demonstration.  Small enough to keep the
#: observation payload bounded and stable, large enough to show the defect.
SOURCE_WINDOW_RADIUS = 6


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
    original_line_count: Optional[int] = None,
    production_paths: Optional[Sequence[str]] = None,
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
        raw,
        workspace_root,
        script_path,
        original_line_count,
        production_paths=production_paths,
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


def prepare_pdb_probe(
    fixture_dir: Path,
    scenario: DemoScenario,
    parent_dir: Path,
    *,
    model_selects_breakpoint: bool = False,
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
    module.write_text(original + probe_driver_source(probe), encoding="utf-8", newline="\n")
    return PdbProbe(
        source_dir=source_dir,
        parent_dir=parent_dir,
        script=probe.module_path,
        breakpoint_line=breakpoint_line,
        focus_function=probe.focus_function,
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
        # Durable external-runtime classification: a Docker/dependency launch
        # failure is infrastructure evidence, not a model-reproduced bug.
        self.runtime_infrastructure_failure = False
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
            if is_external_isolated_task(self.task):
                return
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
    isolated = is_external_isolated_task(task)
    # An isolated repository-scale treatment is PDB-disabled unless the
    # caller explicitly selects a PDB policy.  Omitting the specs is
    # intentional: disabled actions must not appear in the model request.
    pdb_enabled = not isolated or (
        pdb_policy is not None and pdb_policy is not PdbPolicy.DISABLED
    )
    external_interactive = (isolated and pdb_enabled) or interactive_debugger_controls
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
        isolated = is_external_isolated_task(task)
        public_target = arguments.get("public_target")
        if isolated and not public_target:
            raise _safe_rejection(
                "no public reproduction command is declared; provide a "
                "model-selected public_target that already exists in the "
                "workspace. Hidden verifier tests are withheld"
            )
        if isolated:
            try:
                public_target = validate_public_runtime_target(
                    context.workspace,
                    str(public_target),
                )
            except ValueError as exc:
                raise _safe_rejection(str(exc)) from exc
            argv = [
                "python",
                "-m",
                "pytest",
                public_target,
                "-q",
                "-p",
                "no:cacheprovider",
            ]
            result = context.test_runner.run_tests(
                argv,
                task.reproduction.cwd,
                task.reproduction.timeout_seconds,
                kind=TestRunKind.REPRODUCTION,
            )
        else:
            result = context.test_runner.run_reproduction(task)
        runtime_classification = (
            classify_public_runtime_result(result) if isolated else None
        )
        if runtime_classification is PublicRuntimeClassification.DEPENDENCY_FAILURE:
            context.runtime_infrastructure_failure = True
        if result.timed_out:
            if isolated:
                classification = PublicRuntimeClassification.TIMEOUT.value
                payload = {
                    "phase": phase,
                    "public_target": public_target,
                    "passed": False,
                    "failure_reproduced": False,
                    "runtime_classification": classification,
                    "exit_code": result.command_result.exit_code,
                }
                if phase == "baseline":
                    context.baseline_failure_reproduced = False
                else:
                    context.post_patch_f2p_passed = False
                return _ok(payload, "public pytest target timed out")
            raise ToolTimeoutError("reproduction command timed out")
        if result.launch_error or result.command_result.exit_code is None:
            if isolated:
                classification = PublicRuntimeClassification.DEPENDENCY_FAILURE.value
                payload = {
                    "phase": phase,
                    "public_target": public_target,
                    "passed": False,
                    "failure_reproduced": False,
                    "runtime_classification": classification,
                    "exit_code": result.command_result.exit_code,
                }
                if phase == "baseline":
                    context.baseline_failure_reproduced = False
                else:
                    context.post_patch_f2p_passed = False
                return _ok(payload, "public pytest runtime was unavailable")
            raise ToolExecutionError("reproduction command could not be launched")
        reproduced = not result.passed
        if not isolated:
            reproduced = bool(result.reproduction_match) and not result.passed
        else:
            reproduced = runtime_classification is PublicRuntimeClassification.TARGET_FAILED
        prefixes = production_path_prefixes(task) if isolated else ()
        module_path = ""
        original_line_count: Optional[int] = None
        if isolated:
            module_path = ""
        else:
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
            "exit_code": result.command_result.exit_code,
            "expected_exit_code": (
                None if isolated else task.reproduction.expected_exit_code
            ),
            "passed": bool(result.passed),
            "failure_reproduced": reproduced,
            "runtime_classification": (
                runtime_classification.value if runtime_classification else None
            ),
            "public_target": public_target if isolated else None,
            "failure_output": reproduction_failure_output(
                result,
                context.workspace.root,
                module_path,
                original_line_count,
                production_paths=prefixes if isolated else None,
            ) if not result.passed else "",
            "failure_output_raw": reproduction_failure_output_raw(
                result, context.workspace.root
            ) if not result.passed else "",
        }
        if not isolated:
            payload["node_id"] = task.tests.fail_to_pass[0]
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
            if is_external_isolated_task(task):
                context.regression_passed = True
                return _ok(
                    {
                        "node_ids": [],
                        "node_count": 0,
                        "exit_code": 0,
                        "all_passed": True,
                        "empty_official_p2p": True,
                    },
                    "official pass-to-pass set is empty; regression is vacuously preserved",
                )
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
        if is_external_isolated_task(task) and not task.tests.pass_to_pass:
            p2p: list[bool] = []
        else:
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
            session = context.pdb_session_factory(workspace)
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
            match = find_function(
                context.workspace, arguments["name"], arguments.get("path")
            )
        except (SourceInspectionError, SourceParseError, WorkspaceError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        if match is None:
            raise ToolExecutionError("declared symbol was not found in the declared file")
        return _ok(_json_safe(match.to_mapping(), "find_function"), "declared symbol located")

    def handle_find_class(action: Action, arguments: dict[str, object]) -> ToolResult:
        try:
            match = find_class(
                context.workspace, arguments["name"], arguments.get("path")
            )
        except (SourceInspectionError, SourceParseError, WorkspaceError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        if match is None:
            raise ToolExecutionError("declared class was not found in the repository")
        return _ok(_json_safe(match.to_mapping(), "find_class"), "declared class located")

    def handle_search_code(action: Action, arguments: dict[str, object]) -> ToolResult:
        try:
            matches, truncated = search_code(
                context.workspace,
                arguments["query"],
                path=arguments.get("path"),
                max_matches=arguments.get("max_matches", 100),
                case_sensitive=arguments.get("case_sensitive", True),
            )
        except (SourceInspectionError, WorkspaceError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        return _ok(
            {
                "matches": [item.to_mapping() for item in matches],
                "match_count": len(matches),
                "truncated": truncated,
            },
            "bounded repository search completed",
        )

    def handle_get_source_window(action: Action, arguments: dict[str, object]) -> ToolResult:
        line = arguments["line"]
        if line < 1:
            raise _safe_rejection("line must be positive")
        try:
            window = get_source_window(
                context.workspace, arguments["path"], line, SOURCE_WINDOW_RADIUS
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
            )
        )
        return _ok(dict(declared), "root-cause hypothesis recorded")

    # -- patch lifecycle ---------------------------------------------------

    def handle_apply_patch(action: Action, arguments: dict[str, object]) -> ToolResult:
        # A new apply attempt replaces or abandons the previous candidate, so
        # any earlier Validate evidence is no longer about the workspace.
        context.clear_validation_evidence()
        diff = arguments["patch"]
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
            except (PatchStateError, PatchApplyError) as exc:
                raise _safe_rejection(bounded_diagnostic(exc)) from exc
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
        except (PatchValidationError, PatchAuthorizationError, PatchStateError) as exc:
            context.observe(
                lambda: context.observability.patch_rejected(
                    attempt_index, bounded_diagnostic(exc, context.workspace.root)
                )
            )
            raise _safe_rejection(bounded_diagnostic(exc)) from exc
        except PatchApplyError as exc:
            context.observe(
                lambda: context.observability.patch_apply_failed(
                    attempt_index, bounded_diagnostic(exc, context.workspace.root)
                )
            )
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
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
            "hunk_count_adjustments": [
                list(item) for item in result.hunk_count_adjustments
            ],
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
        except (PatchStateError, PatchApplyError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
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
        except (PatchStateError, PatchApplyError) as exc:
            raise ToolExecutionError(bounded_diagnostic(exc)) from exc
        context.syntax_passed = bool(result.all_passed)
        return _ok(
            {
                "all_passed": bool(result.all_passed),
                "results": [item.to_mapping() for item in result.results],
            },
            "patched source syntax validated",
        )

    # -- bounded runtime evidence -----------------------------------------

    def handle_start_pdb(action: Action, arguments: dict[str, object]) -> ToolResult:
        if pdb_policy is PdbPolicy.DISABLED:
            raise _safe_rejection("PDB access is disabled by evaluation policy")
        isolated = is_external_isolated_task(task)
        probe = context.probe
        if not isolated and probe is None:
            raise _safe_rejection("no runtime probe is configured for this task")
        if context.pdb_session is not None:
            raise _safe_rejection("a PDB session is already active")
        if (isolated or interactive_debugger_controls) and context.pdb_session_started:
            raise _safe_rejection(
                "interactive debugger pilot permits one PDB session per case"
            )
        script = probe.script if probe is not None else ""
        breakpoint_line = probe.breakpoint_line if probe is not None else 0
        selected_symbol = None
        if isolated:
            try:
                script, breakpoint_line, selected_symbol = (
                    validate_model_selected_pdb_target(
                        context.workspace,
                        str(arguments.get("path") or ""),
                        int(arguments.get("breakpoint_line") or 0),
                        prefixes=production_path_prefixes(task),
                        symbol=(
                            str(arguments["symbol"])
                            if arguments.get("symbol")
                            else None
                        ),
                    )
                )
            except (TypeError, ValueError) as exc:
                raise _safe_rejection(str(exc)) from exc
            try:
                workspace = TaskWorkspace(
                    context.workspace.root, parent_dir=str(Path(context.workspace.root).parent)
                )
            except WorkspaceError as exc:
                diag = bounded_diagnostic(exc)
                raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        else:
            try:
                workspace = TaskWorkspace(str(probe.source_dir), parent_dir=str(probe.parent_dir))
            except WorkspaceError as exc:
                diag = bounded_diagnostic(exc)
                raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
            breakpoint_line = (
                int(arguments["breakpoint_line"])
                if interactive_debugger_controls
                else probe.breakpoint_line
            )
        context.pdb_workspace = workspace
        session = context.pdb_session_factory(workspace)
        context.pdb_session = session
        if breakpoint_line <= 0:
            context.release_pdb()
            raise _safe_rejection("breakpoint_line must be positive")
        try:
            session.start()
            context.pdb_session_started = True
            started = session.start_paused_target(script, [breakpoint_line])
        except (PdbSessionError, PdbSessionTimeoutError) as exc:
            context.release_pdb()
            diag = bounded_diagnostic(exc)
            raise ToolExecutionError(diag, safe_diagnostic=diag) from exc
        if started.get("state") != "paused":
            context.release_pdb()
            raise ToolExecutionError(
                "runtime target did not reach the declared breakpoint",
                safe_diagnostic="runtime target did not reach the declared breakpoint",
            )
        context.pdb_pause_generation = 1
        context.observe(
            lambda: context.observability.debugger_started(
                script, [f"{script}:{breakpoint_line}"]
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
        if isolated:
            payload["path"] = script
            if selected_symbol:
                payload["symbol"] = selected_symbol
        elif not interactive_debugger_controls:
            payload["focus_function"] = probe.focus_function
        return _ok(
            payload,
            "debugger paused at the declared breakpoint",
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
        context.observe(
            lambda: context.observability.stack_observed(dict(stack))
        )
        return _ok(_json_safe(dict(stack), "get_stack_summary"), "bounded stack summary collected")

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
        context.observe(
            lambda: context.observability.locals_observed(dict(result))
        )
        return _ok(_json_safe(dict(result), "get_frame_locals"), "bounded frame locals collected")

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
        return _ok(
            _json_safe(dict(result), action.name),
            f"debugger execution control completed: {action.name}",
        )

    def handle_stop_pdb(action: Action, arguments: dict[str, object]) -> ToolResult:
        if (isolated or interactive_debugger_controls) and context.pdb_session is None:
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

    repro_validator = _validator(
        {"phase": str, "public_target": str} if isolated else {"phase": str},
    )
    if isolated:
        start_pdb_validator = _validator(
            {"path": str, "breakpoint_line": int},
            optional={"symbol": str},
            minimums={"breakpoint_line": 1},
        )
    elif interactive_debugger_controls:
        start_pdb_validator = _validator(
            {"breakpoint_line": int},
            minimums={"breakpoint_line": 1},
        )
    else:
        start_pdb_validator = _validator({})
    tool_specs = [
        spec(ActionName.RUN_REPRODUCTION, repro_validator, handle_run_reproduction),
        spec(ActionName.RUN_REGRESSION_TESTS, _validator({}), handle_run_regression_tests),
        spec(ActionName.CLASSIFY_OUTCOME, _validator({}), handle_classify_outcome),
        spec(
            ActionName.FIND_FUNCTION,
            _validator({"name": str}, optional={"path": str}),
            handle_find_function,
        ),
        spec(
            ActionName.FIND_CLASS,
            _validator({"name": str}, optional={"path": str}),
            handle_find_class,
        ),
        spec(
            ActionName.SEARCH_CODE,
            _validator(
                {"query": str},
                optional={"path": str, "max_matches": int, "case_sensitive": bool},
                minimums={"max_matches": 1},
            ),
            handle_search_code,
        ),
        spec(
            ActionName.GET_SOURCE_WINDOW,
            _validator({"path": str, "line": int}),
            handle_get_source_window,
        ),
        spec(
            ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS,
            _validator(
                {
                    "hypothesis_id": str,
                    "statement": str,
                    "target_file": str,
                    "target_symbol": str,
                    "confidence": str,
                },
                enums={
                    "confidence": tuple(item.value for item in HypothesisConfidence)
                },
            ),
            handle_express_hypothesis,
        ),
        spec(ActionName.APPLY_PATCH, _validator({"patch": str}), handle_apply_patch),
    ]
    if not isolated or pdb_enabled:
        tool_specs.extend(
            [
                spec(ActionName.GET_FAILURE_TRACE, _validator({}), handle_get_failure_trace),
                spec(ActionName.REVERT_PATCH, _validator({}), handle_revert_patch),
                spec(ActionName.SYNTAX_CHECK, _validator({}), handle_syntax_check),
                spec(ActionName.START_PDB_SESSION, start_pdb_validator, handle_start_pdb),
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
        )
    if external_interactive:
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
    if not isolated or pdb_enabled:
        tool_specs.append(spec(ActionName.STOP_PDB_SESSION, _validator({}), handle_stop_pdb))
    return ToolRegistry(tuple(tool_specs))


__all__ = [
    "MAX_DIAGNOSTIC_CHARS",
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
