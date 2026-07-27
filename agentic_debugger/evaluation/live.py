"""Opt-in live-model evaluation over the existing controller and verifier."""
from __future__ import annotations
import hashlib, io, json, os, re, shutil, subprocess, tempfile, threading, time, uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol
from agentic_debugger.agent.controller import ControllerRunConfig, ControllerRunResult, ControllerStopReason, DeterministicController
from agentic_debugger.agent.controller_policy import ActionName, ControllerBudgetLimits, ControllerBudgetState, HypothesisConfidence, HypothesisLedger, HypothesisStatus
from agentic_debugger.agent.model_adapter import ActionDirective, AddHypothesisDirective, ControllerSnapshot, ModelAdapterError, ModelDirective, ReviseHypothesisDirective, SetHypothesisStatusDirective, TransitionDirective
from agentic_debugger.agent.state_machine import ControllerState, TRANSITION_GRAPH
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.runner import CURATED_RELATIVE_ROOT, localization_record
from agentic_debugger.demo.tools import DemoToolContext, build_registry, legal_reproduction_phases, prepare_pdb_probe
from agentic_debugger.evaluation.runner import bounded_error, load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.events.schema import RunEvent
from agentic_debugger.runtime.workspace import TaskWorkspace

class LiveEvaluationError(RuntimeError): pass
class LiveOptInError(LiveEvaluationError): pass
class LiveConfigurationError(LiveEvaluationError): pass
class LiveTransportError(LiveEvaluationError):
    def __init__(self, message: str, *, kind: str = "transport_error", timed_out: bool = False):
        super().__init__(message)
        self.kind = kind
        self.timed_out = timed_out
class LiveModelAdapterError(ModelAdapterError): pass

LIVE_SCHEMA_VERSION = "1.0"
LIVE_PROTOCOL_VERSION = "1.1"
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
LIVE_ACTION_CONTRACTS={ActionName.RUN_REPRODUCTION.value:{"phase":{"type":"string","enum":[]}},ActionName.RUN_REGRESSION_TESTS.value:{},ActionName.CLASSIFY_OUTCOME.value:{},ActionName.FIND_FUNCTION.value:{"name":"string","path":"string"},ActionName.GET_SOURCE_WINDOW.value:{"path":"string","line":"integer"},ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS.value:{"hypothesis_id":"string","statement":"string","target_file":"string","target_symbol":"string","confidence":{"type":"string","enum":[item.value for item in HypothesisConfidence]}},ActionName.APPLY_PATCH.value:{"patch":"unified-diff string"},ActionName.REVERT_PATCH.value:{},ActionName.SYNTAX_CHECK.value:{},ActionName.START_PDB_SESSION.value:{},ActionName.GET_STACK_SUMMARY.value:{},ActionName.GET_FRAME_LOCALS.value:{"frame_id":"integer","pause_generation":"integer"},ActionName.SAFE_EVAL_EXPRESSION.value:{"frame_id":"integer","pause_generation":"integer","expression":"string"},ActionName.STOP_PDB_SESSION.value:{}}

