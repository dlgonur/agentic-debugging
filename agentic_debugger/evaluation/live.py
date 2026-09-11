"""Opt-in live-model evaluation over the existing controller and verifier.

Thin compatibility facade: all behavior lives in the cohesive
``live_*`` modules; this module re-exports the complete historical
surface (including the private helpers consumed across the
repository) so existing imports keep working unchanged.
"""
from __future__ import annotations

# ``uuid``/``shutil`` are part of the historical module surface:
# tests address ``agentic_debugger.evaluation.live.uuid`` (logical
# identity) and ``live.shutil`` (cleanup failure injection).
import shutil, uuid

from agentic_debugger.evaluation.live_contracts import (
    LiveEvaluationError,
    LiveOptInError,
    LiveConfigurationError,
    LiveTransportError,
    ModelRequestBudgetExceeded,
    DirectiveRejectionCategory,
    MAX_REJECTION_DETAIL_CHARS,
    LiveModelAdapterError,
    LIVE_SCHEMA_VERSION,
    LIVE_PROTOCOL_VERSION,
    LIVE_CONFIG_SCHEMA_VERSION,
    MAX_MODEL_RESPONSE_BYTES,
    MODEL_HISTORY_WINDOW,
    DIRECTIVE_NORMALIZATION_SCHEMA_VERSION,
    DIRECTIVE_NORMALIZATION_POLICY_ID,
    DIRECTIVE_NORMALIZATION_POLICY,
    PDB_BREAKPOINT_SELECTION_SCHEMA_VERSION,
    PDB_BREAKPOINT_SELECTION_POLICY_ID,
    PDB_BREAKPOINT_SELECTION_POLICY,
    PROOF_HISTORY_WINDOW,
    MAX_COMMAND_ARGUMENTS,
    COMMAND_ERROR_SCHEMA_VERSION,
    PROVIDER_COMPLETION_ENVELOPE_SCHEMA,
    _TYPED_COMMAND_ERROR_KINDS,
    LIVE_DIRECTIVE_SCHEMA,
    LiveCaseStatus,
    _SECRET_KEY,
    _SECRET_VALUE,
    _SECRET_ARGUMENT,
    _USAGE_FIELDS,
    redact_for_recording,
    _proof_observation_for_provider,
    _command,
    LiveModelConfig,
    LiveExecutionAuthorization,
    LiveRunLimits,
    LiveTreatmentBudget,
    _SAFE_RUN_LABEL,
    _new_evaluation_identity,
)
from agentic_debugger.evaluation.live_transport import (
    ModelTransport,
    _BoundedCapture,
    _read_pipe,
    _terminate_process,
    _typed_command_error_detail,
    _typed_command_error_kind,
    JsonlCommandTransport,
)
from agentic_debugger.evaluation.live_usage import (
    _AttemptUsage,
    _compute_attempt_usage,
    LiveModelMetrics,
    _LogicalRequestUsage,
)
from agentic_debugger.evaluation.live_directive_schema import (
    _rejected,
    _require_field,
    _PDB_ACTIONS,
    _SESSION_ACTIONS,
    _directive_schema_for_state,
    _action_contracts_for_state,
    _validate_enum_constrained_arguments,
    _legal_transition_targets,
)
from agentic_debugger.evaluation.live_directive_parse import (
    _resolve_raw_directive,
    _normalize_redundant_trailing_brace,
    _normalization_content_record,
    _normalize_exact_json_markdown_fence,
    _normalize_prose_wrapped_exact_json_markdown_fence,
    _normalize_prose_wrapped_exact_json_object,
    _normalize_unterminated_exact_json_markdown_fence,
    _normalize_exact_json_fence_then_redundant_trailing_brace,
    _resolve_provider_directive,
    _validate_directive_constraints,
    _parse,
    validate_synthetic_qualification_content,
)
from agentic_debugger.evaluation.live_adapter import (
    LiveModelAdapter,
)
from agentic_debugger.evaluation.live_finalize import (
    LiveCaseResult,
    RejectedLiveReport,
    rejected_live_report,
    _project_events_safe,
    _owned_case_dir,
    _owned_evaluation_dir,
    _remove_owned_evaluation_dir,
    _remove_owned_case_dir,
    _interrupted_case_result,
    _finalize_live_case,
)
from agentic_debugger.evaluation.live_operation import (
    _ControllerOperationObserver,
    _acceptance_live_case,
    run_live_case,
    _acceptance_live_evaluation,
    run_live_evaluation,
)
from agentic_debugger.evaluation.live_report import (
    render_live_report,
    _schema_error,
    _require_fields,
    _string,
    _boolean,
    _count,
    _optional_mapping,
    _validate_counter_pair,
    _validate_case,
    _validate_configuration_metadata,
    validate_live_report,
)
from agentic_debugger.evaluation.live_operation import (
    DeterministicController,
    EvaluationVerifier,
)

__all__=["DirectiveRejectionCategory","JsonlCommandTransport","LiveCaseResult","LiveCaseStatus","LiveConfigurationError","LiveEvaluationError","LiveExecutionAuthorization","LiveModelAdapter","LiveModelAdapterError","LiveModelConfig","LiveModelMetrics","LiveOptInError","LiveRunLimits","LiveTreatmentBudget","LiveTransportError","ModelTransport","LIVE_PROTOCOL_VERSION","LIVE_SCHEMA_VERSION","redact_for_recording","render_live_report","rejected_live_report","run_live_case","run_live_evaluation","validate_live_report"]
