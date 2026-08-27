"""Direct canonical Ollama Cloud source for the capability-ladder workflow.

This is intentionally separate from the legacy configured-command source:
the selected alias comes from the repository-owned Ollama roster and no
app-owned model profile file participates in readiness or execution.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from agentic_debugger.application.command_transport import CancellableJsonlCommandTransport
from agentic_debugger.application.events import OperatorStage, SessionEventKind
from agentic_debugger.application.local_source import LocalSourceError, run_local_session
from agentic_debugger.application.sources import ModelExecutionError
from agentic_debugger.application.worker_scenarios import ScenarioContext, ScenarioInputError
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.tools import DemoToolContext
from agentic_debugger.evaluation.live import (
    MAX_MODEL_RESPONSE_BYTES,
    LiveModelAdapter,
    LiveModelConfig,
    LiveRunLimits,
)

OLLAMA_CLOUD_SOURCE_NAME = "ollama_cloud_capability_ladder"
_MAX_ALIAS_CHARS = 128


@dataclass(frozen=True)
class LadderRuntimeContract:
    """Frozen accepted live-treatment bounds for one lower ladder rung."""

    max_model_requests: int
    max_controller_steps: int
    max_model_phase_seconds: int
    max_retries: int = 0


LADDER_RUNTIME_CONTRACTS: dict[str, LadderRuntimeContract] = {
    "pdb-required-boundary-006": LadderRuntimeContract(24, 24, 600),
    "pdb-required-caller-callee-007": LadderRuntimeContract(24, 24, 3600),
    "pdb-required-multistage-units-008": LadderRuntimeContract(24, 24, 3600),
}


def ladder_runtime_contract(task_id: str) -> LadderRuntimeContract:
    """Return the accepted task-specific lower-rung budget envelope."""

    try:
        return LADDER_RUNTIME_CONTRACTS[task_id]
    except KeyError as exc:
        raise ScenarioInputError("task is not an accepted lower ladder rung") from exc


def _progress(ctx: ScenarioContext, stage: OperatorStage) -> None:
    if ctx.emitter is not None:
        ctx.emitter.emit(SessionEventKind.OPERATOR_PROGRESS, {"stage": stage.value})


def _validate(params: Mapping[str, Any]) -> tuple[str, DemoPolicy]:
    if set(params) != {"model_alias", "policy"}:
        raise ScenarioInputError("Ollama Cloud ladder source parameters are invalid")
    alias = params.get("model_alias")
    policy_value = params.get("policy")
    if type(alias) is not str or not alias or len(alias.encode("utf-8")) > _MAX_ALIAS_CHARS:
        raise ScenarioInputError("model_alias must be a bounded non-empty string")
    if type(policy_value) is not str or policy_value not in {item.value for item in DemoPolicy}:
        raise ScenarioInputError("unknown ladder debugger policy")
    return alias, DemoPolicy(policy_value)


def _config(
    alias: str,
    *,
    logical_call_ceiling: int,
    idle_timeout_seconds: int | None = None,
    request_timeout_seconds: int | None = None,
) -> tuple[LiveModelConfig, Any]:
    from scripts.ollama_cloud_command_adapter import EXPECTED_OLLAMA_VERSION, is_treatment_eligible, resolve_cloud_model

    spec = resolve_cloud_model(alias)
    if spec.readiness != "live_verified" or not is_treatment_eligible(spec):
        raise ScenarioInputError("selected Ollama Cloud alias is not eligible")
    root = Path(__file__).resolve().parents[2]
    request_timeout = (
        spec.request_timeout_seconds
        if request_timeout_seconds is None
        else request_timeout_seconds
    )
    idle_timeout = (
        spec.idle_timeout_seconds
        if idle_timeout_seconds is None
        else idle_timeout_seconds
    )
    command: list[str] = [
        sys.executable,
        str(root / "scripts" / "ollama_cloud_command_adapter.py"),
        "--model", spec.local_alias,
        "--timeout", str(int(idle_timeout)),
        "--max-logical-model-calls", str(logical_call_ceiling),
        "--expected-version", EXPECTED_OLLAMA_VERSION,
    ]
    # The adapter's own outer deadline must be explicit whenever it differs
    # from the stream watchdog.  Otherwise the adapter would silently fall
    # back to ``--timeout`` even though the canonical profile carries a
    # separate request bound.
    if request_timeout != idle_timeout:
        command.extend(("--request-timeout", str(int(request_timeout))))
    if spec.thinking_level is not None:
        command.extend(("--thinking-level", spec.thinking_level))
    return LiveModelConfig(
        model_name=spec.local_alias,
        command=tuple(command),
        request_timeout_seconds=request_timeout,
        tool_version="ollama-cloud-adapter-v1.3-ladder",
    ), spec


def build_ollama_live_config(
    alias: str,
    *,
    logical_call_ceiling: int = 32,
    idle_timeout_seconds: int | None = None,
    request_timeout_seconds: int | None = None,
) -> LiveModelConfig:
    """Canonical provider-free Ollama LiveModelConfig for Local Project.

    Reuses :func:`scripts.ollama_cloud_command_adapter.build_ollama_live_config`
    so Local Project and the ladder share one validated construction path.
    """

    from scripts.ollama_cloud_command_adapter import build_ollama_live_config as _build

    return _build(
        alias,
        logical_call_ceiling=logical_call_ceiling,
        idle_timeout_seconds=idle_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )


def run_ollama_cloud_session(ctx: ScenarioContext, params: Mapping[str, Any]) -> None:
    alias, policy = _validate(params)
    if ctx.emitter is None:
        raise ScenarioInputError("Ollama Cloud source requires the shared emitter")
    task_id = ctx.emitter.task_id
    contract = ladder_runtime_contract(task_id)
    scenario = scenario_for(task_id)
    if not scenario.runtime_probe.exact_public_reproduction:
        raise ScenarioInputError("lower ladder rung is missing its exact-PDB scenario contract")
    limits = LiveRunLimits(
        max_model_requests=contract.max_model_requests,
        max_controller_steps=contract.max_controller_steps,
        max_model_phase_seconds=contract.max_model_phase_seconds,
        max_retries=contract.max_retries,
        continue_on_task_failure=False,
    )
    _progress(ctx, OperatorStage.STARTING)
    _progress(ctx, OperatorStage.PREFLIGHT)
    config, profile = _config(
        alias,
        logical_call_ceiling=contract.max_model_requests,
        idle_timeout_seconds=300,
        request_timeout_seconds=contract.max_model_phase_seconds,
    )
    ctx.emitter.emit(
        SessionEventKind.MODEL_CONFIGURED,
        {
            "profile_id": profile.local_alias,
            "config_fingerprint": config.configuration_fingerprint,
            "display_name": profile.upstream_model,
            "protocol_version": "1.3",
            "tool_version": config.tool_version,
        },
    )
    _progress(ctx, OperatorStage.PREPARING_WORKSPACE)
    adapters: list[LiveModelAdapter] = []

    def model_factory(demo_context: DemoToolContext, registry: Any) -> LiveModelAdapter:
        transport = CancellableJsonlCommandTransport(
            config,
            max_output_bytes=MAX_MODEL_RESPONSE_BYTES,
            cancel_check=ctx.token.check,
            activity_observer=ctx.liveness_reporter,
        )
        adapter = LiveModelAdapter(
            task=demo_context.task,
            policy=policy,
            config=config,
            transport=transport,
            limits=limits,
            registry=registry,
            evaluation_id=ctx.emitter.session_id,
            case_id=f"{ctx.emitter.session_id}:{ctx.emitter.task_id}",
            run_id=ctx.run_id or f"{ctx.emitter.session_id}:ladder",
            trajectory_id=ctx.run_id or f"{ctx.emitter.session_id}:ladder",
            proof_required=scenario.runtime_probe.exact_public_reproduction,
            proof_source_line=scenario.runtime_probe.breakpoint_line,
            proof_observed_local_names=scenario.runtime_probe.inspect_expressions,
            progress_observer=lambda stage: _progress(ctx, OperatorStage(stage)),
        )
        adapters.append(adapter)
        return adapter

    def verifier_patch(demo_context: DemoToolContext, result: Any) -> Optional[str]:
        if result is None or result.final_state is not ControllerState.DONE:
            return None
        if not demo_context.patch_applied or not demo_context.candidate_patch:
            return None
        _progress(ctx, OperatorStage.VERIFICATION)
        return demo_context.candidate_patch

    try:
        run_local_session(
            ctx,
            task_id=task_id,
            policy=policy,
            initial_patch=lambda workspace: "",
            model_factory=model_factory,
            verifier_patch=verifier_patch,
            fail_on_controller_failure=True,
            max_model_calls=contract.max_controller_steps,
            registry_pdb_policy=pdb_policy_for(policy),
        )
    except ModelExecutionError:
        raise
    except LocalSourceError as exc:
        raise LocalSourceError(str(exc)) from exc


__all__ = [
    "LADDER_RUNTIME_CONTRACTS",
    "LadderRuntimeContract",
    "OLLAMA_CLOUD_SOURCE_NAME",
    "ladder_runtime_contract",
    "run_ollama_cloud_session",
]
