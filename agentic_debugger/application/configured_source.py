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

import re
import json
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
    PreModelSetupFailure,
    ScenarioContext,
    ScenarioInputError,
)
from agentic_debugger.agent.controller_policy import PdbPolicy
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

_KNOWN_PARAMS = frozenset(
    {
        "config_root",
        "profile_id",
        "policy",
        "expected_fingerprint",
        "external_task_path",
        "external_repository_root",
        "external_root",
        "external_bundle_path",
        "external_preflight_path",
        "external_readiness_mode",
        "external_instance_id",
        "external_manifest_fingerprint",
        "external_authority_revision",
        "external_project",
        "external_bug_id",
        "external_buggy_revision",
        "model_phase_seconds",
    }
)
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.swerebench.execution import (
    OfficialSWERebenchVerifier,
    build_docker_execution_context,
    load_private_bundle,
)
_MAX_CONFIG_ROOT_CHARS = 2048
_MAX_PROFILE_ID_CHARS = 128
_MAX_POLICY_CHARS = 64
#: A safe configuration fingerprint is a SHA-256 hex digest.
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")

_DEFAULT_MAX_MODEL_REQUESTS = 64
_DEFAULT_MAX_CONTROLLER_STEPS = 64
_DEFAULT_MAX_RETRIES = 2
_PROVIDER_METRICS_NAME = "provider.metrics.json"
class ConfiguredSourceError(LocalSourceError):
    """Raised when the configured source itself fails (never for scientific
    outcomes; the verifier remains the correctness authority)."""


def _require_text(params: Mapping[str, Any], key: str, maximum: int) -> str:
    value = params.get(key)
    if type(value) is not str or not value:
        raise PreModelSetupFailure(
            f"configured source param {key!r} must be a non-empty string"
        )
    if len(value.encode("utf-8")) > maximum:
        raise PreModelSetupFailure(
            f"configured source param {key!r} exceeds the byte bound"
        )
    return value


def _optional_fingerprint(params: Mapping[str, Any]) -> Optional[str]:
    """Validate the optional pinned configuration fingerprint.

    The Start action pins the selected profile's safe fingerprint so the
    worker can detect a configuration that changed between selection and
    worker load (TOCTOU).  When present it must be an exact SHA-256 hex
    digest; any other shape fails closed.
    """
    value = params.get("expected_fingerprint")
    if value is None:
        return None
    if type(value) is not str or _FINGERPRINT_RE.fullmatch(value) is None:
        raise PreModelSetupFailure(
            "configured source param 'expected_fingerprint' must be a "
            "64-character lowercase hex fingerprint"
        )
    return value


