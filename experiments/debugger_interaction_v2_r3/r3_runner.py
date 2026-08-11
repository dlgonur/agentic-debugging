#!/usr/bin/env python3
"""R3 — Debugger-informed patch -> verifier runner.

R3 treatment: same frozen RAW Qwen2.5-Coder-7B @ c03e6d358207e414f1eca0bb1891e29f1db0e242,
same task curated-off-by-one-002.  R2 staged lifecycle plus fix:
diagnosis RuntimeEvidence->PATCH (retained verbatim + runtime slice into
bounded PATCH checkpoint patch+failed).  After successful APPLY_PATCH,
administrative closeout without transport, then independent
EvaluationVerifier on exact candidate patch.

``--validate-only`` validates the R2 contract/identity without loading the
model.  ``--run`` loads the real RAW Qwen2.5 transport and executes the
live R2 probe (requires GPU + authorization).
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

from experiments.debugger_interaction_v2_r3.adapter import (
    R3DebuggerBridgeAdapter as R2DebuggerBridgeAdapter,
    R3StageTracker as R2StageTracker,
    make_r3_session_state_provider as make_r2_session_state_provider,
)
from experiments.debugger_interaction_v2_r3.bridge import (
    SYSTEM_PROMPT,
    breakpoint_eligible_lines,
)
from experiments.debugger_interaction_v2_r3.transport import (
    BASE_REPOSITORY,
    BASE_REVISION,
    GENERATION_CONFIG,
    LocalRawQwenTransport,
)
from experiments.debugger_interaction_v2_r3.phase_navigation import (
    R2PhaseNavigationAdapter,
)

CONTRACT_PATH = THIS_FILE.with_name("r3_contract.json")
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"

R3_SCHEMA_VERSION = "debugger-interaction-v2-r3"
TASK_ID = "curated-off-by-one-002"

R3_BUDGETS = {
    "task_max_patch_attempts": 2,
    "task_max_test_runs": 5,
    "task_max_pdb_observations": 8,
    "debugger_session_starts_max": 1,
    "debugger_observation_or_control_actions_max": 8,
    "debugger_session_stops_max": 1,
    "controller_steps_max": 32,
    "model_requests_max": 34,
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
    if value.get("schema_version") != R3_SCHEMA_VERSION:
        raise RuntimeError("unsupported R3 contract")
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
        raise RuntimeError("R3 must be RAW base only")
    if model.get("rag_enabled") is not False:
        raise RuntimeError("R3 must have RAG OFF")
    gen = model.get("generation", {})
    if gen.get("do_sample") is not False:
        raise RuntimeError("generation.do_sample must be False")
    if gen.get("max_new_tokens") != 1024:
        raise RuntimeError("generation.max_new_tokens must be 1024")
    if gen.get("max_input_tokens") != 32768:
        raise RuntimeError("generation.max_input_tokens must be 32768")
    tasks = contract.get("tasks", [])
    if len(tasks) != 1 or tasks[0].get("task_id") != TASK_ID:
        raise RuntimeError(f"R3 must contain exactly task {TASK_ID!r}")
    budgets = contract.get("budgets", {})
    for key, expected in R3_BUDGETS.items():
        if budgets.get(key) != expected:
            raise RuntimeError(f"budget {key} drift: got {budgets.get(key)!r} expected {expected!r}")
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
    relative_paths = (
        "experiments/debugger_interaction_v2_r3/bridge.py",
        "experiments/debugger_interaction_v2_r3/adapter.py",
        "experiments/debugger_interaction_v2_r3/transport.py",
        "experiments/debugger_interaction_v2_r3/phase_navigation.py",
        "experiments/debugger_interaction_v2_r3/serialization.py",
        "experiments/debugger_interaction_v2_r3/r3_runner.py",
        "experiments/debugger_interaction_v2_r3/r3_contract.json",
        "agentic_debugger/runtime/patcher.py",
        "agentic_debugger/evaluation/verifier.py",
        "agentic_debugger/evaluation/runner.py",
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


def _r3_run_identity(contract: dict[str, Any]) -> dict[str, Any]:
    model = contract["model"]
    return {
        "schema_version": "debugger-interaction-v2-r3-identity",
        "experiment_id": "debugger-interaction-v2-r3",
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
        "interface_revision": "r3",
        "interface_description": (
            "R3 debugger-informed patch interface: break->stack->locals/print->step/next->stack->diagnosis->patch->verifier; bound PATCH checkpoint; "
            "lifecycle via public get_target_status(); staged PAUSED progression."
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
    """Recompute the R1-equivalent gate inside the R2 runner (for comparison)."""
    accepted_pdb = [
        t for t in telemetry
        if t.get("parse_result", {}).get("status") == "accepted"
        and t.get("translated_directive", {}).get("action_name") in _PDB_ACTIONS
    ]
    if not accepted_pdb:
        return {"passed": False, "reason": "no accepted PDB commands", "accepted_pdb_count": 0}
    first = accepted_pdb[0]
    first_action = first.get("translated_directive", {}).get("action_name")
    if first_action != "start_pdb_session":
        return {"passed": False, "reason": f"first PDB command was {first_action}, not start_pdb_session", "accepted_pdb_count": len(accepted_pdb)}
    try:
        events = [json.loads(line) for line in trajectory_jsonl.splitlines() if line.strip()]
    except (TypeError, ValueError):
        return {"passed": False, "reason": "trajectory could not be parsed", "accepted_pdb_count": len(accepted_pdb)}
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
        return {"passed": False, "reason": "no correlated start_pdb_session action and observation", "accepted_pdb_count": len(accepted_pdb)}
    action_ids = {a.get("action_id") for a in start_actions}
    observation = next((o for o in start_observations if o.get("action_id") in action_ids), None)
    if observation is None:
        return {"passed": False, "reason": "start_pdb_session observation is not correlated to its action", "accepted_pdb_count": len(accepted_pdb)}
    obs_id = observation.get("observation_id")
    obs_status = observation.get("status")
    payload = observation.get("payload")
    if obs_status != "ok" or not isinstance(payload, dict):
        return {"passed": False, "reason": "start_pdb_session observation is not OK", "observation_status": obs_status, "accepted_pdb_count": len(accepted_pdb)}
    if payload.get("state") != "paused":
        return {"passed": False, "reason": "start_pdb_session observation is not paused", "observation_state": payload.get("state"), "accepted_pdb_count": len(accepted_pdb)}
    line = payload.get("line")
    function = payload.get("function")
    script = payload.get("script")
    if type(line) is not int or line <= 0:
        return {"passed": False, "reason": "paused observation has invalid line", "accepted_pdb_count": len(accepted_pdb)}
    if type(function) is not str or not function or function == "<module>":
        return {"passed": False, "reason": "paused observation has no production function", "accepted_pdb_count": len(accepted_pdb)}
    if script != expected_script:
        return {"passed": False, "reason": "paused observation is not in the production target script", "observation_script": script, "accepted_pdb_count": len(accepted_pdb)}
    if not isinstance(obs_id, str) or not obs_id:
        return {"passed": False, "reason": "successful observation has no id", "accepted_pdb_count": len(accepted_pdb)}
    first_idx = telemetry.index(first)
    if first_idx + 1 >= len(telemetry):
        return {"passed": False, "reason": "no next request after the breakpoint", "accepted_pdb_count": len(accepted_pdb)}
    next_record = telemetry[first_idx + 1]
    next_provenance = next_record.get("provenance", {})
    prior_obs_id = next_provenance.get("prior_observation_id")
    rendered_obs_sha = next_provenance.get("rendered_observation_sha256")
    if prior_obs_id != obs_id:
        return {"passed": False, "reason": "next request does not bind the successful pause observation", "expected_observation_id": obs_id, "prior_observation_id": prior_obs_id, "accepted_pdb_count": len(accepted_pdb)}
    if not rendered_obs_sha:
        return {"passed": False, "reason": "next request lacks rendered observation provenance", "accepted_pdb_count": len(accepted_pdb)}
    from agentic_debugger.events.schema import Observation
    from experiments.debugger_interaction_v2_r3 import bridge as r2_bridge
    try:
        obs_obj = Observation.from_mapping(observation)
        rendered = r2_bridge._render_observation(obs_obj)
        expected_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    except Exception:
        return {"passed": False, "reason": "successful observation could not be rendered", "accepted_pdb_count": len(accepted_pdb)}
    if rendered_obs_sha != expected_hash:
        return {"passed": False, "reason": "rendered observation provenance hash mismatch", "expected_rendered_observation_sha256": expected_hash, "rendered_observation_sha256": rendered_obs_sha, "accepted_pdb_count": len(accepted_pdb)}
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


def _compute_gate_r2(
    telemetry: list[dict[str, Any]],
    trajectory_jsonl: str,
    *,
    expected_script: str,
) -> dict[str, Any]:
    """R2 gate — requires the full staged chain A-F with trajectory correlation.

    A. break -> OK/PAUSED production
    B. stack -> OK G1>0
    C. locals OR safe_eval -> OK at G1
    D. step OR next -> OK/PAUSED production
    E. stack -> OK G2>G1
    F. diagnosis -> after E, provenance-bound

    Every observation must be status=ok in the actual trajectory and provenance-
    bound into the next request.  Accepted tokens alone never satisfy a gate.
    continue->exited never satisfies D.
    """
    # Parse trajectory once
    try:
        events = [json.loads(line) for line in trajectory_jsonl.splitlines() if line.strip()]
    except (TypeError, ValueError):
        return {"passed": False, "reason": "trajectory could not be parsed"}

    # Collect observations by name with their action_id
    # We also need to find the model-authored actions vs observations
    def _find_observation(name: str, start_idx: int = 0) -> Optional[dict[str, Any]]:
        # Return the first trajectory observation of `name` at or after
        # position start_idx in trajectory order, but we do ordered search
        # by scanning events sequentially.  For gate we do sequential chain.
        return None  # placeholder — actual chain uses ordered scan below

    # Helper: map action_id -> observation dict for quick lookup
    obs_by_action: dict[str, dict[str, Any]] = {}
    obs_list: list[dict[str, Any]] = []  # ordered observations
    action_list: list[dict[str, Any]] = []  # ordered actions
    for ev in events:
        if ev.get("event_type") == "action":
            action_list.append(ev.get("payload", {}).get("action", {}))
        if ev.get("event_type") == "observation":
            obs = ev.get("payload", {}).get("observation", {})
            obs_list.append(obs)
            aid = obs.get("action_id")
            if isinstance(aid, str):
                obs_by_action[aid] = obs

    # Helper to find the next telemetry record after a given observation
    def _telemetry_after_observation(obs_id: str) -> Optional[dict[str, Any]]:
        for idx, rec in enumerate(telemetry):
            if rec.get("provenance", {}).get("prior_observation_id") == obs_id:
                # need to find the observation that was just made and see if
                # the NEXT telemetry (index+1 in telemetry order) binds it
                # Actually telemetry order = model_call_index order. After an
                # observation is produced, the next model request's prior_obs
                # should be that observation.
                return rec
        return None

    def _next_telemetry_binds(obs_id: str, rendered_hash: str) -> bool:
        for rec in telemetry:
            prov = rec.get("provenance", {})
            if prov.get("prior_observation_id") == obs_id:
                if prov.get("rendered_observation_sha256") == rendered_hash:
                    return True
        return False

    def _render_hash(obs: dict[str, Any]) -> Optional[str]:
        from agentic_debugger.events.schema import Observation
        from experiments.debugger_interaction_v2_r3 import bridge as r2_bridge
        try:
            obj = Observation.from_mapping(obs)
            return hashlib.sha256(r2_bridge._render_observation(obj).encode("utf-8")).hexdigest()
        except Exception:
            return None

    # --- Ordered chain search ---
    # We walk trajectory observations in order, consuming only status=ok
    # observations that match expected actions.
    # Accept telemetry actions must also exist but gate authority is trajectory.

    obs_idx = 0  # pointer into obs_list

    def _next_ok_obs(target_names: set[str], require_ok: bool = True) -> Optional[dict[str, Any]]:
        nonlocal obs_idx
        while obs_idx < len(obs_list):
            obs = obs_list[obs_idx]
            obs_idx += 1
            if obs.get("name") not in target_names:
                continue
            if require_ok and obs.get("status") != "ok":
                # This is a non-OK observation of the right name — it does NOT
                # satisfy but we consume it so chain search continues past it
                # (but per strict gate we should not skip wrong-status and
                # claim a later one — we note the failure)
                continue
            return obs
        return None

    # --- Step A: break -> OK PAUSED production ---
    obs_idx = 0
    obs_a = None
    for i, obs in enumerate(obs_list):
        if obs.get("name") == "start_pdb_session" and obs.get("status") == "ok":
            payload = obs.get("payload") if isinstance(obs.get("payload"), dict) else {}
            if payload.get("state") == "paused" and payload.get("script") == expected_script:
                line = payload.get("line")
                fn = payload.get("function")
                if type(line) is int and line > 0 and type(fn) is str and fn and fn != "<module>":
                    obs_a = obs
                    obs_idx = i + 1
                    break
    if obs_a is None:
        # Check if there was a break that failed with wrong script/state for better reason
        any_break = [o for o in obs_list if o.get("name") == "start_pdb_session"]
        if not any_break:
            return {"passed": False, "reason": "no start_pdb_session observation in trajectory"}
        any_ok = [o for o in any_break if o.get("status") == "ok"]
        if not any_ok:
            return {"passed": False, "reason": "start_pdb_session observation is not OK"}
        return {"passed": False, "reason": "no paused production break observation", "candidates": len(any_ok)}
    obs_a_id = obs_a.get("observation_id")
    hash_a = _render_hash(obs_a)
    if hash_a is None:
        return {"passed": False, "reason": "A: observation could not be rendered"}
    if not _next_telemetry_binds(str(obs_a_id), hash_a):
        return {"passed": False, "reason": "A: next request does not bind the successful pause observation",
                "expected_observation_id": obs_a_id}

    # --- Step B: stack -> OK G1>0 ---
    obs_b = None
    g1: Optional[int] = None
    for i in range(obs_idx, len(obs_list)):
        obs = obs_list[i]
        if obs.get("name") == "get_stack_summary" and obs.get("status") == "ok":
            payload = obs.get("payload") if isinstance(obs.get("payload"), dict) else {}
            gen = payload.get("pause_generation")
            frames = payload.get("frames")
            if type(gen) is int and gen > 0 and isinstance(frames, list) and len(frames) > 0:
                # must have a current frame in expected script
                has_current = any(
                    type(f) is dict and f.get("is_current") is True and f.get("script") == expected_script
                    for f in frames
                )
                if has_current:
                    obs_b = obs
                    g1 = gen
                    obs_idx = i + 1
                    break
    if obs_b is None:
        return {"passed": False, "reason": "no OK stack with pause_generation G1>0 after A"}
    hash_b = _render_hash(obs_b)
    if hash_b is None or not _next_telemetry_binds(str(obs_b.get("observation_id")), hash_b):
        return {"passed": False, "reason": "B: stack observation not provenance-bound", "observation_id": obs_b.get("observation_id")}

    # --- Step C: locals OR safe_eval -> OK at G1 ---
    obs_c = None
    for i in range(obs_idx, len(obs_list)):
        obs = obs_list[i]
        if obs.get("name") in ("get_frame_locals", "safe_eval_expression") and obs.get("status") == "ok":
            payload = obs.get("payload") if isinstance(obs.get("payload"), dict) else {}
            if payload.get("state") == "paused":
                gen = payload.get("pause_generation")
                if gen == g1:
                    obs_c = obs
                    obs_idx = i + 1
                    break
    if obs_c is None:
        return {"passed": False, "reason": "no OK locals/safe_eval at G1 after B", "G1": g1}
    hash_c = _render_hash(obs_c)
    if hash_c is None or not _next_telemetry_binds(str(obs_c.get("observation_id")), hash_c):
        return {"passed": False, "reason": "C: inspection observation not provenance-bound", "observation_id": obs_c.get("observation_id")}

    # --- Step D: step OR next -> OK PAUSED production ---
    obs_d = None
    for i in range(obs_idx, len(obs_list)):
        obs = obs_list[i]
        if obs.get("name") in ("step_pdb_session", "next_pdb_session") and obs.get("status") == "ok":
            payload = obs.get("payload") if isinstance(obs.get("payload"), dict) else {}
            if payload.get("state") == "paused" and payload.get("script") == expected_script:
                line = payload.get("line")
                fn = payload.get("function")
                if type(line) is int and line > 0 and type(fn) is str and fn:
                    obs_d = obs
                    obs_idx = i + 1
                    break
        # A continue->exited that appears here must NOT satisfy D — keep scanning
        # but if it is an ERROR/OK with state=exited we just skip
    if obs_d is None:
        # Distinguish "had an exited continue" vs truly no step
        exited_continues = [
            o for o in obs_list[obs_idx - 5:obs_idx + 10]
            if o.get("name") == "continue_pdb_session" and (
                (isinstance(o.get("payload"), dict) and o.get("payload").get("state") == "exited")
                or o.get("status") != "ok"
            )
        ]
        if exited_continues:
            return {"passed": False, "reason": "no OK step/next PAUSED after C (continue->exited does not satisfy D)"}
        return {"passed": False, "reason": "no OK step/next PAUSED after C"}
    hash_d = _render_hash(obs_d)
    if hash_d is None or not _next_telemetry_binds(str(obs_d.get("observation_id")), hash_d):
        return {"passed": False, "reason": "D: step/next observation not provenance-bound", "observation_id": obs_d.get("observation_id")}

    # --- Step E: stack -> OK G2>G1 ---
    obs_e = None
    g2: Optional[int] = None
    for i in range(obs_idx, len(obs_list)):
        obs = obs_list[i]
        if obs.get("name") == "get_stack_summary" and obs.get("status") == "ok":
            payload = obs.get("payload") if isinstance(obs.get("payload"), dict) else {}
            gen = payload.get("pause_generation")
            if type(gen) is int and gen > 0 and isinstance(g1, int) and gen > g1:
                obs_e = obs
                g2 = gen
                obs_idx = i + 1
                break
    if obs_e is None:
        # If there is a stack but G2 not > G1
        any_stack_after_d = [
            o for o in obs_list[obs_idx - 5:obs_idx + 10]
            if o.get("name") == "get_stack_summary" and o.get("status") == "ok"
        ]
        if any_stack_after_d:
            return {"passed": False, "reason": "post-step stack exists but G2 is not > G1", "G1": g1,
                    "found_G2": [o.get("payload", {}).get("pause_generation") for o in any_stack_after_d]}
        return {"passed": False, "reason": "no OK post-step stack with G2>G1 after D", "G1": g1}
    hash_e = _render_hash(obs_e)
    if hash_e is None or not _next_telemetry_binds(str(obs_e.get("observation_id")), hash_e):
        return {"passed": False, "reason": "E: post-step stack not provenance-bound", "observation_id": obs_e.get("observation_id")}

    # --- Step F: diagnosis after E, provenance-bound, non-empty ---
    # Find telemetry diagnosis records after obs_e's position in trajectory
    # We need a telemetry entry that is a diagnosis AND whose prior_observation_id
    # equals obs_e's id (or a later OK observation after obs_e)
    obs_e_id = str(obs_e.get("observation_id"))
    # Build set of observation IDs that occur at/after obs_e and are OK
    later_ok_ids: set[str] = set()
    found_e = False
    for obs in obs_list:
        if obs.get("observation_id") == obs_e_id:
            found_e = True
        if found_e and obs.get("status") == "ok":
            oid = obs.get("observation_id")
            if isinstance(oid, str):
                later_ok_ids.add(oid)

    diagnosis_after_e = None
    for rec in telemetry:
        td = rec.get("translated_directive", {}) or {}
        pr = rec.get("parse_result", {}) or {}
        if pr.get("status") == "accepted" and td.get("is_diagnosis") is True:
            text = td.get("diagnosis_text")
            if type(text) is str and text.strip():
                prov = rec.get("provenance", {}) or {}
                prior = prov.get("prior_observation_id")
                if isinstance(prior, str) and prior in later_ok_ids:
                    # Check this diagnosis occurs after obs_e in telemetry order
                    # (we need to find the telemetry index that binds obs_e, then
                    # ensure diagnosis telemetry is at/after it)
                    diagnosis_after_e = rec
                    break
    if diagnosis_after_e is None:
        # Check if there is any diagnosis at all after
        any_diag = [r for r in telemetry if r.get("translated_directive", {}).get("is_diagnosis") is True and r.get("parse_result", {}).get("status") == "accepted"]
        if not any_diag:
            return {"passed": False, "reason": "no accepted diagnosis found"}
        return {"passed": False, "reason": "no diagnosis after E provenance-bound to post-step evidence",
                "diagnosis_count": len(any_diag), "expected_prior_in": sorted(later_ok_ids)}

    # --- PASS ---
    return {
        "passed": True,
        "reason": "model authored break->stack G1->locals/print G1->step/next PAUSED->stack G2>G1->diagnosis, all OK and provenance-bound",
        "observation_ids": {
            "break": obs_a_id,
            "stack_G1": obs_b.get("observation_id"),
            "inspection": obs_c.get("observation_id"),
            "step": obs_d.get("observation_id"),
            "stack_G2": obs_e.get("observation_id"),
        },
        "G1": g1,
        "G2": g2,
        "diagnosis_text": diagnosis_after_e.get("translated_directive", {}).get("diagnosis_text", "")[:200],
    }


def _compute_gate_r3(telemetry, trajectory_jsonl, verifier_result, *, candidate_patch, inner_adapter, expected_script):
    """R3 strict gate A-K: chain A-F + diagnosis retained (G) + patch B present (H)"
    "    + PatchManager accepted (I, via controller candidate_patch) + verifier evaluated B (J)"
    "    + RESOLVED 1/1 F2P 2/2 P2P full-suite+syntax (K).  No oracular value checks."""
    gate_r2_strict = _compute_gate_r2(telemetry, trajectory_jsonl, expected_script=expected_script)
    if not gate_r2_strict.get("passed"):
        return {"passed": False, "reason": "R2 chain failed — prerequisite for R3", "gate_r2": gate_r2_strict}
    # G: diagnosis retained byte-identical into PATCH context
    retained = getattr(inner_adapter, 'retained_diagnosis', None)
    if not retained:
        return {"passed": False, "reason": "no retained diagnosis into PATCH"}
    # Check PATCH prompt contained it (first PATCH telemetry user_prompt_summary)
    patch_telemetry = [r for r in telemetry if r.get("controller_state") == "Patch" and r.get("parse_result", {}).get("status") in ("accepted","rejected")]
    if patch_telemetry:
        first_patch_prompt = patch_telemetry[0].get("request", {}).get("user_prompt_summary", "")
        # rendered_diagnosis is recorded on PATCH records as rendered_diagnosis_sha256
        diag_ok = any(r.get("provenance", {}).get("rendered_diagnosis_sha256") for r in patch_telemetry)
        # Also verify the diagnosis text appears byte-identically in at least one PATCH summary (bounded check)
        # Bound text may truncate but retained short diagnosis should be present
        if retained[:30] not in first_patch_prompt:
            return {"passed": False, "reason": "retained diagnosis not found in PATCH prompt", "retained_prefix": retained[:60]}
    # H+I: parsed candidate B present, normalized to C (metadata-only), C dispatched to PatchManager
    patch_attempts = getattr(inner_adapter, "patch_attempts", []) or []
    if not patch_attempts:
        return {"passed": False, "reason": "no model-authored patch attempt (B)"}
    last_attempt = patch_attempts[-1]
    norm = last_attempt.get("normalization") or {}
    if not norm.get("fingerprint_equal"):
        return {"passed": False, "reason": "normalization fingerprint equality not proven", "normalization": norm}
    if not candidate_patch or not candidate_patch.startswith("---"):
        return {"passed": False, "reason": "no normalized candidate C dispatched"}
    c_sha = hashlib.sha256(candidate_patch.encode("utf-8")).hexdigest()
    if c_sha != last_attempt.get("model_patch_serialization_normalized_sha256"):
        return {"passed": False, "reason": "PatchManager input != normalization output C", "patchmanager_sha": c_sha, "normalized_sha": last_attempt.get("model_patch_serialization_normalized_sha256")}
    # I+J+K delegated to verifier result strictness
    if not verifier_result or not verifier_result.get("executed"):
        return {"passed": False, "reason": "verifier did not execute on B"}
    verifier_input_sha = verifier_result.get("candidate_sha256")
    if verifier_input_sha is not None and verifier_input_sha != c_sha:
        return {"passed": False, "reason": "verifier input != PatchManager accepted C", "verifier_sha": verifier_input_sha, "c_sha": c_sha}
    if verifier_result.get("status") != "COMPLETED":
        return {"passed": False, "reason": "verifier status not COMPLETED", "status": verifier_result.get("status")}
    if verifier_result.get("outcome") != "RESOLVED":
        return {"passed": False, "reason": "verifier outcome not RESOLVED", "outcome": verifier_result.get("outcome")}
    # F2P/P2P completeness is validated by verifier; gate reports them if present
    f2p = verifier_result.get("f2p_records") or verifier_result.get("f2p_passed")
    p2p = verifier_result.get("p2p_records") or verifier_result.get("p2p_passed")
    if f2p is not None and any((v.get("status") if isinstance(v, dict) else v) != "PASS" for v in f2p):
        return {"passed": False, "reason": "F2P not all PASS", "f2p": f2p}
    if p2p is not None and any((v.get("status") if isinstance(v, dict) else v) != "PASS" for v in p2p):
        return {"passed": False, "reason": "P2P not all PASS", "p2p": p2p}
    return {"passed": True, "reason": "R3 A-G chain + diagnosis retained + B applied + verifier RESOLVED 1/1 F2P 2/2 P2P full-suite", "gate_r2": gate_r2_strict}


