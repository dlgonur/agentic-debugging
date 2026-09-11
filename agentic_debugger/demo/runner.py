"""The deterministic offline demonstration runner.

This module owns the demo case orchestration and report assembly:
:func:`run_demo_case` (workspace, probe, registry, deterministic
controller run, independent verifier evaluation, workspace release),
``_assemble_case``, :func:`run_demo`, and ``results_json``, plus the
public import surface of the demo runner.

Architecture (one-way dependency graph):

* :mod:`agentic_debugger.demo.report_records` — the versioned report
  vocabulary and the immutable result/report records;
* :mod:`agentic_debugger.demo.report_facts` — digests, environment
  record, localization/runtime-evidence classification, and the
  controller/verifier/trajectory record projections;
* this module — case orchestration, report assembly/release, and the
  public surface.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerRunResult,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    ControllerState,
    HypothesisLedger,
    PdbPolicy,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.demo.catalog import (
    DemoCatalogError,
    DemoScenario,
    build_reference_patch,
    scenario_for,
    scenario_ids,
)
from agentic_debugger.demo.context import DemoToolContext
from agentic_debugger.demo.isolation import OfflineGuard, guard_scope_note
from agentic_debugger.demo.model import DEMO_MODEL_NAME, DemoPolicyModel
from agentic_debugger.demo.pdb_probe import prepare_pdb_probe
from agentic_debugger.demo.policies import DEMO_POLICIES, DemoPolicy, pdb_policy_for

from agentic_debugger.demo.report_facts import (
    _controller_record,
    _trajectory_record,
    _verifier_record,
    classify_localization,
    classify_runtime_evidence,
    environment_record,
    fixture_digest,
    localization_record,
    source_tree_digest,
)
from agentic_debugger.demo.report_records import (
    CURATED_RELATIVE_ROOT,
    DEMO_MAX_MODEL_CALLS,
    DEMO_TOOL_VERSION,
    NONDETERMINISTIC_RESULT_KEYS,
    RESULTS_SCHEMA_VERSION,
    RUNTIME_ATTRIBUTION_NOTE,
    DemoCaseResult,
    DemoCaseStatus,
    DemoError,
    DemoInputError,
    DemoReport,
    LocalizationOutcome,
    RuntimeEvidenceOutcome,
    _PDB_ACTION_NAMES,
    _PDB_OBSERVATION_ACTION_NAMES,
    _bounded,
    _diagnostic,
    _utc_now,
    curated_task_ids,
    deterministic_view,
)
from agentic_debugger.evaluation.runner import bounded_error, load_task, normalize_output
from agentic_debugger.demo.tools import build_registry
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.events.replay import (
    ReplayError,
    replay_events,
    semantic_projection,
)
from agentic_debugger.events.schema import EventType, ObservationStatus
from agentic_debugger.runtime.workspace import TaskWorkspace

def run_demo_case(
    *,
    repository_root: str | Path,
    task_id: str,
    policy: DemoPolicy,
    workspace_parent: str | Path,
    rag_context: Any = None,
) -> DemoCaseResult:
    """Execute one curated task under one demonstration policy end to end.

    ``rag_context`` is an optional additive seam: when supplied (and only
    then) it is handed to the offline model, which records its retrieval
    evidence on the case result without changing any directive.  Default
    no-RAG behavior is unchanged and the default case record is
    byte-identical.
    """

    if type(policy) is not DemoPolicy:
        raise DemoInputError("policy must be a DemoPolicy")
    repo = Path(repository_root).resolve()
    fixture_dir = repo / CURATED_RELATIVE_ROOT / task_id
    if not (fixture_dir / "task.json").is_file():
        raise DemoInputError(f"curated task manifest is missing: {task_id}")
    try:
        scenario = scenario_for(task_id)
    except DemoCatalogError as exc:
        raise DemoInputError(str(exc)) from exc

    parent = Path(workspace_parent)
    if not parent.is_dir():
        raise DemoInputError(f"workspace_parent must be an existing directory: {parent}")

    pdb_mode = pdb_policy_for(policy)
    # Load the manifest before creating any directory so an invalid task cannot
    # leave an orphaned case workspace behind, and so a malformed canonical
    # manifest surfaces as an input error rather than an uncaught traceback.
    try:
        task = load_task(str(fixture_dir / "task.json"))
    except DemoInputError:
        raise
    except Exception as exc:
        raise DemoInputError(
            f"curated task manifest {task_id!r} is invalid: {_diagnostic(exc)}"
        ) from exc

    case_parent = parent / f"case-{task_id}-{policy.value}"
    case_parent.mkdir(parents=True, exist_ok=False)
    diagnostics: list[str] = []
    started = time.monotonic()

    workspace: Optional[TaskWorkspace] = None
    context: Optional[DemoToolContext] = None
    model: Optional[DemoPolicyModel] = None
    probe = None
    controller_result: Optional[ControllerRunResult] = None
    evaluation = None
    verifier_note: Optional[str] = None
    semantic_events: tuple[dict[str, Any], ...] = ()
    events_jsonl = ""
    replay_error: Optional[str] = None
    patch_text = ""
    status = DemoCaseStatus.HARNESS_ERROR
    guard = OfflineGuard()
    # Hash the canonical fixture around the controller phase so the
    # demonstration reports fixture immutability itself rather than relying
    # only on the verifier, whose own hash is taken after the controller ran.
    fixture_before = fixture_digest(fixture_dir)
    fixture_after: Optional[str] = None

    try:
        with guard:
            workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_parent))
            if scenario.runtime_probe.exact_public_reproduction:
                (Path(workspace.root) / "task.json").write_text(
                    json.dumps(task.agent_visible_mapping(), sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
            source_path = Path(workspace.root) / scenario.reference_repair.target_path
            patch_text = build_reference_patch(
                source_path.read_text(encoding="utf-8"), scenario.reference_repair
            )
            if pdb_mode is not PdbPolicy.DISABLED:
                probe = prepare_pdb_probe(
                    fixture_dir, scenario, case_parent, task=task
                )

            context = DemoToolContext(
                task=task,
                workspace=workspace,
                patch=patch_text,
                probe=probe,
            )
            model = DemoPolicyModel(
                scenario=scenario,
                patch=patch_text,
                pdb_policy=pdb_mode,
                rag_context=rag_context,
            )
            controller = DeterministicController(
                build_registry(
                    context,
                    interactive_debugger_controls=(
                        scenario.runtime_probe.exact_public_reproduction
                    ),
                ),
                model,
                ControllerRunConfig(
                    max_model_calls=DEMO_MAX_MODEL_CALLS,
                    require_pdb_evidence_before_patch=(
                        scenario.runtime_probe.exact_public_reproduction
                    ),
                ),
            )
            snapshot = ControllerSnapshot(
                f"{task_id}--{policy.value}",
                task_id,
                ControllerState.REPRODUCE,
                0,
                ControllerBudgetLimits.from_task_constraints(task.constraints),
                ControllerBudgetState(),
                HypothesisLedger(),
            )
            controller_result = controller.run(snapshot)
            fixture_after = fixture_digest(fixture_dir)

            stream = io.StringIO()
            logger = JsonlEventLogger(controller_result.run_id, controller_result.task_id, stream=stream)
            try:
                for event in project_controller_run(
                    controller_result,
                    tool_version=DEMO_TOOL_VERSION,
                    model=DEMO_MODEL_NAME,
                    timestamp=_utc_now(),
                    duration_ms=None,
                ):
                    logger.append(event)
                logger.flush()
            finally:
                logger.close()
            events_jsonl = stream.getvalue()
            try:
                trajectory = replay_events(events_jsonl)
                semantic_events = semantic_projection(
                    trajectory, workspace_roots=[str(case_parent), str(workspace.root)]
                )
            except ReplayError as exc:
                replay_error = _diagnostic(exc, str(case_parent))
                diagnostics.append(f"replay validation failed: {replay_error}")

            # The verifier independently re-evaluates the candidate diff from a
            # clean baseline; it never sees the controller's workspace.
            evaluation = EvaluationVerifier(str(repo), workspace_parent=str(case_parent)).evaluate(
                task, patch_text
            )
            status = (
                DemoCaseStatus.COMPLETED
                if controller_result.final_state is ControllerState.DONE
                else DemoCaseStatus.CONTROLLER_STOPPED
            )
    except BaseException as exc:  # noqa: BLE001 - recorded, not swallowed silently
        diagnostics.append(f"case execution failed: {_diagnostic(exc, str(case_parent))}")
        if evaluation is None:
            verifier_note = "verifier skipped because the demonstration case failed"
        status = DemoCaseStatus.HARNESS_ERROR
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        cleanup_ok = _release(context, workspace, case_parent, diagnostics)
        if not cleanup_ok:
            status = DemoCaseStatus.HARNESS_ERROR

    wall_clock_ms = int((time.monotonic() - started) * 1000)
    return _assemble_case(
        task_id=task_id,
        policy=policy,
        pdb_mode=pdb_mode,
        task=task,
        status=status,
        controller_result=controller_result,
        context=context,
        model_used=model,
        evaluation=evaluation,
        verifier_note=verifier_note,
        semantic_events=semantic_events,
        events_jsonl=events_jsonl,
        replay_error=replay_error,
        patch_text=patch_text,
        diagnostics=diagnostics,
        wall_clock_ms=wall_clock_ms,
        offline_ledger=guard.ledger,
        fixture_before=fixture_before,
        fixture_after=fixture_after,
    )

def _release(
    context: Optional[DemoToolContext],
    workspace: Any,
    case_parent: Path,
    diagnostics: list[str],
) -> bool:
    """Release every workspace and subprocess resource; never raise."""

    ok = True
    root = str(case_parent)
    if context is not None:
        # release_pdb keeps a handle whose stop() failed, so a second pass
        # here is a genuine retry rather than a no-op.
        for error in context.release_pdb():
            ok = False
            diagnostics.append(f"pdb cleanup failed: {_diagnostic(error, root)}")
    if workspace is not None:
        try:
            workspace.cleanup()
            # TaskWorkspace.cleanup swallows its own rmtree failure, so the
            # only reliable check is whether the root is actually gone.
            if os.path.exists(workspace.root):
                raise DemoError("controller workspace root remains after cleanup")
        except BaseException as exc:  # noqa: BLE001 - cleanup must continue
            ok = False
            diagnostics.append(
                f"controller workspace cleanup failed: {_diagnostic(exc, root)}"
            )
    try:
        if case_parent.exists():
            shutil.rmtree(case_parent)
        if case_parent.exists():
            ok = False
            diagnostics.append("case workspace parent still exists after cleanup")
    except BaseException as exc:  # noqa: BLE001 - cleanup must continue
        ok = False
        diagnostics.append(f"case workspace cleanup failed: {_diagnostic(exc, root)}")
    return ok


def _assemble_case(
    *,
    task_id: str,
    policy: DemoPolicy,
    pdb_mode: Any,
    task: Any,
    status: DemoCaseStatus,
    controller_result: Optional[ControllerRunResult],
    context: Optional[DemoToolContext],
    model_used: Any,
    evaluation: Any,
    verifier_note: Optional[str],
    semantic_events: tuple[dict[str, Any], ...],
    events_jsonl: str,
    replay_error: Optional[str],
    patch_text: str,
    diagnostics: list[str],
    wall_clock_ms: int,
    offline_ledger: Any,
    fixture_before: str,
    fixture_after: Optional[str],
) -> DemoCaseResult:
    tool_calls = tuple(context.tool_calls) if context else ()
    gate_records = tuple(item.to_mapping() for item in model_used.gate_records) if model_used else ()
    gate_reached = bool(gate_records)
    gate_allowed = bool(gate_records and gate_records[-1]["allowed"])

    declared = dict(context.declared_localization) if context and context.declared_localization else None
    changed_files = list(context.patch_changed_files) if context else []
    patch_applied = bool(context.patch_applied) if context else False

    localization = localization_record(
        declared,
        changed_files,
        patch_applied,
        task.oracle.target_files,
        task.oracle.target_symbols,
    )
    pdb_actions = sum(1 for name in tool_calls if name in _PDB_ACTION_NAMES)
    pdb_observations = sum(1 for name in tool_calls if name in _PDB_OBSERVATION_ACTION_NAMES)
    evidence_collected = bool(model_used.runtime_evidence_collected) if model_used else False
    session_started = bool(context.pdb_session_started) if context else False
    runtime_outcome = classify_runtime_evidence(
        gate_reached=gate_reached,
        gate_allowed=gate_allowed,
        session_started=session_started,
        evidence_collected=evidence_collected,
        proof_required=bool(
            context
            and context.probe is not None
            and context.probe.exact_public_reproduction
        ),
    )

    controller_record = (
        _controller_record(controller_result, tool_calls)
        if controller_result is not None
        else {
            "final_state": None,
            "stop_reason": None,
            "model_calls": 0,
            "step_count": 0,
            "tool_call_count": len(tool_calls),
            "tool_calls": list(tool_calls),
            "budget_state": None,
            "states_visited": [],
        }
    )

    return DemoCaseResult(
        task_id=task_id,
        policy=policy.value,
        pdb_policy=pdb_mode.value,
        status=status,
        controller=controller_record,
        gate_decisions=gate_records,
        localization=localization,
        patch={
            "source": "offline_catalog_reference_repair",
            "attempted": "apply_patch" in tool_calls,
            "applied": patch_applied,
            "changed_files": changed_files,
            "sha256": hashlib.sha256(patch_text.encode("utf-8")).hexdigest() if patch_text else None,
            "line_count": patch_text.count("\n") if patch_text else 0,
            "syntax_passed": context.syntax_passed if context else None,
        },
        runtime_evidence={
            "policy": pdb_mode.value,
            "gate_reached": gate_reached,
            "gate_allowed": gate_allowed,
            "gate_reason": gate_records[-1]["reason"] if gate_records else None,
            "session_started": session_started,
            "pdb_action_count": pdb_actions,
            # Attempted inspection calls, counted before dispatch.
            "pdb_observation_attempts": pdb_observations,
            # Inspection calls whose handler actually returned evidence.
            "pdb_observations_succeeded": len(context.pdb_observation_names) if context else 0,
            "pdb_observation_names": list(context.pdb_observation_names) if context else [],
            "evidence_collected": evidence_collected,
            "outcome": runtime_outcome.value,
            "diagnosis_change_attributable_to_runtime_evidence": False,
            "attribution_note": RUNTIME_ATTRIBUTION_NOTE,
        },
        controller_validation={
            "baseline_failure_reproduced": context.baseline_failure_reproduced if context else None,
            "post_patch_f2p_passed": context.post_patch_f2p_passed if context else None,
            "designated_regression_passed": context.regression_passed if context else None,
            "outcome": context.controller_outcome if context else None,
            "abort_reason": model_used.abort_reason if model_used else None,
        },
        verifier=_verifier_record(evaluation, verifier_note),
        trajectory=_trajectory_record(semantic_events, replay_error),
        tool_errors=tuple(dict(item) for item in (context.tool_errors if context else [])),
        offline={
            **offline_ledger.to_mapping(),
            "guard_installed": True,
            "guard_scope": guard_scope_note(),
            "canonical_fixture_sha256_before_controller": fixture_before,
            "canonical_fixture_sha256_after_controller": fixture_after,
            "canonical_fixture_unchanged_by_controller": None
            if fixture_after is None
            else fixture_after == fixture_before,
        },
        diagnostics=tuple(diagnostics),
        wall_clock_ms=wall_clock_ms,
        semantic_events=semantic_events,
        events_jsonl=events_jsonl,
        retrieval=model_used.retrieval_record if model_used else None,
    )


def run_demo(
    *,
    repository_root: str | Path,
    workspace_parent: str | Path,
    task_ids: Optional[Sequence[str]] = None,
    policies: Optional[Sequence[DemoPolicy]] = None,
) -> DemoReport:
    """Run every requested task/policy case and assemble the demo report."""

    repo = Path(repository_root).resolve()
    discovered = curated_task_ids(repo)
    selected = tuple(task_ids) if task_ids is not None else discovered
    unknown = sorted(set(selected) - set(discovered))
    if unknown:
        raise DemoInputError(f"unknown curated task ids: {unknown}")
    missing_scenarios = sorted(set(selected) - set(scenario_ids()))
    if missing_scenarios:
        raise DemoInputError(
            f"no demonstration scenario for curated tasks: {missing_scenarios}"
        )
    chosen_policies = tuple(policies) if policies is not None else DEMO_POLICIES
    if not chosen_policies:
        raise DemoInputError("at least one demonstration policy is required")

    cases: list[DemoCaseResult] = []
    for task_id in selected:
        for policy in chosen_policies:
            cases.append(
                run_demo_case(
                    repository_root=repo,
                    task_id=task_id,
                    policy=policy,
                    workspace_parent=workspace_parent,
                )
            )
    return DemoReport(environment=environment_record(repo), cases=tuple(cases))


def results_json(report: DemoReport) -> str:
    """Serialize the report deterministically apart from its marked sections."""

    return json.dumps(
        report.to_mapping(), ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True
    ) + "\n"


__all__ = [
    "CURATED_RELATIVE_ROOT",
    "DEMO_MAX_MODEL_CALLS",
    "DEMO_TOOL_VERSION",
    "NONDETERMINISTIC_RESULT_KEYS",
    "RESULTS_SCHEMA_VERSION",
    "RUNTIME_ATTRIBUTION_NOTE",
    "DemoCaseResult",
    "DemoCaseStatus",
    "DemoError",
    "DemoInputError",
    "DemoReport",
    "LocalizationOutcome",
    "RuntimeEvidenceOutcome",
    "classify_localization",
    "classify_runtime_evidence",
    "curated_task_ids",
    "deterministic_view",
    "environment_record",
    "fixture_digest",
    "localization_record",
    "results_json",
    "run_demo",
    "run_demo_case",
    "source_tree_digest",
]
