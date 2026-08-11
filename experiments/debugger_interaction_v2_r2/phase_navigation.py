"""R2 phase-navigation adapter (self-contained, no hidden deps).

Reuses the D1 forced-runtime-entry mechanism: AFTER a verified baseline
failure reproduction, the harness deterministically performs the two legal
administrative phase transitions

    REPRODUCE -> UNDERSTAND -> RUNTIME_EVIDENCE

and then hands the model to the R2 RUNTIME_EVIDENCE staged command surface.
From that point onward every debugger/action choice is model-authored.
"""

from __future__ import annotations

from typing import Any

from agentic_debugger.agent.model_adapter import (
    ControllerSnapshot,
    ModelDirective,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState

from experiments.debugger_interaction_v2_r2.bridge import (
    _baseline_reproduction_succeeded,
)
from experiments.debugger_interaction_v2_r2.adapter import (
    NOT_AVAILABLE,
    NOT_RECORDED,
)

_ADMIN_REASON_REPRODUCE_TO_UNDERSTAND = (
    "R2 administrative phase navigation: REPRODUCE->UNDERSTAND after "
    "verified baseline reproduction"
)
_ADMIN_REASON_UNDERSTAND_TO_RUNTIME = (
    "R2 administrative phase navigation: UNDERSTAND->RUNTIME_EVIDENCE "
    "(forced runtime-entry diagnostic)"
)


class R2PhaseNavigationAdapter:
    """Experiment-local wrapper that automates ONLY administrative phase
    navigation after verified reproduction, then delegates to the model."""

    def __init__(self, inner_adapter: Any) -> None:
        self._inner = inner_adapter
        self._admin_nav_done = False
        self._admin_transitions: list[dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    def next_directive(
        self,
        snapshot: ControllerSnapshot,
    ) -> ModelDirective:
        state = snapshot.state
        last_obs = snapshot.last_observation

        if not self._admin_nav_done:
            if (
                state is ControllerState.REPRODUCE
                and _baseline_reproduction_succeeded(last_obs)
            ):
                self._record_admin_transition(
                    snapshot, ControllerState.UNDERSTAND,
                    _ADMIN_REASON_REPRODUCE_TO_UNDERSTAND,
                )
                return TransitionDirective(
                    ControllerState.UNDERSTAND,
                    _ADMIN_REASON_REPRODUCE_TO_UNDERSTAND,
                )
            if state is ControllerState.UNDERSTAND:
                self._admin_nav_done = True
                self._record_admin_transition(
                    snapshot, ControllerState.RUNTIME_EVIDENCE,
                    _ADMIN_REASON_UNDERSTAND_TO_RUNTIME,
                )
                return TransitionDirective(
                    ControllerState.RUNTIME_EVIDENCE,
                    _ADMIN_REASON_UNDERSTAND_TO_RUNTIME,
                )

        return self._inner.next_directive(snapshot)

    @property
    def telemetry(self) -> list[dict[str, Any]]:
        return list(self._admin_transitions) + self._inner.telemetry

    @property
    def post_debug_diagnoses(self) -> list[dict[str, Any]]:
        return self._inner.post_debug_diagnoses

    @property
    def admin_transitions(self) -> list[dict[str, Any]]:
        return list(self._admin_transitions)

    def _record_admin_transition(
        self,
        snapshot: ControllerSnapshot,
        target_state: ControllerState,
        reason: str,
    ) -> None:
        last_obs = snapshot.last_observation
        self._admin_transitions.append({
            "model_call_index": snapshot.model_call_index,
            "transport_attempt_index": 0,
            "controller_state": snapshot.state.value,
            "d1_authorship": "administrative",
            "request": {
                "system_prompt_sha256": None,
                "user_prompt_sha256": None,
                "user_prompt_summary": None,
            },
            "raw_response_text": NOT_AVAILABLE,
            "raw_response_status": "administrative_navigation",
            "raw_response_bytes": NOT_AVAILABLE,
            "transport_error_category": None,
            "parse_result": {
                "status": "administrative",
                "command_token": None,
                "normalized_command": None,
                "rejection_category": None,
                "rejection_message": None,
            },
            "translated_directive": {
                "kind": "transition",
                "action_name": None,
                "arguments": None,
                "target_state": target_state.value,
                "reason": reason,
                "is_diagnosis": False,
                "diagnosis_text": None,
            },
            "provenance": {
                "prior_observation_id": (
                    last_obs.observation_id if last_obs is not None else None
                ),
                "prior_observation_sha256": None,
                "rendered_observation_sha256": None,
            },
            "usage": {
                "prompt_tokens": NOT_RECORDED,
                "completion_tokens": NOT_RECORDED,
                "total_tokens": NOT_RECORDED,
                "provider_reported": False,
            },
            "timing": {
                "request_duration_ms": NOT_RECORDED,
                "parse_duration_ms": NOT_RECORDED,
            },
        })


# Keep R1 name as alias for imports that copied from r1_runner
R1PhaseNavigationAdapter = R2PhaseNavigationAdapter

__all__ = ["R2PhaseNavigationAdapter", "R1PhaseNavigationAdapter"]
