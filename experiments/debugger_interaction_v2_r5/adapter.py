"""R5 — generalized bounded patch checkpoint adapter.

Derived from the accepted R3 adapter (immutable).  R5 deltas:

* binds EXCLUSIVELY to the R5 bridge (prompt rendering, command masks,
  parsing, diagnosis/PATCH context, runtime-slice rendering, telemetry and
  provenance hashes);
* the system prompt is computed per task via
  ``bridge.build_system_prompt(script_path)`` and used for both
  ``transport.request(system_prompt=...)`` and
  ``telemetry.system_prompt_sha256``;
* observation rendering passes the production-script region filter
  (``filter_scripts={script_path}, original_line_count=...``) so the appended
  neutral pytest launcher frames never appear in model-facing text.

All staged semantics, diagnosis retention into the bounded PATCH checkpoint,
R3.1 patch progression, and the R3.2 fail-closed metadata-only hunk-count
normalization (B -> C) are identical to the accepted R3 adapter.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    ModelAdapterError,
    ModelDirective,
    ControllerSnapshot,
)
from agentic_debugger.events.schema import Observation

from experiments.debugger_interaction_v2_r5 import bridge
from experiments.debugger_interaction_v2_r5.serialization import (
    SerializationNormalizationError,
    normalize_hunk_counts,
)
from experiments.debugger_interaction_v2_r5.bridge import (
    DebuggerContext,
    DebuggerLifecycle,
    R2Stage,
)


class TransportError(Exception):
    def __init__(self, category: str, detail: str = "") -> None:
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail


class TransportResponse:
    def __init__(self, raw_text: str, usage: Optional[dict[str, Any]] = None) -> None:
        self.raw_text = raw_text
        self.usage = usage


class ModelTransport(Protocol):
    def request(self, system_prompt: str, user_prompt: str, timeout_seconds: float) -> TransportResponse:
        ...


@dataclass(frozen=True)
class SessionState:
    lifecycle: DebuggerLifecycle
    r2_stage: R2Stage
    paused_line: Optional[int] = None
    paused_function: Optional[str] = None
    status_diagnostic: Optional[str] = None


class SessionStateProvider(Protocol):
    def __call__(self) -> SessionState:
        ...


def make_r5_session_state_provider(context: Any, get_r2_stage: Callable[[], R2Stage]) -> Callable[[], SessionState]:
    def provider() -> SessionState:
        pdb_session = getattr(context, "pdb_session", None)
        started = getattr(context, "pdb_session_started", False)
        if pdb_session is None and not started:
            return SessionState(lifecycle=DebuggerLifecycle.NOT_STARTED, r2_stage=R2Stage.NOT_STARTED)
        if pdb_session is None and started:
            return SessionState(lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED, r2_stage=R2Stage.CONSUMED_OR_ENDED)
        stage = get_r2_stage()
        # R5.2 terminal stage: the target ended during a step/next; the
        # stage tracker's terminal stage is authoritative and the worker
        # status query (which would report exited) must not mask it.
        if stage is R2Stage.PAUSED_AFTER_TERMINAL_STEP:
            return SessionState(lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED, r2_stage=stage)
        if stage in (R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK, R2Stage.READY_FOR_DIAGNOSIS):
            return SessionState(lifecycle=DebuggerLifecycle.PAUSED, r2_stage=stage)
        try:
            result = pdb_session.get_target_status()
        except Exception as exc:
            return SessionState(lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED, r2_stage=R2Stage.CONSUMED_OR_ENDED, status_diagnostic=f"get_target_status failed: {type(exc).__name__}")
        state_val = result.get("state") if isinstance(result, dict) else None
        if state_val == "paused":
            effective = stage
            if effective is R2Stage.NOT_STARTED:
                effective = R2Stage.PAUSED_NEEDS_STACK
            return SessionState(lifecycle=DebuggerLifecycle.PAUSED, r2_stage=effective)
        if state_val in ("exited", "failed", "terminated"):
            return SessionState(lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED, r2_stage=R2Stage.CONSUMED_OR_ENDED)
        return SessionState(lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED, r2_stage=R2Stage.CONSUMED_OR_ENDED)
    return provider


NOT_RECORDED = "NOT_RECORDED"
NOT_AVAILABLE = "NOT_AVAILABLE"
MAX_RAW_TEXT_BYTES = 65536


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def serialize_whole_file_to_diff(
    script_path: str,
    original_source: str,
    model_path: str,
    model_content: str,
) -> str:
    """Deterministic whole-file repair serialization (R5.2).

    The MODEL authors the complete replacement file content; this function
    only performs the mechanical unified-diff serialization (standard-library
    ``difflib``) against the original production source.  The resulting diff
    is what PatchManager applies.  Fails closed on a wrong path or content
    identical to the original.
    """
    if model_path != script_path:
        raise bridge.BridgeParseError(
            bridge.BridgeRejection.INVALID_PATCH,
            f"file path {model_path!r} is not the writable production path "
            f"{script_path!r}",
        )
    original_lines = original_source.splitlines()
    new_lines = model_content.splitlines()
    if original_lines == new_lines:
        raise bridge.BridgeParseError(
            bridge.BridgeRejection.INVALID_PATCH,
            "file replacement content is identical to the original source",
        )
    diff = "".join(difflib.unified_diff(
        [line + "\n" for line in original_lines],
        [line + "\n" for line in new_lines],
        fromfile=f"a/{script_path}",
        tofile=f"b/{script_path}",
        lineterm="\n",
        n=3,
    ))
    if not diff.strip():
        raise bridge.BridgeParseError(
            bridge.BridgeRejection.INVALID_PATCH,
            "file replacement produced an empty diff",
        )
    return diff


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bound_text(text: str, limit: int = MAX_RAW_TEXT_BYTES) -> str:
    enc = text.encode("utf-8")
    return text if len(enc) <= limit else text[: limit - 3] + "..."


def _extract_usage(usage: Optional[dict[str, Any]]) -> dict[str, Any]:
    if usage is None or type(usage) is not dict:
        return {"prompt_tokens": NOT_RECORDED, "completion_tokens": NOT_RECORDED, "total_tokens": NOT_RECORDED, "provider_reported": False}
    result: dict[str, Any] = {"provider_reported": True}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = usage.get(key)
        result[key] = val if type(val) is int and val >= 0 else NOT_RECORDED
    return result


@dataclass
class TelemetryRecord:
    model_call_index: int
    transport_attempt_index: int
    controller_state: str
    system_prompt_sha256: str
    user_prompt_sha256: str
    user_prompt_summary: str
    raw_response_text: str = NOT_AVAILABLE
    raw_response_status: str = "transport_failure"
    raw_response_bytes: Any = NOT_AVAILABLE
    transport_error_category: Optional[str] = None
    parse_status: str = "not_attempted"
    command_token: Optional[str] = None
    normalized_command: Optional[str] = None
    rejection_category: Optional[str] = None
    rejection_message: Optional[str] = None
    directive_kind: Optional[str] = None
    action_name: Optional[str] = None
    directive_arguments: Optional[dict[str, Any]] = None
    target_state: Optional[str] = None
    directive_reason: Optional[str] = None
    is_diagnosis: bool = False
    diagnosis_text: Optional[str] = None
    prior_observation_id: Optional[str] = None
    prior_observation_sha256: Optional[str] = None
    rendered_observation_sha256: Optional[str] = None
    rendered_diagnosis_sha256: Optional[str] = None
    model_patch_raw_sha256: Optional[str] = None
    model_patch_serialization_normalized_sha256: Optional[str] = None
    # R5.2: deterministic single-fence unwrap record (None when no unwrap).
    fence_unwrap: Optional[dict] = None
    prompt_tokens: Any = NOT_RECORDED
    completion_tokens: Any = NOT_RECORDED
    total_tokens: Any = NOT_RECORDED
    provider_reported: bool = False
    request_duration_ms: Any = NOT_RECORDED
    parse_duration_ms: Any = NOT_RECORDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_call_index": self.model_call_index,
            "transport_attempt_index": self.transport_attempt_index,
            "controller_state": self.controller_state,
            "request": {"system_prompt_sha256": self.system_prompt_sha256, "user_prompt_sha256": self.user_prompt_sha256, "user_prompt_summary": self.user_prompt_summary},
            "raw_response_text": self.raw_response_text,
            "raw_response_status": self.raw_response_status,
            "raw_response_bytes": self.raw_response_bytes,
            "transport_error_category": self.transport_error_category,
            "parse_result": {"status": self.parse_status, "command_token": self.command_token, "normalized_command": self.normalized_command, "rejection_category": self.rejection_category, "rejection_message": self.rejection_message},
            "translated_directive": {"kind": self.directive_kind, "action_name": self.action_name, "arguments": self.directive_arguments, "target_state": self.target_state, "reason": self.directive_reason, "is_diagnosis": self.is_diagnosis, "diagnosis_text": self.diagnosis_text},
            "provenance": {"prior_observation_id": self.prior_observation_id, "prior_observation_sha256": self.prior_observation_sha256, "rendered_observation_sha256": self.rendered_observation_sha256, "rendered_diagnosis_sha256": self.rendered_diagnosis_sha256},
            "fence_unwrap": self.fence_unwrap,
            "usage": {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens, "total_tokens": self.total_tokens, "provider_reported": self.provider_reported},
            "timing": {"request_duration_ms": self.request_duration_ms, "parse_duration_ms": self.parse_duration_ms},
        }


def _is_ok(observation: Optional[Observation]) -> bool:
    if observation is None:
        return False
    status = observation.status.value if hasattr(observation.status, "value") else str(observation.status)
    return status in ("ok", "completed")


class R5StageTracker:
    def __init__(self) -> None:
        self._stage = R2Stage.NOT_STARTED
        self._last_processed_id: Optional[str] = None

    @property
    def stage(self) -> R2Stage:
        return self._stage

    def update_from_observation(self, obs: Optional[Observation]) -> None:
        if obs is None or not _is_ok(obs):
            return
        oid = obs.observation_id
        if oid == self._last_processed_id:
            return
        payload = obs.payload if isinstance(obs.payload, dict) else {}
        name = obs.name
        state_val = payload.get("state")
        advanced = False
        if self._stage is R2Stage.NOT_STARTED and name == "start_pdb_session" and state_val == "paused":
            self._stage = R2Stage.PAUSED_NEEDS_STACK; advanced = True
        elif self._stage is R2Stage.PAUSED_NEEDS_STACK and name == "get_stack_summary":
            self._stage = R2Stage.PAUSED_NEEDS_INSPECTION; advanced = True
        elif self._stage is R2Stage.PAUSED_NEEDS_INSPECTION and name in ("get_frame_locals", "safe_eval_expression"):
            self._stage = R2Stage.PAUSED_NEEDS_STEP; advanced = True
        elif self._stage is R2Stage.PAUSED_NEEDS_STEP and name in ("step_pdb_session", "next_pdb_session") and state_val == "paused":
            self._stage = R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK; advanced = True
        elif self._stage is R2Stage.PAUSED_NEEDS_STEP and name in ("step_pdb_session", "next_pdb_session") and state_val in ("exited", "failed", "terminated"):
            # R5.2 terminal runtime progression: the target ended during the
            # control action (crash-on-step bug class).  Real terminal
            # evidence (exit code / error) is preserved; diagnosis follows.
            self._stage = R2Stage.PAUSED_AFTER_TERMINAL_STEP; advanced = True
        elif self._stage is R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK and name == "get_stack_summary":
            self._stage = R2Stage.READY_FOR_DIAGNOSIS; advanced = True
        if advanced:
            self._last_processed_id = oid


class R5DebuggerBridgeAdapter:
    def __init__(self, transport: ModelTransport, model_name: str, task_description: str, *, script_path: str, source_text: str, eligible_lines: tuple[int, ...], original_line_count: int, session_state_provider: Callable[[], SessionState], stage_tracker: R5StageTracker, max_retries: int = 1, request_timeout_seconds: float = 60.0) -> None:
        self._transport = transport
        self._model_name = model_name
        self._task_description = task_description
        self._max_retries = max_retries
        self._request_timeout = request_timeout_seconds
        self._telemetry: list[TelemetryRecord] = []
        self._post_debug_diagnoses: list[dict[str, Any]] = []
        self._script_path = script_path
        self._source_text = source_text
        self._eligible_lines = eligible_lines
        self._original_line_count = original_line_count
        # R5: per-task frozen system prompt (path substitution only).
        self._system_prompt = bridge.build_system_prompt(script_path)
        self._session_state_provider = session_state_provider
        self._stage_tracker = stage_tracker
        self._last_paused_line: Optional[int] = None
        self._last_paused_function: Optional[str] = None
        self._retained_diagnosis: Optional[str] = None
        self._diagnosis_provenance: Optional[dict[str, Any]] = None
        self._runtime_slice: dict[str, str] = {}
        self._g1: Optional[int] = None
        self._g2: Optional[int] = None
        # R3.1: PATCH progression — first PATCH turn requires a repair attempt
        self._patch_attempted = False
        # R3.2: per-attempt A/B/C identities + normalization records
        self._patch_attempts: list[dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def retained_diagnosis(self) -> Optional[str]:
        return self._retained_diagnosis

    @property
    def diagnosis_provenance(self) -> Optional[dict[str, Any]]:
        return self._diagnosis_provenance

    @property
    def runtime_slice(self) -> dict[str, str]:
        return dict(self._runtime_slice)

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def system_prompt_sha256(self) -> str:
        return _sha256(self._system_prompt)

    def _render_observation(self, obs: Optional[Observation]) -> str:
        return bridge._render_observation(
            obs,
            filter_scripts=frozenset({self._script_path}),
            original_line_count=self._original_line_count,
        )

    def _capture_runtime_slice(self, obs: Optional[Observation]) -> None:
        if obs is None or not _is_ok(obs):
            return
        payload = obs.payload if isinstance(obs.payload, dict) else {}
        name = obs.name
        # R5.2: the real reproduction failure output is part of the bounded
        # runtime evidence slice (surfaced again at PATCH time).
        if name == "run_reproduction" and "reproduction" not in self._runtime_slice:
            if payload.get("failure_reproduced") is True:
                failure_output = payload.get("failure_output")
                if type(failure_output) is str and failure_output:
                    self._runtime_slice["reproduction"] = self._render_observation(obs)
                    summary = bridge.crash_summary_from_failure_output(
                        failure_output, self._script_path
                    )
                    if summary:
                        self._runtime_slice["crash_summary"] = summary
                    return
        if name == "get_stack_summary" and self._g1 is None:
            gen = payload.get("pause_generation")
            if type(gen) is int and gen > 0 and "stack_G1" not in self._runtime_slice:
                self._runtime_slice["stack_G1"] = self._render_observation(obs)
                self._g1 = gen
                return
        if name in ("get_frame_locals", "safe_eval_expression") and "inspection" not in self._runtime_slice:
            gen = payload.get("pause_generation")
            if self._g1 is not None and gen != self._g1:
                return
            self._runtime_slice["inspection"] = self._render_observation(obs)
            if self._g1 is None and type(gen) is int:
                self._g1 = gen
            return
        if name in ("step_pdb_session", "next_pdb_session") and "step" not in self._runtime_slice:
            state_val = payload.get("state")
            if state_val in ("paused", "exited", "failed", "terminated"):
                self._runtime_slice["step"] = self._render_observation(obs)
                return
        if name == "get_stack_summary" and self._g1 is not None and "stack_G2" not in self._runtime_slice:
            gen = payload.get("pause_generation")
            if type(gen) is int and gen > self._g1:
                self._runtime_slice["stack_G2"] = self._render_observation(obs)
                self._g2 = gen
                return

    def next_directive(self, snapshot: ControllerSnapshot) -> ModelDirective:
        state = snapshot.state
        last_obs = snapshot.last_observation
        self._stage_tracker.update_from_observation(last_obs)
        self._capture_runtime_slice(last_obs)
        self._update_paused_tracking(last_obs)
        prior_obs_id = last_obs.observation_id if last_obs is not None else None
        prior_obs_sha = _sha256(_canonical_json(last_obs.to_mapping())) if last_obs is not None else None
        session_state = self._session_state_provider()
        if session_state.lifecycle is DebuggerLifecycle.PAUSED or session_state.r2_stage is bridge.R2Stage.PAUSED_AFTER_TERMINAL_STEP:
            session_state = SessionState(lifecycle=session_state.lifecycle, r2_stage=session_state.r2_stage, paused_line=self._last_paused_line, paused_function=self._last_paused_function, status_diagnostic=session_state.status_diagnostic)
        if state.value == "Patch" and self._retained_diagnosis is not None:
            debugger_ctx = DebuggerContext(script_path=self._script_path, source_text=self._source_text, eligible_lines=self._eligible_lines, lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED, r2_stage=R2Stage.CONSUMED_OR_ENDED, retained_diagnosis=self._retained_diagnosis, runtime_slice=dict(self._runtime_slice) if self._runtime_slice else None)
        else:
            debugger_ctx = DebuggerContext(script_path=self._script_path, source_text=self._source_text, eligible_lines=self._eligible_lines, lifecycle=session_state.lifecycle, r2_stage=session_state.r2_stage, paused_line=session_state.paused_line, paused_function=session_state.paused_function, runtime_slice=dict(self._runtime_slice) if (session_state.r2_stage is bridge.R2Stage.PAUSED_AFTER_TERMINAL_STEP and self._runtime_slice) else None)
        feedback: Optional[str] = None
        # R3.1: PATCH progression — first PATCH turn is NEEDS_FIRST_REPAIR
        patch_stage: Optional[bridge.R3PatchStage] = None
        if state.value == "Patch":
            if self._patch_attempted:
                patch_stage = bridge.R3PatchStage.RETRY
            else:
                patch_stage = bridge.R3PatchStage.NEEDS_FIRST_REPAIR
        for attempt in range(self._max_retries + 1):
            user_prompt = bridge.render_prompt(state=state, last_observation=last_obs, task_description=self._task_description, feedback=feedback, debugger=debugger_ctx, patch_stage=patch_stage)
            sys_hash = _sha256(self._system_prompt)
            user_hash = _sha256(user_prompt)
            rendered_obs_sha = self._compute_rendered_obs_hash(last_obs)
            rendered_diag_sha = _sha256(self._retained_diagnosis) if self._retained_diagnosis else None
            raw_text: str
            raw_status: str
            transport_error_cat: Optional[str] = None
            raw_bytes: Any
            usage: dict[str, Any]
            req_start = time.monotonic()
            try:
                response = self._transport.request(system_prompt=self._system_prompt, user_prompt=user_prompt, timeout_seconds=self._request_timeout)
                raw_text = response.raw_text
                raw_status = "decoded"
                raw_bytes = len(raw_text.encode("utf-8")) if type(raw_text) is str else NOT_AVAILABLE
                usage = _extract_usage(response.usage)
            except TransportError as exc:
                raw_text = NOT_AVAILABLE; raw_status = "transport_failure"; raw_bytes = NOT_AVAILABLE; transport_error_cat = exc.category; usage = _extract_usage(None); response = None  # type: ignore
            except Exception as exc:
                raw_text = NOT_AVAILABLE; raw_status = "transport_failure"; raw_bytes = NOT_AVAILABLE; transport_error_cat = type(exc).__name__; usage = _extract_usage(None); response = None  # type: ignore
            req_ms = int((time.monotonic() - req_start) * 1000)
            record = TelemetryRecord(model_call_index=snapshot.model_call_index, transport_attempt_index=attempt + 1, controller_state=state.value, system_prompt_sha256=sys_hash, user_prompt_sha256=user_hash, user_prompt_summary=_bound_text(user_prompt, 1000), raw_response_text=raw_text if raw_status == "decoded" else NOT_AVAILABLE, raw_response_status=raw_status, raw_response_bytes=raw_bytes, transport_error_category=transport_error_cat, parse_status="not_attempted", prompt_tokens=usage.get("prompt_tokens", NOT_RECORDED), completion_tokens=usage.get("completion_tokens", NOT_RECORDED), total_tokens=usage.get("total_tokens", NOT_RECORDED), provider_reported=usage.get("provider_reported", False), request_duration_ms=req_ms, prior_observation_id=prior_obs_id, prior_observation_sha256=prior_obs_sha, rendered_observation_sha256=rendered_obs_sha, rendered_diagnosis_sha256=rendered_diag_sha)
            self._telemetry.append(record)
            if raw_status == "transport_failure":
                if attempt < self._max_retries:
                    feedback = f"transport failure ({transport_error_cat}); retry"; continue
                raise ModelAdapterError(f"transport failed after {attempt + 1} attempts: {transport_error_cat}")
            parse_start = time.monotonic()
            try:
                parse_text, fence_unwrap = bridge.unwrap_single_fence(raw_text)
                record.fence_unwrap = (
                    fence_unwrap.to_mapping() if fence_unwrap is not None else None
                )
                result = bridge.parse(parse_text, state, last_obs, lifecycle=session_state.lifecycle, r2_stage=session_state.r2_stage, patch_stage=patch_stage)
                # R5.2 whole-file representation: mechanically serialize the
                # model-authored complete file content into the unified diff
                # that is actually applied (recorded; semantics untouched).
                whole_file_meta: Optional[dict[str, Any]] = None
                if hasattr(result.directive, "name") and getattr(result.directive.name, "value", None) == "apply_patch":
                    args = result.directive.arguments
                    if "whole_file_path" in args:
                        b_diff = serialize_whole_file_to_diff(
                            self._script_path, self._source_text,
                            args["whole_file_path"], args["whole_file_content"],
                        )
                        whole_file_meta = {
                            "path": args["whole_file_path"],
                            "model_whole_file_sha256": _sha256(args["whole_file_content"]),
                            "generated_diff_sha256": _sha256(b_diff),
                        }
                        result = bridge.BridgeResult(
                            command_token="file",
                            normalized_command=f"file {args['whole_file_path']}",
                            directive=ActionDirective(result.directive.name, {"patch": b_diff}),
                            is_diagnosis=result.is_diagnosis,
                            diagnosis_text=result.diagnosis_text,
                        )
                    # R3.2: normalize only hunk-count metadata (B -> C); fail closed otherwise
                    b_diff = result.directive.arguments["patch"]
                    try:
                        c_diff, norm_record = normalize_hunk_counts(b_diff)
                    except SerializationNormalizationError as exc:
                        raise bridge.BridgeParseError(
                            bridge.BridgeRejection.INVALID_PATCH,
                            f"patch serialization normalization failed: {exc}",
                        ) from exc
                    result = bridge.BridgeResult(
                        command_token=result.command_token,
                        normalized_command=result.normalized_command,
                        directive=ActionDirective(result.directive.name, {"patch": c_diff}),
                        is_diagnosis=result.is_diagnosis,
                        diagnosis_text=result.diagnosis_text,
                    )
            except bridge.BridgeParseError as exc:
                parse_ms = int((time.monotonic() - parse_start) * 1000)
                record.parse_status = "rejected"; record.rejection_category = exc.category.value; record.rejection_message = exc.detail; record.parse_duration_ms = parse_ms
                if attempt < self._max_retries:
                    feedback = f"{exc.category.value}: {exc.detail}"; continue
                raise ModelAdapterError(f"bridge parse failed after {attempt + 1} attempts: {exc.category.value}: {exc.detail}") from exc
            parse_ms = int((time.monotonic() - parse_start) * 1000)
            record.parse_status = "accepted"; record.command_token = result.command_token; record.normalized_command = result.normalized_command; record.parse_duration_ms = parse_ms
            directive = result.directive
            if hasattr(directive, "name"):
                record.directive_kind = "action"; record.action_name = directive.name.value; record.directive_arguments = dict(directive.arguments)
                # R3.2: record A/B/C identities for the dispatched candidate
                if record.action_name == "apply_patch":
                    record.model_patch_raw_sha256 = norm_record.model_patch_raw_sha256
                    record.model_patch_serialization_normalized_sha256 = norm_record.model_patch_serialization_normalized_sha256
                    attempt: dict[str, Any] = {
                        "model_call_index": snapshot.model_call_index,
                        "raw_model_response_sha256": _sha256(raw_text) if raw_text != NOT_AVAILABLE else None,
                        "model_patch_raw_sha256": norm_record.model_patch_raw_sha256,
                        "model_patch_serialization_normalized_sha256": norm_record.model_patch_serialization_normalized_sha256,
                        "normalization": norm_record.to_mapping(),
                        "fence_unwrap": record.fence_unwrap,
                    }
                    if whole_file_meta is not None:
                        attempt["representation"] = "whole_file"
                        attempt["whole_file"] = whole_file_meta
                    else:
                        attempt["representation"] = "unified_diff"
                    self._patch_attempts.append(attempt)
            elif hasattr(directive, "target_state"):
                record.directive_kind = "transition"; record.target_state = directive.target_state.value; record.directive_reason = directive.reason
            record.is_diagnosis = result.is_diagnosis; record.diagnosis_text = result.diagnosis_text
            # R3.1: first genuine PATCH repair attempt advances to RETRY
            if state.value == "Patch" and result.command_token == "patch" and not self._patch_attempted:
                self._patch_attempted = True
            if result.is_diagnosis and result.diagnosis_text:
                if self._retained_diagnosis is None:
                    self._retained_diagnosis = result.diagnosis_text
                    self._diagnosis_provenance = {"model_call_index": snapshot.model_call_index, "prior_observation_id": prior_obs_id, "rendered_observation_sha256": rendered_obs_sha, "G1": self._g1, "G2": self._g2, "diagnosis_text_sha256": _sha256(result.diagnosis_text)}
                self._post_debug_diagnoses.append({"text": result.diagnosis_text, "model_call_index": snapshot.model_call_index, "controller_state": state.value, "raw_response_text": _bound_text(raw_text), "provenance": "model-authored, bound to model_call_index " + str(snapshot.model_call_index)})
            return directive
        raise ModelAdapterError("adapter exhausted retries without resolution")

    @property
    def patch_attempted(self) -> bool:
        return self._patch_attempted

    @property
    def patch_attempts(self) -> list[dict[str, Any]]:
        return list(self._patch_attempts)

    @property
    def telemetry(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._telemetry]

    @property
    def post_debug_diagnoses(self) -> list[dict[str, Any]]:
        return list(self._post_debug_diagnoses)

    def _update_paused_tracking(self, last_obs: Optional[Observation]) -> None:
        if last_obs is None:
            return
        name = last_obs.name; payload = last_obs.payload
        if type(payload) is not dict:
            return
        status = last_obs.status.value if hasattr(last_obs.status, "value") else str(last_obs.status)
        if status not in ("ok", "completed"):
            return
        if name == "start_pdb_session":
            self._last_paused_line = payload.get("line"); self._last_paused_function = payload.get("function")
        elif name in ("continue_pdb_session", "step_pdb_session", "next_pdb_session"):
            state_val = payload.get("state")
            if state_val == "paused":
                self._last_paused_line = payload.get("line"); self._last_paused_function = payload.get("function")
            elif state_val in ("exited", "failed", "terminated"):
                # R5.2 terminal progression: retain the last paused location
                # so the terminal stage can name where the target crashed.
                pass
            else:
                self._last_paused_line = None; self._last_paused_function = None

    def _compute_rendered_obs_hash(self, last_obs: Optional[Observation]) -> Optional[str]:
        if last_obs is None:
            return None
        rendered = self._render_observation(last_obs)
        return _sha256(rendered) if rendered else None


class ScriptedBridgeAdapter:
    """Deterministic scripted adapter for engineering tests only.

    Steps are raw command texts (as the model would emit them).  Reference
    repairs may be used ONLY inside deterministic engineering tests; they are
    never part of the live prompt or live semantic path.
    """

    def __init__(self, steps: tuple[str, ...], model_name: str = "scripted-r5", task_description: str = "test task", *, script_path: str = "", source_text: str = "", eligible_lines: tuple[int, ...] = (), original_line_count: int = 0, session_state_provider: Optional[Callable[[], SessionState]] = None, stage_tracker: Optional[R5StageTracker] = None) -> None:
        self._steps = steps; self._index = 0; self._model_name = model_name; self._task_description = task_description
        self._telemetry: list[TelemetryRecord] = []; self._post_debug_diagnoses: list[dict[str, Any]] = []
        self._script_path = script_path; self._source_text = source_text; self._eligible_lines = eligible_lines
        self._original_line_count = original_line_count
        self._system_prompt = bridge.build_system_prompt(script_path)
        self._stage_tracker = stage_tracker or R5StageTracker()
        self._session_state_provider = session_state_provider or (lambda: SessionState(lifecycle=DebuggerLifecycle.NOT_STARTED, r2_stage=R2Stage.NOT_STARTED))
        self._last_paused_line: Optional[int] = None; self._last_paused_function: Optional[str] = None
        self._retained_diagnosis: Optional[str] = None; self._diagnosis_provenance: Optional[dict[str, Any]] = None
        self._runtime_slice: dict[str, str] = {}; self._g1: Optional[int] = None; self._g2: Optional[int] = None
        self._patch_attempted = False
        self._patch_attempts: list[dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def retained_diagnosis(self) -> Optional[str]:
        return self._retained_diagnosis

    @property
    def diagnosis_provenance(self) -> Optional[dict[str, Any]]:
        return self._diagnosis_provenance

    @property
    def patch_attempted(self) -> bool:
        return self._patch_attempted

    @property
    def patch_attempts(self) -> list[dict[str, Any]]:
        return list(self._patch_attempts)

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def _render_observation(self, obs: Optional[Observation]) -> str:
        return bridge._render_observation(
            obs,
            filter_scripts=frozenset({self._script_path}) if self._script_path else None,
            original_line_count=self._original_line_count or None,
        )

    def next_directive(self, snapshot: ControllerSnapshot) -> ModelDirective:
        if self._index >= len(self._steps):
            raise ModelAdapterError("scripted bridge steps exhausted")
        raw_text = self._steps[self._index]; self._index += 1
        state = snapshot.state; last_obs = snapshot.last_observation
        self._stage_tracker.update_from_observation(last_obs); self._capture_slice(last_obs); self._update_paused_tracking(last_obs)
        prior_obs_id = last_obs.observation_id if last_obs else None
        prior_obs_sha = _sha256(_canonical_json(last_obs.to_mapping())) if last_obs is not None else None
        rendered_obs_sha = self._compute_rendered_obs_hash(last_obs)
        rendered_diag_sha = _sha256(self._retained_diagnosis) if self._retained_diagnosis else None
        session_state = self._session_state_provider()
        if session_state.lifecycle is DebuggerLifecycle.PAUSED or session_state.r2_stage is bridge.R2Stage.PAUSED_AFTER_TERMINAL_STEP:
            session_state = SessionState(lifecycle=session_state.lifecycle, r2_stage=session_state.r2_stage, paused_line=self._last_paused_line, paused_function=self._last_paused_function)
        if state.value == "Patch" and self._retained_diagnosis is not None:
            debugger_ctx = DebuggerContext(script_path=self._script_path, source_text=self._source_text, eligible_lines=self._eligible_lines, lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED, r2_stage=R2Stage.CONSUMED_OR_ENDED, retained_diagnosis=self._retained_diagnosis, runtime_slice=dict(self._runtime_slice) if self._runtime_slice else None)
        else:
            debugger_ctx = DebuggerContext(script_path=self._script_path, source_text=self._source_text, eligible_lines=self._eligible_lines, lifecycle=session_state.lifecycle, r2_stage=session_state.r2_stage, paused_line=session_state.paused_line, paused_function=session_state.paused_function, runtime_slice=dict(self._runtime_slice) if (session_state.r2_stage is bridge.R2Stage.PAUSED_AFTER_TERMINAL_STEP and self._runtime_slice) else None)
        # R3.1 PATCH stage
        patch_stage: Optional[bridge.R3PatchStage] = None
        if state.value == "Patch":
            patch_stage = bridge.R3PatchStage.RETRY if self._patch_attempted else bridge.R3PatchStage.NEEDS_FIRST_REPAIR
        user_prompt = bridge.render_prompt(state=state, last_observation=last_obs, task_description=self._task_description, feedback=None, debugger=debugger_ctx, patch_stage=patch_stage)
        record = TelemetryRecord(model_call_index=snapshot.model_call_index, transport_attempt_index=1, controller_state=state.value, system_prompt_sha256=_sha256(self._system_prompt), user_prompt_sha256=_sha256(user_prompt), user_prompt_summary=_bound_text(user_prompt, 1000), raw_response_text=raw_text, raw_response_status="decoded", raw_response_bytes=len(raw_text.encode("utf-8")), parse_status="not_attempted", prior_observation_id=prior_obs_id, prior_observation_sha256=prior_obs_sha, rendered_observation_sha256=rendered_obs_sha, rendered_diagnosis_sha256=rendered_diag_sha)
        self._telemetry.append(record)
        try:
            parse_text, fence_unwrap = bridge.unwrap_single_fence(raw_text)
            record.fence_unwrap = (
                fence_unwrap.to_mapping() if fence_unwrap is not None else None
            )
            result = bridge.parse(parse_text, state, last_obs, lifecycle=session_state.lifecycle, r2_stage=session_state.r2_stage, patch_stage=patch_stage)
            whole_file_meta: Optional[dict[str, Any]] = None
            if hasattr(result.directive, "name") and getattr(result.directive.name, "value", None) == "apply_patch":
                args = result.directive.arguments
                if "whole_file_path" in args:
                    b_diff = serialize_whole_file_to_diff(
                        self._script_path, self._source_text,
                        args["whole_file_path"], args["whole_file_content"],
                    )
                    whole_file_meta = {
                        "path": args["whole_file_path"],
                        "model_whole_file_sha256": _sha256(args["whole_file_content"]),
                        "generated_diff_sha256": _sha256(b_diff),
                    }
                    result = bridge.BridgeResult(
                        command_token="file",
                        normalized_command=f"file {args['whole_file_path']}",
                        directive=ActionDirective(result.directive.name, {"patch": b_diff}),
                        is_diagnosis=result.is_diagnosis,
                        diagnosis_text=result.diagnosis_text,
                    )
                b_diff = result.directive.arguments["patch"]
                try:
                    c_diff, norm_record = normalize_hunk_counts(b_diff)
                except SerializationNormalizationError as exc:
                    raise bridge.BridgeParseError(bridge.BridgeRejection.INVALID_PATCH, f"patch serialization normalization failed: {exc}") from exc
                result = bridge.BridgeResult(command_token=result.command_token, normalized_command=result.normalized_command, directive=ActionDirective(result.directive.name, {"patch": c_diff}), is_diagnosis=result.is_diagnosis, diagnosis_text=result.diagnosis_text)
        except bridge.BridgeParseError as exc:
            record.parse_status = "rejected"; record.rejection_category = exc.category.value; record.rejection_message = exc.detail
            raise ModelAdapterError(f"scripted bridge parse failed: {exc.category.value}: {exc.detail}") from exc
        record.parse_status = "accepted"; record.command_token = result.command_token; record.normalized_command = result.normalized_command
        directive = result.directive
        if hasattr(directive, "name"):
            record.directive_kind = "action"; record.action_name = directive.name.value; record.directive_arguments = dict(directive.arguments)
            if record.action_name == "apply_patch":
                record.model_patch_raw_sha256 = norm_record.model_patch_raw_sha256
                record.model_patch_serialization_normalized_sha256 = norm_record.model_patch_serialization_normalized_sha256
                attempt: dict[str, Any] = {
                    "model_call_index": snapshot.model_call_index,
                    "raw_model_response_sha256": _sha256(raw_text) if raw_text != NOT_AVAILABLE else None,
                    "model_patch_raw_sha256": norm_record.model_patch_raw_sha256,
                    "model_patch_serialization_normalized_sha256": norm_record.model_patch_serialization_normalized_sha256,
                    "normalization": norm_record.to_mapping(),
                    "fence_unwrap": record.fence_unwrap,
                }
                if whole_file_meta is not None:
                    attempt["representation"] = "whole_file"
                    attempt["whole_file"] = whole_file_meta
                else:
                    attempt["representation"] = "unified_diff"
                self._patch_attempts.append(attempt)
        elif hasattr(directive, "target_state"):
            record.directive_kind = "transition"; record.target_state = directive.target_state.value; record.directive_reason = directive.reason
        record.is_diagnosis = result.is_diagnosis; record.diagnosis_text = result.diagnosis_text
        if state.value == "Patch" and result.command_token == "patch" and not self._patch_attempted:
            self._patch_attempted = True
        if result.is_diagnosis and result.diagnosis_text:
            if self._retained_diagnosis is None:
                self._retained_diagnosis = result.diagnosis_text
                self._diagnosis_provenance = {"model_call_index": snapshot.model_call_index, "prior_observation_id": prior_obs_id, "rendered_observation_sha256": rendered_obs_sha, "G1": self._g1, "G2": self._g2, "diagnosis_text_sha256": _sha256(result.diagnosis_text)}
            self._post_debug_diagnoses.append({"text": result.diagnosis_text, "model_call_index": snapshot.model_call_index, "controller_state": state.value, "raw_response_text": _bound_text(raw_text), "provenance": "model-authored, bound to model_call_index " + str(snapshot.model_call_index)})
        return directive

    def _capture_slice(self, obs: Optional[Observation]) -> None:
        if obs is None or not _is_ok(obs):
            return
        payload = obs.payload if isinstance(obs.payload, dict) else {}
        name = obs.name
        # R5.2: the real reproduction failure output is part of the bounded
        # runtime evidence slice (surfaced again at PATCH time).
        if name == "run_reproduction" and "reproduction" not in self._runtime_slice:
            if payload.get("failure_reproduced") is True:
                failure_output = payload.get("failure_output")
                if type(failure_output) is str and failure_output:
                    self._runtime_slice["reproduction"] = self._render_observation(obs)
                    summary = bridge.crash_summary_from_failure_output(
                        failure_output, self._script_path
                    )
                    if summary:
                        self._runtime_slice["crash_summary"] = summary
                    return
        if name == "get_stack_summary" and self._g1 is None:
            gen = payload.get("pause_generation")
            if type(gen) is int and gen > 0 and "stack_G1" not in self._runtime_slice:
                self._runtime_slice["stack_G1"] = self._render_observation(obs); self._g1 = gen; return
        if name in ("get_frame_locals", "safe_eval_expression") and "inspection" not in self._runtime_slice:
            gen = payload.get("pause_generation")
            if self._g1 is not None and gen != self._g1:
                return
            self._runtime_slice["inspection"] = self._render_observation(obs)
            if self._g1 is None and type(gen) is int:
                self._g1 = gen
            return
        if name in ("step_pdb_session", "next_pdb_session") and "step" not in self._runtime_slice:
            state_val = payload.get("state")
            if state_val in ("paused", "exited", "failed", "terminated"):
                self._runtime_slice["step"] = self._render_observation(obs); return
        if name == "get_stack_summary" and self._g1 is not None and "stack_G2" not in self._runtime_slice:
            gen = payload.get("pause_generation")
            if type(gen) is int and gen > self._g1:
                self._runtime_slice["stack_G2"] = self._render_observation(obs); self._g2 = gen; return

    def _update_paused_tracking(self, last_obs: Optional[Observation]) -> None:
        if last_obs is None:
            return
        name = last_obs.name; payload = last_obs.payload
        if type(payload) is not dict:
            return
        status = last_obs.status.value if hasattr(last_obs.status, "value") else str(last_obs.status)
        if status not in ("ok", "completed"):
            return
        if name == "start_pdb_session":
            self._last_paused_line = payload.get("line"); self._last_paused_function = payload.get("function")
        elif name in ("continue_pdb_session", "step_pdb_session", "next_pdb_session"):
            state_val = payload.get("state")
            if state_val == "paused":
                self._last_paused_line = payload.get("line"); self._last_paused_function = payload.get("function")
            elif state_val in ("exited", "failed", "terminated"):
                # R5.2 terminal progression: retain the last paused location
                # so the terminal stage can name where the target crashed.
                pass
            else:
                self._last_paused_line = None; self._last_paused_function = None

    @property
    def telemetry(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._telemetry]

    @property
    def post_debug_diagnoses(self) -> list[dict[str, Any]]:
        return list(self._post_debug_diagnoses)

    def _compute_rendered_obs_hash(self, last_obs: Optional[Observation]) -> Optional[str]:
        if last_obs is None:
            return None
        rendered = self._render_observation(last_obs)
        return _sha256(rendered) if rendered else None


__all__ = [
    "R5DebuggerBridgeAdapter",
    "R5StageTracker",
    "ScriptedBridgeAdapter",
    "ModelTransport",
    "TransportError",
    "TransportResponse",
    "TelemetryRecord",
    "SessionState",
    "SessionStateProvider",
    "make_r5_session_state_provider",
    "NOT_RECORDED",
    "NOT_AVAILABLE",
]
