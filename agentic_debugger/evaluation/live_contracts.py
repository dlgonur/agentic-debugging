"""Live-model opt-in, configuration, authorization, and budget contracts.

This module owns the configuration/authorization layer of live
evaluation: the live exception taxonomy (including the historical
``ModelRequestBudgetExceeded`` compatibility signal), rejection
categories, schema/policy/version constants, the directive and
breakpoint-selection policies, secret redaction, the provider-facing
proof-observation projection, and the frozen configuration,
authorization, run-limit, treatment-budget, and evaluation-identity
contracts.  No provider execution happens here."""

from __future__ import annotations

import hashlib, json, re, uuid

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits, HypothesisConfidence, HypothesisStatus,
)
from agentic_debugger.agent.model_adapter import ModelAdapterError


class LiveEvaluationError(RuntimeError): pass
class LiveOptInError(LiveEvaluationError): pass
class LiveConfigurationError(LiveEvaluationError): pass
class LiveTransportError(LiveEvaluationError):
    def __init__(self, message: str, *, kind: str = "transport_error", timed_out: bool = False, safe_message: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.timed_out = timed_out
        self.safe_message = safe_message

class ModelRequestBudgetExceeded(LiveEvaluationError):
    """Historical transport signal: the frozen public-evidence budget.

    Retained for backward compatibility with frozen campaign evidence and
    historical transports only.  New execution never raises it: model
    request size is provider-owned (Task 43), so Agentic Debugger hands
    the intended request to the configured transport regardless of its
    serialized size.  A provider that rejects an oversized request
    surfaces a provider/transport failure (``request_too_large`` as an
    EXTERNAL provider error kind), never this pre-transport signal.
    The ``except ModelRequestBudgetExceeded`` handler in
    :meth:`LiveModelAdapter.next_directive` and the
    ``public_evidence_budget_exceeded`` terminalization below exist solely
    to interpret historical evidence; they are unreachable in new runs.
    """

    def __init__(self, request_byte_count: int, limit: int) -> None:
        super().__init__(
            f"Canonical public request exceeds the public-evidence byte budget "
            f"({request_byte_count} > {limit})"
        )
        self.request_byte_count = int(request_byte_count)
        self.limit = int(limit)

class DirectiveRejectionCategory(str, Enum):
    """Closed vocabulary for why a provider-completed directive was rejected.

    Only these bounded, pre-authored strings ever reach the retry-feedback
    context; raw provider text is never echoed back into it.
    """
    ILLEGAL_ACTION = "illegal_action"
    ILLEGAL_TRANSITION = "illegal_transition"
    INVALID_ARGUMENT_VALUE = "invalid_argument_value"
    MALFORMED_DIRECTIVE = "malformed_directive"
    AMBIGUOUS_ENVELOPE = "ambiguous_response_envelope"

MAX_REJECTION_DETAIL_CHARS = 200

class LiveModelAdapterError(ModelAdapterError):
    def __init__(self, message: str, *, category: "DirectiveRejectionCategory" = DirectiveRejectionCategory.MALFORMED_DIRECTIVE, detail: str = "", stage: str | None = None, reason_code: str | None = None, content: str | None = None, directive_rejection: bool = False, error_kind: str | None = None, safe_message: str | None = None):
        super().__init__(message)
        self.category = category
        text = str(detail)
        self.detail = text if len(text) <= MAX_REJECTION_DETAIL_CHARS else text[:MAX_REJECTION_DETAIL_CHARS - 3] + "..."
        default_stages = {
            DirectiveRejectionCategory.ILLEGAL_ACTION: ("illegal_action", "illegal_action"),
            DirectiveRejectionCategory.ILLEGAL_TRANSITION: ("illegal_transition", "illegal_transition"),
            DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE: ("invalid_arguments", "invalid_argument"),
            DirectiveRejectionCategory.AMBIGUOUS_ENVELOPE: ("envelope_failure", "ambiguous_envelope"),
            DirectiveRejectionCategory.MALFORMED_DIRECTIVE: ("schema_failure", "malformed_directive"),
        }
        default_stage, default_reason = default_stages[category]
        self.stage = stage or default_stage
        self.reason_code = reason_code or default_reason
        self.content = content if type(content) is str else None
        self.directive_rejection = directive_rejection
        effective_error_kind = error_kind or (
            self.reason_code if directive_rejection else None
        )
        self.error_kind = (
            effective_error_kind
            if type(effective_error_kind) is str
            and effective_error_kind
            and len(effective_error_kind.encode("utf-8", errors="ignore")) <= 64
            else None
        )
        if safe_message is None and directive_rejection and self.detail:
            safe_message = self.detail
        if type(safe_message) is str and safe_message:
            safe_text = str(redact_for_recording(safe_message)).replace("\r", " ").replace("\n", " ")
            encoded = safe_text.encode("utf-8", errors="replace")
            if len(encoded) > MAX_REJECTION_DETAIL_CHARS:
                safe_text = encoded[: MAX_REJECTION_DETAIL_CHARS - 3].decode(
                    "utf-8", errors="ignore"
                ) + "..."
            self.safe_message = safe_text
        else:
            self.safe_message = None

LIVE_SCHEMA_VERSION = "1.1"
LIVE_PROTOCOL_VERSION = "1.3"
LIVE_CONFIG_SCHEMA_VERSION = "1.0"
MAX_MODEL_RESPONSE_BYTES = 1_048_576
MODEL_HISTORY_WINDOW = 32
DIRECTIVE_NORMALIZATION_SCHEMA_VERSION = "directive-normalization-v3"
DIRECTIVE_NORMALIZATION_POLICY_ID = (
    "redundant-trailing-brace-v1-or-exact-json-markdown-fence-v1-or-"
    "exact-json-markdown-fence-then-redundant-trailing-brace-v1-or-"
    "unterminated-exact-json-markdown-fence-v1-or-"
    "prose-wrapped-exact-json-markdown-fence-v1-or-"
    "prose-wrapped-exact-json-object-v1"
)
DIRECTIVE_NORMALIZATION_POLICY = {
    "enabled": True,
    "schema_version": DIRECTIVE_NORMALIZATION_SCHEMA_VERSION,
    "policy_id": DIRECTIVE_NORMALIZATION_POLICY_ID,
    "strict_json_first": True,
    "leading_json_whitespace_allowed": True,
    "trailing_json_whitespace_allowed": True,
    "exactly_one_redundant_trailing_closing_brace": True,
    "exact_json_markdown_fence": {
        "enabled": True,
        "policy_id": "exact-lowercase-json-lf-fence-v1",
        "opening": "```json\\n",
        "closing": "\\n```",
        "optional_outer_json_whitespace": True,
        "top_level_mapping_required": True,
        "strict_inner_json_whitespace_allowed": True,
        "composed_repairs": {
            "only_ordered_branch": "exact_json_markdown_fence_then_one_redundant_trailing_closing_brace",
            "general_chaining_rejected": True,
            "reverse_order_rejected": True,
        },
    },
    "unterminated_exact_json_markdown_fence": {
        "enabled": True,
        "policy_id": "unterminated-exact-lowercase-json-lf-fence-v1",
        "opening": "```json\\n",
        "closing": "absent",
        "optional_outer_json_whitespace": True,
        "top_level_mapping_required": True,
        "strict_inner_json_whitespace_allowed": True,
        "trailing_prose_rejected": True,
    },
    "prose_wrapped_exact_json_markdown_fence": {
        "enabled": True,
        "policy_id": "prose-wrapped-exact-lowercase-json-lf-fence-v1",
        "opening": "```json\\n",
        "closing": "\\n```",
        "exactly_one_fence_pair_required": True,
        "top_level_mapping_required": True,
        "prose_ignored_outside_fence": True,
        "nested_fences_rejected": True,
    },
    "prose_wrapped_exact_json_object": {
        "enabled": True,
        "policy_id": "prose-wrapped-exact-json-object-v1",
        "top_level_mapping_required": True,
        "prefix_and_suffix_braces_rejected": True,
        "multiple_objects_rejected": True,
    },
    "multiple_redundant_delimiters_rejected": True,
    "trailing_prose_rejected": True,
    "multiple_objects_rejected": True,
    "semantic_repair_disabled": True,
}
PDB_BREAKPOINT_SELECTION_SCHEMA_VERSION = "pdb-breakpoint-selection-v1"
PDB_BREAKPOINT_SELECTION_POLICY_ID = "model-selected-runtime-validated-v1"
PDB_BREAKPOINT_SELECTION_POLICY = {
    "schema_version": PDB_BREAKPOINT_SELECTION_SCHEMA_VERSION,
    "policy_id": PDB_BREAKPOINT_SELECTION_POLICY_ID,
    "model_selects": "positive_integer",
    "minimum": 1,
    "exact_line_enum": False,
    "model_value_rewrite": False,
    "runtime_validation_authority": "start_pdb_session",
    "target_scope": "configured_production_file_and_focus_function",
    "proof_line_binding": "actual_runtime_pause_line",
    "failed_start": {
        "proof_observation": False,
        "valid_session_allowance": False,
        "provider_retry": False,
    },
}
# Ten entries retain the complete bounded exact-PDB sequence at diagnosis and
# patch time while dropping repeated early baseline/source payloads.  This is
# count-based context management (a fixed number of history entries), not a
# request-size ceiling: model request size is provider-owned and no
# byte/token/character bound is enforced on the request below.
PROOF_HISTORY_WINDOW = 10
MAX_COMMAND_ARGUMENTS = 32
COMMAND_ERROR_SCHEMA_VERSION = "command-error-v1"
PROVIDER_COMPLETION_ENVELOPE_SCHEMA = "provider-completion-v1"
_TYPED_COMMAND_ERROR_KINDS = frozenset({
    "adapter_error",
    "configuration",
    "http_error",
    "invalid_completion",
    "invalid_directive",
    "invalid_request",
    "invalid_response",
    "logical_call_limit",
    "model_mismatch",
    "preflight_failed",
    "request_too_large",
    "response_too_large",
    "timeout",
    "tool_call_rejected",
})
LIVE_DIRECTIVE_SCHEMA={
    "action":{"kind":"action","required":["name","arguments"]},
    "transition":{"kind":"transition","required":["target_state","reason"]},
    "add_hypothesis":{"kind":"add_hypothesis","required":["hypothesis_id","statement","confidence","evidence_refs","requires_runtime_evidence"],"constraints":{"confidence":{"type":"string","enum":[item.value for item in HypothesisConfidence]}}},
    "revise_hypothesis":{"kind":"revise_hypothesis","required":["hypothesis_id","statement","confidence","evidence_refs","requires_runtime_evidence"],"constraints":{"confidence":{"type":"string","enum":[item.value for item in HypothesisConfidence]}}},
    "set_hypothesis_status":{"kind":"set_hypothesis_status","required":["hypothesis_id","status"],"constraints":{"status":{"type":"string","enum":[item.value for item in HypothesisStatus if item is not HypothesisStatus.ACTIVE]}}},
}
class LiveCaseStatus(str, Enum):
    RESOLVED="RESOLVED"; UNRESOLVED="UNRESOLVED"; BUDGET_LIMITED="BUDGET_LIMITED"; PDB_NOT_REACHED="PDB_NOT_REACHED"; VALIDATION_NOT_REACHED="VALIDATION_NOT_REACHED"; CONTROLLER_FAILED="CONTROLLER_FAILED"; CONTROLLER_REJECTED="CONTROLLER_REJECTED"; MODEL_DIRECTIVE_REJECTED="MODEL_DIRECTIVE_REJECTED"; TIMED_OUT="TIMED_OUT"; PROVIDER_ERROR="PROVIDER_ERROR"; VERIFIER_FAILED="VERIFIER_FAILED"; EVENT_REPORTING_FAILED="EVENT_REPORTING_FAILED"; CLEANUP_FAILED="CLEANUP_FAILED"; HARNESS_ERROR="HARNESS_ERROR"; INCOMPLETE="INCOMPLETE"

_SECRET_KEY=re.compile(r"(?:api[_-]?key|access[_-]?key|auth(?:orization)?|credential|password|secret|token|private[_-]?key)",re.I)
_SECRET_VALUE=re.compile(r"(?i)\b(?:bearer|basic)\s+\S+|\b(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|token)\s*[:=]\s*\S+")
_SECRET_ARGUMENT=re.compile(r"^--?(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|private[_-]?key|token)(?:=|$)",re.I)
_USAGE_FIELDS={"prompt_tokens","completion_tokens","total_tokens","cached_input_tokens","cache_write_input_tokens","provider_reported","missing_fields"}
def redact_for_recording(value:Any, *, _usage_context:bool=False, _event_metadata_context:bool=False)->Any:
    if isinstance(value,Mapping):
        result={}
        for key,item in value.items():
            name=str(key)
            if name == "token_usage" and isinstance(item,Mapping):
                result[name]=redact_for_recording(item,_usage_context=True,_event_metadata_context=False)
            elif name == "metadata" and isinstance(item,Mapping) and set(item).issubset({"duration_ms","tool_version","model","tokens","cost"}) and {"duration_ms","tool_version","model","tokens","cost"}.issubset(set(item)):
                result[name]=redact_for_recording(item,_usage_context=False,_event_metadata_context=True)
            elif _usage_context and name in _USAGE_FIELDS and ((name in {"prompt_tokens","completion_tokens","total_tokens","cached_input_tokens","cache_write_input_tokens"} and (type(item) is int or item is None)) or (name=="provider_reported" and type(item) is bool) or (name=="missing_fields" and isinstance(item,list))):
                result[name]=redact_for_recording(item,_usage_context=False,_event_metadata_context=False)
            elif _event_metadata_context and name=="tokens" and (type(item) is int or item is None):
                result[name]=item
            elif _SECRET_KEY.search(name):
                result[name]="<redacted>"
            else:
                result[name]=redact_for_recording(item,_usage_context=False,_event_metadata_context=False)
        return result
    if isinstance(value,(list,tuple)): return [redact_for_recording(v,_usage_context=False,_event_metadata_context=False) for v in value]
    return _SECRET_VALUE.sub("<redacted>",value) if isinstance(value,str) else value


def _proof_observation_for_provider(value: Any) -> Any:
    """Keep exact-PDB semantics while omitting duplicated audit metadata.

    Authoritative observations and events remain unchanged. This projection
    is used only in model requests, where verbose safe-value metadata and
    repeated proof identity fields otherwise duplicate the same evidence.
    """

    if not isinstance(value, Mapping):
        return value
    # action/run/task identities are already present in the surrounding
    # controller request. Observation id, name, status, payload, and bounded
    # summary are the complete model-relevant semantics.
    result = {
        field: value[field]
        for field in (
            "observation_id",
            "name",
            "status",
            "payload",
            "summary",
            "truncated",
        )
        if field in value
    }
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        return result
    compact_payload = dict(payload)
    proof = payload.get("proof")
    if isinstance(proof, Mapping):
        compact_payload["proof"] = {
            field: proof[field]
            for field in (
                "exact_reproduction",
                "production_file",
                "production_frame",
                "breakpoint_line",
            )
            if field in proof
        }
    result["payload"] = compact_payload
    return result

def _command(value):
    if not isinstance(value,(list,tuple)) or not value or len(value)>MAX_COMMAND_ARGUMENTS or any(type(v) is not str or not v.strip() or "\x00" in v for v in value): raise LiveConfigurationError("live command is missing or invalid")
    if any(_SECRET_ARGUMENT.search(v) or _SECRET_VALUE.search(v) for v in value): raise LiveConfigurationError("live command must not contain credential arguments")
    return tuple(value)

@dataclass(frozen=True)
class LiveModelConfig:
    model_name:str; command:tuple[str,...]; request_timeout_seconds:float=60.0; tool_version:str="live-command-v1"
    def __post_init__(self):
        if type(self.model_name) is not str or not self.model_name.strip(): raise LiveConfigurationError("live model name is missing")
        if _SECRET_VALUE.search(self.model_name): raise LiveConfigurationError("live model name contains a credential-shaped value")
        _command(self.command)
        if type(self.request_timeout_seconds) not in (int,float) or not 0<self.request_timeout_seconds<=3600: raise LiveConfigurationError("live request timeout is invalid")
        if type(self.tool_version) is not str or not self.tool_version.strip() or _SECRET_VALUE.search(self.tool_version): raise LiveConfigurationError("live tool version is invalid")
    @classmethod
    def from_mapping(cls,value):
        allowed={"schema_version","model_name","command","request_timeout_seconds","tool_version"}
        if not isinstance(value,Mapping): raise LiveConfigurationError("live configuration must be an object")
        if set(value)-allowed: raise LiveConfigurationError("live configuration contains an unsupported field")
        if value.get("schema_version","1.0")!="1.0": raise LiveConfigurationError("unsupported live configuration version")
        return cls(value.get("model_name",""),_command(value.get("command")),value.get("request_timeout_seconds",60.0),value.get("tool_version","live-command-v1"))
    @classmethod
    def from_file(cls,path):
        try: value=json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError: raise LiveConfigurationError("live configuration is missing") from None
        except (OSError,UnicodeError,json.JSONDecodeError,TypeError): raise LiveConfigurationError("live configuration could not be read") from None
        return cls.from_mapping(value)
    @property
    def configuration_fingerprint(self) -> str:
        canonical=json.dumps({"schema_version":LIVE_CONFIG_SCHEMA_VERSION,"model_name":self.model_name,"command":list(self.command),"request_timeout_seconds":self.request_timeout_seconds,"tool_version":self.tool_version,"directive_normalization_policy":DIRECTIVE_NORMALIZATION_POLICY},ensure_ascii=False,sort_keys=True,separators=(",",":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    def to_metadata(self, limits: "LiveRunLimits") -> dict[str,Any]:
        return {"schema_version":LIVE_CONFIG_SCHEMA_VERSION,"protocol_version":LIVE_PROTOCOL_VERSION,"model_name":self.model_name,"tool_version":self.tool_version,"configuration_fingerprint":self.configuration_fingerprint,"request_timeout_seconds":self.request_timeout_seconds,"continue_on_task_failure":limits.continue_on_task_failure,"limits":limits.to_mapping()}

@dataclass(frozen=True)
class LiveExecutionAuthorization:
    _confirmed:bool=field(repr=False,compare=False)
    @classmethod
    def authorize(cls,confirmed,live_selected=True):
        if live_selected is not True or confirmed is not True: raise LiveOptInError("live execution requires explicit selection and confirmation")
        return cls(True)
    def __post_init__(self):
        if self._confirmed is not True: raise LiveOptInError("invalid live execution authorization")

@dataclass(frozen=True)
class LiveRunLimits:
    # ``max_retries`` governs provider/transport retries only.  A malformed
    # or illegal model directive is NOT a network retry: bounded directive
    # repair attempts (same controller snapshot, typed directive_feedback,
    # no controller advance, no tool dispatch, real model requests inside
    # the overall request accounting) are governed separately by
    # ``max_directive_repairs``.  The frozen scientific default is 0
    # (one malformed directive terminates the case); interactive runs opt
    # in explicitly.
    #
    # Task 44 (Unbounded Session Progress v1): ``max_model_requests`` and
    # ``max_controller_steps`` are ``None`` for unbounded
    # interactive/configured execution — counters remain observational
    # telemetry with no execution authority.  An explicit finite value is
    # honored ONLY for explicit callers (frozen scientific treatments,
    # operator CLI, deterministic harnesses).  Generic application
    # sources always pass ``None``.
    max_model_requests:int|None=None; max_controller_steps:int|None=None; max_model_phase_seconds:int=900; max_retries:int=2; continue_on_task_failure:bool=True; max_response_bytes:int=MAX_MODEL_RESPONSE_BYTES; max_elapsed_seconds:int|None=None; treatment_budget:"LiveTreatmentBudget|None"=None; max_directive_repairs:int=0
    def __post_init__(self):
        if self.max_elapsed_seconds is not None:
            if type(self.max_elapsed_seconds) is not int:
                raise LiveConfigurationError("max_elapsed_seconds is invalid")
            object.__setattr__(self,"max_model_phase_seconds",self.max_elapsed_seconds)
        # Task 44: total-session count ceilings are optional (None =
        # unbounded telemetry-only).  Finite values keep their historical
        # bounds when explicitly requested (frozen/operator/harness).
        for name,value,low,high,nullable in (("max_model_requests",self.max_model_requests,1,512,True),("max_controller_steps",self.max_controller_steps,1,256,True),("max_model_phase_seconds",self.max_model_phase_seconds,1,3600,False),("max_retries",self.max_retries,0,8,False),("max_response_bytes",self.max_response_bytes,1024,4*1024*1024,False),("max_directive_repairs",self.max_directive_repairs,0,8,False)):
            if nullable and value is None:
                continue
            if type(value) is not int or isinstance(value,bool) or not low<=value<=high: raise LiveConfigurationError(name+" is invalid")
        if type(self.continue_on_task_failure) is not bool: raise LiveConfigurationError("continue_on_task_failure is invalid")
        if self.treatment_budget is not None:
            if not isinstance(self.treatment_budget, LiveTreatmentBudget): raise LiveConfigurationError("treatment_budget is invalid")
            # A treatment-budgeted (frozen scientific) run keeps its finite
            # envelope: the three global ceilings must agree exactly.
            if self.max_model_requests != self.treatment_budget.max_model_requests or self.max_controller_steps != self.treatment_budget.max_controller_steps or self.max_retries != self.treatment_budget.max_retries:
                raise LiveConfigurationError("treatment budget must be the authoritative global envelope")
        if type(self.continue_on_task_failure) is not bool: raise LiveConfigurationError("continue_on_task_failure is invalid")
        if self.treatment_budget is not None:
            if not isinstance(self.treatment_budget, LiveTreatmentBudget): raise LiveConfigurationError("treatment_budget is invalid")
            if self.max_model_requests != self.treatment_budget.max_model_requests or self.max_controller_steps != self.treatment_budget.max_controller_steps or self.max_retries != self.treatment_budget.max_retries:
                raise LiveConfigurationError("treatment budget must be the authoritative global envelope")
            # The frozen treatment identity carries zero directive repairs;
            # a treatment-budgeted run must never silently repair directives.
            if self.max_directive_repairs != 0:
                raise LiveConfigurationError("treatment budget admits zero directive repairs")
    def to_mapping(self) -> dict[str,Any]:
        result={"max_model_requests":self.max_model_requests,"max_controller_steps":self.max_controller_steps,"max_model_phase_seconds":self.max_model_phase_seconds,"max_retries":self.max_retries,"max_directive_repairs":self.max_directive_repairs,"max_response_bytes":self.max_response_bytes,"continue_on_task_failure":self.continue_on_task_failure}
        if self.treatment_budget is not None: result["treatment_budget"]=self.treatment_budget.to_mapping()
        return result

@dataclass(frozen=True)
class LiveTreatmentBudget:
    """Internal, versioned Level-32 treatment envelope.

    This is provenance/configuration only.  It is intentionally never added
    to the model request contract.

    Task 44: the 40/40 total-session values are historical provenance for
    interpreting frozen campaign evidence and for the explicit official
    frozen operator route (``treatment_budget`` present in
    ``LiveRunLimits``).  Generic interactive/configured execution never
    sets ``treatment_budget`` and passes ``None`` (Live/Controller) /
    ``0`` (provider adapter) — unbounded with counters as telemetry
    only.  The envelope therefore cannot leak into generic execution.
    """
    logical_decision_ceiling:int=40
    max_controller_steps:int=40
    max_model_requests:int=40
    max_patch_attempts:int=40
    max_test_runs:int=40
    max_pdb_observations:int=40
    max_source_observations:int=40
    max_retries:int=0
    schema_version:str="treatment-budget-v1"
    def __post_init__(self):
        if self.schema_version != "treatment-budget-v1": raise LiveConfigurationError("unsupported treatment budget schema")
        values=(self.logical_decision_ceiling,self.max_controller_steps,self.max_model_requests,self.max_patch_attempts,self.max_test_runs,self.max_pdb_observations,self.max_source_observations)
        if any(type(value) is not int or value < 1 for value in values) or type(self.max_retries) is not int or self.max_retries < 0:
            raise LiveConfigurationError("treatment budget values are invalid")
        if not (self.logical_decision_ceiling == self.max_controller_steps == self.max_model_requests):
            raise LiveConfigurationError("global treatment ceiling fields must agree")
        if any(value < self.logical_decision_ceiling for value in (self.max_controller_steps,self.max_model_requests,self.max_patch_attempts,self.max_test_runs,self.max_pdb_observations,self.max_source_observations)):
            raise LiveConfigurationError("derived action caps cannot bind before the global decision ceiling")
    def to_mapping(self) -> dict[str,Any]:
        return {"schema_version":self.schema_version,"logical_decision_ceiling":self.logical_decision_ceiling,"max_controller_steps":self.max_controller_steps,"max_model_requests":self.max_model_requests,"derived_action_caps":{"max_patch_attempts":self.max_patch_attempts,"max_test_runs":self.max_test_runs,"max_pdb_observations":self.max_pdb_observations,"max_source_observations":self.max_source_observations},"max_retries":self.max_retries}
    def controller_limits(self) -> ControllerBudgetLimits:
        return ControllerBudgetLimits(max_patch_attempts=self.max_patch_attempts,max_test_runs=self.max_test_runs,max_pdb_observations=self.max_pdb_observations,max_active_hypotheses=3,max_source_observations=self.max_source_observations)

_SAFE_RUN_LABEL=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
def _new_evaluation_identity(run_label: str|None) -> tuple[str,str|None]:
    if run_label is not None and (type(run_label) is not str or not _SAFE_RUN_LABEL.fullmatch(run_label) or _SECRET_VALUE.search(run_label)):
        raise LiveConfigurationError("run label is invalid")
    label=run_label or "eval"
    return f"{label}-{uuid.uuid4().hex}",run_label
