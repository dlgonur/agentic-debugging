"""D1 — Forced-Runtime-Entry Sanity Diagnostic: phase-navigation adapter.

D1 changes exactly one thing vs the frozen S1 treatment: AFTER a verified
baseline failure reproduction, the experiment-local harness deterministically
performs the existing legal administrative phase transitions

    REPRODUCE -> UNDERSTAND -> RUNTIME_EVIDENCE

and then hands the model to the EXISTING S1 RUNTIME_EVIDENCE state-specific
command surface.  From that point onward every debugger/action choice is
model-authored.

This module implements ``D1PhaseNavigationAdapter``, a thin wrapper around
the unchanged S1 ``DebuggerBridgeAdapter`` (or ``ScriptedBridgeAdapter`` for
offline tests) that implements the existing ``ModelAdapter`` Protocol
(``model_name`` + ``next_directive(snapshot)``).  The wrapper:

* delegates every call that is NOT an administrative phase navigation to the
  inner adapter unchanged (so the model authors reproduce, break, stack,
  locals, print, step, next, continue, stop, diagnosis, patch itself);
* only after a real observation with ``failure_reproduced == true`` performs
  the two legal administrative transitions;
* if reproduction never succeeds, never forces runtime entry (the wrapper
  stays in delegation; the controller's budget/stop logic governs).

The wrapper NEVER:
* chooses a debugger command (source / break / stack / locals / print / step
  / next / continue / diagnosis / patch);
* chooses a breakpoint for the model;
* injects runtime evidence;
* modifies PDB observations;
* fabricates a model response.

Administrative transitions are recorded in telemetry tagged
``d1_authorship: "administrative"`` and ``raw_response_status:
"administrative_navigation"``.  They are TransitionDirectives, so they carry
no ``action_name`` and their ``parse_result.status`` is ``"administrative"``
(never ``"accepted"``) — they cannot be counted by the existing
``_compute_gate_b`` filter (which requires
``parse_result.status == "accepted"`` AND ``action_name in _PDB_ACTIONS``).
This is the deterministic enforcement of the D1 rule: administrative
transitions inserted by the harness do NOT count as model debugger commands.
"""

from __future__ import annotations

from typing import Any, Optional

from agentic_debugger.agent.model_adapter import (
    ControllerSnapshot,
    ModelDirective,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState

from experiments.debugger_interaction_v2.bridge import (
    _baseline_reproduction_succeeded,
)
from experiments.debugger_interaction_v2.adapter import (
    NOT_AVAILABLE,
    NOT_RECORDED,
)

# Deterministic reasons for the two administrative transitions.  These are
# the ONLY automated directives in D1; every other directive is delegated.
_ADMIN_REASON_REPRODUCE_TO_UNDERSTAND = (
    "D1 administrative phase navigation: REPRODUCE->UNDERSTAND after "
    "verified baseline reproduction"
)
_ADMIN_REASON_UNDERSTAND_TO_RUNTIME = (
    "D1 administrative phase navigation: UNDERSTAND->RUNTIME_EVIDENCE "
    "(forced runtime-entry diagnostic)"
)


class D1PhaseNavigationAdapter:
    """Experiment-local wrapper that automates ONLY administrative phase
    navigation after verified reproduction, then delegates to the model.

    Implements the existing ``ModelAdapter`` Protocol so the unchanged
    ``DeterministicController`` can drive it.
    """

    def __init__(self, inner_adapter: Any) -> None:
        self._inner = inner_adapter
        self._admin_nav_done = False
        self._admin_transitions: list[dict[str, Any]] = []

    # -- ModelAdapter Protocol ---------------------------------------------

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    def next_directive(
        self,
        snapshot: ControllerSnapshot,
    ) -> ModelDirective:
        state = snapshot.state
        last_obs = snapshot.last_observation

        # -- Administrative phase navigation (D1 treatment) ----------------
        # Only BEFORE the navigation has completed, and only from a REAL
        # reproduction observation with failure_reproduced == true.  The
        # model is not consulted for these two transitions.
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

        # -- Everything else is model-authored -----------------------------
        return self._inner.next_directive(snapshot)

    # -- Accessors ---------------------------------------------------------

    @property
    def telemetry(self) -> list[dict[str, Any]]:
        """Model-facing telemetry: administrative transitions FIRST, then the
        inner adapter's model-call telemetry.

        Administrative records are tagged ``d1_authorship="administrative"``
        and can never be counted as accepted PDB commands by the existing
        Gate-B computation.
        """

        return list(self._admin_transitions) + self._inner.telemetry

    @property
    def post_debug_diagnoses(self) -> list[dict[str, Any]]:
        return self._inner.post_debug_diagnoses

    @property
    def admin_transitions(self) -> list[dict[str, Any]]:
        return list(self._admin_transitions)

    # -- Internal ----------------------------------------------------------

    def _record_admin_transition(
        self,
        snapshot: ControllerSnapshot,
        target_state: ControllerState,
        reason: str,
    ) -> None:
        last_obs = snapshot.last_observation
        self._admin_transitions.append({
            "model_call_index": snapshot.model_call_index,
            "transport_attempt_index": 0,  # no transport call
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


__all__ = ["D1PhaseNavigationAdapter"]
