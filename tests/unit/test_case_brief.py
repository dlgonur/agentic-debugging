from dataclasses import replace

from agentic_debugger.application.case_brief import (
    EvidenceStageKind,
    EvidenceStageState,
    project_case_brief,
)
from agentic_debugger.application.events import (
    SessionStatus,
    SourceKind,
    VerifierStage,
    VerifierStageStatus,
)
from agentic_debugger.application.presentation import (
    DebuggerViewState,
    DiagnosisView,
    LocalRecord,
    PatchAttemptView,
    PatchStage,
    PresentationIdentity,
    VerifierStageView,
    VerifierSummaryView,
    initial_session_view,
)
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome


def _view():
    return initial_session_view(
        PresentationIdentity(
            task_id="case-1",
            source_kind=SourceKind.OFFLINE_DEMO,
            session_id="sess-case-0001",
        )
    )


def _resolved_view():
    return replace(
        _view(),
        status=SessionStatus.SUCCEEDED,
        pdb_observed=True,
        debugger=DebuggerViewState(
            script="buggy.py",
            line=12,
            function="compute",
            locals=(LocalRecord("value", "4"),),
            session_started=True,
        ),
        diagnosis=DiagnosisView(
            text="The boundary drops one item.",
            file_path="buggy.py",
            symbol="compute",
            confidence="high",
            evidence_refs=("debugger.location:12", "debugger.local:value"),
        ),
        patch_attempts=(
            PatchAttemptView(
                attempt_index=0,
                stage=PatchStage.VERIFIED,
                changed_files=("buggy.py",),
            ),
        ),
        verifier_stages=(
            VerifierStageView(
                VerifierStage.BASELINE_REPRODUCTION,
                VerifierStageStatus.COMPLETED,
            ),
        ),
        verifier_summary=VerifierSummaryView(
            status="COMPLETED",
            outcome=SemanticOutcome.RESOLVED,
            f2p_passed=1,
            f2p_total=1,
            p2p_passed=3,
            p2p_total=3,
            workspace_cleaned=True,
        ),
        cleanup_verified=True,
    )


def test_resolved_brief_preserves_six_stage_evidence_chain():
    brief = project_case_brief(_resolved_view())
    assert tuple(stage.kind for stage in brief.stages) == tuple(EvidenceStageKind)
    assert brief.verdict == "RESOLVED"
    assert brief.verdict_authoritative is True
    assert brief.stage(EvidenceStageKind.REPRODUCE).state is EvidenceStageState.PROVEN
    assert brief.stage(EvidenceStageKind.INSPECT).state is EvidenceStageState.RECORDED
    diagnosis = brief.stage(EvidenceStageKind.DIAGNOSE)
    assert diagnosis.state is EvidenceStageState.RECORDED
    assert diagnosis.references == (
        "debugger.location:12",
        "debugger.local:value",
    )
    assert brief.stage(EvidenceStageKind.CHANGE).state is EvidenceStageState.RECORDED
    assert brief.stage(EvidenceStageKind.VERIFY).state is EvidenceStageState.PROVEN
    assert brief.stage(EvidenceStageKind.CLEANUP).state is EvidenceStageState.PROVEN


def test_controller_claim_and_applied_patch_never_become_verifier_proof():
    view = replace(
        _view(),
        status=SessionStatus.SUCCEEDED,
        diagnosis=DiagnosisView(text="I think this is fixed", confidence="high"),
        patch_attempts=(PatchAttemptView(0, PatchStage.APPLIED),),
        cleanup_verified=True,
    )
    brief = project_case_brief(view)
    assert brief.verdict_authoritative is False
    assert brief.stage(EvidenceStageKind.DIAGNOSE).state is EvidenceStageState.RECORDED
    assert brief.stage(EvidenceStageKind.CHANGE).state is EvidenceStageState.RECORDED
    assert brief.stage(EvidenceStageKind.VERIFY).state is EvidenceStageState.NOT_RECORDED


def test_non_resolved_verifier_result_is_authoritative_failure():
    view = replace(
        _resolved_view(),
        status=SessionStatus.UNRESOLVED,
        verifier_summary=VerifierSummaryView(
            status="COMPLETED",
            outcome=SemanticOutcome.REGRESSION,
            f2p_passed=0,
            f2p_total=1,
            p2p_passed=0,
            p2p_total=2,
            workspace_cleaned=True,
        ),
    )
    brief = project_case_brief(view)
    verification = brief.stage(EvidenceStageKind.VERIFY)
    assert brief.verdict == "REGRESSION"
    assert brief.verdict_authoritative is True
    assert verification.state is EvidenceStageState.FAILED
    assert "fail-to-pass 0/1" in verification.detail
    assert "pass-to-pass 0/2" in verification.detail


def test_replay_prefix_degrades_to_pending_without_inference():
    brief = project_case_brief(_view())
    assert brief.verdict_authoritative is False
    assert all(stage.state is EvidenceStageState.PENDING for stage in brief.stages)


def test_terminal_missing_evidence_and_cleanup_not_required_are_explicit():
    view = replace(
        _view(),
        status=SessionStatus.CANCELLED,
        cleanup_not_required=True,
    )
    brief = project_case_brief(view)
    assert brief.stage(EvidenceStageKind.REPRODUCE).state is EvidenceStageState.NOT_RECORDED
    assert brief.stage(EvidenceStageKind.INSPECT).state is EvidenceStageState.NOT_RECORDED
    assert brief.stage(EvidenceStageKind.VERIFY).state is EvidenceStageState.NOT_RECORDED
    assert brief.stage(EvidenceStageKind.CLEANUP).state is EvidenceStageState.NOT_REQUIRED