def _validate_params(
    params: Mapping[str, Any]
) -> tuple[str, str, str, Optional[str], Optional[int]]:
    extra = set(params.keys()) - _KNOWN_PARAMS
    if extra:
        raise PreModelSetupFailure(f"unknown configured source params: {sorted(extra)}")
    config_root = _require_text(params, "config_root", _MAX_CONFIG_ROOT_CHARS)
    profile_id = _require_text(params, "profile_id", _MAX_PROFILE_ID_CHARS)
    policy = _require_text(params, "policy", _MAX_POLICY_CHARS)
    if policy not in {candidate.value for candidate in DemoPolicy}:
        raise PreModelSetupFailure(f"unknown demonstration policy: {policy!r}")
    expected_fingerprint = _optional_fingerprint(params)
    model_phase_seconds = params.get("model_phase_seconds")
    if model_phase_seconds is not None and (
        type(model_phase_seconds) is not int or not 1 <= model_phase_seconds <= 3600
    ):
        raise PreModelSetupFailure(
            "configured source param 'model_phase_seconds' must be an int in [1, 3600]"
        )
    return config_root, profile_id, policy, expected_fingerprint, model_phase_seconds


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
    (
        config_root,
        profile_id,
        policy_value,
        expected_fingerprint,
        model_phase_seconds,
    ) = _validate_params(
        params
    )
    if ctx.emitter is None:
        raise PreModelSetupFailure("configured source requires the shared emitter")
    policy = DemoPolicy(policy_value)
    task_id = ctx.emitter.task_id

    try:
        profile = CommandModelConfigStore(Path(config_root)).get(profile_id)
    except CommandConfigError as exc:
        raise PreModelSetupFailure(
            f"configured model profile is unavailable: {exc}"
        ) from exc

    # Configuration TOCTOU pin: the Start action captured the selected
    # profile's safe fingerprint; the worker recomputes it from the
    # configuration it actually loaded.  A mismatch means the configuration
    # changed between selection and load, so the session fails closed before
    # any model.configured emission or executable launch.  The diagnostic
    # carries only the safe profile id and the two fingerprints (hashes of
    # credential-free validated configuration), never an executable or value.
    if (
        expected_fingerprint is not None
        and profile.configuration_fingerprint != expected_fingerprint
    ):
        raise PreModelSetupFailure(
            "configured model profile changed between selection and launch: "
            f"profile {profile_id!r} fingerprint {expected_fingerprint} "
            f"does not match the loaded configuration "
            f"{profile.configuration_fingerprint}"
        )

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
        # The treatment/run supplies this explicitly.  Do not infer a
        # scientific budget from a transport presentation flag such as
        # ``--stream``; the worker owns the separate overall task deadline.
        max_elapsed_seconds=model_phase_seconds,
        max_retries=_DEFAULT_MAX_RETRIES,
        max_response_bytes=MAX_MODEL_RESPONSE_BYTES,
        # Streaming GPT-OSS generations can occupy the full bounded request
        # window; repeating an already-started provider generation multiplies
        # the wall-clock budget without adding evidence.  V7 opts out via the
        # explicit --stream treatment flag while legacy profiles retain the
        # historical retry policy.
        retry_provider_timeouts="--stream" not in profile.live_command(),
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

    external_task_path = params.get("external_task_path")
    if external_task_path is not None:
        readiness_mode = params.get("external_readiness_mode", "preflight")
        if readiness_mode not in {"preflight", "direct"}:
            raise PreModelSetupFailure(
                "external readiness mode must be 'preflight' or 'direct'"
            )
        if readiness_mode == "direct" and "external_preflight_path" in params:
            raise PreModelSetupFailure(
                "direct readiness mode does not accept preflight records"
            )
        required_external = (
            "external_repository_root",
            "external_root",
            "external_bundle_path",
            "external_instance_id",
            "external_manifest_fingerprint",
            "external_authority_revision",
            "external_project",
            "external_bug_id",
            "external_buggy_revision",
        )
        values = {key: params.get(key) for key in required_external}
        missing = [
            key for key, value in values.items()
            if type(value) is not str or not value
        ]
        if missing:
            raise PreModelSetupFailure(
                "external configured source is missing required runtime metadata: "
                + ", ".join(missing)
            )
        task = DebugTask.from_file(str(external_task_path))
        repository_root = Path(str(values["external_repository_root"])).resolve()
        external_root = Path(str(values["external_root"])).resolve()
        if not repository_root.is_dir() or not external_root.is_dir():
            raise PreModelSetupFailure("external repository and runtime roots must exist")
        bundle = load_private_bundle(Path(str(values["external_bundle_path"])))
        baseline_valid: bool | None = None
        if readiness_mode == "preflight":
            preflight_path = params.get("external_preflight_path")
            if type(preflight_path) is not str or not preflight_path:
                raise PreModelSetupFailure(
                    "external preflight record is required in preflight readiness mode"
                )
            try:
                preflight = json.loads(Path(preflight_path).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise PreModelSetupFailure(
                    "external preflight record is missing or invalid"
                ) from exc
            if preflight.get("instance_id") != values["external_instance_id"]:
                raise PreModelSetupFailure("external preflight identity does not match the task")
            if not preflight.get("verifier_baseline_valid"):
                raise PreModelSetupFailure("external official verifier baseline is not valid")
            baseline_valid = True
        if task.task_id != task_id or task.source is None or task.source.kind != "external":
            raise PreModelSetupFailure("external task is not bound to the Local Application task")
        execution_context = build_docker_execution_context(
            bundle=bundle,
            external_root=ctx.work_dir.resolve(),
            instance_id=str(values["external_instance_id"]),
            manifest_fingerprint=str(values["external_manifest_fingerprint"]),
            authority_revision=str(values["external_authority_revision"]),
            project=str(values["external_project"]),
            bug_id=str(values["external_bug_id"]),
            buggy_revision=str(values["external_buggy_revision"]),
        )
        verifier = OfficialSWERebenchVerifier(
            bundle,
            work_root=external_root,
            baseline_valid=baseline_valid,
        )

        def _external_verifier(_task: Any, candidate: str) -> dict[str, Any]:
            return verifier.evaluate(candidate)

        # Option B: the existing PDB launcher is not coupled to the public
        # failing pytest process.  Pilot-10 therefore records PDB as not
        # exercised; the separate frozen treatment owns debugger claims.
    try:
        if external_task_path is not None:
            run_local_session(
                ctx,
                task_id=task_id,
                policy=policy,
                initial_patch=_initial_patch,
                model_factory=_model_factory,
                verifier_patch=_verifier_patch,
                fail_on_controller_failure=True,
                max_model_calls=_DEFAULT_MAX_CONTROLLER_STEPS,
                registry_pdb_policy=PdbPolicy.DISABLED,
                fixture_dir=repository_root,
                task=task,
                execution_context=execution_context,
                verifier_evaluator=_external_verifier,
                verifier_repository_root=repository_root,
                require_external_source_context=True,
            )
        else:
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
    finally:
        _persist_provider_metrics(ctx, adapters)


def _persist_provider_metrics(ctx: ScenarioContext, adapters: list[Any]) -> None:
    """Persist bounded, secret-free adapter metrics for configured sessions."""
    if ctx.session_dir is None or not adapters:
        return
    metrics = adapters[-1].metrics.to_mapping()
    try:
        ctx.session_dir.mkdir(parents=True, exist_ok=True)
        (ctx.session_dir / _PROVIDER_METRICS_NAME).write_text(
            json.dumps(metrics, ensure_ascii=True, allow_nan=False, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
    except OSError:
        # The worker's terminal result remains truthful; a missing artifact is
        # projected as unavailable rather than replaced with invented values.
        return


__all__ = [
    "CONFIGURED_SOURCE_NAME",
    "ConfiguredSourceError",
    "run_configured_session",
]
