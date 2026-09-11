"""Immutable presentation view records and identity contracts.

This module owns the immutable presentation model dataclasses derived only
from application-owned session events — debugger/patch/verifier/source/
diagnosis views, the timeline entry, model provenance, and the aggregate
``SessionViewState`` — together with ``PresentationIdentity`` and its
initialization paths.

Absent historical data is represented honestly: optional fields stay
``None``/empty rather than being reconstructed, matching the ``NOT
RECORDED`` display rule for recorded material.

Dependency rule: pure data contracts; imports only event vocabulary,
``SessionSpec``, and the token-usage fold model.  No I/O, no reducer
logic, no controller/PDB/patch/verifier/model state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.event_contracts import (
    OperatorStage,
    SessionEventKind,
    SessionPhase,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
    SourceSnapshotStage,
    VerifierStage,
    VerifierStageStatus,
    validate_session_id,
)
from agentic_debugger.application.presentation_tokens import SessionTokenUsage
from agentic_debugger.application.session import SessionSpec
from agentic_debugger.application.workstream import WorkstreamEntry
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome

#: Deterministic tail cap for the derived activity timeline.
MAX_TIMELINE_ENTRIES = 2000


@dataclass(frozen=True)
class FrameRecord:
    """One bounded presentation copy of a recorded stack frame."""

    index: int
    function: str
    file: str
    line: int
    is_current: bool


@dataclass(frozen=True)
class LocalRecord:
    """One bounded presentation copy of a recorded local summary."""

    name: str
    summary: str


@dataclass(frozen=True)
class DebuggerViewState:
    """Recorded debugger presentation data (never live PDB state).

    ``pause_generation`` is nullable (Repair Pass 3): historical traces that
    never recorded a generation stay ``None`` (``NOT RECORDED``) instead of
    receiving a synthesized counter.
    """

    script: Optional[str] = None
    line: Optional[int] = None
    function: Optional[str] = None
    pause_generation: Optional[int] = None
    frames: Tuple[FrameRecord, ...] = ()
    locals: Tuple[LocalRecord, ...] = ()
    breakpoints: Tuple[str, ...] = ()
    session_started: bool = False


class PatchStage(str, Enum):
    """Presentation stage of one normalized patch attempt.

    ``VERIFIED`` means the independent verifier completed and the applied
    candidate was part of that completed evaluation; it never means the
    repair is correct.  ``APPLY_FAILED`` is the real PatchManager apply
    failure path (distinct from a validation/authorization ``REJECTED``).
    """

    PROPOSED = "proposed"
    REJECTED = "rejected"
    APPLY_FAILED = "apply_failed"
    APPLIED = "applied"
    REVERTED = "reverted"
    VERIFIED = "verified"


@dataclass(frozen=True)
class PatchAttemptView:
    """One normalized patch attempt in presentation order."""

    attempt_index: int
    stage: PatchStage
    patch_sha256: Optional[str] = None
    patch_text: Optional[str] = None
    changed_files: Tuple[str, ...] = ()
    syntax_passed: Optional[bool] = None
    rejection_reason: Optional[str] = None
    apply_failure_reason: Optional[str] = None


@dataclass(frozen=True)
class VerifierStageView:
    """Informational progress of one verifier stage."""

    stage: VerifierStage
    status: VerifierStageStatus


@dataclass(frozen=True)
class VerifierSummaryView:
    """Final verifier summary copied from the terminal verifier event."""

    status: Optional[str]
    outcome: Optional[SemanticOutcome]
    f2p_passed: Optional[int]
    f2p_total: Optional[int]
    p2p_passed: Optional[int]
    p2p_total: Optional[int]
    workspace_cleaned: Optional[bool]
    classification: Optional[str] = None
    official_test_execution_proven: Optional[bool] = None


@dataclass(frozen=True)
class SourceView:
    """One bounded app-owned source snapshot (initial/applied/reverted).

    ``text`` is the bounded captured content; ``truncated=True`` means the
    original file exceeded the capture bound and only a prefix is present.
    ``line_count`` counts the captured text.  The logical ``path`` is the
    repository/workspace-relative identity used by debugger location events.
    """

    path: str
    sha256: str
    text: str
    line_count: int
    truncated: bool
    stage: SourceSnapshotStage


@dataclass(frozen=True)
class DiagnosisView:
    """Recorded diagnosis presentation copy (never chain-of-thought).

    ``observed_values`` is the bounded structured mapping the durable
    ``diagnosis.recorded`` event carried (for example the Local Project
    session's recorded repository basename and source HEAD).  It is copied
    verbatim from the event payload, never inferred.
    """

    text: Optional[str] = None
    file_path: Optional[str] = None
    symbol: Optional[str] = None
    confidence: Optional[str] = None
    observed_values: Optional[Mapping[str, Any]] = None
    evidence_refs: Tuple[str, ...] = ()
    proof_contract: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class TimelineEntry:
    """One derived activity timeline entry."""

    sequence: int
    event_kind: SessionEventKind
    summary: str
    timestamp_utc: Optional[str] = None
    duration_seconds: Optional[float] = None
    operation_key: Optional[str] = None


@dataclass(frozen=True)
class ModelProvenanceView:
    """Safe recorded provenance of a configured command-model session.

    Carries only the safe fields recorded by ``model.configured``: the
    selected profile id, the safe configuration fingerprint, the display
    label, and protocol/tool version metadata.  It never carries the
    executable, argv, or environment values, and it never claims a provider
    or model identity merely because a command was named that way.
    """

    profile_id: Optional[str] = None
    config_fingerprint: Optional[str] = None
    display_name: Optional[str] = None
    protocol_version: Optional[str] = None
    tool_version: Optional[str] = None
    treatment_revision: Optional[int] = None
    treatment_id: Optional[str] = None
    result_location: Optional[str] = None


@dataclass(frozen=True)
class SessionViewState:
    """Immutable presentation state of one session or replay."""

    session_id: Optional[str] = None
    task_id: str = ""
    source_kind: SourceKind = SourceKind.OFFLINE_DEMO
    status: SessionStatus = SessionStatus.CREATED
    phase: Optional[SessionPhase] = None
    controller_phase: Optional[ControllerState] = None
    run_id: Optional[str] = None
    termination_reason: Optional[SessionTerminationReason] = None
    debugger: DebuggerViewState = DebuggerViewState()
    patch_attempts: Tuple[PatchAttemptView, ...] = ()
    verifier_stages: Tuple[VerifierStageView, ...] = ()
    verifier_summary: Optional[VerifierSummaryView] = None
    diagnosis: Optional[DiagnosisView] = None
    sources: Tuple[SourceView, ...] = ()
    cleanup_verified: Optional[bool] = None
    #: Explicit positive proof that cleanup was not required because no
    #: disposable runtime resources were ever created. Presence means
    #: "Not required"; absence means unknown unless proven. Never inferred
    #: from missing timeline entries.
    cleanup_not_required: bool = False
    model_provenance: Optional[ModelProvenanceView] = None
    operator_stage: Optional[OperatorStage] = None
    #: Typed operational facts retained by the reducer so live widgets never
    #: parse display summaries.  They are derived exclusively from v1 events.
    latest_model_request_index: Optional[int] = None
    outstanding_model_request_index: Optional[int] = None
    #: Last typed model-request failure kind (e.g. malformed_directive,
    #: illegal_action, invalid_argument) retained so a terminal session can
    #: show the typed cause instead of only a generic "model error".
    latest_model_error_kind: Optional[str] = None
    latest_model_error_message: Optional[str] = None
    latest_controller_step_index: Optional[int] = None
    current_tool_name: Optional[str] = None
    #: Last structured target a tool event carried (e.g. a source range).
    #: Cleared only when a later tool event carries a different/absent
    #: target; it never claims more than the producing boundary recorded.
    current_tool_target: Optional[str] = None
    pdb_observed: bool = False
    #: Cumulative provider-reported token usage over completed model
    #: requests (reducer-derived from durable events; never a second
    #: journal authority).
    token_usage: SessionTokenUsage = SessionTokenUsage()
    #: Typed official-verifier milestone: True only after the operator
    #: observed real official test execution (never inferred from stage).
    official_execution_proven: Optional[bool] = None
    timeline: Tuple[TimelineEntry, ...] = ()
    #: Curated operational workstream (semantic work units, bounded).  The
    #: complete forensic ledger remains ``timeline``; this is presentation
    #: curation over the same durable facts, never a second authority.
    workstream: Tuple[WorkstreamEntry, ...] = ()


def _initial_debugger() -> DebuggerViewState:
    return DebuggerViewState()


@dataclass(frozen=True)
class PresentationIdentity:
    """Immutable presentation identity of one live or recorded session.

    ``source_kind`` may be any live or recorded :class:`SourceKind`;
    recorded kinds initialize replay presentation without weakening the
    live-start rules of :class:`ExecutionSourceSpec`.  ``session_id`` is
    optional at initialization: when absent it is bound from the first
    reduced event (the ``session.created`` event of a valid stream).
    """

    task_id: str
    source_kind: SourceKind
    session_id: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.source_kind) is not SourceKind:
            raise ApplicationInputError("source_kind must be a SourceKind")
        _validate_task_id(self.task_id)
        if self.session_id is not None:
            try:
                validate_session_id(self.session_id)
            except Exception as exc:
                raise ApplicationInputError(
                    f"invalid session id: {self.session_id!r}"
                ) from exc


def _validate_task_id(value: str) -> None:
    if type(value) is not str or not value.strip():
        raise ApplicationInputError("task_id must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ApplicationInputError("task_id must be UTF-8 text")
    if len(encoded) > 256:
        raise ApplicationInputError("task_id exceeds the 256-byte bound")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ApplicationInputError("task_id contains control characters")


def presentation_identity(spec: SessionSpec) -> PresentationIdentity:
    """Presentation identity of a live session spec (offline or configured)."""
    if type(spec) is not SessionSpec:
        raise ApplicationInputError("spec must be a SessionSpec")
    return PresentationIdentity(task_id=spec.task_id, source_kind=spec.source.kind)


def initial_session_view(identity: PresentationIdentity) -> SessionViewState:
    """Return the initial presentation state for a presentation identity."""
    if type(identity) is not PresentationIdentity:
        raise ApplicationInputError("identity must be a PresentationIdentity")
    return SessionViewState(
        session_id=identity.session_id,
        task_id=identity.task_id,
        source_kind=identity.source_kind,
    )
