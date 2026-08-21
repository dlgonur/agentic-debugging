from __future__ import annotations

import json
from pathlib import Path

from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import (
    LiveModelConfig,
    LiveRunLimits,
    run_live_case,
)


ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "pdb-required-boundary-006"


class DeterministicConfiguredTransport:
    """A zero-provider JSON transport exercising the real live adapter."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self._index = 0

    def request(self, payload, timeout_seconds):
        del timeout_seconds
        self.requests.append(json.loads(json.dumps(payload)))
        directives = [
            {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
            {"kind": "transition", "target_state": "Understand", "reason": "baseline reproduced"},
            {"kind": "action", "name": "find_function", "arguments": {"name": "tail_window", "path": "window_tail.py"}},
            {"kind": "action", "name": "get_source_window", "arguments": {"path": "window_tail.py", "line": 1}},
            {"kind": "add_hypothesis", "hypothesis_id": "proof-boundary-006", "statement": scenario_for(TASK_ID).root_cause_statement, "confidence": "low", "evidence_refs": ["observation:find_function", "observation:get_source_window"], "requires_runtime_evidence": True},
            {"kind": "transition", "target_state": "RuntimeEvidence", "reason": "exact PDB proof is required"},
            {"kind": "action", "name": "start_pdb_session", "arguments": {"breakpoint_line": 9}},
            {"kind": "action", "name": "get_stack_summary", "arguments": {}},
            {"kind": "action", "name": "get_frame_locals", "arguments": {"frame_id": 0, "pause_generation": 1}},
            {"kind": "action", "name": "step_pdb_session", "arguments": {}},
            {"kind": "action", "name": "stop_pdb_session", "arguments": {}},
            {"kind": "transition", "target_state": "Understand", "reason": "PDB proof observations collected"},
            None,
            None,
            {"kind": "transition", "target_state": "Patch", "reason": "diagnosis is bound to exact runtime observations"},
            {"kind": "action", "name": "apply_patch", "arguments": {"patch": "--- a/window_tail.py\n+++ b/window_tail.py\n@@ -7,4 +7,4 @@\n     start_index = max(item_count - requested_size, 0)\n     end_index = item_count\n-    selected = values[start_index:end_index - (1 if requested_size == item_count else 0)]\n+    selected = values[start_index:end_index]\n     return selected\n"}},
            {"kind": "action", "name": "syntax_check", "arguments": {}},
            {"kind": "transition", "target_state": "Validate", "reason": "candidate syntax is valid"},
            {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "post_patch"}},
            {"kind": "action", "name": "run_regression_tests", "arguments": {}},
            {"kind": "action", "name": "classify_outcome", "arguments": {}},
            {"kind": "transition", "target_state": "Done", "reason": "independent validation evidence is complete"},
        ]
        directive = directives[self._index]
        self._index += 1
        if directive is not None:
            return directive

        observations = [
            entry.get("controller", {}).get("last_observation")
            for entry in self.requests
            if isinstance(entry.get("controller", {}).get("last_observation"), dict)
        ]
        proof_observations = [
            entry for entry in observations
            if entry.get("name") in {"start_pdb_session", "get_stack_summary", "get_frame_locals", "step_pdb_session"}
        ]
        refs = [entry["observation_id"] for entry in proof_observations]
        if self._index == 13:
            return {"kind": "revise_hypothesis", "hypothesis_id": "proof-boundary-006", "statement": scenario_for(TASK_ID).root_cause_statement, "confidence": "low", "evidence_refs": refs, "requires_runtime_evidence": False}
        locals_observation = next(entry for entry in proof_observations if entry["name"] == "get_frame_locals")
        requested = next(item for item in locals_observation["payload"]["locals"] if item["name"] == "requested_size")
        start = next(entry for entry in proof_observations if entry["name"] == "start_pdb_session")
        return {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "proof-boundary-006", "statement": scenario_for(TASK_ID).root_cause_statement, "target_file": "window_tail.py", "target_symbol": "tail_window", "confidence": "low", "evidence_refs": refs, "observed_values": {"requested_size": requested["value"]}}}


def run_synthetic_live_case(tmp_path: Path):
    transport = DeterministicConfiguredTransport()
    result = run_live_case(
        repository_root=str(ROOT),
        task_id=TASK_ID,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        repetition=1,
        workspace_parent=str(tmp_path),
        config=LiveModelConfig("deterministic-configured-model", ("synthetic-zero-provider",)),
        limits=LiveRunLimits(
            max_model_requests=24,
            max_controller_steps=24,
            max_model_phase_seconds=600,
            max_retries=0,
            continue_on_task_failure=False,
        ),
        transport=transport,
        evaluation_id="synthetic-proof",
        retain_observable_model_directives=True,
    )
    return result, transport


def test_real_live_case_uses_zero_provider_and_completes_exact_pdb_proof(tmp_path: Path) -> None:
    result, transport = run_synthetic_live_case(tmp_path)
    assert result.status.value == "RESOLVED"
    assert result.controller["completed"] is True
    assert result.verifier["outcome"] == "RESOLVED"
    assert result.verifier["canonical_fixture_unchanged"] is True
    assert result.verifier["workspace_cleaned"] is True
    measurements = result.measurements
    assert measurements["logical_model_call_count"] == 22
    assert measurements["transport_attempt_count"] == 22
    assert measurements["logical_model_call_count"] == len(transport.requests)
    assert measurements["logical_model_call_count"] <= 24
    assert measurements["transport_attempt_count"] <= 24
    assert measurements["cumulative_request_bytes"] <= 600_000
    assert measurements["max_request_bytes"] <= 50_000
    assert measurements["retry_count"] == 0
    assert measurements["provider_error_count"] == 0
    assert measurements["termination_reason"] is None
    assert measurements["controller_wall_duration_ms"] >= 0
    assert measurements["verifier_wall_duration_ms"] >= 0

    transition_request_pairs = [
        (request, request.get("controller", {}).get("legal_transition_targets", []), directive)
        for request, directive in zip(transport.requests, [entry.get("directive") for entry in result.evidence["observable_model_directives"]])
    ]
    runtime_request = next(item for item in transition_request_pairs if item[2].get("target_state") == "RuntimeEvidence")
    assert "Patch" not in runtime_request[1]
    patch_request = next(item for item in transition_request_pairs if item[2].get("target_state") == "Patch")
    assert "Patch" in patch_request[1]
    assert "apply_patch" not in runtime_request[0]["controller"]["allowed_actions"]

    provider_visible = json.dumps(transport.requests, sort_keys=True)
    assert "oracle" not in provider_visible
    assert "private_verifier" not in provider_visible
    assert "reference_repair" not in provider_visible
