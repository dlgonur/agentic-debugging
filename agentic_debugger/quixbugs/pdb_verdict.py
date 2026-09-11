"""Deterministic reachability driver, sequence evidence, and verdicts.

This module owns the scientific evaluation layer of the contained PDB
architecture: the deterministic controller-driven reachability driver,
event-sequence ordering evidence, durable event validation,
reachability verdict semantics, and the typed verdict/evidence
contract.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerRunResult,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisConfidence,
    HypothesisLedger,
    PdbGateContext,
    PdbGateDecision,
    PdbPolicy,
    decide_pdb_access,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    AddHypothesisDirective,
    ControllerSnapshot,
    ModelAdapterError,
    ModelDirective,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.bugsinpy.adapter import ExternalWorkspace
from agentic_debugger.bugsinpy.wsl import (
    DISTRO,
    ResourceLimits,
    WslBubblewrapRunner,
    build_bwrap_command,
    build_env_wrapped_command,
    build_linux_timeout_argv,
    build_prlimit_argv,
    build_wsl_command,
    to_wsl_path,
)
from agentic_debugger.demo.catalog import DemoCatalogError, RuntimeProbe, probe_driver_source, resolve_probe_breakpoint
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.tools import DemoToolContext, PdbProbe, build_registry
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.events.schema import RunEvent
from agentic_debugger.evaluation.runner import bounded_error
from agentic_debugger.evaluation.task_schema import DebugTask, TaskSource
from agentic_debugger.quixbugs.adapter import (
    QuixBugsAdapter,
    QuixBugsPreflightFacts,
    QuixBugsSourceAcquirer,
    QuixPreflightReport,
)
from agentic_debugger.runtime.exceptions import PdbSessionError
from agentic_debugger.runtime.execution import PdbLaunchPlan, VerifiedExecutionContext
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.workspace import TaskWorkspace

@dataclass
class DeterministicPdbReachabilityDriver:
    """Fixed, no-model directive script that exercises exactly one reviewed
    PDB reachability sequence: reproduce -> low-confidence runtime-evidence
    hypothesis -> real controller PDB gate check -> start session -> one
    bounded stack observation -> one bounded frame-locals observation -> stop
    session -> intentional early termination (no patch is required by scope).

    This is shaped like a :class:`~agentic_debugger.agent.model_adapter.ModelAdapter`
    (``next_directive(snapshot) -> ModelDirective``) so it can drive the real
    :class:`~agentic_debugger.agent.controller.DeterministicController`
    unchanged, but it never contacts a model or provider: every directive is
    fixed ahead of time, and the only "decision" it makes is calling the real
    :func:`decide_pdb_access` gate and refusing to proceed if it denies access.
    """

    hypothesis_id: str
    hypothesis_statement: str
    gate_policy: PdbPolicy = PdbPolicy.ON_UNCERTAINTY
    model_name: str = "deterministic-pdb-reachability-v1"
    _failure_reproduced: bool = field(default=False, init=False, repr=False)
    _pause_generation: Optional[int] = field(default=None, init=False, repr=False)
    _runtime_cursor: int = field(default=0, init=False, repr=False)
    gate_decisions: list[PdbGateDecision] = field(default_factory=list, init=False, repr=False)

    def _pdb_gate(self, snapshot: ControllerSnapshot) -> PdbGateDecision:
        active = snapshot.hypotheses.active_hypotheses()
        return decide_pdb_access(
            self.gate_policy,
            PdbGateContext(
                source_state=ControllerState.UNDERSTAND,
                failure_reproduced=self._failure_reproduced,
                remaining_pdb_observations=max(0, snapshot.budget_limits.max_pdb_observations - snapshot.budget_state.pdb_observations),
                failed_patch_attempts=snapshot.budget_state.patch_attempts,
                active_hypothesis=active[0] if active else None,
            ),
        )

    def _observe(self, snapshot: ControllerSnapshot) -> None:
        observation = snapshot.last_observation
        if observation is None or observation.status.value != "ok":
            return
        if observation.name == ActionName.RUN_REPRODUCTION.value and observation.payload.get("phase") == "baseline":
            self._failure_reproduced = bool(observation.payload.get("failure_reproduced"))
        elif observation.name == ActionName.GET_STACK_SUMMARY.value:
            generation = observation.payload.get("pause_generation")
            if type(generation) is int:
                self._pause_generation = generation

    def next_directive(self, snapshot: ControllerSnapshot) -> ModelDirective:
        self._observe(snapshot)
        state = snapshot.state

        if state is ControllerState.REPRODUCE:
            if snapshot.model_call_index == 0:
                return ActionDirective(ActionName.RUN_REPRODUCTION, {"phase": "baseline"})
            return TransitionDirective(
                ControllerState.UNDERSTAND,
                "baseline failure reproduced through the contained external runner",
            )

        if state is ControllerState.UNDERSTAND:
            if not snapshot.hypotheses.active_hypotheses():
                return AddHypothesisDirective(
                    self.hypothesis_id, self.hypothesis_statement, HypothesisConfidence.LOW, (), True,
                )
            decision = self._pdb_gate(snapshot)
            self.gate_decisions.append(decision)
            if not decision.allowed:
                raise ModelAdapterError(f"controller PDB gate denied runtime-evidence access: {decision.reason.value}")
            return TransitionDirective(
                ControllerState.RUNTIME_EVIDENCE,
                f"controller PDB gate allowed access ({decision.reason.value}); collecting bounded runtime evidence",
            )

        if state is ControllerState.RUNTIME_EVIDENCE:
            if self._runtime_cursor == 0:
                self._runtime_cursor = 1
                return ActionDirective(ActionName.START_PDB_SESSION, {})
            if self._runtime_cursor == 1:
                self._runtime_cursor = 2
                return ActionDirective(ActionName.GET_STACK_SUMMARY, {})
            if self._runtime_cursor == 2:
                if self._pause_generation is None:
                    raise ModelAdapterError("stack summary did not report a pause generation")
                self._runtime_cursor = 3
                return ActionDirective(
                    ActionName.GET_FRAME_LOCALS, {"frame_id": 0, "pause_generation": self._pause_generation},
                )
            if self._runtime_cursor == 3:
                self._runtime_cursor = 4
                return ActionDirective(ActionName.STOP_PDB_SESSION, {})
            return TransitionDirective(
                ControllerState.FAILED,
                "deterministic reachability case complete; patch verification is out of scope for this infrastructure task",
            )

        raise ModelAdapterError(f"deterministic reachability driver has no scripted step for state {state.value!r}")


def _project_events(result: ControllerRunResult, *, tool_version: str) -> str:
    stream = io.StringIO()
    logger = JsonlEventLogger(result.run_id, result.task_id, stream=stream)
    try:
        for event in project_controller_run(
            result, tool_version=tool_version, model=None,
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), duration_ms=None,
        ):
            logger.append(RunEvent.from_mapping(event.to_mapping()))
        logger.flush()
        return stream.getvalue()
    finally:
        logger.close()


def _pdb_observation_counts(result: ControllerRunResult) -> dict[str, int]:
    """Aggregate observation counts, retained for reporting only.

    This is deliberately never the sole (or even primary) basis for the
    PASSED verdict -- see :func:`evaluate_reachability_sequence_from_events`,
    which requires the exact named actions to have succeeded, in order, with
    the expected payload identity at each step.
    """
    observed_names = {ActionName.GET_STACK_SUMMARY.value, ActionName.GET_FRAME_LOCALS.value, ActionName.SAFE_EVAL_EXPRESSION.value}
    successful = 0
    failed = 0
    for step in result.steps:
        if step.action is None or step.action.name not in observed_names or step.observation is None:
            continue
        if step.observation.status.value == "ok":
            successful += 1
        else:
            failed += 1
    return {"successful_pdb_observation_count": successful, "failed_pdb_observation_count": failed}


#: Required for a valid, complete event trail: one event of each name below
#: must be present in the serialized ``events_jsonl``, independent of the
#: structural ``result.steps`` check (this validates that projection/
#: serialization itself actually produced complete, parseable evidence).
_REQUIRED_EVENT_NAMES = frozenset({
    "run_reproduction", "start_pdb_session", "get_stack_summary",
    "get_frame_locals", "stop_pdb_session", "run_finished",
})
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _after(index: Optional[int]) -> int:
    return -1 if index is None else index


def _find_event_index(events: tuple[RunEvent, ...], *, event_type: str, name: str, after: Optional[int] = None) -> Optional[int]:
    """Index of the first recorded event of ``event_type``/``name`` strictly after ``after``."""
    start = _after(after)
    for index, event in enumerate(events):
        if index <= start:
            continue
        if event.event_type.value == event_type and event.name == name:
            return index
    return None


@dataclass(frozen=True)
class ReachabilitySequenceEvidence:
    """Structural, per-event evidence that the exact reviewed action sequence
    (reproduce -> start -> stack -> locals -> stop -> intentional terminal
    transition) succeeded in order, not merely an aggregate observation
    count.

    Evaluated directly against the recorded, parsed ``RunEvent`` trail -- the
    same artifact that is actually persisted and reported -- rather than the
    in-memory ``ControllerRunResult``, so the identical check can be replayed
    offline against a previously captured ``events_jsonl`` (see
    ``scripts/quixbugs_gcd_pdb_reachability_offline_revalidation.py``).
    """

    ok: bool
    reasons: tuple[str, ...]
    steps: dict[str, Any]

    def to_mapping(self) -> dict[str, Any]:
        return {"ok": self.ok, "reasons": list(self.reasons), "steps": self.steps}


def evaluate_reachability_sequence_from_events(
    events: tuple[RunEvent, ...], *, expected_script: str, expected_function: str, expected_breakpoint_line: int,
) -> ReachabilitySequenceEvidence:
    reasons: list[str] = []
    evidence: dict[str, Any] = {}

    reproduction_index = _find_event_index(events, event_type="observation", name=ActionName.RUN_REPRODUCTION.value)
    if reproduction_index is None:
        reasons.append("no run_reproduction observation event was recorded")
    else:
        observation = events[reproduction_index].payload.get("observation", {})
        payload = observation.get("payload", {}) if isinstance(observation, dict) else {}
        evidence["run_reproduction"] = payload
        ok = (
            isinstance(observation, dict) and observation.get("status") == "ok"
            and payload.get("phase") == "baseline"
            and payload.get("failure_reproduced") is True
        )
        if not ok:
            reasons.append("baseline run_reproduction did not succeed with failure_reproduced=true")

    start_index = _find_event_index(events, event_type="observation", name=ActionName.START_PDB_SESSION.value, after=reproduction_index)
    if start_index is None:
        reasons.append("no start_pdb_session observation event was recorded after baseline reproduction")
    else:
        observation = events[start_index].payload.get("observation", {})
        payload = observation.get("payload", {}) if isinstance(observation, dict) else {}
        evidence["start_pdb_session"] = payload
        ok = (
            isinstance(observation, dict) and observation.get("status") == "ok"
            and payload.get("state") == "paused"
            and payload.get("script") == expected_script
            and payload.get("line") == expected_breakpoint_line
            and payload.get("function") == expected_function
        )
        if not ok:
            reasons.append("start_pdb_session did not pause at the reviewed script/breakpoint/function")

    stack_index = _find_event_index(events, event_type="observation", name=ActionName.GET_STACK_SUMMARY.value, after=start_index)
    if start_index is None or stack_index is None:
        reasons.append("no get_stack_summary observation event was recorded after a successful start_pdb_session")
    else:
        observation = events[stack_index].payload.get("observation", {})
        payload = observation.get("payload", {}) if isinstance(observation, dict) else {}
        evidence["get_stack_summary"] = payload
        frames = payload.get("frames")
        ok = (
            isinstance(observation, dict) and observation.get("status") == "ok"
            and isinstance(frames, list) and len(frames) >= 1
            and payload.get("script") == expected_script
        )
        if not ok:
            reasons.append("get_stack_summary did not return a successful bounded stack observation")

    locals_index = _find_event_index(events, event_type="observation", name=ActionName.GET_FRAME_LOCALS.value, after=stack_index)
    if stack_index is None or locals_index is None:
        reasons.append("no get_frame_locals observation event was recorded after a successful get_stack_summary")
    else:
        observation = events[locals_index].payload.get("observation", {})
        payload = observation.get("payload", {}) if isinstance(observation, dict) else {}
        evidence["get_frame_locals"] = payload
        locals_list = payload.get("locals")
        ok = (
            isinstance(observation, dict) and observation.get("status") == "ok"
            and isinstance(locals_list, list) and len(locals_list) >= 1
        )
        if not ok:
            reasons.append("get_frame_locals did not return a successful bounded locals observation")

    stop_index = _find_event_index(events, event_type="observation", name=ActionName.STOP_PDB_SESSION.value, after=locals_index)
    if locals_index is None or stop_index is None:
        reasons.append("no stop_pdb_session observation event was recorded after a successful get_frame_locals")
    else:
        observation = events[stop_index].payload.get("observation", {})
        payload = observation.get("payload", {}) if isinstance(observation, dict) else {}
        evidence["stop_pdb_session"] = payload
        ok = (
            isinstance(observation, dict) and observation.get("status") == "ok"
            and payload.get("stopped") is True
            and payload.get("workspace_removed") is True
        )
        if not ok:
            reasons.append("stop_pdb_session did not report stopped=true and workspace_removed=true")

    if stop_index is None:
        reasons.append("the intentional terminal transition could not be evaluated without a successful stop_pdb_session")
    else:
        transition_index = _find_event_index(events, event_type="transition", name="state_transition", after=stop_index)
        final_index = _find_event_index(events, event_type="final", name="run_finished", after=stop_index)
        terminal_ok = (
            transition_index is not None
            and events[transition_index].payload.get("target_state") == ControllerState.FAILED.value
            and final_index is not None
            and final_index == len(events) - 1
            and events[final_index].payload.get("final_state") == ControllerState.FAILED.value
            and transition_index < final_index
        )
        if not terminal_ok:
            reasons.append("no intentional terminal transition to Failed immediately followed stop_pdb_session")
        else:
            evidence["terminal_transition"] = {
                "reason": events[transition_index].payload.get("reason"),
                "target_state": events[transition_index].payload.get("target_state"),
            }

    return ReachabilitySequenceEvidence(ok=not reasons, reasons=tuple(reasons), steps=evidence)


def validate_events_jsonl(
    events_jsonl: str, *, run_id: Optional[str] = None, task_id: Optional[str] = None,
) -> tuple[bool, tuple[str, ...], tuple[RunEvent, ...]]:
    """Independently validate the serialized event trail: non-empty, every
    line parses and validates as a real ``RunEvent``, all events share one
    consistent run_id/task_id, sequence numbers are contiguous from 0, and
    every required event name is present. This is what actually proves
    "successful event projection/serialization" and "complete, valid,
    non-empty event evidence" rather than merely the absence of an exception
    from ``_project_events``.

    ``run_id``/``task_id`` are optional so this same function can revalidate
    a previously captured ``events_jsonl`` offline, without a live
    ``ControllerRunResult`` to cross-check against; when supplied, they must
    match what the events themselves carry.
    """
    if not events_jsonl.strip():
        return False, ("events_jsonl is empty",), ()
    lines = [line for line in events_jsonl.split("\n") if line]
    if not lines:
        return False, ("events_jsonl has no lines",), ()
    parsed: list[RunEvent] = []
    for line in lines:
        try:
            mapping = json.loads(line)
            event = RunEvent.from_mapping(mapping)
        except Exception as exc:  # noqa: BLE001 - any parse/validation failure fails closed
            return False, (f"events_jsonl line failed to parse/validate: {bounded_error(exc)}",), ()
        parsed.append(event)
    observed_run_ids = {event.run_id for event in parsed}
    observed_task_ids = {event.task_id for event in parsed}
    if len(observed_run_ids) != 1 or len(observed_task_ids) != 1:
        return False, ("events_jsonl does not share a single consistent run_id/task_id",), ()
    if run_id is not None and run_id not in observed_run_ids:
        return False, ("events_jsonl run_id does not match the controller run",), ()
    if task_id is not None and task_id not in observed_task_ids:
        return False, ("events_jsonl task_id does not match the controller run",), ()
    for index, event in enumerate(parsed):
        if event.sequence != index:
            return False, (f"events_jsonl sequence is not contiguous at index {index}",), ()
    names = {event.name for event in parsed}
    missing = _REQUIRED_EVENT_NAMES - names
    if missing:
        return False, (f"events_jsonl is missing required event names: {sorted(missing)}",), ()
    return True, (), tuple(parsed)


def _provenance_present(launch_plan: Optional[PdbLaunchPlan], bundle_hashes: Optional[dict[str, str]]) -> bool:
    return (
        launch_plan is not None
        and bundle_hashes is not None
        and len(bundle_hashes) >= len(_PDB_RUNTIME_MODULES) + 1  # +1 for agentic_debugger/__init__.py
        and all(_SHA256_HEX.fullmatch(value) for value in bundle_hashes.values())
    )


def determine_reachability_verdict(
    *,
    result_present: bool,
    quixbugs_authorized: bool,
    contained_authorized: bool,
    any_gate_allowed: bool,
    sequence_ok: bool,
    events_valid: bool,
    stop_reason_is_failed: bool,
    final_state_is_failed: bool,
    cleanup_succeeded: bool,
    canonical_source_unchanged: bool,
    provenance_present: bool,
    diagnostics_empty: bool,
) -> str:
    """The single, fail-closed PASSED/FAILED decision for one reachability case.

    Every argument is a fact that must independently hold; there is no
    aggregate count or "no exception was raised" shortcut. In particular,
    ``sequence_ok`` (from :func:`evaluate_reachability_sequence_from_events`)
    requires a successful ``start_pdb_session`` paused at the reviewed
    script/breakpoint/function, a successful bounded stack observation, a
    successful bounded frame-locals observation, a successful
    ``stop_pdb_session`` with ``stopped=true``/``workspace_removed=true``,
    and the intentional terminal transition immediately following it --
    ``events_valid`` requires the serialized event trail to be non-empty,
    fully parseable, contiguous, and complete. A caller cannot pass a single
    aggregate observation count in place of these.
    """

    passed = (
        result_present
        and quixbugs_authorized
        and contained_authorized
        and any_gate_allowed
        and sequence_ok
        and events_valid
        and stop_reason_is_failed
        and final_state_is_failed
        and cleanup_succeeded
        and canonical_source_unchanged
        and provenance_present
        and diagnostics_empty
    )
    return "REACHABILITY_CASE_PASSED" if passed else "REACHABILITY_CASE_FAILED"


@dataclass(frozen=True)
class ContainedPdbReachabilityResult:
    task_id: str
    verdict: str
    quixbugs_preflight: QuixPreflightReport
    contained_preflight: Optional[ContainedPdbPreflightReport]
    controller_final_state: Optional[str]
    controller_stop_reason: Optional[str]
    gate_decisions: tuple[dict[str, str], ...]
    pdb_observations: dict[str, int]
    events_jsonl: str
    launch_plan: Optional[dict[str, Any]]
    pdb_runtime_bundle_hashes: Optional[dict[str, str]]
    cleanup_attempted: bool
    cleanup_succeeded: bool
    cleanup_error: Optional[str]
    canonical_source_unchanged: Optional[bool]
    sequence_evidence: Optional[dict[str, Any]] = None
    events_valid: Optional[bool] = None
    events_validation_reasons: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "verdict": self.verdict,
            "quixbugs_preflight": self.quixbugs_preflight.to_mapping(),
            "contained_preflight": self.contained_preflight.to_mapping() if self.contained_preflight else None,
            "controller_final_state": self.controller_final_state,
            "controller_stop_reason": self.controller_stop_reason,
            "gate_decisions": list(self.gate_decisions),
            "pdb_observations": dict(self.pdb_observations),
            "events_jsonl": self.events_jsonl,
            "launch_plan": self.launch_plan,
            "pdb_runtime_bundle_hashes": self.pdb_runtime_bundle_hashes,
            "cleanup_attempted": self.cleanup_attempted,
            "cleanup_succeeded": self.cleanup_succeeded,
            "cleanup_error": self.cleanup_error,
            "canonical_source_unchanged": self.canonical_source_unchanged,
            "sequence_evidence": self.sequence_evidence,
            "events_valid": self.events_valid,
            "events_validation_reasons": list(self.events_validation_reasons),
            "diagnostics": list(self.diagnostics),
        }
