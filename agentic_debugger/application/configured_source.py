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

from agentic_debugger.application.level32 import LEVEL32_TASK_ID
from agentic_debugger.application.level32_materialization import (
    SourceAcquisitionMode,
    build_level32_interactive_scenario,
    materialize_level32_task,
)
from agentic_debugger.application.ollama_cloud_source import (
    INTERACTIVE_LADDER_DIRECTIVE_REPAIRS,
    LADDER_RUNTIME_CONTRACTS,
    LadderRuntimeContract,
    ladder_runtime_contract,
)
from agentic_debugger.demo.catalog import scenario_for

#: The one production configured command-model source name the worker
#: dispatches.
CONFIGURED_SOURCE_NAME = "configured_command_model"

_KNOWN_PARAMS = frozenset(
    {"config_root", "profile_id", "policy", "expected_fingerprint", "provider", "model_id"}
)

#: Registry provider kinds this source may resolve (the unified provider
#: platform subset that is not the app-owned profile store).
_REGISTRY_PROVIDERS = frozenset(
    {"ollama_cloud", "opencode_go", "commandcode_goat"}
)
_MAX_CONFIG_ROOT_CHARS = 2048
_MAX_PROFILE_ID_CHARS = 128
_MAX_POLICY_CHARS = 64
#: A safe configuration fingerprint is a SHA-256 hex digest.
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")

#: Task 44 (Unbounded Session Progress v1): interactive/configured
#: sessions have NO Agentic-Debugger-owned total model-request /
#: directive / controller-step execution ceiling.  Progress counters are
#: telemetry only.  The historical finite defaults below are retained as
#: provenance constants for interpreting pre-Task-44 evidence and for
#: explicit frozen/operator callers only; generic execution passes
#: ``None`` (LiveRunLimits/controller) and ``0`` (provider-adapter
#: logical ceiling = unbounded) and never consults these values.
_DEFAULT_MAX_MODEL_REQUESTS = 64
_DEFAULT_MAX_CONTROLLER_STEPS = 64
_DEFAULT_MAX_RETRIES = 2

#: Unbounded sentinel for the provider-adapter logical ceiling
#: (``--max-logical-model-calls 0`` = no upper-bound termination).
_UNBOUNDED_LOGICAL_CEILING = 0


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
        raise ScenarioInputError(
            "configured source param 'expected_fingerprint' must be a "
            "64-character lowercase hex fingerprint"
        )
    return value


def _validate_policy(params: Mapping[str, Any]) -> str:
    policy = params.get("policy")
    if type(policy) is not str or not policy:
        raise ScenarioInputError(
            "configured source param 'policy' must be a non-empty string"
        )
    if len(policy.encode("utf-8")) > _MAX_POLICY_CHARS:
        raise ScenarioInputError(
            "configured source param 'policy' exceeds the byte bound"
        )
    if policy not in {candidate.value for candidate in DemoPolicy}:
        raise ScenarioInputError(f"unknown demonstration policy: {policy!r}")
    return policy


def _validate_store_params(
    params: Mapping[str, Any]
) -> tuple[str, str, str, Optional[str]]:
    extra = set(params.keys()) - _KNOWN_PARAMS
    if extra:
        raise ScenarioInputError(f"unknown configured source params: {sorted(extra)}")
    if "provider" in params or "model_id" in params:
        raise ScenarioInputError(
            "store-profile configured sessions accept only config_root, "
            "profile_id, policy, and expected_fingerprint"
        )
    config_root = _require_text(params, "config_root", _MAX_CONFIG_ROOT_CHARS)
    profile_id = _require_text(params, "profile_id", _MAX_PROFILE_ID_CHARS)
    policy = _validate_policy(params)
    expected_fingerprint = _optional_fingerprint(params)
    return config_root, profile_id, policy, expected_fingerprint


def _is_registry_provider(provider: str) -> bool:
    try:
        from agentic_debugger.application.provider_connections import is_known_provider
        return is_known_provider(provider)
    except Exception:
        return False