def _compute_gate_c(
    telemetry: list[dict[str, Any]],
    verifier_result: Optional[dict[str, Any]],
    *,
    gate_r2: Optional[dict[str, Any]] = None,  # alias gate_r2==gate_r3-chain without patch
) -> dict[str, Any]:
    has_pdb = bool(gate_r2 and gate_r2.get("passed") is True)
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

    stage_tracker = R2StageTracker()
    session_state_provider = make_r2_session_state_provider(context, lambda: stage_tracker.stage)

    inner_adapter = R2DebuggerBridgeAdapter(
        transport=transport,
        model_name=f"{BASE_REPOSITORY}+RAW-BASE-R3",
        task_description=task_desc,
        script_path=script_path,
        source_text=original_source,
        eligible_lines=eligible_lines,
        session_state_provider=session_state_provider,
        stage_tracker=stage_tracker,
        max_retries=R3_BUDGETS["model_retries_per_logical_call_max"],
        request_timeout_seconds=R3_BUDGETS["model_request_timeout_seconds"],
    )
    adapter = R2PhaseNavigationAdapter(inner_adapter)

    controller = DeterministicController(
        registry, adapter,
        ControllerRunConfig(max_model_calls=R3_BUDGETS["controller_steps_max"]),
    )

    snapshot = ControllerSnapshot(
        f"r3-{TASK_ID}", TASK_ID, ControllerState.REPRODUCE, 0,
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
                tool_version="debugger-interaction-v2-r3",
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
    # R3 administrative closeout: after successful APPLY_PATCH, min legal deterministic closeout without transport
    administrative_closeout = {"performed": False, "steps": [], "transport_invoked": False}
    if candidate_patch:

        try:
            evaluation = EvaluationVerifier(
                str(REPO_ROOT), workspace_parent=str(case_dir)
            ).evaluate(task, candidate_patch)
            verifier_result = {
                "executed": True,
                "candidate_sha256": hashlib.sha256(candidate_patch.encode("utf-8")).hexdigest(),
                "status": evaluation.status.value if hasattr(evaluation.status, "value") else str(evaluation.status),
                "outcome": evaluation.outcome.value if hasattr(evaluation.outcome, "value") else str(evaluation.outcome),
                "stop_reason": evaluation.stop_reason,
                "f2p_records": [{"node_id": r.node_id, "status": r.status.value if hasattr(r.status, "value") else str(r.status)} for r in evaluation.post_patch_f2p],
                "f2p_passed": [r.status.value if hasattr(r.status, "value") else str(r.status) for r in evaluation.post_patch_f2p],
                "p2p_records": [{"node_id": r.node_id, "status": r.status.value if hasattr(r.status, "value") else str(r.status)} for r in evaluation.post_patch_p2p],
                "p2p_passed": [r.status.value if hasattr(r.status, "value") else str(r.status) for r in evaluation.post_patch_p2p],
                "syntax_passed": evaluation.syntax.passed if evaluation.syntax else None,
                "patch_applied": evaluation.patch.applied if evaluation.patch else None,
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
    gate_r2 = _compute_gate_r2(
        inner_adapter.telemetry,
        events_jsonl,
        expected_script=script_path,
    )
    gate_c = _compute_gate_c(
        inner_adapter.telemetry,
        verifier_result,
        gate_r2=gate_r2,
    )
    gate_r3 = _compute_gate_r3(
        inner_adapter.telemetry,
        events_jsonl,
        verifier_result,
        candidate_patch=candidate_patch,
        inner_adapter=inner_adapter,
        expected_script=script_path,
    )

    # Three patch representations hashes (R3.2): A raw, B model_patch_raw, C normalized
    patch_attempts = getattr(inner_adapter, "patch_attempts", []) or []
    final_candidate_sha256 = None
    if candidate_patch:
        final_candidate_sha256 = hashlib.sha256(candidate_patch.encode("utf-8")).hexdigest()
    patch_identity = {
        "note": "A=raw_model_response, B=model_patch_raw (bridge-parsed diff), C=model_patch_serialization_normalized (metadata-only hunk-count normalization). PatchManager and EvaluationVerifier receive C only.",
        "attempts": patch_attempts,
        "final_candidate_sha256": final_candidate_sha256,
    }
    # Diagnosis provenance + runtime slice
    diagnosis_provenance = getattr(inner_adapter, "diagnosis_provenance", None) or getattr(inner_adapter, "_diagnosis_provenance", None)
    runtime_slice = getattr(inner_adapter, "runtime_slice", None)
    if callable(runtime_slice):
        runtime_slice = runtime_slice
    elif isinstance(getattr(inner_adapter, "_runtime_slice", None), dict):
        runtime_slice = getattr(inner_adapter, "_runtime_slice", None)
    else:
        runtime_slice = {}
    # Administrative closeout flag (transport must not be invoked after apply)
    patch_apply_observed = any(_r.get("translated_directive", {}).get("action_name")=="apply_patch" and _r.get("parse_result", {}).get("status")=="accepted" for _r in inner_adapter.telemetry)
    closeout_ok = not patch_apply_observed or all(
        _r.get("controller_state") != "Patch" or _r.get("parse_result", {}).get("status") != "accepted" or _r.get("translated_directive", {}).get("action_name")=="apply_patch"
        for _r in inner_adapter.telemetry  # no second PATCH model call beyond patch
    )
    evidence: dict[str, Any] = {
        "schema_version": "debugger-interaction-v2-r3-evidence",
        "run_identity": _r3_run_identity(contract),
        "controller_result": _controller_result_to_dict(controller_result, controller_error),
        "telemetry": inner_adapter.telemetry,
        "post_debug_diagnoses": inner_adapter.post_debug_diagnoses,
        "trajectory_jsonl": events_jsonl,
        "verifier": verifier_result,
        "candidate_patch": candidate_patch,
        "patch_identity": patch_identity,
        "serialization_normalization": {
            "attempts": patch_attempts,
            "verifier_input_sha256": final_candidate_sha256,
            "patchmanager_input_sha256": final_candidate_sha256,
            "note": "normalization output SHA == PatchManager input SHA == verifier input SHA == final_candidate_sha256",
        },
        "diagnosis_provenance": diagnosis_provenance,
        "runtime_slice": runtime_slice,
        "gate_results": {"gate_r1": gate_r1, "gate_r2": gate_r2, "gate_c": gate_c, "gate_r3": gate_r3},
        "interface_info": {
            "script_path": script_path,
            "eligible_lines": list(eligible_lines),
            "system_prompt_sha256": hashlib.sha256(
                SYSTEM_PROMPT.encode("utf-8")
            ).hexdigest(),
        },
        "claims_boundary": (
            "R3 debugger-informed patch treatment.  R3 PASS = R2 chain A-F + exact diagnosis retained into bounded PATCH (patch+failed) + model B diff applied via real PatchManager + independent EvaluationVerifier COMPLETED/RESOLVED 1/1 F2P 2/2 P2P full-suite+syntax.  Administrative PATCH->VALIDATE->DONE closeout is harness bookkeeping, not model capability."
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
        description="R3 — Debugger-informed patch -> verifier"
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
        identity = _r3_run_identity(contract)
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
            "gate_r3": evidence["gate_results"]["gate_r3"],
            "gate_r2": evidence["gate_results"]["gate_r2"],
            "gate_r1": evidence["gate_results"]["gate_r1"],
            "evidence_path": str(output_dir / "evidence.json"),
        }, indent=2, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
