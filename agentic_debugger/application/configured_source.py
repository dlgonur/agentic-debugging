"""Production configured command-model execution source (Task 8).

The second supported Local Application V1 live mode: a user-configured local
command model executed through the accepted existing JSON-lines command
transport (``evaluation.live.JsonlCommandTransport`` protocol) and the
accepted ``LiveModelAdapter`` controller contract.

This source is a thin sibling of the deterministic source: both run the
shared :func:`agentic_debugger.application.local_source.run_local_session`
pipeline (real controller, tool registry, PDB, PatchManager, disposable
workspace, and independent verifier inside the accepted Task-3 worker
process with one shared session emitter).  The meaningful difference is
model construction:

- the validated app-owned profile (``CommandModelConfigStore``) becomes a
  ``LiveModelConfig``; the transport is the cancellable application variant
  of the accepted command transport (``CancellableJsonlCommandTransport``);
- ``LiveModelAdapter`` drives the same ``DeterministicController`` contract
  as every other supported model: directive validation and tool policy
  remain controller-owned, malformed model output is never reinterpreted as
  a valid directive, and the configured transport never mutates
  PatchManager/PDB/verifier directly;
- cancellation flows through the accepted Task-3 token into the transport's
  poll (the command tree is terminated promptly) and is never converted
  into a model error;
- the independent verifier runs only when the controller actually completed
  with an applied candidate patch, and evaluates that real applied
  candidate; the final ``EvaluationResult`` remains the correctness
  authority;
- a controller run that did not complete is an honest session failure
  (``ModelExecutionError`` with the exact Task-1 termination reason), never
  an orderly completion.

Provenance: one ``model.configured`` event (profile id, safe configuration
fingerprint, display label, protocol/tool version) is emitted through the
shared emission authority right after ``session.started``, so history/replay
can identify the selected profile without persisting the executable, argv,
or environment values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from agentic_debugger.application.command_config import (
    CommandConfigError,
    CommandModelConfigStore,
)
from agentic_debugger.application.command_transport import (
    CancellableJsonlCommandTransport,
)
from agentic_debugger.application.events import SessionEventKind
from agentic_debugger.application.local_source import (
    LocalSourceError,
    run_local_session,
)
from agentic_debugger.application.sources import ModelExecutionError
from agentic_debugger.application.worker_scenarios import (
    ScenarioContext,
    ScenarioInputError,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.tools import DemoToolContext
from agentic_debugger.evaluation.live import (
    MAX_MODEL_RESPONSE_BYTES,
    LiveModelAdapter,
    LiveModelConfig,
    LiveRunLimits,
)

#: The one production configured command-model source name the worker
#: dispatches.
CONFIGURED_SOURCE_NAME = "configured_command_model"

_KNOWN_PARAMS = frozenset({"config_root", "profile_id", "policy"})
_MAX_CONFIG_ROOT_CHARS = 2048
_MAX_PROFILE_ID_CHARS = 128
_MAX_POLICY_CHARS = 64

_DEFAULT_MAX_MODEL_REQUESTS = 64
_DEFAULT_MAX_CONTROLLER_STEPS = 64
_DEFAULT_MAX_RETRIES = 2


class ConfiguredSourceError(LocalSourceError):
    """Raised when the configured source itself fails (never for scientific
    outcomes; the verifier remains the correctness authority)."""


def _require_text(params: Mapping[str, Any], key: str, maximum: int) -> str:
    value = params.get(key)
    if type(value) is not str or not value:
        raise ScenarioInputError(
            f"configured source param {key!r} must be a non-empty string"
        )
    if len(value.encode("utf-8")) > maximum:
        raise ScenarioInputError(
            f"configured source param {key!r} exceeds the byte bound"
        )
    return value


def _validate_params(params: Mapping[str, Any]) -> tuple[str, str, str]:
    extra = set(params.keys()) - _KNOWN_PARAMS
    if extra:
        raise ScenarioInputError(f"unknown configured source params: {sorted(extra)}")
    config_root = _require_text(params, "config_root", _MAX_CONFIG_ROOT_CHARS)
    profile_id = _require_text(params, "profile_id", _MAX_PROFILE_ID_CHARS)
    policy = _require_text(params, "policy", _MAX_POLICY_CHARS)
    if policy not in {candidate.value for candidate in DemoPolicy}:
        raise ScenarioInputError(f"unknown demonstration policy: {policy!r}")
    return config_root, profile_id, policy


def run_configured_session(
    ctx: ScenarioContext,
    params: Mapping[str, Any],
) -> None:
    """Execute one real configured command-model debugging session.

    Runs inside the worker process after ``session.started`` (the work
    directory exists).  Every event flows through ``ctx.emitter`` (the
    session's single shared emission authority); cancellation honors
    ``ctx.token`` at every safe boundary, including inside the model
    transport's request poll.
    """
    config_root, profile_id, policy_value = _validate_params(params)
    if ctx.emitter is None:
        raise ScenarioInputError("configured source requires the shared emitter")
    policy = DemoPolicy(policy_value)
    task_id = ctx.emitter.task_id

    try:
        profile = CommandModelConfigStore(Path(config_root)).get(profile_id)
    except CommandConfigError as exc:
        raise ScenarioInputError(
            f"configured model profile is unavailable: {exc}"
        ) from exc

    # Safe provenance through the shared emission authority: history/replay
    # identifies the selected profile by id + fingerprint, never by a live
    # executable object or secret value.
    ctx.emitter.emit(
        SessionEventKind.MODEL_CONFIGURED,
        {
            "profile_id": profile.profile_id,
            "config_fingerprint": profile.configuration_fingerprint,
            "display_name": profile.display_name,
            "protocol_version": profile.protocol_version,
            "tool_version": profile.tool_version,
        },
    )

    live_config = LiveModelConfig(
        model_name=profile.display_name,
        command=profile.live_command(),
        request_timeout_seconds=profile.request_timeout_seconds,
        tool_version=profile.tool_version,
    )
    limits = LiveRunLimits(
        max_model_requests=_DEFAULT_MAX_MODEL_REQUESTS,
        max_controller_steps=_DEFAULT_MAX_CONTROLLER_STEPS,
        # The session deadline is enforced by the worker's cancellation
        # token (deadline + transport poll), never duplicated into a second
        # model-phase budget that could race the token's timeout
        # classification.
        max_elapsed_seconds=None,
        max_retries=_DEFAULT_MAX_RETRIES,
        max_response_bytes=MAX_MODEL_RESPONSE_BYTES,
    )

    def _initial_patch(workspace: Any) -> str:
        # The configured model proposes its own candidate through the real
        # apply_patch tool; the tool context starts with no patch.
        return ""

    def _model_factory(demo_context: DemoToolContext, registry: Any) -> Any:
        transport = CancellableJsonlCommandTransport(
            live_config,
            max_output_bytes=limits.max_response_bytes,
            cancel_check=ctx.token.check,
            cwd=profile.cwd,
            environment=dict(profile.environment) if profile.environment else None,
        )
        run_id = ctx.run_id or f"{task_id}--{policy_value}"
        adapter = LiveModelAdapter(
            task=demo_context.task,
            policy=policy,
            config=live_config,
            transport=transport,
            limits=limits,
            registry=registry,
            evaluation_id=ctx.emitter.session_id,
            case_id=f"{ctx.emitter.session_id}:{task_id}",
            run_id=run_id,
            trajectory_id=run_id,
        )
        adapters.append(adapter)
        return adapter

    adapters: list[Any] = []

    def _verifier_patch(
        demo_context: DemoToolContext, result: Any
    ) -> Optional[str]:
        # The independent verifier evaluates the candidate the configured
        # model actually applied, and only when the controller completed
        # with an applied patch; otherwise there is nothing to verify
        # honestly and the verifier stays the sole correctness authority.
        if result is None or result.final_state is not ControllerState.DONE:
            return None
        if not demo_context.patch_applied or not demo_context.candidate_patch:
            return None
        return demo_context.candidate_patch

    try:
        run_local_session(
            ctx,
            task_id=task_id,
            policy=policy,
            initial_patch=_initial_patch,
            model_factory=_model_factory,
            verifier_patch=_verifier_patch,
            fail_on_controller_failure=True,
            max_model_calls=_DEFAULT_MAX_CONTROLLER_STEPS,
            registry_pdb_policy=pdb_policy_for(policy),
        )
    except ModelExecutionError as exc:
        # Enrich the honest failure with the adapter's bounded transport
        # termination reason (a bounded vocabulary value, never raw command
        # output), so the terminal diagnostic is actionable without
        # leaking secrets.
        if adapters:
            transport_reason = adapters[-1].metrics.termination_reason
            if transport_reason:
                raise ModelExecutionError(
                    f"{exc} (model transport: {transport_reason})",
                    exc.termination_reason,
                ) from exc
        raise
    except LocalSourceError as exc:
        raise ConfiguredSourceError(str(exc)) from exc


__all__ = [
    "CONFIGURED_SOURCE_NAME",
    "ConfiguredSourceError",
    "run_configured_session",
]