class LiveCaseStatus(str, Enum):
    RESOLVED="RESOLVED"; UNRESOLVED="UNRESOLVED"; CONTROLLER_FAILED="CONTROLLER_FAILED"; CONTROLLER_REJECTED="CONTROLLER_REJECTED"; TIMED_OUT="TIMED_OUT"; PROVIDER_ERROR="PROVIDER_ERROR"; VERIFIER_FAILED="VERIFIER_FAILED"; EVENT_REPORTING_FAILED="EVENT_REPORTING_FAILED"; CLEANUP_FAILED="CLEANUP_FAILED"; HARNESS_ERROR="HARNESS_ERROR"; INCOMPLETE="INCOMPLETE"

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
        if type(self.request_timeout_seconds) not in (int,float) or not 0<self.request_timeout_seconds<=300: raise LiveConfigurationError("live request timeout is invalid")
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
    max_model_requests:int=64; max_controller_steps:int=64; max_model_phase_seconds:int=900; max_retries:int=2; continue_on_task_failure:bool=True; max_response_bytes:int=MAX_MODEL_RESPONSE_BYTES; max_elapsed_seconds:int|None=None
    def __post_init__(self):
        if self.max_elapsed_seconds is not None:
            if type(self.max_elapsed_seconds) is not int:
                raise LiveConfigurationError("max_elapsed_seconds is invalid")
            object.__setattr__(self,"max_model_phase_seconds",self.max_elapsed_seconds)
        for name,value,low,high in (("max_model_requests",self.max_model_requests,1,512),("max_controller_steps",self.max_controller_steps,1,256),("max_model_phase_seconds",self.max_model_phase_seconds,1,3600),("max_retries",self.max_retries,0,8),("max_response_bytes",self.max_response_bytes,1024,4*1024*1024)):
            if type(value) is not int or not low<=value<=high: raise LiveConfigurationError(name+" is invalid")
        if type(self.continue_on_task_failure) is not bool: raise LiveConfigurationError("continue_on_task_failure is invalid")
    def to_mapping(self) -> dict[str,Any]:
        return {"max_model_requests":self.max_model_requests,"max_controller_steps":self.max_controller_steps,"max_model_phase_seconds":self.max_model_phase_seconds,"max_retries":self.max_retries,"max_response_bytes":self.max_response_bytes,"continue_on_task_failure":self.continue_on_task_failure}

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
    def request(self,payload,timeout_seconds):
        try: request_bytes=(json.dumps(payload,ensure_ascii=False,allow_nan=False)+"\n").encode("utf-8")
        except (TypeError,ValueError,UnicodeError): raise LiveTransportError("model request could not be serialized",kind="request_serialization") from None
        environment={"PATH":os.environ.get("PATH",""),"PYTHONIOENCODING":"utf-8"}
        if os.name=="nt" and os.environ.get("SystemRoot"): environment["SystemRoot"]=os.environ["SystemRoot"]
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
        if stdout.truncated: raise LiveTransportError("model response exceeded the configured output bound",kind="response_too_large")
        if process.returncode!=0: raise LiveTransportError("model command failed",kind="process_error")
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
    model_requests:int=0; model_responses:int=0; retries:int=0; provider_errors:int=0; provider_error_kinds:list[str]=field(default_factory=list); prompt_tokens:int|None=None; completion_tokens:int|None=None; total_tokens:int|None=None; usage_reported:bool=False; usage_missing_fields:list[str]=field(default_factory=list); termination_reason:str|None=None
    def error(self,kind):
        self.provider_errors+=1
        if kind not in self.provider_error_kinds: self.provider_error_kinds.append(kind)
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
    def to_mapping(self): return {"model_request_count":self.model_requests,"model_response_count":self.model_responses,"retry_count":self.retries,"provider_error_count":self.provider_errors,"provider_error_kinds":self.provider_error_kinds,"token_usage":{"prompt_tokens":self.prompt_tokens,"completion_tokens":self.completion_tokens,"total_tokens":self.total_tokens,"provider_reported":self.usage_reported,"missing_fields":sorted(set(self.usage_missing_fields))},"termination_reason":self.termination_reason}

def _parse(value)->ModelDirective:
    try:
        if value["kind"]=="action": return ActionDirective(ActionName(value["name"]),value["arguments"])
        if value["kind"]=="transition": return TransitionDirective(ControllerState(value["target_state"]),value["reason"])
        if value["kind"]=="add_hypothesis": return AddHypothesisDirective(value["hypothesis_id"],value["statement"],HypothesisConfidence(value["confidence"]),tuple(value.get("evidence_refs",())),value.get("requires_runtime_evidence",False))
        if value["kind"]=="revise_hypothesis": return ReviseHypothesisDirective(value["hypothesis_id"],value["statement"],HypothesisConfidence(value["confidence"]),tuple(value["evidence_refs"]),value["requires_runtime_evidence"])
        if value["kind"]=="set_hypothesis_status": return SetHypothesisStatusDirective(value["hypothesis_id"],HypothesisStatus(value["status"]))
    except (KeyError,TypeError,ValueError,ModelAdapterError): pass
    raise LiveModelAdapterError("invalid model directive")


def _action_contracts_for_state(state: ControllerState) -> dict[str, dict[str, Any]]:
    contracts = {name: dict(arguments) for name, arguments in LIVE_ACTION_CONTRACTS.items()}
    contracts[ActionName.RUN_REPRODUCTION.value] = {
        "phase": {"type": "string", "enum": list(legal_reproduction_phases(state))}
    }
    return contracts


