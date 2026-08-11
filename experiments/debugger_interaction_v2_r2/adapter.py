"""R2 — staged model adapter with public lifecycle + idempotent stage tracking.

The adapter:

1. Queries the actual PDB session state via the public
   ``PdbSession.get_target_status()`` API (never the private
   ``_target_lifecycle_state`` field).  A non-null session whose target
   state is exited/failed/terminated is never rendered as PAUSED.

2. Tracks an experiment-local R2 stage from verified status=ok controller
   observations.  The same observation never advances the stage twice
   (last_processed_observation_id).

3. Renders staged prompts via ``bridge.render_prompt`` with ``R2Stage``.

Control-plane lifecycle status queries are NOT model-authored debugger
actions and are never presented as model debugger evidence.

Provenance binding is identical to R1.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from agentic_debugger.agent.model_adapter import (
    ModelAdapterError,
    ModelDirective,
    ControllerSnapshot,
)
from agentic_debugger.events.schema import Observation

from experiments.debugger_interaction_v2_r2 import bridge
from experiments.debugger_interaction_v2_r2.bridge import (
    DebuggerContext,
    DebuggerLifecycle,
    R2Stage,
)


# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------


class TransportError(Exception):
    def __init__(self, category: str, detail: str = "") -> None:
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail


class TransportResponse:
    def __init__(
        self,
        raw_text: str,
        usage: Optional[dict[str, Any]] = None,
    ) -> None:
        self.raw_text = raw_text
        self.usage = usage


class ModelTransport(Protocol):
    def request(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> TransportResponse:
        ...


# ---------------------------------------------------------------------------
# Session state (R2 — public API based)
# ---------------------------------------------------------------------------


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


def make_r2_session_state_provider(
    context: Any,
    get_r2_stage: Callable[[], R2Stage],
) -> Callable[[], SessionState]:
    """Build an R2 session-state provider from ``context``.

    Authority is the verified observation chain, not a live status probe.

    * ``pdb_session is None and not started`` → NOT_STARTED
    * ``pdb_session is None and started`` → CONSUMED_OR_ENDED
    * ``pdb_session is not None`` + stage after step (PAUSED_AFTER_STEP)
        → PAUSED (step/next is valid paused evidence; get_target_status
          cannot validate the stepped line against the original breakpoint)
    * ``pdb_session is not None`` + normal paused stages → try the public
        ``PdbSession.get_target_status()`` API; on success delegate to stage,
        on failure fail-closed to CONSUMED (except when already past step).
    """

    def provider() -> SessionState:
        pdb_session = getattr(context, "pdb_session", None)
        started = getattr(context, "pdb_session_started", False)

        if pdb_session is None and not started:
            return SessionState(
                lifecycle=DebuggerLifecycle.NOT_STARTED,
                r2_stage=R2Stage.NOT_STARTED,
            )

        if pdb_session is None and started:
            return SessionState(
                lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED,
                r2_stage=R2Stage.CONSUMED_OR_ENDED,
            )

        # pdb_session is not None — derive from stage + public status
        stage = get_r2_stage()

        # After step/next the worker's get_target_status() will fail
        # (line 3 not in active_breakpoints [2]) and transition the
        # session to FAILED.  Observation-derived paused evidence is the
        # true authority here.  Trust the stage after step.
        if stage in (R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK, R2Stage.READY_FOR_DIAGNOSIS):
            return SessionState(
                lifecycle=DebuggerLifecycle.PAUSED,
                r2_stage=stage,
            )

        # Before step: trust public get_target_status() when it succeeds.
        try:
            result = pdb_session.get_target_status()
        except Exception as exc:
            diag = f"get_target_status failed: {type(exc).__name__}"
            return SessionState(
                lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED,
                r2_stage=R2Stage.CONSUMED_OR_ENDED,
                status_diagnostic=diag,
            )
        state_val = result.get("state") if isinstance(result, dict) else None
        if state_val == "paused":
            effective = stage
            if effective is R2Stage.NOT_STARTED:
                effective = R2Stage.PAUSED_NEEDS_STACK
            return SessionState(
                lifecycle=DebuggerLifecycle.PAUSED,
                r2_stage=effective,
            )
        if state_val in ("exited", "failed", "terminated"):
            return SessionState(
                lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED,
                r2_stage=R2Stage.CONSUMED_OR_ENDED,
            )
        return SessionState(
            lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED,
            r2_stage=R2Stage.CONSUMED_OR_ENDED,
        )

    return provider


# Backward compat for callers that used the R1 name
make_session_state_provider = make_r2_session_state_provider


# ---------------------------------------------------------------------------
# Telemetry (identical structure to R1)
# ---------------------------------------------------------------------------

NOT_RECORDED = "NOT_RECORDED"
NOT_AVAILABLE = "NOT_AVAILABLE"
MAX_RAW_TEXT_BYTES = 65536


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bound_text(text: str, limit: int = MAX_RAW_TEXT_BYTES) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return text[: limit - 3] + "..."


def _extract_usage(usage: Optional[dict[str, Any]]) -> dict[str, Any]:
    if usage is None or type(usage) is not dict:
        return {
            "prompt_tokens": NOT_RECORDED,
            "completion_tokens": NOT_RECORDED,
            "total_tokens": NOT_RECORDED,
            "provider_reported": False,
        }
    result: dict[str, Any] = {"provider_reported": True}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = usage.get(key)
        if type(val) is int and val >= 0:
            result[key] = val
        else:
            result[key] = NOT_RECORDED
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
            "request": {
                "system_prompt_sha256": self.system_prompt_sha256,
                "user_prompt_sha256": self.user_prompt_sha256,
                "user_prompt_summary": self.user_prompt_summary,
            },
            "raw_response_text": self.raw_response_text,
            "raw_response_status": self.raw_response_status,
            "raw_response_bytes": self.raw_response_bytes,
            "transport_error_category": self.transport_error_category,
            "parse_result": {
                "status": self.parse_status,
                "command_token": self.command_token,
                "normalized_command": self.normalized_command,
                "rejection_category": self.rejection_category,
                "rejection_message": self.rejection_message,
            },
            "translated_directive": {
                "kind": self.directive_kind,
                "action_name": self.action_name,
                "arguments": self.directive_arguments,
                "target_state": self.target_state,
                "reason": self.directive_reason,
                "is_diagnosis": self.is_diagnosis,
                "diagnosis_text": self.diagnosis_text,
            },
            "provenance": {
                "prior_observation_id": self.prior_observation_id,
                "prior_observation_sha256": self.prior_observation_sha256,
                "rendered_observation_sha256": self.rendered_observation_sha256,
            },
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "provider_reported": self.provider_reported,
            },
            "timing": {
                "request_duration_ms": self.request_duration_ms,
                "parse_duration_ms": self.parse_duration_ms,
            },
        }


# ---------------------------------------------------------------------------
# R2 stage tracker — experiment-local, idempotent
# ---------------------------------------------------------------------------


_R2_STAGE_ORDER: tuple[R2Stage, ...] = (
    R2Stage.NOT_STARTED,
    R2Stage.PAUSED_NEEDS_STACK,
    R2Stage.PAUSED_NEEDS_INSPECTION,
    R2Stage.PAUSED_NEEDS_STEP,
    R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK,
    R2Stage.READY_FOR_DIAGNOSIS,
)


def _is_ok(observation: Optional[Observation]) -> bool:
    if observation is None:
        return False
    status = observation.status.value if hasattr(observation.status, "value") else str(observation.status)
    return status in ("ok", "completed")


class R2StageTracker:
    """Experiment-local, fail-closed, idempotent R2 stage advancement.

    Only ``status=ok`` observations of the expected action advance the stage.
    The same ``observation_id`` never advances twice.
    """

    def __init__(self) -> None:
        self._stage = R2Stage.NOT_STARTED
        self._last_processed_id: Optional[str] = None

    @property
    def stage(self) -> R2Stage:
        return self._stage

    def update_from_observation(self, obs: Optional[Observation]) -> None:
        if obs is None:
            return
        if not _is_ok(obs):
            return
        oid = obs.observation_id
        if oid == self._last_processed_id:
            return
        payload = obs.payload if isinstance(obs.payload, dict) else {}
        name = obs.name
        state_val = payload.get("state")

        advanced = False
        if self._stage is R2Stage.NOT_STARTED and name == "start_pdb_session" and state_val == "paused":
            self._stage = R2Stage.PAUSED_NEEDS_STACK
            advanced = True
        elif self._stage is R2Stage.PAUSED_NEEDS_STACK and name == "get_stack_summary":
            self._stage = R2Stage.PAUSED_NEEDS_INSPECTION
            advanced = True
        elif self._stage is R2Stage.PAUSED_NEEDS_INSPECTION and name in ("get_frame_locals", "safe_eval_expression"):
            self._stage = R2Stage.PAUSED_NEEDS_STEP
            advanced = True
        elif self._stage is R2Stage.PAUSED_NEEDS_STEP and name in ("step_pdb_session", "next_pdb_session") and state_val == "paused":
            self._stage = R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK
            advanced = True
        elif self._stage is R2Stage.PAUSED_AFTER_STEP_NEEDS_STACK and name == "get_stack_summary":
            self._stage = R2Stage.READY_FOR_DIAGNOSIS
            advanced = True
        # READY_FOR_DIAGNOSIS is terminal — no further advance by observations
        # (diagnosis is a transition, not an observation)
        if advanced:
            self._last_processed_id = oid


# ---------------------------------------------------------------------------
# The R2 adapter
# ---------------------------------------------------------------------------


class R2DebuggerBridgeAdapter:
    """R2 staged model adapter.

    Implements the same ``ModelAdapter`` Protocol as R1.  Each turn it:
    1. advances the R2 stage from the last verified OK observation (idempotent);
    2. queries the session state via the public get_target_status() provider;
    3. renders the R2 staged prompt;
    4. calls the transport;  5. parses through the R2 bridge staged mask;
    6. records telemetry with provenance;  7. retries once on parse rejection.
    """

    def __init__(
        self,
        transport: ModelTransport,
        model_name: str,
        task_description: str,
        *,
        script_path: str,
        source_text: str,
        eligible_lines: tuple[int, ...],
        session_state_provider: Callable[[], SessionState],
        stage_tracker: R2StageTracker,
        max_retries: int = 1,
        request_timeout_seconds: float = 60.0,
    ) -> None:
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
        self._session_state_provider = session_state_provider
        self._stage_tracker = stage_tracker
        self._last_paused_line: Optional[int] = None
        self._last_paused_function: Optional[str] = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def next_directive(
        self,
        snapshot: ControllerSnapshot,
    ) -> ModelDirective:
        state = snapshot.state
        last_obs = snapshot.last_observation

        # Stage advancement — idempotent from verified OK observation
        self._stage_tracker.update_from_observation(last_obs)
        self._update_paused_tracking(last_obs)

        prior_obs_id = None
        prior_obs_sha = None
        if last_obs is not None:
            prior_obs_id = last_obs.observation_id
            prior_obs_payload = _canonical_json(last_obs.to_mapping())
            prior_obs_sha = _sha256(prior_obs_payload)

        session_state = self._session_state_provider()
        if session_state.lifecycle is DebuggerLifecycle.PAUSED:
            session_state = SessionState(
                lifecycle=session_state.lifecycle,
                r2_stage=session_state.r2_stage,
                paused_line=self._last_paused_line,
                paused_function=self._last_paused_function,
                status_diagnostic=session_state.status_diagnostic,
            )

        debugger_ctx = DebuggerContext(
            script_path=self._script_path,
            source_text=self._source_text,
            eligible_lines=self._eligible_lines,
            lifecycle=session_state.lifecycle,
            r2_stage=session_state.r2_stage,
            paused_line=session_state.paused_line,
            paused_function=session_state.paused_function,
        )

        feedback: Optional[str] = None

        for attempt in range(self._max_retries + 1):
            user_prompt = bridge.render_prompt(
                state=state,
                last_observation=last_obs,
                task_description=self._task_description,
                feedback=feedback,
                debugger=debugger_ctx,
            )
            sys_hash = _sha256(bridge.SYSTEM_PROMPT)
            user_hash = _sha256(user_prompt)
            rendered_obs_sha = self._compute_rendered_obs_hash(last_obs)

            raw_text: str
            raw_status: str
            transport_error_cat: Optional[str] = None
            raw_bytes: Any
            usage: dict[str, Any]
            req_start = time.monotonic()

            try:
                response = self._transport.request(
                    system_prompt=bridge.SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    timeout_seconds=self._request_timeout,
                )
                raw_text = response.raw_text
                raw_status = "decoded"
                raw_bytes = len(raw_text.encode("utf-8")) if type(raw_text) is str else NOT_AVAILABLE
                usage = _extract_usage(response.usage)
            except TransportError as exc:
                raw_text = NOT_AVAILABLE
                raw_status = "transport_failure"
                raw_bytes = NOT_AVAILABLE
                transport_error_cat = exc.category
                usage = _extract_usage(None)
                response = None  # type: ignore
            except Exception as exc:
                raw_text = NOT_AVAILABLE
                raw_status = "transport_failure"
                raw_bytes = NOT_AVAILABLE
                transport_error_cat = type(exc).__name__
                usage = _extract_usage(None)
                response = None  # type: ignore

            req_ms = int((time.monotonic() - req_start) * 1000)

            record = TelemetryRecord(
                model_call_index=snapshot.model_call_index,
                transport_attempt_index=attempt + 1,
                controller_state=state.value,
                system_prompt_sha256=sys_hash,
                user_prompt_sha256=user_hash,
                user_prompt_summary=_bound_text(user_prompt, 1000),
                raw_response_text=raw_text if raw_status == "decoded" else NOT_AVAILABLE,
                raw_response_status=raw_status,
                raw_response_bytes=raw_bytes,
                transport_error_category=transport_error_cat,
                parse_status="not_attempted",
                prompt_tokens=usage.get("prompt_tokens", NOT_RECORDED),
                completion_tokens=usage.get("completion_tokens", NOT_RECORDED),
                total_tokens=usage.get("total_tokens", NOT_RECORDED),
                provider_reported=usage.get("provider_reported", False),
                request_duration_ms=req_ms,
                prior_observation_id=prior_obs_id,
                prior_observation_sha256=prior_obs_sha,
                rendered_observation_sha256=rendered_obs_sha,
            )
            self._telemetry.append(record)

            if raw_status == "transport_failure":
                if attempt < self._max_retries:
                    feedback = f"transport failure ({transport_error_cat}); retry"
                    continue
                raise ModelAdapterError(
                    f"transport failed after {attempt + 1} attempts: "
                    f"{transport_error_cat}"
                )

            parse_start = time.monotonic()
            try:
                result = bridge.parse(
                    raw_text, state, last_obs,
                    lifecycle=session_state.lifecycle,
                    r2_stage=session_state.r2_stage,
                )
            except bridge.BridgeParseError as exc:
                parse_ms = int((time.monotonic() - parse_start) * 1000)
                record.parse_status = "rejected"
                record.rejection_category = exc.category.value
                record.rejection_message = exc.detail
                record.parse_duration_ms = parse_ms
                if attempt < self._max_retries:
                    feedback = f"{exc.category.value}: {exc.detail}"
                    continue
                raise ModelAdapterError(
                    f"bridge parse failed after {attempt + 1} attempts: "
                    f"{exc.category.value}: {exc.detail}"
                ) from exc

            parse_ms = int((time.monotonic() - parse_start) * 1000)
            record.parse_status = "accepted"
            record.command_token = result.command_token
            record.normalized_command = result.normalized_command
            record.parse_duration_ms = parse_ms

            directive = result.directive
            if hasattr(directive, "name"):
                record.directive_kind = "action"
                record.action_name = directive.name.value
                record.directive_arguments = dict(directive.arguments)
            elif hasattr(directive, "target_state"):
                record.directive_kind = "transition"
                record.target_state = directive.target_state.value
                record.directive_reason = directive.reason
            record.is_diagnosis = result.is_diagnosis
            record.diagnosis_text = result.diagnosis_text

            if result.is_diagnosis and result.diagnosis_text:
                self._post_debug_diagnoses.append({
                    "text": result.diagnosis_text,
                    "model_call_index": snapshot.model_call_index,
                    "controller_state": state.value,
                    "raw_response_text": _bound_text(raw_text),
                    "provenance": "model-authored, bound to model_call_index "
                                  f"{snapshot.model_call_index}",
                })

            return directive

        raise ModelAdapterError("adapter exhausted retries without resolution")

    @property
    def telemetry(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._telemetry]

    @property
    def post_debug_diagnoses(self) -> list[dict[str, Any]]:
        return list(self._post_debug_diagnoses)

    def _update_paused_tracking(
        self, last_obs: Optional[Observation],
    ) -> None:
        if last_obs is None:
            return
        name = last_obs.name
        payload = last_obs.payload
        if type(payload) is not dict:
            return
        status = last_obs.status.value if hasattr(last_obs.status, "value") else str(last_obs.status)
        if status not in ("ok", "completed"):
            return
        if name == "start_pdb_session":
            self._last_paused_line = payload.get("line")
            self._last_paused_function = payload.get("function")
        elif name in ("continue_pdb_session", "step_pdb_session", "next_pdb_session"):
            state_val = payload.get("state")
            if state_val == "paused":
                self._last_paused_line = payload.get("line")
                self._last_paused_function = payload.get("function")
            else:
                self._last_paused_line = None
                self._last_paused_function = None

    @staticmethod
    def _compute_rendered_obs_hash(
        last_obs: Optional[Observation],
    ) -> Optional[str]:
        if last_obs is None:
            return None
        rendered = bridge._render_observation(last_obs)
        if not rendered:
            return None
        return _sha256(rendered)


# ---------------------------------------------------------------------------
# ScriptedBridgeAdapter — deterministic R2 test double
# ---------------------------------------------------------------------------


class ScriptedBridgeAdapter:
    """Deterministic R2 adapter that feeds pre-written strings through the R2 parser."""

    def __init__(
        self,
        steps: tuple[str, ...],
        model_name: str = "scripted-bridge",
        task_description: str = "test task",
        *,
        script_path: str = "",
        source_text: str = "",
        eligible_lines: tuple[int, ...] = (),
        session_state_provider: Optional[Callable[[], SessionState]] = None,
        stage_tracker: Optional[R2StageTracker] = None,
    ) -> None:
        self._steps = steps
        self._index = 0
        self._model_name = model_name
        self._task_description = task_description
        self._telemetry: list[TelemetryRecord] = []
        self._post_debug_diagnoses: list[dict[str, Any]] = []
        self._script_path = script_path
        self._source_text = source_text
        self._eligible_lines = eligible_lines
        self._stage_tracker = stage_tracker or R2StageTracker()
        self._session_state_provider = session_state_provider or (
            lambda: SessionState(lifecycle=DebuggerLifecycle.NOT_STARTED, r2_stage=R2Stage.NOT_STARTED)
        )
        self._last_paused_line: Optional[int] = None
        self._last_paused_function: Optional[str] = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def next_directive(
        self,
        snapshot: ControllerSnapshot,
    ) -> ModelDirective:
        if self._index >= len(self._steps):
            raise ModelAdapterError("scripted bridge steps exhausted")

        raw_text = self._steps[self._index]
        self._index += 1

        state = snapshot.state
        last_obs = snapshot.last_observation
        self._stage_tracker.update_from_observation(last_obs)
        self._update_paused_tracking(last_obs)

        prior_obs_id = last_obs.observation_id if last_obs else None
        prior_obs_sha = None
        rendered_obs_sha = None
        if last_obs is not None:
            prior_obs_sha = _sha256(_canonical_json(last_obs.to_mapping()))
        rendered_obs_sha = R2DebuggerBridgeAdapter._compute_rendered_obs_hash(last_obs)

        session_state = self._session_state_provider()
        if session_state.lifecycle is DebuggerLifecycle.PAUSED:
            session_state = SessionState(
                lifecycle=session_state.lifecycle,
                r2_stage=session_state.r2_stage,
                paused_line=self._last_paused_line,
                paused_function=self._last_paused_function,
            )

        debugger_ctx = DebuggerContext(
            script_path=self._script_path,
            source_text=self._source_text,
            eligible_lines=self._eligible_lines,
            lifecycle=session_state.lifecycle,
            r2_stage=session_state.r2_stage,
            paused_line=session_state.paused_line,
            paused_function=session_state.paused_function,
        )

        user_prompt = bridge.render_prompt(
            state=state,
            last_observation=last_obs,
            task_description=self._task_description,
            feedback=None,
            debugger=debugger_ctx,
        )

        record = TelemetryRecord(
            model_call_index=snapshot.model_call_index,
            transport_attempt_index=1,
            controller_state=state.value,
            system_prompt_sha256=_sha256(bridge.SYSTEM_PROMPT),
            user_prompt_sha256=_sha256(user_prompt),
            user_prompt_summary=_bound_text(user_prompt, 1000),
            raw_response_text=raw_text,
            raw_response_status="decoded",
            raw_response_bytes=len(raw_text.encode("utf-8")),
            parse_status="not_attempted",
            prior_observation_id=prior_obs_id,
            prior_observation_sha256=prior_obs_sha,
            rendered_observation_sha256=rendered_obs_sha,
        )
        self._telemetry.append(record)

        try:
            result = bridge.parse(
                    raw_text, state, last_obs,
                    lifecycle=session_state.lifecycle,
                    r2_stage=session_state.r2_stage,
                )
        except bridge.BridgeParseError as exc:
            record.parse_status = "rejected"
            record.rejection_category = exc.category.value
            record.rejection_message = exc.detail
            raise ModelAdapterError(
                f"scripted bridge parse failed: {exc.category.value}: {exc.detail}"
            ) from exc

        record.parse_status = "accepted"
        record.command_token = result.command_token
        record.normalized_command = result.normalized_command
        directive = result.directive
        if hasattr(directive, "name"):
            record.directive_kind = "action"
            record.action_name = directive.name.value
            record.directive_arguments = dict(directive.arguments)
        elif hasattr(directive, "target_state"):
            record.directive_kind = "transition"
            record.target_state = directive.target_state.value
            record.directive_reason = directive.reason
        record.is_diagnosis = result.is_diagnosis
        record.diagnosis_text = result.diagnosis_text

        if result.is_diagnosis and result.diagnosis_text:
            self._post_debug_diagnoses.append({
                "text": result.diagnosis_text,
                "model_call_index": snapshot.model_call_index,
                "controller_state": state.value,
                "raw_response_text": _bound_text(raw_text),
                "provenance": "model-authored, bound to model_call_index "
                              f"{snapshot.model_call_index}",
            })

        return directive

    def _update_paused_tracking(
        self, last_obs: Optional[Observation],
    ) -> None:
        if last_obs is None:
            return
        name = last_obs.name
        payload = last_obs.payload
        if type(payload) is not dict:
            return
        status = last_obs.status.value if hasattr(last_obs.status, "value") else str(last_obs.status)
        if status not in ("ok", "completed"):
            return
        if name == "start_pdb_session":
            self._last_paused_line = payload.get("line")
            self._last_paused_function = payload.get("function")
        elif name in ("continue_pdb_session", "step_pdb_session", "next_pdb_session"):
            state_val = payload.get("state")
            if state_val == "paused":
                self._last_paused_line = payload.get("line")
                self._last_paused_function = payload.get("function")
            else:
                self._last_paused_line = None
                self._last_paused_function = None

    @property
    def telemetry(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._telemetry]

    @property
    def post_debug_diagnoses(self) -> list[dict[str, Any]]:
        return list(self._post_debug_diagnoses)


__all__ = [
    "R2DebuggerBridgeAdapter",
    "R2StageTracker",
    "ScriptedBridgeAdapter",
    "ModelTransport",
    "TransportError",
    "TransportResponse",
    "TelemetryRecord",
    "SessionState",
    "SessionStateProvider",
    "make_r2_session_state_provider",
    "make_session_state_provider",
    "NOT_RECORDED",
    "NOT_AVAILABLE",
]
