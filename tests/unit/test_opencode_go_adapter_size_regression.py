"""Blocker A mandatory regression: the Local Application request ceiling.

The historical QuixBugs campaign 20,000-byte public-evidence budget must not
be silently inherited into Local Application V1.  Two independent proofs:

1. The measured ``curated-none-handling-001`` ``pdb-on-uncertainty``
   reference trajectory (21 Local Application model requests, canonical
   compact JSON sizes measured independently) is reconstructed
   deterministically with EXACT byte counts and must be fully admitted by
   the Local Application ceiling ``MAX_PUBLIC_REQUEST_BYTES`` (25,000),
   while the exact constructed prompt plus the simulated Windows command
   line stays below the separate 30,000-character guard.

2. The REAL deterministic configured-command trajectory for
   ``curated-none-handling-001`` under ``pdb-on-uncertainty`` is driven
   through a recording transport / deterministic model fixture (the real
   controller, tool registry, PDB probe, and disposable workspace; ZERO
   provider contact).  Every generated request must pass
   ``build_protocol_message``; request count and the maximum canonical
   request bytes, constructed prompt bytes, and simulated Windows
   command-line characters are recorded.

The configured limit + 1 fails closed.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scripts import opencode_go_command_adapter as adapter

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.demo.runner import CURATED_RELATIVE_ROOT, load_task, prepare_pdb_probe
from agentic_debugger.demo.tools import DemoToolContext, build_registry
from agentic_debugger.evaluation.live import (
    LiveModelAdapter,
    LiveModelConfig,
    LiveRunLimits,
)
from agentic_debugger.runtime.workspace import TaskWorkspace

TASK_ID = "curated-none-handling-001"
POLICY = "pdb-on-uncertainty"

#: The measured canonical request sizes of the accepted configured-command
#: reference trajectory (21 Local Application model requests).
MEASURED_REFERENCE_BYTES = [
    3_225, 6_811, 9_949, 9_348, 11_070, 12_808, 13_501, 13_536, 14_732, 15_935,
    16_594, 17_578, 18_580, 19_717, 19_375, 20_488, 20_903, 21_741, 22_601,
    23_287, 23_824,
]


# --- Measured reference trajectory reconstruction --------------------------

def _observation(name: str, payload: Mapping[str, Any], summary: str, index: int) -> dict[str, Any]:
    """A realistic protocol-1.3 observation record (the shape the accepted
    LiveModelAdapter embeds into history entries)."""
    return {
        "observation_id": f"obs-{index}",
        "action_id": name,
        "run_id": f"{TASK_ID}--{POLICY}",
        "task_id": TASK_ID,
        "name": name,
        "status": "completed",
        "payload": dict(payload),
        "summary": summary,
        "truncated": False,
    }


def _observation_for(call_index: int, size_class: str) -> dict[str, Any]:
    """The current-observation content for one call.

    The measured trajectory's size variance is dominated by the CURRENT
    observation (source windows and PDB frame/eval dumps are large;
    transition observations are compact) — this is what produces the two
    measured dips (calls 4 and 14)."""
    if size_class == "large":
        return _observation(
            "get_source_window",
            {"path": "display_name.py", "start_line": 1, "end_line": 60, "source": "def format_display_name(first, last):\n    if first is None:\n        return last\n    if last is None:\n        return first\n    return first + ' ' + last\n\n\ndef parse_user(row):\n    return format_display_name(row.get('first'), row.get('last'))\n"},
            "source window with the defect region and the failing caller",
            call_index,
        )
    if size_class == "medium":
        return _observation(
            "safe_eval_expression",
            {
                "frame_id": 0,
                "pause_generation": 1,
                "expression": "locals().get('first')",
                "result": "None",
                "frame_locals": {"first": "None", "last": "'Doe'", "prefix": "None"},
            },
            "frame locals and evaluated expression",
            call_index,
        )
    return _observation(
        "run_reproduction",
        {"phase": "baseline", "failure_reproduced": True},
        "baseline failure reproduced",
        call_index,
    )


def _compact_history_observation(index: int) -> dict[str, Any]:
    """The compact observation embedded in history entries (bounded history
    summaries, as produced by the accepted bounded history contract)."""
    return {
        "observation_id": f"obs-{index}",
        "action_id": "run_reproduction",
        "run_id": f"{TASK_ID}--{POLICY}",
        "task_id": TASK_ID,
        "name": "run_reproduction",
        "status": "completed",
        "payload": {"phase": "baseline"},
        "summary": "baseline failure reproduced",
        "truncated": True,
    }


_CONTRACT_RUN_REPRODUCTION = {"properties": {"phase": {"type": "string", "enum": ["baseline", "post_patch"]}}, "required": ["phase"], "additional_properties": False}
_CONTRACT_FIND_FUNCTION = {"properties": {"name": {"type": "string"}, "path": {"type": "string"}}, "required": ["name", "path"], "additional_properties": False}
_CONTRACT_GET_SOURCE_WINDOW = {"properties": {"path": {"type": "string"}, "line": {"type": "integer"}}, "required": ["path", "line"], "additional_properties": False}
_CONTRACT_ADD_HYPOTHESIS = {"properties": {"hypothesis_id": {"type": "string"}, "statement": {"type": "string"}, "confidence": {"type": "string", "enum": ["low", "medium", "high"]}, "evidence_refs": {"type": "array"}, "requires_runtime_evidence": {"type": "boolean"}}, "required": ["hypothesis_id", "statement", "confidence", "evidence_refs", "requires_runtime_evidence"], "additional_properties": False}
_CONTRACT_EXPRESS = {"properties": {"hypothesis_id": {"type": "string"}, "statement": {"type": "string"}, "target_file": {"type": "string"}, "target_symbol": {"type": "string"}, "confidence": {"type": "string"}}, "required": ["hypothesis_id", "statement", "target_file", "target_symbol", "confidence"], "additional_properties": False}
_CONTRACT_EMPTY = {"properties": {}, "additional_properties": False}
_CONTRACT_FRAME_LOCALS = {"properties": {"frame_id": {"type": "integer"}, "pause_generation": {"type": "integer"}}, "required": ["frame_id", "pause_generation"], "additional_properties": False}
_CONTRACT_SAFE_EVAL = {"properties": {"frame_id": {"type": "integer"}, "pause_generation": {"type": "integer"}, "expression": {"type": "string"}}, "required": ["frame_id", "pause_generation", "expression"], "additional_properties": False}
_CONTRACT_REVISE_HYPOTHESIS = {"properties": {"hypothesis_id": {"type": "string"}, "statement": {"type": "string"}, "confidence": {"type": "string", "enum": ["low", "medium", "high"]}, "evidence_refs": {"type": "array"}, "requires_runtime_evidence": {"type": "boolean"}}, "required": ["hypothesis_id", "statement", "confidence", "evidence_refs", "requires_runtime_evidence"], "additional_properties": False}
_CONTRACT_APPLY_PATCH = {"properties": {"patch": {"type": "string", "min_length": 1}}, "required": ["patch"], "additional_properties": False}


def _state_shape(state: str) -> tuple[list[str], list[str], dict[str, Any]]:
    """Per-state directive schema, allowed actions, and action contracts —
    mirroring the accepted LiveModelAdapter's state-specific request content
    (the reference trajectory's call-0 request is 3,225 bytes precisely
    because only the current state's contracts are embedded)."""
    if state == "Reproduce":
        return (
            ["action", "transition"],
            ["run_reproduction"],
            {"run_reproduction": _CONTRACT_RUN_REPRODUCTION},
        )
    if state == "Understand":
        return (
            ["action", "transition", "add_hypothesis", "revise_hypothesis", "set_hypothesis_status"],
            ["find_function", "get_source_window", "add_hypothesis", "express_root_cause_hypothesis", "revise_hypothesis"],
            {
                "find_function": _CONTRACT_FIND_FUNCTION,
                "get_source_window": _CONTRACT_GET_SOURCE_WINDOW,
                "add_hypothesis": _CONTRACT_ADD_HYPOTHESIS,
                "express_root_cause_hypothesis": _CONTRACT_EXPRESS,
                "revise_hypothesis": _CONTRACT_REVISE_HYPOTHESIS,
            },
        )
    if state == "RuntimeEvidence":
        return (
            ["action", "transition", "revise_hypothesis", "set_hypothesis_status"],
            ["start_pdb_session", "get_stack_summary", "get_frame_locals", "safe_eval_expression", "stop_pdb_session", "revise_hypothesis"],
            {
                "start_pdb_session": _CONTRACT_EMPTY,
                "get_stack_summary": _CONTRACT_EMPTY,
                "get_frame_locals": _CONTRACT_FRAME_LOCALS,
                "safe_eval_expression": _CONTRACT_SAFE_EVAL,
                "stop_pdb_session": _CONTRACT_EMPTY,
                "revise_hypothesis": _CONTRACT_REVISE_HYPOTHESIS,
            },
        )
    if state == "Patch":
        return (
            ["action", "transition"],
            ["apply_patch", "syntax_check"],
            {"apply_patch": _CONTRACT_APPLY_PATCH, "syntax_check": _CONTRACT_EMPTY},
        )
    return (
        ["action", "transition"],
        ["run_reproduction", "run_regression_tests", "classify_outcome"],
        {
            "run_reproduction": _CONTRACT_RUN_REPRODUCTION,
            "run_regression_tests": _CONTRACT_EMPTY,
            "classify_outcome": _CONTRACT_EMPTY,
        },
    )


def _reconstructed_request(
    index: int, state: str, history: list[dict[str, Any]], *, last_observation: Any = None
) -> dict[str, Any]:
    """One protocol-1.3 request in the shape of the accepted
    LiveModelAdapter ``_request_context`` payload."""
    schema, allowed, contracts = _state_shape(state)
    legal_targets = {
        "Reproduce": ["Understand", "Failed"],
        "Understand": ["RuntimeEvidence", "Patch", "Failed"],
        "RuntimeEvidence": ["Understand", "Patch", "Failed"],
        "Patch": ["Validate", "Failed"],
        "Validate": ["Done", "Failed"],
    }.get(state, ["Failed"])
    hypotheses: list[dict[str, Any]] = []
    if index >= 5 and state in ("Understand", "RuntimeEvidence", "Patch", "Validate"):
        hypotheses = [{
            "hypothesis_id": "h-none-handling",
            "statement": "format_display_name mishandles a None first argument",
            "confidence": "low",
            "evidence_refs": ["observation:find_function", "observation:get_source_window", "observation:get_stack_summary", "observation:get_frame_locals", "observation:safe_eval_expression"],
            "requires_runtime_evidence": index < 14,
        }]
    return {
        "protocol": {
            "name": "agentic-debugger-live-jsonl",
            "version": "1.3",
            "request_id": f"{TASK_ID}--{POLICY}:model-call:{index}:attempt:1:uuid-reconstructed",
            "logical_model_call_index": index,
            "transport_attempt_index": 1,
        },
        "identity": {
            "evaluation_id": f"eval-{TASK_ID}",
            "case_id": f"eval-{TASK_ID}:{TASK_ID}",
            "run_id": f"{TASK_ID}--{POLICY}",
            "trajectory_id": f"{TASK_ID}--{POLICY}",
        },
        "task": {
            "task_id": TASK_ID,
            "instruction": "Fix the None handling defect in display_name formatting",
        },
        "policy": POLICY,
        "directive_schema": schema,
        "action_contracts": contracts,
        "controller": {
            "state": state,
            "task_id": TASK_ID,
            "model_call_index": index,
            "allowed_actions": allowed,
            "legal_transition_targets": legal_targets,
            "budget_limits": {
                "max_patch_attempts": 3,
                "max_test_runs": 10,
                "max_pdb_observations": 15,
                "max_active_hypotheses": 3,
                "max_source_observations": 10,
            },
            "budget_state": {
                "patch_attempts": 0,
                "test_runs": 0,
                "pdb_observations": min(max(index - 6, 0), 8),
                "source_observations": min(max(index - 2, 0), 6),
            },
            "hypotheses": hypotheses,
            "last_observation": copy.deepcopy(last_observation),
        },
        "history": history,
        "directive_feedback": None,
        "instructions": "Return one directive JSON object. The request is the complete bounded current context; do not rely on process-local memory. Never return credentials.",
    }


def build_measured_reference_trajectory() -> tuple[list[dict[str, Any]], list[int]]:
    """Reconstruct the measured 21-request trajectory with EXACT canonical
    byte counts.  History entries carry compact bounded observation summaries
    (the measured trajectory's size variance is dominated by the current
    observation: source windows and PDB frame/eval dumps are large,
    transition observations are compact — this produces the two measured
    dips at calls 4 and 14).  The current observation's summary is padded so
    the canonical serialization is byte-exact with the measured table
    (deterministic, never a tiny hand-written request)."""
    states = [
        "Reproduce", "Reproduce", "Understand", "Understand", "Understand",
        "Understand", "Understand", "RuntimeEvidence", "RuntimeEvidence",
        "RuntimeEvidence", "RuntimeEvidence", "RuntimeEvidence", "Understand",
        "Understand", "Patch", "Patch", "Patch", "Validate", "Validate",
        "Validate", "Validate",
    ]
    # Per-call current-observation size class (large/medium/small).
    observation_classes = [
        "small", "large", "large", "small", "large", "medium", "small",
        "large", "medium", "small", "large", "medium", "medium", "small",
        "small", "large", "medium", "medium", "small", "large", "medium",
    ]
    requests: list[dict[str, Any]] = []
    sizes: list[int] = []
    history: list[dict[str, Any]] = []
    for index in range(1, 22):
        state = states[index - 1]
        entry_index = index - 1
        _, allowed, _ = _state_shape(state)
        history = list(history)
        history.append({
            "request_index": index,
            "state": state,
            "allowed_actions": list(allowed),
            "last_observation": _compact_history_observation(entry_index),
        })
        del history[:-32]
        target = MEASURED_REFERENCE_BYTES[index - 1]
        current: int | None = None
        chosen: dict[str, Any] | None = None
        for size_class in (observation_classes[index - 1], "small"):
            candidate = _observation_for(index, size_class)
            request = _reconstructed_request(index, state, history, last_observation=candidate)
            candidate_size = len(adapter.canonical_public_request(request).encode("utf-8"))
            if candidate_size <= target:
                current = candidate_size
                chosen = candidate
                break
        assert current is not None and chosen is not None, (
            f"call {index}: even a compact observation exceeds measured size; "
            "adjust the base templates"
        )
        delta = target - current
        if delta > 0:
            request["controller"]["last_observation"]["summary"] = (
                chosen["summary"] + "x" * delta
            )
        actual = len(adapter.canonical_public_request(request).encode("utf-8"))
        assert actual == target, f"call {index}: {actual} != {target}"
        requests.append(request)
        sizes.append(actual)
    return requests, sizes


def test_measured_21_call_reference_trajectory_admitted_by_ceiling() -> None:
    requests, sizes = build_measured_reference_trajectory()
    assert len(requests) == 21
    assert sizes == MEASURED_REFERENCE_BYTES

    max_canonical = 0
    max_prompt = 0
    max_command_line = 0
    for request, size in zip(requests, sizes):
        message = adapter.build_protocol_message(request)  # must succeed
        canonical = len(adapter.canonical_public_request(request).encode("utf-8"))
        max_canonical = max(max_canonical, canonical)
        max_prompt = max(max_prompt, len(message.encode("utf-8")))
        command = [
            "C:/tools/opencode-ai/bin/opencode.exe", "run", message, "--pure",
            "--format", "json", "--model", "opencode-go/deepseek-v4-pro",
            "--dir", "C:/Users/test/AppData/Local/Temp/opencode-go-run-abcdef",
        ]
        max_command_line = max(max_command_line, len(subprocess.list2cmdline(command)))

    # The measured trajectory maximum is 23,824 bytes: the historical
    # 20,000-byte campaign budget would have rejected calls 15+ even with
    # perfect model responses; the Local Application ceiling admits all 21.
    assert max_canonical == 23_824
    assert max_canonical > 20_000, "regression does not exceed the historical budget"
    assert max_canonical <= adapter.MAX_PUBLIC_REQUEST_BYTES
    assert max_prompt < adapter.MAX_NATIVE_COMMAND_LINE_CHARS
    assert max_command_line < adapter.MAX_NATIVE_COMMAND_LINE_CHARS
    assert max_command_line > max_prompt, "command line quoting is simulated"
    adapter._MAX_MEASURED_PROMPT_BYTES = max_prompt  # recorded for the report
    adapter._MAX_MEASURED_COMMAND_LINE_CHARS = max_command_line


def test_ceiling_plus_one_fails_closed_on_measured_max() -> None:
    requests, sizes = build_measured_reference_trajectory()
    last = dict(requests[-1])
    # Grow the largest measured request (23,824) to exactly ceiling + 1:
    # the configured limit + 1 must fail closed.
    last["_pad"] = ""
    current = len(adapter.canonical_public_request(last).encode("utf-8"))
    last["_pad"] = "x" * (adapter.MAX_PUBLIC_REQUEST_BYTES + 1 - current)
    assert len(adapter.canonical_public_request(last).encode("utf-8")) == adapter.MAX_PUBLIC_REQUEST_BYTES + 1
    with pytest.raises(ValueError, match="exceeds the Local Application ceiling"):
        adapter.build_protocol_message(last)


# --- Real deterministic configured-command trajectory ----------------------

class RecordingScriptedTransport:
    """A recording transport / deterministic model fixture that mirrors the
    accepted pdb-on-uncertainty decision model phase machine, reading every
    decision input (including the PDB pause generation and tool outcomes)
    from the incoming request's controller snapshot and last observation."""

    def __init__(self, patch_text: str, scenario: Any) -> None:
        self.requests: list[dict[str, Any]] = []
        self.patch_text = patch_text
        self.scenario = scenario
        self._phase = "reproduce"

    def request(self, payload: Mapping[str, Any], timeout_seconds: float) -> Mapping[str, Any]:
        self.requests.append(dict(payload))
        directive = self._next_directive(payload)
        return {
            "directive": directive,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def _observation(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        controller = payload.get("controller") if isinstance(payload.get("controller"), Mapping) else {}
        observation = controller.get("last_observation")
        if not isinstance(observation, Mapping):
            return {}
        return observation

    def _payload_of(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        observation = self._observation(payload)
        inner = observation.get("payload")
        return inner if isinstance(inner, Mapping) else {}

    def _observation_ok(self, payload: Mapping[str, Any]) -> bool:
        observation = self._observation(payload)
        return bool(observation) and observation.get("status") == "completed"

    def _next_directive(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        controller = payload.get("controller") if isinstance(payload.get("controller"), Mapping) else {}
        state = controller.get("state")
        scenario = self.scenario
        if state == "Reproduce":
            if self._phase == "reproduce":
                self._phase = "reproduce-check"
                return {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
            self._phase = "understand-locate"
            return {"kind": "transition", "target_state": "Understand", "reason": "declared failure reproduced"}
        if state == "Understand":
            if self._phase == "understand-locate":
                self._phase = "understand-window"
                return {"kind": "action", "name": "find_function", "arguments": {"name": scenario.localization.symbol, "path": scenario.localization.file_path}}
            if self._phase == "understand-window":
                self._phase = "understand-hypothesis"
                return {"kind": "action", "name": "get_source_window", "arguments": {"path": scenario.localization.file_path, "line": 1}}
            if self._phase == "understand-hypothesis":
                self._phase = "understand-declare"
                return {"kind": "add_hypothesis", "hypothesis_id": scenario.hypothesis_id, "statement": scenario.root_cause_statement, "confidence": "low", "evidence_refs": ["observation:find_function", "observation:get_source_window"], "requires_runtime_evidence": True}
            if self._phase == "understand-declare":
                self._phase = "understand-gate"
                return {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": scenario.hypothesis_id, "statement": scenario.root_cause_statement, "target_file": scenario.localization.file_path, "target_symbol": scenario.localization.symbol, "confidence": "low"}}
            if self._phase == "understand-gate":
                # The request's legal transition targets are authoritative
                # (they already encode the PDB gate decision): transition to
                # RuntimeEvidence only when it is advertised as legal.
                targets = controller.get("legal_transition_targets")
                if isinstance(targets, list) and "RuntimeEvidence" in targets:
                    self._phase = "runtime-start"
                    return {"kind": "transition", "target_state": "RuntimeEvidence", "reason": "pdb gate allowed runtime evidence"}
                self._phase = "patch-apply"
                return {"kind": "transition", "target_state": "Patch", "reason": "pdb gate withheld runtime evidence"}
            if self._phase == "understand-revise":
                self._phase = "patch-apply"
                return {"kind": "revise_hypothesis", "hypothesis_id": scenario.hypothesis_id, "statement": scenario.root_cause_statement, "confidence": "low", "evidence_refs": ["observation:find_function", "observation:get_source_window", "observation:get_stack_summary", "observation:get_frame_locals", "observation:safe_eval_expression"], "requires_runtime_evidence": False}
            self._phase = "patch-apply"
            return {"kind": "transition", "target_state": "Patch", "reason": "bounded runtime evidence collected; diagnosis unchanged"}
        if state == "RuntimeEvidence":
            if self._phase == "runtime-start":
                self._phase = "runtime-stack"
                return {"kind": "action", "name": "start_pdb_session", "arguments": {}}
            if self._phase == "runtime-stack":
                self._phase = "runtime-locals"
                return {"kind": "action", "name": "get_stack_summary", "arguments": {}}
            if self._phase == "runtime-locals":
                self._phase = "runtime-eval"
                generation = self._payload_of(payload).get("pause_generation")
                self._pause_generation = generation if type(generation) is int and generation > 0 else 1
                self._eval_index = 0
                return {"kind": "action", "name": "get_frame_locals", "arguments": {"frame_id": 0, "pause_generation": self._pause_generation}}
            if self._phase == "runtime-eval":
                expressions = list(scenario.runtime_probe.inspect_expressions)
                if self._eval_index < len(expressions):
                    expression = expressions[self._eval_index]
                    self._eval_index += 1
                    return {"kind": "action", "name": "safe_eval_expression", "arguments": {"frame_id": 0, "pause_generation": self._pause_generation, "expression": expression}}
                self._phase = "runtime-exit"
                return {"kind": "action", "name": "stop_pdb_session", "arguments": {}}
            self._phase = "understand-revise"
            return {"kind": "transition", "target_state": "Understand", "reason": "bounded runtime evidence collected"}
        if state == "Patch":
            if self._phase == "patch-apply":
                self._phase = "patch-syntax"
                return {"kind": "action", "name": "apply_patch", "arguments": {"patch": self.patch_text}}
            if self._phase == "patch-syntax":
                self._phase = "patch-validate"
                return {"kind": "action", "name": "syntax_check", "arguments": {}}
            self._phase = "validate-reproduce"
            return {"kind": "transition", "target_state": "Validate", "reason": "candidate patch applied and syntax checked"}
        if state == "Validate":
            if self._phase == "validate-reproduce":
                self._phase = "validate-regression"
                return {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "post_patch"}}
            if self._phase == "validate-regression":
                self._phase = "validate-classify"
                return {"kind": "action", "name": "run_regression_tests", "arguments": {}}
            if self._phase == "validate-classify":
                self._phase = "validate-finish"
                return {"kind": "action", "name": "classify_outcome", "arguments": {}}
            self._phase = "finished"
            return {"kind": "transition", "target_state": "Done", "reason": "controller validation classified the candidate as RESOLVED"}
        raise AssertionError(f"unexpected state {state!r} in phase {self._phase!r}")


def run_real_trajectory(tmp_path: Path) -> tuple[list[dict[str, Any]], Any]:
    """Drive the real controller pipeline for curated-none-handling-001 under
    pdb-on-uncertainty through the recording transport; returns the recorded
    requests and the controller run result."""
    scenario = scenario_for(TASK_ID)
    fixture_dir = REPO_ROOT / CURATED_RELATIVE_ROOT / TASK_ID
    task = load_task(str(fixture_dir / "task.json"))

    case_parent = tmp_path / f"case-{TASK_ID}-{POLICY}"
    case_parent.mkdir(parents=True, exist_ok=False)
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(case_parent))
    source_path = Path(workspace.root) / scenario.reference_repair.target_path
    patch_text = build_reference_patch(
        source_path.read_text(encoding="utf-8"), scenario.reference_repair
    )
    probe = prepare_pdb_probe(fixture_dir, scenario, case_parent)
    context = DemoToolContext(
        task=task,
        workspace=workspace,
        patch=patch_text,
        probe=probe,
    )
    registry = build_registry(context)
    transport = RecordingScriptedTransport(patch_text, scenario)
    config = LiveModelConfig(model_name="recording-fixture", command=[sys.executable, "-c", "pass"], request_timeout_seconds=30.0)
    limits = LiveRunLimits(max_model_requests=64, max_controller_steps=64, max_retries=0)
    model = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=config,
        transport=transport,
        limits=limits,
        registry=registry,
        evaluation_id=f"eval-{TASK_ID}",
        case_id=f"eval-{TASK_ID}:{TASK_ID}",
        run_id=f"{TASK_ID}--{POLICY}",
        trajectory_id=f"{TASK_ID}--{POLICY}",
    )
    controller = DeterministicController(
        registry,
        model,
        ControllerRunConfig(max_model_calls=64),
    )
    snapshot = ControllerSnapshot(
        f"{TASK_ID}--{POLICY}",
        TASK_ID,
        ControllerState.REPRODUCE,
        0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(),
        HypothesisLedger(),
    )
    result = controller.run(snapshot)
    return transport.requests, result


def test_real_configured_command_trajectory_requests_all_build(tmp_path: Path) -> None:
    requests, result = run_real_trajectory(tmp_path)
    assert requests, "recording transport received no requests"
    assert result.final_state is ControllerState.DONE, (
        f"real trajectory did not complete: {result.final_state.value} ({result.stop_reason})"
    )
    assert result.model_calls == len(requests)

    max_canonical = 0
    max_prompt = 0
    max_command_line = 0
    for request in requests:
        message = adapter.build_protocol_message(request)  # mandatory: must succeed
        canonical = len(adapter.canonical_public_request(request).encode("utf-8"))
        max_canonical = max(max_canonical, canonical)
        max_prompt = max(max_prompt, len(message.encode("utf-8")))
        command = [
            "C:/tools/opencode-ai/bin/opencode.exe", "run", message, "--pure",
            "--format", "json", "--model", "opencode-go/deepseek-v4-pro",
            "--dir", "C:/Users/test/AppData/Local/Temp/opencode-go-run-abcdef",
        ]
        max_command_line = max(max_command_line, len(subprocess.list2cmdline(command)))

    # Record the measured real-trajectory facts (reported in validation.md).
    adapter._REAL_TRAJECTORY = {
        "request_count": len(requests),
        "max_canonical_request_bytes": max_canonical,
        "max_constructed_prompt_bytes": max_prompt,
        "max_simulated_command_line_chars": max_command_line,
    }
    assert max_canonical <= adapter.MAX_PUBLIC_REQUEST_BYTES
    assert max_prompt < adapter.MAX_NATIVE_COMMAND_LINE_CHARS
    assert max_command_line < adapter.MAX_NATIVE_COMMAND_LINE_CHARS
