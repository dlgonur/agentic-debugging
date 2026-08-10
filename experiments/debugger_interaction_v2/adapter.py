"""DebuggerBridgeAdapter — the S1 model adapter.

This adapter plugs into the existing ``DeterministicController`` via the
``ModelAdapter`` Protocol (``model_adapter.py:488``).  It:

1. formats a state-specific prompt from the ``ControllerSnapshot``;
2. calls the transport (real model or test double);
3. captures the raw decoded text **before** parsing (always retained);
4. parses through the deterministic bridge (``bridge.py``);
5. translates the parse result to a typed ``ModelDirective``;
6. records full telemetry per model call;
7. retries once on parse rejection (v1 budget: max_retries=1);
8. on exhaustion raises ``ModelAdapterError`` → controller ``MODEL_ERROR``.

On parse failure it does NOT fabricate a ``TransitionDirective(FAILED)``.
The model did not choose FAILED.  The rejection is preserved as an
interface/adapter rejection in telemetry, and the controller's existing
``MODEL_ERROR`` path handles it cleanly (controller.py:944).

Provenance binding (Amendment 2):
   The adapter binds the exact real PDB observation to the next rendered
   model request via ``prior_observation_id``, ``prior_observation_sha256``,
   and ``rendered_observation_sha256``.  This proves the model's next request
   actually contained the observation it is claimed to have received.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from agentic_debugger.agent.model_adapter import (
    ModelAdapterError,
    ModelDirective,
    ControllerSnapshot,
)
from agentic_debugger.events.schema import Observation

from experiments.debugger_interaction_v2 import bridge


# ---------------------------------------------------------------------------
# Transport protocol (experiment-local)
# ---------------------------------------------------------------------------


class TransportError(Exception):
    """Raised when the transport fails before any model text is produced."""

    def __init__(self, category: str, detail: str = "") -> None:
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail


class TransportResponse:
    """The envelope returned by a transport call.

    ``raw_text`` is the exact decoded model text — always present when the
    transport succeeded, even if the text is empty.  ``usage`` may be absent
    (reported as ``NOT_RECORDED``).
    """

    def __init__(
        self,
        raw_text: str,
        usage: Optional[dict[str, Any]] = None,
    ) -> None:
        self.raw_text = raw_text
        self.usage = usage


class ModelTransport(Protocol):
    """Experiment-local transport protocol (simpler than the production one)."""

    def request(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> TransportResponse:
        ...


# ---------------------------------------------------------------------------
# Telemetry record
# ---------------------------------------------------------------------------

NOT_RECORDED = "NOT_RECORDED"
NOT_AVAILABLE = "NOT_AVAILABLE"
MAX_RAW_TEXT_BYTES = 65536  # 64 KiB cap for telemetry retention


def _canonical_json(value: Any) -> str:
    """Serialise to canonical JSON (sorted keys, compact)."""

    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bound_text(text: str, limit: int = MAX_RAW_TEXT_BYTES) -> str:
    """Bound a string for telemetry retention."""

    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return text[: limit - 3] + "..."


def _extract_usage(usage: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Extract usage data, using NOT_RECORDED for missing fields."""

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
    """One per model call (including retries)."""

    model_call_index: int
    transport_attempt_index: int
    controller_state: str

    # Request
    system_prompt_sha256: str
    user_prompt_sha256: str
    user_prompt_summary: str

    # Raw response (ALWAYS retained when text exists)
    raw_response_text: str = NOT_AVAILABLE  # NOT_AVAILABLE if transport failed
    raw_response_status: str = "transport_failure"  # "decoded" | "transport_failure"
    raw_response_bytes: Any = NOT_AVAILABLE  # int or NOT_AVAILABLE
    transport_error_category: Optional[str] = None

    # Parse result
    parse_status: str = "not_attempted"  # "accepted" | "rejected" | "not_attempted"
    command_token: Optional[str] = None
    normalized_command: Optional[str] = None
    rejection_category: Optional[str] = None
    rejection_message: Optional[str] = None

    # Translated directive
    directive_kind: Optional[str] = None
    action_name: Optional[str] = None
    directive_arguments: Optional[dict[str, Any]] = None
    target_state: Optional[str] = None
    directive_reason: Optional[str] = None
    is_diagnosis: bool = False
    diagnosis_text: Optional[str] = None

    # Provenance binding (Amendment 2)
    prior_observation_id: Optional[str] = None
    prior_observation_sha256: Optional[str] = None
    rendered_observation_sha256: Optional[str] = None

    # Usage
    prompt_tokens: Any = NOT_RECORDED
    completion_tokens: Any = NOT_RECORDED
    total_tokens: Any = NOT_RECORDED
    provider_reported: bool = False

    # Timing
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
# The adapter
# ---------------------------------------------------------------------------


