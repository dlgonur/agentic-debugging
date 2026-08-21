"""Opt-in live-model evaluation over the existing controller and verifier."""
from __future__ import annotations
import hashlib, io, json, os, re, shutil, subprocess, tempfile, threading, time, uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol
from agentic_debugger.agent.controller import ControllerRunConfig, ControllerRunResult, ControllerStopReason, DeterministicController
from agentic_debugger.agent.controller_policy import (
    ActionName,
    BudgetKind,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisConfidence,
    HypothesisLedger,
    HypothesisStatus,
    PdbGateContext,
    PdbPolicy,
    allowed_actions_for_state,
    budget_kind_for_action,
    decide_pdb_access,
)
from agentic_debugger.agent.model_adapter import ActionDirective, AddHypothesisDirective, ControllerSnapshot, ModelAdapterError, ModelDirective, ReviseHypothesisDirective, SetHypothesisStatusDirective, TransitionDirective
from agentic_debugger.agent.state_machine import ControllerState, TRANSITION_GRAPH
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.runner import CURATED_RELATIVE_ROOT, localization_record
from agentic_debugger.agent.tool_registry import MAX_TOOL_ARGUMENT_BYTES, ToolRegistry, _detach_json_dict
from agentic_debugger.demo.tools import DemoToolContext, build_registry, legal_reproduction_phases, prepare_pdb_probe
from agentic_debugger.demo.external_runtime import is_external_isolated_task
from agentic_debugger.evaluation.runner import bounded_error, load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.events.schema import RunEvent
from agentic_debugger.rag.context import PUBLIC_REQUEST_BYTE_BUDGET, RagContext
from agentic_debugger.evaluation.request_envelope import (
    MAX_HTTP_REQUEST_BODY_BYTES,
    MAX_PUBLIC_REQUEST_BYTES,
    MAX_STDIN_REQUEST_BYTES,
)
from agentic_debugger.runtime.workspace import TaskWorkspace

class LiveEvaluationError(RuntimeError): pass
class LiveOptInError(LiveEvaluationError): pass
class LiveConfigurationError(LiveEvaluationError): pass
class LiveTransportError(LiveEvaluationError):
    def __init__(self, message: str, *, kind: str = "transport_error", timed_out: bool = False):
        super().__init__(message)
        self.kind = kind
        self.timed_out = timed_out
        self.adapter_error = False
        self.adapter_error_message = None


COMMAND_ADAPTER_ERROR_SCHEMA_VERSION = "live-command-error-v1"
COMMAND_ADAPTER_EVENT_SCHEMA_VERSION = "live-command-event-v1"
PROVIDER_GENERATION_EVENT = "provider_generation_started"
COMMAND_ADAPTER_TELEMETRY_SCHEMA_VERSION = "live-command-telemetry-v1"
PROVIDER_GENERATION_TELEMETRY_EVENT = "provider_generation_telemetry"
COMMAND_ADAPTER_ERROR_KINDS = frozenset({
    "adapter_error", "configuration", "http_error", "invalid_completion",
    "invalid_directive", "invalid_request", "invalid_response",
    "logical_call_limit", "model_mismatch", "preflight_failed",
    "request_too_large", "response_too_large", "timeout", "tool_call_rejected",
})
MODEL_OUTPUT_ADAPTER_ERROR_KINDS = frozenset({
    "invalid_directive", "invalid_response", "invalid_completion", "tool_call_rejected",
})
PROVIDER_ADAPTER_ERROR_KINDS = frozenset({"http_error", "timeout"})
MAX_COMMAND_ADAPTER_ERROR_MESSAGE_CHARS = 256


def parse_command_adapter_error(stderr: str) -> tuple[str, str] | None:
    """Parse only the exact bounded command-adapter error envelope."""

    if type(stderr) is not str:
        return None
    text = stderr.strip()
    if not text or len(text.encode("utf-8", errors="replace")) > 1024:
        return None
    candidates: list[Mapping[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, Mapping):
            return None
        if set(value) == {"schema_version", "event"}:
            if (
                value.get("schema_version") == COMMAND_ADAPTER_EVENT_SCHEMA_VERSION
                and value.get("event") == PROVIDER_GENERATION_EVENT
            ):
                continue
            return None
        if (
            value.get("schema_version") == COMMAND_ADAPTER_TELEMETRY_SCHEMA_VERSION
            and value.get("event") == PROVIDER_GENERATION_TELEMETRY_EVENT
        ):
            continue
        if set(value) != {"schema_version", "kind", "message"}:
            return None
        candidates.append(value)
    if len(candidates) != 1:
        return None
    value = candidates[0]
    if value.get("schema_version") != COMMAND_ADAPTER_ERROR_SCHEMA_VERSION:
        return None
    kind = value.get("kind")
    message = value.get("message")
    if (
        type(kind) is not str
        or kind not in COMMAND_ADAPTER_ERROR_KINDS
        or type(message) is not str
        or not message
        or len(message.encode("utf-8", errors="replace")) > MAX_COMMAND_ADAPTER_ERROR_MESSAGE_CHARS
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in message)
    ):
        return None
    return kind, message


def parse_provider_generation_started(stderr: str) -> bool:
    """Recognize only the exact bounded generation-boundary event."""

    if type(stderr) is not str or len(stderr.encode("utf-8", errors="replace")) > 65536:
        return False
    for line in stderr.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(value, Mapping)
            and set(value) == {"schema_version", "event"}
            and value.get("schema_version") == COMMAND_ADAPTER_EVENT_SCHEMA_VERSION
            and value.get("event") == PROVIDER_GENERATION_EVENT
        ):
            return True
    return False


_GENERATION_TELEMETRY_FIELDS = frozenset({
    "schema_version",
    "event",
    "first_response_chunk_latency_seconds",
    "last_chunk_elapsed_seconds",
    "completion_elapsed_seconds",
    "timeout_phase",
    "progress_occurred",
    "progress_before_timeout",
    "wire_bytes_observed",
    "retained_content_bytes",
    "discarded_thinking_bytes",
    "discarded_thinking_chunks",
    "stream_chunks_observed",
})


def parse_provider_generation_telemetry(stderr: str) -> dict[str, Any] | None:
    """Parse one bounded, numeric-only generation progress record."""

    if type(stderr) is not str or len(stderr.encode("utf-8", errors="replace")) > 65536:
        return None
    found: dict[str, Any] | None = None
    for line in stderr.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, Mapping):
            continue
        if (
            value.get("schema_version") != COMMAND_ADAPTER_TELEMETRY_SCHEMA_VERSION
            or value.get("event") != PROVIDER_GENERATION_TELEMETRY_EVENT
            or set(value) != _GENERATION_TELEMETRY_FIELDS
        ):
            continue
        numeric_fields = (
            "first_response_chunk_latency_seconds",
            "last_chunk_elapsed_seconds",
            "completion_elapsed_seconds",
        )
        count_fields = (
            "wire_bytes_observed",
            "retained_content_bytes",
            "discarded_thinking_bytes",
            "discarded_thinking_chunks",
            "stream_chunks_observed",
        )
        if any(
            value[field] is not None
            and (type(value[field]) not in (int, float) or value[field] < 0)
            for field in numeric_fields
        ):
            continue
        if any(type(value[field]) is not int or value[field] < 0 for field in count_fields):
            continue
        if type(value["progress_occurred"]) is not bool or type(value["progress_before_timeout"]) is not bool:
            continue
        if value["timeout_phase"] not in {None, "metadata", "generation", "stream_idle"}:
            continue
        found = {
            key: value[key]
            for key in _GENERATION_TELEMETRY_FIELDS
            if key not in {"schema_version", "event"}
        }
    return found


def _command_adapter_transport_error(stderr: str) -> LiveTransportError | None:
    parsed = parse_command_adapter_error(stderr)
    if parsed is None:
        return None
    kind, message = parsed
    error = LiveTransportError(f"command adapter reported {kind}", kind=kind)
    error.adapter_error = True
    error.adapter_error_message = message
    return error

