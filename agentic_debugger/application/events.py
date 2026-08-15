"""Versioned application-owned session vocabulary and :class:`SessionEvent`.

This module is the bottom layer of ``agentic_debugger.application``: it owns
the versioned application vocabulary (source kinds, session lifecycle,
termination reasons, event kinds, verifier stages) and the strict
:class:`SessionEvent` model with bounded, safe payload contracts.

``SessionEvent`` is a separate application-owned contract.  It is not a
canonical ``RunEvent`` 1.0 record, never mixes into canonical trajectory
files, and carries only bounded data already safe for model/tool observation
(see the safe-data rules below).

Dependency rule: this module imports only lightweight existing enums and
taxonomies for validation (``ControllerState``, ``ObservationStatus``,
``SemanticOutcome``, ``EvaluationStatus``).  It never imports or executes
controller, verifier, PDB, patch, demo, live-model, GPU, or experiment code.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Sequence

from agentic_debugger import SchemaValidationError
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application import ApplicationContractError
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
from agentic_debugger.evaluation.runner import EvaluationStatus
from agentic_debugger.events.schema import ObservationStatus, validate_json_compatible

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


# ---------------------------------------------------------------------------
# Bounded value helpers (fail-closed, credential-safe)
# ---------------------------------------------------------------------------


def _check_required(mapping: Mapping[str, Any], required: set, label: str) -> None:
    missing = required - set(mapping.keys())
    if missing:
        raise SchemaValidationError(
            f"Missing required fields in {label}: {sorted(missing)}"
        )


def _check_no_unknown(mapping: Mapping[str, Any], known: set, label: str) -> None:
    extra = set(mapping.keys()) - known
    if extra:
        raise SchemaValidationError(
            f"Unknown fields in {label}: {sorted(extra)}"
        )


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


def _looks_like_secret(value: str) -> bool:
    return contains_credential_shape(value)


def _bounded_text(
    value: Any,
    label: str,
    max_chars: int,
    *,
    allow_empty: bool = False,
    nullable: bool = False,
) -> Optional[str]:
    if value is None and nullable:
        return None
    if type(value) is not str:
        raise SchemaValidationError(f"{label} must be a string or null")
    if not value.strip() and not allow_empty:
        raise SchemaValidationError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise SchemaValidationError(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise SchemaValidationError(
            f"{label} exceeds the {max_chars}-byte bound"
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise SchemaValidationError(f"{label} contains control characters")
    if _looks_like_secret(value):
        raise SchemaValidationError(
            f"{label} contains a credential-shaped value"
        )
    return value


def _bounded_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, label, max_chars)


def _multiline_text(value: Any, label: str, max_chars: int) -> str:
    """Bound UTF-8 text that may contain normal source whitespace.

    Source/patch content legitimately contains newlines and tabs, so this
    validator allows ``\\n``, ``\\t`` and ``\\r`` but rejects every other
    control character (NUL, ``0x7F``, and the rest of ``0x00-0x1F``) and any
    oversized value.  It intentionally does not apply the credential-shape
    regex here: source code may legitimately mention token-like identifiers,
    so safe-data enforcement for source/patch content happens at the producer
    boundary through the shared :func:`contains_credential_shape` policy
    (captured source snapshots and patch bodies that match it are withheld
    before they can enter an event).
    """
    if type(value) is not str:
        raise SchemaValidationError(f"{label} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise SchemaValidationError(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise SchemaValidationError(
            f"{label} exceeds the {max_chars}-byte bound"
        )
    for char in value:
        code = ord(char)
        if code == 0x00 or code == 0x7F or (
            code < 0x20 and char not in "\n\t\r"
        ):
            raise SchemaValidationError(
                f"{label} contains a prohibited control character"
            )
    return value


def _multiline_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    if value is None:
        return None
    return _multiline_text(value, label, max_chars)


def _identifier(value: Any, label: str) -> str:
    if value is None:
        raise SchemaValidationError(f"{label} must be a non-empty string")
    return _bounded_text(value, label, MAX_IDENTIFIER_CHARS)


def _sha256_hex(value: Any, label: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SchemaValidationError(f"{label} must be a 64-character hex digest")
    return value


def _nonneg_int(value: Any, label: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise SchemaValidationError(f"{label} must be a non-negative integer")
    return value


def _int_or_none(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    return _nonneg_int(value, label)


def _bool_or_none(value: Any, label: str) -> Optional[bool]:
    if value is None:
        return None
    if type(value) is not bool:
        raise SchemaValidationError(f"{label} must be a boolean or null")
    return value


def _enum_or_none(value: Any, label: str, enum_type: type) -> Optional[Any]:
    if value is None:
        return None
    return _enum(value, label, enum_type)


def _enum(value: Any, label: str, enum_type: type) -> Any:
    if type(value) is not str or not value:
        raise SchemaValidationError(f"{label} must be a non-empty string")
    try:
        return enum_type(value)
    except ValueError:
        raise SchemaValidationError(
            f"{label} is not a valid {enum_type.__name__}: {value!r}"
        )


def _string_tuple(
    value: Any,
    label: str,
    *,
    max_items: int,
    max_chars: int = MAX_TUPLE_TEXT_CHARS,
) -> tuple[str, ...]:
    if type(value) is not tuple and type(value) is not list:
        raise SchemaValidationError(f"{label} must be a list of strings")
    if len(value) > max_items:
        raise SchemaValidationError(
            f"{label} exceeds the {max_items}-item bound"
        )
    return tuple(
        _bounded_text(item, f"{label}[{index}]", max_chars)  # type: ignore[arg-type]
        for index, item in enumerate(value)
    )


def _frame_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaValidationError(f"{label} must be a mapping")
    required = {"index", "function", "file", "line", "is_current"}
    _check_required(value, required, label)
    _check_no_unknown(value, required, label)
    return {
        "index": _nonneg_int(value["index"], f"{label}.index"),
        "function": _bounded_text(
            value["function"], f"{label}.function", MAX_IDENTIFIER_CHARS
        ),
        "file": _bounded_text(value["file"], f"{label}.file", MAX_SHORT_TEXT_CHARS),
        "line": _nonneg_int(value["line"], f"{label}.line"),
        "is_current": _bool(value["is_current"], f"{label}.is_current"),
    }


def _bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise SchemaValidationError(f"{label} must be a boolean")
    return value


def _local_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaValidationError(f"{label} must be a mapping")
    required = {"name", "summary"}
    _check_required(value, required, label)
    _check_no_unknown(value, required, label)
    return {
        "name": _bounded_text(value["name"], f"{label}.name", MAX_IDENTIFIER_CHARS),
        "summary": _bounded_text(value["summary"], f"{label}.summary", MAX_TEXT_CHARS),
    }


def _detached(payload: Mapping[str, Any]) -> dict[str, Any]:
    validate_json_compatible(payload, "payload")
    try:
        return json.loads(
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError, OverflowError):
        raise SchemaValidationError("payload is not strictly JSON-compatible")


class _FrozenList(tuple):
    """Tuple-backed JSON sequence with no mutating list protocol."""

    def __new__(cls, values: Any = ()) -> "_FrozenList":
        return tuple.__new__(cls, values)


class _FrozenDict(tuple, Mapping[str, Any]):
    """Tuple-backed JSON mapping whose canonical pairs are the tuple itself."""

    def __new__(
        cls,
        values: Any,
    ) -> "_FrozenDict":
        items = (
            tuple(values.items())
            if isinstance(values, Mapping)
            else tuple(values)
        )
        return tuple.__new__(cls, tuple((str(key), value) for key, value in items))

    def __iter__(self):
        return (key for key, _ in tuple.__iter__(self))

    def __len__(self) -> int:
        return tuple.__len__(self)

    def __getitem__(self, key: str) -> Any:
        for item_key, item_value in tuple.__iter__(self):
            if item_key == key:
                return item_value
        raise KeyError(key)


def _freeze(value: Any) -> Any:
    """Deep-freeze JSON-compatible values into tuple-backed structures."""
    if isinstance(value, Mapping):
        return _FrozenDict((str(key), _freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return _FrozenList(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """Deep-convert frozen structures back into plain JSON data."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Per-kind payload contracts
