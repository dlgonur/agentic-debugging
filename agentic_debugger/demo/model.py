"""The deterministic offline model used by the Task 9 demonstration.

The MVP has no approved model provider, so this adapter takes the model's
place.  It is intentionally *not* a blind replay of a recorded directive list:
its branch points read the live controller snapshot and the observation the
controller actually received, so the demonstration reacts to real tool results
rather than to assumptions about them.

Two behaviours are genuinely decided at run time:

* whether the failure was reproduced, read from the reproduction observation;
* whether the debugger may be used, decided by the accepted
  :func:`~agentic_debugger.agent.controller_policy.decide_pdb_access` gate over
  the live budget, hypothesis and reproduction state.

Everything else the model "produces" (the localization claim, the root-cause
statement, the candidate diff and the probe expressions) is fixed catalog data.
That is the central limitation of this demonstration and is reported as such:
the two policy variants receive the *same* candidate repair, so any outcome
difference between them can only come from the platform, never from model
reasoning quality.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from agentic_debugger.agent.controller_policy import (
    ActionName,
    HypothesisConfidence,
    PdbGateContext,
    PdbGateDecision,
    PdbPolicy,
    RootCauseHypothesis,
    decide_pdb_access,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    AddHypothesisDirective,
    ControllerSnapshot,
    ModelAdapterError,
    ModelDirective,
    ReviseHypothesisDirective,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.catalog import DemoScenario
from agentic_debugger.events.schema import ObservationStatus

#: Name reported in event metadata and results.  It is not a provider model id.
DEMO_MODEL_NAME = "offline-deterministic-demo"


@dataclass(frozen=True)
class GateRecord:
    """A single recorded invocation of the accepted PDB access gate."""

    policy: str
    allowed: bool
    reason: str
    failure_reproduced: bool
    remaining_pdb_observations: int
    failed_patch_attempts: int
    active_hypothesis_id: Optional[str]
    active_hypothesis_confidence: Optional[str]
    active_hypothesis_requires_runtime_evidence: Optional[bool]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "allowed": self.allowed,
            "reason": self.reason,
            "failure_reproduced": self.failure_reproduced,
            "remaining_pdb_observations": self.remaining_pdb_observations,
            "failed_patch_attempts": self.failed_patch_attempts,
            "active_hypothesis_id": self.active_hypothesis_id,
            "active_hypothesis_confidence": self.active_hypothesis_confidence,
            "active_hypothesis_requires_runtime_evidence":
                self.active_hypothesis_requires_runtime_evidence,
        }


class DemoPolicyModel:
    """A deterministic, provider-free model adapter for one demonstration run."""

    model_name = DEMO_MODEL_NAME

    def __init__(
        self,
        *,
        scenario: DemoScenario,
        patch: str,
        pdb_policy: PdbPolicy,
        rag_context: Optional[Any] = None,
    ) -> None:
        if type(pdb_policy) is not PdbPolicy:
            raise ModelAdapterError("pdb_policy must be a PdbPolicy")
        if type(patch) is not str or not patch:
            raise ModelAdapterError("patch must be a non-empty unified diff")
        self._scenario = scenario
        self._patch = patch
        self._pdb_policy = pdb_policy
        self._rag_context = rag_context
        self.retrieval_record: Optional[dict[str, Any]] = None
        if rag_context is not None:
            # Only a validated RagContext may cross this boundary; arbitrary
            # lookalike objects are rejected (repair 1).
            from agentic_debugger.rag.context import RagContext

            if not isinstance(rag_context, RagContext):
                raise ModelAdapterError(
                    "rag_context must be a validated RagContext"
                )
            record = rag_context.to_record_mapping()
            if type(record) is not dict:
                raise ModelAdapterError("rag_context record must be a mapping")
            self.retrieval_record = record

        self._phase = "reproduce"
        self._symbol_line: Optional[int] = None
        self._pause_generation = 1
        self._eval_index = 0
        self._runtime_failure: Optional[str] = None
        self._static_refs: tuple[str, ...] = ()
        self._collected_refs: list[str] = []
        self._runtime_refs: list[str] = []
        self._observed_values: dict[str, Any] = {}
        self._proof_contract: Optional[dict[str, Any]] = None

        self.calls = 0
        self.gate_records: list[GateRecord] = []
        self.failure_reproduced: Optional[bool] = None
        self.runtime_evidence_collected = False
        self.abort_reason: Optional[str] = None

    # -- observation helpers ----------------------------------------------

    @staticmethod
    def _observation_ok(snapshot: ControllerSnapshot) -> bool:
        observation = snapshot.last_observation
        return observation is not None and observation.status is ObservationStatus.OK

    @staticmethod
    def _payload(snapshot: ControllerSnapshot) -> dict[str, Any]:
        observation = snapshot.last_observation
        if observation is None or type(observation.payload) is not dict:
            return {}
        return observation.payload

    def _remaining_pdb(self, snapshot: ControllerSnapshot) -> int:
        return max(
            0,
            snapshot.budget_limits.max_pdb_observations
            - snapshot.budget_state.pdb_observations,
        )

    @staticmethod
    def _active_hypothesis(snapshot: ControllerSnapshot) -> Optional[RootCauseHypothesis]:
        active = snapshot.hypotheses.active_hypotheses()
        return active[0] if active else None

    # -- gate --------------------------------------------------------------

    def _consult_gate(self, snapshot: ControllerSnapshot) -> PdbGateDecision:
        hypothesis = self._active_hypothesis(snapshot)
        remaining = self._remaining_pdb(snapshot)
        decision = decide_pdb_access(
            self._pdb_policy,
            PdbGateContext(
                # Every input comes from the live snapshot; nothing is assumed.
                source_state=snapshot.state,
                failure_reproduced=bool(self.failure_reproduced),
                remaining_pdb_observations=remaining,
                # The demonstration consults the gate exactly once, before any
                # patch attempt, so this is always zero today. It is passed
                # from live state so a future multi-attempt flow stays correct.
                failed_patch_attempts=snapshot.budget_state.patch_attempts,
                active_hypothesis=hypothesis,
            ),
        )
        self.gate_records.append(
            GateRecord(
                policy=self._pdb_policy.value,
                allowed=decision.allowed,
                reason=decision.reason.value,
                failure_reproduced=bool(self.failure_reproduced),
                remaining_pdb_observations=remaining,
                failed_patch_attempts=snapshot.budget_state.patch_attempts,
                active_hypothesis_id=hypothesis.hypothesis_id if hypothesis else None,
                active_hypothesis_confidence=(
                    hypothesis.confidence.value if hypothesis else None
                ),
                active_hypothesis_requires_runtime_evidence=(
                    hypothesis.requires_runtime_evidence if hypothesis else None
                ),
            )
        )
        return decision

    # -- directive production ---------------------------------------------

    def next_directive(self, snapshot: ControllerSnapshot) -> ModelDirective:
        self.calls += 1
        state = snapshot.state
        if state is ControllerState.REPRODUCE:
            return self._reproduce(snapshot)
        if state is ControllerState.UNDERSTAND:
            return self._understand(snapshot)
        if state is ControllerState.RUNTIME_EVIDENCE:
            return self._runtime(snapshot)
        if state is ControllerState.PATCH:
            return self._patch_phase(snapshot)
        if state is ControllerState.VALIDATE:
            return self._validate(snapshot)
        raise ModelAdapterError(f"offline demonstration model reached {state.value}")

    # -- phases ------------------------------------------------------------

    def _reproduce(self, snapshot: ControllerSnapshot) -> ModelDirective:
        if self._phase == "reproduce":
            self._phase = "reproduce-check"
            return ActionDirective(ActionName.RUN_REPRODUCTION, {"phase": "baseline"})
        if self._phase == "reproduce-check":
            reproduced = self._observation_ok(snapshot) and bool(
                self._payload(snapshot).get("failure_reproduced")
            )
            self.failure_reproduced = reproduced
            if not reproduced:
                self.abort_reason = "failure_not_reproduced"
                return TransitionDirective(
                    ControllerState.FAILED,
                    "declared failure did not reproduce on the untouched baseline",
                )
            self._phase = "understand-locate"
            return TransitionDirective(
                ControllerState.UNDERSTAND, "declared failure reproduced"
            )
        raise ModelAdapterError(f"unexpected reproduce phase {self._phase!r}")

    def _understand(self, snapshot: ControllerSnapshot) -> ModelDirective:
        scenario = self._scenario
        if self._phase == "understand-locate":
            self._phase = "understand-window"
            return ActionDirective(
                ActionName.FIND_FUNCTION,
                {
                    "name": scenario.localization.symbol,
                    "path": scenario.localization.file_path,
                },
            )
        if self._phase == "understand-window":
            if not self._observation_ok(snapshot):
                self.abort_reason = "symbol_not_located"
                return TransitionDirective(
                    ControllerState.FAILED,
                    "declared defect symbol could not be located statically",
                )
            start_line = self._payload(snapshot).get("start_line")
            self._symbol_line = start_line if type(start_line) is int and start_line > 0 else 1
            self._static_refs = ("observation:find_function",)
            self._phase = "understand-hypothesis"
            return ActionDirective(
                ActionName.GET_SOURCE_WINDOW,
                {"path": scenario.localization.file_path, "line": self._symbol_line},
            )
        if self._phase == "understand-hypothesis":
            # Cite only the static observations that actually succeeded.
            if self._observation_ok(snapshot):
                self._static_refs = self._static_refs + ("observation:get_source_window",)
            self._phase = "understand-declare"
            return AddHypothesisDirective(
                scenario.hypothesis_id,
                scenario.root_cause_statement,
                HypothesisConfidence.LOW,
                self._static_refs,
                True,
            )
        if self._phase == "understand-declare":
            if self._scenario.runtime_probe.exact_public_reproduction:
                # The exact proof path must create its diagnosis only after
                # the real PDB observations.  The active hypothesis already
                # exists, so the controller's authoritative runtime gate can
                # be consumed without a deliberately rejected diagnosis.
                self._phase = "runtime-start"
                return TransitionDirective(
                    ControllerState.RUNTIME_EVIDENCE,
                    "exact-runtime proof requires PDB evidence before diagnosis",
                )
            self._phase = "understand-gate"
            return ActionDirective(
                ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS,
                {
                    "hypothesis_id": scenario.hypothesis_id,
                    "statement": scenario.root_cause_statement,
                    "target_file": scenario.localization.file_path,
                    "target_symbol": scenario.localization.symbol,
                    "confidence": HypothesisConfidence.LOW.value,
                },
            )
        if self._phase == "understand-gate":
            if self._scenario.runtime_probe.exact_public_reproduction:
                self._phase = "runtime-start"
                return TransitionDirective(
                    ControllerState.RUNTIME_EVIDENCE,
                    "exact-runtime proof requires PDB evidence before diagnosis",
                )
            decision = self._consult_gate(snapshot)
            if decision.allowed:
                self._phase = "runtime-start"
                return TransitionDirective(
                    ControllerState.RUNTIME_EVIDENCE,
                    f"pdb gate allowed runtime evidence ({decision.reason.value})",
                )
            self._phase = "patch-apply"
            return TransitionDirective(
                ControllerState.PATCH,
                f"pdb gate withheld runtime evidence ({decision.reason.value})",
            )
        if self._phase == "understand-revise":
            # The offline model's diagnosis is fixed before the run, so the
            # revision records only a fact it can support: the runtime evidence
            # it asked for has now been collected. The statement is unchanged
            # and the confidence is NOT raised, because nothing in this
            # demonstration evaluates the evidence against the diagnosis.
            self._phase = (
                "understand-diagnose"
                if self._scenario.runtime_probe.exact_public_reproduction
                else "understand-runtime-collected"
            )
            return ReviseHypothesisDirective(
                self._scenario.hypothesis_id,
                self._scenario.root_cause_statement,
                HypothesisConfidence.LOW,
                tuple(self._static_refs) + tuple(self._collected_refs) + tuple(self._runtime_refs),
                False,
            )
        if self._phase == "understand-diagnose":
            self._phase = "understand-runtime-collected"
            return ActionDirective(
                ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS,
                {
                    "hypothesis_id": self._scenario.hypothesis_id,
                    "statement": self._scenario.root_cause_statement,
                    "target_file": self._scenario.localization.file_path,
                    "target_symbol": self._scenario.localization.symbol,
                    "confidence": HypothesisConfidence.LOW.value,
                    "evidence_refs": list(self._runtime_refs),
                    "observed_values": dict(self._observed_values),
                },
            )
        if self._phase == "understand-runtime-collected":
            self._phase = "patch-apply"
            return TransitionDirective(
                ControllerState.PATCH,
                "requested runtime evidence collected; diagnosis unchanged",
            )
        if self._phase == "understand-runtime-failed":
            self._phase = "patch-apply"
            return TransitionDirective(
                ControllerState.PATCH,
                "runtime evidence unavailable; continuing on static evidence",
            )
        raise ModelAdapterError(f"unexpected understand phase {self._phase!r}")

    def _runtime(self, snapshot: ControllerSnapshot) -> ModelDirective:
        probe = self._scenario.runtime_probe
        if self._phase == "runtime-start":
            self._phase = "runtime-stack"
            arguments = (
                {"breakpoint_line": probe.breakpoint_line}
                if probe.exact_public_reproduction
                else {}
            )
            return ActionDirective(ActionName.START_PDB_SESSION, arguments)
        if self._phase == "runtime-stack":
            if not self._observation_ok(snapshot):
                return self._abandon_runtime("pdb session did not reach the breakpoint")
            if probe.exact_public_reproduction:
                self._remember_runtime_observation(snapshot)
                self._proof_contract = dict(self._payload(snapshot).get("proof") or {})
            self._phase = "runtime-locals"
            return ActionDirective(ActionName.GET_STACK_SUMMARY, {})
        if self._phase == "runtime-locals":
            if not self._observation_ok(snapshot):
                return self._abandon_runtime("stack summary was not available")
            if probe.exact_public_reproduction:
                self._remember_runtime_observation(snapshot)
            else:
                self._record_collected("get_stack_summary")
            generation = self._payload(snapshot).get("pause_generation")
            if type(generation) is int and generation > 0:
                self._pause_generation = generation
            self._phase = "runtime-eval"
            return ActionDirective(
                ActionName.GET_FRAME_LOCALS,
                {"frame_id": 0, "pause_generation": self._pause_generation},
            )
        if self._phase == "runtime-eval":
            # Each expression is issued one at a time so that every single
            # observation is inspected before the next directive is produced.
            if not self._observation_ok(snapshot):
                return self._abandon_runtime(
                    "frame locals were not available"
                    if self._eval_index == 0
                    else "restricted expression evaluation failed"
                )
            if probe.exact_public_reproduction:
                self._remember_runtime_observation(snapshot)
                for item in self._payload(snapshot).get("locals", []):
                    if (
                        type(item) is dict
                        and item.get("name") in probe.inspect_expressions
                    ):
                        self._observed_values[item["name"]] = item.get("value")
                self._phase = "runtime-step"
                return ActionDirective(ActionName.NEXT_PDB_SESSION, {})
            self._record_collected(
                "get_frame_locals" if self._eval_index == 0 else "safe_eval_expression"
            )
            if self._eval_index < len(probe.inspect_expressions):
                expression = probe.inspect_expressions[self._eval_index]
                self._eval_index += 1
                return ActionDirective(
                    ActionName.SAFE_EVAL_EXPRESSION,
                    {
                        "frame_id": 0,
                        "pause_generation": self._pause_generation,
                        "expression": expression,
                    },
                )
            self.runtime_evidence_collected = True
            self._phase = "runtime-exit"
            return ActionDirective(ActionName.STOP_PDB_SESSION, {})
        if self._phase == "runtime-step":
            if not self._observation_ok(snapshot):
                return self._abandon_runtime("step did not produce a valid runtime pause")
            self._remember_runtime_observation(snapshot)
            self.runtime_evidence_collected = True
            self._phase = "runtime-exit"
            return ActionDirective(ActionName.STOP_PDB_SESSION, {})
        if self._phase == "runtime-exit":
            if self.runtime_evidence_collected:
                self._phase = "understand-revise"
                return TransitionDirective(
                    ControllerState.UNDERSTAND, "bounded runtime evidence collected"
                )
            self._phase = "understand-runtime-failed"
            return TransitionDirective(
                ControllerState.UNDERSTAND,
                f"runtime evidence abandoned ({self._runtime_failure or 'unknown reason'})",
            )
        raise ModelAdapterError(f"unexpected runtime phase {self._phase!r}")

    def _record_collected(self, name: str) -> None:
        reference = f"observation:{name}"
        if reference not in self._collected_refs:
            self._collected_refs.append(reference)

    def _remember_runtime_observation(self, snapshot: ControllerSnapshot) -> None:
        observation = snapshot.last_observation
        if observation is not None and observation.observation_id not in self._runtime_refs:
            self._runtime_refs.append(observation.observation_id)

    def _abandon_runtime(self, reason: str) -> ModelDirective:
        """Always release the debugger before leaving RuntimeEvidence."""

        self._runtime_failure = reason
        self.runtime_evidence_collected = False
        self._phase = "runtime-exit"
        return ActionDirective(ActionName.STOP_PDB_SESSION, {})

    def _patch_phase(self, snapshot: ControllerSnapshot) -> ModelDirective:
        if self._phase == "patch-apply":
            self._phase = "patch-syntax"
            return ActionDirective(ActionName.APPLY_PATCH, {"patch": self._patch})
        if self._phase == "patch-syntax":
            if not self._observation_ok(snapshot) or not self._payload(snapshot).get("applied"):
                self.abort_reason = "patch_not_applied"
                return TransitionDirective(
                    ControllerState.FAILED,
                    "candidate patch did not apply to the disposable workspace",
                )
            self._phase = "patch-validate"
            return ActionDirective(ActionName.SYNTAX_CHECK, {})
        if self._phase == "patch-validate":
            if not self._observation_ok(snapshot) or not self._payload(snapshot).get("all_passed"):
                self.abort_reason = "syntax_check_failed"
                return TransitionDirective(
                    ControllerState.FAILED, "patched source failed syntax validation"
                )
            self._phase = "validate-reproduce"
            return TransitionDirective(
                ControllerState.VALIDATE, "candidate patch applied and syntax checked"
            )
        raise ModelAdapterError(f"unexpected patch phase {self._phase!r}")

    def _validate(self, snapshot: ControllerSnapshot) -> ModelDirective:
        if self._phase == "validate-reproduce":
            self._phase = "validate-regression"
            return ActionDirective(ActionName.RUN_REPRODUCTION, {"phase": "post_patch"})
        if self._phase == "validate-regression":
            if not self._observation_ok(snapshot):
                self.abort_reason = "post_patch_reproduction_failed"
                return TransitionDirective(
                    ControllerState.FAILED, "post-patch reproduction could not be executed"
                )
            self._phase = "validate-classify"
            return ActionDirective(ActionName.RUN_REGRESSION_TESTS, {})
        if self._phase == "validate-classify":
            if not self._observation_ok(snapshot):
                self.abort_reason = "regression_execution_failed"
                return TransitionDirective(
                    ControllerState.FAILED, "designated regression tests could not be executed"
                )
            self._phase = "validate-finish"
            return ActionDirective(ActionName.CLASSIFY_OUTCOME, {})
        if self._phase == "validate-finish":
            outcome = self._payload(snapshot).get("outcome")
            if self._observation_ok(snapshot) and outcome == "RESOLVED":
                self._phase = "finished"
                return TransitionDirective(
                    ControllerState.DONE,
                    "controller validation classified the candidate as RESOLVED",
                )
            self.abort_reason = f"controller_outcome_{outcome or 'unclassified'}"
            self._phase = "finished"
            return TransitionDirective(
                ControllerState.FAILED,
                f"controller validation classified the candidate as {outcome or 'unclassified'}",
            )
        raise ModelAdapterError(f"unexpected validate phase {self._phase!r}")


__all__ = ["DEMO_MODEL_NAME", "DemoPolicyModel", "GateRecord"]
