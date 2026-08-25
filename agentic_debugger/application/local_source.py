"""Shared production local execution pipeline (Task 8).

Task 7's deterministic offline source and Task 8's configured command-model
source run the same accepted application execution pipeline: real
:class:`DeterministicController`, the real tool registry, real PDB, the real
PatchManager, the real independent :class:`EvaluationVerifier`, and a
disposable task workspace, all inside the accepted Task-3 worker process
with one shared :class:`SessionEventEmitter` (the session's single emission
authority and durable journal).

:func:`run_local_session` is that one shared pipeline; the meaningful
difference between the two supported modes is *model construction* (plus
the source-specific failure semantics):

- ``initial_patch(workspace)`` — the candidate the tool context starts with
  (deterministic: the catalog reference patch; configured: ``""``, the
  model proposes the patch through the real ``apply_patch`` tool);
- ``model_factory(demo_context, registry)`` — the model adapter
  (deterministic: :class:`DemoPolicyModel`; configured:
  :class:`LiveModelAdapter` over the accepted JSON-lines command
  transport);
- ``verifier_patch(demo_context, result)`` — the patch the independent
  verifier evaluates, or ``None`` to skip verification (deterministic: the
  reference patch; configured: the actually-applied candidate patch, only
  when the controller completed and a patch is applied);
- ``fail_on_controller_failure`` — the configured path fails the session
  honestly (``ModelExecutionError`` with the exact Task-1 termination
  reason) when the controller did not complete; the deterministic path
  preserves the accepted Task-7 completion semantics.

Rules carried over from Task 7:

- It is NOT a synthetic ``worker_scenarios`` mode; every produced event is
  the truthful projection of a real operation.
- Cancellation flows through the accepted Task-3 token; it is never
  converted into a scientific verdict or a fabricated terminal.
- The disposable execution workspace lives under the worker-owned work
  directory and is released by this pipeline (best effort) and by the
  worker cleanup cycle (authoritative).  Persisted app-owned artifacts
  (``candidate.patch``, ``evaluation.json``) are written into the durable
  session directory, never into the disposable work directory.
- Source snapshots are captured by the real demo tool handlers through
  ``SessionObservability``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerRunResult,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
    PdbPolicy,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application.controller_adapter import (
    ControllerObservationContext,
    ControllerSessionEventAdapter,
)
from agentic_debugger.application.events import (
    SessionEventKind,
    SessionTerminationReason,
    contains_credential_shape,
)
from agentic_debugger.application.observability import (
    ObservabilityContext,
    SessionObservability,
)
from agentic_debugger.application.sources import ModelExecutionError
from agentic_debugger.application.verifier_observer import (
    VerifierSessionEventAdapter,
)
from agentic_debugger.application.worker_scenarios import (
    ScenarioContext,
    ScenarioInputError,
)
from agentic_debugger.cancellation import CancellationError
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.isolation import OfflineGuard
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.tools import (
    DemoToolContext,
    build_registry,
    prepare_pdb_probe,
)
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.runtime.workspace import TaskWorkspace

#: App-owned artifact names persisted into the durable session directory.
CANDIDATE_PATCH_NAME = "candidate.patch"
EVALUATION_JSON_NAME = "evaluation.json"

_MAX_DIAGNOSTIC_CHARS = 400


class LocalSourceError(RuntimeError):
    """The production local execution pipeline itself failed.

    Never raised for scientific outcomes (the verifier remains the
    correctness authority) and never for cancellation (the neutral
    :class:`CancellationError` owns that path).
    """


def _bounded_diagnostic(text: str) -> str:
    cleaned = "".join(
        char if 0x20 <= ord(char) != 0x7F else " " for char in str(text)
    )
    if len(cleaned) > _MAX_DIAGNOSTIC_CHARS:
        cleaned = cleaned[: _MAX_DIAGNOSTIC_CHARS - 3] + "..."
    return cleaned or "unspecified"


def _curated_fixture_dir(task_id: str) -> Path:
    import agentic_debugger

    package_dir = Path(agentic_debugger.__file__).resolve().parent
    fixture_dir = package_dir / "datasets" / "curated" / task_id
    if not (fixture_dir / "task.json").is_file():
        raise ScenarioInputError(f"curated task manifest is missing: {task_id}")
    return fixture_dir


def _repository_root() -> Path:
    import agentic_debugger

    return Path(agentic_debugger.__file__).resolve().parent.parent


def run_local_session(
    ctx: ScenarioContext,
    *,
    task_id: str,
    policy: DemoPolicy,
    initial_patch: Callable[[TaskWorkspace], str],
    model_factory: Callable[[DemoToolContext, Any], Any],
    verifier_patch: Callable[[DemoToolContext, ControllerRunResult], Optional[str]],
    fail_on_controller_failure: bool,
    max_model_calls: int,
    registry_pdb_policy: Optional[PdbPolicy] = None,
) -> None:
    """Execute one real local debugging session through the shared pipeline.

    Runs inside the worker process after ``session.started`` (the work
    directory exists).  Every event flows through ``ctx.emitter`` (the
    session's single shared emission authority); cancellation honors
    ``ctx.token`` at every safe boundary.
    """
    if ctx.emitter is None:
        raise ScenarioInputError("local execution source requires the shared emitter")
    if type(policy) is not DemoPolicy:
        raise ScenarioInputError("local execution source requires a validated policy")
    policy_value = policy.value
    pdb_mode = pdb_policy_for(policy)
    session_id = ctx.emitter.session_id
    task_name = ctx.emitter.task_id
    source_kind = ctx.emitter.source_kind
    if task_id != task_name:
        raise ScenarioInputError(
            f"source task {task_id!r} does not match the "
            f"session task {task_name!r}"
        )

    fixture_dir = _curated_fixture_dir(task_id)
    scenario = scenario_for(task_id)
    case_parent = ctx.work_dir / f"case-{task_id}-{policy_value}"
    diagnostics: list[str] = []

    observability = SessionObservability(
        ObservabilityContext(
            session_id=session_id,
            task_id=task_id,
            source_kind=source_kind,
            run_id=ctx.run_id,
        ),
        emitter=ctx.emitter,
    )
    controller_adapter = ControllerSessionEventAdapter(
        ControllerObservationContext(
            session_id=session_id,
            task_id=task_id,
            source_kind=source_kind,
            run_id=ctx.run_id,
        ),
        emitter=ctx.emitter,
    )

    workspace: Optional[TaskWorkspace] = None
    demo_context: Optional[DemoToolContext] = None
    evaluation = None
    try:
        ctx.token.check()  # cancellation boundary before any execution work
        case_parent.mkdir(parents=False, exist_ok=False)
        guard = OfflineGuard()
        with guard:
            workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_parent))
            patch_text = initial_patch(workspace)
            task = load_task(str(fixture_dir / "task.json"))
            if scenario.runtime_probe.exact_public_reproduction:
                # The controller/model workspace is public task state.  The
                # verifier retains the canonical task separately.
                (Path(workspace.root) / "task.json").write_text(
                    json.dumps(task.agent_visible_mapping(), sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
            probe = (
                prepare_pdb_probe(fixture_dir, scenario, case_parent, task=task)
                if pdb_mode is not PdbPolicy.DISABLED
                else None
            )
            demo_context = DemoToolContext(
                task=task,
                workspace=workspace,
                patch=patch_text,
                probe=probe,
                observability=observability,
            )
            registry = build_registry(
                demo_context,
                pdb_policy=registry_pdb_policy,
                interactive_debugger_controls=(
                    scenario.runtime_probe.exact_public_reproduction
                ),
            )
            model = model_factory(demo_context, registry)
            controller = DeterministicController(
                registry,
                model,
                ControllerRunConfig(
                    max_model_calls=max_model_calls,
                    require_pdb_evidence_before_patch=(
                        scenario.runtime_probe.exact_public_reproduction
                    ),
                ),
                observer=controller_adapter,
            )
            task = demo_context.task
            snapshot = ControllerSnapshot(
                run_id=ctx.run_id or f"{task_id}--{policy_value}",
                task_id=task_id,
                state=ControllerState.REPRODUCE,
                model_call_index=0,
                budget_limits=ControllerBudgetLimits.from_task_constraints(
                    task.constraints
                ),
                budget_state=ControllerBudgetState(),
                hypotheses=HypothesisLedger(),
            )
            result = controller.run(snapshot, cancel_check=ctx.token.check)
            ctx.token.check()  # controller -> verifier boundary

            if fail_on_controller_failure and result.stop_reason is not ControllerStopReason.DONE:
                raise ModelExecutionError(
                    "controller run ended without completion "
                    f"(stop: {result.stop_reason.value})",
                    _termination_reason_for(result.stop_reason),
                )

            # Independent verifier: progress events through the shared
            # emitter; the final EvaluationResult stays the only authority.
            # ``verifier_patch`` returns None when there is nothing to
            # verify honestly (configured runs without an applied candidate).
            verification_patch = verifier_patch(demo_context, result)
            if verification_patch is not None:
                ctx.emitter.emit(
                    SessionEventKind.SESSION_STATUS_CHANGED,
                    {"status": "running", "phase": "verifying"},
                )
                verifier_adapter = VerifierSessionEventAdapter(
                    ObservabilityContext(
                        session_id=session_id,
                        task_id=task_id,
                        source_kind=source_kind,
                        run_id=ctx.run_id,
                    ),
                    emitter=ctx.emitter,
                )
                verifier_adapter.started()
                evaluation = EvaluationVerifier(
                    str(_repository_root()),
                    # Keep verifier workspaces outside the application-owned
                    # session tree.  The UI stores sessions under the
                    # repository's .ui-review directory; placing pytest's
                    # disposable verifier copy there lets repository config
                    # files become its rootdir and changes collected node
                    # identities.  EvaluationVerifier owns and cleans its
                    # default system-temp workspace, while the controller
                    # workspace remains under case_parent and is released
                    # below.
                    progress_observer=verifier_adapter,
                    cancel_check=ctx.token.check,
                ).evaluate(task, verification_patch)
                verifier_adapter.completed(evaluation)
                _persist_artifacts(ctx, task_id, verification_patch, evaluation)
    except (CancellationError, ModelExecutionError):
        raise
    except Exception as exc:
        raise LocalSourceError(
            f"local session failed: {_bounded_diagnostic(exc)}"
        ) from exc
    finally:
        _release(demo_context, workspace, case_parent, diagnostics)
        if diagnostics and not ctx.token.is_cancelled:
            # Cleanup truth stays with the worker cleanup cycle when the
            # session was cancelled: a cancellation must not be flipped into
            # a generic failure by a best-effort release diagnostic.
            raise LocalSourceError(
                "local session cleanup failed: "
                + "; ".join(diagnostics[:4])
            )


def _termination_reason_for(stop_reason: ControllerStopReason) -> SessionTerminationReason:
    """Map a failed controller stop to the honest Task-1 termination reason."""
    if stop_reason is ControllerStopReason.MODEL_ERROR:
        return SessionTerminationReason.MODEL_ERROR
    if stop_reason is ControllerStopReason.DIRECTIVE_REJECTED:
        return SessionTerminationReason.DIRECTIVE_EXHAUSTED
    if stop_reason is ControllerStopReason.MODEL_CALL_LIMIT:
        return SessionTerminationReason.DIRECTIVE_EXHAUSTED
    return SessionTerminationReason.CONTROLLER_FAILED


def _persist_artifacts(
    ctx: ScenarioContext,
    task_id: str,
    patch_text: str,
    evaluation: Any,
) -> None:
    """Write the app-owned artifacts into the durable session directory.

    The session directory (journal parent) survives the disposable work
    directory cleanup, so a reopened/replayed session keeps its candidate
    patch and verifier record.  Every artifact is emitted through the shared
    emission authority with its exact content hash.

    The durable ``candidate.patch`` body is gated by the same shared
    credential-content policy the application evidence uses: a patch whose
    content matches the policy is withheld from the durable artifact (the
    raw body is never persisted and never redacted into a fake original).
    Truthful patch identity/hash/lifecycle remain available through the
    already-recorded safe patch events, and the history manifest only
    references artifacts that were actually written, so a withheld body is
    never claimed.  The in-memory candidate the verifier evaluated is
    unchanged by this gate.
    """
    session_dir = ctx.session_dir
    if session_dir is None:
        return
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    if not contains_credential_shape(patch_text):
        candidate_path = session_dir / CANDIDATE_PATCH_NAME
        candidate_path.write_text(patch_text, encoding="utf-8", newline="\n")
        ctx.emitter.emit(
            SessionEventKind.ARTIFACT_WRITTEN,
            {
                "path": CANDIDATE_PATCH_NAME,
                "sha256": _file_sha256(candidate_path),
            },
        )
    evaluation_path = session_dir / EVALUATION_JSON_NAME
    evaluation_path.write_text(
        json.dumps(evaluation.to_mapping(), ensure_ascii=False, allow_nan=False, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    ctx.emitter.emit(
        SessionEventKind.ARTIFACT_WRITTEN,
        {
            "path": EVALUATION_JSON_NAME,
            "sha256": _file_sha256(evaluation_path),
        },
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release(
    demo_context: Optional[DemoToolContext],
    workspace: Optional[TaskWorkspace],
    case_parent: Path,
    diagnostics: list[str],
) -> None:
    """Best-effort release of every disposable execution resource.

    Mirrors the accepted demo ``_release`` behavior; the worker cleanup
    cycle remains the authoritative cleanup owner.
    """
    if demo_context is not None:
        for error in demo_context.release_pdb():
            diagnostics.append(
                f"pdb cleanup failed: {_bounded_diagnostic(error)}"
            )
    if workspace is not None:
        try:
            workspace.cleanup()
            if Path(workspace.root).exists():
                raise RuntimeError("controller workspace root remains after cleanup")
        except Exception as exc:
            diagnostics.append(
                f"controller workspace cleanup failed: {_bounded_diagnostic(exc)}"
            )
    try:
        if case_parent.exists():
            shutil.rmtree(case_parent)
        if case_parent.exists():
            diagnostics.append("case workspace parent still exists after cleanup")
    except Exception as exc:
        diagnostics.append(
            f"case workspace cleanup failed: {_bounded_diagnostic(exc)}"
        )


__all__ = [
    "CANDIDATE_PATCH_NAME",
    "EVALUATION_JSON_NAME",
    "LocalSourceError",
    "run_local_session",
]