def _legal_transition_targets(state: ControllerState) -> list[str]:
    return [candidate.value for candidate in ControllerState if candidate in TRANSITION_GRAPH[state]]

class LiveModelAdapter:
    def __init__(self,*,task,policy,config,transport,limits,evaluation_id="evaluation",case_id="case",run_id="run",trajectory_id="trajectory",clock=time.monotonic):
        self.model_name=config.model_name; self.task=task; self.policy=policy; self.config=config; self.transport=transport; self.limits=limits; self.evaluation_id=evaluation_id; self.case_id=case_id; self.run_id=run_id; self.trajectory_id=trajectory_id; self.metrics=LiveModelMetrics(); self.clock=clock; self.model_phase_elapsed_seconds=0.0; self.history=[]
    @staticmethod
    def _hypotheses(snapshot):
        return [{"hypothesis_id":item.hypothesis_id,"statement":item.statement,"confidence":item.confidence.value,"status":item.status.value,"evidence_refs":list(item.evidence_refs),"requires_runtime_evidence":item.requires_runtime_evidence,"revision":item.revision} for item in snapshot.hypotheses.hypotheses]
    def _request_context(self, snapshot, *, logical_request_index: int, transport_attempt_index: int):
        request_id = f"{self.run_id}:model-call:{logical_request_index}:attempt:{transport_attempt_index}:{uuid.uuid4().hex}"
        return {"protocol":{"name":"agentic-debugger-live-jsonl","version":LIVE_PROTOCOL_VERSION,"request_id":request_id,"logical_model_call_index":logical_request_index,"transport_attempt_index":transport_attempt_index},"identity":{"evaluation_id":self.evaluation_id,"case_id":self.case_id,"run_id":self.run_id,"trajectory_id":self.trajectory_id},"task":self.task.agent_visible_mapping(),"policy":self.policy.value,"directive_schema":LIVE_DIRECTIVE_SCHEMA,"action_contracts":_action_contracts_for_state(snapshot.state),"controller":{"state":snapshot.state.value,"task_id":snapshot.task_id,"model_call_index":snapshot.model_call_index,"allowed_actions":[x.value for x in snapshot.allowed_actions],"legal_transition_targets":_legal_transition_targets(snapshot.state),"budget_limits":{"max_patch_attempts":snapshot.budget_limits.max_patch_attempts,"max_test_runs":snapshot.budget_limits.max_test_runs,"max_pdb_observations":snapshot.budget_limits.max_pdb_observations,"max_active_hypotheses":snapshot.budget_limits.max_active_hypotheses,"max_source_observations":snapshot.budget_limits.max_source_observations},"budget_state":{"patch_attempts":snapshot.budget_state.patch_attempts,"test_runs":snapshot.budget_state.test_runs,"pdb_observations":snapshot.budget_state.pdb_observations,"source_observations":snapshot.budget_state.source_observations},"hypotheses":self._hypotheses(snapshot),"last_observation":snapshot.last_observation.to_mapping() if snapshot.last_observation else None},"history":list(self.history[-32:]),"instructions":"Return one directive JSON object. The request is the complete bounded current context; do not rely on process-local memory. Never return credentials."}
    def next_directive(self,snapshot):
        logical_request_index = snapshot.model_call_index
        history_entry={"request_index":logical_request_index,"state":snapshot.state.value,"allowed_actions":[x.value for x in snapshot.allowed_actions],"last_observation":redact_for_recording(snapshot.last_observation.to_mapping()) if snapshot.last_observation else None}
        self.history.append(history_entry)
        del self.history[:-32]
        for attempt in range(self.limits.max_retries+1):
            if self.metrics.model_requests>=self.limits.max_model_requests: self.metrics.termination_reason="model_request_limit"; raise LiveModelAdapterError("live model request limit reached")
            request=redact_for_recording(self._request_context(snapshot,logical_request_index=logical_request_index,transport_attempt_index=attempt+1))
            try:
                request_bytes=json.dumps(request,ensure_ascii=False,allow_nan=False).encode("utf-8")
            except (TypeError,ValueError,UnicodeError):
                self.metrics.termination_reason="request_serialization"; raise LiveModelAdapterError("live model context could not be serialized") from None
            if len(request_bytes)>MAX_MODEL_RESPONSE_BYTES:
                self.metrics.termination_reason="request_too_large"; raise LiveModelAdapterError("live model context exceeded the configured request bound")
            self.metrics.model_requests+=1
            timeout_seconds=min(self.config.request_timeout_seconds,self._remaining())
            phase_started=self.clock()
            try:
                response=self.transport.request(request,timeout_seconds)
                if not isinstance(response,Mapping): raise LiveModelAdapterError("invalid model response")
                self.metrics.model_responses+=1
                self.metrics.usage(response.get("usage"))
                raw_directive=response.get("directive",response)
                directive=_parse(raw_directive)
                self.history[-1]["directive"]=redact_for_recording(raw_directive)
                return directive
            except LiveTransportError as exc:
                self.metrics.error(exc.kind)
                if attempt<self.limits.max_retries: self.metrics.retries+=1; continue
                self.metrics.termination_reason="request_timeout" if exc.timed_out else "provider_or_transport_error"; raise LiveModelAdapterError("model transport failed") from None
            except LiveModelAdapterError:
                self.metrics.error("invalid_model_response")
                if attempt<self.limits.max_retries: self.metrics.retries+=1; continue
                self.metrics.termination_reason="invalid_model_response"; raise
            finally:
                self.model_phase_elapsed_seconds += max(0.0,self.clock()-phase_started)
    def _remaining(self):
        left=self.limits.max_model_phase_seconds-self.model_phase_elapsed_seconds
        if left<=0: self.metrics.termination_reason="elapsed_time_limit"; raise LiveModelAdapterError("live elapsed time limit reached")
        return left

