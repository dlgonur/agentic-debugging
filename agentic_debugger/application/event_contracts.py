"""Versioned application-owned session vocabulary and lifecycle contracts.

This module owns the versioned application vocabulary — source kinds,
session lifecycle statuses/phases, termination reasons, event kinds,
verifier stages — together with the strict lifecycle transition rules,
session/timestamp identity validation, and the shared credential-shape
policies used by the application event schema and its producers.

It is the bottom contract layer of the application event architecture:

* :mod:`agentic_debugger.application.event_fields` adds bounded-value
  validation primitives on top of these contracts;
* :mod:`agentic_debugger.application.event_payloads` adds the per-kind
  payload contracts;
* :mod:`agentic_debugger.application.events` remains the public facade
  owning :class:`SessionEvent` and complete-stream validation.

Dependency rule: this module imports only ``SchemaValidationError``.  It
never imports or executes controller, verifier, PDB, patch, demo,
live-model, GPU, or experiment code.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from agentic_debugger import SchemaValidationError

#: Schema version of the application-owned session event model.
SESSION_EVENT_SCHEMA_VERSION = "session-event-v1"

#: Bounded payload rules (UTF-8 byte limits; truncation is producer-side and
#: the marker must be included inside the limit).
MAX_IDENTIFIER_CHARS = 256
MAX_TEXT_CHARS = 4096
MAX_SHORT_TEXT_CHARS = 512
MAX_STACK_FRAMES = 64
MAX_LOCALS = 512
MAX_BREAKPOINTS = 64
MAX_CHANGED_FILES = 64
MAX_TUPLE_TEXT_CHARS = 512
MAX_SHA256_HEX = 64
MAX_JSON_DEPTH = 8
#: Bounded app-owned source snapshot text.  Curated fixture modules are small;
#: 64 KiB covers them with margin while still failing closed on unbounded
#: files.  The Task-3 journal record bound (8 MiB) accommodates the resulting
#: events.  Truncation is producer-side and marked with ``truncated=True``.
MAX_SOURCE_TEXT_CHARS = 65536
#: Bounded candidate patch text preserved for new app-owned sessions (the
#: verifier's own candidate bound is 100_000 characters).
MAX_PATCH_TEXT_CHARS = 100000

_UTC_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)$"
)

#: Accepted credential-shape policy (mirrors the private policy in
#: ``agentic_debugger/evaluation/live.py``): key=value shaped secrets,
#: bearer/basic tokens, plus the common quoted Python assignment forms
#: (``{"api_key": "..."}`` dict literals and ``os.environ["API_KEY"] = ...``
#: environment writes).  Payload and configuration text fields that match
#: are rejected fail-closed.
#:
#: This is the single credential-shape policy shared by the event schema,
#: the execution-source spec validation, and the Task-4 producer-side
#: source/patch content policy (see :func:`contains_credential_shape`), so
#: the schema and the producers cannot drift apart.  The pattern is
#: keyword-anchored and requires an assignment/bearer shape, so harmless
#: source identifiers such as ``token_count`` or ``secretary`` do not match.
_SECRET_VALUE = re.compile(
    r"(?i)\b(?:bearer|basic)\s+\S+"
    r"|\b(?:api[_-]?key|access[_-]?token|authorization|credential|password|"
    r"secret|token)\s*['\"]?\s*\]?\s*[:=]\s*['\"]?\s*\S+"
)

#: Runtime-local name policy (Repair Pass 2): a bounded producer-side rule
#: for PDB frame locals.  A local whose *name* is exactly a credential-shaped
#: identifier (``api_key``, ``access_token``, ``authorization``,
#: ``credential``, ``password``, ``secret``, ``token``) never exposes its
#: summarized value in an application event; harmless names such as
#: ``token_count``, ``secretary``, or ``password_length`` are not credentials
#: merely because of a substring.  See
#: :func:`agentic_debugger.application.observability` for the redaction.
_CREDENTIAL_NAME = re.compile(
    r"(?i)^(?:api[_-]?key|access[_-]?token|authorization|credential|"
    r"password|secret|token)$"
)

#: Allowed session identifier charset: lowercase alphanumeric start, then
#: lowercase alphanumerics, dots, underscores and hyphens, at most 128 bytes.
_SESSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class SourceKind(str, Enum):
    """Execution-source vocabulary of a session.

    Live-startable kinds execute a new bounded session.  Recorded kinds are
    replay-only: they never enter the live-start workflow and never invoke
    tools, PDB, patch application, model calls, or verification.
    """

    OFFLINE_DEMO = "offline_demo"
    CONFIGURED_MODEL = "configured_model"
    OLLAMA_CLOUD_LADDER = "ollama_cloud_ladder"
    LEVEL32_OPERATOR = "level32_operator"
    LOCAL_PROJECT = "local_project"
    SESSION_BUNDLE = "session_bundle"
    CANONICAL_TRAJECTORY = "canonical_trajectory"
    EXPERIMENT_EVIDENCE = "experiment_evidence"

    @property
    def recorded(self) -> bool:
        """Whether this kind opens recorded material instead of a live run."""
        return self in (
            SourceKind.SESSION_BUNDLE,
            SourceKind.CANONICAL_TRAJECTORY,
            SourceKind.EXPERIMENT_EVIDENCE,
        )


class SessionStatus(str, Enum):
    """Operational lifecycle status of an application session.

    Terminal statuses describe how the application session itself ended.
    They are deliberately separate from any verifier/scientific outcome:
    ``SUCCEEDED`` means orderly end-to-end completion (the verifier may
    still report an unsuccessful repair).
    """

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    UNRESOLVED = "unresolved"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    CLEANUP_FAILED = "cleanup_failed"

    @property
    def terminal(self) -> bool:
        return self in (
            SessionStatus.SUCCEEDED,
            SessionStatus.UNRESOLVED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
            SessionStatus.TIMED_OUT,
            SessionStatus.INTERRUPTED,
            SessionStatus.CLEANUP_FAILED,
        )


class SessionPhase(str, Enum):
    """Fine-grained substate of a ``RUNNING`` session."""

    WAITING_MODEL = "waiting_model"
    EXECUTING_TOOL = "executing_tool"
    PDB_PAUSED = "pdb_paused"
    VERIFYING = "verifying"
    CLEANING = "cleaning"


class OperatorStage(str, Enum):
    """Truthful operational boundaries exposed by ladder operators."""

    STARTING = "starting"
    PREFLIGHT = "preflight"
    PREPARING_WORKSPACE = "preparing_workspace"
    MODEL_RUNNING = "model_running"
    DEBUGGER = "debugger"
    CANDIDATE = "candidate"
    VERIFICATION = "verification"
    OFFICIAL_VERIFICATION = "official_verification"
    OFFICIAL_VERIFICATION_PREPARING = "official_verification_preparing"
    OFFICIAL_EVALUATOR_STARTED = "official_evaluator_started"
    OFFICIAL_EVALUATOR_COMPLETED = "official_evaluator_completed"
    FINALIZING = "finalizing"
    CLEANUP = "cleanup"
    COMPLETED = "completed"


class SessionTerminationReason(str, Enum):
    """Application-level failure/termination taxonomy.

    Verifier errors never infer a correctness verdict; journal failures
    preserve any already-produced scientific artifact; cleanup failure is a
    distinct honest terminal state.
    """

    DONE = "done"
    UNRESOLVED = "unresolved"
    MODEL_ERROR = "model_error"
    DIRECTIVE_EXHAUSTED = "directive_exhausted"
    CONTROLLER_FAILED = "controller_failed"
    PDB_ERROR = "pdb_error"
    SUBPROCESS_ERROR = "subprocess_error"
    VERIFIER_ERROR = "verifier_error"
    JOURNAL_ERROR = "journal_error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    CLEANUP_FAILED = "cleanup_failed"


class SessionEventKind(str, Enum):
    """The V1 application-owned event vocabulary (architecture §8.1)."""

    SESSION_CREATED = "session.created"
    SESSION_STARTED = "session.started"
    SESSION_STATUS_CHANGED = "session.status_changed"
    SESSION_CANCEL_REQUESTED = "session.cancel_requested"
    SESSION_COMPLETED = "session.completed"
    SESSION_FAILED = "session.failed"
    SESSION_CANCELLED = "session.cancelled"
    CONTROLLER_STEP = "controller.step"
    CONTROLLER_TRANSITION = "controller.transition"
    MODEL_REQUEST_STARTED = "model.request_started"
    MODEL_REQUEST_COMPLETED = "model.request_completed"
    MODEL_DIRECTIVE_ACCEPTED = "model.directive_accepted"
    MODEL_DIRECTIVE_REJECTED = "model.directive_rejected"
    MODEL_CONFIGURED = "model.configured"
    OPERATOR_PROGRESS = "operator.progress"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    DEBUGGER_STARTED = "debugger.started"
    DEBUGGER_LOCATION_CHANGED = "debugger.location_changed"
    DEBUGGER_STACK_OBSERVED = "debugger.stack_observed"
    DEBUGGER_LOCALS_OBSERVED = "debugger.locals_observed"
    PATCH_PROPOSED = "patch.proposed"
    PATCH_REJECTED = "patch.rejected"
    PATCH_APPLY_FAILED = "patch.apply_failed"
    PATCH_APPLIED = "patch.applied"
    PATCH_REVERTED = "patch.reverted"
    SOURCE_SNAPSHOT = "source.snapshot"
    DIAGNOSIS_RECORDED = "diagnosis.recorded"
    VERIFIER_STARTED = "verifier.started"
    VERIFIER_STAGE_STARTED = "verifier.stage_started"
    VERIFIER_STAGE_COMPLETED = "verifier.stage_completed"
    VERIFIER_COMPLETED = "verifier.completed"
    CLEANUP_STARTED = "cleanup.started"
    CLEANUP_COMPLETED = "cleanup.completed"
    CLEANUP_NOT_REQUIRED = "cleanup.not_required"
    ARTIFACT_WRITTEN = "artifact.written"


class VerifierStage(str, Enum):
    """Verifier progress stages exposed as informational presentation data.

    Stage progress is never a correctness verdict; only the final
    ``EvaluationResult`` is authoritative.
    """

    PREPARE_WORKSPACE = "prepare_workspace"
    BASELINE_REPRODUCTION = "baseline_reproduction"
    PRE_PATCH_TARGETED = "pre_patch_targeted"
    APPLY_CANDIDATE = "apply_candidate"
    SYNTAX_VALIDATION = "syntax_validation"
    POST_PATCH_REPRODUCTION = "post_patch_reproduction"
    F2P_P2P_CHECKS = "f2p_p2p_checks"
    BROADER_SUITE = "broader_suite"
    CLASSIFICATION = "classification"
    CLEANUP_INTEGRITY = "cleanup_integrity"


class VerifierStageStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class SourceSnapshotStage(str, Enum):
    """Material source state of one app-owned source snapshot.

    ``INITIAL`` is the pristine task source, ``APPLIED`` is the workspace
    state after an accepted patch was applied, and ``REVERTED`` is the
    workspace state after that patch was reverted.  The accepted candidate
    patch itself (the diff text) is carried by ``patch.proposed``.
    """

    INITIAL = "initial"
    APPLIED = "applied"
    REVERTED = "reverted"


class ModelRequestStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"


# ---------------------------------------------------------------------------
# Lifecycle rules
# ---------------------------------------------------------------------------


def _terminal_status_for(reason: SessionTerminationReason) -> SessionStatus:
    if reason is SessionTerminationReason.DONE:
        return SessionStatus.SUCCEEDED
    if reason is SessionTerminationReason.UNRESOLVED:
        return SessionStatus.UNRESOLVED
    if reason is SessionTerminationReason.TIMEOUT:
        return SessionStatus.TIMED_OUT
    if reason is SessionTerminationReason.CANCELLED:
        return SessionStatus.CANCELLED
    if reason is SessionTerminationReason.INTERRUPTED:
        return SessionStatus.INTERRUPTED
    if reason is SessionTerminationReason.CLEANUP_FAILED:
        return SessionStatus.CLEANUP_FAILED
    return SessionStatus.FAILED


def _compatible_reasons(status: SessionStatus) -> frozenset[SessionTerminationReason]:
    if status is SessionStatus.SUCCEEDED:
        return frozenset({SessionTerminationReason.DONE})
    if status is SessionStatus.UNRESOLVED:
        return frozenset({SessionTerminationReason.UNRESOLVED})
    if status is SessionStatus.FAILED:
        return frozenset(
            {
                SessionTerminationReason.MODEL_ERROR,
                SessionTerminationReason.DIRECTIVE_EXHAUSTED,
                SessionTerminationReason.CONTROLLER_FAILED,
                SessionTerminationReason.PDB_ERROR,
                SessionTerminationReason.SUBPROCESS_ERROR,
                SessionTerminationReason.VERIFIER_ERROR,
                SessionTerminationReason.JOURNAL_ERROR,
            }
        )
    if status is SessionStatus.CANCELLED:
        return frozenset({SessionTerminationReason.CANCELLED})
    if status is SessionStatus.TIMED_OUT:
        return frozenset({SessionTerminationReason.TIMEOUT})
    if status is SessionStatus.INTERRUPTED:
        return frozenset({SessionTerminationReason.INTERRUPTED})
    if status is SessionStatus.CLEANUP_FAILED:
        return frozenset({SessionTerminationReason.CLEANUP_FAILED})
    return frozenset()


_SESSION_TRANSITIONS: Dict[SessionStatus, tuple[SessionStatus, ...]] = {
    SessionStatus.CREATED: (
        SessionStatus.STARTING,
        SessionStatus.CANCELLED,
        SessionStatus.FAILED,
        SessionStatus.INTERRUPTED,
        SessionStatus.TIMED_OUT,
    ),
    SessionStatus.STARTING: (
        SessionStatus.RUNNING,
        SessionStatus.CANCELLED,
        SessionStatus.FAILED,
        SessionStatus.TIMED_OUT,
        SessionStatus.INTERRUPTED,
        SessionStatus.CLEANUP_FAILED,
    ),
    SessionStatus.RUNNING: (
        SessionStatus.RUNNING,
        SessionStatus.SUCCEEDED,
        SessionStatus.UNRESOLVED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
        SessionStatus.TIMED_OUT,
        SessionStatus.INTERRUPTED,
        SessionStatus.CLEANUP_FAILED,
    ),
    SessionStatus.SUCCEEDED: (),
    SessionStatus.UNRESOLVED: (),
    SessionStatus.FAILED: (),
    SessionStatus.CANCELLED: (),
    SessionStatus.TIMED_OUT: (),
    SessionStatus.INTERRUPTED: (),
    SessionStatus.CLEANUP_FAILED: (),
}


def allowed_transitions() -> Mapping[SessionStatus, tuple[SessionStatus, ...]]:
    """Return the versioned session lifecycle transition map."""
    return {
        status: tuple(targets) for status, targets in _SESSION_TRANSITIONS.items()
    }


def can_transition(current: SessionStatus, target: SessionStatus) -> bool:
    """Whether ``target`` is a legal lifecycle successor of ``current``."""
    if type(current) is not SessionStatus or type(target) is not SessionStatus:
        raise SchemaValidationError("session statuses are required")
    return target in _SESSION_TRANSITIONS[current]


def terminal_status_for(reason: SessionTerminationReason) -> SessionStatus:
    """Default terminal status for an orderly termination reason."""
    if type(reason) is not SessionTerminationReason:
        raise SchemaValidationError("termination reason is required")
    return _terminal_status_for(reason)


def compatible_reasons(status: SessionStatus) -> frozenset[SessionTerminationReason]:
    """Termination reasons consistent with a terminal status."""
    if type(status) is not SessionStatus:
        raise SchemaValidationError("session status is required")
    return _compatible_reasons(status)


def validate_session_id(value: Any) -> str:
    """Validate one application session identifier."""
    if type(value) is not str or _SESSION_ID_RE.match(value) is None:
        raise SchemaValidationError(
            "session_id must match [a-z0-9][a-z0-9._-]{0,127}"
        )
    return value


def validate_utc_timestamp(value: Any) -> str:
    """Validate a strict ISO-8601 UTC timestamp (Z or +00:00)."""
    if type(value) is not str or not value:
        raise SchemaValidationError("timestamp must be a non-empty string")
    if _UTC_ISO_RE.match(value) is None:
        raise SchemaValidationError(
            f"timestamp must be ISO-8601 UTC (ending with Z or +00:00), got {value!r}"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        raise SchemaValidationError(
            f"timestamp is not a valid calendar date/time: {value!r}"
        )
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise SchemaValidationError(
            f"timestamp must be timezone-aware UTC: {value!r}"
        )
    return value


def contains_credential_shape(value: str) -> bool:
    """Whether ``value`` matches the accepted credential-shape policy.

    One shared policy for the event schema (regular text fields reject
    credential-shaped values fail-closed) and the producer-side content
    policy for source snapshots and patch bodies (see
    :mod:`agentic_debugger.application.source_snapshots` and
    :mod:`agentic_debugger.application.observability`).
    """
    return _SECRET_VALUE.search(value) is not None


def is_credential_name(name: str) -> bool:
    """Whether a runtime local *name* is credential-shaped.

    Exact-name policy for PDB frame locals (see :data:`_CREDENTIAL_NAME`):
    the name itself is the secret key, so the summarized value must not be
    exposed.  The match is anchored to the whole name, so harmless
    identifiers such as ``token_count``, ``secretary``, or
    ``password_length`` never match.
    """
    if type(name) is not str:
        return False
    return _CREDENTIAL_NAME.match(name) is not None
