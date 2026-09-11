"""Per-kind payload contracts for application session events.

This module owns the strict per-:class:`~agentic_debugger.application.event_contracts.SessionEventKind`
payload validators and the ``_PAYLOAD_VALIDATORS`` registry they form.
Each validator enforces the exact required/optional field set, bound, and
semantic-coherence rules for one event kind's payload; the registry is the
single validation authority consulted by
:class:`~agentic_debugger.application.events.SessionEvent`.

Dependency rule: builds on the vocabulary/bound constants from
:mod:`agentic_debugger.application.event_contracts` and the bounded-value
primitives from :mod:`agentic_debugger.application.event_fields`.  It never
imports controller, verifier, PDB, patch, demo, live-model, or experiment
code beyond the lightweight enums/taxonomies used for validation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Dict, Optional

from agentic_debugger import SchemaValidationError
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.token_usage import (
    TOKEN_USAGE_PAYLOAD_FIELDS,
    TokenUsageCoverage,
    coverage_from_payload,
    usage_from_payload,
)
from agentic_debugger.application.event_contracts import (
    MAX_BREAKPOINTS,
    MAX_CHANGED_FILES,
    MAX_IDENTIFIER_CHARS,
    MAX_LOCALS,
    MAX_PATCH_TEXT_CHARS,
    MAX_SHORT_TEXT_CHARS,
    MAX_SOURCE_TEXT_CHARS,
    MAX_STACK_FRAMES,
    MAX_TEXT_CHARS,
    ModelRequestStatus,
    OperatorStage,
    SessionEventKind,
    SessionPhase,
    SessionStatus,
    SessionTerminationReason,
    SourceSnapshotStage,
    VerifierStage,
    VerifierStageStatus,
    _compatible_reasons,
    contains_credential_shape,
)
from agentic_debugger.application.event_fields import (
    _bool,
    _bool_or_none,
    _bounded_text,
    _bounded_text_or_none,
    _check_no_unknown,
    _check_required,
    _enum,
    _frame_mapping,
    _int_or_none,
    _local_mapping,
    _multiline_text,
    _multiline_text_or_none,
    _nonneg_int,
    _sha256_hex,
    _string_tuple,
)
from agentic_debugger.evaluation.outcome_taxonomy import SemanticOutcome
from agentic_debugger.evaluation.runner import EvaluationStatus
from agentic_debugger.events.schema import ObservationStatus, validate_json_compatible


def _payload_created(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {"spec_fingerprint"}
    optional = {"retry_of_session_id"}
    _check_required(payload, required, "session.created payload")
    _check_no_unknown(payload, required | optional, "session.created payload")
    result = {"spec_fingerprint": _sha256_hex(payload["spec_fingerprint"], "spec_fingerprint")}
    if "retry_of_session_id" in payload:
        result["retry_of_session_id"] = _bounded_text(
            payload["retry_of_session_id"], "retry_of_session_id", 128
        )
    return result


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


def _payload_operator_progress(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate observer-only progress from an authoritative ladder run."""

    if not isinstance(payload, Mapping):
        raise SchemaValidationError("operator.progress payload must be a mapping")
    required = {"stage"}
    # ``official_execution_proven`` is the additive typed milestone: it is
    # emitted only after the operator observed official test execution and
    # lets presentation state carry that fact without parsing ``detail``.
    optional = {"detail", "official_execution_proven"}
    _check_required(payload, required, "operator.progress payload")
    _check_no_unknown(payload, required | optional, "operator.progress payload")
    stage = _enum(payload["stage"], "stage", OperatorStage)
    detail = payload.get("detail")
    if detail is not None:
        if type(detail) is not str or not detail or len(detail.encode("utf-8")) > 512:
            raise SchemaValidationError("operator.progress detail is invalid")
    proven = payload.get("official_execution_proven")
    if proven is not None and type(proven) is not bool:
        raise SchemaValidationError("official_execution_proven must be a bool")
    result: dict[str, Any] = {"stage": stage.value, "detail": detail}
    if proven is not None:
        result["official_execution_proven"] = proven
    return result


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
    executable object.  Optional additive fields ``route``
    (``direct_api``/``legacy_cli``) and ``api_protocol`` (the provider
    protocol family) distinguish how a subscription provider served a
    session without changing historical payloads.

    Task-28 additive repair: the strict schema also accepts and preserves
    the SAFE ``ModelBinding`` runtime provenance emitted by
    ``ModelBinding.model_configured_payload()`` — ``model_binding_fingerprint``,
    ``effective_protocol``, ``endpoint_contract``, ``transport_profile``,
    and ``provider_runtime_identity``.  These fields are OPTIONAL so that
    historical ``session-event-v1`` journals and routes without
    provider-runtime authority (configured command-profile, offline, ladder)
    remain valid.  The overall session-event schema version is unchanged:
    optional ``model.configured`` fields are additive by convention.

    Alias coherence: ``api_protocol``/``effective_protocol`` and
    ``endpoint_contract``/``transport_profile`` originate from one
    ``ModelBinding``.  When both members of a pair are present they must
    agree; contradictory aliases fail closed rather than journaling
    ambiguous runtime provenance.
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
    optional = {
        "treatment_revision",
        "treatment_id",
        "result_location",
        "provider",
        "route",
        "api_protocol",
        "auth_mode",
        "provider_model_id",
        "endpoint",
        "model_binding_fingerprint",
        "effective_protocol",
        "endpoint_contract",
        "transport_profile",
        "provider_runtime_identity",
    }
    _check_required(payload, required, "model.configured payload")
    _check_no_unknown(payload, required | optional, "model.configured payload")
    result = {
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
    if "treatment_revision" in payload:
        result["treatment_revision"] = _nonneg_int(payload["treatment_revision"], "treatment_revision")
    if "treatment_id" in payload:
        result["treatment_id"] = _bounded_text(payload["treatment_id"], "treatment_id", MAX_IDENTIFIER_CHARS)
    if "result_location" in payload:
        result["result_location"] = _bounded_text(payload["result_location"], "result_location", MAX_SHORT_TEXT_CHARS)
    if "provider" in payload:
        result["provider"] = _bounded_text(payload["provider"], "provider", MAX_SHORT_TEXT_CHARS)
    if "route" in payload:
        result["route"] = _bounded_text(payload["route"], "route", MAX_SHORT_TEXT_CHARS)
    if "api_protocol" in payload:
        result["api_protocol"] = _bounded_text(payload["api_protocol"], "api_protocol", MAX_SHORT_TEXT_CHARS)
    if "auth_mode" in payload:
        result["auth_mode"] = _bounded_text(payload["auth_mode"], "auth_mode", MAX_SHORT_TEXT_CHARS)
    if "provider_model_id" in payload:
        result["provider_model_id"] = _bounded_text(payload["provider_model_id"], "provider_model_id", MAX_SHORT_TEXT_CHARS)
    if "endpoint" in payload:
        result["endpoint"] = _bounded_text(payload["endpoint"], "endpoint", MAX_SHORT_TEXT_CHARS)
    if "model_binding_fingerprint" in payload:
        result["model_binding_fingerprint"] = _sha256_hex(
            payload["model_binding_fingerprint"], "model_binding_fingerprint"
        )
    if "provider_runtime_identity" in payload:
        result["provider_runtime_identity"] = _sha256_hex(
            payload["provider_runtime_identity"], "provider_runtime_identity"
        )
    if "effective_protocol" in payload:
        result["effective_protocol"] = _bounded_text(
            payload["effective_protocol"], "effective_protocol", MAX_SHORT_TEXT_CHARS
        )
    if "endpoint_contract" in payload:
        result["endpoint_contract"] = _bounded_text(
            payload["endpoint_contract"], "endpoint_contract", MAX_SHORT_TEXT_CHARS
        )
    if "transport_profile" in payload:
        result["transport_profile"] = _bounded_text(
            payload["transport_profile"], "transport_profile", MAX_SHORT_TEXT_CHARS
        )
    if "api_protocol" in result and "effective_protocol" in result:
        if result["api_protocol"] != result["effective_protocol"]:
            raise SchemaValidationError(
                "model.configured api_protocol and effective_protocol must agree"
            )
    if "endpoint_contract" in result and "transport_profile" in result:
        if result["endpoint_contract"] != result["transport_profile"]:
            raise SchemaValidationError(
                "model.configured endpoint_contract and transport_profile must agree"
            )
    return result


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
    optional = {"error_kind", "error_message", "token_usage", "token_usage_coverage"}
    _check_required(payload, required, "model.request_completed payload")
    _check_no_unknown(payload, required | optional, "model.request_completed payload")
    result = {
        "request_index": _nonneg_int(payload["request_index"], "request_index"),
        "status": _enum(payload["status"], "status", ModelRequestStatus).value,
    }
    has_kind = "error_kind" in payload
    has_message = "error_message" in payload
    if has_kind != has_message:
        raise SchemaValidationError(
            "model.request_completed error_kind and error_message must appear together"
        )
    if has_kind:
        if result["status"] == ModelRequestStatus.OK.value:
            raise SchemaValidationError(
                "successful model.request_completed cannot carry error detail"
            )
        error_kind = _bounded_text(
            payload["error_kind"], "error_kind", MAX_IDENTIFIER_CHARS
        )
        error_message = _bounded_text(
            payload["error_message"], "error_message", MAX_SHORT_TEXT_CHARS
        )
        assert error_kind is not None and error_message is not None
        if contains_credential_shape(error_kind) or contains_credential_shape(error_message):
            raise SchemaValidationError(
                "model.request_completed error detail contains credential-shaped text"
            )
        result["error_kind"] = error_kind
        result["error_message"] = error_message
    if "token_usage" in payload:
        # Optional provider-reported counts (additive v1 field): strict
        # non-negative integer dimensions, unknown-field rejection, and
        # coverage-aware semantic consistency. Historical events without the
        # block remain valid; nothing here is derived or defaulted.
        coverage: Optional[TokenUsageCoverage] = None
        if "token_usage_coverage" in payload:
            try:
                coverage = coverage_from_payload(payload["token_usage_coverage"], payload["token_usage"])
            except ValueError as exc:
                raise SchemaValidationError(
                    f"model.request_completed token_usage_coverage is invalid: {exc}"
                ) from None
        try:
            usage_from_payload(payload["token_usage"], coverage=coverage)
        except ValueError as exc:
            raise SchemaValidationError(
                f"model.request_completed token_usage is invalid: {exc}"
            ) from None
        block = payload["token_usage"]
        result["token_usage"] = {
            field: block[field]
            for field in TOKEN_USAGE_PAYLOAD_FIELDS
            if field in block
        }
        if "token_usage_coverage" in payload:
            cov_block = payload["token_usage_coverage"]
            result["token_usage_coverage"] = {
                field: cov_block[field]
                for field in TOKEN_USAGE_PAYLOAD_FIELDS
                if field in cov_block
            }
    elif "token_usage_coverage" in payload:
        raise SchemaValidationError(
            "model.request_completed cannot carry token_usage_coverage without token_usage"
        )
    return result


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
    # ``target`` is the additive structured-operation refinement: a bounded
    # logical target (e.g. ``cookiecutter/config.py:40-80``) the producing
    # boundary genuinely owns.  It is optional so historical tool events and
    # the controller-observer projection stay valid without it.
    required = {"tool_name"} if not completed else {"tool_name", "status"}
    optional = {"target"}
    _check_required(payload, required, label)
    _check_no_unknown(payload, required | optional, label)
    result = {
        "tool_name": _bounded_text(payload["tool_name"], "tool_name", MAX_IDENTIFIER_CHARS)
    }
    target = payload.get("target")
    if target is not None:
        result["target"] = _bounded_text_or_none(target, "target", MAX_SHORT_TEXT_CHARS)
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
    optional = {"evidence_refs", "observed_values", "proof_contract"}
    _check_required(payload, required, "diagnosis.recorded payload")
    _check_no_unknown(payload, required | optional, "diagnosis.recorded payload")
    result = {
        "text": _multiline_text_or_none(payload["text"], "text", MAX_TEXT_CHARS),
        "file_path": _bounded_text_or_none(
            payload["file_path"], "file_path", MAX_SHORT_TEXT_CHARS
        ),
        "symbol": _bounded_text_or_none(payload["symbol"], "symbol", MAX_IDENTIFIER_CHARS),
        "confidence": _bounded_text_or_none(
            payload["confidence"], "confidence", MAX_SHORT_TEXT_CHARS
        ),
    }
    if "evidence_refs" in payload:
        result["evidence_refs"] = _string_tuple(
            payload["evidence_refs"], "evidence_refs", max_items=32
        )
    if "observed_values" in payload:
        values = payload["observed_values"]
        if not isinstance(values, Mapping):
            raise SchemaValidationError("observed_values must be a mapping")
        try:
            validate_json_compatible(values)
            if len(json.dumps(values, ensure_ascii=False, separators=(",", ":"))) > MAX_TEXT_CHARS:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise SchemaValidationError("observed_values must be bounded JSON") from None
        result["observed_values"] = dict(values)
    if "proof_contract" in payload:
        contract = payload["proof_contract"]
        if not isinstance(contract, Mapping):
            raise SchemaValidationError("proof_contract must be a mapping")
        required_contract = {
            "exact_reproduction", "task_id", "reproduction_argv", "pytest_node",
            "workspace_id", "production_file", "production_file_sha256",
            "breakpoint_line", "production_frame",
        }
        _check_required(contract, required_contract, "proof_contract")
        _check_no_unknown(contract, required_contract, "proof_contract")
        result["proof_contract"] = {
            "exact_reproduction": _bool(contract["exact_reproduction"], "exact_reproduction"),
            "task_id": _bounded_text(contract["task_id"], "task_id", MAX_IDENTIFIER_CHARS),
            "reproduction_argv": _string_tuple(contract["reproduction_argv"], "reproduction_argv", max_items=32),
            "pytest_node": _bounded_text(contract["pytest_node"], "pytest_node", MAX_SHORT_TEXT_CHARS),
            "workspace_id": _bounded_text(contract["workspace_id"], "workspace_id", MAX_SHORT_TEXT_CHARS),
            "production_file": _bounded_text(contract["production_file"], "production_file", MAX_SHORT_TEXT_CHARS),
            "production_file_sha256": _sha256_hex(contract["production_file_sha256"], "production_file_sha256"),
            "breakpoint_line": _nonneg_int(contract["breakpoint_line"], "breakpoint_line"),
            "production_frame": _bounded_text(contract["production_frame"], "production_frame", MAX_IDENTIFIER_CHARS),
        }
    return result


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
    optional = {
        "private_checks_passed",
        "classification",
        "official_test_execution_proven",
    }
    _check_required(payload, required, "verifier.completed payload")
    _check_no_unknown(payload, required | optional, "verifier.completed payload")
    status = payload["status"]
    if status is not None:
        status = _enum(status, "status", EvaluationStatus).value
    outcome = payload["outcome"]
    if outcome is not None:
        outcome = _enum(outcome, "outcome", SemanticOutcome).value
    result = {
        "status": status,
        "outcome": outcome,
        "f2p_passed": _int_or_none(payload["f2p_passed"], "f2p_passed"),
        "f2p_total": _int_or_none(payload["f2p_total"], "f2p_total"),
        "p2p_passed": _int_or_none(payload["p2p_passed"], "p2p_passed"),
        "p2p_total": _int_or_none(payload["p2p_total"], "p2p_total"),
        "workspace_cleaned": _bool_or_none(payload["workspace_cleaned"], "workspace_cleaned"),
    }
    if "private_checks_passed" in payload:
        result["private_checks_passed"] = _bool(payload["private_checks_passed"], "private_checks_passed")
    if "classification" in payload:
        result["classification"] = _bounded_text_or_none(payload["classification"], "classification", MAX_SHORT_TEXT_CHARS)
    if "official_test_execution_proven" in payload:
        result["official_test_execution_proven"] = _bool(
            payload["official_test_execution_proven"],
            "official_test_execution_proven",
        )
    return result


def _payload_cleanup_completed(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("cleanup.completed payload must be a mapping")
    required = {"verified"}
    _check_required(payload, required, "cleanup.completed payload")
    _check_no_unknown(payload, required, "cleanup.completed payload")
    return {"verified": _bool(payload["verified"], "verified")}


def _payload_cleanup_not_required(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("cleanup.not_required payload must be a mapping")
    _check_no_unknown(payload, set(), "cleanup.not_required payload")
    if payload:
        raise SchemaValidationError("cleanup.not_required payload must be empty")
    return {}


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
    SessionEventKind.OPERATOR_PROGRESS: _payload_operator_progress,
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
    SessionEventKind.CLEANUP_NOT_REQUIRED: _payload_cleanup_not_required,
    SessionEventKind.ARTIFACT_WRITTEN: _payload_artifact_written,
}