@dataclass(frozen=True)
class LiveCaseResult:
    task_id:str; policy:str; repetition:int; status:LiveCaseStatus; controller:dict[str,Any]; verifier:dict[str,Any]; measurements:dict[str,Any]; reporting:dict[str,Any]; events_jsonl:str; diagnostics:tuple[str,...]=(); case_id:str=""; run_id:str=""; trajectory_id:str=""
    def to_mapping(self): return redact_for_recording({"schema_version":LIVE_SCHEMA_VERSION,"case_id":self.case_id,"run_id":self.run_id,"trajectory_id":self.trajectory_id,"task_id":self.task_id,"policy":self.policy,"repetition":self.repetition,"status":self.status.value,"controller":self.controller,"verifier":self.verifier,"measurements":self.measurements,"reporting":self.reporting,"events_jsonl":self.events_jsonl,"diagnostics":list(self.diagnostics)})

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

def _acceptance_live_case(*,repository_root,task_id,policy,repetition,workspace_parent,config,limits,transport,evaluation_id="local"):
    repo=Path(repository_root).resolve(); parent=Path(workspace_parent).resolve(); task=load_task(str(repo/CURATED_RELATIVE_ROOT/task_id/"task.json"))
    case_id=f"{evaluation_id}:{task_id}:{policy.value}:r{repetition}"; run_id=f"live-{case_id}"; started=time.monotonic()
    case_dir=None; workspace=None; context=None; result=None; verifier=None; adapter=None; metrics=LiveModelMetrics(); events=""; diagnostics=[]; interrupted=False; harness_failed=False; controller_failed=False; verifier_failed=False; event_failed=False; verifier_started=False
    try:
        case_dir=_owned_case_dir(parent)
        workspace=TaskWorkspace(str(repo/CURATED_RELATIVE_ROOT/task_id),parent_dir=str(case_dir))
        probe=prepare_pdb_probe(repo/CURATED_RELATIVE_ROOT/task_id,scenario_for(task_id),case_dir) if policy is DemoPolicy.PDB_ON_UNCERTAINTY else None
        context=DemoToolContext(task=task,workspace=workspace,patch="",probe=probe)
        adapter=LiveModelAdapter(task=task,policy=policy,config=config,transport=transport,limits=limits,evaluation_id=evaluation_id,case_id=case_id,run_id=run_id,trajectory_id=run_id)
        metrics=adapter.metrics
        controller=DeterministicController(build_registry(context,pdb_policy=pdb_policy_for(policy)),adapter,ControllerRunConfig(max_model_calls=limits.max_controller_steps))
        try:
            result=controller.run(ControllerSnapshot(run_id,task_id,ControllerState.REPRODUCE,0,ControllerBudgetLimits.from_task_constraints(task.constraints),ControllerBudgetState(),HypothesisLedger()))
        except KeyboardInterrupt:
            interrupted=True; diagnostics.append("controller interrupted by operator")
        except Exception as exc:
            controller_failed=True; diagnostics.append(redact_for_recording(bounded_error(exc)))
    except KeyboardInterrupt:
        interrupted=True; diagnostics.append("run interrupted by operator")
    except Exception as exc:
        harness_failed=True; diagnostics.append(redact_for_recording(bounded_error(exc)))
    if result is not None and context is not None and result.final_state is ControllerState.DONE and context.patch_applied and context.candidate_patch:
        try:
            verifier_started=True
            verifier=EvaluationVerifier(str(repo),workspace_parent=str(case_dir)).evaluate(task,context.candidate_patch)
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
        case_removed,case_error=_remove_owned_case_dir(case_dir,parent)
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
    elif metrics.termination_reason in {"request_timeout","elapsed_time_limit"}: status=LiveCaseStatus.TIMED_OUT
    elif metrics.termination_reason in {"provider_or_transport_error","invalid_model_response"}: status=LiveCaseStatus.PROVIDER_ERROR
    elif controller_failed: status=LiveCaseStatus.CONTROLLER_FAILED
    elif result is None: status=LiveCaseStatus.HARNESS_ERROR
    elif result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED: status=LiveCaseStatus.CONTROLLER_REJECTED
    elif result.final_state is not ControllerState.DONE: status=LiveCaseStatus.CONTROLLER_FAILED
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
            if step.action and step.action.name in {ActionName.START_PDB_SESSION,ActionName.GET_STACK_SUMMARY,ActionName.GET_FRAME_LOCALS,ActionName.SAFE_EVAL_EXPRESSION}:
                if step.observation and step.observation.status.value=="ok": pdb_success+=1
                else: pdb_failed+=1
    model_phase_ms=int(adapter.model_phase_elapsed_seconds*1000) if adapter else 0
    measurements=metrics.to_mapping(); measurements.update({"successful_pdb_observation_count":pdb_success,"failed_pdb_observation_count":pdb_failed,"tool_call_count":len(context.tool_calls) if context else 0,"case_elapsed_duration_ms":int((time.monotonic()-started)*1000),"model_phase_elapsed_duration_ms":model_phase_ms,"model_transport_duration_ms":model_phase_ms,"elapsed_scope":"case_observed; model_phase=transport_only"})
    completed=status not in {LiveCaseStatus.INCOMPLETE,LiveCaseStatus.CLEANUP_FAILED}
    reporting={"mode":"live","completed":completed,"partial":not completed,"interrupted":interrupted,"event_recorded":bool(events),"cleanup":"cleaned" if case_removed and not cleanup_errors else "failed","case_directory_owned":case_dir is not None}
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
    )