# ---------------------------------------------------------------------------


def _payload_created(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {"spec_fingerprint"}
    _check_required(payload, required, "session.created payload")
    _check_no_unknown(payload, required, "session.created payload")
    return {"spec_fingerprint": _sha256_hex(payload["spec_fingerprint"], "spec_fingerprint")}


def _payload_empty(payload: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError(f"{label} must be a mapping")
    _check_no_unknown(payload, set(), label)
    if payload:
        raise SchemaValidationError(f"{label} must be empty")
    return {}


def _payload_status_changed(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("session.status_changed payload must be a mapping")
    required = {"status", "phase"}
    _check_required(payload, required, "session.status_changed payload")
    _check_no_unknown(payload, required, "session.status_changed payload")
    status = _enum(payload["status"], "status", SessionStatus)
    if status is not SessionStatus.RUNNING:
        raise SchemaValidationError(
            "session.status_changed may only carry the running status"
        )
    phase = _enum(payload["phase"], "phase", SessionPhase)
    return {"status": status.value, "phase": phase.value}


def _payload_terminal(payload: Mapping[str, Any], label: str, kind: SessionEventKind) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError(f"{label} must be a mapping")
    required = {"status", "termination_reason"}
    _check_required(payload, required, label)
    _check_no_unknown(payload, required, label)
    status = _enum(payload["status"], "status", SessionStatus)
    reason = _enum(payload["termination_reason"], "termination_reason", SessionTerminationReason)
    if kind is SessionEventKind.SESSION_COMPLETED and status not in (
        SessionStatus.SUCCEEDED,
        SessionStatus.UNRESOLVED,
    ):
        raise SchemaValidationError(
            "session.completed status must be succeeded or unresolved"
        )
    if kind is SessionEventKind.SESSION_FAILED and status not in (
        SessionStatus.FAILED,
        SessionStatus.TIMED_OUT,
        SessionStatus.INTERRUPTED,
        SessionStatus.CLEANUP_FAILED,
    ):
        raise SchemaValidationError(
            "session.failed status must be failed, timed_out, interrupted or cleanup_failed"
        )
    if kind is SessionEventKind.SESSION_CANCELLED and status is not SessionStatus.CANCELLED:
        raise SchemaValidationError("session.cancelled status must be cancelled")
    if reason not in _compatible_reasons(status):
        raise SchemaValidationError(
            f"termination reason {reason.value!r} is not compatible with status {status.value!r}"
        )
    return {"status": status.value, "termination_reason": reason.value}


def _payload_controller_step(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("controller.step payload must be a mapping")
    required = {"step_index", "directive_kind", "stop_reason"}
    _check_required(payload, required, "controller.step payload")
    _check_no_unknown(payload, required, "controller.step payload")
    return {
        "step_index": _nonneg_int(payload["step_index"], "step_index"),
        "directive_kind": _bounded_text_or_none(
            payload["directive_kind"], "directive_kind", 64
        ),
        "stop_reason": _bounded_text_or_none(payload["stop_reason"], "stop_reason", 64),
    }


def _payload_model_configured(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``model.configured``: safe configured command-model provenance.

    Task-8 additive event: records only safe provenance (profile id,
    configuration fingerprint, display label, protocol/tool version) after
    ``session.started``.  It never carries the executable, argv,
    environment overrides, or any credential-shaped value: history/replay
    stores configuration provenance and a fingerprint, not a live
    executable object.
    """
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("model.configured payload must be a mapping")
    required = {
        "profile_id",
        "config_fingerprint",
        "display_name",
        "protocol_version",
        "tool_version",
    }
    _check_required(payload, required, "model.configured payload")
    _check_no_unknown(payload, required, "model.configured payload")
    return {
        "profile_id": _bounded_text(
            payload["profile_id"], "profile_id", MAX_IDENTIFIER_CHARS
        ),
        "config_fingerprint": _sha256_hex(
            payload["config_fingerprint"], "config_fingerprint"
        ),
        "display_name": _bounded_text(
            payload["display_name"], "display_name", MAX_SHORT_TEXT_CHARS
        ),
        "protocol_version": _bounded_text(
            payload["protocol_version"], "protocol_version", MAX_SHORT_TEXT_CHARS
        ),
        "tool_version": _bounded_text(
            payload["tool_version"], "tool_version", MAX_SHORT_TEXT_CHARS
        ),
    }


def _payload_request_started(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("model.request_started payload must be a mapping")
    required = {"request_index"}
    _check_required(payload, required, "model.request_started payload")
    _check_no_unknown(payload, required, "model.request_started payload")
    return {"request_index": _nonneg_int(payload["request_index"], "request_index")}


def _payload_request_completed(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("model.request_completed payload must be a mapping")
    required = {"request_index", "status"}
    _check_required(payload, required, "model.request_completed payload")
    _check_no_unknown(payload, required, "model.request_completed payload")
    return {
        "request_index": _nonneg_int(payload["request_index"], "request_index"),
        "status": _enum(payload["status"], "status", ModelRequestStatus).value,
    }


def _payload_directive_accepted(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("model.directive_accepted payload must be a mapping")
    required = {"directive_kind", "action_name", "target_state"}
    _check_required(payload, required, "model.directive_accepted payload")
    _check_no_unknown(payload, required, "model.directive_accepted payload")
    return {
        "directive_kind": _bounded_text_or_none(
            payload["directive_kind"], "directive_kind", 64
        ),
        "action_name": _bounded_text_or_none(
            payload["action_name"], "action_name", MAX_IDENTIFIER_CHARS
        ),
        "target_state": _bounded_text_or_none(
            payload["target_state"], "target_state", 64
        ),
    }


def _payload_directive_rejected(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("model.directive_rejected payload must be a mapping")
    required = {"directive_kind", "rejection_category"}
    _check_required(payload, required, "model.directive_rejected payload")
    _check_no_unknown(payload, required, "model.directive_rejected payload")
    return {
        "directive_kind": _bounded_text_or_none(
            payload["directive_kind"], "directive_kind", 64
        ),
        "rejection_category": _bounded_text(
            payload["rejection_category"], "rejection_category", MAX_IDENTIFIER_CHARS
        ),
    }


def _payload_tool(payload: Mapping[str, Any], label: str, *, completed: bool) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError(f"{label} must be a mapping")
    required = {"tool_name"} if not completed else {"tool_name", "status"}
    _check_required(payload, required, label)
    _check_no_unknown(payload, required, label)
    result = {
        "tool_name": _bounded_text(payload["tool_name"], "tool_name", MAX_IDENTIFIER_CHARS)
    }
    if completed:
        result["status"] = _enum(payload["status"], "status", ObservationStatus).value
    return result


def _payload_debugger_started(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("debugger.started payload must be a mapping")
    required = {"script", "breakpoints"}
    _check_required(payload, required, "debugger.started payload")
    _check_no_unknown(payload, required, "debugger.started payload")
    return {
        "script": _bounded_text_or_none(payload["script"], "script", MAX_SHORT_TEXT_CHARS),
        "breakpoints": _string_tuple(
            payload["breakpoints"], "breakpoints", max_items=MAX_BREAKPOINTS
        ),
    }


def _payload_location_changed(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("debugger.location_changed payload must be a mapping")
    required = {"script", "line", "function", "pause_generation"}
    _check_required(payload, required, "debugger.location_changed payload")
    _check_no_unknown(payload, required, "debugger.location_changed payload")
    line = payload["line"]
    if line is not None:
        if type(line) is not int or isinstance(line, bool) or line < 1:
            raise SchemaValidationError("line must be a positive integer or null")
    return {
        "script": _bounded_text_or_none(payload["script"], "script", MAX_SHORT_TEXT_CHARS),
        "line": line,
        "function": _bounded_text_or_none(
            payload["function"], "function", MAX_IDENTIFIER_CHARS
        ),
        # Nullable (Repair Pass 3): historical traces that never recorded a
        # pause generation stay NOT RECORDED instead of receiving a
        # synthesized counter.
        "pause_generation": _int_or_none(
            payload["pause_generation"], "pause_generation"
        ),
    }


def _payload_stack_observed(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("debugger.stack_observed payload must be a mapping")
    required = {"pause_generation", "frames"}
    _check_required(payload, required, "debugger.stack_observed payload")
    _check_no_unknown(payload, required, "debugger.stack_observed payload")
    frames = payload["frames"]
    if type(frames) is not tuple and type(frames) is not list:
        raise SchemaValidationError("frames must be a list")
    if len(frames) > MAX_STACK_FRAMES:
        raise SchemaValidationError(
            f"frames exceeds the {MAX_STACK_FRAMES}-item bound"
        )
    return {
        "pause_generation": _int_or_none(
            payload["pause_generation"], "pause_generation"
        ),
        "frames": tuple(
            _frame_mapping(item, f"frames[{index}]") for index, item in enumerate(frames)
        ),
    }


def _payload_locals_observed(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("debugger.locals_observed payload must be a mapping")
    required = {"pause_generation", "locals"}
    _check_required(payload, required, "debugger.locals_observed payload")
    _check_no_unknown(payload, required, "debugger.locals_observed payload")
    locals_value = payload["locals"]
    if type(locals_value) is not tuple and type(locals_value) is not list:
        raise SchemaValidationError("locals must be a list")
    if len(locals_value) > MAX_LOCALS:
        raise SchemaValidationError(f"locals exceeds the {MAX_LOCALS}-item bound")
    return {
        "pause_generation": _int_or_none(
            payload["pause_generation"], "pause_generation"
        ),
        "locals": tuple(
            _local_mapping(item, f"locals[{index}]") for index, item in enumerate(locals_value)
        ),
    }


def _payload_patch(payload: Mapping[str, Any], label: str, *, applied: bool = False, rejected: bool = False, reverted: bool = False) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError(f"{label} must be a mapping")
    if applied:
        required = {"attempt_index", "changed_files", "syntax_passed"}
        optional: set[str] = set()
    elif rejected:
        required = {"attempt_index", "rejection_reason"}
        optional = set()
    elif reverted:
        required = {"attempt_index"}
        optional = set()
    else:
        required = {"attempt_index", "patch_sha256"}
        # Additive Task-4 revision: the exact bounded candidate diff text is
        # preserved for new app-owned sessions (it is the model-authored
        # candidate already recorded in canonical tool observations).
        optional = {"patch_text"}
    _check_required(payload, required, label)
    _check_no_unknown(payload, required | optional, label)
    result = {"attempt_index": _nonneg_int(payload["attempt_index"], "attempt_index")}
    if applied:
        result["changed_files"] = _string_tuple(
            payload["changed_files"], "changed_files", max_items=MAX_CHANGED_FILES
        )
        result["syntax_passed"] = _bool_or_none(payload["syntax_passed"], "syntax_passed")
    elif rejected:
        result["rejection_reason"] = _bounded_text(
            payload["rejection_reason"], "rejection_reason", MAX_SHORT_TEXT_CHARS
        )
    elif reverted:
        pass
    else:
        result["patch_sha256"] = _sha256_hex(payload["patch_sha256"], "patch_sha256")
        if "patch_text" in payload:
            result["patch_text"] = _multiline_text(
                payload["patch_text"], "patch_text", MAX_PATCH_TEXT_CHARS
            )
    return result


def _payload_patch_apply_failed(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("patch.apply_failed payload must be a mapping")
    required = {"attempt_index", "apply_failure_reason"}
    _check_required(payload, required, "patch.apply_failed payload")
    _check_no_unknown(payload, required, "patch.apply_failed payload")
    return {
        "attempt_index": _nonneg_int(payload["attempt_index"], "attempt_index"),
        "apply_failure_reason": _bounded_text(
            payload["apply_failure_reason"], "apply_failure_reason", MAX_SHORT_TEXT_CHARS
        ),
    }


def _payload_controller_transition(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("controller.transition payload must be a mapping")
    required = {"source_state", "target_state", "reason"}
    _check_required(payload, required, "controller.transition payload")
    _check_no_unknown(payload, required, "controller.transition payload")
    return {
        "source_state": _enum(payload["source_state"], "source_state", ControllerState).value,
        "target_state": _enum(payload["target_state"], "target_state", ControllerState).value,
        "reason": _bounded_text_or_none(
            payload["reason"], "reason", MAX_TEXT_CHARS
        ),
    }


def _payload_source_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("source.snapshot payload must be a mapping")
    required = {"path", "sha256", "text", "line_count", "truncated", "stage"}
    _check_required(payload, required, "source.snapshot payload")
    _check_no_unknown(payload, required, "source.snapshot payload")
    line_count = payload["line_count"]
    if type(line_count) is not int or isinstance(line_count, bool) or line_count < 1:
        raise SchemaValidationError("line_count must be a positive integer")
    path = _bounded_text(payload["path"], "path", MAX_SHORT_TEXT_CHARS)
    if _has_drive_letter(path) or path.startswith("/") or path.startswith("\\"):
        raise SchemaValidationError(
            "source.snapshot path must be a logical relative path"
        )
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if ".." in parts:
        raise SchemaValidationError(
            "source.snapshot path must not contain .. traversal"
        )
    return {
        "path": path,
        "sha256": _sha256_hex(payload["sha256"], "sha256"),
        "text": _multiline_text(payload["text"], "text", MAX_SOURCE_TEXT_CHARS),
        "line_count": line_count,
        "truncated": _bool(payload["truncated"], "truncated"),
        "stage": _enum(payload["stage"], "stage", SourceSnapshotStage).value,
    }


def _has_drive_letter(path: str) -> bool:
    return len(path) >= 2 and path[1] == ":" and path[0].isalpha()


def _payload_diagnosis_recorded(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("diagnosis.recorded payload must be a mapping")
    required = {"text", "file_path", "symbol", "confidence"}
    _check_required(payload, required, "diagnosis.recorded payload")
    _check_no_unknown(payload, required, "diagnosis.recorded payload")
    return {
        "text": _multiline_text_or_none(payload["text"], "text", MAX_TEXT_CHARS),
        "file_path": _bounded_text_or_none(
            payload["file_path"], "file_path", MAX_SHORT_TEXT_CHARS
        ),
        "symbol": _bounded_text_or_none(payload["symbol"], "symbol", MAX_IDENTIFIER_CHARS),
        "confidence": _bounded_text_or_none(
            payload["confidence"], "confidence", MAX_SHORT_TEXT_CHARS
        ),
    }


def _payload_verifier_stage(payload: Mapping[str, Any], label: str, *, completed: bool) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError(f"{label} must be a mapping")
    required = {"stage"} if not completed else {"stage", "status"}
    _check_required(payload, required, label)
    _check_no_unknown(payload, required, label)
    result = {"stage": _enum(payload["stage"], "stage", VerifierStage).value}
    if completed:
        result["status"] = _enum(payload["status"], "status", VerifierStageStatus).value
    return result


def _payload_verifier_completed(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("verifier.completed payload must be a mapping")
    required = {
        "status", "outcome", "f2p_passed", "f2p_total",
        "p2p_passed", "p2p_total", "workspace_cleaned",
    }
    _check_required(payload, required, "verifier.completed payload")
    _check_no_unknown(payload, required, "verifier.completed payload")
    status = payload["status"]
    if status is not None:
        status = _enum(status, "status", EvaluationStatus).value
    outcome = payload["outcome"]
    if outcome is not None:
        outcome = _enum(outcome, "outcome", SemanticOutcome).value
    return {
        "status": status,
        "outcome": outcome,
        "f2p_passed": _int_or_none(payload["f2p_passed"], "f2p_passed"),
        "f2p_total": _int_or_none(payload["f2p_total"], "f2p_total"),
        "p2p_passed": _int_or_none(payload["p2p_passed"], "p2p_passed"),
        "p2p_total": _int_or_none(payload["p2p_total"], "p2p_total"),
        "workspace_cleaned": _bool_or_none(payload["workspace_cleaned"], "workspace_cleaned"),
    }


def _payload_cleanup_completed(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("cleanup.completed payload must be a mapping")
    required = {"verified"}
    _check_required(payload, required, "cleanup.completed payload")
    _check_no_unknown(payload, required, "cleanup.completed payload")
    return {"verified": _bool(payload["verified"], "verified")}


def _payload_artifact_written(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("artifact.written payload must be a mapping")
    required = {"path", "sha256"}
    _check_required(payload, required, "artifact.written payload")
    _check_no_unknown(payload, required, "artifact.written payload")
    return {
        "path": _bounded_text(payload["path"], "path", MAX_SHORT_TEXT_CHARS),
        "sha256": _sha256_hex(payload["sha256"], "sha256"),
    }


_PAYLOAD_VALIDATORS: Dict[SessionEventKind, Any] = {
    SessionEventKind.SESSION_CREATED: _payload_created,
    SessionEventKind.SESSION_STARTED: lambda p: _payload_empty(p, "session.started payload"),
    SessionEventKind.SESSION_STATUS_CHANGED: _payload_status_changed,
    SessionEventKind.SESSION_CANCEL_REQUESTED: lambda p: _payload_empty(p, "session.cancel_requested payload"),
    SessionEventKind.SESSION_COMPLETED: lambda p: _payload_terminal(p, "session.completed payload", SessionEventKind.SESSION_COMPLETED),
    SessionEventKind.SESSION_FAILED: lambda p: _payload_terminal(p, "session.failed payload", SessionEventKind.SESSION_FAILED),
    SessionEventKind.SESSION_CANCELLED: lambda p: _payload_terminal(p, "session.cancelled payload", SessionEventKind.SESSION_CANCELLED),
    SessionEventKind.CONTROLLER_STEP: _payload_controller_step,
    SessionEventKind.CONTROLLER_TRANSITION: _payload_controller_transition,
    SessionEventKind.MODEL_REQUEST_STARTED: _payload_request_started,
    SessionEventKind.MODEL_REQUEST_COMPLETED: _payload_request_completed,
    SessionEventKind.MODEL_DIRECTIVE_ACCEPTED: _payload_directive_accepted,
    SessionEventKind.MODEL_DIRECTIVE_REJECTED: _payload_directive_rejected,
    SessionEventKind.MODEL_CONFIGURED: _payload_model_configured,
    SessionEventKind.TOOL_STARTED: lambda p: _payload_tool(p, "tool.started payload", completed=False),
    SessionEventKind.TOOL_COMPLETED: lambda p: _payload_tool(p, "tool.completed payload", completed=True),
    SessionEventKind.DEBUGGER_STARTED: _payload_debugger_started,
    SessionEventKind.DEBUGGER_LOCATION_CHANGED: _payload_location_changed,
    SessionEventKind.DEBUGGER_STACK_OBSERVED: _payload_stack_observed,
    SessionEventKind.DEBUGGER_LOCALS_OBSERVED: _payload_locals_observed,
    SessionEventKind.PATCH_PROPOSED: lambda p: _payload_patch(p, "patch.proposed payload"),
    SessionEventKind.PATCH_REJECTED: lambda p: _payload_patch(p, "patch.rejected payload", rejected=True),
    SessionEventKind.PATCH_APPLY_FAILED: _payload_patch_apply_failed,
    SessionEventKind.PATCH_APPLIED: lambda p: _payload_patch(p, "patch.applied payload", applied=True),
    SessionEventKind.PATCH_REVERTED: lambda p: _payload_patch(p, "patch.reverted payload", reverted=True),
    SessionEventKind.SOURCE_SNAPSHOT: _payload_source_snapshot,
    SessionEventKind.DIAGNOSIS_RECORDED: _payload_diagnosis_recorded,
    SessionEventKind.VERIFIER_STARTED: lambda p: _payload_empty(p, "verifier.started payload"),
    SessionEventKind.VERIFIER_STAGE_STARTED: lambda p: _payload_verifier_stage(p, "verifier.stage_started payload", completed=False),
    SessionEventKind.VERIFIER_STAGE_COMPLETED: lambda p: _payload_verifier_stage(p, "verifier.stage_completed payload", completed=True),
    SessionEventKind.VERIFIER_COMPLETED: _payload_verifier_completed,
    SessionEventKind.CLEANUP_STARTED: lambda p: _payload_empty(p, "cleanup.started payload"),
    SessionEventKind.CLEANUP_COMPLETED: _payload_cleanup_completed,
    SessionEventKind.ARTIFACT_WRITTEN: _payload_artifact_written,
}


# ---------------------------------------------------------------------------
# SessionEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionEvent:
    """One immutable, JSON-compatible application-owned session event.

    ``sequence`` is authoritative ordering (contiguous, starting at 0);
    ``timestamp_utc`` is informational.  ``run_id`` is null until
    ``session.started`` binds the underlying execution run and therefore
    serves as the started indicator for cleanup semantics.

    Construction is validated: ``__post_init__`` re-validates every field
    through the same strict rules as ``from_mapping``, so no public
    construction path can produce an invalid event.  The payload is
    canonicalized into a frozen nested JSON structure (tuple-backed, no
    mutating protocol); caller-owned input and ``to_mapping()`` output are
    never shared with the event.
    """

    schema_version: str
    session_id: str
    task_id: str
    run_id: Optional[str]
    sequence: int
    timestamp_utc: str
    source_kind: SourceKind
    event_kind: SessionEventKind
    controller_phase: Optional[ControllerState]
    payload: Mapping[str, Any]

    _KNOWN_FIELDS = {
        "schema_version", "session_id", "task_id", "run_id", "sequence",
        "timestamp_utc", "source_kind", "event_kind", "controller_phase",
        "payload",
    }
    _REQUIRED_FIELDS = _KNOWN_FIELDS

    def __post_init__(self) -> None:
        if self.schema_version != SESSION_EVENT_SCHEMA_VERSION:
            raise SchemaValidationError(
                f"Unsupported session event schema version: {self.schema_version!r}"
            )
        validate_session_id(self.session_id)
        _identifier(self.task_id, "task_id")
        if self.run_id is not None:
            _identifier(self.run_id, "run_id")
        _nonneg_int(self.sequence, "sequence")
        validate_utc_timestamp(self.timestamp_utc)
        if type(self.source_kind) is not SourceKind:
            raise SchemaValidationError("source_kind must be a SourceKind")
        if type(self.event_kind) is not SessionEventKind:
            raise SchemaValidationError("event_kind must be a SessionEventKind")
        if self.controller_phase is not None and type(self.controller_phase) is not ControllerState:
            raise SchemaValidationError(
                "controller_phase must be a ControllerState or null"
            )
        if not isinstance(self.payload, Mapping):
            raise SchemaValidationError("event.payload must be a mapping")
        canonical = _PAYLOAD_VALIDATORS[self.event_kind](self.payload)
        object.__setattr__(self, "payload", _freeze(_detached(canonical)))

    @staticmethod
    def from_mapping(m: Any) -> SessionEvent:
        if not isinstance(m, Mapping):
            raise SchemaValidationError("event must be a mapping")
        _check_required(m, SessionEvent._REQUIRED_FIELDS, "event")
        _check_no_unknown(m, SessionEvent._KNOWN_FIELDS, "event")
        controller_phase_raw = m["controller_phase"]
        controller_phase = (
            None
            if controller_phase_raw is None
            else _enum(controller_phase_raw, "controller_phase", ControllerState)
        )
        return SessionEvent(
            schema_version=m["schema_version"],
            session_id=m["session_id"],
            task_id=m["task_id"],
            run_id=m["run_id"],
            sequence=m["sequence"],
            timestamp_utc=m["timestamp_utc"],
            source_kind=_enum(m["source_kind"], "source_kind", SourceKind),
            event_kind=_enum(m["event_kind"], "event_kind", SessionEventKind),
            controller_phase=controller_phase,
            payload=m["payload"],
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "timestamp_utc": self.timestamp_utc,
            "source_kind": self.source_kind.value,
            "event_kind": self.event_kind.value,
            "controller_phase": (
                self.controller_phase.value if self.controller_phase is not None else None
            ),
            "payload": _thaw(self.payload),
        }


# ---------------------------------------------------------------------------
# Complete-stream contract
# ---------------------------------------------------------------------------


def validate_session_event_stream(events: Sequence[SessionEvent]) -> None:
    """Validate one complete session event stream without reordering it.

    Rules: contiguous sequences from 0; constant session/task/source
    identity; ``session.created`` first; at most one ``session.started``
    which binds a constant ``run_id``; lifecycle transitions legal; at most
    one ``cancel_requested`` before the terminal event; exactly one terminal
    event in terminal position.

    Cleanup follows one deterministic lifecycle: ``cleanup.completed`` must
    follow an active ``cleanup.started``; a new ``cleanup.started`` may not
    begin while one is active; ``session.completed`` and (for a started
    session) ``session.cancelled`` require the terminal cleanup cycle to be
    completed with ``verified=True``; ``cleanup_failed`` requires an
    attempted cleanup that did not end verified.

    Incomplete (crash-interrupted) journals are a history concern and are
    not classified here.
    """

    if not events:
        raise ApplicationContractError("session event stream is empty")
    if events[0].event_kind is not SessionEventKind.SESSION_CREATED:
        raise ApplicationContractError(
            "session event stream must begin with session.created"
        )
    session_id = events[0].session_id
    task_id = events[0].task_id
    source_kind = events[0].source_kind
    status: SessionStatus = SessionStatus.CREATED
    run_id: Optional[str] = None
    started = False
    cancel_requested = False
    terminal_index: Optional[int] = None
    cleanup_active = False
    cleanup_started_ever = False
    last_completed_verified: Optional[bool] = None
    for index, event in enumerate(events):
        if event.sequence != index:
            raise ApplicationContractError(
                f"non-contiguous sequence at event {index}: "
                f"expected {index}, got {event.sequence}"
            )
        if event.session_id != session_id:
            raise ApplicationContractError("mixed session IDs in stream")
        if event.task_id != task_id:
            raise ApplicationContractError("mixed task IDs in stream")
        if event.source_kind is not source_kind:
            raise ApplicationContractError("mixed source kinds in stream")
        if terminal_index is not None:
            raise ApplicationContractError(
                f"events after terminal event at {terminal_index}"
            )
        kind = event.event_kind
        if started:
            if event.run_id is None:
                raise ApplicationContractError(
                    f"run_id missing after session.started at event {index}"
                )
            if run_id is not None and event.run_id != run_id:
                raise ApplicationContractError(
                    f"run_id changed at event {index}"
                )
        elif kind is not SessionEventKind.SESSION_STARTED and event.run_id is not None:
            raise ApplicationContractError(
                f"run_id present before session.started at event {index}"
            )
        if kind is SessionEventKind.SESSION_STARTED:
            if started:
                raise ApplicationContractError("duplicate session.started")
            if event.run_id is None:
                raise ApplicationContractError(
                    "session.started requires a run_id"
                )
            started = True
            run_id = event.run_id
            _apply_status(status, SessionStatus.STARTING, index)
            status = SessionStatus.STARTING
        elif kind is SessionEventKind.SESSION_STATUS_CHANGED:
            _apply_status(status, SessionStatus.RUNNING, index)
            status = SessionStatus.RUNNING
        elif kind is SessionEventKind.SESSION_CANCEL_REQUESTED:
            if cancel_requested:
                raise ApplicationContractError("duplicate session.cancel_requested")
            cancel_requested = True
        elif kind is SessionEventKind.CLEANUP_STARTED:
            if cleanup_active:
                raise ApplicationContractError(
                    f"duplicate cleanup.started at event {index} "
                    "(a cleanup cycle is already active)"
                )
            cleanup_active = True
            cleanup_started_ever = True
        elif kind is SessionEventKind.CLEANUP_COMPLETED:
            if not cleanup_active:
                raise ApplicationContractError(
                    f"cleanup.completed at event {index} without a "
                    "preceding cleanup.started"
                )
            cleanup_active = False
            last_completed_verified = event.payload["verified"]
        elif kind in (
            SessionEventKind.SESSION_COMPLETED,
            SessionEventKind.SESSION_FAILED,
            SessionEventKind.SESSION_CANCELLED,
        ):
            new_status = SessionStatus(event.payload["status"])
            _apply_status(status, new_status, index)
            # A previously verified cleanup does not authorize terminal
            # completion when a later cleanup cycle was started and remains
            # incomplete (or was never started for this terminal).
            effective_verified = (
                not cleanup_active and last_completed_verified is True
            )
            if kind is SessionEventKind.SESSION_COMPLETED:
                if not effective_verified:
                    raise ApplicationContractError(
                        "session.completed requires the terminal cleanup "
                        "cycle to be completed with verified=True"
                    )
            elif kind is SessionEventKind.SESSION_CANCELLED:
                # A session cancelled before it started has nothing to clean.
                if started and not effective_verified:
                    raise ApplicationContractError(
                        "session.cancelled requires the terminal cleanup "
                        "cycle to be completed with verified=True"
                    )
            elif new_status is SessionStatus.CLEANUP_FAILED:
                if not cleanup_started_ever:
                    raise ApplicationContractError(
                        "cleanup_failed requires an attempted cleanup"
                    )
                if effective_verified:
                    raise ApplicationContractError(
                        "cleanup_failed cannot follow verified cleanup"
                    )
            status = new_status
            terminal_index = index
    if terminal_index is None:
        raise ApplicationContractError("session event stream has no terminal event")
    if terminal_index != len(events) - 1:
        raise ApplicationContractError(
            f"events after terminal event at {terminal_index}"
        )


def _apply_status(current: SessionStatus, target: SessionStatus, index: int) -> None:
    if not can_transition(current, target):
        raise ApplicationContractError(
            f"illegal session status transition at event {index}: "
            f"{current.value} -> {target.value}"
        )


__all__ = [
    "MAX_BREAKPOINTS",
    "MAX_CHANGED_FILES",
    "MAX_IDENTIFIER_CHARS",
    "MAX_JSON_DEPTH",
    "MAX_LOCALS",
    "MAX_PATCH_TEXT_CHARS",
    "MAX_SHORT_TEXT_CHARS",
    "MAX_SHA256_HEX",
    "MAX_SOURCE_TEXT_CHARS",
    "MAX_STACK_FRAMES",
    "MAX_TEXT_CHARS",
    "MAX_TUPLE_TEXT_CHARS",
    "ModelRequestStatus",
    "SESSION_EVENT_SCHEMA_VERSION",
    "SessionEvent",
    "SessionEventKind",
    "SessionPhase",
    "SessionStatus",
    "SessionTerminationReason",
    "SourceKind",
    "SourceSnapshotStage",
    "VerifierStage",
    "VerifierStageStatus",
    "allowed_transitions",
    "can_transition",
    "compatible_reasons",
    "contains_credential_shape",
    "is_credential_name",
    "terminal_status_for",
    "validate_session_event_stream",
    "validate_session_id",
    "validate_utc_timestamp",
]