def _validate_registry_params(
    params: Mapping[str, Any]
) -> tuple[str, str, str]:
    """Validate the provider-registry parameter contract.

    Provider-resolved models are the unified provider platform's command
    models (Ollama Cloud, OpenCode Go, CommandCode GOAT, and configured custom providers).
    They resolve through :func:`resolve_provider_live_config` — the same canonical
    builder Local Project uses — so the profile-store parameters
    (``config_root``/``profile_id``/``expected_fingerprint``) are mutually
    exclusive with the provider parameters and fail closed when mixed.
    """
    extra = set(params.keys()) - _KNOWN_PARAMS
    if extra:
        raise ScenarioInputError(f"unknown configured source params: {sorted(extra)}")
    provider = params.get("provider")
    if type(provider) is not str or not _is_registry_provider(provider):
        raise ScenarioInputError(
            f"configured source param 'provider' {provider!r} is not a known provider"
        )
    model_id = params.get("model_id")
    if type(model_id) is not str or not model_id:
        raise ScenarioInputError(
            "configured source param 'model_id' must be a non-empty string"
        )
    if len(model_id.encode("utf-8")) > _MAX_PROFILE_ID_CHARS:
        raise ScenarioInputError(
            "configured source param 'model_id' exceeds the byte bound"
        )
    forbidden = set(params.keys()) & {
        "config_root",
        "profile_id",
        "expected_fingerprint",
    }
    if forbidden:
        raise ScenarioInputError(
            "provider-resolved models cannot mix profile-store params: "
            f"{sorted(forbidden)}"
        )
    policy = _validate_policy(params)
    return provider, model_id, policy