class ModelRequestBudgetExceeded(LiveEvaluationError):
    """The transport rejected the model request before any provider process.

    Raised only when the canonical public request serialization of the model
    request is a valid non-negative byte count above the frozen
    public-evidence budget: the next public request could not have been
    constructed within the frozen case limit, so the case stops before
    another wrapper/provider process is launched.  This is a typed,
    non-retryable case-level signal: it is never a transport/provider
    failure, never a malformed-directive rejection, and never launches a
    model/provider process for the rejected request.
    """

    def __init__(self, request_byte_count: int, limit: int) -> None:
        super().__init__(
            f"OpenCode canonical public request exceeds the public-evidence byte budget "
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
    def __init__(self, message: str, *, category: "DirectiveRejectionCategory" = DirectiveRejectionCategory.MALFORMED_DIRECTIVE, detail: str = ""):
        super().__init__(message)
        self.category = category
        text = str(detail)
        self.detail = text if len(text) <= MAX_REJECTION_DETAIL_CHARS else text[:MAX_REJECTION_DETAIL_CHARS - 3] + "..."

LIVE_SCHEMA_VERSION = "1.0"
LIVE_PROTOCOL_VERSION = "1.3"
MAX_LIVE_REQUEST_TIMEOUT_SECONDS = 1200.0
LIVE_CONFIG_SCHEMA_VERSION = "1.0"
MAX_MODEL_RESPONSE_BYTES = 1_048_576
MAX_COMMAND_ARGUMENTS = 32
LIVE_DIRECTIVE_SCHEMA={
    "action":{"kind":"action","required":["name","arguments"]},
    "transition":{"kind":"transition","required":["target_state","reason"]},
    "add_hypothesis":{"kind":"add_hypothesis","required":["hypothesis_id","statement","confidence","evidence_refs","requires_runtime_evidence"],"constraints":{"confidence":{"type":"string","enum":[item.value for item in HypothesisConfidence]}}},
    "revise_hypothesis":{"kind":"revise_hypothesis","required":["hypothesis_id","statement","confidence","evidence_refs","requires_runtime_evidence"],"constraints":{"confidence":{"type":"string","enum":[item.value for item in HypothesisConfidence]}}},
    "set_hypothesis_status":{"kind":"set_hypothesis_status","required":["hypothesis_id","status"],"constraints":{"status":{"type":"string","enum":[item.value for item in HypothesisStatus if item is not HypothesisStatus.ACTIVE]}}},
}
class LiveCaseStatus(str, Enum):
    RESOLVED="RESOLVED"; UNRESOLVED="UNRESOLVED"; PDB_NOT_REACHED="PDB_NOT_REACHED"; VALIDATION_NOT_REACHED="VALIDATION_NOT_REACHED"; CONTROLLER_FAILED="CONTROLLER_FAILED"; CONTROLLER_REJECTED="CONTROLLER_REJECTED"; TIMED_OUT="TIMED_OUT"; PROVIDER_ERROR="PROVIDER_ERROR"; VERIFIER_FAILED="VERIFIER_FAILED"; EVENT_REPORTING_FAILED="EVENT_REPORTING_FAILED"; CLEANUP_FAILED="CLEANUP_FAILED"; HARNESS_ERROR="HARNESS_ERROR"; INCOMPLETE="INCOMPLETE"

_SECRET_KEY=re.compile(r"(?:api[_-]?key|access[_-]?key|auth(?:orization)?|credential|password|secret|token|private[_-]?key)",re.I)
_SECRET_VALUE=re.compile(r"(?i)\b(?:bearer|basic)\s+\S+|\b(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|token)\s*[:=]\s*\S+")
_SECRET_ARGUMENT=re.compile(r"^--?(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|private[_-]?key|token)(?:=|$)",re.I)
_USAGE_FIELDS={"prompt_tokens","completion_tokens","total_tokens","provider_reported","missing_fields"}
def redact_for_recording(value:Any, *, _usage_context:bool=False, _event_metadata_context:bool=False)->Any:
    if isinstance(value,Mapping):
        result={}
        for key,item in value.items():
            name=str(key)
            if name == "token_usage" and isinstance(item,Mapping):
                result[name]=redact_for_recording(item,_usage_context=True,_event_metadata_context=False)
            elif name == "metadata" and isinstance(item,Mapping) and set(item).issubset({"duration_ms","tool_version","model","tokens","cost"}) and {"duration_ms","tool_version","model","tokens","cost"}.issubset(set(item)):
                result[name]=redact_for_recording(item,_usage_context=False,_event_metadata_context=True)
            elif _usage_context and name in _USAGE_FIELDS and ((name in {"prompt_tokens","completion_tokens","total_tokens"} and (type(item) is int or item is None)) or (name=="provider_reported" and type(item) is bool) or (name=="missing_fields" and isinstance(item,list))):
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
        if type(self.request_timeout_seconds) not in (int,float) or not 0<self.request_timeout_seconds<=MAX_LIVE_REQUEST_TIMEOUT_SECONDS: raise LiveConfigurationError("live request timeout is invalid")
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
        canonical=json.dumps({"schema_version":LIVE_CONFIG_SCHEMA_VERSION,"model_name":self.model_name,"command":list(self.command),"request_timeout_seconds":self.request_timeout_seconds,"tool_version":self.tool_version},ensure_ascii=False,sort_keys=True,separators=(",",":"))
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
    max_model_requests:int=64; max_controller_steps:int=64; max_model_phase_seconds:int=900; max_retries:int=2; continue_on_task_failure:bool=True; max_response_bytes:int=MAX_MODEL_RESPONSE_BYTES; max_elapsed_seconds:int|None=None; retry_provider_timeouts:bool=True
    def __post_init__(self):
        if self.max_elapsed_seconds is not None:
            if type(self.max_elapsed_seconds) is not int:
                raise LiveConfigurationError("max_elapsed_seconds is invalid")
            object.__setattr__(self,"max_model_phase_seconds",self.max_elapsed_seconds)
        for name,value,low,high in (("max_model_requests",self.max_model_requests,1,512),("max_controller_steps",self.max_controller_steps,1,256),("max_model_phase_seconds",self.max_model_phase_seconds,1,3600),("max_retries",self.max_retries,0,8),("max_response_bytes",self.max_response_bytes,1024,4*1024*1024)):
            if type(value) is not int or not low<=value<=high: raise LiveConfigurationError(name+" is invalid")
        if type(self.continue_on_task_failure) is not bool: raise LiveConfigurationError("continue_on_task_failure is invalid")
        if type(self.retry_provider_timeouts) is not bool: raise LiveConfigurationError("retry_provider_timeouts is invalid")
    def to_mapping(self) -> dict[str,Any]:
        return {"max_model_requests":self.max_model_requests,"max_controller_steps":self.max_controller_steps,"max_model_phase_seconds":self.max_model_phase_seconds,"max_retries":self.max_retries,"max_response_bytes":self.max_response_bytes,"continue_on_task_failure":self.continue_on_task_failure,"retry_provider_timeouts":self.retry_provider_timeouts}

_SAFE_RUN_LABEL=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
def _new_evaluation_identity(run_label: str|None) -> tuple[str,str|None]:
    if run_label is not None and (type(run_label) is not str or not _SAFE_RUN_LABEL.fullmatch(run_label) or _SECRET_VALUE.search(run_label)):
        raise LiveConfigurationError("run label is invalid")
    label=run_label or "eval"
    return f"{label}-{uuid.uuid4().hex}",run_label

class ModelTransport(Protocol):
    def request(self,payload:Mapping[str,Any],timeout_seconds:float)->Mapping[str,Any]: ...

class _BoundedCapture:
    def __init__(self, maximum_bytes:int):
        self.maximum_bytes=maximum_bytes; self.data=bytearray(); self.truncated=False; self.lock=threading.Lock()
    def add(self, chunk:bytes):
        with self.lock:
            remaining=self.maximum_bytes-len(self.data)
            if remaining>0: self.data.extend(chunk[:remaining])
            if len(chunk)>remaining: self.truncated=True
    def text(self)->str:
        with self.lock: return bytes(self.data).decode("utf-8",errors="replace")

def _read_pipe(pipe:Any,capture:_BoundedCapture):
    try:
        while True:
            chunk=pipe.read(8192)
            if not chunk: return
            capture.add(chunk)
    except Exception:
        return

def _terminate_process(process:subprocess.Popen):
    try:
        process.terminate(); process.wait(timeout=1)
    except Exception:
        try: process.kill(); process.wait(timeout=2)
        except Exception: pass

class JsonlCommandTransport:
    def __init__(self,config,*,max_output_bytes=MAX_MODEL_RESPONSE_BYTES):
        if type(max_output_bytes) is not int or not 1024<=max_output_bytes<=4*1024*1024: raise LiveConfigurationError("max response bytes is invalid")
        self.config=config; self.max_output_bytes=max_output_bytes
        self.last_provider_generation_started = False
    @staticmethod
    def subprocess_environment():
        environment={"PATH":os.environ.get("PATH",""),"PYTHONIOENCODING":"utf-8"}
        if os.name=="nt" and os.environ.get("SystemRoot"): environment["SystemRoot"]=os.environ["SystemRoot"]
        return environment
    def request(self,payload,timeout_seconds):
        self.last_provider_generation_started = False
        try: request_bytes=(json.dumps(payload,ensure_ascii=False,allow_nan=False)+"\n").encode("utf-8")
        except (TypeError,ValueError,UnicodeError): raise LiveTransportError("model request could not be serialized",kind="request_serialization") from None
        environment=self.subprocess_environment()
        try:
            process=subprocess.Popen(list(self.config.command),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=False,env=environment,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=="nt" else 0)
        except (OSError,ValueError): raise LiveTransportError("model command could not be launched",kind="launch_error") from None
        stdout=_BoundedCapture(self.max_output_bytes); stderr=_BoundedCapture(self.max_output_bytes)
        threads=[threading.Thread(target=_read_pipe,args=(process.stdout,stdout),daemon=True),threading.Thread(target=_read_pipe,args=(process.stderr,stderr),daemon=True)]
        for thread in threads: thread.start()
        deadline=time.monotonic()+timeout_seconds
        write_error=[]
        def write_request():
            try:
                assert process.stdin is not None
                process.stdin.write(request_bytes)
                process.stdin.close()
            except (BrokenPipeError,OSError) as exc:
                write_error.append(exc)
        writer=threading.Thread(target=write_request,daemon=True)
        writer.start()
        writer.join(timeout=max(0.0,deadline-time.monotonic()))
        if writer.is_alive():
            _terminate_process(process)
            for thread in threads: thread.join(timeout=2)
            raise LiveTransportError("model request stdin write timed out",kind="request_timeout",timed_out=True) from None
        try: process.wait(timeout=max(0.0,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            _terminate_process(process)
            for thread in threads: thread.join(timeout=2)
            raise LiveTransportError("model request timed out",kind="request_timeout",timed_out=True) from None
        for thread in threads: thread.join(timeout=2)
        self.last_provider_generation_started = parse_provider_generation_started(stderr.text())
        if stdout.truncated: raise LiveTransportError("model response exceeded the configured output bound",kind="response_too_large")
        if process.returncode!=0:
            typed = _command_adapter_transport_error(stderr.text())
            if typed is not None:
                raise typed
            raise LiveTransportError("model command failed",kind="process_error")
        try: value=json.loads(stdout.text())
        except (UnicodeError,json.JSONDecodeError): raise LiveTransportError("model response was invalid JSON",kind="invalid_response") from None
        if not isinstance(value,Mapping): raise LiveTransportError("model response was not an object",kind="invalid_response")
        # A wrapper may emit its completed JSON response and close stdin before
        # the bounded request writer finishes.  A successful JSON response is
        # the provider completion contract; the harmless broken-pipe signal is
        # not a transport failure in that case.
        return value

@dataclass
class LiveModelMetrics:
    model_requests:int=0; model_responses:int=0; retries:int=0; provider_errors:int=0; provider_error_kinds:list[str]=field(default_factory=list); adapter_error_kinds:list[str]=field(default_factory=list); invalid_model_responses:int=0; setup_error_kinds:list[str]=field(default_factory=list); provider_generation_calls:int=0; prompt_tokens:int|None=None; completion_tokens:int|None=None; total_tokens:int|None=None; usage_reported:bool=False; usage_missing_fields:list[str]=field(default_factory=list); termination_reason:str|None=None; max_canonical_public_request_bytes:int=0; rejected_canonical_public_request_bytes:int|None=None; rejected_stdin_request_bytes:int|None=None
    def error(self,kind):
        self.provider_errors+=1
        if kind not in self.provider_error_kinds: self.provider_error_kinds.append(kind)
    def adapter_error(self, kind: str) -> None:
        if kind not in self.adapter_error_kinds: self.adapter_error_kinds.append(kind)
    def invalid_model_response(self, kind: str | None = None) -> None:
        self.invalid_model_responses += 1
        if kind is not None: self.adapter_error(kind)
    def setup_error(self, kind: str) -> None:
        if kind not in self.setup_error_kinds: self.setup_error_kinds.append(kind)
    generation_telemetry:list[dict[str,Any]]=field(default_factory=list)
    def observe_provider_generation(self, transport: Any) -> None:
        if getattr(transport, "last_provider_generation_started", False) is True:
            self.provider_generation_calls += 1
        telemetry = getattr(transport, "last_provider_generation_telemetry", None)
        if isinstance(telemetry, Mapping):
            self.generation_telemetry.append(dict(telemetry))
            del self.generation_telemetry[:-64]
    def usage(self,value):
        names=("prompt_tokens","completion_tokens","total_tokens")
        if not isinstance(value,Mapping):
            self.usage_missing_fields.extend(x for x in names if x not in self.usage_missing_fields); return
        self.usage_reported=True
        for name in names:
            number=value.get(name)
            if type(number) is int and number >= 0:
                old=getattr(self,name); setattr(self,name,number if old is None else old+number)
            elif name not in self.usage_missing_fields: self.usage_missing_fields.append(name)
    def observe_request_size(self, canonical_bytes: int, *, stdin_bytes: int | None = None) -> None:
        self.max_canonical_public_request_bytes = max(
            self.max_canonical_public_request_bytes, canonical_bytes
        )
        if canonical_bytes > MAX_PUBLIC_REQUEST_BYTES:
            self.rejected_canonical_public_request_bytes = canonical_bytes
        if stdin_bytes is not None and stdin_bytes > MAX_STDIN_REQUEST_BYTES:
            self.rejected_stdin_request_bytes = stdin_bytes
    def to_mapping(self): return {"model_request_count":self.model_requests,"model_response_count":self.model_responses,"retry_count":self.retries,"provider_error_count":self.provider_errors,"provider_error_kinds":self.provider_error_kinds,"adapter_error_kinds":self.adapter_error_kinds,"invalid_model_response_count":self.invalid_model_responses,"setup_error_kinds":self.setup_error_kinds,"provider_generation_calls":self.provider_generation_calls,"generation_telemetry":self.generation_telemetry,"token_usage":{"prompt_tokens":self.prompt_tokens,"completion_tokens":self.completion_tokens,"total_tokens":self.total_tokens,"provider_reported":self.usage_reported,"missing_fields":sorted(set(self.usage_missing_fields))},"termination_reason":self.termination_reason,"request_size":{"max_canonical_public_request_bytes":self.max_canonical_public_request_bytes,"canonical_public_request_bytes_limit":MAX_PUBLIC_REQUEST_BYTES,"rejected_canonical_public_request_bytes":self.rejected_canonical_public_request_bytes,"stdin_request_bytes_limit":MAX_STDIN_REQUEST_BYTES,"rejected_stdin_request_bytes":self.rejected_stdin_request_bytes,"http_request_body_bytes_limit":MAX_HTTP_REQUEST_BODY_BYTES}}

def _rejected(category: "DirectiveRejectionCategory", detail: str = "") -> LiveModelAdapterError:
    return LiveModelAdapterError("invalid model directive", category=category, detail=detail)

def _require_field(value: Mapping[str, Any], key: str) -> Any:
    try:
        return value[key]
    except (KeyError, TypeError):
        raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, f"missing '{key}'") from None

_PDB_ACTIONS = frozenset({
    ActionName.START_PDB_SESSION,
    ActionName.GET_STACK_SUMMARY,
    ActionName.GET_FRAME,
    ActionName.GET_FRAME_LOCALS,
    ActionName.SAFE_EVAL_EXPRESSION,
    ActionName.INSPECT_CALLER_FRAME,
    ActionName.CONTINUE_PDB_SESSION,
    ActionName.STEP_PDB_SESSION,
    ActionName.NEXT_PDB_SESSION,
    ActionName.STOP_PDB_SESSION,
})
_SESSION_ACTIONS = frozenset({
    ActionName.GET_STACK_SUMMARY,
    ActionName.GET_FRAME,
    ActionName.GET_FRAME_LOCALS,
    ActionName.SAFE_EVAL_EXPRESSION,
    ActionName.INSPECT_CALLER_FRAME,
    ActionName.CONTINUE_PDB_SESSION,
    ActionName.STEP_PDB_SESSION,
    ActionName.NEXT_PDB_SESSION,
    ActionName.STOP_PDB_SESSION,
})


def _directive_schema_for_state(state: ControllerState) -> dict[str, dict[str, Any]]:
    """Expose only directive kinds the deterministic controller can apply."""

    kinds = {"action", "transition"}
    if state is ControllerState.UNDERSTAND:
        kinds.update({"add_hypothesis", "revise_hypothesis", "set_hypothesis_status"})
    elif state is ControllerState.RUNTIME_EVIDENCE:
        kinds.update({"revise_hypothesis", "set_hypothesis_status"})
    order = (
        "action",
        "transition",
        "add_hypothesis",
        "revise_hypothesis",
        "set_hypothesis_status",
    )
    return {
        kind: _detach_json_dict(
            LIVE_DIRECTIVE_SCHEMA[kind],
            max_bytes=MAX_TOOL_ARGUMENT_BYTES,
        )
        for kind in order
        if kind in kinds
    }


def _action_contracts_for_state(
    state: ControllerState,
    *,
    registry: ToolRegistry,
    policy: DemoPolicy | None = None,
    session_active: bool = False,
    pdb_available: bool = True,
    pdb_observations_remaining: int | None = None,
    post_patch_f2p_collected: bool = False,
    regression_collected: bool = False,
    candidate_applied: bool = False,
    external_discovery: bool | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the effective contract derived from the supplied registry."""

    if type(registry) is not ToolRegistry:
        raise LiveConfigurationError("live tool registry is required")
    contracts = registry.argument_contracts()
    registered = set(registry.names())
    effective = set(allowed_actions_for_state(state)) & registered
    if state is ControllerState.REPRODUCE and external_discovery is False:
        effective -= {
            ActionName.SEARCH_CODE,
            ActionName.FIND_FUNCTION,
            ActionName.FIND_CLASS,
            ActionName.GET_SOURCE_WINDOW,
        }
    if policy is DemoPolicy.STATIC_BASELINE or not pdb_available:
        effective -= _PDB_ACTIONS
    if state is ControllerState.RUNTIME_EVIDENCE:
        if session_active:
            effective.discard(ActionName.START_PDB_SESSION)
        else:
            effective -= _SESSION_ACTIONS
    if pdb_observations_remaining is not None and pdb_observations_remaining <= 0:
        effective = {
            action
            for action in effective
            if budget_kind_for_action(action) is not BudgetKind.PDB_OBSERVATIONS
        }
        if not session_active:
            effective.discard(ActionName.START_PDB_SESSION)
    if state is ControllerState.VALIDATE and not (
        post_patch_f2p_collected and regression_collected
    ):
        # classify_outcome is legal in Validate but meaningless until both
        # required evidence values exist.  Hide it rather than leaving the
        # model to rediscover a deterministic tool error.
        effective.discard(ActionName.CLASSIFY_OUTCOME)
    if state is ControllerState.PATCH and not candidate_applied:
        effective.discard(ActionName.SYNTAX_CHECK)
        effective.discard(ActionName.REVERT_PATCH)
    if state is ControllerState.VALIDATE and not candidate_applied:
        effective -= {
            ActionName.RUN_REPRODUCTION,
            ActionName.RUN_REGRESSION_TESTS,
            ActionName.CLASSIFY_OUTCOME,
            ActionName.REVERT_PATCH,
        }
    result: dict[str, dict[str, Any]] = {}
    for action in ActionName:
        if action not in effective or action.value not in contracts:
            continue
        result[action.value] = dict(contracts[action.value])
    if ActionName.RUN_REPRODUCTION.value in result:
        properties = result[ActionName.RUN_REPRODUCTION.value].get("properties")
        if not isinstance(properties, Mapping) or "phase" not in properties:
            raise LiveConfigurationError("registry run_reproduction contract is not validator-derived")
        phase = dict(properties["phase"])
        phase["enum"] = list(legal_reproduction_phases(state))
        properties = dict(properties)
        properties["phase"] = phase
        result[ActionName.RUN_REPRODUCTION.value]["properties"] = properties
    return _detach_json_dict(result, max_bytes=MAX_TOOL_ARGUMENT_BYTES)
def _validate_enum_constrained_arguments(
    name: ActionName,
    arguments: Mapping[str, Any],
    contracts: Mapping[str, Mapping[str, Any]],
) -> None:
    """Reject an argument value outside its advertised, state-specific enum.

    Only arguments whose contract already declares an ``enum`` (currently
    ``run_reproduction.phase``) are checked here; this mirrors exactly what
    ``_action_contracts_for_state`` already advertises to the model, so the
    contract stays the single source of truth.
    """
    contract = contracts.get(name.value, {})
    properties = contract.get("properties", contract)
    for argument_name, spec in properties.items():
        if isinstance(spec, Mapping) and "enum" in spec and arguments.get(argument_name) not in spec["enum"]:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, f"'{argument_name}' must be one of {list(spec['enum'])}")

def _legal_transition_targets(
    state: ControllerState,
    *,
    pdb_transition_allowed: bool = True,
    candidate_applied: bool = False,
    external_source_context_observed: bool | None = None,
) -> list[str]:
    return [
        candidate.value
        for candidate in ControllerState
        if candidate in TRANSITION_GRAPH[state]
        and (candidate is not ControllerState.RUNTIME_EVIDENCE or pdb_transition_allowed)
        and not (
            state is ControllerState.PATCH
            and candidate is ControllerState.VALIDATE
            and not candidate_applied
        )
        and not (
            state is ControllerState.VALIDATE
            and candidate is ControllerState.DONE
            and not candidate_applied
        )
        and not (
            candidate is ControllerState.PATCH
            and external_source_context_observed is False
        )
    ]

def _resolve_raw_directive(response: Mapping[str, Any]) -> Any:
    """Unwrap the provider envelope without silently guessing when it is ambiguous.

    The wire convention is either a bare directive object or a
    ``{"usage": ..., "directive": {...}}`` wrapper.  A response carrying both a
    top-level ``kind`` and a nested ``directive`` mixes both conventions at
    once; guessing which one is meant would risk silently substituting a
    directive the model never actually returned, so it is rejected instead.
    """
    wrapped = "directive" in response
    inline = "kind" in response
    if wrapped and inline:
        raise _rejected(DirectiveRejectionCategory.AMBIGUOUS_ENVELOPE, "response has both a top-level 'kind' and a nested 'directive'")
    return response["directive"] if wrapped else response

def _parse(
    value: Any,
    snapshot: ControllerSnapshot,
    *,
    action_contracts: Mapping[str, Mapping[str, Any]] | None = None,
    legal_transition_targets: set[str] | None = None,
    directive_kinds: set[str] | None = None,
) -> ModelDirective:
    if not isinstance(value, Mapping):
        raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "directive must be a JSON object")
    kind = value.get("kind")
    # ``kind`` is provider-controlled JSON.  Check its shape before using it
    # in set membership so arrays and objects cannot escape the bounded
    # provider-completed invalid-directive layer as unhashable values.
    if type(kind) is not str:
        raise _rejected(
            DirectiveRejectionCategory.MALFORMED_DIRECTIVE,
            "unrecognized or missing directive 'kind'",
        )
    known_kinds = set(LIVE_DIRECTIVE_SCHEMA)
    if kind not in known_kinds:
        raise _rejected(
            DirectiveRejectionCategory.MALFORMED_DIRECTIVE,
            "unrecognized or missing directive 'kind'",
        )
    if directive_kinds is not None and kind not in directive_kinds:
        raise _rejected(
            DirectiveRejectionCategory.ILLEGAL_ACTION,
            f"directive kind '{kind}' is not legal in state '{snapshot.state.value}'",
        )
    if kind == "action":
        arguments = _require_field(value, "arguments")
        if not isinstance(arguments, Mapping):
            raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "'arguments' must be a JSON object")
        try:
            name = ActionName(_require_field(value, "name"))
        except ValueError:
            raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "unrecognized action name") from None
        effective_actions = {
            ActionName(name) for name in action_contracts
        } if action_contracts is not None else set(snapshot.allowed_actions)
        if name not in effective_actions:
            raise _rejected(DirectiveRejectionCategory.ILLEGAL_ACTION, f"action '{name.value}' is not allowed in state '{snapshot.state.value}'")
        if action_contracts is not None:
            _validate_enum_constrained_arguments(name, arguments, action_contracts)
        try:
            return ActionDirective(name, dict(arguments))
        except ModelAdapterError:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, "action arguments failed validation") from None
    if kind == "transition":
        try:
            target = ControllerState(_require_field(value, "target_state"))
        except ValueError:
            raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "unrecognized target_state") from None
        reason = _require_field(value, "reason")
        if legal_transition_targets is not None and target.value not in legal_transition_targets:
            raise _rejected(DirectiveRejectionCategory.ILLEGAL_TRANSITION, f"'{target.value}' is not reachable from '{snapshot.state.value}'")
        if legal_transition_targets is None and target not in TRANSITION_GRAPH[snapshot.state]:
            raise _rejected(DirectiveRejectionCategory.ILLEGAL_TRANSITION, f"'{target.value}' is not reachable from '{snapshot.state.value}'")
        try:
            return TransitionDirective(target, reason)
        except ModelAdapterError:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, "'reason' failed validation") from None
    if kind in ("add_hypothesis", "revise_hypothesis"):
        hypothesis_id = _require_field(value, "hypothesis_id")
        statement = _require_field(value, "statement")
        try:
            confidence = HypothesisConfidence(_require_field(value, "confidence"))
        except ValueError:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, "'confidence' must be low, medium, or high") from None
        evidence_refs_raw = _require_field(value, "evidence_refs")
        requires_runtime_evidence = _require_field(value, "requires_runtime_evidence")
        if type(evidence_refs_raw) is not list:
            raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "'evidence_refs' must be a JSON array") from None
        evidence_refs = tuple(evidence_refs_raw)
        directive_cls = AddHypothesisDirective if kind == "add_hypothesis" else ReviseHypothesisDirective
        try:
            return directive_cls(hypothesis_id, statement, confidence, evidence_refs, requires_runtime_evidence)
        except ModelAdapterError:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, "hypothesis fields failed validation") from None
    if kind == "set_hypothesis_status":
        hypothesis_id = _require_field(value, "hypothesis_id")
        try:
            status = HypothesisStatus(_require_field(value, "status"))
        except ValueError:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, "unrecognized hypothesis status") from None
        try:
            return SetHypothesisStatusDirective(hypothesis_id, status)
        except ModelAdapterError:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, "hypothesis status fields failed validation") from None
    raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "unrecognized or missing directive 'kind'")

class LiveModelAdapter:
    def __init__(self,*,task,policy,config,transport,limits,registry=None,evaluation_id="evaluation",case_id="case",run_id="run",trajectory_id="trajectory",clock=time.monotonic,rag_context=None):
        if type(registry) is not ToolRegistry:
            raise LiveConfigurationError("live tool registry is required")
        self.model_name=config.model_name; self.task=task; self.policy=policy; self.config=config; self.transport=transport; self.limits=limits; self.registry=registry; self.evaluation_id=evaluation_id; self.case_id=case_id; self.run_id=run_id; self.trajectory_id=trajectory_id; self.metrics=LiveModelMetrics(); self.clock=clock; self.model_phase_elapsed_seconds=0.0; self.history=[]; self.pdb_gate_decisions=[]; self.directive_rejections=[]; self.directive_attempts=[]
        # Optional RAG context: when None (the default) the public request is
        # byte-for-byte unchanged and ``retrieved_context`` is never emitted.
        # When supplied it must be a validated RagContext; arbitrary
        # lookalike objects are rejected at this boundary (repair 1).
        if rag_context is not None and not isinstance(rag_context, RagContext):
            raise LiveConfigurationError("rag_context must be a validated RagContext")
        self._rag_context=rag_context
        self._failure_reproduced = False
        self._pdb_session_active = False
        self._runtime_transition_authorized = False
        self._post_patch_f2p_collected = False
        self._regression_collected = False
        self._candidate_applied = False
        self._source_context_observed = False
        # Per-logical-call PDB gate cache: ``(model_call_index, decision)``.
        # ``model_call_index`` is constant across the transport retry loop and
        # increments only on the next controller step, so it is the natural
        # per-:func:`next_directive` scope.  The cache is reused by every reread
        # (``_runtime_transition_allowed``, ``_effective_contract``,
        # ``_request_context`` and ``legal_transition_targets``) so the gate is
        # evaluated at most once per logical call and every consumer sees the
        # identical decision.  ``_pdb_gate_recorded_for_index`` bounds recording
        # to at most one append per logical call regardless of retry count.
        self._pdb_gate_decision_cache: tuple[int, Any] | None = None
        self._pdb_gate_recorded_for_index: int | None = None
    @staticmethod
    def _hypotheses(snapshot):
        return [{"hypothesis_id":item.hypothesis_id,"statement":item.statement,"confidence":item.confidence.value,"status":item.status.value,"evidence_refs":list(item.evidence_refs),"requires_runtime_evidence":item.requires_runtime_evidence,"revision":item.revision} for item in snapshot.hypotheses.hypotheses]
    def _observe_snapshot(self, snapshot: ControllerSnapshot) -> None:
        observation = snapshot.last_observation
        if observation is None:
            return
        if observation.name in {
            ActionName.APPLY_PATCH.value,
            ActionName.REVERT_PATCH.value,
        }:
            # A new or reverted candidate invalidates prior Validate evidence.
            self._post_patch_f2p_collected = False
            self._regression_collected = False
            if observation.name == ActionName.APPLY_PATCH.value:
                self._candidate_applied = (
                    observation.status.value == "ok"
                    and observation.payload.get("applied") is True
                )
            elif (
                observation.status.value == "ok"
                and observation.payload.get("reverted") is True
            ):
                self._candidate_applied = False
            return
        if observation.status.value != "ok":
            return
        payload = observation.payload
        if observation.name == ActionName.GET_SOURCE_WINDOW.value:
            self._source_context_observed = True
            return
        if observation.name == ActionName.RUN_REPRODUCTION.value:
            phase = payload.get("phase")
            if phase == "baseline":
                if type(payload.get("failure_reproduced")) is bool:
                    self._failure_reproduced = payload["failure_reproduced"]
            elif phase == "post_patch":
                self._post_patch_f2p_collected = True
        elif observation.name == ActionName.RUN_REGRESSION_TESTS.value:
            self._regression_collected = True
        elif observation.name == ActionName.START_PDB_SESSION.value:
            self._pdb_session_active = payload.get("state") == "paused"
        elif observation.name == ActionName.STOP_PDB_SESSION.value:
            self._pdb_session_active = payload.get("stopped") is True
            if self._pdb_session_active:
                self._pdb_session_active = False

    def _evaluate_pdb_gate(self, snapshot: ControllerSnapshot) -> object:
        """Pure PDB gate evaluation; never appends to ``pdb_gate_decisions``.

        Mirrors :func:`decide_pdb_access` consumed by the contained
        reachability driver (``contained_pdb.py``): the decision is a function
        of policy, source state, failure reproduction, remaining observations,
        failed patch attempts and the active hypothesis only.
        """
        active = snapshot.hypotheses.active_hypotheses()
        source_state = snapshot.state
        if source_state is ControllerState.RUNTIME_EVIDENCE:
            # RuntimeEvidence is reached only after an authorized transition
            # from Understand. This keeps the accepted gate's source-state
            # semantics while preserving the lifecycle state in the request.
            source_state = ControllerState.UNDERSTAND
        decision = decide_pdb_access(
            PdbPolicy.DISABLED
            if self.policy is DemoPolicy.STATIC_BASELINE
            else PdbPolicy.ON_UNCERTAINTY,
            PdbGateContext(
                source_state=source_state,
                failure_reproduced=self._failure_reproduced,
                remaining_pdb_observations=max(
                    0,
                    snapshot.budget_limits.max_pdb_observations
                    - snapshot.budget_state.pdb_observations,
                ),
                failed_patch_attempts=snapshot.budget_state.patch_attempts,
                active_hypothesis=active[0] if active else None,
            ),
        )
        return decision

    def _record_pdb_gate_decision(self, snapshot: ControllerSnapshot, decision: object) -> None:
        """Append one PDB gate decision to the public record.

        Called exactly once per real ``UNDERSTAND -> RUNTIME_EVIDENCE`` gate
        consumption, bounded by ``_pdb_gate_recorded_for_index`` so repeated
        malformed or denied transport attempts within the same logical call
        never re-append an identical decision.
        """
        active = snapshot.hypotheses.active_hypotheses()
        source_state = snapshot.state
        if source_state is ControllerState.RUNTIME_EVIDENCE:
            source_state = ControllerState.UNDERSTAND
        self.pdb_gate_decisions.append({
            "source_state": source_state.value,
            "failure_reproduced": self._failure_reproduced,
            "remaining_pdb_observations": max(0, snapshot.budget_limits.max_pdb_observations - snapshot.budget_state.pdb_observations),
            "failed_patch_attempts": snapshot.budget_state.patch_attempts,
            "active_hypothesis_id": active[0].hypothesis_id if active else None,
            "active_hypothesis_confidence": active[0].confidence.value if active else None,
            "active_hypothesis_requires_runtime_evidence": active[0].requires_runtime_evidence if active else None,
            "allowed": decision.allowed,
            "reason": decision.reason.value,
        })

    def _cached_pdb_gate_decision(self, snapshot: ControllerSnapshot) -> object:
        """Return the gate decision for this logical call, computing it once.

        ``model_call_index`` is constant across the transport retry loop and
        increments only on the next controller step, so caching by it scopes
        the decision to a single :func:`next_directive` call and guarantees
        every reread within that call sees the same decision.
        """
        cached = self._pdb_gate_decision_cache
        if cached is not None and cached[0] == snapshot.model_call_index:
            return cached[1]
        decision = self._evaluate_pdb_gate(snapshot)
        self._pdb_gate_decision_cache = (snapshot.model_call_index, decision)
        return decision

    def _runtime_transition_allowed(self, snapshot: ControllerSnapshot) -> bool:
        if not self._pdb_surface_available or self.policy is DemoPolicy.STATIC_BASELINE:
            return False
        return bool(self._cached_pdb_gate_decision(snapshot).allowed)

    @property
    def _pdb_surface_available(self) -> bool:
        """Return whether the effective registry exposes a real PDB entry point.

        The outer policy is only a treatment request.  The registry is the
        effective capability boundary supplied to this adapter, so a policy
        cannot advertise RuntimeEvidence when the corresponding debugger
        surface was not registered.
        """

        return ActionName.START_PDB_SESSION in set(self.registry.names())

    def _effective_contract(self, snapshot: ControllerSnapshot) -> dict[str, dict[str, Any]]:
        pdb_observations_remaining = max(
            0,
            snapshot.budget_limits.max_pdb_observations
            - snapshot.budget_state.pdb_observations,
        )
        pdb_available = (
            self._pdb_surface_available
            and self.policy is not DemoPolicy.STATIC_BASELINE
            and (
                snapshot.state is ControllerState.RUNTIME_EVIDENCE
                and self._runtime_transition_authorized
                or snapshot.state is not ControllerState.RUNTIME_EVIDENCE
            )
        )
        return _action_contracts_for_state(
            snapshot.state,
            registry=self.registry,
            policy=self.policy,
            session_active=self._pdb_session_active,
            pdb_available=pdb_available,
            pdb_observations_remaining=pdb_observations_remaining,
            post_patch_f2p_collected=self._post_patch_f2p_collected,
            regression_collected=self._regression_collected,
            candidate_applied=(snapshot.candidate_applied or self._candidate_applied),
            external_discovery=is_external_isolated_task(self.task),
        )
    def _request_context(self, snapshot, *, logical_request_index: int, transport_attempt_index: int, rejection: Mapping[str, Any] | None = None):
        request_id = f"{self.run_id}:model-call:{logical_request_index}:attempt:{transport_attempt_index}:{uuid.uuid4().hex}"
        contracts = self._effective_contract(snapshot)
        runtime_allowed = self._runtime_transition_allowed(snapshot)
        effective_actions = list(contracts)
        run_id = self.run_id
        external_reproduce_instructions = (
            "No hidden reproduction target is supplied. In Reproduce, use only "
            "bounded discovery tools over public checkout files to identify a "
            "legitimate public pytest target; hidden verifier tests remain "
            "private. If no legitimate public reproduction is available, "
            "transition to Understand without claiming reproduction. "
        ) if snapshot.state is ControllerState.REPRODUCE and is_external_isolated_task(self.task) else ""
        payload = {"protocol":{"name":"agentic-debugger-live-jsonl","version":LIVE_PROTOCOL_VERSION,"request_id":request_id,"logical_model_call_index":logical_request_index,"transport_attempt_index":transport_attempt_index},"identity":{"evaluation_id":self.evaluation_id,"case_id":self.case_id,"run_id":self.run_id,"trajectory_id":self.trajectory_id},"task":self.task.agent_visible_mapping(),"policy":self.policy.value,"directive_schema":_directive_schema_for_state(snapshot.state),"action_contracts":contracts,"controller":{"state":snapshot.state.value,"task_id":snapshot.task_id,"model_call_index":snapshot.model_call_index,"allowed_actions":effective_actions,"legal_transition_targets":_legal_transition_targets(snapshot.state,pdb_transition_allowed=runtime_allowed,candidate_applied=(snapshot.candidate_applied or self._candidate_applied),external_source_context_observed=self._source_context_observed if is_external_isolated_task(self.task) else None),"budget_limits":{"max_patch_attempts":snapshot.budget_limits.max_patch_attempts,"max_test_runs":snapshot.budget_limits.max_test_runs,"max_pdb_observations":snapshot.budget_limits.max_pdb_observations,"max_active_hypotheses":snapshot.budget_limits.max_active_hypotheses,"max_source_observations":snapshot.budget_limits.max_source_observations},"budget_state":{"patch_attempts":snapshot.budget_state.patch_attempts,"test_runs":snapshot.budget_state.test_runs,"pdb_observations":snapshot.budget_state.pdb_observations,"source_observations":snapshot.budget_state.source_observations},"hypotheses":self._hypotheses(snapshot),"last_observation":snapshot.last_observation.to_mapping() if snapshot.last_observation else None},"history":list(self.history[-32:]),"directive_feedback":dict(rejection) if rejection else None,"instructions":external_reproduce_instructions+"Return one directive JSON object. The request is the complete bounded current context; do not rely on process-local memory. Never return credentials. The 'directive_feedback' field is always present; it is null on the first transport attempt. When 'directive_feedback' is non-null, the previous transport attempt's directive was rejected for the stated category; do not repeat it, and choose a directive that satisfies the allowed_actions, legal_transition_targets, and action_contracts already advertised in this request."}
        if self._rag_context is not None:
            payload["retrieved_context"] = self._rag_context.to_request_mapping()
        return payload
    def next_directive(self,snapshot):
        self._observe_snapshot(snapshot)
        # The gate is consumed from ``UNDERSTAND``.  ``_runtime_transition_authorized``
        # marks that the controller is already inside an authorized RUNTIME_EVIDENCE
        # visit; it is reset to ``False`` whenever the controller has left that
        # state, so a fresh ``UNDERSTAND -> RUNTIME_EVIDENCE`` lifecycle (a later
        # controller step with a different ``model_call_index``) is a distinct
        # gate consumption rather than a reread of the prior visit.
        if snapshot.state is not ControllerState.RUNTIME_EVIDENCE and self._runtime_transition_authorized:
            self._runtime_transition_authorized = False
        if snapshot.state is ControllerState.RUNTIME_EVIDENCE and not self._runtime_transition_authorized:
            self._runtime_transition_authorized = self._runtime_transition_allowed(snapshot)
        effective_contract = self._effective_contract(snapshot)
        logical_request_index = snapshot.model_call_index
        history_entry={"request_index":logical_request_index,"state":snapshot.state.value,"allowed_actions":list(effective_contract),"last_observation":redact_for_recording(snapshot.last_observation.to_mapping()) if snapshot.last_observation else None}
        self.history.append(history_entry)
        del self.history[:-32]
        rejection: dict[str, Any] | None = None
        for attempt in range(self.limits.max_retries+1):
            if self.metrics.model_requests>=self.limits.max_model_requests: self.metrics.termination_reason="model_request_limit"; raise LiveModelAdapterError("live model request limit reached")
            request=redact_for_recording(self._request_context(snapshot,logical_request_index=logical_request_index,transport_attempt_index=attempt+1,rejection=rejection))
            try:
                request_bytes=json.dumps(request,ensure_ascii=False,allow_nan=False).encode("utf-8")
            except (TypeError,ValueError,UnicodeError):
                self.metrics.termination_reason="request_serialization"; raise LiveModelAdapterError("live model context could not be serialized") from None
            try:
                canonical_request_bytes = len(json.dumps(
                    request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8"))
            except (TypeError,ValueError,UnicodeError):
                self.metrics.termination_reason="request_serialization"; raise LiveModelAdapterError("live model context could not be serialized") from None
            self.metrics.observe_request_size(
                canonical_request_bytes,
                stdin_bytes=len(request_bytes) + 1,
            )
            if canonical_request_bytes > MAX_PUBLIC_REQUEST_BYTES:
                self.metrics.setup_error("request_too_large")
                self.metrics.termination_reason="setup_failure"
                raise LiveModelAdapterError("live model context exceeded the configured public request bound")
            if len(request_bytes) + 1 > MAX_STDIN_REQUEST_BYTES:
                self.metrics.setup_error("request_too_large")
                self.metrics.termination_reason="setup_failure"
                raise LiveModelAdapterError("live model context exceeded the configured stdin request bound")
            # RAG-enabled guard: the canonical public request (which is what
            # the transport serializes and byte-bounds) must stay inside the
            # frozen public-evidence budget even with the retrieved context
            # added.  Fail closed here, before any transport call; the guard
            # applies only when RAG context was explicitly enabled, so the
            # frozen QuixBugs runner behavior is unchanged.
            if self._rag_context is not None and len(request_bytes)>PUBLIC_REQUEST_BYTE_BUDGET:
                self.metrics.termination_reason="request_too_large"; raise LiveModelAdapterError("live model context plus RAG context exceeded the public request bound")
            self.metrics.model_requests+=1
            timeout_seconds=min(self.config.request_timeout_seconds,self._remaining())
            phase_started=self.clock()
            try:
                response=self.transport.request(request,timeout_seconds)
                if not isinstance(response,Mapping): raise LiveModelAdapterError("invalid model response",category=DirectiveRejectionCategory.MALFORMED_DIRECTIVE,detail="model response was not a JSON object")
                self.metrics.model_responses+=1
                self.metrics.usage(response.get("usage"))
                raw_directive=_resolve_raw_directive(response)
                attempt_record={
                    "model_call_index": logical_request_index,
                    "transport_attempt_index": attempt+1,
                    "state": snapshot.state.value,
                    "directive": redact_for_recording(raw_directive),
                    "accepted": False,
                    "rejection": None,
                }
                self.directive_attempts.append(attempt_record)
                del self.directive_attempts[:-256]
                # Canonical PDB gate recording point.  The model's real
                # ``UNDERSTAND -> RUNTIME_EVIDENCE`` transition *attempt* is
                # visible in ``raw_directive`` before :func:`_parse` can reject
                # a denied transition as ``ILLEGAL_TRANSITION`` (a denied gate
                # removes RUNTIME_EVIDENCE from ``legal_transition_targets``).
                # Recording here captures both allowed and denied real attempts
                # exactly once, bounded per logical call by
                # ``_pdb_gate_recorded_for_index`` so repeated malformed or
                # denied transport attempts in the same call never re-append.
                if (
                    isinstance(raw_directive, Mapping)
                    and raw_directive.get("kind") == "transition"
                    and raw_directive.get("target_state") == ControllerState.RUNTIME_EVIDENCE.value
                    and snapshot.state is ControllerState.UNDERSTAND
                    and not self._runtime_transition_authorized
                    and self._pdb_surface_available
                    and self.policy is not DemoPolicy.STATIC_BASELINE
                    and self._pdb_gate_recorded_for_index != snapshot.model_call_index
                ):
                    gate_decision = self._cached_pdb_gate_decision(snapshot)
                    self._record_pdb_gate_decision(snapshot, gate_decision)
                    self._pdb_gate_recorded_for_index = snapshot.model_call_index
                contracts = effective_contract
                directive=_parse(raw_directive,snapshot,action_contracts=contracts,directive_kinds=set(_directive_schema_for_state(snapshot.state)),legal_transition_targets=set(_legal_transition_targets(snapshot.state,pdb_transition_allowed=self._runtime_transition_allowed(snapshot),candidate_applied=(snapshot.candidate_applied or self._candidate_applied),external_source_context_observed=self._source_context_observed if is_external_isolated_task(self.task) else None)))
                attempt_record["accepted"] = True
                if isinstance(directive, TransitionDirective) and directive.target_state is ControllerState.RUNTIME_EVIDENCE:
                    self._runtime_transition_authorized = True
                self.history[-1]["directive"]=redact_for_recording(raw_directive)
                return directive
            except ModelRequestBudgetExceeded as exc:
                # The canonical public request for this logical call exceeds
                # the frozen public-evidence budget.  The transport rejected
                # it before any process launch; it is never retried, never
                # counted as a provider error, and never fed back as a
                # malformed directive.  The logical call is unaccounted (the
                # request was constructed but no provider process was
                # launched) and the case terminates with the typed
                # budget-exhausted termination reason.
                self.metrics.model_requests-=1
                self.metrics.termination_reason="public_evidence_budget_exceeded"
                raise
            except LiveTransportError as exc:
                rejection=None
                if exc.adapter_error:
                    self.metrics.adapter_error(exc.kind)
                    if exc.kind in MODEL_OUTPUT_ADAPTER_ERROR_KINDS:
                        self.metrics.invalid_model_response(exc.kind)
                        rejection={"category":DirectiveRejectionCategory.MALFORMED_DIRECTIVE.value,"message":"the command adapter rejected the model output","rejected_transport_attempt":attempt+1}
                        self.directive_rejections.append(dict(rejection))
                        if attempt<self.limits.max_retries: self.metrics.retries+=1; continue
                        self.metrics.termination_reason="invalid_model_response"
                        raise LiveModelAdapterError("model output was rejected by the command adapter", detail="the command adapter rejected the model output") from None
                    if exc.kind == "response_too_large":
                        # The 8 MiB wire guard is a provider-response bound,
                        # not a local setup failure. Keep the bound unchanged
                        # but make the terminal subtype durable and distinct.
                        self.metrics.error("provider_response_bound_exceeded")
                        self.metrics.termination_reason = "provider_response_bound_exceeded"
                        raise LiveModelAdapterError("provider response exceeded the configured bound", detail="provider response exceeded the configured wire bound") from None
                    if exc.kind in PROVIDER_ADAPTER_ERROR_KINDS:
                        self.metrics.error(exc.kind)
                        if attempt<self.limits.max_retries and not (
                            exc.kind == "timeout" and not self.limits.retry_provider_timeouts
                        ):
                            self.metrics.retries+=1; continue
                        self.metrics.termination_reason="request_timeout" if exc.kind == "timeout" else "provider_or_transport_error"
                        raise LiveModelAdapterError("model transport failed") from None
                    self.metrics.setup_error(exc.kind)
                    self.metrics.termination_reason="setup_failure"
                    raise LiveModelAdapterError("model adapter setup failed", detail="the command adapter failed before a valid model treatment") from None
                if exc.kind == "process_error":
                    # A generic child exit is local command infrastructure
                    # evidence, not proof of a provider outage.  Preserve the
                    # transport kind in setup evidence and keep the frozen
                    # retry policy, but fail closed as infrastructure on the
                    # terminal attempt.
                    self.metrics.setup_error(exc.kind)
                    if attempt < self.limits.max_retries:
                        self.metrics.retries += 1
                        continue
                    self.metrics.termination_reason = "setup_failure"
                    raise LiveModelAdapterError(
                        "model command adapter infrastructure failed",
                        detail="the command adapter exited without a typed error envelope",
                    ) from None
                self.metrics.error(exc.kind)
                if attempt<self.limits.max_retries and not (
                    exc.timed_out and not self.limits.retry_provider_timeouts
                ):
                    self.metrics.retries+=1; continue
                self.metrics.termination_reason="request_timeout" if exc.timed_out else "provider_or_transport_error"; raise LiveModelAdapterError("model transport failed") from None
            except LiveModelAdapterError as exc:
                rejection={"category":exc.category.value,"message":exc.detail or "the directive was rejected","rejected_transport_attempt":attempt+1}
                if (
                    self.directive_attempts
                    and self.directive_attempts[-1].get("model_call_index") == logical_request_index
                    and self.directive_attempts[-1].get("transport_attempt_index") == attempt+1
                    and self.directive_attempts[-1].get("accepted") is False
                ):
                    self.directive_attempts[-1]["rejection"] = dict(rejection)
                self.directive_rejections.append(dict(rejection))
                self.metrics.invalid_model_response()
                if attempt<self.limits.max_retries: self.metrics.retries+=1; continue
                self.metrics.termination_reason="invalid_model_response"; raise
            finally:
                self.metrics.observe_provider_generation(self.transport)
                self.model_phase_elapsed_seconds += max(0.0,self.clock()-phase_started)
    def _remaining(self):
        left=self.limits.max_model_phase_seconds-self.model_phase_elapsed_seconds
        if left<=0: self.metrics.termination_reason="elapsed_time_limit"; raise LiveModelAdapterError("live elapsed time limit reached")
        return left

@dataclass(frozen=True)
class LiveCaseResult:
    task_id:str; policy:str; repetition:int; status:LiveCaseStatus; controller:dict[str,Any]; verifier:dict[str,Any]; measurements:dict[str,Any]; reporting:dict[str,Any]; events_jsonl:str; diagnostics:tuple[str,...]=(); case_id:str=""; run_id:str=""; trajectory_id:str=""; evidence:dict[str,Any]|None=None
    def to_mapping(self):
        result={"schema_version":LIVE_SCHEMA_VERSION,"case_id":self.case_id,"run_id":self.run_id,"trajectory_id":self.trajectory_id,"task_id":self.task_id,"policy":self.policy,"repetition":self.repetition,"status":self.status.value,"controller":self.controller,"verifier":self.verifier,"measurements":self.measurements,"reporting":self.reporting,"events_jsonl":self.events_jsonl,"diagnostics":list(self.diagnostics)}
        if self.evidence is not None:
            result["evidence"]=self.evidence
        return redact_for_recording(result)

@dataclass(frozen=True)
class RejectedLiveReport:
    reason: str
    def to_mapping(self):
        return {"schema_version":LIVE_SCHEMA_VERSION,"report_id":"rejected-live-evaluation","evaluation_id":None,"run_label":None,"mode":"live","disposition":"attempted_but_rejected","completion":"not_started","model":None,"configuration":None,"selected_tasks":[],"selected_policies":[],"repetitions":0,"expected_case_count":0,"started_case_count":0,"completed_case_count":0,"incomplete_case_count":0,"unstarted_case_count":0,"interrupted":False,"evaluation_cleanup":"not_started","evaluation_cleanup_error":None,"rejection_reason":redact_for_recording(self.reason),"cases":[]}

def rejected_live_report(reason: str) -> RejectedLiveReport:
    return RejectedLiveReport(str(redact_for_recording(reason)))

def _project_events_safe(result: ControllerRunResult, config: LiveModelConfig) -> str:
    stream=io.StringIO()
    logger=JsonlEventLogger(result.run_id,result.task_id,stream=stream)
    try:
        for event in project_controller_run(result,tool_version=config.tool_version,model=config.model_name,timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),duration_ms=None):
            logger.append(RunEvent.from_mapping(redact_for_recording(event.to_mapping())))
        logger.flush()
        return stream.getvalue()
    finally:
        logger.close()

def _owned_case_dir(parent: Path) -> Path:
    if not parent.is_dir(): raise LiveConfigurationError("workspace parent is not an existing directory")
    for _ in range(16):
        path=parent/f"agentic-live-case-{uuid.uuid4().hex}"
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise LiveEvaluationError("live case directory collision limit reached")

def _owned_evaluation_dir() -> Path:
    parent_root=Path(tempfile.gettempdir())
    for _ in range(16):
        path=parent_root/f"agentic-live-evaluation-{uuid.uuid4().hex}"
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise LiveEvaluationError("live evaluation directory collision limit reached")

def _remove_owned_evaluation_dir(path: Path|None) -> tuple[bool,str|None]:
    if path is None: return True,None
    try:
        if path.parent.resolve()!=Path(tempfile.gettempdir()).resolve() or not path.name.startswith("agentic-live-evaluation-"):
            return False,"evaluation directory ownership check failed"
        shutil.rmtree(path)
        return (not path.exists()),None if not path.exists() else "evaluation directory remains"
    except KeyboardInterrupt:
        return False,"evaluation cleanup interrupted"
    except Exception as exc:
        return False,redact_for_recording(bounded_error(exc))

def _remove_owned_case_dir(path: Path|None,parent: Path) -> tuple[bool,str|None]:
    if path is None: return True,None
    try:
        if path.parent.resolve()!=parent.resolve() or not path.name.startswith("agentic-live-case-"): return False,"case directory ownership check failed"
        shutil.rmtree(path)
        return (not path.exists()),None if not path.exists() else "case directory remains"
    except KeyboardInterrupt:
        return False,"case directory cleanup interrupted"
    except Exception as exc:
        return False,redact_for_recording(bounded_error(exc))

def _interrupted_case_result(task_id:str,policy:DemoPolicy,repetition:int,evaluation_id:str,diagnostic:str)->LiveCaseResult:
    case_id=f"{evaluation_id}:{task_id}:{policy.value}:r{repetition}"
    measurements=LiveModelMetrics(termination_reason="interrupted").to_mapping()
    measurements.update({"successful_pdb_observation_count":0,"failed_pdb_observation_count":0,"tool_call_count":0,"case_elapsed_duration_ms":0,"model_phase_elapsed_duration_ms":0,"model_transport_duration_ms":0,"elapsed_scope":"case_observed; model_phase=transport_only"})
    return LiveCaseResult(
        task_id=task_id,
        policy=policy.value,
        repetition=repetition,
        status=LiveCaseStatus.INCOMPLETE,
        controller={"completed":False,"final_state":None,"stop_reason":"interrupted","model_calls":0,"exception":False},
        verifier={"executed":False,"failure":False,"status":None,"outcome":None,"baseline_valid":None,"patch_application":None,"fail_to_pass":None,"pass_to_pass":None,"workspace_cleaned":None,"canonical_fixture_unchanged":None,"localization":{"outcome":"NO_LOCALIZATION"}},
        measurements=measurements,
        reporting={"mode":"live","completed":False,"partial":True,"interrupted":True,"event_recorded":False,"cleanup":"not_started","case_directory_owned":False},
        events_jsonl="",
        diagnostics=("run interrupted before case setup: "+diagnostic,),
        case_id=case_id,
        run_id=f"live-{case_id}",
        trajectory_id=f"live-{case_id}",
    )

def _finalize_live_case(*,task_id,policy,repetition,case_id,run_id,config,task,context,workspace,result,metrics,live_adapter,started,interrupted,controller_failed,diagnostics,verify,extra_cleanup,extra_cleanup_owned,evidence=None,campaign_version=2):
    """Shared verifier/event/cleanup/status/report tail for one live case.

    Both the curated (:func:`_acceptance_live_case`) and QuixBugs
    (:mod:`agentic_debugger.evaluation.live_quixbugs`) live case pipelines
    converge here so verifier invocation, event projection, cleanup
    accounting, status classification, and report assembly stay a single
    accepted implementation rather than two independently maintained copies.
    ``verify`` is a zero-argument callable invoked only under the exact same
    guarded conditions the original curated-only implementation used;
    ``extra_cleanup`` performs the caller-owned case-directory cleanup (a
    local temp directory for curated cases, an owned external WSL workspace
    for QuixBugs cases) and returns ``(removed, error)``.

    ``campaign_version`` selects the versioned terminal-classification
    contract.  Campaigns below v4 keep the frozen classification unchanged
    (a ``pdb-on-uncertainty`` case that completed without PDB stack
    observations classifies as ``PDB_NOT_REACHED`` even when the verifier
    executed).  v4 campaigns classify a case whose verifier executed by the
    verifier semantic outcome (``RESOLVED`` / ``UNRESOLVED``, or
    ``VERIFIER_FAILED`` when the verifier did not complete) before any
    ``PDB_NOT_REACHED`` rule; ``PDB_NOT_REACHED`` then applies only when no
    authoritative verifier result exists.
    """
    verifier=None; verifier_started=False; verifier_failed=False; event_failed=False; events=""
    if result is not None and context is not None and result.final_state is ControllerState.DONE and context.patch_applied and context.candidate_patch:
        try:
            verifier_started=True
            verifier=verify()
        except KeyboardInterrupt:
            interrupted=True; diagnostics.append("verifier interrupted by operator")
        except Exception as exc:
            verifier_failed=True; diagnostics.append(redact_for_recording(bounded_error(exc)))
    elif result is not None and result.final_state is ControllerState.DONE:
        diagnostics.append("controller completed without an accepted patch")
    if result is not None:
        try: events=_project_events_safe(result,config)
        except KeyboardInterrupt:
            interrupted=True; diagnostics.append("event projection interrupted by operator")
        except Exception as exc:
            event_failed=True; diagnostics.append(redact_for_recording(bounded_error(exc)))
    cleanup_errors=[]
    if context is not None:
        try: cleanup_errors.extend(redact_for_recording(bounded_error(exc)) for exc in context.release_pdb())
        except KeyboardInterrupt:
            interrupted=True; cleanup_errors.append("PDB cleanup interrupted")
        except Exception as exc: cleanup_errors.append(redact_for_recording(bounded_error(exc)))
    if workspace is not None:
        try:
            workspace.cleanup()
            if Path(workspace.root).exists(): cleanup_errors.append("task workspace root remains after cleanup")
        except KeyboardInterrupt:
            interrupted=True; cleanup_errors.append("task workspace cleanup interrupted")
        except Exception as exc: cleanup_errors.append(redact_for_recording(bounded_error(exc)))
    try:
        case_removed,case_error=extra_cleanup()
    except KeyboardInterrupt:
        case_removed=False; case_error="case cleanup interrupted"; interrupted=True
    except Exception as exc:
        case_removed=False; case_error=redact_for_recording(bounded_error(exc))
    if case_error:
        cleanup_errors.append(case_error)
        if "interrupted" in case_error:
            interrupted=True
    diagnostics.extend(cleanup_errors)
    if interrupted: metrics.termination_reason="interrupted"
    if interrupted: status=LiveCaseStatus.INCOMPLETE
    elif cleanup_errors: status=LiveCaseStatus.CLEANUP_FAILED
    elif event_failed: status=LiveCaseStatus.EVENT_REPORTING_FAILED
    elif verifier_failed: status=LiveCaseStatus.VERIFIER_FAILED
    elif metrics.termination_reason=="public_evidence_budget_exceeded" and metrics.model_responses>=1:
        # The next public request exceeded the frozen public-evidence budget
        # after at least one genuinely completed provider response.  The case
        # stopped before another provider process was launched; the pre-PDB
        # completed-response shape is terminalized as PDB_NOT_REACHED with the
        # completed-response terminal transport evidence bound to the last
        # completed provider response.  When the controller reached Patch and
        # applied a candidate but the next public request would have exceeded
        # the budget before the transition to Validate, the case is
        # terminalized as VALIDATION_NOT_REACHED (the verifier never ran).
        # Zero-contact budget stops keep the existing fail-closed
        # infrastructure classification below.
        if context is not None and context.patch_applied and context.candidate_patch and result is not None and result.final_state is not ControllerState.DONE:
            status=LiveCaseStatus.VALIDATION_NOT_REACHED
        else:
            status=LiveCaseStatus.PDB_NOT_REACHED
    elif metrics.termination_reason in {"request_timeout","elapsed_time_limit"}: status=LiveCaseStatus.TIMED_OUT
    elif metrics.termination_reason in {"provider_or_transport_error","invalid_model_response","provider_response_bound_exceeded"}: status=LiveCaseStatus.PROVIDER_ERROR
    elif controller_failed: status=LiveCaseStatus.CONTROLLER_FAILED
    elif result is None: status=LiveCaseStatus.HARNESS_ERROR
    elif result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED: status=LiveCaseStatus.CONTROLLER_REJECTED
    elif result.final_state is not ControllerState.DONE: status=LiveCaseStatus.CONTROLLER_FAILED
    elif campaign_version >= 4 and verifier is not None:
        # v4 verifier-authoritative classification: a case whose independent
        # verifier executed is classified by the verifier semantic outcome
        # before any PDB_NOT_REACHED rule.  A verifier that ran but did not
        # complete stays an honest VERIFIER_FAILED infrastructure outcome.
        if verifier.status.value != "COMPLETED": status=LiveCaseStatus.VERIFIER_FAILED
        elif verifier.outcome is not None and verifier.outcome.value=="RESOLVED": status=LiveCaseStatus.RESOLVED
        else: status=LiveCaseStatus.UNRESOLVED
    elif policy is DemoPolicy.PDB_ON_UNCERTAINTY and not any(
        step.action and step.action.name in {
            ActionName.GET_STACK_SUMMARY.value,
            ActionName.GET_FRAME_LOCALS.value,
            ActionName.SAFE_EVAL_EXPRESSION.value,
            ActionName.CONTINUE_PDB_SESSION.value,
            ActionName.STEP_PDB_SESSION.value,
            ActionName.NEXT_PDB_SESSION.value,
        }
        and step.observation and step.observation.status.value == "ok"
        for step in (result.steps if result is not None else ())
    ): status=LiveCaseStatus.PDB_NOT_REACHED
    elif verifier is None: status=LiveCaseStatus.UNRESOLVED
    elif verifier.status.value!="COMPLETED": status=LiveCaseStatus.VERIFIER_FAILED
    elif verifier.outcome is not None and verifier.outcome.value=="RESOLVED": status=LiveCaseStatus.RESOLVED
    else: status=LiveCaseStatus.UNRESOLVED
    controller_data={"completed":bool(result and result.final_state is ControllerState.DONE),"final_state":result.final_state.value if result else None,"stop_reason":result.stop_reason.value if result else ("controller_exception" if controller_failed else ("interrupted" if interrupted else None)),"model_calls":result.model_calls if result else metrics.model_requests,"exception":controller_failed}
    verification={"executed":verifier_started and verifier is not None,"failure":verifier_failed,"status":verifier.status.value if verifier else None,"outcome":verifier.outcome.value if verifier and verifier.outcome else None,"baseline_valid":verifier.baseline.valid if verifier else None,"patch_application":verifier.patch_application.to_mapping() if verifier else None,"fail_to_pass":{"passed":verifier.f2p_passed,"total":verifier.f2p_total} if verifier else None,"pass_to_pass":{"passed":verifier.p2p_passed,"total":verifier.p2p_total} if verifier else None,"workspace_cleaned":verifier.workspace.cleaned if verifier else None,"canonical_fixture_unchanged":verifier.workspace.canonical_fixture_unchanged if verifier else None}
    verification["localization"]=localization_record(context.declared_localization,context.patch_changed_files,context.patch_applied,task.oracle.target_files,task.oracle.target_symbols) if context else {"outcome":"NO_LOCALIZATION"}
    pdb_success=0; pdb_failed=0
    if result is not None:
        for step in result.steps:
            if step.action and step.action.name in {
                ActionName.GET_STACK_SUMMARY,
                ActionName.GET_FRAME_LOCALS,
                ActionName.SAFE_EVAL_EXPRESSION,
                ActionName.CONTINUE_PDB_SESSION,
                ActionName.STEP_PDB_SESSION,
                ActionName.NEXT_PDB_SESSION,
            }:
                if step.observation and step.observation.status.value=="ok": pdb_success+=1
                else: pdb_failed+=1
    model_phase_ms=int(live_adapter.model_phase_elapsed_seconds*1000) if live_adapter else 0
    measurements=metrics.to_mapping(); measurements.update({"successful_pdb_observation_count":pdb_success,"failed_pdb_observation_count":pdb_failed,"tool_call_count":len(context.tool_calls) if context else 0,"case_elapsed_duration_ms":int((time.monotonic()-started)*1000),"model_phase_elapsed_duration_ms":model_phase_ms,"model_transport_duration_ms":model_phase_ms,"elapsed_scope":"case_observed; model_phase=transport_only"})
    completed=status not in {LiveCaseStatus.INCOMPLETE,LiveCaseStatus.CLEANUP_FAILED}
    reporting={"mode":"live","completed":completed,"partial":not completed,"interrupted":interrupted,"event_recorded":bool(events),"cleanup":"cleaned" if case_removed and not cleanup_errors else "failed","case_directory_owned":extra_cleanup_owned}
    return LiveCaseResult(
        task_id=task_id,
        policy=policy.value,
        repetition=repetition,
        status=status,
        controller=controller_data,
        verifier=verification,
        measurements=measurements,
        reporting=reporting,
        events_jsonl=events,
        diagnostics=tuple(diagnostics),
        case_id=case_id,
        run_id=run_id,
        trajectory_id=run_id,
        evidence=evidence,
    )

def _acceptance_live_case(*,repository_root,task_id,policy,repetition,workspace_parent,config,limits,transport,evaluation_id="local",interactive_debugger_controls=False,retain_observable_model_directives=False):
    repo=Path(repository_root).resolve(); parent=Path(workspace_parent).resolve(); task=load_task(str(repo/CURATED_RELATIVE_ROOT/task_id/"task.json"))
    case_id=f"{evaluation_id}:{task_id}:{policy.value}:r{repetition}"; run_id=f"live-{case_id}"; started=time.monotonic()
    case_dir=None; workspace=None; context=None; result=None; live_adapter=None; metrics=LiveModelMetrics(); diagnostics=[]; interrupted=False; controller_failed=False
    try:
        case_dir=_owned_case_dir(parent)
        workspace=TaskWorkspace(str(repo/CURATED_RELATIVE_ROOT/task_id),parent_dir=str(case_dir))
        probe=prepare_pdb_probe(
            repo/CURATED_RELATIVE_ROOT/task_id,
            scenario_for(task_id),
            case_dir,
            model_selects_breakpoint=interactive_debugger_controls,
        ) if policy is DemoPolicy.PDB_ON_UNCERTAINTY else None
        context=DemoToolContext(task=task,workspace=workspace,patch="",probe=probe)
        registry=build_registry(
            context,
            pdb_policy=pdb_policy_for(policy),
            interactive_debugger_controls=interactive_debugger_controls,
        )
        live_adapter=LiveModelAdapter(task=task,policy=policy,config=config,transport=transport,limits=limits,registry=registry,evaluation_id=evaluation_id,case_id=case_id,run_id=run_id,trajectory_id=run_id)
        metrics=live_adapter.metrics
        controller=DeterministicController(registry,live_adapter,ControllerRunConfig(max_model_calls=limits.max_controller_steps))
        try:
            result=controller.run(ControllerSnapshot(run_id,task_id,ControllerState.REPRODUCE,0,ControllerBudgetLimits.from_task_constraints(task.constraints),ControllerBudgetState(),HypothesisLedger()))
        except KeyboardInterrupt:
            interrupted=True; diagnostics.append("controller interrupted by operator")
        except Exception as exc:
            controller_failed=True; diagnostics.append(redact_for_recording(bounded_error(exc)))
    except KeyboardInterrupt:
        interrupted=True; diagnostics.append("run interrupted by operator")
    except Exception as exc:
        diagnostics.append(redact_for_recording(bounded_error(exc)))
    observable_evidence = None
    if retain_observable_model_directives and live_adapter is not None:
        observable_evidence = {
            "observable_model_directive_attempts": list(live_adapter.directive_attempts),
            "observable_model_directives": [
                {
                    "model_call_index": entry.get("request_index"),
                    "state": entry.get("state"),
                    "directive": entry.get("directive"),
                    "last_observation": entry.get("last_observation"),
                }
                for entry in live_adapter.history
                if entry.get("directive") is not None
            ]
        }
    return _finalize_live_case(
        task_id=task_id,policy=policy,repetition=repetition,case_id=case_id,run_id=run_id,config=config,
        task=task,context=context,workspace=workspace,result=result,metrics=metrics,live_adapter=live_adapter,
        started=started,interrupted=interrupted,controller_failed=controller_failed,diagnostics=diagnostics,
        verify=lambda: EvaluationVerifier(str(repo),workspace_parent=str(case_dir)).evaluate(task,context.candidate_patch),
        extra_cleanup=lambda: _remove_owned_case_dir(case_dir,parent),
        extra_cleanup_owned=case_dir is not None,
        evidence=observable_evidence,
    )

run_live_case=_acceptance_live_case

def _acceptance_live_evaluation(*,repository_root,authorization,config,limits,task_ids=None,policies=None,repetitions=1,workspace_parent=None,transport_factory=None,evaluation_id=None,interactive_debugger_controls=False,retain_observable_model_directives=False):
    if type(authorization) is not LiveExecutionAuthorization: raise LiveOptInError("live execution requires explicit authorization")
    if type(repetitions) is not int or not 1<=repetitions<=100: raise LiveConfigurationError("repetitions is invalid")
    evaluation_id,run_label=_new_evaluation_identity(evaluation_id)
    repo=Path(repository_root).resolve()
    available=tuple(sorted(path.name for path in (repo/CURATED_RELATIVE_ROOT).iterdir() if (path/"task.json").is_file()))
    selected=tuple(task_ids) if task_ids is not None else available
    if not selected or set(selected)-set(available): raise LiveConfigurationError("unknown or empty curated task selection")
    if len(set(selected)) != len(selected): raise LiveConfigurationError("duplicate task selection is invalid")
    chosen=tuple(policies) if policies is not None else (DemoPolicy.STATIC_BASELINE,DemoPolicy.PDB_ON_UNCERTAINTY)
    if not chosen or any(type(item) is not DemoPolicy for item in chosen): raise LiveConfigurationError("invalid live policy selection")
    if len(set(chosen)) != len(chosen): raise LiveConfigurationError("duplicate policy selection is invalid")
    expected=len(selected)*len(chosen)*repetitions; owned=workspace_parent is None
    parent=_owned_evaluation_dir() if owned else Path(workspace_parent).resolve()
    cases=[]; interrupted=False; stop=False; evaluation_cleanup_error=None
    try:
        for task_id in selected:
            for policy in chosen:
                for repetition in range(1,repetitions+1):
                    try:
                        transport=transport_factory(load_task(str(repo/CURATED_RELATIVE_ROOT/task_id/"task.json")),policy,repetition) if transport_factory else JsonlCommandTransport(config,max_output_bytes=limits.max_response_bytes)
                        cases.append(run_live_case(repository_root=repo,task_id=task_id,policy=policy,repetition=repetition,workspace_parent=parent,config=config,limits=limits,transport=transport,evaluation_id=evaluation_id,interactive_debugger_controls=interactive_debugger_controls,retain_observable_model_directives=retain_observable_model_directives))
                    except KeyboardInterrupt:
                        interrupted=True; stop=True; cases.append(_interrupted_case_result(task_id,policy,repetition,evaluation_id,"transport setup")); break
                    if cases[-1].status is LiveCaseStatus.INCOMPLETE:
                        interrupted=True; stop=True; break
                    if cases[-1].status not in {LiveCaseStatus.RESOLVED,LiveCaseStatus.UNRESOLVED} and not limits.continue_on_task_failure:
                        stop=True; break
                if stop: break
            if stop: break
    finally:
        if owned:
            try:
                evaluation_removed,evaluation_cleanup_error=_remove_owned_evaluation_dir(parent)
            except KeyboardInterrupt:
                evaluation_removed=False; evaluation_cleanup_error="evaluation cleanup interrupted"; interrupted=True
            except Exception as exc:
                evaluation_removed=False; evaluation_cleanup_error=redact_for_recording(bounded_error(exc))
            if evaluation_cleanup_error and "interrupted" in evaluation_cleanup_error:
                interrupted=True
    started_count=len(cases); completed_count=sum(1 for case in cases if case.reporting.get("completed")); incomplete_count=started_count-completed_count; unstarted_count=expected-started_count
    completion="interrupted" if interrupted else ("partial" if unstarted_count or incomplete_count or evaluation_cleanup_error else "complete")
    return {"schema_version":LIVE_SCHEMA_VERSION,"report_id":evaluation_id,"evaluation_id":evaluation_id,"run_label":run_label,"mode":"live","disposition":"configured_live_execution","completion":completion,"model":config.model_name,"configuration":config.to_metadata(limits),"selected_tasks":list(selected),"selected_policies":[item.value for item in chosen],"repetitions":repetitions,"expected_case_count":expected,"started_case_count":started_count,"completed_case_count":completed_count,"incomplete_case_count":incomplete_count,"unstarted_case_count":unstarted_count,"interrupted":interrupted,"evaluation_cleanup":"failed" if evaluation_cleanup_error else ("cleaned" if owned else "not_owned"),"evaluation_cleanup_error":evaluation_cleanup_error,"cases":[case.to_mapping() for case in cases]}

run_live_evaluation=_acceptance_live_evaluation

def render_live_report(report):
    payload=report.to_mapping() if isinstance(report,RejectedLiveReport) else dict(report)
    payload=redact_for_recording(payload)
    configuration=payload.get("configuration") or {}
    lines=["Task 10A real-model evaluation",f"schema: {payload.get('schema_version',LIVE_SCHEMA_VERSION)}",f"mode: {payload.get('mode','unknown')}",f"disposition: {payload.get('disposition','unknown')}",f"completion: {payload.get('completion','unknown')}",f"evaluation_id: {payload.get('evaluation_id') or 'none'}",f"model: {payload.get('model') or 'unknown'}",f"configuration_fingerprint: {configuration.get('configuration_fingerprint') or 'none'}",f"cases: {payload.get('completed_case_count',len(payload.get('cases',[])))}/{payload.get('expected_case_count',len(payload.get('cases',[])))}"]
    if payload.get("evaluation_cleanup") == "failed": lines.append("evaluation cleanup: failed")
    if payload.get("rejection_reason"): lines.append(f"rejection: {payload['rejection_reason']}")
    for case in payload.get("cases",[]):
        measurements=case.get("measurements",{}); usage=measurements.get("token_usage",{})
        lines.append(f"- {case.get('case_id',case.get('task_id'))} status={case.get('status')} requests={measurements.get('model_request_count')} retries={measurements.get('retry_count')} tokens={usage.get('total_tokens')} termination={measurements.get('termination_reason')}")
    return "\n".join(lines)+"\n"

def _schema_error(message: str):
    raise LiveConfigurationError("live report schema is invalid: "+message)

def _require_fields(value: Mapping[str,Any], fields: tuple[str,...], label: str):
    missing=[field for field in fields if field not in value]
    if missing:
        _schema_error(f"{label} is missing fields: {','.join(missing)}")

def _string(value: Any, label: str, *, nullable: bool=False):
    if nullable and value is None:
        return
    if type(value) is not str or not value:
        _schema_error(f"{label} must be a non-empty string")

def _boolean(value: Any, label: str):
    if type(value) is not bool:
        _schema_error(f"{label} must be boolean")

def _count(value: Any, label: str):
    if type(value) is not int or value < 0:
        _schema_error(f"{label} must be a non-negative integer")

def _optional_mapping(value: Any, label: str):
    if value is not None and not isinstance(value,Mapping):
        _schema_error(f"{label} must be an object or null")

def _validate_counter_pair(value: Any, label: str):
    if not isinstance(value,Mapping):
        _schema_error(f"{label} must be an object")
    _require_fields(value,("passed","total"),label)
    _count(value["passed"],label+".passed")
    _count(value["total"],label+".total")
    if value["passed"] > value["total"]:
        _schema_error(f"{label}.passed exceeds total")

def _validate_case(case: Any):
    if not isinstance(case,Mapping):
        _schema_error("case must be an object")
    required=("schema_version","case_id","run_id","trajectory_id","task_id","policy","repetition","status","controller","verifier","measurements","reporting","events_jsonl","diagnostics")
    _require_fields(case,required,"case")
    if case["schema_version"] != LIVE_SCHEMA_VERSION:
        _schema_error("case schema version is unsupported")
    for field in ("case_id","run_id","trajectory_id","task_id","policy"):
        _string(case[field],"case."+field)
    if type(case["repetition"]) is not int or case["repetition"] < 1:
        _schema_error("case.repetition must be a positive integer")
    status=case["status"]
    if status not in {item.value for item in LiveCaseStatus}:
        _schema_error("case.status is unsupported")
    controller=case["controller"]
    if not isinstance(controller,Mapping):
        _schema_error("case.controller must be an object")
    _require_fields(controller,("completed","final_state","stop_reason","model_calls","exception"),"case.controller")
    _boolean(controller["completed"],"case.controller.completed")
    _string(controller["final_state"],"case.controller.final_state",nullable=True)
    _string(controller["stop_reason"],"case.controller.stop_reason",nullable=True)
    _count(controller["model_calls"],"case.controller.model_calls")
    _boolean(controller["exception"],"case.controller.exception")
    verifier=case["verifier"]
    if not isinstance(verifier,Mapping):
        _schema_error("case.verifier must be an object")
    _require_fields(verifier,("executed","failure","status","outcome","baseline_valid","patch_application","fail_to_pass","pass_to_pass","workspace_cleaned","canonical_fixture_unchanged","localization"),"case.verifier")
    for field in ("executed","failure"):
        _boolean(verifier[field],"case.verifier."+field)
    for field in ("status","outcome"):
        _string(verifier[field],"case.verifier."+field,nullable=True)
    for field in ("baseline_valid","workspace_cleaned","canonical_fixture_unchanged"):
        if verifier[field] is not None:
            _boolean(verifier[field],"case.verifier."+field)
    for field in ("patch_application","fail_to_pass","pass_to_pass"):
        _optional_mapping(verifier[field],"case.verifier."+field)
    _optional_mapping(verifier["localization"],"case.verifier.localization")
    measurements=case["measurements"]
    if not isinstance(measurements,Mapping):
        _schema_error("case.measurements must be an object")
    measurement_fields=("model_request_count","model_response_count","retry_count","provider_error_count","provider_error_kinds","token_usage","termination_reason","successful_pdb_observation_count","failed_pdb_observation_count","tool_call_count","case_elapsed_duration_ms","model_phase_elapsed_duration_ms","model_transport_duration_ms","elapsed_scope")
    _require_fields(measurements,measurement_fields,"case.measurements")
    for field in ("model_request_count","model_response_count","retry_count","provider_error_count","successful_pdb_observation_count","failed_pdb_observation_count","tool_call_count","case_elapsed_duration_ms","model_phase_elapsed_duration_ms","model_transport_duration_ms"):
        _count(measurements[field],"case.measurements."+field)
    if not isinstance(measurements["provider_error_kinds"],list) or any(type(item) is not str or not item for item in measurements["provider_error_kinds"]):
        _schema_error("case.measurements.provider_error_kinds must be a string array")
    _string(measurements["termination_reason"],"case.measurements.termination_reason",nullable=True)
    _string(measurements["elapsed_scope"],"case.measurements.elapsed_scope")
    if measurements["model_phase_elapsed_duration_ms"] != measurements["model_transport_duration_ms"] or measurements["elapsed_scope"] != "case_observed; model_phase=transport_only":
        _schema_error("model timing scope is inconsistent")
    usage=measurements["token_usage"]
    if not isinstance(usage,Mapping):
        _schema_error("case.measurements.token_usage must be an object")
    _require_fields(usage,("prompt_tokens","completion_tokens","total_tokens","provider_reported","missing_fields"),"case.measurements.token_usage")
    for field in ("prompt_tokens","completion_tokens","total_tokens"):
        if usage[field] is not None:
            _count(usage[field],"case.measurements.token_usage."+field)
    _boolean(usage["provider_reported"],"case.measurements.token_usage.provider_reported")
    if not isinstance(usage["missing_fields"],list) or any(type(item) is not str or not item for item in usage["missing_fields"]):
        _schema_error("case.measurements.token_usage.missing_fields must be a string array")
    reporting=case["reporting"]
    if not isinstance(reporting,Mapping):
        _schema_error("case.reporting must be an object")
    _require_fields(reporting,("mode","completed","partial","interrupted","event_recorded","cleanup","case_directory_owned"),"case.reporting")
    _string(reporting["mode"],"case.reporting.mode")
    if reporting["mode"] != "live":
        _schema_error("case.reporting.mode is unsupported")
    for field in ("completed","partial","interrupted","event_recorded","case_directory_owned"):
        _boolean(reporting[field],"case.reporting."+field)
    if reporting["partial"] != (not reporting["completed"]):
        _schema_error("case.reporting completed/partial values are inconsistent")
    if reporting["cleanup"] not in {"cleaned","failed","not_started"}:
        _schema_error("case.reporting.cleanup is unsupported")
    if type(case["events_jsonl"]) is not str:
        _schema_error("case.events_jsonl must be a string")
    if not isinstance(case["diagnostics"],list) or any(type(item) is not str for item in case["diagnostics"]):
        _schema_error("case.diagnostics must be a string array")
    if reporting["event_recorded"] != bool(case["events_jsonl"]):
        _schema_error("case event reporting is inconsistent")
    if reporting["cleanup"] == "not_started" and reporting["case_directory_owned"]:
        _schema_error("case cleanup was not started for an owned directory")
    if reporting["cleanup"] == "failed" and reporting["completed"]:
        _schema_error("cleanup failure cannot be a completed case")
    if status == LiveCaseStatus.RESOLVED.value and not (reporting["completed"] and controller["completed"] and verifier["executed"] and verifier["outcome"] == "RESOLVED" and not reporting["interrupted"]):
        _schema_error("resolved case state is inconsistent")
    if status == LiveCaseStatus.INCOMPLETE.value and not (not reporting["completed"] and reporting["partial"] and reporting["interrupted"] and measurements["termination_reason"] == "interrupted"):
        _schema_error("incomplete case state is inconsistent")
    if status == LiveCaseStatus.CLEANUP_FAILED.value and not (not reporting["completed"] and reporting["partial"] and reporting["cleanup"] == "failed"):
        _schema_error("cleanup-failed case state is inconsistent")
    if reporting["interrupted"] and status != LiveCaseStatus.INCOMPLETE.value:
        _schema_error("interrupted case has a non-incomplete status")
    if status == LiveCaseStatus.PROVIDER_ERROR and not (measurements["provider_error_count"] > 0 and measurements["termination_reason"] in {"provider_or_transport_error","invalid_model_response","provider_response_bound_exceeded"}):
        _schema_error("provider-error case measurements are inconsistent")
    if status == LiveCaseStatus.TIMED_OUT and measurements["termination_reason"] not in {"request_timeout","elapsed_time_limit"}:
        _schema_error("timed-out case termination is inconsistent")
    if status == LiveCaseStatus.CONTROLLER_REJECTED and controller["stop_reason"] != ControllerStopReason.DIRECTIVE_REJECTED.value:
        _schema_error("controller-rejected case state is inconsistent")
    if status == LiveCaseStatus.EVENT_REPORTING_FAILED and reporting["event_recorded"]:
        _schema_error("event-reporting failure has recorded events")
    if status == LiveCaseStatus.VERIFIER_FAILED and not verifier["failure"] and verifier.get("status") == "COMPLETED":
        _schema_error("verifier-failed case state is inconsistent")

def _validate_configuration_metadata(value: Any):
    if not isinstance(value,Mapping):
        _schema_error("report.configuration must be an object")
    required=("schema_version","protocol_version","model_name","tool_version","configuration_fingerprint","request_timeout_seconds","continue_on_task_failure","limits")
    _require_fields(value,required,"report.configuration")
    if value["schema_version"] != LIVE_CONFIG_SCHEMA_VERSION or value["protocol_version"] != LIVE_PROTOCOL_VERSION:
        _schema_error("report.configuration schema/protocol version is unsupported")
    _string(value["model_name"],"report.configuration.model_name")
    _string(value["tool_version"],"report.configuration.tool_version")
    if type(value["configuration_fingerprint"]) is not str or not re.fullmatch(r"[0-9a-f]{64}",value["configuration_fingerprint"]):
        _schema_error("report.configuration fingerprint is invalid")
    if type(value["request_timeout_seconds"]) not in (int,float) or not 0 < value["request_timeout_seconds"] <= MAX_LIVE_REQUEST_TIMEOUT_SECONDS:
        _schema_error("report.configuration request timeout is invalid")
    _boolean(value["continue_on_task_failure"],"report.configuration.continue_on_task_failure")
    limits=value["limits"]
    if not isinstance(limits,Mapping):
        _schema_error("report.configuration.limits must be an object")
    limit_fields=("max_model_requests","max_controller_steps","max_model_phase_seconds","max_retries","max_response_bytes","continue_on_task_failure")
    _require_fields(limits,limit_fields,"report.configuration.limits")
    for field in limit_fields[:-1]:
        _count(limits[field],"report.configuration.limits."+field)
    _boolean(limits["continue_on_task_failure"],"report.configuration.limits.continue_on_task_failure")
    if limits["continue_on_task_failure"] != value["continue_on_task_failure"]:
        _schema_error("report configuration failure policy is inconsistent")

def validate_live_report(report):
    payload=report.to_mapping() if isinstance(report,RejectedLiveReport) else report
    if not isinstance(payload,Mapping):
        _schema_error("report must be an object")
    required=("schema_version","report_id","evaluation_id","run_label","mode","disposition","completion","model","configuration","selected_tasks","selected_policies","repetitions","expected_case_count","started_case_count","completed_case_count","incomplete_case_count","unstarted_case_count","interrupted","evaluation_cleanup","evaluation_cleanup_error","cases")
    _require_fields(payload,required,"report")
    if payload["schema_version"] != LIVE_SCHEMA_VERSION:
        _schema_error("report schema version is unsupported")
    for field in ("report_id","mode","disposition","completion","evaluation_cleanup"):
        _string(payload[field],"report."+field)
    _string(payload["evaluation_id"],"report.evaluation_id",nullable=True)
    _string(payload["run_label"],"report.run_label",nullable=True)
    if payload["mode"] != "live":
        _schema_error("report.mode is unsupported")
    if payload["disposition"] not in {"configured_live_execution","attempted_but_rejected"}:
        _schema_error("report.disposition is unsupported")
    if payload["completion"] not in {"complete","partial","interrupted","not_started"}:
        _schema_error("report.completion is unsupported")
    if payload["model"] is not None:
        _string(payload["model"],"report.model")
    if not isinstance(payload["selected_tasks"],list) or any(type(item) is not str or not item for item in payload["selected_tasks"]):
        _schema_error("report.selected_tasks must be a string array")
    if not isinstance(payload["selected_policies"],list) or any(type(item) is not str or not item for item in payload["selected_policies"]):
        _schema_error("report.selected_policies must be a string array")
    if len(set(payload["selected_tasks"])) != len(payload["selected_tasks"]):
        _schema_error("report.selected_tasks are not unique")
    if len(set(payload["selected_policies"])) != len(payload["selected_policies"]):
        _schema_error("report.selected_policies are not unique")
    if type(payload["repetitions"]) is not int or payload["repetitions"] < 0:
        _schema_error("report.repetitions must be a non-negative integer")
    for field in ("expected_case_count","started_case_count","completed_case_count","incomplete_case_count","unstarted_case_count"):
        _count(payload[field],"report."+field)
    _boolean(payload["interrupted"],"report.interrupted")
    if payload["evaluation_cleanup"] not in {"cleaned","failed","not_owned","not_started"}:
        _schema_error("report.evaluation_cleanup is unsupported")
    if payload["evaluation_cleanup_error"] is not None:
        _string(payload["evaluation_cleanup_error"],"report.evaluation_cleanup_error")
    cases=payload["cases"]
    if not isinstance(cases,list):
        _schema_error("report.cases must be an array")
    expected=payload["expected_case_count"]; started=payload["started_case_count"]; completed=payload["completed_case_count"]; incomplete=payload["incomplete_case_count"]; unstarted=payload["unstarted_case_count"]
    if started != len(cases) or started > expected or completed + incomplete != started or unstarted != expected-started:
        _schema_error("report case counts are inconsistent")
    if payload["disposition"] == "attempted_but_rejected":
        if not (payload["evaluation_id"] is None and payload["run_label"] is None and payload["configuration"] is None and payload["model"] is None and payload["completion"] == "not_started" and payload["repetitions"] == 0 and not payload["selected_tasks"] and not payload["selected_policies"] and expected == started == completed == incomplete == unstarted == 0 and not payload["interrupted"] and payload["evaluation_cleanup"] == "not_started" and not cases):
            _schema_error("rejected report state is inconsistent")
        _string(payload.get("rejection_reason"),"report.rejection_reason")
        return payload
    if payload["evaluation_id"] is None or payload["evaluation_id"] != payload["report_id"] or payload["model"] is None or not payload["selected_tasks"] or not payload["selected_policies"] or payload["repetitions"] < 1:
        _schema_error("configured report is missing execution identity")
    if payload["completion"] == "not_started":
        _schema_error("configured report cannot use not_started completion")
    _validate_configuration_metadata(payload["configuration"])
    if expected != len(payload["selected_tasks"])*len(payload["selected_policies"])*payload["repetitions"]:
        _schema_error("report expected case count is inconsistent")
    if payload["completion"] == "interrupted" and not payload["interrupted"]:
        _schema_error("interrupted completion requires interrupted flag")
    if payload["interrupted"] and payload["completion"] != "interrupted":
        _schema_error("interrupted report has invalid completion")
    if payload["interrupted"] and not any(isinstance(case,Mapping) and isinstance(case.get("reporting"),Mapping) and case["reporting"].get("interrupted") is True for case in cases):
        if not (payload["evaluation_cleanup_error"] and "interrupted" in payload["evaluation_cleanup_error"]):
            _schema_error("interrupted report has no interrupted case or cleanup event")
    if payload["completion"] == "complete" and (payload["interrupted"] or unstarted or incomplete or payload["evaluation_cleanup"] == "failed"):
        _schema_error("complete report has incomplete execution or cleanup")
    if payload["completion"] == "partial" and not (unstarted or incomplete or payload["evaluation_cleanup"] == "failed"):
        _schema_error("partial report has no partial condition")
    identities={"case_id":set(),"run_id":set(),"trajectory_id":set()}; combinations=set()
    for case in cases:
        _validate_case(case)
        combination=(case["task_id"],case["policy"],case["repetition"])
        if combination in combinations or combination[0] not in payload["selected_tasks"] or combination[1] not in payload["selected_policies"] or not 1 <= combination[2] <= payload["repetitions"]:
            _schema_error("case task/policy/repetition coverage is inconsistent")
        combinations.add(combination)
        for field in identities:
            if case[field] in identities[field]:
                _schema_error("case identities are not unique")
            identities[field].add(case[field])
    if sum(1 for case in cases if case["reporting"]["completed"]) != completed:
        _schema_error("report completed count is inconsistent")
    return payload

__all__=["COMMAND_ADAPTER_ERROR_KINDS","COMMAND_ADAPTER_ERROR_SCHEMA_VERSION","DirectiveRejectionCategory","JsonlCommandTransport","LiveCaseResult","LiveCaseStatus","LiveConfigurationError","LiveEvaluationError","LiveExecutionAuthorization","LiveModelAdapter","LiveModelAdapterError","LiveModelConfig","LiveModelMetrics","LiveOptInError","LiveRunLimits","LiveTransportError","MODEL_OUTPUT_ADAPTER_ERROR_KINDS","ModelTransport","PROVIDER_ADAPTER_ERROR_KINDS","LIVE_PROTOCOL_VERSION","LIVE_SCHEMA_VERSION","parse_command_adapter_error","redact_for_recording","render_live_report","rejected_live_report","run_live_case","run_live_evaluation","validate_live_report"]
