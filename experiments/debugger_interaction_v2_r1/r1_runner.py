#!/usr/bin/env python3
"""R1 — Repaired debugger interaction v2 runner.

R1 treatment revision: same frozen RAW Qwen2.5-Coder-7B model, same task
(``curated-off-by-one-002``), same budgets as D1/S1, but with the repaired
model-facing interface (source + breakpoint-eligible lines, source
rendering, error diagnostics, lifecycle visibility).

``--validate-only`` validates the R1 contract/identity without loading the
model.  ``--run`` loads the real RAW Qwen2.5 transport and executes the
live R1 probe (requires GPU + authorization).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
from agentic_debugger.agent.model_adapter import (
    ControllerSnapshot,
    ModelAdapterError,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.trajectory import project_controller_run
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.tools import (
    DemoToolContext,
    build_registry,
    prepare_pdb_probe,
)
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.runtime.workspace import TaskWorkspace

from experiments.debugger_interaction_v2_r1.adapter import (
    R1DebuggerBridgeAdapter,
    make_session_state_provider,
)
from experiments.debugger_interaction_v2_r1.bridge import (
    SYSTEM_PROMPT,
    breakpoint_eligible_lines,
)
from experiments.debugger_interaction_v2_r1.transport import (
    BASE_REPOSITORY,
    BASE_REVISION,
    GENERATION_CONFIG,
    LocalRawQwenTransport,
)
from experiments.debugger_interaction_v2_r1.phase_navigation import (
    R1PhaseNavigationAdapter,
)

CONTRACT_PATH = THIS_FILE.with_name("r1_contract.json")
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"

R1_SCHEMA_VERSION = "debugger-interaction-v2-r1.1"
TASK_ID = "curated-off-by-one-002"

V1_BUDGETS = {
    "task_max_patch_attempts": 2,
    "task_max_test_runs": 5,
    "task_max_pdb_observations": 8,
    "debugger_session_starts_max": 1,
    "debugger_observation_or_control_actions_max": 8,
    "debugger_session_stops_max": 1,
    "controller_steps_max": 24,
    "model_requests_max": 28,
    "model_retries_per_logical_call_max": 1,
    "model_phase_seconds_max": 600,
    "model_request_timeout_seconds": 60,
    "pdb_request_timeout_seconds": 5,
    "debugger_accepted_actions_max": 10,
}


# ---------------------------------------------------------------------------
# Contract / identity helpers
# ---------------------------------------------------------------------------


def _load_contract() -> dict[str, Any]:
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if value.get("schema_version") != R1_SCHEMA_VERSION:
        raise RuntimeError("unsupported R1 contract")
    return value


_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _git_head(repository_root: Path) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repository_root),
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    sha = completed.stdout.strip()
    return sha if _COMMIT_SHA_RE.match(sha) else None


def _contract_sha256(contract: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _fixture_tree_sha256(task_dir: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        p for p in task_dir.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    for path in files:
        relative = path.relative_to(task_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    model = contract.get("model", {})
    if model.get("base_repository") != BASE_REPOSITORY:
        raise RuntimeError("model base_repository drift")
    if model.get("base_revision") != BASE_REVISION:
        raise RuntimeError("model base_revision drift")
    if model.get("adapter_applied") is not False:
        raise RuntimeError("R1 must be RAW base only")
    if model.get("rag_enabled") is not False:
        raise RuntimeError("R1 must have RAG OFF")
    gen = model.get("generation", {})
    if gen.get("do_sample") is not False:
        raise RuntimeError("generation.do_sample must be False")
    if gen.get("max_new_tokens") != 1024:
        raise RuntimeError("generation.max_new_tokens must be 1024")
    if gen.get("max_input_tokens") != 32768:
        raise RuntimeError("generation.max_input_tokens must be 32768")
    tasks = contract.get("tasks", [])
    if len(tasks) != 1 or tasks[0].get("task_id") != TASK_ID:
        raise RuntimeError(f"R1 must contain exactly task {TASK_ID!r}")
    budgets = contract.get("budgets", {})
    for key, expected in V1_BUDGETS.items():
        if budgets.get(key) != expected:
            raise RuntimeError(f"budget {key} drift")
    fixture_dir = CURATED_ROOT / TASK_ID
    fixture_hash = _fixture_tree_sha256(fixture_dir)
    frozen_hash = tasks[0].get("fixture_tree_sha256")
    if frozen_hash and fixture_hash != frozen_hash:
        raise RuntimeError("fixture tree hash drift")
    return {
        "contract_sha256": _contract_sha256(contract),
        "fixture_tree_sha256": fixture_hash,
        "validated": True,
    }


def _candidate_source_manifest() -> dict[str, str]:
    """Hash every source file that can affect the R1.1 treatment."""
    relative_paths = (
        "experiments/debugger_interaction_v2_r1/bridge.py",
        "experiments/debugger_interaction_v2_r1/adapter.py",
        "experiments/debugger_interaction_v2_r1/transport.py",
        "experiments/debugger_interaction_v2_r1/phase_navigation.py",
        "experiments/debugger_interaction_v2_r1/r1_runner.py",
        "agentic_debugger/agent/tool_registry.py",
        "agentic_debugger/demo/tools.py",
        "agentic_debugger/runtime/pdb_session.py",
        "agentic_debugger/runtime/pdb_worker.py",
    )
    manifest: dict[str, str] = {}
    for relative in relative_paths:
        path = REPO_ROOT / relative
        if not path.is_file():
            raise RuntimeError(f"candidate source manifest file missing: {relative}")
        manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(manifest.items()))


def _r1_run_identity(contract: dict[str, Any]) -> dict[str, Any]:
    model = contract["model"]
    return {
        "schema_version": "debugger-interaction-v2-r1.1-identity",
        "experiment_id": "debugger-interaction-v2-r1",
        "source_commit_sha": _git_head(REPO_ROOT),
        "candidate_source_manifest": _candidate_source_manifest(),
        "experiment_contract_sha256": _contract_sha256(contract),
        "model_condition": "RAW_BASE",
        "adapter_applied": False,
        "adapter_path": None,
        "base_repository": model["base_repository"],
        "base_revision": model["base_revision"],
        "rag_enabled": False,
        "generation": model["generation"],
        "task_id": TASK_ID,
        "system_prompt_sha256": hashlib.sha256(
            SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
        "interface_revision": "r1.1",
        "interface_description": (
            "Repaired debugger interface: source + breakpoint-eligible lines "
            "at the break decision point; get_source_window observations with "
            "actual source content; bounded/redacted ToolExecutionError "
            "diagnostics; state-aware legal-command visibility."
        ),
        "budgets": contract["budgets"],
        "runtime_python": {
            "executable": sys.executable,
            "version": platform.python_version(),
        },
    }


def _build_task_description(task: Any) -> str:
    mapping = task.agent_visible_mapping()
    lines = []
    title = mapping.get("title", "")
    desc = mapping.get("description", "")
    if title:
        lines.append(f"Title: {title}")
    if desc:
        lines.append(f"Description: {desc}")
    tests = mapping.get("tests", {})
    f2p = tests.get("fail_to_pass", [])
    p2p = tests.get("pass_to_pass", [])
    if f2p:
        lines.append(f"Failing tests (must pass after fix): {f2p}")
    if p2p:
        lines.append(f"Passing tests (must still pass): {p2p}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

_PDB_ACTIONS = frozenset({
    "start_pdb_session", "get_stack_summary", "get_frame_locals",
    "safe_eval_expression", "continue_pdb_session", "step_pdb_session",
    "next_pdb_session",
})


def _compute_gate_r1(
    telemetry: list[dict[str, Any]],
    trajectory_jsonl: str,
    *,
    expected_script: str,
) -> dict[str, Any]:
    """R1 gate: correlate an accepted model breakpoint with real evidence.

    Acceptance of a directive is never sufficient.  The trajectory must
    contain the corresponding real ``start_pdb_session`` observation with
    ``status=ok``, ``state=paused``, a positive line, a production function,
    and the next request must bind that exact observation and its rendered
    hash.
    """

    accepted_pdb = [
        t for t in telemetry
        if t.get("parse_result", {}).get("status") == "accepted"
        and t.get("translated_directive", {}).get("action_name") in _PDB_ACTIONS
    ]

    if not accepted_pdb:
        return {"passed": False, "reason": "no accepted PDB commands",
                "accepted_pdb_count": 0}

    first = accepted_pdb[0]
    first_action = first.get("translated_directive", {}).get("action_name")
    if first_action != "start_pdb_session":
        return {"passed": False,
                "reason": f"first PDB command was {first_action}, not start_pdb_session",
                "accepted_pdb_count": len(accepted_pdb)}

    # Parse the projected trajectory and correlate the model-authored action
    # with the actual tool observation.  Directive acceptance alone is never
    # evidence of a successful runtime pause.
    try:
        events = [json.loads(line) for line in trajectory_jsonl.splitlines() if line.strip()]
    except (TypeError, ValueError):
        return {"passed": False, "reason": "trajectory could not be parsed",
                "accepted_pdb_count": len(accepted_pdb)}

    start_actions = []
    start_observations = []
    for event in events:
        if event.get("event_type") == "action" and event.get("name") == "start_pdb_session":
            action = event.get("payload", {}).get("action", {})
            start_actions.append(action)
        if event.get("event_type") == "observation" and event.get("name") == "start_pdb_session":
            observation = event.get("payload", {}).get("observation", {})
            start_observations.append(observation)

    if not start_actions or not start_observations:
        return {"passed": False,
                "reason": "no correlated start_pdb_session action and observation",
                "accepted_pdb_count": len(accepted_pdb)}

    # The first model-authored breakpoint's action must have a real matching
    # observation, not merely an accepted directive record.
    action_ids = {a.get("action_id") for a in start_actions}
    observation = next(
        (o for o in start_observations if o.get("action_id") in action_ids),
        None,
    )
    if observation is None:
        return {"passed": False,
                "reason": "start_pdb_session observation is not correlated to its action",
                "accepted_pdb_count": len(accepted_pdb)}

    obs_id = observation.get("observation_id")
    obs_status = observation.get("status")
    payload = observation.get("payload")
    if obs_status != "ok" or not isinstance(payload, dict):
        return {"passed": False,
                "reason": "start_pdb_session observation is not OK",
                "observation_status": obs_status,
                "accepted_pdb_count": len(accepted_pdb)}
    if payload.get("state") != "paused":
        return {"passed": False,
                "reason": "start_pdb_session observation is not paused",
                "observation_state": payload.get("state"),
                "accepted_pdb_count": len(accepted_pdb)}
    line = payload.get("line")
    function = payload.get("function")
    script = payload.get("script")
    if type(line) is not int or line <= 0:
        return {"passed": False, "reason": "paused observation has invalid line",
                "accepted_pdb_count": len(accepted_pdb)}
    if type(function) is not str or not function or function == "<module>":
        return {"passed": False,
                "reason": "paused observation has no production function",
                "accepted_pdb_count": len(accepted_pdb)}
    if script != expected_script:
        return {"passed": False,
                "reason": "paused observation is not in the production target script",
                "observation_script": script,
                "accepted_pdb_count": len(accepted_pdb)}
    if not isinstance(obs_id, str) or not obs_id:
        return {"passed": False, "reason": "successful observation has no id",
                "accepted_pdb_count": len(accepted_pdb)}

    first_idx = telemetry.index(first)
    if first_idx + 1 >= len(telemetry):
        return {"passed": False,
                "reason": "no next request after the breakpoint",
                "accepted_pdb_count": len(accepted_pdb)}

    next_record = telemetry[first_idx + 1]
    next_provenance = next_record.get("provenance", {})
    prior_obs_id = next_provenance.get("prior_observation_id")
    rendered_obs_sha = next_provenance.get("rendered_observation_sha256")
    if prior_obs_id != obs_id:
        return {"passed": False,
                "reason": "next request does not bind the successful pause observation",
                "expected_observation_id": obs_id,
                "prior_observation_id": prior_obs_id,
                "accepted_pdb_count": len(accepted_pdb)}
    if not rendered_obs_sha:
        return {"passed": False,
                "reason": "next request lacks rendered observation provenance",
                "accepted_pdb_count": len(accepted_pdb)}

    # Recompute the rendered observation hash from the exact observation
    # mapping used by the controller/adapter renderer.
    from agentic_debugger.events.schema import Observation
    from experiments.debugger_interaction_v2_r1 import bridge as r1_bridge
    try:
        obs_obj = Observation.from_mapping(observation)
        rendered = r1_bridge._render_observation(obs_obj)
        expected_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    except Exception:
        return {"passed": False, "reason": "successful observation could not be rendered",
                "accepted_pdb_count": len(accepted_pdb)}
    if rendered_obs_sha != expected_hash:
        return {"passed": False,
                "reason": "rendered observation provenance hash mismatch",
                "expected_rendered_observation_sha256": expected_hash,
                "rendered_observation_sha256": rendered_obs_sha,
                "accepted_pdb_count": len(accepted_pdb)}

    return {
        "passed": True,
        "reason": "model-authored breakpoint reached real PDB with OK/PAUSED production observation; exact observation bound into next request",
        "first_command": first_action,
        "observation_id": obs_id,
        "observation_line": line,
        "observation_function": function,
        "prior_observation_id": prior_obs_id,
        "rendered_observation_sha256": rendered_obs_sha,
        "accepted_pdb_count": len(accepted_pdb),
    }


def _compute_gate_c(
    telemetry: list[dict[str, Any]],
    verifier_result: Optional[dict[str, Any]],
    *,
    gate_r1: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    # Successful runtime evidence requires the actual R1 gate proof, not
    # merely an accepted debugger directive.
    has_pdb = bool(gate_r1 and gate_r1.get("passed") is True)
    has_diagnosis = any(
        t.get("translated_directive", {}).get("is_diagnosis") is True
        for t in telemetry
    )
    has_patch = any(
        t.get("translated_directive", {}).get("action_name") == "apply_patch"
        and t.get("parse_result", {}).get("status") == "accepted"
        for t in telemetry
    )
    verifier_executed = verifier_result is not None and verifier_result.get("executed") is True
    resolved = False
    if verifier_executed and "outcome" in (verifier_result or {}):
        resolved = (verifier_result or {}).get("outcome") == "RESOLVED"
    return {
        "has_pdb_evidence": has_pdb,
        "has_diagnosis": has_diagnosis,
        "has_patch": has_patch,
        "verifier_executed": verifier_executed,
        "resolved": resolved,
        "passed": has_pdb and has_patch and verifier_executed,
    }


# ---------------------------------------------------------------------------
# Experiment execution
# ---------------------------------------------------------------------------


def _controller_result_to_dict(result: Any, error: Optional[str]) -> dict[str, Any]:
    if result is None:
        return {"error": error or "controller failed"}
    return {
        "final_state": result.final_state.value if hasattr(result.final_state, "value") else str(result.final_state),
        "stop_reason": result.stop_reason.value if hasattr(result.stop_reason, "value") else str(result.stop_reason),
        "model_calls": result.model_calls,
        "steps_count": len(result.steps),
    }


def _utc_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def run_experiment(
    contract: dict[str, Any],
    transport: Any,
    output_dir: Path,
    *,
    original_source: str,
    eligible_lines: tuple[int, ...],
    script_path: str,
) -> dict[str, Any]:
    """Build context, adapter, controller, run, project, verify, write evidence."""

    fixture_dir = CURATED_ROOT / TASK_ID
    task = load_task(str(fixture_dir / "task.json"))
    scenario = scenario_for(TASK_ID)
    task_desc = _build_task_description(task)

    case_dir = output_dir / f"case-{TASK_ID}"
    case_dir.mkdir(parents=True, exist_ok=True)

    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_dir))
    probe = prepare_pdb_probe(
        fixture_dir, scenario, case_dir, model_selects_breakpoint=True,
    )
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=probe)
    registry = build_registry(
        context, pdb_policy=PdbPolicy.ALWAYS_ON,
        interactive_debugger_controls=True,
    )

    session_state_provider = make_session_state_provider(context)

    inner_adapter = R1DebuggerBridgeAdapter(
        transport=transport,
        model_name=f"{BASE_REPOSITORY}+RAW-BASE-R1",
        task_description=task_desc,
        script_path=script_path,
        source_text=original_source,
        eligible_lines=eligible_lines,
        session_state_provider=session_state_provider,
        max_retries=V1_BUDGETS["model_retries_per_logical_call_max"],
        request_timeout_seconds=V1_BUDGETS["model_request_timeout_seconds"],
    )
    adapter = R1PhaseNavigationAdapter(inner_adapter)

    controller = DeterministicController(
        registry, adapter,
        ControllerRunConfig(max_model_calls=V1_BUDGETS["controller_steps_max"]),
    )

    snapshot = ControllerSnapshot(
        f"r1-{TASK_ID}", TASK_ID, ControllerState.REPRODUCE, 0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(), HypothesisLedger(),
    )

    run_start = time.monotonic()
    try:
        controller_result = controller.run(snapshot)
    except Exception as exc:
        controller_result = None
        controller_error = f"{type(exc).__name__}: {exc}"
    else:
        controller_error = None
    run_duration_ms = int((time.monotonic() - run_start) * 1000)

    events_jsonl = ""
    if controller_result is not None:
        stream = io.StringIO()
        logger = JsonlEventLogger(
            controller_result.run_id, controller_result.task_id, stream=stream
        )
        try:
            for event in project_controller_run(
                controller_result,
                tool_version="debugger-interaction-v2-r1",
                model=adapter.model_name,
                timestamp=_utc_now(), duration_ms=run_duration_ms,
            ):
                logger.append(event)
            logger.flush()
        finally:
            logger.close()
        events_jsonl = stream.getvalue()

    verifier_result: Optional[dict[str, Any]] = None
    candidate_patch = context.candidate_patch if context.candidate_patch else None
    if candidate_patch:
        try:
            evaluation = EvaluationVerifier(
                str(REPO_ROOT), workspace_parent=str(case_dir)
            ).evaluate(task, candidate_patch)
            verifier_result = {
                "executed": True,
                "status": evaluation.status.value if hasattr(evaluation.status, "value") else str(evaluation.status),
                "outcome": evaluation.outcome.value if hasattr(evaluation.outcome, "value") else str(evaluation.outcome),
                "f2p_passed": [t.status.value if hasattr(t.status, "value") else str(t.status) for t in evaluation.f2p_records],
                "p2p_passed": [t.status.value if hasattr(t.status, "value") else str(t.status) for t in evaluation.p2p_records],
            }
        except Exception as exc:
            verifier_result = {"executed": True, "error": f"{type(exc).__name__}: {exc}"}
    else:
        verifier_result = {"executed": False}

    gate_r1 = _compute_gate_r1(
        inner_adapter.telemetry,
        events_jsonl,
        expected_script=script_path,
    )
    gate_c = _compute_gate_c(
        inner_adapter.telemetry,
        verifier_result,
        gate_r1=gate_r1,
    )

    evidence: dict[str, Any] = {
        "schema_version": "debugger-interaction-v2-r1-evidence",
        "run_identity": _r1_run_identity(contract),
        "controller_result": _controller_result_to_dict(controller_result, controller_error),
        "telemetry": inner_adapter.telemetry,
        "post_debug_diagnoses": inner_adapter.post_debug_diagnoses,
        "trajectory_jsonl": events_jsonl,
        "verifier": verifier_result,
        "candidate_patch": candidate_patch,
        "gate_results": {"gate_r1": gate_r1, "gate_c": gate_c},
        "interface_info": {
            "script_path": script_path,
            "eligible_lines": list(eligible_lines),
            "system_prompt_sha256": hashlib.sha256(
                SYSTEM_PROMPT.encode("utf-8")
            ).hexdigest(),
        },
        "claims_boundary": (
            "R1 is the repaired debugger-interaction treatment.  The interface "
            "confounds identified in D1/S2 are removed.  The model still makes "
            "the meaningful debugger decision.  R1 PASS = one model-authored "
            "breakpoint -> real PDB -> non-error paused observation -> "
            "provenance bound."
        ),
    }

    evidence_path = output_dir / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    context.release_pdb()
    return evidence


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="R1 — Repaired Debugger Interaction v2"
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output-dir", type=str, default=None)
    args = parser.parse_args()

    if not args.validate_only and not args.run:
        parser.error("select --validate-only or --run")

    contract = _load_contract()

    if args.validate_only:
        validation = _validate_contract(contract)
        identity = _r1_run_identity(contract)
        source_commit_sha = identity.get("source_commit_sha")
        validation = dict(validation)
        validation["source_commit_sha"] = source_commit_sha
        validation["experiment_contract_sha256"] = identity.get("experiment_contract_sha256")
        validation["runtime_python"] = identity.get("runtime_python")
        validation["system_prompt_sha256"] = identity.get("system_prompt_sha256")
        if source_commit_sha is None:
            print(json.dumps({
                "status": "FAIL",
                "reason": "source_commit_sha unresolved",
                "validation": validation, "run_identity": identity,
            }, indent=2, ensure_ascii=False))
            return 1
        print(json.dumps({
            "status": "PASS",
            "validation": validation, "run_identity": identity,
        }, indent=2, ensure_ascii=False))
        return 0

    if args.run:
        if not args.output_dir:
            parser.error("--output-dir is required for --run")
        _validate_contract(contract)
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        fixture_dir = CURATED_ROOT / TASK_ID
        scenario = scenario_for(TASK_ID)
        original_source = (fixture_dir / scenario.runtime_probe.module_path).read_text(encoding="utf-8")
        eligible_lines = breakpoint_eligible_lines(original_source)
        script_path = scenario.runtime_probe.module_path

        transport = LocalRawQwenTransport(
            max_new_tokens=GENERATION_CONFIG["max_new_tokens"],
            max_input_tokens=GENERATION_CONFIG["max_input_tokens"],
        )

        evidence = run_experiment(
            contract, transport, output_dir,
            original_source=original_source,
            eligible_lines=eligible_lines,
            script_path=script_path,
        )

        print(json.dumps({
            "status": "COMPLETE",
            "gate_r1": evidence["gate_results"]["gate_r1"],
            "gate_c": evidence["gate_results"]["gate_c"],
            "evidence_path": str(output_dir / "evidence.json"),
        }, indent=2, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())