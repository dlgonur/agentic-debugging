"""Production deterministic offline execution source (Task 7, Task 8 refactor).

This is the real application execution source that Task 3's internal
scenario harness explicitly deferred.  It binds the accepted deterministic
repository stack -- the real :class:`DeterministicController`, the real tool
registry (``demo.tools.build_registry``), real PDB, the real PatchManager,
the real independent :class:`EvaluationVerifier`, and a disposable task
workspace -- into one application session lifecycle running inside the
accepted Task-3 worker process.

Task 8 refactor: the execution pipeline itself now lives in the shared
:mod:`agentic_debugger.application.local_source` module so the deterministic
offline source and the configured command-model source visibly share the
same pipeline, with model construction (and the source-specific failure
semantics) being the meaningful difference.  This module remains the
deterministic source's public face: same source name, same error type, same
artifact names, same parameter contract, same behavior.

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

from pathlib import Path
from typing import Any, Mapping, Optional

from agentic_debugger.application.local_source import (
    CANDIDATE_PATCH_NAME,
    EVALUATION_JSON_NAME,
    LocalSourceError,
    run_local_session,
)
from agentic_debugger.application.worker_scenarios import (
    ScenarioContext,
    ScenarioInputError,
)
from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
from agentic_debugger.demo.model import DemoPolicyModel
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.demo.runner import DEMO_MAX_MODEL_CALLS
from agentic_debugger.demo.tools import DemoToolContext

#: The one production deterministic source name the worker dispatches.
DETERMINISTIC_SOURCE_NAME = "deterministic_offline"

_KNOWN_PARAMS = frozenset({"task_id", "policy"})
_MAX_TASK_ID_CHARS = 128
_MAX_POLICY_CHARS = 64


class DeterministicSourceError(LocalSourceError):
    """Raised when the production source itself fails (never for scientific
    outcomes; the verifier remains the correctness authority)."""


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
    policy = DemoPolicy(policy_value)

    scenario = scenario_for(task_id)

    def _initial_patch(workspace: Any) -> str:
        from agentic_debugger.demo.catalog import build_reference_patch

        source_path = Path(workspace.root) / scenario.reference_repair.target_path
        return build_reference_patch(
            source_path.read_text(encoding="utf-8"), scenario.reference_repair
        )

    def _model_factory(demo_context: DemoToolContext, registry: Any) -> Any:
        from agentic_debugger.demo.policies import pdb_policy_for

        return DemoPolicyModel(
            scenario=scenario,
            patch=demo_context.patch,
            pdb_policy=pdb_policy_for(policy),
        )

    def _verifier_patch(demo_context: DemoToolContext, result: Any) -> Optional[str]:
        # Accepted Task-7 semantics: the deterministic source always
        # verifies the catalog reference patch the demonstration model
        # proposes (the model's own candidate is that same patch).
        return demo_context.patch

    try:
        run_local_session(
            ctx,
            task_id=task_id,
            policy=policy,
            initial_patch=_initial_patch,
            model_factory=_model_factory,
            verifier_patch=_verifier_patch,
            fail_on_controller_failure=False,
            max_model_calls=DEMO_MAX_MODEL_CALLS,
        )
    except LocalSourceError as exc:
        raise DeterministicSourceError(str(exc)) from exc


__all__ = [
    "CANDIDATE_PATCH_NAME",
    "DETERMINISTIC_SOURCE_NAME",
    "DeterministicSourceError",
    "EVALUATION_JSON_NAME",
    "run_deterministic_session",
]
