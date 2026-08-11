"""R1 — repaired model adapter with debugger lifecycle + source affordance.

This is the R1 treatment revision of the S1 ``DebuggerBridgeAdapter``.  It
adds:

1. A ``session_state_provider`` callback that queries the actual
   ``DemoToolContext`` PDB state (``pdb_session``, ``pdb_session_started``,
   ``pdb_pause_generation``, paused line/function) to build the
   ``DebuggerContext`` for prompt rendering.

2. The original fixture source and breakpoint-eligible lines passed to the
   bridge so the model sees the target source at the ``break`` decision
   point.

The adapter implements the same ``ModelAdapter`` Protocol as S1:
``model_name`` property + ``next_directive(snapshot) -> ModelDirective``.
On parse failure it retries once (same budget as S1).  On exhaustion it
raises ``ModelAdapterError`` → controller ``MODEL_ERROR``.

Provenance binding is identical to S1.
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

from experiments.debugger_interaction_v2_r1 import bridge
from experiments.debugger_interaction_v2_r1.bridge import (
    DebuggerContext,
    DebuggerLifecycle,
)


# ---------------------------------------------------------------------------
# Transport protocol (reused from S1)
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
# Session state provider (queries the actual DemoToolContext)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionState:
    """A snapshot of the PDB session state at prompt-rendering time."""

    lifecycle: DebuggerLifecycle
    paused_line: Optional[int] = None
    paused_function: Optional[str] = None


class SessionStateProvider(Protocol):
    """Callable that returns the current PDB session state."""

    def __call__(self) -> SessionState:
        ...


def make_session_state_provider(context: Any) -> Callable[[], SessionState]:
    """Build a session-state provider from a ``DemoToolContext``.

    The lifecycle is derived from the **actual** context state, using
    ``pdb_session_started`` as well as ``pdb_session`` to model the
    one-session-per-case guard:

    - ``pdb_session is not None`` → ``PAUSED`` (the session is active).
    - ``pdb_session is None and pdb_session_started`` → ``CONSUMED_OR_ENDED``
      (one-session guard consumed; ``break`` is NOT legal even though
      ``pdb_session is None``).
    - ``pdb_session is None and not pdb_session_started`` → ``NOT_STARTED``.

    The paused line/function are NOT read here — the adapter overrides
    them from its own observation tracking (more reliable than the
    provider's closure capture).
    """

    def provider() -> SessionState:
        pdb_session = getattr(context, "pdb_session", None)
        started = getattr(context, "pdb_session_started", False)

        if pdb_session is not None:
            return SessionState(lifecycle=DebuggerLifecycle.PAUSED)
        if started:
            return SessionState(lifecycle=DebuggerLifecycle.CONSUMED_OR_ENDED)
        return SessionState(lifecycle=DebuggerLifecycle.NOT_STARTED)

    return provider


# ---------------------------------------------------------------------------
# Telemetry (identical structure to S1)
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
# The R1 adapter
# ---------------------------------------------------------------------------


class R1DebuggerBridgeAdapter:
    """The R1 model adapter.

    Plugs into the unchanged ``DeterministicController`` via the
    ``ModelAdapter`` Protocol.  Each turn it:
    1. queries the session state from the provider;
    2. builds the ``DebuggerContext``;
    3. renders the R1 prompt (with source + lifecycle + eligible lines);
    4. calls the transport;
    5. parses through the R1 bridge;
    6. records telemetry with provenance;
    7. retries once on parse rejection.
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
        # Track the last paused line/function from observations for the
        # session state provider.
        self._last_paused_line: Optional[int] = None
        self._last_paused_function: Optional[str] = None

    # -- ModelAdapter Protocol --------------------------------------------

    @property
    def model_name(self) -> str:
        return self._model_name

    def next_directive(
        self,
        snapshot: ControllerSnapshot,
    ) -> ModelDirective:
        state = snapshot.state
        last_obs = snapshot.last_observation

        # Update paused line/function tracking from the last observation.
        self._update_paused_tracking(last_obs)

        prior_obs_id = None
        prior_obs_sha = None
        rendered_obs_sha = None
        if last_obs is not None:
            prior_obs_id = last_obs.observation_id
            prior_obs_payload = _canonical_json(last_obs.to_mapping())
            prior_obs_sha = _sha256(prior_obs_payload)

        # Query the actual session state.
        session_state = self._session_state_provider()
        # Override the paused line/function with our tracked values (more
        # reliable than the provider's closure capture).
        if session_state.lifecycle is DebuggerLifecycle.PAUSED:
            session_state = SessionState(
                lifecycle=session_state.lifecycle,
                paused_line=self._last_paused_line,
                paused_function=self._last_paused_function,
            )

        debugger_ctx = DebuggerContext(
            script_path=self._script_path,
            source_text=self._source_text,
            eligible_lines=self._eligible_lines,
            lifecycle=session_state.lifecycle,
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
                    raw_text, state, last_obs, session_state.lifecycle
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

    # -- Public accessors -------------------------------------------------

    @property
    def telemetry(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._telemetry]

    @property
    def post_debug_diagnoses(self) -> list[dict[str, Any]]:
        return list(self._post_debug_diagnoses)

    # -- Internal helpers -------------------------------------------------

    def _update_paused_tracking(
        self, last_obs: Optional[Observation],
    ) -> None:
        """Track the paused line/function from the last PDB observation."""

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
# ScriptedBridgeAdapter — deterministic test double (identical to S1)
# ---------------------------------------------------------------------------


class ScriptedBridgeAdapter:
    """A deterministic adapter that feeds pre-written command strings through
    the real R1 bridge parser.  For the PDB integration test."""

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
        self._session_state_provider = session_state_provider or (
            lambda: SessionState(lifecycle=DebuggerLifecycle.NOT_STARTED)
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
        self._update_paused_tracking(last_obs)

        prior_obs_id = last_obs.observation_id if last_obs else None
        prior_obs_sha = None
        rendered_obs_sha = None
        if last_obs is not None:
            prior_obs_sha = _sha256(_canonical_json(last_obs.to_mapping()))
        rendered_obs_sha = R1DebuggerBridgeAdapter._compute_rendered_obs_hash(last_obs)

        session_state = self._session_state_provider()
        if session_state.lifecycle is DebuggerLifecycle.PAUSED:
            session_state = SessionState(
                lifecycle=session_state.lifecycle,
                paused_line=self._last_paused_line,
                paused_function=self._last_paused_function,
            )

        debugger_ctx = DebuggerContext(
            script_path=self._script_path,
            source_text=self._source_text,
            eligible_lines=self._eligible_lines,
            lifecycle=session_state.lifecycle,
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
                    raw_text, state, last_obs, session_state.lifecycle
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
    "R1DebuggerBridgeAdapter",
    "ScriptedBridgeAdapter",
    "ModelTransport",
    "TransportError",
    "TransportResponse",
    "TelemetryRecord",
    "SessionState",
    "SessionStateProvider",
    "make_session_state_provider",
    "NOT_RECORDED",
    "NOT_AVAILABLE",
]