def _resolve_registry_model(
    provider: str, model_id: str, *, logical_call_ceiling: int | None = None
) -> tuple[Any, Any, str]:
    """(LiveModelConfig, provenance payload, config_fingerprint) for one
    provider-registry model.

    Resolution goes through the canonical registry builder — the same one
    Local Project uses — so an unavailable provider, an unknown model
    identity, or an invalid configuration fails closed here, before any
    ``model.configured`` emission or executable launch.  The fingerprint
    is the deterministic configuration identity of the provider/model
    pair (the registry has no file-backed configuration to hash).

    Task 44: ``logical_call_ceiling=None`` (the generic default) means
    unbounded (``0`` to the provider adapter = no upper-bound
    termination).  An explicit finite ceiling is honored only for
    explicit callers (frozen/operator).
    """
    import hashlib

    from agentic_debugger.application.model_providers import (
        ProviderRegistryError,
        resolve_provider_live_config,
    )

    ceiling = (
        _UNBOUNDED_LOGICAL_CEILING
        if logical_call_ceiling is None
        else int(logical_call_ceiling)
    )
    try:
        live_config, provenance = resolve_provider_live_config(
            provider,
            model_id,
            logical_call_ceiling=ceiling,
        )
    except ProviderRegistryError as exc:
        raise ScenarioInputError(
            f"provider model is unavailable: {exc}"
        ) from exc
    fingerprint = hashlib.sha256(
        f"provider:{provider}:{model_id}".encode("utf-8")
    ).hexdigest()
    return live_config, provenance, fingerprint


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

    Two mutually exclusive parameter contracts select the command model:

    - the app-owned profile store (``config_root``/``profile_id``); or
    - the unified provider registry (``provider``/``model_id``) for
      Ollama Cloud, OpenCode Go, and CommandCode GOAT models.
    """
    if ctx.emitter is None:
        raise ScenarioInputError("configured source requires the shared emitter")
    task_id = ctx.emitter.task_id

    # Provider-neutral ladder contract: if task_id is an accepted
    # ladder rung, the rung's proof contract is honored regardless of
    # which provider is selected.  Lower ladder rungs enforce their
    # public reproduction probe; Level 32 materializes its interactive
    # workspace.  Task 44: total-session model-request / controller-step
    # counts are NOT execution ceilings for interactive execution —
    # ``ladder_contract`` below carries only the time/retry/repair
    # dimensions; request/step dimensions are always unbounded (None/0).
    if task_id in LADDER_RUNTIME_CONTRACTS:
        ladder_contract = ladder_runtime_contract(task_id)
        ladder_scenario = scenario_for(task_id)
        if not ladder_scenario.runtime_probe.exact_public_reproduction:
            raise ScenarioInputError(
                f"lower ladder task {task_id!r} requires exact public reproduction probe"
            )
        is_lower_ladder = True
        is_level32 = False
    elif task_id == LEVEL32_TASK_ID:
        is_lower_ladder = False
        is_level32 = True
        # Task 44: the historical 25/25 total-session values are NOT
        # consulted for execution.  Only time/retry/repair dimensions
        # are carried; request/step ceilings are unbounded.  The 25s
        # remain as provenance constants for pre-Task-44 evidence.
        ladder_contract = LadderRuntimeContract(
            max_model_requests=25,
            max_controller_steps=25,
            max_model_phase_seconds=3600,
            max_retries=1,
            max_directive_repairs=INTERACTIVE_LADDER_DIRECTIVE_REPAIRS,
        )
        ladder_scenario = None
    else:
        is_lower_ladder = False
        is_level32 = False
        ladder_contract = None
        ladder_scenario = None

    cwd: Optional[Path] = None
    environment: Optional[dict[str, str]] = None
    if "provider" in params or "model_id" in params:
        provider, model_id, policy_value = _validate_registry_params(params)
        policy = DemoPolicy(policy_value)
        # Task 44: interactive execution is unbounded — the provider
        # adapter receives the unbounded sentinel (0 = no upper-bound
        # termination) regardless of ladder membership.  Historical
        # task-specific ceilings (24 lower / 25 Level 32 / 64 general)
        # are provenance only and never gate execution.
        ceiling = _UNBOUNDED_LOGICAL_CEILING
        live_config, provenance, fingerprint = _resolve_registry_model(
            provider, model_id, logical_call_ceiling=ceiling
        )
        # Repair 24 (F2): the executable authority comes from the SAME
        # authoritative snapshot that built live_config (provenance carries
        # the safe provider runtime identity derived inside the resolver
        # from that snapshot).  Never infer it by sampling mutable global
        # config before/after resolution — ABA A->B->A would otherwise look
        # unchanged while live_config genuinely uses B.  The ceiling above
        # (general 64, lower-ladder task-specific) is unchanged.
        _provenance_route = str(provenance.get("route") or "direct_api")
        expected_authority = provenance.get("provider_runtime_identity")
        # Repair 25: real direct execution requires proven executable authority.
        # Missing/malformed authority fails closed (no CURRENT fallback — a
        # missing key is unprovable, whether from a resolver regression or a
        # stale fake).  Test fakes must carry a valid synthetic authority.
        # Direct-API routes require a well-formed executable authority;
        # without it the executable/credential pair cannot be proven
        # coherent and fails closed before any credential egress, event
        # emission, or child construction.  Legacy CLI routes carry an
        # authority when available but materialize no raw credential.
        if _provenance_route == "direct_api" and (
            not isinstance(expected_authority, str) or len(expected_authority) != 64
        ):
            raise ScenarioInputError(
                "provider configuration is incomplete or changed during resolution"
            )
        # Direct-API routes receive exactly one bounded credential
        # override in the adapter child environment (never argv, never
        # evidence); legacy CLI routes read the operator auth store in
        # place and need no override.  V2-04: resolved through the
        # CredentialVault authority, never by touching stores directly.
        from agentic_debugger.application.credential_vault import (
            CredentialVaultError,
            CredentialVault,
        )

        try:
            _env = CredentialVault.default().transport_materialization(
                provider,
                route=_provenance_route,
                expected_provider_authority=expected_authority,
            )
        except CredentialVaultError as exc:
            # V2-04 (repair 21): an auth-required direct route whose
            # credential authority became unavailable between resolution
            # and transport establishment fails closed as a typed
            # session-start error (credential-free text).
            raise ScenarioInputError(
                f"provider credential is unavailable: {exc}"
            ) from exc
        environment = dict(_env) if _env is not None else None
        # Session-scoped runtime metadata (profile-gated, non-secret): the
        # stable Agentic Debugger session identity is issued to the adapter
        # child alongside the credential channel for providers whose
        # runtime profile requires it (OpenCode Go x-opencode-session).
        # Generic providers receive no extra values.
        if _provenance_route == "direct_api":
            try:
                from agentic_debugger.application import provider_runtime as _rt

                _prof = _rt.runtime_profile_for_kind(provider)
                if getattr(_prof, "requires_session_header", False):
                    _raw_sid = getattr(getattr(ctx, "emitter", None), "session_id", None)
                    _sid = _raw_sid if _rt.is_valid_transport_session_id(_raw_sid) else _rt.new_transport_session_id()
                    _sess_env = _rt.transport_session_environment(_sid)
                    _merged = dict(environment) if environment is not None else {}
                    _merged.update(_sess_env)
                    environment = _merged
            except Exception:
                pass
        ctx.emitter.emit(
            SessionEventKind.MODEL_CONFIGURED,
            {
                "profile_id": model_id,
                "config_fingerprint": fingerprint,
                "display_name": str(provenance.get("display_name") or model_id),
                "protocol_version": str(
                    provenance.get("protocol_version") or "1.3"
                ),
                "tool_version": str(live_config.tool_version),
                "provider": provider,
                **(
                    {"route": str(provenance["route"])}
                    if "route" in provenance
                    else {}
                ),
                **(
                    {"api_protocol": str(provenance["api_protocol"])}
                    if "api_protocol" in provenance
                    else {}
                ),
                **(
                    {"provider_model_id": str(provenance["provider_model_id"])}
                    if "provider_model_id" in provenance
                    else {}
                ),
                **(
                    {"endpoint": str(provenance["endpoint"])}
                    if "endpoint" in provenance
                    else {}
                ),
            },
        )
    else:
        config_root, profile_id, policy_value, expected_fingerprint = (
            _validate_store_params(params)
        )
        policy = DemoPolicy(policy_value)

        try:
            profile = CommandModelConfigStore(Path(config_root)).get(profile_id)
        except CommandConfigError as exc:
            raise ScenarioInputError(
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
            raise ScenarioInputError(
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
        cwd = profile.cwd
        environment = dict(profile.environment) if profile.environment else None

    staging_root: Optional[Path] = None
    fixture_dir: Optional[Path] = None
    if is_level32:
        try:
            staging_root = ctx.work_dir / "level32_staging"
            fixture_dir = materialize_level32_task(
                staging_root,
                mode=SourceAcquisitionMode.INTERACTIVE_CACHE_FIRST,
            )
            ladder_scenario = build_level32_interactive_scenario(task_id=task_id)
        except Exception as exc:
            ctx.emitter.emit(
                SessionEventKind.DIAGNOSIS_RECORDED,
                {
                    "text": f"Level-32 workspace preparation failed: {exc}",
                    "file_path": None,
                    "symbol": None,
                    "confidence": "observed",
                },
            )
            raise ConfiguredSourceError(
                f"Level-32 workspace preparation failed: {exc}"
            ) from exc

    # Task 44: interactive/configured execution is unbounded.  The
    # ladder contract contributes ONLY time/retry/repair dimensions;
    # total-session request/step dimensions are always None (telemetry
    # only, no execution ceiling) regardless of ladder membership.
    # Directive repair is an explicit interactive-only concept: these are
    # executable unqualified provider runs, never a qualified scientific
    # treatment (the qualified ladder and Level-32 paths keep zero).
    if (is_lower_ladder or is_level32) and ladder_contract is not None:
        limits = LiveRunLimits(
            max_model_requests=None,
            max_controller_steps=None,
            max_model_phase_seconds=ladder_contract.max_model_phase_seconds,
            max_retries=ladder_contract.max_retries,
            max_directive_repairs=INTERACTIVE_LADDER_DIRECTIVE_REPAIRS,
            continue_on_task_failure=False,
            max_response_bytes=MAX_MODEL_RESPONSE_BYTES,
        )
    else:
        limits = LiveRunLimits(
            max_model_requests=None,
            max_controller_steps=None,
            # The session deadline is enforced by the worker's cancellation
            # token (deadline + transport poll), never duplicated into a second
            # model-phase budget that could race the token's timeout
            # classification.
            max_elapsed_seconds=None,
            max_retries=_DEFAULT_MAX_RETRIES,
            max_directive_repairs=_DEFAULT_MAX_RETRIES,
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
            activity_observer=ctx.liveness_reporter,
            cwd=cwd,
            environment=environment,
        )
        run_id = ctx.run_id or f"{task_id}--{policy_value}"
        # Lower-ladder exact-PDB proof binding (provider-neutral).
        # Interactive Level-32 uses standard pdb-on-uncertainty without mandatory
        # proof gating (LiveModelAdapter.proof_required=False,
        # ControllerRunConfig.require_pdb_evidence_before_patch=False).
        if is_lower_ladder and ladder_scenario is not None:
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
                proof_required=ladder_scenario.runtime_probe.exact_public_reproduction,
                proof_source_line=ladder_scenario.runtime_probe.breakpoint_line,
                proof_observed_local_names=ladder_scenario.runtime_probe.inspect_expressions,
            )
        else:
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
        # Task 44: interactive execution is unbounded — no total-session
        # controller-step ceiling.  ``None`` means the controller serves
        # directives for as long as the protocol permits; step/request
        # counters remain telemetry only.  Historical task-specific
        # ceilings (24 lower / 25 Level 32 / 64 general) are provenance
        # only and never gate execution.
        _max_calls = None
        run_local_session(
            ctx,
            task_id=task_id,
            policy=policy,
            initial_patch=_initial_patch,
            model_factory=_model_factory,
            verifier_patch=_verifier_patch,
            fail_on_controller_failure=True,
            max_model_calls=_max_calls,
            registry_pdb_policy=pdb_policy_for(policy),
            fixture_dir=fixture_dir,
            scenario=ladder_scenario if is_level32 else None,
            repository_root=staging_root,
            # Successful Session Token Efficiency v1: live configured
            # models (stateless LiveModelAdapter) deterministically
            # continue mandatory validation without mechanical requests.
            deterministic_post_patch_validation=True,
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
