"""Production deterministic offline execution source (Task 7).

This is the real application execution source that Task 3's internal
scenario harness explicitly deferred.  It binds the accepted deterministic
repository stack -- the real :class:`DeterministicController`, the real tool
registry (``demo.tools.build_registry``), real PDB, the real PatchManager,
the real independent :class:`EvaluationVerifier`, and a disposable task
workspace -- into one application session lifecycle running inside the
accepted Task-3 worker process.

Rules:

- It is NOT a synthetic ``worker_scenarios`` mode: every produced event is
  the truthful projection of a real operation through the session's single
  shared emission authority (the coordinator's :class:`SessionEventEmitter`
  / durable journal), and every pane-visible fact corresponds to an actual
  executed controller/debugger/patch/verifier step.
- It never duplicates the debugging engine: it reuses the accepted demo
  composition blocks (``DemoToolContext``, ``build_registry``,
  ``DemoPolicyModel``, ``OfflineGuard``) and only factors the orchestration
  needed to make that stack an application execution source.
- Cooperative cancellation flows through the accepted Task-3 token into the
  controller and verifier checkpoints; cancellation is never converted into
  a scientific verdict or a fabricated terminal.
- The session's disposable execution workspace lives under the worker-owned
  work directory and is released by this source (best effort) and by the
  worker cleanup cycle (authoritative).  Persisted app-owned artifacts
  (``candidate.patch``, ``evaluation.json``) are written into the durable
  session directory, never into the disposable work directory, so history
  remains replayable after cleanup.
- Source snapshots (initial/applied) are captured by the real demo tool
  handlers through ``SessionObservability``; source stays displayable after
  the disposable workspace is removed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping, Optional

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
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
from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.controller_adapter import (
    ControllerObservationContext,
    ControllerSessionEventAdapter,
)
from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.events import SessionEventKind
from agentic_debugger.application.observability import (
    ObservabilityContext,
    SessionObservability,
)
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
from agentic_debugger.demo.model import DemoPolicyModel
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.runner import DEMO_MAX_MODEL_CALLS
from agentic_debugger.demo.tools import (
    DemoToolContext,
    build_registry,
    prepare_pdb_probe,
)
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.runtime.workspace import TaskWorkspace

#: The one production deterministic source name the worker dispatches.
DETERMINISTIC_SOURCE_NAME = "deterministic_offline"

_MAX_DIAGNOSTIC_CHARS = 400
_KNOWN_PARAMS = frozenset({"task_id", "policy"})
_MAX_TASK_ID_CHARS = 128
_MAX_POLICY_CHARS = 64

#: App-owned artifact names persisted into the durable session directory.
CANDIDATE_PATCH_NAME = "candidate.patch"
EVALUATION_JSON_NAME = "evaluation.json"


class DeterministicSourceError(RuntimeError):
    """Raised when the production source itself fails (never for scientific
    outcomes; the verifier remains the correctness authority)."""


def _bounded_diagnostic(text: str) -> str:
    cleaned = "".join(
        char if 0x20 <= ord(char) != 0x7F else " " for char in str(text)
    )
    if len(cleaned) > _MAX_DIAGNOSTIC_CHARS:
        cleaned = cleaned[: _MAX_DIAGNOSTIC_CHARS - 3] + "..."
    return cleaned or "unspecified"


def _require_text(params: Mapping[str, Any], key: str, maximum: int) -> str:
    value = params.get(key)
    if type(value) is not str or not value:
        raise ScenarioInputError(f"deterministic source param {key!r} must be a non-empty string")
    if len(value.encode("utf-8")) > maximum:
        raise ScenarioInputError(f"deterministic source param {key!r} exceeds the byte bound")
    return value


def _validate_params(params: Mapping[str, Any]) -> tuple[str, str]:
    extra = set(params.keys()) - _KNOWN_PARAMS
    if extra:
        raise ScenarioInputError(f"unknown deterministic source params: {sorted(extra)}")
    task_id = _require_text(params, "task_id", _MAX_TASK_ID_CHARS)
    policy = _require_text(params, "policy", _MAX_POLICY_CHARS)
    if policy not in {candidate.value for candidate in DemoPolicy}:
        raise ScenarioInputError(
            f"unknown demonstration policy: {policy!r}"
        )
    return task_id, policy


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


def run_deterministic_session(
    ctx: ScenarioContext,
    params: Mapping[str, Any],
) -> None:
    """Execute one real deterministic offline debugging session.

    Runs inside the worker process after ``session.started`` (the work
    directory exists).  Every event flows through ``ctx.emitter`` (the
    session's single shared emission authority); cancellation honors
    ``ctx.token`` at every safe boundary.
    """
    task_id, policy_value = _validate_params(params)
    if ctx.emitter is None:
        raise ScenarioInputError("deterministic source requires the shared emitter")
    policy = DemoPolicy(policy_value)
    pdb_mode = pdb_policy_for(policy)
    session_id = ctx.emitter.session_id
    task_name = ctx.emitter.task_id
    source_kind = ctx.emitter.source_kind
    if task_id != task_name:
        raise ScenarioInputError(
            f"deterministic source task {task_id!r} does not match the "
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
            source_path = Path(workspace.root) / scenario.reference_repair.target_path
            patch_text = _build_reference_patch(source_path, scenario)
            probe = (
                prepare_pdb_probe(fixture_dir, scenario, case_parent)
                if pdb_mode is not PdbPolicy.DISABLED
                else None
            )
            demo_context = DemoToolContext(
                task=load_task(str(fixture_dir / "task.json")),
                workspace=workspace,
                patch=patch_text,
                probe=probe,
                observability=observability,
            )
            model = DemoPolicyModel(
                scenario=scenario,
                patch=patch_text,
                pdb_policy=pdb_mode,
            )
            controller = DeterministicController(
                build_registry(demo_context),
                model,
                ControllerRunConfig(max_model_calls=DEMO_MAX_MODEL_CALLS),
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
            controller.run(snapshot, cancel_check=ctx.token.check)
            ctx.token.check()  # controller -> verifier boundary

            # Independent verifier: progress events through the shared
            # emitter; the final EvaluationResult stays the only authority.
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
                workspace_parent=str(case_parent),
                progress_observer=verifier_adapter,
                cancel_check=ctx.token.check,
            ).evaluate(task, patch_text)
            verifier_adapter.completed(evaluation)
            _persist_artifacts(ctx, task_id, patch_text, evaluation)
    except CancellationError:
        raise
    except Exception as exc:
        raise DeterministicSourceError(
            f"deterministic session failed: {_bounded_diagnostic(exc)}"
        ) from exc
    finally:
        _release(demo_context, workspace, case_parent, diagnostics)
        if diagnostics and not ctx.token.is_cancelled:
            # Cleanup truth stays with the worker cleanup cycle when the
            # session was cancelled: a cancellation must not be flipped into
            # a generic failure by a best-effort release diagnostic.
            raise DeterministicSourceError(
                "deterministic session cleanup failed: "
                + "; ".join(diagnostics[:4])
            )


def _build_reference_patch(source_path: Path, scenario: Any) -> str:
    from agentic_debugger.demo.catalog import build_reference_patch

    return build_reference_patch(
        source_path.read_text(encoding="utf-8"), scenario.reference_repair
    )


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
    """
    session_dir = ctx.session_dir
    if session_dir is None:
        return
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
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
    root = str(case_parent)
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
    "DETERMINISTIC_SOURCE_NAME",
    "DeterministicSourceError",
    "EVALUATION_JSON_NAME",
    "run_deterministic_session",
]
