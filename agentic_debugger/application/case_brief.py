"""Pure Evidence Review projection over immutable session presentation state.

The brief is a navigation aid, not a second correctness system.  Every field
is copied from :class:`SessionViewState` or conservatively marked pending/not
recorded.  In particular, a diagnosis and an applied patch are controller
facts; only ``verifier.completed`` may produce a correctness verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from agentic_debugger.application.events import (
    SessionStatus,
    VerifierStage,
    VerifierStageStatus,
)
from agentic_debugger.application.presentation import (
    PatchStage,
    SessionViewState,
)


class EvidenceStageKind(str, Enum):
    REPRODUCE = "reproduce"
    INSPECT = "inspect"
    DIAGNOSE = "diagnose"
    CHANGE = "change"
    VERIFY = "verify"
    CLEANUP = "cleanup"


class EvidenceStageState(str, Enum):
    PROVEN = "proven"
    RECORDED = "recorded"
    FAILED = "failed"
    PENDING = "pending"
    NOT_RECORDED = "not_recorded"
    NOT_REQUIRED = "not_required"


@dataclass(frozen=True)
class EvidenceStage:
    kind: EvidenceStageKind
    state: EvidenceStageState
    title: str
    detail: str
    references: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseBrief:
    """One bounded six-stage review of the recorded evidence prefix."""

    task_id: str
    verdict: str
    verdict_authoritative: bool
    stages: Tuple[EvidenceStage, ...]

    def stage(self, kind: EvidenceStageKind) -> EvidenceStage:
        for item in self.stages:
            if item.kind is kind:
                return item
        raise KeyError(kind)


_TERMINAL_STATUSES = frozenset(
    {
        SessionStatus.SUCCEEDED,
        SessionStatus.UNRESOLVED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
        SessionStatus.TIMED_OUT,
        SessionStatus.INTERRUPTED,
        SessionStatus.CLEANUP_FAILED,
    }
)


def _waiting_state(view: SessionViewState) -> EvidenceStageState:
    return (
        EvidenceStageState.NOT_RECORDED
        if view.status in _TERMINAL_STATUSES
        else EvidenceStageState.PENDING
    )


def _bounded(value: Optional[str], maximum: int = 180) -> str:
    if not value:
        return "not recorded"
    normalized = " ".join(value.split())
    if len(normalized) <= maximum:
        return normalized
    return normalized[: maximum - 1] + "…"


def _verifier_stage_state(
    view: SessionViewState, stage: VerifierStage
) -> Optional[VerifierStageStatus]:
    for item in reversed(view.verifier_stages):
        if item.stage is stage:
            return item.status
    return None


def _reproduction_stage(view: SessionViewState) -> EvidenceStage:
    summary = view.verifier_summary
    progress = _verifier_stage_state(view, VerifierStage.BASELINE_REPRODUCTION)
    if summary is not None and summary.status == "COMPLETED":
        return EvidenceStage(
            EvidenceStageKind.REPRODUCE,
            EvidenceStageState.PROVEN,
            "Original failure reproduced",
            "The independent verifier accepted a genuine failing baseline in a clean workspace.",
            ("verifier.completed", "verifier.stage.baseline_reproduction"),
        )
    if progress is VerifierStageStatus.FAILED or (
        summary is not None and summary.status == "BASELINE_INVALID"
    ):
        return EvidenceStage(
            EvidenceStageKind.REPRODUCE,
            EvidenceStageState.FAILED,
            "Baseline proof failed",
            "The verifier did not accept the reported bug as a genuine reproducible failure.",
            ("verifier.stage.baseline_reproduction",),
        )
    if progress is not None:
        return EvidenceStage(
            EvidenceStageKind.REPRODUCE,
            EvidenceStageState.PENDING,
            "Reproducing original failure",
            f"Verifier baseline stage is {progress.value}.",
            ("verifier.stage.baseline_reproduction",),
        )
    return EvidenceStage(
        EvidenceStageKind.REPRODUCE,
        _waiting_state(view),
        "Original failure",
        "Pending" if not view.status.terminal else "Not recorded",
    )


def _inspection_stage(view: SessionViewState) -> EvidenceStage:
    debugger = view.debugger
    if view.pdb_observed:
        location = (
            f"{debugger.script or 'unknown file'}:{debugger.line or '?'}"
            f" in {debugger.function or 'unknown function'}"
        )
        detail = f"Runtime state observed at {location}; {len(debugger.locals)} local value(s) recorded."
        references = tuple(
            item
            for item in (
                f"{debugger.script}:{debugger.line}"
                if debugger.script and debugger.line is not None
                else None,
                *(
                    f"local:{local.name}"
                    for local in debugger.locals[:6]
                ),
            )
            if item is not None
        )
        return EvidenceStage(
            EvidenceStageKind.INSPECT,
            EvidenceStageState.RECORDED,
            "Runtime state inspected with PDB",
            detail,
            references,
        )
    if debugger.session_started:
        return EvidenceStage(
            EvidenceStageKind.INSPECT,
            EvidenceStageState.RECORDED,
            "Debugger session recorded",
            "Debugger active, awaiting observation.",
            ("debugger.started",),
        )
    return EvidenceStage(
        EvidenceStageKind.INSPECT,
        _waiting_state(view),
        "Runtime inspection",
        "Pending" if not view.status.terminal else "Not recorded",
    )


def _diagnosis_stage(view: SessionViewState) -> EvidenceStage:
    diagnosis = view.diagnosis
    if diagnosis is None:
        return EvidenceStage(
            EvidenceStageKind.DIAGNOSE,
            _waiting_state(view),
            "Controller diagnosis",
            "Pending" if not view.status.terminal else "Not recorded",
        )
    target = diagnosis.symbol or diagnosis.file_path
    detail = _bounded(diagnosis.text)
    if target:
        detail = f"{detail} Target: {target}."
    if diagnosis.confidence:
        detail = f"{detail} Confidence: {diagnosis.confidence}."
    return EvidenceStage(
        EvidenceStageKind.DIAGNOSE,
        EvidenceStageState.RECORDED,
        "Controller claim recorded",
        detail,
        tuple(diagnosis.evidence_refs),
    )


def _change_stage(view: SessionViewState) -> EvidenceStage:
    if not view.patch_attempts:
        return EvidenceStage(
            EvidenceStageKind.CHANGE,
            _waiting_state(view),
            "Candidate change",
            "Pending" if not view.status.terminal else "Not recorded",
        )
    attempt = view.patch_attempts[-1]
    changed = ", ".join(attempt.changed_files) or "changed files not recorded"
    failed = attempt.stage in {
        PatchStage.REJECTED,
        PatchStage.APPLY_FAILED,
        PatchStage.REVERTED,
    }
    return EvidenceStage(
        EvidenceStageKind.CHANGE,
        EvidenceStageState.FAILED if failed else EvidenceStageState.RECORDED,
        f"Candidate attempt {attempt.attempt_index + 1}: {attempt.stage.value}",
        f"{changed}. Patch state is not a correctness verdict.",
        tuple(attempt.changed_files),
    )


def _verification_stage(view: SessionViewState) -> EvidenceStage:
    summary = view.verifier_summary
    if summary is None:
        state = (
            EvidenceStageState.PENDING
            if view.verifier_stages or view.status not in _TERMINAL_STATUSES
            else EvidenceStageState.NOT_RECORDED
        )
        return EvidenceStage(
            EvidenceStageKind.VERIFY,
            state,
            "Independent verification",
            "Pending" if not view.status.terminal else "Not recorded",
        )
    outcome = summary.outcome.value if summary.outcome is not None else "no outcome"
    f2p = (
        f"{summary.f2p_passed}/{summary.f2p_total}"
        if summary.f2p_passed is not None and summary.f2p_total is not None
        else "not recorded"
    )
    p2p = (
        f"{summary.p2p_passed}/{summary.p2p_total}"
        if summary.p2p_passed is not None and summary.p2p_total is not None
        else "not recorded"
    )
    resolved = summary.status == "COMPLETED" and outcome == "RESOLVED"
    return EvidenceStage(
        EvidenceStageKind.VERIFY,
        EvidenceStageState.PROVEN if resolved else EvidenceStageState.FAILED,
        f"Authoritative verdict: {outcome}",
        f"Verifier status {summary.status or 'not recorded'}; fail-to-pass {f2p}; pass-to-pass {p2p}.",
        ("verifier.completed",),
    )


def _cleanup_stage(view: SessionViewState) -> EvidenceStage:
    summary = view.verifier_summary
    verifier_cleaned = summary.workspace_cleaned if summary is not None else None
    if view.cleanup_verified is True:
        suffix = (
            " Verifier workspace cleanup was also recorded."
            if verifier_cleaned is True
            else ""
        )
        return EvidenceStage(
            EvidenceStageKind.CLEANUP,
            EvidenceStageState.PROVEN,
            "Cleanup integrity verified",
            "Session-owned disposable resources were removed and verified."
            + suffix,
            ("cleanup.completed",),
        )
    if view.cleanup_not_required:
        return EvidenceStage(
            EvidenceStageKind.CLEANUP,
            EvidenceStageState.NOT_REQUIRED,
            "Cleanup not required",
            "The recorded execution created no disposable resources.",
            ("cleanup.not_required",),
        )
    if view.cleanup_verified is False:
        return EvidenceStage(
            EvidenceStageKind.CLEANUP,
            EvidenceStageState.FAILED,
            "Cleanup integrity failed",
            "Disposable resource removal was attempted but not verified.",
            ("cleanup.completed",),
        )
    return EvidenceStage(
        EvidenceStageKind.CLEANUP,
        _waiting_state(view),
        "Cleanup integrity",
        "Pending" if not view.status.terminal else "Not recorded",
    )


def project_case_brief(view: SessionViewState) -> CaseBrief:
    """Project one honest case brief from the current replay/live prefix."""
    if not isinstance(view, SessionViewState):
        raise TypeError("view must be a SessionViewState")
    summary = view.verifier_summary
    authoritative = summary is not None
    if summary is None:
        verdict = "Awaiting verification"
    elif summary.outcome is not None:
        verdict = summary.outcome.value
    else:
        verdict = summary.status or "Verifier result incomplete"
    return CaseBrief(
        task_id=view.task_id,
        verdict=verdict,
        verdict_authoritative=authoritative,
        stages=(
            _reproduction_stage(view),
            _inspection_stage(view),
            _diagnosis_stage(view),
            _change_stage(view),
            _verification_stage(view),
            _cleanup_stage(view),
        ),
    )


__all__ = [
    "CaseBrief",
    "EvidenceStage",
    "EvidenceStageKind",
    "EvidenceStageState",
    "project_case_brief",
]