class DebuggerBridgeAdapter:
    """The S1 model adapter.

    Implements the ``ModelAdapter`` Protocol: ``model_name`` property +
    ``next_directive(snapshot) -> ModelDirective``.  The controller calls
    ``next_directive`` each turn; this adapter formats the state-specific
    prompt, calls the transport, parses the response through the bridge,
    records telemetry, and returns the typed directive.

    On parse failure the adapter retries once (v1 budget:
    ``model_retries_per_logical_call_max=1``).  On exhaustion it raises
    ``ModelAdapterError``, which the controller maps to ``MODEL_ERROR``
    (controller.py:944).  No directive is fabricated.
    """

    def __init__(
        self,
        transport: ModelTransport,
        model_name: str,
        task_description: str,
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

    # -- ModelAdapter Protocol --------------------------------------------

    @property
    def model_name(self) -> str:
        return self._model_name

    def next_directive(
        self,
        snapshot: ControllerSnapshot,
    ) -> ModelDirective:
        """Format prompt, call transport, parse, record telemetry, return directive."""

        state = snapshot.state
        last_obs = snapshot.last_observation

        # Compute observation provenance BEFORE rendering the prompt.
        prior_obs_id = None
        prior_obs_sha = None
        rendered_obs_sha = None
        if last_obs is not None:
            prior_obs_id = last_obs.observation_id
            prior_obs_payload = _canonical_json(last_obs.to_mapping())
            prior_obs_sha = _sha256(prior_obs_payload)

        feedback: Optional[str] = None

        for attempt in range(self._max_retries + 1):
            # -- Render prompt ------------------------------------------------
            user_prompt = bridge.render_prompt(
                state=state,
                last_observation=last_obs,
                task_description=self._task_description,
                feedback=feedback,
            )
            sys_hash = _sha256(bridge.SYSTEM_PROMPT)
            user_hash = _sha256(user_prompt)

            # Compute rendered observation hash (the observation text that
            # was actually included in this request).
            rendered_obs_sha = self._compute_rendered_obs_hash(last_obs)

            # -- Call transport -----------------------------------------------
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

            # -- Record telemetry (BEFORE parsing) ---------------------------
            record = TelemetryRecord(
                model_call_index=snapshot.model_call_index,
                transport_attempt_index=attempt + 1,
                controller_state=state.value,
                system_prompt_sha256=sys_hash,
                user_prompt_sha256=user_hash,
                user_prompt_summary=_bound_text(user_prompt, 500),
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

            # -- Transport failure path ---------------------------------------
            if raw_status == "transport_failure":
                # No text was produced — do not attempt to parse.
                # Retry if budget remains, else raise ModelAdapterError.
                if attempt < self._max_retries:
                    feedback = f"transport failure ({transport_error_cat}); retry"
                    continue
                raise ModelAdapterError(
                    f"transport failed after {attempt + 1} attempts: "
                    f"{transport_error_cat}"
                )

            # -- Parse through the bridge ------------------------------------
            parse_start = time.monotonic()
            try:
                result = bridge.parse(raw_text, state, last_obs)
            except bridge.BridgeParseError as exc:
                parse_ms = int((time.monotonic() - parse_start) * 1000)
                record.parse_status = "rejected"
                record.rejection_category = exc.category.value
                record.rejection_message = exc.detail
                record.parse_duration_ms = parse_ms
                # Raw text is ALREADY retained in the record (above).
                # Retry if budget remains, else raise ModelAdapterError.
                if attempt < self._max_retries:
                    feedback = f"{exc.category.value}: {exc.detail}"
                    continue
                raise ModelAdapterError(
                    f"bridge parse failed after {attempt + 1} attempts: "
                    f"{exc.category.value}: {exc.detail}"
                ) from exc

            parse_ms = int((time.monotonic() - parse_start) * 1000)

            # -- Success: fill in the record ---------------------------------
            record.parse_status = "accepted"
            record.command_token = result.command_token
            record.normalized_command = result.normalized_command
            record.parse_duration_ms = parse_ms

            directive = result.directive
            # Extract directive metadata for telemetry.
            if hasattr(directive, "name"):
                # ActionDirective
                record.directive_kind = "action"
                record.action_name = directive.name.value
                record.directive_arguments = dict(directive.arguments)
            elif hasattr(directive, "target_state"):
                # TransitionDirective
                record.directive_kind = "transition"
                record.target_state = directive.target_state.value
                record.directive_reason = directive.reason
            record.is_diagnosis = result.is_diagnosis
            record.diagnosis_text = result.diagnosis_text

            # Record post-debug diagnosis if applicable.
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

        # Should not reach here, but fail closed.
        raise ModelAdapterError("adapter exhausted retries without resolution")

    # -- Public accessors -------------------------------------------------

    @property
    def telemetry(self) -> list[dict[str, Any]]:
        """Return all telemetry records as dicts for evidence serialisation."""

        return [r.to_dict() for r in self._telemetry]

    @property
    def post_debug_diagnoses(self) -> list[dict[str, Any]]:
        return list(self._post_debug_diagnoses)

    # -- Internal helpers -------------------------------------------------

    @staticmethod
    def _compute_rendered_obs_hash(
        last_obs: Optional[Observation],
    ) -> Optional[str]:
        """Compute the SHA-256 of the observation text rendered into the prompt.

        This is the hash of the natural-text observation rendering that
        ``bridge.render_prompt`` includes in the user prompt.  It proves the
        exact observation text the model received.
        """

        if last_obs is None:
            return None
        rendered = bridge._render_observation(last_obs)
        if not rendered:
            return None
        return _sha256(rendered)


# ---------------------------------------------------------------------------
# ScriptedBridgeAdapter — deterministic test double for offline tests
# ---------------------------------------------------------------------------


class ScriptedBridgeAdapter:
    """A deterministic adapter that feeds pre-written command strings through
    the real bridge parser.

    This is for the PDB integration test (test_debugger_interaction_v2_pdb.py).
    It does NOT call a real transport.  Each step is a raw text string that
    the model would have produced; the adapter runs it through ``bridge.parse``
    and returns the resulting typed directive.  This proves the full
    bridge→controller→registry→PDB→observation→model loop works end-to-end
    without loading the real model.

    It also records telemetry with provenance, exactly like
    ``DebuggerBridgeAdapter``, so the integration test can verify observation
    provenance binding.
    """

    def __init__(
        self,
        steps: tuple[str, ...],
        model_name: str = "scripted-bridge",
        task_description: str = "test task",
    ) -> None:
        self._steps = steps
        self._index = 0
        self._model_name = model_name
        self._task_description = task_description
        self._telemetry: list[TelemetryRecord] = []
        self._post_debug_diagnoses: list[dict[str, Any]] = []

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

        # Compute provenance.
        prior_obs_id = last_obs.observation_id if last_obs else None
        prior_obs_sha = None
        rendered_obs_sha = None
        if last_obs is not None:
            prior_obs_sha = _sha256(_canonical_json(last_obs.to_mapping()))
        rendered_obs_sha = DebuggerBridgeAdapter._compute_rendered_obs_hash(last_obs)

        # Render prompt (for telemetry — the scripted adapter doesn't send it).
        user_prompt = bridge.render_prompt(
            state=state,
            last_observation=last_obs,
            task_description=self._task_description,
            feedback=None,
        )

        record = TelemetryRecord(
            model_call_index=snapshot.model_call_index,
            transport_attempt_index=1,
            controller_state=state.value,
            system_prompt_sha256=_sha256(bridge.SYSTEM_PROMPT),
            user_prompt_sha256=_sha256(user_prompt),
            user_prompt_summary=_bound_text(user_prompt, 500),
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
            result = bridge.parse(raw_text, state, last_obs)
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

    @property
    def telemetry(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._telemetry]

    @property
    def post_debug_diagnoses(self) -> list[dict[str, Any]]:
        return list(self._post_debug_diagnoses)


__all__ = [
    "DebuggerBridgeAdapter",
    "ScriptedBridgeAdapter",
    "ModelTransport",
    "TransportError",
    "TransportResponse",
    "TelemetryRecord",
    "NOT_RECORDED",
    "NOT_AVAILABLE",
]