run_live_case=_acceptance_live_case

def _acceptance_live_evaluation(*,repository_root,authorization,config,limits,task_ids=None,policies=None,repetitions=1,workspace_parent=None,transport_factory=None,evaluation_id=None):
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
                        cases.append(run_live_case(repository_root=repo,task_id=task_id,policy=policy,repetition=repetition,workspace_parent=parent,config=config,limits=limits,transport=transport,evaluation_id=evaluation_id))
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
    if status == LiveCaseStatus.PROVIDER_ERROR and not (measurements["provider_error_count"] > 0 and measurements["termination_reason"] in {"provider_or_transport_error","invalid_model_response"}):
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
    if type(value["request_timeout_seconds"]) not in (int,float) or not 0 < value["request_timeout_seconds"] <= 300:
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

__all__=["JsonlCommandTransport","LiveCaseResult","LiveCaseStatus","LiveConfigurationError","LiveEvaluationError","LiveExecutionAuthorization","LiveModelAdapter","LiveModelAdapterError","LiveModelConfig","LiveModelMetrics","LiveOptInError","LiveRunLimits","LiveTransportError","ModelTransport","LIVE_SCHEMA_VERSION","redact_for_recording","render_live_report","rejected_live_report","run_live_case","run_live_evaluation","validate_live_report"]
