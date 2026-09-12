"""The Local Project execution-source session orchestration.

This module is the ``local_project`` scenario entry point
(:func:`run_local_project_session`): the single session orchestration
authority that validates scenario params, binds/accepts the V2-02
SessionLaunch, derives the role environments, resolves the model
binding, constructs the honest task + tool context + registry, runs the
deterministic controller, drives the independent verifier, and writes
the durable session artifacts (candidate patch, verification
certificate, canonical task contract, disposition sidecar).

Architecture (one-way dependency graph):

* :mod:`agentic_debugger.application.local_project_helpers` — scenario
  param validation, command splitting/bounding, inventory translation,
  the honest ``LocalProjectTask`` adapter, PDB probe preparation, and
  the sandboxed isolated workspace view;
* :mod:`agentic_debugger.application.local_project_tools` — the isolated
  tool context and controller tool registry construction;
* this module — session orchestration and the public import surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from agentic_debugger.agent.controller import ControllerRunConfig, ControllerStopReason, DeterministicController
from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application.events import SessionEventKind, SessionTerminationReason, contains_credential_shape
from agentic_debugger.application.local_project import assert_path_inside_workspace
from agentic_debugger.application.local_project_helpers import (
    LOCAL_PROJECT_SOURCE_NAME,
    _DEFAULT_MAX_RETRIES,
    _IsolatedWorkspace,
    _UNBOUNDED_LOGICAL_CEILING,
    _bounded,
    _build_local_task,
    _inventory_tracked_python_files,
    _resolve_pdb_probe,
    _run_command_bounded,
    _split_command,
    _validate_params,
)
from agentic_debugger.application.local_project_tools import (
    _LocalToolContext,
    _build_local_registry,
)
from agentic_debugger.application.observability import ObservabilityContext, SessionObservability
from agentic_debugger.application.source_snapshots import SourceSnapshotStage, capture_source_snapshot
from agentic_debugger.application.sources import ModelExecutionError
from agentic_debugger.application.worker_scenarios import ScenarioContext, ScenarioInputError
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.evaluation.live import LiveModelAdapter, LiveRunLimits, MAX_MODEL_RESPONSE_BYTES
from agentic_debugger.evaluation.task_schema import Constraints
from agentic_debugger.runtime.exceptions import (
    PatchApplyError,
    PatchAuthorizationError,
    PatchRevertError,
    PatchStateError,
    PatchValidationError,
)
from agentic_debugger.application.local_project_helpers import LocalProjectSourceError, LocalProjectTask

def run_local_project_session(ctx: ScenarioContext, params: Mapping[str, Any]) -> str:
    """Returns disposition string: FIXED or UNRESOLVED (typed, not sidecar)."""
    validated=_validate_params(params)
    isolated=Path(validated["isolated_workspace"])
    repo_root=Path(validated["project_repo_path"])
    bug_description=validated["bug_description"]
    repro_cmd=validated["reproduction_command"]
    verify_cmd=validated["verification_command"]
    config_root=validated["config_root"]
    expected_fp=validated["expected_fingerprint"]
    if ctx.emitter is None: raise ScenarioInputError("local_project requires emitter")
    if not isolated.is_dir(): raise ScenarioInputError(f"isolated workspace missing: {isolated}")
    # V2-02 SessionLaunch authority: one launch binding per session.  The
    # worker builds it once before dispatch (see ``worker.run_worker``)
    # and carries it on the ScenarioContext so the source, its
    # project/PDB/verifier children, AND terminal worker cleanup all share
    # one authority without recomputing any session-start fact.  Direct
    # (non-worker) callers have no context launch, so the source builds
    # one here through the same factory as the narrow fallback.
    # Project/PDB/verifier children receive explicit derived role
    # environments (declarative project runtime: platform essentials plus
    # the fixed per-session materialization — never arbitrary ambient
    # inheritance); none of them inherits the worker process environment
    # implicitly, so Agentic Debugger control/model/provider channels
    # cannot leak into project execution.  Values stay inside these
    # mappings — they are never logged, journaled, or exposed to the
    # controller/model.
    from agentic_debugger.application.execution_environment import ExecutionEnvironment, ExecutionRole
    from agentic_debugger.application.executor import ProductExecutor
    from agentic_debugger.application.session_runtime import (
        SessionLaunch,
        build_local_project_launch,
    )
    _ctx_launch = getattr(ctx, "session_launch", None)
    if _ctx_launch is not None:
        if type(_ctx_launch) is not SessionLaunch:
            raise ScenarioInputError("local_project session_launch must be a SessionLaunch or None")
        if _ctx_launch.session_id != ctx.emitter.session_id:
            raise ScenarioInputError("session launch identity does not match the session")
        if _ctx_launch.task_id != ctx.emitter.task_id:
            raise ScenarioInputError("session launch task does not match the session")
        # Corroboration-only: legacy transport params may confirm the
        # authoritative launch but never override it.  Any contradiction
        # in a mirrored session-start fact fails closed here, before any
        # project/model execution.  Comparison is by safe
        # representation/fingerprint — never materialized values.
        from agentic_debugger.application.session_runtime import (
            check_launch_matches_params,
        )
        try:
            check_launch_matches_params(
                _ctx_launch,
                policy=validated["policy"],
                provider_id=validated["provider"],
                model_id=validated["model_id"],
                profile_id=validated["profile_id"],
                project_spec=validated["project_runtime_spec"],
            )
        except Exception as exc:
            raise ScenarioInputError(f"session launch mismatch: {exc}") from exc
        session_launch = _ctx_launch
    else:
        _ctx_authority = getattr(ctx, "product_environment", None)
        if _ctx_authority is not None and not isinstance(_ctx_authority, ExecutionEnvironment):
            raise ScenarioInputError("local_project product_environment must be an ExecutionEnvironment or None")
        if _ctx_authority is not None and _ctx_authority.uses_legacy_bridge:
            raise ScenarioInputError("local_project requires a declarative session authority")
        try:
            session_launch = build_local_project_launch(
                session_id=ctx.emitter.session_id,
                task_id=ctx.emitter.task_id,
                policy=validated["policy"],
                provider_id=validated["provider"],
                model_id=validated["model_id"],
                profile_id=validated["profile_id"],
                launch_snapshot=dict(os.environ),
                project_spec=validated["project_runtime_spec"],
                is_ollama=validated["is_ollama"],
                ollama_alias=validated["ollama_alias"],
                config_root=validated["config_root"],
            )
        except Exception as exc:
            raise ScenarioInputError(f"session launch failed: {exc}") from exc
    execution_environment = session_launch.execution_environment
    project_command_environment=execution_environment.role_environment(ExecutionRole.PROJECT_COMMAND)
    pdb_worker_environment=execution_environment.role_environment(ExecutionRole.PRODUCT_PDB)
    verifier_command_environment=execution_environment.role_environment(ExecutionRole.VERIFIER)
    session_executor = ProductExecutor(
        execution_environment=execution_environment,
        capabilities=session_launch.capabilities,
    )
    session_capabilities = session_launch.capabilities
    # Single-authority rebind: from here on, the session-start facts owned
    # by SessionLaunch come from the launch, never from the mirrored
    # validated transport params above (those were only its construction
    # input for the fallback, or corroboration for a supplied launch).
    # Genuinely source-specific facts (repository/worktree paths, bug
    # description, repro/verify commands, config root, legacy Ollama
    # routing markers) stay on the validated params.
    policy = DemoPolicy(session_launch.agent.controller_policy)
    provider = session_launch.agent.provider_id
    model_id = session_launch.agent.model_id
    profile_id = session_launch.profile_id
    is_ollama = validated["is_ollama"]
    ollama_alias = validated["ollama_alias"] or (
        profile_id if provider is None and is_ollama else None
    )

    from agentic_debugger.application.model_gateway import ModelGateway
    # Task 44 (Unbounded Session Progress v1): Local Project execution
    # is unbounded.  SessionBudgets count dimensions and the historical
    # Local Project default (32) are telemetry/provenance only and are
    # never consulted for execution authority.  The adapter receives the
    # unbounded sentinel (0), the LiveModelAdapter and controller receive
    # None.  Time/retry/repair/response bounds below are unchanged.
    max_model_requests = _UNBOUNDED_LOGICAL_CEILING
    gateway = ModelGateway.default(config_root=config_root)
    model_binding = session_launch.model_binding
    if model_binding is None:
        model_binding = gateway.resolve(
            provider_id=provider,
            model_id=model_id,
            profile_id=profile_id,
            logical_call_ceiling=max_model_requests,
            is_ollama=is_ollama,
            ollama_alias=ollama_alias,
            config_root=config_root,
        )

    if expected_fp is not None and model_binding.config_fingerprint is not None and model_binding.config_fingerprint != expected_fp:
        raise ScenarioInputError("model profile fingerprint mismatch")
    # Session/task identity likewise comes from the launch (proven equal
    # to the emitter binding by the checks above / by fallback
    # construction); run identity stays on the context (not launch-owned).
    session_id=session_launch.session_id
    task_id=session_launch.task_id
    source_kind=ctx.emitter.source_kind
    run_id=ctx.run_id or f"{task_id}--local"
    observability=SessionObservability(ObservabilityContext(session_id=session_id, task_id=task_id, source_kind=source_kind, run_id=ctx.run_id), emitter=ctx.emitter)
    ctx.token.check()
    observability.diagnosis_recorded(text=bug_description, file_path=None, symbol=None, confidence="user-reported", observed_values={"repo_basename": repo_root.name, "source_head": validated["project_head"][:12]})
    tracked=_inventory_tracked_python_files(isolated, environment=project_command_environment)
    local_task, initial_state=_build_local_task(bug_description, repro_cmd, verify_cmd, isolated, tracked)
    if repro_cmd:
        # Baseline reproduction runs through the session Executor seam
        # (fixed PROJECT_COMMAND role environment, capability-gated).
        from agentic_debugger.runtime.exceptions import CommandExecutionError as _InitialCommandError
        _start = time.monotonic()
        try:
            _initial_argv = _split_command(repro_cmd)
        except ValueError as exc:
            exit_code, out, err = 127, "", f"parse failed: {exc}"
        else:
            if not _initial_argv:
                exit_code, out, err = 127, "", "empty"
            else:
                try:
                    _initial_result = session_executor.run_project_command(
                        _initial_argv, _IsolatedWorkspace(isolated), 30.0,
                        cancel_check=ctx.token.check,
                    )
                except _InitialCommandError as exc:
                    exit_code, out, err = 127, "", f"launch failed: {exc}"
                else:
                    if _initial_result.exit_code is not None:
                        exit_code = _initial_result.exit_code
                    elif _initial_result.timed_out:
                        exit_code = 124
                    else:
                        exit_code = 127
                    out, err = _initial_result.stdout or "", _initial_result.stderr or ""
                    if _initial_result.timed_out:
                        err = (err + " timed out 30.0s").strip()
                    out, err = _bounded(out), _bounded(err)
        repro_output=_bounded(out+err, 2000)
        try:
            observability.diagnosis_recorded(text=f"reproduction result exit {exit_code}: {repro_output[:500]}", file_path=None, symbol=None, confidence="observed")
        except: pass
        initial_state=ControllerState.REPRODUCE
    else:
        initial_state=ControllerState.UNDERSTAND
        try:
            observability.diagnosis_recorded(text="no reproduction command supplied; starting from bug description and source inspection", file_path=None, symbol=None, confidence="observed")
        except: pass
    probe=_resolve_pdb_probe(repro_cmd, isolated, pdb_policy_for(policy))
    ctx.token.check()
    ctx.emitter.emit(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"})
    for rel in tracked[:3]:
        try:
            assert_path_inside_workspace(isolated, rel)
            snap=capture_source_snapshot(isolated, rel, SourceSnapshotStage.INITIAL)
            observability.source_snapshot(snap)
        except Exception:
            continue
    demo_context=_LocalToolContext(isolated=isolated, tracked=tracked, task=local_task, probe=probe, observability=observability, command_environment=project_command_environment, pdb_worker_environment=pdb_worker_environment, executor=session_executor, capabilities=session_capabilities)
    registry=_build_local_registry(demo_context, pdb_policy=pdb_policy_for(policy), interactive_debugger_controls=False)
    # Task 44: unbounded — request/step dimensions are None (telemetry
    # only).  Retry/repair/response/time dimensions are unchanged.
    limits=LiveRunLimits(max_model_requests=None, max_controller_steps=None, max_elapsed_seconds=None, max_retries=_DEFAULT_MAX_RETRIES, max_directive_repairs=_DEFAULT_MAX_RETRIES, max_response_bytes=MAX_MODEL_RESPONSE_BYTES)
    try:
        transport, live_config = gateway.create_transport(
            model_binding,
            cancel_check=ctx.token.check,
            activity_observer=ctx.liveness_reporter,
            max_model_requests=_UNBOUNDED_LOGICAL_CEILING,
            max_controller_steps=_UNBOUNDED_LOGICAL_CEILING,
            max_response_bytes=MAX_MODEL_RESPONSE_BYTES,
            credential_binding=session_launch.credential_binding,
            credential_ticket=session_launch.credential_ticket,
            session_id=session_id,
        )
    except Exception as exc:
        raise ScenarioInputError(f"model profile unavailable: {exc}") from exc

    model_provenance_payload = model_binding.model_configured_payload()
    ctx.emitter.emit(SessionEventKind.MODEL_CONFIGURED, model_provenance_payload)
    adapters: list[Any]=[]
    def _model_factory(dctx, reg):  # type: ignore[no-untyped-def]
        adapter=LiveModelAdapter(task=dctx.task, policy=policy, config=live_config, transport=transport, limits=limits, registry=reg, evaluation_id=session_id, case_id=f"{session_id}:{task_id}", run_id=run_id, trajectory_id=run_id)
        adapters.append(adapter)
        return adapter
    from agentic_debugger.application.controller_adapter import ControllerSessionEventAdapter, ControllerObservationContext
    controller_obs=ControllerSessionEventAdapter(ControllerObservationContext(session_id=session_id, task_id=task_id, source_kind=source_kind, run_id=ctx.run_id), emitter=ctx.emitter)
    snapshot=ControllerSnapshot(run_id=run_id, task_id=task_id, state=initial_state, model_call_index=0, budget_limits=ControllerBudgetLimits.from_task_constraints(local_task.constraints), budget_state=ControllerBudgetState(), hypotheses=HypothesisLedger())
    model=_model_factory(demo_context, registry)
    # Task 44: unbounded controller (None = no total-session ceiling).
    controller=DeterministicController(registry, model, ControllerRunConfig(max_model_calls=None, require_pdb_evidence_before_patch=False), observer=controller_obs)
    try:
        result=controller.run(snapshot, cancel_check=ctx.token.check)
    except ModelExecutionError:
        raise
    except Exception as exc:
        if adapters:
            tr=adapters[-1].metrics.termination_reason
            if tr:
                raise ModelExecutionError(f"{exc} (model transport: {tr})", SessionTerminationReason.MODEL_ERROR if tr=="model_error" else SessionTerminationReason.CONTROLLER_FAILED) from exc
        raise LocalProjectSourceError(f"controller failed: {exc}") from exc
    ctx.token.check()
    has_active_candidate=bool(
        demo_context.patch_applied and demo_context.candidate_patch
    )
    if result.stop_reason is not ControllerStopReason.DONE:
        # A controller classification is not the correctness authority.  If
        # it left an active, schema-valid candidate, retain the candidate and
        # let the independent verifier decide.  Transport failure or a run
        # with no candidate remains an operational controller failure.
        from agentic_debugger.application.local_source import _controller_failure_category
        _, term_reason=_controller_failure_category(result)
        transport_reason=(
            adapters[-1].metrics.termination_reason if adapters else None
        )
        if transport_reason:
            ctx.emitter.emit(SessionEventKind.DIAGNOSIS_RECORDED, {"text": f"controller did not complete: {result.stop_reason.value}", "file_path": None, "symbol": None, "confidence": "observed"})
            raise ModelExecutionError(f"controller run ended without completion (stop: {result.stop_reason.value}) (model transport: {transport_reason})", term_reason)
        if not has_active_candidate:
            ctx.emitter.emit(SessionEventKind.DIAGNOSIS_RECORDED, {"text": f"controller did not complete: {result.stop_reason.value}", "file_path": None, "symbol": None, "confidence": "observed"})
            raise ModelExecutionError(f"controller run ended without completion (stop: {result.stop_reason.value})", term_reason)
        ctx.emitter.emit(
            SessionEventKind.DIAGNOSIS_RECORDED,
            {
                "text": (
                    "controller stopped without a success claim; active candidate "
                    "retained for independent verification"
                ),
                "file_path": None,
                "symbol": None,
                "confidence": "system",
            },
        )
    patch_text: Optional[str]=demo_context.candidate_patch if has_active_candidate else None
    verification_result: Optional[Any]=None
    verified_fixed=False
    from agentic_debugger.application.session_runtime import SessionCapability as _VerifierCapability
    verifier_granted = session_capabilities.has(_VerifierCapability.VERIFIER)
    if has_active_candidate and patch_text is not None and verifier_granted:
        from agentic_debugger.application.verifier_observer import (
            VerifierSessionEventAdapter,
        )
        from agentic_debugger.evaluation.local_project_verifier import (
            LocalProjectEvaluationPlan,
            LocalProjectVerifier,
        )

        verifier_events=VerifierSessionEventAdapter(
            ObservabilityContext(
                session_id=session_id,
                task_id=task_id,
                source_kind=source_kind,
                run_id=ctx.run_id,
            ),
            emitter=ctx.emitter,
        )
        verifier_events.started()
        verification_plan=LocalProjectEvaluationPlan(
            source_repo_path=str(repo_root),
            source_head_commit=validated["project_head"],
            candidate_patch=patch_text,
            reproduction_argv=(tuple(_split_command(repro_cmd)) if repro_cmd else None),
            regression_argv=(tuple(_split_command(verify_cmd)) if verify_cmd else None),
            allowed_paths=tuple(tracked),
            denied_paths=("tests", "task.json"),
            timeout_seconds=30.0,
            workspace_parent=validated.get("parent_tmpdir"),
        )
        # V2-02 verifier environment seam (single fixed authority): the
        # session execution authority supplies ONE verifier-role mapping
        # (declarative project runtime — no Agentic Debugger
        # control/provider authority) plus the SAME per-session
        # project-secret redaction authority (derived by the launch
        # environment from the one materialization — never a second,
        # independently resolvable one).  The verifier copies the mapping
        # once and uses it for BOTH its CommandRunner children and its
        # owned Git subprocesses; the environment is never a model-selected
        # tool argument and cannot be mutated once verification begins.
        independent_verifier=LocalProjectVerifier(
            progress_observer=verifier_events,
            cancel_check=ctx.token.check,
            product_environment=dict(verifier_command_environment),
            product_secret_redactor=execution_environment.project_secret_redactor(),
        )
        verification_result=independent_verifier.evaluate(verification_plan)
        verifier_events.completed(verification_result)
        verified_fixed=verification_result.resolved
    elif has_active_candidate and patch_text is not None and not verifier_granted:
        ctx.emitter.emit(
            SessionEventKind.DIAGNOSIS_RECORDED,
            {
                "text": "independent verification not run: verifier capability is not available in this session",
                "file_path": None,
                "symbol": None,
                "confidence": "system",
            },
        )
    else:
        ctx.emitter.emit(
            SessionEventKind.DIAGNOSIS_RECORDED,
            {
                "text": "independent verification not run: no active candidate patch",
                "file_path": None,
                "symbol": None,
                "confidence": "system",
            },
        )
    session_dir=ctx.session_dir
    disposition="FIXED" if verified_fixed else "UNRESOLVED"
    artifact_failures: list[str]=[]
    if session_dir is not None:
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            artifact_failures.append(f"session directory unavailable: {exc}")
        if has_active_candidate and patch_text and not contains_credential_shape(patch_text):
            try:
                candidate_path=session_dir / "candidate.patch"
                candidate_path.write_text(patch_text, encoding="utf-8", newline="\n")
                sha=hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
                ctx.emitter.emit(SessionEventKind.ARTIFACT_WRITTEN, {"path": "candidate.patch", "sha256": sha})
                for rel in tracked[:1]:
                    try:
                        snap=capture_source_snapshot(isolated, rel, SourceSnapshotStage.APPLIED)
                        observability.source_snapshot(snap)
                        break
                    except Exception:
                        continue
            except Exception as exc:
                # The candidate artifact is required for Apply To Project;
                # its absence must be visible, not silent.
                artifact_failures.append(f"candidate.patch write failed: {exc}")
        if verification_result is not None:
            try:
                from agentic_debugger.application.local_project import (
                    LOCAL_PROJECT_VERIFICATION_FILE_NAME,
                    LocalProjectTaskSpec,
                    LocalProjectVerificationCertificate,
                    local_project_task_spec_sha256,
                )
                from agentic_debugger.evaluation.runner import TestRecordStatus

                task_spec = LocalProjectTaskSpec.from_mapping(
                    json.loads(
                        (session_dir / "local_project_task.json").read_text(
                            encoding="utf-8"
                        )
                    )
                )
                certificate=LocalProjectVerificationCertificate(
                    task_id=task_id,
                    session_id=task_spec.session_id,
                    task_spec_sha256=local_project_task_spec_sha256(task_spec),
                    source_head_commit=verification_result.source_head_commit,
                    candidate_sha256=verification_result.candidate_sha256,
                    status=verification_result.status.value,
                    outcome=(
                        verification_result.outcome.value
                        if verification_result.outcome is not None
                        else None
                    ),
                    baseline_failure_reproduced=bool(
                        verification_result.baseline_reproduction is not None
                        and verification_result.baseline_reproduction.status
                        is TestRecordStatus.FAIL
                    ),
                    baseline_regression_passed=bool(
                        verification_result.baseline_regression is not None
                        and verification_result.baseline_regression.passed
                    ),
                    post_patch_reproduction_passed=bool(
                        verification_result.post_patch_reproduction is not None
                        and verification_result.post_patch_reproduction.passed
                    ),
                    regression_passed=bool(
                        verification_result.regression is not None
                        and verification_result.regression.passed
                    ),
                    f2p_passed=verification_result.f2p_passed,
                    f2p_total=verification_result.f2p_total,
                    p2p_passed=verification_result.p2p_passed,
                    p2p_total=verification_result.p2p_total,
                    verifier_workspace_cleaned=verification_result.workspace.cleaned,
                    source_repo_unchanged=(
                        verification_result.workspace.canonical_fixture_unchanged
                    ),
                )
                certificate_path=session_dir / LOCAL_PROJECT_VERIFICATION_FILE_NAME
                certificate_path.write_text(
                    json.dumps(certificate.to_mapping(), indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                certificate_sha=hashlib.sha256(certificate_path.read_bytes()).hexdigest()
                ctx.emitter.emit(
                    SessionEventKind.ARTIFACT_WRITTEN,
                    {
                        "path": LOCAL_PROJECT_VERIFICATION_FILE_NAME,
                        "sha256": certificate_sha,
                    },
                )
            except Exception as exc:
                artifact_failures.append(
                    f"local_project_verification.json write failed: {exc}"
                )
        try:
            # Keep the canonical artifact authoritative.  The app pre-writes
            # this file as ``LocalProjectTaskSpec.to_mapping()`` before the
            # worker starts; the session must not replace it with a second,
            # incompatible schema (Apply To Project and history/reopen read
            # the canonical ``source_repo_path`` / ``source_head_commit``).
            # ``from_mapping`` is read strictly (unknown keys rejected) and
            # this writer is the same authority that produced the file, so a
            # failure here is an honest artifact failure, not a silent
            # rewrite of the contract.
            task_path=session_dir / "local_project_task.json"
            from agentic_debugger.application.local_project import (
                LocalProjectTaskSpec,
                SessionBudgets,
            )
            _spec=LocalProjectTaskSpec.from_mapping(json.loads(task_path.read_text(encoding="utf-8")))
            _new_spec=LocalProjectTaskSpec(
                session_id=_spec.session_id,
                source_repo_path=_spec.source_repo_path,
                source_head_commit=_spec.source_head_commit,
                isolated_workspace_path=_spec.isolated_workspace_path,
                bug_description=_spec.bug_description,
                reproduction_command=_spec.reproduction_command,
                verification_command=_spec.verification_command,
                model_runtime=_spec.model_runtime,
                budgets=SessionBudgets(**_spec.budgets.to_mapping()),
                created_at_utc=_spec.created_at_utc,
                project_runtime=dict(_spec.project_runtime),
            )
            task_path.write_text(
                json.dumps(_new_spec.to_mapping(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as exc:
            artifact_failures.append(f"local_project_task.json write failed: {exc}")
        # Audit sidecar (not authority; the typed return is authoritative).
        try:
            disp_path=session_dir / "local_project_disposition.json"
            disp_path.write_text(
                json.dumps(
                    {
                        "disposition": disposition,
                        "has_active_candidate": has_active_candidate,
                        "verifier_status": (
                            verification_result.status.value
                            if verification_result is not None
                            else None
                        ),
                        "verifier_outcome": (
                            verification_result.outcome.value
                            if verification_result is not None
                            and verification_result.outcome is not None
                            else None
                        ),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            artifact_failures.append(f"local_project_disposition.json write failed: {exc}")
    for failure in artifact_failures[:4]:
        try:
            ctx.emitter.emit(SessionEventKind.DIAGNOSIS_RECORDED, {"text": _bounded(failure, 400), "file_path": None, "symbol": None, "confidence": "system"})
        except Exception:
            pass
    ctx.emitter.emit(SessionEventKind.CONTROLLER_STEP, {"step_index": 2, "directive_kind": "verification", "stop_reason": "done" if verified_fixed else "unresolved"})
    ctx.token.check()
    return disposition

__all__=["LOCAL_PROJECT_SOURCE_NAME","LocalProjectTask","run_local_project_session"]
