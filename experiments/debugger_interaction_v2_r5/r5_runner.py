#!/usr/bin/env python3
"""R5 — generalized raw debugger-informed repair matrix runner.

Same frozen RAW Qwen2.5-Coder-7B-Instruct treatment as accepted R3, applied
to the complete tracked five-task curated set under ONE generalized common
treatment:

  curated-none-handling-001
  curated-off-by-one-002
  curated-wrong-branch-003
  curated-mutation-alias-004
  curated-caller-callee-005

Generalization deltas vs R3:
- target script = the task's single writable production path derived from
  constraints.allowed_write_paths (reported design choice);
- the failing execution is driven by a neutral cwd-safe pytest launcher
  generated mechanically from task.reproduction.argv (no hand-authored
  semantic probe, no RuntimeProbe, no scenario);
- breakpoint eligibility derived from the ORIGINAL source only; the appended
  launcher region is never a candidate;
- model-facing stack rendering is filtered to the original production source
  region; raw payload evidence remains complete;
- model-facing task context excludes all test metadata (no F2P/P2P node ids,
  no reproduction argv);
- per-task strict gates require pauses inside the original production region
  only, with per-task F2P/P2P counts.

``--validate-only`` validates the frozen contract/identity without loading the
model.  ``--measure-latency`` measures real first-pause latency for all five
tasks under the generalized launcher and derives the single common
``pdb_request_timeout_seconds`` (pre-registered rule).  ``--run`` executes the
complete live matrix in frozen order (requires GPU + authorization).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

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
from agentic_debugger.demo.tools import (
    DemoToolContext,
    build_registry,
)
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.workspace import TaskWorkspace

from experiments.debugger_interaction_v2_r5.adapter import (
    R5DebuggerBridgeAdapter,
    R5StageTracker,
    make_r5_session_state_provider,
)
from experiments.debugger_interaction_v2_r5.bridge import (
    SYSTEM_PROMPT_TEMPLATE,
    breakpoint_eligible_lines,
)
from experiments.debugger_interaction_v2_r5.launcher import (
    R5LauncherError,
    build_r5_launcher_source,
    fixture_tree_sha256,
    prepare_r5_probe,
    task_target_module_path,
)
from experiments.debugger_interaction_v2_r5.transport import (
    BASE_REPOSITORY,
    BASE_REVISION,
    GENERATION_CONFIG,
    LocalRawQwenTransport,
)
from experiments.debugger_interaction_v2_r5.phase_navigation import (
    R5PhaseNavigationAdapter,
)

CONTRACT_PATH = THIS_FILE.with_name("r5_contract.json")
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"

LifecycleEvent = Optional[Callable[[str, dict[str, Any]], None]]


def _emit_lifecycle(
    callback: LifecycleEvent,
    event: str,
    **details: Any,
) -> None:
    if callback is not None:
        callback(event, details)

R5_SCHEMA_VERSION = "debugger-interaction-v2-r5"

# Frozen pre-registered matrix order — do not reorder.
R5_TASKS = (
    "curated-none-handling-001",
    "curated-off-by-one-002",
    "curated-wrong-branch-003",
    "curated-mutation-alias-004",
    "curated-caller-callee-005",
)

R5_BUDGETS = {
    "task_max_patch_attempts": 4,
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
    "pdb_request_timeout_seconds": 5,  # replaced by measured frozen value
    "debugger_accepted_actions_max": 10,
}

# Pre-registered timeout derivation rule: ONE common value for all five tasks.
_TIMEOUT_FLOOR_SECONDS = 15
_TIMEOUT_MARGIN_FACTOR = 3.0
_TIMEOUT_CEIL_STEP_SECONDS = 5


def derive_common_pdb_timeout(max_latency_ms: int) -> int:
    """Derive the single common pdb request timeout from the worst measured
    first-pause latency: ceil(max * 3 / 1000) rounded up to a multiple of 5,
    floored at 15 seconds.  Frozen in the contract before any live run."""
    if type(max_latency_ms) is not int or max_latency_ms <= 0:
        raise ValueError("max_latency_ms must be a positive integer")
    seconds = max_latency_ms * _TIMEOUT_MARGIN_FACTOR / 1000.0
    ceiled = math.ceil(seconds / _TIMEOUT_CEIL_STEP_SECONDS) * _TIMEOUT_CEIL_STEP_SECONDS
    return max(_TIMEOUT_FLOOR_SECONDS, ceiled)


# ---------------------------------------------------------------------------
# Contract / identity helpers
# ---------------------------------------------------------------------------


def _load_contract(name: str = "r5_contract.json") -> dict[str, Any]:
    path = CONTRACT_PATH.with_name(name)
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != R5_SCHEMA_VERSION:
        raise RuntimeError("unsupported R5 contract")
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


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_contract(
    contract: dict[str, Any], *, repo: str, revision: str, gen: dict[str, Any],
    adapter_path: Optional[str] = None,
) -> dict[str, Any]:
    """Validate the frozen contract.

    ``adapter_path`` selects the R6 matched PEFT-adapter condition (cp118):
    the contract must declare ``adapter_applied: true`` and the on-disk
    adapter must match the frozen contract identity exactly (tree identity +
    per-file SHA-256 + declared base model).  ``None`` keeps the RAW-base
    contract behavior unchanged.
    """
    model = contract.get("model", {})
    if model.get("base_repository") != repo:
        raise RuntimeError("model base_repository drift")
    if model.get("base_revision") != revision:
        raise RuntimeError("model base_revision drift")
    if adapter_path is None:
        if model.get("adapter_applied") is not False:
            raise RuntimeError("RAW-base contract must have adapter_applied False")
    else:
        if model.get("adapter_applied") is not True:
            raise RuntimeError("adapter contract must have adapter_applied True")
        from experiments.debugger_interaction_v2_r5.transport_cp118 import (
            validate_adapter_identity,
        )
        adapter_identity = model.get("adapter") or {}
        validate_adapter_identity(Path(adapter_path), adapter_identity)
    if model.get("rag_enabled") is not False:
        raise RuntimeError("R5 must have RAG OFF")
    if gen.get("do_sample") is not False:
        raise RuntimeError("generation.do_sample must be False")
    if gen.get("max_new_tokens") != 1024:
        raise RuntimeError("generation.max_new_tokens must be 1024")
    if gen.get("max_input_tokens") != 32768:
        raise RuntimeError("generation.max_input_tokens must be 32768")
    tasks = contract.get("tasks", [])
    if [t.get("task_id") for t in tasks] != list(R5_TASKS):
        raise RuntimeError("R5 tasks must match the frozen pre-registered order")
    for entry in tasks:
        task_id = entry.get("task_id")
        frozen_hash = entry.get("fixture_tree_sha256")
        fixture_dir = CURATED_ROOT / task_id
        actual = fixture_tree_sha256(fixture_dir)
        if frozen_hash and actual != frozen_hash:
            raise RuntimeError(f"fixture tree hash drift for {task_id}")
    budgets = contract.get("budgets", {})
    for key in R5_BUDGETS:
        if key not in budgets:
            raise RuntimeError(f"budget {key} missing from contract")
    for key in ("controller_steps_max", "model_requests_max"):
        if budgets.get(key) != R5_BUDGETS[key]:
            raise RuntimeError(f"budget {key} drift")
    # Timeout must be frozen and consistent with the recorded derivation.
    frozen_timeout = budgets.get("pdb_request_timeout_seconds")
    derivation = contract.get("timeout_derivation", {})
    max_measured = derivation.get("max_first_pause_latency_ms")
    if type(frozen_timeout) is not int or frozen_timeout <= 0:
        raise RuntimeError("pdb_request_timeout_seconds must be frozen positive")
    if type(max_measured) is not int or max_measured <= 0:
        raise RuntimeError("timeout derivation must record measured latency")
    expected = derive_common_pdb_timeout(max_measured)
    if frozen_timeout != expected:
        raise RuntimeError(
            f"pdb_request_timeout_seconds {frozen_timeout} != derived {expected}"
        )
    return {
        "contract_sha256": _contract_sha256(contract),
        "validated": True,
    }


def _candidate_source_manifest() -> dict[str, str]:
    """SHA-256 manifest of every source file that materially affects an R5
    run, derived from the actual R5 execution graph (runner imports and the
    modules they reach).  R5.9 closeout: includes the common deterministic
    sanitizer (controls model-facing runtime/verifier diagnostics) and the
    fail-closed actual-prompt anti-leakage auditor (clean-holdout
    acceptance authority), plus the controller/model-adapter/state-machine
    and task runner/workspace/task-schema surfaces the run depends on."""
    relative_paths = (
        # -- R5 experiment package ----------------------------------------
        "experiments/debugger_interaction_v2_r5/bridge.py",
        "experiments/debugger_interaction_v2_r5/adapter.py",
        "experiments/debugger_interaction_v2_r5/transport.py",
        "experiments/debugger_interaction_v2_r5/transport_14b.py",
        "experiments/debugger_interaction_v2_r5/transport_cp118.py",
        "experiments/debugger_interaction_v2_r5/phase_navigation.py",
        "experiments/debugger_interaction_v2_r5/serialization.py",
        "experiments/debugger_interaction_v2_r5/launcher.py",
        "experiments/debugger_interaction_v2_r5/anti_leakage.py",
        "experiments/debugger_interaction_v2_r5/r5_runner.py",
        "experiments/debugger_interaction_v2_r5/r5_contract.json",
        "experiments/debugger_interaction_v2_r5/r5_contract_14b.json",
        "experiments/debugger_interaction_v2_r5/r5_contract_cp118.json",
        # -- controller / adapter / state machine surface -----------------
        "agentic_debugger/agent/controller.py",
        "agentic_debugger/agent/controller_policy.py",
        "agentic_debugger/agent/model_adapter.py",
        "agentic_debugger/agent/state_machine.py",
        "agentic_debugger/agent/trajectory.py",
        "agentic_debugger/agent/tool_registry.py",
        # -- task / evaluation surface ------------------------------------
        "agentic_debugger/evaluation/task_schema.py",
        "agentic_debugger/evaluation/runner.py",
        "agentic_debugger/evaluation/verifier.py",
        "agentic_debugger/evaluation/outcome_taxonomy.py",
        # -- events / trajectory surface ----------------------------------
        "agentic_debugger/events/schema.py",
        "agentic_debugger/events/logger.py",
        # -- demo tool + sanitizer surface --------------------------------
        "agentic_debugger/demo/tools.py",
        "agentic_debugger/demo/sanitize.py",
        "agentic_debugger/demo/catalog.py",
        # -- runtime / task runner / workspace surface --------------------
        "agentic_debugger/runtime/patcher.py",
        "agentic_debugger/runtime/pdb_session.py",
        "agentic_debugger/runtime/pdb_worker.py",
        "agentic_debugger/runtime/pdb_protocol.py",
        "agentic_debugger/runtime/test_runner.py",
        "agentic_debugger/runtime/command_runner.py",
        "agentic_debugger/runtime/execution.py",
        "agentic_debugger/runtime/exceptions.py",
        "agentic_debugger/runtime/workspace.py",
        # -- registered tool surface (source window / search) -------------
        "agentic_debugger/skills/file_skills.py",
        "agentic_debugger/skills/search_skills.py",
    )
    manifest: dict[str, str] = {}
    for relative in relative_paths:
        path = REPO_ROOT / relative
        if not path.is_file():
            raise RuntimeError(f"candidate source manifest file missing: {relative}")
        manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(manifest.items()))


def _r5_run_identity(contract: dict[str, Any], adapter_path: Optional[str] = None) -> dict[str, Any]:
    model = contract["model"]
    if model.get("model_role"):
        model_condition = "SCRIPTED_TRAJECTORY"
        adapter_applied = False
        adapter_label = None
        adapter_identity = None
    else:
        adapter_applied = model.get("adapter_applied") is True
        model_condition = "PEFT_ADAPTER" if adapter_applied else "RAW_BASE"
        adapter_label = model.get("adapter_label")
        adapter_identity = model.get("adapter") if adapter_applied else None
    return {
        "schema_version": "debugger-interaction-v2-r5-identity",
        "experiment_id": contract.get("experiment_id"),
        "source_commit_sha": _git_head(REPO_ROOT),
        "candidate_source_manifest": _candidate_source_manifest(),
        "experiment_contract_sha256": _contract_sha256(contract),
        "model_condition": model_condition,
        "model_role": model.get("model_role"),
        "adapter_applied": adapter_applied,
        "adapter_label": adapter_label,
        "adapter_path": (
            str(Path(adapter_path).resolve()) if adapter_path is not None else None
        ),
        "adapter_identity": adapter_identity,
        "base_repository": model["base_repository"],
        "base_revision": model["base_revision"],
        "rag_enabled": False,
        "generation": model["generation"],
        "tasks": [t["task_id"] for t in contract["tasks"]],
        "system_prompt_template_sha256": _sha256(SYSTEM_PROMPT_TEMPLATE),
        "interface_revision": contract.get("interface_revision"),
        "interface_description": (
            "R5.9 clean-holdout generalized debugger-informed repair: "
            "reproduce -> break -> stack G1 -> locals/print -> step/next -> "
            "EITHER stack G2>G1 inside the original production region "
            "(normal path) OR the R5.9 production-exception path "
            "(production frame unwound during a real exception/failure; "
            "G2=None; sanitized production exception attached; no fake "
            "original-region G2 claimed) -> diagnosis -> patch -> verifier.  "
            "Common deterministic sanitizer for the reproduction diagnostic "
            "and the verifier-feedback failing records (production exception "
            "frames only; hidden test source/assertions/literals/node ids/"
            "test names never forwarded; fail closed to a generic behavioral "
            "statement).  Region-filtered model-facing observations; "
            "whole-file and unified-diff repair representations with "
            "deterministic serialization; real verifier-feedback repair "
            "loop; fail-closed ACTUAL-PROMPT anti-leakage audit "
            "(leakage_findings == [] required).  Neutral cwd-safe pytest "
            "launcher; original-source-region breakpoint derivation; no test "
            "metadata in prompts; bounded PATCH checkpoint; metadata-only "
            "B->C normalization."
        ),
        "budgets": contract["budgets"],
        "timeout_derivation": contract.get("timeout_derivation"),
        "runtime_python": {
            "executable": sys.executable,
            "version": platform.python_version(),
        },
    }


def _build_task_description(task: Any) -> str:
    """Model-facing task context: title + description only.

    Deliberately excludes F2P/P2P node ids, reproduction argv, and all other
    test/evaluation metadata (harness-only).
    """
    mapping = task.agent_visible_mapping()
    lines = []
    title = mapping.get("title", "")
    desc = mapping.get("description", "")
    if title:
        lines.append(f"Title: {title}")
    if desc:
        lines.append(f"Description: {desc}")
    return "\n".join(lines)


def _contract_budget_limits(contract: dict[str, Any], task: Any) -> "ControllerBudgetLimits":
    """Controller budget limits for the frozen final treatment.

    Derived from the per-task public constraints, with the common frozen
    contract patch-attempt budget overriding the fixture default (the
    fixture constraints are canonical and never edited; the experiment
    contract is the frozen treatment surface).  Other budget fields stay
    exactly as the task constraints declare.
    """
    limits = ControllerBudgetLimits.from_task_constraints(task.constraints)
    frozen_patch_attempts = contract["budgets"]["task_max_patch_attempts"]
    if type(frozen_patch_attempts) is not int or frozen_patch_attempts <= 0:
        raise RuntimeError("contract task_max_patch_attempts must be a positive integer")
    return ControllerBudgetLimits(
        max_patch_attempts=frozen_patch_attempts,
        max_test_runs=limits.max_test_runs,
        max_pdb_observations=limits.max_pdb_observations,
        max_active_hypotheses=limits.max_active_hypotheses,
        max_source_observations=limits.max_source_observations,
    )


# ---------------------------------------------------------------------------
# Gates (strict, provenance-bound, original-region)
# ---------------------------------------------------------------------------

_PDB_ACTIONS = frozenset({
    "start_pdb_session", "get_stack_summary", "get_frame_locals",
    "safe_eval_expression", "continue_pdb_session", "step_pdb_session",
    "next_pdb_session",
})


def _render_hash(obs: dict[str, Any], *, expected_script: str, original_line_count: int) -> Optional[str]:
    from agentic_debugger.events.schema import Observation
    from experiments.debugger_interaction_v2_r5 import bridge as r5_bridge
    try:
        obj = Observation.from_mapping(obs)
        rendered = r5_bridge._render_observation(
            obj,
            filter_scripts=frozenset({expected_script}),
            original_line_count=original_line_count,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    except Exception:
        return None


def _in_original_region(payload: dict[str, Any], original_line_count: int) -> bool:
    line = payload.get("line")
    return type(line) is int and 1 <= line <= original_line_count


def _compute_gate_r5_chain(
    telemetry: list[dict[str, Any]],
    trajectory_jsonl: str,
    *,
    expected_script: str,
    original_line_count: int,
) -> dict[str, Any]:
    """Strict debugger chain gate A-F with original-region pause checks.

    A. break -> OK/PAUSED production, non-module function, original region
    B. stack -> OK G1>0 with current frame in the original production region
    C. locals OR safe_eval -> OK at G1
    D. step OR next -> OK/PAUSED in the original production region
    E. stack -> OK G2>G1
    F. diagnosis after E, provenance-bound

    Every observation must be status=ok in the actual trajectory and
    provenance-bound into the next request.  Accepted tokens alone never
    satisfy a gate.  An appended-driver pause (line > original_line_count)
    never satisfies the gate.
    """
    try:
        events = [json.loads(line) for line in trajectory_jsonl.splitlines() if line.strip()]
    except (TypeError, ValueError):
        return {"passed": False, "reason": "trajectory could not be parsed"}

    obs_list: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("event_type") == "observation":
            obs_list.append(ev.get("payload", {}).get("observation", {}))

    def _next_telemetry_binds(obs_id: str, rendered_hash: str) -> bool:
        for rec in telemetry:
            prov = rec.get("provenance", {})
            if prov.get("prior_observation_id") == obs_id:
                if prov.get("rendered_observation_sha256") == rendered_hash:
                    return True
        return False

    # --- Step A: break -> OK PAUSED original-region production ---
    obs_idx = 0
    obs_a = None
    for i, obs in enumerate(obs_list):
        if obs.get("name") == "start_pdb_session" and obs.get("status") == "ok":
            payload = obs.get("payload") if isinstance(obs.get("payload"), dict) else {}
            if (
                payload.get("state") == "paused"
                and payload.get("script") == expected_script
                and _in_original_region(payload, original_line_count)
            ):
                fn = payload.get("function")
                if type(fn) is str and fn and fn != "<module>":
                    obs_a = obs
                    obs_idx = i + 1
                    break
    if obs_a is None:
        any_break = [o for o in obs_list if o.get("name") == "start_pdb_session"]
        if not any_break:
            return {"passed": False, "reason": "no start_pdb_session observation in trajectory"}
        any_ok = [o for o in any_break if o.get("status") == "ok"]
        if not any_ok:
            return {"passed": False, "reason": "start_pdb_session observation is not OK"}
        return {"passed": False, "reason": "no paused production-region break observation", "candidates": len(any_ok)}
    obs_a_id = obs_a.get("observation_id")
    hash_a = _render_hash(obs_a, expected_script=expected_script, original_line_count=original_line_count)
    if hash_a is None:
        return {"passed": False, "reason": "A: observation could not be rendered"}
    if not _next_telemetry_binds(str(obs_a_id), hash_a):
        return {"passed": False, "reason": "A: next request does not bind the successful pause observation",
                "expected_observation_id": obs_a_id}

    # --- Step B: stack -> OK G1>0, current frame in original region ---
    obs_b = None
    g1: Optional[int] = None
    for i in range(obs_idx, len(obs_list)):
        obs = obs_list[i]
        if obs.get("name") == "get_stack_summary" and obs.get("status") == "ok":
            payload = obs.get("payload") if isinstance(obs.get("payload"), dict) else {}
            gen = payload.get("pause_generation")
            frames = payload.get("frames")
            if type(gen) is int and gen > 0 and isinstance(frames, list) and len(frames) > 0:
                has_current = any(
                    type(f) is dict and f.get("is_current") is True
                    and f.get("script") == expected_script
                    and type(f.get("line")) is int and 1 <= f.get("line") <= original_line_count
                    for f in frames
                )
                if has_current:
                    obs_b = obs
                    g1 = gen
                    obs_idx = i + 1
                    break
    if obs_b is None:
        return {"passed": False, "reason": "no OK stack with G1>0 and current frame in original region after A"}
    hash_b = _render_hash(obs_b, expected_script=expected_script, original_line_count=original_line_count)
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
    hash_c = _render_hash(obs_c, expected_script=expected_script, original_line_count=original_line_count)
    if hash_c is None or not _next_telemetry_binds(str(obs_c.get("observation_id")), hash_c):
        return {"passed": False, "reason": "C: inspection observation not provenance-bound", "observation_id": obs_c.get("observation_id")}

    # --- Step D: step OR next -> OK; either PAUSED in the target script
    #     (normal progression; a real step may land beyond the original
    #     region — e.g. the launcher — when the target's real control flow
    #     goes there; that pause is only accepted when corroborated by the
    #     real reproduction failure output) or a real TERMINAL outcome
    #     (exited/failed/terminated) with real exit/error evidence ---
    obs_d = None
    terminal = False
    step_outside_region = False
    for i in range(obs_idx, len(obs_list)):
        obs = obs_list[i]
        if obs.get("name") in ("step_pdb_session", "next_pdb_session") and obs.get("status") == "ok":
            payload = obs.get("payload") if isinstance(obs.get("payload"), dict) else {}
            if payload.get("state") == "paused":
                fn = payload.get("function")
                if type(fn) is str and fn:
                    obs_d = obs
                    terminal = False
                    # R5.9: a real post-step pause may land anywhere the
                    # target's real control flow goes — inside the original
                    # production region (normal G2) or outside it (the
                    # production frame unwound during a real
                    # exception/failure; production-exception path).
                    step_outside_region = not (
                        payload.get("script") == expected_script
                        and _in_original_region(payload, original_line_count)
                    )
                    obs_idx = i + 1
                    break
            elif payload.get("state") in ("exited", "failed", "terminated"):
                exit_code = payload.get("exit_code")
                error = payload.get("error")
                has_terminal_evidence = (
                    type(exit_code) is int
                    or (type(error) is str and bool(error))
                )
                if has_terminal_evidence:
                    obs_d = obs
                    terminal = True
                    obs_idx = i + 1
                    break
    if obs_d is None:
        return {"passed": False, "reason": "no OK step/next PAUSED or TERMINAL evidence after C", "G1": g1}
    hash_d = _render_hash(obs_d, expected_script=expected_script, original_line_count=original_line_count)
    if hash_d is None or not _next_telemetry_binds(str(obs_d.get("observation_id")), hash_d):
        return {"passed": False, "reason": "D: step/next observation not provenance-bound", "observation_id": obs_d.get("observation_id")}

    def _sanitized_reproduction() -> Optional[str]:
        """The SANITIZED reproduction diagnostic (never raw pytest output).

        The payload ``failure_output`` is produced by the common
        deterministic sanitizer; the raw bounded output lives in
        ``failure_output_raw`` (audit-only) and is never a gate input.
        """
        for obs in obs_list:
            if obs.get("name") == "run_reproduction" and obs.get("status") == "ok":
                payload = obs.get("payload") if isinstance(obs.get("payload"), dict) else {}
                if payload.get("failure_reproduced") is True:
                    failure_output = payload.get("failure_output")
                    if type(failure_output) is str and failure_output:
                        return failure_output
        return None

    def _diagnosis_after(obs_d_id: str) -> Optional[dict[str, Any]]:
        """The first accepted non-empty diagnosis whose provenance binds it
        to an OK observation that followed ``obs_d_id``."""
        later_ok_ids: set[str] = set()
        found = False
        for obs in obs_list:
            if obs.get("observation_id") == obs_d_id:
                found = True
            if found and obs.get("status") == "ok":
                oid = obs.get("observation_id")
                if isinstance(oid, str):
                    later_ok_ids.add(oid)
        for rec in telemetry:
            td = rec.get("translated_directive", {}) or {}
            pr = rec.get("parse_result", {}) or {}
            if pr.get("status") == "accepted" and td.get("is_diagnosis") is True:
                text = td.get("diagnosis_text")
                if type(text) is str and text.strip():
                    prov = rec.get("provenance", {}) or {}
                    prior = prov.get("prior_observation_id")
                    if isinstance(prior, str) and prior in later_ok_ids:
                        return rec
        return None

    # --- R5.9 PRODUCTION-EXCEPTION path: the step/next pause is OUTSIDE
    #     the original production region — the real production frame unwound
    #     during a real exception/failure (e.g. pytest caught the exception
    #     and control returned to the appended launcher).  No original-region
    #     G2 exists and none is claimed.  The diagnosis must be bound to the
    #     step observation and the trajectory must carry the SANITIZED
    #     reproduction diagnostic as corroborating failure evidence.  The
    #     classification is mechanical: pause outside the production region +
    #     sanitized reproduction evidence; no task-id special case. ---
    if step_outside_region and not terminal:
        repro_text = _sanitized_reproduction()
        if repro_text is None:
            return {"passed": False, "reason": "step/next pause outside the original region without sanitized reproduction diagnostic", "G1": g1}
        production_exception = "production exception:" in repro_text
        diagnosis_rec = _diagnosis_after(str(obs_d.get("observation_id")))
        if diagnosis_rec is None:
            return {"passed": False, "reason": "no diagnosis bound to the production-unwind step evidence", "G1": g1}
        return {
            "passed": True,
            "reason": (
                "model authored break->stack G1->locals/print G1->step/next "
                "PAUSED outside the original production region (the "
                "production frame unwound during a real "
                f"exception/failure{' with a sanitized production exception' if production_exception else '; only the sanitized generic diagnostic is available'}"
                ")->diagnosis, all OK, provenance-bound; no original-region "
                "G2 exists and none is claimed"
            ),
            "terminal_path": False,
            "production_exception_path": production_exception,
            "step_outside_region": True,
            "observation_ids": {
                "break": obs_a_id,
                "stack_G1": obs_b.get("observation_id"),
                "inspection": obs_c.get("observation_id"),
                "step": obs_d.get("observation_id"),
            },
            "G1": g1,
            "G2": None,
            "diagnosis_text": diagnosis_rec.get("translated_directive", {}).get("diagnosis_text", "")[:200],
        }

    # --- Terminal path: the target ended during the control action.  The
    #     diagnosis must be bound to the terminal step observation and the
    #     trajectory must carry the SANITIZED reproduction diagnostic. ---
    if terminal:
        repro_text = _sanitized_reproduction()
        if repro_text is None:
            return {"passed": False, "reason": "terminal step without sanitized reproduction diagnostic", "G1": g1}
        obs_terminal_id = str(obs_d.get("observation_id"))
        diagnosis_after_terminal = _diagnosis_after(obs_terminal_id)
        if diagnosis_after_terminal is None:
            return {"passed": False, "reason": "no diagnosis bound to terminal step evidence", "G1": g1}
        return {
            "passed": True,
            "reason": "model authored break->stack G1->locals/print G1->step/next TERMINAL (real exit/error evidence + sanitized reproduction diagnostic)->diagnosis, all OK, provenance-bound",
            "terminal_path": True,
            "production_exception_path": "production exception:" in repro_text,
            "step_outside_region": False,
            "observation_ids": {
                "break": obs_a_id,
                "stack_G1": obs_b.get("observation_id"),
                "inspection": obs_c.get("observation_id"),
                "step": obs_d.get("observation_id"),
            },
            "G1": g1,
            "G2": None,
            "diagnosis_text": diagnosis_after_terminal.get("translated_directive", {}).get("diagnosis_text", "")[:200],
        }

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
        return {"passed": False, "reason": "no OK post-step stack with G2>G1 after D", "G1": g1}
    hash_e = _render_hash(obs_e, expected_script=expected_script, original_line_count=original_line_count)
    if hash_e is None or not _next_telemetry_binds(str(obs_e.get("observation_id")), hash_e):
        return {"passed": False, "reason": "E: post-step stack not provenance-bound", "observation_id": obs_e.get("observation_id")}

    # --- Step F: diagnosis after E, provenance-bound, non-empty ---
    obs_e_id = str(obs_e.get("observation_id"))
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
                    diagnosis_after_e = rec
                    break
    if diagnosis_after_e is None:
        any_diag = [r for r in telemetry if r.get("translated_directive", {}).get("is_diagnosis") is True and r.get("parse_result", {}).get("status") == "accepted"]
        if not any_diag:
            return {"passed": False, "reason": "no accepted diagnosis found"}
        return {"passed": False, "reason": "no diagnosis after E provenance-bound to post-step evidence",
                "diagnosis_count": len(any_diag), "expected_prior_in": sorted(later_ok_ids)}

    return {
        "passed": True,
        "reason": "model authored break->stack G1->locals/print G1->step/next PAUSED->stack G2>G1->diagnosis, all OK, original-region, provenance-bound",
        "terminal_path": False,
        "production_exception_path": False,
        "step_outside_region": step_outside_region,
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


def _compute_gate_r5_patch(
    telemetry,
    trajectory_jsonl,
    verifier_result,
    *,
    candidate_patch,
    inner_adapter,
    expected_script,
    original_line_count,
    f2p_count: int,
    p2p_count: int,
) -> dict[str, Any]:
    """Strict R5 gate: chain A-F + diagnosis retained + B present + PatchManager
    accepted + verifier evaluated the exact accepted candidate + per-task
    F2P/P2P completeness + full-suite + syntax + canonical unchanged + cleaned."""
    gate_chain = _compute_gate_r5_chain(
        telemetry, trajectory_jsonl,
        expected_script=expected_script,
        original_line_count=original_line_count,
    )
    if not gate_chain.get("passed"):
        return {"passed": False, "reason": "R5 chain failed — prerequisite for patch gate", "gate_chain": gate_chain}
    # G: diagnosis retained byte-identical into PATCH context
    retained = getattr(inner_adapter, "retained_diagnosis", None)
    if not retained:
        return {"passed": False, "reason": "no retained diagnosis into PATCH"}
    patch_telemetry = [r for r in telemetry if r.get("controller_state") == "Patch" and r.get("parse_result", {}).get("status") in ("accepted", "rejected")]
    if patch_telemetry:
        first_patch_prompt = patch_telemetry[0].get("request", {}).get("user_prompt_summary", "")
        first_patch_prompt_full = patch_telemetry[0].get("request", {}).get("user_prompt_full", "")
        diag_ok = any(r.get("provenance", {}).get("rendered_diagnosis_sha256") for r in patch_telemetry)
        if not diag_ok:
            return {"passed": False, "reason": "diagnosis provenance sha not recorded on PATCH telemetry"}
        if retained[:30] not in first_patch_prompt and retained[:30] not in first_patch_prompt_full:
            return {"passed": False, "reason": "retained diagnosis not found in PATCH prompt", "retained_prefix": retained[:60]}
    # H+I: parsed candidate B present, normalized to C (metadata-only), C dispatched
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
        return {"passed": False, "reason": "verifier did not execute on C"}
    verifier_input_sha = verifier_result.get("candidate_sha256")
    if verifier_input_sha is not None and verifier_input_sha != c_sha:
        return {"passed": False, "reason": "verifier input != PatchManager accepted C", "verifier_sha": verifier_input_sha, "c_sha": c_sha}
    if verifier_result.get("status") != "COMPLETED":
        return {"passed": False, "reason": "verifier status not COMPLETED", "status": verifier_result.get("status")}
    if verifier_result.get("outcome") != "RESOLVED":
        return {"passed": False, "reason": "verifier outcome not RESOLVED", "outcome": verifier_result.get("outcome")}
    f2p = verifier_result.get("f2p_records") or []
    p2p = verifier_result.get("p2p_records") or []
    if len(f2p) != f2p_count or any((v.get("status") if isinstance(v, dict) else v) != "PASS" for v in f2p):
        return {"passed": False, "reason": "F2P not all PASS or count mismatch", "expected": f2p_count, "got": [v.get("status") if isinstance(v, dict) else v for v in f2p]}
    if len(p2p) != p2p_count or any((v.get("status") if isinstance(v, dict) else v) != "PASS" for v in p2p):
        return {"passed": False, "reason": "P2P not all PASS or count mismatch", "expected": p2p_count, "got": [v.get("status") if isinstance(v, dict) else v for v in p2p]}
    if verifier_result.get("full_suite_consistent") is not True:
        return {"passed": False, "reason": "full-suite consistency not PASS", "full_suite": verifier_result.get("full_suite_consistent")}
    if verifier_result.get("syntax_passed") is not True:
        return {"passed": False, "reason": "syntax not PASS", "syntax": verifier_result.get("syntax_passed")}
    if verifier_result.get("canonical_fixture_unchanged") is not True:
        return {"passed": False, "reason": "canonical fixture changed", "canonical": verifier_result.get("canonical_fixture_unchanged")}
    if verifier_result.get("workspace_lifecycle") != "CLEANED":
        return {"passed": False, "reason": "verifier workspace not CLEANED", "lifecycle": verifier_result.get("workspace_lifecycle")}
    return {
        "passed": True,
        "reason": f"R5 A-G chain + diagnosis retained + B applied + verifier RESOLVED {f2p_count}/{f2p_count} F2P {p2p_count}/{p2p_count} P2P full-suite+syntax canonical-unchanged cleaned",
        "gate_chain": gate_chain,
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


_VERIFIER_FEEDBACK_DETAIL_CHARS = 900


def _bounded_verifier_detail(record: Any) -> str:
    """Bounded tail of one real failing-test record (the exception/assertion
    summary is at the end of pytest -vv output)."""
    raw = ""
    for attr in ("stdout", "stderr"):
        value = getattr(record, attr, None)
        if type(value) is str and value:
            raw = value if not raw else raw + "\n" + value
    if not raw.strip():
        return ""
    if len(raw) <= _VERIFIER_FEEDBACK_DETAIL_CHARS:
        return raw
    marker = "... [output truncated] ...\n"
    return marker + raw[-(_VERIFIER_FEEDBACK_DETAIL_CHARS - len(marker)):]


def _make_verifier_feedback(
    script_path: str,
    original_line_count: int,
    lifecycle_event: LifecycleEvent = None,
) -> Callable[[Any, str], dict[str, Any]]:
    """Build the real-verifier feedback callback for the tool layer.

    Every accepted candidate patch is evaluated by the independent
    EvaluationVerifier in its own disposable workspace; a bounded feedback
    mapping (status/outcome/counts/failing details) is returned for the
    apply_patch observation.  This is the accepted real verifier — the same
    class of evidence a developer gets from running the tests — and it is
    never a fabricated or oracle-based signal.

    R5.9: failing records are SANITIZED by the common deterministic
    sanitizer before they can reach any prompt: only the record status and
    a genuine production exception of the CANDIDATE are forwarded — never
    node ids, test function names, test source, assertion expressions, or
    hidden expected literals.
    """
    from agentic_debugger.demo.sanitize import sanitize_verifier_failure_output

    def feedback(task: Any, diff: str) -> dict[str, Any]:
        candidate_sha256 = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        _emit_lifecycle(
            lifecycle_event,
            "verifier_feedback_start",
            candidate_sha256=candidate_sha256,
        )
        try:
            evaluation = EvaluationVerifier(
                str(REPO_ROOT), workspace_parent=None
            ).evaluate(task, diff)
        except BaseException as exc:  # noqa: BLE001 - log before caller bounds it
            _emit_lifecycle(
                lifecycle_event,
                "verifier_feedback_error",
                candidate_sha256=candidate_sha256,
                error_type=type(exc).__name__,
            )
            raise
        _emit_lifecycle(
            lifecycle_event,
            "verifier_feedback_complete",
            candidate_sha256=candidate_sha256,
            status=(
                evaluation.status.value
                if hasattr(evaluation.status, "value")
                else str(evaluation.status)
            ),
            outcome=(
                evaluation.outcome.value
                if hasattr(evaluation.outcome, "value")
                else str(evaluation.outcome)
            ),
            stop_reason=evaluation.stop_reason,
        )
        failures: list[dict[str, Any]] = []
        for kind, records in (
            ("f2p", evaluation.post_patch_f2p),
            ("p2p", evaluation.post_patch_p2p),
        ):
            for record in records:
                status = record.status.value if hasattr(record.status, "value") else str(record.status)
                if status != "PASS":
                    raw_detail = _bounded_verifier_detail(record)
                    failures.append({
                        "kind": kind,
                        "status": status,
                        "production_exception": sanitize_verifier_failure_output(
                            raw_detail, script_path, original_line_count
                        ),
                    })
        full_suite = evaluation.full_suite
        return {
            "status": evaluation.status.value if hasattr(evaluation.status, "value") else str(evaluation.status),
            "outcome": evaluation.outcome.value if hasattr(evaluation.outcome, "value") else str(evaluation.outcome),
            "stop_reason": evaluation.stop_reason,
            "f2p_total": evaluation.f2p_total,
            "f2p_passed": evaluation.f2p_passed,
            "p2p_total": evaluation.p2p_total,
            "p2p_passed": evaluation.p2p_passed,
            "full_suite": (
                full_suite.status.value if full_suite is not None and hasattr(full_suite.status, "value") else None
            ),
            "syntax": evaluation.syntax.passed if evaluation.syntax else None,
            "failures": failures[:3],
            "candidate_sha256": candidate_sha256,
        }
    return feedback


def _preflight_reachable_breakpoint(
    *,
    probe_source_dir: Path,
    parent_dir: Path,
    module_path: str,
    eligible_lines: tuple[int, ...],
    pdb_session_factory: Callable[[TaskWorkspace], PdbSession],
    emit: Callable[[str, dict[str, Any]], None],
) -> tuple[tuple[int, ...], dict[str, Any]]:
    """Retain the first source-ordered line reached by the real reproduction.

    Candidates are tried individually in fresh disposable PDB sessions and the
    search stops at the first pause.  This obeys the protocol's breakpoint-list
    bound, makes the ordering explicit, and exposes no verifier, oracle,
    reference repair, or hidden test content.
    """
    emit(
        "breakpoint_reachability_start",
        {
            "module_path": module_path,
            "structural_candidate_count": len(eligible_lines),
            "structural_candidates_sha256": _sha256(
                ",".join(str(line) for line in eligible_lines)
            ),
        },
    )
    attempts: list[dict[str, Any]] = []
    selected: Optional[tuple[int, str]] = None
    for candidate in eligible_lines:
        workspace = TaskWorkspace(
            str(probe_source_dir), parent_dir=str(parent_dir)
        )
        session: Optional[PdbSession] = None
        cleanup_errors: list[str] = []
        try:
            session = pdb_session_factory(workspace)
            session.start()
            result = session.start_paused_target(module_path, [candidate])
        finally:
            if session is not None:
                try:
                    session.stop()
                except Exception as exc:  # pragma: no cover - defensive evidence
                    cleanup_errors.append(
                        f"session.stop: {type(exc).__name__}: {exc}"
                    )
            try:
                workspace.cleanup()
            except Exception as exc:  # pragma: no cover - defensive evidence
                cleanup_errors.append(
                    f"workspace.cleanup: {type(exc).__name__}: {exc}"
                )
        if cleanup_errors:
            raise R5LauncherError(
                "breakpoint reachability preflight cleanup failed: "
                + "; ".join(cleanup_errors)
            )
        line = result.get("line")
        function = result.get("function")
        paused = (
            result.get("state") == "paused"
            and line == candidate
            and type(function) is str
            and function not in ("", "<module>")
        )
        attempts.append({
            "candidate": candidate,
            "state": result.get("state"),
            "line": line,
            "function": function,
            "paused_in_function": paused,
        })
        if paused:
            selected = (candidate, function)
            break
    if selected is None:
        raise R5LauncherError(
            "real reproduction did not reach any function-scoped structural "
            f"breakpoint candidate after {len(attempts)} bounded attempt(s)"
        )
    line, function = selected
    audit = {
        "method": "source_ordered_individual_real_reproduction",
        "structural_candidates": list(eligible_lines),
        "attempts": attempts,
        "selected_lines": [line],
        "selected_function": function,
        "state": result.get("state"),
        "cleanup": "cleaned",
        "oracle_or_verifier_consulted": False,
    }
    emit("breakpoint_reachability_complete", audit)
    return (line,), audit


def run_experiment(
    contract: dict[str, Any],
    transport: Any,
    output_dir: Path,
    *,
    task_id: str,
    pdb_session_factory: Callable[[TaskWorkspace], PdbSession],
    lifecycle_event: LifecycleEvent = None,
) -> dict[str, Any]:
    experiment_started = time.monotonic()
    experiment_started_at = _utc_now()

    def emit(event: str, details: dict[str, Any]) -> None:
        _emit_lifecycle(
            lifecycle_event,
            event,
            task_id=task_id,
            **details,
        )

    emit("run_experiment_start", {})
    fixture_dir = CURATED_ROOT / task_id
    task = load_task(str(fixture_dir / "task.json"))
    task_desc = _build_task_description(task)

    # --- Mandatory derivation order: original source FIRST ---
    module_path = task_target_module_path(task)
    original_source = (fixture_dir / module_path).read_text(encoding="utf-8")
    original_source_sha256 = hashlib.sha256(original_source.encode("utf-8")).hexdigest()
    original_source_line_count = len(original_source.splitlines())
    structural_eligible_lines = breakpoint_eligible_lines(original_source)
    if not structural_eligible_lines:
        raise R5LauncherError("no mechanically eligible breakpoint lines in original source")
    if any(
        line < 1 or line > original_source_line_count
        for line in structural_eligible_lines
    ):
        raise R5LauncherError("eligible breakpoint line escapes the original source region")

    case_dir = output_dir / f"case-{task_id}"
    case_dir.mkdir(parents=True, exist_ok=True)

    # --- THEN the disposable runtime copy with the neutral launcher ---
    emit("probe_materialization_start", {"case_dir": str(case_dir)})
    r5_probe = prepare_r5_probe(
        fixture_dir, module_path, task.reproduction.argv, case_dir,
        original_source_sha256=original_source_sha256,
        original_source_line_count=original_source_line_count,
        eligible_lines=structural_eligible_lines,
        task_id=task_id,
    )
    emit(
        "probe_materialization_complete",
        {"probe_source_dir": str(r5_probe.source_dir)},
    )
    if r5_probe.driver_start_line <= original_source_line_count:
        raise R5LauncherError("appended driver must start beyond the original source")

    if contract.get("model", {}).get("model_role"):
        eligible_lines = structural_eligible_lines
        breakpoint_reachability = {
            "method": "not_applied_to_scripted_training_transport",
            "structural_candidates": list(structural_eligible_lines),
            "selected_lines": list(structural_eligible_lines),
            "oracle_or_verifier_consulted": False,
        }
    else:
        eligible_lines, breakpoint_reachability = _preflight_reachable_breakpoint(
            probe_source_dir=r5_probe.source_dir,
            parent_dir=case_dir,
            module_path=module_path,
            eligible_lines=structural_eligible_lines,
            pdb_session_factory=pdb_session_factory,
            emit=emit,
        )

    emit("task_workspace_create_start", {"parent_dir": str(case_dir)})
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_dir))
    emit(
        "task_workspace_create_complete",
        {"workspace_name": Path(workspace.root).name},
    )
    context = DemoToolContext(
        task=task, workspace=workspace, patch="", probe=r5_probe.probe,
        pdb_session_factory=pdb_session_factory,
        verifier_feedback_fn=_make_verifier_feedback(
            module_path, original_source_line_count, emit
        ),
    )
    registry = build_registry(
        context, pdb_policy=PdbPolicy.ALWAYS_ON,
        interactive_debugger_controls=True,
    )

    stage_tracker = R5StageTracker(module_path, original_source_line_count)
    session_state_provider = make_r5_session_state_provider(context, lambda: stage_tracker.stage)

    contract_model = contract["model"]
    if contract_model.get("model_role"):
        # R6 training-trajectory generation: the transport is a scripted
        # perfect-protocol stand-in, never a real model; the run identity
        # must never be mistaken for a model-produced result.
        model_name = (
            f"{contract_model['base_repository']}"
            f"+{contract_model['base_revision'][:7]}"
            f"+{contract_model['model_role']}-R5"
        )
    elif contract_model.get("adapter_applied") is True:
        adapter_label = contract_model.get("adapter_label") or "peft"
        model_name = (
            f"{contract_model['base_repository']}"
            f"+{contract_model['base_revision'][:7]}"
            f"+{adapter_label}-R5"
        )
    else:
        model_name = (
            f"{contract_model['base_repository']}"
            f"+{contract_model['base_revision'][:7]}-R5"
        )

    inner_adapter = R5DebuggerBridgeAdapter(
        transport=transport,
        model_name=model_name,
        task_description=task_desc,
        script_path=module_path,
        source_text=original_source,
        eligible_lines=eligible_lines,
        original_line_count=original_source_line_count,
        session_state_provider=session_state_provider,
        stage_tracker=stage_tracker,
        max_retries=R5_BUDGETS["model_retries_per_logical_call_max"],
        request_timeout_seconds=R5_BUDGETS["model_request_timeout_seconds"],
        lifecycle_event=emit,
    )
    adapter = R5PhaseNavigationAdapter(inner_adapter)

    controller = DeterministicController(
        registry, adapter,
        ControllerRunConfig(max_model_calls=R5_BUDGETS["controller_steps_max"]),
    )

    snapshot = ControllerSnapshot(
        f"r5-{task_id}", task_id, ControllerState.REPRODUCE, 0,
        _contract_budget_limits(contract, task),
        ControllerBudgetState(), HypothesisLedger(),
    )

    run_start = time.monotonic()
    emit("controller_start", {})
    try:
        controller_result = controller.run(snapshot)
    except Exception as exc:
        controller_result = None
        controller_error = f"{type(exc).__name__}: {exc}"
    else:
        controller_error = None
    run_duration_ms = int((time.monotonic() - run_start) * 1000)
    emit(
        "controller_complete",
        {
            "duration_ms": run_duration_ms,
            "error_type": (
                controller_error.split(":", 1)[0] if controller_error else None
            ),
        },
    )
    if context.tool_errors:
        emit("tool_errors_observed", {"tool_errors": context.tool_errors})

    events_jsonl = ""
    if controller_result is not None:
        stream = io.StringIO()
        logger = JsonlEventLogger(
            controller_result.run_id, controller_result.task_id, stream=stream
        )
        try:
            for event in project_controller_run(
                controller_result,
                tool_version="debugger-interaction-v2-r5",
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
        final_candidate_sha256_for_log = hashlib.sha256(
            candidate_patch.encode("utf-8")
        ).hexdigest()
        emit(
            "final_verifier_start",
            {"candidate_sha256": final_candidate_sha256_for_log},
        )
        try:
            # Verifier workspace MUST be outside the repository tree: pytest's
            # rootdir discovery walks up from the workspace and would find the
            # repo's pyproject.toml, resolving fixture test node ids against
            # the repo root (baseline collection invalid). workspace_parent=None
            # uses the system temp dir (accepted verifier default).
            evaluation = EvaluationVerifier(
                str(REPO_ROOT), workspace_parent=None
            ).evaluate(task, candidate_patch)
            verifier_result = {
                "executed": True,
                "candidate_sha256": hashlib.sha256(candidate_patch.encode("utf-8")).hexdigest(),
                "status": evaluation.status.value if hasattr(evaluation.status, "value") else str(evaluation.status),
                "outcome": evaluation.outcome.value if hasattr(evaluation.outcome, "value") else str(evaluation.outcome),
                "stop_reason": evaluation.stop_reason,
                "f2p_records": [{"node_id": r.node_id, "status": r.status.value if hasattr(r.status, "value") else str(r.status)} for r in evaluation.post_patch_f2p],
                "p2p_records": [{"node_id": r.node_id, "status": r.status.value if hasattr(r.status, "value") else str(r.status)} for r in evaluation.post_patch_p2p],
                "f2p_total": evaluation.f2p_total,
                "f2p_passed": evaluation.f2p_passed,
                "p2p_total": evaluation.p2p_total,
                "p2p_passed": evaluation.p2p_passed,
                "full_suite_consistent": (
                    evaluation.full_suite.status.value if evaluation.full_suite is not None and hasattr(evaluation.full_suite.status, "value") else None
                ) == "PASS" if evaluation.full_suite is not None else None,
                "syntax_passed": evaluation.syntax.passed if evaluation.syntax else None,
                "patch_applied": evaluation.patch_application.success if evaluation.patch_application else None,
                "canonical_fixture_unchanged": (
                    evaluation.workspace.canonical_fixture_unchanged if evaluation.workspace else None
                ),
                "workspace_lifecycle": (
                    evaluation.workspace.lifecycle.value if evaluation.workspace and hasattr(evaluation.workspace.lifecycle, "value") else None
                ),
            }
            emit(
                "final_verifier_complete",
                {
                    "candidate_sha256": final_candidate_sha256_for_log,
                    "status": verifier_result.get("status"),
                    "outcome": verifier_result.get("outcome"),
                    "stop_reason": verifier_result.get("stop_reason"),
                },
            )
        except Exception as exc:
            verifier_result = {"executed": True, "error": f"{type(exc).__name__}: {exc}"}
            emit(
                "final_verifier_error",
                {
                    "candidate_sha256": final_candidate_sha256_for_log,
                    "error_type": type(exc).__name__,
                },
            )
    else:
        verifier_result = {"executed": False}
        emit("final_verifier_skipped", {"reason": "no_candidate_patch"})

    gate_chain = _compute_gate_r5_chain(
        inner_adapter.telemetry,
        events_jsonl,
        expected_script=module_path,
        original_line_count=original_source_line_count,
    )
    gate_patch = _compute_gate_r5_patch(
        inner_adapter.telemetry,
        events_jsonl,
        verifier_result,
        candidate_patch=candidate_patch,
        inner_adapter=inner_adapter,
        expected_script=module_path,
        original_line_count=original_source_line_count,
        f2p_count=len(task.tests.fail_to_pass),
        p2p_count=len(task.tests.pass_to_pass),
    )

    patch_attempts = getattr(inner_adapter, "patch_attempts", []) or []
    final_candidate_sha256 = None
    if candidate_patch:
        final_candidate_sha256 = hashlib.sha256(candidate_patch.encode("utf-8")).hexdigest()
    patch_identity = {
        "note": "A=raw_model_response, B=model_patch_raw (bridge-parsed diff), C=model_patch_serialization_normalized (metadata-only hunk-count normalization). PatchManager and EvaluationVerifier receive C only.",
        "attempts": patch_attempts,
        "final_candidate_sha256": final_candidate_sha256,
    }
    diagnosis_provenance = getattr(inner_adapter, "diagnosis_provenance", None)
    runtime_slice = getattr(inner_adapter, "runtime_slice", None) or {}

    # Cleanup of task-owned state (best-effort, recorded).
    cleanup_record: dict[str, Any] = {"release_pdb": [], "workspace_cleanup": None}
    emit("task_cleanup_start", {})
    try:
        cleanup_record["release_pdb"] = [
            str(exc) for exc in context.release_pdb()
        ]
    except Exception as exc:
        cleanup_record["release_pdb"] = [f"{type(exc).__name__}: {exc}"]
    try:
        workspace.cleanup()
        cleanup_record["workspace_cleanup"] = "cleaned"
    except Exception as exc:
        cleanup_record["workspace_cleanup"] = f"{type(exc).__name__}: {exc}"
    emit("task_cleanup_complete", {"cleanup": cleanup_record})

    total_duration_ms = int((time.monotonic() - experiment_started) * 1000)

    evidence: dict[str, Any] = {
        "schema_version": "debugger-interaction-v2-r5-evidence",
        "run_identity": _r5_run_identity(contract),
        "task": {
            "task_id": task_id,
            "bug_category": task.oracle.bug_category,  # reporting only; never in prompt
            "module_path": module_path,
            "original_source_sha256": original_source_sha256,
            "original_source_line_count": original_source_line_count,
            "eligible_breakpoint_lines": list(eligible_lines),
            "structural_breakpoint_lines": list(structural_eligible_lines),
            "breakpoint_reachability": breakpoint_reachability,
            "runtime_appended_driver_start_line": r5_probe.driver_start_line,
            "launcher_sha256": _sha256(
                build_r5_launcher_source(task.reproduction.argv)
            ),
            "f2p_count": len(task.tests.fail_to_pass),
            "p2p_count": len(task.tests.pass_to_pass),
        },
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
        "verifier_feedback_history": list(getattr(context, "verifier_feedback_history", []) or []),
        "budget_limits": {
            "max_patch_attempts": _contract_budget_limits(contract, task).max_patch_attempts,
            "max_test_runs": _contract_budget_limits(contract, task).max_test_runs,
            "max_pdb_observations": _contract_budget_limits(contract, task).max_pdb_observations,
        },
        "gate_results": {"gate_chain": gate_chain, "gate_patch": gate_patch},
        "interface_info": {
            "script_path": module_path,
            "eligible_lines": list(eligible_lines),
            "system_prompt_sha256": inner_adapter.system_prompt_sha256,
            "system_prompt_template_sha256": _sha256(SYSTEM_PROMPT_TEMPLATE),
            "task_description": task_desc,
        },
        "cleanup": cleanup_record,
        "tool_errors": context.tool_errors,
        "runtime": {
            "started_at": experiment_started_at,
            "controller_duration_ms": run_duration_ms,
            "total_duration_ms": total_duration_ms,
        },
        "claims_boundary": (
            "R5 generalized debugger-informed patch treatment.  R5 PASS = "
            "chain A-F (original-region, provenance-bound; the R5.9 "
            "production-exception path truthfully reports G2=None when the "
            "production frame unwound during a real exception/failure and "
            "never claims an original-region G2) + exact diagnosis "
            "retained into bounded PATCH + model B diff applied via real "
            "PatchManager + independent EvaluationVerifier COMPLETED/RESOLVED "
            "with all task F2P/P2P PASS + full-suite consistency + syntax + "
            "canonical unchanged + workspaces cleaned + zero hidden-test "
            "leakage in every actual prompt (sanitized diagnostics + "
            "region-filtered stacks + fail-closed actual-prompt audit).  "
            "Administrative PATCH->VALIDATE->DONE closeout is harness "
            "bookkeeping, not model capability."
        ),
    }

    evidence_path = output_dir / "evidence.json"
    emit(
        "evidence_write_start",
        {"path": str(evidence_path), "total_duration_ms": total_duration_ms},
    )
    evidence_path.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    emit(
        "evidence_write_complete",
        {"path": str(evidence_path), "bytes": evidence_path.stat().st_size},
    )
    return evidence


# ---------------------------------------------------------------------------
# First-pause latency measurement (deterministic, model-free)
# ---------------------------------------------------------------------------


def measure_first_pause_latency_ms(task_id: str, probe_timeout: float = 120.0) -> dict[str, Any]:
    """Measure real start_paused_target latency for one task under the
    generalized pytest launcher, using the first eligible line that pauses.

    Deterministic validation only; never part of the live treatment.
    """
    fixture_dir = CURATED_ROOT / task_id
    task = load_task(str(fixture_dir / "task.json"))
    module_path = task_target_module_path(task)
    original_source = (fixture_dir / module_path).read_text(encoding="utf-8")
    original_source_sha256 = hashlib.sha256(original_source.encode("utf-8")).hexdigest()
    original_source_line_count = len(original_source.splitlines())
    eligible_lines = breakpoint_eligible_lines(original_source)

    import tempfile
    with tempfile.TemporaryDirectory(prefix="r5-latency-") as tmp:
        parent = Path(tmp)
        r5_probe = prepare_r5_probe(
            fixture_dir, module_path, task.reproduction.argv, parent,
            original_source_sha256=original_source_sha256,
            original_source_line_count=original_source_line_count,
            eligible_lines=eligible_lines,
            task_id=task_id,
        )
        attempts: list[dict[str, Any]] = []
        for line in eligible_lines:
            workspace = TaskWorkspace(str(r5_probe.source_dir), parent_dir=str(parent))
            session = PdbSession(
                workspace,
                startup_timeout=probe_timeout,
                request_timeout=probe_timeout,
                shutdown_timeout=10.0,
            )
            try:
                session.start()
                started_at = time.monotonic()
                result = session.start_paused_target(module_path, [line])
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                if result.get("state") == "paused":
                    attempts.append({
                        "line": line, "paused": True,
                        "function": result.get("function"),
                        "latency_ms": elapsed_ms,
                    })
                    return {
                        "task_id": task_id,
                        "module_path": module_path,
                        "original_source_line_count": original_source_line_count,
                        "paused_line": line,
                        "latency_ms": elapsed_ms,
                        "attempts": attempts,
                    }
                attempts.append({"line": line, "paused": False, "state": result.get("state"), "latency_ms": elapsed_ms})
            finally:
                try:
                    session.stop()
                except Exception:
                    pass
                try:
                    workspace.cleanup()
                except Exception:
                    pass
        return {
            "task_id": task_id,
            "module_path": module_path,
            "original_source_line_count": original_source_line_count,
            "paused_line": None,
            "latency_ms": None,
            "attempts": attempts,
        }


# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------


def _first_causal_failure(evidence: dict[str, Any], task_id: str) -> str:
    """Classify the FIRST causal boundary from gate/controller evidence."""
    telemetry = evidence.get("telemetry") or []
    controller = evidence.get("controller_result") or {}
    verifier = evidence.get("verifier") or {}
    gates = evidence.get("gate_results") or {}

    accepted_pdb = [
        t for t in telemetry
        if t.get("parse_result", {}).get("status") == "accepted"
        and t.get("translated_directive", {}).get("action_name") in _PDB_ACTIONS
    ]
    reproduction_ok = any(
        t.get("translated_directive", {}).get("action_name") == "run_reproduction"
        and t.get("parse_result", {}).get("status") == "accepted"
        for t in telemetry
    )
    start_ok = any(
        t.get("translated_directive", {}).get("action_name") == "start_pdb_session"
        and t.get("parse_result", {}).get("status") == "accepted"
        for t in telemetry
    )
    stack_ok = any(
        t.get("translated_directive", {}).get("action_name") == "get_stack_summary"
        and t.get("parse_result", {}).get("status") == "accepted"
        for t in telemetry
    )
    inspect_ok = any(
        t.get("translated_directive", {}).get("action_name") in ("get_frame_locals", "safe_eval_expression")
        and t.get("parse_result", {}).get("status") == "accepted"
        for t in telemetry
    )
    step_ok = any(
        t.get("translated_directive", {}).get("action_name") in ("step_pdb_session", "next_pdb_session")
        and t.get("parse_result", {}).get("status") == "accepted"
        for t in telemetry
    )
    diagnosis_ok = any(
        t.get("translated_directive", {}).get("is_diagnosis") is True
        and t.get("parse_result", {}).get("status") == "accepted"
        for t in telemetry
    )
    patch_ok = any(
        t.get("translated_directive", {}).get("action_name") == "apply_patch"
        and t.get("parse_result", {}).get("status") == "accepted"
        for t in telemetry
    )

    if controller.get("error"):
        return "harness defect"
    if not reproduction_ok:
        return "reproduction"
    if not start_ok:
        return "debugger entrypoint"
    chain = gates.get("gate_chain") or {}
    if not chain.get("passed"):
        reason = chain.get("reason", "")
        if "no paused production-region break" in reason or "start_pdb_session observation is not OK" in reason:
            return "breakpoint selection"
        if "stack" in reason and "G1" in reason:
            return "stack"
        if "locals" in reason or "safe_eval" in reason or "inspection" in reason:
            return "state inspection"
        if "step" in reason or "next" in reason:
            if "PAUSED" in reason or "post-step" in reason or "G2" in reason:
                return "post-step pause"
            if "terminal" in reason or "reproduction failure output" in reason or "outside the original region" in reason:
                return "post-step pause"
            return "step/control"
        if "diagnosis" in reason:
            return "diagnosis"
        return "debugger entrypoint"
    if not diagnosis_ok:
        return "diagnosis"
    if not patch_ok:
        return "patch generation"
    if not evidence.get("candidate_patch"):
        return "patch apply"
    patch_gate = gates.get("gate_patch") or {}
    if not patch_gate.get("passed"):
        reason = patch_gate.get("reason", "")
        if "normalization" in reason or "fingerprint" in reason or "serialization" in reason:
            return "patch serialization"
        if "retained" in reason or "diagnosis" in reason:
            return "diagnosis transfer"
        if "PatchManager" in reason or "verifier input" in reason or "verifier did not execute" in reason:
            return "verifier"
        if "RESOLVED" in reason or "F2P" in reason or "P2P" in reason or "full-suite" in reason or "syntax" in reason:
            return "semantic repair"
        return "patch apply"
    if verifier.get("status") == "COMPLETED" and verifier.get("outcome") == "RESOLVED":
        return "none"
    return "semantic repair"


def _matrix_row(evidence: dict[str, Any], task_id: str, contract_sha: str, contract: dict[str, Any]) -> dict[str, Any]:
    chain = evidence.get("gate_results", {}).get("gate_chain") or {}
    patch_gate = evidence.get("gate_results", {}).get("gate_patch") or {}
    verifier = evidence.get("verifier") or {}
    task_meta = evidence.get("task") or {}
    telemetry = evidence.get("telemetry") or []
    controller = evidence.get("controller_result") or {}
    model = contract.get("model", {})

    def _first_of(kind: str) -> Optional[dict[str, Any]]:
        for t in telemetry:
            td = t.get("translated_directive", {}) or {}
            if t.get("parse_result", {}).get("status") == "accepted":
                if kind == "break" and td.get("action_name") == "start_pdb_session":
                    return t
                if kind == "inspection" and td.get("action_name") in ("get_frame_locals", "safe_eval_expression"):
                    return t
                if kind == "step" and td.get("action_name") in ("step_pdb_session", "next_pdb_session"):
                    return t
        return None

    break_rec = _first_of("break")
    inspect_rec = _first_of("inspection")
    step_rec = _first_of("step")
    patch_attempts = (evidence.get("patch_identity") or {}).get("attempts") or []
    last_attempt = patch_attempts[-1] if patch_attempts else {}
    norm = last_attempt.get("normalization") or {}

    return {
        "task_id": task_id,
        "bug_category": task_meta.get("bug_category"),
        "model_identity": f"{model.get('base_repository')}@{model.get('base_revision')}",
        "treatment_hash": contract_sha,
        "system_prompt_sha256": (evidence.get("interface_info") or {}).get("system_prompt_sha256"),
        "reproduction_success": any(
            t.get("translated_directive", {}).get("action_name") == "run_reproduction"
            and t.get("parse_result", {}).get("status") == "accepted"
            for t in telemetry
        ),
        "breakpoint_line": (break_rec or {}).get("translated_directive", {}).get("arguments", {}).get("breakpoint_line"),
        "G1": chain.get("G1"),
        "terminal_path": bool(chain.get("terminal_path")),
        "production_exception_path": bool(chain.get("production_exception_path")),
        "step_outside_region": bool(chain.get("step_outside_region")),
        "inspection_command": (inspect_rec or {}).get("translated_directive", {}).get("action_name"),
        "inspection_type": (inspect_rec or {}).get("translated_directive", {}).get("action_name"),
        "step_next_command": (step_rec or {}).get("translated_directive", {}).get("action_name"),
        "G2": chain.get("G2"),
        "diagnosis_present": bool((evidence.get("diagnosis_provenance") or {}).get("diagnosis_text_sha256")),
        "B_sha": last_attempt.get("model_patch_raw_sha256"),
        "normalization_required": bool(last_attempt) and (norm.get("header_fields_changed") or 0) > 0,
        "C_sha": last_attempt.get("model_patch_serialization_normalized_sha256"),
        "patch_applied": bool(evidence.get("candidate_patch")),
        "verifier_status": verifier.get("status"),
        "verifier_outcome": verifier.get("outcome"),
        "f2p": f"{verifier.get('f2p_passed', '?')}/{verifier.get('f2p_total', '?')}",
        "p2p": f"{verifier.get('p2p_passed', '?')}/{verifier.get('p2p_total', '?')}",
        "full_suite": verifier.get("full_suite_consistent"),
        "first_causal_failure": _first_causal_failure(evidence, task_id),
        "per_task_pass": bool(patch_gate.get("passed")),
        "controller_final_state": controller.get("final_state"),
        "controller_stop_reason": controller.get("stop_reason"),
        "model_calls": controller.get("model_calls"),
    }


def _clean_holdout_5_of_5(
    primary_target_5_of_5: bool,
    prompt_audit_passed: bool,
    leakage_findings: int,
) -> bool:
    """The explicit fail-closed clean-holdout authority (R5.9 closeout).

    CLEAN 5/5 holds ONLY when every row passes its strict per-task gate AND
    the fail-closed actual-prompt anti-leakage audit is empty.
    """
    return bool(
        primary_target_5_of_5
        and prompt_audit_passed
        and leakage_findings == 0
    )


def run_matrix(contract: dict[str, Any], output_dir: Path, *, transport_factory: Callable[[], Any]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    contract_sha = _contract_sha256(contract)
    pdb_timeout = contract["budgets"]["pdb_request_timeout_seconds"]

    def session_factory(workspace: TaskWorkspace) -> PdbSession:
        return PdbSession(
            workspace,
            startup_timeout=float(pdb_timeout),
            request_timeout=float(pdb_timeout),
            shutdown_timeout=2.0,
        )

    transport = transport_factory()
    rows: list[dict[str, Any]] = []
    per_task_evidence: dict[str, str] = {}
    for task_id in R5_TASKS:
        case_output = output_dir / task_id
        case_output.mkdir(parents=True, exist_ok=True)
        evidence = run_experiment(
            contract, transport, case_output,
            task_id=task_id,
            pdb_session_factory=session_factory,
        )
        per_task_evidence[task_id] = str(case_output / "evidence.json")
        rows.append(_matrix_row(evidence, task_id, contract_sha, contract))

    aggregate = {
        "end_to_end_resolved": sum(1 for r in rows if r["verifier_outcome"] == "RESOLVED"),
        "tasks_total": len(R5_TASKS),
        "debugger_chain_success": sum(1 for r in rows if r["G2"] is not None),
        "terminal_path_success": sum(1 for r in rows if r["terminal_path"]),
        "production_exception_path_success": sum(1 for r in rows if r["production_exception_path"]),
        "step_outside_region_success": sum(1 for r in rows if r["step_outside_region"]),
        "inspection_success": sum(1 for r in rows if r["inspection_command"] is not None),
        "step_post_step_success": sum(1 for r in rows if r["G2"] is not None),
        "diagnosis_success": sum(1 for r in rows if r["diagnosis_present"]),
        "semantic_patch_produced": sum(1 for r in rows if r["B_sha"] is not None),
        "patch_applied": sum(1 for r in rows if r["patch_applied"]),
        "verifier_resolved": sum(1 for r in rows if r["verifier_outcome"] == "RESOLVED"),
        "verifier_resolved_conditional_on_patch": sum(
            1 for r in rows if r["patch_applied"] and r["verifier_outcome"] == "RESOLVED"
        ),
        "patch_applied_total": sum(1 for r in rows if r["patch_applied"]),
        "b_applied_unchanged": sum(1 for r in rows if r["B_sha"] is not None and not r["normalization_required"]),
        "b_to_c_normalized": sum(1 for r in rows if r["B_sha"] is not None and r["normalization_required"]),
        "failures_by_first_causal_boundary": _aggregate_boundaries(rows),
        "additional_non_off_by_one_resolved": sum(
            1 for r in rows
            if r["task_id"] != "curated-off-by-one-002"
            and r["verifier_outcome"] == "RESOLVED"
        ),
        "primary_target_5_of_5": all(r["per_task_pass"] for r in rows),
        "secondary_milestone_2_additional": sum(
            1 for r in rows
            if r["task_id"] != "curated-off-by-one-002"
            and r["verifier_outcome"] == "RESOLVED"
        ) >= 2,
    }

    # R5.9: fail-closed ACTUAL-PROMPT anti-leakage audit of every row.
    # AUDIT-ONLY: reads the already-written evidence (exact prompts) against
    # the forbidden content derived from the fixture evaluation assets;
    # never runs during prompt construction.
    from experiments.debugger_interaction_v2_r5.anti_leakage import audit_matrix_dir

    anti_leakage = audit_matrix_dir(output_dir, CURATED_ROOT)
    aggregate["leakage_findings"] = anti_leakage["leakage_findings_total"]
    aggregate["clean_holdout_prompt_audit_passed"] = anti_leakage["passed"]
    # R5.9 closeout: the explicit fail-closed clean-holdout authority.  The
    # scientific result is CLEAN 5/5 ONLY when every row passes its strict
    # per-task gate AND the actual-prompt audit is empty.
    aggregate["clean_holdout_5_of_5"] = _clean_holdout_5_of_5(
        aggregate.get("primary_target_5_of_5"),
        aggregate.get("clean_holdout_prompt_audit_passed"),
        aggregate.get("leakage_findings"),
    )

    matrix = {
        "schema_version": "debugger-interaction-v2-r5-matrix",
        "contract_sha256": contract_sha,
        "treatment_revision": contract.get("interface_revision"),
        "model": contract["model"],
        "budgets": contract["budgets"],
        "timeout_derivation": contract.get("timeout_derivation"),
        "rows": rows,
        "aggregate": aggregate,
        "per_task_evidence": per_task_evidence,
        "anti_leakage": anti_leakage,
        "claims_boundary": (
            "Machine-readable R5 matrix.  A row is PASS only when the strict "
            "per-task gate (chain A-F original-region + retained diagnosis + "
            "PatchManager/verifier candidate identity + COMPLETED/RESOLVED + "
            "all F2P/P2P + full-suite + syntax + canonical unchanged + "
            "cleaned) passes.  The anti_leakage section is the fail-closed "
            "audit of every exact actual prompt (leakage_findings == [] "
            "required).  No row is hidden."
        ),
    }
    (output_dir / "matrix.json").write_text(
        json.dumps(matrix, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    return matrix


def _aggregate_boundaries(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        boundary = r["first_causal_failure"]
        counts[boundary] = counts.get(boundary, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _run_anti_leakage_audit(output_dir: Path) -> int:
    """Fail-closed ACTUAL-PROMPT anti-leakage audit of a matrix output dir.

    AUDIT-ONLY: examines every exact ``telemetry[*].request.user_prompt_full``
    in the already-written evidence files against the forbidden content
    derived from the actual fixture evaluation assets.  Never runs during
    prompt construction.  Writes ``anti-leakage.json`` next to the matrix
    and exits 1 when any finding exists.
    """
    from experiments.debugger_interaction_v2_r5.anti_leakage import audit_matrix_dir

    audit = audit_matrix_dir(output_dir, CURATED_ROOT)
    out_path = output_dir / "anti-leakage.json"
    out_path.write_text(
        json.dumps(audit, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "PASS" if audit["passed"] else "FAIL",
        "scanned_prompt_count": audit["scanned_prompt_count"],
        "leakage_findings_total": audit["leakage_findings_total"],
        "audit_path": str(out_path),
    }, indent=2, ensure_ascii=False))
    return 0 if audit["passed"] else 1


def _model_specs() -> dict[str, dict[str, Any]]:
    """Frozen model-identity registry for the runner.

    ``raw7b`` is the accepted RAW Qwen2.5-Coder-7B identity (r5.1-r5.3).
    ``coder14b`` is the model-identity escalation (r5.4): the same treatment
    with the stronger Qwen2.5-Coder-14B base; results are labeled with the
    14B identity, never as RAW-7B results.
    ``cp118`` is the R6 matched condition: the historical project-fine-tuned
    checkpoint cp118 (QLoRA adapter on the SAME 7B base revision) under the
    IDENTICAL frozen r5.9 clean-holdout treatment; results are labeled with
    the cp118 adapter identity, never as RAW-7B results.
    """
    from experiments.debugger_interaction_v2_r5 import transport_14b, transport_cp118
    return {
        "raw7b": {
            "contract_name": "r5_contract.json",
            "transport_class": LocalRawQwenTransport,
            "repo": BASE_REPOSITORY,
            "revision": BASE_REVISION,
            "generation": GENERATION_CONFIG,
            "requires_adapter_path": False,
        },
        "coder14b": {
            "contract_name": "r5_contract_14b.json",
            "transport_class": transport_14b.LocalQwen14BTransport,
            "repo": transport_14b.BASE_REPOSITORY,
            "revision": transport_14b.BASE_REVISION,
            "generation": transport_14b.GENERATION_CONFIG,
            "requires_adapter_path": False,
        },
        "cp118": {
            "contract_name": "r5_contract_cp118.json",
            "transport_class": transport_cp118.LocalQwenPeftTransport,
            "repo": transport_cp118.BASE_REPOSITORY,
            "revision": transport_cp118.BASE_REVISION,
            "generation": transport_cp118.GENERATION_CONFIG,
            "requires_adapter_path": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="R5 — generalized debugger-informed repair matrix"
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--measure-latency", action="store_true")
    parser.add_argument("--audit-dir", type=str, default=None,
                        help="fail-closed ACTUAL-PROMPT anti-leakage audit of a matrix output dir (writes anti-leakage.json; strictly post-run, never part of prompt construction)")
    parser.add_argument("--task", type=str, default=None,
                        help="run a single pre-registered task (affected-set rerun under a repaired revision)")
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--model", type=str, default="raw7b",
                        choices=("raw7b", "coder14b", "cp118"),
                        help="frozen model identity (raw7b = accepted RAW base; coder14b = escalation identity; cp118 = matched project-fine-tuned adapter)")
    parser.add_argument("--adapter-path", type=str, default=None,
                        help="required for --model cp118: path to the frozen cp118 PEFT adapter directory (identity-verified against the frozen contract)")
    args = parser.parse_args()

    if not (args.validate_only or args.run or args.measure_latency or args.task or args.audit_dir):
        parser.error("select --validate-only, --measure-latency, --run, --task, or --audit-dir")

    if args.audit_dir:
        return _run_anti_leakage_audit(Path(args.audit_dir))

    specs = _model_specs()
    spec = specs[args.model]
    if spec.get("requires_adapter_path") and args.adapter_path is None:
        parser.error(f"--model {args.model} requires --adapter-path")
    if not spec.get("requires_adapter_path") and args.adapter_path is not None:
        parser.error(f"--model {args.model} does not accept --adapter-path")
    contract = _load_contract(spec["contract_name"])
    transport_class = spec["transport_class"]
    adapter_path = args.adapter_path

    def validate() -> dict[str, Any]:
        return _validate_contract(
            contract, repo=spec["repo"], revision=spec["revision"],
            gen=spec["generation"], adapter_path=adapter_path,
        )

    def build_transport() -> Any:
        kwargs: dict[str, Any] = {}
        if spec.get("requires_adapter_path"):
            kwargs["adapter_path"] = adapter_path
        return transport_class(**kwargs)

    if args.validate_only:
        validation = validate()
        identity = _r5_run_identity(contract, adapter_path)
        source_commit_sha = identity.get("source_commit_sha")
        validation = dict(validation)
        validation["source_commit_sha"] = source_commit_sha
        validation["experiment_contract_sha256"] = identity.get("experiment_contract_sha256")
        validation["runtime_python"] = identity.get("runtime_python")
        validation["system_prompt_template_sha256"] = identity.get("system_prompt_template_sha256")
        validation["model_identity"] = args.model
        validation["adapter_identity"] = identity.get("adapter_identity")
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

    if args.measure_latency:
        results = []
        for task_id in R5_TASKS:
            result = measure_first_pause_latency_ms(task_id)
            results.append(result)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        latencies = [r["latency_ms"] for r in results if r.get("latency_ms") is not None]
        if len(latencies) != len(R5_TASKS):
            print(json.dumps({
                "status": "FAIL",
                "reason": "not all tasks produced a real first pause",
                "results": results,
            }, indent=2, ensure_ascii=False))
            return 1
        max_latency = max(latencies)
        derived = derive_common_pdb_timeout(max_latency)
        print(json.dumps({
            "status": "PASS",
            "max_first_pause_latency_ms": max_latency,
            "per_task_latency_ms": {r["task_id"]: r["latency_ms"] for r in results},
            "derived_pdb_request_timeout_seconds": derived,
            "rule": "ceil(max_ms * 3 / 1000 / 5) * 5, floor 15",
        }, indent=2, ensure_ascii=False))
        return 0

    if args.task:
        if not args.output_dir:
            parser.error("--output-dir is required for --task")
        if args.task not in R5_TASKS:
            parser.error(f"--task must be one of the pre-registered tasks: {R5_TASKS}")
        validate()
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        pdb_timeout = contract["budgets"]["pdb_request_timeout_seconds"]

        def session_factory(workspace: TaskWorkspace) -> PdbSession:
            return PdbSession(
                workspace,
                startup_timeout=float(pdb_timeout),
                request_timeout=float(pdb_timeout),
                shutdown_timeout=2.0,
            )

        case_output = output_dir / args.task
        case_output.mkdir(parents=True, exist_ok=True)
        evidence = run_experiment(
            contract, build_transport(), case_output,
            task_id=args.task,
            pdb_session_factory=session_factory,
        )
        print(json.dumps({
            "status": "COMPLETE",
            "task_id": args.task,
            "interface_revision": contract.get("interface_revision"),
            "gate_chain": evidence["gate_results"]["gate_chain"],
            "gate_patch": evidence["gate_results"]["gate_patch"],
            "evidence_path": str(case_output / "evidence.json"),
        }, indent=2, ensure_ascii=False))
        return 0

    if args.run:
        if not args.output_dir:
            parser.error("--output-dir is required for --run")
        validate()
        output_dir = Path(args.output_dir)
        matrix = run_matrix(
            contract, output_dir,
            transport_factory=build_transport,
        )
        print(json.dumps({
            "status": "COMPLETE",
            "matrix_path": str(output_dir / "matrix.json"),
            "aggregate": matrix["aggregate"],
        }, indent=2, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
